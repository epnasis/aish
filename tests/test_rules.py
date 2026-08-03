"""The rules engine (#191) as pure logic — no Agent, no model, no network.

The enforcement-point wiring (seed, gate, records, escalation) is pinned in
tests/test_agent.py; this file pins the vocabulary: what a rule file compiles
to, what a trigger is a function of, and what a binding decides. Everything
here is a fixed function of declared evidence, which is the whole property the
engine rests on.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from aish import rules

CANONICAL = """---
name: bounded-material
description: Answer from the material I gave you.
when:
  prompt:
    has: material
then:
  answer_from: material
  never_use: [web_search]
---

Answer from the material the user gave you.
"""

PATTERN_RULE = """---
name: pattern-rule
description: An owner-written pattern, for shapes with no built-in detector.
when:
  prompt:
    matches: ^\\s*deploy\\b
then:
  never_use: [web_search]
---
"""

SESSION_RULE = """---
name: no-forget-when-triggered
description: An unattended session never deletes the owner's knowledge.
when:
  session:
    origin: automation
then:
  never_use: [forget_memory]
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


def bind_canonical(
    tmp_path: Path, task: str, known=("youtube_analyze", "read_url")
) -> rules.Binding:
    rule = load_one(tmp_path, CANONICAL)
    verdict, evidence = rules.evaluate(rule, rules.TurnContext(task=task))
    assert verdict == rules.VERDICT_BIND
    return rules.bind(rule, evidence, "b1", set(known))


class TestRuleFileFormat:
    def test_frontmatter_compiles_to_the_contract_obligation_shape(self, tmp_path):
        rule = load_one(tmp_path, CANONICAL)
        assert rule.obligations == (
            {"verb": "answer_from", "to": "material", "of": "deliverable"},
            {"verb": "never_use", "what": ["web_search"]},
        )
        assert rule.tier == 0 and rule.fail == rules.FAIL_DEFAULT
        assert rule.prose.startswith("Answer from the material the user gave you.")

    def test_tier_and_fail_are_present_from_day_one(self, tmp_path):
        """v1 is Tier 0 only, but the FIELDS ship now so a v0 file does not
        break the day a scored trigger arrives."""
        rule = load_one(tmp_path, SESSION_RULE)
        assert rule.tier == 0
        assert rule.fail == rules.FAIL_DEFAULT

    def test_a_rule_cannot_declare_words_that_lift_a_prohibition(self, tmp_path):
        """#191 A4. The gate has NO view of what the model said, and there is
        no frontmatter key that gives it one — a hand-written word list cannot
        cover a language, and similarity cannot tell asserting a failure from
        mentioning one. Whether the answer disclosed belongs to Verify."""
        source = Path(rules.__file__).read_text()
        # It survives only inside RETIRED_KEYS, which exists to REJECT it.
        assert source.count("disclosure_terms") == 1
        assert "disclosure_terms" in rules.RETIRED_KEYS
        assert not any(hasattr(rules.Binding, a) for a in ("disclosed", "disclosure_met"))

    def test_a_broken_regex_is_a_named_error_not_an_exception(self, tmp_path):
        """A hand-edited typo must be visible in the corpus and the log, never
        an exception thrown inside a gate half a turn later."""
        broken = PATTERN_RULE.replace("matches: ^\\s*deploy\\b", "matches: ^[unclosed")
        rule = load_one(tmp_path, broken)
        assert rule.error
        verdict, evidence = rules.evaluate(rule, rules.TurnContext(task="anything"))
        assert verdict == rules.VERDICT_ERROR
        assert "unparseable" in evidence["error"]

    @pytest.mark.parametrize(
        "text,expected",
        [
            (CANONICAL.replace("  prompt:\n    has: material\n", "  vibes: yes\n"),
             "unknown `when:` subject"),
            (CANONICAL.replace("has: material", "has: vibes"), "unknown `has:` value"),
            (
                CANONICAL.replace("    has: material\n", "").replace(
                    "  answer_from: material\n", ""
                ),
                "needs `has:` or `matches:`",
            ),
            (
                CANONICAL.replace("has: material", "has: material\n    matches: ^x"),
                "ONE of `has:`, `matches:` or `like:`",
            ),
            (CANONICAL.replace("has: material", "matches: ^x"), "needs `when: prompt: has:"),
            (
                CANONICAL.replace("  answer_from: material\n", "")
                .replace("  never_use: [web_search]\n", "")
                .replace("  never_use: [web_search]\n", ""),
                "no obligation",
            ),
        ],
    )
    def test_an_uncompilable_rule_says_what_is_wrong(self, tmp_path, text, expected):
        assert expected in load_one(tmp_path, text).error

    def test_session_origin_must_be_a_known_value(self, tmp_path):
        both = SESSION_RULE.replace("origin: automation", "origin: nonsense")
        assert "unknown `origin:` value" in load_one(tmp_path, both).error
        neither = SESSION_RULE.replace("    origin: automation\n", "")
        assert "needs `origin:`" in load_one(tmp_path, neither).error


class TestRuleLifecycle:
    """Rules inherit the knowledge layer's retire primitives VERBATIM — the
    same two frontmatter fields, the same read-time evaluation."""

    def test_disabled_and_expired_rules_are_skipped_with_a_reason(self, tmp_path):
        write(tmp_path, "off", SESSION_RULE.replace("when:", "enabled: false\nwhen:"))
        yesterday = date.today() - timedelta(days=1)
        write(
            tmp_path,
            "old",
            CANONICAL.replace("name: bounded-material",
                              f"name: bounded-material\nexpires: {yesterday.isoformat()}"),
        )
        write(tmp_path, "live", SESSION_RULE.replace("no-forget-when-triggered", "live-rule"))
        active, skipped = rules.partition(rules.load_rules([tmp_path]))
        assert [r.name for r in active] == ["live-rule"]
        assert sorted(skipped, key=lambda s: s["rule"]) == [
            {"rule": "bounded-material", "why": "expired"},
            {"rule": "no-forget-when-triggered", "why": "disabled"},
        ]

    def test_a_rule_stays_valid_through_its_expiry_day(self, tmp_path):
        today = date.today()
        write(
            tmp_path,
            "r",
            CANONICAL.replace(
                "name: bounded-material", f"name: bounded-material\nexpires: {today}"
            ),
        )
        active, skipped = rules.partition(rules.load_rules([tmp_path]))
        assert len(active) == 1 and not skipped


class TestTriggers:
    def test_an_owner_written_pattern_still_works(self, tmp_path):
        """`match:` remains for shapes with no built-in detector — command
        prefixes, file paths. What it must never do is stand in for intent."""
        rule = load_one(tmp_path, PATTERN_RULE)
        hit = rules.evaluate(rule, rules.TurnContext(task="deploy the web app"))
        assert hit[0] == rules.VERDICT_BIND
        assert hit[1]["matched"] is True and hit[1]["span"] == [0, 6]
        miss = rules.evaluate(rule, rules.TurnContext(task="what is deployment"))
        assert miss[0] == rules.VERDICT_ABSTAIN and miss[1]["matched"] is False

    def test_abstention_evidence_is_the_inputs_not_the_conclusion(self, tmp_path):
        """Contract §4: a record of 'the message did not match' cannot be
        re-examined after the pattern moves; the pattern itself can."""
        rule = load_one(tmp_path, PATTERN_RULE)
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
            CANONICAL.replace("never_use: [web_search]", "never_use: [nothing_at_all]")
        )
        assert record["obligations"][1]["what"] == ["web_search"]

    def test_seed_text_states_every_obligation_imperatively(self, tmp_path):
        text = rules.seed_text([bind_canonical(tmp_path, "https://youtu.be/abc")])
        assert "MUST" in text and "MUST NOT call web_search" in text
        assert "bounded-material" in text
        assert "ASK the user" in text  # the escape is stated, not only the ban


class TestSourceRouting:
    """`route: source` — the obligation names the source the OWNER handed over,
    and the harness resolves which reader can read it. One rule covers every
    source; nothing has to be maintained when a new reader appears (#191 A3)."""

    def test_a_youtube_link_routes_to_the_transcript_tool(self, tmp_path):
        binding = bind_canonical(tmp_path, "summarize https://youtu.be/kJQP7kiw5Fk")
        assert binding.readers == ("youtube_analyze",)
        assert binding.sources == ("https://youtu.be/kJQP7kiw5Fk",)

    def test_any_other_link_routes_to_the_page_reader(self, tmp_path):
        binding = bind_canonical(tmp_path, "who wrote https://example.com/post")
        assert binding.readers == ("read_url",)

    def test_two_sources_route_to_both_readers(self, tmp_path):
        binding = bind_canonical(
            tmp_path, "compare https://youtu.be/x with https://example.com/p"
        )
        assert set(binding.readers) == {"youtube_analyze", "read_url"}

    def test_the_binding_snapshots_what_the_source_resolved_to(self, tmp_path):
        """"source" alone would send a later reader back to guess which link
        was in the message — the resolution is turn-specific, so it is
        recorded (contract corollary 1)."""
        binding = bind_canonical(tmp_path, "https://youtu.be/x")
        route = rules.binding_record(binding)["obligations"][0]
        assert route["to"] == "material"
        assert route["readers"] == ["youtube_analyze"]
        assert route["sources"] == ["https://youtu.be/x"]
        assert "present" not in route  # a link is not already in context

    def test_a_missing_reader_is_unsatisfiable_at_bind_time(self, tmp_path):
        binding = bind_canonical(tmp_path, "https://youtu.be/x", known=("read_url",))
        assert binding.satisfiable is False
        assert binding.unsatisfiable == ("youtube_analyze",)
        assert "not available" in rules.seed_text([binding])

    @pytest.mark.parametrize(
        "url,reader",
        [
            ("https://youtu.be/x", "youtube_analyze"),
            ("https://www.youtube.com/watch?v=x", "youtube_analyze"),
            ("https://m.youtube.com/watch?v=x", "youtube_analyze"),
            ("https://music.youtube.com/watch?v=x", "youtube_analyze"),
            ("https://example.com/a", "read_url"),
            ("https://notyoutube.com/a", "read_url"),
            ("https://youtube.com.evil.test/a", "read_url"),
        ],
    )
    def test_the_reader_is_chosen_by_host_not_by_substring(self, url, reader):
        """A host is matched as a host, never as a substring: `youtube.com` in
        the middle of an attacker-chosen name is not YouTube."""
        assert rules.reader_for(url) == reader


class TestTriggerFindsSourcesNotIntent:
    """#191 A1/A2. The first version of this rule asked "is the message ONLY a
    link?" — sentence shape standing in for INTENT — and abstained on every
    way the owner actually types. Regex keeps the job it is honest at: finding
    the URLs. It never guesses what the request means."""

    @pytest.mark.parametrize(
        "task",
        [
            "https://youtu.be/kJQP7kiw5Fk",
            "summarize https://youtu.be/kJQP7kiw5Fk",
            "who is the author of https://youtu.be/kJQP7kiw5Fk",
            "what's their argument in https://youtu.be/x? in 3 bullets",
            "podsumuj https://youtu.be/x",
            "  <https://youtu.be/x>  ",
            "https://example.com/article — is this true?",
        ],
    )
    def test_a_message_carrying_a_source_binds_however_it_is_phrased(self, tmp_path, task):
        rule = load_one(tmp_path, CANONICAL)
        verdict, evidence = rules.evaluate(rule, rules.TurnContext(task=task))
        assert verdict == rules.VERDICT_BIND
        assert evidence["sources"] and evidence["sources"][0]["kind"] == "url"

    @pytest.mark.parametrize(
        "task",
        ["find me a hotel in Rome", "what is tar -xzf", "email me the report"],
    )
    def test_a_message_with_no_source_does_not_bind(self, tmp_path, task):
        rule = load_one(tmp_path, CANONICAL)
        verdict, evidence = rules.evaluate(rule, rules.TurnContext(task=task))
        assert verdict == rules.VERDICT_ABSTAIN
        assert evidence["sources"] == []


