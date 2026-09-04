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
import copy
import functools
import json
import mimetypes
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from . import ratelimit

# --------------------------------------------------------- the sent seam
#
# The request a model call sends exists only inside the adapter, AFTER
# conversion: Anthropic hoists every system message into a top-level param and
# merges tool results into one user message, the OpenAI shape relabels later
# system messages as `user`, ollama passes the aish shape through. A manifest
# taken from `agent.messages` would be false about role and cardinality on
# three of the four (#352). So each adapter hands the request it is about to
# send — exactly the keyword arguments the client receives, minus transport
# flags — to whoever is observing this thread, and the agent writes the `sent`
# record from that. Thread-local for the same reason `ratelimit.hooks` is: a
# keyword added to the `ollama.chat` convention for the recorder's benefit
# would break the one invariant that keeps the backends interchangeable.


class SentRequest(NamedTuple):
    """One request as the provider will see it."""

    provider: str
    # The keyword arguments handed to the provider client, `stream` and
    # `stream_options` excluded — those decide how the answer arrives, not
    # what was asked.
    payload: dict
    # Per provider message: the aish-side index it came from (an int), or the
    # list of indices where the adapter merged several into one.
    origins: list
    # aish-side indices hoisted into a top-level `system` parameter (Anthropic);
    # empty elsewhere.
    system_origins: list
    # Per provider message: [(path, bytes)] for each base64 block the adapter
    # encoded into it, in the order the blocks appear. The bytes themselves are
    # never stored; the manifest names the file and its size instead.
    media: list


_SENT = threading.local()


class observe_sent:  # noqa: N801 — used as a context manager, reads as one
    """Receive every `SentRequest` an adapter reports on this thread."""

    def __init__(self, callback: Callable[[SentRequest], None]):
        self._callback = callback
        self._previous: Callable | None = None

    def __enter__(self) -> None:
        self._previous = getattr(_SENT, "current", None)
        _SENT.current = self._callback

    def __exit__(self, *_exc) -> None:
        _SENT.current = self._previous


def _report_sent(request: SentRequest) -> None:
    callback = getattr(_SENT, "current", None)
    if callback is not None:
        callback(request)


def passthrough_request(provider: str, kwargs: dict) -> SentRequest:
    """The request for a backend that takes the aish shape as it is (ollama).

    There is no conversion to capture after, so the seam is the call itself:
    what the ollama library is handed is what this records, and the library
    does its own encoding of `images` file paths after this point. Keys the
    agent keeps on a message for its own bookkeeping (`_stub`) are dropped
    here because the library drops them too — pydantic ignores unknown fields
    — so the record holds what reached the provider, not what aish carried.
    """
    payload = {k: v for k, v in kwargs.items() if k not in ("stream", "stream_options")}
    messages = []
    media: list[list[tuple[str, int]]] = []
    for message in payload.get("messages") or []:
        messages.append({k: v for k, v in message.items() if not str(k).startswith("_")})
        entries: list[tuple[str, int]] = []
        for path in list(message.get("images") or []) + list(message.get("documents") or []):
            try:
                entries.append((str(path), os.path.getsize(path)))
            except OSError:
                entries.append((str(path), -1))
        media.append(entries)
    payload["messages"] = messages
    return SentRequest(provider, payload, list(range(len(messages))), [], media)


MEDIA_PLACEHOLDER = "[aish: {bytes} bytes of {path} — never stored]"
MEDIA_PLACEHOLDER_UNKNOWN = "[aish: base64 media — never stored]"


def without_media(message: dict, media: list[tuple[str, int]]) -> dict:
    """A copy of one provider message with every base64 payload replaced by a
    placeholder naming the file and its size — the fourth reader state, *never
    stored*, distinct from purged. The k-th base64 string found walking the
    message pairs with the k-th media entry: both adapters emit blocks in the
    order they encoded them, and an unreadable file emits a text note and no
    entry, so the two sequences line up by construction.
    """
    entries = list(media)
    stored = copy.deepcopy(message)

    def placeholder() -> str:
        if not entries:
            return MEDIA_PLACEHOLDER_UNKNOWN
        path, size = entries.pop(0)
        return MEDIA_PLACEHOLDER.format(path=path, bytes=size)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and _is_base64_payload(key, value, node):
                    # Only the base64 itself goes: a data URL keeps its
                    # `data:<mime>;base64,` head, so the stored blob still
                    # says what KIND of thing was there.
                    head, sep, _payload = value.partition(";base64,")
                    node[key] = f"{head}{sep}{placeholder()}" if sep else placeholder()
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(stored)
    return stored


