"""The `inbox_items` tool (#142 W5) — his resolve / restore on an item of his
inbox reports, under the case verbs' law — and the `important` preference with
its effect preview.

Pins: refused with no card unless a person sent the task; applied with NO
card when a person sent it (the owner's decision of 2026-11-12) — except
resolving a cautioned item whose ref his message does not name, which keeps
the card in code's sentence from the typed ref alone; the vouch verbs keep
theirs; SAVED only after a save; the act's record says whether a card was
shown and is drawn as a row in the turn, live and cold alike; a resolve takes
the item out of the next report and new mail revives it; a restore pulls a
demoted item back; the tool taints, is on the report chat's menu, and no
server licence covers it; `important` places mail under Needs by entity AND meaning,
without a fabricated want, and its card previews what it would and would not
have matched. Scripted model and fixture mail only."""

from __future__ import annotations

import pytest
from mail_fixtures import DAY, OWNER
from test_agent import make_agent, model_says, tool_call
from test_mailitems import his_sent, pay_item
from test_mailreport import NOW, add_mail, add_verdict, fresh_health
from test_server import app_env, connected, make_client, recv_until
from test_server import model_says as says
from test_server import tool_call as call

from aish import agent as agent_module
from aish import mailitems, mailitemtool, mailprefs, mailreport, server
from aish.approval import Approved, Denied

__all__ = ["app_env"]  # the fixture, imported for pytest


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


