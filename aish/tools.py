"""Tool implementations: shell execution and documentation lookup.

Security model: run_command executes arbitrary shell strings and therefore
MUST only be reached through the agent's approval gate. read_docs is
auto-approved, so it never accepts a shell string — only a bare command
name, validated and resolved against PATH before anything is executed.
"""

import contextlib
import datetime
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

try:
    import termios
    import tty

    _HAS_TERMIOS = True
except ImportError:  # non-unix
    _HAS_TERMIOS = False

DETACH_KEY = b"\x02"  # Ctrl-B

# Enough for the model to work with without blowing a 32k context on one result.
HEAD_CHARS = 4000
TAIL_CHARS = 2000
DOCS_MAX_CHARS = 6000

COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def truncate(text: str, head: int = HEAD_CHARS, tail: int = TAIL_CHARS) -> str:
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    tail_text = text[-tail:] if tail else ""  # text[-0:] is the WHOLE string
    return f"{text[:head]}\n[... {omitted} characters omitted ...]\n{tail_text}"


# --- the tool result envelope (#192, docs/trace-contract.md §3.4) -----------

STATUS_OK = "ok"
STATUS_INCOMPLETE = "incomplete"
STATUS_FAILED = "failed"

# How the status was decided. Closed vocabulary; the first five are the
# contract's, the last two are additions this phase had to make and which are
# recorded in §3.4:
#   error_field — a JSON payload with a populated error channel and exit 0.
#     This is the youtube_analyze shape exactly (`transcript: ""` beside a
#     non-empty `error_log`, exit 0). Without #193's declared required-fields
#     contract, `empty_output` cannot see it: the payload as a whole is 575
#     chars, so it is not empty — only the field that mattered was.
#   prefix — no envelope; the legacy startswith() sniff decided. Recorded
#     EXPLICITLY rather than left absent, because "absence must never be the
#     evidence" (contract corollary 2) — and because counting these is the
#     honest measure of how much of the tool surface is still un-enveloped.
VERDICT_EXIT_CODE = "exit_code"
VERDICT_REQUIRED_FIELDS = "required_fields"
VERDICT_EMPTY_OUTPUT = "empty_output"
VERDICT_ERROR_FIELD = "error_field"
VERDICT_GATE = "gate"
VERDICT_EXCEPTION = "exception"
VERDICT_PREFIX = "prefix"

# Field names a wrapper conventionally reports failure through. Deterministic
# and declared — this is not the runtime guessing at prose, it is reading a
# named channel whose presence the wrapper author chose.
ERROR_FIELDS = ("error", "error_log", "errors")


class ToolOutcome(str):
    """A tool result string carrying the runtime's verdict alongside it.

    Deliberately a `str` SUBCLASS. Every existing caller — the model-facing
    result, `_with_feedback`, the tests — keeps treating it as the result text,
    so the envelope adds information with no ripple through ~30 dispatch
    branches. More importantly the metadata travels WITH the value instead of
    living in instance state (`_run_meta`), which is what makes it correct on
    the parallel read-only path where several calls are in flight at once and a
    shared attribute would be a race.

    Caveat, and the reason construction is always the LAST step: string
    operations return a plain `str`, so slicing or concatenating a ToolOutcome
    silently drops the envelope. Build it after any text manipulation, never
    before.
    """

    __slots__ = ("meta",)

    meta: dict

    def __new__(cls, text: str, **meta) -> "ToolOutcome":
        outcome = super().__new__(cls, text)
        outcome.meta = meta
        return outcome


def classify_output(text: str, exit_code: int, required: list[str] | None = None) -> tuple:
    """(status, verdict_by, evidence) for a tool's raw output — the whole point
    of #192: the runtime owes the model a verdict it did not have to infer from
    a string prefix.

    Deterministic, in escalating order of specificity. `required` is the
    declared required-field list from #193's tool contract; until that ships it
    is empty everywhere, and exit code + emptiness + a populated error channel
    are the floor.
    """
    evidence: dict = {}
    if exit_code != 0:
        return STATUS_FAILED, VERDICT_EXIT_CODE, evidence
    if not text.strip():
        return STATUS_INCOMPLETE, VERDICT_EMPTY_OUTPUT, evidence

    payload = _json_object(text)
    if payload is None:
        if required:
            # Declared fields with no JSON to hold them. Grading this `ok`
            # would make the contract opt-out-able by a wrapper that simply
            # stops printing JSON — the exact silence #193 exists to end.
            return (
                STATUS_INCOMPLETE,
                VERDICT_REQUIRED_FIELDS,
                {"declared": list(required), "missing": list(required),
                 "empty": [], "payload": "not_json"},
            )
        if required is not None:
            evidence["declared"] = []
        return STATUS_OK, VERDICT_EXIT_CODE, evidence

    if required:
        missing = [f for f in required if f not in payload]
        empty = [f for f in required if f in payload and not payload[f]]
        evidence = {"declared": list(required), "missing": missing, "empty": empty}
        if missing or empty:
            return STATUS_INCOMPLETE, VERDICT_REQUIRED_FIELDS, evidence

    reported = [f for f in ERROR_FIELDS if payload.get(f)]
    if reported:
        evidence = {**evidence, "error_fields": reported}
        return STATUS_INCOMPLETE, VERDICT_ERROR_FIELD, evidence

    if required is not None:
        evidence.setdefault("declared", list(required))
    return STATUS_OK, VERDICT_EXIT_CODE, evidence


