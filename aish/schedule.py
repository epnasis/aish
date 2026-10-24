"""Automations the owner authored: the store and the time logic (epic #142 W2, S1).

An automation is one inert JSON file in `<config home>/automations/`, named
`<id>.json`. Nothing here runs anything and nothing here imports the server:
`scheduler.py` owns the runtime, and `agent.py`'s `schedule_task` is the one
writer the model has, through the diff-approval card. Code never writes the
owner-authored directory — the only writer of what has FIRED is the ledger in
the state tree, which is why an automation has no `fired` field to flip.

Two schedule shapes, exactly one per task: `{"at": "<ISO local datetime>"}`, a
one-shot (S1), and `{"cron": "<5-field expr>"}`, recurring (S2), which may also
carry `"until": "<ISO local datetime>"`, its structural end (S5), and
`"from": "<ISO local datetime>"`, its structural start (S6). The JSON key is
`from`; the Python field is `Task.start`, because `from` is a keyword. Anything
else is refused at load and at creation, never guessed at.
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

from . import atomic_write, files, owner_ua, paths, vocab
from .approval import EXEC_WRAPPERS, check_denied

AUTOMATIONS_DIRNAME = "automations"

#: What a task is. A `monitor` (S3) is "watch X, tell me when it changes":
#: each run compares against the snapshot the previous run saved and ends with
#: a verdict aish parses (`parse_verdict`).
KIND_TASK = "task"
KIND_MONITOR = "monitor"
KINDS = (KIND_TASK, KIND_MONITOR)

#: When a finished run pushes. Defaulted per kind (`DEFAULT_NOTIFY`), never
#: forced: an owner-written value is honoured as long as it is valid.
NOTIFY_ALWAYS = "always"
NOTIFY_ON_CHANGE = "on_change"
NOTIFY_NEVER = "never"
NOTIFY_POLICIES = (NOTIFY_ALWAYS, NOTIFY_ON_CHANGE, NOTIFY_NEVER)
DEFAULT_NOTIFY = {KIND_TASK: NOTIFY_ALWAYS, KIND_MONITOR: NOTIFY_ON_CHANGE}

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
    "model", "allow", "sites", "cwd", "roots", "kind", "notify", "psu_ua",
)

# Statuses `next_due` answers with.
PENDING = "pending"
DUE = "due"
EXPIRED = "expired"
FIRED = "fired"
DISABLED = "disabled"
#: A cron task past its `until` with nothing left to fire (S5). DERIVED from the
#: file, the ledger and the clock every time, never stored: `settled` stays
#: one-shot keys only, and a cron task still never settles.
ENDED = "ended"


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
    """Exactly one of `at` (a one-shot) and `cron` (recurring) is set —
    `validate_task` is the only constructor that guarantees it."""

    id: str
    prompt: str
    at: datetime | None  # local wall-clock time, naive; None for a cron task
    note: str = ""
    enabled: bool = True
    created: str = ""
    model: str = ""
    allow: tuple[str, ...] = ()
    sites: tuple[str, ...] = ()
    cwd: str = ""
    roots: tuple[str, ...] = field(default=())
    cron: str = ""  # normalised 5-field expr; "" for a one-shot
    kind: str = KIND_TASK
    notify: str = NOTIFY_ALWAYS  # resolved: the owner's value, else the kind's default
    #: The browser UA of the owner who delegated this one-shot at a moment
    #: (owner_ua); "" for every cron task, which validate_task enforces.
    psu_ua: str = ""
    #: A cron task's structural end (S5), local wall-clock, inclusive: no
    #: occurrence after it is ever due. None for no end, and for every one-shot.
    until: datetime | None = None
    #: A cron task's structural start (S6), the JSON `schedule.from`: local
    #: wall-clock, inclusive — no occurrence before it is ever due. None for no
    #: start, and for every one-shot. It only ever RESTRICTS: the scheduler's
    #: own `since` floor still applies beside it.
    start: datetime | None = None

    @property
    def recurring(self) -> bool:
        return bool(self.cron)

    @property
    def monitor(self) -> bool:
        return self.kind == KIND_MONITOR

    @property
    def schedule(self) -> dict[str, str]:
        # Every field of the schedule, or a rewrite through `to_dict` (`/tasks
        # disable` then `enable`) silently strips the one left out.
        if self.cron:
            out = {"cron": self.cron}
            if self.start is not None:
                out["from"] = self.start.isoformat()
            if self.until is not None:
                out["until"] = self.until.isoformat()
            return out
        return {"at": self.one_shot_at().isoformat()}

    def past_until(self, now: datetime) -> bool:
        """Whether `now` is after this task's structural end (never, without one)."""
        return self.until is not None and now > self.until

    def before_start(self, now: datetime) -> bool:
        """Whether `now` is before this task's structural start (never, without one)."""
        return self.start is not None and now < self.start

    def one_shot_at(self) -> datetime:
        if self.at is None:
            raise ValueError(f"automation {self.id!r} is recurring and has no single time")
        return self.at

    def key_for(self, occurrence: datetime) -> str:
        """What the fired ledger is keyed by: the task and ONE occurrence. It
        carries the time, so an owner who hand-edits `at` (or the cron expr)
        has re-armed the task — the edited task is a new run, and nothing has
        to clear an old flag for it to fire."""
        return f"{self.id}:{occurrence.isoformat()}"

    @property
    def key(self) -> str:
        """A one-shot's single ledger key. A cron task has one per occurrence
        (`key_for`), so asking it for THE key is a bug, and raises."""
        return self.key_for(self.one_shot_at())

    @property
    def deadline(self) -> datetime | None:
        """When a one-shot is given up; None for every cron task, whose
        occurrences are given up one at a time as the next one arrives. A cron
        task WITH an `until` does end: its final occurrence (the last at or
        before `until`) stays due through `until + STALE_AFTER` and no later —
        `due()` derives that ending each time; nothing stores it here."""
        return None if self.at is None else self.at + STALE_AFTER

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
        # Absent for a plain task, so a task file renders byte-identically to
        # S1/S2's; spelled out for a monitor, so the file says what it does.
        if self.monitor:
            out["kind"] = self.kind
            out["notify"] = self.notify
        # Only when set, so an owner-less task renders as it always did, and a
        # rewrite (`/tasks enable|disable`) keeps what the card approved.
        if self.psu_ua:
            out["psu_ua"] = self.psu_ua
        return out

    def render(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"


def parse_at(value: object, name: str = "schedule.at") -> datetime:
    """An ISO datetime as LOCAL wall-clock time. One with an offset is
    converted to local time (unambiguous); a bare date is refused, because
    "2026-10-15" says when, not what time. `name` is the field it came from:
    `schedule.until` and `schedule.from` are read by the same rule."""
    if not isinstance(value, str) or "T" not in value and " " not in value.strip():
        raise InvalidTask(
            f"{name} must be an ISO local date and time like "
            f"'2026-10-15T22:05', got {value!r}"
        )
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        raise InvalidTask(
            f"{name} {value!r} is not an ISO date and time like '2026-10-15T22:05'"
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
    as_of: datetime | None = None,
) -> Task:
    """One task from its JSON object, or InvalidTask. Fails CLOSED: an unknown
    key is refused rather than ignored, because a key the owner wrote that aish
    silently drops is a constraint he believes is in force.

    `now` is passed only at CREATION, where a time already in the past is an
    error; a task file loaded later is legitimately in the past. A cron expr
    must have a next occurrence after `now`, else `as_of` (the loader's clock),
    else the wall clock — an expr that can never run is refused, never armed.

    `until` (cron only) is checked against the clock at CREATION only, exactly
    as `at` is: an end already past, or one before the expr's next occurrence,
    is a card that could never run. A loaded file past its `until` is not a
    problem — it is an automation that ended, and `due()` says so.

    `from` (cron only, S6) must come before `until` ALWAYS, at load too: that
    window is empty whatever the clock says, and no file predating S6 carries
    the field. The no-occurrence check generalizes to [max(now, from), until]
    and stays creation-only. A `from` already past at creation is allowed and
    inert — `since` already keeps a new schedule from firing retroactively."""
    if not isinstance(raw, dict):
        raise InvalidTask("an automation must be a JSON object")
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        raise InvalidTask(f"unknown field(s) {', '.join(unknown)}; known: {', '.join(FIELDS)}")
    task_id = raw.get("id")
    # fullmatch: `$` also matches before a trailing newline (#5 S3).
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise InvalidTask(
            f"id {task_id!r} must be lowercase letters, digits and hyphens, e.g. 'eon-invoices'"
        )
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidTask("prompt is required: the whole instruction the run starts from")
    schedule = raw.get("schedule")
    if isinstance(schedule, dict) and "until" in schedule and "cron" not in schedule:
        raise InvalidTask(
            "schedule.until ends a recurring schedule and goes with cron only; "
            'a one-shot already runs once, at "at"'
        )
    if isinstance(schedule, dict) and "from" in schedule and "cron" not in schedule:
        raise InvalidTask(
            "schedule.from starts a recurring schedule and goes with cron only; "
            'a one-shot already starts once, at "at"'
        )
    if not isinstance(schedule, dict) or set(schedule) not in (
        {"at"}, {"cron"}, {"cron", "until"}, {"cron", "from"}, {"cron", "from", "until"}
    ):
        raise InvalidTask(
            'schedule must be exactly one of {"at": "<ISO local datetime>"} (once) '
            'or {"cron": "<minute hour day-of-month month day-of-week>"} (recurring, '
            'optionally with "from" and "until")'
        )
    at: datetime | None = None
    until: datetime | None = None
    start: datetime | None = None
    cron = ""
    if "at" in schedule:
        at = parse_at(schedule["at"])
        if now is not None and at <= now:
            raise InvalidTask(
                f"schedule.at {at.isoformat(timespec='minutes')} is not in the future "
                f"(it is now {now.isoformat(timespec='minutes')})"
            )
    else:
        if not isinstance(schedule["cron"], str):
            raise InvalidTask(f"schedule.cron must be a string, got {schedule['cron']!r}")
        cron = parse_cron(schedule["cron"]).expr
        upcoming = cron_next(cron, now or as_of or datetime.now())  # NoOccurrence: InvalidTask
        if "from" in schedule:
            start = parse_at(schedule["from"], "schedule.from")
        if "until" in schedule:
            until = parse_at(schedule["until"], "schedule.until")
        if start is not None and until is not None and start >= until:
            raise InvalidTask(
                f"schedule.from {start.isoformat(timespec='minutes')} is not before "
                f"schedule.until {until.isoformat(timespec='minutes')}, so no occurrence "
                "could ever run"
            )
        if now is not None and until is not None and until <= now:
            raise InvalidTask(
                f"schedule.until {until.isoformat(timespec='minutes')} is not in the "
                f"future (it is now {now.isoformat(timespec='minutes')})"
            )
        if now is not None and start is not None and start > now:
            # The first run of the window: `cron_next` is strictly-after, so
            # probing a hair before `from` makes an occurrence exactly AT it
            # the first, inclusive, as `until`'s edge is.
            upcoming = cron_next(cron, start - timedelta(microseconds=1))
        if now is not None and until is not None and upcoming > until:
            since_words = (
                f"at or after schedule.from {start.isoformat(timespec='minutes')}"
                if start is not None and start > now else "after now"
            )
            raise InvalidTask(
                f"schedule.cron {cron!r} has no occurrence {since_words} and at or "
                f"before schedule.until {until.isoformat(timespec='minutes')} (its next is "
                f"{upcoming.isoformat(timespec='minutes')}), so it would never run"
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
    kind, notify = _validate_kind_and_notify(raw, recurring=bool(cron))
    psu_ua = _validate_psu_ua(raw, recurring=bool(cron))
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
        cron=cron,
        kind=kind,
        notify=notify,
        psu_ua=psu_ua,
        until=until,
        start=start,
    )


def _validate_psu_ua(raw: dict, *, recurring: bool) -> str:
    """The owner's browser UA a one-shot carries (owner_ua). Refused on a cron
    task: a recurring run is background work by the owner's rule, and a UA
    there would claim him present at every occurrence. It ends up in an HTTP
    header, so control characters are refused rather than carried."""
    value = raw.get("psu_ua", "")
    if not isinstance(value, str):
        raise InvalidTask("psu_ua must be a string")
    value = value.strip()
    if not value:
        return ""
    if recurring:
        raise InvalidTask(
            "psu_ua is for a one-shot only; a recurring automation runs in the "
            "background and must not claim the owner is present"
        )
    if len(value) > owner_ua.MAX_UA_CHARS:
        raise InvalidTask(f"psu_ua is longer than {owner_ua.MAX_UA_CHARS} characters")
    if _CONTROL_CHARS.search(value):
        raise InvalidTask("psu_ua contains a control character")
    return value


def _validate_kind_and_notify(raw: dict, *, recurring: bool) -> tuple[str, str]:
    """`kind` and the resolved `notify`. The default is the kind's, and a value
    the owner wrote is honoured, never overridden — but only where aish can do
    what it says, so a policy that would silently mean something else is
    refused rather than kept."""
    kind = raw.get("kind", KIND_TASK)
    if kind not in KINDS:
        raise InvalidTask(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    notify = raw.get("notify", DEFAULT_NOTIFY[kind])
    if notify not in NOTIFY_POLICIES:
        raise InvalidTask(
            f"notify must be one of {', '.join(NOTIFY_POLICIES)}, got {notify!r}"
        )
    if kind == KIND_MONITOR and not recurring:
        raise InvalidTask(
            "a monitor compares each run against the one before, so it needs a "
            'recurring schedule: {"cron": "..."}, not {"at": "..."}'
        )
    if kind == KIND_TASK and notify != NOTIFY_ALWAYS:
        # on_change has no verdict to read on a plain task, and the done-push
        # policy is threaded only into monitor runs in this slice.
        raise InvalidTask(
            f"notify {notify!r} applies to monitors only; a plain task always "
            'notifies when it finishes — use "kind": "monitor" or drop notify'
        )
    return kind, notify


# -- monitors: the snapshot store and the verdict parse (S3) --------------------

#: The largest snapshot block a run may hand back. Over it the block is
#: MALFORMED — the run is unreadable — and never clipped: a clipped baseline
#: would corrupt every later comparison against it, silently.
SNAPSHOT_MAX_CHARS = 8000

#: What a monitor's answer must end with. aish's own protocol words, matched
#: against MODEL output — so the list is declared, with what a miss costs.
VERDICT_LINE_MARKER = "VERDICT:"
SNAPSHOT_FENCE_OPEN = "```snapshot"
SNAPSHOT_FENCE_CLOSE = "```"
VERDICT_MARKERS = vocab.declare(
    "schedule.VERDICT_MARKERS",
    (VERDICT_LINE_MARKER, SNAPSHOT_FENCE_OPEN, SNAPSHOT_FENCE_CLOSE),
    languages="aish's own protocol words",
    on_miss=vocab.FRICTION,
    structural="the parse's mandatory `unreadable` ending: no marker, no silence",
    note="A miss is an `unreadable` run: a noisy push (on the streak) and a "
    "baseline that is NOT refreshed. It can never permit anything, and it can "
    "never make a run silent — silence needs a parse that matched.",
)

# Statuses `parse_verdict` answers with. `baseline` is a fact about the disk
# (there was no prior snapshot), never the model's word.
BASELINE = "baseline"
CHANGED = "changed"
UNCHANGED = "unchanged"
UNREADABLE = "unreadable"

_VERDICT_LINE = re.compile(
    r"^[ \t]*VERDICT:[ \t]*(?P<word>[A-Za-z]*)[ \t]*(?:[-—:–][ \t]*)?(?P<summary>.*?)[ \t]*$",
    re.M,
)
_VERDICT_WORDS = {BASELINE, CHANGED, UNCHANGED}


@dataclass(frozen=True)
class Verdict:
    """What a monitor run's answer said, as far as aish could read it.
    `reason` says why a run is `unreadable` and is empty otherwise; `said` is
    the model's own verdict word, kept beside the status code assigned."""

    status: str
    snapshot: str = ""
    summary: str = ""
    said: str = ""
    reason: str = ""


def parse_verdict(answer: str, *, first_run: bool) -> Verdict:
    """The monitor protocol's ending, read mechanically.

    Anchored on the LAST `VERDICT:` line, because aish may append text after
    the model's block (the quick-reply net), and a model may mention the word
    earlier. A readable answer needs BOTH that line AND a well-formed
    ```snapshot block after it, inside `SNAPSHOT_MAX_CHARS`; anything less is
    `unreadable`, never guessed into changed or unchanged (L8). This replaces
    issue #5's "unparseable → treat as changed": a guess in either direction
    stands where the evidence should be. A first run (no snapshot on disk) is
    `baseline` — and needs the block too, so a first run without one fails
    noisy rather than arming a monitor with nothing to compare. An answer with
    more than one ```snapshot fence, or a block holding a `VERDICT:` line, is
    `unreadable` too: a snapshot carrying the protocol's own markers is never
    trusted, in either direction, because a page can carry a forged ending."""
    verdict = _read_verdict(answer or "", first_run=first_run)
    vocab.note("schedule.VERDICT_MARKERS", matched=verdict.status != UNREADABLE)
    return verdict


def _read_verdict(answer: str, *, first_run: bool) -> Verdict:
    lines = list(_VERDICT_LINE.finditer(answer))
    if not lines:
        return Verdict(UNREADABLE, reason="no VERDICT: line")
    line = lines[-1]
    word = line.group("word").lower()
    summary = line.group("summary").strip()
    if word not in _VERDICT_WORDS:
        return Verdict(UNREADABLE, said=word, reason=f"verdict word {word!r} is not one aish reads")
    rest = answer[line.end():].split("\n")[1:]  # the lines after the VERDICT line
    body, problem = _snapshot_block(rest)
    if problem:
        return Verdict(UNREADABLE, said=word, summary=summary, reason=problem)
    # A snapshot carrying the protocol's own markers is never trusted, in
    # either direction. The block holds what a run READ, and a page can hold
    # a complete forged ending (a VERDICT: line and its own ```snapshot fence):
    # copied verbatim, the last-VERDICT anchor reads the PAGE's verdict past
    # the model's own, and stores the page's bytes as the baseline. The whole
    # answer is checked, because the forged ending parses as a clean block.
    openers = sum(1 for text in answer.split("\n") if text.strip() == SNAPSHOT_FENCE_OPEN)
    if openers != 1 or _VERDICT_LINE.search(body):
        return Verdict(
            UNREADABLE, said=word, summary=summary,
            reason="the answer carries the protocol's own markers inside what was read "
            f"({openers} ```snapshot fences, or a VERDICT: line in the snapshot)",
        )
    if first_run:
        return Verdict(BASELINE, snapshot=body, summary=summary, said=word)
    if word == BASELINE:
        return Verdict(
            UNREADABLE, said=word, summary=summary,
            reason="the run said baseline, but a previous snapshot exists",
        )
    return Verdict(word, snapshot=body, summary=summary, said=word)


def _snapshot_block(lines: list[str]) -> tuple[str, str]:
    """The first ```snapshot block in `lines` (blank lines may precede it) and
    "" — or "" and why there is none. Text after the closing fence is allowed."""
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index == len(lines) or lines[index].strip() != SNAPSHOT_FENCE_OPEN:
        return "", "no ```snapshot block after the VERDICT: line"
    body: list[str] = []
    for text in lines[index + 1:]:
        if text.strip() == SNAPSHOT_FENCE_CLOSE:
            joined = "\n".join(body)
            if len(joined) > SNAPSHOT_MAX_CHARS:
                return "", (
                    f"the snapshot block is {len(joined)} chars, over the "
                    f"{SNAPSHOT_MAX_CHARS} cap (never clipped)"
                )
            return joined, ""
        body.append(text.rstrip("\r"))
    return "", "the ```snapshot block is never closed"


SNAPSHOTS_DIRNAME = "snapshots"
_SESSION_NAME = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_SNAPSHOT_HEADER = re.compile(
    r"^<!-- aish monitor snapshot: saved (?P<at>\S+) by (?P<session>\S+) -->$"
)


class SnapshotUnreadable(Exception):
    """A snapshot file exists and cannot be read. Distinct from ABSENT, as the
    ledger is: absent is "no baseline yet", unreadable is "aish does not know
    the baseline", and running the second as the first would overwrite it."""


@dataclass(frozen=True)
class Snapshot:
    """The last good baseline of one monitor: what it saw, and the stamp of
    the run that saved it — carried in the file, because an unchanged refresh
    rewrites the file and its mtime says nothing about when it was seen."""

    body: str
    saved_at: datetime
    session: str


def snapshot_path(state_dir: Path, task_id: str) -> Path:
    """`<state dir>/scheduler/snapshots/<id>.md`, built ONLY from an id that is
    an automation id. `fullmatch`, not `TASK_ID_RE.match`: `$` also matches
    before a trailing newline, and a path is no place to find out."""
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"not an automation id: {task_id!r}")
    return state_dir / "scheduler" / SNAPSHOTS_DIRNAME / f"{task_id}.md"


def read_snapshot(state_dir: Path, task_id: str) -> Snapshot | None:
    path = snapshot_path(state_dir, task_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise SnapshotUnreadable(f"{path}: {exc}") from exc
    header, _, body = text.partition("\n")
    match = _SNAPSHOT_HEADER.match(header)
    if not match or not _SESSION_NAME.match(match.group("session")):
        raise SnapshotUnreadable(f"{path}: the first line is not aish's snapshot stamp")
    try:
        saved_at = datetime.fromisoformat(match.group("at"))
    except ValueError:
        raise SnapshotUnreadable(f"{path}: the stamp's time does not parse") from None
    if len(body) > SNAPSHOT_MAX_CHARS:
        raise SnapshotUnreadable(f"{path}: over the {SNAPSHOT_MAX_CHARS}-char cap")
    return Snapshot(body=body, saved_at=saved_at, session=match.group("session"))


def write_snapshot(
    state_dir: Path, task_id: str, body: str, *, saved_at: datetime, session: str
) -> Path:
    """Replace the baseline, whole, through `atomic_write`."""
    if not _SESSION_NAME.match(session):
        raise ValueError(f"not a session name: {session!r}")
    if len(body) > SNAPSHOT_MAX_CHARS:
        raise ValueError(f"snapshot over the {SNAPSHOT_MAX_CHARS}-char cap")
    path = snapshot_path(state_dir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = saved_at.isoformat(timespec="seconds")
    atomic_write.publish(
        path, f"<!-- aish monitor snapshot: saved {stamp} by {session} -->\n{body}",
        encoding="utf-8",
    )
    return path


def snapshot_ids(state_dir: Path) -> list[str]:
    """The automation ids that have a snapshot file. A name that is not an id
    is not aish's, and is left alone."""
    directory = state_dir / "scheduler" / SNAPSHOTS_DIRNAME
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.md") if TASK_ID_RE.fullmatch(p.stem))


def remove_snapshot(state_dir: Path, task_id: str) -> bool:
    try:
        snapshot_path(state_dir, task_id).unlink()
    except FileNotFoundError:
        return False
    return True


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


def load_tasks(
    directory: Path,
    *,
    deny_prefixes: Collection[str] = (),
    as_of: datetime | None = None,
) -> Loaded:
    """Every task in `directory`. Fails per FILE, never per directory: one
    unparseable file is a problem entry, and every other task still loads."""
    loaded = Loaded()
    if not directory.is_dir():
        return loaded
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            task = validate_task(raw, deny_prefixes=deny_prefixes, as_of=as_of)
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


@dataclass(frozen=True)
class Due:
    """Where a task stands, and the ONE occurrence that answer is about — a
    one-shot's `at`, a cron task's latest occurrence at or before now (None
    when it has none inside the horizon)."""

    status: str
    occurrence: datetime | None
    #: ENDED only: the final occurrence was one the task could have missed and
    #: it never fired before its grace ran out — a run that did not happen,
    #: which the scheduler records (S5). False for every other arm.
    final_missed: bool = False


def _occurrence_of(task_id: str, key: str) -> datetime | None:
    head, sep, tail = key.partition(":")
    if not sep or head != task_id:
        return None
    try:
        return datetime.fromisoformat(tail)
    except ValueError:
        return None


def last_fired(task_id: str, fired: Iterable[str]) -> datetime | None:
    """The latest occurrence of `task_id` the ledger says fired. Exact id match:
    ids hold no ':', so `eon` never reads `eon-invoices`' keys."""
    found = [when for key in fired if (when := _occurrence_of(task_id, key)) is not None]
    return max(found, default=None)


def due(
    task: Task, fired: Collection[str], now: datetime, *, since: datetime | None = None
) -> Due:
    """Where this task stands at `now`: PENDING (not yet), DUE (fire it),
    EXPIRED (a one-shot past `STALE_AFTER` unfired — report it, never fire it),
    FIRED or DISABLED. `fired` is the set of ledger keys — the one authority.

    A cron task is DUE when its LATEST occurrence at or before `now` is unfired:
    an older miss is never fired, so a daemon down over N slots fires the last
    one, once. That occurrence must also be one the task could have missed —
    at or after `since` (when this schedule began to exist; the scheduler
    supplies it) and after the task's last fired occurrence. Without `since`,
    nothing past is provably a miss, so nothing is due. A cron task without
    an `until` never expires and has no deadline.

    With an `until` (S5) the search runs at or before `min(now, until)`, so an
    occurrence strictly after `until` is never the answer, and one exactly at
    it is. Once `now` is past `until` the task is ENDED — outranking DISABLED
    and every PENDING-shaped arm, which would read as alive — except that the
    final occurrence, if it is still a provable miss, keeps the one-shot's
    grace: DUE through `until + STALE_AFTER`, then ENDED with `final_missed`.

    With a `from` (S6) no occurrence before it is ever a provable miss
    (`_a_provable_miss`), so before the start a task is PENDING — no status of
    its own — and a final occurrence before it ENDS without firing."""
    if not task.recurring:
        return Due(_one_shot_status(task, fired, now), task.at)
    if task.until is not None and now > task.until:
        return _ended_or_final(task, fired, now, since)
    try:
        occurrence = cron_prev(task.cron, now)
    except NoOccurrence:
        return Due(PENDING, None)
    if task.key_for(occurrence) in fired:
        return Due(FIRED, occurrence)
    if not task.enabled:
        return Due(DISABLED, occurrence)
    if not _a_provable_miss(task, occurrence, fired, since):
        return Due(PENDING, occurrence)
    return Due(DUE, occurrence)


def _a_provable_miss(
    task: Task, occurrence: datetime, fired: Collection[str], since: datetime | None
) -> bool:
    """Whether `occurrence` is one this task could have missed. The ONE site
    both `due()`'s live arm and `_ended_or_final`'s grace arm ask, so `from`
    (S6) is enforced for both by the line below: a final occurrence before
    `from` ENDS rather than fires. `from` only ever restricts — it never
    stands in for `since`, so with no `since` nothing is a miss, as before;
    the effective beginning is max(since, from), falling out of the two."""
    if since is None or occurrence < since:
        return False
    if task.start is not None and occurrence < task.start:
        return False
    previous = last_fired(task.id, fired)
    # The clock went back (the fall-back hour): this occurrence's key may have
    # been pruned, but a later one fired, so it is not a miss.
    return previous is None or occurrence > previous


def _ended_or_final(
    task: Task, fired: Collection[str], now: datetime, since: datetime | None
) -> Due:
    """`now` is past `until`: every arm but one is ENDED. The one is the final
    occurrence still within its grace, unfired and a provable miss — the
    covered period's work is still wanted, late, as a one-shot's would be."""
    assert task.until is not None
    try:
        final = cron_prev(task.cron, task.until)
    except NoOccurrence:
        return Due(ENDED, None)
    if task.key_for(final) in fired or not task.enabled:
        return Due(ENDED, final)
    if not _a_provable_miss(task, final, fired, since):
        return Due(ENDED, final)
    if now - task.until <= STALE_AFTER:
        return Due(DUE, final)
    return Due(ENDED, final, final_missed=True)


def _one_shot_status(task: Task, fired: Collection[str], now: datetime) -> str:
    at = task.one_shot_at()
    if task.key in fired:
        return FIRED
    if not task.enabled:
        return DISABLED
    if now < at:
        return PENDING
    if now - at <= STALE_AFTER:
        return DUE
    return EXPIRED


def next_due(
    task: Task, fired: Collection[str], now: datetime, *, since: datetime | None = None
) -> str:
    """`due(...)`'s status alone."""
    return due(task, fired, now, since=since).status


#: Cap on how many skipped occurrences are counted one by one; past it the
#: record says "at least" this many.
SKIP_COUNT_CAP = 1000


@dataclass(frozen=True)
class Skipped:
    """Occurrences of a cron task that will never fire because a later one is
    now the latest. `count_capped` means `count` is a floor, not a total."""

    first: datetime
    last: datetime
    count: int
    count_capped: bool


def skipped_before(
    task: Task, occurrence: datetime, *, since: datetime, after: datetime | None
) -> Skipped | None:
    """The occurrences in [max(since, from), occurrence) that are also later
    than `after` (the last one that fired or was already recorded as skipped).
    Bounded: the scan starts at the floor, never further back. The `from`
    floor (S6) is what keeps a record from claiming occurrences before the
    task's start were "skipped" — they were never in its window."""
    floor = since if task.start is None else max(since, task.start)
    start = floor - timedelta(microseconds=1)
    if after is not None and after > start:
        start = after
    try:
        first = cron_next(task.cron, start)
        if first >= occurrence:
            return None
        last = cron_prev(task.cron, occurrence - timedelta(minutes=1))
    except NoOccurrence:
        return None
    count, cursor = 1, first
    while cursor < last and count < SKIP_COUNT_CAP:
        cursor = cron_next(task.cron, cursor)
        count += 1
    return Skipped(first, last, count, count_capped=cursor < last)


#: Occurrence keys kept per CRON task in the fired ledger. Once a later
#: occurrence fired, older keys are never consulted (`due` refuses anything at
#: or before the last fired one); more than one survives a clock that steps
#: back, as in the fall-back hour.
LEDGER_KEEP_PER_CRON_TASK = 4


def prune_ledger(
    data: dict[str, Any], task: Task, *, protected: Collection[str]
) -> list[str]:
    """Drop, in place, all but the newest `LEDGER_KEEP_PER_CRON_TASK` occurrence
    keys of cron task `task`; returns what was dropped. A one-shot's key is
    never dropped while its task file exists (`protected`): a pruned one-shot
    key reads as never-fired, then EXPIRED, then pushes a false "did not run".
    A one-shot task prunes nothing, and an unparseable key is left alone."""
    if not task.recurring:
        return []
    mine = sorted(
        (when, key) for key in data if (when := _occurrence_of(task.id, key)) is not None
    )
    dropped = [key for _when, key in mine[:-LEDGER_KEEP_PER_CRON_TASK] if key not in protected]
    for key in dropped:
        del data[key]
    return dropped


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


def when_words(task: Task, now: datetime) -> str:
    """When a task runs, in the card's own words — shared by the creation card
    (`describe`) and the terminal's `/tasks`, so the two can never describe
    the same schedule differently."""
    if task.recurring:
        # A plain statement of the window: "from <a past date>" is a fact
        # about the schedule, never a promise of something to come.
        begin = f" from {day_time(task.start)}" if task.start is not None else ""
        end = f" until {day_time(task.until)}" if task.until is not None else ""
        return f"on the schedule `{task.cron}`{begin}{end}"
    return f"ONCE, at {_when(task.one_shot_at(), now)}"


def day_time(when: datetime) -> str:
    return when.strftime("%a %Y-%m-%d %H:%M")


def upcoming(task: Task, now: datetime, count: int) -> list[datetime]:
    """A cron task's next `count` occurrences after `now`, CLIPPED to its
    window: none before `from` (an occurrence exactly at it is the first), none
    after `until` — every surface that lists what is coming reads this, so
    none can show a run the schedule will never start. Fewer (or none) near
    the end."""
    out: list[datetime] = []
    cursor = now
    if task.start is not None and task.start > now:
        # `cron_next` is strictly-after: a hair before `from` keeps it inclusive.
        cursor = task.start - timedelta(microseconds=1)
    while len(out) < count:
        try:
            cursor = cron_next(task.cron, cursor)
        except NoOccurrence:
            break
        if task.until is not None and cursor > task.until:
            break
        out.append(cursor)
    return out


#: How many coming runs a card, the model's result and `automations` list. The
#: dates are the cron expression EVALUATED — the same `upcoming` the scheduler's
#: window is clipped by — so they cannot disagree with what will run, which a
#: translation of the expression into words could (#459).
NEXT_SHOWN = 5


def next_runs_words(task: Task, now: datetime) -> str:
    """A cron task's next `NEXT_SHOWN` runs, or that none is left."""
    coming = upcoming(task, now, NEXT_SHOWN)
    if not coming:
        return "no run is left before its end"
    return "next: " + ", ".join(day_time(o) for o in coming)


def final_occurrence(task: Task) -> datetime | None:
    """The last occurrence at or before `until`; None without an `until`, when
    there is none inside the horizon, or when it falls before `from` (S6) — a
    hand-edited window no occurrence lands in has no final run, and `due()`
    never fires one there (`_a_provable_miss`)."""
    if task.until is None:
        return None
    try:
        final = cron_prev(task.cron, task.until)
    except NoOccurrence:
        return None
    if task.start is not None and final < task.start:
        return None
    return final


def _ending_line(task: Task) -> str:
    """What the `until` means, from the field, said on the card — so a wrong
    reading of "after I'm back" shows as a wrong date the owner can deny."""
    assert task.until is not None
    final = final_occurrence(task)
    grace = (task.until + STALE_AFTER).strftime("%H:%M %a %Y-%m-%d")
    last = f"Its last run is {day_time(final)}, then it ends" if final else "It ends"
    return (
        f"{last}: no occurrence after {day_time(task.until)} is ever run. If that last "
        f"run cannot start on time (or aish is down), it may still start late, until "
        f"{grace}, and never after. When it has ended you are notified once, whatever "
        "the notify setting (if notifications are set up)."
    )


def _starting_line(task: Task, now: datetime, *, replacing: bool) -> str:
    """What the `from` means, said on the card from the CLIPPED upcoming —
    never from the field raw, so a mistyped past date still shows the real
    first run, and a wrong reading of "od 7 listopada" shows as a wrong date
    the owner can deny before approving."""
    assert task.start is not None
    coming = upcoming(task, now, 1)
    if not coming:
        return "No run is left in its window."
    # A replaced task may already have run: its next run is not its first.
    first = f"Its {'next' if replacing else 'first'} run is {day_time(coming[0])}"
    if task.start > now:
        return f"{first}: no occurrence before {day_time(task.start)} is ever run."
    return f"{first}."


def _listed(items: Iterable[str]) -> str:
    return ", ".join(f"`{item}`" for item in items)


_NOTIFY_WORDS = {
    NOTIFY_ON_CHANGE: "You are notified only when a run reports a change.",
    NOTIFY_ALWAYS: "You are notified every time a run finishes, changed or not.",
    NOTIFY_NEVER: "You are not notified when a run finishes; the chat still holds "
    "each run, and anything it waits on for approval still notifies you.",
}


def _monitor_lines(task: Task) -> list[str]:
    if not task.monitor:
        return []
    return [
        "It is a MONITOR: each run reads what it watches, compares it with the "
        "snapshot the previous run saved, and ends with a verdict — changed or "
        "unchanged — plus a new snapshot. The first run only saves the baseline.",
        _NOTIFY_WORDS[task.notify],
        "A run that ends without a verdict aish can read, or that fails, keeps the "
        "old baseline and notifies you, if notifications are set up (the first "
        "time, then at most once a day while it lasts).",
    ]


def describe(task: Task, now: datetime, *, replacing: bool = False) -> str:
    """The approval card's meaning, in words: when it runs, the WHOLE prompt
    (never truncated — it is exactly what will run with nobody watching), what
    may run without asking and where it may act as him. The JSON diff stays
    underneath; this is what he is agreeing to."""
    verb = "Replaces automation" if replacing else "New automation"
    ending: list[str] = []
    if task.recurring:
        nexts = next_runs_words(task, now)
        when = (
            f"Runs {when_words(task, now)} — {nexts} — each time in a new chat with "
            "nobody watching."
        )
        if task.start is not None:
            ending.append(_starting_line(task, now, replacing=replacing))
        if task.until is not None:
            ending.append(_ending_line(task))
    else:
        when = f"Runs {when_words(task, now)}, in a new chat with nobody watching."
    # An edit keeps the file's `enabled`, so a paused task's card would
    # otherwise read as live: the times above are when it WOULD run.
    paused = (
        ["It is PAUSED: none of those runs happen until it is resumed."]
        if not task.enabled else []
    )
    lines = [
        f"{verb} '{task.id}'" + (f" — {task.note}" if task.note else ""),
        when,
        *paused,
        *ending,
        *_monitor_lines(task),
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
    if task.psu_ua:
        lines.append(
            "Requests it makes to your bank will say you are present, with the "
            "browser you are using now — this run is yours, delegated."
        )
    if task.model:
        lines.append(f"Model: {task.model} (if it cannot be built, the run does not start).")
    if task.cwd:
        lines.append(f"Working directory: {task.cwd}")
    if task.roots:
        lines.append(f"Also trusted for reading: {_listed(task.roots)}")
    lines.append("Everything else that changes something waits for you on a card.")
    if task.deadline is None:
        lines.append(
            "A run that cannot start is retried until the next scheduled time, then "
            "skipped and recorded; you are notified the first time runs fail to start, "
            "then at most once a day while they keep failing (if notifications are set "
            "up). After aish was down, only the latest missed run fires, late."
        )
    else:
        lines.append(
            f"If it has not started by {task.deadline.strftime('%H:%M %a %Y-%m-%d')}, "
            "aish gives it up and records that (and notifies you, if notifications "
            "are set up)."
        )
    return "\n".join(lines)


# -- changing one: the edit, its delta, and a deletion (#459) -----------------

#: Optional fields an edit may drop by name (`schedule_task`'s `clear`).
#: `roots` has no `schedule_task` argument at all, so an edit keeps it and
#: cannot drop it; `psu_ua` is aish's, never the model's (below).
CLEARABLE = ("from", "until", "allow", "sites", "model", "cwd")

#: What an edit may set, beside the schedule's own fields.
EDITABLE = ("prompt", "note", "model", "cwd", "allow", "sites", "kind", "notify", "enabled")

_SCHEDULE_FIELDS = ("at", "cron", "from", "until")


def edited(old: Task, change: dict[str, Any], clear: Collection[str] = ()) -> dict[str, Any]:
    """The raw task an edit of `old` proposes: the file's own task with ONLY
    the named fields changed, for `validate_task` to judge.

    `enabled` and `created` are the file's unless named, so an edit never
    re-arms a paused task as a side effect. A shape change that would drop a
    window bound the owner approved (`from`/`until` on a cron task turned
    one-shot) is refused unless `clear` names it. `psu_ua` (the owner present,
    delegated) is minted only at creation: an edit keeps a one-shot's and a
    task that becomes recurring loses it, as validate_task requires."""
    unknown = sorted(set(clear) - set(CLEARABLE))
    if unknown:
        raise InvalidTask(
            f"clear cannot drop {', '.join(unknown)}; it drops only {', '.join(CLEARABLE)}"
        )
    both = sorted(set(clear) & set(change))
    if both:
        raise InvalidTask(f"{', '.join(both)} is both set and cleared; do one")
    if "at" in change and "cron" in change:
        raise InvalidTask("pass at (once) or cron (recurring), not both")
    raw = old.to_dict()
    schedule = dict(raw["schedule"])
    if "at" in change:
        dropped = [k for k in ("from", "until") if k in schedule and k not in clear]
        if dropped:
            words = " and ".join(dropped)
            raise InvalidTask(
                f"{old.id} has a {words}, which a one-shot cannot carry, so making it run "
                f"once would drop it; pass clear: {dropped} if that is what the user wants"
            )
        schedule = {"at": change["at"]}
    elif "cron" in change:
        schedule = {k: v for k, v in schedule.items() if k != "at"}
        schedule["cron"] = change["cron"]
    for key in ("from", "until"):
        if key in clear:
            schedule.pop(key, None)
        if key in change:
            schedule[key] = change[key]
    raw["schedule"] = schedule
    for key in EDITABLE:
        if key in change:
            raw[key] = change[key]
    for key in ("model", "cwd", "allow", "sites"):
        if key in clear:
            raw.pop(key, None)
    if "kind" in change and change["kind"] != old.kind and "notify" not in change:
        # The new kind's own default, never the old kind's policy carried over
        # (a plain task refuses every notify but "always").
        raw.pop("notify", None)
    if "at" not in schedule:
        raw.pop("psu_ua", None)
    return raw


def _value_words(value: object) -> str:
    if value in (None, "", ()):
        return "none"
    if isinstance(value, datetime):
        return day_time(value)
    if isinstance(value, tuple):
        return _listed(value)
    return f"`{value}`"


def changes(old: Task, new: Task) -> list[str]:
    """What an edit changes, field by field, computed from the two tasks —
    never from anything the model said about its own call."""
    out: list[str] = []
    fields: tuple[tuple[str, object, object], ...] = (
        ("at", old.at, new.at),
        ("cron", old.cron, new.cron),
        ("from", old.start, new.start),
        ("until", old.until, new.until),
        ("note", old.note, new.note),
        ("kind", old.kind, new.kind),
        ("notify", old.notify, new.notify),
        ("model", old.model, new.model),
        ("cwd", old.cwd, new.cwd),
        ("commands it may run without asking", old.allow, new.allow),
        ("sites it may act on as you", old.sites, new.sites),
    )
    for name, before, after in fields:
        if before != after:
            out.append(f"{name}: {_value_words(before)} → {_value_words(after)}")
    if old.prompt != new.prompt:
        out.append("the instruction it runs: rewritten (the whole new one is below)")
    if old.enabled != new.enabled:
        out.append("paused → resumed" if new.enabled else "resumed → PAUSED")
    if old.psu_ua != new.psu_ua:
        out.append("no longer says you are present to your bank" if not new.psu_ua
                   else "now says you are present to your bank")
    return out


def describe_deletion(task_id: str, task: Task | None, problem: str, now: datetime) -> str:
    """The delete card's meaning. `task` is None for a file that does not
    load, which is said AS that, with its error."""
    if task is None:
        what = (f"Deletes the automation file {task_id}.json, which is not a runnable "
                f"automation ({problem}).")
    else:
        note = f" — {task.note}" if task.note else ""
        what = f"Deletes automation '{task.id}'{note}, which runs {when_words(task, now)}."
    return (
        f"{what} It stops running, and there is no way back short of a new "
        "automation on its own card."
    )
