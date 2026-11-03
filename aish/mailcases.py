"""Case memory, minimal, and the fuzzy correlator (#142 W5, slice S1).

A **case** is a piece of the owner's life that several mails belong to: an
order, a parcel, a booking, an invoice. S1 keeps the smallest version the
correlator and the backtest need — cases with their accumulated keys, their
mails, the parties and authenticated sender domains seen on them, the amounts,
and the payment coordinates — and nothing a later slice owns (closure, the
follow-up queue, the owner's surface are S3).

**Everything here is code.** Keys are found by regex, amounts by regex, IBANs
by regex AND the mod-97 checksum. The Reader's judgements never write a key,
never admit an alias and never merge a case: a judged output that minted a
permanent match key would be oversight the model could appoint (#295 P3).
What a Reader DOES propose — "this mail belongs to case c2" from a sender not
admitted on it — is recorded as a proposal with a count, and admits nothing.

**Alias admission is deterministic corroboration only, and the corroboration
is HIS.** A sender domain is admitted on a case when it has been seen on the
case in AUTHENTICATED mail and the owner then acted on the case TOWARD that
domain — he wrote to an address at it, or replied to a mail from it. Co-
occurrence alone is not enough: DMARC proves a sender controls its own domain,
not that it is his counterparty, and an order or tracking number is the kind
of string that leaks — an attacker passing DMARC from his own domain with the
owner's real order number in the body must not become an admitted sender on
that order. The owner-act path for admitting an alias by hand is S3.

**Frozen clocks.** Every fact carries the position `(at, digest)` of the mail
that produced it, and every query answers from facts STRICTLY BEFORE the
reviewed mail — so a mail's own keys never correlate it with itself, and the
backtest's March mail never sees April's case state.

**Persistence**: one JSON file per case under `<state>/mail/cases/`, one
writer (`CaseStore`), `atomic_write`, an unreadable file counted and skipped.
Eviction is whole-unit: the file goes and a dated tombstone says it went.

`docs/mail-assistant.md`.
"""

from __future__ import annotations

import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atomic_write, mailindex, vocab

CASES_DIRNAME = "cases"
TOMBSTONES_DIRNAME = "tombstones"
# The owner's case acts, one JSON line each, appended by `CaseStore.act` only.
ACTS_NAME = "case-acts.jsonl"

OPEN = "open"
CLOSED = "closed"
# How an address became a party of a case (its `via`). Only one HE addressed
# — or a domain admitted by his act toward it — is someone he dealt with.
OWNER_ADDRESSED = "owner"
INBOUND = "inbound"
#: How an address came to be admitted on a case (`Case.admissions`): his own
#: mail addressed to it after it was seen there authenticated, or his trust.
BY_OWNER_ACT = "owner_act_toward_sender"
BY_OWNER_TRUST = "owner_trust"
# The kind of a key he typed when he created a case: matched literally
# (`_says_key`), where an extracted key needs its introducing word.
OWNER_KEY = "yours"

# The owner's verbs (`CaseStore.act`). Each is HIS act: nothing in the
# pipeline calls one, and the agent offers them only through a card in a task
# he sent (`docs/mail-assistant.md` §The case verbs).
CLOSE, REOPEN, MERGE, MOVE, RENAME, DISMISS, RESTORE, NOTE, REMIND, CREATE = (
    "close", "reopen", "merge", "move", "rename", "dismiss", "restore", "note", "remind",
    "create",
)
TRUST, UNTRUST = "trust", "untrust"
VERBS = (CLOSE, REOPEN, MERGE, MOVE, RENAME, DISMISS, RESTORE, NOTE, REMIND, CREATE, TRUST,
         UNTRUST)

