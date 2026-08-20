"""`aish explain` — read back what governed a turn, from the records alone.

The governance records are good and nothing reads them (#214). Asked whether a
rule had actually verified a price in one session, the only way to answer was a
hand-written pass over the JSONL; the answer was in there and exact, and getting
it took a scripting session. Every one of those records renders nowhere by
design, so there is no UI path either.

**The one law this module exists to enforce** (docs/trace-contract.md §0): an
explanation is ASSEMBLED FROM RECORDED EVIDENCE, never re-derived from source.
Asked once why its output was truncated, a model went grepping through aish's
own source, found one of three truncators and confidently named the wrong one.
So this reader makes no model call, opens no rule file, and imports nothing that
could tell it how aish behaves *today*. Where the log cannot answer, it says so.

**Three states, never two.** "Not recorded" and "recorded, and it was empty" are
different answers that route to different repairs (corollary 2), and deletion
adds a third: redaction and the Retry rewind both delete records in place, and
the evidence store can be purged, so "recorded, then removed" must not collapse
into "never recorded". Every field below reports which of the three it is.

**Joins are by id, never by position.** Trace records carry an int `turn`, and a
tool `call` number joins a gate verdict to the action it governed. The one thing
this module infers positionally is the turn BRACKET — records between a
`task_start` and the next one — because that is how the turn boundary is
written, not a guess about ordering. Note the collision the log warns about:
`message` records carry a top-level `turn` that is a client-minted STRING event
id, unrelated to the int counter on trace steps.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import evidence
from .session import synthetic_kind

NOT_RECORDED = "not recorded"
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"

# Steps rendered in their own sections rather than in the generic trace dump.
_SECTIONED = frozenset(
    {"brief", "rule_eval", "binding", "gate", "context", "knowledge", "trim",
     "tool_start", "tool", "thinking", "thinking_start", "thinking_cancel"}
)


def state_dir() -> Path:
    """Where sessions and the evidence store live. Matches the entry points."""
    return Path(os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish")))


@dataclass
class Turn:
    """One turn's records, bracketed by task_start. `ordinal` is this turn's
    position in the file; `counter` is the agent's own int turn id where the
    records carry one — they can differ, because reopening a chat restarts the
    agent's counter and the log deliberately keeps both."""

    ordinal: int
    counter: int | None = None
    # The client-minted STRING event id (#202) carried on this turn's user
    # message — the only identity that is stable live, on replay, and in the
    # log. The web panel addresses turns by it: a browser cannot count
    # ordinals, because its first paint is capped and its "turn 4" is not the
    # log's turn 4 on any long chat. An id cannot be counted wrong.
    turn_id: str = ""
    ts: str = ""
    prompt: str = ""
    status: str | None = None
    error: str = ""
    records: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[dict]:
        return [s for s in self.steps if s.get("kind") == kind]


@dataclass
class Log:
    path: Path
    turns: list[Turn]
    kinds_present: set[str]  # every trace-step kind anywhere in the file
    redactions: list[dict]
    title: str = ""

    def wrote(self, kind: str) -> bool:
        """Did the aish that wrote this log emit this kind AT ALL? A kind absent
        from the whole file means the log predates the record — a different
        answer from a turn that has none of them, and the reader must not
        present the first as the second."""
        return kind in self.kinds_present


def _records(path: Path) -> list[dict]:
    """Every parseable record, in file order. Tolerant of torn lines by design:
    a reader that raises on one bad line cannot explain the session that
    produced it, and a torn line is exactly what every other reader of this file
    silently skips."""
    out: list[dict] = []
    for line in path.read_text(errors="replace").splitlines():
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


def _starts_a_turn(record: dict, current: Turn | None, on_task_start: bool) -> bool:
    """Where one turn ends and the next begins.

    `task_start` is the boundary when the log has any: it is written FIRST and
    carries the prompt verbatim, so the seed-time records (`rule_eval`,
    `context`, `knowledge`) land inside the turn they describe rather than
    trailing the previous one.

    But it is written by the CLI and the server, not by the agent — so a log
    from any other writer, or one predating the bracket, has none. Falling back
    to the user's own messages keeps those readable instead of reporting a
    session with no turns, which is the "a pre-contract log is not a failure"
    rule (#214). The fallback is chosen per FILE, never per record: mixing the
    two boundaries in one log would double-count every web turn.
    """
    kind = record.get("kind")
    if on_task_start:
        return kind == "task_start"
    if kind != "message" or record.get("role") != "user":
        return False
    # aish's own [aish: …] notes are not turns — live they never reached the
    # transcript at all, and one landing mid-turn would split it in two.
    if synthetic_kind(str(record.get("content") or "")) == "note":
        return False
    return current is None or bool(current.prompt)


def load(path: os.PathLike | str) -> Log:
    """Parse a session log into turn brackets."""
    path = Path(path)
    records = _records(path)
    on_task_start = any(r.get("kind") == "task_start" for r in records)
    turns: list[Turn] = []
    kinds: set[str] = set()
    redactions: list[dict] = []
    title = ""
    current: Turn | None = None
    for record in records:
        kind = record.get("kind")
        if kind == "title":
            title = str(record.get("title") or "")
        if kind == "redact":
            redactions.append(record)
        if _starts_a_turn(record, current, on_task_start):
            current = Turn(ordinal=len(turns) + 1, ts=str(record.get("ts") or ""))
            turns.append(current)
        if current is None:
            # Records before the first boundary: session setup (model, cwd,
            # title). Inventing a turn 0 for them would put a bracket in the
            # file that the writer never wrote.
            continue
        if kind == "task_start":
            current.prompt = str(record.get("prompt") or "")
        current.records.append(record)
        if kind == "task_end":
            current.status = record.get("status")
            current.error = str(record.get("error") or "")
        elif kind == "message":
            current.messages.append(record)
            if record.get("role") == "user" and not current.prompt:
                current.prompt = str(record.get("content") or "")
            if record.get("role") == "user" and not current.turn_id:
                current.turn_id = str(record.get("turn") or "")
        elif kind == "trace":
            step = record.get("step")
            if isinstance(step, dict):
                current.steps.append(step)
                kinds.add(str(step.get("kind")))
                if current.counter is None and isinstance(step.get("turn"), int):
                    current.counter = step["turn"]
    return Log(path=path, turns=turns, kinds_present=kinds, redactions=redactions, title=title)


def resolve(target: str, root: os.PathLike | str | None = None) -> list[Path]:
    """Session logs matching `target`: a path, a bare filename, or a substring.
    Returns every match so an ambiguous name is reported rather than guessed at
    — picking one silently is how a diagnosis ends up about the wrong chat."""
    direct = Path(target).expanduser()
    if direct.is_file():
        return [direct]
    root = Path(root) if root is not None else state_dir()
    return sorted(p for p in root.glob("session-*.jsonl") if target in p.name)


