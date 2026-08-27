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
    ("result_cut", "a result was cut with no way to read the rest"),
    ("continuation_unread", "the rest of a cut result was offered and not read back"),
    ("args_malformed", "the model's arguments did not parse"),
    ("reasoning_truncated", "reasoning was cut by a cap"),
    ("result_stubbed", "a result was stubbed after the model had read it"),
    ("steering", "text was typed while the task ran"),
    ("reminder_demoted", "the per-task reminder reached the model as a user message"),
    ("brief_changed", "what the model was handed changed mid-turn"),
    ("stop_unusual", "the model stopped for an unusual reason"),
    ("context_full", "the prompt nearly filled the context window"),
    ("context_fixed_cost", "most of the context was aish's own fixed overhead"),
    ("context_unattributed", "a trim removed text it did not say which results came from"),
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


def _frame_of(step: dict) -> dict:
    """The evidence frame this tool step recorded, resolved (#289).

    Nothing at all when the step recorded neither a frame nor a reason for
    having none — that is a log written before frames existed, or a tool that
    never had a page, and inventing a key for it would make a reader believe a
    capture was attempted.

    The bytes live in a bounded LRU store, so a reference outliving them is the
    NORMAL end of a frame's life, not a fault. It is reported as `purged`, the
    same word every other deleted-but-referenced blob gets, because "the
    picture is gone" and "no picture was taken" route to different repairs and
    only one of them is worth investigating."""
    path = str(step.get("frame") or "")
    if not path:
        skipped = str(step.get("frame_skipped") or "")
        return {"frame": "", "frame_skipped": skipped} if skipped else {}
    try:
        there = Path(path).is_file()
    except OSError:  # a path this process cannot even stat is not a picture
        there = False
    return {
        "frame": path,
        "frame_state": RECORDED if there else PURGED,
        "frame_skipped": "",
        # What the picture is EVIDENCE OF, rather than merely what it is of:
        # the address the shutter fired at, and the address the page came from
        # when the action moved it. Carried through untouched — this reader may
        # not re-derive a navigation from anything, and the writer is the only
        # side that had both pages in front of it.
        "frame_url": str(step.get("frame_url") or ""),
        "frame_from": str(step.get("frame_from") or ""),
    }


# ------------------------------------------- a failed sign-in, put into words
#
# The four observations behind a failed sign-in reach the log as a token and a
# group of booleans (#325, trace contract §3.4.1). Those are the right things
# to STORE and the wrong things to hand a person, so the wording lives HERE, in
# the assembly, and travels in the document — one source both renderers read,
# rather than a copy per renderer that drifts on the day the wording matters.
#
# **The wording is the risk in this feature, not the plumbing.** A token spoken
# carelessly is the failure the record exists to prevent: `FAILED_CAPTCHA` was
# a token once, and the sentence a renderer gave it told the owner for weeks
# that reCAPTCHA had refused a sign-in that was never submitted (#321). So each
# line below says what aish DID or DID NOT SEE, names aish as the observer, and
# names no cause. `unexplained` in particular is allowed to say plainly that
# aish does not know.
#
# The keys are checked against `signin.FAILURE_VERDICTS` by a test rather than
# by an import: this reader may not import the module that holds the verdict
# TABLE, or it could explain a recorded token by re-running the rule that
# produced it — which is §0's whole prohibition (`test_the_reader_cannot_reach
# _aishs_behaviour`, and `signin` is on its forbidden list for this reason).
SIGNIN_VERDICT_WORDS = {
    "refused": "aish read this as the site refusing the saved password",
    "contradiction": (
        "aish's two observers disagreed about this attempt, and aish did not "
        "resolve it"
    ),
    "never_sent": (
        "aish did not recognise anything carrying the password leaving the page"
    ),
    "unexplained": "aish does not know why this attempt did not get in",
}

# What each observation says, in both polarities. BOTH, deliberately: a group
# whose five observations are all false is a real and positive set of things
# aish looked for and did not see, and printing only the true ones would render
# it identically to an attempt that recorded no observations at all. That
# collapse is the one this reader exists to refuse.
SIGNIN_OBSERVED_WORDS: dict[str, tuple[str, str]] = {
    "credential_seen_leaving": (
        "aish watched the saved password leave the page",
        "aish did not see anything carrying the saved password leave the page "
        "— its matcher has declared blind spots, so this is not proof that "
        "nothing was sent",
    ),
    "refusal_status": (
        "the site answered a request carrying the password with a refusal status",
        "no request carrying the password came back with a refusal status",
    ),
    "page_said_no": (
        "the page showed a message after the submit that was not there before",
        "the page showed no new message after the submit",
    ),
    "body_to_own_origin": (
        "the page sent something to one of the site's own addresses in the "
        "submit window",
        "aish saw the page send nothing to the site's own addresses in the "
        "submit window",
    ),
}

# The declaration is a string rather than a flag, and it is the field most able
# to bring the retired captcha OUTCOME back — so its sentence carries its own
# evidentiary status, in the same words the tool result uses (`browser
# .DECLARED_WIDGET`, which this reader may not import).
SIGNIN_WIDGET_SAID = (
    "the page declares it is protected by {vendor} — a declaration aish "
    "observed on the page, not something aish saw act on this attempt"
)
SIGNIN_NO_WIDGET_SAID = "the page declared no anti-automation widget"

# A token this reader does not know. It is rendered rather than blanked, for
# the reason `frame_skipped` still renders words no writer emits any more:
# retiring an entry retires the WRITER, never the reading of it, and a fact a
# reader cannot render is a fact it has erased.
SIGNIN_UNKNOWN_VERDICT = "a verdict this reader has no words for"

