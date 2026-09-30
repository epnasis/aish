# The plan — aish's own checklist for the objective

`plan.py`, the `plan` tool in `tools.py`, `Agent._plan_call`. Epic #423; this page
covers slice 2 of the owner's three-level model, #433: **the plan.** The record's
schema is contract §3.15; this page is why it is shaped that way.

> **What runs today.** The acting model has a `plan` tool on its menu. When it calls
> it, code checks the list and records a revision; nothing executes. The plan is
> shown to the model in the tool's result and in the per-task reminder beside the
> objective, and to the owner under the objective strip on the web and through
> `/plan` in the CLI. He can drop a task or ask for a replan. A task marked done
> without evidence that resolves is recorded as pending. A task that repeats calls
> exactly with no live plan is asked, once per doubling, to write one (the repeat
> nudge). `AISH_PLAN=0` takes the tool off the menu; `AISH_PLAN=tool` keeps it and
> turns the replan triggers and the repeat nudge off. Both exist for the measurement
> below.

## Which level this is

The owner's decisions of 2026-09-29 (epic #423): the **objective** is why he is here
(`docs/objective.md`); the **plan** is how aish gets there; **hints** are his card
comments. For the plan he decided:

- it is created **only by aish, when planning**, and never from his messages;
- it is **optional**: only for objectives that need several steps;
- a task sits **above a single command** ("test service X"): if one tool call
  finishes it, it is not a task;
- **done needs evidence** that the work was done and produced a result;
- **replanning** may add, change or drop tasks, and that is expected;
- it is **visible to him**: an odd task shows him a misunderstanding early, and he can
  drop a task or ask for a replan;
- **card comments are hints**: they may prompt a replan, never become tasks.

Why it exists, in his words: a checklist so that, while working through a lot of
output, aish — especially a small-context model — does not forget what it planned.

## The tool, and why it only records

`plan` takes the WHOLE list every time (`tasks: [{id, title, state, evidence}]`) and
goes through `_dispatch` like every tool (L1). It is not in `READ_ONLY_TOOLS`, so it
never takes the parallel path, and it executes nothing: `plan.revise` validates the
list against the plan in force, the agent writes one `plan` record through its
log-only sink, and the result says what was recorded. Its description carries the
owner's rules as MUSTs with one example (the only phrasing measured to be followed,
`docs/agent-core.md` §Narration), inside the menu fence
(`tests/test_tool_menu_size.py`; the native menu is 33,006 of 34,000 characters with
it).

**Whole-list replace, not add/update/remove calls**, because a model that has to name
what it removes forgets to, and a list that is sent whole can be compared whole. What
the model leaves out is not lost: an open task it omits is kept as `dropped_replan`
(`dropped: vanished`), and a done task it omits stays done. Nothing is ever deleted.

**Matching** is by `id`, then by title (whitespace squashed, case folded), then new.
Models renumber; a title that did not change is the same task. An `id` that names a
CLOSED task (done or dropped) under a different title is taken as a new task with a
fresh id, so a renumbering model cannot turn a finished task into another and lose
its evidence.

**Not progress.** A plan call learns nothing about the world, so it never counts as
progress for the #108 step budget: a model re-planning in a circle still reaches the
stall cap. It still enters the loop detector like any call.

`tests/test_plan.py`: `TestTheRecordKind`, `TestParse`, `TestTheTool`, `TestReplanKeepsWhatItReplaces`.

## Done needs evidence

A `done` task must cite what proved it: an exact part of a successful call's command
or arguments, or that call's ref `t<turn>.c<call>`, or part of a `!` command he ran
with exit code 0, or of an earlier answer. `plan.resolve_evidence` looks it up in the
chat's LIVE records (L7). The model does not know call refs up front, which is why a
quote is accepted and the RESOLVED ref is what gets recorded; a result that downgraded
something lists up to eight recent successful calls with their refs, so the next call
can cite one.

A done that does not resolve is **recorded as pending**, with `downgraded` naming what
was cited and why it did not resolve, and the result says `NOT DONE`. The downgrade is
a fact in the record, never a silent correction (L8).

**What resolution does NOT establish**: that the cited call proves the task. "The
call happened and succeeded" is code's; "it proves this task" is the model's claim.
The measurement below reads a sample of dones blind for exactly that. `TestDoneNeedsEvidence`.

## His drop is final

A task he drops is `dropped_by_owner`, and the model cannot change it: a model list
that tries is recorded with `refused`, and the task stays as he left it. Re-adding it
under the same title matches it by title and is refused the same way.

