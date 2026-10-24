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

import difflib
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atomic_write, mailindex, vocab

CASES_DIRNAME = "cases"
TOMBSTONES_DIRNAME = "tombstones"

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

_KEY_KINDS = {
    "order": ("order", "zamówienie", "zamowienie", "bestellung", "commande"),
    "tracking": ("tracking", "przesyłka", "przesylka", "paczka", "parcel", "shipment",
                 "sendung", "awb"),
    "booking": ("booking", "rezerwacja", "reservation", "confirmation", "pnr", "reference"),
    "invoice": ("invoice", "faktura", "rachunek"),
}

_KEY_PATTERN = re.compile(
    r"(?P<word>" + "|".join(re.escape(w) for w in sorted(_KEY_WORDS, key=len, reverse=True))
    + r")\w*\.?\s*(?:no\.?|nr\.?|number|numer|code|kod|id)?\s*[:#]?\s*"
    r"(?P<key>[A-Z0-9][A-Z0-9/-]{3,29})\b",
    re.I,
)
_UPS = re.compile(r"\b1Z[0-9A-Z]{16}\b")

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


def extract_keys(text: str) -> dict[str, str]:
    """{normalized key: kind}. A key must carry a digit — `order confirmed`
    has no reference in it."""
    out: dict[str, str] = {}
    for match in _KEY_PATTERN.finditer(text or ""):
        key = match.group("key").upper().strip("-/")
        if not any(ch.isdigit() for ch in key) or len(key) < 4:
            continue
        word = match.group("word").lower()
        kind = next((k for k, words in _KEY_KINDS.items() if word in words), "reference")
        out.setdefault(key, kind)
    for match in _UPS.finditer(text or ""):
        out.setdefault(match.group(0).upper(), "tracking")
    vocab.note("mailcases._KEY_WORDS", matched=bool(out))
    return out


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
    admitted: dict[str, dict[str, Any]] = field(default_factory=dict)  # domain -> {at, by}
    owner_acts: list[Position] = field(default_factory=list)
    amounts: list[dict[str, Any]] = field(default_factory=list)  # {currency, cents, at}
    coordinates: list[dict[str, Any]] = field(default_factory=list)  # {value, at}
    proposals: dict[str, int] = field(default_factory=dict)  # "domain" -> count

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created": list(self.created),
            "created_by": self.created_by,
            "keys": {k: {**v, "at": list(v["at"])} for k, v in self.keys.items()},
            "mails": [list(p) for p in self.mails],
            "parties": {a: {**v, "at": list(v["at"])} for a, v in self.parties.items()},
            "domains": {d: list(p) for d, p in self.domains.items()},
            "admitted": {d: {**v, "at": list(v["at"])} for d, v in self.admitted.items()},
            "owner_acts": [list(p) for p in self.owner_acts],
            "amounts": [{**a, "at": list(a["at"])} for a in self.amounts],
            "coordinates": [{**c, "at": list(c["at"])} for c in self.coordinates],
            "proposals": dict(self.proposals),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Case:
        def stamped(name: str) -> dict[str, dict[str, Any]]:
            return {k: {**v, "at": _pos(v["at"])} for k, v in (raw.get(name) or {}).items()}

        return cls(
            id=str(raw["id"]),
            created=_pos(raw["created"]),
            created_by=str(raw["created_by"]),
            keys=stamped("keys"),
            mails=[_pos(p) for p in raw.get("mails") or ()],
            parties=stamped("parties"),
            domains={d: _pos(p) for d, p in (raw.get("domains") or {}).items()},
            admitted=stamped("admitted"),
            owner_acts=[_pos(p) for p in raw.get("owner_acts") or ()],
            amounts=[{**a, "at": _pos(a["at"])} for a in raw.get("amounts") or ()],
            coordinates=[{**c, "at": _pos(c["at"])} for c in raw.get("coordinates") or ()],
            proposals={str(k): int(v) for k, v in (raw.get("proposals") or {}).items()},
        )

    # -- as-of views: strictly before the reviewed mail

    def keys_before(self, as_of: Position) -> set[str]:
        return {k for k, v in self.keys.items() if v["at"] < as_of}

    def transacted_before(self, as_of: Position) -> bool:
        if self.created_by == "owner" and self.created < as_of:
            return True
        return any(p < as_of for p in self.owner_acts)

    def admitted_before(self, as_of: Position) -> set[str]:
        return {d for d, v in self.admitted.items() if v["at"] < as_of}

    def coordinates_before(self, as_of: Position) -> set[str]:
        return {c["value"] for c in self.coordinates if c["at"] < as_of}

    def last_before(self, as_of: Position) -> Position | None:
        earlier = [p for p in self.mails if p < as_of]
        return max(earlier) if earlier else None


# ---------------------------------------------------------------- the store


