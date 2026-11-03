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

    def test_his_reply_addressed_to_the_sender_admits_it(self):
        index, store = build(
            store_order(),
            gmail("o", frm=OWNER, to="orders@lampshop.example", subject="Re: Order LS-48213",
                  at=T0 + 60, in_reply_to="s1@lampshop.example", labels=("SENT",)),
        )
        (case,) = store.cases.values()
        assert case.admitted["lampshop.example"]["by"] == "owner_act_toward_sender"

    def test_his_bcc_to_the_shop_admits_it(self):
        index, store = build(
            store_order(),
            gmail("o", frm=OWNER, to="bip@wenda.example", bcc="orders@lampshop.example",
                  subject="Fwd: Order LS-48213 confirmed", at=T0 + 60,
                  in_reply_to="s1@lampshop.example", labels=("SENT",)),
        )
        (case,) = store.cases.values()
        assert "lampshop.example" in case.admitted

    def test_a_shared_providers_domain_is_admitted_by_nobodys_act(self):
        """N1: admission is domain-scoped, so at a domain many addresses use it
        would admit everyone there; his act names one address, so nobody is."""
        fillers = [gmail(f"f{n}", frm=f"user{n}@gmail.example", subject="hi", at=T0 - DAY + n)
                   for n in range(mailindex.ENTITY_DOMAIN_MAX_SENDERS)]
        seller = gmail("s1", frm="seller@gmail.example", subject="Order LS-48213 confirmed",
                       body="Your order LS-48213. Total 249,00 zł.", at=T0,
                       message_id="s1@gmail.example")
        index, store = build(
            *fillers, seller,
            gmail("o", frm=OWNER, to="seller@gmail.example", subject="Re: Order LS-48213",
                  at=T0 + 60, in_reply_to="s1@gmail.example", labels=("SENT",)),
        )
        (case,) = [c for c in store.cases.values() if "LS-48213" in c.keys]
        assert case.admitted == {}
        # Note 2: the withholding is recorded where the admission would be.
        assert case.withheld["gmail.example"]["by"] == mailindex.AIM_SHARED_DOMAIN
        loaded = mailcases.Case.from_json(json.loads(json.dumps(case.as_json())))
        assert loaded.withheld == case.withheld

    def test_a_reply_or_forward_addressed_elsewhere_admits_nobody(self):
        """An act toward a sender is addressed TO it: In-Reply-To naming the
        sender's mail is not enough. His forward of the shop's confirmation to
        his other address, and his reply sent to someone else, admit nothing."""
        for to, subject in (("bip@wenda.example", "Fwd: Order LS-48213 confirmed"),
                            ("help@other.example", "Re: Order LS-48213")):
            index, store = build(
                store_order(),
                gmail("o", frm=OWNER, to=to, subject=subject,
                      at=T0 + 60, in_reply_to="s1@lampshop.example", labels=("SENT",)),
            )
            (case,) = store.cases.values()
            assert len(case.mails) == 2 and case.admitted == {}, to

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


class TestInflectedKeys:
    """Polish declines the nouns that introduce a key; the stems find the
    inflected forms (round-3, the owner's InPost notices yielded nothing)."""

    def test_inflected_forms_yield_their_keys_with_their_kinds(self):
        assert mailcases.extract_keys("Numer paczki: 620999000111222333444555") == {
            "620999000111222333444555": "tracking"}
        assert mailcases.extract_keys("Szczegóły zamówienia nr 98103010/2026") == {
            "98103010/2026": "order"}
        assert mailcases.extract_keys("Numer przesyłki: PX7687080605") == {
            "PX7687080605": "tracking"}
        assert mailcases.extract_keys("Do faktury nr VQGEQB1M-0005") == {
            "VQGEQB1M-0005": "invoice"}

    def test_a_stem_introduces_a_key_only_under_an_identity_marker(self):
        """Round-3 note N2: "Twój PIN do odbioru paczki: 646235" is a one-time
        secret beside the noun, not the parcel's number: a short all-digit
        token needs an identity marker; "kod" is never one."""
        assert mailcases.extract_keys("Twój PIN do odbioru paczki: 646235") == {}
        assert mailcases.extract_keys("przesyłki kod 778812") == {}
        # Without a marker, an identifier-shaped token still stands.
        assert mailcases.extract_keys("Do faktury VQGEQB1M-0005") == {"VQGEQB1M-0005": "invoice"}
        assert mailcases.extract_keys("fakturę do zamówienia 261509440") == {
            "261509440": "order"}
        assert mailcases.extract_keys("tymczasowej rezerwacji: XYO3K4") == {"XYO3K4": "booking"}
        # Under a marker, a short number is the matter's number.
        assert mailcases.extract_keys("zamówienia nr 66786") == {"66786": "order"}
        assert mailcases.extract_keys("Nr przesyłki PX7687080605") == {
            "PX7687080605": "tracking"}

    def test_each_list_is_counted_by_what_it_matched(self):
        from aish import vocab

        vocab.drain()
        mailcases.extract_keys("Order 77123 — numer paczki: 620999000111222333444555")
        tallies = vocab.drain()
        assert tallies["mailcases._KEY_WORDS"]["matched"] == 1
        assert tallies["mailcases._KEY_STEMS"]["matched"] == 1

    def test_a_compound_beyond_the_bounded_ending_is_not_an_introducing_word(self):
        """`paczkomat WAW45H` names a locker shared by unrelated parcels."""
        assert mailcases.extract_keys("Wybrany paczkomat: WAW45H") == {}

    def test_the_whole_words_keep_their_unbounded_tail(self):
        assert mailcases.extract_keys("Bestellungsnummer: 4711220") == {"4711220": "order"}

    def test_a_date_is_never_a_key(self):
        for text in ("Data zamówienia: 2026-10-10", "zamówienia: 26/08/2026",
                     "order 16.10.2026"):
            assert mailcases.extract_keys(text) == {}, text

    def test_the_stems_are_counted_under_their_own_name(self):
        from aish import vocab

        vocab.drain()
        mailcases.extract_keys("Numer paczki: 620999000111222333444555")
        mailcases.extract_keys("order 77123 shipped")
        tallies = vocab.drain()
        assert tallies["mailcases._KEY_STEMS"]["matched"] == 1
        assert tallies["mailcases._KEY_WORDS"]["matched"] == 1


