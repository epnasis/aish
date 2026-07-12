"""Conservative read-only command classification for auto-approval.

Philosophy: prompting on a safe command costs one keystroke; auto-approving
an unsafe one costs data. So this parser only approves what it positively
understands — anything ambiguous (unusual metacharacters, unknown binaries,
quoting it can't parse, a quoted '|' that confuses the raw split) falls
through to the interactive prompt. False negatives are fine; false
positives are not.

Chained commands (a | b, a && b, a || b) are split and every segment is
evaluated independently: ALL segments must be read-only or user-allowlisted
for the whole command to auto-approve.
"""

import re
import shlex
from pathlib import Path

DEFAULT_ALLOWLIST = Path.home() / ".config" / "aish" / "allow.txt"

SAFE_COMMANDS = frozenset(
    {
        "basename",
        "cat",
        "column",
        "cut",
        "date",
        "df",
        "dirname",
        "du",
        "echo",
        "file",
        "find",
        "grep",
        "head",
        "id",
        "ls",
        "man",
        "md5",
        "md5sum",
        "printf",
        "ps",
        "pwd",
        "sha256sum",
        "shasum",
        "sort",
        "stat",
        "tail",
        "tr",
        "type",
        "uname",
        "uptime",
        "wc",
        "which",
        "whoami",
    }
)

# Otherwise-safe commands with flags that write or execute.
UNSAFE_FLAGS = {
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"),
    "sort": ("-o", "--output"),
}

# Anything enabling redirection, substitution, expansion, or sequencing we
# don't model. Scanned on the raw string, so quoting or escaping can't hide
# these from us (at worst we reject a safe command). '&' and '|' are handled
# by the chain splitter, ';' stays forbidden.
FORBIDDEN_CHARS = frozenset(";<>`$(){}\n")

_CHAIN_SPLIT = re.compile(r"\|\||&&|\|")


def split_chain(command: str) -> list[str] | None:
    """Split on | , && , || into independently-evaluated segments.
    None means the command uses constructs we don't model — fail closed."""
    if any(ch in FORBIDDEN_CHARS for ch in command):
        return None
    if "&" in command.replace("&&", ""):  # stray single & = backgrounding
        return None
    segments = [s.strip() for s in _CHAIN_SPLIT.split(command)]
    if not segments or any(not s for s in segments):
        return None
    return segments


def _segment_is_safe(segment: str) -> bool:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    if not tokens:
        return False
    name = tokens[0]
    if name not in SAFE_COMMANDS:
        return False
    for flag in UNSAFE_FLAGS.get(name, ()):
        if any(tok == flag or tok.startswith(flag + "=") for tok in tokens[1:]):
            return False
    return True


def _matches_prefix(segment: str, prefixes: list[str]) -> bool:
    return any(segment == p or segment.startswith(p + " ") for p in prefixes)


def is_read_only(command: str) -> bool:
    """True only if every chained segment is a positively-known safe command."""
    segments = split_chain(command)
    return segments is not None and all(_segment_is_safe(s) for s in segments)


def is_auto_approvable(command: str, prefixes: list[str]) -> bool:
    """True if EVERY chained segment is independently read-only or matches a
    user-persisted prefix. One unvetted segment means the whole command prompts."""
    segments = split_chain(command)
    if segments is None:
        return False
    return all(_segment_is_safe(s) or _matches_prefix(s, prefixes) for s in segments)


def unvetted_segments(command: str, prefixes: list[str]) -> list[str]:
    """The segments that would still need a prompt — what the 'always allow'
    flow should ask about, one by one."""
    segments = split_chain(command)
    if segments is None:
        return []
    return [s for s in segments if not (_segment_is_safe(s) or _matches_prefix(s, prefixes))]


def suggest_prefix(segment: str) -> str:
    """Default 'always allow' rule: first two tokens ('git status'), unless
    the second is a value-ish argument starting with '-' or a path. Two tokens
    scope the rule to a subcommand instead of a whole binary."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    if len(tokens) >= 2 and not tokens[1].startswith(("-", "/", "~", ".")):
        return f"{tokens[0]} {tokens[1]}"
    return tokens[0] if tokens else segment.strip()


def load_prefixes(path: Path) -> list[str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def save_prefix(path: Path, prefix: str) -> None:
    prefix = prefix.strip()
    if not prefix or prefix in load_prefixes(path):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(prefix + "\n")
