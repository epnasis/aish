"""Web server tests: the same FakeChat pattern as test_agent.py, driven over
a real WebSocket via Starlette's TestClient (which runs the app's event loop
in a thread, so the worker-thread bridge is exercised for real). No model,
no network; the only real commands executed are harmless touch/ls in tmp dirs.
"""

import asyncio
import contextlib
import json
import os
import re
import shlex
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import aish.notify as notify_module
import aish.server as server_module
from aish.agent import DENIED_RESULT, WRITE_DENIED
from aish.server import SESSION_TITLE_PROMPT, TITLE_PROMPT, create_app
from aish.session import SessionLog, synthetic_kind


def tool_call(name: str, **arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def model_says(content: str = "", tool_calls: list | None = None):
    return SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls or None)
    )


# Titling calls (#172 answer export, #175 chat auto-title) are one-shot side
# calls — a bare prompt, no tools, outside the conversation. FakeChat answers
# them WITHOUT consuming the script, so a test's response list stays a script of
# its task turns and adding a titler didn't mean rewriting every test.
_TITLE_PROMPTS = (
    SESSION_TITLE_PROMPT.split("\n")[0],
    TITLE_PROMPT.split("\n")[0],
)


def _is_title_call(kwargs: dict) -> bool:
    messages = kwargs.get("messages") or []
    if len(messages) != 1:
        return False
    return str(messages[0].get("content", "")).startswith(_TITLE_PROMPTS)


class FakeChat:
    """Scripted backend. The web server always streams (on_token is wired),
    so stream=True returns the response as a one-chunk iterator — the same
    shape ollama's streaming yields.

    `title` is the canned answer to a titling call. It defaults to None — the
    backend declining to name things — so every test that doesn't care about
    titles behaves exactly as it did before there was a titler."""

    def __init__(self, responses: list, title: str | None = None):
        self.responses = list(responses)
        self.calls: list[dict] = []  # conversation turns only
        self.title_calls: list[dict] = []
        self.title = title

    def __call__(self, **kwargs):
        if _is_title_call(kwargs):
            self.title_calls.append(kwargs)
            return model_says(self.title or "")
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if kwargs.get("stream"):
            return iter([response])
        return response


@pytest.fixture
def app_env(tmp_path):
    """Isolated state/allow/deny files so tests never touch real config."""
    allow = tmp_path / "allow.txt"
    deny = tmp_path / "deny.txt"
    allow.write_text("ls\n", encoding="utf-8")
    deny.write_text("rm -rf\n", encoding="utf-8")
    return {
        "state_dir": tmp_path / "state",
        "allow_path": allow,
        "deny_path": deny,
        "config_path": tmp_path / "config.toml",
        "lessons_path": tmp_path / "lessons.md",
        "cwd": str(tmp_path),
    }


# The access token is unconditional now (#178 P1-2): an app built without one
# generates its own at startup. Tests get a fixed one so URLs stay printable.
TEST_TOKEN = "test-token"


class TokenClient(TestClient):
    """TestClient that appends the app's token to any request not already
    carrying one — the fixture-side answer to the unconditional token (#178
    P1-2), so the hundreds of pre-existing bare `/ws`, `/upload?…` calls keep
    working WITHOUT weakening the server-side requirement. Tests that pass an
    explicit token= to make_client get `auto_token=None` (plain TestClient
    behaviour), so their deliberate token omissions still hit the gate."""

    def __init__(self, app, auto_token=None, **kwargs):
        super().__init__(app, **kwargs)
        self.auto_token = auto_token

    def _tokened(self, url):
        url = str(url)
        if not self.auto_token or "token=" in url:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}token={self.auto_token}"

    def request(self, method, url, **kwargs):
        params = kwargs.get("params")
        if params is not None:
            # httpx REPLACES the URL query with `params`, so the token must
            # ride inside params here, not on the URL.
            if self.auto_token and "token" not in params:
                kwargs["params"] = {**dict(params), "token": self.auto_token}
            return super().request(method, url, **kwargs)
        return super().request(method, self._tokened(url), **kwargs)

    def websocket_connect(self, url, *args, **kwargs):
        return super().websocket_connect(self._tokened(url), *args, **kwargs)


def make_client(app_env, responses, title=None, **kwargs):
    chat = FakeChat(responses, title=title)
    explicit_token = "token" in kwargs
    kwargs.setdefault("token", TEST_TOKEN)
    app = create_app("fake", client_chat=chat, **app_env, **kwargs)
    auto = None if explicit_token else kwargs["token"]
    return TokenClient(app, auto_token=auto), chat


@contextlib.contextmanager
def connected(client, path="/ws"):
    """(ws, hello, replay) with the socket ALWAYS closed on exit — a failing
    assertion mid-test must not leave the session open, or TestClient's
    shutdown would wait on it forever."""
    with client.websocket_connect(path) as ws:
        hello = ws.receive_json()
        replay = ws.receive_json()
        assert hello["type"] == "hello"
        assert replay["type"] == "replay"
        yield ws, hello, replay


def recv_until(ws, wanted: str, limit: int = 200) -> dict:
    """Drain events until one of type `wanted` arrives (tokens etc. skipped).
    An unexpected error event fails fast instead of hanging the receive."""
    for _ in range(limit):
        event = ws.receive_json()
        if event["type"] == wanted:
            return event
        if event["type"] == "error":
            raise AssertionError(f"error while waiting for {wanted!r}: {event['text']}")
    raise AssertionError(f"no {wanted!r} event within {limit} events")


def drain_done(client, name, token=TEST_TOKEN):
    """Wait for a background session's task to finish: attach and return once
    `done` is seen — LIVE or already in the replay. A triggered task races the
    attach, so `connected(...)` + recv_until(ws, "done") hangs whenever the
    task finished first (its done is then replayed, never re-sent)."""
    with client.websocket_connect(f"/ws?token={token}&session={name}") as ws:
        hello = ws.receive_json()
        replay = ws.receive_json()
        assert hello["type"] == "hello" and replay["type"] == "replay"
        if any(e.get("type") == "done" for e in replay["events"]):
            return
        recv_until(ws, "done")


def recv_step(ws, kind: str, limit: int = 200) -> dict:
    """Drain events until a trace `step` of the given kind arrives (#95: the
    mid-task injected-message note is emitted as a step)."""
    for _ in range(limit):
        event = ws.receive_json()
        if event["type"] == "step" and event.get("kind") == kind:
            return event
        if event["type"] == "error":
            raise AssertionError(f"error while waiting for step {kind!r}: {event['text']}")
    raise AssertionError(f"no step {kind!r} within {limit} events")


def tool_results(chat, call_index=1):
    return [m for m in chat.calls[call_index]["messages"] if m["role"] == "tool"]


def find_tool_step(events, name):
    """The finished `tool` trace step for a given tool, from a live transcript
    or a cold reconstruction (both spread the step dict onto a `step` event)."""
    return next(
        e for e in events
        if e.get("type") == "step" and e.get("kind") == "tool" and e.get("name") == name
    )


# Event types that are live-only chrome or control-flow, reconstructed
# differently (or not at all) by design — excluded from the hot/cold guard.
# Everything NOT listed is a DURABLE trace event the cold path must reproduce,
# so a new event type added to the live stream but not persisted/reconstructed
# (or vice versa) fails the guard until it is handled on both paths.
_EPHEMERAL_EVENTS = {
    "token", "echo", "status", "error", "hello", "replay", "history",
    "queued", "cwd_queued", "cwd_dequeued", "approval_request",
    "approval_resolved", "cwd_changed",
    "model_changed", "session_state", "file_list", "job_list",
    "model_list", "session_list", "session_deleted", "session_renamed",
}


def trace_shape(events):
    """The durable-trace projection of an event stream: (type, discriminator)
    per event, with consecutive stream chunks coalesced (the live path streams
    N output lines; the cold path replays one) and ephemeral chrome dropped.
    Hot (bridge.transcript) and cold (reconstruct_events) must project equal."""
    shape = []
    for event in events:
        kind = event["type"]
        if kind in _EPHEMERAL_EVENTS:
            continue
        if kind == "stream":
            if not (shape and shape[-1] == ("stream",)):
                shape.append(("stream",))
        elif kind == "step":
            shape.append(("step", event.get("kind"), event.get("name")))
        elif kind == "command_end":
            shape.append(("command_end", event.get("status")))
        elif kind == "workspace":
            shape.append(("workspace", event.get("change"), event.get("path")))
        else:
            shape.append((kind,))
    return shape


class TestStreamCoalescer:
    """Issue #109: the live per-line output of a huge command is batched into
    fewer, larger `stream` events before it reaches the browser. This is a
    live-only transport optimization — the logged tool output and cold replay
    are untouched (see test_large_bang_output_batches_stream_events for the
    end-to-end batching, and the hot/cold parity guard elsewhere)."""

    def _coalescer(self, sink):
        from aish.server import StreamCoalescer

        c = StreamCoalescer(sink)
        c.MAX_DELAY = 3600  # keep the delay-flush thread from firing mid-test
        return c

    def test_batches_lines_and_flushes_remainder(self):
        emitted = []
        c = self._coalescer(emitted.append)
        for i in range(120):
            c.line(f"line-{i}")
        # 120 lines, MAX_LINES=50 → two full 50-line batches emitted; the last
        # 20 stay buffered until the explicit command-end flush.
        assert len(emitted) == 2
        assert emitted[0] == "\n".join(f"line-{i}" for i in range(50))
        assert emitted[1] == "\n".join(f"line-{i}" for i in range(50, 100))
        c.flush()
        assert len(emitted) == 3
        assert emitted[2] == "\n".join(f"line-{i}" for i in range(100, 120))
        # No output is lost: the batches rejoin to the original line stream.
        assert "\n".join(emitted).split("\n") == [f"line-{i}" for i in range(120)]

    def test_flush_of_empty_buffer_emits_nothing(self):
        emitted = []
        c = self._coalescer(emitted.append)
        c.flush()
        assert emitted == []

    def test_byte_cap_flushes_a_single_large_line(self):
        emitted = []
        c = self._coalescer(emitted.append)
        big = "x" * (17 * 1024)  # over MAX_BYTES on its own
        c.line(big)
        assert emitted == [big]


class TestConnect:
    def test_hello_carries_model_session_scope(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, replay):
            assert hello["model"] == "fake"
            assert hello["session"].startswith("session-")
            assert hello["busy"] is False
            assert hello["cwd"] == app_env["cwd"]
            assert hello["rev"]  # static-files fingerprint for staleness checks
            assert hello["log_path"].endswith(hello["session"])  # #146: /session + copy
            assert replay["events"] == []

    def test_index_stamps_asset_revision(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            response = client.get("/")
            assert response.status_code == 200
            assert 'src="app.js?v=' in response.text
            assert 'href="style.css?v=' in response.text
            assert response.headers["cache-control"] == "no-cache"

    def test_hello_title_is_first_user_message(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        with client:
            with connected(client) as (ws, hello, _):
                assert hello["title"] == ""  # fresh session: client shows "New chat"
                ws.send_json({"type": "task", "text": "rename all the photos"})
                recv_until(ws, "done")
            with connected(client) as (_ws, hello, _):
                assert hello["title"] == "rename all the photos"

    def test_task_streams_and_finishes(self, app_env):
        client, chat = make_client(app_env, [model_says("hi there")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "say hi"})
            assert recv_until(ws, "user")["text"] == "say hi"
            done = recv_until(ws, "done")
            assert done["result"] == "hi there"
            assert "sources" not in done  # no web use, no sources field
            sent_user = [m for m in chat.calls[0]["messages"] if m["role"] == "user"]
            assert sent_user[-1]["content"] == "say hi"

    def test_done_carries_sources_after_read_url(self, app_env, monkeypatch):
        from types import SimpleNamespace

        import aish.agent as agent_module

        monkeypatch.setattr(
            agent_module.web, "read_url", lambda url, topic=None: f"[{url}] text"
        )
        read_call = SimpleNamespace(
            function=SimpleNamespace(name="read_url", arguments={"url": "https://x.example/"})
        )
        client, _ = make_client(
            app_env,
            [model_says(tool_calls=[read_call]), model_says("answer")],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "research"})
            done = recv_until(ws, "done")
            assert done["sources"] == [{"url": "https://x.example/"}]


class TestSessionStamp:
    """#182: every event a bridge fans out (or records for replay) names its
    session, so the client can drop live deliveries for a chat that is no
    longer on screen — the client-side switch window of a prefetched swipe or
    an offline-mirror tap, where the old session's events kept arriving until
    the server processed the resume."""

    class _Viewer:
        def __init__(self):
            self.items: list = []
            self.outbox = SimpleNamespace(put_nowait=self.items.append)

    def test_bridge_stamps_fanout_but_not_the_transcript(self):
        bridge = server_module.Bridge(lambda: None, session="session-x.jsonl")
        viewer = self._Viewer()
        bridge.viewers.add(viewer)
        bridge.emit({"type": "token", "text": "hi"})
        assert viewer.items[0]["session"] == "session-x.jsonl"
        # The stamp rides only the live delivery: the recorded transcript stays
        # byte-identical to reconstruct_events (hot/cold parity), and replayed
        # events bypass the client's firewall anyway.
        assert "session" not in bridge.transcript[0]

    def test_stamp_never_overwrites_an_events_own_session(self):
        # An event that names its session itself (session_state) keeps it.
        bridge = server_module.Bridge(lambda: None, session="session-x.jsonl")
        viewer = self._Viewer()
        bridge.viewers.add(viewer)
        bridge.emit({"type": "session_state", "session": "session-other.jsonl"})
        assert viewer.items[0]["session"] == "session-other.jsonl"

    def test_unnamed_bridge_stamps_nothing(self):
        # Back-compat for bridges built without a session (tests): events pass
        # through untouched, which the client reads as "not scoped".
        bridge = server_module.Bridge(lambda: None)
        viewer = self._Viewer()
        bridge.viewers.add(viewer)
        bridge.emit({"type": "token", "text": "hi"})
        assert "session" not in viewer.items[0]

    def test_live_task_events_carry_the_sessions_name(self, app_env):
        client, _ = make_client(app_env, [model_says("hi there")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "say hi"})
            for _ in range(200):
                event = ws.receive_json()
                assert event["session"] == hello["session"], event
                if event["type"] == "done":
                    break
            else:
                raise AssertionError("no done event")


class TestSecurityHeaders:
    """#178 P0-2: CSP + Referrer-Policy stamped on EVERY http response class —
    index, static assets, the service worker, JSON endpoints, and errors —
    with img-src limited to self/data:/the whitelisted embed hosts (the
    zero-click ![](https://attacker/…) exfiltration channel)."""

    PATHS = ("/", "/app.js", "/sw.js", "/manifest.json", "/offline/index",
             "/style.css", "/no-such-path-xyz")

    def test_headers_on_every_response_class(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            for path in self.PATHS:
                response = client.get(path)
                csp = response.headers.get("content-security-policy")
                assert csp, f"no CSP on {path}"
                assert "default-src 'self'" in csp
                assert "script-src 'self'" in csp
                assert (
                    "img-src 'self' data: https://img.youtube.com "
                    "https://i.ytimg.com https://maps.googleapis.com" in csp
                )
                assert "frame-ancestors 'none'" in csp
                assert "object-src 'none'" in csp
                assert response.headers.get("referrer-policy") == "no-referrer"

    def test_connect_src_names_websocket_forms_of_own_host(self, app_env):
        # 'self' plus the explicit ws/wss forms of the request's Host header,
        # so the socket keeps working behind the wss reverse proxy.
        client, _ = make_client(app_env, [])
        with client:
            csp = client.get("/").headers["content-security-policy"]
            assert "connect-src 'self' ws://testserver wss://testserver" in csp

    def test_hostile_host_header_never_reaches_the_policy(self):
        # Header-injection guard: a Host that could smuggle extra CSP sources
        # (spaces/semicolons) degrades to bare 'self', not into the policy.
        csp = server_module.content_security_policy("evil.com; img-src *")
        assert "evil.com" not in csp and "img-src *" not in csp
        assert "connect-src 'self';" in csp

    def test_index_has_no_inline_scripts(self):
        # script-src 'self' only holds because index.html loads all JS from
        # files — an inline <script> added later would silently break the app.
        html = (server_module.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag, f"inline script would violate CSP: {tag}"


class TestQuickReplyPromptGuidance:
    """Issue #78: the system prompt must forbid terminating quick-reply chips
    ("Thanks, that's all") — the user can end the chat anytime, so chips must
    only offer useful next steps."""

    def test_forbids_terminating_chips(self):
        context = server_module.web_usage_context(
            "model", "ollama", "/allow", "/deny", "/state"
        )
        assert "NEVER generate a chip whose only purpose is to end the conversation" in context
        assert "Thanks, that's all" in context
        assert "useful next step" in context


class TestQuickReplyNet:
    """Issue #46: a web final answer that ends in a question with no chip gets
    a deterministic fallback set; [no-chips] opts out and is stripped."""

    def test_suffix_appends_for_bare_question(self):
        result, suffix = server_module.apply_quick_reply_net("Ready to deploy?")
        assert suffix == "\n\n" + "\n".join(server_module.FALLBACK_CHIPS)
        assert result.endswith("\n".join(server_module.FALLBACK_CHIPS))
        assert "aish-reply://yes" in result

    def test_existing_chip_left_untouched(self):
        answer = "Deploy?\n[Go](aish-reply://go)"
        assert server_module.apply_quick_reply_net(answer) == (answer, None)

    def test_non_question_left_untouched(self):
        answer = "All done — the build passed."
        assert server_module.apply_quick_reply_net(answer) == (answer, None)

    def test_no_chips_tag_strips_and_suppresses(self):
        result, suffix = server_module.apply_quick_reply_net(
            "What should we build next? [no-chips]"
        )
        assert suffix is None
        assert "no-chips" not in result
        assert "aish-reply://" not in result
        assert result == "What should we build next?"

    def test_task_appends_fallback_chips_over_socket(self, app_env):
        client, _ = make_client(app_env, [model_says("Shall I proceed?")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "go"})
            events = []
            for _ in range(200):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "done":
                    break
            done = next(e for e in events if e["type"] == "done")
            assert "aish-reply://tell me more" in done["result"]
            # the suffix also streams as a token so an already-streamed answer
            # gains the chips live, not only in done.result.
            token_text = "".join(e["text"] for e in events if e["type"] == "token")
            assert "aish-reply://yes" in token_text

    def test_task_strips_no_chips_tag_over_socket(self, app_env):
        client, _ = make_client(
            app_env, [model_says("How would you like to approach this? [no-chips]")]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "advise"})
            done = recv_until(ws, "done")
            assert "no-chips" not in done["result"]
            assert "aish-reply://" not in done["result"]

    def test_task_keeps_model_supplied_chips(self, app_env):
        answer = "Proceed?\n[Yes, go](aish-reply://yes)\n[Hold](aish-reply://hold)"
        client, _ = make_client(app_env, [model_says(answer)])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "go"})
            done = recv_until(ws, "done")
            assert done["result"] == answer
            assert "tell me more" not in done["result"].lower()


