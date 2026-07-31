# The rules engine — turn contracts the harness enforces

`rules.py`, plus the two enforcement points it owns in `agent.py` (`seed_rules`, `_rule_gate`).

**How to use this file.** The laws come first; they are what the whole engine rests on and every one of them was argued for before it was coded. Then the model (rule → binding → enforcement point), the evaluation ladder, the vocabulary, the file format, and last: **what v1 actually built, what it deliberately did not, and the one place the shipped check is weaker than the design.** If you are here to add a trigger kind or an obligation verb, read the laws and the vocabulary — the rest is mechanism.

Records are specified in `docs/trace-contract.md` (binding) with the rationale in `docs/trace-records.md`. The agent loop and the gates this one sits beside are in `docs/agent-core.md`. The knowledge lifecycle rules inherit is in `docs/knowledge-layer.md`.

---

## Why this exists

aish had three artifact classes and **none of them was binding**. A skill is prose the model *may* consult; a memory is a fact retrieval *may* surface; a tool is a capability the model *may* invoke. `pinned: yes` memory was the attempt at binding policy and it fails for a reason no better-written memory can fix: **pinned prose in a context window is still advice.**

So when the owner says *"if I give you only a YouTube URL, analyse that video and nothing else"*, there was nowhere to write it down. He told the model; the model wrote a memory; the memory was not retrieved on the triggering turn — a bare URL has no lexical or semantic surface to match — and when it was retrieved it was ignored.

> "The most frustrating element is that we set the rule and it's not followed. If we do it like that, it's never gonna end."

A **rule** is the fourth artifact class: a deterministic *precondition → obligation* pair the **harness** evaluates and enforces. Same md+frontmatter family as skills and memory, same lifecycle, git-backed, hand-editable — **one format family, three binding semantics**: skills *may* be consulted, memory *may* be recalled, rules *must* be obeyed.

**This was an extraction, not an invention.** aish was already full of rules living in bespoke Python: the stop gate, the skill-read gate, the egress gate, `cd`-not-sticky, the denylist, the loop detector, the near-duplicate memory gate. What was missing was a *vocabulary* — somewhere to write one down that is not a code change. The clearest exhibit in the audit that produced this engine: `always-answer-user-comments-before-tools`, a pinned memory restating the stop gate — a rule the harness already enforces in code. The owner was so unsure advice would hold that he duplicated an existing code rule into prose.

---

## The laws

**R1 · Rules restrict. Allowlists license. The two never share a file, a vocabulary, or an evaluation tier.** There is deliberately **no seventh verb** — no *allow*, no *auto-approve*, no *trust*. "Auto-approve trippy" is not expressible on purpose. Autonomy grants stay where they live (the persistent allowlist, session prefixes, trusted roots, the recipient parse) and stay structural forever. The consequence is the safety property the whole engine leans on: **a rule-engine bug can OVER-restrict — loud, visible, annoying — and can never UNDER-restrict.**

**R2 · A verdict is a fixed function of declared evidence, and the actor controls no input to it.** Three channels are closed, not one. *Authority*: code decides whether the verdict function runs and what its output means. *Evidence*: the function sees only what the rule declares, gathered by the harness, never framed or summarised by the acting model — this is the provenance discipline exactly, because tool results are what an injection controls. *Output space*: a closed vocabulary, so "the model said something persuasive" is not a possible verdict. "Deterministic" was always a proxy for *incorruptible by the party being policed*; determinism buys that for free, which is why the instinct was right — but an isolated, schema-bound, closed-vocabulary model call can have it too.

