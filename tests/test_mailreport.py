"""The daily inbox report (#142 W5, the owner's premise amendment).

Pins the section rules, the deterministic follow-ups (your turn vs their
turn, the N-day boundary, both inboxes, per counterparty), the exclusions,
the caution markers, the sanitiser on every field, that no content-derived
URL survives, the stale-watcher banner, the empty day, the dry run, delivery
and the ledger. Fixtures only: no mail, no model, no network, no send."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import urllib.error

import pytest
from mail_fixtures import DAY, OWNER, T0, gmail

from aish import cli, mailindex, mailreport, mailverdicts, mailview, mailwatch, server, vocab

NOW = T0 + 30 * DAY
HOSTILE = "Pay\x9b2J now‮evil\x1b[31mred\x1b]8;;http://x.example\x07link\x1b\\  end"


def identity(text: str) -> str:
    return text


def fresh_health(state, now: float = NOW) -> None:
    path = mailwatch.health_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_pass_at": now - 60, "last_pass": {}, "sources": {}}))


def add_mail(
    state, gid: str, *, frm: str, at: float, to: str = OWNER, subject: str = "",
    sent: bool = False, mid: str = "", in_reply_to: str = "", references: str = "",
    list_id: str = "", mailbox: str = "gmail", body: str = "body", bcc: str = "",
) -> mailindex.Header:
    parsed = mailindex.from_gmail(
        gmail(gid, frm=frm, to=to, subject=subject, body=body, at=at,
              message_id=mid or f"{gid}@mail.example", in_reply_to=in_reply_to,
              references=references, labels=("SENT",) if sent else ("INBOX",),
              list_id=list_id, bcc=bcc),
        frozenset({OWNER}),
    )
    assert parsed is not None
    header = parsed.header
    if mailbox == "tuta":
        digest = hashlib.sha256(f"tuta:{gid}".encode()).hexdigest()
        header = dataclasses.replace(header, mailbox="tuta", at_source="header",
                                     digest=digest, provider_id="7")
    mailindex.MailIndex(state).add(header, parsed.body)
    return header


def add_verdict(
    state, header: mailindex.Header, *, wanted: str = "nothing", disposition: str = "surface",
    deadline: str = "none", suspicion: str = "none", sender_case: str = "consistent",
    kind: str = "notice", meaning: str = "A notice.", ending: str = "ok", gated: bool = False,
    recorded_at: float | None = None, case_flags: dict | None = None,
    keeper: bool = False, model: str = "gemini:test", auth: bool | None = None,
) -> None:
    gate: dict = {"newsletter": {"gated": gated, "by": ["List-Id"] if gated else []}}
    if auth is not None:
        gate["auth"] = {"authenticated": auth}
    record = {
        "kind": "mail_verdict", "mail": header.digest, "provider_id": header.provider_id,
        "at": header.at, "at_source": header.at_source, "model": model,
        "ending": "gated" if gated else ending,
        "gate": gate,
        "case_flags": case_flags or {},
        "verdict": None if gated or ending != "ok" else {
            "disposition": disposition, "wanted": wanted, "deadline": deadline,
            "suspicion": suspicion, "sender_case": sender_case, "kind": kind,
            "meaning": meaning,
        },
        "keeper_clamped": ({"from": disposition, "to": "surface", "signals": ["case_key"],
                            "cases": []} if keeper else None),
        "recorded_at": recorded_at if recorded_at is not None else header.at + 60,
    }
    mailverdicts.VerdictStore(state).write(record)


def build(state, **kw) -> mailreport.Report:
    kw.setdefault("now", NOW)
    kw.setdefault("clean", identity)
    return mailreport.build(state, **kw)


def subjects(items) -> list[str]:
    return [i.header.subject for i in items]


# ---------------------------------------------------------------- sections


class TestNeedsYourAction:
    def test_pay_and_reply_earn_a_line_decide_alone_is_counted_wait_and_nothing_are_not(
        self, tmp_path
    ):
        fresh_health(tmp_path)
        for n, wanted in enumerate(("pay", "reply", "decide", "wait", "nothing")):
            h = add_mail(tmp_path, f"m{n}", frm=f"s{n}@shop.example", subject=wanted,
                         at=NOW - DAY + n)
            add_verdict(tmp_path, h, wanted=wanted, disposition="hold")
        report = build(tmp_path)
        assert sorted(subjects(report.needs)) == ["pay", "reply"]
        assert subjects(report.counted_asks) == ["decide"]
        assert report.counted_visible == []  # wanted nothing, but held — not "surface"

    def test_order_is_dated_deadlines_soonest_then_pay_reply_decide(self, tmp_path):
        fresh_health(tmp_path)
        rows = [("decide-undated", "decide", "none"), ("pay-undated", "pay", "none"),
                ("reply-undated", "reply", "cannot_tell"), ("late", "decide", "2026-12-20"),
                ("soon", "reply", "2026-11-02")]
        for n, (subject, wanted, deadline) in enumerate(rows):
            h = add_mail(tmp_path, f"m{n}", frm=f"s{n}@shop.example", subject=subject,
                         at=NOW - DAY + n)
            add_verdict(tmp_path, h, wanted=wanted, deadline=deadline, disposition="hold")
        report = build(tmp_path)
        # A dated `decide` keeps its line and its word; an undated one is counted.
        assert subjects(report.needs) == ["soon", "late", "pay-undated", "reply-undated"]
        assert subjects(report.counted_asks) == ["decide-undated"]

    def test_within_one_deadline_rank_new_lines_come_first(self, tmp_path):
        fresh_health(tmp_path)
        old = add_mail(tmp_path, "old", frm="a@shop.example", subject="old", at=NOW - 3 * DAY)
        add_verdict(tmp_path, old, wanted="pay")
        mailreport.append_ledger(tmp_path, {"at": NOW - DAY, "as_of": NOW - DAY,
                                            "status": "delivered", "digests": [old.digest]})
        new = add_mail(tmp_path, "new", frm="b@shop.example", subject="new", at=NOW - 2 * DAY)
        add_verdict(tmp_path, new, wanted="reply")
        report = build(tmp_path)
        assert subjects(report.needs) == ["new", "old"]
        assert [i.new for i in report.needs] == [True, False]

    def test_a_line_is_short_and_names_what_by_when_who_subject_and_ref(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "abc123def0", frm="Faktury <billing@shop.example>",
                     subject="Faktura 2026/41", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", deadline="2026-11-05", disposition="hold",
                    meaning="An invoice for the October order.")
        text = mailreport.render(build(tmp_path), clean=identity, account="me@example.org")
        line = next(t for t in text.splitlines() if t.startswith("- PAY"))
        assert line.startswith("- PAY, by 2026-11-05 — billing@shop.example — “Faktura 2026/41”")
        assert "Faktury" not in line  # a display name is the sender's own words
        assert " — new" in line and f"ref {h.digest[:12]}" in line
        # The subject says little ("Faktura" alone), so the Reader's sentence adds.
        assert "the Reader: An invoice for the October order." in line
        assert "gmail, " not in line and mailreport._when(h.at) not in line
        assert ("[Gmail](https://mail.google.com/mail/u/0/?authuser=me%40example.org"
                "#all/abc123def0)") in line

    def test_the_readers_sentence_is_left_out_when_the_subject_says_it(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="billing@shop.example",
                     subject="Your October invoice is ready to pay", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", meaning="An invoice to pay.")
        line = next(t for t in mailreport.render(build(tmp_path), clean=identity).splitlines()
                    if t.startswith("- PAY"))
        assert "the Reader:" not in line
        assert len(line) <= 160


def facts_of(state, digest: str) -> tuple[str, ...]:
    report = build(state)
    found = [i for line in report.needs for i in line.mails() if i.reading.digest == digest]
    return found[0].facts if found else ()


class TestTheLineTest:
    """Change 1: a recorded fact earns a line; `decide` alone is counted."""

    def test_decide_alone_is_counted_not_listed(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "d", frm="a@shop.example", subject="choose", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="decide")
        report = build(tmp_path)
        assert report.needs == [] and subjects(report.counted_asks) == ["choose"]
        text = mailreport.render(report, clean=identity)
        assert ("1 more mail(s) ask something, with no date, amount or correspondent behind "
                "it — counted, not listed; `/mail` in the terminal lists them.") in text
        assert "choose" not in text

    def test_a_dated_deadline_earns_a_line(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "d", frm="a@shop.example", subject="choose", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="decide", deadline="2026-12-01")
        assert facts_of(tmp_path, h.digest) == ("dated deadline",)

    def test_a_stored_amount_earns_a_line_and_is_on_it(self, tmp_path, monkeypatch):
        from aish import mailitems

        fresh_health(tmp_path)
        h = add_mail(tmp_path, "d", frm="a@shop.example", subject="choose", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="decide")
        monkeypatch.setattr(mailitems.Context, "stored_amounts",
                            lambda self, d: frozenset({("PLN", 12050)}) if d == h.digest
                            else frozenset())
        assert facts_of(tmp_path, h.digest) == ("money recorded",)
        text = mailreport.render(build(tmp_path), clean=identity)
        assert "— 120,50 PLN —" in text

    def test_a_recorded_payment_account_earns_a_line(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "d", frm="a@shop.example", subject="choose", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="decide")
        path = next(mailverdicts.store_dir(tmp_path).glob("*/*.json"))
        raw = json.loads(path.read_text())
        for record in raw["by_model"].values():
            record["code_coordinates"] = ["PL61109010140000071219812874"]
        path.write_text(json.dumps(raw))
        assert facts_of(tmp_path, h.digest) == ("money recorded",)

    def test_an_authenticated_sender_he_wrote_to_earns_a_line(self, tmp_path):
        fresh_health(tmp_path)
        add_mail(tmp_path, "o", frm=OWNER, to="anna@client.example", subject="hello",
                 at=NOW - 20 * DAY, sent=True)
        h = add_mail(tmp_path, "d", frm="anna@client.example", subject="choose", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="decide")
        assert facts_of(tmp_path, h.digest) == ("a sender you write to",)
        assert "- DECIDE — anna@client.example" in mailreport.render(build(tmp_path),
                                                                     clean=identity)

    def test_an_unauthenticated_from_he_wrote_to_earns_nothing(self, tmp_path):
        """An unauthenticated From is a claim: it inherits nobody's history."""
        fresh_health(tmp_path)
        add_mail(tmp_path, "o", frm=OWNER, to="anna@client.example", subject="hello",
                 at=NOW - 20 * DAY, sent=True)
        forged = mailindex.from_gmail(
            gmail("forged", frm="anna@client.example", to=OWNER, subject="choose",
                  at=NOW - DAY, auth="fail"), frozenset({OWNER}))
        assert forged is not None
        mailindex.MailIndex(tmp_path).add(forged.header, forged.body)
        add_verdict(tmp_path, forged.header, wanted="decide")
        report = build(tmp_path)
        assert report.needs == [] and subjects(report.counted_asks) == ["choose"]

    def test_mail_from_a_sender_he_wrote_to_only_later_earns_nothing_yet(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "d", frm="anna@client.example", subject="choose",
                     at=NOW - 2 * DAY)
        add_verdict(tmp_path, h, wanted="decide")
        add_mail(tmp_path, "o", frm=OWNER, to="anna@client.example", subject="Re: choose",
                 at=NOW - DAY, sent=True, in_reply_to="d@mail.example")
        assert facts_of(tmp_path, h.digest) == ()

    def test_his_show_first_preference_earns_a_line(self, tmp_path):
        from aish import mailprefs

        fresh_health(tmp_path)
        h = add_mail(tmp_path, "d", frm="a@shop.example", subject="choose", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="decide")
        planned = mailprefs.plan(tmp_path, {"action": "add", "kind": "show_first",
                                            "sender": "a@shop.example"})
        assert mailprefs.apply(tmp_path, planned, source={"session": "t"},
                               now=NOW - 2 * DAY).ok
        assert facts_of(tmp_path, h.digest) == ("your preference",)

    @pytest.mark.parametrize("marks", [{"suspicion": "suspicious"},
                                       {"sender_case": "mismatch"}])
    def test_any_caution_earns_its_own_line(self, tmp_path, marks):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "d", frm="a@shop.example", subject="choose", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="decide", **marks)
        assert facts_of(tmp_path, h.digest) == ("caution",)

    def test_a_counted_mail_that_later_earns_a_fact_gets_its_line(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "d", frm="anna@client.example", subject="choose",
                     at=NOW - 2 * DAY)
        add_verdict(tmp_path, h, wanted="decide")
        post = FakePost([ok()])
        run(tmp_path, post, now=NOW - DAY)
        (entry,) = mailreport.read_ledger(tmp_path)
        assert entry["counted"] == [h.digest] and entry["digests"] == []
        assert build(tmp_path).counted_asks == []  # counted once: the count is news
        # He writes to her about something else; the line test now passes and
        # the earlier count does not bury it.
        add_mail(tmp_path, "o", frm=OWNER, to="anna@client.example", subject="other",
                 at=NOW - 3 * DAY, sent=True)
        assert subjects(build(tmp_path).needs) == ["choose"]


