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

**Invoice facts ride the same run.** After the notes, each indexed note's
attachments are fetched through `obsidian_attachment` into a private scratch
directory, read by the document pipeline, reduced to typed facts by
`invoicefacts`, and deleted — bounded per run, one at a time
(`refresh_invoices`, docs/payments.md "Invoice facts").

The files hold the owner's financial data: `<state>/payments/`, mode 0600,
never rendered to a model by this module, and nothing here logs a note's
content or an account number.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import atomic_write, files, invoicefacts, invoicereader, mailcases, tool_plugins

LIST_TOOL = "obsidian_find"
READ_TOOL = "obsidian_read"
INVOICE_TOOL = "obsidian_attachment"
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
BACKLOG_EVERY = timedelta(minutes=15)

ENV_SWITCH = "AISH_PAYNOTES"

# Outcome words, recorded in index.json.
OK = "ok"
PARTIAL = "partial"  # listed and read what the bound allowed; reads remain
NO_TOOL = "no tool"
LIST_FAILED = "list failed"
ERROR = "error"

#: (tool name, args[, downloads_dir=]) -> the `execute` envelope text.
RunTool = Callable[..., str]


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


class PluginRunner:
    """`run_tool` over the owner's installed plugins. `tools` names the ones
    this refresh may call: present and not declaring themselves mutating."""

    def __init__(self, by_name: dict[str, tool_plugins.Tool], cwd: str) -> None:
        self.by_name = by_name
        self.cwd = cwd
        self.tools = frozenset(by_name)

    def __call__(self, name: str, args: dict, downloads_dir: Path | None = None,
                 should_stop: Callable[[], bool] | None = None) -> str:
        tool = self.by_name[name]
        problem = tool_plugins.validate_args(tool, args)
        if problem:
            return str(tool_plugins.envelope(name, problem + "\n", 2))
        return str(
            tool_plugins.execute(
                tool, args, self.cwd, caps=(OUTPUT_CAP, OUTPUT_CAP), cap_source="paynotes",
                downloads_dir=downloads_dir, should_stop=should_stop,
            )
        )


def plugin_runner(cwd: str) -> PluginRunner | None:
    """`run_tool` over the owner's installed plugins, or None when either note
    tool is absent. A tool that declares itself mutating is treated as absent:
    the refresh runs unattended and may only ever read. The attachment tool is
    optional — without it the invoice half records `no tool` on its own."""
    tools, _warnings = tool_plugins.discover(cwd)
    by_name = {t.name: t for t in tools
               if t.name in (LIST_TOOL, READ_TOOL, INVOICE_TOOL) and not t.mutating}
    if LIST_TOOL not in by_name or READ_TOOL not in by_name:
        return None
    return PluginRunner(by_name, cwd)


def _tool_available(run_tool: RunTool | None, name: str) -> bool:
    """A runner that names its tools is asked; any other callable (a test's
    fake) is taken to have them all."""
    if run_tool is None:
        return False
    names = getattr(run_tool, "tools", None)
    return names is None or name in names


_EXIT_RE = re.compile(r"\n?\[exit code: (-?\d+)\]")


def _split_envelope(text: str) -> tuple[int | None, str]:
    """(exit code, the wrapper's own output) of one `execute` result."""
    matches = list(_EXIT_RE.finditer(text or ""))
    if not matches:
        return None, (text or "").strip()
    last = matches[-1]
    return int(last.group(1)), text[: last.start()].strip()


def _call_json(
    run_tool: RunTool, name: str, args: dict, **kwargs: Any
) -> tuple[dict | None, str]:
    """(payload, "") on success, (None, reason) otherwise. The reason is the
    plugin's own words, capped."""
    exit_code, body = _split_envelope(run_tool(name, args, **kwargs))
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
        "invoices",
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
    if "invoices" in previous_index:
        # The invoice half's own section: this half does not own it.
        index["invoices"] = previous_index["invoices"]
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


