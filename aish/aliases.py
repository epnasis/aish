"""User-configurable command aliases, expanded BEFORE the approval gate.

Why expand here and not let the shell do it: aish runs every command through a
non-interactive `/bin/sh -c` (see tools.run_command), which never sources the
user's ~/.zshrc, so their shell aliases don't exist. More importantly, the
approval gate (approval.py) classifies a command by PARSING it and a denylist
blocks unrecoverable commands — so the gate must see the REAL command, not an
opaque alias. We therefore keep an aish-owned name→expansion map and rewrite the
command's first word before approval/denylist/execution ever see it. The gate
then classifies `ls -l`, not `ll`, and the user/transcript see what actually ran.

Expansion rule (deliberately NOT a shell): only the first whitespace-delimited
word is expanded, and only on an exact alias-name match; the rest of the command
is preserved verbatim. Leading whitespace, a non-matching first word, or an
empty command all pass through unchanged.
"""

import re
import subprocess
from collections.abc import Mapping

# Alias names we accept from an import. Conservative on purpose: the value is
# reparsed and re-approved anyway, but the NAME becomes a TOML key and a
# first-word match token, so we skip odd shell-only names (`..`, `-`, names with
# spaces or metacharacters). A hand-written config may still use any valid TOML
# key; expansion itself does not require a name to match this.
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")

# First word (no leading whitespace) + the verbatim remainder (its leading
# separator included). DOTALL so a multi-line command's remainder survives.
_FIRST_WORD_RE = re.compile(r"^(\S+)(\s.*)?$", re.DOTALL)

# Backstop for the seen-set loop guard: a pathological config can't spin forever.
_MAX_DEPTH = 32


def expand(command: str, aliases: Mapping[str, str]) -> str:
    """Rewrite the first word of `command` using `aliases`, recursively.

    Recursion lets `ll` → `ls -l` where `ls` might itself be an alias, while a
    seen-set breaks cycles (a=b, b=a) and self-references (the classic
    `ls = "ls --color"` expands exactly once). Anything but a first-word match
    returns the command untouched.
    """
    if not aliases:
        return command
    seen: set[str] = set()
    for _ in range(_MAX_DEPTH):
        # A leading space means the first token isn't at the start; treat the
        # command as opaque rather than guessing where the "command" begins.
        if not command or command[:1].isspace():
            return command
        match = _FIRST_WORD_RE.match(command)
        if match is None:  # unreachable given the leading-space guard, but be safe
            return command
        word = match.group(1)
        rest = match.group(2) or ""
        if word not in aliases or word in seen:
            return command
        seen.add(word)
        command = aliases[word] + rest
    return command


def sanitize(aliases: object) -> dict[str, str]:
    """Keep only well-formed name→string entries from a config table.

    Malformed config (a non-table `aliases`, non-string values, empty
    expansions, odd names) is dropped silently so a typo in config.toml can
    never make a command un-runnable.
    """
    if not isinstance(aliases, Mapping):
        return {}
    clean: dict[str, str] = {}
    for name, value in aliases.items():
        if not isinstance(value, str) or not value.strip():
            continue
        if not NAME_RE.match(name):
            continue
        clean[name] = value
    return clean


def _unquote(value: str) -> str:
    """Undo zsh's single-quoting of an alias value: `'ls -l'` → `ls -l`, with
    its `'\\''` escape for an embedded quote turned back into a bare `'`."""
    value = value.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("'\\''", "'")
    return value


def parse_alias_output(text: str) -> dict[str, str]:
    """Parse the `name=value` lines emitted by zsh's `alias` builtin.

    zsh prints each alias as `name='value'` (single-quoted, `'\\''`-escaped);
    simple values may be unquoted. Names failing NAME_RE (e.g. `..`, `-`) are
    skipped so only clean, TOML-safe entries survive.
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        name, _, raw = line.partition("=")
        name = name.strip()
        if not NAME_RE.match(name):
            continue
        expansion = _unquote(raw)
        if expansion:
            result[name] = expansion
    return result


def import_from_zsh() -> dict[str, str]:
    """Read the user's real aliases via an INTERACTIVE zsh (`zsh -ic 'alias'`),
    which sources ~/.zshrc. Returns {} if zsh is missing or the call fails."""
    try:
        proc = subprocess.run(
            ["zsh", "-ic", "alias"],
            capture_output=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_alias_output(proc.stdout)


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML basic (double-quoted) value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_toml_lines(aliases: Mapping[str, str]) -> list[str]:
    """`name = "value"` lines, name-sorted for a stable, reviewable diff."""
    return [f'{name} = "{_toml_escape(aliases[name])}"' for name in sorted(aliases)]


def merge_into_config_text(text: str, new_aliases: Mapping[str, str]) -> str:
    """Return config text with `new_aliases` added under an `[aliases]` table,
    preserving existing content and comments (so no TOML rewriter needed).

    New keys are inserted directly after an existing `[aliases]` header, or a
    fresh `[aliases]` table is appended at EOF. Callers are expected to have
    already dropped names that exist in config (existing entries always win).
    """
    lines = render_toml_lines(new_aliases)
    if not lines:
        return text
    existing = text.splitlines()
    header_idx = next(
        (i for i, line in enumerate(existing) if line.strip() == "[aliases]"), None
    )
    if header_idx is None:
        block = ["", "[aliases]", *lines]
        body = "\n".join(existing + block)
    else:
        body = "\n".join(existing[: header_idx + 1] + lines + existing[header_idx + 1 :])
    return body + "\n"