def _is_base64_payload(key: str, value: str, holder: dict) -> bool:
    if key == "data" and holder.get("type") == "base64":
        return True  # Anthropic `source` block
    if key in ("url", "file_data") and value.startswith("data:") and ";base64," in value:
        return True  # OpenAI data URL parts
    return False


@dataclass
class ToolFunction:
    name: str
    arguments: dict
    # Did the provider's argument JSON parse? A decode failure yields `{}`,
    # which is indistinguishable downstream from a call the model deliberately
    # made with no arguments — and the two route to completely different
    # repairs (#240). The raw string dies here, so the fact has to be recorded
    # here too.
    malformed: bool = False


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
    # The model's reasoning text, when the provider exposes it (Anthropic
    # thinking blocks; Ollama's Message.thinking). Display-only — never echoed
    # back to the API (raw_blocks carries the canonical copy for Anthropic).
    thinking: str = ""
    # Why the provider stopped (Anthropic stop_reason, OpenAI finish_reason).
    # A turn cut off at the token limit and a turn that finished are the same
    # shape downstream without this (#240).
    stop: str = ""
    # True when `content` is aish's sentence, not the model's. Without it a
    # dossier attributes the harness's own words to the model.
    synthesized: bool = False


@dataclass
class ChatChunk:
    """One response (or stream chunk) in the shape Agent expects."""

    message: ChatMessage
    prompt_eval_count: int = 0
    eval_count: int = 0
    # The provider's usage report, with its units intact (#262). See
    # `usage_detail` for why collapsing it to the two ints above loses the
    # only fact about cost that cannot be recovered later.
    usage: dict | None = None


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


# The provider SDKs retry 429/5xx themselves, with their own backoff, silently,
# INSIDE one call — openai and anthropic both default to 2 extra attempts. That
# made aish's retry policy a fiction stacked on an invisible one: one visible
# "retrying once…" was up to six HTTP requests, none of which aish could see,
# classify, pace or record, and most 429s never surfaced to any code that could
# have recorded them (#261). aish owns the retry policy now — `ratelimit.py` for
# the classification, `agent._chat_turn` for the loop — so there must be exactly
# one of them. `docs/rate-limits.md`.
SDK_RETRIES = 0


# What a provider's "input tokens" number MEANS. Not decoration: the same field
# name carries three different units across the three backends, so a daily total
# that sums them without this flag is adding incompatible things (#262).
#
#   - OpenAI-shaped (incl. Gemini's compat layer): `prompt_tokens` INCLUDES
#     cached tokens; the cached subset is reported separately.
#   - Anthropic: `input_tokens` EXCLUDES cache reads and cache writes, which are
#     their own fields and bill at different rates (a cache read is ~10% of base
#     input, so collapsing them cannot distinguish a 1M-token turn that cost 1M
#     from one that cost 100k-equivalent).
#   - Ollama: `prompt_eval_count` EXCLUDES tokens served from KV-cache reuse.
INPUT_INCLUDES_CACHE = "input_includes_cache"
INPUT_EXCLUDES_CACHE = "input_excludes_cache"
INPUT_EXCLUDES_KV_REUSE = "input_excludes_kv_reuse"


def usage_detail(semantics: str, **counts: int) -> dict:
    """The provider's usage report, verbatim, labelled with its own units.

    This exists because the collapse was lossy in a way nothing downstream could
    undo. Every backend flattened its report into two ints, so the cache split —
    the single biggest determinant of what a turn actually cost, and the thing
    an agent loop that resends its whole history every step lives or dies on —
    was discarded at the adapter and could only ever be re-derived from the
    provider's documentation as it reads TODAY. That is evidence that decays:
    providers change what they report and how they bill it, and a log written
    last month cannot be reinterpreted once they do.

    Zero-valued counts are dropped. A provider that does not report cache reads
    and a turn that had none are different facts, and only the absent key can
    tell them apart.
    """
    detail: dict = {"semantics": semantics}
    detail.update({name: int(value) for name, value in counts.items() if value})
    return detail