# `ok: false` and no verdict at all. Two meanings the RECORD cannot separate,
# so neither may this: the attempt ended before the failure table ran (a
# second factor, a covered button, a refusal before the submit), or the log
# was written before the verdict was recorded at all. Said once, and only
# where a reader is actually asking why — never over an attempt that worked.
SIGNIN_NO_VERDICT = (
    "no verdict was recorded for this attempt — either it ended before aish "
    "judged it, or this log predates the record"
)


def _observed_said(observed: dict) -> list[str]:
    """The observations, in the order the record writes them, as sentences.

    Unknown keys are carried through as `name: value` rather than dropped. A
    whitelist here is what kept `verdict` and `observed` themselves out of the
    dossier for a day after they started being written, and the same silence
    would swallow the next observation somebody adds."""
    said = []
    for name, value in observed.items():
        if name == "declared_widget":
            vendor = str(value or "")
            said.append(
                SIGNIN_WIDGET_SAID.format(vendor=vendor)
                if vendor else SIGNIN_NO_WIDGET_SAID
            )
        elif name in SIGNIN_OBSERVED_WORDS:
            yes, no = SIGNIN_OBSERVED_WORDS[name]
            said.append(yes if value else no)
        else:
            said.append(f"{name}: {value!r}")
    return said


def _signin_of(step: dict) -> dict:
    """The automatic sign-in that happened inside this call, resolved.

    Its own block and not part of `_frame_of`, because it is about a DIFFERENT
    DOCUMENT: the login page the model never asked for, never saw and cannot
    name. Folding it into the page frame would state a guarantee the capture
    does not make.

    **The verdict and its observations are put into words HERE**, not in the
    renderer, so that `render` and the panel print the same sentence and no
    second author has to get a token's wording right twice (#243's rule, and
    the answer to the two-renderers problem #325 left open). The raw `observed`
    values travel alongside, because this reader reports what was RECORDED and
    a machine reading the dossier wants the booleans, not the prose."""
    block = step.get("signin")
    if not isinstance(block, dict) or not block.get("host"):
        return {}
    path = str(block.get("frame") or "")
    state = ""
    if path:
        try:
            state = RECORDED if Path(path).is_file() else PURGED
        except OSError:  # a path this process cannot stat is not a picture
            state = PURGED
    # Both or neither, exactly as the writer records them: a token with no
    # observations under it is a rendering of the verdict, which is the one
    # thing §4 says an evidence record may not be — so a block carrying only
    # one half is read as no judgement rather than as half a judgement.
    raw = block.get("observed")
    verdict = str(block.get("verdict") or "")
    observed = dict(raw) if verdict and isinstance(raw, dict) and raw else {}
    if not observed:
        verdict = ""
    return {
        "host": str(block.get("host") or ""),
        # Tri-state on purpose: True is a session seen to come up, False one
        # that was not, and None a log written before the outcome was recorded
        # — a reader that collapsed the third into the second would be
        # asserting a failure nothing observed.
        "ok": block.get("ok") if isinstance(block.get("ok"), bool) else None,
        "frame": path,
        "frame_state": state,
        "frame_skipped": "" if path else str(block.get("frame_skipped") or ""),
        "console": [str(line) for line in (block.get("console") or [])],
        "covered": str(block.get("covered") or ""),
        # The token verbatim, its one sentence, the values as recorded, and
        # those values as sentences. Empty throughout when no failure was
        # judged — which is a success, a second factor, an ending before the
        # submit, or a log written before any of this.
        "verdict": verdict,
        "verdict_said": (
            SIGNIN_VERDICT_WORDS.get(verdict, SIGNIN_UNKNOWN_VERDICT)
            if verdict else ""
        ),
        "observed": observed,
        "observed_said": _observed_said(observed),
    }


def _covered_of(step: dict) -> dict:
    """What was found covering the control this call pressed, or `{}`.

    aish's own observation, unlike the console beside it — the driver asked
    what sat at the control's centre point. Reported exactly as recorded and
    never widened: a step with no block says nothing about coverage, which
    includes every step written before #321."""
    block = step.get("covered")
    if not isinstance(block, dict) or not block.get("by"):
        return {}
    return {"by": str(block["by"]), "dismissed": bool(block.get("dismissed"))}


def _page_evidence(step: dict) -> dict:
    """The page's own console, what covered a control, and the sign-in inside
    this call, if any were recorded. Handed on verbatim: this reader reports
    what was RECORDED and never interprets it."""
    out: dict = {}
    if console := [str(line) for line in (step.get("console") or [])]:
        out["console"] = console
    if covered := _covered_of(step):
        out["covered"] = covered
    if signin := _signin_of(step):
        out["signin"] = signin
    return out


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
                # aish's own observations that the action did not do what it
                # looked like it should: the sentence saying it could not be
                # carried out as asked, and an action whose delta against the
                # page last shown came back empty. Facts, never verdicts —
                # they are what the chat renderer keys console surfacing on,
                # and this reader reports them wherever they were recorded.
                "problem": str(step.get("problem") or ""),
                "unchanged": bool(step.get("unchanged")),
                # What this call had to cut, and whether it left a way back.
                # Recorded for every truncator (contract §3.4); `read`
                # is which cached output a paging call actually read, which is
                # what makes "offered" and "used" joinable (#274).
                "truncation": step.get("truncation") or {},
                "read": step.get("continuation") or "",
                # The picture of the page this call read (#289), and whether
                # the bytes it points at are still there. Three states, like
                # every other reference in this file: no key at all means the
                # writer recorded nothing (a log older than the frame, or a
                # tool that has no page), RECORDED means the file resolved, and
                # PURGED means the record outlived the picture — which is the
                # ordinary end for a store that is a bounded LRU cache.
                **_frame_of(step),
                # What the PAGE said while this call was carried out, and what
                # an automatic sign-in inside it left behind. Spread the same
                # way the frame is, so a key exists only where the writer wrote
                # one: a clean page and a page nobody listened to are different
                # facts, and an empty list would say the second about the first.
                **_page_evidence(step),
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
                "problem": "",
                "unchanged": False,
                "truncation": {},
                "read": "",
                "gates": per_call.pop(call, []),
                # A call that never completed never reached a page, so there is
                # nothing to have pictured — and no key, rather than an empty
                # one claiming a capture was considered.
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


