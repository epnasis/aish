"""Model backends: route --model strings to a chat callable.

Every backend exposes the same calling convention as ollama.chat — the shape
Agent already speaks — so the agent loop never knows which provider it is on:

    chat(model=..., messages=[...], tools=[...], options={...}, think=..., stream=...)

returning (or yielding, when stream=True) objects with a .message
(.content / .tool_calls) and prompt_eval_count / eval_count usage fields.

Cloud providers are addressed with a provider prefix: ``gemini:<model>``,
``openai:<model>`` (bare ``gemini`` / ``openai`` picks that provider's
default model). Anything without a known prefix is an Ollama model, so all
existing invocations keep working unchanged.
"""

import base64
import json
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ToolFunction:
    name: str
    arguments: dict


@dataclass
class ToolCall:
    function: ToolFunction
    # Provider passthrough (e.g. Gemini thought signatures) that must be
    # echoed on the next request; Agent keeps it in history verbatim.
    extra_content: dict | None = None


@dataclass
class ChatMessage:
    content: str = ""
    tool_calls: list = field(default_factory=list)
    # Provider-native content blocks for the whole assistant turn (Anthropic:
    # thinking + text + tool_use). Agent stores them; the backend echoes them
    # verbatim next turn — required for thinking/tool-use continuations.
    raw_blocks: list | None = None


@dataclass
class ChatChunk:
    """One response (or stream chunk) in the shape Agent expects."""

    message: ChatMessage
    prompt_eval_count: int = 0
    eval_count: int = 0


@dataclass(frozen=True)
class Provider:
    name: str
    env_key: str
    base_url: str | None
    default_model: str
    key_url: str
    kind: str = "openai-compat"


PROVIDERS = {
    "gemini": Provider(
        name="gemini",
        env_key="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-3.5-flash",
        key_url="https://aistudio.google.com/apikey (free tier available)",
    ),
    "openai": Provider(
        name="openai",
        env_key="OPENAI_API_KEY",
        base_url=None,  # the openai SDK default
        default_model="gpt-5.6",
        key_url="https://platform.openai.com/api-keys",
    ),
    "claude": Provider(
        name="claude",
        env_key="ANTHROPIC_API_KEY",
        base_url=None,
        default_model="claude-opus-4-8",
        key_url="https://platform.claude.com/ (or `ant auth login`)",
        kind="anthropic",
    ),
}


class BackendError(RuntimeError):
    """Backend cannot be constructed (unknown provider, missing API key)."""


# What each provider's API accepts as native user-message media. Ollama is
# best-effort: the images key only helps on vision models (llava, qwen-vl,
# gemma3, …) — text-only models ignore it. Gemini's OpenAI-compat layer
# documents image data URLs but not file parts, so PDFs stay tool-territory
# there. claude-max runs a different agent loop entirely.
MEDIA_SUPPORT = {
    "ollama": frozenset({"image"}),
    "gemini": frozenset({"image"}),
    "openai": frozenset({"image", "pdf"}),
    "claude": frozenset({"image", "pdf"}),
}

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def media_support(provider_name: str) -> frozenset:
    return MEDIA_SUPPORT.get(provider_name, frozenset())


