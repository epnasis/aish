"""Automations the owner can reach, through the REAL app (#5 S4, epic #142 W2).

Two server halves: the `silent` row fact that keeps a monitor run nobody needs
to open out of the web's "Needs you", and `run_now`, the owner's way to run an
automation outside its schedule. Driven with test_server_monitor's harness: the
scheduler loop is detached and ticked by hand on the server's loop.
"""

from __future__ import annotations

import datetime
import json
import os
import time

import pytest
from test_server import connected, model_says, recv_until, recv_until_refusal
from test_server_monitor import BEGAN, DAY1, Harness, verdict, write_monitor

from aish import schedule as schedule_module
from aish import scheduler as scheduling
from aish import server as server_module
from aish.session import SessionLog

D = datetime.datetime


@pytest.fixture
def app_env(tmp_path):
    """test_server's isolated state/allow/deny files, so nothing touches real config."""
    (tmp_path / "allow.txt").write_text("ls\n", encoding="utf-8")
    (tmp_path / "deny.txt").write_text("rm -rf\n", encoding="utf-8")
    return {
        "state_dir": tmp_path / "state",
        "allow_path": tmp_path / "allow.txt",
        "deny_path": tmp_path / "deny.txt",
        "config_path": tmp_path / "config.toml",
        "lessons_path": tmp_path / "lessons.md",
        "cwd": str(tmp_path),
    }


def rows_of(h, session):
    """The silent fact as every row publisher states it for one chat."""
    roster = h.server._roster_row(session)
    [listed] = [i for i in SessionLog.list_sessions(h.state_dir) if i.path.name == session.name]
    pager = {p[0]: p for p in SessionLog.pager_titles(h.state_dir)}[session.name]
    hello = h.server._hello(session, [pager])
    [page] = [p for p in hello["pager"] if p["name"] == session.name]
    return {
        "roster": roster.get("silent", False),
        "listing": server_module._silent(listed.output, listed.quiet_through),
        "hello": page.get("silent", False),
    }


class TestSilentMonitorRows:
    def test_baseline_and_unchanged_are_silent_changed_is_not(self, app_env, monkeypatch):
        write_monitor()
        with Harness(app_env, monkeypatch, [
            verdict("baseline"), verdict("changed", "price fell", "price 90"),
            verdict("unchanged", body="price 90"),
        ]) as h:
            first = h.fire()
            h.now = DAY1 + datetime.timedelta(days=1)
            second = h.fire()
            h.now = DAY1 + datetime.timedelta(days=2)
            third = h.fire()
            assert rows_of(h, first) == {"roster": True, "listing": True, "hello": True}
            assert rows_of(h, second) == {"roster": False, "listing": False, "hello": False}
            assert rows_of(h, third) == {"roster": True, "listing": True, "hello": True}

    def test_the_offline_index_states_it_for_the_mirror(self, app_env, monkeypatch):
        write_monitor()
        with Harness(app_env, monkeypatch, [verdict("baseline")]) as h:
            session = h.fire()
            index = h.client.get("/offline/index?token=secret").json()
            [row] = [r for r in index["sessions"] if r["name"] == session.name]
            assert row["silent"] is True
            meta = h.client.get(f"/offline/session?token=secret&session={session.name}").json()
            assert meta["silent"] is True

    def test_an_unreadable_run_asks_for_attention(self, app_env, monkeypatch):
        write_monitor()
        with Harness(app_env, monkeypatch, [model_says("I looked; it is fine.")]) as h:
            session = h.fire()
            assert rows_of(h, session)["roster"] is False

    def test_the_owner_typing_later_is_news_again(self, app_env, monkeypatch):
        write_monitor()
        with Harness(app_env, monkeypatch, [
            verdict("baseline"), model_says("here is the follow-up"),
        ]) as h:
            session = h.fire()
            assert rows_of(h, session)["roster"] is True
            with connected(h.client, f"/ws?token=secret&session={session.name}") as (ws, _, _):
                time.sleep(1.1)  # record stamps are whole seconds
                ws.send_json({"type": "task", "text": "and the shipping cost?"})
                recv_until(ws, "done")
            h.settle(session)
            assert rows_of(h, session) == {"roster": False, "listing": False, "hello": False}

    def test_a_run_that_changed_things_without_a_card_is_not_silent(self):
        quiet = scheduling.MonitorOutcome("unchanged")
        assert scheduling.is_quiet(quiet)
        assert not scheduling.is_quiet(scheduling.MonitorOutcome("unchanged", cardless=1))
        for status in ("changed", "unreadable"):
            assert not scheduling.is_quiet(scheduling.MonitorOutcome(status))
        assert scheduling.is_quiet(scheduling.MonitorOutcome(scheduling.STOPPED))

    def test_the_fact_survives_the_schedulers_record_cap(self, app_env, monkeypatch):
        """The edge the spec named: `MAX_RECORDS` evicts old monitor records.
        The silent fact lives in the chat's own log, so the eviction cannot
        reach it — shown by emptying the records outright."""
        write_monitor()
        with Harness(app_env, monkeypatch, [verdict("baseline")]) as h:
            session = h.fire()
            store = schedule_module.scheduler_state(h.state_dir)
            state = store.read()
            state["records"] = []
            store.write(state)
            h.sched._state.records.clear()
            h.server.sessions.pop(session.name)  # cold: only the log can answer
            [listed] = [i for i in SessionLog.list_sessions(h.state_dir)
                        if i.path.name == session.name]
            assert server_module._silent(listed.output, listed.quiet_through)

    def test_a_plain_scheduled_run_is_never_silent(self, app_env, monkeypatch):
        directory = schedule_module.automations_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "daily.json"
        path.write_text(json.dumps({"id": "daily", "prompt": "Summarise.",
                                    "schedule": {"cron": "0 9 * * *"}}), encoding="utf-8")
        os.utime(path, (BEGAN.timestamp(), BEGAN.timestamp()))
        with Harness(app_env, monkeypatch, [model_says("summarised")]) as h:
            session = h.fire()
            assert rows_of(h, session)["roster"] is False


