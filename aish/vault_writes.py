"""What an Obsidian write can DESTROY, and what that licenses (#379).

The sibling of `recipients.py`. That module asks *can this mail reach anybody
but the owner?*; this one asks *can this write destroy anything the owner did
not opt in?* — and a No means `obsidian_write` runs with **no approval card**,
in every session, attended or triggered.

**The licence is computed from facts aish reads itself, never from the
wrapper's claim.** `obsidian_write` is `mutating: yes` and stays so — declaring
a writing tool read-only is the downgrade #209/#326 refuse — and nothing in its
`TOOL.md` is consulted here. aish re-reads the vault list from `config.toml`,
resolves the target path, and parses the note's frontmatter on its own, so a
wrapper claiming safety it does not have gains nothing (`docs/tools-layer.md`,
the fence). The plugin's own code is neither imported nor run.

What is licensed, split by what changes for the owner (one permission per
consequence, never per tool):

- `create`: the target `<vault>/<folder>/<title>.md` is inside the vault after
  resolution and DOES NOT EXIST. A create can destroy nothing. `if_exists`
  is not read: with `"unique"` the wrapper numbers a sibling, its own
  behaviour; the licence asserts only that the named base is absent.
- `append` to a note whose FRONTMATTER `tags` carry `aish` — the owner's
  per-note opt-in, made in Obsidian's Properties. Append destroys nothing.
- `set_frontmatter` on such a note, ONLY when every key is a plain word other
  than `tags`, ABSENT from the note's existing frontmatter, and every value
  is a single-line scalar (or a list of them) — no `null`, which removes a
  key. Adding a key destroys nothing. Overwriting one, removing one, or
  touching `tags` each has no undo aish can see (the trash copy is the
  wrapper's), and a newline in a value writes a second YAML line that can
  rewrite `tags` by another route — so all of those card.

What is deliberately NOT licensed, and why:

- `replace` and `replace_section` on an opted-in note. Their undo depends on
  the wrapper's trash-first copy, a claim aish cannot verify from here — so
  they card. The next slice is an aish-side backup taken BEFORE the write, at
  which point the licence can widen on evidence aish holds.
- Any call with `attachments`: it copies arbitrary local files into a synced
  vault, which is a different consequence from writing text.
- `obsidian_delete`, ever, and every other tool name.

**Everything fails CLOSED.** An unreadable config, an unreadable or ambiguous
note, a path that cannot be resolved, a decode error — each is `None`, which
the caller turns into a card. #356 is the scar for the other direction: a
reader that answered "nothing there" for "could not read" silently turned a
fence off.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from . import files
from .paths import config_home as _config_home

TOOL = "obsidian_write"
AISH_TAG = "aish"

# The audit reason a caller records when this licenses a run. Names the POLICY,
# never the origin (#377): `auto (user)` would say a human decided.
OWNER_OPTED = "owner-opted vault write"

# Obsidian's own directories. Never a licensed destination, never searched for
# a note: `.trash` holds the wrapper's backups and `.obsidian` its config.
VAULT_INTERNAL_DIRS = frozenset({".obsidian", ".trash"})

# A title the licence can compute the exact target of. The wrapper rewrites
# titles that carry filename-forbidden characters, trailing punctuation or
# runs of whitespace; a title it might rewrite lands at a path aish did not
# check, so those are refused rather than mirrored.
_UNSAFE_TITLE_CHARS = re.compile(r'[\\/:#^\[\]|?*"<>\x00-\x1f]')
_TRAILING_PUNCTUATION = ".,;:!。，；：！"
_TITLE_MAX = 120

# Only the head of a note is read: frontmatter is the first block of the file.
_HEAD_BYTES = 64 * 1024


def owner_opted_write(
    name: str, args: Mapping, *, config_home: Path | None = None
) -> str | None:
    """The policy string when this call can destroy nothing the owner did not
    opt in, else None (card). `config_home` is the test seam; production reads
    the same tree every other config consumer does."""
    if name != TOOL or not isinstance(args, Mapping):
        return None
    if not _blank(args.get("attachments")):
        return None
    try:
        vault = _vault_root(args.get("vault"), config_home)
        if vault is None:
            return None
        action = str(args.get("action") or "").strip()
        if action == "create":
            licensed = _create_target_is_new(vault, args)
        elif action == "append":
            licensed = _opted_in_note(vault, args.get("note")) is not None
        elif action == "set_frontmatter":
            # ONLY keys the note does not already hold: adding one destroys
            # nothing, overwriting one has no undo aish can see.
            keys = _plain_frontmatter_keys(args.get("frontmatter"))
            note = _opted_in_note(vault, args.get("note"))
            licensed = (
                keys is not None and note is not None and not keys & _frontmatter_keys(note)
            )
        else:
            licensed = False
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        # OSError: any filesystem read; ValueError covers UnicodeDecodeError,
        # TOMLDecodeError and json errors; RuntimeError covers a resolve()
        # symlink loop; TypeError/AttributeError a config whose shape is not
        # the one expected. Not knowing is a card, never a grant.
        return None
    return OWNER_OPTED if licensed else None


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


# --------------------------------------------------------------------------
# The vault
# --------------------------------------------------------------------------


def _vault_root(wanted: object, config_home: Path | None) -> Path | None:
    """The configured vault this call names — the `vault` argument, or the
    single configured vault when there is exactly one — as an existing
    directory. The wrapper falls back to a vault named `Main` among several;
    the licence does not guess and cards instead."""
    if config_home is not None:
        config_path = config_home / "config.toml"
    else:
        config_path = Path(os.environ.get("AISH_CONFIG") or _config_home() / "config.toml")
    table = tomllib.loads(config_path.read_text(encoding="utf-8"))
    obsidian = table.get("obsidian") if isinstance(table, dict) else None
    vaults = obsidian.get("vaults") if isinstance(obsidian, dict) else None
    if not isinstance(vaults, dict) or not vaults:
        return None
    name = str(wanted or "").strip()
    if name:
        matches = [k for k in vaults if str(k).lower() == name.lower()]
        if len(matches) != 1:
            return None
        raw = vaults[matches[0]]
    elif len(vaults) == 1:
        raw = next(iter(vaults.values()))
    else:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    root = Path(raw).expanduser()
    # A relative vault path resolves against the wrapper's cwd, which aish
    # cannot know from here.
    if not root.is_absolute() or not root.is_dir():
        return None
    return root


def _inside(vault: Path, path: Path) -> bool:
    """Whether `path` lands inside the vault — THE containment test (#309),
    which resolves both sides so a symlink anywhere along the path counts, and
    judges a not-yet-created leaf by its real parent."""
    return files.contains(vault, path)


def _relative_ref(raw: object) -> Path | None:
    """A caller-supplied vault-relative path, or None for anything that is
    absolute, home-anchored, empty, NUL-bearing or climbs with `..`."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().rstrip("/")
    if not text or text.startswith(("/", "~")) or "\x00" in text:
        return None
    rel = Path(text)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return rel


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def _plain_title(raw: object) -> str | None:
    """The title, when it is one the wrapper would use verbatim as a filename."""
    if not isinstance(raw, str):
        return None
    title = raw
    if (
        not title
        or title != title.strip()
        or _UNSAFE_TITLE_CHARS.search(title)
        or title[-1] in _TRAILING_PUNCTUATION
        or title.startswith(".")
        # Only a single plain space between words: any other whitespace
        # character, or a run of them, is collapsed by the wrapper.
        or any(c.isspace() and c != " " for c in title)
        or "  " in title
        or len(title) > _TITLE_MAX
    ):
        return None
    return title


def _create_target_is_new(vault: Path, args: Mapping) -> bool:
    title = _plain_title(args.get("title"))
    if title is None:
        return False
    folder_raw = args.get("folder")
    if _blank(folder_raw):
        folder = Path()
    else:
        rel = _relative_ref(folder_raw)
        # A dot-directory is Obsidian's own, never a note destination.
        if rel is None or any(part.startswith(".") for part in rel.parts):
            return False
        folder = rel
    target = vault / folder / f"{title}.md"
    if not _inside(vault, target):
        return False
    parent = target.parent
    if parent.exists() and not parent.is_dir():
        return False
    return not _present(target)


def _present(path: Path) -> bool:
    """Whether SOMETHING is at `path`: a file, a directory, a symlink even
    when dangling, or an iCloud placeholder (`.Name.md.icloud`) standing in
    for an offloaded note. Only "no such entry" is absence; any other failure
    to look raises and the caller cards — `os.path.lexists` would report an
    unsearchable parent as "nothing there" (#356)."""
    for candidate in (path, path.parent / f".{path.name}.icloud"):
        try:
            os.lstat(candidate)
            return True
        except FileNotFoundError:
            continue
    return False


# --------------------------------------------------------------------------
# edits of an opted-in note
# --------------------------------------------------------------------------


def _opted_in_note(vault: Path, ref: object) -> Path | None:
    """The one existing `.md` file inside the vault this reference names,
    when its frontmatter tags carry `aish`; else None."""
    path = _resolve_note(vault, ref)
    if path is None or not _inside(vault, path):
        return None
    # A symlinked note is replaced BY the write (rename over the link), which
    # changes the vault's structure without the owner's say — card.
    if path.is_symlink():
        return None
    real = path.resolve()
    if not real.is_file() or real.suffix.lower() != ".md":
        return None
    # A symlink may land inside `.trash` or `.obsidian` even though the
    # reference named neither; the resolved path is what is judged.
    if VAULT_INTERNAL_DIRS & set(real.parts):
        return None
    return real if AISH_TAG in _frontmatter_tags(real) else None


def _resolve_note(vault: Path, ref: object) -> Path | None:
    """A vault-relative path as given, then with `.md` added, else a bare name
    that matches exactly one note under the vault. Ambiguous or absent is
    None. The candidate order is the wrapper's, deliberately: `Foo.md` must
    mean `Foo.md.md` here when only that exists, since that is the file the
    write reaches. An offloaded note (`.Name.md.icloud`) counts as PRESENT at
    every step — aish cannot read it, so a reference that reaches one cards
    rather than resolving past it to a different note."""
    rel = _relative_ref(ref)
    if rel is None:
        return None
    if any(part in VAULT_INTERNAL_DIRS for part in rel.parts):
        return None
    for candidate in (rel, rel.with_name(rel.name + ".md")):
        if _present(vault / candidate):
            return vault / candidate
    stem = rel.name.lower()
    wanted = {stem, stem + ".md"} if not stem.endswith(".md") else {stem}
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if d not in VAULT_INTERNAL_DIRS]
        for name in filenames:
            logical = _ICLOUD_PLACEHOLDER.sub(r"\1", name)
            if logical.lower() in wanted:
                hits.append(Path(dirpath) / name)
        if len(hits) > 1:
            return None
    return hits[0] if len(hits) == 1 else None


