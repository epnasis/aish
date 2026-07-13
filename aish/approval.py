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
DEFAULT_DENYLIST = Path.home() / ".config" / "aish" / "deny.txt"


class Blocked:
    """Approver verdict for denylisted commands: not executable through the
    model at all — only the user can run these, via the ! prefix."""

    def __init__(self, reason: str):
        self.reason = reason

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


# Wrappers that don't change what the underlying command does.
_WRAPPERS = ("sudo", "nohup", "time", "command")

_DISKUTIL_DESTRUCTIVE = {
    "erasedisk",
    "erasevolume",
    "zerodisk",
    "reformat",
    "partitiondisk",
    "secureerase",
}

# rm with recursive+force in either order, even inside strings we can't
# fully parse (unquoted ;, subshells, ...). Fail closed on the worst one.
_RAW_RM_RF_RE = re.compile(
    r"(?:^|[;&|`$(]\s*)(?:sudo\s+)?rm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*[fF]|-[a-zA-Z]*[fF][a-zA-Z]*[rR])"
)


def _strip_wrappers(tokens: list[str]) -> list[str]:
    while tokens and tokens[0].rsplit("/", 1)[-1] in _WRAPPERS:
        tokens = tokens[1:]
    return tokens


def _flag_letters(tokens: list[str]) -> set[str]:
    letters: set[str] = set()
    for token in tokens[1:]:
        if token.startswith("-") and not token.startswith("--"):
            letters.update(token[1:])
    return letters


def _segment_deny_reason(segment: str) -> str | None:
    """Built-in denylist: command classes whose effects are not recoverable."""
    try:
        tokens = _strip_wrappers(shlex.split(segment))
    except ValueError:
        return None  # unparseable → the raw regex scan is the safety net
    if not tokens:
        return None
    name = tokens[0].rsplit("/", 1)[-1]
    flags = _flag_letters(tokens)
    longs = {t for t in tokens[1:] if t.startswith("--")}

    if name == "rm":
        recursive = bool({"r", "R"} & flags) or "--recursive" in longs
        force = "f" in flags or "--force" in longs
        if recursive and force:
            return "rm -rf: recursive force delete is unrecoverable"
    if name in ("shred", "srm"):
        return f"{name}: secure deletion is unrecoverable"
    if name.startswith("mkfs"):
        return "mkfs: formatting a filesystem is unrecoverable"
    if name == "dd" and any(t.startswith("of=/dev/") for t in tokens[1:]):
        return "dd writing to a raw device is unrecoverable"
    if name == "diskutil" and len(tokens) > 1 and tokens[1].lower() in _DISKUTIL_DESTRUCTIVE:
        return "diskutil erase/partition is unrecoverable"
    if name == "git" and len(tokens) > 1:
        subcommand = next((t for t in tokens[1:] if not t.startswith("-")), "")
        if subcommand == "clean" and ("f" in flags or "--force" in longs):
            return "git clean -f deletes untracked files unrecoverably"
        if subcommand == "push" and ("--force" in longs or "f" in flags):
            if "--force-with-lease" not in longs:
                return "git push --force can destroy remote history"
    return None


def check_denied(command: str, extra_prefixes: list[str] | None = None) -> str | None:
    """Reason string if the command hits the denylist, else None.
    User prefixes from deny.txt match segments the same way allow.txt does."""
    segments = split_chain(command)
    if segments is None:
        if _RAW_RM_RF_RE.search(command):
            return "rm -rf inside a compound command"
        return None
    for segment in segments:
        reason = _segment_deny_reason(segment)
        if reason:
            return reason
        for prefix in extra_prefixes or ():
            if segment == prefix or segment.startswith(prefix + " "):
                return f"matches your denylist entry '{prefix}'"
    return None


_DESTRUCTIVE_COMMANDS = {
    "chmod",
    "chown",
    "dd",
    "kill",
    "killall",
    "launchctl",
    "mv",
    "pkill",
    "reboot",
    "rm",
    "shutdown",
    "truncate",
}


def looks_destructive(command: str) -> bool:
    """Cheap heuristic for the approval prompt's red warning — advisory only,
    never a substitute for the gate itself."""
    if ">" in command or "<" in command:
        return True
    for segment in split_chain(command) or [command]:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return True
        if tokens and tokens[0].rsplit("/", 1)[-1] == "sudo":
            return True
        tokens = _strip_wrappers(tokens)
        if not tokens:
            continue
        if tokens[0].rsplit("/", 1)[-1] in _DESTRUCTIVE_COMMANDS:
            return True
        if "--force" in tokens:
            return True
    return False


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
