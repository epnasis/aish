"""The review file — every judged mail as one CSV row per (mail, model) (#142 W5 S1.6).

The owner asked for a file he can sort and filter in a spreadsheet. It is the
SAME join `/mail` shows (`mailview.collect`, verdict store × index by digest —
one authority, never a second join), over a backtest run's scratch tree or the
live store alike, with every row the store holds: a gated, invalid, error or
unadmitted reading keeps its ending visible, and no cell is ever left blank to
stand for "unknown" — it says what is missing.

**Every cell takes one path**, in this order: the caller's sanitiser (REQUIRED,
`mailview`'s law — the terminal's `cli._plain`), whitespace flattened, the
spreadsheet-formula escape (`formula_safe`), then the cap. The escape runs on
every cell, code-written ones included, so there is no list of "safe" columns
to drift. Written with `csv.QUOTE_ALL`.

**Truth is not stored, so it is recomputed** when a backtest exports: the
caller passes `truth`, and the header names the columns
`*_recomputed_at_export` so the file says so itself.

`docs/mail-assistant.md` §The review file.
"""

from __future__ import annotations

import csv
import io
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import atomic_write, mailindex, mailreader, mailverdicts, mailview

Clean = Callable[[str], str]
#: digest -> (truth label, tier), or None when the run's index lacks the mail.
TruthOf = Callable[[str], "tuple[str, str] | None"]

EXPORTS_DIRNAME = "exports"
CELL_CHARS = 300
MEANING_CHARS = 300
GATE_CHARS = 600

# Leading characters a spreadsheet reads as the start of a formula (=, +, -,
# @) or that some strip before doing so (tab, CR).
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")

COLUMNS = (
    "date", "from_addr", "display_name", "subject", "mailbox", "model", "ending",
    "kind", "meaning", "disposition_raw", "disposition_effective", "clamped",
    "owner_wrote_days", "wanted", "deadline", "suspicion", "sender_case", "case",
    "newsletter_gated_by", "gate", "charter_version", "digest", "provider_id",
)
TRUTH_COLUMNS = ("truth_recomputed_at_export", "tier_recomputed_at_export")

NOT_IN_INDEX = "(not in the index)"


def formula_safe(text: str) -> str:
    """Text a spreadsheet shows as text: a leading formula character — also
    one behind leading whitespace — is preceded by an apostrophe."""
    if text.startswith(_FORMULA_LEAD) or text.lstrip().startswith(_FORMULA_LEAD):
        return "'" + text
    return text


def cell(clean: Clean, value: Any, cap: int = CELL_CHARS) -> str:
    """One cell: sanitiser → flattened whitespace → formula escape → cap."""
    text = " ".join(clean(str(value if value is not None else "")).split())
    text = formula_safe(text)
    return text if len(text) <= cap else text[: cap - 1] + "…"


def verdict_word(record: dict[str, Any], axis: str) -> str:
    """A verdict axis as the backtest report counts it: `gated` for a gated
    mail, `no_verdict` for any other reading with no verdict, else the word."""
    if str(record.get("ending") or "") == mailreader.GATED:
        return "gated"
    verdict = record.get("verdict")
    if not isinstance(verdict, dict):
        return "no_verdict"
    return str(verdict.get(axis) or "")


def _text_field(record: dict[str, Any], name: str) -> str:
    word = verdict_word(record, name)
    if word in ("gated", "no_verdict"):
        return word
    return word or "(empty)"


def _owner_wrote(record: dict[str, Any]) -> str:
    if "owner_wrote_days" not in record:
        return "not recorded"
    days = record.get("owner_wrote_days")
    return "never" if days is None else str(days)


def _gated_by(record: dict[str, Any]) -> str:
    newsletter = ((record.get("gate") or {}).get("newsletter") or {})
    if not isinstance(newsletter, dict) or not newsletter.get("gated"):
        return "not gated"
    return "+".join(str(b) for b in newsletter.get("by") or ()) or "?"


