"""The bank closes paid bills (#142 W5, slice 1c — the owner's decision of
2026-11-12: "Paid bills should match in bank history").

Four things, each code, none a model:

- **A fresh ledger** (`plugin_sync`): before the morning brief, at most once a
  day, the owner's own `bank_sync` plugin tool is run through the runner a
  model's call uses (`tool_plugins.execute`: its secrets, lock and cooldown
  apply), under a hard time limit. The bank tools are HIS plugins, not
  repository code, so everything here is soft: no tool, no ledger, a failed
  sync, a lapsed consent or secrets this process cannot read each end the same
  way — *could not check the bank* — with its own reason recorded. Never
  "unpaid".
- **Reading the ledger** (`read_ledger`): his plugin's SQLite cache in the
  state tree, opened READ-ONLY (`mode=ro`); an absent file, an unexpected
  schema and a locked file are each "could not check", with the reason.
  Checked means the OLDEST last sync among the linked accounts is at most
  `FRESH_DAYS` old — the newest would hide one stale account.
- **Amounts recorded at ingest** (`record_amounts`): the amounts
  `mailcases.extract_amounts` finds in an authenticated mail whose reading
  wants `pay`, keyed to the mail, written once and never re-read from a
  purgeable body; `backfill_amounts` does the same once for older mail whose
  body is still held.
- **The match and the pairing** (`match`, `assign`): an outgoing booked
  transaction settles an open Pay item when its amount is one of the item's
  recorded amounts, it was booked in the window, and something other than the
  amount ties it to the item. A payment is spent once: the first pairing is
  recorded (`record_pairs`) and a recorded transaction is never handed to
  another item.

The evidence a reader renders is the transaction's own date and amount only
(`evidence_words`): its counterparty and description are text another party
chose and are never rendered. No account number is rendered anywhere.
`docs/mail-assistant.md` §The bank as a settlement source.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import mailcases, mailgate, mailindex, mailreport

#: A ledger whose oldest linked account was last synced longer ago than this
#: is "could not check". The spec's choice (2026-11-12).
FRESH_DAYS = 2
#: A payment booked this many days BEFORE the mail arrived still settles it
#: (a standing order may leave before the reminder arrives); one booked
#: earlier is last month's. The spec's choice.
BOOKED_BEFORE_MAIL_DAYS = 7
#: The sync's hard time limit inside `aish-mailreport`. A CHOICE: the brief
#: is built on what is there when it passes, and says so.
SYNC_LIMIT_S = 120
SYNC_TOOL = "bank_sync"

AMOUNTS_NAME = "pay-amounts.jsonl"
PAIRS_NAME = "bank-pairs.jsonl"

# How the sync step ended — closed words, recorded in the report's ledger.
SYNC_OK = "ok"
SYNC_NO_TOOL = "no_tool"
SYNC_FAILED = "failed"
SYNC_TIME_LIMIT = "time_limit"
SYNC_SECRETS = "secrets_unreadable"
SYNC_NOT_RUN = "not_run"
SYNC_DONE_TODAY = "already_today"

TIE_STRONG = "strong"
TIE_NAMED = "named"

BOOKED = "BOOK"
REASON_CHARS = 200
#: How long a read waits on a ledger the sync is writing before it says the
#: ledger was locked.
LEDGER_BUSY_WAIT_S = 2.0


def ledger_path(state_dir: os.PathLike | str) -> Path:
    """Where the bank plugin keeps its cache: `<state>/bank/bank.db`, the path
    its own `banklib.db_path` resolves from the same state dir."""
    return Path(state_dir) / "bank" / "bank.db"


def _day(ts: float) -> datetime.date:
    return datetime.date.fromtimestamp(ts)


# ---------------------------------------------------------------- the sync


@dataclass(frozen=True)
class SyncOutcome:
    status: str
    reason: str = ""


Sync = Callable[[float], SyncOutcome]


def plugin_sync(limit_s: float) -> SyncOutcome:
    """Run his `bank_sync` tool once, no model involved, through
    `tool_plugins.execute` — the runner a model's call uses — with its
    timeout lowered to `limit_s`. Each ending is named, never interpreted:
    the tool's own first output line is the reason of a failed run."""
    from . import tool_plugins, tools

    found = [t for t in tool_plugins.discover(os.getcwd())[0] if t.name == SYNC_TOOL]
    if not found:
        return SyncOutcome(SYNC_NO_TOOL, f"no {SYNC_TOOL} plugin tool is installed")
    tool = dataclasses.replace(found[0], timeout=int(max(1, min(limit_s, found[0].timeout))))
    outcome = tool_plugins.execute(tool, {}, os.getcwd())
    meta = getattr(outcome, "meta", {}) or {}
    text = " ".join(str(outcome).strip().splitlines()[:1])[:REASON_CHARS]
    if meta.get("error") == "timeout":
        return SyncOutcome(SYNC_TIME_LIMIT, f"the sync did not finish in {tool.timeout} s")
    if meta.get("error") == "unset_secret":
        return SyncOutcome(SYNC_SECRETS, text)
    if meta.get("status") not in (None, tools.STATUS_OK):
        return SyncOutcome(SYNC_FAILED, text or "the sync tool reported failure")
    return SyncOutcome(SYNC_OK)


