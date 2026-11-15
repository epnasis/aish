"""Bills learned from the bank, and bill notes matched to them
(`aish/paysig.py`, docs/payments.md §Matching bills to payments).

The owner's rule (2026-11-13): the payee account is the bill; a note's words
never identify one. Pinned here: two accounts under one payee name are two
bills, each note landing on its own by the bank's transfer titles; three
accounts of one payee with no notes are three bills, each with its own
expectation; a renamed payee on an unchanged account is one bill; a note whose
words reach two bills is "several bills of this payee"; the new-account mark
never fires on history; the expected-payment timing boundaries; card merchants.

Each rule is pinned by a fixture that would come out differently under the
rival behaviour, the measured traps among them: the neighbouring cycle's
payment is B and never "paid"; an invoice date years back never widens the
window; a payment in the second month after is reachable; a C pairing yields
to a later-read invoice's A claim; a fake invoice naming a new account is
marked and cannot take his bill's payment; a bundle note pairs nothing;
`shared_file` is its own finding. Also kept from the earlier slice: both
ledger eras, the exclusions, the stores, the single locked writer, the CLI's
silence on amounts and accounts, and the report job's call.

Every name, account and amount here is invented. No model, network, vault,
bank or real state is touched.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import sqlite3
import stat
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_mailbank import SCHEMA as LEDGER_SCHEMA
from test_mailbank import run_report
from test_mailreport import NOW as REPORT_NOW
from test_mailreport import fresh_health

from aish import cli, mailbank, mailcases, mailindex, mailitems, mailreport, paynotes, paysig, vocab


def account(n: int) -> str:
    """An invented, IBAN-shaped account."""
    return f"PL{n:02d}" + str(n).rjust(24, "0")


GAS, GAS_NEW, FLAT, GARAGE, SAVINGS = (account(n) for n in (11, 13, 31, 32, 41))
GAS_PAYEE = "GAZOWNIA TESTOWA SA"
EON_PAYEE = "PRĄDEX TESTOWA S.A."
AS_OF = date(2026, 11, 12)


def note(file: str, *, paid: str = "true", paid_to: str | None = "Gazownia",
         due: str | None = None, amount: int | None = None, tags: tuple[str, ...] = (),
         path: str | None = None) -> paynotes.PaymentNote:
    return paynotes.PaymentNote.from_record({
        "path": path or f"Payments/{file}", "file": file, "status": "read", "month": file[:7],
        "paid_to": paid_to, "paid": paid, "date_due": due,
        "amount": {"minor": amount, "currency": "PLN"} if amount is not None else None,
        "tags": list(tags)})


def invoice(of: paynotes.PaymentNote, *, accounts=(), amounts=(), weak=(), dates=(), due=(),
            numbers=(), status="read", shared=False, elsewhere=False, payable=True,
            name="f.pdf") -> paynotes.InvoiceRecord:
    """An invoice record in the collector's shape (`invoicefacts.merge`)."""
    def amount(minor, strong):
        return {"minor": minor, "currency": "PLN", "currency_seen": strong, "count": 1,
                "currency_source": "written" if strong else "document",
                "payable": "line" if payable else None, "ambiguous_grouping": False}
    return paynotes.InvoiceRecord.from_record({
        "path": of.path, "name": name, "status": status, "shared_file": shared,
        "fetched_elsewhere": elsewhere,
        "facts": {"accounts": [{"account": a, "count": 1, "near_keyword": "line"}
                               for a in accounts],
                  "amounts": [amount(m, True) for m in amounts] + [amount(m, False)
                                                                   for m in weak],
                  "dates": [{"date": d, "count": 1, "due": None} for d in dates]
                  + [{"date": d, "count": 1, "due": "line"} for d in due],
                  "invoice_numbers": [{"value": n} for n in numbers]}})


def pay(tx: str, day: str, acct: str | None, *, minor: int = 10000, title: str = "",
        payee: str = GAS_PAYEE) -> paysig.Payment:
    return paysig.Payment(tx, date.fromisoformat(day), "PLN", minor,
                          mailcases.normalize_coordinate(acct) if acct else None,
                          paysig.fold(payee), paysig.fold(title))


def run(notes, invoices, payments, **kw) -> paysig.Matched:
    made = paysig.notes_from(notes, invoices, kw.pop("own", frozenset()))
    kw.setdefault("recorded", {})
    kw.setdefault("mail_spent", set())
    kw.setdefault("reach", AS_OF)
    kw.setdefault("as_of", AS_OF)
    return paysig.match(made, payments, **kw)


def by_month(matched: paysig.Matched) -> dict[str, paysig.NoteResult]:
    return {r.note.month + ("*" if r.note.aish_made else ""): r for r in matched.results}


# ---------------------------------------------------------------- the bill


class TestTheBill:
    def test_a_note_is_its_path_and_its_words_never_its_bill(self):
        made = paysig.notes_from([note("2022-08 Prądex garaż Klonowa 12.md", paid_to="Prądex",
                                       path="Payments/x.md")])[0]
        assert made.id == "Payments/x.md"
        assert made.payee_words == {"pradex"} and made.other_words == {"garaz", "klonowa"}
        twins = paysig.notes_from([note("2026-01 Gazownia.md", tags=("aish",), path="P/a.md"),
                                   note("2026-01 Gazownia.md", path="P/z.md")])
        assert sorted(t.id for t in twins) == ["P/a.md", "P/z.md"]

    def test_the_window_reaches_the_second_month_after(self):
        made = paysig.notes_from([note("2026-06 Gazownia.md")])[0]
        assert made.window() == (date(2026, 4, 1), date(2026, 8, 31))
        made = note("2026-06 Gazownia.md")
        found = run([made], [invoice(made, accounts=[GAS], amounts=[500])],
                    [pay("t", "2026-08-25", GAS, minor=500)])
        assert found.results[0].tx == "t" and found.results[0].finding == paysig.PAID_FOUND

    def test_an_old_invoice_date_never_widens_the_window(self):
        made = note("2026-06 Gazownia.md")
        old = invoice(made, accounts=[GAS], amounts=[500], dates=["2019-01-01"])
        matched = run([made], [old], [pay("t", "2026-02-15", GAS, minor=500)])
        assert matched.results[0].note.window()[0] == date(2026, 4, 1)
        assert not matched.results[0].tx

    def test_a_later_invoice_date_narrows_the_window(self):
        made = note("2026-06 Gazownia.md")
        late = invoice(made, accounts=[GAS], amounts=[500], dates=["2026-05-20"])
        matched = run([made], [late], [pay("t", "2026-05-01", GAS, minor=500)])
        assert matched.results[0].note.window()[0] == date(2026, 5, 20)
        assert not matched.results[0].tx

    def test_a_due_line_sets_the_end_never_before_the_month_after(self):
        made = note("2026-06 Gazownia.md")
        early = paysig.notes_from([made], [invoice(made, due=["2026-06-10"])])[0]
        assert early.window()[1] == date(2026, 7, 31)
        late = paysig.notes_from([made], [invoice(made, due=["2026-07-20"])])[0]
        assert late.window()[1] == date(2026, 8, 19)

    def test_the_reminder_offer_is_stated_honestly(self):
        made = note("2026-06 Gazownia.md")
        one = paysig.notes_from([made], [invoice(made, amounts=[500], due=["2026-06-20"])])[0]
        assert one.reminder == {"to_pay": "one", "due": "due line"}
        several = paysig.notes_from([made], [invoice(made, amounts=[500, 600])])[0]
        assert several.reminder == {"to_pay": "several", "due": "month"}
        weak = paysig.notes_from([made], [invoice(made, weak=[500])])[0]
        assert weak.reminder["to_pay"] == "none read"

    def test_his_own_account_on_an_invoice_is_no_payee(self):
        made = note("2026-06 Gazownia.md")
        matched = run([made], [invoice(made, accounts=[SAVINGS], amounts=[500])],
                      [pay("t", "2026-06-10", SAVINGS, minor=500)], own=frozenset({SAVINGS}))
        assert not matched.results[0].tx
        assert matched.results[0].finding == paysig.PAID_NO_ACCOUNT


