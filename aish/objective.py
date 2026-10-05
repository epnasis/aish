"""The Objective — why the owner is here, in one statement a model writes (#423, #432).

aish protects only the latest task prompt. The owner states what he wants briefly and
adds to it as he goes, often in Polish, so his goal is spread over many messages and
is never said as one thing. This module keeps it as one thing: a short statement of
his objective, written by a model in its own words from his typed messages, citing
the messages it rests on (owner decisions, 2026-09-29, epic #423).

Three parts:

- **The material.** The chat's live records, each citable item given a stable ref
  (`material`). The objective reads only one kind of item: a message the owner TYPED.
  His comments on approval cards are hints about the action in hand, never
  objective material, and they are excluded by construction (`owner_messages`).
- **The tracker.** An isolated role (`docs/roles.md`, charter
  `aish/charters/tracker.md`), called at task end off the interactive path
  (`track_at_boundary`). Its input is bounded: the current statement, the
  owner's messages since the last point the tracker accounted for (the NEW
  ones), and, as context, the earlier ones the statement rests on plus his most
  recent ones already read (`select_earlier`). It answers `revised` with a new
  statement and its cites, `unchanged`, or `unknown`, and code checks the answer
  (`validate_answer`) before anything records it.
- **The owner's edit** (`owner_edit`). A revision with `origin: owner`, never a
  user message, and the tracker cannot write over it: a revision it computed
  against the statement he replaced is dropped, and a later one can rest only on
  messages he typed after the edit.

Each revision is a renderless `objective` record carrying its trail of earlier
statements, so one record is the whole picture. It reaches the model through the
per-task reminder (`agent.objective_delta`) and the owner through the web strip and
`/objective`. `docs/objective.md`, contract §3.14.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import roles, secrets
from .session import synthetic_kind

TRACKER = "tracker"

ORIGIN_TRACKER = "tracker"
ORIGIN_OWNER = "owner"

# The tracker's closed vocabularies. Each can say "I cannot tell" (roles R4):
# a verdict that forces a reading makes guessing the cheapest answer.
REVISED = "revised"
UNCHANGED = "unchanged"
UNKNOWN = "unknown"
VERDICTS = (REVISED, UNCHANGED, UNKNOWN)
# How a revised statement relates to the one before it — the model's word.
CHANGES = ("evolved", "pivoted", UNKNOWN)
# Set by code, never by the model: there was no statement before, or the owner
# wrote this one himself.
CHANGE_NEW = "new"
CHANGE_EDITED = "edited"

# The tracker's input is bounded whatever the chat's length: the current
# statement, at most this many of the owner's unread messages (the newest), each
# cut at this many characters, and the `earlier` context below. Older unread
# ones are counted, not shown.
TRACKER_MESSAGES = 12
TRACKER_MESSAGE_CHARS = 2000
# What it is shown of what it has ALREADY read (epic #423, 2026-09-30): the
# messages the statement and its trail cite, and his newest this-many already
# read, so a new message is read against what he said before it. Shown only the
# new message, the tracker read "what are places to live for a family like
# mine?" in a chat about a family trip as a pivot to relocating; the
# experiment's control — the same charter words with no earlier messages —
# kept the false pivot, so it is the messages themselves that remove it.
TRACKER_RECENT = 12
# The most characters of earlier messages it is shown, newest kept first, so
# the input stays bounded whatever the lineage holds. Every earlier section in
# the experiment was under 2 300 characters.
TRACKER_EARLIER_CHARS = 12000
# The longest statement, a model's or his. The charter declares the same cap
# (`max_chars`), and a test holds the two equal.
STATEMENT_CHARS = 400

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

SYNTHETIC_PREFIX = "["


def _aish_wrote(content: str) -> bool:
    """Text aish put in the owner's slot, never his words. The bracket test
    is this module's own and wider than `session.synthetic_kind`; the shared
    classifier is asked TOO, so a marker added there (claude-max's rules note,
    `<system-reminder>`, 2026-10-05) can never reach the tracker as his."""
    return content.lstrip().startswith(SYNTHETIC_PREFIX) or bool(synthetic_kind(content))

# What a reader of the material is SHOWN of each item. The owner's words travel
# nearly whole; an answer is cut, since it is cited as evidence and never quoted.
OWNER_CHARS = 4000
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
        and not _aish_wrote(str(record.get("content") or ""))
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
    when. Unlike a line position it does not move when a Retry or a redaction
    rewrites OTHER records of the file; a record whose own text a redaction
    rewrites gets a new ref, and two identical records in the same second share
    one (timestamps are to the second)."""
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