class TestMayInterestIsCounted:
    def test_kept_visible_mail_that_asks_nothing_is_counted_and_filed_mail_is_not(
        self, tmp_path
    ):
        fresh_health(tmp_path)
        a = add_mail(tmp_path, "a", frm="a@news.example", subject="surfaced", at=NOW - DAY)
        add_verdict(tmp_path, a, wanted="nothing", disposition="surface")
        b = add_mail(tmp_path, "b", frm="b@shop.example", subject="archived", at=NOW - DAY)
        add_verdict(tmp_path, b, wanted="nothing", disposition="archive")
        c = add_mail(tmp_path, "c", frm="c@shop.example", subject="keeper", at=NOW - DAY)
        add_verdict(tmp_path, c, wanted="nothing", disposition="archive", keeper=True)
        report = build(tmp_path)
        assert sorted(subjects(report.counted_visible)) == ["keeper", "surfaced"]
        assert report.needs == []
        text = mailreport.render(report, clean=identity)
        assert "May interest you" not in text and "surfaced" not in text
        assert "2 new mail(s) the assistant would keep visible ask nothing — counted" in text
        assert report.counts()["counted"] == 2

    def test_a_cautioned_mail_that_asks_nothing_keeps_its_own_line_once(self, tmp_path):
        """The caution floor: removing the section must not turn a flagged mail
        into a number."""
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@news.example", subject="Account change",
                     at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="nothing", disposition="surface",
                    suspicion="suspicious")
        report = build(tmp_path)
        assert subjects(report.needs) == ["Account change"] and report.counted_visible == []
        line = next(t for t in mailreport.render(report, clean=identity).splitlines()
                    if "Account change" in t)
        assert line.startswith("- **CAUTION: the Reader judged it suspicious.** ASKS NOTHING")
        assert "leaves the brief" not in line
        run(tmp_path, FakePost([ok()]), now=NOW)
        assert build(tmp_path, now=NOW + DAY).needs == []  # listed once, while news


class TestExclusions:
    def test_newsletter_gated_and_one_time_codes_are_excluded_everywhere_and_counted(
        self, tmp_path
    ):
        fresh_health(tmp_path)
        gated = add_mail(tmp_path, "g", frm="news@list.example", subject="Weekly", at=NOW - DAY)
        add_verdict(tmp_path, gated, gated=True)
        by_kind = add_mail(tmp_path, "k", frm="id@bank.example", subject="Sign in", at=NOW - DAY)
        add_verdict(tmp_path, by_kind, wanted="decide", kind="verification code")
        by_subject = add_mail(tmp_path, "s", frm="id@gov.example",
                              subject="Twój kod weryfikacyjny: 491022", at=NOW - DAY)
        add_verdict(tmp_path, by_subject, wanted="nothing", kind="notice")
        link = add_mail(tmp_path, "l", frm="x@air.example", subject="Your sign-in link",
                        at=NOW - DAY)
        add_verdict(tmp_path, link, wanted="nothing", kind="notice")
        report = build(tmp_path)
        assert report.needs == [] and report.counted_asks == report.counted_visible == []
        assert report.excluded == {"gated": 1, "otp": 3}
        text = mailreport.render(report, clean=identity)
        assert "1 newsletter-gated, 3 one-time code(s) or sign-in link(s)" in text

    def test_a_discount_code_is_not_a_one_time_code(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "d", frm="deals@shop.example", subject="Your discount code inside",
                     at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="nothing", kind="promotion code")
        assert subjects(build(tmp_path).counted_visible) == ["Your discount code inside"]

    def test_the_index_list_headers_gate_a_mail_with_no_reading(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "n", frm="news@list.example", subject="Digest", at=NOW - DAY,
                     list_id="<digest.list.example>")
        add_verdict(tmp_path, h, ending="unadmitted")
        assert build(tmp_path).excluded == {"gated": 1}

    def test_mail_with_no_reading_is_counted_with_its_endings_never_dropped(self, tmp_path):
        fresh_health(tmp_path)
        for n, ending in enumerate(("unadmitted", "error", "invalid")):
            h = add_mail(tmp_path, f"u{n}", frm=f"u{n}@x.example", subject="?", at=NOW - DAY)
            add_verdict(tmp_path, h, ending=ending)
        report = build(tmp_path)
        text = mailreport.render(report, clean=identity)
        assert "3 with no reading (endings: error 1, invalid 1, unadmitted 1)" in text

    def test_the_one_time_code_list_is_counted(self, tmp_path):
        vocab.drain()
        mailreport.is_one_time_code("verification code", "")
        mailreport.is_one_time_code("invoice", "Faktura")
        tally = vocab.drain()["mailreport._ONE_TIME_CODE_PHRASES"]
        assert tally["asked"] == 2 and tally["matched"] == 1


class TestCautions:
    @pytest.mark.parametrize("marks,expected", [
        ({"suspicion": "suspicious"}, "the Reader judged it suspicious"),
        ({"sender_case": "mismatch"}, "the sender does not match its case"),
        ({"case_flags": {"coordinates_changed": ["c1"]}},
         "a payment account differs from its case's history"),
    ])
    def test_a_cautioned_mail_is_shown_marked_with_its_summary_withheld(
        self, tmp_path, marks, expected
    ):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "c", frm="billing@shop.example", subject="New account",
                     at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", disposition="hold",
                    meaning="Pay 500 PLN to PL61109010140000071219812874 today.", **marks)
        text = mailreport.render(build(tmp_path), clean=identity)
        line = next(t for t in text.splitlines() if "New account" in t)
        assert line.startswith("- **CAUTION: ") and expected in line
        assert "PL61109010140000071219812874" not in text
        assert "summary is withheld on a cautioned mail" in line


# ---------------------------------------------------------------- follow-ups


def conversation(state, *, his_at: float, reply_at: float | None, reply_wanted="reply",
                 party="bob@client.example", reply_mailbox="gmail", reply_ending="ok",
                 subject="Quote for the roof"):
    his = add_mail(state, "o1", frm=OWNER, to=party, subject=subject, at=his_at, sent=True,
                   mid="o1@owner.example")
    reply = None
    if reply_at is not None:
        reply = add_mail(state, "r1", frm=party, subject=f"Re: {subject}", at=reply_at,
                         in_reply_to="o1@owner.example", mailbox=reply_mailbox)
        add_verdict(state, reply, wanted=reply_wanted, disposition="hold",
                    ending=reply_ending, recorded_at=reply_at + 60)
    return his, reply