def _menu(step: dict, root: os.PathLike | str | None) -> tuple[str, list[dict] | None]:
    """(status, parsed menu). The status is the three-state answer for the
    bytes: resolved, purged, or never stored."""
    digest = str((step.get("tools") or {}).get("digest") or "")
    if not digest:
        return NOT_RECORDED, None
    blob = evidence.get(digest, root)
    if blob is None:
        return "bytes purged or stored elsewhere", None
    try:
        parsed = json.loads(blob)
    except ValueError:
        return "stored bytes are unreadable", None
    return "resolved", parsed if isinstance(parsed, list) else None


def _fmt_evidence(ev: dict) -> str:
    """A rule verdict's evidence, as recorded. Scored verdicts carry the floor
    that was IN FORCE, which is the whole point — the floor in the file today
    may not be the one that decided."""
    if not isinstance(ev, dict):
        return ""
    if "sim" in ev:
        floor = ev.get("floor")
        bit = f"sim {ev['sim']}"
        if floor is not None:
            bit += f" vs floor {floor}"
        if ev.get("rail") is not None:
            bit += f", rail {ev['rail']}"
        return bit
    if "matched" in ev:
        return f"matched={ev['matched']}" + (f" span {ev['span']}" if ev.get("span") else "")
    if "field" in ev:
        return f"{ev['field']}={ev.get('value')!r} required {ev.get('required')}"
    if "error" in ev:
        return f"judge unavailable: {ev['error']}"
    return ", ".join(f"{k}={v}" for k, v in list(ev.items())[:4])


def _gate_line(gate: dict) -> str:
    who = f"{gate.get('gate')}" + (f"/{gate.get('rule')}" if gate.get("rule") else "")
    verdict = gate.get("verdict", gate.get("decision", "?"))
    detail = _fmt_evidence(gate.get("evidence") or {})
    return f"{who} at {gate.get('at')} → {verdict}" + (f" — {detail}" if detail else "")


PASSED_VERDICTS = ("allowed", "allow", "ok", "pass")
# `advised` is an answer that WAS DELIVERED, carrying a not-followed note — not
# a termination. The contract records that conflating the two had the ledger
# counting shipped answers as stops, so it is worth reading but must never be
# summarised as a refusal.
ADVISED_VERDICTS = ("advised",)


def _verdict_of(gate: dict) -> str:
    return str(gate.get("verdict", gate.get("decision", ""))).lower()


def _refused(gate: dict) -> bool:
    """A verdict worth reading. 'allowed' is the overwhelming majority — one per
    always-rule per call — and printing all of them buries the one that stopped
    something, which is the only reason anyone opened this."""
    return _verdict_of(gate) not in PASSED_VERDICTS


# ---------------------------------------------------------------------------
# The dossier — one assembly, two renderers (#243)
# ---------------------------------------------------------------------------
#
# `render` above prints for a terminal; the web panel draws DOM. If each walked
# the records itself there would be two implementations of "what does this log
# say", and they would disagree about ABSENCE first — which is this reader's
# entire subject. So the walk happens once, here, and produces plain
# JSON-serialisable data that both renderers consume.
#
# States are machine values, distinct from the display strings above. A field is
# never merely absent.
RECORDED = "recorded"
MISSING = "not_recorded"
EMPTY = "empty"
PURGED = "purged"
UNREADABLE = "unreadable"
# A log written before #240 has no reasoning record, but its rendered
# `thinking` step kept a fragment. Showing that fragment as "the reasoning"
# is how someone concludes the model barely thought about it, so it is its
# own state and says which one it is.
FRAGMENTS = "fragments"

# What the notes pass looks for. Named here rather than inline so the empty
# state can say WHICH checks found nothing — "nothing unusual in this turn" is
# a claim no checker is entitled to make, since it only knows the classes
# somebody thought to code (#243).
CHECKS: tuple[tuple[str, str], ...] = (
    ("tool_failed", "a tool call failed"),
    ("call_incomplete", "a call was recorded but never completed"),
    ("gate_refused", "a gate refused a call"),
    ("verify_refused", "an answer was held or noted by a verify check"),
    ("rule_abstained", "a rule nearly matched and abstained"),
    ("args_truncated", "arguments were cut by a cap"),
    ("args_malformed", "the model's arguments did not parse"),
    ("reasoning_truncated", "reasoning was cut by a cap"),
    ("result_stubbed", "a result was stubbed after the model had read it"),
    ("steering", "text was typed while the task ran"),
    ("reminder_demoted", "the per-task reminder reached the model as a user message"),
    ("brief_changed", "what the model was handed changed mid-turn"),
    ("stop_unusual", "the model stopped for an unusual reason"),
    ("context_full", "the prompt nearly filled the context window"),
    ("task_unfinished", "the task did not end normally"),
)

# How close to `num_ctx` counts as "nearly full". Not a truncation — the point
# is that a turn at 92% of its window behaves differently from one at 30%, and
# nothing else in the log flags it (docs/diagnostics.md, "consumed vs sent").
CONTEXT_FULL_FRACTION = 0.85

# How close to its floor an abstention has to be before it is worth a look. A
# rule corpus is evaluated in FULL against every turn, so "this rule did not
# apply" is the ordinary case for almost all of them — listing them all put six
# rows of noise above the two real findings on the first live turn this was run
# against. A near miss is a different thing: it is the shape of "the rule you
# wrote did not fire and you expected it to".
ABSTAIN_NEAR_FLOOR = 0.12

# Stop reasons that mean "the model finished its turn". Anything else is worth
# a look, and is listed rather than judged.
ORDINARY_STOPS = frozenset({"", "stop", "end_turn", "stop_sequence", "tool_use", "tool_calls"})


def _blob(digest: str, root: os.PathLike | str | None) -> tuple[str, str | None]:
    """(state, text) for one evidence reference. `purged` and `not recorded`
    are different answers and only `recorded` entitles anyone to quote it."""
    if not digest:
        return MISSING, None
    text = evidence.get(digest, root)
    if text is None:
        return PURGED, None
    return RECORDED, text


def _brief_in_force(turn: Turn, log: Log) -> tuple[dict | None, int | None]:
    """The most recent brief at or before this turn, and the turn it was written
    at.

    The brief is interned — written only when what the model was handed CHANGES
    — so most turns have none of their own, and a panel that rendered only its
    own turn's brief would show an empty "what it was given" on nearly every
    turn, which is the one screen the whole feature exists for.

    Carrying it forward is not the same as it being written here, and the two
    must stay distinguishable: a reader who cannot tell them apart can conclude
    "the tools changed at turn 7" off a record that merely says they had not
    changed since turn 3.
    """
    for candidate in reversed(log.turns[: turn.ordinal]):
        found = candidate.of_kind("brief")
        if found:
            return found[-1], candidate.ordinal
    return None, None


