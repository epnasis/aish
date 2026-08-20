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


def _given(turn: Turn, log: Log, root: os.PathLike | str | None) -> dict:
    """What the model was handed: the system text, the tool menu, the knowledge
    offered and preloaded, and the rules in force."""
    briefs = []
    for record in turn.of_kind("brief"):
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
        "state": RECORDED if briefs else (MISSING if not log.wrote("brief") else EMPTY),
        "briefs": briefs,
        # An empty `briefs` with state EMPTY means the menu and system text were
        # unchanged since an earlier turn — the interning rule, not a gap.
        "carried": bool(not briefs and log.wrote("brief")),
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
                  section="did", call=call["call"])
        elif not call["ok"]:
            _note(rows, "tool_failed",
                  f"{call['name']} failed ({call['status']})",
                  section="did", call=call["call"])
        for gate in call["refused"]:
            _note(rows, "gate_refused",
                  f"{call['name']} was refused by {gate.get('rule') or gate.get('at')}",
                  section="did", call=call["call"])
        if call["args_truncated"]:
            _note(rows, "args_truncated",
                  f"{call['args_truncated']} characters of {call['name']}'s arguments were "
                  f"cut from the record by {call['cap_source'] or 'a cap'}",
                  section="did", call=call["call"])

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

    for row in doc["given"]["rules"].get("groups", {}).get("abstain", []):
        ev = row.get("evidence") or {}
        detail = ""
        if "sim" in ev and "floor" in ev:
            detail = f" (similarity {ev['sim']} against a floor of {ev['floor']})"
        _note(rows, "rule_abstained",
              f"the rule {row.get('rule')} was evaluated and did not bind{detail}",
              section="given")

    for call in doc["thought"]["calls"]:
        if call["truncated"]:
            _note(rows, "reasoning_truncated",
                  f"{call['truncated']} characters of reasoning were cut from the record "
                  f"by {call['cap_source'] or 'a cap'}",
                  section="thought", model_call=call["model_call"])
        if call["malformed"]:
            _note(rows, "args_malformed",
                  "the model's arguments did not parse for: "
                  + ", ".join(str(n) for n in call["malformed"]),
                  section="thought", model_call=call["model_call"])
        if call["stop"] and call["stop"] not in ORDINARY_STOPS:
            _note(rows, "stop_unusual",
                  f"the model stopped with reason {call['stop']!r}",
                  section="thought", model_call=call["model_call"])

    for record in doc["trim"]:
        if record.get("stubbed"):
            listed = ", ".join(
                f"{s.get('tool')} (#{s.get('at')})" for s in record["stubbed"]
            )
            _note(rows, "result_stubbed",
                  f"{record.get('affected')} earlier result(s) were replaced with a stub "
                  f"before this call: {listed}",
                  section="did")
        elif record.get("affected"):
            _note(rows, "result_stubbed",
                  f"{record.get('affected')} earlier result(s) were stubbed; which ones "
                  f"was not recorded",
                  section="did")

    for record in doc["steering"]:
        _note(rows, "steering",
              "you typed while the task was running and it was folded into the model's "
              f"messages: {record['text'][:120]}",
              section="given")

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
              section="thought", model_call=fullest["model_call"])

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
        why = " (unchanged since an earlier turn)" if given["carried"] else ""
        out.append(f"  {'brief':<10} {_state_note(given['state'], why)}")
    for brief in given["briefs"]:
        options = brief["options"]
        out.append(
            f"  {'brief':<10} model {options.get('model')} · num_ctx "
            f"{options.get('num_ctx')} · think {'on' if options.get('think') else 'off'}"
            f" · at model call {brief.get('model_call', '?')}"
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


def render(
    turn: Turn,
    log: Log,
    root: os.PathLike | str | None,
    show_tools: bool = False,
    show_context: bool = False,
) -> str:
    """The terminal rendering of one turn — a dumb renderer over `dossier`."""
    doc = dossier(turn, log, root)
    out: list[str] = []
    label = f"turn {doc['ordinal']}"
    if doc["counter"] is not None and doc["counter"] != doc["ordinal"]:
        label += f" (agent turn {doc['counter']})"
    out.append(f"{BOLD}── {label} ── {doc['ts']} {'─' * max(0, 40 - len(label))}{RESET}")
    out.append(f"  {'prompt':<10} {doc['prompt'].strip()[:400] or DIM + '(empty)' + RESET}")

    _given_lines(doc["given"], show_tools, show_context, out)
    _did_lines(doc["did"], out)

    # Steering text typed WHILE the task ran. It is folded into the model's
    # messages mid-task without passing through the recorder, so its trace step
    # is the only place it exists — and it is not restored when a session
    # resumes, so a dossier that skipped it would show a turn the model was
    # never actually given (#241).
    for record in doc["steering"]:
        out.append(f"  {'steering':<10} typed mid-task: {record['text'][:300]}")

    for record in doc["trim"]:
        out.append(
            f"  {'trim':<10} {record.get('policy')}: {record.get('affected')} message(s), "
            f"{record.get('bytes_before')} → {record.get('bytes_after')} bytes "
            f"(keep {record.get('keep_chars')}, cap from {record.get('cap_source')})"
        )
        # WHICH results were stubbed. Without this the transcript still shows the
        # full text, so a reader can conclude the model ignored something it was
        # never given.
        if stubbed := record.get("stubbed"):
            listed = ", ".join(f"#{s.get('at')} {s.get('tool')}" for s in stubbed)
            out.append(f"  {'':<10} {BOLD}stubbed for the model{RESET}: {listed}")
            if record.get("stubbed_truncated"):
                out.append(f"  {'':<10} … and {record['stubbed_truncated']} more")
        elif record.get("affected"):
            out.append(
                f"  {'':<10} {DIM}which messages: {NOT_RECORDED} (log predates #241){RESET}"
            )

    _thought_lines(doc["thought"], out)
    _produced_lines(doc["produced"], out)

    produced = doc["produced"]
    answer = produced["answer"][:400] if produced["answer"] else DIM + "(none)" + RESET
    out.append(f"  {'answer':<10} {answer}")
    if produced["status"] and produced["status"] != "ok":
        out.append(f"  {'':<10} task ended {produced['status']}: {produced['error'][:300]}")
    elif produced["status"] is None:
        out.append(f"  {'':<10} {DIM}no task_end — interrupted, or still running{RESET}")

    # Facts about this turn that are worth a look, computed over the dossier and
    # never over the log — so the terminal and the panel say the same things.
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
    return "\n".join(out)


def explain(
    target: os.PathLike | str,
    turn: int | None = None,
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
    wanted = [t for t in log.turns if turn is None or t.ordinal == turn or t.counter == turn]
    if not wanted:
        head.append(f"{DIM}no turn {turn} in this log ({len(log.turns)} turns){RESET}")
        return "\n".join(head)
    body = [render(t, log, root, show_tools, show_context) for t in wanted]
    return "\n".join(head + [""] + body)
