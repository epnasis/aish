"""Retrieval self-curation loop (#185): ledger scan, suspect classification,
verdict parsing, the code envelope, and the judge loop. No model, no network —
the judge is an injected callable and the ledger reads synthetic JSONL logs."""

import json
from datetime import datetime
from pathlib import Path

from aish import curate as curate_module
from aish.curate import (
    LEDGER_DAYS,
    MIN_INJECTIONS,
    RULE_MIN_FIRES,
    Verdict,
    dead_weight,
    dup_candidates,
    judge_prompt,
    load_recent_actions,
    missing,
    parse_pair_verdict,
    parse_verdict,
    rule_signals,
    run_curate,
    scan_ledger,
    scan_ratings,
    scan_rules,
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


class TestEnvelopeGuards:
    """Failure modes from the first live v2 run, each now refused by CODE:
    family merges, pinned-rule disables, and a skill losing to a memory."""

    def _corpus(self, monkeypatch, tmp_path):
        import aish.skills as skills_module

        gm = tmp_path / "gm"
        monkeypatch.setattr(skills_module, "GLOBAL_MEMORY_DIR", gm)
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", tmp_path / "gs")
        return gm

    def test_family_names_are_never_duplicate_candidates(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        make_entry(gm, "gws-gmail", "gmail shared conventions")
        make_entry(gm, "gws-gmail-send", "send a gmail message")
        make_entry(gm, "gws-gmail-reply", "reply to a gmail thread")
        make_entry(gm, "gws-gmail-reply-all", "reply-all to a gmail thread")
        import aish.skills as skills_module

        entries = skills_module.load_entries(str(tmp_path), None)
        pairs = dup_candidates(entries, lambda q, e: {id(x): 0.95 for x in e})
        family = {frozenset(("gws-gmail", "gws-gmail-send")),
                  frozenset(("gws-gmail-reply", "gws-gmail-reply-all"))}
        assert all(frozenset((a.name, b.name)) not in family for a, b, _ in pairs)

    def test_pinned_rule_cannot_be_disabled_by_verdict(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        path = make_entry(gm, "noisy", "always do the thing", extra="pinned: yes\n")
        state = tmp_path / "state"
        records = []
        for i in range(MIN_INJECTIONS):
            records += [
                knowledge([{"label": "noisy", "kind": "memory", "sim": 0.29}]),
                user(f"task {i}"),
            ]
        write_log(state, "session-20260728-100000-000001.jsonl", records)
        run_curate(judge=lambda p: "VERDICT: disable\nREASON: never used",
                   scores=lambda q, e: {}, notify_fn=lambda *a: None,
                   env={}, state_dir=state, now=NOW)
        text = path.read_text(encoding="utf-8")
        assert "status: disabled" not in text
        assert load_recent_actions(state, NOW)["noisy"] == "skip"

    def test_skill_never_loses_a_merge_to_a_memory(self, tmp_path, monkeypatch):
        gm = self._corpus(monkeypatch, tmp_path)
        skill_dir = tmp_path / "gs"
        skill = make_entry(skill_dir, "attachments-play", "handle chat attachments",
                           body="full playbook here")
        memory = make_entry(gm, "attachments-fact", "chat attachments live in uploads")
        state = tmp_path / "state"
        state.mkdir()
        run_curate(judge=lambda p: ("VERDICT: merge\nKEEP: attachments-fact\n"
                                    "REASON: same topic"),
                   scores=lambda q, e: {id(x): 0.9 for x in e},
                   notify_fn=lambda *a: None, env={}, state_dir=state, now=NOW)
        assert "status: disabled" not in skill.read_text(encoding="utf-8")
        assert "status: disabled" not in memory.read_text(encoding="utf-8")


# --- the rule ledger (#191) ------------------------------------------------


def rule_eval(turn, rows, ts="2026-07-28T10:00:00"):
    step = {"kind": "rule_eval", "turn": turn, "at": "seed",
            "corpus": {"total": len(rows), "active": len(rows), "skipped": []},
            "evaluated": rows, "truncated": 0}
    return {"ts": ts, "kind": "trace", "step": step}


def eval_row(rule, verdict, binding=None, origin="user"):
    row = {"rule": rule, "trigger": "message_shape", "tier": 0, "verdict": verdict,
           "evidence": {"origin": origin}, "ms": 0.1}
    if binding:
        row["binding"] = binding
    return row


def binding_rec(turn, rule, bid="b1", readers=("read_url",)):
    step = {"kind": "binding", "turn": turn, "id": bid, "rule": rule, "at": "seed",
            "tier": 0, "evidence": {},
            "obligations": [{"verb": "route", "to": "source", "of": "deliverable",
                             "readers": list(readers), "sources": ["https://x.test/a"]},
                            {"verb": "prohibit", "what": ["web_search"]}],
            "satisfiable": True, "unsatisfiable": [], "seeded": True}
    return {"ts": "2026-07-28T10:00:00", "kind": "trace", "step": step}


def gate_rec(turn, rule, verdict, call=1, bid="b1", escalated=False, rounds=1):
    step = {"kind": "gate", "turn": turn, "call": call, "at": "gate",
            "gate": "rule.prohibit", "binding": bid, "rule": rule,
            "tool": "web_search", "action": {}, "verdict": verdict, "tier": 0,
            "evidence": {}, "round": rounds, "max_rounds": 2, "escalated": escalated}
    return {"ts": "2026-07-28T10:00:00", "kind": "trace", "step": step}


def tool_rec(turn, name, call=2):
    step = {"kind": "tool", "turn": turn, "call": call, "name": name, "ok": True, "secs": 0.1}
    return {"ts": "2026-07-28T10:00:00", "kind": "trace", "step": step}


class TestRuleLedger:
    """Keyed to blast radius, not tier: a readable regex still has a bind rate
    and an override rate that are invisible without counting. A reader over the
    contract's records — no schema change, no model call."""

    def test_records_are_joined_by_TURN_not_by_position(self, tmp_path):
        """Governance records are emitted mid-turn, at turn end and from the
        server thread, so `_windows`' positional pairing cannot carry them.
        Here the gate record is written BEFORE its own rule_eval in the file."""
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            gate_rec(1, "bounded-material", "refused"),
            binding_rec(1, "bounded-material"),
            rule_eval(1, [eval_row("bounded-material", "bind", "b1")]),
        ])
        ledger = scan_rules(tmp_path, now=NOW)
        stat = ledger.rules["bounded-material"]
        assert stat.binds == 1 and stat.refusals == 1 and ledger.turns == 1

    def test_bind_rate_counts_every_turn_the_rule_was_evaluated_on(self, tmp_path):
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            rule_eval(1, [eval_row("r", "bind", "b1")]),
            binding_rec(1, "r"),
            rule_eval(2, [eval_row("r", "abstain")]),
            rule_eval(3, [eval_row("r", "abstain")]),
            rule_eval(4, [eval_row("r", "abstain")]),
        ])
        stat = scan_rules(tmp_path, now=NOW).rules["r"]
        assert stat.evaluated == 4 and stat.binds == 1 and stat.abstains == 3
        assert stat.bind_rate == 0.25

    def test_compliance_means_the_model_did_what_the_refusal_said(self, tmp_path):
        """Not merely 'it stopped pushing' — that counts giving up as
        compliance. The model must have called the reader it was pointed at."""
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            rule_eval(1, [eval_row("r", "bind", "b1")]),
            binding_rec(1, "r", readers=("read_url",)),
            gate_rec(1, "r", "refused"),
            tool_rec(1, "read_url"),
            rule_eval(2, [eval_row("r", "bind", "b1")]),
            binding_rec(2, "r", readers=("read_url",)),
            gate_rec(2, "r", "refused"),
            tool_rec(2, "read_docs"),  # went elsewhere: not compliance
        ])
        stat = scan_rules(tmp_path, now=NOW).rules["r"]
        assert stat.refusals == 2 and stat.complied == 1
        assert stat.compliance_rate == 0.5

    def test_an_owner_override_is_counted_apart_from_the_escalation(self, tmp_path):
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            rule_eval(1, [eval_row("r", "bind", "b1")]),
            binding_rec(1, "r"),
            gate_rec(1, "r", "allowed", escalated=True, rounds=3),
            rule_eval(2, [eval_row("r", "bind", "b1")]),
            binding_rec(2, "r"),
            gate_rec(2, "r", "refused", escalated=True, rounds=3),
        ])
        stat = scan_rules(tmp_path, now=NOW).rules["r"]
        assert stat.escalations == 2 and stat.overrides == 1
        assert stat.override_rate == 0.5

    def test_provenance_of_the_binding_is_kept(self, tmp_path):
        """A source the owner typed and a source named inside inbound mail are
        the same string and different facts."""
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            rule_eval(1, [eval_row("r", "bind", "b1", origin="user")]),
            rule_eval(2, [eval_row("r", "bind", "b1", origin="email")]),
        ])
        assert scan_rules(tmp_path, now=NOW).rules["r"].origins == {"user": 1, "email": 1}

    def test_a_pre_contract_log_contributes_nothing(self, tmp_path):
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            knowledge([{"label": "x", "kind": "memory", "sim": 0.4}]),
            user("hello"),
        ])
        ledger = scan_rules(tmp_path, now=NOW)
        assert ledger.turns == 0 and ledger.rules == {}

    def test_logs_outside_the_window_are_never_opened(self, tmp_path):
        write_log(tmp_path, "session-20250101-100000-000001.jsonl", [
            rule_eval(1, [eval_row("r", "bind", "b1")]),
        ])
        assert scan_rules(tmp_path, days=LEDGER_DAYS, now=NOW).turns == 0


