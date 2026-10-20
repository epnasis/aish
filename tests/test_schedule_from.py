"""A structural start on recurring automations (epic #142 W2, S6, issue #457).

`schedule.from` (Python: `Task.start`) starts a cron task: no occurrence
before it is ever due, enforced at the one provable-miss site; skip records
never reach before it; every surface that lists what is coming clips at it;
`run_now` refuses before it. The mirror of S5's `until`
(`test_schedule_until.py`). Fake clock, scripted seam, no server.
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
from aish.schedule import DUE, ENDED, PENDING, InvalidTask, due, validate_task
from aish.scheduler import REFUSED, STARTED, TriggerOutcome, run_prompt

D = datetime
DAILY = "0 9 * * *"
BEGAN = D(2026, 10, 20, 22, 0)  # creation: the file's mtime, so `since`
START = D(2026, 11, 7, 0, 0)  # a Saturday
FIRST = D(2026, 11, 7, 9, 0)
UNTIL = D(2026, 11, 14, 23, 59)


def raw(task_id="trip", start=START, until=UNTIL, expr=DAILY, **over):
    sched = {"cron": expr}
    if start is not None:
        sched["from"] = start.isoformat(timespec="minutes")
    if until is not None:
        sched["until"] = until.isoformat(timespec="minutes")
    body = {"id": task_id, "prompt": "Check the trip bookings.", "schedule": sched}
    body.update(over)
    return body


def write_file(tmp_path, began=BEGAN, task_id="trip", **over):
    directory = tmp_path / "automations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps(raw(task_id, **over)), encoding="utf-8")
    os.utime(path, (began.timestamp(), began.timestamp()))
    return path


def task(now=BEGAN, **over):
    return validate_task(raw(**over), now=now)


def key(when):
    return f"trip:{when.isoformat()}"


def loaded(tmp_path, as_of):
    return schedule.load_tasks(tmp_path / "automations", as_of=as_of)


# -- validation -------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("shape", [
        {"at": "2026-11-01T09:00"},
        {"cron": DAILY},
        {"cron": DAILY, "until": "2026-11-14T23:59"},
        {"cron": DAILY, "from": "2026-11-07T00:00"},
        {"cron": DAILY, "from": "2026-11-07T00:00", "until": "2026-11-14T23:59"},
    ])
    def test_the_five_allowed_shapes(self, shape):
        t = validate_task({"id": "x", "prompt": "p", "schedule": shape}, now=BEGAN)
        assert t.schedule == {k: datetime.fromisoformat(v).isoformat() if k != "cron" else v
                              for k, v in shape.items()}

    def test_from_lands_on_task_start(self):
        t = task()
        assert t.start == START and t.until == UNTIL
        assert t.schedule == {"cron": DAILY, "from": START.isoformat(),
                              "until": UNTIL.isoformat()}

    def test_from_beside_at_is_refused_with_its_own_sentence(self):
        with pytest.raises(InvalidTask, match="schedule.from starts a recurring schedule "
                                              "and goes with cron only"):
            validate_task({"id": "x", "prompt": "p", "schedule": {
                "at": "2026-11-01T09:00", "from": "2026-10-25T00:00"}}, now=BEGAN)

    def test_from_alone_is_refused(self):
        with pytest.raises(InvalidTask, match="cron only"):
            validate_task({"id": "x", "prompt": "p",
                           "schedule": {"from": "2026-11-07T00:00"}}, now=BEGAN)

    def test_a_bare_date_is_refused_by_the_at_rule(self):
        with pytest.raises(InvalidTask, match="schedule.from must be an ISO local date and time"):
            validate_task(raw(start=None, schedule={"cron": DAILY, "from": "2026-11-07"}),
                          now=BEGAN)

    def test_an_offset_is_converted_to_local_time(self):
        aware = datetime(2026, 11, 7, 8, 0).astimezone()
        t = validate_task(raw(start=None, schedule={"cron": DAILY, "from": aware.isoformat()}),
                          now=BEGAN)
        assert t.start == aware.astimezone().replace(tzinfo=None)

    @pytest.mark.parametrize("start", [UNTIL, UNTIL + timedelta(days=1)])
    def test_from_not_before_until_is_refused_at_creation_and_at_load(self, tmp_path, start):
        with pytest.raises(InvalidTask, match="is not before schedule.until"):
            task(start=start)
        write_file(tmp_path, start=start)
        [problem] = loaded(tmp_path, BEGAN).problems
        assert "is not before schedule.until" in problem.error

    def test_no_occurrence_in_the_window_is_a_creation_error_only(self, tmp_path):
        # 09:00 on the 14th is before `from`; the 15th's is after `until`.
        late_start = D(2026, 11, 14, 10, 0)
        with pytest.raises(InvalidTask, match="no occurrence at or after schedule.from "
                                              "2026-11-14T10:00 and at or before"):
            task(start=late_start)
        write_file(tmp_path, start=late_start)
        assert loaded(tmp_path, BEGAN).problems == []

    def test_a_first_run_exactly_at_from_is_inside_the_window(self):
        t = task(start=D(2026, 11, 14, 9, 0), until=D(2026, 11, 14, 9, 30))
        assert schedule.upcoming(t, BEGAN, 3) == [D(2026, 11, 14, 9, 0)]

    def test_a_past_from_at_creation_is_allowed(self):
        assert task(start=D(2026, 10, 1, 0, 0)).start == D(2026, 10, 1, 0, 0)

    def test_without_until_a_far_from_still_needs_an_occurrence(self):
        # Inert check that the window probe, not only `now`, is horizon-bound.
        with pytest.raises(InvalidTask, match="no occurrence within"):
            task(start=START, until=None, expr="0 0 30 2 *")


class TestRoundTrip:
    def test_render_then_load_keeps_from(self, tmp_path):
        t = task()
        (tmp_path / "trip.json").write_text(t.render(), encoding="utf-8")
        assert schedule.load_tasks(tmp_path, as_of=BEGAN).tasks == [t]

    @pytest.mark.parametrize("until", [UNTIL, None])
    def test_tasks_disable_then_enable_keeps_from(self, until):
        path = schedule.automations_dir()
        path.mkdir(parents=True, exist_ok=True)
        (path / "trip.json").write_text(json.dumps(raw(until=until)), encoding="utf-8")
        now = D(2026, 11, 1, 12, 0)
        admin.set_enabled("trip", False, deny_prefixes=(), now=now)
        admin.set_enabled("trip", True, deny_prefixes=(), now=now)
        saved = json.loads((path / "trip.json").read_text(encoding="utf-8"))
        expected = {"cron": DAILY, "from": START.isoformat()}
        if until is not None:
            expected["until"] = until.isoformat()
        assert saved["schedule"] == expected


# -- due(): the edge ----------------------------------------------------------


class TestDueAtTheStart:
    def test_a_minute_before_from_nothing_is_due(self):
        t = task(start=FIRST)
        now = FIRST - timedelta(minutes=1)
        assert due(t, set(), now, since=BEGAN) == schedule.Due(PENDING, D(2026, 11, 6, 9, 0))

    def test_at_from_it_is_due(self):
        t = task(start=FIRST)
        assert due(t, set(), FIRST + timedelta(seconds=10), since=BEGAN) == schedule.Due(
            DUE, FIRST)

    def test_after_from_it_is_ordinary(self):
        nxt = FIRST + timedelta(days=1)
        assert due(task(), {key(FIRST)}, nxt + timedelta(minutes=5), since=BEGAN) == (
            schedule.Due(DUE, nxt))

    def test_from_never_stands_in_for_since(self):
        # Fail-closed is kept: no `since`, nothing is provably a miss.
        assert due(task(), set(), FIRST + timedelta(hours=1), since=None).status == PENDING

    def test_a_later_since_still_wins(self):
        since = FIRST + timedelta(hours=1)
        assert due(task(), set(), FIRST + timedelta(hours=2), since=since).status == PENDING

    def test_a_final_occurrence_before_from_ends_and_never_fires(self, tmp_path):
        # Hand-edited: from < until, but no occurrence lands in [from, until].
        start, until = D(2026, 11, 14, 10, 0), D(2026, 11, 14, 23, 59)
        write_file(tmp_path, start=start, until=until)
        [t] = loaded(tmp_path, BEGAN).tasks
        now = until + timedelta(hours=2)
        assert due(t, set(), now, since=BEGAN) == schedule.Due(ENDED, D(2026, 11, 14, 9, 0))
        assert schedule.final_occurrence(t) is None
        sched, seam, pushes = build(tmp_path, Clock(now))
        tick(sched)
        assert seam.requests == []
        [(title, body)] = [p for p in pushes if p[0].endswith(" ended")]
        assert "final run" not in body
        assert not any(r["kind"] == "final_not_started" for r in records(tmp_path))


# -- the scheduler ------------------------------------------------------------


class TestTheStart:
    def test_nothing_fires_before_from_across_ticks(self, tmp_path):
        write_file(tmp_path)
        clock = Clock(D(2026, 10, 21, 9, 0, 10))
        sched, seam, _ = build(tmp_path, clock)
        for now in (D(2026, 10, 21, 9, 0, 10), D(2026, 11, 6, 9, 0, 10),
                    D(2026, 11, 7, 8, 59, 50)):
            clock.now = now
            tick(sched)
        assert seam.requests == []
        clock.now = FIRST + timedelta(seconds=10)
        tick(sched)
        [request] = seam.requests
        assert request.dedup_key == key(FIRST)
        assert ("Scheduled for 2026-11-07 09:00 (recurring: `0 9 * * *`, from 2026-11-07 "
                "00:00, until 2026-11-14 23:59); fired at 2026-11-07 09:00:10") in request.prompt

    def test_daemon_down_over_the_first_slot_fires_the_latest_and_skips_from_the_start(
        self, tmp_path
    ):
        # Created Oct 20, starts Nov 7, aish-web down Nov 7-10.
        write_file(tmp_path)
        clock = Clock(D(2026, 10, 21, 12, 0))
        sched, seam, _ = build(tmp_path, clock)
        tick(sched)  # seeds `since` at the Oct 20 mtime
        clock.now = D(2026, 11, 10, 10, 0)
        tick(sched)
        assert [r.dedup_key for r in seam.requests] == [key(D(2026, 11, 10, 9, 0))]
        [skip] = [r for r in records(tmp_path) if r["kind"] == "skipped"]
        # The skip record's own "from" key is the RANGE start, not schedule.from.
        assert (skip["from"], skip["through"], skip["count"]) == (
            "2026-11-07T09:00:00", "2026-11-09T09:00:00", 3)

    def test_an_edited_earlier_from_fires_one_catch_up_with_a_skip_record(self, tmp_path):
        # Deliberate: (id, expr) keeps `since`, so moving `from` earlier is
        # extend-until's semantics at the other edge — one late run, recorded.
        write_file(tmp_path)
        clock = Clock(D(2026, 10, 21, 12, 0))
        sched, seam, _ = build(tmp_path, clock)
        tick(sched)
        clock.now = D(2026, 10, 25, 10, 0)
        write_file(tmp_path, start=D(2026, 10, 22, 0, 0), began=clock.now)
        tick(sched)
        tick(sched)
        assert [r.dedup_key for r in seam.requests] == [key(D(2026, 10, 25, 9, 0))]
        [skip] = [r for r in records(tmp_path) if r["kind"] == "skipped"]
        assert (skip["from"], skip["through"], skip["count"]) == (
            "2026-10-22T09:00:00", "2026-10-24T09:00:00", 3)

    def test_disabled_before_from_and_enabled_after_it_fires_nothing_past(self, tmp_path):
        write_file(tmp_path)
        clock = Clock(D(2026, 10, 21, 12, 0))
        sched, seam, _ = build(tmp_path, clock)
        tick(sched)
        clock.now = D(2026, 11, 1, 12, 0)
        write_file(tmp_path, enabled=False, began=clock.now)
        tick(sched)
        clock.now = D(2026, 11, 9, 12, 0)
        write_file(tmp_path, began=clock.now)
        tick(sched)
        assert seam.requests == []  # Nov 9 09:00 is before the re-enable's `since`
        clock.now = D(2026, 11, 10, 9, 0, 10)
        tick(sched)
        assert [r.dedup_key for r in seam.requests] == [key(D(2026, 11, 10, 9, 0))]
        assert not any(r["kind"] == "skipped" for r in records(tmp_path))

    def test_disabled_and_enabled_before_from_starts_on_time(self, tmp_path):
        write_file(tmp_path)
        clock = Clock(D(2026, 10, 21, 12, 0))
        sched, seam, _ = build(tmp_path, clock)
        tick(sched)
        clock.now = D(2026, 10, 25, 12, 0)
        write_file(tmp_path, enabled=False, began=clock.now)
        tick(sched)
        clock.now = D(2026, 11, 1, 12, 0)
        write_file(tmp_path, began=clock.now)
        tick(sched)
        clock.now = FIRST + timedelta(seconds=10)
        tick(sched)
        assert [r.dedup_key for r in seam.requests] == [key(FIRST)]


class TestRunNowBeforeTheStart:
    def test_refused_before_from_naming_the_start_and_the_way_on(self, tmp_path):
        write_file(tmp_path)
        sched, seam, _ = build(tmp_path, Clock(START - timedelta(minutes=1)))
        outcome = asyncio.run(sched.fire_now("trip"))
        assert outcome.status == REFUSED and seam.requests == []
        assert "has not started: it starts 00:00 Sat 2026-11-07 (its from)" in outcome.error
        assert "ask aish in a chat" in outcome.error
        assert "re-save it with an earlier start" in outcome.error

    def test_at_from_it_runs(self, tmp_path):
        write_file(tmp_path)
        sched, _seam, _ = build(tmp_path, Clock(START), TriggerOutcome(STARTED, session="s-1"))
        assert asyncio.run(sched.fire_now("trip")).status == STARTED


# -- rendering: everything clips at from ----------------------------------------


class TestRendering:
    def test_upcoming_clips_both_ends(self):
        t = task(until=D(2026, 11, 9, 23, 59))
        assert schedule.upcoming(t, BEGAN, 5) == [
            FIRST, D(2026, 11, 8, 9, 0), D(2026, 11, 9, 9, 0)]

    def test_upcoming_after_from_is_unchanged(self):
        assert schedule.upcoming(task(), D(2026, 11, 8, 10, 0), 1) == [D(2026, 11, 9, 9, 0)]

    def test_when_words_states_the_window(self):
        assert schedule.when_words(task(), BEGAN) == (
            "on the schedule `0 9 * * *` from Sat 2026-11-07 00:00 until Sat 2026-11-14 23:59")
        assert schedule.when_words(task(until=None), BEGAN) == (
            "on the schedule `0 9 * * *` from Sat 2026-11-07 00:00")

    def test_the_card_says_the_first_run_and_clips_the_next_two(self):
        note = schedule.describe(task(), BEGAN)
        assert "— next: Sat 2026-11-07 09:00, Sun 2026-11-08 09:00 — each time" in note
        assert ("Its first run is Sat 2026-11-07 09:00: no occurrence before Sat "
                "2026-11-07 00:00 is ever run.") in note
        assert "Its last run is Sat 2026-11-14 09:00, then it ends" in note
        lines = note.split("\n")
        assert lines.index(next(x for x in lines if x.startswith("Its first run"))) < (
            lines.index(next(x for x in lines if x.startswith("Its last run"))))

    def test_a_past_from_still_shows_a_sane_first_run(self):
        note = schedule.describe(task(start=D(2026, 10, 1, 0, 0)), BEGAN)
        assert "Its first run is Wed 2026-10-21 09:00." in note
        assert "no occurrence before" not in note

    def test_a_replacing_card_says_next_not_first(self):
        note = schedule.describe(task(), BEGAN, replacing=True)
        assert "Its next run is Sat 2026-11-07 09:00" in note

    def test_a_card_without_from_is_unchanged(self):
        note = schedule.describe(task(start=None), BEGAN)
        assert "first run" not in note and " from " not in note.split("\n")[1]

    def test_the_run_prompt_states_the_start(self):
        prompt = run_prompt(task(until=None), FIRST, FIRST)
        assert "(recurring: `0 9 * * *`, from 2026-11-07 00:00); fired at" in prompt


NOW = D(2026, 10, 25, 10, 0)


def write_task(**over):
    directory = schedule.automations_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "trip.json").write_text(json.dumps(raw(**over)), encoding="utf-8")


@pytest.fixture
def state_dir(tmp_path):
    path = tmp_path / "state"
    path.mkdir()
    return path


def listing_text(state_dir, now):
    listing = admin.read_listing(state_dir, deny_prefixes=(), now=now)
    return "\n".join(admin.task_lines(listing, now))


class TestTasksListing:
    def test_a_task_not_yet_started_says_starts(self, state_dir):
        write_task()
        text = listing_text(state_dir, NOW)
        assert "    starts 09:00 Sat 2026-11-07" in text and "next " not in text
        assert "runs on the schedule `0 9 * * *` from Sat 2026-11-07 00:00 until" in text

    def test_disabled_before_from(self, state_dir):
        write_task(enabled=False)
        assert "would start 09:00 Sat 2026-11-07 if enabled" in listing_text(state_dir, NOW)

    def test_after_from_it_says_next(self, state_dir):
        write_task()
        assert "    next 09:00 Mon 2026-11-09" in listing_text(state_dir, D(2026, 11, 8, 10, 0))

    def test_a_final_before_from_lists_as_ended_never_may_still_start(self, state_dir):
        write_task(start=D(2026, 11, 14, 10, 0))
        text = listing_text(state_dir, UNTIL + timedelta(hours=1))
        assert "· ENDED ·" in text and "may still start" not in text

    def test_enabled_words_say_it_starts(self, state_dir):
        write_task()
        t = schedule.load_tasks(schedule.automations_dir(), as_of=NOW).tasks[0]
        assert admin.enabled_words(t, state_dir, NOW) == (
            "it starts 09:00 Sat 2026-11-07, the first scheduled time in its window; it "
            "ends 23:59 Sat 2026-11-14, after its last run, for 09:00 Sat 2026-11-14")


# -- the tool: from is structure, and the card reads it from the field ----------


class TestScheduleTaskFrom:
    def args(self, **over):
        base = {"id": "trip-check", "cron": DAILY, "note": "trip",
                "prompt": "Check the trip bookings and email me if anything needs attention."}
        base.update(over)
        return base

    def test_from_lands_in_the_file_and_the_result_names_the_clipped_next(self):
        start = (datetime.now() + timedelta(days=5)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        agent, cards = card_agent()
        result = str(agent._dispatch("schedule_task", self.args(**{"from": start.isoformat()})))
        [plan] = cards
        saved = json.loads(plan.target.read_text(encoding="utf-8"))
        assert saved["schedule"] == {"cron": DAILY, "from": start.isoformat()}
        first = start.replace(hour=9)
        assert f"Its first run is {schedule.day_time(first)}" in plan.note
        # Never `cron_next(now)` raw, which would name tomorrow's 09:00.
        assert f"(next: {first.isoformat(sep=' ', timespec='minutes')}; it starts at" in result
        assert "no occurrence before that ever runs" in result

    def test_from_beside_at_is_an_error_to_the_model(self):
        agent, cards = card_agent()
        at = (datetime.now() + timedelta(days=1)).replace(microsecond=0).isoformat()
        result = agent._dispatch("schedule_task", self.args(
            cron="", at=at, **{"from": "2030-01-01T00:00"}))
        assert "schedule.from starts a recurring schedule" in str(result) and cards == []

    def test_an_empty_from_is_dropped_and_the_card_shows_no_start(self):
        agent, cards = card_agent()
        agent._dispatch("schedule_task", self.args(**{"from": ""}))
        [plan] = cards
        assert json.loads(plan.target.read_text(encoding="utf-8"))["schedule"] == {
            "cron": DAILY}
        assert "first run" not in plan.note