def _given(turn: Turn, log: Log, root: os.PathLike | str | None) -> dict:
    """What the model was handed: the system text, the tool menu, the knowledge
    offered and preloaded, and the rules in force."""
    own = turn.of_kind("brief")
    carried_from = None
    records = list(own)
    if not records:
        carried, carried_from = _brief_in_force(turn, log)
        records = [carried] if carried else []
    briefs = []
    for record in records:
        parts = []
        for part in record.get("system") or []:
            state, text = _blob(str(part.get("digest") or ""), root)
            parts.append(
                {
                    "at": part.get("at"),
                    "chars": part.get("chars"),
                    "digest": part.get("digest"),
                    "state": state,
                    "text": text,
                }
            )
        system_state = RECORDED if parts else (
            MISSING if record.get("system") is None else EMPTY
        )
        menu = record.get("tools") or {}
        menu_state, parsed = _menu(record, root)
        briefs.append(
            {
                "model_call": record.get("model_call"),
                # Written FOR this turn, or the one still in force from an
                # earlier one. The panel says which; they are different facts.
                "written_here": carried_from is None,
                "in_force_since": carried_from,
                "options": record.get("options") or {},
                "system": {"state": system_state, "parts": parts},
                "tools": {
                    "state": {
                        "resolved": RECORDED,
                        NOT_RECORDED: MISSING,
                    }.get(menu_state, PURGED if parsed is None else RECORDED),
                    "digest": menu.get("digest"),
                    "count": menu.get("count"),
                    "names": list(menu.get("names") or []),
                    "entries": parsed,
                },
            }
        )
    given: dict = {
        "state": RECORDED if briefs else MISSING,
        "briefs": briefs,
        "carried": carried_from is not None,
    }
    contexts = turn.of_kind("context")
    given["context"] = {
        "state": RECORDED if contexts else (MISSING if not log.wrote("context") else EMPTY),
        "records": [
            {
                "index": record.get("index") or {},
                "preload": record.get("preload") or {},
            }
            for record in contexts
        ],
    }
    given["knowledge"] = [
        {"mode": record.get("mode"), "items": record.get("items") or []}
        for record in turn.of_kind("knowledge")
    ]
    given["rules"] = _rules_data(turn, log)
    # Trims that fired at task SEED, not mid-task. They shaped what the model
    # started from, so they belong here; `mid_task_budget` is an event in the
    # flow and is rendered between rounds instead.
    given["trims"] = [
        dict(r) for r in turn.of_kind("trim") if r.get("policy") != "mid_task_budget"
    ]
    return given


def _rules_data(turn: Turn, log: Log) -> dict:
    """The rule corpus as evaluated, grouped by verdict — the three groups route
    to three different repairs, which is why they are not one list."""
    evals = turn.of_kind("rule_eval")
    if not evals:
        return {"state": MISSING if not log.wrote("rule_eval") else EMPTY, "groups": {}}
    bound = {b.get("rule"): b for b in turn.of_kind("binding")}
    groups: dict[str, list[dict]] = {}
    corpus: dict = {}
    dropped = 0
    for record in evals:
        corpus = record.get("corpus") or corpus
        dropped += int(record.get("truncated") or 0)
        for row in record.get("evaluated") or []:
            verdict = str(row.get("verdict", "?"))
            entry = dict(row)
            if verdict == "bind":
                entry["binding"] = bound.get(row.get("rule")) or {}
            groups.setdefault(verdict, []).append(entry)
    return {
        "state": RECORDED,
        "corpus": corpus,
        "groups": groups,
        "skipped": list(corpus.get("skipped") or []),
        "dropped": dropped,
    }


def _thought(turn: Turn, log: Log) -> dict:
    """What the model was thinking, per model call, in the order it thought it."""
    records = turn.of_kind("reasoning")
    fragments = [str(s.get("gist")) for s in turn.of_kind("thinking") if s.get("gist")]
    if records:
        state = RECORDED
    elif fragments:
        state = FRAGMENTS
    else:
        state = MISSING if not log.wrote("reasoning") else EMPTY
    return {
        "state": state,
        "fragments": fragments,
        "calls": [
            {
                "model_call": record.get("model_call"),
                "text": record.get("text") or "",
                "truncated": record.get("truncated") or 0,
                "cap_source": record.get("cap_source"),
                "said": record.get("said") or "",
                "said_truncated": record.get("said_truncated") or 0,
                "stop": record.get("stop") or "",
                "tokens": record.get("tokens") or [],
                "blocks": record.get("blocks") or [],
                # A LIST of tool names, not a count: "arguments did not parse
                # for read_url" routes to a different repair from "one call
                # failed to parse".
                "malformed": list(record.get("malformed") or []),
                "synthesized": bool(record.get("synthesized")),
            }
            for record in records
        ],
    }


def _did(turn: Turn) -> dict:
    """Every tool call: the arguments as emitted, the verdicts that governed it,
    and what came back. Joined by call id, never by position."""
    per_call: dict[int, list[dict]] = {}
    turn_level: list[dict] = []
    for gate in turn.of_kind("gate"):
        if gate.get("at") == "verify" or not gate.get("call"):
            turn_level.append(gate)
        else:
            per_call.setdefault(int(gate["call"]), []).append(gate)
    emitted = {int(c.get("call") or 0): c for c in turn.of_kind("call")}

    calls = []
    for step in turn.of_kind("tool"):
        call = int(step.get("call") or 0)
        record = emitted.pop(call, None)
        gates = per_call.pop(call, [])
        calls.append(
            {
                "call": call,
                "name": step.get("name"),
                "summary": step.get("summary") or "",
                "args": (record or {}).get("args") or {},
                "args_state": RECORDED if record else MISSING,
                "args_truncated": (record or {}).get("truncated") or 0,
                "cap_source": (record or {}).get("cap_source"),
                "ok": bool(step.get("ok")),
                "status": step.get("status"),
                "secs": step.get("secs", 0),
                "command": step.get("command") or "",
                "error": step.get("error") or "",
                "output": step.get("output") or "",
                "decision": step.get("decision"),
                "verdict_by": step.get("verdict_by"),
                "gates": gates,
                "refused": [g for g in gates if _refused(g)],
                "completed": True,
            }
        )
    # Arguments recorded for a call that produced no tool step. The `call`
    # record is written BEFORE the call runs precisely so a crash still leaves
    # them; dropping them here would waste that.
    for call, record in sorted(emitted.items()):
        calls.append(
            {
                "call": call,
                "name": record.get("name"),
                "summary": "",
                "args": record.get("args") or {},
                "args_state": RECORDED,
                "args_truncated": record.get("truncated") or 0,
                "cap_source": record.get("cap_source"),
                "ok": False,
                "status": None,
                "secs": 0,
                "command": "",
                "error": "",
                "output": "",
                "decision": None,
                "verdict_by": None,
                "gates": per_call.pop(call, []),
                "refused": [],
                "completed": False,
            }
        )
    orphans = [
        {"call": call, "gates": gates} for call, gates in sorted(per_call.items())
    ]
    return {"calls": calls, "orphan_gates": orphans, "verify": turn_level}


def _produced(turn: Turn, did: dict) -> dict:
    """The answer, and the checks that ran over it before it shipped."""
    answers = [
        str(m.get("content") or "")
        for m in turn.messages
        if m.get("role") == "assistant"
        and not m.get("interim")
        and (m.get("content") or "").strip()
    ]
    verify = did["verify"]
    # Three buckets, not two: an `advised` answer SHIPPED, with a note. Folding
    # it in with refusals is the conflation the trace contract names.
    stopped = [g for g in verify if _refused(g) and _verdict_of(g) not in ADVISED_VERDICTS]
    advised = [g for g in verify if _verdict_of(g) in ADVISED_VERDICTS]
    return {
        "answer": answers[-1] if answers else "",
        "answer_state": RECORDED if answers else EMPTY,
        "status": turn.status,
        "error": turn.error,
        "verify": {
            "stopped": stopped,
            "advised": advised,
            "passed": len(verify) - len(stopped) - len(advised),
        },
    }


