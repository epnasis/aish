"""The plan — aish's own checklist for reaching the owner's objective (#423, #433).

Level two of the owner's three-level model (epic #423, 2026-09-29): the objective
(`objective.py`) is why he is here; the plan is how aish gets there. It is written
ONLY by the acting model, through the `plan` tool, when a piece of work needs
several steps. It never comes from his messages, and his card comments (hints)
never become tasks.

Code holds the rules the model cannot be trusted to hold:

- **Done needs evidence.** A task marked done must cite a successful call, a `!`
  command he ran, or an earlier answer, and the citation must resolve against
  the chat's own live records (`resolve_evidence`). Otherwise it is recorded as
  pending and the downgrade is recorded with it.
- **Replanning keeps what it replaces.** A task that disappears from the model's
  list is kept as `dropped_replan`; nothing is ever deleted.
- **His drop is final.** A task the owner dropped is `dropped_by_owner` and the
  model cannot change it — refused on write, and re-applied on every read
  (`current`), so no interleaving with a model call can lose it.

Each revision is a renderless `plan` record (contract §3.15). It reaches the model
through the tool's result and the per-task reminder (`agent.plan_delta`), and the
owner through the objective strip and `/plan`. `docs/plan.md`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import objective, provenance

PLAN_TOOL = "plan"

ORIGIN_MODEL = "model"
ORIGIN_OWNER = "owner"
ACTION_REVISE = "revise"
ACTION_DROP = "drop"
ACTION_REPLAN = "replan"

PENDING = "pending"
DOING = "doing"
DONE = "done"
DROPPED_REPLAN = "dropped_replan"
DROPPED_BY_OWNER = "dropped_by_owner"
STATES = (PENDING, DOING, DONE, DROPPED_REPLAN, DROPPED_BY_OWNER)
OPEN = (PENDING, DOING)
# What the model may send. `dropped` is written as DROPPED_REPLAN: a drop the
# model makes is a replan by definition.
MODEL_DROPPED = "dropped"
MODEL_STATES = (PENDING, DOING, DONE, MODEL_DROPPED)

MAX_TASKS = 20
TASK_TITLE_CHARS = 160
ID_CHARS = 12
# A quote shorter than this matches too much to be evidence of anything.
QUOTE_MIN_CHARS = 6
QUOTE_CHARS = 200
# How many recent successful calls a result lists when a done was downgraded.
CITABLE_SHOWN = 8

STATE_WORDS = {
    PENDING: "pending",
    DOING: "doing",
    DONE: "done",
    DROPPED_REPLAN: "dropped (replan)",
    DROPPED_BY_OWNER: "dropped by the owner — never work on it",
}


class PlanError(ValueError):
    """A plan call that writes nothing; the message is what the model is told."""


def _step(record: dict) -> dict:
    step = record.get("step") if record.get("kind") == "trace" else None
    return step if isinstance(step, dict) else {}


def title_key(title: str) -> str:
    return " ".join(str(title).split()).casefold()


# Shell quoting is not part of what a call DID: the owner's measurement (#433)
# saw a model cite `curl -s http://…/rain/pl-maz-0419` for a call that ran as
# `curl -s "http://…/rain/pl-maz-0419"`, and every such done was downgraded.
_QUOTES = str.maketrans("", "", "'\"`")


def _squash(text: str) -> str:
    return " ".join(str(text).translate(_QUOTES).split()).casefold()


def is_revision(step: dict) -> bool:
    return step.get("kind") == "plan" and isinstance(step.get("tasks"), list)


def _live_revisions(records: Iterable[dict]) -> list[dict]:
    return [
        step
        for record in records
        if not record.get("superseded") and is_revision(step := _step(record))
    ]


def owner_drops(records: Iterable[dict]) -> set[str]:
    """The title keys of every task the owner dropped, from LIVE owner records."""
    return {
        title_key(step.get("title") or "")
        for step in _live_revisions(records)
        if step.get("origin") == ORIGIN_OWNER and step.get("action") == ACTION_DROP
        and step.get("title")
    }


def counts(tasks: Iterable[dict]) -> dict[str, int]:
    out = dict.fromkeys(STATES, 0)
    for task in tasks:
        state = task.get("state")
        if state in out:
            out[state] += 1
    return out


def _overlay(plan: dict, dropped: set[str]) -> dict:
    if not dropped:
        return plan
    changed = False
    for task in plan.get("tasks") or ():
        if task.get("state") != DROPPED_BY_OWNER and title_key(task.get("title") or "") in dropped:
            task["state"] = DROPPED_BY_OWNER
            for key in ("evidence", "downgraded", "dropped"):
                task.pop(key, None)
            changed = True
    if changed:
        plan["counts"] = counts(plan["tasks"])
    return plan


def current(records: Iterable[dict]) -> dict | None:
    """The plan in force: the newest LIVE model revision, with every live owner
    drop applied over it (contract §3.15, the owner-drop overlay). A copy.

    His records carry the whole list as he saw it, so each is self-contained,
    but the plan is never READ from them: a Retry that discards the model
    revision he acted on must not leave his record carrying its states. When
    his record is the newest, `changed_by` says so and `owner_revision` names it."""
    records = list(records)
    revisions = _live_revisions(records)
    model = [s for s in revisions if s.get("origin") == ORIGIN_MODEL]
    if not model:
        return None
    plan = _overlay(copy.deepcopy(model[-1]), owner_drops(records))
    plan["counts"] = counts(plan.get("tasks") or ())
    newest = revisions[-1]
    if newest is not model[-1]:
        plan["changed_by"] = ORIGIN_OWNER
        plan["owner_revision"] = newest.get("revision")
    return plan


def next_revision(records: Iterable[dict]) -> int:
    """1 + the highest revision in the file, superseded ones included."""
    numbers = [
        int(_step(r).get("revision") or 0) for r in records if _step(r).get("kind") == "plan"
    ]
    return max(numbers, default=0) + 1


def replan_pending(records: Iterable[dict]) -> int | None:
    """The revision of his replan request while no model revision answered it."""
    last_model = 0
    request = 0
    for step in _live_revisions(records):
        number = int(step.get("revision") or 0)
        if step.get("origin") == ORIGIN_MODEL:
            last_model = max(last_model, number)
        elif step.get("action") == ACTION_REPLAN:
            request = max(request, number)
    # A tie means the model's write raced his: its read predates his record,
    # so it did not answer him (review finding) — his request stands.
    return request if request >= last_model and request else None


def open_tasks(plan: dict | None) -> list[dict]:
    return [t for t in (plan or {}).get("tasks") or () if t.get("state") in OPEN]


# ---------------------------------------------------------------- evidence


@dataclass(frozen=True)
class Citable:
    """Something a done may rest on: a successful call, a `!` command he ran
    with exit 0, or an earlier final answer."""

    ref: str
    kind: str  # "tool" | "ran" | "answer"
    tool: str
    label: str  # one line, for the result's list of citable calls
    texts: tuple[str, ...]  # squashed, folded: what a quote is matched against


def _strings(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 3:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _strings(item, out, depth + 1)


def citable(records: Iterable[dict]) -> list[Citable]:
    """Everything in the chat's LIVE records a done may cite, newest first."""
    records = [r for r in records if not r.get("superseded")]
    args: dict[tuple[Any, Any], Any] = {}
    for record in records:
        step = _step(record)
        if step.get("kind") == "call":
            args[(step.get("turn"), step.get("call"))] = step.get("args")
    out: list[Citable] = []
    for record in records:
        step = _step(record)
        if step.get("kind") != "tool" or step.get("ok") is not True:
            continue
        name = str(step.get("name") or "")
        call = step.get("call")
        if name == PLAN_TOOL or not isinstance(call, int) or isinstance(call, bool):
            continue
        turn = step.get("turn")
        texts: list[str] = [str(step.get("command") or ""), str(step.get("summary") or "")]
        _strings(args.get((turn, call)), texts)
        label = str(step.get("command") or step.get("summary") or "")
        out.append(
            Citable(
                f"t{turn}.c{call}", "tool", name, " ".join(label.split())[:120],
                tuple(_squash(t) for t in texts if t.strip()),
            )
        )
    for item in objective.material(records, read_only=frozenset()):
        if item.kind in (objective.RAN, objective.ANSWER):
            kind = "ran" if item.kind == objective.RAN else "answer"
            out.append(Citable(item.ref, kind, "", " ".join(item.text.split())[:120],
                               (_squash(item.text),)))
    out.reverse()
    return out