# The cap the agent puts on a card comment as it enters a tool step
# (`agent.COMMENT_CHARS`). Spelled here, like STOPPED_ANSWER, and pinned equal
# by a test: only a comment exactly this long can have been cut.
COMMENT_CHARS = 400


def _scrubbed(text: str) -> str:
    """Audit `command` records carry the card comment as typed — the tool step's
    copy went through `secrets.scrub` (#323) and this one did not. Anything
    taken from the audit record is scrubbed the same way before a model sees it."""
    from . import secrets as secret_store

    try:
        return secret_store.scrub(text)
    except Exception:  # noqa: BLE001 — cannot scrub: do not pass it on at all
        return ""


def _whole_comment(comment: str, feedbacks: list[str]) -> str:
    """The owner's full sentence, when the tool step holds a CUT copy and an
    audit record of the same task holds it whole.

    Joined only when the step's comment is exactly `COMMENT_CHARS` long — the
    one case in which it can have been cut — and exactly one audit comment in
    the task begins with it. A shorter comment is already whole, and joining it
    by prefix attached another card's longer sentence to it (review finding)."""
    if len(comment) != COMMENT_CHARS:
        return comment
    head = _squash(comment)
    matches = [
        whole for whole in (_scrubbed(text) for text in feedbacks)
        if head and _squash(whole).startswith(head) and len(whole) > len(comment)
    ]
    return matches[0] if len(matches) == 1 else comment


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
                if _aish_wrote(content):
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
            items.extend(_command_items(task[position:], record, turn, has_calls, read_only))
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


def _following(rest: list[tuple[int, dict]]) -> list[dict]:
    """The records after a `command` record, up to the next one — so a
    `cmd_end` or an `[I ran …]` message is joined to THIS command only. `!cd`
    writes no `cmd_end`, and the next command's must not be taken for its."""
    out = []
    for _, r in rest[1:]:
        if r.get("kind") == "command":
            break
        out.append(r)
    return out


def _gated_read(command: str, read_only: frozenset[str]) -> bool:
    """An audit record of a card that approved a READ: a sensitive file
    (`read <path>`) or a read-only tool behind an egress card
    (`tool read_url(…)`). Approved, and still changes nothing."""
    if command.startswith("read "):
        return True
    if command.startswith("tool "):
        return command[len("tool "):].partition("(")[0].strip() in read_only
    return False


