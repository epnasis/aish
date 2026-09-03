"""Backend routing, message conversion, and OpenAI-compat normalization."""

import json
from types import SimpleNamespace

import pytest

from aish import backends
from aish.backends import (
    GEMINI_THINKING_BODY,
    AnthropicBackend,
    BackendError,
    OpenAICompatBackend,
    _from_anthropic,
    anthropic_tools,
    convert_messages,
    convert_messages_anthropic,
    make_chat,
    parse_model,
    split_thoughts,
)

# ---------------------------------------------------------------- routing


def test_plain_name_routes_to_ollama():
    assert parse_model("qwen3.6:35b-a3b") == ("ollama", "qwen3.6:35b-a3b")


def test_unknown_prefix_stays_ollama():
    # Ollama tags use ':' too — only known providers are treated as prefixes.
    assert parse_model("llama3:8b") == ("ollama", "llama3:8b")


def test_bare_provider_uses_default_model():
    provider, model = parse_model("gemini")
    assert provider == "gemini"
    assert model == backends.PROVIDERS["gemini"].default_model


def test_prefixed_model():
    assert parse_model("gemini:gemini-3.1-pro") == ("gemini", "gemini-3.1-pro")
    assert parse_model("openai:gpt-5.6-luna") == ("openai", "gpt-5.6-luna")


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(BackendError, match="GEMINI_API_KEY"):
        make_chat("gemini")


def test_make_chat_with_injected_client():
    chat, provider, model = make_chat("gemini:gemini-3.5-flash", client=object())
    assert provider == "gemini"
    assert model == "gemini-3.5-flash"
    # Wrapped by the governor (#261), which is what puts EVERY consumer of the
    # key behind one pacing point rather than only the agent loop.
    assert isinstance(chat.__wrapped__, OpenAICompatBackend)


# ------------------------------------------------------- message conversion


def test_convert_plain_messages_pass_through():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert convert_messages(messages) == messages


def test_convert_collapses_extra_system_messages_to_user():
    # Gemini's OpenAI-compat gateway drops ALL system instructions when more
    # than one system message is present (issue #74). Only the first stays
    # system; later ones (aish's recency reminder) become user turns in place.
    messages = [
        {"role": "system", "content": "main prompt"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "<system-reminder>reminder</system-reminder>"},
        {"role": "user", "content": "go"},
    ]
    out = convert_messages(messages)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user", "user"]
    assert out[0]["content"] == "main prompt"
    assert out[3] == {"role": "user", "content": "<system-reminder>reminder</system-reminder>"}


def test_convert_pairs_tool_results_with_synthetic_ids():
    messages = [
        {"role": "user", "content": "list files"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "run_command", "arguments": {"command": "ls"}}},
                {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}},
            ],
        },
        {"role": "tool", "tool_name": "run_command", "content": "a.txt"},
        {"role": "tool", "tool_name": "read_file", "content": "contents"},
    ]
    out = convert_messages(messages)
    calls = out[1]["tool_calls"]
    assert [c["id"] for c in calls] == ["call_1_0", "call_1_1"]
    assert calls[0]["type"] == "function"
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls"}
    assert out[2] == {"role": "tool", "tool_call_id": "call_1_0", "content": "a.txt"}
    assert out[3]["tool_call_id"] == "call_1_1"


def test_orphan_tool_message_becomes_user_content():
    out = convert_messages([{"role": "tool", "tool_name": "run_command", "content": "x"}])
    assert out[0]["role"] == "user"
    assert "run_command" in out[0]["content"]
    assert "x" in out[0]["content"]


# ------------------------------------------------- response normalization


def _completion(content=None, tool_calls=None, prompt=7, completion=3):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


class FakeClient:
    """Stands in for openai.OpenAI: records kwargs, returns canned responses."""

    def __init__(self, response=None, stream_chunks=None):
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if kwargs.get("stream"):
                    return iter(stream_chunks or [])
                return response

        self.chat = SimpleNamespace(completions=_Completions())


