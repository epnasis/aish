"""Interactive CLI: one-shot task from argv, or a REPL keeping conversation state."""

import argparse
import os
import sys
import tomllib
from pathlib import Path

from .agent import Agent, environment_context
from .approval import (
    DEFAULT_ALLOWLIST,
    is_auto_approvable,
    load_prefixes,
    save_prefix,
    suggest_prefix,
    unvetted_segments,
)
from .session import SessionLog

BOLD = "\033[1m"
DIM = "\033[2m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RESET = "\033[0m"

ECHO_PREVIEW_LINES = 12
REPLAY_TOOL_LINES = 4

SLASH_COMMANDS = ("/clear", "/exit", "/help", "/new", "/quit", "/resume")

SLASH_HELP = f"""{DIM}commands (Tab autocompletes):
  /resume        load the previous session into this one (replays it on screen)
  /new, /clear   fresh conversation in a new session file
  /help          this help
  /quit, /exit   quit (plain 'exit' works too)
input: Enter submits · Option/Alt+Enter (or Esc,Enter) adds a newline · pasted
newlines are kept · !<cmd> runs directly without the model · !cd <dir> moves
the working directory · Ctrl-C cancels a running command{RESET}"""

# BoxPrompt instance when stdin is a TTY; None means plain input() fallback.
_box = None


class LogRef:
    """Mutable indirection so /new can swap the session log everywhere at once."""

    def __init__(self, log: SessionLog):
        self.log = log

    def message(self, message: dict) -> None:
        self.log.message(message)

    def command(self, command: str, decision: str) -> None:
        self.log.command(command, decision)


def load_config(path: Path) -> dict:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        print(f"{YELLOW}warning: ignoring invalid config {path}: {exc}{RESET}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def edit_line(initial: str) -> str:
    """Line editing with the command pre-filled."""
    if _box is not None:
        return _box.edit(initial)
    try:
        import readline

        readline.set_startup_hook(lambda: readline.insert_text(initial))
        try:
            return input(f"{YELLOW}edit>{RESET} ").strip()
        finally:
            readline.set_startup_hook(None)
    except ImportError:
        return input(f"{YELLOW}edit ({initial})>{RESET} ").strip()


def allow_segments_flow(command: str, allow_path: Path) -> None:
    """'always allow' asks about each unvetted chained segment independently."""
    prefixes = load_prefixes(allow_path)
    for segment in unvetted_segments(command, prefixes) or [command]:
        suggestion = suggest_prefix(segment)
        answer = input(
            f"{YELLOW}always allow prefix{RESET} [{BOLD}{suggestion}{RESET}] "
            f"(enter=yes, s=skip, or type a different prefix): "
        ).strip()
        if answer.lower() == "s":
            continue
        save_prefix(allow_path, answer or suggestion)
        print(f"{DIM}  saved: {answer or suggestion} → {allow_path}{RESET}")


def make_approver(ask_all: bool, allow_path: Path, log):
    def record(command: str, decision: str) -> None:
        if log:
            log.command(command, decision)

    def ask_approval(command: str) -> str | None:
        if not ask_all and is_auto_approvable(command, load_prefixes(allow_path)):
            print(f"\n{GREEN}✓ auto-approved:{RESET} {BOLD}{command}{RESET}")
            record(command, "auto")
            return command

        print(f"\n{YELLOW}{BOLD}▶ run command?{RESET}\n  {BOLD}{command}{RESET}")
        try:
            answer = input(f"{YELLOW}[y/N/a(lways)/e(dit)]{RESET} ").strip().lower()
        except EOFError:
            record(command, "denied")
            return None

        if answer in ("y", "yes"):
            record(command, "approved")
            return command
        if answer == "a":
            allow_segments_flow(command, allow_path)
            record(command, "approved+allowlisted")
            return command
        if answer == "e":
            edited = edit_line(command)
            if edited:
                record(f"{command} => {edited}", "edited")
                return edited
            record(command, "denied")
            return None
        record(command, "denied")
        return None

    return ask_approval


def echo(text: str) -> None:
    lines = text.splitlines()
    shown = lines[:ECHO_PREVIEW_LINES]
    print(DIM + "\n".join(f"  {line}" for line in shown) + RESET)
    if len(lines) > ECHO_PREVIEW_LINES:
        print(f"{DIM}  … ({len(lines) - ECHO_PREVIEW_LINES} more lines fed to model){RESET}")


def stream_line(line: str) -> None:
    print(f"{DIM}  {line}{RESET}")


def read_task(cwd: str) -> str:
    """Boxed prompt (rules hugging the input, expanding with multiline entry);
    plain prompt when stdin is piped."""
    home = str(Path.home())
    display = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    if _box is None:
        return input(f"\naish:{display}> ")
    print()
    return _box.read(display)


def replay_history(messages: list[dict]) -> None:
    """Print a loaded conversation so the user sees what they resumed."""
    for message in messages:
        role = message.get("role")
        content = (message.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            print(f"\n{BOLD}❯{RESET} {content}")
        elif role == "assistant":
            print(f"{GREEN}{content}{RESET}")
        else:
            lines = content.splitlines()
            print(DIM + "\n".join(f"  {line}" for line in lines[:REPLAY_TOOL_LINES]) + RESET)
            if len(lines) > REPLAY_TOOL_LINES:
                print(f"{DIM}  … ({len(lines) - REPLAY_TOOL_LINES} more lines){RESET}")


def handle_slash(task: str, agent: Agent, logref: LogRef, state_dir: Path) -> str:
    """Dispatch a /command; returns 'exit' or 'handled'."""
    command = task.split()[0].lower()
    if command in ("/quit", "/exit"):
        return "exit"
    if command in ("/new", "/clear"):
        agent.reset()
        logref.log = SessionLog.new(state_dir)
        print(f"{DIM}fresh conversation — session {logref.log.path.name}{RESET}")
        return "handled"
    if command == "/resume":
        candidates = [
            f for f in sorted(state_dir.glob("session-*.jsonl")) if f != logref.log.path
        ]
        for candidate in reversed(candidates):  # newest session that has content
            messages = SessionLog.load_messages(candidate)
            if not messages:
                continue
            agent.load_history(messages)
            for message in messages:  # keep the current session file self-contained
                logref.message(message)
            print(f"{DIM}resumed {len(messages)} messages from {candidate.name}:{RESET}")
            replay_history(messages)
            return "handled"
        print(f"{DIM}no previous session to resume{RESET}")
        return "handled"
    if command == "/help":
        print(SLASH_HELP)
        return "handled"
    print(f"{DIM}unknown command {command} — try /help{RESET}")
    return "handled"


def usage_context(
    model: str, vi_mode: bool, allow_path: Path, state_dir: Path, config_path: Path
) -> str:
    """Self-knowledge for the system prompt: aish should be able to explain
    and (via approved commands) reconfigure itself."""
    return f"""\
About aish (you) — use this to answer questions about your own usage:
- Approval prompt keys: y=run once, n=deny, a=always allow (saves command \
prefixes to {allow_path}; chained |/&&/|| segments are vetted and allowlisted \
independently; read-only commands auto-approve), e=edit the command first.
- REPL escapes: `!<command>` runs directly without you (no approval); \
`!cd <dir>` changes the shared working directory. Ctrl-C cancels only the \
running command. Ctrl-D or `exit` quits.
- REPL slash commands (Tab autocompletes them): /resume loads and replays the \
previous session into this one; /new or /clear starts a fresh conversation; \
/help lists commands; /quit or /exit quits.
- Multiline input: Enter submits; Option/Alt+Enter (or Esc then Enter) \
inserts a newline; pasted text keeps its newlines.
- Sessions: conversation + command audit trail logged to {state_dir}; \
`aish --resume` continues the most recent session.
- Config file: {config_path} (TOML). Keys: vi_mode, model, num_ctx, \
max_steps. vi_mode (prompt vi editing) is currently {str(vi_mode).lower()}; \
enable it with the line `vi_mode = true`. Config is read at startup only — \
changes take effect on the next aish start. CLI flags override config; \
$AISH_MODEL overrides the model key.
- Durable context: an AISH.md file in the working directory or \
~/.config/aish/AISH.md is loaded into your system prompt — the right place \
for host facts and user preferences.
- Current model: {model} (change via --model, $AISH_MODEL, or config).
When the user asks you to change one of your settings, edit the config file \
with a normal shell command (it goes through approval like any command)."""


def load_context_files(cwd: str) -> list[str]:
    parts = []
    for path in (Path.home() / ".config" / "aish" / "AISH.md", Path(cwd) / "AISH.md"):
        try:
            if path.is_file():
                parts.append(f"[context from {path}]\n{path.read_text(encoding='utf-8')}")
        except OSError:
            continue
    return parts


def main() -> int:
    config_path = Path(
        os.environ.get("AISH_CONFIG", str(Path.home() / ".config" / "aish" / "config.toml"))
    )
    config = load_config(config_path)

    parser = argparse.ArgumentParser(
        prog="aish",
        description="Local LLM agent that runs CLI commands (with your approval).",
    )
    parser.add_argument("task", nargs="*", help="task to perform; omit for interactive mode")
    parser.add_argument(
        "--model",
        default=os.environ.get("AISH_MODEL") or config.get("model") or "qwen3.6:35b-a3b",
        help="Ollama model (default: $AISH_MODEL, config, or qwen3.6:35b-a3b)",
    )
    parser.add_argument(
        "--num-ctx", type=int, default=int(config.get("num_ctx", 32768)),
        help="context window tokens",
    )
    parser.add_argument(
        "--max-steps", type=int, default=int(config.get("max_steps", 25)),
        help="max model turns per task",
    )
    parser.add_argument("--think", action="store_true", help="enable model thinking (slow)")
    parser.add_argument(
        "--vi-mode", "--vi",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("vi_mode", False)),
        help="vi editing in the prompt (config key: vi_mode)",
    )
    parser.add_argument(
        "--ask-all",
        action="store_true",
        help="prompt for every command, including read-only ones",
    )
    parser.add_argument(
        "--resume", action="store_true", help="continue the most recent session"
    )
    args = parser.parse_args()

    cwd = os.getcwd()
    state_dir = Path(
        os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish"))
    )
    allow_path = Path(os.environ.get("AISH_ALLOWLIST", str(DEFAULT_ALLOWLIST)))

    history: list[dict] = []
    if args.resume:
        latest = SessionLog.latest(state_dir)
        if latest is None:
            print(f"{DIM}no previous session found — starting fresh{RESET}")
            log = SessionLog.new(state_dir)
        else:
            history = SessionLog.load_messages(latest)
            log = SessionLog(latest)
    else:
        log = SessionLog.new(state_dir)
    logref = LogRef(log)

    global _box
    if sys.stdin.isatty():
        from .prompt import BoxPrompt

        _box = BoxPrompt(args.vi_mode, state_dir, SLASH_COMMANDS)

    context = "\n\n".join(
        [
            environment_context(cwd),
            usage_context(args.model, args.vi_mode, allow_path, state_dir, config_path),
            *load_context_files(cwd),
        ]
    )
    agent = Agent(
        model=args.model,
        approve=make_approver(args.ask_all, allow_path, logref),
        echo=echo,
        stream=stream_line,
        num_ctx=args.num_ctx,
        max_steps=args.max_steps,
        think=args.think,
        cwd=cwd,
        context=context,
        on_message=logref.message,
    )
    if history:
        agent.load_history(history)
        print(f"{DIM}resumed {len(history)} messages from {log.path.name}:{RESET}")
        replay_history(history)

    if args.task:
        print(f"{GREEN}{agent.run_task(' '.join(args.task))}{RESET}")
        return 0

    print(
        f"{DIM}aish — model {args.model} · session {log.path.name} · /help · Ctrl-D quits{RESET}"
    )
    while True:
        try:
            task = read_task(agent.cwd).strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print(f"{DIM}(input cleared — Ctrl-D or /quit to exit){RESET}")
            continue
        if task in ("exit", "quit"):
            return 0
        if not task:
            continue
        if task.startswith("/"):
            if handle_slash(task, agent, logref, state_dir) == "exit":
                return 0
            continue
        if task.startswith("!"):
            command = task[1:].strip()
            if command:
                logref.command(command, "user-direct")
                try:
                    agent.run_user_command(command)
                except KeyboardInterrupt:
                    print(f"\n{YELLOW}(command interrupted){RESET}")
            continue
        try:
            print(f"\n{GREEN}{agent.run_task(task)}{RESET}")
        except KeyboardInterrupt:
            print(f"\n{YELLOW}(task interrupted){RESET}")


if __name__ == "__main__":
    sys.exit(main())
