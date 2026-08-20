# Diagnostics — reading back why a turn went the way it did

`explain.py` (the reader), `evidence.py` (the bytes it points at), and the `brief` record `agent.py` writes. Issues #214 (reader) and #239 (the brief).

**How to use this file.** The law first, because everything else is a consequence of it. Then the three states, the brief and why it is per model call, the evidence store, turn bracketing, and what is still missing.

---

## The law

> **An explanation is assembled from recorded evidence, never re-derived from source.** (`docs/trace-contract.md` §0)

Asked once why its output had been truncated, a model went grepping through aish's own source, found one of three truncators, and confidently reported the wrong cause. It could not have done better: the log recorded the *outcome* (a short result) and nothing about the *decision* (which truncator, what cap, from where).

So the reader makes **no model call**, opens **no rule file**, and imports nothing that could report how aish behaves *today* instead of how it behaved then. `TestReaderIsPureAndHonest.test_no_model_and_no_live_state_can_reach_the_reader` pins that as a source-level check, because the failure mode is an import that looks harmless — reaching for the live tool table to "helpfully" fill in a description is exactly how a reader starts lying about the past.

Where the log cannot answer, the reader **says so**. "No rule was evaluated for this turn" is a real and useful answer.

---

## Three states, never two

`docs/trace-contract.md` corollary 2 says absence must never be the evidence. Deletion makes that a three-way distinction, not a two-way one:

| state | what it means | how it is reached |
|---|---|---|
| **not recorded** | the aish that wrote this log never emitted this kind | reading a log older than the record |
| **recorded, empty** | it was evaluated and selected nothing | a turn where retrieval matched nothing |
| **recorded, then removed** | it existed and was deliberately deleted | `redact_turn`, the Retry rewind, `evidence.purge` |

Collapsing the third into the first is the failure that matters: someone diagnosing a capture bug that is really a redaction, or reading "no knowledge was injected" off a turn whose knowledge was purged. `TestReaderIsPureAndHonest` covers all three, and `Log.wrote(kind)` is how the first two are told apart — a kind absent from the **whole file** means the writer did not exist, which is a different answer from a turn that happens to have none of them.

---

## The brief, and why it is per model call

**The brief** is everything the model was handed before it acted. Slice 1 records the part that answers the owner's actual question: **the tool menu** — which tools were on it, under what descriptions, with what argument schemas — plus the model id, `num_ctx` and the thinking flag.

The failure this exists for: a model that produces a workaround instead of using a capability. The decisive evidence is never the reasoning, it is **what the model was holding**. Before this record, "was the tool absent, mis-described, or never offered?" was answerable only by reading the installed wheel and the plugin directory *as they are today* — re-derivation, and of evidence that decays, since both are mutable.

**Per model call, not per turn.** The menu is not a per-turn fact: `_refresh_plugin_tools` runs at the top of every `_chat_turn`, so a tool written or dropped in mid-task changes it between one step of a turn and the next. A per-turn record would let a reader conclude that the call which misbehaved held a menu it never held — the confident-false-conclusion class this record exists to prevent. `model_call` names which call within the turn it was true of.

This **amends `docs/trace-contract.md` §2 design fork 1(b)**, which deliberately left the high-volume kinds unstamped. The amendment is narrow: `brief` carries `model_call`; `thinking` is still untouched.

**Written only when the STAMP changes** — the menu digest and the system text together. Both are "what it was handed", and a reader asking why a turn went wrong cannot know in advance which of the two moved. That is the interning rule — *present at or before first reference*, not "exactly once", because the log is not append-only (below). `TestBriefWriter` pins both halves of renderlessness, the per-call grain, and replay byte-identity.

In practice the menu is near-constant and the system side is what paces the record: the per-task reminder carries the current time, so one brief lands per task, and more when a tool or a rule moves mid-task. What makes that rate affordable is content addressing rather than restraint — the standing prompt is the bulk of the text and is on disk once, however many tasks quote it.

## What it was TOLD — the system text (#239)