# --------------------------------------------------------- invoice facts
#
# The notes carry the bill; the attached invoice carries the facts a bank
# payment can be matched on: the account to pay and the amount. So each
# indexed note's attachments are fetched through the owner's own read-only
# `obsidian_attachment` plugin, read in a child process by the repo's document
# pipeline (`invoicereader`), reduced to FACTS per page by `invoicefacts`, and
# deleted. Only the facts are kept (docs/payments.md, "Invoice facts").

INVOICE_ENV_SWITCH = "AISH_PAYNOTES_INVOICES"
#: Attachments one run may fetch and read. Small: a scanned page is ~1 s and
#: ~150 MB of OCR (docs/documents-and-pdf.md), and this machine also runs a
#: local model.
MAX_INVOICES_PER_RUN = 25
#: Wall clock one run may spend on invoices, checked BETWEEN attachments.
INVOICE_RUN_BUDGET = timedelta(minutes=10)
#: Wall clock one attachment's reading may take (conversion and OCR, in the
#: reader child, which is killed at it), and the least it is given when the
#: run's budget is nearly spent.
ATTACHMENT_SECONDS = 180.0
ATTACHMENT_MIN_SECONDS = 30.0
#: Seconds the plugin may wait for iCloud to download an offloaded file (its
#: own default for attachments).
INVOICE_FETCH_WAIT = 60
#: A fetched copy above this is not read. Invoices are kilobytes to a few MB;
#: the bound keeps an arbitrary file from being loaded whole to be hashed.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
#: Visits in a row that read no new page before an attachment is given up on.
#: A choice: a permanent failure (a broken PDF, a page OCR cannot read) would
#: otherwise be fetched and read again on every run, for ever. A changed note
#: or a new extractor version gives it a fresh start.
MAX_FRUITLESS_VISITS = 3

# Outcome words for one attachment that was not (fully) read. Words, never the
# pipeline's sentence, because these are what `aish paynotes` prints; the
# sentence is kept in the record's `reason`.
FETCH_FAILED = "fetch failed"
TOO_LARGE = "too large"
NOT_A_DOCUMENT = invoicereader.NOT_A_DOCUMENT
CONVERSION_FAILED = invoicereader.CONVERSION_FAILED
OCR_UNAVAILABLE = invoicereader.OCR_UNAVAILABLE
OCR_FAILED = invoicereader.OCR_FAILED
PAGES_NOT_READ = invoicereader.PAGES_NOT_READ
INVOICE_ERROR = "error"

# How an attachment's words were obtained.
READ_TEXT = "text layer"
READ_OCR = "ocr"
READ_MIXED = "mixed"
NOT_READ = "not read"

NO_NOTES = "no notes"
DEFERRED = "deferred"


def invoices_path(state_dir: Path) -> Path:
    return payments_dir(state_dir) / "invoices.jsonl"


def scratch_dir(state_dir: Path) -> Path:
    """The ONE place a fetched invoice copy and its rendition ever exist:
    private (0700) and emptied before and after every attachment."""
    return payments_dir(state_dir) / "invoice-scratch"


def invoices_enabled() -> bool:
    return enabled() and os.environ.get(INVOICE_ENV_SWITCH, "1").strip() != "0"


#: (file, private rendition store, pages already read, seconds, should_stop)
#: -> `invoicereader.run_reader`'s answer: per-page facts, never text.
ReadDocument = Callable[[Path, Path, set[int], float, Callable[[], bool]], dict]


def read_document(source: Path, store: Path, skip: set[int], seconds: float,
                  should_stop: Callable[[], bool]) -> dict:
    """The file through the repo's one document pipeline, in a child process
    at the lowest priority, killed at `seconds` or on Stop
    (`invoicereader`)."""
    return invoicereader.run_reader(source, store, skip, seconds, should_stop)


def _read_as(pages: dict[int, dict]) -> str:
    hows = {p.get("how") for p in pages.values()}
    by_text = "text" in hows
    by_ocr = bool(hows & {"ocr", "ocr_empty"})
    if by_text and by_ocr:
        return READ_MIXED
    if by_text:
        return READ_TEXT
    if by_ocr:
        return READ_OCR
    return NOT_READ


