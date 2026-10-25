---
name: aish-cli-help
description: Use when the user asks how terminal aish works — approval keys, slash commands, resuming or deleting a chat, the trash, model switching, local model settings, config and aliases, rate limits and token usage, multiline input or REPL shortcuts.
keywords: aish, terminal, cli, approval, key, slash, command, resume, delete, trash, model, config, alias, rate, limit, usage, token, multiline, shortcut, repl
---
How terminal aish works, for answering the user's questions about it. Answer from this page; where it is silent, say so rather than guessing.

## Approval prompt keys
- `y` run once, `n` deny, `e` edit the command first.
- `a` always allow: saves the command's prefix to `~/.config/aish/allow.txt`. The suggested prefix is the static subcommand path (`gh issue create`, never a blanket `gh`); chained `|`/`&&`/`||` segments are vetted and allowlisted independently; read-only commands auto-approve.
- `c` allow for THIS CHAT only (`s` still works): the same prefix flow, kept in memory and forgotten on exit.
- Auto-approval is confined to the chat's roots (the launch directory plus any the user added): a command whose path arguments point outside them, and read_file outside them, prompt even when otherwise read-only or allowlisted. Only the user widens this, with `/cd` or `/add-dir`.
- Safety denylist: unrecoverable command classes (rm -rf, shred, mkfs, dd to raw devices, diskutil erase, git clean -f, git push --force) are blocked outright — they cannot be approved. The user can extend the list with segment prefixes in `~/.config/aish/deny.txt` and run a blocked command themselves with the `!` prefix.

## REPL escapes and keys
- `!<command>` runs directly without aish (no approval). `!cd <dir>` is an alias for `/cd`: it moves the project directory and re-anchors the chat's root.
- Ctrl-C cancels only the running command. Ctrl-B detaches a running command into a background job. Ctrl-D or `exit` quits.
- Multiline input: Enter submits; a newline is Ctrl+J, a trailing backslash then Enter, or Option/Alt+Enter (in iTerm2 only with "Left Option key: Esc+"). Pasted text keeps its newlines.
- `@<path>` references a project file; the prompt autocompletes project files after `@`.

## Slash commands
Tab completes them, and an unambiguous prefix works (`/res` for `/resume`).
- `/resume` opens a live picker over ALL earlier chats with start date, message count and the model each last used (summary = the chat's first message). Typing filters by title, full contents and model name, deterministically (exact title, then phrase, then all-words, then fuzzy — no LLM); arrows select, Enter switches, Esc cancels. `/resume <text>` pre-fills the filter; `/resume N` switches to the N-th newest. Resuming SWITCHES the REPL to that chat — its conversation, log file, last model and working directory become current; the chat left behind is untouched. It never merges two conversations. `aish --resume` opens the same picker at launch (piped input resumes the most recent chat).
- `/delete` opens the same picker to delete an earlier chat (y/N confirm; the current chat cannot be deleted).
- `/new` or `/clear` (or plain `clear`) starts a fresh conversation and clears the screen.
- `/model <name>` switches the model for this chat; `/model` alone opens a type-to-filter picker over installed Ollama models and the cloud providers (typing `provider:model` offers that exact cloud model). Adding `--save` persists the choice as the startup default; `/model --save` alone persists the current model.
- `/cd <dir>` moves the project directory AND re-anchors the chat's root (user only; aish's own commands always run in the project directory). `/add-dir <dir>` (alias `/dir-add`) adds another directory tree to the chat's roots.
- `/jobs` lists background jobs since aish launched; `/chat` shows this chat's log file (`/session` still works); `/objective` and `/plan` show or change what the chat is for and aish's plan; `/aliases` lists aliases and `/aliases import` pulls in the user's zsh aliases; `/learn` distils the conversation into saved skills/memory (after a task that went the long way round — very many tool calls, repeated calls, refused steps, several card comments, or a loop stop — the quick-reply menu offers "Save this as a lesson?", whose number sends `/learn`; never twice in a row); `/help` lists commands; `/quit` or `/exit` quits.

