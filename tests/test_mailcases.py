"""The minimal case store and the fuzzy correlator (#142 W5 S1). Synthetic only;
the account numbers are the published example IBANs."""

from __future__ import annotations

import json

from mail_fixtures import DAY, OWNER, T0, gmail

from aish import mailcases, mailgate, mailindex
from aish.mailwatch import ingest

OWNERS = frozenset({OWNER})
IBAN_OLD = "PL27 1140 2004 0000 3002 0135 5387"
IBAN_NEW = "DE89 3704 0044 0532 0130 00"


def build(*messages: dict, state_dir=None):
    index = mailindex.MailIndex(state_dir)
    store = mailcases.CaseStore(state_dir)
    items = [mailindex.from_gmail(m, OWNERS) for m in messages]
    ingest(index, store, [i for i in items if i is not None])
    return index, store


def header(index, msg) -> mailindex.Header:
    item = mailindex.from_gmail(msg, OWNERS)
    assert item is not None
    found = index.get(item.header.digest)
    assert found is not None
    return found


def store_order(at=T0, gid="s1", key="LS-48213"):
    return gmail(gid, frm="Lamp Shop Example <orders@lampshop.example>",
                 subject=f"Order {key} confirmed",
                 body=f"Thank you for your order {key}. Total 249,00 zł. "
                      f"Your parcel will travel with Parcel Courier.", at=at,
                 message_id=f"{gid}@lampshop.example")


class TestExtraction:
    def test_a_key_needs_an_introducing_word_and_a_digit(self):
        keys = mailcases.extract_keys(
            "Order LS-48213 confirmed. Zamówienie nr 4471-2290. Order confirmed. "
            "Tracking: 1Z999AA10123456784")
        assert keys == {"LS-48213": "order", "4471-2290": "order",
                        "1Z999AA10123456784": "tracking"}

    def test_an_introducing_word_must_be_a_whole_word(self):
        assert mailcases.extract_keys("the border 12345 crossing, a preorder X9123") == {}

    def test_amounts_in_both_writing_conventions(self):
        assert mailcases.extract_amounts("Kwota 1 249,00 zł oraz EUR 12.50") == (
            ("PLN", 124900), ("EUR", 1250))

    def test_an_account_must_pass_its_checksum(self):
        assert mailcases.extract_coordinates(f"konto {IBAN_OLD}") == (
            "PL27114020040000300201355387",)
        assert mailcases.extract_coordinates("PL27 1140 2004 0000 3002 0135 5388") == ()
        assert mailcases.extract_coordinates("61 1090 1014 0000 0712 1981 2874") == (
            "PL61109010140000071219812874",)


class TestCaseCreation:
    def test_an_authenticated_mail_with_a_key_opens_an_inbound_case(self):
        index, store = build(store_order())
        (case,) = store.cases.values()
        assert case.created_by == "inbound" and "LS-48213" in case.keys
        assert case.domains == {"lampshop.example": header(index, store_order()).position}

    def test_an_unauthenticated_stranger_cannot_plant_a_case(self):
        _index, store = build(gmail("x", frm="eve@evil.example", subject="Order LS-99999",
                                    auth="fail"))
        assert store.cases == {}

    def test_a_key_in_his_own_mail_opens_an_owner_case(self):
        _index, store = build(gmail("o", frm=OWNER, to="desk@hotel.example",
                                    subject="Booking BK7731 question", labels=("SENT",)))
        (case,) = store.cases.values()
        assert case.created_by == "owner"

    def test_observing_the_same_mail_twice_changes_nothing(self, tmp_path):
        index, store = build(store_order(), state_dir=tmp_path)
        before = json.dumps({k: v.as_json() for k, v in store.cases.items()}, sort_keys=True)
        item = mailindex.from_gmail(store_order(), OWNERS)
        assert item is not None
        store.observe(header(index, store_order()), item.body, True, index)
        after = json.dumps({k: v.as_json() for k, v in store.cases.items()}, sort_keys=True)
        assert before == after


