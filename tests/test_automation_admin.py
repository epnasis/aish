"""The owner's automations from the terminal: `/tasks` and `/runs` (#5 S4).

Every test runs against the suite's isolated config home (`AISH_CONFIG_HOME`,
set by conftest's `isolated_global_dirs`, which is what `automations_dir()`
resolves through at call time) and a tmp state dir — never the owner's.
"""

from __future__ import annotations

import builtins
import dataclasses
import inspect
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aish import atomic_write, cli, schedule
from aish import automation_admin as admin
from aish.session import SessionLog

NOW = datetime(2026, 10, 15, 12, 0)


def write(task_id, **body):
    directory = schedule.automations_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps({"id": task_id, "prompt": f"Do {task_id}.", **body}),
                    encoding="utf-8")
    return path


def folder():
    return sorted(p.name for p in schedule.automations_dir().iterdir())


@pytest.fixture
def state_dir(tmp_path):
    path = tmp_path / "state"
    path.mkdir()
    return path


def slash(text, state_dir):
    return cli.handle_slash(text, None, None, state_dir)  # type: ignore[arg-type]


class TestTasksListing:
    def test_every_shape_says_where_it_stands(self, state_dir):
        write("watch", schedule={"cron": "0 9 * * *"}, kind="monitor", note="price watch")
        write("later", schedule={"at": "2026-10-15T22:05"})
        write("ran", schedule={"at": "2026-10-15T08:00"})
        write("gone", schedule={"at": "2026-10-13T08:00"})
        write("off", schedule={"cron": "0 7 * * 1-5"}, enabled=False)
        (schedule.automations_dir() / "broken.json").write_text("{not json", encoding="utf-8")
        schedule.fired_ledger(state_dir).write({
            "ran:2026-10-15T08:00:00": {"fired_at": "2026-10-15T08:00:12",
                                        "session": "session-x.jsonl"},
            "watch:2026-10-15T09:00:00": {"fired_at": "2026-10-15T09:00:05"},
        })
        schedule.scheduler_state(state_dir).write(
            {"settled": {"gone:2026-10-13T08:00:00": "expired"}}
        )
        listing = admin.read_listing(state_dir, deny_prefixes=(), now=NOW)
        text = "\n".join(admin.task_lines(listing, NOW))
        watch = schedule.validate_task(json.loads(
            (schedule.automations_dir() / "watch.json").read_text()))
        assert f"runs {schedule.when_words(watch, NOW)}" in text  # the card's own words
        assert "watch — price watch\n    monitor · runs on the schedule `0 9 * * *` · " \
            "enabled · notify on_change" in text
        assert "next 09:00 Fri 2026-10-16 · last ran for 09:00 Thu 2026-10-15" in text
        assert "later\n    task · runs ONCE, at 22:05 today (Thu 2026-10-15)" in text
        assert "pending — runs at 22:05 Thu 2026-10-15" in text
        assert "fired at 2026-10-15 08:00:12, in session-x.jsonl" in text
        assert "expired: it did not run, and it will not" in text
        assert "DISABLED" in text and "would next run at 07:00 Fri 2026-10-16 if enabled" in text
        assert "PROBLEM broken.json — not a runnable automation" in text

    def test_an_unreadable_ledger_is_unknown_never_pending(self, state_dir):
        write("later", schedule={"at": "2026-10-15T22:05"})
        (state_dir / "scheduler").mkdir()
        (state_dir / "scheduler" / "fired.json").write_text("{torn", encoding="utf-8")
        listing = admin.read_listing(state_dir, deny_prefixes=(), now=NOW)
        assert "unknown — aish cannot read its record of fired runs" in "\n".join(
            admin.task_lines(listing, NOW))

    def test_the_cli_prints_it(self, state_dir, capsys):
        write("later", schedule={"at": "2099-10-15T22:05"})
        (schedule.automations_dir() / "broken.json").write_text("[]", encoding="utf-8")
        assert slash("/tasks", state_dir) == "handled"
        out = capsys.readouterr().out
        assert "later" in out and "PROBLEM broken.json" in out


