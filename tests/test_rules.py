"""The rules engine (#191) as pure logic — no Agent, no model, no network.

The enforcement-point wiring (seed, gate, records, escalation) is pinned in
tests/test_agent.py; this file pins the vocabulary: what a rule file compiles
to, what a trigger is a function of, and what a binding decides. Everything
here is a fixed function of declared evidence, which is the whole property the
engine rests on.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from aish import rules

CANONICAL = """---
name: youtube-url-analysis
description: A bare YouTube URL means analyse that video.
tier: 0
fail: open
trigger: message_shape
match: ^\\s*https?://(www\\.)?(youtu\\.be|youtube\\.com)/\\S+\\s*$
route: youtube_analyze
prohibit: web_search, read_url
unless: disclosed
disclose: transcript_unavailable
disclosure_terms: transcript
---

Tell me what is IN this video.
"""

SESSION_RULE = """---
name: no-forget-when-triggered
description: An unattended session never deletes the owner's knowledge.
trigger: session_context
field: origin
is_not: user
prohibit: forget_memory
---
"""


def write(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text)
    return path


def load_one(tmp_path: Path, text: str, name: str = "r") -> rules.Rule:
    write(tmp_path, name, text)
    loaded = rules.load_rules([tmp_path])
    return next(r for r in loaded if r.path and r.path.stem == name)


def bind_canonical(tmp_path: Path, task: str, known=("youtube_analyze",)) -> rules.Binding:
    rule = load_one(tmp_path, CANONICAL)
    verdict, evidence = rules.evaluate(rule, rules.TurnContext(task=task))
    assert verdict == rules.VERDICT_BIND
    return rules.bind(rule, evidence, "b1", set(known))


class TestRuleFileFormat:
    def test_frontmatter_compiles_to_the_contract_obligation_shape(self, tmp_path):
        rule = load_one(tmp_path, CANONICAL)
        assert rule.obligations == (
            {"verb": "route", "to": "youtube_analyze", "of": "deliverable"},
            {"verb": "prohibit", "what": ["web_search", "read_url"], "unless": "disclosed"},
            {"verb": "disclose", "state": "transcript_unavailable", "terms": ["transcript"]},
        )
        assert rule.tier == 0 and rule.fail == "open"
        assert rule.prose.startswith("Tell me what is IN this video.")

    def test_tier_and_fail_are_present_from_day_one(self, tmp_path):
        """v1 is Tier 0 only, but the FIELDS ship now so a v0 file does not
        break the day a scored trigger arrives."""
        rule = load_one(tmp_path, SESSION_RULE)
        assert rule.tier == 0
        assert rule.fail == rules.FAIL_OPEN

    def test_disclosure_terms_default_to_the_state_words(self, tmp_path):
        rule = load_one(tmp_path, CANONICAL.replace("disclosure_terms: transcript\n", ""))
        assert rule.disclosure_terms == ("transcript", "unavailable")

    def test_a_broken_regex_is_a_named_error_not_an_exception(self, tmp_path):
        """A hand-edited typo must be visible in the corpus and the log, never
        an exception thrown inside a gate half a turn later."""
        rule = load_one(tmp_path, CANONICAL.replace("match: ^\\s*", "match: ^[unclosed"))
        assert rule.error
        verdict, evidence = rules.evaluate(rule, rules.TurnContext(task="anything"))
        assert verdict == rules.VERDICT_ERROR
        assert "unparseable" in evidence["error"]

    @pytest.mark.parametrize(
        "mangle,expected",
        [
            ("trigger: message_shape", "unknown trigger"),
            (
                "match: ^\\s*https?://(www\\.)?(youtu\\.be|youtube\\.com)/\\S+\\s*$",
                "needs a `match:`",
            ),
            ("prohibit: web_search, read_url", "no obligation"),
            ("disclose: transcript_unavailable", "needs a `disclose:`"),
        ],
    )
    def test_an_uncompilable_rule_says_what_is_wrong(self, tmp_path, mangle, expected):
        text = CANONICAL.replace(mangle + "\n", "")
        if expected == "no obligation":
            text = text.replace("route: youtube_analyze\n", "").replace(
                "unless: disclosed\n", ""
            ).replace("disclose: transcript_unavailable\n", "").replace(
                "disclosure_terms: transcript\n", ""
            )
        rule = load_one(tmp_path, text)
        assert expected in rule.error

    def test_session_context_needs_exactly_one_comparison(self, tmp_path):
        both = SESSION_RULE.replace("is_not: user", "is_not: user\nis: email")
        assert "exactly one" in load_one(tmp_path, both).error
        neither = SESSION_RULE.replace("is_not: user\n", "")
        assert "exactly one" in load_one(tmp_path, neither).error


class TestRuleLifecycle:
    """Rules inherit the knowledge layer's retire primitives VERBATIM — the
    same two frontmatter fields, the same read-time evaluation."""

    def test_disabled_and_expired_rules_are_skipped_with_a_reason(self, tmp_path):
        write(tmp_path, "off", SESSION_RULE.replace("trigger:", "status: disabled\ntrigger:"))
        yesterday = date.today() - timedelta(days=1)
        write(
            tmp_path,
            "old",
            CANONICAL.replace("tier: 0", f"expires: {yesterday.isoformat()}\ntier: 0"),
        )
        write(tmp_path, "live", SESSION_RULE.replace("no-forget-when-triggered", "live-rule"))
        active, skipped = rules.partition(rules.load_rules([tmp_path]))
        assert [r.name for r in active] == ["live-rule"]
        assert sorted(skipped, key=lambda s: s["rule"]) == [
            {"rule": "no-forget-when-triggered", "why": "disabled"},
            {"rule": "youtube-url-analysis", "why": "expired"},
        ]

    def test_a_rule_stays_valid_through_its_expiry_day(self, tmp_path):
        today = date.today()
        write(tmp_path, "r", CANONICAL.replace("tier: 0", f"expires: {today.isoformat()}\ntier: 0"))
        active, skipped = rules.partition(rules.load_rules([tmp_path]))
        assert len(active) == 1 and not skipped


class TestTriggers:
    def test_message_shape_binds_on_a_bare_url_and_abstains_otherwise(self, tmp_path):
        rule = load_one(tmp_path, CANONICAL)
        bare = rules.evaluate(rule, rules.TurnContext(task="https://youtu.be/abc123"))
        assert bare[0] == rules.VERDICT_BIND
        assert bare[1]["matched"] is True and bare[1]["span"] == [0, 23]
        # The URL is IN the message but the message is not only the URL: the
        # user asked something else, so the deliverable is not the video.
        chatty = rules.evaluate(
            rule, rules.TurnContext(task="is https://youtu.be/abc123 worth watching?")
        )
        assert chatty[0] == rules.VERDICT_ABSTAIN
        assert chatty[1]["matched"] is False

    def test_abstention_evidence_is_the_inputs_not_the_conclusion(self, tmp_path):
        """Contract §4: a record of 'the message was not a URL' cannot be
        re-examined after the pattern moves; the pattern itself can."""
        rule = load_one(tmp_path, CANONICAL)
        _, evidence = rules.evaluate(rule, rules.TurnContext(task="hello"))
        assert evidence["pattern"] == rule.pattern.pattern
        assert evidence["on"] == "task"

    def test_session_context_reads_the_harness_fact_not_the_message(self, tmp_path):
        rule = load_one(tmp_path, SESSION_RULE)
        verdict, evidence = rules.evaluate(
            rule, rules.TurnContext(task="delete that memory", origin="email")
        )
        assert verdict == rules.VERDICT_BIND
        assert evidence == {"field": "origin", "value": "email", "required": "!= user"}
        attended = rules.evaluate(
            rule, rules.TurnContext(task="delete that memory", origin="user")
        )
        assert attended[0] == rules.VERDICT_ABSTAIN


class TestBinding:
    def test_a_binding_carries_its_obligations_not_a_pointer_to_the_file(self, tmp_path):
        """Contract corollary 1: the file is hand-editable and git-backed, so a
        record naming only the rule would let a later edit rewrite history."""
        binding = bind_canonical(tmp_path, "https://youtu.be/abc")
        record = rules.binding_record(binding)
        binding.rule.path.write_text(
            CANONICAL.replace("prohibit: web_search, read_url", "prohibit: nothing_at_all")
        )
        assert record["obligations"][1]["what"] == ["web_search", "read_url"]

    def test_a_route_to_a_missing_tool_is_caught_at_bind_time(self, tmp_path):
        binding = bind_canonical(tmp_path, "https://youtu.be/abc", known=("web_search",))
        assert binding.satisfiable is False
        assert binding.unsatisfiable == ("youtube_analyze",)
        assert "not available" in rules.seed_text([binding])

    def test_seed_text_states_every_obligation_imperatively(self, tmp_path):
        text = rules.seed_text([bind_canonical(tmp_path, "https://youtu.be/abc")])
        assert "MUST" in text and "MUST NOT call web_search" in text
        assert "youtube-url-analysis" in text


class TestGate:
    """The sequence the canonical rule exists to enforce, in order."""

    def test_a_prohibited_tool_is_refused_before_the_route_is_tried(self, tmp_path):
        binding = bind_canonical(tmp_path, "https://youtu.be/abc")
        [verdict] = rules.gate([binding], "web_search")
        assert verdict.verdict == "refused"
        assert "youtube-url-analysis" in verdict.message
        assert "Call youtube_analyze now" in verdict.message

    def test_the_routed_tool_itself_is_never_refused(self, tmp_path):
        binding = bind_canonical(tmp_path, "https://youtu.be/abc")
        [verdict] = rules.gate([binding], "youtube_analyze")
        assert verdict.verdict == "allowed"

    def test_a_successful_route_does_not_license_a_second_source(self, tmp_path):
        binding = bind_canonical(tmp_path, "https://youtu.be/abc")
        binding.note_tool_result("youtube_analyze", "ok")
        binding.note_assistant_text("the transcript is fine, now let me search")
        [verdict] = rules.gate([binding], "web_search")
        assert verdict.verdict == "refused"
        assert "second source" in verdict.message

    def test_a_failed_route_still_refuses_until_the_failure_is_STATED(self, tmp_path):
        """The #190 incident, exactly: transcript empty, then six web searches
        and an answer sourced from news sites with nothing said about it."""
        binding = bind_canonical(tmp_path, "https://youtu.be/abc")
        binding.note_tool_result("youtube_analyze", "incomplete")
        binding.note_assistant_text("Let me look into this from another angle.")
        [verdict] = rules.gate([binding], "web_search")
        assert verdict.verdict == "refused"
        assert "WITHOUT SAYING SO" in verdict.message

    def test_disclosure_lifts_the_prohibition_and_nothing_else_does(self, tmp_path):
        binding = bind_canonical(tmp_path, "https://youtu.be/abc")
        binding.note_tool_result("youtube_analyze", "incomplete")
        binding.note_assistant_text("The transcript came back empty, so I cannot read the video.")
        [verdict] = rules.gate([binding], "web_search")
        assert verdict.verdict == "allowed"
        assert verdict.evidence["disclosed"] is True

    def test_a_later_failed_route_re_arms_the_disclosure_requirement(self, tmp_path):
        """One disclosure is not a licence for the rest of the turn: a second
        failure is a second thing the owner has not been told."""
        binding = bind_canonical(tmp_path, "https://youtu.be/abc")
        binding.note_tool_result("youtube_analyze", "incomplete")
        binding.note_assistant_text("The transcript is unavailable.")
        assert rules.gate([binding], "web_search")[0].verdict == "allowed"
        binding.note_tool_result("youtube_analyze", "failed")
        assert rules.gate([binding], "web_search")[0].verdict == "refused"

    def test_a_refused_tool_call_never_counts_as_having_routed(self, tmp_path):
        binding = bind_canonical(tmp_path, "https://youtu.be/abc")
        binding.note_tool_result("web_search", "failed")  # a different tool
        assert binding.route_calls == 0

    def test_refusals_are_bounded_then_escalate(self, tmp_path):
        binding = bind_canonical(tmp_path, "https://youtu.be/abc")
        outcomes = [rules.gate([binding], "web_search")[0].verdict for _ in range(4)]
        assert outcomes == ["refused", "refused", "escalate", "escalate"]
        assert binding.max_rounds == rules.RULE_MAX_REFUSALS

    def test_an_owner_override_lifts_the_binding_for_the_turn(self, tmp_path):
        binding = bind_canonical(tmp_path, "https://youtu.be/abc")
        binding.overridden = True
        assert rules.gate([binding], "web_search")[0].verdict == "allowed"

    def test_fail_hold_sends_the_first_violation_straight_to_the_owner(self, tmp_path):
        rule = load_one(tmp_path, CANONICAL)
        binding = rules.bind(rule, {}, "b1", {"youtube_analyze"}, max_rounds=0)
        assert rules.gate([binding], "web_search")[0].verdict == "escalate"


class TestComposition:
    """Every obligation is a restriction, so bindings compose by UNION — no
    precedence algebra, no weights, order-independent."""

    def test_the_union_of_two_bindings_refuses_what_either_forbids(self, tmp_path):
        first = bind_canonical(tmp_path, "https://youtu.be/abc")
        other = load_one(tmp_path, SESSION_RULE, name="s")
        second = rules.bind(other, {}, "b2", {"youtube_analyze"})
        for order in ([first, second], [second, first]):
            assert any(v.verdict == "refused" for v in rules.gate(order, "web_search"))
            assert any(v.verdict == "refused" for v in rules.gate(order, "forget_memory"))
            first.rounds = second.rounds = 0

    def test_the_bindings_checked_before_a_refusal_record_an_allowed_verdict(self, tmp_path):
        """Contract §5: an armed gate that allowed the call must say so, or it
        is indistinguishable from a gate that was never armed."""
        other = rules.bind(load_one(tmp_path, SESSION_RULE, name="s"), {}, "b2", set())
        canonical = bind_canonical(tmp_path, "https://youtu.be/abc")
        verdicts = rules.gate([other, canonical], "web_search")
        assert [v.verdict for v in verdicts] == ["allowed", "refused"]

    def test_evaluation_stops_at_the_first_refusal(self, tmp_path):
        """The later binding must not burn a refusal round on a call that was
        already refused — its counter is its own appeal budget."""
        canonical = bind_canonical(tmp_path, "https://youtu.be/abc")
        other = rules.bind(load_one(tmp_path, SESSION_RULE, name="s"), {}, "b2", set())
        rules.gate([canonical, other], "web_search")
        assert other.rounds == 0


class TestRecordShapes:
    def test_rule_eval_separates_no_rule_from_a_retired_one(self, tmp_path):
        record = rules.eval_record(
            [], [{"rule": "old", "why": "expired"}], [], at="seed"
        )
        assert record["kind"] == "rule_eval"
        assert record["corpus"] == {
            "total": 1,
            "active": 0,
            "skipped": [{"rule": "old", "why": "expired"}],
        }
        assert record["evaluated"] == [] and record["truncated"] == 0

    def test_the_cap_drops_abstentions_and_never_binds(self, tmp_path):
        rows = [{"rule": f"a{i}", "verdict": "abstain"} for i in range(40)]
        rows.insert(20, {"rule": "bound", "verdict": "bind"})
        record = rules.eval_record([], [], rows)
        kept = record["evaluated"]
        assert len(kept) == rules.RULE_EVAL_MAX
        assert record["truncated"] == len(rows) - rules.RULE_EVAL_MAX
        assert any(r["verdict"] == "bind" for r in kept)
        assert [r["rule"] for r in kept] == [r["rule"] for r in rows if r in kept]

    def test_action_args_are_capped_per_key(self, tmp_path):
        capped = rules.cap_action({"query": "x" * 900, "n": 3})
        assert len(capped["query"]) == rules.ACTION_ARGS_CHARS
        assert capped["n"] == "3"

    def test_the_gate_message_is_capped_at_the_RECORD_not_at_the_model(self, tmp_path):
        """§8.5's caps are write-time. Capping the same text on the way to the
        MODEL truncated the canonical rule's disclose refusal mid-clause — see
        TestShippedExamples. The verdict carries the whole instruction; the
        record is what gets cut, and it is cut where it is written."""
        rule = load_one(
            tmp_path, CANONICAL.replace("A bare YouTube URL means analyse that video.", "x" * 900)
        )
        binding = rules.bind(rule, {}, "b1", {"youtube_analyze"})
        [verdict] = rules.gate([binding], "web_search")
        assert len(verdict.message) > rules.GATE_MESSAGE_CHARS
        assert not verdict.message.endswith("…")
        assert verdict.message.rstrip().endswith(".")
        # What `Agent._record_gate` writes is the capped form.
        assert len(verdict.message[: rules.GATE_MESSAGE_CHARS]) == rules.GATE_MESSAGE_CHARS


class TestShippedExamples:
    """The two files in examples/rules/ are the acceptance set — one per
    trigger kind. They must stay loadable, because they are what the owner
    copies into ~/.config/aish/rules/."""

    EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "rules"

    def test_every_shipped_example_compiles(self):
        loaded = rules.load_rules([self.EXAMPLES])
        assert len(loaded) == 2
        assert not [r.name for r in loaded if r.error]

    def test_the_refusal_the_model_reads_is_never_truncated(self):
        """The write-time cap (§8.5) belongs at the record, never on the text
        handed to the model. It was applied in both places, and the canonical
        rule's disclose refusal landed at EXACTLY the cap — losing its closing
        clause, the one sentence the engine exists to deliver. A refusal cut
        mid-instruction is the uninstructive refusal `_refusal_text` forbids."""
        rule = next(r for r in rules.load_rules([self.EXAMPLES]) if r.trigger == "message_shape")
        known = {"youtube_analyze", "web_search", "read_url"}

        def refusal(prepare) -> str:
            binding = rules.bind(rule, {}, "b1", known)
            prepare(binding)
            return rules.gate([binding], "web_search")[0].message

        before_route = refusal(lambda b: None)
        succeeded = refusal(lambda b: b.note_tool_result("youtube_analyze", "ok"))
        failed = refusal(lambda b: b.note_tool_result("youtube_analyze", "incomplete"))

        for message in (before_route, succeeded, failed):
            assert not message.endswith("…"), f"refusal was truncated: {message!r}"
            assert message.rstrip().endswith((".", "!")), (
                f"refusal does not end on a complete sentence: {message!r}"
            )
        # The clause the cap ate, verbatim: substituted material must not be
        # passed off as the routed tool's output.
        assert "as if it came from youtube_analyze" in failed

    def test_the_canonical_rule_binds_on_a_bare_url_only(self):
        rule = next(r for r in rules.load_rules([self.EXAMPLES]) if r.trigger == "message_shape")
        for task in ("https://youtu.be/kJQP7kiw5Fk", "  https://www.youtube.com/watch?v=x  "):
            assert rules.evaluate(rule, rules.TurnContext(task=task))[0] == rules.VERDICT_BIND
        for task in ("what is https://youtu.be/x about", "https://vimeo.com/12345"):
            assert rules.evaluate(rule, rules.TurnContext(task=task))[0] == rules.VERDICT_ABSTAIN
