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
from dataclasses import dataclass
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
    "keeper_clamped", "owner_wrote_days", "wanted", "deadline", "suspicion", "sender_case", "case",
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


MISSING = "(missing)"


def verdict_word(record: dict[str, Any], axis: str) -> str:
    """A verdict axis as the backtest report counts it: `gated` for a gated
    mail, `no_verdict` for any other reading with no verdict, `(missing)` for
    a verdict that lacks the axis (only corrupt or hand-written bytes can),
    else the word. Never empty."""
    if str(record.get("ending") or "") == mailreader.GATED:
        return "gated"
    verdict = record.get("verdict")
    if not isinstance(verdict, dict):
        return "no_verdict"
    if axis not in verdict:
        return MISSING
    return str(verdict.get(axis) or "") or MISSING


def _text_field(record: dict[str, Any], name: str) -> str:
    """`kind` and `meaning` may lawfully be empty (`may_be_empty`): an empty
    one reads `(empty)`, an absent one `(missing)`."""
    verdict = record.get("verdict")
    if isinstance(verdict, dict) and name in verdict and not verdict.get(name):
        return "(empty)"
    return verdict_word(record, name)


def _owner_wrote(record: dict[str, Any]) -> str:
    if "owner_wrote_days" not in record:
        return "not recorded"
    days = record.get("owner_wrote_days")
    return "never" if days is None else str(days)


def _gated_by(record: dict[str, Any]) -> str:
    """The list header(s) that gated the mail, as the report's
    `gated_by_truth` keys them. A gated ending whose gate facts say nothing
    reads `?`, never "not gated" beside `ending: gated`."""
    gate = record.get("gate")
    newsletter = gate.get("newsletter") if isinstance(gate, dict) else None
    newsletter = newsletter if isinstance(newsletter, dict) else {}
    gated = newsletter.get("gated") or record.get("ending") == mailreader.GATED
    if not gated:
        return "not gated"
    return "+".join(str(b) for b in newsletter.get("by") or ()) or "?"


def _signals(keeper: dict[str, Any]) -> str:
    signals = keeper.get("signals")
    return "+".join(str(s) for s in signals) if isinstance(signals, list) and signals else "?"


def _keeper_cell(record: dict[str, Any], keeper: dict[str, Any] | None,
                 closure: Any) -> str:
    if not keeper:
        return "no"
    released = "; released: you closed its case" if mailverdicts.keeper_released(
        record, closure=closure) else ""
    return f"yes (from {keeper.get('from')}: {_signals(keeper)}{released})"


def row_of(
    row: mailview.Row, clean: Clean, truth: TruthOf | None = None
) -> list[str]:
    record, header = row.record, row.header
    raw = verdict_word(record, "disposition")
    effective = mailverdicts.effective_disposition(record, closure=row.closure) or raw
    clamped = mailverdicts.clamp_of(record)
    keeper = mailverdicts.keeper_clamp_of(record)
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
        (_keeper_cell(record, keeper, row.closure), CELL_CHARS),
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


BOM = "﻿"


def write(
    path: Path, state_dir: os.PathLike | str, *, clean: Clean, truth: TruthOf | None = None
) -> tuple[Path, int, int]:
    """Write the review file to exactly `path`. UTF-8 with a BOM, so a
    spreadsheet reads the Polish and the accents as what they are."""
    text, rows, unreadable = render(state_dir, clean=clean, truth=truth)
    atomic_write.publish(path, BOM + text, encoding="utf-8")
    return path, rows, unreadable


# ---------------------------------------------------------------- where it lands

#: Replaces where the Downloads folder is. The tests point it at a temp dir:
#: ~/Downloads is a macOS privacy-protected folder, and a process outside the
#: owner's own terminal that opens one has hung on this machine.
DOWNLOADS_ENV = "AISH_DOWNLOADS_DIR"
LIVE_RUN = "live"
_MAX_SAME_SECOND = 1000


def downloads_dir() -> Path:
    override = os.environ.get(DOWNLOADS_ENV, "").strip()
    return Path(override).expanduser() if override else Path.home() / "Downloads"


def interactive() -> bool:
    """Whether this process was started from a terminal: stdin is a tty. A
    launchd job, a pipe from another program, an agent's shell — none is, and
    none of them is where the review file is meant to be generated."""
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def file_name(run: str, now: float) -> str:
    """`aish-verdicts-<run>-<export stamp>.csv`: which run, and when to the
    second. Two exports in one second are told apart by `_claim`'s counter."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"aish-verdicts-{run}-{stamp}.csv"


def _claim(directory: Path, name: str) -> Path:
    """A path in `directory` that no other export holds — `name`, else
    `name-2`, `-3`, … — claimed with O_EXCL, so a second export in the same
    second takes the next name instead of replacing the first file."""
    stem = name.removesuffix(".csv")
    for n in range(1, _MAX_SAME_SECOND + 1):
        candidate = directory / (name if n == 1 else f"{stem}-{n}.csv")
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise FileExistsError(f"{_MAX_SAME_SECOND} exports named {stem}-* already in {directory}")


def _publish_new(directory: Path, name: str, payload: str) -> Path:
    path = _claim(directory, name)
    try:
        atomic_write.publish(path, payload, encoding="utf-8")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _why(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


@dataclass(frozen=True)
class Written:
    path: Path
    rows: int
    unreadable: int
    #: "" when the file is where it was asked to go (an explicit path, or the
    #: Downloads folder); otherwise what was observed and where it went.
    note: str = ""


def export(
    state_dir: os.PathLike | str,
    *,
    clean: Clean,
    run: str,
    fallback_dir: Path,
    out_file: Path | None = None,
    truth: TruthOf | None = None,
    now: float | None = None,
) -> Written:
    """Write the review file where the owner will look for it.

    `out_file` wins: a file path is written as named, a directory gets the
    generated name inside it. Otherwise the Downloads folder — tried ONLY
    when this process runs in a terminal, so a background process never
    opens it at all. Whatever cannot be written there lands in
    `fallback_dir` under the generated name, and `note` says what was
    observed. The folder never fails the export; only the fallback can."""
    text, rows, unreadable = render(state_dir, clean=clean, truth=truth)
    payload = BOM + text
    name = file_name(run, time.time() if now is None else now)
    note = ""
    if out_file is not None:
        target = Path(out_file).expanduser()
        try:
            if target.is_dir():
                return Written(_publish_new(target, name, payload), rows, unreadable)
            atomic_write.publish(target, payload, encoding="utf-8")
            return Written(target, rows, unreadable)
        except OSError as exc:
            note = f"could not write {target} ({_why(exc)})"
    elif not interactive():
        note = "not run from a terminal, so the Downloads folder was not tried"
    else:
        downloads = downloads_dir()
        if not downloads.is_dir():
            note = f"{downloads} does not exist"
        elif not os.access(downloads, os.W_OK):
            note = f"{downloads} is not writable"
        else:
            try:
                return Written(_publish_new(downloads, name, payload), rows, unreadable)
            except OSError as exc:
                note = f"writing into {downloads} failed ({_why(exc)})"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    path = _publish_new(fallback_dir, name, payload)
    return Written(path, rows, unreadable, f"{note} — written to {fallback_dir} instead")


def live_fallback_dir(state_dir: os.PathLike | str) -> Path:
    """Where `/mail export` writes when Downloads is not used: the live state
    tree's own exports folder (where every export went before 2026-10-26)."""
    return mailindex.mail_dir(state_dir) / EXPORTS_DIRNAME
