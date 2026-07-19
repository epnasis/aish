---
name: verify
description: Drive the real aish-web UI (server + frontend) with a scripted model backend to verify web-facing changes end to end.
---

# Verifying aish changes at the web surface

The web UI is the main surface (approval cards, diffs, streaming). Drive it
with the REAL server and REAL frontend but a scripted model — same shape as
`tests/test_server.py`'s `FakeChat` — so no model or network is needed.

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
3. Drive with the Chrome tools: type into the "Ask aish" box and click the
   send arrow (programmatic form_input + Enter does NOT submit — click the
   button). Each queued task pops the next scripted response.
4. Observability: print tool-role messages inside `ScriptedChat.__call__` —
   that is exactly what the model receives (approval notes, denial guidance).
   Check side effects (files, allowlist) in the temp workdir.

## Gotchas

- One response is popped per agent turn; script enough pairs
  (tool_call + final text) for every task you plan to send.
- `stream=True` must return `iter([response])`.
- The card's feedback field swallows Enter (by design); use buttons.
