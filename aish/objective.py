"""The Objective — what a chat is FOR, as a ledger of goals and tasks (#423, #424).

aish protects only the latest task prompt. The goal of a long chat is spread over
many owner messages, in his language, and is never stated as one thing — so
anchoring on the first message loses it and anchoring on the latest loses it too.
This module keeps a separate record of it: goals with states, tasks under them,
every state a claim that cites the log.

Three parts, and only the second involves a model:

- **The material.** The owner's own texts, final answers, and the records of what
  ran, was denied, was stopped or failed — read from the session log and given a
  stable ref each. Never tool outputs, never reminders (`material`).
- **The distiller.** An isolated role (`docs/roles.md`, charter
  `aish/charters/distiller.md`) that turns the previous revision plus the new
  material into the next revision. Its answer is validated HERE, in code, before
  anything records it (`validate_answer`): cites resolve, `done` has evidence,
  constraints are verbatim, transitions are legal, owner-set fields are untouched.
- **The extractive floor.** No model: the owner's uncovered texts, verbatim,
  filtered by length, the synthetic-note prefix and exact duplicates only
  (`floor`). It is what stands when the distiller cannot.

`covers_to_turn` is computed here and never taken from the model: it is the last
turn up to which every owner text the floor would keep is cited by the revision.

Written at task end, off the interactive path (`distill_at_boundary`), as a
renderless `objective` record. Recorded only in this slice: nothing reads it back
into the model or the screen yet. `docs/objective.md`, contract §3.14.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import roles

DISTILLER = "distiller"

GOAL_STATES = ("active", "parked", "done", "dropped", "unknown")
TASK_STATES = ("pending", "in_progress", "done", "stopped", "superseded", "unknown")
CHANGES = ("new", "unchanged", "refined", "expanded", "pivoted", "unknown")

ORIGIN_DISTILLER = "distiller"
ORIGIN_EXTRACTIVE = "extractive"
ORIGIN_OWNER = "owner"
# Revisions that ACCOUNT for owner texts. An extractive revision copies them and
# accounts for nothing, so the distiller is never handed one as its base.
BASE_ORIGINS = frozenset({ORIGIN_DISTILLER, ORIGIN_OWNER})

# Material kinds.
OWNER = "owner"  # a message he typed
COMMENT = "comment"  # the sentence he typed on an approval card he denied or held
DENIAL = "denial"  # an action he denied or held, with no sentence
ANSWER = "answer"  # a final answer
ACTION = "action"  # a state-changing tool call that ran and succeeded
CANCEL = "cancel"  # a task he stopped
FAILED = "failed"  # a task that ended in failure
RAN = "ran"  # a `!` command he ran himself, recorded exit code 0
RAN_UNCHECKED = "ran_unchecked"  # one he ran whose exit code is not 0 or not recorded

OWNER_WORDS = frozenset({OWNER, COMMENT})  # text the owner authored himself
EVIDENCE = frozenset({ANSWER, ACTION, RAN})  # what `done` may cite
# What `stopped`/`dropped` may cite: his words, and his acts.
OWNER_ACTS = frozenset({OWNER, COMMENT, DENIAL, CANCEL, RAN, RAN_UNCHECKED})
ASKED = frozenset({OWNER, COMMENT, ANSWER})  # where a task was asked for or planned

# The floor's mechanical filter. A length threshold, the synthetic-note prefix
# and exact duplicates — and deliberately NO word list: which short replies are
# "noise" is a judgement, and a list of them is a vocabulary nobody measured.
# 12 keeps a two-word request naming a provider (17 chars in the #422 chat) and
# drops the one-word replies ("tak", "Continue") the epic names.
FLOOR_MIN_CHARS = 12
SYNTHETIC_PREFIX = "["

# What the distiller is SHOWN of each item. The owner's words travel nearly
# whole, since a constraint must be quoted from them; an answer is cut, since it
# is cited as evidence and never quoted.
OWNER_CHARS = 4000
# Answers travel nearly whole since the distiller was given thinking and the
# whole chat (owner decision, #424); the cap only bounds a runaway answer.
ANSWER_CHARS = 8000
ACT_CHARS = 200
# An action's arguments, from its `call` record: enough to see what a command or
# a write was. Tools whose body IS the deliverable — a skill — get far more, so
# whether it is self-contained can be judged at all.
ACTION_ARGS_CHARS = 600
SKILL_ARGS_CHARS = 6000
WHOLE_ARGS_TOOLS = frozenset({"create_skill", "create_tool"})

# `session.STOPPED_ANSWER`, spelled here rather than imported so this module
# stays importable without the session layer (a pinned test holds them equal).
STOPPED_ANSWER = "(task stopped by user — any partial work is above)"

_ID = re.compile(r"[A-Za-z0-9_.-]{1,24}")


# ---------------------------------------------------------------- the material


@dataclass(frozen=True)
class Item:
    """One citable thing. `text` is what the distiller is shown; for owner
    texts it is also what a constraint is checked against."""

    ref: str
    kind: str
    turn: int
    text: str

    def as_json(self) -> dict[str, Any]:
        return {"ref": self.ref, "kind": self.kind, "turn": self.turn, "text": self.text}


def _cut(text: str, cap: int) -> str:
    text = text.strip()
    if len(text) <= cap:
        return text
    return text[:cap] + f" [… cut here; {len(text)} chars in all]"


def read_records(path: Path, upto: int | None = None) -> list[dict]:
    """The log's parseable records, in file order, up to byte `upto`.

    `upto` is the size the file had when the task ended, so a distill that runs
    late — while the NEXT task is writing — reads exactly the chat as it stood
    at its own boundary. A torn line is skipped, as every reader of the log does.
    """
    with path.open("rb") as handle:
        raw = handle.read() if upto is None else handle.read(upto)
    out: list[dict] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _step(record: dict) -> dict:
    step = record.get("step") if record.get("kind") == "trace" else None
    return step if isinstance(step, dict) else {}


def _read_only_tools() -> frozenset[str]:
    """Tools whose success changes nothing — never done-evidence.

    The native set has exactly one definition, beside the loop that dispatches
    (imported lazily, and only here). Plugin tools declare it in their own
    manifest (`mutating`), and a read-only plugin — a mail search — must not
    count as an action either; they are read from the installed plugins, which
    is the set the agent itself classifies by (`Agent._is_readonly_plugin`).
    """
    from .agent import READ_ONLY_TOOLS

    names = set(READ_ONLY_TOOLS)
    try:
        from . import tool_plugins

        found, _problems = tool_plugins.discover(os.getcwd())
        names |= {t.name for t in found if not t.mutating}
    except Exception:  # noqa: BLE001 — no plugins readable: the native set stands
        pass
    return frozenset(names)


_OWN_KINDS = frozenset({"objective", "role"})


def _opens_task(record: dict, bracketed: bool) -> bool:
    if bracketed:
        return record.get("kind") == "task_start"
    # A log with no task brackets (the CLI writes none): a typed owner message
    # that is a model call's first input opens the task.
    return (
        record.get("kind") == "message"
        and record.get("role") == "user"
        and record.get("model_call", 0) == 0
        and not str(record.get("content") or "").lstrip().startswith(SYNTHETIC_PREFIX)
    )


def material(records: Iterable[dict], read_only: frozenset[str] | None = None) -> list[Item]:
    """Every citable item in the log's LIVE records, each stamped with its task's
    turn (contract §2).

    A task's turn is the first integer `turn` stamped on a trace record inside
    it — the join key every governance record already uses — and one more than
    the previous task's where nothing inside it was stamped. In a log with no
    stamps, records before the first task (a `model` line) form a group of
    their own, so its first task is turn 2 — kept, because the benchmark
    goldens are numbered that way and a turn number is only a join key.
    Superseded records
    are skipped (L7): a discarded Retry attempt is not something the owner's
    goal can cite.
    """
    read_only = _read_only_tools() if read_only is None else read_only
    live = [(i, r) for i, r in enumerate(records) if not r.get("superseded")]
    bracketed = any(r.get("kind") == "task_start" for _, r in live)

    tasks: list[list[tuple[int, dict]]] = []
    for index, record in live:
        if _opens_task(record, bracketed) or not tasks:
            tasks.append([])
        tasks[-1].append((index, record))

    items: list[Item] = []
    previous = 0
    for task in tasks:
        stamped = [
            stamp
            for _, r in task
            # A distill's own records are stamped with the turn it DISTILLED,
            # and a slow one lands inside the next task — so they say nothing
            # about which turn the task they sit in is.
            if _step(r).get("kind") not in _OWN_KINDS
            and isinstance(stamp := _step(r).get("turn"), int)
            and not isinstance(stamp, bool)
        ]
        turn = stamped[0] if stamped else previous + 1
        previous = turn
        items.extend(_task_items(task, turn, read_only))
    return items


def _content_ref(prefix: str, *parts: Any) -> str:
    """A ref for a record with no id of its own: a digest of what it says and
    when. Stable across a Retry or a redaction rewriting the file, unlike a
    line position."""
    blob = "\x1f".join(str(p) for p in parts)
    return f"{prefix}#{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


def _message_ref(index: int, record: dict) -> str:
    ident = str(record.get("turn") or "")
    if ident:
        return f"m:{ident}"
    return _content_ref("m", record.get("ts"), record.get("role"), record.get("content"))


FEEDBACK = " (feedback: "


def _feedback(decision: str) -> tuple[str, str]:
    """An audit `command` record's decision split into the verdict and the
    owner's card comment — written whole there as `<verdict> (feedback: …)`
    by the server, where the tool step keeps only `COMMENT_CHARS` of it."""
    verdict, sep, rest = decision.partition(FEEDBACK)
    if not sep:
        return decision, ""
    return verdict, rest[:-1] if rest.endswith(")") else rest


def _whole_comment(comment: str, feedbacks: list[str]) -> str:
    """The owner's full sentence, when the tool step holds a capped copy and an
    audit record of the same task holds it whole."""
    head = _squash(comment)
    for text in feedbacks:
        if head and _squash(text).startswith(head) and len(text) > len(comment):
            return text
    return comment


def _args_text(name: str, args: Any) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    cap = SKILL_ARGS_CHARS if name in WHOLE_ARGS_TOOLS else ACTION_ARGS_CHARS
    return _cut(json.dumps(args, ensure_ascii=False), cap)


def _task_items(task: list[tuple[int, dict]], turn: int, read_only: frozenset[str]) -> list[Item]:
    items: list[Item] = []
    answer: tuple[int, dict] | None = None
    records = [r for _, r in task]
    has_calls = any(
        _step(r).get("kind") == "tool" and isinstance(_step(r).get("call"), int) for r in records
    )
    feedbacks = [
        _feedback(str(r.get("decision") or ""))[1]
        for r in records
        if r.get("kind") == "command"
    ]
    calls = {
        (_step(r).get("turn"), _step(r).get("call")): _step(r).get("args")
        for r in records
        if _step(r).get("kind") == "call"
    }
    for position, (index, record) in enumerate(task):
        kind = record.get("kind")
        if kind == "message":
            role = record.get("role")
            content = str(record.get("content") or "")
            if role == "user" and content.strip():
                # L4: a synthetic turn is classified lexically. aish's own
                # notes and reminders are never the owner's words.
                if content.lstrip().startswith(SYNTHETIC_PREFIX):
                    continue
                items.append(
                    Item(_message_ref(index, record), OWNER, turn, _cut(content, OWNER_CHARS))
                )
            elif role == "assistant" and not record.get("interim") and content.strip():
                answer = (index, record)
            continue
        if kind == "task_end" and record.get("status") not in (None, "ok"):
            error = str(record.get("error") or record.get("status") or "")
            items.append(Item(f"t{turn}.end", FAILED, turn, _cut(error, ACT_CHARS)))
            continue
        if kind == "command":
            items.extend(_command_items(task[position:], record, turn, has_calls))
            continue
        step = _step(record)
        if step.get("kind") != "tool" or not isinstance(step.get("call"), int):
            continue
        ref = f"t{step.get('turn', turn)}.c{step['call']}"
        name = str(step.get("name") or "")
        what = f"{name}: {step.get('command') or step.get('summary') or ''}".strip()
        decision = step.get("decision")
        if decision in ("denied", "held"):
            comment = str(step.get("comment") or "").strip()
            if comment:
                whole = _whole_comment(comment, feedbacks)
                items.append(Item(ref, COMMENT, turn, _cut(whole, OWNER_CHARS)))
            else:
                items.append(Item(ref, DENIAL, turn, _cut(f"{decision} — {what}", ACT_CHARS)))
        elif step.get("ok") is True and name not in read_only:
            args = _args_text(name, calls.get((step.get("turn", turn), step["call"])))
            text = _cut(what, ACT_CHARS) + (f"\nargs: {args}" if args else "")
            items.append(Item(ref, ACTION, turn, text))
    if answer is not None:
        index, record = answer
        content = str(record.get("content") or "").strip()
        kind = CANCEL if content == STOPPED_ANSWER else ANSWER
        items.append(Item(_message_ref(index, record), kind, turn, _cut(content, ANSWER_CHARS)))
    return items


def _command_items(
    rest: list[tuple[int, dict]], record: dict, turn: int, has_calls: bool
) -> list[Item]:
    """What an audit `command` record says about the owner — the one record
    every era of the log has.

    - `user-direct`: a `!` command HE ran. Its exit code comes from the next
      `cmd_end`; it is evidence only when that code is recorded and 0.
    - a denial or a held approval, with or without his comment: taken from here
      only in a task whose tool steps carry no call id (logs from before
      contract §2), where no tool step can be joined to it. Where they can, the
      tool step is the item and this record only lends it the whole comment.
    - an approved gated action: likewise only in such a task, where it is the
      one record that says an action was allowed to run. Auto-approved
      read-only commands are not actions.
    """
    command = str(record.get("command") or "")
    decision = str(record.get("decision") or "")
    ref = _content_ref("c", record.get("ts"), command, decision)
    if decision == "user-direct":
        exit_code = next(
            (r.get("exit_code") for _, r in rest[1:] if r.get("kind") == "cmd_end"), None
        )
        output = next(
            (
                str(r.get("content") or "").partition("\n")[2]
                for _, r in rest[1:]
                if r.get("kind") == "message" and r.get("role") == "user"
                and str(r.get("content") or "").startswith("[I ran `")
            ),
            "",
        )
        kind = RAN if exit_code == 0 else RAN_UNCHECKED
        text = f"he ran: {command}" + (f"\n{output.strip()}" if output.strip() else "")
        return [Item(ref, kind, turn, _cut(text, ACT_CHARS * 2))]
    if has_calls:
        return []
    verdict, comment = _feedback(decision)
    if comment and (verdict.startswith("denied") or verdict.startswith("approved")):
        return [Item(ref, COMMENT, turn, _cut(comment, OWNER_CHARS))]
    if verdict.startswith("denied"):
        return [Item(ref, DENIAL, turn, _cut(f"denied — {command}", ACT_CHARS))]
    if verdict.startswith("approved"):
        return [Item(ref, ACTION, turn, _cut(command, ACTION_ARGS_CHARS))]
    return []


# ---------------------------------------------------------------- the floor


def floor_keeps(item: Item) -> bool:
    """Would the extractive floor copy this item? Mechanical only."""
    if item.kind not in OWNER_WORDS:
        return False
    text = item.text.strip()
    return len(text) >= FLOOR_MIN_CHARS and not text.startswith(SYNTHETIC_PREFIX)


def floor(items: Iterable[Item], after_turn: int, upto_turn: int) -> list[Item]:
    """The owner's texts in (`after_turn`, `upto_turn`], verbatim, minus the
    short ones, the synthetic notes and exact duplicates of an earlier one."""
    kept: list[Item] = []
    seen: set[str] = set()
    for item in items:
        if not (after_turn < item.turn <= upto_turn) or not floor_keeps(item):
            continue
        text = item.text.strip()
        if text in seen:
            continue
        seen.add(text)
        kept.append(item)
    return kept


# ---------------------------------------------------------------- revisions


@dataclass
class Revision:
    """A validated Objective: the value a distiller call returns, and the body
    of an `objective` record."""

    goals: list[dict[str, Any]]
    change: str = "unknown"
    covers_to_turn: int = 0
    not_goal_bearing: list[str] = field(default_factory=list)
    # Every `done` the model claimed without evidence, turned into `unknown`
    # by code rather than rejecting the whole revision (owner decision, #424).
    downgrades: list[dict[str, str]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "change": self.change,
            "goals": self.goals,
            "covers_to_turn": self.covers_to_turn,
            "not_goal_bearing": list(self.not_goal_bearing),
            "downgrades": list(self.downgrades),
        }


def objective_records(records: Iterable[dict]) -> list[dict]:
    """Every `objective` step in the log, superseded ones included."""
    return [_step(r) for r in records if _step(r).get("kind") == "objective"]


def current(records: Iterable[dict], origins: frozenset[str] | None = None) -> dict | None:
    """The newest LIVE revision — of the given origins, when given (L7)."""
    found = None
    for record in records:
        step = _step(record)
        if step.get("kind") != "objective" or record.get("superseded"):
            continue
        if origins is None or step.get("origin") in origins:
            found = step
    return found


def next_revision(records: Iterable[dict]) -> int:
    """1 + the highest revision in the file. Superseded ones count: an id that
    was handed out is never reissued (the `last_turn` rule)."""
    numbers = [int(s.get("revision") or 0) for s in objective_records(records)]
    return max(numbers, default=0) + 1


def _bare_ref(cite: Any) -> str:
    return str(cite.get("ref") or "") if isinstance(cite, dict) else str(cite or "")


def _owner_set(item: dict) -> list[str]:
    return [f for f in ("state", "text") if item.get(f"{f}_by") == ORIGIN_OWNER]


def _quote_view(quote: Any) -> Any:
    if isinstance(quote, dict):
        cites = [_bare_ref(c) for c in quote.get("cites") or ()]
        return {"text": quote.get("text"), "cites": cites}
    return UNSTATED


def _model_view(goals: list[dict]) -> list[dict]:
    """A revision as the distiller is shown it: cites as bare refs, and who set
    each field, so it knows which ones it may not touch."""
    out = []
    for goal in goals:
        out.append(
            {
                "id": goal.get("id"),
                "text": goal.get("text"),
                "state": goal.get("state"),
                **({"was": goal["was"]} if goal.get("was") else {}),
                "owner_set": _owner_set(goal),
                "why": _quote_view(goal.get("why")),
                "done_when": _quote_view(goal.get("done_when")),
                "cites": [_bare_ref(c) for c in goal.get("cites") or ()],
                "constraints": [
                    {"text": c.get("text"), "cites": [_bare_ref(x) for x in c.get("cites") or ()]}
                    for c in goal.get("constraints") or ()
                ],
                "tasks": [
                    {
                        "id": t.get("id"),
                        "text": t.get("text"),
                        "state": t.get("state"),
                        **({"was": t["was"]} if t.get("was") else {}),
                        "owner_set": _owner_set(t),
                        "cites": [_bare_ref(c) for c in t.get("cites") or ()],
                        **({"replaced_by": t["replaced_by"]} if t.get("replaced_by") else {}),
                    }
                    for t in goal.get("tasks") or ()
                ],
            }
        )
    return out


# ---------------------------------------------------------------- the input


def compose_input(
    chat: str, boundary: int, items: list[Item], base: dict | None
) -> str:
    """The distiller's ONE input: ALL of the chat's material up to the boundary,
    and the previous revision.

    Rebuilt from everything at every boundary (owner decision, #424) rather
    than previous revision + delta: a ledger built from deltas inherits every
    omission of every earlier revision, and the measured one never recovered
    the purpose it missed at the first boundary. The previous revision is here
    only so the model keeps ids and can say how this one differs; code uses it
    for `change`, owner-set fields, `was` and quoted purposes.

    JSON, because the validator reads it back: what a cite is checked against
    is exactly what the model was shown, in production and in the exam alike.
    """
    material_items = [i for i in items if i.turn <= boundary]
    payload = {
        "chat": chat,
        "boundary_turn": boundary,
        "previous": (
            {
                "revision": base.get("revision"),
                "turn": int(base.get("turn") or base.get("covers_to_turn") or 0),
                "goals": _model_view(base.get("goals") or []),
            }
            if base
            else None
        ),
        "material": [i.as_json() for i in material_items],
        # The owner's texts the answer must account for: cite each, or list it
        # under `not_goal_bearing`. Named outright because, asked only in prose,
        # the local model left his stated purpose uncited (measured, #424).
        "must_cover": [i.ref for i in floor(material_items, 0, boundary)],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


@dataclass
class Context:
    """What a distiller answer is validated against — read back from its input."""

    chat: str
    boundary: int
    previous_turn: int
    previous: list[dict]
    ledger: dict[str, Item]
    must_cover: list[str]
    first: bool = False  # no previous revision at all

    def fresh(self) -> set[str]:
        """Refs of what happened after the previous revision was taken."""
        return {ref for ref, i in self.ledger.items() if i.turn > self.previous_turn}


def parse_input(text: str) -> Context:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"the distiller's input is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the distiller's input is not a JSON object")
    previous = raw.get("previous") or {}

    def item(entry: dict) -> Item:
        return Item(
            str(entry.get("ref") or ""),
            str(entry.get("kind") or ""),
            int(entry.get("turn") or 0),
            str(entry.get("text") or ""),
        )

    material_items = [item(e) for e in raw.get("material") or () if isinstance(e, dict)]
    return Context(
        chat=str(raw.get("chat") or ""),
        boundary=int(raw.get("boundary_turn") or 0),
        previous_turn=int(previous.get("turn") or 0),
        previous=[_from_model_view(g) for g in previous.get("goals") or ()],
        ledger={i.ref: i for i in material_items},
        must_cover=[str(r) for r in raw.get("must_cover") or ()],
        first=raw.get("previous") is None,
    )


def _from_model_view(goal: dict) -> dict:
    """The model view back into record form, for the transition checks. Who set
    each field survives as `owner_set`, which is all the checks need."""

    def by(item: dict) -> dict:
        owned = set(item.get("owner_set") or ())
        return {
            f"{f}_by": ORIGIN_OWNER if f in owned else ORIGIN_DISTILLER for f in ("state", "text")
        }

    return {
        **goal,
        **by(goal),
        "tasks": [{**t, **by(t)} for t in goal.get("tasks") or ()],
    }


# ---------------------------------------------------------------- validation


# A goal's `why` or `done_when` the owner never stated. The one abstention the
# quote fields have (R4): a field that must be a quote and cannot say "he never
# said" makes quoting something beside the point the cheapest answer. Counted
# on the record, and scored by the measurement.
UNSTATED = "unstated"


def _squash(text: str) -> str:
    return " ".join(text.split())


def _cites(raw: Any, where: str, ctx: Context, cap: int) -> list[Item]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{where}: cites must be a list of refs")
    if len(raw) > cap:
        raise ValueError(f"{where}: at most {cap} cites")
    out = []
    for cite in raw:
        ref = _bare_ref(cite).strip()
        found = ctx.ledger.get(ref)
        if found is None:
            raise ValueError(
                f"{where}: cite {ref!r} names nothing in the material you were given — "
                "copy a \"ref\" exactly from an item under \"material\""
            )
        out.append(found)
    return out


def _has(cited: list[Item], kinds: frozenset[str]) -> bool:
    return any(c.kind in kinds for c in cited)


def _require(cited: list[Item], kinds: frozenset[str], where: str, what: str) -> None:
    if not _has(cited, kinds):
        raise ValueError(f"{where}: {what} (kinds {', '.join(sorted(kinds))})")


# Transitions a distiller may make without an owner cite, from the base's state.
# Everything may become `unknown`; reopening a closed item needs his word.
_GOAL_REOPEN = {"done", "dropped"}
_TASK_REOPEN = {"done", "stopped"}
_TASK_TERMINAL = {"superseded"}


def _last_known(before: dict | None) -> str | None:
    """The state an item last had that was not `unknown` — its own, or the one
    it carried in `was` when it became unknown."""
    if before is None:
        return None
    state = before.get("state")
    if state != "unknown":
        return state
    return str(before.get("was") or "") or None


def _check_transition(
    before: dict | None, after: dict, where: str, cited: list[Item], closed: set[str],
    terminal: set[str], fresh: set[str],
) -> None:
    """Legal moves from the previous revision's state. Reopening a closed item
    must rest on something the owner did SINCE that revision — a cite of the
    message he asked in, which it already had, justifies nothing new.

    `unknown` is always legal to ENTER, and leaving it is judged from the state
    before it (`was`): otherwise done → unknown → in_progress, two revisions,
    would reopen what one revision may not."""
    if before is None:
        return
    old, new = _last_known(before), after.get("state")
    for field_name, by in (("state", "state_by"), ("text", "text_by")):
        if before.get(by) == ORIGIN_OWNER and before.get(field_name) != after.get(field_name):
            raise ValueError(
                f"{where}: {field_name} was set by the owner and may not be changed "
                f"(it is {before.get(field_name)!r})"
            )
    if old is None or old == new or new == "unknown":
        return
    if old in terminal:
        raise ValueError(f"{where}: a {old} item may only become unknown")
    if old in closed:
        _require(
            [c for c in cited if c.ref in fresh],
            OWNER_ACTS,
            where,
            f"reopening a {old} item needs an owner cite from after the previous revision",
        )


def _quote(raw: Any, where: str, ctx: Context, cap: int, cite_cap: int) -> tuple[Any, list[Item]]:
    """A `why` / `done_when`: `"unstated"`, or `{text, cites}` whose text is a
    verbatim quote of an owner text it cites."""
    if isinstance(raw, str) and raw.strip().lower() == UNSTATED:
        return UNSTATED, []
    if not isinstance(raw, dict):
        raise ValueError(
            f'{where}: must be {{"text": <his exact words>, "cites": [<ref>]}} or "{UNSTATED}"'
        )
    text = roles.capped(str(raw.get("text") or ""), cap)
    if not text:
        raise ValueError(f"{where}: text is required, or say \"{UNSTATED}\"")
    cited = _cites(raw.get("cites"), where, ctx, cite_cap)
    if not any(c.kind in OWNER_WORDS and _squash(text) in _squash(c.text) for c in cited):
        raise ValueError(
            f"{where}: {text!r} is not a verbatim quote from the owner text it cites — "
            "copy the words exactly as he wrote them, in his language"
        )
    return {"text": text, "cites": [c.ref for c in cited]}, cited


def _check_sticky(
    before: dict | None, field_name: str, quote: Any, cited: list[Item], where: str,
    ctx: Context,
) -> None:
    """Once his purpose (`why`) or finish line (`done_when`) has been quoted,
    only the owner may remove or replace it: the new text must be QUOTED FROM
    something he wrote after the words it replaces — a later message merely
    sitting among the cites does not count, or padding the cites would launder
    any replacement.

    The old quote's turn is its cites' latest turn in the material; a cite the
    material no longer holds (its message was discarded by a Retry) counts as
    the previous revision's own turn, so the rule never falls back to "any
    turn at all"."""
    old = (before or {}).get(field_name)
    if not isinstance(old, dict):
        return
    if quote == UNSTATED:
        raise ValueError(
            f"{where}: {field_name} was quoted before ({old.get('text')!r}); keep it — "
            "only the owner can take it back"
        )
    if _squash(str(quote["text"])) == _squash(str(old.get("text") or "")):
        return
    refs = [_bare_ref(c) for c in old.get("cites") or ()]
    old_turn = max(
        (ctx.ledger[r].turn if r in ctx.ledger else ctx.previous_turn for r in refs),
        default=ctx.previous_turn,
    )
    sources = [
        c for c in cited
        if c.kind in OWNER_WORDS and _squash(str(quote["text"])) in _squash(c.text)
    ]
    if not any(c.turn > old_turn for c in sources):
        raise ValueError(
            f"{where}: {field_name} replaces {old.get('text')!r}; the new words must be "
            "quoted from something he wrote at a later turn than those — otherwise keep "
            f"the earlier {field_name}"
        )


def validate_answer(shape: roles.Shape, payload: Any, inputs: dict[str, str]) -> Revision:
    """A distiller answer as a `Revision`, or `ValueError` with a message the
    model can act on (it is fed back on the one corrective retry)."""
    ctx = parse_input(inputs.get("material", ""))
    caps = shape.caps
    text_cap = caps.get("max_chars", 200)
    quote_cap = caps.get("max_constraint_chars", 300)
    cite_cap = caps.get("max_cites", 8)
    if not isinstance(payload, dict):
        raise ValueError("the reply must be a JSON object")
    change = str(payload.get("change") or "").strip().lower()
    if change not in CHANGES:
        raise ValueError(f"change must be one of {', '.join(CHANGES)} (got {change!r})")
    if ctx.first:
        # Not the model's to say: with no previous revision the change IS new,
        # and code knows it — the same reason covers_to_turn is computed.
        change = "new"
    elif change == "new":
        raise ValueError('change is "new" only for the first revision; there is a previous one')
    raw_goals = payload.get("goals")
    if not isinstance(raw_goals, list):
        raise ValueError("the reply must have a 'goals' array")
    if len(raw_goals) > caps.get("max_goals", 12):
        raise ValueError(f"at most {caps.get('max_goals', 12)} goals")

    prev_goals = {str(g.get("id")): g for g in ctx.previous}
    prev_tasks = {
        str(t.get("id")): t for g in ctx.previous for t in g.get("tasks") or ()
    }
    fresh = ctx.fresh()
    goal_ids: set[str] = set()
    task_ids: set[str] = set()
    goals: list[dict[str, Any]] = []
    cited_refs: set[str] = set()
    downgrades: list[dict[str, str]] = []

    def wrap(items: list[Item]) -> list[dict[str, str]]:
        cited_refs.update(i.ref for i in items)
        return [{"session": ctx.chat, "ref": i.ref} for i in items]

    for n, raw in enumerate(raw_goals, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"goal {n} must be an object")
        gid = str(raw.get("id") or "").strip()
        where = f"goal {gid or n}"
        if not _ID.fullmatch(gid):
            raise ValueError(f"{where}: id must be 1-24 letters, digits, '.', '_' or '-'")
        if gid in goal_ids:
            raise ValueError(f"{where}: two goals share the id {gid!r}")
        goal_ids.add(gid)
        text = roles.capped(str(raw.get("text") or ""), text_cap)
        if not text:
            raise ValueError(f"{where}: text is required")
        state = str(raw.get("state") or "").strip().lower()
        if state not in GOAL_STATES:
            raise ValueError(f"{where}: state must be one of {', '.join(GOAL_STATES)}")
        cited = _cites(raw.get("cites"), where, ctx, cite_cap)
        if state != "unknown":
            _require(cited, OWNER_WORDS, where, "a goal must cite where the owner asked for it")
        before = prev_goals.get(gid)
        if state == "done" and not _has(cited, EVIDENCE) and not _owner_held(before, state):
            state = "unknown"
            downgrades.append({"goal": gid, "from": "done", "why": "no answer or action cited"})
        if state == "dropped" and (before is None or before.get("state") != "dropped"):
            _check_dropped(cited, where, fresh)
        why, why_cited = _quote(raw.get("why"), f"{where}, why", ctx, quote_cap, cite_cap)
        _check_sticky(before, "why", why, why_cited, where, ctx)
        done_when, when_cited = _quote(
            raw.get("done_when"), f"{where}, done_when", ctx, quote_cap, cite_cap
        )
        _check_sticky(before, "done_when", done_when, when_cited, where, ctx)
        record: dict[str, Any] = {"id": gid, "text": text, "state": state}
        _check_transition(before, record, where, cited, _GOAL_REOPEN, set(), fresh)
        _stamp_was(record, before)
        record["state_by"] = _by(before, "state", state)
        record["text_by"] = _by(before, "text", text)
        record["cites"] = wrap(cited)
        record["why"] = _wrap_quote(why, why_cited, wrap)
        record["done_when"] = _wrap_quote(done_when, when_cited, wrap)
        record["constraints"] = _constraints(raw.get("constraints"), where, ctx, before, wrap, caps)
        record["tasks"] = _tasks(
            raw.get("tasks"), where, ctx, prev_tasks, task_ids, wrap, caps, fresh, downgrades
        )
        goals.append(record)

    # A goal the previous revision had and the answer left out is carried
    # forward whole — a pivot never overwrites (D3), and a quoted purpose is
    # never lost by omission. Code does it, so a model that forgets a parked
    # goal cannot delete it. Tasks are REBUILT (all the material is in front of
    # the model), except the ones the owner set, which are his.
    for gid, before in prev_goals.items():
        if gid not in goal_ids:
            goals.append(_restore(before, ctx, task_ids))
            goal_ids.add(gid)
    for goal in goals:
        prev_goal = prev_goals.get(str(goal["id"]))
        for task in (prev_goal or {}).get("tasks") or ():
            tid = str(task.get("id"))
            if tid not in task_ids and _owner_set(task):
                goal["tasks"].append(_restore_task(task, ctx))
                task_ids.add(tid)
                # …and the task that replaced it, which the model had no
                # reason to keep: a carried `replaced_by` must resolve, or the
                # answer is rejected over state it never emitted.
                replacement = str(task.get("replaced_by") or "")
                while replacement and replacement not in task_ids and replacement in prev_tasks:
                    carried = prev_tasks[replacement]
                    goal["tasks"].append(_restore_task(carried, ctx))
                    task_ids.add(replacement)
                    replacement = str(carried.get("replaced_by") or "")
    for goal in goals:
        for task in goal["tasks"]:
            if task["state"] == "superseded" and task.get("replaced_by") not in task_ids:
                raise ValueError(
                    f"task {task['id']}: replaced_by must name another task of this revision"
                )
    if sum(1 for g in goals if g["state"] == "active") > 1:
        raise ValueError("at most one goal may be active; park the others")

    not_goal_bearing = [
        r for r in dict.fromkeys(_not_goal_bearing(payload.get("not_goal_bearing"), ctx))
        if r not in cited_refs  # cited is the stronger claim; it wins
    ]
    _coverage(ctx, cited_refs, set(not_goal_bearing))
    return Revision(
        goals=goals,
        change=change,
        covers_to_turn=ctx.boundary,
        not_goal_bearing=not_goal_bearing,
        downgrades=downgrades,
    )


def _owner_held(before: dict | None, state: str) -> bool:
    """The owner set this item's state to exactly this — his claim, not the
    model's, so code never downgrades it."""
    return before is not None and before.get("state_by") == ORIGIN_OWNER and (
        before.get("state") == state
    )


def _wrap_quote(quote: Any, cited: list[Item], wrap) -> Any:
    if quote == UNSTATED:
        return UNSTATED
    return {"text": quote["text"], "cites": wrap(cited)}


def _not_goal_bearing(raw: Any, ctx: Context) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("not_goal_bearing must be a list of refs")
    allowed = set(ctx.must_cover)
    out = []
    for entry in raw:
        ref = _bare_ref(entry).strip()
        if ref not in allowed:
            raise ValueError(
                f"not_goal_bearing: {ref!r} is not one of the refs under \"must_cover\""
            )
        out.append(ref)
    return out


def _coverage(ctx: Context, cited: set[str], dismissed: set[str]) -> None:
    """Every owner text the floor keeps must be cited or explicitly listed as
    not goal-bearing (owner decision, #424). Anything else is sent back on the
    corrective retry, naming the refs — `covers_to_turn` is then the boundary,
    and it is code that knows it."""
    missing = [r for r in ctx.must_cover if r not in cited and r not in dismissed]
    if missing:
        raise ValueError(
            "these owner texts are neither cited nor listed under not_goal_bearing: "
            + ", ".join(missing)
            + " — cite each where it belongs, or list it under not_goal_bearing"
        )


def _check_dropped(cited: list[Item], where: str, fresh: set[str]) -> None:
    """A goal becomes `dropped` only on the owner's own word or act, given
    SINCE the previous revision and AFTER he asked for it — never by inference,
    and never on the strength of the message that created the goal."""
    asked = [c.turn for c in cited if c.kind in OWNER_WORDS]
    if not any(
        c.kind in OWNER_ACTS and c.ref in fresh and asked and c.turn > min(asked)
        for c in cited
    ):
        raise ValueError(
            f"{where}: dropped needs the owner's own word or act, after the previous "
            "revision and after he asked for the goal — cite it, or use parked or unknown"
        )


def _stamp_was(record: dict, before: dict | None) -> None:
    """An item that becomes `unknown` remembers the state it left, so the next
    revision's transition is judged from there."""
    if record["state"] == "unknown" and (was := _last_known(before)):
        record["was"] = was


def _by(before: dict | None, field_name: str, value: Any) -> str:
    if before is not None and before.get(field_name) == value:
        return str(before.get(f"{field_name}_by") or ORIGIN_DISTILLER)
    return ORIGIN_DISTILLER


def _restore_quote(quote: Any, ctx: Context) -> Any:
    if not isinstance(quote, dict):
        return UNSTATED
    return {
        "text": quote.get("text"),
        "cites": [{"session": ctx.chat, "ref": _bare_ref(c)} for c in quote.get("cites") or ()],
    }


def _restore(goal: dict, ctx: Context, task_ids: set[str]) -> dict:
    tasks = []
    for task in goal.get("tasks") or ():
        tid = str(task.get("id"))
        if tid not in task_ids:
            tasks.append(_restore_task(task, ctx))
            task_ids.add(tid)
    return {
        "id": goal.get("id"),
        "text": goal.get("text"),
        "state": goal.get("state"),
        **({"was": goal["was"]} if goal.get("was") else {}),
        "state_by": goal.get("state_by") or ORIGIN_DISTILLER,
        "text_by": goal.get("text_by") or ORIGIN_DISTILLER,
        "cites": [{"session": ctx.chat, "ref": _bare_ref(c)} for c in goal.get("cites") or ()],
        "why": _restore_quote(goal.get("why"), ctx),
        "done_when": _restore_quote(goal.get("done_when"), ctx),
        "constraints": [
            {
                "text": c.get("text"),
                "cites": [{"session": ctx.chat, "ref": _bare_ref(x)} for x in c.get("cites") or ()],
            }
            for c in goal.get("constraints") or ()
        ],
        "tasks": tasks,
    }


def _restore_task(task: dict, ctx: Context) -> dict:
    out = {
        "id": task.get("id"),
        "text": task.get("text"),
        "state": task.get("state"),
        **({"was": task["was"]} if task.get("was") else {}),
        "state_by": task.get("state_by") or ORIGIN_DISTILLER,
        "text_by": task.get("text_by") or ORIGIN_DISTILLER,
        "cites": [{"session": ctx.chat, "ref": _bare_ref(c)} for c in task.get("cites") or ()],
    }
    if task.get("replaced_by"):
        out["replaced_by"] = task["replaced_by"]
    return out


def _constraints(raw: Any, where: str, ctx: Context, before: dict | None, wrap, caps) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{where}: constraints must be a list")
    out = []
    for n, entry in enumerate(raw, 1):
        at = f"{where}, constraint {n}"
        if not isinstance(entry, dict):
            raise ValueError(f"{at}: must be an object with text and cites")
        text = roles.capped(str(entry.get("text") or ""), caps.get("max_constraint_chars", 300))
        if not text:
            raise ValueError(f"{at}: text is required")
        cited = _cites(entry.get("cites"), at, ctx, caps.get("max_cites", 8))
        if not any(c.kind in OWNER_WORDS and _squash(text) in _squash(c.text) for c in cited):
            raise ValueError(
                f"{at}: {text!r} is not a verbatim quote from the owner text it cites — "
                "copy the words exactly as he wrote them, in his language"
            )
        out.append({"text": text, "cites": wrap(cited)})
    return out


def _tasks(
    raw: Any, where: str, ctx: Context, prev_tasks: dict, task_ids: set, wrap, caps, fresh,
    downgrades: list[dict[str, str]],
):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{where}: tasks must be a list")
    if len(raw) > caps.get("max_tasks", 30):
        raise ValueError(f"{where}: at most {caps.get('max_tasks', 30)} tasks")
    out = []
    for n, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"{where}, task {n}: must be an object")
        tid = str(entry.get("id") or "").strip()
        at = f"task {tid or n}"
        if not _ID.fullmatch(tid):
            raise ValueError(f"{at}: id must be 1-24 letters, digits, '.', '_' or '-'")
        if tid in task_ids:
            raise ValueError(f"{at}: two tasks share the id {tid!r}")
        task_ids.add(tid)
        text = roles.capped(str(entry.get("text") or ""), caps.get("max_chars", 200))
        if not text:
            raise ValueError(f"{at}: text is required")
        state = str(entry.get("state") or "").strip().lower()
        if state not in TASK_STATES:
            raise ValueError(f"{at}: state must be one of {', '.join(TASK_STATES)}")
        cited = _cites(entry.get("cites"), at, ctx, caps.get("max_cites", 8))
        if state in ("pending", "in_progress"):
            _require(cited, ASKED, at, f"{state} must cite where it was asked or planned")
        elif (
            state == "done"
            and not _has(cited, EVIDENCE)
            and not _owner_held(prev_tasks.get(tid), state)
        ):
            state = "unknown"
            downgrades.append({"task": tid, "from": "done", "why": "no answer or action cited"})
        elif state == "stopped":
            _require(cited, OWNER_ACTS, at, "stopped needs the owner's own act")
        record: dict[str, Any] = {"id": tid, "text": text, "state": state}
        before = prev_tasks.get(tid)
        _check_transition(before, record, at, cited, _TASK_REOPEN, _TASK_TERMINAL, fresh)
        _stamp_was(record, before)
        record["state_by"] = _by(before, "state", state)
        record["text_by"] = _by(before, "text", text)
        record["cites"] = wrap(cited)
        if state == "superseded":
            replaced_by = str(entry.get("replaced_by") or "").strip()
            if not replaced_by or replaced_by == tid:
                raise ValueError(f"{at}: superseded needs replaced_by naming another task")
            record["replaced_by"] = replaced_by
        out.append(record)
    return out


