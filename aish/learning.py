"""Learning suggestions — a lesson offered on a task that went the long way round (#446).

When a task's RECORDED shape says it went roundabout — the loop detector stopped it,
it made far more tool calls than tasks usually do, it repeated calls exactly, the
harness refused its tool calls, or the owner corrected it on the cards — the finished
answer carries one extra quick-reply chip, "Save this as a lesson?", which sends
`/learn`. Nothing else happens: the detector refuses nothing, holds nothing and writes
no knowledge. Whether a lesson is saved is the owner's tap and then the ordinary,
diff-approved `/learn` flow.

Three properties carry the design (`docs/learning-suggestions.md`):

- **Measured, never judged.** The trigger is a pure function of the records this task
  wrote to its own log (`shape_of`), against named thresholds (`LONG_TASK_CALLS` …).
  No model is asked whether the task went well. The SAME function reads a live task
  at its answer and a recorded task in a replay check (`tasks_of` /
  `replay_checks.learning_suggested`), so the two cannot disagree about one log.
- **Every evaluation leaves a record** (`learning`, contract §3.19), tripped or not,
  with its counts and the thresholds in force — the record is also the consultation
  counter. Whether the chip was shown, and if not why (`withheld`), is on it.
- **The chip goes only on an answer to a task a person sent** (typed, tapped, a Retry
  he pressed), never on one aish started itself (a trigger's prompt, restart
  recovery): an automated chat that loops the same way every night would otherwise
  carry the same suggestion every morning. The record is written either way.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .plan import call_key
from .session import _is_superseded, _record_or_none

#: The renderless trace kind this module writes (contract §3.19).
LEARNING_STEP = "learning"

# ── Thresholds — each a named constant, each recorded on every record ─────────
# Measured 2026-10-12 over the owner's 1,692 web tasks (every log under
# ~/.local/state/aish with task_start/task_end brackets; CLI logs have none):
#   tool calls per task  p50 0 · p90 12 · p95 18 · p99 31 · max 103 — 34 tasks ≥ 25
#   exact repeats (K)    p95 1 · p99 5 · max 50                     — 17 tasks ≥ 5
#   harness refusals     p99 1 · max 2                              — 13 tasks ≥ 2
#   card comments        p99 1 · max 3                              —  9 tasks ≥ 2
# Through `tasks_of` itself: 55 of 1,575 answered tasks (3.5%) in 41 of 470
# chats, 5 of them straight after another tripped task. The #436 source task
# ("yes", 41 calls, 10 exact repeats, 2 card comments) trips three of them.
#: A task with at least this many tool calls is long: above the owner's p99 (31)
#: band's lower edge, about one task in fifty.
LONG_TASK_CALLS = 25
#: At least this many calls repeating an earlier call's (tool, arguments) exactly —
#: the repeat nudge's K (`plan.Repeats`, contract §3.16), counted from the records.
REPEATED_CALLS_AT = 5
#: At least this many tool calls the harness REFUSED (a rule gate or the stop
#: gate; contract §3.3, `gate` rows with a `tool`).
REFUSALS_AT = 2
#: At least this many approval cards the owner answered WITH a comment — the
#: sentence that steers or stops the action (contract §3.4 `comment`). One is
#: ordinary steering; two in one task is the owner correcting it.
CORRECTIONS_AT = 2

#: How the agent's loop ends a task it stopped: the `source` `_finish_stopped`
#: stamps on its note (contract §3.17). Any of them trips the detector. aish's
#: own enum values, never page or model text.
STOP_SOURCES = frozenset({"loop_stop", "stall_stop", "step_limit"})

#: The chip, in the quick-reply syntax both surfaces already render: a button on
#: the web, a numbered menu entry in the terminal. Its payload is the typed form
#: of the command, so tapping it is exactly the owner typing `/learn`.
CHIP_LABEL = "Save this as a lesson?"
CHIP_PAYLOAD = "/learn"
CHIP = f"[{CHIP_LABEL}](aish-reply://{CHIP_PAYLOAD})"

# ── Why a tripped task got no chip — a closed vocabulary, checked in this order
#: aish started the task itself: a trigger's prompt, or restart recovery.
NOT_OWNER_SENT = "not_owner_sent"
#: The task WAS `/learn`: a distillation pass is long by nature, and offering a
#: lesson on the lesson would loop.
LEARN_TASK = "learn_task"
#: The CLI ran one task from argv and exits: there is no next prompt to take it.
ONE_SHOT = "one_shot"
#: The chat's previous evaluated task already showed the chip; never twice in a row.
COOLDOWN = "cooldown"
WITHHELD = (NOT_OWNER_SENT, LEARN_TASK, ONE_SHOT, COOLDOWN)

#: `AISH_LEARNING=0` turns the whole mechanism off (no record, no chip).
DISABLE_ENV = "AISH_LEARNING"

SIGNALS = ("stopped", "calls", "repeats", "refusals", "corrections")
THRESHOLDS = {
    "calls": LONG_TASK_CALLS,
    "repeats": REPEATED_CALLS_AT,
    "refusals": REFUSALS_AT,
    "corrections": CORRECTIONS_AT,
}


def disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip() == "0"


@dataclass
class TurnShape:
    """What one task's records say about how it went. Counts only."""

    records: int = 0
    calls: int = 0
    repeats: int = 0
    refusals: int = 0
    corrections: int = 0
    stopped: str | None = None
    _seen: set = field(default_factory=set, repr=False)

    def observe(self, record: dict) -> None:
        self.records += 1
        kind = record.get("kind")
        if kind == "message":
            source = record.get("source")
            if source in STOP_SOURCES:
                self.stopped = str(source)
            return
        if kind != "trace":
            return
        step = record.get("step")
        if not isinstance(step, dict):
            return
        step_kind = step.get("kind")
        if step_kind == "call":
            self.calls += 1
            key = call_key(str(step.get("name") or ""), step.get("args") or {})
            if key in self._seen:
                self.repeats += 1
            self._seen.add(key)
        elif step_kind == "gate":
            # A refused TOOL CALL. The stop gate's arming row is `refused` too,
            # but names no tool: it records the owner's denial, which is
            # counted once, as a correction, from the tool step.
            if step.get("verdict") == "refused" and step.get("at") == "gate" and step.get("tool"):
                self.refusals += 1
        elif step_kind == "tool" and step.get("comment"):
            self.corrections += 1

    def tripped(self) -> list[str]:
        """The signals over their threshold, in `SIGNALS` order."""
        out = ["stopped"] if self.stopped else []
        out += [name for name, at in THRESHOLDS.items() if getattr(self, name) >= at]
        return out

    def counts(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in THRESHOLDS}


