"""Update one installed third-party skill to its source's current version
(#467 slice 4c) — `update_skill(name)` and `aish skill update NAME`.

Nothing changes without the owner (P4): every update is a card showing a
diff of the INSTALLED bytes — verified against the lock's hashes before
anything else happens — against the bytes that WILL install
(`skill_import.install_bytes` of the new copy), and on approval exactly
those bytes are written.

`prepare` reads; it writes nothing anywhere. It takes the installed NAME
only: the source and the folder inside it come from the lock, never from
the caller, so an update cannot switch publisher (that is delete + import).
It refuses, before any network step, an entry with no recorded source,
commit, path or file hashes, and an installed folder that differs from its
hashes. The new copy is fetched through `skill_import.stage` — the source
re-checked against `skill-sources` NOW, redirects off, symlinks refused —
into stage's own temporary clone, read whole into memory and the clone
removed before `prepare` returns.

`apply` writes, in this order: the new files into a folder under the
QUARANTINE root (never inside the third-party tree — a staged SKILL.md
there would win the skill's name and be served unchecked to any chat),
then the old folder is removed and the staged one moved in (a copy when the
two trees are on different disks), then the skill's Python environment is
installed again, then the lock entry. Until the lock is written, every read
aish SERVES from the folder is refused, because the files on disk are not
the ones the lock's hashes describe; that window spans the environment
install, which can take minutes. That is fail-closed, not atomic.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import skill_env, skill_grants, skill_import, skills
from .files import make_diff
from .skill_import import SkillImportError

#: Beside the pending imports in the quarantine root, under a name that holds
#: no SKILL.md and that `aish skill approve` refuses as a skill name, so a
#: staged update can never read as a pending import.
UPDATES_DIR = ".updates"

BINARIES_NOT_COMPARED = (
    "binary files are skipped, as at import: they are not installed, compared or shown"
)

ADDED, REMOVED, MODIFIED, MODE = "added", "removed", "modified", "executable bit changed"


def _again(why: str) -> SkillImportError:
    return SkillImportError(
        f"NOT updated: {why} Nothing was changed. To take the source's current "
        "version: delete it and import again."
    )


@dataclass(frozen=True)
class FileDiff:
    """One changed file on the card: its status and a unified diff of the
    installed bytes against the bytes that will install, bounded by
    `files.make_diff`. `note` says what is not shown, and why."""

    path: str
    status: str
    diff: str
    note: str = ""

    def as_json(self) -> dict[str, str]:
        return {"path": self.path, "status": self.status, "diff": self.diff, "note": self.note}


@dataclass(frozen=True)
class Prepared:
    """An update read and compared, waiting for the owner's decision."""

    name: str  # the installed folder, which is also the lock's key
    folder: Path
    entry: dict  # the lock entry as read, compared again at apply
    old_name: str  # the installed SKILL.md's declared name, before the update
    declared: str  # the new SKILL.md's declared name
    description: str
    old_commit: str
    new_commit: str | None
    files: list[tuple[str, str, bool]]  # keywords cleaned: what the card shows
    dropped: list[str]
    skipped: list[str]
    planned: dict[str, skill_import.InstallFile]
    recorded: dict[str, str]
    changes: skill_import.FileChanges
    diffs: list[FileDiff]
    old_needs: skill_env.Needs
    new_needs: skill_env.Needs

    @property
    def source(self) -> str:
        return str(self.entry["source"])

    @property
    def sub_path(self) -> str:
        return str(self.entry["path"])

    def commits(self) -> str:
        return f"commit {_short(self.old_commit)} → {_short(self.new_commit)}"

    def flags(self) -> list[str]:
        """The import card's own review flags, of the NEW bundle."""
        return (
            skill_import.safety_scan(self.files)
            + skill_import.keywords_flag(self.dropped)
            + skill_env.card_flags(self.new_needs)
        )

    def packages(self) -> list[str]:
        return package_delta(self.old_needs, self.new_needs)

    def card(self) -> dict[str, Any]:
        """What the card adds to the import card. Names and diffs here are
        the publisher's text: they reach the owner's card and terminal only,
        never the model's tool result."""
        return {
            "replaces": self.name,
            "from_commit": self.old_commit,
            "to_commit": self.new_commit or "",
            "summary": f"replaces {self.name} ({self.commits()})",
            "changes": skill_outdated_line(self.changes),
            "diffs": [d.as_json() for d in self.diffs],
            "packages": self.packages(),
            "binaries": BINARIES_NOT_COMPARED,
        }

    def counts(self) -> str:
        """The tool result's account of what changed — counts composed by
        aish, never a file name or a line of the diff (the auditor-findings
        precedent: a stranger's text reaches the owner's card, not the model)."""
        return (
            f"{len(self.changes.added)} file(s) added, {len(self.changes.removed)} removed, "
            f"{len(self.changes.modified)} modified, {len(self.changes.mode)} with the "
            "executable bit changed"
        )