class TestRuleSignals:
    """Proposals, never actions — a rule is owner property."""

    def _ledger(self, records):
        return records

    def test_a_rule_that_never_binds_is_flagged_as_dead_weight(self, tmp_path):
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            rule_eval(t, [eval_row("r", "abstain")]) for t in range(1, RULE_MIN_FIRES + 1)
        ])
        signals = dict(rule_signals(scan_rules(tmp_path, now=NOW)))
        assert "dead weight" in signals["r"]

    def test_a_broad_trigger_is_flagged_with_its_rate(self, tmp_path):
        records = []
        for turn in range(1, 5):
            records.append(rule_eval(turn, [eval_row("r", "bind", "b1")]))
            records.append(binding_rec(turn, "r"))
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", records)
        signals = dict(rule_signals(scan_rules(tmp_path, now=NOW)))
        assert "binds on 100% of turns" in signals["r"]

    def test_a_rule_the_owner_keeps_overriding_is_called_WRONG(self, tmp_path):
        records = []
        for turn in range(1, 5):
            records.append(rule_eval(turn, [eval_row("r", "bind", "b1")]))
            records.append(binding_rec(turn, "r"))
            records.append(gate_rec(turn, "r", "allowed", escalated=True, rounds=3))
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", records)
        signals = [s for name, s in rule_signals(scan_rules(tmp_path, now=NOW)) if name == "r"]
        assert any("the rule is wrong, not the model" in s for s in signals)

    def test_a_broken_rule_file_is_surfaced(self, tmp_path):
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            rule_eval(1, [eval_row("r", "error")]),
        ])
        signals = dict(rule_signals(scan_rules(tmp_path, now=NOW)))
        assert "broken file" in signals["r"]

    def test_a_small_sample_proposes_nothing(self, tmp_path):
        """Every rate below RULE_MIN_FIRES is noise, and a proposal made from
        noise is how a ledger loses the owner's trust."""
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            rule_eval(1, [eval_row("r", "abstain")]),
        ])
        assert rule_signals(scan_rules(tmp_path, now=NOW)) == []