class TestCommandApproval:
    def responses(self, command):
        return [
            model_says(tool_calls=[tool_call("run_command", command=command)]),
            model_says("finished"),
        ]

    def test_approve_runs_command(self, app_env, tmp_path):
        marker = tmp_path / "ran42"
        client, _ = make_client(app_env, self.responses(f"touch {marker}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            assert request["kind"] == "command"
            assert request["command"] == f"touch {marker}"
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")
            assert marker.exists()

    def test_deny_never_executes(self, app_env, tmp_path):
        marker = tmp_path / "pwned"
        client, chat = make_client(app_env, self.responses(f"touch {marker}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")
            assert not marker.exists()
            assert tool_results(chat)[-1]["content"] == DENIED_RESULT

    def test_deny_with_comment_reaches_model(self, app_env, tmp_path):
        """#13: feedback typed into the card comes back as model guidance."""
        marker = tmp_path / "pwned2"
        client, chat = make_client(app_env, self.responses(f"touch {marker}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "deny",
                    "comment": "wrong flag on macOS, use -f",
                }
            )
            resolved = recv_until(ws, "approval_resolved")
            assert resolved["decision"] == "denied"
            assert resolved["comment"] == "wrong flag on macOS, use -f"
            recv_until(ws, "done")
            assert not marker.exists()
            result = tool_results(chat)[-1]["content"]
            assert result.startswith(DENIED_RESULT)
            assert "wrong flag on macOS, use -f" in result

    def test_approve_with_comment_holds_original_for_adjustment(self, app_env, tmp_path):
        """#81: APPROVE + comment = continue but ADJUST — the original command
        is HELD (never run), and the model is told to adjust and re-propose."""
        marker = tmp_path / "ran43"
        client, chat = make_client(app_env, self.responses(f"touch {marker}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "approve",
                    "comment": "run it verbosely instead",
                }
            )
            resolved = recv_until(ws, "approval_resolved")
            assert resolved["decision"] == "approved"
            assert resolved["comment"] == "run it verbosely instead"
            recv_until(ws, "done")
            assert not marker.exists()  # HELD — the original never ran
            result = tool_results(chat)[-1]["content"]
            assert result.startswith("NOT RUN")
            assert "run it verbosely instead" in result
            assert "ADJUSTED" in result

    def test_always_allow_persists_prefix_and_skips_future_prompts(
        self, app_env, tmp_path
    ):
        """#34: "Always allow" writes the shown prefix to the allowlist file,
        so the rule outlives the session and later calls auto-approve."""
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
            model_says("first done"),
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/b")]),
            model_says("second done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch a"})
            request = recv_until(ws, "approval_request")
            assert request["prefixes"] == ["touch"]
            ws.send_json(
                {"type": "approval", "id": request["id"], "action": "approve_always"}
            )
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")
            assert (tmp_path / "a").exists()
            allowed = app_env["allow_path"].read_text(encoding="utf-8").splitlines()
            assert "touch" in allowed

            ws.send_json({"type": "task", "text": "touch b"})
            auto = None
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "done":
                    break
                assert event["type"] != "approval_request"
                if event["type"] == "echo" and "auto-approved" in event["text"]:
                    auto = event
            assert auto is not None
            assert (tmp_path / "b").exists()

    def test_edit_with_comment_holds_for_adjustment(self, app_env, tmp_path):
        """#81: an edit that ALSO carries a comment is still a commented
        approval, so it holds — neither the original nor the edited form runs;
        the model adjusts and re-proposes. (Edit WITHOUT a comment runs the
        edit — see test_edit_runs_edited_command.)"""
        original, edited = tmp_path / "orig43", tmp_path / "edited43"
        client, chat = make_client(app_env, self.responses(f"touch {original}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "edit",
                    "command": f"touch {edited}",
                    "comment": "always use the edited name",
                }
            )
            recv_until(ws, "done")
            assert not edited.exists() and not original.exists()  # HELD
            result = tool_results(chat)[-1]["content"]
            assert result.startswith("NOT RUN")
            assert "always use the edited name" in result

    def test_edit_runs_edited_command(self, app_env, tmp_path):
        original, edited = tmp_path / "original", tmp_path / "edited42"
        client, chat = make_client(app_env, self.responses(f"touch {original}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "edit",
                    "command": f"touch {edited}",
                }
            )
            assert recv_until(ws, "approval_resolved")["decision"] == "edited"
            recv_until(ws, "done")
            assert edited.exists() and not original.exists()
            assert "user edited the command" in tool_results(chat)[-1]["content"]

    def test_edited_command_still_hits_denylist(self, app_env, tmp_path):
        target = tmp_path / "precious"
        target.mkdir()
        client, chat = make_client(app_env, self.responses(f"touch {tmp_path}/harmless"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "list"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "edit",
                    "command": f"rm -rf {target}",
                }
            )
            recv_until(ws, "done")
            assert target.exists()
            assert "BLOCKED by the safety denylist" in tool_results(chat)[-1]["content"]

    def test_denylisted_command_never_prompts(self, app_env):
        client, chat = make_client(app_env, self.responses("rm -rf /tmp/x"))
        with client, connected(client) as (ws, _, _):
            recv_done = None
            ws.send_json({"type": "task", "text": "nuke"})
            for _ in range(200):
                event = ws.receive_json()
                assert event["type"] != "approval_request"
                if event["type"] == "done":
                    recv_done = event
                    break
            assert recv_done is not None
            assert "BLOCKED by the safety denylist" in tool_results(chat)[-1]["content"]

    def test_allow_this_session_skips_future_prompts(self, app_env, tmp_path):
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
            model_says("first done"),
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/b")]),
            model_says("second done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch a"})
            request = recv_until(ws, "approval_request")
            assert request["prefixes"] == ["touch"]
            ws.send_json(
                {"type": "approval", "id": request["id"], "action": "approve_session"}
            )
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")
            assert (tmp_path / "a").exists()

            ws.send_json({"type": "task", "text": "touch b"})
            auto = None
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "done":
                    break
                assert event["type"] != "approval_request"
                if event["type"] == "echo" and "auto-approved" in event["text"]:
                    auto = event
            assert auto is not None
            assert (tmp_path / "b").exists()

    def _session_allow_responses(self, tmp_path):
        return [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
            model_says("first done"),
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/b")]),
            model_says("second done"),
        ]

    def test_allow_this_session_does_not_leak_into_another_session(self, app_env, tmp_path):
        """The allowance belongs to the chat that granted it, not to the process
        (#176) — it lives on that session's own agent beside its roots, so a
        second chat in the same server is still asked."""
        client, _ = make_client(app_env, self._session_allow_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch a"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {"type": "approval", "id": request["id"], "action": "approve_session"}
            )
            recv_until(ws, "done")

            ws.send_json({"type": "new"})
            recv_until(ws, "hello")
            recv_until(ws, "replay")

            ws.send_json({"type": "task", "text": "touch b"})
            second = recv_until(ws, "approval_request")  # asked again, not inherited
            assert second["prefixes"] == ["touch"]
            ws.send_json({"type": "approval", "id": second["id"], "action": "deny"})
            recv_until(ws, "done")
            assert not (tmp_path / "b").exists()

    def test_allow_this_session_does_not_survive_a_cold_reopen(
        self, app_env, tmp_path, monkeypatch
    ):
        """Prefixes are never written to disk, so a session evicted from memory
        and reopened cold from its log re-grants nothing — the gate asks again
        rather than a persistence the log cannot back (#176)."""
        monkeypatch.setattr(server_module, "MAX_OPEN_SESSIONS", 2)
        client, _ = make_client(app_env, self._session_allow_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "new"})
            name = recv_until(ws, "hello")["session"]
            recv_until(ws, "replay")

            ws.send_json({"type": "task", "text": "touch a"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {"type": "approval", "id": request["id"], "action": "approve_session"}
            )
            recv_until(ws, "done")

            # Move off it (viewerless), then churn past the cap so it is evicted.
            server = client.app.state.server
            for _ in range(2):
                ws.send_json({"type": "new"})
                recv_until(ws, "hello")
                recv_until(ws, "replay")
            assert name not in server.sessions

            ws.send_json({"type": "resume", "path": name})  # reopened cold from disk
            assert recv_until(ws, "hello")["session"] == name
            recv_until(ws, "replay")

            ws.send_json({"type": "task", "text": "touch b"})
            second = recv_until(ws, "approval_request")
            assert second["prefixes"] == ["touch"]
            ws.send_json({"type": "approval", "id": second["id"], "action": "deny"})
            recv_until(ws, "done")
            assert not (tmp_path / "b").exists()

    def test_trust_directory_widens_roots_for_session(self, app_env, tmp_path_factory):
        """The card's "Trust directory" on a root-escaping command: the command
        runs, and allowlisted commands in that directory auto-approve after."""
        outside = tmp_path_factory.mktemp("elsewhere")
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"ls {outside}")]),
            model_says("first done"),
            model_says(tool_calls=[tool_call("run_command", command=f"ls {outside}")]),
            model_says("second done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "look elsewhere"})
            request = recv_until(ws, "approval_request")
            assert request["escapes"] == [str(outside)]
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve_trust"})
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")

            ws.send_json({"type": "task", "text": "look again"})
            auto = None
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "done":
                    break
                assert event["type"] != "approval_request"
                if event["type"] == "echo" and "auto-approved" in event["text"]:
                    auto = event
            assert auto is not None

    def test_in_root_command_card_has_no_escapes(self, app_env, tmp_path):
        client, _ = make_client(app_env, self.responses(f"touch {tmp_path}/plain"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            request = recv_until(ws, "approval_request")
            assert request["escapes"] == []
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")

    def test_allowlisted_readonly_auto_approves(self, app_env):
        client, _ = make_client(app_env, self.responses("ls"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "list"})
            auto = None
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "done":
                    break
                assert event["type"] != "approval_request"
                if event["type"] == "echo" and "auto-approved" in event["text"]:
                    auto = event
            assert auto is not None and auto["text"] == "✓ auto-approved: ls"


class TestTerminalFraming:
    """#52: run_command is framed by recorded command_start / command_end
    events so the browser can draw a bounded terminal block, and a reconnect
    replays the frame identically."""

    def _drain(self, ws) -> list[dict]:
        events = []
        for _ in range(200):
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "done":
                return events
            if event["type"] == "error":
                raise AssertionError(f"error: {event['text']}")
        raise AssertionError("no done within 200 events")

    def test_framing_events_emitted_live_and_recorded(self, app_env, tmp_path):
        marker = tmp_path / "framed"
        # `ls` is allowlisted, so it auto-approves and streams without a card.
        responses = [
            model_says(tool_calls=[tool_call("run_command", command="ls")]),
            model_says("done"),
        ]
        _ = marker  # keep tmp_path scoping obvious
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "list it"})
            events = self._drain(ws)
            starts = [e for e in events if e["type"] == "command_start"]
            ends = [e for e in events if e["type"] == "command_end"]
            assert len(starts) == 1
            assert starts[0]["cwd"] == app_env["cwd"]
            assert starts[0]["command"] == "ls"
            # Live deliveries carry the session stamp (#182); the recorded
            # frame (checked below via the reconnect replay) does not.
            assert ends == [{"type": "command_end", "status": "exit",
                             "exit_code": 0, "session": hello["session"]}]

        # A reconnect replays the recorded frame identically (phone lock/unlock,
        # session switch) — the block must reconstruct from the transcript.
        with client, connected(client) as (_ws, _hello, replay):
            kinds = [e["type"] for e in replay["events"]]
            assert "command_start" in kinds and "command_end" in kinds
            start = next(e for e in replay["events"] if e["type"] == "command_start")
            end = next(e for e in replay["events"] if e["type"] == "command_end")
            assert start["command"] == "ls" and start["cwd"] == app_env["cwd"]
            assert end["status"] == "exit" and end["exit_code"] == 0

    def test_denied_command_has_no_framing(self, app_env, tmp_path):
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("ok"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            events = []
            for _ in range(200):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "done":
                    break
            assert not any(e["type"].startswith("command_") for e in events)


class TestBangCommands:
    """A user-typed ! command runs directly as the user's own action — no model,
    no approval gate — mirroring the CLI's ! escape (cli.main). !cd is the /cd
    alias. The empty responses list means the model is never consulted: a stray
    model call would IndexError and surface as an error event, failing the test."""

    def _drain(self, ws) -> list[dict]:
        events = []
        for _ in range(200):
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "done":
                return events
            if event["type"] == "error":
                raise AssertionError(f"error: {event['text']}")
        raise AssertionError("no done within 200 events")

    def test_bang_command_runs_and_streams_without_model_or_approval(self, app_env):
        client, chat = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "!echo direct-hit"})
            assert recv_until(ws, "user")["text"] == "!echo direct-hit"
            events = self._drain(ws)
            # No approval card: a ! command is the user's own action (CLI parity).
            assert not any(e["type"] == "approval_request" for e in events)
            starts = [e for e in events if e["type"] == "command_start"]
            assert starts and starts[0]["command"] == "echo direct-hit"
            assert starts[0]["cwd"] == app_env["cwd"]
            # user=True so the web renders it inline in the transcript, not
            # inside the model's activity trace (it's a direct user action).
            assert starts[0].get("user") is True
            streamed = " ".join(e["text"] for e in events if e["type"] == "stream")
            assert "direct-hit" in streamed
            ends = [e for e in events if e["type"] == "command_end"]
            assert ends and ends[0] == {"type": "command_end", "status": "exit",
                                        "exit_code": 0, "session": hello["session"]}
            # The output rode the terminal block; done carries no answer bubble.
            assert events[-1] == {"type": "done", "result": "", "session": hello["session"]}
            assert chat.calls == []  # the model was never asked

    def test_large_bang_output_batches_stream_events(self, app_env):
        """Issue #109: a command producing hundreds of output lines must not
        emit one `stream` event per line (the frontend then reflows per line and
        the tab freezes). The coalescer batches them — far fewer events — while
        delivering every line intact."""
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            # awk is portable and streams one line per iteration through on_line.
            ws.send_json({"type": "task", "text": "!awk 'BEGIN{for(i=1;i<=300;i++)print i}'"})
            events = self._drain(ws)
            streams = [e for e in events if e["type"] == "stream"]
            # 300 lines batched at ~50/chunk → an order of magnitude fewer events.
            assert 0 < len(streams) < 50
            lines = [ln for ln in "\n".join(e["text"] for e in streams).split("\n") if ln]
            assert lines == [str(n) for n in range(1, 301)]

    def test_bang_mutating_command_bypasses_approval_like_cli(self, app_env, tmp_path):
        """A ! command that mutates state still runs without a card — exactly as
        the CLI's ! runs `touch` directly. The gate guards the model, not the
        user typing their own command."""
        marker = tmp_path / "bang-made-me"
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": f"!touch {marker}"})
            events = self._drain(ws)
            assert not any(e["type"] == "approval_request" for e in events)
            assert marker.exists()

    def test_bang_cd_moves_cwd_and_reanchors_root(self, app_env, tmp_path):
        """!cd is the /cd alias: it must not be shadowed by the general !command
        path — it moves cwd, re-anchors roots[0], and refreshes the UI cwd."""
        project = tmp_path / "bang-project"
        project.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": f"!cd {project}"})
            changed = recv_until(ws, "cwd_changed")
            assert changed["cwd"] == str(project)
            assert changed["roots"][0] == str(project)
            recv_until(ws, "done")

    def test_bang_session_title_shows_command_not_annotation(self, app_env):
        """The reconnect hello title uses the same bang-aware derivation as the
        drawer, so a ! session reads as '! <cmd>' — never the internal
        '[I ran … myself]' conversation annotation."""
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "!echo titled"})
            self._drain(ws)
        with client, connected(client) as (_ws, hello, _):
            assert hello["title"] == "! echo titled"

    def test_bang_command_replays_as_terminal_block_when_cold(self, app_env):
        """A ! command survives eviction/restart: reopened cold from its log it
        reconstructs into the same user → terminal-block → done event stream a
        live client saw, not the internal "[I ran … myself]" annotation."""
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            old_name = hello["session"]
            ws.send_json({"type": "task", "text": "!echo cold-hit"})
            self._drain(ws)
        client2, _ = make_client(app_env, [])
        with client2, connected(client2) as (ws, _, _):
            ws.send_json({"type": "resume", "path": old_name})
            recv_until(ws, "hello")
            replay = recv_until(ws, "replay")
            kinds = [e["type"] for e in replay["events"]]
            assert "command_start" in kinds and "command_end" in kinds
            user_ev = next(e for e in replay["events"] if e["type"] == "user")
            assert user_ev["text"] == "!echo cold-hit"
            start = next(e for e in replay["events"] if e["type"] == "command_start")
            assert start["command"] == "echo cold-hit"
            assert start.get("user") is True  # inline transcript block on cold replay too
            streamed = " ".join(
                e["text"] for e in replay["events"] if e["type"] == "stream"
            )
            assert "cold-hit" in streamed
            # No raw internal annotation leaks into the transcript.
            assert not any("I ran `" in json.dumps(e) for e in replay["events"])

    def test_bang_command_is_interruptible_by_stop(self, app_env):
        """A long-running ! command is cancellable (issue #76): Stop signals its
        whole process group — the `sh -c` shell AND its child `sleep` — the
        terminal block renders an interrupted status, and the session returns to
        idle promptly (not after the 30s sleep) with no hung worker."""
        client, chat = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            # sh -c keeps a child `sleep` alive, so the stop must reach the whole
            # group, not just the shell it launched.
            ws.send_json({"type": "task", "text": "!sh -c 'sleep 30'"})
            recv_until(ws, "command_start")
            started = time.monotonic()
            ws.send_json({"type": "stop"})
            end = recv_until(ws, "command_end")
            elapsed = time.monotonic() - started
            assert end["status"] == "interrupted"
            assert elapsed < 10  # terminated on the stop, not after the sleep
            recv_until(ws, "done")
            # The worker cleared: the session is idle again, never wedged busy.
            assert client.app.state.server.active.busy is False
            # And a stale cancel didn't leak — a follow-up ! command still runs.
            ws.send_json({"type": "task", "text": "!echo recovered"})
            events = []
            for _ in range(200):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "done":
                    break
            streamed = " ".join(e["text"] for e in events if e["type"] == "stream")
            assert "recovered" in streamed
            assert chat.calls == []  # a ! command never touches the model


class TestWriteApproval:
    def responses(self, path, content):
        return [
            model_says(tool_calls=[tool_call("write_file", path=path, content=content)]),
            model_says("written"),
        ]

    def test_approve_commits(self, app_env, tmp_path):
        target = tmp_path / "note.txt"
        client, _ = make_client(app_env, self.responses(str(target), "hello\n"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "write it"})
            request = recv_until(ws, "approval_request")
            assert request["kind"] == "write"
            assert request["verb"] == "create"
            assert "+hello" in request["diff"]
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")
            assert target.read_text(encoding="utf-8") == "hello\n"

    def test_deny_leaves_disk_untouched(self, app_env, tmp_path):
        target = tmp_path / "note.txt"
        client, chat = make_client(app_env, self.responses(str(target), "hello\n"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "write it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")
            assert not target.exists()
            assert tool_results(chat)[-1]["content"] == WRITE_DENIED

    def test_approve_write_with_comment_holds_for_adjustment(self, app_env, tmp_path):
        """#81: APPROVE + comment holds the write — nothing lands; the model
        adjusts to the comment and re-proposes."""
        target = tmp_path / "note.txt"
        client, chat = make_client(app_env, self.responses(str(target), "hello\n"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "write it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "approve",
                    "comment": "keep future notes under docs/",
                }
            )
            recv_until(ws, "done")
            assert not target.exists()  # HELD — nothing was written
            result = tool_results(chat)[-1]["content"]
            assert result.startswith("NOT WRITTEN")
            assert "keep future notes under docs/" in result
            assert "ADJUSTED" in result

    def test_deny_write_with_comment_reaches_model(self, app_env, tmp_path):
        target = tmp_path / "note.txt"
        client, chat = make_client(app_env, self.responses(str(target), "hello\n"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "write it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "deny",
                    "comment": "wrong file — put it in docs/",
                }
            )
            recv_until(ws, "done")
            assert not target.exists()
            result = tool_results(chat)[-1]["content"]
            assert result.startswith(WRITE_DENIED)
            assert "wrong file — put it in docs/" in result

    def test_approved_edit_step_carries_diff(self, app_env, tmp_path):
        """#55: an applied edit's trace step carries the diff the approval card
        computed, so the web timeline renders WHAT changed — live AND cold."""
        target = tmp_path / "note.txt"
        target.write_text("old line\n", encoding="utf-8")
        responses = [
            model_says(tool_calls=[tool_call(
                "edit_file", path=str(target), old_str="old line", new_str="new line")]),
            model_says("edited"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "edit it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")
            server = client.app.state.server
            hot = list(server.active.bridge.transcript)
            path = server.active.logref.log.path
        assert target.read_text(encoding="utf-8") == "new line\n"
        for events in (hot, SessionLog.reconstruct_events(path)):
            step = find_tool_step(events, "edit_file")
            assert step["decision"] == "approved"
            assert "+new line" in step["diff"]
            assert "-old line" in step["diff"]

    def test_denied_edit_step_carries_diff_and_reason(self, app_env, tmp_path):
        """#55/#67: a denied edit stays in the timeline marked denied, with the
        proposed (not-applied) diff and the user's feedback — live AND cold."""
        target = tmp_path / "note.txt"
        target.write_text("old line\n", encoding="utf-8")
        responses = [
            model_says(tool_calls=[tool_call(
                "edit_file", path=str(target), old_str="old line", new_str="new line")]),
            model_says("understood"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "edit it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"],
                          "action": "deny", "comment": "leave it as is"})
            recv_until(ws, "done")
            server = client.app.state.server
            hot = list(server.active.bridge.transcript)
            path = server.active.logref.log.path
        assert target.read_text(encoding="utf-8") == "old line\n"  # never touched disk
        for events in (hot, SessionLog.reconstruct_events(path)):
            step = find_tool_step(events, "edit_file")
            assert step["decision"] == "denied"
            assert step["ok"] is False
            assert "+new line" in step["diff"]
            assert step["comment"] == "leave it as is"


class TestReconnect:
    def pending_responses(self, tmp_path):
        return [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("finished"),
        ]

    def test_replay_carries_pending_approval(self, app_env, tmp_path):
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({"type": "task", "text": "run it"})
                request = recv_until(ws, "approval_request")
            # phone locked: socket gone, agent still waiting on the approval
            with connected(client) as (ws2, hello, replay):
                assert hello["busy"] is True
                replayed = [
                    e for e in replay["events"] if e["type"] == "approval_request"
                ]
                assert replayed and replayed[0]["id"] == request["id"]
                ws2.send_json(
                    {"type": "approval", "id": request["id"], "action": "approve"}
                )
                recv_until(ws2, "done")

    def test_cd_queued_while_busy(self, app_env, tmp_path):
        # A /cd mid-task can't move state under the running agent, so it's
        # queued and applied when the task finishes — not rejected. It surfaces
        # as a deduplicated queue card (#92), not an invisible echo.
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")  # agent now blocked → busy
            ws.send_json({"type": "cd", "path": str(tmp_path)})
            queued = recv_until(ws, "cwd_queued")
            assert queued["path"] == str(tmp_path)
            assert client.app.state.server.active.pending_cwd == str(tmp_path)
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_second_cd_overwrites_pending_and_re_emits(self, app_env, tmp_path):
        # Dedup (#92): a second cd while one is queued overwrites pending_cwd
        # (single card) and re-emits so the frontend updates in place.
        sub = tmp_path / "sub"
        sub.mkdir()
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "cd", "path": str(tmp_path)})
            recv_until(ws, "cwd_queued")
            ws.send_json({"type": "cd", "path": str(sub)})
            second = recv_until(ws, "cwd_queued")
            assert second["path"] == str(sub)
            assert client.app.state.server.active.pending_cwd == str(sub)
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_dequeue_cwd_clears_pending(self, app_env, tmp_path):
        # Remove (#92): dequeue_cwd clears the pending change and tells the
        # frontend to drop the card.
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "cd", "path": str(tmp_path)})
            recv_until(ws, "cwd_queued")
            ws.send_json({"type": "dequeue_cwd"})
            recv_until(ws, "cwd_dequeued")
            assert client.app.state.server.active.pending_cwd is None
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_cd_and_message_applied_mid_task_between_steps(self, app_env, tmp_path):
        # #95: a /cd AND a message queued while the task runs are BOTH consumed
        # between steps of the SAME task — the cd rebases (card retired via
        # cwd_dequeued, top bar refreshed via cwd_changed) and the message is
        # injected as a steering note — all before the task's own `done`, not
        # deferred to _finish_turn as separate follow-ups.
        sub = tmp_path / "work"
        sub.mkdir()
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("first done"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "first"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "cd", "path": str(sub)})
            recv_until(ws, "cwd_queued")
            ws.send_json({"type": "task", "text": "second"})
            recv_until(ws, "queued")
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "cwd_dequeued")  # cd applied mid-task → card retired
            changed = recv_until(ws, "cwd_changed")  # top bar updated immediately
            assert changed["cwd"] == str(sub)
            injected = recv_step(ws, "injected")  # message injected as steering
            assert injected["text"] == "second"
            recv_until(ws, "done")
            server = client.app.state.server
            assert server.active.pending_cwd is None
            assert str(server.active.agent.cwd) == str(sub)  # rebased mid-task
            assert server.active.queue == []  # message injected once, not relaunched
            # the model saw the steering line in the SAME task
            assert any(
                m.get("content") == "second" for m in server.active.agent.messages
            )

    def test_pending_cwd_card_replays_on_reconnect(self, app_env, tmp_path):
        # The card is backend-authoritative (#92): a reconnect while a cd is
        # pending re-emits cwd_queued so the card reappears.
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({"type": "task", "text": "run it"})
                request = recv_until(ws, "approval_request")
                ws.send_json({"type": "cd", "path": str(tmp_path)})
                recv_until(ws, "cwd_queued")
            with connected(client) as (ws2, hello, _):
                assert hello["busy"] is True
                requeued = recv_until(ws2, "cwd_queued")
                assert requeued["path"] == str(tmp_path)
                ws2.send_json({"type": "approval", "id": request["id"], "action": "approve"})
                recv_until(ws2, "done")

    def test_cd_applied_mid_task_updates_bar_before_done(self, app_env, tmp_path):
        # #95: a /cd queued while a multi-step task runs is applied at the next
        # step boundary — the top bar (cwd_changed) and card (cwd_dequeued)
        # update BEFORE the task's own `done`, so a long task stays responsive.
        sub = tmp_path / "work"
        sub.mkdir()
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("done"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "go"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "cd", "path": str(sub)})
            recv_until(ws, "cwd_queued")
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "cwd_dequeued")  # applied mid-task, card retired
            changed = recv_until(ws, "cwd_changed")
            assert changed["cwd"] == str(sub)
            server = client.app.state.server
            assert server.active.agent.cwd == str(sub)
            assert server.active.pending_cwd is None
            recv_until(ws, "done")


class TestStopAndQueue:
    def test_stop_cancels_task_waiting_on_approval(self, app_env, tmp_path):
        from aish.agent import CANCELLED_RESULT

        marker = tmp_path / "never"
        client, chat = make_client(
            app_env,
            [model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")])],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            recv_until(ws, "approval_request")
            ws.send_json({"type": "stop"})
            done = recv_until(ws, "done")
            assert done["result"] == CANCELLED_RESULT
            assert not marker.exists()
            assert len(chat.calls) == 1  # no model call after the stop
            # Stop with nothing running must not dead-end (#48): it reconciles
            # the foreground to idle with a benign `stopped` sync, never an
            # `error` the UI would render as a task failure.
            ws.send_json({"type": "stop"})  # nothing running anymore
            stopped = recv_until(ws, "stopped")
            assert stopped["type"] == "stopped"

    def test_message_while_busy_injected_into_running_task(self, app_env, tmp_path):
        # #95: a message typed while a task runs still queues (chip appears), but
        # is now DRAINED and INJECTED into the running task between steps —
        # steering, not a deferred separate task. Consumed exactly once.
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
                model_says("first answer"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "first task"})
            request = recv_until(ws, "approval_request")

            ws.send_json({"type": "task", "text": "second task"})
            queued = recv_until(ws, "queued")  # still queues as a chip
            assert queued["position"] == 1

            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            injected = recv_step(ws, "injected")  # drained + injected mid-task
            assert injected["text"] == "second task"
            done = recv_until(ws, "done")
            assert done["result"] == "first answer"  # same task, no second `done`
            server = client.app.state.server
            assert server.active.queue == []  # consumed once, not relaunched
            assert any(
                m.get("content") == "second task" for m in server.active.agent.messages
            )

    def test_bang_command_queued_while_busy_runs_as_shell_not_injected(
        self, app_env, tmp_path
    ):
        # #105: a ! command queued while busy must run as the user's OWN shell
        # command (via _finish_turn/_launch → _run_user_command), not be drained
        # mid-task and injected as a plain model prompt.
        bang_file = tmp_path / "bang"
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
                model_says("first answer"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "first task"})
            request = recv_until(ws, "approval_request")

            ws.send_json({"type": "task", "text": f"!touch {bang_file}"})
            assert recv_until(ws, "queued")["position"] == 1

            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            # First task finishes with NO mid-task injection of the ! item.
            assert recv_until(ws, "done")["result"] == "first answer"
            # _finish_turn relaunches it as a user-direct command: it echoes as a
            # `user` event carrying the ! text (never an `injected` steering step).
            assert recv_until(ws, "user")["text"] == f"!touch {bang_file}"
            recv_until(ws, "done")  # the ! command's own (empty) done

        # It actually ran as a shell command (the file exists) and was never
        # injected verbatim as a model steering message.
        assert bang_file.exists()
        server = client.app.state.server
        assert server.active.queue == []
        assert not any(
            m.get("content") == f"!touch {bang_file}"
            for m in server.active.agent.messages
        )