def contract_text(shape: roles.Shape) -> str:
    """The output contract, generated from the declared caps and this module's
    vocabularies, so the words the model is given and the rules code enforces
    cannot drift apart."""
    caps = shape.caps
    quote = (
        f'{{"text": "<his exact words, at most {caps.get("max_constraint_chars", 300)} '
        f'characters>", "cites": ["<ref>"]}} or "{UNSTATED}"'
    )
    return "\n".join(
        [
            "Reply with ONE JSON object and nothing else. No prose before or after it, "
            "no code fence.",
            "",
            "{",
            f'  "change": one of {", ".join(json.dumps(c) for c in CHANGES)},',
            '  "goals": [',
            "    {",
            '      "id": a short id, e.g. "g1" — keep the id of a goal you were given,',
            f'      "text": at most {caps.get("max_chars", 200)} characters,',
            f'      "state": one of {", ".join(json.dumps(s) for s in GOAL_STATES)},',
            '      "cites": ["<ref>", ...],',
            f'      "why": {quote},',
            f'      "done_when": {quote},',
            '      "constraints": [{"text": "<an exact quote of the owner>", '
            '"cites": ["<ref>"]}],',
            '      "tasks": [',
            '        {"id": a short id, unique across ALL goals, e.g. "t1",',
            f'         "text": at most {caps.get("max_chars", 200)} characters,',
            f'         "state": one of {", ".join(json.dumps(s) for s in TASK_STATES)},',
            '         "cites": ["<ref>", ...],',
            '         "replaced_by": "<task id>" — only when state is "superseded"}',
            "      ]",
            "    }",
            "  ],",
            '  "not_goal_bearing": ["<ref>", ...] — refs from "must_cover" you did not cite',
            "}",
            "",
            f"At most {caps.get('max_goals', 12)} goals, {caps.get('max_tasks', 30)} tasks per "
            f"goal, {caps.get('max_cites', 8)} cites per item. A ref is copied exactly from "
            'the "ref" of an item under "material".',
            'Every ref in "must_cover" must be cited somewhere or listed in "not_goal_bearing".',
            "An answer that breaks a rule is rejected and you are asked once more.",
        ]
    )


