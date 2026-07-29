"""Retrieval self-curation loop (#185): ledger scan, suspect classification,
and the trigger pass. No model, no network — the HTTP POST is a seam and the
ledger reads synthetic JSONL logs, mirroring test_email_poll.py discipline."""

import json
from datetime import datetime

from aish.curate import (
    GONE_NOTE,
    LEDGER_DAYS,
    MIN_INJECTIONS,
    build_prompt,
    corpus_identities,
    dead_weight,
    missing,
    render_report,
    run_curate,
    scan_ledger,
)

NOW = datetime(2026, 7, 29, 8, 30)


def write_log(state_dir, name, records):
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def knowledge(items):
    step = {"kind": "knowledge", "mode": "semantic", "items": items}
    return {"ts": "2026-07-28T10:00:00", "kind": "trace", "step": step}


def user(text, ts="2026-07-28T10:00:01"):
    return {"ts": ts, "kind": "message", "role": "user", "content": text}


def tool(name, summary):
    step = {"kind": "tool", "name": name, "summary": summary, "ok": True}
    return {"ts": "2026-07-28T10:00:02", "kind": "trace", "step": step}


class TestLedgerScan:
    def test_knowledge_step_pairs_with_the_following_user_message(self, tmp_path):
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            knowledge([{"label": "gh-tricks", "kind": "memory", "sim": 0.41, "rail": 0}]),
            user("open an issue"),
        ])
        ledger = scan_ledger(tmp_path, now=NOW)
        row = ledger.entries["gh-tricks"]
        assert row.injections == 1
        assert row.sims == [0.41]
        assert ledger.tasks == 1
        assert ledger.tasks_with_injection == 1

    def test_read_after_injection_is_engagement_not_miss(self, tmp_path):
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            knowledge([{"label": "deploy-web", "kind": "skill", "sim": 0.6, "rail": 0}]),
            user("ship it"),
            tool("read_skill", "deploy-web"),
        ])
        row = scan_ledger(tmp_path, now=NOW).entries["deploy-web"]
        assert row.reads == 1
        assert row.miss_reads == 0

    def test_read_without_injection_counts_as_miss(self, tmp_path):
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            user("email marta about the recipe"),
            tool("read_skill", "gws-gmail-send"),
        ])
        row = scan_ledger(tmp_path, now=NOW).entries["gws-gmail-send"]
        assert row.miss_reads == 1
        assert row.injections == 0

    def test_legacy_items_without_sims_count_and_flag_lexical(self, tmp_path):
        # Pre-#183 logs carry only label/kind; #183 lexical mode carries score.
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            knowledge([{"label": "old-entry", "kind": "memory", "score": 3}]),
            user("anything"),
        ])
        ledger = scan_ledger(tmp_path, now=NOW)
        assert ledger.entries["old-entry"].injections == 1
        assert ledger.entries["old-entry"].sims == []
        assert ledger.lexical_tasks == 1

    def test_old_sessions_outside_window_are_skipped(self, tmp_path):
        stale = f"session-{2026}0101-000000-000001.jsonl"
        write_log(tmp_path, stale, [
            knowledge([{"label": "ancient", "kind": "memory", "sim": 0.5}]),
            user("old task"),
        ])
        ledger = scan_ledger(tmp_path, days=LEDGER_DAYS, now=NOW)
        assert "ancient" not in ledger.entries
        assert ledger.tasks == 0


class TestClassification:
    def _ledger_with(self, tmp_path, injections, reads=0):
        records = []
        for i in range(injections):
            records += [
                knowledge([{"label": "noisy", "kind": "memory", "sim": 0.30, "rail": 3}]),
                user(f"task {i}"),
            ]
            if i < reads:
                records.append(tool("read_skill", "noisy"))
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", records)
        return scan_ledger(tmp_path, now=NOW)

    def test_repeated_unengaged_injection_is_dead_weight(self, tmp_path):
        ledger = self._ledger_with(tmp_path, injections=MIN_INJECTIONS)
        assert [r.name for r in dead_weight(ledger)] == ["noisy"]

    def test_engagement_clears_the_entry(self, tmp_path):
        ledger = self._ledger_with(tmp_path, injections=MIN_INJECTIONS, reads=1)
        assert dead_weight(ledger) == []

    def test_below_min_injections_is_not_evidence(self, tmp_path):
        ledger = self._ledger_with(tmp_path, injections=MIN_INJECTIONS - 1)
        assert dead_weight(ledger) == []

    def test_missing_needs_repeat_and_more_misses_than_hits(self, tmp_path):
        records = [user("t0"), tool("read_skill", "wanted"), user("t1"),
                   tool("read_skill", "wanted")]
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", records)
        ledger = scan_ledger(tmp_path, now=NOW)
        assert [r.name for r in missing(ledger)] == ["wanted"]


