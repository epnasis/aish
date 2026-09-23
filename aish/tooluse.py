"""How each tool call came out, and how big a tool menu each model call held.

A pure scan over the session logs: no model call, no live state, nothing read
from the tool table as it is TODAY. Same shape as `vocab.scan` and
`usage.scan` — a `scan_*` pass over recorded evidence, which
`docs/trace-contract.md` §7 sanctions.

Two things, both as RECORDED:

- **Outcomes per tool** — calls, and how many the log marks failed, refused by
  a gate, or aimed at a tool that did not exist. Each is a field the writer
  set; none is re-derived here.
- **Menu size per model call** — the bytes of the menu the call was handed,
  looked up by the digest its `brief` recorded. A menu whose bytes were purged,
  or a call with no digest at all, is counted as such and never as 0.

What it CANNOT say: whether the tool a model chose was the right one for the
task. No record captures that, and a failure count is not a proxy for it — a
call can succeed at the wrong thing.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from . import evidence

# `Agent._dispatch` writes it, this module counts it off the recorded step's
# error field — imported, never respelled, so the two cannot drift
# (test_the_writer_imports_the_prefix_rather_than_respelling_it).
from .tools import UNKNOWN_TOOL_PREFIX

RECORDED, PURGED, MISSING = "recorded", "purged", "missing"


@dataclass
class ToolCounters:
    """One tool's recorded outcomes.

    `refused` is a subset of `failed` in every log written since the agent
    derives `ok` from a refusing decision. An older log could record a refused
    call as `ok: true`; it is counted as recorded — refused, and not failed.
    """

    tool: str
    calls: int = 0
    failed: int = 0
    refused: int = 0
    unknown: int = 0

    @property
    def ok(self) -> int:
        return self.calls - self.failed


@dataclass
class MenuDay:
    """One day of model calls and the menus they were handed.

    The median and max are over RECORDED menus only, and None when there were
    none — a purged or missing menu is its own column and never a 0 in them.
    """

    day: str
    calls: int = 0
    recorded: int = 0
    purged: int = 0
    missing: int = 0
    median_chars: float | None = None
    max_chars: int | None = None
    #: Tool count as the brief recorded it. Survives a purge, because it lives
    #: in the log and not in the evidence store.
    median_tools: float | None = None


@dataclass
class Report:
    tools: dict[str, ToolCounters] = field(default_factory=dict)
    menu_days: list[MenuDay] = field(default_factory=list)


@dataclass
class _Handed:
    """What one model call was handed: the state of its menu's bytes."""

    day: str
    state: str
    chars: int = 0
    tools: int | None = None


def scan(root=None, days: int | None = None) -> Report:
    """Every chat log in the window, summed per tool and per day.

    One unreadable file must not take the report down with it (the containment
    rule a single corrupt session log taught the web server)."""
    from .explain import _records, state_dir
    from .rules import REFUSED_DECISIONS
    from .usage import _within

    directory = Path(root) if root is not None else state_dir()
    report = Report()
    if not directory.is_dir():
        return report
    sizes: dict[str, int | None] = {}  # digest -> chars; one menu serves hundreds of calls
    handed: list[_Handed] = []
    for path in sorted(directory.glob("session-*.jsonl")):
        if days is not None and not _within(path.stem, days):
            continue
        try:
            records = _records(path)
        except OSError:
            continue
        _scan_records(
            records, report.tools, handed, sizes, directory, _stem_day(path.stem),
            REFUSED_DECISIONS,
        )
    report.menu_days = _by_day(handed)
    return report


def _scan_records(
    records: list[dict],
    tools: dict[str, ToolCounters],
    handed: list[_Handed],
    sizes: dict[str, int | None],
    directory: Path,
    file_day: str,
    refused_decisions: frozenset[str],
) -> None:
    # A brief is written only when the menu or the system prompt CHANGES
    # (`Agent._record_brief`), so the menu a model call held is the latest
    # brief before it in the same log. Counting briefs would count changes.
    brief: dict | None = None
    for record in records:
        step = record.get("step")
        if not isinstance(step, dict):
            continue
        kind = step.get("kind")
        if kind == "tool":
            _count_tool(tools, step, refused_decisions)
        elif kind == "brief":
            brief = step
        elif kind == "reasoning":
            day = str(record.get("ts") or "")[:10] or file_day
            handed.append(_menu_of(brief, day, sizes, directory))


def _count_tool(
    tools: dict[str, ToolCounters], step: dict, refused_decisions: frozenset[str]
) -> None:
    name = str(step.get("name") or "(unnamed)")
    counters = tools.setdefault(name, ToolCounters(tool=name))
    counters.calls += 1
    if step.get("ok") is False:
        counters.failed += 1
    if step.get("decision") in refused_decisions:
        counters.refused += 1
    if str(step.get("error") or "").startswith(UNKNOWN_TOOL_PREFIX):
        counters.unknown += 1