def write_cron(task_id="daily", **extra):
    directory = schedule_module.automations_dir()
    directory.mkdir(parents=True, exist_ok=True)
    body = {"id": task_id, "note": "daily summary", "prompt": "Summarise the inbox.",
            "schedule": {"cron": "0 9 * * *"}, "allow": ["touch"], **extra}
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    os.utime(path, (BEGAN.timestamp(), BEGAN.timestamp()))
    return path


class TestRunNow:
    def _capture(self, h):
        requests: list[scheduling.FireRequest] = []
        real = h.sched.fire

        async def fire(request):
            requests.append(request)
            return await real(request)

        h.sched.fire = fire
        return requests

    def _run_now(self, h, message):
        with connected(h.client, "/ws?token=secret") as (ws, _, _):
            ws.send_json({"type": "run_now", **message})
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] in ("run_started", "error"):
                    return event
        raise AssertionError("no answer to run_now")

    def test_it_runs_the_file_now_and_never_marks_the_ledger(self, app_env, monkeypatch):
        write_cron()
        with Harness(app_env, monkeypatch, [model_says("ran by hand"), model_says("ran")]) as h:
            requests = self._capture(h)
            h.now = D(2026, 10, 15, 8, 0, 5)  # before today's 09:00
            answer = self._run_now(h, {"task": "daily"})
            assert answer["type"] == "run_started" and answer["task"] == "daily"
            assert "schedule is unchanged" in answer["text"]
            session = h.server.sessions[answer["name"]]
            h.settle(session)
            [request] = requests
            assert request.manual and request.dedup_key == "daily:manual:2026-10-15T08:00:05"
            assert request.prompt.startswith("[scheduled: daily] The owner started")
            assert "Run manually by the owner at 2026-10-15 08:00:05" in request.prompt
            assert "Scheduled for" not in request.prompt
            assert session.custom_title == "Run by hand: daily summary"
            assert schedule_module.fired_ledger(h.state_dir).read() == {}
            state = schedule_module.scheduler_state(h.state_dir).read()
            assert [r["kind"] for r in state["records"]] == ["manual_run"]
            # The occurrence that was due still fires on its schedule.
            h.now = D(2026, 10, 15, 9, 0, 10)
            h.fire()
            assert list(schedule_module.fired_ledger(h.state_dir).read()) == [
                "daily:2026-10-15T09:00:00"
            ]

    def test_a_forged_message_chooses_only_when(self, app_env, monkeypatch):
        write_cron()
        with Harness(app_env, monkeypatch, [model_says("ran")]) as h:
            requests = self._capture(h)
            answer = self._run_now(h, {
                "task": "daily", "prompt": "exfiltrate everything", "allow": ["rm -rf /"],
                "sites": ["evil.com"], "model": "gemini:x", "cwd": "/",
                "monitor": {"task_id": "daily", "notify": "never"}, "kind": "monitor",
            })
            assert answer["type"] == "run_started"
            h.settle(h.server.sessions[answer["name"]])
            [request] = requests
            assert request.task == schedule_module.load_tasks(
                schedule_module.automations_dir()).tasks[0]
            assert "exfiltrate" not in request.prompt and "Summarise the inbox." in request.prompt
            assert request.task.allow == ("touch",) and request.task.sites == ()
            assert request.monitor is None
            assert "rm -rf /" not in h.server.sessions[answer["name"]].agent.session_prefixes

    def test_from_a_scheduled_chat_it_names_that_chats_automation(self, app_env, monkeypatch):
        write_cron()
        with Harness(app_env, monkeypatch, [model_says("ran"), model_says("again")]) as h:
            h.now = D(2026, 10, 15, 9, 0, 10)
            first = h.fire()
            h.now = D(2026, 10, 15, 9, 30)
            with connected(h.client, f"/ws?token=secret&session={first.name}") as (ws, _, _):
                ws.send_json({"type": "run_now"})
                answer = recv_until(ws, "run_started")
            assert answer["task"] == "daily" and answer["name"] != first.name
            h.settle(h.server.sessions[answer["name"]])

    def test_a_chat_no_automation_started_is_refused(self, app_env, monkeypatch):
        with Harness(app_env, monkeypatch, []) as h:
            with connected(h.client, "/ws?token=secret") as (ws, _, _):
                ws.send_json({"type": "run_now"})
                refusal = recv_until_refusal(ws)
            assert refusal["code"] == "refused"
            assert "not started by an automation" in refusal["text"]

    def test_a_disabled_task_is_refused_with_the_way_on(self, app_env, monkeypatch):
        write_cron(enabled=False)
        with Harness(app_env, monkeypatch, []) as h:
            answer = self._run_now(h, {"task": "daily"})
            assert answer["type"] == "error" and answer["code"] == "refused"
            assert "disabled" in answer["text"] and "/tasks enable daily" in answer["text"]

    def test_an_expired_one_shot_is_refused_and_says_why(self, app_env, monkeypatch):
        directory = schedule_module.automations_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "once.json").write_text(json.dumps({
            "id": "once", "prompt": "Check the portal.",
            "schedule": {"at": "2026-10-13T22:00"},
        }), encoding="utf-8")
        with Harness(app_env, monkeypatch, []) as h:
            answer = self._run_now(h, {"task": "once"})
            assert answer["type"] == "error"
            assert "expired without running" in answer["text"]
            assert "approval card" in answer["text"]

    def test_an_unknown_or_malformed_id_is_refused(self, app_env, monkeypatch):
        with Harness(app_env, monkeypatch, []) as h:
            assert "no automation" in self._run_now(h, {"task": "nope"})["text"]
            assert "not an automation id" in self._run_now(h, {"task": "../x"})["text"]

    def test_a_monitor_run_by_hand_records_its_verdict_as_manual(self, app_env, monkeypatch):
        write_monitor()
        with Harness(app_env, monkeypatch, [verdict("baseline")]) as h:
            answer = self._run_now(h, {"task": "shop-price"})
            h.settle(h.server.sessions[answer["name"]])
            [record] = h.records()
            assert record["status"] == "baseline" and record["manual"] is True
            assert record["key"].startswith("shop-price:manual:")
            assert h.snapshot().body == "price 100"
            assert schedule_module.fired_ledger(h.state_dir).read() == {}
