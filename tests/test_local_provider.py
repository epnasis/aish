"""The `local:` provider (#404): a self-hosted OpenAI-compatible server
(mlx_lm.server on another Mac, in the owner's setup) as a first-class backend.

No server, no network: the OpenAI client is a fake that records what it was
handed, and what is asserted is what would have been SENT — the address, the
key, the model, `max_tokens`, the thinking switch — plus how the answer is
attributed and sized. The one test that builds the real SDK client never makes
a request with it.
"""

import datetime
import json
from types import SimpleNamespace

import pytest

from aish import agent as agent_module
from aish import backends, cli, embeddings, ratelimit, tool_plugins, usage
from aish import skills as skills_module
from aish.agent import Agent
from aish.backends import BackendError, make_chat, parse_model

URL = "http://mi.lan:8080/v1"
REPO = "mlx-community/Qwen3.6-35B-A3B-8bit"


def _completion(content="ok", tool_calls=None, reasoning=None, usage_=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning is not None:
        message.reasoning = reasoning
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=usage_ or SimpleNamespace(prompt_tokens=7, completion_tokens=3),
    )


class FakeClient:
    """Stands in for openai.OpenAI: records kwargs, answers from a script."""

    def __init__(self, responses=None, stream_chunks=None):
        self.calls: list[dict] = []
        self.responses = list(responses or [])
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(json.loads(json.dumps(kwargs)))
                if kwargs.get("stream"):
                    return iter(stream_chunks or [])
                return outer.responses.pop(0) if outer.responses else _completion()

        self.chat = SimpleNamespace(completions=_Completions())


@pytest.fixture
def local_env(monkeypatch):
    monkeypatch.setenv("AISH_LOCAL_URL", URL)
    return monkeypatch


# ----------------------------------------------------------------- routing


class TestLocalRouting:
    def test_the_rest_of_the_spec_is_the_model_verbatim(self):
        assert parse_model(f"local:{REPO}") == ("local", REPO)
        assert parse_model("local:/Users/x/mlx/model") == ("local", "/Users/x/mlx/model")

    def test_bare_local_asks_for_the_servers_own_model(self):
        """mlx-lm maps `default_model` to the model it was started with."""
        assert parse_model("local") == ("local", "default_model")

    def test_it_is_not_a_cloud_provider(self):
        assert backends.PROVIDERS["local"].cloud is False
        assert all(p.cloud for name, p in backends.PROVIDERS.items() if name != "local")


