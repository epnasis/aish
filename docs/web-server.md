# Web server — aish-web

`server.py` (~4.4k lines), `pty_session.py`, `email_poll.py`. The same Agent behind a Starlette WebSocket instead of a TTY.

**How to use this file.** The laws come first — six invariants that explain most of the code, cited by the sections below instead of re-argued. Then the four objects (`Bridge`, `Session`, `Client`, `WebServer`), the wire protocol, and one section per subsystem, each naming the test class that pins it. `tests/test_server_docs.py` fails if a route or a `Test*` class is documented nowhere.

Frontend behaviour is in `docs/web-frontend.md`; the Agent's own gates are in `docs/agent-core.md`. Full narrative history is in git.

---

## The laws

**L1 · The transport changes, the gate never does.** The web is a different way to *reach* the approval callbacks, not a different policy. `make_web_approvers` mirrors `cli.make_approver` exactly — denylist first, also on edited commands; auto-approval scoped to the live roots; per-session prefixes on the session's Agent; "Always allow" writing the same file the CLI's `a` writes. If a change would make the web approve something the CLI would prompt for, it is wrong.

**L2 · The loop thread owns shared state; the worker thread only computes.** Agent work runs off-loop and every callback marshals back with `call_soon_threadsafe`. Because the transcript append and all viewer-outbox pushes happen on the loop thread, there is no locking anywhere in the fan-out — that is what keeps a replay snapshot race-free. A new callback that touches a `Session`, a `Client` or the transcript from the worker thread breaks this silently.

**L3 · A parked worker must not starve the server.** A held approval blocks its thread indefinitely (`Bridge.ask` waits on a `queue.Queue`), so anything that can block on an approval or run user-command-long goes through `WebServer._in_worker` and its dedicated pool. Everything short stays on the default executor **by rule** — a few parked workers on the shared pool starved the very calls a client needs to attach and answer them, which is livelock with restart-only recovery.

**L4 · Live state is not transcript.** `SessionLog.reconstruct_events` is *defined* as "what a live session shows", so hot and cold must agree. Anything that is momentary — control role, the queue strip, cwd chips, a rename, a refusal — is emitted with `record=False` and re-derived on attach. Anything recorded must replay identically.

**L5 · The site that knows stamps it.** Provenance is written where it is known and read downstream, never re-derived: the fan-out stamps each live delivery with its session, `_refuse` stamps `code:"refused"` because the client cannot tell which channel an event came from, `kind:"origin"` records how a session was started, `_turn_event` mints the turn id. A consumer guessing from prose or shape is the bug this prevents.

**L6 · Fail closed, and unattended is not attended.** The access token always exists (a random one is generated when none is configured) and gates every surface; an unbuildable per-trigger model answers 503 rather than falling back to the server default; an unclean recipient parse counts as not-owner; a missing approver refuses. Origin is the axis: a `user` session behaves exactly as it always did, everything else is scoped.

---

## The objects

### `Bridge` — one per session, the thread boundary
`run_task` runs via the worker pool; callbacks emit JSON events through `call_soon_threadsafe`; the approval callbacks block the worker on a `queue.Queue` slot until a browser answers (L1, L2). `emit` is the recorded path, `record` appends to the transcript, `_put` fans out to every viewer's outbox and stamps the session name (L5) without overwriting an event's own `session`. `answer`'s pending-slot guard drops the duplicate when two viewers answer the same card. `ask` calls `on_wait(event, has_viewers)` right before blocking — the hook hold notifications hang off (`TestBridgeOnWait`).

### `Session` — Agent + SessionLog + Bridge + busy flag
Carries `origin` + `trigger_meta`, its `viewers` set (owned by the bridge), its `queue` of messages typed while busy, `pending_cwd`, `controller`, and `open_turn` (which mints the turn id `[REDACT]` needs while the turn is still live). Capped at `MAX_OPEN_SESSIONS` with idle eviction — `_evict_idle` never closes a busy session, one with viewers, or the default.

### `Client` — one per socket
Its own `ws`, `outbox`, `sender` coroutine and independently chosen `viewing` session, tracked in `WebServer.clients`. Connections coexist WITHOUT preempting: `_attach` does not close the previous socket, a bare connection lands on `WebServer._default` and a `?session=` one on that session (`TestMultiConnection`, `TestConnect`). Per-connection state that used to be singletons on `WebServer` lives here; `WebServer.active` survives only as a representative-session helper for HTTP endpoints, which have no socket context.

### `WebServer` — the process
Holds the sessions, the clients, the single console, the worker pool, the trigger guards and the token. `create_app` injects everything tests need to vary (console command, trigger limits, public URL, base cwd).

---

## Threading and scheduling

Three thread contexts, and mixing them is the recurring defect: the **event loop** (owns all shared state, L2), the **worker pool** (`WORKER_POOL_SIZE`=32, threads named `aish-worker-*`, reached only through `_in_worker`, L3), and the console's **reader thread** (marshals back with `call_soon_threadsafe`, same discipline). 32 is several times `MAX_OPEN_SESSIONS` + `MAX_CONCURRENT_TRIGGERED` + restart-resumes; idle threads are near-free. `shutdown()` releases the pool with `cancel_futures=True` **after** the deny loop has unblocked every held approval, or shutdown deadlocks on a parked worker (`TestWorkerPool` pins the routing both ways).

Cold opens are **single-flight**: concurrent `_open_by_name` callers on one name share one Future and one Session (`WebServer._opening`), so a restart-recovery resume racing a reconnecting PWA cannot orphan a session whose approvals could never be answered (`TestConcurrentColdOpen`).

---

## The wire protocol

Two kinds of client message, and the distinction is the control model. **Action** messages call `_claim(client)` first, stamping `Session.controller` and broadcasting a non-recorded `role` event when it changes. **View** messages never claim.

