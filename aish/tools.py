"""Tool implementations: shell execution and documentation lookup.

Security model: run_command executes arbitrary shell strings and therefore
MUST only be reached through the agent's approval gate. read_docs is
auto-approved, so it never accepts a shell string — only a bare command
name, validated and resolved against PATH before anything is executed.
"""

import re
import shlex
import shutil
import subprocess
import threading
from collections.abc import Callable

# Enough for the model to work with without blowing a 32k context on one result.
HEAD_CHARS = 4000
TAIL_CHARS = 2000
DOCS_MAX_CHARS = 6000

COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def truncate(text: str, head: int = HEAD_CHARS, tail: int = TAIL_CHARS) -> str:
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[... {omitted} characters omitted ...]\n{text[-tail:]}"


def _decode(data: bytes | None) -> str:
    """Commands can emit arbitrary bytes (binary plists, etc.) — never let
    decoding crash the agent."""
    return (data or b"").decode("utf-8", errors="replace")


def run_command(
    command: str,
    timeout: float = 120,
    cwd: str | None = None,
    on_line: Callable[[str], None] | None = None,
) -> str:
    """Execute a shell command, streaming output lines via on_line as they
    arrive (stderr merged into stdout so ordering is preserved live).

    Ctrl-C cancels the command — not the session — and returns partial output.
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

    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, _on_timeout)
    timer.start()
    lines: list[str] = []
    cancelled = False
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = _decode(raw).rstrip("\n")
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
        timer.cancel()

    parts = []
    output = "\n".join(lines)
    if output.strip():
        parts.append(output)
    if timed_out.is_set():
        parts.append(f"ERROR: command timed out after {timeout}s (any partial output is above)")
    elif cancelled:
        parts.append("[cancelled by user with Ctrl-C — any partial output is above]")
    parts.append(f"[exit code: {proc.returncode}]")
    return truncate("\n".join(parts))


TOPIC_CONTEXT_LINES = 4
TRUNCATION_HINT = (
    "\n[docs truncated — call read_docs again with a 'topic' (e.g. a flag name) "
    "to search the full text]"
)


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


def _fetch_docs(name: str) -> tuple[str, str] | None:
    """Full documentation text and its source label, or None if none exists."""
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

    if shutil.which(name) is None:
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
                    }
                },
                "required": ["command"],
            },
        },
    },
]