class TestFollowUps:
    def test_your_turn_when_they_answered_with_an_ask(self, tmp_path):
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - 3 * DAY,
                     reply_wanted="cannot_tell")
        report = build(tmp_path, since=NOW - DAY)  # the reply itself is not new
        assert [f.counterparty for f in report.yours] == ["bob@client.example"]
        assert report.theirs == []
        text = mailreport.render(report, clean=identity)
        assert "3 day(s) waiting on you — bob@client.example sent a message after your " \
               "“Quote for the roof” (the Reader: cannot_tell)" in text

    @pytest.mark.parametrize("wanted", ["reply", "decide", "pay"])
    def test_an_ask_from_someone_he_wrote_to_is_one_open_line_not_two(self, tmp_path, wanted):
        """He wrote to them, so the ask earns a line (and stays every morning
        while open); the conversation is not repeated under Your turn."""
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - 3 * DAY,
                     reply_wanted=wanted)
        report = build(tmp_path, since=NOW - DAY)
        assert subjects(report.needs) == ["Re: Quote for the roof"]
        assert report.yours == []

    def test_an_undecided_reading_is_your_turn(self, tmp_path):
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - 3 * DAY,
                     reply_wanted="cannot_tell")
        assert len(build(tmp_path, since=NOW - DAY).yours) == 1

    def test_no_reading_is_your_turn_and_says_so(self, tmp_path):
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - 3 * DAY,
                     reply_ending="unadmitted")
        report = build(tmp_path, since=NOW - DAY)
        assert len(report.yours) == 1
        assert "(the Reader: no reading recorded)" in mailreport.render(report, clean=identity)

    @pytest.mark.parametrize("wanted", ["nothing", "wait"])
    def test_an_answer_that_asks_nothing_is_neither_turn(self, tmp_path, wanted):
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - 3 * DAY,
                     reply_wanted=wanted)
        report = build(tmp_path, since=NOW - DAY)
        assert report.yours == [] and report.theirs == []

    def test_their_turn_from_exactly_n_days_and_not_a_second_before(self, tmp_path):
        fresh_health(tmp_path)
        n = mailreport.THEIR_TURN_DAYS * DAY
        conversation(tmp_path, his_at=NOW - n, reply_at=None)
        assert [f.counterparty for f in build(tmp_path).theirs] == ["bob@client.example"]
        assert build(tmp_path, now=NOW - 1).theirs == []

    def test_their_turn_line_shows_his_subject_and_days(self, tmp_path):
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 6 * DAY, reply_at=None)
        text = mailreport.render(build(tmp_path), clean=identity)
        assert ("6 day(s) with no reply — you wrote to bob@client.example: "
                "“Quote for the roof”") in text

    def test_both_inboxes_his_gmail_mail_answered_through_tuta(self, tmp_path):
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - 2 * DAY,
                     reply_mailbox="tuta", reply_wanted="cannot_tell")
        report = build(tmp_path, since=NOW - DAY)
        assert len(report.yours) == 1 and report.yours[0].latest.mailbox == "tuta"

    def test_a_stranger_joining_by_references_is_not_an_answer(self, tmp_path):
        """A conversation is a union any mail can join by naming a Message-ID;
        only someone he WROTE TO can make it his turn — and the stranger
        cannot hide that bob never answered."""
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=None)
        joiner = add_mail(tmp_path, "x", frm="attacker@evil.example", subject="Re: urgent",
                          at=NOW - DAY, references="<o1@owner.example>")
        add_verdict(tmp_path, joiner, wanted="pay", disposition="surface")
        report = build(tmp_path, since=NOW - 2 * DAY)
        assert report.yours == []
        assert [f.counterparty for f in report.theirs] == ["bob@client.example"]

    def test_notes_to_himself_and_to_the_bot_are_not_conversations(self, tmp_path):
        fresh_health(tmp_path)
        add_mail(tmp_path, "self", frm=OWNER, to=OWNER, subject="note", at=NOW - 9 * DAY,
                 sent=True)
        add_mail(tmp_path, "bot", frm=OWNER, to="aish@wenda.eu", subject="do x",
                 at=NOW - 9 * DAY, sent=True)
        assert build(tmp_path).theirs == []

    def test_a_mailing_list_sender_is_not_waiting_on_him(self, tmp_path):
        fresh_health(tmp_path)
        add_mail(tmp_path, "o", frm=OWNER, to="list@groups.example", subject="unsubscribe",
                 at=NOW - 9 * DAY, sent=True)
        add_mail(tmp_path, "l", frm="list@groups.example", subject="digest", at=NOW - 20 * DAY,
                 list_id="<g.groups.example>")
        assert build(tmp_path).theirs == []

    def test_a_mail_already_under_needs_your_action_is_not_repeated(self, tmp_path):
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - DAY / 2)
        report = build(tmp_path)
        assert subjects(report.needs) == ["Re: Quote for the roof"]
        assert report.yours == []

    def test_outside_the_window_nothing_is_followed_up(self, tmp_path):
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 61 * DAY, reply_at=None)
        assert build(tmp_path).theirs == []

    def test_a_sender_dated_future_answer_is_ordered_by_when_aish_first_read_it(
        self, tmp_path
    ):
        """Tuta's date is the sender's: an answer read BEFORE his message but
        dated after it does not count as answering him."""
        fresh_health(tmp_path)
        his = add_mail(tmp_path, "o1", frm=OWNER, to="bob@client.example", subject="Q",
                       at=NOW - 6 * DAY, sent=True, mid="o1@owner.example")
        old = add_mail(tmp_path, "r0", frm="bob@client.example", subject="Re: Q",
                       at=NOW + 30 * DAY, mailbox="tuta", references="<o1@owner.example>")
        add_verdict(tmp_path, old, wanted="nothing", recorded_at=his.at - DAY)
        assert [f.counterparty for f in build(tmp_path).theirs] == ["bob@client.example"]

    def test_no_sent_mail_in_the_index_says_follow_ups_cannot_be_computed(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "i", frm="a@x.example", subject="hi", at=NOW - DAY)
        add_verdict(tmp_path, h)
        text = mailreport.render(build(tmp_path), clean=identity)
        assert "The thread index holds none of your sent mail from the last 60 days" in text
        assert "follow-ups cannot be computed from it" in text


# ---------------------------------------------------------------- eligibility


class TestWhatIsNew:
    def test_the_first_report_counts_seven_days(self, tmp_path):
        fresh_health(tmp_path)
        for gid, age in (("in", 6), ("out", 8)):
            h = add_mail(tmp_path, gid, frm=f"{gid}@x.example", subject=gid, at=NOW - age * DAY)
            add_verdict(tmp_path, h, wanted="decide", disposition="hold")
        assert subjects(build(tmp_path).counted_asks) == ["in"]

    def test_a_mail_counted_once_is_never_counted_again(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="decide", disposition="hold")
        mailreport.append_ledger(tmp_path, {"at": NOW - DAY / 2, "as_of": NOW - DAY / 2,
                                            "status": "delivered", "digests": [],
                                            "counted": [h.digest]})
        assert build(tmp_path).counted_asks == []

    def test_a_mail_read_late_still_reaches_the_next_report(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "late", frm="a@x.example", subject="late", at=NOW - 3 * DAY)
        add_verdict(tmp_path, h, wanted="reply", disposition="hold", recorded_at=NOW - 60)
        mailreport.append_ledger(tmp_path, {"at": NOW - DAY, "as_of": NOW - DAY,
                                            "status": "delivered", "digests": []})
        assert subjects(build(tmp_path).needs) == ["late"]

    def test_items_over_a_sections_cap_are_not_consumed(self, tmp_path):
        fresh_health(tmp_path)
        for n in range(mailreport.NEEDS_SHOWN + 2):
            h = add_mail(tmp_path, f"m{n}", frm=f"s{n}@x.example", subject=f"s{n}",
                         at=NOW - DAY + n)
            add_verdict(tmp_path, h, wanted="pay", disposition="hold")
        report = build(tmp_path)
        assert len(report.needs) == mailreport.NEEDS_SHOWN + 2
        assert len(report.shown_digests()) == mailreport.NEEDS_SHOWN
        assert "(+2 more, not shown — they stay eligible" in mailreport.render(
            report, clean=identity)

    def test_a_mail_in_both_inboxes_is_shown_once_as_the_gmail_copy(self, tmp_path):
        fresh_health(tmp_path)
        tuta = add_mail(tmp_path, "t", frm="a@x.example", subject="twice", at=NOW - DAY,
                        mid="same@x.example", mailbox="tuta")
        add_verdict(tmp_path, tuta, wanted="pay", disposition="hold")
        gm = add_mail(tmp_path, "g", frm="a@x.example", subject="twice", at=NOW - DAY + 5,
                      mid="same@x.example")
        add_verdict(tmp_path, gm, wanted="pay", disposition="hold")
        report = build(tmp_path)
        assert [i.header.mailbox for i in report.needs] == ["gmail"]
        assert report.duplicates == 1


# ---------------------------------------------------------------- open means shown again


def delivered(state, now: float, digests=(), **extra) -> None:
    mailreport.append_ledger(state, {"at": now, "as_of": now, "status": "delivered",
                                     "digests": list(digests), **extra})


