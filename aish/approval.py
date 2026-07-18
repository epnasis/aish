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

import os
import re
import shlex
from collections.abc import Collection
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

# Commands whose arguments are arbitrary code or another command. A bare-binary
# allowlist prefix on any of these would silently grant arbitrary execution, so
# such a prefix never auto-approves — only an explicitly narrower saved prefix
# (e.g. `python manage.py`, not `python`) may.
EXEC_WRAPPERS = frozenset(
    {
        "python", "python2", "python3", "bash", "sh", "zsh", "fish", "dash", "ksh",
        "perl", "ruby", "node", "deno", "bun", "php", "lua", "awk", "gawk",
        "xargs", "env", "eval", "exec", "nice", "timeout", "watch", "ssh", "make",
    }
)

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


def _has_unsafe_flag(name: str, tokens: list[str]) -> bool:
    return any(
        tok == flag or tok.startswith(flag + "=")
        for flag in UNSAFE_FLAGS.get(name, ())
        for tok in tokens[1:]
    )


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
    return not _has_unsafe_flag(name, tokens)


def _matched_prefix(segment: str, prefixes: Collection[str]) -> str | None:
    return next((p for p in prefixes if segment == p or segment.startswith(p + " ")), None)


def _matches_prefix(segment: str, prefixes: Collection[str]) -> bool:
    return _matched_prefix(segment, prefixes) is not None


def _prefix_approves(segment: str, prefixes: Collection[str]) -> bool:
    """A user allowlist prefix auto-approves a segment only if it does not smuggle
    in a write/exec flag or resolve to an interpreter that the bare prefix would
    otherwise wave through. Fixes the hole where allow-listing a benign `find`
    (or `python`) silently granted `find -delete` / arbitrary code."""
    match = _matched_prefix(segment, prefixes)
    if match is None:
        return False
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    if not tokens:
        return False
    name = tokens[0].rsplit("/", 1)[-1]
    if _has_unsafe_flag(name, tokens) or _has_unsafe_flag(tokens[0], tokens):
        return False
    # A bare-binary prefix cannot authorize an interpreter/exec wrapper; require
    # a saved prefix of at least two tokens (an explicitly scoped subcommand).
    if name in EXEC_WRAPPERS and len(match.split()) < 2:
        return False
    return True


def is_read_only(command: str) -> bool:
    """True only if every chained segment is a positively-known safe command."""
    segments = split_chain(command)
    return segments is not None and all(_segment_is_safe(s) for s in segments)


def _within_roots(target: Path, resolved_roots: list[Path]) -> bool:
    return any(target.is_relative_to(r) for r in resolved_roots)


def _token_escapes(token: str, cwd: str, resolved_roots: list[Path]) -> bool:
    """Conservative: only tokens that could name a path outside the session
    roots trip this — absolute, ~-anchored, or containing '..'. Plain relative
    tokens can't leave the cwd (itself verified to be under a root)."""
    candidate = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
    if token.startswith("-") and candidate is token:
        return False
    expanded = os.path.expanduser(candidate)
    anchored = os.path.isabs(expanded)
    if not anchored and ".." not in Path(candidate).parts:
        return False
    try:
        target = (Path(expanded) if anchored else Path(cwd) / expanded).resolve()
        return not _within_roots(target, resolved_roots)
    except OSError:
        return True


def _segment_escapes_roots(segment: str, cwd: str, resolved_roots: list[Path]) -> bool:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return True
    return any(_token_escapes(t, cwd, resolved_roots) for t in tokens[1:])


def paths_escape_roots(command: str, cwd: str, roots) -> bool:
    """True when the command's cwd or any path-like argument resolves outside
    every session root — such commands prompt instead of auto-approving, so a
    read-only verb can't quietly pull files from elsewhere on the machine."""
    try:
        resolved_roots = [Path(r).resolve() for r in roots]
        if not _within_roots(Path(cwd).resolve(), resolved_roots):
            return True
    except OSError:
        return True
    segments = split_chain(command)
    if segments is None:
        return True
    return any(_segment_escapes_roots(s, cwd, resolved_roots) for s in segments)


def is_auto_approvable(
    command: str, prefixes: Collection[str], cwd: str | None = None, roots=None
) -> bool:
    """True if EVERY chained segment is independently read-only or matches a
    user-persisted prefix. One unvetted segment means the whole command prompts.
    When cwd/roots are given, path arguments escaping the roots also force a
    prompt — and the user allowlist never bypasses that check."""
    segments = split_chain(command)
    if segments is None:
        return False
    if cwd is not None and roots and paths_escape_roots(command, cwd, roots):
        return False
    return all(_segment_is_safe(s) or _prefix_approves(s, prefixes) for s in segments)


