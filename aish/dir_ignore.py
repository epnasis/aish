"""Configurable, gitignore-style ignore list for the web directory picker (#87).

The folder browser (`server.handle_dirs`) and the @-file completion index both
hide build/dependency/cache noise. WHICH names count as noise is user-editable
via config.toml's ``[directory_picker]`` ``ignore`` array; this module owns the
default set, the name-level matcher, and the config write-back that seeds those
defaults so the user can see and edit them (mirroring how aliases are written
back — see aliases.py).

Matching is deliberately SIMPLE and name-level (``fnmatch`` on the basename),
NOT a real gitignore engine: no path-relative rules, no negation, no ``**``. A
trailing ``/`` on a pattern means "directories only". This is a pure in-memory
filter over an already-listed directory — it must never trigger an extra
scandir/stat per subfolder (that caused the #86 server freeze).

Robustness mirrors aliases.py: a missing/blank/malformed config silently
degrades to the built-in defaults, so a config typo can never empty the picker.
"""

import fnmatch
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

# Seeded into config.toml on first use and used whenever the config lacks a
# usable list. Extends prompt.ATFILE_IGNORED_DIRS with common build/tooling
# noise plus macOS/Windows system junk (files as well as dirs — the picker lists
# both). fnmatch globs like "*.egg-info" are allowed.
DEFAULT_IGNORE: tuple[str, ...] = (
    # version control
    ".git", ".hg", ".svn",
    # Python envs / caches
    ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    # JS / build output
    "node_modules", "dist", "build", "*.egg-info", "target",
    ".next", ".cache", ".gradle",
    # editors
    ".idea", ".vscode",
    # OS-generated junk
    ".DS_Store", "Thumbs.db", ".localized",
    ".Trash", ".Spotlight-V100", ".fseventsd",
)

CONFIG_SECTION = "directory_picker"
CONFIG_KEY = "ignore"


def matches(name: str, patterns: Sequence[str], is_dir: bool) -> bool:
    """True if `name` (a bare basename) matches any ignore pattern.

    ``fnmatchcase`` for cross-platform determinism (plain ``fnmatch`` folds case
    per-OS); a trailing ``/`` restricts a pattern to directories."""
    for pat in patterns:
        if pat.endswith("/"):
            if not is_dir:
                continue
            pat = pat[:-1]
        if pat and fnmatch.fnmatchcase(name, pat):
            return True
    return False


def sanitize(patterns: object) -> list[str]:
    """Keep only non-empty string patterns; drop anything malformed silently
    (a non-list, non-string entries, blanks). Mirrors aliases.sanitize."""
    if not isinstance(patterns, (list, tuple)):
        return []
    return [p.strip() for p in patterns if isinstance(p, str) and p.strip()]


def load_patterns(config: Mapping[str, object]) -> list[str]:
    """The ignore list from a parsed config table, or the built-in defaults when
    the section is absent, blank, or malformed."""
    section = config.get(CONFIG_SECTION) if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        cleaned = sanitize(section.get(CONFIG_KEY))
        if cleaned:
            return cleaned
    return list(DEFAULT_IGNORE)


def render_default_block() -> str:
    """The ``[directory_picker]`` TOML table seeded with DEFAULT_IGNORE. The
    default names contain no quotes/backslashes, so no TOML escaping is needed."""
    entries = ",\n".join(f'    "{p}"' for p in DEFAULT_IGNORE)
    return (
        f"[{CONFIG_SECTION}]\n"
        "# gitignore-style names hidden in the web folder browser (#87).\n"
        '# fnmatch globbing (e.g. "*.egg-info"); a trailing / means dirs only.\n'
        f"{CONFIG_KEY} = [\n"
        f"{entries},\n"
        "]"
    )


def merge_default_into_config_text(text: str) -> str | None:
    """Return `text` with a seeded ``[directory_picker]`` table appended, or None
    if the section already exists (never clobber the user's edits)."""
    if any(line.strip() == f"[{CONFIG_SECTION}]" for line in text.splitlines()):
        return None
    body = text.rstrip("\n")
    prefix = body + "\n\n" if body else ""
    return prefix + render_default_block() + "\n"


def seed_config(config_path: Path) -> None:
    """Write DEFAULT_IGNORE into config.toml under ``[directory_picker]`` if the
    user has no such section, so the defaults are visible and editable. Atomic
    write (tmp + replace) with a TOML round-trip guard, matching cli.py's config
    writers. Best-effort: any failure is swallowed — the picker still works off
    the built-in defaults if seeding can't happen."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError:
        return
    merged = merge_default_into_config_text(text)
    if merged is None:
        return
    try:
        tomllib.loads(merged)  # never write a config we can't read back
    except tomllib.TOMLDecodeError:
        return
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = config_path.with_name(config_path.name + ".tmp")
        tmp.write_text(merged, encoding="utf-8")
        tmp.replace(config_path)
    except OSError:
        return