class TestGate:
    """The sequence the source-authority rule enforces."""

    def test_a_prohibited_tool_is_refused_before_the_source_is_read(self, tmp_path):
        binding = bind_canonical(tmp_path, "summarize https://youtu.be/x")
        [verdict] = rules.gate([binding], "web_search")
        assert verdict.verdict == "refused"
        assert "bounded-material" in verdict.message
        assert "Call youtube_analyze now" in verdict.message
        assert "ASK them in plain text" in verdict.message

    def test_the_routed_reader_itself_is_never_refused(self, tmp_path):
        binding = bind_canonical(tmp_path, "summarize https://youtu.be/x")
        assert rules.gate([binding], "youtube_analyze")[0].verdict == "allowed"

    def test_a_successful_read_does_not_license_a_second_source(self, tmp_path):
        binding = bind_canonical(tmp_path, "summarize https://youtu.be/x")
        binding.note_tool_result("youtube_analyze", "ok")
        [verdict] = rules.gate([binding], "web_search")
        assert verdict.verdict == "refused"
        assert "widen the material" in verdict.message

    def test_a_FAILED_read_still_does_not_license_a_second_source(self, tmp_path):
        """#190's incident: the transcript came back empty and six web searches
        followed. A dead source is a reason to say so and ask — never a licence
        to substitute."""
        binding = bind_canonical(tmp_path, "summarize https://youtu.be/x")
        binding.note_tool_result("youtube_analyze", "incomplete")
        [verdict] = rules.gate([binding], "web_search")
        assert verdict.verdict == "refused"
        assert "SAY SO" in verdict.message and "ASK" in verdict.message

    def test_NOTHING_the_model_says_can_lift_the_prohibition(self, tmp_path):
        """The anti-regression for #191 A4. v1 lifted the prohibition when the
        model's prose contained a declared word — a hand-written list that
        cannot cover a language, and that similarity cannot fix because
        "the transcript is unavailable" and "let me get the transcript another
        way" are the same topic and opposite meanings. The gate now has no
        view of model prose at all, in ANY language."""
        for said in (
            "The transcript is unavailable.",
            "Transkrypcja jest niedostępna.",
            "let me get the transcript another way",
            "",
        ):
            binding = bind_canonical(tmp_path, "summarize https://youtu.be/x")
            binding.note_tool_result("youtube_analyze", "incomplete")
            assert not hasattr(binding, "note_assistant_text")
            assert rules.gate([binding], "web_search")[0].verdict == "refused", said

    def test_a_refused_tool_call_never_counts_as_having_read_the_source(self, tmp_path):
        binding = bind_canonical(tmp_path, "summarize https://youtu.be/x")
        binding.note_tool_result("web_search", "failed")  # not a routed reader
        assert binding.route_calls == 0

    def test_refusals_are_bounded_then_escalate(self, tmp_path):
        binding = bind_canonical(tmp_path, "summarize https://youtu.be/x")
        outcomes = [rules.gate([binding], "web_search")[0].verdict for _ in range(4)]
        assert outcomes == ["refused", "refused", "escalate", "escalate"]
        assert binding.max_rounds == rules.RULE_MAX_REFUSALS

    def test_only_the_owner_can_lift_it(self, tmp_path):
        binding = bind_canonical(tmp_path, "summarize https://youtu.be/x")
        binding.overridden = True
        assert rules.gate([binding], "web_search")[0].verdict == "allowed"

    def test_fail_hold_sends_the_first_violation_straight_to_the_owner(self, tmp_path):
        rule = load_one(tmp_path, CANONICAL)
        binding = rules.bind(rule, {}, "b1", {"read_url"}, max_rounds=0)
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
            tmp_path, CANONICAL.replace("Answer from the material I gave you.", "x" * 900)
        )
        binding = rules.bind(rule, {}, "b1", {"read_url"})
        [verdict] = rules.gate([binding], "web_search")
        assert len(verdict.message) > rules.GATE_MESSAGE_CHARS
        assert not verdict.message.endswith("…")
        assert verdict.message.rstrip().endswith(".")
        # What `Agent._record_gate` writes is the capped form.
        assert len(verdict.message[: rules.GATE_MESSAGE_CHARS]) == rules.GATE_MESSAGE_CHARS


class TestShippedExamples:
    """The files in examples/rules/ are the acceptance set — one per trigger
    kind, plus one per enforcement point. They must stay loadable, because they
    are what the owner copies into ~/.config/aish/rules/."""

    EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "rules"

    def test_every_shipped_example_compiles(self):
        """They are what the owner copies into ~/.config/aish/rules/, so a
        broken one is a broken corpus, not a broken test fixture."""
        loaded = rules.load_rules([self.EXAMPLES])
        assert loaded, "the examples went missing"
        assert not [f"{r.name}: {r.error}" for r in loaded if r.error]

    def test_no_shipped_example_needs_a_verb_that_is_not_built(self):
        """An example that does not run is an example that teaches the wrong
        grammar. `always-use-show-image` shipped for months written against an
        unbuilt verb, warning on every session start until it was parked — the
        loudness was correct, having to park it was not."""
        loaded = rules.load_rules([self.EXAMPLES])
        parked = [r.name for r in loaded if r.status == "disabled"]
        assert not parked, f"a shipped example is disabled: {parked}"

    def test_the_examples_cover_every_enforcement_point(self):
        """Gate and Verify are enforced by different machinery and fail in
        different ways. The folder has to show both, or half the format is
        learned only from the docs."""
        obligations = {
            o["verb"] for r in rules.load_rules([self.EXAMPLES]) for o in r.obligations
        }
        assert obligations & rules.VERIFY_VERBS, "no example decides anything at turn end"
        assert obligations - rules.VERIFY_VERBS, "no example decides anything at the gate"

    def test_the_examples_cover_every_built_subject(self):
        """An example per subject is how the format is actually learned — by
        the owner and by a model reading the folder."""
        subjects = {r.trigger for r in rules.load_rules([self.EXAMPLES])}
        assert subjects >= {"message_shape", "session_context", "action_shape"}

    def _canonical(self):
        return next(
            r for r in rules.load_rules([self.EXAMPLES]) if r.trigger == "message_shape"
        )

    def test_the_refusal_the_model_reads_is_never_truncated(self):
        """The write-time cap belongs at the record, never on the text handed
        to the model. It was applied in both places and a refusal landed at
        EXACTLY the cap, losing its closing clause. A refusal cut mid-
        instruction is the uninstructive refusal `_refusal_text` forbids."""
        rule = self._canonical()
        known = {"youtube_analyze", "web_search", "read_url"}
        evidence = rules.evaluate(rule, rules.TurnContext(task="https://youtu.be/x"))[1]

        def refusal(prepare) -> str:
            binding = rules.bind(rule, evidence, "b1", known)
            prepare(binding)
            return rules.gate([binding], "web_search")[0].message

        messages = [
            refusal(lambda b: None),
            refusal(lambda b: b.note_tool_result("youtube_analyze", "ok")),
            refusal(lambda b: b.note_tool_result("youtube_analyze", "incomplete")),
        ]
        for message in messages:
            assert not message.endswith("…"), f"refusal was truncated: {message!r}"
            assert message.rstrip().endswith((".", "!")), (
                f"refusal does not end on a complete sentence: {message!r}"
            )
        # The clause the cap ate: substituted material must never be passed off
        # as the source's own.
        assert "as if it were theirs" in messages[-1]

    def test_the_canonical_rule_binds_however_the_request_is_phrased(self):
        """The v1 trigger ("the message is ONLY a link") abstained on every one
        of these — sentence shape standing in for intent (#191 A1)."""
        rule = self._canonical()
        for task in (
            "https://youtu.be/kJQP7kiw5Fk",
            "  https://www.youtube.com/watch?v=x  ",
            "summarize https://youtu.be/x",
            "who is the author of https://youtu.be/x",
            "what is https://youtu.be/x about",
            "https://vimeo.com/12345",
        ):
            assert rules.evaluate(rule, rules.TurnContext(task=task))[0] == rules.VERDICT_BIND
        for task in ("find me a hotel", "what is tar -xzf"):
            assert rules.evaluate(rule, rules.TurnContext(task=task))[0] == rules.VERDICT_ABSTAIN

    def test_a_vimeo_link_is_still_a_source_but_a_different_reader(self):
        """"YouTube or other for that matter" — the rule is about sources, not
        about YouTube. Only the reader differs."""
        rule = self._canonical()
        evidence = rules.evaluate(rule, rules.TurnContext(task="https://vimeo.com/12345"))[1]
        binding = rules.bind(rule, evidence, "b1", {"read_url", "youtube_analyze"})
        assert binding.readers == ("read_url",)


class TestRetiredKeys:
    """Removing a format that had already governed live turns. A retired key
    must fail LOUDLY and name its replacement: a file that reads as if it still
    works, and quietly does not, is worse than one that stops loading. Azure
    silently ignores unknown fields — exactly the wrong behaviour for a file
    the owner hand-edits."""

    # The v1 format, verbatim. Every key in it is now retired.
    OLD_FORMAT = """---
name: bounded-material
description: Answer from the material I gave you.
tier: 0
fail: open
trigger: message_shape
contains: source
route: source
prohibit: web_search
unless: disclosed
disclosure_terms: transcript
disclose: source_unavailable
---

Prose.
"""

    @pytest.mark.parametrize(
        "key,names",
        [
            ("trigger", "`when: prompt:`"),
            ("contains", "`when: prompt: has:`"),
            ("route", "`then: answer_from:`"),
            ("prohibit", "`then: never_use:`"),
            ("disclose", "answer_must_include"),
            ("tier", "the trigger's own form"),
            ("fail", "`if_unsure:"),
            ("unless", "only the owner can lift it"),
            ("disclosure_terms", "no longer reads the model's prose"),
        ],
    )
    def test_every_retired_key_names_its_replacement(self, tmp_path, key, names):
        """Not just "this is wrong" — WHAT to write instead. A rule file is
        edited by someone who will not go and read the source."""
        assert key in rules.RETIRED_KEYS
        assert names in rules.RETIRED_KEYS[key], rules.RETIRED_KEYS[key]

    def test_a_file_in_the_old_format_fails_loudly_and_binds_nothing(self, tmp_path):
        rule = load_one(tmp_path, self.OLD_FORMAT)
        assert "was retired" in rule.error
        assert rule.obligations == ()
        verdict, evidence = rules.evaluate(rule, rules.TurnContext(task="https://x.test/a"))
        assert verdict == rules.VERDICT_ERROR and "retired" in evidence["error"]

    def test_the_gate_has_no_key_that_could_read_the_model_s_prose(self, tmp_path):
        """#191 A4, still true after the rename: no frontmatter key gives the
        gate a view of what the model SAID, in any language."""
        source = Path(rules.__file__).read_text()
        assert source.count("disclosure_terms") == 1  # only inside RETIRED_KEYS
        assert not any(hasattr(rules.Binding, a) for a in ("disclosed", "disclosure_met"))