@dataclass
class StoreHealth:
    loaded: int = 0
    unreadable: list[str] = field(default_factory=list)


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
        self.health = StoreHealth()
        if self.directory is not None and self.directory.is_dir():
            for path in sorted(self.directory.glob("*.json")):
                try:
                    case = Case.from_json(json.loads(path.read_text()))
                except (OSError, ValueError, TypeError, KeyError):
                    self.health.unreadable.append(path.name)
                    continue
                self.cases[case.id] = case
            self.health.loaded = len(self.cases)

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
    ) -> Observation:
        """Fold one mail into the case store. Deterministic; idempotent per
        mail (a mail already on a case changes nothing)."""
        position = header.position
        text = f"{header.subject}\n{body}"
        keys = extract_keys(text)
        touched: dict[str, Case] = {}
        for key in keys:
            for case in self.by_key(key):
                touched[case.id] = case
        if header.direction == "out" and header.in_reply_to and index is not None:
            for parent_id in header.in_reply_to:
                parent = index.get(index.by_mid.get(parent_id, ""))
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
            case.mails.append(position)
            for key, kind in keys.items():
                case.keys.setdefault(key, {"kind": kind, "at": position})
            if header.direction == "out":
                if case.created != position:
                    case.owner_acts.append(position)
                for addr in header.correspondents:
                    case.parties.setdefault(addr, {"at": position, "name": ""})
                admitted += self._admit_toward(case, header, index)
            else:
                case.parties.setdefault(
                    header.from_addr,
                    {"at": position, "name": mailindex.display_name(header.from_value)},
                )
                if authenticated:
                    domain = mailindex.domain_of(header.from_addr)
                    case.domains.setdefault(domain, position)
                    for currency, cents in extract_amounts(text):
                        case.amounts.append({"currency": currency, "cents": cents, "at": position})
                    for value in extract_coordinates(text):
                        case.coordinates.append({"value": value, "at": position})
            self._save(case)
        return Observation(keys, tuple(sorted(touched)), created, tuple(admitted))

    def _admit_toward(
        self, case: Case, header: mailindex.Header, index: mailindex.MailIndex | None
    ) -> list[str]:
        """An owner act on a case admits the authenticated senders it was AIMED
        at — the domains he wrote to, and the domain of the mail he replied to
        — when they were already seen on the case. As of the act, never
        earlier, and never a domain he did not address."""
        aimed = {mailindex.domain_of(a) for a in header.correspondents}
        if index is not None:
            for parent_id in header.in_reply_to:
                parent = index.get(index.by_mid.get(parent_id, ""))
                if parent is not None and parent.direction == "in":
                    aimed.add(mailindex.domain_of(parent.from_addr))
        out = []
        for domain, seen in case.domains.items():
            if domain in aimed and domain not in case.admitted and seen < header.position:
                case.admitted[domain] = {"at": header.position, "by": "owner_act_toward_sender"}
                out.append(f"{case.id}:{domain}")
        return out

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

    # -- as-of reads

    def admitted_domains(self, as_of: Position) -> frozenset[str]:
        return frozenset(d for c in self.cases.values() for d in c.admitted_before(as_of))

    def owner_case_parties(self, as_of: Position) -> dict[str, list[float]]:
        """address -> times at which a case the owner transacted on included it."""
        out: dict[str, list[float]] = {}
        for case in self.cases.values():
            if not case.transacted_before(as_of):
                continue
            since = case.created if case.created_by == "owner" else min(
                p for p in case.owner_acts if p < as_of
            )
            for addr, info in case.parties.items():
                if info["at"] < as_of:
                    out.setdefault(addr, []).append(max(since, info["at"])[0])
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
    keys = set(extract_keys(text))
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


def case_flags(
    store: CaseStore,
    header: mailindex.Header,
    coordinates: tuple[str, ...] | set[str],
    case_ids: tuple[str, ...] | list[str],
) -> CaseFlags:
    """For each case this mail is linked to: does it carry payment coordinates
    the case has never had, and is its sender outside the case's admitted
    senders? Both read history strictly before the mail."""
    as_of = header.position
    domain = mailindex.domain_of(header.from_addr)
    changed, outsider = [], []
    wanted = {normalize_coordinate(c) for c in coordinates if c}
    for case_id in dict.fromkeys(case_ids):
        case = store.cases.get(case_id)
        if case is None:
            continue
        history = case.coordinates_before(as_of)
        if wanted and history and not wanted <= history:
            changed.append(case_id)
        admitted = case.admitted_before(as_of)
        if header.direction == "in" and admitted and domain not in admitted:
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
        "admitted_senders": sorted(case.admitted_before(as_of)),
        "payment_coordinates_seen": sorted(case.coordinates_before(as_of)),
        "mails": mails[-12:],
    }


def path_of(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / CASES_DIRNAME