#: The address grammar of the trust verb — the ONE authority, read by the
#: verb's check, the report's chip and the server's link fence
#: (`server._REPORT_FOREIGN_LINK`). The address comes from an inbound header,
#: so this grammar is the fence: nothing outside it reaches a link or a card.
TRUST_ADDRESS_PATTERN = (
    r"[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9])?"
    r"@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
#: What a case id looks like — code-made, `case-` and twelve hex.
CASE_ID_PATTERN = r"case-[0-9a-f]{12}"
_DIGEST_PATTERN = r"[0-9a-f]{64}"


def trust_address(value: Any) -> str:
    """`value` as a trustable address, lowercased, or ValueError: exactly the
    grammar's, never an address with anything else in it."""
    address = str(value or "").strip().lower()
    if not re.fullmatch(TRUST_ADDRESS_PATTERN, address) or len(address) > 254:
        raise ValueError("an address to trust must be one plain address (name@domain)")
    return address


#: The chip verbs and their words' shape — one authority with the server's
#: link fence (`server._REPORT_FOREIGN_LINK`).
CHIP_VERBS = (TRUST, UNTRUST)


def trust_chip_text(address: str, case_id: str, verb: str = TRUST) -> str:
    """The words the report's trust (or untrust) chip sends — code-built from
    a validated address and a case id, or "" when either is not."""
    try:
        address = trust_address(address)
    except ValueError:
        return ""
    if verb not in CHIP_VERBS or not re.fullmatch(CASE_ID_PATTERN, case_id or ""):
        return ""
    return f"{verb} sender {address} on case {case_id}"

CASE_TITLE_CHARS = 80
CASE_NOTE_CHARS = 400
# A case verb waits this long for a running watcher pass to let go of the
# store; past it the act is refused and says so (`store_lock`).
STORE_LOCK_WAIT_S = 20.0

MAX_CANDIDATES = 5
TIME_WINDOW_DAYS = 7  # a courier mail days after the store's
RECENT_DAYS = 30
NAME_SIMILARITY = 0.85  # difflib ratio between display names

# The words that introduce a reference number. Consulted once per mail; a
# miss BREAKS correlation by key quietly (the mail still gets candidates by
# amount, name and time), and matching nothing is the normal answer for most
# mail, so it is not demanded.
_KEY_WORDS = vocab.declare(
    "mailcases._KEY_WORDS",
    (
        "order", "zamówienie", "zamowienie", "bestellung", "commande",
        "tracking", "przesyłka", "przesylka", "paczka", "parcel", "shipment",
        "sendung", "awb", "booking", "rezerwacja", "reservation",
        "confirmation", "pnr", "reference", "invoice", "faktura", "rachunek",
    ),
    languages="EN + PL + DE + FR",
    on_miss=vocab.BREAKS,
    demanded=False,
    structural="the UPS `1Z` shape and the IBAN checksum need no introducing word",
    note="A miss is a case key not found: the mail is still correlated by amount, "
    "name and time window.",
)

# Polish declines these nouns ("Numer paczki", "do zamówienia", "przesyłkę"),
# and a whole-word entry above finds only the nominative. Each stem is an
# introducing word with a BOUNDED ending — at most `STEM_SUFFIX_MAX` letters —
# so `paczk` finds paczki / paczkę / paczkami but never "paczkomat", whose
# locker id ("WAW45H") would otherwise read as a tracking key and link
# unrelated parcels to one locker. The whole words keep their unbounded tail
# (German "Bestellungsnummer"). Counted under its own name.
_KEY_STEMS = vocab.declare(
    "mailcases._KEY_STEMS",
    ("zamówieni", "zamowieni", "przesyłk", "przesylk", "paczk", "rezerwacj", "faktur",
     "rachun"),
    languages="PL (inflected stems)",
    on_miss=vocab.BREAKS,
    demanded=False,
    structural="the whole-word list above, and the UPS `1Z` shape",
    note="A miss is a key in an inflected form not found (the before-picture: 0 of the "
    "owner's InPost notices yielded their parcel number). A false match is recorded on a "
    "case only from his own or an authenticated mail — other mail may attach to a case by "
    "it and characterises nothing; the bounded ending keeps compounds like 'paczkomat' out, "
    "and without an identity marker a short all-digit token (a PIN) is no key.",
)
STEM_SUFFIX_MAX = 3
# Polish declension alternates a stem's LAST consonant in the locative and
# dative (paczka -> paczce, przesyłka -> przesyłce, faktura -> fakturze). One
# rule table, applied by code to EVERY stem (`_stem_forms`), so a new stem gets
# its alternations without anyone writing them: the method, not the word.
_STEM_ALTERNATIONS = (("k", "c"), ("g", "dz"), ("ch", "sz"), ("r", "rz"), ("t", "ci"),
                      ("d", "dzi"), ("ł", "l"))


def _stem_forms(stems: tuple[str, ...]) -> dict[str, str]:
    """Every form a stem may take before its bounded ending -> the stem."""
    forms = {stem: stem for stem in stems}
    for stem in stems:
        for tail, alternate in _STEM_ALTERNATIONS:
            if stem.endswith(tail):
                forms.setdefault(stem[: -len(tail)] + alternate, stem)
    return forms


_STEM_FORMS = _stem_forms(tuple(_KEY_STEMS))

_KEY_KINDS = {
    "order": ("order", "zamówienie", "zamowienie", "bestellung", "commande", "zamówieni",
              "zamowieni"),
    "tracking": ("tracking", "przesyłka", "przesylka", "paczka", "parcel", "shipment",
                 "sendung", "awb", "przesyłk", "przesylk", "paczk"),
    "booking": ("booking", "rezerwacja", "reservation", "confirmation", "pnr", "reference",
                "rezerwacj"),
    "invoice": ("invoice", "faktura", "rachunek", "faktur", "rachun"),
}

# The words that say a token IDENTIFIES the matter ("Numer paczki: …",
# "zamówienia nr …"). Under one of them a stem-introduced token is a key in
# any key shape; without one it must be identifier-shaped (`_identifier_shaped`).
# Matched against mail text, so declared and counted. One authority for BOTH
# places a marker may stand (before the stem, after it).
_IDENTITY_MARKERS = vocab.declare(
    "mailcases._IDENTITY_MARKERS",
    ("numer", "nr", "no", "number", "nummer"),
    languages="PL + EN + DE",
    on_miss=vocab.BREAKS,
    demanded=False,
    structural="`_identifier_shaped`: a token that is a reference by its own shape needs no marker",
    note="A miss is a short all-digit reference after an inflected stem with no marker: not "
    "a key (it has the shape of a one-time code). Never consulted for the whole words.",
)
# The connectors that may stand between an introducing word and its token
# without saying it is the matter's number ("order code X9123"); NOT identity
# markers — "kod odbioru" names a one-time secret, not the parcel.
_KEY_CONNECTORS = vocab.declare(
    "mailcases._KEY_CONNECTORS",
    ("code", "kod", "id"),
    languages="EN + PL",
    on_miss=vocab.BREAKS,
    demanded=False,
    note="Pre-existing connectors of the key pattern, declared when the markers were.",
)


def _alternation(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(w) + r"\.?" for w in sorted(words, key=len, reverse=True))


_KEY_PATTERN = re.compile(
    # The leading \b: "border 12345" and "preorder X9123" are not orders.
    r"(?:\b(?P<pre>" + _alternation(_IDENTITY_MARKERS) + r")\s+)?"
    r"\b(?:(?P<word>" + "|".join(re.escape(w) for w in sorted(_KEY_WORDS, key=len, reverse=True))
    + r")\w*|(?P<stem>" + "|".join(re.escape(w) for w in sorted(_STEM_FORMS, key=len,
                                                                 reverse=True))
    + r")\w{0," + str(STEM_SUFFIX_MAX) + r"}\b)"
    + r"\.?\s*(?P<post>" + _alternation((*_IDENTITY_MARKERS, *_KEY_CONNECTORS)) + r")?"
    + r"\s*[:#]?\s*"
    r"(?P<key>[A-Z0-9][A-Z0-9/-]{3,29})\b",
    re.I,
)
_UPS = re.compile(r"\b1Z[0-9A-Z]{16}\b")
# A calendar date after an introducing word ("Data zamówienia: 2026-10-10") is
# a date, not a reference: many unrelated orders share one. Structural.
_DATE_SHAPED = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}")

_CURRENCY_WORDS = vocab.declare(
    "mailcases._CURRENCY_WORDS",
    ("zł", "zl", "pln", "eur", "€", "usd", "$", "gbp", "£", "chf"),
    languages="currency signs and ISO codes",
    on_miss=vocab.BREAKS,
    demanded=False,
    note="A miss is an amount not extracted: one correlation signal fewer.",
)
_CURRENCY_CODE = {"zł": "PLN", "zl": "PLN", "pln": "PLN", "eur": "EUR", "€": "EUR",
                  "usd": "USD", "$": "USD", "gbp": "GBP", "£": "GBP", "chf": "CHF"}
_NUMBER = r"(\d{1,3}(?:[  .]\d{3})*(?:[.,]\d{2})|\d+(?:[.,]\d{2})?)"
_CUR = "(" + "|".join(re.escape(c) for c in _CURRENCY_CODE) + ")"
_AMOUNT_AFTER = re.compile(_NUMBER + r"\s?" + _CUR + r"(?![a-z])", re.I)
_AMOUNT_BEFORE = re.compile(r"(?<![a-z])" + _CUR + r"\s?" + _NUMBER, re.I)

_IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?)\b")
_NRB = re.compile(r"(?<![\w])(\d{2}(?:[ ]?\d{4}){6})(?![\w])")  # a Polish account, no prefix


# ---------------------------------------------------------------- extraction


def _says_key(upper_text: str, key: str) -> bool:
    """Does the (upper-cased) text carry `key` as a whole token?"""
    return re.search(r"(?<![A-Z0-9])" + re.escape(key) + r"(?![A-Z0-9])", upper_text) is not None