class TestTheWholeMaterialChannel:
    """An attachment is the LEAST ambiguous "here, answer from this" there is —
    and it was invisible to the first detector, because attachments reach the
    agent as separate parameters and never appear in the message text at all.
    A rule that covers links but not the PDF you just dropped in covers the
    easy half of its own subject."""

    def _ctx(self, task="", **kw):
        return rules.TurnContext(task=task, **kw)

    def _evaluate(self, tmp_path, ctx):
        return rules.evaluate(load_one(tmp_path, CANONICAL), ctx)

    def test_an_attached_document_binds_with_no_url_in_sight(self, tmp_path):
        verdict, evidence = self._evaluate(
            tmp_path, self._ctx("summarize this", documents=("/tmp/report.pdf",))
        )
        assert verdict == rules.VERDICT_BIND
        assert evidence["sources"] == [{"ref": "/tmp/report.pdf", "kind": "attachment"}]

    def test_an_attached_image_binds_too(self, tmp_path):
        verdict, evidence = self._evaluate(
            tmp_path, self._ctx("what is this?", images=("/tmp/shot.png",))
        )
        assert verdict == rules.VERDICT_BIND
        assert evidence["sources"][0]["kind"] == "attachment"

    def test_attached_material_needs_no_reader_and_says_so(self, tmp_path):
        """The second shape of `route`: the material is already in context, so
        the obligation is satisfied by construction. Recorded, not hidden."""
        rule = load_one(tmp_path, CANONICAL)
        _, evidence = rules.evaluate(
            rule, self._ctx("summarize this", documents=("/tmp/report.pdf",))
        )
        binding = rules.bind(rule, evidence, "b1", {"read_url", "read_file"})
        assert binding.readers == ()
        assert binding.present == ("/tmp/report.pdf",)
        assert binding.satisfiable is True  # nothing to call cannot be missing
        route = rules.binding_record(binding)["obligations"][0]
        assert route["present"] == ["/tmp/report.pdf"]
        assert "already in front of you" in rules.seed_text([binding])

    def test_the_prohibition_still_bites_for_attached_material(self, tmp_path):
        """The half that does the work when there is no reader to call."""
        rule = load_one(tmp_path, CANONICAL)
        _, evidence = rules.evaluate(
            rule, self._ctx("summarize this", documents=("/tmp/report.pdf",))
        )
        binding = rules.bind(rule, evidence, "b1", {"read_url"})
        [verdict] = rules.gate([binding], "web_search")
        assert verdict.verdict == "refused"
        assert "already in front of you" in verdict.message
        assert "ASK them" in verdict.message

    @pytest.mark.parametrize(
        "task,ref",
        [
            ("summarize ~/Downloads/report.pdf", "~/Downloads/report.pdf"),
            ("read /var/log/system.log please", "/var/log/system.log"),
            ("what does ./notes.md say", "./notes.md"),
            ("check ../other/data.csv", "../other/data.csv"),
            ("summarize report.pdf", "report.pdf"),
            ("what is in agent.py", "agent.py"),
        ],
    )
    def test_a_path_the_owner_typed_is_material_too(self, tmp_path, task, ref):
        verdict, evidence = self._evaluate(tmp_path, self._ctx(task))
        assert verdict == rules.VERDICT_BIND
        assert evidence["sources"] == [{"ref": ref, "kind": "path"}]

    def test_a_named_path_routes_to_the_file_reader(self, tmp_path):
        rule = load_one(tmp_path, CANONICAL)
        _, evidence = rules.evaluate(rule, self._ctx("summarize ~/report.pdf"))
        binding = rules.bind(rule, evidence, "b1", {"read_file", "read_url"})
        assert binding.readers == ("read_file",)

    @pytest.mark.parametrize(
        "task",
        [
            "find me a hotel in Rome",
            "what is tar -xzf",
            "the version is 1.2.3",
            "i.e. the second one",
            "run it e.g. tomorrow",
            "email me at pawel@wenda.eu",
            "is 3.5 better than 4.0",
        ],
    )
    def test_prose_is_not_mistaken_for_material(self, tmp_path, task):
        """The bare-filename form is the one that can false-positive, so it is
        held to a known extension list rather than 'anything with a dot'."""
        verdict, evidence = self._evaluate(tmp_path, self._ctx(task))
        assert verdict == rules.VERDICT_ABSTAIN, evidence["sources"]

    def test_a_url_is_never_double_counted_as_a_path(self, tmp_path):
        _, evidence = self._evaluate(tmp_path, self._ctx("summarize https://x.test/a/b.pdf"))
        assert [s["kind"] for s in evidence["sources"]] == ["url"]

    def test_attachments_and_links_compose_into_one_material_set(self, tmp_path):
        _, evidence = self._evaluate(
            tmp_path,
            self._ctx("compare this with https://x.test/a", documents=("/tmp/r.pdf",)),
        )
        assert [s["kind"] for s in evidence["sources"]] == ["attachment", "url"]

    def test_the_narrow_url_detector_still_ignores_attachments(self, tmp_path):
        """`contains: url` is the narrower detector, kept for a rule that
        really does mean web pages only. It must not silently widen."""
        rule = load_one(tmp_path, CANONICAL.replace("has: material", "has: link"))
        verdict, _ = rules.evaluate(
            rule, self._ctx("summarize this", documents=("/tmp/report.pdf",))
        )
        assert verdict == rules.VERDICT_ABSTAIN

    def test_provenance_is_carried_for_attached_material_too(self, tmp_path):
        """An attachment the owner uploaded and one that arrived on inbound
        mail are the same bytes and different facts."""
        _, evidence = self._evaluate(
            tmp_path, self._ctx("summarize", documents=("/tmp/r.pdf",), origin="email")
        )
        assert evidence["origin"] == "email"

    def test_a_natively_delivered_attachment_is_ONE_source_not_two(self, tmp_path):
        """The web server names an attachment BOTH ways: an image the backend
        can see is passed as a parameter AND announced in the text as
        "[image attached: … file at <path>]". The path detector would find that
        same path, so dedup by ref must collapse them — and the ATTACHMENT
        reading must win, because the material really is in context and there
        is nothing to call."""
        path = "/tmp/uploads/shot.png"
        _, evidence = self._evaluate(
            tmp_path,
            self._ctx(f"what is this? [image attached: shot.png — file at {path}]",
                      images=(path,)),
        )
        assert evidence["sources"] == [{"ref": path, "kind": "attachment"}]

    def test_a_file_the_backend_could_NOT_take_natively_routes_to_read_file(self, tmp_path):
        """The third shape the server produces: no parameter, just
        "[attached file: <path>]". The model must read it — so the route
        resolves to a reader rather than to `present`."""
        path = "/tmp/uploads/report.txt"
        rule = load_one(tmp_path, CANONICAL)
        _, evidence = rules.evaluate(rule, self._ctx(f"summarize this [attached file: {path}]"))
        assert evidence["sources"] == [{"ref": path, "kind": "path"}]
        binding = rules.bind(rule, evidence, "b1", {"read_file", "read_url"})
        assert binding.readers == ("read_file",) and binding.present == ()

    def test_keep_in_mind_is_refused_with_the_admission_line(self, tmp_path):
        """The verb that was proposed, tried and deleted. An unenforced verb
        inside an enforcement engine is the costume the owner objected to, so
        the refusal states the admission line AND both destinations — the build
        queue if a check is buildable, memory if nothing could ever check it."""
        rule = load_one(tmp_path, CANONICAL.replace(
            "  never_use: [web_search]", "  keep_in_mind: be nice about it"
        ))
        assert "`keep_in_mind:` was retired" in rule.error
        assert "compiles to a declared check" in rule.error
        assert "belongs in memory" in rule.error

    def test_the_designed_verbs_say_what_is_MISSING_not_just_that_it_is_wrong(
        self, tmp_path, monkeypatch
    ):
        """A refused rule is a legible gap — the owner can decide to rephrase
        toward what exists, or to extend the engine. "Not expressible" alone
        gives him neither choice.

        `VERBS_DESIGNED` is EMPTY now: every verb the design named is built, and
        `ask_me_first` — which used to be this test's example — was the last
        one. The machinery is what is being pinned, not the entry, because the
        next designed verb must arrive with this behaviour already working.
        """
        monkeypatch.setitem(rules.VERBS_DESIGNED, "answer_in_my_language",
                            "match the language I wrote in — needs a judge")
        rule = load_one(tmp_path, CANONICAL.replace(
            "  never_use: [web_search]", "  answer_in_my_language: true"
        ))
        assert "designed but not built yet" in rule.error
        assert "match the language I wrote in" in rule.error
        assert ", ".join(sorted(rules.VERBS)) in rule.error

    def test_every_designed_verb_is_now_built(self):
        """The build queue, as a test. An entry here is a rule the owner can
        write that will loudly fail to load — which is the intended behaviour,
        but it should be a deliberate state rather than a forgotten one."""
        assert rules.VERBS_DESIGNED == {}
        assert rules.SUBJECTS_DESIGNED == ()

    def test_a_phrase_only_a_judge_could_check_is_refused_by_name(self, tmp_path):
        """The admission line, at the one place it is tempting to bend: a verb
        ships only if it compiles to a declared check. "be less annoying" is a
        judged question, and the judged tier is not built — so it is refused
        with the two structural forms that ARE available, rather than shipping
        as a promise nothing keeps."""
        rule = load_one(tmp_path, CANONICAL.replace(
            "  never_use: [web_search]", "  answer_must_not_include: be less annoying about it"
        ))
        assert "only a judge can check" in rule.error
        assert "picture" in rule.error and "pattern" in rule.error


class TestEnabledFlag:
    def test_enabled_false_retires_a_rule(self, tmp_path):
        write(tmp_path, "r", CANONICAL.replace("name: bounded-material",
                                               "name: bounded-material\nenabled: false"))
        active, skipped = rules.partition(rules.load_rules([tmp_path]))
        assert not active and skipped == [{"rule": "bounded-material", "why": "disabled"}]

    def test_a_rule_is_enabled_unless_it_says_otherwise(self, tmp_path):
        active, _ = rules.partition([load_one(tmp_path, CANONICAL)])
        assert len(active) == 1

    def test_status_disabled_is_retired_and_says_so(self, tmp_path):
        """`status:` reads like a field you report, not one you set."""
        rule = load_one(tmp_path, CANONICAL.replace("name: bounded-material",
                                                    "name: bounded-material\nstatus: disabled"))
        assert "`status:` was retired" in rule.error and "enabled: false" in rule.error


ACTION_RULE = """---
name: never-edit-aish-itself
description: aish's own source changes through issues, not directly.
when:
  action:
    path_under: {root}
then:
  never_use: [write_file, edit_file]
---

File an issue instead. The config directory is fine to change.
"""

ALWAYS_RULE = """---
name: always-on
description: Applies to every turn.
when: always
then:
  never_use: [web_search]
---
"""


class TestActionSubject:
    """`when: action:` asks about a call aish is ABOUT to make — the cheapest
    enforcement in the vocabulary, because the gate it needs already exists and
    every field is a fact the harness holds before dispatch."""

    def _bound(self, tmp_path, root, known=("write_file", "edit_file")):
        rule = load_one(tmp_path, ACTION_RULE.format(root=root))
        assert not rule.error, rule.error
        verdict, evidence = rules.evaluate(rule, rules.TurnContext(task="fix a bug"))
        assert verdict == rules.VERDICT_BIND
        return rules.bind(rule, evidence, "b1", set(known))

    def test_it_arms_at_seed_and_decides_at_the_gate(self, tmp_path):
        """The condition is about a call nobody has proposed yet, so binding
        is not "it applied" — it is "it is watching". The gate records say
        whether it ever fired."""
        binding = self._bound(tmp_path, str(tmp_path / "src"))
        assert binding.evidence == {
            "on": "action", "conditions": {"path_under": str(tmp_path / "src")}
        }

    def test_a_write_inside_the_path_is_refused(self, tmp_path):
        binding = self._bound(tmp_path, str(tmp_path / "src"))
        [verdict] = rules.gate([binding], "write_file", {"path": str(tmp_path / "src/a.py")})
        assert verdict.verdict == "refused"
        assert "never-edit-aish-itself" in verdict.message

    def test_a_write_OUTSIDE_the_path_is_untouched(self, tmp_path):
        """The owner's requirement exactly: the source is off-limits, the
        config beside it is his to change."""
        binding = self._bound(tmp_path, str(tmp_path / "src"))
        [verdict] = rules.gate([binding], "write_file", {"path": str(tmp_path / "config/a.md")})
        assert verdict.verdict == "allowed"

    def test_a_tool_the_rule_does_not_name_is_untouched(self, tmp_path):
        binding = self._bound(tmp_path, str(tmp_path / "src"))
        [verdict] = rules.gate([binding], "read_file", {"path": str(tmp_path / "src/a.py")})
        assert verdict.verdict == "allowed"

    def test_dot_dot_cannot_walk_out_of_the_condition(self, tmp_path):
        """Resolved, never string-matched. A path condition that `..` steps
        around protects nothing — and the direction that matters is that a
        path LEAVING the root stops matching, not that it starts."""
        binding = self._bound(tmp_path, str(tmp_path / "src"))
        outside = str(tmp_path / "src" / ".." / "elsewhere" / "a.py")
        assert rules.gate([binding], "write_file", {"path": outside})[0].verdict == "allowed"

    def test_a_path_inside_reached_through_dot_dot_still_matches(self, tmp_path):
        """The mirror: resolution must not become an escape hatch either."""
        binding = self._bound(tmp_path, str(tmp_path / "src"))
        inside = str(tmp_path / "other" / ".." / "src" / "a.py")
        assert rules.gate([binding], "write_file", {"path": inside})[0].verdict == "refused"

    def test_a_relative_path_resolves_against_the_session_cwd(self, tmp_path):
        binding = self._bound(tmp_path, str(tmp_path / "src"))
        verdict = rules.gate([binding], "write_file", {"path": "a.py"},
                             cwd=str(tmp_path / "src"))[0]
        assert verdict.verdict == "refused"

    def test_a_path_named_inside_a_shell_command_counts(self, tmp_path):
        """`write_file` is not the only way to change a file."""
        rule = load_one(tmp_path, ACTION_RULE.format(root=str(tmp_path / "src")).replace(
            "never_use: [write_file, edit_file]", "never_use: [run_command]"
        ))
        _, evidence = rules.evaluate(rule, rules.TurnContext(task="x"))
        binding = rules.bind(rule, evidence, "b1", {"run_command"})
        command = f"sed -i '' s/a/b/ {tmp_path}/src/a.py"
        assert rules.gate([binding], "run_command", {"command": command})[0].verdict == "refused"

    def test_the_tool_shorthand_expands(self, tmp_path):
        """`when: {action: remember}` is the common case; the long form is
        ceremony for it. The record always carries the expanded condition."""
        rule = load_one(tmp_path, ALWAYS_RULE.replace(
            "when: always", "when:\n  action: remember"
        ).replace("never_use: [web_search]", "never_use: [forget_memory]"))
        assert not rule.error, rule.error
        assert rule.action == {"tool": "remember"}

    @pytest.mark.parametrize("field", ["sends_to", "host"])
    def test_the_unbuilt_fields_say_what_they_need(self, tmp_path, field):
        rule = load_one(tmp_path, ACTION_RULE.format(root="/x").replace(
            "path_under: /x", f"{field}: someone@example.com"
        ))
        assert "designed but not built yet" in rule.error
        assert "recipient/host parse" in rule.error

    def test_an_unknown_action_field_lists_the_real_ones(self, tmp_path):
        rule = load_one(tmp_path, ACTION_RULE.format(root="/x").replace(
            "path_under: /x", "vibes: bad"
        ))
        assert "unknown `action:` field" in rule.error
        assert "command_starts_with" in rule.error


