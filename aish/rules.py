"""The rules engine (#191): owner-authored turn contracts the HARNESS enforces.

The fourth artifact class. Skills *may* be consulted, memory *may* be recalled,
tools *may* be invoked — a rule MUST be obeyed, because obeying it is not the
model's job. A rule on disk is inert; when a trigger matches a turn the harness
creates a **binding** — (rule, evidence snapshot, obligations) attached to that
turn — and every downstream check queries the binding, never the file.

Everything here is pure: parsing, trigger evaluation, the binding object and the
gate verdict. `agent.py` owns the enforcement points (seed, gate) and the trace
emission; this module owns the vocabulary and the decisions, so both are
testable with no Agent, no model and no filesystem beyond the rule files.

Read `docs/rules-engine.md` before changing any of it — in particular the two
lines that look arbitrary and are not: rules only ever RESTRICT (there is no
allow verb, on purpose), and a refusal is BOUNDED (a gate that refuses forever
wedges a small model into a stall-out).
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import NamedTuple

from . import skills

GLOBAL_RULES_DIR = Path.home() / ".config" / "aish" / "rules"

# Trigger kinds. v1 ships the two Tier-0 ones; the remaining four (tool
# outcome, task domain, action shape, deliverable shape) are named in
# docs/rules-engine.md and arrive as new evaluators against this same runtime.
TRIGGER_MESSAGE_SHAPE = "message_shape"
TRIGGER_SESSION_CONTEXT = "session_context"
TRIGGER_KINDS = frozenset({TRIGGER_MESSAGE_SHAPE, TRIGGER_SESSION_CONTEXT})

# Obligation verbs. Every one is a RESTRICTION — see the module docstring.
VERB_ROUTE = "route"
VERB_PROHIBIT = "prohibit"
VERB_DISCLOSE = "disclose"

# Trigger verdicts (docs/trace-contract.md §3.1) — a closed vocabulary, not a
# sentence. `error` is a broken rule FILE (unparseable trigger); `unevaluable`
# is a working rule whose evaluator failed at runtime. They fail in opposite
# directions and must never be conflated: a typo must not hold every turn.
VERDICT_BIND = "bind"
VERDICT_ABSTAIN = "abstain"
VERDICT_UNEVALUABLE = "unevaluable"
VERDICT_ERROR = "error"

# Failure direction for an UNEVALUABLE trigger, declared per rule.
FAIL_OPEN = "open"  # do not bind — the owner is watching
FAIL_HOLD = "hold"  # bind, and take the first violation straight to the owner
FAIL_DIRECTIONS = frozenset({FAIL_OPEN, FAIL_HOLD})

# Bounded refuse-first. The model gets this many instructive refusals per
# binding; the next violation escalates to the owner. Its own constant rather
# than GATE_MAX_REFUSALS so tuning the skill gate cannot silently retune this.
RULE_MAX_REFUSALS = 2

# Write-time caps (contract §8.5) — named, so a truncated record can say which
# cap cut it.
RULE_EVAL_MAX = 24  # evaluated[] rows; abstentions drop first, binds never
GATE_MESSAGE_CHARS = 400
ACTION_ARGS_CHARS = 400
EVIDENCE_CHARS = 600

_SLUG_WORDS = re.compile(r"[^a-z0-9]+")


class RuleError(ValueError):
    """A rule file the loader can parse but not compile."""


@dataclass(frozen=True)
class Rule:
    """One owner-authored file. Inert until a turn binds it."""

    name: str
    description: str
    prose: str
    trigger: str
    tier: int
    fail: str
    obligations: tuple[dict, ...]
    status: str = ""
    expires: date | None = None
    path: Path | None = None
    # Trigger parameters, pre-compiled at load so a bad regex is an `error`
    # verdict on a named rule rather than an exception inside the gate.
    pattern: re.Pattern | None = None
    field_name: str = ""
    equals: str = ""
    not_equals: str = ""
    # Non-empty when the file could not be compiled — carried rather than
    # raised so the rule still appears in the corpus with an `error` verdict.
    error: str = ""

    @property
    def routed_tool(self) -> str:
        for obligation in self.obligations:
            if obligation["verb"] == VERB_ROUTE:
                return str(obligation["to"])
        return ""

    @property
    def disclosure_terms(self) -> tuple[str, ...]:
        for obligation in self.obligations:
            if obligation["verb"] == VERB_DISCLOSE:
                return tuple(obligation.get("terms") or ())
        return ()


@dataclass
class TurnContext:
    """The facts a Tier-0 trigger is a function of. Gathered by the HARNESS —
    never framed or summarised by the acting model (contract corollary 3)."""

    task: str = ""
    origin: str = "user"


@dataclass
class Binding:
    """The runtime object: (rule, evidence snapshot, obligations) for one turn.

    Mutable because the turn moves under it — the routed tool gets called, its
    verdict lands, the model discloses or does not — and every gate decision is
    a function of that state plus the compiled obligations. The obligations are
    COPIED here, so a rule file edited mid-turn cannot retroactively change what
    governed it (contract corollary 1).
    """

    id: str
    rule: Rule
    evidence: dict
    obligations: tuple[dict, ...]
    at: str = "seed"
    satisfiable: bool = True
    unsatisfiable: tuple[str, ...] = ()
    seeded: bool = False
    # Turn state.
    rounds: int = 0  # refusals issued so far
    max_rounds: int = RULE_MAX_REFUSALS
    overridden: bool = False  # the owner allowed the violation for this turn
    route_calls: int = 0
    route_status: str = ""  # #192's envelope status of the last routed call
    disclosed: bool = False
    _pending_disclosure: bool = field(default=False, repr=False)

    @property
    def name(self) -> str:
        return self.rule.name

    def note_tool_result(self, tool: str, status: str) -> None:
        """A tool call completed. Only the ROUTED tool moves binding state —
        this is what makes `disclose` reachable: a route that came back
        `incomplete` is the failure the owner must be told about."""
        if tool and tool == self.rule.routed_tool:
            self.route_calls += 1
            self.route_status = status or ""
            self._pending_disclosure = status != "ok"
            if self._pending_disclosure:
                self.disclosed = False

    def note_assistant_text(self, text: str) -> None:
        """Model prose emitted alongside its tool calls — the only place a
        mid-task disclosure can live, since a text-ONLY turn ends the task.

        Tier 0, and deliberately weak: it checks that the failure was NAMED,
        not that it was named well. The strong check is Verify (turn end), and
        it needs a judge. Until then the property this buys is the one that
        actually failed in #190: substitution can no longer be SILENT.
        """
        if not self._pending_disclosure or not text:
            return
        lowered = text.casefold()
        terms = self.rule.disclosure_terms
        if not terms or any(term in lowered for term in terms):
            self.disclosed = True
            self._pending_disclosure = False

    def disclosure_met(self) -> bool:
        """Whether an `unless: disclosed` prohibition currently lifts.

        Three states, and only the third lifts it: the route was never tried;
        the route SUCCEEDED (there is nothing to substitute for — the escape is
        asking the owner, which is what "without asking" in the canonical rule
        means); the route failed and the model said so.
        """
        return self.route_calls > 0 and self.route_status != "ok" and self.disclosed


@dataclass
class GateVerdict:
    """One binding's answer about one proposed call."""

    verdict: str  # "allowed" | "refused" | "escalate"
    binding: Binding
    evidence: dict
    message: str = ""
    round: int = 0