# ---------------------------------------------------------------- the evidence


class TestTierA:
    def test_account_and_amount_pair_and_are_recorded_as_new(self):
        made = note("2026-06 Gazownia.md")
        matched = run([made], [invoice(made, accounts=[GAS], amounts=[500])],
                      [pay("t", "2026-06-10", GAS, minor=500)])
        assert matched.results[0].tier == paysig.TIER_A
        assert matched.new_pairs() == [(made.path, "t")]

    def test_a_neighbouring_cycle_payment_is_b_and_never_paid(self):
        june, aug = note("2026-06 Gazownia.md"), note("2026-08 Gazownia.md")
        invoices = [invoice(june, accounts=[GAS], amounts=[500]),
                    invoice(aug, accounts=[GAS], amounts=[700])]
        matched = by_month(run([june, aug], invoices, [pay("t", "2026-07-30", GAS, minor=700)]))
        assert matched["2026-06"].finding == paysig.B_AMOUNT_DIFFERS
        assert not matched["2026-06"].tx
        assert matched["2026-08"].tx == "t"  # B spent nothing

    def test_b_with_no_amount_read_says_so(self):
        made = note("2026-06 Gazownia.md", paid="false")
        matched = run([made], [invoice(made, accounts=[GAS])],
                      [pay("t", "2026-06-10", GAS, minor=500)])
        assert matched.results[0].finding == paysig.B_NO_AMOUNT and not matched.results[0].tx

    def test_a_strong_amount_wins_over_an_earlier_weak_one(self):
        made = note("2026-06 Gazownia.md")
        bill = invoice(made, accounts=[GAS], amounts=[500], weak=[1006])
        matched = run([made], [bill], [pay("w", "2026-05-01", GAS, minor=1006),
                                       pay("s", "2026-06-10", GAS, minor=500)])
        assert matched.results[0].tx == "s" and paysig.WEAK_AMOUNT not in matched.results[0].marks

    def test_a_weak_amount_alone_pairs_and_is_marked(self):
        made = note("2026-06 Gazownia.md")
        matched = run([made], [invoice(made, accounts=[GAS], weak=[1006])],
                      [pay("w", "2026-06-01", GAS, minor=1006)])
        assert matched.results[0].tx == "w" and paysig.WEAK_AMOUNT in matched.results[0].marks

    def test_an_amount_with_no_account_is_never_evidence(self):
        made = note("2026-06 Gazownia.md")
        matched = run([made], [invoice(made, amounts=[500])],
                      [pay("t", "2026-06-10", GAS, minor=500)])
        assert not matched.results[0].tx

    def test_a_unique_claim_goes_first(self):
        jan, mar = note("2026-01 Gazownia.md"), note("2026-03 Gazownia.md")
        invoices = [invoice(jan, accounts=[GAS], amounts=[100, 200]),
                    invoice(mar, accounts=[GAS], amounts=[100])]
        payments = [pay("both", "2026-02-10", GAS, minor=100),
                    pay("jan only", "2026-02-20", GAS, minor=200)]
        matched = by_month(run([jan, mar], invoices, payments))
        # Month order alone would hand January the earliest and leave March empty.
        assert matched["2026-01"].tx == "jan only" and matched["2026-03"].tx == "both"

    def test_his_note_takes_the_payment_before_an_aish_note_of_its_month(self):
        mine = note("2026-06 Gazownia.md", path="Payments/z.md")
        made = note("2026-06 Gazownia.md", tags=("aish",), path="Payments/a.md")
        invoices = [invoice(mine, accounts=[GAS], amounts=[500]),
                    invoice(made, accounts=[GAS], amounts=[500])]
        matched = by_month(run([mine, made], invoices, [pay("t", "2026-06-10", GAS, minor=500)]))
        assert matched["2026-06"].tx == "t" and not matched["2026-06*"].tx

    def test_the_invoice_number_breaks_a_tie(self):
        made = note("2026-06 Gazownia.md")
        bill = invoice(made, accounts=[GAS], amounts=[500], numbers=["FV/77/2026"])
        payments = [pay("a", "2026-06-01", GAS, minor=500, title="oplata"),
                    pay("b", "2026-06-10", GAS, minor=500, title="faktura FV 77/2026")]
        result = run([made], [bill], payments).results[0]
        assert result.tx == "b" and paysig.BY_INVOICE_NUMBER in result.marks

    def test_a_bundle_note_pairs_nothing(self):
        made = note("2026-06 Gazownia.md")
        matched = run([made], [invoice(made, accounts=[GAS], amounts=[500], name="a.pdf"),
                               invoice(made, accounts=[GAS_NEW], amounts=[600], name="b.pdf")],
                      [pay("t", "2026-06-10", GAS, minor=500)])
        assert matched.results[0].finding == paysig.SEVERAL_INVOICES
        assert not matched.results[0].tx

    def test_a_bundled_last_cycle_invoice_never_prices_the_note(self):
        made = note("2026-06 Gazownia.md")
        last = invoice(made, accounts=[GAS], amounts=[700], dates=["2026-04-05"], name="a.pdf")
        this = invoice(made, accounts=[GAS], amounts=[500], dates=["2026-06-05"], name="b.pdf")
        payments = [pay("old", "2026-05-10", GAS, minor=700),
                    pay("new", "2026-06-20", GAS, minor=500)]
        result = run([made], [last, this], payments).results[0]
        assert result.tx == "new" and paysig.OTHER_ATTACHMENTS_IGNORED in result.marks
        only_old = run([made], [last, this], payments[:1]).results[0]
        assert not only_old.tx and only_old.finding == paysig.B_AMOUNT_DIFFERS

    def test_of_equally_near_invoices_the_newest_prices_the_note(self):
        made = note("2026-06 Gazownia.md")
        before = invoice(made, accounts=[GAS], amounts=[700], dates=["2026-06-05"], name="a.pdf")
        after = invoice(made, accounts=[GAS], amounts=[500], dates=["2026-06-25"], name="b.pdf")
        notes = paysig.notes_from([made], [before, after])
        assert {m for _, m in notes[0].invoice.strong} == {500}

    def test_one_invoice_is_priced_whole_and_unmarked(self):
        made = note("2026-06 Gazownia.md")
        result = run([made], [invoice(made, accounts=[GAS], amounts=[500, 700])],
                     [pay("t", "2026-06-20", GAS, minor=700)]).results[0]
        assert result.tx == "t" and paysig.OTHER_ATTACHMENTS_IGNORED not in result.marks

    def test_a_shared_file_is_its_own_finding_and_no_evidence(self):
        made = note("2026-06 Gazownia.md")
        matched = run([made], [invoice(made, accounts=[GAS], amounts=[500], shared=True)],
                      [pay("t", "2026-06-10", GAS, minor=500)])
        assert matched.results[0].finding == paysig.SHARED_FILE and not matched.results[0].tx

    def test_a_file_fetched_elsewhere_is_its_own_finding(self):
        made = note("2026-06 Gazownia.md")
        matched = run([made], [invoice(made, accounts=[GAS], amounts=[500], elsewhere=True)],
                      [pay("t", "2026-06-10", GAS, minor=500)])
        assert matched.results[0].finding == paysig.FETCHED_ELSEWHERE

    def test_an_unread_record_is_no_invoice(self):
        made = note("2026-06 Gazownia.md")
        matched = run([made], [invoice(made, accounts=[GAS], amounts=[500], status="pending")],
                      [pay("t", "2026-06-10", GAS, minor=500)])
        assert matched.results[0].finding == paysig.PAID_NO_INVOICE

    def test_a_bill_after_this_month_pairs_nothing(self):
        made = note("2026-12 Gazownia.md", paid="false")
        matched = run([made], [invoice(made, accounts=[GAS], amounts=[500])],
                      [pay("t", "2026-11-10", GAS, minor=500)])
        assert not matched.results[0].tx

    def test_a_recorded_pair_stands_and_is_never_reassigned(self):
        made = note("2026-06 Gazownia.md")
        bills = [invoice(made, accounts=[GAS], amounts=[500])]
        payments = [pay("t1", "2026-06-10", GAS, minor=500), pay("t2", "2026-06-11", GAS,
                                                                minor=500)]
        kept = run([made], bills, payments, recorded={made.path: "t2"}).results[0]
        assert kept.tx == "t2" and not kept.new
        gone = run([made], bills, payments, recorded={made.path: "vanished"})
        assert not gone.results[0].tx and not gone.new_pairs()

    def test_a_payment_spent_on_a_mail_bill_is_not_available(self):
        made = note("2026-06 Gazownia.md")
        matched = run([made], [invoice(made, accounts=[GAS], amounts=[500])],
                      [pay("t", "2026-06-10", GAS, minor=500)], mail_spent={"t"})
        assert not matched.results[0].tx
        assert matched.results[0].finding == paysig.PAID_ON_MAIL_BILL