def tally(value: Revision) -> dict[str, dict[str, int]]:
    """Per-call state counts, for the role record's `flags` (the counters)."""
    goals: dict[str, int] = {}
    tasks: dict[str, int] = {}
    for goal in value.goals:
        goals[goal["state"]] = goals.get(goal["state"], 0) + 1
        for task in goal.get("tasks") or ():
            tasks[task["state"]] = tasks.get(task["state"], 0) + 1
    return {
        "goal_state": goals,
        "task_state": tasks,
        "downgraded": {"done": len(value.downgrades)},
        # How much of what he said the model set aside rather than cited — the
        # one number that shows a distiller dismissing everything to pass.
        "coverage": {
            "not_goal_bearing": len(value.not_goal_bearing),
        },
        "quotes": {
            "stated": sum(1 for g in value.goals for k in ("why", "done_when")
                          if isinstance(g.get(k), dict)),
            UNSTATED: sum(1 for g in value.goals for k in ("why", "done_when")
                          if g.get(k) == UNSTATED),
        },
    }


# ---------------------------------------------------------------- exam assertions


def _all_states(value: Revision) -> list[str]:
    return [g["state"] for g in value.goals] + [
        t["state"] for g in value.goals for t in g.get("tasks") or ()
    ]


def _texts(value: Revision) -> str:
    return " ".join(
        [g["text"] for g in value.goals]
        + [c["text"] for g in value.goals for c in g.get("constraints") or ()]
        + [t["text"] for g in value.goals for t in g.get("tasks") or ()]
    ).casefold()


