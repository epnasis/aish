"""The automation store and its time logic (epic #142 W2, S1) — no server, no clock."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest

from aish import schedule
from aish.schedule import (
    DISABLED,
    DUE,
    EXPIRED,
    FIRED,
    PENDING,
    STALE_AFTER,
    InvalidTask,
    LedgerUnreadable,
    next_due,
    validate_task,
)

NOW = datetime(2026, 10, 15, 18, 30)


def raw(**over):
    base = {
        "id": "eon-invoices",
        "prompt": "Download the October E.ON invoices.",
        "schedule": {"at": "2026-10-15T22:05"},
    }
    base.update(over)
    return base


class TestAutomationValidation:
    def test_a_minimal_task_validates(self):
        task = validate_task(raw())
        assert task.id == "eon-invoices"
        assert task.at == datetime(2026, 10, 15, 22, 5)
        assert task.enabled is True and task.allow == () and task.sites == ()

    @pytest.mark.parametrize(
        "over, fragment",
        [
            ({"id": "Bad Id"}, "id"),
            ({"id": ""}, "id"),
            ({"prompt": "  "}, "prompt"),
            ({"schedule": {"every": "1d"}}, "schedule"),
            ({"schedule": {"at": "2026-10-15T22:05", "every": "1d"}}, "schedule"),
            ({"schedule": "2026-10-15T22:05"}, "schedule"),
            ({"schedule": {"at": "tonight"}}, "schedule.at"),
            ({"schedule": {"at": "2026-10-15"}}, "schedule.at"),
            ({"schedule": {"at": 1760558700}}, "schedule.at"),
            ({"enabled": "yes"}, "enabled"),
            ({"allow": "git pull"}, "allow"),
            ({"allow": [""]}, "empty"),
            ({"allow": ["rm -rf /"]}, "denylist"),
            ({"allow": ["python3"]}, "bare interpreter"),
            ({"allow": ["ls\x1b[2J"]}, "control"),
            ({"sites": ["https://eon.pl/mojeon"]}, "bare host"),
            ({"sites": ["eon.pl:443"]}, "bare host"),
            ({"cwd": "relative/dir"}, "absolute"),
            ({"note": "two\nlines"}, "one line"),
            ({"unexpected": True}, "unknown field"),
        ],
    )
    def test_a_bad_field_is_refused(self, over, fragment):
        with pytest.raises(InvalidTask, match=fragment):
            validate_task(raw(**over))

    def test_a_user_denylist_entry_refuses_the_prefix(self):
        with pytest.raises(InvalidTask, match="denylist"):
            validate_task(raw(allow=["git push --force"]), deny_prefixes=["git push"])

    def test_creation_refuses_a_time_already_past(self):
        with pytest.raises(InvalidTask, match="not in the future"):
            validate_task(raw(schedule={"at": "2026-10-15T18:00"}), now=NOW)

    def test_a_loaded_task_may_be_in_the_past(self):
        assert validate_task(raw(schedule={"at": "2026-10-15T18:00"})).at.hour == 18

    def test_an_offset_is_converted_to_local_time(self):
        at = "2026-10-15T20:05:00+00:00"
        expected = datetime.fromisoformat(at).astimezone().replace(tzinfo=None)
        assert validate_task(raw(schedule={"at": at})).at == expected

    def test_sites_are_normalised_to_the_press_grant_vocabulary(self):
        # `_approved_sites` is suffix-matched on www-stripped hosts.
        task = validate_task(raw(sites=["WWW.EON.pl", "eon.pl"]))
        assert task.sites == ("eon.pl",)

    def test_a_run_may_not_be_rooted_over_the_automations_folder(self):
        home = schedule.automations_dir().parent
        with pytest.raises(InvalidTask, match="automations folder"):
            validate_task(raw(cwd=str(home)))
        with pytest.raises(InvalidTask, match="automations folder"):
            validate_task(raw(roots=[str(schedule.automations_dir() / "x")]))


class TestAutomationStore:
    def test_round_trip(self, tmp_path):
        task = validate_task(raw(allow=["git pull"], sites=["eon.pl"], model="qwen3:8b",
                                 note="E.ON portal is down until 22:00"))
        (tmp_path / "eon-invoices.json").write_text(task.render(), encoding="utf-8")
        loaded = schedule.load_tasks(tmp_path)
        assert loaded.problems == []
        assert loaded.tasks == [task]

    def test_one_bad_file_never_hides_the_others(self, tmp_path):
        (tmp_path / "good.json").write_text(json.dumps(raw(id="good")), encoding="utf-8")
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        (tmp_path / "wrong.json").write_text(json.dumps(raw(id="other")), encoding="utf-8")
        (tmp_path / "shape.json").write_text(
            json.dumps(raw(id="shape", schedule={"cron": "* * * * *"})), encoding="utf-8"
        )
        loaded = schedule.load_tasks(tmp_path)
        assert [t.id for t in loaded.tasks] == ["good"]
        problems = {os.path.basename(p.path): p.error for p in loaded.problems}
        assert set(problems) == {"broken.json", "wrong.json", "shape.json"}
        assert "does not match the file name" in problems["wrong.json"]

    def test_a_missing_folder_is_no_tasks_and_no_problem(self, tmp_path):
        loaded = schedule.load_tasks(tmp_path / "absent")
        assert loaded.tasks == [] and loaded.problems == []

    def test_the_folder_resolves_through_the_config_home_at_call_time(self, tmp_path,
                                                                      monkeypatch):
        monkeypatch.setenv("AISH_CONFIG_HOME", str(tmp_path))
        assert schedule.automations_dir() == tmp_path / "automations"


class TestAutomationReloadSignal:
    def test_an_in_place_edit_changes_the_signature(self, tmp_path):
        path = tmp_path / "eon-invoices.json"
        path.write_text(json.dumps(raw()), encoding="utf-8")
        before = schedule.signature(tmp_path)
        dir_mtime = tmp_path.stat().st_mtime_ns
        st = path.stat()
        path.write_text(json.dumps(raw(prompt="Something else entirely.")), encoding="utf-8")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        # The case the per-file signal exists for: the directory did not move.
        os.utime(tmp_path, ns=(dir_mtime, dir_mtime))
        assert tmp_path.stat().st_mtime_ns == dir_mtime
        assert schedule.signature(tmp_path) != before

    def test_an_unchanged_folder_keeps_its_signature(self, tmp_path):
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")
        assert schedule.signature(tmp_path) == schedule.signature(tmp_path)


class TestFiredLedgerKeying:
    def test_the_key_carries_the_time(self):
        assert validate_task(raw()).key == "eon-invoices:2026-10-15T22:05:00"

    def test_editing_at_rearms_the_task(self):
        first = validate_task(raw())
        fired = {first.key}
        moved = validate_task(raw(schedule={"at": "2026-10-16T09:00"}))
        assert next_due(first, fired, datetime(2026, 10, 16, 10, 0)) == FIRED
        assert next_due(moved, fired, datetime(2026, 10, 16, 10, 0)) == DUE

    def test_a_cosmetic_respelling_of_the_same_time_does_not_rearm(self):
        assert validate_task(raw()).key == validate_task(
            raw(schedule={"at": "2026-10-15T22:05:00"})
        ).key

    def test_absent_ledger_is_empty_and_a_corrupt_one_is_unreadable(self, tmp_path):
        store = schedule.fired_ledger(tmp_path)
        assert store.read() == {}
        store.path.parent.mkdir(parents=True)
        store.path.write_text("{half", encoding="utf-8")
        with pytest.raises(LedgerUnreadable):
            store.read()

    def test_the_ledger_round_trips(self, tmp_path):
        store = schedule.fired_ledger(tmp_path)
        store.write({"a:1": {"session": "s"}})
        assert store.read() == {"a:1": {"session": "s"}}


class TestAutomationStaleness:
    task = validate_task(raw())

    def test_before_the_time_it_is_pending(self):
        assert next_due(self.task, set(), self.task.at - timedelta(seconds=1)) == PENDING

    def test_at_the_time_it_is_due(self):
        assert next_due(self.task, set(), self.task.at) == DUE

    def test_exactly_at_the_bound_it_is_still_due(self):
        assert next_due(self.task, set(), self.task.at + STALE_AFTER) == DUE

    def test_past_the_bound_it_is_expired_never_due(self):
        late = self.task.at + STALE_AFTER + timedelta(seconds=1)
        assert next_due(self.task, set(), late) == EXPIRED

    def test_a_disabled_task_never_comes_due(self):
        task = validate_task(raw(enabled=False))
        assert next_due(task, set(), task.at) == DISABLED


class TestAutomationCardMeaning:
    def test_the_whole_prompt_rides_the_card_untruncated(self):
        long_prompt = "Check the E.ON portal. " * 200
        task = validate_task(raw(prompt=long_prompt, allow=["git pull"], sites=["eon.pl"]))
        card = schedule.describe(task, NOW)
        assert long_prompt.strip() in card
        assert "22:05 today" in card
        assert "`git pull`" in card and "`eon.pl`" in card

    def test_no_grants_says_so_in_words(self):
        card = schedule.describe(validate_task(raw()), NOW)
        assert "read-only ones only" in card
        assert "act on as you without asking: none" in card