def _command_items(
    rest: list[tuple[int, dict]], record: dict, turn: int, has_calls: bool,
    read_only: frozenset[str] = frozenset(),
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
        after = _following(rest)
        exit_code = next((r.get("exit_code") for r in after if r.get("kind") == "cmd_end"), None)
        output = next(
            (
                str(r.get("content") or "").partition("\n")[2]
                for r in after
                if r.get("kind") == "message" and r.get("role") == "user"
                and str(r.get("content") or "").startswith(f"[I ran `{command}`")
            ),
            "",
        )
        kind = RAN if exit_code == 0 else RAN_UNCHECKED
        text = f"he ran: {command}" + (f"\n{output.strip()}" if output.strip() else "")
        return [Item(ref, kind, turn, _cut(text, ACT_CHARS * 2))]
    if has_calls:
        return []
    verdict, comment = _feedback(decision)
    comment = _scrubbed(comment) if comment else ""
    out: list[Item] = []
    if comment and verdict.startswith(("denied", "approved", "edited")):
        out.append(Item(ref, COMMENT, turn, _cut(comment, OWNER_CHARS)))
    if verdict.startswith("denied"):
        if not comment:
            out.append(Item(ref, DENIAL, turn, _cut(f"denied — {command}", ACT_CHARS)))
        return out
    # An EDITED approval ran the owner's rewrite (`old => new`); an approval
    # with a comment was held, not run. A gated read changed nothing.
    ran_it = verdict.startswith("edited") or (verdict.startswith("approved") and not comment)
    if ran_it and not _gated_read(command, read_only):
        out.append(Item(_content_ref("c", record.get("ts"), command, decision, "ran"),
                        ACTION, turn, _cut(command, ACTION_ARGS_CHARS)))
    return out


def owner_messages(records: Iterable[dict]) -> list[Item]:
    """The messages the owner TYPED, in order — the only material the objective
    is made of.

    Filtered by kind from `material`, so the refs are the ones every other
    reader of the chat uses. A card comment is his words too, but it is a hint
    about the action in hand (owner decision, 2026-09-29): it never enters the
    objective, and this filter is the one place that makes it so.

    In a TRIGGERED chat (a schedule, an e-mail, a webhook) the opening message
    is the trigger's prompt, which aish composed, with no marker to classify it
    by; position and provenance identify it, exactly as replay does
    (`SessionLog.reconstruct_events`), so it is not his either.
    """
    records = list(records)
    origin = next(
        (str(r.get("origin") or "") for r in records if r.get("kind") == "origin"), "user"
    )
    owned = [i for i in material(records, read_only=frozenset()) if i.kind == OWNER]
    return owned[1:] if origin not in ("", "user") else owned


# ---------------------------------------------------------------- revisions


def is_revision(step: dict) -> bool:
    """An `objective` step of the current shape. A #424 ledger record (goals,
    or an extractive floor) has no statement and is ignored by every reader."""
    return (
        step.get("kind") == "objective"
        and isinstance(step.get("statement"), str)
        and bool(step["statement"].strip())
    )


def current(records: Iterable[dict]) -> dict | None:
    """The newest LIVE revision (L7), or None when the chat has none."""
    found = None
    for record in records:
        step = _step(record)
        if is_revision(step) and not record.get("superseded"):
            found = step
    return found


def next_revision(records: Iterable[dict]) -> int:
    """1 + the highest revision in the file. Superseded ones count, and so do
    #424's: an id that was handed out is never reissued (the `last_turn` rule)."""
    numbers = [
        int(_step(r).get("revision") or 0) for r in records if _step(r).get("kind") == "objective"
    ]
    return max(numbers, default=0) + 1


def accounted_to(records: Iterable[dict]) -> int:
    """The last turn whose owner messages the objective has already read: the
    current revision's `covers_to_turn`, or a later tracker call that read
    newer messages and left the statement as it was. Live records only, so a
    Retry that discards a task discards what it covered."""
    turn = 0
    for record in records:
        if record.get("superseded"):
            continue
        step = _step(record)
        if is_revision(step) or (
            step.get("kind") == "role" and step.get("charter") == TRACKER
        ):
            covered = step.get("covers_to_turn")
            if isinstance(covered, int) and not isinstance(covered, bool):
                turn = max(turn, covered)
    return turn


def _wrap(chat: str, refs: Iterable[str]) -> list[dict[str, str]]:
    return [{"session": chat, "ref": ref} for ref in refs]


def _trail_after(base: dict | None, left: str) -> list[dict]:
    """The trail a revision replacing `base` carries: the base's own trail,
    then the base itself, labelled with how it was left."""
    if base is None:
        return []
    entry = {
        "revision": base.get("revision"),
        "turn": base.get("turn"),
        "origin": base.get("origin"),
        "statement": base.get("statement"),
        "cites": list(base.get("cites") or ()),
        "left": left,
    }
    return [dict(e) for e in base.get("trail") or () if isinstance(e, dict)] + [entry]


# ---------------------------------------------------------------- the input


def compose_input(
    chat: str,
    boundary: int,
    base: dict | None,
    messages: list[Item],
    omitted: int = 0,
    earlier: Iterable[Item] = (),
) -> str:
    """The tracker's ONE input: the current statement, the owner's messages it
    has already read that give the new ones their sense (`earlier`), and the
    ones it has not read yet (`messages`, the new ones).

    JSON, because the validator reads it back: what a cite is checked against
    is exactly what the model was shown, in production and in the exam alike.
    """
    payload = {
        "chat": chat,
        "boundary_turn": boundary,
        "current": (
            {
                "statement": base.get("statement"),
                "origin": base.get("origin"),
                "revision": base.get("revision"),
            }
            if base
            else None
        ),
        "earlier": [_shown(m) for m in earlier],
        "messages": [_shown(m) for m in messages],
        "omitted": omitted,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _shown(m: Item) -> dict[str, Any]:
    return {"ref": m.ref, "turn": m.turn, "text": _cut(m.text, TRACKER_MESSAGE_CHARS)}


def select_messages(items: list[Item], after: int, upto: int) -> tuple[list[Item], int]:
    """The owner messages in (`after`, `upto`], the newest `TRACKER_MESSAGES`
    of them, and how many older ones were left out."""
    unread = [i for i in items if after < i.turn <= upto]
    shown = unread[-TRACKER_MESSAGES:]
    return shown, len(unread) - len(shown)


def lineage_refs(base: dict | None) -> list[str]:
    """The refs the statement rests on — its own cites and those of every
    earlier statement in its trail, oldest first. A revision cites only what IT
    added, so its own cites alone are not what led to it."""
    if not base:
        return []
    refs: list[str] = []
    for entry in [*(base.get("trail") or ()), base]:
        if not isinstance(entry, dict):
            continue
        for cite in entry.get("cites") or ():
            ref = str(cite.get("ref") if isinstance(cite, dict) else cite or "")
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def edit_floor(history: Iterable[dict], base: dict | None) -> int:
    """The turn up to which he had every message in front of him when he last
    wrote the objective himself, in this statement's lineage; 0 when he never
    did. Nothing at or before it is shown to the tracker, even as context, so a
    revision after his edit still rests only on what he said later.

    The base carries its own `covers_to_turn`; a trail entry does not, so an
    edit further back is looked up by its revision number, and its `turn` (the
    latest turn when he saved) stands in when that record is not found."""
    if not base:
        return 0
    lineage = [e for e in [*(base.get("trail") or ()), base] if isinstance(e, dict)]
    edit = next((e for e in reversed(lineage) if e.get("origin") == ORIGIN_OWNER), None)
    if edit is None:
        return 0
    covered = _int(edit.get("covers_to_turn"))
    if covered is None:
        # Superseded records are searched too, on purpose: a revision number is
        # never reissued, and what he had in front of him when he saved does not
        # stop being true because a Retry later superseded the record.
        covered = next(
            (
                found
                for record in history
                if is_revision(step := _step(record))
                and step.get("origin") == ORIGIN_OWNER
                and step.get("revision") == edit.get("revision")
                and (found := _int(step.get("covers_to_turn"))) is not None
            ),
            None,
        )
    return covered if covered is not None else _int(edit.get("turn")) or 0


def select_earlier(
    items: list[Item], base: dict | None, after: int, floor: int = 0
) -> tuple[list[Item], int]:
    """The owner messages already read that the tracker is shown as context:
    those the statement's lineage cites (the `TRACKER_MESSAGES` most recently
    cited: the last in trail order, a revision's own cites in the order it gave
    them) and his newest `TRACKER_RECENT` already read — each once, in turn order,
    only from turns in (`floor`, `after`]. Bounded at `TRACKER_EARLIER_CHARS`,
    newest kept first; returns them and how many that bound left out."""
    read = [i for i in items if floor < i.turn <= after]
    by_ref = {i.ref: i for i in read}
    cited = [by_ref[r] for r in lineage_refs(base) if r in by_ref][-TRACKER_MESSAGES:]
    wanted = {i.ref for i in cited} | {i.ref for i in read[-TRACKER_RECENT:]}
    # Once per ref: two identical id-less messages in one second share a digest ref.
    chosen = [i for i in read if i.ref in wanted and by_ref[i.ref] is i]
    kept: list[Item] = []
    spent = 0
    for item in reversed(chosen):
        spent += len(_cut(item.text, TRACKER_MESSAGE_CHARS))
        if spent > TRACKER_EARLIER_CHARS:
            break
        kept.append(item)
    return kept[::-1], len(chosen) - len(kept)


@dataclass
class Context:
    """What a tracker answer is validated against — read back from its input."""

    statement: str
    messages: dict[str, Item]


def parse_input(text: str) -> Context:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"the tracker's input is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the tracker's input is not a JSON object")
    given = raw.get("current")
    base: dict = given if isinstance(given, dict) else {}
    messages = {}
    # A cite may name a message under `earlier` as well as a new one: a revision
    # can rest on what he said before, and it was shown both.
    for entry in [*(raw.get("messages") or ()), *(raw.get("earlier") or ())]:
        if isinstance(entry, dict) and entry.get("ref") and str(entry["ref"]) not in messages:
            ref = str(entry["ref"])
            turn = int(entry.get("turn") or 0)
            messages[ref] = Item(ref, OWNER, turn, str(entry.get("text") or ""))
    return Context(statement=str(base.get("statement") or ""), messages=messages)


# ---------------------------------------------------------------- the answer


@dataclass(frozen=True)
class Answer:
    """A validated tracker answer: the value `roles.run` returns."""

    verdict: str
    statement: str = ""
    change: str = ""
    cites: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "statement": self.statement,
            "change": self.change,
            "cites": list(self.cites),
        }