# ---------------------------------------------------------------- the bills


TEL_1, TEL_2, TEL_3 = (account(n) for n in (51, 52, 53))
TEL_PAYEE = "TELEFONIA TESTOWA SA"


def paid(acct: str | None, months, *, day: int = 12, minor: int = 10000, title: str = "",
         payee: str = GAS_PAYEE, prefix: str = "p") -> list[paysig.Payment]:
    """One payment per month, each `YYYY-MM`, on `day`."""
    return [pay(f"{prefix}{i}", f"{m}-{day:02d}", acct, minor=minor, title=title, payee=payee)
            for i, m in enumerate(months)]


def months(first: str, count: int, step: int = 1) -> list[str]:
    return [paysig._add_months(first, i * step) for i in range(count)]


def learn(payments, as_of: date = AS_OF, printed=(), reach: date | None = AS_OF):
    return paysig.learn_bills(payments, as_of, printed, reach=reach)


class TestTheBills:
    def test_a_payee_renamed_with_the_account_unchanged_is_one_bill(self):
        payments = (paid(GAS, months("2019-01", 24), payee="STAREX TESTOWA SA", prefix="r")
                    + paid(GAS, months("2021-01", 70), payee=EON_PAYEE, prefix="e"))
        bills = learn(payments)
        assert len(bills) == 1
        bill = next(iter(bills.values()))
        assert [n["name"] for n in bill.names] == ["pradex testowa sa", "starex testowa sa"]
        assert {"starex", "pradex"} <= bill.name_words
        # A note written under the old name finds the same bill, by the bank's names.
        old = note("2019-06 Starex.md", paid_to="Starex")
        result = run([old], [], payments).results[0]
        assert result.route == paysig.ROUTE_NAME and result.tier == paysig.TIER_C

    def test_three_accounts_of_one_payee_with_no_notes_are_three_bills(self):
        payments = [p for i, acct in enumerate((TEL_1, TEL_2, TEL_3))
                    for p in paid(acct, months("2025-01", 22), day=10 + i, payee=TEL_PAYEE,
                                  title=f"{i}0000000000001", prefix=f"t{i}")]
        bills = learn(payments, as_of=date(2026, 11, 1))
        assert len(bills) == 3
        assert {b.account for b in bills.values()} == {TEL_1, TEL_2, TEL_3}
        assert all(b.cadence == paysig.MONTHLY and b.notes == 0 for b in bills.values())
        # Each expects its own payment: paid to October, each by its own day in November.
        assert sorted(b.expectation["expected_by"] for b in bills.values()) == [
            "2026-11-10", "2026-11-11", "2026-11-12"]
        assert {b.expectation["state"] for b in bills.values()} == {paysig.NEXT_EXPECTED}

    def test_two_payments_make_no_bill_unless_an_invoice_prints_the_account(self):
        payments = paid(GAS, ["2026-01", "2026-03"])
        assert learn(payments) == {}
        printed = learn(payments, printed=[GAS])
        assert [b.cadence for b in printed.values()] == [paysig.FEW]

    def test_cadence_classes(self):
        assert paysig.cadence_of([date(2026, m, 5) for m in (1, 2, 3, 4)])[0] == paysig.MONTHLY
        assert paysig.cadence_of([date(2026, m, 5) for m in (1, 3, 5, 7)]) == (
            paysig.BIMONTHLY, 2)
        assert paysig.cadence_of([date(2026, m, 5) for m in (1, 4, 7, 10)])[0] == \
            paysig.QUARTERLY
        assert paysig.cadence_of([date(y, 3, 5) for y in (2023, 2024, 2025)])[0] == \
            paysig.YEARLY
        assert paysig.cadence_of([date(2026, m, 5) for m in (1, 2, 7, 8, 12)])[0] == \
            paysig.IRREGULAR
        assert paysig.cadence_of([date(2026, 1, 5), date(2026, 2, 5)])[0] == paysig.FEW

    def test_the_profile_is_the_last_36_months_of_its_own_payments(self):
        old = paid(GAS, months("2015-01", 12), minor=5000, prefix="o", title="stara umowa")
        new = paid(GAS, months("2023-01", 40, step=1), minor=9000, prefix="n", title="nowa")
        bill = next(iter(learn(old + new).values()))
        assert bill.payments == 36 and bill.amounts == paysig.SAME_AMOUNT
        assert bill.title_words == {"nowa"} and bill.first_month == "2015-01"

    def test_card_merchants_are_bills_only_when_they_look_like_one(self):
        subscription = paid(None, months("2025-06", 15), minor=4999, title="STREAMFLIX COM",
                            payee="", prefix="s")
        # Twice a month, two amounts: as regular as the subscription, by cadence
        # and by amount; only its payments per month tell it apart.
        shop = [pay(f"g{i}", f"{m}-{d:02d}", None, minor=1000 * (1 + d % 2), title="SKLEP "
                    "SPOZYWCZY /WARSZAWA", payee="") for i, (m, d) in enumerate(
                        (m, d) for m in months("2025-06", 15) for d in (3, 18))]
        varied = [pay(f"v{i}", f"{m}-05", None, minor=1000 + i * 37, title="KWIACIARNIA",
                      payee="") for i, m in enumerate(months("2025-06", 15))]
        bills = learn(subscription + shop + varied)
        assert [(b.payee, b.account) for b in bills.values()] == [("streamflix com", None)]
        assert next(iter(bills.values())).to_json()["kind"] == "no account"

    def test_the_bill_id_names_no_account(self):
        bill = next(iter(learn(paid(GAS, months("2026-01", 4))).values()))
        assert GAS not in bill.id and GAS[-10:] not in bill.id