class TestMultiConnection:
    """#102: N connections (phone, laptop, headless test) share one token and
    coexist WITHOUT preempting — each views a session independently, events fan
    out to every viewer, and control is last-actor-drives."""

    def test_second_connection_does_not_preempt_and_both_get_events(self, app_env):
        # Two sockets viewing the same session both receive its events, and the
        # first is NOT closed when the second connects (the old CLOSE_REPLACED
        # behaviour is gone).
        client, _ = make_client(app_env, [model_says("shared answer")])
        with client, connected(client) as (ws_a, hello_a, _):
            name = hello_a["session"]
            with connected(client, f"/ws?session={name}") as (ws_b, hello_b, _):
                assert hello_b["session"] == name  # B joined the SAME session
                # A is still alive (not preempted): its action drives both views.
                ws_a.send_json({"type": "task", "text": "go"})
                assert recv_until(ws_a, "user")["text"] == "go"
                assert recv_until(ws_b, "user")["text"] == "go"  # fanned to B too
                assert recv_until(ws_a, "done")["result"] == "shared answer"
                assert recv_until(ws_b, "done")["result"] == "shared answer"

    def test_action_stamps_control_and_broadcasts_role_to_both(self, app_env, tmp_path):
        # An action from B claims control; both viewers get a `role` event so
        # each tab knows whether IT drives.
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws_a, hello_a, _):
            name = hello_a["session"]
            with connected(client, f"/ws?session={name}") as (ws_b, _, _):
                # B acts (a /cd — no model needed) → B becomes the controller.
                ws_b.send_json({"type": "cd", "path": str(tmp_path)})
                role_b = recv_until(ws_b, "role")
                role_a = recv_until(ws_a, "role")
                assert role_b["you"] is True  # B drives
                assert role_a["you"] is False  # A is now an observer
                # Same controller id reported to both tabs.
                assert role_a["controller"] == role_b["controller"]
                assert role_b["controller"] is not None
                server = client.app.state.server
                assert server.sessions[name].controller is not None

    def test_either_client_can_answer_approval_exactly_once(self, app_env, tmp_path):
        # The approval card fans out to both viewers; the NON-initiating client
        # answers it and the command runs exactly once (the event loop
        # serializes messages, so only one answer() reaches the blocked worker).
        marker = tmp_path / "shared-ran"
        client, chat = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
                model_says("finished"),
            ],
        )
        with client, connected(client) as (ws_a, hello_a, _):
            name = hello_a["session"]
            with connected(client, f"/ws?session={name}") as (ws_b, _, _):
                ws_a.send_json({"type": "task", "text": "run it"})
                req_a = recv_until(ws_a, "approval_request")
                req_b = recv_until(ws_b, "approval_request")
                assert req_a["id"] == req_b["id"]  # same card on both
                # B (not the initiator) approves.
                ws_b.send_json({"type": "approval", "id": req_b["id"], "action": "approve"})
                assert recv_until(ws_a, "done")["result"] == "finished"
                assert recv_until(ws_b, "done")["result"] == "finished"
                assert marker.exists()
                # Exactly two model calls (initial + post-tool) proves the
                # command ran once — a double answer would have re-run it and
                # over-run the scripted responses into an error.
                assert len(chat.calls) == 2
                # A stale duplicate answer from A is a harmless no-op: the slot
                # was consumed, so it neither errors nor re-runs anything.
                ws_a.send_json({"type": "approval", "id": req_a["id"], "action": "approve"})
                ws_a.send_json({"type": "jobs"})
                assert recv_until(ws_a, "job_list")  # A's stream stays healthy

    def test_viewers_of_different_sessions_are_isolated(self, app_env):
        # A client viewing session X receives nothing from activity in session Y.
        client, _ = make_client(app_env, [model_says("beta answer")])
        with client, connected(client) as (ws_a, _, _):
            # B opens a brand-new session and runs a whole task there.
            with connected(client) as (ws_b, _, _):
                ws_b.send_json({"type": "new"})
                recv_until(ws_b, "hello")
                recv_until(ws_b, "replay")
                ws_b.send_json({"type": "task", "text": "beta"})
                assert recv_until(ws_b, "done")["result"] == "beta answer"
                # A viewed the original session throughout. It may get a
                # cross-session `session_state` heads-up (the drawer badge), but
                # NONE of Y's transcript events (user/token/step/done/command)
                # leak into A's stream. Drain up to A's own jobs reply.
                ws_a.send_json({"type": "jobs"})
                leaked = {"user", "token", "step", "done", "command_start",
                          "command_end", "stream", "approval_request"}
                for _ in range(50):
                    ev = ws_a.receive_json()
                    if ev["type"] == "job_list":
                        break
                    assert ev["type"] not in leaked, f"leaked Y event: {ev['type']}"
                else:
                    raise AssertionError("A never got its jobs reply")

    def test_disconnect_clears_viewer_and_releases_control(self, app_env, tmp_path):
        # When the controller disconnects, control is released and the remaining
        # viewer is told (role → controller null); the viewer set drops it. B is
        # the OUTER (surviving) connection so it can observe A leaving.
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws_b, hello_b, _):
            name = hello_b["session"]
            with connected(client, f"/ws?session={name}") as (ws_a, _, _):
                ws_a.send_json({"type": "cd", "path": str(tmp_path)})  # A claims control
                recv_until(ws_a, "role")  # A: you=true
                recv_until(ws_b, "role")  # B: you=false, controller = A
                server = client.app.state.server
                assert len(server.sessions[name].viewers) == 2
            # A disconnected (inner scope exited). B, still open, is told control
            # was released — a deterministic signal that _detach ran.
            released = recv_until(ws_b, "role")
            assert released["controller"] is None
            assert released["you"] is False
            sess = client.app.state.server.sessions[name]
            assert sess.controller is None
            assert len(sess.viewers) == 1  # only B remains

    def test_eviction_skips_sessions_with_viewers(self, app_env, monkeypatch):
        # A non-default session that still has a viewer is never evicted, even as
        # other viewerless sessions are churned past the cap.
        monkeypatch.setattr(server_module, "MAX_OPEN_SESSIONS", 3)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws_a, _, _):
            # A moves onto its own non-default session and stays there.
            ws_a.send_json({"type": "new"})
            held = recv_until(ws_a, "hello")["session"]
            recv_until(ws_a, "replay")
            server = client.app.state.server
            with connected(client) as (ws_b, _, _):
                # B churns sessions to drive eviction. Capture the first one it
                # abandons — it is the viewerless candidate that must be evicted.
                ws_b.send_json({"type": "new"})
                churned = recv_until(ws_b, "hello")["session"]
                recv_until(ws_b, "replay")
                ws_b.send_json({"type": "new"})  # abandons `churned` (now viewerless)
                recv_until(ws_b, "hello")
                recv_until(ws_b, "replay")
                ws_b.send_json({"type": "new"})  # cap hit → eviction runs
                recv_until(ws_b, "hello")
                recv_until(ws_b, "replay")
                assert held in server.sessions  # kept: A still views it
                assert churned not in server.sessions  # evicted: viewerless


class RaisingChat:
    """A backend that always raises, simulating a model/transport failure. The
    agent retries once then surfaces ModelUnavailable, so run_task raises and
    the server must emit a terminal error that clears the foreground."""

    def __init__(self, message: str = "boom: model exploded"):
        self.message = message
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError(self.message)


def raising_client(app_env) -> TokenClient:
    app = create_app("fake", client_chat=RaisingChat(), token=TEST_TOKEN, **app_env)
    return TokenClient(app, auto_token=TEST_TOKEN)


def recv_any(ws, wanted: str, limit: int = 200) -> dict:
    """Like recv_until but does NOT treat an `error` event as fatal — used
    when the error IS the event under test."""
    for _ in range(limit):
        event = ws.receive_json()
        if event["type"] == wanted:
            return event
    raise AssertionError(f"no {wanted!r} event within {limit} events")


class TestModelError:
    """#48: a mid-task model error must leave the session and its foreground
    consistent — a terminal event clears busy, the busy flag is false, Stop
    afterward is a graceful no-op, and a cold re-attach shows it finished."""

    def test_error_emits_terminal_and_clears_busy(self, app_env):
        client = raising_client(app_env)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "do it"})
            error = recv_any(ws, "error")
            assert "model unavailable" in error["text"]
            # Server-side truth: the busy flag cleared with the error.
            assert client.app.state.server.active.busy is False
            assert client.app.state.server.active.state() == "idle"

    def test_stop_after_error_is_graceful_noop(self, app_env):
        client = raising_client(app_env)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "do it"})
            recv_any(ws, "error")
            # The wedged-foreground reconciliation: Stop never dead-ends.
            ws.send_json({"type": "stop"})
            stopped = recv_any(ws, "stopped")
            assert stopped["type"] == "stopped"

    def test_reattached_errored_session_shows_finished(self, app_env):
        # Re-attaching an errored session (switch away and back, or phone
        # lock/unlock) must report idle (not running) and replay the recorded
        # error — never a stuck "working" foreground.
        client = raising_client(app_env)
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "do it"})
            recv_any(ws, "error")
            # Re-show the same session: hello reports its authoritative state
            # and the transcript replay carries the terminal error.
            ws.send_json({"type": "resume", "path": name})
            hello2 = recv_any(ws, "hello")
            replay = recv_any(ws, "replay")
            assert hello2["session"] == name
            assert hello2["busy"] is False
            assert any(e["type"] == "error" for e in replay["events"])

    def test_errored_session_is_deletable(self, app_env):
        # busy cleared → state() == "idle" → the delete guard allows removal.
        client = raising_client(app_env)
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "do it"})
            recv_any(ws, "error")
            ws.send_json({"type": "delete_session", "name": name})
            deleted = recv_any(ws, "session_deleted")
            assert deleted["name"] == name


class TestSessions:
    def test_new_session_swaps_log_and_clears_transcript(self, app_env):
        client, _ = make_client(app_env, [model_says("answer one")])
        with client:
            with connected(client) as (ws, hello, _):
                first = hello["session"]
                ws.send_json({"type": "task", "text": "task one"})
                recv_until(ws, "done")
                ws.send_json({"type": "new"})
                fresh = recv_until(ws, "hello")
                assert fresh["session"] != first
                # The empty replay is the client's clear-screen signal.
                cleared = recv_until(ws, "replay")
                assert cleared["events"] == []
            # A reconnect naming the fresh session (as the real client does via
            # ?session=) replays it empty. A BARE reconnect now lands on the
            # default startup session instead (#102), not the last-shown one.
            with connected(client, f"/ws?session={fresh['session']}") as (_ws, _, replay):
                assert replay["events"] == []

    def test_session_list_reports_waiting_state_for_pending_approval(
        self, app_env, tmp_path
    ):
        # The drawer's "Active now" grouping keys off this per-session state:
        # a session blocked on an approval must surface as "waiting".
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("done"),
            ],
        )
        with client, connected(client) as (ws, hello, _):
            current = hello["session"]
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")  # agent now blocked
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            row = next(s for s in listing["sessions"] if s["name"] == current)
            assert row["state"] == "waiting"
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_session_list_includes_and_names_current(self, app_env):
        # The drawer lists the active session too (MRU: it sorts first) and
        # names it in "current" so the UI can mark "you are here" (#29).
        client, _ = make_client(app_env, [model_says("alpha done")])
        with client, connected(client) as (ws, hello, _):
            session_a = hello["session"]
            ws.send_json({"type": "task", "text": "alpha task"})
            recv_until(ws, "done")

            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert listing["current"] == session_a
            row = listing["sessions"][0]
            assert row["name"] == session_a
            # The drawer's preview line and day-grouping timestamp.
            assert row["snippet"] == "alpha done"
            assert row["ts"] > 0

            # A brand-new chat is current but has no messages yet, so it is
            # not listed — nothing carries the current mark.
            ws.send_json({"type": "new"})
            fresh = recv_until(ws, "hello")
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert listing["current"] == fresh["session"]
            names = [s["name"] for s in listing["sessions"]]
            assert fresh["session"] not in names
            assert session_a in names

    def test_session_list_labels_directory_from_the_log_when_cold(self, app_env):
        # The row's directory used to come ONLY from sessions still open in
        # memory, so it showed on a handful of rows and silently vanished from
        # the rest as the eviction sweep closed them. It now falls back to the
        # cwd recorded in the log, and a session sitting in the server's own
        # workspace shows none at all — the same path on every row is noise.
        state_dir = app_env["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        elsewhere = Path(app_env["cwd"]) / "worktree-a"
        elsewhere.mkdir()
        moved = state_dir / "session-20200101-000000-000000.jsonl"
        moved.write_text(
            '{"kind": "message", "role": "user", "content": "in the worktree"}\n'
            + json.dumps({"kind": "cwd", "cwd": str(elsewhere)})
            + '\n{"kind": "message", "role": "assistant", "content": "worktree answer"}\n',
            encoding="utf-8",
        )

        client, _ = make_client(app_env, [model_says("home answer")])
        with client, connected(client) as (ws, hello, _):
            here = hello["session"]
            ws.send_json({"type": "task", "text": "at home"})
            recv_until(ws, "done")

            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            rows = {s["name"]: s["cwd"] for s in listing["sessions"]}
            assert rows[moved.name] == str(elsewhere)  # cold — read from its log
            assert rows[here] == ""  # open, but never left the baseline workspace

            # A live session outranks its log: it may have moved since the last
            # recorded /cd, so the in-memory agent answers for it.
            ws.send_json({"type": "cd", "path": str(elsewhere)})
            recv_until(ws, "cwd_changed")
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            rows = {s["name"]: s["cwd"] for s in listing["sessions"]}
            assert rows[here] == str(elsewhere)

    def test_reviewing_old_session_keeps_order_until_new_message(self, app_env):
        # Resuming an older session only READS it: the file keeps its mtime,
        # so the MRU order (drawer + swipe pager) is unchanged. Only a new
        # message makes the session "latest" again.
        state_dir = app_env["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        old = state_dir / "session-20200101-000000-000000.jsonl"
        old.write_text(
            '{"kind": "message", "role": "user", "content": "old topic"}\n'
            '{"kind": "message", "role": "assistant", "content": "old answer"}\n',
            encoding="utf-8",
        )
        stale = time.time() - 3600
        os.utime(old, (stale, stale))

        client, _ = make_client(app_env, [model_says("fresh done"), model_says("revived")])
        with client, connected(client) as (ws, hello, _):
            fresh = hello["session"]
            ws.send_json({"type": "task", "text": "fresh topic"})
            recv_until(ws, "done")

            ws.send_json({"type": "resume", "path": old.name})
            recv_until(ws, "hello")
            assert os.path.getmtime(old) == pytest.approx(stale, abs=1)
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert [s["name"] for s in listing["sessions"]] == [fresh, old.name]
            assert listing["current"] == old.name

            ws.send_json({"type": "task", "text": "revive it"})
            recv_until(ws, "done")
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert [s["name"] for s in listing["sessions"]] == [old.name, fresh]

    def test_list_and_resume_previous_session(self, app_env):
        client, chat = make_client(
            app_env, [model_says("first answer"), model_says("second answer")]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "remember the zebra"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            recv_until(ws, "hello")

            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert listing["sessions"], "previous session missing from list"
            assert "zebra" in listing["sessions"][0]["title"]

            resumed_name = listing["sessions"][0]["name"]
            ws.send_json({"type": "resume", "path": resumed_name})
            hello = recv_until(ws, "hello")
            assert hello["session"] == resumed_name  # switched, not merged
            replay = recv_until(ws, "replay")
            # Still open in memory: the live transcript replays as-is.
            users = [e for e in replay["events"] if e["type"] == "user"]
            assert users and "zebra" in users[0]["text"]

            ws.send_json({"type": "task", "text": "what animal did I mention?"})
            recv_until(ws, "done")
            contents = json.dumps(chat.calls[-1]["messages"])
            assert "zebra" in contents  # resumed context reached the model

    def test_hello_pager_pages_recent_chats_oldest_first(self, app_env):
        # The swipe pager pages through hello["pager"]: recent chats by last
        # interaction, oldest→newest (back = older, forward = newer). Chats
        # with no user input are not pages — except the current one.
        client, _ = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, hello, _):
            first = hello["session"]
            assert [p["name"] for p in hello["pager"]] == [first]
            ws.send_json({"type": "task", "text": "remember the yak"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            hello = recv_until(ws, "hello")
            second = hello["session"]
            assert [p["name"] for p in hello["pager"]] == [first, second]
            assert hello["pager"][0]["title"] == "remember the yak"
            recv_until(ws, "replay")
            # Back on the first chat, the still-empty new one is not a page.
            ws.send_json({"type": "resume", "path": first})
            hello = recv_until(ws, "hello")
            assert [p["name"] for p in hello["pager"]] == [first]

    def test_every_pager_page_carries_a_usable_timestamp(self, app_env):
        # The client's working-set deck seeds from the pager and filters seed
        # candidates by age, so an undated page is silently discarded. With no
        # `ts` here, a launch that never opened the Sessions drawer seeded
        # NOTHING and the swipe pager was left with a single page — swiping
        # between chats did nothing at all. Every page must carry a real stamp,
        # including the current chat's synthesized page (the one with no file
        # activity behind it yet).
        client, _ = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, hello, _):
            first = hello["session"]
            # The synthesized page for a brand-new, still-empty chat.
            assert [p["name"] for p in hello["pager"]] == [first]
            assert hello["pager"][0]["ts"] > 0
            ws.send_json({"type": "task", "text": "remember the yak"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            hello = recv_until(ws, "hello")
            # Now a real on-disk page plus the synthesized one: both stamped,
            # and ordered oldest→newest by that stamp, matching the page order.
            stamps = [p["ts"] for p in hello["pager"]]
            assert len(stamps) == 2
            assert all(isinstance(t, (int, float)) and t > 0 for t in stamps)
            assert stamps == sorted(stamps)

    def test_pager_orders_by_last_interaction_and_spans_restarts(self, app_env):
        # Interacting with an old chat moves it to the newest end, and a
        # fresh server lists chats it never opened (swipe loads them from
        # disk via resume) — same recency order as the sessions drawer.
        responses = [model_says("a"), model_says("b"), model_says("a2")]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello, _):
            session_a = hello["session"]
            ws.send_json({"type": "task", "text": "alpha task"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            hello = recv_until(ws, "hello")
            session_b = hello["session"]
            ws.send_json({"type": "task", "text": "beta task"})
            recv_until(ws, "done")
            ws.send_json({"type": "resume", "path": session_a})
            recv_until(ws, "replay")
            ws.send_json({"type": "task", "text": "alpha again"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            hello = recv_until(ws, "hello")
            assert [p["name"] for p in hello["pager"]] == [
                session_b, session_a, hello["session"]
            ]
        client2, _ = make_client(app_env, [])
        with client2, connected(client2) as (_ws, hello, _):
            names = [p["name"] for p in hello["pager"]]
            assert names[:2] == [session_b, session_a]  # never opened here
            assert names[-1] == hello["session"]

    def test_resume_from_disk_replays_history(self, app_env):
        # First server instance writes a session to disk…
        client, _ = make_client(app_env, [model_says("noted the walrus")])
        with client, connected(client) as (ws, hello, _):
            old_name = hello["session"]
            ws.send_json({"type": "task", "text": "remember the walrus"})
            recv_until(ws, "done")
        # …a fresh instance (nothing in memory) reopens it from the file.
        client2, chat2 = make_client(app_env, [model_says("the walrus")])
        with client2, connected(client2) as (ws, _, _):
            ws.send_json({"type": "resume", "path": old_name})
            hello = recv_until(ws, "hello")
            assert hello["session"] == old_name
            replay = recv_until(ws, "replay")
            # A logged session reconstructs into the same user/step/done event
            # stream a live one replays — not a flat history blob.
            user_ev = next(e for e in replay["events"] if e["type"] == "user")
            assert "walrus" in user_ev["text"]
            done_ev = next(e for e in replay["events"] if e["type"] == "done")
            assert "noted the walrus" in done_ev["result"]

            ws.send_json({"type": "task", "text": "what animal?"})
            recv_until(ws, "done")
            assert "walrus" in json.dumps(chat2.calls[-1]["messages"])

    def test_cold_reconstruction_matches_live_transcript(self, app_env):
        # The guard for the hot/cold invariant. A live run's canonical event
        # record (bridge.transcript) and the cold reconstruction from its log
        # must project to the SAME durable trace shape. This is the single test
        # that keeps the two paths from drifting: add a new trace event type to
        # the live stream without persisting + reconstructing it (as command
        # framing once was) and this fails immediately.
        client, _ = make_client(app_env, [
            model_says(tool_calls=[tool_call("run_command", command="ls")]),
            model_says("listed the directory"),
        ])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "list files"})
            recv_until(ws, "done")
            server = client.app.state.server
            hot = list(server.active.bridge.transcript)
            path = server.active.logref.log.path

        cold = SessionLog.reconstruct_events(path)
        assert cold is not None
        # A run_command must survive the round-trip as its full terminal-block
        # sequence, not a bare tool step — the whole point of the framing work.
        assert ("command_start",) in trace_shape(cold)
        assert trace_shape(hot) == trace_shape(cold)

    def test_cold_reconstruction_matches_live_for_held_command(self, app_env, tmp_path):
        # #81: an approve+comment HOLD never runs, so it emits no terminal block
        # (like a denial). Cold replay must match — the None-framing synthesize
        # path must NOT fabricate a command_start for a command that never ran.
        # A mutating command (not read-only) so it actually prompts.
        client, _ = make_client(app_env, [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/z")]),
            model_says("acknowledged"),
        ])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "make a file"})
            request = recv_until(ws, "approval_request")
            ws.send_json({
                "type": "approval", "id": request["id"],
                "action": "approve", "comment": "put it under tmp/ instead",
            })
            recv_until(ws, "done")
            server = client.app.state.server
            hot = list(server.active.bridge.transcript)
            path = server.active.logref.log.path

        cold = SessionLog.reconstruct_events(path)
        assert cold is not None
        assert ("command_start",) not in trace_shape(cold)  # held → no terminal block
        assert trace_shape(hot) == trace_shape(cold)

    def test_cwd_and_trust_changes_log_and_reconstruct(self, app_env, tmp_path):
        # #94: a /cd and a /add-dir emit live `workspace` timeline markers AND
        # persist, so the cold reconstruction projects the identical shape —
        # the same hot/cold invariant the trace and command framing obey.
        elsewhere, shared = tmp_path / "elsewhere", tmp_path / "shared"
        elsewhere.mkdir()
        shared.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "cd", "path": str(elsewhere)})
            live_cd = recv_until(ws, "workspace")
            assert live_cd["change"] == "cwd" and live_cd["path"] == str(elsewhere)
            ws.send_json({"type": "add_dir", "path": str(shared)})
            live_trust = recv_until(ws, "workspace")
            assert live_trust["change"] == "trust"
            server = client.app.state.server
            hot = list(server.active.bridge.transcript)
            path = server.active.logref.log.path

        cold = SessionLog.reconstruct_events(path)
        assert cold is not None
        shape = trace_shape(cold)
        assert ("workspace", "cwd", str(elsewhere)) in shape
        assert ("workspace", "trust", str(shared.resolve())) in shape
        # The consistency invariant: the live `workspace` events and the ones
        # reconstruct_events replays are byte-identical. (The full-transcript
        # shape differs only by the /cd + /add-dir context notes the agent
        # injects into the conversation, which predate #94.)
        ws_hot = [e for e in hot if e["type"] == "workspace"]
        ws_cold = [e for e in cold if e["type"] == "workspace"]
        assert ws_hot == ws_cold

    def test_cold_open_restores_cwd_and_trusted_roots(self, app_env, tmp_path):
        # #94: reopening a session cold restores where it left off (cwd + the
        # dirs it trusted), not the server's launch dir.
        elsewhere, shared = tmp_path / "elsewhere", tmp_path / "shared"
        elsewhere.mkdir()
        shared.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "cd", "path": str(elsewhere)})
            recv_until(ws, "workspace")
            ws.send_json({"type": "add_dir", "path": str(shared)})
            recv_until(ws, "workspace")

        # Fresh server over the same state dir → the session is cold-loaded.
        client2, _ = make_client(app_env, [])
        with client2, connected(client2, f"/ws?session={name}") as (_, hello2, _):
            assert hello2["cwd"] == str(elsewhere)
            assert str(shared.resolve()) in hello2["roots"]

    def test_cold_open_keeps_the_servers_uploads_root(self, app_env, tmp_path):
        # #176: restoring a workspace is AUTHORITATIVE — it rebuilds roots to be
        # exactly that chat's own, so a dir from another chat can't ride along.
        # The uploads dir belongs to the SERVER, not to any one chat, and is
        # re-added afterwards; without that a cold-opened session would lose
        # read access to its own attachments.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "cd", "path": str(elsewhere)})
            recv_until(ws, "workspace")

        client2, _ = make_client(app_env, [])
        with client2, connected(client2, f"/ws?session={name}") as (_, hello2, _):
            uploads = client2.app.state.server.uploads_dir.resolve()
            assert hello2["cwd"] == str(elsewhere)
            assert str(uploads) in hello2["roots"]

    def test_cold_open_skips_vanished_cwd(self, app_env, tmp_path):
        # #94: a restored cwd that no longer exists falls back to the default
        # instead of crashing the cold open.
        gone = tmp_path / "gone"
        gone.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "cd", "path": str(gone)})
            recv_until(ws, "workspace")
        gone.rmdir()  # the directory disappears before the session is reopened

        client2, _ = make_client(app_env, [])
        with client2, connected(client2, f"/ws?session={name}") as (_, hello2, _):
            assert hello2["cwd"] == app_env["cwd"]  # gracefully back to default

    def test_connect_with_session_param_reattaches_after_restart(self, app_env):
        # The client names its session on (re)connect so a server restart
        # doesn't strand it in the fresh startup session.
        client, _ = make_client(app_env, [model_says("noted the walrus")])
        with client, connected(client) as (ws, hello, _):
            old_name = hello["session"]
            ws.send_json({"type": "task", "text": "remember the walrus"})
            recv_until(ws, "done")
        client2, _ = make_client(app_env, [])
        with client2, connected(client2, f"/ws?session={old_name}") as (_, hello, replay):
            assert hello["session"] == old_name
            user_evs = [e for e in replay["events"] if e["type"] == "user"]
            assert user_evs and any("walrus" in e["text"] for e in user_evs)

    def test_connect_with_unknown_session_falls_back_to_active(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client, "/ws?session=session-gone.jsonl") as (_, hello, _):
            assert hello["session"].startswith("session-")
            assert hello["session"] != "session-gone.jsonl"

    def test_parallel_sessions_run_and_finish_independently(self, app_env, tmp_path):
        # Session A blocks on an approval; session B runs a full task while A
        # is still waiting; switching back to A replays the pending card and
        # approving it finishes A's task.
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
            model_says("B says hi"),  # session B's whole task
            model_says("A finished"),  # session A resumes after approval
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello_a, _):
            session_a = hello_a["session"]
            ws.send_json({"type": "task", "text": "touch a file"})
            request = recv_until(ws, "approval_request")

            ws.send_json({"type": "new"})
            hello_b = recv_until(ws, "hello")
            assert hello_b["session"] != session_a
            recv_until(ws, "replay")

            ws.send_json({"type": "task", "text": "say hi"})
            done_b = recv_until(ws, "done")
            assert done_b["result"] == "B says hi"

            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            state_by_name = {s["name"]: s["state"] for s in listing["sessions"]}
            assert state_by_name[session_a] == "waiting"

            ws.send_json({"type": "resume", "path": session_a})
            back = recv_until(ws, "hello")
            assert back["session"] == session_a and back["busy"] is True
            replay = recv_until(ws, "replay")
            pending = [e for e in replay["events"] if e["type"] == "approval_request"]
            assert pending and pending[0]["id"] == request["id"]

            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            done_a = recv_until(ws, "done")
            assert done_a["result"] == "A finished"
            assert (tmp_path / "a").exists()

    def test_background_finish_sends_notice(self, app_env, tmp_path):
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("A done in background"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello_a, _):
            session_a = hello_a["session"]
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "new"})
            recv_until(ws, "hello")
            recv_until(ws, "replay")
            # Approve A's card while B is shown: A finishes in the background
            # and the client gets a session_state heads-up.
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            notice = recv_until(ws, "session_state")
            assert notice["session"] == session_a
            assert notice["state"] == "idle"

    def test_resume_from_disk_restores_recorded_model(self, app_env, monkeypatch):
        switched = FakeChat([model_says("hi from gemini"), model_says("still gemini")])
        monkeypatch.setattr(
            server_module.backends,
            "make_chat",
            lambda spec: (switched, "gemini", "gemini-3-pro"),
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "set_model", "spec": "gemini:gemini-3-pro"})
            recv_until(ws, "model_changed")
            ws.send_json({"type": "task", "text": "hello there"})
            recv_until(ws, "done")
        # Fresh server instance: nothing in memory, must restore from the log.
        client2, _ = make_client(app_env, [])
        with client2, connected(client2) as (ws, _, _):
            ws.send_json({"type": "resume", "path": name})
            hello2 = recv_until(ws, "hello")
            assert hello2["model"] == "gemini:gemini-3-pro"  # sticky, not reset

    def test_resume_rejects_path_escape(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "resume", "path": "../../../etc/passwd"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "no such session" in error["text"]

    def test_delete_background_session_removes_file_and_list_entry(self, app_env):
        client, _ = make_client(app_env, [model_says("noted")])
        with client, connected(client) as (ws, hello, _):
            first = hello["session"]
            ws.send_json({"type": "task", "text": "remember the zebra"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            recv_until(ws, "hello")

            ws.send_json({"type": "delete_session", "name": first})
            recv_until(ws, "session_deleted")
            listing = recv_until(ws, "session_list")
            assert first not in [s["name"] for s in listing["sessions"]]
            assert not (app_env["state_dir"] / first).exists()

    def test_delete_leaves_sibling_session_untouched(self, app_env):
        # The title-menu "Delete chat" only ever names ONE session; a second
        # real session (its file and its open in-memory entry) must survive.
        client, _ = make_client(app_env, [model_says("a"), model_says("b")])
        with client, connected(client) as (ws, hello, _):
            first = hello["session"]
            ws.send_json({"type": "task", "text": "first topic"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            second = recv_until(ws, "hello")["session"]
            ws.send_json({"type": "task", "text": "second topic"})
            recv_until(ws, "done")

            ws.send_json({"type": "delete_session", "name": first})
            recv_until(ws, "session_deleted")
            listing = recv_until(ws, "session_list")
            names = [s["name"] for s in listing["sessions"]]
            assert first not in names
            assert second in names
            assert not (app_env["state_dir"] / first).exists()
            assert (app_env["state_dir"] / second).is_file()

    def test_delete_active_session_lands_on_new_chat(self, app_env):
        client, _ = make_client(app_env, [model_says("noted")])
        with client, connected(client) as (ws, hello, _):
            first = hello["session"]
            ws.send_json({"type": "task", "text": "remember the zebra"})
            recv_until(ws, "done")
            assert (app_env["state_dir"] / first).is_file()

            ws.send_json({"type": "delete_session", "name": first})
            # Client is moved to a fresh chat BEFORE the delete happens.
            fresh = recv_until(ws, "hello")
            assert fresh["session"] != first
            cleared = recv_until(ws, "replay")
            assert cleared["events"] == []
            recv_until(ws, "session_deleted")
            listing = recv_until(ws, "session_list")
            assert listing["current"] == fresh["session"]
            assert first not in [s["name"] for s in listing["sessions"]]
            assert not (app_env["state_dir"] / first).exists()

    def test_delete_cold_session_straight_from_disk(self, app_env):
        state_dir = app_env["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        old = state_dir / "session-20200101-000000-000000.jsonl"
        old.write_text(
            '{"kind": "message", "role": "user", "content": "old topic"}\n',
            encoding="utf-8",
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "delete_session", "name": old.name})
            recv_until(ws, "session_deleted")
            recv_until(ws, "session_list")
            assert not old.exists()

    def test_delete_running_session_refused(self, app_env, tmp_path):
        marker = tmp_path / "never"
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
                model_says("gave up"),
            ],
        )
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "touch it"})
            request = recv_until(ws, "approval_request")

            ws.send_json({"type": "delete_session", "name": name})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "still running" in error["text"]
            assert (app_env["state_dir"] / name).is_file()

            # The pending approval survived the refused delete untouched.
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")

    def test_delete_rejects_path_escape_and_unknown_names(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            for name in ("../../../etc/passwd", "session-nonexistent.jsonl"):
                ws.send_json({"type": "delete_session", "name": name})
                error = ws.receive_json()
                assert error["type"] == "error"
                assert "no such session" in error["text"]


class TestDeckCheck:
    """The client's swipe deck is per-device localStorage, so it outlives
    sessions deleted elsewhere — a "phantom page" that used to swallow the
    swipe aimed at it. `deck_check` answers with ground truth from stat(), so
    the deck heals WITHOUT ever treating absence from a capped or filtered
    listing as deletion (the state dir can hold 15x more sessions than the
    30-page pager window), and the by-name miss error is machine-readable so
    the client prunes on the code, never on prose."""

    @staticmethod
    def _write_session(state_dir, stamp, content="a topic"):
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / f"session-20200101-{stamp:06d}-000000.jsonl"
        path.write_text(
            json.dumps({"kind": "message", "role": "user", "content": content}) + "\n",
            encoding="utf-8",
        )
        os.utime(path, (1577836800 + stamp, 1577836800 + stamp))
        return path.name

    def test_missing_and_unsafe_names_reported_gone_alive_files_never(self, app_env):
        state_dir = app_env["state_dir"]
        # 33 real chats: more than the pager window lists, so the oldest are
        # invisible to every capped listing yet must never be called gone —
        # pruning on that absence would delete most of a long-lived working set.
        names = [self._write_session(state_dir, i) for i in range(33)]
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            pager_names = {p["name"] for p in hello["pager"]}
            assert [n for n in names if n not in pager_names], "cap not exceeded"
            asked = names + ["session-19990101-000000-000000.jsonl", "../../etc/passwd"]
            ws.send_json({"type": "deck_check", "names": asked})
            verdict = recv_until(ws, "deck_gone")
            assert set(verdict["names"]) == {
                "session-19990101-000000-000000.jsonl",  # deleted: file is gone
                "../../etc/passwd",  # unresolvable: can never be resumed
            }

    def test_open_unwritten_session_is_alive(self, app_env):
        # A brand-new chat records nothing until first use, so it has NO file
        # yet — but it is open in memory, and the deck member for the chat on
        # screen must never be pruned out from under the user.
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            current = hello["session"]
            assert not (app_env["state_dir"] / current).exists()
            ws.send_json(
                {
                    "type": "deck_check",
                    "names": [current, "session-19990101-000000-000000.jsonl"],
                }
            )
            verdict = recv_until(ws, "deck_gone")
            assert verdict["names"] == ["session-19990101-000000-000000.jsonl"]

    def test_all_alive_answers_nothing(self, app_env):
        name = self._write_session(app_env["state_dir"], 1)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "deck_check", "names": [name]})
            ws.send_json({"type": "sessions", "query": ""})
            event = ws.receive_json()
            # No deck_gone was queued ahead of the listing: silence means alive.
            assert event["type"] == "session_list"

    def test_resume_of_missing_session_is_machine_readable(self, app_env):
        # The frontend prunes the phantom on this code + name; the prose text
        # is for humans and is not a contract.
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "resume", "path": "session-19990101-000000-000000.jsonl"}
            )
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "no_such_session"
            assert error["name"] == "session-19990101-000000-000000.jsonl"
            assert "no such session" in error["text"]