_ICLOUD_PLACEHOLDER = re.compile(r"^\.(.+)\.icloud$")


_TAGS_KEY = re.compile(r"^tags:[ \t]*(.*?)[ \t]*$")
_LIST_ITEM = re.compile(r"^[ \t]*-[ \t]*(.*?)[ \t]*$")


def _frontmatter_block(path: Path) -> list[str] | None:
    """The lines between a note's leading `---` fences, or None when there is
    no closed block at the top. ONE reading of where the header ends, shared
    by the tags reader and the key reader below."""
    with path.open("rb") as fh:
        head = fh.read(_HEAD_BYTES)
    lines = head.decode("utf-8").splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None
    block: list[str] = []
    for line in lines[1:]:
        if line.rstrip() in ("---", "..."):
            return block
        block.append(line)
    return None


def _frontmatter_keys(path: Path) -> frozenset[str]:
    """Every top-level key the note's frontmatter already holds, lower-cased.
    A line at column 0 with a colon in it is a key; list items and indented
    continuations are not. Read so that `set_frontmatter` can be licensed
    ONLY for keys that are ABSENT: adding one destroys nothing, while
    overwriting one has no undo but the wrapper's trash copy."""
    block = _frontmatter_block(path)
    if block is None:
        return frozenset()
    keys = set()
    for line in block:
        match = _TOP_LEVEL_KEY.match(line)
        if match:
            keys.add(match.group(1).strip().strip("\"'").lower())
    return frozenset(keys)


