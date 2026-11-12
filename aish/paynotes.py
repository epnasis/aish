"""The payment notes index: the owner's bill notes, readable without the vault.

The owner keeps one Obsidian note per bill in `Payments/`. Only the aish-web
process can open the vault (it holds the macOS privacy grant; every other
process gets a hang or a refusal), yet the mail and brief code and offline
analysis need what those notes say. So aish-web exports a compact INDEX of
them into the state tree, and everything else reads the index.

**The vault is read only through the owner's own plugin tools** — one
`obsidian_find` listing of the folder, then `obsidian_read` per note — run
through `tool_plugins.discover` + `tool_plugins.execute` exactly as a model's
call would be. No shell, no direct file access from repo code: the plugin owns
path confinement and iCloud placeholder handling, and a second reader of the
vault would be a second set of those rules to keep in step.

**The refresh is incremental.** The listing gives each note's `modified`
(local mtime, minute resolution), `size` and `offloaded`; that triple is the
note's marker. A note is read when it is new, its marker changed, its last
read did not succeed, or the parser changed since. An offloaded note already
read is never downloaded to check it; if its marker moved it is flagged
`stale` instead (`docs/payments.md`). At most `MAX_READS_PER_RUN` reads per
run, never-tried first and failed last; the rest are `pending` and counted,
and the loop comes back sooner only while runs are making progress.

**Parsing records, it never resolves.** Every field is typed or recorded as
unparsed with its raw text capped. Frontmatter and the body's detail bullets
fill each other's gaps; where both are present and disagree the field is
null and the disagreement is a `conflicts` entry, because which one is right
is the owner's call, not this module's.

The files hold the owner's financial data: `<state>/payments/`, mode 0600,
never rendered to a model by this module, and nothing here logs a note's
content or an account number.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import unicodedata
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import atomic_write, mailcases, tool_plugins

LIST_TOOL = "obsidian_find"
READ_TOOL = "obsidian_read"
FOLDER = "Payments"
SCHEMA = 1
#: Bumped whenever parsing changes, so every note is re-read once under the
#: new parser instead of keeping what the old one made of it.
PARSER_VERSION = 1

#: Reads one refresh may make. Each is a wrapper subprocess, and a read of an
#: offloaded note may wait up to the plugin's own `wait` (20 s) for iCloud.
MAX_READS_PER_RUN = 150
#: `obsidian_find` returns at most `max` hits (default 20); the folder holds
#: ~500. Far above it, so a listing that still came back cut is visible as
#: `listing_truncated` rather than as notes silently missing.
LIST_MAX = 100_000
#: `obsidian_find` reads `preview_lines` as `int(x or 3)`, so 0 cannot turn the
#: preview off; 1 is the smallest it honours.
LIST_PREVIEW_LINES = 1
#: Output cap handed to `execute`; well above any listing of this folder.
OUTPUT_CAP = 8 * 1024 * 1024

RAW_CAP = 120
REASON_CAP = 300

REFRESH_EVERY = timedelta(days=1)
#: While reads remain after a bounded run, the next one comes this soon.
BACKLOG_EVERY = timedelta(minutes=5)

ENV_SWITCH = "AISH_PAYNOTES"

# Outcome words, recorded in index.json.
OK = "ok"
PARTIAL = "partial"  # listed and read what the bound allowed; reads remain
NO_TOOL = "no tool"
LIST_FAILED = "list failed"
ERROR = "error"

RunTool = Callable[[str, dict], str]


# ------------------------------------------------------------------ paths


def payments_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "payments"


def notes_path(state_dir: Path) -> Path:
    return payments_dir(state_dir) / "notes.jsonl"


def index_path(state_dir: Path) -> Path:
    return payments_dir(state_dir) / "index.json"


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "1").strip() != "0"


# ------------------------------------------------------------ the tool seam


def plugin_runner(cwd: str) -> RunTool | None:
    """`run_tool` over the owner's installed plugins, or None when either tool
    is absent. A tool that declares itself mutating is treated as absent: the
    refresh runs unattended and may only ever read."""
    tools, _warnings = tool_plugins.discover(cwd)
    by_name = {t.name: t for t in tools}
    for name in (LIST_TOOL, READ_TOOL):
        if name not in by_name or by_name[name].mutating:
            return None

    def run(name: str, args: dict) -> str:
        tool = by_name[name]
        problem = tool_plugins.validate_args(tool, args)
        if problem:
            return str(tool_plugins.envelope(name, problem + "\n", 2))
        return str(
            tool_plugins.execute(
                tool, args, cwd, caps=(OUTPUT_CAP, OUTPUT_CAP), cap_source="paynotes"
            )
        )

    return run


_EXIT_RE = re.compile(r"\n?\[exit code: (-?\d+)\]")


def _split_envelope(text: str) -> tuple[int | None, str]:
    """(exit code, the wrapper's own output) of one `execute` result."""
    matches = list(_EXIT_RE.finditer(text or ""))
    if not matches:
        return None, (text or "").strip()
    last = matches[-1]
    return int(last.group(1)), text[: last.start()].strip()


def _call_json(run_tool: RunTool, name: str, args: dict) -> tuple[dict | None, str]:
    """(payload, "") on success, (None, reason) otherwise. The reason is the
    plugin's own words, capped."""
    exit_code, body = _split_envelope(run_tool(name, args))
    if exit_code != 0:
        why = body or f"{name} exited {exit_code} with no output"
        return None, _cap(why, REASON_CAP)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, f"{name} output was not JSON"
    if not isinstance(payload, dict):
        return None, f"{name} output was not a JSON object"
    return payload, ""


# ----------------------------------------------------------------- parsing


def _cap(text: Any, limit: int = RAW_CAP) -> str:
    value = str(text)
    return value if len(value) <= limit else value[:limit] + "…"


def _scalar(value: Any) -> str:
    """The plugin's frontmatter parser yields strings or lists; an empty value
    (`key:` or `key: []`) is an empty list. Both mean "nothing written"."""
    if isinstance(value, list):
        return "" if not value else ", ".join(str(v) for v in value)
    return "" if value is None else str(value).strip()


_AMOUNT_RE = re.compile(
    r"^(?P<int>\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,](?P<frac>\d{1,2}))?"
    r"(?:\s*(?P<cur>[A-Za-z]{3}|zł))?$"
)


def parse_amount(raw: str) -> tuple[int, str | None] | None:
    """(minor units, currency written beside it or None), or None when the
    text is not unambiguously an amount. `1,234` is refused: three digits
    after a comma could be thousands or a typo, and a guess is worse than a
    gap."""
    text = unicodedata.normalize("NFC", raw or "").strip()
    match = _AMOUNT_RE.match(text)
    if not match:
        return None
    whole = int(re.sub(r"\D", "", match.group("int")))
    frac = (match.group("frac") or "0").ljust(2, "0")
    currency = match.group("cur")
    if currency:
        currency = "PLN" if currency.lower() == "zł" else currency.upper()
    return whole * 100 + int(frac), currency


def parse_paid(raw: str) -> str:
    folded = raw.strip().casefold()
    if folded in {"true", "yes", "1"}:
        return "true"
    if folded in {"false", "no", "0"}:
        return "false"
    return "unknown"


def parse_date(raw: str) -> str | None:
    text = raw.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def parse_month(raw: str) -> str | None:
    text = raw.strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if not match or not 1 <= int(match.group(2)) <= 12:
        return None
    return text


def parse_account(raw: str) -> str | None:
    """The repo's one coordinate normaliser (`mailcases.normalize_coordinate`):
    spaces out, upper case, a bare 26-digit Polish account read as its PL
    IBAN — so this index and the mail cases compare in one space."""
    text = raw.strip().strip("`").strip()
    value = mailcases.normalize_coordinate(text)
    # An IBAN's shape, or a bare run of digits (a foreign domestic account).
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}|\d{10,34}", value):
        return None
    return value


