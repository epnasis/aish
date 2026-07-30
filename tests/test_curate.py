"""Retrieval self-curation loop (#185): ledger scan, suspect classification,
verdict parsing, the code envelope, and the judge loop. No model, no network —
the judge is an injected callable and the ledger reads synthetic JSONL logs."""

import json
from datetime import datetime

from aish.curate import (
    LEDGER_DAYS,
    MIN_INJECTIONS,
    Verdict,
    dead_weight,
    dup_candidates,
    judge_prompt,
    load_recent_actions,
    missing,
    parse_pair_verdict,
    parse_verdict,
    run_curate,
    scan_ledger,
    update_entry_meta,
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


class TestVerdictParsing:
    def test_accepts_each_verb_and_strips_thinking(self):
        text = "<think>hmm let me\nponder</think>VERDICT: Pin.\nREASON: standing rule"
        v = parse_verdict(text)
        assert v == Verdict(action="pin", reason="standing rule")
        for verb in ("repair", "disable", "skip"):
            body = f"VERDICT: {verb}\nREASON: r\nDESCRIPTION: d\nKEYWORDS: k"
            assert parse_verdict(body).action == verb

    def test_repair_without_description_is_a_parse_failure(self):
        assert parse_verdict("VERDICT: repair\nREASON: bad keywords") is None

    def test_unknown_verb_or_prose_is_rejected(self):
        assert parse_verdict("VERDICT: delete\nREASON: nuke it") is None
        assert parse_verdict("I think this entry should be repaired.") is None


def make_entry(directory, name, description, keywords="", kind="memory",
               body="body text", extra=""):
    directory.mkdir(parents=True, exist_ok=True)
    kw = f"keywords: {keywords}\n" if keywords else ""
    path = directory / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{kw}{extra}---\n{body}\n",
        encoding="utf-8",
    )
    return path


class TestUpdateEntryMeta:
    def test_repair_replaces_identity_and_preserves_body_and_extras(self, tmp_path):
        path = make_entry(tmp_path, "e", "old desc", keywords="a, b",
                          extra="expires: 2027-01-01\n", body="precious body\nline2")
        update_entry_meta(path, description="new desc", keywords="x, y, X")
        text = path.read_text(encoding="utf-8")
        assert "description: new desc" in text
        assert "keywords: x, y" in text and ", X" not in text  # deduped
        assert "old desc" not in text
        assert "expires: 2027-01-01" in text  # unknown lines survive
        assert text.endswith("precious body\nline2\n")  # body byte-identical

    def test_disable_and_pin_toggle_lifecycle_lines(self, tmp_path):
        path = make_entry(tmp_path, "e", "desc")
        update_entry_meta(path, disabled=True)
        assert "status: disabled" in path.read_text(encoding="utf-8")
        update_entry_meta(path, pinned=True)
        assert "pinned: yes" in path.read_text(encoding="utf-8")

    def test_no_frontmatter_refuses(self, tmp_path):
        path = tmp_path / "bare.md"
        path.write_text("just text", encoding="utf-8")
        import pytest

        with pytest.raises(ValueError):
            update_entry_meta(path, disabled=True)


