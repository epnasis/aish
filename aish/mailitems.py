"""Open items and whether they are settled (#142 W5, the conversation-state and
settlement redesign of 2026-11-01 / 2026-11-02).

An **item** is one thing the daily report would put in front of the owner:

- `needs` — an inbound mail whose reading wants him to pay, reply or decide
  (anchor: that mail);
- `yours` — someone he wrote to answered with an ask (anchor: the FIRST asking
  mail after his last message to them; per counterparty, as before);
- `theirs` — he wrote last and they have not answered (anchor: his last
  message to them).

`derive` is the ONE derivation, called by the watcher's settlement pass and by
the 07:30 report alike, so the two can never disagree about what is open.

**Whether an item is still open** is `Context.state_of`, from four sources in a
fixed order — his word, then a verified judgement, then a deterministic rule:

1. **His acts** (`owner`): `resolve` / `restore` on the anchor (the
   `inbox_items` tool, this module's acts ledger), and a case he CLOSED that
   the anchor is on (`mailcases.Closure`'s law). His resolve covers only mail
   aish first read at or before it: any non-gated inbound mail in the item's
   conversation, from anyone, first read later REVIVES it (reviving only shows
   more). His restore pins the item open against every judgement and rule
   whose evidence aish read at or before the restore.
2. **A verified judgement** (`judge`): the `item-settler` role's answer on one
   (item, candidate) pair, re-verified here by code (`tier`) every time it is
   read — the candidate is still tied, still first read after the anchor,
   still passes the tier rule, carries no caution — and revived like his
   resolve.
3. **The rule** (`rule`): his own outbound mail, first read after the anchor,
   addressed to the item's counterparty or whose OWN In-Reply-To resolves
   (`MailIndex.reply_parent`) to one of the item's mails or to a mail from the
   counterparty in its conversation. Never conversation membership: a stranger
   can join a conversation by naming a Message-ID; only his own header names
   what he answered.

Settled by a judgement or the rule is **probably settled**: the report demotes
it into a named, restorable line; only his word removes an item (or the
window). Every clock is aish's FIRST-READ clock (`first_read`), never a
sender-written date.

**His own mail delivered to his other inbox** (`own_copies`) is recognised by
evidence and mints nothing: never an item, never observed into the case store,
never read by the Reader. The act is the Gmail original's.

Pure: no model, no network. `docs/mail-assistant.md` §Open items and settlement.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    atomic_write,
    mailcases,
    mailgate,
    mailindex,
    mailprefs,
    mailreader,
    mailreport,
    mailvouch,
)

NEEDS, YOURS, THEIRS = "needs", "yours", "theirs"
KINDS = (NEEDS, YOURS, THEIRS)

OPEN, SETTLED, CANNOT_TELL = "open", "settled", "cannot_tell"
OWNER, JUDGE, RULE = "owner", "judge", "rule"
RESOLVE, RESTORE = "resolve", "restore"
VERBS = (RESOLVE, RESTORE)

#: How far apart his Gmail original's receipt time and the copy's Date header
#: may be. Observed on the owner's own pair (2026-11-01): 3 s. A CHOICE.
OWN_COPY_SKEW_S = 300
#: Two mails from one sender with one subject that aish first read this close
#: together render once, "(×2)". A CHOICE.
NEAR_DUPLICATE_S = 600

#: At most this many later mails are weighed against one item, newest first.
CANDIDATES_PER_ITEM = 3
#: A domain names an entity (its first label, and "the same domain") only when
#: fewer than this many distinct addresses at it appear in any header of his
#: mail — a shared provider's ("gmail") names nobody. Structural, not a list of
#: providers; the number is a CHOICE.
ENTITY_DOMAIN_MAX_SENDERS = 5
ENTITY_TOKEN_MIN = 4
#: The tier rule's third-party history: the sender's domain seen on at least
#: this many distinct cases he transacted on, first seen this long before the
#: candidate. Both CHOICES, from the design (2026-11-02, change 1).
THIRD_PARTY_MIN_CASES = 2
THIRD_PARTY_MIN_AGE_DAYS = 30

#: The case-key kinds (`mailcases._KEY_KINDS`) that make an item a delivery.
DELIVERY_KEY_KINDS = ("order", "tracking")

ACTS_NAME = "item-acts.jsonl"
LOCK_NAME = "items.lock"
SETTLEMENTS_DIRNAME = "settlements"
ITEMS_LOCK_WAIT_S = 10.0


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- the item


@dataclass(frozen=True)
class OpenItem:
    """What an item IS, independent of how a report draws it."""

    kind: str
    anchor: str  # digest
    counterparty: str
    rests_on: tuple[str, ...]  # the mails it stands on (needs: the anchor; yours: every ask)
    conversation: frozenset[str]  # every digest in the anchor's conversation, now
    anchor_read: float  # aish's first-read clock of the anchor

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.anchor}|{self.counterparty}"


@dataclass
class Evidence:
    """What settled an item, in fields code can render — never model prose."""

    digest: str = ""  # the mail that is the evidence (his reply, the candidate)
    tier: str = ""  # "his act" | "same counterparty" | "third party"
    cases: tuple[str, ...] = ()  # a closed case's ids
    amounts: tuple[tuple[str, int], ...] = ()  # STORED amounts on the evidence mail
    sender: str = ""  # the evidence mail's From address (his own, for his mail)
    to: tuple[str, ...] = ()  # whom his evidence mail was addressed to
    when: float = 0.0  # the evidence mail's own time, for display (logic reads first_read)

    @classmethod
    def of(cls, ctx: Context, header: mailindex.Header, tier: str) -> Evidence:
        return cls(header.digest, tier,
                   amounts=tuple(sorted(ctx.stored_amounts(header.digest))),
                   sender=header.from_addr,
                   to=header.correspondents if header.direction == "out" else (),
                   when=header.at or ctx.first_read(header.digest))


@dataclass
class State:
    state: str = OPEN
    source: str = ""
    at: float = 0.0  # when the deciding act or judgement happened, aish's clock
    evidence: Evidence = field(default_factory=Evidence)
    revived: bool = False  # an earlier resolution no longer covers what aish read since
    restored_at: float = 0.0  # his restore: evidence must be read after it

    @property
    def by_owner(self) -> bool:
        return self.state == SETTLED and self.source == OWNER

    @property
    def probably(self) -> bool:
        """Demoted, not removed: a judgement or the rule, never his word."""
        return self.state == SETTLED and self.source in (JUDGE, RULE)


# ---------------------------------------------------------------- his own mail


def own_copies(index: mailindex.MailIndex) -> dict[str, str]:
    """{digest of a copy of his own sent mail delivered INBOUND to another of
    his inboxes: digest of the Gmail original}.

    Through the Tuta bridge a Message-ID is the bridge's own (every one ends
    `@tutabridge.local`) and no In-Reply-To or References survives, so the
    same-Message-ID law cannot recognise a copy there — observed on the
    owner's pair of 2026-11-01, where the body and the Date header differ too.
    What the evidence supports instead, every part checked:

    - the gateway AUTHENTICATED the copy (its sender's domain is proven), so a
      stranger cannot write it;
    - its From is an address his Gmail SENT mail is sent from (direction by the
      SENT label, never a From header);
    - a Gmail SENT original exists from that address with the same subject, the
      same To set and the same Cc set, received within `OWN_COPY_SKEW_S` of the
      copy's Date header.

    A copy whose original is not in the index is not recognised, and stays an
    ordinary mail — the visible direction."""
    mine = mailreport.owner_addresses(index)
    originals: dict[tuple[str, str], list[mailindex.Header]] = {}
    for h in index.by_digest.values():
        # Mail he sends only to HIMSELF (one inbox to another) is a mail to
        # him, not a copy of a mail to someone else: never matched.
        if (h.mailbox == "gmail" and h.direction == "out" and h.from_addr
                and not set(h.to) | set(h.cc) <= mine):
            originals.setdefault((h.from_addr, h.subject), []).append(h)
    out: dict[str, str] = {}
    for h in index.by_digest.values():
        if h.direction != "in" or h.mailbox == "gmail" or not h.from_addr:
            continue
        matches = originals.get((h.from_addr, h.subject))
        if not matches:
            continue
        sent_at = mailindex.date_header_time(h.date_header)
        if sent_at is None or not mailgate.authenticate_header(h).authenticated:
            continue
        for g in matches:
            if (set(g.to) == set(h.to) and set(g.cc) == set(h.cc)
                    and abs(g.at - sent_at) <= OWN_COPY_SKEW_S):
                out[h.digest] = g.digest
                break
    return out


# ---------------------------------------------------------------- his acts on items


def acts_path(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / ACTS_NAME


def read_acts(state_dir: os.PathLike | str) -> list[dict[str, Any]]:
    """His resolve / restore acts, oldest first; a line that does not parse
    costs that line."""
    try:
        text = acts_path(state_dir).read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if (isinstance(entry, dict) and entry.get("verb") in VERBS
                and re.fullmatch(r"[0-9a-f]{64}", str(entry.get("anchor") or ""))):
            out.append(entry)
    return out


@contextlib.contextmanager
def acts_lock(
    state_dir: os.PathLike | str, wait_s: float = ITEMS_LOCK_WAIT_S,
    *, sleep: Callable[[float], None] = time.sleep,
) -> Iterator[bool]:
    """The item acts' one writer at a time — their own lock, not the
    watcher's: no pass writes his acts, so a long backfill must not block one."""
    path = mailindex.mail_dir(state_dir) / LOCK_NAME
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


def append_act(state_dir: os.PathLike | str, act: dict[str, Any]) -> None:
    path = acts_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(act, sort_keys=True, ensure_ascii=False) + "\n")