# A paid line is a heading or a ✅ (or both) before "Zapłacono", any case.
_PAID_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*(?:✅️?\s*)?|✅️?\s*)zapłacono\b(?P<rest>.*)$", re.IGNORECASE
)
_PAID_REST_RE = re.compile(r"^\s*(?P<date>\d{4}-\d{2}-\d{2})\s*(?:[—–-]\s*(?P<amount>.+?))?\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s*\*\*(?P<label>[^*]+?)\*\*\s*:?\s*(?P<value>.*?)\s*$")
# Whole-value only: `FV 123/2026` is not the invoice number `FV`.
_INVOICE_TOKEN = r"[A-Za-z0-9][A-Za-z0-9/_.\-]*"
_INVOICE_RE = re.compile(rf"(?:nr\.?\s*)?(?P<number>{_INVOICE_TOKEN})")


def parse_invoice(raw: str) -> str | None:
    """The invoice number when the WHOLE value is one, else None."""
    match = _INVOICE_RE.fullmatch(raw.strip().strip("`").strip())
    return match.group("number") if match else None


def _label(raw: str) -> str:
    return raw.strip().rstrip(":").strip().casefold()


def parse_body(body: str) -> dict[str, Any]:
    """The paid lines and the detail bullets the owner's notes carry."""
    out: dict[str, Any] = {
        "paid_lines": [],
        "amounts": [],
        "due": [],
        "accounts": [],
        "invoices": [],
        "unparsed": {},
    }
    unparsed: dict[str, str] = out["unparsed"]
    for line in unicodedata.normalize("NFC", body or "").split("\n"):
        paid = _PAID_LINE_RE.match(line)
        if paid:
            rest = _PAID_REST_RE.match(paid.group("rest"))
            entry: dict[str, Any] = {"date": None, "minor": None, "currency": None}
            if rest and parse_date(rest.group("date")):
                entry["date"] = parse_date(rest.group("date"))
                amount = parse_amount(rest.group("amount") or "") if rest.group("amount") else None
                if amount:
                    entry["minor"], entry["currency"] = amount[0], amount[1]
                elif rest.group("amount"):
                    entry["raw"] = _cap(rest.group("amount"))
            else:
                entry["raw"] = _cap(paid.group("rest").strip())
            out["paid_lines"].append(entry)
            continue
        bullet = _BULLET_RE.match(line)
        if not bullet:
            continue
        label, value = _label(bullet.group("label")), bullet.group("value").strip()
        if label.startswith("kwota"):
            amount = parse_amount(value)
            if amount:
                out["amounts"].append(amount)
            else:
                unparsed.setdefault("body.amount", _cap(value))
        elif label.startswith("termin"):
            due = parse_date(value)
            if due:
                out["due"].append(due)
            else:
                unparsed.setdefault("body.date_due", _cap(value))
        elif label.startswith("rachunek"):
            account = parse_account(value)
            if account:
                out["accounts"].append(account)
            else:
                unparsed.setdefault("body.account_number", "(not an account number)")
        elif label.startswith("faktura"):
            invoice = parse_invoice(value)
            if invoice:
                out["invoices"].append(invoice)
            else:
                unparsed.setdefault("body.invoice_number", _cap(value))
    return out


