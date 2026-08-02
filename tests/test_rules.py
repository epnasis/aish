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
name: bounded-material
description: Answer from the material I gave you.
when:
  prompt:
    has: source
then:
  answer_from: source
  never_use: [web_search]
  must_tell_me_when: the material could not be read
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
            {"verb": "answer_from", "to": "source", "of": "deliverable"},
            {"verb": "never_use", "what": ["web_search"]},
            {"verb": "must_tell_me_when", "state": "the material could not be read"},
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
            (CANONICAL.replace("  prompt:\n    has: source\n", "  vibes: yes\n"),
             "unknown `when:` subject"),
            (CANONICAL.replace("has: source", "has: vibes"), "unknown `has:` value"),
            (
                CANONICAL.replace("    has: source\n", "").replace(
                    "  answer_from: source\n", ""
                ),
                "needs `has:` or `matches:`",
            ),
            (
                CANONICAL.replace("has: source", "has: source\n    matches: ^x"),
                "OR `matches:`, not both",
            ),
            (CANONICAL.replace("has: source", "matches: ^x"), "needs `when: prompt: has:"),
            (
                CANONICAL.replace("  answer_from: source\n", "")
                .replace("  never_use: [web_search]\n", "")
                .replace("  must_tell_me_when: the material could not be read\n", ""),
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
        assert route["to"] == "source"
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
            ("disclose", "`then: must_tell_me_when:`"),
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
        rule = load_one(tmp_path, CANONICAL.replace("has: source", "has: link"))
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

    def test_the_designed_verbs_say_what_is_MISSING_not_just_that_it_is_wrong(self, tmp_path):
        """A refused rule is a legible gap — the owner can decide to rephrase
        toward what exists, or to extend the engine. "Not expressible" alone
        gives him neither choice."""
        rule = load_one(tmp_path, CANONICAL.replace(
            "  never_use: [web_search]", "  ask_me_first: true"
        ))
        assert "designed but not built yet" in rule.error
        assert "hold for the owner" in rule.error
        assert ", ".join(sorted(rules.VERBS)) in rule.error

    def test_a_phrase_only_a_judge_could_check_is_refused_by_name(self, tmp_path):
        """The admission line, at the one place it is tempting to bend: a verb
        ships only if it compiles to a declared check. "be less annoying" is a
        judged question, and the judged tier is not built — so it is refused
        with the two structural forms that ARE available, rather than shipping
        as a promise nothing keeps."""
        rule = load_one(tmp_path, CANONICAL.replace(
            "  never_use: [web_search]", "  answer_must_not: be less annoying about it"
        ))
        assert "only a judge can check" in rule.error
        assert "raw_image_links" in rule.error and "pattern" in rule.error


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
