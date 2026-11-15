"""Bills learned from the bank; bill notes matched to their payments.

The owner, 2026-11-13: *"Account or org name is to be matched, not names in
Obsidian. The garage and the flat have different accounts and transfer
titles. A telecom has three accounts and three contracts."* So **the payee account is the bill**. A
note's wording never identifies one, and a bill exists whether or not a note
does: every bill is learned by code from the bank ledger alone (`learn_bills`).

A note attaches to a bill, and its payment is found, by evidence strongest
first — all code, no model, the same inputs give the same answers:

- **A** — a payment to an account the invoice prints, of an amount it prints.
  The only tier written to the pairing ledger.
- **B** — a payment to the invoice's account, no amount agreeing. A FINDING,
  never "paid": measured, it is most often the neighbouring cycle's payment.
  It spends nothing.
- **C** — for a note with no usable invoice account: the one bill whose payee
  names (over time) carry the note's `paid_to` words; among several bills of
  that payee, the one whose recurring TRANSFER TITLE carries the note's other
  words. Recomputed every run, so it yields to any later A claim.

One transaction pays one bill across this ledger and the mail bills'
(`mailbank`). Only `learn_and_record` writes, under the item acts' lock: the
report job after its bank sync, and `aish paysig --learn`. Every finding is
an observation; none states a cause. `docs/payments.md` §Matching bills to payments.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import mailbank, mailcases, mailreport, paynotes, vocab

SCHEMA = 3
SIGNATURES_NAME = "signatures.json"  # each note's outcome
BILLS_NAME = "bills.json"  # the bills learned from the bank
PAIRS_NAME = "note-pairs.jsonl"

#: The import era cuts names at about 19 characters; both eras compare this.
PAYEE_PREFIX_CHARS = 19
#: The notes index and the invoice facts older than this are "could not
#: check" (both refresh daily).
INPUT_FRESH_DAYS = 2
#: A due date read on an invoice reaches this far: real payments land after it.
DUE_GRACE_DAYS = 30
#: An invoice-number candidate shorter than this is no tie-breaker.
INVOICE_NUMBER_MIN = 4

#: A bill's profile is read from its own last 36 months of payments.
BILL_HISTORY_MONTHS = 36
#: Payments an account needs to be a bill without a note's invoice naming it.
BILL_MIN_PAYMENTS = 3
#: A title token is the bill's when it recurs in this share of its titles.
TITLE_TOKEN_SHARE = 0.5
#: A cadence holds when this share of the gaps between payment months sits
#: at the median gap.
CADENCE_SHARE = 0.6
#: A bill is expected each cycle once it has this many payments.
EXPECT_MIN_PAYMENTS = 6
#: A card merchant is a bill only at about one payment per month it is paid
#: in: measured, a shop visited every month is otherwise as "regular" as a
#: subscription.
CARD_MAX_PER_MONTH = 1.3
#: A bill with no cadence (irregular, or too few payments) has ended? when
#: its last payment is older than this — a stated choice, not measured.
UNCADENCED_ENDED_MONTHS = 12

TIER_A = "A"
TIER_C = "C"

# Cadences, by the median gap in months between payment months.
MONTHLY = "monthly"
BIMONTHLY = "every 2 months"
QUARTERLY = "quarterly"
HALF_YEARLY = "half-yearly"
YEARLY = "yearly"
IRREGULAR = "irregular"
FEW = "too few payments"
EXPECTED_CADENCES = (MONTHLY, BIMONTHLY, QUARTERLY)

# Amount profiles.
SAME_AMOUNT = "always the same"
FEW_AMOUNTS = "a few values"
VARIES = "varies"

# A bill's status.
ACTIVE = "active"
ENDED = "ended?"
EXPECTED_NOT_SEEN = "expected, not yet seen"
NEXT_EXPECTED = "next payment not yet due"
NO_EXPECTATION = "no expectation"

# How a note reached its bill.
ROUTE_INVOICE = "invoice account"
ROUTE_NAME = "payee name, one bill"
ROUTE_TITLE = "payee name and transfer title"
ROUTE_SEVERAL = "several bills of this payee"
ROUTE_NO_BILL = "no bill of this payee"
ROUTE_NO_PAYEE = "the note names no payee"
ROUTES = (ROUTE_INVOICE, ROUTE_NAME, ROUTE_TITLE, ROUTE_SEVERAL, ROUTE_NO_BILL, ROUTE_NO_PAYEE)

# The findings — one per note, all observations (docs/payments.md).
PAID_FOUND = "note says paid, payment found"
UNPAID_FOUND = "note says unpaid, payment found (the note is behind)"
B_AMOUNT_DIFFERS = "a payment to the invoice's account exists in the window, amount differs"
B_NO_AMOUNT = "a payment to the invoice's account exists in the window, no amount read"
SEVERAL_INVOICES = "several invoices on one note"
SEVERAL_BILLS = ROUTE_SEVERAL
PAID_NO_INVOICE = "note says paid, no payment found: no invoice read yet, no bill of this payee"
PAID_NO_ACCOUNT = ("note says paid, no payment found: the invoice shows no account, no bill "
                   "of this payee")
PAID_NOT_IN_WINDOW = "note says paid, no payment found: bill known, no payment in the window"
PAID_ON_MAIL_BILL = "note says paid, and its payment is already paired to a mail bill"
UNPAID_DUE_AHEAD = "note says unpaid, no payment, due date ahead"
UNPAID_DUE_PASSED = "note says unpaid, no payment, due date passed"
UNPAID_MONTH_AHEAD = "note says unpaid, no payment, no due date known, month ahead"
UNPAID_MONTH_PASSED = "note says unpaid, no payment, no due date known, month passed"
PAID_UNCLEAR = "the note's paid field is unknown or contradictory"
LEDGER_SHORT = "the bank ledger does not reach this month"
SHARED_FILE = "one document attached to several notes"
FETCHED_ELSEWHERE = "the invoice was found outside the note's own folders"
FINDINGS = (PAID_FOUND, UNPAID_FOUND, B_AMOUNT_DIFFERS, B_NO_AMOUNT, SEVERAL_INVOICES,
            SEVERAL_BILLS, PAID_NO_INVOICE, PAID_NO_ACCOUNT, PAID_NOT_IN_WINDOW,
            PAID_ON_MAIL_BILL, UNPAID_DUE_AHEAD, UNPAID_DUE_PASSED, UNPAID_MONTH_AHEAD,
            UNPAID_MONTH_PASSED, PAID_UNCLEAR, LEDGER_SHORT, SHARED_FILE, FETCHED_ELSEWHERE)
# Marks a note can carry beside its finding.
NEW_ACCOUNT = "the invoice names a new account"
WEAK_AMOUNT = "weak amount"
AMOUNT_DIFFERS = "amount differs"
BY_INVOICE_NUMBER = "chosen by the invoice number in the title"
AISH_BESIDE_OWNER = ("paired to an aish-made note while an owner note of the same bill shares "
                     "the window")
NAME_DISAGREES = "name disagrees"
OTHER_ATTACHMENTS_IGNORED = "amounts on the note's other invoices of this account were not used"
# Per account.
UNNOTED = "payment with no note"

AISH_TAG = "aish"

#: Phrases the bank writes on a savings-goal or own-account transfer, folded.
#: Measured 2026-11-12: there is no structural type field (the API era's
#: transaction code is empty on every row) and goal sub-accounts are not in
#: the accounts table, so these words are what tells a savings transfer from a
#: payment. The API era writes the same own-transfer phrase as the last line of
#: its remittance information and has no savings-goal rows at all.
_OWN_TRANSFER_PHRASES = vocab.declare(
    "paysig._OWN_TRANSFER_PHRASES",
    ("przelew na twoje cele", "przelew wlasny"),
    languages="PL (the bank's operation phrases)",
    on_miss=vocab.PERMITS,
    structural="the payee account is one of the accounts table's own accounts",
    note="a miss lets a savings transfer in as a payment to someone",
)


# ---------------------------------------------------------------- folding


def fold(text: str) -> str:
    """`mailreport.fold` (lower case, diacritics and ł folded), dots removed
    first so `S.A.` is `sa`."""
    return mailreport.fold((text or "").replace(".", ""))


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def _words(text: str) -> frozenset[str]:
    """Folded tokens holding a letter: house numbers, dates and invoice
    numbers are no word of a payee or a purpose."""
    return frozenset(t for t in tokens(text) if re.search(r"[a-z]", t))


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(text))


def _month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_start(month: str) -> date:
    return date(int(month[:4]), int(month[5:7]), 1)


def _add_months(month: str, delta: int) -> str:
    index = int(month[:4]) * 12 + int(month[5:7]) - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _month_end(month: str) -> date:
    return _month_start(_add_months(month, 1)) - timedelta(days=1)


def _month_index(day: date) -> int:
    return day.year * 12 + day.month - 1


def _shift(day: date, months: int) -> date:
    """The same day `months` later, clamped to that month's end."""
    end = _month_end(_add_months(_month_of(day), months))
    return date(end.year, end.month, min(day.day, end.day))


