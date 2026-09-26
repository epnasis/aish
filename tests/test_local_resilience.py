"""What a `local:` call sends to stay out of a loop, and what it does when the
server goes away under it (#417, #419).

No server, no network. Where the openai SDK's own behaviour is the thing being
relied on — how it wraps a dropped connection, how it surfaces mlx-lm's HTTP 404
— the real SDK is driven over an httpx `MockTransport`, so every failure is
produced where a real one would be and wrapped exactly as mi's would be.
"""

import json

import httpx
import openai
import pytest

from aish import agent as agent_module
from aish import backends, ratelimit
from aish.agent import Agent
from aish.backends import BackendError, make_chat
from tests.test_local_provider import REPO, URL, FakeClient, _delta, _finish, local_env
from tests.test_repetition import LOOP, PREAMBLE

__all__ = ["local_env"]  # the fixture, re-exported so pytest finds it here


# ------------------------------------------------------------------ sampling


class TestLocalSampling:
    """mlx-lm 0.31.3 is greedy unless the request says otherwise (`--temp`
    defaults to 0.0) and has no server flag for presence_penalty, so every
    `local:` request states its sampling (#417)."""

    def _sent(self, stream=False):
        client = FakeClient(stream_chunks=[_finish()])
        chat, _, _ = make_chat(f"local:{REPO}", client=client)
        out = chat(model=REPO, messages=[{"role": "user", "content": "x"}], stream=stream)
        if stream:
            list(out)
        return client.calls[0]["extra_body"]

    @pytest.mark.parametrize("stream", [False, True])
    def test_the_defaults_ride_every_request(self, local_env, stream):
        body = self._sent(stream)
        assert {k: body[k] for k in backends.DEFAULT_LOCAL_SAMPLING} == {
            "temperature": 1.0, "top_p": 0.95, "top_k": 20, "presence_penalty": 1.5,
        }

    def test_the_owner_replaces_them_whole(self, local_env):
        local_env.setenv("AISH_LOCAL_SAMPLING", '{"temperature": 0.7, "min_p": 0.05}')
        assert self._sent() == {
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": 0.7,
            "min_p": 0.05,
        }

    def test_an_empty_object_sends_none(self, local_env):
        local_env.setenv("AISH_LOCAL_SAMPLING", "{}")
        assert self._sent() == {"chat_template_kwargs": {"enable_thinking": False}}

    @pytest.mark.parametrize(
        "value",
        ["hot", "[1.0]", '{"temprature": 1.0}', '{"top_k": 20.5}', '{"temperature": "1"}',
         '{"temperature": true}'],
    )
    def test_anything_it_cannot_mean_is_a_clear_error(self, local_env, value):
        local_env.setenv("AISH_LOCAL_SAMPLING", value)
        with pytest.raises(BackendError, match="AISH_LOCAL_SAMPLING"):
            make_chat(f"local:{REPO}", client=FakeClient())

    def test_the_sent_record_shows_what_went(self, local_env):
        seen = []
        with backends.observe_sent(seen.append):
            self._sent()
        (request,) = seen
        assert request.payload["extra_body"]["presence_penalty"] == 1.5

    def test_cloud_providers_send_none(self):
        client = FakeClient()
        backends.OpenAICompatBackend(client, "openai")(
            model="gpt", messages=[{"role": "user", "content": "x"}]
        )
        assert not {"temperature", "top_k", "extra_body"} & set(client.calls[0])


# ---------------------------------------- a stopped stream closes its response


