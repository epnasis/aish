# The terminal client — `aish`

`cli.py` (REPL, argv, slash commands, rendering), `prompt.py` (the input UI), `aliases.py`, `dir_ignore.py`, `notify.py`.

**How to use this file.** The CLI is the other half of the same Agent — the web server is not a superset of it, and several rules exist precisely because a terminal session cannot be recovered, replayed, or watched by a second device. Sections below name the test class that pins each.

---

## The laws

**L1 · The CLI and the web must gate identically.** `cli.make_approver` and `server.make_web_approvers` implement the same policy in two renderings. The CLI's approvers return bool/str/None; the web's may also return `Denied(comment)`/`Approved(comment)`. Anything that changes what is auto-approved must change both, or the same command behaves differently depending on which surface you happen to be on. `TestDenylistApprover`.

This law reaches plugin tools too, and #377 is the case that proved it. `make_tool_approver` auto-approves exactly one mutation — a `gmail_send` whose every recipient is the owner (`recipients.owner_scoped_send`, the same import the web uses) — because that send cannot reach past him and so is not a decision he has. Fixing it in the web alone would have moved the asymmetry rather than closed it: an identical mail needing no approval in the browser and a `[y/N]` in the terminal. `tests/test_recipients.py` drives both approvers over one table and fails if they ever disagree. The CLI has no unattended origin, so the origin-scoped half of the policy (`TRIGGERED_SAFE_TOOLS`) has nothing to say here. Rationale in `docs/agent-core.md`.

**L2 · A terminal session dies with its terminal.** No restart recovery, no task markers, no resurrection — the web writes `task_start`/`task_end`, the CLI deliberately does not. There is nobody to watch a resumed run.

**L3 · The gate must see the REAL command.** Aliases are expanded before approval, never after.

**L4 · Launching from `$HOME` must not make the home tree a root.** `default_workspace` re-anchors to `~/aish`; every other cwd is respected as given. `TestDefaultWorkspace`.

---

## The REPL

`read_task` reads through `prompt.py`'s boxed input UI — built as a small prompt_toolkit `Application` rather than a `PromptSession`, because the footer-under-input layout requires it. `@`-mention file completion comes from `list_files` (the same walk, cap and scoring as the web's), filtered by `dir_ignore`. `TestAtFileCompleter`, `TestSlashCompleter`.

**Slash commands** are declared in `SLASH_COMMANDS` and dispatched by `handle_slash`. Adding one needs every place in that chain — and, if it should exist on the web too, the frontend's own handler; a missing case there falls through to "unknown command", which is how a shipped command can exist everywhere except the browser. `TestSlashCommands`.

`/learn` and `/feedback` are parsed into flow prompts by `parse_learn` and `parse_feedback` (`TestParseLearn`, `TestParseFeedback`). `parse_feedback` takes `block_flow`/`attachments` flags that the CLI always passes as neither — the block flow is web-only, because the classic flow's `gh issue create` has to upload assets. See `docs/web-server.md`.

**Rendering.** `stream_line` and `echo` handle live output, `colorize_diff` the write cards, `print_sources` the citations a `read_url` turn accumulated, `print_answer_images` the inline images (through `term_image`, see `docs/media-and-images.md`), and `replay_history` reprints a resumed session — through `strip_attachment_notes`/`attachment_names`, since a session started on the web carries per-file notes addressed to the model, and reprinting those verbatim showed them as if the user had typed them (`docs/session-log.md`). Quick-reply chips are parsed by `parse_reply_chips` and offered as a numbered menu rather than tappable buttons. `TestLiveTimer`.

---

## The gate says why (#252)

`print_intent` puts the model's stated reason above every gate prompt — command, tool, write, read, import — dimmed and attributed, before the y/N line. The terminal and the web card gate identically, so they show the same reason — and it is printed whole for the same reason the card renders it whole: the one-line snippet the trace keeps cuts the sentence that motivated the feature at an abbreviation and loses all of it. The rationale, the incident, and why nothing tells the model where this lands are in `docs/agent-core.md`.