The model's revision is written through the agent's sink, not conditionally, so one
computed just before his drop landed could omit it. So the drop is **re-applied on
every read** (`plan.current`, the owner-drop overlay): the last word on any title he
dropped is his, whatever order the writes landed in. His drop and replan records
survive a Retry and a redaction of the turn they sit in (`session._is_owner_objective`
covers both kinds), like his objective edits. `TestTheOwner`.

## Where it reaches the model

- **The tool result**, after every call: the recorded list, downgrades, refusals.
- **The per-task reminder, as a delta** (`agent.plan_delta`), next to the objective:
  in full when the list is not the newest one already in history, `PLAN_UNCHANGED`
  when it is, `PLAN_NONE` when a Retry took the plan away. "Already in history"
  includes the plan tool's own results, because the model wrote most of what it
  holds. Never in `messages[0]`, so the prefix stays byte-stable. The `context`
  record carries `plan: {revision, shown}`.
- **The newest plan call's arguments are exempt from the #429 argument lever**
  (`Agent._newest_plan_call`): the model's own latest list is never cut to 80
  characters. Older plan calls are trimmable like any. A message holding both the
  newest plan call and another long call has only the other call cut, and is then
  marked stubbed, so its plan call stays whole even after a newer one exists — the
  conservative direction. **The newest recorded plan RESULT is exempt from the output
  lever too** (`Agent._newest_plan_result`): a reminder saying `PLAN_UNCHANGED` points at
  it, and its downgrade notes live nowhere else (review finding: without it a mid-task
  trim could leave the model only its own pre-validation arguments, including a done
  that code had downgraded). `TestTheReminder`, `TestTheArgumentLever`,
  `TestReviewFindings`.

## Replan triggers — one line each, never a forced call

The first three fire only while the plan has an open task, because a nudge to a
finished plan is noise; the repeat nudge is their mirror image and fires only while
there is NO live plan. Each appends ONE line and the model decides. `AISH_PLAN=tool`
turns them all off.

| trigger | where the line goes |
|---|---|
| a denial with a comment | appended to the denial's own result (`_call_result`, off the recorded decision). The NATIVE plan tool is **exempt from the stop gate** (a plugin that takes the name is not) that denial arms: it executes nothing, and this is the moment the owner's model most wants it revised. Deny still means stop — every tool that acts stays refused, and only a text-only turn ends the task |
| a stall | an `[aish: …]` note after `STALL_REPLAN_AT` (4) no-progress steps, once per task; the stall cap (8) leaves room to act on it |
| a failed task end | one reminder segment on the next task, when the previous one raised or ended at the stall cap, the step ceiling or the loop detector (`_task_unfinished`, kept on the agent: a restart of aish-web between the two tasks loses it) |
| exact repeats, no live plan | the **repeat nudge**, below: an `[aish: …]` note after the step whose calls crossed the threshold. The one trigger that fires WITHOUT a plan — its job is to get one written |

The hold with a comment (approve + comment) is a hint too, and is deliberately NOT a
trigger: the owner named the denial.

His own actions are not triggers but reach the model the same way: a drop or a replan
request made while a task runs sets `Agent.plan_owner_changed`, and the loop appends
one `[aish: …]` note before its next model call; made while idle, the next reminder
carries it (a pending replan request rides inside the full rendering). `TestTriggers`,
`TestTheOwnerMidTask`.

### The repeat nudge (decided 2026-09-30)

**Why.** In the Japan chat (`session-20260929-214924-097047`, turns 10–13) the model
re-ran the same `trippy search` and the same comma-joined `trippy details` over and
over, and never called `plan`. Neither stop fired: the loop detector keys on
(tool, args, RESULT) and a live search returns a slightly different result each time
("kept 14/25" vs "kept 12/20"), so every repeat counted as progress, and the stall
counter never started. The replay experiment (`~/.cache/aish-exp-plan/`, P2) took three
points inside those turns and sent each 5 times per arm: the line below got a plan call
**11/15**, the request as sent **0/15**, and the same counted facts WITHOUT the
instruction to plan **0/15** — stating the counts alone did nothing. Plan-first rules,
reminders and planning steps were measured too (P1: an imperative rule 2g got a
plan-first call 0/32 on the Japan requests, and the plans the other arms wrote were read
as too fine) and, by the owner's decision, are NOT built.

**What is counted** (`plan.Repeats`, a `run_task` local, so per task by construction):
every tool call the native loop dispatched, in order. N = the calls so far; K = the
calls whose (tool, arguments) equal an earlier call's in the same task. **Results are
never looked at.** The key is `plan.call_key`: the tool name and the arguments as
canonical JSON — keys sorted (the order a backend serialised them in is not the model's
choice), every value compared EXACTLY, whitespace and case included. No squashing and no
fuzzy match: anything looser counts as a repeat a call the model did not repeat.