def _one(values: list) -> tuple[Any, bool]:
    """(the single distinct value or None, whether several distinct values exist)."""
    distinct = list(dict.fromkeys(values))
    if len(distinct) == 1:
        return distinct[0], False
    return None, len(distinct) > 1


def parse_note(path: str, frontmatter: dict, body: str, attachments: list, tags: list) -> dict:
    """The typed fields of one note read. Pure: no I/O, no clock."""
    fm = frontmatter if isinstance(frontmatter, dict) else {}
    name = Path(path).name
    record: dict[str, Any] = {
        "has_frontmatter": bool(fm),
        "category": _scalar(fm.get("category")) or None,
        "paid_to": _scalar(fm.get("paid_to")) or None,
        "paid_for": _scalar(fm.get("paid_for")) or None,
        "created": _scalar(fm.get("created")) or None,
        "updated": _scalar(fm.get("updated")) or None,
        "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
        "attachments": [str(a) for a in attachments] if isinstance(attachments, list) else [],
    }
    unparsed: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    parsed_body = parse_body(body)
    unparsed.update(parsed_body["unparsed"])

    def conflict(name_: str, from_fm: Any, from_body: Any) -> None:
        conflicts.append({"field": name_, "frontmatter": from_fm, "body": from_body})

    # amount + currency
    fm_currency_raw = _scalar(fm.get("currency"))
    fm_currency = fm_currency_raw.upper() if re.fullmatch(r"[A-Za-z]{3}", fm_currency_raw) else None
    if fm_currency_raw and not fm_currency:
        unparsed["currency"] = _cap(fm_currency_raw)
    raw_amount = _scalar(fm.get("amount"))
    fm_amount: dict | None = None
    if raw_amount:
        parsed = parse_amount(raw_amount)
        if parsed is None:
            unparsed["amount"] = _cap(raw_amount)
        else:
            minor, inline_currency = parsed
            if inline_currency and fm_currency and inline_currency != fm_currency:
                unparsed["currency"] = _cap(f"{inline_currency} beside {fm_currency}")
            else:
                currency = inline_currency or fm_currency
                fm_amount = {
                    "minor": minor,
                    "currency": currency or "PLN",
                    "currency_source": "frontmatter" if currency else "default",
                }
    body_amount: dict | None = None
    one, several = _one(parsed_body["amounts"])
    if several:
        conflict("amount", None, [{"minor": m, "currency": c} for m, c in
                                  dict.fromkeys(parsed_body["amounts"])])
    elif one is not None:
        minor, inline_currency = one
        currency = inline_currency or fm_currency
        body_amount = {
            "minor": minor,
            "currency": currency or "PLN",
            "currency_source": ("body" if inline_currency
                                else "frontmatter" if fm_currency else "default"),
        }
    amount: dict | None = None
    if fm_amount and body_amount:
        if (fm_amount["minor"], fm_amount["currency"]) == (
            body_amount["minor"], body_amount["currency"]
        ):
            amount = {**fm_amount, "source": "frontmatter"}
        else:
            conflict("amount", fm_amount, body_amount)
    elif fm_amount:
        amount = {**fm_amount, "source": "frontmatter"}
    elif body_amount and not several:
        amount = {**body_amount, "source": "body"}
    record["amount"] = amount
    record["amount_raw"] = _cap(raw_amount) if raw_amount else None

    # month: frontmatter, else the file name's YYYY-MM prefix
    raw_month = _scalar(fm.get("month"))
    fm_month = parse_month(raw_month) if raw_month else None
    if raw_month and not fm_month:
        unparsed["month"] = _cap(raw_month)
    name_match = re.match(r"^(\d{4}-\d{2})\s", name)
    name_month = parse_month(name_match.group(1)) if name_match else None
    if fm_month and name_month and fm_month != name_month:
        conflicts.append({"field": "month", "frontmatter": fm_month, "file_name": name_month})
        record["month"], record["month_source"] = None, "conflict"
    elif fm_month:
        record["month"], record["month_source"] = fm_month, "frontmatter"
    elif name_month:
        record["month"], record["month_source"] = name_month, "file_name"
    else:
        record["month"], record["month_source"] = None, None

    # the three gap-fillable scalars
    def merge(field_name: str, fm_value: Any, body_values: list) -> None:
        body_value, body_several = _one(body_values)
        if body_several:
            conflict(field_name, fm_value, list(dict.fromkeys(body_values)))
            record[field_name], record[f"{field_name}_source"] = None, "conflict"
        elif fm_value is not None and body_value is not None and fm_value != body_value:
            conflict(field_name, fm_value, body_value)
            record[field_name], record[f"{field_name}_source"] = None, "conflict"
        elif fm_value is not None:
            record[field_name], record[f"{field_name}_source"] = fm_value, "frontmatter"
        elif body_value is not None:
            record[field_name], record[f"{field_name}_source"] = body_value, "body"
        else:
            record[field_name], record[f"{field_name}_source"] = None, None

    raw_due = _scalar(fm.get("date_due"))
    fm_due = parse_date(raw_due) if raw_due else None
    if raw_due and not fm_due:
        unparsed["date_due"] = _cap(raw_due)
    merge("date_due", fm_due, parsed_body["due"])

    raw_invoice = _scalar(fm.get("invoice_number"))
    # Kept verbatim when the whole value is one token; otherwise unparsed.
    fm_invoice = raw_invoice if raw_invoice and re.fullmatch(_INVOICE_TOKEN, raw_invoice) else None
    if raw_invoice and not fm_invoice:
        unparsed["invoice_number"] = _cap(raw_invoice)
    merge("invoice_number", fm_invoice, parsed_body["invoices"])

    raw_account = _scalar(fm.get("account_number"))
    fm_account = parse_account(raw_account) if raw_account else None
    if raw_account and not fm_account:
        # The raw text is never kept for an account: it may be most of one.
        unparsed["account_number"] = "(not an account number)"
    merge("account_number", fm_account, parsed_body["accounts"])
    account = record["account_number"]
    record["account_valid"] = (
        mailcases.iban_valid(account) if account and account[:2].isalpha() else None
    )

    # paid: frontmatter, with a paid line in the body as evidence of "true"
    raw_paid = _scalar(fm.get("paid"))
    fm_paid = parse_paid(raw_paid) if raw_paid else "unknown"
    if raw_paid and fm_paid == "unknown":
        unparsed["paid"] = _cap(raw_paid)
    has_paid_line = any(p.get("date") for p in parsed_body["paid_lines"])
    if fm_paid == "false" and has_paid_line:
        conflict("paid", "false", "paid line")
        record["paid"], record["paid_source"] = "conflict", "conflict"
    elif fm_paid != "unknown":
        record["paid"], record["paid_source"] = fm_paid, "frontmatter"
    elif has_paid_line:
        record["paid"], record["paid_source"] = "true", "body"
    else:
        record["paid"], record["paid_source"] = "unknown", None

    record["paid_lines"] = parsed_body["paid_lines"]
    record["conflicts"] = conflicts
    record["unparsed"] = unparsed
    return record


