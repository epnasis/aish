"""Open items and their settlement state (#142 W5, the conversation-state and
settlement redesign) — `aish/mailitems.py`.

Pins: his own copy recognised by evidence and minting nothing; the ONE
derivation; aish's first-read clock (never a sender-written date); the order
owner > judge > rule with revival; his closed case under the read-clock law;
the deterministic rule by addressing and his own In-Reply-To, never
membership; the tier rule, including the stranger who joins a conversation by
References; "later on both clocks"; near-duplicates. Synthetic mail only —
every sender here is invented."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os

from mail_fixtures import DAY, OWNER
from test_mailreport import NOW, add_mail, add_verdict

from aish import mailcases, mailgate, mailindex, mailitems, mailreport

TUTA_AUTH = ("status=authenticated; status-source=tuta-server; "
             "encryption-auth=tutacrypt_authentication_succeeded")


def ctx(state, now: float = NOW) -> mailitems.Context:
    return mailitems.Context(state, now=now)


def touch(state, header: mailindex.Header, at: float) -> None:
    """When aish wrote the mail's index entry — its first-read clock when the
    mail has no reading."""
    os.utime(mailindex.header_path(state, header.digest), (at, at))


def tuta_copy(state, original: mailindex.Header, *, gid: str = "copy", skew: float = 3.0,
              auth: str = TUTA_AUTH, cc: tuple[str, ...] | None = None,
              subject: str | None = None) -> mailindex.Header:
    """His sent mail as the Tuta bridge delivers it to his other inbox: the
    bridge's own Message-ID, no reply headers, a re-rendered Date, a body that
    differs — the shape observed on his pair of 2026-11-01."""
    when = original.at + skew
    header = dataclasses.replace(
        original,
        digest=hashlib.sha256(f"tuta:{gid}".encode()).hexdigest(),
        message_id=f"{gid}@tutabridge.local", in_reply_to=(), references=(),
        direction="in", mailbox="tuta", at=when, at_source="date_header",
        date_header=mailindex.email.utils.formatdate(when),
        cc=original.cc if cc is None else cc,
        subject=original.subject if subject is None else subject,
        gateway_auth=auth, gateway_auth_observed=mailindex.FIRST_AND_ONLY,
        auth_results=(), provider_id="9", body_digest="")
    mailindex.MailIndex(state).add(header, "re-rendered text of his own mail")
    return header


def his_sent(state, gid: str, *, to: str, at: float, subject: str, cc: str = "",
             in_reply_to: str = "", frm: str = OWNER) -> mailindex.Header:
    header = add_mail(state, gid, frm=frm, to=to + (f", {cc}" if cc else ""), at=at,
                      subject=subject, sent=True, mid=f"{gid}@owner.example",
                      in_reply_to=in_reply_to)
    return header


# ---------------------------------------------------------------- his own copy


class TestHisOwnCopy:
    def test_the_bridge_copy_of_his_sent_mail_is_recognised_without_its_message_id(
        self, tmp_path
    ):
        sent = his_sent(tmp_path, "s1", to="agent@broker.example", cc="me@tuta.example",
                        at=NOW - DAY, subject="Re: Offer")
        copy = tuta_copy(tmp_path, sent)
        assert copy.message_id != sent.message_id  # the bridge's own id
        assert mailitems.own_copies(mailindex.MailIndex(tmp_path)) == {copy.digest: sent.digest}

    def test_each_part_of_the_evidence_is_required(self, tmp_path):
        sent = his_sent(tmp_path, "s1", to="agent@broker.example", cc="me@tuta.example",
                        at=NOW - DAY, subject="Re: Offer")
        tuta_copy(tmp_path, sent, gid="late", skew=mailitems.OWN_COPY_SKEW_S + 60)
        tuta_copy(tmp_path, sent, gid="forged",
                  auth="status=hard_fail; status-source=tuta-server")
        tuta_copy(tmp_path, sent, gid="other-cc", cc=("someone@else.example",))
        tuta_copy(tmp_path, sent, gid="other-subject", subject="Re: Offer (2)")
        assert mailitems.own_copies(mailindex.MailIndex(tmp_path)) == {}

    def test_mail_he_sends_only_to_himself_is_not_a_copy(self, tmp_path):
        """Mail to only his own addresses is a mail TO him, read like any other."""
        self_note = his_sent(tmp_path, "s2", to=OWNER, at=NOW - DAY, subject="Note to self")
        tuta_copy(tmp_path, self_note, gid="self-copy")
        assert mailitems.own_copies(mailindex.MailIndex(tmp_path)) == {}

    def test_a_copy_is_never_an_item_and_never_observed_into_a_case(self, tmp_path):
        sent = his_sent(tmp_path, "s1", to="agent@broker.example", cc="me@tuta.example",
                        at=NOW - DAY, subject="Re: Order 77123")
        copy = tuta_copy(tmp_path, sent)
        add_verdict(tmp_path, copy, wanted="reply", disposition="surface")
        assert ctx(tmp_path).derive().needs == []
        # Ingest in one pass: the copy first, its original second.
        state = tmp_path / "fresh"
        index, store = mailindex.MailIndex(state), mailcases.CaseStore(state)
        body = "Order 77123 — please confirm."
        parsed = [mailindex.Parsed(copy, body, ()), mailindex.Parsed(sent, body, ())]
        from aish import mailwatch

        observed = mailwatch.ingest(index, store, parsed)
        assert copy.digest not in observed and sent.digest in observed
        assert all(copy.position not in c.mails for c in store.cases.values())


# ---------------------------------------------------------------- the derivation


class TestTheOneDerivation:
    def test_needs_yours_and_theirs_with_their_anchors(self, tmp_path):
        ask = add_mail(tmp_path, "n1", frm="club@swim.example", subject="Pay", at=NOW - DAY)
        add_verdict(tmp_path, ask, wanted="pay")
        his = his_sent(tmp_path, "o1", to="bob@client.example", at=NOW - 9 * DAY,
                       subject="Quote")
        first = add_mail(tmp_path, "r1", frm="bob@client.example", subject="Re: Quote",
                         at=NOW - 8 * DAY, in_reply_to="o1@owner.example")
        add_verdict(tmp_path, first, wanted="cannot_tell")
        second = add_mail(tmp_path, "r2", frm="bob@client.example", subject="Re: Quote",
                          at=NOW - 7 * DAY, in_reply_to="o1@owner.example")
        add_verdict(tmp_path, second, wanted="cannot_tell")
        quiet = his_sent(tmp_path, "o2", to="carol@shop.example", at=NOW - 10 * DAY,
                         subject="Return")
        derived = ctx(tmp_path).derive()
        assert [i.open_item.anchor for i in derived.needs] == [ask.digest]
        (yours,) = derived.yours
        assert yours.open_item.anchor == first.digest  # the FIRST asking mail
        assert yours.open_item.rests_on == (first.digest, second.digest)
        (theirs,) = derived.theirs
        assert theirs.open_item.anchor == quiet.digest and his.digest != quiet.digest

    def test_the_report_and_the_watcher_derive_the_same_items(self, tmp_path):
        from aish import mailsettle

        ask = add_mail(tmp_path, "n1", frm="club@swim.example", subject="Pay", at=NOW - DAY)
        add_verdict(tmp_path, ask, wanted="pay")
        his_sent(tmp_path, "o2", to="carol@shop.example", at=NOW - 10 * DAY, subject="Return")
        report = mailreport.build(tmp_path, now=NOW, clean=lambda t: t)
        shown = {r.open_item.key for r in (*report.needs, *report.yours, *report.theirs)}
        assert shown == {i.key for i in mailsettle.showable(ctx(tmp_path))}


# ---------------------------------------------------------------- the clock


class TestTheFirstReadClock:
    def test_a_mail_with_no_reading_is_read_when_aish_indexed_it_not_when_it_says(
        self, tmp_path
    ):
        old = add_mail(tmp_path, "b1", frm="x@y.example", subject="?", at=NOW - 40 * DAY)
        touch(tmp_path, old, NOW - 2 * DAY)
        assert ctx(tmp_path).first_read(old.digest) == NOW - 2 * DAY

    def test_a_reading_clock_wins(self, tmp_path):
        mail = add_mail(tmp_path, "a1", frm="x@y.example", subject="?", at=NOW - 3 * DAY)
        add_verdict(tmp_path, mail, recorded_at=NOW - DAY)
        assert ctx(tmp_path).first_read(mail.digest) == NOW - DAY

    def test_with_no_clock_at_all_it_is_never_covered_nor_evidence(self):
        index = mailindex.MailIndex(None)
        parsed = mailindex.from_rfc822(
            "From: a@b.example\nTo: o@x.example\nSubject: s\nDate: Mon, 1 Jan 2026 10:00:00 "
            "+0000\nMessage-ID: <z@b.example>\n\nbody", provider_id="1", in_sent_folder=False,
            mailbox="tuta")
        assert parsed is not None
        index.add(parsed.header, parsed.body)
        context = mailitems.Context(None, now=NOW, index=index, readings={})
        assert context.first_read(parsed.header.digest) == float("inf")


# ---------------------------------------------------------------- the state


def act(state, verb: str, anchor: str, at: float) -> None:
    mailitems.append_act(state, {"at": at, "verb": verb, "anchor": anchor, "by": "owner"})


class TestHisWord:
    def test_resolve_settles_and_later_mail_in_the_conversation_from_anyone_revives(
        self, tmp_path
    ):
        ask = add_mail(tmp_path, "n1", frm="club@swim.example", subject="Pay",
                       at=NOW - 3 * DAY, mid="n1@swim.example")
        add_verdict(tmp_path, ask, wanted="pay", recorded_at=NOW - 3 * DAY)
        act(tmp_path, mailitems.RESOLVE, ask.digest, NOW - 2 * DAY)
        (item,) = ctx(tmp_path).derive().items()
        state = ctx(tmp_path).state_of(item)
        assert state.by_owner and state.source == mailitems.OWNER
        joiner = add_mail(tmp_path, "j1", frm="anyone@else.example", subject="Re: Pay",
                          at=NOW - DAY, in_reply_to="n1@swim.example")
        add_verdict(tmp_path, joiner, recorded_at=NOW - DAY)
        (item,) = [i for i in ctx(tmp_path).derive().items() if i.kind == mailitems.NEEDS]
        state = ctx(tmp_path).state_of(item)
        assert state.state == mailitems.OPEN and state.revived

    def test_restore_pins_open_against_evidence_read_before_it(self, tmp_path):
        ask = add_mail(tmp_path, "n1", frm="agent@broker.example", subject="Offer",
                       at=NOW - 3 * DAY)
        add_verdict(tmp_path, ask, wanted="decide", recorded_at=NOW - 3 * DAY)
        his_sent(tmp_path, "o1", to="agent@broker.example", at=NOW - 2 * DAY,
                 subject="Re: Offer")
        (item,) = ctx(tmp_path).derive().items()
        assert ctx(tmp_path).state_of(item).source == mailitems.RULE
        act(tmp_path, mailitems.RESTORE, ask.digest, NOW - DAY)
        assert ctx(tmp_path).state_of(item).state == mailitems.OPEN
        his_sent(tmp_path, "o2", to="agent@broker.example", at=NOW - DAY / 2,
                 subject="Re: Offer")
        assert ctx(tmp_path).state_of(item).source == mailitems.RULE  # new evidence


class TestHisClosedCase:
    def build(self, tmp_path, *, read_at: float):
        his = his_sent(tmp_path, "o1", to="shop@watches.example", at=NOW - 9 * DAY,
                       subject="Order 265104")
        answer = add_mail(tmp_path, "r1", frm="shop@watches.example", subject="Re: Order 265104",
                          at=NOW - 8 * DAY, in_reply_to="o1@owner.example",
                          body="About order 265104.")
        add_verdict(tmp_path, answer, wanted="reply", recorded_at=read_at)
        store = mailcases.CaseStore(tmp_path)
        index = mailindex.MailIndex(tmp_path)
        for header in (his, answer):
            store.observe(header, index.body(header.digest) or "", True, index)
        (case,) = store.cases.values()
        store.act(mailcases.CLOSE, {"case": case.id}, by="owner", now=NOW - 5 * DAY)
        return answer

    def test_a_case_he_closed_resolves_the_items_on_it(self, tmp_path):
        self.build(tmp_path, read_at=NOW - 8 * DAY)
        (row,) = ctx(tmp_path).derive().yours
        state = ctx(tmp_path).state_of(row.open_item)
        assert state.by_owner and state.evidence.cases

    def test_mail_read_after_his_close_is_not_covered_by_it(self, tmp_path):
        self.build(tmp_path, read_at=NOW - 4 * DAY)
        (row,) = ctx(tmp_path).derive().yours
        assert ctx(tmp_path).state_of(row.open_item).state == mailitems.OPEN


class TestTheRule:
    def test_his_reply_to_the_counterparty_after_the_mail_settles_it_probably(self, tmp_path):
        offer = add_mail(tmp_path, "n1", frm="agent@broker.example", subject="Offer",
                         at=NOW - 3 * DAY, mid="offer@broker.example")
        add_verdict(tmp_path, offer, wanted="decide", recorded_at=NOW - 3 * DAY)
        reply = his_sent(tmp_path, "o1", to="agent@broker.example", at=NOW - 2 * DAY,
                         subject="Re: Offer", in_reply_to="offer@broker.example")
        (item,) = ctx(tmp_path).derive().items()
        state = ctx(tmp_path).state_of(item)
        assert state.probably and state.source == mailitems.RULE
        assert state.evidence.digest == reply.digest

    def test_his_reply_named_by_his_own_in_reply_to_counts_without_addressing(self, tmp_path):
        offer = add_mail(tmp_path, "n1", frm="agent@broker.example", subject="Offer",
                         at=NOW - 3 * DAY, mid="offer@broker.example")
        add_verdict(tmp_path, offer, wanted="decide", recorded_at=NOW - 3 * DAY)
        his_sent(tmp_path, "o1", to="assistant@broker.example", at=NOW - 2 * DAY,
                 subject="Re: Offer", in_reply_to="offer@broker.example")
        (item,) = ctx(tmp_path).derive().items()
        assert ctx(tmp_path).state_of(item).source == mailitems.RULE

    def test_membership_by_references_is_not_an_answer(self, tmp_path):
        offer = add_mail(tmp_path, "n1", frm="agent@broker.example", subject="Offer",
                         at=NOW - 3 * DAY, mid="offer@broker.example")
        add_verdict(tmp_path, offer, wanted="decide", recorded_at=NOW - 3 * DAY)
        add_mail(tmp_path, "o1", frm=OWNER, to="someone@else.example", at=NOW - 2 * DAY,
                 subject="Unrelated", sent=True, references="<offer@broker.example>")
        (item,) = [i for i in ctx(tmp_path).derive().items() if i.kind == mailitems.NEEDS]
        assert ctx(tmp_path).state_of(item).state == mailitems.OPEN

    def test_his_mail_before_the_item_settles_nothing(self, tmp_path):
        his_sent(tmp_path, "o1", to="agent@broker.example", at=NOW - 4 * DAY, subject="Hello")
        offer = add_mail(tmp_path, "n1", frm="agent@broker.example", subject="Offer",
                         at=NOW - 3 * DAY)
        add_verdict(tmp_path, offer, wanted="decide", recorded_at=NOW - 3 * DAY)
        (item,) = [i for i in ctx(tmp_path).derive().items() if i.kind == mailitems.NEEDS]
        assert ctx(tmp_path).state_of(item).state == mailitems.OPEN


# ---------------------------------------------------------------- the tier rule


def pay_item(state):
    ask = add_mail(state, "n1", frm="club@swim.example", subject="Pay for March",
                   at=NOW - 3 * DAY, mid="n1@swim.example", body="Pay 640 PLN for March.")
    add_verdict(state, ask, wanted="pay", recorded_at=NOW - 3 * DAY)
    (item,) = [i for i in ctx(state).derive().items() if i.kind == mailitems.NEEDS]
    return item


class TestTheTierRule:
    def test_the_same_counterparty_is_tier_b(self, tmp_path):
        item = pay_item(tmp_path)
        thanks = add_mail(tmp_path, "k1", frm="club@swim.example", subject="Thank you",
                          at=NOW - 2 * DAY, body="We booked your 640 PLN payment.")
        add_verdict(tmp_path, thanks, recorded_at=NOW - 2 * DAY)
        (candidate,) = ctx(tmp_path).candidates(item)
        assert candidate.tier == "same counterparty" and "counterparty" in candidate.ties

    def test_a_stranger_joining_the_conversation_is_not_tier_b(self, tmp_path):
        """The review's blocking finding: a hostile mail naming the thread's
        Message-ID joins the conversation; its own membership must not be its
        credential."""
        item = pay_item(tmp_path)
        joiner = add_mail(tmp_path, "k1", frm="billing@attacker.example", subject="Re: Pay",
                          at=NOW - 2 * DAY, references="<n1@swim.example>",
                          body="Payment received, all settled.")
        add_verdict(tmp_path, joiner, recorded_at=NOW - 2 * DAY)
        (candidate,) = ctx(tmp_path).candidates(ctx(tmp_path).derive().items()[0])
        assert "conversation" in candidate.ties
        assert candidate.tier == "" and candidate.refused
        assert item.anchor == ctx(tmp_path).derive().items()[0].anchor

    def test_a_third_party_tied_only_by_name_is_refused_with_code_words(self, tmp_path):
        pay_item(tmp_path)
        courier = add_mail(tmp_path, "k1", frm="info@courier.example", subject="Picked up",
                           at=NOW - 2 * DAY, body="Your parcel from Swim was picked up.")
        add_verdict(tmp_path, courier, recorded_at=NOW - 2 * DAY)
        context = ctx(tmp_path)
        (candidate,) = context.candidates(context.derive().items()[0])
        assert candidate.ties == ("entity",)
        assert candidate.tier == ""
        assert "a third party tied to it only by entity" in candidate.refused

    def test_a_caution_on_any_reading_refuses_every_tier(self, tmp_path):
        pay_item(tmp_path)
        thanks = add_mail(tmp_path, "k1", frm="club@swim.example", subject="Thank you",
                          at=NOW - 2 * DAY)
        add_verdict(tmp_path, thanks, recorded_at=NOW - 2 * DAY, suspicion="suspicious")
        context = ctx(tmp_path)
        (candidate,) = context.candidates(context.derive().items()[0])
        assert candidate.tier == "" and "suspicious" in candidate.refused

    def test_a_backfilled_old_mail_is_not_later_than_the_item(self, tmp_path):
        """First read today, but its own time is before the item's: a mail
        backfilled now is not evidence about a newer item."""
        pay_item(tmp_path)
        old = add_mail(tmp_path, "k1", frm="club@swim.example", subject="Thanks (February)",
                       at=NOW - 30 * DAY)
        touch(tmp_path, old, NOW - DAY)
        context = ctx(tmp_path)
        assert context.candidates(context.derive().items()[0]) == []


class TestTheThirdPartyTier:
    def test_history_and_a_stored_amount_admit_a_third_party(self, tmp_path):
        """Authenticated, on a case the owner transacted on beside the item,
        with a STORED equal amount, and the domain on two of his cases first
        seen 30+ days before: tier (c)."""
        store = mailcases.CaseStore(tmp_path)
        index = mailindex.MailIndex(tmp_path)
        for n in (1, 2):  # history: two owner cases the courier was seen on
            his = his_sent(tmp_path, f"h{n}", to=f"shop{n}@store.example",
                           at=NOW - 90 * DAY + n, subject=f"Order AB{n}0001")
            seen = add_mail(tmp_path, f"c{n}", frm="info@courier.example",
                            subject=f"Parcel for order AB{n}0001", at=NOW - 80 * DAY + n)
            index = mailindex.MailIndex(tmp_path)
            for header in (his, seen):
                store.observe(header, index.body(header.digest) or "", True, index)
        his = his_sent(tmp_path, "h3", to="shop@store.example", at=NOW - 5 * DAY,
                       subject="Order XY77001")
        ask = add_mail(tmp_path, "n1", frm="shop@store.example", subject="Order XY77001: pay",
                       at=NOW - 4 * DAY, body="Pay 120,00 zł for order XY77001.")
        add_verdict(tmp_path, ask, wanted="pay", recorded_at=NOW - 4 * DAY)
        paid = add_mail(tmp_path, "k1", frm="info@courier.example",
                        subject="Cash on delivery for order XY77001",
                        at=NOW - 2 * DAY, body="We collected 120,00 zł for order XY77001.")
        add_verdict(tmp_path, paid, recorded_at=NOW - 2 * DAY)
        index = mailindex.MailIndex(tmp_path)
        for header in (his, ask, paid):
            store.observe(header, index.body(header.digest) or "", True, index)
        context = ctx(tmp_path)
        item = next(i for i in context.derive().items() if i.anchor == ask.digest)
        (candidate,) = [c for c in context.candidates(item) if c.header.digest == paid.digest]
        assert {"case", "amount"} <= set(candidate.ties)
        assert candidate.tier == "third party"
        assert context.history("courier.example", paid) == 2


# ---------------------------------------------------------------- judgements


def judged(state, item: mailitems.OpenItem, candidate: mailindex.Header, answer: str,
           at: float, digest: str = "charter-digest") -> None:
    mailitems.write_judgement(state, item.anchor, {
        "item": item.key, "candidate": candidate.digest, "charter_digest": digest,
        "at": at, "ending": "ok", "answer": answer, "reason": "MODEL PROSE", "tier": ""})


class TestAJudgementIsReverified:
    def test_settled_by_settles_until_a_caution_appears_on_the_candidate(self, tmp_path):
        item = pay_item(tmp_path)
        thanks = add_mail(tmp_path, "k1", frm="club@swim.example", subject="Thank you",
                          at=NOW - 2 * DAY)
        add_verdict(tmp_path, thanks, recorded_at=NOW - 2 * DAY)
        judged(tmp_path, item, thanks, "settled_by", NOW - 2 * DAY + 60)
        state = ctx(tmp_path).state_of(item)
        assert state.probably and state.source == mailitems.JUDGE
        assert state.evidence.digest == thanks.digest and state.evidence.tier
        add_verdict(tmp_path, thanks, recorded_at=NOW - 2 * DAY, model="local:other",
                    sender_case="mismatch")
        assert ctx(tmp_path).state_of(item).state == mailitems.OPEN

    def test_a_later_ask_in_the_conversation_revives_a_judged_item(self, tmp_path):
        item = pay_item(tmp_path)
        thanks = add_mail(tmp_path, "k1", frm="club@swim.example", subject="Thank you",
                          at=NOW - 2 * DAY)
        add_verdict(tmp_path, thanks, recorded_at=NOW - 2 * DAY)
        judged(tmp_path, item, thanks, "settled_by", NOW - 2 * DAY + 60)
        again = add_mail(tmp_path, "r2", frm="club@swim.example", subject="Re: Pay for March",
                         at=NOW - DAY, in_reply_to="n1@swim.example")
        add_verdict(tmp_path, again, wanted="pay", recorded_at=NOW - DAY)
        item = next(i for i in ctx(tmp_path).derive().items() if i.anchor == item.anchor)
        state = ctx(tmp_path).state_of(item)
        assert state.state == mailitems.OPEN and state.revived

    def test_one_unreadable_judgement_file_costs_that_file(self, tmp_path):
        item = pay_item(tmp_path)
        bad = mailitems.settlements_dir(tmp_path) / "zz" / ("z" * 64 + ".json")
        bad.parent.mkdir(parents=True)
        bad.write_text("{not json")
        assert ctx(tmp_path).judgements_unreadable == 1
        assert ctx(tmp_path).state_of(item).state == mailitems.OPEN


# ---------------------------------------------------------------- near-duplicates


class TestNearDuplicates:
    def test_one_sender_one_subject_read_together_render_once_with_every_ref(self, tmp_path):
        a = add_mail(tmp_path, "d1", frm="admin@school.example", subject="New message",
                     at=NOW - DAY, body="same")
        b = add_mail(tmp_path, "d2", frm="admin@school.example", subject="New message",
                     at=NOW - DAY, body="same")
        for h in (a, b):
            add_verdict(tmp_path, h, recorded_at=NOW - DAY + 60)
        report = mailreport.build(tmp_path, now=NOW, clean=lambda t: t)
        (item,) = report.interest
        assert len(item.twins) == 1 and not item.twins_differ
        assert set(report.shown_digests()) == {a.digest, b.digest}
        text = mailreport.render(report, clean=lambda t: t)
        assert f"(×2 — also ref {item.twins[0][:12]})" in text

    def test_twins_whose_texts_differ_say_so(self, tmp_path):
        for n, body in enumerate(("Invoice 1 for 100 zł", "Invoice 2 for 900 zł")):
            h = add_mail(tmp_path, f"f{n}", frm="billing@shop.example", subject="Faktura",
                         at=NOW - DAY, body=body)
            add_verdict(tmp_path, h, wanted="pay", recorded_at=NOW - DAY + 60)
        report = mailreport.build(tmp_path, now=NOW, clean=lambda t: t)
        (item,) = report.needs
        assert item.twins_differ
        assert "their texts differ" in mailreport.render(report, clean=lambda t: t)


class TestHealthOfTheStore:
    def test_an_unreadable_act_line_costs_that_line(self, tmp_path):
        path = mailitems.acts_path(tmp_path)
        path.parent.mkdir(parents=True)
        good = {"at": 1.0, "verb": "resolve", "anchor": "a" * 64, "by": "owner"}
        path.write_text("{oops\n" + json.dumps(good) + "\n")
        assert mailitems.read_acts(tmp_path) == [good]

    def test_gate_auth_is_what_it_reads(self, tmp_path):
        sent = his_sent(tmp_path, "s1", to="agent@broker.example", at=NOW - DAY, subject="x")
        copy = tuta_copy(tmp_path, sent)
        assert mailgate.authenticate_header(copy).authenticated


def unauthenticated(state, gid: str, *, frm: str, subject: str, at: float,
                    body: str = "") -> mailindex.Header:
    """A mail whose From is only a claim: Gmail's header says DMARC failed."""
    from mail_fixtures import gmail

    parsed = mailindex.from_gmail(gmail(gid, frm=frm, subject=subject, body=body, at=at,
                                        auth="fail"), frozenset({OWNER}))
    assert parsed is not None
    mailindex.MailIndex(state).add(parsed.header, parsed.body)
    return parsed.header


