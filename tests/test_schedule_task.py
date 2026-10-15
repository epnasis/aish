"""Creating an automation, and the fence around the folder it lands in (epic #142 W2, S1).

The properties: an automation is created ONLY through `schedule_task`'s diff
card, only in the owner's own chat, and no other write — file tool or shell,
in any origin — reaches the folder. FakeChat scripts the model; nothing here
runs a real model or reaches the network.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_agent import make_agent, model_says, tool_call, tool_messages

from aish import agent as agent_module
from aish import browse, schedule
from aish.agent import Agent, Approved, Denied

AT = (datetime.now() + timedelta(days=1)).replace(hour=22, minute=5, second=0, microsecond=0)


def schedule_args(**over):
    args = {
        "id": "eon-invoices",
        "at": AT.isoformat(timespec="minutes"),
        "note": "E.ON invoices once the portal is back",
        "prompt": "Log in to eon.pl and download the October invoices into ~/Documents.",
    }
    args.update(over)
    return args


def card_agent(answer=True, origin="user"):
    cards = []

    def approve_write(plan):
        cards.append(plan)
        return answer

    agent, _ = make_agent([model_says("ok")], approve_write=approve_write, origin=origin)
    return agent, cards


class TestScheduleTaskCreation:
    def test_it_draws_a_diff_card_that_carries_meaning_and_persists_on_yes(self):
        agent, cards = card_agent()
        result = agent._dispatch("schedule_task", schedule_args(
            allow=["git pull"], sites=["www.eon.pl"],
        ))
        assert "Scheduled 'eon-invoices'" in str(result)
        [plan] = cards
        assert plan.target == schedule.automations_dir() / "eon-invoices.json"
        # The card says what he is agreeing to, in words — prompt untruncated.
        assert "Runs ONCE, at 22:05 tomorrow" in plan.note
        assert schedule_args()["prompt"] in plan.note
        assert "`git pull`" in plan.note and "`eon.pl`" in plan.note
        saved = json.loads(plan.target.read_text(encoding="utf-8"))
        assert saved["schedule"] == {"at": AT.isoformat()}
        assert saved["sites"] == ["eon.pl"] and saved["allow"] == ["git pull"]
        # …and what it wrote is exactly what the scheduler will load.
        assert schedule.load_tasks(schedule.automations_dir()).problems == []

    def test_a_no_writes_nothing(self):
        agent, _ = card_agent(answer=False)
        result = agent._dispatch("schedule_task", schedule_args())
        assert "DENIED" in str(result) or "denied" in str(result)
        assert not (schedule.automations_dir() / "eon-invoices.json").exists()

    def test_a_deny_with_a_comment_stops_and_an_approve_with_one_holds(self):
        agent, _ = card_agent(answer=Denied("not tonight"))
        agent._dispatch("schedule_task", schedule_args())
        assert agent._pending_comment_response
        agent, _ = card_agent(answer=Approved("make it 23:00"))
        result = agent._dispatch("schedule_task", schedule_args())
        assert "make it 23:00" in str(result)
        assert not (schedule.automations_dir() / "eon-invoices.json").exists()

    def test_reusing_an_id_is_an_edit_on_the_same_card(self):
        agent, cards = card_agent()
        agent._dispatch("schedule_task", schedule_args())
        later = (AT + timedelta(hours=1)).isoformat(timespec="minutes")
        agent._dispatch("schedule_task", schedule_args(at=later))
        assert cards[1].is_new is False
        assert cards[1].note.startswith("Replaces automation 'eon-invoices'")

    @pytest.mark.parametrize(
        "over, fragment",
        [
            ({"at": "2020-01-01T09:00"}, "not in the future"),
            ({"at": "tonight"}, "schedule.at"),
            ({"id": "Not An Id"}, "id"),
            ({"allow": ["rm -rf /"]}, "denylist"),
            ({"sites": ["https://eon.pl/x"]}, "bare host"),
        ],
    )
    def test_an_invalid_request_draws_no_card(self, over, fragment):
        agent, cards = card_agent()
        result = agent._dispatch("schedule_task", schedule_args(**over))
        assert str(result).startswith("ERROR") and fragment in str(result)
        assert cards == []

    @pytest.mark.parametrize("origin", ["schedule", "email", "webhook"])
    def test_a_triggered_session_may_never_author_an_automation(self, origin):
        agent, cards = card_agent(origin=origin)
        result = agent._dispatch("schedule_task", schedule_args())
        assert str(result).startswith("NOT EXECUTED: schedule_task works only")
        assert getattr(result, "meta", {}).get("decision") == "blocked"
        assert cards == []  # no card: there is nothing to approve
        assert not schedule.automations_dir().exists()

    def test_the_tool_is_reached_through_the_ordinary_loop(self):
        cards = []
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("schedule_task", **schedule_args())]),
                model_says("Scheduled for 22:05."),
            ],
            approve_write=lambda plan: cards.append(plan) or True,
        )
        assert agent.run_task("check after 10pm") == "Scheduled for 22:05."
        assert len(cards) == 1
        assert "Scheduled 'eon-invoices'" in tool_messages(agent.messages)[0]["content"]


class TestAutomationFence:
    """Unapprovable in EVERY origin, by file tool and by shell — the charter
    fence's two nets pointed at the files that start runs."""

    ORIGINS = ["user", "schedule"]

    def _agent(self, origin):
        asked = []
        agent, _ = make_agent(
            [model_says("ok")], origin=origin,
            approve=lambda cmd: asked.append(cmd) or True,
            approve_write=lambda plan: asked.append(plan) or True,
        )
        return agent, asked

    @pytest.mark.parametrize("origin", ORIGINS)
    @pytest.mark.parametrize("tool", ["write_file", "edit_file"])
    def test_a_file_tool_write_is_blocked(self, origin, tool):
        agent, asked = self._agent(origin)
        target = schedule.automations_dir() / "evil.json"
        args = {"path": str(target), "content": "{}", "old_str": "a", "new_str": "b"}
        result = agent._dispatch(tool, args)
        assert str(result).startswith("NOT EXECUTED") and "automations folder" in str(result)
        assert result.meta["decision"] == "blocked"
        assert asked == [] and not target.exists()

    def _spellings(self, home: Path) -> list[str]:
        store = schedule.automations_dir()
        rel = f"~/{home.name}/automations"
        return [
            f"echo '{{}}' > {store}/evil.json",
            f"cat {store}/eon-invoices.json",
            f"echo x > {rel}/evil.json",
            f"echo x > $HOME/{home.name}/automations/evil.json",
            f"echo x > ${{HOME}}/{home.name}/automations/evil.json",
            f"D={store}; echo x > $D/evil.json",
            f"echo x > {store}/../automations/evil.json",
            f"echo x > {home}/autom*/evil.json",
            "echo x > $AISH_CONFIG_HOME/automations/evil.json",
            f"python3 -c \"open('{store}/evil.json','w')\"",
            f"cp /tmp/x {store}/",
        ]

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_a_shell_write_is_blocked_in_the_known_spellings(self, origin, tmp_path, monkeypatch):
        # Under the real home, so the `~` and `$HOME` spellings name it.
        home = Path.home() / f".aish-test-{tmp_path.name}"
        home.mkdir()
        try:
            monkeypatch.setenv("AISH_CONFIG_HOME", str(home))
            agent, asked = self._agent(origin)
            leaked = [c for c in self._spellings(home)
                      if not agent._command_touches_automations(c)]
            assert not leaked, f"reaches the approver as an ordinary card: {leaked}"
            result = agent._dispatch("run_command", {
                "command": f"echo x > ~/{home.name}/automations/evil.json"
            })
            assert str(result).startswith("NOT EXECUTED") and asked == []
            assert result.meta["decision"] == "blocked"
            assert not (home / "automations").exists()
        finally:
            shutil.rmtree(home)

    def test_a_symlink_into_the_folder_is_the_folder(self, tmp_path):
        agent, asked = self._agent("user")
        store = schedule.automations_dir()
        store.mkdir(parents=True)
        link = Path(agent.cwd) / "shortcut"
        link.symlink_to(store)
        try:
            result = agent._dispatch("write_file", {"path": str(link / "x.json"),
                                                    "content": "{}"})
            assert str(result).startswith("NOT EXECUTED") and asked == []
        finally:
            link.unlink()

    def test_reading_a_task_file_with_read_file_is_not_fenced(self):
        # The fence refuses writes and any shell mention; read_file is the
        # carve-out that keeps a task file readable. Outside the session roots
        # it draws the ordinary read card — asked, never blocked.
        store = schedule.automations_dir()
        store.mkdir(parents=True)
        path = store / "eon-invoices.json"
        path.write_text(schedule.validate_task({
            "id": "eon-invoices", "prompt": "Download the October E.ON invoices.",
            "schedule": {"at": AT.isoformat()},
        }).render(), encoding="utf-8")
        reads = []
        agent, _ = make_agent(
            [model_says("ok")],
            approve_read=lambda p, reason: reads.append((p, reason)) or True,
        )
        result = agent._dispatch("read_file", {"path": str(path)})
        assert "Download the October E.ON invoices." in str(result)
        assert "NOT EXECUTED" not in str(result)
        assert reads and reads[0][1] == "outside"

    def test_the_charter_fence_is_unchanged_by_the_shared_nets(self):
        agent, _ = self._agent("user")
        charters = agent_module.roles.CHARTERS_DIR
        assert agent._command_touches_a_charter(f"echo x > {charters}/x.md")
        assert not agent._command_touches_a_charter(
            f"echo x > {schedule.automations_dir()}/x.json"
        )


class TestTheRunsSiteGrants:
    """`sites` seeds the press-grant set, so on a profile site the per-site
    card is already answered while another site still asks (epic #142 W2)."""

    def _agent(self, url):
        asked = []

        def approve_tool(name, args, preview):
            asked.append(preview)
            return True

        agent = Agent(model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
                      approve_tool=approve_tool, origin="schedule")
        controls = browse.controls_from([
            {"n": 1, "kind": browse.BUTTON, "name": "Dalej", "submits": True,
             "sends_to": url},
        ])
        agent._browse_view.remember(
            browse.Snapshot(url=url, title="", text="Wyspowa", controls=controls)
        )
        agent.restore_site_grants(["eon.pl"])  # what open_session's profile does
        return agent, asked

    def test_a_press_on_a_profile_site_draws_no_site_card(self):
        agent, asked = self._agent("https://eon.pl/mojeon")
        assert agent._browse_gate("browse_act", {"target": "Dalej"}) is None
        assert asked == []

    def test_a_press_on_any_other_site_still_asks(self):
        agent, asked = self._agent("https://other.example/account")
        agent._browse_gate("browse_act", {"target": "Dalej"})
        assert len(asked) == 1
