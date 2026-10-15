"""Automations the owner authored: the store and the time logic (epic #142 W2, S1).

An automation is one inert JSON file in `<config home>/automations/`, named
`<id>.json`. Nothing here runs anything and nothing here imports the server:
`scheduler.py` owns the runtime, and `agent.py`'s `schedule_task` is the one
writer the model has, through the diff-approval card. Code never writes the
owner-authored directory — the only writer of what has FIRED is the ledger in
the state tree, which is why an automation has no `fired` field to flip.

Two schedule shapes, exactly one per task: `{"at": "<ISO local datetime>"}`, a
one-shot (S1), and `{"cron": "<5-field expr>"}`, recurring (S2). Anything else
is refused at load and at creation, never guessed at.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import atomic_write, files, paths
from .approval import EXEC_WRAPPERS, check_denied

AUTOMATIONS_DIRNAME = "automations"

#: How late a one-shot may still fire. Past this the run is given up and
#: reported — a "check after 10pm" that fires two days later answers a
#: question nobody is asking any more.
STALE_AFTER = timedelta(hours=24)

TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$"
)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

FIELDS = (
    "id", "note", "prompt", "schedule", "enabled", "created",
    "model", "allow", "sites", "cwd", "roots",
)

# Statuses `next_due` answers with.
PENDING = "pending"
DUE = "due"
EXPIRED = "expired"
FIRED = "fired"
DISABLED = "disabled"


def automations_dir() -> Path:
    """Resolved at CALL time through the one config-home knob, so a test's
    isolated config home (and `AISH_CONFIG_HOME`) moves it with everything
    else (#390's lesson: an import-bound path is a path no fixture reaches)."""
    return paths.config_home() / AUTOMATIONS_DIRNAME


class InvalidTask(ValueError):
    """A task file or a `schedule_task` call that does not describe exactly one
    runnable automation. The message is shown to the model or logged as-is."""


# -- cron ---------------------------------------------------------------------

#: How far `cron_next`/`cron_prev` look before giving up. A syntactically valid
#: expr can be impossible (`0 0 30 2 *`), and an unbounded scan for it would
#: hang the tick. Four years covers every leap day except across 2100.
CRON_HORIZON = timedelta(days=4 * 366)

_CRON_FIELDS = (  # name, lowest, highest
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)
_CRON_ITEM = re.compile(r"^(?:\*|(\d+)(?:-(\d+))?)(?:/(\d+))?$")


class NoOccurrence(InvalidTask):
    """A cron expr with no occurrence inside `CRON_HORIZON` of the asked time."""


@dataclass(frozen=True)
class Cron:
    """One parsed 5-field expr, vixie semantics, naive local wall-clock.

    `dom_star`/`dow_star` are vixie's flags: the field's text begins with `*`.
    When NEITHER is starred a day matches if EITHER field matches (the OR
    rule); otherwise both must — which for a plain `*` is no restriction."""

    expr: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]  # 0..6, Sunday = 0 (7 is folded into 0)
    dom_star: bool
    dow_star: bool

    def day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.doms
        dow_ok = (day.isoweekday() % 7) in self.dows
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok


