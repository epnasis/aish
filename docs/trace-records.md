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

## The truncator with the largest blast radius

The per-task trim takes every prior tool output down to a 200-char stub. It is recorded, because a truncation nobody can see is indistinguishable from a model that ignored the result. `TestTrimIsRecorded`.

---

## Render errors are live-only, and deduped

A failure to render is a fact about the turn that WROTE the image — the only turn anyone can act on it in, which is why the model is handed the note solely when live. Reporting it on every read made it noise that COMPOUNDED: each report is a log write, and a log write reads as fresh activity, so a chat holding a dead image was marked unread by the act of reading it, forever. One had accumulated 53 identical records, up to four per open.

`noteRenderError` returns early when not live — the gate sits in that ONE function rather than at its call sites, so a future call site cannot forget it — and `_render_error` drops a report identical to `Session.last_render_error` as the backstop no client can route around.

A property of the transcript is not an event; only its arrival was. It renders NOWHERE, by both mechanisms above, so hot/cold parity holds by the same argument the `[aish: …]` notes make — and the user's signal is the broken-picture note already in the answer. The note goes through `Agent.add_system_note`, **not** `add_user_context`, because the latter calls `note_owner_hosts`, which would let a host in a model-chosen image src launder itself into egress provenance. `TestRenderErrorReports`.
