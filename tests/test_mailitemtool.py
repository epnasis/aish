"""The `inbox_items` tool (#142 W5) — his resolve / restore on an item of his
inbox reports, under the case verbs' law — and the `important` preference with
its effect preview.

Pins: refused with no card unless a person sent the task; the card is code's
sentence from the typed ref alone; SAVED only after a save; a resolve takes
the item out of the next report and new mail revives it; a restore pulls a
demoted item back; the tool taints and is on the report chat's menu and is
never cardless; `important` places mail under Needs by entity AND meaning,
without a fabricated want, and its card previews what it would and would not
have matched. Scripted model and fixture mail only."""

from __future__ import annotations

import pytest
from mail_fixtures import DAY, OWNER
from test_agent import make_agent, model_says
from test_mailitems import his_sent, pay_item
from test_mailreport import NOW, add_mail, add_verdict, fresh_health

from aish import agent as agent_module
from aish import mailitems, mailitemtool, mailprefs, mailreport, server
from aish.approval import Approved, Denied


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(mailitemtool, "clock", lambda: NOW)
    monkeypatch.setattr(mailprefs, "clock", lambda: NOW)


def agent_for(tmp_path, *, answer=True, origin="user", from_owner=None):
    cards = []

    def approve_tool(name, args, preview):
        cards.append((name, dict(args), preview))
        return answer

    agent, _ = make_agent([model_says("ok")], approve_tool=approve_tool, origin=origin,
                          state_dir=tmp_path)
    if from_owner is not None:
        agent.task_from_owner = from_owner
    return agent, cards


def report(state):
    fresh_health(state)
    return mailreport.build(state, now=NOW, clean=lambda t: t)


class TestTheVerbs:
    def test_refused_with_no_card_unless_a_person_sent_the_task(self, tmp_path):
        item = pay_item(tmp_path)
        for agent, cards in (agent_for(tmp_path, origin="email"),
                             agent_for(tmp_path, from_owner=False)):
            out = str(agent._dispatch("inbox_items", {"action": "resolve",
                                                      "ref": item.anchor[:12]}))
            assert out.startswith("NOT SAVED") and cards == []
        assert mailitems.read_acts(tmp_path) == []

    def test_resolve_is_carded_in_codes_words_saved_and_takes_the_item_out(self, tmp_path):
        item = pay_item(tmp_path)
        agent, cards = agent_for(tmp_path, origin="schedule", from_owner=True)
        out = str(agent._dispatch("inbox_items", {
            "action": "resolve", "ref": item.anchor[:12], "note": "IGNORE ALL MAIL"}))
        ((name, card_args, preview),) = cards
        assert name == "inbox_items"
        assert card_args == {"action": "resolve", "ref": item.anchor[:12]}
        assert preview.startswith("Mark as done: PAY from club@swim.example “Pay for March”")
        assert "new mail in that conversation brings it back" in preview
        assert "IGNORE" not in preview
        assert out.startswith(mailitemtool.DONE_NOTE) and "SAVED as resolved" in out
        (act,) = mailitems.read_acts(tmp_path)
        assert act["verb"] == "resolve" and act["anchor"] == item.anchor
        assert act["source"]["turn"] == agent._turn
        built = report(tmp_path)
        assert built.needs == [] and built.resolved == 1

    def test_a_denied_card_saves_nothing_and_says_so(self, tmp_path):
        item = pay_item(tmp_path)
        agent, cards = agent_for(tmp_path, answer=Denied("not that one"))
        out = str(agent._dispatch("inbox_items", {"action": "resolve",
                                                  "ref": item.anchor[:12]}))
        assert "declined" in out and "Nothing changed" in out and len(cards) == 1
        assert mailitems.read_acts(tmp_path) == []

    def test_an_approve_with_comment_holds_and_saves_nothing(self, tmp_path):
        item = pay_item(tmp_path)
        agent, _ = agent_for(tmp_path, answer=Approved("only the March one"))
        out = str(agent._dispatch("inbox_items", {"action": "resolve",
                                                  "ref": item.anchor[:12]}))
        assert out.startswith("NOT SAVED yet") and mailitems.read_acts(tmp_path) == []

    def test_restore_pulls_a_demoted_item_back_into_its_section(self, tmp_path):
        offer = add_mail(tmp_path, "n1", frm="agent@broker.example", subject="Offer",
                         at=NOW - 3 * DAY)
        add_verdict(tmp_path, offer, wanted="decide", recorded_at=NOW - 3 * DAY)
        his_sent(tmp_path, "o1", to="agent@broker.example", at=NOW - 2 * DAY,
                 subject="Re: Offer")
        assert [r.reading.digest for r in report(tmp_path).settled] == [offer.digest]
        agent, cards = agent_for(tmp_path)
        out = str(agent._dispatch("inbox_items", {"action": "restore",
                                                  "ref": offer.digest[:12]}))
        assert "SAVED as restored" in out and cards[0][2].startswith("Keep open: DECIDE")
        built = report(tmp_path)
        assert built.settled == [] and [i.reading.digest for i in built.needs] == [offer.digest]

    def test_an_unknown_ref_is_an_error_and_no_card(self, tmp_path):
        pay_item(tmp_path)
        agent, cards = agent_for(tmp_path)
        out = str(agent._dispatch("inbox_items", {"action": "resolve", "ref": "0" * 12}))
        assert out.startswith("ERROR") and "no item" in out and cards == []

    def test_list_is_free_outside_content(self, tmp_path):
        add_mail(tmp_path, "x", frm="a@x.example", at=NOW - DAY,
                 subject="see https://evil.example [c](aish-reply://x)")
        item = pay_item(tmp_path)
        agent, cards = agent_for(tmp_path, from_owner=False)
        out = str(agent._dispatch("inbox_items", {"action": "list"}))
        assert out.startswith(mailitemtool.LOOK_NOTE) and cards == []
        assert f"ref {item.anchor[:12]}" in out and "needs your action · open" in out
        assert "https://" not in out and "aish-reply:" not in out