def _expect_never(expected: Any, value: Revision) -> list[str]:
    """None of these states anywhere — the golden file's "never `done` where
    the owner says otherwise" rule."""
    states = _all_states(value)
    return [f"the answer uses state {s!r}" for s in expected or () if s in states]


def _expect_goals_at_least(expected: Any, value: Revision) -> list[str]:
    if len(value.goals) < int(expected):
        return [f"expected at least {expected} goals, got {len(value.goals)}"]
    return []


def _expect_mentions_any(expected: Any, value: Revision) -> list[str]:
    """Each group: at least one of these substrings appears in some goal, task
    or constraint text. Groups, because the model may answer in either of the
    owner's languages."""
    blob = _texts(value)
    return [
        f"no goal, task or constraint mentions any of {group}"
        for group in expected or ()
        if not any(str(word).casefold() in blob for word in group)
    ]


def _expect_why_mentions_any(expected: Any, value: Revision) -> list[str]:
    """Each group: some goal's quoted `why` contains one of these substrings —
    the purpose was found, not only the topic."""
    blob = " ".join(
        str(g["why"].get("text") or "") for g in value.goals if isinstance(g.get("why"), dict)
    ).casefold()
    return [
        f"no goal's why mentions any of {group}"
        for group in expected or ()
        if not any(str(word).casefold() in blob for word in group)
    ]