Recorded as **the bytes that go out**, not as a list of contributors. The system content is assembled from four sources that each change on their own schedule — the static prompt, the caller's environment context, the live skills/memory index, and the per-task reminder carrying preloaded knowledge and the rules in force — and recording them separately would make the reader reassemble them in the right order to answer *"what was it actually told"*. That reassembly is a re-derivation, and it would be wrong the first time any of the four changed shape.

The `context` and `knowledge` records already name **which** memory or skill was injected. They cannot answer the question the owner actually asks, which is not "which memory" but *"did the belief it acted on come from a rule, from a memory, or from nowhere"* — a model that decided it must only consult one kind of source, and a reader who cannot tell whether a rule said so. Only the text settles that.

**No cap, deliberately.** Everything here is about to be sent to a model, so it already fits in `num_ctx` by construction; a cap could only truncate evidence the model itself received whole. This is the one place in the trace where §8.5's named-constant cap rule does not apply, because the constraint is upstream and tighter.

`_system_evidence` records the position, the length, and the digest of each system-role message as it stands in `messages` at the moment of the call. `TestBriefWriter` pins that the stored bytes are byte-identical to what was sent, that a changed memory or rule writes a new brief while the menu digest stays put, and that the parts are in send order.

---

## The evidence store

`evidence.py`: content-addressed bytes under `<state_dir>/evidence/<aa>/<sha256>`. The log holds names and digests; the bytes live once.

Three reasons the bytes are **not** in the log, in order of importance:

1. **Erasability.** The log records injected knowledge by name only, and the contract says so explicitly — it is the one text an audit record must not duplicate (§3.10). Verbatim bodies in every log would break that: a secret pasted once and then remembered would be written into every later session that preloads that memory, so redacting the original turn would leave copies elsewhere forever, and `forget_memory` would stop being a deletion. `purge(digest)` drops the bytes once, everywhere, and every reader then honestly says *purged*.
2. **The log is not append-only.** `redact_turn` and `rewind_last_turn` — the latter fires on **every web Retry**, a daily action — both rewrite it in place. A blob written inside one turn can be deleted while later turns still reference it. Blobs outside the rewritten file cannot be lost that way.
3. **Deduplication.** The menu is ~31 KB and near-constant, so one copy serves hundreds of sessions.

**A blob is re-hashed on read.** A file that does not hash to its own name has been truncated or tampered with, and handing it back would let a reader quote evidence that is not what was recorded — so it reads as absent instead. **`put` with no state dir still returns the digest**, so a caller with nowhere to write records a reference the reader reports as unresolvable rather than as never recorded.

`TestEvidenceStore` pins the round trip, the single-copy guarantee, purging, and the tamper check; `TestReasoningCapture` and `TestEmittedArguments` pin the two #240 records, including that the default cap cannot bite at the default context window.

This is **not a second log.** It has no records, no ordering and no clock; the digest is the entire join. The argument against a parallel debug log — two files correlated by timestamp, which is the positional correlation the trace contract exists to kill — does not apply to something joined by content address.

---

## Turn bracketing — the one positional inference

Everything else joins by id: trace records carry an int `turn`, and a `call` number joins a gate verdict to the action it governed. The turn **boundary** is positional, because that is how it is written, not a guess about ordering.

`task_start` is the boundary when the log has any: it is written first and carries the prompt verbatim, so the seed-time records (`rule_eval`, `context`, `knowledge`) land inside the turn they describe rather than trailing the previous one.

**But `task_start` comes from the CLI and the server, not from the agent.** A log written by any other path, or one predating the bracket, has none — and bracketing on it alone reported those as sessions with no turns at all. The fallback is the user's own messages, with aish's `[aish: …]` notes excluded via `synthetic_kind` (they never reached the transcript live, and one landing mid-turn would split it). **The choice is made per FILE, never per record**: mixing both boundaries in one log would double-count every web turn.