def extract_keys(text: str) -> dict[str, str]:
    """{normalized key: kind}. A key must carry a digit — `order confirmed`
    has no reference in it."""
    out: dict[str, str] = {}
    by_word = by_stem = by_connector = False
    for match in _KEY_PATTERN.finditer(text or ""):
        key = match.group("key").upper().strip("-/")
        if not any(ch.isdigit() for ch in key) or len(key) < 4 or _DATE_SHAPED.fullmatch(key):
            continue
        if match.group("stem") and not _governed(match) and not _identifier_shaped(key):
            continue
        word = (match.group("word") or match.group("stem")).lower()
        word = _STEM_FORMS.get(word, word)
        kind = next((k for k, words in _KEY_KINDS.items() if word in words), "reference")
        if match.group("stem"):
            by_stem = True
        else:
            by_word = True
        post = (match.group("post") or "").rstrip(".").lower()
        by_connector = by_connector or post in _KEY_CONNECTORS
        out.setdefault(key, kind)
    for match in _UPS.finditer(text or ""):
        out.setdefault(match.group(0).upper(), "tracking")
    # Each list counted by what IT matched: a mail can hit both.
    vocab.note("mailcases._KEY_WORDS", matched=by_word)
    vocab.note("mailcases._KEY_STEMS", matched=by_stem)
    vocab.note("mailcases._KEY_CONNECTORS", matched=by_connector)
    return out




#: An all-digit token shorter than this, with no identity marker, has the
#: shape of a one-time code or PIN (4-6 digits), not of a matter's reference.
UNGOVERNED_DIGITS_MIN = 7


def _identifier_shaped(key: str) -> bool:
    """Does the token carry the shape of a REFERENCE on its own — letters and
    digits mixed ("XYO3K4", "PX7694845874"), or a long number ("261509440")?
    A short all-digit token is the shape of a one-time code ("646235"). Used
    only where no identity marker says what the token is. Structural."""
    letters = any(c.isalpha() for c in key)
    digits = sum(c.isdigit() for c in key)
    return (letters and digits > 0) or digits >= UNGOVERNED_DIGITS_MIN


def _governed(match: re.Match) -> bool:
    """Is a stem-introduced token directly governed by an identity marker,
    before the stem or right after it? The genitive forms the stems exist for
    are exactly the ones a marker governs ("numer paczki"); without one the
    inflected noun only stands near a token ("PIN do odbioru paczki: 646235")."""
    marks = (match.group("pre"), match.group("post"))
    governed = any(m and m.rstrip(".").lower() in _IDENTITY_MARKERS for m in marks)
    vocab.note("mailcases._IDENTITY_MARKERS", matched=governed)
    return governed


def mail_keys(subject: str, text: str, reader_kind: str = "") -> dict[str, str]:
    """The keys ONE MAIL contributes — none at all when the mail is a one-time
    code or sign-in link by the classification the report already uses
    (`mailreport.is_one_time_code`, over the Reader's kind and the subject):
    such a mail's references are one-time secrets, not a matter's identity
    (round-4 note 3: a 7+-digit OTP or a mixed one-time code after an
    introducing word). Restrict-only: it can drop keys, never add one. At
    ingest there is no reading yet, so only the subject half applies there;
    the readers apply the Reader's kind as well."""
    from . import mailreport  # mailreport imports this module

    if mailreport.is_one_time_code(reader_kind, subject):
        return {}
    return extract_keys(text)


def _cents(number: str) -> int | None:
    text = number.replace(" ", " ").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d{2})?", text):
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{2}", text):
        text = text.replace(",", ".")
    try:
        return round(float(text) * 100)
    except ValueError:
        return None


def extract_amounts(text: str) -> tuple[tuple[str, int], ...]:
    """(currency, cents) for every amount with a currency next to it."""
    found: list[tuple[str, int]] = []
    # A zero amount ("0,00 zł to pay") correlates nothing, so it is not kept.
    for number, currency in _AMOUNT_AFTER.findall(text or ""):
        cents = _cents(number)
        if cents:
            found.append((_CURRENCY_CODE[currency.lower()], cents))
    for currency, number in _AMOUNT_BEFORE.findall(text or ""):
        cents = _cents(number)
        if cents:
            found.append((_CURRENCY_CODE[currency.lower()], cents))
    vocab.note("mailcases._CURRENCY_WORDS", matched=bool(found))
    return tuple(dict.fromkeys(found))


def iban_valid(value: str) -> bool:
    """ISO 13616 mod-97: the structural check that needs no list of banks."""
    compact = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact):
        return False
    rotated = compact[4:] + compact[:4]
    digits = "".join(str(int(ch, 36)) for ch in rotated)
    return int(digits) % 97 == 1


def normalize_coordinate(value: str) -> str:
    """An account string compared the way a bank would: no spaces, upper case,
    and a bare 26-digit Polish account read as its PL IBAN."""
    compact = re.sub(r"[\s-]+", "", value or "").upper()
    if re.fullmatch(r"\d{26}", compact):
        compact = "PL" + compact
    return compact


def extract_coordinates(text: str) -> tuple[str, ...]:
    """Every checksum-valid IBAN (and prefix-less Polish account) in `text`."""
    out = []
    for raw in _IBAN.findall(text or ""):
        value = normalize_coordinate(raw)
        if iban_valid(value):
            out.append(value)
    for raw in _NRB.findall(text or ""):
        value = normalize_coordinate(raw)
        if iban_valid(value):
            out.append(value)
    return tuple(dict.fromkeys(out))


# ---------------------------------------------------------------- the case


Position = tuple[float, str]


def _pos(raw: Any) -> Position:
    return (float(raw[0]), str(raw[1]))


