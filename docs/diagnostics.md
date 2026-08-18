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

**Written only when the digest changes.** An ordinary session logs one brief; a session whose capabilities moved logs one per move, naming the call it moved at. That is the interning rule — *present at or before first reference*, not "exactly once", because the log is not append-only (below). `TestBriefWriter` pins both halves of renderlessness, the per-call grain, and replay byte-identity.

---

## The evidence store

`evidence.py`: content-addressed bytes under `<state_dir>/evidence/<aa>/<sha256>`. The log holds names and digests; the bytes live once.

Three reasons the bytes are **not** in the log, in order of importance:

1. **Erasability.** The log records injected knowledge by name only, and the contract says so explicitly — it is the one text an audit record must not duplicate (§3.10). Verbatim bodies in every log would break that: a secret pasted once and then remembered would be written into every later session that preloads that memory, so redacting the original turn would leave copies elsewhere forever, and `forget_memory` would stop being a deletion. `purge(digest)` drops the bytes once, everywhere, and every reader then honestly says *purged*.
2. **The log is not append-only.** `redact_turn` and `rewind_last_turn` — the latter fires on **every web Retry**, a daily action — both rewrite it in place. A blob written inside one turn can be deleted while later turns still reference it. Blobs outside the rewritten file cannot be lost that way.
3. **Deduplication.** The menu is ~31 KB and near-constant, so one copy serves hundreds of sessions.

**A blob is re-hashed on read.** A file that does not hash to its own name has been truncated or tampered with, and handing it back would let a reader quote evidence that is not what was recorded — so it reads as absent instead. **`put` with no state dir still returns the digest**, so a caller with nowhere to write records a reference the reader reports as unresolvable rather than as never recorded.

`TestEvidenceStore` pins the round trip, the single-copy guarantee, purging, and the tamper check.

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
```

`TestSubcommand` drives this through `main()` rather than calling the reader directly, so the argv wiring is covered too. An ambiguous name **lists candidates and stops**. Guessing would produce a confident diagnosis about the wrong chat, which is worse than no answer.

Verdicts are grouped by outcome rather than printed in file order — bound, unevaluable, abstained, skipped — because the groups route to different repairs (#197) and a 24-rule corpus in file order buried the one abstention that mattered. **Allowed gates are collapsed to a count**: there is one per always-rule per call, and printing them all buries the refusal, which is the only reason anyone opened the file. **Verify verdicts are their own section** — they are about the turn's answer and carry no call id, so filing them under the tool-call join read like a broken join when nothing was broken.

---

## What is still missing

The reader reports each of these as *not recorded* rather than guessing:

- **the rest of the brief** (#239) — the system prompt, and the injected knowledge and rule text. The per-turn records name what was injected; the bytes are not stored yet.
- **reasoning** (#240) — a 26-character fragment of the model's thinking is kept for a status ticker; the full text is received and discarded. Also the tool arguments as the model emitted them, which the message serializer drops.
- **coverage holes** (#241) — a third trim site that records nothing, steering text typed mid-task, and the backend adapter that demotes system authority to a user message on some providers.
- **claude-max** (#242) — its SDK loop emits none of these records and drops thinking blocks entirely. A dossier for one of its turns must say so wholesale rather than assembling something half-plausible.