def _empty_scratch(scratch: Path) -> None:
    shutil.rmtree(scratch, ignore_errors=True)


def _fresh_scratch(scratch: Path) -> tuple[Path, Path]:
    """(fetch dir, rendition store), both new and empty, under a 0700 root."""
    _empty_scratch(scratch)
    scratch.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(scratch.parent, 0o700)
    scratch.mkdir(mode=0o700)
    os.chmod(scratch, 0o700)
    fetch, store = scratch / "fetch", scratch / "renditions"
    fetch.mkdir(mode=0o700)
    store.mkdir(mode=0o700)
    return fetch, store


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: The plugin's fallback directory: `attachlib.download_dir(prefix=...)` is a
#: `mkdtemp` directly in the system temp dir with this prefix.
STRAY_DIR_PREFIX = "aish-obsidian-"


def _remove_stray(path: Path) -> bool:
    """Delete a copy the plugin left OUTSIDE the scratch — its fallback is a
    fresh `mkdtemp` that nothing else deletes — but only in exactly that
    shape: a regular file (not a symlink) in a `aish-obsidian-*` directory
    that sits directly in the system temp dir; then that directory if it is
    now empty. Nothing else is ever deleted."""
    temp = files.resolved(tempfile.gettempdir())
    try:
        if path.is_symlink() or not path.is_file():
            return False
        resolved = files.resolved(path)
    except OSError:
        return False
    if temp is None or resolved is None:
        return False
    parent = resolved.parent
    if parent.parent != temp or not parent.name.startswith(STRAY_DIR_PREFIX):
        return False
    try:
        resolved.unlink()
        if not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        return False
    return True


def _fetched_elsewhere(note_path: str, from_vault: str) -> bool:
    """Did the plugin's resolver find the file somewhere other than where the
    note's own attachments live — the note's folder, its `attachments/`, or
    the vault root's `attachments/`? Its last resort is a vault-wide search by
    name, and a same-named file elsewhere may belong to another bill."""
    note_dir = posixpath.dirname(note_path)
    allowed = {note_dir, posixpath.join(note_dir, "attachments"), "attachments"}
    return posixpath.dirname(from_vault.strip("/")) not in allowed


def _page_summary(pages: dict[int, dict], total: int) -> dict[str, int]:
    hows = [p.get("how") for p in pages.values()]
    done = sum(1 for h in hows if h != "unread")
    return {
        "pages": total,
        "pages_read": done,
        "pages_text": hows.count("text"),
        "pages_ocr": hows.count("ocr"),
        "pages_ocr_empty": hows.count("ocr_empty"),
        "pages_unread": max(0, total - done),
    }


