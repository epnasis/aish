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

Quality-of-life on top of the pillars:

- Positively-identified read-only commands (`ls`, `grep`, `find` without `-exec`, …)
  auto-approve to avoid prompt fatigue; anything the conservative parser doesn't fully
  understand still prompts. Disable with `--ask-all`.
- `read_docs` takes an optional `topic` to search full man pages past the truncation
  limit (the model is told about this whenever docs come back truncated).
- Old tool outputs are compacted between tasks and under context pressure, so long
  REPL sessions never silently evict the system prompt.

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
