"""Conservative read-only command classification for auto-approval.

Philosophy: prompting on a safe command costs one keystroke; auto-approving
an unsafe one costs data. So this parser only approves what it positively
understands — anything ambiguous (unusual metacharacters, unknown binaries,
quoting it can't parse) falls through to the interactive prompt. False
negatives are fine; false positives are not.

Understanding is quote-aware, because `/bin/sh -c` is what runs the command:
a metacharacter inside a single-quoted argument is inert text the shell will
never act on, so refusing it buys no safety and costs every tool whose
argument is a quoted mini-language (jq, awk, find -name).

Chained commands (a | b, a && b, a || b) are split and every segment is
evaluated independently: ALL segments must be read-only or user-allowlisted
for the whole command to auto-approve.
"""

import os
import re
import shlex
import shutil
from collections.abc import Collection
from pathlib import Path

from . import vocab
from .files import contains, is_sensitive_path, resolved, within_roots

DEFAULT_ALLOWLIST = Path.home() / ".config" / "aish" / "allow.txt"
DEFAULT_DENYLIST = Path.home() / ".config" / "aish" / "deny.txt"

# Admin-owned system bin directories. A binary invoked by absolute path from
# one of these is trusted to be the tool its bare name denotes, so it may be
# reduced to that basename for SAFE_COMMANDS / allowlist matching even when the
# directory isn't on the current PATH (issues #16, #28). Writable/untrusted dirs
# and relative paths (./gh) are deliberately excluded — they must still prompt.
# Resolved once so a symlinked trusted dir still compares equal.
_TRUSTED_BIN_DIRS = frozenset(
    os.path.realpath(d)
    for d in ("/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin", "/usr/sbin", "/sbin")
)


class Blocked:
    """Approver verdict for denylisted commands: not executable through the
    model at all — only the user can run these, via the ! prefix."""

    def __init__(self, reason: str):
        self.reason = reason


class Denied:
    """Approver verdict for a refusal that carries the user's explanation
    (typed into the approval card): the action did NOT run, and the comment
    goes back to the model as direct guidance on what to do instead."""

    def __init__(self, comment: str):
        self.comment = comment


class Approved:
    """Approver verdict for an approval that carries feedback typed into the
    card: the action DID run (as `command` if the user edited it), and the
    comment goes to the model as guidance to apply now and going forward."""

    def __init__(self, comment: str, command: str | None = None):
        self.comment = comment
        self.command = command