def _expect_absent(expected: Any, value: Revision) -> list[str]:
    blob = json.dumps(value.as_json(), ensure_ascii=False).casefold()
    return [
        f"the output still carries {needle!r}"
        for needle in expected or ()
        if str(needle).casefold() in blob
    ]


def _expect_active_goal(expected: Any, value: Revision) -> list[str]:
    active = [g for g in value.goals if g["state"] == "active"]
    if bool(expected) and not active:
        return ["expected an active goal, got none"]
    return []


ASSERTIONS: dict[str, Callable[[Any, Revision], list[str]]] = {
    "never_state": _expect_never,
    "goals_at_least": _expect_goals_at_least,
    "mentions_any": _expect_mentions_any,
    "absent": _expect_absent,
    "has_active_goal": _expect_active_goal,
    "why_mentions_any": _expect_why_mentions_any,
}


# ---------------------------------------------------------------- emission


@dataclass(frozen=True)
class Boundary:
    """A task end, captured synchronously when it happened: the log, how many
    bytes it held, the bytes of its LAST line, and the task's turn. Everything
    else is read later, off the interactive path.

    `tail` is what makes the byte offset trustworthy. A Retry or a redaction
    rewrites the file in place, shifting every offset after the first line it
    touches; a distill that trusted `upto` alone would read a torn prefix that
    is neither the chat before the rewrite nor after it. So the distill reads
    only if the file still ends, at `upto`, with exactly this line — and writes
    only if it still does when the write lands.
    """

    path: Path
    upto: int
    turn: int
    model_spec: str
    state_dir: str | None
    tail: bytes = b""