def test_non_stream_response_normalized():
    tc = SimpleNamespace(
        function=SimpleNamespace(name="run_command", arguments='{"command": "ls"}')
    )
    backend = OpenAICompatBackend(FakeClient(_completion(content="hi", tool_calls=[tc])), "gemini")
    result = backend(model="m", messages=[{"role": "user", "content": "x"}])
    assert result.message.content == "hi"
    assert result.message.tool_calls[0].function.name == "run_command"
    assert result.message.tool_calls[0].function.arguments == {"command": "ls"}
    assert (result.prompt_eval_count, result.eval_count) == (7, 3)


def test_bad_tool_arguments_fall_back_to_empty_dict():
    tc = SimpleNamespace(function=SimpleNamespace(name="f", arguments="not json"))
    backend = OpenAICompatBackend(FakeClient(_completion(tool_calls=[tc])), "gemini")
    result = backend(model="m", messages=[])
    assert result.message.tool_calls[0].function.arguments == {}


def test_tools_passed_through_and_ollama_kwargs_dropped():
    client = FakeClient(_completion(content="ok"))
    backend = OpenAICompatBackend(client, "gemini")
    schemas = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    backend(
        model="m",
        messages=[{"role": "user", "content": "x"}],
        tools=schemas,
        options={"num_ctx": 32768},
        think=True,
    )
    kwargs = client.calls[0]
    assert kwargs["tools"] == schemas
    assert "options" not in kwargs
    assert "think" not in kwargs


# ----------------------------------------------------------------- streaming


def _delta_chunk(content=None, tool_calls=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)


def test_stream_preserves_gemini_thought_signature():
    extra = {"google": {"thought_signature": "SIG"}}
    frag = SimpleNamespace(
        index=0,
        function=SimpleNamespace(name="f", arguments="{}"),
        extra_content=extra,
    )
    chunks = [_delta_chunk(tool_calls=[frag])]
    backend = OpenAICompatBackend(FakeClient(stream_chunks=chunks), "gemini")
    out = list(backend(model="m", messages=[], stream=True))
    assert out[-1].message.tool_calls[0].extra_content == extra


def test_stream_gemini_null_index_calls_stay_separate():
    # Gemini's OpenAI-compat streaming sends index=None with one complete
    # call per fragment (unique id each); only the first call of a turn
    # carries a thought signature. Keying slots on the index used to merge
    # both calls into one ("read_urlread_url" with unparseable arguments).
    extra = {"google": {"thought_signature": "SIG"}}
    frag1 = SimpleNamespace(
        index=None,
        id="0zgx4nnk",
        function=SimpleNamespace(name="read_url", arguments='{"url": "https://a.example"}'),
        extra_content=extra,
    )
    frag2 = SimpleNamespace(
        index=None,
        id="f19844a0",
        function=SimpleNamespace(name="read_url", arguments='{"url": "https://b.example"}'),
        extra_content=None,
    )
    chunks = [_delta_chunk(tool_calls=[frag1]), _delta_chunk(tool_calls=[frag2])]
    backend = OpenAICompatBackend(FakeClient(stream_chunks=chunks), "gemini")
    out = list(backend(model="m", messages=[], stream=True))
    calls = out[-1].message.tool_calls
    assert [c.function.name for c in calls] == ["read_url", "read_url"]
    assert calls[0].function.arguments == {"url": "https://a.example"}
    assert calls[1].function.arguments == {"url": "https://b.example"}
    assert calls[0].extra_content == extra
    assert calls[1].extra_content is None