# Which model call issued a tool call, and whether that is RECORDED or merely
# inferred from the order the log was written in. Chosen per TURN, not per file:
# one session can hold both local turns (which record it) and claude-max turns
# (whose SDK loop records no model calls at all), so a file-level answer would
# label one of them wrongly.
GROUPING_RECORDED = "recorded"
GROUPING_INFERRED = "inferred"
GROUPING_NONE = "none"


def _walk_rounds(turn: Turn) -> dict[int, int]:
    """Which model call each step of the turn sat under, by file order.

    File order IS the chronology within a turn — every one of these records is
    emitted from the main thread in sequence, including the parallel batch,
    whose `call` records are written at collection. So this is not a guess about
    ordering; it is reading the order that was written. It IS an inference about
    ATTACHMENT, which is why anything relying on it is labelled `inferred`.
    """
    at: dict[int, int] = {}
    seen = 0
    for index, step in enumerate(turn.steps):
        kind = step.get("kind")
        if kind == "reasoning":
            seen = int(step.get("model_call") or seen + 1)
        elif kind == "thinking" and not seen:
            # Pre-#240 logs have no reasoning record; the rendered thinking step
            # is the only round boundary they kept.
            seen = 1
        at[index] = seen
    return at


def _rounds(turn: Turn, doc: dict) -> dict:
    """The turn as it actually happened: think, call, get results, think again.

    The panel and the reader were organised by RECORD KIND, which is how a file
    is organised and not how a turn is. Reading them, the owner could not tell
    which thinking followed which result — "I don't know which information was
    retrieved after which tool" — and that causal chain is the whole reason to
    open a dossier at all.

    Rounds reference `thought` and `did` by id rather than copying them: tool
    output is the bulk of the payload and this document is fetched to a phone.

    Events that sit BETWEEN rounds ride here too, because they are exactly the
    "what changed between two thoughts" facts that were invisible: text typed
    while the task ran, a mid-task trim that stubbed a result the model had
    already read, and a change to what the model was being handed.
    """
    thoughts = doc["thought"]["calls"]
    calls = doc["did"]["calls"]
    at = _walk_rounds(turn)

    placed: dict[int, int] = {}   # call id -> model call
    inferred_any = False
    for index, step in enumerate(turn.steps):
        if step.get("kind") != "call" or not step.get("call"):
            continue
        recorded = step.get("model_call")
        if recorded:
            placed[int(step["call"])] = int(recorded)
        elif at.get(index):
            placed[int(step["call"])] = at[index]
            inferred_any = True

    if not thoughts and not calls:
        grouping = GROUPING_NONE
    elif calls and not any(s.get("model_call") for s in turn.of_kind("call")):
        # Nothing stamped: either a log older than the stamp, or a backend whose
        # loop records no model calls at all (claude-max).
        grouping = GROUPING_INFERRED if inferred_any else GROUPING_NONE
    elif inferred_any:
        grouping = GROUPING_INFERRED
    else:
        grouping = GROUPING_RECORDED

    # Between-round events, by the LAST COMPLETED model call at the time they
    # were written. Never "the next one": both are emitted before a call that a
    # cancel can stop from ever happening, and an event naming a round that
    # never occurred is a lie.
    after: dict[int, list[dict]] = {}
    for index, step in enumerate(turn.steps):
        kind = step.get("kind")
        if kind == "trim" and step.get("policy") == "mid_task_budget":
            after.setdefault(at.get(index, 0), []).append({"kind": "trim", "record": dict(step)})
        elif kind == "injected":
            after.setdefault(at.get(index, 0), []).append(
                {"kind": "steering", "text": str(step.get("text") or "")}
            )

    numbers = sorted({int(t["model_call"]) for t in thoughts if t.get("model_call")} | set(
        placed.values()
    ))
    rounds = []
    for number in numbers:
        events = list(after.get(number - 1, []))
        for brief in doc["given"]["briefs"]:
            if brief["written_here"] and brief.get("model_call") == number and number > 1:
                events.append({"kind": "brief_changed", "model_call": number})
        rounds.append(
            {
                "model_call": number,
                # References, never copies.
                "thought": next(
                    (t["model_call"] for t in thoughts if t.get("model_call") == number), None
                ),
                "calls": [c["call"] for c in calls if placed.get(c["call"]) == number],
                "before": events,
            }
        )
    # Calls belonging to no round — claude-max, or a log with neither the stamp
    # nor any reasoning to infer from. Listed flat rather than folded into round
    # one, which would be a fabricated join.
    unplaced = [c["call"] for c in calls if c["call"] not in placed]
    # Events whose round never got rendered — a turn with no reasoning records
    # at all still has steering and trims, and dropping them would hide the one
    # place some of them exist (steering is not restored on resume).
    consumed = {number - 1 for number in numbers}
    loose = [event for key, events in after.items() if key not in consumed for event in events]
    return {
        "grouping": grouping,
        "rounds": rounds,
        "unplaced": unplaced,
        "loose": loose,
    }


def _event_note(
    rows: list[dict], event: dict, model_call: int | None, section: str = "flow"
) -> None:
    """One event as a row worth looking at.

    `model_call` is the round it happened before, so the row deep-links to that
    round rather than to the top of the stream. None when no round could be
    named, and then the text says so instead of implying a position — a seed
    trim happened before the model was called at all, so it says that."""
    where: dict = {"section": section}
    if model_call is not None:
        where["model_call"] = model_call
    if model_call:
        when = f"before model call {model_call}"
    elif section == "given":
        when = "before the model was called at all"
    else:
        when = "at some point in this turn"
    if event["kind"] == "trim":
        record = event["record"]
        if stubbed := record.get("stubbed"):
            listed = ", ".join(f"{x.get('tool')} (#{x.get('at')})" for x in stubbed)
            rows.append({"check": "result_stubbed", "where": where,
                         "text": f"{record.get('affected')} earlier result(s) were replaced "
                                 f"with a stub {when}: {listed}"})
        elif record.get("affected"):
            rows.append({"check": "result_stubbed", "where": where,
                         "text": f"{record.get('affected')} earlier result(s) were stubbed "
                                 f"{when}; which ones was not recorded"})
    elif event["kind"] == "steering":
        rows.append({"check": "steering", "where": where,
                     "text": "you typed while the task was running and it was folded into "
                             f"the model's messages, {when}: {event['text'][:120]}"})


def _note(rows: list[dict], check: str, text: str, **where) -> None:
    rows.append({"check": check, "text": text, "where": where})