# --------------------------------------------------------------------- load


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,\s]+", value or "") if part.strip()]


def _terms_from_state(state: str) -> list[str]:
    """Default disclosure terms from the state slug: `transcript_empty` →
    ['transcript', 'empty']. Explicit `disclosure_terms:` overrides it."""
    return [w for w in _SLUG_WORDS.split(state.casefold()) if len(w) > 2]


class _Compiled(NamedTuple):
    """What survives compilation: the trigger's parameters, resolved once at
    load, plus the obligations. Nothing here is re-derived at gate time."""

    trigger: str
    obligations: tuple[dict, ...]
    pattern: re.Pattern | None
    field_name: str
    equals: str
    not_equals: str


def _compile(front: dict[str, str]) -> _Compiled:
    """Trigger parameters + compiled obligations, or a RuleError naming the
    problem. Everything a gate needs is resolved HERE so the gate itself is
    pure set membership — the cost law (#191) falls out of that, not out of a
    policy bolted on afterwards."""
    trigger = front.get("trigger", "").strip()
    if trigger not in TRIGGER_KINDS:
        raise RuleError(
            f"unknown trigger {trigger!r} — v1 supports "
            + ", ".join(sorted(TRIGGER_KINDS))
        )
    pattern: re.Pattern | None = None
    field_name = equals = not_equals = ""
    if trigger == TRIGGER_MESSAGE_SHAPE:
        source = front.get("match", "").strip()
        if not source:
            raise RuleError("message_shape needs a `match:` regex")
        try:
            pattern = re.compile(source)
        except re.error as exc:
            raise RuleError(f"unparseable `match:` regex — {exc}") from exc
    else:
        field_name = front.get("field", "").strip()
        if field_name != "origin":
            raise RuleError("session_context supports `field: origin` in v1")
        equals, not_equals = front.get("is", "").strip(), front.get("is_not", "").strip()
        if bool(equals) == bool(not_equals):
            raise RuleError("session_context needs exactly one of `is:` / `is_not:`")

    obligations: list[dict] = []
    if route := front.get("route", "").strip():
        obligations.append({"verb": VERB_ROUTE, "to": route, "of": "deliverable"})
    if prohibited := _split_list(front.get("prohibit", "")):
        obligation: dict = {"verb": VERB_PROHIBIT, "what": prohibited}
        unless = front.get("unless", "").strip()
        if unless:
            if unless != "disclosed":
                raise RuleError(f"unknown `unless:` condition {unless!r} — v1 has 'disclosed'")
            obligation["unless"] = unless
        obligations.append(obligation)
    if state := front.get("disclose", "").strip():
        terms = _split_list(front.get("disclosure_terms", "")) or _terms_from_state(state)
        obligations.append({"verb": VERB_DISCLOSE, "state": state, "terms": terms})
    if not obligations:
        raise RuleError("a rule with no obligation restricts nothing")
    if any(o.get("unless") == "disclosed" for o in obligations) and not any(
        o["verb"] == VERB_DISCLOSE for o in obligations
    ):
        raise RuleError("`unless: disclosed` needs a `disclose:` obligation to name the state")
    return _Compiled(trigger, tuple(obligations), pattern, field_name, equals, not_equals)