# ---------------------------------------------------------------- the ledger


@dataclass(frozen=True)
class Transaction:
    id: str  # "<account>|<entry_id>"
    booking_date: str  # YYYY-MM-DD
    currency: str
    cents: int
    counterparty: str
    description: str
    creditor_accounts: tuple[str, ...]  # normalized, from the raw record


@dataclass
class Ledger:
    """The outgoing booked transactions, and whether the check ran."""

    readable: bool = False
    checked: bool = False
    reason: str = ""  # why it could not check, in code's words
    synced_to: str = ""  # YYYY-MM-DD: the OLDEST last sync among the linked accounts
    transactions: list[Transaction] = field(default_factory=list)


def _cents(amount: Any, amount_num: Any) -> int | None:
    try:
        value = decimal.Decimal(str(amount).strip()) if amount not in (None, "") else (
            decimal.Decimal(str(abs(float(amount_num)))))
    except (decimal.InvalidOperation, TypeError, ValueError):
        return None
    cents = abs(value) * 100
    if cents != cents.to_integral_value():
        return None
    return int(cents)


def _creditor_accounts(raw: Any) -> tuple[str, ...]:
    """The creditor's account in an Enable Banking transaction record, as the
    bank wrote it: `creditor_account.iban`, or `.other.identification`."""
    try:
        record = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        return ()
    account = record.get("creditor_account") if isinstance(record, dict) else None
    if not isinstance(account, dict):
        return ()
    values = [account.get("iban"), account.get("bban")]
    other = account.get("other")
    if isinstance(other, dict):
        values.append(other.get("identification"))
    return tuple(dict.fromkeys(mailcases.normalize_coordinate(str(v)) for v in values
                               if isinstance(v, str) and v.strip()))


