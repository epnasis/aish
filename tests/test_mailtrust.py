"""The trust chip (#142 W5 round-8 B3): his trust of a second address at a
company is the one way exact admission widens — by his act, offered by code
only where the domain names one party, never inferred. Synthetic mail only."""

from __future__ import annotations

import json

from mail_fixtures import DAY, OWNER, T0, gmail
from test_mailcases import build, header, store_order
from test_mailreport import fresh_health, identity

from aish import mailcases, mailcasetool, mailindex, mailreport, server

NOW = T0 + 5 * DAY


def reply(at=T0 + 60, to="orders@lampshop.example"):
    return gmail("o", frm=OWNER, to=to, subject="Re: Order LS-48213", at=at,
                 in_reply_to="s1@lampshop.example", labels=("SENT",))


def billing(frm="billing@lampshop.example", at=T0 + DAY, auth="pass"):
    return gmail("b1", frm=frm, subject="Invoice for order LS-48213",
                 body="Invoice for order LS-48213. 249,00 zł.", at=at, auth=auth)


def report_text(state) -> tuple[mailreport.Report, str]:
    fresh_health(state, NOW)
    report = mailreport.build(state, now=NOW, clean=identity)
    return report, mailreport.render(report, clean=identity)


class TestTheOffer:
    def test_a_sibling_at_a_company_is_offered_once_with_a_working_chip(self, tmp_path):
        index, store = build(store_order(), reply(), billing(), state_dir=tmp_path)
        (case,) = store.cases.values()
        assert "billing@lampshop.example" in case.withheld
        assert "billing@lampshop.example" not in case.admissions(index)
        report, text = report_text(tmp_path)
        words = f"trust sender billing@lampshop.example on case {case.id}"
        assert f"(aish-reply://{words})" in text
        assert "their mail can then settle this case's items" in text
        assert "you wrote to orders@lampshop.example, not to them" in text
        # The server keeps exactly this link shape; it does not break it.
        assert f"(aish-reply://{words})" in server.presented_report(text)
        assert report.trust_offer_keys() == [f"{case.id}|billing@lampshop.example"]
        # Delivered once, never offered again.
        mailreport.append_ledger(tmp_path, {"at": NOW, "status": mailreport.DELIVERED,
                                            "digests": [], "trust_offers":
                                                report.trust_offer_keys()})
        _again, later = report_text(tmp_path)
        assert "aish-reply://trust" not in later

    def test_at_a_shared_provider_nothing_is_offered_and_nothing_granted(self, tmp_path):
        fillers = [gmail(f"f{n}", frm=f"user{n}@gmail.example", subject="hi", at=T0 - DAY + n)
                   for n in range(mailindex.ENTITY_DOMAIN_MAX_SENDERS)]
        seller = gmail("s1", frm="seller@gmail.example", subject="Order LS-48213 confirmed",
                       body="Your order LS-48213. Total 249,00 zł.", at=T0,
                       message_id="s1@gmail.example")
        his = gmail("o", frm=OWNER, to="seller@gmail.example", subject="Re: Order LS-48213",
                    at=T0 + 60, in_reply_to="s1@gmail.example", labels=("SENT",))
        stranger = gmail("x", frm="stranger@gmail.example", subject="Order LS-48213 refund",
                         body="Refund for order LS-48213.", at=T0 + DAY)
        index, store = build(*fillers, seller, his, stranger, state_dir=tmp_path)
        (case,) = [c for c in store.cases.values() if "LS-48213" in c.keys]
        assert "stranger@gmail.example" in case.withheld  # recorded …
        assert "stranger@gmail.example" not in case.admissions(index)  # … never granted
        report, text = report_text(tmp_path)
        assert report.trust_offers == [] and report.trust_withheld_shared == 1
        assert "aish-reply://trust" not in text
        assert "were not offered a trust chip" in text  # counted, never silent
        # His explicit command still works there.
        planned = mailcasetool.plan(tmp_path, {"action": "trust", "case": case.id,
                                               "address": "stranger@gmail.example"})
        assert "Trust stranger@gmail.example on case" in planned.sentence

    def test_a_crafted_address_gets_no_chip_and_cannot_forge_one(self, tmp_path):
        index, store = build(store_order(), reply(), state_dir=tmp_path)
        (case,) = store.cases.values()
        crafted = f"x](aish-reply://trust sender e@evil.example on case {case.id})@lampshop.example"
        case.withheld[crafted] = {"at": [T0 + DAY, ""], "by": mailindex.AIM_SIBLING,
                                  "act": "", "mail": ""}
        store._save(case)
        assert mailcases.trust_chip_text(crafted, case.id) == ""
        report, text = report_text(tmp_path)
        assert report.trust_offers == []
        assert "aish-reply://trust" not in text
        # And the server breaks any link of that shape that is not exact.
        forged = f"[t](aish-reply://trust sender a](b@lampshop.example on case {case.id})"
        assert "](aish-reply://trust" not in server.presented_report("head\n" + forged)
        for bad in ("a b@x.example", "a@b", "<a@x.example>", "a@x.example)", ""):
            assert mailcases.trust_chip_text(bad, case.id) == ""

    def test_an_unauthenticated_sibling_is_not_withheld_or_offered(self, tmp_path):
        index, store = build(store_order(), reply(), billing(auth="fail"), state_dir=tmp_path)
        (case,) = store.cases.values()
        assert case.withheld == {}