class TestTheRecheckRecordsKeysToo:
    """Round-4 note 4: the Tuta recheck backfilled domains, amounts and
    accounts but not keys. It now records exactly what observation records
    from trusted mail (`CaseStore._record_facts`)."""

    def test_a_mail_authenticated_later_gains_its_keys(self):
        store = mailcases.CaseStore()
        index = mailindex.MailIndex()
        his = mailindex.from_gmail(gmail("o1", frm=OWNER, to="shop@store.example",
                                         subject="Order AB12345", at=T0,
                                         labels=("SENT",)), frozenset({OWNER}))
        assert his is not None
        index.add(his.header, his.body)
        store.observe(his.header, his.body, True, index)
        later = mailindex.from_gmail(gmail("i1", frm="shop@store.example",
                                           subject="Order AB12345 — invoice FV99887",
                                           body="Your order AB12345, invoice FV99887, 40,00 zł",
                                           at=T0 + DAY, auth="fail"), frozenset({OWNER}))
        assert later is not None
        index.add(later.header, later.body)
        store.observe(later.header, later.body, False, index)
        (case,) = store.cases.values()
        assert "FV99887" not in case.keys and case.amounts == []
        assert store.record_authenticated(case, later.header, later.body)
        assert case.keys["FV99887"]["at"] == later.header.position
        assert case.amounts and case.amounts[0]["cents"] == 4000
        assert not store.record_authenticated(case, later.header, later.body)  # idempotent


class TestTheMarkersHaveOneAuthority:
    """Round-4 note 2: the identity markers are a declared, counted list, and
    the pattern's both marker positions derive from it."""

    def test_every_declared_marker_governs_before_and_after_the_stem(self):
        for marker in mailcases._IDENTITY_MARKERS:
            assert mailcases.extract_keys(f"{marker} paczki 646235") == {"646235": "tracking"}
            assert mailcases.extract_keys(f"paczki {marker} 646235") == {"646235": "tracking"}

    def test_the_markers_are_counted_where_they_are_consulted(self):
        from aish import vocab

        vocab.drain()
        mailcases.extract_keys("numer paczki 646235")
        mailcases.extract_keys("odbioru paczki: 646235")
        tally = vocab.drain()["mailcases._IDENTITY_MARKERS"]
        assert tally["asked"] == 2 and tally["matched"] == 1


class TestStemAlternationsAreARule:
    """Round-4 note 6: the locative/dative alternate a stem's last consonant
    (paczka -> paczce, przesyłka -> przesyłce, faktura -> fakturze). One rule
    table applied to every stem; never a word written per form."""

    def test_the_alternated_forms_introduce_keys(self):
        assert mailcases.extract_keys("Powiadomienie o przesylce 29777837645") == {
            "29777837645": "tracking"}
        assert mailcases.extract_keys("w paczce nr 646235") == {"646235": "tracking"}
        assert mailcases.extract_keys("w fakturze nr FV/2026/41") == {"FV/2026/41": "invoice"}

    def test_every_stem_ending_in_an_alternating_consonant_gets_its_form(self):
        for stem in mailcases._KEY_STEMS:
            for tail, alternate in mailcases._STEM_ALTERNATIONS:
                if stem.endswith(tail):
                    assert mailcases._STEM_FORMS[stem[: -len(tail)] + alternate] == stem

    def test_the_bounded_ending_and_the_date_rule_still_hold(self):
        assert mailcases.extract_keys("Wybrany paczkomat: WAW45H") == {}
        assert mailcases.extract_keys("w przesyłce z dnia 2026-10-10") == {}