class TestTheToggle:
    def test_a_round_trip_changes_only_enabled(self, state_dir, capsys):
        path = write("daily", schedule={"cron": "0 9 * * 1-5"}, allow=["touch"],
                     sites=["www.Eon.pl"], note="daily", created="2026-10-01")
        before = schedule.validate_task(json.loads(path.read_text()))
        slash("/tasks disable daily", state_dir)
        assert "disabled daily" in capsys.readouterr().out
        off = schedule.validate_task(json.loads(path.read_text()))
        assert off == dataclasses.replace(before, enabled=False)
        slash("/tasks enable daily", state_dir)
        assert "enabled daily — its next scheduled time is" in capsys.readouterr().out
        assert schedule.validate_task(json.loads(path.read_text())) == before
        assert folder() == ["daily.json"]

    def test_an_unchanged_state_writes_nothing(self, state_dir, capsys):
        path = write("daily", schedule={"cron": "0 9 * * *"})
        stamp = path.stat().st_mtime_ns
        slash("/tasks enable daily", state_dir)
        assert "already enabled" in capsys.readouterr().out
        assert path.stat().st_mtime_ns == stamp

    def test_a_problem_file_is_refused_not_rewritten(self, state_dir, capsys):
        path = schedule.automations_dir()
        path.mkdir(parents=True, exist_ok=True)
        broken = path / "broken.json"
        broken.write_text('{"id": "broken", "enabled": true}', encoding="utf-8")
        before = broken.read_bytes()
        slash("/tasks disable broken", state_dir)
        out = capsys.readouterr().out
        assert "will not rewrite one field of it" in out and "/tasks delete broken" in out
        assert broken.read_bytes() == before

    def test_a_denylisted_prefix_makes_it_a_problem_here_too(self, state_dir):
        write("daily", schedule={"cron": "0 9 * * *"}, allow=["rm -rf build"])
        with pytest.raises(admin.AdminError, match="not a runnable automation"):
            admin.set_enabled("daily", False, deny_prefixes=["rm -rf"], now=NOW)

    @pytest.mark.parametrize("name", ["nope", "../etc/passwd", ".hidden", ""])
    def test_only_a_file_the_folder_holds_can_be_named(self, state_dir, name):
        write("daily", schedule={"cron": "0 9 * * *"})
        with pytest.raises(admin.AdminError):
            admin.set_enabled(name, False, deny_prefixes=(), now=NOW)
        with pytest.raises(admin.AdminError):
            admin.delete(name)
        assert folder() == ["daily.json"]

    def test_it_is_a_toggle_and_a_shredder_never_an_author(self, state_dir, monkeypatch):
        """Structural: the writer takes no content, and every write it makes
        lands on a file that already existed, holding that file's own task
        with only `enabled` changed."""
        assert list(inspect.signature(admin.set_enabled).parameters) == [
            "task_id", "enabled", "deny_prefixes", "now"
        ]
        assert list(inspect.signature(admin.delete).parameters) == ["task_id"]
        a = write("a", schedule={"cron": "0 9 * * *"}, kind="monitor", notify="always")
        write("b", schedule={"at": "2026-10-20T10:00"})
        existed = {p: schedule.validate_task(json.loads(p.read_text()))
                   for p in schedule.automations_dir().glob("*.json")}
        published: list[tuple[Path, str]] = []
        real = atomic_write.publish

        def spy(path, text, **kwargs):
            published.append((Path(path), text))
            return real(path, text, **kwargs)

        monkeypatch.setattr(atomic_write, "publish", spy)
        for task_id in ("a", "b"):
            admin.set_enabled(task_id, False, deny_prefixes=(), now=NOW)
            admin.set_enabled(task_id, True, deny_prefixes=(), now=NOW)
        admin.delete("b")
        assert published and all(path in existed for path, _ in published)
        for path, text in published:
            written = schedule.validate_task(json.loads(text))
            assert dataclasses.replace(written, enabled=True) == existed[path]
        assert folder() == ["a.json"] and a.exists()

    def test_the_schedulers_own_never_writes_pin_is_untouched(self):
        source = Path(__file__).with_name("test_scheduler.py").read_text()
        assert "def test_code_never_writes_the_automations_folder" in source