def notes(doc: dict) -> dict:
    """Things in this turn worth a look — facts, never causes (#243).

    A pure function of the dossier, so the terminal and the panel surface the
    same list and neither re-reads the log. Every row states what happened and
    says where in the dossier it came from; none of them says WHY the turn went
    the way it did, because a confident wrong cause wearing evidence styling is
    the failure this whole feature exists to end.

    The empty case reports which checks ran. "Nothing unusual" is a claim a
    checker cannot make: it only knows the classes someone thought to code, and
    stating it above the evidence would contradict the evidence exactly when the
    cause is a class nobody anticipated.
    """
    rows: list[dict] = []
    for call in doc["did"]["calls"]:
        if not call["completed"]:
            _note(rows, "call_incomplete",
                  f"{call['name']} was proposed but no result was recorded",
                  section="flow", call=call["call"])
        elif not call["ok"]:
            _note(rows, "tool_failed",
                  f"{call['name']} failed ({call['status']})",
                  section="flow", call=call["call"])
        for gate in call["refused"]:
            _note(rows, "gate_refused",
                  f"{call['name']} was refused by {gate.get('rule') or gate.get('at')}",
                  section="flow", call=call["call"])
        if call["args_truncated"]:
            _note(rows, "args_truncated",
                  f"{call['args_truncated']} characters of {call['name']}'s arguments were "
                  f"cut from the record by {call['cap_source'] or 'a cap'}",
                  section="flow", call=call["call"])

    # One row per OUTCOME, not per rule. A corpus of two dozen rules produces a
    # dozen verify verdicts on an ordinary turn, and a list that long is the
    # wall of evidence this pass exists to replace.
    verify = doc["produced"]["verify"]
    for bucket, phrasing in (
        ("stopped", "the answer was held by {n} verify check(s): {rules}"),
        ("advised", "the answer shipped with a note from {n} verify check(s): {rules}"),
    ):
        gates = verify[bucket]
        if gates:
            named = [str(g.get("rule") or g.get("at") or "?") for g in gates]
            _note(rows, "verify_refused",
                  phrasing.format(n=len(gates), rules=", ".join(sorted(set(named)))),
                  section="produced")

    near = []
    for row in doc["given"]["rules"].get("groups", {}).get("abstain", []):
        ev = row.get("evidence") or {}
        try:
            gap = float(ev["floor"]) - float(ev["sim"])
        except (KeyError, TypeError, ValueError):
            continue  # no distance recorded — nothing to call near
        if 0 <= gap <= ABSTAIN_NEAR_FLOOR:
            near.append(f"{row.get('rule')} ({ev['sim']} against a floor of {ev['floor']})")
    if near:
        _note(rows, "rule_abstained",
              f"{len(near)} rule(s) came close to applying and did not: " + ", ".join(near),
              section="given")

    for call in doc["thought"]["calls"]:
        if call["truncated"]:
            _note(rows, "reasoning_truncated",
                  f"{call['truncated']} characters of reasoning were cut from the record "
                  f"by {call['cap_source'] or 'a cap'}",
                  section="flow", model_call=call["model_call"])
        if call["malformed"]:
            _note(rows, "args_malformed",
                  "the model's arguments did not parse for: "
                  + ", ".join(str(n) for n in call["malformed"]),
                  section="flow", model_call=call["model_call"])
        if call["stop"] and call["stop"] not in ORDINARY_STOPS:
            _note(rows, "stop_unusual",
                  f"the model stopped with reason {call['stop']!r}",
                  section="flow", model_call=call["model_call"])

    # Read off the FLOW, not the raw records, so each row can name the round it
    # happened before. A row citing only "the flow" lands the reader on a
    # section header, which is indistinguishable from the tap doing nothing —
    # and the steering row was pointing at the wrong section entirely.
    for rnd in doc["flow"]["rounds"]:
        for event in rnd["before"]:
            _event_note(rows, event, model_call=rnd["model_call"])
    for event in doc["flow"]["loose"]:
        _event_note(rows, event, model_call=None)
    # …and the trims that fired at task SEED, which are not events in the flow
    # but are the same fact about the same turn: the model did not have what the
    # transcript still shows it having. Reading events off the flow alone lost
    # these, which on a real turn was the sharpest row on the list.
    for record in doc["given"].get("trims") or []:
        _event_note(rows, {"kind": "trim", "record": record}, model_call=None, section="given")

    briefs = doc["given"]["briefs"]
    for brief in briefs:
        if (brief["options"] or {}).get("system_role") == "first_only":
            _note(rows, "reminder_demoted",
                  f"on {brief['options'].get('provider')} the per-task reminder — which "
                  f"carries the rules — reached the model as a user message, not a system one",
                  section="given")
            break
    if len(briefs) > 1:
        _note(rows, "brief_changed",
              f"what the model was handed changed {len(briefs) - 1} time(s) during this turn",
              section="given")

    # The fullest call, not every call: the window is a property of the turn and
    # repeating it per call buries everything else.
    window = 0
    for brief in briefs:
        window = int((brief["options"] or {}).get("num_ctx") or 0) or window
    fullest = max(
        (c for c in doc["thought"]["calls"] if c["tokens"]),
        key=lambda c: int(c["tokens"][0] or 0),
        default=None,
    )
    if window and fullest and int(fullest["tokens"][0] or 0) > window * CONTEXT_FULL_FRACTION:
        _note(rows, "context_full",
              f"the prompt reached {fullest['tokens'][0]} tokens against a "
              f"{window}-token window",
              section="flow", model_call=fullest["model_call"])

    status = doc["produced"]["status"]
    if status is None:
        _note(rows, "task_unfinished",
              "no end was recorded for this task — it was interrupted, or it is still running",
              section="produced")
    elif status != "ok":
        _note(rows, "task_unfinished",
              f"the task ended {status}: {doc['produced']['error'][:200]}",
              section="produced")

    return {"rows": rows, "checks": [{"id": i, "label": lb} for i, lb in CHECKS]}


def find(log: Log, ref: str | int | None) -> list[Turn]:
    """The turns a reference names — by turn id first, ordinal second.

    The id is preferred because it is the only identity that cannot be counted
    wrong: the web transcript's first paint is bounded, so a browser's "turn 4"
    is not the log's turn 4 on a long chat, and a diagnosis about the wrong turn
    is worse than none. The ordinal and the agent's own counter remain, for logs
    written before ids existed and for typing a number at a terminal.
    """
    if ref is None or ref == "":
        return list(log.turns)
    text = str(ref)
    by_id = [t for t in log.turns if t.turn_id and t.turn_id == text]
    if by_id:
        return by_id
    try:
        number = int(text)
    except ValueError:
        return []
    return [t for t in log.turns if t.ordinal == number or t.counter == number]


def dossier(turn: Turn, log: Log, root: os.PathLike | str | None) -> dict:
    """One turn, assembled from its records — the data both renderers read."""
    did = _did(turn)
    doc = {
        "ordinal": turn.ordinal,
        "counter": turn.counter,
        "ts": turn.ts,
        "prompt": turn.prompt,
        "given": _given(turn, log, root),
        "thought": _thought(turn, log),
        "did": did,
        "produced": _produced(turn, did),
        "steering": [
            {"text": str(r.get("text") or "")} for r in turn.of_kind("injected")
        ],
        "trim": [dict(r) for r in turn.of_kind("trim")],
    }
    # After `given`/`thought`/`did` exist, because it references into them.
    doc["flow"] = _rounds(turn, doc)
    doc["notes"] = notes(doc)
    return doc