@dataclass
class Case:
    id: str
    created: Position
    created_by: str  # "owner" (a mail he sent carried the key) | "inbound"
    keys: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> {kind, at}
    mails: list[Position] = field(default_factory=list)
    parties: dict[str, dict[str, Any]] = field(default_factory=dict)  # addr -> {at, name}
    domains: dict[str, Position] = field(default_factory=dict)  # authenticated senders
    owner_acts: list[Position] = field(default_factory=list)
    amounts: list[dict[str, Any]] = field(default_factory=list)  # {currency, cents, at}
    coordinates: list[dict[str, Any]] = field(default_factory=list)  # {value, at}
    proposals: dict[str, int] = field(default_factory=dict)  # "domain" -> count
    # address -> {at, by, act}: an authenticated sender on the case whom his
    # act did NOT address, though it addressed another address at the same
    # domain (`mailindex.AIM_SIBLING`). Recorded so the withholding is
    # observable; it admits nothing.
    withheld: dict[str, dict[str, Any]] = field(default_factory=dict)
    # What the OWNER said about the case (the case verbs, `CaseStore.act`).
    # Code never writes these from mail: a title from his rename, a closure
    # from his close, and so on. Files written before them read as an open,
    # untitled, visible case.
    title: str = ""
    status: str = "open"  # OPEN | CLOSED
    closed_at: float | None = None  # wall time of his close
    dismissed: bool = False  # "not a case": hidden from the list, kept, undoable
    merged_into: str = ""  # he merged this case into another; kept, never observed
    notes: list[dict[str, Any]] = field(default_factory=list)  # {at, text}
    reminders: list[dict[str, Any]] = field(default_factory=list)  # {at, due, text}
    # {at, address, mail}: an address HE said to trust on this case (the trust
    # verb, `CaseStore.act`) — an owner act, read by `Case.admissions`.
    trusted: list[dict[str, Any]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created": list(self.created),
            "created_by": self.created_by,
            "keys": {k: {**v, "at": list(v["at"])} for k, v in self.keys.items()},
            "mails": [list(p) for p in self.mails],
            "parties": {a: {**v, "at": list(v["at"]),
                            **({"addressed": list(v["addressed"])}
                               if v.get("addressed") is not None else {})}
                        for a, v in self.parties.items()},
            "domains": {d: list(p) for d, p in self.domains.items()},
            "owner_acts": [list(p) for p in self.owner_acts],
            "amounts": [{**a, "at": list(a["at"])} for a in self.amounts],
            "coordinates": [{**c, "at": list(c["at"])} for c in self.coordinates],
            "proposals": dict(self.proposals),
            "withheld": {d: {**v, "at": list(v["at"])} for d, v in self.withheld.items()},
            "title": self.title,
            "status": self.status,
            "closed_at": self.closed_at,
            "dismissed": self.dismissed,
            "merged_into": self.merged_into,
            "notes": [dict(n) for n in self.notes],
            "reminders": [dict(r) for r in self.reminders],
            "trusted": [dict(t) for t in self.trusted],
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Case:
        def stamped(name: str) -> dict[str, dict[str, Any]]:
            return {k: {**v, "at": _pos(v["at"])} for k, v in (raw.get(name) or {}).items()}

        parties = stamped("parties")
        for info in parties.values():
            if info.get("addressed") is not None:
                info["addressed"] = _pos(info["addressed"])
        return cls(
            id=str(raw["id"]),
            created=_pos(raw["created"]),
            created_by=str(raw["created_by"]),
            keys=stamped("keys"),
            mails=[_pos(p) for p in raw.get("mails") or ()],
            parties=parties,
            domains={d: _pos(p) for d, p in (raw.get("domains") or {}).items()},
            owner_acts=[_pos(p) for p in raw.get("owner_acts") or ()],
            amounts=[{**a, "at": _pos(a["at"])} for a in raw.get("amounts") or ()],
            coordinates=[{**c, "at": _pos(c["at"])} for c in raw.get("coordinates") or ()],
            proposals={str(k): int(v) for k, v in (raw.get("proposals") or {}).items()},
            withheld=stamped("withheld"),
            title=str(raw.get("title") or ""),
            status=CLOSED if raw.get("status") == CLOSED else OPEN,
            closed_at=float(raw["closed_at"]) if raw.get("closed_at") is not None else None,
            dismissed=raw.get("dismissed") is True,
            merged_into=str(raw.get("merged_into") or ""),
            notes=[dict(n) for n in raw.get("notes") or () if isinstance(n, dict)],
            reminders=[dict(r) for r in raw.get("reminders") or () if isinstance(r, dict)],
            trusted=[dict(t) for t in raw.get("trusted") or () if isinstance(t, dict)],
        )

    def display_title(self) -> str:
        """His title, else one code derives from the case's first key — its
        kind and the key itself, both regex output, never mail prose."""
        if self.title:
            return self.title
        if self.keys:
            key = min(self.keys, key=lambda k: (self.keys[k]["at"], k))
            return f"{self.keys[key].get('kind') or 'reference'} {key}"
        return f"untitled case {self.id}"

    # -- as-of views: strictly before the reviewed mail

    def keys_before(self, as_of: Position) -> set[str]:
        return {k for k, v in self.keys.items() if v["at"] < as_of}

    def transacted_before(self, as_of: Position) -> bool:
        if self.created_by == "owner" and self.created < as_of:
            return True
        return any(p < as_of for p in self.owner_acts)

    def admissions(self, index: mailindex.MailIndex | None) -> dict[str, dict[str, Any]]:
        """Every address admitted on this case, DERIVED from scratch on every
        call — never stored, so no transient state can ratchet a grant
        (round-8 B2). An address is admitted at his first mail on the case
        that ADDRESSES it exactly (To/Cc/Bcc, `mailindex.aimed_at`) after it
        was seen on the case in authenticated mail, or at his trust of it
        (`trusted`, the case's own record of his act). address -> {at, by,
        act|mail}. With no index the case's mail cannot be read: nothing."""
        if index is None:
            return {}
        seen = authenticated_senders(self, index)
        out: dict[str, dict[str, Any]] = {}
        for position in sorted(self.mails):
            header = index.get(position[1])
            if header is None or header.direction != "out":
                continue
            for address in header.addressed:
                first = seen.get(address)
                if first is not None and first < position and address not in out:
                    out[address] = {"at": position, "by": BY_OWNER_ACT, "act": header.digest}
        for act in self.trusted:
            address = str(act.get("address") or "").lower()
            at = (float(act.get("at") or 0.0), str(act.get("mail") or ""))
            if address and (address not in out or at < tuple(out[address]["at"])):
                out[address] = {"at": at, "by": BY_OWNER_TRUST, "mail": act.get("mail")}
        return out

    def admitted_before(self, as_of: Position, index: mailindex.MailIndex | None) -> set[str]:
        """The ADDRESSES admitted on the case strictly before `as_of`."""
        return {a for a, v in self.admissions(index).items() if tuple(v["at"]) < as_of}

    def admitted_domains_before(self, as_of: Position,
                                index: mailindex.MailIndex | None) -> set[str]:
        """The domains of those addresses — for what is about a DOMAIN (a
        lookalike, which domain an account came from, the Reader's view)."""
        return {mailindex.domain_of(a) for a in self.admitted_before(as_of, index)}

    def coordinates_before(self, as_of: Position,
                           index: mailindex.MailIndex | None = None) -> set[str]:
        """The case's account history as of the mail.

        When the case has ADMITTED senders, only accounts those senders sent
        count. Otherwise a stranger passing DMARC on its own domain could mail
        the order number with its own account first, and that account would
        then read as "the case's account" — the comparison would vouch for
        the very value it exists to question. With no admitted sender yet the
        whole authenticated history is all there is, and that is weaker; it
        is stated in `docs/mail-assistant.md`, not hidden."""
        admitted = self.admitted_domains_before(as_of, index)
        return {
            c["value"] for c in self.coordinates
            if c["at"] < as_of and (not admitted or c.get("domain") in admitted)
        }

    def dealt_with_at(self, address: str, index: mailindex.MailIndex | None,
                      as_of: Position | None = None) -> Position | None:
        """When this party became someone he dealt with on the case: he
        addressed it, or it is admitted there (`admissions`) — before `as_of`
        when given."""
        addressed = self.addressed_at(address, index)
        held = self.admissions(index).get(address)
        via_admission = (tuple(held["at"]) if held is not None
                         and (as_of is None or tuple(held["at"]) < as_of) else None)
        options = [p for p in (addressed, via_admission) if p is not None]
        return min(options) if options else None  # type: ignore[type-var]

    def addressed_at(self, address: str, index: mailindex.MailIndex | None = None
                     ) -> Position | None:
        """When he first ADDRESSED this party on this case, or None. A party
        recorded before provenance existed (no `via`) is derived from his own
        outbound mail on the case when an index is at hand — never assumed."""
        info = self.parties.get(address)
        if info is None:
            return None
        if info.get("addressed") is not None:
            return tuple(info["addressed"])  # type: ignore[return-value]
        if "via" in info or index is None:
            return None
        his = [p for p in self.mails
               if (h := index.get(p[1])) is not None and h.direction == "out"
               and address in h.addressed]
        return min(his) if his else None

    def last_before(self, as_of: Position) -> Position | None:
        earlier = [p for p in self.mails if p < as_of]
        return max(earlier) if earlier else None


# ---------------------------------------------------------------- the store


@dataclass
class StoreHealth:
    loaded: int = 0
    unreadable: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Closure:
    """What the owner's closures release, read once from the store.

    `closed_at`: case id -> when he closed it. `members`: mail digest -> the
    cases it is on NOW (a mail he moved is on its new case)."""

    closed_at: dict[str, float] = field(default_factory=dict)
    members: dict[str, frozenset[str]] = field(default_factory=dict)

    def releases(self, digest: str, read_at: float, recorded_cases: Iterable[str]) -> bool:
        """Is a keeper clamp on this mail released? Only when the mail is on
        at least one case — the cases its record names AND the ones it is on
        now, so a move can only add a case that must close — and EVERY one of
        them was closed at or after aish first read the mail (`read_at`, aish's
        own clock). A mail that arrives on a case after he closed it stays
        held: his close was about the mail he had seen."""
        members = set(self.members.get(digest, ()))
        if not members:
            # The mail must be a MEMBER of a case now (with a trusted closure,
            # `mailitems.trusted_closure`, a trusted one): recorded case ids
            # alone — a record's raw membership, written before the trust law —
            # release nothing (round-5 review).
            return False
        cases = set(recorded_cases) | members
        return all(c in self.closed_at and read_at <= self.closed_at[c] for c in cases)


def _absorb(into: Case, other: Case) -> None:
    """`other`'s facts become `into`'s. The earliest creation wins; an owner
    creation that does not win stays his act on the merged case."""
    if other.created < into.created:
        if into.created_by == "owner":
            into.owner_acts.append(into.created)
        into.created, into.created_by = other.created, other.created_by
    elif other.created_by == "owner":
        into.owner_acts.append(other.created)
    for key, info in other.keys.items():
        if key not in into.keys or info["at"] < into.keys[key]["at"]:
            into.keys[key] = info
    into.mails = sorted(set(into.mails) | set(other.mails))
    for addr, info in other.parties.items():
        held = into.parties.get(addr)
        if held is None or info["at"] < held["at"]:
            into.parties[addr] = {**info}
        kept = into.parties[addr]
        marks = [p for p in (info.get("addressed"), (held or {}).get("addressed")) if p]
        if marks:
            kept["addressed"] = min(tuple(p) for p in marks)
            kept["via"] = OWNER_ADDRESSED
        elif held is not None and ("via" not in info or "via" not in held):
            # One side predates provenance: keep it UNKNOWN (derivable from his
            # own mail on the case), never "inbound" — a merge must not strip
            # the standing the legacy record could still prove.
            kept.pop("via", None)
            kept.pop("authenticated", None)
    for domain, seen in other.domains.items():
        into.domains[domain] = min(seen, into.domains.get(domain, seen))
    # Admissions are derived from the mails and his trust, which both follow
    # the merge; his trust acts and the withholding records travel with it.
    into.trusted += [dict(t) for t in other.trusted]
    for address, info in other.withheld.items():
        into.withheld.setdefault(address, dict(info))
    into.owner_acts = sorted(set(into.owner_acts) | set(other.owner_acts))
    into.amounts += other.amounts
    into.coordinates += other.coordinates
    for domain, count in other.proposals.items():
        into.proposals[domain] = into.proposals.get(domain, 0) + count
    into.notes += other.notes
    into.reminders += other.reminders


@contextlib.contextmanager
def store_lock(
    state_dir: os.PathLike | str,
    wait_s: float = STORE_LOCK_WAIT_S,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[bool]:
    """Yield True while this process holds the mail store's one lock — the
    SAME `watch.lock` a watcher pass and the backfill hold
    (`mailwatch.pass_lock`) — or False after `wait_s` without it. A case verb
    takes it so a pass that loaded the store earlier cannot overwrite his act
    when it saves a case."""
    path = mailindex.mail_dir(state_dir) / "watch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        waited = 0.0
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if waited >= wait_s:
                    yield False
                    return
                sleep(0.5)
                waited += 0.5
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_acts(state_dir: os.PathLike | str) -> list[dict[str, Any]]:
    """The owner's case acts, oldest first. An unreadable line costs that line."""
    path = mailindex.mail_dir(state_dir) / ACTS_NAME
    try:
        text = path.read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


@dataclass(frozen=True)
class Observation:
    """What `observe` did with one mail — for the verdict record, so nothing
    downstream re-derives it."""

    keys: dict[str, str]
    cases: tuple[str, ...]
    created: str = ""
    admitted: tuple[str, ...] = ()  # "case:domain" admissions this mail caused


class CaseStore:
    """The one code writer of `<state>/mail/cases/`. `state_dir=None` keeps it
    in memory (the backtest's scratch arm, the tests)."""

    def __init__(self, state_dir: os.PathLike | str | None = None) -> None:
        root = mailindex.mail_dir(state_dir) if state_dir is not None else None
        self.directory = root / CASES_DIRNAME if root is not None else None
        self.tombstones = root / TOMBSTONES_DIRNAME if root is not None else None
        self.cases: dict[str, Case] = {}
        # Cases he merged into another: kept whole, read by the case verbs and
        # the closure, and by NOTHING that observes or correlates mail — those
        # read `cases`, where the merged-into case now holds everything.
        self.merged: dict[str, Case] = {}
        self.health = StoreHealth()
        if self.directory is not None and self.directory.is_dir():
            for path in sorted(self.directory.glob("*.json")):
                try:
                    case = Case.from_json(json.loads(path.read_text()))
                except (OSError, ValueError, TypeError, KeyError):
                    self.health.unreadable.append(path.name)
                    continue
                (self.merged if case.merged_into else self.cases)[case.id] = case
            self.health.loaded = len(self.cases) + len(self.merged)

    def _save(self, case: Case) -> None:
        if self.directory is not None:
            atomic_write.publish(
                self.directory / f"{case.id}.json",
                json.dumps(case.as_json(), ensure_ascii=False, sort_keys=True),
            )

    def by_key(self, key: str) -> list[Case]:
        return [c for c in self.cases.values() if key in c.keys]

    # -- the one write path for mail

    def observe(
        self,
        header: mailindex.Header,
        body: str,
        authenticated: bool,
        index: mailindex.MailIndex | None = None,
        reader_kind: str = "",
    ) -> Observation:
        """Fold one mail into the case store. Deterministic; idempotent per
        mail (a mail already on a case changes nothing)."""
        position = header.position
        text = f"{header.subject}\n{body}"
        keys = mail_keys(header.subject, text, reader_kind)
        touched: dict[str, Case] = {}
        for key in keys:
            for case in self.by_key(key):
                touched[case.id] = case
        # A key HE gave a case (`create`) is matched as itself, wherever the
        # mail says it: no introducing word is needed, because he named the
        # string. It joins mail to his case the way an extracted key does, and
        # opens nothing.
        upper = text.upper()
        for case in self.cases.values():
            if any(info.get("kind") == OWNER_KEY and _says_key(upper, key)
                   for key, info in case.keys.items()):
                touched[case.id] = case
        if header.direction == "out" and header.in_reply_to and index is not None:
            for parent_id in header.in_reply_to:
                parent = index.reply_parent(parent_id, position)
                if parent is None:
                    continue
                for case in self.cases.values():
                    if parent.position in case.mails:
                        touched[case.id] = case
        created = ""
        if not touched and keys and (header.direction == "out" or authenticated):
            # An unauthenticated stranger cannot plant a case: the key it
            # carries may be real, but its claim to be the counterparty is not.
            first = sorted(keys)[0]
            digest = hashlib.sha256(f"{first}|{position[0]}|{position[1]}".encode()).hexdigest()
            case = Case(
                id=f"case-{digest[:12]}",
                created=position,
                created_by="owner" if header.direction == "out" else "inbound",
            )
            self.cases[case.id] = case
            touched[case.id] = case
            created = case.id
        admitted: list[str] = []
        for case in touched.values():
            if position in case.mails:
                continue
            before = set(case.admissions(index))
            case.mails.append(position)
            # A case's recorded facts come only from his own or AUTHENTICATED
            # mail (`_record_facts`, the one list and the one law).
            self._record_facts(case, header, text, authenticated, keys)
            if header.direction == "out":
                if case.created != position:
                    case.owner_acts.append(position)
                for addr in header.addressed:
                    info = case.parties.setdefault(
                        addr, {"at": position, "name": "", "via": OWNER_ADDRESSED})
                    # A party that joined inbound becomes one he ADDRESSED only
                    # from this act of his, never earlier.
                    if "addressed" not in info or tuple(info["addressed"]) > position:
                        info["addressed"] = position
            else:
                # An inbound sender is a party by joining, which is not trust:
                # its provenance is kept, so nothing counts it as someone he
                # dealt with (`addressed_at`).
                case.parties.setdefault(
                    header.from_addr,
                    {"at": position, "name": mailindex.display_name(header.from_value),
                     "via": INBOUND, "authenticated": bool(authenticated)},
                )
            admitted += [f"{case.id}:{a}" for a in sorted(set(case.admissions(index)) - before)]
            record_withheld(case, index)
            self._save(case)
        return Observation(keys, tuple(sorted(touched)), created, tuple(admitted))

    def record_authenticated(self, case: Case, header: mailindex.Header, body: str,
                             reader_kind: str = "") -> bool:
        """The facts observation records only from TRUSTED mail — for a mail
        already on `case` that was observed unauthenticated and is
        authenticated now (the Tuta recheck): exactly `_record_facts`, keys
        included, so the list of facts and the law have one authority. True
        when anything changed (and was saved)."""
        if header.direction != "in" or header.position not in case.mails:
            return False
        text = f"{header.subject}\n{body}"
        changed = self._record_facts(case, header, text, True,
                                     mail_keys(header.subject, text, reader_kind))
        if changed:
            self._save(case)
        return changed

    @staticmethod
    def _record_facts(case: Case, header: mailindex.Header, text: str, authenticated: bool,
                      keys: dict[str, str]) -> bool:
        """THE facts a case records about one of its mails, and the one law
        for them: written only from his own mail or AUTHENTICATED mail;
        unauthenticated mail is attached and characterises nothing. Keys with
        their kinds from either; the sender's domain, amounts and payment
        accounts from authenticated INBOUND mail. Each at the mail's own
        position, idempotent per fact; a first sighting moves only EARLIER.
        True when anything changed."""
        position = header.position
        trusted = header.direction == "out" or authenticated
        if not trusted:
            return False
        changed = False
        for key, kind in keys.items():
            held = case.keys.get(key)
            if held is None:
                case.keys[key] = {"kind": kind, "at": position}
                changed = True
        if header.direction != "in":
            return changed
        domain = mailindex.domain_of(header.from_addr)
        if domain and (domain not in case.domains or position < case.domains[domain]):
            case.domains[domain] = position
            changed = True
        amounts = {(a["currency"], a["cents"]) for a in case.amounts if a["at"] == position}
        for currency, cents in extract_amounts(text):
            if (currency, cents) not in amounts:
                case.amounts.append({"currency": currency, "cents": cents, "at": position})
                amounts.add((currency, cents))
                changed = True
        values = {c["value"] for c in case.coordinates if c["at"] == position}
        for value in extract_coordinates(text):
            if value not in values:
                case.coordinates.append({"value": value, "at": position, "domain": domain})
                values.add(value)
                changed = True
        return changed


    def propose(self, case_id: str, domain: str) -> None:
        """A Reader said this mail belongs to `case_id`. Counted; admits nothing."""
        case = self.cases.get(case_id)
        if case is None or not domain:
            return
        case.proposals[domain] = case.proposals.get(domain, 0) + 1
        self._save(case)

    def evict(self, case_id: str, reason: str, now: float | None = None) -> bool:
        """Whole-unit eviction: the case file goes and a dated tombstone stays."""
        case = self.cases.pop(case_id, None)
        if case is None:
            return False
        if self.directory is not None and self.tombstones is not None:
            (self.directory / f"{case_id}.json").unlink(missing_ok=True)
            atomic_write.publish(
                self.tombstones / f"{case_id}.json",
                json.dumps({"id": case_id, "at": time.time() if now is None else now,
                            "reason": reason, "keys": len(case.keys),
                            "mails": len(case.mails)}),
            )
        return True

    # -- the owner's verbs: one writer, every act checked again when applied

    def find(self, case_id: str) -> Case | None:
        return self.cases.get(case_id) or self.merged.get(case_id)

    def resolve(self, case_id: str) -> Case | None:
        """The live case `case_id` is, following his merges (`merged_into`),
        or None. Bounded, so a corrupt cycle cannot loop."""
        case = self.find(case_id)
        for _ in range(len(self.merged) + 1):
            if case is None or not case.merged_into:
                return case
            case = self.find(case.merged_into)
        return None

    def check(self, verb: str, fields: dict[str, Any]) -> str:
        """Why this act cannot be applied to the store as it stands, or "".
        Asked when the card is composed AND again under the lock just before
        the act is applied, so a watcher pass between the two cannot make the
        card describe a state that is gone."""
        if verb not in VERBS:
            return f"unknown case verb {verb!r}"
        if verb == CREATE:
            if not str(fields.get("title") or "").strip():
                return "a new case needs a title"
            taken = [k for k in fields.get("keys") or () if self.by_key(k)]
            if taken:
                return f"key {taken[0]} is already on case {self.by_key(taken[0])[0].id}"
            return ""
        case = self.cases.get(str(fields.get("case") or ""))
        if case is None:
            merged = self.merged.get(str(fields.get("case") or ""))
            if merged is not None:
                return f"case {merged.id} was merged into {merged.merged_into}"
            return f"there is no case {fields.get('case')!r}"
        if verb == CLOSE and case.status == CLOSED:
            return f"case {case.id} is already closed"
        if verb == REOPEN and case.status != CLOSED:
            return f"case {case.id} is not closed"
        if verb == DISMISS and case.dismissed:
            return f"case {case.id} is already dismissed"
        if verb == RESTORE and not case.dismissed:
            return f"case {case.id} is not dismissed"
        if verb == RENAME and not str(fields.get("title") or "").strip():
            return "a rename needs a title"
        if verb in (NOTE, REMIND) and not str(fields.get("text") or "").strip():
            return f"a {verb} needs text"
        if verb == REMIND and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fields.get("due") or "")):
            return "a reminder needs a due date as YYYY-MM-DD"
        if verb in (MERGE, MOVE):
            into = self.cases.get(str(fields.get("into") or ""))
            if into is None:
                return f"there is no case {fields.get('into')!r} to {verb} into"
            if into.id == case.id:
                return f"a case cannot be {verb}d into itself"
        if verb == MOVE:
            digest = str(fields.get("mail") or "")
            if not any(p[1] == digest for p in case.mails):
                return f"that mail is not on case {case.id}"
        if verb == TRUST:
            try:
                address = trust_address(fields.get("address"))
            except ValueError as exc:
                return str(exc)
            if any(str(t.get("address")) == address for t in case.trusted):
                return f"{address} is already trusted on case {case.id}"
            mail = str(fields.get("mail") or "")
            if mail and not re.fullmatch(_DIGEST_PATTERN, mail):
                return "the trust's mail must be a mail id"
        if verb == UNTRUST:
            try:
                address = trust_address(fields.get("address"))
            except ValueError as exc:
                return str(exc)
            if not any(str(t.get("address")) == address for t in case.trusted):
                return f"{address} is not trusted on case {case.id}"
        return ""

    def act(self, verb: str, fields: dict[str, Any], *, by: str, now: float) -> dict[str, Any]:
        """Apply one owner act and record it. Raises ValueError (the `check`
        words) when it cannot apply. Returns the act's record, which is also
        appended to the acts ledger."""
        problem = self.check(verb, fields)
        if problem:
            raise ValueError(problem)
        changed: list[Case] = []
        record: dict[str, Any] = {"at": now, "verb": verb, "by": by}
        if verb == CREATE:
            title = str(fields["title"]).strip()[:CASE_TITLE_CHARS]
            salt = 0
            while True:
                digest = hashlib.sha256(f"owner-act|{title}|{now}|{salt}".encode()).hexdigest()
                if self.find(f"case-{digest[:12]}") is None:
                    break
                salt += 1
            case = Case(id=f"case-{digest[:12]}", created=(now, ""), created_by="owner",
                        title=title)
            for key in fields.get("keys") or ():
                case.keys[str(key)] = {"kind": OWNER_KEY, "at": (now, "")}
            self.cases[case.id] = case
            changed.append(case)
            record.update(case=case.id, title=title, keys=sorted(case.keys))
        else:
            case = self.cases[str(fields["case"])]
            record["case"] = case.id
            if verb == CLOSE:
                case.status, case.closed_at = CLOSED, now
            elif verb == REOPEN:
                case.status, case.closed_at = OPEN, None
            elif verb == DISMISS:
                case.dismissed = True
            elif verb == RESTORE:
                case.dismissed = False
            elif verb == TRUST:
                # His act, on the case's own record: `Case.admissions` reads
                # it. A closed or dismissed case keeps it too.
                address = trust_address(fields["address"])
                mail = str(fields.get("mail") or "")
                case.trusted.append({"at": now, "address": address, "mail": mail})
                record.update(address=address, mail=mail)
            elif verb == UNTRUST:
                # His revocation: the trust leaves the case's record, the act
                # stays in the ledger, and `Case.admissions` re-derives.
                address = trust_address(fields["address"])
                case.trusted = [t for t in case.trusted if str(t.get("address")) != address]
                record.update(address=address)
            elif verb == RENAME:
                record["from"] = case.title
                case.title = str(fields["title"]).strip()[:CASE_TITLE_CHARS]
                record["title"] = case.title
            elif verb == NOTE:
                case.notes.append({"at": now, "text": str(fields["text"])[:CASE_NOTE_CHARS]})
            elif verb == REMIND:
                case.reminders.append({"at": now, "due": str(fields["due"]),
                                       "text": str(fields["text"])[:CASE_NOTE_CHARS]})
                record["due"] = str(fields["due"])
            elif verb == MOVE:
                into = self.cases[str(fields["into"])]
                digest = str(fields["mail"])
                moved = [p for p in case.mails if p[1] == digest]
                case.mails = [p for p in case.mails if p[1] != digest]
                into.mails = sorted(set(into.mails) | set(moved))
                changed.append(into)
                record.update(into=into.id, mail=digest)
            elif verb == MERGE:
                into = self.cases[str(fields["into"])]
                _absorb(into, case)
                case.merged_into = into.id
                self.cases.pop(case.id)
                self.merged[case.id] = case
                changed.append(into)
                record["into"] = into.id
            changed.insert(0, case)
        for touched in changed:
            self._save(touched)
        if self.directory is not None:
            path = self.directory.parent / ACTS_NAME
            with path.open("a") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record

    def closure(self) -> Closure:
        """What his closures release: each closed case's closure time (a case
        merged into a closed one closes with it), and each mail's cases now."""
        closed = {c.id: c.closed_at for c in self.cases.values()
                  if c.status == CLOSED and c.closed_at is not None}
        for case in self.merged.values():
            target = self.cases.get(case.merged_into)
            if target is not None and target.id in closed:
                closed[case.id] = closed[target.id]
        members: dict[str, set[str]] = {}
        for case in self.cases.values():
            for _at, digest in case.mails:
                members.setdefault(digest, set()).add(case.id)
        return Closure({k: float(v) for k, v in closed.items() if v is not None},
                       {d: frozenset(ids) for d, ids in members.items()})

    # -- as-of reads

    def admitted_domains(self, as_of: Position,
                         index: mailindex.MailIndex | None) -> frozenset[str]:
        return frozenset(d for c in self.cases.values()
                         for d in c.admitted_domains_before(as_of, index))

    def owner_case_parties(self, as_of: Position,
                           index: mailindex.MailIndex | None = None
                           ) -> dict[str, list[float]]:
        """address -> times at which a case the owner transacted on included it
        AS SOMEONE HE DEALT WITH: a party he addressed on that case, or an
        address admitted there (`Case.admissions`) — never a sender that
        merely joined by quoting a key (round-3 review: an authenticated
        stranger quoting his real order number earned a "case" act, so the
        grade `known`, and third-party history)."""
        out: dict[str, list[float]] = {}
        for case in self.cases.values():
            if not case.transacted_before(as_of):
                continue
            since = case.created if case.created_by == "owner" else min(
                p for p in case.owner_acts if p < as_of
            )
            for addr in case.parties:
                when = case.dealt_with_at(addr, index, as_of)
                if when is not None and when < as_of:
                    out.setdefault(addr, []).append(max(since, when)[0])
        return out


