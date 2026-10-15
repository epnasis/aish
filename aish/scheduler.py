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
next tick asks again; for a one-shot only the deadline (`schedule.STALE_AFTER`)
ends it, for a cron occurrence the arrival of the next one does — the older
occurrence is then recorded as skipped (S2).

Everything that touches the disk runs through `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import re
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

#: A cron task whose runs keep failing to open pushes on the first failure of
#: a streak, then at most once per this while it lasts (the email poller's
#: pattern) — a failing every-minute task must not push every minute.
STREAK_RENOTIFY_AFTER = timedelta(days=1)

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
        return self.task.key_for(self.scheduled_for)


# Reads back the prefix run_prompt writes — aish's own text in the recorded
# task prompt, which is the only place the session log keeps the automation id.
_SCHEDULED_PREFIX = re.compile(r"^\[scheduled: ([a-z0-9][a-z0-9-]{0,63})\]")


def run_prompt(task: Task, fired_at: datetime, scheduled_for: datetime | None = None) -> str:
    """The prompt a run starts from. Both times are stated because a late fire
    is something the run can OBSERVE and should not have to infer. The sites
    and prefixes are restated because the trigger prompt is owner-authored
    text, and that is where the egress gate reads the hosts he named.
    `scheduled_for` is the occurrence; a one-shot's is its `at`."""
    occurrence = scheduled_for or task.one_shot_at()
    recurs = f" (recurring: `{task.cron}`)" if task.recurring else ""
    lines = [
        f"[scheduled: {task.id}] An automation the owner approved has fired.",
        f"Scheduled for {occurrence.isoformat(sep=' ', timespec='minutes')}{recurs}; "
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


def _parse_stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _task_of(key: str) -> str:
    return key.partition(":")[0]


@dataclass
class _State:
    """What the scheduler remembers beyond the ledger: the malfunctions it
    recorded, the keys it gave up on, and what it already pushed about.

    `cron` is per cron task id: the expr it was seen with, `since` (when that
    schedule began to exist — the file's mtime when first seen, never the
    free-text `created`) and `skipped_through` (the last occurrence already
    recorded as skipped). `streaks` is per cron task id: its open-failure
    streak, which is what its pushes are counted against."""

    records: list[dict[str, Any]] = field(default_factory=list)
    settled: dict[str, str] = field(default_factory=dict)
    notified: list[str] = field(default_factory=list)
    problems: list[dict[str, str]] = field(default_factory=list)
    cron: dict[str, dict[str, str]] = field(default_factory=dict)
    streaks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": self.records[-MAX_RECORDS:],
            "settled": self.settled,
            "notified": self.notified[-MAX_RECORDS:],
            "problems": self.problems,
            "cron": self.cron,
            "streaks": self.streaks,
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
        self._state_loading: asyncio.Future | None = None

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
        state = await self._ensure_state()
        await self._reload_if_changed()
        try:
            ledger = await asyncio.to_thread(self.ledger.read)
        except LedgerUnreadable as exc:
            await self._ledger_unreadable(str(exc))
            return
        fired = set(ledger) | self._fired_here
        for task in list(self.tasks):
            now = self.clock()
            if task.recurring:
                # A cron task never settles and never expires; `settled` is
                # one-shot keys only.
                await self._tick_cron(task, fired, now)
                continue
            if task.key in state.settled:
                continue
            status = schedule.next_due(task, fired, now)
            if status == EXPIRED:
                await self._expire(task, now)
            elif status == DUE:
                await self._fire(task, task.one_shot_at(), now)

    async def _tick_cron(self, task: Task, fired: set[str], now: datetime) -> None:
        since = self._since(task)
        verdict = schedule.due(task, fired, now, since=since)
        if verdict.occurrence is not None:
            self._forget_superseded(task, verdict.occurrence)
        if verdict.status != DUE or verdict.occurrence is None or since is None:
            return
        await self._record_skipped(task, verdict.occurrence, since, fired, now)
        await self._fire(task, verdict.occurrence, now)

    def _since(self, task: Task) -> datetime | None:
        assert self._state is not None
        entry = self._state.cron.get(task.id)
        if not entry or entry.get("cron") != task.cron:
            return None
        return _parse_stamp(entry.get("since"))

    def _forget_superseded(self, task: Task, latest: datetime) -> None:
        """Drop this task's in-memory retry and refusal state for every
        occurrence older than `latest`: once a successor has arrived, an older
        occurrence is never fired, so nothing will ever read those keys."""
        current = task.key_for(latest)
        for store in (self._retry_open_at, self._last_refusal):
            for key in [k for k in store if k != current and _task_of(k) == task.id]:
                del store[key]

    async def _record_skipped(
        self, task: Task, occurrence: datetime, since: datetime, fired: set[str],
        now: datetime,
    ) -> None:
        """Older occurrences that will never fire because `occurrence` is now
        the latest: recorded ONCE per range, never pushed — the run that fires
        instead, or the open-failure streak, is what reaches the owner."""
        assert self._state is not None
        entry = self._state.cron[task.id]
        floors = [
            when for when in (
                schedule.last_fired(task.id, fired),
                _parse_stamp(entry.get("skipped_through")),
            ) if when is not None
        ]
        skipped = schedule.skipped_before(
            task, occurrence, since=since, after=max(floors, default=None)
        )
        if skipped is None:
            return
        key = f"{task.id}:skipped:{_stamp(skipped.first)}/{_stamp(skipped.last)}"
        if not any(r.get("key") == key for r in self._state.records):
            self._state.records.append({
                "kind": "skipped", "task": task.id, "key": key,
                "from": _stamp(skipped.first), "through": _stamp(skipped.last),
                "count": skipped.count, "count_capped": skipped.count_capped,
                "latest": _stamp(occurrence), "observed_at": _stamp(now),
            })
        entry["skipped_through"] = _stamp(skipped.last)
        await self._save_state()

    # -- loading --------------------------------------------------------------

    async def _ensure_state(self) -> _State:
        # The resumer reports into this state at startup while the first tick
        # loads it; two independent loads would each hold a copy and the last
        # save would drop the other's record. So the load is shared.
        if self._state is None:
            if self._state_loading is None:
                self._state_loading = asyncio.ensure_future(
                    asyncio.to_thread(self._load_state)
                )
            loaded = await self._state_loading
            if self._state is None:
                self._state = loaded
        return self._state

    async def report_interrupted(self, session: str, prompt: str, log_size: int) -> None:
        """A schedule-origin run whose session aish-web found unfinished at
        start: it will never be resumed (#187), so it is a run that did not
        complete and must not end in silence. Recorded, and pushed once per
        interruption — the key carries the log's size, which the skip leaves
        untouched, so every rescan of the same interruption is the same key
        while a later, new interruption of that chat is a new one."""
        state = await self._ensure_state()
        match = _SCHEDULED_PREFIX.match(prompt or "")
        automation = match.group(1) if match else ""
        if automation:
            title = f"aish automation {automation} was interrupted"
            subject = f"The run of automation {automation}"
        else:
            title = "aish: a scheduled run was interrupted"
            subject = "The scheduled run"
        key = f"interrupted:{session}:{log_size}"
        if key in state.notified:
            return
        state.records.append({
            "kind": "interrupted", "session": session, "task": automation,
            "observed_at": _stamp(self.clock()),
        })
        await self._push_once(
            key,
            title,
            f"{subject} in chat {session} was interrupted: aish-web stopped "
            "while it was running, and scheduled runs are not resumed. The chat's "
            "transcript shows what it did and any approvals it was waiting on.",
        )

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
            cron=dict(data.get("cron") or {}),
            streaks=dict(data.get("streaks") or {}),
        )

    async def _reload_if_changed(self) -> None:
        directory = self.directory()
        current = await asyncio.to_thread(schedule.signature, directory)
        if current == self._signature:
            return
        now = self.clock()
        # The deny list is a file read too, so it is read inside the thread.
        loaded = await asyncio.to_thread(
            lambda: schedule.load_tasks(
                directory, deny_prefixes=list(self.deny_prefixes()), as_of=now
            )
        )
        self._signature = current
        self.tasks = loaded.tasks
        self.problems = loaded.problems
        assert self._state is not None
        self._state.problems = [{"path": p.path, "error": p.error} for p in loaded.problems]
        mtimes = {name: mtime for name, mtime, _size in current}
        self._note_cron_schedules(mtimes, now)
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

    def _note_cron_schedules(self, mtimes: dict[str, int], now: datetime) -> None:
        """When each cron schedule began to exist, kept once per (id, expr).
        It is the file's mtime when first seen — the card's write, or the
        owner's edit that set this expr or re-enabled it — capped at now, so a
        task approved between two ticks still owns the occurrence in between.
        A disabled or deleted task forgets it, so re-enabling never fires an
        occurrence that passed while it was off."""
        assert self._state is not None
        live = {t.id: t for t in self.tasks if t.recurring and t.enabled}
        for task_id in [i for i in self._state.cron if i not in live]:
            del self._state.cron[task_id]
        for task_id in [i for i in self._state.streaks if i not in live]:
            del self._state.streaks[task_id]
        for task_id, task in live.items():
            entry = self._state.cron.get(task_id)
            if entry and entry.get("cron") == task.cron:
                continue
            mtime_ns = mtimes.get(f"{task_id}.json")
            began = datetime.fromtimestamp(mtime_ns / 1e9) if mtime_ns else now
            self._state.cron[task_id] = {"cron": task.cron, "since": _stamp(min(began, now))}

    # -- outcomes -------------------------------------------------------------

    async def _fire(self, task: Task, occurrence: datetime, now: datetime) -> None:
        key = task.key_for(occurrence)
        retry_at = self._retry_open_at.get(key)
        if retry_at is not None and now < retry_at:
            return
        request = FireRequest(task, run_prompt(task, now, occurrence), occurrence, now)
        outcome = await self.fire(request)
        if outcome.opened:
            self._retry_open_at.pop(key, None)
            self._last_refusal.pop(key, None)
            await self._mark_fired(task, occurrence, outcome.session, now)
            if task.recurring:
                await self._end_streak(task)
            return
        self._last_refusal[key] = outcome.error or outcome.status
        if outcome.status != OPEN_FAILED:
            return  # rate/capacity: nothing marked, the next tick asks again
        self._retry_open_at[key] = now + OPEN_RETRY_AFTER
        await self._record(task, "open_failed", now, occurrence, error=outcome.error)
        if task.recurring:
            await self._streak_failed(task, occurrence, now, outcome.error)
            return
        deadline = task.deadline
        assert deadline is not None  # a one-shot always has one
        await self._push_once(
            f"{key}:open_failed",
            f"aish automation {task.id} did not start",
            f"automation {task.id} did not start: {outcome.error}. aish will try again "
            f"until {deadline.strftime('%H:%M %a %Y-%m-%d')}.",
        )

    async def _streak_failed(
        self, task: Task, occurrence: datetime, now: datetime, error: str
    ) -> None:
        """A cron task's open failure counts against its STREAK, never its
        occurrence: pushed on the first failure, then at most once per
        `STREAK_RENOTIFY_AFTER` while it lasts. Kept out of `notified`, which a
        per-occurrence key would flood and evict unrelated dedupe entries from."""
        assert self._state is not None
        streak = self._state.streaks.setdefault(
            task.id, {"started": _stamp(now), "failures": 0}
        )
        streak["failures"] = int(streak.get("failures", 0)) + 1
        pushed_at = _parse_stamp(streak.get("pushed_at"))
        if pushed_at is not None and now - pushed_at < STREAK_RENOTIFY_AFTER:
            await self._save_state()
            return
        # Stamped on the ATTEMPT, as the email poller does: an unconfigured
        # notifier costs one no-op a day, not one per failure.
        streak["pushed_at"] = _stamp(now)
        await self._save_state()
        try:
            upcoming = schedule.cron_next(task.cron, occurrence).strftime("%H:%M %a %Y-%m-%d")
            until = f"until its next scheduled time, {upcoming}"
        except schedule.NoOccurrence:
            until = "for now; aish found no next scheduled time"
        failures = streak["failures"]
        history = (
            f" Runs of it have failed to start {failures} times in a row since "
            f"{streak['started'].replace('T', ' ')}." if failures > 1 else ""
        )
        await self._notify(
            f"aish automation {task.id} did not start",
            f"automation {task.id} did not start: {error}. aish will keep trying this "
            f"run {until}.{history}",
        )

    async def _end_streak(self, task: Task) -> None:
        assert self._state is not None
        if self._state.streaks.pop(task.id, None) is not None:
            await self._save_state()

    async def _mark_fired(
        self, task: Task, occurrence: datetime, session: str, now: datetime
    ) -> None:
        key = task.key_for(occurrence)
        self._fired_here.add(key)
        # One-shot keys whose task file exists are never pruned: such a key
        # pruned would read as never-fired, then EXPIRED, then a false push.
        protected = {t.key for t in self.tasks if not t.recurring}
        here = dict.fromkeys(self._fired_here)
        schedule.prune_ledger(here, task, protected=protected)
        self._fired_here = set(here)
        entry = {"fired_at": _stamp(now), "scheduled_for": _stamp(occurrence),
                 "session": session}

        def write() -> None:
            # Pruned inside the one write, so the ledger keeps one writer.
            data = self.ledger.read()
            data[key] = entry
            schedule.prune_ledger(data, task, protected=protected)
            self.ledger.write(data)

        try:
            await asyncio.to_thread(write)
        except (OSError, LedgerUnreadable) as exc:
            # Still fired, and this process will not fire it again; a restart
            # before the ledger is writable would. Said where it can be seen.
            print(f"[scheduler] {key} fired as {session} but the ledger "
                  f"could not be written: {exc}", file=sys.stderr)
            await self._record(task, "ledger_write_failed", now, occurrence,
                               error=str(exc), session=session)

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
        await self._record(task, "expired", now, task.one_shot_at(), error=observed)
        # Its own key: the terminal sentence must reach him even when a
        # "did not start, will try again" push already went out for this run.
        await self._push_once(
            f"{task.key}:expired",
            f"aish automation {task.id} did not run",
            f"automation {task.id} did not run: it was scheduled for "
            f"{task.one_shot_at().strftime('%H:%M %a %Y-%m-%d')} and had not started "
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

    async def _record(
        self, task: Task, kind: str, now: datetime, occurrence: datetime, **extra: str
    ) -> None:
        assert self._state is not None
        self._state.records.append({
            "kind": kind, "task": task.id, "key": task.key_for(occurrence),
            "scheduled_for": _stamp(occurrence), "observed_at": _stamp(now), **extra,
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
