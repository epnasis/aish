"""Backend routing, message conversion, and OpenAI-compat normalization."""

import json
from types import SimpleNamespace

import pytest

from aish import backends
from aish.backends import (
    AnthropicBackend,
    BackendError,
    OpenAICompatBackend,
    anthropic_tools,
    convert_messages,
    convert_messages_anthropic,
    make_chat,
    parse_model,
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
    assert isinstance(chat, OpenAICompatBackend)


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


def test_media_support_map():
    assert "image" in backends.media_support("ollama")
    assert "pdf" in backends.media_support("claude")
    assert "pdf" not in backends.media_support("gemini")
    assert backends.media_support("claude-max") == frozenset()