class TestAliasAdmission:
    def test_co_occurrence_alone_admits_nobody(self):
        """DMARC proves a sender owns ITS domain. An attacker passing DMARC with
        the owner's real order number is not thereby his counterparty."""
        index, store = build(
            store_order(),
            gmail("o", frm=OWNER, to="orders@lampshop.example", subject="Re: Order LS-48213",
                  at=T0 + 60, in_reply_to="s1@lampshop.example", labels=("SENT",)),
            gmail("x", frm="billing@lampshop-payments.example",
                  subject="Order LS-48213 payment failed", at=T0 + DAY),
        )
        (case,) = store.cases.values()
        assert set(case.admitted) == {"lampshop.example"}

    def test_his_reply_to_the_senders_mail_admits_it(self):
        index, store = build(
            store_order(),
            gmail("o", frm=OWNER, to="help@other.example", subject="Re: Order LS-48213",
                  at=T0 + 60, in_reply_to="s1@lampshop.example", labels=("SENT",)),
        )
        (case,) = store.cases.values()
        assert case.admitted["lampshop.example"]["by"] == "owner_act_toward_sender"

    def test_a_domain_he_did_not_address_is_not_admitted_by_his_act(self):
        index, store = build(
            store_order(),
            gmail("c", frm="parcels@courier.example", subject="Parcel for order LS-48213",
                  at=T0 + 30),
            gmail("o", frm=OWNER, to="orders@lampshop.example", subject="Re: Order LS-48213",
                  at=T0 + 60, in_reply_to="s1@lampshop.example", labels=("SENT",)),
        )
        (case,) = store.cases.values()
        assert set(case.admitted) == {"lampshop.example"}

    def test_a_judged_proposal_is_counted_and_admits_nothing(self):
        _index, store = build(store_order())
        (case,) = store.cases.values()
        store.propose(case.id, "lampshop-payments.example")
        store.propose(case.id, "lampshop-payments.example")
        assert case.proposals == {"lampshop-payments.example": 2}
        assert "lampshop-payments.example" not in case.admitted
        assert "lampshop-payments.example" not in store.admitted_domains((T0 + 9 * DAY, "z"))


class TestCorrelator:
    def test_a_courier_notice_finds_the_store_order_by_name_and_time(self):
        """The courier/store pair, synthetic: no shared key at all."""
        courier = gmail("c", frm="Parcel Courier <noreply@courier.example>",
                        subject="Your parcel is arriving tomorrow",
                        body="Your parcel from Lamp Shop Example arrives tomorrow.",
                        at=T0 + 2 * DAY)
        index, store = build(store_order(), courier)
        found = mailcases.candidates(store, header(index, courier), "Your parcel from Lamp "
                                     "Shop Example arrives tomorrow.")
        (cand,) = found
        assert set(cand.reasons) >= {"name", "window"} and "key" not in cand.reasons

    def test_a_shared_tracking_number_is_a_key_match(self):
        shipped = gmail("s", frm="orders@lampshop.example", subject="Order LS-48213 shipped",
                        body="Tracking: 1Z999AA10123456784", at=T0)
        courier = gmail("c", frm="noreply@courier.example", subject="Parcel 1Z999AA10123456784",
                        at=T0 + 20 * DAY)
        index, store = build(shipped, courier)
        found = mailcases.candidates(store, header(index, courier), "")
        assert found[0].reasons[0] == "key"

    def test_a_mail_never_correlates_with_the_case_it_opened_itself(self):
        index, store = build(store_order())
        assert mailcases.candidates(store, header(index, store_order()), "") == ()

    def test_a_case_from_the_future_is_not_a_candidate(self):
        early = gmail("e", frm="x@other.example", subject="Order LS-48213?", at=T0 - DAY,
                      auth="fail")
        index, store = build(early, store_order())
        assert len(store.cases) == 1  # opened by the store's mail, a day LATER
        assert mailcases.candidates(store, header(index, early), "") == ()

    def test_the_shortlist_is_bounded(self):
        orders = [store_order(at=T0 + n, gid=f"s{n}", key=f"LS-{48200 + n}") for n in range(9)]
        probe = gmail("p", frm="noreply@courier.example", subject="Parcel",
                      body="from Lamp Shop Example", at=T0 + DAY)
        index, store = build(*orders, probe)
        found = mailcases.candidates(store, header(index, probe), "from Lamp Shop Example")
        assert len(found) == mailcases.MAX_CANDIDATES