def _parse(path: Path) -> Rule:
    """One rule file. A file that cannot be COMPILED still yields a Rule —
    carrying its error — because a broken rule must be visible in the corpus
    and in the log, not silently absent (contract corollary 2)."""
    text = path.read_text(encoding="utf-8")
    front: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            _, header, body = parts
            for line in header.strip().splitlines():
                key, _, value = line.partition(":")
                if key.strip():
                    front[key.strip()] = value.strip()
    name = front.get("name") or path.stem
    description = front.get("description", "")
    prose = body.strip()
    if not description:
        for line in prose.splitlines():
            if line.strip():
                description = line.strip().lstrip("# ").strip()
                break
    status = front.get("status", "").casefold()
    expires = skills.parse_expiry(front.get("expires", ""), path)
    try:
        tier = int(front.get("tier", "0") or 0)
    except ValueError:
        tier = 0
    fail = front.get("fail", FAIL_OPEN).strip().casefold() or FAIL_OPEN
    if fail not in FAIL_DIRECTIONS:
        warnings.warn(
            f"{path}: unknown fail direction {fail!r}; using {FAIL_OPEN!r}", stacklevel=2
        )
        fail = FAIL_OPEN
    try:
        compiled = _compile(front)
    except RuleError as exc:
        return Rule(
            name=name,
            description=description,
            prose=prose,
            trigger=front.get("trigger", "").strip() or "unknown",
            tier=tier,
            fail=fail,
            obligations=(),
            status=status,
            expires=expires,
            path=path,
            error=str(exc),
        )
    return Rule(
        name=name,
        description=description,
        prose=prose,
        trigger=compiled.trigger,
        tier=tier,
        fail=fail,
        obligations=compiled.obligations,
        status=status,
        expires=expires,
        path=path,
        pattern=compiled.pattern,
        field_name=compiled.field_name,
        equals=compiled.equals,
        not_equals=compiled.not_equals,
    )


_CACHE: dict[Path, tuple[float, Rule]] = {}


def rule_dirs() -> list[Path]:
    """Global only, matching memory. A rule is a policy about how aish behaves,
    not a property of a checkout, and a project-local rule file would be a
    policy anyone who hands you a repo can write."""
    return [GLOBAL_RULES_DIR]