class TestPeek:
    """`peek` is the swipe-neighbor prefetch: a VIEW message answering another
    session's transcript snapshot WITHOUT switching to it, so a committed
    swipe can paint instantly. It must not move the client's view, must not
    record anything, and a miss answers `gone` on the peek itself — never the
    resume path's error event, whose toast would blame the user for a request
    they never made."""

    @staticmethod
    def _write_session(state_dir, stamp, content="peek target topic"):
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / f"session-20200101-{stamp:06d}-000000.jsonl"
        path.write_text(
            json.dumps({"kind": "message", "role": "user", "content": content}) + "\n"
            + json.dumps({"kind": "message", "role": "assistant", "content": "the answer"})
            + "\n",
            encoding="utf-8",
        )
        return path.name

    def test_peek_returns_events_without_switching_view(self, app_env):
        name = self._write_session(app_env["state_dir"], 1)
        client, _ = make_client(app_env, [model_says("hi there")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "peek", "path": name})
            peek = recv_until(ws, "peek")
            assert peek["name"] == name
            assert peek["events"], "peek must carry the target's transcript"
            texts = json.dumps(peek["events"])
            assert "peek target topic" in texts
            # The client's view did NOT move: a task typed now still runs in
            # the original session (no hello/replay was pushed by the peek).
            ws.send_json({"type": "task", "text": "still here"})
            done = recv_until(ws, "done")
            assert done is not None
            assert hello["session"] != name

    def test_peek_is_idempotent_and_records_nothing(self, app_env):
        name = self._write_session(app_env["state_dir"], 2)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "peek", "path": name})
            first = recv_until(ws, "peek")
            ws.send_json({"type": "peek", "path": name})
            second = recv_until(ws, "peek")
            # Peeking must not grow the peeked transcript (nothing recorded).
            assert len(first["events"]) == len(second["events"])

    def test_peek_of_missing_session_answers_gone_not_error(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "peek", "path": "session-19990101-000000-000000.jsonl"}
            )
            peek = recv_until(ws, "peek")
            assert peek["gone"] is True
            assert peek["name"] == "session-19990101-000000-000000.jsonl"


class TestRename:
    def test_rename_active_session_updates_header_and_list(self, app_env):
        client, _ = make_client(app_env, [model_says("noted")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "the original derived title"})
            recv_until(ws, "done")

            ws.send_json({"type": "rename_session", "name": name, "title": "My Custom Name"})
            renamed = recv_until(ws, "session_renamed")
            assert renamed["name"] == name
            assert renamed["title"] == "My Custom Name"
            listing = recv_until(ws, "session_list")
            row = next(s for s in listing["sessions"] if s["name"] == name)
            assert row["title"] == "My Custom Name"

    def test_latest_rename_wins_across_reconnect(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "first message"})
            recv_until(ws, "done")
            for title in ("one", "two", "three"):
                ws.send_json({"type": "rename_session", "name": name, "title": title})
                recv_until(ws, "session_renamed")
                recv_until(ws, "session_list")

        # A fresh server (nothing in memory) must show the LATEST title on hello.
        client2, _ = make_client(app_env, [])
        with client2, client2.websocket_connect(f"/ws?session={name}") as ws:
            hello2 = ws.receive_json()
            assert hello2["type"] == "hello"
            assert hello2["title"] == "three"

    def test_rename_cold_session_from_disk(self, app_env):
        state_dir = app_env["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        old = state_dir / "session-20200101-000000-000000.jsonl"
        old.write_text(
            '{"kind": "message", "role": "user", "content": "old topic"}\n',
            encoding="utf-8",
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "rename_session", "name": old.name, "title": "Archived"})
            recv_until(ws, "session_renamed")
            listing = recv_until(ws, "session_list")
            row = next(s for s in listing["sessions"] if s["name"] == old.name)
            assert row["title"] == "Archived"
        # The renamed cold session still reconstructs its conversation cleanly.
        messages, _, custom_title, *_ = SessionLog._parse(old)
        assert custom_title == "Archived"
        assert messages == [{"role": "user", "content": "old topic"}]

    def test_rename_rejects_empty_title(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "hi"})
            recv_until(ws, "done")
            ws.send_json({"type": "rename_session", "name": name, "title": "   "})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "empty" in error["text"]

    def test_rename_rejects_path_escape_and_unknown_names(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            for name in ("../../../etc/passwd", "session-nonexistent.jsonl"):
                ws.send_json({"type": "rename_session", "name": name, "title": "x"})
                # rename stamps control first (a `role` event) before rejecting.
                error = recv_until(ws, "error")
                assert "no such session" in error["text"]


class TestAutoTitle:
    """#175 — a chat is named after its subject, not after its first prompt.

    The stored name is the same append-only `kind:"title"` record a rename
    writes, so the cold read path (drawer, pager) never makes a model call.
    """

    def _run(self, ws, text="tell me about the Bali eSIM options"):
        ws.send_json({"type": "task", "text": text})
        return recv_until(ws, "done")

    def test_first_answer_names_the_chat_and_persists_it(self, app_env):
        client, chat = make_client(
            app_env, [model_says("Airalo is the one.")], title="Bali eSIM data plans"
        )
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            self._run(ws)
            renamed = recv_until(ws, "session_renamed")
            assert renamed["title"] == "Bali eSIM data plans"
            assert renamed["name"] == name
        parsed = SessionLog._parse(app_env["state_dir"] / name)
        assert parsed.title == "Bali eSIM data plans"
        assert parsed.title_auto is True  # an auto name, replaceable by a later one

    def test_the_titler_sees_the_conversation_not_the_whole_transcript(self, app_env):
        client, chat = make_client(app_env, [model_says("Airalo.")], title="Bali eSIM plans")
        with client, connected(client) as (ws, _, _):
            self._run(ws)
            recv_until(ws, "session_renamed")
        prompt = chat.title_calls[-1]["messages"][0]["content"]
        assert "Bali eSIM" in prompt and "Airalo." in prompt
        assert len(chat.title_calls[-1]["messages"]) == 1  # never the real message list

    def test_a_hand_typed_rename_is_never_overwritten(self, app_env):
        """Even at a backoff slot the chat has plainly drifted to — naming is
        the user's call the moment they make it, permanently."""
        client, chat = make_client(
            app_env,
            [model_says("one"), model_says("two"), model_says("three")],
            title="A Model Chosen Name",
        )
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            self._run(ws)
            recv_until(ws, "session_renamed")
            ws.send_json({"type": "rename_session", "name": name, "title": "My own name"})
            recv_until(ws, "session_renamed")
            calls_after_rename = len(chat.title_calls)
            self._run(ws, "completely unrelated subject now")  # turn 2
            self._run(ws, "postgres index tuning please")  # turn 3 — a slot, drifted
            assert len(chat.title_calls) == calls_after_rename  # never even asked
        parsed = SessionLog._parse(app_env["state_dir"] / name)
        assert parsed.title == "My own name"
        assert parsed.title_auto is False

    def test_a_stopped_turn_is_not_named(self, app_env, tmp_path):
        """A cancel produced no content — naming a chat after it is noise."""
        from aish.agent import CANCELLED_RESULT

        marker = tmp_path / "never"
        client, chat = make_client(
            app_env,
            [model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")])],
            title="Should Not Appear",
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            recv_until(ws, "approval_request")
            ws.send_json({"type": "stop"})
            assert recv_until(ws, "done")["result"] == CANCELLED_RESULT
        assert not chat.title_calls

    def test_a_fork_earns_its_own_name_at_its_first_turn(self, app_env):
        """The fork copies the parent's log, so without this it wears the
        parent's name forever however far the tangent goes."""
        client, chat = make_client(
            app_env,
            [model_says("zebra"), model_says("giraffes are taller")],
            title="Parent chat name",
        )
        with client, connected(client) as (ws, hello, _):
            source = hello["session"]
            self._run(ws, "remember the zebra")
            recv_until(ws, "session_renamed")
            ws.send_json({"type": "fork"})
            forked = recv_until(ws, "hello")["session"]
            recv_until(ws, "replay")
            assert forked != source
            # The fork's FIRST turn retitles even though it is turn 2 of the
            # copied conversation (no backoff slot, no drift gate).
            chat.title = "Giraffe heights"
            self._run(ws, "what about giraffes?")
            renamed = recv_until(ws, "session_renamed")
            assert renamed["name"] == forked
            assert renamed["title"] == "Giraffe heights"
        assert SessionLog._parse(app_env["state_dir"] / source).title == "Parent chat name"

    def test_an_on_topic_second_turn_costs_no_model_call(self, app_env):
        """Turn 3 is a backoff slot, but a chat still on its subject skips the
        call entirely — the drift gate is free."""
        client, chat = make_client(
            app_env,
            [model_says("a"), model_says("b"), model_says("c")],
            title="Bali eSIM data plans",
        )
        with client, connected(client) as (ws, _, _):
            self._run(ws, "which eSIM for Bali?")
            recv_until(ws, "session_renamed")
            assert len(chat.title_calls) == 1
            self._run(ws, "and the data plans on Bali?")  # turn 2 — not a slot
            self._run(ws, "cheapest Bali eSIM data plans?")  # turn 3 — slot, no drift
            assert len(chat.title_calls) == 1  # still just the first

    def test_a_conversation_that_moves_on_is_renamed(self, app_env):
        client, chat = make_client(
            app_env,
            [model_says("a"), model_says("b"), model_says("c")],
            title="Bali eSIM data plans",
        )
        with client, connected(client) as (ws, _, _):
            self._run(ws, "which eSIM for Bali?")
            recv_until(ws, "session_renamed")
            chat.title = "Postgres index tuning"
            self._run(ws, "different topic entirely now")
            self._run(ws, "explain postgres index tuning please")  # turn 3 — drifted
            renamed = recv_until(ws, "session_renamed")
            assert renamed["title"] == "Postgres index tuning"
        assert len(chat.title_calls) == 2

    def test_a_backend_that_declines_leaves_the_name_alone(self, app_env):
        """Empty/garbage reply, timeout, claude-max — every failure path keeps
        the existing derived title. A name is never worth failing a turn."""
        client, chat = make_client(app_env, [model_says("hi")], title=None)
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            assert self._run(ws, "a question about nothing")["result"] == "hi"
        assert chat.title_calls  # it tried
        assert SessionLog._parse(app_env["state_dir"] / name).title is None


class TestFork:
    def test_fork_seeds_new_session_and_leaves_source_untouched(self, app_env):
        # /fork copies the whole conversation into a NEW session, switches
        # there, replays the prior transcript, and leaves the original intact.
        client, chat = make_client(
            app_env,
            [model_says("the answer is zebra"), model_says("still zebra")],
        )
        with client, connected(client) as (ws, hello, _):
            source = hello["session"]
            ws.send_json({"type": "task", "text": "remember the zebra"})
            recv_until(ws, "done")
            source_bytes = (app_env["state_dir"] / source).read_bytes()

            ws.send_json({"type": "fork"})
            forked = recv_until(ws, "hello")
            assert forked["session"] != source  # a genuinely new session
            replay = recv_until(ws, "replay")
            users = [e for e in replay["events"] if e["type"] == "user"]
            assert users and "zebra" in users[0]["text"]  # history seeded
            assert any(e["type"] == "done" for e in replay["events"])

            # The source file is byte-for-byte unchanged (read-only snapshot).
            assert (app_env["state_dir"] / source).read_bytes() == source_bytes

            # Both sessions are listed; the fork is current.
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            names = [s["name"] for s in listing["sessions"]]
            assert source in names and forked["session"] in names
            assert listing["current"] == forked["session"]

            # Continuing in the fork carries the seeded context to the model.
            ws.send_json({"type": "task", "text": "what animal?"})
            recv_until(ws, "done")
            assert "zebra" in json.dumps(chat.calls[-1]["messages"])

    def test_fork_from_here_truncates_to_that_answer(self, app_env):
        # A per-answer Fork (after=N) branches up to and including that answer,
        # dropping later turns — the "from here" case.
        client, _ = make_client(
            app_env,
            [model_says("first answer alpha"), model_says("second answer beta")],
        )
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "one"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "two"})
            recv_until(ws, "done")

            ws.send_json({"type": "fork", "after": 1})
            forked = recv_until(ws, "hello")
            assert forked["session"] != hello["session"]
            replay = recv_until(ws, "replay")
            users = [e["text"] for e in replay["events"] if e["type"] == "user"]
            dumped = json.dumps(replay["events"])
            assert users == ["one"]  # only the first turn carried over
            assert "alpha" in dumped and "beta" not in dumped

    def test_fork_after_out_of_range_errors(self, app_env):
        client, _ = make_client(app_env, [model_says("only answer")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "hi"})
            recv_until(ws, "done")
            ws.send_json({"type": "fork", "after": 5})
            error = recv_until(ws, "error")
            assert "out of range" in error["text"]

    def test_fork_empty_conversation_refused(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "fork"})
            # fork stamps control first (a `role` event) before refusing (#102).
            error = recv_until(ws, "error")
            assert "nothing to fork" in error["text"]

    def test_fork_while_busy_refused(self, app_env, tmp_path):
        # A task blocked on an approval keeps the session busy; forking then
        # would snapshot a half-finished turn, so it is refused.
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("done"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")  # now busy, blocked
            ws.send_json({"type": "fork"})
            error = recv_until(ws, "error")
            assert "can't fork while this session is working" in error["text"]
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")


class TestRetry:
    def test_retry_discards_previous_answer_everywhere(self, app_env):
        # #60: retry re-runs the prompt AND erases the previous attempt from the
        # model's context, the transcript, and the on-disk log — so nothing about
        # the regeneration is anchored to the discarded answer.
        client, chat = make_client(
            app_env,
            [model_says("first wrong answer"), model_says("second clean answer")],
        )
        with client, connected(client) as (ws, hello, _):
            session_name = hello["session"]
            ws.send_json({"type": "task", "text": "what is 2+2?"})
            done1 = recv_until(ws, "done")
            assert done1["result"] == "first wrong answer"

            ws.send_json({"type": "retry", "text": "what is 2+2?"})
            # The transcript is re-sent rolled back: the discarded answer is gone
            # but the user's prompt bubble stays.
            replay = recv_until(ws, "replay")
            dumped = json.dumps(replay["events"])
            assert "first wrong answer" not in dumped
            assert sum(1 for e in replay["events"] if e["type"] == "user") == 1
            assert not any(e["type"] == "done" for e in replay["events"])
            done2 = recv_until(ws, "done")
            assert done2["result"] == "second clean answer"

        # Model context on the rerun: the discarded answer must not be present,
        # and the prompt must be (the rerun really happened, from scratch).
        rerun_messages = json.dumps(chat.calls[1]["messages"])
        assert "first wrong answer" not in rerun_messages
        assert "what is 2+2?" in rerun_messages

        # Persistence: a cold reload must not resurrect the discarded answer.
        client2, _ = make_client(app_env, [])
        with client2, connected(client2) as (ws2, _, _):
            ws2.send_json({"type": "resume", "path": session_name})
            recv_until(ws2, "hello")
            replay = recv_until(ws2, "replay")
            dumped = json.dumps(replay["events"])
            assert "first wrong answer" not in dumped
            assert "second clean answer" in dumped
            assert sum(1 for e in replay["events"] if e["type"] == "user") == 1

    def test_retry_keeps_earlier_turns(self, app_env):
        # Only the LAST turn is rolled back; earlier answers stay in context.
        client, chat = make_client(
            app_env,
            [model_says("alpha"), model_says("bravo"), model_says("charlie")],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "first"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "second"})
            recv_until(ws, "done")

            ws.send_json({"type": "retry", "text": "second"})
            recv_until(ws, "replay")
            done = recv_until(ws, "done")
            assert done["result"] == "charlie"

        rerun_messages = json.dumps(chat.calls[2]["messages"])
        assert "alpha" in rerun_messages  # the first turn's answer survives
        assert "bravo" not in rerun_messages  # the retried turn's answer is gone
        assert "second" in rerun_messages

    def test_retry_while_busy_cancels_then_reruns_clean(self, app_env, tmp_path):
        # Retry fired while a turn is wedged on an approval: cancel first, then
        # roll back and rerun so the discarded (unexecuted) attempt leaves no
        # trace in the model's context.
        marker = tmp_path / "never"
        client, chat = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
                model_says("clean answer"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            recv_until(ws, "approval_request")  # now busy, blocked on the card
            ws.send_json({"type": "retry", "text": "run it"})
            replay = recv_until(ws, "replay")
            assert not marker.exists()  # the cancelled command never ran
            assert sum(1 for e in replay["events"] if e["type"] == "user") == 1
            done = recv_until(ws, "done")
            assert done["result"] == "clean answer"

        rerun_messages = json.dumps(chat.calls[1]["messages"])
        assert "run it" in rerun_messages
        assert str(marker) not in rerun_messages  # discarded tool call is not replayed


class TestModels:
    def test_model_list_ranked(self, app_env, monkeypatch):
        monkeypatch.setattr(
            server_module,
            "available_models",
            lambda agent, state_dir: [
                ("qwen3:8b", "local · 5 GB"),
                ("gemini", "cloud · default gemini-3-flash"),
            ],
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "models", "query": "gem"})
            listing = recv_until(ws, "model_list")
            assert listing["current"] == "fake"
            assert listing["models"][0]["name"] == "gemini"

    def test_set_model_swaps_backend_and_saves(self, app_env, monkeypatch):
        new_chat = FakeChat([])
        monkeypatch.setattr(
            server_module.backends,
            "make_chat",
            lambda spec: (new_chat, "gemini", "gemini-3-pro"),
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "set_model", "spec": "gemini:gemini-3-pro", "save": True}
            )
            changed = recv_until(ws, "model_changed")
            assert changed["model"] == "gemini:gemini-3-pro"
            assert changed["saved"] is True
            server = client.app.state.server
            assert server.active.agent.chat is new_chat
            assert server.active.agent.provider == "gemini"
            config = app_env["config_path"].read_text(encoding="utf-8")
            assert 'model = "gemini:gemini-3-pro"' in config

    def test_new_chat_inherits_current_model(self, app_env, monkeypatch):
        new_chat = FakeChat([])
        monkeypatch.setattr(
            server_module.backends,
            "make_chat",
            lambda spec: (new_chat, "gemini", "gemini-3-pro"),
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "set_model", "spec": "gemini:gemini-3-pro"})
            recv_until(ws, "model_changed")
            ws.send_json({"type": "new"})
            hello = recv_until(ws, "hello")
            assert hello["model"] == "gemini:gemini-3-pro"  # sticky, not reset

    def test_set_model_claude_max_needs_restart(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "set_model", "spec": "claude-max"})
            # set_model is an action → it stamps control first (a `role` event),
            # so skip to the error rather than reading the raw next frame (#102).
            error = recv_until(ws, "error")
            assert "restart" in error["text"]