def test_gemini_stream_splits_thoughts_from_content():
    # Live-API shape: the thought streams first, then a delta holding BOTH the
    # closing tag and the start of the visible answer.
    chunks = [
        _delta_chunk(content="<thought>**Sky**\n\nWhy blue"),
        _delta_chunk(content="</thought>The sky is blue"),
        _delta_chunk(content=" because of scattering."),
    ]
    client = FakeClient(stream_chunks=chunks)
    backend = OpenAICompatBackend(client, "gemini")
    out = list(backend(model="m", messages=[], stream=True))
    thinking = "".join(c.message.thinking for c in out)
    content = "".join(c.message.content for c in out)
    assert thinking == "**Sky**\n\nWhy blue"
    assert content == "The sky is blue because of scattering."
    assert "<thought>" not in content
    # include_thoughts was requested on the way in
    assert client.calls[0]["extra_body"] == GEMINI_THINKING_BODY


def test_gemini_stream_tag_split_across_deltas():
    chunks = [
        _delta_chunk(content="<thou"),
        _delta_chunk(content="ght>hidden</th"),
        _delta_chunk(content="ought>shown"),
    ]
    backend = OpenAICompatBackend(FakeClient(stream_chunks=chunks), "gemini")
    out = list(backend(model="m", messages=[], stream=True))
    assert "".join(c.message.thinking for c in out) == "hidden"
    assert "".join(c.message.content for c in out) == "shown"


def test_gemini_stream_flushes_heldback_text_at_end():
    # A trailing "<" could open a tag — held back, then flushed as real text.
    chunks = [_delta_chunk(content="a < b <")]
    backend = OpenAICompatBackend(FakeClient(stream_chunks=chunks), "gemini")
    out = list(backend(model="m", messages=[], stream=True))
    assert "".join(c.message.content for c in out) == "a < b <"


def test_gemini_non_stream_splits_thoughts():
    backend = OpenAICompatBackend(
        FakeClient(_completion(content="<thought>plan</thought>answer")), "gemini"
    )
    result = backend(model="m", messages=[{"role": "user", "content": "x"}])
    assert result.message.thinking == "plan"
    assert result.message.content == "answer"


def test_openai_provider_gets_no_thinking_config_and_no_filter():
    client = FakeClient(_completion(content="<thought>not gemini</thought>hi"))
    backend = OpenAICompatBackend(client, "openai")
    result = backend(model="m", messages=[{"role": "user", "content": "x"}])
    assert "extra_body" not in client.calls[0]
    assert result.message.content == "<thought>not gemini</thought>hi"
    assert result.message.thinking == ""


def test_split_thoughts_unterminated_tag_is_all_thinking():
    thinking, visible = split_thoughts("<thought>never closed")
    assert thinking == "never closed"
    assert visible == ""


def test_convert_reemits_extra_content():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {"name": "f", "arguments": {}},
                    "extra_content": {"google": {"thought_signature": "SIG"}},
                }
            ],
        },
        {"role": "tool", "tool_name": "f", "content": "ok"},
    ]
    out = convert_messages(messages)
    assert out[0]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "SIG"}}


# ----------------------------------------------------------------- anthropic


def test_anthropic_tool_schema_conversion():
    schema = [
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "runs it",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            },
        }
    ]
    (tool,) = anthropic_tools(schema)
    assert tool["name"] == "run_command"
    assert tool["input_schema"]["properties"]["command"] == {"type": "string"}


