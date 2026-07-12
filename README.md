# aish

Local LLM agent that runs CLI commands — like a minimal Claude Code powered by Ollama.

Two design pillars:

- **Grounding**: before using a command with non-trivial flags, the agent calls its
  `read_docs` tool (man page → `--help` → `-h` fallback) instead of trusting training
  data, which is frequently wrong for macOS/BSD userland.
- **Mandatory approval**: every proposed shell command is shown verbatim and requires
  an explicit `y` before it executes. There is no code path that runs a model-proposed
  command without approval. `read_docs` is the only auto-approved tool and accepts a
  validated bare command name, never a shell string.

## Usage

```sh
uv sync
uv run aish "how much disk space do node_modules dirs use under ~/dev?"
uv run aish                 # interactive REPL, conversation persists across tasks
```

Model defaults to `qwen3.6:35b-a3b` (override with `--model` or `$AISH_MODEL`).
Requires a tool-calling-capable Ollama model. `--think` enables model thinking
(much slower, rarely worth it).

## Development

```sh
uv run pytest       # unit tests use a fake Ollama client — no model needed
uv run ruff check .
```
