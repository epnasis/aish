"""A structural end on recurring automations (epic #142 W2, S5, issue #456).

`schedule.until` ends a cron task: no occurrence after it is ever due, the
final one keeps a one-shot's grace, the ending is DERIVED (never settled) and
pushed once whatever the notify policy, and every surface that lists what is
coming clips at it. Fake clock, scripted seam, no server — the S1/S2 harness.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

import pytest
from test_schedule_task import card_agent
from test_scheduler import Clock, build, records, tick

from aish import automation_admin as admin
from aish import schedule
from aish.schedule import (
    DISABLED,
    DUE,
    ENDED,
    FIRED,
    PENDING,
    STALE_AFTER,
    InvalidTask,
    due,
    validate_task,
)
from aish.scheduler import OPEN_FAILED, REFUSED, STARTED, TriggerOutcome

D = datetime
DAILY = "0 9 * * *"
UNTIL = D(2026, 11, 14, 23, 59)
FINAL = D(2026, 11, 14, 9, 0)  # a Saturday
BEGAN = D(2026, 10, 20, 22, 0)


def raw(task_id="paris", until=UNTIL, expr=DAILY, **over):
    sched = {"cron": expr}
    if until is not None:
        sched["until"] = until.isoformat(timespec="minutes")
    body = {"id": task_id, "prompt": "Check the Paris trip bookings.", "schedule": sched}
    body.update(over)
    return body


def write_file(tmp_path, began=BEGAN, task_id="paris", **over):
    directory = tmp_path / "automations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps(raw(task_id, **over)), encoding="utf-8")
    os.utime(path, (began.timestamp(), began.timestamp()))
    return path


def seed(tmp_path, *occurrences, task_id="paris"):
    store = schedule.fired_ledger(tmp_path / "state")
    data = store.read()
    for when in occurrences:
        data[f"{task_id}:{when.isoformat()}"] = {"fired_at": when.isoformat(), "session": "s"}
    store.write(data)


def ended_pushes(pushes):
    return [p for p in pushes if p[0].endswith(" ended")]


def task(**over):
    return validate_task(raw(**over), now=BEGAN)


# -- validation -------------------------------------------------------------


class TestValidation:
    def test_until_rides_a_cron_schedule(self):
        t = task()
        assert t.until == UNTIL and t.schedule == {"cron": DAILY, "until": UNTIL.isoformat()}

    def test_until_beside_at_is_refused(self):
        with pytest.raises(InvalidTask, match="cron only"):
            validate_task({"id": "x", "prompt": "p", "schedule": {
                "at": "2026-11-01T09:00", "until": "2026-11-14T23:59"}}, now=BEGAN)

    def test_until_alone_is_refused(self):
        with pytest.raises(InvalidTask, match="cron only"):
            validate_task({"id": "x", "prompt": "p",
                           "schedule": {"until": "2026-11-14T23:59"}}, now=BEGAN)

    def test_a_bare_date_is_refused_by_the_at_rule(self):
        with pytest.raises(InvalidTask, match="schedule.until must be an ISO local date and time"):
            validate_task(raw(until=None, schedule={"cron": DAILY, "until": "2026-11-14"}),
                          now=BEGAN)

    def test_a_past_until_is_a_creation_error_and_never_a_load_problem(self, tmp_path):
        past = D(2026, 10, 1, 23, 59)
        with pytest.raises(InvalidTask, match="not in the future"):
            validate_task(raw(until=past), now=BEGAN)
        write_file(tmp_path, until=past)
        loaded = schedule.load_tasks(tmp_path / "automations", as_of=BEGAN)
        assert loaded.problems == [] and loaded.tasks[0].until == past

    def test_no_occurrence_before_the_end_is_a_creation_error_only(self, tmp_path):
        # Created at 10:00 with the end at 23:59 the same day: 09:00 is gone.
        same_day = D(2026, 10, 20, 23, 59)
        with pytest.raises(InvalidTask, match="no occurrence after now and at or before"):
            validate_task(raw(until=same_day), now=D(2026, 10, 20, 10, 0))
        write_file(tmp_path, until=same_day)
        assert schedule.load_tasks(tmp_path / "automations",
                                   as_of=D(2026, 10, 20, 10, 0)).problems == []

    def test_an_occurrence_exactly_at_the_end_is_inside_it(self):
        assert validate_task(raw(until=FINAL), now=BEGAN).until == FINAL

    def test_the_scheduler_loads_an_ended_file_with_no_problem_and_no_problem_push(
        self, tmp_path
    ):
        write_file(tmp_path, until=D(2026, 10, 1, 23, 59), began=D(2026, 9, 1))
        sched, _seam, pushes = build(tmp_path, Clock(BEGAN))
        tick(sched)
        assert sched.problems == []
        assert not any("was skipped" in title for title, _ in pushes)


class TestRoundTrip:
    def test_render_then_load_keeps_until(self, tmp_path):
        t = task()
        (tmp_path / "paris.json").write_text(t.render(), encoding="utf-8")
        assert schedule.load_tasks(tmp_path, as_of=BEGAN).tasks == [t]

    def test_tasks_disable_then_enable_keeps_until(self):
        path = schedule.automations_dir()
        path.mkdir(parents=True, exist_ok=True)
        (path / "paris.json").write_text(json.dumps(raw()), encoding="utf-8")
        now = D(2026, 11, 1, 12, 0)
        admin.set_enabled("paris", False, deny_prefixes=(), now=now)
        admin.set_enabled("paris", True, deny_prefixes=(), now=now)
        saved = json.loads((path / "paris.json").read_text(encoding="utf-8"))
        assert saved["schedule"] == {"cron": DAILY, "until": UNTIL.isoformat()}


# -- due(): every arm once now is past until --------------------------------


def key(when):
    return f"paris:{when.isoformat()}"


class TestDueAtTheEnd:
    def test_before_the_end_nothing_changes(self):
        now = D(2026, 11, 14, 9, 0, 30)
        assert due(task(), set(), now, since=BEGAN) == schedule.Due(DUE, FINAL)

    def test_an_occurrence_after_until_is_never_due(self):
        # The 15th's 09:00 has passed, but it is after the end: the answer is
        # about the final occurrence, and it fired.
        now = D(2026, 11, 15, 9, 30)
        assert due(task(), {key(FINAL)}, now, since=BEGAN) == schedule.Due(ENDED, FINAL)

    def test_the_end_is_inclusive(self):
        t = task(until=FINAL)
        assert due(t, set(), FINAL + timedelta(minutes=1), since=BEGAN).status == DUE

    def test_an_unfired_final_occurrence_keeps_the_grace(self):
        t = task()
        prev = {key(D(2026, 11, 13, 9, 0))}
        assert due(t, prev, UNTIL + timedelta(hours=3), since=BEGAN) == schedule.Due(DUE, FINAL)
        assert due(t, prev, UNTIL + STALE_AFTER, since=BEGAN).status == DUE
        after = due(t, prev, UNTIL + STALE_AFTER + timedelta(minutes=1), since=BEGAN)
        assert after == schedule.Due(ENDED, FINAL, final_missed=True)

    def test_no_since_or_a_final_before_since_is_ended_never_pending(self):
        t = task()
        now = UNTIL + timedelta(hours=1)
        assert due(t, set(), now, since=None) == schedule.Due(ENDED, FINAL)
        assert due(t, set(), now, since=FINAL + timedelta(minutes=1)) == schedule.Due(ENDED, FINAL)

    def test_a_final_at_or_before_the_last_fired_is_ended(self):
        # A clock that stepped back: a later key fired, so the final is no miss.
        later = {key(D(2026, 11, 15, 9, 0))}
        assert due(task(), later, UNTIL + timedelta(hours=1), since=BEGAN).status == ENDED

    def test_ended_outranks_disabled(self):
        t = task(enabled=False)
        assert due(t, set(), D(2026, 11, 1, 10, 0), since=None).status == DISABLED
        assert due(t, set(), UNTIL + timedelta(hours=1), since=BEGAN).status == ENDED

    def test_no_occurrence_inside_the_horizon_is_ended(self, monkeypatch):
        t = task()

        def none(expr, at_or_before):
            raise schedule.NoOccurrence("none")

        monkeypatch.setattr(schedule, "cron_prev", none)
        assert due(t, set(), UNTIL + timedelta(hours=1), since=BEGAN) == schedule.Due(ENDED, None)

    def test_a_task_without_until_is_untouched(self):
        t = task(until=None)
        assert due(t, {key(FINAL)}, FINAL + timedelta(days=30), since=BEGAN).status == DUE
        assert due(t, set(), D(2026, 10, 21, 8, 0), since=BEGAN).status in (PENDING, FIRED)


# -- the scheduler: the final run, the ending, the push ---------------------


class TestTheEnding:
    def test_a_late_final_fire_crosses_until_and_states_both_times(self, tmp_path):
        write_file(tmp_path)
        seed(tmp_path, D(2026, 11, 13, 9, 0))
        clock = Clock(D(2026, 11, 15, 2, 0))
        sched, seam, pushes = build(tmp_path, clock)
        tick(sched)
        [request] = seam.requests
        assert request.dedup_key == key(FINAL)
        assert ("Scheduled for 2026-11-14 09:00 (recurring: `0 9 * * *`, until 2026-11-14 "
                "23:59); fired at 2026-11-15 02:00:00") in request.prompt
        tick(sched)  # the final fired, so now it has ended
        assert len(seam.requests) == 1
        [(title, body)] = ended_pushes(pushes)
        assert title == "aish automation paris ended"
        assert body == ("automation paris ended as scheduled: its until "
                        "(23:59 Sat 2026-11-14) has passed.")

    def test_the_ending_pushes_once_across_ticks_and_restarts(self, tmp_path):
        write_file(tmp_path)
        seed(tmp_path, FINAL)
        clock = Clock(UNTIL + timedelta(hours=2))
        sched, _seam, pushes = build(tmp_path, clock)
        tick(sched)
        clock.now += timedelta(days=3)
        tick(sched)
        again, _seam2, pushes2 = build(tmp_path, clock)
        tick(again)
        assert len(ended_pushes(pushes)) == 1 and ended_pushes(pushes2) == []
        ended = [r for r in records(tmp_path) if r["kind"] == "ended"]
        assert len(ended) == 1 and ended[0]["until"] == UNTIL.isoformat(timespec="seconds")
        # Derived, never stored: no settled entry for a cron task.
        state = schedule.scheduler_state(tmp_path / "state").read()
        assert state.get("settled") == {}

    @pytest.mark.parametrize("notify", ["never", "on_change", "always"])
    def test_the_ending_push_ignores_the_notify_policy(self, tmp_path, notify):
        write_file(tmp_path, kind="monitor", notify=notify)
        seed(tmp_path, FINAL)
        sched, _seam, pushes = build(tmp_path, Clock(UNTIL + timedelta(hours=2)))
        tick(sched)
        assert len(ended_pushes(pushes)) == 1

    def test_a_disabled_task_also_ends_once(self, tmp_path):
        write_file(tmp_path, enabled=False)
        sched, seam, pushes = build(tmp_path, Clock(UNTIL + timedelta(hours=2)))
        tick(sched)
        assert seam.requests == [] and len(ended_pushes(pushes)) == 1

    def test_a_final_run_that_never_started_says_what_was_observed(self, tmp_path):
        write_file(tmp_path)
        seed(tmp_path, D(2026, 11, 13, 9, 0))
        sched, seam, pushes = build(tmp_path, Clock(UNTIL + STALE_AFTER + timedelta(hours=1)))
        tick(sched)
        assert seam.requests == []
        [final] = [r for r in records(tmp_path) if r["kind"] == "final_not_started"]
        assert final["key"] == key(FINAL)
        assert final["error"] == "aish did not observe an attempt to start it"
        [(_t, body)] = ended_pushes(pushes)
        assert body.endswith("Its final run, scheduled for 09:00 Sat 2026-11-14, did not "
                             "start: aish did not observe an attempt to start it.")

    def test_retries_and_the_streak_stop_at_the_grace_edge(self, tmp_path):
        write_file(tmp_path)
        seed(tmp_path, D(2026, 11, 13, 9, 0))
        clock = Clock(UNTIL + timedelta(hours=23))
        sched, seam, pushes = build(
            tmp_path, clock, TriggerOutcome(OPEN_FAILED, error="model unavailable: gone")
        )
        tick(sched)
        assert len(seam.requests) == 1
        [(title, body)] = pushes
        assert title == "aish automation paris did not start"
        assert ("until 23:59 Sun 2026-11-15: it is the last run before this automation's "
                "end, and it is not tried after that") in body
        clock.now += timedelta(minutes=10)
        tick(sched)
        assert len(seam.requests) == 2  # still inside the grace: retried
        clock.now = UNTIL + STALE_AFTER + timedelta(minutes=1)
        tick(sched)
        clock.now += timedelta(hours=1)
        tick(sched)
        assert len(seam.requests) == 2  # past the grace: never tried again
        state = schedule.scheduler_state(tmp_path / "state").read()
        assert "paris" not in state.get("streaks", {})
        [(_t, body)] = ended_pushes(pushes)
        assert "did not start: it was last refused with: model unavailable: gone." in body

    def test_extending_until_catches_up_once_and_ends_afresh(self, tmp_path):
        write_file(tmp_path)
        seed(tmp_path, FINAL)
        clock = Clock(UNTIL + timedelta(hours=2))
        sched, seam, pushes = build(tmp_path, clock)
        tick(sched)
        assert len(ended_pushes(pushes)) == 1
        # The owner extends the end on the same expr, on the 20th.
        later = D(2026, 11, 30, 23, 59)
        clock.now = D(2026, 11, 20, 10, 0)
        write_file(tmp_path, until=later, began=clock.now)
        tick(sched)
        # `since` is kept (same expr): ONE catch-up fire, and the gap recorded.
        assert [r.dedup_key for r in seam.requests] == [key(D(2026, 11, 20, 9, 0))]
        [skip] = [r for r in records(tmp_path) if r["kind"] == "skipped"]
        assert (skip["from"], skip["through"], skip["count"]) == (
            "2026-11-15T09:00:00", "2026-11-19T09:00:00", 5)
        seed(tmp_path, D(2026, 11, 30, 9, 0))
        clock.now = D(2026, 12, 1, 10, 0)
        tick(sched)
        assert len(ended_pushes(pushes)) == 2
        assert "(23:59 Mon 2026-11-30)" in ended_pushes(pushes)[1][1]


    def test_a_notified_eviction_may_repeat_the_push_but_never_the_records(self, tmp_path):
        write_file(tmp_path)
        seed(tmp_path, D(2026, 11, 13, 9, 0))
        clock = Clock(UNTIL + STALE_AFTER + timedelta(hours=1))
        sched, _seam, pushes = build(tmp_path, clock)
        tick(sched)
        assert sched._state is not None
        sched._state.notified.clear()  # what MAX_RECORDS churn does to the key
        tick(sched)
        assert len(ended_pushes(pushes)) == 2  # the stated, bounded repeat
        kinds = [r["kind"] for r in records(tmp_path)]
        assert kinds.count("ended") == 1 and kinds.count("final_not_started") == 1

    def test_a_task_a_reload_replaced_does_not_end_under_its_old_until(self, tmp_path):
        write_file(tmp_path)
        seed(tmp_path, FINAL)
        sched, _seam, pushes = build(tmp_path, Clock(UNTIL + timedelta(hours=2)))
        tick(sched)  # loads the folder
        stale = sched.tasks[0]
        sched.tasks = []  # what fire_now's reload does when the file changed
        verdict = schedule.due(stale, {key(FINAL)}, UNTIL + timedelta(hours=2), since=BEGAN)
        pushes.clear()
        assert sched._state is not None
        sched._state.notified.clear()
        asyncio.run(sched._ended(stale, verdict, UNTIL + timedelta(hours=2)))
        assert pushes == []


class TestRunNowPastTheEnd:
    def test_refused_whenever_now_is_past_until_even_inside_the_grace(self, tmp_path):
        write_file(tmp_path)
        sched, seam, _ = build(tmp_path, Clock(UNTIL + timedelta(minutes=1)))
        outcome = asyncio.run(sched.fire_now("paris"))
        assert outcome.status == REFUSED and seam.requests == []
        assert "has ended: its until (23:59 Sat 2026-11-14) has passed" in outcome.error
        assert "ask aish for a new automation" in outcome.error

    def test_before_the_end_it_runs(self, tmp_path):
        write_file(tmp_path)
        sched, seam, _ = build(tmp_path, Clock(UNTIL - timedelta(minutes=1)),
                               TriggerOutcome(STARTED, session="s-1"))
        assert asyncio.run(sched.fire_now("paris")).status == STARTED


# -- rendering: everything clips at until -----------------------------------


class TestRendering:
    def test_when_words_names_the_end(self):
        assert schedule.when_words(task(), BEGAN) == (
            "on the schedule `0 9 * * *` until Sat 2026-11-14 23:59")

    def test_the_card_clips_the_next_two_and_says_the_last_run(self):
        t = validate_task(raw(), now=D(2026, 11, 13, 10, 0))
        note = schedule.describe(t, D(2026, 11, 13, 10, 0))
        assert "— next: Sat 2026-11-14 09:00 — each time" in note  # not Sun the 15th
        assert "Its last run is Sat 2026-11-14 09:00, then it ends: no occurrence after " \
            "Sat 2026-11-14 23:59 is ever run." in note
        assert "may still start late, until 23:59 Sun 2026-11-15, and never after" in note
        assert "notified once, whatever the notify setting" in note

    def test_a_card_without_until_is_unchanged(self):
        note = schedule.describe(task(until=None), BEGAN)
        assert "then it ends" not in note and "until" not in note.split("\n")[1]

    def test_upcoming_is_clipped(self):
        assert schedule.upcoming(task(), D(2026, 11, 13, 10, 0), 3) == [FINAL]
        assert schedule.upcoming(task(), D(2026, 11, 14, 10, 0), 3) == []


NOW = D(2026, 11, 13, 10, 0)


def write_task(**over):
    directory = schedule.automations_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "paris.json").write_text(json.dumps(raw(**over)), encoding="utf-8")


@pytest.fixture
def state_dir(tmp_path):
    path = tmp_path / "state"
    path.mkdir()
    return path


def listing_text(state_dir, now):
    listing = admin.read_listing(state_dir, deny_prefixes=(), now=now)
    return "\n".join(admin.task_lines(listing, now))


class TestTasksListing:
    def test_the_next_run_is_clipped_at_the_end(self, state_dir):
        write_task()
        assert "next 09:00 Sat 2026-11-14" in listing_text(state_dir, NOW)
        text = listing_text(state_dir, D(2026, 11, 14, 10, 0))
        assert "no run is left before its until (23:59 Sat 2026-11-14)" in text

    def test_an_ended_task_lists_as_ended(self, state_dir):
        write_task()
        schedule.fired_ledger(state_dir).write({key(FINAL): {"fired_at": "x", "session": "s"}})
        text = listing_text(state_dir, UNTIL + timedelta(hours=1))
        assert "· ENDED · notify" in text
        assert "ended 23:59 Sat 2026-11-14 · last ran for 09:00 Sat 2026-11-14" in text

    def test_ended_outranks_disabled(self, state_dir):
        write_task(enabled=False)
        text = listing_text(state_dir, UNTIL + timedelta(hours=1))
        assert "· ENDED ·" in text and "DISABLED" not in text

    def test_inside_the_grace_an_unfired_final_may_still_run(self, state_dir):
        write_task()
        text = listing_text(state_dir, UNTIL + timedelta(hours=1))
        assert "· enabled ·" in text
        assert ("past its until (23:59 Sat 2026-11-14); its final run, for 09:00 Sat "
                "2026-11-14, has not fired — aish-web may still start it, late, until "
                "23:59 Sun 2026-11-15") in text
        assert "ended 23:59" in listing_text(state_dir, UNTIL + STALE_AFTER + timedelta(1))


class TestToggles:
    def test_enable_past_the_end_is_refused(self, state_dir):
        write_task(enabled=False)
        with pytest.raises(admin.AdminError, match="its until .* has passed — ask aish"):
            admin.set_enabled("paris", True, deny_prefixes=(), now=UNTIL + timedelta(hours=1))

    def test_disable_is_refused_past_the_grace_and_allowed_inside_it(self, state_dir):
        write_task()
        with pytest.raises(admin.AdminError, match="has ended"):
            admin.set_enabled("paris", False, deny_prefixes=(),
                              now=UNTIL + STALE_AFTER + timedelta(minutes=1))
        _task, wrote = admin.set_enabled("paris", False, deny_prefixes=(),
                                         now=UNTIL + timedelta(hours=1))
        assert wrote

    def test_delete_is_allowed(self, state_dir):
        write_task()
        admin.delete("paris")
        assert not (schedule.automations_dir() / "paris.json").exists()

    def test_enabled_words_say_the_end(self, state_dir):
        write_task()
        t = schedule.load_tasks(schedule.automations_dir(), as_of=NOW).tasks[0]
        assert admin.enabled_words(t, state_dir, NOW) == (
            "its next scheduled time is 09:00 Sat 2026-11-14; it ends 23:59 Sat "
            "2026-11-14, after its last run, for 09:00 Sat 2026-11-14")
        assert "no scheduled time is left before its until" in admin.enabled_words(
            t, state_dir, D(2026, 11, 14, 10, 0))


class TestRuns:
    def test_the_ending_and_a_final_that_did_not_start_are_listed(self, state_dir):
        schedule.scheduler_state(state_dir).write({"records": [
            {"kind": "final_not_started", "task": "paris", "key": key(FINAL),
             "scheduled_for": "2026-11-14T09:00:00", "observed_at": "2026-11-16T00:00:00",
             "error": "aish did not observe an attempt to start it"},
            {"kind": "ended", "task": "paris", "key": "paris:ended:x",
             "until": "2026-11-14T23:59:00", "observed_at": "2026-11-16T00:00:01"},
        ]})
        runs, problems = admin.recent_runs(state_dir)
        text = "\n".join(admin.run_lines(runs, problems))
        assert "paris  ended as scheduled: its until (2026-11-14 23:59:00) had passed" in text
        assert ("paris  final run, for 2026-11-14 09:00:00, did not start: aish did not "
                "observe an attempt to start it") in text


# -- the tool: until is structure, and the card reads it from the field -----


class TestScheduleTaskUntil:
    def args(self, **over):
        base = {"id": "paris-trip-monitor", "cron": DAILY, "note": "Paris trip",
                "prompt": "Check the Paris bookings and email me if anything needs attention."}
        base.update(over)
        return base

    def test_until_lands_in_the_file_and_the_card_reads_the_field(self):
        until = (datetime.now() + timedelta(days=5)).replace(
            hour=23, minute=59, second=0, microsecond=0)
        agent, cards = card_agent()
        result = agent._dispatch("schedule_task", self.args(until=until.isoformat()))
        [plan] = cards
        saved = json.loads(plan.target.read_text(encoding="utf-8"))
        assert saved["schedule"] == {"cron": DAILY, "until": until.isoformat()}
        final = schedule.cron_prev(DAILY, until)
        assert f"Its last run is {schedule.day_time(final)}, then it ends" in plan.note
        assert "no occurrence after that ever runs" in str(result)

    def test_until_beside_at_is_an_error_to_the_model(self):
        agent, cards = card_agent()
        at = (datetime.now() + timedelta(days=1)).replace(microsecond=0).isoformat()
        result = agent._dispatch("schedule_task", self.args(
            cron="", at=at, until="2030-01-01T00:00"))
        assert "cron only" in str(result) and cards == []
