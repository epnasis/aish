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
- **It learns.** Correct it once and it saves the fix to structured memory;
  procedures worth repeating become skills — playbooks it checks BEFORE its
  own training data, so it gets smarter with every session. `/learn` distills
  a whole conversation on demand.

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

Recommended: `ollama pull embeddinggemma` (~600 MB) — enables semantic
matching when preloading relevant skills/memories for a task; without it
aish falls back to word matching.

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
| `run_command` | any shell command; `background=true` detaches long jobs | **approval prompt** (read-only commands auto-approve inside the session roots; deletes confined to the scratch workspace auto-approve) |
| `write_file` / `edit_file` | create or edit files | **colored diff + y/N** (auto inside the per-session scratch workspace) |
| `read_file` | read a file | auto inside the session roots; **prompts outside them and on secret paths** (`~/.ssh`, `.env*`, `*.pem`…) |
| `web_search` / `read_url` | DuckDuckGo + fetch page as readable text | auto; every query/URL echoed; public hosts only |
| `read_docs` | man page → `--help` fallback, full-text `topic` search | auto |
| `remember` | save one fact/lesson as a structured memory entry in `~/.config/aish/memory/` (create-or-update by name, deduped) | auto (echoed) |
| `read_skill` | load a playbook (see Memory & skills) | auto |
| `recall` | ranked search over everything it knows — skills, memory, and past sessions (episodic fallback) — snippets first, full entry by name; deterministic ranking, hard output caps | auto (echoed); current session excluded |

Independent lookups batched in one model turn (several searches, a few page
reads) run **in parallel**. Fetched web pages are wrapped in an
"untrusted content — data, not instructions" banner to blunt prompt injection.
`read_url` refuses non-public targets — loopback, LAN (RFC1918), link-local /
cloud-metadata addresses — on the initial URL and again on every redirect
(SSRF guard). To read a local service, ask for `curl`, which goes through
the normal approval prompt. When a site blocks the plain fetcher (HTTP
403/429/503) or serves a JavaScript-only shell, the error suggests the model
retry once via [Jina Reader](https://jina.ai/reader) (`https://r.jina.ai/<url>`),
which renders the page server-side and returns markdown. The retry is a
separate, echoed `read_url` call — the URL visibly goes to a third party,
never as a hidden fallback — and the hint warns against using it for URLs
carrying secrets. Answers cite what was read: after a task that
fetched pages, the CLI prints a dim `Sources:` list and the web UI shows a
collapsed **Sources (n)** row that expands to clickable page titles.

## The safety model

1. **Approval gate** — every proposed command is shown verbatim and waits for
   `y` / `n` / `a`lways / `s`ession / `e`dit. `a` saves command prefixes to the
   persistent allowlist; `s` allows the same prefixes **for this session
   only** — kept in memory, forgotten on exit, never written to disk.
   A command (or file read) reaching **outside the session roots** says so at
   the prompt and adds a `t`rust option: it approves the command *and* adds
   the escaping directory to the session roots, so allowlisted work there
   auto-approves for the rest of the session — one prompt at the boundary
   instead of one per command. In-memory only, like `s`.
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
   `/dir-add`) allows another tree as well; both are user-only. **Execution
   is stateless for the model**: every command runs in the project directory,
   a bare model-issued `cd` is rejected with guidance instead of executing,
   and temporary excursions are `cd <dir> && <command>` subshells that revert
   when the command ends — path-scoped by the same root rules (where `t` can
   trust the destination). The model's anchor to the project can therefore
   never silently drift.
   Launching aish (or `aish-web`) **from your home directory** re-anchors the
   session to `~/aish` (created on first use) instead — otherwise `~/.ssh`,
   `~/.aws`, shell history, and the rest of your home tree would sit inside
   the auto-approval scope. Launch from any other directory (or `/cd`
   afterwards) and that choice is respected as-is.
   Each session also gets a **private scratch workspace** — a temp directory
   (`aish-scratch-…`) where the model may create, edit, *and* delete throwaway
   files (staging a `gh` issue body, a commit message, an intermediate patch)
   without prompting. The path is in the model's system prompt; the auto-approval
   is confined strictly to that directory (paths escaping it via `..` or a
   symlink still prompt, and `rm -rf` stays denylisted even there), and the
   whole directory is deleted when the session ends.
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
is an alias for `/cd`: it moves the project directory and re-anchors the
session root (user-only — the model's commands always run in the project
directory).