def load_rules(dirs: list[Path] | None = None) -> list[Rule]:
    """Every rule file, parse-cached by mtime — the whole corpus, including
    disabled, expired and broken ones. Filtering is the caller's job so the
    `rule_eval` record can distinguish "no rule existed" from "a rule existed
    and was retired" (contract §3.1)."""
    rules: list[Rule] = []
    for directory in dirs if dirs is not None else rule_dirs():
        try:
            paths = sorted(directory.glob("*.md"))
        except OSError:
            continue
        for path in paths:
            try:
                mtime = path.stat().st_mtime
                cached = _CACHE.get(path)
                if cached is not None and cached[0] == mtime:
                    rules.append(cached[1])
                    continue
                rule = _parse(path)
            except OSError:
                continue
            _CACHE[path] = (mtime, rule)
            rules.append(rule)
    return rules


def partition(rules: list[Rule], today: date | None = None) -> tuple[list[Rule], list[dict]]:
    """(active, skipped) — the lifecycle inherited VERBATIM from the knowledge
    layer, `status: disabled` and `expires:`, evaluated at read time so a
    long-running process crosses an expiry without an mtime change."""
    active, skipped = [], []
    for rule in rules:
        if rule.status == "disabled":
            skipped.append({"rule": rule.name, "why": "disabled"})
        elif not skills.lifecycle_active(rule.status, rule.expires, today):
            skipped.append({"rule": rule.name, "why": "expired"})
        else:
            active.append(rule)
    return active, skipped


# ----------------------------------------------------------------- evaluate


def evaluate(rule: Rule, ctx: TurnContext) -> tuple[str, dict]:
    """(verdict, evidence) for one rule against one turn.

    Evidence is the INPUTS the verdict was a function of, never a rendering of
    the verdict (contract §4) — so a trigger can be re-examined after the file
    changes, and a corpus of them can be counted.
    """
    if rule.error:
        return VERDICT_ERROR, {"error": rule.error[:EVIDENCE_CHARS]}
    if rule.trigger == TRIGGER_MESSAGE_SHAPE:
        assert rule.pattern is not None
        match = rule.pattern.search(ctx.task or "")
        evidence: dict = {
            "on": "task",
            "pattern": rule.pattern.pattern,
            "matched": match is not None,
        }
        if match is not None:
            evidence["span"] = [match.start(), match.end()]
            return VERDICT_BIND, evidence
        return VERDICT_ABSTAIN, evidence
    value = getattr(ctx, rule.field_name, "")
    required = f"== {rule.equals}" if rule.equals else f"!= {rule.not_equals}"
    evidence = {"field": rule.field_name, "value": value, "required": required}
    hit = value == rule.equals if rule.equals else value != rule.not_equals
    return (VERDICT_BIND if hit else VERDICT_ABSTAIN), evidence


def unsatisfiable(rule: Rule, known_tools: set[str] | None) -> list[str]:
    """Obligations naming a tool that is not exposed. Caught at BIND time so
    "the rule bound but its tool was gone" is a recorded fact rather than an
    inference from a later failure — a route to a deleted tool would otherwise
    refuse every alternative and offer nothing."""
    if not known_tools:
        return []
    missing = []
    for obligation in rule.obligations:
        if obligation["verb"] == VERB_ROUTE and obligation["to"] not in known_tools:
            missing.append(str(obligation["to"]))
    return missing


def bind(
    rule: Rule,
    evidence: dict,
    binding_id: str,
    known_tools: set[str] | None = None,
    at: str = "seed",
    max_rounds: int = RULE_MAX_REFUSALS,
) -> Binding:
    missing = unsatisfiable(rule, known_tools)
    return Binding(
        id=binding_id,
        rule=rule,
        evidence=evidence,
        obligations=rule.obligations,
        at=at,
        satisfiable=not missing,
        unsatisfiable=tuple(missing),
        max_rounds=max_rounds,
    )


# --------------------------------------------------------------------- gate


ROUTE_FIRST = (
    "NOT EXECUTED — the rule '{rule}' governs this turn: {description}\n"
    "The answer must come from {route}, and {tool} is off-limits until it has "
    "been tried. Call {route} now. Do NOT retry {tool}."
)

ROUTE_SUCCEEDED = (
    "NOT EXECUTED — the rule '{rule}' governs this turn: {description}\n"
    "{route} already answered this turn, so {tool} may not be used as a second "
    "source. Answer from what {route} returned, or ask the user whether to "
    "widen the sources."
)

DISCLOSE_FIRST = (
    "NOT EXECUTED — the rule '{rule}' governs this turn: {description}\n"
    "{route} came back {status} ({state}), and substituting another source "
    "WITHOUT SAYING SO is exactly what this rule forbids. Say plainly, in your "
    "next message, that {state_words} — then {tool} is allowed. Do not present "
    "another source's material as if it came from {route}."
)