Note the name collision the log itself warns about (`docs/session-log.md`): `message` records carry a top-level `turn` that is a client-minted **string** event id, unrelated to the int counter on trace steps. `Turn.ordinal` (position in the file) and `Turn.counter` (the agent's int id) are both reported, because reopening a chat restarts the counter and they genuinely differ.

---

## Reading it

```sh
aish explain <session-name-or-path> [turn]     # one turn, or all of them
aish explain 20260814-131203 2 --tools         # …plus every tool description and schema
aish explain 20260814-131203 2 --context       # …plus the system text as the model got it
```

`TestSubcommand` drives this through `main()` rather than calling the reader directly, so the argv wiring is covered too. An ambiguous name **lists candidates and stops**. Guessing would produce a confident diagnosis about the wrong chat, which is worse than no answer.

Verdicts are grouped by outcome rather than printed in file order — bound, unevaluable, abstained, skipped — because the groups route to different repairs (#197) and a 24-rule corpus in file order buried the one abstention that mattered. **Allowed gates are collapsed to a count**: there is one per always-rule per call, and printing them all buries the refusal, which is the only reason anyone opened the file. **Verify verdicts are their own section** — they are about the turn's answer and carry no call id, so filing them under the tool-call join read like a broken join when nothing was broken.

---

## Reasoning and the arguments (#240)

Two renderless records, both written per model call, both emitted from the ONE point every path goes through — the tool-call path, the text-only path and the final no-tools turn alike, so no caller can forget one.

**`reasoning`** carries what the model produced: the full thinking text, what it said on that call, the provider's stop reason, whether the arguments of any tool call failed to parse, and the *types* of the provider-native blocks it returned. Block **types only** — that is what reveals a provider-redacted thinking block without storing a second copy of the turn.

Before this, the rendered `thinking` step kept a 120-character snippet for a live status ticker, and it averaged **26 characters** across a real month of logs. That fragment was the only durable record of how the model decided. It stays exactly as it was: the full text hangs off a renderless record instead, because the rendered step crosses the wire to a live client and lands in replay, and a quarter-megabyte of reasoning would go into both.

**`call`** carries the tool call as the model emitted it: name and the exact argument dict. The rendered step keeps `summary`, a human label built per tool — the query for a search, the path for an edit — so *"it called the tool, but with arguments that made it fail"* was invisible for every tool except `run_command`, whose command already survives in the audit line. It is emitted **before** the call runs, so a call that then crashes still has its arguments recorded, and the reader reports such a call as *never completed*.

**Two caps, both named constants** (contract §8.5, and a truncated record says which cap cut it):

- `REASONING_CHARS` = 262144 characters, i.e. 256 KB — a backstop, not a limit. A turn cannot generate more than `num_ctx` tokens, so at 32k context this is unreachable by roughly 2×. It exists for a pathological loop or a much larger future window.
- `CALL_ARG_CHARS` = 8000 characters — applied **per argument value**, never to the whole JSON, so a large `content` on a write can't push the `path` beside it out of the record.

**Malformed arguments are a fact about the model's output, not about execution.** A JSON decode failure becomes `{}` inside the backend adapter, which downstream is indistinguishable from a call the model deliberately made with no arguments — and the two route to completely different repairs. The raw string dies in the adapter, so the flag is set there and reported on the `reasoning` record for the call that emitted it.

**A synthesized sentence is marked.** The Anthropic path fabricates *"(the model declined this request for safety reasons)"* on a refusal and logs it as model content; unmarked, a dossier credits the harness's own words to the model.

---

## The channels that could make a reader wrong (#241)

Everything above is about recording what happened. These four are different in kind: each let a reader reach a **confident false conclusion**, which is worse than the gap it replaced. `TestCoverageHoles`.

**The third trim site.** Two trim policies run at task boundaries and have been recorded since #192. A third — `_enforce_budget` — fires **mid-task** and recorded nothing at all, so a result the model read at step 2 could be a 200-character stub by step 7 with no trace of when or why. This is the sharpest case in the whole file: the transcript still holds the **full** text, so the log did not merely omit the truncation, it positively suggested the model had something it never got. Now recorded as `mid_task_budget`.

**Which results were stubbed.** `affected: 3` said something had been cut but never what. All three sites now record `stubbed: [{at, tool}]`, capped by `TRIM_STUBBED_MAX` = 40 with the overflow counted. A log written before this has `affected` and no `stubbed`, and the reader says *which messages: not recorded* — never "nothing was stubbed".

**Steering typed mid-task.** Text typed while a task runs is folded into the model's messages **without passing through the recorder**, and it is not restored when a session resumes. The rendered `injected` trace step is the only place it exists, so the dossier reads it from there. (Note for anyone adding a renderless kind: that name is already taken by this rendered step — 36 instances in the live corpus — and moving it into the renderless registry would make every one of them vanish from cold replay while still rendering live.)

**The reminder is not a system message everywhere.** On the OpenAI-shaped backends, aish's second system message — the per-task reminder carrying the knowledge index, the preloaded skills and the rule prose — is relabelled `user`, because Gemini's compat gateway drops *all* system instructions when more than one is present (#74). A dossier claiming a system-authority instruction was in force would be describing something the model never saw.

`backends.system_role_policy()` **declares** this per provider and the brief records the declared value, rather than leaving a reader to infer it from the converter's source (§0). Declaring and doing are pinned together by `test_declared_system_policy_matches_the_code` — a declared policy that drifts from the code is not a missing record, it is a confident lie. An unknown provider inherits `first_only`, because it goes through the same converter; defaulting to the safe-sounding value would be the wrong way to be wrong.

---

## One assembly, two renderers (#243)

`render` prints for a terminal; the web panel draws DOM. If each walked the records itself there would be two implementations of *"what does this log say"*, and they would disagree about **absence** first — which is this reader's entire subject. So the walk happens once, in `dossier()`, and produces plain JSON-serialisable data that both renderers consume. `render` is a dumb renderer over it, and the endpoint serialises it.

The three states are machine values on the data (`RECORDED` / `MISSING` / `EMPTY` / `PURGED`, plus `FRAGMENTS` for a log written before the full reasoning record, whose rendered `thinking` step kept a snippet). A snippet shown as "the reasoning" is how someone concludes the model barely thought about it, so it says which one it is rather than presenting one as the other.

`TestDossier` pins that the whole document serialises, that resolved and purged bytes stay distinguishable, and that a log predating the brief reports `not_recorded` rather than a carried-forward one.

## Worth a look — facts, never causes

A turn with a two-dozen-rule corpus produces dozens of verdicts, and on a phone the line that matters is buried. `notes()` ranks what is worth reading first. Four properties make that safe, and each is a test:

- **Rows are observations.** "read_url returned 403", never "the 403 is why it improvised". A confident wrong cause wearing evidence styling is the failure this whole feature exists to end.
- **Every row cites where it came from** — section, and call or model call — so it is a shortcut into the evidence rather than a substitute for it.
- **The empty state names the checks it ran.** *"Nothing unusual in this turn"* is a claim a checker is not entitled to make: it knows only the classes someone coded, so on the one turn whose cause is an uncoded class it would state the opposite of the truth, above the evidence.
- **It is a pure function of the `Dossier`**, not of the log, so the terminal and the panel surface the same list and neither re-derives it.

Rows are collapsed per outcome rather than per rule — ten verify verdicts are one row naming the rules, not ten rows — because a list as long as the evidence is not a shortcut. `TestWorthALook` pins the citation, the collapse and the empty state.

## What is still missing

The reader reports each of these as *not recorded* rather than guessing:

- **attachment guidance** — see below; it is the last piece of the brief that is still unstored.
- **attachment guidance** (#241) — the sentences telling the model what it may do with each attached file are still built at hand-over and never stored.
- **consumed vs sent** (#243) — the prompt-token count is on the `reasoning` record and `num_ctx` on the `brief`, so a context that filled up is derivable, but nothing flags it. That belongs in the suspicion list, not in another record.
- **a web view** (#243) — `aish explain` is CLI-only. The contract's §8.7 already says a UI needs a deliberate new endpoint, because none of this is client-side and the offline mirror must not start caching reasoning onto every device.
- **claude-max** (#242) — its SDK loop emits none of these records and drops thinking blocks entirely. A dossier for one of its turns must say so wholesale rather than assembling something half-plausible.