**Slash commands** (Tab completes): `/resume` — live picker over all earlier
sessions (start date, message count, model used, first message): type to
filter by title, contents, and model name
(deterministic ranking: exact title, then phrase, all-words, fuzzy — never an
LLM), ↑/↓ to select, Enter replays the session into the current conversation;
`/resume <n>` picks directly, `/resume <text>` pre-fills the filter ·
`/delete [n|text]` — delete an earlier session permanently (same picker and
argument forms as `/resume`, then a y/N confirm; removes the conversation
AND its command audit log; the current session is excluded — `/new` first
to delete it) · `/new`
or `/clear`
(plain `clear` works too) · `/model [name]` — switch model mid-session; no
arg opens the same type-to-filter picker over local models and cloud
providers; add `--save` to persist the choice as the startup default in
`config.toml` (`/model --save` alone persists the current model) ·
`/learn [hint]` — save this conversation's learnings as skills/memory
(`/learn lessons` migrates the legacy lessons file) ·
`/jobs` · `/help` · `/quit` (or `exit`).

**Multiline input**: Enter submits; newline via Ctrl+J, trailing `\`, or
Option/Alt+Enter (iTerm2: set "Left Option key" to "Esc+"). Pastes keep
their newlines.

**Memory & skills** — how aish learns. Everything is progressive
disclosure: a small capped index of one-line descriptions goes into the
prompt each task (rescanned live, so new entries appear immediately — no
restart), full bodies load on demand, and the long tail is reachable through
the ranked `recall` search — so the library can grow to thousands of entries
without bloating the context.
- **skills** — playbooks for anything worth repeating: markdown files in
  `~/.config/aish/skills/` (global) or `./.aish/skills/` (project, wins on
  name clash) with `name:`/`description:`/`keywords:` frontmatter. The
  description states the trigger ("Use when the user asks to …") — that is
  what makes it discoverable. Skills and memories matching the task are
  **preloaded into context automatically** — selected by embedding
  similarity (local `embeddinggemma` via Ollama, multilingual, vectors
  cached in the state dir; `ollama pull embeddinggemma` once to enable,
  `AISH_EMBED_MODEL` overrides), with exact name/keyword hits always
  included and lexical word-matching as the fallback when no embedding
  model is reachable —
  before the model's first turn — no reliance on the model remembering to
  look; a skill too large to inject whole is truncated and other tools are
  refused until the model reads it in full (or explicitly says why it does
  not apply). aish also updates a skill (appends the gotcha) whenever one
  proves wrong, and when saved knowledge fails to trigger on a task it
  should have matched, telling aish so makes it repair that entry's
  description/keywords so retrieval finds it next time. Ask aish to write
  a skill — it knows the format.
- **memory** — one fact per file in `~/.config/aish/memory/` (or
  `./.aish/memory/`), same format; the description line IS the fact. The 15
  newest show in context, the rest are searchable. Saved via `remember`.
- `./AISH.md` or `~/.config/aish/AISH.md` — durable context you write (host
  facts, preferences), always loaded in full.
- **`/learn [hint]`** — distill the current conversation into skills/memory:
  aish searches existing entries first, updates rather than duplicates, and
  you approve every file diff. A legacy `~/.config/aish/lessons.md` keeps
  working (its lines surface as memory) — `/learn lessons` migrates it
  consciously and retires it.

**Step limit & loop detection** — a task gets `max_steps` model turns
(default 25). At the limit the model gets one final no-tools turn to judge
the state and answer: the finished answer if the task is actually done,
otherwise what was accomplished, what remains, and the next step — say
"continue" to resume where it stopped. Independently, issuing the *exact
same* tool call and getting the *exact same* output is treated as running
in circles: three repeats inject a change-your-approach nudge, five stop
the task with a diagnostic summary instead of burning the rest of the
budget (polling a growing log never trips this — its output changes).

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
become tap-able cards (Approve / Allow this session / Always allow / Deny,
a pencil icon beside the command to edit it before running — plus an
optional comment field whose text travels with *whichever* button you press:
on Deny it tells the model why and what to do instead; on
any approval it reaches the model as guidance for this and future actions.
Either way, a comment forces the model to *answer it first* — before its next
action — rather than silently absorbing the feedback into the next command),
file writes show the colored diff before anything lands on disk, answers stream
live and render as markdown (tables, code blocks, links), command output
keeps its ANSI colors, and locking your phone mid-task loses nothing (on
reconnect the server replays the transcript, including any approval still
waiting). Server restarts are survivable too: the client reconnects into
the session it was on — not a fresh chat — and half-typed composer text is
kept on the device across reconnects and app reloads. Every finished answer gets a speaker button that reads it aloud
with the device's native text-to-speech — no cloud audio API involved. While
reading it expands into a small player: pause/resume, skip to the previous
or next paragraph, and a speed control (0.8×–2×, remembered on the device).
Code blocks are skipped, and the voice follows the answer's detected
language — English or Polish. "Allow this session" auto-approves the command's prefixes until
the session closes — in memory only; "Always allow" saves those same prefixes
to the persistent allowlist (the card shows the exact rule being saved, e.g.
`gh issue create` — never the full command line with its arguments). Cards for
commands or reads that reach outside the session roots call out the escaping
directory and add a "Trust directory" button — one tap adds it to the session
roots, so allowlisted work there auto-approves afterwards (also in memory only).

**Copy buttons**: every code block, table, and command-output block carries a
small copy chip in its corner, and each finished answer has a copy button next
to the speaker. Code and output copy as plain text; tables and whole answers
copy as their markdown source, so they paste as tables/formatting anywhere
markdown is understood. Works over plain-HTTP LAN connections too, where the
browser clipboard API is unavailable.

**Export to PDF**: beside each answer's copy button is an export chip that
saves that one answer as a PDF, and the session-title menu has an "Export to
PDF" item that saves the whole chat — final answers only, without the thinking
or intermediate working steps. Conversion is done entirely on the server
(markdown rendered locally, never sent to any online service) and the file
downloads straight to your device.

**Quick replies**: when the model asks a question with a few short likely
answers, it can end the message with `[Label](aish-reply://answer text)`
links — the web UI renders them as tap chips. Tapping one puts the answer in
the composer, ready to send as-is or edit first, so it goes out as a normal
user message. It's plain markdown (one system prompt sentence, no JSON
schema), so even small local models can use it; once any reply is sent —
chip-fed or typed — the chips disappear. As a safety net, if a final answer
ends in a question but the model forgot to add chips, a generic
Yes / No / Tell-me-more set is appended automatically; the model suppresses
this on a genuinely open-ended question by ending with a `[no-chips]` tag,
which is hidden from you.