# Liberal on purpose: any column-0 line with a colon is taken as a key, so a
# spelling this misses can only card an addition, never free an overwrite.
_TOP_LEVEL_KEY = re.compile(r"^([^\s\-#][^:]*):")


def _frontmatter_tags(path: Path) -> frozenset[str]:
    """The tags a note's frontmatter declares, lower-cased and `#`-stripped —
    inline `[a, b]`, a block list, or a scalar. Only the leading `---` block
    counts; an inline `#aish` in the body is pasted content and licenses
    nothing. A `tags:` key stated twice is refused (#326): nothing here picks
    one of two readings."""
    block = _frontmatter_block(path)
    if block is None:
        return frozenset()
    found = [i for i, line in enumerate(block) if _TAGS_KEY.match(line)]
    if len(found) != 1:
        return frozenset()
    index = found[0]
    match = _TAGS_KEY.match(block[index])
    assert match is not None
    value = match.group(1)
    items: list[str]
    if not value:
        items = []
        for line in block[index + 1 :]:
            item = _LIST_ITEM.match(line)
            if item is None:
                break
            items.append(item.group(1))
    elif value.startswith("[") and value.endswith("]"):
        items = value[1:-1].split(",")
    else:
        items = re.split(r"[,\s]+", value)
    cleaned = (item.strip().strip("\"'").lstrip("#").strip().lower() for item in items)
    return frozenset(tag for tag in cleaned if tag)