def row_of(
    row: mailview.Row, clean: Clean, truth: TruthOf | None = None
) -> list[str]:
    record, header = row.record, row.header
    raw = verdict_word(record, "disposition")
    effective = mailverdicts.effective_disposition(record) or raw
    clamped = mailverdicts.clamp_of(record)
    case = str(record.get("case_choice") or "") or verdict_word(record, "case")
    gate = " · ".join(mailview.gate_words(record, clean)) or "(no gate facts recorded)"
    values: list[tuple[Any, int]] = [
        (time.strftime("%Y-%m-%d %H:%M", time.localtime(row.sort_at)) if row.sort_at
         else "no date recorded", CELL_CHARS),
        (header.from_addr or "(no address)" if header else NOT_IN_INDEX, CELL_CHARS),
        ((mailindex.display_name(header.from_value) or "(no name)") if header else NOT_IN_INDEX,
         mailview.NAME_CHARS),
        ((header.subject or "(no subject)") if header else NOT_IN_INDEX, CELL_CHARS),
        ((header.mailbox or "?") if header else NOT_IN_INDEX, CELL_CHARS),
        (record.get("model") or "(no model)", CELL_CHARS),
        (record.get("ending") or "?", CELL_CHARS),
        (_text_field(record, "kind"), mailview.KIND_CHARS),
        (_text_field(record, "meaning"), MEANING_CHARS),
        (raw, CELL_CHARS),
        (effective, CELL_CHARS),
        (f"yes (from {clamped.get('from')})" if clamped else "no", CELL_CHARS),
        (_owner_wrote(record), CELL_CHARS),
        (verdict_word(record, "wanted"), CELL_CHARS),
        (verdict_word(record, "deadline"), CELL_CHARS),
        (verdict_word(record, "suspicion"), CELL_CHARS),
        (verdict_word(record, "sender_case"), CELL_CHARS),
        (case, CELL_CHARS),
        (_gated_by(record), CELL_CHARS),
        (gate, GATE_CHARS),
        (record.get("version") or "?", CELL_CHARS),
        (record.get("mail") or "?", CELL_CHARS),
        (record.get("provider_id") or "(none)", CELL_CHARS),
    ]
    if truth is not None:
        found = truth(str(record.get("mail") or ""))
        values += [(found[0], CELL_CHARS), (found[1], CELL_CHARS)] if found else [
            (NOT_IN_INDEX, CELL_CHARS), (NOT_IN_INDEX, CELL_CHARS)]
    return [cell(clean, value, cap) for value, cap in values]


def render(
    state_dir: os.PathLike | str, *, clean: Clean, truth: TruthOf | None = None
) -> tuple[str, int, int]:
    """(csv text, rows, unreadable verdict files). Newest first, as `/mail`."""
    view = mailview.collect(state_dir, limit=sys.maxsize)
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(COLUMNS + (TRUTH_COLUMNS if truth is not None else ()))
    for row in view.rows:
        writer.writerow(row_of(row, clean, truth))
    return out.getvalue(), len(view.rows), view.unreadable


def write(
    path: Path, state_dir: os.PathLike | str, *, clean: Clean, truth: TruthOf | None = None
) -> tuple[Path, int, int]:
    """Write the review file. UTF-8 with a BOM, so a spreadsheet reads the
    Polish and the accents as what they are."""
    text, rows, unreadable = render(state_dir, clean=clean, truth=truth)
    atomic_write.publish(path, "﻿" + text, encoding="utf-8")
    return path, rows, unreadable


def live_path(state_dir: os.PathLike | str, now: float | None = None) -> Path:
    """Where `/mail export` writes: the live state tree's own exports folder."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() if now is None else now))
    return mailindex.mail_dir(state_dir) / EXPORTS_DIRNAME / f"verdicts-{stamp}.csv"