def test_anthropic_conversion_prefers_raw_blocks_and_groups_results():
    raw = [
        {"type": "thinking", "thinking": "hmm", "signature": "SIG"},
        {"type": "tool_use", "id": "toolu_1", "name": "run_command", "input": {"command": "ls"}},
        {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {"path": "a"}},
    ]
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{}, {}], "raw_blocks": raw},
        {"role": "tool", "tool_name": "run_command", "content": "out1"},
        {"role": "tool", "tool_name": "read_file", "content": "out2"},
    ]
    system, out = convert_messages_anthropic(messages)
    assert system == "sys"
    assert out[1]["content"] == raw  # echoed verbatim, thinking included
    results = out[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_1", "toolu_2"]
    assert out[2]["role"] == "user" and len(out) == 3


def test_anthropic_conversion_without_raw_blocks_mints_ids():
    messages = [
        {
            "role": "assistant",
            "content": "on it",
            "tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}],
        },
        {"role": "tool", "tool_name": "f", "content": "done"},
    ]
    _, out = convert_messages_anthropic(messages)
    tool_use = out[0]["content"][1]
    assert tool_use["type"] == "tool_use" and tool_use["input"] == {"a": 1}
    assert out[1]["content"][0]["tool_use_id"] == tool_use["id"]


class FakeAnthropicClient:
    def __init__(self, response):
        self.calls = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return response

        self.messages = _Messages()


def _anthropic_block(**kw):
    block = SimpleNamespace(**kw)
    block.model_dump = lambda exclude_none=False: dict(kw)
    return block


def test_anthropic_response_normalized_with_raw_blocks():
    response = SimpleNamespace(
        content=[
            _anthropic_block(type="text", text="hi "),
            _anthropic_block(type="tool_use", id="toolu_9", name="f", input={"x": 1}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=5, output_tokens=2,
            cache_read_input_tokens=100, cache_creation_input_tokens=0,
        ),
    )
    client = FakeAnthropicClient(response)
    backend = AnthropicBackend(client)
    result = backend(
        model="claude-opus-4-8",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
    )
    assert result.message.content == "hi "
    assert result.message.tool_calls[0].function.arguments == {"x": 1}
    assert result.message.raw_blocks[1]["id"] == "toolu_9"
    assert result.prompt_eval_count == 105  # cached tokens counted as input
    sent = client.calls[0]
    assert sent["system"] == "s"
    assert sent["tools"][0]["name"] == "f"
    assert sent["max_tokens"] == AnthropicBackend.MAX_TOKENS


def test_anthropic_thinking_blocks_populate_thinking():
    response = SimpleNamespace(
        content=[
            _anthropic_block(type="thinking", thinking="Comparing the two configs.", signature="S"),
            _anthropic_block(type="text", text="ok"),
        ],
        stop_reason="end_turn",
        usage=None,
    )
    backend = AnthropicBackend(FakeAnthropicClient(response))
    result = backend(model="m", messages=[{"role": "user", "content": "u"}], tools=[])
    assert result.message.thinking == "Comparing the two configs."
    assert result.message.content == "ok"


def test_anthropic_response_without_thinking_has_empty_thinking():
    response = SimpleNamespace(
        content=[_anthropic_block(type="text", text="hi")],
        stop_reason="end_turn",
        usage=None,
    )
    backend = AnthropicBackend(FakeAnthropicClient(response))
    result = backend(model="m", messages=[{"role": "user", "content": "u"}], tools=[])
    assert result.message.thinking == ""


def test_stream_yields_text_then_final_tool_calls_and_usage():
    frag1 = SimpleNamespace(
        index=0, function=SimpleNamespace(name="run_command", arguments='{"comm')
    )
    frag2 = SimpleNamespace(index=0, function=SimpleNamespace(name=None, arguments='and": "ls"}'))
    chunks = [
        _delta_chunk(content="thin"),
        _delta_chunk(content="king"),
        _delta_chunk(tool_calls=[frag1]),
        _delta_chunk(tool_calls=[frag2]),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=5),
        ),
    ]
    backend = OpenAICompatBackend(FakeClient(stream_chunks=chunks), "gemini")
    out = list(backend(model="m", messages=[{"role": "user", "content": "x"}], stream=True))
    text = "".join(c.message.content for c in out if c.message.content)
    assert text == "thinking"
    final = out[-1]
    assert final.message.tool_calls[0].function.name == "run_command"
    assert final.message.tool_calls[0].function.arguments == {"command": "ls"}
    assert (final.prompt_eval_count, final.eval_count) == (11, 5)


# ---------------------------------------------------------------- media