class TestAlwaysSubject:
    """A rule with no condition. Style obligations apply to every turn, and
    spelling that out beats an empty block that reads like an oversight."""

    def test_it_binds_on_every_turn(self, tmp_path):
        rule = load_one(tmp_path, ALWAYS_RULE)
        assert not rule.error, rule.error
        for task in ("anything", "", "https://x.test/a"):
            verdict, evidence = rules.evaluate(rule, rules.TurnContext(task=task))
            assert verdict == rules.VERDICT_BIND and evidence == {"on": "always"}

    def test_it_gates_like_any_other_binding(self, tmp_path):
        rule = load_one(tmp_path, ALWAYS_RULE)
        _, evidence = rules.evaluate(rule, rules.TurnContext(task="x"))
        binding = rules.bind(rule, evidence, "b1", {"web_search"})
        assert rules.gate([binding], "web_search")[0].verdict == "refused"
        assert rules.gate([binding], "read_file")[0].verdict == "allowed"

    @pytest.mark.parametrize(
        "command,expected",
        [
            ("git status", "allowed"),
            ("git commit -m 'fix the parser'", "allowed"),
            ("uv run pytest -q", "allowed"),
            ("ls", "allowed"),
        ],
    )
    def test_a_bare_word_in_a_command_is_not_a_path(self, tmp_path, command, expected):
        """Found by running the real rule: every word in a command resolved
        against cwd, so `git status` run from INSIDE the protected tree matched
        a `path_under` condition naming it — the rule would have refused every
        command in the directory it guards. A token that is not path-shaped is
        never resolved, the same discipline the approval gate uses."""
        rule = load_one(tmp_path, ACTION_RULE.format(root=str(tmp_path)).replace(
            "never_use: [write_file, edit_file]", "never_use: [run_command]"
        ))
        _, evidence = rules.evaluate(rule, rules.TurnContext(task="x"))
        binding = rules.bind(rule, evidence, "b1", {"run_command"})
        verdict = rules.gate([binding], "run_command", {"command": command},
                             cwd=str(tmp_path))[0]
        assert verdict.verdict == expected

    def test_a_path_shaped_token_in_a_command_still_counts(self, tmp_path):
        """The other half: the filter must not become the escape."""
        rule = load_one(tmp_path, ACTION_RULE.format(root=str(tmp_path / "src")).replace(
            "never_use: [write_file, edit_file]", "never_use: [run_command]"
        ))
        _, evidence = rules.evaluate(rule, rules.TurnContext(task="x"))
        binding = rules.bind(rule, evidence, "b1", {"run_command"})
        for command in (f"echo x > {tmp_path}/src/a.py", "rm ./src/a.py"):
            binding.rounds = 0
            verdict = rules.gate([binding], "run_command", {"command": command},
                                 cwd=str(tmp_path))[0]
            assert verdict.verdict == "refused", command


class TestAuthoring:
    """The model names field values; the TOOL renders the file. #205's exhibit
    is a rule aish wrote itself that loaded as `error: a rule with no obligation
    restricts nothing` and had been inert since the day it was written."""

    BASE = {
        "name": "bounded-material",
        "description": "Answer from the material I gave you.",
        "when_subject": "prompt",
        "when_has": "material",
        "answer_from": "material",
        "never_use": ["web_search"],
    }

    def test_rendered_fields_round_trip_through_the_parser(self):
        """The only claim that matters: what render() emits, _parse() reads
        back as the rule that was asked for."""
        rule, errors = rules.lint(rules.render(self.BASE))
        assert not errors and rule is not None
        assert rule.name == "bounded-material"
        assert rule.trigger == rules.TRIGGER_MESSAGE_SHAPE
        verbs = {o["verb"] for o in rule.obligations}
        assert verbs == {rules.VERB_ANSWER_FROM, rules.VERB_NEVER_USE}

    def test_a_value_that_would_break_yaml_is_quoted(self):
        """The renderer exists so no author has to know which values need
        quoting — a colon in a description is the ordinary case, not an edge."""
        rule, errors = rules.lint(rules.render(
            {**self.BASE, "description": "material: use it, don't widen it"}
        ))
        assert not errors and rule is not None
        assert rule.description == "material: use it, don't widen it"

    def test_a_rule_with_no_obligation_is_refused_at_render(self):
        """#205's exhibit, caught before a file exists rather than months
        after: it put the obligation in PROSE, which restricts nothing."""
        fields = {k: v for k, v in self.BASE.items()
                  if k not in ("answer_from", "never_use")}
        with pytest.raises(rules.LintError) as exc:
            rules.render({**fields, "prose": "You MUST use show_image."})
        assert "restricts nothing" in str(exc.value)

    def test_an_unknown_field_names_what_is_available(self):
        with pytest.raises(rules.LintError) as exc:
            rules.render({**self.BASE, "tier": 0})
        assert "unknown field 'tier'" in str(exc.value)
        assert "when_subject" in str(exc.value)

    def test_a_rule_naming_a_missing_tool_does_not_pass_lint(self):
        """A route to a tool that does not exist refuses every alternative and
        offers nothing, on every turn it binds."""
        text = rules.render({**self.BASE, "answer_from": "gws_gmial_send"})
        rule, errors = rules.lint(text, capabilities={"read_url", "web_search"})
        assert rule is None
        assert "gws_gmial_send" in errors[0]

    def test_a_prohibition_on_a_missing_tool_is_caught_too(self):
        """It would never fire — and a rule that never fires looks exactly like
        a rule that is working."""
        text = rules.render({**self.BASE, "never_use": ["web_serch"]})
        rule, errors = rules.lint(text, capabilities={"read_url", "web_search"})
        assert rule is None and "web_serch" in errors[0]

    def test_a_trigger_naming_a_missing_tool_is_caught_too(self):
        """A typo'd `action: tool:` arms every turn and fires on nothing — and
        it is the one trigger kind retro-match cannot replay, so neither
        honesty mechanism would have caught it."""
        text = rules.render({
            "name": "r", "description": "d", "when_subject": "action",
            "when_action": {"tool": "gws_gmial_send"}, "never_use": ["web_search"],
        })
        rule, errors = rules.lint(text, capabilities={"web_search", "read_url"})
        assert rule is None and "gws_gmial_send" in errors[0]

    def test_a_trigger_on_a_path_is_not_mistaken_for_a_tool(self):
        text = rules.render({
            "name": "r", "description": "d", "when_subject": "action",
            "when_action": {"path_under": "~/dev/aish"}, "never_use": ["write_file"],
        })
        rule, errors = rules.lint(text, capabilities={"write_file"})
        assert not errors and rule is not None

    def test_editing_carries_over_everything_not_named(self):
        """#205's sharpest risk: the compiler regenerating a working rule from
        one sentence and silently dropping the four things it already did."""
        original = rules.render(self.BASE)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bounded-material.md"
            path.write_text(original, encoding="utf-8")
            fields = {**rules.author_fields(path), "must_first": "read_url"}
        rule, errors = rules.lint(rules.render(fields))
        assert not errors and rule is not None
        verbs = {o["verb"] for o in rule.obligations}
        assert verbs == {
            rules.VERB_ANSWER_FROM, rules.VERB_NEVER_USE, rules.VERB_MUST_FIRST,
        }, "an edit dropped an obligation it was never asked to touch"

    def test_the_prose_body_survives_an_edit_verbatim(self):
        body = "Widening quietly costs them the ability to trust any answer."
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.md"
            path.write_text(rules.render({**self.BASE, "prose": body}), encoding="utf-8")
            fields = rules.author_fields(path)
        assert body in rules.render({**fields, "description": "changed"})

    def test_explain_says_when_and_what_without_naming_yaml(self):
        """What the owner approves is the MEANING. He did not write the file
        and should not have to audit it."""
        rule, _ = rules.lint(rules.render(self.BASE))
        text = rules.explain(rule)
        assert "material" in text and "web_search" in text
        assert "when:" not in text and "then:" not in text

    def test_explain_names_the_moment_a_verify_rule_is_checked(self):
        rule, _ = rules.lint(rules.render({
            "name": "live-price", "description": "Prices come from the store.",
            "when_subject": "always", "must_first": "read_url",
        }))
        assert "before you see it" in rules.explain(rule)

    def test_a_retired_rule_says_so_in_its_own_card(self):
        rule, _ = rules.lint(rules.render({**self.BASE, "enabled": False}))
        assert "DISABLED" in rules.explain(rule)


