"""Stage a skill from an approved GitHub source for import (#139, #467).

The untrusted surface is IMPORTED SKILLS — their instructions and bundled
scripts are what the model will follow (tools are verified by being BUILT here
via create_tool + diff-approval, so they need no separate import check). The
safety model is therefore: nothing an import fetches is installed until the USER
approves EACH file through the normal write-diff gate — enforced in code
(`agent._import_skill`), not by asking the model nicely.

Admission is by publisher (#467 P1, P6): `check_source` refuses, BEFORE any
clone, every source that is not `https://github.com/<owner>/<repo>` named in
the owner's approved-sources file (`skills.SKILL_SOURCES_FILE`), and every
local path. Both import paths reach it through `stage`.

Every import installs into the third-party tree (`skills.VENDOR_SKILLS_DIR`),
and `record_import` notes in that tree's lock where it came from and a hash of
every installed file — what `read_skill` and `read_file` of it are checked
against (`check_folder`, `verified_file`).

This module only STAGES: fetch (a shallow, read-only `git clone` that never
executes the skill's code) + validate (must be a real SKILL.md) + collect the
text files. Binary assets are skipped (they cannot go through the text diff
gate and are not part of the instruction/execution surface).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .atomic_write import publish
from .files import contains
from .paths import state_home

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


# One owner or repo name: letters, digits, `.`, `_`, `-` and nothing else, so
# nothing that parses differently in git, a shell or a URL can ride in one.
_GITHUB_NAME = re.compile(r"[A-Za-z0-9._-]+")
GITHUB_HOST = "github.com"


def _bad_segment(segment: str) -> bool:
    return segment in ("", ".", "..")


def github_repo(source: str) -> tuple[str, str]:
    """(owner, repo) of `https://github.com/<owner>/<repo>[.git][/]`, or
    SkillImportError saying which part of `source` is not that. The clone is
    then made from these two names alone, never from the text given."""
    raw = source.strip()

    def refuse(why: str) -> SkillImportError:
        return SkillImportError(
            f"NOT imported: {raw!r} is not an address aish imports from — {why}. Only "
            f"https://{GITHUB_HOST}/<owner>/<repo> is accepted. Nothing was fetched."
        )

    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise refuse("it contains whitespace or a control character")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise refuse(f"it does not parse ({exc})") from None
    if parts.scheme != "https":
        raise refuse("only https is accepted")
    # netloc compared whole: this is what refuses a user, a password and a port,
    # and `github.com@evil` / `github.com.evil` with them.
    if parts.netloc.casefold() != GITHUB_HOST or parts.hostname != GITHUB_HOST:
        raise refuse(f"the host must be exactly {GITHUB_HOST}, with no user or port")
    if "?" in raw or "#" in raw:
        raise refuse("it carries a query or a fragment")
    path = parts.path
    if "%" in path:
        raise refuse("it contains percent-encoding")
    path = path.removesuffix("/").removesuffix(".git")
    segments = path.split("/")
    if len(segments) != 3 or segments[0]:
        raise refuse("the path must be exactly /<owner>/<repo>")
    owner, repo = segments[1], segments[2]
    for name in (owner, repo):
        if _bad_segment(name) or not _GITHUB_NAME.fullmatch(name):
            raise refuse(f"{name!r} is not a GitHub owner or repository name")
    return owner, repo


def path_segments(path: str) -> list[str]:
    """The skill folder's path inside the repository as segments, or
    SkillImportError. Absolute paths and `.`/`..` are refused, never resolved:
    the approved-prefix check compares these segments, so they must mean
    exactly the folder that is cloned."""
    text = path.strip()
    # A control character (NUL above all) passed the check and then broke the
    # path join after the clone, as an unhandled error that left the clone on
    # disk — refused here, before anything is fetched, like the URL's own.
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise SkillImportError(
            f"NOT imported: path {path!r} contains a control character. Nothing was fetched."
        )
    if text.startswith(("/", "~")) or "\\" in text:
        raise SkillImportError(
            f"NOT imported: path {path!r} must be relative to the repository root, "
            "with / between folders. Nothing was fetched."
        )
    segments = [s for s in text.split("/") if s]
    if any(s in (".", "..") for s in segments):
        raise SkillImportError(
            f"NOT imported: path {path!r} contains '.' or '..'. Name the skill's "
            "folder directly. Nothing was fetched."
        )
    return segments


@dataclass(frozen=True)
class ApprovedSource:
    """One line of the owner's approved-sources file."""

    owner: str
    repo: str
    prefix: tuple[str, ...]

    def admits(self, owner: str, repo: str, segments: list[str]) -> bool:
        """Owner and repo compare case-insensitively, as GitHub resolves them;
        the prefix case-SENSITIVELY and by whole segments, so `skills/.curated`
        does not admit `skills/.curated-evil` or `skills/.Curated`."""
        return (
            owner.casefold() == self.owner.casefold()
            and repo.casefold() == self.repo.casefold()
            and tuple(segments[: len(self.prefix)]) == self.prefix
        )

    def __str__(self) -> str:
        return "/".join((GITHUB_HOST, self.owner, self.repo, *self.prefix))


