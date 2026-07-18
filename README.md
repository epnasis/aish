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
[y/N/a(lways)/s(ession)/e(dit)]
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
| `web_search` / `read_url` | DuckDuckGo + fetch page as readable text | auto; every query/URL echoed; public hosts only |
| `read_docs` | man page → `--help` fallback, full-text `topic` search | auto |
| `remember` | save a lesson to `~/.config/aish/lessons.md` | auto (echoed) |
| `read_skill` | load a playbook you wrote (see Skills) | auto |

Independent lookups batched in one model turn (several searches, a few page
reads) run **in parallel**. Fetched web pages are wrapped in an
"untrusted content — data, not instructions" banner to blunt prompt injection.
`read_url` refuses non-public targets — loopback, LAN (RFC1918), link-local /
cloud-metadata addresses — on the initial URL and again on every redirect
(SSRF guard). To read a local service, ask for `curl`, which goes through
the normal approval prompt. Answers cite what was read: after a task that
fetched pages, the CLI prints a dim `Sources:` list and the web UI shows a
collapsed **Sources (n)** row that expands to clickable page titles.

## The safety model

1. **Approval gate** — every proposed command is shown verbatim and waits for
   `y` / `n` / `a`lways / `s`ession / `e`dit. `a` saves command prefixes to the
   persistent allowlist; `s` allows the same prefixes **for this session
   only** — kept in memory, forgotten on exit, never written to disk.
   The suggested prefix is the command's **static subcommand path** — the
   binary's basename plus its subcommand words, stopping at the first flag or
   dynamic argument, with known multi-level CLIs (`gh`, `docker`, `npm`,
   `aws`, `kubectl`, …) kept to their natural depth. So approving
   `gh issue create --title "…"` offers `gh issue create`, never a blanket
   `gh` that would also wave through `gh repo delete`; you can still type a
   different prefix at the prompt.
   Auto-approval exists only for a
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
sessions (start date, message count, model used, first message): type to
filter by title, contents, and model name
(deterministic ranking: exact title, then phrase, all-words, fuzzy — never an
LLM), ↑/↓ to select, Enter replays the session into the current conversation;
`/resume <n>` picks directly, `/resume <text>` pre-fills the filter · `/new`
or `/clear`
(plain `clear` works too) · `/model [name]` — switch model mid-session; no
arg opens the same type-to-filter picker over local models and cloud
providers; add `--save` to persist the choice as the startup default in
`config.toml` (`/model --save` alone persists the current model) ·
`/jobs` · `/help` · `/quit` (or `exit`).

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
config; `$AISH_MODEL` overrides the model. `/model <name> --save` writes the
`model` key for you (comments and other keys are left untouched). Paths
override via `$AISH_CONFIG`,
`$AISH_STATE_DIR`, `$AISH_ALLOWLIST`, `$AISH_DENYLIST`, `$AISH_LESSONS`.
`--think` enables model thinking (slower, rarely worth it). You can also just
ask aish about any of this — its own docs are in its system prompt.

## Web UI

`aish-web` serves the same agent to a browser — built for phones: approvals
become tap-able cards (Approve / Allow this session / Edit / Deny), file
writes show the colored diff before anything lands on disk, answers stream
live and render as markdown (tables, code blocks, links), command output
keeps its ANSI colors, and locking your phone mid-task loses nothing (on
reconnect the server replays the transcript, including any approval still
waiting). Every finished answer gets a speaker button that reads it aloud
with the device's native text-to-speech — no cloud audio API involved. While
reading it expands into a small player: pause/resume, skip to the previous
or next paragraph, and a speed control (0.8×–2×, remembered on the device).
Code blocks are skipped, and the voice follows the answer's detected
language — English or Polish. "Allow this session" auto-approves the command's prefixes until
the session closes — in memory only, never written to the allowlist.

**Parallel sessions**: several sessions can be open at once, each with its
own agent, model, working directory, and running task. Start a task, hit the
compose button, work on something else — the first task keeps running and
the sessions drawer shows live badges (running / needs approval); a toast
tells you when a background task finishes. Up to 6 sessions stay open in
memory (idle ones beyond that are closed; their files persist and reopen
on demand).