def _short(commit: str | None) -> str:
    return commit[:12] if commit else "unknown"


def skill_outdated_line(changes: skill_import.FileChanges) -> str:
    from .skill_outdated import changes_line

    return changes_line(changes)


def package_delta(old: skill_env.Needs, new: skill_env.Needs) -> list[str]:
    """What the update does to the skill's Python environment, said from the
    two bundles' own imports (`skill_env.needs`)."""
    lines: list[str] = []
    if old.python and not new.python:
        return ["the new version carries no Python: its environment is removed"]
    if new.python and not old.python:
        lines.append("the new version carries Python: an environment is installed for it")
    added = sorted(set(new.packages) - set(old.packages))
    removed = sorted(set(old.packages) - set(new.packages))
    if added:
        lines.append(f"packages added: {', '.join(added)}")
    if removed:
        lines.append(f"packages removed: {', '.join(removed)}")
    if new.python:
        kept = sorted(set(new.packages) & set(old.packages))
        if kept:
            lines.append(f"packages kept: {', '.join(kept)}")
        lines.append(
            "the environment is installed again on approval: every package at the "
            "version the index serves then (wheels only), recorded in the lock"
        )
    return lines


def _installed_folder(name: str) -> str:
    """The installed folder `name` names: a folder of the third-party tree
    by that name (its spelling on disk), or the folder of the one
    third-party skill whose declared name it is."""
    tree = skills.VENDOR_SKILLS_DIR
    try:
        children = [c for c in tree.iterdir() if c.name != skill_import.LOCK_NAME]
    except OSError:
        children = []
    folders = [c for c in children if c.name.casefold() == name.casefold()]
    if not folders:
        folders = [
            e.path.parent for e in skills._dir_entries(tree, "skill")
            if e.name == name and e.path is not None and e.path.name == skill_import.MANIFEST
        ]
    if len(folders) != 1:
        listed = ", ".join(sorted(c.name for c in children)) or "none"
        raise SkillImportError(
            f"NOT updated: no installed third-party skill named {name!r}. Installed: "
            f"{listed}. Nothing was changed."
        )
    folder = folders[0]
    if folder.is_symlink() or not folder.is_dir():
        raise _again(f"{folder} is not a folder aish installed (a link or a flat file).")
    return folder.name


def _read_installed(folder: Path, recorded: dict[str, str]) -> dict[str, bytes]:
    """Every recorded file's bytes, from ONE read each — the bytes that are
    then verified and diffed, so the card's left side is what was checked."""
    out: dict[str, bytes] = {}
    for rel in recorded:
        path = folder / rel
        if path.is_file() and not path.is_symlink():
            try:
                out[rel] = path.read_bytes()
            except OSError:
                continue
    return out


def _verified(name: str, folder: Path, recorded: dict[str, str]) -> dict[str, bytes]:
    installed = _read_installed(folder, recorded)
    found = skill_import.check_folder(folder, recorded, installed)
    if found:
        raise _again(
            f"third-party skill {name!r} differs from what was installed after review — "
            f"{'; '.join(found)} — so there is no reviewed version to update from."
        )
    return installed