def parse_sources(text: str) -> tuple[list[ApprovedSource], list[int]]:
    """The approved sources in the file's text, and the numbers of the lines
    that approve nothing because they are not `github.com/<owner>/<repo>[/<path>]`
    — said in the refusal, so a typo is not a silent no."""
    sources: list[ApprovedSource] = []
    unread: list[int] = []
    for number, line in enumerate(text.splitlines(), 1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        segments = entry.removesuffix("/").split("/")
        if (
            len(segments) < 3
            or segments[0].casefold() != GITHUB_HOST
            or any(_bad_segment(s) for s in segments[1:])
            or not all(_GITHUB_NAME.fullmatch(s) for s in segments[1:3])
            or any(ch.isspace() or ch == "\\" for ch in entry)
        ):
            unread.append(number)
            continue
        sources.append(ApprovedSource(segments[1], segments[2], tuple(segments[3:])))
    return sources, unread


def check_source(repo: str, path: str = "") -> tuple[str, str, list[str]]:
    """(owner, repo, path segments) when the owner's approved-sources file
    admits this import, else SkillImportError. Runs before anything is fetched:
    a model-composed address is never cloned unless he listed it (#467 P6)."""
    from . import skills as sk

    given = repo.strip()
    if "://" not in given and not given.startswith("git@"):
        raise SkillImportError(
            f"NOT imported: {given!r} is not a URL. aish imports a skill only from an "
            f"approved GitHub source (https://{GITHUB_HOST}/<owner>/<repo>), never from a "
            "folder on this machine — a skill the owner wrote himself is his to put in "
            f"{sk.GLOBAL_SKILLS_DIR} by hand. Nothing was imported."
        )
    owner, name = github_repo(repo)
    segments = path_segments(path)
    sources_file = sk.SKILL_SOURCES_FILE
    try:
        sources, unread = parse_sources(sources_file.read_text(encoding="utf-8"))
        listed = f"it lists {', '.join(map(str, sources)) or 'nothing'}"
    except FileNotFoundError:
        sources, unread, listed = [], [], "that file does not exist, so none is"
    except (OSError, UnicodeDecodeError) as exc:
        sources, unread, listed = [], [], f"that file could not be read ({exc}), so none is"
    if any(source.admits(owner, name, segments) for source in sources):
        return owner, name, segments
    asked = "/".join((GITHUB_HOST, owner, name, *segments))
    ignored = (
        f" Lines {', '.join(map(str, unread))} of it approve nothing: they are not "
        f"{GITHUB_HOST}/<owner>/<repo>[/<path>]." if unread else ""
    )
    raise SkillImportError(
        f"NOT imported: {asked} is not an approved skill source. The owner approves "
        f"sources himself, in {sources_file} — {listed}.{ignored} aish's file and skill "
        "tools refuse to write that file, and there is nothing to approve here. Nothing "
        "was fetched."
    )


def clone_url(owner: str, repo: str) -> str:
    return f"https://{GITHUB_HOST}/{owner}/{repo}.git"


def _clone(url: str, dest: str) -> None:
    """The one network step of an import. Redirects are off: a repository
    GitHub has renamed or transferred answers with a redirect, and following
    one would fetch from an address nobody approved — so such a repository
    fails here and must be approved under its new name."""
    proc = subprocess.run(
        ["git", "-c", "http.followRedirects=false", "clone", "--depth", "1", "--", url, dest],
        capture_output=True, text=True, timeout=CLONE_TIMEOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise SkillImportError(
            f"git clone of {url} failed (redirects are not followed): {proc.stderr.strip()}"
        )


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
# never inside one, so it is not a bundled file and is on no card. Membership
# in the tree is what makes a skill third-party, with or without an entry here.
# Version 2 adds `files`, the sha256 of every installed file, which every
# read_skill/read_file of the skill is checked against; a version-1 entry, or
# any entry without `files`, is served with a line saying nothing was checked.
LOCK_NAME = "skills.lock"
LOCK_VERSION = 2


def reserved_name_error(names: list[str]) -> str | None:
    """The lock's own name is not a skill name: a folder by that name would
    stand where the lock is written and every later record would fail."""
    # Case-folded: the owner's disk is case-insensitive, so `Skills.lock` would
    # stand where the lock is written just the same.
    if any(n.casefold() == LOCK_NAME for n in names):
        return f"{LOCK_NAME!r} is the name of the third-party tree's lock, not a skill name"
    return None


def source_label(repo: str) -> str:
    """The lock's `source`: the canonical address the clone was made from.
    Only called after `check_source` admitted `repo`."""
    return f"https://{GITHUB_HOST}/{'/'.join(github_repo(repo))}"


def lock_path(path: str) -> str:
    """The lock's `path`: the folder inside the repository, as checked."""
    return "/".join(path_segments(path))


def clone_commit(tmp: str) -> str | None:
    """HEAD of a staged clone; None when git cannot say."""
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


def _lock_entries(tree: Path) -> dict | None:
    try:
        data = json.loads((tree / LOCK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data.get("skills") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else None


def record_import(
    tree: Path,
    name: str,
    source: str | None,
    sub_path: str,
    commit: str | None,
    hashes: dict[str, str],
) -> None:
    """Add `name` to the tree's lock. The ONE writer, called by both install
    paths after the files are in place. Atomic against a torn write only: two
    processes importing at once can each read the old lock, and the later
    write drops the earlier entry, whose skill is then served as unchecked.
    Raises OSError; an unreadable lock is replaced rather than trusted."""
    entries = _lock_entries(tree) or {}
    entries[name] = {
        "source": source,
        "path": sub_path,
        "commit": commit,
        "imported_at": date.today().isoformat(),
        "files": hashes,
    }
    text = json.dumps({"version": LOCK_VERSION, "skills": entries}, indent=2, sort_keys=True)
    publish(tree / LOCK_NAME, text + "\n", encoding="utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _walk(folder: Path):
    """(relative posix path, path, is a symlink) for everything under
    `folder` that is not a directory, never following a link."""
    for current, dirnames, filenames in os.walk(folder, followlinks=False):
        for item in sorted(dirnames + filenames):
            path = Path(current) / item
            if item in dirnames and not path.is_symlink():
                continue
            yield path.relative_to(folder).as_posix(), path, path.is_symlink()


def file_hashes(folder: Path) -> dict[str, str]:
    """sha256 of every file of an installed skill, read back from disk — the
    INSTALLED bytes, after any keyword cleaning and the write's own newline,
    which are what a later read is compared against. Raises OSError."""
    return {
        rel: _sha256(path.read_bytes())
        for rel, path, is_link in _walk(folder)
        if not is_link and path.is_file()
    }


def recorded_hashes(tree: Path, folder: str) -> dict[str, str] | None:
    """The hashes recorded for the skill installed as `tree/<folder>`, or None
    when there are none to check against: no lock, no entry (a skill placed
    by hand), or an entry without `files` (a version-1 entry).

    The folder name matches case-insensitively: on the owner's disk
    `HELPER/x.md` opens `helper/x.md`, and an exact lookup would serve it as
    unchecked. Two entries differing only in case cannot both be installed
    there (`existing_skill` finds the first)."""
    entries = _lock_entries(tree) or {}
    entry = entries.get(folder)
    if entry is None:
        matching = [v for k, v in entries.items() if str(k).casefold() == folder.casefold()]
        entry = matching[0] if len(matching) == 1 else None
    hashes = entry.get("files") if isinstance(entry, dict) else None
    if not isinstance(hashes, dict):
        return None
    return {str(rel): str(digest) for rel, digest in hashes.items()}


def check_folder(
    folder: Path, recorded: dict[str, str], already_read: dict[str, bytes]
) -> list[str]:
    """Every way `folder` differs from its recorded hashes: a file changed,
    missing, not in the record, a symlink, or not a regular file. A file in
    `already_read` is judged by those bytes — the ones the caller serves —
    and not read again. Empty means every file matched."""
    found: list[str] = []
    seen: set[str] = set()
    for rel, path, is_link in _walk(folder):
        seen.add(rel)
        if is_link:
            found.append(f"{rel!r} is a symlink")
            continue
        if rel not in recorded:
            found.append(f"{rel!r} is not in the review")
            continue
        if not path.is_file():
            found.append(f"{rel!r} is not a regular file")
            continue
        try:
            data = already_read[rel] if rel in already_read else path.read_bytes()
        except OSError as exc:
            found.append(f"{rel!r} could not be read ({exc})")
            continue
        if _sha256(data) != recorded[rel]:
            found.append(f"{rel!r} changed")
    found += [f"{rel!r} is missing" for rel in sorted(set(recorded) - seen)]
    return found


def modified_refusal(name: str, folder: Path, found: list[str]) -> str:
    return (
        f"third-party skill {name!r} differs from what was installed after review — "
        f"{'; '.join(found)}. aish does not serve it. To use it again the owner deletes "
        f"{folder} and imports it again."
    )


def verified_file(tree: Path, target: Path) -> bytes | None:
    """The bytes of `target`, a file in the third-party tree, checked against
    the hash recorded for it at install — from ONE read, so what is checked is
    what is served. None when nothing is recorded to check against (the
    caller then reads it as before and says so). SkillImportError when it
    differs, or is not part of the reviewed skill at all."""
    root = tree.resolve()
    real = target.resolve()
    # By position, compared case-insensitively, for the reason given on
    # `skills.in_third_party_tree`; `relative_to` would refuse `VENDOR-SKILLS`.
    if [p.casefold() for p in real.parts[: len(root.parts)]] != [
        p.casefold() for p in root.parts
    ]:
        raise ValueError(f"{target} is not in {tree}")
    parts = real.parts[len(root.parts):]
    if len(parts) < 2:
        return None  # beside the skill folders: the lock itself, a flat skill
    recorded = recorded_hashes(root, parts[0])
    if recorded is None:
        return None
    rel = "/".join(parts[1:])
    if rel not in recorded:
        raise SkillImportError(modified_refusal(parts[0], root / parts[0], [
            f"{rel!r} is not in the review"
        ]))
    data = real.read_bytes()
    if _sha256(data) != recorded[rel]:
        raise SkillImportError(modified_refusal(parts[0], root / parts[0], [f"{rel!r} changed"]))
    return data


def stage(
    repo: str, path: str = ""
) -> tuple[str, str, list[tuple[str, str, bool]], list[str], str]:
    """Fetch + validate + collect. Returns
    (skill_name, description, files, skipped_binaries, tmp_to_cleanup) where
    files is a list of (relpath, text, is_executable). Raises SkillImportError
    on failure. The caller MUST rmtree tmp_to_cleanup.

    The source check comes first and alone decides whether anything is
    fetched: `import_skill` raises no card before the clone, so a clone of an
    address the model composed would be an uncarded request carrying whatever
    it put in the address."""
    owner, repo_name, segments = check_source(repo, path)
    tmp = tempfile.mkdtemp(prefix="aish-skill-import-")
    try:
        _clone(clone_url(owner, repo_name), tmp)
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SkillImportError(f"git clone failed: {exc}") from None
    except SkillImportError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    base = Path(tmp).resolve()

    skill_dir = base.joinpath(*segments).resolve()
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

    try:
        entry = sk._parse(manifest, "skill")
    except (UnicodeDecodeError, ValueError):
        shutil.rmtree(tmp, ignore_errors=True)
        raise SkillImportError("SKILL.md is not UTF-8 text — nothing was imported") from None
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
        origin = {
            "source": source_label(repo), "path": lock_path(path), "commit": clone_commit(tmp)
        }
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
    """`record_import` with the hashes of what is now on disk, and a failure
    turned into the sentence both install paths say: the skill is installed
    and still third-party, but nothing records where it came from or what its
    files were, so its reads will say they were not checked."""
    try:
        record_import(tree, name, source, sub_path, commit, file_hashes(tree / name))
    except OSError as exc:
        return (
            f"where it came from and its file hashes were NOT recorded in "
            f"{tree / LOCK_NAME} ({exc}); it is installed, and third-party all the "
            "same, and reading it will say its files were not checked"
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
