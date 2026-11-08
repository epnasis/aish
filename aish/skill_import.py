"""Stage a skill from a git repo or local path for import (issue #139).

The untrusted surface is IMPORTED SKILLS — their instructions and bundled
scripts are what the model will follow (tools are verified by being BUILT here
via create_tool + diff-approval, so they need no separate import check). The
safety model is therefore: nothing an import fetches is installed until the USER
approves EACH file through the normal write-diff gate — enforced in code
(`agent._import_skill`), not by asking the model nicely.

Every import, from a URL or a local path, installs into the third-party tree
(`skills.VENDOR_SKILLS_DIR`, #467) — a local path is no evidence of who wrote
the skill — and `record_import` notes where it came from in that tree's lock.

This module only STAGES: fetch (a shallow, read-only `git clone` that never
executes the skill's code) + validate (must be a real SKILL.md) + collect the
text files. Binary assets are skipped (they cannot go through the text diff
gate and are not part of the instruction/execution surface).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .atomic_write import publish
from .files import contains
from .paths import state_home

_URLISH = re.compile(r"^(https?://|git@|ssh://|git://)")
CLONE_TIMEOUT = 120

# Extension → highlight.js language name (for the review card's code blocks).
_LANG = {
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".py": "python",
    ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
    ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".rb": "ruby", ".go": "go", ".rs": "rust", ".sql": "sql",
    ".html": "xml", ".css": "css", ".pl": "perl", ".php": "php",
}

# Deterministic heuristics that FOCUS the human review — not a verdict. Each is
# a pattern whose presence in a fetched file is worth a second look.
_RISK_PATTERNS = [
    (re.compile(r"\b(curl|wget|nc|ncat|scp|ssh|telnet)\b"), "network access"),
    (re.compile(r"(curl|wget)[^|\n]*\|\s*(sh|bash|zsh|python)"), "pipe-to-shell"),
    (re.compile(r"\beval\b|\bexec\s*\("), "dynamic eval/exec"),
    (re.compile(r"\brm\s+-[rf]{1,2}\b"), "recursive delete"),
    (re.compile(r"\bsudo\b"), "sudo / privilege escalation"),
    (re.compile(r"base64\s+(-d|--decode)"), "base64-decoded payload"),
    (re.compile(r">\s*/etc/|>>?\s*~|>\s*\$HOME"), "writes outside its own dir"),
    (re.compile(r"\bchmod\b|\bchown\b"), "changes file permissions/ownership"),
    (re.compile(r"/etc/passwd|id_rsa|\.ssh/|\.aws/|\.config/aish"), "touches sensitive paths"),
]


def lang_for(path: str) -> str:
    import os.path

    return _LANG.get(os.path.splitext(path)[1].lower(), "")


def safety_scan(files: list[tuple[str, str, bool]]) -> list[str]:
    """Human-readable flags for patterns worth reviewing, e.g.
    'scripts/run.sh: network access, pipe-to-shell'. Deterministic; advisory."""
    flags: list[str] = []
    for rel, text, _is_exec in files:
        hits = [label for pat, label in _RISK_PATTERNS if pat.search(text)]
        if hits:
            # de-dupe while preserving order
            seen: list[str] = []
            for h in hits:
                if h not in seen:
                    seen.append(h)
            flags.append(f"{rel}: {', '.join(seen)}")
    return flags


MANIFEST = "SKILL.md"


def english_keywords(text: str) -> tuple[str, list[str]]:
    """A SKILL.md's text with its `keywords:` line run through the one keyword
    hygiene (`skills.clean_keywords`, #444), and what was dropped. An import
    is a keyword writer like remember/create_skill/curate, and one import
    would otherwise re-pollute the English-only corpus with nothing said.

    Restrict-only, like everywhere: the text is returned UNCHANGED unless a
    keyword was dropped, and then only that one line changes — the
    description, the body and every other header line stay byte-for-byte."""
    from . import skills as sk

    header, _body = sk.split_frontmatter(text)
    if not header:
        return text, []
    for line in header.splitlines():
        key, sep, value = line.partition(":")
        if not sep or key.strip().casefold() != "keywords":
            continue
        kept, dropped = sk.clean_keywords(value)
        if not dropped:
            return text, []
        replacement = f"keywords: {', '.join(kept)}" if kept else ""
        if kept:
            return text.replace(line, replacement, 1), dropped
        # No keyword survived: remove the line with its own line ending.
        for ending in ("\r\n", "\n"):
            if line + ending in text:
                return text.replace(line + ending, "", 1), dropped
        return text.replace(line, "", 1), dropped
    return text, []


def english_manifest(
    files: list[tuple[str, str, bool]],
) -> tuple[list[tuple[str, str, bool]], list[str]]:
    """`english_keywords` applied to the staged SKILL.md, BEFORE review, so
    what the owner reviews is exactly what installs."""
    out: list[tuple[str, str, bool]] = []
    dropped: list[str] = []
    for rel, text, is_exec in files:
        if rel == MANIFEST:
            text, dropped = english_keywords(text)
        out.append((rel, text, is_exec))
    return out, dropped


def keywords_flag(dropped: list[str]) -> list[str]:
    """The line the import's consolidated review shows for a drop — empty
    when nothing was dropped, so it can be added to the flags unconditionally."""
    if not dropped:
        return []
    return [f"{MANIFEST}: keywords not English, not installed: {', '.join(dropped)}"]


class SkillImportError(RuntimeError):
    pass


def looks_like_url(source: str) -> bool:
    return bool(_URLISH.match(source.strip()))


def _decodes(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def symlinks_in(directory: Path) -> list[str]:
    """Every symlink under `directory` (files and directories), relative to it.
    os.walk with followlinks=False lists a linked directory without entering it,
    on every supported Python — `rglob`'s treatment of linked dirs changed in 3.13."""
    found: list[str] = []
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        if ".git" in Path(current).relative_to(directory).parts:
            continue
        for item in dirnames + filenames:
            if item == ".git":
                continue
            if (Path(current) / item).is_symlink():
                found.append(str((Path(current) / item).relative_to(directory)))
    return sorted(found)


def existing_skill(names: list[str]) -> Path | None:
    """Where a skill answering to any of `names` already lives — in the
    owner's tree or the third-party one — or None. An import must never
    replace one (#466), and must never land beside an owner's skill of the
    same name either (#467): his would win every read, so the import would sit
    there unreachable. Checked: the folder by that name, the flat `<name>.md`,
    and any entry whose frontmatter `name:` matches (the frontmatter wins over
    the path, so a different folder can still collide)."""
    from . import skills as sk

    trees = [sk.GLOBAL_SKILLS_DIR, sk.VENDOR_SKILLS_DIR]
    for skills_dir in trees:
        for name in names:
            for candidate in (skills_dir / name, skills_dir / f"{name}.md"):
                if candidate.exists() or candidate.is_symlink():
                    return candidate
    for skills_dir in trees:
        for entry in sk._dir_entries(skills_dir, "skill"):
            if entry.name in names and entry.path is not None:
                return entry.path
    return None


def already_exists_error(path: Path) -> str:
    return (
        f"a skill with this name already exists at {path} — an import never replaces "
        "a skill, so nothing was installed. Delete the existing one first if it "
        "should be replaced."
    )


# Where each third-party skill came from (#467), beside the skill folders and
# never inside one, so it is not a bundled file and is on no card. A record,
# not a decision: membership in the tree is what makes a skill third-party,
# with or without an entry here. `version` leaves room for what a later slice
# adds per skill (file hashes, an audit verdict).
LOCK_NAME = "skills.lock"
LOCK_VERSION = 1


def reserved_name_error(names: list[str]) -> str | None:
    """The lock's own name is not a skill name: a folder by that name would
    stand where the lock is written and every later record would fail."""
    if LOCK_NAME in names:
        return f"{LOCK_NAME!r} is the name of the third-party tree's lock, not a skill name"
    return None


def source_label(repo: str) -> str:
    """The lock's `source`: a URL without any credentials in it (the lock is
    in the config backup), or `local:<absolute path>`."""
    if looks_like_url(repo):
        parts = urlsplit(repo.strip())
        if parts.username or parts.password:
            host = parts.hostname or ""
            netloc = f"{host}:{parts.port}" if parts.port else host
            return urlunsplit(parts._replace(netloc=netloc))
        return repo.strip()
    return f"local:{Path(os.path.expanduser(repo)).resolve()}"


def clone_commit(tmp: str) -> str | None:
    """HEAD of a staged clone; None for a local source or when git cannot say."""
    if not tmp:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", tmp, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = proc.stdout.strip()
    return commit if proc.returncode == 0 and commit else None


def record_import(
    tree: Path, name: str, source: str | None, sub_path: str, commit: str | None
) -> None:
    """Add `name` to the tree's lock. The ONE writer, called by both install
    paths after the files are in place. Atomic against a torn write only: two
    processes importing at once can each read the old lock, and the later
    write drops the earlier entry — accepted, because the lock decides nothing.
    Raises OSError; an unreadable lock is replaced rather than trusted."""
    lock = tree / LOCK_NAME
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
        entries = data.get("skills") if isinstance(data, dict) else None
    except (OSError, ValueError):
        entries = None
    if not isinstance(entries, dict):
        entries = {}
    entries[name] = {
        "source": source,
        "path": sub_path,
        "commit": commit,
        "imported_at": date.today().isoformat(),
    }
    text = json.dumps({"version": LOCK_VERSION, "skills": entries}, indent=2, sort_keys=True)
    publish(lock, text + "\n", encoding="utf-8")


def stage(
    repo: str, path: str = ""
) -> tuple[str, str, list[tuple[str, str, bool]], list[str], str]:
    """Fetch + validate + collect. Returns
    (skill_name, description, files, skipped_binaries, tmp_to_cleanup) where
    files is a list of (relpath, text, is_executable). Raises SkillImportError
    on failure. The caller MUST rmtree tmp_to_cleanup (empty when local)."""
    tmp = ""
    if looks_like_url(repo):
        tmp = tempfile.mkdtemp(prefix="aish-skill-import-")
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", repo, tmp],
            capture_output=True, text=True, timeout=CLONE_TIMEOUT,
        )
        if proc.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SkillImportError(f"git clone failed: {proc.stderr.strip()}")
        base = Path(tmp).resolve()
    else:
        base = Path(os.path.expanduser(repo)).resolve()
        if not base.is_dir():
            raise SkillImportError(f"not a directory or a git URL: {repo}")

    skill_dir = (base / path).resolve() if path else base
    if not contains(base, skill_dir):
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise SkillImportError("path escapes the repository")

    manifest = skill_dir / "SKILL.md"
    if not manifest.is_file():
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise SkillImportError(
            f"no SKILL.md found at {path or '<repo root>'} — point `path` at the skill folder"
        )

    # Before anything is read: a link would be followed out of the skill, and
    # its target (an ssh key, a credentials file) shown on the card and
    # installed as if it were part of the skill (#466). Refused outright rather
    # than skipped, so nothing about the skill silently differs from its source.
    links = symlinks_in(skill_dir)
    if links:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise SkillImportError(
            f"the skill contains symlinks ({', '.join(links)}) — aish never follows a "
            "link out of an imported skill, so nothing was imported"
        )

    from . import skills as sk

    entry = sk._parse(manifest, "skill")
    name = entry.name
    if not sk.NAME_RE.match(name or ""):
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise SkillImportError(f"skill name {name!r} is invalid")

    files: list[tuple[str, str, bool]] = []
    skipped: list[str] = []
    for p in sorted(skill_dir.rglob("*")):
        if p.is_symlink():  # appeared after the check above
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            raise SkillImportError(
                f"{p.relative_to(skill_dir)} is a symlink — nothing was imported"
            )
        if not p.is_file() or ".git" in p.parts:
            continue
        rel = str(p.relative_to(skill_dir))
        text = _decodes(p.read_bytes())
        if text is None:
            skipped.append(rel)  # binary asset — not part of the trust surface
            continue
        files.append((rel, text, os.access(p, os.X_OK)))
    return name, entry.description, files, skipped, tmp