## Documents
- A SCANNED PDF (pages that are pictures, with no text in them) is read with the Mac's own text recognition (OCR) — nothing to install. aish marks those words as machine-recognised (they may contain errors, a value in a table can be listed apart from its label, a ticked box can come out as a stray letter, and signatures are not captured) and says which pages it could not read and why: off a Mac, `AISH_OCR=0`, or more than 12 scanned pages in one go (asking again reads the next ones).
- A PICTURE file — a photo of a letter, a screenshot, a JPEG or HEIC saved from a mail — is read the same way: aish reads its words with the same OCR (`read_file` on it), marked the same way, and says if it found none or could not read it.

- Asked for a PDF, report or printable summary, aish makes one with `make_report` and names the file's path in the answer; open it from there. It lives in the chat's scratch workspace and is deleted with the chat.

## Deleting, and the trash
- A delete is NOT final: the chat moves to the trash and can be restored for **30 days**. `aish trash` lists it, `aish trash restore <name>` brings one back, `aish trash delete <name>` destroys one for good.
- Deleted at once, and not brought back by a restore: the chat's throwaway working files and the stored copies of the requests its steps sent.

## Chats
- Every chat (conversation + command audit trail) is logged under `~/.local/state/aish/` in the same format as the web UI, so a chat moves freely between the terminal and the browser.