class TestExpectations:
    def bill(self, as_of: date, reach: date | None = None, *, last: str = "2026-10",
             count: int = 8, days=(8, 10, 12, 15)):
        payments = [pay(f"x{i}", f"{m}-{days[i % len(days)]:02d}", GAS)
                    for i, m in enumerate(months(paysig._add_months(last, -(count - 1)), count))]
        return next(iter(learn(payments, as_of=as_of, reach=reach or as_of).values()))

    def test_late_counts_from_the_end_of_the_usual_day_range(self):
        # Paid on days 8-15; October paid; November's payment expected by the 15th.
        assert self.bill(date(2026, 11, 15)).expectation == {
            "state": paysig.NEXT_EXPECTED, "expected_by": "2026-11-15"}
        late = self.bill(date(2026, 11, 16))
        assert late.expectation == {"state": paysig.EXPECTED_NOT_SEEN,
                                    "expected_by": "2026-11-15", "days_late": 1}
        assert paysig.bill_findings({late.id: late})[0]["days_late"] == 1

    def test_the_ledger_reach_bounds_what_is_late(self):
        unsynced = self.bill(date(2026, 11, 20), reach=date(2026, 11, 14))
        assert unsynced.expectation["state"] == paysig.NEXT_EXPECTED
        assert self.bill(date(2026, 11, 20), reach=None).expectation is not None
        nothing = paysig.learn_bills(paid(GAS, months("2026-01", 8)), date(2026, 11, 20),
                                     reach=None)
        assert next(iter(nothing.values())).expectation["state"] == paysig.LEDGER_SHORT

    def test_two_cadence_lengths_without_a_payment_is_ended(self):
        assert self.bill(date(2026, 12, 15)).status == paysig.ACTIVE  # October + 2 months
        ended = self.bill(date(2026, 12, 16))
        assert ended.status == paysig.ENDED
        assert ended.expectation["state"] == paysig.NO_EXPECTATION

    def test_too_few_payments_or_a_long_cadence_expect_nothing(self):
        assert self.bill(date(2026, 11, 20), count=5).expectation["state"] == \
            paysig.NO_EXPECTATION
        yearly = paid(GAS, ["2020-03", "2021-03", "2022-03", "2023-03", "2024-03", "2025-03",
                            "2026-03"])
        bill = next(iter(learn(yearly, as_of=date(2026, 11, 20)).values()))
        assert bill.cadence == paysig.YEARLY
        assert bill.expectation["state"] == paysig.NO_EXPECTATION


# ---------------------------------------------------------------- tier C


FLAT_TITLE, GARAGE_TITLE = "oplata lokal Klonowa", "oplata garaz Klonowa"


def flat_and_garage(first: str = "2025-01", count: int = 23) -> list[paysig.Payment]:
    """Two accounts under one payee name, each with its own transfer title."""
    return (paid(FLAT, months(first, count, 2), title=FLAT_TITLE, payee=EON_PAYEE, prefix="f")
            + paid(GARAGE, months(first, count, 2), day=14, minor=3000, title=GARAGE_TITLE,
                   payee=EON_PAYEE, prefix="g"))