class TestRenderedMeaningMatchesTheFile:
    """The card shows the compiled meaning; the file is what runs. Anything
    that renders but parses back as something DIFFERENT is the worst bug
    available here — and the party writing these values is the model, which is
    the party rules exist to bind."""

    BASE = {
        "name": "r", "description": "d", "when_subject": "always",
        "answer_from": "read_url",
    }

    @pytest.mark.parametrize("hostile", [
        "x\nenabled:\n false",          # lands the rule DISABLED
        "x\nexpires:\n 2020-01-01",     # lands it already expired
        "x\nthen:\n  never_use: [x]",   # smuggles an obligation
    ])
    def test_a_value_cannot_smuggle_a_second_key(self, hostile):
        rule, errors = rules.lint(rules.render({**self.BASE, "description": hostile}))
        assert not errors and rule is not None
        assert rule.description == hostile
        assert rule.status == "", "a description turned the rule off"
        assert rule.expires is None, "a description expired the rule"
        assert {o["verb"] for o in rule.obligations} == {rules.VERB_ANSWER_FROM}

    @pytest.mark.parametrize("value", [
        "on", "off", "yes", "no", "~", "null", "0x1A", "0755", "12:34", "1_000",
        "true", "material: use it", "#hash", "- dash", "a  b", "*star", "&amp;amp",
    ])
    def test_yaml_resolutions_survive_verbatim(self, value):
        """No denylist of first characters enumerates YAML 1.1's resolver. The
        renderer round-trips through the parser instead."""
        rule, errors = rules.lint(rules.render({**self.BASE, "description": value}))
        assert not errors and rule is not None
        assert rule.description == value

    def test_surrounding_whitespace_is_normalised_deliberately(self):
        """The one value that does NOT survive verbatim, and on purpose: a
        description is a sentence, and leading space in one is a typo."""
        rule, _ = rules.lint(rules.render({**self.BASE, "description": "  spaced  "}))
        assert rule is not None and rule.description == "spaced"

    def test_a_value_containing_a_frontmatter_marker_keeps_every_key_below_it(self):
        """The diff the owner approves visibly contained `never_use:` — and a
        naive split made everything below the marker PROSE, so the compiled
        rule had no prohibition. Quoting cannot fix it (`"a --- b"` still
        contains the marker), and "empty --- say so" is ordinary writing."""
        rule, errors = rules.lint(rules.render({
            "name": "r", "when_subject": "prompt",
            "when_has": "material", "answer_from": "material",
            "description": "the source came back empty --- say so",
            "never_use": ["web_search"],
        }))
        assert not errors and rule is not None
        assert {o["verb"] for o in rule.obligations} == {
            rules.VERB_ANSWER_FROM, rules.VERB_NEVER_USE,
        }, "an obligation below a --- was read as prose"
        assert rule.description == "the source came back empty --- say so"

    def test_a_frontmatter_marker_in_a_description_is_just_text(self):
        """It used to fail with "a rule needs a `when:` block" — naming
        something the author had in fact supplied, so a retry writes the same
        rule again."""
        rule, errors = rules.lint(rules.render({**self.BASE, "description": "a --- b"}))
        assert not errors and rule is not None and rule.description == "a --- b"

    @pytest.mark.parametrize("key", rules.LIFECYCLE_FIELDS)
    def test_a_lifecycle_key_is_never_silently_absent_from_the_card(self, key):
        """A rule that binds NOTHING must not read as one that works. Both keys
        make a rule inert, and the card has to say so — this is parametrised
        over the constant so a lifecycle key added later inherits the check
        instead of being the fourth instance of this bug."""
        value = {"enabled": False, "expires": "2020-01-01"}[key]
        rule, errors = rules.lint(rules.render({**self.BASE, key: value}))
        assert not errors and rule is not None
        text = rules.explain(rule)
        assert "DISABLED" in text or "EXPIRED" in text, text

    def test_a_regex_with_backslashes_is_not_re_escaped(self):
        rule, errors = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "prompt",
            "when_matches": r"\bhttps?://\S+", "answer_from": "read_url",
        }))
        assert not errors and rule is not None
        assert rule.pattern is not None
        assert rule.pattern.pattern == r"\bhttps?://\S+"

    def test_two_trigger_forms_at_once_are_refused_not_dropped(self):
        """Dropping one silently writes a rule with a trigger nobody chose."""
        with pytest.raises(rules.LintError) as exc:
            rules.render({
                "name": "r", "description": "d", "when_subject": "prompt",
                "when_has": "link", "when_matches": "x", "answer_from": "read_url",
            })
        assert "alternatives" in str(exc.value)


class TestShowAndCredit:
    """The two general forms that replaced a fixed list of named checks. Both
    name a TOOL, so a rule can require something about what aish DID without
    anyone coining a new check name in code first."""

    PATH = "/media/a1b2c3.png"
    VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    TOOLS = {"show_image", "show_video", "read_url"}

    def _call(self, tool, result, args=None):
        return {"tool": tool, "args": args or {}, "status": "ok", "decision": "",
                "result": result}

    def _shown(self):
        return self._call(
            "show_image", f"Image ready. Include this EXACTLY:\n\n![ubud]({self.PATH})"
        )

    def _played(self):
        return self._call("show_video", f"Video ready:\n\n[Watch]({self.VIDEO})")

    def _fail(self, then, answer, calls=()):
        rule, errors = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "always", **then,
        }), capabilities=self.TOOLS)
        assert not errors, errors
        binding = rules.bind(rule, {"on": "always"}, "b1", self.TOOLS)
        return rules.verify([binding], rules.TurnEvidence(answer=answer, calls=calls))

    def test_the_tools_own_output_in_the_answer_satisfies_it(self):
        assert not self._fail(
            {"answer_must_include": "picture"},
            f"Here it is ![ubud]({self.PATH})", (self._shown(),),
        )

    def test_a_picture_fetched_and_then_dropped_does_not(self):
        """The failure the join exists for: the tool ran, the owner saw
        nothing."""
        assert self._fail(
            {"answer_must_include": "picture"},
            "Ubud is greener than the coast.", (self._shown(),),
        )

    def test_a_different_path_does_not_pass_for_it(self):
        """An EQUALITY, not a guess about shape — the exact string the tool
        handed over has to be there."""
        assert self._fail(
            {"answer_must_include": "picture"},
            "Here it is ![x](/media/something-else.png)", (self._shown(),),
        )

    def test_never_calling_the_tool_does_not_satisfy_show(self):
        assert self._fail({"answer_must_include": "picture"}, "no picture", ())

    def test_a_refused_call_cannot_satisfy_it(self):
        refused = {**self._shown(), "decision": "denied", "status": "failed"}
        assert self._fail(
            {"answer_must_include": "picture"},
            f"Here it is ![ubud]({self.PATH})", (refused,),
        )

    def test_credit_passes_when_the_tool_never_ran(self):
        """The difference between the two words: CREDIT is conditional. If
        nothing was used there is nothing to credit."""
        assert not self._fail({"answer_must_include": "sources"}, "an answer", ())

    def test_credit_fails_when_what_ran_is_not_in_the_answer(self):
        call = self._call("read_url", "page text", {"url": "https://shop.example/x"})
        assert self._fail({"answer_must_include": "sources"}, "about 40 EUR", (call,))

    def test_credit_passes_when_the_page_is_linked(self):
        call = self._call("read_url", "page text", {"url": "https://shop.example/x"})
        assert not self._fail(
            {"answer_must_include": "sources"},
            "about 40 EUR — see https://shop.example/x", (call,),
        )

    def test_the_ask_tells_the_model_how_to_produce_it(self):
        """The rule file never names a tool. The ASK has to — the model is the
        one who has to act."""
        call = self._call("read_url", "t", {"url": "https://shop.example/x"})
        [failure] = self._fail({"answer_must_include": "sources"}, "40 EUR", (call,))
        assert "Link the pages you read" in failure.ask
        assert failure.evidence["expected"]["sources"] == ["https://shop.example/x"]

    def test_either_tool_satisfies_a_choice(self):
        then = {"answer_must_include": {"any_of": ["picture", "video"]}}
        assert not self._fail(then, f"see [Watch]({self.VIDEO})", (self._played(),))
        assert not self._fail(then, f"see ![x]({self.PATH})", (self._shown(),))

    def test_neither_tool_fails_the_choice(self):
        assert self._fail(
            {"answer_must_include": {"any_of": ["picture", "video"]}},
            "Ubud is greener than the coast.", (),
        )

    def test_the_ask_names_both_ways_out(self):
        [failure] = self._fail(
            {"answer_must_include": {"any_of": ["picture", "video"]}}, "text", ()
        )
        assert "show_image" in failure.ask and "show_video" in failure.ask

    def test_a_bare_list_is_refused_rather_than_read_as_a_choice(self):
        """`never_use: [a, b]` already means NONE OF THESE. Two lists in one
        file meaning opposite things is the trap `any_of` exists to avoid."""
        with pytest.raises(rules.LintError) as exc:
            rules.render({
                "name": "r", "description": "d", "when_subject": "always",
                "answer_must_include": ["picture", "video"],
            })
        assert "any_of" in str(exc.value)

    def test_a_choice_of_one_is_refused(self):
        rule, errors = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "always",
            "answer_must_include": {"any_of": ["picture"]},
        }), capabilities=self.TOOLS)
        assert rule is None and "at least two" in errors[0]

    def test_the_tool_a_kind_needs_is_checked_even_though_it_is_unnamed(self):
        """A kind is made real by a tool. If that tool is gone the rule can
        never be satisfied — caught in the tool's name, though the rule file
        never mentions it."""
        rule, errors = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "always",
            "answer_must_include": "picture",
        }), capabilities={"read_url"})
        assert rule is None and "show_image" in errors[0]

    def test_a_video_the_tool_validated_is_credited(self):
        assert not self._fail(
            {"answer_must_include": "video"}, f"see {self.VIDEO}", (self._played(),)
        )


class TestRetireWithoutCompiling:
    """The loud broken-rule warning is exactly when an owner reaches for
    retire. If retiring required a valid rule, the only unstoppable rules
    would be the broken ones."""

    def test_a_rule_that_does_not_compile_can_still_be_retired(self):
        broken = "---\nname: x\ndescription: d\nwhen: always\nthen: {}\n---\n\nbody\n"
        disabled = rules.disable_text(broken)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.md"
            path.write_text(disabled, encoding="utf-8")
            rule = rules.load_rules([Path(tmp)])[0]
        assert rule.status == "disabled"
        assert rule.error, "the rule is still broken — retiring does not repair it"

    def test_retiring_twice_does_not_stack_the_key(self):
        text = "---\nname: x\ndescription: d\nenabled: true\nwhen: always\n---\n"
        once = rules.disable_text(text)
        assert once.count("enabled:") == 1
        assert rules.disable_text(once).count("enabled:") == 1

    def test_only_a_top_level_enabled_key_is_replaced(self):
        """An unanchored text edit on a structured file deletes whatever
        happens to share a prefix at any depth."""
        text = ("---\nname: x\ndescription: d\nwhen:\n  prompt:\n"
                "    enabled: keep-me\n---\n")
        assert "enabled: keep-me" in rules.disable_text(text)

    def test_the_body_survives_retiring(self):
        text = "---\nname: x\ndescription: d\nwhen: always\n---\n\nWhy it exists.\n"
        assert "Why it exists." in rules.disable_text(text)


class TestMeaningTrigger:
    """The trigger that needs MEANING. "When I ask to be SHOWN something" is a
    fact about the request and it is gone by the time there is an answer to
    check — the one case the answer-side reframe cannot reach. Anchored on the
    owner's own examples, so a miss is fixed by adding one, not by tuning a
    number he can never see."""

    ANCHORS = ["show me the difference between X and Y", "pokaż mi jak to wygląda"]

    def _rule(self, **over):
        rule, errors = rules.lint(rules.render({
            "name": "show-me", "description": "Show me a picture.",
            "when_subject": "prompt", "when_like": self.ANCHORS,
            "answer_must_include": "picture", **over,
        }), capabilities={"show_image"})
        assert not errors, errors
        return rule

    def _scores(self, mapping):
        return lambda task, sentences: {s: mapping.get(s, 0.0) for s in sentences}

    def test_a_close_message_binds(self):
        rule = self._rule()
        verdict, evidence = rules.evaluate(
            rule, rules.TurnContext(task="show me difference between Ubud and the beach"),
            meaning=self._scores({self.ANCHORS[0]: 0.81}),
        )
        assert verdict == rules.VERDICT_BIND
        assert evidence["closest"] == self.ANCHORS[0]
        assert evidence["floor"] == rules.MEANING_FLOOR

    def test_an_unrelated_message_abstains(self):
        verdict, evidence = rules.evaluate(
            self._rule(), rules.TurnContext(task="what time is my flight"),
            meaning=self._scores({a: 0.2 for a in self.ANCHORS}),
        )
        assert verdict == rules.VERDICT_ABSTAIN
        # Both distributions, not only the hits: you cannot tell a floor sits
        # below a corpus's noise from the matches alone.
        assert len(evidence["sims"]) == len(self.ANCHORS)

    def test_the_evidence_carries_the_floor_that_was_in_force(self):
        """A threshold change later must not silently rewrite what past turns
        meant (contract §4)."""
        _v, evidence = rules.evaluate(
            self._rule(), rules.TurnContext(task="x"), meaning=self._scores({})
        )
        assert evidence["floor"] == rules.MEANING_FLOOR

    def test_no_embedding_model_is_a_third_answer_not_a_quiet_no(self):
        """"The model was down" and "the rule did not apply" are different
        facts. A rule whose evaluation silently degrades to abstaining looks
        exactly like one that is working."""
        verdict, evidence = rules.evaluate(
            self._rule(), rules.TurnContext(task="show me"), meaning=None
        )
        assert verdict == rules.VERDICT_UNEVALUABLE
        assert "embedding" in evidence["why"]

    def test_an_embedding_failure_is_also_unevaluable(self):
        verdict, _e = rules.evaluate(
            self._rule(), rules.TurnContext(task="show me"),
            meaning=lambda task, sentences: None,
        )
        assert verdict == rules.VERDICT_UNEVALUABLE

    def test_the_tier_is_derived_from_the_trigger_not_declared(self):
        """No policy language asks an author to annotate evaluation strategy,
        and "tier: 1" means nothing to the owner."""
        assert self._rule().tier == 1
        structural, _e = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "prompt",
            "when_has": "material", "answer_from": "material",
        }))
        assert structural.tier == 0

    def test_the_examples_are_on_the_card_in_the_owners_own_words(self):
        text = rules.explain(self._rule())
        for anchor in self.ANCHORS:
            assert anchor in text
        assert "0.6" not in text, "the owner was shown a threshold"

    def test_one_message_shape_per_rule(self):
        with pytest.raises(rules.LintError) as exc:
            rules.render({
                "name": "r", "description": "d", "when_subject": "prompt",
                "when_like": ["a"], "when_has": "link", "answer_from": "read_url",
            })
        assert "alternatives" in str(exc.value)

    def test_too_many_examples_is_refused(self):
        rule, errors = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "prompt",
            "when_like": [f"example {i}" for i in range(rules.MEANING_MAX_ANCHORS + 1)],
            "answer_from": "read_url",
        }))
        assert rule is None and "at most" in errors[0]