**R3 · Restriction may be judged. Expansion of autonomy must be structural.** A judge may decide "this turn is bound", "this answer failed verification", "hold this". A judge must **never** decide "this recipient is the owner", "this host is trusted", "this command is read-only". The reasoning is adversarial: a licensing check's input is chosen by the party that benefits from passing it, a judge has a nonzero fooling rate, and an attacker gets unlimited attempts to search it. The recipient parser is the proof — `"pawel@wenda.eu"@evil.com` is valid RFC 5322 routing to evil.com, and the residue-checking parse caught it because **a parse cannot be sweet-talked**. This also closes the injection question: a judged trigger reading fetched pages is an injection surface, but the worst a fooled judge can do is bind a restriction that did not need binding — a spurious refusal, visible in the trace. The dangerous quadrant (judged + licensing + untrusted input) is not expressible.

**R4 · The engine slots in ALONGSIDE the existing gates, never above them.** Stop gate, skill gate, denylist, approval, egress and the origin gates run regardless of what any rule says. `_rule_gate` is a step in `_dispatch` between the skill gate and everything else; it can add a refusal and it has no path to lifting one. `TestRuleGate` pins that a rule cannot make a denial approve.

**R5 · Prose explains, gate enforces.** Every binding announces itself in the model's context before anything is refused — **the model must never be ambushed by a gate**. The gate enforces regardless of whether the model agrees. The pairing was discovered ad hoc in the email trigger prompt (*"answer via a NEW gmail_send"* is the courtesy explanation; the hold on threaded replies is the real enforcement) and is now the shape every rule ships as. Whether the prose actually landed is recorded, not assumed — see the `binding` record's `seeded` field.

**R6 · Every refusal is instructive, and bounded.** Instructive: it names the rule, says why, and says what to do instead — the harness carries the feedback so the owner does not have to. Bounded: `RULE_MAX_REFUSALS` (2) refusals per binding, then escalation, because a gate that refuses forever wedges a small model into a stall-out. The skill gate learned this the same way.

**R7 · Refuse-first when compliance is within the model's power; hold for the human when the decision is the owner's by construction.** `cd`-not-sticky refuses and never escalates; the egress gate goes straight to a hold. Route, prohibit and sequence are the first kind — the model can choose a different action. Trusting a host or reaching a non-owner recipient is the second: the model cannot comply its way out, because the question is not addressed to it.

---

## The model: rule → binding → enforcement point

A **rule** is a static, owner-authored file: trigger → obligations, plus declared tier, failure direction and evidence. **Rules are inert.**

The runtime object is the **binding**: when a trigger matches a turn, the harness creates *(rule, evidence snapshot, obligations)* attached to that turn. Everything downstream is defined over bindings, and this one concept does most of the work:

- **The gate is cheap by construction.** "Is this call compatible with the active bindings?" is set membership, because the expensive evaluation already happened at turn start. The cost law falls out of the model rather than being a policy bolted on.
- **Composition is defined.** Multiple bindings coexist; since every obligation is a restriction (R1), they compose by **union of restrictions** — monotone, order-independent, no precedence algebra, no weights. `TestComposition`.
- **Conflict is surfaced, not resolved.** Two route obligations demanding different sole sources is a genuine contradiction: refuse to the model naming both, escalate if unresolved. Never silently resolved by priority numbers — for a personal tool, conflicts are rare and owner-fixable, and a precedence system would hide exactly what should be loud. *(Designed; not built in v1 — see below.)*
- **Observability is a property of bindings.** Every bind, gate verdict and (later) verification is a trace record carrying the rule name and the declared evidence.
- **Unsatisfiability is caught at bind time.** A route naming a tool that no longer exists surfaces at seed, not months later — otherwise the rule would refuse every alternative while offering nothing. `TestBinding`.
- **A binding carries its COMPILED obligations.** Rule files are hand-editable and git-backed; a record naming only the rule forces a later reader to open today's file and claim it governed a turn three weeks ago.

### The four enforcement points