# Real input context per provider, in TOKENS (#192). This exists because every
# cap in aish derived from `num_ctx`, which is an OLLAMA-ONLY option the cloud
# backends accept and DISCARD — so on a Gemini-1M session both the history
# budget and the plugin output cap were fiction, sized for a context window
# that was not the one in use.
#
# Deliberately per-PROVIDER, not per-model, and deliberately conservative:
# being wrong-low costs some truncation headroom, being wrong-high silently
# overruns a real request. A provider absent from the table falls back to the
# floor rather than to optimism.
CONTEXT_WINDOWS = {
    "gemini": 1_048_576,
    "claude": 200_000,
    "claude-max": 200_000,  # the SDK/CLI login path (claude_max.py), same models
    "openai": 128_000,  # conservative floor for the GPT-5 line, not a measurement
}
DEFAULT_CONTEXT_WINDOW = 128_000


def context_window(provider_name: str, num_ctx: int = 0) -> tuple[int, str]:
    """(tokens, provenance) for the context window actually in force.

    Provenance is recorded alongside the number wherever a cap is logged
    (contract §3.4 `truncation.cap_source`): a log that records a cap but not
    where it came from cannot show whether #192's "size it from the real
    backend" claim actually landed.
    """
    if provider_name == "ollama":
        # For Ollama num_ctx IS the real window — it is the option the server
        # is launched with, not a number we hope applies.
        return num_ctx, f"num_ctx:{num_ctx}"
    window = CONTEXT_WINDOWS.get(provider_name, DEFAULT_CONTEXT_WINDOW)
    return window, f"backend:{provider_name}:{window}"


# How each provider carries aish's SECOND system message — the per-task
# reminder holding the knowledge index, the preloaded skills and the rule prose.
# "first_only" means it reaches the model relabelled as a USER message.
#
# Declared here rather than inferred by a reader, because a dossier that says
# "a system-authority instruction was in force" when the model received an
# ordinary user message is exactly the confident-wrong-conclusion this record
# set exists to prevent (#241). `test_declared_system_policy_matches_the_code`
# pins these values against what the converters actually do, so the two cannot
# drift apart silently.
SYSTEM_ROLE_POLICY = {
    "gemini": "first_only",  # #74: the compat gateway drops ALL system messages when >1
    "openai": "first_only",
    "claude": "hoisted",  # every system message is hoisted into the `system` parameter
    "ollama": "all_system",
}
DEFAULT_SYSTEM_ROLE_POLICY = "first_only"  # every other provider goes through convert_messages


def system_role_policy(provider_name: str) -> str:
    """How this provider carries the per-task system reminder, as a declared
    fact recorded with the turn it governed."""
    return SYSTEM_ROLE_POLICY.get(provider_name, DEFAULT_SYSTEM_ROLE_POLICY)


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
    return _b64_file_sized(path)[0]


def _b64_file_sized(path) -> tuple[str, int]:
    """(base64, size in bytes) — the size travels to the `sent` manifest in
    place of the bytes, which are never stored."""
    raw = Path(path).read_bytes()
    return base64.b64encode(raw).decode("ascii"), len(raw)


def parse_model(model_arg: str) -> tuple[str, str]:
    """'gemini:foo' -> ('gemini', 'foo'); 'gemini' -> its default model;
    anything else -> ('ollama', <arg>)."""
    provider, sep, name = model_arg.partition(":")
    if sep and provider in PROVIDERS:
        return provider, name or PROVIDERS[provider].default_model
    if model_arg in PROVIDERS:
        return model_arg, PROVIDERS[model_arg].default_model
    return "ollama", model_arg