class TestWorkspace:
    def test_cd_moves_cwd_and_reanchors_root(self, app_env, tmp_path):
        project = tmp_path / "other-project"
        project.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "cd", "path": str(project)})
            changed = recv_until(ws, "cwd_changed")
            assert changed["cwd"] == str(project)
            assert changed["roots"][0] == str(project)

    def test_add_dir_appends_root(self, app_env, tmp_path):
        extra = tmp_path / "extra"
        extra.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "add_dir", "path": str(extra)})
            changed = recv_until(ws, "cwd_changed")
            assert str(extra) in changed["roots"]

    def test_cd_bad_path_errors(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "cd", "path": "/definitely/not/here"})
            error = recv_until(ws, "error")
            assert error["text"].startswith("ERROR")


class TestFilesAutocomplete:
    def test_file_list_matches_tui_scoring(self, app_env, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "readme.md").write_text("x", encoding="utf-8")
        (tmp_path / "main.py").write_text("x", encoding="utf-8")
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "files", "query": "read"})
            listing = recv_until(ws, "file_list")
            assert listing["query"] == "read"
            assert "docs/readme.md" in listing["files"]
            assert "main.py" not in listing["files"]


class TestUpload:
    def test_upload_saves_and_lands_in_roots(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            response = client.post("/upload?name=notes.txt", content=b"hello upload")
            assert response.status_code == 200
            path = response.json()["path"]
            with open(path, "rb") as fh:
                assert fh.read() == b"hello upload"
            server = client.app.state.server
            assert server.uploads_dir.resolve() in [
                r for r in server.active.agent.roots
            ]

    def test_upload_rejects_bad_names(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            assert client.post("/upload?name=.hidden", content=b"x").status_code == 400
            assert client.post("/upload", content=b"x").status_code == 400
            # Path components are stripped, never traversed.
            response = client.post("/upload?name=../../evil.txt", content=b"x")
            assert response.status_code == 200
            assert response.json()["path"].endswith("uploads/evil.txt")

    def test_attached_image_goes_native_pdf_falls_back(self, app_env):
        client, chat = make_client(app_env, [model_says("I see it")])
        with client:
            image = client.post("/upload?name=photo.png", content=b"\x89PNG-fake").json()
            pdf = client.post("/upload?name=paper.pdf", content=b"%PDF-fake").json()
            with connected(client) as (ws, _, _):
                ws.send_json(
                    {
                        "type": "task",
                        "text": "what is this?",
                        "attachments": [image["path"], pdf["path"]],
                    }
                )
                user = recv_until(ws, "user")
                assert "you can see it" in user["text"]  # image went native
                assert f"[attached file: {pdf['path']}]" in user["text"]  # pdf fell back
                recv_until(ws, "done")
            sent = [m for m in chat.calls[0]["messages"] if m["role"] == "user"][-1]
            # test provider is "ollama": images native, pdf stays a path note
            assert sent.get("images") == [image["path"]]
            assert "documents" not in sent

    def test_attachment_outside_uploads_never_goes_native(self, app_env, tmp_path):
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"\x89PNG-private")
        client, chat = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "task", "text": "look", "attachments": [str(secret)]}
            )
            user = recv_until(ws, "user")
            assert f"[attached file: {secret}]" in user["text"]
            recv_until(ws, "done")
            sent = [m for m in chat.calls[0]["messages"] if m["role"] == "user"][-1]
            assert "images" not in sent  # nothing base64'd from outside uploads

    def test_upload_requires_token_when_set(self, app_env):
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.post("/upload?name=a.txt", content=b"x").status_code == 403
            assert (
                client.post("/upload?name=a.txt&token=s3cret", content=b"x").status_code
                == 200
            )


class TestFileEndpoint:
    """GET /file (issue #9): images the model generated render inline in the
    transcript — scoped to the active session's roots, like approval."""

    def test_serves_image_inside_roots(self, app_env, tmp_path):
        chart = tmp_path / "chart.png"
        chart.write_bytes(b"\x89PNG-fake-chart")
        client, _ = make_client(app_env, [])
        with client:
            response = client.get("/file", params={"path": str(chart)})
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.content == b"\x89PNG-fake-chart"

    def test_refuses_paths_outside_roots(self, app_env, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside") / "private.png"
        outside.write_bytes(b"\x89PNG-private")
        client, _ = make_client(app_env, [])
        with client:
            response = client.get("/file", params={"path": str(outside)})
            assert response.status_code == 403

    def test_symlink_escaping_roots_refused(self, app_env, tmp_path, tmp_path_factory):
        secret = tmp_path_factory.mktemp("elsewhere") / "secret.png"
        secret.write_bytes(b"\x89PNG-secret")
        link = tmp_path / "innocent.png"
        link.symlink_to(secret)
        client, _ = make_client(app_env, [])
        with client:
            # Resolved BEFORE the containment check, so the link's real
            # target is what gets scoped.
            response = client.get("/file", params={"path": str(link)})
            assert response.status_code == 403

    def test_only_image_types_served(self, app_env, tmp_path):
        notes = tmp_path / "notes.txt"
        notes.write_text("not an image", encoding="utf-8")
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/file", params={"path": str(notes)}).status_code == 415
            missing = tmp_path / "gone.png"
            assert client.get("/file", params={"path": str(missing)}).status_code == 404
            assert client.get("/file", params={"path": "rel.png"}).status_code == 400
            assert client.get("/file").status_code == 400

    def test_requires_token_when_set(self, app_env, tmp_path):
        chart = tmp_path / "chart.png"
        chart.write_bytes(b"\x89PNG")
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.get("/file", params={"path": str(chart)}).status_code == 403
            ok = client.get("/file", params={"path": str(chart), "token": "s3cret"})
            assert ok.status_code == 200


class TestImageRootsAgreement:
    """#188: /file and the PDF exporter must serve from the SAME set. They did
    not — the exporter trusted the session's scratch workspace and /file did
    not, so an image the model wrote where it is TOLD to write throwaway files
    printed fine in a PDF and 403'd in the chat, with nothing anywhere saying
    why. One method now answers for both."""

    def _session(self, app):
        return app.state.server.active

    def test_serves_from_the_scratch_workspace(self, app_env, tmp_path):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client):
                agent = client.app.state.server.active.agent
                shot = Path(agent.scratch_dir) / "shot.png"
                shot.write_bytes(b"\x89PNG-scratch")
                response = client.get("/file", params={"path": str(shot)})
                assert response.status_code == 200
                assert response.content == b"\x89PNG-scratch"

    def test_serves_from_the_media_store(self, app_env, tmp_path):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client):
                agent = client.app.state.server.active.agent
                agent.media_dir.mkdir(parents=True, exist_ok=True)
                pic = agent.media_dir / "abc123-phone.jpg"
                pic.write_bytes(b"\xff\xd8\xff-media")
                assert client.get("/file", params={"path": str(pic)}).status_code == 200

    def test_both_consumers_read_one_definition(self, app_env, tmp_path):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client):
                server = client.app.state.server
                agent = server.active.agent
                roots = server._image_roots()
                for own in (agent.media_dir, agent.scratch_dir):
                    assert any(Path(own).resolve().is_relative_to(r) for r in roots)

    def test_still_refuses_everything_else(self, app_env, tmp_path_factory):
        """Widening to aish's OWN directories must not widen to anyone else's."""
        outside = tmp_path_factory.mktemp("outside") / "private.png"
        outside.write_bytes(b"\x89PNG-private")
        client, _ = make_client(app_env, [])
        with client:
            with connected(client):
                assert client.get("/file", params={"path": str(outside)}).status_code == 403


