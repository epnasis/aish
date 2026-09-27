# The Objective — what a chat is FOR

`objective.py`, `aish/charters/distiller.md`, `scripts/measure_objective.py`. Epic #423;
this page covers slice 1, #424: **the record and the distiller, recorded only.** The
binding owner decisions D1–D9 are in the epic and are not repeated here.

> **What runs today.** At the end of every task, off the interactive path, aish writes
> one `role` record and, usually, one `objective` record to the chat's log. **Nothing
> reads them back.** The model is not shown the Objective, the screen does not draw it,
> the trimmer does not consult it, and no chat is titled from it — those are #425
> (strip, pivots, owner edits), #426 (the trimmer's licence and the reminder) and later.
> A doc implying any of that runs is the defect `docs/roles.md` records at length.

## Why it exists

aish protects only the latest task prompt. In a long chat the goal is spread over many
owner messages, often in Polish, and is never stated as one thing. The #422 chat
(`session-20260925-204008-294943`) is the evidence: a reusable skill comparing forecast
providers against reality for next-day rain and temperature, keys in aish secrets,
charts via `show_image` — assembled across tasks 1–12, followed by tasks that are "tak",
"and?", "Continue", "I?". Anchoring on the first message loses it; anchoring on the
latest loses it too. **Whether that chat stalled from goal loss, from #422, or both is
not established**, and nothing on this page establishes it.

## The three parts

**The material** (`objective.material`). The chat's LIVE records (L7 — a discarded Retry
attempt is not something a goal can cite), grouped into tasks, each item given a ref and
the task's contract-§2 turn:

| kind | from | ref |
|---|---|---|
| `owner` | a typed user message — anything starting `[` is aish's own note (L4) and is not | `m:<id>` |
| `comment` | the sentence he typed on a card he denied or held | `t<N>.c<M>` |
| `denial` | a denied or held action with no sentence | `t<N>.c<M>` |
| `answer` | the task's final, non-interim assistant message, cut at 1 200 chars | `m:<id>` |
| `cancel` | a final answer that is `STOPPED_ANSWER` | `m:<id>` |
| `action` | a `tool` step with `ok: true` for a tool outside `READ_ONLY_TOOLS` | `t<N>.c<M>` |
| `failed` | a `task_end` that recorded a failure | `t<N>.end` |

**Never tool outputs, never reminders.** A card comment is included although the epic
lists "owner messages": it is his own words, and in the #422 chat the clearest statement
of the goal is one — *"The goal is not to compare the weather, but to check for the rain
in 24 hours…"*, typed on a held `create_skill` card at turn 25.

A task's turn is the first integer `turn` stamped inside its bracket, excluding the
distill's own `role`/`objective` records: a slow distill of turn N lands inside task
N+1, stamped N, and must not make task N+1 turn N. A log with no brackets (the CLI writes
none) is grouped by typed messages that are a model call's first input. `TestMaterial`.

**The distiller** (`aish/charters/distiller.md`). The first role with a live caller — see
`docs/roles.md`. It is handed ONE JSON input: the previous revision in a model-facing
form (cites as bare refs, `owner_set` naming the fields he set), `earlier` (the items the
previous revision cites, so a carried cite still resolves), and `new` (everything since).
It answers the whole ledger; `objective.validate_answer` checks it against that same
input — which is what makes an exam case and a production call one code path.

**The extractive floor** (`objective.floor`). No model: the owner's texts in the uncovered
range, verbatim, minus three things decided mechanically — shorter than
`FLOOR_MIN_CHARS` (12), starting with `[`, an exact duplicate of an earlier one. **No word
list.** Which short replies are noise is a judgement; a list of them is a vocabulary
nobody measured (`docs/vocabularies.md`). The consequence is visible and accepted: in the
#422 chat the floor keeps "what's the answer?" and "Czemu nie skoczyłeś?" and drops
"tak" and "Do it". `TestTheFloor`.

## What code checks, and what it computes

Every rule in contract §3.14's table has its enforcing line in `validate_answer`, and a
failing answer gets the validator's own sentence back on the role's one corrective retry.
Three decisions worth their reasons:

- **`covers_to_turn` is computed, never claimed.** It is the last turn up to which every
  owner text the floor would keep is cited somewhere in the revision. The model is never
  asked for it and could not set it. A skipped message is listed in `uncited` and shown
  again next time instead of being certified as represented — which matters because
  #426's trimmer will stub owner turns up to this number.