def make_chat(model_arg: str, client=None) -> tuple[Callable, str, str]:
    """Resolve a --model string to (chat_callable, provider_name, model_name).

    ``client`` injects a pre-built provider client (tests)."""
    provider_name, model_name = parse_model(model_arg)
    if provider_name == "ollama":
        import ollama

        return governed(ollama.chat, "ollama"), "ollama", model_name
    provider = PROVIDERS[provider_name]
    if provider.kind == "anthropic":
        if client is None:
            client = _anthropic_client(provider)
        anthropic = AnthropicBackend(client, provider_name)
        return governed(anthropic, provider_name), provider_name, model_name
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
        client = OpenAI(api_key=api_key, base_url=provider.base_url, max_retries=SDK_RETRIES)
    backend = OpenAICompatBackend(client, provider_name)
    return governed(backend, provider_name), provider_name, model_name


def governed(chat: Callable, provider: str) -> Callable:
    """Wrap a chat callable so every call it makes is paced and every refusal it
    hits is learned from.

    HERE, and not in `agent._chat_turn`, because this is the only in-process
    chokepoint that EVERY consumer of an API key traverses. The agent loop is
    one of them; `server._model_session_title` is another — it calls
    `agent.chat` directly, outside `run_task`, after every eligible completed
    turn, and its `except Exception: return None` swallowed the 429 with no
    record at all. Governing the loop alone would leave the shared quota
    governed at one of its several consumers, which is the shape of the bug
    rather than a fix for it.

    The `ollama.chat` calling convention is preserved exactly — same keywords,
    same return type, streaming still a generator — so nothing downstream can
    tell it is wrapped. `ratelimit.hooks()` is how a caller supplies the
    cancel/status wiring that does not belong in a calling convention.
    """

    @functools.wraps(chat)
    def call(*, model: str = "", messages: list | None = None, stream: bool = False, **kw):
        key = f"{provider}:{model}"
        ticket = ratelimit.reserve_for_call(key, messages)
        try:
            result = chat(model=model, messages=messages, stream=stream, **kw)
        except Exception as exc:  # noqa: BLE001 — observed, then re-raised as-is
            _settle_failure(key, ticket, exc)
            raise
        if not stream:
            ticket.settle(getattr(result, "prompt_eval_count", 0) or 0)
            return result
        return _governed_stream(result, key, ticket)

    return call


def _governed_stream(chunks, key: str, ticket):
    """A stream settles on its LAST chunk, because that is where the counts
    arrive. One that is abandoned part-way settles on nothing, deliberately:
    the estimate stands, because the provider may well have generated the whole
    response and charged for it."""
    try:
        last = None
        for chunk in chunks:
            last = chunk
            yield chunk
    except Exception as exc:  # noqa: BLE001 — observed, then re-raised as-is
        _settle_failure(key, ticket, exc)
        raise
    ticket.settle(getattr(last, "prompt_eval_count", 0) or 0)


def _settle_failure(key: str, ticket, exc: BaseException) -> None:
    failure = ratelimit.classify(exc)
    ratelimit.governor().observe(key, failure)
    if failure.is_rate_limit:
        # The request happened and plausibly counts against RPM, but tokens the
        # provider never processed should not crowd out the retry.
        ticket.rejected()
    else:
        ticket.settle(None)


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
    return anthropic.Anthropic(max_retries=SDK_RETRIES)


# Gemini's OpenAI-compat layer returns thought summaries only when asked, and
# returns them INSIDE content, delimited by these tags (probed against the live
# API 2026-07: streaming plain turns interleave `<thought>…</thought>` in the
# content deltas; non-streaming responses carry the same tags; streaming
# TOOL-CALL turns omit thoughts entirely — a compat-layer gap, so the trace
# header falls back to its deterministic tool line there).
GEMINI_THINKING_BODY = {
    "extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}
}
_THOUGHT_OPEN = "<thought>"
_THOUGHT_CLOSE = "</thought>"