def test_openai_user_media_becomes_data_url_parts(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG-fake")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-fake")
    out = convert_messages(
        [{"role": "user", "content": "look", "images": [str(image)], "documents": [str(pdf)]}]
    )
    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[2]["type"] == "file"
    assert parts[2]["file"]["filename"] == "doc.pdf"
    assert parts[2]["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_openai_missing_media_degrades_to_note(tmp_path):
    out = convert_messages(
        [{"role": "user", "content": "look", "images": [str(tmp_path / "gone.png")]}]
    )
    parts = out[0]["content"]
    assert parts[1]["type"] == "text"
    assert "attachment unavailable" in parts[1]["text"]


def test_anthropic_user_media_becomes_blocks(tmp_path):
    image = tmp_path / "shot.jpg"
    image.write_bytes(b"\xff\xd8fake")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-fake")
    _, out = convert_messages_anthropic(
        [{"role": "user", "content": "look", "images": [str(image)], "documents": [str(pdf)]}]
    )
    blocks = out[0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    assert blocks[1]["type"] == "document"
    assert blocks[1]["source"]["media_type"] == "application/pdf"
    assert blocks[2] == {"type": "text", "text": "look"}


def test_anthropic_tool_media_joins_the_message_holding_the_results(tmp_path):
    """#215: a tool-produced picture arrives as a user message right after the
    tool results it belongs to, and on this API those results ARE a user
    message. Two user entries in a row is not a shape the API takes, so the
    media joins the open one — which is also the truthful shape: one turn, one
    input."""
    image = tmp_path / "page.png"
    image.write_bytes(b"\x89PNGfake")
    _, out = convert_messages_anthropic(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "read_pdf", "arguments": {}}}],
            },
            {"role": "tool", "tool_name": "read_pdf", "content": "page 2 is a scan"},
            {"role": "user", "content": "[aish: 1 picture(s) …]", "images": [str(image)]},
        ]
    )
    assert [entry["role"] for entry in out] == ["assistant", "user"]
    kinds = [block["type"] for block in out[1]["content"]]
    assert kinds == ["tool_result", "image", "text"]


def test_media_support_map():
    assert "image" in backends.media_support("ollama")
    assert "pdf" in backends.media_support("claude")
    assert "pdf" not in backends.media_support("gemini")
    assert backends.media_support("claude-max") == frozenset()


class TestRetryLayers:
    """There must be exactly ONE retry policy, and aish must own it (#261).

    Before this, a single visible "model call failed …; retrying once…" was up
    to twelve HTTP requests: the streaming fallback re-sent (x2), the SDK
    retried underneath (x3), and the agent loop retried on top (x2) — each
    carrying the full ~120k-token history, against the quota that had just run
    out, and none of the inner attempts visible to any code that could classify
    or record them.
    """

    def test_the_sdk_does_not_retry_behind_us(self):
        assert backends.SDK_RETRIES == 0

    def test_stream_fallback_ignores_a_rate_limit(self):
        """The bug in one assertion: a 429 used to take the fallback branch."""

        class RateLimited(Exception):
            status_code = 429
            body = {"error": {"message": "quota exceeded"}}

        assert not backends._rejects_stream_options(RateLimited())

    def test_stream_fallback_ignores_a_server_error(self):
        class Down(Exception):
            status_code = 503

        assert not backends._rejects_stream_options(Down())

    def test_stream_fallback_still_catches_what_it_was_written_for(self):
        """A gateway may reject an unknown field as 400, 404 or 422 — but it
        always names the field, so the field name is the test, not the status."""
        for status in (400, 404, 422):
            exc = type("Rejected", (Exception,), {"status_code": status})(
                "Unrecognized request argument supplied: stream_options"
            )
            assert backends._rejects_stream_options(exc), status

    def test_stream_fallback_ignores_an_unrelated_bad_request(self):
        class BadRequest(Exception):
            status_code = 400

        assert not backends._rejects_stream_options(BadRequest("context length exceeded"))

    def test_a_rate_limit_propagates_out_of_the_stream_helper(self):
        """End to end: the exception must reach the agent loop, which is the
        only layer that classifies, paces and records it."""

        class RateLimited(Exception):
            status_code = 429

        calls = {"n": 0}

        class Client:
            class chat:  # noqa: N801 — mirrors the SDK's attribute shape
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        calls["n"] += 1
                        raise RateLimited("429 quota")

        backend = backends.OpenAICompatBackend(Client(), "gemini")
        with pytest.raises(RateLimited):
            list(backend._stream({"model": "m", "messages": []}))
        assert calls["n"] == 1, "the fallback re-sent a rate-limited request"