PROHIBITED = (
    "NOT EXECUTED — the rule '{rule}' governs this turn: {description}\n"
    "{tool} is prohibited for this turn. {advice}"
)

UNSATISFIABLE_NOTE = (
    " (the rule routes to {route}, which is not available in this session — "
    "say so in your answer rather than substituting silently)"
)

ESCALATION_REFUSAL = (
    "NOT EXECUTED — the rule '{rule}' still forbids {tool}, and the owner is "
    "not available to make an exception. STOP retrying: finish the task with "
    "what the rule allows, and state plainly in your answer what you could not "
    "do and why."
)

OWNER_DENIED = (
    "USER DENIED the exception to the rule '{rule}' — {tool} was NOT run. Do "
    "not retry it; finish with what the rule allows and say what you could not do."
)


def gate(bindings: list[Binding], tool: str) -> list[GateVerdict]:
    """Every active binding's verdict on one proposed call, in bind order.

    Set membership against precomputed obligations — Tier 0 by construction,
    which is what the cost law requires of a per-dispatch check. Composition is
    the UNION of restrictions: monotone, order-independent, no precedence
    algebra, because every obligation is a restriction and a restriction can
    only ever add. The caller stops at the first non-allowed verdict; the
    verdicts before it are the armed-gate `allowed` records §5 requires.
    """
    verdicts: list[GateVerdict] = []
    for binding in bindings:
        verdict = _gate_one(binding, tool)
        verdicts.append(verdict)
        if verdict.verdict != "allowed":
            break
    return verdicts


def _gate_one(binding: Binding, tool: str) -> GateVerdict:
    rule = binding.rule
    for obligation in binding.obligations:
        if obligation["verb"] != VERB_PROHIBIT or tool not in obligation["what"]:
            continue
        evidence = {
            "obligation": VERB_PROHIBIT,
            "matched": tool,
            "unless": obligation.get("unless", ""),
            "route": rule.routed_tool,
            "route_used": binding.route_calls > 0,
            "route_status": binding.route_status,
            "disclosed": binding.disclosed,
            "overridden": binding.overridden,
        }
        if binding.overridden:
            return GateVerdict("allowed", binding, evidence)
        if obligation.get("unless") == "disclosed" and binding.disclosure_met():
            return GateVerdict("allowed", binding, evidence)
        message = _refusal_text(binding, obligation, tool)
        binding.rounds += 1
        if binding.rounds > binding.max_rounds:
            return GateVerdict("escalate", binding, evidence, message, binding.rounds)
        return GateVerdict("refused", binding, evidence, message, binding.rounds)
    return GateVerdict("allowed", binding, {"obligation": None, "matched": None})


def _refusal_text(binding: Binding, obligation: dict, tool: str) -> str:
    """Every refusal is INSTRUCTIVE (#190 decision 2): it names the rule, says
    why, and says what to do instead. A refusal the model cannot act on is a
    wedge, and an uninstructive one is a different incident class from an
    ignored one — which is why the text is recorded, not just sent.

    Returned UNCAPPED. `GATE_MESSAGE_CHARS` is a WRITE-time cap (§8.5) and
    belongs where the record is written, never here: applying it to the text
    handed to the model truncated the canonical rule's disclose refusal at
    exactly 400 chars, losing its second half — "do not present another
    source's material as if it came from <route>", which is the one sentence
    the whole engine exists to deliver. An instruction cut mid-clause is the
    uninstructive refusal this docstring forbids."""
    rule = binding.rule
    common = {
        "rule": rule.name,
        "description": rule.description,
        "tool": tool,
        "route": rule.routed_tool,
    }
    if rule.routed_tool and binding.unsatisfiable:
        advice = UNSATISFIABLE_NOTE.format(route=rule.routed_tool).strip()
        return PROHIBITED.format(advice=advice, **common)
    if rule.routed_tool and obligation.get("unless") == "disclosed":
        if binding.route_calls == 0:
            return ROUTE_FIRST.format(**common)
        if binding.route_status == "ok":
            return ROUTE_SUCCEEDED.format(**common)
        state = ""
        for candidate in binding.obligations:
            if candidate["verb"] == VERB_DISCLOSE:
                state = str(candidate["state"])
        return DISCLOSE_FIRST.format(
            status=binding.route_status or "empty-handed",
            state=state,
            state_words=state.replace("_", " ").replace("-", " "),
            **common,
        )
    advice = (
        f"Use {rule.routed_tool} instead." if rule.routed_tool else "Choose another approach."
    )
    return PROHIBITED.format(advice=advice, **common)