**Inline images**: markdown image syntax in an answer renders right in the
chat. `![caption](https://…)` embeds a web image; `![caption](/absolute/path.png)`
displays a local image file (png/jpg/gif/webp) — the model saves a chart
with matplotlib, references its path, and the picture appears in the
transcript, lazy-loaded, tap for full size. Local files are served by the
token-gated `/file` endpoint, which refuses anything outside the session
roots (symlinks are resolved before that check). In the terminal, the same
markdown displays the image inline on iTerm2, kitty, WezTerm, and ghostty;
other terminals simply keep the path visible as text.

**Parallel sessions**: several sessions can be open at once, each with its
own agent, model, working directory, and running task. Start a task, hit the
compose button, work on something else — the first task keeps running and
the sessions drawer shows live badges (running / needs approval) and
highlights the chat you're looking at; a toast
tells you when a background task finishes. Up to 6 sessions stay open in
memory (idle ones beyond that are closed; their files persist and reopen
on demand). **Swipe the transcript sideways** — a finger on the phone, a
two-finger trackpad swipe on a Mac — to page through
your recent chats — the same list, in the same last-interaction order, as
the sessions drawer, so swiping back is exactly moving down that list (chats
load from disk as needed, opened or not; the most recent 30 are reachable
this way, search covers the rest). Opening an older chat just to review it
does not reorder the list — it stays in place, with its neighbors unchanged;
only sending a message makes it the most recent again. Directions follow Safari: swipe right
goes back to an older chat, swipe left forward to a newer one — and swiping
forward past the newest opens a fresh chat. The view follows your finger, a
pill shows which chat you're heading to, and it turns blue once letting go
will switch — release earlier and it snaps back (on a trackpad the switch
happens the moment the pill turns blue). Code blocks still scroll
sideways normally; the gesture only engages on a clearly horizontal drag.
On a keyboard, **Ctrl+H / Ctrl+L** page the same way (vim h/l: older /
newer), and Cmd/Ctrl+Shift+O starts a new chat, Cmd/Ctrl+Shift+P opens
session search.