class TestTierC:
    def test_flat_and_garage_land_each_on_their_own_bill(self):
        payments = flat_and_garage()
        garage = note("2026-05 Prądex Garaż Klonowa.md", paid_to="Prądex", path="P/g.md")
        flat = note("2026-05 Prądex lokal.md", paid_to="Prądex", path="P/f.md")
        matched = run([garage, flat], [], payments)
        bills = matched.bills
        took = {r.note.id: r for r in matched.results}
        assert bills[took["P/g.md"].bill].account == GARAGE
        assert bills[took["P/f.md"].bill].account == FLAT
        assert {r.route for r in took.values()} == {paysig.ROUTE_TITLE}
        assert {r.tier for r in took.values()} == {paysig.TIER_C}
        assert by_id(payments)[took["P/g.md"].tx].account == GARAGE

    def test_a_note_whose_words_reach_both_titles_is_several_bills(self):
        street = note("2026-05 Prądex Klonowa.md", paid_to="Prądex")
        result = run([street], [], flat_and_garage()).results[0]
        assert result.route == paysig.ROUTE_SEVERAL and result.candidates == 2
        assert result.finding == paysig.SEVERAL_BILLS and not result.tx

    def test_one_bill_of_the_payee_is_found_by_name_alone(self):
        payments = paid(GAS, months("2026-01", 8))
        made = note("2026-05 Gazownia cokolwiek.md")
        result = run([made], [], payments).results[0]
        assert result.route == paysig.ROUTE_NAME and result.tx == "p4"

    def test_only_bills_paid_in_the_window_are_candidates(self):
        payments = (paid(FLAT, months("2019-01", 12), payee=EON_PAYEE, prefix="o")
                    + paid(GARAGE, months("2025-01", 12), payee=EON_PAYEE, prefix="n"))
        result = run([note("2025-06 Prądex.md", paid_to="Prądex")], [], payments).results[0]
        assert result.route == paysig.ROUTE_NAME
        assert by_id(payments)[result.tx].account == GARAGE

    def test_no_bill_of_the_payee_and_no_payee(self):
        payments = paid(GAS, months("2026-01", 8))
        other = run([note("2026-05 Admin.md", paid_to="Administrator")], [], payments)
        assert other.results[0].route == paysig.ROUTE_NO_BILL
        assert other.results[0].finding == paysig.PAID_NO_INVOICE
        nobody = run([note("2026-05 Gazownia.md", paid_to=None)], [], payments)
        assert nobody.results[0].route == paysig.ROUTE_NO_PAYEE

    def test_another_notes_name_never_decides(self):
        """A note spelled like an invoice-matched note of the flat is not sent
        to the flat by that spelling: only the bank's titles decide."""
        payments = flat_and_garage()
        twin = note("2026-03 Prądex Klonowa.md", paid_to="Prądex", path="P/inv.md")
        bare = note("2026-05 Prądex Klonowa.md", paid_to="Prądex", path="P/bare.md")
        matched = run([twin, bare], [invoice(twin, accounts=[FLAT], amounts=[10000])], payments)
        took = {r.note.id: r for r in matched.results}
        assert took["P/inv.md"].tier == paysig.TIER_A
        assert took["P/bare.md"].route == paysig.ROUTE_SEVERAL

    def test_c_is_never_recorded_and_yields_to_a_later_read_invoice(self):
        payments = [*paid(GAS, months("2026-01", 4), prefix="h"),
                    pay("p", "2026-06-20", GAS, minor=300)]
        may, jul = note("2026-05 Gazownia.md", path="P/may.md"), note("2026-07 Gazownia.md",
                                                                       path="P/jul.md")
        before = run([may, jul], [], payments)
        took = {r.note.id: r for r in before.results}
        assert took["P/may.md"].tier == paysig.TIER_C
        assert not before.new_pairs()
        after = run([may, jul], [invoice(jul, accounts=[GAS], amounts=[300])], payments)
        took = {r.note.id: r for r in after.results}
        assert took["P/jul.md"].tx == "p" and took["P/jul.md"].tier == paysig.TIER_A
        assert took["P/may.md"].tx != "p"

    def test_an_unpaid_note_is_attached_through_its_bill(self):
        payments = paid(GAS, months("2026-01", 10))
        result = run([note("2026-10 Gazownia.md", paid="false")], [], payments).results[0]
        assert result.tier == paysig.TIER_C and result.finding == paysig.UNPAID_FOUND

    def test_a_card_merchant_bill_takes_a_note(self):
        payments = paid(None, months("2026-01", 10), minor=4999, title="STREAMFLIX COM",
                        payee="")
        result = run([note("2026-06 Streamflix.md", paid_to="Streamflix")], [],
                     payments).results[0]
        assert result.route == paysig.ROUTE_NAME and result.tier == paysig.TIER_C

    def test_notes_attach_to_bills_with_their_spellings(self):
        payments = paid(GAS, months("2026-01", 8))
        matched = run([note("2026-03 Gazownia Lipowa 7.md"),
                       note("2026-04 Gazownia prad.md")], [], payments)
        bill = next(iter(matched.bills.values()))
        assert bill.notes == 2
        assert sorted(bill.spellings) == ["Gazownia · Lipowa", "Gazownia · prad"]


def by_id(payments) -> dict[str, paysig.Payment]:
    return {p.id: p for p in payments}


# ---------------------------------------------------------------- findings and marks


class TestFindings:
    def test_a_new_account_is_raised_for_a_current_unpaid_note(self):
        payments = paid(GAS, months("2026-01", 10))
        due = note("2026-11 Gazownia.md", paid="false")
        result = run([due], [invoice(due, accounts=[GAS_NEW], amounts=[500])],
                     payments).results[0]
        assert paysig.NEW_ACCOUNT in result.marks
        assert result.observations == {
            "the payee this provider is usually paid to": "gazownia testowa sa"}

    def test_the_new_account_is_never_raised_on_history(self):
        payments = paid(GAS, months("2026-01", 10))
        for kw in ({"paid": "true"}, {"paid": "false", "month": "2026-10"}):
            made = note(f"{kw.get('month', '2026-11')} Gazownia.md", paid=kw["paid"])
            result = run([made], [invoice(made, accounts=[GAS_NEW], amounts=[500])],
                         payments).results[0]
            assert paysig.NEW_ACCOUNT not in result.marks

    def test_no_new_account_when_the_account_is_a_bill_of_the_payee_or_none_is_known(self):
        payments = paid(GAS, months("2026-01", 10))
        due = note("2026-11 Gazownia.md", paid="false")
        known = run([due], [invoice(due, accounts=[GAS], amounts=[500])], payments)
        assert paysig.NEW_ACCOUNT not in known.results[0].marks
        nothing = run([due], [invoice(due, accounts=[GAS_NEW], amounts=[500])], [])
        assert paysig.NEW_ACCOUNT not in nothing.results[0].marks

    def test_a_fake_invoice_naming_a_new_account_takes_nothing(self):
        payments = [*paid(GAS, months("2026-01", 10)), pay("t", "2026-11-05", GAS, minor=500)]
        real = note("2026-11 Gazownia.md", paid="false", path="P/real.md")
        fake = note("2026-11 Gazownia.md", paid="false", tags=("aish",), path="P/fake.md")
        matched = run([real, fake], [invoice(real, accounts=[GAS], amounts=[500]),
                                     invoice(fake, accounts=[GAS_NEW], amounts=[500])],
                      payments)
        took = {r.note.id: r for r in matched.results}
        assert took["P/real.md"].tx == "t" and not took["P/fake.md"].tx
        assert paysig.NEW_ACCOUNT in took["P/fake.md"].marks

    def test_an_aish_note_paired_beside_his_own_is_marked(self):
        mine = note("2026-06 Gazownia.md")
        made = note("2026-07 Gazownia.md", tags=("aish",))
        invoices = [invoice(mine, accounts=[GAS], amounts=[500]),
                    invoice(made, accounts=[GAS], amounts=[600])]
        matched = by_month(run([mine, made], invoices,
                               [*paid(GAS, ["2026-01", "2026-02"], prefix="h"),
                                pay("a", "2026-06-10", GAS, minor=500),
                                pay("b", "2026-07-10", GAS, minor=600)]))
        assert paysig.AISH_BESIDE_OWNER in matched["2026-07*"].marks

    def test_every_finding_is_reachable(self):
        found = set()

        def one(made, records=(), extra=(), reach=AS_OF, base=True, **kw):
            payments = [*(paid(GAS, months("2026-01", 3), prefix="h") if base else ()), *extra]
            matched = run([made], list(records), payments, reach=reach, **kw)
            found.add(matched.results[0].finding)

        with_inv = note("2026-06 Gazownia.md")
        one(with_inv, [invoice(with_inv, accounts=[GAS], amounts=[5])],
            [pay("t", "2026-06-10", GAS, minor=5)])
        unpaid = note("2026-06 Gazownia.md", paid="false")
        one(unpaid, [invoice(unpaid, accounts=[GAS], amounts=[5])],
            [pay("t", "2026-06-10", GAS, minor=5)])
        one(with_inv, [invoice(with_inv, accounts=[GAS], amounts=[5])],
            [pay("t", "2026-06-10", GAS, minor=6)])
        one(with_inv, [invoice(with_inv, accounts=[GAS])], [pay("t", "2026-06-10", GAS)])
        one(with_inv, [invoice(with_inv, accounts=[GAS], name="a"),
                       invoice(with_inv, accounts=[GAS_NEW], name="b")])
        nobody = note("2026-06 Nikt.md", paid_to="Nikt")
        one(nobody)
        one(nobody, [invoice(nobody, amounts=[5])])
        one(with_inv, [invoice(with_inv, accounts=[GAS], amounts=[5])])
        one(with_inv, [invoice(with_inv, accounts=[GAS], amounts=[5])],
            [pay("t", "2026-06-10", GAS, minor=5)], mail_spent={"t"})
        one(note("2026-11 Gazownia.md", paid="false", due="2026-11-25"), base=False)
        one(note("2026-08 Gazownia.md", paid="false", due="2026-08-10"), base=False)
        one(note("2026-11 Gazownia.md", paid="false"), base=False)
        one(note("2026-08 Gazownia.md", paid="false"), base=False)
        one(note("2026-06 Gazownia.md", paid="unknown"))
        one(note("2026-10 Gazownia.md"), reach=date(2026, 10, 1))
        one(with_inv, [invoice(with_inv, accounts=[GAS], shared=True)], base=False)
        one(with_inv, [invoice(with_inv, accounts=[GAS], elsewhere=True)], base=False)
        one(note("2026-05 Prądex Klonowa.md", paid_to="Prądex"), extra=flat_and_garage(),
            base=False)
        assert set(paysig.FINDINGS) - found == set()


