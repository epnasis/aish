# Trace records — governance, identity, refusal

How activity-trace records are emitted, correlated and rendered. **`docs/trace-contract.md` is the binding schema** — merged and enforced, every record must match it. This file is the rationale.

---

## A renderless record needs TWO mechanisms

The mechanical fact everything here is sized against: `app.js`'s `traceStep` calls `ensureTrace()` **before** it dispatches on `step.kind`. So a record kind with no renderer does **not** degrade to "renders nothing" — it opens an **empty live trace card with a running ticker**.

A kind that must not render therefore needs both halves:

1. `Agent._emit_record` — log-only: writes to `step_log`, never to `on_step`.
2. Membership in `session.RENDERLESS_STEPS`, which `reconstruct_events` skips.

**Either alone still produces the card** — the first on cold replay, the second on live emit. `has_trace` is set by the `kind == "trace"` arm BEFORE the skip, so a log holding only governance records still reconstructs instead of falling back to a flat history blob.

**Do not add a third mechanism.** A frontend guard in `traceStep` is explicitly NOT the design, and `tests/js/test_renderless_ghost_card.js` fails if one appears — the Python discipline was sized against `traceStep` behaving exactly as it does. Pinned from both sides by `TestRenderlessRecords`, each half independently mutation-checked, and verified where the artefact would actually appear (at a real client, live and on cold replay) by `TestNoGhostTraceCards`.

---

## A record for nothing happening

Every other kind in the contract describes something that happened. `stall` (§3.11) had to describe something that did **not**, and that is why it took a month of occurrences to notice.

On 2026-08-30 one chat wrote five `task_start` records for one question. Four of them emitted `rule_eval` and `context` and then stopped — no user `message`, no `brief`, no `task_end` — while the owner watched an idle screen and re-sent four times over 45 minutes. Twenty-eight turns across a month of logs have that exact shape. **Not one can say where it stopped**, because the only evidence a stall leaves is the absence of everything after it. §0 corollary 2 by a route no record shape anticipated.

Three things make the watchdog honest rather than merely present:

**It reads liveness from RECORDS, never from the thread.** `_mark_alive` is called by `_sink_step` and `_emit_record`; a thread existing proves nothing, because a wedged thread exists too. The cost is that a turn legitimately sitting in one long call looks silent, which is what the threshold is for and why it sits above both a slow model call and a slow browser step.

**Its own record is excluded from that stamp**, or the first report would silence the watchdog that found it. Re-reporting matters because *"stuck at 13:13"* and *"still stuck at 13:35"* are different facts, and only the second says nobody recovered it.

**It states no cause, and the schema has nowhere to put one.** A stack is an observation; why any of the 28 turns were sitting there is still unknown. A `reason` field would be handed a guess on the first occurrence, and the guess would then stand exactly where the disproving evidence should be — L8, and the whole point of building an instrument instead of sharpening a sentence. When the next one fires it will name a line, and *then* there is something to fix.

