"""Conservative read-only command classification for auto-approval.

Philosophy: prompting on a safe command costs one keystroke; auto-approving
an unsafe one costs data. So this parser only approves what it positively
understands — anything ambiguous (unusual metacharacters, unknown binaries,
quoting it can't parse, a quoted '|' that confuses the raw split) falls
through to the interactive prompt. False negatives are fine; false
positives are not.
"""

import shlex

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
        "printf",
        "ps",
        "pwd",
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

# Anything enabling chaining, redirection, substitution, or expansion we
# can't statically reason about. Scanned on the raw string, so quoting or
# escaping can't hide these from us (at worst we reject a safe command).
FORBIDDEN_CHARS = frozenset(";&<>`$(){}\n")


def is_read_only(command: str) -> bool:
    """True only if every pipeline segment is a positively-known safe command."""
    if any(ch in FORBIDDEN_CHARS for ch in command):
        return False

    for segment in command.split("|"):
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
