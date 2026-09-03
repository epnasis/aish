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

**The second amendment (#352 slice 1): the rendered `tool_start` and `tool` steps carry `model_call` too.** The renderless `call` record has named its issuing model call since #243, and that is enough for a reader over the file — but the browser never sees `call`. The trace card is built from the rendered steps alone, live and on replay, so folding its flat timeline into rounds meant counting rows: the k-th "Thought for" row is model call k *provided* every call that emitted a tool also emitted a thinking row, which is a property of the loop's shape and not a join. The stamp is passed into `_emit_tool_step` from `_call_result` exactly as the `call` record's is — captured where the model call happens, never read off the agent — and it is **omitted, never 0**, when no recorded model call issued the call, which is the claude-max path (#242). The two exception paths that emit their own `tool` step carry it as well, because a crashed call is the one a reader opens the record for. `thinking` is still untouched. `TestTurnAndCallIdentity.test_tool_steps_name_the_model_call_that_issued_them` and its two neighbours.

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
2. **The log is not append-only.** `redact_turn` rewrites it in place and takes records out, so a blob written inside one turn can be deleted while later turns still reference it. Blobs outside the rewritten file cannot be lost that way. **This used to name web Retry as well, and no longer may:** `rewind_last_turn` deleted the discarded turn and was replaced by `supersede_last_turn` (#339), which rewrites in place but removes nothing. What Retry still settles is what a blob's lifetime may NOT follow — `session._is_superseded` states the reader law as *a superseded record is read by evidence, and by nothing that reconstructs the chat's current state*, so a blob whose turn has been discarded is precisely one `aish explain` is expected to resolve. The digest is the lifetime; `purge` is what ends it.
3. **Deduplication.** The menu is ~31 KB and near-constant, so one copy serves hundreds of sessions.

**A blob is re-hashed on read.** A file that does not hash to its own name has been truncated or tampered with, and handing it back would let a reader quote evidence that is not what was recorded — so it reads as absent instead. **`put` with no state dir still returns the digest**, so a caller with nowhere to write records a reference the reader reports as unresolvable rather than as never recorded.

`TestEvidenceStore` pins the round trip, the single-copy guarantee, purging, and the tamper check; `TestReasoningCapture` and `TestEmittedArguments` pin the two #240 records, including that the default cap cannot bite at the default context window.

This is **not a second log.** It has no records, no ordering and no clock; the digest is the entire join. The argument against a parallel debug log — two files correlated by timestamp, which is the positional correlation the trace contract exists to kill — does not apply to something joined by content address.

### Bytes beside their chat — the per-chat store (#352)

`turns.py`: `<state_dir>/turns/<chat>/<sha256>`, one directory per chat, holding every blob that chat's `sent` records reference (contract §3.12): each provider message as sent, the tools payload, a hoisted system text. Same discipline as `evidence.py` — dumb, text-only by construction, `put` returns the digest even with nowhere to write, `get` re-hashes and answers absent on a mismatch — and two deliberate differences.

**Content-addressed WITHIN the chat, never across chats.** The history a chat repeats across its model calls is on disk once, so recording every call costs about what the chat's messages cost; a second chat quoting the same bytes gets its own copy. That is the erasability argument as it now stands, with fewer moving parts than the shared store's: a pasted secret lives in exactly one directory rather than in a deduplicated store reachable from every chat's manifest. **Deleting a chat deletes its directory** (`server._delete_session`, the CLI's `/delete`), and **`redact_turn` unlinks the digests its removed records referenced, minus those a surviving record of the same chat still references** — a set difference the within-chat addressing makes exact (`docs/session-log.md`). The shared `evidence/` store keeps the brief, unchanged, because the menu and the standing prompt are the same bytes in hundreds of chats and one copy is the point there.

**Bounded by chat, never by step.** No blob has a cap — *whole, never cut* (owner, 2026-09-02). `turns.sweep` runs at server start and after a turn ends, never inside a tool call, and only when the tree exceeds `TURNS_BUDGET_BYTES` (2 GiB) does it evict — whole chats, oldest by the newest blob's mtime, until the tree fits. Sized against the corpus on 2026-09-03: 818 logs, 119 MB, 43.3 M chars of message content over 1,100 tasks; had every call ever been recorded here the tree would hold ~150 MB, so the budget is roughly a decade of this owner's use at today's rate on a disk with 1.5 TB free. Large enough that eviction is an exception a reader can name, small enough that a runaway chat cannot fill the disk. An evicted chat keeps a dated tombstone (`.evicted`, INSIDE the directory: a chat is reopened long after it was active and a request recorded then needs somewhere to land without erasing the fact), and the reader says *evidence for this chat was evicted on <date>*.

**The reader's states**, per blob, are therefore six and not three: *recorded · empty · not recorded · evicted (date) · purged · never stored*. `_sent` resolves them; `aish explain … --context` lists the request message by message in the provider's role, and the web dossier carries the same list with a `sig` per digest for `/evidence` (`docs/web-server.md`). *Never stored* is the media state: a picture or a document travels as base64 only inside the adapter, the manifest names the file and its size, and the blob holds a placeholder where the base64 was. The reader's import fence is unchanged — `turns` is stat-and-read, as inert as `evidence`.

**What it captures for free**: every tool's model-facing output, because what the model got back from a tool is a message in the next request. Part 3 of #352 (the whole bytes before a cut) joins to it rather than copying it. `TestSentRecord`, `TestPerChatStore`, `TestEviction`, `TestRedactionUnlinksEvidence`.

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

## The picture of a driven page (#289 slice 1)

aish drives real pages the owner cannot see, so *"it said the portal had no invoices"* has never been checkable. A browse call now records `frame` — a path into the evidence-frame store holding a picture of the page as the model was **shown** it — and the dossier resolves it: `aish explain` prints the path, and the web trace card draws it.

**This reader's three-states rule applies unchanged, and the third state is the ordinary one.** The bytes live in a bounded LRU cache, so a reference outliving its picture is how most frames end, not a fault:

| what the call recorded | what the reader says |
|---|---|
| no `frame` key at all | nothing — a log written before frames existed, or a tool that never had a page |
| `frame`, and the file resolves | the path |
| `frame`, and the file is gone | the path, marked **purged** |
| `frame_skipped` | why no picture was taken: `hands`, `failed` — or, in a log written before #320, `password` or `unknown` |

Those are why `frame_skipped` exists at all, and they route to different reactions. *The owner's own hands were on the browser* is the system working. *The capture failed* is worth investigating. A blank cell would present both as the same thing, and as the same thing again as a step that recorded nothing.

**Two of the words are readable here and are no longer written** (#320). *A password box was on the page* used to refuse the capture, on the argument that a screenshot of a login form is the artifact that must not exist — but aish never types a password on the browse path, so the refused frame was an EMPTY form, and the refusal cost the one picture the owner most needed when a sign-in failed. *The page would not say whether there was one* existed only to resolve that question safely and went with it. This reader still renders both, because they are in logs already on disk and a fact a reader cannot render is a fact it has erased. `docs/browser.md` carries the argument.

The bytes are deliberately NOT in the evidence store, which is text-only by construction (`digest_of` hashes UTF-8, `put` writes with `write_text`), and — since #318 — deliberately not in the media store either, because that one is inside `workspace_roots` and a frame the MODEL can read is an input channel rather than a record. `docs/browser.md` carries the rest of that argument. What matters here is that the governance property is identical either way: **the record only points at the bytes, and the bytes are purgeable on their own schedule.** `_frame_of` is where the resolution happens, and it stats a path rather than importing anything live — the reader's import fence is unchanged.

**A frame proves what happened and prevents nothing.** It is a record, which #295 counts as a real control — but for anything irreversible a record is detection, not protection, so nothing in the gates may ever be relaxed on the grounds that a picture will exist. `TestTheEvidenceFrameOnTheRecord`.

### What the picture is evidence OF, and what the page said (#320's follow-up)

A path on its own answers *what did this page look like*. The question actually asked of a browse step is *what did this press do*, and three more recorded fields answer it. All three are read back verbatim: this reader assembles from evidence and may never re-derive any of them.

- **`frame_url` / `frame_from`** — the address the shutter fired at, and the address the page came from when the action moved it. Printed as one caption under the path. `frame_from` absent beside a present `frame_url` is the positive statement *the address did not change*, not a gap; **both keys absent is the third state again** and grows no caption, because inventing *unknown* would be this reader claiming the writer tried.
- **`console`** — what the PAGE wrote to its own console during the action. A whole day of eon.pl diagnosis argued four causes off page text and a badge, all four wrong, with nothing recorded from the page itself to argue against. It is **page-authored**, so `_console_lines` labels every block *the page's words* — a dossier is read by a person and can be pasted to a model, and this is the last place outside content may pass for aish's own account of itself. **A line here is evidence, never a verdict**: a press that never landed writes nothing at all (that witness is the driver, #321), and a site can throw a real error that has nothing to do with the failure being investigated — eon.pl throws exactly one. `docs/browser.md` states the boundary.
- **`signin`** — the automatic sign-in that happened inside the call, on a page of its own: host, its own picture under the same three states, and its own console. Kept apart from `frame` on purpose, because the model is never shown the sign-in page and a reader that folded the two together would be claiming it was. It is rendered as *aish ATTEMPTED an automatic sign-in* by both readers, and that is the reader law rather than a wording choice: the block's `host` is written whenever an attempt happened, success or failure, so the line it used to carry — *aish signed in again at eon.pl* — asserted an outcome no field in the record holds. The outcome lives on `signin.Record` and in the note the model was given; a dossier may not re-derive it from an attempt's presence. **The verdict and its observations are read back here** (#325) — see the section below, which is where the words for them live.


### Why a sign-in failed, in words, from the record alone (#325)

The `signin` block carries the token the failure table returned and the five observations it was composed from. Those are the right things to STORE and the wrong things to hand a person, and for a day they were *recorded and unreadable*: `_signin_of` whitelisted the keys it resolved, so both were dropped on the way into the dossier and the one tool built to answer *why did that sign-in fail* still could not. A named gap is a work item, not a documented limitation.

**The wording is the risk in this feature, not the plumbing.** `FAILED_CAPTCHA` was a token once, and the sentence a renderer gave it told the owner for weeks that reCAPTCHA had refused a sign-in that was never submitted. So the four sentences are pinned by test, each names aish as the observer, and none names a cause:

| token | what the dossier prints |
|---|---|
| `refused` | *aish read this as the site refusing the saved password* |
| `contradiction` | *aish's two observers disagreed about this attempt, and aish did not resolve it* |
| `never_sent` | *aish did not recognise anything carrying the password leaving the page* |
| `unexplained` | *aish does not know why this attempt did not get in* |

`never_sent` says *did not recognise*, with the matcher as the subject, because the observation under it is an absence from a matcher with declared blind spots — *the password was never sent* would be the confident-absence claim #295 removed from the owner-facing sentence, rebuilt in the reader. `contradiction` blames neither the site nor the password: one of the two observers is wrong, nothing can say which, and a reader may not resolve what the record deliberately left open. `unexplained` is allowed to say plainly that aish does not know, which is the ordinary ending this whole area exists to keep sayable.

**All five observations print, true and false alike.** A group whose observations are all false is a positive set of things aish looked for and did not see; printing only the true ones would render it identically to an attempt that recorded none, and those two route to different repairs. That is the same reason the writer records them unconditionally (trace contract §3.4.1) — a reader that re-imposed the collapse would have undone it at the last step.

**The third state is said out loud rather than left blank.** An attempt with no group at all means the failure table did not run — a second factor, a covered button, a refusal before the submit — OR the log predates the record. The record cannot separate those, so neither does this: on an attempt whose session did not come up it prints *no verdict was recorded for this attempt — either it ended before aish judged it, or this log predates the record*, and on one that worked, or on a log with no outcome recorded at all, it says nothing about a verdict. A token that arrives with no observations under it is read as no judgement, not as half of one (§4).

**The words live in the ASSEMBLY, not in a renderer.** `SIGNIN_VERDICT_WORDS` and `SIGNIN_OBSERVED_WORDS` are consumed by `_signin_of`, which puts `verdict_said` and `observed_said` into the document — so `render` and any panel print the same sentence and no second author has to word a token correctly twice. That is #243's one-assembly rule doing the work it exists for, and it is the answer to the two-renderers problem this feature left open. It differs deliberately from `SS_GROUPING_WORDS`, which is a JS-side word map for a Python-side enum: there the cost of drift is a confusing caption, here it is a cause asserted in aish's voice.

**And the reader still cannot reach the table.** The words are keyed by token, checked against `signin.FAILURE_VERDICTS` by a test rather than by an import — `signin` joined the import fence's forbidden list with this change, because a reader that could import it could explain a recorded verdict by re-running the rule that produced it, which is §0's whole prohibition. A token this reader has no words for is rendered verbatim with *a verdict this reader has no words for* beside it, and an unrecognised key inside `observed` prints as `name: value`: retiring an entry retires the writer, never the reading of it, and a whitelist here is what caused this gap in the first place.

`TestTheConsoleAndTheSignInOnTheRecord`. The capture side, the caps and the banner discipline are `docs/browser.md`.

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

**The brief is carried forward.** It is written only when what the model was handed *changes*, so most turns have none of their own — and a panel that rendered only its own turn's brief would show an empty "what it was given" on nearly every turn, which is the one screen this feature exists for. `_brief_in_force` walks back to the most recent one. Carried forward is **not** the same as written here and the two stay distinguishable (`written_here`, `in_force_since`): a reader who cannot tell them apart concludes "the tools changed at turn 7" off a record that only says they had not changed since turn 3.

`TestDossier` pins that the whole document serialises, that resolved and purged bytes stay distinguishable, that a log predating the brief reports `not_recorded` rather than a carried-forward one, and that the carried case is labelled.

## The flow — a turn in the order it happened

Organised by RECORD KIND, a dossier cannot answer *"what did it think after it got that result"*, which is the question people open one to ask. The owner put it plainly after the first day of use: *"I don't know which information was retrieved after which tool."* Kind is how a **file** is organised; it is not how a turn is.

So both renderers now tell one story in three parts, matching the shape a turn actually has:

1. **Before it started** — the brief in force, the knowledge and rules seeded, and any trim that fired at task seed.
2. **What happened** — **rounds**. Round N is model call N's reasoning, the tool calls it issued, their arguments, the verdicts that governed them and what came back. Then round N+1.
3. **What it answered** — the answer, the delivering pass's verify verdicts, the task status.

**Rounds reference `thought` and `did` by id and never copy them.** Tool output is the bulk of the payload and this document is fetched to a phone.

**Between-round events ride here**, because they are exactly the "what changed between two thoughts" facts a kind-sliced dossier hid: text typed while the task ran, a `mid_task_budget` trim that stubbed a result the model had already read, and a change to what the model was being handed. They are keyed by the last **completed** model call, never the next one — both are written before a call that a cancel can stop from happening, and an event naming a round that never occurred is a lie. A seed-time trim is not an event in the flow and stays in part 1: putting it in a round would assert a causality that is false.

### How a call finds its round, and the three answers

`model_call` is recorded on the `call` record, **captured where the model call happens and passed as an argument** into `_call_result`. Reading `self._model_call` inside the executor happens to be correct today, but only because nothing calls the model between issuing a batch and running it — a property of the loop's shape rather than a join, and the contract's whole posture is that a join must not rest on emit order.

`grouping` is a per-**turn** machine state, because one session can hold both kinds of turn:

| state | when | what the reader shows |
|---|---|---|
| `recorded` | every call names its model call | the rounds, unqualified |
| `inferred` | no stamp, but there is reasoning to attach to | the rounds, **labelled as inferred** |
| `none` | nothing recorded issues a call — claude-max (#242) | no rounds at all; the calls listed flat |

The `inferred` fallback attaches each call to the most recent preceding `reasoning` record in file order. That is *correct* for files this code wrote — every one of these records is emitted from the main thread in sequence, including the parallel batch, whose `call` records are written at collection — but it is an inference about attachment, and a reader must never be shown inference wearing a record's clothes.

`none` is why the fallback is per turn: claude-max routes SDK tool calls straight into `_call_result` without ever entering the model loop, so nothing recorded issued them. Its calls are listed flat rather than folded into round 1, and the stamp is **omitted** rather than written as `0` — absence says "this backend records no model calls", a zero would say "round zero", and those route to different repairs.

`TestTheFlow` pins the grouping, the by-id references, the labelled fallback, the claude-max shape, and both sides of the seed-versus-mid-task trim split.

## The step list — the same turn as a ledger (#352 slice 1)

The web step screen walks a turn forward and back, one exchange at a time, so the dossier carries the turn a second way: `steps`, an ordered list in which every entry is a model call, a tool call, or something that happened between two of them (`trim`, `steering`, `brief_changed`, `model_error`, `retry`). It is not a second walk. `_place_calls` is the ONE placement of a tool call under its model call, shared with `_rounds`, so the flow and the ledger cannot file a call in different rounds; and `_link_events` hands every flow event the id of its step, so a note computed off the flow cites the exact step.

A step carries its `kind`, an `id` (`m<model_call>`, `c<call>`, or a per-kind counter for events), `n` of M, the `panes` it has — `context`/`response` for a model call, `call`/`result` for a tool call plus `page` **only where page evidence was recorded**, one `event` pane for the rest — the short `facts` for its strip, and `ref`erences by id into `thought`, `did`, `context_cost` and `messages`. **Never a copy**: tool output is the bulk of the payload and the document is fetched to a phone.

Three inferences live here, and each is said on the step rather than worn as a record:

- **`placement`** on a tool call is `recorded` when the `call` record named its model call, `inferred` when file order attached it (the same fallback `_rounds` labels), and `none` when nothing recorded issued it — claude-max, or a `tool` step from before call ids, which is placed positionally and says so.
- **`numbering`** on a model call is `inferred` for a log that kept only the rendered `thinking` fragment (the `FRAGMENTS` state): those are still model calls, numbered by order, with the snippet on the step as `fragment`.
- **`ref.shown_how`** on a tool call says where its model-facing text came from. `step_output` and `step_error` are on the `tool` step and joined by id. `message_by_order` and `message_by_name` are joins to a tool-role `message` record — which carries the tool's name and the model call it was first in front of, and **no call id** — so a message is matched to the calls of its round by name and order, and only where that is unambiguous; anything else is `not_matched`, because a wrong text presented as *what the model saw* is precisely the lie this reader exists to prevent. Slice 2's `sent` record makes this join exact.

**`messages`** is this turn's message records, text included once, each with its `model_call` stamp as recorded (None where the writer wrote none). A model call's `context` block is computed from the stamps — which messages were `new` to it (stamped with the previous call), which were `in_front` of it, how many are `unstamped` and cannot be placed, which results a trim `stubbed` between the previous call and this one, and whether the brief changed at it — and is labelled `source: reconstructed`, because it is built from records and the brief rather than from the request that left aish. `context_cost.calls[].by_where` gives the per-side totals (carried from earlier turns, this turn, the system text, the tool menu) for the same call, kept when the parts list is popped.

The `retry` that opened a rerun sits at the END of the attempt it discarded (`docs/session-log.md`), so the rerun reads it off the previous turn and it is the rerun's first step — the same reader rule the trace card follows.

Every note now cites a `step` and a `pane` in its `where`: a row about a call cites that call's step and the pane it was computed from (`NOTE_PANES` — a failed result lands on the result, a refused gate on the call); a row about the turn's beginning lands on the first model call's context, one about its end on the last model call's response, which is where the brief and the answer live on the step screen. `TestTheStepList`.

## Worth a look — facts, never causes

A turn with a two-dozen-rule corpus produces dozens of verdicts, and on a phone the line that matters is buried. `notes()` ranks what is worth reading first. Four properties make that safe, and each is a test:

- **Rows are observations.** "read_url returned 403", never "the 403 is why it improvised". A confident wrong cause wearing evidence styling is the failure this whole feature exists to end.
- **Every row cites where it came from** — section, and call or model call — so it is a shortcut into the evidence rather than a substitute for it. Rows about between-round events are read off the **flow** rather than off the raw records, so each one names the round it happened before; citing only the section lands the reader on a header, which is indistinguishable from the tap doing nothing.
- **The empty state names the checks it ran.** *"Nothing unusual in this turn"* is a claim a checker is not entitled to make: it knows only the classes someone coded, so on the one turn whose cause is an uncoded class it would state the opposite of the truth, above the evidence.
- **It is a pure function of the `Dossier`**, not of the log, so the terminal and the panel surface the same list and neither re-derives it.
- **Every number it compares against is one the writer recorded.** The context-fullness check first compared prompt tokens against `num_ctx` — an Ollama option carried on every turn whatever the backend — and so announced that a Gemini turn sitting at 5% of its million-token window was nearly full. A confident wrong claim at the top of the evidence is the one thing this pass must never produce. The brief now records the window actually in force and its provenance (`backends.context_window`), the reader compares against that, and for a log written before it existed `num_ctx` is trusted **only** on Ollama, where it genuinely is the window. Anywhere else the reader abstains rather than guessing.

**A cut result is two checks, not one (#274).** #269 made a page cut recoverable — the model is handed a continuation key and told to page through. That turns one failure into two, and until now the reader could see neither, because a page cut wrote no `truncation` block at all: `web` truncated inside itself and returned a plain string, so the whole thing was a blank row. `result_cut` is the first shape — cut, with **no** continuation offered — which is a missing capability, and is exactly what the session behind #269 hit. `continuation_unread` is the second — cut, a continuation offered, and **nothing read it back in this turn** — which is a choice, and is the shape to expect from here, because the fix for the first one is a sentence the model has to decide to act on. The contract has named them different incident classes with different repairs since it was written; separating them in the reader is what makes the distinction usable.

Three things keep those rows honest. The read-back set is collected across the **whole turn** before any row is emitted, because the paging call comes after the call it continues and often several calls after — matching against the next call would flag every recovered cut. The row says *"in this turn"* out loud, because the dossier is per turn and a model that pages on its next turn is not the incident this is looking for; the check states what it can see and does not extrapolate. And a cut that WAS read back gets no row at all — that is the machinery working, and flagging it would bury the two that matter. `TestExplainCanTellTheTwoIncidentsApart`, `TestTheDossierShowsIt`.

Neither row is page-specific. `tool_plugins` has written this same block since #192 and **no reader had ever looked at it** — so the first consumer of `truncation` arrived two phases after the first producer, which is its own small lesson about recording a field nothing reads.

`scripts/verify_page_cut_trace.py` drives all three outcomes into real session logs and reads them back with the real `explain` — cut-and-ignored, cut-and-paged, cut-with-nowhere-to-cache — because the unit tests build dossiers by hand and cannot answer the question the phase exists for: can a person, days later, point `aish explain` at a session and be told what was cut and whether the rest was ever read? It is outside the pytest suite (it writes session logs and renders the terminal view), and it has to blank `GLOBAL_RULES_DIR` and friends first: they are module constants with no env override (#254), and the first run of it silently drove the owner's live corpus, where a rule refuses `browse` outright — the check passed on a turn that never happened.

**Only a NEAR abstention is worth a look.** The rule corpus is evaluated in full against every turn, so *"this rule did not apply"* is the ordinary case for almost all of them — on the first live turn this ran against, six rows of that sat above the two real findings. A near miss (`ABSTAIN_NEAR_FLOOR`) is a different thing: it is the shape of *"the rule you wrote did not fire and you expected it to"*.

Rows are collapsed per outcome rather than per rule — ten verify verdicts are one row naming the rules, not ten rows — because a list as long as the evidence is not a shortcut. `TestWorthALook` pins the citation, the collapse and the empty state. The web half of all this is `docs/web-server.md`'s `/explain`, tested by `TestExplainEndpoint`.

## What is still missing

The reader reports each of these as *not recorded* rather than guessing:

- **attachment guidance** (#241) — the sentences telling the model what it may do with each attached file are still built at hand-over and never stored. The last piece of the brief that is unstored.
- **claude-max** (#242) — its SDK loop emits no `brief`, no `reasoning` and no `call` records, and drops thinking blocks entirely. Its turns already report `grouping: none` and list their calls flat rather than inventing a round. **The whole-turn statement now exists for the request side**: each task writes one `sent{coverage: "sdk"}` and the reader says once that this backend sends its own requests (`docs/agent-core.md` has the finding). The same single sentence for the brief, the reasoning and the calls is still owed.
- **the brief diff** (#243 slice B) — *"it worked Tuesday, what changed?"*. Two turns from any two sessions compared by digest: tools added or removed, a description that changed, different knowledge injected, different rules bound. Nearly free, since the digests are already recorded, and a more common shape of the question than "explain this turn".
- **the raw records section** — served by the endpoint (`raw=1`, capped and reporting what it elided) and not yet drawn in the panel. A rendering that cannot be checked against its own source is a narrative.
- **re-measure** — `aish-curate --context` recorded the before picture (`docs/knowledge-layer.md`). The same numbers a few weeks after the history-policy change are what settle whether it worked; nothing in the claim is safe to assume.

Closed since this doc was written: the web view and the flow view (#243 slice A), the system text (#239), recoverable trims, page cuts and the two incident classes they split into (#274), and consumed-vs-sent — which is now the `context_full` check, measured against the window **recorded** as in force rather than against `num_ctx`.