def _block(out: list[str], label: str, lines: list[str]) -> None:
    for offset, line in enumerate(lines):
        out.append(f"  {label if offset == 0 else '':<10} {line}")


def _state_note(state: str, kind_note: str) -> str:
    """The three states as a terminal reader sees them. Never a blank: a blank
    is what lets someone read "it was told nothing" off a log that predates the
    record, or off bytes that were deliberately purged."""
    return {
        MISSING: f"{DIM}{NOT_RECORDED}{kind_note}{RESET}",
        EMPTY: f"{DIM}recorded, and there was none{RESET}",
        PURGED: f"{DIM}bytes purged or stored elsewhere{RESET}",
        UNREADABLE: f"{DIM}stored bytes are unreadable{RESET}",
    }.get(state, "")


def _given_lines(given: dict, show_tools: bool, show_context: bool, out: list[str]) -> None:
    if not given["briefs"]:
        out.append(f"  {'brief':<10} {_state_note(given['state'], ' (log predates #239)')}")
    for brief in given["briefs"]:
        options = brief["options"]
        where = (
            f"at model call {brief.get('model_call', '?')}"
            if brief["written_here"]
            else f"{DIM}unchanged since turn {brief['in_force_since']}{RESET}"
        )
        out.append(
            f"  {'brief':<10} model {options.get('model')} · num_ctx "
            f"{options.get('num_ctx')} · think {'on' if options.get('think') else 'off'}"
            f" · {where}"
        )
        if options.get("system_role") == "first_only":
            # Not a footnote: "why did it ignore the reminder" has a mechanical
            # answer on these backends, and it is invisible from aish's own view.
            out.append(
                f"  {'':<10} {BOLD}the per-task reminder reached this model as a USER "
                f"message{RESET}{DIM} ({options.get('provider')} carries only the first "
                f"system message as system){RESET}"
            )
        system = brief["system"]
        if system["state"] != RECORDED:
            out.append(
                f"  {'':<10} system text: {_state_note(system['state'], ' (log predates #239)')}"
            )
        else:
            total = sum(int(p.get("chars") or 0) for p in system["parts"])
            out.append(f"  {'':<10} system: {len(system['parts'])} message(s), {total} chars")
            for part in system["parts"]:
                status = "resolved" if part["state"] == RECORDED else part["state"]
                out.append(f"  {'':<10}   #{part['at']} · {part['chars']} chars · {status}")
                if show_context and part["text"] is not None:
                    for line in part["text"].splitlines():
                        out.append(f"  {'':<10}   {DIM}│{RESET} {line}")
        menu = brief["tools"]
        status = "resolved" if menu["state"] == RECORDED else menu["state"]
        out.append(
            f"  {'':<10} tools: {menu.get('count', '?')} on the menu "
            f"({str(menu.get('digest') or '')[:12]}… {status})"
        )
        if menu["names"]:
            out.append(f"  {'':<10} {DIM}{', '.join(str(n) for n in menu['names'])}{RESET}")
        if show_tools and menu["entries"]:
            for entry in menu["entries"]:
                function = entry.get("function") or {}
                out.append(f"  {'':<10} {BOLD}{function.get('name')}{RESET}")
                out.append(f"  {'':<10}   {str(function.get('description') or '').strip()}")
                params = function.get("parameters")
                if params:
                    out.append(f"  {'':<10}   {json.dumps(params, ensure_ascii=False)}")

    context = given["context"]
    if context["state"] != RECORDED:
        out.append(f"  {'context':<10} {_state_note(context['state'], '')}")
    for record in context["records"]:
        items = (record["index"] or {}).get("items") or []
        preload = record["preload"] or {}
        out.append(
            f"  {'context':<10} index: {len(items)} item(s) offered; "
            f"preloaded {preload.get('count', 0)} ({preload.get('mode')})"
        )
        if preload.get("names"):
            out.append(f"  {'':<10} {', '.join(str(n) for n in preload['names'])}")

    for record in given["knowledge"]:
        chosen = ", ".join(
            f"{it.get('label')} (sim {it.get('sim')}, rail {it.get('rail')})"
            for it in record["items"]
        )
        out.append(f"  {'knowledge':<10} {record['mode']}: {chosen or '(none)'}")

    for record in given.get("trims") or []:
        out.append(
            f"  {'trim':<10} {record.get('policy')}: {record.get('affected')} message(s), "
            f"{record.get('bytes_before')} → {record.get('bytes_after')} bytes "
            f"(keep {record.get('keep_chars')}, cap from {record.get('cap_source')})"
        )
        if stubbed := record.get("stubbed"):
            listed = ", ".join(f"#{x.get('at')} {x.get('tool')}" for x in stubbed)
            out.append(f"  {'':<10} {BOLD}stubbed for the model{RESET}: {listed}")
            if record.get("stubbed_truncated"):
                out.append(f"  {'':<10} … and {record['stubbed_truncated']} more")
        elif record.get("affected"):
            out.append(
                f"  {'':<10} {DIM}which messages: {NOT_RECORDED} (log predates #241){RESET}"
            )

    _rules_lines(given["rules"], out)


def _rules_lines(rules: dict, out: list[str]) -> None:
    """Grouped by verdict, because the groups route to different repairs (#197):
    nothing covered this, something covered it and retired, something covered it
    and abstained. File order was how a 24-rule corpus buried the one abstention
    that mattered."""
    if rules["state"] != RECORDED:
        note = "" if rules["state"] == MISSING else ""
        out.append(f"  {'rules':<10} {_state_note(rules['state'], note)}")
        return
    corpus = rules["corpus"]
    groups = rules["groups"]
    lines = [
        f"corpus: {corpus.get('total', '?')} total, {corpus.get('active', '?')} active, "
        f"{len(rules['skipped'])} skipped"
    ]
    for row in groups.get("bind", []):
        binding = row.get("binding") or {}
        seeded = "seeded" if binding.get("seeded") else f"{BOLD}NOT SEEDED{RESET}"
        lines.append(
            f"  {BOLD}bound{RESET}       {row.get('rule')} "
            f"({row.get('trigger')}, tier {row.get('tier')}) — {seeded}"
        )
        if not binding.get("satisfiable", True):
            lines.append(
                f"              {BOLD}unsatisfiable{RESET}: {binding.get('unsatisfiable')}"
            )
        for ob in binding.get("obligations") or []:
            lines.append(f"              {json.dumps(ob, ensure_ascii=False)}")
    for verdict in ("unevaluable", "error"):
        for row in groups.get(verdict, []):
            fail = f", failed {row['fail']}" if row.get("fail") else ""
            lines.append(
                f"  {BOLD}{verdict}{RESET} {row.get('rule')} — "
                f"{_fmt_evidence(row.get('evidence') or {})}{fail}"
            )
    for row in groups.get("abstain", []):
        detail = _fmt_evidence(row.get("evidence") or {})
        lines.append(
            f"  abstained   {row.get('rule')} ({row.get('trigger')}, tier {row.get('tier')})"
            + (f" — {detail}" if detail else "")
        )
    for skipped in rules["skipped"]:
        lines.append(f"  skipped     {skipped.get('rule')} ({skipped.get('why')})")
    if rules["dropped"]:
        lines.append(
            f"  {BOLD}{rules['dropped']} abstention row(s) dropped by the cap"
            f" — list is partial{RESET}"
        )
    _block(out, "rules", lines)


