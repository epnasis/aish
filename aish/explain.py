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


def _rules_section(turn: Turn, log: Log, out: list[str]) -> None:
    """Grouped by verdict, because the three groups route to three different
    repairs (#197): nothing covered this, something covered it and retired,
    something covered it and abstained. Flattening them into file order was how
    a 24-rule corpus buried the one abstention that mattered."""
    evals = turn.of_kind("rule_eval")
    if not evals:
        why = NOT_RECORDED if not log.wrote("rule_eval") else "no ruleset evaluated for this turn"
        out.append(f"  {'rules':<10} {DIM}{why}{RESET}")
        return
    bound = {b.get("rule"): b for b in turn.of_kind("binding")}
    groups: dict[str, list[dict]] = {}
    corpus: dict = {}
    dropped = 0
    for record in evals:
        corpus = record.get("corpus") or corpus
        dropped += int(record.get("truncated") or 0)
        for row in record.get("evaluated") or []:
            groups.setdefault(str(row.get("verdict", "?")), []).append(row)

    lines = [
        f"corpus: {corpus.get('total', '?')} total, {corpus.get('active', '?')} active, "
        f"{len(corpus.get('skipped') or [])} skipped"
    ]
    for row in groups.get("bind", []):
        binding = bound.get(row.get("rule")) or {}
        seeded = "seeded" if binding.get("seeded") else f"{BOLD}NOT SEEDED{RESET}"
        lines.append(
            f"  {BOLD}bound{RESET}       {row.get('rule')} "
            f"({row.get('trigger')}, tier {row.get('tier')}) — {seeded}"
        )
        if not binding.get("satisfiable", True):
            lines.append(
                f"              {BOLD}unsatisfiable{RESET}: "
                f"{binding.get('unsatisfiable')}"
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
    for skipped in corpus.get("skipped") or []:
        lines.append(f"  skipped     {skipped.get('rule')} ({skipped.get('why')})")
    if dropped:
        lines.append(
            f"  {BOLD}{dropped} abstention row(s) dropped by the cap"
            f" — list is partial{RESET}"
        )
    _block(out, "rules", lines)


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


def _calls_section(turn: Turn, out: list[str]) -> None:
    per_call: dict[int, list[dict]] = {}
    turn_level: list[dict] = []
    for gate in turn.of_kind("gate"):
        # A verify verdict is about the TURN's answer, not about a tool call, so
        # it carries no meaningful call id. Filing it under "no tool step for
        # this call" read like a broken join when nothing was broken.
        if gate.get("at") == "verify" or not gate.get("call"):
            turn_level.append(gate)
        else:
            per_call.setdefault(int(gate["call"]), []).append(gate)

    # The arguments as the model emitted them, joined by call id (#240). The
    # rendered step carries only a per-tool label, which is a rendering of one
    # argument and silent about the rest.
    emitted = {int(c.get("call") or 0): c for c in turn.of_kind("call")}

    lines: list[str] = []
    for step in turn.of_kind("tool"):
        call = int(step.get("call") or 0)
        status = "ok" if step.get("ok") else f"{BOLD}FAILED{RESET} ({step.get('status')})"
        head = f"  #{call} {step.get('name')}"
        if step.get("summary"):
            head += f" {DIM}{str(step['summary'])[:90]}{RESET}"
        lines.append(head)
        if record := emitted.pop(call, None):
            lines.append(f"     args: {json.dumps(record.get('args') or {}, ensure_ascii=False)}")
            if record.get("truncated"):
                lines.append(
                    f"     {BOLD}… {record['truncated']} argument characters cut by "
                    f"{record.get('cap_source', 'a cap')}{RESET}"
                )
        detail = f"     → {status}, {step.get('secs', 0):.1f}s"
        if step.get("verdict_by"):
            detail += f", verdict by {step['verdict_by']}"
        if step.get("decision"):
            detail += f", decision {step['decision']}"
        lines.append(detail)
        if step.get("command"):
            lines.append(f"     command: {step['command']}")
        if step.get("error"):
            lines.append(f"     error: {str(step['error'])[:200]}")
        gates = per_call.pop(call, [])
        refused = [g for g in gates if _refused(g)]
        for gate in refused:
            lines.append(f"     {BOLD}gate{RESET} {_gate_line(gate)}")
        if len(gates) > len(refused):
            lines.append(f"     {DIM}{len(gates) - len(refused)} gate(s) allowed{RESET}")
    for call, orphans in sorted(per_call.items()):
        for gate in orphans:
            lines.append(f"  #{call} gate {_gate_line(gate)} {DIM}(no tool step recorded){RESET}")
    # A call whose arguments were recorded but which produced no tool step: the
    # call is emitted before it runs precisely so a crash still leaves its
    # arguments, and losing it here would waste that.
    for call, record in sorted(emitted.items()):
        lines.append(
            f"  #{call} {record.get('name')} "
            f"{json.dumps(record.get('args') or {}, ensure_ascii=False)}"
        )
        lines.append(f"     {BOLD}never completed{RESET} {DIM}— no tool step recorded{RESET}")
    _block(out, "calls", lines or [f"{DIM}no tool calls{RESET}"])

    if turn_level:
        # Three buckets, not two. An `advised` answer SHIPPED — folding it in
        # with refusals is the conflation the contract calls out by name.
        stopped = [g for g in turn_level if _refused(g) and _verdict_of(g) not in ADVISED_VERDICTS]
        advised = [g for g in turn_level if _verdict_of(g) in ADVISED_VERDICTS]
        vlines = [_gate_line(g) for g in stopped]
        for gate in advised:
            vlines.append(f"{_gate_line(gate)} {DIM}(answer delivered, with a note){RESET}")
        passed = len(turn_level) - len(stopped) - len(advised)
        if passed:
            vlines.append(f"{DIM}{passed} check(s) passed{RESET}")
        _block(out, "verify", vlines)


def _reasoning_section(turn: Turn, log: Log, out: list[str]) -> None:
    """What the model produced on each call of this turn (#240).

    Falls back to the rendered `thinking` step's fragment for logs written
    before the full record existed — and says which one it is showing, because
    a 26-character snippet presented as "the reasoning" is how someone concludes
    the model barely thought about it.
    """
    records = turn.of_kind("reasoning")
    if not records:
        gists = [s.get("gist") for s in turn.of_kind("thinking") if s.get("gist")]
        if not gists:
            out.append(f"  {'reasoning':<10} {DIM}{NOT_RECORDED}{RESET}")
            return
        out.append(f"  {'reasoning':<10} {DIM}fragments only — this log predates #240{RESET}")
        for gist in gists[:20]:
            out.append(f"  {'':<10} {DIM}· {str(gist)[:160]}{RESET}")
        return

    lines: list[str] = []
    for record in records:
        head = f"call {record.get('model_call', '?')}"
        if record.get("stop"):
            head += f" · stopped: {record['stop']}"
        if record.get("tokens"):
            head += f" · tokens {record['tokens']}"
        if record.get("blocks"):
            head += f" · blocks {', '.join(str(b) for b in record['blocks'])}"
        lines.append(head)
        if record.get("synthesized"):
            lines.append(f"  {BOLD}the text below is aish's sentence, not the model's{RESET}")
        if record.get("malformed"):
            # Different repair from "called with no arguments", which is what
            # this looked like before the flag existed.
            lines.append(
                f"  {BOLD}arguments did not parse{RESET} for: "
                f"{', '.join(str(n) for n in record['malformed'])}"
            )
        for label, key, cut in (
            ("thought", "text", "truncated"),
            ("said", "said", "said_truncated"),
        ):
            body = str(record.get(key) or "")
            if not body:
                continue
            lines.append(f"  {label}:")
            for para in body.splitlines():
                lines.append(f"    {para}")
            if record.get(cut):
                lines.append(
                    f"    {BOLD}… {record[cut]} more characters cut by "
                    f"{record.get('cap_source', 'a cap')}{RESET}"
                )
    _block(out, "reasoning", lines)


def _block(out: list[str], label: str, lines: list[str]) -> None:
    for i, line in enumerate(lines):
        out.append(f"  {label if i == 0 else '':<10} {line.strip() if i == 0 else line}")


def render(turn: Turn, log: Log, root: os.PathLike | str | None, show_tools: bool = False) -> str:
    out: list[str] = []
    label = f"turn {turn.ordinal}"
    if turn.counter is not None and turn.counter != turn.ordinal:
        label += f" (agent turn {turn.counter})"
    out.append(f"{BOLD}── {label} ── {turn.ts} {'─' * max(0, 40 - len(label))}{RESET}")
    out.append(f"  {'prompt':<10} {turn.prompt.strip()[:400] or DIM + '(empty)' + RESET}")

    briefs = turn.of_kind("brief")
    if not briefs:
        why = NOT_RECORDED if not log.wrote("brief") else "unchanged since an earlier turn"
        out.append(f"  {'brief':<10} {DIM}{why}{RESET}")
    for brief in briefs:
        options = brief.get("options") or {}
        menu = brief.get("tools") or {}
        status, parsed = _menu(brief, root)
        out.append(
            f"  {'brief':<10} model {options.get('model')} · num_ctx "
            f"{options.get('num_ctx')} · think {'on' if options.get('think') else 'off'}"
            f" · at model call {brief.get('model_call', '?')}"
        )
        out.append(
            f"  {'':<10} tools: {menu.get('count', '?')} on the menu "
            f"({str(menu.get('digest') or '')[:12]}… {status})"
        )
        names = menu.get("names") or []
        if names:
            out.append(f"  {'':<10} {DIM}{', '.join(str(n) for n in names)}{RESET}")
        if show_tools and parsed:
            for entry in parsed:
                function = entry.get("function") or {}
                out.append(f"  {'':<10} {BOLD}{function.get('name')}{RESET}")
                out.append(f"  {'':<10}   {str(function.get('description') or '').strip()}")
                params = function.get("parameters")
                if params:
                    out.append(f"  {'':<10}   {json.dumps(params, ensure_ascii=False)}")

    for record in turn.of_kind("context"):
        index = record.get("index") or {}
        items = index.get("items") or []
        preload = record.get("preload") or {}
        out.append(
            f"  {'context':<10} index: {len(items)} item(s) offered; "
            f"preloaded {preload.get('count', 0)} ({preload.get('mode')})"
        )
        if preload.get("names"):
            out.append(f"  {'':<10} {', '.join(str(n) for n in preload['names'])}")
    if not turn.of_kind("context"):
        why = NOT_RECORDED if not log.wrote("context") else "no context record for this turn"
        out.append(f"  {'context':<10} {DIM}{why}{RESET}")

    for record in turn.of_kind("knowledge"):
        chosen = ", ".join(
            f"{it.get('label')} (sim {it.get('sim')}, rail {it.get('rail')})"
            for it in record.get("items") or []
        )
        out.append(f"  {'knowledge':<10} {record.get('mode')}: {chosen or '(none)'}")

    _rules_section(turn, log, out)
    _calls_section(turn, out)

    for record in turn.of_kind("trim"):
        out.append(
            f"  {'trim':<10} {record.get('policy')}: {record.get('affected')} message(s), "
            f"{record.get('bytes_before')} → {record.get('bytes_after')} bytes "
            f"(keep {record.get('keep_chars')}, cap from {record.get('cap_source')})"
        )

    _reasoning_section(turn, log, out)

    answers = [
        str(m.get("content") or "")
        for m in turn.messages
        if m.get("role") == "assistant"
        and not m.get("interim")
        and (m.get("content") or "").strip()
    ]
    out.append(f"  {'answer':<10} {(answers[-1][:400] if answers else DIM + '(none)' + RESET)}")
    if turn.status and turn.status != "ok":
        out.append(f"  {'':<10} task ended {turn.status}: {turn.error[:300]}")
    elif turn.status is None:
        out.append(f"  {'':<10} {DIM}no task_end — interrupted, or still running{RESET}")
    return "\n".join(out)


def explain(
    target: os.PathLike | str,
    turn: int | None = None,
    root: os.PathLike | str | None = None,
    show_tools: bool = False,
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
    body = [render(t, log, root, show_tools) for t in wanted]
    return "\n".join(head + [""] + body)