class TestJudgeLoop:
    """The orchestration lives in the script: one bounded judge call per
    suspect, verdicts applied through the code envelope, everything logged
    and cooldown-protected."""

    def _corpus(self, monkeypatch, tmp_path):
        import aish.skills as skills_module

        gm = tmp_path / "gm"
        monkeypatch.setattr(skills_module, "GLOBAL_MEMORY_DIR", gm)
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", tmp_path / "gs")
        return gm

    def _dead_weight_logs(self, state):
        records = []
        for i in range(MIN_INJECTIONS):
            records += [
                knowledge([{"label": "noisy", "kind": "memory", "sim": 0.29, "rail": 3}]),
                user(f"task {i}"),
            ]
        write_log(state, "session-20260728-100000-000001.jsonl", records)

    def test_repair_verdict_is_applied_and_logged(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        path = make_entry(gm, "noisy", "generic thing", keywords="code, change")
        state = tmp_path / "state"
        self._dead_weight_logs(state)
        prompts = []

        def judge(prompt):
            prompts.append(prompt)
            return ("VERDICT: repair\nREASON: generic keywords\n"
                    "DESCRIPTION: precise thing\nKEYWORDS: precise, thing")

        rc = run_curate(judge=judge, scores=lambda q, e: {}, notify_fn=lambda *a: None,
                        env={}, state_dir=state, now=NOW)
        assert rc == 0
        assert len(prompts) == 1
        assert "generic thing" in prompts[0]  # judge saw current identity
        assert '"task 0"' in prompts[0]  # and the evidence excerpts
        text = path.read_text(encoding="utf-8")
        assert "description: precise thing" in text
        recent = load_recent_actions(state, NOW)
        assert recent == {"noisy": "repair"}

    def test_disable_and_pin_apply(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        path = make_entry(gm, "noisy", "a standing rule")
        state = tmp_path / "state"
        self._dead_weight_logs(state)
        run_curate(judge=lambda p: "VERDICT: pin\nREASON: rule", scores=lambda q, e: {},
                   notify_fn=lambda *a: None, env={}, state_dir=state, now=NOW)
        assert "pinned: yes" in path.read_text(encoding="utf-8")

    def test_unparseable_reply_retries_once_then_skips(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        path = make_entry(gm, "noisy", "desc", keywords="k")
        state = tmp_path / "state"
        self._dead_weight_logs(state)
        prompts = []

        def judge(prompt):
            prompts.append(prompt)
            return "I would probably delete this one."

        run_curate(judge=judge, scores=lambda q, e: {}, notify_fn=lambda *a: None,
                   env={}, state_dir=state, now=NOW)
        assert len(prompts) == 2  # one retry with the format nudge
        assert "did not match the required format" in prompts[1]
        assert "status: disabled" not in path.read_text(encoding="utf-8")
        assert load_recent_actions(state, NOW)["noisy"] == "skip"

    def test_cooldown_prevents_rejudging(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        make_entry(gm, "noisy", "desc")
        state = tmp_path / "state"
        self._dead_weight_logs(state)
        calls = []
        judge = lambda p: (calls.append(p), "VERDICT: skip\nREASON: fine")[1]  # noqa: E731
        run_curate(judge=judge, scores=lambda q, e: {}, notify_fn=lambda *a: None,
                   env={}, state_dir=state, now=NOW)
        run_curate(judge=judge, scores=lambda q, e: {}, notify_fn=lambda *a: None,
                   env={}, state_dir=state, now=NOW)
        assert len(calls) == 1  # second pass found the entry cooling down

    def test_ghost_suspect_never_reaches_the_judge(self, tmp_path, monkeypatch):
        self._corpus(monkeypatch, tmp_path)  # entry file never created
        state = tmp_path / "state"
        self._dead_weight_logs(state)
        calls = []
        run_curate(judge=lambda p: calls.append(p) or "VERDICT: skip\nREASON: x",
                   scores=lambda q, e: {}, notify_fn=lambda *a: None,
                   env={}, state_dir=state, now=NOW)
        assert calls == []

    def test_dry_run_makes_no_model_calls_and_no_writes(self, tmp_path, monkeypatch, capsys):
        gm = self._corpus(monkeypatch, tmp_path)
        make_entry(gm, "noisy", "desc")
        state = tmp_path / "state"
        self._dead_weight_logs(state)
        rc = run_curate(judge=lambda p: 1 / 0, scores=lambda q, e: {},
                        notify_fn=lambda *a: None, env={}, state_dir=state,
                        now=NOW, dry_run=True)
        assert rc == 0
        assert "noisy" in capsys.readouterr().out
        assert not (state / "curation-actions.jsonl").exists()

    def test_notification_summarizes_actions(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        make_entry(gm, "noisy", "desc")
        state = tmp_path / "state"
        self._dead_weight_logs(state)
        pushed = []
        run_curate(judge=lambda p: "VERDICT: disable\nREASON: stale",
                   scores=lambda q, e: {}, notify_fn=lambda t, m: pushed.append((t, m)),
                   env={}, state_dir=state, now=NOW)
        assert pushed and "disable=1" in pushed[0][1]


class TestDuplicatePass:
    def _corpus(self, monkeypatch, tmp_path):
        import aish.skills as skills_module

        gm = tmp_path / "gm"
        monkeypatch.setattr(skills_module, "GLOBAL_MEMORY_DIR", gm)
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", tmp_path / "gs")
        return gm

    def test_candidates_via_injected_scores(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        make_entry(gm, "a", "offers with direct links")
        make_entry(gm, "b", "oferty always with direct links")
        make_entry(gm, "c", "unrelated fact")
        import aish.skills as skills_module

        entries = skills_module.load_entries(str(tmp_path), None)
        by = {e.name: e for e in entries}

        def scores(query, ents):
            hi = "a" if "oferty" in query else ("b" if "offers with" in query else None)
            return {id(e): (0.9 if e.name == hi else 0.1) for e in ents}

        pairs = dup_candidates(entries, scores)
        assert [{a.name, b.name} for a, b, _ in pairs] == [{"a", "b"}]
        assert by["c"] not in [p[0] for p in pairs] + [p[1] for p in pairs]

    def test_merge_disables_loser_only(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        keep = make_entry(gm, "offers-links", "offers must carry direct links")
        lose = make_entry(gm, "oferty-linki", "oferty must carry direct links")
        state = tmp_path / "state"
        state.mkdir()

        def judge(prompt):
            assert "duplicates" in prompt
            return "VERDICT: merge\nKEEP: offers-links\nREASON: same rule"

        rc = run_curate(judge=judge, notify_fn=lambda *a: None, env={},
                        state_dir=state, now=NOW,
                        scores=lambda q, e: {id(x): 0.9 for x in e})
        assert rc == 0
        assert "status: disabled" in lose.read_text(encoding="utf-8")
        assert "status: disabled" not in keep.read_text(encoding="utf-8")

    def test_merge_naming_neither_entry_is_invalid(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        import aish.skills as skills_module

        make_entry(gm, "a", "same rule")
        make_entry(gm, "b", "same rule again")
        a, b = skills_module.load_entries(str(tmp_path), None)
        action, survivor = parse_pair_verdict("VERDICT: merge\nKEEP: other\nREASON: r", a, b)
        assert (action, survivor) == ("invalid", None)
        assert parse_pair_verdict("VERDICT: distinct\nREASON: r", a, b) == ("distinct", None)


class TestJudgePrompt:
    def test_prompt_is_self_contained_and_imperative(self, tmp_path, monkeypatch):
        import aish.skills as skills_module

        gm = tmp_path / "gm"
        monkeypatch.setattr(skills_module, "GLOBAL_MEMORY_DIR", gm)
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", tmp_path / "gs")
        make_entry(gm, "noisy", "a desc", keywords="k1", body="the full body")
        entry = skills_module.load_entries(str(tmp_path), None)[0]
        from aish.curate import EntryStats

        stat = EntryStats(name="noisy", injections=7, rails=3, sims=[0.3],
                          evidence=[("2026-07-28T10:00:00", "some task", 0.3)])
        prompt = judge_prompt(entry, stat, "dead-weight")
        assert "the full body" in prompt
        assert "injected into 7 tasks" in prompt or "auto-injected" in prompt
        assert "VERDICT: <repair|pin|disable|skip>" in prompt
        assert "Example:" in prompt  # small models need MUST + example
