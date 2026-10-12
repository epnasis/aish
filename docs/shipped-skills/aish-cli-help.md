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

## Deleting, and the trash
- A delete is NOT final: the chat moves to the trash and can be restored for **30 days**. `aish trash` lists it, `aish trash restore <name>` brings one back, `aish trash delete <name>` destroys one for good.
- Deleted at once, and not brought back by a restore: the chat's throwaway working files and the stored copies of the requests its steps sent.

## Chats
- Every chat (conversation + command audit trail) is logged under `~/.local/state/aish/` in the same format as the web UI, so a chat moves freely between the terminal and the browser.

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