def _mime(path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _b64_file(path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def parse_model(model_arg: str) -> tuple[str, str]:
    """'gemini:foo' -> ('gemini', 'foo'); 'gemini' -> its default model;
    anything else -> ('ollama', <arg>)."""
    provider, sep, name = model_arg.partition(":")
    if sep and provider in PROVIDERS:
        return provider, name or PROVIDERS[provider].default_model
    if model_arg in PROVIDERS:
        return model_arg, PROVIDERS[model_arg].default_model
    return "ollama", model_arg


def make_chat(model_arg: str, client=None) -> tuple:
    """Resolve a --model string to (chat_callable, provider_name, model_name).

    ``client`` injects a pre-built provider client (tests)."""
    provider_name, model_name = parse_model(model_arg)
    if provider_name == "ollama":
        import ollama

        return ollama.chat, "ollama", model_name
    provider = PROVIDERS[provider_name]
    if provider.kind == "anthropic":
        if client is None:
            client = _anthropic_client(provider)
        return AnthropicBackend(client), provider_name, model_name
    if client is None:
        api_key = os.environ.get(provider.env_key, "").strip()
        if not api_key:
            raise BackendError(
                f"{provider.env_key} is not set — get a key at {provider.key_url} "
                f"and `export {provider.env_key}=...`"
            )
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise BackendError(
                "the 'openai' package is missing — reinstall aish "
                "(uv tool install --force --reinstall /path/to/aish)"
            ) from exc
        client = OpenAI(api_key=api_key, base_url=provider.base_url)
    return OpenAICompatBackend(client, provider_name), provider_name, model_name


def list_models(provider_name: str) -> list[str]:
    """Chat-capable model ids from a provider's list endpoint. Needs
    credentials (raises BackendError like make_chat); network errors
    propagate — callers treat any failure as 'catalog unavailable'."""
    provider = PROVIDERS[provider_name]
    if provider.kind == "anthropic":
        client = _anthropic_client(provider)
        return [m.id for m in client.models.list(limit=100)]
    api_key = os.environ.get(provider.env_key, "").strip()
    if not api_key:
        raise BackendError(f"{provider.env_key} is not set")
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=provider.base_url, timeout=10)
    ids = [m.id for m in client.models.list()]
    if provider_name == "gemini":
        ids = [i.removeprefix("models/") for i in ids]
        ids = [i for i in ids if i.startswith("gemini")]
    elif provider_name == "openai":
        # keep chat models; drop whisper/tts/dall-e/embeddings noise
        ids = [i for i in ids if i.startswith("gpt") or (i[:1] == "o" and i[1:2].isdigit())]
    return sorted(set(ids), reverse=True)  # newer version numbers first


def _anthropic_client(provider: Provider):
    # The anthropic SDK resolves credentials itself (ANTHROPIC_API_KEY,
    # ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile on disk) — only
    # fail fast when clearly none of those exist.
    has_creds = (
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        or (Path.home() / ".config" / "anthropic").exists()
    )
    if not has_creds:
        raise BackendError(
            f"no Anthropic credentials — get an API key at {provider.key_url} "
            "and `export ANTHROPIC_API_KEY=...`"
        )
    try:
        import anthropic
    except ModuleNotFoundError as exc:
        raise BackendError(
            "the 'anthropic' package is missing — reinstall aish "
            "(uv tool install --force --reinstall /path/to/aish)"
        ) from exc
    return anthropic.Anthropic()


class OpenAICompatBackend:
    """Chat-completions backend for any OpenAI-compatible API (OpenAI, Gemini)."""

    def __init__(self, client, provider_name: str):
        self.client = client
        self.provider = provider_name

    def __call__(
        self,
        *,
        model: str,
        messages: list,
        tools: list | None = None,
        options: dict | None = None,  # Ollama-only (num_ctx); ignored here
        think: bool = False,  # Ollama-only; cloud models manage reasoning themselves
        stream: bool = False,
    ):
        kwargs = dict(model=model, messages=convert_messages(messages))
        if tools:
            kwargs["tools"] = tools  # aish schemas are already OpenAI-format
        if stream:
            return self._stream(kwargs)
        response = self.client.chat.completions.create(**kwargs)
        return _from_completion(response)

    def _stream(self, kwargs: dict):
        try:
            chunks = self.client.chat.completions.create(
                stream=True, stream_options={"include_usage": True}, **kwargs
            )
        except Exception:
            # Some compatible servers reject stream_options — retry without.
            chunks = self.client.chat.completions.create(stream=True, **kwargs)
        # Tool-call fragments must be accumulated across chunks; only text
        # deltas are useful to the caller incrementally. OpenAI numbers
        # concurrent calls with an integer index; Gemini's compat layer sends
        # index=None and one complete call per fragment, distinguished by id.
        # Key slots on whichever is present, else the two calls of a parallel
        # turn merge into one garbage call ("read_urlread_url").
        pending: dict[tuple, dict] = {}
        usage = (0, 0)
        for chunk in chunks:
            if getattr(chunk, "usage", None):
                usage = (chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                yield ChatChunk(message=ChatMessage(content=delta.content))
            for frag in delta.tool_calls or []:
                index = getattr(frag, "index", None)
                if index is not None:
                    key = ("index", index)
                elif getattr(frag, "id", None):
                    key = ("id", frag.id)
                else:  # no index, no id: continuation of the latest call
                    key = next(reversed(pending), ("index", 0))
                slot = pending.setdefault(key, {"name": "", "arguments": "", "extra": None})
                if frag.function:
                    slot["name"] += frag.function.name or ""
                    slot["arguments"] += frag.function.arguments or ""
                slot["extra"] = _extra_content(frag) or slot["extra"]
        # Insertion order is arrival order, which is the call order for
        # every provider; mixed key types make sorting impossible anyway.
        tool_calls = [
            ToolCall(
                ToolFunction(name=slot["name"], arguments=_parse_args(slot["arguments"])),
                extra_content=slot["extra"],
            )
            for slot in pending.values()
        ]
        yield ChatChunk(
            message=ChatMessage(tool_calls=tool_calls),
            prompt_eval_count=usage[0],
            eval_count=usage[1],
        )


def convert_messages(messages: list[dict]) -> list[dict]:
    """aish/Ollama-style history -> OpenAI chat format.

    Ollama has no tool-call IDs, so synthetic IDs are minted per assistant
    message and handed out in order to the tool messages that follow — aish
    always appends tool results in call order, so positional pairing is exact.
    """
    out: list[dict] = []
    pending_ids: list[str] = []
    for i, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content") or ""
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            pending_ids = []
            for j, call in enumerate(message["tool_calls"]):
                function = call.get("function", {})
                call_id = f"call_{i}_{j}"
                pending_ids.append(call_id)
                entry_call = {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": function.get("name", ""),
                        "arguments": json.dumps(function.get("arguments") or {}),
                    },
                }
                if call.get("extra_content"):
                    entry_call["extra_content"] = call["extra_content"]
                calls.append(entry_call)
            entry = {"role": "assistant", "tool_calls": calls}
            if content:
                entry["content"] = content
            out.append(entry)
        elif role == "tool":
            if pending_ids:
                out.append(
                    {"role": "tool", "tool_call_id": pending_ids.pop(0), "content": content}
                )
            else:
                # Orphaned tool output (e.g. hand-edited history): keep the
                # information without breaking the API's id pairing rules.
                name = message.get("tool_name", "tool")
                out.append({"role": "user", "content": f"[{name} result]\n{content}"})
        elif role == "user" and (message.get("images") or message.get("documents")):
            out.append({"role": "user", "content": _openai_media_parts(message)})
        else:
            out.append({"role": role, "content": content})
    return out


def _openai_media_parts(message: dict) -> list[dict]:
    """User text + attached media as OpenAI content parts (data URLs). An
    unreadable file degrades to a text note instead of failing the call."""
    parts: list[dict] = []
    content = message.get("content") or ""
    if content:
        parts.append({"type": "text", "text": content})
    for path in message.get("images") or []:
        try:
            url = f"data:{_mime(path)};base64,{_b64_file(path)}"
        except OSError:
            parts.append({"type": "text", "text": f"[attachment unavailable: {path}]"})
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})
    for path in message.get("documents") or []:
        try:
            data = f"data:application/pdf;base64,{_b64_file(path)}"
        except OSError:
            parts.append({"type": "text", "text": f"[attachment unavailable: {path}]"})
            continue
        parts.append(
            {"type": "file", "file": {"filename": os.path.basename(path), "file_data": data}}
        )
    return parts