def _diffs(
    installed: dict[str, bytes],
    planned: dict[str, skill_import.InstallFile],
    changes: skill_import.FileChanges,
) -> list[FileDiff]:
    out: list[FileDiff] = []
    for rel in sorted(set(changes.added) | set(changes.removed) | set(changes.modified)
                      | set(changes.mode)):
        status = (
            ADDED if rel in changes.added else REMOVED if rel in changes.removed
            else MODIFIED if rel in changes.modified else MODE
        )
        old_bytes = installed.get(rel, b"")
        new_bytes = planned[rel].data if rel in planned else b""
        notes: list[str] = []
        if rel in changes.newline_only:
            notes.append("only a final newline added")
        if rel in changes.mode:
            notes.append(f"executable bit {'set' if planned[rel].executable else 'cleared'}")
        note = "; ".join(notes)
        try:
            old_text = old_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # Installed before the install paths were one: a binary file the
            # quarantine held then. It is not text, so no diff of it is shown.
            out.append(FileDiff(rel, status, "", "binary in the installed copy; not shown"))
            continue
        diff = make_diff(old_text, new_bytes.decode("utf-8"), rel) if old_bytes != new_bytes else ""
        out.append(FileDiff(rel, status, diff, note))
    return out


def prepare(name: str) -> Prepared:
    """Everything the card shows, read and compared; nothing written.
    Raises SkillImportError — every refusal says nothing was changed."""
    if not skills.NAME_RE.match(name or ""):
        raise SkillImportError(f"NOT updated: {name!r} is not a skill name. Nothing was changed.")
    tree = skills.VENDOR_SKILLS_DIR
    folder_name = _installed_folder(name)
    folder = tree / folder_name
    entries = skill_import._lock_entries(tree) or {}
    entry = entries.get(folder_name)
    if not isinstance(entry, dict):
        spelled = [k for k in entries if str(k).casefold() == folder_name.casefold()]
        raise _again(
            f"the lock has no entry spelled {folder_name!r}"
            + (f" (it has {', '.join(map(repr, spelled))})" if spelled else "")
            + ", so there is no recorded review to update from."
        )
    source, commit, sub_path, files = (
        entry.get("source"), entry.get("commit"), entry.get("path"), entry.get("files")
    )
    if not (isinstance(source, str) and source and isinstance(commit, str) and commit
            and isinstance(sub_path, str) and isinstance(files, dict) and files):
        raise _again(
            f"the lock records no complete review for {folder_name!r} (its source, commit, "
            "path and file hashes)."
        )
    recorded = {str(rel): str(digest) for rel, digest in files.items()}
    # FIRST, before any network step: an update replaces reviewed bytes, so
    # the bytes on disk must be the reviewed ones the card diffs against.
    installed = _verified(folder_name, folder, recorded)
    old_name = skills._parse(folder / skill_import.MANIFEST, "skill").name
    try:
        declared, description, staged, skipped, tmp = skill_import.stage(source, sub_path)
    except SkillImportError as exc:
        raise SkillImportError(
            f"NOT updated: the source's current version could not be staged — {exc} "
            "Nothing was changed."
        ) from None
    try:
        new_commit = skill_import.clone_commit(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    reserved = skill_import.reserved_name_error([declared])
    if reserved is not None:
        raise SkillImportError(f"NOT updated: {reserved}. Nothing was changed.")
    # The new version may declare another name; it must not clash with any
    # OTHER skill, in either tree. The folder being replaced is the one exemption.
    clash = skill_import.existing_skill(sorted({folder_name, declared}), excluding=folder)
    if clash is not None:
        raise SkillImportError(
            f"NOT updated: the new version of {folder_name!r} declares the name "
            f"{declared!r}, and a skill answering to it already exists at {clash}. Nothing "
            "was changed."
        )
    cleaned, dropped = skill_import.english_manifest(staged)
    planned = skill_import.install_bytes(cleaned)
    changes = skill_import.compare_install(
        recorded, skill_import.installed_exec(folder, recorded), planned
    )
    if not changes:
        raise SkillImportError(
            f"NOT updated: {folder_name!r} is already what its source holds "
            f"({_short(commit)} → {_short(new_commit)}; text files compared, binary files "
            "are not). Nothing was changed."
        )
    old_needs = skill_env.needs(
        (rel, data.decode("utf-8", errors="replace")) for rel, data in installed.items()
        if rel.endswith((".py", ".md"))
    )
    return Prepared(
        name=folder_name, folder=folder, entry=entry, old_name=old_name, declared=declared,
        description=description, old_commit=commit, new_commit=new_commit, files=cleaned,
        dropped=dropped, skipped=skipped, planned=planned, recorded=recorded,
        changes=changes, diffs=_diffs(installed, planned, changes),
        old_needs=old_needs, new_needs=skill_env.needs((r, t) for r, t, _x in cleaned),
    )


@dataclass(frozen=True)
class Applied:
    notes: list[str]
    grant: str  # what became of a standing "Always for this skill"; "" when none


def apply(prepared: Prepared, audit: Any = None) -> Applied:
    """Install exactly `prepared.planned` in place of the installed folder.
    Raises SkillImportError saying what state the skill was left in."""
    tree = skills.VENDOR_SKILLS_DIR
    name, folder = prepared.name, prepared.folder
    # Again, at the write: the card may have been open for minutes.
    if skill_import.lock_entry(tree, name) != prepared.entry:
        raise SkillImportError(
            f"NOT updated: the lock's entry for {name!r} changed while the card was open. "
            "Nothing was changed; ask for the update again."
        )
    if folder.is_symlink() or not folder.is_dir():
        raise SkillImportError(
            f"NOT updated: {folder} is no longer an installed folder. Nothing was changed."
        )
    _verified(name, folder, prepared.recorded)
    grant_before = skill_grants.standing(prepared.old_name, prepared.recorded)
    updates = skill_import.quarantine_root() / UPDATES_DIR
    staging: Path | None = None
    try:
        updates.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=updates))
        skill_import.write_install(staging / name, prepared.planned)
    except OSError as exc:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise SkillImportError(
            f"NOT updated: the new version could not be staged ({exc}). Nothing was changed."
        ) from None
    try:
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            raise SkillImportError(
                f"the old version of {name!r} could not be removed whole ({exc}); what is left "
                "differs from the lock, so aish refuses to serve it. The owner deletes "
                f"{folder} and imports it again."
            ) from None
        try:
            # A rename on one disk; a copy (which keeps the executable bits)
            # when the quarantine root and the tree are on two.
            shutil.move(str(staging / name), str(folder))
        except OSError as exc:
            raise SkillImportError(
                f"the old version of {name!r} was removed and the new one could not be "
                f"moved in ({exc}); whatever is at {folder} differs from the lock, so aish "
                f"refuses to serve it. The owner deletes {folder} and imports it again."
            ) from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    # Unconditionally: the old environment was made for the old files. A
    # version that carries no Python leaves none behind to be found by a run.
    shutil.rmtree(skill_env.env_dir(name), ignore_errors=True)
    env = skill_import.install_env(name, folder)
    lock_note = skill_import.lock_failure_note(
        tree, name, prepared.source, prepared.sub_path, prepared.new_commit, env, audit,
        previous=prepared.entry,
    )
    notes = [n for n in (skill_env.install_note(env), lock_note) if n]
    grant = ""
    if grant_before is not None:
        after = skill_import.recorded_hashes(tree, name)
        grant = (
            "its standing approval (Always for this skill) still covers it: the files' "
            "bytes are unchanged"
            if skill_grants.standing(prepared.declared, after) is not None else
            "its standing approval (Always for this skill) no longer covers it: its next "
            "run asks on a card"
        )
    return Applied(notes=notes, grant=grant)
