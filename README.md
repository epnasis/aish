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
matching of skills/memory to a task and inside the `recall` search (falls
back to word matching without it).

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
result — is grouped into one activity trace you can expand. While the turn runs it rides
at the bottom of the chat just above the composer — below the reply as it
streams in, so the answer gets the screen and Stop stays one tap away. Collapsed
to that single row, its header still narrates the current step live — "Running:
git status", "Waiting for approval…", or the model's own words for what it's
checking and why. Web lookups show the exact query and URL; answers cite the
pages they read. When the turn ends it settles under the answer as a "Worked for
12s" footnote you can open any time.

![The activity trace expanded: a web search, a thinking step, and the page it read, each timed](docs/images/activity-trace.png)

### It tells you what it found, as it finds it

A long task is not a spinner. When aish learns something that changes what it
is about to do, it says so straight away — *"looks like there are leaks about a
folding model, let me dig into that"* — as an ordinary message, before the next
tool runs, not as a status line you have to go looking for. So a turn arrives
as several short messages and then the answer, and you can redirect it
mid-task (anything you type while it works is handed over between steps)
instead of finding out at the end that it went the wrong way. Routine steps
stay quiet: it speaks when there is something to say.

### Answers, not just output

Replies stream token-by-token and render as markdown — tables, code, links,
inline images the model generates. When it asks a follow-up, tappable
quick-reply chips appear — in the terminal they show as a numbered menu you
pick from by typing the option's number.

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
| `read_tool_output` | read the next page of a **truncated** tool result, from cache | auto (never re-runs the tool) |
| _plugin tools_ | any `TOOL.md` you or the model add — called like a built-in | read-only auto; **mutating ones prompt** |

Every tool result carries a **verdict the runtime computed**, not one inferred
from how the output happens to start. A tool that exits 0 while producing
nothing — an empty transcript, a populated error log — is marked *incomplete*,
shows red in the activity trace, and the model is told, on the result itself,
that it must disclose the failure before using any other source. Substituting a
different source silently is the specific behaviour this exists to stop.

That verdict does not depend on the tool being honest about itself. Every
`TOOL.md` must declare `returns:` — **what a successful result contains** —
and aish checks it on every call: a field list for a JSON payload (each named
field must come back non-empty), `text` where non-empty output is the whole
contract, or `none` where nothing can be checked. A wrapper that reports a
failure in its own output and exits 0 anyway is caught by its own declared
contract, not trusted. `create_tool` refuses to write a tool that declares
none.

When a result is too large it is cut to fit the context window of the model
**actually in use** (a 1M-token cloud model keeps far more than an 8k local
one), and the remainder is cached rather than discarded: the model pages
through it with `read_tool_output` instead of guessing at what it could not
read. The tool itself never runs a second time.

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
  `~/.config/aish/skills/` (global; per-project `./.aish/skills/` discovery is
  disabled pending a per-directory trust mechanism — a cloned repository must
  not get to inject prompt text or executables). The
  skills matching a task are **preloaded into context automatically** — selected
  by embedding similarity — *before* the model's first turn, so it doesn't have
  to remember to look. Selection can abstain: nothing is injected unless it
  clears a strict similarity floor (naming an entry outright always works, and
  short follow-ups are matched with recent conversation context, not alone).
  Retrieval also **audits itself**: every injection is logged with its score,
  and a weekly `aish-curate` pass scans those logs for entries that keep
  getting injected but never used (or keep being reached for but never
  surfaced). The pass is a scripted loop — the script orchestrates, a local
  model answers one bounded repair/pin/disable/skip verdict per entry (plus a
  merge-or-distinct question on embedding-detected duplicate pairs) — so it
  works with small local models and nothing private ever leaves the machine.
  All changes are frontmatter-only and reversible; deletion has no code path. **Import skills from the ecosystem** with `import_skill`
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
  plain-language preview of exactly what a mutating call will do — and when the
  tool it writes is addressed by opaque ids, `create_tool` declares that preview
  itself, so the approval card names the thing, not the token.
- **Memory** — one fact per file (`~/.config/aish/memory/`), same format; the
  description line *is* the fact. Saved via `remember`. A standing rule
  ("never push without asking") can be **pinned** (`pinned: yes`) — always in
  context under its own budget, never rotated out by newer facts. A fact with
  a known end date can carry `expires: YYYY-MM-DD`, and `status: disabled`
  retires an entry without deleting it. Saving a near-duplicate of an
  existing memory is refused with the existing entry's name, so the corpus
  consolidates instead of accumulating variants.