# The most of the last line kept for the check. A task_end line is ~60 bytes;
# a CLI log's last line may be a whole answer, and a suffix this long is still
# a content address in practice.
TAIL_BYTES = 4096


def boundary_of(
    path: Path, turn: int, model_spec: str, state_dir: str | None
) -> Boundary | None:
    """The boundary as the file stands NOW — one stat and one small read at the
    end of the file. None when there is no log to read."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            upto = handle.tell()
            start = max(0, upto - TAIL_BYTES - 1)
            handle.seek(start)
            # Bounded at `upto`: an append landing after the stat (a previous
            # task's distill finishing now) must not become this boundary's tail,
            # or an unrewritten chat is recorded as rewritten.
            chunk = handle.read(upto - start)
    except OSError:
        return None
    body = chunk[:-1] if chunk.endswith(b"\n") else chunk
    cut = body.rfind(b"\n")
    tail = chunk[cut + 1 :] if cut >= 0 else chunk[-TAIL_BYTES:]
    return Boundary(path, upto, turn, model_spec, state_dir, tail[-TAIL_BYTES:])


def still_there(data: bytes, boundary: Boundary) -> bool:
    """Does the file still end, at the boundary's offset, with its last line?"""
    return len(data) >= boundary.upto and data[: boundary.upto].endswith(boundary.tail)