class TestLocalMissingAddress:
    def test_no_url_is_a_clear_error_naming_the_variable(self, monkeypatch):
        monkeypatch.delenv("AISH_LOCAL_URL", raising=False)
        with pytest.raises(BackendError, match="AISH_LOCAL_URL"):
            make_chat(f"local:{REPO}")

    def test_openai_settings_never_stand_in_for_it(self, monkeypatch):
        """No silent fallback to OpenAI (or to Ollama) when the address is
        missing, whatever the OpenAI variables say."""
        monkeypatch.delenv("AISH_LOCAL_URL", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        with pytest.raises(BackendError, match="AISH_LOCAL_URL"):
            make_chat(f"local:{REPO}")

    def test_the_model_list_needs_it_too(self, monkeypatch):
        monkeypatch.delenv("AISH_LOCAL_URL", raising=False)
        with pytest.raises(BackendError, match="AISH_LOCAL_URL"):
            backends.list_models("local")


class TestLocalTheRealClient:
    """Built for real (the SDK is installed), never used to send anything."""

    def _client(self, local_env):
        chat, provider, model = make_chat(f"local:{REPO}")
        assert (provider, model) == ("local", REPO)
        return chat.__wrapped__.client

    def test_it_points_at_the_local_url_with_a_placeholder_key(self, local_env):
        client = self._client(local_env)
        assert str(client.base_url).rstrip("/") == URL
        assert client.api_key == backends.LOCAL_API_KEY_PLACEHOLDER
        assert client.max_retries == backends.SDK_RETRIES

    def test_the_local_key_is_used_when_set(self, local_env):
        local_env.setenv("AISH_LOCAL_API_KEY", "local-secret")
        assert self._client(local_env).api_key == "local-secret"

    def test_nothing_of_openais_reaches_it(self, local_env):
        """The collision the `openai:` + OPENAI_BASE_URL hack had: the real
        OpenAI key, address and account ids must never ride a local call."""
        local_env.setenv("OPENAI_API_KEY", "sk-real-openai-key")
        local_env.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        local_env.setenv("OPENAI_ORG_ID", "org-real")
        local_env.setenv("OPENAI_PROJECT_ID", "proj-real")
        client = self._client(local_env)
        assert client.api_key == backends.LOCAL_API_KEY_PLACEHOLDER
        assert str(client.base_url).rstrip("/") == URL
        headers = {k: v for k, v in client.default_headers.items() if isinstance(v, str)}
        assert "org-real" not in headers.values()
        assert "proj-real" not in headers.values()
        assert "sk-real-openai-key" not in json.dumps(client.auth_headers)

    def test_real_openai_is_untouched_by_the_local_settings(self, local_env):
        local_env.setenv("OPENAI_API_KEY", "sk-real-openai-key")
        chat, provider, _ = make_chat("openai:gpt-5.6")
        client = chat.__wrapped__.client
        assert provider == "openai"
        assert client.api_key == "sk-real-openai-key"
        assert "mi.lan" not in str(client.base_url)


# ------------------------------------------------------------ what is sent


class TestLocalWhatIsSent:
    def _chat(self, client):
        chat, _, _ = make_chat(f"local:{REPO}", client=client)
        return chat

    def test_model_max_tokens_and_the_thinking_switch(self):
        client = FakeClient()
        schemas = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        self._chat(client)(
            model=REPO,
            messages=[{"role": "user", "content": "x"}],
            tools=schemas,
            options={"num_ctx": 8192},
            think=True,
        )
        sent = client.calls[0]
        assert sent["model"] == REPO
        # mlx-lm caps an unstated max_tokens at 512.
        assert sent["max_tokens"] == backends.DEFAULT_LOCAL_MAX_TOKENS == 16_384
        assert sent["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
        assert sent["tools"] == schemas
        assert "options" not in sent and "think" not in sent

    def test_think_off_says_so_rather_than_leaving_the_template_default(self):
        client = FakeClient()
        self._chat(client)(model=REPO, messages=[{"role": "user", "content": "x"}], think=False)
        assert client.calls[0]["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    def test_the_streaming_path_sends_the_same(self):
        client = FakeClient(stream_chunks=[])
        list(self._chat(client)(
            model=REPO, messages=[{"role": "user", "content": "x"}], think=True, stream=True
        ))
        sent = client.calls[0]
        assert sent["stream"] is True
        assert sent["max_tokens"] == 16_384
        assert sent["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}

    def test_max_tokens_is_configurable(self, monkeypatch):
        monkeypatch.setenv("AISH_LOCAL_MAX_TOKENS", "4096")
        client = FakeClient()
        self._chat(client)(model=REPO, messages=[{"role": "user", "content": "x"}])
        assert client.calls[0]["max_tokens"] == 4096

    @pytest.mark.parametrize("name", ["AISH_LOCAL_MAX_TOKENS", "AISH_LOCAL_CTX"])
    @pytest.mark.parametrize("value", ["lots", "0", "-5", "32k"])
    def test_a_bad_number_is_a_clear_error_not_a_silent_default(self, monkeypatch, name, value):
        monkeypatch.setenv(name, value)
        with pytest.raises(BackendError, match=name):
            make_chat(f"local:{REPO}", client=FakeClient())

    def test_other_providers_get_neither_knob(self):
        client = FakeClient()
        backends.OpenAICompatBackend(client, "openai")(
            model="gpt", messages=[{"role": "user", "content": "x"}], think=True
        )
        assert "max_tokens" not in client.calls[0]
        assert "extra_body" not in client.calls[0]

    def test_the_sent_seam_reports_exactly_what_the_client_got(self):
        client = FakeClient()
        seen = []
        with backends.observe_sent(seen.append):
            self._chat(client)(model=REPO, messages=[{"role": "user", "content": "x"}])
        (request,) = seen
        assert request.provider == "local"
        assert json.loads(json.dumps(request.payload)) == client.calls[0]


# ------------------------------------------------------- what comes back


class TestLocalReasoning:
    """mlx_lm.server 0.31.3 puts reasoning in `message.reasoning` (and
    `delta.reasoning` when streaming); other servers spell it
    `reasoning_content`. Either lands in `thinking`, never in the answer."""

    def test_non_streaming_reasoning_field(self):
        client = FakeClient([_completion(content="42", reasoning="adding it up")])
        chat, _, _ = make_chat(f"local:{REPO}", client=client)
        out = chat(model=REPO, messages=[{"role": "user", "content": "x"}], think=True)
        assert out.message.content == "42"
        assert out.message.thinking == "adding it up"

    def test_reasoning_content_spelling(self):
        message = SimpleNamespace(content="a", tool_calls=None, reasoning_content="why")
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None
        )
        assert backends._from_completion(response).message.thinking == "why"

    def test_streaming_reasoning_deltas(self):
        def delta(**kw):
            fields = {"content": None, "tool_calls": None, **kw}
            return SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(**fields))], usage=None
            )

        chunks = [
            delta(reasoning="step one, "),
            delta(reasoning="step two"),
            delta(content="answer"),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=11, completion_tokens=5),
            ),
        ]
        chat, _, _ = make_chat(f"local:{REPO}", client=FakeClient(stream_chunks=chunks))
        out = list(chat(model=REPO, messages=[{"role": "user", "content": "x"}], stream=True))
        assert "".join(c.message.thinking for c in out) == "step one, step two"
        assert "".join(c.message.content for c in out) == "answer"
        assert (out[-1].prompt_eval_count, out[-1].eval_count) == (11, 5)


