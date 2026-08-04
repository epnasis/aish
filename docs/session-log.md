# Session log — the append-only transcript

`session.py`: one JSONL file per session in `~/.local/state/aish/`, holding the conversation (for `--resume`), the audit trail of every command decision, and the structured activity-trace records both UIs render.

**How to use this file.** The laws are the ones every reader and writer of a log depends on; violating one is how a chat comes back unread forever, or how a line goes missing that no reader will ever report. Then the record kinds, replay, activity, the caches, and the one operation that is not append-only.

---

## The laws

**L1 · Replay is the definition of the UI.** `reconstruct_events()` produces the EXACT event stream a rich client emits live — `user`/`step`/`done`, plus for each `run_command` its full `command_start → stream → command_end → tool` sequence. A cold-loaded session therefore renders identically to a live one, with no fallback path. Everything else here leans on that.

**L2 · Every write goes through the lock.** More than one thread appends: the agent worker logs the conversation while the event loop writes renames and console-open audits. An unserialized write on the buffered handle is a torn or lost JSONL line that **every reader silently skips** — the worst failure shape there is, because nothing reports it. Any NEW path that appends to or rewrites the log must hold `_write_lock`. `TestWriteLock`.

**L3 · Activity is a property of the RECORDS, not of the file.** Reading a chat can append to it, so mtime cannot distinguish a chat that DID something from one that was merely looked at.

**L4 · Text is the only thing a cold replay can classify by.** aish writes synthetic `role:"user"` turns the human never typed; the model must read them as the turn's input, so they are logged like any user message. Classification is therefore lexical — which is also why logs written before the classifier existed replay correctly.

**L5 · Read caches hand out SHARED objects.** They are for consumers that never mutate what they read. Resume paths stay on the raw parse by design.

---

## Records

Written by `_write_line`, each stamped with an ISO timestamp — since the first version of the file, which is why the frontend's message timestamps were a read and not a migration.

| kind | what it carries |
|---|---|
| `message` | a conversation turn; user records also carry a minted `turn` id |
| `cmd_start` / `cmd_end` | terminal-block framing, from the `command_log` sink |
| `trace` | the structured activity-trace steps — the same dicts the web renders |
| `title` | the chat's name, stamped `auto` or not |
| `cwd` / `trust_dir` | the session's own workspace, replayed by `restore_workspace` |
| `origin` | provenance for a non-`user` session |
| `model` | which model this chat runs |
| `task_start` / `task_end` | the restart-recovery bracket |
| `render_error` | a client-reported image failure |
| `redact` | a positioned tombstone |

`step_log` and `command_log` are the log sinks; they are **orthogonal** to the agent's `on_step`/`on_command_*` rich-renderer hooks. The CLI wires only the log sinks, so it logs without changing its inline output — which is what makes the trace UI-agnostic and lets it survive eviction and restart.

`_parse` returns a **`ParsedLog` NamedTuple**, not a bare tuple, so log metadata can grow without rewriting every unpacking call site.

---

## Replay (L1)

The command's output rides on the `tool` step rather than being duplicated in the framing records, and is spliced back in as one `stream` with `run_command`'s trailing `[exit code: N]` marker stripped — matching the live body, where the code arrives via `command_end`. A legacy tool step with no framing gets a synthesized block. One `done` per task carries the final assistant text; it returns `None` for pre-trace logs, and the server falls back to a flat `history` blob then.