def _menu_of(
    brief: dict | None, day: str, sizes: dict[str, int | None], directory: Path
) -> _Handed:
    menu = (brief or {}).get("tools") or {}
    count = menu.get("count")
    tool_count = count if isinstance(count, int) else None
    digest = str(menu.get("digest") or "")
    if not digest:
        return _Handed(day=day, state=MISSING, tools=tool_count)
    if digest not in sizes:
        blob = evidence.get(digest, directory)
        sizes[digest] = None if blob is None else len(blob)
    size = sizes[digest]
    if size is None:
        return _Handed(day=day, state=PURGED, tools=tool_count)
    return _Handed(day=day, state=RECORDED, chars=size, tools=tool_count)


def _stem_day(stem: str) -> str:
    """`session-YYYYMMDD-...` → `YYYY-MM-DD`, for a record with no timestamp."""
    parts = stem.split("-")
    if len(parts) >= 2 and len(parts[1]) == 8 and parts[1].isdigit():
        return f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:]}"
    return "unknown"


def _by_day(handed: list[_Handed]) -> list[MenuDay]:
    grouped: dict[str, list[_Handed]] = {}
    for one in handed:
        grouped.setdefault(one.day, []).append(one)
    out = []
    for day, calls in sorted(grouped.items()):
        chars = [c.chars for c in calls if c.state == RECORDED]
        counts = [c.tools for c in calls if c.tools is not None]
        out.append(
            MenuDay(
                day=day,
                calls=len(calls),
                recorded=len(chars),
                purged=sum(c.state == PURGED for c in calls),
                missing=sum(c.state == MISSING for c in calls),
                median_chars=statistics.median(chars) if chars else None,
                max_chars=max(chars) if chars else None,
                median_tools=statistics.median(counts) if counts else None,
            )
        )
    return out


# ----------------------------------------------------------------- the report

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"

_CANNOT_KNOW = (
    "Outcomes and sizes are as RECORDED. Whether a chosen tool was the right one"
    " for the task is in no record, and a failure count is not a proxy for it."
)

# The ToolCounters caveat, said where a JSON consumer can see it too: `ok` is
# calls minus failed AS RECORDED, and a log written before refusals were forced
# red can record a refused call as ok: true.
_AS_RECORDED = (
    "ok = calls - failed, as recorded; in logs written before refusals were"
    " forced red, a refused call can be recorded ok: true."
)


def _number(value: float | int | None) -> str:
    return "—" if value is None else f"{value:,.0f}"


def render(report: Report, days: int | None) -> str:
    window = "all recorded chats" if days is None else f"the last {days} days"
    lines = [f"{BOLD}tool use{RESET}  {DIM}{window}{RESET}", ""]
    if not report.tools:
        lines.append("  no chat in this window recorded a tool call.")
    else:
        lines.append(f"  {'':<28} {'calls':>7} {'failed':>7} {'refused':>8} {'unknown':>8}")
        ranked = sorted(report.tools.values(), key=lambda c: (-c.calls, c.tool))
        for c in ranked:
            lines.append(
                f"  {c.tool:<28} {c.calls:>7} {c.failed:>7} {c.refused:>8} {c.unknown:>8}"
            )
        from .rules import REFUSED_DECISIONS

        decisions = "/".join(sorted(REFUSED_DECISIONS))
        lines.append(
            f"{DIM}  refused = a gate's recorded decision ({decisions});"
            f" unknown = a call to a tool that did not exist.{RESET}"
        )
    lines += ["", f"  {BOLD}tool menu per model call{RESET}"]
    if not report.menu_days:
        lines.append("  no chat in this window recorded a model call.")
    else:
        lines.append(
            f"  {'':<12} {'calls':>7} {'recorded':>9} {'purged':>7} {'missing':>8}"
            f" {'median chars':>13} {'max chars':>10} {'tools':>6}"
        )
        for d in report.menu_days:
            lines.append(
                f"  {d.day:<12} {d.calls:>7} {d.recorded:>9} {d.purged:>7} {d.missing:>8}"
                f" {_number(d.median_chars):>13} {_number(d.max_chars):>10}"
                f" {_number(d.median_tools):>6}"
            )
        lines.append(
            f"{DIM}  median/max are over recorded menus only; purged and missing are never"
            f" counted as 0.{RESET}"
        )
    lines += ["", f"{DIM}  {_CANNOT_KNOW}{RESET}"]
    return "\n".join(lines)


def json_report(report: Report, days: int | None) -> dict:
    return {
        "days": days,
        "tools": {
            name: {
                "calls": c.calls,
                "ok": c.ok,
                "failed": c.failed,
                "refused": c.refused,
                "unknown": c.unknown,
            }
            for name, c in sorted(report.tools.items())
        },
        # A median or max that could not be taken is ABSENT, never 0.
        "menu_days": [
            {
                "day": d.day,
                "calls": d.calls,
                "recorded": d.recorded,
                "purged": d.purged,
                "missing": d.missing,
                **({"median_chars": d.median_chars} if d.median_chars is not None else {}),
                **({"max_chars": d.max_chars} if d.max_chars is not None else {}),
                **({"median_tools": d.median_tools} if d.median_tools is not None else {}),
            }
            for d in report.menu_days
        ],
        "cannot_know": _CANNOT_KNOW,
        "as_recorded": _AS_RECORDED,
    }