def _did_lines(did: dict, out: list[str]) -> None:
    lines: list[str] = []
    for call in did["calls"]:
        if not call["completed"]:
            lines.append(
                f"  #{call['call']} {call['name']} "
                f"{json.dumps(call['args'], ensure_ascii=False)}"
            )
            lines.append(f"     {BOLD}never completed{RESET} {DIM}— no tool step recorded{RESET}")
            continue
        status = "ok" if call["ok"] else f"{BOLD}FAILED{RESET} ({call['status']})"
        head = f"  #{call['call']} {call['name']}"
        if call["summary"]:
            head += f" {DIM}{str(call['summary'])[:90]}{RESET}"
        lines.append(head)
        if call["args_state"] == RECORDED:
            lines.append(f"     args: {json.dumps(call['args'], ensure_ascii=False)}")
            if call["args_truncated"]:
                lines.append(
                    f"     {BOLD}… {call['args_truncated']} argument characters cut by "
                    f"{call['cap_source'] or 'a cap'}{RESET}"
                )
        detail = f"     → {status}, {call['secs']:.1f}s"
        if call["verdict_by"]:
            detail += f", verdict by {call['verdict_by']}"
        if call["decision"]:
            detail += f", decision {call['decision']}"
        lines.append(detail)
        if call["command"]:
            lines.append(f"     command: {call['command']}")
        if call["error"]:
            lines.append(f"     error: {str(call['error'])[:200]}")
        for gate in call["refused"]:
            lines.append(f"     {BOLD}gate{RESET} {_gate_line(gate)}")
        allowed = len(call["gates"]) - len(call["refused"])
        if allowed:
            lines.append(f"     {DIM}{allowed} gate(s) allowed{RESET}")
    for orphan in did["orphan_gates"]:
        for gate in orphan["gates"]:
            lines.append(
                f"  #{orphan['call']} gate {_gate_line(gate)} "
                f"{DIM}(no tool step recorded){RESET}"
            )
    _block(out, "calls", lines or [f"{DIM}no tool calls{RESET}"])


def _produced_lines(produced: dict, out: list[str]) -> None:
    verify = produced["verify"]
    if verify["stopped"] or verify["advised"] or verify["passed"]:
        vlines = [_gate_line(g) for g in verify["stopped"]]
        for gate in verify["advised"]:
            vlines.append(f"{_gate_line(gate)} {DIM}(answer delivered, with a note){RESET}")
        if verify["passed"]:
            vlines.append(f"{DIM}{verify['passed']} check(s) passed{RESET}")
        _block(out, "verify", vlines)


def _thought_lines(thought: dict, out: list[str]) -> None:
    """What the model produced on each call of this turn (#240).

    Placed right after the calls, deliberately: seeing what it THOUGHT beside
    what it then did is the whole point of the record."""
    if thought["state"] == FRAGMENTS:
        out.append(f"  {'reasoning':<10} {DIM}fragments only — this log predates #240{RESET}")
        for gist in thought["fragments"][:20]:
            out.append(f"  {'':<10} {DIM}· {gist[:160]}{RESET}")
        return
    if thought["state"] != RECORDED:
        out.append(f"  {'reasoning':<10} {_state_note(thought['state'], '')}")
        return
    lines: list[str] = []
    for call in thought["calls"]:
        head = f"call {call['model_call'] if call['model_call'] is not None else '?'}"
        if call["stop"]:
            head += f" · stopped: {call['stop']}"
        if call["tokens"]:
            head += f" · tokens {call['tokens']}"
        if call["blocks"]:
            head += f" · blocks {', '.join(str(b) for b in call['blocks'])}"
        lines.append(head)
        if call["synthesized"]:
            lines.append(f"  {BOLD}the text below is aish's sentence, not the model's{RESET}")
        if call["malformed"]:
            # Different repair from "called with no arguments", which is what
            # this looked like before the flag existed.
            lines.append(
                f"  {BOLD}arguments did not parse{RESET} for: "
                f"{', '.join(str(n) for n in call['malformed'])}"
            )
        for label, key, cut in (
            ("thought", "text", "truncated"),
            ("said", "said", "said_truncated"),
        ):
            body = str(call.get(key) or "")
            if not body:
                continue
            lines.append(f"  {label}:")
            for para in body.splitlines():
                lines.append(f"    {para}")
            if call.get(cut):
                lines.append(
                    f"    {BOLD}… {call[cut]} more characters cut by "
                    f"{call['cap_source'] or 'a cap'}{RESET}"
                )
    _block(out, "reasoning", lines)


GROUPING_WORDS = {
    GROUPING_INFERRED: "round order inferred from the order the log was written "
                       "— this log predates the by-id record",
    GROUPING_NONE: "this backend's loop records no model calls, so the turn "
                   "cannot be shown as rounds",
}


def _flow_lines(doc: dict, out: list[str]) -> None:
    """The turn in the order it happened, which is the order a reader needs.

    Organised by record kind, a dossier cannot answer "what did it think after
    it got that result" — the owner's actual question. Rounds put each thought
    beside the calls it issued and the results they returned."""
    flow = doc["flow"]
    thoughts = {t["model_call"]: t for t in doc["thought"]["calls"]}
    calls = {c["call"]: c for c in doc["did"]["calls"]}
    lines: list[str] = []
    # Whether the reasoning is here at all, said once at the top rather than
    # once per round. A log written before the full record kept only a rendered
    # fragment, and a 26-character snippet shown as "the reasoning" is how
    # someone concludes the model barely thought about it.
    thought_state = doc["thought"]["state"]
    if thought_state == FRAGMENTS:
        lines.append(f"{DIM}reasoning: fragments only — this log predates #240{RESET}")
        for gist in doc["thought"]["fragments"][:20]:
            lines.append(f"  {DIM}· {gist[:160]}{RESET}")
    elif thought_state != RECORDED:
        lines.append(f"  {'':<0}reasoning: {_state_note(thought_state, ' (log predates #240)')}")
    if word := GROUPING_WORDS.get(flow["grouping"]):
        lines.append(f"{DIM}{word}{RESET}")
    for rnd in flow["rounds"]:
        for event in rnd["before"]:
            lines.extend(_event_lines(event, placed=True))
        thought = thoughts.get(rnd["thought"])
        head = f"{BOLD}round {rnd['model_call']}{RESET}"
        if thought:
            if thought["stop"]:
                head += f" · stopped: {thought['stop']}"
            if thought["tokens"]:
                head += f" · tokens {thought['tokens']}"
        lines.append(head)
        if thought is None:
            lines.append(f"  {DIM}no response was recorded for this call{RESET}")
        else:
            if thought["synthesized"]:
                lines.append(f"  {BOLD}the text below is aish's sentence, not the model's{RESET}")
            if thought["text"]:
                lines.append("  thought:")
                lines.extend(f"    {line}" for line in thought["text"].splitlines())
            else:
                lines.append(f"  {DIM}this call recorded no thinking{RESET}")
            if thought["truncated"]:
                lines.append(
                    f"    {BOLD}… {thought['truncated']} more characters cut by "
                    f"{thought['cap_source'] or 'a cap'}{RESET}"
                )
            if thought["said"]:
                lines.append("  said:")
                lines.extend(f"    {line}" for line in thought["said"].splitlines())
            if thought["malformed"]:
                lines.append(
                    f"  {BOLD}arguments did not parse{RESET} for: "
                    f"{', '.join(str(n) for n in thought['malformed'])}"
                )
        if not rnd["calls"] and thought is not None:
            lines.append(f"  {DIM}no tool calls ran under this one{RESET}")
        for number in rnd["calls"]:
            lines.extend(_call_lines(calls[number]))
    if flow["unplaced"]:
        lines.append(f"{BOLD}calls that name no model call{RESET}")
        for number in flow["unplaced"]:
            lines.extend(_call_lines(calls[number]))
    for event in flow["loose"]:
        lines.extend(_event_lines(event, placed=False))
    _block(out, "what happened", lines or [f"{DIM}nothing was recorded for this turn{RESET}"])