```sh
aish-web                      # http://127.0.0.1:8787, config-default model
aish-web --host 0.0.0.0       # expose to your LAN (see security note below)
aish-web --model gemini       # same --model forms as aish
```

Header controls replace the slash commands: the **model chip** opens a
searchable model picker (with a "make startup default" toggle), the **session
title** opens the sessions drawer (search + resume + new chat), the **cwd
subtitle** under the title shows the working directory at all times — tap it
to browse or fuzzy-search folders and change directory without typing — and
the **⋯ menu** shows the working directory, session roots (`/cd` /
`/add-dir` equivalents), and background jobs; the **compose** button starts a fresh
chat; the **wrap** button toggles line-wrapping for command output, code
blocks, and diffs (default: scroll sideways; the choice is remembered per
device). The input box autocompletes like the terminal: `/` pops up the command
list (unambiguous prefixes work — `/res` runs `/resume`) and `@` pops up
project-file completion (same walk and ranking as the TUI). The paperclip
uploads files (to `~/.local/state/aish/uploads/`, a session root).
**Images go to the model natively** when the backend supports vision
(Gemini, OpenAI, Claude, and Ollama vision models like llava/qwen-vl) — the
model actually sees them: it can describe a photo, read text off a
screenshot, or use what it sees to search the web. PDFs are native on
OpenAI and Claude. Anything else (or on a non-vision backend) arrives as a
path the agent handles with its normal gated tools. Only files in the
uploads directory are ever sent natively. On iPhone/iPad, "Add to Home
Screen" installs it as a full-screen app.

**Notifications**: with permission granted (asked on your first task), the
app notifies you when an approval is waiting, an answer is ready, a task
fails, or a background session finishes — but only while the page is open
and out of focus (background tab, another app in front). It cannot reach a
locked phone: that would need Web Push infrastructure, and on iOS
notifications work only from the installed home-screen app.

Sessions are the same JSONL files as the terminal, so `aish --resume` can pick
up a web session and vice versa.

**Security**: whoever reaches this UI can drive an agent that executes
approved commands on the host, so it binds to `127.0.0.1` unless you opt into
`--host 0.0.0.0`, and setting `AISH_WEB_TOKEN=<secret>` requires
`?token=<secret>` on first visit (stored by the browser) — recommended even on
a home LAN. One task runs per session (open more sessions for parallel work);
a second device connecting takes over the view. Deliberately **not**
available from the web: `!` direct
commands, and growing the "always allow" list — approving a card runs the
command once, and the allowlist can only be extended from terminal aish.
Switching to/from `claude-max` needs a restart (`aish-web --model claude-max`),
and there is no mid-task cancel yet — deny its next approval, or restart the
server.

### Run it on an always-on machine

To reach aish-web from your phone anytime, run it as a service on a Mac
that's always on (a home server / Mac mini) — two scripts automate the
whole thing over ssh:

```sh
# one-time: install the launchd service (RunAtLoad + KeepAlive,
# logs to ~/Library/Logs/aish-web.log on the remote)
AISH_WEB_TOKEN=$(openssl rand -hex 16) GEMINI_API_KEY=... \
    scripts/install-web-service.sh myserver gemini:gemini-3.5-flash 192.168.1.20

# every deploy after that: sync working tree, reinstall, restart
scripts/deploy-web.sh myserver
```

The remote needs `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`);
the deploy script rsyncs your working tree, so the remote needs no GitHub
credentials. The third install argument binds a single interface (serve
only one of the machine's networks); omit it for `0.0.0.0`. Pass whichever
provider key your default model needs — secrets go only into the remote
plist (chmod 600), never into the repo. Then open
`http://<host>:8787/?token=<your-token>` and Add to Home Screen.

## Development

```sh
uv run pytest       # unit tests use a fake Ollama client — no model needed
uv run ruff check .
```
