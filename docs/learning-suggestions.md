# Learning suggestions — a lesson offered when a task went the long way round

`learning.py`; the call sites in `server._run_task` and the CLI REPL; the replay check
`replay_checks.learning_suggested`; the record is contract §3.19. Issue #446, part of
#444's single-home audit.

> **What runs today.** At every delivered answer, aish reads back the records the task
> wrote to its own log and counts five things: whether the loop detector, the stall cap or
> the step ceiling stopped it; how many tool calls it made; how many of them repeated an
> earlier call exactly; how many tool calls the harness refused; how many approval cards
> the owner answered with a comment. When any count reaches its threshold, the answer
> carries one more quick-reply chip — **Save this as a lesson?** — which sends `/learn`.
> A `learning` record is written at every answer, tripped or not, saying what was counted,
> what tripped and whether the chip was shown. `AISH_LEARNING=0` turns all of it off.

## Why it exists

"Propose saving a lesson when a task went roundabout" had two homes, and neither could work:

- **A rule** (`propose-learning-on-correction`, disabled). Rules fire on observable
  actions; "this went suboptimally" is not one, and the compiled trigger came out circular
  (fires on `create_skill`, demands `create_skill` first). A category mismatch, not an
  authoring mistake.
- **A pinned memory** asking the model to notice its own roundabout workflow — capability
  phrasing of the kind measured to be ignored by local models, demanding a self-assessment
  the model does not reliably make.

The signals were already in the log. So the harness counts them, and the model is never
asked whether it did well.

## The mechanism

**One function reads a task's shape, live and in replay.** `learning.shape_of(records)`
folds a task's records into five numbers; `TurnShape.tripped()` compares them with the
thresholds. Live, the surface captures the log's size before the task starts
(`learning.log_size`) and, at the answer, reads back exactly the bytes the task appended
(`read_span`). A replay check groups a recorded log by its `task_start`/`task_end`
brackets (`tasks_of`) and runs the same fold. *The bug this prevents:* a live detector fed
by in-memory counters and a replay reader fed by the log are two definitions of "the long
way round", and they drift — the replay would then grade the live detector against a
different question. Reading the task back costs little: over the owner's 1,605 bracketed
tasks the span was 72 KB at the median and 2.4 MB at the worst, and the worst parsed in
9 ms (2026-10-12).

**The signals — each one a record aish writes itself, never text matched by words:**