| | messages |
|---|---|
| **action** | `task` · `command` · `step` · `stop` · `retry` · `approval` · `write` · `tool` · `import` · `cd` · `add_dir` · `new` · `fork` · `rename_session` · `delete_session` · `redact` · `set_model` · `create_issue` · `console_*` |
| **view** | `resume` · `peek` · `sessions` · `models` · `files` · `jobs` · `dequeue` · `dequeue_cwd` · `share_drop` · `render_error` · `client_debug` |

**Receipts (`rid` → `ack`, #210).** A client cannot tell a request that was handled from one that was never heard: the socket reports OPEN long after it died, accepting everything and answering nothing. So any message may carry a client-minted `rid`, and `_handle` echoes `{"type":"ack","rid":…}` once the handler returns. Three properties are load-bearing. It is stamped at the **dispatch point, not in each handler** — a handler that forgets is a silent hole, and this way a new message type is receipted before anyone thinks about it. It is sent **after** the work, so a client that hears the receipt knows the events it was owed have already gone out, and a handler that RAISES sends none — an action that blew up did not happen, and the client must hear that as loudly as one that never arrived. And it is a **delivery** fact, never a semantic one: "handled", not "succeeded", including for a refusal (a refusal is an answer, and the client must stop waiting on it). What actually happened stays with each feature's own events. The client half — which requests must carry one and what an unreceipted one undoes — is `[ACK-LEDGER]` in `docs/web-frontend.md`.

Control is **last-actor-drives**: no locked role, no disabled UI. The approval card fans out to every viewer, any may answer, and the asyncio loop serializes messages so exactly one `answer()` reaches the blocked worker. On disconnect (`_detach` → `_leave`) the client leaves the viewers and, if it was controller, releases control. The `role` event is never recorded (L4) — cold replay re-derives control from live membership.

Switching a client's view is `hello` + full transcript replay (`_show`), which is also what makes phone lock/unlock lossless. A session that fell out of memory is reopened cold from its log (`_cold_open`) and reconstructed into the same event stream, so its activity trace is identical hot or cold.

---

## Sessions

**Open, list, switch.** `_hello` carries model, session scope, recency list, busy/role and peeks (`TestConnect`). `_send_sessions` labels each row with its working directory (`_row_cwd`) — read LIVE from the agent for open sessions, otherwise from the log's last `kind:"cwd"` record, so the label does not depend on which few sessions survived the eviction sweep. A session sitting in `WebServer.base_cwd` shows NO label: stamping the same path on every row is noise, so the chip's *presence* is the signal "this chat is somewhere else". Rows also report waiting-for-approval state (`TestSessions`). `_peek` answers another session's transcript snapshot WITHOUT switching or claiming — nothing recorded, a miss answers `gone` on the peek rather than the resume path's error (`TestPeek`).

**New, fork, rename, delete.** A new chat inherits the model the client is currently using, like the consumer apps; the saved default applies only at server start (`TestModels`). `_fork_session` branches into a NEW session seeded with the conversation up to a point, and sets `retitle_forced` so the fork earns its own name instead of wearing its parent's (`TestFork`, `TestAutoTitle`). `_rename_session` works on cold sessions from disk too, rejects empty titles and path escapes, and the latest rename wins across a reconnect (`TestRename`). `_delete_session` removes the conversation AND its command history permanently — the ONE sender is the frontend's shared confirmation modal (`TestRedactTurn` covers the finer-grained alternative below).

**Redact a turn** (`_redact_turn`): refused while the chat is busy — the turn may be the one running, its records are still being written, and `agent.messages` belongs to the worker thread (L2). Order is log → model context → transcript. The transcript is REBUILT from the scrubbed log via `reconstruct_events` rather than edited in place: that function is defined as what a live session shows (L4), so leaning on the parity invariant both repaints correctly and guarantees no residue survives in memory for the next viewer. The fresh `replay` goes out through the BRIDGE so every viewer repaints — a removal on the phone must not leave the laptop showing the text — and carries `seen: true`, true by construction because it reaches only clients currently viewing, without which the removal (which counts as activity, so every mirror refetches) comes back as an unread dot for the person who just made it. `TestRedactTurn`.

**Titling** (#175). `_maybe_retitle` runs at the end of a successful task, gated by `_retitle_due`: never when a hand-typed name exists, always when `retitle_forced`, otherwise at turns 1, 3, 7, 15 … and only when the chat has drifted from its current title. `_title_source` names from the first two and last two messages, not the whole transcript. `_model_session_title` calls `agent.chat` directly — no `run_task` — so the naming turn never enters the conversation or the log, and every failure path keeps the existing name. The new name broadcasts as `session_renamed` with `record=False` (L4). `TestAutoTitle`.

---

## Turns

`_launch` routes typed text three ways, checked in this order: a `!` prefix runs it directly as the user's own shell command — no model, no approval gate, mirroring the CLI's `!` escape — so a general `!command` never reaches the model (`!cd` stays the `/cd` alias and is dispatched inside `_run_user_command`); then slash handling; then the model task path (`TestBangCommands`). `/learn` and `/feedback` are rewritten server-side into their flow prompts while other slash text passes through verbatim (`TestLearnCommand`).

`_run_task` brackets the turn on disk, emits `task_start`/`task_end`, and awaits `_notify_done`. `_start_task` refuses while busy by emitting `queued` through the bridge; `_stop_task` cancels a task even when it is parked on an approval; `_retry_task` regenerates the last answer from scratch, with `_rollback_transcript_to_last_user` dropping everything after the last `user` event first (`TestStopAndQueue`, `TestRetry`, `TestModelError`). `_finish_turn` is the shared end-of-turn drain for both the model and `!` paths.

Approvals ride the same cards over the socket for commands, writes and mutating plugin tools, including the #81 comment semantics — a comment on deny STOPS, a comment on approve HOLDS the original and reworks it (`TestCommandApproval`, `TestWriteApproval`, `TestToolApproval`). `run_command` is framed by recorded `command_start`/`command_end` events so the browser can draw a bounded terminal block and a reconnect replays it identically (`TestTerminalFraming`).

**Narration (#212).** A turn delivers more than once, so `on_delivered` emits a **recorded** `delivery` event ending each interim message the model wrote alongside its tool calls. Recorded, not live-only (L4): `reconstruct_events` replays the same event from the assistant records, so hot and cold agree — and the merging in `_put` cannot glue an interim message onto the answer, because only consecutive `token` events merge and a `delivery` sits between them. It carries the text as well as the boundary, for the client that never saw the stream: a rule-bound turn cannot stream narration at all (Verify buffers every token), and a mid-stream replay drops the stale live tail. See `docs/agent-core.md` for the agent half and `[DELIVERY]` for the client's.

**Turn identity and time.** `_user_event`/`_turn_event` stamp `ts` (epoch seconds) on every LIVE `user` event so a browser landing mid-turn learns the origin of the clock, and mint the `turn` id the delete chip names (L5). Cold replay is deliberately unstamped — it closes every turn it replays, so it can never land a reader inside a running one (`TestTurnOrigin`, `TestTurnTimestamps`).

**Quick replies.** A final answer ending in a question with no chip gets a deterministic fallback set (`apply_quick_reply_net`); `[no-chips]` opts out and is stripped. The system prompt forbids terminating chips ("Thanks, that's all") — the user can end a chat at any time (`TestQuickReplyNet`, `TestQuickReplyPromptGuidance`).

---

## Live state, not transcript (L4)

Everything here is `record=False` and re-derived on attach; `reconstruct_events` emits none of it.

- **`role`** — who last acted. Re-derived from live membership.
- **The queue strip is backend-authoritative, all of it.** A chip names a message ONE chat's agent is holding, and Remove dequeues from whatever session the client is VIEWING. It used to be sent only to the client that typed it, which holds right until that client looks at a different chat: the chips live outside the transcript, nothing cleared them on a switch, so they followed the viewer — there Remove dequeued from THAT session (a no-op) while the real message ran on where it was queued, and Edit pulled the text into the new chat's composer while the old chat still held its copy, one send from running it twice. The pending-cd card in the same strip had been reconstructed on attach since #92, so half the strip was already right. Now `_show` re-sends the real `session.queue` after the replay (so a second device sees the queue at all), `_start_task` emits `queued` through the BRIDGE so it carries the session stamp the firewall needs, and `_dequeue` announces `dequeued` the same way — a cancel on the phone takes the chip off the laptop. `TestQueueIsBackendAuthoritative`.
- **`cwd_changed` / `cwd_dequeued`** — the top-bar chip and the pending-cd card.
- **`session_renamed`** — cold replay re-reads the title record instead.
- **A refusal is not a failure** (`_refuse`). An `error` event said two unrelated things and the client could only assume the worse. A TURN FAILURE ends the turn: the live trace closes, busy clears, Retry appears. A REFUSAL of the request just made says nothing about the turn — but went out in the same shape, so `_redact_turn`'s correct "can't delete a message while this chat is working" **tore down the running turn's card**, taking Stop and Retry with it and stranding a task with nothing on screen to reach it. The split already existed in the code and simply was not carried across the wire: a turn failure goes through `session.bridge.emit` (recorded, fanned to every viewer), a refusal goes down the ONE socket that asked and is never recorded. `_refuse` stamps `code:"refused"` at the site that knows (L5); the client shows a toast without touching turn state. `_gone_error`'s `no_such_session` is the pre-existing instance of the same idea. `TestRefusalIsNotAFailure` pins both halves — including that no `bridge.emit` error ever carries the code, which would downgrade a real failure to a toast and hang the turn busy forever.

**The roster plane — session state is PUBLISHED, not polled (#204).** A second event plane, deliberately separate from the `Bridge`. The Bridge carries events belonging to ONE conversation, delivered to whoever is reading that conversation, and replayable as its transcript; a roster fact — "chat A is running now" — is none of those three. Its audience is every client whatever it is viewing, the session firewall would correctly drop it as belonging to another chat, and recording it would put it in a transcript it is not part of. Not really a new channel either: the status pushes it replaces already bypassed the Bridge and wrote straight to each socket. This names it and gives it the three things it lacked.

1. **One publisher.** Every transition calls `_touch`, which builds the row, diffs it against the last row broadcast for that session, and publishes on a change. Before this, exactly two transitions were announced (a chat going idle; a chat holding with no viewers) — so a chat STARTING was never announced, nor an approval ANSWERED elsewhere, which is why a phone went on showing *Needs approval* for a card cleared on the laptop, and a triggered job showed as idle everywhere until something asked.
2. **It diffs**, which makes calling it too often FREE. That is what turns the rule into *"call it whenever you touched a session"* — a rule that survives contact with new code, unlike *"announce at exactly the right moments"*, which four repairs' worth of evidence says nobody can keep.
3. **A sequence number** on every event, on the hello and on the snapshot. A gap means a delta was lost and the client asks for a snapshot; without it the client cannot tell *nothing changed* from *I missed something*, which is the same silent drift in a new costume. The snapshot carries the seq it was taken at, so one in flight cannot overwrite newer rows.

`_roster` holds what has been BROADCAST, never what is true: a snapshot goes to one client and tells the others nothing, so seeding it from one would make the next transition diff clean and never reach them. The row carries only what the server holds IN MEMORY (`name, title, state, origin, cwd`) — deriving timestamps here would mean parsing the session's log, and a chat that just did something has just changed its file, so the parse cache is guaranteed to miss at exactly the moment a transition fires. The client stamps arrival time instead, which is also the more honest clock: unread compares against THAT device's seen map. `notice` marks the two transitions worth interrupting someone about and is never part of the diff; the client suppresses it for the chat it is viewing. `session_deleted` rides the same broadcast — a chat deleted on the laptop used to sit on the phone's list until it refreshed. `session_state` is retired; the client keeps its handler so a new client against an old server degrades rather than going silent. `TestSessions::test_background_hold_sends_waiting_notice`, `test_a_hold_someone_is_watching_is_not_announced`, plus `tests/js/test_roster_plane.js`.

**Hello rows carry liveness (#203).** Every `_show` already scans the recency list for the swipe peeks; those pages now also carry each chat's `state` from the in-memory `self.sessions`, which is what lets the client re-derive its attention count on every boot, reconnect and switch instead of only when the rail is opened. Free — one dict lookup per page, no extra disk work. `test_hello_pager_rows_carry_liveness`; client half in `[ATTENTION]`, `docs/web-frontend.md`.

**Session firewall (#182).** The fan-out stamps every live delivery with the bridge's session name and the client drops a stamped event naming anything else, closing the switch window where the old session's tail kept rendering into the new view. Exempt: `hello` (the switch mechanism), `session_state` (sent only to non-viewers), `session_renamed` (by name, idempotent). The stamp rides ONLY the delivery — the recorded transcript stays byte-identical to `reconstruct_events` (L4), and replayed events bypass the gate anyway. `TestSessionStamp` + `tests/js/test_choreo_session_firewall.js`.

**Ghost trace cards.** A record kind with no renderer does not degrade to "renders nothing" — it opens an empty live trace card with a running ticker. Renderless governance records therefore need both halves (log-only emit AND membership in `RENDERLESS_STEPS`); `TestNoGhostTraceCards` verifies it where the artefact would actually appear, live and on cold replay. See `docs/trace-records.md`.

**Render errors.** `_render_error` writes a durable `kind:"render_error"` trace record and hands the model a note ONLY when the failure was in a live turn — merely opening an old chat whose images were evicted must not grow it a turn — and drops a report identical to the last one as the backstop no client can route around (`TestRenderErrorReports`).

---

## Mid-task steering and output

**Steering (#95).** A `/cd` or a message typed while a task runs is applied BETWEEN steps, not only after it. The agent's step loop polls two get/drain callbacks at the top of each iteration (`check_pending_cwd`, `check_pending_messages`, wired over `session_holder`) — lock-free because the event loop is the sole setter and the worker thread the sole consumer (L2). `check_pending_cwd` get-and-clears `Session.pending_cwd`; the agent rebases and rebuilds `messages[0]`. `check_pending_messages` drains the text-only queue items and injects each as a mid-task user turn: an echo plus a distinct `injected` trace step that renders live AND replays, with the text appended straight to `self.messages` and NOT logged as a conversation record, so cold replay does not split the turn. Trade-off: injected steering is not carried into `--resume` history. Items with attachments — and `!` commands, which must run through the user-direct path, not as model prompts — stay queued and run as a normal follow-up at `_finish_turn`. Every cwd move flows through the single `on_state("cwd", …)` handler, the ONE place that emits the `workspace` marker and retires the queue card. claude-max runs its own loop, so mid-task polling does not apply there. `TestReconnect`, `TestWorkspace`.

**Live-output coalescing (#109).** `StreamCoalescer` batches `run_command`'s per-line output into fewer `stream` events (50 lines / 16 KB / 0.1 s / command end) so huge output cannot lock the tab with per-line DOM appends. LIVE-ONLY — logged output and cold replay are untouched and the frontend re-splits on `\n`, so hot/cold parity holds (L4). `TestStreamCoalescer`.

**Skills are rescanned per task**, so a skill added after boot is advertised without a restart (`TestSkillsRefresh`).

---

## Triggered sessions and ingress

A `Session` carries an `origin` (`user`|`schedule`|`email`|`webhook`) + `trigger_meta`, persisted as a `kind:"origin"` log record (L5) so a cold-reopened automated session keeps its provenance; it rides the listing and draws the provenance glyph on a rail row (`TestOriginPersistence`). **`POST /trigger`** (`handle_trigger`) is the programmatic ingress: `open_session(None, origin, meta)`, `add_session(default=False)` so an overnight trigger never becomes the bare-connect landing session, then `_run_task` on the session's own bridge with no client — events buffer into the transcript, observable when the owner opens it (`TestTriggerEndpoint`).

**Ingress hardening (#178 P1-10).** Three guards run after the token/origin gates and BEFORE any session exists: `meta.dedup_key` idempotency (a repeat POST answers 200 with the existing session name + `deduped:true`; a bounded in-memory LRU+TTL — durable cross-restart dedup deliberately stays at the SOURCE, the poller's Gmail label; no key → every POST fires, so existing clients are unchanged), a per-origin token bucket, and a cap on concurrently RUNNING triggered sessions. Both limits answer 429 + `Retry-After`, safe by the poller's contract: a message is marked processed only AFTER a successful trigger, so a refused delivery retries next poll. All three knobs are `create_app`-injectable (`TestTriggerHardening`).

**Per-trigger model override (#186).** The payload may name a `model`; `open_session(model_override=…)` builds it FIRST — before `SessionLog.new`, so a bad spec leaves no orphan log — and swaps chat/model/provider on the fresh Agent. It exists for privacy-scoped automation and therefore FAILS CLOSED (L6): an unbuildable override answers 503, never a silent fallback to the server's possibly-cloud default, which would be exactly the leak the override prevents. Resumed sessions ignore it; a claude-max server refuses it.

### Unattended capability policy

Draft-and-hold, built out of the existing gate: `_triggered_safe` plus a branch atop `approve_tool`, wired through `make_web_approvers(..., get_origin=…)`. In a non-`user` session, SAFE mutations auto-run with no card — `TRIGGERED_SAFE_TOOLS` (relabeling), and `gmail_send`, draft or live, when **every recipient is the owner**. That is **recipient-scoped autonomy**: a send that can only reach the owner's own inbox carries no prompt-injection exfiltration risk. A reply with no explicit `to` is NOT auto-safe — its recipient is not verifiable from the args. Every other mutation (any send or draft addressed beyond the owner, trash, filter-create, drive share) falls through to the normal card, which simply BLOCKS pending until the owner opens the session and answers — which is what makes the notification below load-bearing rather than a nicety. `TestTriggeredCapabilityPolicy`.

**Recipient validation is a parse, never a regex find (#178 P0-3).** `_parse_recipients` uses `email.utils.getaddresses`, rejects quoted and multi-`@` local-parts and any field that re-serializing does not reproduce, and any unclean parse counts as not-owner (L6). The old `findall` passed `"pawel@wenda.eu"@evil.com` — valid RFC 5322, routes to evil.com — as owner-only. Drafts are checked like live sends, because a fully-addressed draft is one tap from sending. `test_adversarial_recipients_never_pass_as_owner` pins the table.

**`email_poll.py`** — the Gmail→`/trigger` poller. Polls the bot mailbox via the `gws` CLI for inbox mail FROM the owner's addresses and requires `dmarc=pass` from Gmail's own Authentication-Results (SPF alone validates the envelope, not From), builds the recipient-scoped-autonomy prompt, and POSTs `origin=email` with `meta.dedup_key` = the Gmail message id. Durable dedup is the `aish-processed` label, applied ONLY after a successful trigger — a failed POST or a 429 leaves the message unlabelled so it retries next poll; a label-ensure failure aborts the whole run to avoid a re-fire loop. Token from the Keychain with an env fallback. A message from a disallowed sender or one failing DMARC is marked processed WITHOUT triggering — dropping it silently would re-read it forever (`TestSkips`). A missing token refuses to run at all rather than posting unauthenticated (`TestTrigger`). The prompt itself is pinned by test, so the owner address and the auto-approve contract it states cannot drift from what `_all_recipients_owner` actually enforces (`TestPrompt`).

Testable by construction: both effectful edges are `run_poll(gws=…, post=…)` parameter seams, so `tests/test_email_poll.py` needs no subprocess and no network. Its `trigger()` helper takes an `origin`, which is how `curate.py` posts `origin="schedule"`.

---

## Notifications

`notify.py` sends Pushover (credentials from the Keychain; unconfigured or failing is a silent no-op that NEVER raises into the approval path). Two triggers, **both only for non-`user` origins and both gated on nobody viewing** — an open tab already shows the card, and without the viewer gate every message the owner types into an automated session would push a phone notification for work being watched live.

1. **A held approval** — `Bridge.ask`'s `on_wait` hook fires right before blocking; `notify_hold` pushes `_describe_hold(event)` on the worker thread inside `ask`'s try/except (`TestHoldNotification`, `TestBridgeOnWait`).
2. **A triggered session finishing** — `_notify_done`, offloaded via `asyncio.to_thread` so the loop never blocks (`TestDoneNotification`).

Both are **priority 0** deliberately: priority 1 is the one level that ignores quiet hours and always sounds, turning an overnight hold into a 2am alarm — and since a held worker waits indefinitely, the hold is still there in the morning. Priority 2 (the only level that re-alerts until acknowledged) needs `retry`/`expire` and is never used. Both carry a deep link `{public_url}/?session={name}`; empty public URL → no link, still notifies.

---

## Restart recovery (#164)

An in-flight task is bracketed on disk by `SessionLog.task_start(prompt)` / `task_end()` from `_run_task` — **web only**, because a CLI session dies with its terminal and must never be resurrected, and the `!` path writes NO marker, since re-running the user's own shell command unattended is a risk, not a recovery. A killed process cannot write anything, so an UNMATCHED `task_start` IS the interruption evidence: no heartbeat, no timer.

`startup` fires `_resume_interrupted` as a background task (never on the startup path). Candidates are scanned newest-first within `RESUME_WINDOW` (12 h) and each is cold-opened through the SAME `_open_by_name` a user's switch uses, so the resumed session replays its full prior transcript, then relaunched with no client — identical to a trigger.

The resumed turn is `RESUME_NOTE`, **not** the original prompt: the request and the partial work are both already in the restored history, so re-issuing it would invite the model to redo side effects it may have completed, like an email already sent. The one exception is a run killed before its own user message was logged — there is nothing to continue from, so the recorded prompt is re-issued. Two things make "continue" real rather than nominal: `keep_history=True` swaps the eager per-task tool-output trim (200-char stubs would gut exactly the results the resume exists to preserve) for an oldest-first trim that runs only as far as the budget demands; and `pending_task`'s `in_flight` list — a `tool_start` with no matching `tool` — is appended to the note, naming the steps whose effect is genuinely UNKNOWN. That is the honest boundary: a completed step logged its result, an in-flight one may or may not have taken effect, so the model is told to verify those specifically instead of re-running the task. `RESUME_MAX_ATTEMPTS` (3) abandons a task that keeps killing the server instead of crash-looping it; `RESUME_MAX_SESSIONS` (3) stops a mass restart stampeding the backend. `TestRestartResume`.

---

## Shipping a new build (#221)

`make ship` (`scripts/ship.sh`) is the ONE local path from a checkout to the running service: guard → lint → tests → `uv tool install` → `launchctl kickstart` → health-check. `scripts/deploy-web.sh <host>` is the remote equivalent.

**The guard is the reason it is a script at all.** `uv tool install` builds the wheel from the **WORKING TREE, not from HEAD** — verified, not assumed: an uncommitted line in `aish/static/app.js` was found inside a freshly built wheel. So a bare install silently ships whatever happens to be uncommitted, and there is nowhere to put a check when the ship step is a command pasted from a doc.

That is not hypothetical. On 2026-08-11 the installed `app.js` hash matched a **dirty** checkout rather than any commit — another session had installed its in-progress frontend to test it live. A routine reinstall from that tree would have shipped 129 uncommitted lines that were simultaneously failing two doc-gate tests, and reverted the live UI under the session driving it. The near-miss was caught by comparing hashes by hand, which is exactly the check a machine should be doing.

So: a dirty tree **refuses**, naming the files and separating those that land in the wheel (`aish/`, `pyproject.toml`) from those that do not — a refusal that cannot tell a stray README from a modified module just teaches people to reach for `--dirty`. Untracked files count, and are the sharper case: hatchling packages off disk, so a new uncommitted module ships like any other while `git diff` shows nothing. A non-`main` branch warns but does not block. `--dirty` is the deliberate override.

The remote script keeps working-tree shipping as its normal mode — that is the point of a remote dev loop — but prompts when the tree is dirty, and with **no tty to prompt on** it refuses rather than treating silence as consent. That non-interactive branch is the one that matters: the failure above happened while nobody was watching a terminal.

`--check` runs the preflight and stops before anything is installed or restarted, which is what makes the guard testable — `TestShipGuard` drives the REAL script against throwaway git repos, never a Python restatement of its logic.

Health-checking probes whatever address the kernel says is listening rather than assuming loopback: this service binds the LAN address, so a `127.0.0.1` probe reports a false failure on a perfectly healthy restart.

---

## The global console

**ONE** persistent interactive pseudo-terminal for the WHOLE server — not per-session — for TTY-reading programs (`gcloud auth`, `ssh`, `sudo`) the non-interactive `!` path cannot drive. It floats above whatever chat is shown and is untouched by chat switches. Held on `WebServer.console` with a global `console_viewers` set — never on a Session, never on the agent.

Message shapes in: `console_open` (no command — the console's command is fixed) · `console_in{data}` · `console_resize{cols,rows}` · `console_close` (HIDE, keep running) · `console_kill` (destroy) · `console_share{text}`. Out: `console_started{command,cwd}` · `console_out{data}` · `console_exit{code}` · `console_error{text}` · `console_shared{text}`.

**tmux-backed for restart survival.** The PTY runs `tmux new-session -A -s aish-console` (attach-or-create), so the shell lives in tmux's detached server process and outlives `aish-web`; our `PtySession` is merely a tmux CLIENT, and the first open after a restart reattaches to the surviving session. Detected via `shutil.which("tmux")`; absent, it spawns `$SHELL` directly — still global, cross-chat and cross-disconnect, but not restart-surviving. The spawn command is injectable, so tests use a trivial echo loop and need no tmux.

**Lifecycle.** NEVER killed on viewer-leave, `console_close`, session close, eviction or disconnect. Only `console_kill` (which also runs `tmux kill-session`, or a reopen would silently reattach to the killed thing) or graceful `shutdown` (which kills only the PTY — the tmux SESSION survives) end it.

**Broadcast.** One PTY, many viewers: `console_in` from any client writes it, `_console_out` fans out to every client in `console_viewers`, pushed straight to each outbox on the loop thread — never through a session bridge, so console I/O is never recorded in any transcript. A second viewer of a running console gets a `tmux refresh-client` poke so its blank xterm repaints with current state (`PtySession.tty` exposes the slave device path for exactly this). Multi-viewer resize is last-resize-wins: tmux sizes the pane to its single client.

**Load-bearing security invariant: the model has NO write path to the console.** `PtySession.write` is reached ONLY from `_console_in` — the user's own socket — and `agent.py`/`tools.py` never reference the PTY layer (asserted by `tests/test_pty.py`). Console I/O stays out of transcripts, cold replay and model context UNLESS the user selects text and taps Share, which injects it through the currently-viewed session's `agent.add_user_context` — the same user-message path as `!`, logged for `--resume`, launching no task. `TestGlobalConsole` covers the wiring, globalness, survival and broadcast.

**`pty_session.py`** — a subprocess on a `pty.openpty()` pair with `start_new_session=True` (setsid → controlling TTY), streamed over `on_output`/`on_exit` callbacks the class guarantees to invoke on the loop thread (L2). The reader thread uses `select` with an idle-flush to coalesce bursts plus an incremental UTF-8 decoder, since multibyte characters can split across reads. `write()` is the sole input path. `kill()` signals the whole process group, closes the master fd idempotently and reaps; `on_exit` fires exactly once. No agent or model reference, by construction.

---

## HTTP surfaces

| route | what it serves |
|---|---|
| `/` · `/index.html` | the app shell (`serve_index`), stamped with the asset revision |
| `/ws` | the WebSocket |
| `/static/{path}` · `/fonts/{name}` | assets; fonts via `serve_config_font` |
| `/file` | one image, scoped to `_workspace_roots()` |
| `/pdf/info` · `/pdf/page` | a PDF's page count, and one page rasterised — same scope |
| `/download` | one file, saved to the device — same scope, narrower rule |
| `/upload` | `POST ?name=<filename>`, raw body |
| `/share` | `POST ?name=&text=&source=`, raw body — the share-sheet inbox |
| `/dirs` | the folder browser |
| `/export/answer` · `/export/session` | PDF |
| `/offline/index` · `/offline/session` | the offline mirror |
| `/trigger` | programmatic ingress |

**Gates.** The token always exists — generated randomly per run when none is configured, printed by `main()` in the launch URL, compared with `hmac.compare_digest` — and EVERY surface above except the shell and its assets requires it (`TestTokenGate`, `TestUnconditionalToken`). `origin_allowed` additionally rejects any browser request (WS handshake, `POST /trigger`) whose `Origin` host:port differs from the request's own `Host`; a missing Origin (curl, the poller) passes, since browsers always send one on the cross-origin requests that are the drive-by vector — WebSockets are exempt from same-origin and a text/plain POST needs no preflight, which is what makes this necessary (`TestOriginGate`). `/trigger` is token-gated unconditionally: the old loopback fallback is gone, because a same-host reverse proxy makes every request look loopback (L6).

**Security headers.** `SecurityHeaders` (pure-ASGI, every HTTP response) stamps `Content-Security-Policy` and `Referrer-Policy: no-referrer`; WebSocket scopes pass through untouched. `img-src` is self/data:/`CSP_IMG_HOSTS` — the YouTube-thumbnail and static-maps whitelist mirroring export.py — which kills the zero-click `![](https://attacker/…)` channel; the SAME whitelist is enforced in the renderer itself (`[INLINEIMG]` in app.js) so a non-whitelisted image degrades to a link on every path rather than relying on the header being present. `connect-src` names `ws://`+`wss://` of the request's own Host, sanity-checked by `_HOST_OK_RE`, so the socket works behind the wss proxy; `script-src 'self'` holds because index.html has no inline scripts. `TestSecurityHeaders`.

**Load-path weight.** `GZipMiddleware` (outermost, min 512 B) compresses every HTTP response — the critical path was 560 KB raw and now ships ~172 KB, which matters because the service worker's 3 s network-first navigation race is decided on exactly that first fetch. `index.html` links `style.css` in BODY below the boot loader, not HEAD: a pending HEAD stylesheet blocks the first paint entirely, so the inlined spinner never showed on a slow link.

**`/file`** serves images the model generated so they render inline, scoped to `_workspace_roots()` — `roots` + the media store + the scratch dir — which is the ONE boundary the PDF exporter uses too. They disagreed before #188: the exporter trusted the scratch workspace and `/file` did not, so a file the model wrote where it is TOLD to write throwaway files printed fine in a PDF and 403'd in the chat with nothing saying why (`TestFileEndpoint`, `TestImageRootsAgreement`).

**`/pdf/info` and `/pdf/page`** (#218) let an attached PDF be looked at, not just read about: `info` answers `{name, pages}` and `page` rasterises one page as a PNG, which the client shows in the photo viewer it already has (`[PREVIEW]` in `docs/web-frontend.md`). Both go through `_pdf_target`, which applies the `/file` rules unchanged — token, absolute path, symlinks resolved BEFORE containment, inside `_workspace_roots()` — plus a `.pdf` suffix and the `%PDF` magic bytes, the same guard `read_pdf` applies to a fetched file, because a `.pdf` that is not a PDF is usually a login wall. **One helper for both**, so a rule cannot be enforced on the page and forgotten on the count. Rendering runs in `asyncio.to_thread` (PyMuPDF is CPU-bound and the loop serves every socket), and pages are **never stored**: the media store holds what the MODEL was shown, and a page somebody swiped past is not that. Repeats are cheap because the answer caches — an upload's bytes cannot change, so its pages are `immutable`; anything else revalidates on an ETag carrying the file's mtime. A page that cannot be produced (out of range, encrypted, corrupt) is a 404 naming which, so the client can show words instead of a broken-image glyph. `TestPdfPreview`.

**`/download`** is how a file gets off the chat and onto the device — the same gate as `/file` (token, absolute path, symlinks resolved before containment, `_workspace_roots()`) plus a rule about WHICH files that is deliberately narrow and states itself: **you may save what aish can already show you, and what you attached.** Images and PDFs are the first half (`/file` renders one inline, `/pdf/page` the other page by page); the uploads dir is the second. Everything else in the roots stays unreachable, because the roots are also a project tree nobody has ever been shown — a `.env` beside the code answers 415 here exactly as it does on `/file`, and a download must not be the looser door standing next to the image endpoint. Every response is `Content-Disposition: attachment` + `nosniff`, so an uploaded `.html` is saved rather than rendered as same-origin markup; the filename is sent in both forms (`_attachment_disposition`), the quoted ASCII one REPLACING what it cannot represent rather than dropping it — a name reduced to `----` says nothing about what you just saved — and that same pass is what keeps a quote or a newline in a filename from ending the header early. `TestDownloadEndpoint`.

**`/upload`** takes a raw body with the filename in the query — no multipart, so no extra dependency — rejects bad names, lands the file in the session's roots, and `_classify_attachments` splits the result into native images, native documents and text notes, with only files inside the uploads dir eligible to go native (`TestUpload`). The note text it appends to the turn is written for the MODEL and must never be rendered to the owner; the frontend parses these exact strings to strip them (`[ATTACHMENT-NOTES]` in `docs/web-frontend.md`), which makes the format a cross-language contract with no shared code behind it — so it is pinned from both ends, by `TestAttachmentNoteFormat` here and `test_attachment_notes.js` there.

**`/share`** is the iPhone share sheet's way in (#213). iOS cannot register a PWA as a share target — Web Share Target is Chromium-only and Safari implements only the outbound half — so there is no manifest entry to add and no amount of frontend work that would produce one; sharing to aish is a **Shortcut** with "Show in Share Sheet" that POSTs here (recipe in the README). `name` decides how the raw body is read, and that is the whole interface: WITH a name it is a file (exactly as `/upload`), WITHOUT one it is text, capped at `SHARE_TEXT_MAX` with an announced truncation. The second form exists because Safari shares a URL rather than a file, and percent-encoding a shared link into a query string inside Shortcuts works right up until the link contains an `&`. `chat=new` marks the item as wanting a chat of its own — advisory, recorded here and acted on by the client, because iOS gives no way to open an installed web app at a chosen URL. The query also carries optional `text` (fine for short things) and a free-form `source` label; either a file or text is required and both may be sent. `_store_upload` is the single writer shared with `/upload`, so a shared file is indistinguishable from a picked one everywhere downstream.

**What arrives is PARKED, never run** — that is the whole security argument for the endpoint. It stores the item, prunes the inbox (`SHARE_MAX_ITEMS`, `SHARE_TTL_S`) and broadcasts the list; it opens no session, calls no model and executes nothing. The share sheet stages work for the owner to pick up, and does not become a way for any app on the phone to start an unattended agent (contrast `/trigger`, which does exactly that and is rate-limited and origin-gated for it). The inbox is persisted to `<state_dir>/shares.json` because the normal case is a share arriving with nothing connected and aish-web restarting under launchd before it is claimed; an unreadable file starts an empty inbox rather than failing to start. `hello` carries the list for that same reason — the broadcast alone would only reach a tab that happened to be open. `share_drop` (a VIEW message: it acts on the server's inbox, not on any chat, so it must not claim control) removes an item — sent, or dismissed with ✕, and the file stays in uploads either way — a claimed share is now an attachment the composer holds by path. `TestShareInbox`.

**Caching on `/file`** splits by whether the bytes at a path can change. An UPLOAD is immutable by construction — `_store_upload` never overwrites, it appends `-1`, `-2` … — so it is served `private, max-age=31536000, immutable`. Everything else is the model's own output, where a regenerated `chart.png` at the same path is exactly what a long max-age would leave stale on screen, so those keep ETag revalidation and no `Cache-Control`. This is what makes the composer's thumbnail a prefetch rather than a duplicate download: an upload is fetched once while the owner types and costs nothing when the sent bubble renders it. Before it, pressing send began a 4.2 MB download of the original at the one moment the owner was watching (`TestFileCaching`).

**`/dirs`** lists folders and files by name for the picker, filtered by `dir_ignore`, and runs its walk in a separate killable process (`_run_fs_child`) with a timeout answering 504 — a hung filesystem must not take the server with it (`TestDirListing`). `list_files` backs @-mention completion with the same walk, cap and scoring as the TUI (`TestFilesAutocomplete`).

**PDF export** calls `export.py` with `_workspace_roots()` as the local-image trust boundary. The answer export's title is written by the answer's OWN model via `_answer_title` (`agent.chat` directly, off-loop under `TITLE_TIMEOUT`, never entering the conversation), with `derive_title` as the deterministic fallback for every failure path including a rambling reply — a title is a nicety and must never fail or block an export (`TestExportEndpoints`, `TestExportAssembly`, `TestExportMedia`).

**Offline endpoints.** `GET /offline/index` (rev + every session's `name/title/snippet/ts/origin`) and `GET /offline/session` deliberately construct NO Agent and take NO session slot, mirroring `handle_export_session`'s token-check + name-safety + `asyncio.to_thread` shape. `/offline/session` has three "don't resend what they have" layers, cheapest first: a matching `If-None-Match` against a weak mtime+size ETag → **304, empty body**; a `since`/`sig` pair whose prefix still verifies → only events after it; otherwise the whole stream. `sig` is `_prefix_sig`, a hash of the first `since` events computed by the SERVER both times and echoed back opaquely, so there is no cross-language canonical-JSON agreement to get wrong. It exists because reconstruction is NOT purely append-only: a command still running at sync time later reconstructs as `command_start → stream → command_end`, splicing events mid-stream, which a naive `since` would silently corrupt. `offline_events()` caps bulk output on `stream.text` and `step.output` — the conversation stays verbatim, the noise shrinks ~10x, and that asymmetry is what makes a full local archive affordable; a pre-trace log falls back to the flat `history` blob. `TestOfflineMirror`. Client half: `docs/web-frontend.md`.

---

## Feedback → GitHub issue

Two flavours. Text-only web `/feedback` uses the **block flow**: the model emits the finished issue as one ` ```aish-issue ` fenced block (line 1 `title:`, optional `---`, body verbatim), parsed once in the backend by `parse_issue_block` and mirrored in app.js, and never runs `gh issue create` — the frontend renders a review card and on confirm the backend files it VERBATIM on the pinned `ISSUE_REPO` as a user-direct action with safe argv, no approval gate (`TestIssueBlockParsing`, `TestIssueCreation`).

CLI feedback, and web feedback carrying attachments, use the **classic flow**: the model drafts in rendered markdown and runs `gh issue create` itself through the approval gate, because it must upload assets. Attachments are published to a PUBLIC release, so consent is explicit — the draft lists every file with a per-file exclude chip, and a block-flow draft that gains attachments mid-adjustment auto-switches to classic via a server-appended, model-only note (`TestFeedbackAttachmentSwitch`).

---

## Not here

- **Anything in `aish/static/`** — the rail, the transcript, the trace card, gesture handling, the console UI, the offline client, PWA reconnect, voice, approval-card accents: `docs/web-frontend.md`.
- **The Agent's own gates** — origin-scoped egress and knowledge writes, approval verdicts, session scope: `docs/agent-core.md`.
- **The log format and replay** — `reconstruct_events`, activity stamps, redaction records: `docs/session-log.md`.
- **Trace record shapes** — `docs/trace-contract.md` (binding) and `docs/trace-records.md` (rationale).