def read_ledger(state_dir: os.PathLike | str, now: float) -> Ledger:
    """The bank ledger, opened read-only. Every failure is "could not check"
    with its own reason; nothing here writes to it."""
    path = ledger_path(state_dir)
    if not path.is_file():
        return Ledger(reason="there is no bank ledger (the bank plugin has never synced here)")
    # A read-only connection to a WAL database creates its -wal and -shm
    # sidecars when they are absent, and a read-only one cannot remove them:
    # a write to the state tree. With no sidecar on disk no connection holds
    # the ledger open, so it is read as immutable — nothing is created; with
    # one present, a writer may be at work, and its locks are honoured.
    busy = any(path.with_name(path.name + suffix).exists()
               for suffix in ("-wal", "-journal"))
    mode = "mode=ro" if busy else "mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?{mode}", uri=True, timeout=LEDGER_BUSY_WAIT_S)
    except sqlite3.Error as exc:
        return Ledger(reason=f"the bank ledger could not be opened ({type(exc).__name__})")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        linked = ("SELECT alias FROM accounts WHERE session_uid IS NOT NULL"
                  if "session_uid" in columns else "SELECT alias FROM accounts")
        aliases = [str(r[0]) for r in conn.execute(linked)]
        stamps = {str(k): str(v) for k, v in conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'last_sync_at:%'")}
        rows = conn.execute(
            "SELECT account, entry_id, booking_date, amount, amount_num, currency, "
            "counterparty, description, raw FROM transactions "
            "WHERE direction = 'out' AND status = ?", (BOOKED,)).fetchall()
    except sqlite3.OperationalError as exc:
        word = ("was locked" if "locked" in str(exc).lower()
                else "does not have the expected tables")
        return Ledger(reason=f"the bank ledger {word} ({type(exc).__name__})")
    except sqlite3.DatabaseError as exc:
        return Ledger(reason=f"the bank ledger could not be read ({type(exc).__name__})")
    finally:
        conn.close()
    ledger = Ledger(readable=True)
    for account, entry, booked, amount, amount_num, currency, party, text, raw in rows:
        cents = _cents(amount, amount_num)
        # Only what a match compares and a reader may render: a real date and
        # a currency code. Anything else in the row is the other party's text.
        if (not cents or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(booked or ""))
                or not re.fullmatch(r"[A-Z]{3}", str(currency or "").upper())):
            continue
        ledger.transactions.append(Transaction(
            f"{account}|{entry}", str(booked), str(currency or "").upper(), cents,
            str(party or ""), str(text or ""), _creditor_accounts(raw)))
    ledger.transactions.sort(key=lambda t: (t.booking_date, t.id))
    synced = []
    for alias in aliases:
        try:
            synced.append(float(stamps[f"last_sync_at:{alias}"]))
        except (KeyError, ValueError):
            ledger.reason = "a linked bank account was never synced"
            return ledger
    if not synced:
        ledger.reason = "the bank ledger holds no linked account"
        return ledger
    oldest = min(synced)
    ledger.synced_to = _day(oldest).isoformat()
    if now - oldest > FRESH_DAYS * 86400:
        ledger.reason = f"the oldest linked account was last synced over {FRESH_DAYS} days ago"
        return ledger
    ledger.checked = True
    return ledger


# ---------------------------------------------------------------- amounts at ingest


