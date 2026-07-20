"""Tool implementations: shell execution and documentation lookup.

Security model: run_command executes arbitrary shell strings and therefore
MUST only be reached through the agent's approval gate. read_docs is
auto-approved, so it never accepts a shell string — only a bare command
name, validated and resolved against PATH before anything is executed.
"""

import datetime
import os
import re
import select
import shlex
import shutil
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


def _decode(data: bytes | None) -> str:
    """Commands can emit arbitrary bytes (binary plists, etc.) — never let
    decoding crash the agent."""
    return (data or b"").decode("utf-8", errors="replace")


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
                proc.kill()
                break
            # Cooperative cancel (web UI Stop button): checked once per
            # select slice, so a stop lands within ~0.5s.
            if should_stop is not None and should_stop():
                cancelled = True
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
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
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
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
                },
                "required": ["note"],
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
]