class TestKeywordListsAreRefused:
    """The exact shape a compiler produces when asked for a MEANING with only
    literal matching available — and extending the list is exactly how it tries
    to fix it. Reported from a real session: three attempts, three longer word
    lists, all wrong."""

    def _lint(self, pattern):
        return rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "prompt",
            "when_matches": pattern, "answer_from": "read_url",
        }), capabilities={"read_url"})

    @pytest.mark.parametrize("pattern", [
        "(?i)(show|display|view|picture)",
        "(?i)(pokaż|show me|zobacz)",
        "show|display|picture|photo",
    ])
    def test_a_word_list_is_refused_and_told_what_to_use(self, pattern):
        rule, errors = self._lint(pattern)
        assert rule is None
        assert "like" in errors[0]
        assert "Docker image" in errors[0], "the refusal must show WHY, not just say no"

    @pytest.mark.parametrize("pattern", [
        r"youtube\.com|youtu\.be",     # a domain list is a literal string
        r"^\s*https?://\S+\s*$",        # structure, not vocabulary
        "^show me",                     # anchored: a real string match
    ])
    def test_a_literal_pattern_is_still_allowed(self, pattern):
        rule, errors = self._lint(pattern)
        assert rule is not None, errors


class TestAnswerBeforeActing:
    """"Answer me before running anything." Declared inexpressible and it was
    not: pure ordering over the turn's own record, needing no understanding of
    whether a question was asked."""

    def _rule(self):
        rule, errors = rules.lint(rules.render({
            "name": "answer-first", "description": "Answer me before running anything.",
            "when_subject": "always", "must_first": "answer",
        }), capabilities={"read_url"})
        assert not errors, errors
        return rule

    def test_it_compiles_without_a_tool_of_that_name(self):
        """`answer` is HIS word, not a tool — the lint must not go looking for
        one."""
        assert self._rule() is not None

    def test_it_is_decided_at_the_gate_not_at_turn_end(self):
        """An ordering that has already gone wrong cannot be repaired by
        asking, so it must not hold the answer back either."""
        rule = self._rule()
        binding = rules.bind(rule, {"on": "always"}, "b1", {"read_url"})
        assert rules.wants_text_first([binding]) == [binding]
        assert rules.has_verify([binding]) is False
        assert "Enforced before a call runs" in rules.explain(rule)

    def test_a_rule_about_a_real_tool_is_untouched(self):
        rule, errors = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "always",
            "must_first": "read_url",
        }), capabilities={"read_url"})
        assert not errors
        binding = rules.bind(rule, {"on": "always"}, "b1", {"read_url"})
        assert rules.wants_text_first([binding]) == []
        assert rules.has_verify([binding]) is True


class TestAnswerSubject:
    """`when: answer:` — a condition on the DELIVERABLE.

    The engine's central move is *check the answer, not the prompt*, and its
    worked example — "the answer quotes a price, so a read of the store's own
    domain must exist in this turn's trace" — could not be written: an
    obligation could be attached to the answer, but never conditioned on it.
    """

    PRICE = r"[0-9][0-9., ]*\s?(zł|PLN|EUR|USD)"

    def _price_rule(self, **over):
        fields = {
            "name": "live-price",
            "description": "If you quote a price, you must have read the store page.",
            "when_subject": "answer", "when_matches": self.PRICE,
            "must_first": "read_url",
        }
        rule, errors = rules.lint(rules.render({**fields, **over}),
                                  capabilities={"read_url", "web_search"})
        assert not errors, errors
        return rule

    def _verify(self, rule, answer, calls=(), meaning=None):
        binding = rules.bind(rule, {"on": "answer"}, "b1", {"read_url"})
        evidence = rules.TurnEvidence(answer=answer, calls=tuple(calls),
                                      meaning=meaning)
        return binding, rules.verify([binding], evidence)

    def test_the_flagship_example_now_compiles(self):
        assert self._price_rule().trigger == rules.TRIGGER_ANSWER_SHAPE

    def test_it_arms_at_seed_because_there_is_no_answer_yet(self):
        """Same shape as `action:`: binding means "it is watching", never "it
        applied". Evaluated against a turn with no answer in it at all."""
        verdict, evidence = rules.evaluate(
            self._price_rule(), rules.TurnContext(task="what does a Switch cost")
        )
        assert verdict == rules.VERDICT_BIND
        assert evidence["on"] == "answer"

    def test_an_answer_with_no_price_asks_for_nothing(self):
        _b, failures = self._verify(self._price_rule(), "The capital is Paris.")
        assert failures == []

    def test_an_answer_quoting_a_price_with_no_fetch_fails(self):
        _b, [failure] = self._verify(self._price_rule(), "It is 249 PLN at the store.")
        assert failure.verb == "must_first"
        assert failure.askable is True

    def test_a_price_backed_by_a_real_read_passes(self):
        _b, failures = self._verify(
            self._price_rule(), "It is 249 PLN at the store.",
            calls=[{"tool": "read_url", "args": {"url": "https://shop.example/x"},
                    "status": "ok"}],
        )
        assert failures == []

    def test_a_refused_read_does_not_satisfy_it(self):
        """A call another gate stopped is not a call — and is never re-asked,
        or the harness argues with itself."""
        _b, [failure] = self._verify(
            self._price_rule(), "It is 249 PLN.",
            calls=[{"tool": "read_url", "args": {}, "decision": "denied"}],
        )
        assert failure.askable is False

    def test_the_ask_says_what_provoked_it(self):
        """R6: the model cannot see the condition, so a goad that does not name
        it is not instructive."""
        _b, [failure] = self._verify(self._price_rule(), "It is 249 PLN.")
        assert "Your answer" in failure.ask
        assert "read_url" in failure.ask
        assert failure.evidence["condition"]["matched"] is True

    def test_a_gate_verb_under_an_answer_condition_is_refused(self):
        """The condition is unknowable before a call runs, so the restriction
        would silently never fire — and one that never fires looks exactly like
        one that works."""
        rule, errors = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "answer",
            "when_matches": "x", "never_use": ["web_search"],
        }), capabilities={"web_search"})
        assert rule is None
        assert "cannot carry `never_use:`" in errors[0]

    def test_a_plain_phrase_is_refused_by_name(self):
        with pytest.raises(rules.LintError) as exc:
            rules.render({
                "name": "r", "description": "d", "when_subject": "answer",
            })
        assert "judged question" in str(exc.value)

    def test_the_condition_can_be_scoped_to_the_ending(self):
        """"If the answer ends with a question, give me tap buttons" — the
        second rule that had nowhere to live."""
        rule, errors = rules.lint(rules.render({
            "name": "chips", "description": "Questions get tap buttons.",
            "when_subject": "answer", "when_matches": r"\?\s*$", "when_in": "ending",
            "answer_must_include": {"pattern": "aish-reply://"},
        }))
        assert not errors, errors
        assert rule.where == "ending"
        # A question in the MIDDLE is not the answer ending in one.
        _b, failures = self._verify(rule, "Is it raining?\n\nIt is not raining.")
        assert failures == []
        _b, [failure] = self._verify(rule, "It is raining.\n\nShall I book a taxi?")
        assert failure.verb == "answer_must_include"

    def test_an_examples_condition_is_scored_and_needs_no_threshold_authored(self):
        rule, errors = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "answer",
            "when_like": ["here is a recipe you can cook"],
            "answer_must_include": {"pattern": "ingredients"},
        }))
        assert not errors, errors
        assert rule.tier == 1
        _b, [failure] = self._verify(
            rule, "A lovely dish to make tonight.",
            meaning=lambda text, anchors: {a: 0.9 for a in anchors},
        )
        assert failure.verb == "answer_must_include"

    def test_with_no_scorer_a_meaning_condition_does_not_fire(self):
        """Opposite direction to the trigger side, deliberately: this condition
        only ever ADDS obligations, so failing to evaluate it can never lift
        one."""
        rule, _e = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "answer",
            "when_like": ["here is a recipe"],
            "answer_must_include": {"pattern": "ingredients"},
        }))
        _b, failures = self._verify(rule, "A lovely dish.")
        assert failures == []

    def test_the_binding_records_why_it_stayed_quiet(self):
        """Armed-and-silent and fired-and-passed are different facts, and only
        the log can tell them apart afterwards."""
        binding, failures = self._verify(self._price_rule(), "No prices here.")
        assert failures == []
        assert binding.answer_condition["matched"] is False
        assert binding.answer_condition["on"] == "answer"

    def test_it_holds_the_answer_back_from_the_stream(self):
        rule = self._price_rule()
        binding = rules.bind(rule, {"on": "answer"}, "b1", {"read_url"})
        assert rules.has_verify([binding]) is True

    def test_an_edit_round_trips_the_condition(self, tmp_path):
        path = tmp_path / "live-price.md"
        path.write_text(rules.render({
            "name": "live-price", "description": "d", "when_subject": "answer",
            "when_matches": self.PRICE, "when_in": "ending", "must_first": "read_url",
        }), encoding="utf-8")
        fields = rules.author_fields(path)
        assert fields["when_subject"] == "answer"
        assert fields["when_matches"] == self.PRICE
        assert fields["when_in"] == "ending"
        again, errors = rules.lint(rules.render(fields), capabilities={"read_url"})
        assert not errors, errors
        assert again.where == "ending"


class TestAskMeFirst:
    """The HOLD verb — R7's other half.

    Route and prohibit are things the model can comply with by choosing
    differently. "Check with me before you file that" is not addressed to the
    model at all, so refusing it would be the harness arguing with someone who
    cannot answer the question."""

    def _rule(self, **over):
        fields = {
            "name": "confirm-issues",
            "description": "Check with me before filing an issue.",
            "when_subject": "action", "when_action": {"tool": "gh_issue_create"},
            "ask_me_first": True,
        }
        rule, errors = rules.lint(rules.render({**fields, **over}),
                                  capabilities={"gh_issue_create", "web_search"})
        return rule, errors

    def _bound(self):
        rule, errors = self._rule()
        assert not errors, errors
        return rules.bind(rule, {"on": "action"}, "b1", {"gh_issue_create"})

    def test_it_compiles(self):
        rule, errors = self._rule()
        assert not errors, errors
        assert rule.obligations[0]["verb"] == "ask_me_first"

    def test_it_holds_the_matching_call(self):
        binding = self._bound()
        [verdict] = rules.gate([binding], "gh_issue_create", {"title": "x"})
        assert verdict.verdict == "hold"
        assert "the owner decides this one" in verdict.message

    def test_it_leaves_every_other_call_alone(self):
        binding = self._bound()
        assert rules.gate([binding], "web_search", {})[0].verdict == "allowed"

    def test_it_does_NOT_refuse_first(self):
        """The bounded refuse-first discipline is for obligations the model can
        satisfy by choosing differently. This one it cannot."""
        binding = self._bound()
        for _ in range(rules.RULE_MAX_REFUSALS + 2):
            assert rules.gate([binding], "gh_issue_create", {})[0].verdict == "hold"
        assert binding.rounds == 0

    def test_it_reaches_dispatch_rather_than_the_parallel_path(self):
        """A held call that never reaches `_dispatch` is not held at all — the
        read-only fan-out bypasses it, and holding an auto-approved read is
        exactly what someone writes this verb for."""
        binding = self._bound()
        assert rules.affects([binding], "gh_issue_create") is True

    def test_it_needs_an_action_subject(self):
        """Without one there is nothing to hold: attached to a prompt condition
        it would mean every call for the whole turn."""
        _rule, errors = self._rule(when_subject="always", when_action=None)
        assert errors and "needs `when: action:`" in errors[0]

    def test_the_seed_tells_the_model_to_expect_it(self):
        assert "The USER decides this one" in rules.seed_text([self._bound()])

    def test_the_card_states_the_promise_it_makes(self):
        rule, _e = self._rule()
        card = rules.explain(rule)
        assert "Put to you before it runs, every time" in card
        assert "refused" not in card

    def test_it_grants_nothing(self):
        """R1: a rule restricts. This verb only ever ADDS a card — it can never
        make a call that would have been approved into one that is not asked."""
        binding = self._bound()
        verdicts = rules.gate([binding], "gh_issue_create", {})
        assert all(v.verdict != "allowed" for v in verdicts)