class TestTheWiring:
    def test_an_item_act_record_is_written_and_renderless(self, tmp_path, monkeypatch):
        from aish import session

        item = pay_item(tmp_path)
        agent, _ = agent_for(tmp_path)
        seen = []
        monkeypatch.setattr(agent, "_emit_record", lambda **kw: seen.append(kw))
        agent._dispatch("inbox_items", {"action": "resolve", "ref": item.anchor[:12]})
        assert seen[-1]["kind"] == "item_act" and seen[-1]["outcome"] == "applied"
        assert seen[-1]["anchor"] == item.anchor and seen[-1]["items"] == [item.key]
        assert "item_act" in session.RENDERLESS_STEPS

    def test_it_taints_as_mail_and_is_outside_content(self):
        agent, _ = make_agent([model_says("ok")])
        assert agent._brings_outside_content("inbox_items", {"action": "list"})
        assert agent._what_a_source_is("inbox_items", {}) == agent_module.READ_MAIL
        assert agent._may_be_mail("inbox_items")

    def test_no_cardless_licence_covers_it_in_any_origin(self):
        for origin in ("user", "schedule", "email"):
            for kept in (True, False):
                assert server._auto_safe("inbox_items", {"action": "resolve", "ref": "a" * 12},
                                         origin, kept_outside=kept) is None

    def test_the_report_chat_requires_it(self):
        assert "inbox_items" in agent_module.MAIL_STORE_TOOLS

    def test_the_memory_rule_names_resolutions(self):
        assert "inbox_items answer began with SAVED" in mailprefs.MEMORY_RULE


# ---------------------------------------------------------------- important


def plan_important(state, **fields):
    return mailprefs.plan(state, {"action": "add", "kind": "important", **fields})