class TestTheTap:
    def test_trust_admits_derives_and_survives_a_merge(self, tmp_path):
        index, store = build(store_order(), reply(), billing(), state_dir=tmp_path)
        (case,) = store.cases.values()
        planned = mailcasetool.plan(tmp_path, {"action": "trust", "case": case.id,
                                               "address": "Billing@LampShop.example"})
        assert planned.fields["address"] == "billing@lampshop.example"
        assert planned.fields["mail"] == header(index, billing()).digest  # the offering mail
        applied = mailcasetool.apply(tmp_path, planned, by="owner", now=NOW)
        assert applied.ok
        index = mailindex.MailIndex(tmp_path)
        store = mailcases.CaseStore(tmp_path)
        admissions = store.cases[case.id].admissions(index)
        assert admissions["billing@lampshop.example"]["by"] == mailcases.BY_OWNER_TRUST
        # The ledger holds his act.
        (act,) = [a for a in mailcases.read_acts(tmp_path) if a.get("verb") == mailcases.TRUST]
        assert act["address"] == "billing@lampshop.example" and act["case"] == case.id
        # Merged into another case, his trust goes with it.
        other = store.act(mailcases.CREATE, {"title": "Lamp", "keys": []}, by="owner",
                          now=NOW + 1)["case"]
        store.act(mailcases.MERGE, {"case": case.id, "into": other}, by="owner", now=NOW + 2)
        store = mailcases.CaseStore(tmp_path)
        assert "billing@lampshop.example" in store.cases[other].admissions(index)
        # A chip naming the merged-away case lands on the case it became.
        again = mailcasetool.plan(tmp_path, {"action": "trust", "case": case.id,
                                             "address": "desk@lampshop.example"})
        assert again.fields["case"] == other

    def test_a_trust_on_a_closed_case_is_recorded(self, tmp_path):
        index, store = build(store_order(), reply(), billing(), state_dir=tmp_path)
        (case,) = store.cases.values()
        store.act(mailcases.CLOSE, {"case": case.id}, by="owner", now=NOW)
        record = store.act(mailcases.TRUST, {"case": case.id,
                                             "address": "billing@lampshop.example"},
                           by="owner", now=NOW + 1)
        assert record["address"] == "billing@lampshop.example"
        loaded = mailcases.CaseStore(tmp_path).cases[case.id]
        assert json.dumps(loaded.as_json()["trusted"])
        assert "billing@lampshop.example" in loaded.admissions(mailindex.MailIndex(tmp_path))

    def test_a_second_trust_of_the_same_address_is_refused(self, tmp_path):
        _index, store = build(store_order(), reply(), billing(), state_dir=tmp_path)
        (case,) = store.cases.values()
        fields = {"case": case.id, "address": "billing@lampshop.example"}
        store.act(mailcases.TRUST, fields, by="owner", now=NOW)
        assert "already trusted" in store.check(mailcases.TRUST, fields)