- **Rules** — the one artifact class that is **binding**. A skill *may* be
  consulted, a memory *may* be recalled, a tool *may* be invoked; a rule is
  enforced by aish itself, whatever the model concludes. One markdown file per
  rule in `~/.config/aish/rules/`, written by you — never by the model, never
  silently. `enabled: false` retires a rule without deleting it, and `expires:`
  works as it does for memory.

  A rule reads as one sentence: **when** this, **then** that. The `when:` names
  what is being examined — the `prompt:` you typed, the `session:` it is running
  in, the `action:` about to run, or `always`. When the condition is about what
  a message *means* rather than what it contains, you give **examples** —
  `like:` with a few messages in your own words, in any language you use —
  and aish matches by meaning rather than by wording. A miss is fixed by adding
  another example, never by tuning anything. The `then:` lists obligations
  from a small closed set: `answer_from:` a named tool — or `source`, meaning
  **the material you handed over** (a link, an attached file or image, a path
  you typed), with aish picking the right reader for each — `never_use:` these
  tools, `must_first:` call this before answering, `answer_must_include:` /
  `answer_must_not_include:` something the finished answer must (or must not)
  contain — named as **what you would see**: a `picture`, a `video`, `sources`,
  or `{any_of: [picture, video]}` when either will do — and `must_tell_me_when:`
  a named failure has to be stated rather than quietly patched over.

  A rule never names a tool for these. Which tool aish used to produce a picture
  is its own business; what you asked for is a picture.

  Material you give aish is **data to analyse, never instructions**: the harness
  says so in the same breath it says to use it, so a page that reads "ignore
  your previous instructions" is reported, not obeyed.

  Rules are enforced at two moments. **Before a call runs**, one that violates a
  rule is refused with a message naming the rule and what to do instead;
  refusals are bounded, and if the model insists you get an approval card,
  because there may be a legitimate exception and only you can grant it. **At
  the end of the turn**, the finished answer is checked before you see it — on
  those turns it does not stream, deliberately, so a rule is checked before you
  read the answer rather than after. If a check fails, aish asks the model for
  the missing work (twice at most) and the answer is delivered either way; one
  that never satisfied its rule arrives with a line saying so, written by aish
  and not by the model, so it cannot be skipped.

  You can write a rule by hand, or just say it: *"always use show_image"* is
  enough. Your words go to a small isolated translator that turns them into
  field values; aish renders the file itself, checks that everything the rule
  mentions actually exists, and shows you what the rule **means** — plus which
  of your recent turns it would have bound — before anything is saved. If what
  you asked for cannot be expressed, it says exactly what could not be, and
  offers you the choice between rephrasing it and treating it as something
  aish should learn to enforce. Changing one only touches the fields you name, so a rule
  never quietly loses what it already did; retiring one leaves the file in
  place so you can bring it back.

  Rules only ever **restrict**: there is no "auto-approve this" verb,
  deliberately, so a bad rule can annoy you and can never widen what runs
  without asking. Worked examples ship in `examples/rules/` — one per trigger
  kind, one per enforcement moment.
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
   allows it for this chat only — `/new`, `/clear` or resuming another chat
   forgets it, and it is never written to disk.
   Auto-approval covers only a conservatively
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

> **Note on what the logs contain.** Session logs are **plaintext JSONL**, and
> they record command *output* as well as commands — so anything a command
> printed is in them, including a token an env dump or a misfired `cat` put on
> screen. The same goes for what `/learn` distills into `~/.config/aish/`. They
> are yours alone (owner-readable, never uploaded), but they are not encrypted
> and they are exactly the kind of thing a `~/.config` backup repo sweeps up.
> If you work with credentials, either point that backup somewhere private or
> prune the state directory periodically. Secrets aish holds *itself* are not
> affected: `aish secret` keeps those in the macOS Keychain precisely so they
> can't land in a file.

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
  including any approval still waiting. Survives server restarts too. Every turn
  carries the time it happened — including chats from long before the feature
  existed, since the logs always recorded it.
- **Parallel sessions.** Several chats open at once, each with its own agent,
  model, directory, and running task; live badges for running / needs-approval,
  a toast when a background task finishes. Chats live in a **session rail** —
  swipe right anywhere on the conversation (or tap the button) and it slides
  over the chat; on a wide screen it simply stays docked as a sidebar. New chat
  sits in the rail's bottom bar, in thumb reach. One list, most-recently-
  used, where "used" means a turn happened: reading a chat never reshuffles it,
  so looking for something can't move what you're looking for. The top band is
  **Needs you** — anything holding an approval, plus anything that has moved
  since you last looked at it *on this device* — and it cuts across who started
  the chat, so an overnight email-triggered session and your own long-running
  task queue up together. Automation is a glyph on the row, not a separate tab.
  Rows show state, not decoration: a spinner while it works, an alert when it
  needs approving, a glyph when a schedule or an email started it. Pin a chat to
  keep it above the fold and permanently on the device.