class TestOpenMeansShownAgain:
    def test_an_open_line_is_listed_again_the_next_day_with_its_age(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="Pay the roofer now",
                     at=NOW - 4 * DAY)
        add_verdict(tmp_path, h, wanted="pay")
        delivered(tmp_path, NOW - DAY, [h.digest])
        report = build(tmp_path)
        assert subjects(report.needs) == ["Pay the roofer now"] and not report.needs[0].new
        line = next(t for t in mailreport.render(report, clean=identity).splitlines()
                    if t.startswith("- PAY"))
        assert " — open 4 day(s)" in line and " — new" not in line

    def test_a_line_he_resolved_leaves_and_is_counted(self, tmp_path):
        from aish import mailitems

        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - 4 * DAY)
        add_verdict(tmp_path, h, wanted="pay")
        mailitems.append_act(tmp_path, {"at": NOW - DAY, "verb": "resolve",
                                        "anchor": h.digest})
        report = build(tmp_path)
        assert report.needs == [] and report.resolved == 1

    def test_a_line_his_own_reply_settles_moves_to_probably_settled(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - 4 * DAY)
        add_verdict(tmp_path, h, wanted="reply")
        add_mail(tmp_path, "o", frm=OWNER, to="a@x.example", subject="Re: a", at=NOW - DAY,
                 sent=True)
        report = build(tmp_path)
        assert report.needs == [] and [i.header.subject for i in report.settled] == ["a"]

    def test_the_last_morning_inside_the_window_says_so(self, tmp_path):
        fresh_health(tmp_path)
        window = mailreport.FOLLOWUP_WINDOW_DAYS * DAY
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - window + DAY / 2)
        add_verdict(tmp_path, h, wanted="pay")
        report = build(tmp_path)
        assert report.needs[0].leaving
        assert "leaves the brief tomorrow, unanswered" in mailreport.render(report,
                                                                          clean=identity)
        assert build(tmp_path, now=NOW + DAY).needs == []  # and then it is gone
        earlier = build(tmp_path, now=NOW - DAY)
        assert not earlier.needs[0].leaving

    def test_showable_agrees_with_the_lines_the_report_draws(self, tmp_path):
        from aish import mailitems, mailsettle

        fresh_health(tmp_path)
        for n, wanted in enumerate(("pay", "decide", "reply")):
            h = add_mail(tmp_path, f"m{n}", frm=f"s{n}@x.example", subject=f"s{n}",
                         at=NOW - 20 * DAY + n)
            add_verdict(tmp_path, h, wanted=wanted)
        delivered(tmp_path, NOW - DAY)  # none is news any more: state decides
        report = build(tmp_path)
        ctx = mailitems.Context(tmp_path, now=NOW)
        needs = {i.anchor for i in mailsettle.showable(ctx) if i.kind == mailitems.NEEDS}
        assert needs == {i.reading.digest for i in report.needs}
        assert len(needs) == 2


# ---------------------------------------------------------------- folding


class TestFolding:
    def test_lines_from_one_sender_on_no_case_fold_into_one(self, tmp_path):
        fresh_health(tmp_path)
        a = add_mail(tmp_path, "a", frm="shop@x.example", subject="Invoice 1", at=NOW - 2 * DAY)
        add_verdict(tmp_path, a, wanted="pay", deadline="2026-12-01")
        b = add_mail(tmp_path, "b", frm="shop@x.example", subject="Invoice 2", at=NOW - DAY)
        add_verdict(tmp_path, b, wanted="reply")
        report = build(tmp_path)
        (line,) = report.needs
        assert [m.reading.digest for m in line.mails()] == [a.digest, b.digest]
        text = mailreport.render(report, clean=identity)
        row = next(t for t in text.splitlines() if t.startswith("- PAY"))
        # The folded mail's ref, with its what-word and deadline where they differ.
        assert f"2 mails, also ref {b.digest[:12]} (REPLY; no date)" in row
        assert report.counts()["needs_action"] == 1 and report.counts()["needs_mails"] == 2
        assert sorted(report.shown_digests()) == sorted([a.digest, b.digest])

    def test_differing_stored_amounts_are_all_listed(self, tmp_path, monkeypatch):
        from aish import mailitems

        fresh_health(tmp_path)
        a = add_mail(tmp_path, "a", frm="shop@x.example", subject="Invoice 1", at=NOW - 2 * DAY)
        add_verdict(tmp_path, a, wanted="pay")
        b = add_mail(tmp_path, "b", frm="shop@x.example", subject="Invoice 2", at=NOW - DAY)
        add_verdict(tmp_path, b, wanted="pay")
        amounts = {a.digest: frozenset({("PLN", 10000)}), b.digest: frozenset({("PLN", 25000)})}
        monkeypatch.setattr(mailitems.Context, "stored_amounts",
                            lambda self, d: amounts.get(d, frozenset()))
        text = mailreport.render(build(tmp_path), clean=identity)
        assert "100,00 PLN, 250,00 PLN" in text

    def test_a_cautioned_mail_is_never_folded(self, tmp_path):
        fresh_health(tmp_path)
        a = add_mail(tmp_path, "a", frm="shop@x.example", subject="Invoice 1", at=NOW - 2 * DAY)
        add_verdict(tmp_path, a, wanted="pay")
        b = add_mail(tmp_path, "b", frm="shop@x.example", subject="New account", at=NOW - DAY)
        add_verdict(tmp_path, b, wanted="pay", case_flags={"coordinates_changed": ["c1"]})
        report = build(tmp_path)
        assert len(report.needs) == 2 and all(not line.folded for line in report.needs)

    def test_lines_on_one_open_case_fold_and_distinct_cases_never_do(
        self, tmp_path, monkeypatch
    ):
        from aish import mailcases, mailitems

        fresh_health(tmp_path)
        a = add_mail(tmp_path, "a", frm="one@x.example", subject="a", at=NOW - 3 * DAY)
        b = add_mail(tmp_path, "b", frm="two@y.example", subject="b", at=NOW - 2 * DAY)
        c = add_mail(tmp_path, "c", frm="one@x.example", subject="c", at=NOW - DAY)
        d = add_mail(tmp_path, "d", frm="one@x.example", subject="d", at=NOW - DAY / 2)
        for h in (a, b, c, d):
            add_verdict(tmp_path, h, wanted="pay")
        cases = {n: mailcases.Case(id=n, created=(NOW - 30 * DAY, ""), created_by="owner")
                 for n in ("case-1", "case-2")}
        trusted = {a.digest: ["case-1"], b.digest: ["case-1"], c.digest: ["case-2"]}
        monkeypatch.setattr(mailitems.Context, "cases_of",
                            lambda self, digest: [cases[i] for i in trusted.get(digest, ())])
        report = build(tmp_path)
        groups = sorted(sorted(m.header.subject for m in line.mails()) for line in report.needs)
        # a and b share case-1 across two senders; c is on case-2 and d on no
        # case: neither folds with a, though the sender is the same — the
        # sender fold is for lines on no case only.
        assert groups == [["a", "b"], ["c"], ["d"]]

    def test_a_closed_case_does_not_fold(self, tmp_path, monkeypatch):
        from aish import mailcases, mailitems

        fresh_health(tmp_path)
        a = add_mail(tmp_path, "a", frm="one@x.example", subject="a", at=NOW - 3 * DAY)
        b = add_mail(tmp_path, "b", frm="two@y.example", subject="b", at=NOW - 2 * DAY)
        for h in (a, b):
            add_verdict(tmp_path, h, wanted="pay")
        closed = mailcases.Case(id="c", created=(NOW - 30 * DAY, ""), created_by="owner",
                                status=mailcases.CLOSED)
        monkeypatch.setattr(mailitems.Context, "cases_of", lambda self, digest: [closed])
        assert len(build(tmp_path).needs) == 2


# ---------------------------------------------------------------- the footer


class TestTheFooterIsCompressed:
    def test_machinery_lines_print_only_when_non_zero(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "g", frm="news@list.example", subject="Weekly", at=NOW - DAY)
        add_verdict(tmp_path, h, gated=True)
        footer = mailreport.render(build(tmp_path), clean=identity).split("\n---\n", 1)[1]
        assert footer.splitlines()[0] == "Not shown: 1 newsletter-gated. `/mail` lists every " \
                                         "reading."
        assert "Settlement:" not in footer and "one-time code" not in footer

    def test_the_preferences_line_is_printed_even_at_zero(self, tmp_path):
        fresh_health(tmp_path)
        footer = mailreport.render(build(tmp_path), clean=identity).split("\n---\n", 1)[1]
        assert "Using 0 of your preferences — none saved." in footer

    def test_hidden_and_shown_despite_counts_are_printed_when_preferences_exist(
        self, tmp_path
    ):
        from aish import mailprefs

        fresh_health(tmp_path)
        planned = mailprefs.plan(tmp_path, {"action": "add", "kind": "hide",
                                            "sender": "nobody@x.example"})
        assert mailprefs.apply(tmp_path, planned, source={"session": "t"},
                               now=NOW - 2 * DAY).ok
        footer = mailreport.render(build(tmp_path), clean=identity).split("\n---\n", 1)[1]
        assert "— hid 0 item(s); 0 shown despite a preference" in footer


# ---------------------------------------------------------------- the miss counter


class TestTheMissCounter:
    def seed(self, state, *, wanted="decide", gated=False, listed=False, read_after=False,
             in_reply=True):
        fresh_health(state)
        h = add_mail(state, "a", frm="anna@x.example", subject="choose", at=NOW - 3 * DAY)
        add_verdict(state, h, wanted=wanted, gated=gated,
                    recorded_at=NOW - DAY / 2 if read_after else None)
        delivered(state, NOW - DAY, [h.digest] if listed else [])
        add_mail(state, "o", frm=OWNER, to="anna@x.example", subject="Re: choose",
                 at=NOW - DAY / 4, sent=True, in_reply_to="a@mail.example" if in_reply else "")
        return h

    def test_an_answered_mail_no_brief_listed_is_a_miss(self, tmp_path):
        h = self.seed(tmp_path)
        report = build(tmp_path)
        assert report.missed == [h.digest]
        assert ("You answered 1 mail(s) since the last brief that it had not listed."
                in mailreport.render(report, clean=identity))

    def test_matched_by_address_when_there_is_no_in_reply_to(self, tmp_path):
        h = self.seed(tmp_path, in_reply=False)
        assert build(tmp_path).missed == [h.digest]

    def test_a_listed_mail_is_no_miss_and_zero_prints_nothing(self, tmp_path):
        self.seed(tmp_path, listed=True)
        report = build(tmp_path)
        assert report.missed == []
        assert "You answered" not in mailreport.render(report, clean=identity)

    def test_the_denominator_excludes_gated_mail_and_mail_read_after_the_brief(
        self, tmp_path
    ):
        self.seed(tmp_path, gated=True)
        assert build(tmp_path).missed == []
        other = tmp_path / "b"
        self.seed(other, read_after=True)
        assert build(other).missed == []

    def test_mail_outside_the_window_is_not_counted(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="anna@x.example", subject="choose", at=NOW - 70 * DAY)
        add_verdict(tmp_path, h, wanted="decide")
        delivered(tmp_path, NOW - DAY)
        add_mail(tmp_path, "o", frm=OWNER, to="anna@x.example", subject="Re: choose",
                 at=NOW - DAY / 4, sent=True, in_reply_to="a@mail.example")
        assert build(tmp_path).missed == []

    def test_a_listed_your_turn_ask_is_no_miss(self, tmp_path):
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - 3 * DAY,
                     reply_wanted="cannot_tell")
        run(tmp_path, FakePost([ok()]), now=NOW - DAY)
        (entry,) = mailreport.read_ledger(tmp_path)
        assert len(entry["followups"]) == 1
        add_mail(tmp_path, "o2", frm=OWNER, to="bob@client.example", subject="Re: Quote",
                 at=NOW - DAY / 4, sent=True, in_reply_to="r1@mail.example")
        assert build(tmp_path).missed == []

    def test_the_ledger_records_it_on_every_attempt(self, tmp_path):
        self.seed(tmp_path)
        post = FakePost([(403, {}, b"{}")])
        run(tmp_path, post)
        entry = mailreport.read_ledger(tmp_path)[-1]
        assert entry["status"] == "failed" and entry["missed"] == 1
        assert "counted" not in entry  # only a delivered report records what it counted