# ---------------------------------------------------------------- the backtest


class TestTheBacktest:
    def test_bills_are_learned_before_the_cut_and_notes_matched_from_it(self):
        payments = [p for p in paid(GAS, months("2026-01", 10)) if p.booked.month != 7]
        payments.append(pay("jul", "2026-07-12", GAS, minor=777))
        jul = note("2026-07 Gazownia.md", path="P/jul.md")
        sep = note("2026-09 Gazownia.md", path="P/sep.md")
        notes = paysig.notes_from([jul, sep], [invoice(jul, accounts=[GAS], amounts=[777])])
        result = paysig.backtest(notes, payments, "2026-05", AS_OF)
        assert result.bills == 1
        assert result.paid == {"A": 1, "C": 1}
        assert result.routes == {paysig.ROUTE_NAME: 1}
        assert result.c_without_invoice == 1 and result.without_invoice == 1
        assert result.c_agrees == 1

    def test_a_bill_first_paid_after_the_cut_is_unknown_to_it(self):
        payments = paid(GAS, months("2026-06", 5))
        sep = note("2026-09 Gazownia.md")
        result = paysig.backtest(paysig.notes_from([sep]), payments, "2026-05", AS_OF)
        assert result.bills == 0 and result.paid == {"none": 1}
        assert result.routes == {paysig.ROUTE_NO_BILL: 1}


# ---------------------------------------------------------------- the bank side


def import_tx(tx: str, *, number: str | None, payee: str = "", title: str = "",
              phrase: str = "PRZELEW ZEWNĘTRZNY WYCHODZĄCY") -> mailbank.Transaction:
    raw = {"Data księgowania": "2025-03-03", "Kwota": "-10,00", "Opis operacji": phrase,
           "Tytuł": title, "Nadawca/Odbiorca": payee}
    if number is not None:
        raw["Numer konta"] = number
    return mailbank.Transaction(tx, "2025-03-03", "PLN", 1000, "", "",
                                mailbank._creditor_accounts(json.dumps(raw)), raw=json.dumps(raw))


def api_tx(tx: str, *, acct: str | None, payee: str = "", lines: list[str] | None = None,
           via: str = "other") -> mailbank.Transaction:
    raw: dict = {"creditor_account": None, "creditor": None, "bank_transaction_code": None,
                 "remittance_information": lines or []}
    if acct:
        raw["creditor_account"] = ({"iban": None, "other": {"identification": acct}}
                                   if via == "other" else {"iban": acct, "other": None})
        raw["creditor"] = {"name": payee}
    return mailbank.Transaction(tx, "2025-10-03", "PLN", 1000, payee, " ".join(lines or []),
                                mailbank._creditor_accounts(json.dumps(raw)), raw=json.dumps(raw))