_PLAIN_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_OPT_IN_KEYS = frozenset({"tags", "tag"})


def _plain_frontmatter_keys(raw: object) -> frozenset[str] | None:
    """The lower-cased keys a `set_frontmatter` argument — a mapping, a JSON
    object string, or `k=v, k2=v2` — would set, when it sets ONLY plain keys
    to ONLY single-line values and never `tags`. Anything else is None (card).

    The reason this reads VALUES and not only key names: the wrapper writes a
    value as a line of YAML, so a value carrying a newline becomes a second
    line, and `paid=x\\ntags:evil` sets `paid` AND rewrites `tags` — the note
    edited out of its own opt-in with no card. Likewise a key holding `:`.
    A `null` value REMOVES a key, whose only undo is the wrapper's trash copy,
    so removals card exactly as `replace` does."""
    pairs = _frontmatter_pairs(raw)
    if not pairs:
        return None
    keys = set()
    for key, value in pairs:
        if not _PLAIN_KEY.match(key) or key.lower() in _OPT_IN_KEYS:
            return None
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, bool | int | float):
                continue
            if not isinstance(item, str) or len(item.splitlines()) > 1:
                return None  # None (a removal), a nested object, a newline
            if item.strip().lower() in ("null", "none"):
                return None  # the kv form's own spelling of a removal
        keys.add(key.lower())
    return frozenset(keys)


def _frontmatter_pairs(raw: object) -> list[tuple[str, object]] | None:
    if isinstance(raw, Mapping):
        return [(str(k), v) for k, v in raw.items()]
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        parsed = json.loads(text)
        return [(str(k), v) for k, v in parsed.items()] if isinstance(parsed, dict) else None
    pairs: list[tuple[str, object]] = []
    for part in text.split(","):
        key, sep, value = part.partition("=")
        if not sep or not key.strip():
            return None
        pairs.append((key.strip(), value.strip().strip("\"'")))
    return pairs
