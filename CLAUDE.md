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

## Architecture

The core invariant: **the model never executes anything directly.** Backends only return structured tool-call requests; `Agent._dispatch()` in `agent.py` is the single execution point, and `run_command` is unreachable there unless the `approve()` callback returns the (possibly user-edited) command.

Data flow: `cli.py` (REPL/argv, slash commands, rendering) constructs an `Agent` with a chat callable from `backends.py` and an `approve` callback, then calls `agent.run_task()`. The agent loops: model proposes tool calls → `_dispatch()` executes them (gated) → results appended → repeat until a final answer.

- **`backends.py`** — routes `--model` strings to a chat callable. Every backend (Ollama, Gemini, Claude API, OpenAI) is adapted to the *exact* `ollama.chat` calling convention (`chat(model, messages, tools, options, think, stream)`), so `agent.py` never knows which provider it's on. New backends must preserve this shape.
- **`claude_max.py`** — the exception: routes through the Claude Agent SDK / local `claude` CLI login. It strips Claude Code to bare inference and injects aish's own tools, so the approval gate still applies. Keeps its own session state.
- **`approval.py`** — conservative parser classifying commands as read-only for auto-approval. Philosophy: only approve what it *positively* understands; anything ambiguous falls through to a prompt. Auto-approval is scoped to session roots (launch dir + `/add-dir`); path arguments escaping them (absolute, `~`, `..`, symlinks resolved) prompt even when allowlisted. When touching this file, err toward prompting.
- **`tools.py`** — `run_command` (approval-gated shell) and `read_docs` (auto-approved, so it takes only a bare command name validated against PATH — never a shell string). The denylist (unrecoverable commands blocked even with approval) lives here.
- **`files.py`** — pure `plan_*` functions compute (old, new, diff) without touching disk; `commit()` writes. The gap between plan and commit is where the diff is shown and approval obtained — nothing is written unseen.
- **`web.py`** — `web_search`/`read_url`, auto-approved but their inputs leave the machine, so every call is echoed and fetched content is wrapped in an untrusted-content banner. http/https only.
- **`session.py`** — append-only JSONL per session in `~/.local/state/aish/`: conversation (for `--resume`) plus audit trail of every command decision.
- **`prompt.py`** — the boxed input UI, built as a small prompt_toolkit `Application` (not `PromptSession`) because the footer-under-input layout requires it.
- **`server.py`** — `aish-web`: the same Agent behind a Starlette WebSocket instead of a TTY. `Bridge` is the core mechanism: `run_task` runs via `asyncio.to_thread`, callbacks emit JSON events through `call_soon_threadsafe`, and the approval callbacks block the worker on a `queue.Queue` slot until the browser answers — the approval gate is unchanged, only the transport differs. The process holds many open `Session`s (each its own Agent + SessionLog + Bridge + busy flag, capped at `MAX_OPEN_SESSIONS` with idle eviction) but shows ONE to the single client: background sessions keep running, their events buffering into their transcript (`Bridge.attached` gates the outbox); switching sessions = hello + full transcript replay, which is also what makes phone lock/unlock lossless. The vanilla-JS frontend lives in `aish/static/` (no build step, iOS-styled, inline SVG icons). Web approvers mirror `cli.make_approver` exactly (denylist first, also on edited commands; auto-approval scoped to live roots; per-session "allow this session" prefixes); the persistent "always allow" list is deliberately terminal-only.
- **`skills.py`** — markdown playbooks from `./.aish/skills/` (project, wins on clash) or `~/.config/aish/skills/`.

## Testing pattern

Tests script the model side instead of running one: `FakeChat` (see `tests/test_agent.py`) returns pre-canned responses shaped like the ollama library's, injected via `Agent(client_chat=...)`. Tool implementations are monkeypatched at the module level (`agent_module.tools.read_docs`, `agent_module.web.web_search`). Follow this pattern — tests must run with no model, no network, no real command execution.

## Documentation duplication

The README's user-facing docs are also summarized in the agent's own system prompt (`SYSTEM_PROMPT_TEMPLATE` in `agent.py` — aish answers questions about itself). When changing user-visible behavior, update both the README and the system prompt text.