def _squash(text: str) -> str:
    return " ".join(text.split())


def validate_answer(shape: roles.Shape, payload: Any, inputs: dict[str, str]) -> Answer:
    """A tracker answer as an `Answer`, or `ValueError` with a message the model
    can act on (it is fed back on the one corrective retry)."""
    ctx = parse_input(inputs.get(TRACKER_INPUT, ""))
    if not isinstance(payload, dict):
        raise ValueError("the reply must be a JSON object")
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)} (got {verdict!r})")
    if verdict != REVISED:
        return Answer(verdict)
    statement = roles.capped(str(payload.get("statement") or ""), shape.caps["max_chars"])
    if not statement:
        raise ValueError('a "revised" verdict needs the new statement')
    raw = payload.get("cites")
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            'a "revised" statement must cite at least one of the messages it rests on — '
            'copy a "ref" exactly from an item under "messages" or "earlier"'
        )
    if len(raw) > shape.caps["max_cites"]:
        raise ValueError(f"at most {shape.caps['max_cites']} cites")
    cites: list[str] = []
    for cite in raw:
        ref = str(cite.get("ref") if isinstance(cite, dict) else cite or "").strip()
        if ref not in ctx.messages:
            raise ValueError(
                f"cite {ref!r} names none of the messages you were given — copy a "
                '"ref" exactly from an item under "messages" or "earlier"'
            )
        if ref not in cites:
            cites.append(ref)
    if _squash(statement) == _squash(ctx.statement):
        # The same words are not a revision, whatever the verdict said.
        return Answer(UNCHANGED)
    if not ctx.statement:
        change = CHANGE_NEW  # code knows there was nothing before; the model need not say
    else:
        change = str(payload.get("change") or "").strip().lower()
        if change not in CHANGES:
            raise ValueError(f"change must be one of {', '.join(CHANGES)} (got {change!r})")
    return Answer(REVISED, statement, change, tuple(cites))