| point | when | what lives there | v1 |
|---|---|---|---|
| **Seed** | turn start | evaluate the corpus, create bindings, announce them (R5) | **built** |
| **Gate** | pre-dispatch | membership against the binding: route / prohibit / sequence / hold | **built** |
| **Verify** | turn end | did the deliverable satisfy route / shape / disclose? | **not built** |
| **Audit** | offline | the ledger and the weekly curate pass, for rules too fuzzy or too expensive to check per turn (style rules like sycophancy belong here, not in a per-turn judge) | not built |

**Verify is the genuinely new capability.** Nothing in aish has ever checked the *answer*: the loop detector and the stall budget are mid-loop. It is where the #190 incident — a 4,600-character answer sourced entirely from news sites and presented as a video's content — would have been caught, and it is the next slice.

---

## The evaluation ladder

An earlier draft of this design said a trigger must be evaluable "without a model call". **That was wrong.** The invariant is R2, not determinism.

- **Tier 0 — structural.** Regex, parse, field check, set membership. Microseconds. Use whenever sufficient.
- **Tier 1 — scored.** Embedding similarity against a code-held threshold. aish already runs this (preflight, the near-duplicate gate): cheap, local, multilingual, resident. This is how *"the task is an accommodation search"* gets evaluated — not by regex, which fires on "Hotel California lyrics".
- **Tier 2 — judged.** A schema-bound generative call, isolated context, closed output, for semantic questions a score cannot answer (*"does this answer analyse the video, or a news article about it?"*). **Isolation is about continuity of transcript, not identity of weights**: the same model is fine, the same context is not, because a judge inheriting the actor's half-built justification will ratify it.
- **Tier 3 — human.** On judge uncertainty, model insistence after refusal, or genuine conflict. **Never by default** — the owner must not be the bottleneck.

**Don't judge what you can check** — but equally, **don't structurally check what you can't honestly express.** A regex cosplaying as a semantic trigger produces silent wrong-binding, which is worse than a judged trigger with a measured error rate. Tiers compose: structural rails propose, semantics confirm — a keyword hit is a prior, never a bypass.

**Cost law.** A generative judge on this machine is seconds, serialized behind the session's own calls. So: **per-dispatch checks are Tier ≤1 against a precomputed binding**, and **generative judging happens only at turn boundaries** — trigger matching at seed over the 2–3 candidates Tier 0/1 prefiltered from sixty, and deliverable verification at turn end. Two calls per *bound* turn, zero per unbound turn. The `ms` field on every evaluation row exists so that claim is falsifiable rather than merely believed.

**Calibration debt is an admission requirement.** A regex is wrong in ways you can read; a scored or judged verdict is wrong at a **rate**, invisible until counted. The standing bill is the retrieval threshold that sat below the corpus noise floor for months and took a 481-call audit to find. Therefore: **no rule with a non-Tier-0 verdict ships without its ledger instrumentation** — binds, refusals, compliance-after-refusal, escalations, owner overrides, and *both* score distributions (at bind and at abstain; the separation is the signal, and the binds alone cannot show it). Not "observability is nice" — admission-gated. **v1 is Tier 0 throughout, which is why it ships without counters**; the records are shaped so the scan is a pure function over the log when the first scored trigger arrives.

**Privacy composes:** the judge runs on the session's own model or a stricter one, and an unbuildable judge **fails closed per the declared direction, never silently falls back to cloud**. Honest caveat: on local, an 8B judges its own kind. Isolation removes the *motive* — self-justification is mostly a context pathology — but same-weights blind spots correlate, and the privacy constraint forbids model diversity. The mitigation is the closed vocabulary and the Tier 0/1 prefilters, not a second opinion.

---

## Vocabulary — six triggers, six verbs

**Trigger kinds.** message shape (T0) · session context: origin, harness events, repeat counts (T0) · **tool outcome** (T0, needs the result envelope — structurally undeliverable by retrieval, which is the whole proof that memory was the wrong channel) · task domain (T1/T2) · action shape: command prefix, recipients, host, path (T0) · deliverable shape (T0 where structural, T2 where semantic).