# ---------------------------------------------------------------- the correlator


@dataclass(frozen=True)
class Candidate:
    case_id: str
    score: int
    reasons: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {"case": self.case_id, "score": self.score, "reasons": list(self.reasons)}


_SCORES = {"key": 100, "amount": 30, "name": 20, "window": 10, "recent": 1}


def _name_tokens(case: Case, as_of: Position) -> set[str]:
    """Names a case's counterparties go by: display names and the first label
    of their authenticated domains."""
    out: set[str] = set()
    for info in case.parties.values():
        if info["at"] >= as_of:
            continue
        if info.get("name"):
            out.add(str(info["name"]).casefold())
    for domain, seen in case.domains.items():
        if seen < as_of:
            label = domain.split(".")[-2] if domain.count(".") >= 1 else domain
            if len(label) >= 4:
                out.add(label.casefold())
    return out


def candidates(
    store: CaseStore, header: mailindex.Header, body: str
) -> tuple[Candidate, ...]:
    """A bounded shortlist of cases this mail may belong to, strongest first.

    Signals, all deterministic: a shared explicit key, a shared amount, a
    counterparty name appearing in this mail, activity inside the time window,
    and recent activity. Only facts strictly before this mail count.
    """
    as_of = header.position
    text = f"{header.subject}\n{body}"
    folded = text.casefold()
    keys = set(mail_keys(header.subject, text))
    amounts = set(extract_amounts(text))
    sender_name = mailindex.display_name(header.from_value).casefold()
    found: list[Candidate] = []
    for case in store.cases.values():
        if not case.created < as_of:
            continue
        last = case.last_before(as_of)
        if last is None:
            continue
        reasons = []
        if keys & case.keys_before(as_of):
            reasons.append("key")
        case_amounts = {(a["currency"], a["cents"]) for a in case.amounts if a["at"] < as_of}
        if amounts & case_amounts:
            reasons.append("amount")
        names = _name_tokens(case, as_of)
        if any(n and len(n) >= 4 and n in folded for n in names) or any(
            sender_name and difflib.SequenceMatcher(None, sender_name, n).ratio()
            >= NAME_SIMILARITY for n in names
        ):
            reasons.append("name")
        gap = header.at - last[0]
        if 0 <= gap <= TIME_WINDOW_DAYS * 86400:
            reasons.append("window")
        if 0 <= gap <= RECENT_DAYS * 86400:
            reasons.append("recent")
        if not reasons:
            continue
        found.append(Candidate(case.id, sum(_SCORES[r] for r in reasons), tuple(reasons)))
    found.sort(key=lambda c: (-c.score, c.case_id))
    return tuple(found[:MAX_CANDIDATES])