def amounts_path(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / AMOUNTS_NAME


def read_amounts(state_dir: os.PathLike | str) -> dict[str, frozenset[tuple[str, int]]]:
    """mail digest -> the amounts recorded for it (possibly none). The first
    entry for a mail stands: an amount is recorded once."""
    try:
        text = amounts_path(state_dir).read_text()
    except OSError:
        return {}
    out: dict[str, frozenset[tuple[str, int]]] = {}
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            digest = str(entry["mail"])
            amounts = frozenset((str(c), int(n)) for c, n in entry["amounts"])
        except (ValueError, KeyError, TypeError):
            continue
        out.setdefault(digest, amounts)
    return out


def _wants_pay(record: dict[str, Any] | None) -> bool:
    verdict = (record or {}).get("verdict")
    return (isinstance(verdict, dict) and verdict.get("wanted") == "pay"
            and (record or {}).get("ending") == "ok")


@dataclass
class Recorded:
    recorded: int = 0  # mails whose amounts were written (zero amounts included)
    no_body: int = 0  # pay-wanted, authenticated, but the body is no longer held
    unauthenticated: int = 0


def record_amounts(
    state_dir: os.PathLike | str, index: mailindex.MailIndex,
    records: Iterable[dict[str, Any]], *, now: float, by: str,
) -> Recorded:
    """For each verdict record whose reading wants `pay`, on an AUTHENTICATED
    mail not yet recorded: the amounts the extractor finds in its subject and
    body, written once. A mail with none found is recorded with none, so it is
    never re-read later from a body that may be gone."""
    held = read_amounts(state_dir)
    done = Recorded()
    lines = []
    for record in records:
        digest = str(record.get("mail") or "")
        if digest in held or not _wants_pay(record):
            continue
        header = index.get(digest)
        if header is None:
            continue
        if not mailgate.authenticate_header(header).authenticated:
            done.unauthenticated += 1
            continue
        body = index.body(digest)
        if body is None:
            done.no_body += 1
            continue
        amounts = mailcases.extract_amounts(f"{header.subject}\n{body}")
        lines.append(json.dumps({"mail": digest, "amounts": [list(a) for a in amounts],
                                 "at": now, "by": by}, sort_keys=True))
        held[digest] = frozenset(amounts)
        done.recorded += 1
    if lines:
        path = amounts_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write("\n".join(lines) + "\n")
    return done


def backfill_amounts(state_dir: os.PathLike | str, *, now: float) -> Recorded:
    """The one-time backfill: every existing reading that wants `pay`, whose
    body the index still holds."""
    readings, _unreadable = mailreport.load_readings(state_dir)
    index = mailindex.MailIndex(state_dir)
    records = [r.record for r in readings.values() if r.record is not None]
    return record_amounts(state_dir, index, records, now=now, by="backfill")


# ---------------------------------------------------------------- the match


def item_amounts(ctx: Any, digest: str) -> frozenset[tuple[str, int]]:
    """Every amount recorded for the mail: the case store's, and the ones
    recorded at ingest for a pay-wanted mail."""
    return frozenset(ctx.stored_amounts(digest)) | ctx.bank.amounts.get(digest, frozenset())


def item_accounts(ctx: Any, digest: str) -> frozenset[str]:
    """Accounts VALIDATED on the item's reading (code-extracted, or the
    Reader's checksum-valid ones) or recorded for its case."""
    out: set[str] = set()
    reading = ctx.readings.get(digest)
    for record in (reading.records if reading is not None else ()):
        coordinates = record.get("coordinates") if isinstance(record, dict) else None
        if isinstance(coordinates, dict):
            out.update(str(c) for c in coordinates.get("code") or () if c)
        verdict = record.get("verdict") if isinstance(record, dict) else None
        for c in (verdict or {}).get("payment_coordinates") or () if isinstance(
                verdict, dict) else ():
            if isinstance(c, dict) and c.get("kind") == "iban" and c.get("value"):
                out.add(str(c["value"]))
    for case in ctx.cases_of(digest):
        out.update(str(c.get("value") or "") for c in case.coordinates)
    return frozenset(mailcases.normalize_coordinate(v) for v in out if v)


def item_keys(ctx: Any, digest: str) -> frozenset[str]:
    """The case keys the correlator stored on the item's trusted cases."""
    return frozenset(k for case in ctx.cases_of(digest) for k, info in case.keys.items()
                     if ctx._trusted_fact(info))


def tie(ctx: Any, item: Any, tx: Transaction) -> str:
    """What ties a transaction to the item beyond its amount — strong (a
    case key in the description, or the creditor account), named (one of
    the party's entity tokens in the counterparty or description, Polish
    diacritics folded on both sides) — or "" when nothing does."""
    upper = tx.description.upper()
    if any(mailcases._says_key(upper, key) for key in item_keys(ctx, item.anchor)):
        return TIE_STRONG
    if set(tx.creditor_accounts) & item_accounts(ctx, item.anchor):
        return TIE_STRONG
    folded = f"{mailreport.fold(tx.counterparty)} {mailreport.fold(tx.description)}"
    tokens = {mailreport.fold(t) for t in ctx.entity_tokens(item.counterparty)}
    if any(t and t in folded for t in tokens):
        return TIE_NAMED
    return ""


def in_window(ctx: Any, item: Any, tx: Transaction, restored_at: float) -> bool:
    """Booked no earlier than `BOOKED_BEFORE_MAIL_DAYS` before the mail
    arrived, not later than today, and — after his restore — only a payment
    booked after the day he restored it (his word outranks earlier evidence)."""
    reading = ctx.readings.get(item.anchor)
    arrived = reading.when if reading is not None else item.anchor_read
    first = (_day(arrived) - datetime.timedelta(days=BOOKED_BEFORE_MAIL_DAYS)).isoformat()
    if not first <= tx.booking_date <= _day(ctx.now).isoformat():
        return False
    return not restored_at or tx.booking_date > _day(restored_at).isoformat()


def match(ctx: Any, item: Any, tx: Transaction, restored_at: float) -> str:
    """The tie when the transaction settles the item, else ""."""
    if (tx.currency, tx.cents) not in item_amounts(ctx, item.anchor):
        return ""
    if not in_window(ctx, item, tx, restored_at):
        return ""
    return tie(ctx, item, tx)


# ---------------------------------------------------------------- the pairing


@dataclass(frozen=True)
class Pairing:
    tx: Transaction
    tie: str
    recorded: bool  # already in the pairing ledger (re-verified), or new this read


def pairs_path(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / PAIRS_NAME


def read_pairs(state_dir: os.PathLike | str) -> list[tuple[str, str]]:
    """(transaction id, item key), oldest first. A line that does not parse
    costs that line."""
    try:
        text = pairs_path(state_dir).read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            out.append((str(entry["tx"]), str(entry["item"])))
        except (ValueError, KeyError, TypeError):
            continue
    return out


class Bank:
    """What one read of the stores knows about the bank: the ledger, the
    amounts recorded at ingest and the pairing ledger."""

    def __init__(self, state_dir: os.PathLike | str | None, now: float) -> None:
        if state_dir is None:
            self.ledger = Ledger(reason="no state directory")
            self.amounts: dict[str, frozenset[tuple[str, int]]] = {}
            self.pairs: list[tuple[str, str]] = []
            return
        self.ledger = read_ledger(state_dir, now)
        self.amounts = read_amounts(state_dir)
        self.pairs = read_pairs(state_dir)


def assign(ctx: Any, eligible: list[tuple[Any, float]]) -> dict[str, Pairing]:
    """item key -> the transaction that settles it.

    Recorded pairs are re-verified first: the transaction still in the
    ledger, still matching, the item still eligible. Then — only when the
    check ran (a fresh ledger) — new pairs, in a fixed order: items oldest
    first, each taking the EARLIEST-booked matching transaction no pairing
    ever recorded or handed out this read. A recorded transaction is spent,
    whatever became of its item: restoring bill A never passes A's payment to
    an unpaid bill B of the same amount. One item, at most one transaction."""
    bank = ctx.bank
    if not bank.ledger.readable:
        return {}
    by_id = {t.id: t for t in bank.ledger.transactions}
    spent = {tx for tx, _item in bank.pairs}
    recorded: dict[str, list[str]] = {}
    for tx, key in bank.pairs:
        recorded.setdefault(key, []).append(tx)
    out: dict[str, Pairing] = {}
    for item, restored_at in eligible:
        for tx_id in recorded.get(item.key, ()):
            tx = by_id.get(tx_id)
            found = match(ctx, item, tx, restored_at) if tx is not None else ""
            if tx is not None and found:
                out[item.key] = Pairing(tx, found, recorded=True)
                break
    if not bank.ledger.checked or not getattr(ctx, "bank_new_pairs", True):
        return out
    for item, restored_at in sorted(eligible, key=lambda e: (e[0].anchor_read, e[0].anchor)):
        if item.key in out:
            continue
        for tx in bank.ledger.transactions:  # earliest booked first
            if tx.id in spent:
                continue
            found = match(ctx, item, tx, restored_at)
            if found:
                out[item.key] = Pairing(tx, found, recorded=False)
                spent.add(tx.id)
                break
    return out


def record_pairs(state_dir: os.PathLike | str, pairings: dict[str, Pairing], *,
                 now: float, by: str) -> int:
    """Append the pairs first made by this read. Only the report run and an
    applied item act write here, each under the item acts' lock."""
    lines = [json.dumps({"tx": p.tx.id, "item": key, "tie": p.tie, "at": now, "by": by},
                        sort_keys=True)
             for key, p in sorted(pairings.items()) if not p.recorded]
    if lines:
        path = pairs_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write("\n".join(lines) + "\n")
    return len(lines)


# ---------------------------------------------------------------- words


def money_words(currency: str, cents: int) -> str:
    whole, part = divmod(int(cents), 100)
    return f"{whole:,}".replace(",", " ") + f",{part:02d} {currency}"


def date_words(day: str) -> str:
    """`10 Nov` from `2026-11-10`; the text as given when it is not a date."""
    try:
        when = datetime.date.fromisoformat(day)
    except ValueError:
        return day
    return f"{when.day} {when.strftime('%b')}"


def evidence_words(currency: str, cents: int, day: str) -> str:
    """The evidence line: the transaction's own date and amount, nothing the
    other party wrote."""
    return f"the bank shows a payment of {money_words(currency, cents)} on {date_words(day)}"


def status_words(ledger: Ledger, sync: SyncOutcome | None) -> str:
    """The brief's bank clause: where the bank stands."""
    if ledger.checked:
        return f"Bank checked to {date_words(ledger.synced_to)}."
    detail = []
    if sync is not None and sync.status == SYNC_TIME_LIMIT:
        detail.append(sync.reason)
    detail.append(f"last synced {date_words(ledger.synced_to)}" if ledger.synced_to
                  else "no sync recorded")
    return f"Could not check the bank ({'; '.join(detail)})."