def quarantine_root() -> Path:
    """Where staged imports wait for review — resolved at CALL time through
    `paths.state_home` (#399), so a run that moved its state tree stages into
    its own tree and `aish skill list` there never shows the owner's pending
    imports. The dir is scratch: nothing about it needs the real home."""
    return state_home() / "skill-imports"


def _origin_path(root: Path, name: str) -> Path:
    """Beside the quarantined folder, not in it, so it never installs as a
    bundled file: where the staged skill came from, for the lock at approve."""
    return root / f"{name}.origin.json"


def stage_to_disk(
    repo: str, path: str = "", root: Path | None = None
) -> tuple[str, Path, list[str]]:
    """Stage an import to a quarantine dir for later, external review (the CLI
    'B' path): the validated files are written under root/<name>/ so the user
    can inspect them with their own editor, then `aish skill approve <name>`
    installs. Returns (name, quarantine_dir, risk_flags)."""
    root = root or quarantine_root()
    name, _description, files, _skipped, tmp = stage(repo, path)
    files, dropped = english_manifest(files)
    try:
        dest = root / name
        shutil.rmtree(dest, ignore_errors=True)
        for rel, text, is_exec in files:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            if is_exec:
                target.chmod(0o755)
        origin = {"source": source_label(repo), "path": path, "commit": clone_commit(tmp)}
        publish(_origin_path(root, name), json.dumps(origin) + "\n", encoding="utf-8")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return name, dest, safety_scan(files) + keywords_flag(dropped)


