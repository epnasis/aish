"""The seen ledger: when the OWNER last read each chat (#232).

Unread used to be a fact about a SCREEN. Each browser kept its own map in
`localStorage` and it never left — *"looking at something is a fact about a
screen, not an account"*. That is the right model for a product with many
users. aish has one owner and one pair of eyes, so a chat read on the phone
whose dot is still on the laptop is the app making a false claim about the
person using it, and the attention band — whose entire value is being short and
true — fills with chats they already know about.

So the ledger lives on the server, where every device can agree with it. Two
properties are what make sharing it safe:

**Monotonic.** A stamp only ever moves FORWARD, so every merge is a `max`.
Arrival order does not matter, a duplicate costs nothing, a re-send is free,
and nothing can ever UN-read a chat. That is what lets a lost broadcast heal on
the next connect instead of needing the receipt machinery (`[ACK-LEDGER]`) — a
client that re-offers everything it knows can only ever be right.

**One clock.** Stamps are the SERVER's, and one offered by a client is clamped
to now. A phone running five minutes fast would otherwise write a seen time
from the future and hide new output in that chat permanently. Clamping can only
leave something unread, which is the direction a mistake is allowed to go.

A deleted chat's stamp deliberately SURVIVES, for the reason `[FORGET-SESSION]`
gives on the client: unread is "output newer than the last look", so dropping
the look is what turns a leftover row into an *alarming* one. There is nothing
to reclaim — the ledger is capped and session names are never reused.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Chats remembered, oldest look dropped first. Matches the client's own cap:
# both sides forget in the same order, so neither can resurrect what the other
# has let go. A chat beyond it falls back to the device's first-run floor,
# which reads as "read" — the conservative direction.
SEEN_MAX = 300


class SeenLedger:
    """chat name → epoch seconds the owner last read it.

    Loads on construction and rewrites on every change. The file is a few
    kilobytes and a change is human-paced (someone opened a chat), so this is
    deliberately synchronous — a thread hop would cost more than the write.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seen: dict[str, float] = {}
        self._load()

    # ---- reading -------------------------------------------------------
    def snapshot(self) -> dict[str, float]:
        return dict(self._seen)

    def stamp(self, name: str) -> float:
        return self._seen.get(name, 0.0)

    # ---- writing -------------------------------------------------------
    def merge(self, marks: dict[str, float | None], now: float | None = None) -> dict[str, float]:
        """Fold client-offered marks in and return ONLY what changed.

        The return value is what gets broadcast: an unchanged stamp publishes
        nothing, so a client re-offering its whole map on every connect — which
        is exactly what makes a lost delta self-healing — is free.

        A mark with no usable time means "now": that is a live "I just opened
        this", where the server's own clock is the honest answer. A mark that
        carries one is a backfill from a device that read the chat while it was
        offline, and it is clamped: never later than now (a fast clock must not
        mark output read that nobody has seen), never earlier than the stamp
        already held (monotonic).
        """
        stamped = time.time() if now is None else now
        changed: dict[str, float] = {}
        for name, offered in marks.items():
            if not isinstance(name, str) or not name:
                continue
            try:
                at = float(offered)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                at = stamped
            if at <= 0 or at > stamped:
                at = stamped
            if at <= self._seen.get(name, 0.0):
                continue
            self._seen[name] = at
            changed[name] = at
        if changed:
            self._trim()
            self._save()
        return changed

    # ---- storage -------------------------------------------------------
    def _trim(self) -> None:
        if len(self._seen) <= SEEN_MAX:
            return
        keep = sorted(self._seen.items(), key=lambda kv: kv[1], reverse=True)[:SEEN_MAX]
        self._seen = dict(keep)

    def _load(self) -> None:
        # A missing or unreadable ledger is not an error: the clients hold the
        # same map and re-offer it on connect, so the worst case is one round
        # of dots coming back, not a lost history.
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for name, at in (raw.get("seen") or {}).items():
            if isinstance(name, str) and isinstance(at, (int, float)) and at > 0:
                self._seen[name] = float(at)
        self._trim()

    def _save(self) -> None:
        # Written whole through a temporary file: a half-flushed ledger read on
        # the next boot would silently un-read whatever it lost.
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"seen": self._seen}))
            os.replace(tmp, self.path)
        except OSError:
            # The dots stay right for this run either way; losing the file
            # costs one round of unread on the next boot and nothing else.
            try:
                tmp.unlink()
            except OSError:
                pass
