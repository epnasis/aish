"""Invoice facts: what an invoice's text states about paying it, by code.

The payment notes mostly carry no amount and no payee account; the invoices
attached to them do, and those match the bank ledger exactly
(`docs/payments.md`, "Invoice facts"). This module turns an invoice's text —
a document rendition's text layer and OCR — into a small set of typed FACTS.
It is pure: no I/O, no clock, no model.

**An account is a checksum, never a digit run.** A Polish NRB (26 digits, any
of the three printed groupings, optional `PL`, a line break allowed between
groups) or an IBAN is kept only when ISO 13616 mod-97 holds, normalised with
the repo's one coordinate normaliser (`mailcases.normalize_coordinate`).

**An amount needs a decimal part or a currency beside it.** That is what keeps
a phone number, a NIP, a REGON, a postal code and a date out. Integer minor
units, no float anywhere.

**Keywords flag, they never choose.** Every amount, date and account is kept;
a keyword on the same or an adjacent line only flags it. Which of several
flagged amounts is the one to pay is a later matcher's question, answered
against the bank ledger — not this module's guess.

**No free text is kept** except the invoice-number candidates, and those only
as the whole value after an invoice-number keyword, capped.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import date
from typing import Any

from . import mailcases, vocab

#: Bumped whenever extraction changes, so every invoice already read is
#: re-extracted once under the new rules instead of keeping the old facts.
EXTRACTOR_VERSION = 3

INVOICE_VALUE_CAP = 40

# The keyword lists below are matched against INVOICE TEXT, so they are
# declared and counted (docs/vocabularies.md). Folded both sides: entries are
# written with their diacritics and folded at compile time; the text is folded
# before matching. A miss BREAKS a flag quietly (the fact is still extracted,
# only unflagged), and most lines carry none of them, so none is demanded.
_PAYABLE_WORDS = vocab.declare(
    "invoicefacts._PAYABLE_WORDS",
    ("razem do zapłaty", "kwota do zapłaty", "do zapłaty", "pozostało do zapłaty",
     "należność", "saldo", "total", "amount due", "total due", "balance due"),
    languages="PL + EN",
    on_miss=vocab.BREAKS,
    demanded=False,
    structural="none — every amount is extracted whatever its line says; this only flags",
    note="A miss is a payable amount left unflagged among the document's amounts.",
)
_DUE_WORDS = vocab.declare(
    "invoicefacts._DUE_WORDS",
    ("termin płatności", "termin zapłaty", "płatne do", "płatny do", "zapłać do",
     "due date", "payment due"),
    languages="PL + EN",
    on_miss=vocab.BREAKS,
    demanded=False,
    structural="none — every date is extracted; this only flags",
    note="A miss is a due date left unflagged among the document's dates.",
)
_INVOICE_WORDS = vocab.declare(
    "invoicefacts._INVOICE_WORDS",
    ("faktura vat nr", "faktura nr", "faktury nr", "numer faktury", "nr faktury",
     "nr dokumentu", "numer dokumentu", "polisa nr", "nr polisy", "numer polisy",
     "invoice no", "invoice number", "invoice nr"),
    languages="PL + EN",
    on_miss=vocab.BREAKS,
    demanded=False,
    note="A miss is no invoice-number candidate: nothing else proposes one.",
)
_ACCOUNT_WORDS = vocab.declare(
    "invoicefacts._ACCOUNT_WORDS",
    ("numer rachunku", "nr rachunku", "rachunek bankowy", "numer konta", "nr konta",
     "na konto", "na rachunek", "wpłat", "przelew", "iban", "bank account",
     "account number"),
    languages="PL + EN",
    on_miss=vocab.BREAKS,
    demanded=False,
    structural="the mod-97 checksum decides what IS an account; this only says it sits "
    "near a payment word",
    note="A miss is a valid account recorded without its near-keyword flag. `wpłat` is a "
    "stem (wpłata, wpłaty, wpłacać).",
)
#: Polish month names (nominative and genitive), for `10 listopada 2026`.
_MONTHS = vocab.declare(
    "invoicefacts._MONTHS",
    {
        "styczeń": 1, "stycznia": 1, "luty": 2, "lutego": 2, "marzec": 3, "marca": 3,
        "kwiecień": 4, "kwietnia": 4, "maj": 5, "maja": 5, "czerwiec": 6, "czerwca": 6,
        "lipiec": 7, "lipca": 7, "sierpień": 8, "sierpnia": 8, "wrzesień": 9,
        "września": 9, "październik": 10, "października": 10, "listopad": 11,
        "listopada": 11, "grudzień": 12, "grudnia": 12,
    },
    languages="PL",
    on_miss=vocab.BREAKS,
    demanded=False,
    structural="numeric dates (ISO, DD.MM.YYYY, DD-MM-YYYY) need no word",
    note="A miss is a written-out date not extracted.",
)

# Characters an invoice uses as a space inside a number: NBSP, NNBSP (the
# French/Polish thousands separator), thin and figure spaces.
_SPACES = {" ", " ", " ", " ", " ", " "}
_MINUS = {"−"}  # the typographic minus; en/em dashes are separators, not signs


def _fold_char(ch: str) -> str:
    """Exactly ONE character for one: positions in the folded text are
    positions in the original, so a value found in the folded text can be cut
    from the original with its case intact."""
    if ch in _SPACES:
        return " "
    if ch in _MINUS:
        return "-"
    if ch in "łŁ":
        return "l"
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    low = base.lower() if len(base) == 1 else ch.lower()
    return low if len(low) == 1 else ch


def fold(text: str) -> str:
    """Diacritics off, lower case, the odd spaces made plain — one char each."""
    return "".join(_fold_char(ch) for ch in unicodedata.normalize("NFC", text or ""))


def _keyword_re(words: Iterable[str]) -> re.Pattern[str]:
    """A word START and no right boundary: Polish inflects the tail
    (`wpłat` -> wpłata, wpłaty; `przelew` -> przelewem)."""
    folded = sorted({fold(w) for w in words}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in folded) + r")")


_PAYABLE_RE = _keyword_re(_PAYABLE_WORDS)
_DUE_RE = _keyword_re(_DUE_WORDS)
_INVOICE_RE = _keyword_re(_INVOICE_WORDS)
_ACCOUNT_RE = _keyword_re(_ACCOUNT_WORDS)
_MONTH_NUMBER = {fold(name): number for name, number in _MONTHS.items()}

# ------------------------------------------------------------------ accounts

_SEP = r"(?:[ \t]*\n[ \t]*|[ \t]{1,2}|-)"
_NRB_RE = re.compile(
    r"(?<![\w])(?:"
    r"\d{26}"
    rf"|\d{{2}}(?:{_SEP}\d{{4}}){{6}}"
    rf"|\d{{4}}(?:{_SEP}\d{{4}}){{5}}{_SEP}\d{{2}}"
    r")(?![\w])"
)
_IBAN_RE = re.compile(
    rf"(?<![\w])[a-z]{{2}}\d{{2}}(?:{_SEP}?[a-z0-9]{{4}}){{2,7}}(?:{_SEP}?[a-z0-9]{{1,4}})?"
)
_GROUP_SPLIT = re.compile(_SEP)


def _iban_candidates(raw: str) -> Iterable[str]:
    """The matched text, then the same with trailing groups dropped: a greedy
    match runs on into a following word ("… 2857 bank"), and only the
    checksum can say where the account ends."""
    parts = [p for p in _GROUP_SPLIT.split(raw) if p]
    for end in range(len(parts), 0, -1):
        compact = "".join(parts[:end])
        if len(compact) < 15:
            break
        yield compact


def _accounts(folded: str, line_of: list[int]) -> dict[str, dict[str, Any]]:
    """{normalised account: {"count", "lines"}}; an occurrence is identified
    by where it ENDS, so one printed account matched by both patterns counts
    once."""
    seen: dict[str, dict[str, Any]] = {}

    def add(value: str, start: int, end: int) -> None:
        entry = seen.setdefault(value, {"ends": set(), "lines": set()})
        entry["ends"].add(end)
        entry["lines"].add(line_of[start])

    for match in _NRB_RE.finditer(folded):
        digits = re.sub(r"\D", "", match.group(0))
        value = mailcases.normalize_coordinate(digits)
        if mailcases.iban_valid(value):
            add(value, match.start(), match.end())
    for match in _IBAN_RE.finditer(folded):
        for compact in _iban_candidates(match.group(0)):
            value = mailcases.normalize_coordinate(compact)
            if mailcases.iban_valid(value):
                # The end of the VALID prefix, not of the greedy match.
                add(value, match.start(), match.start() + _span_of(match.group(0), compact))
                break
    return seen


def _span_of(raw: str, compact: str) -> int:
    """How many characters of `raw` hold the first len(compact) alphanumerics."""
    wanted, taken = len(compact), 0
    for index, ch in enumerate(raw):
        if ch.isalnum():
            taken += 1
            if taken == wanted:
                return index + 1
    return len(raw)


# ------------------------------------------------------------------- amounts

_CUR = r"zl|pln|eur|usd|gbp|chf|€|\$|£"
_AMOUNT_RE = re.compile(
    r"(?<![\w.,/-])"
    rf"(?:(?P<pre>pln|eur|usd|gbp|chf|€|\$|£) ?)?"
    r"(?P<neg>-)?"
    r"(?:(?P<ci>\d{1,3}(?:[ .]\d{3})+|\d+),(?P<cf>\d{2})"
    r"|(?P<di>\d{1,3}(?:[ ,]\d{3})+|\d+)\.(?P<df>\d{2})"
    r"|(?P<ii>\d{1,3}(?:[ .]\d{3})+|\d+))"
    r"(?![\d%]|[.,]\d| %)"
    rf"(?: ?(?P<post>{_CUR})(?![a-z]))?"
)
_CURRENCY_CODE = {"zl": "PLN", "pln": "PLN", "eur": "EUR", "€": "EUR", "usd": "USD",
                  "$": "USD", "gbp": "GBP", "£": "GBP", "chf": "CHF"}


def _minor(whole: str, frac: str) -> int:
    return int(re.sub(r"\D", "", whole)) * 100 + int(frac)


def _amounts_on(line: str, original: str) -> list[tuple[int, str | None, bool]]:
    """(minor units, written currency or None, ambiguous grouping) for every
    amount on one line. `original` is the line before folding, position for
    position, so the separator the document PRINTED can be told apart."""
    out: list[tuple[int, str | None, bool]] = []
    for m in _AMOUNT_RE.finditer(line):
        written = m.group("post") or m.group("pre")
        currency = _CURRENCY_CODE[written] if written else None
        if m.group("ci") is not None:
            group, whole, frac = "ci", m.group("ci"), m.group("cf")
        elif m.group("di") is not None:
            group, whole, frac = "di", m.group("di"), m.group("df")
        else:
            # A bare integer is an amount only with its currency beside it:
            # without one it is a phone number, a NIP, a year or a quantity.
            if currency is None:
                continue
            group, whole, frac = "ii", m.group("ii"), "00"
        # A PLAIN space inside the number may be a thousands separator or the
        # gap between a quantity and a price ("2 234,56"): both readings are
        # kept, both marked. A typographic space (NBSP, NNBSP, thin) is a
        # separator a typesetter chose, so it is not ambiguous.
        start = m.start(group)
        plain_space = any(
            original[start + i] == " " for i, ch in enumerate(whole) if ch == " "
        )
        sign = -1 if m.group("neg") else 1
        out.append((sign * _minor(whole, frac), currency, plain_space))
        if plain_space:
            shorter = whole.split(" ", 1)[1]
            out.append((_minor(shorter, frac), currency, True))
    return out


# --------------------------------------------------------------------- dates

_ISO_RE = re.compile(r"(?<![\d.\-/])(\d{4})-(\d{2})-(\d{2})(?![\d])")
_DMY_RE = re.compile(r"(?<![\d.\-/])(\d{1,2})([.\-/])(\d{1,2})\2(\d{4})(?![\d])")
_WORDS_RE = re.compile(
    r"(?<![\d])(\d{1,2}) +("
    + "|".join(sorted(map(re.escape, _MONTH_NUMBER), key=len, reverse=True))
    + r")(?![a-z]) +(\d{4})(?![\d])"
)


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _dates_on(line: str) -> list[str]:
    out: list[str] = []
    for m in _ISO_RE.finditer(line):
        if value := _iso(int(m.group(1)), int(m.group(2)), int(m.group(3))):
            out.append(value)
    for m in _DMY_RE.finditer(line):
        if value := _iso(int(m.group(4)), int(m.group(3)), int(m.group(1))):
            out.append(value)
    for m in _WORDS_RE.finditer(line):
        if value := _iso(int(m.group(3)), _MONTH_NUMBER[m.group(2)], int(m.group(1))):
            out.append(value)
    return out


# ------------------------------------------------------------ invoice numbers

_VALUE_LEAD = re.compile(r"^[.:#\s]*(?:(?:nr|no)\.?(?=[\s\d:#]))?[.:#\s]*")
_VALUE_END = re.compile(r"\s{2,}|\|")
#: Keywords naming too general a document to trust what follows, unless it is
#: short and led by a digit-bearing token.
_WEAK_INVOICE_WORDS = frozenset(fold(w) for w in ("nr dokumentu", "numer dokumentu"))
_WEAK_MAX_TOKENS = 3
_CODE_PREFIX = re.compile(r"[A-Z]{1,4}|[/\-.]")
_CODE_FOLLOW = re.compile(r"[/\-.]+|.*\d.*")


def _code_run(tokens: list[str]) -> str | None:
    """The first run of tokens holding a digit, with the code-like tokens
    right beside it: an upper-case prefix of up to four letters before it
    (`FV 123/2026`), digit-bearing tokens or separators after it. A word
    ends the run, so "FV/1/2026 z dnia ..." is `FV/1/2026`, and a person's
    name beside a number is never kept."""
    first = next((i for i, t in enumerate(tokens) if any(ch.isdigit() for ch in t)), None)
    if first is None:
        return None
    start = first
    while start > 0 and _CODE_PREFIX.fullmatch(tokens[start - 1]):
        start -= 1
    end = first
    while end + 1 < len(tokens) and _CODE_FOLLOW.fullmatch(tokens[end + 1]):
        end += 1
    return " ".join(tokens[start:end + 1])


def _whole_value(original: str, folded: str, start: int, weak: bool = False) -> str | None:
    """The value after a keyword: the text to the end of the line or the
    table cell (or a wide gap), cut to its code run (`_code_run`), capped,
    kept with its case. None when it holds no digit (a heading, not a
    number), or, after a weak keyword, when it is not short and led by a
    digit-bearing token."""
    lead = _VALUE_LEAD.match(folded[start:])
    begin = start + (lead.end() if lead else 0)
    cut = _VALUE_END.search(folded, begin)
    end = cut.start() if cut else len(folded)
    tokens = original[begin:end].strip().strip(".,;").split()
    if weak and (not tokens or len(tokens) > _WEAK_MAX_TOKENS
                 or not any(ch.isdigit() for ch in tokens[0])):
        return None
    value = _code_run(tokens)
    if value is None:
        return None
    if weak and value.replace(" ", "").isdigit():
        # A bare digit run after a document keyword is shaped like an
        # identity-card number, a PESEL or a NIP: a code needs a letter or a
        # separator here.
        return None
    value = value.strip(".,;")
    return value if len(value) <= INVOICE_VALUE_CAP else value[:INVOICE_VALUE_CAP] + "…"


# ------------------------------------------------------------------ assembly

# The structure a document rendition adds around a page's own words: page
# markers and OCR delimiters carry digits that are not the invoice's.
_STRUCTURE_RE = re.compile(
    r"^(?:\[page \d+ of \d+\]|--- (?:end of )?OCR text, page \d+ ---)$"
)


def _proximity(index: int, flags: list[bool]) -> str | None:
    if flags[index]:
        return "line"
    if (index > 0 and flags[index - 1]) or (index + 1 < len(flags) and flags[index + 1]):
        return "adjacent"
    return None


_RANK = {"line": 2, "adjacent": 1, None: 0}


def _better(old: str | None, new: str | None) -> str | None:
    return new if _RANK[new] > _RANK[old] else old


def page_facts(text: str) -> dict[str, Any]:
    """The facts ONE page (or any text) states, with an unmarked amount's
    currency left unresolved: which currency a document defaults to is a
    property of the whole document (`merge`). Pure."""
    original_lines = [
        "" if _STRUCTURE_RE.match(line.strip()) else line
        for line in unicodedata.normalize("NFC", text or "").splitlines()
    ]
    lines = [fold(line) for line in original_lines]
    payable = [bool(_PAYABLE_RE.search(line)) for line in lines]
    due = [bool(_DUE_RE.search(line)) for line in lines]
    account_word = [bool(_ACCOUNT_RE.search(line)) for line in lines]
    invoice_word = [bool(_INVOICE_RE.search(line)) for line in lines]
    near_payment = [a or p for a, p in zip(account_word, payable, strict=True)]
    # One consultation per list per text, each named literally so the
    # catalogue's sweep (tests/test_vocab.py) can see the call site.
    vocab.note("invoicefacts._PAYABLE_WORDS", matched=any(payable))
    vocab.note("invoicefacts._DUE_WORDS", matched=any(due))
    vocab.note("invoicefacts._ACCOUNT_WORDS", matched=any(account_word))
    vocab.note("invoicefacts._INVOICE_WORDS", matched=any(invoice_word))

    # accounts, over the whole text so one may cross a line break
    joined = "\n".join(lines)
    line_of: list[int] = []
    for number, line in enumerate(lines):
        line_of.extend([number] * (len(line) + 1))
    accounts = []
    for value, entry in _accounts(joined, line_of).items():
        near = None
        for number in entry["lines"]:
            near = _better(near, _proximity(number, near_payment))
        accounts.append({"account": value, "count": len(entry["ends"]), "near_keyword": near})

    # amounts: one entry per (minor, WRITTEN currency, grouping)
    amounts: dict[tuple[int, str | None, bool], dict[str, Any]] = {}
    for number, line in enumerate(lines):
        for minor, cur, ambiguous in _amounts_on(line, original_lines[number]):
            entry = amounts.setdefault((minor, cur, ambiguous), {
                "minor": minor, "currency": cur, "currency_seen": cur is not None,
                "count": 0, "payable": None, "ambiguous_grouping": ambiguous,
            })
            entry["count"] += 1
            entry["payable"] = _better(entry["payable"], _proximity(number, payable))

    # dates
    dates: dict[str, dict[str, Any]] = {}
    months_matched = False
    for number, line in enumerate(lines):
        months_matched = months_matched or bool(_WORDS_RE.search(line))
        for value in _dates_on(line):
            entry = dates.setdefault(value, {"date": value, "count": 0, "due": None})
            entry["count"] += 1
            entry["due"] = _better(entry["due"], _proximity(number, due))
    vocab.note("invoicefacts._MONTHS", matched=months_matched)

    # invoice-number candidates: the value after the keyword, or the next
    # line when the keyword ends its own line
    invoices: list[dict[str, str]] = []
    for number, line in enumerate(lines):
        for match in _INVOICE_RE.finditer(line):
            weak = match.group(0) in _WEAK_INVOICE_WORDS
            candidate = _whole_value(original_lines[number], line, match.end(), weak)
            where = "line"
            ends_line = not line[match.end():].strip(" :#")
            if candidate is None and ends_line and number + 1 < len(lines):
                candidate = _whole_value(original_lines[number + 1], lines[number + 1], 0, weak)
                where = "next line"
            if candidate and not any(i["value"] == candidate for i in invoices):
                invoices.append({"value": candidate, "where": where})

    return {
        "accounts": accounts,
        "amounts": list(amounts.values()),
        "dates": list(dates.values()),
        "invoice_numbers": invoices,
        "keyword_lines": {
            "payable": sum(payable), "due": sum(due),
            "account": sum(account_word), "invoice": sum(invoice_word),
        },
    }


def merge(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """One document's facts from its pages' (`page_facts`), in page order.

    An unmarked amount takes PLN only when every currency WRITTEN anywhere in
    the document is PLN (`currency_source: document`); otherwise its currency
    stays null. `currency_seen` says whether any occurrence had its currency
    written beside it. An amount stays `ambiguous_grouping` only while every
    occurrence of it is. Pure."""
    accounts: dict[str, dict[str, Any]] = {}
    raw_amounts: dict[tuple[int, str | None, bool], dict[str, Any]] = {}
    dates: dict[str, dict[str, Any]] = {}
    invoices: list[dict[str, str]] = []
    keywords = {"payable": 0, "due": 0, "account": 0, "invoice": 0}
    for page in pages:
        for a in page.get("accounts") or []:
            entry = accounts.setdefault(a["account"], {**a, "count": 0, "near_keyword": None})
            entry["count"] += a["count"]
            entry["near_keyword"] = _better(entry["near_keyword"], a["near_keyword"])
        for a in page.get("amounts") or []:
            written = a["currency"] if a.get("currency_seen") else None
            key = (a["minor"], written, bool(a.get("ambiguous_grouping")))
            entry = raw_amounts.setdefault(key, {"count": 0, "payable": None})
            entry["count"] += a["count"]
            entry["payable"] = _better(entry["payable"], a["payable"])
        for d in page.get("dates") or []:
            entry = dates.setdefault(d["date"], {**d, "count": 0, "due": None})
            entry["count"] += d["count"]
            entry["due"] = _better(entry["due"], d["due"])
        for i in page.get("invoice_numbers") or []:
            if not any(x["value"] == i["value"] for x in invoices):
                invoices.append(dict(i))
        for word in keywords:
            keywords[word] += int((page.get("keyword_lines") or {}).get(word, 0))

    written_all = {cur for (_, cur, _) in raw_amounts if cur}
    default = "PLN" if written_all == {"PLN"} else None
    amounts: dict[tuple[int, str | None], dict[str, Any]] = {}
    for (minor, written, ambiguous), raw in raw_amounts.items():
        currency = written or default
        merged = amounts.get((minor, currency))
        if merged is None:
            amounts[(minor, currency)] = {
                "minor": minor, "currency": currency,
                "currency_source": "written" if written else ("document" if default else None),
                "currency_seen": bool(written), "count": raw["count"],
                "payable": raw["payable"], "ambiguous_grouping": ambiguous,
            }
            continue
        merged["count"] += raw["count"]
        merged["payable"] = _better(merged["payable"], raw["payable"])
        if written:
            merged["currency_source"], merged["currency_seen"] = "written", True
        merged["ambiguous_grouping"] = merged["ambiguous_grouping"] and ambiguous
    return {
        "accounts": list(accounts.values()),
        "amounts": list(amounts.values()),
        "dates": list(dates.values()),
        "invoice_numbers": invoices,
        "keyword_lines": keywords,
    }


def extract(text: str) -> dict[str, Any]:
    """The facts a whole text states: `merge` of it as one page. Pure."""
    return merge([page_facts(text)])