# ---------------------------------------------------------------------------
# What filled the context (#262 / #330)
# ---------------------------------------------------------------------------
#
# Per-model-call SPEND has been recorded since #262 and is already on the round
# headers. What was missing is the ATTRIBUTION: which tool result, which
# injected block, which piece of history is responsible for the context a call
# was billed for. That is the instrument #330 needs, because a realistic local
# window is ~60,000 tokens and nothing in aish is designed against that number.
#
# Two units, kept apart on purpose (docs/token-accounting.md):
#
#   chars   — MEASURED. Every message record carries its content, every brief
#             part carries its own char count, and the tool menu's bytes are in
#             the evidence store. Nothing here is modelled.
#   tokens  — REPORTED, by the provider, for the whole call. Never split across
#             the parts below: a per-part token figure is a modelled number in
#             the provider's own unit, inviting a reader to sum the parts and
#             contradict a number the provider actually reported.
#
# So the parts carry chars, the call carries tokens, and the only bridge between
# them is `chars_per_token` — this call's own accounted chars divided by its own
# reported input. Both halves of that ratio are recorded, so it is a
# measurement of THIS call and not a constant applied to it. The corpus it was
# built against spreads from 1.30 to 5.26 (median 3.16), which is exactly why
# no per-part token number is offered.

# How much of the parts list to keep. Ordered biggest-first, so what a cap drops
# is the tail that could not have mattered.
CONTEXT_PARTS_MAX = 14

# When the fixed cost — the standing prompt, the per-task reminder and the tool
# menu — is worth saying out loud on its own. It is paid on EVERY call whatever
# the task, so a turn where it dominates is a turn that spent its window on
# overhead. A fact, not a verdict: the note states the share and names nothing
# as the cause.
FIXED_SHARE_NOTABLE = 0.5

# Where a part of the context came from. `carried` is history from earlier turns
# in this same chat, which is invisible in a per-turn view and is the single
# biggest resident across the owner's corpus.
FROM_SYSTEM = "system"
FROM_TOOLS = "tools"
FROM_CARRIED = "carried"
FROM_TURN = "turn"


def origin_of(record: dict) -> str:
    """Which bucket a message record belongs to. A tool result is named by its
    tool; everything else by its role. Shared with `usage.py` so the two reports
    cannot disagree about what a contributor IS."""
    role = str(record.get("role") or "")
    if role == "tool":
        return str(record.get("tool_name") or "tool")
    return role or "other"


def _menu_chars(step: dict | None, root: os.PathLike | str | None) -> tuple[str, int]:
    """(state, chars) for the tool menu the model was handed.

    The menu is not in the log — the brief records its digest and the bytes live
    once in the evidence store. So this is a lookup of recorded bytes, not a
    re-derivation: the reader never asks what the tool table looks like TODAY,
    only how big the one that was handed over was. A purged blob is `purged`
    and never 0, because a menu nobody can size and a model handed no tools are
    different facts."""
    digest = str(((step or {}).get("tools") or {}).get("digest") or "")
    if not digest:
        return MISSING, 0
    blob = evidence.get(digest, root)
    if blob is None:
        return PURGED, 0
    return RECORDED, len(blob)


def _apply_trim(live: list[dict], step: dict) -> int:
    """Shrink the entries a trim stubbed, and return what it could not attribute.

    Two recorded facts do the work. `stubbed[{at, tool}]` names WHICH results
    were cut, and `bytes_before - bytes_after` is how much text actually went —
    a total the trimmer measured itself. Matching is by tool NAME, oldest-first:
    `stubbed[].at` indexes the live message list, which does not map 1:1 to log
    order across resume, redact or rewind, so claiming per-instance identity
    would be a false precision. That is `usage.py`'s rule, unchanged.

    The recorded total is the AUTHORITY and caps the whole thing. Without that
    cap, `delivered_images` — which replaces a short `[aish: …]` note with a
    shorter constant and leaves a picture behind — read as 200-char stubbing of
    whole user messages, and threw the reconstruction out by 31% on a real log.

    What the record does not name, this returns: a trim written before
    `stubbed[]` existed (#241) says how much went and not what, and that
    remainder must be reported rather than attributed to a guess.
    """
    keep = int(step.get("keep_chars") or 0)
    budget = max(0, int(step.get("bytes_before") or 0) - int(step.get("bytes_after") or 0))
    for stub in step.get("stubbed") or []:
        if budget <= 0:
            break
        name = str(stub.get("tool") or "")
        for entry in live:
            if entry["origin"] == name and entry["chars"] > keep:
                cut = min(entry["chars"] - keep, budget)
                entry["chars"] -= cut
                entry["trimmed"] = True
                budget -= cut
                break
    return budget