class _ThoughtFilter:
    """Incrementally split Gemini's tagged content stream into (thinking,
    visible) text. Tags can split across deltas, so a trailing partial tag is
    held back until the next feed (or flushed at end of stream)."""

    def __init__(self):
        self._in_thought = False
        self._buf = ""

    def feed(self, text: str) -> tuple[str, str]:
        self._buf += text
        thinking: list[str] = []
        visible: list[str] = []
        while self._buf:
            tag = _THOUGHT_CLOSE if self._in_thought else _THOUGHT_OPEN
            out = thinking if self._in_thought else visible
            idx = self._buf.find(tag)
            if idx != -1:
                out.append(self._buf[: idx])
                self._buf = self._buf[idx + len(tag):]
                self._in_thought = not self._in_thought
                continue
            keep = self._partial_suffix(tag)
            cut = len(self._buf) - keep
            out.append(self._buf[:cut])
            self._buf = self._buf[cut:]
            break
        return "".join(thinking), "".join(visible)

    def _partial_suffix(self, tag: str) -> int:
        for k in range(min(len(tag) - 1, len(self._buf)), 0, -1):
            if self._buf.endswith(tag[:k]):
                return k
        return 0

    def flush(self) -> tuple[str, str]:
        """End of stream: held-back text was not a tag after all."""
        buf, self._buf = self._buf, ""
        return (buf, "") if self._in_thought else ("", buf)


def split_thoughts(text: str) -> tuple[str, str]:
    """(thinking, visible) split of a complete tagged response."""
    f = _ThoughtFilter()
    thinking, visible = f.feed(text)
    tail_t, tail_v = f.flush()
    return thinking + tail_t, visible + tail_v


def _rejects_stream_options(exc: BaseException) -> bool:
    """True only for "I do not know the field `stream_options`".

    Deliberately narrow and deliberately NOT status-only: retrying anything
    else here is a second uncoordinated attempt at the provider's expense, and
    for a rate limit it is an attempt spent on the thing that ran out.
    """
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status is not None and not (400 <= int(status) < 500):
        return False
    if status == 429:  # a quota failure is never a schema complaint
        return False
    return "stream_options" in f"{exc} {getattr(exc, 'body', '') or ''}"


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
        converted, origins, media = _convert_messages_traced(messages)
        kwargs: dict[str, Any] = dict(model=model, messages=converted)
        if tools:
            kwargs["tools"] = tools  # aish schemas are already OpenAI-format
        if self.provider == "gemini":
            # Surface thought summaries so the trace can show what the model
            # is thinking; the tagged text is split out of content below and
            # never reaches history or the rendered answer.
            kwargs["extra_body"] = GEMINI_THINKING_BODY
        # The seam (#352): what goes to the client is what is reported, and
        # it is reported on every attempt so a retry that changed nothing
        # still hands the agent the request the model actually received.
        _report_sent(SentRequest(self.provider, dict(kwargs), origins, [], media))
        if stream:
            return self._stream(kwargs)
        response = self.client.chat.completions.create(**kwargs)
        chunk = _from_completion(response)
        if self.provider == "gemini" and chunk.message.content:
            thinking, visible = split_thoughts(chunk.message.content)
            chunk.message.content = visible
            chunk.message.thinking = thinking
        return chunk

    def _stream(self, kwargs: dict):
        try:
            chunks = self.client.chat.completions.create(
                stream=True, stream_options={"include_usage": True}, **kwargs
            )
        except Exception as exc:
            # Some compatible servers reject stream_options — retry without.
            #
            # The catch used to be bare, which made this a THIRD retry layer and
            # the least visible one (#261): a 429 raised at create() took this
            # branch too, so the request was re-sent in full before aish's own
            # loop or the SDK's had any say. On the streaming (web) path that
            # multiplied out to a dozen ~120k-token requests per one visible
            # "retrying once…" — against the quota that had just run out.
            #
            # So it is narrowed to the one failure it was written for, and the
            # test is the argument name in the error rather than the status
            # alone: a gateway may reject an unknown field as 400, 404 or 422,
            # but it always says which field.
            if not _rejects_stream_options(exc):
                raise
            chunks = self.client.chat.completions.create(stream=True, **kwargs)
        # Tool-call fragments must be accumulated across chunks; only text
        # deltas are useful to the caller incrementally. OpenAI numbers
        # concurrent calls with an integer index; Gemini's compat layer sends
        # index=None and one complete call per fragment, distinguished by id.
        # Key slots on whichever is present, else the two calls of a parallel
        # turn merge into one garbage call ("read_urlread_url").
        pending: dict[tuple, dict] = {}
        usage = (0, 0)
        detail: dict | None = None
        thoughts = _ThoughtFilter() if self.provider == "gemini" else None
        for chunk in chunks:
            if getattr(chunk, "usage", None):
                usage = (chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0)
                detail = _openai_usage(chunk.usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                if thoughts is not None:
                    thinking, visible = thoughts.feed(delta.content)
                    if thinking or visible:
                        yield ChatChunk(
                            message=ChatMessage(content=visible, thinking=thinking)
                        )
                else:
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
                _tool_function(slot["name"], slot["arguments"]),
                extra_content=slot["extra"],
            )
            for slot in pending.values()
        ]
        tail_thinking, tail_visible = thoughts.flush() if thoughts is not None else ("", "")
        yield ChatChunk(
            message=ChatMessage(
                content=tail_visible, tool_calls=tool_calls, thinking=tail_thinking
            ),
            prompt_eval_count=usage[0],
            eval_count=usage[1],
            usage=detail,
        )