class TestTheNowOption:
    def test_now_works_only_with_a_dry_run(self, capsys):
        assert mailreport.main(["--now", "2026-11-01T07:30"]) == 2
        assert "--now works only with --dry-run" in capsys.readouterr().err


# ---------------------------------------------------------------- the brief


def brief_of(state, **kw) -> tuple[mailreport.Report, str]:
    report = build(state, **kw)
    return report, mailreport.render_brief(report, clean=identity, account="me@example.org")


def weekday_now(weekday: int) -> float:
    """A `now` near NOW that falls on `weekday` (0 = Monday), same clock time."""
    import datetime

    day = datetime.datetime.fromtimestamp(NOW)
    return NOW + ((weekday - day.weekday()) % 7) * DAY


def known_sender(state, address: str) -> None:
    add_mail(state, f"o-{address}", frm=OWNER, to=address, subject="hello",
             at=NOW - 50 * DAY, sent=True)


class TestTheBrief:
    def test_two_texts_from_one_build(self, tmp_path):
        fresh_health(tmp_path)
        known_sender(tmp_path, "anna@x.example")
        h = add_mail(tmp_path, "abcdef12", frm="Anna <anna@x.example>",
                     subject="Pay the roofer now",
                     at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", deadline="2026-12-01")
        report, brief = brief_of(tmp_path)
        working = mailreport.render(report, clean=identity, account="me@example.org")
        assert f"ref {h.digest[:12]}" in working and "Inbox report —" in working
        line = next(t for t in brief.splitlines() if t.startswith("- "))
        assert line.startswith("- Pay — Anna — “Pay the roofer now” — due ")
        # The only refs and links: the done chip and the Gmail link.
        assert re.sub(r"\(aish-reply://resolve ref [0-9a-f]{12}\)", "", brief).count(
            h.digest[:12]) == 0
        assert re.findall(r"\]\(([^)]*)\)", brief) == [
            f"aish-reply://resolve ref {h.digest[:12]}",
            "https://mail.google.com/mail/u/0/?authuser=me%40example.org#all/abcdef12"]
        for machinery in ("the Reader:", "gmail,", "pref-", "did not weigh", "Probably"):
            assert machinery not in brief
        assert mailreport.visible_length(line) <= mailreport.BRIEF_LINE_TARGET

    def test_a_display_name_only_for_an_authenticated_sender_he_writes_to(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="Your Bank <x@bank.example>", subject="Pay now please",
                     at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", deadline="2026-12-01")
        _report, brief = brief_of(tmp_path)
        assert "Your Bank" not in brief and "x@bank.example" in brief

    def test_the_headline_counts_new_and_still_open_and_health_leads(self, tmp_path):
        fresh_health(tmp_path, now=NOW - 3 * 3600)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", deadline="2026-12-01")
        _report, brief = brief_of(tmp_path)
        first, second = brief.splitlines()[:2]
        assert first.startswith("THE MAIL WATCHER IS NOT HEALTHY")
        assert first.endswith(mailreport.day_words(NOW) + " — 1 new, 0 still open.")
        assert second.startswith("WATCHER:")

    def test_an_empty_day_still_says_so(self, tmp_path):
        fresh_health(tmp_path)
        _report, brief = brief_of(tmp_path)
        assert brief.splitlines()[0] == mailreport.day_words(NOW) + " — 0 new, 0 still open."
        assert "Nothing needs you." in brief
        assert brief.splitlines()[-1] == ("Not listed: nothing; 0 hidden by your preferences, "
                                          "0 shown despite one. /mail shows everything.")

    def test_caution_is_on_the_line_and_the_readers_sentence_is_withheld(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="Faktura", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", suspicion="suspicious",
                    meaning="Pay to PL61109010140000071219812874 today.")
        _report, brief = brief_of(tmp_path)
        line = next(t for t in brief.splitlines() if "Faktura" in t)
        assert line.startswith("- **CAUTION: the Reader judged it suspicious.** Pay")
        assert "PL611" not in brief

    def test_the_readers_sentence_only_when_the_subject_says_little(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="Faktura", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", deadline="2026-12-01",
                    meaning="An invoice for the roof.")
        _report, brief = brief_of(tmp_path)
        assert "An invoice for the roof." in brief

    @pytest.mark.parametrize("days,words", [(0, "(today)"), (3, "(in 3 days)"),
                                            (-11, "(11 days overdue)")])
    def test_date_wording(self, days, words):
        import datetime

        day = (datetime.date.fromtimestamp(NOW) + datetime.timedelta(days=days))
        text = mailreport._date_words(day.isoformat(), NOW)
        assert text == f"{day.day} {day.strftime('%b')} {words}"

    def test_one_stored_amount_is_shown_and_several_are_not(self, tmp_path, monkeypatch):
        from aish import mailitems

        fresh_health(tmp_path)
        one = add_mail(tmp_path, "a", frm="a@x.example", subject="Invoice one", at=NOW - DAY)
        two = add_mail(tmp_path, "b", frm="b@y.example", subject="Invoice two", at=NOW - DAY)
        for h in (one, two):
            add_verdict(tmp_path, h, wanted="pay")
        amounts = {one.digest: frozenset({("PLN", 12050)}),
                   two.digest: frozenset({("PLN", 100), ("PLN", 900)})}
        monkeypatch.setattr(mailitems.Context, "stored_amounts",
                            lambda self, d: amounts.get(d, frozenset()))
        _report, brief = brief_of(tmp_path)
        assert "— 120,50 PLN" in brief
        assert "1,00 PLN" not in brief and "9,00 PLN" not in brief

    def test_several_stored_amounts_alone_earn_no_money_fact(self, tmp_path, monkeypatch):
        """D: an order confirmation's price list is no sum he owes."""
        from aish import mailitems

        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="shop@x.example", subject="Order confirmed",
                     at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="decide")
        monkeypatch.setattr(mailitems.Context, "stored_amounts",
                            lambda self, d: frozenset({("PLN", 100), ("PLN", 900)}))
        report = build(tmp_path)
        assert report.needs == [] and subjects(report.counted_asks) == ["Order confirmed"]

    def test_a_folded_line_says_how_many_mails_and_the_earliest_date(self, tmp_path):
        fresh_health(tmp_path)
        for gid, deadline in (("a", "2026-12-05"), ("b", "2026-12-01")):
            h = add_mail(tmp_path, gid, frm="shop@x.example", subject=f"Invoice {gid}",
                         at=NOW - DAY)
            add_verdict(tmp_path, h, wanted="pay", deadline=deadline)
        _report, brief = brief_of(tmp_path)
        line = next(t for t in brief.splitlines() if t.startswith("- "))
        assert "(2 mails)" in line and "due 1 Dec" in line and "(+1 more dates)" in line

    def test_a_first_contact_ask_with_only_the_readers_word_is_shown_once(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="sales@pitch.example", subject="Quick question",
                     at=NOW - 2 * DAY)
        add_verdict(tmp_path, h, wanted="reply")
        report, brief = brief_of(tmp_path, now=NOW - DAY)
        assert [i.header.subject for i in report.brief_first_contact] == ["Quick question"]
        assert "**New, from senders you have not dealt with**" in brief
        run(tmp_path, FakePost([ok()]), now=NOW - DAY)
        assert mailreport.read_ledger(tmp_path)[-1]["brief"]["first_contact"] == [h.digest]
        report, brief = brief_of(tmp_path)
        assert report.brief_first_contact == [] and "Quick question" not in brief
        assert "1 unanswered from new senders" in brief
        # The working copy still lists it: the chat's model can name it.
        assert "Quick question" in mailreport.render(report, clean=identity)

    def test_a_line_backed_by_a_hard_fact_repeats(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="sales@pitch.example", subject="Quick question",
                     at=NOW - 2 * DAY)
        add_verdict(tmp_path, h, wanted="reply", deadline="2026-12-01")
        run(tmp_path, FakePost([ok()]), now=NOW - DAY)
        report, brief = brief_of(tmp_path)
        assert [i.header.subject for i in report.brief_open] == ["Quick question"]
        assert "**Still open**" in brief and "open 2 days" in brief

    def test_no_weekday_decides_anything_and_an_overdue_bill_stays_listed(self, tmp_path):
        assert not hasattr(mailreport, "BRIEF_LISTS_AGED_ON")
        for weekday in range(7):
            now = weekday_now(weekday)
            state = tmp_path / str(weekday)
            fresh_health(state, now=now)
            h = add_mail(state, "a", frm="a@x.example", subject="Pay this", at=now - 20 * DAY)
            add_verdict(state, h, wanted="pay", deadline="2026-01-01")  # long overdue
            delivered(state, now - 10 * DAY, [h.digest])
            report, brief = brief_of(state, now=now)
            assert [i.header.subject for i in report.brief_open] == ["Pay this"]
            assert "overdue" in brief and "Monday" not in brief

    def test_a_deadline_still_ahead_keeps_an_old_line_listed(self, tmp_path):
        fresh_health(tmp_path)
        now = weekday_now(2)
        fresh_health(tmp_path, now=now)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="Pay this", at=now - 20 * DAY)
        add_verdict(tmp_path, h, wanted="pay", deadline="2099-01-01")
        delivered(tmp_path, now - 10 * DAY, [h.digest])
        assert len(build(tmp_path, now=now).brief_open) == 1

    def test_the_last_morning_is_listed_and_never_grouped(self, tmp_path):
        now = weekday_now(2)
        fresh_health(tmp_path, now=now)
        window = mailreport.FOLLOWUP_WINDOW_DAYS * DAY
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="Pay this",
                     at=now - window + DAY / 2)
        add_verdict(tmp_path, h, wanted="pay", deadline="2026-01-01")
        delivered(tmp_path, now - 10 * DAY, [h.digest])
        report, brief = brief_of(tmp_path, now=now)
        assert len(report.brief_open) == 1 and "leaves the brief tomorrow" in brief

    def test_waiting_on_others_lists_five_to_thirty_days_and_counts_older(self, tmp_path):
        fresh_health(tmp_path)
        add_mail(tmp_path, "o1", frm=OWNER, to="bob@x.example", subject="Recent",
                 at=NOW - 6 * DAY, sent=True)
        add_mail(tmp_path, "o2", frm=OWNER, to="eve@y.example", subject="Old",
                 at=NOW - 40 * DAY, sent=True)
        report, brief = brief_of(tmp_path)
        assert [f.counterparty for f in report.brief_waiting] == ["bob@x.example"]
        assert "**Waiting on others**" in brief and "Recent" in brief and "Old" not in brief
        assert "1 waiting on others over 30 days" in brief

    def test_the_counts_line_always_carries_the_preferences_clause(self, tmp_path):
        from aish import mailprefs

        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay")
        planned = mailprefs.plan(tmp_path, {"action": "add", "kind": "hide",
                                            "sender": "a@x.example"})
        assert mailprefs.apply(tmp_path, planned, source={"session": "t"},
                               now=NOW - 2 * DAY).ok
        _report, brief = brief_of(tmp_path)
        assert brief.splitlines()[-1] == ("Not listed: nothing; 1 hidden by your preferences, "
                                          "0 shown despite one. /mail shows everything.")

    def test_handled_items_are_one_count(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - 4 * DAY)
        add_verdict(tmp_path, h, wanted="reply")
        add_mail(tmp_path, "o", frm=OWNER, to="a@x.example", subject="Re: a", at=NOW - DAY,
                 sent=True)
        _report, brief = brief_of(tmp_path)
        assert "1 item(s) look handled by later mail." in brief
        assert "probably settled" not in brief

    def test_the_brief_is_delivered_beside_the_working_copy(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay")
        post = FakePost([ok()])
        run(tmp_path, post)
        payload = post.calls[0][1]
        assert payload["report"].startswith("Inbox report —")
        assert payload["brief"].startswith(mailreport.day_words(NOW) + " — 1 new")
        entry = mailreport.read_ledger(tmp_path)[-1]
        # A first-contact sender, only the Reader's word: its own section, once.
        assert entry["brief"]["first_contact"] == [h.digest]


def dated(state, gid: str, *, wanted: str, deadline: str = "none", at_days: float = 3,
          now: float = NOW, listed: bool = True, frm: str = "", **verdict) -> mailindex.Header:
    """A line from a sender he writes to (so it repeats), listed before."""
    address = frm or f"{gid}@x.example"
    known_sender(state, address)
    h = add_mail(state, gid, frm=address, subject=f"Subject {gid}", at=now - at_days * DAY)
    add_verdict(state, h, wanted=wanted, deadline=deadline, recorded_at=now - at_days * DAY,
                **verdict)
    if listed:
        delivered(state, now - DAY / 2, [h.digest])
    return h


def iso(days: int, now: float = NOW) -> str:
    import datetime

    return (datetime.date.fromtimestamp(now) + datetime.timedelta(days=days)).isoformat()


class TestDatesByKind:
    """The owner's decision of 2026-11-12: "Why Mondays only? Importance
    differs. Deadline is contextual." A passed date means what the line asks."""

    def test_a_decide_is_listed_the_day_after_its_date_then_counted(self, tmp_path):
        fresh_health(tmp_path)
        yesterday = dated(tmp_path, "y", wanted="decide", deadline=iso(-1))
        dated(tmp_path, "o", wanted="decide", deadline=iso(-2))
        report, brief = brief_of(tmp_path)
        assert [i.reading.digest for i in report.brief_open] == [yesterday.digest]
        line = next(t for t in brief.splitlines() if "Subject y" in t)
        assert "(date passed)" in line and "overdue" not in line
        assert "Subject o" not in brief and "1 whose date passed" in brief
        assert report.brief_sections()["date_passed_counted"] == [
            i.reading.digest for i in report.brief_date_passed]

    def test_a_decide_first_seen_after_its_date_is_listed_once_then_counted(self, tmp_path):
        fresh_health(tmp_path)
        late = dated(tmp_path, "late", wanted="decide", deadline=iso(-2), listed=False)
        report, brief = brief_of(tmp_path)
        assert [i.reading.digest for i in report.brief_new] == [late.digest]
        assert "(date passed)" in brief and report.brief_date_passed == []
        delivered(tmp_path, NOW, [late.digest])
        report, brief = brief_of(tmp_path, now=NOW + DAY)
        assert [i.reading.digest for i in report.brief_date_passed] == [late.digest]
        assert "1 whose date passed" in brief and "Subject late" not in brief

    def test_a_date_passed_decide_on_its_last_morning_is_announced(self, tmp_path):
        fresh_health(tmp_path)
        window = mailreport.FOLLOWUP_WINDOW_DAYS
        last = add_mail(tmp_path, "last", frm="last@x.example", subject="Subject last",
                        at=NOW - (window - 0.5) * DAY)
        add_verdict(tmp_path, last, wanted="decide", deadline=iso(-30),
                    recorded_at=NOW - (window - 0.5) * DAY)
        delivered(tmp_path, NOW - DAY / 2, [last.digest])
        report, brief = brief_of(tmp_path)
        assert [i.reading.digest for i in report.brief_open] == [last.digest]
        line = next(t for t in brief.splitlines() if "Subject last" in t)
        assert mailreport.LEAVING_WORDS in line and "(date passed)" in line

    def test_a_cautioned_or_preferred_or_restored_decide_is_never_reduced(self, tmp_path):
        from aish import mailitems, mailprefs

        fresh_health(tmp_path)
        dated(tmp_path, "c", wanted="decide", deadline=iso(-5), suspicion="suspicious")
        pref = dated(tmp_path, "p", wanted="decide", deadline=iso(-5), frm="p@pref.example")
        planned = mailprefs.plan(tmp_path, {"action": "add", "kind": "show_first",
                                            "domain": "pref.example"})
        assert mailprefs.apply(tmp_path, planned, source={"session": "t"},
                               now=NOW - 9 * DAY).ok
        back = dated(tmp_path, "r", wanted="decide", deadline=iso(-5))
        mailitems.append_act(tmp_path, {"at": NOW - DAY, "verb": "restore",
                                        "anchor": back.digest, "items": [], "by": "owner"})
        report, brief = brief_of(tmp_path)
        assert report.brief_date_passed == []
        assert {i.header.subject for i in report.brief_open} == {
            "Subject c", "Subject p", "Subject r"}
        assert pref.digest in {i.reading.digest for i in report.brief_open}

    def test_still_open_is_ordered_by_importance_not_age(self, tmp_path):
        fresh_health(tmp_path)
        dated(tmp_path, "decide", wanted="decide", deadline="none", at_days=2,
              suspicion="none", meaning="x", kind="notice")
        dated(tmp_path, "reply", wanted="reply", at_days=3)
        dated(tmp_path, "pay", wanted="pay", at_days=4)
        dated(tmp_path, "soon", wanted="decide", deadline=iso(9), at_days=5)
        dated(tmp_path, "sooner", wanted="pay", deadline=iso(3), at_days=6)
        dated(tmp_path, "late", wanted="pay", deadline=iso(-4), at_days=7)
        dated(tmp_path, "later", wanted="pay", deadline=iso(-9), at_days=8)
        dated(tmp_path, "warn", wanted="pay", at_days=9, sender_case="mismatch")
        report, _brief = brief_of(tmp_path)
        order = [i.header.subject.split()[-1] for i in report.brief_open]
        # The undated decide earns its line by its correspondent fact.
        assert order == ["warn", "later", "late", "sooner", "soon", "pay", "reply", "decide"]

    def test_old_undated_bills_and_replies_fold_into_one_line_per_kind(self, tmp_path):
        fresh_health(tmp_path)
        dated(tmp_path, "b1", wanted="pay", at_days=20)
        dated(tmp_path, "b2", wanted="pay", at_days=30)
        dated(tmp_path, "r1", wanted="reply", at_days=16)
        dated(tmp_path, "new", wanted="pay", at_days=5)
        report, brief = brief_of(tmp_path)
        assert [i.header.subject for i in report.brief_open] == ["Subject new"]
        import datetime

        oldest = datetime.date.fromtimestamp(NOW - 30 * DAY)
        assert (f"- 2 bills open longer than 14 days (oldest from {oldest.day} "
                f"{oldest.strftime('%b')}) — ask here to list them") in brief
        assert "- 1 reply open longer than 14 days" in brief
        assert "Subject b1" not in brief and "Subject r1" not in brief
        assert brief.splitlines()[0].endswith("0 new, 4 still open.")
        assert len(report.brief_sections()["grouped"]) == 3
        # The working copy still lists every line.
        working = mailreport.render(report, clean=identity)
        assert "Subject b1" in working and "Subject r1" in working

    def test_dated_cautioned_and_preferred_lines_are_never_grouped(self, tmp_path):
        from aish import mailprefs

        fresh_health(tmp_path)
        dated(tmp_path, "dated", wanted="pay", deadline=iso(-20), at_days=25)
        dated(tmp_path, "warn", wanted="pay", at_days=25, suspicion="suspicious")
        dated(tmp_path, "pref", wanted="pay", at_days=25, frm="bills@pref.example")
        planned = mailprefs.plan(tmp_path, {"action": "add", "kind": "show_first",
                                            "domain": "pref.example"})
        assert mailprefs.apply(tmp_path, planned, source={"session": "t"},
                               now=NOW - 40 * DAY).ok
        report, brief = brief_of(tmp_path)
        assert report.brief_grouped == {} and "open longer than" not in brief
        assert {i.header.subject for i in report.brief_open} == {
            "Subject dated", "Subject warn", "Subject pref"}


class TestTheBriefReviewFixes:
    """The final review of slice 1: trust offers are offered only when HE saw
    them, a broken brief costs nothing, and nothing escapes the counts line."""

    def offers(self, monkeypatch, n: int) -> list:
        made = [mailreport.TrustOffer(f"case-{i:012x}", f"Case {i}", f"desk{i}@shop.example",
                                      ("sales@shop.example",), "") for i in range(n)]
        monkeypatch.setattr(mailreport, "trust_offers",
                            lambda store, index, offered: (
                                [o for o in made if o.key not in offered], 0))
        return made

    def test_an_offer_counts_as_offered_only_when_the_brief_drew_it(self, tmp_path,
                                                                     monkeypatch):
        fresh_health(tmp_path)
        made = self.offers(monkeypatch, mailreport.TRUST_OFFERS_SHOWN + 1)
        post = FakePost([ok()])
        run(tmp_path, post, now=NOW - DAY)
        brief = post.calls[0][1]["brief"]
        chips = re.findall(r"\[trust\]\((aish-reply://[^)]*)\)", brief)
        assert len(chips) == mailreport.TRUST_OFFERS_SHOWN
        # The server keeps exactly these shapes.
        assert server.presented_report(brief).count("](aish-reply://trust sender") == len(chips)
        assert all(mailreport.visible_length(t) <= mailreport.BRIEF_LINE_TARGET
                   for t in brief.splitlines() if t.startswith("- "))
        entry = mailreport.read_ledger(tmp_path)[-1]
        assert entry["trust_offers"] == sorted(o.key for o in made[:-1])
        # Next morning: the one not drawn is offered again; the drawn ones are not.
        report = build(tmp_path)
        assert [o.key for o in report.trust_offers] == [made[-1].key]

    def test_a_broken_brief_delivers_the_working_copy_and_says_so(self, tmp_path,
                                                                    monkeypatch):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="Secret subject 4411",
                     at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay")

        def broken(*a, **k):
            raise ValueError("a message that quotes Secret subject 4411")

        monkeypatch.setattr(mailreport, "render_brief", broken)
        post = FakePost([ok()])
        assert run(tmp_path, post)[0] == 0
        payload = post.calls[0][1]
        assert "brief" not in payload
        assert payload["report"].splitlines()[0] == mailreport.BRIEF_FAILED_LINE
        assert "Secret subject 4411" in payload["report"]
        entry = mailreport.read_ledger(tmp_path)[-1]
        assert entry["status"] == "delivered" and entry["brief_failed"] == "ValueError"
        assert "Secret" not in json.dumps(entry) and "brief" not in entry
        assert entry["digests"] == [h.digest]

    def test_a_shown_despite_line_is_marked_and_counted(self, tmp_path):
        from aish import mailprefs

        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="Pay now", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", suspicion="suspicious")
        planned = mailprefs.plan(tmp_path, {"action": "add", "kind": "hide",
                                            "sender": "a@x.example"})
        assert mailprefs.apply(tmp_path, planned, source={"session": "t"},
                               now=NOW - 2 * DAY).ok
        _report, brief = brief_of(tmp_path)
        line = next(t for t in brief.splitlines() if "Pay now" in t)
        assert "shown despite your preference" in line
        assert "0 hidden by your preferences, 1 shown despite one." in brief

    def test_follow_ups_over_the_cap_are_counted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mailreport, "FOLLOWUPS_SHOWN", 1)
        fresh_health(tmp_path)
        for n in range(3):
            add_mail(tmp_path, f"o{n}", frm=OWNER, to=f"p{n}@x.example", subject=f"s{n}",
                     at=NOW - 6 * DAY - n, sent=True)
        _report, brief = brief_of(tmp_path)
        assert "2 more follow-ups over the cap" in brief