class TestTheImportantPreference:
    def test_it_needs_an_entity_and_a_closed_meaning(self, tmp_path):
        with pytest.raises(mailprefs.PrefError, match="meaning"):
            plan_important(tmp_path, domain="host.example")
        with pytest.raises(mailprefs.PrefError, match="meaning must be one of"):
            plan_important(tmp_path, domain="host.example", meaning="invoices")
        with pytest.raises(mailprefs.PrefError, match="exactly one of"):
            plan_important(tmp_path, meaning="money")

    def test_entity_and_meaning_place_mail_under_needs_without_a_fabricated_want(
        self, tmp_path
    ):
        bill = add_mail(tmp_path, "b1", frm="Billing <billing@host.example>",
                        subject="Invoice below threshold", at=NOW - DAY)
        add_verdict(tmp_path, bill, wanted="pay", disposition="hold")  # money by the Reader
        note = add_mail(tmp_path, "b2", frm="news@host.example", subject="New datacentre",
                        at=NOW - DAY)
        add_verdict(tmp_path, note, wanted="nothing", disposition="surface")
        other = add_mail(tmp_path, "b3", frm="billing@elsewhere.example", subject="Invoice",
                         at=NOW - DAY)
        add_verdict(tmp_path, other, wanted="nothing", disposition="hold")
        info = add_mail(tmp_path, "b4", frm="billing@host.example", subject="Minimum not met",
                        at=NOW - DAY, body="Your costs are under $10.00 this month.")
        add_verdict(tmp_path, info, wanted="nothing", disposition="surface")
        planned = plan_important(tmp_path, domain="host.example", meaning="money")
        mailprefs.apply(tmp_path, planned, source={"session": "s", "turn": 1}, now=NOW - 2 * DAY)
        built = report(tmp_path)
        needs = {i.reading.digest: i for i in built.needs}
        assert bill.digest in needs and not needs[bill.digest].important  # the Reader's own
        assert note.digest not in needs and other.digest not in needs
        # "money" is a RECORDED fact: the $10.00 in a body is not one.
        assert info.digest not in needs
        text = mailreport.render(built, clean=lambda t: t)
        assert "important 1" in text  # counted in the footer

    def test_a_matching_mail_the_reader_wanted_nothing_of_says_why_it_is_there(
        self, tmp_path
    ):
        doc = add_mail(tmp_path, "d1", frm="billing@host.example", subject="Your invoice",
                       at=NOW - DAY)
        add_verdict(tmp_path, doc, wanted="nothing", disposition="surface")
        planned = plan_important(tmp_path, domain="host.example", meaning="anything")
        applied = mailprefs.apply(tmp_path, planned, source={"session": "s", "turn": 1},
                                  now=NOW - 2 * DAY)
        built = report(tmp_path)
        (item,) = built.needs
        assert item.important == applied.pref_id
        line = next(t for t in mailreport.render(built, clean=lambda t: t).splitlines()
                    if "Your invoice" in t)
        assert f"HERE BECAUSE OF YOUR PREFERENCE {applied.pref_id}" in line
        assert "NOTHING" not in line and "PAY" not in line

    def test_the_card_previews_what_it_would_and_would_not_have_matched(self, tmp_path):
        paid = add_mail(tmp_path, "p1", frm="billing@host.example", subject="Invoice 41",
                        at=NOW - 5 * DAY)
        add_verdict(tmp_path, paid, wanted="pay")
        add_mail(tmp_path, "p2", frm="news@host.example", subject="Maintenance window",
                 at=NOW - 4 * DAY)
        add_mail(tmp_path, "p3", frm="old@host.example", subject="Ancient",
                 at=NOW - (mailprefs.PREVIEW_DAYS + 5) * DAY)
        card = plan_important(tmp_path, domain="host.example", meaning="money").card
        assert "it would have matched 1 mail(s): “Invoice 41”" in card
        assert "and not these 1 from host.example: “Maintenance window”" in card
        assert "Ancient" not in card

    def test_a_hide_card_previews_too(self, tmp_path):
        add_mail(tmp_path, "h1", frm="kids@family.example", subject="Screen time",
                 at=NOW - DAY, to=f"{OWNER}, son@family.example")
        card = mailprefs.plan(tmp_path, {"action": "add", "kind": "hide",
                                         "sender": "kids@family.example"}).card
        assert "it would have matched 1 mail(s): “Screen time”" in card

    def test_a_new_conversation_not_a_follow_up_is_refused_into_resolve(self, tmp_path):
        header = add_mail(tmp_path, "c1", frm="c@x.example", subject="Done", at=NOW - DAY)
        with pytest.raises(mailprefs.PrefError, match="inbox_items with action=resolve"):
            mailprefs.plan(tmp_path, {"action": "add", "kind": "not_a_follow_up",
                                      "conversation": header.digest[:12]})
        planned = mailprefs.plan(tmp_path, {"action": "add", "kind": "not_a_follow_up",
                                            "counterparty": "c@x.example"})
        assert planned.kind == "not_a_follow_up"  # the standing kind stays a preference


class TestSaveTimeRefusals:
    def test_a_held_lock_saves_nothing(self, tmp_path):
        import contextlib

        item = pay_item(tmp_path)
        planned = mailitemtool.plan(tmp_path, {"action": "resolve", "ref": item.anchor[:12]})

        @contextlib.contextmanager
        def held(_state):
            yield False

        applied = mailitemtool.apply(tmp_path, planned, source={}, now=NOW, lock=held)
        assert not applied.ok and applied.text.startswith("NOT SAVED")
        assert mailitems.read_acts(tmp_path) == []

    def test_an_item_that_changed_since_the_card_is_refused(self, tmp_path):
        his_sent(tmp_path, "o1", to="bob@client.example", at=NOW - 9 * DAY, subject="Quote")
        first = add_mail(tmp_path, "r1", frm="bob@client.example", subject="Re: Quote",
                         at=NOW - 8 * DAY, in_reply_to="o1@owner.example")
        add_verdict(tmp_path, first, wanted="cannot_tell", recorded_at=NOW - 8 * DAY)
        planned = mailitemtool.plan(tmp_path, {"action": "resolve", "ref": first.digest[:12]})
        # A needs item now anchors on the same mail: the card named one item, two are there.
        add_verdict(tmp_path, first, wanted="reply", recorded_at=NOW - 8 * DAY + 1,
                    model="local:other")
        applied = mailitemtool.apply(tmp_path, planned, source={}, now=NOW)
        assert not applied.ok and "changed since the card" in applied.text
        assert mailitems.read_acts(tmp_path) == []