def convert_messages(messages: list[dict]) -> list[dict]:
    """aish/Ollama-style history -> OpenAI chat format.

    Ollama has no tool-call IDs, so synthetic IDs are minted per assistant
    message and handed out in order to the tool messages that follow — aish
    always appends tool results in call order, so positional pairing is exact.

    aish injects a second ``system`` message (the per-task reminder) directly
    before each user turn for recency. Gemini's OpenAI-compat gateway drops ALL
    system instructions when more than one system message is present (issue
    #74), so only the first system message is kept as ``system``; later ones
    are relabelled ``user``. Their content is already ``<system-reminder>``
    tagged, so recency and instruction-ness survive the relabel.
    """
    return _convert_messages_traced(messages)[0]


def _convert_messages_traced(messages: list[dict]) -> tuple[list[dict], list, list]:
    """`convert_messages`, plus where each output message came from and which
    files were encoded into it — the two things the `sent` record needs and
    the plain converter's return shape has no room for. This conversion is
    one-to-one, so every origin is a single index."""
    out: list[dict] = []
    media: list[list[tuple[str, int]]] = []
    pending_ids: list[str] = []
    seen_system = False
    for i, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content") or ""
        media.append([])
        if role == "system":
            if seen_system:
                out.append({"role": "user", "content": content})
            else:
                seen_system = True
                out.append({"role": "system", "content": content})
        elif role == "assistant" and message.get("tool_calls"):
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
            parts, encoded = _openai_media_parts(message)
            media[-1] = encoded
            out.append({"role": "user", "content": parts})
        else:
            out.append({"role": role, "content": content})
    return out, list(range(len(out))), media


def _openai_media_parts(message: dict) -> tuple[list[dict], list[tuple[str, int]]]:
    """User text + attached media as OpenAI content parts (data URLs), and the
    (path, bytes) of each file that was actually encoded. An unreadable file
    degrades to a text note instead of failing the call, and produces no
    entry — so the entries pair with the data URLs one to one."""
    parts: list[dict] = []
    encoded: list[tuple[str, int]] = []
    content = message.get("content") or ""
    if content:
        parts.append({"type": "text", "text": content})
    for path in message.get("images") or []:
        try:
            data, size = _b64_file_sized(path)
        except OSError:
            parts.append({"type": "text", "text": f"[attachment unavailable: {path}]"})
            continue
        url = f"data:{_mime(path)};base64,{data}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
        encoded.append((str(path), size))
    for path in message.get("documents") or []:
        try:
            data, size = _b64_file_sized(path)
        except OSError:
            parts.append({"type": "text", "text": f"[attachment unavailable: {path}]"})
            continue
        parts.append(
            {
                "type": "file",
                "file": {
                    "filename": os.path.basename(path),
                    "file_data": f"data:application/pdf;base64,{data}",
                },
            }
        )
        encoded.append((str(path), size))
    return parts, encoded


def _parse_args(raw: str) -> tuple[dict, bool]:
    """(arguments, malformed). Malformed means the model emitted something that
    is not a JSON object — a truncated blob, prose, a bare list. It still runs
    as `{}` because refusing the whole turn is worse, but the log must not say
    the model called the tool with no arguments when it did not."""
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}, True
    return (parsed, False) if isinstance(parsed, dict) else ({}, True)


