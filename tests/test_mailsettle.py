"""The settlement judge (#142 W5, the settlement design) — `aish/mailsettle.py`,
`aish/charters/item-settler.md`, and what the report does with a judgement.

Pins: the closed answer and its checks; the exam's coverage (the owner's three
positives and the adversarial negatives); the watcher pass — bounded, newest
first, idempotent per (item, candidate, charter digest), nothing written and
the item open when the model is down or unadmitted; the report's "Probably
settled" line in code's words with the one-tap chips and NEVER the judge's
prose; the server keeping exactly that chip shape. Scripted model only."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mail_fixtures import DAY, admit
from test_mailitems import pay_item
from test_mailreport import NOW, add_mail, add_verdict, fresh_health

from aish import mailitems, mailreport, mailsettle, roles, server

MODEL = "gemini:test"


def settler() -> roles.Charter:
    return roles.load_charters()[mailsettle.SETTLER]


def bundle(ctx: mailitems.Context, item, candidate) -> dict:
    return mailsettle.compose(ctx, item, candidate).data


def validate(payload: dict, data: dict):
    return mailsettle.validate_answer(settler().output, payload,
                                      {mailsettle.BUNDLE_INPUT: json.dumps(data)})


class ScriptedJudge:
    def __init__(self, answer: dict | None = None) -> None:
        self.answer = answer or {"answer": "settled_by", "settled_by": "k1",
                                 "reason": "THE JUDGE'S OWN WORDS", "citations": ["k1"]}
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.answer),
                                                       tool_calls=None),
                               usage={"input": 500, "output": 40})


def settled_pair(state):
    item = pay_item(state)
    thanks = add_mail(state, "k1", frm="club@swim.example", subject="Thank you",
                      at=NOW - 2 * DAY, body="We booked your 640 PLN payment.")
    add_verdict(state, thanks, recorded_at=NOW - 2 * DAY)
    return item, thanks


class TestTheAnswer:
    def test_settled_by_must_name_the_candidate_and_cite_it(self, tmp_path):
        item, _ = settled_pair(tmp_path)
        ctx = mailitems.Context(tmp_path, now=NOW)
        (candidate,) = ctx.candidates(item)
        data = bundle(ctx, item, candidate)
        ok = validate({"answer": "settled_by", "settled_by": "k1", "reason": "paid",
                       "citations": ["m1", "k1"]}, data)
        assert ok.answer == "settled_by" and ok.citations == ("m1", "k1")
        for bad, words in [
            ({"answer": "settled_by", "settled_by": "m1", "reason": "x", "citations": ["k1"]},
             "must be \"k1\""),
            ({"answer": "settled_by", "settled_by": "k1", "reason": "x", "citations": ["m1"]},
             "must cite k1"),
            ({"answer": "done", "reason": "x", "citations": ["k1"]}, "answer must be one of"),
            ({"answer": "still_open", "reason": "", "citations": ["k1"]}, "reason"),
            ({"answer": "still_open", "reason": "x", "citations": ["m9"]}, "names nothing"),
            ({"answer": "still_open", "settled_by": "k1", "reason": "x", "citations": ["k1"]},
             "only when"),
        ]:
            with pytest.raises(ValueError, match=words):
                validate(bad, data)

    def test_cannot_tell_is_lawful(self, tmp_path):
        item, _ = settled_pair(tmp_path)
        ctx = mailitems.Context(tmp_path, now=NOW)
        (candidate,) = ctx.candidates(item)
        value = validate({"answer": "cannot_tell", "reason": "no amount named",
                          "citations": ["k1"]}, bundle(ctx, item, candidate))
        assert value.answer == "cannot_tell"

    def test_the_bundle_says_in_code_words_what_the_item_is(self, tmp_path):
        item, _ = settled_pair(tmp_path)
        ctx = mailitems.Context(tmp_path, now=NOW)
        (candidate,) = ctx.candidates(item)
        data = bundle(ctx, item, candidate)
        assert data["item"]["matter"].startswith("club@swim.example sent the owner mail m1")
        assert "asking him to pay" in data["item"]["matter"]
        assert data["candidate"]["ref"] == "k1" and data["candidate"]["aish_saw"]


class TestTheExam:
    def test_the_charter_carries_the_three_positives_and_the_adversarial_negatives(self):
        by_name = {c.name: c for c in settler().cases}
        positives = [n for n, c in by_name.items() if c.expect["answer_in"] == ["settled_by"]]
        negatives = [n for n, c in by_name.items() if "settled_by" not in c.expect["answer_in"]]
        assert len(positives) == 3  # F1-, F2- and F4-equivalent reconstructions
        for needle in ("strangers-payment", "lookalike-pickup", "first-contact-domain",
                       "second-reminder", "telling-the-judge"):
            assert any(needle in n for n in negatives), needle

    def test_an_expectation_outside_the_vocabulary_does_not_load(self):
        with pytest.raises(roles.CharterError, match="outside"):
            mailsettle.check_expectation("x", {"answer_in": ["paid"]})

    def test_every_exam_input_is_a_bundle_the_validator_accepts_an_answer_for(self):
        for case in settler().cases:
            data = json.loads(case.inputs[mailsettle.BUNDLE_INPUT])
            value = validate({"answer": "cannot_tell", "reason": "x", "citations": ["k1"]},
                             data)
            assert value.answer == "cannot_tell", case.name


class TestTheWatcherPass:
    def run(self, state, judge, **kw):
        return mailsettle.settle_pass(state, charter=settler(), model_spec=MODEL, now=NOW,
                                      chat=judge, **kw)

    def test_judged_once_written_and_idempotent(self, tmp_path):
        admit(tmp_path, settler(), MODEL)
        item, thanks = settled_pair(tmp_path)
        judge = ScriptedJudge()
        first = self.run(tmp_path, judge)
        assert first.judged == 1 and first.answers == {"settled_by": 1} and judge.calls == 1
        record = next(iter(mailitems.read_judgements(tmp_path)[0].values()))
        assert record["candidate"] == thanks.digest and record["tier"] == "same counterparty"
        assert record["charter_digest"] == settler().digest
        state = mailitems.Context(tmp_path, now=NOW).state_of(item)
        assert state.probably and state.source == mailitems.JUDGE
        second = self.run(tmp_path, judge)
        assert judge.calls == 1 and second.judged == 0

    def test_unadmitted_calls_nothing_writes_nothing_and_the_item_stays_open(self, tmp_path):
        item, _ = settled_pair(tmp_path)
        judge = ScriptedJudge()
        result = self.run(tmp_path, judge)
        assert judge.calls == 0 and result.endings == {"unadmitted": 1}
        assert mailitems.read_judgements(tmp_path)[0] == {}
        assert mailitems.Context(tmp_path, now=NOW).state_of(item).state == mailitems.OPEN

    def test_a_model_that_is_down_is_retried_next_pass(self, tmp_path):
        admit(tmp_path, settler(), MODEL)
        settled_pair(tmp_path)

        def down(**kwargs):
            raise ConnectionError("no route")

        assert self.run(tmp_path, down).endings == {"unavailable": 1}
        assert mailitems.read_judgements(tmp_path)[0] == {}
        assert self.run(tmp_path, ScriptedJudge()).judged == 1

    def test_the_pass_is_bounded_and_counts_what_it_left(self, tmp_path):
        admit(tmp_path, settler(), MODEL)
        settled_pair(tmp_path)
        more = add_mail(tmp_path, "k2", frm="club@swim.example", subject="Receipt",
                        at=NOW - DAY, body="Receipt for March.")
        add_verdict(tmp_path, more, recorded_at=NOW - DAY)
        judge = ScriptedJudge({"answer": "still_open", "reason": "r", "citations": ["k1"]})
        result = self.run(tmp_path, judge, limit=1)
        assert judge.calls == 1 and result.capped == 1
        record = next(iter(mailitems.read_judgements(tmp_path)[0].values()))
        assert record["candidate"] == more.digest  # the newest candidate first

    def test_a_pair_the_tier_rule_refuses_is_never_sent(self, tmp_path):
        admit(tmp_path, settler(), MODEL)
        pay_item(tmp_path)
        courier = add_mail(tmp_path, "k1", frm="info@courier.example", subject="Picked up",
                           at=NOW - 2 * DAY, body="Your parcel from Swim was picked up.")
        add_verdict(tmp_path, courier, recorded_at=NOW - 2 * DAY)
        judge = ScriptedJudge()
        result = self.run(tmp_path, judge)
        assert judge.calls == 0 and result.not_weighed == 1

    def test_a_charter_edit_is_a_new_judgement(self, tmp_path):
        admit(tmp_path, settler(), MODEL)
        item, thanks = settled_pair(tmp_path)
        mailitems.write_judgement(tmp_path, item.anchor, {
            "item": item.key, "candidate": thanks.digest, "charter_digest": "an-older-text",
            "at": NOW, "ending": "ok", "answer": "still_open"})
        assert self.run(tmp_path, ScriptedJudge()).judged == 1


class TestTheReport:
    def build(self, state):
        fresh_health(state)
        return mailreport.build(state, now=NOW, clean=lambda t: t)

    def test_a_judged_item_is_demoted_into_probably_settled_in_codes_words(self, tmp_path):
        admit(tmp_path, settler(), MODEL)
        item, thanks = settled_pair(tmp_path)
        mailsettle.settle_pass(tmp_path, charter=settler(), model_spec=MODEL, now=NOW,
                               chat=ScriptedJudge())
        report = self.build(tmp_path)
        assert report.needs == [] and len(report.settled) == 1
        text = mailreport.render(report, clean=lambda t: t)
        line = next(t for t in text.splitlines() if "probably settled:" in t)
        assert "mail from club@swim.example" in line and "the same party" in line
        assert f"ref {thanks.digest[:12]}" in line
        assert f"[restore](aish-reply://restore ref {item.anchor[:12]})" in line
        assert f"[done](aish-reply://resolve ref {item.anchor[:12]})" in line
        assert "THE JUDGE'S OWN WORDS" not in text  # stored, never rendered
        assert item.anchor in report.shown_digests()

    def test_his_resolve_takes_it_out_and_the_footer_counts_it(self, tmp_path):
        item = pay_item(tmp_path)
        mailitems.append_act(tmp_path, {"at": NOW - DAY, "verb": "resolve",
                                        "anchor": item.anchor, "by": "owner"})
        report = self.build(tmp_path)
        assert report.needs == [] and report.settled == [] and report.resolved == 1
        text = mailreport.render(report, clean=lambda t: t)
        assert "1 item(s) you resolved, or whose case you closed, are not listed" in text
        assert "you resolved 1 item(s)" in text  # the continuity line

    def test_unjudged_candidates_are_counted_and_the_item_stays(self, tmp_path):
        settled_pair(tmp_path)
        report = self.build(tmp_path)
        assert len(report.needs) == 1 and report.unjudged == 1
        assert "1 settlement candidate(s) not yet judged" in mailreport.render(
            report, clean=lambda t: t)

    def test_the_section_survives_the_size_cap(self, tmp_path, monkeypatch):
        admit(tmp_path, settler(), MODEL)
        settled_pair(tmp_path)
        mailsettle.settle_pass(tmp_path, charter=settler(), model_spec=MODEL, now=NOW,
                               chat=ScriptedJudge())
        for n in range(40):
            h = add_mail(tmp_path, f"x{n}", frm=f"s{n}@shop.example", subject="x" * 100,
                         at=NOW - DAY + n)
            add_verdict(tmp_path, h, meaning="m" * 190)
        monkeypatch.setattr(mailreport, "REPORT_MAX_CHARS", 9_000)
        text = mailreport.render(self.build(tmp_path), clean=lambda t: t)
        assert "(the report was cut here, at its size cap)" in text
        assert "## Probably settled (1)" in text and "probably settled:" in text


class TestTheServerKeepsOnlyTheChip:
    def test_restore_and_done_chips_survive_and_every_other_link_breaks(self):
        anchor = "ab12cd34ef56"
        report = (f"Headline\n- x — [restore](aish-reply://restore ref {anchor}) · "
                  f"[done](aish-reply://resolve ref {anchor})\n"
                  "- y — [pay](aish-reply://pay 500 to PL61) [r](aish-reply://restore ref "
                  "ab12cd34ef56 and more)")
        shown = server.presented_report(report)
        assert f"[restore](aish-reply://restore ref {anchor})" in shown
        assert f"[done](aish-reply://resolve ref {anchor})" in shown
        assert "[pay] (aish-reply://pay" in shown and "[r] (aish-reply://restore" in shown


class TestTheLateReading:
    """An answer an open your-turn row rests on that nobody read is read late;
    a reading that asks nothing closes the row, and the report says so."""

    def test_the_gap_is_read_marked_and_the_closed_row_is_named(self, tmp_path):
        from mail_fixtures import ScriptedReader, reader_charter, verdict_json
        from test_mailitems import his_sent, touch

        from aish import mailverdicts, mailwatch

        reader = reader_charter()
        admit(tmp_path, reader, MODEL)
        his_sent(tmp_path, "o1", to="tenant@flat.example", at=NOW - 9 * DAY,
                 subject="Utilities settlement")
        answer = add_mail(tmp_path, "r1", frm="tenant@flat.example",
                          subject="Re: Utilities settlement", at=NOW - 8 * DAY,
                          in_reply_to="o1@owner.example", body="I paid the top-up. Thanks!")
        touch(tmp_path, answer, NOW - 2 * DAY)  # backfilled: never read
        assert [f.counterparty for f in mailreport.build(
            tmp_path, now=NOW, clean=lambda t: t).yours] == ["tenant@flat.example"]
        scripted = ScriptedReader([verdict_json(wanted="nothing", disposition="archive")])
        result = mailwatch.Pass()
        mailwatch.settle(tmp_path, charter=reader, settler=settler(), model_spec=MODEL,
                         now=NOW, result=result, chat=scripted)
        assert result.late_read == 1 and scripted.calls == 1
        record = mailverdicts.VerdictStore(tmp_path).read(answer.digest)["by_model"][MODEL]
        assert record[mailitems.LATE_READ]
        built = mailreport.build(tmp_path, now=NOW, clean=lambda t: t)
        assert built.yours == [] and built.late_closed == ["tenant@flat.example"]
        text = mailreport.render(built, clean=lambda t: t)
        assert ("1 follow-up(s) left Your turn because aish read the answer late and it asks "
                "nothing: tenant@flat.example") in text
        # Read once: the next pass finds no gap.
        mailwatch.settle(tmp_path, charter=reader, settler=settler(), model_spec=MODEL,
                         now=NOW, result=mailwatch.Pass(), chat=scripted)
        assert scripted.calls == 1