def latest_acts(acts: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """anchor -> his newest act on it."""
    out: dict[str, dict[str, Any]] = {}
    for act in sorted(acts, key=lambda a: _f(a.get("at"))):
        out[str(act["anchor"])] = act
    return out


# ---------------------------------------------------------------- judgements


def settlements_dir(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / SETTLEMENTS_DIRNAME


def _settlement_path(state_dir: os.PathLike | str, anchor: str) -> Path:
    return settlements_dir(state_dir) / anchor[:2] / f"{anchor}.json"


def pair_key(item_key: str, candidate: str, charter_digest: str) -> str:
    return f"{item_key}|{candidate}|{charter_digest}"


def read_judgements(state_dir: os.PathLike | str) -> tuple[dict[str, dict[str, Any]], int]:
    """(pair key -> the judgement record, unreadable files). Per-file
    containment: one unreadable file costs that anchor's judgements."""
    out: dict[str, dict[str, Any]] = {}
    unreadable = 0
    for path in sorted(settlements_dir(state_dir).glob("*/*.json")):
        try:
            raw = json.loads(path.read_text())
            judged = raw.get("judged") if isinstance(raw, dict) else None
            if not isinstance(judged, dict):
                raise ValueError("no judged")
        except (OSError, ValueError, AttributeError):
            unreadable += 1
            continue
        for key, record in judged.items():
            if isinstance(record, dict):
                out[str(key)] = record
    return out, unreadable


def write_judgement(state_dir: os.PathLike | str, anchor: str, record: dict[str, Any]) -> None:
    """One judgement onto its anchor's file. The watcher is the one writer,
    under the pass lock."""
    path = _settlement_path(state_dir, anchor)
    try:
        held = json.loads(path.read_text())
        if not isinstance(held, dict) or not isinstance(held.get("judged"), dict):
            held = {}
    except (OSError, ValueError):
        held = {}
    judged = dict(held.get("judged") or {})
    judged[pair_key(record["item"], record["candidate"], record["charter_digest"])] = record
    atomic_write.publish(path, json.dumps({"anchor": anchor, "judged": judged},
                                          ensure_ascii=False, sort_keys=True, indent=1))


# ---------------------------------------------------------------- the context


@dataclass
class Candidate:
    """A later mail code tied to an item, and what the tier rule said."""

    header: mailindex.Header
    ties: tuple[str, ...]
    tier: str = ""  # "" when the tier rule refuses it
    refused: str = ""  # code's words for why it cannot settle the item
    body_missing: bool = False
    unread: bool = False  # inbound with no Reader reading: waits to be read, never judged
    meaning: str = ""  # the vouched meaning that made a third party eligible
    vouchable: tuple[str, str] | None = None  # (domain, meaning) his vouch would make eligible


class Context:
    """Everything the derivation and the state need, read once. Read-only."""

    def __init__(
        self,
        state_dir: os.PathLike | str | None,
        *,
        now: float,
        index: mailindex.MailIndex | None = None,
        readings: dict[str, mailreport.Reading] | None = None,
        store: mailcases.CaseStore | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.now = now
        self.index = index if index is not None else mailindex.MailIndex(state_dir)
        if readings is None:
            readings = mailreport.load_readings(state_dir)[0] if state_dir is not None else {}
        self.readings = readings
        self.store = store if store is not None else mailcases.CaseStore(state_dir)
        self.closure = self.store.closure()
        self.acts = latest_acts(read_acts(state_dir)) if state_dir is not None else {}
        judged, self.judgements_unreadable = (
            read_judgements(state_dir) if state_dir is not None else ({}, 0))
        self.judged = judged
        self.copies = own_copies(self.index)
        # His settler vouches: exact (domain, meaning) pairs (`mailvouch`).
        self.vouched = mailvouch.current(state_dir)
        # His `important` preferences: entity + meaning, placing mail under Needs.
        self.important = (mailprefs.Store.load(state_dir).of(mailprefs.IMPORTANT)
                          if state_dir is not None else [])
        self.mine = mailreport.owner_addresses(self.index)
        self._read: dict[str, float] = {}
        self._keys: dict[str, frozenset[str]] = {}
        self._text: dict[str, str | None] = {}
        self._entity: dict[str, frozenset[str]] = {}
        self._cases_of: dict[str, list[mailcases.Case]] = {}
        self._trusted_cases: dict[str, list[mailcases.Case]] = {}
        for case in self.store.cases.values():
            for _at, digest in case.mails:
                self._cases_of.setdefault(digest, []).append(case)
        # Every address seen at each domain, in any header of any mail: a
        # domain many people use (a mail provider, his family's) names nobody.
        seen: dict[str, set[str]] = {}
        for h in self.index.by_digest.values():
            for address in (h.from_addr, *h.to, *h.cc):
                if address:
                    seen.setdefault(mailindex.domain_of(address), set()).add(address)
        self._domain_senders = {d: len(a) for d, a in seen.items()}
        self._passed: dict[str, float] = {}
        for reading in self.readings.values():
            header = self.index.get(reading.digest)
            if header is None or not any(mailreport._authenticated(r) for r in reading.records):
                continue
            first = self._passed.get(header.from_addr)
            self._passed[header.from_addr] = (reading.first_seen if first is None
                                              else min(first, reading.first_seen))

    # -- clocks and facts

    def first_read(self, digest: str) -> float:
        """aish's first-read clock, never a sender-written date:

        - the earliest reading's `recorded_at`;
        - for his own sent mail, its time (the SENT label / Sent folder is his
          evidence, and only he puts mail there);
        - for any other mail with no reading (backfilled, a lapse), when aish
          WROTE its index entry — the file's modification time, aish's own
          clock (a later recheck rewrite only moves it later, the safe way);
        - and when none of those exists, never (`inf`): such a mail can never
          be covered by a resolution nor serve as anyone's evidence."""
        if digest in self._read:
            return self._read[digest]
        header = self.index.get(digest)
        reading = self.readings.get(digest)
        if reading is not None and reading.first_seen:
            read = reading.first_seen
        elif header is None:
            read = float("inf")
        elif header.direction == "out":
            read = header.at
        else:
            read = float("inf")
            if self.state_dir is not None:
                try:
                    read = os.path.getmtime(mailindex.header_path(self.state_dir, digest))
                except OSError:
                    pass
            elif header.at_source == "provider":
                read = header.at
        self._read[digest] = read
        return read

    def gated(self, header: mailindex.Header) -> bool:
        return mailreport.classify(self.readings.get(header.digest), header) == mailreport.GATED_OUT

    def text(self, digest: str) -> str | None:
        """Subject and body, or None when the body is not (or no longer) recorded."""
        if digest not in self._text:
            header = self.index.get(digest)
            body = self.index.body(digest)
            self._text[digest] = (None if header is None or body is None
                                  else f"{header.subject}\n{body}")
        return self._text[digest]

    def keys(self, digest: str) -> frozenset[str]:
        if digest not in self._keys:
            header = self.index.get(digest)
            text = self.text(digest)
            if text is None and header is not None:
                text = header.subject
            self._keys[digest] = frozenset(mailcases.extract_keys(text or ""))
        return self._keys[digest]

    def cases_of(self, digest: str) -> list[mailcases.Case]:
        """The cases this mail is a TRUSTED member of — the ONE reader of case
        membership for anything that grants or decides visibility (the case
        tie, stored amounts, an item's meaning, admitted domains, his closed
        case covering an item, the keeper release). Membership is trusted
        when the mail is his own, or authenticated AND joined through a key a
        trusted mail recorded, a key he typed himself, or a direct reply to
        his mail on that case (`_trusted_member`). Any other attachment is
        display-only: it characterises nothing (round-4 review)."""
        if digest not in self._trusted_cases:
            header = self.index.get(digest)
            self._trusted_cases[digest] = [
                case for case in self._cases_of.get(digest, [])
                if header is not None and self._trusted_member(case, header)]
        return self._trusted_cases[digest]

    def attached_cases_of(self, digest: str) -> list[mailcases.Case]:
        """Every case the mail is attached to, trusted or not — for DISPLAY only."""
        return self._cases_of.get(digest, [])

    def _trusted_member(self, case: mailcases.Case, header: mailindex.Header) -> bool:
        if header.direction == "out":
            return True
        if not mailgate.authenticate_header(header).authenticated:
            return False
        keys = self.keys(header.digest)
        if any(k in case.keys and self._trusted_fact(case.keys[k]) for k in keys):
            return True
        text = (self.text(header.digest) or header.subject or "").upper()
        if any(info.get("kind") == mailcases.OWNER_KEY and mailcases._says_key(text, key)
               for key, info in case.keys.items()):
            return True
        for mid in header.in_reply_to:
            parent = self.index.reply_parent(mid, header.position)
            if (parent is not None and parent.direction == "out"
                    and parent.position in case.mails):
                return True
        return False

    def trusted_closure(self) -> mailcases.Closure:
        """His closures, with each mail's case membership as `cases_of`
        trusts it: a close covers only mail trusted on the case."""
        members = {d: frozenset(c.id for c in self.cases_of(d)) for d in self.closure.members}
        return mailcases.Closure(dict(self.closure.closed_at),
                                 {d: ids for d, ids in members.items() if ids})

    def stored_amounts(self, digest: str) -> frozenset[tuple[str, int]]:
        """Amounts the case store RECORDED for this mail at ingest — never
        re-read from a body, which is purgeable and is what a judge read."""
        header = self.index.get(digest)
        if header is None:
            return frozenset()
        return frozenset((str(a["currency"]), int(a["cents"]))
                         for case in self.cases_of(digest) for a in case.amounts
                         if tuple(a["at"]) == header.position)

    def entity_tokens(self, party: str) -> frozenset[str]:
        """Names the counterparty goes by: the display names it mailed him
        under, and its domain's first label when that domain is not a shared
        provider's (fewer than `ENTITY_DOMAIN_MAX_SENDERS` addresses at it)."""
        if party not in self._entity:
            names: set[str] = set()
            for h in self.index.by_digest.values():
                if h.direction == "in" and h.from_addr == party:
                    name = mailindex.display_name(h.from_value).casefold()
                    if len(name) >= ENTITY_TOKEN_MIN:
                        names.add(name)
            domain = mailindex.domain_of(party)
            if domain and self._domain_senders.get(domain, 0) < ENTITY_DOMAIN_MAX_SENDERS:
                label = domain.split(".")[-2] if domain.count(".") >= 1 else domain
                if len(label) >= ENTITY_TOKEN_MIN:
                    names.add(label.casefold())
            self._entity[party] = frozenset(names)
        return self._entity[party]

    def meanings(self, digest: str) -> frozenset[str]:
        """What a mail MEANS for an `important` preference, from recorded facts
        only (`mailprefs.MEANINGS`): never a word list, never a purgeable body."""
        header = self.index.get(digest)
        reading = self.readings.get(digest)
        out = {"anything"}
        records = reading.records if reading is not None else ()
        wanted = str((reading.verdict if reading else {}).get("wanted") or "")
        accounts = any((r.get("code_coordinates") or (r.get("verdict") or {}).get(
            "payment_coordinates")) for r in records if isinstance(r, dict))
        if wanted == "pay" or self.stored_amounts(digest) or accounts:
            out.add("money")
        if header is not None and mailreader.has_document(header.attachments):
            out.add("documents")
        return frozenset(out)

    def settle_meanings(self, item: OpenItem) -> list[str]:
        """Which `mailvouch.MEANINGS` this ITEM is — a predicate of the item
        over RECORDED facts on its own mails (`rests_on`), never of the later
        mail that might settle it (round-2 review, blocking: "the later mail
        names the counterparty" is the entity tie, a reason to ASK, never part
        of a grant):

        - `payment` — one of its mails carries a recorded money fact
          (`meanings`: a Reader pay, a stored amount or account);
        - `delivery` — one of its mails is on a case whose RECORDED keys
          (`mailcases`, extracted at ingest) include an order or tracking key."""
        out = []
        if any("money" in self.meanings(d) for d in item.rests_on):
            out.append("payment")
        if any(info.get("kind") in DELIVERY_KEY_KINDS and self._trusted_fact(info)
               for d in item.rests_on for case in self.cases_of(d)
               for info in case.keys.values()):
            out.append("delivery")
        return out

    def _trusted_fact(self, info: dict[str, Any]) -> bool:
        """Was this recorded case fact written by his own or AUTHENTICATED
        mail? Read-side twin of `CaseStore.observe`'s write law, for facts
        recorded before that law (a key whose mail is gone or unauthenticated
        characterises nothing). A key he typed himself (`create`) is his."""
        at = info.get("at")
        if info.get("kind") == mailcases.OWNER_KEY:
            return True
        header = self.index.get(str(at[1])) if isinstance(at, (list, tuple)) and len(at) > 1 \
            else None
        if header is None:
            return False
        return header.direction == "out" or mailgate.authenticate_header(header).authenticated

    def important_pref(self, reading: mailreport.Reading,
                       header: mailindex.Header | None) -> str:
        """The `important` preference that places this mail under Needs, or ""."""
        if header is None or not self.important:
            return ""
        meanings = self.meanings(header.digest)
        for pref in self.important:
            if mailprefs.matches_important(pref, header, meanings):
                return pref.id
        return ""

    def caution(self, digest: str) -> list[str]:
        """Why this mail may never settle anything: a caution on any model's
        record of it, or failed authentication from a sender that passed before
        (`mailprefs.floor`, the suppression attack's floor)."""
        reading = self.readings.get(digest)
        header = self.index.get(digest)
        if reading is None or header is None:
            return []
        before = self._passed.get(header.from_addr)
        passed_before = before is not None and before < reading.first_seen
        reasons: list[str] = []
        for record in reading.records:
            for why in mailprefs.floor(record, authenticated_before=passed_before):
                if why not in reasons:
                    reasons.append(why)
        return reasons

    # -- the derivation

    def derive(self, *, needs_since: float | None = None) -> Derived:
        """`derive` over this context."""
        return derive(self, needs_since=needs_since)

    # -- the state

    def state_of(self, item: OpenItem) -> State:
        """Is this item open? His word, then a verified judgement, then the
        rule — see the module docstring for each source's law."""
        state = State()
        act = self.acts.get(item.anchor)
        if act is not None and act.get("verb") == RESTORE:
            state.restored_at = _f(act.get("at"))
        if act is not None and act.get("verb") == RESOLVE:
            at = _f(act.get("at"))
            if self.covers(item, at):
                return State(SETTLED, OWNER, at)
            state.revived = True
        closed = self._closed(item, state.restored_at)
        if closed is not None:
            if closed.state == SETTLED:
                return closed
            state.revived = True
        judged = self._judged(item, state)
        if judged is not None:
            return judged
        ruled = self._ruled(item, state.restored_at)
        if ruled is not None:
            ruled.revived = state.revived
            return ruled
        if any(self._judgements(item, CANNOT_TELL, state.restored_at)):
            state.state = CANNOT_TELL
        return state

    def covers(self, item: OpenItem, at: float) -> bool:
        """Does a resolution made at `at` cover everything the item now rests
        on? Only mail aish read at or before it: a later ask, or any later
        non-gated inbound mail in its conversation from anyone, revives it."""
        if any(self.first_read(d) > at for d in item.rests_on):
            return False
        for digest in item.conversation:
            header = self.index.get(digest)
            if (header is None or header.direction != "in" or digest in self.copies
                    or self.gated(header)):
                continue
            if self.first_read(digest) > at:
                return False
        return True

    def _closed(self, item: OpenItem, restored_at: float) -> State | None:
        """His closed case, under `Closure.releases`' read-clock law, revived
        by later mail like his resolve. None when no case closes it."""
        closure = self.trusted_closure()
        cases = set(closure.members.get(item.anchor, ()))
        if not cases or not all(c in closure.closed_at for c in cases):
            return None
        closed_at = min(closure.closed_at[c] for c in cases)
        if closed_at <= restored_at:
            return None  # his later restore outranks his earlier close
        read_at = max(self.first_read(d) for d in item.rests_on)
        if not closure.releases(item.anchor, read_at, ()) or not self.covers(item, closed_at):
            return State(OPEN, revived=True)
        return State(SETTLED, OWNER, closed_at, Evidence(cases=tuple(sorted(cases))))

    def _judgements(self, item: OpenItem, answer: str, restored_at: float) -> list[dict]:
        out = []
        for record in self.judged.values():
            if (record.get("item") == item.key and record.get("answer") == answer
                    and self.first_read(str(record.get("candidate"))) > restored_at):
                out.append(record)
        # Oldest first by when it was judged, then by the evidence mail's own
        # time, so the newest evidence is the one met first from the end.
        return sorted(out, key=lambda r: (_f(r.get("at")), self._own_time(str(r.get("candidate")))))

    def _own_time(self, digest: str) -> float:
        header = self.index.get(digest)
        return header.at if header is not None else 0.0

    def _judged(self, item: OpenItem, state: State) -> State | None:
        """A `settled_by` judgement that code still verifies: the candidate is
        in the index, first read after the anchor (and after his restore),
        still passes the tier rule with no caution, and nothing aish read
        after the judgement revives the item."""
        for record in reversed(self._judgements(item, "settled_by", state.restored_at)):
            header = self.index.get(str(record.get("candidate")))
            if header is None or not self.later(header, item, state.restored_at):
                continue
            checked = self.check(item, header)
            if (checked is None or not checked.tier or checked.body_missing
                    or checked.unread):
                continue
            at = _f(record.get("at"))
            if not self.covers(item, at):
                state.revived = True
                continue
            return State(SETTLED, JUDGE, at, Evidence.of(self, header, checked.tier),
                         revived=state.revived, restored_at=state.restored_at)
        return None

    def _ruled(self, item: OpenItem, restored_at: float) -> State | None:
        """His own mail after the anchor, aimed at this matter by its
        addressing or its OWN In-Reply-To — the deterministic settlement."""
        after = max(item.anchor_read, restored_at)
        own = set(item.rests_on)
        for header in sorted(self.index.by_digest.values(), key=lambda h: h.position):
            if header.direction != "out" or not self.later(header, item, after):
                continue
            if self._aimed(item, header, own):
                return State(SETTLED, RULE, header.at, Evidence.of(self, header, "his act"),
                             restored_at=restored_at)
        return None

    def _aimed(self, item: OpenItem, header: mailindex.Header, own: set[str]) -> bool:
        if item.counterparty in header.correspondents:
            return True
        for mid in header.in_reply_to:
            parent = self.index.reply_parent(mid, header.position)
            if parent is None:
                continue
            if parent.digest in own or (parent.from_addr == item.counterparty
                                        and parent.digest in item.conversation):
                return True
        return False

    # -- candidates and the tier rule

    def candidates(self, item: OpenItem, *, restored_at: float = 0.0,
                   limit: int = CANDIDATES_PER_ITEM) -> list[Candidate]:
        """Later mail code ties to the item, newest first, at most `limit` —
        each with the tier rule's answer. His mail aimed at the item is the
        rule's, not a candidate."""
        after = max(item.anchor_read, restored_at)
        found: list[Candidate] = []
        own = set(item.rests_on)
        for header in sorted(self.index.by_digest.values(),
                             key=lambda h: self.first_read(h.digest), reverse=True):
            digest = header.digest
            if digest in own or digest == item.anchor or digest in self.copies:
                continue
            if not self.later(header, item, after):
                continue
            if header.direction == "out":
                if self._aimed(item, header, own):
                    continue
            elif self.gated(header) or mailreport.classify(
                    self.readings.get(digest), header) == mailreport.OTP_OUT:
                continue
            ties = self.ties(item, header)
            if not ties:
                continue
            checked = self.check(item, header, ties)
            if checked is not None:
                found.append(checked)
            if len(found) >= limit:
                break
        return found

    def later(self, header: mailindex.Header, item: OpenItem, after: float) -> bool:
        """Is this mail LATER than the item, on both clocks? First read by aish
        after the anchor (and after his restore), and its own time after the
        anchor's own time. Either alone is not enough: a mail backfilled today
        is first read today however old it is, and a sender writes its own
        Date. Requiring both only ever excludes — the safe direction for
        anything that may settle."""
        anchor = self.index.get(item.anchor)
        read = self.first_read(header.digest)
        if read <= after or read > self.now:
            return False
        return bool(anchor is not None and header.at and header.at > anchor.at)

    def ties(self, item: OpenItem, header: mailindex.Header) -> tuple[str, ...]:
        """What code sees linking a later mail to the item. Each is a reason
        to ASK, never to settle."""
        ties = []
        if header.direction == "in" and header.from_addr == item.counterparty:
            ties.append("counterparty")
        if header.digest in item.conversation:
            ties.append("conversation")
        anchor_keys = self.keys(item.anchor)
        if anchor_keys and anchor_keys & self.keys(header.digest):
            ties.append("key")
        anchor_cases = {c.id for c in self.cases_of(item.anchor)}
        if anchor_cases & {c.id for c in self.cases_of(header.digest)}:
            ties.append("case")
        anchor_amounts = self.stored_amounts(item.anchor)
        if anchor_amounts and anchor_amounts & self.stored_amounts(header.digest):
            ties.append("amount")
        if "counterparty" not in ties:
            domain = mailindex.domain_of(item.counterparty)
            same_domain = (header.direction == "in" and domain
                           and mailindex.domain_of(header.from_addr) == domain
                           and self._domain_senders.get(domain, 0) < ENTITY_DOMAIN_MAX_SENDERS)
            text = (self.text(header.digest) or header.subject).casefold()
            if same_domain or any(t in text for t in self.entity_tokens(item.counterparty)):
                ties.append("entity")
        return tuple(ties)

    def check(self, item: OpenItem, header: mailindex.Header,
              ties: tuple[str, ...] | None = None) -> Candidate | None:
        """The tier rule, in code, for one tied later mail (None when it is no
        longer tied at all):

        - (a) his act — his own outbound mail;
        - (b) the same counterparty — the item's counterparty, an address HE
          wrote to in the item's conversation, or a domain admitted on a case
          the anchor is on. Never "a sender in the conversation": a stranger
          joins a conversation by naming a Message-ID, and its own membership
          must not be its credential (review 2026-11-02, the per-counterparty
          law of the follow-ups);
        - (c) a third party — only authenticated, tied by a case it joined with
          the anchor or by an equal STORED amount, and with transactional
          history: its domain on `THIRD_PARTY_MIN_CASES` distinct cases he
          transacted on, first seen `THIRD_PARTY_MIN_AGE_DAYS` before.

        Never `mailgate.grade`. A caution on any record, or failed
        authentication from a sender that passed before, refuses any tier.

        **Every INBOUND candidate, whatever its tier, must be authenticated and
        read** (delivery review 2026-11-02, blocking): an unauthenticated From
        is a claim anyone writes, and the caution floor reads the Reader's
        record — a mail with no reading has no floor at all. An unread one is
        not refused: it WAITS (`unread`), and the settlement step reads it."""
        ties = self.ties(item, header) if ties is None else ties
        if not ties:
            return None
        found = Candidate(header, ties)
        found.body_missing = (self.text(header.digest) is None
                              or self.text(item.anchor) is None)
        caution = self.caution(header.digest)
        if caution:
            found.refused = "; ".join(caution)
            return found
        if header.direction == "out":
            found.tier = "his act"
            return found
        if not mailgate.authenticate_header(header).authenticated:
            found.refused = "its sender is not authenticated"
            return found
        reading = self.readings.get(header.digest)
        found.unread = reading is None or reading.record is None
        sender = header.from_addr
        domain = mailindex.domain_of(sender)
        he_wrote_to = {
            c for d in item.conversation
            if (h := self.index.get(d)) is not None and h.direction == "out"
            for c in h.correspondents}
        admitted = {d for c in self.cases_of(item.anchor) for d in c.admitted}
        if sender == item.counterparty or sender in he_wrote_to or domain in admitted:
            found.tier = "same counterparty"
            return found
        # His vouch: this exact domain may settle this kind of item. An
        # alternative grant by HIS act — the history rule below is unchanged.
        meanings = self.settle_meanings(item)
        allowed = [m for m in meanings if (domain, m) in self.vouched]
        if allowed:
            found.tier, found.meaning = "vouched sender", allowed[0]
            return found
        if meanings:
            found.vouchable = (domain, meanings[0])
        if not {"case", "amount"} & set(ties):
            found.refused = ("a third party tied to it only by "
                             + ", ".join(t for t in ties) + " — not by a case or a stored amount")
            return found
        history = self.history(domain, header)
        if history < THIRD_PARTY_MIN_CASES:
            found.refused = (f"a third party you dealt with (addressed, or admitted) on "
                             f"{history} of your cases {THIRD_PARTY_MIN_AGE_DAYS}+ days before — "
                             f"{THIRD_PARTY_MIN_CASES} needed")
            return found
        found.tier = "third party"
        return found

    def history(self, domain: str, header: mailindex.Header) -> int:
        """Distinct cases he transacted on (as of the mail) where he DEALT with
        this domain at least `THIRD_PARTY_MIN_AGE_DAYS` before it: he addressed
        a party at it, or it is admitted by his act toward it
        (`Case.dealt_with_at`). Never a sighting alone — a sender that merely
        joined a case by quoting its key is no history (round-3 review)."""
        limit = header.at - THIRD_PARTY_MIN_AGE_DAYS * 86400
        count = 0
        for case in self.store.cases.values():
            if not case.transacted_before(header.position):
                continue
            admitted = case.admitted_before(header.position)
            seen = [when[0] for a in case.parties if mailindex.domain_of(a) == domain
                    if (when := case.dealt_with_at(a, admitted, self.index)) is not None]
            if domain in admitted:
                seen.append(case.admitted[domain]["at"][0])
            if seen and min(seen) <= limit:
                count += 1
        return count


# ---------------------------------------------------------------- the derivation


@dataclass
class Derived:
    """Every open-item candidate, before any state is applied."""

    needs: list[mailreport.Item] = field(default_factory=list)
    yours: list[mailreport.FollowUp] = field(default_factory=list)
    theirs: list[mailreport.FollowUp] = field(default_factory=list)
    # Counterparties whose answers ask nothing ONLY because aish read them late
    # (`LATE_READ`): a model's reading took a row out of Your turn, so the
    # report names them rather than letting the row vanish unsaid.
    closed_by_late_reading: list[str] = field(default_factory=list)

    def items(self) -> list[OpenItem]:
        out = [i.open_item for i in self.needs if i.open_item is not None]
        out += [f.open_item for f in (*self.yours, *self.theirs) if f.open_item is not None]
        return out


def needs_item(ctx: Context, reading: mailreport.Reading,
               header: mailindex.Header | None) -> str | None:
    """Is this mail a needs-action item? None when not; "" when its reading
    wants pay / reply / decide; the preference id when his `important`
    preference places it there (mail the Reader read, or that is
    newsletter-gated or unread — showing is free; never a one-time code).
    Inbound only, never his own copy."""
    if header is None or header.direction != "in" or header.digest in ctx.copies:
        return None
    kind = mailreport.classify(reading, header)
    if kind == mailreport.OK and str(reading.verdict.get("wanted") or "") in (
            mailreport.WANTS_ACTION):
        return ""
    if kind in (mailreport.OK, mailreport.GATED_OUT, mailreport.NO_READING):
        return ctx.important_pref(reading, header) or None
    return None


def derive(ctx: Context, *, needs_since: float | None = None) -> Derived:
    """THE derivation of open items, for the watcher and the report alike.
    Needs: every needs-action mail aish first read since `needs_since`
    (default: the follow-up window). Follow-ups: `follow_ups`."""
    window = ctx.now - mailreport.FOLLOWUP_WINDOW_DAYS * 86400
    since = window if needs_since is None else needs_since
    out = Derived()
    for reading in ctx.readings.values():
        header = ctx.index.get(reading.digest)
        placed = needs_item(ctx, reading, header)
        if placed is None or reading.when < since:
            continue
        assert header is not None
        members = frozenset(h.digest for h in ctx.index.conversation(header.digest, (
            float("inf"), "")))
        item = mailreport.Item(reading, header, important=placed)
        item.open_item = OpenItem(NEEDS, header.digest, header.from_addr, (header.digest,),
                             members, ctx.first_read(header.digest))
        out.needs.append(item)
    out.yours, out.theirs, out.closed_by_late_reading = follow_ups(ctx)
    return out


def follow_ups(ctx: Context) -> tuple[list[mailreport.FollowUp], list[mailreport.FollowUp],
                                      list[str]]:
    """(your turn, their turn, closed by a late reading), per counterparty —
    `docs/mail-assistant.md` §Follow-ups. Unchanged in what it lists; each row
    now carries its item: yours anchored on the FIRST asking mail after his
    last message to them, theirs on that last message. A your-turn row he
    answered in its own conversation is not derived at all — his answer moved
    his last message past it, exactly as before."""
    index, readings, now = ctx.index, ctx.readings, ctx.now
    mine = ctx.mine
    listy = {h.from_addr for h in index.by_digest.values()
             if h.direction == "in" and (h.list_id or h.list_unsubscribe)}
    window = now - mailreport.FOLLOWUP_WINDOW_DAYS * 86400
    last_from: dict[str, float] = {}
    for h in index.by_digest.values():
        if h.direction == "in" and h.from_addr and h.digest not in ctx.copies:
            when = mailreport._effective_at(h, readings)
            last_from[h.from_addr] = max(last_from.get(h.from_addr, 0.0), when)
    yours: list[mailreport.FollowUp] = []
    theirs: list[mailreport.FollowUp] = []
    closed: list[str] = []
    for conversation in index.conversations():
        if conversation[-1].at < window:
            continue
        outs = [h for h in conversation if h.direction == "out"]
        members = frozenset(h.digest for h in conversation)
        parties = sorted({c for h in outs for c in h.correspondents} - mine - listy - {""})
        for party in parties:
            his = [h for h in outs if party in h.correspondents][-1]
            answers = [h for h in conversation
                       if h.direction == "in" and h.from_addr == party
                       and h.digest not in ctx.copies
                       and mailreport._effective_at(h, readings) > his.at]
            if not answers:
                if (now - his.at >= mailreport.THEIR_TURN_DAYS * 86400 and his.at >= window
                        and last_from.get(party, 0.0) <= his.at):
                    row = mailreport.FollowUp("theirs", party, his.at, his,
                                              conversation=members, newest=his.at)
                    row.open_item = OpenItem(THEIRS, his.digest, party, (his.digest,), members,
                                        ctx.first_read(his.digest))
                    theirs.append(row)
                continue
            asking: list[tuple[mailindex.Header, mailreport.Reading | None]] = []
            for header in answers:
                reading = readings.get(header.digest)
                kind = mailreport.classify(reading, header)
                if kind in (mailreport.GATED_OUT, mailreport.OTP_OUT):
                    continue
                if kind == mailreport.NO_READING or (
                    reading is not None
                    and str(reading.verdict.get("wanted")) in mailreport.YOUR_TURN_WANTED
                ):
                    asking.append((header, reading))
            if not asking:
                # Named only when a LATE reading is the reason: had those
                # answers stayed unread, they would have counted as asks.
                if any(late_read(readings.get(h.digest))
                       and mailreport.classify(None, h) not in (mailreport.GATED_OUT,
                                                                mailreport.OTP_OUT)
                       for h in answers):
                    closed.append(party)
                continue
            latest, reading = asking[-1]
            row = mailreport.FollowUp(
                "yours", party, mailreport._effective_at(asking[0][0], readings), his,
                reading, latest, len(asking), conversation=members,
                newest=mailreport._effective_at(latest, readings),
                asked=tuple(readings.get(h.digest) for h in answers))
            row.open_item = OpenItem(YOURS, asking[0][0].digest, party,
                                tuple(h.digest for h, _ in asking), members,
                                ctx.first_read(asking[0][0].digest))
            yours.append(row)
    yours.sort(key=lambda f: f.since)
    theirs.sort(key=lambda f: f.since)
    return yours, theirs, sorted(set(closed))


#: The verdict-record field the watcher writes on a reading it made LATE,
#: because an open follow-up rested on an answer nobody had read (`gaps`).
LATE_READ = "late_read"


def late_read(reading: mailreport.Reading | None) -> bool:
    return reading is not None and any(bool(r.get(LATE_READ)) for r in reading.records)


def gaps(ctx: Context, limit: int) -> list[str]:
    """The answers open your-turn rows rest on that the Reader never read —
    the rows that are only "your turn" because an unread answer counts as an
    ask (the fail-visible direction). Oldest row first, at most `limit`. The
    watcher reads them; a reading that asks nothing closes the row, and the
    report names it (`closed_by_late_reading`)."""
    out: list[str] = []
    for row in derive(ctx).yours:
        item = row.open_item
        if item is None or ctx.state_of(item).by_owner:
            continue
        for digest in item.rests_on:
            reading = ctx.readings.get(digest)
            if (reading is None or reading.record is None) and digest not in out:
                out.append(digest)
        if len(out) >= limit:
            break
    return out[:limit]


def unread_candidates(ctx: Context, items: Iterable[OpenItem], limit: int) -> list[str]:
    """Tied later mail that waits for a Reader reading before it can be
    weighed (`Candidate.unread`), newest first, at most `limit` — read by the
    settlement step so the caution floor has a record to read."""
    out: list[str] = []
    for item in items:
        state = ctx.state_of(item)
        if state.by_owner or state.probably:
            continue
        for candidate in ctx.candidates(item, restored_at=state.restored_at):
            if candidate.unread and candidate.tier and candidate.header.digest not in out:
                out.append(candidate.header.digest)
    out.sort(key=lambda d: ctx.first_read(d), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- near-duplicates


def near_duplicates(items: list[mailreport.Item], first_read: Callable[[str], float]
                    ) -> tuple[list[mailreport.Item], dict[str, list[str]]]:
    """Mail one sender sent twice with one subject, that aish first read within
    `NEAR_DUPLICATE_S` of each other, renders once — the Gmail copy preferred,
    as `_one_copy` prefers it. Returns (items, kept digest -> its siblings), so
    every copy is recorded as shown with the one that was."""
    kept: list[mailreport.Item] = []
    siblings: dict[str, list[str]] = {}
    for item in items:
        header = item.header
        twin = None
        if header is not None:
            for other in kept:
                o = other.header
                if (o is not None and o.from_addr == header.from_addr
                        and o.subject == header.subject
                        and abs(first_read(o.digest) - first_read(header.digest))
                        <= NEAR_DUPLICATE_S):
                    twin = other
                    break
        if twin is None:
            kept.append(item)
            continue
        keep, drop = twin, item
        if (twin.header is not None and twin.header.mailbox != "gmail"
                and header is not None and header.mailbox == "gmail"):
            keep, drop = item, twin
            kept[kept.index(twin)] = item
            siblings[keep.reading.digest] = siblings.pop(twin.reading.digest, [])
        siblings.setdefault(keep.reading.digest, []).append(drop.reading.digest)
    return kept, siblings