class TestCurationPass:
    def _posts(self):
        calls = []

        def fake_post(url, body, timeout=30):
            calls.append((url, json.loads(body)))
            return 200, {}, b'{"session": "s1"}'

        return calls, fake_post

    def _actionable_state(self, tmp_path):
        records = []
        for i in range(MIN_INJECTIONS):
            records += [
                knowledge([{"label": "noisy", "kind": "memory", "sim": 0.29, "rail": 3}]),
                user(f"task {i}"),
            ]
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", records)

    def test_triggers_schedule_session_with_week_dedup(self, tmp_path):
        self._actionable_state(tmp_path)
        calls, fake_post = self._posts()
        rc = run_curate(post=fake_post, env={}, token="tok", state_dir=tmp_path, now=NOW)
        assert rc == 0
        assert len(calls) == 1
        url, body = calls[0]
        assert "token=tok" in url
        assert body["origin"] == "schedule"
        # Privacy default (#186): the curation prompt aggregates excerpts
        # from every recent session, so the session runs LOCAL by default,
        # and the dedup key is per-model so an experiment never dedupes
        # into the scheduled run.
        from aish.curate import DEFAULT_MODEL

        assert body["model"] == DEFAULT_MODEL
        assert body["meta"]["dedup_key"] == f"curate-{NOW:%G-W%V}-qwen3-8b"
        assert "noisy" in body["prompt"]
        assert "MUST NOT call forget_memory" in body["prompt"]
        assert "MUST NOT use run_command" in body["prompt"]

    def test_model_override_via_flag_and_env(self, tmp_path):
        self._actionable_state(tmp_path)
        calls, fake_post = self._posts()
        run_curate(post=fake_post, env={}, token="tok", state_dir=tmp_path,
                   now=NOW, model="gemini:gemini-3.5-flash")
        assert calls[-1][1]["model"] == "gemini:gemini-3.5-flash"
        assert calls[-1][1]["meta"]["dedup_key"].endswith("-gemini-gemini-3-5-flash")
        run_curate(post=fake_post, env={"AISH_CURATE_MODEL": "qwen3:4b"},
                   token="tok", state_dir=tmp_path, now=NOW)
        assert calls[-1][1]["model"] == "qwen3:4b"

    def test_nothing_actionable_means_no_trigger(self, tmp_path):
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            knowledge([{"label": "fine", "kind": "skill", "sim": 0.7}]),
            user("task"),
            tool("read_skill", "fine"),
        ])
        calls, fake_post = self._posts()
        rc = run_curate(post=fake_post, env={}, token="tok", state_dir=tmp_path, now=NOW)
        assert rc == 0
        assert calls == []

    def test_dry_run_prints_and_never_posts(self, tmp_path, capsys):
        self._actionable_state(tmp_path)
        calls, fake_post = self._posts()
        rc = run_curate(post=fake_post, env={}, token="tok", state_dir=tmp_path,
                        now=NOW, dry_run=True)
        assert rc == 0
        assert calls == []
        assert "noisy" in capsys.readouterr().out

    def test_no_token_refuses(self, tmp_path, monkeypatch):
        self._actionable_state(tmp_path)
        monkeypatch.setattr("aish.curate.read_token", lambda: "")
        calls, fake_post = self._posts()
        rc = run_curate(post=fake_post, env={}, state_dir=tmp_path, now=NOW)
        assert rc == 2
        assert calls == []

    def test_report_carries_current_identity_lines(self, tmp_path):
        # #185 follow-up (the no-shell rule's other half): the session must
        # never need to list/read files, so the report quotes each suspect's
        # CURRENT description/keywords, and a retired suspect is named a
        # ghost so the model skips it instead of recreating it.
        self._actionable_state(tmp_path)
        report = render_report(
            scan_ledger(tmp_path, now=NOW),
            {"noisy": "a noisy fact [keywords: alpha, beta] (pinned)"},
        )
        assert "now: a noisy fact [keywords: alpha, beta] (pinned)" in report
        ghost = render_report(scan_ledger(tmp_path, now=NOW), {})
        assert GONE_NOTE in ghost

    def test_corpus_identities_reads_active_entries(self, tmp_path):
        import aish.skills as skills_module

        (skills_module.GLOBAL_MEMORY_DIR / "fact-a.md").write_text(
            "---\nname: fact-a\ndescription: a fact\nkeywords: k1, k2\n"
            "pinned: yes\n---\n"
        )
        (skills_module.GLOBAL_MEMORY_DIR / "gone.md").write_text(
            "---\nname: gone\ndescription: retired\nstatus: disabled\n---\n"
        )
        identities = corpus_identities()
        assert identities["fact-a"] == "a fact [keywords: k1, k2] (pinned)"
        assert "gone" not in identities

    def test_report_caps_its_size(self, tmp_path):
        records = []
        for n in range(30):
            for i in range(MIN_INJECTIONS):
                records += [
                    knowledge([{"label": f"entry-{n:02d}", "kind": "memory", "sim": 0.3}]),
                    user(("long prompt " * 30) + f"{n}-{i}"),
                ]
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", records)
        report = render_report(scan_ledger(tmp_path, now=NOW))
        assert len(report) <= 9200
        assert len(build_prompt(report)) < 12000
