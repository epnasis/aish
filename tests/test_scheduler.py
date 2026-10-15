"""The scheduler loop (epic #142 W2, S1): fake clock, fake seam, no sleeps, no server.

The seam is the server's `launch_triggered` in production; here it is a script
of outcomes, so each interleaving the design names is driven explicitly.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

from aish import schedule
from aish.scheduler import (
    AT_CAPACITY,
    DEDUPED,
    OPEN_FAILED,
    OPEN_RETRY_AFTER,
    RATE_LIMITED,
    STARTED,
    Scheduler,
    TriggerOutcome,
)

AT = datetime(2026, 10, 15, 22, 5)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Seam:
    """The trigger seam as a script. Each call pops the next outcome (the
    last one repeats) and records what it was asked — including whether the
    ledger already held the key at the moment of the call."""

    def __init__(self, ledger, *outcomes: TriggerOutcome) -> None:
        self.ledger = ledger
        self.outcomes = list(outcomes) or [TriggerOutcome(STARTED, session="s-1")]
        self.requests: list = []
        self.key_marked_at_call: list[bool] = []

    async def __call__(self, request):
        self.requests.append(request)
        self.key_marked_at_call.append(request.dedup_key in self.ledger.read())
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]


def write_task(directory, task_id="eon-invoices", at=AT, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    body = {"id": task_id, "prompt": "Download the October E.ON invoices.",
            "schedule": {"at": at.isoformat()}, **extra}
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def build(tmp_path, clock, *outcomes, notify=None):
    pushes: list[tuple[str, str]] = []
    state_dir = tmp_path / "state"
    seam = Seam(schedule.fired_ledger(state_dir), *outcomes)
    sched = Scheduler(
        state_dir=state_dir,
        fire=seam,
        notify=notify or (lambda title, body: pushes.append((title, body))),
        directory=lambda: tmp_path / "automations",
        clock=clock,
    )
    return sched, seam, pushes


def tick(sched):
    asyncio.run(sched.tick())


def ledger(tmp_path):
    return schedule.fired_ledger(tmp_path / "state").read()


def records(tmp_path):
    return schedule.scheduler_state(tmp_path / "state").read().get("records", [])


class TestSchedulerFires:
    def test_nothing_fires_before_the_time(self, tmp_path):
        write_task(tmp_path / "automations")
        sched, seam, _ = build(tmp_path, Clock(AT - timedelta(minutes=1)))
        tick(sched)
        assert seam.requests == []

    def test_it_fires_once_when_due(self, tmp_path):
        write_task(tmp_path / "automations")
        clock = Clock(AT + timedelta(seconds=10))
        sched, seam, pushes = build(tmp_path, clock)
        tick(sched)
        tick(sched)
        assert len(seam.requests) == 1
        assert seam.requests[0].dedup_key == "eon-invoices:2026-10-15T22:05:00"
        assert ledger(tmp_path)["eon-invoices:2026-10-15T22:05:00"]["session"] == "s-1"
        assert pushes == []  # a run that started is not a malfunction

    def test_a_late_fire_within_the_bound_states_both_times(self, tmp_path):
        write_task(tmp_path / "automations")
        sched, seam, _ = build(tmp_path, Clock(AT + timedelta(hours=3)))
        tick(sched)
        prompt = seam.requests[0].prompt
        assert prompt.startswith("[scheduled: eon-invoices]")
        assert "Scheduled for 2026-10-15 22:05" in prompt
        assert "fired at 2026-10-16 01:05:00" in prompt
        assert prompt.endswith("Download the October E.ON invoices.")

    def test_the_prompt_restates_the_approved_grants(self, tmp_path):
        write_task(tmp_path / "automations", allow=["git pull"], sites=["eon.pl"])
        sched, seam, _ = build(tmp_path, Clock(AT))
        tick(sched)
        assert "Commands approved for this run: git pull." in seam.requests[0].prompt
        assert "Sites you may act on as the owner: eon.pl." in seam.requests[0].prompt

    def test_the_ledger_is_written_after_the_open_never_before(self, tmp_path):
        write_task(tmp_path / "automations")
        sched, seam, _ = build(tmp_path, Clock(AT))
        tick(sched)
        assert seam.key_marked_at_call == [False]
        assert "eon-invoices:2026-10-15T22:05:00" in ledger(tmp_path)

    def test_a_dedup_answer_counts_as_opened(self, tmp_path):
        # The in-memory dedup caught a repeat: a session exists for this key.
        write_task(tmp_path / "automations")
        sched, _, _ = build(tmp_path, Clock(AT), TriggerOutcome(DEDUPED, session="s-0"))
        tick(sched)
        assert ledger(tmp_path)["eon-invoices:2026-10-15T22:05:00"]["session"] == "s-0"

    def test_an_overlapping_tick_cannot_fire_the_same_key_twice(self, tmp_path):
        write_task(tmp_path / "automations")
        sched, _, _ = build(tmp_path, Clock(AT))
        calls = []

        async def slow_seam(request):
            calls.append(request.dedup_key)
            await asyncio.sleep(0.05)  # the open, still in flight
            return TriggerOutcome(STARTED, session="s-1")

        sched.fire = slow_seam

        async def both():
            await asyncio.gather(sched.tick(), sched.tick())

        asyncio.run(both())
        assert calls == ["eon-invoices:2026-10-15T22:05:00"]

    def test_a_disabled_task_never_fires(self, tmp_path):
        write_task(tmp_path / "automations", enabled=False)
        sched, seam, pushes = build(tmp_path, Clock(AT))
        tick(sched)
        assert seam.requests == [] and pushes == []


class TestSchedulerRefusals:
    def test_a_capacity_refusal_marks_nothing_and_fires_when_the_cap_frees(self, tmp_path):
        """The interleaving the design names: refused, refused, then admitted."""
        write_task(tmp_path / "automations")
        clock = Clock(AT)
        sched, seam, pushes = build(
            tmp_path, clock,
            TriggerOutcome(AT_CAPACITY, error="too many concurrent triggered sessions"),
            TriggerOutcome(RATE_LIMITED, error="rate limit exceeded for origin 'schedule'"),
            TriggerOutcome(STARTED, session="s-1"),
        )
        tick(sched)
        assert ledger(tmp_path) == {}
        clock.now += timedelta(seconds=30)
        tick(sched)
        assert ledger(tmp_path) == {}
        clock.now += timedelta(seconds=30)
        tick(sched)
        assert len(seam.requests) == 3
        assert ledger(tmp_path)["eon-invoices:2026-10-15T22:05:00"]["session"] == "s-1"
        assert pushes == [] and records(tmp_path) == []  # a refusal is not a fire

    def test_refused_until_the_deadline_is_recorded_and_pushed_once(self, tmp_path):
        write_task(tmp_path / "automations")
        clock = Clock(AT)
        refused = TriggerOutcome(AT_CAPACITY, error="too many concurrent triggered sessions")
        sched, seam, pushes = build(tmp_path, clock, refused)
        tick(sched)
        clock.now = AT + schedule.STALE_AFTER + timedelta(seconds=1)
        tick(sched)
        tick(sched)
        assert len(seam.requests) == 1  # never fired past the bound
        assert ledger(tmp_path) == {}
        [record] = records(tmp_path)
        assert record["kind"] == "expired"
        assert "too many concurrent triggered sessions" in record["error"]
        assert len(pushes) == 1
        assert "did not run" in pushes[0][1]

    def test_an_open_failure_never_poisons_the_key(self, tmp_path):
        write_task(tmp_path / "automations", model="nope:missing")
        clock = Clock(AT)
        sched, seam, pushes = build(
            tmp_path, clock,
            TriggerOutcome(OPEN_FAILED, error="model unavailable: no such model"),
            TriggerOutcome(OPEN_FAILED, error="model unavailable: no such model"),
            TriggerOutcome(STARTED, session="s-1"),
        )
        tick(sched)
        assert ledger(tmp_path) == {}
        [record] = records(tmp_path)
        assert record["kind"] == "open_failed"
        assert record["error"] == "model unavailable: no such model"
        assert len(pushes) == 1
        assert "did not start: model unavailable: no such model" in pushes[0][1]
        # Within the retry window nothing is asked again.
        clock.now += OPEN_RETRY_AFTER - timedelta(seconds=1)
        tick(sched)
        assert len(seam.requests) == 1
        clock.now += timedelta(seconds=2)
        tick(sched)  # second failure: recorded, NOT pushed again
        assert len(pushes) == 1
        clock.now += OPEN_RETRY_AFTER
        tick(sched)
        assert "eon-invoices:2026-10-15T22:05:00" in ledger(tmp_path)


class TestSchedulerMalfunctions:
    def test_an_expired_task_is_recorded_pushed_once_and_never_fired(self, tmp_path):
        write_task(tmp_path / "automations")
        clock = Clock(AT + timedelta(hours=25))
        sched, seam, pushes = build(tmp_path, clock)
        tick(sched)
        tick(sched)
        assert seam.requests == []
        [record] = records(tmp_path)
        assert record["kind"] == "expired"
        assert record["error"] == "aish did not observe an attempt to start it"
        assert len(pushes) == 1
        # …and a restart does not push it again: the settlement is durable.
        again, seam2, pushes2 = build(tmp_path, clock)
        tick(again)
        assert seam2.requests == [] and pushes2 == []

    def test_an_invalid_file_is_skipped_loudly_and_the_rest_still_fire(self, tmp_path):
        write_task(tmp_path / "automations")
        (tmp_path / "automations" / "broken.json").write_text("{nope", encoding="utf-8")
        sched, seam, pushes = build(tmp_path, Clock(AT))
        tick(sched)
        assert [r.task.id for r in seam.requests] == ["eon-invoices"]
        assert [p.path.rsplit("/", 1)[-1] for p in sched.problems] == ["broken.json"]
        state = schedule.scheduler_state(tmp_path / "state").read()
        assert state["problems"][0]["path"].endswith("broken.json")
        assert len(pushes) == 1 and "broken.json" in pushes[0][1]
        tick(sched)
        assert len(pushes) == 1  # the same version of the file pushes once

    def test_an_unreadable_ledger_fires_nothing_and_is_never_overwritten(self, tmp_path):
        write_task(tmp_path / "automations")
        path = schedule.fired_ledger(tmp_path / "state").path
        path.parent.mkdir(parents=True)
        path.write_text("{corrupt", encoding="utf-8")
        sched, seam, pushes = build(tmp_path, Clock(AT))
        tick(sched)
        tick(sched)
        assert seam.requests == []
        assert path.read_text(encoding="utf-8") == "{corrupt"
        assert len(pushes) == 1 and "firing none" in pushes[0][1]

    def test_a_notifier_that_raises_never_stops_the_loop(self, tmp_path):
        write_task(tmp_path / "automations")

        def boom(_title, _body):
            raise RuntimeError("push service down")

        sched, seam, _ = build(tmp_path, Clock(AT + timedelta(hours=25)), notify=boom)
        tick(sched)
        assert records(tmp_path)[0]["kind"] == "expired"

    def test_run_survives_a_failing_tick(self, tmp_path, monkeypatch):
        sched, _, _ = build(tmp_path, Clock(AT))
        ticks = []

        async def bad_tick():
            ticks.append(1)
            raise RuntimeError("disk vanished")

        async def stop_after_two(_s):
            if len(ticks) >= 2:
                raise asyncio.CancelledError

        monkeypatch.setattr(sched, "tick", bad_tick)
        sched.sleep = stop_after_two
        try:
            asyncio.run(sched.run())
        except asyncio.CancelledError:
            pass
        assert len(ticks) == 2


class TestSchedulerRestarts:
    def test_restart_matrix(self, tmp_path):
        """Fired before the restart: not again. Due after it: fires."""
        write_task(tmp_path / "automations", "first", AT)
        write_task(tmp_path / "automations", "second", AT + timedelta(hours=2))
        clock = Clock(AT + timedelta(minutes=1))
        before, seam1, _ = build(tmp_path, clock)
        tick(before)
        assert [r.task.id for r in seam1.requests] == ["first"]

        clock.now = AT + timedelta(hours=2, minutes=1)  # the daemon was restarted
        after, seam2, _ = build(tmp_path, clock)
        tick(after)
        assert [r.task.id for r in seam2.requests] == ["second"]

    def test_a_hand_edit_of_at_rearms_an_already_fired_task(self, tmp_path):
        path = write_task(tmp_path / "automations")
        clock = Clock(AT)
        sched, seam, _ = build(tmp_path, clock)
        tick(sched)
        write_task(tmp_path / "automations", at=AT + timedelta(days=1))
        st = path.stat()
        # Make sure the per-file signal moves even on a coarse-mtime filesystem.
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        clock.now = AT + timedelta(days=1)
        tick(sched)
        assert [r.dedup_key for r in seam.requests] == [
            "eon-invoices:2026-10-15T22:05:00", "eon-invoices:2026-10-16T22:05:00",
        ]

    def test_code_never_writes_the_automations_folder(self, tmp_path):
        path = write_task(tmp_path / "automations")
        before = path.read_bytes()
        sched, _, _ = build(tmp_path, Clock(AT))
        tick(sched)
        assert path.read_bytes() == before
        assert sorted(p.name for p in (tmp_path / "automations").iterdir()) == [
            "eon-invoices.json"
        ]
