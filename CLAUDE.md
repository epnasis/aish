# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`aish` is a terminal AI agent (local Ollama model by default, cloud backends optional) that runs shell commands, edits files, and browses the web — with a mandatory user-approval gate on anything that mutates state. The README covers user-facing behavior thoroughly; this file covers what you need to change the code safely.

## Commands

```sh
uv run pytest                          # full test suite — no model/network needed
uv run pytest tests/test_agent.py      # one file
uv run pytest tests/test_agent.py -k compaction   # one test by keyword
uv run ruff check .                    # lint (also formats config: line-length 100)
uv run mypy                            # type check (CI-gated; config in pyproject.toml)
uv run aish                            # run from source
```

After changing code, the user's installed `aish` does NOT pick it up — uv freezes the wheel and caches it at the same version. Reinstall with:

```sh
uv tool install --force --reinstall --no-cache /path/to/aish
```

## Workflow: when to use a worktree

Before editing code, check `git status`. Work in a git worktree on a feature branch (via EnterWorktree) instead of this checkout when either:

- the change is **non-trivial** — a multi-file feature or refactor, not a small fix; or
- the tree is **not clean with changes you didn't make** — the user or another session is mid-work in this checkout.

Merge back to main after tests pass, then remove the worktree. Trivial fixes on a clean tree go directly in this checkout. The reinstall/ship step (`uv tool install ...`) always runs from this main checkout after merge — never from a worktree path.

## Architecture

The core invariant: **the model never executes anything directly.** Backends only return structured tool-call requests; `Agent._dispatch()` in `agent.py` is the single execution point, and `run_command` is unreachable there unless the `approve()` callback returns the (possibly user-edited) command.