def _context_cost(turn: Turn, log: Log, root: os.PathLike | str | None) -> dict:
    """What filled the context of each model call in this turn, and by how much.

    Reconstructed from four recorded things and nothing else: `message` records
    (their text, and the `model_call` stamp saying which call they were first in
    front of), the `brief`'s per-part system char counts and menu digest, the
    `trim` records that ended a result's residency, and the `reasoning` record's
    provider usage.

    **The stamp is what makes this possible, and its absence is a real answer.**
    Without `model_call` on a message, membership in a call's context is
    positional — inferred from the order lines happen to sit in the file — and a
    breakdown built on that would look like attribution while being arithmetic
    on an assumption. A turn without it says so and reports nothing here.

    The check is per TURN and not per file, for the same reason `_rounds` picks
    its grouping per turn: one long chat spans an upgrade, and its early turns
    carry no stamp while its later ones do. A file-level answer would read every
    unstamped message in an early turn as `model_call: 0` — present from the
    first call — and quietly attribute a result to calls that never saw it.

    History from EARLIER TURNS is counted, and needs no stamp: everything
    written before this turn began was in front of every call of it. It is
    resent on every one of them and is the largest resident in the owner's
    corpus, which is why this takes the whole `Log` and not just the turn.
    """
    stamped = any("model_call" in m for m in turn.messages)
    if not stamped:
        return {
            "state": MISSING if not log.wrote("reasoning") else EMPTY,
            "stamped": False,
            "calls": [],
            "peak": None,
            "unattributed_chars": 0,
            "roles": _role_calls(turn),
            "failed": _failed_calls(turn),
        }

    live: list[dict] = []
    brief: dict | None = None
    unattributed = 0
    calls: list[dict] = []
    parts_at: dict[int, list[dict]] = {}

    for past in log.turns[: turn.ordinal]:
        here = past.ordinal == turn.ordinal
        for record in past.records:
            kind = record.get("kind")
            if kind == "message":
                live.append(
                    {
                        "origin": origin_of(record),
                        "chars": len(str(record.get("content") or "")),
                        "ordinal": past.ordinal,
                        "stamp": int(record.get("model_call") or 0),
                        "image_count": len(record.get("images") or []),
                        "trimmed": False,
                    }
                )
                continue
            if kind != "trace":
                continue
            step = record.get("step")
            if not isinstance(step, dict):
                continue
            if step.get("kind") == "brief":
                brief = step
            elif step.get("kind") == "trim":
                unattributed += _apply_trim(live, step)
            elif step.get("kind") == "reasoning" and here:
                number = int(step.get("model_call") or len(calls) + 1)
                snapshot = _snapshot(live, turn.ordinal, number, brief, root)
                parts_at[number] = snapshot.pop("parts")
                snapshot["model_call"] = number
                snapshot.update(_reported(step))
                # The ONE bridge between the two units, and both halves of it
                # are recorded: this call's own measured chars over this call's
                # own reported input. Absent when either half is — never a
                # default divisor, which would make a constant look like a
                # measurement of this call.
                billed = int(snapshot["reported"].get("input") or 0)
                snapshot["chars_per_token"] = (
                    round(snapshot["accounted_chars"] / billed, 2)
                    if billed and not snapshot["unmeasured"] else None
                )
                calls.append(snapshot)

    for index, call in enumerate(calls):
        before = calls[index - 1]["accounted_chars"] if index else 0
        call["added_chars"] = call["accounted_chars"] - before
        call["added_by"] = _added_by(parts_at, calls, index)

    peak = max(calls, key=lambda c: (c["reported"].get("input") or 0), default=None)
    if peak is None:
        peak_out = None
    else:
        parts = sorted(parts_at[peak["model_call"]], key=lambda p: -p["chars"])
        total = sum(p["chars"] for p in parts)
        for part in parts:
            part["share"] = round(part["chars"] / total, 4) if total else 0.0
        fixed = sum(p["chars"] for p in parts if p["where"] in (FROM_SYSTEM, FROM_TOOLS))
        peak_out = {
            "model_call": peak["model_call"],
            "reported": peak["reported"],
            "reported_state": peak["reported_state"],
            "accounted_chars": peak["accounted_chars"],
            "chars_per_token": peak["chars_per_token"],
            "image_count": peak["image_count"],
            "unmeasured": peak["unmeasured"],
            "fixed_share": round(fixed / total, 4) if total else 0.0,
            "parts": parts[:CONTEXT_PARTS_MAX],
            "parts_dropped": max(0, len(parts) - CONTEXT_PARTS_MAX),
        }
    return {
        "state": RECORDED if calls else EMPTY,
        "stamped": True,
        "calls": calls,
        "peak": peak_out,
        # Text a trim removed that no record attributes to an origin. Never
        # folded into a bucket: it is exactly the amount by which the parts
        # below are an upper bound, and hiding it inside one of them would make
        # a guess look like a measurement.
        "unattributed_chars": unattributed,
        "roles": _role_calls(turn),
        "failed": _failed_calls(turn),
    }


