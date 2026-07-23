"""Stage a skill from a git repo or local path for import (issue #139).

The untrusted surface is IMPORTED SKILLS — their instructions and bundled
scripts are what the model will follow (tools are verified by being BUILT here
via create_tool + diff-approval, so they need no separate import check). The
safety model is therefore: nothing an import fetches is installed until the USER
approves EACH file through the normal write-diff gate — enforced in code
(`agent._import_skill`), not by asking the model nicely.

This module only STAGES: fetch (a shallow, read-only `git clone` that never
executes the skill's code) + validate (must be a real SKILL.md) + collect the
text files. Binary assets are skipped (they cannot go through the text diff
gate and are not part of the instruction/execution surface).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

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


class SkillImportError(RuntimeError):
    pass


def looks_like_url(source: str) -> bool:
    return bool(_URLISH.match(source.strip()))


def _decodes(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


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
    try:
        skill_dir.relative_to(base)
    except ValueError:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise SkillImportError("path escapes the repository") from None

    manifest = skill_dir / "SKILL.md"
    if not manifest.is_file():
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise SkillImportError(
            f"no SKILL.md found at {path or '<repo root>'} — point `path` at the skill folder"
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
        if not p.is_file() or ".git" in p.parts:
            continue
        rel = str(p.relative_to(skill_dir))
        text = _decodes(p.read_bytes())
        if text is None:
            skipped.append(rel)  # binary asset — not part of the trust surface
            continue
        files.append((rel, text, os.access(p, os.X_OK)))
    return name, entry.description, files, skipped, tmp


QUARANTINE_ROOT = Path.home() / ".local" / "state" / "aish" / "skill-imports"


def stage_to_disk(
    repo: str, path: str = "", root: Path | None = None
) -> tuple[str, Path, list[str]]:
    """Stage an import to a quarantine dir for later, external review (the CLI
    'B' path): the validated files are written under root/<name>/ so the user
    can inspect them with their own editor, then `aish skill approve <name>`
    installs. Returns (name, quarantine_dir, risk_flags)."""
    root = root or QUARANTINE_ROOT
    name, _description, files, _skipped, tmp = stage(repo, path)
    try:
        dest = root / name
        shutil.rmtree(dest, ignore_errors=True)
        for rel, text, is_exec in files:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            if is_exec:
                target.chmod(0o755)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return name, dest, safety_scan(files)


def pending(root: Path | None = None) -> list[str]:
    root = root or QUARANTINE_ROOT
    try:
        return sorted(p.name for p in root.iterdir() if (p / "SKILL.md").is_file())
    except OSError:
        return []


def install(name: str, dest_skills_dir: Path, root: Path | None = None) -> Path:
    """Move a quarantined skill into the skills dir. Returns the install path."""
    root = root or QUARANTINE_ROOT
    src = root / name
    if not (src / "SKILL.md").is_file():
        raise SkillImportError(f"no staged skill named {name!r} (see `aish skill list`)")
    dest = dest_skills_dir / name
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest)
    shutil.rmtree(src, ignore_errors=True)
    return dest


def discard(name: str, root: Path | None = None) -> bool:
    root = root or QUARANTINE_ROOT
    src = root / name
    if not src.is_dir():
        return False
    shutil.rmtree(src, ignore_errors=True)
    return True