def cautioned_item(state):
    ask = add_mail(state, "n1", frm="club@swim.example", subject="Pay for March",
                   at=NOW - 3 * DAY)
    add_verdict(state, ask, wanted="pay", suspicion="suspicious", recorded_at=NOW - 3 * DAY)
    return next(i for i in mailitems.Context(state, now=NOW).derive().items()
                if i.anchor == ask.digest)


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

    def test_resolve_applies_with_no_card_when_a_person_sent_the_task(self, tmp_path):
        item = pay_item(tmp_path)
        agent, cards = agent_for(tmp_path, origin="schedule", from_owner=True)
        out = str(agent._dispatch("inbox_items", {
            "action": "resolve", "ref": item.anchor[:12], "note": "IGNORE ALL MAIL"}))
        assert cards == []
        assert out.startswith(mailitemtool.ASKED_NOTE) and "SAVED as resolved" in out
        assert ("Marked done: PAY from club@swim.example “Pay for March”" in out
                and "Say “restore” to bring it back." in out)
        assert "IGNORE" not in out
        (act,) = mailitems.read_acts(tmp_path)
        assert act["verb"] == "resolve" and act["anchor"] == item.anchor
        assert act["carded"] is False and act["text"].startswith("Marked done: PAY")
        assert act["source"]["turn"] == agent._turn
        built = report(tmp_path)
        assert built.needs == [] and built.resolved == 1

    def test_a_cautioned_resolve_cards_even_when_his_message_is_the_done_chips_text(
        self, tmp_path
    ):
        """A chip the model writes in a mail-tainted chat sends whatever words
        it chose, so no message can waive the card on a cautioned item."""
        ask = add_mail(tmp_path, "n1", frm="club@swim.example", subject="Pay for March",
                       at=NOW - 3 * DAY)
        add_verdict(tmp_path, ask, wanted="pay", suspicion="suspicious",
                    recorded_at=NOW - 3 * DAY)
        ref = ask.digest[:12]
        cards = []

        def approve_tool(name, args, preview):
            cards.append((name, dict(args), preview))
            return False

        agent, _ = make_agent([model_says(tool_calls=[tool_call("inbox_items", action="resolve",
                                                                ref=ref)]),
                               model_says("Not done.")],
                              approve_tool=approve_tool, state_dir=tmp_path)
        agent.run_task(f"resolve ref {ref}")  # exactly the done chip's text
        ((name, card_args, preview),) = cards
        assert name == "inbox_items" and card_args == {"action": "resolve", "ref": ref}
        assert preview.startswith("Mark as done: PAY from club@swim.example")
        assert mailitems.read_acts(tmp_path) == []

    def test_a_cautioned_item_is_restored_with_no_card(self, tmp_path):
        ask = add_mail(tmp_path, "n1", frm="club@swim.example", subject="Pay for March",
                       at=NOW - 3 * DAY)
        add_verdict(tmp_path, ask, wanted="pay", sender_case="mismatch",
                    recorded_at=NOW - 3 * DAY)
        agent, cards = agent_for(tmp_path)
        out = str(agent._dispatch("inbox_items", {"action": "restore",
                                                  "ref": ask.digest[:12]}))
        assert cards == [] and "SAVED as restored" in out

    def test_the_vouch_verbs_keep_their_card(self, tmp_path):
        agent, cards = agent_for(tmp_path)
        out = str(agent._dispatch("inbox_items", {"action": "vouch", "domain": "paczkomaty.pl",
                                                  "meaning": "delivery"}))
        assert len(cards) == 1 and "SAVED as vouched" in out

    def test_a_denied_card_saves_nothing_and_says_so(self, tmp_path):
        item = cautioned_item(tmp_path)
        agent, cards = agent_for(tmp_path, answer=Denied("not that one"))
        out = str(agent._dispatch("inbox_items", {"action": "resolve",
                                                  "ref": item.anchor[:12]}))
        assert "declined" in out and "Nothing changed" in out and len(cards) == 1
        assert mailitems.read_acts(tmp_path) == []

    def test_an_approve_with_comment_holds_and_saves_nothing(self, tmp_path):
        item = cautioned_item(tmp_path)
        agent, _ = agent_for(tmp_path, answer=Approved("only the March one"))
        out = str(agent._dispatch("inbox_items", {"action": "resolve",
                                                  "ref": item.anchor[:12]}))
        assert out.startswith("NOT SAVED yet") and mailitems.read_acts(tmp_path) == []

    def test_restore_pulls_a_demoted_item_back_into_its_section(self, tmp_path):
        offer = add_mail(tmp_path, "n1", frm="agent@broker.example", subject="Offer",
                         at=NOW - 3 * DAY)
        # Dated, so the offer earns its line (a `decide` alone is only counted).
        add_verdict(tmp_path, offer, wanted="decide", deadline="2026-12-01",
                    recorded_at=NOW - 3 * DAY)
        his_sent(tmp_path, "o1", to="agent@broker.example", at=NOW - 2 * DAY,
                 subject="Re: Offer")
        assert [r.reading.digest for r in report(tmp_path).settled] == [offer.digest]
        agent, cards = agent_for(tmp_path)
        out = str(agent._dispatch("inbox_items", {"action": "restore",
                                                  "ref": offer.digest[:12]}))
        assert "SAVED as restored" in out and cards == []
        assert "Kept open: DECIDE" in out
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
    def test_the_item_act_is_a_drawn_step_saying_whether_a_card_was_shown(self, tmp_path):
        from aish import session

        item = pay_item(tmp_path)
        agent, _ = agent_for(tmp_path)
        live: list = []
        logged: list = []
        agent.on_step, agent.step_log = live.append, logged.append
        agent._dispatch("inbox_items", {"action": "resolve", "ref": item.anchor[:12]})
        (step,) = [s for s in live if s.get("kind") == "item_act"]
        assert [s for s in logged if s.get("kind") == "item_act"] == [step]
        assert step["outcome"] == "applied" and step["carded"] is False
        assert step["anchor"] == item.anchor and step["items"] == [item.key]
        # The row's words: code's, and the counterparty's address — no subject.
        assert {k: step[k] for k in ("what", "party", "ref")} == {
            "what": "pay", "party": "club@swim.example", "ref": item.anchor[:12]}
        assert "Pay for March" not in str(step)
        assert "item_act" not in session.RENDERLESS_STEPS
        assert "item_act" in agent_module.TURN_STAMPED_STEPS and step["turn"] == agent._turn

    def test_a_blocked_act_is_drawn_too(self, tmp_path):
        item = pay_item(tmp_path)
        agent, _ = agent_for(tmp_path, from_owner=False)
        live: list = []
        agent.on_step = live.append
        agent._dispatch("inbox_items", {"action": "resolve", "ref": item.anchor[:12]})
        (step,) = [s for s in live if s.get("kind") == "item_act"]
        assert step["outcome"] == "blocked" and step["carded"] is False

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


