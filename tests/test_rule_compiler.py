"""The prose→rule compiler (#205 §4).

The compiler is scripted exactly as the acting model is elsewhere in this
suite: a list of canned replies, no model, no network. What is being tested is
never "did a model get it right" — it is what the CODE does with each shape of
reply, which is the half that has to hold whatever model is behind it.
"""

from __future__ import annotations

import json

from aish import rule_compiler, rules

TOOLS = {"read_url", "web_search", "show_image", "run_command", "remember"}


def scripted(*replies):
    """A compiler that returns canned replies in order, recording its prompts."""
    seen: list[str] = []
    queue = list(replies)

    def ask(prompt: str) -> str:
        seen.append(prompt)
        return queue.pop(0) if queue else "{}"

    ask.prompts = seen  # type: ignore[attr-defined]
    return ask


GOOD = json.dumps({
    "name": "bounded-material",
    "description": "Answer from the material I gave you.",
    "when_subject": "prompt",
    "when_has": "source",
    "answer_from": "source",
    "never_use": ["web_search"],
    "prose": "Widening quietly costs the ability to trust any answer.",
})


class TestCompiling:
    def test_a_good_reply_compiles_in_one_round(self):
        result = rule_compiler.compile_request("answer from what I give you",
                                               scripted(GOOD), TOOLS)
        assert result and result.rounds == 1
        assert result.fields["when_has"] == "source"
        rule, errors = rules.lint(rules.render(result.fields), known_tools=TOOLS)
        assert not errors and rule is not None

    def test_the_owners_words_reach_the_compiler_verbatim(self):
        """It translates one instruction. Summarising it on the way in would
        put a second interpreter between him and the rule."""
        ask = scripted(GOOD)
        rule_compiler.compile_request("never search when I paste a link", ask, TOOLS)
        assert "never search when I paste a link" in ask.prompts[0]

    def test_only_tools_that_exist_are_offered(self):
        ask = scripted(GOOD)
        rule_compiler.compile_request("x", ask, {"read_url"})
        assert "read_url" in ask.prompts[0]
        assert "gws_gmail_send" not in ask.prompts[0]

    def test_the_vocabulary_is_generated_from_the_code(self):
        """A prompt listing the verbs by hand is a second copy, and the first
        thing that happens to a second copy is that it drifts."""
        ask = scripted(GOOD)
        rule_compiler.compile_request("x", ask, TOOLS)
        prompt = ask.prompts[0]
        for detector in rules.ANSWER_DETECTORS:
            assert detector in prompt
        for verb in (rules.VERB_ANSWER_FROM, rules.VERB_MUST_FIRST):
            assert verb in prompt

    def test_json_inside_a_fence_or_prose_is_still_read(self):
        result = rule_compiler.compile_request(
            "x", scripted(f"Sure! Here you go:\n```json\n{GOOD}\n```\n"), TOOLS
        )
        assert result and result.fields["name"] == "bounded-material"

    def test_keys_the_renderer_does_not_know_are_dropped(self):
        """The output space is the schema. A key nobody asked for is not an
        error to argue about — it simply is not a field."""
        reply = json.dumps({**json.loads(GOOD), "tier": 0, "fail": "open"})
        result = rule_compiler.compile_request("x", scripted(reply), TOOLS)
        assert result and "tier" not in result.fields


class TestRetry:
    def test_a_lint_failure_is_fed_back_and_the_retry_lands(self):
        """The instructive-refusal law, applied to authoring: the compiler is
        told what was wrong in the words the lint used."""
        broken = json.dumps({
            "name": "r", "description": "d", "when_subject": "prompt",
            "when_has": "source", "answer_from": "gws_gmial_send",
        })
        ask = scripted(broken, GOOD)
        result = rule_compiler.compile_request("x", ask, TOOLS)
        assert result and result.rounds == 2
        assert "gws_gmial_send" in ask.prompts[1]

    def test_a_reply_that_is_not_json_is_retried(self):
        result = rule_compiler.compile_request(
            "x", scripted("I think you want a rule about links.", GOOD), TOOLS
        )
        assert result and result.rounds == 2

    def test_it_gives_up_rather_than_looping(self):
        junk = json.dumps({"name": "r", "description": "d", "when_subject": "always"})
        result = rule_compiler.compile_request(
            "x", scripted(*[junk] * (rule_compiler.MAX_ROUNDS + 2)), TOOLS
        )
        assert not result
        assert result.rounds == rule_compiler.MAX_ROUNDS
        assert "restricts nothing" in result.problem


class TestCannot:
    """"Not expressible in the current vocabulary" is useless. The refusal has
    to name WHAT, WHY, and what the two options are — and the second option is
    the point: a failed compile is a feature request in structured form."""

    REFUSAL = json.dumps({"cannot": "'be terser' is about style, which nothing "
                                    "here can check"})

    def test_a_refusal_names_the_options(self):
        result = rule_compiler.compile_request("be terser", scripted(self.REFUSAL), TOOLS)
        assert not result
        assert "about style" in result.problem
        assert "rephrase" in result.problem
        assert "extend aish" in result.problem

    def test_a_refusal_is_never_retried_into_an_approximation(self):
        """Asked again, a compiler told the request is inexpressible will
        invent something close — and something close is the failure this whole
        layer exists to prevent, because it looks like it worked."""
        ask = scripted(self.REFUSAL, GOOD)
        result = rule_compiler.compile_request("be terser", ask, TOOLS)
        assert not result and result.rounds == 1
        assert len(ask.prompts) == 1, "a stated impossibility was argued with"

    def test_what_aish_can_enforce_is_listed_from_the_code(self):
        result = rule_compiler.compile_request("x", scripted(self.REFUSAL), TOOLS)
        for detector in rules.ANSWER_DETECTORS:
            assert detector in result.problem


class TestEditing:
    """An edit describes the CHANGE. Regenerating from one sentence is the
    sharpest risk in the design, so the existing rule goes IN and anything the
    reply omits stays exactly as it was."""

    EXISTING = {
        "name": "bounded-material",
        "description": "Answer from the material I gave you.",
        "when_subject": "prompt",
        "when_has": "link",
        "answer_from": "source",
        "never_use": ["web_search"],
    }

    def test_the_current_rule_is_shown_to_the_compiler(self):
        ask = scripted(json.dumps({"when_has": "source"}))
        rule_compiler.compile_request("also cover attachments", ask, TOOLS,
                                      existing=self.EXISTING)
        assert "never_use" in ask.prompts[0] and "ALREADY EXISTS" in ask.prompts[0]

    def test_omitted_fields_survive_the_edit(self):
        result = rule_compiler.compile_request(
            "also cover attachments", scripted(json.dumps({"when_has": "source"})),
            TOOLS, existing=self.EXISTING,
        )
        assert result
        assert result.fields["when_has"] == "source"
        assert result.fields["never_use"] == ["web_search"], (
            "an edit dropped an obligation the instruction never mentioned"
        )
        assert result.fields["answer_from"] == "source"

    def test_a_reply_that_restates_everything_still_cannot_widen_the_rule(self):
        """Even a compiler that ignores the instruction and re-emits the whole
        document cannot add a permission: there is no verb for one."""
        reply = json.dumps({**self.EXISTING, "answer_from": "web_search"})
        result = rule_compiler.compile_request("x", scripted(reply), TOOLS,
                                               existing=self.EXISTING)
        assert result
        rule, _ = rules.lint(rules.render(result.fields), known_tools=TOOLS)
        assert rule is not None
        assert all(o["verb"] in rules.VERBS for o in rule.obligations)