class TestRuleLedgerHasAReader:
    """Counters nobody reads are the failure this epic is about. The scan
    needed a CALLER more than it needed more counters — it rides the weekly
    pass that already reads these logs."""

    def _state(self, tmp_path, records):
        state = tmp_path / "state"
        write_log(state, "session-20260728-100000-000001.jsonl", records)
        return state

    def _run(self, state, tmp_path, notify_fn=lambda *a: None, dry_run=True):
        logged = []
        original = curate_module.log
        curate_module.log = logged.append
        try:
            run_curate(
                dry_run=dry_run,
                judge=lambda _p: "skip",
                notify_fn=notify_fn,
                env={},
                state_dir=state,
                now=NOW,
            )
        finally:
            curate_module.log = original
        return logged

    def test_the_weekly_pass_reports_rule_signals(self, tmp_path):
        records = []
        for turn in range(1, 5):
            records.append(rule_eval(turn, [eval_row("r", "bind", "b1")]))
            records.append(binding_rec(turn, "r"))
            records.append(gate_rec(turn, "r", "allowed", escalated=True, rounds=3))
        logged = self._run(self._state(tmp_path, records), tmp_path)
        lines = [line for line in logged if line.startswith("rule r:")]
        assert lines, logged
        assert any("the rule is wrong, not the model" in line for line in lines)

    def test_it_proposes_and_never_acts(self, tmp_path):
        """A rule is owner property. The pass may say a rule looks wrong; it
        must not disable, edit or delete one."""
        source = Path(curate_module.__file__).read_text()
        scan = source[source.index("def scan_rules") : source.index("def rule_signals")]
        signals = source[source.index("def rule_signals") :]
        signals = signals[: signals.index("\ndef ")]
        for body in (scan, signals):
            for mutation in ("write_text", "update_entry_meta", "unlink", "os.remove"):
                assert mutation not in body

    def test_the_push_says_the_signals_changed_nothing(self, tmp_path):
        pushed = []
        records = []
        for turn in range(1, 5):
            records.append(rule_eval(turn, [eval_row("r", "bind", "b1")]))
            records.append(binding_rec(turn, "r"))
        self._run(
            self._state(tmp_path, records),
            tmp_path,
            notify_fn=lambda title, body: pushed.append((title, body)),
            dry_run=False,
        )
        assert pushed, "a rule signal must reach the owner, not only the log"
        assert "proposals only — nothing changed" in pushed[0][1]

    def test_a_dry_run_never_pushes(self, tmp_path):
        pushed = []
        records = [rule_eval(t, [eval_row("r", "abstain")]) for t in range(1, 5)]
        self._run(
            self._state(tmp_path, records),
            tmp_path,
            notify_fn=lambda title, body: pushed.append((title, body)),
        )
        assert pushed == []

    def test_a_week_with_nothing_to_curate_still_reports_a_bad_rule(self, tmp_path):
        """The early return a quiet week takes — the exact path where a rule
        the owner overrides every time it fires would otherwise stay silent."""
        pushed = []
        records = []
        for turn in range(1, 5):
            records.append(rule_eval(turn, [eval_row("r", "bind", "b1")]))
            records.append(binding_rec(turn, "r"))
            records.append(gate_rec(turn, "r", "allowed", escalated=True, rounds=3))
        logged = self._run(
            self._state(tmp_path, records),
            tmp_path,
            notify_fn=lambda title, body: pushed.append((title, body)),
            dry_run=False,
        )
        assert any("nothing to curate" in line for line in logged)
        assert pushed and "rule signal" in pushed[0][1]

    def test_a_corpus_with_no_rules_stays_quiet(self, tmp_path):
        logged = self._run(self._state(tmp_path, [user("hello")]), tmp_path)
        assert not [line for line in logged if line.startswith("rule ")]