class TestRenderErrorReports:
    """#188 layer 2: the browser is the only place that knows an image did not
    render, and it used to keep that to itself — the model's only feedback
    channel was the user typing "images don't show"."""

    def _log_steps(self, app_env):
        state = Path(app_env["state_dir"])
        steps = []
        for path in state.glob("session-*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("kind") == "trace":
                    steps.append(record["step"])
        return steps

    def test_a_live_failure_is_logged_and_told_to_the_model(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({
                    "type": "render_error", "what": "image", "live": True,
                    "items": ["https://images.example.com/phone.jpg"],
                })
                ws.send_json({"type": "jobs"})  # round-trip: the report was processed
                recv_until(ws, "job_list")
                agent = client.app.state.server.active.agent
        notes = [
            m for m in agent.messages
            if m.get("role") == "user" and "did not display" in (m.get("content") or "")
        ]
        assert len(notes) == 1
        assert "images.example.com/phone.jpg" in notes[0]["content"]
        assert "show_image" in notes[0]["content"]
        logged = [s for s in self._log_steps(app_env) if s.get("kind") == "render_error"]
        assert logged and logged[0]["items"] == ["https://images.example.com/phone.jpg"]

    def test_the_note_is_a_synthetic_annotation_not_a_transcript_turn(self, app_env):
        """It must not show as a blue user bubble on replay, and it must not
        become the chat's title (#171 classifies by text)."""
        from aish.session import synthetic_kind

        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({
                    "type": "render_error", "live": True, "items": ["/tmp/x.png"],
                })
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
                agent = client.app.state.server.active.agent
        note = [m for m in agent.messages if "did not display" in (m.get("content") or "")][0]
        assert synthetic_kind(note["content"]) == "note"

    def test_a_replayed_failure_is_logged_but_injects_nothing(self, app_env):
        """Opening an old chat whose pictures were evicted must not grow it a
        turn — that is an unread conversation talking to itself."""
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({
                    "type": "render_error", "live": False, "items": ["/gone/old.png"],
                })
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
                agent = client.app.state.server.active.agent
        assert not [m for m in agent.messages if "did not display" in (m.get("content") or "")]
        logged = [s for s in self._log_steps(app_env) if s.get("kind") == "render_error"]
        assert logged and logged[0]["items"] == ["/gone/old.png"]

    def test_the_report_never_widens_egress_provenance(self, app_env):
        """The src is a string the MODEL chose, echoed by the browser. Feeding it
        through the owner-authored path would let an injected image URL vouch for
        its own host on a later egress card (#178 P0-2)."""
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({
                    "type": "render_error", "live": True,
                    "items": ["https://attacker.example/x.png"],
                })
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
                agent = client.app.state.server.active.agent
        assert "attacker.example" not in agent._owner_hosts

    def test_bounded_and_control_stripped(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({
                    "type": "render_error", "live": True,
                    "items": [f"/a{i}.png" for i in range(50)] + ["x\n\rY" + "z" * 500, 7, ""],
                })
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
        logged = [s for s in self._log_steps(app_env) if s.get("kind") == "render_error"][0]
        assert len(logged["items"]) <= server_module.RENDER_ERROR_MAX
        for item in logged["items"]:
            assert len(item) <= server_module.RENDER_ERROR_SRC_CHARS
            assert "\n" not in item and "\r" not in item

    def test_a_junk_report_is_ignored(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                for message in (
                    {"type": "render_error"},
                    {"type": "render_error", "items": "not-a-list"},
                    {"type": "render_error", "items": []},
                    {"type": "render_error", "items": ["   "]},
                ):
                    ws.send_json(message)
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
        assert not [s for s in self._log_steps(app_env) if s.get("kind") == "render_error"]


class TestDirListing:
    def make_tree(self, tmp_path):
        base = tmp_path / "tree"
        for d in ("alpha", "beta/nested", "beta/.hidden", ".git/objects", "projects/aish"):
            (base / d).mkdir(parents=True)
        (base / "file.txt").write_text("not a dir", encoding="utf-8")
        return base

    def test_dirs_lists_folders_and_files(self, app_env, tmp_path):
        base = self.make_tree(tmp_path)
        client, _ = make_client(app_env, [])
        with client:
            body = client.get(f"/dirs?path={base}").json()
            assert body["path"] == str(base)
            # Folders list with items=None (no per-subfolder count — that extra
            # scandir could block in-kernel and freeze the server; #86). Noise
            # dirs like .git are filtered server-side (#87).
            assert body["dirs"] == [
                {"name": "alpha", "items": None},
                {"name": "beta", "items": None},
                {"name": "projects", "items": None},
            ]
            assert body["files"] == ["file.txt"]
            assert body["truncated"] is False

    def test_dirs_filters_noise_dirs(self, app_env, tmp_path):
        base = tmp_path / "proj"
        for d in ("src", "node_modules", ".git", "venv", "__pycache__"):
            (base / d).mkdir(parents=True)
        (base / ".DS_Store").write_text("", encoding="utf-8")
        (base / "main.py").write_text("", encoding="utf-8")
        client, _ = make_client(app_env, [])
        with client:
            body = client.get(f"/dirs?path={base}").json()
            assert [d["name"] for d in body["dirs"]] == ["src"]  # noise dirs gone
            assert body["files"] == ["main.py"]  # .DS_Store filtered

    def test_dirs_filters_glob_egg_info(self, app_env, tmp_path):
        # fnmatch globbing in the default ignore list (#87).
        base = tmp_path / "proj"
        for d in ("keep", "aish.egg-info"):
            (base / d).mkdir(parents=True)
        client, _ = make_client(app_env, [])
        with client:
            body = client.get(f"/dirs?path={base}").json()
            assert [d["name"] for d in body["dirs"]] == ["keep"]

    def test_dirs_honors_config_ignore_list(self, app_env, tmp_path):
        # A user-edited [directory_picker] ignore list is the source of truth:
        # names it lists are hidden, and defaults it omits are NOT (#87).
        app_env["config_path"].write_text(
            '[directory_picker]\nignore = ["secret", "*.bak"]\n', encoding="utf-8"
        )
        base = tmp_path / "proj"
        for d in ("src", "secret", "node_modules"):
            (base / d).mkdir(parents=True)
        (base / "old.bak").write_text("", encoding="utf-8")
        (base / "keep.txt").write_text("", encoding="utf-8")
        client, _ = make_client(app_env, [])
        with client:
            body = client.get(f"/dirs?path={base}").json()
            # "secret" hidden by config; "node_modules" now shown (not in the
            # user's list); "*.bak" file hidden by the glob.
            assert [d["name"] for d in body["dirs"]] == ["node_modules", "src"]
            assert body["files"] == ["keep.txt"]

    def test_dirs_listing_timeout_returns_504(self, app_env, tmp_path, monkeypatch):
        """A hung listing (blocking scandir/stat on a TCC-gated or networked
        path) is killed and returns 504 rather than freezing the server — the
        reason the listing runs out of process (#86)."""
        import aish.server as server_module

        monkeypatch.setattr(server_module.WebServer, "_DIRS_TIMEOUT_S", 0.3)
        monkeypatch.setattr(
            server_module.WebServer, "_DIRS_LIST_SCRIPT", "import time\ntime.sleep(30)\n"
        )
        base = self.make_tree(tmp_path)
        client, _ = make_client(app_env, [])
        with client:
            assert client.get(f"/dirs?path={base}").status_code == 504

    def test_dirs_requires_token_when_set(self, app_env, tmp_path):
        base = self.make_tree(tmp_path)
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.get(f"/dirs?path={base}").status_code == 403
            assert client.get(f"/dirs?path={base}&token=s3cret").status_code == 200

    def test_dirs_rejects_bad_paths(self, app_env, tmp_path):
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/dirs?path=relative/path").status_code == 400
            assert client.get(f"/dirs?path={tmp_path}/nope").status_code == 404
            assert client.get(f"/dirs?path={tmp_path}/tree/file.txt").status_code == 404


class TestTokenGate:
    def test_wrong_token_rejected_right_token_accepted(self, app_env):
        from starlette.websockets import WebSocketDisconnect

        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            # Accepted then closed with the app code — browsers only expose
            # close codes for accepted sockets, and the client needs 4403 to
            # show "wrong token" instead of looping on "reconnecting…".
            with client.websocket_connect("/ws?token=wrong") as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4403
            with connected(client, "/ws?token=s3cret") as (_ws, hello, _):
                assert hello["model"] == "fake"


class TestOriginGate:
    """#178 P1-2: WebSockets are exempt from the same-origin policy and a
    text/plain POST needs no preflight, so a browser Origin that isn't our own
    Host is rejected BEFORE the token is even considered. A missing Origin
    (curl, the launchd poller, native clients) passes — browsers always send
    one on the cross-origin requests that are the drive-by vector."""

    def test_cross_origin_ws_rejected_even_with_valid_token(self, app_env):
        from starlette.websockets import WebSocketDisconnect

        client, _ = make_client(app_env, [])
        with client:
            # TokenClient appends the VALID token — the origin alone rejects.
            with client.websocket_connect(
                "/ws", headers={"Origin": "https://evil.example"}
            ) as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4405

    def test_same_origin_ws_accepted(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            # TestClient's Host header is "testserver".
            with client.websocket_connect(
                "/ws", headers={"Origin": "http://testserver"}
            ) as ws:
                assert ws.receive_json()["type"] == "hello"

    def test_missing_origin_accepted(self, app_env):
        # curl / the poller / native clients send no Origin header at all.
        client, _ = make_client(app_env, [])
        with client, connected(client) as (_ws, hello, _):
            assert hello["type"] == "hello"

    def test_trigger_cross_origin_rejected(self, app_env):
        client, _ = make_client(app_env, [model_says("x")], token="secret")
        with client:
            r = client.post(
                "/trigger?token=secret",
                json={"prompt": "hi", "origin": "email"},
                headers={"Origin": "https://evil.example"},
            )
            assert r.status_code == 403

    def test_origin_matching_rules(self):
        from aish.server import origin_allowed

        # The reverse-proxy case that must keep working (aish.wenda.eu).
        assert origin_allowed("https://aish.wenda.eu", "aish.wenda.eu")
        assert origin_allowed("https://aish.wenda.eu:443", "aish.wenda.eu")
        assert origin_allowed("http://192.168.10.20:8787", "192.168.10.20:8787")
        assert origin_allowed(None, "aish.wenda.eu")
        assert origin_allowed("", "aish.wenda.eu")
        assert not origin_allowed("null", "aish.wenda.eu")
        assert not origin_allowed("https://evil.example", "aish.wenda.eu")
        assert not origin_allowed("https://aish.wenda.eu.evil.example", "aish.wenda.eu")
        assert not origin_allowed("file://x", "aish.wenda.eu")
        # A Host that fails the header-injection sanity check gates closed.
        assert not origin_allowed("https://aish.wenda.eu", "bad host\r\nX: y")


class TestUnconditionalToken:
    """#178 P1-2: there is no token-less mode. An app built without a token
    generates a random one at startup and every gated surface requires it."""

    def test_no_token_app_still_gates_everything(self, app_env):
        from starlette.websockets import WebSocketDisconnect

        chat = FakeChat([model_says("ok")])
        app = create_app("fake", client_chat=chat, **app_env)  # no token given
        generated = app.state.server.token
        assert generated  # generated, held in memory
        client = TestClient(app)  # deliberately NOT TokenClient: no auto-append
        with client:
            assert client.post("/upload?name=a.txt", content=b"x").status_code == 403
            assert client.get("/offline/index").status_code == 403
            assert client.get("/dirs?path=/tmp").status_code == 403
            assert client.post("/export/answer", content=b"# x").status_code == 403
            # /trigger: the old loopback fallback is gone — token or nothing.
            assert client.post("/trigger", json={"prompt": "x"}).status_code == 403
            with client.websocket_connect("/ws") as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4403
            # The generated token is the way in.
            with connected(client, f"/ws?token={generated}") as (_ws, hello, _):
                assert hello["type"] == "hello"
            r = client.post(
                f"/trigger?token={generated}", json={"prompt": "go", "origin": "email"}
            )
            assert r.status_code == 200


class TestConcurrentColdOpen:
    """#178 P1-5: two callers racing a cold open of the same name (restart
    recovery vs a reconnecting PWA) must share ONE Session — the duplicate
    used to overwrite the first in `sessions`, orphaning a live worker whose
    approval cards could never be answered."""

    def test_racing_cold_opens_share_one_session(self, app_env):
        client, _ = make_client(app_env, [])
        server = client.app.state.server
        # A session on disk but NOT in memory: write a log file directly.
        state_dir = app_env["state_dir"]
        log = SessionLog.new(state_dir)
        log.message({"role": "user", "content": "hello from disk"})
        log.close()
        name = log.path.name
        assert name not in server.sessions

        opens = {"n": 0}
        real_open = server.open_session

        def counted(*args, **kwargs):
            opens["n"] += 1
            return real_open(*args, **kwargs)

        server.open_session = counted

        async def race():
            return await asyncio.gather(
                server._open_by_name(name), server._open_by_name(name)
            )

        a, b = asyncio.run(race())
        assert a is not None
        assert a is b
        assert server.sessions[name] is a
        assert opens["n"] == 1  # one build, shared — not two racing ones

    def test_failed_open_does_not_poison_the_name(self, app_env):
        client, _ = make_client(app_env, [])
        server = client.app.state.server
        state_dir = app_env["state_dir"]
        log = SessionLog.new(state_dir)
        log.message({"role": "user", "content": "hi"})
        log.close()
        name = log.path.name

        real_open = server.open_session
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("disk hiccup")
            return real_open(*args, **kwargs)

        server.open_session = flaky

        async def open_twice():
            with pytest.raises(RuntimeError):
                await server._open_by_name(name)
            assert name not in server._opening  # entry removed on failure
            return await server._open_by_name(name)

        session = asyncio.run(open_twice())
        assert session is not None
        assert server.sessions[name] is session


class TestSkillsRefresh:
    def test_skill_added_after_boot_is_advertised(self, app_env, project_scope):
        """Issue #31: the skills index is rebuilt per task, not captured at
        create_app time — a skill created while the server runs reaches the
        model on the next task without a restart."""
        client, chat = make_client(app_env, [model_says("ok")])
        skills_dir = Path(app_env["cwd"]) / ".aish" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "late.md").write_text(
            "---\nname: late\ndescription: Use when testing hot reload\n---\nbody"
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "anything"})
            recv_until(ws, "done")
        system = chat.calls[0]["messages"][0]
        assert system["role"] == "system"
        assert "- late: Use when testing hot reload" in system["content"]


class TestLearnCommand:
    def test_learn_text_is_rewritten_to_prompt(self, app_env):
        client, chat = make_client(app_env, [model_says("saved nothing")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/learn"})
            assert recv_until(ws, "user")["text"] == "/learn"  # transcript keeps the typed form
            recv_until(ws, "done")
        sent_user = [m for m in chat.calls[0]["messages"] if m["role"] == "user"]
        assert "durable learnings" in sent_user[-1]["content"]

    def test_other_slash_text_goes_through_verbatim(self, app_env):
        client, chat = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/etc/hosts looks odd"})
            recv_until(ws, "done")
        sent_user = [m for m in chat.calls[0]["messages"] if m["role"] == "user"]
        assert sent_user[-1]["content"] == "/etc/hosts looks odd"

    def test_feedback_text_is_rewritten_to_flow_prompt(self, app_env):
        client, chat = make_client(app_env, [model_says("drafted")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback dark mode is broken"})
            assert recv_until(ws, "user")["text"] == "/feedback dark mode is broken"
            recv_until(ws, "done")
        sent_user = [m for m in chat.calls[0]["messages"] if m["role"] == "user"]
        assert "GitHub issue" in sent_user[-1]["content"]
        assert "dark mode is broken" in sent_user[-1]["content"]


class TestExportAssembly:
    """Issue #64: the pure markdown-assembly boundary — 'final answers only'
    is a structural rule, tested here without touching a PDF."""

    def test_session_answers_excludes_thinking_and_tool_steps(self):
        from aish.export import session_answers

        messages = [
            {"role": "user", "content": "do a thing"},
            # a working turn that narrated before calling a tool: it IS followed
            # by a tool result, so it is not a final answer.
            {"role": "assistant", "content": "let me check the files first"},
            {"role": "tool", "tool_name": "run_command", "content": "file1 file2"},
            # the real answer to the first question
            {"role": "assistant", "content": "There are two files."},
            {"role": "user", "content": "and now?"},
            {"role": "assistant", "content": ""},  # empty turn — dropped
            {"role": "assistant", "content": "All done — nothing else to do."},
        ]
        answers = session_answers(messages)
        assert answers == ["There are two files.", "All done — nothing else to do."]
        assert not any("check the files" in a for a in answers)  # working step gone
        assert not any("file1 file2" in a for a in answers)  # tool output gone

    def test_assemble_session_markdown_separates_answers(self):
        from aish.export import assemble_session_markdown

        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "answer one"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "answer two"},
        ]
        doc = assemble_session_markdown(messages, "T")
        assert "answer one" in doc and "answer two" in doc
        assert "---" in doc  # a horizontal rule separates them

    def test_render_answer_pdf_is_valid_pdf(self):
        from aish.export import render_answer_pdf

        data = render_answer_pdf("# Hi\n\nSome **markdown** — with an arrow →.", "t")
        assert data.startswith(b"%PDF")
        assert len(data) > 400

    def test_pdf_embeds_unicode_font_for_polish(self):
        # Regression: the PDF built-in fonts (Helvetica/Courier) have no Polish
        # glyphs and render them as black boxes. The bundled Source Sans 3 /
        # Source Code Pro fonts must be embedded so ą/ć/ę/ł/… actually draw.
        from aish.export import render_answer_pdf

        data = render_answer_pdf("Zażółć gęślą jaźń — → `ąęść`", "t")
        assert data.startswith(b"%PDF")
        assert b"SourceSans3" in data  # embedded body font
        assert b"SourceCodePro" in data  # embedded code font

    def test_export_strips_web_only_bits(self):
        # Quick-reply chips, the [no-chips] tag, and emoji variation selectors
        # are web-only / presentational — they must not reach the PDF markdown.
        from aish.export import _strip_web_only

        out = _strip_web_only(
            "Answer text.\n\n[Yes](aish-reply://Yes) [No](aish-reply://No)\n[no-chips]\n"
            "Heart ❤️ done."
        )
        assert "aish-reply" not in out
        assert "no-chips" not in out.lower()
        assert "️" not in out  # variation selector stripped
        assert "Answer text." in out and "Heart ❤ done." in out

    def test_export_wraps_emoji_and_embeds_emoji_font(self):
        # reportlab can't render colour emoji; the bundled Noto Emoji outline
        # font is embedded and emoji runs are wrapped to select it.
        from aish.export import _wrap_emoji, render_answer_pdf

        wrapped = _wrap_emoji("Ship it \U0001F680 now")
        assert 'font-family: aishEmoji' in wrapped and "\U0001F680" in wrapped
        # a symbol Source Sans already has is NOT rerouted to the emoji font
        assert _wrap_emoji("arrow → here") == "arrow → here"

        data = render_answer_pdf("Launch \U0001F680 and celebrate \U0001F389", "t")
        assert data.startswith(b"%PDF")
        assert b"NotoEmoji" in data

    def test_export_wraps_long_code_to_page(self):
        # A very long unbreakable line in a code block must not error and the
        # page CSS carries the CJK wrap that fits it to the page width.
        from aish.export import _PAGE_CSS, render_answer_pdf

        assert "-pdf-word-wrap: CJK" in _PAGE_CSS
        data = render_answer_pdf("```\n" + ("x" * 400) + "\n```\n", "t")
        assert data.startswith(b"%PDF")

    def test_safe_pdf_filename_slugs_and_defaults(self):
        from aish.export import safe_pdf_filename

        assert safe_pdf_filename("rename all/the photos!") == "rename-all-the-photos.pdf"
        assert safe_pdf_filename("") == "aish-export.pdf"
        assert safe_pdf_filename("   ", "fb") == "fb.pdf"

    def test_safe_pdf_filename_transliterates_non_ascii(self):
        # Non-ASCII letters must transliterate to ASCII, not be stripped to a
        # run of dashes. ł/Ł is the load-bearing case (it doesn't decompose
        # under NFKD), so it needs the explicit map.
        from aish.export import safe_pdf_filename

        name = safe_pdf_filename("Zażółć gęślą jaźń")
        data = name.removesuffix(".pdf")
        assert data.isascii()
        assert "----" not in data  # letters weren't stripped into dash runs
        assert "Zazolc" in data
        assert "jazn" in data
        assert name == "Zazolc-gesla-jazn.pdf"


def _png_bytes(width: int = 8, height: int = 8) -> bytes:
    """A tiny real PNG (Pillow is a hard dep of xhtml2pdf, so it is always
    present in the test environment)."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 90, 200)).save(buffer, "PNG")
    return buffer.getvalue()


class TestExportMedia:
    """Issue #133: pictures, maps, and video thumbnails embedded into the PDF.
    All network is faked by monkeypatching export.fetch_image — the tests
    assert on the HTML-rewrite boundary (_MediaEmbedder) plus one end-to-end
    PDF render per shape."""

    def _process(self, markdown_text, roots=()):
        import aish.export as export

        return export._markdown_to_html_fragment(
            markdown_text, export._MediaEmbedder(list(roots))
        )

    # ---- local images -----------------------------------------------------

    def test_local_image_inside_root_is_inlined(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(_png_bytes())
        html = self._process(f"![my shot]({tmp_path}/shot.png)", [tmp_path])
        assert "data:image/png;base64," in html
        assert 'alt="my shot"' in html

    def test_local_image_outside_roots_is_never_read(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        secret = tmp_path / "secret.png"
        secret.write_bytes(_png_bytes())
        html = self._process(f"![leak]({secret})", [root])
        assert "data:image" not in html
        assert "aish-link-card" in html  # captioned link card instead
        assert str(secret) in html

    def test_dotdot_traversal_is_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        secret = tmp_path / "secret.png"
        secret.write_bytes(_png_bytes())
        html = self._process(f"![leak]({root}/../secret.png)", [root])
        assert "data:image" not in html
        assert "aish-link-card" in html

    def test_symlink_escape_is_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        secret = tmp_path / "secret.png"
        secret.write_bytes(_png_bytes())
        link = root / "inside.png"
        link.symlink_to(secret)  # inside the root, but resolves outside
        html = self._process(f"![leak]({link})", [root])
        assert "data:image" not in html
        assert "aish-link-card" in html

    def test_relative_path_is_not_resolved(self, tmp_path):
        (tmp_path / "rel.png").write_bytes(_png_bytes())
        html = self._process("![r](rel.png)", [tmp_path])
        assert "data:image" not in html
        assert "aish-link-card" in html

    def test_non_image_local_file_becomes_card(self, tmp_path):
        (tmp_path / "notes.png").write_text("not an image at all")
        html = self._process(f"![n]({tmp_path}/notes.png)", [tmp_path])
        assert "data:image" not in html
        assert "aish-link-card" in html

    # ---- remote images ----------------------------------------------------

    def test_remote_image_is_fetched_and_inlined(self, monkeypatch):
        import aish.export as export

        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _png_bytes()

        monkeypatch.setattr(export, "fetch_image", fake_fetch)
        html = self._process("![pic](https://example.com/pic.png)")
        assert fetched == ["https://example.com/pic.png"]
        assert "data:image/png;base64," in html

    def test_remote_image_fetch_failure_falls_back_to_card(self, monkeypatch):
        import aish.export as export

        monkeypatch.setattr(export, "fetch_image", lambda url: None)
        html = self._process("![pic](https://example.com/gone.png)")
        assert "data:image" not in html
        assert "aish-link-card" in html
        assert "https://example.com/gone.png" in html

    def test_remote_fetch_budget_is_bounded(self, monkeypatch):
        import aish.export as export

        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _png_bytes()

        monkeypatch.setattr(export, "fetch_image", fake_fetch)
        links = "\n\n".join(
            f"![i{n}](https://example.com/{n}.png)"
            for n in range(export.MAX_REMOTE_FETCHES + 5)
        )
        self._process(links)
        assert len(fetched) == export.MAX_REMOTE_FETCHES

    # ---- YouTube thumbnails -----------------------------------------------

    def test_youtube_link_becomes_thumbnail_card(self, monkeypatch):
        import aish.export as export

        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _png_bytes()

        monkeypatch.setattr(export, "fetch_image", fake_fetch)
        html = self._process("Watch [Demo](https://youtu.be/dQw4w9WgXcQ) now")
        assert fetched == ["https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg"]
        assert "data:image/png;base64," in html
        assert "YouTube video" in html
        assert 'href="https://youtu.be/dQw4w9WgXcQ"' in html  # card links to the video

    def test_youtube_thumbnail_failure_falls_back_to_card(self, monkeypatch):
        import aish.export as export

        monkeypatch.setattr(export, "fetch_image", lambda url: None)
        html = self._process(
            "[Demo](https://www.youtube.com/watch?v=dQw4w9WgXcQ)"
        )
        assert "data:image" not in html
        assert "aish-link-card" in html
        assert "YouTube video" in html

    def test_plain_link_is_untouched(self, monkeypatch):
        import aish.export as export

        monkeypatch.setattr(
            export, "fetch_image", lambda url: pytest.fail("must not fetch")
        )
        html = self._process("[docs](https://example.com/docs)")
        assert '<a href="https://example.com/docs">docs</a>' in html

    # ---- Google Maps snapshots --------------------------------------------

    def test_map_link_without_api_key_is_a_link_card(self, monkeypatch):
        import aish.export as export

        monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
        monkeypatch.setattr(
            export, "fetch_image", lambda url: pytest.fail("must not fetch without a key")
        )
        html = self._process(
            "[Office](https://www.google.com/maps/search/?api=1&query=Central+Park)"
        )
        assert "aish-link-card" in html
        assert "map" in html

    def test_map_link_with_api_key_fetches_static_map(self, monkeypatch):
        import aish.export as export

        monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "K123")
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _png_bytes()

        monkeypatch.setattr(export, "fetch_image", fake_fetch)
        html = self._process(
            "[Office](https://www.google.com/maps/search/?api=1&query=Central+Park)"
        )
        assert len(fetched) == 1
        assert fetched[0].startswith("https://maps.googleapis.com/maps/api/staticmap?")
        assert "markers=Central+Park" in fetched[0]
        assert "key=K123" in fetched[0]
        assert "data:image/png;base64," in html

    def test_directions_link_maps_both_endpoints(self, monkeypatch):
        import aish.export as export

        monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "K123")
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _png_bytes()

        monkeypatch.setattr(export, "fetch_image", fake_fetch)
        self._process("[Route](https://maps.google.com/maps?saddr=Kraków&daddr=Warszawa)")
        assert len(fetched) == 1
        assert "label%3AA%7CKrak" in fetched[0] or "label:A" in fetched[0]

    # ---- end to end -------------------------------------------------------

    def test_render_answer_pdf_with_local_image(self, tmp_path):
        from aish.export import render_answer_pdf

        (tmp_path / "shot.png").write_bytes(_png_bytes(600, 200))
        markdown = f"# Report\n\n![shot]({tmp_path}/shot.png)\n"
        with_image = render_answer_pdf(markdown, "t", [tmp_path])
        without_scope = render_answer_pdf(markdown, "t", [])
        assert with_image.startswith(b"%PDF")
        assert without_scope.startswith(b"%PDF")
        assert len(with_image) > len(without_scope)  # the image bytes made it in

    def test_export_answer_endpoint_inlines_session_root_image(self, app_env):
        cwd = Path(app_env["cwd"])
        (cwd / "shot.png").write_bytes(_png_bytes())
        client, _ = make_client(app_env, [])
        with client:
            response = client.post(
                "/export/answer?title=pic",
                content=f"![shot]({cwd}/shot.png)".encode(),
            )
            assert response.status_code == 200
            assert response.content.startswith(b"%PDF")


class TestExportEndpoints:
    def test_export_answer_returns_pdf_attachment(self, app_env):
        """With no usable backend the title falls back to the answer's own lead
        — here its heading — and names the download."""
        client, _ = make_client(app_env, [])
        with client:
            response = client.post(
                "/export/answer",
                content="# Answer\n\nBody text — with unicode →.".encode(),
            )
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/pdf"
            assert 'attachment; filename="Answer.pdf"' in (
                response.headers["content-disposition"]
            )
            assert response.content.startswith(b"%PDF")

    def test_export_answer_title_is_written_by_the_answers_own_model(self, app_env):
        """The prompt names the request, not the document (#172): the session's
        OWN model titles its answer, and that title names the file."""
        client, chat = make_client(app_env, [], title="Bali eSIM data plans")
        with client:
            with connected(client) as (ws, hello, _):
                name = hello["session"]
            response = client.post(
                f"/export/answer?session={name}",
                content=b"You are correct. Airalo is a good choice on Bali.",
            )
            assert response.status_code == 200
            assert 'filename="Bali-eSIM-data-plans.pdf"' in (
                response.headers["content-disposition"]
            )
            # One extra call, tool-free and non-streaming, and it never touched
            # the conversation — a title must not become a turn in the log.
            titling = chat.title_calls[-1]
            assert not titling["tools"] and not titling.get("stream")
            assert len(titling["messages"]) == 1  # a bare prompt, not the conversation
            log = app_env["state_dir"] / name
            if log.exists():
                assert "Bali eSIM data plans" not in log.read_text(encoding="utf-8")

    def test_export_answer_title_falls_back_when_the_model_rambles(self, app_env):
        """A chatty or failed reply must not name the file — the deterministic
        lead takes over."""
        rambling = model_says("Sure! " + "Here is some prose about your document. " * 6)
        client, _ = make_client(app_env, [rambling])
        with client:
            with connected(client) as (ws, hello, _):
                name = hello["session"]
            response = client.post(
                f"/export/answer?session={name}",
                content=b"# Quarterly report\n\nBody.",
            )
            assert 'filename="Quarterly-report.pdf"' in (
                response.headers["content-disposition"]
            )

    def test_export_answer_rejects_empty_body(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            assert client.post("/export/answer", content=b"").status_code == 400

    def test_export_session_returns_final_answers_only(self, app_env):
        # A task that calls a tool (auto-approved `ls`) then answers: the log
        # then holds a tool step whose text must NOT reach the exported PDF.
        responses = [
            model_says(tool_calls=[tool_call("run_command", command="ls")]),
            model_says("The exported final answer."),
        ]
        client, _ = make_client(app_env, responses)
        with client:
            with connected(client) as (ws, hello, _):
                name = hello["session"]
                ws.send_json({"type": "task", "text": "list and answer"})
                recv_until(ws, "done")
            response = client.get(f"/export/session?session={name}")
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/pdf"
            assert response.content.startswith(b"%PDF")
            assert "attachment" in response.headers["content-disposition"]

            # The pure assembly over the same log proves the tool step is gone.
            from aish.export import session_answers
            from aish.session import SessionLog

            messages = SessionLog.load_messages(app_env["state_dir"] / name)
            answers = session_answers(messages)
            assert answers == ["The exported final answer."]

    def test_export_session_unknown_name_404(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/export/session?session=nope").status_code == 404
            assert (
                client.get("/export/session?session=../../etc/passwd").status_code == 404
            )

    def test_export_endpoints_require_token_when_set(self, app_env):
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.post("/export/answer", content=b"x").status_code == 403
            assert (
                client.post("/export/answer?token=s3cret", content=b"# x").status_code
                == 200
            )
            assert client.get("/export/session?session=x").status_code == 403


ISSUE_BLOCK = (
    "Here is your draft:\n\n"
    "```aish-issue\n"
    "title: Dark mode toggle is broken\n"
    "---\n"
    "The toggle does nothing on tap.\n\n"
    "### Steps\n"
    "- open settings\n"
    "- tap the toggle\n\n"
    "label: bug\n"
    "```\n"
)


class TestIssueBlockParsing:
    """The aish-issue block is the single source of truth (#110): parsed once in
    the backend, mirrored in app.js. Title/body must come out exactly."""

    def test_parses_title_and_body_with_separator(self):
        issue, cleaned = server_module.parse_issue_block(ISSUE_BLOCK)
        assert issue == {
            "title": "Dark mode toggle is broken",
            "body": "The toggle does nothing on tap.\n\n"
            "### Steps\n- open settings\n- tap the toggle\n\nlabel: bug",
        }
        # The raw fence is stripped from the stored answer (b): replay/export
        # never show the fenced source, only the surrounding prose survives.
        assert "```aish-issue" not in cleaned
        assert cleaned == "Here is your draft:"

    def test_optional_separator_absent_body_starts_line_two(self):
        text = "```aish-issue\ntitle: A title\nBody line one.\nBody line two.\n```"
        issue, _ = server_module.parse_issue_block(text)
        assert issue == {"title": "A title", "body": "Body line one.\nBody line two."}

    def test_body_may_itself_contain_a_separator_line(self):
        # A --- deeper in the body is a real horizontal rule, not the optional
        # leading separator, so it must be preserved verbatim.
        text = (
            "```aish-issue\n"
            "title: Has a rule\n"
            "---\n"
            "Intro paragraph.\n"
            "---\n"
            "After the rule.\n"
            "```"
        )
        issue, _ = server_module.parse_issue_block(text)
        assert issue["title"] == "Has a rule"
        assert issue["body"] == "Intro paragraph.\n---\nAfter the rule."

    def test_no_block_returns_none_and_unchanged_text(self):
        text = "Just a normal answer with no issue block."
        assert server_module.parse_issue_block(text) == (None, text)


class TestIssueCreation:
    """Backend-owned creation (#110): confirm files the pre-reviewed draft as a
    user-direct action — no model, no approval gate, repo pinned, safe argv."""

    @staticmethod
    def _fake_run_command(captured):
        def fake(command, **kwargs):
            captured.append(command)
            on_line = kwargs.get("on_line")
            if on_line:
                on_line("https://github.com/epnasis/aish/issues/999")
            return "https://github.com/epnasis/aish/issues/999\n[exit code: 0]"

        return fake

    def test_text_feedback_stashes_block_and_strips_from_answer(self, app_env):
        # A text-only /feedback draft: the block is stashed and stripped from the
        # streamed/stored answer (b); no gh call happens during the task.
        client, chat = make_client(app_env, [model_says(ISSUE_BLOCK)])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback dark mode is broken"})
            done = recv_until(ws, "done")
            assert "```aish-issue" not in done["result"]
            # The model was told to EMIT a block, not to run gh issue create.
            user_prompt = next(
                m["content"] for m in reversed(chat.calls[0]["messages"])
                if m["role"] == "user"
            )
            assert "aish-issue" in user_prompt
            assert "Do NOT run `gh issue create`" in user_prompt

    def test_create_issue_files_reviewed_draft_via_user_direct_path(
        self, app_env, monkeypatch
    ):
        captured: list[str] = []
        monkeypatch.setattr(
            "aish.tools.run_command", self._fake_run_command(captured)
        )
        client, _ = make_client(app_env, [model_says(ISSUE_BLOCK)])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback dark mode is broken"})
            recv_until(ws, "done")  # draft stashed
            ws.send_json({"type": "create_issue"})
            # The user-direct path streams into a terminal block — no approval.
            start = recv_until(ws, "command_start")
            assert start.get("user") is True
            recv_until(ws, "done")
        assert len(captured) == 1
        argv = shlex.split(captured[0])
        # Repo hard-pinned; title/body are the EXACT reviewed text, safely quoted.
        assert argv[:5] == ["gh", "issue", "create", "--repo", "epnasis/aish"]
        assert argv[argv.index("--title") + 1] == "Dark mode toggle is broken"
        body = argv[argv.index("--body") + 1]
        assert body.startswith("The toggle does nothing on tap.")
        assert "label: bug" in body

    def test_create_issue_confirmation_carries_clickable_link(
        self, app_env, monkeypatch
    ):
        # gh prints the new issue's URL to stdout; the confirmation surfaces it as
        # a clickable markdown link, not plain terminal text (#110 follow-up).
        captured: list[str] = []
        monkeypatch.setattr(
            "aish.tools.run_command", self._fake_run_command(captured)
        )
        client, _ = make_client(app_env, [model_says(ISSUE_BLOCK)])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback dark mode is broken"})
            recv_until(ws, "done")
            ws.send_json({"type": "create_issue"})
            done = recv_until(ws, "done")  # the filing confirmation
        assert "[#999](https://github.com/epnasis/aish/issues/999)" in done["result"]

    def test_create_issue_clears_pending_so_a_retap_cannot_double_file(
        self, app_env, monkeypatch
    ):
        captured: list[str] = []
        monkeypatch.setattr(
            "aish.tools.run_command", self._fake_run_command(captured)
        )
        client, _ = make_client(app_env, [model_says(ISSUE_BLOCK)])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback broken"})
            recv_until(ws, "done")
            ws.send_json({"type": "create_issue"})
            recv_until(ws, "done")
            # Second tap: the draft was consumed, so it errors instead of re-filing.
            ws.send_json({"type": "create_issue"})
            err = recv_until(ws, "error")
            assert "no issue draft" in err["text"]
        assert len(captured) == 1  # filed exactly once

    def test_create_issue_without_a_draft_errors_gracefully(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "create_issue"})
            err = recv_until(ws, "error")
            assert "no issue draft" in err["text"]

    def test_feedback_with_attachments_keeps_gated_model_flow(self, app_env):
        # Attachments → the classic model-driven flow: the model is told to run
        # gh issue create itself (approval-gated) with the asset workflow. No
        # backend block flow, so no pending_issue is stashed.
        client, chat = make_client(app_env, [model_says("Here is the draft…")])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {
                    "type": "task",
                    "text": "/feedback see the log",
                    "attachments": ["/tmp/does-not-exist.log"],
                }
            )
            recv_until(ws, "done")
            user_prompt = next(
                m["content"] for m in reversed(chat.calls[0]["messages"])
                if m["role"] == "user"
            )
            assert "gh issue create" in user_prompt
            assert "asset workflow" in user_prompt
            assert "aish-issue" not in user_prompt
            # #130: consent — the draft lists the assets with per-file exclude
            # chips before anything is uploaded to the public release.
            assert "aish-reply://Exclude <name> from the issue" in user_prompt
            assert "PUBLIC GitHub release" in user_prompt
            # No draft was stashed, so a create_issue tap errors.
            ws.send_json({"type": "create_issue"})
            err = recv_until(ws, "error")
            assert "no issue draft" in err["text"]


class TestFeedbackAttachmentSwitch:
    """#130: attachments in the /feedback adjust loop. A text-only draft being
    adjusted auto-switches to the classic upload flow when attachments arrive,
    and uploads are consented — the draft lists the assets with per-file
    exclude chips before anything lands on the public release."""

    @staticmethod
    def _last_user_prompt(chat, call: int) -> str:
        return next(
            m["content"] for m in reversed(chat.calls[call]["messages"])
            if m["role"] == "user"
        )

    def test_adjust_turn_attachment_switches_block_to_classic(self, app_env):
        client, chat = make_client(
            app_env, [model_says(ISSUE_BLOCK), model_says("Updated draft…")]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback dark mode is broken"})
            recv_until(ws, "done")  # block draft stashed
            ws.send_json(
                {
                    "type": "task",
                    "text": "here is a screenshot",
                    "attachments": ["/tmp/shot.png"],
                }
            )
            # The switch note is model-only: the user echo stays clean.
            echo = recv_until(ws, "user")
            assert "SWITCH" not in echo["text"]
            assert "[attached file: /tmp/shot.png]" in echo["text"]
            recv_until(ws, "done")
            prompt = self._last_user_prompt(chat, 1)
            # The attachment was detected and the model re-anchored on the
            # classic flow, with the consent listing (confirm/deselect).
            assert "[attached file: /tmp/shot.png]" in prompt
            assert "SWITCH to the classic flow" in prompt
            assert "aish-reply://Create the issue" in prompt
            assert "aish-reply://Exclude <name> from the issue" in prompt
            assert "PUBLIC GitHub release" in prompt
            # The stale block draft was withdrawn: a Create tap can't file it.
            ws.send_json({"type": "create_issue"})
            err = recv_until(ws, "error")
            assert "no issue draft" in err["text"]

    def test_exclude_reply_passes_through_without_a_second_switch(self, app_env):
        # The deselect chip's reply is an ordinary adjust turn: no attachments
        # and the flow already switched, so the model receives it verbatim (no
        # duplicate switch note) and re-drafts without the excluded file.
        client, chat = make_client(
            app_env,
            [
                model_says(ISSUE_BLOCK),
                model_says("Draft listing shot.png"),
                model_says("Draft without shot.png"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback broken"})
            recv_until(ws, "done")
            ws.send_json(
                {"type": "task", "text": "screenshot", "attachments": ["/tmp/shot.png"]}
            )
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "Exclude shot.png from the issue"})
            recv_until(ws, "done")
            assert self._last_user_prompt(chat, 2) == "Exclude shot.png from the issue"

    def test_textonly_adjust_turn_stays_on_block_flow(self, app_env):
        # No attachments → no switch: the refinement loop keeps the fast
        # backend-owned block flow and the re-emitted draft stays filable.
        client, chat = make_client(
            app_env, [model_says(ISSUE_BLOCK), model_says(ISSUE_BLOCK)]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback broken"})
            recv_until(ws, "done")
            ws.send_json(
                {"type": "task", "text": "I'd like to change the draft: mention iOS"}
            )
            recv_until(ws, "done")
            prompt = self._last_user_prompt(chat, 1)
            assert "SWITCH" not in prompt
            assert prompt == "I'd like to change the draft: mention iOS"

    def test_attachment_outside_feedback_does_not_switch(self, app_env):
        # An attachment in a session with no feedback in progress is a plain
        # attachment — never a flow switch.
        client, chat = make_client(app_env, [model_says("looked at it")])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "task", "text": "what is this?", "attachments": ["/tmp/x.log"]}
            )
            recv_until(ws, "done")
            assert "SWITCH" not in self._last_user_prompt(chat, 0)

    def test_filing_the_issue_closes_the_switch_window(self, app_env, monkeypatch):
        # Once the draft is filed the adjust loop is over: a later attachment
        # in the same session must not drag the model back into feedback.
        monkeypatch.setattr(
            "aish.tools.run_command", TestIssueCreation._fake_run_command([])
        )
        client, chat = make_client(
            app_env, [model_says(ISSUE_BLOCK), model_says("looked at it")]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback broken"})
            recv_until(ws, "done")
            ws.send_json({"type": "create_issue"})
            recv_until(ws, "done")
            ws.send_json(
                {"type": "task", "text": "unrelated", "attachments": ["/tmp/x.log"]}
            )
            recv_until(ws, "done")
            assert "SWITCH" not in self._last_user_prompt(chat, 1)


class TestToolApproval:
    """Mutating plugin tools reuse the command card verbatim over the WS."""

    @pytest.fixture(autouse=True)
    def _opt_in(self, project_scope):
        """Tools are planted in the session cwd's .aish — explicit opt-in (#178 P0-1)."""

    def _write_tool(self, cwd, name, marker, mutating="yes"):
        import stat
        from pathlib import Path

        tdir = Path(cwd) / ".aish" / "tools" / name
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "TOOL.md").write_text(
            f"---\nname: {name}\ndescription: writer tool\nexec: ./run.sh\n"
            f'mutating: {mutating}\nschema: {{"text": {{"type": "string"}}}}\n---\nb\n'
        )
        p = tdir / "run.sh"
        p.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def responses(self):
        return [
            model_says(tool_calls=[tool_call("writer", text="hi")]),
            model_says("finished"),
        ]

    def test_tool_approve_runs(self, app_env, tmp_path):
        marker = tmp_path / "toolran"
        self._write_tool(app_env["cwd"], "writer", marker)
        client, _ = make_client(app_env, self.responses())
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run tool"})
            request = recv_until(ws, "approval_request")
            assert request["kind"] == "tool"
            assert request["tool"] == "writer"
            assert request["args"] == {"text": "hi"}
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")
            assert marker.exists()

    def test_tool_deny_never_runs(self, app_env, tmp_path):
        marker = tmp_path / "toolpwned"
        self._write_tool(app_env["cwd"], "writer", marker)
        client, chat = make_client(app_env, self.responses())
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run tool"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")
            assert not marker.exists()
            assert tool_results(chat)[-1]["content"] == DENIED_RESULT

    def _write_preview_tool(self, cwd, name="pv"):
        import stat
        from pathlib import Path

        tdir = Path(cwd) / ".aish" / "tools" / name
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "TOOL.md").write_text(
            f"---\nname: {name}\ndescription: d\nexec: ./run.sh\nmutating: yes\npreview: yes\n"
            f'schema: {{"id": {{"type": "string"}}}}\n---\nb\n'
        )
        p = tdir / "run.sh"
        p.write_text(
            "#!/bin/sh\n"
            'if [ -n "$AISH_TOOL_PREVIEW" ]; then echo "would delete 42"; exit 0; fi\ncat\n'
        )
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def test_preview_included_in_request(self, app_env, tmp_path):
        self._write_preview_tool(app_env["cwd"])
        client, _ = make_client(
            app_env,
            [model_says(tool_calls=[tool_call("pv", id="42")]), model_says("done")],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run tool"})
            request = recv_until(ws, "approval_request")
            assert request["kind"] == "tool"
            assert request["preview"] == "would delete 42"
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")


def _alive(pid: int) -> bool:
    """True while `pid` is a live (non-reaped) process. A fully-reaped pid raises
    ProcessLookupError on signal 0; a zombie the parent hasn't wait()ed yet still
    answers, so this is only used against processes the server owns and reaps."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestGlobalConsole:
    """Issue #148 follow-up: ONE persistent GLOBAL console (the "Quake console")
    for the whole server, not per-session. The USER drives it (ungated, like `!`);
    the model has no path to its input. It survives viewer-leave, session close,
    and disconnects. I/O is private unless explicitly shared. Tests inject a
    trivial python echo loop as the console command, so no tmux/shell is needed."""

    _CHILD = (
        "import sys\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write('GOT:' + line)\n"
        "    sys.stdout.flush()\n"
    )

    def _cmd(self):
        import sys as _sys

        return f"{shlex.quote(_sys.executable)} -c {shlex.quote(self._CHILD)}"

    def _client(self, app_env, responses=None, command=None):
        return make_client(
            app_env, responses or [], console_command=command or self._cmd()
        )

    def _drain_out(self, ws, wanted):
        seen = ""
        for _ in range(200):
            event = ws.receive_json()
            if event["type"] == "console_out":
                seen += event["data"]
                if wanted in seen:
                    return seen
        raise AssertionError(f"never saw {wanted!r} in console_out")

    def test_console_is_global_on_the_server_not_the_session(self, app_env):
        client, _ = self._client(app_env)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            server = client.app.state.server
            # It lives on the WebServer, never on a Session.
            assert server.console is not None
            assert not hasattr(server.active, "pty")
            ws.send_json({"type": "console_kill"})

    def test_console_in_and_out_over_the_socket(self, app_env):
        client, _ = self._client(app_env)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            # Bytes IN over the socket → the child → bytes OUT over the socket.
            ws.send_json({"type": "console_in", "data": "hello\n"})
            assert "GOT:hello" in self._drain_out(ws, "GOT:hello")
            # EOF ends the child; the exit is reported over the socket.
            ws.send_json({"type": "console_in", "data": "\x04"})
            assert recv_until(ws, "console_exit")["code"] == 0
        # The server forgot the console once it exited (no dangling handle).
        assert client.app.state.server.console is None

    def test_console_survives_viewer_leave_and_session_close(self, app_env):
        client, _ = self._client(app_env, command="cat")  # blocks on stdin, stays alive
        # Keep the TestClient (and thus the server) open ACROSS the ws disconnect —
        # only the inner `connected` context closes the socket. Exiting `with
        # client` would run lifespan shutdown, which is what detaches the console.
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({"type": "console_open"})
                recv_until(ws, "console_started")
                server = client.app.state.server
                assert server.console is not None
                pid = server.console.pid
                # Hiding the overlay must NOT kill it.
                ws.send_json({"type": "console_close"})
                assert server.console is not None
            # Socket fully closed (viewer left) → console STILL running (global).
            assert server.console is not None
            assert _alive(pid), "console was killed on disconnect — it must persist"
            # Closing a session must not touch the global console either.
            server.active.close()
            assert server.console is not None
            assert _alive(pid)
            server.console.kill()  # cleanup

    def test_console_output_broadcasts_to_every_viewer(self, app_env):
        client, _ = self._client(app_env)
        with client:
            with connected(client) as (ws1, _, _), connected(client) as (ws2, _, _):
                ws1.send_json({"type": "console_open"})
                recv_until(ws1, "console_started")
                ws2.send_json({"type": "console_open"})  # attaches to the SAME console
                recv_until(ws2, "console_started")
                assert len(client.app.state.server.console_viewers) == 2
                # One PTY: input from either socket, output to BOTH.
                ws1.send_json({"type": "console_in", "data": "hi\n"})
                assert "GOT:hi" in self._drain_out(ws1, "GOT:hi")
                assert "GOT:hi" in self._drain_out(ws2, "GOT:hi")
                ws1.send_json({"type": "console_kill"})

    def test_reopen_attaches_to_the_existing_console(self, app_env):
        client, _ = self._client(app_env, command="cat")
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            first = client.app.state.server.console
            # Hide, then reopen: a NEW console_started, but the SAME PtySession.
            ws.send_json({"type": "console_close"})
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            assert client.app.state.server.console is first
            ws.send_json({"type": "console_kill"})

    def test_console_kill_terminates_and_reports_exit(self, app_env):
        client, _ = self._client(app_env, command="cat")
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            server = client.app.state.server
            pid = server.console.pid
            ws.send_json({"type": "console_kill"})
            recv_until(ws, "console_exit")
            assert server.console is None
            deadline = time.time() + 5
            while time.time() < deadline and _alive(pid):
                time.sleep(0.02)
            assert not _alive(pid), "killed console left a zombie"

    def test_console_output_is_never_recorded_in_the_transcript(self, app_env):
        # Console I/O is private: it must not enter any session transcript (and
        # thus never a cold replay or the model's context) unless explicitly shared.
        client, _ = self._client(app_env)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            ws.send_json({"type": "console_in", "data": "secret\n"})
            self._drain_out(ws, "GOT:secret")
            transcript = client.app.state.server.active.bridge.transcript
            assert not any(e["type"].startswith("console_") for e in transcript)
            assert not any("secret" in json.dumps(e) for e in transcript)
            ws.send_json({"type": "console_kill"})

    def test_share_injects_selection_into_the_viewed_chats_context(self, app_env):
        client, _ = self._client(app_env, command="cat")
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            ws.send_json({"type": "console_share", "text": "device code ABC-123"})
            recv_until(ws, "console_shared")
            ws.send_json({"type": "console_kill"})
        # The shared text is now a user turn the model will see — via the same
        # user-message path as `!`, not the console stream.
        messages = client.app.state.server.active.agent.messages
        shared = [m for m in messages if "device code ABC-123" in str(m.get("content", ""))]
        assert shared
        # …and it stays out of the transcript on replay too: live it produced no
        # bubble (just the toast), so a reload must not invent one (#171).
        assert synthetic_kind(shared[0]["content"]) == "note"

    def test_model_run_command_never_touches_the_console(self, app_env, tmp_path):
        # A normal model task that runs a command must not create or feed the
        # console: the model has no console path. server.console stays None.
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("done"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "make a file"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")
            assert client.app.state.server.console is None


class _FakeBridge:
    """Minimal Bridge stand-in for unit-testing the approvers: records emitted
    events and canned-answers asks. `ask` should never be reached for an
    auto-approved (safe) triggered mutation — the test asserts that."""

    def __init__(self, answer=None):
        self.events: list = []
        self.asked: list = []
        self._answer = answer or {"action": "approve"}

    def emit(self, event, record=True):
        self.events.append(event)

    def ask(self, request):
        request.setdefault("id", "uid-1")
        self.asked.append(request)
        return self._answer


class _FakeLog:
    def __init__(self):
        self.records: list = []

    def command(self, command, decision):
        self.records.append((command, decision))


class TestTriggeredCapabilityPolicy:
    """The #160 draft-and-hold policy, unit-tested on approve_tool directly."""

    def _approver(self, origin, answer=None):
        bridge, log = _FakeBridge(answer), _FakeLog()
        approvers = server_module.make_web_approvers(
            bridge, log, Path("/x/allow"), Path("/x/deny"),
            ask_all=False, get_scope=lambda: (".", []),
            trust_dir=lambda p: "", get_origin=lambda: origin,
        )
        return approvers[3], bridge, log  # approve_tool

    def test_safe_tool_auto_runs_in_triggered_session(self):
        approve_tool, bridge, log = self._approver("email")
        result = approve_tool("gmail_label", {"message_id": "m1", "add": "Receipts"})
        assert result is True
        assert bridge.asked == []  # no card — auto-run, no human needed
        assert log.records and log.records[0][1] == "auto (email)"

    def test_draft_recipients_are_checked_too(self):
        # #178 P0-3: a draft addressed to a third party is a staged
        # exfiltration one tap from sending — it holds like a live send
        # (still draftable, just through the card). Owner-addressed drafts
        # keep their autonomy.
        approve_tool, bridge, _ = self._approver("email")
        assert approve_tool(
            "gmail_send", {"to": "pawel@wenda.eu", "body": "hi", "draft": True}
        ) is True
        assert bridge.asked == []
        approve_tool2, bridge2, _ = self._approver("email")
        approve_tool2("gmail_send", {"to": "x@y.z", "body": "hi", "draft": True})
        assert len(bridge2.asked) == 1 and bridge2.asked[0]["tool"] == "gmail_send"
        # A live (non-draft) send to a third party falls through to the card.
        approve_tool3, bridge3, _ = self._approver("email")
        approve_tool3("gmail_send", {"to": "x@y.z", "body": "hi"})
        assert len(bridge3.asked) == 1 and bridge3.asked[0]["tool"] == "gmail_send"

    def test_unsafe_tool_holds_in_triggered_session(self):
        approve_tool, bridge, _ = self._approver("email")
        approve_tool("gmail_trash", {"message_id": "m1"})
        assert len(bridge.asked) == 1  # trash always prompts, even when triggered

    def test_user_session_always_prompts_even_for_safe_tools(self):
        # The policy is scoped to NON-user origins; a human-driven session gates
        # every mutation as before (no silent auto-run).
        approve_tool, bridge, _ = self._approver("user")
        approve_tool("gmail_label", {"message_id": "m1", "add": "Receipts"})
        assert len(bridge.asked) == 1

    def test_triggered_safe_helper(self):
        assert server_module._triggered_safe("gmail_label", {})
        # A draft is no longer auto-safe on the draft flag alone (#178 P0-3):
        # its recipients must be verifiably the owner, like a live send.
        assert not server_module._triggered_safe("gmail_send", {"draft": True})
        assert server_module._triggered_safe(
            "gmail_send", {"draft": True, "to": "pawel@wenda.eu"}
        )
        assert not server_module._triggered_safe(
            "gmail_send", {"draft": True, "to": "x@evil.com"}
        )
        assert not server_module._triggered_safe("gmail_send", {})
        assert not server_module._triggered_safe("gmail_trash", {"message_id": "m"})

    def test_owner_scoped_send_auto_runs(self):
        # A live send to the owner needs no approval (recipient-scoped autonomy).
        approve_tool, bridge, _ = self._approver("email")
        assert approve_tool("gmail_send",
                            {"to": "pawel@wenda.eu", "body": "here you go"}) is True
        assert bridge.asked == []

    def test_send_to_third_party_still_holds(self):
        approve_tool, bridge, _ = self._approver("email")
        approve_tool("gmail_send", {"to": "stranger@evil.com", "body": "x"})
        assert len(bridge.asked) == 1

    def test_mixed_recipients_hold(self):
        # Owner + a third party = NOT all-owner, so it holds.
        approve_tool, bridge, _ = self._approver("email")
        approve_tool("gmail_send",
                     {"to": "pawel@wenda.eu, stranger@evil.com", "body": "x"})
        assert len(bridge.asked) == 1

    def test_all_recipients_owner_helper(self):
        f = server_module._all_recipients_owner
        assert f({"to": "pawel@wenda.eu"})
        assert f({"to": "Pawel <pawel@wenda.email>"})  # display-name form
        assert f({"to": "pawel@wenda.eu", "cc": "pawel@wenda.email"})
        assert not f({"to": "pawel@wenda.eu", "bcc": "x@other.com"})
        assert not f({"reply_to_msg_id": "abc"})  # reply recipient unverifiable
        # A reply is unsafe EVEN with an owner `to` — it also hits the original
        # (possibly third-party) sender.
        assert not f({"reply_to_msg_id": "abc", "to": "pawel@wenda.eu"})
        assert not f({})

    def test_adversarial_recipients_never_pass_as_owner(self):
        """#178 P0-3: the old regex FOUND the owner address inside adversarial
        fields instead of validating what the field routes to. Every one of
        these must hold for approval, not auto-send."""
        f = server_module._all_recipients_owner
        # Valid RFC 5322 quoted local-part — routes to evil.com, reads as owner.
        assert not f({"to": '"pawel@wenda.eu"@evil.com'})
        assert not f({"to": "pawel@wenda.eu@evil.com"})  # multi-@, malformed
        assert not f({"to": "pawel@wenda.eu, attacker@evil.com"})
        assert not f({"to": ["pawel@wenda.eu", "attacker@evil.com"]})
        # Owner address as DISPLAY NAME on an attacker address.
        assert not f({"to": "pawel@wenda.eu <attacker@evil.com>"})
        assert not f({"to": '"pawel@wenda.eu" <attacker@evil.com>'})
        # Owner address in an RFC comment beside an attacker address.
        assert not f({"to": "attacker@evil.com (pawel@wenda.eu)"})
        assert not f({"to": "<pawel@wenda.eu> attacker@evil.com"})  # residue
        assert not f({"to": "pawel@wenda.eu; attacker@evil.com"})
        assert not f({"to": ""})
        assert not f({"to": "not-an-address"})
        # An owner-only field in every legitimate spelling still passes.
        assert f({"to": " pawel@wenda.eu "})
        assert f({"to": "PAWEL@WENDA.EU"})  # case-insensitive owner match
        assert f({"to": '"Wenda, Pawel" <pawel@wenda.eu>'})  # comma in quotes
        assert f({"to": ["pawel@wenda.eu", "pawel@wenda.email"]})  # list form

    def test_parse_recipients_rejects_residue_and_quoting(self):
        p = server_module._parse_recipients
        assert p("a@b.co, c@d.co") == ["a@b.co", "c@d.co"]
        assert p("Some One <a@b.co>") == ["a@b.co"]
        assert p('"pawel@wenda.eu"@evil.com') is None  # quoted local-part
        assert p("a@b.co@c.co") is None  # multi-@ local-part / malformed
        assert p("") is None
        assert p("a@b.co extra-junk") is None  # residue escapes the parse


class TestTriggerEndpoint:
    """The loopback/token-gated /trigger ingress and origin plumbing (#160)."""

    def test_trigger_spawns_origin_tagged_session_and_runs(self, app_env):
        client, _ = make_client(app_env, [model_says("triaged")], token="secret")
        with client:
            r = client.post("/trigger?token=secret",
                            json={"prompt": "new mail arrived", "origin": "email",
                                  "meta": {"msg": "abc"}, "title": "Email: booking"})
            assert r.status_code == 200
            name = r.json()["session"]
            assert r.json()["origin"] == "email"
            # Drive the spawned session to completion, then confirm provenance.
            with connected(client, f"/ws?token=secret&session={name}") as (ws, _, _):
                recv_until(ws, "done")
                ws.send_json({"type": "sessions"})
                lst = recv_until(ws, "session_list")
            row = next(s for s in lst["sessions"] if s["name"] == name)
            assert row["origin"] == "email"
            session = client.app.state.server.sessions[name]
            assert session.origin == "email"
            assert session.trigger_meta == {"msg": "abc"}

    def test_trigger_prompt_is_a_system_turn_live_and_on_replay(self, app_env):
        # A schedule/email/webhook prompt is not the owner typing (#171), and
        # what he types into that chat afterwards IS. Live and cold must agree.
        client, _ = make_client(app_env, [model_says("triaged"), model_says("replied")],
                                token="secret")
        with client:
            r = client.post("/trigger?token=secret",
                            json={"prompt": "new mail from the bank", "origin": "email"})
            name = r.json()["session"]
            with connected(client, f"/ws?token=secret&session={name}") as (ws, _, _):
                recv_until(ws, "done")
                ws.send_json({"type": "task", "text": "reply to it"})
                recv_until(ws, "done")
            server = client.app.state.server
            live = [e for e in server.sessions[name].bridge.transcript if e["type"] == "user"]
            path = server.state_dir / name

        assert [(e["text"], e.get("synthetic")) for e in live] == [
            ("new mail from the bank", "trigger"),
            ("reply to it", None),
        ]
        cold = [e for e in SessionLog.reconstruct_events(path) if e["type"] == "user"]
        assert [(e["text"], e.get("synthetic")) for e in cold] == [
            (e["text"], e.get("synthetic")) for e in live
        ]

    def test_pager_pages_carry_origin(self, app_env):
        # The swipe pager pages within one lane (Recent vs Automated), so every
        # page needs its origin — including cold ones read off disk.
        client, _ = make_client(app_env, [model_says("triaged"), model_says("ok")],
                                token="secret")
        with client, connected(client, "/ws?token=secret") as (ws, hello, _):
            mine = hello["session"]
            ws.send_json({"type": "task", "text": "my own chat"})
            recv_until(ws, "done")
            r = client.post("/trigger?token=secret",
                            json={"prompt": "new mail", "origin": "email"})
            auto = r.json()["session"]
            with connected(client, f"/ws?token=secret&session={auto}") as (auto_ws, _, _):
                recv_until(auto_ws, "done")
            ws.send_json({"type": "resume", "path": mine})
            hello = recv_until(ws, "hello")
            origins = {p["name"]: p["origin"] for p in hello["pager"]}
            assert origins[mine] == "user"
            assert origins[auto] == "email"

    def test_cold_open_does_not_rewrite_provenance(self, app_env):
        """Reopening a triggered session must leave its log alone. The origin
        record is already on disk (open_session just parsed it back), and
        re-writing it appended a record — plus the pending model one it flushed
        — on every open, bumping the mtime that orders every recency list."""
        client, _ = make_client(app_env, [model_says("triaged")], token="secret")
        with client:
            r = client.post("/trigger?token=secret",
                            json={"prompt": "new mail", "origin": "email"})
            name = r.json()["session"]
            with connected(client, f"/ws?token=secret&session={name}") as (ws, _, _):
                recv_until(ws, "done")
            server = client.app.state.server
            path = server.state_dir / name
            before, mtime = path.read_bytes(), path.stat().st_mtime_ns

            session, _history = server.open_session(path)  # the cold-open path

            assert session.origin == "email"  # provenance survives the round trip
            assert path.read_bytes() == before  # …without one byte written back
            assert path.stat().st_mtime_ns == mtime

    def test_trigger_rejects_bad_token(self, app_env):
        client, _ = make_client(app_env, [model_says("x")], token="secret")
        with client:
            r = client.post("/trigger?token=nope", json={"prompt": "hi"})
            assert r.status_code == 403

    def test_trigger_rejects_user_origin(self, app_env):
        client, _ = make_client(app_env, [model_says("x")], token="secret")
        with client:
            r = client.post("/trigger?token=secret",
                            json={"prompt": "hi", "origin": "user"})
            assert r.status_code == 400

    def test_trigger_requires_prompt(self, app_env):
        client, _ = make_client(app_env, [model_says("x")], token="secret")
        with client:
            r = client.post("/trigger?token=secret", json={"origin": "email"})
            assert r.status_code == 400

    def test_trigger_does_not_move_the_default_session(self, app_env):
        # #178 P1-6: an overnight trigger must not become the landing spot for
        # the next bare connect (nor eviction-immune via default status).
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        with client:
            server = client.app.state.server
            before = server._default
            r = client.post("/trigger?token=secret",
                            json={"prompt": "go", "origin": "email"})
            assert r.status_code == 200
            name = r.json()["session"]
            assert server._default is before
            assert server._default.name != name
            # Let the background task finish so shutdown is clean.
            with connected(client, f"/ws?token=secret&session={name}") as (ws, _, _):
                recv_until(ws, "done")
            assert server._default is before


class TestTriggerHardening:
    """Idempotency, per-origin rate limit, and concurrency cap on /trigger
    (#178 P1-10) — every refusal happens BEFORE a session is created."""

    def test_dedup_key_reuses_the_session(self, app_env):
        client, _ = make_client(app_env, [model_says("one")], token="secret")
        with client:
            server = client.app.state.server
            before = set(server.sessions)
            body = {"prompt": "mail", "origin": "email",
                    "meta": {"dedup_key": "gmail-m1"}}
            r1 = client.post("/trigger?token=secret", json=body)
            assert r1.status_code == 200
            name = r1.json()["session"]
            drain_done(client, name, token="secret")
            # The retry storm case: the same delivery POSTed again answers 200
            # with the SAME session and opens nothing new.
            r2 = client.post("/trigger?token=secret", json=body)
            assert r2.status_code == 200
            assert r2.json() == {"session": name, "origin": "email", "deduped": True}
            assert set(server.sessions) - before == {name}

    def test_trigger_model_override_runs_the_session_on_it(self, app_env, monkeypatch):
        # #186: a privacy-scoped trigger names its own model; the session runs
        # on it and records it, independent of the server default.
        override = FakeChat([model_says("done locally")])
        monkeypatch.setattr(
            server_module.backends, "make_chat",
            lambda spec: (override, "ollama", spec),
        )
        client, _ = make_client(app_env, [], token="secret")
        with client:
            r = client.post("/trigger?token=secret", json={
                "prompt": "curate", "origin": "schedule", "model": "qwen3:8b",
            })
            assert r.status_code == 200
            name = r.json()["session"]
            drain_done(client, name, token="secret")
            agent = client.app.state.server.sessions[name].agent
            assert agent.model == "qwen3:8b"
            assert override.calls  # the override chat, not the server default

    def test_trigger_model_override_fails_closed(self, app_env, monkeypatch):
        # An unbuildable override must refuse — silently running the prompt on
        # the (possibly cloud) default is the leak the override prevents. And
        # the refusal happens before any session log exists.
        def broken(spec):
            raise server_module.backends.BackendError("no such model")

        monkeypatch.setattr(server_module.backends, "make_chat", broken)
        client, _ = make_client(app_env, [], token="secret")
        with client:
            server = client.app.state.server
            logs_before = set(Path(app_env["state_dir"]).glob("session-*.jsonl"))
            before = set(server.sessions)
            r = client.post("/trigger?token=secret", json={
                "prompt": "curate", "origin": "schedule", "model": "nope:missing",
            })
            assert r.status_code == 503
            assert "model unavailable" in r.json()["error"]
            assert set(server.sessions) == before
            assert set(Path(app_env["state_dir"]).glob("session-*.jsonl")) == logs_before

    def test_no_dedup_key_fires_every_post(self, app_env):
        # Backward compatibility: clients that send no key keep old behavior.
        client, _ = make_client(app_env, [model_says("a"), model_says("b")],
                                token="secret")
        with client:
            names = set()
            for _ in range(2):
                r = client.post("/trigger?token=secret",
                                json={"prompt": "go", "origin": "email"})
                assert r.status_code == 200
                assert "deduped" not in r.json()
                names.add(r.json()["session"])
            assert len(names) == 2
            for name in names:
                drain_done(client, name, token="secret")

    def test_dedup_key_expires(self, app_env, monkeypatch):
        monkeypatch.setattr(server_module, "TRIGGER_DEDUP_TTL", -1)  # everything stale
        client, _ = make_client(app_env, [model_says("a"), model_says("b")],
                                token="secret")
        with client:
            body = {"prompt": "go", "origin": "email", "meta": {"dedup_key": "k"}}
            names = set()
            for _ in range(2):
                r = client.post("/trigger?token=secret", json=body)
                assert r.status_code == 200
                names.add(r.json()["session"])
                drain_done(client, r.json()["session"], token="secret")
            assert len(names) == 2  # an expired key no longer dedupes

    def test_rate_limit_429_before_any_session(self, app_env):
        client, _ = make_client(app_env, [model_says("a"), model_says("b")],
                                token="secret", trigger_rate_capacity=2,
                                trigger_rate_refill_s=3600)
        with client:
            server = client.app.state.server
            names = []
            for _ in range(2):
                r = client.post("/trigger?token=secret",
                                json={"prompt": "go", "origin": "webhook"})
                assert r.status_code == 200
                names.append(r.json()["session"])
            before = set(server.sessions)
            r = client.post("/trigger?token=secret",
                            json={"prompt": "go", "origin": "webhook"})
            assert r.status_code == 429
            assert r.headers["Retry-After"]
            assert "rate limit" in r.json()["error"]
            assert set(server.sessions) == before  # refused BEFORE creation
            for name in names:
                drain_done(client, name, token="secret")

    def test_rate_limit_is_per_origin(self, app_env):
        client, _ = make_client(app_env, [model_says("a"), model_says("b")],
                                token="secret", trigger_rate_capacity=1,
                                trigger_rate_refill_s=3600)
        with client:
            r1 = client.post("/trigger?token=secret",
                             json={"prompt": "go", "origin": "email"})
            assert r1.status_code == 200
            assert client.post("/trigger?token=secret",
                               json={"prompt": "go", "origin": "email"}).status_code == 429
            # A different origin has its own bucket.
            r2 = client.post("/trigger?token=secret",
                             json={"prompt": "go", "origin": "schedule"})
            assert r2.status_code == 200
            for r in (r1, r2):
                drain_done(client, r.json()["session"], token="secret")

    def test_concurrency_cap_429_while_triggered_sessions_run(self, app_env):
        release = threading.Event()

        class BlockingChat:
            """Holds the triggered task mid-run so the session stays busy."""

            def __call__(self, **kwargs):
                if _is_title_call(kwargs):
                    return model_says("")
                release.wait(timeout=10)
                response = model_says("done")
                return iter([response]) if kwargs.get("stream") else response

        app = create_app("fake", client_chat=BlockingChat(), **app_env,
                         token="secret", max_concurrent_triggered=1)
        client = TokenClient(app, auto_token="secret")
        try:
            with client:
                r1 = client.post("/trigger?token=secret",
                                 json={"prompt": "go", "origin": "email"})
                assert r1.status_code == 200  # busy is set before the POST returns
                # Different origin, so the rate limiter is not what refuses it.
                r2 = client.post("/trigger?token=secret",
                                 json={"prompt": "go", "origin": "webhook"})
                assert r2.status_code == 429
                assert r2.headers["Retry-After"]
                assert "concurrent" in r2.json()["error"]
                release.set()
                name = r1.json()["session"]
                drain_done(client, name, token="secret")
                # busy clears in _finish_turn, AFTER the done we just saw —
                # wait for it so the next POST reads the settled count.
                server = client.app.state.server
                for _ in range(200):
                    if not server.sessions[name].busy:
                        break
                    time.sleep(0.01)
                # With the first one finished the cap admits the next.
                r3 = client.post("/trigger?token=secret",
                                 json={"prompt": "go", "origin": "webhook"})
                assert r3.status_code == 200
                drain_done(client, r3.json()["session"], token="secret")
        finally:
            release.set()


class TestWorkerPool:
    """Agent workers run on the DEDICATED executor (#178 Gate 3): a parked
    approval holds a worker thread indefinitely, and on the shared default
    executor a few of those starved the very _show/to_thread calls a client
    needs to attach and answer them — livelock, restart-only recovery."""

    def test_run_task_executes_on_a_worker_pool_thread(self, app_env):
        seen: list[str] = []

        class RecordingChat:
            def __call__(self, **kwargs):
                if _is_title_call(kwargs):
                    return model_says("")
                seen.append(threading.current_thread().name)
                response = model_says("hi")
                return iter([response]) if kwargs.get("stream") else response

        app = create_app("fake", client_chat=RecordingChat(), **app_env,
                         token=TEST_TOKEN)
        client = TokenClient(app, auto_token=TEST_TOKEN)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "hello"})
            recv_until(ws, "done")
        assert seen and all(name.startswith("aish-worker") for name in seen)

    def test_saturated_worker_pool_does_not_block_attach(self, app_env):
        # With EVERY worker thread parked (as held approvals would), a viewer
        # can still connect and replay — the short ops must never share the
        # pool, or nobody could attach to answer what parked it.
        client, _ = make_client(app_env, [model_says("ok")])
        release = threading.Event()
        try:
            with client:
                server = client.app.state.server
                for _ in range(server_module.WORKER_POOL_SIZE):
                    server.worker_pool.submit(release.wait, 30)
                with connected(client) as (_ws, hello, replay):
                    assert hello["session"]
                    assert replay["events"] == []
                release.set()
        finally:
            release.set()

    def test_shutdown_does_not_wait_on_parked_workers(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        release = threading.Event()
        with client:
            server = client.app.state.server
            server.worker_pool.submit(release.wait, 30)
        # Reaching here means lifespan shutdown returned while the fake parked
        # worker was still waiting (wait=False + cancel_futures).
        release.set()


class TestOriginPersistence:
    """origin survives a cold reopen from the log (#160)."""

    def test_origin_recorded_and_reparsed(self, app_env, tmp_path):
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        with client:
            r = client.post("/trigger?token=secret",
                            json={"prompt": "go", "origin": "schedule"})
            name = r.json()["session"]
            with connected(client, f"/ws?token=secret&session={name}") as (ws, _, _):
                recv_until(ws, "done")
        # Re-parse straight off disk: the origin record round-trips.
        path = Path(app_env["state_dir"]) / name
        origin = SessionLog._parse(path).origin
        assert origin == "schedule"
        # And a fresh, untagged session parses as the default "user".
        assert SessionLog._info_from(path, [{"role": "user", "content": "x"}]).origin == "user"


class TestHoldNotification:
    """Pushover notification when an unattended triggered session holds (#163).
    The notify_hold closure is reachable as bridge.on_wait, so its gating is
    tested directly — no need to deadlock a real blocking approval."""

    def _held_event(self):
        return {"type": "approval_request", "kind": "tool",
                "tool": "gmail_trash", "args": {"message_id": "m1"}, "id": "u1"}

    def _spawn_email_session(self, client, monkeypatch, calls):
        monkeypatch.setattr(server_module.notify, "configured", lambda: True)
        monkeypatch.setattr(server_module.notify, "pushover",
                            lambda *a, **k: calls.append((a, k)) or True)
        r = client.post("/trigger?token=secret",
                        json={"prompt": "go", "origin": "email", "title": "Email: hi"})
        return client.app.state.server.sessions[r.json()["session"]]

    def test_triggered_hold_with_no_viewer_notifies(self, app_env, monkeypatch):
        monkeypatch.setenv("AISH_PUBLIC_URL", "https://aish.test")
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        calls: list = []
        with client:
            session = self._spawn_email_session(client, monkeypatch, calls)
            session.bridge.on_wait(self._held_event(), False)  # no viewers
        assert len(calls) == 1
        (title, body), kw = calls[0]
        assert "approval" in title.lower() and "Email: hi" in title
        assert "gmail_trash" in body
        assert kw["url"].endswith(f"/?session={session.name}")
        assert kw["url"].startswith("https://aish.test")
        # Normal priority: a hold must not bypass quiet hours (see notify_hold).
        assert kw["priority"] == 0

    def test_triggered_hold_with_a_viewer_stays_silent(self, app_env, monkeypatch):
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        calls: list = []
        with client:
            session = self._spawn_email_session(client, monkeypatch, calls)
            session.bridge.on_wait(self._held_event(), True)  # someone is watching
        assert calls == []

    def test_user_session_never_notifies_on_hold(self, app_env, monkeypatch):
        monkeypatch.setattr(server_module.notify, "configured", lambda: True)
        calls: list = []
        monkeypatch.setattr(server_module.notify, "pushover",
                            lambda *a, **k: calls.append(1))
        client, _ = make_client(app_env, [model_says("hi")])
        with client:
            default = client.app.state.server._default
            assert default.origin == "user"
            default.bridge.on_wait(self._held_event(), False)
        assert calls == []

    def test_describe_hold_shapes(self):
        d = server_module._describe_hold
        assert "gmail_trash" in d({"kind": "tool", "tool": "gmail_trash", "args": {}})
        assert d({"kind": "tool", "tool": "t", "preview": "delete X"}).endswith("delete X")
        assert d({"kind": "command", "command": "rm x"}).startswith("run:")
        assert d({"kind": "write", "verb": "edit", "target": "/a"}) == "edit /a"


class TestDoneNotification:
    """Pushover notification when a TRIGGERED session finishes (#163), and its
    viewer gating: a session someone is watching must stay silent, or every
    message typed into an automated chat pushes a phone notification for work
    happening on screen. Called directly — the coroutine is the whole unit."""

    def _server(self, app_env, monkeypatch, calls):
        monkeypatch.setattr(server_module.notify, "configured", lambda: True)
        monkeypatch.setattr(server_module.notify, "pushover",
                            lambda *a, **k: calls.append((a, k)) or True)
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        return client.app.state.server

    def _session(self, origin="email", viewers=()):
        return SimpleNamespace(origin=origin, viewers=set(viewers),
                               custom_title="Email: hi", name="session-x.jsonl")

    def test_triggered_done_with_no_viewer_notifies(self, app_env, monkeypatch):
        calls: list = []
        server = self._server(app_env, monkeypatch, calls)
        server.public_url = "https://aish.test"
        asyncio.run(server._notify_done(self._session(), "sent the reply"))
        assert len(calls) == 1
        (title, body), kw = calls[0]
        assert "finished" in title.lower() and "Email: hi" in title
        assert body == "sent the reply"
        assert kw["url"] == "https://aish.test/?session=session-x.jsonl"
        assert kw["priority"] == 0

    def test_suite_never_reaches_the_real_notifier(self, app_env, monkeypatch):
        # Regression: the restart-recovery tests run an email-origin session to
        # completion with no viewer, so _notify_done called the REAL
        # notify.pushover — which reads the developer's live Keychain and
        # pushed to their actual phone on every `pytest` run. The conftest
        # kill switch must make that structurally impossible, with nothing
        # here stubbed.
        sent: list = []
        monkeypatch.setattr(notify_module.urllib.request, "urlopen",
                            lambda *a, **k: sent.append(1))
        monkeypatch.setattr(notify_module.secrets, "get", lambda name: "real-cred")
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        server = client.app.state.server
        server.public_url = "https://aish.test"
        assert notify_module.configured() is False
        asyncio.run(server._notify_done(self._session(), "sent the reply"))
        assert sent == []  # no POST, real creds present or not

    def test_triggered_done_with_a_viewer_stays_silent(self, app_env, monkeypatch):
        calls: list = []
        server = self._server(app_env, monkeypatch, calls)
        session = self._session(viewers=[object()])
        asyncio.run(server._notify_done(session, "sent the reply"))
        assert calls == []

    def test_user_session_never_notifies_on_done(self, app_env, monkeypatch):
        calls: list = []
        server = self._server(app_env, monkeypatch, calls)
        asyncio.run(server._notify_done(self._session(origin="user"), "hi"))
        assert calls == []


class TestBridgeOnWait:
    """The Bridge.ask hook that powers hold notifications fires before blocking
    and reports whether anyone is viewing."""

    def test_on_wait_fires_with_viewer_flag_and_does_not_block(self):
        seen: list = []
        bridge = server_module.Bridge(lambda: None)

        def hook(event, has_viewers):
            seen.append((event["kind"], has_viewers))
            bridge.answer(event["id"], {"action": "deny"})  # unblock immediately

        bridge.on_wait = hook
        result = bridge.ask({"type": "approval_request", "kind": "tool", "tool": "x"})
        assert result == {"action": "deny"}
        assert seen == [("tool", False)]  # empty viewers → False

    def test_on_wait_error_never_breaks_the_gate(self):
        bridge = server_module.Bridge(lambda: None)

        def boom(event, has_viewers):
            bridge.answer(event["id"], {"action": "approve"})
            raise RuntimeError("notify blew up")

        bridge.on_wait = boom
        # The gate still returns the answer despite the hook raising.
        assert bridge.ask({"type": "approval_request", "kind": "tool", "tool": "x"}) \
            == {"action": "approve"}


class TestRestartResume:
    """Restart recovery (#164): a task killed mid-run by a server restart is
    picked up again at the next startup. Without this an interrupted automated
    session — an email trigger the poller has already marked processed — simply
    never happens, and nothing anywhere would ever notice."""

    @staticmethod
    def _interrupted_log(app_env, prompt="answer the mail", *, with_history=True,
                         attempts=1, origin="email"):
        """A session log in the state the OS leaves behind when the process is
        killed mid-task: a task_start with no task_end."""
        state_dir = Path(app_env["state_dir"])
        log = SessionLog(state_dir / "session-20260101-120000-000000.jsonl")
        log.model("fake")
        if origin != "user":
            log.origin(origin)
        for _ in range(attempts):
            log.task_start(prompt)
            if with_history:
                log.message({"role": "user", "content": prompt})
        log.close()
        return log.path

    @staticmethod
    def _wait(predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _resumed_prompt(self, chat):
        """The text of the resumed turn as the model saw it: the last user
        message of the first model call the resumed task made."""
        for message in reversed(chat.calls[0]["messages"]):
            if message.get("role") == "user":
                return message.get("content", "")
        return ""

    def test_interrupted_task_resumes_at_startup(self, app_env):
        path = self._interrupted_log(app_env)
        client, chat = make_client(app_env, [model_says("sent the reply")])
        with client:
            server = client.app.state.server
            assert self._wait(lambda: path.name in server.sessions), "never resumed"
            session = server.sessions[path.name]
            assert self._wait(lambda: not session.busy), "resumed task never finished"
        # Continues the conversation instead of re-issuing the prompt, so work
        # already done (a sent email) is not repeated.
        assert "[automatic resume]" in self._resumed_prompt(chat)
        assert session.origin == "email"  # provenance survives the resume
        # The completed run closed its bracket: a second restart won't redo it.
        assert SessionLog.pending_task(path) is None

    def test_resume_names_the_step_that_was_in_flight(self, app_env):
        # The one genuinely unknown part of an interrupted run: a step that
        # started and never reported back. The model is told to verify THOSE
        # rather than re-run the task.
        state_dir = Path(app_env["state_dir"])
        log = SessionLog(state_dir / "session-20260101-120000-000000.jsonl")
        log.model("fake")
        log.task_start("answer the mail")
        log.message({"role": "user", "content": "answer the mail"})
        log.step({"kind": "tool_start", "name": "gmail_search", "summary": "id:abc"})
        log.step({"kind": "tool", "name": "gmail_search", "ok": True})
        log.message({"role": "tool", "tool_name": "gmail_search", "content": "the mail"})
        log.step({"kind": "tool_start", "name": "gmail_send", "summary": "to pawel"})
        log.close()  # killed with the send in flight
        client, chat = make_client(app_env, [model_says("verified, already sent")])
        with client:
            server = client.app.state.server
            assert self._wait(lambda: log.path.name in server.sessions)
            assert self._wait(lambda: not server.sessions[log.path.name].busy)
        prompt = self._resumed_prompt(chat)
        assert "gmail_send: to pawel" in prompt
        assert "gmail_search" not in prompt  # it reported back; nothing to verify

    def test_resume_keeps_the_interrupted_output_verbatim(self, app_env):
        # The point of resuming: prior results arrive whole, so the model
        # continues instead of recomputing them.
        state_dir = Path(app_env["state_dir"])
        log = SessionLog(state_dir / "session-20260101-120000-000000.jsonl")
        log.model("fake")
        log.task_start("summarize the page")
        log.message({"role": "user", "content": "summarize the page"})
        log.message({"role": "tool", "tool_name": "read_url", "content": "PAGE" * 2000})
        log.close()
        client, chat = make_client(app_env, [model_says("summarized")])
        with client:
            server = client.app.state.server
            assert self._wait(lambda: log.path.name in server.sessions)
            assert self._wait(lambda: not server.sessions[log.path.name].busy)
        sent = [m for m in chat.calls[0]["messages"] if m.get("role") == "tool"]
        assert sent and len(sent[0]["content"]) == 4 * 2000  # untrimmed

    def test_resume_note_renders_as_a_system_turn_live_and_on_replay(self, app_env):
        # #171: the resume note is aish talking to itself, so it must never look
        # like something the user typed — and it must look the same after a
        # reload, which is why the live event and the cold replay are asserted
        # together. The re-issued original prompt above it stays a real bubble.
        path = self._interrupted_log(app_env, "answer the mail", origin="user")
        client, _chat = make_client(app_env, [model_says("already sent")])
        with client:
            server = client.app.state.server
            assert self._wait(lambda: path.name in server.sessions)
            session = server.sessions[path.name]
            assert self._wait(lambda: not session.busy)
            live = [e for e in session.bridge.transcript if e["type"] == "user"]

        assert [e.get("synthetic") for e in live] == ["resume"]  # the resumed turn
        cold = [e for e in SessionLog.reconstruct_events(path) if e["type"] == "user"]
        assert [(e["text"], e.get("synthetic")) for e in cold] == [
            ("answer the mail", None),  # what the user really asked for
            (live[0]["text"], "resume"),  # …and the same synthetic turn, framed alike
        ]

    def test_resume_reissues_prompt_when_nothing_was_logged(self, app_env):
        # Killed before the user message itself was logged: the history holds
        # nothing to continue from, so the recorded prompt is re-issued.
        path = self._interrupted_log(app_env, "read msg 42", with_history=False)
        client, chat = make_client(app_env, [model_says("read it")])
        with client:
            server = client.app.state.server
            assert self._wait(lambda: path.name in server.sessions)
            assert self._wait(lambda: not server.sessions[path.name].busy)
        assert self._resumed_prompt(chat) == "read msg 42"

    def test_finished_session_is_not_resumed(self, app_env):
        state_dir = Path(app_env["state_dir"])
        log = SessionLog(state_dir / "session-20260101-120000-000000.jsonl")
        log.task_start("all done")
        log.message({"role": "user", "content": "all done"})
        log.task_end()
        log.close()
        client, chat = make_client(app_env, [])
        with client:
            time.sleep(0.3)
            assert log.path.name not in client.app.state.server.sessions
        assert chat.calls == []

    def test_repeatedly_interrupted_task_is_abandoned(self, app_env):
        # A task that keeps killing the server must not crash-loop it.
        path = self._interrupted_log(app_env, attempts=server_module.RESUME_MAX_ATTEMPTS)
        client, chat = make_client(app_env, [])
        with client:
            time.sleep(0.3)
            assert path.name not in client.app.state.server.sessions
        assert chat.calls == []

    def test_stale_interrupted_task_is_not_resumed(self, app_env):
        path = self._interrupted_log(app_env)
        old = time.time() - server_module.RESUME_WINDOW - 60
        os.utime(path, (old, old))
        client, chat = make_client(app_env, [])
        with client:
            time.sleep(0.3)
            assert path.name not in client.app.state.server.sessions
        assert chat.calls == []

    def test_task_markers_bracket_a_normal_run(self, app_env):
        client, _ = make_client(app_env, [model_says("hi there")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "hello"})
            recv_until(ws, "done")
            path = Path(app_env["state_dir"]) / hello["session"]
            assert SessionLog.pending_task(path) is None
            kinds = [json.loads(line)["kind"] for line in path.read_text().splitlines()]
            assert kinds.count("task_start") == 1 and kinds.count("task_end") == 1

    def test_user_command_leaves_no_resume_marker(self, app_env):
        # A ! command is the user's own shell action — re-running it unattended
        # is a risk, not a recovery, so it writes no marker.
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "!echo hi"})
            recv_until(ws, "done")
            path = Path(app_env["state_dir"]) / hello["session"]
            assert SessionLog.pending_task(path) is None
            assert "task_start" not in path.read_text()


class TestOfflineMirror:
    """Issue #165: the read-only endpoints the PWA mirrors conversations
    through. They must serve a session straight off disk — no Agent, no session
    slot — and must not resend what the client already holds."""

    def test_index_lists_sessions_and_gates_on_token(self, app_env):
        client, _ = make_client(app_env, [model_says("mirrored answer")], token="s3cret")
        with client, connected(client, "/ws?token=s3cret") as (ws, hello, _):
            ws.send_json({"type": "task", "text": "a task worth keeping"})
            recv_until(ws, "done")

            assert client.get("/offline/index").status_code == 403
            payload = client.get("/offline/index?token=s3cret").json()

        assert payload["rev"]  # the frontend's staleness check rides along
        row = next(s for s in payload["sessions"] if s["name"] == hello["session"])
        assert row["title"] == "a task worth keeping"
        assert row["origin"] == "user"
        assert row["ts"] > 0

    def test_session_returns_the_same_events_a_client_replays(self, app_env):
        client, _ = make_client(app_env, [model_says("the recommendation")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "what should I do?"})
            recv_until(ws, "done")
            payload = client.get(f"/offline/session?session={hello['session']}").json()

        # Same shape the live `replay` frame carries, which is what lets the
        # cached copy render through the unchanged onReplay path.
        assert payload["base"] == 0
        assert payload["total"] == len(payload["events"])
        types = [e["type"] for e in payload["events"]]
        assert types[0] == "user" and types[-1] == "done"
        assert payload["events"][0]["text"] == "what should I do?"
        assert payload["events"][-1]["result"] == "the recommendation"
        assert payload["title"] == "what should I do?"
        assert payload["sig"]

    def test_unknown_session_is_404_and_traversal_is_refused(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/offline/session?session=nope.jsonl").status_code == 404
            assert client.get(
                "/offline/session?session=session-../../etc/passwd"
            ).status_code == 404

    def test_unchanged_session_answers_304_with_no_body(self, app_env):
        client, _ = make_client(app_env, [model_says("done")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "first"})
            recv_until(ws, "done")
            url = f"/offline/session?session={hello['session']}"
            first = client.get(url)
            etag = first.headers["etag"]
            again = client.get(url, headers={"If-None-Match": etag})

        assert first.status_code == 200 and etag
        assert again.status_code == 304
        assert not again.content

    def test_since_and_sig_return_only_the_new_events(self, app_env):
        client, _ = make_client(
            app_env, [model_says("answer one"), model_says("answer two")]
        )
        with client, connected(client) as (ws, hello, _):
            url = f"/offline/session?session={hello['session']}"
            ws.send_json({"type": "task", "text": "first"})
            recv_until(ws, "done")
            first = client.get(url).json()

            ws.send_json({"type": "task", "text": "second"})
            recv_until(ws, "done")
            delta = client.get(f"{url}&since={first['total']}&sig={first['sig']}").json()
            # A prefix the server can't vouch for falls back to the whole thing
            # rather than silently splicing onto a stream that moved.
            stale = client.get(f"{url}&since={first['total']}&sig=deadbeef").json()

        assert delta["base"] == first["total"]
        assert delta["events"][0] == {"type": "user", "text": "second"}
        assert delta["events"][-1]["result"] == "answer two"
        assert delta["total"] == first["total"] + len(delta["events"])

        assert stale["base"] == 0
        assert len(stale["events"]) == stale["total"]

    def test_bulk_command_output_is_trimmed_but_the_answer_is_not(self, app_env, tmp_path):
        # The mirror keeps the conversation verbatim and caps the noise: that
        # asymmetry is what makes a full local archive affordable.
        long_answer = "conclusion. " * 2000
        path = tmp_path / "session-trim.jsonl"
        huge = "x" * (server_module.OFFLINE_OUTPUT_CAP * 3)
        records = [
            {"kind": "message", "role": "user", "content": "run it"},
            {"kind": "trace", "step": {"kind": "tool", "name": "read_file", "output": huge}},
            {"kind": "message", "role": "assistant", "content": long_answer},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in records))

        events = server_module.offline_events(path)
        step = next(e for e in events if e["type"] == "step")
        assert len(step["output"]) < server_module.OFFLINE_OUTPUT_CAP + 200
        assert "trimmed for offline" in step["output"]
        assert step["output"].startswith("x") and step["output"].endswith("x")
        # The answer is the whole point of going back to an old chat.
        assert next(e for e in events if e["type"] == "done")["result"] == long_answer.strip()

    def test_legacy_log_without_traces_still_mirrors(self, app_env, tmp_path):
        # Pre-trace logs can't reconstruct an event stream; they fall back to the
        # flat history blob the frontend already knows how to render, so old
        # conversations are mirrored too instead of being silently skipped.
        path = tmp_path / "session-legacy.jsonl"
        path.write_text(
            json.dumps({"kind": "message", "role": "user", "content": "old question"}) + "\n"
        )
        events = server_module.offline_events(path)
        assert [e["type"] for e in events] == ["history"]
        assert events[0]["messages"][0]["content"] == "old question"
