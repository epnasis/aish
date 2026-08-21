# The terminal client — `aish`

`cli.py` (REPL, argv, slash commands, rendering), `prompt.py` (the input UI), `aliases.py`, `dir_ignore.py`, `notify.py`.

**How to use this file.** The CLI is the other half of the same Agent — the web server is not a superset of it, and several rules exist precisely because a terminal session cannot be recovered, replayed, or watched by a second device. Sections below name the test class that pins each.

---

## The laws

**L1 · The CLI and the web must gate identically.** `cli.make_approver` and `server.make_web_approvers` implement the same policy in two renderings. The CLI's approvers return bool/str/None; the web's may also return `Denied(comment)`/`Approved(comment)`. Anything that changes what is auto-approved must change both, or the same command behaves differently depending on which surface you happen to be on. `TestDenylistApprover`.

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

`print_intent` puts the model's stated reason above the command and tool prompts, dimmed and attributed, before the y/N line. The terminal and the web card gate identically, so they show the same reason — and it is printed whole for the same reason the card renders it whole: the one-line snippet the trace keeps cuts the sentence that motivated the feature at an abbreviation and loses all of it. The rationale, the incident, and why nothing tells the model where this lands are in `docs/agent-core.md`.

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

## CLI-only entry points

- `aish secret <set|get|list|rm>` — the Keychain store (`docs/tools-layer.md`).
- `aish skill <import|approve|list|discard>` — staged import with review in your own editor (`docs/knowledge-layer.md`).

Both are intercepted before the main argument parser.