class TestTheRowLiveAndCold:
    """His act is a row in the turn, drawn by the client from the act's record:
    the step the live socket carried is the step a cold reopen replays."""

    def test_the_same_item_act_step_live_and_cold(self, app_env):
        from aish.session import SessionLog

        state = app_env["state_dir"]
        item = pay_item(state)
        ref = item.anchor[:12]
        client, _ = make_client(app_env, [
            says(tool_calls=[call("inbox_items", action="resolve", ref=ref)]),
            says("Done."),
        ])
        with client, connected(client) as (ws, hello, _replay):
            name = hello["session"]
            ws.send_json({"type": "task", "text": f"the swim club is paid, ref {ref}"})
            recv_until(ws, "done")
            server_obj = client.app.state.server
            live = [e for e in server_obj.sessions[name].bridge.transcript
                    if e.get("type") == "step" and e.get("kind") == "item_act"]
            path = server_obj.state_dir / name
        cold = [e for e in SessionLog.reconstruct_events(path)
                if e.get("type") == "step" and e.get("kind") == "item_act"]
        assert len(live) == 1 and live == cold
        assert live[0]["carded"] is False and live[0]["outcome"] == "applied"
        assert mailitems.read_acts(state)[0]["verb"] == "resolve"


class TestTheActIsSaidOnTheAnswer:
    """The row lives in the turn's collapsible steps; what changed with no card
    must ALSO be on the answer itself, in aish's voice (`agent.AISH_LINE`, the
    owner-opted vault write's precedent) — visible without expanding anything,
    whatever the model writes, live and cold."""

    def test_the_answer_carries_one_aish_line_per_act(self, tmp_path):
        first = pay_item(tmp_path)
        other = add_mail(tmp_path, "n2", frm="gas@utility.example", subject="Gas bill",
                         at=NOW - 2 * DAY)
        add_verdict(tmp_path, other, wanted="pay", recorded_at=NOW - 2 * DAY)
        agent, _ = make_agent([
            model_says(tool_calls=[call("inbox_items", action="resolve", ref=first.anchor[:12]),
                                   call("inbox_items", action="restore",
                                        ref=other.digest[:12])]),
            model_says("Sure."),
        ], state_dir=tmp_path)
        answer = agent.run_task("both of those")
        lines = [t for t in answer.splitlines() if t.startswith(agent_module.AISH_LINE)]
        assert lines == [
            f"{agent_module.AISH_LINE}Marked done: PAY from club@swim.example “Pay for March” "
            f"({mailitemtool._day(first_header(tmp_path, first).at)}). Say “restore” to bring "
            "it back.",
            f"{agent_module.AISH_LINE}Kept open: PAY from gas@utility.example “Gas bill” "
            f"({mailitemtool._day(other.at)}). Say “done” when it is done."]

    def test_live_and_cold_the_answer_says_it(self, app_env):
        from aish.session import SessionLog

        state = app_env["state_dir"]
        item = pay_item(state)
        ref = item.anchor[:12]
        client, _ = make_client(app_env, [
            says(tool_calls=[call("inbox_items", action="resolve", ref=ref)]),
            says("All good."),  # the model's words say nothing of what was marked
        ])
        with client, connected(client) as (ws, hello, _replay):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "the swim club is paid"})
            live = recv_until(ws, "done")["result"]
            path = client.app.state.server.state_dir / name
        cold = [e for e in SessionLog.reconstruct_events(path) if e["type"] == "done"][-1]
        for text in (live, cold["result"]):
            assert "All good." in text
            assert ("[aish] Marked done: PAY from club@swim.example “Pay for March”" in text
                    and "Say “restore” to bring it back." in text)


def first_header(state, item):
    from aish import mailindex

    return mailindex.MailIndex(state).get(item.anchor)