class TestDelete:
    def _answer(self, monkeypatch, reply):
        def fake_input(prompt=""):
            fake_input.prompt = prompt
            if reply is EOFError:
                raise EOFError
            return reply

        monkeypatch.setattr(builtins, "input", fake_input)
        return fake_input

    @pytest.mark.parametrize("reply", ["", "n", "no", EOFError])
    def test_no_is_the_default_and_keeps_the_file(self, state_dir, monkeypatch, capsys, reply):
        write("daily", schedule={"cron": "0 9 * * *"})
        asked = self._answer(monkeypatch, reply)
        slash("/tasks delete daily", state_dir)
        assert "[y/N]" in asked.prompt and "no way back" in asked.prompt
        assert "approval card" in asked.prompt
        assert "cancelled" in capsys.readouterr().out
        assert folder() == ["daily.json"]

    def test_yes_removes_exactly_that_file(self, state_dir, monkeypatch, capsys):
        write("daily", schedule={"cron": "0 9 * * *"})
        write("other", schedule={"cron": "0 9 * * *"})
        self._answer(monkeypatch, "y")
        slash("/tasks delete daily", state_dir)
        assert "deleted daily.json" in capsys.readouterr().out
        assert folder() == ["other.json"]

    def test_a_problem_file_can_be_deleted(self, state_dir, monkeypatch):
        directory = schedule.automations_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "broken.json").write_text("{nope", encoding="utf-8")
        self._answer(monkeypatch, "yes")
        slash("/tasks delete broken", state_dir)
        assert folder() == []

    def test_usage(self, state_dir, capsys):
        slash("/tasks frobnicate daily", state_dir)
        assert "usage: /tasks" in capsys.readouterr().out


def scheduled_chat(state_dir, stamp, task_id="daily", *, quiet=False, answer="done"):
    log = SessionLog(state_dir / f"session-{stamp}-000000.jsonl")
    log.origin("schedule")
    log.message({"role": "user", "content": f"[scheduled: {task_id}] An automation fired."})
    log.message({"role": "assistant", "content": answer})
    if quiet:
        log.quiet(through=log.output_at, automation=task_id, status="unchanged")
    log.close()
    return log.path


def seen(state_dir, marks):
    (state_dir / "seen.json").write_text(json.dumps({"seen": marks, "floor": 0}))