**Obligation kinds.** **route** (this deliverable comes from tool X) · **prohibit** (these sources are off-limits without asking) · **sequence** (A before B) · **disclose** (a named failure state must be stated, never silently patched) · **shape** (the deliverable includes/avoids something) · **hold** (this waits for the owner).

Every verb is a restriction. See R1 for why there is no seventh.

A trigger reads facts the **harness** gathered, never the acting model's account of its own turn (R2). `message_shape` reads the task text; `session_context` reads `Agent.origin`. Both record the inputs they were a function of — the pattern itself, the field and the value — rather than a rendering of the answer, so a trigger can be re-examined after the file changes and a corpus of them can be counted. `TestTriggers`.

---

## The file format

One file per rule in `~/.config/aish/rules/`, global only — a rule is a policy about how aish behaves, not a property of a checkout, and a project-local rule file would be a policy anyone who hands you a repo can write. Two worked examples ship in `examples/rules/`, one per v1 trigger kind, and `TestShippedExamples` keeps them loadable.

```markdown
---
name: youtube-url-analysis
description: A message that is nothing but a YouTube URL means analyse THAT video.
tier: 0
fail: open
trigger: message_shape
match: ^\s*<?https?://(www\.)?(youtu\.be|youtube\.com)/\S+\s*$
route: youtube_analyze
prohibit: web_search, read_url
unless: disclosed
disclose: transcript_unavailable
disclosure_terms: transcript, youtube_analyze
---

Prose the model is shown verbatim when this rule binds. Explain the intent —
this is the half that keeps a refusal from being an ambush.
```

| key | meaning |
|---|---|
| `trigger` | `message_shape` (needs `match:`) or `session_context` (needs `field: origin` plus exactly one of `is:` / `is_not:`) |
| `tier` / `fail` | declared from day one so a v0 file does not break when a scored trigger arrives. `fail` is the direction for an **unevaluable** trigger: `open` = do not bind, the owner is watching; `hold` = bind conservatively AND send the first violation straight to the owner, since the harness could not confirm the trigger and must not decide the exception itself |
| `route` | the tool the deliverable must come from |
| `prohibit` | comma- or space-separated tool names, plus optional `unless: disclosed` |
| `disclose` | the failure state that must be stated; `disclosure_terms` overrides the words derived from the state slug |
| `status` / `expires` | the knowledge lifecycle, inherited verbatim through `skills.lifecycle_active` — evaluated at read time, so a long-running process crosses an expiry without an mtime change |

A file that parses but does not **compile** — unknown trigger, unparseable regex, `unless: disclosed` with no `disclose:`, a rule with no obligation at all — still yields a `Rule`, carrying its error. It is recorded with verdict `error` and binds nothing. That is deliberate: a hand-edited typo must be visible in the corpus and in the log, never an exception thrown inside a gate half a turn later, and never a silent absence. `error` (broken file) and `unevaluable` (working rule, failed evaluator) fail in **opposite** directions and are never conflated — a typo must not hold every unattended turn. `TestRuleFileFormat`, `TestRuleLifecycle`.

---

## What v1 built

The file format · the loader with lifecycle inheritance · the binding runtime · **seed** and **gate** with bounded refuse-first and Tier-3 escalation · the three trace records · trigger kinds **message shape** and **session context** · obligation verbs **route**, **prohibit**, **disclose**.

**Seed** (`Agent.seed_rules`) runs at the top of every task, from the same position the knowledge step is emitted — before the user message, so nothing this turn dispatches can outrun it. It is called by **both** entry points: `run_task`, and `ClaudeMaxAgent.run_task`, whose SDK owns its own loop. A rule that governed only local turns would be a rule the model escapes by being asked on a different backend. The prose rides the per-task system reminder (so it is replaced every turn rather than accumulating) on the native loop, and the prompt itself under claude-max; `mark_rules_seeded` writes the `binding` records only once it has landed. `TestRuleSeeding`.

