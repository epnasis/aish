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
harness below (its own `state_dir`, `allow_path`, `deny_path`, `config_path`
and `cwd`, on port 8899); if a check genuinely needs the live server, click ＋
for a NEW chat first and drive only that.

This is not hygiene, it is a real incident. On 2026-07-28 a verification run
drove the live UI through Chrome while the browser was parked on the owner's
own shopping chat, and typed five probes into it — `BANANA`, `KIWI`,
`render-once-probe-37472`, two `read_url on example.com`. There is no way to
delete a message from a chat, so they were permanent, they auto-retitled the
chat, and they were still being read as "what's new here" days later.

Port is not a defence: `scripts/aish-preview.sh` points preview at PROD's
`AISH_STATE_DIR` on purpose, so :8788 writes the same sessions as :8787.

## The harness is NOT fully isolated — know what it still shares (#254)

The temp `state_dir` isolates sessions. It does **not** isolate the knowledge
layer: `rules.GLOBAL_RULES_DIR`, `skills.GLOBAL_SKILLS_DIR` and
`skills.GLOBAL_MEMORY_DIR` all resolve from `Path.home()` with no override, so
a verification run reads — and could write — the owner's live rules, skills and
memory. `AISH_CONFIG` does not help; it names `config.toml` only.

Two consequences, and the second is the one that bites:

- **The owner's rules govern your run.** Verifying #252, a scripted step with
  no narration never reached an approval card at all: `answer-me-first` refused
  the tool call and re-prompted first, which reads exactly like the feature
  being broken. Expect rules in front of anything you are trying to observe,
  and check the corpus before concluding the harness is wrong.
- **`remember` / `create_skill` / `import_skill` write for real.** Approving
  one of those on a harness card lands in `~/.config/aish/`, permanently. Only
  your choice of scripted tool calls prevents it. Do not script them.

## Recipe

1. Write a launcher script (scratchpad) that:
   - builds `SimpleNamespace` responses shaped like ollama's
     (`model_says` / `tool_call` helpers — copy from `tests/test_server.py`),
   - passes a `ScriptedChat` class as `client_chat=` to
     `aish.server.create_app(...)` with isolated `state_dir` / `allow_path` /
     `deny_path` / `cwd` under a temp dir,
   - runs `uvicorn.run(app, host="127.0.0.1", port=8899)`.
2. `uv run python <script> <workdir>` in the background; wait for
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