# The name of the tracker's one input, declared in its charter.
TRACKER_INPUT = "objective"


def contract_text(shape: roles.Shape) -> str:
    """The output contract, generated from the declared caps and this module's
    vocabularies, so the words the model is given and the rules code enforces
    cannot drift apart."""
    return "\n".join(
        [
            "Reply with ONE JSON object and nothing else. No prose before or after it, "
            "no code fence.",
            "",
            "{",
            f'  "verdict": one of {", ".join(json.dumps(v) for v in VERDICTS)},',
            f'  "statement": the objective, at most {shape.caps["max_chars"]} characters '
            '— only when the verdict is "revised",',
            f'  "change": one of {", ".join(json.dumps(c) for c in CHANGES)} '
            '— only when the verdict is "revised" and there was a current statement,',
            '  "cites": ["<ref>", ...] — only when the verdict is "revised": the refs of '
            f"the messages it rests on, 1 to {shape.caps['max_cites']}",
            "}",
            "",
            'A ref is copied exactly from the "ref" of an item under "messages" or '
            '"earlier". An answer that breaks a rule is rejected and you are asked once more.',
        ]
    )


def tally(value: Answer) -> dict[str, dict[str, int]]:
    """Per-call counts, for the role record's `flags` (the counters)."""
    out = {"verdict": {value.verdict: 1}}
    if value.change:
        out["change"] = {value.change: 1}
    return out


# ---------------------------------------------------------------- exam assertions


def _expect_verdict(expected: Any, value: Answer) -> list[str]:
    allowed = [str(v) for v in expected or ()]
    if value.verdict not in allowed:
        return [f"expected verdict in {allowed}, got {value.verdict!r}"]
    return []


def _expect_change(expected: Any, value: Answer) -> list[str]:
    allowed = [str(v) for v in expected or ()]
    if value.change not in allowed:
        return [f"expected change in {allowed}, got {value.change!r}"]
    return []


def _expect_cites(expected: Any, value: Answer) -> list[str]:
    """Each group: at least one of these refs is cited."""
    return [
        f"cites none of {group}"
        for group in expected or ()
        if not any(str(ref) in value.cites for ref in group)
    ]


def _expect_mentions_any(expected: Any, value: Answer) -> list[str]:
    """Each group: at least one of these substrings is in the statement.
    Groups, because the model may write in either of the owner's languages."""
    text = value.statement.casefold()
    return [
        f"the statement mentions none of {group}"
        for group in expected or ()
        if not any(str(word).casefold() in text for word in group)
    ]


def _expect_absent(expected: Any, value: Answer) -> list[str]:
    text = value.statement.casefold()
    return [
        f"the statement carries {needle!r}"
        for needle in expected or ()
        if str(needle).casefold() in text
    ]


ASSERTIONS: dict[str, Callable[[Any, Answer], list[str]]] = {
    "verdict_in": _expect_verdict,
    "change_in": _expect_change,
    "cites_any": _expect_cites,
    "mentions_any": _expect_mentions_any,
    "absent": _expect_absent,
}