**Gate** (`Agent._rule_gate`) runs in `_dispatch` after the stop and skill gates. Refusals are `ToolOutcome`s carrying `decision: "blocked"`, so a refused action is never a green step — that rule is applied once in `_emit_tool_step`, not per refusal site. `TestRuleGate`.

**Bindings force dispatch off the parallel read-only path.** `_execute_tool_calls` fans read-only tools out concurrently, and that path bypasses `_dispatch` entirely — so `_bindings` joins the skill gate and the stop gate in the condition that forces sequential execution. This is not an optimisation detail: `web_search` and `read_url`, the two tools the canonical rule prohibits, are exactly what that branch fans out. A rule that only holds for serial turns is not a rule.

**Refusal text is uncapped.** `GATE_MESSAGE_CHARS` = 400 is a **write-time** cap (contract §8.5) and applies where the record is written, never to the text handed to the model. It was applied in both places, and the canonical rule's disclose refusal landed at *exactly* 400 characters — losing its closing clause, *"do not present another source's material as if it came from `<route>`"*, which is the one sentence this engine exists to deliver. An instruction cut mid-clause is the uninstructive refusal R6 forbids. `TestShippedExamples` pins that every refusal the canonical rule can produce ends on a complete sentence.

**Escalation.** After `RULE_MAX_REFUSALS` instructive refusals, the next violation is the model's insistence, and **insistence is its appeal**: it goes to the owner on the ordinary approval card, carrying the rule and the refusal count. An approval **overrides the binding for the rest of the turn** (per-call re-prompting would be friction on a decision already made) and is recorded, because a rule the owner overrides every time it fires is a wrong rule. A denial is final and arms the stop gate, exactly as on every other card. With **no approver** — an unattended session — the refusal becomes final and says so: *stop retrying, finish with what the rule allows, and state what you could not do.* That fails to restriction, and it terminates rather than looping into the stall cap. `TestBoundedRefusalAndEscalation`.

**Two shipped examples**, one per trigger kind. The canonical YouTube rule is the message-shape one. The session-context one is `origin ≠ user → prohibit forget_memory` — which `_knowledge_gate` now also enforces in Python, so installing it adds a refusal that names a rule and adds no restriction that was not already there. It ships as the worked example of the second trigger kind **and** of the extraction direction: a conduct rule moving out of bespoke Python into a file the owner can edit, with the Python left in place as the backstop. That overlap is R1's safety property demonstrated rather than asserted.

### The records

Three renderless kinds, all through `Agent._emit_record` and all in `session.RENDERLESS_STEPS`. Both halves are required — see `docs/trace-records.md` for why a kind with no renderer opens an empty live card rather than doing nothing.

- **`rule_eval`**, once per turn at seed, one row per active rule **whatever the verdict**, plus the skipped corpus with reasons. It is emitted even when the corpus is empty. That looks like noise and is not: *no rule existed*, *a rule existed but was retired* and *a rule existed and abstained* are three different answers with three different repairs, and absence must never be the evidence.
- **`binding`**, one per binding at creation, carrying the compiled obligations, the evidence snapshot, satisfiability and `seeded`.
- **`gate`**, one per active binding per dispatched call — including the ones it **allowed**, because an armed gate that stays silent is indistinguishable from a disarmed one. It carries `round` / `max_rounds` (a gate that lifted because its counter ran out is a completely different event from one that lifted because the model complied), `escalated`, and the exact refusal text handed to the model. `TestRecordShapes` pins the caps: binds and refusals are never dropped by one, only abstentions are.

Every record is stamped with the turn by `_emit_record` and, where it concerns one action, with the call id — published thread-locally in `_call_result` so a verdict cannot join to the wrong call under a future refactor.

---

## The one place the shipped check is weaker than the design