class TestEvidenceMustBeAuthenticatedAndRead:
    """The delivery review's blocking finding: a forged From of the
    counterparty, never read, passed tier (b) and its floor never ran."""

    def test_a_forged_counterparty_from_settles_nothing(self, tmp_path):
        pay_item(tmp_path)
        forged = unauthenticated(tmp_path, "k1", frm="club@swim.example",
                                 subject="Payment received", at=NOW - 2 * DAY,
                                 body="We booked your 640 PLN payment.")
        touch(tmp_path, forged, NOW - 2 * DAY)
        context = ctx(tmp_path)
        (candidate,) = context.candidates(context.derive().items()[0])
        assert candidate.tier == "" and candidate.refused == "its sender is not authenticated"

    def test_an_unread_authenticated_candidate_waits_for_the_reader(self, tmp_path):
        pay_item(tmp_path)
        thanks = add_mail(tmp_path, "k1", frm="club@swim.example", subject="Thank you",
                          at=NOW - 2 * DAY, body="We booked your 640 PLN payment.")
        touch(tmp_path, thanks, NOW - 2 * DAY)
        context = ctx(tmp_path)
        item = context.derive().items()[0]
        (candidate,) = context.candidates(item)
        assert candidate.tier == "same counterparty" and candidate.unread
        assert mailitems.unread_candidates(context, [item], 5) == [thanks.digest]
        # A stored settled_by on an unread candidate does not settle the item.
        mailitems.write_judgement(tmp_path, item.anchor, {
            "item": item.key, "candidate": thanks.digest, "charter_digest": "d", "at": NOW,
            "ending": "ok", "answer": "settled_by"})
        assert ctx(tmp_path).state_of(item).state == mailitems.OPEN