## Nothing printed may repaint the terminal (#327)

A terminal reads control sequences as instructions, not as text: a string can move the cursor, erase the lines above it, set a colour that outlives it, or (via the Unicode bidi controls) reorder what is rendered. The web renders through `textContent` and has no such property; the terminal does, and it is where the owner reads an approval card. So a page, a skill repo or the model could make a card APPEAR to say something other than what was recorded — the log honest, the screen not. `_plain` used to guard exactly one site (`plan.note`).

**Two classes of output, sanitised differently — and the record is never touched, only the screen.**

- **Card bodies and model-authored prose → `_plain` (card mode): every control goes.** C0 except tab and newline, C1 (0x9b is an 8-bit CSI), every ESC-led sequence with its string terminator (CSI, OSC/DCS/APC/PM/SOS — an OSC 8 hyperlink's target goes with it), the Unicode Bidi_Control set (U+061C, U+200E/F, U+202A–E, U+2066–9), and the two line separators U+2028/9 — `str.splitlines()` breaks on those, so unmarked they turn one added diff line into an add AND a delete. Each becomes a visible U+FFFD marker rather than silently vanishing, so a card that had something stripped says so. CRLF collapses to LF first (a diff of a CRLF file is not an attack); a lone CR is one — `rm -rf /\rls` paints as `ls`. An unterminated OSC is deliberately NOT consumed to end of string: a real terminal would swallow the rest of the card, and hiding the card is the attack. `TestCardSanitiser`.
- **A command's live output → `_plain_live`: SGR survives, cursor and erase do not.** `ls`, `git`, `rg --color` send `ESC [ … m` on purpose and this is where the user expects it. SGR means NUMERIC parameters only (`;`-separated, `:` for `38:2::r:g:b`): the private-parameter forms that also end in `m` are terminal-state changes, not colour — `ESC[>4;2m` re-encodes what the owner types at the next `[y/N]`, `ESC[?4m` makes the terminal reply INTO stdin — and are marked. So is every other CSI final byte, the string sequences, the other ESC forms and the lone C0/C1 controls (except tab, newline and CR — a line arrives already split on LF, so CR can only overwrite within its own line). Bidi controls are left alone here: `cat` of real RTL text needs them, and live output is not a decision surface. `TestLiveSanitiser`, `TestLiveOutputSite`.

**Scope.** This is about what a terminal EXECUTES (control sequences) and what the bidi algorithm REORDERS. Visually deceptive Unicode that a terminal merely draws — homoglyphs, full-width `ｒｍ`, zero-width joiners and spaces (U+200B/C/D), U+FEFF, soft hyphens, invisible operators (U+2062–4), U+180E, variation selectors — is NOT addressed here; a card can still be made to LOOK like a different command through those, and only a reader who knows that can catch it.

**Sites covered, so the next one added knows the rule** (`TestCardSites` drives each through the real renderer): the run-command card body one line above `[y/N]`, its blocked/auto-approved/edited siblings, the outside-roots paths, the `a`/`c` prefix suggestions INSIDE their `input()` prompts and the `saved:`/`chat-allowed:` confirmations (all carved out of the model's string — and Enter there writes the raw one to `allow.txt` permanently), the `trust_dir` notes; `colorize_diff`, per line BEFORE its own SGR wrapping so the colouring survives; the file-edit card's target, rule and note; every field of the skill-import card and of `aish skill import`'s flag list (attacker-authored by construction); `print_intent` above every gate; the plugin tool card's name, args and raw preview stdout; the read card's path; `echo` — the chokepoint for `_note` labels and tool results, which in the CLI never carries live command output (that is `stream_line`); the streamed final answer (`print_answer_piece`, per piece — a sequence split across two tokens leaves a lone marked ESC and inert text) and the non-streamed one; `print_sources`, `print_chip_menu`, the echoed chip selection and `replay_history`. The `aish explain` dossier goes through `_plain_live`, because it quotes a page's own console output AND colours its own headings. Not covered, and still printing raw: `curate.py`'s judge text to stderr and `server.py`'s client text to the operator's terminal — neither sits beside a gate.

## The terminal says CHAT; the machine says session (#260)

**"Session" is the machine's word** — a log file, an entry in `WebServer.sessions`, a process lifetime. **"Chat" is the person's word** for the thing they are looking at. The web was renamed on that line first (`docs/web-frontend.md`) and the terminal was deliberately left out, on the argument that here the session genuinely IS the artefact: you handle log paths, `--resume`, files in `~/.local/state/aish/`.

That argument covers the machine-facing surfaces, and they are exactly the ones still excluded. What it does not survive is that **it is the same person and the same object**: a chat started in the browser is resumed in the terminal, so deleting a *chat* in one place and reading `no such session` in the other hands the owner two names for one thing — the complaint that started the web rename in the first place. So the line is drawn by AUDIENCE, not by surface. Identifiers, comments, docstrings, the wire, `session-*.jsonl`, `~/.local/state/aish/` and the recorded verdict tokens go on saying `session`; every string a person reads says chat, including `SYSTEM_PROMPT_TEMPLATE` and `usage_context`, because aish answers questions about itself out of those.

Three things a find-and-replace would have got wrong, which is why `tests/test_chat_vocabulary.py` extends to `cli.py` and the CLI prompt rather than a `sed` running once:

- **`approved+session` is PERSISTED.** The approver records it; `aish explain` and the web trace card read it back out of logs written years apart. The wording above it changed (`chat-allowed:`, `allow prefix for THIS CHAT`); the stored token did not, and `test_the_wire_still_says_session` pins that.
- **Killing Ollama is not the end of a chat.** The identity prompt used to say stopping the server "terminates the current aish session". It does not: aish stays at the prompt, and the chat's log survives and resumes — what dies is the model process. Renaming it to *chat* would have made a wrong sentence wrong in the owner's vocabulary, so it says what actually stops (`YOU STOP MID-ANSWER`, `cuts this chat off until Ollama is back`). Same for `/jobs`, which is process-wide and survives `/new`: it now says *since aish launched*.
- **A signed-in browser session is a cookie jar.** The three in the CLI system prompt are allowlisted by exact line, with what they mean instead. An allowlist without reasons rots into a list of things someone gave up on.

**The old spellings still work, and are still completed.** `/session` dispatches beside `/chat` (in the browser too — the same chats, so the same command), `aish usage` takes `--session` beside `--chat`, and the approval-scope key answers to `s` as well as the `c(hat)` it now offers. A rename that breaks the command the owner's fingers already know is a worse bug than the inconsistency it fixes. `--resume` keeps its name — it does not contain the word — and only its help text moved.

---

## Models

`available_models` merges local Ollama models with the cloud catalog; `cloud_model_catalog` caches provider API results for `CATALOG_TTL` and will only wait `CATALOG_FETCH_WAIT` for them, so the picker never hangs on a slow network. `rank_models` orders them, `switch_model` swaps the backend live, `model_spec` parses the `--model` string, and `save_default_model` persists the choice. `TestModels`, `TestModelPicker`, `TestModelAndJobs`, `TestModelSave`.

A new web chat inherits the model the client is currently using; the saved default applies only at process start.

---

## Resume

`aish --resume` at launch adopts the resumed session's recorded model, and restores its workspace authoritatively — its own recorded cwd and trusted dirs, nothing inherited from the session you were in before (see `restore_workspace` in `docs/agent-core.md`; the CLI reuses ONE live Agent across `/resume`, which is exactly why that call had to become authoritative). `pick_session` is the interactive picker. `TestLaunchResume`.

---

## Aliases (L3)

aish runs every command through a non-interactive `/bin/sh -c`, which never sources the user's `~/.zshrc`, so their shell aliases do not exist. More importantly the approval gate classifies a command by **parsing** it, and the denylist blocks unrecoverable commands — so the gate must see the real command, not an opaque alias.

`aliases.py` therefore keeps an aish-owned name→expansion map and rewrites the command's **first whitespace-delimited word** before approval, denylist and execution ever see it. Deliberately NOT a shell: first word only, no recursive expansion, no argument substitution. The gate then classifies `ls -l` rather than `ll`, and the user and the transcript see what actually ran. `TestExpand`, `TestGateSeesExpanded`, `TestUserCommandPath`.

`import_from_zsh` scrapes `alias` output (`parse_alias_output`), `sanitize` drops anything unsafe or malformed, and `merge_into_config_text` writes them into `config.toml` while preserving what is already there. `TestParseZshOutput`, `TestSanitize`, `TestMergeConfig`, `TestConfigLoading`.

---

## Config and context

`load_config` reads `config.toml`; malformed config degrades to defaults rather than to nothing. `identity_context` and `usage_context` build the system-prompt sections that describe aish to itself — **when user-visible behaviour changes, both the README and these strings need updating**, since aish answers questions about itself from them. `load_context_files` pulls in the project's own context files. `TestConfig`, `TestUsageContext`.

---

## `dir_ignore.py`

The configurable gitignore-style ignore list shared by the web folder browser and `@`-file completion (#87), user-editable via `config.toml`'s `[directory_picker] ignore`, with defaults written back so they are visible — mirroring `aliases.py`. Malformed config degrades to defaults, never to an empty picker.

Matching is deliberately **name-level `fnmatch` on basenames** (a trailing `/` means directories only): a pure in-memory filter that must never add a per-subfolder `stat`, which is what caused the #86 freeze. `TestMatches`, `TestSanitize`, `TestLoadPatterns`, `TestSeedConfig`.

---

## `notify.py`

Pushover sending, with credentials from the Keychain. Unconfigured or failing is a **silent no-op that never raises into the approval path** — a notification failure must not take down the thing it was announcing. `AISH_NOTIFY=0` silences pushes without touching stored credentials, which is also the suite-wide guard: `notify.configured()` reads the LIVE Keychain, so without it a test that runs a triggered session to completion sends a REAL push to the developer's phone. The autouse fixture in `tests/conftest.py` sets it for the whole suite, and `test_suite_never_reaches_the_real_notifier` pins that. `TestKillSwitch`.

The two triggers and their gating are in `docs/web-server.md` — both are web-only, because they exist for sessions nobody is watching.

---

## Deleting a chat, and the way back (#177)

`/delete` no longer unlinks: it calls the same `session.trash_session` the web calls, so the terminal's delete and the browser's delete are one act rather than two that happen to share a word (L1's argument, applied to a destruction instead of a gate). The chat can be restored for 30 days.

The terminal has no Recently deleted list to notice a chat sitting in, so two things carry it instead. The `/delete` confirmation **names the command that undoes it** — `aish trash restore <name>` — because a way back nobody is told about is not one. And `aish trash <list|restore NAME|delete NAME>` is the surface itself, a CLI-only entry point like `aish secret`; it takes either the chat's own name or the trash entry's, since the name someone has in front of them is whichever one they were last shown.

`_purge_trash_at_launch` runs the age purge on a daemon thread at startup — the mirror of the server's background `_purge_trash`. Without it a terminal-only user's trash would never expire and the 30 days would be a promise nothing keeps; on a thread because the prompt must not wait on a directory, and silent about failure for the same reason the evidence sweep is. `TestTrash` (`tests/test_session.py`) holds the mechanism; the CLI's half is in `tests/test_cli.py`.

## CLI-only entry points

- `aish secret <set|get|list|rm>` — the Keychain store (`docs/tools-layer.md`).
- `aish skill <import|approve|list|discard>` — staged import with review in your own editor (`docs/knowledge-layer.md`).
- `aish trash <list|restore NAME|delete NAME>` — Recently deleted for the terminal (above).

All three are intercepted before the main argument parser.
