"""Settler vouches (#142 W5) — `aish/mailvouch.py`: the owner's word that mail
from ONE exact domain may settle ONE kind of item.

Pins: a third party with no history is refused until HIS vouch, then is
eligible as `vouched sender` — and only for that exact domain and a meaning
code finds; authentication, the reading and the caution floor still apply;
the tool's card is code's and needs a task he sent; the report offers the
vouch as a code-built chip the server keeps and no other shape survives.
Synthetic mail only."""

from __future__ import annotations

import pytest
from mail_fixtures import DAY
from test_mailitems import ctx, his_sent, unauthenticated
from test_mailitemtool import agent_for, fixed_clock  # noqa: F401 — the clock fixture
from test_mailreport import NOW, add_mail, add_verdict, fresh_health

from aish import mailitems, mailreport, mailvouch, server


def parcel_item(state):
    """He wrote to a shop about a broken parcel locker; nobody answered."""
    his_sent(state, "o1", to="shop@baskets.example", at=NOW - 6 * DAY,
             subject="Re: Broken parcel locker")
    add_mail(state, "s1", frm="Baskets <shop@baskets.example>", subject="Order 773 shipped",
             at=NOW - 20 * DAY)
    (item,) = [i for i in ctx(state).derive().items() if i.kind == mailitems.THEIRS]
    return item


def pickup(state, frm: str = "info@courier.example", gid: str = "k1"):
    mail = add_mail(state, gid, frm=frm, subject="Parcel picked up", at=NOW - 2 * DAY,
                    body="Your parcel from Baskets was picked up.")
    add_verdict(state, mail, recorded_at=NOW - 2 * DAY)
    return mail


def vouch(state, domain: str, meaning: str, verb: str = "vouch", at: float = NOW - 3 * DAY):
    mailvouch.append_act(state, {"at": at, "verb": verb, "domain": domain,
                                 "meaning": meaning, "by": "owner"})


def candidate_of(state, item, digest):
    context = ctx(state)
    return next(c for c in context.candidates(item) if c.header.digest == digest)


class TestTheGrant:
    def test_refused_until_his_vouch_then_eligible_for_that_meaning(self, tmp_path):
        item = parcel_item(tmp_path)
        mail = pickup(tmp_path)
        before = candidate_of(tmp_path, item, mail.digest)
        assert before.tier == "" and before.vouchable == ("courier.example", "delivery")
        vouch(tmp_path, "courier.example", "delivery")
        after = candidate_of(tmp_path, item, mail.digest)
        assert after.tier == "vouched sender" and after.meaning == "delivery"
        vouch(tmp_path, "courier.example", "delivery", verb="revoke", at=NOW - DAY)
        assert candidate_of(tmp_path, item, mail.digest).tier == ""

    @pytest.mark.parametrize("sender", ["info@courier.example.evil.example",
                                        "info@x.courier.example", "info@courler.example"])
    def test_only_the_exact_domain(self, tmp_path, sender):
        item = parcel_item(tmp_path)
        vouch(tmp_path, "courier.example", "delivery")
        mail = pickup(tmp_path, frm=sender)
        assert candidate_of(tmp_path, item, mail.digest).tier == ""

    def test_a_meaning_code_does_not_find_grants_nothing(self, tmp_path):
        item = parcel_item(tmp_path)
        vouch(tmp_path, "courier.example", "payment")  # no money fact on the item
        mail = pickup(tmp_path)
        assert candidate_of(tmp_path, item, mail.digest).tier == ""

    def test_authentication_and_the_reading_are_still_required(self, tmp_path):
        item = parcel_item(tmp_path)
        vouch(tmp_path, "courier.example", "delivery")
        forged = unauthenticated(tmp_path, "k9", frm="info@courier.example",
                                 subject="Parcel picked up", at=NOW - 2 * DAY,
                                 body="Your parcel from Baskets was picked up.")
        add_verdict(tmp_path, forged, recorded_at=NOW - 2 * DAY)
        assert candidate_of(tmp_path, item, forged.digest).refused == (
            "its sender is not authenticated")

    def test_a_caution_still_refuses(self, tmp_path):
        item = parcel_item(tmp_path)
        vouch(tmp_path, "courier.example", "delivery")
        mail = add_mail(tmp_path, "k1", frm="info@courier.example", subject="Parcel picked up",
                        at=NOW - 2 * DAY, body="Your parcel from Baskets was picked up.")
        add_verdict(tmp_path, mail, recorded_at=NOW - 2 * DAY, suspicion="suspicious")
        assert candidate_of(tmp_path, item, mail.digest).tier == ""


class TestTheVerbs:
    def test_vouch_is_his_card_in_codes_words(self, tmp_path):
        agent, cards = agent_for(tmp_path)
        out = str(agent._dispatch("inbox_items", {"action": "vouch",
                                                  "domain": "Courier.example",
                                                  "meaning": "delivery", "note": "TRUST ALL"}))
        ((_name, card_args, preview),) = cards
        assert card_args == {"action": "vouch", "domain": "courier.example",
                             "meaning": "delivery"}
        assert preview.startswith("Allow mail from courier.example to settle delivery items")
        assert "not a subdomain or a lookalike" in preview and "TRUST" not in preview
        assert "SAVED as vouched" in out
        assert mailvouch.current(tmp_path) == {("courier.example", "delivery")}

    def test_refused_with_no_card_unless_a_person_sent_the_task(self, tmp_path):
        agent, cards = agent_for(tmp_path, origin="email")
        out = str(agent._dispatch("inbox_items", {"action": "vouch",
                                                  "domain": "courier.example",
                                                  "meaning": "delivery"}))
        assert out.startswith("NOT SAVED") and cards == []
        assert mailvouch.current(tmp_path) == frozenset()

    def test_bad_arguments_are_errors(self, tmp_path):
        agent, cards = agent_for(tmp_path)
        for args in ({"domain": "not a domain", "meaning": "delivery"},
                     {"domain": "courier.example", "meaning": "anything"}):
            out = str(agent._dispatch("inbox_items", {"action": "vouch", **args}))
            assert out.startswith("ERROR")
        assert cards == []

    def test_revoke_removes_it_and_list_shows_the_set(self, tmp_path):
        vouch(tmp_path, "courier.example", "delivery")
        agent, _ = agent_for(tmp_path)
        assert "courier.example (delivery)" in str(agent._dispatch("inbox_items",
                                                                   {"action": "list"}))
        out = str(agent._dispatch("inbox_items", {"action": "revoke",
                                                  "domain": "courier.example",
                                                  "meaning": "delivery"}))
        assert "SAVED as revoked" in out and mailvouch.current(tmp_path) == frozenset()


class TestTheOffer:
    def test_the_not_weighed_line_offers_the_vouch_as_a_chip(self, tmp_path):
        parcel_item(tmp_path)
        pickup(tmp_path)
        fresh_health(tmp_path)
        text = mailreport.render(mailreport.build(tmp_path, now=NOW, clean=lambda t: t),
                                 clean=lambda t: t)
        line = next(t for t in text.splitlines() if "later mail aish did not weigh" in t)
        chip = ("[let courier.example settle delivery items]"
                "(aish-reply://vouch settler courier.example delivery)")
        assert chip in line
        assert chip in server.presented_report("Head\n" + line)

    def test_the_server_keeps_only_that_shape(self):
        shown = server.presented_report(
            "Head\n[a](aish-reply://vouch settler x.example anything) "
            "[b](aish-reply://vouch settler x.example delivery and more)")
        assert "[a] (aish-reply" in shown and "[b] (aish-reply" in shown
