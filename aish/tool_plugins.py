"""Plugin tools: droppable TOOL.md manifests the model calls exactly like a
native tool. A tool is a folder ``<name>/TOOL.md`` (plus an optional wrapper
script and bundled files) under ``~/.config/aish/tools/`` (global). The
project scope (``./.aish/tools/``, wins on name clash) exists in the code but
is DISABLED by default (#178 P0-1): see INCLUDE_PROJECT_DIRS.

Design (epic #141 — skills-primary, tools-as-scalpel): a tool exists ONLY for
a hot, shell-fragile, reliability-critical operation a documented skill snippet
can't do safely. Its one irreducible advantage over a skill's script: the model
never composes a shell string — validated JSON args go to the executable on
stdin, so free-text arguments cannot be mangled by shell quoting.

- **Schema on the way IN, prose on the way OUT.** Args are validated against
  the manifest schema; output is raw stdout+stderr with the exit code appended
  (no output schema, matching run_command's ``[exit code: N]`` convention).
- **Native and plugin tools are indistinguishable to the model** — this module
  emits the exact ``{"type":"function","function":{...}}`` shape native tools
  use, so ``agent._dispatch`` routes both the same way through the same gate.
- **Discovery mirrors skills' folder scan**; an invalid manifest is skipped
  (never crashes discovery) and its reason surfaced as a warning.
- ``mutating`` is declared per tool and is a floor, never authority: a
  read-only tool auto-runs; a mutating one is gated. A manifest that fails to
  declare it is invalid (fail-closed), never silently treated as read-only.
  The floor is MONOTONE across scopes (#178 P1-3): a shadowing manifest may
  raise a tool to mutating, never lower it — a downgrading shadow is refused
  in ``discover`` and the mutating tool survives.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .tools import (
    STATUS_FAILED,
    STATUS_OK,
    VERDICT_EMPTY_OUTPUT,
    VERDICT_ERROR_FIELD,
    VERDICT_EXCEPTION,
    VERDICT_EXIT_CODE,
    VERDICT_REQUIRED_FIELDS,
    ToolOutcome,
    classify_output,
)

GLOBAL_TOOLS_DIR = Path.home() / ".config" / "aish" / "tools"
# Soft budget on the TOTAL exposed tool count (native + plugin). Every tool
# schema is resent on EVERY turn, so an unbounded tool list quietly eats the
# context window (epic #178 item 14: 18 plugin tools ≈ 6.9k tokens/turn on a
# 32k local window). The budget changes NOTHING — no tool is hidden — it only
# produces a one-line consolidation nudge via budget_warning().
TOOL_BUDGET = 25
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
# The two declarations that are NOT a field list. `text` says non-empty output
# is the whole contract; `none` says the runtime cannot check anything beyond
# the exit code. Both are explicit ON PURPOSE — `returns:` is required, so the
# way to opt out of a field contract is to say so, never to omit it (#193).
RETURNS_TEXT = "text"
RETURNS_NONE = "none"
_ARG_TYPES = {"string", "integer", "number", "boolean"}
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 900
# Plugin output caps (#192). These WERE hardcoded at 6000+2000 and consulted
# nothing — not the model, not the context window — so on a Gemini-1M session
# they threw away 19 KB of a 27 KB payload for no reason, and the model, handed
# a dead end with no continuation, improvised. They are now derived from the
# real backend window by `output_caps()`; the constants below are the FLOOR,
# which is today's behaviour, so a small local window never gets worse.
_OUT_HEAD = 6000
_OUT_TAIL = 2000
_OUT_FLOOR = _OUT_HEAD + _OUT_TAIL
# Share of the context window one tool result may claim, and the ceiling that
# keeps a huge window from handing the model a 400 KB wall of text it will not
# read. CHARS_PER_TOKEN mirrors agent.CHARS_PER_TOKEN_BUDGET.
_OUT_SHARE = 0.15
_OUT_CEILING = 120_000
_CHARS_PER_TOKEN = 3
_HEAD_RATIO = 0.75  # 3:1 head:tail, the ratio the flat constants encoded

# Continuation store: the full untruncated output, content-addressed, so page 2
# is served from disk and THE WRAPPER NEVER RE-RUNS (a re-run is not merely
# slow — for a mutating or nondeterministic tool it is a different result, or a
# second side effect).
CONTINUATION_MAX_BYTES = 64 * 1024 * 1024
CONTINUATION_MAX_FILES = 200


# SECURITY (#178 P0-1, interim): project-scope discovery is OFF by default. A
# read-only manifest in a cloned repository's ./.aish/tools would otherwise be
# exposed to the model and run its bundled wrapper with NO approval card, no
# denylist, no root scoping, and the full server environment. The code path
# stays alive behind this switch so the future per-directory trust grant can
# re-enable it; NEITHER entry point (cli.py, server.py) may set it — only
# tests opt in (the `project_scope` fixture). skills.INCLUDE_PROJECT_DIRS is
# the same switch for ./.aish/skills + ./.aish/memory.
INCLUDE_PROJECT_DIRS = False


def tool_dirs(cwd: str) -> list[Path]:
    if INCLUDE_PROJECT_DIRS:
        return [Path(cwd) / ".aish" / "tools", GLOBAL_TOOLS_DIR]
    return [GLOBAL_TOOLS_DIR]


@dataclass
class Tool:
    """One validated plugin tool. ``executable`` is either a bare PATH command
    or a ``./``-prefixed wrapper resolved inside ``dir``."""

    name: str
    description: str
    executable: str
    mutating: bool
    schema: dict  # arg name -> {"type": str, "required": bool, "description": str}
    timeout: int
    body: str
    dir: Path
    mtime: float = 0.0
    # raw command prefixes this tool should be used INSTEAD OF (drift nudge) —
    # not necessarily commands it wraps; alternatives count too.
    prefer_over: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()  # env-var names injected from the Keychain at exec
    preview: bool = False  # if set, resolve args to a human sentence before the gate (#157)
    # The declared output contract (#193), fed straight to classify_output's
    # `required`. None = `returns: none`, no contract beyond the exit code; ()
    # = `returns: text`, non-empty output is the whole contract; a tuple of
    # names = the JSON payload must carry them all, non-empty. The DATACLASS
    # default is the lenient one only because direct construction (tests, other
    # code) is not the trust boundary — `_parse_tool` is, and it refuses a
    # manifest that declares nothing.
    returns: tuple[str, ...] | None = None


def _truncate(text: str, head: int = _OUT_HEAD, tail: int = _OUT_TAIL) -> str:
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n… [{omitted} chars truncated] …\n{text[-tail:]}"


def output_caps(window_tokens: int) -> tuple[int, int]:
    """(head, tail) chars for a plugin result, sized from the REAL backend
    context window. Floored at today's flat 6000+2000 so a small local window
    never regresses, and ceilinged so a 1M window does not hand the model a
    novel it will not read."""
    budget = int(window_tokens * _CHARS_PER_TOKEN * _OUT_SHARE)
    budget = max(_OUT_FLOOR, min(_OUT_CEILING, budget))
    head = int(budget * _HEAD_RATIO)
    return head, budget - head


# A key is the content digest, optionally carrying how much of that content the
# model was ALREADY SHOWN. The suffix rides on the key rather than in a sidecar
# file so the bytes stay purely content-addressed — two tools that produce the
# same output still share one file, and pruning still has exactly one thing to
# delete per cached output.
_KEY_RE = re.compile(r"([0-9a-f]{4,32})(?:s([0-9]{1,9}))?")


def store_continuation(text: str, store_dir, shown: int | None = None) -> str:
    """Cache a full tool output and return its content-addressed key. Returns
    "" if the store is unwritable — a missing continuation degrades to today's
    dead end, which must never be an exception in the middle of a tool call.

    `shown` is how many leading characters the caller already put in front of
    the model, when that is not the cap `read_continuation` would assume. A
    browse result is cut at `web.PAGE_MAX_CHARS` and has no idea what the
    agent's context window makes of `output_caps`; paging against the wrong
    anchor is the silent mid-output hole the docstring below exists to
    prevent."""
    try:
        store = Path(store_dir)
        store.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        path = store / f"{digest}.txt"
        if path.exists():
            path.touch()  # refresh recency; the bytes are already right
        else:
            path.write_text(text, encoding="utf-8")
        prune_continuations(store)
        return digest if shown is None else f"{digest}s{max(0, int(shown))}"
    except OSError:
        return ""


def read_continuation(key: str, store_dir, page: int, head: int, tail: int) -> str | None:
    """Page `page` (1-based) of a cached output, or None when the key is
    unknown — evicted, or a key the model invented.

    Paging RESUMES WHERE THE SHOWN RESULT STOPPED, which is why page 2 starts
    at what was shown and not at `head + tail`: the truncated result showed the
    first `head` chars and then jumped to the LAST `tail` chars, so a page 2
    anchored on the total kept size would skip `tail` characters never seen —
    a silent hole in the middle of an output it was told it could page through
    to the end, which is exactly the class of unannounced gap #192 exists to
    remove. Page 1 is therefore the shown head alone, and pages concatenate to
    the original text exactly (the tail is simply read a second time when
    paging reaches it)."""
    parsed = _KEY_RE.fullmatch(key or "")
    if parsed is None:
        return None
    digest, carried = parsed.group(1), parsed.group(2)
    path = Path(store_dir) / f"{digest}.txt"
    try:
        text = path.read_text(encoding="utf-8")
        path.touch()
    except OSError:
        return None
    # What the model was shown wins over what this backend's caps would have
    # shown: the cut already happened, at whatever size the producing tool uses.
    shown = int(carried) if carried is not None else head
    size = max(1, head + tail)
    if max(1, page) <= 1:
        return text[:shown]
    start = shown + (page - 2) * size
    if start >= len(text):
        return ""
    return text[start : start + size]


def prune_continuations(store_dir) -> None:
    """LRU-evict the continuation store. Best-effort by the same reasoning as
    media.prune: a file vanishing under us must not raise into a tool call."""
    try:
        files = [p for p in Path(store_dir).iterdir() if p.is_file()]
    except OSError:
        return
    entries = []
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, path))
    entries.sort()  # oldest first
    total = sum(size for _, size, _ in entries)
    kept = len(entries)
    for _, size, path in entries:
        if kept <= CONTINUATION_MAX_FILES and total <= CONTINUATION_MAX_BYTES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        kept -= 1


def _parse_bool(value: str) -> bool | None:
    s = value.strip().lower()
    if s in ("yes", "true", "1"):
        return True
    if s in ("no", "false", "0"):
        return False
    return None


def resolve_executable(tool_dir: Path, executable: str) -> str | None:
    """A bare name resolves on PATH; a path (``./x`` or containing ``/``) must
    resolve to a file INSIDE ``tool_dir`` and be executable — so a manifest can
    only ever run its own bundled wrapper, never an arbitrary absolute path."""
    executable = executable.strip()
    if not executable:
        return None
    if "/" not in executable:
        return shutil.which(executable)
    if os.path.isabs(executable):
        return None
    candidate = (tool_dir / executable).resolve()
    try:
        candidate.relative_to(tool_dir.resolve())
    except ValueError:
        return None  # escapes the tool dir
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _parse_tool(manifest: Path) -> tuple[Tool | None, list[str]]:
    """Parse+validate one TOOL.md. Returns (tool, errors); a non-empty errors
    list means the tool is skipped. The linter is deterministic and pure."""
    errors: list[str] = []
    try:
        text = manifest.read_text(encoding="utf-8")
        mtime = manifest.stat().st_mtime
    except OSError as exc:
        return None, [f"cannot read {manifest}: {exc}"]

    if not text.startswith("---"):
        return None, [f"{manifest}: missing YAML frontmatter"]
    parts = text.split("---", 2)
    if len(parts) != 3:
        return None, [f"{manifest}: malformed frontmatter"]
    _, front, body = parts

    fields: dict[str, str] = {}
    for line in front.strip().splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()

    tool_dir = manifest.parent
    name = fields.get("name", "") or tool_dir.name
    if not NAME_RE.match(name):
        errors.append(f"{manifest}: invalid name {name!r} (need [A-Za-z0-9_-], 1-64)")

    description = fields.get("description", "")
    if not description:
        errors.append(f"{manifest}: description is required")

    executable = fields.get("exec", "")
    if not executable:
        errors.append(f"{manifest}: exec is required")
    elif resolve_executable(tool_dir, executable) is None:
        errors.append(
            f"{manifest}: exec {executable!r} does not resolve "
            "(not on PATH, or not an executable wrapper inside the tool dir)"
        )

    mutating = _parse_bool(fields.get("mutating", ""))
    if mutating is None:
        errors.append(f"{manifest}: mutating must be declared as yes/no (fail-closed)")

    preview = False
    if "preview" in fields:
        parsed_preview = _parse_bool(fields["preview"])
        if parsed_preview is None:
            errors.append(f"{manifest}: preview must be yes/no if declared")
        else:
            preview = parsed_preview

    timeout = DEFAULT_TIMEOUT
    if "timeout" in fields:
        try:
            timeout = int(fields["timeout"])
            if not (1 <= timeout <= MAX_TIMEOUT):
                errors.append(f"{manifest}: timeout must be 1-{MAX_TIMEOUT}s")
        except ValueError:
            errors.append(f"{manifest}: timeout must be an integer")

    schema: dict = {}
    raw_schema = fields.get("schema", "").strip()
    if raw_schema:
        try:
            schema = json.loads(raw_schema)
        except json.JSONDecodeError as exc:
            errors.append(f"{manifest}: schema is not valid JSON ({exc})")
        else:
            errors.extend(_validate_schema(manifest, schema))

    returns, returns_errors = _parse_returns(manifest, fields)
    errors.extend(returns_errors)

    secrets = tuple(re.split(r"[,\s]+", fields.get("secrets", "").strip())) if fields.get(
        "secrets", ""
    ).strip() else ()
    secrets = tuple(s for s in secrets if s)
    for s in secrets:
        if not _ENV_NAME_RE.match(s):
            errors.append(f"{manifest}: secret name {s!r} must be [A-Za-z_][A-Za-z0-9_]*")

    if errors:
        return None, errors
    return (
        Tool(
            name=name,
            description=description,
            executable=executable,
            mutating=bool(mutating),
            schema=schema,
            timeout=timeout,
            body=body.strip(),
            dir=tool_dir,
            mtime=mtime,
            prefer_over=tuple(
                p.strip() for p in fields.get("prefer_over", "").split(",") if p.strip()
            ),
            secrets=secrets,
            preview=preview,
            returns=returns,
        ),
        [],
    )


def _parse_returns(manifest: Path, fields: dict) -> tuple[tuple[str, ...] | None, list[str]]:
    """The declared output contract, or an error that skips the tool.

    Required, and fail-closed for the same reason `mutating` is: the runtime's
    only other success signal is the wrapper's exit code, supplied by whoever
    wrote the wrapper. `youtube_analyze` printed `transcript: null` beside a
    populated `error_log` and exited 0 — a failure the harness graded `ok`
    because nothing had ever declared what a successful result must contain.
    An omitted `returns:` is that hole; it is therefore an error, and the way
    to have no field contract is to write `none` and mean it.
    """
    raw = fields.get("returns", "").strip()
    if not raw:
        return None, [
            f"{manifest}: returns is required — declare the fields a SUCCESSFUL result "
            f"must contain (e.g. 'returns: transcript'), or '{RETURNS_TEXT}' if non-empty "
            f"output is the whole contract, or '{RETURNS_NONE}' if the exit code is all "
            "the runtime can check (fail-closed)"
        ]
    tokens = [t for t in re.split(r"[,\s]+", raw) if t]
    lowered = [t.lower() for t in tokens]
    for word in (RETURNS_TEXT, RETURNS_NONE):
        if word in lowered:
            if len(tokens) > 1:
                return None, [
                    f"{manifest}: returns {raw!r} mixes {word!r} with field names — "
                    "it is one or the other"
                ]
            return (None if word == RETURNS_NONE else ()), []
    bad = [t for t in tokens if not _FIELD_NAME_RE.match(t)]
    if bad:
        return None, [
            f"{manifest}: returns field name(s) {', '.join(repr(b) for b in bad)} invalid "
            "(need [A-Za-z_][A-Za-z0-9_.-]*)"
        ]
    return tuple(tokens), []


def lint(manifest: Path) -> list[str]:
    """Public: the deterministic validation errors for a TOOL.md (empty =
    valid). Used by create_tool to refuse writing an invalid manifest."""
    return _parse_tool(manifest)[1]


def _validate_schema(manifest: Path, schema: object) -> list[str]:
    if not isinstance(schema, dict):
        return [f"{manifest}: schema must be a JSON object of arg -> spec"]
    errors = []
    for arg, spec in schema.items():
        if not isinstance(spec, dict):
            errors.append(f"{manifest}: schema arg {arg!r} must be an object")
            continue
        atype = spec.get("type")
        if atype not in _ARG_TYPES:
            errors.append(
                f"{manifest}: schema arg {arg!r} type {atype!r} "
                f"must be one of {sorted(_ARG_TYPES)}"
            )
        if "required" in spec and not isinstance(spec["required"], bool):
            errors.append(f"{manifest}: schema arg {arg!r} 'required' must be true/false")
    return errors


def to_tool_def(tool: Tool) -> dict:
    """The native tool-def shape, so the model cannot tell plugin from native."""
    properties = {}
    required = []
    for arg, spec in tool.schema.items():
        prop = {"type": spec.get("type", "string")}
        if spec.get("description"):
            prop["description"] = str(spec["description"])
        properties[arg] = prop
        if spec.get("required"):
            required.append(arg)
    parameters: dict = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        },
    }


def validate_args(tool: Tool, args: dict) -> str | None:
    """Structured error string (for the correct-and-retry loop) or None if the
    args satisfy the schema. Unknown args and missing required args are errors;
    types are checked leniently (ints accepted for number)."""
    problems = []
    for arg, spec in tool.schema.items():
        if spec.get("required") and arg not in args:
            problems.append(f"missing required arg {arg!r}")
    for arg, value in args.items():
        spec = tool.schema.get(arg)
        if spec is None:
            problems.append(f"unknown arg {arg!r}")
            continue
        if not _type_ok(spec.get("type"), value):
            problems.append(f"arg {arg!r} should be {spec.get('type')}, got {type(value).__name__}")
    if problems:
        allowed = ", ".join(sorted(tool.schema)) or "(none)"
        joined = "; ".join(problems)
        return f"ERROR: invalid args for {tool.name}: {joined}. Allowed args: {allowed}"
    return None


def _type_ok(atype: object, value: object) -> bool:
    if atype == "string":
        return isinstance(value, str)
    if atype == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if atype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if atype == "boolean":
        return isinstance(value, bool)
    return True


def execute(
    tool: Tool,
    args: dict,
    cwd: str,
    get_secret=None,
    caps: tuple[int, int] | None = None,
    cap_source: str = "",
    store_dir=None,
) -> ToolOutcome:
    """Run the tool: validated args as JSON on stdin, raw output + exit code
    back. No shell — the args never pass through shell word-splitting.

    Returns a ``ToolOutcome`` — a str carrying the runtime's verdict (#192), so
    callers that only want the text are unchanged while the trace gets a status
    it did not have to sniff from a string prefix.

    ``caps``/``cap_source`` size truncation from the real backend window and
    record where that size came from; ``store_dir`` caches the full output so a
    truncated result can be paged WITHOUT re-running the wrapper. All three are
    optional so a bare call still works (tests, `aish tool check`), falling back
    to the flat floor.

    Any secrets the manifest declares are resolved (default: the macOS Keychain
    via ``secrets.get``) and injected into ONLY this subprocess's environment —
    never into args, so a value can't leak into logs or the model's context. A
    declared-but-unset secret is a hard error rather than a silent empty env."""
    exe = resolve_executable(tool.dir, tool.executable)
    if exe is None:
        return ToolOutcome(
            f"ERROR: tool {tool.name!r} executable {tool.executable!r} could not be "
            "resolved (not on PATH, or not an executable wrapper inside the tool dir).",
            status=STATUS_FAILED,
            verdict_by=VERDICT_EXCEPTION,
            error="unresolved_executable",
        )
    env = None
    if tool.secrets:
        if get_secret is None:
            from . import secrets as secrets_store

            get_secret = secrets_store.get
        env = dict(os.environ)
        for sec in tool.secrets:
            value = get_secret(sec)
            if value is None:
                return ToolOutcome(
                    f"ERROR: tool {tool.name!r} needs secret {sec!r} but it is not set. "
                    f"Add it with: aish secret set {sec}",
                    status=STATUS_FAILED,
                    verdict_by=VERDICT_EXCEPTION,
                    error="unset_secret",
                )
            env[sec] = value
    try:
        proc = subprocess.run(
            [exe],
            input=json.dumps(args),
            capture_output=True,
            text=True,
            timeout=tool.timeout,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ToolOutcome(
            f"ERROR: tool {tool.name!r} timed out after {tool.timeout}s",
            status=STATUS_FAILED,
            verdict_by=VERDICT_EXCEPTION,
            error="timeout",
        )
    except OSError as exc:
        return ToolOutcome(
            f"ERROR: tool {tool.name!r} failed to start: {exc}",
            status=STATUS_FAILED,
            verdict_by=VERDICT_EXCEPTION,
            error="start_failed",
        )
    out = (proc.stdout or "") + (proc.stderr or "")
    return envelope(
        tool.name,
        out,
        proc.returncode,
        caps,
        cap_source,
        store_dir,
        required=None if tool.returns is None else list(tool.returns),
    )


def envelope(
    name: str,
    out: str,
    exit_code: int,
    caps: tuple[int, int] | None = None,
    cap_source: str = "",
    store_dir=None,
    required: list[str] | None = None,
) -> ToolOutcome:
    """Wrap a wrapper's raw output in the #192 envelope: a runtime-computed
    verdict, and — when the output had to be cut — a continuation the model can
    actually follow instead of a dead end it has to improvise around."""
    head, tail = caps if caps else (_OUT_HEAD, _OUT_TAIL)
    source = cap_source or f"constant:{_OUT_HEAD}+{_OUT_TAIL}"
    status, verdict_by, evidence = classify_output(out, exit_code, required)
    meta: dict = {
        "status": status,
        "verdict_by": verdict_by,
        "exit_code": exit_code,
        "bytes": len(out),
    }
    if evidence:
        meta["required"] = evidence

    body = out
    if len(out) > head + tail:
        key = store_continuation(out, store_dir) if store_dir else ""
        body = _truncate(out, head, tail)
        meta["truncation"] = {
            "kept": head + tail,
            "omitted": len(out) - head - tail,
            "head": head,
            "tail": tail,
            "truncator": "tool_plugins",
            "cap_source": source,
            "continuation": key,
            "offered": bool(key),
        }
        if key:
            # The instruction is IMPERATIVE and names the exact call, because a
            # capability described vaguely in aish's own prompts does not land
            # on small models — and because the failure this replaces is a
            # model that, handed a silent dead end, substituted another source
            # without saying so.
            body += (
                f"\n\n[aish: this output was truncated — {len(out)} chars total, "
                f"{head + tail} shown. The FULL output is cached. To read the "
                f'next part call read_tool_output(continuation="{key}", page=2) '
                "— it is served from the cache and does NOT re-run the tool. "
                "Do NOT substitute another source for the omitted part; page "
                "through it or say what you could not read.]"
            )
    text = f"{body}\n[exit code: {exit_code}]"
    if status != STATUS_OK:
        text += "\n" + _incomplete_note(name, status, verdict_by, evidence)
    return ToolOutcome(text, **meta)


def _incomplete_note(name: str, status: str, verdict_by: str, evidence: dict) -> str:
    """The imperative, in-band note that #190's failure-policy MEMORY
    structurally could not be: it is delivered on the result itself, on the
    channel where the triggering state lives, at the moment it lives there —
    rather than depending on retrieval keyed on user text to surface a rule
    about a tool OUTCOME."""
    why = {
        VERDICT_EXIT_CODE: "it exited non-zero",
        VERDICT_EMPTY_OUTPUT: "it produced no output at all",
        VERDICT_ERROR_FIELD: (
            "it reported errors in "
            + ", ".join(evidence.get("error_fields", ["its error field"]))
            + " and exited 0 anyway"
        ),
        VERDICT_REQUIRED_FIELDS: (
            "required fields came back missing or empty: "
            + ", ".join(evidence.get("missing", []) + evidence.get("empty", []))
        ),
    }.get(verdict_by, "the runtime could not confirm it succeeded")
    return (
        f"[aish: {name} reported status={status} — {why}. This result is NOT "
        "usable as if it had succeeded. You MUST tell the user the tool failed "
        "before using any other source, and you MUST NOT present substituted "
        "material as if it came from the source the user named.]"
    )


PREVIEW_ENV_FLAG = "AISH_TOOL_PREVIEW"
_PREVIEW_TIMEOUT = 20
_PREVIEW_MAX = 600


def preview(tool: Tool, args: dict, cwd: str, get_secret=None) -> str | None:
    """Resolve a mutating tool's args to ONE human sentence shown on the approval
    card BEFORE the gate (#157) — the tool's plan/commit gap, mirroring files.py's
    diff. Runs the SAME wrapper with ``AISH_TOOL_PREVIEW=1`` set; the wrapper is
    contracted to resolve + describe (e.g. ``rem show <id>``) WITHOUT mutating and
    print the sentence to stdout. Fail-OPEN: any problem (no preview declared,
    error, empty, timeout, unset secret) returns None so the caller falls back to
    the raw-args card — a preview is an upgrade, never a dependency, and never a
    blocker on the mutation itself."""
    if not tool.preview:
        return None
    exe = resolve_executable(tool.dir, tool.executable)
    if exe is None:
        return None
    env = dict(os.environ)
    env[PREVIEW_ENV_FLAG] = "1"
    if tool.secrets:
        if get_secret is None:
            from . import secrets as secrets_store

            get_secret = secrets_store.get
        for sec in tool.secrets:
            value = get_secret(sec)
            if value is None:
                return None
            env[sec] = value
    try:
        proc = subprocess.run(
            [exe],
            input=json.dumps(args),
            capture_output=True,
            text=True,
            timeout=min(tool.timeout, _PREVIEW_TIMEOUT),
            cwd=cwd,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    return text[:_PREVIEW_MAX] if text else None


def discover(cwd: str) -> tuple[list[Tool], list[str]]:
    """All valid tools (project before global, first wins on name clash) plus a
    list of warnings for skipped invalid manifests.

    ``mutating`` is a MONOTONE FLOOR across scopes (#178 P1-3): a shadowing
    (project) manifest may RAISE a global tool to mutating, never lower it. A
    shadow that would downgrade a mutating tool to read-only — routing its
    wrapper through the ungated parallel read path — is REFUSED outright: the
    mutating manifest is kept (and stays gated by approve_tool) and the
    downgrading one is rejected with a loud warning naming both paths."""
    tools: dict[str, Tool] = {}
    warnings: list[str] = []
    for directory in tool_dirs(cwd):
        try:
            subdirs = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            continue
        for sub in subdirs:
            manifest = sub / "TOOL.md"
            if not manifest.is_file():
                continue
            tool, errors = _parse_tool(manifest)
            if errors:
                warnings.extend(errors)
            elif tool is not None:
                existing = tools.get(tool.name)
                if existing is not None:
                    if tool.mutating and not existing.mutating:
                        # The earlier winner (project scope) declared read-only
                        # while this shadowed manifest is mutating: a warning is
                        # not a gate for a mutability downgrade, so the shadow
                        # is refused and the mutating tool survives.
                        tools[tool.name] = tool
                        warnings.append(
                            f"{existing.dir / 'TOOL.md'}: REFUSED — it shadows "
                            f"{manifest} but downgrades tool {tool.name!r} from "
                            "mutating to read-only, which would bypass the "
                            "approval gate; the mutating manifest is kept "
                            "(`mutating` is a monotone floor across scopes)"
                        )
                        continue
                    # project dirs come first, so `existing` wins and this one is
                    # shadowed. A silent shadow is a sharp edge for EXECUTABLES —
                    # doubly so when the mutability differs — so warn.
                    mut = (
                        ""
                        if existing.mutating == tool.mutating
                        else f" — and their `mutating` flags DIFFER "
                        f"({existing.mutating} vs {tool.mutating})"
                    )
                    warnings.append(
                        f"{manifest}: tool {tool.name!r} is shadowed by "
                        f"{existing.dir / 'TOOL.md'}{mut}"
                    )
                else:
                    tools[tool.name] = tool
    return list(tools.values()), warnings


def budget_warning(names: list[str]) -> str | None:
    """One-line nudge when the exposed tool count exceeds the soft TOOL_BUDGET,
    or None when within budget. ``names`` is EVERY exposed tool name (native +
    plugin — the schemas the model pays for each turn). The hint names the
    largest ``<prefix>_*`` family (e.g. "9 reminders_*"): a per-subcommand
    explosion is exactly the drift epic #141's tools-as-scalpel doctrine
    forbids, and consolidating that family is the highest-value fix. Pure and
    deterministic; behavior never changes — no tool is hidden."""
    total = len(names)
    if total <= TOOL_BUDGET:
        return None
    families: dict[str, int] = {}
    for name in names:
        prefix, sep, _ = name.partition("_")
        if sep and prefix:
            families[prefix] = families.get(prefix, 0) + 1
    hint = ""
    biggest = max(families.items(), key=lambda kv: (kv[1], kv[0]), default=None)
    if biggest is not None and biggest[1] >= 2:
        hint = f" (largest family: {biggest[1]} {biggest[0]}_* — consolidate those first)"
    return (
        f"{total} tools exposed exceeds the soft budget of {TOOL_BUDGET}; "
        f"every tool schema costs context on every turn{hint}"
    )


def signature(cwd: str) -> tuple:
    """Cheap change-detector for the per-iteration rescan: the set of TOOL.md
    paths and their mtimes. Rebuild the tool list only when this moves."""
    sig = []
    for directory in tool_dirs(cwd):
        try:
            subdirs = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            continue
        for sub in subdirs:
            manifest = sub / "TOOL.md"
            try:
                sig.append((str(manifest), manifest.stat().st_mtime))
            except OSError:
                continue
    return tuple(sig)