class _ClosableStream:
    """What the openai SDK's `Stream` offers: iteration and `close()`."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self):
        self.closed = True


class TestAStoppedStreamClosesItsResponse:
    """mlx-lm stops generating only when a write to the client fails
    (`handle_completion`, 0.31.3), so a reply aish stops reading must close the
    HTTP response rather than wait for the garbage collector to (#417)."""

    def test_a_repeating_reply_closes_the_response(self, local_env):
        def endless():
            yield _delta(reasoning=PREAMBLE)
            while True:
                yield _delta(reasoning=LOOP)

        stream = _ClosableStream(endless())
        answer = [_delta(content="done"), _finish()]
        client = FakeClient(streams=[answer])
        create = client.chat.completions.create
        client.chat.completions.create = (
            lambda **kw: (client.calls.append(kw), stream)[1] if not client.calls else create(**kw)
        )
        chat, _, _ = make_chat(f"local:{REPO}", client=client)
        steps: list[dict] = []
        agent = Agent(model=REPO, approve=lambda _c: True, client_chat=chat,
                      on_token=lambda _t: None, step_log=steps.append)
        agent.provider = "local"
        assert agent.run_task("go") == "done"
        assert stream.closed
        first = next(s for s in steps if s.get("kind") == "reasoning")
        assert first["repetition"]["period_chars"] == len(LOOP)


# -------------------------------------------- lost connections (#419)

OVER_WINDOW = (
    "prompt of 120000 tokens + max_tokens 16384 exceeds the context length 100000 "
    "this server admits"
)
_CHUNK = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": REPO}


def _sse(*events, done=True) -> bytes:
    body = b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events)
    return body + (b"data: [DONE]\n\n" if done else b"")


