# Agent core — the loop, the gate, the scope

`agent.py`, `approval.py`, `tools.py`, `files.py`, `web.py`, `backends.py`, `claude_max.py`.

**How to use this file.** The laws come first — they are the invariants the whole gate rests on, and most of the code below is one of them applied. Then the loop, the gate, and the tool modules, each naming the test class that pins it. If you are changing what runs without asking, read L1–L4 before anything else.

Knowledge tools are in `docs/knowledge-layer.md`, plugin tools in `docs/tools-layer.md`, images in `docs/media-and-images.md`, record shapes in `docs/trace-records.md`.

---

## The laws

**L1 · The model never executes anything directly.** Backends return structured tool-call *requests*; `Agent._dispatch()` is the single execution point, and `run_command` is unreachable there unless the `approve()` callback returns the (possibly user-edited) command. Every alternative execution path is a bug, which is why claude-max — whose SDK owns its own loop — routes through `_locked_dispatch` back into the same `_dispatch` (`TestSingleExecutionPath`).

**L2 · A comment flips what a verdict means (#81).** Without a comment: `True`/edited-string approves, `None`/`False` denies, `Blocked(reason)` is unapprovable. With one, approve and deny mean *opposite* things. `Denied(comment)` = **STOP** — denied, the comment appended to the result, and the stop gate armed. `Approved(comment)` = **CONTINUE but ADJUST** — the original action is HELD and never runs, even if the command was edited; the model reworks it to the comment and re-proposes, and the adjusted action is approved again before it runs. `TestApprovalComment`, `TestApproveContract`.

**L3 · Approve only what you positively understand.** `approval.py` classifies a command as read-only only when it can account for every part of it; anything ambiguous falls through to a prompt. When touching that file, err toward prompting — a false prompt costs a keystroke, a false auto-approval costs whatever the command did.

**L4 · Scope belongs to the session, not the process.** The `roots` that bound auto-approval and the `session_prefixes` granted with "Allow this session" both live on the Agent and are reset by the same call. A grant made in the chat you leave must not follow you into the chat you land in.

**L5 · Plan, show, then commit.** `files.py`'s `plan_*` functions compute `(old, new, diff)` without touching disk; `commit()` writes. The gap between them is where the diff is shown and approval obtained — nothing is ever written unseen. The plugin-tool `preview` seam is the same idea for actions that have no diff.

**L6 · Unattended is not attended.** `Agent.origin` scopes capabilities whose safety argument was "a human is watching". A `user` session behaves byte-identically to before any of these gates existed; everything else is scoped.

**L7 · The verdict travels with the value.** A tool result carries its own outcome (`ToolOutcome`) rather than being sniffed from its text downstream, and a refused action is never recorded as a green step. Both replaced string-prefix guessing that had been quietly wrong for a long time.

---

## The task loop

`run_task` starts by calling **`_reset_task_state()`** — the shared extraction of the per-task reset (stop gate, skill gates, `_run_meta`, `task_sources`, `_cancel`, the turn counter). **Add new per-task fields THERE, never inline in `run_task`**, or claude-max wedges: the SDK owns its loop, so `run_task` never runs there and `ClaudeMaxAgent.run_task` calls the shared reset instead (`TestStopGateReset`).

Then it loops: the model proposes tool calls → `_dispatch()` executes them under the gate → results are appended → repeat until a final answer (`TestLoop`).

**The step budget is progress-gated, not a flat cap (#108).** A step counts as progress when a tool call yields a `(tool, args, result)` tuple seen for the first time — reusing the loop detector's `repeats` dict, so no extra model call and no timer. A progressing task may run past `max_steps` up to the unconditional hard ceiling `max(max_steps, HARD_STEP_CEILING=60)`; `MAX_STALL_STEPS` (8) consecutive no-progress steps stop it early. The loop ends three ways: a text-only answer, a cancel, or `_finish_stopped()` — reached at a stall, at the ceiling, or when loop detection fires (identical call *and* identical result: warn at 3 repeats, stop at 5) — which runs one final no-tools turn so the model reports its completion state instead of cutting off. The ceiling is never silently exceeded. `TestStepLimitAndLoops`, `TestCancel`.

**Read-only tools run in parallel.** `_execute_tool_calls` fans the read-only set out concurrently, which is why anything gated must be routed OUT of that path (`_read_needs_prompt`) rather than gated inside it. Read-only plugin tools parallelize the same way — that uniformity is deliberate (`TestParallelReadOnlyTools`, `TestReadonlyPluginParallel`).

**Context.** Per-task, prior tool outputs are trimmed to 200-char stubs; a resumed run passes `keep_history=True` and gets `_trim_history_to_budget` instead — oldest-first, only as far as the budget demands — because the eager trim would gut exactly the results a resume exists to preserve. The trim is itself recorded, being the truncator with the largest blast radius (`TestContextCompaction`, `TestContextAndHistory`, `TestTrimIsRecorded`). `_trim_tool_message` rewrites message dicts **in place**, which is why the session parse caches are never handed to a resume path.

**Turn and call identity.** `Agent._turn` advances in `_reset_task_state` (so claude-max counts turns too) and a per-turn `call` counter is assigned in `_call_result`. `call` is carried as a LOCAL, never read back off the agent — read-only tools run in parallel and an attribute would hand two concurrent calls the same id. `TestTurnAndCallIdentity`.

**Status and streaming.** `_note` gates terminal chatter; the status protocol's `note()` channel carries live "Thinking: …" without recording it; the model's own words ride the persisted `thinking` step (`say` = preamble alongside tool calls, `gist` = first line of thinking), keys omitted when empty so old logs replay identically. `TestLiveStatus`, `TestStreaming`, `TestThinkingStatus`, `TestElapsedTimeReporting`, `TestActivityTraceSteps`, `TestModelResilience`.

**Command framing.** `run_command` surfaces `command_start` (cwd + command) and `command_end` (exit code / detached / interrupted) so a UI can draw a bounded terminal block (`TestCommandFraming`).

**Mid-task steering.** The loop polls two get/drain callbacks at the top of each iteration so a `/cd` or a message typed during a long task is applied between steps (`TestMidTaskSteering`; the server side is in `docs/web-server.md`).

**Redaction reaches the live conversation too.** Scrubbing the log is the durable half; the running `agent.messages` must lose the same turn, identified by occurrence because the live message dicts hold no ids (`TestRedactTurn`).

---

## The approval gate

**The stop gate (L2).** Eager models run another tool before addressing a denial, so while `self._pending_comment_response` is set every tool call is refused. The main loop clears it only on a **text-only turn** — not a same-turn text+tool, or a chatty preamble alongside a command slips past — and that text-only turn ends the task, because deny means stop. `run_task` resets the flag so it never leaks across tasks. Approvals never arm the gate; the HOLD plus re-approval is what keeps an unwanted original from running. `TestStopGate`, `TestApprovalGate`.

**The rule gate (#191).** `_rule_gate` runs in `_dispatch` immediately after the stop and skill gates, checking the call against this turn's rule bindings. It sits ALONGSIDE every gate on this page, never above them: rules only ever restrict, so the engine can add a refusal and has no path to lifting one. Its bindings also join `_pending_skill_reads` and `_pending_comment_response` in the condition that forces `_execute_tool_calls` off the parallel path — that path bypasses `_dispatch` entirely, and `web_search` is both what it fans out and what the canonical rule prohibits. Unlike those two, a binding disables concurrency only for the calls it actually governs (`rules.affects`), so an unrelated batch of local reads keeps it. Everything else about it — the vocabulary, the bounded refusals, the records — is in `docs/rules-engine.md`. `TestRuleGate`, `TestRuleSeeding`, `TestBoundedRefusalAndEscalation`, `TestBoundedMaterial`.

**Blocked and background.** The denylist (unrecoverable commands, blocked even with approval) lives in `tools.py` and is checked first, on edited commands too. Detached and background jobs have their own accounting (`TestBlockedAndBackground`, `TestBackgroundJobs`, `TestDetach`).

**What runs without asking**, and the four things that pull it back:

1. **The read-only classification** (`approval.py`) — chaining, the user allowlist, full-path invocations that resolve to exactly the binary PATH finds, and binaries in trusted system bin dirs invoked by absolute path. `TestChaining`, `TestUserAllowlist`, `TestPathInvocation`, `TestTrustedBinDir`, `TestDenylist`, `TestLooksDestructive`.
2. **Root scoping** — a path argument escaping every root prompts even when the command is allowlisted. `escaping_dirs()` names the escapes so the prompt can offer "trust this directory" (`Agent.trust_root`, session memory only). `TestRootScoping` (both files), `TestEscapingDirs`.
3. **Symlink escapes** — a plain relative token that IS an in-root symlink pointing outside must prompt; a token naming nothing on disk is never resolved, so patterns, flags and future files cannot false-positive. `TestSymlinkEscapes`.
4. **Sensitive operands** — shell path operands get `files.is_sensitive_path` on their RESOLVED target, mirroring the `read_file` prompt, so `cat link-to-ssh-key` or an in-root `.env` prompts even when containment holds. `TestSensitiveOperands`, `TestIsSensitivePath`.

**The scratch workspace** is the deliberate exception: create AND delete auto-approve, scoped strictly inside the per-session scratch dir, everything else unchanged (`TestScratchWorkspace`, in both test files).

**Prefix lifetimes.** `a` / "Always allow" is durable and file-backed, shared by CLI and web. `s` / "Allow this session" lands in `Agent.session_prefixes` and dies with the session.

---

## Session scope (L4)

Both halves of the scope live on the Agent and are reset by the SAME call, `restore_workspace`, so a session change drops them by construction rather than by someone remembering to. The approvers never own the prefix set: `cli.make_approver` and `server.make_web_approvers` both take a late-bound `get_session_prefixes()` reading the live set off the agent (the same holder pattern as `get_scope`/`trust_dir`, because the agent is built after its approvers), keeping a set of their own only for an unwired approver in a test. `restore_workspace` CLEARS the prefixes and never restores them — unlike trusted dirs they are not recorded on disk, so resuming BACK to the chat that granted one asks again rather than inventing persistence the log cannot back. The persistent `a` allowlist is untouched by all of it.

**`restore_workspace(cwd, trusted)` is AUTHORITATIVE, never additive.** On resume or cold-open it rebuilds `roots` to be EXACTLY that session's own workspace: its recorded `kind:"cwd"` (or `Agent.launch_cwd` when it never recorded one, matching the base the web gives a cold-opened session) plus its own recorded `kind:"trust_dir"` dirs, which `/add-dir` and an approval-prompt "trust this directory" both write. Both directions are bugs: a dir trusted in the chat you LEAVE must not stay auto-approvable in the one you land in (the CLI reuses ONE live Agent across `/resume`, so this leaked; the web builds a fresh Agent per cold open and never did), and a dir this chat DID trust must come back. `/new` re-anchors the same way — you are still in that directory in this terminal, but the previous chat's grants stay with it. It sets state directly, never via `rebase`/`trust_root`, so restoring emits no fresh record and cannot feed a replay loop. Corollary: roots a PROCESS owns rather than a session — the web's uploads dir — are appended AFTER the call. `TestWorkspacePersistence`.

**Execution is stateless.** Every `run_command` runs in `Agent.cwd`; a bare model-issued `cd` is rejected with guidance instead of executing (`CD_NOT_STICKY`); excursions are `cd x && …` subshells, which is why `cd` is in `SAFE_COMMANDS` — it is subshell-scoped and root-checked. Only user actions move cwd: `/cd` and `!cd` move it AND re-anchor `roots[0]` (`Agent.rebase`). Do not reintroduce model-driven cwd mutation. `TestCwdAndCd`, `TestBangCommands`.

---

## Origin gates (L6)

`Agent.origin` is a constructor kwarg defaulting to `"user"`; the server's `open_session` passes the session's origin and cli.py passes nothing.

**Egress (#178 P0-2).** In a non-`user` session, `web_search`/`read_url` (`EGRESS_TOOLS` — read-only locally, but their INPUTS leave the machine) hold on the existing `approve_tool` channel when they reach a host outside **provenance**: `_owner_hosts`, extracted by `_hosts_in_text` from owner-authored text ONLY — the task or trigger prompt, mid-task steering, `add_user_context`, and user-role turns in `load_history`; never tool results or fetched pages, which are exactly what an injection controls — plus `_approved_hosts`, hosts vouched on an earlier card. `_egress_gate` mirrors the #81 verdicts, passes the novel host as the card's `preview` (zero frontend change), and fails closed: no approver → `EGRESS_NO_APPROVER`, unparseable URL → gated. `_read_needs_prompt` routes gated calls out of the parallel read path so the gate cannot be bypassed. `_recall` also drops the past-session archive search and says so in the result, so the model does not retry. `TestEgressGate`.

**Knowledge writes (#196).** The capability the axis was never applied to: `remember`/`forget_memory` auto-approve, deliberately, so capturing a fact costs nothing — but that reasoning is attended-only. Unattended, the text proposing the write can be injected email while the result persists into EVERY future session and is retrieved by preflight: a **persistence primitive reachable from untrusted input**, strictly worse than the read exposure P0-2 closed, because it outlives the session. `_knowledge_gate` runs immediately before both branches (neither is in `READ_ONLY_TOOLS`, so the parallel path can never reach them) and returns on its first line for `origin == "user"`. Non-`user`: **deletion is refused structurally**, with no card, because there is no unattended case where autonomously deleting the owner's knowledge is right, so asking would only park a worker on a question already decided — and a policy that exists only as prompt text is not a policy. **Saving holds** on the card, because a triggered session does legitimately learn things and the owner should see what lands in the corpus. Every refusal names the slug and the real destination, so the harness carries the feedback instead of the owner. `_triggered_safe` must never shortcut `remember`. `TestKnowledgeGate`.

---

## Tool modules

### `tools.py`
`run_command` (approval-gated shell) and `read_docs` — auto-approved, so it takes only a bare command name validated against PATH, never a shell string (`TestRunCommand`, `TestReadDocs`, `TestReadDocsTopic`, `TestStreamingAndCwd`, `TestBinaryOutput`, `TestTruncate`).

**The result envelope (#192, L7).** `ToolOutcome` + `classify_output`. It exists because the runtime used to decide whether a call succeeded by sniffing a string prefix, which recorded a tool returning an empty transcript, a populated `error_log` and **exit 0** as a green ✓ while the model silently substituted six web searches for the source the user had named. `ToolOutcome` is deliberately a **`str` subclass**: every existing caller keeps treating it as the result text, so the verdict rides along with no ripple through ~30 dispatch branches — and, the load-bearing half, the metadata travels WITH the value rather than in instance state, which is what makes it correct on the parallel read path where several calls are in flight (`_run_meta` could not have been used there). The caveat follows from the same choice: a string operation returns a plain `str` and silently drops the envelope, so a `ToolOutcome` is always built LAST, after any concatenation. `classify_output` is the deterministic verdict — exit code, then emptiness, then declared required fields, then a populated error channel (`error`/`error_log`/`errors`: a NAMED field the wrapper author chose, never prose). `read_tool_output` pages a truncated result from the cache, so truncation is no longer a dead end and the wrapper never re-runs. `TestEnvelopeEndToEnd`.

**A refused action is never a green step.** One rule applied last in `_emit_tool_step`: a `decision` in `REFUSED_DECISIONS` forces `ok: false`, `status: "failed"`, `verdict_by: "gate"`, whichever path set it. Wider than the contract's own table — `run_command` sets `decision` but never `ok`, and `DENIED_RESULT`/`HELD_FOR_ADJUSTMENT`/`BLOCKED_RESULT` start with none of the sniffed prefixes, so a denied *shell command* logged green too: ten sites, one defect. `TestRefusalsAreNotLoggedGreen`, `TestRunCommandRefusalsAreRedToo`.

### `files.py`
`plan_*` computes, `commit()` writes, and the gap between them is the review (L5). `read_file` prompts on sensitive paths. `edit_file` has two rescue layers for the failure loop small models hit — pasting `read_file`'s numbered output, and slightly-off indentation. `TestReadFile`, `TestPlanWrite`, `TestPlanEdit`, `TestEditRescue`, `TestFileTools`.

### `web.py`
`web_search`/`read_url` are auto-approved but their inputs leave the machine, so every call is echoed and fetched content is wrapped in an untrusted-content banner. http/https only. `fetch_binary` is the same guarded fetch for non-text bodies, sharing `_require_public` and `_opener` so the redirect re-check applies. `TestWebSearch`, `TestReadUrl`, `TestHtmlToText`, `TestJinaFallbackHint`, `TestSsrfGuard`, `TestWebTools`, `TestLive`.

**TLS trust has ONE source (#189).** Python's default store on macOS is Apple's legacy `/etc/ssl/cert.pem`, not the Keychain system store — years stale and missing newer roots (GlobalSign Root R46 among them) — so fetches to whole swathes of the web failed with `unable to get local issuer certificate` while other hosts worked fine, which reads as a broken SITE rather than a broken client, and it silently affected `read_url` too, not just the image fetch that surfaced it. `_opener` carries an `HTTPSHandler` built from **certifi**'s current Mozilla root set, falling back to the default context if certifi is ever absent: a stale store still verifies most of the web, refusing everything would be worse. `_PublicOnlyRedirects` stays wired alongside it — verifying a certificate on a fetch aimed at cloud metadata is no win. `TestTrustStore`.

---

## Backends

`backends.py` routes `--model` strings to a chat callable. Every backend (Ollama, Gemini, Claude API, OpenAI) is adapted to the *exact* `ollama.chat` calling convention — `chat(model, messages, tools, options, think, stream)` — so `agent.py` never knows which provider it is on. New backends must preserve that shape. `context_window` is what sizes plugin-output truncation, so an Ollama-8k session and a Gemini-1M session get visibly different caps (`TestBackendSizedCaps`).

**Gemini thinking (compat layer).** `OpenAICompatBackend` requests `include_thoughts` for provider gemini; the compat API returns thought summaries INSIDE content delimited by `<thought>…</thought>`, so `_ThoughtFilter`/`split_thoughts` split them into `ChatMessage.thinking` on both the streaming path (stateful — tags can split across deltas, trailing partials are held back and flushed) and the non-streaming one. Unfiltered they would stream straight into the answer bubble and the history. Known gap, probed against the live API: streaming TOOL-CALL turns omit thoughts entirely while non-streaming includes them, so on Gemini the tool phase falls back to the deterministic status line.

### `claude_max.py` — the exception
Routes through the Claude Agent SDK / local `claude` CLI login, stripping Claude Code to bare inference and injecting aish's own tools so the approval gate still applies (L1). Keeps its own session state. Three invariants:

1. **The SDK owns the loop**, so the inner Agent's `run_task` never runs — hence the shared `_reset_task_state()` above (`TestStopGateReset`).
2. **The constructor has NO `**kwargs` sink.** Every capability the entry points pass is either kept on the wrapper (`on_message`, `on_token`, `status`, `max_steps`) or forwarded to the inner Agent (approvers including `approve_tool`/`approve_import`, trace/command/state sinks, steering callbacks, `semantic`, `aliases`, `origin`); an unknown kwarg is a TypeError, so a new Agent capability can never silently no-op here. This is not theoretical: `origin` was added to `open_session`'s `common` dict and, missing here, made **every** claude-max web session raise at construction — which is also what makes both origin gates hold on this backend, since they live in `_dispatch` and `_locked_dispatch` is the single path to it. `TestConstructorWiring`.
3. **SDK tool handlers may run concurrently while the inner Agent is not thread-safe.** `_locked_dispatch` is the single execution path: a `threading.Lock` around `inner._call_result(… inner._dispatch …)`, which also emits the same `tool_start`/`tool` trace steps as the native loop and contains tool exceptions. `TestSingleExecutionPath`, `TestHandlerRobustness`.

Plugin tools register on the SDK MCP server (`_current_tool_defs` = `TOOL_SCHEMAS + inner._plugin_defs`, refreshed per task, mutating ones only when a tool approver is wired). Tested through a FakeSDK injected at the `_load_sdk` seam — no SDK package, no CLI, no network.
