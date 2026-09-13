"""The pin ledger: which chats the OWNER has pinned, shared by every device.

The pin used to live only in each browser's IndexedDB mirror, on the reasoning
that "Available offline" is a promise about THIS device's copy. But the promise
the owner is making is about the CHAT — *this one matters, keep it* — and a
chat pinned on the laptop that sinks into the date buckets on the phone is the
app disagreeing with the person using it. Same finding as the seen ledger
(#232): aish has one owner, so a per-device copy of an owner-fact is a bug.

Unlike seen, a pin is a TOGGLE, so the merge cannot be a max — and that is
also why NO CLIENT TIME is trusted here, not even clamped. The seen ledger's
clamp is safe because a max-merge has a safe direction for a clock mistake to
land in (something stays unread). Last-writer-wins has none: clamping a
future-stamped offer to now would PROMOTE a stale toggle to the freshest
action, and honouring a slow clock would silently discard the owner's newest
one. So an offer carries no time at all; every accepted toggle is stamped by
the server as it lands. The semantics are last-RECONNECT-wins: two devices
toggling the same chat opposite ways during an offline window converge on
whichever reconnects later — a near-nonexistent event for one owner, and "the
device I just reconnected shows what I last set on it" is the defensible
outcome even then.

That leaves two rules that make sharing the toggle safe:

**An offer that changes nothing is free.** A device re-offers its unconfirmed
toggles on every connect; one stating what is already held is skipped without
a broadcast, so the repair for a lost message costs nothing and cannot echo.

**A seed may only introduce, never overturn.** The upgrade migrates each
device's existing local pins by offering them as seeds. A seed lands only on a
name the ledger has never heard of: a device that was offline across the
upgrade would otherwise re-offer its stale local pin as a fresh action and
resurrect a pin the owner deliberately removed elsewhere in the meantime.

Forgetting drops tombstones first: an unpin entry exists only so a later seed
cannot resurrect the pin, while a pin entry is the owner's standing promise —
the cap evicts the oldest UNPINNED entries and never silently drops a pin.

A deleted chat's entry is dropped ([MIRROR-FORGET]: gone is gone, pin
included), and the caller is expected to refuse offers naming chats that no
longer exist — a surviving outbox on a device that missed the delete would
otherwise recreate an entry nothing can ever remove. Session names are never
reused, so nothing can inherit a dropped entry.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Entries remembered. Pins themselves are owner-curated and few; the cap exists
# for the unpin tombstones, which only need to outlive any device's re-offer.
PINS_MAX = 300


class PinLedger:
    """chat name → (pinned, epoch seconds the server recorded the toggle)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pins: dict[str, tuple[bool, float]] = {}
        self._load()

    # ---- reading -------------------------------------------------------
    def snapshot(self) -> dict[str, dict[str, bool | float]]:
        return {name: {"pinned": p, "at": at} for name, (p, at) in self._pins.items()}

    def state(self, name: str) -> bool:
        held = self._pins.get(name)
        return held[0] if held else False

    # ---- writing -------------------------------------------------------
    def merge(
        self, marks: dict[str, dict], now: float | None = None
    ) -> dict[str, dict[str, bool | float]]:
        """Fold client-offered toggles in and return ONLY what changed.

        A mark is `{"pinned": bool, "seed": bool?}` — no time: every accepted
        toggle is stamped here, in the one clock the protocol has. An offer
        stating what is already held is skipped, so a re-offer is free and
        idempotent. A seed lands only on an unknown name — it introduces a
        pre-upgrade pin and may never overturn an action already recorded.
        """
        stamped = time.time() if now is None else now
        changed: dict[str, dict[str, bool | float]] = {}
        for name, offered in marks.items():
            if not isinstance(name, str) or not name or not isinstance(offered, dict):
                continue
            pinned = bool(offered.get("pinned"))
            held = self._pins.get(name)
            if offered.get("seed") and held is not None:
                continue
            if held is not None and held[0] == pinned:
                continue  # nothing the owner can see would move
            self._pins[name] = (pinned, stamped)
            changed[name] = {"pinned": pinned, "at": stamped}
        if changed:
            self._trim()
            self._save()
        return changed

    def forget(self, name: str) -> None:
        """The chat no longer exists; its entry goes with it."""
        if self._pins.pop(name, None) is not None:
            self._save()

    # ---- storage -------------------------------------------------------
    def _trim(self) -> None:
        if len(self._pins) <= PINS_MAX:
            return
        # Oldest tombstones first; pins are never silently dropped, so a
        # ledger somehow holding more than the cap in PINS alone simply stays
        # over it — running a few entries long is cheaper than breaking the
        # promise the entry records.
        tombstones = sorted(
            (name for name, (p, _) in self._pins.items() if not p),
            key=lambda name: self._pins[name][1],
        )
        for name in tombstones[: max(0, len(self._pins) - PINS_MAX)]:
            del self._pins[name]

    def _load(self) -> None:
        # A missing or unreadable ledger is not an error: the clients hold the
        # same map and re-offer what the server never confirmed, so the worst
        # case is one round of re-seeding, not a lost history.
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for name, entry in (raw.get("pins") or {}).items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            at = entry.get("at")
            if isinstance(at, (int, float)) and at > 0:
                self._pins[name] = (bool(entry.get("pinned")), float(at))

    def _save(self) -> None:
        # Written whole through a temporary file: a half-flushed ledger read
        # on the next boot would silently drop pins.
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"pins": self.snapshot()}))
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