def rating_rec(turn, rating, comment="", ts="2026-07-28T10:05:00"):
    return {"ts": ts, "kind": "rating", "turn": turn, "rating": rating, "comment": comment}


class TestRatingLedger:
    """#207's records, read by the weekly pass. No model, no judgement — the
    count is the thing."""

    def test_ratings_are_collected_by_turn(self, tmp_path):
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            user("one"), rating_rec("t1", "down", "stale price"),
            user("two"), rating_rec("t2", "up"),
        ])
        ratings = scan_ratings(tmp_path, now=NOW)
        assert ratings["t1"]["rating"] == "down"
        assert ratings["t1"]["comment"] == "stale price"
        assert ratings["t2"]["rating"] == "up"

    def test_the_reason_arrives_as_a_second_record_and_wins(self, tmp_path):
        """The tap records immediately so the count is never lost; a reason
        typed after is a second record for the same turn."""
        write_log(tmp_path, "session-20260728-100000-000001.jsonl", [
            rating_rec("t1", "down"),
            rating_rec("t1", "down", "it never read the page"),
        ])
        ratings = scan_ratings(tmp_path, now=NOW)
        assert len(ratings) == 1
        assert ratings["t1"]["comment"] == "it never read the page"

    def test_logs_outside_the_window_are_not_read(self, tmp_path):
        write_log(tmp_path, "session-20250101-100000-000001.jsonl", [rating_rec("t1", "up")])
        assert scan_ratings(tmp_path, now=NOW) == {}