def _process_attachment(
    run_tool: RunTool,
    read: ReadDocument,
    scratch: Path,
    note_path: str,
    name: str,
    previous: dict | None,
    stop: threading.Event | None,
    seconds: float,
) -> dict:
    """Fetch, read the pages not read before, extract, delete. Returns the
    record's outcome fields plus `progressed` (popped by the caller). Every
    path through here leaves the scratch directory empty."""
    fetch_dir, store = _fresh_scratch(scratch)
    should_stop = stop.is_set if stop is not None else (lambda: False)
    failed = {"status": "unread", "read_as": NOT_READ, "progressed": False}
    try:
        payload, why = _call_json(
            run_tool, INVOICE_TOOL,
            {"note": note_path, "name": name, "wait": INVOICE_FETCH_WAIT},
            downloads_dir=fetch_dir, should_stop=should_stop,
        )
        if payload is None:
            return {**failed, "fetch": "failed", "reason_class": FETCH_FAILED, "reason": why}
        # The plugin's report is a claim; the file on disk, inside the
        # scratch directory, is the evidence.
        reported = Path(str(payload.get("path") or ""))
        inside = bool(payload.get("path")) and files.contains(fetch_dir, reported, strict=True)
        if not inside or not reported.is_file():
            outside = bool(payload.get("path")) and not inside
            stray = outside and _remove_stray(reported)
            return {**failed, "fetch": "failed", "reason_class": FETCH_FAILED,
                    "reason": "the plugin reported no file inside aish's scratch directory",
                    **({"stray_removed": True} if stray else {}),
                    # Not in the plugin's fallback shape: left alone, and said.
                    **({"stray_copy_may_exist": True} if outside and not stray else {})}
        from_vault = str(payload.get("from_vault") or "").strip("/") or None
        where = {"from_vault": from_vault,
                 "fetched_elsewhere": bool(from_vault) and _fetched_elsewhere(
                     note_path, from_vault or "")}
        size = reported.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            return {**failed, **where, "fetch": "ok", "bytes": size, "reason_class": TOO_LARGE,
                    "reason": f"{size} bytes, above the {MAX_ATTACHMENT_BYTES} bytes read"}
        digest = _sha256_of(reported)
        same_file = (previous is not None and previous.get("sha256") == digest
                     and previous.get("extractor") == invoicefacts.EXTRACTOR_VERSION)
        if same_file and previous is not None and previous.get("status") == "read":
            # The note changed, its attachment did not: the facts stand.
            kept = {k: v for k, v in previous.items() if k not in ("path", "name")}
            return {**kept, **where, "fetch": "ok", "unchanged": True, "progressed": False}
        before: dict[int, dict] = {}
        if same_file and previous is not None:
            before = {int(k): v for k, v in (previous.get("page_detail") or {}).items()}
        done_before = {n for n, p in before.items() if p.get("how") != "unread"}
        answer = read(reported, store, done_before, seconds, should_stop)
        pages = dict(before)
        for number, page in (answer.get("pages") or {}).items():
            if int(number) not in done_before:
                pages[int(number)] = page
        # A killed reader answers no page count; a continued visit knows it.
        known = (previous or {}) if same_file else {}
        total = int(answer.get("pages_total") or known.get("pages") or 0)
        summary = _page_summary(pages, total)
        read_pages = summary["pages_read"]
        if total and read_pages >= total:
            status = "read"
        elif read_pages:
            status = "partial"
        else:
            status = "unread"
        unread = [pages[n] for n in sorted(pages) if pages[n].get("how") == "unread"]
        reason_class = answer.get("reason_class") or (unread[0].get("reason_class")
                                                      if unread else None)
        reason = answer.get("reason") or (unread[0].get("reason") if unread else None)
        if status != "read" and not reason_class:
            reason_class = PAGES_NOT_READ
        facts = invoicefacts.merge(
            [pages[n]["facts"] for n in sorted(pages) if pages[n].get("facts") is not None]
        ) if read_pages else None
        return {
            "status": status, "fetch": "ok", "bytes": size, "sha256": digest, **where,
            "kind": answer.get("kind") or known.get("kind"),
            "read_as": _read_as({n: p for n, p in pages.items() if p.get("how") != "unread"}),
            **summary,
            "page_detail": {str(n): pages[n] for n in sorted(pages)},
            "reason_class": None if status == "read" else reason_class,
            "reason": None if status == "read" else reason,
            "facts": facts,
            "progressed": read_pages > len(done_before),
        }
    finally:
        _empty_scratch(scratch)


#: What a record knows only from one particular file's bytes.
_BYTES_FACTS = ("sha256", "facts", "page_detail")


def _changed(previous: dict, note_marker: dict | None) -> bool:
    return (previous.get("extractor") != invoicefacts.EXTRACTOR_VERSION
            or previous.get("note_marker") != note_marker)


def _invoice_needs_read(previous: dict | None, note_marker: dict | None) -> bool:
    if previous is None or _changed(previous, note_marker):
        return True
    if previous.get("status") == "read":
        return False
    return int(previous.get("fruitless") or 0) < MAX_FRUITLESS_VISITS


def _invoice_priority(previous: dict | None) -> int:
    """Never tried first, then changed ones, then those whose last read did
    not complete — the notes refresh's order, for the same reason."""
    if previous is None or (previous.get("status") == "pending"
                            and not previous.get("tried_at")):
        return 0
    if previous.get("status") == "read":
        return 1
    return 2


