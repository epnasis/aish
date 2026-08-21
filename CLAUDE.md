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

## Shipping

After changing code, the user's installed `aish` does NOT pick it up — uv freezes the wheel and caches it at the same version. Ship with:

```sh
make ship          # guard → lint → tests → reinstall → restart → health-check
make ship-check    # the preflight alone: what would ship, and whether it may
```

**Never run `uv tool install` by hand.** It builds the wheel from the **WORKING TREE, not from HEAD**, so anything uncommitted — including another session's half-finished work in a checkout you did not inspect — ships silently. `make ship` refuses on a dirty tree for exactly this; `--dirty` overrides it when you mean it. On 2026-08-11 a bare install came within one command of shipping 129 uncommitted frontend lines that were also failing two doc-gate tests, and nothing about the command would have said so. `scripts/ship.sh`, `tests/test_ship_guard.py`.

The remote equivalent is `scripts/deploy-web.sh <host>`, which ships the working tree ON PURPOSE (that is the remote dev loop) but asks first when the tree is dirty — and, with no tty to ask on, refuses rather than assuming consent.

## Workflow: always work in a worktree

**Every change goes in a git worktree on a feature branch (via EnterWorktree), then merges to main. There is no trivial-enough exception.** This checkout is never edited directly — not for a one-line fix, not for a doc tweak.

Why the rule has no exception: this checkout may hold the owner's or another session's in-flight work, and a `git add -A` sweeps it into an unrelated commit while a wheel built from that tree ships it silently (see Shipping). On 2026-08-18 a whole multi-file feature was built and committed straight to main while a worktree created for it sat unused — the tree happened to be clean, so nothing was captured, and nothing about the commands would have said otherwise. A rule with a "trivial" carve-out is a rule you decide about under time pressure; this one you do not.

**The way this goes wrong is `cd`.** Prefixing a shell call with `cd /Users/epnasis/dev/aish` silently leaves the worktree, because the worktree lives *inside* that path. Confirm `pwd` is the worktree before the first edit, and keep it there.

Merge back to main after tests pass, then remove the worktree. `make ship` always runs from this main checkout after merge — never from a worktree path, and never as a bare `uv tool install` (see Shipping).

## Never type into a chat you did not create