def _snapshot(
    live: list[dict],
    ordinal: int,
    number: int,
    brief: dict | None,
    root: os.PathLike | str | None,
) -> dict:
    """The context as it stood for one model call: every part, measured.

    A message is in front of call N when it was appended before call N started —
    which is what the stamp says, since it records the call that was in flight
    when the message was written. Everything from an earlier turn is in front of
    every call of this one.
    """
    buckets: dict[tuple[str, str], dict] = {}
    images = 0
    for entry in live:
        if entry["ordinal"] < ordinal:
            where = FROM_CARRIED
        elif entry["ordinal"] == ordinal and entry["stamp"] < number:
            where = FROM_TURN
        else:
            continue
        key = (where, entry["origin"])
        part = buckets.setdefault(
            key,
            {"origin": entry["origin"], "where": where, "chars": 0, "items": 0,
             "trimmed": 0, "state": RECORDED},
        )
        part["chars"] += entry["chars"]
        part["items"] += 1
        part["trimmed"] += 1 if entry["trimmed"] else 0
        images += entry["image_count"]

    parts = list(buckets.values())
    unmeasured: list[str] = []
    system = (brief or {}).get("system")
    if system is None:
        # No brief in force: what the model was TOLD was never recorded, so its
        # size is unknown. Not zero — a turn with no system text has never
        # happened, and reporting 0 would put a confident falsehood at the top
        # of the breakdown.
        parts.append({"origin": "system text", "where": FROM_SYSTEM, "chars": 0,
                      "items": 0, "trimmed": 0, "state": MISSING})
        unmeasured.append("the system text")
    else:
        parts.append(
            {
                "origin": "system text",
                "where": FROM_SYSTEM,
                "chars": sum(int(p.get("chars") or 0) for p in system),
                "items": len(system),
                "trimmed": 0,
                "state": RECORDED,
            }
        )
    menu_state, menu_chars = _menu_chars(brief, root)
    parts.append(
        {
            "origin": "tool menu",
            "where": FROM_TOOLS,
            "chars": menu_chars,
            "items": int(((brief or {}).get("tools") or {}).get("count") or 0),
            "trimmed": 0,
            "state": menu_state,
        }
    )
    if menu_state != RECORDED:
        unmeasured.append(
            "the tool menu (its bytes are purged)" if menu_state == PURGED
            else "the tool menu"
        )
    if images:
        # Char-invisible and token-huge: an image occupies the window and
        # contributes nothing this reader can measure. It gets a row so an
        # unaccounted call has a visible reason rather than a silent gap.
        parts.append({"origin": f"{images} image(s)", "where": FROM_TURN, "chars": 0,
                      "items": images, "trimmed": 0, "state": UNREADABLE})
        unmeasured.append(f"{images} image(s), which carry no characters at all")
    return {
        "parts": parts,
        "accounted_chars": sum(p["chars"] for p in parts),
        "image_count": images,
        "unmeasured": unmeasured,
    }


def _reported(step: dict) -> dict:
    """The provider's own usage for one call, verbatim, with its semantics label.

    `usage` is authoritative where it exists: `tokens[0]` means three different
    things across the three backends and only the label says which. A call whose
    backend reported nothing gets no numbers at all — claude-max drives its own
    loop and reports no input tokens, and reading that as 0 would be a confident
    lie about a day of real spend.
    """
    detail = step.get("usage") or {}
    tokens = step.get("tokens") or []
    reported: dict = {}
    if detail:
        reported = {k: v for k, v in detail.items() if v not in (None, "")}
    elif tokens:
        reported = {"input": int(tokens[0] or 0),
                    "output": int(tokens[1] or 0) if len(tokens) > 1 else 0}
    return {"reported": reported, "reported_state": RECORDED if reported else MISSING}


def _added_by(parts_at: dict[int, list[dict]], calls: list[dict], index: int) -> list[dict]:
    """Which origins grew between the previous call and this one — the per-STEP
    answer. Measured by differencing two snapshots, so an origin that shrank
    (a trim fired between the two) shows as a negative and is not hidden."""
    number = calls[index]["model_call"]
    before = {}
    if index:
        before = {(p["where"], p["origin"]): p["chars"]
                  for p in parts_at[calls[index - 1]["model_call"]]}
    moved = []
    for part in parts_at[number]:
        delta = part["chars"] - before.get((part["where"], part["origin"]), 0)
        if delta:
            moved.append({"origin": part["origin"], "where": part["where"], "chars": delta})
    return sorted(moved, key=lambda p: -abs(p["chars"]))[:3]


def _role_calls(turn: Turn) -> list[dict]:
    """Model calls an isolated role made inside this turn (#297).

    Reported BESIDE the acting loop's calls and never folded into them: a role's
    spend is real money on the same key, but it is a different context — the
    whole point of the mechanism is that the role's transcript never enters the
    acting one.

    **`answer_chars` is the role's own recorded answer, NOT what the acting
    model was handed.** #330 asks whether a role returns less than it consumes,
    and this reader cannot answer that: the block the answer is RENDERED into is
    what enters the acting context, nothing records its size, and no field joins
    a role record to the tool message that carried it. So the two numbers are
    given the names of what they actually measure and the comparison is left
    unmade. Naming it "what it returned" would have made an unjoined guess read
    as the measurement the issue asked for.
    """
    out = []
    for step in turn.of_kind("role"):
        given = step.get("input") or {}
        out.append(
            {
                "charter": str(step.get("charter") or ""),
                "model": str(step.get("model") or ""),
                "status": str(step.get("status") or ""),
                "reported": {k: v for k, v in (step.get("usage") or {}).items()
                             if v not in (None, "")},
                "input_chars": int(given.get("chars") or 0),
                "answer_chars": len(json.dumps(step.get("output"), ensure_ascii=False))
                if step.get("output") is not None else 0,
                "answer_state": RECORDED if step.get("output") is not None else MISSING,
            }
        )
    return out