Data flow: `cli.py` (REPL/argv, slash commands, rendering) constructs an `Agent` with a chat callable from `backends.py` and an `approve` callback, then calls `agent.run_task()`. The agent loops: model proposes tool calls → `_dispatch()` executes them (gated) → results appended → repeat until a final answer. Approver verdicts: `True`/edited-string approve, `None`/`False` deny, `Blocked(reason)` (denylist — unapprovable), `Denied(comment)` (deny carrying the user's typed feedback, appended to the denial result as guidance), `Approved(comment, command=None)` (approval carrying feedback — the action runs, `command` overrides when edited, and the comment is appended to the result as forward guidance; both feedback verdicts are web-card-only — CLI approvers still return bool/str/None). A verdict comment arms the **comment gate** (`_comment_gate`, issue #81): the FEEDBACK/APPROVED notes *ask* the model to answer the comment in plain text before its next tool call, but eager models ignore soft phrasing, so the gate *enforces* it — while `self._pending_comment_response` is set, every tool call is refused; the main loop clears it only on a **text-only turn** (not a same-turn text+tool: chatty preamble alongside a command would otherwise let it run unexplained), and that text-only turn ends the task so the user reviews before continuing. `run_task` resets the flag so it never leaks across tasks. Bounded by `max_steps`, no countdown. The loop ends three ways: a text-only answer, a cancel, or `_finish_stopped()` — reached at the `max_steps` boundary or when loop detection fires (identical call + identical result: warn at 3 repeats, stop at 5) — which runs one final no-tools turn so the model reports completion state instead of cutting off; the budget is never silently exceeded.

- **`backends.py`** — routes `--model` strings to a chat callable. Every backend (Ollama, Gemini, Claude API, OpenAI) is adapted to the *exact* `ollama.chat` calling convention (`chat(model, messages, tools, options, think, stream)`), so `agent.py` never knows which provider it's on. New backends must preserve this shape.
- **`claude_max.py`** — the exception: routes through the Claude Agent SDK / local `claude` CLI login. It strips Claude Code to bare inference and injects aish's own tools, so the approval gate still applies. Keeps its own session state.
- **`approval.py`** — conservative parser classifying commands as read-only for auto-approval. Philosophy: only approve what it *positively* understands; anything ambiguous falls through to a prompt. Auto-approval is scoped to session roots (launch dir + `/add-dir` + directories the user trusted at a prompt); path arguments escaping them (absolute, `~`, `..`, symlinks resolved) prompt even when allowlisted — `escaping_dirs()` names the escapes so the prompt/card can offer "trust this directory" (`Agent.trust_root`, session-memory only). When touching this file, err toward prompting.

Model execution is **stateless**: every `run_command` runs in the project directory (`Agent.cwd`), a bare model-issued `cd` is rejected with guidance instead of executing (see `CD_NOT_STICKY` in `agent.py`), and excursions are `cd x && ...` subshells (`cd` is in `SAFE_COMMANDS` because it is subshell-scoped and root-checked). Only user actions move cwd: `/cd` and its alias `!cd` both move cwd AND re-anchor `roots[0]` (`Agent.rebase`). This keeps the model permanently anchored to the project directory — do not reintroduce model-driven cwd mutation.
- **`tools.py`** — `run_command` (approval-gated shell) and `read_docs` (auto-approved, so it takes only a bare command name validated against PATH — never a shell string). The denylist (unrecoverable commands blocked even with approval) lives here.
- **`files.py`** — pure `plan_*` functions compute (old, new, diff) without touching disk; `commit()` writes. The gap between plan and commit is where the diff is shown and approval obtained — nothing is written unseen.
- **`web.py`** — `web_search`/`read_url`, auto-approved but their inputs leave the machine, so every call is echoed and fetched content is wrapped in an untrusted-content banner. http/https only.
- **`session.py`** — append-only JSONL per session in `~/.local/state/aish/`: conversation (for `--resume`), audit trail of every command decision, plus `kind:"trace"` records — the structured activity-trace steps (the same dicts the web renders), logged from BOTH entry points via the agent's `step_log` sink so the trace is UI-agnostic and survives eviction/restart. Terminal-block framing (`kind:"cmd_start"`/`"cmd_end"`) is persisted the same way via the `command_log` sink. `reconstruct_events()` replays a logged session as the EXACT event stream a rich client emits live — `user`/`step`/`done` plus, for each run_command, its full `command_start → stream → command_end → tool` sequence — so a cold-loaded web session feeds the frontend identical events and renders an identical terminal block (no fallback path). The command's output rides on the `tool` step (not duplicated in the framing records) and is spliced back in as one `stream` with run_command's trailing `[exit code: N]` marker stripped (`_EXIT_MARKER_RE`), matching the live body where the code arrives via `command_end`; a legacy tool step with no framing gets a synthesized block. One `done` per task carries the final assistant text; it returns `None` for pre-trace logs, and the server falls back to a flat `history` blob then. `step_log`/`command_log` are orthogonal to the agent's `on_step`/`on_command_*` (rich-renderer hooks; `on_step` gates terminal chatter via `_note`) — the CLI wires the log sinks only, so it logs without changing its inline output. Its ranked excerpt search (`search_excerpts`, output hard-capped) backs the sessions section of the `recall` tool; the agent gets `state_dir` + `current_session` at construction and excludes the live session from results.
- **`prompt.py`** — the boxed input UI, built as a small prompt_toolkit `Application` (not `PromptSession`) because the footer-under-input layout requires it.
- **`server.py`** — `aish-web`: the same Agent behind a Starlette WebSocket instead of a TTY. `Bridge` is the core mechanism: `run_task` runs via `asyncio.to_thread`, callbacks emit JSON events through `call_soon_threadsafe`, and the approval callbacks block the worker on a `queue.Queue` slot until the browser answers — the approval gate is unchanged, only the transport differs. The process holds many open `Session`s (each its own Agent + SessionLog + Bridge + busy flag, capped at `MAX_OPEN_SESSIONS` with idle eviction) but shows ONE to the single client: background sessions keep running, their events buffering into their transcript (`Bridge.attached` gates the outbox); switching sessions = hello + full transcript replay, which is also what makes phone lock/unlock lossless. A session that fell out of memory (idle-evicted or after a restart) is reopened cold from its log and reconstructed into the same replay event stream (`SessionLog.reconstruct_events`), so its activity trace looks identical hot or cold. The vanilla-JS frontend lives in `aish/static/` (no build step, iOS-styled, inline SVG icons). Web approvers mirror `cli.make_approver` exactly (denylist first, also on edited commands; auto-approval scoped to live roots; per-session "allow this session" prefixes; "Always allow" saves the card's shown prefixes to the same persistent allowlist file as the CLI's `a` answer).
- **`skills.py`** — the knowledge store: skills (playbooks, `./.aish/skills/` project-wins or `~/.config/aish/skills/`) and memory (one fact per file, same md+frontmatter format, `.aish/memory/` dirs), plus legacy `lessons.md` lines as synthetic memory entries until `/learn lessons` migrates them. Progressive disclosure is the design rule: `knowledge_index()` renders a capped, byte-stable name+description index that `agent.compose_system_content()` rebuilds into `messages[0]` at EVERY `run_task` (that live rescan is what makes new skills appear without restart — don't reintroduce boot-time caching); full bodies load via `read_skill`; the long tail is searched by the `recall` tool (`rank_entries` + `recall_text`: deterministic difflib tiers, two-phase, hard caps, mtime-cached parsing). A per-task `TASK_REMINDER` system message is re-inserted before each user message (directly, never via `_append`, so it stays out of logs/transcripts) — recency is what makes small local models actually consult skills. `remember` writes via `save_memory` (slug-validated, create-or-update, deduped) and stays auto-approved; skill files are written with normal diff-approved `write_file`/`edit_file`.

Startup safety: launching either entry point from `$HOME` re-anchors the session to `~/aish` (`cli.default_workspace`, also used by `create_app`) so the home tree never becomes the auto-approval root; explicit `cwd` overrides are respected.

Web-only UI conventions the model is told about (in `web_usage_context`): quick-reply chips — `[Label](aish-reply://answer text)` links rendered as one-tap buttons by `app.js` — and the approval-card feedback field that produces `Denied(comment)`.

## Testing pattern

Tests script the model side instead of running one: `FakeChat` (see `tests/test_agent.py`) returns pre-canned responses shaped like the ollama library's, injected via `Agent(client_chat=...)`. Tool implementations are monkeypatched at the module level (`agent_module.tools.read_docs`, `agent_module.web.web_search`). Follow this pattern — tests must run with no model, no network, no real command execution.

## Documentation duplication

The README's user-facing docs are also summarized in the agent's own system prompt (`SYSTEM_PROMPT_TEMPLATE` in `agent.py` — aish answers questions about itself). When changing user-visible behavior, update both the README and the system prompt text.
