"""`/mail` — what the shadow inbox pipeline recorded, for the owner (#142 W5 S1.5).

The pipeline writes verdicts, an index and a health file; until this surface
the owner could see none of it. `/mail` lists the recent shadow results
newest first — the mail (date, sender, subject), the Reader's reading (kind
and meaning), what it WOULD have done (disposition, what the mail wants of
him) and the Gate's facts — under the watcher's health line, so a watcher that
has stopped is visible where he looks (the email-poll outage lesson, #362).

**The join is by digest.** The verdict store holds no subject and no sender
(`mailverdicts.build_record` keeps digest, provider id, gate and verdict); the
mail's own fields come from the index file for that digest
(`mailindex.load_header`). A verdict whose index file is missing or
unreadable is shown as such, never dropped.

**Rendering is the caller's sanitiser, applied to every string.** Almost
everything shown here was written by a stranger (subject, display name,
attachment names, flag details) or by a model steered by one (meaning, kind,
and the closed words, which are validated at write time and NOT trusted at
read time). `lines` takes `clean` with no default, and every string from a
record or an index file passes through it with its cap — the terminal passes
`cli._plain`, which also removes C1, every ESC form, bidi controls and the
Unicode line separators. Nothing here strips anything itself, so there is one
sanitiser and no second opinion about it.

Reads only. `docs/mail-assistant.md` §The owner surface.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from . import mailindex, mailverdicts, mailwatch

#: How many results `/mail` shows by default, and the most `/mail <n>` shows.
MAIL_SHOWN = 20
MAIL_LIMIT_MAX = 200

# Render-time caps. Subjects are stored uncapped; `[:120]` is the backtest
# report's precedent. Each cap is applied AFTER the sanitiser so a marker
# cannot be cut in half.
SUBJECT_CHARS = 120
ADDRESS_CHARS = 120
NAME_CHARS = 60
MEANING_CHARS = 300
KIND_CHARS = 40
WORD_CHARS = 24
WHY_CHARS = 200
FLAG_TEXT_CHARS = 80
ATTACHMENT_CHARS = 60
ATTACHMENTS_SHOWN = 5
FLAGS_SHOWN = 6

# The watcher is scheduled every 15 minutes (`scripts/install-mailwatch-service.sh`);
# three intervals with no pass is shown as a warning. It says what was
# observed — no pass recorded since — never why.
STALE_AFTER_S = 45 * 60

Clean = Callable[[str], str]


@dataclass
class Row:
    """One recorded reading of one mail by one model, joined to its index entry."""

    record: dict[str, Any]
    header: mailindex.Header | None
    sort_at: float = 0.0


@dataclass
class View:
    rows: list[Row] = field(default_factory=list)
    total: int = 0
    unreadable: int = 0


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def collect(state_dir: os.PathLike | str, limit: int = MAIL_SHOWN) -> View:
    """The newest `limit` readings, by the mail's own time, each joined to its
    index entry by digest. One unreadable verdict file costs that file and is
    counted."""
    view = View()
    rows: list[Row] = []
    for path in sorted(mailverdicts.store_dir(state_dir).glob("*/*.json")):
        try:
            raw = json.loads(path.read_text())
            by_model = raw.get("by_model") if isinstance(raw, dict) else None
            if not isinstance(by_model, dict):
                raise ValueError("no by_model")
        except (OSError, ValueError, AttributeError):
            view.unreadable += 1
            continue
        for record in by_model.values():
            if isinstance(record, dict):
                rows.append(Row(record, None, _float(record.get("at"))))
    view.total = len(rows)
    rows.sort(key=lambda r: (r.sort_at, _float(r.record.get("recorded_at"))), reverse=True)
    view.rows = rows[: max(0, limit)]
    for row in view.rows:
        row.header = mailindex.load_header(state_dir, str(row.record.get("mail") or ""))
    return view


def read_health(state_dir: os.PathLike | str) -> dict[str, Any] | None:
    try:
        raw = json.loads(mailwatch.health_path(state_dir).read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _when(ts: Any) -> str:
    value = _float(ts)
    if not value:
        return "never"
    return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _age(ts: Any, now: float) -> str:
    seconds = max(0.0, now - _float(ts))
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} days ago"


def health_lines(state: dict[str, Any] | None, now: float, clean: Clean) -> list[tuple[str, str]]:
    """(severity, text) pairs: "warn" for anything the owner should notice —
    no pass recorded, none recently, a failing listing on any inbox."""
    if state is None:
        return [("warn", "watcher: no pass recorded (no watch_health.json) — "
                         "aish-mailwatch has not run against this state tree")]
    out: list[tuple[str, str]] = []
    last_pass = state.get("last_pass_at")
    stale = now - _float(last_pass) > STALE_AFTER_S
    summary = _mapping(state.get("last_pass"))
    counts = (f"{int(_float(summary.get('listed')))} listed, "
              f"{int(_float(summary.get('assessed')))} assessed")
    line = f"watcher: last pass {_when(last_pass)} ({_age(last_pass, now)}); {counts}"
    if stale:
        line += " — no pass recorded since"
    out.append(("warn" if stale else "ok", line))
    sources = _mapping(state.get("sources"))
    if not sources and int(_float(state.get("failed_listings"))):
        # A health file from before per-inbox health: one streak for the pass.
        sources = {"mailbox": {"failed_listings": state.get("failed_listings"),
                               "last_ok_at": state.get("last_ok_at")}}
    for name, source in sorted(sources.items()):
        if not isinstance(source, dict):
            continue
        streak = int(_float(source.get("failed_listings")))
        label = clean(str(name))[:WORD_CHARS]
        ran = source.get("last_run_at")
        if ran is not None and _float(ran) != _float(last_pass):
            # Not read on the last pass: its streak and counts are history,
            # and are not shown as current.
            out.append(("warn", f"watcher: {label} was not read on the last pass; last read "
                                f"{_when(ran)}"))
            continue
        if streak:
            failure = clean(str(source.get("last_failure") or ""))[:WHY_CHARS]
            out.append(("warn", (
                f"watcher: {label} listing FAILED on the last {streak} pass(es); last good "
                f"listing {_when(source.get('last_ok_at'))}"
                + (f" — {failure}" if failure else "")
            )))
        else:
            out.append(("ok", f"watcher: {label} listing ok"))
        note = clean(str(source.get("note") or ""))[:WHY_CHARS]
        if note:
            out.append(("warn", f"watcher: {label}: {note}"))
        unparseable = int(_float(source.get("unparseable")))
        if unparseable:
            out.append(("warn", f"watcher: {label}: {unparseable} mail(s) on the last pass could "
                                "not be parsed and were not assessed"))
        pending = int(_float(source.get("unfetched")))
        if pending:
            out.append(("warn" if streak else "ok",
                        f"watcher: {label}: {pending} listed mail(s) not yet fetched — "
                        "the next pass tries them again"))
    return out


def _capped(clean: Clean, value: Any, cap: int) -> str:
    text = " ".join(clean(str(value if value is not None else "")).split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def gate_words(record: dict[str, Any], clean: Clean) -> list[str]:
    gate = _mapping(record.get("gate"))
    words: list[str] = []
    auth = _mapping(gate.get("auth"))
    if auth:
        if auth.get("authenticated") is True:
            words.append("authenticated")
        else:
            why = auth.get("why") or auth.get("verdict") or "no result"
            words.append(f"NOT authenticated ({_capped(clean, why, WHY_CHARS)})")
    grade = _mapping(gate.get("grade"))
    if grade.get("grade"):
        words.append(f"grade {_capped(clean, grade['grade'], WORD_CHARS)}")
    newsletter = _mapping(gate.get("newsletter"))
    if newsletter.get("gated"):
        by = "+".join(_capped(clean, b, WORD_CHARS) for b in newsletter.get("by") or ())
        words.append(f"newsletter-gated ({by or '?'})")
    flags = [f for f in gate.get("flags") or () if isinstance(f, dict)]
    if flags:
        shown = ", ".join(
            f"{_capped(clean, f.get('what'), WORD_CHARS)}"
            f"({_capped(clean, f.get('detail'), FLAG_TEXT_CHARS)})"
            for f in flags[:FLAGS_SHOWN]
        )
        more = f" +{len(flags) - FLAGS_SHOWN} more" if len(flags) > FLAGS_SHOWN else ""
        words.append(f"flags: {shown}{more}")
    case_flags = _mapping(record.get("case_flags"))
    raised = [_capped(clean, name, WORD_CHARS) for name, cases in sorted(case_flags.items())
              if cases]
    if raised:
        words.append("case flags: " + ", ".join(raised))
    return words


def row_lines(row: Row, clean: Clean) -> list[tuple[str, str]]:
    """The lines for one reading. EVERY string from the record or the index
    goes through `clean` — including the closed words, which were validated
    when written and are not trusted now."""
    record, header = row.record, row.header
    when = _when(row.sort_at)
    model = _capped(clean, record.get("model") or "(no model)", WORD_CHARS * 2)
    if header is not None:
        sender = _capped(clean, header.from_addr or "(no address)", ADDRESS_CHARS)
        name = _capped(clean, mailindex.display_name(header.from_value), NAME_CHARS)
        subject = _capped(clean, header.subject or "(no subject)", SUBJECT_CHARS)
        box = _capped(clean, getattr(header, "mailbox", "") or "", WORD_CHARS)
        head = f"{when}  {sender}" + (f" ({name})" if name else "") + f" — {subject}"
        if box:
            head += f"  [{box}]"
    else:
        pid = _capped(clean, record.get("provider_id") or "?", ADDRESS_CHARS)
        head = f"{when}  (not in the index: provider id {pid})"
    out = [("head", head)]
    verdict = record.get("verdict")
    verdict = verdict if isinstance(verdict, dict) else None
    ending = _capped(clean, record.get("ending") or "?", WORD_CHARS)
    if verdict is not None:
        kind = _capped(clean, verdict.get("kind") or "?", KIND_CHARS)
        meaning = _capped(clean, verdict.get("meaning") or "", MEANING_CHARS)
        out.append(("body", f"  reading: {kind} — {meaning}"))
        would = f"  would: {_capped(clean, mailverdicts.effective_disposition(record), WORD_CHARS)}"
        clamped = mailverdicts.clamp_of(record)
        if clamped:
            source = _capped(clean, clamped.get("from"), WORD_CHARS)
            days = _capped(clean, clamped.get("owner_wrote_days"), WORD_CHARS)
            limit = _capped(clean, clamped.get("limit"), WORD_CHARS)
            would += (f" [CLAMPED from {source}: you wrote in this conversation {days} days "
                      f"before it, within {limit}]")
        would += f"; wants of you: {_capped(clean, verdict.get('wanted'), WORD_CHARS)}"
        deadline = _capped(clean, verdict.get("deadline") or "", WORD_CHARS)
        if deadline and deadline != "none":
            would += f" by {deadline}"
        for axis, quiet in (("suspicion", "none"), ("sender_case", "consistent")):
            value = _capped(clean, verdict.get(axis) or "", WORD_CHARS)
            if value and value != quiet:
                would += f"; {axis.replace('_', ' ')}: {value}"
        out.append(("body", would + f"  ({model})"))
    else:
        why = _capped(clean, record.get("why") or "", WHY_CHARS)
        out.append(("body", f"  ending: {ending}" + (f" — {why}" if why else "") + f"  ({model})"))
    gate = gate_words(record, clean)
    if gate:
        out.append(("gate", "  gate: " + " · ".join(gate)))
    attachments = _mapping(record.get("attachments"))
    names = [n for n in attachments.get("names") or () if isinstance(n, str)]
    if names:
        shown = ", ".join(_capped(clean, n, ATTACHMENT_CHARS) for n in names[:ATTACHMENTS_SHOWN])
        count = int(_float(attachments.get("count"))) or len(names)
        more = f" (+{count - ATTACHMENTS_SHOWN} more)" if count > ATTACHMENTS_SHOWN else ""
        out.append(("body", f"  attachments (names only, not read): {shown}{more}"))
    return out


def lines(
    state_dir: os.PathLike | str,
    *,
    clean: Clean,
    limit: int = MAIL_SHOWN,
    now: float | None = None,
) -> list[tuple[str, str]]:
    """Everything `/mail` prints, as (style, text). `clean` is REQUIRED: there
    is no unsanitised way to render this."""
    now = time.time() if now is None else now
    out = health_lines(read_health(state_dir), now, clean)
    view = collect(state_dir, limit)
    if view.unreadable:
        out.append(("warn", f"{view.unreadable} verdict file(s) could not be read"))
    if not view.rows:
        out.append(("body", "no shadow results recorded yet"))
        return out
    out.append(("body", f"newest {len(view.rows)} of {view.total} recorded reading(s) — "
                        "shadow mode: nothing was done to any of these mails"))
    for row in view.rows:
        out.extend(row_lines(row, clean))
    return out


def parse_limit(args: Iterable[str]) -> int | None:
    """`/mail` → MAIL_SHOWN, `/mail 50` → 50 (capped at MAIL_LIMIT_MAX); anything else None."""
    words = [a for a in args if a]
    if not words:
        return MAIL_SHOWN
    if len(words) == 1 and words[0].isdigit() and int(words[0]) > 0:
        return min(int(words[0]), MAIL_LIMIT_MAX)
    return None