What it deliberately does **not** do is disambiguate `task_start`. An unmatched one is still the restart-recovery signal, so a live stall still spends `RESUME_MAX_ATTEMPTS` as though the process had died — the record makes that visible without pretending to have fixed it (#336).

---

## Turn and call identity (contract §2)

`Agent._turn` advances in `_reset_task_state`, so claude-max — whose SDK loop never enters `run_task` — counts turns too. A per-turn `call` counter is assigned in `_call_result` and carried as a **LOCAL, never read back off the agent**: read-only tools run in parallel, and an attribute would hand two concurrent calls the same id.

Stamped on `tool_start`/`tool`/`knowledge` plus every renderless kind. `thinking` is deliberately left alone as the high-volume kind.

This replaces positional correlation — pairing a record with the NEXT user record, which the curation ledger's own docstring apologises for. `TestTurnAndCallIdentity`.

---

## A refused action is never a green step

ONE rule, applied last in `_emit_tool_step`: a `decision` in `REFUSED_DECISIONS` (`denied`/`held`/`blocked`/`rejected`) forces `ok: false`, `status: "failed"`, `verdict_by: "gate"` — whichever path set it.

This is **wider than the contract's §6.13 table**, which names five plugin constants. `run_command` sets `decision` in `_run_meta` but never `ok`, and `DENIED_RESULT` ("USER DENIED…"), `HELD_FOR_ADJUSTMENT` ("NOT RUN…") and `BLOCKED_RESULT` ("BLOCKED…") begin with none of the sniffed prefixes — so a denied **shell command** logged green too. Ten sites, one defect. Deriving both fields from one source also keeps them coherent: `ok` IS `status == "ok"`, and the envelope and `_run_meta` could previously disagree. `TestRefusalsAreNotLoggedGreen`, `TestRunCommandRefusalsAreRedToo`.

**The sharpest instance is the near-duplicate memory gate**, whose refusal begins "NOT saved — ": the gate that demonstrably WORKED in the #190 evidence was logging its refusals as successes. It now emits an `admission` record carrying `sim`/`floor`/`mode`/`against` via `save_memory(on_admission=…)` — a callback, not a changed return type, because the string return is what cli.py, curate.py and every test read — which is what finally makes `DEDUP_MIN_SIM` measurable. `TestNearDuplicateAdmission`.

---

## Two facts wearing one field name

`output` on a `tool` step meant **what the command printed**. On a denied one it meant **what the owner typed on the card**. Nothing on the record said which — a reader had to already know `decision` to read the field correctly, which is a field that cannot be read at the point a dossier reads it. That is the defect #323 fixed, and it is worth naming as a class rather than as a bug: whenever one key carries two facts, the log is not merely incomplete, it is confidently wrong, and it is wrong in the direction of the reader believing a command printed the owner's own sentence.

So the sentence became `comment`, a key of its own, on **every** path that refused or held because of one. It was already that on the write path; `run_command` used `output`, and the eight tool gates (plugin, egress, mail-link, remember, browse, the rule gate's owner card, the tool-file and rule-file writes, the skill import) left it only as prose inside the result text, which lands in `error`. `output` now means stdout on every path, including the empty string on a step where nothing ran.

**One carrier per shape, and one funnel.** A gate refusal already travels on `tools.ToolOutcome.meta`, which is correct on the parallel read path where an instance attribute would race, so `_gate_outcome` grew a `comment` argument and nothing else had to be plumbed. `run_command` and the writes already travel on `_run_meta`. Both are merged in `_emit_tool_step`, so the scrub and the cap are applied THERE — once, last, over both carriers — for the same reason the refused-decision rule and the `output` scrub are: a fix per site is a fix the next site does not inherit. Owner-authored is not the same as safe to store; the card is a text box he may have pasted a value into, and `agent._owner_comment` sends it through the same `secrets.scrub` as every other free text before capping it at `COMMENT_CHARS`. `TestApprovalCommentIsARecordedField`.

**The stop gate now records its own life.** The arming, each refusal it makes, and the clearing are `gate` records in the shape the contract had specified and nothing had written (§6.1). Each is emitted by the line that made the decision — `armed_by: "denial_comment"` is a CAUSE, and the only place it is earned is the `if comment:` branch inside `_arm_stop_gate`; `cleared_by: "text_only_turn"` only in the branch that tested `content and not tool_calls`. Assembling either afterwards would state a cause nothing checked. `max_rounds: 0` says the gate is unbounded rather than leaving the field out: it never lifts by exhausting a counter, which is exactly the distinction §6.2 says the skill gate still cannot make about itself.

**A held action's replacement carries `replaces`.** Approve + comment holds A and the model re-proposes B, and nothing joined B to A or to the sentence that caused it. `_emit_tool_step` registers a held call under its tool name and hands the id to the next call to that tool in the same turn. What the field asserts is only what was observed — *the first later call to the same tool while a hold was outstanding* — and not that the model actually reworked anything; the held call's own args are on the record, so that question is the reader's next lookup rather than the harness's guess.

**Nothing about a verdict moved.** A held action is still held even when the approval carried an edited `command`, a denial still arms the gate, and the gate still clears only on a text-only turn — pinned beside the records that now describe them, because a recording change that quietly moved a gate would be far worse than the gap it closed.

---

## The truncator with the largest blast radius

The per-task trim takes every prior tool output down to a 200-char stub. It is recorded, because a truncation nobody can see is indistinguishable from a model that ignored the result. `TestTrimIsRecorded`.

---

## Render errors are live-only, and deduped

A failure to render is a fact about the turn that WROTE the image — the only turn anyone can act on it in, which is why the model is handed the note solely when live. Reporting it on every read made it noise that COMPOUNDED: each report is a log write, and a log write reads as fresh activity, so a chat holding a dead image was marked unread by the act of reading it, forever. One had accumulated 53 identical records, up to four per open.

`noteRenderError` returns early when not live — the gate sits in that ONE function rather than at its call sites, so a future call site cannot forget it — and `_render_error` drops a report identical to `Session.last_render_error` as the backstop no client can route around.

A property of the transcript is not an event; only its arrival was. It renders NOWHERE, by both mechanisms above, so hot/cold parity holds by the same argument the `[aish: …]` notes make — and the user's signal is the broken-picture note already in the answer. The note goes through `Agent.add_system_note`, **not** `add_user_context`, because the latter calls `note_owner_hosts`, which would let a host in a model-chosen image src launder itself into egress provenance. `TestRenderErrorReports`.