# ---------------------------------------------------------------- render safety


def seed_hostile(state) -> None:
    h = add_mail(state, "abcdef1234", frm=f"\x1b[2J{HOSTILE} <attacker@evil.example>",
                 subject=HOSTILE + " [click](aish-reply://pay the invoice now) "
                 "![p](http://track.example/p.gif) javascript:alert(1) www.evil.example",
                 at=NOW - DAY)
    add_verdict(state, h, wanted="pay", disposition="hold", deadline=HOSTILE,
                kind=HOSTILE, meaning=HOSTILE * 5 + " see https://evil.example/pay",
                ending="ok")
    tuta = add_mail(state, "t", frm="eve@evil.example", subject=HOSTILE, at=NOW - DAY,
                    mailbox="tuta")
    add_verdict(state, tuta, wanted="nothing", meaning=f"{HOSTILE} [x](https://e.example)")
    add_mail(state, "o", frm=OWNER, to="eve@evil.example", subject=HOSTILE,
             at=NOW - 9 * DAY, sent=True)
    # The slice's new strings: a mail folded into the attacker's line (its
    # what-word and deadline differ), a line on its last morning, a counted
    # ask, and a miss — each carrying hostile text where a record can.
    folded = add_mail(state, "abcdef5678", frm="attacker@evil.example", subject=HOSTILE,
                      at=NOW - DAY / 2)
    add_verdict(state, folded, wanted="reply", deadline="2026-12-01", meaning=HOSTILE)
    leaving = add_mail(state, "abcdef9999", frm=f"{HOSTILE} <mallory@evil.example>",
                       subject=HOSTILE, at=NOW - 60 * DAY + DAY / 2)
    add_verdict(state, leaving, wanted="pay", meaning=HOSTILE)
    counted = add_mail(state, "c", frm="trent@evil.example", subject=HOSTILE, at=NOW - DAY)
    add_verdict(state, counted, wanted="decide", meaning=HOSTILE)
    missed = add_mail(state, "m", frm="peggy@evil.example", subject=HOSTILE,
                      at=NOW - 4 * DAY, mid="missed@evil.example")
    add_verdict(state, missed, wanted="decide", meaning=HOSTILE, recorded_at=NOW - 4 * DAY)
    mailreport.append_ledger(state, {"at": NOW - 3 * DAY, "as_of": NOW - 3 * DAY,
                                     "status": "delivered", "digests": []})
    add_mail(state, "o2", frm=OWNER, to="peggy@evil.example", subject=HOSTILE,
             at=NOW - DAY, sent=True, in_reply_to="missed@evil.example")