def _parse_args(raw: str) -> dict:
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extra_content(tool_call) -> dict | None:
    """Provider extensions on a tool call (Gemini: extra_content.google.
    thought_signature). The openai SDK parses unknown fields into pydantic
    extras, so plain attribute access works; fall back to model_extra."""
    extra = getattr(tool_call, "extra_content", None)
    if extra is None:
        model_extra = getattr(tool_call, "model_extra", None)
        if isinstance(model_extra, dict):
            extra = model_extra.get("extra_content")
    if hasattr(extra, "model_dump"):
        extra = extra.model_dump()
    return extra if isinstance(extra, dict) else None


def _from_completion(response) -> ChatChunk:
    message = response.choices[0].message
    tool_calls = [
        ToolCall(
            ToolFunction(name=tc.function.name, arguments=_parse_args(tc.function.arguments)),
            extra_content=_extra_content(tc),
        )
        for tc in (message.tool_calls or [])
    ]
    usage = getattr(response, "usage", None)
    return ChatChunk(
        message=ChatMessage(content=message.content or "", tool_calls=tool_calls),
        prompt_eval_count=(usage.prompt_tokens or 0) if usage else 0,
        eval_count=(usage.completion_tokens or 0) if usage else 0,
    )


# --------------------------------------------------------------- Anthropic


class AnthropicBackend:
    """Native Messages-API backend for Claude (API key / `ant auth login`)."""

    MAX_TOKENS = 16000
    MAX_TOKENS_STREAM = 64000

    def __init__(self, client):
        self.client = client

    def _request(self, *, model, messages, tools, think, max_tokens) -> dict:
        system, converted = convert_messages_anthropic(messages)
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            messages=converted,
            # Auto-cache the growing conversation prefix: agent loops resend
            # the whole history every turn, so cache reads cut cost sharply.
            cache_control={"type": "ephemeral"},
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = anthropic_tools(tools)
        if think:
            kwargs["thinking"] = {"type": "adaptive"}
        return kwargs

    def __call__(
        self,
        *,
        model: str,
        messages: list,
        tools: list | None = None,
        options: dict | None = None,  # Ollama-only; ignored
        think: bool = False,
        stream: bool = False,
    ):
        if stream:
            return self._stream(
                self._request(
                    model=model, messages=messages, tools=tools, think=think,
                    max_tokens=self.MAX_TOKENS_STREAM,
                )
            )
        response = self.client.messages.create(
            **self._request(
                model=model, messages=messages, tools=tools, think=think,
                max_tokens=self.MAX_TOKENS,
            )
        )
        return _from_anthropic(response)

    def _stream(self, kwargs: dict):
        with self.client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield ChatChunk(message=ChatMessage(content=text))
            final = stream.get_final_message()
        done = _from_anthropic(final)
        done.message.content = ""  # text already streamed above
        yield done