def disabled() -> bool:
    """`AISH_OBJECTIVE=0` turns emission off. The suite sets it (conftest), for
    the reason `AISH_NOTIFY=0` exists: a background writer appending to logs
    that tests assert on, at a moment no test controls."""
    return os.environ.get("AISH_OBJECTIVE", "").strip() == "0"


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), threading.Lock())


def _parse_bytes(data: bytes) -> list[dict]:
    out: list[dict] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def distill(
    boundary: Boundary,
    *,
    chat_fn: Callable[..., Any] | None = None,
    model_name: str = "",
    check_admission: bool = True,
    charter: roles.Charter | None = None,
    records: list[dict] | None = None,
    history: list[dict] | None = None,
) -> list[dict]:
    """The records one task end produces, in the order they are written.

    `records` is the chat as it stood at the boundary — the material comes from
    it. `history` is the whole file as it stands now, which is where the base
    and the next revision number come from: a previous distill may have
    written its revision AFTER this boundary was captured, and reading those
    from the bounded prefix would reissue a revision number and lose the base.

    Always a `role` record (the D7 record, whatever the outcome — "unexamined"
    is a fact in the log, never an absence). Then an `objective` record: the
    distiller's revision when it validated, else the extractive floor when any
    owner text is uncovered, else nothing — and the previous revision stands.
    """
    if records is None:
        records = read_records(boundary.path, boundary.upto)
    if history is None:
        history = records
    chat = boundary.path.stem
    base = current(history, BASE_ORIGINS)
    if base is not None and int(base.get("turn") or 0) >= boundary.turn:
        # Distills are serialised per chat but not queued in order: a later
        # boundary can be distilled first, and it already covers this one.
        return [
            _skip_record(
                boundary.turn,
                f"a later boundary (turn {base.get('turn')}) was already distilled",
            )
        ]
    items = material(records)
    revision = next_revision(history)
    out: list[dict] = []

    try:
        charter = charter or roles.load_charters()[DISTILLER]
    except (roles.CharterError, KeyError) as exc:
        out.append(_skip_record(boundary.turn, f"the distiller charter does not load: {exc}"))
        charter = None

    result: roles.Result | None = None
    if charter is not None:
        text = compose_input(chat, boundary.turn, items, base)
        result = roles.run(
            charter,
            {"material": text},
            (),
            model_spec=boundary.model_spec,
            chat=chat_fn,
            model_name=model_name,
            state_dir=boundary.state_dir,
            check_admission=check_admission,
        )
        out.append(role_record(charter, result, boundary.turn))

    if result is not None and result.status == roles.Status.OK and result.value is not None:
        value: Revision = result.value
        out.append(
            {
                "kind": "objective",
                "turn": boundary.turn,
                "revision": revision,
                "origin": ORIGIN_DISTILLER,
                "chat": chat,
                "covers_to_turn": value.covers_to_turn,
                "confirmed": False,
                "change": value.change,
                "not_goal_bearing": value.not_goal_bearing,
                "downgrades": value.downgrades,
                "base": base.get("revision") if base else None,
                "charter": charter.name if charter else DISTILLER,
                "version": charter.version if charter else "",
                "model": result.model,
                "goals": value.goals,
            }
        )
        return out

    covered = int(base.get("covers_to_turn") or 0) if base else 0
    extract = floor(items, covered, boundary.turn)
    if extract:
        out.append(
            {
                "kind": "objective",
                "turn": boundary.turn,
                "revision": revision,
                "origin": ORIGIN_EXTRACTIVE,
                "chat": chat,
                "covers_to_turn": boundary.turn,
                "confirmed": False,
                "base": base.get("revision") if base else None,
                "goals": list(base.get("goals") or []) if base else [],
                "extract": [{"ref": i.ref, "turn": i.turn, "text": i.text} for i in extract],
            }
        )
    return out


