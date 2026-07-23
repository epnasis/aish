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


class SkillImportError(RuntimeError):
    pass


def looks_like_url(source: str) -> bool:
    return bool(_URLISH.match(source.strip()))


def _decodes(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def stage(repo: str, path: str = "") -> tuple[str, list[tuple[str, str, bool]], list[str], str]:
    """Fetch + validate + collect. Returns
    (skill_name, files, skipped_binaries, tmp_to_cleanup) where files is a list
    of (relpath, text, is_executable). Raises SkillImportError on failure. The
    caller MUST rmtree tmp_to_cleanup (empty string when the source was local)."""
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
    return name, files, skipped, tmp
