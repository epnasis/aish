"""Retrieval self-curation: ledger + scripted judge loop (#185, #186).

The #183 audit was a hand-run reconstruction; this module makes it a loop.
Three layers, deliberately separated because they have different costs:

Layer 1 — the LEDGER (`scan_ledger`): pure code over the session logs, zero
model calls. Since #183 every preflight injection is logged as a `knowledge`
trace step carrying per-entry sim/rail and the selection mode, and the same
logs record what the task did next. The scan pairs each injection with its
task window (user message → next user message) and measures the two signals
no judge is needed for: ENGAGEMENT (the model read_skill'd an entry that was
injected for it) and MISSES (the model deliberately read_skill'd an entry
preflight did NOT inject — retrieval failed to surface it).

Layer 2 — the JUDGE LOOP (`run_curate`, console entry point `aish-curate`,
weekly via launchd): the ORCHESTRATION LIVES IN THIS SCRIPT, not in a model
session. v1 handed one agentic session a 12-suspect work queue and an 8B
local model could not hold it — 55 reads, zero actions (#186 A/B). Now the
script loops; each model interaction is ONE bounded decision with everything
it needs in the prompt (identity, body, stats, evidence) and a forced
verdict format: repair | pin | disable | skip. No tools, no session, no
server — `backends.make_chat` is called directly, so the model is a pure
judge: text in, one verdict out. The ACTION ENVELOPE IS CODE, not prompt
obedience: `parse_verdict` accepts only the four verbs, `update_entry_meta`
rewrites frontmatter only (body preserved byte-for-byte), and deletion has
no code path at all. Everything is reversible via the knowledge git backup.
Every judged entry lands in `curation-actions.jsonl`, which also drives the
cooldown: an entry acted on (or skipped) recently is not re-judged, making a
retried launchd job idempotent without any server-side dedup.

Layer 3 — the DUPLICATE PASS: cross-entry judgment doesn't fit a one-entry
prompt, so it is its own bounded sub-pass. Candidate pairs are proposed
DETERMINISTICALLY (identity-line embedding similarity via SemanticIndex,
difflib ratio fallback — same thresholds as save_memory's near-duplicate
gate) and the judge answers one pairwise question: merge or distinct. A
merge disables the loser and optionally repairs the survivor — still inside
the same envelope, still no deletion.

Privacy (#186): evidence excerpts quote what the owner typed, so the judge
DEFAULTS TO THE LOCAL MODEL and everything stays on this machine — no
prompt ever leaves it unless --model names a cloud spec explicitly. This
box's ceiling is 8B-class (16 GB minus resident VMs; a 14B loaded here
crashed the host).

Deliberately NOT here: automatic threshold retuning. The ledger informs;
`PREFLIGHT_MIN_SIM` changes by human decision — a self-tuning floor fed by
noisy proxies is a silent-regression machine.

Testability: the judge is a `judge=` callable seam (prompt → text), the
duplicate scorer a `scores=` seam, the notifier a `notify_fn=` seam, and the
state dir and clock are injectable — no model, no network, no Keychain.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import explain, skills
from .embeddings import entry_text
from .session import RATING_NONE

# The judge reads evidence excerpts of what the owner typed, so it must run
# on a model meeting the strictest privacy bar among its sources: a LOCAL
# one. Cloud specs are for explicit experiments only (--model /
# AISH_CURATE_MODEL). qwen3:8b is also this host's memory ceiling.
DEFAULT_MODEL = "qwen3:8b"

LEDGER_DAYS = 14  # how far back the scan reads
MIN_INJECTIONS = 4  # fewer than this is not evidence of dead weight
MISS_MIN = 2  # deliberate reads of a never-injected entry before it's a miss
EVIDENCE_PER_ENTRY = 3  # recent example tasks quoted per suspect
EVIDENCE_PROMPT_CHARS = 120  # per quoted task prompt
SUSPECTS_MAX = 12  # entries judged per pass — small on purpose, weekly cadence

JUDGE_BODY_CHARS = 2500  # entry body quoted to the judge (identity is short)
JUDGE_NUM_CTX = 8192  # one bounded decision needs no more
ACTION_COOLDOWN_DAYS = 10  # judged (incl. skipped) entries rest between passes
PAIRS_MAX = 6  # duplicate pairs judged per pass
# Embedding floor for MERGE candidates — deliberately stricter than
# save_memory's DEDUP_MIN_SIM (0.55): on the live corpus every true
# duplicate scored >= 0.77 while every related-but-distinct trap (read vs
# triage, home address vs home city) sat at 0.63-0.66. The judge only sees
# pairs a human would also call "probably the same thing".
MERGE_MIN_SIM = 0.72
ACTIONS_FILE = "curation-actions.jsonl"

VERDICTS = ("repair", "pin", "disable", "skip")
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def log(msg: str) -> None:
    print(f"[curate] {msg}", file=sys.stderr, flush=True)


@dataclass
class EntryStats:
    """Per-entry ledger row, accumulated across task windows."""

    name: str
    kind: str = ""
    injections: int = 0
    reads: int = 0  # read_skill of this entry AFTER it was injected — engagement
    miss_reads: int = 0  # read_skill of this entry when it was NOT injected
    sims: list[float] = field(default_factory=list)
    rails: int = 0  # injections that entered on a name/keyword rail
    evidence: list[tuple[str, str, float | None]] = field(default_factory=list)
    # (ts, prompt head, sim) — newest kept, EVIDENCE_PER_ENTRY per entry

    @property
    def median_sim(self) -> float | None:
        if not self.sims:
            return None
        ordered = sorted(self.sims)
        return ordered[len(ordered) // 2]


@dataclass
class Ledger:
    entries: dict[str, EntryStats] = field(default_factory=dict)
    tasks: int = 0
    tasks_with_injection: int = 0
    lexical_tasks: int = 0  # selection ran without embeddings (fallback visible)

    def stat(self, name: str, kind: str = "") -> EntryStats:
        row = self.entries.setdefault(name, EntryStats(name))
        if kind and not row.kind:
            row.kind = kind
        return row


def _windows(path: Path):
    """Yield (ts, prompt, injected_items, read_skill_names) task windows from
    one session log. The `knowledge` trace step is emitted just BEFORE its
    user message is appended (agent.run_task order), so a pending injection
    attaches to the NEXT user record — the same pairing the #183 audit used."""
    pending: list[dict] | None = None
    current: dict | None = None
    for line in path.open(encoding="utf-8"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        kind = rec.get("kind")
        if kind == "trace":
            step = rec.get("step", {})
            sk = step.get("kind")
            if sk == "knowledge":
                pending = step.get("items", [])
            elif sk == "tool" and current is not None:
                if step.get("name") == "read_skill":
                    current["reads"].append(str(step.get("summary", "")).strip())
        elif kind == "message" and rec.get("role") == "user":
            if current is not None:
                yield current
            current = {
                "ts": str(rec.get("ts", "")),
                "prompt": str(rec.get("content", ""))[:500],
                "injected": pending or [],
                "reads": [],
            }
            pending = None
    if current is not None:
        yield current


def scan_ledger(state_dir, days: int = LEDGER_DAYS, now: datetime | None = None) -> Ledger:
    """Aggregate every task window in the recent logs into per-entry stats.
    Session files are date-named (session-YYYYMMDD-…), so the age cut is a
    filename comparison — no file is opened outside the window."""
    now = now or datetime.now()
    floor = f"session-{(now - timedelta(days=days)):%Y%m%d}"
    ledger = Ledger()
    for path in sorted(Path(state_dir).glob("session-*.jsonl")):
        if path.name < floor:
            continue
        try:
            for window in _windows(path):
                ledger.tasks += 1
                injected_names = set()
                lexical = False
                for item in window["injected"]:
                    name = str(item.get("label") or item.get("name") or "")
                    if not name:
                        continue
                    injected_names.add(name)
                    row = ledger.stat(name, str(item.get("kind", "")))
                    row.injections += 1
                    sim = item.get("sim")
                    if isinstance(sim, (int, float)):
                        row.sims.append(float(sim))
                    else:
                        lexical = lexical or "score" in item
                    if item.get("rail") or "score" in item:
                        row.rails += 1
                    if len(row.evidence) < EVIDENCE_PER_ENTRY:
                        row.evidence.append(
                            (
                                window["ts"],
                                " ".join(window["prompt"].split())[:EVIDENCE_PROMPT_CHARS],
                                float(sim) if isinstance(sim, (int, float)) else None,
                            )
                        )
                if injected_names:
                    ledger.tasks_with_injection += 1
                    if lexical:
                        ledger.lexical_tasks += 1
                for read in window["reads"]:
                    if not read:
                        continue
                    row = ledger.stat(read, "skill")
                    if read in injected_names:
                        row.reads += 1
                    else:
                        row.miss_reads += 1
        except OSError:
            continue
    return ledger


# --- the rule ledger (#191) ------------------------------------------------
#
# Keyed to BLAST RADIUS, not to evaluation tier. The admission rule was written
# as "no non-Tier-0 verdict ships without instrumentation", but its REASON is
# that some quantities are only visible in aggregate — and a Tier-0 regex has
# that problem the moment its trigger goes from firing on almost nothing to
# firing on any message carrying a link. The pattern is readable; the bind rate
# and the override rate are not.
#
# A reader, not a schema change: `binding` and `gate` records already carry
# everything, joined by `turn` — which is what landing the trace contract first
# bought. Zero model calls, and every signal below is a PROPOSAL. Nothing here
# edits or retires a rule; rules are owner property.

RULE_SIGNALS_IN_PUSH = 3  # a push notification is a headline, not a report
RULE_MIN_FIRES = 3  # below this, every rate is noise
RULE_BROAD_BIND_RATE = 0.33  # binding on a third of turns is a cost to accept deliberately
RULE_OVERRIDE_WRONG_RATE = 0.5  # overridden more often than not = the rule is wrong

# BINDING IS NOT FIRING, and counting only binds made the ledger unreadable.
# Three trigger kinds bind at seed and decide LATER — `action:` at the gate,
# `answer:` at turn end, `result:` when a result lands — so for them a bind
# means "it is watching" and their bind count is a constant, one per turn.
# Measured on the live corpus: 17 of 23 rules read `binds on 100% of turns`,
# every one of them advising the owner to narrow a trigger that binds by
# construction and cannot be narrowed. The signal that exists to catch an
# over-broad pattern cannot see one here, and the signal that exists to catch a
# dead rule (`never bound`) is blind to this whole family, because they always
# bind. `applied` is the count that answers the question either signal was
# asking, and the gate records already carry it.
ARMING_TRIGGERS = frozenset({"action_shape", "answer_shape", "result_state"})
# Triggers whose bind rate is a property of the grammar rather than of the
# pattern the owner wrote. `always` is here for the same reason as the arming
# three: "narrow the trigger" is not advice about a rule that has none.
STRUCTURAL_BIND_TRIGGERS = ARMING_TRIGGERS | {"always"}
# An obligation that DEMANDS something of the turn. Never applying makes one of
# these inert — the condition is not matching what the owner actually says. The
# purely prohibitive verbs are the opposite case: `never_use` that never fires
# is the rule DETERRING, which is the rule working, and flagging it as dead
# weight would propose retiring the rules that are earning their keep silently.
DEMANDING_VERBS = frozenset({"answer_from", "must_first", "answer_must_include"})
# Never-applied means something different for each of the three, and so does
# the repair. One sentence for all of them would name the wrong thing twice.
NEVER_APPLIED = {
    "answer_shape": "its condition never matched an answer",
    "action_shape": "nothing ever proposed the action it watches",
    "result_state": "the tool it watches never came back that way",
}


@dataclass
class RuleStats:
    """Per-rule ledger row. One turn contributes at most one bind."""

    name: str
    trigger: str = ""  # a rule has exactly one; the last one the window saw
    evaluated: int = 0
    binds: int = 0
    # Turns the rule actually CONSTRAINED something, as opposed to merely
    # arming. For a trigger decided at seed the two are the same number; for
    # the arming three they are not, and the difference is the whole signal.
    applied: int = 0
    abstains: int = 0
    unevaluable: int = 0
    errors: int = 0
    refusals: int = 0
    escalations: int = 0
    overrides: int = 0  # the owner allowed the violation
    complied: int = 0  # a refusal was followed by the routed reader, same turn
    origins: dict[str, int] = field(default_factory=dict)
    verbs: set[str] = field(default_factory=set)  # as COMPILED, from the bindings
    # A count alone reports a fixed file in the present tense: "29 turns could
    # not compile it" reads identically whether the breakage was this morning or
    # nine days ago and long since repaired. Both timestamps, because it is
    # their ORDER that answers it — the corpus's real errors were a grammar
    # migration passing through, every one of them clean on every turn since.
    last_error: str = ""
    last_clean: str = ""

    @property
    def still_broken(self) -> bool:
        """No clean evaluation SINCE the last error. A rule that never
        evaluated cleanly at all is broken too — "" sorts below any timestamp."""
        return bool(self.last_error) and self.last_clean <= self.last_error

    @property
    def arming(self) -> bool:
        """Binds at seed, decides later — so `binds` counts watching, not firing."""
        return self.trigger in ARMING_TRIGGERS

    @property
    def bind_rate(self) -> float:
        return self.binds / self.evaluated if self.evaluated else 0.0

    @property
    def applied_rate(self) -> float:
        return self.applied / self.binds if self.binds else 0.0

    @property
    def override_rate(self) -> float:
        return self.overrides / self.escalations if self.escalations else 0.0

    @property
    def compliance_rate(self) -> float:
        return self.complied / self.refusals if self.refusals else 0.0


@dataclass
class RuleLedger:
    rules: dict[str, RuleStats] = field(default_factory=dict)
    turns: int = 0  # turns that reached the rule engine at all
    # When the scan ran. Carried rather than passed, because "3 days ago" must
    # be relative to the window the counts came from and not to whenever
    # someone got round to formatting them — and a caller that has to remember
    # to thread a clock through is a caller that will forget.
    scanned_at: datetime | None = None

    def stat(self, name: str) -> RuleStats:
        return self.rules.setdefault(name, RuleStats(name))


def scan_ratings(state_dir, days: int = LEDGER_DAYS, now: datetime | None = None) -> dict:
    """The owner's verdicts on recent answers (#207), keyed by turn id.

    Pure code, no model. The kill metric for turn-end verification reads this:
    a thumbs-down on a turn where every rule that bound was satisfied is the
    evidence that the checking is not catching what he actually minds. Counting
    it is the whole reason the control exists.
    """
    now = now or datetime.now()
    floor = f"session-{(now - timedelta(days=days)):%Y%m%d}"
    ratings: dict[str, dict] = {}
    for path in sorted(Path(state_dir).glob("session-*.jsonl")):
        if path.name < floor:
            continue
        try:
            for line in path.open(encoding="utf-8"):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("kind") != "rating":
                    continue
                turn = str(record.get("turn", ""))
                if not turn:
                    continue
                # Last write wins: the tap records at once and a reason may
                # follow as a second record for the same turn.
                ratings[turn] = {
                    "session": path.name,
                    "rating": record.get("rating", ""),
                    "comment": record.get("comment", ""),
                    "ts": record.get("ts", ""),
                }
        except OSError:
            continue
    # A withdrawn rating is not an opinion — drop it rather than counting a
    # tap someone took back.
    return {k: v for k, v in ratings.items() if v["rating"] != RATING_NONE}


def _rule_turns(path: Path):
    """Yield one dict per turn from a session log, joined by the `turn` id the
    trace contract stamps — never by position. `curate._windows` pairs a
    knowledge step with the NEXT user message because that is the order
    run_task happens to emit in; governance records are emitted mid-turn, at
    turn end and from the server thread, so that heuristic cannot carry them."""
    turns: dict[int, dict] = {}
    for line in path.open(encoding="utf-8"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("kind") != "trace":
            continue
        step = rec.get("step", {})
        kind = step.get("kind")
        if kind not in ("rule_eval", "binding", "gate", "tool"):
            continue
        turn = step.get("turn")
        if not isinstance(turn, int):
            continue
        entry = turns.setdefault(
            turn,
            {"turn": turn, "eval": None, "ts": "", "bindings": {}, "gates": [],
             "tools": []},
        )
        if kind == "rule_eval":
            entry["eval"] = step
            # The record's own timestamp, not the step's — a verdict has no
            # clock of its own, and "when" is the difference between a file
            # that is broken and one that WAS.
            entry["ts"] = str(rec.get("ts") or "")
        elif kind == "binding":
            entry["bindings"][str(step.get("id"))] = step
        elif kind == "gate":
            entry["gates"].append(step)
        elif str(step.get("name")):
            entry["tools"].append(step)
    yield from (turns[key] for key in sorted(turns))


def _readers_of(binding: dict) -> set[str]:
    readers: set[str] = set()
    for obligation in binding.get("obligations") or []:
        if obligation.get("verb") == "route":
            readers.update(obligation.get("readers") or [])
            if isinstance(obligation.get("to"), str) and obligation["to"] != "source":
                readers.add(obligation["to"])
    return readers


def scan_rules(state_dir, days: int = LEDGER_DAYS, now: datetime | None = None) -> RuleLedger:
    """Per-rule counters over the recent session logs. Pure code, no model."""
    now = now or datetime.now()
    floor = f"session-{(now - timedelta(days=days)):%Y%m%d}"
    ledger = RuleLedger(scanned_at=now)
    for path in sorted(Path(state_dir).glob("session-*.jsonl")):
        if path.name < floor:
            continue
        try:
            for turn in _rule_turns(path):
                if turn["eval"] is None:
                    continue
                ledger.turns += 1
                for row in turn["eval"].get("evaluated") or []:
                    stat = ledger.stat(str(row.get("rule", "")))
                    stat.evaluated += 1
                    stat.trigger = str(row.get("trigger") or "") or stat.trigger
                    verdict = row.get("verdict")
                    stamp = str(turn["ts"])
                    if verdict == "error":
                        stat.last_error = max(stat.last_error, stamp)
                    else:
                        stat.last_clean = max(stat.last_clean, stamp)
                    if verdict == "bind":
                        stat.binds += 1
                        # A trigger decided at seed has already fired by the
                        # time it binds; only the arming three have to wait for
                        # a gate or a verify row to say whether they ever did.
                        if not stat.arming:
                            stat.applied += 1
                        origin = str((row.get("evidence") or {}).get("origin") or "")
                        if origin:
                            stat.origins[origin] = stat.origins.get(origin, 0) + 1
                    elif verdict == "abstain":
                        stat.abstains += 1
                    elif verdict == "unevaluable":
                        stat.unevaluable += 1
                    elif verdict == "error":
                        stat.errors += 1
                _score_gates(ledger, turn)
        except OSError:
            continue
    return ledger


def _applied(gate: dict) -> bool:
    """Did this gate row show the rule actually CONSTRAINING the turn, rather
    than watching it? One reader for all three arming triggers, because the
    three record their firing in three different places and a caller that had
    to know which is which would be re-deriving the engine from source."""
    evidence = gate.get("evidence") or {}
    if gate.get("verdict") != "allowed":
        # refused / held / advised — a decision was taken against this turn.
        return True
    if gate.get("at") == "verify":
        # An `answer:` rule that passed. `applied` says whether its condition
        # held at all: the pass row is written for armed-and-silent too, and
        # "the answer had no price in it" is not "the price was verified".
        return bool(evidence.get("applied"))
    # `result:` latched — the named tool came back in the named state.
    return bool((evidence.get("armed") or {}).get("fired"))


def _score_gates(ledger: RuleLedger, turn: dict) -> None:
    """Refusals, escalations, overrides — and compliance, which is the only
    one that needs the turn's tool steps: a refusal was COMPLIED WITH when the
    model went on to call the reader the refusal pointed it at, in the same
    turn. 'No further refusal' would count giving up as compliance."""
    called = {str(step.get("name")) for step in turn["tools"]}
    refused_bindings: set[str] = set()
    applied: set[str] = set()  # one turn contributes at most one, per rule
    for gate in turn["gates"]:
        name = str(gate.get("rule") or "")
        if not name:
            continue
        stat = ledger.stat(name)
        if stat.arming and _applied(gate):
            applied.add(name)
        if gate.get("verdict") == "refused":
            stat.refusals += 1
            refused_bindings.add(str(gate.get("binding")))
        if gate.get("escalated"):
            stat.escalations += 1
            if gate.get("verdict") == "allowed":
                stat.overrides += 1
    for name in applied:
        ledger.stat(name).applied += 1
    # The compiled obligations, snapshotted from the binding rather than read
    # off today's rule file — the contract's corollary 1. `verbs` is what tells
    # a never-applied DEMAND (inert) from a never-fired PROHIBITION (working).
    for binding in turn["bindings"].values():
        stat = ledger.stat(str(binding.get("rule") or ""))
        stat.verbs.update(
            str(o.get("verb")) for o in binding.get("obligations") or [] if o.get("verb")
        )
    for binding_id in refused_bindings:
        binding = turn["bindings"].get(binding_id)
        if binding and (_readers_of(binding) & called):
            ledger.stat(str(binding.get("rule") or "")).complied += 1


def _ago(stamp: str, now: datetime) -> str:
    """How long ago, in the words a person uses. Never a bare date: the reader
    is deciding whether to go and look at a file right now."""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return "at an unrecorded time"
    days = (now.date() - when.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def rule_signals(ledger: RuleLedger, now: datetime | None = None) -> list[tuple[str, str]]:
    """(rule, proposal) pairs for the owner to act on or ignore. Deliberately
    NOT actions: a rule is owner property, and every one of these is a
    judgement about intent that a counter can only prompt."""
    now = now or ledger.scanned_at or datetime.now()
    out: list[tuple[str, str]] = []
    for stat in sorted(ledger.rules.values(), key=lambda s: s.name):
        if stat.evaluated >= RULE_MIN_FIRES and stat.binds == 0:
            out.append((stat.name, f"never bound in {stat.evaluated} turns — dead weight?"))
        if stat.errors:
            # Whether to go and open the file is the whole decision this line
            # exists to inform, and a count cannot answer it. The corpus's real
            # errors were a grammar migration passing through in early August:
            # reported as "broken file", nine days after the last one and clean
            # on every turn since, which is a fortnight of chasing a fixed bug.
            when = _ago(stat.last_error, now)
            out.append((
                stat.name,
                f"{stat.errors} turns could not compile it — still broken, last {when}"
                if stat.still_broken
                else f"{stat.errors} turns could not compile it — last {when}, "
                     "clean on every evaluation since",
            ))
        # The `never bound` line above cannot see an arming rule: it binds every
        # turn by construction. This is the same question asked where the answer
        # lives — and only of a rule that DEMANDS something, since a prohibition
        # that never fires is deterrence, not death.
        if (
            stat.arming
            and stat.binds >= RULE_MIN_FIRES
            and stat.applied == 0
            and stat.verbs & DEMANDING_VERBS
        ):
            out.append((
                stat.name,
                f"armed on {stat.binds} turns and never once applied — "
                + NEVER_APPLIED[stat.trigger],
            ))
        # Only where narrowing is a thing the owner could do. A trigger that
        # binds by construction reads as 100% forever, and telling him to narrow
        # it every week is how a ledger loses his attention.
        if (
            stat.trigger not in STRUCTURAL_BIND_TRIGGERS
            and stat.binds >= RULE_MIN_FIRES
            and stat.bind_rate >= RULE_BROAD_BIND_RATE
        ):
            out.append((
                stat.name,
                f"binds on {stat.bind_rate:.0%} of turns — narrow the trigger, "
                "or accept the cost deliberately",
            ))
        if stat.escalations >= RULE_MIN_FIRES and stat.override_rate >= RULE_OVERRIDE_WRONG_RATE:
            out.append((
                stat.name,
                f"you overrode it on {stat.override_rate:.0%} of escalations — "
                "the rule is wrong, not the model",
            ))
        if stat.refusals >= RULE_MIN_FIRES and stat.complied == 0:
            out.append((
                stat.name,
                f"{stat.refusals} refusals and never complied — the refusal text "
                "may not be actionable",
            ))
    return out


def dead_weight(ledger: Ledger) -> list[EntryStats]:
    """Entries injected repeatedly with zero engagement, worst first. Skills
    have a hard engagement signal (read_skill); memories cannot (a fact
    influences an answer invisibly), so for them frequency + low similarity
    is the flag and the judge makes the call."""
    rows = [
        r
        for r in ledger.entries.values()
        if r.injections >= MIN_INJECTIONS and r.reads == 0
    ]
    rows.sort(key=lambda r: (-r.injections, r.median_sim or 0.0))
    return rows[:SUSPECTS_MAX]


def missing(ledger: Ledger) -> list[EntryStats]:
    """Entries the model deliberately reached for that preflight never (or
    rarely) surfaced — their identity lines need repair, not the threshold."""
    rows = [
        r
        for r in ledger.entries.values()
        if r.miss_reads >= MISS_MIN and r.miss_reads > r.injections
    ]
    rows.sort(key=lambda r: -r.miss_reads)
    return rows[:SUSPECTS_MAX]


# ---------------------------------------------------------------------------
# Layer 2: the judge loop — one entry, one bounded decision, envelope in code.


@dataclass
class Verdict:
    action: str  # one of VERDICTS
    reason: str = ""
    description: str = ""
    keywords: str = ""


def judge_prompt(entry, stat: EntryStats | None, category: str) -> str:
    """Everything the judge needs about ONE entry, plus a forced answer
    format with an example — imperative phrasing because small local models
    ignore anything softer."""
    body = entry.body.strip()
    if len(body) > JUDGE_BODY_CHARS:
        body = body[:JUDGE_BODY_CHARS] + "…"
    lines = [
        "You are a strict knowledge-base curator. Judge ONE saved entry and "
        "answer ONLY in the exact format shown at the end.",
        "",
        f'THE ENTRY ({entry.kind} "{entry.name}"):',
        f"description: {entry.description}",
        f"keywords: {', '.join(entry.keywords) if entry.keywords else '(none)'}",
        f"pinned: {'yes' if entry.pinned else 'no'}",
        "body:",
        body or "(empty)",
        "",
    ]
    if stat is not None and category == "dead-weight":
        sim = f"{stat.median_sim:.2f}" if stat.median_sim is not None else "n/a"
        lines += [
            "RETRIEVAL EVIDENCE (last two weeks): this entry was auto-injected "
            f"into {stat.injections} tasks and NEVER used ({stat.rails} entered "
            f"via keyword match, median similarity {sim}). Tasks it was "
            "injected into:",
        ]
        for ts, prompt, s in stat.evidence:
            tag = f" (sim {s:.2f})" if s is not None else ""
            lines.append(f'- {ts[:16]}{tag}: "{prompt}"')
    elif stat is not None:
        lines += [
            "RETRIEVAL EVIDENCE (last two weeks): the assistant deliberately "
            f"looked this entry up {stat.miss_reads} times, but retrieval "
            f"surfaced it only {stat.injections} times — its description/"
            "keywords fail to match the tasks that need it.",
        ]
    lines += [
        "",
        "DECIDE exactly one action:",
        "- repair — the entry is useful but its description/keywords are "
        "generic or wrong, so retrieval mis-fires; you MUST then provide a "
        "corrected description and 3-6 DISTINCTIVE keywords (never generic "
        "words like 'code', 'change', 'file').",
        "- pin — it is a standing always/never behavior rule that must apply "
        "to every task regardless of topic.",
        "- disable — stale, redundant, or noise; reversible retirement.",
        "- skip — the entry is fine as-is, or the evidence is insufficient.",
        "",
        "Answer EXACTLY in this format, nothing after it:",
        "VERDICT: <repair|pin|disable|skip>",
        "REASON: <one sentence>",
        "DESCRIPTION: <only for repair>",
        "KEYWORDS: <comma-separated, only for repair>",
        "",
        "Example:",
        "VERDICT: repair",
        "REASON: Generic keywords make it fire on unrelated tasks.",
        "DESCRIPTION: Use the trippy CLI for hotel and villa searches with "
        "live prices.",
        "KEYWORDS: hotel, villa, trippy, accommodation",
    ]
    return "\n".join(lines)


def _fields(text: str) -> dict[str, str]:
    """KEY: value lines from a judge reply, thinking spans stripped, keys
    casefolded. Later duplicates win (models sometimes restate)."""
    out: dict[str, str] = {}
    for line in _THINK_RE.sub("", text).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().isalpha():
            out[key.strip().casefold()] = value.strip()
    return out


def parse_verdict(text: str) -> Verdict | None:
    """The ONLY door into the action envelope: anything but the four verbs —
    or a repair without a replacement description — is a parse failure, and
    the caller retries once then records an unparseable skip."""
    fields = _fields(text)
    action = fields.get("verdict", "").casefold().strip(" .")
    if action not in VERDICTS:
        return None
    verdict = Verdict(
        action=action,
        reason=fields.get("reason", ""),
        description=fields.get("description", ""),
        keywords=fields.get("keywords", ""),
    )
    if action == "repair" and not verdict.description:
        return None
    return verdict


def update_entry_meta(
    path: Path,
    *,
    description: str | None = None,
    keywords: str | None = None,
    pinned: bool | None = None,
    disabled: bool | None = None,
) -> None:
    """Frontmatter-only rewrite: description/keywords/pinned/status may
    change, every other frontmatter line and the ENTIRE body are preserved
    byte-for-byte. This is the whole mutation surface of the judge loop —
    bodies and deletion have no code path here, which is what makes the
    envelope enforcement code rather than prompt obedience."""
    text = path.read_text(encoding="utf-8")
    # The same reading as `_parse` (#209). Its own regex was near-correct but
    # stricter than this writer needs — no CRLF, no trailing space on either
    # marker — so the judge loop would have raised on an entry `_parse` reads
    # happily, i.e. refused to repair a file it had just been asked about.
    header, body = skills.split_frontmatter(text)
    if not header:
        raise ValueError(f"no frontmatter in {path}")
    keep: list[str] = []
    for line in header.splitlines():
        key = line.partition(":")[0].strip().casefold()
        if key == "description" and description is not None:
            continue
        if key == "keywords" and keywords is not None:
            continue
        if key == "pinned" and pinned is not None:
            continue
        if key == "status" and disabled is not None:
            continue
        keep.append(line)
    front = keep
    if description is not None:
        front.insert(1, f"description: {skills.frontmatter_value(description)}")
    if keywords is not None:
        seen: set[str] = set()
        words = []
        # Judge-authored, and interpolated onto one line: a bare `.strip()`
        # would let `status: disabled` ride in on a keyword and retire an
        # entry the envelope refuses to disable directly (#209).
        for word in (skills.frontmatter_value(w) for w in keywords.split(",")):
            if word and word.casefold() not in seen:
                seen.add(word.casefold())
                words.append(word)
        if words:
            front.append(f"keywords: {', '.join(words[: skills.KEYWORDS_MAX])}")
    if pinned:
        front.append("pinned: yes")
    if disabled:
        front.append("status: disabled")
    path.write_text("---\n" + "\n".join(front) + "\n---\n" + body, encoding="utf-8")


def load_recent_actions(state_dir, now: datetime) -> dict[str, str]:
    """name -> last action within the cooldown window. Skips count too:
    re-judging the same 'fine as-is' entry weekly is the loop's version of
    thrash, and the ledger's stats lag reality by up to LEDGER_DAYS."""
    path = Path(state_dir) / ACTIONS_FILE
    floor = (now - timedelta(days=ACTION_COOLDOWN_DAYS)).isoformat()
    recent: dict[str, str] = {}
    if not path.is_file():
        return recent
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("ts", "") >= floor and rec.get("name"):
            recent[rec["name"]] = rec.get("action", "")
    return recent


def log_action(state_dir, record: dict) -> None:
    path = Path(state_dir) / ACTIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Layer 3: the duplicate pass — deterministic pairing, one pairwise question.


def _same_family(a, b) -> bool:
    """gws-gmail vs gws-gmail-send, gws-gmail-reply vs gws-gmail-reply-all:
    a name extending another with a dash is a FAMILY — deliberately similar
    siblings, the near-duplicate trap the first live v2 run fell into (it
    merged reply into reply-all). Families are never duplicate candidates."""
    return a.name.startswith(b.name + "-") or b.name.startswith(a.name + "-")


def dup_candidates(entries: list, scores=None) -> list[tuple]:
    """(entry_a, entry_b, similarity) pairs likely to be duplicates, best
    first. Pairing is DETERMINISTIC — identity-line embeddings when `scores`
    (SemanticIndex.scores) works, difflib ratio otherwise — reusing the
    save_memory near-duplicate thresholds. The judge only ever answers
    merge-or-distinct on a proposed pair; it never goes looking."""
    pairs: list[tuple] = []
    if scores is not None:
        for i, entry in enumerate(entries):
            sims = scores(entry_text(entry), entries)
            if sims is None:
                scores = None  # embeddings down: fall through to difflib
                break
            for other in entries[i + 1 :]:
                sim = sims.get(id(other), 0.0)
                if sim >= MERGE_MIN_SIM and not _same_family(entry, other):
                    pairs.append((entry, other, sim))
    if scores is None:
        for i, entry in enumerate(entries):
            text_a = entry_text(entry).casefold()
            for other in entries[i + 1 :]:
                ratio = difflib.SequenceMatcher(
                    None, text_a, entry_text(other).casefold()
                ).ratio()
                if ratio >= skills.DEDUP_LEXICAL_RATIO and not _same_family(entry, other):
                    pairs.append((entry, other, ratio))
    pairs.sort(key=lambda p: -p[2])
    return pairs[:PAIRS_MAX]


def pair_prompt(a, b, sim: float) -> str:
    return "\n".join(
        [
            "You are a strict knowledge-base curator. Two saved entries look "
            f"like duplicates (similarity {sim:.2f}). Decide whether they say "
            "the same thing.",
            "",
            f'ENTRY A ({a.kind} "{a.name}"): {a.description}',
            f"  keywords: {', '.join(a.keywords) if a.keywords else '(none)'}",
            f'ENTRY B ({b.kind} "{b.name}"): {b.description}',
            f"  keywords: {', '.join(b.keywords) if b.keywords else '(none)'}",
            "",
            "If they express the SAME rule or fact, answer merge and name the "
            "better-written one to keep (the other is retired, reversibly). "
            "If they cover genuinely different things, answer distinct.",
            "",
            "Answer EXACTLY in this format, nothing after it:",
            "VERDICT: <merge|distinct>",
            f"KEEP: <{a.name}|{b.name}, only for merge>",
            "REASON: <one sentence>",
        ]
    )


def parse_pair_verdict(text: str, a, b) -> tuple[str, object | None]:
    """("merge", survivor_entry) | ("distinct", None) | ("invalid", None).
    A merge naming neither entry is invalid — the envelope never guesses."""
    fields = _fields(text)
    action = fields.get("verdict", "").casefold().strip(" .")
    if action == "distinct":
        return "distinct", None
    if action == "merge":
        keep = fields.get("keep", "").strip()
        for entry in (a, b):
            if entry.name == keep:
                return "merge", entry
        return "invalid", None
    return "invalid", None


# ---------------------------------------------------------------------------


def _make_judge(model: str):
    """A prompt→text callable over the exact ollama chat convention every
    backend is adapted to. No tools, no streaming, small context — the judge
    is stateless and each call is independent."""
    from . import backends

    chat, _provider, name = backends.make_chat(model)

    def ask(prompt: str) -> str:
        response = chat(
            model=name,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            options={"num_ctx": JUDGE_NUM_CTX},
            think=False,
        )
        message = getattr(response, "message", None)
        if message is None and isinstance(response, dict):
            message = response.get("message")
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        return str(content or "")

    return ask


RETRY_NUDGE = (
    "\n\nYour previous reply did not match the required format. Reply again "
    "with ONLY the format lines, starting with 'VERDICT:'."
)


def _judged(judge, prompt: str, parse):
    """One judged decision with a single format retry; None if both fail."""
    verdict = parse(judge(prompt))
    if verdict is None:
        verdict = parse(judge(prompt + RETRY_NUDGE))
    return verdict


def run_curate(
    *,
    judge: Callable[[str], str] | None = None,
    scores=None,
    notify_fn: Callable[[str, str], object] | None = None,
    env: Mapping[str, str] | None = None,
    state_dir=None,
    now: datetime | None = None,
    dry_run: bool = False,
    model: str | None = None,
) -> int:
    """One curation pass, orchestrated HERE: suspects and duplicate pairs are
    computed deterministically, each gets one bounded judge call, verdicts
    are applied through the code envelope, and everything lands in the
    action log (which is also the cooldown that makes retries idempotent)."""
    env = os.environ if env is None else env
    now = now or datetime.now()
    if state_dir is None:
        state_dir = Path.home() / ".local" / "state" / "aish"
    if model is None:
        model = env.get("AISH_CURATE_MODEL", "").strip() or DEFAULT_MODEL

    ledger = scan_ledger(state_dir, now=now)
    entries = {
        e.name: e
        for e in skills.load_entries(str(Path.home()), None)
        if e.path is not None  # legacy lesson lines have no file to update
    }
    recent = load_recent_actions(state_dir, now)

    suspects = [(s, "dead-weight") for s in dead_weight(ledger)]
    suspects += [(s, "missed") for s in missing(ledger)]
    suspects = [
        (s, category)
        for s, category in suspects
        if s.name in entries and s.name not in recent
    ][:SUSPECTS_MAX]

    # Same scorer in dry runs as live: the first live v2 run merged pairs a
    # dry run never showed, because dry runs fell back to difflib while live
    # used embeddings. Fidelity beats saving a few local embed calls.
    if scores is None:
        try:
            from .embeddings import SemanticIndex

            scores = SemanticIndex(state_dir).scores
        except Exception:  # noqa: BLE001 — embeddings are an upgrade, never a dependency
            scores = None
    active = [e for e in entries.values()]
    pairs = [
        (a, b, sim)
        for a, b, sim in dup_candidates(active, scores)
        if a.name not in recent and b.name not in recent
    ]

    # The rule ledger rides the same weekly pass — same logs, same reading, no
    # model calls. Placed before every early return: it is INDEPENDENT of the
    # knowledge pass, and a week with nothing to curate can still hold a rule
    # that never binds or that the owner overrides every time it fires.
    # It PROPOSES and never acts — a rule is owner property, and every signal
    # is a judgement about intent that a counter can only prompt. Counters
    # nobody reads are the failure this epic is about, so the scan needed a
    # CALLER more than it needed more counters (#205).
    rule_ledger = scan_rules(state_dir, now=now)
    proposals = rule_signals(rule_ledger, now=now)
    for rule_name, proposal in proposals:
        log(f"rule {rule_name}: {proposal}")
    if rule_ledger.rules and not proposals:
        log(
            f"rules: {len(rule_ledger.rules)} evaluated over {rule_ledger.turns} "
            "turns, nothing to propose"
        )

    if dry_run:
        for rule_name, proposal in proposals:
            print(f"rule {rule_name}: {proposal}")
        print(f"suspects ({len(suspects)}):")
        for s, category in suspects:
            print(f"  {s.name} [{category}] injections={s.injections} misses={s.miss_reads}")
        print(f"duplicate candidates ({len(pairs)}):")
        for a, b, sim in pairs:
            print(f"  {a.name} <-> {b.name} ({sim:.2f})")
        return 0
    if not suspects and not pairs:
        log(f"nothing to curate ({ledger.tasks} tasks scanned)")
        _push(_notifier(notify_fn), now, "", proposals)
        return 0

    if judge is None:
        try:
            judge = _make_judge(model)
        except Exception as exc:  # noqa: BLE001 — backend build failed: config error
            log(f"cannot build judge model {model!r}: {exc}")
            return 2

    counts = {"repair": 0, "pin": 0, "disable": 0, "skip": 0, "unparseable": 0, "merge": 0}
    acted_this_run: set[str] = set()

    for stat, category in suspects:
        entry = entries[stat.name]
        assert entry.path is not None  # entries were filtered to file-backed
        verdict = _judged(judge, judge_prompt(entry, stat, category), parse_verdict)
        if verdict is None:
            counts["unparseable"] += 1
            record = {"action": "skip", "reason": "unparseable judge reply"}
        elif verdict.action == "disable" and entry.pinned:
            # Envelope guard, not judge wisdom: a pinned standing rule is the
            # owner's explicit "always apply" — the loop may repair it, never
            # retire it (the first live v2 run disabled two pinned rules).
            counts["skip"] += 1
            record = {"action": "skip", "reason": "refused: pinned standing rule"}
        else:
            counts[verdict.action] += 1
            record = {"action": verdict.action, "reason": verdict.reason}
            try:
                if verdict.action == "repair":
                    update_entry_meta(
                        entry.path,
                        description=verdict.description,
                        keywords=verdict.keywords or None,
                    )
                elif verdict.action == "pin":
                    update_entry_meta(entry.path, pinned=True)
                elif verdict.action == "disable":
                    update_entry_meta(entry.path, disabled=True)
            except (OSError, ValueError) as exc:
                log(f"apply failed for {entry.name}: {exc}")
                record = {"action": "skip", "reason": f"apply failed: {exc}"}
        acted_this_run.add(entry.name)
        log_action(
            state_dir,
            {"ts": now.isoformat(), "name": entry.name, "category": category,
             "model": model, **record},
        )
        log(f"{entry.name}: {record['action']} — {record['reason'][:80]}")

    for a, b, sim in pairs:
        if a.name in acted_this_run or b.name in acted_this_run:
            continue
        action, survivor = _judged(
            judge, pair_prompt(a, b, sim), lambda t, a=a, b=b: (
                v if (v := parse_pair_verdict(t, a, b))[0] != "invalid" else None
            ),
        ) or ("invalid", None)
        if action == "merge" and survivor is not None:
            loser = b if survivor is a else a
            if loser.kind == "skill" and survivor.kind == "memory":
                # Envelope guard: a skill (a playbook with a body) may never
                # lose a merge to a memory (a one-line fact) — the first live
                # v2 run retired a real skill in favor of its memory echo.
                log_action(
                    state_dir,
                    {"ts": now.isoformat(), "name": loser.name, "category": "duplicate",
                     "model": model, "action": "skip",
                     "reason": f"refused: skill may not lose to memory {survivor.name}"},
                )
                log(f"{loser.name}: merge refused (skill vs memory)")
                continue
            assert loser.path is not None  # entries were filtered to file-backed
            try:
                update_entry_meta(loser.path, disabled=True)
                counts["merge"] += 1
                outcome = f"merged into {survivor.name}"
            except (OSError, ValueError) as exc:
                log(f"merge apply failed for {loser.name}: {exc}")
                outcome = f"merge failed: {exc}"
            log_action(
                state_dir,
                {"ts": now.isoformat(), "name": loser.name, "category": "duplicate",
                 "model": model, "action": "disable", "reason": outcome},
            )
            acted_this_run.update((a.name, b.name))
            log(f"{loser.name}: {outcome}")
        else:
            for entry in (a, b):
                log_action(
                    state_dir,
                    {"ts": now.isoformat(), "name": entry.name, "category": "duplicate",
                     "model": model, "action": "skip",
                     "reason": "judged distinct" if action == "distinct" else "unparseable"},
                )

    summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
    log(f"pass complete: {summary or 'no decisions'}")
    _push(_notifier(notify_fn), now, summary, proposals)
    return 0


def _notifier(notify_fn):
    """The injected notifier, or the real one when configured. Resolved in one
    place so BOTH exit paths push — the early return is the one a week with no
    knowledge decisions but a misbehaving rule takes."""
    if notify_fn is not None:
        return notify_fn
    from . import notify

    return notify.pushover if notify.configured() else None


def _push(notify_fn, now: datetime, summary: str, proposals: list[tuple[str, str]]) -> None:
    """One push for the whole pass. Rule signals ride it because a proposal
    nobody sees is the same failure as a counter nobody reads — and it says
    explicitly that they changed nothing, so a rule the owner values is never
    assumed to have been touched."""
    if notify_fn is None or not (summary or proposals):
        return
    body = f"Week {now:%G-W%V}: {summary or 'no knowledge decisions'}."
    if proposals:
        body += f" {len(proposals)} rule signal(s): " + "; ".join(
            f"{name} — {text}" for name, text in proposals[:RULE_SIGNALS_IN_PUSH]
        )
    notify_fn(
        "aish knowledge curation",
        body + " All changes reversible (knowledge git; disabled entries can be "
        "re-enabled). Rule signals are proposals only — nothing changed.",
    )



# ---------------------------------------------------------------------------
# Context health (#243) — did the history policy change actually help?
# ---------------------------------------------------------------------------


@dataclass
class ContextStats:
    """What trimming did to recent sessions, from recorded evidence only.

    The claim behind the policy change is testable: aish used to discard every
    prior tool result at the start of every task, so a chat's history was gone
    by turn two and the model re-ran lookups it had already done. If that was
    right, trims per task should fall, characters destroyed should fall, and
    repeated identical calls should fall with them. If it was wrong, this says
    so — which is the point of measuring rather than asserting.
    """

    sessions: int = 0
    tasks: int = 0
    trims: int = 0
    tasks_with_trim: int = 0
    chars_destroyed: int = 0
    stubbed_messages: int = 0
    recoverable: int = 0            # stubs carrying a key back to the full text
    by_policy: dict = field(default_factory=dict)
    repeat_calls: int = 0           # identical (tool, args) issued more than once
    repeats_after_trim: int = 0     # …in a task that had already lost history
    prompt_tokens: list = field(default_factory=list)

    @property
    def trims_per_task(self) -> float:
        return self.trims / self.tasks if self.tasks else 0.0

    @property
    def recoverable_share(self) -> float:
        return self.recoverable / self.stubbed_messages if self.stubbed_messages else 0.0

    @property
    def median_prompt_tokens(self) -> int:
        if not self.prompt_tokens:
            return 0
        ordered = sorted(self.prompt_tokens)
        return ordered[len(ordered) // 2]


def scan_context(state_dir, days: int = LEDGER_DAYS, now: datetime | None = None) -> ContextStats:
    """Aggregate trimming and repeated-call evidence across recent sessions.

    Pure: reads the logs and nothing else, makes no model call, and asks no
    live object how aish behaves today. Session files are date-named, so the
    age cut is a filename comparison and no file outside the window is opened.
    """
    now = now or datetime.now()
    floor = f"session-{(now - timedelta(days=days)):%Y%m%d}"
    stats = ContextStats()
    for path in sorted(Path(state_dir).glob("session-*.jsonl")):
        if path.name < floor:
            continue
        try:
            records = _context_records(path)
        except OSError:
            continue
        if records is None:
            continue
        stats.sessions += 1
        _fold_session(records, stats)
    return stats


def _context_records(path: Path) -> list[dict] | None:
    """Every trace step in one log, in file order, or None when unreadable."""
    steps: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue  # a line that parses to a bare value is not a record
        if record.get("kind") == "task_start":
            steps.append({"kind": "task_start"})
        step = record.get("step")
        if isinstance(step, dict):
            steps.append(step)
    return steps


def _fold_session(steps: list[dict], stats: ContextStats) -> None:
    """One session's steps into the totals, task by task."""
    seen_calls: set[tuple] = set()
    trimmed_here = False
    task_open = False
    for step in steps:
        kind = step.get("kind")
        if kind == "task_start":
            stats.tasks += 1
            seen_calls = set()
            trimmed_here = False
            task_open = True
            continue
        if kind == "trim":
            stats.trims += 1
            if task_open and not trimmed_here:
                stats.tasks_with_trim += 1
            trimmed_here = True
            policy = str(step.get("policy") or "?")
            stats.by_policy[policy] = stats.by_policy.get(policy, 0) + 1
            before = int(step.get("bytes_before") or 0)
            after = int(step.get("bytes_after") or 0)
            stats.chars_destroyed += max(0, before - after)
            for stub in step.get("stubbed") or []:
                stats.stubbed_messages += 1
                if stub.get("continuation"):
                    stats.recoverable += 1
        elif kind == "call":
            # The "searched again" signature: the same tool with the same
            # arguments, issued twice in one task. It is the shape of a model
            # that lost what it already found — not proof of it, which is why
            # the count is reported beside the trim count and not as a verdict.
            key = (str(step.get("name") or ""), json.dumps(step.get("args") or {}, sort_keys=True))
            if key in seen_calls:
                stats.repeat_calls += 1
                if trimmed_here:
                    stats.repeats_after_trim += 1
            seen_calls.add(key)
        elif kind == "reasoning":
            tokens = step.get("tokens") or []
            if tokens and isinstance(tokens[0], int):
                stats.prompt_tokens.append(tokens[0])


def context_report(stats: ContextStats) -> str:
    """The numbers, plainly, with no conclusion attached to them."""
    if not stats.sessions:
        return "no sessions in the window"
    out = [
        f"sessions            {stats.sessions}",
        f"tasks               {stats.tasks}",
        f"trims               {stats.trims} ({stats.trims_per_task:.2f} per task)",
        f"tasks that trimmed  {stats.tasks_with_trim}"
        + (f" ({stats.tasks_with_trim / stats.tasks:.0%})" if stats.tasks else ""),
        f"characters dropped  {stats.chars_destroyed:,}",
        f"messages stubbed    {stats.stubbed_messages}",
        f"  recoverable       {stats.recoverable} ({stats.recoverable_share:.0%})",
        f"repeated calls      {stats.repeat_calls}"
        f" ({stats.repeats_after_trim} in a task that had already lost history)",
        f"median prompt       {stats.median_prompt_tokens:,} tokens",
    ]
    for policy, count in sorted(stats.by_policy.items(), key=lambda kv: -kv[1]):
        out.append(f"  policy {policy:<20} {count}")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="aish retrieval self-curation: scripted judge loop (#185/#186)"
    )
    parser.add_argument(
        "--context",
        action="store_true",
        help="report what trimming did to recent sessions; no model calls, no writes",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=LEDGER_DAYS,
        help="how far back to look (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list suspects and duplicate candidates; no model calls, no writes",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "judge model spec (default: $AISH_CURATE_MODEL or "
            f"{DEFAULT_MODEL}; keep it LOCAL — prompts quote excerpts from "
            "private sessions)"
        ),
    )
    args = parser.parse_args()
    if args.context:
        # One source of truth for "where the logs are" — the reader already
        # resolves it, honouring AISH_STATE_DIR.
        print(context_report(scan_context(explain.state_dir(), days=args.days)))
        return 0
    return run_curate(dry_run=args.dry_run, model=args.model)


if __name__ == "__main__":
    sys.exit(main())