```sh
aish-web                      # http://127.0.0.1:8787, config-default model
aish-web --host 0.0.0.0       # expose to your LAN (see security note below)
aish-web --model gemini       # same --model forms as aish
```

Header controls replace the slash commands (except `/learn`, which works by
typing it as a message). The nav bar has two rows. On top: a **‹ Sessions**
back button (top left, the standard chat-app spot — it carries an orange
badge when a background session needs your attention) opens the sessions
drawer — recent chats grouped by day (running/waiting ones under "Active
now"), each row a status icon, title, last-message preview and time, plus
search; each row's trash icon deletes that session after an inline "Delete?"
confirm (permanent: conversation and audit log; refused while running;
deleting the current chat lands you on a fresh one). The **centered session
title** (with its ˅ caret) opens a menu: new chat, switch model, change
directory, line wrap, and workspace & jobs. The **compose pencil** (top
right) starts a fresh chat. The second row is a context bar: the **working
directory** chip (folder name + path) taps into a folder picker to browse or
fuzzy-search folders without typing (recents first; an absolute or `~` path
in its search jumps straight there), and the **model chip** opens a
searchable model picker (with a "make startup default" toggle). Your tool
activity — thinking time, recalled knowledge, each command and its output —
is grouped into one collapsible **activity trace** per turn, live while it
runs and summarised ("Worked for Xs · N steps") once done. Each executed
command renders as a **terminal block**: a black panel with a pinned prompt
line (`dir$ command`), the live ANSI output (capped with a "Show all output"
expander when long), and a pinned exit code (or a "detached"/"interrupted"
label). Line wrap (in the
title menu) toggles wrapping for command output, code, and diffs (default:
scroll sideways; remembered per device). The input box autocompletes like the
terminal: `/` pops up the command list (unambiguous prefixes work — `/res`
runs `/resume`) and `@` pops up project-file completion (same walk and
ranking as the TUI). The composer's **＋** button opens attach file, reference
a path (@), slash command (/), and photo; attachments upload to
`~/.local/state/aish/uploads/` (a session root). Messages sent while a task
runs queue as chips above the composer (tap ✕ to cancel one).
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
available from the web: `!` direct commands.
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

### Preview a branch beside production

To try a feature branch's web UI without disturbing the running service, serve
the working tree on a second port and reverse-proxy it under a path on the same
origin as production. `make preview` runs the current checkout on `:8788` from
source (production stays on `:8787`), reading the token/provider key from your
environment or, as a convenience on the server, from the prod launchd plist:

```sh
make preview          # this tree on :8788, sharing prod's sessions
```

Add one stable block to the production `server { server_name aish.<domain>; … }`
so the preview is reachable at `https://aish.<domain>/preview/` — same origin,
so the browser token is shared and no separate login is needed. Configured once,
reused for every branch:

```nginx
# http { } scope — needed for the WebSocket upgrade:
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

# inside the existing aish server { } block:
location /preview/ {
    proxy_pass http://192.168.10.20:8788/;   # trailing slash strips /preview/
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
}
```

The web client derives its API/WebSocket base from the page path, so it works
mounted under `/preview/` unchanged. **Shared state:** preview uses production's
`AISH_STATE_DIR`, so both UIs show the same sessions and knowledge — but the
append-only logs have no cross-process lock, so don't drive the *same* session
from `/` and `/preview/` at once (use different or throwaway sessions on
preview).

## Development

```sh
uv run pytest       # unit tests use a fake Ollama client — no model needed
uv run ruff check .
```