def _ref_shape(text: str) -> bool:
    head, dot, tail = text.partition(".")
    return bool(dot) and head[:1] == "t" and head[1:].isdigit() and tail[:1] == "c" and (
        tail[1:].isdigit()
    )


def resolve_evidence(cited: str, candidates: list[Citable]) -> tuple[dict | None, str]:
    """(evidence, why-not). The evidence is the record's own ref, whatever form
    the model cited; `why` says what failed when it does not resolve."""
    text = " ".join(str(cited or "").split())
    if not text:
        return None, "no evidence was cited"
    if _ref_shape(text):
        for candidate in candidates:
            if candidate.kind == "tool" and candidate.ref == text:
                return _evidence(candidate, text), ""
        return None, f"{text} names no successful call in this chat"
    quote = _squash(text)
    if len(quote) < QUOTE_MIN_CHARS:
        return None, f"a quote must be at least {QUOTE_MIN_CHARS} characters"
    for kind in ("tool", "ran", "answer"):
        for candidate in candidates:
            if candidate.kind == kind and any(quote in t for t in candidate.texts):
                return _evidence(candidate, text), ""
    return None, "it matches no successful call, command you ran, or earlier answer in this chat"


def _evidence(candidate: Citable, cited: str) -> dict:
    out = {"ref": candidate.ref, "kind": candidate.kind, "quote": cited[:QUOTE_CHARS]}
    if candidate.tool:
        out["tool"] = candidate.tool
    return out


