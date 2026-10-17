"""Who carries the owner's browser UA (#360 follow-up, `aish/owner_ua.py`).

The rule: a request the owner made — or delegated ONCE, at a moment — carries
his User-Agent from that moment; recurring automations, monitors and
email/webhook triggers are background and carry none; the terminal reuses the
last browser UA aish-web saw. One test class per row of that table, each with
its positive and its negative. No model, no network: FakeChat scripts the
model and the scheduler is ticked by hand (test_server_monitor's Harness).
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_agent import make_agent
from test_agent import model_says as agent_says
from test_schedule_task import card_agent, schedule_args
from test_server import FakeChat, connected, make_client, model_says, recv_until
from test_server_monitor import BEGAN, Harness

from aish import automation_admin, owner_ua, schedule
from aish import scheduler as scheduling
from aish.agent import Agent
from aish.session import SessionLog

KEY = owner_ua.ENV_KEY
BROWSER_A = "Mozilla/5.0 (Macintosh; owner laptop)"
BROWSER_B = "Mozilla/5.0 (iPhone; owner phone)"


@pytest.fixture
def app_env(tmp_path):
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


def task_starts(path: Path) -> list[dict]:
    return [r for r in map(json.loads, path.read_text().splitlines())
            if r.get("kind") == "task_start"]


def wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── the module ───────────────────────────────────────────────────────────────


class TestTheStore:
    def test_clean_strips_caps_and_drops_control_characters(self):
        assert owner_ua.clean("  Mozilla/5.0  ") == "Mozilla/5.0"
        assert len(owner_ua.clean("x" * 2000)) == owner_ua.MAX_UA_CHARS
        assert owner_ua.clean("a\r\nX-Injected: 1") == "aX-Injected: 1"
        assert owner_ua.clean("") == ""

    def test_remember_then_last_round_trips_and_writes_only_on_change(self, tmp_path):
        assert owner_ua.last(tmp_path) is None
        assert owner_ua.remember(tmp_path, BROWSER_A) is True
        assert owner_ua.last(tmp_path) == BROWSER_A
        assert owner_ua.remember(tmp_path, BROWSER_A) is False
        assert owner_ua.remember(tmp_path, "") is False
        assert owner_ua.last(tmp_path) == BROWSER_A

    def test_last_never_raises(self, tmp_path):
        (tmp_path / owner_ua.FILENAME).mkdir()
        assert owner_ua.last(tmp_path) is None
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / owner_ua.FILENAME).write_text("  \n", encoding="utf-8")
        assert owner_ua.last(empty) is None


class TestTaskStartRecord:
    def test_the_shape_without_a_ua_is_unchanged(self, tmp_path):
        log = SessionLog.new(tmp_path)
        log.task_start("go")
        log.close()
        [record] = task_starts(log.path)
        assert set(record) == {"ts", "kind", "prompt"}
        assert SessionLog.pending_task(log.path)["psu_ua"] == ""

    def test_a_ua_rides_the_start_and_the_scan_reads_it(self, tmp_path):
        log = SessionLog.new(tmp_path)
        log.task_start("go", psu_ua=BROWSER_A)
        log.close()
        [record] = task_starts(log.path)
        assert record["psu_ua"] == BROWSER_A
        assert SessionLog.pending_task(log.path)["psu_ua"] == BROWSER_A
        assert SessionLog.last_task(log.path)["psu_ua"] == BROWSER_A


# ── the web: typed, Retry ────────────────────────────────────────────────────


class TestTypedMessage:
    def test_a_typed_message_stamps_records_and_remembers(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        with client, client.websocket_connect("/ws", headers={"user-agent": BROWSER_A}) as ws:
            hello = ws.receive_json()
            ws.receive_json()
            ws.send_json({"type": "task", "text": "hi"})
            recv_until(ws, "done")
            session = client.app.state.server.sessions[hello["session"]]
            assert session.agent.plugin_env[KEY] == BROWSER_A
            [start] = task_starts(session.logref.log.path)
            assert start["psu_ua"] == BROWSER_A
        assert owner_ua.last(app_env["state_dir"]) == BROWSER_A

    def test_a_socket_without_a_ua_stamps_nothing(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        with client, client.websocket_connect("/ws", headers={"user-agent": ""}) as ws:
            hello = ws.receive_json()
            ws.receive_json()
            ws.send_json({"type": "task", "text": "hi"})
            recv_until(ws, "done")
            session = client.app.state.server.sessions[hello["session"]]
            assert KEY not in session.agent.plugin_env
            [start] = task_starts(session.logref.log.path)
            assert "psu_ua" not in start
        assert owner_ua.last(app_env["state_dir"]) is None


class TestRetry:
    def test_retry_carries_the_pressing_browser(self, app_env):
        client, _ = make_client(app_env, [model_says("first"), model_says("second")])
        with client:
            with client.websocket_connect("/ws", headers={"user-agent": BROWSER_A}) as ws:
                hello = ws.receive_json()
                ws.receive_json()
                ws.send_json({"type": "task", "text": "what is 2+2?"})
                recv_until(ws, "done")
            name = hello["session"]
            with client.websocket_connect(
                f"/ws?session={name}", headers={"user-agent": BROWSER_B}
            ) as ws:
                ws.receive_json()
                ws.receive_json()
                ws.send_json({"type": "retry", "text": "what is 2+2?"})
                recv_until(ws, "done")
            session = client.app.state.server.sessions[name]
            assert session.agent.plugin_env[KEY] == BROWSER_B
            starts = task_starts(session.logref.log.path)
            assert starts[-1]["psu_ua"] == BROWSER_B
        assert owner_ua.last(app_env["state_dir"]) == BROWSER_B

    def test_retry_from_a_socket_without_a_ua_keeps_the_latest_stamp(self, app_env):
        client, _ = make_client(app_env, [model_says("first"), model_says("second")])
        with client:
            with client.websocket_connect("/ws", headers={"user-agent": BROWSER_A}) as ws:
                hello = ws.receive_json()
                ws.receive_json()
                ws.send_json({"type": "task", "text": "q"})
                recv_until(ws, "done")
            name = hello["session"]
            with client.websocket_connect(
                f"/ws?session={name}", headers={"user-agent": ""}
            ) as ws:
                ws.receive_json()
                ws.receive_json()
                ws.send_json({"type": "retry", "text": "q"})
                recv_until(ws, "done")
            session = client.app.state.server.sessions[name]
            assert session.agent.plugin_env[KEY] == BROWSER_A
        assert owner_ua.last(app_env["state_dir"]) == BROWSER_A

    def test_retry_stamps_before_the_busy_check(self, app_env):
        """A press while the turn is still running defers the rerun
        (`pending_retry`); the stamp must already be on the agent by then."""
        client, _ = make_client(app_env, [])
        with client:
            server = client.app.state.server
            with connected(client) as (_ws, hello, _):
                session = server.sessions[hello["session"]]
            session.busy = True
            stops: list[str] = []

            async def stop(_client):
                stops.append("stop")

            server._stop_task = stop
            fake = type("C", (), {})()
            fake.viewing = session
            fake.ws = type("W", (), {"headers": {"user-agent": BROWSER_B}})()
            client.portal.call(server._retry_task, fake, "q")
            assert session.pending_retry == "q" and stops == ["stop"]
            assert session.agent.plugin_env[KEY] == BROWSER_B
            session.busy = False
            session.pending_retry = None


# ── restart recovery ─────────────────────────────────────────────────────────


class TestResume:
    @staticmethod
    def _interrupted(app_env, *, psu_ua=None, with_history=True):
        state_dir = Path(app_env["state_dir"])
        log = SessionLog(state_dir / "session-20260101-120000-000000.jsonl")
        log.model("fake")
        log.task_start("answer it", psu_ua=psu_ua)
        if with_history:
            log.message({"role": "user", "content": "answer it"})
        log.close()
        return log.path

    @pytest.mark.parametrize("with_history", [True, False], ids=["continue", "reissue"])
    def test_a_resumed_turn_carries_the_ua_it_recorded(self, app_env, with_history):
        path = self._interrupted(app_env, psu_ua=BROWSER_A, with_history=with_history)
        client, _ = make_client(app_env, [model_says("done")])
        with client:
            server = client.app.state.server
            assert wait(lambda: path.name in server.sessions)
            session = server.sessions[path.name]
            assert wait(lambda: not session.busy)
            assert session.agent.plugin_env[KEY] == BROWSER_A
        # …and the continuation's own start re-records it for a second restart.
        assert task_starts(path)[-1]["psu_ua"] == BROWSER_A

    @pytest.mark.parametrize("with_history", [True, False], ids=["continue", "reissue"])
    def test_a_turn_that_recorded_none_resumes_with_none(self, app_env, with_history):
        path = self._interrupted(app_env, with_history=with_history)
        client, _ = make_client(app_env, [model_says("done")])
        with client:
            server = client.app.state.server
            assert wait(lambda: path.name in server.sessions)
            session = server.sessions[path.name]
            assert wait(lambda: not session.busy)
            assert KEY not in session.agent.plugin_env
        assert "psu_ua" not in task_starts(path)[-1]


# ── automations ──────────────────────────────────────────────────────────────


class TestOneShotCarriesTheWritersUA:
    def test_the_card_shows_it_and_the_file_holds_it(self):
        agent, cards = card_agent()
        agent.plugin_env[KEY] = BROWSER_A
        agent._dispatch("schedule_task", schedule_args())
        [plan] = cards
        assert f'"psu_ua": "{BROWSER_A}"' in plan.diff
        saved = json.loads(plan.target.read_text(encoding="utf-8"))
        assert saved["psu_ua"] == BROWSER_A
        [task] = schedule.load_tasks(schedule.automations_dir()).tasks
        assert task.psu_ua == BROWSER_A

    def test_the_card_says_in_words_that_the_bank_will_be_told(self):
        import dataclasses
        from datetime import datetime
        agent, cards = card_agent()
        agent.plugin_env[KEY] = BROWSER_A
        agent._dispatch("schedule_task", schedule_args())
        [task] = schedule.load_tasks(schedule.automations_dir()).tasks
        words = schedule.describe(task, datetime(2026, 1, 1))
        assert "say you are present" in words
        unstamped = dataclasses.replace(task, psu_ua=None)
        assert "say you are present" not in schedule.describe(unstamped, datetime(2026, 1, 1))

    def test_a_writer_with_no_ua_writes_none(self):
        agent, cards = card_agent()
        agent._dispatch("schedule_task", schedule_args())
        [plan] = cards
        assert "psu_ua" not in plan.diff
        assert "psu_ua" not in json.loads(plan.target.read_text(encoding="utf-8"))

    def test_a_cron_task_never_gets_one(self):
        agent, cards = card_agent()
        agent.plugin_env[KEY] = BROWSER_A
        args = schedule_args(cron="0 9 * * *")
        del args["at"]
        result = agent._dispatch("schedule_task", args)
        assert not str(result).startswith("ERROR"), result
        [plan] = cards
        assert "psu_ua" not in json.loads(plan.target.read_text(encoding="utf-8"))


class TestValidation:
    def _raw(self, **over):
        raw = {"id": "once", "prompt": "do it", "schedule": {"at": "2026-12-01T09:00"}}
        raw.update(over)
        return raw

    def test_a_one_shot_may_carry_one(self):
        assert schedule.validate_task(self._raw(psu_ua=BROWSER_A)).psu_ua == BROWSER_A

    def test_a_cron_task_carrying_one_is_refused(self):
        with pytest.raises(schedule.InvalidTask, match="one-shot only"):
            schedule.validate_task(self._raw(schedule={"cron": "0 9 * * *"}, psu_ua=BROWSER_A))

    @pytest.mark.parametrize("bad", ["x" * 513, "a\r\nb", 7])
    def test_an_oversized_control_or_non_string_value_is_refused(self, bad):
        with pytest.raises(schedule.InvalidTask, match="psu_ua"):
            schedule.validate_task(self._raw(psu_ua=bad))

    def test_to_dict_omits_it_when_empty(self):
        assert "psu_ua" not in schedule.validate_task(self._raw()).to_dict()


class TestToggleRoundTrip:
    def test_enable_disable_keeps_the_ua(self):
        directory = schedule.automations_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "once.json"
        at = (datetime.now() + timedelta(days=1)).replace(microsecond=0)
        path.write_text(json.dumps({
            "id": "once", "prompt": "do it", "schedule": {"at": at.isoformat()},
            "psu_ua": BROWSER_A,
        }), encoding="utf-8")
        now = datetime.now()
        _task, wrote = automation_admin.set_enabled("once", False, deny_prefixes=(), now=now)
        assert wrote
        assert json.loads(path.read_text(encoding="utf-8"))["psu_ua"] == BROWSER_A
        automation_admin.set_enabled("once", True, deny_prefixes=(), now=now)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["psu_ua"] == BROWSER_A and saved["enabled"] is True


def write_task(task_id, schedule_value, **extra):
    directory = schedule.automations_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps({
        "id": task_id, "note": task_id, "prompt": "Do the thing.",
        "schedule": schedule_value, **extra,
    }), encoding="utf-8")
    os.utime(path, (BEGAN.timestamp(), BEGAN.timestamp()))
    return path


class TestScheduledFire:
    def test_a_one_shot_fire_carries_its_ua(self, app_env, monkeypatch):
        write_task("once", {"at": "2026-10-15T09:00"}, psu_ua=BROWSER_A)
        with Harness(app_env, monkeypatch, [model_says("done")]) as h:
            session = h.fire()
            assert session.agent.plugin_env[KEY] == BROWSER_A
            assert task_starts(session.logref.log.path)[0]["psu_ua"] == BROWSER_A

    def test_a_one_shot_without_one_carries_none(self, app_env, monkeypatch):
        write_task("once", {"at": "2026-10-15T09:00"})
        with Harness(app_env, monkeypatch, [model_says("done")]) as h:
            session = h.fire()
            assert KEY not in session.agent.plugin_env

    def test_a_cron_fire_carries_none(self, app_env, monkeypatch):
        write_task("daily", {"cron": "0 9 * * *"})
        with Harness(app_env, monkeypatch, [model_says("done")]) as h:
            session = h.fire()
            assert KEY not in session.agent.plugin_env
            assert "psu_ua" not in task_starts(session.logref.log.path)[0]

    def test_a_cron_file_carrying_one_never_loads(self, app_env, monkeypatch):
        write_task("daily", {"cron": "0 9 * * *"}, psu_ua=BROWSER_A)
        loaded = schedule.load_tasks(schedule.automations_dir())
        assert loaded.tasks == [] and "one-shot only" in loaded.problems[0].error


class TestRunNow:
    def _run_now(self, h, ua):
        with h.client.websocket_connect(
            "/ws?token=secret", headers={"user-agent": ua}
        ) as ws:
            ws.receive_json()
            ws.receive_json()
            ws.send_json({"type": "run_now", "task": "daily"})
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] in ("run_started", "error"):
                    return event
        raise AssertionError("no answer to run_now")

    def test_it_carries_the_pressing_browser_and_never_writes_it(self, app_env, monkeypatch):
        path = write_task("daily", {"cron": "0 9 * * *"})
        before = path.read_bytes()
        with Harness(app_env, monkeypatch, [model_says("ran")]) as h:
            requests: list[scheduling.FireRequest] = []
            real = h.sched.fire

            async def fire(request):
                requests.append(request)
                return await real(request)

            h.sched.fire = fire
            answer = self._run_now(h, BROWSER_B)
            assert answer["type"] == "run_started"
            session = h.server.sessions[answer["name"]]
            h.settle(session)
            [request] = requests
            assert request.psu_ua == BROWSER_B
            assert session.agent.plugin_env[KEY] == BROWSER_B
        assert path.read_bytes() == before

    def test_a_press_with_no_ua_carries_none(self, app_env, monkeypatch):
        write_task("daily", {"cron": "0 9 * * *"})
        with Harness(app_env, monkeypatch, [model_says("ran")]) as h:
            answer = self._run_now(h, "")
            session = h.server.sessions[answer["name"]]
            h.settle(session)
            assert KEY not in session.agent.plugin_env


# ── triggers ─────────────────────────────────────────────────────────────────


class TestTriggers:
    @pytest.mark.parametrize("origin", ["email", "webhook"])
    def test_meta_can_never_claim_the_owner(self, app_env, origin):
        client, _ = make_client(app_env, [model_says("triaged")], token="secret")
        with client:
            r = client.post(
                "/trigger?token=secret",
                json={"prompt": "new mail", "origin": origin,
                      "meta": {"psu_ua": BROWSER_A, KEY: BROWSER_A}},
                headers={"user-agent": BROWSER_A},
            )
            session = client.app.state.server.sessions[r.json()["session"]]
            assert wait(lambda: not session.busy)
            assert KEY not in session.agent.plugin_env
            assert "psu_ua" not in task_starts(session.logref.log.path)[0]


# ── the terminal ─────────────────────────────────────────────────────────────


class _Tty(io.StringIO):
    def isatty(self):
        return True


class TestTerminal:
    def launch(self, monkeypatch, tmp_path, *, tty, remembered, argv=("do it",)):
        from aish import backends, cli

        state, work = tmp_path / "state", tmp_path / "work"
        for path in (state, work):
            path.mkdir(exist_ok=True)
        if remembered:
            owner_ua.remember(state, remembered)
        monkeypatch.chdir(work)
        monkeypatch.setenv("AISH_STATE_DIR", str(state))
        monkeypatch.setenv("AISH_CONFIG", str(tmp_path / "absent.toml"))
        monkeypatch.setenv("AISH_ALLOWLIST", str(tmp_path / "allow.txt"))
        monkeypatch.setenv("AISH_DENYLIST", str(tmp_path / "deny.txt"))
        monkeypatch.setenv("AISH_LESSONS", str(tmp_path / "lessons.md"))
        monkeypatch.delenv("AISH_MODEL", raising=False)
        monkeypatch.setattr(cli, "_box", None)
        monkeypatch.setattr(sys, "stdin", _Tty() if tty else io.StringIO())
        chat = FakeChat([model_says("done")])
        monkeypatch.setattr(backends, "make_chat", lambda spec: (chat, "ollama", "fake"))
        seen: list[dict] = []
        real = Agent.run_task

        def run_task(self, *a, **k):
            seen.append(dict(self.plugin_env))
            return real(self, *a, **k)

        monkeypatch.setattr(Agent, "run_task", run_task)
        monkeypatch.setattr(sys, "argv", ["aish", *argv])
        assert cli.main() == 0
        return seen

    def test_a_tty_task_carries_the_last_browser(self, monkeypatch, tmp_path):
        assert self.launch(monkeypatch, tmp_path, tty=True, remembered=BROWSER_A) == [
            {KEY: BROWSER_A}
        ]

    def test_a_piped_task_carries_none(self, monkeypatch, tmp_path):
        assert self.launch(monkeypatch, tmp_path, tty=False, remembered=BROWSER_A) == [{}]

    def test_a_tty_with_no_browser_ever_seen_carries_none(self, monkeypatch, tmp_path):
        assert self.launch(monkeypatch, tmp_path, tty=True, remembered=None) == [{}]

    def test_the_stamp_is_read_per_task_and_cleared_when_absent(self, monkeypatch, tmp_path):
        from aish import cli

        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        agent, _ = make_agent([agent_says("ok")])
        cli.stamp_terminal_owner_ua(agent, attended=True)
        assert KEY not in agent.plugin_env
        owner_ua.remember(tmp_path, BROWSER_A)
        cli.stamp_terminal_owner_ua(agent, attended=True)
        assert agent.plugin_env[KEY] == BROWSER_A
        owner_ua.remember(tmp_path, BROWSER_B)
        cli.stamp_terminal_owner_ua(agent, attended=True)
        assert agent.plugin_env[KEY] == BROWSER_B
        cli.stamp_terminal_owner_ua(agent, attended=False)
        assert KEY not in agent.plugin_env


class TestClaudeMaxForwardsThePluginEnv:
    def test_the_stamp_lands_on_the_inner_agent(self, monkeypatch, tmp_path):
        from test_claude_max import make_max_agent

        agent, _ = make_max_agent(monkeypatch, tmp_path)
        agent.plugin_env[KEY] = BROWSER_A
        assert agent.inner.plugin_env[KEY] == BROWSER_A
