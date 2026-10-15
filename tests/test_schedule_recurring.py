"""Recurring automations (epic #142 W2, S2): due-ness, catch-up, the ledger, pushes, DST.

Fake clock, scripted trigger seam, no sleeps, no server — the S1 harness in
`test_scheduler.py`, reused. Every datetime is naive local wall-clock and
constructed directly, so the DST tables need no tz database.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

import pytest
from test_scheduler import Clock, build, ledger, records, tick

from aish import schedule
from aish.schedule import (
    DISABLED,
    DUE,
    FIRED,
    LEDGER_KEEP_PER_CRON_TASK,
    PENDING,
    InvalidTask,
    due,
    next_due,
    prune_ledger,
    validate_task,
)
from aish.scheduler import AT_CAPACITY, OPEN_FAILED, RATE_LIMITED, STARTED, TriggerOutcome

D = datetime
DAILY = "0 9 * * *"
#: Just after the 2026-10-14 slot, so a run on the 15th has no older misses.
BEGAN = D(2026, 10, 14, 10, 0)


def cron_raw(expr=DAILY, task_id="daily", **over):
    base = {"id": task_id, "prompt": "Summarise new invoices.", "schedule": {"cron": expr}}
    base.update(over)
    return base


def write_cron(tmp_path, expr=DAILY, task_id="daily", began=BEGAN, **extra):
    """A cron task file whose mtime — when the schedule began to exist — is `began`."""
    directory = tmp_path / "automations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps(cron_raw(expr, task_id, **extra)), encoding="utf-8")
    stamp = began.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def seed_ledger(tmp_path, *keys):
    store = schedule.fired_ledger(tmp_path / "state")
    data = store.read()
    for key in keys:
        data[key] = {"fired_at": key.partition(":")[2], "session": "s-old"}
    store.write(data)


def fired_keys(seam):
    return [r.dedup_key for r in seam.requests]


def skips(tmp_path):
    return [r for r in records(tmp_path) if r["kind"] == "skipped"]


class TestRecurringValidation:
    def test_a_cron_task_validates_and_has_no_single_time(self):
        task = validate_task(cron_raw("0  9 * * 1-5"), now=D(2026, 10, 15, 18, 30))
        assert task.recurring and task.cron == "0 9 * * 1-5"
        assert task.at is None and task.deadline is None
        assert task.schedule == {"cron": "0 9 * * 1-5"}
        assert task.key_for(D(2026, 10, 16, 9, 0)) == "daily:2026-10-16T09:00:00"
        with pytest.raises(ValueError):
            task.key  # noqa: B018 — a cron task has no single key

    def test_it_round_trips_through_its_file(self, tmp_path):
        task = validate_task(cron_raw(), now=D(2026, 10, 15))
        (tmp_path / "daily.json").write_text(task.render(), encoding="utf-8")
        loaded = schedule.load_tasks(tmp_path, as_of=D(2026, 10, 15))
        assert loaded.problems == [] and loaded.tasks == [task]

    @pytest.mark.parametrize(
        "sched",
        [
            {"at": "2026-10-16T09:00", "cron": DAILY},
            {},
            {"cron": ["0", "9"]},
            {"cron": "@daily"},
            {"cron": "0 9 * * MON"},
        ],
    )
    def test_exactly_one_supported_shape(self, sched):
        with pytest.raises(InvalidTask):
            validate_task(cron_raw(schedule=sched), now=D(2026, 10, 15))

    def test_an_impossible_expr_is_refused_at_creation(self):
        with pytest.raises(InvalidTask, match="no occurrence"):
            validate_task(cron_raw("0 0 30 2 *"), now=D(2026, 10, 15))

    def test_an_impossible_expr_is_a_load_problem_and_never_hides_others(self, tmp_path):
        (tmp_path / "feb30.json").write_text(
            json.dumps(cron_raw("0 0 30 2 *", "feb30")), encoding="utf-8"
        )
        (tmp_path / "daily.json").write_text(json.dumps(cron_raw()), encoding="utf-8")
        loaded = schedule.load_tasks(tmp_path, as_of=D(2026, 10, 15))
        assert [t.id for t in loaded.tasks] == ["daily"]
        [problem] = loaded.problems
        assert problem.path.endswith("feb30.json") and "no occurrence" in problem.error

    def test_an_impossible_file_is_pushed_once_per_version(self, tmp_path):
        write_cron(tmp_path, "0 0 30 2 *", "feb30")
        sched, seam, pushes = build(tmp_path, Clock(D(2026, 10, 15, 9, 0)))
        tick(sched)
        tick(sched)
        assert seam.requests == []
        assert [t for t, _ in pushes] == ["aish automation file feb30.json was skipped"]


class TestRecurringDue:
    since = D(2026, 10, 1)

    def task(self, expr=DAILY, **over):
        return validate_task(cron_raw(expr, **over), now=D(2026, 10, 1))

    def test_the_latest_occurrence_is_due(self):
        verdict = due(self.task(), set(), D(2026, 10, 15, 9, 0, 10), since=self.since)
        assert verdict == schedule.Due(DUE, D(2026, 10, 15, 9, 0))

    def test_only_the_latest_miss_is_ever_due(self):
        # Down for a week: yesterday and before are never the answer.
        verdict = due(self.task(), set(), D(2026, 10, 15, 13, 0), since=self.since)
        assert verdict.occurrence == D(2026, 10, 15, 9, 0)

    def test_without_since_nothing_past_is_due(self):
        assert next_due(self.task(), set(), D(2026, 10, 15, 9, 1)) == PENDING

    def test_an_occurrence_before_the_schedule_existed_is_not_a_miss(self):
        # Approved at 10:00: today's 09:00 never existed for it.
        status = next_due(self.task(), set(), D(2026, 10, 15, 10, 0, 30),
                          since=D(2026, 10, 15, 10, 0))
        assert status == PENDING

    def test_fired_and_disabled(self):
        task = self.task()
        fired = {task.key_for(D(2026, 10, 15, 9, 0))}
        assert next_due(task, fired, D(2026, 10, 15, 9, 5), since=self.since) == FIRED
        off = self.task(enabled=False)
        assert next_due(off, set(), D(2026, 10, 15, 9, 5), since=self.since) == DISABLED

    def test_a_cron_task_never_expires(self):
        task = self.task()
        late = D(2027, 3, 1, 9, 30)  # months unfired
        assert next_due(task, set(), late, since=self.since) == DUE

    def test_an_occurrence_at_or_before_the_last_fired_one_is_never_due(self):
        # The clock stepped back past a fired occurrence whose key was pruned.
        task = self.task("*/5 * * * *")
        fired = {task.key_for(D(2026, 10, 25, 2, 55))}
        assert next_due(task, fired, D(2026, 10, 25, 2, 0, 10), since=self.since) == PENDING


class TestRecurringFires:
    def test_on_time_once_per_occurrence(self, tmp_path):
        write_cron(tmp_path)
        clock = Clock(D(2026, 10, 15, 9, 0, 10))
        sched, seam, pushes = build(tmp_path, clock)
        tick(sched)
        tick(sched)
        clock.now = D(2026, 10, 16, 9, 0, 10)
        tick(sched)
        assert fired_keys(seam) == ["daily:2026-10-15T09:00:00", "daily:2026-10-16T09:00:00"]
        prompt = seam.requests[0].prompt
        assert "Scheduled for 2026-10-15 09:00 (recurring: `0 9 * * *`)" in prompt
        assert seam.requests[0].scheduled_for == D(2026, 10, 15, 9, 0)
        assert set(ledger(tmp_path)) == {"daily:2026-10-15T09:00:00",
                                         "daily:2026-10-16T09:00:00"}
        assert pushes == [] and skips(tmp_path) == []

    def test_a_task_approved_after_todays_slot_waits_for_tomorrow(self, tmp_path):
        write_cron(tmp_path, began=D(2026, 10, 15, 10, 0))
        clock = Clock(D(2026, 10, 15, 10, 0, 30))
        sched, seam, _ = build(tmp_path, clock)
        tick(sched)
        assert seam.requests == []
        clock.now = D(2026, 10, 16, 9, 0, 5)
        tick(sched)
        assert fired_keys(seam) == ["daily:2026-10-16T09:00:00"]

    def test_a_task_approved_between_ticks_owns_the_slot_in_between(self, tmp_path):
        # Written 08:59:59, first seen 09:00:20: its mtime, not the tick, is when it began.
        write_cron(tmp_path, began=D(2026, 10, 15, 8, 59, 59))
        sched, seam, _ = build(tmp_path, Clock(D(2026, 10, 15, 9, 0, 20)))
        tick(sched)
        assert fired_keys(seam) == ["daily:2026-10-15T09:00:00"]

    def test_down_over_one_slot_fires_it_late_and_says_both_times(self, tmp_path):
        write_cron(tmp_path)
        seed_ledger(tmp_path, "daily:2026-10-14T09:00:00")
        sched, seam, _ = build(tmp_path, Clock(D(2026, 10, 15, 13, 0)))
        tick(sched)
        assert fired_keys(seam) == ["daily:2026-10-15T09:00:00"]
        prompt = seam.requests[0].prompt
        assert "Scheduled for 2026-10-15 09:00" in prompt
        assert "fired at 2026-10-15 13:00:00" in prompt
        assert skips(tmp_path) == []

    def test_down_over_many_slots_fires_the_latest_and_records_one_skip(self, tmp_path):
        write_cron(tmp_path, began=D(2026, 10, 1, 12, 0))
        seed_ledger(tmp_path, "daily:2026-10-11T09:00:00")
        clock = Clock(D(2026, 10, 15, 13, 0))
        sched, seam, pushes = build(tmp_path, clock)
        tick(sched)
        assert fired_keys(seam) == ["daily:2026-10-15T09:00:00"]
        [skip] = skips(tmp_path)
        assert (skip["from"], skip["through"], skip["count"]) == (
            "2026-10-12T09:00:00", "2026-10-14T09:00:00", 3)
        assert skip["latest"] == "2026-10-15T09:00:00"
        assert pushes == []  # the run that fired instead is the evidence
        # A restart re-derives nothing new.
        again, seam2, _ = build(tmp_path, clock)
        tick(again)
        assert seam2.requests == [] and len(skips(tmp_path)) == 1

    def test_the_skip_record_is_idempotent_across_restarts_while_refused(self, tmp_path):
        write_cron(tmp_path, began=D(2026, 10, 1, 12, 0))
        seed_ledger(tmp_path, "daily:2026-10-11T09:00:00")
        clock = Clock(D(2026, 10, 15, 13, 0))
        for _ in range(3):
            sched, _, _ = build(tmp_path, clock, TriggerOutcome(AT_CAPACITY, error="cap"))
            tick(sched)
            tick(sched)
        assert len(skips(tmp_path)) == 1

    def test_a_refusal_that_outlasts_its_occurrence_is_skipped(self, tmp_path):
        write_cron(tmp_path)
        clock = Clock(D(2026, 10, 15, 9, 0, 5))
        sched, seam, pushes = build(
            tmp_path, clock,
            TriggerOutcome(RATE_LIMITED, error="rate"),
            TriggerOutcome(AT_CAPACITY, error="cap"),
            TriggerOutcome(STARTED, session="s-2"),
        )
        tick(sched)
        clock.now = D(2026, 10, 15, 18, 0)
        tick(sched)
        assert "daily:2026-10-15T09:00:00" in sched._last_refusal
        clock.now = D(2026, 10, 16, 9, 0, 5)
        tick(sched)
        assert fired_keys(seam) == ["daily:2026-10-15T09:00:00"] * 2 + [
            "daily:2026-10-16T09:00:00"]
        [skip] = skips(tmp_path)
        assert (skip["from"], skip["through"], skip["count"]) == (
            "2026-10-15T09:00:00", "2026-10-15T09:00:00", 1)
        # The superseded occurrence's in-memory state is gone.
        assert not any(k.startswith("daily:2026-10-15") for k in sched._last_refusal)
        assert pushes == []

    def test_a_cron_task_never_settles_or_expires(self, tmp_path):
        write_cron(tmp_path)
        clock = Clock(D(2026, 10, 15, 9, 0, 5))
        sched, _, pushes = build(tmp_path, clock, TriggerOutcome(AT_CAPACITY, error="cap"))
        for day in range(4):
            clock.now = D(2026, 10, 15, 9, 0, 5) + timedelta(days=day, hours=12)
            tick(sched)
        state = schedule.scheduler_state(tmp_path / "state").read()
        assert state["settled"] == {}
        assert not any(r["kind"] == "expired" for r in records(tmp_path))
        assert pushes == []

    def test_editing_the_expr_re_arms_it_and_never_back_fires(self, tmp_path):
        write_cron(tmp_path)
        clock = Clock(D(2026, 10, 15, 9, 0, 5))
        sched, seam, _ = build(tmp_path, clock)
        tick(sched)
        # At 10:00 he moves it to 08:00 (already past today) …
        write_cron(tmp_path, "0 8 * * *", began=D(2026, 10, 15, 10, 0))
        clock.now = D(2026, 10, 15, 10, 0, 30)
        tick(sched)
        # … and later to 10:30, which is still ahead today.
        write_cron(tmp_path, "30 10 * * *", began=D(2026, 10, 15, 10, 15))
        clock.now = D(2026, 10, 15, 10, 30, 5)
        tick(sched)
        assert fired_keys(seam) == ["daily:2026-10-15T09:00:00", "daily:2026-10-15T10:30:00"]

    def test_re_enabling_never_fires_what_passed_while_it_was_off(self, tmp_path):
        write_cron(tmp_path, enabled=False)
        clock = Clock(D(2026, 10, 15, 9, 0, 5))
        sched, seam, _ = build(tmp_path, clock)
        tick(sched)
        write_cron(tmp_path, began=D(2026, 10, 15, 12, 0))  # enabled again at noon
        clock.now = D(2026, 10, 15, 12, 0, 30)
        tick(sched)
        assert seam.requests == []
        clock.now = D(2026, 10, 16, 9, 0, 5)
        tick(sched)
        assert fired_keys(seam) == ["daily:2026-10-16T09:00:00"]


class TestRecurringLedger:
    def test_it_keeps_the_last_k_occurrences_per_cron_task(self, tmp_path):
        write_cron(tmp_path)
        write_task_once = tmp_path / "automations" / "once.json"
        write_task_once.write_text(json.dumps({
            "id": "once", "prompt": "x", "schedule": {"at": "2026-10-01T08:00"},
        }), encoding="utf-8")
        seed_ledger(tmp_path, "once:2026-10-01T08:00:00")
        clock = Clock(D(2026, 10, 10, 9, 0, 5))
        sched, _, _ = build(tmp_path, clock)
        for day in range(7):
            clock.now = D(2026, 10, 10, 9, 0, 5) + timedelta(days=day)
            tick(sched)
        keys = set(ledger(tmp_path))
        assert {k for k in keys if k.startswith("daily:")} == {
            f"daily:2026-10-{d}T09:00:00" for d in (13, 14, 15, 16)}
        assert "once:2026-10-01T08:00:00" in keys
        assert len([k for k in sched._fired_here if k.startswith("daily:")]) == (
            LEDGER_KEEP_PER_CRON_TASK)

    def test_a_live_one_shot_key_is_never_pruned(self):
        # Pin the false-expiry bug. A one-shot whose key were pruned would read
        # as never-fired, then EXPIRED, then push "did not run" about a run
        # that ran.
        once = validate_task({"id": "daily", "prompt": "x",
                              "schedule": {"at": "2026-10-01T08:00"}})
        cron = validate_task(cron_raw(), now=D(2026, 10, 1))
        data = {once.key: {}} | {
            cron.key_for(D(2026, 10, d, 9, 0)): {} for d in range(10, 15)
        }
        late = D(2026, 10, 15)
        assert schedule.next_due(once, set(data), late) == FIRED
        unprotected = dict(data)
        prune_ledger(unprotected, cron, protected=())
        assert once.key not in unprotected
        assert schedule.next_due(once, set(unprotected), late) == schedule.EXPIRED  # the bug
        dropped = prune_ledger(data, cron, protected={once.key})
        assert once.key not in dropped and once.key in data
        assert schedule.next_due(once, set(data), late) == FIRED

    def test_a_one_shot_task_prunes_nothing(self):
        once = validate_task({"id": "x", "prompt": "x", "schedule": {"at": "2026-10-01T08:00"}})
        data = {f"x:2026-10-0{d}T08:00:00": {} for d in range(1, 9)}
        assert prune_ledger(data, once, protected=()) == [] and len(data) == 8

    def test_pruning_matches_the_exact_id(self):
        cron = validate_task(cron_raw(task_id="eon"), now=D(2026, 10, 1))
        data = {f"eon-invoices:2026-10-0{d}T09:00:00": {} for d in range(1, 9)}
        data |= {f"eon:2026-10-0{d}T09:00:00": {} for d in range(1, 9)}
        prune_ledger(data, cron, protected=())
        assert len([k for k in data if k.startswith("eon-invoices:")]) == 8
        assert len([k for k in data if k.startswith("eon:")]) == LEDGER_KEEP_PER_CRON_TASK


class TestRecurringStreakPushes:
    def test_first_pushes_same_day_does_not_next_day_does_success_resets(self, tmp_path):
        write_cron(tmp_path)
        fail = TriggerOutcome(OPEN_FAILED, error="model unavailable: no such model")
        clock = Clock(D(2026, 10, 15, 9, 0, 10))
        sched, seam, pushes = build(tmp_path, clock, fail)
        tick(sched)
        assert len(pushes) == 1
        title, body = pushes[0]
        assert title == "aish automation daily did not start"
        assert "until its next scheduled time, 09:00 Fri 2026-10-16" in body
        assert "will try again until" not in body  # a cron run has no deadline
        clock.now = D(2026, 10, 15, 9, 6)  # the 5-minute retry, still failing
        tick(sched)
        assert len(seam.requests) == 2 and len(pushes) == 1
        clock.now = D(2026, 10, 16, 9, 0, 10)
        tick(sched)
        assert len(pushes) == 2
        assert "failed to start 3 times in a row since 2026-10-15 09:00:10" in pushes[1][1]
        seam.outcomes = [TriggerOutcome(STARTED, session="s-ok")]
        clock.now = D(2026, 10, 17, 9, 0, 10)
        tick(sched)
        assert len(pushes) == 2
        state = schedule.scheduler_state(tmp_path / "state").read()
        assert state["streaks"] == {}
        seam.outcomes = [fail]
        clock.now = D(2026, 10, 18, 9, 0, 10)
        tick(sched)
        assert len(pushes) == 3 and "times in a row" not in pushes[2][1]

    def test_occurrence_keys_never_reach_notified(self, tmp_path):
        write_cron(tmp_path, "0 * * * *", task_id="hourly")
        fail = TriggerOutcome(OPEN_FAILED, error="boom")
        clock = Clock(D(2026, 10, 15, 0, 0, 10))
        sched, _, pushes = build(tmp_path, clock, fail)
        for hour in range(72):
            clock.now = D(2026, 10, 15, 0, 0, 10) + timedelta(hours=hour)
            tick(sched)
        state = schedule.scheduler_state(tmp_path / "state").read()
        assert not any(k.startswith("hourly:") for k in state["notified"])
        assert len(pushes) == 3  # one per day of a three-day streak

    def test_a_one_shot_open_failure_keeps_its_s1_push(self, tmp_path):
        from test_scheduler import AT, write_task
        write_task(tmp_path / "automations")
        sched, _, pushes = build(tmp_path, Clock(AT), TriggerOutcome(OPEN_FAILED, error="x"))
        tick(sched)
        assert "will try again until 22:05 Fri 2026-10-16" in pushes[0][1]


class TestDaylightSaving:
    """The naive clock as the OS presents it across a transition, tick by tick."""

    def run_ticks(self, tmp_path, expr, times):
        write_cron(tmp_path, expr)
        clock = Clock(times[0])
        sched, seam, _ = build(tmp_path, clock)
        fires = []
        for when in times:
            clock.now = when
            before = len(seam.requests)
            tick(sched)
            fires.append([
                (r.scheduled_for, r.fired_at) for r in seam.requests[before:]
            ])
        return fires

    def test_spring_forward_fires_the_skipped_slot_at_the_jump(self, tmp_path):
        # Europe/Warsaw 2026-03-29: 01:59:59 is followed by 03:00:00.
        times = [D(2026, 3, 29, 1, 59, 30), D(2026, 3, 29, 3, 0, 0), D(2026, 3, 29, 3, 0, 30)]
        fires = self.run_ticks(tmp_path, "30 2 * * *", times)
        assert fires == [[], [(D(2026, 3, 29, 2, 30), D(2026, 3, 29, 3, 0))], []]

    @pytest.mark.parametrize(
        "expr,expected_first_pass,expected_second_pass,expected_after",
        [
            ("30 2 * * *", 1, 0, 0),
            ("0 * * * *", 1, 0, 1),
            ("*/5 * * * *", 12, 0, 1),
        ],
    )
    def test_fall_back_fires_the_repeated_hour_once(
        self, tmp_path, expr, expected_first_pass, expected_second_pass, expected_after
    ):
        # Europe/Warsaw 2026-10-25: 02:59:59 is followed by 02:00:00 again.
        first = [D(2026, 10, 25, 2, m, 10) for m in range(0, 60, 5)]
        second = list(first)
        after = [D(2026, 10, 25, 3, 0, 10)]
        fires = self.run_ticks(tmp_path, expr, [D(2026, 10, 25, 1, 59)] + first + second + after)
        fires = fires[1:]
        count = [sum(len(f) for f in part) for part in (
            fires[:12], fires[12:24], fires[24:])]
        assert count == [expected_first_pass, expected_second_pass, expected_after]


class TestRecurringCard:
    def test_it_says_the_expr_and_the_next_two_runs(self):
        task = validate_task(cron_raw("0 9 * * 1-5", note="morning invoices"),
                             now=D(2026, 10, 15, 18, 30))
        text = schedule.describe(task, D(2026, 10, 15, 18, 30))
        assert ("Runs on the schedule `0 9 * * 1-5` — next: Fri 2026-10-16 09:00, "
                "Mon 2026-10-19 09:00") in text
        assert "Summarise new invoices." in text
        assert "read-only ones only" in text
        assert "has not started by" not in text
        assert "only the latest missed run fires" in text


class TestScheduleTaskRecurring:
    def test_a_cron_call_draws_a_card_and_saves_the_expr(self):
        from test_schedule_task import card_agent
        agent, cards = card_agent()
        result = agent._dispatch("schedule_task", {
            "id": "weekday-invoices", "cron": "0 9 * * 1-5",
            "note": "invoices each weekday", "prompt": "Summarise new invoices.",
        })
        assert "on the schedule `0 9 * * 1-5`" in str(result)
        [plan] = cards
        assert "Runs on the schedule `0 9 * * 1-5` — next:" in plan.note
        saved = json.loads(plan.target.read_text(encoding="utf-8"))
        assert saved["schedule"] == {"cron": "0 9 * * 1-5"}
        assert schedule.load_tasks(schedule.automations_dir()).problems == []

    @pytest.mark.parametrize(
        "over,fragment",
        [
            ({"at": "2030-01-01T09:00", "cron": "0 9 * * *"}, "exactly one"),
            ({}, "exactly one"),
            ({"cron": "0 0 30 2 *"}, "no occurrence"),
            ({"cron": "0 9 * * MON"}, "not supported"),
        ],
    )
    def test_bad_schedules_are_refused_before_any_card(self, over, fragment):
        from test_schedule_task import card_agent
        agent, cards = card_agent()
        result = agent._dispatch("schedule_task", {
            "id": "x", "note": "n", "prompt": "p", **over,
        })
        assert str(result).startswith("ERROR") and fragment in str(result)
        assert cards == []


def test_the_loop_survives_a_cron_task_in_a_full_tick(tmp_path):
    # One cron, one one-shot, one broken file: one tick handles all three.
    write_cron(tmp_path)
    (tmp_path / "automations" / "broken.json").write_text("{", encoding="utf-8")
    from test_scheduler import write_task
    write_task(tmp_path / "automations", at=D(2026, 10, 15, 9, 0))
    sched, seam, _ = build(tmp_path, Clock(D(2026, 10, 15, 9, 0, 10)))
    asyncio.run(sched.tick())
    assert sorted(fired_keys(seam)) == ["daily:2026-10-15T09:00:00",
                                        "eon-invoices:2026-10-15T09:00:00"]