class TestRuns:
    def test_runs_join_chats_and_records_newest_first(self, state_dir):
        opened = scheduled_chat(state_dir, "20261014-090000")
        unopened = scheduled_chat(state_dir, "20261015-090000", "watch")
        silent = scheduled_chat(state_dir, "20261015-093000", "watch", quiet=True)
        user = SessionLog(state_dir / "session-20261015-100000-000000.jsonl")
        user.message({"role": "user", "content": "hello"})
        user.close()
        schedule.fired_ledger(state_dir).write({
            "daily:2026-10-14T09:00:00": {"scheduled_for": "2026-10-14T09:00:00",
                                          "session": opened.name},
        })
        schedule.scheduler_state(state_dir).write({"records": [
            {"kind": "monitor", "task": "watch", "session": unopened.name,
             "status": "changed", "summary": "price fell", "snapshot_identical": False,
             "verdict_matches_bytes": True},
            {"kind": "monitor", "task": "watch", "session": silent.name,
             "status": "unchanged", "snapshot_identical": True,
             "verdict_matches_bytes": True},
            {"kind": "expired", "task": "once", "observed_at": "2026-10-15T11:00:00",
             "error": "aish did not observe an attempt to start it"},
            {"kind": "skipped", "task": "daily", "observed_at": "2026-10-13T09:00:00",
             "from": "2026-10-11T09:00:00", "through": "2026-10-12T09:00:00",
             "count": 2, "latest": "2026-10-13T09:00:00", "count_capped": False},
        ]})
        seen(state_dir, {opened.name: 4_000_000_000})  # read, in the far future
        runs, problems = admin.recent_runs(state_dir)
        assert problems == []
        assert [(r.task, r.session) for r in runs] == [
            ("once", ""), ("watch", silent.name), ("watch", unopened.name),
            ("daily", opened.name), ("daily", ""),
        ]
        by = {r.session: r for r in runs if r.session}
        assert by[unopened.name].unseen and not by[unopened.name].silent
        assert "changed — price fell · snapshot differs from the baseline" in \
            by[unopened.name].what
        assert by[silent.name].unseen and by[silent.name].silent
        assert not by[opened.name].unseen and by[opened.name].what == \
            "ran for 2026-10-14 09:00:00"
        assert not any(r.unseen for r in runs if not r.session)
        text = "\n".join(admin.run_lines(runs, problems))
        assert "[not opened]" in text and "[not opened, silent]" in text
        assert "did not run: aish did not observe" in text
        assert "skipped: 2 occurrence(s) from 2026-10-11 09:00:00" in text
        assert "resume" not in text.lower()  # names chats, never offers to resume one

    def test_an_unreadable_state_file_is_said_not_absorbed(self, state_dir):
        scheduled_chat(state_dir, "20261015-090000")
        (state_dir / "scheduler").mkdir()
        (state_dir / "scheduler" / "state.json").write_text("{torn", encoding="utf-8")
        runs, problems = admin.recent_runs(state_dir)
        assert len(runs) == 1 and problems[0].startswith("aish cannot read its scheduler state")

    def test_the_stores_it_reads_are_published_atomically(self, state_dir, monkeypatch):
        """Reading while aish-web writes is safe only because the writes are
        whole-file publishes; pinned so a future store cannot quietly stop."""
        calls = []
        monkeypatch.setattr(atomic_write, "publish", lambda path, *a, **k: calls.append(path))
        schedule.fired_ledger(state_dir).write({})
        schedule.scheduler_state(state_dir).write({})
        assert calls == [state_dir / "scheduler" / "fired.json",
                         state_dir / "scheduler" / "state.json"]

    def test_the_cli_prints_it(self, state_dir, capsys):
        scheduled_chat(state_dir, "20261015-090000")
        assert slash("/runs", state_dir) == "handled"
        assert "daily  ran" in capsys.readouterr().out


class TestStartupNotice:
    def test_it_counts_unopened_runs_and_not_silent_ones(self, state_dir):
        now = datetime.now()
        stamp = (now - timedelta(hours=1)).strftime("%Y%m%d-%H%M%S")
        scheduled_chat(state_dir, stamp)
        scheduled_chat(state_dir, (now - timedelta(hours=2)).strftime("%Y%m%d-%H%M%S"),
                       "watch", quiet=True)
        # Unopened too, but started before the window the notice names.
        scheduled_chat(state_dir, (now - timedelta(days=30)).strftime("%Y%m%d-%H%M%S"))
        assert admin.notice(state_dir, now) == (
            "1 scheduled run from the last 14 days you haven't opened "
            "(silent monitor runs not counted) — /runs"
        )

    def test_opening_it_clears_it(self, state_dir):
        now = datetime.now()
        path = scheduled_chat(state_dir, (now - timedelta(hours=1)).strftime("%Y%m%d-%H%M%S"))
        seen(state_dir, {path.name: path.stat().st_mtime + 5})
        assert admin.notice(state_dir, now) == ""

    def test_the_cli_prints_it_and_survives_a_bad_state_dir(self, state_dir, capsys):
        now = datetime.now()
        scheduled_chat(state_dir, (now - timedelta(hours=1)).strftime("%Y%m%d-%H%M%S"))
        cli.print_runs_notice(state_dir)
        assert "1 scheduled run" in capsys.readouterr().out
        cli.print_runs_notice(state_dir / "missing")
        assert capsys.readouterr().out == ""
