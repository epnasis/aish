# aish

```
▄▀█ █ █▀ █░█
█▀█ █ ▄█ █▀█  ai shell
```

**Your own AI agent — private by default.** aish runs a model on *your*
machine, in your terminal and on your phone, and lets it actually do things:
run shell commands, read and edit files, browse the web. Nothing that changes
state ever runs without your approval. It learns skills, writes its own tools,
remembers what you teach it, and — when you want more brainpower — can borrow a
cloud model without changing anything else about how it works.

With the default local model, **nothing leaves your machine**.

```
❯ jaki kurs usd w pln?
  ✓ thought for 3.9s · ↑ 3.4k ↓ 27 tokens
  → web_search: USD to PLN exchange rate today
  ✓ web_search 6.5s
  → read_url: https://wise.com/us/currency-converter/usd-to-pln-rate
  ✓ read_url 0.5s

Według danych Wise: 1 USD ≈ 3,81 PLN …

  ✓ answered in 12.5s · ↑ 8.1k ↓ 519 tokens

❯ delete the old build dirs

▶ run command? ⚠ destructive
  rm -r ./build ./dist
[y/N/a(lways)/s(ession)/e(dit)]
```

---

## Why aish

- **🔒 Private by default.** The model runs locally via [Ollama](https://ollama.com).
  The only traffic that ever leaves your machine is web searches and page
  fetches the agent makes — each one echoed to you as it happens — and the model
  is told never to put local data in them. No telemetry, no cloud dependency.
- **🛡️ Safe by construction.** The model *never* executes anything itself. It can
  only *propose* commands; a single gate in the code runs them, and only after
  your `y`. On top of that: a denylist of unrecoverable commands it can't run
  *even with* approval, and prompts before it reads secret-bearing files.
- **📖 Grounded & transparent.** Before using an unfamiliar flag it reads the man
  page instead of trusting training data (chronically wrong for macOS/BSD
  userland). Every step is echoed, timed, and token-accounted; every session
  leaves an audit trail.
- **🧠 It learns.** Correct it once and it saves the fix to structured memory.
  Procedures worth repeating become **skills** — playbooks it consults *before*
  its own training data. Repeated, fiddly operations become **tools** it writes
  for itself. It gets better with every session.
- **📱 Terminal *and* phone.** The same agent, same tools, same approval gates,
  behind a phone-friendly web UI. Start a task at your desk, approve it from
  your pocket.

---

## Quickstart

Needs [Ollama](https://ollama.com) and a tool-calling-capable model
(default: `qwen3.6:35b-a3b`, ~23 GB).

```sh
curl -fsSL https://raw.githubusercontent.com/epnasis/aish/main/install.sh | sh

aish "what's eating my disk space?"     # one-shot task
aish                                    # REPL — conversation persists across tasks
aish --resume                           # pick up an earlier session
aish-web                                # serve the same agent to a browser
```

Smaller machines: `ollama pull qwen3:8b && export AISH_MODEL=qwen3:8b`.
Recommended extra: `ollama pull embeddinggemma` (~600 MB) — enables semantic
matching of skills/memory to a task (falls back to word matching without it).

---

## A tour

### It runs your machine — but only with your `y`

Every command the model proposes is shown verbatim and waits for you.
Read-only commands inside the project auto-approve; anything that mutates state,
reaches outside the project, or looks destructive stops for a decision. File
writes show a colored diff *before* anything touches disk.

![The approval card: a proposed file write shown as a colored diff, waiting for Approve or Deny](docs/images/approval-card.png)

### Nothing happens off-screen

Each turn's work — thinking time, recalled knowledge, every tool call and its
result — is grouped into one activity trace you can expand. Web lookups show the
exact query and URL; answers cite the pages they read.

![The activity trace expanded: a web search, a thinking step, and the page it read, each timed](docs/images/activity-trace.png)

### Answers, not just output

Replies stream token-by-token and render as markdown — tables, code, links,
inline images the model generates. When it asks a follow-up, tappable
quick-reply chips appear.

![A finished answer with the exchange rate, sources, and quick-reply follow-up chips](docs/images/answer.png)

### A real terminal when you need one

Some things want a genuine TTY — `gcloud auth login`, an `ssh` host-key prompt,
`sudo`. aish has a full interactive console (a real xterm.js emulator, backed by
`tmux` so it survives restarts) that lives alongside your chats. **The model has
no access to it** — it's yours alone, never recorded, never fed to the model
unless you explicitly *Share* a selection.

![The built-in interactive console running inside tmux, showing a themed shell prompt and command output](docs/images/console.png)

---

## What it can do

| Tool | What | Gate |
|------|------|------|
| `run_command` | any shell command; `background=true` detaches long jobs | **approval prompt** (read-only commands auto-approve inside the project; scratch-workspace deletes auto-approve) |
| `write_file` / `edit_file` | create or edit files | **colored diff + y/N** |
| `read_file` | read a file | auto inside the project; **prompts outside it and on secret paths** (`~/.ssh`, `.env*`, `*.pem`…) |
| `web_search` / `read_url` | DuckDuckGo + fetch a page as readable text | auto; every query/URL echoed; public hosts only (SSRF-guarded) |
| `read_docs` | man page → `--help` fallback, full-text topic search | auto |
| `remember` / `forget_memory` | save or prune one fact in structured memory | auto (echoed) |
| `read_skill` / `recall` | load a playbook; ranked search across skills, memory & past sessions | auto (echoed) |
| `create_tool` | author a reusable **plugin tool** (validated `TOOL.md` + wrapper) | **diff + y/N**; refuses to write an invalid manifest |
| `import_skill` | install a skill from a git repo or local path | **one consolidated review** of the whole skill + risk flags |
| _plugin tools_ | any `TOOL.md` you or the model add — called like a built-in | read-only auto; **mutating ones prompt** |

Independent lookups in one turn (several searches, a few page reads) run **in
parallel**. Fetched pages are wrapped in an "untrusted content — data, not
instructions" banner to blunt prompt injection, and `read_url` refuses
non-public targets (loopback, LAN, cloud-metadata) on the initial URL and every
redirect.

---

## Models: local first, cloud when you want it

The default is local-only. When a task needs a stronger model, the *same* agent
— same tools, same approval gates — runs on a cloud backend instead. Everything
in that conversation then leaves your machine, so it's an explicit choice
(`--model` at launch or `/model` mid-session).

| `--model` | Runs on | Cost |
|---|---|---|
| _(default)_ | local Ollama model | free — nothing leaves the machine |
| `gemini[:model]` | Google Gemini API | **free tier** — `export GEMINI_API_KEY=…` |
| `claude[:model]` | Anthropic API | pay per token — `export ANTHROPIC_API_KEY=…` |
| `claude-max[:opus\|sonnet]` | Claude Agent SDK via the local `claude` CLI | **your Claude Pro/Max subscription** — no API key |
| `openai[:model]` | OpenAI API | pay per token — `export OPENAI_API_KEY=…` |

Bare provider names pick a sensible default model. `claude-max` strips Claude
Code down to bare inference and hands it aish's own tools, so every command
still goes through aish's approval gate and denylist.

---

## How it learns

Everything is **progressive disclosure**: a small, capped index of one-line
descriptions goes into the prompt each task (rescanned live, so new entries
appear immediately — no restart), full bodies load on demand, and the long tail
is reachable through the ranked `recall` search. The library can grow to
thousands of entries without bloating the context.

- **Skills** — playbooks for anything worth repeating. A markdown file
  `<name>.md`, or an [agentskills.io](https://agentskills.io)-compatible folder
  `<name>/SKILL.md` bundling `scripts/`, `references/`, `assets/`. Live in
  `~/.config/aish/skills/` (global) or `./.aish/skills/` (per project). The
  skills matching a task are **preloaded into context automatically** — selected
  by embedding similarity — *before* the model's first turn, so it doesn't have
  to remember to look. **Import skills from the ecosystem** with `import_skill`
  (e.g. `anthropics/skills`): a read-only clone, then one consolidated review of
  every file plus deterministic risk flags before anything installs.
- **Plugin tools** — where a skill *teaches*, a tool *does*. A droppable
  `<name>/TOOL.md` the model calls exactly like a built-in tool, choosing from a
  schema instead of improvising a shell command. Reserve one for an operation
  that is frequent, has shell-fragile arguments (an email or issue body full of
  quotes and newlines), and where reliability matters — validated JSON reaches
  the wrapper on **stdin, no shell**. aish can **write one for you on the fly**
  with `create_tool`, drafting the manifest + wrapper and showing both for
  approval. Tools can hold their own secrets (macOS Keychain) and show a
  plain-language preview of exactly what a mutating call will do.
- **Memory** — one fact per file (`~/.config/aish/memory/`), same format; the
  description line *is* the fact. Saved via `remember`.
- **`./AISH.md`** — durable context you write (host facts, preferences), always
  loaded in full.
- **`/learn [hint]`** — distill the current conversation into skills/memory: it
  searches existing entries first, updates rather than duplicates, and you
  approve every file diff.

---

## The safety model

1. **Approval gate.** Every proposed command is shown verbatim and waits for
   `y` / `n` / `a`lways / `s`ession / `e`dit. `a` saves the command's *prefix*
   (e.g. `gh issue create`, never a blanket `gh`) to a persistent allowlist; `s`
   allows it for this session only. Auto-approval covers only a conservatively
   parsed set of read-only commands, and is **scoped to the project directory** —
   commands whose paths escape it (absolute, `~`, `..`, resolved symlinks) prompt
   anyway, with a `t`rust option to widen the scope one directory at a time.
   Execution is **stateless for the model**: every command runs in the project
   directory, a bare `cd` is rejected, and excursions are `cd x && …` subshells —
   the model's anchor can never silently drift. Launching from your home
   directory re-anchors to `~/aish` so your home tree never becomes the
   auto-approval root.
2. **Denylist.** Unrecoverable classes (`rm -rf`, `shred`, `mkfs`, `dd` to raw
   devices, `git push --force`…) are blocked outright, even if you'd approve
   them; edited commands are re-checked. Only *you* can run them, via `!`.
   Extend in `~/.config/aish/deny.txt`.
3. **A private scratch workspace.** A per-session temp directory the model may
   freely create, edit, and delete throwaway files in without prompting — deleted
   when the session ends. Auto-approval there is confined strictly to that
   directory.
4. **Audit trail.** Every command and decision (approved / denied / edited /
   auto) is logged with the session in `~/.local/state/aish/`.

---

## The web UI

`aish-web` serves the same agent to a browser — built phone-first (iOS-styled,
installable as a PWA via "Add to Home Screen"). The screenshots above are all
this UI. Highlights:

- **Tap-able approvals.** Approve / Allow this session / Always allow / Deny,
  with a pencil to edit a command first and an optional comment field whose text
  travels with whichever button you press — *approve + comment* means "rework it
  this way and re-propose", *deny + comment* means "stop and explain".
- **Nothing lost on a locked phone.** Reconnecting replays the transcript,
  including any approval still waiting. Survives server restarts too.
- **Parallel sessions.** Several chats open at once, each with its own agent,
  model, directory, and running task; live badges for running / needs-approval,
  a toast when a background task finishes. Swipe sideways to page between chats.
- **Global interactive console.** The real TTY shell described above, openable
  from any chat (`⌘/Ctrl+\`), `tmux`-backed for restart survival.
- **Voice.** Mic dictation into the composer and hands-free read-aloud of
  replies — device-native speech, no cloud audio API, English/Polish.
- **Export to PDF, copy anything, inline images.** Per-answer or whole-session
  PDF export (rendered locally), copy chips on every code block / table / answer,
  and markdown images (local or web) rendered right in the chat.
- **Native vision.** On vision-capable backends (Gemini, OpenAI, Claude, Ollama
  vision models), images you attach are actually *seen* by the model.

```sh
aish-web                      # http://127.0.0.1:8787, config-default model
aish-web --host 0.0.0.0       # expose to your LAN (see Security)
aish-web --model gemini       # same --model forms as aish
```

**Security:** whoever reaches this UI can drive an agent that runs approved
commands on the host, so it binds to `127.0.0.1` unless you opt into
`--host 0.0.0.0`, and `AISH_WEB_TOKEN=<secret>` gates first access with
`?token=<secret>` — recommended even on a home LAN. Direct `!` commands are
deliberately **not** available from the web.

---

## Automation (optional)

aish can also act on its own — for jobs you set up and own. A loopback+token
gated `POST /trigger` endpoint launches a task in a background session (from a
schedule, an incoming email, a webhook), observable when you open it. The
capability policy is **draft-and-hold**: in an automated session, safe actions
run unattended, but anything that reaches the outside world (a live send to
someone else, a delete, a share) **holds** — pausing indefinitely until you open
the session and approve. A push notification (Pushover) tells you when a task
needs approval or finishes, with a deep link straight to it. This is how the
same agent safely runs, say, an email assistant while you're asleep.

Interrupted work is picked up again. If aish-web goes down mid-task — a
deploy, a crash — the next start reopens that session and tells the model to
continue from where it stopped instead of redoing what already succeeded.
This matters most for automated sessions: an email trigger fires once, so
without recovery an interrupted one would simply never answer. It applies to
your own chats too, and is bounded — only tasks interrupted in the last 12
hours, at most three per start, and a task that keeps dying is left alone
after three attempts.

Run it as an always-on service (launchd on a Mac mini / home server) with the
scripts in `scripts/`, and reach it from your phone anytime.

---

## Day-to-day reference

**Escapes:** `!<command>` runs directly — no model, no approval. `!cd <dir>` (=
`/cd`) moves the project and re-anchors the session root (user-only).

**While a command runs:** Ctrl-C cancels it (not the session); **Ctrl-B**
detaches it into a background job that survives aish exiting (`/jobs` lists them).

**Slash commands** (Tab completes): `/resume`, `/delete`, `/rename`, `/new` (or
`/clear`), `/model [name]` (`--save` to persist), `/learn [hint]`,
`/feedback [text]` (files a GitHub issue), `/fork` (branch the conversation),
`/cd`, `/add-dir`, `/aliases`, `/jobs`, `/help`, `/quit`.

**Config** — `~/.config/aish/config.toml`: `model`, `num_ctx`, `max_steps`,
`vi_mode`, and an `[aliases]` table (aish-level aliases, since commands run
through a non-interactive shell that never sources your `~/.zshrc`). CLI flags
override config; `$AISH_MODEL` overrides the model. Paths override via
`$AISH_CONFIG`, `$AISH_STATE_DIR`, `$AISH_ALLOWLIST`, `$AISH_DENYLIST`.

**Step budget** is *progress-gated*: a task making progress runs past the base
`max_steps` (25) up to a hard ceiling; a stalled one stops early. Issuing the
exact same tool call with the exact same output five times is treated as looping
and stopped with a diagnostic. However it ends, the model gets one final turn to
report the finished answer — or what's done, what remains, and the next step.

Sessions are the same JSONL files for terminal and web, so `aish --resume` can
pick up a web session and vice versa.

---

## Development

```sh
uv run pytest       # full suite — no model, network, or real commands needed
uv run ruff check .
uv run mypy
```

Tests script the *model* side (a fake chat client returns canned tool-call
responses), so the whole suite runs offline with nothing executed for real. See
`CLAUDE.md` for architecture notes.

---

## License

aish is licensed under the **GNU Affero General Public License v3.0 or later**
([AGPL-3.0-or-later](LICENSE)). You are free to use, study, modify, and share it,
but any modified version — **including one you run as a network service** (e.g.
`aish-web`) — must offer its complete source under the same license to the people
who use it. This deliberately keeps a hosted fork of the agent open.