# ---------------------------------------------------------------- the model's revision


@dataclass
class Incoming:
    id: str
    title: str
    state: str
    evidence: str


def parse_tasks(args: Any) -> list[Incoming]:
    """The model's list, or PlanError with a sentence it can act on."""
    raw = args.get("tasks") if isinstance(args, dict) else None
    if isinstance(raw, str):
        # Some models send the array as a JSON string.
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    if not isinstance(raw, list):
        raise PlanError('"tasks" must be a list of {id, title, state, evidence} objects')
    if len(raw) > MAX_TASKS:
        raise PlanError(f"at most {MAX_TASKS} tasks — a task is bigger than one tool call")
    out: list[Incoming] = []
    for position, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise PlanError(f"task {position} is not an object")
        title = provenance.disarm_markers(
            " ".join(str(entry.get("title") or "").split())[:TASK_TITLE_CHARS]
        )
        if not title:
            raise PlanError(f"task {position} has no title")
        state = str(entry.get("state") or PENDING).strip().lower()
        if state not in MODEL_STATES:
            raise PlanError(
                f"task {position} state must be one of {', '.join(MODEL_STATES)} (got {state!r})"
            )
        ident = " ".join(str(entry.get("id") or "").split())[:ID_CHARS]
        evidence = str(entry.get("evidence") or "").strip()
        out.append(Incoming(ident, title, state, evidence))
    ids = [t.id for t in out if t.id]
    if len(ids) != len(set(ids)):
        raise PlanError("two tasks share an id — each id names one task")
    open_titles = [title_key(t.title) for t in out if t.state in OPEN]
    if len(open_titles) != len(set(open_titles)):
        raise PlanError("two open tasks share a title")
    return out