**When it fires** — the rule, stated before the tests were written:

- after a step's results are appended, when K ≥ `plan.REPEAT_NUDGE_AT` (2) the first
  time, and afterwards only once K has at least **doubled** since the last threshold
  crossed (2 → 4 → 8 …). "Once per task unless repeats grow further" is exactly that,
  and it is geometric: a task that made K exact repeats got at most ⌊log₂ K⌋ lines. A
  step whose calls jump K past two levels at once (1 → 4) is one crossing, one record;
- AND the task has no live plan: **no `plan` call in this task** (whatever it returned —
  the line says "none of them to the plan tool", and that must stay true) **and the
  chat's plan in force has no open task**. A finished plan from an earlier task is not
  a live one;
- AND the replan triggers are on (`AISH_PLAN` is not `tool`) and the NATIVE `plan` tool
  is on the menu (not under `AISH_PLAN=0`);
- AND the step is not one that ENDS the task — the loop detector fires on it, or it is
  the stall cap's last step — because the wrap-up turn that follows has no tools, so
  "before your next call" would be false (review finding);
- AND the stop gate is not armed: after a denial with a comment, deny means stop (L2),
  and a line inviting "your next call" would argue with it (review finding).

A crossing whose line is suppressed still advances the level: each level is decided
exactly once, and its record says which way.

**Why 2.** The three P2 points were at (N, K) = (13, 2), (6, 2) and (21, 4), and the
line got a plan call at 8/10 of the K=2 samples. In that chat K first reached 2 in
exactly the four long turns (10–13, at N = 7, 5, 13, 6); turns 1–9 never did (turn 8:
one repeat in 19 calls). Replayed over that chat's `call` records, grouped by model
call as the loop evaluates them, the built detector crosses seven times in turns 10–13
— turn 10 at (N, K) = (7, 2) and (14, 4), turn 11 at (5, 2), turn 12 at (13, 2), turn 13
at (6, 2), (21, 4) and (32, 8) — which include all three P2 points, and at each of those
three it emits a line **byte-identical** to the one tested there (checked against every
arm-(b) line in `p2.jsonl`). The other four crossings were never sent to a model.

