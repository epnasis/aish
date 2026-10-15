"""The scheduler inside aish-web: fires the owner's one-shot automations (epic #142 W2, S1).

`schedule.py` decides WHAT is due; this decides WHEN to look and what happens
around a fire. It never opens a session itself: `fire` is the server's shared
trigger seam, the same guarded path `POST /trigger` takes (dedup, per-origin
rate bucket, concurrency cap), so a scheduled run is an ordinary triggered
session with `origin="schedule"` and nothing else.

Ordering, and the window it accepts. The fired ledger is written right after
the seam reports a session was OPENED — the same order `handle_trigger` uses
for its dedup store, so a failed open never poisons the key. Marking after the
run COMPLETES instead would re-fire a crashed half-done run as a duplicate. A
process killed between the open and the ledger write fires that run again
after restart; that window is accepted, and it is milliseconds wide.

A refusal is not a fire. A rate or concurrency refusal marks nothing and the
next tick asks again; only the deadline (`schedule.STALE_AFTER`) ends it.

Everything that touches the disk runs through `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import schedule
from .schedule import DUE, EXPIRED, LedgerUnreadable, Task

#: How often the loop looks. A one-shot asked for "after 10pm" is honoured to
#: within this; the per-tick cost is one listdir and one stat per task file.
TICK_S = 30.0

#: How long a run whose session could not be OPENED waits before it is tried
#: again. Retrying is right (the owner may fix the model, a backend may come
#: back) but every tick would rebuild an unbuildable backend twice a minute.
OPEN_RETRY_AFTER = timedelta(minutes=5)

#: Records kept in the state file; older ones are dropped first.
MAX_RECORDS = 200

# What the trigger seam answers. `STARTED` and `DEDUPED` both mean a session
# exists for this key; the three refusals mean none does.
STARTED = "started"
DEDUPED = "deduped"
RATE_LIMITED = "rate_limited"
AT_CAPACITY = "at_capacity"
OPEN_FAILED = "open_failed"


@dataclass(frozen=True)
class TriggerOutcome:
    """What the shared trigger seam did. `error` is the observed refusal or
    exception text, stated as the seam saw it and nothing more."""

    status: str
    session: str = ""
    error: str = ""

    @property
    def opened(self) -> bool:
        return self.status in (STARTED, DEDUPED)


@dataclass(frozen=True)
class FireRequest:
    task: Task
    prompt: str
    scheduled_for: datetime
    fired_at: datetime

    @property
    def dedup_key(self) -> str:
        return self.task.key


def run_prompt(task: Task, fired_at: datetime) -> str:
    """The prompt a run starts from. Both times are stated because a late fire
    is something the run can OBSERVE and should not have to infer. The sites
    and prefixes are restated because the trigger prompt is owner-authored
    text, and that is where the egress gate reads the hosts he named."""
    lines = [
        f"[scheduled: {task.id}] An automation the owner approved has fired.",
        f"Scheduled for {task.at.isoformat(sep=' ', timespec='minutes')}; "
        f"fired at {fired_at.isoformat(sep=' ', timespec='seconds')}.",
        "Nobody is watching this run. Anything that changes state and needs approval "
        "is held on a card until the owner answers it; a denial is final. When you "
        "finish, report what you did, what you found, and what is held for approval.",
    ]
    if task.allow:
        lines.append("Commands approved for this run: " + ", ".join(task.allow) + ".")
    if task.sites:
        lines.append("Sites you may act on as the owner: " + ", ".join(task.sites) + ".")
    return "\n".join(lines) + "\n\n" + task.prompt


def _stamp(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


@dataclass
class _State:
    """What the scheduler remembers beyond the ledger: the malfunctions it
    recorded, the keys it gave up on, and what it already pushed about."""

    records: list[dict[str, Any]] = field(default_factory=list)
    settled: dict[str, str] = field(default_factory=dict)
    notified: list[str] = field(default_factory=list)
    problems: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": self.records[-MAX_RECORDS:],
            "settled": self.settled,
            "notified": self.notified[-MAX_RECORDS:],
            "problems": self.problems,
        }


class Scheduler:
    """One loop per server. `fire` is the trigger seam, `notify(title, body)`
    a push that never raises (`notify.pushover` already is one); `clock` and
    `sleep` are seams so the tests use a fake clock and no real waits."""

    def __init__(
        self,
        *,
        state_dir: Path,
        fire: Callable[[FireRequest], Awaitable[TriggerOutcome]],
        notify: Callable[[str, str], object],
        directory: Callable[[], Path] = schedule.automations_dir,
        deny_prefixes: Callable[[], Collection[str]] = lambda: (),
        clock: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        tick_s: float = TICK_S,
    ) -> None:
        self.fire = fire
        self.notify = notify
        self.directory = directory
        self.deny_prefixes = deny_prefixes
        self.clock = clock
        self.sleep = sleep
        self.tick_s = tick_s
        self.ledger = schedule.fired_ledger(state_dir)
        self.state_store = schedule.scheduler_state(state_dir)
        self.tasks: list[Task] = []
        self.problems: list[schedule.LoadProblem] = []
        self._signature: tuple | None = None
        self._state: _State | None = None
        # False when the state file exists and cannot be read: never write
        # over it, because absent and corrupt are different answers.
        self._state_writable = True
        # Fired keys this process knows of, so a ledger write that FAILED does
        # not turn into a second fire on the next tick.
        self._fired_here: set[str] = set()
        self._retry_open_at: dict[str, datetime] = {}
        self._last_refusal: dict[str, str] = {}
        self._ledger_problem_pushed = False
        self._ticking = False

    # -- the loop -------------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the loop outlives any one tick
                print(f"[scheduler] tick failed: {exc!r}", file=sys.stderr)
            await self.sleep(self.tick_s)

    async def tick(self) -> None:
        # One tick at a time. Two interleaved ticks could both read a key as
        # unfired while the first is still inside the open, and the server's
        # dedup store is only written after that open returns.
        if self._ticking:
            return
        self._ticking = True
        try:
            await self._tick()
        finally:
            self._ticking = False

    async def _tick(self) -> None:
        if self._state is None:
            self._state = await asyncio.to_thread(self._load_state)
        await self._reload_if_changed()
        try:
            ledger = await asyncio.to_thread(self.ledger.read)
        except LedgerUnreadable as exc:
            await self._ledger_unreadable(str(exc))
            return
        fired = set(ledger) | self._fired_here
        for task in list(self.tasks):
            if task.key in self._state.settled:
                continue
            now = self.clock()
            status = schedule.next_due(task, fired, now)
            if status == EXPIRED:
                await self._expire(task, now)
            elif status == DUE:
                await self._fire(task, now)

    # -- loading --------------------------------------------------------------

    def _load_state(self) -> _State:
        try:
            data = self.state_store.read()
        except LedgerUnreadable as exc:
            self._state_writable = False
            print(f"[scheduler] state file unreadable, not writing over it: {exc}",
                  file=sys.stderr)
            return _State()
        return _State(
            records=list(data.get("records") or []),
            settled=dict(data.get("settled") or {}),
            notified=list(data.get("notified") or []),
            problems=list(data.get("problems") or []),
        )

    async def _reload_if_changed(self) -> None:
        directory = self.directory()
        current = await asyncio.to_thread(schedule.signature, directory)
        if current == self._signature:
            return
        # The deny list is a file read too, so it is read inside the thread.
        loaded = await asyncio.to_thread(
            lambda: schedule.load_tasks(directory, deny_prefixes=list(self.deny_prefixes()))
        )
        self._signature = current
        self.tasks = loaded.tasks
        self.problems = loaded.problems
        assert self._state is not None
        self._state.problems = [{"path": p.path, "error": p.error} for p in loaded.problems]
        mtimes = {name: mtime for name, mtime, _size in current}
        for problem in loaded.problems:
            print(f"[scheduler] {problem.path} is not a runnable automation and was "
                  f"skipped: {problem.error}", file=sys.stderr)
            # Once per version of the file: an owner mid-edit is pushed about
            # the version he saved, never every tick about the same one.
            name = Path(problem.path).name
            await self._push_once(
                f"file:{name}:{mtimes.get(name, 0)}",
                f"aish automation file {name} was skipped",
                f"{name} is not a runnable automation, so it will not run: {problem.error}",
            )
        await self._save_state()

    # -- outcomes -------------------------------------------------------------

    async def _fire(self, task: Task, now: datetime) -> None:
        retry_at = self._retry_open_at.get(task.key)
        if retry_at is not None and now < retry_at:
            return
        request = FireRequest(task, run_prompt(task, now), task.at, now)
        outcome = await self.fire(request)
        if outcome.opened:
            self._retry_open_at.pop(task.key, None)
            self._last_refusal.pop(task.key, None)
            await self._mark_fired(task, outcome.session, now)
            return
        self._last_refusal[task.key] = outcome.error or outcome.status
        if outcome.status != OPEN_FAILED:
            return  # rate/capacity: nothing marked, the next tick asks again
        self._retry_open_at[task.key] = now + OPEN_RETRY_AFTER
        await self._record(task, "open_failed", now, error=outcome.error)
        await self._push_once(
            f"{task.key}:open_failed",
            f"aish automation {task.id} did not start",
            f"automation {task.id} did not start: {outcome.error}. aish will try again "
            f"until {task.deadline.strftime('%H:%M %a %Y-%m-%d')}.",
        )

    async def _mark_fired(self, task: Task, session: str, now: datetime) -> None:
        self._fired_here.add(task.key)
        entry = {"fired_at": _stamp(now), "scheduled_for": _stamp(task.at),
                 "session": session}

        def write() -> None:
            data = self.ledger.read()
            data[task.key] = entry
            self.ledger.write(data)

        try:
            await asyncio.to_thread(write)
        except (OSError, LedgerUnreadable) as exc:
            # Still fired, and this process will not fire it again; a restart
            # before the ledger is writable would. Said where it can be seen.
            print(f"[scheduler] {task.key} fired as {session} but the ledger "
                  f"could not be written: {exc}", file=sys.stderr)
            await self._record(task, "ledger_write_failed", now, error=str(exc),
                               session=session)

    async def _expire(self, task: Task, now: datetime) -> None:
        assert self._state is not None
        # A refusal this process saw, else one an earlier process RECORDED —
        # only "no attempt was observed" when neither exists.
        last = self._last_refusal.get(task.key) or next(
            (r.get("error", "") for r in reversed(self._state.records)
             if r.get("key") == task.key and r.get("kind") == "open_failed"),
            "",
        )
        observed = (
            f"it was last refused with: {last}" if last
            else "aish did not observe an attempt to start it"
        )
        self._state.settled[task.key] = "expired"
        await self._record(task, "expired", now, error=observed)
        # Its own key: the terminal sentence must reach him even when a
        # "did not start, will try again" push already went out for this run.
        await self._push_once(
            f"{task.key}:expired",
            f"aish automation {task.id} did not run",
            f"automation {task.id} did not run: it was scheduled for "
            f"{task.at.strftime('%H:%M %a %Y-%m-%d')} and had not started "
            f"{int(schedule.STALE_AFTER.total_seconds() // 3600)} h later; {observed}. "
            "It will not run.",
        )

    async def _ledger_unreadable(self, error: str) -> None:
        # Firing with an unreadable ledger could re-fire everything in it, so
        # nothing fires until it reads again — and the owner hears it once.
        print(f"[scheduler] fired ledger unreadable, firing nothing: {error}",
              file=sys.stderr)
        if not self._ledger_problem_pushed:
            self._ledger_problem_pushed = True
            await self._notify(
                "aish automations are paused",
                f"aish cannot read its record of fired automations ({error}), so it "
                "is firing none until it can.",
            )

    async def _record(self, task: Task, kind: str, now: datetime, **extra: str) -> None:
        assert self._state is not None
        self._state.records.append({
            "kind": kind, "task": task.id, "key": task.key,
            "scheduled_for": _stamp(task.at), "observed_at": _stamp(now), **extra,
        })
        await self._save_state()

    async def _push_once(self, key: str, title: str, body: str) -> None:
        assert self._state is not None
        if key in self._state.notified:
            return
        self._state.notified.append(key)
        await self._save_state()
        await self._notify(title, body)

    async def _notify(self, title: str, body: str) -> None:
        # The record above is the durable half; a push is best-effort and must
        # never take the loop down with it.
        try:
            await asyncio.to_thread(self.notify, title, body)
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] notification failed: {exc!r}", file=sys.stderr)

    async def _save_state(self) -> None:
        if not self._state_writable or self._state is None:
            return
        data = self._state.to_dict()
        try:
            await asyncio.to_thread(self.state_store.write, data)
        except OSError as exc:
            print(f"[scheduler] could not write {self.state_store.path}: {exc}",
                  file=sys.stderr)