| signal | read from | trips at |
|---|---|---|
| `stopped` | a `message` record whose `source` is `loop_stop`, `stall_stop` or `step_limit` (`_finish_stopped`, contract §3.17) | any |
| `calls` | `call` trace records (§3.13's sibling, #240) — one per tool call dispatched | `LONG_TASK_CALLS` = 25 |
| `repeats` | the same `call` records: calls whose `(tool, arguments)` equal an earlier call's in the task, by `plan.call_key` — the repeat nudge's K (§3.16) | `REPEATED_CALLS_AT` = 5 |
| `refusals` | `gate` records with `at: "gate"`, `verdict: "refused"` and a `tool` — a rule gate or the stop gate refusing a tool call (§3.3, §6.1) | `REFUSALS_AT` = 2 |
| `corrections` | `tool` steps carrying a `comment` — the owner's sentence on an approval card (§3.4) | `CORRECTIONS_AT` = 2 |

The stop gate's ARMING row is `refused` too but names no tool; it records his denial,
which is counted once, as a correction. Counting both would score one comment twice.

**Thresholds, and the measurement behind them.** Over the owner's 1,692 web tasks with
brackets (every log under `~/.local/state/aish` on 2026-10-12): tool calls per task p50 0,
p90 12, p95 18, p99 31, max 103 (34 tasks ≥ 25); exact repeats p95 1, p99 5, max 50
(17 tasks ≥ 5); harness refusals p99 1, max 2 (13 tasks ≥ 2); card comments p99 1, max 3
(9 tasks ≥ 2); no task in those logs was stopped by the loop detector, the stall cap or
the ceiling (the `source` stamp is recent, 2026-10-05, so older stops are not visible
this way). Those distributions are over all 1,692 bracketed tasks. **Run through the
shipped fold itself (`learning.tasks_of`), the thresholds trip 55 of the 1,575 answered
tasks (3.5%), in 41 of 470 chats** — by signal: `calls` 31, `repeats` 16, `refusals` 11,
`corrections` 9.
The #436 source task ("yes": 41 calls, 10 exact repeats, 2 card comments) trips three
signals; two earlier tasks of the same chat trip one each. Each threshold is a named
constant and every record carries the values in force, so a reader never has to know
which version of this file wrote it (contract §0, corollary 1).

**Considered and left out, with the reason:**

- *Answer-check refusals* (`gate{at: "verify", verdict: "refused"}`) — p90 is 1 per task,
  almost all from one rule sending drafts back; as a signal it would put the chip on a
  tenth of all answers.
- *A user message reversing a just-given answer* — the issue lists it as optional. No
  crisp recorded signal exists for it short of matching his words, which the vocabulary
  doctrine forbids (`docs/vocabularies.md`). His card comments are the crisp form of
  "he corrected it", and they are in. A Retry is crisp too, but the attempt it discards
  is rolled out of the model's context, so the `/learn` the chip would start could not
  see what went wrong.
- *Plan calls excluded from `calls`* — not done; the repeat nudge counts them, and one
  definition of K is worth more than the difference.

## The action — one chip, and why not the queue

The issue left two routes open: a chip on the answer, or a candidate queued for the weekly
curation. **The chip shipped**, because the seam already carries it on both surfaces: the
web renders `[label](aish-reply://…)` as a one-tap button and the CLI lists the same
syntax as a numbered menu (`parse_reply_chips`). The queue would have needed a new store,
a new consumer in `curate.py`, and a judge deciding later what the owner could decide now
with the conversation in front of him. Nothing of the queue was built.

**The chip only ever adds.** It is appended to the answer the owner is shown; it never
enters the model's history (`agent.messages` holds the answer as the model wrote it), it
refuses nothing, holds nothing and writes no knowledge. Tapping it is exactly typing
`/learn`: on the web it is sent as an ordinary message (`[CHIP-NEVER-COMMANDS]` — a chip
never routes through the client's slash handler) and the server expands `/learn` as it
expands a typed one; in the terminal its number resolves to `/learn` and the REPL expands
it. Every write the distillation proposes is diff-approved as always.

**Where it goes on the web answer:** after the quick-reply net (`apply_quick_reply_net`),
because the net skips any answer that already holds an `aish-reply://` link — appended
first, the suggestion would silently take the place of the Yes/No replies an open question
gets. It streams as a `token` like the net's suffix, and rides on `done.result`.
`_notify_done` is handed the answer without it: a push body is not the place for a button.

**Cold replay re-derives it.** `reconstruct_events` reads the turn's `learning` record and,
when it says `chip: true`, puts the chip on the replayed `done` with the same function the
live path used (`learning.with_chip`), so a reload shows what the live turn showed (L4).
The fallback Yes/No chips are NOT re-derived on replay, and never were; on an answer that
carried both, a reload shows the suggestion and not the fallback. That asymmetry predates
this and is not fixed here.

## Who sees it — the attended question

**The chip goes only on an answer to a task a person sent.** On the web that is a task
from a live socket (`_launch`, typed, tapped or queued), a Retry he pressed, or a
continuation that Retry chose (`owner_sent=True`). A trigger's prompt and restart
recovery are aish's own (`owner_sent=False`). In the terminal every task is typed. The
record is written either way, with `withheld: "not_owner_sent"`.

*The bug this prevents:* an automated chat that loops the same way every night — a
schedule, an e-mail trigger — would carry the same suggestion every morning, one per run,
for one lesson. That is the accumulation the issue warns of.

**Why "who asked" and not "who is watching".** He starts tasks and walks away; the answer
keeps, and he reads it later (the completion push is suppressed for his own chats for the
same reason, `_notify_done`). A test on viewers at delivery would withhold the chip from
exactly the long tasks he left running — the ones most likely to have gone the long way.
A triggered chat he later types into is his conversation from then on: his own tasks there
are owner-sent and can carry the chip.

**Withheld, in this order** (`learning.WITHHELD`, recorded on the record):

1. `not_owner_sent` — above.
2. `learn_task` — the task was `/learn` itself, told apart by aish's own template
   (`is_learn_prompt`: the text starts with `LEARN_PROMPT` or `LEARN_LESSONS_PROMPT` up to
   its first placeholder). A distillation reads recall, skills and memory and is long by
   nature; a suggestion on the lesson would loop.
3. `one_shot` — `aish "task"` exits after the answer; no prompt follows to take a chip.
4. `cooldown` — the chat's most recent evaluated task already showed it. **Never twice in
   a row**: in the owner's logs 5 tripped tasks immediately followed a tripped task in the
   same chat. Read from the log (live records only — a Retry that discarded the earlier
   task took its chip with it), and only when this task trips.

## The record — contract §3.19

One `learning` trace record per delivered answer, written by the surface through
`logref.step` (the log's one lock) inside the turn's brackets, stamped with the turn.
Renderless (`RENDERLESS_STEPS`); its only visible effect is the chip on `done`. Not
activity, so it never marks a read chat unread.

```json
{"kind": "learning", "turn": 12, "records": 214,
 "counts": {"calls": 41, "repeats": 10, "refusals": 1, "corrections": 2},
 "stopped": null,
 "at": {"calls": 25, "repeats": 5, "refusals": 2, "corrections": 2},
 "tripped": ["calls", "repeats", "corrections"],
 "chip": true}
```

`withheld` is present only when `tripped` is non-empty and `chip` is false. A failure to
evaluate is recorded as `{"kind": "learning", "turn": …, "chip": false, "unevaluated":
"<exception type>"}`, so the absence of a record never has to stand for it.

**No record**, by design: a turn that FAILED (no answer to carry anything; its `task_end`
says why), a `!` command (no model task), `AISH_LEARNING=0`, and a session with no log
file. **The record is the consultation counter** the vocabulary doctrine asks for
(`docs/vocabularies.md`): one row per evaluation, so how often the detector was asked and
how often each signal tripped is a scan over the logs.

## Replay evidence

`replay_checks.learning_suggested` (in `LIBRARY`, so any scenario can `use` it): every
answered task whose recomputed shape trips has a `learning` record that tripped the same
signals, and no record trips on a task that does not. On a log written before the
detector, a long-way task FAILS for want of a record — that is how a baseline reproduces
(R4). Against the #436 source log (`session-20260929-214924-097047`, read-only,
2026-10-12):

```
FAIL learning_suggested: 3 of 3 long-way tasks unsuggested … first: task 7 ['corrections']
     {'calls': 19, 'repeats': 1, 'refusals': 1, 'corrections': 3}
```

with task 12 (`["calls", "repeats", "corrections"]`) and task 9 (`["repeats"]`) the other
two, and the nine ordinary tasks of that chat tripping nothing. `ctx-trippy-loop` can
adopt it by adding `"learning_suggested"` to `[checks] use` — the scenario lives in the
owner's private tree and is not edited from here.

**Tests**, `tests/test_learning.py`: `TestTheShape` (each signal at its threshold and one
below it), `TestTheDecision` (the span, the withheld order, the cooldown on a real log),
`TestItNeverReachesTheAnswer` (a failed evaluation is recorded, never raised; the kill
switch), `TestTheLearnTask` (`is_learn_prompt` on every expansion), `TestOnTheWeb` (the
real task path through the server with a scripted model: the chip on `done` and in the
stream, the loop stop, the reload, `/learn`, the cooldown, a trigger), `TestInTheTerminal`
(the REPL's numbered menu and its `/learn`, the one-shot record), `TestTheCheck` (the
replay check on generated logs, with and without the records) and `TestTheRecordKind`
(renderless, not activity, inert on replay).

## What is not done, and what is not checked

- **Whether a suggested lesson helps** — nothing measures what `/learn` saves after a tap,
  or whether the next such task goes better.
- **Whether the thresholds are right.** They are the owner's p95–p99 band on one machine's
  logs, chosen so the chip is rare (3.5% of answered tasks). The records make the rate he actually
  sees a scan; the constants move when that says so.
- **CLI logs have no brackets**, so the replay check reads no task in one, and the
  measurement above is web tasks only. The live CLI detector reads its span by offset and
  is unaffected.
- **A CLI `--resume` does not re-offer the chip**: `replay_history` rebuilds the menu from
  the last answer's text, which never held it.
- **`repeats` is counted from the recorded arguments**, which `CALL_ARG_CHARS` caps; two
  calls differing only past the cap count as a repeat there and not in the agent's own
  repeat nudge.
- **claude-max**: its SDK owns the loop, so `stopped` never trips there; the other counts
  are whatever its records hold.

## Retiring the two old homes

Single home (#444's migration principle): once this ships, the pinned memory
`proactively-propose-capturing-learnings` and the disabled rule
`propose-learning-on-correction` are retired. Both live in the owner's config tree, which
nothing in this repo edits; the edits are listed on the issue for him to apply.