SAFE_COMMANDS = vocab.declare(
    "approval.SAFE_COMMANDS",
    languages="program names — locale-invariant",
    on_miss=vocab.FRICTION,
    structural="the root scoping and `UNSAFE_FLAGS` beside it — being ON this "
    "list is necessary and never sufficient",
    note="The one list here that is safe by CONSTRUCTION rather than by "
    "judgement: it is an allowlist, so a miss can only ever put a card in front "
    "of the owner. `approval.py`'s standing doctrine — err toward prompting — "
    "is the same statement. Its counter is therefore a use-rate and not a "
    "health check, and it is the highest-volume list in the tree.",
    entries=frozenset(
    {
        "basename",
        "cat",
        # cd is subshell-scoped (execution is stateless — a bare model cd is
        # rejected before it gets here) and its path argument still goes
        # through root scoping, so `cd <in-root-or-trusted> && ...` may
        # auto-approve when the other segments do.
        "cd",
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
    ),
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
# don't model — judged PER QUOTING CONTEXT, never on the raw string. What runs
# the command is `/bin/sh -c` (tools.run_command), so the shell's own rules are
# the only ones that decide whether a character does anything: inside single
# quotes every character is inert, inside double quotes only $ and ` still
# expand, and a backslash-escaped character is literal text. '&' and '|' are
# handled by the chain splitter, ';' stays forbidden unquoted.
FORBIDDEN_CHARS = frozenset(";<>`$(){}\n")
FORBIDDEN_IN_DOUBLE_QUOTES = frozenset("`$")

# How the shell will treat one character.
_BARE, _ESCAPED, _SINGLE, _DOUBLE = "bare", "escaped", "single", "double"

# Inside double quotes a backslash escapes only these; before anything else it
# is a literal backslash. Matching the shell here rather than guessing keeps
# the token values identical to what the command will actually receive.
_DOUBLE_QUOTE_ESCAPES = '$`"\\\n'


class _MalformedCommand(ValueError):
    """A quote or a backslash that never closes. Everything after it means
    something different depending on how it would have closed, so callers fail
    closed rather than guess."""


def _shell_marks(command: str):
    """Yield (position, character, context) for every character the shell will
    see, where context says how it will be treated. The position indexes the
    ORIGINAL string, so a caller can slice raw text back out with its quoting
    intact."""
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\":
            if i + 1 >= n:
                raise _MalformedCommand("trailing backslash")
            yield i, command[i + 1], _ESCAPED
            i += 2
        elif ch == "'":
            end = command.find("'", i + 1)
            if end < 0:
                raise _MalformedCommand("unterminated single quote")
            for offset in range(i + 1, end):
                yield offset, command[offset], _SINGLE
            i = end + 1
        elif ch == '"':
            i += 1
            while True:
                if i >= n:
                    raise _MalformedCommand("unterminated double quote")
                if command[i] == '"':
                    i += 1
                    break
                if command[i] == "\\" and i + 1 < n and command[i + 1] in _DOUBLE_QUOTE_ESCAPES:
                    yield i, command[i + 1], _ESCAPED
                    i += 2
                    continue
                yield i, command[i], _DOUBLE
                i += 1
        else:
            yield i, ch, _BARE
            i += 1


def split_chain(command: str) -> list[str] | None:
    """Split on | , && , || into independently-evaluated segments.
    None means the command uses constructs we don't model — fail closed.

    Quote-aware (#265): an operator separates segments only where the shell
    would treat it as one, so the pipe inside `jq '.listings[0] | keys'` stays
    part of the argument instead of cutting the command into two fragments that
    match nothing.
    """
    try:
        marks = list(_shell_marks(command))
    except _MalformedCommand:
        return None
    segments: list[str] = []
    start = index = 0
    while index < len(marks):
        position, ch, context = marks[index]
        if context == _DOUBLE and ch in FORBIDDEN_IN_DOUBLE_QUOTES:
            return None
        if context != _BARE:
            index += 1
            continue
        if ch in FORBIDDEN_CHARS:
            return None
        if ch in "|&":
            doubled = index + 1 < len(marks) and marks[index + 1] == (position + 1, ch, _BARE)
            if ch == "&" and not doubled:  # stray single & = backgrounding
                return None
            segments.append(command[start:position])
            width = 2 if doubled else 1
            start = position + width
            index += width
            continue
        index += 1
    segments.append(command[start:])
    segments = [s.strip() for s in segments]
    if not segments or any(not s for s in segments):
        return None
    return segments


def _tokens_with_quoting(segment: str) -> list[tuple[str, bool]] | None:
    """(token, was_quoted) pairs. A quoted word is an ARGUMENT however
    word-shaped it looks — the distinction `suggest_prefix` needs and the one
    shlex throws away. None when the quoting doesn't parse."""
    try:
        marks = list(_shell_marks(segment))
    except _MalformedCommand:
        return None
    tokens: list[tuple[str, bool]] = []
    chars: list[str] = []
    quoted = started = False
    for _position, ch, context in marks:
        if context == _BARE and ch.isspace():
            if started:
                tokens.append(("".join(chars), quoted))
                chars, quoted, started = [], False, False
            continue
        started = True
        chars.append(ch)
        quoted = quoted or context != _BARE
    if started:
        tokens.append(("".join(chars), quoted))
    return tokens


def _has_unsafe_flag(name: str, tokens: list[str]) -> bool:
    return any(
        tok == flag or tok.startswith(flag + "=")
        for flag in UNSAFE_FLAGS.get(name, ())
        for tok in tokens[1:]
    )


def _in_trusted_bindir(abs_path: str) -> bool:
    """True when abs_path names a binary living directly in a trusted system bin
    directory. The directory is symlink-resolved (also defusing '..'), but the
    binary itself is not, so a trusted-dir → Cellar Homebrew symlink still
    qualifies while '/opt/homebrew/bin/../../tmp/gh' does not."""
    try:
        return os.path.realpath(os.path.dirname(abs_path)) in _TRUSTED_BIN_DIRS
    except OSError:
        return False


def _resolves_to_path_binary(abs_path: str, name: str) -> bool:
    """True when abs_path is the very same file its bare `name` finds on PATH —
    so a full-path invocation of an on-PATH tool counts as the bare name."""
    on_path = shutil.which(name)
    if not on_path:
        return False
    try:
        return os.path.realpath(abs_path) == os.path.realpath(on_path)
    except OSError:
        return False


def _canonical_tokens(tokens: list[str]) -> list[str]:
    """Rewrite an absolute-path command back to its bare name so SAFE_COMMANDS
    and saved allowlist prefixes match either spelling — but only when the path
    can be trusted to name the expected binary: it lives in a trusted system bin
    directory, OR it resolves to the very same file its bare name finds on PATH.
    A path in a writable/untrusted dir, one that shadows the PATH binary, or a
    relative path (./gh) stays untouched, so it still fails closed everywhere a
    bare name is required."""
    head = os.path.expanduser(tokens[0])
    if not os.path.isabs(head):
        return tokens
    name = head.rsplit("/", 1)[-1]
    if _in_trusted_bindir(head) or _resolves_to_path_binary(head, name):
        return [name, *tokens[1:]]
    return tokens


def _segment_is_safe(segment: str) -> bool:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    if not tokens:
        return False
    tokens = _canonical_tokens(tokens)
    name = tokens[0]
    # Counted here rather than at `is_read_only`, because THIS is the question
    # the list answers: one segment, one verdict. The candidate count is the
    # segment's own token count — what the classifier had in front of it.
    known = name in SAFE_COMMANDS
    vocab.note("approval.SAFE_COMMANDS", matched=known, candidates=len(tokens))
    if not known:
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
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    if not tokens:
        return False
    tokens = _canonical_tokens(tokens)
    # Match the raw segment first (preserves exact-string rules), then the
    # canonical spelling, so '/opt/homebrew/bin/gh pr list' still matches a
    # saved 'gh pr list' — but only because _canonical_tokens proved the path
    # is the same binary PATH would run.
    match = _matched_prefix(segment, prefixes) or _matched_prefix(shlex.join(tokens), prefixes)
    if match is None:
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


def _path_candidate(token: str) -> str | None:
    """The path-like part of a token: the value of a --flag=value token, the
    token itself otherwise, None for bare flags (never paths)."""
    if token.startswith("-"):
        return token.split("=", 1)[1] if "=" in token else None
    return token


def _token_escape(token: str, cwd: str, resolved_roots: list[Path]) -> tuple[bool, Path | None]:
    """Whether a token resolves — symlinks and '..' defused — outside every
    session root. A relative token with no '~'/'..'/absolute anchor can only
    escape via a symlink, so it is resolved only when it names something on
    disk: a token naming nothing (grep patterns, command words, files yet to
    be created) can't leak anything and keeps auto-approving un-stat'ed.
    Returns (escapes, resolved target) — target is provided whenever
    resolution succeeded, also for in-root tokens, so callers can run further
    checks on it (sensitivity); it is None when the token isn't path-like or
    the escape can't be resolved (fail closed, but nothing to offer trust on)."""
    candidate = _path_candidate(token)
    if candidate is None:
        return False, None
    expanded = os.path.expanduser(candidate)
    if (
        not os.path.isabs(expanded)
        and ".." not in Path(candidate).parts
        and not os.path.lexists(os.path.join(cwd, expanded))
    ):
        return False, None
    target = resolved(candidate, cwd)
    if target is None:
        return True, None
    return not within_roots(resolved_roots, target), target


def _token_needs_prompt(token: str, cwd: str, resolved_roots: list[Path]) -> bool:
    """Escaping the roots forces a prompt; so does resolving to a path that
    commonly holds credentials (mirrors the read_file sensitivity prompt —
    `cat link-to-ssh-key` must not slip a secret through the shell path)."""
    escaped, target = _token_escape(token, cwd, resolved_roots)
    if escaped:
        return True
    return target is not None and is_sensitive_path(str(target), cwd)


def _segment_escapes_roots(segment: str, cwd: str, resolved_roots: list[Path]) -> bool:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return True
    return any(_token_needs_prompt(t, cwd, resolved_roots) for t in tokens[1:])


def paths_escape_roots(command: str, cwd: str, roots) -> bool:
    """True when the command's cwd or any path-like argument resolves outside
    every session root (symlinks defused) or onto a sensitive path — such
    commands prompt instead of auto-approving, so a read-only verb can't
    quietly pull files (or credentials) from elsewhere on the machine."""
    try:
        resolved_roots = [Path(r).resolve() for r in roots]
    except OSError:
        return True
    if not within_roots(resolved_roots, cwd):
        return True
    segments = split_chain(command)
    if segments is None:
        return True
    return any(_segment_escapes_roots(s, cwd, resolved_roots) for s in segments)


def escaping_dirs(command: str, cwd: str, roots) -> list[str]:
    """Best-effort list of directories outside every session root that the
    command's cwd or path-like arguments resolve into — what a
    'trust this directory for this session' prompt should offer. Unlike
    paths_escape_roots this is advisory display data, never a gate: escapes
    that can't be resolved to a concrete path are simply omitted."""
    dirs: list[str] = []

    def offer(target: Path) -> None:
        try:
            directory = target if target.is_dir() else target.parent
        except OSError:
            return
        if str(directory) not in dirs:
            dirs.append(str(directory))

    try:
        resolved_roots = [Path(r).resolve() for r in roots]
    except OSError:
        return []
    resolved_cwd = resolved(cwd)
    if resolved_cwd is not None and not within_roots(resolved_roots, resolved_cwd):
        offer(resolved_cwd)
    for segment in split_chain(command) or []:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        for token in tokens[1:]:
            escaped, target = _token_escape(token, cwd, resolved_roots)
            if escaped and target is not None:
                offer(target)
    return dirs


def path_within(path: str, cwd: str, scratch_dir: Path) -> bool:
    """True iff `path` resolves to a location STRICTLY inside scratch_dir
    (symlinks and '..' defused). Backs auto-approval of writes into the
    ephemeral scratch workspace; the scratch dir itself and anything that
    escapes it (or can't be resolved) return False — fail closed."""
    return contains(scratch_dir, path, cwd, strict=True)


def is_scratch_delete(command: str, cwd: str, scratch_dir: Path) -> bool:
    """True iff `command` is a single `rm` invocation whose every path operand
    resolves STRICTLY inside scratch_dir — a delete confined to the ephemeral
    scratch workspace, safe to auto-approve. Fail closed on ANY ambiguity, so
    the command otherwise drops through to the normal denylist + prompt path:
    chained/piped commands or shell metacharacters, a verb that isn't a bare
    `rm` (no sudo/wrappers), recursive+force together (that stays denylisted
    even here), no operands, or any operand resolving onto or outside the
    scratch dir."""
    segments = split_chain(command)
    if segments is None or len(segments) != 1:
        return False
    try:
        tokens = shlex.split(segments[0])
    except ValueError:
        return False
    if not tokens or tokens[0].rsplit("/", 1)[-1] != "rm":
        return False
    operands: list[str] = []
    flags: set[str] = set()
    longs: set[str] = set()
    after_ddash = False
    for tok in tokens[1:]:
        if not after_ddash and tok == "--":
            after_ddash = True
            continue
        if not after_ddash and tok != "-" and tok.startswith("-"):
            if tok.startswith("--"):
                longs.add(tok)
            else:
                flags.update(tok[1:])
            continue
        operands.append(tok)
    recursive = bool({"r", "R"} & flags) or "--recursive" in longs
    force = "f" in flags or "--force" in longs
    if recursive and force:  # rm -rf stays denylisted, even inside scratch
        return False
    if not operands:
        return False
    return all(path_within(operand, cwd, scratch_dir) for operand in operands)


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

# The two Keychain services aish keeps. Matched as whole argv tokens, so a file
# that merely mentions one is untouched.
_KEYCHAIN_SERVICES = frozenset({"aish", "aish-signin"})

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
    if name == "security" and any(t.strip() in _KEYCHAIN_SERVICES for t in tokens[1:]):
        # aish's own Keychain namespaces (#142, #280). Everything else in this
        # denylist is here for being UNRECOVERABLE; this one is here for the
        # other reason a command may have no yes — reading it hands the model
        # the owner's stored passwords, which every other fence in the browser
        # exists to keep out of its context. A card would not help: the owner
        # has said he will not read one, and this is precisely the request that
        # looks reasonable in passing.
        return (
            "aish's own Keychain: reading it would put the owner's stored "
            "passwords into the model's context"
        )
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


_DESTRUCTIVE_COMMANDS = vocab.declare(
    "approval._DESTRUCTIVE_COMMANDS",
    demanded=False,  # one ask per command, and most commands are not
    # destructive — `sudo`/`--force` mark one without this list matching
    languages="program names — locale-invariant",
    on_miss=vocab.BREAKS,
    structural="none, and none is needed — the GATE is `check_denied` and the "
    "approval card itself, neither of which consults this list",
    note="ADVISORY ONLY: it decides whether the prompt carries a red "
    "'destructive' marker, never whether the command runs. So a miss removes a "
    "warning from a card the owner still has to approve — it permits nothing, "
    "and it costs no friction either. The counter is the only thing that would "
    "say it had stopped firing.",
    entries={
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
    },
)


def looks_destructive(command: str) -> bool:
    """Whether to show the red warning at the prompt — advisory only, never a
    substitute for the gate. Keyed on command VERBS (rm, mv, kill, sudo, …),
    NOT on redirects: `2>/dev/null` and a `>` inside a quoted awk/sed program
    are not destructive, and flagging them just breeds approval fatigue."""
    verdict, by_list, verbs = _destructive_verdict(command)
    # One consultation per COMMAND, not per segment, so a pipeline does not read
    # as ten questions; `verbs` is how many command verbs the scan actually got
    # to look at. `by_list` and not `verdict`: `sudo` and `--force` mark a
    # command destructive without this list matching anything, and folding them
    # in would credit the list with catches it did not make.
    vocab.note("approval._DESTRUCTIVE_COMMANDS", matched=by_list, candidates=verbs)
    return verdict


def _destructive_verdict(command: str) -> tuple[bool, bool, int]:
    """(destructive, the LIST said so, how many verbs were examined).

    Split out from `looks_destructive` so the counter can tell the list's own
    catches apart from the two tests beside it, without a flag threaded through
    the loop."""
    verbs = 0
    for segment in _deny_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue  # can't parse → don't cry wolf
        if tokens and tokens[0].rsplit("/", 1)[-1] == "sudo":
            return True, False, verbs
        tokens = _strip_wrappers(tokens)
        if not tokens:
            continue
        verbs += 1
        name = tokens[0].rsplit("/", 1)[-1]
        if name in _DESTRUCTIVE_COMMANDS:
            return True, True, verbs
        if "--force" in tokens[1:]:  # explicit only; bare -f means "file" too often
            return True, False, verbs
    return False, False, verbs


# CLIs whose static command path nests deeper than one subcommand level:
# how many tokens after the binary can belong to the path ('gh issue create'
# = 2). A ceiling, not a fill — collection still stops at the first flag or
# dynamic-looking argument. Unlisted tools keep one level ('git status').
SUBCOMMAND_DEPTH = {
    "aws": 2,       # aws s3 ls
    "az": 2,
    "docker": 2,    # docker compose up
    "gcloud": 3,    # gcloud compute instances list
    "gh": 2,        # gh issue create
    "kubectl": 2,   # kubectl get pods
    "npm": 2,       # npm run dev
    "pnpm": 2,
    "podman": 2,
    "uv": 2,        # uv pip install
    "yarn": 2,
}

# A token that can be part of a command path: word-ish, no '/', '=', or
# leading '-'/'.'  — flags, paths, and KEY=value assignments are dynamic
# arguments, not subcommands. Interior dots stay allowed so an exec-wrapper
# script name ('python manage.py') still scopes the rule.
_SUBCOMMAND_WORD = re.compile(r"^[A-Za-z0-9][\w.-]*$")


def suggest_prefix(segment: str) -> str:
    """Default 'always allow' rule: the static command path — the binary's
    basename plus its subcommand words ('gh issue create'), stopping at the
    first flag or dynamic argument. Scopes the rule to a subcommand instead
    of a whole binary, so allowlisting 'gh issue create' never waves through
    'gh repo delete'.

    A QUOTED word is an argument however word-shaped it looks, so it never
    becomes part of the rule: `jq 'keys' data.json` suggests `jq`, not the
    per-invocation `jq keys` that would need re-approving on the next filter.
    """
    quoted_tokens = _tokens_with_quoting(segment)
    if quoted_tokens is None:  # unparseable quoting — best effort, still asked
        quoted_tokens = [(word, False) for word in segment.split()]
    if not quoted_tokens:
        return segment.strip()
    binary = quoted_tokens[0][0].rsplit("/", 1)[-1]
    parts = [binary]
    for token, was_quoted in quoted_tokens[1 : 1 + SUBCOMMAND_DEPTH.get(binary, 1)]:
        if was_quoted or not _SUBCOMMAND_WORD.match(token):
            break
        parts.append(token)
    return " ".join(parts)


def prefix_suggestions(command: str, prefixes: Collection[str]) -> list[str]:
    """The 'always allow' / 'allow this session' rules to offer for a command
    that prompted — EMPTY when no rule could ever silence it.

    A command the parser cannot model is refused BEFORE the allowlist is
    consulted, and so is one whose paths escape the session roots. Offering
    'Always' there promises something the gate will not honour, and the prefix
    it derives from an unparsed command is junk that lands in allow.txt
    forever: `jq '.listings[0] | keys' f.json` was cut inside its own filter
    and saved a rule named `keys'` (#265). Both callers show the buttons only
    when this returns something.
    """
    if split_chain(command) is None:
        return []
    return [suggest_prefix(segment) for segment in unvetted_segments(command, prefixes)]


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
