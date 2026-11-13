"""`aish skill outdated` — which installed third-party skills have newer
versions upstream (#467 slice 4b).

A report for the owner's terminal: nothing installs, the lock is not
written, and no model reads it. For each skill in the third-party tree it
reads the lock entry, re-checks the recorded source against `skill-sources`
NOW (`skill_import.check_source`, no network when it refuses), asks the
source which commit its default branch points at (`skill_import._ls_remote`)
and, when that is not the recorded commit, makes ONE shallow clone per
source (`skill_import._clone`) shared by every skill from it. The skill's
recorded folder in that clone goes through the install pipeline
(`collect` → `install_bytes`) and is compared with the lock's hashes
(`compare_install`) — installed bytes against bytes that WOULD install, so
keyword cleaning and the added final newline are never reported as changes.

Each skill costs only its own row when anything about it fails.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import skill_import, skills
from .files import contains

NOT_COMPARED = "text files compared; binary files are not"


@dataclass(frozen=True)
class Row:
    name: str
    line: str


@dataclass
class _Upstream:
    """One source's answers, shared across its skills."""

    head: str | None = None
    error: str = ""
    clone: Path | None = None
    cloned_commit: str | None = None
    clone_error: str = ""


def _short(commit: str | None) -> str:
    return commit[:12] if commit else "unknown"


def _installed_names(tree: Path) -> list[str]:
    """What is installed in the third-party tree: every folder (a link
    included) and every flat `<name>.md`; the lock itself is not a skill."""
    try:
        children = sorted(tree.iterdir())
    except FileNotFoundError:
        return []
    names: list[str] = []
    for child in children:
        if child.name == skill_import.LOCK_NAME or child.name.startswith("."):
            continue
        if child.is_dir() or child.is_symlink():
            names.append(child.name)
        elif child.suffix == ".md":
            names.append(child.stem)
    return names


def changes_line(changes: skill_import.FileChanges) -> str:
    parts: list[str] = []
    if changes.added:
        parts.append(f"added {', '.join(changes.added)}")
    if changes.removed:
        parts.append(f"removed {', '.join(changes.removed)}")
    if changes.modified:
        named = [
            f"{rel} (only a final newline added)" if rel in changes.newline_only else rel
            for rel in changes.modified
        ]
        parts.append(f"modified {', '.join(named)}")
    if changes.mode:
        parts.append(f"executable bit changed on {', '.join(changes.mode)}")
    return "; ".join(parts)


def _clone_once(upstream: _Upstream, owner: str, repo: str) -> None:
    if upstream.clone is not None or upstream.clone_error:
        return
    tmp = tempfile.mkdtemp(prefix="aish-skill-outdated-")
    try:
        skill_import._clone(skill_import.clone_url(owner, repo), tmp)
    except (skill_import.SkillImportError, OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        upstream.clone_error = str(exc) or exc.__class__.__name__
        return
    upstream.clone = Path(tmp)
    upstream.cloned_commit = skill_import.clone_commit(tmp)


def _check_one(
    tree: Path, name: str, upstreams: dict[tuple[str, str], _Upstream]
) -> str:
    entry = skill_import.lock_entry(tree, name)
    files = entry.get("files") if entry else None
    source = entry.get("source") if entry else None
    commit = entry.get("commit") if entry else None
    if not (source and commit and isinstance(files, dict)):
        return "no recorded review (source, commit and file hashes) — delete it and import again"
    recorded = {str(rel): str(digest) for rel, digest in files.items()}
    sub_path = str(entry.get("path") or "") if entry else ""
    try:
        owner, repo, segments = skill_import.check_source(str(source), sub_path)
    except skill_import.SkillImportError:
        where = "/".join(filter(None, (str(source), sub_path)))
        return (
            f"source no longer approved — not checked ({where} is not admitted by "
            f"{skills.SKILL_SOURCES_FILE}; aish did not contact it)"
        )
    key = (owner.casefold(), repo.casefold())
    upstream = upstreams.setdefault(key, _Upstream())
    if upstream.head is None and not upstream.error:
        try:
            upstream.head = skill_import._ls_remote(skill_import.clone_url(owner, repo))
        except (skill_import.SkillImportError, OSError, subprocess.SubprocessError) as exc:
            upstream.error = str(exc) or exc.__class__.__name__
    if upstream.error:
        return f"could not ask the source: {upstream.error}"
    if upstream.head == commit:
        return f"up to date (commit {_short(str(commit))})"
    _clone_once(upstream, owner, repo)
    if upstream.clone is None:
        return (
            f"new commits upstream ({_short(str(commit))} → {_short(upstream.head)}); "
            f"the clone to compare files failed: {upstream.clone_error}"
        )
    moved = f"{_short(str(commit))} → {_short(upstream.cloned_commit)}"
    if upstream.cloned_commit == commit:
        return f"up to date (commit {_short(str(commit))}; the source answered another first)"
    base = upstream.clone.resolve()
    skill_dir = base.joinpath(*segments).resolve()
    if not contains(base, skill_dir):
        return f"new commits upstream ({moved}); its folder now leads out of the repository"
    if not (skill_dir / skill_import.MANIFEST).is_file():
        return f"new commits upstream ({moved}); its folder is gone upstream"
    links = skill_import.symlinks_in(skill_dir)
    if links:
        return (
            f"new commits upstream ({moved}); the folder now holds symlinks "
            f"({', '.join(links)}), which an import refuses"
        )
    new_files, _binaries = skill_import.collect(skill_dir)
    changes = skill_import.compare_install(
        recorded, skill_import.installed_exec(tree / name, recorded),
        skill_import.install_bytes(new_files),
    )
    if not changes:
        return f"new commits upstream ({moved}); this skill's files unchanged ({NOT_COMPARED})"
    return f"CHANGED upstream ({moved}): {changes_line(changes)} ({NOT_COMPARED})"


def check(tree: Path | None = None) -> list[Row]:
    """One row per installed third-party skill. Sources are asked one after
    another and the clones made one after another; nothing here stops early
    on a cancel."""
    tree = tree or skills.VENDOR_SKILLS_DIR
    upstreams: dict[tuple[str, str], _Upstream] = {}
    rows: list[Row] = []
    try:
        for name in _installed_names(tree):
            try:
                line = _check_one(tree, name, upstreams)
            except Exception as exc:  # noqa: BLE001 — one skill must cost only its own row
                line = f"could not be checked: {exc.__class__.__name__}: {exc}"
            rows.append(Row(name, line))
    finally:
        for upstream in upstreams.values():
            if upstream.clone is not None:
                shutil.rmtree(upstream.clone, ignore_errors=True)
    return rows