class TestTheBankSide:
    def test_the_import_era_account_is_under_numer_konta(self):
        tx = import_tx("a|1", number=GAS[2:], payee="Gazownia Testowa", title="Gaz Lipowa")
        # The mail bills' reader finds no creditor account in this era at all.
        assert tx.creditor_accounts == ()
        payment, era = paysig.normalise(tx, frozenset())
        assert era == "import" and payment is not None
        assert payment.account == GAS
        assert payment.payee == "gazownia testowa" and payment.title == "gaz lipowa"

    def test_the_api_era_account_is_the_creditor_account_and_the_phrase_is_not_title(self):
        for via in ("other", "iban"):
            tx = api_tx("a|2", acct=GAS, payee=EON_PAYEE, via=via,
                        lines=["Oplata Lipowa", "PRZELEW ZEWNĘTRZNY WYCHODZĄCY"])
            payment, era = paysig.normalise(tx, frozenset())
            assert era == "api" and payment is not None
            assert payment.account == GAS
            assert payment.payee == "pradex testowa sa"
            assert payment.title == "oplata lipowa"

    def test_a_card_payment_has_no_account(self):
        payment, _ = paysig.normalise(api_tx("a|3", acct=None, lines=["SKLEP", "ZAKUP PRZY "
                                                                      "UŻYCIU KARTY"]),
                                      frozenset())
        assert payment is not None and payment.account is None

    def test_savings_and_own_transfers_are_not_payments(self):
        vocab.reset()
        own = frozenset({SAVINGS})
        cases = [
            import_tx("i|1", number=None, phrase="PRZELEW NA TWOJE CELE"),
            import_tx("i|2", number=GAS[2:], phrase="PRZELEW WŁASNY"),
            api_tx("a|4", acct=GAS, lines=["na wakacje", "PRZELEW WŁASNY"]),
            api_tx("a|5", acct=SAVINGS, lines=["odklad", "PRZELEW WEWNĘTRZNY WYCHODZĄCY"]),
        ]
        whys = [paysig.normalise(tx, own)[1] for tx in cases]
        assert whys == ["own_phrase", "own_phrase", "own_phrase", "own_account"]
        counted = vocab.drain()["paysig._OWN_TRANSFER_PHRASES"]
        assert counted == {"asked": 3, "matched": 3}

    def test_duplicate_date_amount_account_rows_are_distinct(self):
        ledger = mailbank.Ledger(readable=True, transactions=[
            api_tx("a|7", acct=GAS, lines=["x", "PRZELEW ZEWNĘTRZNY WYCHODZĄCY"]),
            api_tx("a|8", acct=GAS, lines=["x", "PRZELEW ZEWNĘTRZNY WYCHODZĄCY"])])
        side = paysig.read_payments(ledger)
        assert [p.id for p in side.payments] == ["a|7", "a|8"]

    def test_the_ledger_is_read_through_mailbanks_one_opening(self, tmp_path):
        ledger_db(tmp_path, [("t1", "2026-10-10", 1000, api_raw(GAS, GAS_PAYEE, "gaz"))],
                  own=[SAVINGS])
        plain = mailbank.read_ledger(tmp_path, NOW_DT.timestamp())
        kept = mailbank.read_ledger(tmp_path, NOW_DT.timestamp(), keep_raw=True)
        assert plain.transactions[0].raw == "" and kept.transactions[0].raw
        assert kept.own_accounts == {SAVINGS}


# ---------------------------------------------------------------- the learner




# ---------------------------------------------------------------- the stores and the run


NOW_DT = dt.datetime(2026, 11, 12, 7, 0)


def api_raw(acct: str, payee: str, title: str) -> dict:
    return {"creditor_account": {"iban": None, "other": {"identification": acct}},
            "creditor": {"name": payee},
            "remittance_information": [title, "PRZELEW ZEWNĘTRZNY WYCHODZĄCY"]}


def ledger_db(state: Path, rows, *, synced_at: float | None = None, own=()) -> None:
    path = mailbank.ledger_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(LEDGER_SCHEMA)
    conn.execute("INSERT INTO accounts(alias, session_uid, iban, raw, linked_at) "
                 "VALUES ('a1', 's', ?, '{}', 'x')", (own[0] if own else None,))
    conn.execute("INSERT INTO meta VALUES ('last_sync_at:a1', ?)",
                 (str(int(synced_at if synced_at is not None else NOW_DT.timestamp() - 3600)),))
    for entry, day, cents, raw in rows:
        conn.execute("INSERT INTO transactions VALUES ('a1', ?, ?, ?, ?, ?, 'PLN', 'out', '', '', "
                     "'BOOK', ?, '', 'x')", (entry, day, day, f"{cents / 100:.2f}",
                                             -cents / 100, json.dumps(raw)))
    conn.commit()
    conn.close()


def notes_index(state: Path, notes: list[dict], *, written: dt.datetime = NOW_DT,
                invoices_written: dt.datetime | None = None) -> None:
    """The notes index and the invoice facts, each with its digest; every
    note carries one invoice printing GAS and 123,45."""
    records = [{"status": "read", "path": f"Payments/{n['file']}", "month": n["file"][:7],
                "paid_to": "Gazownia", "paid": "true", "tags": [], **n} for n in notes]
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
    facts = [invoice(paynotes.PaymentNote.from_record(r), accounts=[GAS], amounts=[12345]).raw
             for r in records]
    facts_text = "".join(json.dumps(f, sort_keys=True) + "\n" for f in facts)
    folder = paynotes.payments_dir(state)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.jsonl").write_text(text)
    (folder / "invoices.jsonl").write_text(facts_text)
    (folder / "index.json").write_text(json.dumps({
        "outcome": "ok", "notes_written_at": written.isoformat(timespec="seconds"),
        "notes_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "invoices": {"outcome": "ok",
                     "written_at": (invoices_written or written).isoformat(timespec="seconds"),
                     "invoices_sha256": hashlib.sha256(facts_text.encode()).hexdigest()}}))


def a_learnable_tree(state: Path, *, synced_at: float | None = None,
                     written: dt.datetime = NOW_DT,
                     invoices_written: dt.datetime | None = None) -> None:
    notes_index(state, [{"file": f"2026-{m:02d} Gazownia.md"} for m in (5, 7, 9, 11)],
                written=written, invoices_written=invoices_written)
    ledger_db(state, [(f"e{m}", f"2026-{m:02d}-10", 12345, api_raw(GAS, GAS_PAYEE, "gaz"))
                      for m in (5, 7, 9, 11)], synced_at=synced_at)