def role_record(charter: roles.Charter, result: roles.Result, turn: int) -> dict:
    """The §D7 `role` record, in the shape `Agent._record_role` writes."""
    record: dict[str, Any] = {
        "kind": "role",
        "turn": turn,
        "charter": charter.name,
        "version": charter.version,
        "role_kind": charter.kind,
        "status": result.status,
        "model": result.model,
        "attempts": result.attempts,
        "ms": result.ms,
        "degradation": charter.degradation,
        "input": {
            "name": result.input_name,
            "trust": result.input_trust,
            "chars": result.input_chars,
            "digest": result.input_digest,
        },
    }
    if result.why:
        record["why"] = result.why
    if result.usage:
        record["usage"] = result.usage
    if result.value is not None:
        record["flags"] = roles.tally_flags(charter.output, result.value)
    return record


def _skip_record(turn: int, why: str) -> dict:
    return {
        "kind": "role",
        "turn": turn,
        "charter": DISTILLER,
        "version": "",
        "status": roles.Status.UNAVAILABLE,
        "why": why,
        "model": "",
        "attempts": 0,
        "ms": 0,
    }


REWRITTEN = (
    "the chat was rewritten after this task ended (a Retry or a redaction), so the "
    "boundary it was taken at no longer exists"
)


def distill_at_boundary(boundary: Boundary, log: Any) -> list[dict]:
    """Distill and write. NEVER raises: this runs after the answer, and nothing
    it does may reach the task.

    `log` is the chat's `SessionLog`. Both the read and the write go through
    its write lock, which is the lock every rewrite of the file holds: the read
    is `log.snapshot()`, and the write is `log.append_steps_if`, which appends
    only if the boundary's last line is still where it was. So a Retry pressed
    before the read produces no revision, and one pressed while the model was
    thinking produces the `role` record (the call was made and paid for) with
    `discarded` set, and no revision — never a live revision distilled from an
    attempt the owner discarded.

    One distill per chat at a time (a per-path lock).
    """
    if disabled():
        return []
    written: list[dict] = []
    try:
        with _lock_for(boundary.path):
            started = time.perf_counter()
            data = log.snapshot()
            if not still_there(data, boundary):
                out = [_skip_record(boundary.turn, REWRITTEN)]
            else:
                try:
                    out = distill(
                        boundary,
                        records=_parse_bytes(data[: boundary.upto]),
                        history=_parse_bytes(data),
                    )
                except Exception as exc:  # noqa: BLE001 — a bug must not reach the task
                    out = [
                        _skip_record(
                            boundary.turn,
                            f"the distill raised {type(exc).__name__}: {exc}"[:300],
                        )
                    ]
                    out[0]["ms"] = int((time.perf_counter() - started) * 1000)

            def unchanged() -> bool:
                return still_there(log.snapshot_locked(), boundary)

            if log.append_steps_if(out, unchanged):
                written.extend(out)
            else:
                roles_only = [
                    {**r, "discarded": REWRITTEN} for r in out if r.get("kind") == "role"
                ]
                log.append_steps_if(roles_only, lambda: True)
                written.extend(roles_only)
    except Exception:  # noqa: BLE001 — a write refused (a trashed chat) ends it quietly
        pass
    return written