**What it will also catch** (measured 2026-09-30 over every `call` record in the state
dir: 983 logs, 709 tasks with calls): 60 tasks reached K ≥ 2, at N = 3–28 (median 8).
The call that crossed was `browse_act` in 22 of them, `run_command` 10, `browse` 8,
`read_file` 6, `read_url` 5, others 9. Driving a page repeats calls by construction
(#251), so on browse tasks this line will often be asked for where no loop was. Whether
it helps or costs there is not measured.

**The line** states only what code counted, then the instruction:

> [aish: this task has made {N} tool calls, none of them to the plan tool; {K} of them
> repeated an earlier call exactly: `{call}` (run {n} times); …. Before your next call,
> write a plan with the plan tool: what is left to do, one task per item.]

Up to `REPEAT_LISTED` (3) calls, the most-repeated first (ties: the first repeated
first); a `run_command` shows its command, any other tool `name {arguments JSON}`;
each cut to `REPEAT_SHOWN_CHARS` (100) with `…`. One `[aish: …]` user message after the
step's tool results, never in `messages[0]`, never a forced call. It is **never
progress**: it touches neither `seen` nor the stall count, so a task hammering one call
with one result stops at the loop detector after exactly as many model calls with the
nudge as without (`test_it_is_never_progress`).

**Recorded** at every crossing, sent or not, as a renderless `repeat_nudge` record
(contract §3.16): the counts, the threshold, the calls shown, and either the line as
sent or why it was not. `aish explain` shows it between the rounds it fell between, as
a step on the step screen, and as a row worth a look. `TestTheRepeatNudge`,
`TestTheRepeatNudgeIsExplained`.

**Not covered.** claude-max (the SDK owns its loop, so nothing counts there). A
continuation after a Retry or a restart counts from zero: calls made by the attempt it
continues are not in this task's count. Whether the plan the line elicits then makes the
task SUCCEED is not established — P2 measured one response; P3 (does planning help
completion) is a separate question.

## Where it reaches the owner

- **Web:** under the objective on the pinned strip — `Plan · 2 of 5 done · 1 dropped`,
  then the open tasks, the one being worked on first, folded into `+N more open` past
  three (`[OBJECTIVE-STRIP]`, `docs/web-frontend.md`). A tap opens the objective sheet
  at its plan section: every task with its state and the evidence each done rests on,
  a trailing ✕ on each open task (the Recents row's ✕), asked about in the shared
  confirm modal because aish cannot bring it back, and *Ask aish to revise the plan*.
  Both are receipted actions (`plan_action`), never chat messages. `/plan` opens the
  same sheet.
- **CLI:** `/plan` prints the list with evidence; `/plan drop <id>` and `/plan replan`.
  `TestTheCliCommand`, `TestTheWeb`, `tests/js/test_plan_strip.js`.

The plan rides the `objective` event (`docs/web-server.md`): one announcement and one
ordering for the whole strip. A model revision repaints it as it lands, from the
agent's thread (`announce_objective_threadsafe`).

## What is not checked, and what is not done

- **That a task is above single-command level.** The description tells the model;
  nothing counts calls per plan task (the repeat nudge counts calls per aish task, not
  per plan task).
- **That a resolved evidence step proves its task** — see *Done needs evidence*.
- **claude-max** gets the tool (the SDK routes it through `_locked_dispatch`) but no
  reminder, so the plan reaches that model only through the tool's results.
- **Evidence can be laundered within the letter of the rule**: any successful call's
  arguments are quotable, so a model could run a harmless call carrying the words it
  wants to cite. Resolution proves the call happened, never that it proves the task.
- **A title he dropped stays dropped for the whole chat**: the overlay applies to any
  later task with the same title, by design.
- **Two announcement races self-heal on the next event**: `objective_version` is
  bumped on the agent's thread as well as the loop's, and a drop landing between the
  reminder's read and its write is neither in that reminder nor noted mid-task (the
  overlay still enforces it; the next reminder shows it).
- **A redacted message can survive in a task title** written after it, as the
  objective's statements can (`docs/objective.md`).

## Measurement

Run 2026-09-29 on the isolated harness, local Qwen3.6-35B-A3B-8bit on mi, `think`
on, AISH_OBJECTIVE=0, code at 7521c94 (before the review and measurement fixes
below). Scripts, pass bar (written before the scored runs), every chat log and the
scores: `~/.cache/aish-433/`.

**Scenario.** One task per fresh chat: get Warsaw's 24 h rain from six local stub
services, each with its own protocol documented only in ~5 KB of noisy docs
(token-then-data, a 12 KB region list, an API-key header, always-503, three pages to
sum, coordinates), then a table. A first scenario (health + version of six services)
was piloted once and dropped before scoring: the model did it in one shell loop, so
it is not multi-step and it correctly wrote no plan.

| arm | n | all 6 attempted | completed /6 | answer correct /6 | plan used | dones (all with evidence) | downgrades | median s |
|---|---|---|---|---|---|---|---|---|
| A no plan tool | 20 | 20/20 | 5.95 | 5.70 | — | — | — | 90 |
| B plan tool | 20 | 20/20 | 5.95 | 5.65 | 2/20 | 7 | 23 | 96 |
| C plan + triggers | 20 | 20/20 | 6.00 | 5.85 | 0/20 | 0 | 0 | 90 |

**Pass bar not met, and not measurable here**: the bar (C's all-attempted rate at
least 15 points over A's, void if A ≥ 90%) is void — the baseline attempted all six
services in every run, so this scenario leaves no room for the plan to help. No
trigger fired in C (no stall, no denial, no failed end). No run errored; 141 cards
were approved by the driver; 1.6 h of runs in all.

**What it did show:**
- **The model rarely plans on this task**: 2 of 40 runs with the tool on. Why is not
  established.
- **Evidence resolution, observed in one run (B19)**: 23 downgrades before 7 dones
  landed. The model cited OUTPUT values (`rain_mm=3.2`), then the command text without
  the shell quotes the call ran with, then a ref (`t1.c6`) to a failed call; each was
  refused correctly by the rule as written. Two fixes followed, unmeasured live:
  shell quotes are ignored when matching (`TestMeasurementFindings`), and a task done
  on resolved evidence stays done when a later citation fails to resolve (it had
  flipped back to pending twice in that run). The NOT DONE line now says "never its
  output".
- **Blind reading of all 7 recorded dones** (Fable 5.0, arm hidden): 6 supported,
  1 partial — bravo's cited call shows a 24 h rain value for region `pl-maz-0419` but
  not that the region is Warsaw (that link was in an earlier, uncited call). No done
  was unsupported. Seven dones from one run is not a sample of the arm.

**Not established**: whether the plan helps completion on a task where the baseline
forgets subtasks — that needs a scenario whose baseline fails, which this one did not.