def _answer_body(text="recovered") -> bytes:
    return _sse(
        {**_CHUNK, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {**_CHUNK, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    )


class _Server:
    """The real openai SDK over an httpx MockTransport. `script` is consumed
    one request at a time; its last entry repeats forever."""

    def __init__(self, *script):
        self.script = list(script)
        self.bodies: list[dict] = []

        def handle(request):
            self.bodies.append(json.loads(request.content))
            step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            if isinstance(step, Exception):
                raise step
            status, body = step
            kind = "text/event-stream" if status == 200 else "application/json"
            return httpx.Response(status, headers={"content-type": kind}, content=body)

        self.client = openai.OpenAI(
            base_url=URL, api_key="x", max_retries=backends.SDK_RETRIES,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )


def _dropped():
    return httpx.RemoteProtocolError("Server disconnected without sending a response.")


def _refused():
    return httpx.ConnectError("[Errno 61] Connection refused")


def _local_agent(server: _Server, *, history_chars=9000, provider="local"):
    if provider == "local":
        chat, _, _ = make_chat(f"local:{REPO}", client=server.client)
    else:
        chat = backends.governed(backends.OpenAICompatBackend(server.client, provider), provider)
    steps: list[dict] = []
    agent = Agent(model=REPO, approve=lambda _c: True, client_chat=chat,
                  on_token=lambda _t: None, step_log=steps.append)
    agent.provider = provider
    if history_chars:
        agent.messages.append(
            {"role": "tool", "tool_name": "run_command", "content": "y" * history_chars}
        )
    return agent, steps


def _errors(steps):
    return [s for s in steps if s.get("kind") == "model_error"]


class TestLocalLostConnection:
    """A `local:` request that loses its connection after it was sent is re-sent
    unchanged once, then once smaller, then the turn ends saying what was seen
    — never re-sent for two minutes into a server it may be crashing (#419)."""

    def test_a_server_that_drops_every_request_gets_three_sends(self, local_env):
        server = _Server(_dropped())
        agent, steps = _local_agent(server)
        with pytest.raises(agent_module.ModelUnavailable) as caught:
            agent.run_task("hi")

        assert len(server.bodies) == 3
        first, second, third = server.bodies
        assert second == first  # one identical re-send
        assert len(json.dumps(third["messages"])) < len(json.dumps(first["messages"]))

        errors = _errors(steps)
        assert [e["lost_connection"] for e in errors] == [1, 2, 3]
        assert [e["action"] for e in errors] == ["retry", "retry", "give_up"]
        assert errors[-1]["bound"] == "lost_connection"
        assert all(
            e["exception_chain"][:2] == ["APIConnectionError", "RemoteProtocolError"]
            for e in errors
        )
        assert all("elapsed_s" in e for e in errors)
        (trim,) = [s for s in steps if s.get("kind") == "trim"]
        assert trim["policy"] == "lost_connection_oldest_first"
        # The record order is the order things happened: the failure, then
        # the shrink it answered, then the next failure.
        assert steps.index(errors[1]) < steps.index(trim) < steps.index(errors[2])

        said = str(caught.value)
        assert "lost on 3 sends of the same model call" in said
        assert "send 2: the same request" in said
        assert "send 3: after the history was shortened" in said
        assert "RemoteProtocolError: Server disconnected without sending a response." in said
        assert "aish does not know why" in said

    def test_one_lost_connection_is_re_sent_unchanged_and_recovers(self, local_env):
        server = _Server(_dropped(), (200, _answer_body()))
        agent, steps = _local_agent(server)
        assert agent.run_task("hi") == "recovered"
        assert len(server.bodies) == 2 and server.bodies[0] == server.bodies[1]
        assert not [s for s in steps if s.get("kind") == "trim"]

    def test_refused_connections_are_not_counted(self, local_env):
        """A connection never made never delivered the request, so it cannot be
        the request that brought the server down — and it is exactly what a
        restarting server answers. Those keep the ordinary retry."""
        server = _Server(_dropped(), _refused(), _refused(), _refused(), (200, _answer_body()))
        agent, steps = _local_agent(server)
        assert agent.run_task("hi") == "recovered"
        assert len(server.bodies) == 5
        assert all(body == server.bodies[0] for body in server.bodies)
        errors = _errors(steps)
        assert [e.get("lost_connection") for e in errors] == [1, None, None, None]
        assert errors[1]["exception_chain"][:2] == ["APIConnectionError", "ConnectError"]

    def test_a_second_loss_with_nothing_to_shorten_ends_the_turn(self, local_env):
        server = _Server(_dropped())
        agent, steps = _local_agent(server, history_chars=0)
        with pytest.raises(agent_module.ModelUnavailable) as caught:
            agent.run_task("hi")
        assert len(server.bodies) == 2
        assert _errors(steps)[-1]["bound"] == "lost_connection"
        assert "nothing left to shorten" in str(caught.value)
        assert "aish does not know why" in str(caught.value)

    def test_a_cut_off_stream_counts_as_a_lost_connection(self, local_env):
        cut = _sse(
            {**_CHUNK, "choices": [{"index": 0, "delta": {"content": "par"},
                                    "finish_reason": None}]},
            done=False,
        )
        server = _Server((200, cut))
        agent, steps = _local_agent(server)
        with pytest.raises(agent_module.ModelUnavailable):
            agent.run_task("hi")
        assert len(server.bodies) == 3
        assert [e["matched"] for e in _errors(steps)] == ["stream_cut_off"] * 3

    def test_other_providers_keep_the_plain_retry(self, local_env):
        server = _Server(_dropped())
        agent, steps = _local_agent(server, provider="openai")
        with pytest.raises(agent_module.ModelUnavailable):
            agent.run_task("hi")
        assert len(server.bodies) > 3
        assert _errors(steps)[-1]["bound"] == "wait_budget"
        assert not any("lost_connection" in e for e in _errors(steps))
        assert not [s for s in steps if s.get("kind") == "trim"]


class TestLocalOverWindowRefusal:
    """mlx-lm answers a request it cannot hold with an HTTP 404 and
    `{"error": "<message>"}` (`handle_completion`, 0.31.3). With a message that
    says `exceeds the context length` that is the one 4xx aish answers by
    sending a smaller request (#388) — checked through the real openai SDK."""

    def test_the_sdk_error_is_classified_as_an_overflow(self, local_env):
        server = _Server((404, json.dumps({"error": OVER_WINDOW}).encode()))
        chat, _, _ = make_chat(f"local:{REPO}", client=server.client)
        with pytest.raises(openai.NotFoundError) as caught:
            list(chat(model=REPO, messages=[{"role": "user", "content": "x"}], stream=True))
        assert OVER_WINDOW in str(caught.value)
        failure = ratelimit.classify(caught.value)
        assert failure.kind == ratelimit.CONTEXT_OVERFLOW
        assert failure.status == 404
        assert failure.matched == "exceeds the context length"

    def test_it_is_shrunk_and_retried_once(self, local_env):
        server = _Server((404, json.dumps({"error": OVER_WINDOW}).encode()),
                         (200, _answer_body()))
        agent, steps = _local_agent(server)
        assert agent.run_task("hi") == "recovered"
        assert len(server.bodies) == 2
        assert len(json.dumps(server.bodies[1]["messages"])) < len(
            json.dumps(server.bodies[0]["messages"])
        )
        (error,) = _errors(steps)
        assert error["class"] == ratelimit.CONTEXT_OVERFLOW and error["status"] == 404
        (trim,) = [s for s in steps if s.get("kind") == "trim"]
        assert trim["policy"] == "overflow_oldest_first"
