# The trace contract

**Status:** specification, partially built. **#193's CONTRACT half (2026-08-02) implemented the `required` evidence on §3.4 and the §5 "tool contract with no declared required fields" row**: `returns:` is now a required manifest field, so `verdict_by: "required_fields"` is reachable and `required.declared` is real rather than always empty. The `tool_check` record (§3.6) and the birth run that produces it are still unbuilt — a mutating tool cannot be smoke-run without mutating, and that decision is deferred. **#192 (2026-07-31) implemented §1.2, §1.3, §2, §3.4, §3.5, and the §3.7 near-duplicate half**, closing §6.7 and §6.13. Two `verdict_by` values were added during that build and are marked inline in §3.4. **#191's Verify slice (2026-08-02) added the `gate{at:"verify"}` rows §7 asks for, and `advised` acquired a second meaning there: an answer DELIVERED with a not-followed note. Calling that `stopped` had the ledger counting shipped answers as terminations.** **#191 (2026-07-31) implemented §3.1, §3.2 and the rule half of §3.3** — the `rule.*` gate ids only; the twelve hardcoded gates in §6 still log what they logged before, since extracting them is deferred (see `docs/rules-engine.md`). Its verdicts are Tier 0 throughout, so §7's counters are not yet due — the records are shaped so the scan is a pure function over the log when the first scored trigger arrives. Everything else is still unbuilt.
**Scope:** what #191, #192, #193, #194 and #196 must write to the session log so that #197 can answer *"what governed this turn, what fired, what didn't, and why?"* from the log alone.
**Gate:** phase 1 of #190's build order. A binding that ships without logging its evidence, or a gate that ships without logging which tier decided, makes that question unanswerable forever for everything built before someone notices. Retrofitting it is how #183's calibration problem ended up costing a 481-call audit.

Vocabulary is #190's ("Shared vocabulary" section) — *invariant, structural/scored/judged, judged trigger, injection surface, calibration debt, binding, isolation, prose explains / gate enforces*. No synonyms are introduced here.

---

## 0 · The one rule this document exists to enforce

> **An explanation is assembled from recorded evidence, never re-derived from source.**

Session B is the anti-pattern: asked why its output was truncated, the model grepped aish's own source, found one of three truncators, and confidently reported the wrong cause. It could not have done better — the log recorded the *outcome* (a short result) and nothing about the *decision* (which truncator, what cap, from where). Every shape below is chosen so that the corresponding question is a lookup, not a search.

Three corollaries used throughout:

1. **A record must be self-contained against later edits.** Rule files, tool manifests, thresholds and prompts all change. A record naming only `rule: youtube-url-analysis` forces the reader to open the file *as it reads today* — which is re-derivation with extra steps. Bindings therefore carry their compiled obligations; scored verdicts carry the floor that was in force.
2. **Absence must never be the evidence.** #190's key falsification ("no knowledge injection on that turn at all") had to be established by the *absence* of a record. That is a proof only for someone who already knows the emitting code. Every evaluation that happens must leave a record saying it happened, including when it decided nothing.
3. **The evidence channel is what the harness declared, never the acting model's summary.** This is #178's provenance discipline and #191's evidence channel, applied to logging: a `judged` verdict records the fields the harness gathered and handed to the judge, plus the judge's closed-vocabulary answer. It never records the acting model's prose account of why it did something.

---

## 1 · The carrier: existing mechanism, no parallel one

Everything specified here rides the mechanism that already exists.

| concern | mechanism today | change |
|---|---|---|
| durable write | `SessionLog._record("trace", step=…)` via the agent's `step_log` sink | none |
| serialisation | `SessionLog._write_lock`, held inside `_record` | none — new writers use `logref.step(...)`, never a raw handle |
| live render | `Agent._sink_step` → `on_step` | **new**: a log-only emit path (see 1.2) |
| cold replay | `SessionLog.reconstruct_events` | **new**: a renderless-kind registry (see 1.3) |

