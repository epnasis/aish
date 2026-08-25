---
name: verify
description: Drive the real aish-web UI (server + frontend) with a scripted model backend to verify web-facing changes end to end.
---

# Verifying aish changes at the web surface

The web UI is the main surface (approval cards, diffs, streaming). Drive it
with the REAL server and REAL frontend but a scripted model — same shape as
`tests/test_server.py`'s `FakeChat` — so no model or network is needed.

## The rule that comes before the recipe

**A verification MUST NOT type into a chat it did not create.** Use the
harness below (its own `state_dir`, `allow_path`, `deny_path`, `config_path`,
`cwd` and `AISH_CONFIG_HOME`, on port 8899 — the next section says exactly what
that does and does not cover); if a check genuinely needs the live server,
click ＋ for a NEW chat first and drive only that.

This is not hygiene, it is a real incident. On 2026-07-28 a verification run
drove the live UI through Chrome while the browser was parked on the owner's
own shopping chat, and typed five probes into it — `BANANA`, `KIWI`,
`render-once-probe-37472`, two `read_url on example.com`. There is no way to
delete a message from a chat, so they were permanent, they auto-retitled the
chat, and they were still being read as "what's new here" days later.

Port is not a defence: `scripts/aish-preview.sh` points preview at PROD's
`AISH_STATE_DIR` on purpose, so :8788 writes the same sessions as :8787.

## What the harness isolates, and what it still shares (#254)

Isolated, and you must pass every one of them: `state_dir`, `allow_path`,
`deny_path`, `config_path`, `cwd` — and, since #254, the owner's config tree
through the **`AISH_CONFIG_HOME`** environment variable. One knob moves all
four directories under it: `rules/`, `skills/`, `memory/`, `tools/`.

    AISH_CONFIG_HOME=$WORKDIR/config uv run python <script> $WORKDIR

**Set it in the environment, not in Python.** The four constants
(`rules.GLOBAL_RULES_DIR`, `skills.GLOBAL_SKILLS_DIR`,
`skills.GLOBAL_MEMORY_DIR`, `tool_plugins.GLOBAL_TOOLS_DIR`) are resolved at
IMPORT time, so a `setattr` after `import aish.server` is too late for
anything already bound.

Two reasons it matters, and the second is the one that bites:

- **The owner's rules govern your run.** Verifying #252, a scripted step with
  no narration never reached an approval card at all: `answer-me-first` refused
  the tool call and re-prompted first, which reads exactly like the feature
  being broken. With an empty config home the card appears immediately.
- **`remember` / `create_skill` / `import_skill` write for real.** Approving
  one of those on a harness card used to land in `~/.config/aish/`,
  permanently. The knob is what makes that structurally impossible; before it,
  only your choice of scripted tool calls prevented it.

Keep the owner's corpus (leave the variable unset) only when the rules or the
retrieval ARE what you are verifying; then expect them in front of everything
and read the trace before believing the feature is at fault.

**Still shared, and no knob moves them:**

- **The Keychain** — `notify.pushover` reads live Pushover credentials and
  pushes to the owner's real phone; `secrets` and `signin` read real stored
  credentials. Export `AISH_NOTIFY=0` unless notifications are the subject.
- **The browser profile and downloads**, which hang off `AISH_STATE_DIR` (the
  environment variable, not `create_app`'s `state_dir` argument) — a real
  Chrome driven here is signed in AS THE OWNER. Export `AISH_STATE_DIR` too if
  the run can reach `browser`.
- **The network and the real shell.** `run_command` executes for real inside
  `cwd`, and `web_search` / `read_url` leave the machine.

The pytest suite makes the same cut in `tests/conftest.py`
(`isolated_global_dirs`, `no_real_notifications`, `no_real_secrets`,
`no_real_browser`), pinned by `test_suite_never_reaches_the_real_knowledge_store`.

## Recipe

1. Write a launcher script (scratchpad) that:
   - builds `SimpleNamespace` responses shaped like ollama's
     (`model_says` / `tool_call` helpers — copy from `tests/test_server.py`),
   - passes a `ScriptedChat` class as `client_chat=` to
     `aish.server.create_app(...)` with isolated `state_dir` / `allow_path` /
     `deny_path` / `cwd` under a temp dir,
   - runs `uvicorn.run(app, host="127.0.0.1", port=8899)`.
2. `AISH_CONFIG_HOME=<workdir>/config uv run python <script> <workdir>` in the
   background; wait for
   `curl http://127.0.0.1:8899/` → 200.
3. Drive with the Chrome tools: type into the "Ask aish" box and either click
   the send arrow or press Cmd/Ctrl+Enter. A bare Enter does NOT submit — it
   inserts a newline, by design (`[ENTER]`, `docs/web-frontend.md`). Each
   queued task pops the next scripted response.
4. Observability: print tool-role messages inside `ScriptedChat.__call__` —
   that is exactly what the model receives (approval notes, denial guidance).
   Check side effects (files, allowlist) in the temp workdir.

## Gotchas

- One response is popped per agent turn; script enough pairs
  (tool_call + final text) for every task you plan to send.
- `stream=True` must return `iter([response])`.
- The card's feedback field swallows Enter (by design); use buttons.