def _json_object(text: str) -> dict | None:
    """The payload as a JSON object, or None. Tolerant of a wrapper that prints
    a banner line before its JSON, which is common enough that being strict
    here would silently disable the whole error-channel check."""
    stripped = text.strip()
    start = stripped.find("{")
    if start == -1:
        return None
    try:
        value = json.loads(stripped[start:])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _decode(data: bytes | None) -> str:
    """Commands can emit arbitrary bytes (binary plists, etc.) — never let
    decoding crash the agent."""
    return (data or b"").decode("utf-8", errors="replace")


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Send `sig` to the child's whole process group so its descendants die too,
    not just the shell. The child leads its own group (start_new_session=True),
    so its pgid equals its pid. Best-effort: the group may already be gone, or
    the platform may lack process groups — fall back to the bare process."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, AttributeError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError, ValueError):
            proc.send_signal(sig)


def _stop_group(proc: subprocess.Popen) -> None:
    """Cancel a running command by signaling its process group, escalating
    SIGINT → SIGTERM → SIGKILL and giving each a moment to land. SIGINT first
    mirrors an interactive Ctrl-C; SIGKILL is the last resort for a process that
    ignores the gentler signals. Reaps the child so its returncode is set."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        _signal_group(proc, sig)
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue
    _signal_group(proc, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=2)


def run_command(
    command: str,
    timeout: float = 120,
    cwd: str | None = None,
    on_line: Callable[[str], None] | None = None,
    allow_detach: bool = False,
    log_dir=None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Execute a shell command, streaming output lines via on_line as they
    arrive (stderr merged into stdout so ordering is preserved live).

    Ctrl-C cancels the command — not the session — and returns partial output.
    When allow_detach is set on a TTY, Ctrl-B hands the still-running command
    to the background-job table and returns immediately.
    """
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # Own process group so a cancel/timeout can signal the whole group —
            # the shell AND everything it spawned — not just the shell (a bare
            # terminate leaves grandchildren like a `sleep` inside `sh -c` alive).
            start_new_session=True,
        )
    except OSError as exc:
        return f"ERROR: failed to start command: {exc}"

    watch_keys = allow_detach and _HAS_TERMIOS and sys.stdin.isatty()
    stdin_fd = sys.stdin.fileno() if watch_keys else -1
    saved_term = None
    deadline = None if timeout is None else time.monotonic() + timeout
    assert proc.stdout is not None  # Popen was given stdout=PIPE
    out_fd = proc.stdout.fileno()
    lines: list[str] = []
    buf = b""
    cancelled = timed_out = False

    try:
        if watch_keys:
            saved_term = termios.tcgetattr(stdin_fd)
            tty.setcbreak(stdin_fd)  # cbreak keeps ISIG, so Ctrl-C still signals
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                _signal_group(proc, signal.SIGKILL)
                break
            # Cooperative cancel (web UI Stop button): checked once per
            # select slice, so a stop lands within ~0.5s.
            if should_stop is not None and should_stop():
                cancelled = True
                _stop_group(proc)
                break
            slice_t = 0.5
            if deadline is not None:
                slice_t = min(0.5, max(0.0, deadline - time.monotonic()))
            watch = [out_fd, stdin_fd] if watch_keys else [out_fd]
            ready, _, _ = select.select(watch, [], [], slice_t)

            if watch_keys and stdin_fd in ready:
                if os.read(stdin_fd, 1) == DETACH_KEY:
                    _flush_buf(buf, lines, on_line)
                    return _detach_running(proc, command, lines, log_dir, on_line)

            if out_fd in ready:
                chunk = os.read(out_fd, 65536)
                if not chunk:
                    break
                buf += chunk
                *complete, buf = buf.split(b"\n")
                for raw in complete:
                    line = _decode(raw)
                    lines.append(line)
                    if on_line:
                        on_line(line)
        proc.wait()
    except KeyboardInterrupt:
        cancelled = True
        _stop_group(proc)
    finally:
        if saved_term is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_term)

    _flush_buf(buf, lines, on_line)
    parts = []
    output = "\n".join(lines)
    if output.strip():
        parts.append(output)
    if timed_out:
        parts.append(f"ERROR: command timed out after {timeout}s (any partial output is above)")
    elif cancelled:
        parts.append("[stopped by user — any partial output is above]")
    parts.append(f"[exit code: {proc.returncode}]")
    return truncate("\n".join(parts))