def revise(
    base: dict | None,
    args: Any,
    records: list[dict],
    *,
    turn: int,
    revision: int,
    chat: str,
    call: int = 0,
    model_call: int = 0,
) -> dict:
    """The record a model's plan call writes. `base` is the plan in force
    (owner overlay applied); `records` are the chat's records for evidence."""
    incoming = parse_tasks(args)
    base_tasks = [dict(t) for t in (base or {}).get("tasks") or ()]
    by_id = {str(t.get("id")): t for t in base_tasks}
    matched: set[str] = set()
    candidates: list[Citable] | None = None
    tasks: list[dict] = []
    refused: list[dict] = []
    downgraded = 0
    used_ids = set(by_id)
    next_id = 1

    def fresh_id(wanted: str) -> str:
        nonlocal next_id
        if wanted and wanted not in used_ids:
            used_ids.add(wanted)
            return wanted
        while str(next_id) in used_ids:
            next_id += 1
        used_ids.add(str(next_id))
        return str(next_id)

    for entry in incoming:
        found = by_id.get(entry.id) if entry.id else None
        if found is not None and (
            str(found.get("id")) in matched
            # An id names the same task only while that task is open or keeps
            # its title: a model that renumbers must not turn a finished or
            # dropped task into a different one, and lose its record.
            or (found.get("state") not in OPEN
                and title_key(found.get("title") or "") != title_key(entry.title))
        ):
            found = None
        if found is None:
            key = title_key(entry.title)
            found = next(
                (
                    t for t in base_tasks
                    if str(t.get("id")) not in matched and title_key(t.get("title") or "") == key
                ),
                None,
            )
        if found is not None:
            matched.add(str(found.get("id")))
        if found is not None and found.get("state") == DROPPED_BY_OWNER:
            wanted_state = DROPPED_REPLAN if entry.state == MODEL_DROPPED else entry.state
            if entry.state != MODEL_DROPPED or title_key(entry.title) != title_key(
                found.get("title") or ""
            ):
                refused.append({
                    "id": str(found.get("id")),
                    "why": "dropped by the owner; the model cannot change it",
                    "asked": {"state": wanted_state, "title": entry.title},
                })
            tasks.append({"id": str(found.get("id")), "title": found.get("title"),
                          "state": DROPPED_BY_OWNER})
            continue
        task: dict[str, Any] = {
            "id": str(found.get("id")) if found is not None else fresh_id(entry.id),
            "title": entry.title,
        }
        if entry.state == MODEL_DROPPED:
            task["state"] = DROPPED_REPLAN
            task["dropped"] = "explicit"
        elif entry.state == DONE:
            if candidates is None:
                candidates = citable(records)
            evidence, why = (
                resolve_evidence(entry.evidence, candidates) if entry.evidence
                else (None, "no evidence was cited")
            )
            # A task already done on resolved evidence keeps it: a later citation
            # that does not resolve adds nothing, and must not undo the done
            # (measurement finding, #433).
            carried = (
                found.get("evidence")
                if found is not None and found.get("state") == DONE
                else None
            )
            if evidence is not None or carried:
                task["state"] = DONE
                task["evidence"] = evidence or carried
            else:
                task["state"] = PENDING
                task["downgraded"] = {"from": DONE, "quote": entry.evidence[:QUOTE_CHARS],
                                      "why": why}
                downgraded += 1
        else:
            task["state"] = entry.state
        tasks.append(task)

    for task in base_tasks:
        if str(task.get("id")) in matched:
            continue
        kept = {k: task[k] for k in ("id", "title", "state", "evidence", "dropped") if k in task}
        if task.get("state") in OPEN:
            kept["state"] = DROPPED_REPLAN
            kept["dropped"] = "vanished"
        tasks.append(kept)

    record: dict[str, Any] = {
        "kind": "plan",
        "turn": turn,
        "revision": revision,
        "origin": ORIGIN_MODEL,
        "action": ACTION_REVISE,
        "chat": chat,
        "base": (base or {}).get("revision"),
        "tasks": tasks,
        "counts": counts(tasks),
    }
    if call:
        record["call"] = call
    if model_call:
        record["model_call"] = model_call
    if downgraded:
        record["downgraded"] = downgraded
    if refused:
        record["refused"] = refused
    return record


# ---------------------------------------------------------------- what the model reads


def render(plan: dict | None) -> str:
    """One line per task — the text the model is shown, in the tool result and
    in the reminder alike, so "already shown" is a substring test."""
    lines = []
    for task in (plan or {}).get("tasks") or ():
        lines.append(f"[{task.get('id')}] {STATE_WORDS.get(task.get('state'), task.get('state'))}"
                     f" — {task.get('title')}")
    return "\n".join(lines)


PLAN_RECORDED = "Plan recorded as revision {revision}"
OPEN_RULE = (
    "Attempt every open task, or change the plan with the plan tool, before your final answer."
)


def tool_result(record: dict, candidates: list[Citable] | None = None) -> str:
    c = record.get("counts") or {}
    active = sum(c.get(s, 0) for s in (PENDING, DOING, DONE))
    lines = [
        PLAN_RECORDED.format(revision=record.get("revision"))
        + f" ({c.get(DONE, 0)} of {active} done). The owner sees it.",
        render(record),
    ]
    for task in record.get("tasks") or ():
        down = task.get("downgraded")
        if down:
            lines.append(
                f"NOT DONE: [{task.get('id')}] was recorded as pending — {down.get('why')}. "
                "Cite the successful call that proved it: its ref, or an exact part of its "
                "command or arguments — never its output."
            )
    for refusal in record.get("refused") or ():
        lines.append(f"REFUSED: [{refusal.get('id')}] {refusal.get('why')}.")
    if record.get("downgraded") and candidates:
        shown = [c for c in candidates if c.kind == "tool"][:CITABLE_SHOWN]
        if shown:
            lines.append("Recent successful calls you can cite:")
            lines.extend(f"  {c.ref} {c.tool}: {c.label}" for c in shown)
    if (record.get("counts") or {}).get(PENDING, 0) + (record.get("counts") or {}).get(DOING, 0):
        lines.append(OPEN_RULE)
    return "\n".join(lines)