def _tool_function(name: str, raw: str) -> ToolFunction:
    """The one place a provider's argument string becomes a ToolFunction, so
    the malformed flag cannot be dropped at one call site and kept at another."""
    arguments, malformed = _parse_args(raw)
    return ToolFunction(name=name, arguments=arguments, malformed=malformed)


def _extra_content(tool_call) -> dict | None:
    """Provider extensions on a tool call (Gemini: extra_content.google.
    thought_signature). The openai SDK parses unknown fields into pydantic
    extras, so plain attribute access works; fall back to model_extra."""
    extra = getattr(tool_call, "extra_content", None)
    if extra is None:
        model_extra = getattr(tool_call, "model_extra", None)
        if isinstance(model_extra, dict):
            extra = model_extra.get("extra_content")
    if extra is not None and hasattr(extra, "model_dump"):
        extra = extra.model_dump()
    return extra if isinstance(extra, dict) else None


def _from_completion(response) -> ChatChunk:
    choice = response.choices[0]
    message = choice.message
    tool_calls = [
        ToolCall(
            _tool_function(tc.function.name, tc.function.arguments),
            extra_content=_extra_content(tc),
        )
        for tc in (message.tool_calls or [])
    ]
    usage = getattr(response, "usage", None)
    return ChatChunk(
        message=ChatMessage(
            content=message.content or "",
            tool_calls=tool_calls,
            stop=str(getattr(choice, "finish_reason", "") or ""),
        ),
        prompt_eval_count=(usage.prompt_tokens or 0) if usage else 0,
        eval_count=(usage.completion_tokens or 0) if usage else 0,
        usage=_openai_usage(usage),
    )


def _openai_usage(usage: Any) -> dict | None:
    """`prompt_tokens` is the TOTAL and `cached` is the subset of it that was
    served from the provider's prompt cache — the number that decides whether a
    120k-token resend was expensive or nearly free."""
    if not usage:
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    return usage_detail(
        INPUT_INCLUDES_CACHE,
        input=usage.prompt_tokens or 0,
        cached=(getattr(details, "cached_tokens", 0) or 0) if details else 0,
        output=usage.completion_tokens or 0,
    )


# --------------------------------------------------------------- Anthropic


class AnthropicBackend:
    """Native Messages-API backend for Claude (API key / `ant auth login`)."""

    MAX_TOKENS = 16000
    MAX_TOKENS_STREAM = 64000

    def __init__(self, client, provider_name: str = "claude"):
        self.client = client
        self.provider = provider_name

    def _request(self, *, model, messages, tools, think, max_tokens) -> dict:
        system, converted, system_origins, origins, media = _convert_anthropic_traced(messages)
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
        # The seam (#352): reported here because this is the one place both
        # the streaming and the plain path build the request.
        _report_sent(SentRequest(self.provider, dict(kwargs), origins, system_origins, media))
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
    system, out, _system_origins, _origins, _media = _convert_anthropic_traced(messages)
    return system, out


def _convert_anthropic_traced(
    messages: list[dict],
) -> tuple[str, list[dict], list[int], list, list]:
    """`convert_messages_anthropic`, plus the provenance the `sent` record
    needs: which aish-side messages were hoisted into `system`, which went
    into each output message (a LIST where several were merged — tool results
    sharing one user message, a picture joining the results it belongs to),
    and which files were encoded into each. Empty messages the API rejects
    are dropped here and so have no output to be the origin of."""
    system_parts: list[str] = []
    system_origins: list[int] = []
    out: list[dict] = []
    origins: list[list[int]] = []
    media: list[list[tuple[str, int]]] = []
    pending_ids: list[str] = []
    for i, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content") or ""
        if role == "system":
            system_parts.append(content)
            system_origins.append(i)
        elif role == "assistant" and message.get("raw_blocks"):
            out.append({"role": "assistant", "content": message["raw_blocks"]})
            origins.append([i])
            media.append([])
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
            origins.append([i])
            media.append([])
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
                    origins[-1].append(i)
                else:
                    out.append({"role": "user", "content": [block]})
                    origins.append([i])
                    media.append([])
            else:
                name = message.get("tool_name", "tool")
                out.append({"role": "user", "content": f"[{name} result]\n{content}"})
                origins.append([i])
                media.append([])
        elif role == "user" and (message.get("images") or message.get("documents")):
            blocks, encoded = _anthropic_media_blocks(message)
            # A tool-produced picture arrives as a user message immediately
            # after the tool results it belongs to (agent._deliver_tool_media),
            # and those results are themselves a user message here. Two user
            # entries in a row is not a shape this API takes, so the media
            # joins the entry that is already open rather than opening a second
            # one — which is also the truthful shape: same turn, same input.
            last = out[-1] if out else None
            if last and last["role"] == "user" and isinstance(last["content"], list):
                last["content"].extend(blocks)
                origins[-1].append(i)
                media[-1].extend(encoded)
            else:
                out.append({"role": "user", "content": blocks})
                origins.append([i])
                media.append(encoded)
        elif content:  # user turns; skip empty messages — the API rejects them
            out.append({"role": role, "content": content})
            origins.append([i])
            media.append([])
    flat: list = [group[0] if len(group) == 1 else group for group in origins]
    return "\n".join(system_parts), out, system_origins, flat, media