# ---------------------------------------------------------------- case flags


@dataclass(frozen=True)
class CaseFlags:
    """Deterministic comparisons of this mail against case history."""

    coordinates_changed: tuple[str, ...] = ()  # case ids whose history lacks a value here
    sender_not_admitted: tuple[str, ...] = ()  # case ids with admitted senders, not this one

    def as_json(self) -> dict[str, Any]:
        return {
            "coordinates_changed": list(self.coordinates_changed),
            "sender_not_admitted": list(self.sender_not_admitted),
        }


def record_withheld(case: Case, index: mailindex.MailIndex | None) -> bool:
    """Record every authenticated sender on the case whom he never addressed
    there, at a domain where he addressed ANOTHER address on the case — the
    sibling his exact law does not admit (`mailindex.AIM_SIBLING`), and the
    trust chip's trigger. Only adds; admits nothing. True when it recorded."""
    if index is None:
        return False
    admitted = case.admissions(index)
    outs = [h for p in sorted(case.mails)
            if (h := index.get(p[1])) is not None and h.direction == "out"]
    changed = False
    for sender, seen in authenticated_senders(case, index).items():
        if sender in admitted or sender in case.withheld:
            continue
        acts = [h for h in outs if mailindex.aim(h, sender) == mailindex.AIM_SIBLING]
        if acts and not any(mailindex.aimed_at(h, sender) for h in outs):
            act = acts[0]
            case.withheld[sender] = {"at": max(seen, act.position), "by": mailindex.AIM_SIBLING,
                                     "act": act.digest, "mail": seen[1]}
            changed = True
    return changed