class TestUsageDetail:
    """The provider's usage report survives the adapter, units intact (#262).

    Every backend used to flatten its report into two ints, so the cache split —
    the biggest single determinant of what a turn cost, on a loop that resends
    its whole history every step — was discarded where the fields still existed.
    Worse, the surviving number meant three different things: it INCLUDES cached
    tokens on OpenAI-shaped providers, EXCLUDES them on Anthropic (which summed
    all three into it), and excludes KV-cache reuse on Ollama. A daily total
    across providers was adding incompatible units.
    """

    def test_openai_shaped_input_includes_the_cached_subset(self):
        usage = SimpleNamespace(
            prompt_tokens=129_623,
            completion_tokens=250,
            prompt_tokens_details=SimpleNamespace(cached_tokens=100_000),
        )
        detail = backends._openai_usage(usage)
        assert detail["semantics"] == backends.INPUT_INCLUDES_CACHE
        assert detail["input"] == 129_623
        assert detail["cached"] == 100_000
        assert detail["output"] == 250

    def test_a_provider_that_reports_no_cache_omits_the_key(self):
        """A provider that does not report cache reads and a turn that had none
        are different facts; only the absent key tells them apart."""
        usage = SimpleNamespace(
            prompt_tokens=100, completion_tokens=5, prompt_tokens_details=None
        )
        assert "cached" not in backends._openai_usage(usage)

    def test_no_usage_at_all_is_none_not_zeros(self):
        assert backends._openai_usage(None) is None

    def test_anthropic_keeps_the_three_way_split_beside_the_sum(self):
        """A cache read bills at ~10% of base input, so the sum alone cannot
        tell a 1M-token turn that cost 1M from one that cost 100k-equivalent."""
        response = SimpleNamespace(
            content=[],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=1_000,
                cache_read_input_tokens=120_000,
                cache_creation_input_tokens=2_000,
                output_tokens=300,
            ),
        )
        chunk = _from_anthropic(response)
        # The sum stays: everything downstream sizes context from it, and a
        # cache read still occupies the window.
        assert chunk.prompt_eval_count == 123_000
        assert chunk.usage["semantics"] == backends.INPUT_EXCLUDES_CACHE
        assert chunk.usage["input"] == 1_000
        assert chunk.usage["cache_read"] == 120_000
        assert chunk.usage["cache_write"] == 2_000

    def test_the_semantics_flag_is_never_dropped(self):
        assert backends.usage_detail(backends.INPUT_EXCLUDES_KV_REUSE) == {
            "semantics": backends.INPUT_EXCLUDES_KV_REUSE
        }

    def test_the_three_semantics_are_distinct(self):
        flags = {
            backends.INPUT_INCLUDES_CACHE,
            backends.INPUT_EXCLUDES_CACHE,
            backends.INPUT_EXCLUDES_KV_REUSE,
        }
        assert len(flags) == 3


# ------------------------------------------------------- the sent seam (#352)