def _anthropic_media_blocks(message: dict) -> tuple[list[dict], list[tuple[str, int]]]:
    """User text + attached media as Anthropic content blocks, and the
    (path, bytes) of each file that was actually encoded. An unreadable file
    degrades to a text note instead of failing the call, and produces no
    entry — so the entries pair with the base64 blocks one to one."""
    blocks: list[dict] = []
    encoded: list[tuple[str, int]] = []
    for path in message.get("images") or []:
        try:
            data, size = _b64_file_sized(path)
        except OSError:
            blocks.append({"type": "text", "text": f"[attachment unavailable: {path}]"})
            continue
        blocks.append(
            {"type": "image", "source": {"type": "base64", "media_type": _mime(path), "data": data}}
        )
        encoded.append((str(path), size))
    for path in message.get("documents") or []:
        try:
            data, size = _b64_file_sized(path)
        except OSError:
            blocks.append({"type": "text", "text": f"[attachment unavailable: {path}]"})
            continue
        blocks.append(
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            }
        )
        encoded.append((str(path), size))
    content = message.get("content") or ""
    if content:
        blocks.append({"type": "text", "text": content})
    return blocks, encoded


def _from_anthropic(response) -> ChatChunk:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    raw_blocks: list[dict] = []
    for block in response.content:
        raw_blocks.append(
            block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)
        )
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(block.text)
        elif block_type == "thinking":
            thinking_parts.append(getattr(block, "thinking", "") or "")
        elif block_type == "tool_use":
            tool_calls.append(
                ToolCall(ToolFunction(name=block.name, arguments=dict(block.input or {})))
            )
    content = "".join(text_parts)
    stop = str(getattr(response, "stop_reason", "") or "")
    synthesized = False
    if not content and not tool_calls and stop == "refusal":
        content = "(the model declined this request for safety reasons)"
        # aish's sentence, not the model's. Recorded as such so a dossier can
        # never attribute the harness's words to the model (#240).
        synthesized = True
    usage = getattr(response, "usage", None)
    prompt_tokens = 0
    detail = None
    if usage:
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        prompt_tokens = (usage.input_tokens or 0) + cache_read + cache_write
        # The SUM stays, because everything downstream sizes context from it and
        # a cache read still occupies the window. What is new is that the three
        # parts survive alongside it: they bill at three different rates, and
        # the sum alone cannot tell a cheap turn from an expensive one (#262).
        detail = usage_detail(
            INPUT_EXCLUDES_CACHE,
            input=usage.input_tokens or 0,
            cache_read=cache_read,
            cache_write=cache_write,
            output=usage.output_tokens or 0,
        )
    return ChatChunk(
        message=ChatMessage(
            content=content,
            tool_calls=tool_calls,
            stop=stop,
            synthesized=synthesized,
            raw_blocks=raw_blocks or None,
            thinking="\n".join(part for part in thinking_parts if part),
        ),
        prompt_eval_count=prompt_tokens,
        eval_count=(usage.output_tokens or 0) if usage else 0,
        usage=detail,
    )