def _date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        return None


# ---------------------------------------------------------------- the note


Amount = tuple[str | None, int]  # (currency or None when the document did not say, minor)


@dataclass(frozen=True)
class Invoice:
    """The union of one note's usable invoice facts."""

    read: bool = False  # at least one attachment read that is evidence for this note
    accounts: frozenset[str] = frozenset()
    strong: frozenset[Amount] = frozenset()
    weak: frozenset[Amount] = frozenset()
    earliest: date | None = None
    due: date | None = None  # read on a due-date line
    numbers: tuple[str, ...] = ()
    shared_file: bool = False  # an attachment is one file behind several notes
    fetched_elsewhere: bool = False
    to_pay: str = "none read"  # one | several | none read — for a future reminder
    other_amounts_ignored: bool = False  # several invoices of its account; one priced it


def _amount_is_weak(a: dict) -> bool:
    return not a.get("currency_seen") or bool(a.get("ambiguous_grouping"))


def invoice_of(records: Iterable[paynotes.InvoiceRecord], own_accounts: frozenset[str],
               month: str) -> Invoice:
    """One note's invoice evidence. A record found outside the note's own
    folders, or one file behind several notes, is evidence for neither note;
    an account that is one of his own is no payee."""
    usable = []
    shared = elsewhere = False
    for record in records:
        if record.status not in ("read", "partial"):
            continue
        if record.raw.get("shared_file"):
            shared = True
            continue
        if record.raw.get("fetched_elsewhere"):
            elsewhere = True
            continue
        usable.append(record)
    if not usable:
        return Invoice(shared_file=shared, fetched_elsewhere=elsewhere)
    middle = _month_start(month) + timedelta(days=14)

    def payees(record: paynotes.InvoiceRecord) -> set[str]:
        values = {mailcases.normalize_coordinate(str(a.get("account") or ""))
                  for a in record.accounts}
        return {v for v in values if v and v not in own_accounts}

    accounts = set().union(*(payees(r) for r in usable))
    naming = [r for r in usable if payees(r)]
    priced = usable
    if len(accounts) == 1 and len(naming) > 1:
        # Several invoices of one account on one note (last cycle's bundled
        # with this one): amounts come only from the attachment whose read
        # dates sit nearest the note's month, ties to the newest.
        def nearness(record: paynotes.InvoiceRecord) -> tuple[int, int]:
            days = [d for d in (_date(x.get("date")) for x in record.dates) if d is not None]
            if not days:
                return (10**6, 0)
            return (min(abs((d - middle).days) for d in days), -max(days).toordinal())
        priced = [min(naming, key=lambda r: (nearness(r), r.name))]
    ignored = any(a for r in usable if r not in priced for a in r.amounts)
    strong, weak, dates, due_dates, numbers = set(), set(), [], [], []
    payable_strong = set()
    for record in usable:
        for a in (record.amounts if record in priced else ()):
            if not isinstance(a.get("minor"), int):
                continue
            amount = (a.get("currency"), int(a["minor"]))
            if _amount_is_weak(a):
                weak.add(amount)
            else:
                strong.add(amount)
                if a.get("payable"):
                    payable_strong.add(amount)
        for d in record.dates:
            day = _date(d.get("date"))
            if day is not None:
                dates.append(day)
                if d.get("due"):
                    due_dates.append(day)
        numbers += [str(i.get("value") or "") for i in record.invoice_numbers]
    # A due line can print an old date too; the one nearest the note's month.
    due = min(due_dates, key=lambda d: (abs((d - middle).days), d)) if due_dates else None
    to_pay = ("one" if len(payable_strong) == 1 else "several" if payable_strong
              else "none read")
    return Invoice(True, frozenset(accounts), frozenset(strong), frozenset(weak - strong),
                   min(dates) if dates else None, due,
                   tuple(n for n in dict.fromkeys(numbers)
                         if len(_compact(n)) >= INVOICE_NUMBER_MIN),
                   shared, elsewhere, to_pay, ignored)


@dataclass(frozen=True)
class Note:
    """One bill note: an occurrence of some bill, never its identity."""

    id: str  # the note's vault path
    month: str
    paid: str  # true | false | unknown | conflict
    note_due: date | None
    note_amount: Amount | None
    aish_made: bool
    payee_words: frozenset[str]  # of its paid_to
    other_words: frozenset[str]  # its file name's after the month, minus the payee's
    name_disagrees: bool
    provider_label: str
    purpose_label: str
    invoice: Invoice = Invoice()

    @property
    def path(self) -> str:
        return self.id

    @property
    def due(self) -> date | None:
        return self.invoice.due or self.note_due

    def window(self) -> tuple[date, date]:
        """From the second month before (or the invoice's earliest read date,
        when LATER — a read date only narrows), to a read due date plus 30
        days (never before the end of the month after), else the end of the
        second month after."""
        start = _month_start(_add_months(self.month, -2))
        if self.invoice.earliest is not None and self.invoice.earliest > start:
            start = self.invoice.earliest
        if self.due is not None:
            end = max(self.due + timedelta(days=DUE_GRACE_DAYS),
                      _month_end(_add_months(self.month, 1)))
        else:
            end = _month_end(_add_months(self.month, 2))
        return start, max(start, end)

    @property
    def several_accounts(self) -> bool:
        return len(self.invoice.accounts) > 1

    @property
    def spelling(self) -> str:
        """The note's name as he writes it, without the words that carry a
        digit (house numbers, dates, invoice numbers)."""
        return _safe_label(f"{self.provider_label} · {self.purpose_label}".strip(" ·"))

    @property
    def reminder(self) -> dict[str, str]:
        """What the invoice offers a future reminder — stored, not rendered."""
        return {"to_pay": self.invoice.to_pay if self.invoice.read else "none read",
                "due": "due line" if self.invoice.due else "month"}