# ---------------------------------------------------------------- the owner's actions


def _parse(data: bytes) -> list[dict]:
    return objective._parse_bytes(data)


def _latest_turn(records: list[dict]) -> int:
    turns = [i.turn for i in objective.material(records, read_only=frozenset())]
    stamped = [
        t for r in records
        if isinstance(t := _step(r).get("turn"), int) and not isinstance(t, bool)
    ]
    return max(turns + stamped, default=0)


def _owner_write(log: Any, build: Any) -> dict:
    """Append the owner's revision under the log's write lock, only if the file
    did not change between the read and the write (`objective.owner_edit`'s
    shape). `build(records)` returns the record or raises ValueError."""
    for _ in range(3):
        data = log.snapshot()
        records = _parse(data)
        record = build(records)
        size = len(data)

        def unchanged(size: int = size, data: bytes = data) -> bool:
            now = log.snapshot_locked()
            tail = objective.TAIL_BYTES
            return len(now) == size and now[-tail:] == data[-tail:]

        if log.append_steps_if([record], unchanged):
            return record
    raise ValueError("the chat kept changing while the plan was being saved; try again")


def owner_drop(log: Any, task_id: str) -> dict:
    """His drop of one open task: a revision with that task `dropped_by_owner`."""
    wanted = str(task_id).strip()

    def build(records: list[dict]) -> dict:
        plan = current(records)
        if plan is None:
            raise ValueError("this chat has no plan")
        task = next((t for t in plan["tasks"] if str(t.get("id")) == wanted), None)
        if task is None:
            raise ValueError(f"the plan has no task {wanted}")
        if task.get("state") not in OPEN:
            raise ValueError(f"task {wanted} is not open")
        tasks = []
        for t in plan["tasks"]:
            if t is task:
                tasks.append({"id": t.get("id"), "title": t.get("title"),
                              "state": DROPPED_BY_OWNER})
            else:
                tasks.append(t)
        return {
            "kind": "plan", "turn": _latest_turn(records), "revision": next_revision(records),
            "origin": ORIGIN_OWNER, "action": ACTION_DROP, "chat": Path(log.path).stem,
            "base": plan.get("revision"), "task": wanted, "title": task.get("title"),
            "tasks": tasks, "counts": counts(tasks),
        }

    return _owner_write(log, build)


def owner_replan(log: Any) -> dict:
    """His request that aish revise the plan. The tasks are carried unchanged;
    the request is pending until the next model revision."""

    def build(records: list[dict]) -> dict:
        plan = current(records)
        if plan is None:
            raise ValueError("this chat has no plan to revise")
        return {
            "kind": "plan", "turn": _latest_turn(records), "revision": next_revision(records),
            "origin": ORIGIN_OWNER, "action": ACTION_REPLAN, "chat": Path(log.path).stem,
            "base": plan.get("revision"), "tasks": plan["tasks"], "counts": counts(plan["tasks"]),
        }

    return _owner_write(log, build)


# ---------------------------------------------------------------- reading the log


# Every line a plan reader needs holds this; the rest of a long log is skipped
# unparsed on the reminder's and the strip's path.
_NEEDLE = b'"plan"'


def plan_records(data: bytes) -> list[dict]:
    return objective._parse_bytes(data, _NEEDLE)


def read_records(path: Path | None) -> list[dict]:
    """The whole chat's records (the evidence reader needs every kind). Never
    raises: an unreadable log is a chat with no records."""
    if path is None:
        return []
    try:
        return objective._parse_bytes(Path(path).read_bytes())
    except OSError:
        return []


def view(data: bytes) -> dict[str, Any] | None:
    """What the strip and `/plan` show: the plan in force, its counts, and
    whether his replan request is pending. None when the chat has no plan."""
    records = plan_records(data)
    plan = current(records)
    if plan is None:
        return None
    out = {key: plan.get(key) for key in ("revision", "turn", "tasks")}
    out["changed_by"] = plan.get("changed_by") or ORIGIN_MODEL
    out["counts"] = counts(plan.get("tasks") or ())
    out["replan_requested"] = replan_pending(records) is not None
    return out


def read_view(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        return view(Path(path).read_bytes())
    except OSError:
        return None