class TestSentAtTheSeam:
    """Every adapter reports the request it is about to send, AFTER conversion,
    and what it reports serialises byte-for-byte to what its client received.
    A manifest read off the aish side would be false about role and
    cardinality on three of the four backends."""

    @staticmethod
    def _canonical(value):
        from aish.agent import _canonical

        return _canonical(value)

    @staticmethod
    def _sent(client_kwargs):
        return {k: v for k, v in client_kwargs.items() if k not in ("stream", "stream_options")}

    HISTORY = [
        {"role": "system", "content": "base"},
        {"role": "system", "content": "<system-reminder>per-task</system-reminder>"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "run_command", "arguments": {"command": "ls"}}},
                {"function": {"name": "read_file", "arguments": {"path": "a"}}},
            ],
        },
        {"role": "tool", "tool_name": "run_command", "content": "out1", "_stub": True},
        {"role": "tool", "tool_name": "read_file", "content": "out2"},
        {"role": "user", "content": "and now?"},
    ]
    TOOLS = [{"type": "function", "function": {"name": "f", "parameters": {}}}]

    def test_openai_shape_reports_what_the_client_received(self):
        seen = []
        client = FakeClient(_completion(content="ok"))
        backend = OpenAICompatBackend(client, "openai")
        with backends.observe_sent(seen.append):
            backend(model="m", messages=self.HISTORY, tools=self.TOOLS, options={"num_ctx": 1})
        (request,) = seen
        assert request.provider == "openai"
        assert self._canonical(request.payload) == self._canonical(self._sent(client.calls[0]))
        roles = [m["role"] for m in request.payload["messages"]]
        assert roles[:2] == ["system", "user"]  # the reminder was demoted, and the record says so
        assert request.origins == list(range(len(self.HISTORY)))
        assert request.system_origins == []
        assert "options" not in request.payload and "think" not in request.payload

    def test_gemini_reports_its_extra_body_too(self):
        seen = []
        client = FakeClient(_completion(content="ok"))
        backend = OpenAICompatBackend(client, "gemini")
        with backends.observe_sent(seen.append):
            backend(model="m", messages=self.HISTORY, tools=self.TOOLS)
        (request,) = seen
        assert request.payload["extra_body"] == backends.GEMINI_THINKING_BODY
        assert self._canonical(request.payload) == self._canonical(self._sent(client.calls[0]))

    def test_the_streaming_path_reports_the_same_request(self):
        seen = []
        client = FakeClient(stream_chunks=[_delta_chunk(content="hi")])
        backend = OpenAICompatBackend(client, "openai")
        with backends.observe_sent(seen.append):
            list(backend(model="m", messages=self.HISTORY, tools=self.TOOLS, stream=True))
        (request,) = seen
        assert self._canonical(request.payload) == self._canonical(self._sent(client.calls[0]))

    def test_anthropic_reports_the_hoisted_system_and_the_merged_results(self):
        seen = []
        client = FakeAnthropicClient(
            SimpleNamespace(content=[_anthropic_block(type="text", text="hi")],
                            stop_reason="end_turn", usage=None)
        )
        backend = AnthropicBackend(client, "claude")
        with backends.observe_sent(seen.append):
            backend(model="claude-x", messages=self.HISTORY, tools=self.TOOLS, think=True)
        (request,) = seen
        assert request.provider == "claude"
        assert self._canonical(request.payload) == self._canonical(self._sent(client.calls[0]))
        assert request.payload["system"] == "base\n<system-reminder>per-task</system-reminder>"
        assert request.system_origins == [0, 1]
        # user(2), assistant(3), the two tool results merged into one user
        # message (4, 5), user(6): four provider messages from seven.
        assert [m["role"] for m in request.payload["messages"]] == [
            "user", "assistant", "user", "user"
        ]
        assert request.origins == [2, 3, [4, 5], 6]
        assert request.payload["thinking"] == {"type": "adaptive"}

    def test_media_is_replaced_by_a_placeholder_and_only_by_that(self, tmp_path):
        """The stored blob is what was sent with the base64 swapped for a
        placeholder naming the file and its size — on both media-carrying
        shapes, and nothing else about the message moves."""
        import base64

        image_bytes = b"\x89PNG" + b"\x01" * 50
        pdf_bytes = b"%PDF" + b"\x02" * 20
        image = tmp_path / "shot.png"
        image.write_bytes(image_bytes)
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(pdf_bytes)
        history = [
            {"role": "user", "content": "look", "images": [str(image)], "documents": [str(pdf)]},
        ]
        anthropic_client = FakeAnthropicClient(
            SimpleNamespace(content=[], stop_reason="end_turn", usage=None)
        )
        for backend in (
            OpenAICompatBackend(FakeClient(_completion(content="ok")), "openai"),
            AnthropicBackend(anthropic_client, "claude"),
        ):
            seen = []
            with backends.observe_sent(seen.append):
                backend(model="m", messages=history)
            (request,) = seen
            assert request.media == [[(str(image), 54), (str(pdf), 24)]]
            sent = request.payload["messages"][0]
            assert base64.b64encode(image_bytes).decode() in self._canonical(sent)
            stored = backends.without_media(sent, request.media[0])
            text = self._canonical(stored)
            assert base64.b64encode(image_bytes).decode() not in text
            assert base64.b64encode(pdf_bytes).decode() not in text
            assert f"[aish: 54 bytes of {image} — never stored]" in text
            assert f"[aish: 24 bytes of {pdf} — never stored]" in text
            # The placeholder is the ONLY difference: put the base64 back and
            # the two serialise identically.
            image_b64 = base64.b64encode(image_bytes).decode()
            pdf_b64 = base64.b64encode(pdf_bytes).decode()
            restored = text.replace(
                f"[aish: 54 bytes of {image} — never stored]", image_b64
            ).replace(f"[aish: 24 bytes of {pdf} — never stored]", pdf_b64)
            assert restored == self._canonical(sent)

    def test_an_unreadable_file_produces_a_note_and_no_media_entry(self, tmp_path):
        seen = []
        backend = OpenAICompatBackend(FakeClient(_completion(content="ok")), "openai")
        with backends.observe_sent(seen.append):
            backend(model="m", messages=[
                {"role": "user", "content": "look", "images": [str(tmp_path / "gone.png")]}
            ])
        (request,) = seen
        assert request.media == [[]]
        stored = backends.without_media(request.payload["messages"][0], [])
        assert stored == request.payload["messages"][0]

    def test_the_pass_through_shape_is_the_call_itself(self):
        kwargs = dict(model="m", messages=self.HISTORY, tools=self.TOOLS,
                      options={"num_ctx": 1}, think=False, stream=True)
        request = backends.passthrough_request("ollama", kwargs)
        assert request.provider == "ollama"
        assert "stream" not in request.payload
        assert request.payload["options"] == {"num_ctx": 1}
        assert all("_stub" not in m for m in request.payload["messages"])
        assert request.origins == list(range(len(self.HISTORY)))

    def test_nothing_is_reported_off_the_observing_thread(self):
        """Thread-local, like the governor's hooks: a title call on another
        thread must not land in this call's record."""
        import threading

        seen = []
        backend = OpenAICompatBackend(FakeClient(_completion(content="ok")), "openai")

        def elsewhere():
            backend(model="m", messages=[{"role": "user", "content": "title?"}])

        with backends.observe_sent(seen.append):
            worker = threading.Thread(target=elsewhere)
            worker.start()
            worker.join()
        assert seen == []

    def test_the_declared_system_policy_still_matches_the_traced_converter(self):
        """`convert_messages` is now a view over the traced converter; the
        pinned policy table must keep describing it."""
        out = convert_messages(self.HISTORY)
        assert [m["role"] for m in out[:2]] == ["system", "user"]
        assert backends.system_role_policy("openai") == "first_only"
        system, out = convert_messages_anthropic(self.HISTORY)
        assert system.startswith("base")
        assert backends.system_role_policy("claude") == "hoisted"