def _note_words(file_name: str, paid_to: str | None) -> tuple[frozenset[str], frozenset[str],
                                                               bool, str]:
    stem = re.sub(r"\.md$", "", file_name)
    stem = re.sub(r"^\d{4}-\d{2}\s*", "", stem).strip()
    payee = _words(paid_to or "")
    named = set(tokens(stem))
    others = frozenset(w for w in _words(stem) if w not in payee)
    disagrees = not payee or not payee <= named
    label = " ".join(w for w in stem.split() if not _words(w) <= payee) or stem
    return payee, others, disagrees, label


def notes_from(notes: Iterable[paynotes.PaymentNote],
               invoices: Iterable[paynotes.InvoiceRecord] = (),
               own_accounts: frozenset[str] = frozenset()) -> list[Note]:
    """The index's read notes, each with its invoice evidence."""
    by_path: dict[str, list[paynotes.InvoiceRecord]] = defaultdict(list)
    for record in invoices:
        by_path[record.path].append(record)
    out = []
    for note in notes:
        if note.status != "read" or not note.month:
            continue
        payee, others, disagrees, label = _note_words(note.file, note.paid_to)
        amount = ((note.currency or "PLN", int(note.amount_minor))
                  if note.amount_minor is not None else None)
        out.append(Note(
            id=note.path, month=note.month, paid=note.paid, note_due=_date(note.date_due),
            note_amount=amount, aish_made=AISH_TAG in {str(t).casefold() for t in note.tags},
            payee_words=payee, other_words=others, name_disagrees=disagrees,
            provider_label=note.paid_to or "", purpose_label=label,
            invoice=invoice_of(by_path.get(note.path, ()), own_accounts, note.month)))
    return sorted(out, key=lambda n: (n.month, n.id))


# ---------------------------------------------------------------- the bank side


@dataclass(frozen=True)
class Payment:
    id: str  # mailbank's transaction id: the same id in both pairing ledgers
    booked: date
    currency: str
    minor: int
    account: str | None  # normalised; None for a card payment
    payee: str  # folded
    title: str  # folded

    @property
    def payee_prefix(self) -> str:
        return self.payee[:PAYEE_PREFIX_CHARS].strip()

    @property
    def merchant(self) -> str:
        """A card payment's merchant: its title before the first `/` (the
        import era writes `MERCHANT /CITY … Data waluty: …`), digits dropped,
        cut like a payee name. Empty when the title names nothing."""
        head = re.sub(r"\d+", " ", self.title.split("/")[0])
        return " ".join(tokens(head))[:PAYEE_PREFIX_CHARS].strip()

    @property
    def bill_key(self) -> str | None:
        """The bill this payment belongs to: its account, else its merchant."""
        if self.account:
            return _account_key(self.account)
        return f"card:{self.merchant}" if self.merchant else None


def _account_key(account: str) -> str:
    # A bill's id names no account: its stores and printouts are read back.
    return "account:" + hashlib.sha256(account.encode()).hexdigest()[:12]


@dataclass
class BankSide:
    payments: list[Payment] = field(default_factory=list)
    own_account: int = 0  # left out: paid to one of his own accounts
    own_phrase: int = 0  # left out: a savings or own-transfer phrase
    eras: Counter[str] = field(default_factory=Counter)


def _raw(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str)]
    return [value] if isinstance(value, str) else []


def normalise(tx: mailbank.Transaction, own_accounts: frozenset[str]
              ) -> tuple[Payment | None, str]:
    """(the payment, its era) or (None, why it is not a payment to anyone).

    Two eras, two raw shapes. The CSV import names the account `Numer konta`,
    the payee `Nadawca/Odbiorca`, the title `Tytuł` and the operation `Opis
    operacji`; it has no creditor-account field. The bank API puts the account
    under `creditor_account`, the payee under `creditor.name`, and the title
    then the operation phrase in `remittance_information`."""
    raw = _raw(tx.raw)
    if "Opis operacji" in raw or "Numer konta" in raw:
        era = "import"
        number = str(raw.get("Numer konta") or "").strip().strip("'")
        account = mailcases.normalize_coordinate(number) if number else ""
        payee = str(raw.get("Nadawca/Odbiorca") or tx.counterparty or "")
        title = str(raw.get("Tytuł") or "")
        phrase = str(raw.get("Opis operacji") or "")
    else:
        era = "api"
        account = tx.creditor_accounts[0] if tx.creditor_accounts else ""
        creditor = raw.get("creditor")
        name = creditor.get("name") if isinstance(creditor, dict) else None
        payee = str(name or tx.counterparty or "")
        lines = _strings(raw.get("remittance_information"))
        phrase = lines[-1] if len(lines) > 1 else ""
        title = " ".join(lines[:-1] if len(lines) > 1 else lines) or tx.description
    if account and account in own_accounts:
        return None, "own_account"
    if phrase:
        hit = fold(phrase) in _OWN_TRANSFER_PHRASES
        vocab.note("paysig._OWN_TRANSFER_PHRASES", matched=hit)
        if hit:
            return None, "own_phrase"
    booked = _date(tx.booking_date)
    if booked is None:
        return None, "no_date"
    return Payment(tx.id, booked, tx.currency, tx.cents, account or None, fold(payee),
                   fold(title)), era


def read_payments(ledger: mailbank.Ledger) -> BankSide:
    side = BankSide()
    for tx in ledger.transactions:
        payment, why = normalise(tx, ledger.own_accounts)
        if payment is None:
            if why == "own_account":
                side.own_account += 1
            elif why == "own_phrase":
                side.own_phrase += 1
            continue
        side.eras[why] += 1
        side.payments.append(payment)
    side.payments.sort(key=lambda p: (p.booked, p.id))
    return side


class _ByKey:
    """Payments per key (an account, or a bill), date-sorted, for window
    queries."""

    def __init__(self, payments: Iterable[Payment], key: Callable[[Payment], str | None]
                 ) -> None:
        self.rows: dict[str, list[Payment]] = defaultdict(list)
        for p in payments:
            k = key(p)
            if k:
                self.rows[k].append(p)
        self.days: dict[str, list[date]] = {}
        for k, rows in self.rows.items():
            rows.sort(key=lambda p: (p.booked, p.id))
            self.days[k] = [p.booked for p in rows]

    def within(self, k: str, start: date, end: date) -> list[Payment]:
        days = self.days.get(k, [])
        lo = bisect.bisect_left(days, start)
        hi = bisect.bisect_right(days, end)
        return self.rows[k][lo:hi] if k in self.rows else []


# ---------------------------------------------------------------- the bills