def _flush_buf(buf: bytes, lines: list[str], on_line) -> None:
    """Emit any trailing bytes with no final newline as one last line."""
    if buf:
        line = _decode(buf)
        lines.append(line)
        if on_line:
            on_line(line)


# Copies stdin→stdout; run as an independent process so it outlives aish.
_DRAIN_SCRIPT = "import shutil,sys; shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)"


def _detach_running(proc, command, collected, log_dir, on_line) -> str:
    """Hand a running foreground command to the background-job table. Its
    still-open output pipe is drained by an INDEPENDENT process in its own
    session, so output keeps flowing to the log — and the child never blocks on
    a full pipe — even after aish exits. (A daemon thread would die with aish,
    stalling the child once its 64 KB pipe buffer filled.)"""
    directory = Path(log_dir) if log_dir else Path.home() / ".local" / "state" / "aish" / "jobs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = directory / f"job-{stamp}-{len(JOBS) + 1}.log"
    log_file = log_path.open("wb")
    if collected:
        log_file.write(("\n".join(collected) + "\n").encode())
        log_file.flush()
    JOBS.append({"pid": proc.pid, "command": command, "log": str(log_path), "proc": proc})

    try:
        subprocess.Popen(
            [sys.executable, "-c", _DRAIN_SCRIPT],
            stdin=proc.stdout,
            stdout=log_file,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        proc.stdout.close()  # the drainer holds the only read end now
        log_file.close()     # …and, via its dup, the only write end
    except OSError:
        # Couldn't spawn a drainer: fall back to an in-process daemon thread
        # (works while aish runs, but won't outlive it).
        out_fd = proc.stdout.fileno()

        def drain() -> None:
            try:
                while chunk := os.read(out_fd, 65536):
                    log_file.write(chunk)
                    log_file.flush()
                proc.wait()
            finally:
                log_file.close()

        threading.Thread(target=drain, daemon=True).start()

    message = (
        f"[detached to background: pid {proc.pid}, log: {log_path}]\n"
        f"Still running. Check with: tail -n 30 {log_path} — stop with: kill {proc.pid}"
    )
    if on_line:
        on_line(message)
    return message


TOPIC_CONTEXT_LINES = 4
TRUNCATION_HINT = (
    "\n[docs truncated — call read_docs again with a 'topic' (e.g. a flag name) "
    "to search the full text]"
)


# Background jobs started this session (the processes outlive aish).
JOBS: list[dict] = []


def start_background(command: str, cwd: str | None = None, log_dir=None) -> str:
    """Start a detached long-running command; output goes to a log file the
    model (or user) can tail. The process survives aish exiting."""
    directory = Path(log_dir) if log_dir else Path.home() / ".local" / "state" / "aish" / "jobs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = directory / f"job-{stamp}-{len(JOBS) + 1}.log"
    log_file = log_path.open("wb")
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        log_file.close()
        return f"ERROR: failed to start background job: {exc}"
    JOBS.append({"pid": proc.pid, "command": command, "log": str(log_path), "proc": proc})
    return (
        f"[background job started: pid {proc.pid}, log: {log_path}]\n"
        f"Check progress with: tail -n 30 {log_path} — stop with: kill {proc.pid}"
    )


def jobs_table() -> str:
    if not JOBS:
        return "no background jobs this session"
    lines = []
    for i, job in enumerate(JOBS, 1):
        code = job["proc"].poll()
        status = "running" if code is None else f"exit {code}"
        lines.append(f"{i:>3}. [{status:>8}] pid {job['pid']} · {job['command']} · {job['log']}")
    return "\n".join(lines)


def read_docs(command: str, topic: str | None = None) -> str:
    """Look up documentation for a command: man page, then --help, then -h.

    With a topic, returns only the lines matching it (plus context) from the
    FULL documentation — the way past the truncation limit on big man pages.
    """
    name = command.strip()
    if not COMMAND_NAME_RE.match(name):
        return (
            f"ERROR: read_docs takes a bare command name (got {name!r}). "
            "Pass a single command name with no arguments or shell syntax."
        )

    found = _fetch_docs(name)
    if found is None:
        if shutil.which(name) is None:
            return f"ERROR: '{name}' not found on this system (no man page, not in PATH)."
        return (
            f"NO DOCUMENTATION FOUND for '{name}' (tried man, --help, -h). "
            "Proceed with maximum caution: use only flags you are certain of, "
            "or tell the user documentation is unavailable."
        )
    text, source = found

    if topic:
        matched = _filter_topic(text, topic)
        if matched:
            return truncate(
                f"[{source} — lines matching {topic!r}]\n{matched}", head=DOCS_MAX_CHARS, tail=0
            )
        return truncate(
            f"[{source}] NO LINES MATCH {topic!r}; start of docs instead:\n{text}",
            head=DOCS_MAX_CHARS,
            tail=0,
        )

    result = f"[{source}]\n{text}"
    if len(result) > DOCS_MAX_CHARS:
        return truncate(result, head=DOCS_MAX_CHARS, tail=0) + TRUNCATION_HINT
    return result


def _resolves_into_cwd(resolved: str) -> bool:
    """True if a PATH-resolved binary lives in the current directory — i.e. a
    '.'-in-PATH would let a doc lookup run a locally-planted executable."""
    try:
        return os.path.dirname(os.path.realpath(resolved)) == os.path.realpath(os.getcwd())
    except OSError:
        return True  # can't tell whose binary this is → refuse to run it


def _fetch_docs(name: str) -> tuple[str, str] | None:
    """Full documentation text and its source label, or None if none exists.

    NOTE: the --help/-h fallback EXECUTES the resolved binary (one conventional
    help flag, 10s timeout, no stdin) — a deliberate grounding tradeoff, tried
    only after the man page fails. A candidate that resolves into the current
    directory is refused, so a '.'-in-PATH can't turn a doc lookup into running
    an attacker-planted binary.
    """
    quoted = shlex.quote(name)
    man = subprocess.run(
        f"man {quoted} 2>/dev/null | col -b",
        shell=True,
        capture_output=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )
    man_text = _decode(man.stdout).strip()
    if man_text:
        return man_text, f"man {name}"

    resolved = shutil.which(name)
    if resolved is None or _resolves_into_cwd(resolved):
        return None

    for flag in ("--help", "-h"):
        try:
            help_run = subprocess.run(
                [name, flag],
                capture_output=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        output = (_decode(help_run.stdout) + _decode(help_run.stderr)).strip()
        if output:
            return output, f"{name} {flag}"
    return None


def _filter_topic(text: str, topic: str) -> str:
    """Lines matching topic (case-insensitive) with surrounding context,
    overlapping regions merged, gaps marked."""
    lines = text.splitlines()
    needle = topic.lower()
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if needle in line.lower():
            keep.update(
                range(max(0, i - TOPIC_CONTEXT_LINES), min(len(lines), i + TOPIC_CONTEXT_LINES + 1))
            )
    if not keep:
        return ""

    out: list[str] = []
    previous = None
    for i in sorted(keep):
        if previous is not None and i > previous + 1:
            out.append("  [...]")
        out.append(lines[i])
        previous = i
    return "\n".join(out)


_WHEN_SUBJECT = (
    "Which subject the trigger examines: 'prompt' (what the user typed, plus their "
    "attachments), 'session' (how this session was started), 'action' (the call "
    "about to run), or 'always'. Pick the NARROWEST one that is true — a rule that "
    "binds every turn costs every turn."
)

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_docs",
            "description": (
                "Read the documentation for a CLI command (man page, falling back to "
                "--help / -h). ALWAYS call this before using a command whose flags you "
                "are not completely certain about, and after any usage/unknown-flag error. "
                "If docs come back truncated, call again with a 'topic' to search the "
                "full text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bare command name only, e.g. 'tar' — no arguments.",
                    },
                    "topic": {
                        "type": "string",
                        "description": (
                            "Optional search term (e.g. a flag name like 'maxdepth'): "
                            "returns only matching lines with context from the full docs."
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": (
                "Read a skill — a proven playbook with workflows, exact commands, "
                "and safety rules. When a skill in your context matches the task, "
                "read it BEFORE acting and follow it over your built-in approach "
                "from training data — skills encode what actually worked on this "
                "machine."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name as listed in your context, e.g. 'sweepy'.",
                    }
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save one durable fact or lesson to your memory so future sessions "
                "have it — ESPECIALLY after you get a command wrong and find the "
                "working form, and whenever the user states a preference or a fact "
                "about this machine. Write the corrected, ready-to-use form. Recent "
                "memory is shown in your context; the rest is searchable with "
                "recall. Don't record one-off or secret details. For multi-step "
                "procedures, write a skill file instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "One-line fact, e.g. 'macOS ps: sort by mem = ps aux -m'.",
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Optional stable slug (letters/digits/dashes). Reusing an "
                            "existing name UPDATES that memory instead of duplicating it."
                        ),
                    },
                    "keywords": {
                        "type": "string",
                        "description": (
                            "Comma-separated retrieval keywords: singular topical "
                            "nouns and synonyms a user would type in a task, in "
                            "every language the user uses (e.g. 'price, buy, shop, "
                            "cena, kup, sklep'). These make the memory findable — "
                            "provide them."
                        ),
                    },
                    "pinned": {
                        "type": "boolean",
                        "description": (
                            "Set true ONLY for a standing rule or preference that "
                            "MUST apply to every future task — e.g. the user says "
                            "'never push to a remote without asking' → remember it "
                            "with pinned: true. Pinned memories are always shown in "
                            "your context as standing rules and never rotate out. "
                            "Ordinary facts (paths, commands, one-off details) MUST "
                            "stay unpinned."
                        ),
                    },
                    "expires": {
                        "type": "string",
                        "description": (
                            "YYYY-MM-DD date after which the fact stops applying; "
                            "the entry then drops out of your context and recall "
                            "automatically. You MUST set it when the fact has a "
                            "known end date (e.g. 'parking pass code is 4412' with "
                            "expires: 2026-08-31). Omit for durable facts."
                        ),
                    },
                    "disabled": {
                        "type": "boolean",
                        "description": (
                            "Set true to RETIRE an existing entry without deleting "
                            "it (reversible — false re-enables): pass its name and "
                            "restate its description in note. Use this instead of "
                            "forget_memory when curating stale or noisy entries; "
                            "omit for normal saves."
                        ),
                    },
                    "force": {
                        "type": "boolean",
                        "description": (
                            "Only when a save was refused as similar to an existing "
                            "entry AND you verified the facts are genuinely "
                            "different: retry with force: true. Otherwise UPDATE the "
                            "named entry (remember with its name) or forget_memory "
                            "it — never force past a real duplicate."
                        ),
                    },
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": (
                "Permanently delete ONE stale or wrong memory entry by its slug "
                "name. Use this to prune memory that is outdated, incorrect, or "
                "superseded, and to CONSOLIDATE duplicates: first remember() the "
                "single canonical fact (reusing or picking one slug), then "
                "forget_memory() each redundant slug so only the canonical entry "
                "remains. Names come from the memory index in your context or "
                "from recall. Only affects your own memory files — never other "
                "files. Verify the name (recall first) before forgetting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The exact slug of the memory entry to delete.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a text file with line numbers, optionally a specific line range. "
                "Prefer this over `cat`/`sed -n`/`head`/`tail` — it needs no approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (rel to cwd or abs)."},
                    "offset": {
                        "type": "integer",
                        "description": "1-based line to start from (default 1).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to return (default 2000).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a file or overwrite it entirely with new content. The user sees "
                "a diff and must approve before anything is written. Use edit_file for "
                "small changes to a large existing file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (rel to cwd or abs)."},
                    "content": {"type": "string", "description": "The full new file contents."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact string in a file with a new string. old_str must match "
                "exactly and be UNIQUE in the file (include surrounding lines for context "
                "if needed) — the edit fails rather than guess. NEVER include the 'NNN  ' "
                "line-number prefixes that read_file shows; copy the raw file text. The "
                "user approves a diff before it is written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (rel to cwd or abs)."},
                    "old_str": {"type": "string", "description": "Exact unique text to replace."},
                    "new_str": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web (DuckDuckGo); returns titles, URLs, and snippets. "
                "Use for information NOT on this machine: current events, software "
                "releases, unfamiliar error messages, general facts. Snippets alone "
                "are rarely enough — follow up with read_url on the best result. "
                "Queries leave this machine: NEVER include private local data "
                "(file contents, key values, personal details) in a query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords, like you would type into a search engine.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": (
                "Fetch a web page and return its readable text. Use after web_search "
                "to read a promising result, or on any URL the user gives you. If the "
                "page comes back truncated, call again with a 'topic' to search the "
                "full page text (works like read_docs topics)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full http(s) URL of the page to read.",
                    },
                    "topic": {
                        "type": "string",
                        "description": (
                            "Optional word or phrase: returns only matching lines "
                            "with context from the full page text."
                        ),
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_image",
            "description": (
                "Display a picture to the user. Call this WHENEVER the answer should "
                "include an image — the user asks what something looks like, asks for "
                "a photo/picture/diagram, or you are recommending a product worth "
                "seeing. It fetches the image, verifies it really is one, stores it "
                "where the UI can display it, and returns the exact markdown line to "
                "put in your answer.\n"
                "You MUST use this tool for every image you show. NEVER write an "
                "![alt](https://…) markdown image yourself — the UI refuses to load "
                "remote images and it renders as a dead link. NEVER download an image "
                "with curl/wget: files outside this store are not displayable, and it "
                "costs the user an approval prompt. To find a picture, web_search for "
                "the subject, read_url a promising page, then pass an image URL from "
                "that page here."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Full http(s) URL of the image file itself (ending .jpg/"
                            ".png/.gif/.webp, not the page it appears on), OR an "
                            "absolute path to an image already on this machine."
                        ),
                    },
                    "caption": {
                        "type": "string",
                        "description": (
                            "Short description of what the picture shows — becomes the "
                            "alt text, so write it for someone who cannot see it."
                        ),
                    },
                },
                "required": ["source", "caption"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": (
                "Search everything you know: saved skills (how-to playbooks), "
                "memory (facts, preferences, past lessons), and past conversation "
                "sessions with this user. Use it BEFORE guessing at a procedure "
                "that might have been solved before, when the user refers to "
                "earlier work ('like we did yesterday', 'what went wrong last "
                "time'), and ALWAYS before creating a new skill or memory — update "
                "the existing entry instead of duplicating it. Returns ranked "
                "matches with snippets; call again with 'name' set to a returned "
                "entry or session file name for its full text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Keywords describing the task, fact, or past work "
                            "you are looking for."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Optional entry name or session file name from a "
                            "previous result: return that item's full text."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_tool_output",
            "description": (
                "Read the next part of a tool result that was TRUNCATED. When a "
                "tool's output is too large it is cut, and the note on that "
                "result gives you a 'continuation' key — pass it here with the "
                "next page number to read the rest. The full output is served "
                "from a cache, so this does NOT re-run the tool and costs "
                "nothing. Use this instead of guessing at the omitted part, and "
                "NEVER substitute a different source for content you could not "
                "read without telling the user you did so."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "continuation": {
                        "type": "string",
                        "description": (
                            "The continuation key printed on the truncated result."
                        ),
                    },
                    "page": {
                        "type": "integer",
                        "description": (
                            "Which page to read; the truncated result showed "
                            "page 1, so start at 2 and increment."
                        ),
                    },
                },
                "required": ["continuation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command on the user's machine. The user sees the exact "
                "command and must approve it before it executes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The exact shell command to run.",
                    },
                    "background": {
                        "type": "boolean",
                        "description": (
                            "Set true for long-running commands (servers, watchers, big "
                            "upgrades): runs detached, output goes to a log file you can "
                            "tail with normal commands."
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_tool",
            "description": (
                "Create a reusable plugin tool (a TOOL.md + wrapper) so a fragile, "
                "repeated operation runs the SAME reliable way every time. Create a "
                "tool ONLY when ALL THREE hold: (1) it is invoked FREQUENTLY, (2) its "
                "arguments are FREE-TEXT or otherwise fragile through shell quoting "
                "(e.g. an email body, an issue body), AND (3) reliability MATTERS "
                "(it mutates state or produces user-facing output). If any is false, "
                "write a skill instead — do NOT create a tool for read-only, simple-"
                "argument, or one-off operations. The wrapper receives the validated "
                "arguments as a JSON object on STDIN, prints results to stdout, and MUST "
                "exit NON-ZERO whenever it did not do what it promises — a wrapper that "
                "prints an error into its output and exits 0 anyway reports a failure as "
                "a success, and the model then answers from something else. Declare what "
                "a good result contains in 'returns' as well: aish checks it on every "
                "call, so the contract does not depend on the exit code alone. (No shell "
                "quoting in the argument path — that is the whole point.) "
                "aish writes each tool as its OWN directory: "
                "<scope>/tools/<name>/ containing TOOL.md (the manifest) and the wrapper "
                "script — the manifest shown first, then the wrapper, each diff-approved. "
                "Do NOT describe or invent any other layout (there is no flat '.json' "
                "manifest), and do NOT ask the user to choose file paths — pass the "
                "'scope' argument and just call create_tool; aish handles placement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Tool name, [a-z0-9_-], e.g. 'gh_issue_create'. "
                        "One tool = one operation (split, don't make an ops menu).",
                    },
                    "description": {
                        "type": "string",
                        "description": "What the tool does and when to use it (this is "
                        "what the model sees to pick it).",
                    },
                    "mutating": {
                        "type": "boolean",
                        "description": "true if it changes state / has side effects "
                        "(then every call is approval-gated). Be conservative.",
                    },
                    "schema": {
                        "type": "string",
                        "description": "JSON object of arg -> {type, required, description}, "
                        'e.g. {"title": {"type": "string", "required": true}}. '
                        "Types: string, integer, number, boolean. Use {} for no args.",
                    },
                    "wrapper": {
                        "type": "string",
                        "description": "The wrapper script body. It reads the JSON args on "
                        "stdin and prints output. Map the stable args to the real CLI here "
                        "so the model never composes that command again. If you set "
                        "'preview', the wrapper MUST check the AISH_TOOL_PREVIEW environment "
                        "variable at the TOP: when it is set, RESOLVE the args (look the id "
                        "up), print ONE human-readable sentence to stdout and exit 0 WITHOUT "
                        "mutating anything — that sentence is what the user approves. A "
                        "preview that mutates defeats the approval gate.",
                    },
                    "wrapper_lang": {
                        "type": "string",
                        "description": "'sh' (default) or 'python' — sets the file "
                        "extension and shebang.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Optional per-call timeout in seconds (default 120).",
                    },
                    "scope": {
                        "type": "string",
                        "description": "'global' (default, ~/.config/aish/tools, backed "
                        "up) — the only usable scope; 'project' (./.aish/tools) is "
                        "disabled pending a per-directory trust mechanism and is "
                        "refused.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional prose body for the TOOL.md: how the "
                        "underlying CLI behaves, gotchas.",
                    },
                    "returns": {
                        "type": "string",
                        "description": "REQUIRED — the tool's success contract, which aish "
                        "CHECKS on every call. If the wrapper prints a JSON OBJECT, list the "
                        "fields a SUCCESSFUL result must contain, non-empty, space-separated (e.g. "
                        "'transcript' for a transcript fetcher, 'url id' for an uploader): "
                        "aish then marks the call FAILED whenever one of them comes back "
                        "missing, null or empty, no matter what the exit code said. Use "
                        "'text' when the wrapper prints prose, or a JSON ARRAY, or anything "
                        "else where non-empty output is the whole contract (a search that "
                        "legitimately finds nothing SUCCEEDED — do not make emptiness a "
                        "failure). Use 'none' ONLY when nothing about the output can "
                        "be checked — that is an opt-out and it is recorded as one. Do NOT "
                        "list optional fields: every field you name here is one the tool "
                        "PROMISES, and a promise it cannot keep is reported to the user as "
                        "a failure.",
                    },
                    "prefer_over": {
                        "type": "string",
                        "description": "Optional: raw command(s) this tool should be used "
                        "INSTEAD OF — comma-separated, prefixes allowed (e.g. "
                        "'gh issue create, gh issue new'). These need not be commands the "
                        "tool wraps — list any raw command a person might reach for that "
                        "this tool does better. If the model runs one, aish nudges it here.",
                    },
                    "secrets": {
                        "type": "string",
                        "description": "Optional: comma/space-separated env-var names the "
                        "wrapper needs (e.g. 'FASTMAIL_TOKEN'). aish injects them from the "
                        "Keychain into the wrapper's env at run time — you never put secret "
                        "VALUES in the tool. The user sets them with `aish secret set NAME`.",
                    },
                    "preview": {
                        "type": "boolean",
                        "description": "Set true when the tool's arguments are OPAQUE "
                        "IDENTIFIERS (an id, a UUID, a message key) instead of human-legible "
                        "text — the approval card then says WHAT is being acted on rather "
                        "than showing a raw token, and you MUST write the AISH_TOOL_PREVIEW "
                        "branch described under 'wrapper'. Example: reminders_delete takes "
                        "id='F5D0CC92-…'; in preview mode its wrapper runs `rem show <id>` "
                        "and prints \"Delete 'aish test — EDITED' (list Online, due Fri Jul "
                        "31 9:00, flagged)\", so the user approves a reminder, not a token. "
                        "Leave it false when every argument already explains itself (a "
                        "title, a message body).",
                    },
                },
                "required": ["name", "description", "mutating", "schema", "wrapper", "returns"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_skill",
            "description": (
                "Import a skill (a playbook) from a git repository or local path into "
                "the user's skills. Use when the user asks to add/install a skill from a "
                "public repo (e.g. anthropics/skills, VoltAgent/awesome-agent-skills). "
                "SAFETY: an imported skill is untrusted content — its instructions and "
                "scripts are what you'd later follow — so aish shows the user EVERY file "
                "for approval before anything is installed (you cannot skip this). Only a "
                "shallow read-only clone happens; the skill's code is never executed on "
                "import. After staging you should summarize for the user what the skill "
                "does and what its scripts do, so they can review before approving."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "A git URL (https/ssh) or a local directory path.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Subdirectory within the repo that holds the skill "
                        "(the folder containing SKILL.md). Omit if it's at the repo root.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional: install under this name instead of the "
                        "skill's declared name.",
                    },
                },
                "required": ["repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_rule",
            "description": (
                "Write a RULE — a standing instruction aish ENFORCES on you, unlike a "
                "skill or a memory, which only inform you. Create one when the user says "
                "something should ALWAYS or NEVER happen ('always use show_image', 'never "
                "search the web when I give you a link'). For a one-off, just do it; for "
                "a fact about them or their world, use remember instead. "
                "You do NOT write the file and you do NOT write YAML: name the field "
                "values below and aish renders, validates and shows the user what it "
                "MEANS before anything is saved. If the rule cannot be expressed in these "
                "fields, say so and tell the user exactly what could not be expressed — "
                "that is a feature request for aish, not a reason to write vague prose. "
                "RULES ONLY RESTRICT: there is no verb that grants permission or "
                "auto-approves anything, by design."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short kebab-case name, e.g. 'bounded-material'. "
                        "It is how the user will refer to the rule.",
                    },
                    "description": {
                        "type": "string",
                        "description": "One line: what the rule requires, in the user's "
                        "own terms. Shown whenever the rule binds.",
                    },
                    "when_subject": {"type": "string", "description": _WHEN_SUBJECT},
                    "when_has": {
                        "type": "string",
                        "description": "For when_subject='prompt': what the message must "
                        "carry — 'source' (any material: a link, an attachment or a typed "
                        "path), 'link', 'attachment' or 'path'.",
                    },
                    "when_matches": {
                        "type": "string",
                        "description": "For when_subject='prompt': a regex the message "
                        "must match. Prefer when_has — a keyword regex fires on 'the "
                        "Docker image is broken' and is the classic way to write a rule "
                        "that binds the wrong turns.",
                    },
                    "when_origin": {
                        "type": "string",
                        "description": "For when_subject='session': 'owner' (the user is "
                        "there) or 'automation' (nobody is).",
                    },
                    "when_action": {
                        "type": "object",
                        "description": "For when_subject='action': any of tool, "
                        "path_under, command_starts_with. All named conditions must hold.",
                    },
                    "answer_from": {
                        "type": "string",
                        "description": "A tool name, or 'source' meaning the material the "
                        "user handed over (aish picks the right reader for each kind). "
                        "Everything else is then refused for this answer.",
                    },
                    "never_use": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tool names that must not run on a matching turn.",
                    },
                    "must_first": {
                        "type": "string",
                        "description": "A tool that must have RUN before the answer is "
                        "delivered. Checked at the end of the turn against what actually "
                        "happened, not against what you say happened.",
                    },
                    "answer_must_include": {
                        "type": "string",
                        "description": "A named check on the finished answer: "
                        "'links_to_what_you_read'. A plain phrase is NOT accepted — a "
                        "check nothing can evaluate is a promise nothing keeps.",
                    },
                    "answer_must_not": {
                        "type": "string",
                        "description": "A named check the answer must FAIL: "
                        "'raw_image_links'. Same rule — named checks only.",
                    },
                    "must_tell_me_when": {
                        "type": "string",
                        "description": "A failure the user must be told about rather than "
                        "quietly patched over, e.g. 'the material could not be read'.",
                    },
                    "prose": {
                        "type": "string",
                        "description": "The body: WHY this rule exists, in the user's "
                        "words. Shown to you when the rule binds, so write what a reader "
                        "needs in order to comply well — never the obligation itself, "
                        "which the fields above already enforce.",
                    },
                },
                "required": ["name", "description", "when_subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_rule",
            "description": (
                "Change an existing rule. Name ONLY the fields that change — everything "
                "else is carried over from the file unchanged, so a rule cannot silently "
                "lose what it already did. Never re-state the whole rule: that is how a "
                "working rule gets quietly broken by one sentence of new prose. Same "
                "fields as create_rule."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The rule to change."},
                    "description": {"type": "string"},
                    "when_subject": {"type": "string", "description": _WHEN_SUBJECT},
                    "when_has": {"type": "string"},
                    "when_matches": {"type": "string"},
                    "when_origin": {"type": "string"},
                    "when_action": {"type": "object"},
                    "answer_from": {"type": "string"},
                    "never_use": {"type": "array", "items": {"type": "string"}},
                    "must_first": {"type": "string"},
                    "answer_must_include": {"type": "string"},
                    "answer_must_not": {"type": "string"},
                    "must_tell_me_when": {"type": "string"},
                    "prose": {"type": "string"},
                    "enabled": {
                        "type": "boolean",
                        "description": "false retires the rule; true brings it back.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retire_rule",
            "description": (
                "Stop a rule binding, reversibly — the file stays and the user can bring "
                "it back with edit_rule. Use when the user says a rule is wrong, "
                "annoying, or no longer applies. There is no delete: the rules folder is "
                "the user's own git-backed knowledge, and removing a file from it is "
                "theirs to do."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The rule to retire."},
                },
                "required": ["name"],
            },
        },
    },
]