def shape_of(records: Iterable[dict]) -> TurnShape:
    shape = TurnShape()
    for record in records:
        if not _is_superseded(record):
            shape.observe(record)
    return shape


def log_size(path: Path | str | None) -> int:
    """Where the next task's records will start: the log's size now (0 when the
    file does not exist yet — a chat writes nothing until its first record)."""
    if path is None:
        return 0
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def read_span(path: Path | str, start: int, end: int | None = None) -> list[dict]:
    """The records between two byte offsets of a log. A torn line is skipped, as
    every reader of the log skips one."""
    with Path(path).open("rb") as handle:
        handle.seek(start)
        data = handle.read() if end is None else handle.read(max(0, end - start))
    out = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        record = _record_or_none(line)
        if record is not None:
            out.append(record)
    return out


def _previous_offer(path: Path | str, before: int) -> bool:
    """Did the chat's most recent evaluated task before byte `before` show the
    chip? Read only when this task trips (about one task in thirty), and live
    records only: a Retry that discarded that task took its chip with it."""
    offered = False
    for record in read_span(path, 0, before):
        if record.get("kind") != "trace" or _is_superseded(record):
            continue
        step = record.get("step")
        if isinstance(step, dict) and step.get("kind") == LEARNING_STEP:
            offered = bool(step.get("chip"))
    return offered


@dataclass(frozen=True)
class Decision:
    record: dict[str, Any]

    @property
    def chip(self) -> bool:
        return bool(self.record.get("chip"))