def _event_lines(event: dict, placed: bool) -> list[str]:
    """Something that happened BETWEEN two thoughts.

    These are the "what changed while it was running" facts that a kind-sliced
    dossier hid: a result the model had already read replaced by a stub, text
    typed mid-task, a change to what it was being handed. `placed` is False when
    no round could be named — the event still shows, without a claim about when.
    """
    where = "before this call" if placed else "at some point in this turn"
    if event["kind"] == "trim":
        record = event["record"]
        stubbed = ", ".join(
            f"#{x.get('at')} {x.get('tool')}" for x in record.get("stubbed") or []
        )
        detail = f": {stubbed}" if stubbed else f" ({NOT_RECORDED} which)"
        return [
            f"  {BOLD}⚠ {where}{RESET} {record.get('affected')} earlier result(s) "
            f"were stubbed for the model{detail}"
        ]
    if event["kind"] == "steering":
        return [f"  {BOLD}⚠ you typed mid-task{RESET}: {event['text'][:200]}"]
    if event["kind"] == "brief_changed":
        return [f"  {BOLD}⚠ what the model was handed changed here{RESET}"]
    return []


def _call_lines(call: dict) -> list[str]:
    """One tool call: what was asked, what governed it, what came back."""
    lines: list[str] = []
    if not call["completed"]:
        lines.append(
            f"  → #{call['call']} {call['name']} "
            f"{json.dumps(call['args'], ensure_ascii=False)}"
        )
        lines.append(f"     {BOLD}never completed{RESET} {DIM}— no tool step recorded{RESET}")
        return lines
    status = "ok" if call["ok"] else f"{BOLD}FAILED{RESET} ({call['status']})"
    head = f"  → #{call['call']} {call['name']}"
    if call["summary"]:
        head += f" {DIM}{str(call['summary'])[:90]}{RESET}"
    lines.append(head)
    if call["args_state"] == RECORDED:
        lines.append(f"     args: {json.dumps(call['args'], ensure_ascii=False)}")
        if call["args_truncated"]:
            lines.append(
                f"     {BOLD}… {call['args_truncated']} argument characters cut by "
                f"{call['cap_source'] or 'a cap'}{RESET}"
            )
    detail = f"     {status}, {call['secs']:.1f}s"
    if call["verdict_by"]:
        detail += f", verdict by {call['verdict_by']}"
    if call["decision"]:
        detail += f", decision {call['decision']}"
    lines.append(detail)
    if call["command"]:
        lines.append(f"     command: {call['command']}")
    if call["error"]:
        lines.append(f"     error: {str(call['error'])[:200]}")
    for gate in call["refused"]:
        lines.append(f"     {BOLD}gate{RESET} {_gate_line(gate)}")
    allowed = len(call["gates"]) - len(call["refused"])
    if allowed:
        lines.append(f"     {DIM}{allowed} gate(s) allowed{RESET}")
    return lines


def render(
    turn: Turn,
    log: Log,
    root: os.PathLike | str | None,
    show_tools: bool = False,
    show_context: bool = False,
) -> str:
    """The terminal rendering of one turn — a dumb renderer over `dossier`.

    Three parts, in the shape a turn actually has: what it was given before it
    started, what happened while it ran, and what it produced. Organised by
    RECORD KIND — which is how a file is organised, and how this used to read —
    a dossier cannot answer "what did it think after it got that result", which
    is the question people open one to ask.
    """
    doc = dossier(turn, log, root)
    out: list[str] = []
    label = f"turn {doc['ordinal']}"
    if doc["counter"] is not None and doc["counter"] != doc["ordinal"]:
        label += f" (agent turn {doc['counter']})"
    out.append(f"{BOLD}── {label} ── {doc['ts']} {'─' * max(0, 40 - len(label))}{RESET}")
    out.append(f"  {'prompt':<10} {doc['prompt'].strip()[:400] or DIM + '(empty)' + RESET}")

    # Facts worth reading first, computed over the dossier and never over the
    # log — so the terminal and the panel say the same things, in the same order.
    rows = doc["notes"]["rows"]
    if rows:
        _block(out, "worth a look", [f"{BOLD}·{RESET} {row['text']}" for row in rows])
    else:
        _block(
            out,
            "worth a look",
            [f"{DIM}nothing flagged by the {len(doc['notes']['checks'])} checks this "
             f"reader runs{RESET}"],
        )

    _given_lines(doc["given"], show_tools, show_context, out)
    _flow_lines(doc, out)
    _produced_lines(doc["produced"], out)

    produced = doc["produced"]
    answer = produced["answer"][:400] if produced["answer"] else DIM + "(none)" + RESET
    out.append(f"  {'answer':<10} {answer}")
    if produced["status"] and produced["status"] != "ok":
        out.append(f"  {'':<10} task ended {produced['status']}: {produced['error'][:300]}")
    elif produced["status"] is None:
        out.append(f"  {'':<10} {DIM}no task_end — interrupted, or still running{RESET}")
    return "\n".join(out)


def explain(
    target: os.PathLike | str,
    turn: int | str | None = None,
    root: os.PathLike | str | None = None,
    show_tools: bool = False,
    show_context: bool = False,
) -> str:
    root = Path(root) if root is not None else state_dir()
    log = load(target)
    head = [f"{BOLD}{log.path.name}{RESET}" + (f" — {log.title}" if log.title else "")]
    if log.redactions:
        for record in log.redactions:
            head.append(
                f"{DIM}redacted {record.get('records')} record(s) at "
                f"{record.get('at')} — turn {record.get('turn')} is gone from this file{RESET}"
            )
    if not log.turns:
        head.append(f"{DIM}no turns in this log{RESET}")
        return "\n".join(head)
    wanted = find(log, turn)
    if not wanted:
        head.append(f"{DIM}no turn {turn} in this log ({len(log.turns)} turns){RESET}")
        return "\n".join(head)
    body = [render(t, log, root, show_tools, show_context) for t in wanted]
    return "\n".join(head + [""] + body)