def _read_previous_invoices(state_dir: Path) -> dict[tuple[str, str], dict]:
    records: dict[tuple[str, str], dict] = {}
    try:
        for line in invoices_path(state_dir).read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if (isinstance(record, dict) and isinstance(record.get("path"), str)
                        and isinstance(record.get("name"), str)):
                    records[(record["path"], record["name"])] = record
    except (OSError, ValueError):
        records = {}
    return records


def _current_index(state_dir: Path) -> dict:
    try:
        index = json.loads(index_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return index if isinstance(index, dict) else {}


def _write_invoice_section(state_dir: Path, section: dict) -> dict:
    """index.json with this half's section replaced and the notes half's
    fields untouched."""
    index = _current_index(state_dir)
    index["invoices"] = section
    _write_index(state_dir, index)
    return section


def _invoices_failed(state_dir: Path, now: datetime, outcome: str, why: str | None,
                     **extra: Any) -> dict:
    previous = _current_index(state_dir).get("invoices") or {}
    section = {key: previous[key] for key in (
        "written_at", "invoices_sha256", "counts", "remaining", "extractor",
    ) if key in previous}
    section.update({"refreshed_at": _stamp(now), "outcome": outcome, "error": why, **extra})
    return _write_invoice_section(state_dir, section)


def invoices_deferred(state_dir: Path, now: datetime) -> dict:
    """A run that did not start because a chat was busy: recorded, the facts
    file untouched, and the next run comes after the backlog gap."""
    return _invoices_failed(state_dir, now, DEFERRED, None, deferred=True)


def invoice_counts(records: list[dict]) -> dict[str, Any]:
    def status(name: str) -> int:
        return sum(1 for r in records if r.get("status") == name)

    done = [r for r in records if r.get("status") in ("read", "partial")]
    reasons: dict[str, int] = {}
    for r in records:
        if r.get("status") in ("unread", "partial") and r.get("reason_class"):
            reasons[r["reason_class"]] = reasons.get(r["reason_class"], 0) + 1

    def facts(r: dict) -> dict:
        return r.get("facts") or {}

    return {
        "listed": len(records),
        "read": status("read"),
        "partial": status("partial"),
        "unread": status("unread"),
        "pending": status("pending"),
        "gave_up": sum(1 for r in records if r.get("gave_up")),
        "by_text_layer": sum(1 for r in done if r.get("read_as") == READ_TEXT),
        "by_ocr": sum(1 for r in done if r.get("read_as") == READ_OCR),
        "by_mixed": sum(1 for r in done if r.get("read_as") == READ_MIXED),
        "not_read_reasons": dict(sorted(reasons.items())),
        "with_account": sum(1 for r in done if facts(r).get("accounts")),
        "with_payable_amount": sum(
            1 for r in done if any(a.get("payable") for a in facts(r).get("amounts") or [])
        ),
        "fetched_elsewhere": sum(1 for r in records if r.get("fetched_elsewhere")),
        "shared_file": sum(1 for r in records if r.get("shared_file")),
    }


def refresh_invoices(
    run_tool: RunTool | None,
    state_dir: Path,
    now: datetime,
    *,
    max_reads: int | None = None,
    budget: timedelta | None = None,
    stop: threading.Event | None = None,
    busy: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    read: ReadDocument | None = None,
) -> dict:
    """Fetch and read the attachments of the indexed notes that are new,
    changed or unfinished, at most `max_reads` and for at most `budget` of
    wall clock, one at a time, stopping early on `stop` or when `busy` says a
    chat is working; write invoices.jsonl and index.json's `invoices`
    section. Returns that section."""
    state_dir = Path(state_dir)
    if not _tool_available(run_tool, INVOICE_TOOL):
        return _invoices_failed(state_dir, now, NO_TOOL,
                                f"{INVOICE_TOOL} is not installed as a read-only plugin")
    assert run_tool is not None  # _tool_available said so
    loaded = load(state_dir, now)
    if not loaded.available:
        return _invoices_failed(state_dir, now, NO_NOTES, loaded.reason)
    reader = read or read_document
    scratch = scratch_dir(state_dir)
    _empty_scratch(scratch)  # whatever a crashed run left behind
    previous = _read_previous_invoices(state_dir)

    # Listing order is the notes file's order: the vault listing's, newest
    # modified first.
    listed: list[tuple[str, str, dict | None]] = []
    seen: set[tuple[str, str]] = set()
    for note in loaded.notes:
        marker = note.raw.get("marker") if isinstance(note.raw.get("marker"), dict) else None
        for name in note.attachments:
            key = (note.path, name)
            if key not in seen:
                seen.add(key)
                listed.append((note.path, name, marker))
    wanted = [(p, n) for p, n, m in listed if _invoice_needs_read(previous.get((p, n)), m)]
    wanted.sort(key=lambda key: _invoice_priority(previous.get(key)))  # stable

    bound = MAX_INVOICES_PER_RUN if max_reads is None else max_reads
    allowed = (INVOICE_RUN_BUDGET if budget is None else budget).total_seconds()
    started = monotonic()
    markers = {(p, n): m for p, n, m in listed}
    results: dict[tuple[str, str], dict] = {}
    progressed = 0
    deferred = False
    for key in wanted:
        left = allowed - (monotonic() - started)
        if len(results) >= bound or left <= 0:
            break
        if stop is not None and stop.is_set():
            break
        if busy is not None and busy():
            deferred = True
            break
        path, name = key
        before = previous.get(key)
        seconds = max(ATTACHMENT_MIN_SECONDS, min(ATTACHMENT_SECONDS, left))
        try:
            outcome = _process_attachment(run_tool, reader, scratch, path, name, before, stop,
                                          seconds)
        except Exception as exc:  # noqa: BLE001 — one attachment never ends the run
            outcome = {"status": "unread", "reason_class": INVOICE_ERROR,
                       "reason": type(exc).__name__, "read_as": NOT_READ, "progressed": False}
        moved = outcome.pop("progressed", False)
        progressed += int(bool(moved))
        fresh = before is None or _changed(before, markers[key])
        fruitless = 0 if (moved or outcome.get("status") == "read") else (
            (0 if fresh else int((before or {}).get("fruitless") or 0)) + 1)
        kept = dict(before or {})
        for stale in ("unchanged", "gave_up", "stray_removed", "stray_copy_may_exist",
                      "stale_facts_dropped"):
            kept.pop(stale, None)
        if "sha256" not in outcome and any(k in kept for k in _BYTES_FACTS):
            # This visit could not establish the bytes are the ones the kept
            # facts came from (a failed fetch, a too-large copy, an error):
            # facts of possibly other bytes are not kept beside the new visit.
            for k in _BYTES_FACTS:
                kept.pop(k, None)
            outcome["stale_facts_dropped"] = True
        record = {
            **kept, **outcome, "path": path, "name": name, "note_marker": markers[key],
            "extractor": invoicefacts.EXTRACTOR_VERSION, "tried_at": _stamp(now),
            "attempts": int((before or {}).get("attempts") or 0) + 1,
            "fruitless": fruitless,
        }
        if moved:
            record["read_at"] = _stamp(now)
        if record["status"] != "read" and fruitless >= MAX_FRUITLESS_VISITS:
            record["gave_up"] = f"gave up after {fruitless} attempts without progress"
        results[key] = record
    _empty_scratch(scratch)

    records: list[dict] = []
    wanted_set = set(wanted)
    for path, name, _marker_now in listed:
        key = (path, name)
        if key in results:
            records.append(results[key])
        elif key not in wanted_set:
            records.append(previous[key])
        else:
            # Whatever an earlier read made of it is kept, but not counted as
            # read: it changed since, or was never finished.
            records.append({**(previous.get(key) or {}), "path": path, "name": name,
                            "status": "pending"})

    # The same vault file behind two notes' attachments: a matcher must not
    # count it as evidence for both bills.
    notes_by_file: dict[str, set[str]] = {}
    for r in records:
        if r.get("from_vault"):
            notes_by_file.setdefault(r["from_vault"], set()).add(r["path"])
    for r in records:
        if r.get("from_vault"):
            r["shared_file"] = len(notes_by_file[r["from_vault"]]) > 1

    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    _publish(invoices_path(state_dir), text)
    counts = invoice_counts(records)
    counts["read_this_run"] = len(results)
    counts["read_ok_this_run"] = progressed
    counts["removed"] = len(set(previous) - seen)
    section = {
        "refreshed_at": _stamp(now),
        "written_at": _stamp(now),
        "outcome": PARTIAL if counts["pending"] else OK,
        "error": None,
        "counts": counts,
        "remaining": counts["pending"],
        "deferred": deferred,
        "extractor": invoicefacts.EXTRACTOR_VERSION,
        "invoices_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    return _write_invoice_section(state_dir, section)


def refresh_invoices_recorded(
    run_tool: RunTool | None, state_dir: Path, now: datetime, **kwargs: Any
) -> dict:
    """`refresh_invoices`, with an unexpected exception recorded as outcome
    `error` (its TYPE only: a message could quote an invoice) — and the
    scratch directory emptied whatever happened."""
    try:
        return refresh_invoices(run_tool, state_dir, now, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the server outlives any refresh
        print(f"[paynotes] invoice refresh failed: {type(exc).__name__}", file=sys.stderr)
        try:
            return _invoices_failed(Path(state_dir), now, ERROR, type(exc).__name__)
        except Exception as again:  # noqa: BLE001
            print(f"[paynotes] could not record the failure: {type(again).__name__}",
                  file=sys.stderr)
            return {"outcome": ERROR, "error": type(exc).__name__}
    finally:
        _empty_scratch(scratch_dir(Path(state_dir)))


# --------------------------------------------------------------- the loop


def next_wait(index: dict) -> timedelta:
    """Sooner only while a backlog is being worked off: a partial run that
    made no successful read (iCloud offline, every read failing) waits the
    full day, or it would spend its whole budget on failures every 15 minutes.
    The notes and the invoices are two backlogs under the one rule: either
    one progressing brings the next run sooner."""
    for half in (index, index.get("invoices") or {}):
        progressed = (half.get("counts") or {}).get("read_ok_this_run", 0) > 0
        if half.get("outcome") == PARTIAL and progressed:
            return BACKLOG_EVERY
    if (index.get("invoices") or {}).get("deferred"):
        # A busy chat put the invoices off: back after the gap, not a day.
        return BACKLOG_EVERY
    return REFRESH_EVERY


class Refresher:
    """One per aish-web. Refreshes at start, then daily — sooner while reads
    remain. The work runs on its OWN single thread: it is a chain of wrapper
    subprocesses that can take minutes, and the default executor is for
    short calls a client needs (docs/web-server.md, L3). The invoice half runs
    after the notes half of the same run, on the same thread, so attachments
    are read strictly one at a time."""

    def __init__(
        self,
        state_dir: Path,
        *,
        make_runner: Callable[[], RunTool | None],
        clock: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self.state_dir = Path(state_dir)
        #: Is any chat working? The invoice half does not start while one is,
        #: and stops between attachments when one starts.
        self.busy = busy
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
        index = refresh_recorded(runner, self.state_dir, now, stop=self.stop)
        if invoices_enabled() and not self.stop.is_set():
            if self.busy():
                section = invoices_deferred(self.state_dir, now)
            else:
                section = refresh_invoices_recorded(runner, self.state_dir, now,
                                                    stop=self.stop, busy=self.busy)
            index = {**index, "invoices": section}
        return index

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
                if invoices := index.get("invoices"):
                    print(f"[paynotes] invoices {invoices.get('outcome')}: "
                          f"{(invoices.get('counts') or {}).get('read_this_run', 0)} read this "
                          f"run, {invoices.get('remaining', 0)} remaining", file=sys.stderr)
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


@dataclass
class InvoiceRecord:
    """One attachment's facts. Only `status` read or partial carries facts
    this run established; an unread record keeps an earlier read's facts, if
    any, the way an unread note keeps its earlier parse."""

    path: str  # the note's vault path
    name: str  # the attachment's name, as the note lists it
    status: str  # read | partial | unread | pending
    read_as: str | None = None  # text layer | ocr | mixed | not read
    reason_class: str | None = None
    accounts: list[dict] = field(default_factory=list)
    amounts: list[dict] = field(default_factory=list)
    dates: list[dict] = field(default_factory=list)
    invoice_numbers: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_record(cls, record: dict) -> InvoiceRecord:
        facts = record.get("facts") or {}
        return cls(
            path=str(record.get("path")),
            name=str(record.get("name")),
            status=str(record.get("status")),
            read_as=record.get("read_as"),
            reason_class=record.get("reason_class"),
            accounts=list(facts.get("accounts") or []),
            amounts=list(facts.get("amounts") or []),
            dates=list(facts.get("dates") or []),
            invoice_numbers=list(facts.get("invoice_numbers") or []),
            raw=record,
        )


@dataclass
class LoadedInvoices:
    """What `load_invoices` found. `available` False means NO facts file —
    never zero invoices."""

    available: bool
    reason: str = ""
    invoices: list[InvoiceRecord] = field(default_factory=list)
    section: dict = field(default_factory=dict)
    age: timedelta | None = None


def load_invoices(state_dir: Path, now: datetime | None = None) -> LoadedInvoices:
    state_dir = Path(state_dir)
    try:
        index = json.loads(index_path(state_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LoadedInvoices(False, "no index: aish-web has not written one yet")
    except (OSError, ValueError) as exc:
        return LoadedInvoices(False, f"index unreadable: {type(exc).__name__}")
    section = index.get("invoices") if isinstance(index, dict) else None
    if not isinstance(section, dict):
        return LoadedInvoices(False, "no invoice facts: none collected yet")
    digest = section.get("invoices_sha256")
    if not digest:
        last = section.get("outcome") or "unknown"
        return LoadedInvoices(
            False, f"no invoice facts: never collected successfully (last attempt: {last})",
            section=section)
    try:
        data = invoices_path(state_dir).read_bytes()
    except OSError as exc:
        return LoadedInvoices(False, f"invoices file unreadable: {type(exc).__name__}",
                              section=section)
    if hashlib.sha256(data).hexdigest() != digest:
        return LoadedInvoices(False, "invoices file does not match the index", section=section)
    invoices = [InvoiceRecord.from_record(json.loads(line))
                for line in data.decode("utf-8").splitlines() if line.strip()]
    age = None
    try:
        age = (now or datetime.now()) - datetime.fromisoformat(str(section.get("written_at")))
    except ValueError:
        pass
    return LoadedInvoices(True, "", invoices, section, age)


def _invoice_summary(state_dir: Path, now: datetime | None) -> list[str]:
    """Counts and outcome words only: the facts file holds payee accounts."""
    loaded = load_invoices(state_dir, now)
    lines = ["invoice facts (from the notes' attachments)"]
    section = loaded.section
    if section.get("refreshed_at"):
        lines.append(f"last collection attempt: {section['refreshed_at']} — outcome: "
                     f"{section.get('outcome')}")
    if not loaded.available:
        lines.append(loaded.reason)
        return lines
    counts = section.get("counts") or {}
    for key, label in (("listed", "attachments listed"), ("by_text_layer", "read by text layer"),
                       ("by_ocr", "read by OCR"), ("by_mixed", "read by text layer and OCR"),
                       ("partial", "partly read"), ("unread", "not read"),
                       ("with_account", "with a valid account"),
                       ("with_payable_amount", "with a payable amount"),
                       ("gave_up", "given up on"),
                       ("fetched_elsewhere", "found outside the note's own folders"),
                       ("shared_file", "one file behind several notes"),
                       ("read_this_run", "read this run")):
        if key in counts:
            lines.append(f"  {label}: {counts[key]}")
    for reason, number in (counts.get("not_read_reasons") or {}).items():
        lines.append(f"    not (fully) read — {reason}: {number}")
    lines.append(f"  remaining: {section.get('remaining', 0)}")
    return lines


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
        return "\n".join(lines + _invoice_summary(state_dir, now))
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
    return "\n".join(lines + _invoice_summary(state_dir, now))
