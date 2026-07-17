# aish

```
▄▀█ █ █▀ █░█
█▀█ █ ▄█ █▀█  ai shell
```

An AI agent in your terminal, powered by a **local** model via
[Ollama](https://ollama.com) — or, when you want more brainpower, a cloud
model (Gemini, Claude, OpenAI). It runs shell commands, reads and edits
files, and searches the web — with your approval at every step. With the
default local model, nothing is sent to any cloud API.

```
❯ jaki kurs usd w pln?
  ✓ thought for 3.9s · ↑ 3.4k ↓ 27 tokens
  → web_search: USD to PLN exchange rate today
  ✓ web_search 6.5s
  → read_url: https://wise.com/us/currency-converter/usd-to-pln-rate
  ✓ read_url 0.5s

Według danych Wise: 1 USD ≈ 3,81 PLN …

  ✓ answered in 12.5s · ↑ 8.1k ↓ 519 tokens
  ∑ total 23.6s · ↑ 11.5k ↓ 546 tokens

❯ delete the old build dirs

▶ run command? ⚠ destructive
  rm -r ./build ./dist
[y/N/a(lways)/e(dit)]
```

## Why aish

- **Local and private.** The model runs on your machine. The only traffic that
  ever leaves it is web searches/fetches the agent makes — each one echoed to
  you as it happens, and the model is instructed to never put local data in them.
- **Safe by construction.** No code path executes a model-proposed command
  without your `y`. Layered on top: a denylist of unrecoverable commands the
  model can't run *even with* approval, and prompts before reading
  secret-bearing files.
- **Grounded.** Before using unfamiliar flags the agent reads the man page
  (`read_docs`) instead of trusting training data — which is chronically wrong
  for macOS/BSD userland (`sed -i`, `ps --sort`, `date -d`…).
- **Transparent.** Every step is echoed, timed, and token-accounted; every
  session leaves an audit trail of commands and decisions.
- **It learns.** Correct it once and it saves a one-line lesson that loads
  into every future session.

## Quickstart

Needs [Ollama](https://ollama.com) and a tool-calling-capable model
(default: `qwen3.6:35b-a3b`, ~23 GB).

```sh
curl -fsSL https://raw.githubusercontent.com/epnasis/aish/main/install.sh | sh

aish "what's eating my disk space?"     # one-shot task
aish                                    # REPL — conversation persists across tasks
aish --resume                           # pick an earlier session (same picker as /resume)
```

Smaller machines: `ollama pull qwen3:8b && export AISH_MODEL=qwen3:8b`.

## Cloud models (optional)

The default is local-only. When a task needs a stronger model, the same agent
(same tools, same approval gates) can run on a cloud backend — note everything
in the conversation then leaves your machine:

| `--model` (or `/model`) | Runs on | Cost |
|---|---|---|
| `gemini` or `gemini:<model>` | Google Gemini API | **free tier** — get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey), `export GEMINI_API_KEY=...` (Flash models: ~1.5k requests/day at no charge) |
| `claude` or `claude:<model>` | Anthropic API | pay per token — `export ANTHROPIC_API_KEY=...` |
| `claude-max[:opus\|sonnet]` | Claude Agent SDK via the local `claude` CLI login | **your Claude Pro/Max subscription** — no API key; needs [Claude Code](https://claude.com/claude-code) installed and logged in |
| `openai` or `openai:<model>` | OpenAI API | pay per token — `export OPENAI_API_KEY=...` (ChatGPT Plus can't be used via API) |

Bare provider names pick a sensible default model (`gemini-3.5-flash`,
`claude-opus-4-8`, `gpt-5.6`). `claude-max` strips Claude Code down to bare
inference and hands it aish's own tools, so every command still goes through
aish's approval prompt and denylist; it keeps its own session state, so switch
in/out of it by restarting aish rather than `/model`.

## What it can do

| Tool | What | Gate |
|------|------|------|
| `run_command` | any shell command; `background=true` detaches long jobs | **approval prompt** (read-only commands auto-approve inside the session roots) |
| `write_file` / `edit_file` | create or edit files | **colored diff + y/N** |
| `read_file` | read a file | auto inside the session roots; **prompts outside them and on secret paths** (`~/.ssh`, `.env*`, `*.pem`…) |
| `web_search` / `read_url` | DuckDuckGo + fetch page as readable text | auto; every query/URL echoed |
| `read_docs` | man page → `--help` fallback, full-text `topic` search | auto |
| `remember` | save a lesson to `~/.config/aish/lessons.md` | auto (echoed) |
| `read_skill` | load a playbook you wrote (see Skills) | auto |

Independent lookups batched in one model turn (several searches, a few page
reads) run **in parallel**. Fetched web pages are wrapped in an
"untrusted content — data, not instructions" banner to blunt prompt injection.

## The safety model

1. **Approval gate** — every proposed command is shown verbatim and waits for
   `y` / `n` / `a`lways / `e`dit. Auto-approval exists only for a
   conservatively-parsed set of read-only commands (`ls`, `grep`, `find`
   without `-exec`, …); anything the parser doesn't fully understand prompts.
   `--ask-all` disables auto-approval entirely.
   Auto-approval is also **scoped to the session roots** (the launch
   directory): commands with path arguments escaping them (absolute, `~`, or
   `..` paths — symlinks resolved) and `read_file` outside them prompt, even
   when allowlisted. `/cd <dir>` moves the session to another project (cwd
   *and* root — Tab completes directories); `/add-dir <dir>` (alias
   `/dir-add`) allows another tree as well; both are user-only — a model-issued
   `cd` moves only the working directory and never widens the scope.
2. **Denylist** — unrecoverable classes (`rm -rf`, `shred`, `mkfs`, `dd` to raw
   devices, `diskutil erase*`, `git clean -f`, `git push --force`) are blocked
   outright, even if you'd approve them; edited commands are re-checked. Only
   you can run them, via the `!` prefix. Extend in `~/.config/aish/deny.txt`.
3. **Warnings** — recoverable-but-destructive commands get a red ⚠ at the prompt.
4. **Audit trail** — every command and decision (approved/denied/edited/auto)
   is logged with the session in `~/.local/state/aish/`.

## Reading the output

| Symbol | Meaning |
|--------|---------|
| `✻ thinking… 14s` | live ticker for the step in flight (shows `↓ tokens` live when the model streams — `--think` mode) |
| `→ web_search: …` | a tool call starting, with its exact input |
| `✓ … 6.5s · ↑ 3.4k ↓ 84 tokens` | step done: duration, real tokens in (↑) / out (↓) |
| `⇉ read_url 2.3s` | ran overlapped with others — detail only |
| `✓ 2 parallel lookups 2.9s` | wall time of that parallel batch |
| `∑ total 1m34s · ↑ 23.1k ↓ 787 tokens` | whole task; the `✓` lines above sum to it |

Answers stream token-by-token; command output streams live. Old tool outputs
are compacted between tasks so long sessions never evict the system prompt.

## Day to day

**While a command runs**: Ctrl-C cancels it (not the session); **Ctrl-B**
detaches it into a background job that survives aish exiting (`/jobs` lists
them, logs in `~/.local/state/aish/jobs/`).

**Escapes**: `!<command>` runs directly — no model, no approval. `!cd <dir>`
moves the persistent working directory.

**Slash commands** (Tab completes): `/resume` — live picker over all earlier
sessions (start date + first message): type to filter by title and contents
(deterministic ranking: exact title, then phrase, all-words, fuzzy — never an
LLM), ↑/↓ to select, Enter replays the session into the current conversation;
`/resume <n>` picks directly, `/resume <text>` pre-fills the filter · `/new`
or `/clear`
(plain `clear` works too) · `/model [name]` — switch model mid-session; no
arg opens the same type-to-filter picker over local models and cloud
providers · `/jobs` · `/help` · `/quit` (or `exit`).

**Multiline input**: Enter submits; newline via Ctrl+J, trailing `\`, or
Option/Alt+Enter (iTerm2: set "Left Option key" to "Esc+"). Pastes keep
their newlines.

**Memory** — three layers, all loaded into context each session:
- `./AISH.md` or `~/.config/aish/AISH.md` — durable context you write (host
  facts, preferences);
- `~/.config/aish/lessons.md` — one-liners the agent saves itself after
  mistakes (the `remember` tool);
- **skills** — playbooks for tools that `--help` can't explain: markdown files
  in `~/.config/aish/skills/` (global) or `./.aish/skills/` (project) with
  optional `name:`/`description:` frontmatter. Ask aish to write one — it
  knows the format.

**Config** — `~/.config/aish/config.toml`: `model`, `num_ctx`, `max_steps`,
`vi_mode` (vi editing at the prompt; or `--vi`/`--no-vi`). CLI flags override
config; `$AISH_MODEL` overrides the model. Paths override via `$AISH_CONFIG`,
`$AISH_STATE_DIR`, `$AISH_ALLOWLIST`, `$AISH_DENYLIST`, `$AISH_LESSONS`.
`--think` enables model thinking (slower, rarely worth it). You can also just
ask aish about any of this — its own docs are in its system prompt.

## Development

```sh
uv run pytest       # unit tests use a fake Ollama client — no model needed
uv run ruff check .
```