def _cron_field(text: str, name: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for item in text.split(","):
        match = _CRON_ITEM.match(item)
        if not match:
            raise InvalidTask(
                f"cron {name} field {text!r}: {item!r} is not supported — use numbers, "
                "'*', '*/n', ranges 'a-b' and comma lists (no names like MON, no "
                "@daily, no L, W, # or ?)"
            )
        start_s, end_s, step_s = match.groups()
        if step_s and start_s is not None and end_s is None:
            raise InvalidTask(
                f"cron {name} field {text!r}: {item!r} is not supported — a step goes "
                "after '*' or a range, e.g. '*/15' or '0-30/10'"
            )
        step = int(step_s) if step_s else 1
        if step < 1:
            raise InvalidTask(f"cron {name} field {text!r}: a step must be at least 1")
        if start_s is None:
            start, end = low, high
        else:
            start = int(start_s)
            end = int(end_s) if end_s is not None else start
        if not (low <= start <= high and low <= end <= high):
            raise InvalidTask(f"cron {name} field {text!r}: values must be {low}-{high}")
        if start > end:
            raise InvalidTask(f"cron {name} field {text!r}: range {start}-{end} runs backwards")
        values.update(range(start, end + 1, step))
    return values


@functools.lru_cache(maxsize=256)
def parse_cron(expr: str) -> Cron:
    """`expr` as a `Cron`, or InvalidTask. Says nothing about whether the expr
    can ever occur — `cron_next` answers that, within its horizon."""
    if not isinstance(expr, str) or _CONTROL_CHARS.search(expr.replace("\t", " ")):
        raise InvalidTask(f"schedule.cron must be a 5-field cron expression, got {expr!r}")
    parts = expr.split()
    if len(parts) != 5:
        raise InvalidTask(
            f"schedule.cron {expr!r} must have exactly 5 fields: minute hour "
            "day-of-month month day-of-week, e.g. '0 9 * * 1-5' (weekdays at 09:00)"
        )
    sets = [_cron_field(p, n, lo, hi) for p, (n, lo, hi) in zip(parts, _CRON_FIELDS, strict=True)]
    minutes, hours, doms, months, dows = sets
    return Cron(
        expr=" ".join(parts),
        minutes=tuple(sorted(minutes)),
        hours=tuple(sorted(hours)),
        doms=frozenset(doms),
        months=frozenset(months),
        dows=frozenset(d % 7 for d in dows),
        dom_star=parts[2].startswith("*"),
        dow_star=parts[4].startswith("*"),
    )


def _minute(when: datetime) -> datetime:
    return when.replace(second=0, microsecond=0)


def cron_next(expr: str, after: datetime) -> datetime:
    """The first occurrence strictly after `after`. Raises NoOccurrence when
    there is none within `CRON_HORIZON`, so an impossible expr costs one
    bounded scan and never a hung tick."""
    cron = parse_cron(expr)
    start = _minute(after) + timedelta(minutes=1)
    day = start.replace(hour=0, minute=0)
    limit = after + CRON_HORIZON
    while day <= limit:
        if cron.day_matches(day):
            for hour in cron.hours:
                for minute in cron.minutes:
                    candidate = day.replace(hour=hour, minute=minute)
                    if candidate >= start:
                        return candidate
        day += timedelta(days=1)
    raise NoOccurrence(
        f"schedule.cron {cron.expr!r} has no occurrence within "
        f"{CRON_HORIZON.days} days of {after.isoformat(timespec='minutes')}"
    )


def cron_prev(expr: str, at_or_before: datetime) -> datetime:
    """The latest occurrence at or before `at_or_before` — the backward twin of
    `cron_next`, under the same horizon bound."""
    cron = parse_cron(expr)
    end = _minute(at_or_before)
    day = end.replace(hour=0, minute=0)
    limit = at_or_before - CRON_HORIZON
    while day >= limit.replace(hour=0, minute=0, second=0, microsecond=0):
        if cron.day_matches(day):
            for hour in reversed(cron.hours):
                for minute in reversed(cron.minutes):
                    candidate = day.replace(hour=hour, minute=minute)
                    if candidate <= end:
                        return candidate
        day -= timedelta(days=1)
    raise NoOccurrence(
        f"schedule.cron {cron.expr!r} has no occurrence within "
        f"{CRON_HORIZON.days} days before {at_or_before.isoformat(timespec='minutes')}"
    )


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    at: datetime  # local wall-clock time, naive
    note: str = ""
    enabled: bool = True
    created: str = ""
    model: str = ""
    allow: tuple[str, ...] = ()
    sites: tuple[str, ...] = ()
    cwd: str = ""
    roots: tuple[str, ...] = field(default=())

    @property
    def schedule(self) -> dict[str, str]:
        return {"at": self.at.isoformat()}

    @property
    def key(self) -> str:
        """What the fired ledger is keyed by. It carries the time, so an owner
        who hand-edits `at` has re-armed the task — the edited task is a new
        run, and nothing has to clear an old flag for it to fire."""
        return f"{self.id}:{self.at.isoformat()}"

    @property
    def deadline(self) -> datetime:
        return self.at + STALE_AFTER

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "note": self.note,
            "prompt": self.prompt,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "created": self.created,
        }
        for name in ("model", "cwd"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        for name in ("allow", "sites", "roots"):
            if getattr(self, name):
                out[name] = list(getattr(self, name))
        return out

    def render(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"


def parse_at(value: object) -> datetime:
    """An ISO datetime as LOCAL wall-clock time. One with an offset is
    converted to local time (unambiguous); a bare date is refused, because
    "2026-10-15" says when, not what time."""
    if not isinstance(value, str) or "T" not in value and " " not in value.strip():
        raise InvalidTask(
            f"schedule.at must be an ISO local date and time like "
            f"'2026-10-15T22:05', got {value!r}"
        )
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        raise InvalidTask(
            f"schedule.at {value!r} is not an ISO date and time like '2026-10-15T22:05'"
        ) from None
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return when


def _strings(raw: dict, name: str) -> list[str]:
    value = raw.get(name, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise InvalidTask(f"{name} must be a list of strings")
    return [v.strip() for v in value]


def _validate_prefix(prefix: str, deny_prefixes: Collection[str]) -> str:
    if not prefix:
        raise InvalidTask("allow holds an empty command prefix — it would match nothing")
    if _CONTROL_CHARS.search(prefix):
        raise InvalidTask(f"allow prefix {prefix!r} contains a control character")
    reason = check_denied(prefix, list(deny_prefixes))
    if reason:
        raise InvalidTask(f"allow prefix {prefix!r} is on the denylist ({reason})")
    words = prefix.split()
    if len(words) < 2 and words[0].rsplit("/", 1)[-1] in EXEC_WRAPPERS:
        # The shell gate never lets a bare interpreter prefix approve anything
        # (approval._prefix_approves), so this grant would show on the card and
        # do nothing — the card must not offer what the gate will not honour.
        raise InvalidTask(
            f"allow prefix {prefix!r} is a bare interpreter; aish never "
            "auto-approves one — name the subcommand, e.g. 'python3 scripts/x.py'"
        )
    return prefix


def _validate_site(site: str) -> str:
    host = site.lower()
    host = host[4:] if host.startswith("www.") else host
    if not _HOST_RE.match(host):
        raise InvalidTask(
            f"sites entry {site!r} is not a bare host name like 'eon.pl' "
            "(no scheme, path or port)"
        )
    return host


def _validate_dir(value: str, name: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InvalidTask(f"{name} {value!r} must be an absolute path")
    store = automations_dir()
    if files.contains(path, store) or files.contains(store, path):
        raise InvalidTask(
            f"{name} {value!r} contains or is inside the automations folder; a run "
            "must never be scoped over the files that start runs"
        )
    return str(path)


def validate_task(
    raw: object,
    *,
    deny_prefixes: Collection[str] = (),
    now: datetime | None = None,
) -> Task:
    """One task from its JSON object, or InvalidTask. Fails CLOSED: an unknown
    key is refused rather than ignored, because a key the owner wrote that aish
    silently drops is a constraint he believes is in force.

    `now` is passed only at CREATION, where a time already in the past is an
    error; a task file loaded later is legitimately in the past."""
    if not isinstance(raw, dict):
        raise InvalidTask("an automation must be a JSON object")
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        raise InvalidTask(f"unknown field(s) {', '.join(unknown)}; known: {', '.join(FIELDS)}")
    task_id = raw.get("id")
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise InvalidTask(
            f"id {task_id!r} must be lowercase letters, digits and hyphens, e.g. 'eon-invoices'"
        )
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidTask("prompt is required: the whole instruction the run starts from")
    schedule = raw.get("schedule")
    if not isinstance(schedule, dict) or set(schedule) != {"at"}:
        raise InvalidTask(
            'schedule must be exactly {"at": "<ISO local datetime>"} — one-shot runs '
            "are the only kind aish supports so far"
        )
    at = parse_at(schedule["at"])
    if now is not None and at <= now:
        raise InvalidTask(
            f"schedule.at {at.isoformat(timespec='minutes')} is not in the future "
            f"(it is now {now.isoformat(timespec='minutes')})"
        )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise InvalidTask("enabled must be true or false")
    for name in ("note", "created", "model", "cwd"):
        if not isinstance(raw.get(name, ""), str):
            raise InvalidTask(f"{name} must be a string")
    note = str(raw.get("note", "")).strip()
    if "\n" in note:
        raise InvalidTask("note is one line")
    allow = tuple(_validate_prefix(p, deny_prefixes) for p in _strings(raw, "allow"))
    sites = tuple(dict.fromkeys(_validate_site(s) for s in _strings(raw, "sites")))
    cwd = str(raw.get("cwd", "")).strip()
    roots = tuple(_validate_dir(r, "roots entry") for r in _strings(raw, "roots"))
    return Task(
        id=task_id,
        prompt=prompt.strip(),
        at=at,
        note=note,
        enabled=enabled,
        created=str(raw.get("created", "")).strip(),
        model=str(raw.get("model", "")).strip(),
        allow=allow,
        sites=sites,
        cwd=_validate_dir(cwd, "cwd") if cwd else "",
        roots=roots,
    )


@dataclass(frozen=True)
class LoadProblem:
    """A file in the automations folder that is NOT a task. Kept beside the
    tasks rather than dropped: a broken file is an automation the owner
    believes exists and that will never run."""

    path: str
    error: str


@dataclass
class Loaded:
    tasks: list[Task] = field(default_factory=list)
    problems: list[LoadProblem] = field(default_factory=list)


def load_tasks(directory: Path, *, deny_prefixes: Collection[str] = ()) -> Loaded:
    """Every task in `directory`. Fails per FILE, never per directory: one
    unparseable file is a problem entry, and every other task still loads."""
    loaded = Loaded()
    if not directory.is_dir():
        return loaded
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            task = validate_task(raw, deny_prefixes=deny_prefixes)
            if task.id != path.stem:
                raise InvalidTask(
                    f"id {task.id!r} does not match the file name {path.name!r}"
                )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            # json.JSONDecodeError and InvalidTask are both ValueErrors.
            loaded.problems.append(LoadProblem(str(path), str(exc)))
            continue
        loaded.tasks.append(task)
    return loaded


def signature(directory: Path) -> tuple[tuple[str, int, int], ...]:
    """What changed, per FILE: name, mtime and size. The directory's own mtime
    moves only when an entry is added or removed, so an owner editing a task
    in place would never be noticed by it."""
    if not directory.is_dir():
        return ()
    out = []
    for path in directory.glob("*.json"):
        try:
            st = path.stat()
        except OSError:
            continue
        out.append((path.name, st.st_mtime_ns, st.st_size))
    return tuple(sorted(out))


def next_due(task: Task, fired: Collection[str], now: datetime) -> str:
    """Where this task stands at `now`: PENDING (not yet), DUE (fire it),
    EXPIRED (past `STALE_AFTER` unfired — report it, never fire it), FIRED or
    DISABLED. `fired` is the set of ledger keys — the one authority."""
    if task.key in fired:
        return FIRED
    if not task.enabled:
        return DISABLED
    if now < task.at:
        return PENDING
    if now - task.at <= STALE_AFTER:
        return DUE
    return EXPIRED


class LedgerUnreadable(Exception):
    """The fired ledger exists and cannot be read. Distinct from ABSENT: an
    absent ledger is "nothing has fired yet", an unreadable one is "aish does
    not know what has fired", and treating the second as the first would
    re-fire every run in it."""


class JsonStore:
    """A small JSON object on disk, written whole through `atomic_write`.

    The fired ledger has one writer, the scheduler loop. The scheduler's state
    file has two, both on the event loop: the loop's ticks and the resumer's
    `report_interrupted`. Each save snapshots the in-memory state on the loop
    and writes it in a thread, so two saves that overlap can land in either
    order, and the older snapshot may be written last. That window is
    milliseconds wide and is accepted rather than serialised: the worst it can
    do is drop the newest record or notified key until the next save, which
    can cost one duplicate push after a restart and can never cause a fire."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise LedgerUnreadable(f"{self.path}: {exc}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LedgerUnreadable(f"{self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise LedgerUnreadable(f"{self.path}: not a JSON object")
        return data

    def write(self, data: dict[str, Any]) -> None:
        atomic_write.publish(
            self.path, json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def fired_ledger(state_dir: Path) -> JsonStore:
    return JsonStore(state_dir / "scheduler" / "fired.json")


def scheduler_state(state_dir: Path) -> JsonStore:
    return JsonStore(state_dir / "scheduler" / "state.json")


def _when(at: datetime, now: datetime) -> str:
    day = (at.date() - now.date()).days
    label = {0: "today", 1: "tomorrow"}.get(day, at.strftime("%A"))
    return f"{at.strftime('%H:%M')} {label} ({at.strftime('%a %Y-%m-%d')})"


def _listed(items: Iterable[str]) -> str:
    return ", ".join(f"`{item}`" for item in items)


def describe(task: Task, now: datetime, *, replacing: bool = False) -> str:
    """The approval card's meaning, in words: when it runs, the WHOLE prompt
    (never truncated — it is exactly what will run with nobody watching), what
    may run without asking and where it may act as him. The JSON diff stays
    underneath; this is what he is agreeing to."""
    verb = "Replaces automation" if replacing else "New automation"
    lines = [
        f"{verb} '{task.id}'" + (f" — {task.note}" if task.note else ""),
        f"Runs ONCE, at {_when(task.at, now)}, in a new chat with nobody watching.",
        "",
        "The whole instruction it will run:",
        task.prompt,
        "",
    ]
    if task.allow:
        lines.append(
            "Commands that may run without asking you: those starting with "
            f"{_listed(task.allow)} (plus read-only ones)."
        )
    else:
        lines.append("Commands that may run without asking you: read-only ones only.")
    if task.sites:
        lines.append(
            f"Sites it may press things on as you without the per-site card: "
            f"{_listed(task.sites)} (presses that draw their own card still ask)."
        )
    else:
        lines.append("Sites it may act on as you without asking: none.")
    if task.model:
        lines.append(f"Model: {task.model} (if it cannot be built, the run does not start).")
    if task.cwd:
        lines.append(f"Working directory: {task.cwd}")
    if task.roots:
        lines.append(f"Also trusted for reading: {_listed(task.roots)}")
    lines += [
        "Everything else that changes something waits for you on a card.",
        f"If it has not started by {task.deadline.strftime('%H:%M %a %Y-%m-%d')}, "
        "aish gives it up and records that (and notifies you, if notifications "
        "are set up).",
    ]
    return "\n".join(lines)