class TestLocalAccounting:
    def test_usage_is_counted_with_the_cache_split(self):
        """mlx-lm reports the prompt-cache hit as `cached_tokens`, a subset of
        `prompt_tokens` (0.31.3, `completion_usage_response`)."""
        reported = SimpleNamespace(
            prompt_tokens=12_000,
            completion_tokens=300,
            prompt_tokens_details=SimpleNamespace(cached_tokens=11_500),
        )
        client = FakeClient([_completion(usage_=reported)])
        chat, _, _ = make_chat(f"local:{REPO}", client=client)
        out = chat(model=REPO, messages=[{"role": "user", "content": "x"}])
        assert (out.prompt_eval_count, out.eval_count) == (12_000, 300)
        assert out.usage == {
            "semantics": backends.INPUT_INCLUDES_CACHE,
            "input": 12_000,
            "cached": 11_500,
            "output": 300,
        }

    def test_a_session_is_attributed_to_local_not_openai(self, tmp_path):
        path = tmp_path / "session-20260922-101010-000001.jsonl"
        records = [
            {"ts": "2026-09-22T10:10:10", "kind": "model", "model": f"local:{REPO}"},
            {"ts": "2026-09-22T10:10:11", "kind": "trace",
             "step": {"kind": "reasoning", "model_call": 1, "tokens": [100, 10]}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        session = usage.scan_session(path)
        assert (session.provider, session.model) == ("local", REPO)
        assert session.calls[0].provider == "local"
        assert session.spend == 110

    def test_the_governor_key_is_the_local_provider(self, monkeypatch):
        keys = []
        real = ratelimit.reserve_for_call

        def spy(key, messages):
            keys.append(key)
            return real(key, messages)

        monkeypatch.setattr(ratelimit, "reserve_for_call", spy)
        chat, _, _ = make_chat(f"local:{REPO}", client=FakeClient())
        chat(model=REPO, messages=[{"role": "user", "content": "x"}])
        assert keys == [f"local:{REPO}"]


class TestLocalFailures:
    def test_a_refused_connection_is_transport_and_says_what_matched(self):
        """What the SDK raises when nothing listens at the address. The class
        is read off the exception's own words; the record names them."""
        import httpx
        import openai

        exc = openai.APIConnectionError(request=httpx.Request("POST", f"{URL}/chat/completions"))
        failure = ratelimit.classify(exc)
        assert failure.kind == ratelimit.TRANSPORT
        assert failure.retryable
        assert failure.status is None
        assert failure.matched.lower() == "connection"


class TestLocalWindow:
    def test_default_window_and_its_provenance(self):
        assert backends.context_window("local") == (32_768, "backend:local:32768")

    def test_the_owner_states_it(self, monkeypatch):
        monkeypatch.setenv("AISH_LOCAL_CTX", "131072")
        assert backends.context_window("local") == (131_072, "backend:local:131072")

    def test_the_agent_sizes_history_and_caps_from_it(self, monkeypatch):
        monkeypatch.setenv("AISH_LOCAL_CTX", "65536")
        agent = Agent(model=REPO, approve=lambda _c: True, client_chat=lambda **_: None)
        agent.provider = "local"
        budget, source = agent._history_budget()
        assert source == "backend:local:65536"
        assert budget == 65_536 * agent_module.CHARS_PER_TOKEN_BUDGET
        caps, cap_source = agent._output_caps()
        assert cap_source == "backend:local:65536"
        assert caps == tool_plugins.output_caps(65_536)

    def test_policies_are_declared(self):
        assert backends.system_role_policy("local") == "first_only"
        assert backends.media_support("local") == frozenset()


class TestLocalRolesStayOffIt:
    def test_a_local_session_gives_a_role_no_model(self):
        """The shipped charter declares `cloud-fast`; the owner's own server is
        not that, exactly as Ollama is not."""
        agent = Agent(model=REPO, approve=lambda _c: True, client_chat=lambda **_: None)
        agent.provider = "local"
        assert agent._role_model() == ""
        agent.provider = "gemini"
        assert agent._role_model() == f"gemini:{REPO}"


# ------------------------------------------------------------ the picker


class TestLocalSelectable:
    def test_the_picker_offers_it_as_the_owners_server(self, monkeypatch):
        monkeypatch.setattr(cli, "cloud_model_catalog", lambda _state: {"local": [REPO]})
        agent = SimpleNamespace(provider="ollama", model="qwen3:8b")
        rows = dict(cli.available_models(agent, state_dir=object()))
        assert rows["local"].startswith("your server · default default_model")
        assert rows[f"local:{REPO}"].startswith("your server · ")
        assert rows["gemini"].startswith("cloud · ")

    def test_typing_a_local_spec_offers_that_exact_model(self):
        ranked = cli.rank_models([], f"local:{REPO}")
        assert ranked[0][0] == f"local:{REPO}"
        assert ranked[0][1].startswith("your server · ")

    def test_the_model_list_comes_from_the_server(self, local_env, monkeypatch):
        listed = SimpleNamespace(list=lambda: [SimpleNamespace(id="b"), SimpleNamespace(id="a")])
        built = []

        def fake_client(timeout=None):
            built.append(timeout)
            return SimpleNamespace(models=listed)

        monkeypatch.setattr(backends, "_local_client", fake_client)
        assert backends.list_models("local") == ["a", "b"]
        assert built == [10]

    def test_identity_says_where_it_runs(self, local_env):
        text = cli.identity_context(REPO, "local")
        assert URL in text
        assert "not a cloud service" in text
        assert "not Ollama" in text

    def test_spec_round_trips(self):
        agent = SimpleNamespace(provider="local", model=REPO)
        assert cli.model_spec(agent) == f"local:{REPO}"
        assert parse_model(cli.model_spec(agent)) == ("local", REPO)


# ------------------------------------------------------ split embed host


class TestEmbedHost:
    def _fake_ollama(self, monkeypatch):
        import ollama

        calls = []

        class FakeOllamaClient:
            def __init__(self, host=None):
                calls.append(("client", host))

            def embed(self, model, input):  # noqa: A002 — the library's keyword
                calls.append(("client.embed", model))
                return {"embeddings": [[1.0, 0.0] for _ in input]}

        def module_embed(model, input):  # noqa: A002
            calls.append(("module.embed", model))
            return {"embeddings": [[0.0, 1.0] for _ in input]}

        embeddings._embed_client.cache_clear()
        monkeypatch.setattr(ollama, "Client", FakeOllamaClient)
        monkeypatch.setattr(ollama, "embed", module_embed)
        return calls

    def test_unset_keeps_the_module_level_client(self, monkeypatch):
        calls = self._fake_ollama(monkeypatch)
        monkeypatch.delenv("AISH_EMBED_HOST", raising=False)
        assert embeddings._ollama_embed("embeddinggemma", ["a"]) == [[0.0, 1.0]]
        assert calls == [("module.embed", "embeddinggemma")]

    def test_set_builds_a_client_for_that_host_only_once(self, monkeypatch):
        calls = self._fake_ollama(monkeypatch)
        monkeypatch.setenv("AISH_EMBED_HOST", "http://127.0.0.1:11434")
        embeddings._ollama_embed("embeddinggemma", ["a"])
        embeddings._ollama_embed("embeddinggemma", ["b"])
        assert calls == [
            ("client", "http://127.0.0.1:11434"),
            ("client.embed", "embeddinggemma"),
            ("client.embed", "embeddinggemma"),
        ]
        embeddings._embed_client.cache_clear()

    def test_an_unreachable_embed_host_still_degrades_to_lexical(self, monkeypatch):
        import ollama

        class Down:
            def __init__(self, host=None):
                pass

            def embed(self, model, input):  # noqa: A002
                raise ConnectionError("Failed to connect to Ollama")

        embeddings._embed_client.cache_clear()
        monkeypatch.setattr(ollama, "Client", Down)
        monkeypatch.setenv("AISH_EMBED_HOST", "http://nowhere:11434")
        index = embeddings.SemanticIndex()
        entry = SimpleNamespace(name="n", description="d", keywords=[])
        assert index.scores("task", [entry]) is None
        assert "Failed to connect" in index.error
        embeddings._embed_client.cache_clear()


# ---------------------------------------------------- prompt-prefix stability


class _AdvancingClock(datetime.datetime):
    """`now()` moves a minute on every read, so anything that stamps the time
    per model call shows up as a changed prefix."""

    ticks = 0

    @classmethod
    def now(cls, tz=None):
        cls.ticks += 1
        base = datetime.datetime(2026, 9, 22, 10, 0, tzinfo=datetime.UTC)
        moment = base + datetime.timedelta(minutes=cls.ticks)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


PLUGIN = """---
name: {name}
description: {name} does a thing
exec: ./run.sh
mutating: no
returns: text
schema: {{"text": {{"type": "string", "required": true}}, "mode": {{"type": "string"}}}}
---
body
"""


class TestPromptPrefixStability:
    """mlx-lm reuses the KV cache for the longest common token PREFIX of an
    earlier request. Within one task every call must therefore send the same
    tools (same order, same bytes) and a message list that EXTENDS the previous
    call's rather than rewriting any of it — the main system prompt and the
    per-task reminder above all, since they come first."""

    def _run(self, monkeypatch, tmp_path):
        _AdvancingClock.ticks = 0
        monkeypatch.setattr(datetime, "datetime", _AdvancingClock)
        for name in ("zeta_tool", "alpha_tool"):
            directory = tool_plugins.GLOBAL_TOOLS_DIR / name
            directory.mkdir()
            (directory / "TOOL.md").write_text(PLUGIN.format(name=name))
            script = directory / "run.sh"
            script.write_text("#!/bin/sh\ncat\n")
            script.chmod(0o755)
        for slug, fact in (
            ("deploy-host", "Deploy the photo gallery to host gallery.lan with rsync."),
            ("deploy-user", "The photo gallery deploy runs as user www."),
        ):
            skills_module.save_memory(
                fact, skills_module.GLOBAL_MEMORY_DIR, name=slug,
                keywords="photo, gallery, deploy",
            )
        monkeypatch.setattr(agent_module.tools, "read_docs", lambda command, topic=None: "doc")

        def calls(*names):
            return [
                SimpleNamespace(
                    function=SimpleNamespace(name="read_docs", arguments=json.dumps({"command": n}))
                )
                for n in names
            ]

        client = FakeClient([
            _completion(content="", tool_calls=calls("ls")),
            _completion(content="", tool_calls=calls("git", "rsync")),
            _completion(content="", tool_calls=calls("cat")),
            _completion(content="deployed"),
        ])
        chat, _, _ = make_chat(f"local:{REPO}", client=client)
        agent = Agent(
            model=REPO, approve=lambda _c: True, client_chat=chat,
            cwd=str(tmp_path), state_dir=str(tmp_path / "state"),
        )
        agent.provider = "local"
        assert agent.run_task("deploy the photo gallery") == "deployed"
        return client.calls

    def test_every_call_in_a_task_extends_the_one_before(self, monkeypatch, tmp_path):
        sent = self._run(monkeypatch, tmp_path)
        assert len(sent) == 4
        for before, after in zip(sent, sent[1:], strict=False):
            # Byte identity, in the order sent: a reordered key re-tokenises.
            assert json.dumps(after["tools"]) == json.dumps(before["tools"])
            prefix = after["messages"][: len(before["messages"])]
            assert json.dumps(prefix) == json.dumps(before["messages"])
            assert after["extra_body"] == before["extra_body"]
            assert after["max_tokens"] == before["max_tokens"]

    def test_the_scenario_exercises_what_it_claims(self, monkeypatch, tmp_path):
        """Guards the test above against passing vacuously: the plugins are on
        the menu, the reminder carries the preloaded knowledge, and its time
        came from the advancing clock — so a per-call stamp would have moved."""
        sent = self._run(monkeypatch, tmp_path)
        names = [t["function"]["name"] for t in sent[0]["tools"]]
        assert names[-2:] == ["alpha_tool", "zeta_tool"]  # sorted, after the natives
        first, reminder, task = sent[0]["messages"][:3]
        assert first["role"] == "system"
        assert reminder["role"] == "user" and reminder["content"].startswith("<system-reminder>")
        assert "Current local time" in reminder["content"]
        assert "gallery.lan" in reminder["content"]
        assert task == {"role": "user", "content": "deploy the photo gallery"}
        assert "Current local time: 2026-09-22T" in reminder["content"]
        assert _AdvancingClock.ticks >= 1

    def test_two_chats_send_the_same_prefix(self, monkeypatch, tmp_path):
        """mlx-lm checkpoints the cache at the end of the leading system
        messages and, on a model whose cache cannot be trimmed, reuses only an
        EXACT prefix. Each chat has its own scratch dir, so a path in the
        system message meant no chat could reuse another's: every new chat
        re-read ~35k tokens (26 s on mi, 2026-09-22)."""
        monkeypatch.setattr(datetime, "datetime", _AdvancingClock)
        sent = []
        for chat_name in ("one", "two"):
            client = FakeClient([_completion(content="hi")])
            chat, _, _ = make_chat(f"local:{REPO}", client=client)
            state = tmp_path / chat_name
            state.mkdir()
            log = state / "session-x.jsonl"
            agent = Agent(
                model=REPO, approve=lambda _c: True, client_chat=chat, cwd=str(tmp_path),
                state_dir=str(state), current_session=lambda log=log: log,
            )
            agent.provider = "local"
            agent.run_task("hello")
            sent.append((client.calls[0], agent.scratch_dir))
        (one, one_dir), (two, two_dir) = sent
        assert one_dir != two_dir
        assert json.dumps(one["tools"]) == json.dumps(two["tools"])
        assert json.dumps(one["messages"][0]) == json.dumps(two["messages"][0])
        assert str(one_dir) in one["messages"][1]["content"]  # the path still reaches it