No new file, no new store, no second log. `render_error` (#188) is the precedent for every property below: a `kind:"trace"` record that is durable evidence, renders nowhere, and is written through `logref.step`.

### 1.1 · Who writes

- **Agent-side** (rule engine, gates, envelope, admission, birth check): through the agent's `step_log` sink, which both entry points already wire to `logref.step`. Records therefore appear in CLI and web sessions identically, and claude-max inherits them because `_locked_dispatch` routes through `inner._dispatch`.
- **Server-side** (anything the browser or an HTTP handler reports): `session.logref.step({...})` directly, as `_render_error` already does.

### 1.2 · Rendered vs log-only

`Agent._sink_step` writes to `step_log` **and** `on_step`. Every kind specified here is **log-only**. Add one method beside `_emit_step`:

```python
def _emit_record(self, **fields) -> None:
    """Durable governance evidence. Log-only: never handed to on_step, so it
    reaches no renderer and cannot open a live trace card."""
    if self.step_log is not None:
        self.step_log(fields)
```

This is not stylistic. `app.js`'s `traceStep` calls `ensureTrace()` **before** dispatching on `step.kind`, so a step kind with no renderer does not degrade to "renders nothing" — it opens an empty live trace card with a running ticker. A new kind reaching the frontend is a visible bug, not a no-op. That mechanical fact is why the registry in 1.3 must exist *before* the first new kind ships, not after.

### 1.3 · Renderless registry

`reconstruct_events` today special-cases exactly one kind:

```python
if sk == "render_error":
    continue
```

Replace with a set, in `session.py`, and skip membership:

```python
RENDERLESS_STEPS = frozenset({
    "render_error",   # #188
    "rule_eval", "binding", "gate",   # #191
    "trim",                            # #192
    "tool_check",                      # #193
    "admission",                       # #194
    "context",                         # #208
})
```

One set, one skip, named at the top of the module — not a growing chain of `if sk == …: continue` that the next author forgets to extend.

**Hot/cold parity holds by construction:** these kinds are emitted only through `_emit_record` (never live) and skipped on replay (never cold). The argument is #171's for `[aish: …]` notes and #188's for `render_error`, unchanged.

**A pre-contract log is byte-identical.** Old logs contain none of these kinds, so every new branch is inert on them; `has_trace` is already set by the `kind == "trace"` arm before the skip, exactly as `render_error` does today, so a log containing *only* governance records still reconstructs rather than falling back to a flat history blob. This must be pinned by a test that replays a fixture log captured before the change and asserts byte-identical event output.

---

## 2 · Turn identity and call identity

#197's unit of analysis is an incident the owner points at, which spans turns. Today, correlating records to a turn is **positional**: `curate._windows` pairs a `knowledge` step with the *next* user message because that is the order `run_task` happens to emit in, and its docstring says so. That heuristic is already fragile (a note landing mid-turn splits it) and it cannot survive records that are emitted mid-turn, at turn end, or from the server thread.

**Specification.** Two counters, stamped on every record defined in this document:

- `turn` — integer, monotonic within a session, incremented once per `run_task` (and once per `_run_task` on the web). It is the join key for "what governed this turn".
- `call` — integer, monotonic within a turn, assigned when a tool call is dispatched. It is the join key between a `gate` verdict and the `tool` step for the action it governed.

`call` requires adding a key to the existing `tool_start` / `tool` steps. That is additive (the frontend ignores unknown keys; old logs simply lack it) and it replaces the by-name matching `SessionLog.pending_task` already apologises for in its own docstring ("Read-only calls run in parallel, so match by name rather than assuming the last start is the one finishing"). Parallel read-only tools make name-matching genuinely ambiguous today.

> **Design fork 1 — how far to stamp `turn`.** Options: (a) new kinds only, leaving `knowledge`/`thinking`/`tool` positional; (b) new kinds plus `tool_start`/`tool`/`knowledge`; (c) every trace record.
> **Recommendation: (b).** It is the smallest set that makes the join complete — a governance answer always needs the tool steps and the knowledge step alongside the gate records — and it leaves the high-volume `thinking` steps untouched. (c) buys nothing #197 asks for. (a) leaves #197 doing the same positional inference `curate._windows` does, which is the thing this document exists to stop.

---

## 3 · Record shapes

Every record below carries `kind`, `turn`, and — where it concerns one action — `call`. `ts` is added by `_write_line`. All free text fields are capped (see §8).

### 3.1 · `rule_eval` — the ruleset evaluated for a turn (#191)

Emitted **once per turn at seed**, before the user message is appended (the same position `knowledge` is emitted from today). One row per rule in the active corpus, whatever the verdict.

```json
{
  "kind": "rule_eval",
  "turn": 7,
  "at": "seed",
  "corpus": {
    "total": 12,
    "active": 10,
    "skipped": [{"rule": "old-hotel-rule", "why": "disabled"},
                {"rule": "wc-2026-tickets", "why": "expired"}]
  },
  "evaluated": [
    {"rule": "youtube-url-analysis", "trigger": "message_shape", "tier": 0,
     "verdict": "bind", "binding": "b1",
     "evidence": {"on": "task", "pattern": "^\\s*https?://(www\\.)?(youtu\\.be|youtube\\.com)/\\S+\\s*$",
                  "matched": true, "span": [0, 43]},
     "ms": 0.2},

    {"rule": "file-issue-dont-investigate", "trigger": "task_domain", "tier": 1,
     "verdict": "abstain",
     "evidence": {"mode": "semantic", "sim": 0.212, "floor": 0.35, "rail": 0,
                  "query_chars": 31, "context_used": false},
     "ms": 11.4},

    {"rule": "no-forget-when-triggered", "trigger": "session_context", "tier": 0,
     "verdict": "abstain",
     "evidence": {"field": "origin", "value": "user", "required": "!= user"},
     "ms": 0.1},

    {"rule": "answer-analyses-the-video", "trigger": "deliverable_shape", "tier": 2,
     "verdict": "unevaluable", "fail": "open",
     "evidence": {"judge": "qwen3:8b", "error": "connection refused"},
     "ms": 902.0}
  ],
  "truncated": 0
}
```

| field | why #197 needs it |
|---|---|
| `corpus.total` / `active` / `skipped[]` | Separates the three answers #197 must distinguish and must never conflate: **no rule existed**, **a rule existed but was retired**, **a rule existed and abstained**. Each routes to a different repair destination in #197's table. `why` is the `entry_active` reason, so a rule silently expiring is legible. |
| `evaluated[].rule` | Join key to the rule file and to the per-rule ledger counters. |
| `trigger` | One of #191's six trigger kinds. #197's second repair shape is "the trigger is wrong or too narrow" — that diagnosis is only possible if the kind is recorded, since amending a `message_shape` trigger and amending a `task_domain` trigger are different proposals. |
| `tier` | #197 explicitly requires "whether it was structural, scored or judged". Also the ledger's admission key: a non-Tier-0 verdict without counters is #183 again. |
| `verdict` | Closed vocabulary: `bind` \| `abstain` \| `unevaluable` \| `error`. Not a sentence. |
| `binding` | Present only on `bind`; the id of the binding record it produced, so the two are joinable without guessing. |
| `evidence` | **The inputs the verdict was a function of** (see §4). Tier-shaped. |
| `fail` | Which failure direction was applied on `unevaluable` — `open` \| `hold`. #191 makes this per-enforcement-point *and* origin-dependent; "why did nothing bind during the embedding outage?" must not require re-deriving that policy from source. |
| `ms` | #191's cost law is a design claim ("two judged calls per bound turn, zero per unbound turn"). Unmeasured, it is unfalsifiable — and the first symptom of it being wrong is a slow assistant with no explanation. |
| `truncated` | Number of abstention rows dropped by the cap (§8). Non-zero means the reader is looking at a partial list and must say so. |

**Invariant:** every rule counted in `corpus.active` appears exactly once in `evaluated`. Absence from the list means the rule was not in the active corpus, which `corpus.skipped` explains. This is corollary 2 made structural.

### 3.2 · `binding` — the runtime object (#191)

One record per binding, at creation. Separate from `rule_eval` because a binding is not only a seed-time object: #191's deferred **tool-outcome** triggers fire mid-turn, where no `rule_eval` row exists to carry them.

```json
{
  "kind": "binding",
  "turn": 7,
  "id": "b1",
  "rule": "youtube-url-analysis",
  "at": "seed",
  "tier": 0,
  "evidence": {"on": "task", "pattern": "…", "matched": true, "span": [0, 43]},
  "obligations": [
    {"verb": "route", "to": "youtube_analyze", "of": "deliverable"},
    {"verb": "prohibit", "what": ["web_search", "read_url"], "unless": "disclosed"},
    {"verb": "disclose", "state": "transcript_empty"}
  ],
  "satisfiable": true,
  "unsatisfiable": [],
  "seeded": true
}
```

| field | why #197 needs it |
|---|---|
| `id` | Every downstream `gate` verdict references it. Names alone are ambiguous: the same rule may bind twice in a turn (seed, then re-bind on a tool outcome), and those two bindings can have different evidence. |
| `obligations` | **The compiled obligations as they were at bind time.** Rule files are hand-editable and git-backed; a record naming only the rule forces #197 to read today's file and claim it governed a turn three weeks ago. This is corollary 1, and it is the difference between an explanation and a plausible story. |
| `satisfiable` / `unsatisfiable[]` | #191's bind-time unsatisfiability check (a `route` naming a tool that no longer exists). Makes "the rule bound but its tool was gone" a recorded fact rather than an inference from a later failure. |
| `seeded` | Whether the *prose explains* half actually reached the model's context. #190 decision: the model must never be ambushed by a gate. If a refusal follows a binding that was never seeded, that is an enforcement-point bug — #197's third repair shape — and it is only detectable if the seeding is recorded separately from the binding. |
| `evidence` | Duplicated from the `rule_eval` row deliberately: a mid-turn binding has no `rule_eval` row, so the record must stand alone. |

### 3.3 · `gate` — a verdict at an enforcement point

The workhorse. One shape for **every** gate in the system, rule-derived or hardcoded, so #197 and the ledger have one reader.

```json
{
  "kind": "gate",
  "turn": 7,
  "call": 3,
  "at": "gate",
  "gate": "rule.prohibit",
  "binding": "b1",
  "rule": "youtube-url-analysis",
  "tool": "web_search",
  "action": {"query": "smoleńsk raport podkomisji"},
  "verdict": "refused",
  "tier": 0,
  "evidence": {"obligation": "prohibit", "matched": "web_search",
               "disclosed": false, "route_used": false},
  "round": 1,
  "max_rounds": 2,
  "escalated": false,
  "message": "NOT EXECUTED — the rule 'youtube-url-analysis' routes this…"
}
```

| field | why #197 needs it |
|---|---|
| `at` | Enforcement point: `seed` \| `gate` \| `verify` \| `approval` \| `loop` \| `post`. #191 defines failure directions **per enforcement point**; without this the reader cannot tell which policy applied. |
| `gate` | Stable identifier of *which mechanism* decided — the inventory in §6 fixes the vocabulary. This is the ledger's primary key and the answer to "which of the twelve things refused me?". |
| `binding` / `rule` | Null for the hardcoded gates. Present, and joinable, for rule-derived ones. |
| `tool` / `action` | What was gated. `action` is the args **as gated** (after alias expansion, before any edit) — capped, and never the post-edit command, which is already on the `tool` step. |
| `verdict` | Closed vocabulary: `allowed` \| `refused` \| `held` \| `blocked` \| `advised` \| `stopped`. `advised` exists for the non-blocking `prefer_over` nudge, which today produces no record at all. `stopped` is the loop/stall/ceiling terminator. |
| `tier` | Same requirement as §3.1. A gate is Tier 0 by construction under #191's cost law; recording it makes a future violation of that law visible instead of merely slow. |
| `evidence` | §4. |
| `round` / `max_rounds` | **Bounded refusals are a mandatory #191 mechanic.** "Why did the model give up?" and "why did this escalate to me?" are both unanswerable without the round number — and a gate that lifted because its counter ran out is a *completely different* event from one that lifted because the model complied. The skill gate has this bug today (§6.2). |
| `escalated` | Whether it reached Tier 3 (human). Distinguishes "refused and the model complied" from "refused and the owner had to decide". |
| `message` | The exact refusal text handed to the model, capped. #190 decision 2 says every refusal must be instructive; whether it *was* is checkable only if it is recorded. "The refusal was instructive and the model ignored it anyway" is a distinct incident class from "the refusal said nothing useful", and they route to different repairs. |

**When a gate logs.** Not every gate on every call — twelve records per tool call is noise that will get switched off. The rule:

> **A gate logs when it is ARMED.** Armed means its precondition state is non-empty: the stop gate while `_pending_comment_response` is set; the skill gate while `_pending_skill_reads` is non-empty; the egress gate in a non-`user` origin with an egress tool; a rule gate while a binding is active; approval always (it always runs).

Consequence: the *absence* of a `gate` record means the gate was disarmed. For that to be a usable answer, the disarmed condition must be recoverable from session-level facts already in the log — `origin` (a `kind:"origin"` record), `roots` (`kind:"cwd"` / `kind:"trust_dir"`), the active rule corpus (`rule_eval.corpus`). Where it is not recoverable, the gate must log `verdict:"allowed"` with the reason. §6 names the one place where this bites today: auto-approval, which logs `"auto"` and no reason at all.

### 3.4 · Tool result envelope — **extend `tool`, do not add a kind** (#192)

The `tool` step already carries `name`, `secs`, `ok`, `summary`, `error`, and (for `run_command` and writes) `decision` / `command` / `output`. #192 replaces the prefix sniff that computes `ok`; it does not need a new record.

```json
{
  "kind": "tool", "turn": 7, "call": 3,
  "name": "youtube_analyze", "secs": 4.2,
  "summary": "url=https://youtu.be/…",

  "ok": false,
  "status": "incomplete",
  "verdict_by": "required_fields",
  "exit_code": 0,
  "decision": "approved",
  "required": {"declared": ["transcript"], "missing": [], "empty": ["transcript"]},
  "bytes": 575,

  "truncation": {
    "kept": 8000, "omitted": 19365,
    "head": 6000, "tail": 2000,
    "truncator": "tool_plugins",
    "cap_source": "backend:gemini-3.5-flash:1048576",
    "continuation": "sha256:ab12…", "offered": true
  }
}
```

| field | why #197 needs it |
|---|---|
| `status` | `ok` \| `incomplete` \| `failed` — the honest verdict. `ok` is **kept** as `status == "ok"` so the frontend needs no change and old logs are unaffected. |
| `verdict_by` | Which deterministic rule produced the status: `exit_code` \| `required_fields` \| `empty_output` \| `error_field` \| `gate` \| `exception` \| `prefix`. Without it, "why is this red?" is a guess. **`error_field` and `prefix` were added when #192 was built** (2026-07-31), each for a reason the spec had not foreseen. `error_field`: the canonical `youtube_analyze` shape is a 575-char JSON payload with `transcript: ""` beside a populated `error_log` and exit 0 — `empty_output` cannot see it (the payload is not empty, only the field that mattered was) and `required_fields` needs #193's contract, which does not exist yet, so without a third floor the phase would not have fixed its own motivating case. It reads a NAMED channel the wrapper author chose (`error`/`error_log`/`errors`), never prose. `prefix`: the legacy `startswith()` sniff still decides for native tools not yet enveloped, and it is recorded EXPLICITLY rather than left absent — absence must never be the evidence (corollary 2), and counting these rows is the honest measure of how much of the tool surface remains un-enveloped. |
| `decision` | `approved` \| `denied` \| `held` \| `blocked` \| `auto` \| `rejected`. **Was set only for `run_command` and writes.** A held or denied *plugin tool* logged no decision and `ok: true` — see §6.13. Any audit of these logs, the #185 ledger included, counted a held mutation as a completed one. **Built in #192 as ONE rule rather than a fix per site:** a `decision` in `{denied, held, blocked, rejected}` means the action did not happen, so `_emit_tool_step` forces `ok: false` / `status: "failed"` / `verdict_by: "gate"` last, whichever path set it. That was necessary because the defect is wider than §6.13's table — `run_command` sets `decision` in `_run_meta` but no `ok`, and `DENIED_RESULT` ("USER DENIED…"), `HELD_FOR_ADJUSTMENT` ("NOT RUN…") and `BLOCKED_RESULT` ("BLOCKED…") sniff as SUCCESS, so a denied *shell command* logged green too. Deriving both fields from one source also keeps them coherent: `ok` is defined as `status == "ok"`, and the envelope and `_run_meta` could previously disagree. |
| `required.declared` / `missing` / `empty` | The evidence behind `incomplete`. The declared list comes from #193's tool contract, which is a file that will be edited — so it is snapshotted here rather than read back later (corollary 1). **Built 2026-08-02 as the manifest's `returns:` field** (`docs/tools-layer.md`), required on the same fail-closed terms as `mutating`. `declared: []` means the tool declared `returns: text` — a real contract whose only term is non-empty output; the key's ABSENCE means `returns: none`, the recorded opt-out. Declared fields with a payload that is not a JSON object is `incomplete` with `payload: "not_json"`, or a wrapper could shed its contract by no longer printing JSON. |
| `bytes` | Real payload size before truncation. Makes truncation ratio measurable across the corpus, which is what turns "truncation manufactures improvisation" from a hypothesis into a counted signal. |
| `truncation.truncator` | `tools` \| `tool_plugins` \| `history_trim`. **This single field is what Session B got wrong.** Three truncators with three markers is unguessable; naming the one that cut is the entire diagnosis. |
| `truncation.cap_source` | Where the cap came from: `constant:_OUT_HEAD+_OUT_TAIL` \| `num_ctx:8192` \| `backend:<spec>:<window>`. #192's job is to make caps track the real backend; a log recording the cap but not its provenance cannot show whether that landed. |
| `truncation.offered` | Whether a continuation was actually handed to the model. "Improvised after a dead end" and "improvised despite being offered page 2" are different incident classes with different repairs. |
| `truncation.continuation` | The content-addressed cache key. Makes a later paging call joinable to the call it continues. |

A continuation fetch is an ordinary `tool` step with `"page": 2, "from": "cache"` — so "the wrapper re-ran for page 2" (the thing #192 forbids) is visible rather than assumed.

### 3.5 · `trim` — the third truncator, currently silent (#192)

`Agent._trim_tool_message` destroys every prior tool output down to 200-char stubs at the start of every task, unconditionally, and records **nothing**. It is the truncator with the largest blast radius and zero evidence. It cannot ride the `tool` step (it edits history, not a call), so it gets its own renderless kind:

```json
{"kind": "trim", "turn": 8, "policy": "eager_stub",
 "affected": 6, "bytes_before": 31402, "bytes_after": 1200,
 "keep_chars": 200, "budget": null,
 "cap_source": "constant:TRIM_KEEP_CHARS",
 "oldest_first": false}
```

`policy` is `eager_stub` \| `budget_oldest_first` (the `keep_history=True` resume path). `budget` is the char budget when one applied, `null` when the trim was unconditional — which is exactly the fact #192 says is wrong and which no current record states.

### 3.6 · `tool_check` — birth check (#193)

```json
{"kind": "tool_check", "turn": 12, "tool": "youtube_analyze",
 "phase": "birth", "attempt": 1,
 "check": {"args": {"url": "https://youtu.be/…"}, "source": "model_supplied"},
 "path": "dispatch",
 "status": "failed", "exit_code": 1, "verdict_by": "exit_code",
 "required": {"declared": ["transcript"], "missing": ["transcript"]},
 "stderr": "ModuleNotFoundError: No module named 'youtube_transcript_api'",
 "offered": false}
```

| field | why #197 needs it |
|---|---|
| `phase` | `birth` \| `recheck` \| `manual` (`aish tool check`). #190's accepted residual risk is that a birth check is a *one-time* verdict — `youtube_analyze` passed at 11:17 and failed at 11:22. "Passed at birth, failing at use" must read as exactly that, not as a contradiction. |
| `path` | `dispatch` \| `none`. The entire point of #193 is that the check runs through the **real** dispatch path — the mismatch between "worked in my shell" and "works under dispatch" is the bug class that shipped. If that claim is unrecorded, a future regression routing it through a shell is undetectable. |
| `attempt` | Correct-and-retry rounds. Makes the #190 audit's churn signal (five `create_tool` attempts for `gh_issue_create`) countable instead of a hand audit. |
| `offered` | Whether the tool was exposed after this verdict. #193's contract is "fail → not offered"; a green log with `offered: true` after a failure is a gate bug. |
| `check.source` | `model_supplied` \| `stored`. A stored check is a regression test; a model-supplied one is a fresh claim. |

### 3.7 · `admission` — memory admission control (#194) and the near-duplicate gate

One kind covers both, because both are "a write to the knowledge corpus was classified and possibly redirected".

```json
{"kind": "admission", "turn": 9, "target": "memory",
 "name": "youtube-analyze-transcript-failure-policy",
 "verdict": "redirected",
 "classification": "behaviour",
 "tier": 0,
 "evidence": {"rail": "phrasing", "matched": ["if … fails", "never"],
              "tool_names": ["youtube_analyze"], "sim": null},
 "destination": "rule",
 "message": "NOT saved — this is a behaviour, not a fact. …",
 "chars": 184}
```

```json
{"kind": "admission", "turn": 4, "target": "memory",
 "name": "custom-tool-shebang-validation-rules",
 "verdict": "refused_duplicate",
 "classification": "fact",
 "tier": 1,
 "evidence": {"mode": "semantic", "sim": 0.612, "floor": 0.55,
              "against": "always-use-python-uv"},
 "destination": null,
 "message": "NOT saved — a similar memory already exists — always-use-python-uv: …"}
```

| field | why #197 needs it |
|---|---|
| `verdict` | `admitted` \| `redirected` \| `refused_duplicate` \| `refused_invalid`. |
| `classification` + `evidence` | A scored or judged classifier is right *at a rate*. Recording the inputs is the admission price (#190 decision 8). For the near-duplicate gate specifically, `sim` and `floor` are the fields that would have told us months ago whether `DEDUP_MIN_SIM = 0.55` is calibrated — it is currently documented as "provisional until measured on the live corpus" and nothing measures it. |
| `destination` | `rule` \| `tool` \| `issue` \| `null`. #197's repair loop requires diagnoses to end at a **named destination**; the ledger counts whether redirects were followed. |
| `message` | The redirect text. Same reasoning as `gate.message`: #194's whole claim is that the refusal is instructive. |

> **Design fork 2 — recording whether a redirect was followed.** A `followed` field cannot be set at write time (it is knowable only later, if at all). Options: (a) leave it out and let the ledger correlate a later `binding`-creating rule file or `tool_check` in the same session; (b) emit a second `admission` record with `verdict:"redirect_followed"` when a rule/tool lands within N turns of a redirect.
> **Recommendation: (a).** (b) requires the harness to guess causality, and a wrong guess in a ledger that measures improvement is worse than a missing number. Flagged because it is the one #197 counter that correlation may not deliver cleanly.

### 3.8 · `knowledge` — two changes to an existing record (#183 generalised)

The preflight record is the template #197's abstention requirement generalises from. Two defects.

**(a) It is suppressed when nothing is injected.** `run_task` emits the step only under `if preload.names:`. So "preflight ran and selected nothing" and "preflight never ran" are the same log. This is *exactly* #190's key falsification — established by absence, provable only by someone who knows the emitting code. **The step must always be emitted for a task that reached preflight.**

**(b) It records only what won.** #197's primary question is "why didn't this fire?".

```json
{"kind": "knowledge", "turn": 3, "mode": "semantic",
 "items": [{"label": "trippy-hotel-search", "kind": "skill", "sim": 0.512, "rail": 3}],
 "floors": {"inject": 0.35, "rail": 0.24, "top": 4, "budget_chars": 3200},
 "corpus": {"active": 67, "pinned": 9, "scored": 58},
 "query": {"chars": 31, "context_used": true, "context_chars": 400},
 "considered": [
   {"label": "youtube-analyze-transcript-failure-policy", "kind": "memory",
    "sim": 0.191, "rail": 0, "why": "below_floor"},
   {"label": "long-playbook", "kind": "skill", "sim": 0.402, "rail": 0,
    "why": "budget_exhausted"}
 ],
 "truncated": 3}
```

| field | why #197 needs it |
|---|---|
| `floors` | **The thresholds in force at the time.** #183's calibration failure was partly invisible because the floor lives as a constant in source: a log recording the outcome but not the threshold cannot be re-examined after someone moves the constant. This is the sharpest instance of "evidence, not conclusions" in the whole document. |
| `corpus.pinned` | Pinned standing rules never compete for preflight slots (#183). Recording that they were *excluded by policy* closes "why wasn't my standing rule injected?" with an answer instead of silence. |
| `query.context_used` | Whether prior user turns joined the embedding query (the short-task branch). It changes the meaning of every `sim` in the record. |
| `considered[]` | The abstentions: entries that were scored and not injected, with the reason. `why` is a closed vocabulary: `below_floor` \| `budget_exhausted` \| `oversized` \| `pinned_excluded` \| `outranked`. |
| `truncated` | Considered rows dropped by the cap. |

`items[]` keeps its current shape exactly (`label`, `kind`, `sim`/`rail` or `score`) — `curate.scan_ledger` reads it and must not break.

### 3.9 · `incident` — the #197 unit itself

Written when the owner points at a failure and aish produces an explanation. It lives in the session where the complaint was made and *references* the session under analysis.

```json
{"kind": "incident", "id": "inc-20260730-1",
 "pointed_at": {"session": "session-20260730-112152-153277.jsonl", "turn": 4},
 "class": "rule_did_not_bind",
 "confidence": "recorded",
 "evidence_refs": [
   {"session": "session-20260730-112152-153277.jsonl", "turn": 4, "kind": "rule_eval"},
   {"session": "session-20260730-112152-153277.jsonl", "turn": 4, "call": 1, "kind": "tool"}
 ],
 "gaps": ["no rule_eval record — session predates the rules engine"],
 "repair": {"destination": "rule", "target": "youtube-url-analysis",
            "action": "amend_trigger", "applied": false}}
```

| field | why |
|---|---|
| `class` | Closed vocabulary — the recurrence metric is meaningless if the classes drift. Proposed set: `no_rule_existed`, `rule_did_not_bind`, `rule_bound_but_ignored`, `gate_did_not_refuse`, `tool_failed_silently`, `truncation_hid_evidence`, `knowledge_not_retrieved`, `knowledge_ignored`, `memory_used_as_repair`, `unverified_deliverable`. |
| `confidence` | `recorded` \| `partial` \| `inferred`. **Load-bearing.** It is the boundary between an explanation assembled from evidence and the Session-B guess with better framing. An `inferred` incident is a legitimate output — but it must be labelled, and it must not be counted in the improvement metric with the same weight. |
| `evidence_refs[]` | Resolvable pointers. This is why §2's `turn`/`call` ids exist. |
| `gaps[]` | Where the log could not answer. #197: *"Where the log cannot answer, it must say so rather than infer."* A record with an empty `gaps` list and `confidence: recorded` is a strong claim; the field forces it to be made explicitly. |
| `repair` | The named destination. `applied` is set when the mutation lands (a rule file written, a `tool_check` passed). |

### 3.10 · `context` — what the model was TOLD (#208)

**BUILT (2026-08-02).** Emitted at seed, next to `rule_eval`, on every task that reached index composition. Renderless.

The other nine records answer what the model **did**, what **governed** it, and what it **stored**. None of them answers what it was **given**, and the thing it is given is not small: `knowledge_index` pastes up to `INDEX_SKILLS_MAX` skill lines, `INDEX_PINNED_MAX` standing rules and `INDEX_MEMORY_MAX` memory descriptions into `messages[0]` before the first token, on every task, unsolicited.

The incident that named it: a session answered a question about a sick child with the owner's holiday street address. The log showed `recall("Kuba")` returning a family profile, then a `google_maps` call whose query *already contained the address*, with nothing in between. The address had arrived in the index, via one memory's `description` field. The owner read their own log and could not find it, correctly, because it was not there.

```json
{"kind": "context", "turn": 1,
 "index": {
   "items": [{"label": "user-staying-villa-victoriya-bali", "kind": "memory", "slot": "memory"}],
   "caps": {"skills": 30, "pinned": 20, "memory": 15},
   "corpus": {"skills": 31, "pinned": 4, "memory": 74, "lessons": 0},
   "omitted": {"skills": 1, "pinned": 0, "memory": 59},
   "chars": 3117},
 "preload": {"mode": "semantic", "count": 0, "names": []}}
```

| field | why |
|---|---|
| `index.items[]` | `label` + `kind` + `slot` (`skill` \| `pinned` \| `memory`). **Names only, never descriptions.** The name identifies the entry, which is the question being asked; the description would be an unbounded second copy of the prompt in the log — and for memories saved through `remember` the description often *is* the whole fact, which is the one text an audit record must not duplicate. |
| `index.caps` | The thresholds in force at the time, for §3.8's `floors` reason: they live as constants in source, so a log recording only the outcome cannot be re-read after someone moves one. |
| `index.corpus` / `index.omitted` | The abstention half (§5). "Why did it know that?" and "why did my entry never reach the prompt?" are one question asked from either side, and only the first was ever answerable. `corpus.memory` counts unpinned entries *including* legacy lessons — that is the population competing for the recency cap. |
| `index.chars` | Sizes the channel: how much text entered `messages[0]` that nobody asked for. |
| `preload` | `mode` + `count` + `names`. Rides here so the **empty** preload case is provable — §3.8(a)'s defect — without touching the `knowledge` record's `items[]`, which `curate.scan_ledger` reads. §3.8(a) and (b) remain open for `knowledge` itself; this does not close them, it stops the absence from being unprovable. |

**Why this one cannot be reconstructed later, unlike every other gap in this document.** A missing `rule_eval` can be recovered by re-running the rules against the logged message; a missing `floors` can be recovered from a git blame of the constant. The index cannot: it is a pure function of a **mutable directory at a moment in time**, ordered by mtime, filtered by `expires`. Touch one file and yesterday's index is unrecoverable. The entry in the incident above carried `expires: 2026-08-07` — after that date the investigation returns nothing at all and the question stops being merely tedious. **This is the sharpest instance in the document of evidence that decays rather than merely being absent**, and it is why the record is unconditional rather than sampled or capped by relevance.

`TestContextRecord` (agent side, emit discipline), `TestIndexSelectionRecord` (skills side, payload).

> **Known second-order defect, not fixed here.** A memory's `description` is both the **retrieval key** `recall` ranks on and the **payload** injected into every task. So an entry written to be findable necessarily becomes a fact in every unrelated conversation — a question about Python gets the holiday address too. The record makes this measurable (`chars`, `items[]`); it does not fix it. The fix is descriptions that say what an entry is *about*, with specifics in the body behind `recall`, which is a corpus migration plus a `remember` prompt change.

---

## 4 · Evidence, not conclusions

Every `evidence` object in this document follows one rule:

> **Record the inputs the verdict was a function of, not a rendering of the verdict.**

`"evidence": {"reason": "the task was not about hotels"}` is worthless — it is the conclusion restated. `{"mode": "semantic", "sim": 0.212, "floor": 0.35, "rail": 0}` can be re-examined after the floor moves, aggregated across a corpus, and used to notice that the floor sits below the noise floor. That distinction is the whole content of #183's 481-call bill.

Shapes by tier:

**Tier 0 — structural.** The concrete facts tested and their values.
```json
{"field": "origin", "value": "email", "required": "!= user"}
{"on": "task", "pattern": "…", "matched": true, "span": [0, 43]}
{"hosts": ["news.example.com"], "known": 4, "known_source": "owner_text|approved"}
{"recipients": ["pawel@wenda.eu"], "parse": "clean", "all_owner": true}
```

**Tier 1 — scored.** Always `mode`, the score, and **the floor in force**. Deliberately the same vocabulary as the `knowledge` step's items (`sim`, `rail`, `score`, `mode`) so one reader handles both.
```json
{"mode": "semantic", "sim": 0.612, "floor": 0.55, "rail": 0, "against": "always-use-python-uv"}
```

**Tier 2 — judged.** The **declared** evidence the harness gathered, the judge's identity, and its closed-vocabulary answer.
```json
{"judge": "qwen3:8b", "isolated": true,
 "declared": {"deliverable_chars": 4600, "sources": ["news.example.com", "…"],
              "routed_tool": "youtube_analyze", "routed_tool_status": "incomplete"},
 "answer": "fail", "vocab": ["pass", "fail", "unclear"], "ms": 890}
```

Three constraints on Tier 2 records, all from #190/#191 and none negotiable:

1. **`declared` is what the harness gathered.** Never the acting model's summary of its own work. A judge record that quotes the actor's justification has recorded the thing the isolation invariant exists to exclude.
2. **`isolated`** asserts the judge ran in a fresh context. It is a claim the code makes about itself, and it is the one property that makes a judged verdict trustworthy at all — so it is recorded, and a future refactor that quietly passes the actor's transcript is then visible in the log rather than only in a diff.
3. **`answer` must be in `vocab`.** Recording the vocabulary alongside the answer makes an out-of-vocabulary reply (a parse failure) a legible event rather than a silently coerced one — the discipline `curate.parse_verdict` already applies.

**Never recorded as evidence:** the acting model's prose rationale for its own action. It may appear in `gate.message` (that is aish's text, shown *to* the model) and in the escalation card's re-proposal rationale, both clearly labelled — never in an `evidence` object.

---

## 5 · Abstentions are decisions

The primary #197 question is *"why didn't this fire?"*. Restated as a contract:

> **Any mechanism that evaluates a condition must leave a record that it evaluated, including when the answer was no.**

Concretely, and each of these is a live gap today:

| what abstained | must record | today |
|---|---|---|
| a rule that did not bind | one `rule_eval.evaluated[]` row, verdict `abstain`, with the evidence and the floor | does not exist |
| preflight selecting nothing | a `knowledge` step with `items: []` | **suppressed entirely** — but `context.preload.count` now proves the empty case (§3.10) |
| preflight scoring an entry below the floor | a `considered[]` row with `why` | not recorded |
| the knowledge index composing nothing, or capping entries out | `context` with `index.items: []` and `index.omitted` | **built** — §3.10 / #208 (2026-08-02) |
| an armed gate that allowed the call | `gate` with `verdict:"allowed"` | only auto-approval, as the bare string `"auto"` |
| a disarmed gate | *nothing* — provided the disarming fact is a session-level record | see §3.3 |
| a tool contract with no declared required fields | `required.declared: []` on the `tool` step | **built** — `returns: text` (2026-08-02) |
| a rule whose trigger could not be evaluated | verdict `unevaluable` + `fail` direction | does not exist |

The asymmetry is deliberate: abstentions are capped and truncatable (§8), binds and refusals never are. Losing the tail of a scored abstention list costs precision in a ledger; losing a bind loses the answer.

---

## 6 · Conformance inventory — the gates that already exist

#191's audit lists ~12 de-facto rules already enforcing policy in Python, plus #196's new origin gate. #197 **will be asked about these**, because they are the gates that fire today. Each row: what it does, what it logs now, and what it must log to be answerable.

Verdict column: **✓** answerable today · **~** partly · **✕** invisible.

### 6.1 · Stop gate (#81) — `_stop_gate` · ✕

Fires on a denial carrying a comment; refuses every tool call until a text-only turn.

*Today:* for `run_command` only, `_run_meta` puts `decision:"blocked"` and a fixed sentence on the `tool` step. For **any other tool**, the refusal text lands in the step's `error` field as prose, or nowhere. The **arming** — which denial, which comment, at which call — is not recorded at all, and neither is the clearing.

*Must log:* `gate{gate:"stop_gate", at:"gate", verdict:"refused", evidence:{armed_by_call: 2, armed_by: "denial_comment", comment: "…"}}` on each refusal, and one `gate{verdict:"allowed", evidence:{cleared_by:"text_only_turn"}}` when it clears. Without the arm record, "why was everything refused for four steps?" requires reading the conversation and inferring.

### 6.2 · Skill-read gate (#40) — `_skill_gate` · ✕

Refuses other tools while a preloaded truncated skill is unread; bounded to `GATE_MAX_REFUSALS` rounds.

*Today:* same shape as the stop gate — `decision:"blocked"` for `run_command`, prose in `error` otherwise. **The counter decay is invisible**, so the log cannot distinguish *the gate lifted because the skill was read* from *the gate lifted because the counter ran out*. Those are opposite outcomes: one is the mechanism working, the other is the model successfully ignoring it.

*Must log:* `gate{gate:"skill_gate", verdict:"refused", round:1, max_rounds:2, evidence:{pending:["x","y"], first:"x"}}` per refusal, and a terminal record with `verdict:"allowed", evidence:{lifted_by:"read"|"rounds_exhausted"}`.

### 6.3 · Egress / provenance gate (#178 P0-2) — `_egress_gate` · ~

*Today:* the approval round-trip writes a `kind:"command"` record with a prose decision string; the tool step carries the refusal text in `error`. The **novel host list**, the size and source of the provenance set, and whether a plain approve cached the host into `_approved_hosts` are all unrecorded.

*Must log:* `gate{gate:"egress", verdict:…, evidence:{hosts:["…"], known:4, known_source:"owner_text", cached_after: true}}`. "Why did this host prompt when that one didn't?" is a routine #197 question and is currently unanswerable without re-deriving `_hosts_in_text` by hand.

### 6.4 · Denylist — `check_denied` · ✓ (formalise only)

*Today:* `kind:"command"` record with `"blocked: <reason>"`, plus `decision:"blocked"` on the tool step. The reason is present, so the question is answerable.

*Must log:* a `gate{gate:"denylist", verdict:"blocked", tier:0, evidence:{pattern:"…"}}` for ledger uniformity. Low priority; correctness is already there.

### 6.5 · Approval + roots (auto-approval) — `is_auto_approvable` · ✕ (highest volume)

*Today:* `record(command, "auto")`. **No reason.** Which prefix matched, whether it came from the persistent allowlist / a session grant / `SAFE_COMMANDS`, whether path operands stayed in roots, which escapes were checked and cleared — none of it. Auto-approval is the single highest-volume decision aish makes and it is the least explained.

*Must log:* `gate{gate:"approval", at:"approval", verdict:"allowed", tier:0, evidence:{prefix:"git status", source:"always"|"session"|"safe_command", roots_ok:true, escapes:[], sensitive:false}}` — and on the prompting path, `verdict:"held"` with the same evidence plus the escapes that forced the prompt.

The other approval outcomes are recorded but as **prose decision strings** on a `kind:"command"` record: `"approved+always:git status,ls"`, `"auto (email)"`, `"approved (feedback: …)"`, `"blocked: reason"`. That is greppable, not readable — and `reconstruct_events` never reads `kind:"command"` at all, so it is invisible to every trace consumer. Structured `gate` records should be emitted **alongside**, not instead: the `kind:"command"` audit trail is a separate, older contract and nothing here changes it.

### 6.6 · Loop detector / stall budget / step ceiling · ✕

*Today:* **nothing structured.** The nudge and the stop are `[aish: …]` user-role messages, which `synthetic_kind` classifies as notes and `reconstruct_events` **skips**. So "why did this task stop?" is answerable only by string-matching aish's own prose inside a conversation record — the precise anti-pattern this document forbids.

*Must log:* `gate{gate:"loop"|"stall"|"ceiling", at:"loop", verdict:"advised"|"stopped", evidence:{repeats:5, tool:"read_url", args_sha:"…", stall:8, step:34, ceiling:60}}`. The step budget's progress-gating (#108) is invisible for the same reason: a task that ran to 47 steps and one that stalled at 12 leave logs that differ only in length.

### 6.7 · Near-duplicate memory gate (#178 P1-8) · ✓ *(closed by #192)*

The one gate that demonstrably **worked** in the #190 sessions — and it was invisible.

*Was:* a prose refusal in the tool result. The similarity score and the floor were not returned, not logged, and therefore never measured; `DEDUP_MIN_SIM = 0.55` is documented in source as "provisional until measured on the live corpus" and nothing measured it. Worse, the refusal string begins `"NOT saved — …"`, so `_emit_tool_step`'s prefix sniff recorded **`ok: true`** — a refusal logged as a success.

*Now:* `_near_duplicate` returns `(entry, score, floor, mode)` and `save_memory` takes an `on_admission` callback — a callback rather than a changed return type deliberately, since the string return is what `cli.py`, `curate.py` and every test read. `Agent._record_admission` emits the §3.7 `admission` record with Tier-1 evidence (`mode`/`sim`/`floor`/`against`) on BOTH verdicts, and the refusal now carries `decision: "rejected"`, so it is red. `DEDUP_MIN_SIM` becomes measurable from the logs on the next corpus pass.

### 6.8 · Preflight injection (#183) · ~

Covered in §3.8. Records the winners with good diagnostics; records neither the losers nor the floors, and suppresses itself entirely when nothing is injected.

### 6.9 · `cd`-not-sticky · ✓

*Today:* `_run_meta` with `decision:"rejected"` and the guidance text as `output`. Answerable.

*Must log:* a `gate{gate:"cd_not_sticky", verdict:"refused"}` for ledger uniformity. Note it as the model of a well-behaved refusal: the decision, the reason and the instruction are all in the record.

### 6.10 · `prefer_over` drift nudge (#140) · ✕

*Today:* **entirely invisible.** The nudge is appended to the result string *after* `_run_meta["output"]` has already been captured, so it is not even in the logged output. There is no way to ask whether the nudge has ever changed behaviour — which is the only question #140 has.

*Must log:* `gate{gate:"prefer_over", at:"post", verdict:"advised", tool:"run_command", evidence:{prefix:"gh issue create", suggests:"gh_issue_create"}}`, and the ledger then counts raw-command uses of a covered prefix over time. A nudge whose count never falls is a nudge that does not work.

### 6.11 · Recipient-scoped autonomy · ~

*Today:* `record(f"tool {name}({shown})", f"auto ({origin})")` — a prose string whose args repr happens to contain the recipients. The parse verdict itself (clean vs residue, the property `test_adversarial_recipients_never_pass_as_owner` pins) is not recorded.

*Must log:* `gate{gate:"recipient_scope", at:"approval", verdict:"allowed", tier:0, evidence:{recipients:[…], parse:"clean", all_owner:true, checked:"to,cc,bcc"}}`. Autonomy grants are Tier 0 forever (#190 decision 7); a licensing grant that leaves no structured evidence cannot be audited, and this is the one gate where a silent regression is a security regression.

### 6.12 · Draft-and-hold safe list · ~

Same record and same gap as 6.11; `evidence:{safe_list:"TRIGGERED_SAFE_TOOLS", hit:"gmail_send", origin:"email"}`.

### 6.13 · Plugin-tool verdicts (#141) — the green-lie corollary · ✓ *(closed by #192)*

`_dispatch_plugin_tool` set **no** `_run_meta`, so a denied, held or blocked plugin call was logged by prefix sniff alone. Measured against the actual strings:

| result constant | first token | logged `ok` |
|---|---|---|
| `DENIED_RESULT` | `USER DENIED` | **true** |
| `TOOL_HELD_FOR_ADJUSTMENT` | `NOT RUN` | **true** |
| `BLOCKED_RESULT` | `BLOCKED` | **true** |
| `EGRESS_DENIED` | `USER DENIED` | **true** |
| `READ_DENIED` | `USER DENIED` | **true** |

Five refusal paths logged green. The write path logs `decision:"held", ok:false` correctly, so the two halves of the same #81 semantics disagreed in the log. §3.4's `status` + `decision` fix all five at once, because they are computed by the runtime rather than sniffed.

**Wider than this table, as built.** `run_command`'s own refusals have the same defect by a different route: it sets `decision` in `_run_meta` but never `ok`, so the sniff decided there too — and `DENIED_RESULT`, `HELD_FOR_ADJUSTMENT` and `BLOCKED_RESULT` are shared with the shell path. A denied shell command therefore also logged `ok: true`. #192 fixes all ten sites with one rule in `_emit_tool_step` (see §3.4's `decision` row) rather than ten edits that the eleventh refusal site would not inherit.

### 6.14 · Origin gate on `remember` / `forget_memory` (#196) · new

Not yet built. It must ship **with** its records from day one — it is the first gate to be built after this contract and is therefore the conformance test for it:

`gate{gate:"origin.forget", at:"gate", verdict:"refused", tier:0, evidence:{origin:"schedule", tool:"forget_memory"}}`
`gate{gate:"origin.remember", at:"approval", verdict:"held", tier:0, evidence:{origin:"email", name:"…"}}`

---

## 7 · Ledger counters

#197's real ask is *evidence of improvement over time*, and #190 decision 8 makes counters the admission price for any non-structural verdict. These are computed by a `scan_*` pass over the logs in the shape `curate.scan_ledger` already uses — pure code, zero model calls, reading only the records above.

**Per rule, per window:**

| counter | source | why |
|---|---|---|
| `evaluated`, `bound`, `abstained`, `unevaluable` | `rule_eval.evaluated[].verdict` | A rule that never binds is dead weight; one that is unevaluable half the time has a broken trigger. |
| `sim_at_bind[]`, `sim_at_abstain[]` | `evidence.sim` on both verdicts | **Both distributions.** #183's lesson exactly: you cannot tell a threshold sits below the corpus noise floor from the binds alone — the separation between the two distributions is the signal. Recording only binds re-runs the 481-call audit. |
| `refusals`, `rounds[]`, `complied_after_refusal` | `gate.round`, next `tool` step | Does refusing actually change behaviour? |
| `escalations`, `owner_overrides` | `gate.escalated`, the approval answer | A rule the owner overrides every time it fires is **wrong** (#191's lifecycle signal). |
| `verify_pass`, `verify_fail`, `verify_unverified` | `gate{at:"verify"}` | `unverified` (judge unavailable) must be counted separately — a rule whose verification silently degrades to disclosure looks compliant. **Built (#191):** `verdict:"allowed"` with `evidence.checked` is the pass, `"refused"` is a check that failed and was asked about, `"advised"` is one that failed and shipped a note. A pass row is emitted only for bindings that actually decide something at turn end, so `checked: true` never claims a check that did not run. |
| `judge_calls`, `judge_ms_p50/p95` | `ms` on Tier-2 rows | #191's cost law, measured. |

**Per gate id** (the §6 vocabulary): `armed`, `fired`, `allowed`, `refused`, `held`, `blocked`, `advised`. This is what turns "which of the twelve gates is doing the work?" into a table.

**Per tool:** `calls`, `status{ok,incomplete,failed}`, `decision{approved,denied,held,blocked,auto}`, `truncated`, `continuation_offered`, `continuation_used`, `checks{pass,fail}`, `check_to_failure_gap` (birth pass → first runtime failure — the 11:17/11:22 signal, which is #190's accepted residual risk made measurable).

**Per incident class:** `first_seen`, `occurrences`, `repairs[{destination, at}]`, `recurrence_after_repair`. The last one is the metric the epic is judged by: *a class that keeps recurring after a repair means the repair was aimed at the wrong layer.* It is computable only if incidents carry a closed-vocabulary class and repairs carry a timestamped destination — §3.9.

**Counters are the admission gate, not a follow-up.** A rule with a Tier ≥1 verdict whose sim distributions are not being counted does not ship. That is #190 decision 8, restated where an implementer will read it.

---

## 8 · Compatibility constraints — non-negotiable

1. **Additive only.** Every record here is a new `kind` inside the existing `kind:"trace"` envelope, or a new key on an existing step. No existing field changes meaning; no existing field is removed. `tool.ok` in particular is **kept**, defined as `status == "ok"`, so the frontend needs no change.
2. **`reconstruct_events` must be byte-identical for pre-contract logs.** Pinned by a test that replays a fixture log captured before the change. New kinds are inert on old logs; `has_trace` must still be set by the `kind == "trace"` arm *before* the renderless skip, so a log containing only governance records reconstructs rather than falling back to a flat history blob (the existing `render_error` arm already has this ordering — preserve it).
3. **Renderless kinds say so explicitly**, in `RENDERLESS_STEPS` (§1.3), and are emitted only through `_emit_record` (§1.2) so they never reach `on_step`. Both halves are required: skipping on replay without suppressing live emission produces a hot/cold divergence, and `traceStep` opens a trace card before dispatching on kind, so an unhandled kind is a visible defect rather than a no-op.
4. **All writes go through `step_log` / `logref.step`**, which holds `SessionLog._write_lock` inside `_record`. Both the worker thread and the loop thread write; an unserialised append on the buffered handle is a torn JSONL line every reader silently skips. No new code path may open the log itself.
5. **Caps.** Every free-text field is capped at write time; the caps are named constants, not literals, so they can be tuned and so a truncated record says which cap cut it. Proposed: `GATE_MESSAGE_CHARS` 400 · `ACTION_ARGS_CHARS` 400 · `EVIDENCE_CHARS` 600 · `RULE_EVAL_MAX` 24 rows · `KNOWLEDGE_CONSIDERED_MAX` 8 rows · `INCIDENT_REFS_MAX` 20. Truncation of a *list* sets `truncated: <n>`; truncation of a *string* uses the existing `…` convention. **Binds and refusals are never dropped by a cap** — only abstentions and `considered` rows are, ordered best-score-first.
6. **No secrets, no fetched content.** `evidence` records hosts, names, scores, field names and enum values. It never records a secret value, an email body, or fetched page text. Recipients are already present in the existing `kind:"command"` audit line, so recording them structurally adds no exposure. Owner hosts are already in the log.
7. **Offline mirror.** `offline_events` wraps `reconstruct_events` and caps bulk output; renderless kinds are skipped upstream of it, so it needs no change and the mirror ships no governance evidence to the browser. (A future "explain this turn" UI would need a deliberate new endpoint — out of scope here, and noted so nobody assumes the data is already client-side.)
8. **`curate.scan_ledger` keeps working.** It reads `knowledge` steps' `items[]` and `tool` steps' `name`/`summary`. Neither changes shape. `_windows`' positional pairing keeps working, and once `turn` exists it should migrate to it — but the migration is not a precondition for this contract.

---

## 9 · Design forks for human decision

**Forks 3 and 6 were DECIDED by the owner on 2026-07-31 and are no longer open** — see the resolutions inline below. The remaining forks stand at their recommendations unless a phase finds cause to revisit.

Collected. Each is a genuine judgement call, not an oversight.

**Fork 1 — how far to stamp `turn`** (§2). Recommendation: new kinds plus `tool_start` / `tool` / `knowledge`; leave `thinking` alone.

**Fork 2 — recording whether a redirect was followed** (§3.7). Recommendation: leave it to ledger correlation rather than have the harness guess causality.

**Fork 3 — should a rule refusal render in the UI? — DECIDED: renderless in v1.** A `gate{verdict:"refused"}` is arguably something the owner should *see* — aish refusing itself is exactly the visible behaviour #191 promises. But rendering it means a renderer, a trace row, hot/cold parity work, and a decision about what a refused-then-complied turn looks like. **Recommendation: renderless in v1**, with a follow-up to render `refused` / `held` rows once #191's behaviour is settled. The model-facing refusal already surfaces indirectly in the answer, and #191's acceptance criteria are about model behaviour, not UI. Flagged because it is the one place this document's "renderless" default might be wrong. **Owner decision (2026-07-31): renderless for v1, as recommended.** Revisit once #191's behaviour is settled — a refusal the owner never sees is acceptable while the model-facing half is being proven, not permanently.

**Fork 4 — one `gate` kind or several?** The alternative is `rule_gate`, `approval_gate`, `stop_gate`… as distinct kinds. **Recommendation: one kind, discriminated by the `gate` field.** One reader, one ledger key, one renderless entry; a new gate is a new enum value, not a new record type nobody's reader knows about. The cost is that the `evidence` object is polymorphic — which it would be either way.

**Fork 5 — `gate{verdict:"allowed"}` volume.** §3.3's armed-only rule keeps volume proportional to governance activity, but auto-approval is exempted (it always logs) and auto-approval is the highest-volume decision aish makes. On a busy session that is a `gate` record per command. **Recommendation: log it anyway** — it is the single biggest current blind spot (§6.5) and JSONL lines are cheap next to command output already in the log. If it proves noisy, the honest fix is sampling with a recorded sample rate, never silent suppression.

**Fork 6 — where `incident` records live. — DECIDED: session log.** Written into the session where the owner complained (proposed), or into a separate incident log? A separate store makes the ledger's recurrence query trivial and makes incidents survive session deletion; the session log keeps everything in one append-only place with one lock and one reader. **Recommendation: session log**, with the ledger scan building the index — consistent with every other decision in aish's logging. Flagged because the recurrence metric is #197's headline deliverable and a scan over hundreds of session files is the slower path. **Owner decision (2026-07-31): session log, as recommended.** One append-only place, one lock, one reader; the ledger scan builds the index. If recurrence queries prove too slow, add an index — do not move the source of truth.

**Fork 7 — `isolated: true` is a self-report.** §4 records that a judge ran in a fresh context, but the record is written by the same code that would be wrong if it didn't. It catches a *refactor* that breaks isolation (the record and the code diverge visibly), not a *lie*. There is no cheap way to make it stronger. Noted so nobody mistakes it for a proof.

**Fork 8 — `rule_eval` cost on an empty corpus.** Until rules exist, this record is `{"corpus":{"total":0,…},"evaluated":[]}` on every turn. That is one line per turn saying "no rules". It is also precisely #197's *"No rule was evaluated for this turn"* answer, made explicit instead of inferred — corollary 2. **Recommendation: emit it.** Flagged because it looks like noise and is not.

---

## 10 · Acceptance for phase 1 and after

This document is the gate. A phase conforms when:

- Every decision it adds emits a record from §3, through `_emit_record` or `logref.step`.
- Every abstention it can make is recorded per §5.
- Every scored or judged verdict records its inputs **and its floor** per §4, and its ledger counters per §7 exist before the verdict ships.
- Its records are in `RENDERLESS_STEPS`, and the pre-contract replay test still passes byte-identically.
- Its entry in §6 moves to ✓.

And the whole contract is validated the day #197 is built, by one test: point it at `session-20260730-112152-153277.jsonl` — the Session B log — and ask why the answer came from news sites. Under this contract the answer is a lookup: `tool` step with `status:"incomplete"`, `required.empty:["transcript"]`; a `gate` record showing the substitution was ungated because no rule bound; a `rule_eval` row (had rules existed) explaining why. Today it is a grep through aish's own source that returns the wrong truncator.