def authenticated_senders(
    case: Case, index: mailindex.MailIndex, *, before: Position | None = None
) -> dict[str, Position]:
    """Every sender address seen on the case in AUTHENTICATED inbound mail, at
    its first such mail — strictly before `before` when given."""
    from . import mailgate

    out: dict[str, Position] = {}
    for position in sorted(case.mails):
        if before is not None and position >= before:
            break
        header = index.get(position[1])
        if (header is None or header.direction != "in" or not header.from_addr
                or header.from_addr in out):
            continue
        if mailgate.authenticate_header(header).authenticated:
            out[header.from_addr] = position
    return out


def case_flags(
    store: CaseStore,
    header: mailindex.Header,
    coordinates: tuple[str, ...] | set[str],
    case_ids: tuple[str, ...] | list[str],
    index: mailindex.MailIndex | None = None,
) -> CaseFlags:
    """For each case this mail is linked to: does it carry payment coordinates
    the case has never had, and is its sender outside the case's admitted
    senders? Both read history strictly before the mail."""
    as_of = header.position
    changed, outsider = [], []
    wanted = {normalize_coordinate(c) for c in coordinates if c}
    for case_id in dict.fromkeys(case_ids):
        case = store.cases.get(case_id)
        if case is None:
            continue
        history = case.coordinates_before(as_of, index)
        if wanted and history and not wanted <= history:
            changed.append(case_id)
        # Exact: an address at an admitted address's domain is somebody else
        # until he trusts it (round-8 B2).
        admitted = case.admitted_before(as_of, index)
        if header.direction == "in" and admitted and header.from_addr not in admitted:
            outsider.append(case_id)
    return CaseFlags(tuple(changed), tuple(outsider))


def case_file(store: CaseStore, case_id: str, index: mailindex.MailIndex,
              as_of: Position) -> dict[str, Any]:
    """What the Reader is shown when it asks for a case file: the case as it
    stood strictly before the mail, with its mails named by subject and date."""
    case = store.cases.get(case_id)
    if case is None:
        return {}
    mails = []
    for position in sorted(p for p in case.mails if p < as_of):
        header = index.get(position[1])
        if header is not None:
            mails.append({
                "direction": header.direction,
                "from": header.from_addr,
                "date": time.strftime("%Y-%m-%d", time.gmtime(header.at)),
                "subject": header.subject[:200],
            })
    return {
        "keys": sorted(case.keys_before(as_of)),
        "owner_transacted": case.transacted_before(as_of),
        "admitted_senders": sorted(case.admitted_domains_before(as_of, index)),
        "payment_coordinates_seen": sorted(case.coordinates_before(as_of, index)),
        "mails": mails[-12:],
    }


def path_of(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / CASES_DIRNAME