## Doing something later
- "Check after 10pm" and the like become a one-shot automation: aish proposes it with schedule_task and the user approves a write prompt whose summary states when it runs, the whole instruction, and what it may do without asking.
- It is RUN by aish-web (the always-on server), not by this terminal: at that time aish-web opens a new automated chat for it. If aish-web is not running, nothing fires until it starts; a run that cannot start within 24 hours is given up and reported once.
- Recurring works too ("every weekday at 9"): the automation gets a cron schedule like `0 9 * * 1-5` (local time) and the prompt summary shows its next five runs as dates; after downtime only the latest missed run fires.
- A recurring automation can end: `"until": "<ISO local time>"` beside the cron (cron only). No occurrence after it runs; the prompt summary says the last run and the end; a last run that could not start on time may still start up to 24 h late. Its ending pushes once, whatever `notify` says. Once ended, `/tasks` lists it as ended and `/tasks enable` is refused (`/tasks disable` too, once the 24 h are over); `/tasks delete` still works.
- A recurring automation can start later: `"from": "<ISO local time>"` beside the cron (cron only), with or without `until`. No occurrence before it runs (one exactly at it is the first); the prompt summary says the first run. A start already past is allowed and changes nothing. Until it, `/tasks` says `starts <first run>` instead of `next`.
- "Tell me when X changes" becomes a monitor (`"kind": "monitor"`, cron only): each run compares against the previous run's snapshot and ends with a verdict; a change pushes its one-line summary, no change is silent (`"notify"`: `on_change` by default, or `always` / `never`), and a run aish cannot read keeps the old baseline and pushes once, then at most daily. Runs happen in aish-web.
- Saved as a plain JSON file in `~/.config/aish/automations/`; editing its `at` or its cron expression re-arms it, deleting the file cancels it.
- Asked to change an automation, aish reads it with its `automations` tool (the user's own chat only) and proposes the change on a write prompt headed "Changes:" — only the named fields change, the rest (paused or not included) is kept. It can pause, resume or delete one the same way, each on its own prompt. If the file changed while the prompt was open, approving writes nothing and aish says so.
- `/tasks` lists the automations: when each runs and runs next, enabled or not, its notify policy, and whether a one-shot is pending, fired or expired; a file aish cannot load is listed as a PROBLEM with its error.
- `/tasks enable <id>` / `/tasks disable <id>` turn one on or off — the only field they change; what an automation does changes only on a new approval card. A file that does not load is refused (fix it in an editor, or delete it). `/tasks delete <id>` removes the file after a y/N (default No); there is no way back short of a new automation on its own card.
- `/runs` lists recent scheduled runs newest first — what each did (a monitor's verdict, "ran for 09:00") or why it did not happen (expired, did not start, skipped) — and marks chats not opened yet. It names chats; scheduled chats are never resumed.
- At startup aish says how many scheduled runs from the last 14 days have not been opened (silent monitor runs not counted).
- Running an automation now, outside its schedule, is the web's `/run-now`. The web also has `/tasks`, as a sheet with Pause / Resume / Run now / Delete per automation.
- `/mail` (or `/mail <n>`, up to 200) lists the inbox assistant's shadow results newest first — sender, subject, which inbox, the Reader's reading, what it WOULD have done, and the mail checks (authenticated or why not, newsletter-gated, flags) — under the watcher's health line, which warns when no pass ran lately or an inbox's listing failed. Shadow mode: nothing was done to any of those mails. The results come from `aish-mailwatch`, scheduled by `scripts/install-mailwatch-service.sh`. `/m` is ambiguous now; `/mo` is `/model`.
- `/mail export` writes every recorded result as a CSV file (one row per mail and model: date, sender, subject, the reading, what aish would do, whether the live-conversation rule overrode it, the mail checks) to sort and filter in a spreadsheet, into the Downloads folder (or `/mail export <path>` for another file or folder), and prints its path. When Downloads does not exist, cannot be written, or aish was not started from a terminal, the file goes to aish's own state folder instead and the printed line says so. A mail in a conversation he wrote in within the last 30 days is never filed away (archived or expired) — shown as CLAMPED.

## Models
- The current model is named on aish's identity line. `--model`, `$AISH_MODEL` or the config's `model` key set the startup default; `/model <name> --save` persists a switch.
- `local:<model>` runs on the user's own OpenAI-compatible server (e.g. mlx-lm) at `AISH_LOCAL_URL`, with:
  - `AISH_LOCAL_CTX` — tokens one whole request may use on that server, answer included (default 98304). aish keeps the prompt under it minus the answer cap, counting in tokens, and shortens old tool results and then old messages when it would not fit; if the current task alone does not fit, it is sent anyway and the chat says so.
  - `AISH_LOCAL_MAX_TOKENS` — the answer cap (default 16384).
  - `AISH_LOCAL_SAMPLING` — a JSON object of sampling fields sent on every request, replacing the default temperature 1.0, top_p 0.95, top_k 20, presence_penalty 1.5 (the server is greedy without them).
  - A `local:` request whose connection is lost after sending is re-sent once, then once with a shortened conversation, then the turn ends saying what was seen.
- Streamed reasoning (not the answer, and not on claude-max) that has become one passage repeated many times is stopped early.

## Rate limits and token usage
- A cloud API has a per-minute ceiling, and the whole conversation is resent on every step, so a long task can spend millions of input tokens a minute. aish paces itself against that ceiling across every chat, learns it from the first 429, and once it knows it, caps how much history it carries so several steps fit in a minute. Waiting on a rate limit is aish pacing deliberately, not a hang. A quota that is SPENT rather than busy cannot be waited out.
- The user sets their own tier with `AISH_RATE_LIMIT_<PROVIDER>="rpm=..,tpm=.."`.
- `aish usage` shows where the tokens went (`--chat <name>` for which tool filled a chat's context, `--window N` to count the calls that would not fit a window that size). `aish explain <chat> <turn>` ends with one turn's cost: each model call's reported tokens, what each step added, and what the fullest call's context was made of. Long pages and images fill it fastest; the standing prompt and the tool menu are paid on every call.
- How full the context was on the last model call is shown at the right end of the line under the prompt (`ctx 34% · 68.0k/200.0k`); a leading `~` means estimated from characters, because Ollama does not report the prompt prefix it reused.

## Config
- Config file: `~/.config/aish/config.toml`. Keys: `vi_mode`, `model`, `num_ctx`, `max_steps`, and an `[aliases]` table. `vi_mode = true` turns on vi editing at the prompt.
- `[aliases]` maps a command name to an expansion (`ll = "ls -l"`); aish rewrites a command's FIRST word from it BEFORE the approval gate, so the gate still sees the real command.
- Config is read at startup only — changes take effect on the next start. CLI flags override config; `$AISH_MODEL` overrides the model key.
- To change a setting, aish edits the config file with a normal shell command, which goes through approval like any command.
- Durable context: an `AISH.md` in the working directory or `~/.config/aish/AISH.md` is loaded into aish's system prompt — the place for host facts and preferences.
- Memory entries live in `~/.config/aish/memory/` (one small markdown file each) and skills in `~/.config/aish/skills/`; legacy one-line lessons in `~/.config/aish/lessons.md` still appear in the Memory section.