def pending(root: Path | None = None) -> list[str]:
    root = root or quarantine_root()
    try:
        return sorted(p.name for p in root.iterdir() if (p / "SKILL.md").is_file())
    except OSError:
        return []


def install(name: str, root: Path | None = None) -> tuple[Path, list[str], str]:
    """Move a quarantined skill into the third-party tree — always that tree,
    decided here rather than by the caller (#467). Returns (install path,
    keywords dropped as not English, a note when the lock could not be
    written). The quarantine is hand-editable between stage and approve, so
    the manifest is cleaned again here, at the write."""
    from . import skills as sk

    # The name becomes a path under the tree, so it must not be able to leave it.
    if not sk.NAME_RE.match(name or ""):
        raise SkillImportError(f"invalid skill name {name!r}")
    root = root or quarantine_root()
    src = root / name
    if not (src / MANIFEST).is_file():
        raise SkillImportError(f"no staged skill named {name!r} (see `aish skill list`)")
    # The quarantine is hand-editable, so both checks run again here, at the write.
    links = symlinks_in(src)
    if links:
        raise SkillImportError(
            f"the staged skill contains symlinks ({', '.join(links)}) — nothing was installed"
        )
    declared = sk._parse(src / MANIFEST, "skill").name
    reserved = reserved_name_error([name, declared])
    if reserved is not None:
        raise SkillImportError(reserved)
    clash = existing_skill(sorted({name, declared}))
    if clash is not None:
        raise SkillImportError(already_exists_error(clash))
    tree = sk.VENDOR_SKILLS_DIR
    dest = tree / name
    shutil.copytree(src, dest)
    shutil.rmtree(src, ignore_errors=True)
    manifest = dest / MANIFEST
    text, dropped = english_keywords(manifest.read_text(encoding="utf-8"))
    if dropped:
        manifest.write_text(text, encoding="utf-8")
    origin_file = _origin_path(root, name)
    try:
        origin = json.loads(origin_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        origin = {}
    if not isinstance(origin, dict):
        origin = {}
    origin_file.unlink(missing_ok=True)
    return dest, dropped, lock_failure_note(
        tree, name, origin.get("source"), str(origin.get("path") or ""), origin.get("commit")
    )


def lock_failure_note(
    tree: Path, name: str, source: str | None, sub_path: str, commit: str | None
) -> str:
    """`record_import`, with a failure turned into the sentence both install
    paths say: the skill is installed and still third-party, only the record
    of where it came from is missing."""
    try:
        record_import(tree, name, source, sub_path, commit)
    except OSError as exc:
        return (
            f"where it came from was NOT recorded in {tree / LOCK_NAME} ({exc}); "
            "it is installed, and third-party all the same"
        )
    return ""


def discard(name: str, root: Path | None = None) -> bool:
    root = root or quarantine_root()
    src = root / name
    if not src.is_dir():
        return False
    shutil.rmtree(src, ignore_errors=True)
    _origin_path(root, name).unlink(missing_ok=True)
    return True