# -------------------------------------------------------------------- seed


SEED_HEADER = (
    "RULES IN FORCE FOR THIS TURN — the harness enforces these. They are NOT "
    "advice: a call that violates one is refused before it runs, whatever you "
    "decide about it."
)


def seed_text(bindings: list[Binding]) -> str:
    """The *prose explains* half of the pairing (#190): the model is told, in
    plain language, what constrains it and why — so it is never AMBUSHED by a
    gate. The gate enforces regardless of whether the model agrees; this text
    exists so a refusal is a reminder rather than a surprise."""
    if not bindings:
        return ""
    lines = [SEED_HEADER]
    for binding in bindings:
        rule = binding.rule
        lines.append(f"\n• {rule.name} — {rule.description}")
        for obligation in binding.obligations:
            lines.append("  " + _obligation_line(obligation))
        if binding.unsatisfiable:
            lines.append(
                "  · WARNING: "
                + ", ".join(binding.unsatisfiable)
                + " is not available in this session — say so in your answer "
                "instead of substituting another source silently."
            )
        if rule.prose:
            lines.append("  " + rule.prose.replace("\n", "\n  "))
    return "\n".join(lines)


def _obligation_line(obligation: dict) -> str:
    verb = obligation["verb"]
    if verb == VERB_ROUTE:
        return f"· MUST: the {obligation['of']} comes from {obligation['to']}."
    if verb == VERB_PROHIBIT:
        what = ", ".join(obligation["what"])
        if obligation.get("unless") == "disclosed":
            return (
                f"· MUST NOT call {what} — unless the routed tool has already "
                "failed AND you have said so in plain text first."
            )
        return f"· MUST NOT call {what} for this turn."
    return (
        f"· MUST state it plainly if this happens: {obligation['state']} — "
        "never patch over it with another source."
    )


# ------------------------------------------------------------------ records


def eval_record(
    rules: list[Rule],
    skipped: list[dict],
    rows: list[dict],
    at: str = "seed",
) -> dict:
    """The §3.1 `rule_eval` record. Emitted for EVERY turn, including when the
    corpus is empty — "no rule was evaluated for this turn" is an answer #197
    needs stated, not inferred from an absent record (contract fork 8)."""
    kept, dropped = _cap_rows(rows)
    return {
        "kind": "rule_eval",
        "at": at,
        "corpus": {
            "total": len(rules) + len(skipped),
            "active": len(rules),
            "skipped": skipped,
        },
        "evaluated": kept,
        "truncated": dropped,
    }


def _cap_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Binds and refusals are NEVER dropped by a cap (contract §8.5); losing an
    abstention costs a ledger some precision, losing a bind loses the answer."""
    if len(rows) <= RULE_EVAL_MAX:
        return rows, 0
    budget = RULE_EVAL_MAX - sum(1 for r in rows if r["verdict"] != VERDICT_ABSTAIN)
    kept = []
    for row in rows:
        if row["verdict"] != VERDICT_ABSTAIN:
            kept.append(row)
        elif budget > 0:
            kept.append(row)
            budget -= 1
    return kept, len(rows) - len(kept)


def binding_record(binding: Binding) -> dict:
    """The §3.2 `binding` record. Carries the COMPILED obligations, not just
    the rule name: rule files are hand-editable and git-backed, so a record
    naming only the rule would force a later reader to open today's file and
    claim it governed a turn three weeks ago."""
    return {
        "kind": "binding",
        "id": binding.id,
        "rule": binding.name,
        "at": binding.at,
        "tier": binding.rule.tier,
        "evidence": binding.evidence,
        "obligations": [dict(o) for o in binding.obligations],
        "satisfiable": binding.satisfiable,
        "unsatisfiable": list(binding.unsatisfiable),
        "seeded": binding.seeded,
    }


def cap_action(args: dict | None) -> dict:
    """The args AS GATED — before any edit, capped per §8.5."""
    shown = {}
    for key, value in (args or {}).items():
        text = value if isinstance(value, str) else repr(value)
        shown[key] = text[:ACTION_ARGS_CHARS]
    return shown
