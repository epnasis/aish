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

    @property
    def recurring(self) -> bool:
        return bool(self.cron)

    @property
    def monitor(self) -> bool:
        return self.kind == KIND_MONITOR

    @property
    def schedule(self) -> dict[str, str]:
        if self.cron:
            return {"cron": self.cron}
        return {"at": self.one_shot_at().isoformat()}

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
        """When a one-shot is given up. A cron task never is: it has no
        deadline, only a next occurrence."""
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
    as_of: datetime | None = None,
) -> Task:
    """One task from its JSON object, or InvalidTask. Fails CLOSED: an unknown
    key is refused rather than ignored, because a key the owner wrote that aish
    silently drops is a constraint he believes is in force.

    `now` is passed only at CREATION, where a time already in the past is an
    error; a task file loaded later is legitimately in the past. A cron expr
    must have a next occurrence after `now`, else `as_of` (the loader's clock),
    else the wall clock — an expr that can never run is refused, never armed."""
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
    if not isinstance(schedule, dict) or set(schedule) not in ({"at"}, {"cron"}):
        raise InvalidTask(
            'schedule must be exactly one of {"at": "<ISO local datetime>"} (once) '
            'or {"cron": "<minute hour day-of-month month day-of-week>"} (recurring)'
        )
    at: datetime | None = None
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
        cron_next(cron, now or as_of or datetime.now())  # NoOccurrence is an InvalidTask
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
    nothing past is provably a miss, so nothing is due. A cron task never
    expires and has no deadline."""
    if not task.recurring:
        return Due(_one_shot_status(task, fired, now), task.at)
    try:
        occurrence = cron_prev(task.cron, now)
    except NoOccurrence:
        return Due(PENDING, None)
    if task.key_for(occurrence) in fired:
        return Due(FIRED, occurrence)
    if not task.enabled:
        return Due(DISABLED, occurrence)
    if since is None or occurrence < since:
        return Due(PENDING, occurrence)
    previous = last_fired(task.id, fired)
    if previous is not None and occurrence <= previous:
        # The clock went back (the fall-back hour): this occurrence's key may
        # have been pruned, but a later one fired, so it is not a miss.
        return Due(PENDING, occurrence)
    return Due(DUE, occurrence)


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
    """The occurrences in [since, occurrence) that are also later than `after`
    (the last one that fired or was already recorded as skipped). Bounded: the
    scan starts at the floor, never further back."""
    start = since - timedelta(microseconds=1)
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
        return f"on the schedule `{task.cron}`"
    return f"ONCE, at {_when(task.one_shot_at(), now)}"


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
    if task.recurring:
        first = cron_next(task.cron, now)
        upcoming = (first, cron_next(task.cron, first))
        when = (
            f"Runs {when_words(task, now)} — next: "
            + ", ".join(o.strftime("%a %Y-%m-%d %H:%M") for o in upcoming)
            + " — each time in a new chat with nobody watching."
        )
    else:
        when = f"Runs {when_words(task, now)}, in a new chat with nobody watching."
    lines = [
        f"{verb} '{task.id}'" + (f" — {task.note}" if task.note else ""),
        when,
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