class TestTheReobservation:
    """Round-5 note 4: indexed mail is folded through the case store again
    under today's laws, deliberately (`aish-mailwatch --reobserve`)."""

    def index_only(self, state, gid: str, *, at: float, auth: str = "pass"):
        parsed = mailindex.from_gmail(gmail(
            gid, frm="pl.no.reply@dhl.example", subject="Powiadomienie o przesylce 29777837645",
            body="Twoja przesyłka jest w drodze.", at=at, auth=auth), frozenset({OWNER}))
        assert parsed is not None
        mailindex.MailIndex(state).add(parsed.header, parsed.body)
        return parsed.header

    def test_opens_the_case_the_laws_now_find_and_is_idempotent(self, tmp_path):
        from aish import mailwatch

        first = self.index_only(tmp_path, "d1", at=T0)
        second = self.index_only(tmp_path, "d2", at=T0 + DAY)
        done = mailwatch.reobserve(tmp_path)
        assert done.opened == 1 and done.joined == 1
        (case,) = done.new_multi_mail_cases
        assert case["mails"] == 2
        stored = mailcases.CaseStore(tmp_path).cases[case["case"]]
        assert {p[1] for p in stored.mails} == {first.digest, second.digest}
        again = mailwatch.reobserve(tmp_path)
        assert again.opened == 0 and again.joined == 0 and again.new_multi_mail_cases == []
        assert mailcases.CaseStore(tmp_path).cases[case["case"]].mails == stored.mails

    def test_an_unauthenticated_mail_opens_nothing(self, tmp_path):
        from aish import mailwatch

        self.index_only(tmp_path, "d1", at=T0, auth="fail")
        done = mailwatch.reobserve(tmp_path)
        assert done.opened == 0 and mailcases.CaseStore(tmp_path).cases == {}

    def test_the_backlog_fold_admits_by_the_live_law(self, tmp_path):
        """One law for live, backfill and the fold: his forward to his other
        address admits nobody; his reply addressed to the shop admits it."""
        from aish import mailwatch

        def index_all(state, *raws):
            for raw in raws:
                parsed = mailindex.from_gmail(raw, frozenset({OWNER}))
                assert parsed is not None
                mailindex.MailIndex(state).add(parsed.header, parsed.body)

        forward = tmp_path / "forward"
        index_all(forward, store_order(), gmail(
            "f", frm=OWNER, to="bip@wenda.example", subject="Fwd: Order LS-48213 confirmed",
            at=T0 + 60, in_reply_to="s1@lampshop.example", labels=("SENT",)))
        done = mailwatch.reobserve(forward)
        assert done.opened == 1 and done.admitted == []
        (case,) = mailcases.CaseStore(forward).cases.values()
        assert len(case.mails) == 2 and case.admitted == {}

        reply = tmp_path / "reply"
        index_all(reply, store_order(), gmail(
            "r", frm=OWNER, to="orders@lampshop.example", subject="Re: Order LS-48213",
            at=T0 + 60, in_reply_to="s1@lampshop.example", labels=("SENT",)))
        done = mailwatch.reobserve(reply)
        (case,) = mailcases.CaseStore(reply).cases.values()
        assert done.admitted == [f"{case.id}:lampshop.example"]
        assert mailwatch.reobserve(reply).admitted == []


class TestHisBccIsAddressingOnTheCase:
    """Round-7 note 5: the party provenance reads `Header.addressed`."""

    def test_a_party_he_bccd_is_one_he_addressed(self):
        index, store = build(
            store_order(),
            gmail("o", frm=OWNER, to="bip@wenda.example", bcc="desk@lampshop.example",
                  subject="Re: Order LS-48213", at=T0 + 60, labels=("SENT",)),
        )
        (case,) = store.cases.values()
        his = header(index, gmail("o", frm=OWNER, to="bip@wenda.example",
                                  bcc="desk@lampshop.example", subject="Re: Order LS-48213",
                                  at=T0 + 60, labels=("SENT",)))
        assert case.addressed_at("desk@lampshop.example", index) == his.position

    def test_a_legacy_party_is_derived_from_his_bcc(self):
        index, store = build(
            store_order(),
            gmail("o", frm=OWNER, to="bip@wenda.example", bcc="desk@lampshop.example",
                  subject="Re: Order LS-48213", at=T0 + 60, labels=("SENT",)),
        )
        (case,) = store.cases.values()
        legacy = mailcases.Case.from_json(case.as_json())
        legacy.parties["desk@lampshop.example"] = {"at": case.created, "name": ""}
        assert legacy.addressed_at("desk@lampshop.example", index) is not None