# ------------------------------------------------------------- the refresh


def _marker(hit: dict) -> dict:
    return {
        "modified": str(hit.get("modified") or ""),
        "size": hit.get("size") if isinstance(hit.get("size"), int) else None,
        "offloaded": bool(hit.get("offloaded")),
    }


def _marker_moved(previous: dict, marker: dict) -> bool:
    old = previous.get("marker") or {}
    return (old.get("modified"), old.get("size")) != (marker["modified"], marker["size"])


def _needs_read(previous: dict | None, marker: dict) -> bool:
    if previous is None or previous.get("status") != "read":
        return True
    if previous.get("parser") != PARSER_VERSION:
        return True
    if marker["offloaded"]:
        # Never downloaded just to check: a moved marker on an offloaded note
        # is marked `stale` instead (eviction alone moves it too).
        return False
    return _marker_moved(previous, marker)


def _read_priority(previous: dict | None) -> int:
    """Budget order: never tried first, then changed notes, then notes whose
    last read failed — so one note that keeps failing cannot starve the rest."""
    if previous is None or (previous.get("status") == "pending"
                            and not previous.get("tried_at")):
        return 0
    if previous.get("status") == "read":
        return 1
    return 2


def _stamp(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _read_previous(state_dir: Path) -> tuple[dict[str, dict], dict]:
    """The last index's records by path, and its index.json. A previous state
    that cannot be read is a reason to re-read everything, never to fail."""
    records: dict[str, dict] = {}
    try:
        for line in notes_path(state_dir).read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if isinstance(record, dict) and isinstance(record.get("path"), str):
                    records[record["path"]] = record
    except (OSError, ValueError):
        records = {}
    try:
        index = json.loads(index_path(state_dir).read_text(encoding="utf-8"))
        if not isinstance(index, dict):
            index = {}
    except (OSError, ValueError):
        index = {}
    return records, index


def _publish(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    atomic_write.publish(path, text, encoding="utf-8")
    # mkstemp already creates 0600; stated here so the law has its own line.
    os.chmod(path, 0o600)


def _write_index(state_dir: Path, index: dict) -> None:
    _publish(index_path(state_dir), json.dumps(index, ensure_ascii=False, indent=2) + "\n")


def _failed(state_dir: Path, previous_index: dict, now: datetime, outcome: str, why: str) -> dict:
    """index.json for a run that wrote no notes: what happened now, beside what
    the notes file (untouched) still describes."""
    index = {key: previous_index[key] for key in (
        "notes_written_at", "notes_sha256", "counts", "reads_remaining", "listing_truncated",
    ) if key in previous_index}
    index.update({
        "schema": SCHEMA,
        "refreshed_at": _stamp(now),
        "outcome": outcome,
        "error": why,
    })
    _write_index(state_dir, index)
    return index


def counts_of(records: list[dict]) -> dict[str, int]:
    def status(name: str) -> int:
        return sum(1 for r in records if r.get("status") == name)

    read = [r for r in records if r.get("status") == "read"]
    return {
        "listed": len(records),
        "read": status("read"),
        "unread": status("unread"),
        "pending": status("pending"),
        "unparsed_amount": sum(1 for r in read if "amount" in (r.get("unparsed") or {})),
        "no_amount": sum(1 for r in read if r.get("amount") is None),
        "paid_true": sum(1 for r in read if r.get("paid") == "true"),
        "paid_false": sum(1 for r in read if r.get("paid") == "false"),
        "paid_unknown": sum(1 for r in read if r.get("paid") == "unknown"),
        "paid_conflict": sum(1 for r in read if r.get("paid") == "conflict"),
        "with_conflicts": sum(1 for r in read if r.get("conflicts")),
        "stale": sum(1 for r in read if r.get("stale")),
    }


def refresh(
    run_tool: RunTool | None,
    state_dir: Path,
    now: datetime,
    *,
    max_reads: int | None = None,
    stop: threading.Event | None = None,
) -> dict:
    """List `Payments/`, read what is new or changed, write the index. Returns
    the index.json it wrote. Never raises for anything the vault or the plugin
    does; an unexpected exception is recorded as outcome `error` by the loop."""
    state_dir = Path(state_dir)
    previous, previous_index = _read_previous(state_dir)
    if run_tool is None:
        return _failed(state_dir, previous_index, now, NO_TOOL,
                       f"{LIST_TOOL} or {READ_TOOL} is not installed as a read-only plugin")

    listing, why = _call_json(run_tool, LIST_TOOL, {
        "folder": FOLDER, "max": LIST_MAX, "preview_lines": LIST_PREVIEW_LINES,
    })
    hits = listing.get("hits") if listing else None
    if listing is None or not isinstance(hits, list):
        return _failed(state_dir, previous_index, now, LIST_FAILED,
                       why or f"{LIST_TOOL} output had no hits list")
    total = listing.get("total_matching")
    truncated = isinstance(total, int) and total > len(hits)

    bound = MAX_READS_PER_RUN if max_reads is None else max_reads
    listed = [h for h in hits if isinstance(h, dict) and isinstance(h.get("note"), str)]
    wanted = [h["note"] for h in listed if _needs_read(previous.get(h["note"]), _marker(h))]
    wanted.sort(key=lambda p: _read_priority(previous.get(p)))  # stable: newest first within
    budget = set(wanted[:bound])
    records: list[dict] = []
    reads = 0
    reads_ok = 0
    removed = len(set(previous) - {h["note"] for h in listed})
    for hit in listed:
        path = hit["note"]
        marker = _marker(hit)
        before = previous.get(path)
        base = {"path": path, "file": Path(path).name, "listed": marker}
        if before is not None and not _needs_read(before, marker):
            # `stale`: listed as changed since the read, but offloaded, so not
            # downloaded to find out — eviction alone also moves the marker.
            records.append({**before, **base, "stale": _marker_moved(before, marker)})
            continue
        if path not in budget or (stop is not None and stop.is_set()):
            # Whatever an earlier read made of it is kept, but it is not
            # counted as read: the note changed since, or was never read.
            records.append({**(before or {}), **base, "status": "pending"})
            continue
        reads += 1
        payload, why = _call_json(run_tool, READ_TOOL, {"note": path})
        if payload is not None and payload.get("note") != path:
            # The plugin's resolver falls back to a vault-wide name search when
            # the path is gone; a same-named note elsewhere is not this one.
            payload, why = None, "read resolved to a different note"
        if payload is None:
            kept = dict(before) if before else {}
            records.append({**kept, **base, "status": "unread", "unread_reason": why,
                            "tried_at": _stamp(now)})
            continue
        reads_ok += 1
        fields = parse_note(
            path,
            payload.get("frontmatter") or {},
            str(payload.get("content") or ""),
            payload.get("attachments") or [],
            payload.get("tags") or [],
        )
        records.append({**base, **fields, "status": "read", "marker": marker,
                        "parser": PARSER_VERSION, "read_at": _stamp(now)})

    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    _publish(notes_path(state_dir), text)
    counts = counts_of(records)
    counts["read_this_run"] = reads
    counts["read_ok_this_run"] = reads_ok
    counts["removed"] = removed
    remaining = counts["pending"]
    index = {
        "schema": SCHEMA,
        "refreshed_at": _stamp(now),
        "notes_written_at": _stamp(now),
        "outcome": PARTIAL if remaining else OK,
        "error": None,
        "counts": counts,
        "reads_remaining": remaining,
        "listing_truncated": truncated,
        "notes_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    _write_index(state_dir, index)
    return index


def refresh_recorded(
    run_tool: RunTool | None, state_dir: Path, now: datetime, **kwargs: Any
) -> dict:
    """`refresh`, with an unexpected exception recorded in index.json as
    outcome `error` instead of raised. Only the exception's TYPE is recorded:
    its message could quote a note."""
    try:
        return refresh(run_tool, state_dir, now, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the server outlives any refresh
        print(f"[paynotes] refresh failed: {type(exc).__name__}", file=sys.stderr)
        try:
            _, previous_index = _read_previous(Path(state_dir))
            return _failed(Path(state_dir), previous_index, now, ERROR, type(exc).__name__)
        except Exception as again:  # noqa: BLE001
            print(f"[paynotes] could not record the failure: {type(again).__name__}",
                  file=sys.stderr)
            return {"outcome": ERROR, "error": type(exc).__name__}


# --------------------------------------------------------------- the loop


def next_wait(index: dict) -> timedelta:
    """Sooner only while a backlog is being worked off: a partial run that
    made no successful read (iCloud offline, every read failing) waits the
    full day, or it would spend its whole budget on failures every 5 minutes."""
    progressed = (index.get("counts") or {}).get("read_ok_this_run", 0) > 0
    if index.get("outcome") == PARTIAL and progressed:
        return BACKLOG_EVERY
    return REFRESH_EVERY


class Refresher:
    """One per aish-web. Refreshes at start, then daily — sooner while reads
    remain. The work runs on its OWN single thread: it is a chain of wrapper
    subprocesses that can take minutes, and the default executor is for
    short calls a client needs (docs/web-server.md, L3)."""

    def __init__(
        self,
        state_dir: Path,
        *,
        make_runner: Callable[[], RunTool | None],
        clock: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.make_runner = make_runner
        self.clock = clock
        self.sleep = sleep
        self.stop = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aish-paynotes")

    def _once(self) -> dict:
        now = self.clock()
        try:
            runner = self.make_runner()
        except Exception as exc:  # noqa: BLE001 — recorded, never raised into the server
            why = f"plugin discovery failed: {type(exc).__name__}"
            print(f"[paynotes] {why}", file=sys.stderr)
            _, previous_index = _read_previous(self.state_dir)
            return _failed(self.state_dir, previous_index, now, ERROR, why)
        return refresh_recorded(runner, self.state_dir, now, stop=self.stop)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    index = await loop.run_in_executor(self._executor, self._once)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — the loop outlives any one run
                    print(f"[paynotes] run failed: {type(exc).__name__}", file=sys.stderr)
                    index = {}
                outcome = index.get("outcome")
                print(f"[paynotes] {outcome}: {(index.get('counts') or {}).get('listed', 0)} "
                      f"listed, {index.get('reads_remaining', 0)} reads remaining",
                      file=sys.stderr)
                await self.sleep(next_wait(index).total_seconds())
        finally:
            self.stop.set()
            self._executor.shutdown(wait=False, cancel_futures=True)


# ----------------------------------------------------------------- readers


@dataclass
class PaymentNote:
    path: str
    file: str
    status: str  # read | unread | pending
    month: str | None = None
    category: str | None = None
    paid_to: str | None = None
    paid_for: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    date_due: str | None = None
    paid: str = "unknown"  # true | false | unknown | conflict
    invoice_number: str | None = None
    account_number: str | None = None
    tags: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    paid_lines: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    unparsed: dict[str, str] = field(default_factory=dict)
    unread_reason: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_record(cls, record: dict) -> PaymentNote:
        amount = record.get("amount") or {}
        return cls(
            path=str(record.get("path")),
            file=str(record.get("file")),
            status=str(record.get("status")),
            month=record.get("month"),
            category=record.get("category"),
            paid_to=record.get("paid_to"),
            paid_for=record.get("paid_for"),
            amount_minor=amount.get("minor"),
            currency=amount.get("currency"),
            date_due=record.get("date_due"),
            paid=str(record.get("paid") or "unknown"),
            invoice_number=record.get("invoice_number"),
            account_number=record.get("account_number"),
            tags=list(record.get("tags") or []),
            attachments=list(record.get("attachments") or []),
            paid_lines=list(record.get("paid_lines") or []),
            conflicts=list(record.get("conflicts") or []),
            unparsed=dict(record.get("unparsed") or {}),
            unread_reason=record.get("unread_reason"),
            raw=record,
        )


@dataclass
class Loaded:
    """What `load` found. `available` False means NO index — never zero bills."""

    available: bool
    reason: str = ""
    notes: list[PaymentNote] = field(default_factory=list)
    index: dict = field(default_factory=dict)
    age: timedelta | None = None  # since the notes file was written


def load(state_dir: Path, now: datetime | None = None) -> Loaded:
    state_dir = Path(state_dir)
    try:
        index = json.loads(index_path(state_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Loaded(False, "no index: aish-web has not written one yet")
    except (OSError, ValueError) as exc:
        return Loaded(False, f"index unreadable: {type(exc).__name__}")
    if not isinstance(index, dict):
        return Loaded(False, "index unreadable: not a JSON object")
    digest = index.get("notes_sha256")
    if not digest:
        # The outcome word only: the error text can quote a note's name, and
        # this reason is what `aish paynotes` prints. index.json keeps it.
        last = index.get("outcome") or "unknown"
        return Loaded(False, f"no index: never refreshed successfully (last attempt: {last})",
                      index=index)
    try:
        data = notes_path(state_dir).read_bytes()
    except OSError as exc:
        return Loaded(False, f"notes file unreadable: {type(exc).__name__}", index=index)
    if hashlib.sha256(data).hexdigest() != digest:
        return Loaded(False, "notes file does not match the index", index=index)
    notes = [PaymentNote.from_record(json.loads(line))
             for line in data.decode("utf-8").splitlines() if line.strip()]
    age = None
    try:
        written = datetime.fromisoformat(str(index.get("notes_written_at")))
        age = (now or datetime.now()) - written
    except ValueError:
        pass
    return Loaded(True, "", notes, index, age)


def summary(state_dir: Path, now: datetime | None = None) -> str:
    """The owner's `aish paynotes`: counts, age and outcome — no note content."""
    loaded = load(state_dir, now)
    lines = ["payment notes index (read-only; only aish-web refreshes it — "
             "this command cannot open the vault)"]
    index = loaded.index
    if index.get("refreshed_at"):
        lines.append(f"last refresh attempt: {index['refreshed_at']} — outcome: "
                     f"{index.get('outcome')}")
    if not loaded.available:
        lines.append(loaded.reason)
        return "\n".join(lines)
    if loaded.age is not None:
        hours = loaded.age.total_seconds() / 3600
        lines.append(f"notes written: {index.get('notes_written_at')} ({hours:.1f} h ago)")
    counts = index.get("counts") or {}
    for key in ("listed", "read", "unread", "pending", "unparsed_amount", "no_amount",
                "paid_true", "paid_false", "paid_unknown", "paid_conflict",
                "with_conflicts", "stale", "removed", "read_this_run", "read_ok_this_run"):
        if key in counts:
            lines.append(f"  {key.replace('_', ' ')}: {counts[key]}")
    lines.append(f"  reads remaining: {index.get('reads_remaining', 0)}")
    if index.get("listing_truncated"):
        lines.append("  the listing came back cut: some notes are not in the index")
    return "\n".join(lines)
