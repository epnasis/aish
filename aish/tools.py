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


def run_command(command: str, timeout: float = 120) -> str:
    """Execute a shell command; return combined output and exit status."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"

    parts = []
    if result.stdout:
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr.rstrip()}")
    parts.append(f"[exit code: {result.returncode}]")
    return truncate("\n".join(parts))


def read_docs(command: str) -> str:
    """Look up documentation for a command: man page, then --help, then -h."""
    name = command.strip()
    if not COMMAND_NAME_RE.match(name):
        return (
            f"ERROR: read_docs takes a bare command name (got {name!r}). "
            "Pass a single command name with no arguments or shell syntax."
        )

    quoted = shlex.quote(name)
    man = subprocess.run(
        f"man {quoted} 2>/dev/null | col -b",
        shell=True,
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )
    if man.stdout.strip():
        return truncate(f"[man {name}]\n{man.stdout.strip()}", head=DOCS_MAX_CHARS, tail=0)

    if shutil.which(name) is None:
        return f"ERROR: '{name}' not found on this system (no man page, not in PATH)."

    for flag in ("--help", "-h"):
        try:
            help_run = subprocess.run(
                [name, flag],
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        output = (help_run.stdout + help_run.stderr).strip()
        if output:
            return truncate(f"[{name} {flag}]\n{output}", head=DOCS_MAX_CHARS, tail=0)

    return (
        f"NO DOCUMENTATION FOUND for '{name}' (tried man, --help, -h). "
        "Proceed with maximum caution: use only flags you are certain of, "
        "or tell the user documentation is unavailable."
    )


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_docs",
            "description": (
                "Read the documentation for a CLI command (man page, falling back to "
                "--help / -h). ALWAYS call this before using a command whose flags you "
                "are not completely certain about, and after any usage/unknown-flag error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bare command name only, e.g. 'tar' — no arguments.",
                    }
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