# ---------------------------------------------------------------- emission


@dataclass(frozen=True)
class Boundary:
    """A task end, captured synchronously when it happened: the log, how many
    bytes it held, the bytes of its LAST line, and the task's turn. Everything
    else is read later, off the interactive path.

    `tail` is what makes the byte offset trustworthy. A Retry or a redaction
    rewrites the file in place, shifting every offset after the first line it
    touches; a call that trusted `upto` alone would read a torn prefix that
    is neither the chat before the rewrite nor after it. So the tracker reads
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
            # task's tracker finishing now) must not become this boundary's
            # tail, or an unrewritten chat is recorded as rewritten.
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


def _parse_bytes(data: bytes, needle: bytes | None = None) -> list[dict]:
    """The records in `data`. With `needle`, only lines containing it are
    decoded — the cheap path for a reader that wants one record kind out of a
    log that can run to megabytes."""
    out: list[dict] = []
    for line in data.split(b"\n"):
        if needle is not None and needle not in line:
            continue
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


# Every line an objective reader needs holds one of these; the rest of a
# multi-megabyte log is never decoded on the reminder's or the strip's path.
_OBJECTIVE_NEEDLE = b'"objective"'


def current_of(data: bytes) -> dict | None:
    """The current revision in a log's bytes."""
    return current(_parse_bytes(data, _OBJECTIVE_NEEDLE))


def read_current(path: Path | None) -> dict | None:
    """The current revision of the chat at `path`, or None. Never raises: it
    runs on the task path (the reminder) and the attach path (the strip)."""
    if path is None:
        return None
    try:
        return current_of(Path(path).read_bytes())
    except (OSError, ValueError):
        return None


def last_tracker_role(data: bytes) -> dict | None:
    """The newest live tracker `role` record — what the strip says when there
    is no statement yet: whether the tracker ran, and what it answered."""
    found = None
    for record in _parse_bytes(data, b'"role"'):
        step = _step(record)
        if step.get("kind") == "role" and step.get("charter") == TRACKER and not record.get(
            "superseded"
        ):
            found = step
    return found


def track(
    boundary: Boundary,
    *,
    chat_fn: Callable[..., Any] | None = None,
    model_name: str = "",
    check_admission: bool = True,
    charter: roles.Charter | None = None,
    records: list[dict] | None = None,
    history: list[dict] | None = None,
) -> tuple[list[dict], int | None]:
    """The records one task end produces, in the order they are written, and
    the revision number of the statement the tracker was shown (None: none).

    `records` is the chat as it stood at the boundary — the messages come from
    it. `history` is the whole file as it stands now, which is where the base,
    the coverage and the next revision number come from: an earlier call may
    have written AFTER this boundary was captured.

    Nothing at all when there is no unread owner message: no call is made.
    Otherwise always a `role` record (the D7 record, whatever the outcome —
    "unadmitted" is a fact in the log, never an absence), then an `objective`
    record when the tracker revised the statement.
    """
    if records is None:
        records = read_records(boundary.path, boundary.upto)
    if history is None:
        history = records
    chat = boundary.path.stem
    base = current(history)
    base_revision = int(base["revision"]) if base and base.get("revision") else None
    after = accounted_to(history)
    if after >= boundary.turn:
        return [], base_revision  # a later boundary, or his edit, already read this far
    owned = owner_messages(records)
    messages, omitted = select_messages(owned, after, boundary.turn)
    if not messages:
        return [], base_revision
    earlier, earlier_omitted = select_earlier(owned, base, after, edit_floor(history, base))

    try:
        charter = charter or roles.load_charters()[TRACKER]
    except (roles.CharterError, KeyError) as exc:
        why = f"the tracker charter does not load: {exc}"
        return [_skip_record(boundary.turn, why)], base_revision

    text = compose_input(chat, boundary.turn, base, messages, omitted, earlier)
    result = roles.run(
        charter,
        {TRACKER_INPUT: text},
        (),
        model_spec=boundary.model_spec,
        chat=chat_fn,
        model_name=model_name,
        state_dir=boundary.state_dir,
        check_admission=check_admission,
    )
    role = role_record(charter, result, boundary.turn)
    role["input"]["messages"] = len(messages)
    role["input"]["omitted"] = omitted
    role["input"]["earlier"] = len(earlier)
    role["input"]["earlier_omitted"] = earlier_omitted
    out = [role]
    value = result.value if result.status == roles.Status.OK else None
    if not isinstance(value, Answer):
        return out, base_revision
    role["verdict"] = value.verdict
    if value.verdict == UNKNOWN:
        return out, base_revision
    role["covers_to_turn"] = boundary.turn
    if value.verdict == REVISED:
        out.append(
            {
                "kind": "objective",
                "turn": boundary.turn,
                "revision": next_revision(history),
                "origin": ORIGIN_TRACKER,
                "chat": chat,
                "covers_to_turn": boundary.turn,
                "statement": value.statement,
                "cites": _wrap(chat, value.cites),
                "change": value.change,
                "base": base_revision,
                "charter": charter.name,
                "version": charter.version,
                "model": result.model,
                "trail": _trail_after(base, value.change),
            }
        )
    return out, base_revision


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