@dataclass
class Bill:
    """A payee account — or, for card payments, a merchant — learned from the
    bank alone. Its notes and their spellings attach after matching."""

    id: str
    account: str | None  # None: a card merchant (`no account`)
    names: list[dict[str, str]]  # {name, first, last} as the bank writes it, newest first
    name_words: frozenset[str]  # every name it was paid under, folded
    title_words: frozenset[str]  # words recurring in its transfer titles
    cadence: str
    cadence_months: int | None
    day_range: tuple[int, int] | None
    amounts: str
    payments: int  # in its own last 36 months
    first_month: str
    last_month: str
    last_paid: date
    status: str = ACTIVE
    expectation: dict[str, Any] = field(default_factory=dict)
    notes: int = 0
    spellings: list[str] = field(default_factory=list)
    senders: list[str] = field(default_factory=list)  # a later slice fills it

    @property
    def payee(self) -> str:
        return self.names[0]["name"] if self.names else ""

    def to_json(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["name_words"] = sorted(self.name_words)
        out["title_words"] = sorted(self.title_words)
        out["day_range"] = list(self.day_range) if self.day_range else None
        out["last_paid"] = self.last_paid.isoformat()
        out["kind"] = "account" if self.account else "no account"
        return out


def cadence_of(days: list[date]) -> tuple[str, int | None]:
    """(class, median gap in months) of the months payments fell in."""
    months = sorted({_month_index(d) for d in days})
    if len(months) < BILL_MIN_PAYMENTS:
        return FEW, None
    gaps = [b - a for a, b in zip(months, months[1:], strict=False)]
    median = statistics.median_low(gaps)
    near = sum(1 for g in gaps if abs(g - median) <= (0 if median <= 1 else 1))
    if near / len(gaps) < CADENCE_SHARE:
        return IRREGULAR, None
    for upto, name in ((1, MONTHLY), (2, BIMONTHLY), (4, QUARTERLY), (7, HALF_YEARLY)):
        if median <= upto:
            return name, median
    return (YEARLY, median) if 11 <= median <= 13 else (IRREGULAR, None)


def amount_profile(minors: list[int]) -> str:
    counts = Counter(minors)
    if len(counts) == 1:
        return SAME_AMOUNT
    top = sum(n for _, n in counts.most_common(3))
    return FEW_AMOUNTS if top / len(minors) >= 0.8 else VARIES


def _day_range(days: list[date]) -> tuple[int, int]:
    """The usual day of the month: the extreme tenth on each side dropped."""
    ordered = sorted(d.day for d in days)
    cut = len(ordered) // 10
    kept = ordered[cut:len(ordered) - cut] or ordered
    return kept[0], kept[-1]


def expectation_of(bill: Bill, seen_to: date | None) -> tuple[str, dict[str, Any]]:
    """(status, expectation), as far as the ledger reaches (`seen_to`). A bill
    whose last payment is older than two cadence lengths — or, with no
    cadence, older than `UNCADENCED_ENDED_MONTHS` — has ended?, and expects
    nothing. A monthly, two-monthly or quarterly bill with enough payments
    expects its next payment by the end of its usual day range in the month
    one cadence after the last one."""
    if seen_to is None:
        return ACTIVE, {"state": LEDGER_SHORT}
    span = (2 * bill.cadence_months if bill.cadence_months is not None
            else UNCADENCED_ENDED_MONTHS)
    if seen_to > _shift(bill.last_paid, span):
        return ENDED, {"state": NO_EXPECTATION}
    if (bill.cadence not in EXPECTED_CADENCES or bill.cadence_months is None
            or bill.payments < EXPECT_MIN_PAYMENTS or bill.day_range is None):
        return ACTIVE, {"state": NO_EXPECTATION}
    month = _add_months(_month_of(bill.last_paid), bill.cadence_months)
    by = min(_month_start(month) + timedelta(days=bill.day_range[1] - 1), _month_end(month))
    if seen_to > by:
        return ACTIVE, {"state": EXPECTED_NOT_SEEN, "expected_by": by.isoformat(),
                        "days_late": (seen_to - by).days}
    return ACTIVE, {"state": NEXT_EXPECTED, "expected_by": by.isoformat()}


def _bill_of(key: str, rows: list[Payment], seen_to: date | None) -> Bill:
    rows = sorted(rows, key=lambda p: (p.booked, p.id))
    last = rows[-1].booked
    since = _month_start(_add_months(_month_of(last), -(BILL_HISTORY_MONTHS - 1)))
    recent = [p for p in rows if p.booked >= since]
    card = key.startswith("card:")
    names: dict[str, list[str]] = {}
    for p in rows:  # every name it was paid under, over all of its history
        name = p.merchant if card else p.payee_prefix
        if name:
            seen = names.setdefault(name, [_month_of(p.booked), _month_of(p.booked)])
            seen[1] = _month_of(p.booked)
    ordered = sorted(names.items(), key=lambda kv: (kv[1][1], kv[1][0]), reverse=True)
    titles = Counter(w for p in recent for w in _words(p.title))
    cadence, months = cadence_of([p.booked for p in recent])
    bill = Bill(
        id=key, account=None if card else rows[-1].account,
        names=[{"name": n, "first": f, "last": la} for n, (f, la) in ordered],
        name_words=frozenset(w for p in rows for w in _words(p.merchant if card else p.payee)),
        title_words=frozenset(w for w, n in titles.items()
                              if n / len(recent) >= TITLE_TOKEN_SHARE),
        cadence=cadence, cadence_months=months, day_range=_day_range([p.booked for p in recent]),
        amounts=amount_profile([p.minor for p in recent]), payments=len(recent),
        first_month=_month_of(rows[0].booked), last_month=_month_of(last), last_paid=last)
    bill.status, bill.expectation = expectation_of(bill, seen_to)
    return bill


def learn_bills(payments: Iterable[Payment], as_of: date,
                invoice_accounts: Iterable[str] = (), *,
                reach: date | None) -> dict[str, Bill]:
    """Every bill the ledger shows up to `as_of`. Pure, deterministic, no model.

    An account is a bill when it was paid at least three times, or when a
    note's invoice prints it and it was paid at all. A card merchant is a bill
    only when it looks like one by the bank's own record: a regular cadence,
    at most about one payment per month it is paid in, and an amount that is
    always the same or one of a few."""
    rows: dict[str, list[Payment]] = defaultdict(list)
    for p in payments:
        if p.booked <= as_of and p.bill_key:
            rows[p.bill_key].append(p)
    printed = {_account_key(a) for a in invoice_accounts}
    # Whether a payment is late can be said only as far as the ledger reaches.
    seen_to = min(as_of, reach) if reach is not None else None
    out = {}
    for key, paid in rows.items():
        if key.startswith("card:"):
            if len(paid) < BILL_MIN_PAYMENTS:
                continue
            bill = _bill_of(key, paid, seen_to)
            months = len({_month_index(p.booked) for p in paid
                          if p.booked >= _month_start(_add_months(
                              bill.last_month, -(BILL_HISTORY_MONTHS - 1)))})
            if (bill.cadence in (FEW, IRREGULAR) or bill.amounts == VARIES
                    or bill.payments / max(1, months) > CARD_MAX_PER_MONTH):
                continue
        elif len(paid) >= BILL_MIN_PAYMENTS or key in printed:
            bill = _bill_of(key, paid, seen_to)
        else:
            continue
        out[key] = bill
    return out


# ---------------------------------------------------------------- the matcher


@dataclass
class NoteResult:
    note: Note
    finding: str = ""
    tier: str = ""
    tx: str = ""
    bill: str = ""  # the bill it attached to, by `route`
    route: str = ""
    candidates: int = 0  # bills of its payee, when there were several
    marks: list[str] = field(default_factory=list)
    observations: dict[str, str] = field(default_factory=dict)
    new: bool = False  # an A pair first made by this run
    claimants: int = 0  # notes its payment qualified for under A, before assignment


@dataclass
class Matched:
    results: list[NoteResult] = field(default_factory=list)
    bills: dict[str, Bill] = field(default_factory=dict)
    unnoted: dict[str, list[str]] = field(default_factory=dict)  # account -> tx ids
    shared_claims: int = 0  # payments that qualified for several notes under A
    shared_by_order: int = 0  # ... of them resolved by month order, not a unique claim

    def new_pairs(self) -> list[tuple[str, str]]:
        return [(r.note.id, r.tx) for r in self.results if r.new and r.tx]


def _order(note: Note) -> tuple:
    # His notes take payments before notes aish made, within a month.
    return (note.month, note.aish_made, note.due or date.max, note.id)


def _a_candidates(note: Note, by_account: _ByKey, as_of: date) -> tuple[list[Payment], bool]:
    """(payments satisfying A for the note, whether only by a weak amount)."""
    if not note.invoice.accounts or note.several_accounts:
        return [], False
    start, end = note.window()
    inside = [p for a in sorted(note.invoice.accounts)
              for p in by_account.within(a, start, min(end, as_of))]

    def agrees(p: Payment, amounts: frozenset[Amount]) -> bool:
        return any(m == p.minor and (c is None or c == p.currency) for c, m in amounts)

    strong = [p for p in inside if agrees(p, note.invoice.strong)]
    if strong:
        return sorted(strong, key=lambda p: (p.booked, p.id)), False
    weak = [p for p in inside if agrees(p, note.invoice.weak)]
    return sorted(weak, key=lambda p: (p.booked, p.id)), bool(weak)


def _best(note: Note, candidates: list[Payment]) -> tuple[Payment, bool]:
    """The earliest, unless an invoice number of the note is in a title."""
    numbered = [p for p in candidates
                if any(_compact(n) in _compact(p.title) for n in note.invoice.numbers)]
    if numbered and len(numbered) < len(candidates):
        return numbered[0], True
    return candidates[0], False


def bills_of_payee(note: Note, bills: dict[str, Bill]) -> list[Bill]:
    """The bills whose names, over time, carry every word of the note's
    payee. A note naming no payee has none."""
    if not note.payee_words:
        return []
    return [b for b in bills.values() if note.payee_words <= b.name_words]


def route_c(note: Note, bills: dict[str, Bill], by_bill: _ByKey) -> tuple[str, Bill | None, int]:
    """(route, bill, how many bills of the payee) for a note with no usable
    invoice account. The bank's own record decides — the payee's names and its
    transfer titles — never another note's name.

    Of the payee's bills, those paid in the note's window are the candidates
    (all of them when none was). One candidate is the bill. Among several, the
    one whose recurring title words carry a word of the note, while no word of
    the note is another candidate's OWN title word (one no other candidate's
    titles recur); otherwise it is several bills of this payee."""
    if not note.payee_words:
        return ROUTE_NO_PAYEE, None, 0
    named = bills_of_payee(note, bills)
    if not named:
        return ROUTE_NO_BILL, None, 0
    start, end = note.window()
    live = [b for b in named if by_bill.within(b.id, start, end)] or named
    if len(live) == 1:
        return ROUTE_NAME, live[0], 1
    own = {b.id: b.title_words - frozenset().union(*(o.title_words for o in live if o is not b))
           for b in live}
    hits = [b for b in live if note.other_words & b.title_words
            and not any(note.other_words & own[o.id] for o in live if o is not b)]
    if len(hits) == 1:
        return ROUTE_TITLE, hits[0], len(live)
    return ROUTE_SEVERAL, None, len(live)


def match(notes: list[Note], payments: Iterable[Payment], *, recorded: dict[str, str],
          mail_spent: set[str], as_of: date, reach: date | None,
          bills: dict[str, Bill] | None = None, unnoted_from: date | None = None) -> Matched:
    """Every note's finding, A then C, then B reported. Pure.

    `recorded` is the A pairing ledger (note id -> tx): a recorded pair stands
    while its transaction is in the ledger and is never reassigned. `mail_spent`
    are the mail bills' pairs. `bills` given: use them (the backtest learns
    them before its cut) instead of learning them from these payments."""
    seen = [p for p in payments if p.booked <= as_of]
    by_id = {p.id: p for p in seen}
    by_account = _ByKey(seen, lambda p: p.account)
    by_bill = _ByKey(seen, lambda p: p.bill_key)
    if bills is None:
        bills = learn_bills(seen, as_of, (a for n in notes for a in n.invoice.accounts),
                            reach=reach)
    spent = set(recorded.values()) | set(mail_spent)
    current = _month_of(as_of)
    order = sorted(notes, key=_order)
    results = {n.id: NoteResult(n) for n in notes}
    out = Matched(bills=bills)

    # Recorded A pairs stand; a vanished transaction settles nothing.
    for note in order:
        if note.id in recorded:
            payment = by_id.get(recorded[note.id])
            if payment is not None:
                results[note.id].tx, results[note.id].tier = payment.id, TIER_A
    open_notes = [n for n in order if n.id not in recorded and n.month <= current]

    # A: every claim evaluated once, up front.
    qualifies: dict[str, list[Payment]] = {}
    weak_only: dict[str, bool] = {}
    for note in open_notes:
        found, weak = _a_candidates(note, by_account, as_of)
        qualifies[note.id] = [p for p in found if p.id not in spent]
        weak_only[note.id] = weak
    claims: dict[str, set[str]] = defaultdict(set)
    for note_id, found in qualifies.items():
        for p in found:
            claims[p.id].add(note_id)
    out.shared_claims = sum(1 for c in claims.values() if len(c) > 1)

    def take(note: Note, candidates: list[Payment], tier: str) -> None:
        chosen, by_number = _best(note, candidates)
        result = results[note.id]
        result.tx, result.tier = chosen.id, tier
        result.new = tier == TIER_A
        result.claimants = len(claims.get(chosen.id, ()))
        if tier == TIER_A and weak_only.get(note.id):
            result.marks.append(WEAK_AMOUNT)
        if tier == TIER_A and note.invoice.other_amounts_ignored:
            result.marks.append(OTHER_ATTACHMENTS_IGNORED)
        if by_number:
            result.marks.append(BY_INVOICE_NUMBER)
        spent.add(chosen.id)

    for note in open_notes:
        unique = [p for p in qualifies[note.id] if claims[p.id] == {note.id} and p.id not in spent]
        if unique:
            take(note, unique, TIER_A)
    for note in open_notes:
        if results[note.id].tx:
            continue
        left = [p for p in qualifies[note.id] if p.id not in spent]
        if left:
            take(note, left, TIER_A)
            if results[note.id].claimants > 1:
                out.shared_by_order += 1

    # Each note's bill: by its invoice account, else by the bank's names and titles.
    for note in order:
        result = results[note.id]
        if note.invoice.accounts:
            if not note.several_accounts:
                key = _account_key(next(iter(note.invoice.accounts)))
                result.route, result.bill = ROUTE_INVOICE, key if key in bills else ""
            else:
                result.route = ROUTE_INVOICE
            continue
        route, bill, many = route_c(note, bills, by_bill)
        result.route, result.bill = route, bill.id if bill else ""
        result.candidates = many if route == ROUTE_SEVERAL else 0

    # C: over what A left, for notes with no usable invoice account.
    for note in open_notes:
        result = results[note.id]
        if result.tx or note.invoice.accounts or not result.bill:
            continue
        start, end = note.window()
        left = [p for p in by_bill.within(result.bill, start, min(end, as_of))
                if p.id not in spent]
        if not left:
            continue
        if note.note_amount is not None:
            exact = [p for p in left if (p.currency, p.minor) == note.note_amount]
            left = exact or left
        # Nearest the note's own month: measured, 221 of 251 A pairings were
        # paid in it, and the earliest in the window is a cycle before.
        month = _month_index(_month_start(note.month))
        left.sort(key=lambda p: (abs(_month_index(p.booked) - month), p.booked, p.id))
        take(note, left, TIER_C)
        chosen = by_id[result.tx]
        if note.note_amount is not None and (chosen.currency, chosen.minor) != note.note_amount:
            result.marks.append(AMOUNT_DIFFERS)

    # Marks and findings.
    for note in order:
        result = results[note.id]
        if result.tx and note.aish_made and result.bill:
            paid_day = by_id[result.tx].booked
            if any(not other.note.aish_made and other.bill == result.bill
                   and other.note.window()[0] <= paid_day <= other.note.window()[1]
                   for other in results.values()):
                result.marks.append(AISH_BESIDE_OWNER)
        usual = _names_new_account(note, result, bills, current)
        if usual is not None:
            result.marks.append(NEW_ACCOUNT)
            if usual:
                result.observations["the payee this provider is usually paid to"] = usual
        if note.name_disagrees:
            result.marks.append(NAME_DISAGREES)
        result.finding = _finding(note, result, by_account, by_bill, mail_spent, as_of, reach)
        out.results.append(result)

    for bill in bills.values():
        bill.notes, bill.spellings = 0, []
    for result in out.results:
        bill = bills.get(result.bill)
        if bill is not None:
            bill.notes += 1
            if result.note.spelling and result.note.spelling not in bill.spellings:
                bill.spellings.append(result.note.spelling)

    taken = {r.tx for r in out.results if r.tx} | spent
    for account, firsts in _paired_accounts(out.results, by_id).items():
        first = unnoted_from or firsts
        out.unnoted[account] = [p.id for p in by_account.within(account, first, as_of)
                                if p.id not in taken]
    return out


def _paired_accounts(results: list[NoteResult], by_id: dict[str, Payment]) -> dict[str, date]:
    """Accounts with an A pairing -> the earliest window start among them."""
    out: dict[str, date] = {}
    for r in results:
        if r.tier == TIER_A and by_id.get(r.tx) and by_id[r.tx].account:
            account = by_id[r.tx].account
            assert account is not None
            start = r.note.window()[0]
            out[account] = min(out.get(account, start), start)
    return out


def _names_new_account(note: Note, result: NoteResult, bills: dict[str, Bill],
                       current: str) -> str | None:
    """None, or the payee name the provider is usually paid to: for a note
    NOT yet paid, of this month or later, whose invoice prints an account
    that is no bill of its payee. History never raises it, and neither does a
    payee with no bill to differ from."""
    if (note.paid == "true" or result.tx or note.month < current
            or not note.invoice.accounts):
        return None
    named = bills_of_payee(note, bills)
    if not named:
        return None
    known = {b.account for b in named}
    if note.invoice.accounts <= known:
        return None
    newest = max(named, key=lambda b: (b.last_paid, b.id))
    return newest.payee


def _finding(note: Note, result: NoteResult, by_account: _ByKey, by_bill: _ByKey,
             mail_spent: set[str], as_of: date, reach: date | None) -> str:
    if note.paid not in ("true", "false"):
        return PAID_UNCLEAR
    if result.tx:
        return PAID_FOUND if note.paid == "true" else UNPAID_FOUND
    if note.several_accounts:
        return SEVERAL_INVOICES
    start, end = note.window()
    if note.invoice.accounts:
        inside = [p for a in sorted(note.invoice.accounts)
                  for p in by_account.within(a, start, min(end, as_of))]
        if note.paid == "true" and any(p.id in mail_spent for p in inside):
            return PAID_ON_MAIL_BILL
        if inside:
            read_any = note.invoice.strong or note.invoice.weak
            return B_AMOUNT_DIFFERS if read_any else B_NO_AMOUNT
    elif result.bill and note.paid == "true" and any(
            p.id in mail_spent for p in by_bill.within(result.bill, start, min(end, as_of))):
        return PAID_ON_MAIL_BILL
    if not note.invoice.read:
        if note.invoice.shared_file:
            return SHARED_FILE
        if note.invoice.fetched_elsewhere:
            return FETCHED_ELSEWHERE
    if result.route == ROUTE_SEVERAL:
        return SEVERAL_BILLS
    if reach is None or reach < (end if note.paid == "true" else start):
        return LEDGER_SHORT
    if note.paid == "true":
        if note.invoice.accounts or result.bill:
            return PAID_NOT_IN_WINDOW
        if note.invoice.read:
            return PAID_NO_ACCOUNT
        return PAID_NO_INVOICE
    if note.due is not None:
        return UNPAID_DUE_AHEAD if note.due >= as_of else UNPAID_DUE_PASSED
    return UNPAID_MONTH_AHEAD if note.month >= _month_of(as_of) else UNPAID_MONTH_PASSED


# ---------------------------------------------------------------- the stores


def signatures_path(state_dir: os.PathLike | str) -> Path:
    return paynotes.payments_dir(Path(state_dir)) / SIGNATURES_NAME


def bills_path(state_dir: os.PathLike | str) -> Path:
    return paynotes.payments_dir(Path(state_dir)) / BILLS_NAME


def pairs_path(state_dir: os.PathLike | str) -> Path:
    return paynotes.payments_dir(Path(state_dir)) / PAIRS_NAME


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_store(state_dir: os.PathLike | str) -> dict[str, Any]:
    """The last written note outcomes, or {}."""
    return _read_json(signatures_path(state_dir))


def read_bills(state_dir: os.PathLike | str) -> dict[str, Any]:
    """The last written bills and their findings, or {}."""
    return _read_json(bills_path(state_dir))


def read_pairs(state_dir: os.PathLike | str) -> dict[str, str]:
    """note id -> tx, the first pairing of each note and of each transaction
    standing. A line that does not parse costs that line."""
    try:
        text = pairs_path(state_dir).read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    used: set[str] = set()
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            note, tx = str(entry["note"]), str(entry["tx"])
        except (ValueError, KeyError, TypeError):
            continue
        if note in out or tx in used:
            continue
        out[note] = tx
        used.add(tx)
    return out


def spent_transactions(state_dir: os.PathLike | str) -> set[str]:
    """Transactions already paired to a bill note — not available to a mail
    bill (`mailbank.assign`)."""
    return set(read_pairs(state_dir).values())


def bill_findings(bills: dict[str, Bill]) -> list[dict[str, Any]]:
    """Per bill: its expectation when a payment is late, or `ended?`."""
    out = []
    for bill in sorted(bills.values(), key=lambda b: b.id):
        if bill.expectation.get("state") == EXPECTED_NOT_SEEN:
            out.append({"bill": bill.id, "finding": EXPECTED_NOT_SEEN,
                        "days_late": bill.expectation["days_late"],
                        "expected_by": bill.expectation["expected_by"]})
    return out


def _write_stores(state_dir: Path, matched: Matched, *, now: datetime, checked_to: str,
                  by: str, pairs: bool) -> int:
    """Bills and note outcomes whole; the NEW tier-A pairs appended (rewritten
    whole through the atomic publish). Only A is ever recorded."""
    stamp = now.isoformat(timespec="seconds")
    head = {"schema": SCHEMA, "written_at": stamp, "checked_to": checked_to, "by": by}
    paynotes._publish(bills_path(state_dir), json.dumps({
        **head, "bills": {b.id: b.to_json() for b in matched.bills.values()},
        "findings": bill_findings(matched.bills),
    }, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    paynotes._publish(signatures_path(state_dir), json.dumps({
        **head,
        "notes": {r.note.id: {"finding": r.finding, "tier": r.tier, "marks": r.marks,
                              "bill": r.bill, "route": r.route, "candidates": r.candidates,
                              "observations": r.observations, "reminder": r.note.reminder}
                  for r in matched.results},
    }, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    new = matched.new_pairs() if pairs else []
    if new:
        flags = {r.note.id: r.marks for r in matched.results}
        path = pairs_path(state_dir)
        try:
            existing = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        lines = "".join(json.dumps({"note": note, "tx": tx, "tier": TIER_A,
                                    "flags": flags.get(note, []), "at": stamp,
                                    "checked_to": checked_to, "by": by},
                                   ensure_ascii=False, sort_keys=True) + "\n"
                        for note, tx in new)
        paynotes._publish(path, existing + lines)
    return len(new)


# ---------------------------------------------------------------- the run


@dataclass
class Inputs:
    notes: list[Note] = field(default_factory=list)
    ledger: mailbank.Ledger | None = None
    unreadable: str = ""  # nothing can be computed
    stale: str = ""  # computed for display; no pairing may be made
    invoice_records: int = 0


def _inputs(state_dir: Path, now: datetime) -> Inputs:
    loaded = paynotes.load(state_dir, now)
    if not loaded.available:
        return Inputs(unreadable=f"the notes index: {loaded.reason}")
    invoices = paynotes.load_invoices(state_dir, now)
    if not invoices.available:
        return Inputs(unreadable=f"the invoice facts: {invoices.reason}")
    ledger = mailbank.read_ledger(state_dir, now.timestamp(), keep_raw=True)
    if not ledger.readable:
        return Inputs(unreadable=f"the bank ledger: {ledger.reason}")
    stale = ""
    fresh = timedelta(days=INPUT_FRESH_DAYS)
    if loaded.age is None or loaded.age > fresh:
        stale = f"the notes index is older than {INPUT_FRESH_DAYS} days"
    elif invoices.age is None or invoices.age > fresh:
        stale = f"the invoice facts are older than {INPUT_FRESH_DAYS} days"
    elif not ledger.checked:
        stale = f"the bank ledger: {ledger.reason}"
    notes = notes_from(loaded.notes, invoices.invoices, ledger.own_accounts)
    return Inputs(notes, ledger, "", stale, len(invoices.invoices))


def _reach(ledger: mailbank.Ledger) -> date | None:
    return _date(ledger.synced_to)


def recompute(state_dir: os.PathLike | str, now: datetime) -> tuple[Inputs, Matched]:
    """Match for display: nothing is written."""
    inputs = _inputs(Path(state_dir), now)
    if inputs.ledger is None:
        return inputs, Matched()
    matched = match(inputs.notes, read_payments(inputs.ledger).payments,
                    recorded=read_pairs(state_dir),
                    mail_spent={tx for tx, _ in mailbank.read_pairs(state_dir)},
                    as_of=now.date(), reach=_reach(inputs.ledger))
    return inputs, matched


def learn_and_record(state_dir: os.PathLike | str, now: datetime, *, by: str = "report",
                     lock_wait_s: float | None = None) -> dict[str, Any]:
    """The ONE writer of the stores: learn the bills, match and write under
    the item acts' lock, so neither pairing ledger moves while this one reads
    the other. Stale inputs record the outcomes and NO new pairing. Counts
    only in what it returns: the report ledger keeps it."""
    from . import mailitems

    state_dir = Path(state_dir)
    inputs = _inputs(state_dir, now)
    if inputs.ledger is None:
        return {"outcome": "could not check", "reason": inputs.unreadable}
    payments = read_payments(inputs.ledger).payments
    wait = mailitems.ITEMS_LOCK_WAIT_S if lock_wait_s is None else lock_wait_s
    with mailitems.acts_lock(state_dir, wait) as held:
        if not held:
            return {"outcome": "lock held",
                    "reason": "the item acts' lock was held; nothing was written"}
        matched = match(inputs.notes, payments, recorded=read_pairs(state_dir),
                        mail_spent={tx for tx, _ in mailbank.read_pairs(state_dir)},
                        as_of=now.date(), reach=_reach(inputs.ledger))
        recorded = _write_stores(state_dir, matched, now=now,
                                 checked_to=inputs.ledger.synced_to, by=by,
                                 pairs=not inputs.stale)
    tiers = Counter(r.tier for r in matched.results if r.tier)
    return {"outcome": "could not check" if inputs.stale else "ok", "reason": inputs.stale,
            "tiers": dict(sorted(tiers.items())), "bills": len(matched.bills),
            "expected_not_seen": len(bill_findings(matched.bills)),
            "pairs_recorded": recorded, "checked_to": inputs.ledger.synced_to}


# ---------------------------------------------------------------- the backtest


@dataclass
class Backtest:
    paid: Counter[str] = field(default_factory=Counter)  # tier A / B / C / none
    routes: Counter[str] = field(default_factory=Counter)  # of paid notes with no account
    unpaid_found: int = 0
    unpaid: int = 0
    c_without_invoice: int = 0  # notes with no read invoice, matched by C
    without_invoice: int = 0
    c_agrees: int = 0  # notes A-matched whose C-alone match is the same payment
    c_disagrees: int = 0
    c_silent: int = 0  # ... for which C alone matched nothing
    bills: int = 0


def _strip_invoice(note: Note) -> Note:
    return Note(**{**note.__dict__, "invoice": Invoice()})


def backtest(notes: list[Note], payments: list[Payment], cut: str, as_of: date) -> Backtest:
    """Bills learned from the ledger BEFORE `cut` (and the invoice accounts of
    the notes before it); the notes from the cut on matched with them."""
    learned_as_of = _month_start(cut) - timedelta(days=1)
    printed = [a for n in notes if n.month < cut for a in n.invoice.accounts]

    def bills() -> dict[str, Bill]:  # each match fills its own bills' note counts
        return learn_bills(payments, learned_as_of, printed, reach=learned_as_of)

    full = match(notes, payments, recorded={}, mail_spent=set(), as_of=as_of, reach=as_of,
                 bills=bills())
    c_only = match([_strip_invoice(n) for n in notes], payments, recorded={}, mail_spent=set(),
                   as_of=as_of, reach=as_of, bills=bills())
    c_tx = {r.note.id: r.tx for r in c_only.results if r.tier == TIER_C}
    out = Backtest(bills=len(full.bills))
    current = _month_of(as_of)
    for r in full.results:
        if not cut <= r.note.month <= current or r.note.aish_made:
            continue
        if r.note.paid == "true":
            if r.tier:
                out.paid[r.tier] += 1
            elif r.finding in (B_AMOUNT_DIFFERS, B_NO_AMOUNT):
                out.paid["B"] += 1
            else:
                out.paid["none"] += 1
            if r.route != ROUTE_INVOICE:
                out.routes[r.route] += 1
            if not r.note.invoice.read:
                out.without_invoice += 1
                out.c_without_invoice += r.tier == TIER_C
            elif r.tier == TIER_A:
                other = c_tx.get(r.note.id)
                if other is None:
                    out.c_silent += 1
                elif other == r.tx:
                    out.c_agrees += 1
                else:
                    out.c_disagrees += 1
        elif r.note.paid == "false":
            out.unpaid += 1
            out.unpaid_found += bool(r.tx)
    return out


# ---------------------------------------------------------------- words


def _safe_name(text: str) -> str:
    """A payee name for a terminal: no run of digits long enough to be part
    of an account, an invoice or a card number."""
    return re.sub(r"\d{5,}", "…", text)


def _safe_label(text: str) -> str:
    """A note's name for a terminal: its words without any that carry a
    digit — those are house numbers, dates and invoice numbers."""
    return " ".join(w for w in text.split() if not re.search(r"\d", w))


def _status_words(bill: Bill) -> str:
    state = bill.expectation.get("state")
    if state == EXPECTED_NOT_SEEN:
        return f"{EXPECTED_NOT_SEEN}, {bill.expectation['days_late']} days late"
    return bill.status


def summary(state_dir: os.PathLike | str, now: datetime) -> str:
    """`aish paysig`: names and counts. No amount, no account number, no title."""
    inputs, matched = recompute(state_dir, now)
    lines = ["bills learned from the bank, and the bill notes matched to them (recomputed now "
             "for display; only the report job and `aish paysig --learn` write the stores)"]
    store = read_store(state_dir)
    if store.get("written_at"):
        lines.append(f"stores last written {store['written_at']}, bank checked to "
                     f"{store.get('checked_to') or 'unknown'}")
    if inputs.unreadable:
        lines.append(f"could not check: {inputs.unreadable}")
        return "\n".join(lines)
    lines.append(f"invoice records: {inputs.invoice_records}")
    if inputs.stale:
        lines.append(f"could not check (no new pairing would be made): {inputs.stale}")
    bills = sorted(matched.bills.values(),
                   key=lambda b: (b.status == ENDED, b.notes == 0, b.cadence, -b.payments, b.id))
    cadences = Counter(b.cadence for b in bills if b.status != ENDED)
    lines.append(f"bills: {len(bills)} ({sum(1 for b in bills if b.status == ENDED)} ended?); "
                 f"with notes {sum(1 for b in bills if b.notes)}, without "
                 f"{sum(1 for b in bills if not b.notes)}; not ended, by cadence: "
                 + ", ".join(f"{k} {v}" for k, v in sorted(cadences.items())))
    lines.append("  payee (as the bank writes it) | cadence | payments | notes | status")
    for b in bills:
        payee = _safe_name(b.payee) + ("" if b.account else " (no account)")
        lines.append(f"  {payee} | {b.cadence} | {b.payments} | {b.notes} | {_status_words(b)}")
    tiers = Counter(r.tier for r in matched.results if r.tier)
    lines.append(f"notes paired: tier A {tiers[TIER_A]}, tier C {tiers[TIER_C]}")
    routes = Counter(r.route for r in matched.results)
    lines.append("notes by route to their bill: " + ", ".join(
        f"{k} {routes[k]}" for k in ROUTES if routes[k]))
    kinds = Counter(r.finding for r in matched.results)
    lines.append("findings:")
    lines.extend(f"  {kind}: {kinds[kind]}" for kind in FINDINGS if kinds[kind])
    marks = Counter(m for r in matched.results for m in r.marks)
    lines.extend(f"  marked {mark}: {n}" for mark, n in sorted(marks.items()))
    lines.append(f"  {UNNOTED}: {sum(len(v) for v in matched.unnoted.values())}")
    late = [b for b in bills if b.expectation.get("state") == EXPECTED_NOT_SEEN]
    lines.append(f"{EXPECTED_NOT_SEEN}: {len(late)}")
    lines.extend(f"  {_safe_name(b.payee)}: {b.expectation['days_late']} days late"
                 for b in sorted(late, key=lambda b: -b.expectation["days_late"]))
    new = [r for r in matched.results if NEW_ACCOUNT in r.marks]
    lines.append(f"{NEW_ACCOUNT}: {len(new)}")
    lines.extend(f"  {_safe_label(r.note.provider_label) or '(no paid_to)'} {r.note.month}; "
                 f"usually paid to {_safe_name(next(iter(r.observations.values()), '?'))}"
                 for r in new)
    return "\n".join(lines)


def backtest_summary(state_dir: os.PathLike | str, now: datetime, cut: str) -> str:
    """`aish paysig --backtest YYYY-MM`. Counts only."""
    inputs = _inputs(Path(state_dir), now)
    if inputs.ledger is None:
        return f"could not check: {inputs.unreadable}"
    as_of = min(now.date(), _reach(inputs.ledger) or now.date())
    result = backtest(inputs.notes, read_payments(inputs.ledger).payments, cut, as_of)
    paid = result.paid
    return "\n".join([
        f"backtest: bills learned from the bank before {cut} ({result.bills}); notes "
        f"{cut} to {_month_of(as_of)} matched (bank to {as_of.isoformat()}), "
        f"{inputs.invoice_records} invoice records",
        f"  paid notes: tier A {paid['A']}, finding B {paid['B']}, tier C {paid['C']}, "
        f"none {paid['none']}",
        "  paid notes with no invoice account, by route: " + (", ".join(
            f"{k} {result.routes[k]}" for k in ROUTES if result.routes[k]) or "none"),
        f"  paid notes with no read invoice: {result.without_invoice}, matched by C "
        f"{result.c_without_invoice}",
        f"  A-matched notes by C alone: same payment {result.c_agrees}, another "
        f"{result.c_disagrees}, none {result.c_silent}",
        f"  unpaid notes: {result.unpaid}, payment found anyway {result.unpaid_found}",
    ])
