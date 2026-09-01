"""Hosts the owner has agreed data may ride an address to — machine-wide, forever.

**Why this is not a chat's log (#295 M3).** `_approved_hosts` was per chat,
restored from that chat's own `egress_vouch` records (#341). Measured on the
owner's own history, that scoping re-collects the same answer indefinitely: the
SAME yes for allegro.pl was given in three separate chats inside one week. And
across his ENTIRE recorded history there are 273 query-carrying page opens
landing on exactly 18 distinct hosts — allegro.pl 76, www.ryanair.com 47,
www.google.com 46, eon.pl 27, www.imdb.com 26, www.ticketmaster.pl 18,
www.linkedin.com 11, www.qatarairways.com 6, then a tail of one-offs. All of it
his ordinary web. So "ask once per host, EVER" costs almost nothing in the
steady state, because the steady state is the new-host rate; "once per host per
chat" costs a card every time he opens a new chat about the same shop.

The grant is HIS OWN ACT, which is what makes widening its scope legal under
epic #295 P3 — *anything that GRANTS permission is a fixed rule, an external
fact, or the owner's own act* — and P6 requires the widened capability to stay
recorded, hence a store that says per host when it arrived and how.

**EXACT hosts, never suffixes.** `www.google.com` vouched must not vouch
`docs.google.com`. That is load-bearing rather than fussy: it is what keeps an
attacker-controlled endpoint parked on a giant's domain still asking. Nothing
here matches — membership is decided by the caller with `in`, against the exact
strings `urlsplit` produced — and nothing here normalises a host beyond the
lowercasing the readers already did.

**This is state, not knowledge.** `~/.config/aish` is auto-committed to a git
remote on a timer, and a list of every host the owner reads is a map of his life;
it has no business there. It lives beside the other machine-wide state, under
`AISH_STATE_DIR`.

**One knob for the store and for the logs it seeds from**, for the reason
`paths.py` records about the config tree: the failure worth designing against is
a run that BELIEVES it is isolated. Seeding reads session logs out of the same
directory the store lives in, so redirecting `state_dir` redirects both and
there is no way to isolate one while sharing the other.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

from .session import SessionLog

STORE_NAME = "egress-vouches.json"

#: Stamped on the file so a future shape change can be RECOGNISED. Nothing reads
#: it back today and this comment says so rather than promising a degradation
#: path no line implements — the first draft claimed an older aish would read a
#: newer store as "hosts I cannot understand", and it would not: `_read` returns
#: whatever `hosts` holds. When a v2 shape arrives, the reader is what has to
#: learn about it; until then this is a stamp, not a check.
VERSION = 1


def state_dir() -> Path:
    """Where machine-wide aish state lives — resolved at CALL time.

    Not bound at import, because `create_app` exports `AISH_STATE_DIR` at
    startup (`server.py`), so a constant frozen at import would point the web
    server's store somewhere the CLI's is not. `browser.state_dir` and
    `explain.state_dir` resolve it the same way, for the same reason."""
    return Path(
        os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish"))
    )


def store() -> Path:
    return state_dir() / STORE_NAME


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _read() -> dict | None:
    """The store as written, or None when it is there and cannot be read."""
    try:
        raw = json.loads(store().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _current() -> dict | None:
    """The store, seeded on first use — or None when the file is present and
    UNREADABLE.

    **Absent and corrupt are two different answers and the difference is the
    whole of this function.** Absent means the migration has never run, so it
    runs. Corrupt means there are bytes here that nobody can parse, and those
    are never written over: re-running the migration on top of them would
    destroy the only record of what was granted, which is what epic #295 P6
    exists to protect. Both degrade the same way — no hosts, so the next
    address asks — and asking costs a card while clobbering costs the record."""
    if store().exists():
        return _read()
    data = _seeded()
    # A failed write costs a re-scan next time and never a wrong answer, so it
    # is not worth failing a session over.
    try:
        _write(data)
    except OSError:
        pass
    return data


def _write(data: dict) -> None:
    """Write the store so a reader never sees half of one.

    The scratch name carries the PID because `aish-web` and a terminal session
    write the same file: with one shared scratch name, two writers racing would
    interleave inside it and `replace` would then publish a file that is neither
    of theirs. A losing racer costs one host and one later card; a torn file
    costs the whole record, and `_current` would refuse to repair it — correctly
    — for the rest of the machine's life."""
    path = store()
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        scratch.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        scratch.replace(path)
    finally:
        scratch.unlink(missing_ok=True)


def hosts() -> list[str]:
    """Every host the owner has agreed data may ride an address to.

    Seeds itself from his own recorded acts on FIRST use and never again: the
    existence of the file is the record that the migration ran, so a machine
    with an empty-but-present store is one that has been asked and answered
    nothing, not one that has never looked. Nothing re-scans at startup."""
    data = _current()
    found = data.get("hosts") if data else None
    return sorted(found) if isinstance(found, dict) else []


def add(new: list[str]) -> None:
    """Record hosts the owner has just vouched for on a card.

    Read-modify-write rather than a lock: `aish-web` and a terminal session can
    both be running, and two writes racing lose one host — which costs one more
    card the next time it is reached and can never grant one that was not
    approved. The safe direction is also the cheap one here.

    An unreadable store is left exactly as it is, for the reason `_current`
    gives: the vouch then lives only in the agent that collected it, and the
    next one asks again. Losing a yes costs a card; overwriting bytes nobody can
    read costs the record."""
    wanted = [host for host in new if host]
    if not wanted:
        return
    data = _current()
    if data is None:
        return
    found = data.get("hosts")
    if not isinstance(found, dict):
        found = {}
    for host in wanted:
        found.setdefault(host, {"added": _now(), "how": "card"})
    data["hosts"] = found
    try:
        _write(data)
    except OSError:
        pass


def _seeded() -> dict:
    """The store as it is born: every yes the owner has already given.

    **A yes he has already given is his act, so it counts.** Re-asking for
    allegro.pl on a machine whose own logs record him approving it dozens of
    times is the thing this slice exists to stop. Two sources, both of them acts
    he PERFORMED rather than pages he happened to visit: an `egress_vouch`
    record (an approved egress card) and an approved read whose command names a
    URL. No heuristics, no external list, no inference.

    Measured on the owner's machine before this shipped: of the 18 hosts his 273
    query-carrying page opens land on, 11 are covered by approvals already in
    his logs and 7 would ask once, ever.

    **One scan, ever.** The result is written and the written file is what later
    runs read. A log that cannot be read is skipped rather than failing the
    scan: one corrupt session file has taken the whole web server down before,
    and a reader over many files must fail per file."""
    found: dict[str, dict] = {}
    at = _now()
    scanned = 0
    try:
        paths = sorted(state_dir().glob("session-*.jsonl"))
    except OSError:
        paths = []
    for path in paths:
        try:
            seen = SessionLog.egress_vouches(path) + SessionLog.approved_read_hosts(path)
        except (OSError, ValueError):
            continue
        scanned += 1
        for host in seen:
            found.setdefault(host, {"added": at, "how": "seeded"})
    return {
        "version": VERSION,
        # What the migration did. Kept because the capability it granted must
        # not outrun the record of where it came from (#295 P6).
        "seeded": {"at": at, "logs": scanned, "hosts": len(found)},
        "hosts": found,
    }