**A verification run MUST NOT send a message to a chat it did not start.** Use the isolated harness in `.claude/skills/verify/SKILL.md` (its own `state_dir`, port 8899); if a check genuinely needs the live server, click ＋ for a NEW chat first and drive only that. The port is not a defence — `scripts/aish-preview.sh` points preview at PROD's `AISH_STATE_DIR` deliberately, so :8788 writes the same sessions as :8787. On 2026-07-28 a verification run drove the live UI through Chrome while the browser was parked on the owner's own shopping chat and typed five probes into it; a chat has no way to delete a message, so they are permanent, they auto-retitled the chat, and days later they were still what the owner saw when the chat flagged itself as having something new (#201).

## Where the knowledge lives

**This file is a routing table, not the knowledge.** It holds the rules that apply to every task; the rationale for each area lives in `docs/` and is read on demand.

**Read an area's doc BEFORE changing code in it.** Each one records why the code is shaped the way it is — invariants, ownership fences, and the bug that put them there. Almost none of it is derivable from the code, and several of the fences look like arbitrary indirection until you read why they exist.

| Touching… | Read first |
|---|---|
| `agent.py`, `claude_max.py`, `approval.py`, `tools.py`, `files.py`, `web.py`, `backends.py` | `docs/agent-core.md` |
| `browser.py`, `browse.py`, `_browser_read`, `_login_gate`, `_browse_gate`, anything that renders or drives a page in Chrome | `docs/browser.md` |
| `cli.py`, `prompt.py`, `aliases.py`, `dir_ignore.py`, `notify.py` | `docs/cli.md` |
| `media.py`, `show_image`, anything that renders an image | `docs/media-and-images.md` |
| `recordings.py`, `read_media`, anything that reads a video or audio file | `docs/recordings-and-video.md` |
| `documents.py`, `read_pdf`, `workspace_roots` (where aish may read unasked) | `docs/documents-and-pdf.md` |
| anything that emits or replays a trace record | `docs/trace-contract.md` (binding schema) + `docs/trace-records.md` (rationale) |
| `session.py` | `docs/session-log.md` |
| `server.py`, `pty_session.py`, `email_poll.py` | `docs/web-server.md` |
| `aish/static/` (`app.js`, `style.css`, `sw.js`) | `docs/web-frontend.md` |
| `skills.py`, `embeddings.py`, `curate.py`, `skill_import.py` | `docs/knowledge-layer.md` |
| `rules.py`, `rule_compiler.py`, `seed_rules`, `_rule_gate` | `docs/rules-engine.md` |
| `tool_plugins.py`, `secrets.py` | `docs/tools-layer.md` |
| `explain.py`, `evidence.py`, the `brief` record | `docs/diagnostics.md` |
| `ratelimit.py`, `_chat_turn`'s retry loop, anything that retries a model call | `docs/rate-limits.md` |
| `usage.py`, `aish usage`, anything that reports or attributes token spend | `docs/token-accounting.md` |
| `export.py` | `docs/export-pdf.md` |

**New rationale goes in the area's doc, never here.** This file spent a year as a decision log — a paragraph per issue — and grew past the 150k-char limit Claude Code warns at, which is the point where the whole thing stops being reliable context. `tests/test_claude_md_size.py` fails if it passes 40k. Add a line here only for a rule that applies to *every* task; everything else belongs in `docs/`.

## Architecture

The core invariant: **the model never executes anything directly.** Backends only return structured tool-call requests; `Agent._dispatch()` in `agent.py` is the single execution point, and `run_command` is unreachable there unless the `approve()` callback returns the (possibly user-edited) command.

Data flow: `cli.py` (REPL/argv, slash commands, rendering) constructs an `Agent` with a chat callable from `backends.py` and an `approve` callback, then calls `agent.run_task()`. The agent loops: model proposes tool calls → `_dispatch()` executes them (gated) → results appended → repeat until a final answer. Approver verdicts: `True`/edited-string approve, `None`/`False` deny, `Blocked(reason)` (denylist — unapprovable), `Denied(comment)` and `Approved(comment, command=None)` — both web-card-only (CLI approvers still return bool/str/None), and both constructed ONLY when the user typed a comment. **A comment makes approve and deny mean opposite things (issue #81):** `Denied(comment)` = STOP — the action is denied, the comment appended to the denial result, and the **stop gate** armed; `Approved(comment, ...)` = CONTINUE but ADJUST — the original action is HELD (never run, even if `command` was edited), and the model is told to rework it to the comment and re-propose (the adjusted action is approved again before it runs). The **stop gate** (`_stop_gate`, arms via `_arm_stop_gate`, only denials arm it): eager models run another tool before addressing a denial, so while `self._pending_comment_response` is set every tool call is refused; the main loop clears it only on a **text-only turn** (not a same-turn text+tool: chatty preamble alongside a command would otherwise slip past), and that text-only turn ends the task — deny means stop. `run_task` resets the flag so it never leaks across tasks. Approvals never arm the gate (continue); the HOLD + re-approval is what keeps an unwanted original command from running. The step budget is **progress-gated (#108)**, not a flat cap: a step counts as progress when a tool call yields a (tool, args, result) tuple seen for the first time (reusing the loop detector's `repeats` dict — no extra model call, no timer). A progressing task may run past `max_steps` up to the unconditional hard ceiling `max(max_steps, HARD_STEP_CEILING=60)`; `MAX_STALL_STEPS` (8) consecutive no-progress steps stop it early. The loop ends three ways: a text-only answer, a cancel, or `_finish_stopped()` — reached at a stall, the hard ceiling, or when loop detection fires (identical call + identical result: warn at 3 repeats, stop at 5) — which runs one final no-tools turn so the model reports completion state instead of cutting off; the ceiling is never silently exceeded.

Model execution is **stateless**: every `run_command` runs in the project directory (`Agent.cwd`), a bare model-issued `cd` is rejected with guidance instead of executing (see `CD_NOT_STICKY` in `agent.py`), and excursions are `cd x && ...` subshells (`cd` is in `SAFE_COMMANDS` because it is subshell-scoped and root-checked). Only user actions move cwd: `/cd` and its alias `!cd` both move cwd AND re-anchor `roots[0]` (`Agent.rebase`). This keeps the model permanently anchored to the project directory — do not reintroduce model-driven cwd mutation.

### Module map

- **`agent.py`** — the loop and the single execution point (`_dispatch`); approval verdicts, the stop gate, the progress-gated step budget. → `docs/agent-core.md`
- **`backends.py`** — routes `--model` strings to a chat callable. Every backend (Ollama, Gemini, Claude API, OpenAI) is adapted to the *exact* `ollama.chat` calling convention (`chat(model, messages, tools, options, think, stream)`), so `agent.py` never knows which provider it's on. New backends must preserve this shape.
- **`claude_max.py`** — the exception: routes through the Claude Agent SDK / local `claude` CLI login, stripping Claude Code to bare inference and injecting aish's own tools so the approval gate still applies. → `docs/agent-core.md`
- **`approval.py`** — conservative parser classifying commands as read-only for auto-approval, scoped to session roots. When touching this file, err toward prompting. → `docs/agent-core.md`
- **`tools.py`** — `run_command` (approval-gated shell), `read_docs`, the denylist, and the `ToolOutcome` result envelope. → `docs/agent-core.md`
- **`files.py`** — pure `plan_*` functions compute (old, new, diff) without touching disk; `commit()` writes. The gap between plan and commit is where the diff is shown and approval obtained — nothing is written unseen. → `docs/agent-core.md`
- **`web.py`** — `web_search`/`read_url`/`fetch_binary`: auto-approved, but their inputs leave the machine, so every call is echoed and fetched content is wrapped in an untrusted-content banner. http/https only, one SSRF guard, one TLS trust store. → `docs/agent-core.md`
- **`browser.py`** — the real Chrome behind `read_url` when a fetch is not enough: JavaScript-only pages and sites the owner is signed into. Headful but off-screen (headless is what such sites block), one owner thread, one persistent profile that is never in the git-backed config tree. Reading as the signed-in owner is gated in EVERY session. → `docs/browser.md`
- **`browse.py`** — the model's own hands on a page (#237): a page as a NUMBERED LIST OF CONTROLS, and the labelling that decides which of them need their own approval card. The browser-free half of `browser.browse_*`. → `docs/browser.md`
- **`media.py`** — the content-addressed, bounded-LRU media store behind `show_image`. → `docs/media-and-images.md`
- **`recordings.py`** — video and audio read by TIME: probe once, then seek. Frames come from a range-seek into the stream, never a download, and each is stamped with the timestamp it was actually decoded at. → `docs/recordings-and-video.md`
- **`documents.py`** — PDF → a page-marked markdown rendition behind `read_pdf`: convert once, then read it like a file. Every page is classified (text / columns / table / scan / figure) BEFORE it is read, so a hollow extraction can never pass as a complete one. → `docs/documents-and-pdf.md`
- **`session.py`** — append-only JSONL per session in `~/.local/state/aish/`: conversation, audit trail, trace records, and the replay that makes a cold session render identically to a live one. → `docs/session-log.md`
- **`cli.py`** — the terminal client: REPL, argv, slash commands, model picker, rendering. Gates identically to the web; a terminal session dies with its terminal and is never resurrected. → `docs/cli.md`
- **`prompt.py`** — the boxed input UI, a small prompt_toolkit `Application` (not `PromptSession`) because the footer-under-input layout requires it. → `docs/cli.md`
- **`aliases.py`** — user aliases expanded BEFORE the approval gate, so the gate parses the real command and not an opaque name. → `docs/cli.md`
- **`notify.py`** — Pushover sending; unconfigured or failing is a silent no-op that never raises into the approval path. → `docs/cli.md`
- **`server.py`** — `aish-web`: the same Agent behind a Starlette WebSocket instead of a TTY. The approval gate is unchanged; only the transport differs. → `docs/web-server.md`
- **`aish/static/`** — the vanilla-JS frontend (no build step, iOS-styled). → `docs/web-frontend.md`
- **`seen.py`** — the seen ledger: when the OWNER last read each chat, shared by every device. Monotonic and server-clocked, which is what makes sharing it safe. → `docs/web-server.md`
- **`pty_session.py`** — the PTY behind the one global interactive console. The model has NO write path to it, by construction. → `docs/web-server.md`
- **`email_poll.py`** — the Gmail→`/trigger` poller; both effectful edges are parameter seams, so it tests with no subprocess and no network. → `docs/web-server.md`
- **`skills.py`** — the knowledge store (skills + memory): progressive disclosure, pre-flight injection, lifecycle. → `docs/knowledge-layer.md`
- **`embeddings.py`** — the semantic layer over retrieval. Lexical word-matching is the guaranteed floor; embeddings are an upgrade, never a dependency. → `docs/knowledge-layer.md`
- **`curate.py`** — the retrieval self-curation loop; the orchestration lives in the script, not in a model session. → `docs/knowledge-layer.md`
- **`skill_import.py` + `import_skill`** — import a skill from a git repo or local path under ONE consolidated review. Safety is enforced in code, not by asking the model. → `docs/knowledge-layer.md`
- **`rules.py`** — the rules engine (#191): owner-authored turn contracts the harness enforces. Rules only ever RESTRICT, so a bug over-restricts and can never under-restrict. → `docs/rules-engine.md`
- **`rule_compiler.py`** — the owner's plain language → rule field values (#205). Isolated because it is more accurate; safe because code validates it and the owner approves it. The acting model never learns the grammar. → `docs/rules-engine.md`
- **`tool_plugins.py`** — droppable `TOOL.md` plugin tools, indistinguishable from native ones to the model and gated by the same `_dispatch`. → `docs/tools-layer.md`
- **`secrets.py`** — local secret store backed by the macOS login Keychain; structurally un-committable. → `docs/tools-layer.md`
- **`explain.py` + `evidence.py`** — reading back why a turn went the way it did: `aish explain` assembles a
  dossier from recorded evidence only, and the content-addressed evidence store holds the bytes it points at,
  purgeably. No model call, and it must never be able to re-derive behaviour from source. → `docs/diagnostics.md`
- **`ratelimit.py`** — what a failed model call WAS (quota vs blip vs permanent), how long to
  wait, and the record that says it happened. There must be exactly one retry policy and aish
  owns it; a 429 that recovered still leaves evidence. → `docs/rate-limits.md`
- **`usage.py`** — what was spent and what filled the context that made it cost that. A pure
  scan over the logs, under the same reader law as `explain`; it reports what was RECORDED
  and says plainly what it cannot know. → `docs/token-accounting.md`
- **`export.py`** — local Markdown → PDF for the web UI; the text never leaves the machine. → `docs/export-pdf.md`
- **`dir_ignore.py`** — the configurable gitignore-style ignore list shared by the web folder browser and @-file completion. Name-level `fnmatch` on basenames only — it must never add a per-subfolder stat. → `docs/cli.md`

Startup safety: launching either entry point from `$HOME` re-anchors the session to `~/aish` (`cli.default_workspace`, also used by `create_app`) so the home tree never becomes the auto-approval root; explicit `cwd` overrides are respected.

Web-only UI conventions the model is told about (in `web_usage_context`): quick-reply chips — `[Label](aish-reply://answer text)` links rendered as one-tap buttons by `app.js` — and the approval-card feedback field that produces `Denied(comment)`.

## Testing pattern

Tests script the model side instead of running one: `FakeChat` (see `tests/test_agent.py`) returns pre-canned responses shaped like the ollama library's, injected via `Agent(client_chat=...)`. Tool implementations are monkeypatched at the module level (`agent_module.tools.read_docs`, `agent_module.web.web_search`). Follow this pattern — tests must run with no model, no network, no real command execution. Frontend logic gets dependency-free Node checks in `tests/js/`: the REAL function is extracted from app.js by source markers and run in a `vm` context against a minimal fake DOM — the shipped code is tested, never a hand-copied duplicate. `tests/test_frontend_js.py` runs each script inside the pytest suite (skipped when node is absent).

**Tests must never reach the outside world, and "no network" includes the developer's phone.** `notify.configured()` reads the LIVE macOS Keychain, so any test that runs a triggered session to completion with no viewer reaches `_notify_done` and sends a REAL Pushover push — the restart-recovery tests did exactly that, firing two notifications on every `uv run pytest` (bodies `read it` / `sent the reply`, and no deep-link, since the test env sets no `AISH_PUBLIC_URL` — that missing URL is what identified them). The autouse `no_real_notifications` fixture in `tests/conftest.py` sets `AISH_NOTIFY=0` suite-wide, which short-circuits `pushover()` before it touches credentials or the network; tests asserting notification behaviour monkeypatch `notify.pushover`/`configured` themselves and are unaffected, and `test_notify.py` (which exercises the sender) opts out via its own autouse fixture with `urlopen` stubbed. The general rule: a module that reaches a real credential store needs a suite-wide guard, not per-test discipline — `test_suite_never_reaches_the_real_notifier` pins it.

The choreography harness (`tests/js/harness.js`) and the ownership lint (`tests/js/test_ownership.js`) are described in `docs/web-frontend.md`. A race fix ships with its interleaving as a test, not just its decision function.

## Documentation duplication

The README's user-facing docs are also summarized in the agent's own system prompt (`SYSTEM_PROMPT_TEMPLATE` in `agent.py` — aish answers questions about itself). When changing user-visible behavior, update both the README and the system prompt text.