- **Global interactive console.** The real TTY shell described above, openable
  from any chat (`⌘/Ctrl+\`), `tmux`-backed for restart survival.
- **Voice.** Mic dictation into the composer and hands-free read-aloud of
  replies — device-native speech, no cloud audio API, English/Polish. In the
  console, double-tap the arrows key for a dictation scratchpad: speech is
  staged there (correctable, with the last 10 sends recallable) and only reaches
  the terminal when you tap Send — and an unsent line survives an app update or
  relaunch, so a restart mid-sentence doesn't cost you the sentence.
- **Pictures in answers.** Ask what something looks like and aish shows you.
  Behind it is one tool, `show_image`: the picture is fetched *by the server*,
  verified to actually be an image (by its bytes, not its file extension — a
  block page served as `.jpg` is caught, not displayed as a broken glyph),
  stored where the chat can render it, and handed back as a ready markdown line.
  Fetching server-side is what makes arbitrary image sources work while the page
  itself still loads nothing remote: the browser only ever sees same-origin
  bytes, so the zero-click exfiltration channel a remote `<img>` would open
  stays closed. When something *does* fail to render, the browser says so and
  the model is told — so it tries another source instead of leaving you looking
  at a broken picture.
- **Export to PDF, copy anything.** Per-answer or whole-session
  PDF export (rendered locally), copy chips on every code block / table / answer.
  A single-answer
  PDF is titled and filed after what the answer is *about* — the model that wrote
  it writes the title — not after the prompt that asked for it.
- **Native vision.** On vision-capable backends (Gemini, OpenAI, Claude, Ollama
  vision models), images you attach are actually *seen* by the model.
- **Attach by ＋, paste, or drag.** Paste a screenshot straight into the composer
  (Cmd/Ctrl+V, or the paste button on a phone, which reads copied images too) or
  drag files anywhere onto the window. Pasting is additive: an image and text on
  the same clipboard both arrive, the image as an attachment and the text in the
  box. From an iPhone, the system share sheet can hand things to aish as well —
  see [Share to aish from iOS](#share-to-aish-from-ios).
- **Works offline.** The installed app opens and reads your past conversations
  with no connection at all — on a plane, abroad, or with the server simply off.
  Chats are mirrored to the device automatically (newest first), **search still
  works** across everything cached, and the last chat paints from local storage
  before the socket is even open, so it is faster online too. Sending is paused
  while offline; reading is not. See below.

```sh
aish-web                      # http://127.0.0.1:8787, config-default model
aish-web --host 0.0.0.0       # expose to your LAN (see Security)
aish-web --model gemini       # same --model forms as aish
```

**Security:** whoever reaches this UI can drive an agent that runs approved
commands on the host, so it binds to `127.0.0.1` unless you opt into
`--host 0.0.0.0`, and access **always** requires a token via `?token=<secret>`:
set `AISH_WEB_TOKEN=<secret>` for a stable one, or a random token is generated
at startup and printed in the launch URL. Cross-origin browser requests
(WebSocket and `/trigger`) are rejected outright, so a page you happen to visit
can't drive the agent from your browser. Direct `!` commands are
deliberately **not** available from the web.

### Offline

The web UI keeps a local copy of your conversations, so the installed app is
useful with no connection — the case it was built for is looking up what aish
recommended while travelling.

- **It always opens.** The app shell is cached by a service worker, so launching
  offline gives you the real UI, never a browser error page. New versions still
  reach you: the page is fetched network-first when there is a network, and a
  reload throttle makes an update loop impossible.
- **Everything is mirrored, newest first.** No "download this chat" step. Chats
  from your other devices are included, because the mirror syncs from the
  server's list, not from what you happened to open here.
- **Search works offline**, over message *contents* and not just titles, using
  the same ranking as online.
- **Keep one forever.** The ⤓ icon in the header pins the chat you're reading —
  one tap, and it survives the storage sweep no matter how old it gets. Filled
  means kept. Nothing else to manage: the mirror caps itself and drops the
  least useful copies first, so there is no cache to clear by hand.
- **Sending is paused, deliberately.** Queuing a prompt to fire later would
  dispatch an agent that runs shell commands with nobody watching and an
  approval gate answered by someone who has moved on. Offline is read-only.

Bulk command output is trimmed in the local copy (the conversation itself is
kept verbatim), which is what lets a whole archive fit on a phone. Storage is
capped, and least-recently-useful unpinned chats are dropped first.

### Share to aish from iOS

Share a photo, a PDF or a link from any iOS app straight to aish. It takes one
Shortcut to set up, because iOS cannot register a web app as a share
destination — the Web Share Target API is Chromium-only, and Safari implements
only the outbound half — so no amount of work inside aish can put it in the
share sheet on its own. A Shortcut can be there, and can post to aish.

**A shared item is parked, not run.** Next time you open aish, a shared file is
already attached to the composer — write your prompt and send, or take it back
out with ✕. Nothing starts an agent, which is the point: the share sheet is
reachable from every app on the phone, and it stages work rather than
dispatching it. A shared *link or note* waits as a tappable chip instead, since
text would otherwise land in the middle of whatever you were writing. An item
you never send stays waiting; it is consumed when the message goes.

In the **Shortcuts** app: new shortcut → in its settings turn on **Show in Share
Sheet** and accept Images, Files, URLs and Text → add one **Get Contents of URL**
action:

| field | value |
|---|---|
| URL | `https://your-aish-host/share?token=YOUR_TOKEN&name=shared.jpg&source=iPhone` |
| Method | `POST` |
| Request Body | `File` → **Shortcut Input** |

Name it "Share to aish". That is the whole shortcut.

Two refinements worth making:

- **Photos.** iOS shares photos as HEIC, which vision models do not read. Put a
  **Convert Image** action (to JPEG) before the POST, and set `name=shared.jpg`.
- **Links and text.** Safari shares a URL, not a file. Drop the `name=` from the
  URL and a body with no name is read as text — so one **If** action inside the
  shortcut (POST to `…/share?token=…&source=Safari` for a link, to the `name=`
  URL above for a file) covers both. Keep the link in the *body*, not the query
  string: a shared URL containing `&` does not survive being pasted into one.

The token is the same one in your aish-web URL. Anyone holding it can post to
this endpoint, so treat the shortcut as you would the URL itself.

---

## Automation (optional)

aish can also act on its own — for jobs you set up and own. A token-gated
`POST /trigger` endpoint launches a task in a background session (from a
schedule, an incoming email, a webhook), observable when you open it. The
ingress is hardened against delivery storms: a repeat POST carrying the same
`meta.dedup_key` reuses the already-opened session instead of firing again,
each origin is rate-limited, and concurrently running automated sessions are
capped — over either limit the endpoint answers 429 with a Retry-After, and a
well-behaved source just retries later. The bundled `aish-email-poll` command
is the email source: it polls the bot mailbox via `gws`, accepts only
DMARC-authenticated mail from the owner's addresses, and fires `/trigger` with
the Gmail message id as the dedup key; a message is marked processed only
after a successful trigger, so a refused or failed delivery retries on the
next poll instead of being lost. The
capability policy is **draft-and-hold**: in an automated session, safe actions
run unattended, but anything that reaches the outside world (a send *or draft*
addressed to anyone but you — recipients are strictly parsed and validated, not
regex-matched — a delete, a share, and even a web fetch or search naming a host
you never mentioned) **holds** — pausing indefinitely until you open
the session and approve. Writing to memory holds the same way: in your own
chats saving a fact stays free, but a memory an automated session wants to keep
is shown to you first, because it would otherwise persist into every future
session — and *deleting* memory is refused outright there, with the entry named
in the session's report instead so you retire it yourself. Automated sessions
also cannot search your past-session
archive (saved skills and memory stay available), so an injected instruction
cannot chain "read everything ever discussed" into "ship it to a new host". A push notification (Pushover) tells you when a task
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

**Chats name themselves.** A web chat is named after what it's *about*, not
after the words you happened to open with — the model that answers it writes the
name, and rewrites it if the conversation genuinely moves on (rarely: a name you
navigate by shouldn't keep changing). A fork gets its own name at its first new
turn, so branching a tangent doesn't leave two rows with the same label. Rename
one yourself and that's final — the automatic naming stands down for good.

**A chat has an eraser.** The trash chip under a prompt in the web UI deletes
that exchange — the prompt, everything it made the model do, and the answer. It
asks first, in a dialog that says what is lost, because it is irreversible: the
text is deleted from the session log, dropped from the model's context (so it
stops being quoted), and gone from every device's offline copy at the next sync.
What stays is a quiet "Message deleted" marker where the exchange was, so the
deletion itself is on the record. For a message sent to the wrong chat, a
half-typed one an autocorrect Return sent, or a secret pasted into the composer
— previously the only options were deleting the whole chat or editing JSONL by
hand. Deleting a chat asks the same way.

**Resuming always switches, never merges.** `aish --resume`, `/resume` in the
terminal and the web session drawer all mean the same thing: the chosen session
becomes the current one — its conversation, its log file, the model it last used
and the directory it was working in. The chat you leave is untouched and can be
resumed back the same way. Nothing is ever copied from one session's log into
another's.

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