def unvetted_segments(command: str, prefixes: Collection[str]) -> list[str]:
    """The segments that would still need a prompt — what the 'always allow'
    flow should ask about, one by one."""
    segments = split_chain(command)
    if segments is None:
        return []
    return [s for s in segments if not (_segment_is_safe(s) or _prefix_approves(s, prefixes))]


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


# Shell sequencing/pipe operators. Unlike split_chain's FORBIDDEN_CHARS, this
# splits even when redirects or subshells are present — the denylist must
# inspect every verb, not fail open the moment it sees a metacharacter.
_DENY_SPLIT = re.compile(r"[;\n]|\|\|?|&&?")
_SHELL_NAMES = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_CMD_WRAPPERS = frozenset(
    {"env", "xargs", "nohup", "time", "command", "nice", "timeout", "sudo", "stdbuf"}
)
_FIND_EXEC_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _find_exec_commands(tokens: list[str]) -> list[str]:
    """The command(s) `find ... -exec <cmd> ... {} ;/+` would run."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in _FIND_EXEC_FLAGS:
            cmd, j = [], i + 1
            while j < len(tokens) and tokens[j] not in (";", "+"):
                if tokens[j] != "{}":
                    cmd.append(tokens[j])
                j += 1
            if cmd:
                out.append(shlex.join(cmd))
            i = j
        i += 1
    return out


def _unwrap_exec(segment: str) -> list[str]:
    """Command string(s) embedded inside an exec wrapper, so the denylist can
    see through `sh -c '...'`, `xargs rm`, `env VAR=x cmd`, `find -exec ...`."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return []
    if not tokens:
        return []
    name = tokens[0].rsplit("/", 1)[-1]
    if name in _SHELL_NAMES and "-c" in tokens:
        idx = tokens.index("-c")
        return [tokens[idx + 1]] if idx + 1 < len(tokens) else []
    if name == "find":
        return _find_exec_commands(tokens)
    if name in _CMD_WRAPPERS:
        rest = tokens[1:]
        while rest and (rest[0].startswith("-") or (name == "env" and "=" in rest[0])):
            rest = rest[1:]
        return [shlex.join(rest)] if rest else []
    return []


def _collect_deny_segments(command: str, out: list[str], depth: int) -> None:
    if depth > 6:  # bound recursion through nested wrappers
        return
    for piece in _DENY_SPLIT.split(command):
        piece = piece.strip()
        if not piece:
            continue
        out.append(piece)
        for inner in _unwrap_exec(piece):
            _collect_deny_segments(inner, out, depth + 1)


def _deny_segments(command: str) -> list[str]:
    out: list[str] = []
    _collect_deny_segments(command, out, 0)
    return out


def check_denied(command: str, extra_prefixes: list[str] | None = None) -> str | None:
    """Reason string if the command hits the denylist, else None.
    User prefixes from deny.txt match segments the same way allow.txt does."""
    for segment in _deny_segments(command):
        reason = _segment_deny_reason(segment)
        if reason:
            return reason
        for prefix in extra_prefixes or ():
            if segment == prefix or segment.startswith(prefix + " "):
                return f"matches your denylist entry '{prefix}'"
    # Last-resort net for rm -rf hidden in forms we couldn't segment cleanly
    # (unbalanced quoting, exotic substitution).
    if _RAW_RM_RF_RE.search(command):
        return "rm -rf inside a compound command"
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
    """Whether to show the red warning at the prompt — advisory only, never a
    substitute for the gate. Keyed on command VERBS (rm, mv, kill, sudo, …),
    NOT on redirects: `2>/dev/null` and a `>` inside a quoted awk/sed program
    are not destructive, and flagging them just breeds approval fatigue."""
    for segment in _deny_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue  # can't parse → don't cry wolf
        if tokens and tokens[0].rsplit("/", 1)[-1] == "sudo":
            return True
        tokens = _strip_wrappers(tokens)
        if not tokens:
            continue
        name = tokens[0].rsplit("/", 1)[-1]
        if name in _DESTRUCTIVE_COMMANDS:
            return True
        if "--force" in tokens[1:]:  # explicit only; bare -f means "file" too often
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