def anthropic_tools(schemas: list[dict]) -> list[dict]:
    """OpenAI-style function schemas -> Anthropic tool definitions."""
    out = []
    for schema in schemas:
        function = schema.get("function", {})
        out.append(
            {
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return out


def convert_messages_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """aish history -> (system prompt, Anthropic messages).

    Assistant turns produced by this backend carry ``raw_blocks`` — the
    provider's own content blocks (thinking + text + tool_use) — which are
    echoed verbatim, as the API requires for thinking/tool-use continuations.
    Tool results attach to the real tool_use IDs from those blocks; turns
    without raw blocks (imported histories) get synthetic IDs instead.
    """
    system_parts: list[str] = []
    out: list[dict] = []
    pending_ids: list[str] = []
    for i, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content") or ""
        if role == "system":
            system_parts.append(content)
        elif role == "assistant" and message.get("raw_blocks"):
            out.append({"role": "assistant", "content": message["raw_blocks"]})
            pending_ids = [
                block.get("id", "")
                for block in message["raw_blocks"]
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
        elif role == "assistant" and message.get("tool_calls"):
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": content})
            pending_ids = []
            for j, call in enumerate(message["tool_calls"]):
                function = call.get("function", {})
                call_id = f"call_{i}_{j}"
                pending_ids.append(call_id)
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": function.get("name", ""),
                        "input": function.get("arguments") or {},
                    }
                )
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            if pending_ids:
                block = {
                    "type": "tool_result",
                    "tool_use_id": pending_ids.pop(0),
                    "content": content,
                }
                # All results for one assistant turn must share one user
                # message — extend it if the previous entry already is one.
                last = out[-1] if out else None
                if (
                    last
                    and last["role"] == "user"
                    and isinstance(last["content"], list)
                    and last["content"]
                    and last["content"][-1].get("type") == "tool_result"
                ):
                    last["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                name = message.get("tool_name", "tool")
                out.append({"role": "user", "content": f"[{name} result]\n{content}"})
        elif role == "user" and (message.get("images") or message.get("documents")):
            out.append({"role": "user", "content": _anthropic_media_blocks(message)})
        elif content:  # user turns; skip empty messages — the API rejects them
            out.append({"role": role, "content": content})
    return "\n".join(system_parts), out


def _anthropic_media_blocks(message: dict) -> list[dict]:
    """User text + attached media as Anthropic content blocks. An unreadable
    file degrades to a text note instead of failing the call."""
    blocks: list[dict] = []
    for path in message.get("images") or []:
        try:
            source = {"type": "base64", "media_type": _mime(path), "data": _b64_file(path)}
        except OSError:
            blocks.append({"type": "text", "text": f"[attachment unavailable: {path}]"})
            continue
        blocks.append({"type": "image", "source": source})
    for path in message.get("documents") or []:
        try:
            source = {
                "type": "base64",
                "media_type": "application/pdf",
                "data": _b64_file(path),
            }
        except OSError:
            blocks.append({"type": "text", "text": f"[attachment unavailable: {path}]"})
            continue
        blocks.append({"type": "document", "source": source})
    content = message.get("content") or ""
    if content:
        blocks.append({"type": "text", "text": content})
    return blocks


def _from_anthropic(response) -> ChatChunk:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    raw_blocks: list[dict] = []
    for block in response.content:
        raw_blocks.append(
            block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)
        )
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(block.text)
        elif block_type == "tool_use":
            tool_calls.append(
                ToolCall(ToolFunction(name=block.name, arguments=dict(block.input or {})))
            )
    content = "".join(text_parts)
    if not content and not tool_calls and getattr(response, "stop_reason", None) == "refusal":
        content = "(the model declined this request for safety reasons)"
    usage = getattr(response, "usage", None)
    prompt_tokens = 0
    if usage:
        prompt_tokens = (
            (usage.input_tokens or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        )
    return ChatChunk(
        message=ChatMessage(
            content=content,
            tool_calls=tool_calls,
            raw_blocks=raw_blocks or None,
        ),
        prompt_eval_count=prompt_tokens,
        eval_count=(usage.output_tokens or 0) if usage else 0,
    )