class TestResultSubject:
    """`when: result:` — the condition #190 was founded on.

    "If the transcript comes back empty, say so — do not go and get a news
    article instead" is a fact about a TOOL RESULT. Retrieval keys on the
    user's text, so no memory could ever be delivered on the turn it mattered:
    a bare URL has no lexical or semantic surface to match. This is the trigger
    kind that was structurally undeliverable by the knowledge layer.
    """

    def _rule(self, **over):
        fields = {
            "name": "transcript-failure",
            "description": "If the transcript comes back empty, tell me — don't substitute.",
            "when_subject": "result",
            "when_result_of": "youtube_analyze", "when_result_was": "empty",
            "never_use": ["web_search"],
        }
        return rules.lint(
            rules.render({**fields, **over}),
            capabilities={"youtube_analyze", "web_search"},
        )

    def _bound(self, **over):
        rule, errors = self._rule(**over)
        assert not errors, errors
        return rules.bind(rule, {"on": "result", "of": rule.result_of,
                                 "was": rule.result_was}, "b1", {"web_search"})

    def test_the_founding_memory_now_compiles(self):
        rule, errors = self._rule()
        assert not errors, errors
        assert rule.trigger == rules.TRIGGER_RESULT_STATE

    def test_it_arms_at_seed_so_the_model_is_told_before_it_happens(self):
        """The whole difference from the memory that failed. The condition is in
        context BEFORE the tool runs, rather than arriving as a refusal for a
        rule the model was never shown."""
        binding = self._bound()
        text = rules.seed_text([binding])
        assert "CONDITIONAL" in text
        assert "youtube_analyze" in text
        assert "web_search" in text

    def test_it_restricts_nothing_while_armed(self):
        """Refusing a web search BEFORE the transcript failed is a different and
        much worse rule than the one he wrote."""
        binding = self._bound()
        assert binding.active is False
        [verdict] = rules.gate([binding], "web_search", {"query": "x"})
        assert verdict.verdict == "allowed"

    def test_a_healthy_result_does_not_fire_it(self):
        binding = self._bound()
        binding.note_tool_result("youtube_analyze", "ok")
        assert binding.active is False
        assert rules.gate([binding], "web_search", {})[0].verdict == "allowed"

    def test_an_empty_result_fires_it_and_the_substitution_is_refused(self):
        binding = self._bound()
        binding.note_tool_result("youtube_analyze", "incomplete")
        assert binding.active is True
        [verdict] = rules.gate([binding], "web_search", {"query": "x"})
        assert verdict.verdict == "refused"
        assert "web_search" in verdict.message

    def test_another_tool_failing_does_not_fire_it(self):
        binding = self._bound()
        binding.note_tool_result("read_url", "incomplete")
        assert binding.active is False

    def test_error_and_empty_are_different_states(self):
        binding = self._bound(when_result_was="error")
        binding.note_tool_result("youtube_analyze", "incomplete")
        assert binding.active is False
        binding.note_tool_result("youtube_analyze", "failed")
        assert binding.active is True

    def test_a_later_success_does_not_un_fire_it(self):
        """Latched on purpose. A retry that works does not undo an answer built
        partly on a source that failed — and nothing would say so."""
        binding = self._bound()
        binding.note_tool_result("youtube_analyze", "incomplete")
        binding.note_tool_result("youtube_analyze", "ok")
        assert binding.active is True

    def test_a_typo_in_the_watched_tool_is_refused(self):
        """The least visible typo in the vocabulary: the rule looks armed all
        turn and simply never fires."""
        _rule, errors = self._rule(when_result_of="youtube_analize")
        assert errors and "waits on a tool that does not exist" in errors[0]

    def test_an_unknown_state_is_refused_with_the_options(self):
        _rule, errors = self._rule(when_result_was="truncated")
        assert errors and "empty" in errors[0] and "error" in errors[0]

    def test_verify_is_silent_while_it_is_armed(self):
        binding = self._bound(never_use="", must_first="youtube_analyze")
        assert rules.verify([binding], rules.TurnEvidence(answer="anything")) == []

    def test_the_card_says_when_in_english(self):
        rule, _e = self._rule()
        assert "Once youtube_analyze has come back" in rules.explain(rule)

    def test_it_round_trips_through_an_edit(self, tmp_path):
        path = tmp_path / "r.md"
        rule, _e = self._rule()
        path.write_text(rules.render({
            "name": "transcript-failure", "description": "d",
            "when_subject": "result", "when_result_of": "youtube_analyze",
            "when_result_was": "empty", "never_use": ["web_search"],
        }), encoding="utf-8")
        fields = rules.author_fields(path)
        assert fields["when_result_of"] == "youtube_analyze"
        assert fields["when_result_was"] == "empty"


class TestASkillIsACapability:
    """"For accommodation use trippy" names ONE capability to the owner. Which
    side of aish's internal tool/skill fence it lives on is not his concern —
    the decision was recorded and never reached the code, so the design's own
    worked example for `must_first` failed the lint as a missing tool."""

    def _rule(self, **over):
        fields = {
            "name": "accommodation-via-trippy",
            "description": "Accommodation searches go through trippy.",
            "when_subject": "prompt",
            "when_like": ["find me a villa in Uluwatu", "szukam noclegu w Warszawie"],
            "must_first": "trippy_search",
            "never_use": ["web_search"],
        }
        return rules.lint(
            rules.render({**fields, **over}),
            capabilities={"web_search", "read_url", "trippy_search"},
            skill_names={"trippy_search"},
        )

    def test_a_rule_may_require_a_skill(self):
        rule, errors = self._rule()
        assert not errors, errors
        assert rule is not None

    def test_an_unknown_name_is_still_refused(self):
        _rule, errors = self._rule(must_first="tripy_serch")
        assert errors and "does not exist" in errors[0]

    def test_reading_the_skill_satisfies_it(self):
        """The other half. Without it the lint would accept a rule naming a
        skill that Verify could never see satisfied — a rule that asks forever,
        which is worse than one that refuses to compile."""
        rule, _e = self._rule()
        binding = rules.bind(rule, {"on": "task"}, "b1", {"trippy_search"})
        evidence = rules.TurnEvidence(
            answer="Two villas, both available.",
            calls=({"tool": "read_skill", "args": {"name": "trippy_search"},
                    "status": "ok"},),
        )
        assert rules.verify([binding], evidence) == []

    def test_reading_a_DIFFERENT_skill_does_not(self):
        rule, _e = self._rule()
        binding = rules.bind(rule, {"on": "task"}, "b1", {"trippy_search"})
        evidence = rules.TurnEvidence(
            answer="Two villas.",
            calls=({"tool": "read_skill", "args": {"name": "gh_issue"},
                    "status": "ok"},),
        )
        assert len(rules.verify([binding], evidence)) == 1

    def test_a_refused_skill_read_is_not_a_read(self):
        rule, _e = self._rule()
        binding = rules.bind(rule, {"on": "task"}, "b1", {"trippy_search"})
        evidence = rules.TurnEvidence(
            answer="Two villas.",
            calls=({"tool": "read_skill", "args": {"name": "trippy_search"},
                    "decision": "denied"},),
        )
        [failure] = rules.verify([binding], evidence)
        assert failure.askable is False

    def test_a_skill_cannot_be_routed_to(self):
        """`answer_from` means "the deliverable comes from HERE and everything
        else is refused". A skill is guidance; it produces no deliverable, so
        routing to one prohibits every tool in favour of something that can
        never satisfy the route."""
        _rule, errors = self._rule(must_first="", answer_from="trippy_search")
        assert errors and "names a skill" in errors[0]
        assert "must_first" in errors[0]


class TestSecretsInCommands:
    """"Never put a secret inline in a command." A join against his own
    keychain — a pattern would need the secret written into the rule file,
    which is the very thing the rule stops."""

    ACTION = {"tool": "run_command", "command_has": "a_secret"}

    def _rule(self):
        rule, errors = rules.lint(rules.render({
            "name": "no-inline-secrets", "description": "No secrets in commands.",
            "when_subject": "action", "when_action": self.ACTION,
            "never_use": ["run_command"],
        }), capabilities={"run_command"})
        assert not errors, errors
        return rule

    def test_it_fires_only_when_a_stored_secret_is_in_the_command(self):
        found = lambda cmd: "hunter2hunter2" in cmd  # noqa: E731
        assert rules.action_matches(
            self.ACTION, "run_command", {"command": "curl -H 'k: hunter2hunter2'"},
            secrets_in=found,
        )
        assert not rules.action_matches(
            self.ACTION, "run_command", {"command": "ls -la"}, secrets_in=found
        )

    def test_no_keychain_means_no_match_rather_than_a_crash(self):
        assert not rules.action_matches(
            self.ACTION, "run_command", {"command": "anything"}, secrets_in=None
        )

    def test_the_card_says_it_in_english(self):
        text = rules.explain(self._rule())
        assert "one of your stored secrets" in text
        assert "command_has" not in text

    def test_a_made_up_value_is_refused_with_the_reason(self):
        rule, errors = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "action",
            "when_action": {"tool": "run_command", "command_has": "sk-[a-z]+"},
            "never_use": ["run_command"],
        }), capabilities={"run_command"})
        assert rule is None
        assert "would be the very thing it stops" in errors[0]


class TestMeaningOverTheAnswer:
    """Sycophancy, per turn rather than in an offline audit. His argument, and
    it holds: a model's reflex lands in its FIRST paragraph, so embedding one
    paragraph against a few anchors is milliseconds and local."""

    ANCHORS = ["You're absolutely right, I apologize for the confusion",
               "Great question! I'm so glad you asked"]

    def _fail(self, answer, scores):
        rule, errors = rules.lint(rules.render({
            "name": "no-flattery", "description": "No flattery up front.",
            "when_subject": "always",
            "answer_must_not_include": {"like": self.ANCHORS, "in": "opening"},
        }))
        assert not errors, errors
        binding = rules.bind(rule, {"on": "always"}, "b1", set())
        evidence = rules.TurnEvidence(
            answer=answer,
            meaning=lambda text, anchors: {a: scores.get(a, 0.0) for a in anchors},
        )
        return rules.verify([binding], evidence)

    def test_a_flattering_opening_is_caught(self):
        assert self._fail(
            "You are so right, sorry about that!\n\nThe answer is 42.",
            {self.ANCHORS[0]: 0.83},
        )

    def test_a_plain_opening_passes(self):
        assert not self._fail("The answer is 42.", {a: 0.1 for a in self.ANCHORS})

    def test_only_the_opening_is_looked_at(self):
        """The point of `in: opening` — the same words later in a long answer
        are usually quoting or explaining, not grovelling."""
        seen = []
        rule, _e = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "always",
            "answer_must_not_include": {"like": self.ANCHORS, "in": "opening"},
        }))
        binding = rules.bind(rule, {"on": "always"}, "b1", set())
        rules.verify([binding], rules.TurnEvidence(
            answer="First para.\n\nYou're absolutely right, I apologize.",
            meaning=lambda text, anchors: seen.append(text) or {a: 0.0 for a in anchors},
        ))
        assert seen == ["First para."], seen

    def test_no_scorer_abstains_rather_than_passing_quietly(self):
        rule, _e = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "always",
            "answer_must_not_include": {"like": self.ANCHORS, "in": "opening"},
        }))
        binding = rules.bind(rule, {"on": "always"}, "b1", set())
        assert rules.verify([binding], rules.TurnEvidence(answer="anything")) == []

    def test_the_evidence_records_both_distributions(self):
        [failure] = self._fail(
            "You are so right, sorry!", {self.ANCHORS[0]: 0.9, self.ANCHORS[1]: 0.2}
        )
        assert len(failure.evidence["sims"]) == 2
        assert failure.evidence["floor"] == rules.MEANING_FLOOR

    def test_a_position_that_does_not_exist_is_refused(self):
        with pytest.raises(rules.RuleError):
            rules._answer_check("answer_must_not_include",
                                {"like": ["x"], "in": "middle"})

    def test_slicing_is_by_paragraph_not_by_characters(self):
        answer = "One.\n\nTwo.\n\nThree."
        assert rules.slice_answer(answer, "opening") == "One."
        assert rules.slice_answer(answer, "ending") == "Three."
        assert rules.slice_answer(answer, "anywhere") == answer