class TestCaseFlags:
    def builder_case(self, *, later_auth="pass", later_from="office@builder.example"):
        invoice = gmail("i", frm="office@builder.example", subject="Invoice 2026/41",
                        body=f"Invoice 2026/41, 12 300,00 zł to {IBAN_OLD}", at=T0,
                        message_id="i@builder.example")
        reply = gmail("r", frm=OWNER, to="office@builder.example", subject="Re: Invoice 2026/41",
                      at=T0 + 60, in_reply_to="i@builder.example", labels=("SENT",))
        change = gmail("c", frm=later_from, subject="Invoice 2026/41 — new account",
                       body=f"Please pay invoice 2026/41 to {IBAN_NEW}", at=T0 + DAY,
                       auth=later_auth)
        index, store = build(invoice, reply, change)
        return index, store, header(index, change)

    def test_an_account_the_case_never_had_is_flagged(self):
        index, store, change = self.builder_case()
        (case,) = store.cases.values()
        flags = mailcases.case_flags(store, change, mailcases.extract_coordinates(IBAN_NEW),
                                     [case.id])
        assert flags.coordinates_changed == (case.id,)

    def test_the_same_account_again_is_not_flagged(self):
        index, store, change = self.builder_case()
        (case,) = store.cases.values()
        same = mailcases.case_flags(store, change, mailcases.extract_coordinates(IBAN_OLD),
                                    [case.id])
        assert same.coordinates_changed == ()

    def test_a_sender_outside_the_cases_admitted_senders_is_flagged(self):
        index, store, change = self.builder_case(later_from="office@bui1der.example",
                                                 later_auth="fail")
        (case,) = store.cases.values()
        flags = mailcases.case_flags(store, change, (), [case.id])
        assert flags.sender_not_admitted == (case.id,)
        assert mailgate.lookalike_of("bui1der.example", store.admitted_domains(change.position))

    def test_a_stranger_cannot_seed_the_account_it_later_asks_for(self):
        """Found in delivery review's wake: a sender passing DMARC on its OWN
        domain mails the order number with its own account; once the case has
        an admitted sender, only that sender's accounts are the history."""
        order = gmail("o", frm=OWNER, to="shop@shop.example", subject="Order LS-77001",
                      at=T0, labels=("SENT",), message_id="o@example.org")
        shop = gmail("s", frm="shop@shop.example", subject="Re: Order LS-77001",
                     body=f"Pay order LS-77001 to {IBAN_OLD}", at=T0 + 60,
                     in_reply_to="o@example.org", message_id="s@shop.example")
        thanks = gmail("t", frm=OWNER, to="shop@shop.example", subject="Re: Order LS-77001",
                       at=T0 + 120, in_reply_to="s@shop.example", labels=("SENT",))
        seed = gmail("x1", frm="billing@shop-billing.example", subject="Order LS-77001",
                     body=f"Order LS-77001: account {IBAN_NEW}", at=T0 + DAY)
        ask = gmail("x2", frm="billing@shop-billing.example", subject="Order LS-77001 overdue",
                    body=f"Pay order LS-77001 now to {IBAN_NEW}", at=T0 + 2 * DAY)
        index, store = build(order, shop, thanks, seed, ask)
        (case,) = store.cases.values()
        assert set(case.admitted) == {"shop.example"}
        flags = mailcases.case_flags(store, header(index, ask),
                                     mailcases.extract_coordinates(IBAN_NEW), [case.id])
        assert flags.coordinates_changed == (case.id,)
        assert flags.sender_not_admitted == (case.id,)

    def test_an_unauthenticated_mail_never_enters_the_account_history(self):
        _index, store, _change = self.builder_case(later_auth="fail")
        (case,) = store.cases.values()
        assert {c["value"] for c in case.coordinates} == {"PL27114020040000300201355387"}


class TestEviction:
    def test_a_case_goes_whole_and_leaves_a_dated_tombstone(self, tmp_path):
        _index, store = build(store_order(), state_dir=tmp_path)
        (case_id,) = store.cases
        assert store.evict(case_id, "owner deleted it", now=T0)
        assert not (tmp_path / "mail" / "cases" / f"{case_id}.json").exists()
        tomb = json.loads((tmp_path / "mail" / "tombstones" / f"{case_id}.json").read_text())
        assert tomb["reason"] == "owner deleted it" and tomb["at"] == T0
        assert mailcases.CaseStore(tmp_path).cases == {}

    def test_one_unreadable_case_file_costs_that_file(self, tmp_path):
        build(store_order(), store_order(gid="s2", key="LS-11111", at=T0 + 5),
              state_dir=tmp_path)
        victim = sorted((tmp_path / "mail" / "cases").glob("*.json"))[0]
        victim.write_text("[")
        again = mailcases.CaseStore(tmp_path)
        assert again.health.loaded == 1 and again.health.unreadable == [victim.name]