**Deliveries (#212).** A turn says several things on its way to the answer, and replay used to keep only the last: `answer = content` on every assistant record, last one winning. Live those interim messages had streamed — so a chat the owner watched say three things came back cold as a chat that said one, which is L1 failing quietly on the most ordinary turn there is. Each non-empty assistant text now replays as the `token` + `delivery` pair a live client receives, **in position** among the steps it interleaved with; `flush` lifts the last one out to become `done`. A turn that only ever said one thing produces neither event and is byte-identical to before, which is most old logs. Nothing new is written to disk — interim assistant entries have always been logged; only the reader changed.

**Synthetic user turns (#171, L4).** `synthetic_kind()` classifies by text, using markers the producers build from or are pinned to by test — never an import of `server.py`, the higher layer.

- **`"resume"`** — a synthetic turn that really DID start a task. Replays as a normal `user` event carrying `synthetic`, so the frontend still runs the whole turn-management path and only swaps the blue bubble for a quiet system row.
- **`"note"`** — the annotations that start no task: `[aish: …]` loop, stall and step-limit nudges, `/cd` and `/add-dir` announcements, console text shared into context. **Skipped entirely**, because live they never reached the transcript at all (a `/cd` shows as a `workspace` marker), and one landing mid-turn also SPLIT that turn in two.
- **`"trigger"`** — a triggered session's opening prompt is arbitrary text with no marker, so it is identified positionally: the first user message of a non-`user`-origin log, the same message the server marks live.

The same classifier keeps aish's own notes out of `_derive_title` and `_derive_snippet`, so a chat opened with a `/cd` is no longer titled with the announcement that produced it.

**Renderless records.** `RENDERLESS_STEPS` is skipped by `reconstruct_events`, and it is the ONE registry for "this record renders nowhere" — see `docs/trace-records.md` for why that needs two mechanisms rather than one.

---

## Activity (L3)

"Last interaction" used to be the file's mtime. A chat holding an image that fails to render — a remote host off the fetch whitelist, an evicted media file — had a `render_error` written on **every replay**, landing a second or so AFTER the client stamped "seen": so the unread check saw activity newer than the look that caused it, and the chat came back unread the moment you left it, permanently, with opening it to check being what re-armed the signal.

`ParsedLog.activity_ts` is computed in `_parse`'s existing single pass by `_is_activity`, whose rule reuses a distinction this module already draws: **a record that renders nowhere is not activity.** `RENDERLESS_STEPS` is the registry, so a future governance record inherits this instead of re-teaching it, and `model` is excluded for the same reason it is written lazily at all — it says which model a chat runs, never that it ran.

`SessionInfo.activity` carries it, falling back to mtime for logs with no usable stamps, because nothing may lose its place in the list. Every consumer of "when did this chat last do something" reads it: the session-list row, the title peek, and `/offline/session`'s `ts`. `_by_recency` deliberately stays on `stat()` — it is the cheap newest-first pre-filter and a superset — and so does the offline ETag, which genuinely IS about the bytes on disk. `TestActivityIsNotAFileTouch`.

## Output (#203) — the second stamp, and why it is second

Activity answers "did anything happen in this chat", which is the right question to ORDER by: a chat mid-turn is the most recent thing there is. **Unread is a different question and had been reading the same number** — so every thinking step a chat took moved its stamp past the device's last look and the row marked itself unread with nothing new behind it. Three consumer-side patches went in before the fact itself got split; see `[ATTENTION]` in `docs/web-frontend.md` for that history.

`_is_output` is read OFF `reconstruct_events` rather than invented beside it: **output is exactly what that function turns into transcript content** — a `user` bubble, or the assistant text that becomes a turn's `done`. So a `message` with role `user` (minus aish's own `[aish: …]` notes, which never reached the transcript live) or role `assistant` with non-empty text. Everything else `reconstruct_events` emits is a trace step, a workspace marker or command framing, and everything it ignores — `model`, `title`, `origin`, the audit `command` line, `task_start`/`task_end` — was never on screen at all.

Consequences, each a false unread that is now gone: renaming or redacting a turn from another device, a turn CANCELLED with no answer, and a chat that is simply thinking. `SessionInfo.output` reports **0.0** rather than falling back to mtime — "we could not tell when this chat last spoke" must not become "it spoke just now", and every reader treats zero as "use the activity stamp", so old logs behave exactly as they did before the split. `TestOutputStamp`.

**A failed turn is the one output with no message to carry it**, so `task_end` records HOW the turn ended (`status`, plus `error` capped at `TASK_ERROR_CAP`) and a failed one counts. Until that existed the failure text was live-only: a client not connected when a background job died learned nothing, "why did last night's job fail?" was unanswerable from the log, and cold replay could only synthesize the generic `INTERRUPTED_TASK` from the fact that steps were left unfinished. `reconstruct_events` now prefers the recorded reason over that inference — the log knows why, and the guess was only ever made because nothing else was written. Additive: a `task_end` with no `status` predates the field, reads as "we do not know", and every branch is inert on it (`test_reconstruct_events_old_logs_are_byte_identical`). A turn that ended FINE is not output by itself — its answer already is, and stamping twice for one event is how a marker becomes a second source of truth.

---

## Search and history

`search_excerpts` is the ranked excerpt search behind the sessions half of the `recall` tool, with hard-capped output; the agent gets `state_dir` + `current_session` at construction and excludes the live session from results (`TestSearchExcerpts`).

`user_command_history()` aggregates the user's OWN successful `!` commands across recent sessions — only `decision:"user-direct"` records whose `cmd_end` reports exit 0, so model tool-loop commands never pollute the palette — and feeds the web terminal-mode autocomplete. Its command/exit pairing lives in `_parse` (`ParsedLog.user_cmds`) so it rides the parse cache.

`task_start`/`task_end` bracket a web task on disk. `pending_task()` returns the unfinished one, with an `attempts` count = starts since the last end, and `interrupted_sessions()` lists them newest-first inside an age window — the durable half of restart recovery (`docs/web-server.md`).

---

## Auto-titling (#175)

`set_title(title, auto=…)` stamps the `title` record with **who chose the name**, and `ParsedLog.title_auto` reads it back — that flag is what makes a hand-typed rename permanent. A record with no `auto` key predates the feature and was, by definition, a manual rename.

`title_drifted(title, recent_text)` is the free lexical gate in front of the retitle model call: the fraction of the title's 4+-character words still present in the recent exchange, below `DRIFT_KEPT_RATIO` = drifted. Deliberately **not** embeddings — spending a model call to decide whether to spend a model call is a bad trade, and being wrong either way is cheap.

---

## Parse caches (L5)

The read-only listing paths — `info`/`list_sessions`, `load_entries`, `_peek`/`pager_titles`, `user_command_history` — serve from stat-keyed module caches (`_PARSE_CACHE`/`_ENTRY_CACHE`, key `(mtime_ns, size)`), because re-parsing hundreds of JSONL logs on every session switch was the bulk of a switch's server time, and the parsing holds the GIL.

They hand out SHARED objects, so resume paths (`load_messages`, the CLI's own parses) stay on the raw `_parse` **by design**: restored history flows into an Agent whose `_trim_tool_message` rewrites message dicts in place, which would poison a shared cache. `_by_recency` prunes cache keys for deleted files, scoped to its own state dir, since tests run many dirs in one process.

---

## Redaction — the one non-append-only operation (#202)

The transcript was append-only and replayed whole, so anything that landed in it was permanent short of deleting the chat or hand-editing JSONL with the server stopped: a probe fired at the wrong chat, a message an autocorrect Return sent half-typed, a secret pasted into the composer.

`redact_turn(turn)` is the removal, and **the unit is the TURN, never one bubble** — an answer repeats what it was asked and a command echoes the argument it was given, so half a turn is not a removal. It is a REAL delete plus a POSITIONED tombstone rather than a hide-only marker: the records leave the file and a dated `redact` record takes their place AT THEIR POSITION, so the removal stays auditable — when, which turn, how many records — while the text is gone from disk, which is the only answer that works for the pasted-secret case.

Three details are load-bearing (`TestRedaction`):

1. **The cut starts at the turn's `task_start`, not its user message.** `task_start` is written FIRST and carries the prompt VERBATIM, so cutting from the message would leave a copy of exactly what was removed — and would strand an unmatched `task_start`, which restart recovery reads as a task to resume. The same walk-back decides the END, or the removal takes the NEXT turn's `task_start` with it.
2. **Turns are NAMED.** `SessionLog.message` mints a `turn` id on every user record and `reconstruct_events` carries it on the `user` event, with `_turn_id` falling back to the record's LINE INDEX for logs written before ids existed — which is every chat holding a message someone wants gone. The id is minted by the SERVER so a LIVE turn is removable too: the message you most want back is the one you just sent, and a log-minted id would only exist after a cold replay.
3. **`Redaction.occurrence` says WHICH identically-worded turn it was**, because the live Agent's messages hold no ids — its dicts go straight to the backends — and two turns saying "ok" would otherwise be indistinguishable. Dropping the wrong one from the model's context is the one thing this must not do.

**A redaction IS activity**, unlike the renderless records L3 exempts: every device mirrors the transcript and only refetches a session whose activity stamp MOVED, so an unmoved stamp would leave the removed text in IndexedDB on each of them. Hence `_parse` takes the LATEST activity stamp rather than the last one in file order — a rewrite made file order stop meaning chronological order.

An AUTO title is re-derived and its stale records DELETED, because a model-written name is a summary of the conversation and can quote the very text being removed. A hand-typed title is the user's own words and is left alone.

The model-context half of the same removal is in `docs/agent-core.md`.