class TestRetroMatch:
    """Replay a candidate over turns that already happened. A manufactured turn
    tests the harness; a real one tests the rule."""

    TURNS = [
        {"turn": "1", "prompt": "summarize https://example.com/post", "origin": "user"},
        {"turn": "2", "prompt": "what is the capital of France", "origin": "user"},
        {"turn": "3", "prompt": "read ~/notes/plan.md and tell me", "origin": "user"},
    ]

    def _rule(self, **over):
        fields = {"name": "r", "description": "d", "when_subject": "prompt",
                  "when_has": "material", "answer_from": "material", **over}
        rule, _ = rules.lint(rules.render(fields))
        return rule

    def test_it_reports_which_real_turns_would_have_bound(self):
        match = rules.retro_match(self._rule(), self.TURNS)
        assert match.checked == 3
        assert [t["turn"] for t in match.bound] == ["1", "3"]

    def test_a_narrower_trigger_binds_strictly_fewer_turns(self):
        """The behaviour diff the design asks for, in its simplest form: the
        same history, two rules, a comparable answer."""
        wide = rules.retro_match(self._rule(), self.TURNS)
        narrow = rules.retro_match(
            self._rule(when_has="link", answer_from="read_url"), self.TURNS
        )
        assert {t["turn"] for t in narrow.bound} < {t["turn"] for t in wide.bound}

    def test_an_action_rule_reports_that_it_cannot_be_replayed(self):
        """It arms every turn and fires per CALL, and calls are not replayed
        here. Counting its binds would read as "this fires constantly" for a
        rule that may never fire — on the one trigger kind where bound and
        fired are different things."""
        rule, errors = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "action",
            "when_action": {"tool": "remember"}, "never_use": ["remember"],
        }))
        assert not errors and rule is not None
        match = rules.retro_match(rule, self.TURNS)
        assert match.per_call is True
        assert match.checked == 0 and not match.bound

    def test_a_rule_binding_nothing_is_visible_as_such(self):
        """Not an error — it may be about the future. But the owner has to see
        it, because the other explanation is that the rule is wrong."""
        match = rules.retro_match(self._rule(when_has="attachment"), self.TURNS)
        assert match.checked == 3 and not match.bound

    def test_an_attachment_turn_is_replayed_as_carrying_material(self, tmp_path):
        """Attachments reach the agent as separate parameters and appear in no
        message text. Reading only `content` made the canonical shipped rule's
        card say "would never have fired" — understating on the most common
        trigger kind."""
        log = tmp_path / "session-20260101-000000-000000.jsonl"
        log.write_text(json.dumps({
            "kind": "message", "role": "user", "turn": "a",
            "content": "what does this say", "documents": ["/tmp/report.pdf"],
        }) + "\n", encoding="utf-8")
        turns = rules.past_turns(tmp_path)
        rule, _ = rules.lint(rules.render({
            "name": "r", "description": "d", "when_subject": "prompt",
            "when_has": "attachment", "answer_from": "material",
        }))
        assert [t["turn"] for t in rules.retro_match(rule, turns).bound] == ["a"]

    def test_past_turns_skips_text_the_owner_never_typed(self, tmp_path):
        """aish's own notes arrive in the user slot. Counting them would have a
        rule appear to bind on turns nobody took."""
        log = tmp_path / "session-20260101-000000-000000.jsonl"
        log.write_text("\n".join(json.dumps(r) for r in [
            {"kind": "message", "role": "user", "content": "real question", "turn": "a"},
            {"kind": "message", "role": "user", "content": "[aish: rework this]", "turn": "b"},
            {"kind": "message", "role": "assistant", "content": "an answer"},
        ]) + "\n", encoding="utf-8")
        turns = rules.past_turns(tmp_path)
        assert [t["prompt"] for t in turns] == ["real question"]


class TestSeedingCostsOnlyWhatItBuys:
    """An ordinary turn — nothing about prices, images or mail — seeded 9,562
    characters across 15 bound rules, because everything except a `prompt:`
    condition ARMS at seed. Conditional about enforcement, unconditional about
    announcement. That is how a rules engine turns into a fatter system prompt,
    which is the thing the owner said he did not want."""

    def _binding(self, **over):
        fields = {"name": "r", "description": "d", "when_subject": "always",
                  "prose": "A paragraph of explanation that costs real context."}
        fields.update(over)
        caps = {"web_search", "read_url", "write_file", "run_command"}
        rule, errors = rules.lint(rules.render(fields), capabilities=caps)
        assert not errors, errors
        return rules.bind(rule, {"on": "always"}, "b1", caps)

    def test_a_rule_checked_only_at_the_end_does_not_seed_its_prose(self):
        """There is nothing to warn about in advance: the check runs whatever
        the model was told, and the question asked on failure explains it at the
        moment it is relevant. R5 is about GATES — nothing here refuses."""
        binding = self._binding(answer_must_include={"pattern": "x"})
        assert rules.seeds_prose(binding) is False
        text = rules.seed_text([binding])
        assert "A paragraph of explanation" not in text
        assert "checked once your answer is written" in text

    def test_a_rule_that_can_REFUSE_still_explains_itself(self):
        binding = self._binding(never_use=["web_search"])
        assert rules.seeds_prose(binding) is True
        assert "A paragraph of explanation" in rules.seed_text([binding])

    def test_an_armed_action_rule_seeds_its_obligation_but_not_its_essay(self):
        """It is watching a call nobody has proposed. The obligation steers; the
        essay is written for the moment of refusal, and the refusal carries it
        in full and uncapped."""
        binding = self._binding(when_subject="action",
                                when_action={"command_starts_with": "pip"},
                                never_use=["run_command"])
        text = rules.seed_text([binding])
        assert "MUST NOT call run_command" in text
        assert "A paragraph of explanation" not in text

    def test_an_action_rule_states_its_CONDITION_or_it_is_a_lie(self):
        """The unconditional wording said "MUST NOT call write_file, edit_file,
        run_command for this turn" for a rule that only guards one directory.
        Believed, that disables editing any file anywhere: a narrow guard read
        as a blanket ban."""
        binding = self._binding(when_subject="action",
                                when_action={"path_under": "~/dev/aish"},
                                never_use=["write_file", "run_command"])
        text = rules.seed_text([binding])
        assert "WHEN it touches anything under ~/dev/aish" in text
        assert "run_command for this turn" not in text
        # Said ONCE in the header, not repeated per rule: identical for every
        # action rule, and six copies of it is the cost this pass removes.
        assert text.count("restricts only that case") == 1


class TestOneCommandHasManySpellings:
    """`pip`, `pip3`, `python -m pip` are one intent. Forcing a file per
    spelling makes the owner maintain the shape of a shell instead of stating a
    policy — and a rule covering two thirds of a thing is exactly the silent
    under-restriction the engine exists to make impossible."""

    def _rule(self, value):
        rule, errors = rules.lint(rules.render({
            "name": "uv-only", "description": "d", "when_subject": "action",
            "when_action": {"command_starts_with": value},
            "never_use": ["run_command"],
        }), capabilities={"run_command"})
        assert not errors, errors
        return rule

    def test_a_list_matches_any_of_them(self):
        rule = self._rule(["pip", "pip3", "python -m pip"])
        for command in ("pip install x", "pip3 install x", "python -m pip install x"):
            assert rules.action_matches(rule.action, "run_command",
                                        {"command": command}), command

    def test_what_it_does_not_name_stays_allowed(self):
        rule = self._rule(["pip", "python -m pip"])
        for command in ("uv run x.py", "uv add requests", "uvx ruff"):
            assert not rules.action_matches(rule.action, "run_command",
                                            {"command": command}), command

    def test_a_SCALAR_WITH_A_SPACE_is_one_prefix_and_is_never_split(self):
        """`gh issue` is one prefix containing a space. Splitting it on
        whitespace would silently widen the rule to every `gh` command there
        is — a restriction quietly becoming a much bigger one."""
        rule = self._rule("gh issue")
        assert rules.action_matches(rule.action, "run_command",
                                    {"command": "gh issue create -t x"})
        assert not rules.action_matches(rule.action, "run_command",
                                        {"command": "gh pr list"})

    def test_the_card_reads_every_spelling_aloud(self):
        card = rules.explain(self._rule(["pip", "python -m pip"]))
        assert "`pip` or `python -m pip`" in card


class TestLinksYouDidNotOpen:
    """The general form of a rule the owner kept writing per topic.

    His failure was never "visas": he asked about entry requirements, got
    government URLs, and they were wrong because the site had changed. Written
    as a topic rule it needs a list — travel, tax, health, legal — maintained
    forever and wrong at the edges by construction. Written as a fact about the
    ANSWER it needs no list at all."""

    def _binding(self):
        rule, errors = rules.lint(rules.render({
            "name": "opened-links-only", "description": "Only links you opened.",
            "when_subject": "always",
            "answer_must_not_include": "unverified_links",
        }), capabilities={"read_url"})
        assert not errors, errors
        return rules.bind(rule, {"on": "always"}, "b1", {"read_url"})

    def _check(self, answer, calls=()):
        return rules.verify([self._binding()],
                            rules.TurnEvidence(answer=answer, calls=tuple(calls)))

    def test_an_answer_with_no_links_is_fine(self):
        assert self._check("Mutexes are exclusive, semaphores count.") == []

    def test_a_link_that_was_opened_passes(self):
        assert self._check(
            "See [the guide](https://example.com/guide).",
            [{"tool": "read_url", "args": {"url": "https://example.com/guide"},
              "status": "ok"}],
        ) == []

    def test_a_link_that_was_NEVER_opened_fails(self):
        [failure] = self._check("See [the guide](https://example.com/guide).")
        assert "example.com/guide" in failure.ask
        assert failure.evidence["unverified"] == ["https://example.com/guide"]

    def test_a_link_whose_FETCH_FAILED_does_not_count_as_opened(self):
        """His addition, and it is the difference between "I tried" and "it
        works": a 404 is not a link you can hand someone."""
        [failure] = self._check(
            "See [the guide](https://example.com/gone).",
            [{"tool": "read_url", "args": {"url": "https://example.com/gone"},
              "status": "failed"}],
        )
        assert "example.com/gone" in failure.ask

    def test_seeing_a_url_in_a_search_RESULT_is_not_opening_it(self):
        """The distinction the whole check rests on. Quoting a URL out of a
        search snippet is precisely the move being stopped, and a snippet is
        tool OUTPUT, never a target the harness acted on."""
        [failure] = self._check(
            "See [the shop](https://shop.example/item).",
            [{"tool": "web_search", "args": {"query": "shop item"},
              "result": "Top hit: https://shop.example/item — great prices",
              "status": "ok"}],
        )
        assert failure.evidence["unverified"] == ["https://shop.example/item"]

    def test_a_video_validated_by_show_video_counts(self):
        """`show_video` takes the URL as an argument, so it was acted on. A
        check that flagged it would make the video rules unsatisfiable."""
        assert self._check(
            "Here it is: [watch](https://youtu.be/abc123)",
            [{"tool": "show_video", "args": {"url": "https://youtu.be/abc123"},
              "status": "ok"}],
        ) == []

    def test_a_trailing_slash_or_anchor_is_the_same_page(self):
        assert self._check(
            "See [it](https://example.com/guide/#setup).",
            [{"tool": "read_url", "args": {"url": "https://example.com/guide"},
              "status": "ok"}],
        ) == []

    def test_it_reports_every_bad_link_not_only_the_first(self):
        [failure] = self._check(
            "[a](https://a.example/x) and [b](https://b.example/y)")
        assert len(failure.evidence["unverified"]) == 2

    def test_it_can_only_be_written_as_a_PROHIBITION(self):
        """"Must include a link you never opened" is not a sentence anyone
        means."""
        with pytest.raises(rules.RuleError):
            rules._answer_check("answer_must_include", "unverified_links")
