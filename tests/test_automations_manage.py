"""Reading, changing, pausing and deleting an existing automation (#459).

The session that filed it: asked to change one automation's cron, the model
had no way to read it — it grepped the disk with the shell for two minutes,
then re-sent the whole task, which (as the replace was built) also re-enabled
a paused one and added a field nobody asked for. The properties pinned here:
the model READS automations through one tool, in the owner's chat only; an
edit changes ONLY what it names; pausing and deleting come on a card and are
refused once outside content entered the task; and a card left open never
overwrites a change made while it was open. FakeChat scripts nothing a real
model would; no network, no real command.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from test_agent import make_agent, model_says

from aish import automation_admin, files, owner_ua, schedule, tool_menu, tools
from aish.cli import make_write_approver

AT = (datetime.now() + timedelta(days=1)).replace(hour=22, minute=5, second=0, microsecond=0)
UNTIL = (datetime.now() + timedelta(days=21)).replace(hour=23, minute=59, second=0, microsecond=0)
PROMPT = "Check gov.pl for strikes affecting the trip; email if anything changed."


def write_task(task_id="paris-monitor", **over):
    raw = {
        "id": task_id,
        "note": "Daily Paris strike check",
        "prompt": PROMPT,
        "schedule": {"cron": "0 9 * * *"},
        "enabled": True,
        "created": "2026-10-20T22:03:39",
        "sites": ["gov.pl"],
        "allow": ["git pull"],
    }
    raw.update(over)
    path = schedule.automations_dir() / f"{task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return path


def agent_with_card(tmp_path, answer=True, origin="user", on_card=None):
    cards = []

    def approve_write(plan):
        cards.append(plan)
        if on_card is not None:
            on_card(plan)
        return answer

    agent, _ = make_agent(
        [model_says("ok")], approve_write=approve_write, origin=origin, state_dir=tmp_path,
    )
    return agent, cards


def saved(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestReadingThem:
    def test_with_none_it_says_so(self, tmp_path):
        agent, _ = agent_with_card(tmp_path)
        assert agent._dispatch("automations", {}) == "The user has no automations."

    def test_the_list_is_the_terminals_listing(self, tmp_path):
        write_task()
        write_task("eon-invoices", enabled=False, schedule={"at": AT.isoformat()})
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch("automations", {}))
        listing = automation_admin.read_listing(tmp_path, deny_prefixes=(), now=datetime.now())
        for line in automation_admin.task_lines(listing, datetime.now()):
            assert line.split("\n")[0] in out
        assert "paris-monitor" in out and "DISABLED" in out
        assert cards == []  # reading draws no card

    def test_one_carries_its_whole_stored_file(self, tmp_path):
        path = write_task()
        agent, _ = agent_with_card(tmp_path)
        out = str(agent._dispatch("automations", {"id": "paris-monitor"}))
        task = schedule.validate_task(saved(path))
        assert task.render() in out
        assert "ONLY the fields that change" in out

    def test_a_problem_file_is_named_as_one(self, tmp_path):
        (schedule.automations_dir()).mkdir(parents=True, exist_ok=True)
        (schedule.automations_dir() / "broken.json").write_text("{nope", encoding="utf-8")
        agent, _ = agent_with_card(tmp_path)
        out = str(agent._dispatch("automations", {"id": "broken"}))
        assert "not a runnable automation" in out and "delete=true" in out

    def test_an_unknown_id_lists_the_real_ones(self, tmp_path):
        write_task()
        agent, _ = agent_with_card(tmp_path)
        out = str(agent._dispatch("automations", {"id": "paris"}))
        assert out.startswith("ERROR") and "paris-monitor" in out

    @pytest.mark.parametrize("origin", ["schedule", "trigger", "email"])
    def test_a_triggered_run_cannot_read_them(self, tmp_path, origin):
        write_task()
        agent, _ = agent_with_card(tmp_path, origin=origin)
        out = str(agent._dispatch("automations", {}))
        assert out.startswith("NOT EXECUTED") and PROMPT not in out

    def test_it_is_on_every_menu_and_declared(self):
        assert "automations" in tool_menu.CORE_TOOLS
        names = [t["function"]["name"] for t in tools.TOOL_SCHEMAS]
        assert "automations" in names


class TestAnEditChangesOnlyWhatItNames:
    def test_the_filing_session_cron_change(self, tmp_path):
        path = write_task()
        before = saved(path)
        agent, cards = agent_with_card(tmp_path)
        result = str(agent._dispatch(
            "schedule_task", {"id": "paris-monitor", "cron": "0 9,20 * * *"}
        ))
        after = saved(path)
        assert after["schedule"] == {"cron": "0 9,20 * * *"}
        assert {k: v for k, v in after.items() if k != "schedule"} == {
            k: v for k, v in before.items() if k != "schedule"
        }
        [plan] = cards
        assert plan.note.startswith("Changes:\n  · cron: `0 9 * * *` → `0 9,20 * * *`\n\n")
        assert plan.note.count("  · ") == 1  # nothing else changes
        assert result.startswith("Changed 'paris-monitor': cron")
        assert "counts from now" in result

    def test_a_paused_one_stays_paused_and_its_card_says_so(self, tmp_path):
        path = write_task(enabled=False)
        agent, cards = agent_with_card(tmp_path)
        agent._dispatch("schedule_task", {"id": "paris-monitor", "note": "Twice daily"})
        assert saved(path)["enabled"] is False
        assert "It is PAUSED" in cards[0].note

    def test_a_full_resend_keeps_paused_too(self, tmp_path):
        # The filing session's shape — every field re-sent — used to rebuild
        # the task with enabled: true.
        path = write_task(enabled=False)
        agent, _ = agent_with_card(tmp_path)
        agent._dispatch("schedule_task", {
            "id": "paris-monitor", "cron": "0 9,20 * * *", "prompt": PROMPT,
            "note": "Daily Paris strike check", "sites": ["gov.pl"], "allow": ["git pull"],
        })
        assert saved(path)["enabled"] is False
        assert saved(path)["created"] == "2026-10-20T22:03:39"

    def test_clear_drops_a_field_and_set_plus_clear_is_refused(self, tmp_path):
        path = write_task(schedule={"cron": "0 9 * * *", "until": UNTIL.isoformat()})
        agent, cards = agent_with_card(tmp_path)
        agent._dispatch("schedule_task", {"id": "paris-monitor", "clear": ["until", "sites"]})
        assert saved(path)["schedule"] == {"cron": "0 9 * * *"} and "sites" not in saved(path)
        out = str(agent._dispatch(
            "schedule_task", {"id": "paris-monitor", "allow": ["ls"], "clear": ["allow"]}
        ))
        assert out.startswith("ERROR") and "both set and cleared" in out
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "clear": ["prompt"]}))
        assert out.startswith("ERROR") and "clear cannot drop prompt" in out
        assert len(cards) == 1

    def test_turning_recurring_into_once_never_silently_drops_its_end(self, tmp_path):
        path = write_task(schedule={"cron": "0 9 * * *", "until": UNTIL.isoformat()})
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch(
            "schedule_task", {"id": "paris-monitor", "at": AT.isoformat(timespec="minutes")}
        ))
        assert out.startswith("ERROR") and "until" in out and cards == []
        agent._dispatch("schedule_task", {
            "id": "paris-monitor", "at": AT.isoformat(timespec="minutes"), "clear": ["until"],
        })
        assert saved(path)["schedule"] == {"at": AT.isoformat()}

    def test_an_edit_never_mints_owner_presence(self, tmp_path):
        path = write_task()
        agent, _ = agent_with_card(tmp_path)
        agent.plugin_env[owner_ua.ENV_KEY] = "Mozilla/5.0 test"
        agent._dispatch(
            "schedule_task", {"id": "paris-monitor", "at": AT.isoformat(timespec="minutes")}
        )
        assert "psu_ua" not in saved(path)
        # The same env DOES stamp a one-shot created fresh — so the line above
        # is the edit path declining, not the key being wrong.
        agent._dispatch("schedule_task", {
            "id": "fresh-once", "at": AT.isoformat(timespec="minutes"), "prompt": PROMPT,
        })
        assert saved(schedule.automations_dir() / "fresh-once.json")["psu_ua"] == "Mozilla/5.0 test"

    def test_a_kind_change_takes_the_new_kinds_notify(self, tmp_path):
        path = write_task(kind="monitor", notify="on_change", sites=[], allow=[])
        agent, _ = agent_with_card(tmp_path)
        agent._dispatch("schedule_task", {"id": "paris-monitor", "kind": "task"})
        assert "kind" not in saved(path) and "notify" not in saved(path)

    def test_nothing_to_change_draws_no_card(self, tmp_path):
        write_task()
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "cron": "0 9 * * *"}))
        assert out.startswith("Nothing to change") and cards == []

    def test_a_problem_file_takes_a_whole_task_or_nothing(self, tmp_path):
        schedule.automations_dir().mkdir(parents=True, exist_ok=True)
        broken = schedule.automations_dir() / "paris-monitor.json"
        broken.write_text("{nope", encoding="utf-8")
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "cron": "0 9 * * *"}))
        assert out.startswith("ERROR") and "not a runnable automation" in out and cards == []
        agent._dispatch("schedule_task", {
            "id": "paris-monitor", "cron": "0 9 * * *", "prompt": PROMPT, "note": "n",
        })
        assert saved(broken)["prompt"] == PROMPT

    def test_clear_on_a_new_id_is_refused(self, tmp_path):
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch("schedule_task", {
            "id": "fresh", "cron": "0 9 * * *", "prompt": PROMPT, "clear": ["until"],
        }))
        assert out.startswith("ERROR") and cards == []


class TestPausing:
    @pytest.mark.parametrize("value", [False, "false", "False"])
    def test_enabled_false_pauses_on_a_card(self, tmp_path, value):
        path = write_task()
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "enabled": value}))
        assert saved(path)["enabled"] is False
        assert "resumed → PAUSED" in cards[0].note and "It is PAUSED" in cards[0].note
        assert "nothing runs until it is resumed" in out

    def test_a_flag_that_is_not_a_boolean_is_refused(self, tmp_path):
        path = write_task()
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "enabled": "no"}))
        assert out.startswith("ERROR") and cards == [] and saved(path)["enabled"] is True

    def test_resuming_past_its_end_is_refused_by_the_terminals_rule(self, tmp_path):
        past = (datetime.now() - timedelta(days=3)).replace(second=0, microsecond=0)
        write_task(enabled=False, schedule={"cron": "0 9 * * *", "until": past.isoformat()})
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "enabled": True}))
        assert "has ended" in out and cards == []

    def test_outside_content_cannot_pause_one(self, tmp_path):
        path = write_task()
        agent, cards = agent_with_card(tmp_path)
        agent._tainted = True
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "enabled": False}))
        assert out.startswith("NOT EXECUTED") and "/tasks" in out
        assert cards == [] and saved(path)["enabled"] is True

    def test_outside_content_may_still_resume_one_on_its_card(self, tmp_path):
        path = write_task(enabled=False)
        agent, cards = agent_with_card(tmp_path)
        agent._tainted = True
        agent._dispatch("schedule_task", {"id": "paris-monitor", "enabled": True})
        assert len(cards) == 1 and saved(path)["enabled"] is True


class TestDeleting:
    def test_it_deletes_on_a_card_that_says_delete(self, tmp_path):
        path = write_task()
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "delete": True}))
        [plan] = cards
        assert plan.remove is True and plan.new == ""
        assert plan.note.startswith("Deletes automation 'paris-monitor'")
        assert "no way back" in plan.note
        assert not path.exists() and out.startswith("Deleted automation 'paris-monitor'")

    def test_a_no_keeps_it(self, tmp_path):
        path = write_task()
        agent, _ = agent_with_card(tmp_path, answer=False)
        agent._dispatch("schedule_task", {"id": "paris-monitor", "delete": "true"})
        assert path.exists()

    def test_delete_takes_nothing_else(self, tmp_path):
        path = write_task()
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch(
            "schedule_task", {"id": "paris-monitor", "delete": True, "cron": "0 8 * * *"}
        ))
        assert out.startswith("ERROR") and "cron" in out and cards == [] and path.exists()

    def test_outside_content_cannot_delete_one(self, tmp_path):
        path = write_task()
        agent, cards = agent_with_card(tmp_path)
        agent._tainted = True
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "delete": True}))
        assert out.startswith("NOT EXECUTED") and cards == [] and path.exists()

    def test_an_unknown_id_is_an_error_without_a_card(self, tmp_path):
        agent, cards = agent_with_card(tmp_path)
        out = str(agent._dispatch("schedule_task", {"id": "nothing-here", "delete": True}))
        assert out.startswith("ERROR") and cards == []

    def test_a_problem_file_may_be_deleted(self, tmp_path):
        schedule.automations_dir().mkdir(parents=True, exist_ok=True)
        broken = schedule.automations_dir() / "broken.json"
        broken.write_text("{nope", encoding="utf-8")
        agent, cards = agent_with_card(tmp_path)
        agent._dispatch("schedule_task", {"id": "broken", "delete": True})
        assert "not a runnable automation" in cards[0].note and not broken.exists()

    @pytest.mark.parametrize("origin", ["schedule", "trigger"])
    def test_a_triggered_run_cannot_delete_one(self, tmp_path, origin):
        path = write_task()
        agent, cards = agent_with_card(tmp_path, origin=origin)
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "delete": True}))
        assert out.startswith("NOT EXECUTED") and cards == [] and path.exists()

    def test_commit_is_no_general_unlinker(self, tmp_path):
        target = tmp_path / "keep.txt"
        target.write_text("x", encoding="utf-8")
        plan = files.WritePlan(target, str(target), "x", "", False, remove=True)
        assert files.commit(plan).startswith("ERROR") and target.exists()

    def test_the_terminal_card_says_delete(self, tmp_path, monkeypatch, capsys):
        path = write_task()
        plan = files.WritePlan(path, str(path), path.read_text(), "", False, remove=True,
                               note="Deletes automation 'paris-monitor'.")
        monkeypatch.setattr("builtins.input", lambda *_: "n")
        make_write_approver(None)(plan)
        assert "delete file?" in capsys.readouterr().out


class TestACardLeftOpen:
    """The filing session's read card stayed open 346 s. The owner may change
    the same automation meanwhile (/tasks disable, the web sheet, another
    chat); the approved bytes must not silently undo that."""

    def test_a_pause_made_while_an_edit_card_is_open_survives_it(self, tmp_path):
        path = write_task()

        def owner_pauses_meanwhile(_plan):
            automation_admin.set_enabled(
                "paris-monitor", False, deny_prefixes=(), now=datetime.now()
            )

        agent, _ = agent_with_card(tmp_path, on_card=owner_pauses_meanwhile)
        out = str(agent._dispatch(
            "schedule_task", {"id": "paris-monitor", "cron": "0 9,20 * * *"}
        ))
        assert out.startswith("NOT WRITTEN") and "id=paris-monitor" in out
        assert saved(path)["enabled"] is False
        assert saved(path)["schedule"] == {"cron": "0 9 * * *"}

    def test_a_delete_card_does_not_delete_a_changed_file(self, tmp_path):
        path = write_task()

        def edited_meanwhile(_plan):
            write_task(note="edited elsewhere")

        agent, _ = agent_with_card(tmp_path, on_card=edited_meanwhile)
        out = str(agent._dispatch("schedule_task", {"id": "paris-monitor", "delete": True}))
        assert out.startswith("NOT WRITTEN") and saved(path)["note"] == "edited elsewhere"

    def test_a_create_does_not_overwrite_a_file_that_appeared(self, tmp_path):
        def created_meanwhile(_plan):
            write_task("eon-invoices", note="made elsewhere")

        agent, _ = agent_with_card(tmp_path, on_card=created_meanwhile)
        out = str(agent._dispatch("schedule_task", {
            "id": "eon-invoices", "at": AT.isoformat(timespec="minutes"), "prompt": PROMPT,
            "note": "n",
        }))
        assert out.startswith("NOT WRITTEN")
        assert saved(schedule.automations_dir() / "eon-invoices.json")["note"] == "made elsewhere"
