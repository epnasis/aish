"""Standing approvals for third-party skill runs — "Always for this skill"
(epic #467 slice 2c).

**A grant is the owner's own act, recorded** (the `vouches.py` precedent, epic
#295 P3/P6): it is written only when he chooses "Always for this skill" on a
`use_skill` card, and only with content the AGENT composed for that card — the
client picks an offer, it never supplies one. Nothing else in aish writes this
file: the file and skill tools refuse it and so does the shell fence
(`skills.third_party_write_refusal`, `Agent._command_touches_third_party`).

**Bound to the skill's review identity**, not its name alone: the per-file
hashes the lock recorded at install. A grant covers a run only while the lock
still records exactly those hashes, so a reinstall or update with different
files is never covered, and a skill with no recorded hashes is never grantable
(its card offers no "Always"). A byte-identical reinstall IS covered — it is
the same reviewed text.

**Append-only acts with tombstones.** One JSON object per line:
`{"act": "granted", "skill", "files", "at", "how"}` or
`{"act": "revoked", "skill", "at", "how"}`. The LATEST act for a skill decides;
`aish skill revoke NAME` appends a revocation, and the owner may edit the file
by hand. A file that is present and has any line this cannot read grants
NOTHING — a malformed revocation must not let the grant above it stand.

**In the config tree on purpose**, unlike the vouches store: it is a short list
of skill names he chose to trust, which belongs with the skills and the sources
file it qualifies, and it is backed up with them. That cuts both ways and is
named rather than hidden: the git-backed tree can bring a revoked grant back on
a restore, and the file rides to the private backup remote.
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass, field

from . import skills

GRANTED = "granted"
REVOKED = "revoked"
#: The `policy` a cardless run names in the owner-facing line and the record.
STANDING_GRANT = "standing grant"


@dataclass(frozen=True)
class Offer:
    """What one `use_skill` card offers to remember: the skill and the hashes
    the agent read for that card. Composed by the agent, never by a client."""

    skill: str
    files: dict[str, str] = field(hash=False)

    def wire(self) -> dict:
        """What the card shows: the name and how many files are bound — the
        hashes themselves stay on this side."""
        return {"skill": self.skill, "files": len(self.files)}


@dataclass(frozen=True)
class Grant:
    """A standing grant that covers a run: when he gave it and on what."""

    skill: str
    at: str
    how: str


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _acts() -> list[dict] | None:
    """Every act in the file, in order; [] when there is no file; None when
    the file is there and cannot be read whole — which grants nothing."""
    path = skills.SKILL_GRANTS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        return None
    acts: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            act = json.loads(line)
        except ValueError:
            return None
        if not _well_formed(act):
            return None
        acts.append(act)
    return acts


def _well_formed(act: object) -> bool:
    if not isinstance(act, dict) or not isinstance(act.get("skill"), str):
        return False
    if act.get("act") == REVOKED:
        return True
    files = act.get("files")
    return (
        act.get("act") == GRANTED
        and isinstance(files, dict)
        and bool(files)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
    )


def readable() -> bool:
    """Can a grant recorded now be honoured? False when the file is present
    and unreadable — the card then offers no "Always", since one recorded
    into that file would be ignored."""
    return _acts() is not None


def standing(skill: str, recorded: dict[str, str] | None) -> Grant | None:
    """The grant covering a run of `skill` whose lock records `recorded`, or
    None. Exact name, and exact equality of the whole hash map: one file
    added, removed or changed since the grant is not covered."""
    if not recorded:
        return None
    acts = _acts()
    if not acts:
        return None
    latest = next((a for a in reversed(acts) if a["skill"] == skill), None)
    if latest is None or latest["act"] != GRANTED or latest["files"] != recorded:
        return None
    return Grant(skill=skill, at=str(latest.get("at", "")), how=str(latest.get("how", "")))


def last_act(skill: str) -> str:
    """The latest act recorded for `skill` — GRANTED, REVOKED — or "" when
    there is none, or "unreadable" when the file cannot be read whole. Says
    what the file holds, not whether a run would be covered."""
    acts = _acts()
    if acts is None:
        return "unreadable"
    return next((a["act"] for a in reversed(acts) if a["skill"] == skill), "")


def _append(act: dict) -> bool:
    """One line, one `write` on an O_APPEND descriptor, so a terminal and
    aish-web recording at once cannot interleave inside a line."""
    path = skills.SKILL_GRANTS_FILE
    line = json.dumps(act, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def record(offer: Offer, how: str) -> Grant | None:
    """Record the owner's "Always for this skill" for exactly what the card
    offered, and return the grant — or None when it could not be recorded
    (an unreadable file, which is left as it is, or a failed write)."""
    if not offer.files or _acts() is None:
        return None
    at = _now()
    act = {"act": GRANTED, "skill": offer.skill, "files": dict(offer.files),
           "at": at, "how": how}
    return Grant(skill=offer.skill, at=at, how=how) if _append(act) else None


def revoke(skill: str, how: str) -> bool:
    """Append a revocation. Appended whatever the file holds: a revocation can
    only ever take a permission away, so it is safe even into a file this
    cannot read — and it stands once that file is repaired."""
    return _append({"act": REVOKED, "skill": skill, "at": _now(), "how": how})