UNSAFE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\u2028\u2029]"
)


class TestRenderSafety:
    def test_hostile_bytes_reach_the_report_stripped_and_marked(self, tmp_path):
        fresh_health(tmp_path)
        seed_hostile(tmp_path)
        text = mailreport.render(build(tmp_path, clean=cli._mail_cell),
                                 clean=cli._mail_cell, account="me@example.org")
        assert not UNSAFE.search(text.replace("\n", ""))
        assert "\N{REPLACEMENT CHARACTER}" in text  # removed is marked, never hidden

    def test_no_url_but_the_code_built_gmail_link_survives(self, tmp_path):
        fresh_health(tmp_path)
        seed_hostile(tmp_path)
        text = mailreport.render(build(tmp_path, clean=cli._mail_cell),
                                 clean=cli._mail_cell, account="me@example.org")
        for gone in ("aish-reply:", "javascript:", "http://", "www.evil", "evil.example/pay",
                     "track.example", "e.example)", "x.example"):
            assert gone not in text, gone
        assert "](" not in text.replace("](https://mail.google.com/mail/u/0/", "")
        urls = re.findall(r"https?://\S+", text)
        assert urls and all(
            u.startswith("https://mail.google.com/mail/u/0/?authuser=me%40example.org#all/")
            for u in urls)

    def test_every_mail_and_model_string_passes_through_the_sanitiser(self, tmp_path):
        fresh_health(tmp_path)
        seed_hostile(tmp_path)
        seen: list[str] = []

        def recording(text: str) -> str:
            seen.append(text)
            return "«ok»"

        text = mailreport.render(build(tmp_path, clean=recording), clean=recording)
        for fragment in ("evil", "\x9b", "‮", "attacker", "Pay", "now"):
            assert fragment not in text, fragment
        assert any(HOSTILE in s for s in seen)

    def test_the_new_strings_are_present_and_a_stored_amount_passes_the_sanitiser(
        self, tmp_path, monkeypatch
    ):
        from aish import mailitems

        fresh_health(tmp_path)
        seed_hostile(tmp_path)
        def amounts(self, digest):
            header = self.index.get(digest)
            return (frozenset({(HOSTILE, 100)})
                    if header is not None and header.from_addr == "attacker@evil.example"
                    else frozenset())

        monkeypatch.setattr(mailitems.Context, "stored_amounts", amounts)
        seen: list[str] = []

        def recording(text: str) -> str:
            seen.append(text)
            return "«ok»"

        text = mailreport.render(build(tmp_path, clean=recording), clean=recording)
        assert "mails, also ref " in text and "leaves the brief tomorrow" in text
        assert "2 more mail(s) ask something" in text and "You answered 1 mail(s)" in text
        assert any(HOSTILE in s for s in seen) and "evil" not in text and "\x9b" not in text
        clean_text = mailreport.render(build(tmp_path, clean=cli._mail_cell),
                                       clean=cli._mail_cell)
        assert not UNSAFE.search(clean_text.replace("\n", ""))

    def test_every_brief_string_passes_the_sanitiser_and_no_foreign_link_survives(
        self, tmp_path
    ):
        fresh_health(tmp_path)
        seed_hostile(tmp_path)
        seen: list[str] = []

        def recording(text: str) -> str:
            seen.append(text)
            return "«ok»"

        report = build(tmp_path, clean=recording)
        brief = mailreport.render_brief(report, clean=recording)
        assert any(HOSTILE in s for s in seen)
        for fragment in ("evil", "\x9b", "‮", "attacker", "Pay\x9b"):
            assert fragment not in brief, fragment
        clean = mailreport.render_brief(build(tmp_path, clean=cli._mail_cell),
                                        clean=cli._mail_cell, account="me@example.org")
        assert not UNSAFE.search(clean.replace("\n", ""))
        for target in re.findall(r"\]\(([^)]*)\)", clean):
            assert (re.fullmatch(r"aish-reply://resolve ref [0-9a-f]{12}", target)
                    or target.startswith("https://mail.google.com/mail/u/0/")), target
        assert server.presented_report(clean).count("](") == clean.count("](")

    def test_gmail_links_are_built_only_from_a_hex_gmail_id(self):
        base = mailindex.from_gmail(gmail("abcdef12", frm="a@x.example", at=T0),
                                    frozenset({OWNER})).header
        assert mailreport.gmail_link(base, "").endswith("#all/abcdef12")
        for bad in ("../../x", "abc", "ABCDEF12", "abcdef12?x=1"):
            assert mailreport.gmail_link(dataclasses.replace(base, provider_id=bad), "") == ""
        assert mailreport.gmail_link(dataclasses.replace(base, mailbox="tuta"), "") == ""