def evaluate(
    path: Path | str,
    span_start: int,
    *,
    owner_sent: bool,
    learn_task: bool = False,
    one_shot: bool = False,
) -> Decision:
    """The detector at an answer: this task's records → the `learning` record.

    `owner_sent`, `learn_task` and `one_shot` are facts the SURFACE knows about how
    the task was started; nothing here infers them. The record is returned, not
    written, so the caller writes it through its own `logref.step` (the one lock)."""
    shape = shape_of(read_span(path, span_start))
    tripped = shape.tripped()
    record: dict[str, Any] = {
        "kind": LEARNING_STEP,
        "records": shape.records,
        "counts": shape.counts(),
        "stopped": shape.stopped,
        "at": dict(THRESHOLDS),
        "tripped": tripped,
        "chip": False,
    }
    if not tripped:
        return Decision(record)
    if not owner_sent:
        record["withheld"] = NOT_OWNER_SENT
    elif learn_task:
        record["withheld"] = LEARN_TASK
    elif one_shot:
        record["withheld"] = ONE_SHOT
    elif _previous_offer(path, span_start):
        record["withheld"] = COOLDOWN
    else:
        record["chip"] = True
    return Decision(record)


def at_answer(
    path: Path | str | None,
    span_start: int,
    turn: int,
    write: Callable[[dict], None],
    *,
    owner_sent: bool,
    learn_task: bool = False,
    one_shot: bool = False,
) -> bool:
    """Evaluate, write the record, and say whether to show the chip. Never raises:
    a suggestion is never worth a task. A failure to evaluate is itself recorded
    (`unevaluated`), so an absent record never has to stand for it."""
    if disabled() or path is None:
        return False
    try:
        decision = evaluate(
            path, span_start, owner_sent=owner_sent, learn_task=learn_task, one_shot=one_shot
        )
        record = {**decision.record, "turn": turn}
    except Exception as exc:  # noqa: BLE001 — never worth the answer it rides on
        record = {"kind": LEARNING_STEP, "turn": turn, "chip": False,
                  "unevaluated": type(exc).__name__}
    try:
        write(record)
    except Exception:  # noqa: BLE001 — an unwritable log is the log's problem
        return False
    return bool(record.get("chip"))


def is_learn_prompt(text: str) -> bool:
    """Is this task text `/learn`'s expansion? Compared against aish's OWN
    templates (the part before their first placeholder), so every route a
    `/learn` arrives by — typed, tapped, a Retry of one, the CLI — is told
    apart without a flag threaded through each of them."""
    from .agent import LEARN_LESSONS_PROMPT, LEARN_PROMPT

    return text.startswith(
        tuple(template.split("{", 1)[0] for template in (LEARN_PROMPT, LEARN_LESSONS_PROMPT))
    )


def with_chip(text: str) -> str:
    """The answer as the owner is shown it when the chip rides on it. One
    definition, used live and by the cold replay, so the two cannot differ."""
    return f"{text.rstrip()}\n\n{CHIP}"


# ── Reading a recorded log, task by task (the replay check's half) ────────────


@dataclass
class RecordedTask:
    index: int  # 1-based, in log order
    shape: TurnShape
    record: dict | None  # this task's `learning` record, if it wrote one
    #: `task_end`'s status: "ok" for an answered task; None when the task never
    #: ended (the process died in it), "" when the record predates `status`.
    status: str | None

    @property
    def answered(self) -> bool:
        """An answer was delivered, so the detector had something to ride on."""
        return self.status in ("ok", "")


def tasks_of(path: Path | str) -> list[RecordedTask]:
    """Every bracketed task of a log, with its shape and its `learning` record.
    Bracketed means between `task_start` and `task_end` (the web's brackets): a
    CLI log has none and yields no task, which a check reports as such."""
    tasks: list[RecordedTask] = []
    current: list[dict] | None = None

    def close(records: list[dict], status: str | None) -> None:
        learned = [
            r["step"] for r in records
            if r.get("kind") == "trace" and isinstance(r.get("step"), dict)
            and r["step"].get("kind") == LEARNING_STEP
        ]
        tasks.append(RecordedTask(len(tasks) + 1, shape_of(records),
                                  learned[-1] if learned else None, status))

    for record in read_span(path, 0):
        if _is_superseded(record):
            continue
        kind = record.get("kind")
        if kind == "task_start":
            if current is not None:
                close(current, None)
            current = []
        elif kind == "task_end" and current is not None:
            close(current, str(record.get("status") or ""))
            current = None
        elif current is not None:
            current.append(record)
    if current is not None:
        close(current, None)
    return tasks
