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
| `message` | a conversation turn; user AND assistant records carry a minted `turn` id |
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

**The turn counter is session state living on the agent, so reopening has to restore it** — `SessionLog.last_turn` beside `restore_state`, applied by `Agent.resume_turns` at all three resume sites. `turn` is the join key the whole trace contract rests on (`docs/trace-contract.md` §2): the rule ledger buckets `rule_eval`, `binding` and `gate` records by it, "joined by id, never by position". But `_turn` starts at 0 on a fresh `Agent`, and a chat gets a fresh one every time it is reopened — on the web that is every restart of `aish-web`, which is every ship. So a conversation spanning a restart wrote a second `turn: 1` and the ledger silently merged two turns into one, attributing the later turn's gate verdicts to the earlier turn's bindings. Found in a live log: two tasks a day apart, both stamped `turn: 1`. Restoring is monotonic (`max`) rather than an assignment — a stale or truncated log must not be able to wind the counter back into ids already handed out. **Two different things are called `turn` in this file**, and only the isinstance check keeps them apart: the counter lives at `step.turn` and is an int, while `message` and `rating` records carry a top-level `turn` that is the client's minted event id, a string. `TestLastTurn`.

**And the same is true of what the chat has already FETCHED** — `SessionLog.calls_that_ran` (#267). The rule engine's `unverified_links` grades an answer's links against the URLs aish actually opened, and once that ledger is the chat's rather than the turn's (`docs/rules-engine.md`) it has to survive the agent that filled it, or a reopened chat demands a re-read of pages sitting in the transcript it just replayed. **One step is TWO records** under the trace contract §2: `call` carries the model's own arguments, `tool` carries the runtime's verdict, and they are joined by the `(turn, call)` pair — never by position, because read-only tools run in parallel and their steps interleave, and never by `call` alone, because that counter restarts every turn. A step missing either id (a pre-contract log) or missing its `tool` half (a session killed mid-call) is skipped rather than guessed at: the cost is one repeat fetch, where a wrong pairing would hand a success to a URL that failed. The reader states what the log says and stops there — it reports a failed call AS failed and knows nothing about URLs, because what counts as *opened* is the rule engine's fence and must have exactly one definition. `TestCallsThatRan`.

---

## Replay (L1)

The command's output rides on the `tool` step rather than being duplicated in the framing records, and is spliced back in as one `stream` with `run_command`'s trailing `[exit code: N]` marker stripped — matching the live body, where the code arrives via `command_end`. A legacy tool step with no framing gets a synthesized block. One `done` per task carries the final assistant text; it returns `None` for pre-trace logs, and the server falls back to a flat `history` blob then.

**A `done` NAMES its answer (#229).** It carries `answer` — the id of the assistant record `flush` promoted, via `_turn_id`, so a record written before ids existed falls back to its line index and every chat already on disk is covered. That id is the fork point (`truncate_at_answer_id`), and it replaced an ordinal: `truncate_at_answer` counted final answers over the log while the browser counted rendered ones over its view, and on a trimmed view (#228) the two numbers named different records — a fork tapped on the twentieth answer branched from the sixth. Worth knowing that the two counts could disagree even on a WHOLE view: a turn that spoke, called a tool and then stopped is one answer here and none to `truncate_at_answer`, which requires that no `tool` message follow. `truncate_at_answer` is kept for pages cached before ids shipped; `_cut_after_answer` is the shared tail — everything up to the NEXT user message, so a trace record logged after the answer is not orphaned into a dangling step. `TestAnswerIdentity`.

**Deliveries (#212).** A turn says several things on its way to the answer, and replay used to keep only the last: `answer = content` on every assistant record, last one winning. Live those interim messages had streamed — so a chat the owner watched say three things came back cold as a chat that said one, which is L1 failing quietly on the most ordinary turn there is. Each non-empty assistant text now replays as the `token` + `delivery` pair a live client receives, **in position** among the steps it interleaved with; `flush` lifts the last one out to become `done`. A turn that only ever said one thing produces neither event and is byte-identical to before, which is most old logs. Nothing new is written to disk — interim assistant entries have always been logged; only the reader changed.

**Synthetic user turns (#171, L4).** `synthetic_kind()` classifies by text, using markers the producers build from or are pinned to by test — never an import of `server.py`, the higher layer.

- **`"resume"`** — a synthetic turn that really DID start a task. Replays as a normal `user` event carrying `synthetic`, so the frontend still runs the whole turn-management path and only swaps the blue bubble for a quiet system row.
- **`"note"`** — the annotations that start no task: `[aish: …]` loop, stall and step-limit nudges, `/cd` and `/add-dir` announcements, console text shared into context. **Skipped entirely**, because live they never reached the transcript at all (a `/cd` shows as a `workspace` marker), and one landing mid-turn also SPLIT that turn in two.
- **`"trigger"`** — a triggered session's opening prompt is arbitrary text with no marker, so it is identified positionally: the first user message of a non-`user`-origin log, the same message the server marks live.

The same classifier keeps aish's own notes out of `_derive_title` and `_derive_snippet`, so a chat opened with a `/cd` is no longer titled with the announcement that produced it.

**Attachments: two forms, and only one of them is written (#231).** A message that carried a file stores an **embed** — `![[cat.png]]`, wiki-link style as in Obsidian, name alone for a file aish keeps and a full path for one living elsewhere. **It means the same thing wherever it sits (#233):** alone on a line it is an attached photo, inside a sentence it is that file in that position, and the position is the only thing saying which file belongs to which clause. That is the record: what the log holds and what the owner sees, copies and reuses. What a MODEL gets is the **guidance form**, a sentence per file saying whether it may look at the picture, read the PDF, or must open the file itself — built by `attachment_guidance` at hand-over time, and again by `Agent.load_history` when a stored conversation goes back into a model, so a restored turn reads to the model exactly as the live one did. It is never stored, because what a file can be used for is a fact about the backend answering right now, not about the message.

`real_attachments` is what decides whether a reference IS an attachment, and its test is whether the file exists. That is the rule that replaced whole-line matching once an embed could be typed: prose about `![[note]]` names nothing on disk, so it stays prose — the check applies to embeds only, since nobody types the bracketed prose forms by accident and a file since deleted should still tell the model it was once attached. It also bounds the risk of a reference being INPUT: a bare name resolves against the uploads folder and nowhere else, and one containing `..` resolves to itself, which exists nowhere.

Two derivations read a stored message and they differ ON PURPOSE. `message_body` keeps inline references verbatim — it rebuilds a model's view of a stored turn, and flattening there would have made a restored turn read differently from the live one, losing the position quietly. `strip_attachment_notes` is for DISPLAY (titles, previews, search) and reads an inline reference as its file name.

`to_record_form` converts either into the record form and is **idempotent**, which is what makes it safe to apply at the one choke point (`Agent._append`, via `run_task`) instead of at each call site: a retry rewinding the model's own context hands guidance back around, and it lands on the stored shape rather than writing prose into a fresh log line. `attachment_refs` reads both forms; `resolve_attachment` turns a bare name back into a path against the uploads folder.

Why it mattered: one string served both audiences, so the sentence written for the model was the ONLY record that a message had a photo. Everything facing the owner had to undo it — the bubble hid it, the title ignored it, copy stripped it, reuse re-parsed it, in two languages — all re-deriving from prose a fact the record already held as a list. `TestAttachmentForms`, `TestAttachmentFormsAcrossTheSeam`.

**The legacy prose forms are read forever, and this is why.** A turn carrying attachments has a line per file appended to it by the web server (`[image attached: cat.png — you can see it; file at /…/uploads/cat.png]`), written for the MODEL. It sits *inside* an otherwise ordinary user message rather than replacing it, so `synthetic_kind` cannot see it — and a chat opened with a photo was therefore named with the sentence aish wrote to itself, absolute path and all, in the header, the rail, the PWA tab title, the PDF and the offline mirror. `strip_attachment_notes` is the one Python definition of "what the owner actually wrote" and every derivation reads through it; `attachment_names` names a turn that has no words of its own, because a photo sent with nothing typed is still about something ("cat.png" beats an unnamed chat). Both are prefix-matched on a whole line so text a human typed that merely resembles a note is never eaten — being wrong in that direction would delete something they wrote. The frontend has the same split for the same reason (`[ATTACHMENT-NOTES]` in `docs/web-frontend.md`), and the format is pinned from both languages by `TestAttachmentNoteFormat` + `test_attachment_notes.js`.

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

**Search reads the CHAT, not the log (#266).** `visible_messages` is the one derivation of what a session says — user turns and assistant answers, an attachment read as its file name, aish's own `[aish: …]` notes dropped — and both the search index (`_cached_entry`) and the preview line (`_derive_snippet`) read through it. The index used to be every `kind:"message"` record, tool results included, so a `web_search` result list, a fetched page, a file read or a `recall` excerpt made a chat "match" a word that appears in no bubble: filtering the real archive for `tefal` returned five chats whose only mention of it was something the model read and never repeated. The mirror in `app.js` had the rule right from the start (`offlineSearchText`, `docs/web-frontend.md`) and the server never adopted it — which is the argument for one function rather than two that agree today. Reasoning is not a special case here: it lives in trace records and never enters `messages`.

**Approximate matching is a fallback, not a tier.** `rank` runs the literal tiers (exact title, phrase in title/model, phrase in contents, all words) and only if *nothing at all* matched does it call `_closest`. Mixed in as tier 1, it was most of what came back: difflib's 0.75 cutoff is a length artifact for short words — `tel` scores exactly 0.75 against `tefal`, and so do `tea`, `fal`, `efl` — and a 474-session vocabulary always contains such a word, so fifty coincidences buried three real hits. `_closest` adds two bounds on top of being a fallback: a candidate must be within `FUZZY_LEN_SLACK` of the query word's length (a typo keeps a word's length; a much shorter word is a different word), and the answer is ranked by its weakest word's closeness and capped at `CLOSEST_MAX`, because "close enough" over an archive has no natural end. It is also the fast path — a query that matches anything now costs no difflib at all, on every keystroke over every session.

`search_excerpts` is the ranked excerpt search behind the sessions half of the `recall` tool, with hard-capped output; the agent gets `state_dir` + `current_session` at construction and excludes the live session from results (`TestSearchExcerpts`).

`user_command_history()` aggregates the user's OWN successful `!` commands across recent sessions — only `decision:"user-direct"` records whose `cmd_end` reports exit 0, so model tool-loop commands never pollute the palette — and feeds the web terminal-mode autocomplete. Its command/exit pairing lives in `_parse` (`ParsedLog.user_cmds`) so it rides the parse cache.

`task_start`/`task_end` bracket a web task on disk. `pending_task()` returns the unfinished one, with an `attempts` count = starts since the last end, and `interrupted_sessions()` lists them newest-first inside an age window — the durable half of restart recovery (`docs/web-server.md`).

---

## Auto-titling (#175)

`set_title(title, auto=…)` stamps the `title` record with **who chose the name**, and `ParsedLog.title_auto` reads it back — that flag is what makes a hand-typed rename permanent. A record with no `auto` key predates the feature and was, by definition, a manual rename.

`title_drifted(title, recent_text)` is the free lexical gate in front of the retitle model call: the fraction of the title's 4+-character words still present in the recent exchange, below `DRIFT_KEPT_RATIO` = drifted. Deliberately **not** embeddings — spending a model call to decide whether to spend a model call is a bad trade, and being wrong either way is cheap.

---

## One unreadable log costs its own chat and nothing else

Every reader here walks a file line by line and skips a line it cannot parse. None of them survived a line that **parses to something that is not a record** — a bare JSON string, a number — because they all call `.get()` on whatever comes back.

That second mode is not hypothetical. On 2026-08-20 one session log's lines had been reformatted, so a bare string sat on a line of its own; `_parse` raised `AttributeError`, `pager_titles` calls `_parse` for **every** session on attach, so the websocket closed during the handshake and every client — the owner's phone included — sat on the boot spinner with no chat list at all. One corrupted file, and the whole app was unreachable.

`_record_or_none` is now the only place a log line is parsed, and it answers `None` for both failure modes. Ten call sites shared this walk; nine had the bug and **one already had the `isinstance` guard**, which is the argument for one helper rather than nine fixes — the guard existed, it just was not where the crash was. `TestOneBadLogCannotTakeTheAppDown` pins the recovery, that the session list survives a bad log beside a good one, and — at the source level — that no reader has grown its own `json.loads` back.

The general rule this is an instance of: **a reader of many files must fail per file.** Anything that walks the whole state directory on a hot path (attach, the rail, search) turns one bad file into a total outage unless the failure is contained where it happens.

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
