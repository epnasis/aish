"""The owner's automations from the terminal: `/tasks` and `/runs` (#5 S4).

Two halves, and the fence between them is the point of the file.

READING is the listing and the runs: the task files, the fired ledger, the
scheduler's state file and the session logs, all read and never written. Safe
while aish-web is running because every one of them is published whole through
`atomic_write` (`schedule.JsonStore.write`, `SessionLog`'s appends are lines),
so a read sees the old file or the new one, never half of either.

WRITING is a toggle and a shredder, never an author. `set_enabled` rewrites
one existing file with exactly one field changed — the content is the file's
own validated task with `enabled` replaced, re-rendered — and `delete` removes
one existing file. Neither can create a file or set any other field, so the
approval card (`schedule_task`) stays the only author of what an automation
DOES. The owner typing the slash command is the consent, as for `/cd`; the
model's fence around the folder (`_command_touches_automations`, the file
tools) is untouched by this, and was always scoped to model calls.

Every path resolves at call time through the same seams the suite isolates:
`schedule.automations_dir()` (config home) and the `state_dir` the caller
passes (the state tree), never a path bound at import.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import atomic_write, schedule
from . import scheduler as scheduling
from .seen import SeenLedger
from .seen import ledger_path as seen_ledger_path
from .session import SessionLog, silent_output

#: How many runs `/runs` shows, newest first.
RUNS_SHOWN = 20

#: How far back the startup notice looks for runs he has not opened. The scan
#: parses each candidate chat's log, so it is bounded by the chats' start time
#: (read from the file name, no I/O) — and the notice says so.
NOTICE_DAYS = 14


class AdminError(Exception):
    """A `/tasks` change that did not happen; the message says why and what
    to do instead."""


# -- reading: the automations ------------------------------------------------


@dataclass
class Listing:
    tasks: list[schedule.Task] = field(default_factory=list)
    problems: list[schedule.LoadProblem] = field(default_factory=list)
    fired: dict[str, Any] = field(default_factory=dict)
    settled: dict[str, Any] = field(default_factory=dict)
    #: Set when a store exists and cannot be read: "aish does not know" is
    #: never presented as "nothing fired".
    ledger_error: str = ""
    state_error: str = ""


def read_listing(
    state_dir: Path, *, deny_prefixes: Collection[str], now: datetime
) -> Listing:
    loaded = schedule.load_tasks(
        schedule.automations_dir(), deny_prefixes=deny_prefixes, as_of=now
    )
    return _with_stores(Listing(tasks=loaded.tasks, problems=loaded.problems), state_dir)


def _with_stores(listing: Listing, state_dir: Path) -> Listing:
    try:
        listing.fired = schedule.fired_ledger(state_dir).read()
    except schedule.LedgerUnreadable as exc:
        listing.ledger_error = str(exc)
    try:
        settled = schedule.scheduler_state(state_dir).read().get("settled")
        listing.settled = settled if isinstance(settled, dict) else {}
    except schedule.LedgerUnreadable as exc:
        listing.state_error = str(exc)
    return listing


def _clock(when: datetime) -> str:
    return when.strftime("%H:%M %a %Y-%m-%d")


def _one_shot_state(task: schedule.Task, listing: Listing, now: datetime) -> str:
    """Where a one-shot stands, from the ledger and the state's `settled` — a
    fired one-shot with no such line would silently look alive."""
    entry = listing.fired.get(task.key)
    if isinstance(entry, dict):
        session = entry.get("session") or "?"
        return f"fired at {str(entry.get('fired_at', '?')).replace('T', ' ')}, in {session}"
    if listing.settled.get(task.key) == schedule.EXPIRED:
        return "expired: it did not run, and it will not"
    if listing.ledger_error:
        return f"unknown — aish cannot read its record of fired runs ({listing.ledger_error})"
    status = schedule.next_due(task, set(listing.fired), now)
    if status == schedule.DISABLED:
        return "disabled — it will not run while disabled"
    if status == schedule.PENDING:
        return f"pending — runs at {_clock(task.one_shot_at())}"
    if status == schedule.DUE:
        return "due — aish-web starts it within 30 s while it is running"
    return ("past its time by more than "
            f"{int(schedule.STALE_AFTER.total_seconds() // 3600)} h and not yet "
            "recorded — aish-web records it as expired on its next tick")


def _final_may_still_run(task: schedule.Task, listing: Listing, now: datetime) -> bool:
    """Past `until`, the one arm of `schedule.due` that is not ENDED whatever
    the scheduler's `since` says: enabled, inside the grace, the final
    occurrence unfired. Whether it IS due there depends on `since`, which
    only aish-web holds — so the listing says "may", never "will". A final
    occurrence before `from` (S6) is no final run — `final_occurrence` returns
    None for it, exactly as `due()` never fires it."""
    assert task.until is not None
    final = schedule.final_occurrence(task)
    return (
        final is not None and task.enabled
        and now - task.until <= schedule.STALE_AFTER
        and task.key_for(final) not in listing.fired
    )


def _ran(task: schedule.Task, listing: Listing) -> str:
    """When a cron task last ran, from the fired ledger — "" when it never has."""
    if listing.ledger_error:
        return "whether it ran is unknown (the record of fired runs cannot be read)"
    last = schedule.last_fired(task.id, listing.fired)
    return f"last ran for {_clock(last)}" if last is not None else ""


def _ran_words(task: schedule.Task, listing: Listing) -> str:
    ran = _ran(task, listing)
    return f" · {ran}" if ran else ""


def _state(task: schedule.Task, listing: Listing, now: datetime) -> str:
    ended = (task.past_until(now) and not listing.ledger_error
             and not _final_may_still_run(task, listing, now))
    # ENDED outranks DISABLED: a disabled task past its end will not run
    # again whatever is done to the toggle.
    return "ENDED" if ended else "enabled" if task.enabled else "DISABLED"


def _stands(task: schedule.Task, listing: Listing, now: datetime, state: str) -> str:
    """Where a task stands, the line under its header (without when it last
    ran, which `_ran` says)."""
    if task.recurring and task.past_until(now):
        assert task.until is not None
        final = schedule.final_occurrence(task)
        if state == "ENDED":
            return f"ended {_clock(task.until)}"
        if listing.ledger_error:
            return f"past its until ({_clock(task.until)})"
        assert final is not None
        grace = _clock(task.until + schedule.STALE_AFTER)
        return (f"past its until ({_clock(task.until)}); its final run, for "
                f"{_clock(final)}, has not fired — aish-web may still start it, "
                f"late, until {grace}")
    if task.recurring:
        coming = schedule.upcoming(task, now, 1)  # clipped to [from, until]
        if coming and task.before_start(now):
            # Not yet started: a bare "next <date>" would read as live.
            return (f"starts {_clock(coming[0])}" if task.enabled
                    else f"would start {_clock(coming[0])} if enabled")
        if coming:
            return (f"next {_clock(coming[0])}" if task.enabled
                    else f"would next run at {_clock(coming[0])} if enabled")
        if task.until is not None:
            return f"no run is left before its until ({_clock(task.until)})"
        return "no next occurrence within the horizon"
    return _one_shot_state(task, listing, now)


def _scheduler_state_words(listing: Listing) -> str:
    return (f"aish cannot read its scheduler state: {listing.state_error}"
            if listing.state_error else "")


def task_lines(listing: Listing, now: datetime) -> list[str]:
    """The `/tasks` listing as plain lines: one header per task, then where it
    stands. Problem files come last, AS problems, with their error."""
    lines: list[str] = []
    for task in listing.tasks:
        state = _state(task, listing, now)
        note = f" — {task.note}" if task.note else ""
        lines.append(
            f"{task.id}{note}\n    {task.kind} · runs {schedule.when_words(task, now)} · "
            f"{state} · notify {task.notify}"
        )
        ran = _ran_words(task, listing) if task.recurring else ""
        lines.append(f"    {_stands(task, listing, now, state)}{ran}")
    for problem in listing.problems:
        lines.append(
            f"PROBLEM {Path(problem.path).name} — not a runnable automation, so it "
            f"will not run:\n    {problem.error}"
        )
    if listing.state_error:
        lines.append(f"({_scheduler_state_words(listing)})")
    return lines


def task_rows(listing: Listing, now: datetime) -> dict[str, Any]:
    """The same listing as `task_lines`, as STRUCTURED rows for the web's
    Automations sheet — built from the same helpers, so the two surfaces
    cannot describe one task differently, and the client derives nothing.
    `allow` and `sites` are on the row because resuming re-arms unattended
    runs that carry them."""
    rows = []
    for task in listing.tasks:
        state = _state(task, listing, now)
        rows.append({
            "id": task.id,
            "note": task.note,
            "kind": task.kind,
            "when": f"runs {schedule.when_words(task, now)}",
            "state": state,
            "stands": _stands(task, listing, now, state),
            "ran": _ran(task, listing) if task.recurring else "",
            "notify": task.notify,
            "allow": list(task.allow),
            "sites": list(task.sites),
        })
    # A problem file is named for delete by its stem, as `/tasks delete` is.
    problems = [
        {"file": Path(problem.path).name, "id": Path(problem.path).stem,
         "error": problem.error}
        for problem in listing.problems
    ]
    notes = [_scheduler_state_words(listing)] if listing.state_error else []
    return {"rows": rows, "problems": problems, "notes": notes}


# -- writing: the toggle and the shredder ------------------------------------


def automation_file(task_id: str) -> Path:
    """The ONE existing `<id>.json` the owner named, or AdminError. Only a file
    the folder already holds can be named, which is what makes both writers
    unable to create one."""
    directory = schedule.automations_dir()
    if not task_id or "/" in task_id or task_id.startswith("."):
        raise AdminError(f"{task_id!r} is not an automation id")
    path = directory / f"{task_id}.json"
    if path not in set(directory.glob("*.json")) or not path.is_file():
        raise AdminError(f"there is no automation {task_id!r} (/tasks lists them)")
    return path


def set_enabled(
    task_id: str, enabled: bool, *, deny_prefixes: Collection[str], now: datetime
) -> tuple[schedule.Task, bool]:
    """Turn one automation on or off: (the task, whether the file was written).

    The only content this can write is the file's OWN validated task with
    `enabled` replaced — no parameter carries any other field. A file that
    does not validate is refused, because a field of a file that does not
    parse cannot be changed without guessing at the rest of it."""
    path = automation_file(task_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        task = schedule.validate_task(raw, deny_prefixes=deny_prefixes, as_of=now)
        if task.id != path.stem:
            raise schedule.InvalidTask(
                f"id {task.id!r} does not match the file name {path.name!r}"
            )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise AdminError(
            f"{path.name} is not a runnable automation ({exc}), so aish will not "
            f"rewrite one field of it; fix it in an editor, or /tasks delete {task_id}"
        ) from None
    refusal = toggle_refusal(task, enabled, now)
    if refusal:
        raise AdminError(refusal)
    if task.enabled == enabled:
        return task, False
    updated = dataclasses.replace(task, enabled=enabled)
    atomic_write.publish(path, updated.render(), encoding="utf-8")
    return updated, True


def toggle_refusal(task: schedule.Task, enabled: bool, now: datetime) -> str:
    """Why turning `task` on or off would do nothing, or "" when it may. One
    answer for every surface that toggles: `/tasks`, and `schedule_task`'s
    `enabled` (#459)."""
    if task.until is None or not task.past_until(now):
        return ""
    # Enabling past the end is provably useless: the scheduler re-seeds
    # `since` at the enable, after the final occurrence, so nothing is ever
    # due again. Disabling stays possible through the grace, where it still
    # stops a late final run; past the grace there is nothing to stop.
    if enabled or now - task.until > schedule.STALE_AFTER:
        return (
            f"automation {task.id} has ended: its until ({_clock(task.until)}) has passed — "
            f"ask aish for a new automation, which comes on its own approval card "
            f"(or /tasks delete {task.id})"
        )
    return ""


def enabled_words(task: schedule.Task, state_dir: Path, now: datetime) -> str:
    """What turning `task` on means now, as observed from the stores — said
    after `/tasks enable`, because a one-shot whose time has passed does not
    simply resume."""
    if task.recurring:
        coming = schedule.upcoming(task, now, 1)  # clipped to [from, until]
        final = schedule.final_occurrence(task)
        if coming:
            ends = (f"; it ends {_clock(task.until)}, after its last run, for {_clock(final)}"
                    if task.until is not None and final is not None else "")
            if task.before_start(now):
                return (f"it starts {_clock(coming[0])}, the first scheduled time in its "
                        f"window{ends}")
            return f"its next scheduled time is {_clock(coming[0])}{ends}"
        if task.until is not None:
            return (f"no scheduled time is left before its until ({_clock(task.until)}), "
                    "so it will not run again")
        return "it has no next occurrence within the horizon"
    return _one_shot_state(task, _with_stores(Listing(), state_dir), now)


def delete(task_id: str) -> Path:
    """Remove one automation file — a problem file too. Its snapshot and its
    scheduler state go when aish-web next reloads the folder."""
    path = automation_file(task_id)
    path.unlink()
    return path


# -- reading: the runs -------------------------------------------------------


@dataclass(frozen=True)
class Run:
    at: datetime
    task: str
    what: str
    session: str = ""
    title: str = ""
    #: A chat whose output postdates the owner's look in the shared seen
    #: ledger. Session-less records are never unseen: each already pushed.
    unseen: bool = False
    silent: bool = False


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _monitor_words(record: dict) -> str:
    status = str(record.get("status", "?"))
    words = f"monitor: {status}"
    if record.get("manual"):
        words += " (run by hand)"
    if status == schedule.CHANGED and record.get("summary"):
        words += f" — {record['summary']}"
    if status == schedule.UNREADABLE and record.get("reason"):
        words += f" ({record['reason']})"
    identical = record.get("snapshot_identical")
    if identical is True:
        words += " · snapshot identical to the baseline"
    elif identical is False:
        words += " · snapshot differs from the baseline"
    if record.get("verdict_matches_bytes") is False:
        words += " · the verdict and the bytes disagree"
    if record.get("snapshot_not_written"):
        words += f" · baseline not written: {record['snapshot_not_written']}"
    return words


def _record_words(record: dict) -> str:
    kind = record.get("kind")
    if kind == "monitor":
        return _monitor_words(record)
    if kind == "monitor_run_failed":
        return f"monitor run did not finish: {record.get('error', '')}"
    if kind == "interrupted":
        return "interrupted: aish-web stopped while it was running"
    if kind == scheduling.MANUAL_RUN:
        return "run by hand"
    if kind == "ledger_write_failed":
        return f"ran, but its fired record could not be written: {record.get('error', '')}"
    if kind == "skipped":
        capped = "at least " if record.get("count_capped") else ""
        return (f"skipped: {capped}{record.get('count', '?')} occurrence(s) from "
                f"{str(record.get('from', '?')).replace('T', ' ')} through "
                f"{str(record.get('through', '?')).replace('T', ' ')} were passed over "
                f"when {str(record.get('latest', '?')).replace('T', ' ')} came due")
    if kind == "expired":
        return f"did not run: {record.get('error', '')}"
    if kind == "ended":
        return (f"ended as scheduled: its until "
                f"({str(record.get('until', '?')).replace('T', ' ')}) had passed")
    if kind == "final_not_started":
        return (f"final run, for {str(record.get('scheduled_for', '?')).replace('T', ' ')}, "
                f"did not start: {record.get('error', '')}")
    if kind == "open_failed":
        return f"did not start: {record.get('error', '')}"
    return str(kind)


def _first_prompt(path: Path) -> str:
    try:
        messages = SessionLog._cached_parse(path).messages
    except OSError:
        return ""
    first = next((m.get("content") for m in messages if m.get("role") == "user"), "")
    return first if isinstance(first, str) else ""


def _session_paths(state_dir: Path, since: datetime | None = None) -> list[Path]:
    """Session logs newest-started first (the name carries the start time)."""
    paths = sorted(state_dir.glob("session-*.jsonl"), key=lambda p: p.name, reverse=True)
    if since is None:
        return paths
    return [p for p in paths if SessionLog._started_at(p) >= since]


def _unseen(info: Any, seen: SeenLedger) -> bool:
    return bool(info.output) and info.output > max(seen.stamp(info.path.name), seen.floor())


def _silent(info: Any) -> bool:
    return silent_output(info.output, info.quiet_through)


def recent_runs(state_dir: Path, *, limit: int = RUNS_SHOWN) -> tuple[list[Run], list[str]]:
    """The newest `limit` scheduled runs, newest first, and any store aish
    could not read. A run is a schedule-origin CHAT (annotated with what the
    scheduler recorded about it), or a record with no chat — a run that never
    started, expired, or a range of skipped occurrences."""
    problems: list[str] = []
    try:
        records = schedule.scheduler_state(state_dir).read().get("records") or []
    except schedule.LedgerUnreadable as exc:
        records, problems = [], [f"aish cannot read its scheduler state: {exc}"]
    try:
        ledger = schedule.fired_ledger(state_dir).read()
    except schedule.LedgerUnreadable as exc:
        ledger = {}
        problems.append(f"aish cannot read its record of fired runs: {exc}")
    seen = SeenLedger(seen_ledger_path(state_dir))

    by_session: dict[str, list[dict]] = {}
    sessionless: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("session")
        if name:
            by_session.setdefault(str(name), []).append(record)
        elif record.get("kind") in (
            "skipped", "expired", "open_failed", "ended", "final_not_started"
        ):
            sessionless.append(record)
    fired_for = {
        str(entry.get("session")): (key, entry) for key, entry in ledger.items()
        if isinstance(entry, dict) and entry.get("session")
    }

    runs: list[Run] = []
    for path in _session_paths(state_dir):
        if len(runs) >= limit:
            break
        info = SessionLog.info(path)
        if info is None or info.origin != "schedule":
            continue
        name = path.name
        notes = by_session.get(name, [])
        task_id = next((str(r.get("task")) for r in notes if r.get("task")), "") \
            or scheduling.automation_of(_first_prompt(path))
        words = [_record_words(r) for r in notes if r.get("kind") != scheduling.MANUAL_RUN]
        if name in fired_for:
            occurrence = str(fired_for[name][1].get("scheduled_for", "")).replace("T", " ")
            words.insert(0, f"ran for {occurrence}" if occurrence else "ran")
        elif any(r.get("kind") == scheduling.MANUAL_RUN for r in notes):
            words.insert(0, "run by hand")
        runs.append(Run(
            at=SessionLog._started_at(path), task=task_id or "?",
            what=" · ".join(words) or "ran", session=name, title=info.title,
            unseen=_unseen(info, seen), silent=_silent(info),
        ))
    for record in sessionless:
        when = _stamp(record.get("observed_at"))
        if when is not None:
            runs.append(Run(at=when, task=str(record.get("task", "?")),
                            what=_record_words(record)))
    runs.sort(key=lambda run: run.at, reverse=True)
    return runs[:limit], problems


def run_lines(runs: list[Run], problems: list[str]) -> list[str]:
    lines: list[str] = []
    for run in runs:
        mark = ""
        if run.session and run.unseen:
            mark = "  [not opened, silent]" if run.silent else "  [not opened]"
        where = f"\n    chat {run.session}" + (f" — {run.title}" if run.title else "") \
            if run.session else ""
        lines.append(f"{run.at.strftime('%Y-%m-%d %H:%M')}  {run.task}  {run.what}{mark}{where}")
    lines.extend(f"({problem})" for problem in problems)
    return lines


def unopened_count(state_dir: Path, now: datetime) -> int:
    """Schedule-origin chats from the last NOTICE_DAYS whose output postdates
    the owner's look in the shared seen ledger, silent monitor runs excepted.
    A log that has not changed since that look cannot be unseen, so it is
    never parsed."""
    seen = SeenLedger(seen_ledger_path(state_dir))
    count = 0
    for path in _session_paths(state_dir, since=now - timedelta(days=NOTICE_DAYS)):
        try:
            if path.stat().st_mtime <= max(seen.stamp(path.name), seen.floor()):
                continue
        except OSError:
            continue
        info = SessionLog.info(path)
        if info is None or info.origin != "schedule":
            continue
        if _unseen(info, seen) and not _silent(info):
            count += 1
    return count


def notice(state_dir: Path, now: datetime) -> str:
    """The startup line, or "" when there is nothing to say. Worded as what
    it counts: chats he has not opened — there is no separate look stamp."""
    count = unopened_count(state_dir, now)
    if not count:
        return ""
    runs = "scheduled run" if count == 1 else "scheduled runs"
    return (f"{count} {runs} from the last {NOTICE_DAYS} days you haven't opened "
            "(silent monitor runs not counted) — /runs")
