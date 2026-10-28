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
    list_id: str = "", mailbox: str = "gmail", body: str = "body",
) -> mailindex.Header:
    parsed = mailindex.from_gmail(
        gmail(gid, frm=frm, to=to, subject=subject, body=body, at=at,
              message_id=mid or f"{gid}@mail.example", in_reply_to=in_reply_to,
              references=references, labels=("SENT",) if sent else ("INBOX",),
              list_id=list_id),
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
    keeper: bool = False, model: str = "gemini:test",
) -> None:
    record = {
        "kind": "mail_verdict", "mail": header.digest, "provider_id": header.provider_id,
        "at": header.at, "at_source": header.at_source, "model": model,
        "ending": "gated" if gated else ending,
        "gate": {"newsletter": {"gated": gated, "by": ["List-Id"] if gated else []}},
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
    def test_pay_reply_and_decide_are_in_and_wait_and_nothing_are_not(self, tmp_path):
        fresh_health(tmp_path)
        for n, wanted in enumerate(("pay", "reply", "decide", "wait", "nothing")):
            h = add_mail(tmp_path, f"m{n}", frm=f"s{n}@shop.example", subject=wanted,
                         at=NOW - DAY + n)
            add_verdict(tmp_path, h, wanted=wanted, disposition="hold")
        report = build(tmp_path)
        assert sorted(subjects(report.needs)) == ["decide", "pay", "reply"]
        assert report.interest == []  # wanted nothing, but held — not "surface"

    def test_order_is_dated_deadlines_soonest_then_pay_reply_decide(self, tmp_path):
        fresh_health(tmp_path)
        rows = [("decide-undated", "decide", "none"), ("pay-undated", "pay", "none"),
                ("reply-undated", "reply", "cannot_tell"), ("late", "decide", "2026-12-20"),
                ("soon", "reply", "2026-11-02")]
        for n, (subject, wanted, deadline) in enumerate(rows):
            h = add_mail(tmp_path, f"m{n}", frm=f"s{n}@shop.example", subject=subject,
                         at=NOW - DAY + n)
            add_verdict(tmp_path, h, wanted=wanted, deadline=deadline, disposition="hold")
        assert subjects(build(tmp_path).needs) == [
            "soon", "late", "pay-undated", "reply-undated", "decide-undated"]

    def test_each_item_names_date_sender_subject_meaning_wanted_deadline_and_inbox(
        self, tmp_path
    ):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "abc123def0", frm="Faktury <billing@shop.example>",
                     subject="Faktura 2026/41", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", deadline="2026-11-05", disposition="hold",
                    meaning="An invoice for the October order.")
        text = mailreport.render(build(tmp_path), clean=identity, account="me@example.org")
        line = next(t for t in text.splitlines() if t.startswith("- PAY"))
        assert "PAY by 2026-11-05" in line
        assert "Faktury <billing@shop.example>" in line and "“Faktura 2026/41”" in line
        assert "the Reader: An invoice for the October order." in line
        assert "gmail, " in line and mailreport._when(h.at) in line
        assert ("[open in Gmail](https://mail.google.com/mail/u/0/?authuser=me%40example.org"
                "#all/abc123def0)") in line


class TestMayInterestYou:
    def test_effective_surface_with_nothing_wanted_is_in_and_filed_mail_is_not(self, tmp_path):
        fresh_health(tmp_path)
        a = add_mail(tmp_path, "a", frm="a@news.example", subject="surfaced", at=NOW - DAY)
        add_verdict(tmp_path, a, wanted="nothing", disposition="surface")
        b = add_mail(tmp_path, "b", frm="b@shop.example", subject="archived", at=NOW - DAY)
        add_verdict(tmp_path, b, wanted="nothing", disposition="archive")
        c = add_mail(tmp_path, "c", frm="c@shop.example", subject="keeper", at=NOW - DAY)
        add_verdict(tmp_path, c, wanted="nothing", disposition="archive", keeper=True)
        assert sorted(subjects(build(tmp_path).interest)) == ["keeper", "surfaced"]

    def test_the_section_says_interest_matching_is_not_built(self, tmp_path):
        fresh_health(tmp_path)
        text = mailreport.render(build(tmp_path), clean=identity)
        head = text.split("## May interest you", 1)[1]
        assert "Not matched to your interests — that is not built yet" in head


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
        assert report.needs == [] and report.interest == []
        assert report.excluded == {"gated": 1, "otp": 3}
        text = mailreport.render(report, clean=identity)
        assert "1 newsletter-gated, 3 one-time code(s) or sign-in link(s)" in text

    def test_a_discount_code_is_not_a_one_time_code(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "d", frm="deals@shop.example", subject="Your discount code inside",
                     at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="nothing", kind="promotion code")
        assert subjects(build(tmp_path).interest) == ["Your discount code inside"]

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
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - 3 * DAY)
        report = build(tmp_path, since=NOW - DAY)  # the reply itself is not new
        assert [f.counterparty for f in report.yours] == ["bob@client.example"]
        assert report.theirs == []
        text = mailreport.render(report, clean=identity)
        assert "3 day(s) waiting on you — bob@client.example sent a message after your " \
               "“Quote for the roof” (the Reader: reply)" in text

    @pytest.mark.parametrize("wanted", ["decide", "pay", "cannot_tell"])
    def test_any_ask_or_an_undecided_reading_is_your_turn(self, tmp_path, wanted):
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - 3 * DAY,
                     reply_wanted=wanted)
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
                     reply_mailbox="tuta")
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
    def test_the_first_report_covers_seven_days(self, tmp_path):
        fresh_health(tmp_path)
        for gid, age in (("in", 6), ("out", 8)):
            h = add_mail(tmp_path, gid, frm=f"{gid}@x.example", subject=gid, at=NOW - age * DAY)
            add_verdict(tmp_path, h, wanted="pay", disposition="hold")
        assert subjects(build(tmp_path).needs) == ["in"]

    def test_a_mail_shown_once_is_never_shown_again(self, tmp_path):
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", disposition="hold")
        mailreport.append_ledger(tmp_path, {"at": NOW - DAY / 2, "as_of": NOW - DAY / 2,
                                            "status": "delivered", "digests": [h.digest]})
        assert build(tmp_path).needs == []

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
            add_verdict(tmp_path, h, wanted="decide", disposition="hold")
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
        assert "Nothing new asks you to pay, reply or decide." in text
        assert "Your turn (0):\n- none" in text and "(0):\n- none" in text
        assert "Nothing new the assistant would keep in front of you." in text


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
        assert entry["counts"] == {"needs_action": 1, "your_turn": 0, "their_turn": 0,
                                   "may_interest": 0}
        assert entry["digests"] == [h.digest]

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