class TestTheStoresAndTheWriter:
    def test_learn_and_record_writes_both_stores_0600(self, tmp_path):
        a_learnable_tree(tmp_path)
        outcome = paysig.learn_and_record(tmp_path, NOW_DT)
        assert outcome["outcome"] == "ok" and outcome["pairs_recorded"] == 4
        for path in (paysig.signatures_path(tmp_path), paysig.pairs_path(tmp_path)):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not [p for p in paysig.signatures_path(tmp_path).parent.iterdir()
                    if p.name.startswith(".")]  # no scratch file left behind
        assert stat.S_IMODE(paysig.bills_path(tmp_path).stat().st_mode) == 0o600
        store = paysig.read_store(tmp_path)
        assert store["checked_to"] == NOW_DT.date().isoformat()
        may = store["notes"]["Payments/2026-05 Gazownia.md"]
        assert may["reminder"] == {"to_pay": "one", "due": "month"}
        assert may["route"] == paysig.ROUTE_INVOICE and may["tier"] == paysig.TIER_A
        bills = paysig.read_bills(tmp_path)
        assert len(bills["bills"]) == 1 and may["bill"] in bills["bills"]
        bill = bills["bills"][may["bill"]]
        assert bill["account"] == GAS and bill["notes"] == 4
        assert bill["cadence"] == paysig.BIMONTHLY and bills["findings"] == []
        assert len(paysig.read_pairs(tmp_path)) == 4
        lines = [json.loads(x) for x in paysig.pairs_path(tmp_path).read_text().splitlines()]
        assert {x["tier"] for x in lines} == {"A"} and all("checked_to" in x for x in lines)
        # A second run records nothing twice.
        assert paysig.learn_and_record(tmp_path, NOW_DT)["pairs_recorded"] == 0
        assert len(paysig.pairs_path(tmp_path).read_text().splitlines()) == 4

    def test_a_stale_ledger_learns_but_pairs_nothing(self, tmp_path):
        a_learnable_tree(tmp_path, synced_at=NOW_DT.timestamp() - 5 * 86400)
        outcome = paysig.learn_and_record(tmp_path, NOW_DT)
        assert outcome["outcome"] == "could not check" and outcome["pairs_recorded"] == 0
        assert not paysig.pairs_path(tmp_path).exists()

    @pytest.mark.parametrize("old", ["notes", "invoices"])
    def test_a_stale_index_or_invoice_facts_pair_nothing(self, tmp_path, old):
        three_days_ago = NOW_DT - dt.timedelta(days=3)
        if old == "notes":
            a_learnable_tree(tmp_path, written=three_days_ago, invoices_written=NOW_DT)
        else:
            a_learnable_tree(tmp_path, invoices_written=three_days_ago)
        outcome = paysig.learn_and_record(tmp_path, NOW_DT)
        assert outcome["outcome"] == "could not check" and "older than" in outcome["reason"]
        assert not paysig.pairs_path(tmp_path).exists()

    @pytest.mark.parametrize("broken", ["index", "invoices", "ledger"])
    def test_an_unreadable_input_is_could_not_check_and_writes_nothing(self, tmp_path, broken):
        a_learnable_tree(tmp_path)
        if broken in ("index", "invoices"):
            name = "notes.jsonl" if broken == "index" else "invoices.jsonl"
            (paynotes.payments_dir(tmp_path) / name).write_text("changed\n")
        else:
            mailbank.ledger_path(tmp_path).write_bytes(b"not a database")
        outcome = paysig.learn_and_record(tmp_path, NOW_DT)
        assert outcome["outcome"] == "could not check"
        assert not paysig.signatures_path(tmp_path).exists()
        assert not paysig.pairs_path(tmp_path).exists()
        assert "could not check" in paysig.summary(tmp_path, NOW_DT)

    def test_only_the_lock_holder_writes(self, tmp_path):
        a_learnable_tree(tmp_path)
        lock = mailindex_lock(tmp_path)
        with open(lock, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            outcome = paysig.learn_and_record(tmp_path, NOW_DT, lock_wait_s=0)
        assert outcome["outcome"] == "lock held"
        assert not paysig.signatures_path(tmp_path).exists()

    def test_display_and_backtest_write_nothing(self, tmp_path):
        a_learnable_tree(tmp_path)
        before = sorted(p.name for p in paynotes.payments_dir(tmp_path).iterdir())
        paysig.summary(tmp_path, NOW_DT)
        paysig.backtest_summary(tmp_path, NOW_DT, "2026-09")
        assert sorted(p.name for p in paynotes.payments_dir(tmp_path).iterdir()) == before

    def test_the_mail_bills_never_take_a_payment_a_note_holds(self, tmp_path, monkeypatch):
        a_learnable_tree(tmp_path)
        paysig.learn_and_record(tmp_path, NOW_DT)
        conn = sqlite3.connect(mailbank.ledger_path(tmp_path))
        conn.execute("INSERT INTO transactions VALUES ('a1', 'later', '2026-11-11', '2026-11-11', "
                     "'1.00', -1.0, 'PLN', 'out', '', '', 'BOOK', '{}', '', 'x')")
        conn.commit()
        conn.close()
        bank = mailbank.Bank(tmp_path, NOW_DT.timestamp())
        held = set(paysig.read_pairs(tmp_path).values())
        assert len(held) == 4 and bank.note_spent == held
        item = SimpleNamespace(key="mail-item", anchor="m", anchor_read=0.0)
        ctx = SimpleNamespace(bank=bank, bank_new_pairs=True)
        # Every transaction matches this item: only spending decides.
        monkeypatch.setattr(mailbank, "match", lambda *_a: mailbank.TIE_STRONG)
        paired = mailbank.assign(ctx, [(item, 0.0)])
        assert paired["mail-item"].tx.id == "a1|later"


def mailindex_lock(state: Path) -> Path:
    path = mailindex.mail_dir(state) / mailitems.LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestTheCommand:
    @pytest.mark.parametrize("args", [[], ["--backtest", "2026-11"], ["--learn"]])
    def test_no_amount_and_no_account_number_is_printed(self, tmp_path, monkeypatch, capsys,
                                                        args):
        a_learnable_tree(tmp_path)
        monkeypatch.setattr(cli, "state_home", lambda: tmp_path)
        cli._paysig_cli(args)
        said = capsys.readouterr().out
        assert said.strip()
        for secret in (GAS, GAS[2:], GAS[-10:], "123,45", "123.45", "12345"):
            assert secret not in said
        if not args:
            assert "gazownia testowa" in said  # the payee, as the bank writes it
        elif args[0] == "--backtest":
            assert "tier A 1" in said

    def test_a_bad_argument_prints_the_usage(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "state_home", lambda: tmp_path)
        assert cli._paysig_cli(["--backtest", "2026-13"]) == 2
        assert "usage" in capsys.readouterr().out


class TestTheReportJob:
    def test_it_learns_after_the_bank_sync(self, tmp_path, monkeypatch):
        fresh_health(tmp_path)
        order = []

        def sync(_limit):
            order.append("sync")
            return mailbank.SyncOutcome(mailbank.SYNC_OK)

        def learn(state, now, **_kw):
            order.append("learn")
            return {"outcome": "ok", "reason": "", "pairs_recorded": 0}

        monkeypatch.setattr(paysig, "learn_and_record", learn)
        run_report(tmp_path, sync)
        assert order == ["sync", "learn"]
        assert mailreport.read_ledger(tmp_path)[-1]["paysig"]["outcome"] == "ok"

    def test_a_failing_learner_never_breaks_the_report(self, tmp_path, monkeypatch):
        fresh_health(tmp_path)

        def boom(*_a, **_kw):
            raise RuntimeError("a note name could be in here")

        monkeypatch.setattr(paysig, "learn_and_record", boom)
        post = run_report(tmp_path, lambda _limit: mailbank.SyncOutcome(mailbank.SYNC_OK))
        assert post.calls
        entry = mailreport.read_ledger(tmp_path)[-1]
        assert entry["paysig"] == {"outcome": "error", "reason": "RuntimeError"}

    def test_a_dry_run_does_not_learn(self, tmp_path, monkeypatch):
        fresh_health(tmp_path)
        monkeypatch.setattr(paysig, "learn_and_record",
                            lambda *_a, **_k: pytest.fail("learned on a dry run"))
        run_report(tmp_path, None, dry_run=True, now=REPORT_NOW)