def _failed_calls(turn: Turn) -> list[dict]:
    """Model calls that never returned. `sent_chars` is what the agent measured
    itself at the moment it tried, which is the one context size in the log that
    is not a reconstruction of anything."""
    return [
        {
            "model_call": step.get("model_call"),
            "sent_chars": int(step.get("sent_chars") or 0),
            "sent_messages": int(step.get("sent_messages") or 0),
            "action": str(step.get("action") or ""),
            "text": str(step.get("text") or "")[:200],
        }
        for step in turn.of_kind("model_error")
    ]


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
    # Which cached outputs were actually read back, so a cut can be told from a
    # cut that was recovered. Collected across the whole turn first: the paging
    # call comes AFTER the call it continues, and often several calls after.
    paged = {call["read"] for call in doc["did"]["calls"] if call["read"]}
    for call in doc["did"]["calls"]:
        cut = call["truncation"]
        if cut and not cut.get("offered"):
            # The #269 shape: a dead end. Distinguished from the one below
            # because they are different incidents with different repairs —
            # this one is a missing capability, that one is a choice.
            _note(rows, "result_cut",
                  f"{cut.get('omitted', 0)} characters of {call['name']}'s result were cut "
                  f"by {cut.get('truncator') or 'a cap'} and no continuation was offered",
                  section="flow", call=call["call"])
        elif cut and cut.get("continuation") not in paged:
            _note(rows, "continuation_unread",
                  f"{cut.get('omitted', 0)} characters of {call['name']}'s result were cut "
                  f"by {cut.get('truncator') or 'a cap'}; a continuation was offered and "
                  "nothing read it back in this turn",
                  section="flow", call=call["call"])
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
    #
    # The window must be the one that was RECORDED as in force. `num_ctx` is an
    # Ollama option carried on every turn whatever the backend, so comparing
    # against it called a Gemini turn sitting at 5% of its million-token window
    # "nearly full" — a confident wrong claim at the top of the evidence, which
    # is the one thing this pass must never produce. For a log written before
    # the window was recorded, `num_ctx` is trustworthy ONLY on Ollama, where it
    # genuinely is the window; anywhere else the reader cannot know, and
    # abstains rather than guessing.
    window = 0
    for brief in briefs:
        options = brief["options"] or {}
        if recorded := int(options.get("window") or 0):
            window = recorded
        elif options.get("provider") == "ollama":
            window = int(options.get("num_ctx") or 0) or window
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

    # What filled the context, and what that says about a 60,000-token window.
    # Facts only: each row states a measured share and names nothing as a cause.
    #
    # They cite the FLOW, and the round they are about where there is one. The
    # context block is a reading of the model calls, and `flow` is the section
    # both renderers already anchor — a row citing a section no panel draws is
    # a tap that does nothing, which reads exactly like a broken reader.
    context = doc["context_cost"]
    peak = context["peak"]
    if peak and peak["fixed_share"] >= FIXED_SHARE_NOTABLE and not peak["unmeasured"]:
        _note(rows, "context_fixed_cost",
              f"at the fullest call the standing prompt, the task reminder and the tool "
              f"menu were {peak['fixed_share'] * 100:.0f}% of the measured context "
              f"({peak['accounted_chars']:,} chars in all)",
              section="flow", model_call=peak["model_call"])
    if context["unattributed_chars"]:
        _note(rows, "context_unattributed",
              f"{context['unattributed_chars']:,} characters were removed by a trim that "
              "did not record which results they came from, so the breakdown below is an "
              "upper bound",
              section="flow")
    # There is deliberately NO row about whether a role saved context. #330 asks
    # it, and the log cannot answer it: what enters the acting context is the
    # block a role's answer is RENDERED into, nothing records that size, and no
    # field joins a role record to the tool message that carried it. A row built
    # on `answer_chars` would put the answer's own size where the reader would
    # read the rendered block's — the shape of confident-wrong this file exists
    # to prevent. The numbers are reported; the comparison is not made.

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
        # What filled the context of each model call, and what it was billed
        # (#262/#330). Takes the whole log, not just the turn: history from
        # earlier turns is resent on every call of this one.
        "context_cost": _context_cost(turn, log, root),
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