`disclose` belongs at **Verify**: the question is whether the *deliverable* states the failure, and that is a turn-end, Tier-2 question. Verify does not exist yet, so v1 enforces it at the gate as *"prohibited unless the failure has been stated"*, checked Tier 0: the routed tool returned a non-`ok` envelope status, and the model has since emitted assistant text containing one of the rule's declared disclosure terms.

Assistant text is the only place a mid-task disclosure can live, because in aish a text-only turn **ends the task** — so the disclosure rides the preamble the model emits alongside its tool calls, which is text the user sees. Ordering is load-bearing: the text is fed to the bindings *before* the same turn's tool calls are dispatched.

**This check is weak on purpose and its weakness is bounded.** It verifies the failure was *named*, not that it was named *well*: a model that says "the transcript is unavailable, let me search" satisfies it. What it buys is the property that actually failed in the incident this engine came from — **substitution can no longer be silent** — and it cannot be satisfied by saying nothing. What it does not buy is a guarantee that the final answer discloses anything, or that the substituted material is not presented as the routed tool's output. That is Verify's job, it needs a judge, and it is the next slice. `TestDiscloseBeforeSubstituting` pins both halves: undisclosed substitution is refused, disclosed substitution proceeds, and a **successful** route never licenses a second source at all.

Two smaller honest edges, both recorded rather than hidden:

- A **later** failed route re-arms the disclosure requirement. One disclosure is not a licence for the rest of the turn.
- An identical refusal repeated is identical `(tool, args, result)`, so the loop detector can fire on a model that will not stop. That is a correct outcome — the final no-tools turn makes it report — but it means the stall/loop terminators, not the rule engine, are what bound a maximally stubborn model.

---

## Deferred, in order

Each arrives as a new verdict function or a new enforcement point **against this same binding runtime**. Nothing in v1 is scaffolding to be torn out.

1. **Tool-outcome triggers.** The result envelope makes them available now; they are what lets a rule bind *mid-turn* on "the transcript came back empty" rather than only at seed. The `binding` record already carries `at` for exactly this.
2. **Verify.** See above. It also turns `shape` and the honest half of `disclose` into real obligations.
3. **Tier 1 scored triggers.** Unlocks migrating the ~25 behaviour-shaped memories in the live corpus. Ledger instrumentation is the admission price.
4. **The rule card and retro-match preview.** The owner says, in plain language, *"stop substituting web search when the transcript fails"* — mid-task, in a denial comment, anywhere — and the model compiles it into the vocabulary and presents a card: plain-language intent, compiled trigger and obligations, declared tier and failure direction. Plus the feature that makes review real rather than ritual: because triggers are functions of logged evidence, the harness runs the proposed trigger against recent session logs and shows *"this would have bound on these 3 turns — here they are."* That single feature dissolves the core complaint: the owner provides feedback in prose, approves behaviour he can **see**, and never hand-writes trigger syntax unless he wants to. The card is sugar over the same file, never a second store.
5. **Tier 2 judged triggers and verification.** Last — exhaust the cheaper rungs first.
6. **Conflict surfacing**, `sequence`, `shape` and `hold` verbs, and the remaining trigger kinds, as rules that need them appear.
7. **Extraction of the hardcoded conduct rules.** Opportunistic, and a direction rather than a milestone. **Security rules never migrate** — R3 says autonomy stays structural, and the egress, recipient-scope and denylist gates are licensing checks wearing a gate's clothes.

**Lifecycle.** Passive retirement (`status: disabled`, `expires:`) is inherited today. Ledger-driven review is deferred with the counters: a rule that never binds is dead weight, a rule the owner overrides every time it fires is **wrong**, and a scored trigger whose bind rate drifts is miscalibrated. The weekly curate pass should read those and **propose** repair or retirement on a card — never auto-retire. Rules are owner property.

**Not building, ever, unless the reasoning above changes:** an allow verb; project-scope rule files; priority numbers to resolve conflicts; auto-authored rules that land without the owner seeing them.