- **A goal or task the answer leaves out is carried forward by code.** A pivot never
  overwrites (D3), and a model that forgets a parked goal cannot delete it by omission.
- **Reopening and dropping must rest on something NEW.** `done`→`in_progress` and
  `stopped`→`pending` need an owner cite from the delta, and a goal becomes `dropped` only
  on an owner word or act in the delta that comes after he asked for the goal. Citing the
  message that created the goal — which the base already had — justifies nothing, and
  without this rule every "owner cite" requirement is satisfied by the goal's own origin.

A constraint is checked as a substring with whitespace runs collapsed on both sides and
nothing else — no case folding, no translation. Carrying an unchanged constraint (same
text, same cites) is not re-verified: it was verified when it was written, and its cited
text may have been cut in `earlier`. `TestValidation`, `TestTransitions`, `TestCoverage`.

**What is NOT checked**, stated so the words do not outrun the code: that a goal's TEXT
says what its cites say; that `done` evidence actually delivers the task (an answer ref
is accepted as evidence of the kind, not of the content); that `change` is truthful.
Those are the distiller's judgements, and the golden file is where they are measured.

## Emission — at task end, off the interactive path

- **Web:** `server._run_task` captures the boundary (the log's size and the agent's turn)
  synchronously right after `task_end`, and `_distill_after_turn` runs
  `objective.distill_at_boundary` on a worker thread as an epilogue, after busy has
  cleared — beside the titler (#397), never inside the turn. Every task end, including a
  failed or stopped one: those are owner-relevant facts.
- **CLI:** `cli.distill_in_background`, a daemon thread after each REPL task that
  returned. A one-shot `aish "task"` exits first and records nothing; a task interrupted
  by Ctrl-C or a model error records nothing either.
- **The boundary is bytes, not "now".** The distill reads the file only up to the size it
  had at `task_end`, so a late distill cannot read the next task's half-written records.
- **One at a time per chat** (a per-path lock), so revisions land in order.
- **It never raises.** A raising distill writes a `role` record saying so; a refused
  write (a trashed chat) ends it quietly. `AISH_OBJECTIVE=0` turns emission off, and the
  suite sets it (`tests/conftest.py`) for the reason `AISH_NOTIFY=0` exists: a background
  writer appending to logs hundreds of tests read back. `TestItNeverReachesTheTask`.

**What a task end writes.** Always a `role` record (the D7 shape, `turn` = the boundary).
Then the distiller's revision if it validated; else an `extractive` revision if any owner
text is uncovered; else nothing, and the previous revision stands. `TestDistill`,
`TestEmission` (the web end to end through a real task, and the CLI thread).
The record kind itself is renderless and not activity: `TestTheRecordKind`.

**Which model.** The charter declares class `session`: the session's own backend,
including a local one, so his text never rides to a different provider than he chose.
`AISH_ROLE_MODEL` does not apply to it. claude-max has no stateless seam, so there it is
**N/A**: the `role` record says `unavailable`, and the floor is written. And the charter
must be **admitted** — `scripts/role-admission.py --model <spec> distiller` — for the
exact model spec the session runs; until then every task end records `unadmitted` and
writes the floor. On a fresh install nothing is admitted. `TestWhichModel`,
`TestTheCharter`, `TestExamAssertions`.

**Contention, not measured away.** On a single local model server the distill competes
with the owner's NEXT turn for the same GPU. It cannot delay the answer it follows, but
it can delay the one after it if he types quickly. The measured latency is below; nothing
yields to a starting turn yet.

## Retry, forks, redaction

The current revision is the newest LIVE `objective` record. A revision lands after its
task's `task_end`, so Retry of that task supersedes it with the task. A revision that
lands late — inside the next task's bracket — is superseded if the owner retries THAT
task, and the previous revision stands; the next distill re-covers the gap. Revision
numbers are never reissued (1 + the highest in the file, superseded included).

## Measurement

`scripts/measure_objective.py` runs the distiller over a recorded log at given task
boundaries, chained (each boundary's base is the previous boundary's revision), calling
the backend directly — never through a chat, never through aish-web. Results for the
#422 chat are in the #424 hand-off; the golden file
(`scripts/fixtures-424/golden-session-20260925-204008-294943.json`) is **a DRAFT for the
owner's review**, derived from the log and citing its records.