_TASK_START = b'"kind": "task_start"'


def _outcome_of(out: list[dict], turn: int) -> dict | None:
    """The tracker's result as an `outcome` row on the turn it read (contract
    §3.18): a revised objective, or a call that failed on a model it named.
    Unchanged, not admitted and no-model-to-run-on draw nothing — they would be
    a row on nearly every turn, and the strip already says them."""
    revision = next((r for r in out if r.get("kind") == "objective"), None)
    role = next((r for r in out if r.get("kind") == "role"), None)
    step: dict[str, Any] = {"kind": "outcome", "of": "objective", "turn": turn,
                            "after_turn": True}
    if revision is not None:
        return {**step, "status": "revised", "revision": revision.get("revision"),
                "change": revision.get("change"), "statement": revision.get("statement")}
    if role is not None and role.get("model") and role.get("status") in (
        roles.Status.INVALID, roles.Status.UNAVAILABLE,
    ):
        return {**step, "status": str(role["status"]), "model": role["model"],
                "why": secrets.scrub(str(role.get("why") or ""))[:400]}
    return None


def _skip_record(turn: int, why: str) -> dict:
    return {
        "kind": "role",
        "turn": turn,
        "charter": TRACKER,
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
OUTRANKED = (
    "the objective changed while the tracker was reading (an owner edit, or another "
    "revision), so this answer was computed against a statement that is no longer current"
)


def _revision_number(step: dict | None) -> int | None:
    return int(step["revision"]) if step and step.get("revision") else None


def track_at_boundary(boundary: Boundary, log: Any) -> list[dict]:
    """Track and write. NEVER raises: this runs after the answer, and nothing
    it does may reach the task.

    `log` is the chat's `SessionLog`. Both the read and the write go through
    its write lock, which is the lock every rewrite of the file holds: the read
    is `log.snapshot()`, and the write is `log.append_steps_if`, which appends
    only if the boundary's last line is still where it was AND the current
    revision is still the one the tracker was shown. So a Retry pressed while
    the model was thinking, or an owner edit saved meanwhile, keeps the `role`
    record (the call was made and paid for) with `discarded` set, and drops
    the revision — the owner's edit always outranks the tracker.

    One call per chat at a time (a per-path lock).
    """
    if disabled():
        return []
    written: list[dict] = []
    try:
        with _lock_for(boundary.path):
            started = time.perf_counter()
            data = log.snapshot()
            base_revision: int | None = None
            if not still_there(data, boundary):
                out = [_skip_record(boundary.turn, REWRITTEN)]
            else:
                try:
                    out, base_revision = track(
                        boundary,
                        records=_parse_bytes(data[: boundary.upto]),
                        history=_parse_bytes(data),
                    )
                except Exception as exc:  # noqa: BLE001 — a bug must not reach the task
                    out = [
                        _skip_record(
                            boundary.turn,
                            f"the tracker raised {type(exc).__name__}: {exc}"[:300],
                        )
                    ]
                    out[0]["ms"] = int((time.perf_counter() - started) * 1000)
            if not out:
                return written
            if outcome := _outcome_of(out, boundary.turn):
                # In the same conditional append: drawn only if what it
                # reports was written (§3.18).
                out.append(outcome)
            why_dropped: list[str] = []

            def unchanged() -> bool:
                now = log.snapshot_locked()
                if not still_there(now, boundary):
                    why_dropped.append(REWRITTEN)
                    return False
                if any(r.get("kind") == "objective" for r in out) and (
                    _revision_number(current_of(now)) != base_revision
                ):
                    why_dropped.append(OUTRANKED)
                    return False
                if _TASK_START in now[boundary.upto:]:
                    # His next turn began while this one was read: the
                    # revision still stands (it may), but a row written now
                    # would land in THAT turn's records, and a row about turn
                    # N on turn N+1's card is a false placement (§3.18).
                    out[:] = [r for r in out if r.get("kind") != "outcome"]
                return True

            if log.append_steps_if(out, unchanged):
                written.extend(out)
            else:
                why = why_dropped[-1] if why_dropped else REWRITTEN
                roles_only = [
                    {**r, "discarded": why}
                    for r in out
                    if r.get("kind") == "role"
                ]
                for record in roles_only:
                    record.pop("covers_to_turn", None)  # it covered nothing that stands
                log.append_steps_if(roles_only, lambda: True)
                written.extend(roles_only)
    except Exception:  # noqa: BLE001 — a write refused (a trashed chat) ends it quietly
        pass
    return written


# ---------------------------------------------------------------- the owner's edit


def owner_edit(log: Any, statement: str) -> dict:
    """Write the owner's own statement as a revision — `origin: owner`, never a
    user message — and return it. `ValueError` for an empty statement.

    Its `covers_to_turn` is the chat's latest turn: he had every message up to
    now in front of him when he wrote it, so the tracker's next input starts
    after them, and it can move his statement only on something he says later.
    Written under the log's write lock, and only if the chat did not change
    between the read and the write (retried a few times, then refused).
    """
    text = roles.capped(statement, STATEMENT_CHARS)
    if not text:
        raise ValueError("an objective can't be empty")
    path = Path(log.path)
    chat = path.stem
    for _ in range(3):
        data = log.snapshot()
        records = _parse_bytes(data)
        base = current(records)
        turns = [i.turn for i in material(records, read_only=frozenset())]
        latest = max(turns, default=0)
        record = {
            "kind": "objective",
            "turn": latest,
            "revision": next_revision(records),
            "origin": ORIGIN_OWNER,
            "chat": chat,
            "covers_to_turn": max(latest, accounted_to(records)),
            "statement": text,
            "cites": [],
            "change": CHANGE_EDITED,
            "base": _revision_number(base),
            "trail": _trail_after(base, CHANGE_EDITED),
        }
        size = len(data)

        def unchanged(size: int = size, data: bytes = data) -> bool:
            now = log.snapshot_locked()
            return len(now) == size and now[-TAIL_BYTES:] == data[-TAIL_BYTES:]

        if log.append_steps_if([record], unchanged):
            return record
    raise ValueError("the chat kept changing while the objective was being saved; try again")


# ---------------------------------------------------------------- what the screen shows


def view(data: bytes) -> dict[str, Any]:
    """What the strip and `/objective` show, from one read of the log: the
    current revision (or none) and, when there is none, what the tracker last
    did — so "no objective yet" can say whether anything tried."""
    revision = current_of(data)
    out: dict[str, Any] = {"objective": None, "tracker": None}
    if revision is not None:
        out["objective"] = {
            key: revision.get(key)
            for key in (
                "revision", "turn", "origin", "statement", "cites", "change", "trail",
                "model", "covers_to_turn",
            )
        }
        out["objective"]["sources"] = _sources(data, revision.get("cites") or ())
    role = last_tracker_role(data)
    if role is not None:
        out["tracker"] = {
            key: role[key]
            for key in ("turn", "status", "verdict", "why", "model", "discarded")
            if role.get(key) not in (None, "")
        }
    return out


# How much of each cited message the strip's sheet shows as a source.
SOURCE_CHARS = 300


def _sources(data: bytes, cites: Iterable[Any]) -> list[dict[str, Any]]:
    """The owner messages a statement cites, so he can check the reading
    against what he actually wrote. Found by the message's own id (`m:<id>`),
    which touches only the lines that carry it; a digest ref (`m#…`, a log
    from before ids) cannot be found that way and is listed without its text
    rather than paying for a parse of the whole chat."""
    out: list[dict[str, Any]] = []
    for cite in cites:
        ref = str(cite.get("ref") if isinstance(cite, dict) else cite or "")
        text = None
        if ref.startswith("m:"):
            ident = ref[2:]
            needle = json.dumps({"turn": ident}, ensure_ascii=False)[1:-1].encode()
            for record in _parse_bytes(data, needle):
                if (
                    record.get("kind") == "message"
                    and record.get("role") == "user"
                    and str(record.get("turn") or "") == ident
                    and not record.get("superseded")
                ):
                    text = _cut(str(record.get("content") or ""), SOURCE_CHARS)
        out.append({"ref": ref, "text": text})
    return out


def read_view(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"objective": None, "tracker": None}
    try:
        return view(Path(path).read_bytes())
    except OSError:
        return {"objective": None, "tracker": None}