def _by_charter(roles: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for role in roles:
        grouped.setdefault(role["charter"] or "?", []).append(role)
    return grouped


def _human(count: int) -> str:
    for limit, suffix in ((1_000_000, "M"), (1_000, "k")):
        if abs(count) >= limit:
            return f"{count / limit:.1f}{suffix}"
    return str(count)


# What a part's origin says about WHEN it is paid for. The two fixed rows say
# "every call" because that is the whole point of them: they are rent, charged
# again on every step whatever the task does, and a reader who sees them beside
# a tool result without that word reads them as one-off costs.
WHERE_WORDS = {
    FROM_SYSTEM: "every call",
    FROM_TOOLS: "every call",
    FROM_CARRIED: "earlier turns",
    FROM_TURN: "this turn",
}


def _context_cost_lines(context: dict, out: list[str]) -> None:
    """What filled the context of each call, and what the provider billed for it.

    Two columns in two different units, and the header says which is which,
    because the whole risk in this section is a reader adding a measured char
    count to a reported token count. Chars are measured; tokens are reported;
    the shares are of chars and are labelled an estimate of token share.
    """
    lines: list[str] = []
    if not context["stamped"]:
        lines.append(
            f"{DIM}this log does not stamp its messages with the model call they were "
            f"first in front of,{RESET}"
        )
        lines.append(
            f"{DIM}so WHAT filled each call's context is not recorded — the totals on "
            f"each round still stand{RESET}"
        )
    for call in context["calls"]:
        reported = call["reported"]
        if call["reported_state"] == MISSING:
            billed = f"{DIM}{NOT_RECORDED}{RESET}"
        else:
            billed = (
                f"{_human(int(reported.get('input') or 0)):>7} in"
                f" {_human(int(reported.get('output') or 0)):>6} out"
            )
            if cached := int(reported.get("cached") or reported.get("cache_read") or 0):
                billed += f" {DIM}({_human(cached)} cached){RESET}"
        moved = ", ".join(
            f"{p['origin']} {'+' if p['chars'] > 0 else ''}{_human(p['chars'])}"
            for p in call["added_by"]
        )
        lines.append(
            f"call {call['model_call']:<3} {billed}"
            f" · {_human(call['accounted_chars']):>7} chars accounted"
            + (f" · {moved}" if moved else "")
        )
    peak = context["peak"]
    if peak:
        head = f"{BOLD}at call {peak['model_call']}, the fullest{RESET}"
        if peak["reported_state"] != MISSING:
            head += f" — the provider billed {int(peak['reported'].get('input') or 0):,} " \
                    f"input tokens for it"
        lines.append("")
        lines.append(head)
        for part in peak["parts"]:
            if part["state"] != RECORDED:
                size = _state_note(part["state"], "")
                share = ""
            else:
                size = f"{part['chars']:,} chars"
                share = f"{part['share'] * 100:5.1f}%"
            count = f"{part['items']:>3}" if part["items"] else "  —"
            lines.append(
                f"  {part['origin'][:24]:<24} {DIM}{WHERE_WORDS[part['where']]:<13}{RESET}"
                f" {count} {size:>16} {share:>6}"
            )
        if peak["parts_dropped"]:
            lines.append(f"  {DIM}… {peak['parts_dropped']} smaller contributor(s){RESET}")
        # The bridge between the two units, said once and only where both halves
        # of it were recorded. It is this call's own ratio, not a constant.
        if peak["chars_per_token"]:
            lines.append(
                f"{DIM}  shares are of MEASURED CHARS and are an ESTIMATE of token share: "
                f"this call ran at{RESET}"
            )
            lines.append(
                f"{DIM}  {peak['chars_per_token']} chars per reported token, and that ratio "
                f"moves with the content.{RESET}"
            )
        for missing in peak["unmeasured"]:
            lines.append(f"  {BOLD}not measured here:{RESET} {missing}")
    if context["unattributed_chars"]:
        lines.append(
            f"  {BOLD}{context['unattributed_chars']:,} chars{RESET} were removed by a trim "
            f"that did not record which results they came from"
        )
    if context["roles"]:
        # Collapsed per charter. A role's fire rate and its trade are properties
        # of the CHARTER, and seven identical rows each repeating the same
        # caveat is the wall of evidence a summary exists to replace.
        lines.append("")
        lines.append(
            f"{BOLD}isolated roles{RESET} {DIM}— spent beside this turn's context, never "
            f"inside it{RESET}"
        )
        for charter, group in sorted(_by_charter(context["roles"]).items()):
            spend = [r["reported"] for r in group if r["reported"]]
            billed = (
                f"{_human(sum(int(s.get('input') or 0) for s in spend))} in + "
                f"{_human(sum(int(s.get('output') or 0) for s in spend))} out"
                if spend else NOT_RECORDED
            )
            given = sum(r["input_chars"] for r in group)
            answered = [r for r in group if r["answer_state"] == RECORDED]
            # "read N chars, answered in M" and NOT "returned M": what reaches
            # the acting context is the block the answer is rendered into, and
            # nothing records its size.
            traded = (
                f"read {given:,} chars, answered in "
                f"{sum(r['answer_chars'] for r in answered):,}"
                if len(answered) == len(group)
                else f"read {given:,} chars, answer {NOT_RECORDED}"
            )
            lines.append(f"  {charter:<24} {len(group):>3} call(s) · {billed} · {traded}")
        lines.append(
            f"{DIM}  what the acting model was handed is the block each answer was rendered "
            f"into,{RESET}"
        )
        lines.append(
            f"{DIM}  and no record carries its size — so whether a role SAVED context is not "
            f"said here.{RESET}"
        )
    for failed in context["failed"]:
        lines.append(
            f"  {BOLD}call {failed['model_call']} did not return{RESET} — "
            f"{failed['sent_chars']:,} chars in {failed['sent_messages']} messages had been "
            f"sent ({failed['action'] or 'no action recorded'})"
        )
    if lines:
        _block(out, "what it cost", lines)


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


def _where_the_picture_was_taken(call: dict) -> str:
    """The caption for a page frame: the address, and the move that reached it.

    Says nothing at all when the writer recorded no address — a frame from a
    log written before this is still a frame, and inventing "unknown" for it
    would be the reader claiming the writer tried and failed."""
    at = str(call.get("frame_url") or "")
    if not at:
        return ""
    came_from = str(call.get("frame_from") or "")
    if came_from:
        # Worded to exactly what was recorded: `frame_from` is the page the
        # chat was LAST SHOWN, which is what the delta is a delta of. "It
        # navigated here from X" would claim no other document sat between the
        # two, and nothing checks that.
        return f"taken at {at} — aish was on {came_from} before this"
    return f"taken at {at} — the address did not change"


def _console_lines(console: list, whose: str, indent: str = "     ") -> list[str]:
    """The console, marked as the PAGE's own words wherever it is printed.

    A dossier is read by a person and can be pasted to a model, so the same
    discipline applies here as in the tool result: these lines are outside
    content and must never read as aish's account of itself."""
    if not console:
        return []
    lines = [f"{indent}{DIM}{whose} wrote to its own console (the page's words):{RESET}"]
    lines.extend(f"{indent}  {DIM}{str(line)[:200]}{RESET}" for line in console)
    return lines


def _signin_lines(signin: dict) -> list[str]:
    """The automatic sign-in that happened inside this call.

    On a different page from the one the call reported, so it is labelled as
    one. The three reference states are the frame's own (§3.4): a path that
    resolves, a path whose bytes the bounded store has since dropped, and a
    recorded reason there is no picture at all.

    **It says ATTEMPTED, and adds the outcome only where one was recorded.**
    `host` is written whenever a sign-in was tried, success or failure — the
    contract says so — so "aish signed in again", which is what this line used
    to read, was a claim about an outcome no field then carried. `ok` carries
    it now: `SignInResult.ok`, set only where the walled URL was read afresh
    and the session was seen to come up. A block without the key is an older
    log and gets the attempt sentence alone — a dossier assembled from
    recorded evidence may not say more than the evidence does.

    **The verdict and its five observations are printed as recorded** (#325),
    in words the ASSEMBLY chose. This function selects nothing and words
    nothing: the sentence it prints for a token is the sentence any other
    renderer of this document prints, which is what stops the two from
    disagreeing about a vocabulary whose careless wording is the failure the
    record exists to prevent.

    **All five observations print, true and false alike.** A group whose
    observations are all false is a positive set of things aish looked for and
    did not see; printing only the true ones would render it identically to an
    attempt that recorded nothing, and those two route to different repairs.
    An attempt with NO group and no session prints `SIGNIN_NO_VERDICT`, which
    states both meanings that absence can carry and picks neither."""
    host = str(signin.get("host") or "")
    if not host:
        return []
    ok = signin.get("ok")
    outcome = (
        " — the session came up" if ok is True
        else " — the session was not seen to come up" if ok is False
        else ""
    )
    lines = [
        f"     {DIM}aish attempted an automatic sign-in at {host} "
        f"during this call{outcome}{RESET}"
    ]
    if verdict := str(signin.get("verdict") or ""):
        said = str(signin.get("verdict_said") or "")
        lines.append(f"       {DIM}verdict recorded: {BOLD}{verdict}{RESET}"
                     f"{DIM} — {said}{RESET}")
        if observed := list(signin.get("observed_said") or []):
            lines.append(
                f"       {DIM}what aish observed, and what that verdict was "
                f"decided from:{RESET}"
            )
            lines.extend(f"         {DIM}{line}{RESET}" for line in observed)
    elif ok is False:
        lines.append(f"       {DIM}{SIGNIN_NO_VERDICT}{RESET}")
    if path := str(signin.get("frame") or ""):
        gone = signin.get("frame_state") != RECORDED
        lines.append(
            f"       {DIM}picture of the sign-in page: {path}"
            + (f" {BOLD}(purged){RESET}{DIM}" if gone else "")
            + RESET
        )
    elif skipped := str(signin.get("frame_skipped") or ""):
        lines.append(f"       {DIM}no picture of the sign-in page — {skipped}{RESET}")
    if by := str(signin.get("covered") or ""):
        lines.append(f"       {DIM}{_covered_line(by, dismissed=False)}{RESET}")
    lines.extend(
        _console_lines(signin.get("console") or [], "the sign-in page", indent="       ")
    )
    return lines


def _covered_line(by: str, *, dismissed: bool) -> str:
    """One sentence naming what was on top of the control (#321).

    Worded to exactly what was recorded, which is narrower than it looks: what
    is known is that a CLICK could not land, and nothing here knows whether a
    rung below it then got the press through. The step's own status and its
    notice say that; a caption implying the action failed would be a claim this
    field cannot make."""
    said = f"a click could not land — the page had {by!r} on top of the control"
    return said + (" — aish dismissed it and clicked again" if dismissed else "")


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
    if cut := call["truncation"]:
        rest = (
            f'read_tool_output(continuation="{cut.get("continuation")}")'
            if cut.get("offered")
            else f"{BOLD}no continuation offered{RESET}"
        )
        lines.append(
            f"     {BOLD}… {cut.get('omitted', 0)} result characters cut{RESET} by "
            f"{cut.get('truncator') or 'a cap'} "
            f"{DIM}({cut.get('cap_source') or 'cap unknown'}){RESET} — {rest}"
        )
    if call["read"]:
        lines.append(f"     {DIM}read back from cache: {call['read']}{RESET}")
    if call.get("frame"):
        gone = call.get("frame_state") != RECORDED
        lines.append(
            f"     {DIM}picture of the page: {call['frame']}"
            + (f" {BOLD}(purged){RESET}{DIM}" if gone else "")
            + RESET
        )
        # What the picture is evidence OF. Said on the line under it because
        # the path alone answers "what did this page look like", and the
        # question actually asked of a browse step is "what did this press do".
        if where := _where_the_picture_was_taken(call):
            lines.append(f"     {DIM}{where}{RESET}")
    elif call.get("frame_skipped"):
        lines.append(
            f"     {DIM}no picture of the page — {call['frame_skipped']}{RESET}"
        )
    if cover := call.get("covered"):
        # Before the console, because a press that never landed writes nothing
        # to a console — nothing ran — so this is the line that explains the
        # silence beneath it rather than another entry in it.
        lines.append(
            f"     {DIM}{_covered_line(cover['by'], dismissed=cover['dismissed'])}"
            f"{RESET}"
        )
    if problem := str(call.get("problem") or ""):
        # aish's own sentence about its own act, reported verbatim. It is why
        # the chat renderer surfaced this call's console; the dossier prints
        # both unconditionally, because it is opened on purpose.
        lines.append(f"     {DIM}problem: {problem[:200]}{RESET}")
    if call.get("unchanged"):
        lines.append(
            f"     {DIM}nothing on the page changed when aish did this{RESET}"
        )
    lines.extend(_console_lines(call.get("console") or [], "the page"))
    if signin := call.get("signin"):
        lines.extend(_signin_lines(signin))
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
    _context_cost_lines(doc["context_cost"], out)
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