# ---------------------------------------------------------------- health, empty day


class TestTheReportSaysWhatItRestsOn:
    def test_a_stale_watcher_leads_the_report(self, tmp_path):
        fresh_health(tmp_path, now=NOW - 3 * 3600)
        text = mailreport.render(build(tmp_path), clean=identity)
        first, second = text.splitlines()[:2]
        assert "THE MAIL WATCHER IS NOT HEALTHY, this report may be incomplete" in first
        assert second.startswith("WATCHER: watcher: last pass")
        assert "no pass recorded since" in second

    def test_no_watcher_at_all_leads_the_report(self, tmp_path):
        text = mailreport.render(build(tmp_path), clean=identity)
        assert "NOT HEALTHY" in text.splitlines()[0]

    def test_an_empty_day_still_says_so_in_every_section(self, tmp_path):
        fresh_health(tmp_path)
        add_mail(tmp_path, "o", frm=OWNER, to="bob@client.example", subject="hi",
                 at=NOW - DAY, sent=True)
        text = mailreport.render(build(tmp_path), clean=identity)
        assert text.splitlines()[0].endswith(": nothing needs you.")
        assert "Nothing open asks you to pay, reply or decide." in text
        assert "Your turn (0):\n- none" in text and "(0):\n- none" in text
        assert "Not shown: nothing else." in text


# ---------------------------------------------------------------- run, delivery, ledger


class FakePost:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, body, timeout):
        self.calls.append((url, json.loads(body)))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def ok(session="s.jsonl", **extra):
    return 200, {}, json.dumps({"session": session, **extra}).encode()


def run(state, post, *, dry_run=False, token="tok", sleeps=None, now=NOW, since=None):
    printed: list[str] = []
    code = mailreport.run(
        state, clean=identity, now=now, dry_run=dry_run, since=since, account="",
        base="http://aish.test", token=token, post=post,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
        out=printed.append,
    )
    return code, printed


def tree(path):
    return sorted((str(p.relative_to(path)), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in path.rglob("*") if p.is_file())


class TestRun:
    def test_dry_run_prints_and_sends_and_writes_nothing(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", disposition="hold")
        before = tree(tmp_path)
        post = FakePost([])
        code, printed = run(tmp_path, post, dry_run=True)
        assert code == 0 and post.calls == []
        # The owner's brief by default; the working copy on request.
        assert printed[0].startswith(mailreport.day_words(NOW) + " — ")
        printed = []
        assert mailreport.run(tmp_path, clean=identity, now=NOW, dry_run=True, since=None,
                              account="", base="", token="", post=post,
                              out=printed.append, working_copy=True) == 0
        assert printed[0].startswith("Inbox report — ")
        assert tree(tmp_path) == before

    def test_delivery_is_one_schedule_trigger_with_the_report_outside_the_prompt(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="Secret subject 9431",
                     at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", disposition="hold", meaning="Meaning 5512.")
        post = FakePost([ok()])
        code, _ = run(tmp_path, post)
        assert code == 0
        url, payload = post.calls[0]
        assert url == "http://aish.test/trigger?token=tok"
        assert payload["origin"] == "schedule"
        assert payload["meta"]["kind"] == "report"
        assert payload["meta"]["dedup_key"].startswith("mailreport:")
        assert payload["title"].startswith("Inbox report — ")
        assert "Secret subject 9431" in payload["report"] and "Meaning 5512." in payload["report"]
        assert "Secret subject 9431" not in payload["prompt"]
        assert "Meaning 5512" not in payload["prompt"] and "a@x.example" not in payload["prompt"]
        assert payload["report"].splitlines()[0] in payload["prompt"]  # the headline only

    def test_the_ledger_records_counts_session_and_the_shown_mail(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", disposition="hold")
        run(tmp_path, FakePost([ok("chat-1.jsonl")]))
        (entry,) = mailreport.read_ledger(tmp_path)
        assert entry["status"] == "delivered" and entry["session"] == "chat-1.jsonl"
        assert entry["counts"] == {"needs_action": 1, "needs_mails": 1, "your_turn": 0,
                                   "their_turn": 0, "counted": 0, "probably_settled": 0,
                                   "missed": 0}
        assert entry["digests"] == [h.digest]
        assert entry["counted"] == [] and entry["followups"] == [] and entry["missed"] == 0

    def test_an_empty_day_still_delivers(self, tmp_path):
        fresh_health(tmp_path)
        post = FakePost([ok()])
        assert run(tmp_path, post)[0] == 0
        assert post.calls[0][1]["report"].splitlines()[0].endswith("nothing needs you.")

    def test_a_refusal_is_retried_and_a_lasting_one_is_a_recorded_failure(self, tmp_path):
        fresh_health(tmp_path)
        sleeps: list[float] = []
        refused = (429, {"Retry-After": "45"}, b"{}")
        post = FakePost([refused, ok()])
        assert run(tmp_path, post, sleeps=sleeps)[0] == 0
        assert len(post.calls) == 2 and sleeps == [45.0]

        other = tmp_path / "b"
        fresh_health(other)
        post = FakePost([urllib.error.URLError(ConnectionRefusedError(61, "refused"))]
                        * (len(mailreport.RETRY_WAITS_S) + 1))
        code, _ = run(other, post)
        assert code == 1
        (entry,) = mailreport.read_ledger(other)
        assert entry["status"] == "failed" and "refused" in entry["error"]
        assert "digests" not in entry

    def test_a_request_that_may_have_arrived_is_unknown_never_failed(self, tmp_path):
        fresh_health(tmp_path)
        timeout = urllib.error.URLError(TimeoutError("timed out"))
        post = FakePost([timeout] * (len(mailreport.RETRY_WAITS_S) + 1))
        assert run(tmp_path, post)[0] == 1
        assert mailreport.read_ledger(tmp_path)[-1]["status"] == "unknown"

    def test_a_retry_after_a_lost_answer_finds_the_chat_the_first_one_made(self, tmp_path):
        fresh_health(tmp_path)
        post = FakePost([urllib.error.URLError(TimeoutError("timed out")),
                         ok("first.jsonl", deduped=True)])
        assert run(tmp_path, post)[0] == 0
        entry = mailreport.read_ledger(tmp_path)[-1]
        assert entry["status"] == "deduped" and entry["session"] == "first.jsonl"

    def test_a_non_retryable_status_fails_at_once(self, tmp_path):
        fresh_health(tmp_path)
        post = FakePost([(403, {}, b"{}")])
        assert run(tmp_path, post)[0] == 1 and len(post.calls) == 1

    def test_no_token_sends_nothing_and_says_so(self, tmp_path):
        fresh_health(tmp_path)
        post = FakePost([])
        assert run(tmp_path, post, token="")[0] == 1 and post.calls == []
        assert "no /trigger token" in mailreport.read_ledger(tmp_path)[-1]["error"]

    def test_a_day_already_delivered_is_not_delivered_again(self, tmp_path):
        fresh_health(tmp_path)
        run(tmp_path, FakePost([ok()]))
        post = FakePost([])
        code, printed = run(tmp_path, post, now=NOW + 60)
        assert code == 0 and post.calls == [] and "already delivered" in printed[0]

    def test_the_report_fits_under_the_servers_cap(self):
        assert mailreport.REPORT_MAX_CHARS < server.REPORT_NOTE_MAX_CHARS


class TestHealthInMail:
    def test_none_recorded_is_one_plain_line(self, tmp_path):
        assert mailreport.health_lines(tmp_path, NOW, identity) == [
            ("ok", "daily report: none recorded")]

    def test_a_failed_attempt_is_a_warning_in_mail(self, tmp_path):
        mailreport.append_ledger(tmp_path, {"at": NOW - DAY, "status": "delivered",
                                            "session": "s.jsonl"})
        mailreport.append_ledger(tmp_path, {"at": NOW - 60, "status": "failed",
                                            "error": "POST /trigger answered HTTP 503"})
        fresh_health(tmp_path)
        lines = mailview.lines(tmp_path, clean=cli._mail_cell, now=NOW)
        warn = [t for s, t in lines if s == "warn" and t.startswith("daily report")]
        assert warn and "FAILED — POST /trigger answered HTTP 503" in warn[0]

    def test_no_report_for_over_a_day_is_a_warning(self, tmp_path):
        mailreport.append_ledger(tmp_path, {"at": NOW - 27 * 3600, "status": "delivered"})
        (severity, text), = mailreport.health_lines(tmp_path, NOW, identity)
        assert severity == "warn" and text.startswith("daily report: none delivered since")
