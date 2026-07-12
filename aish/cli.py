"""Interactive CLI: one-shot task from argv, or a REPL keeping conversation state."""

import argparse
import os
import shutil
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

# Interactive prompt session (prompt_toolkit); None when stdin is piped.
_prompt_session = None


def load_config(path: Path) -> dict:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        print(f"{YELLOW}warning: ignoring invalid config {path}: {exc}{RESET}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def make_prompt_session(vi_mode: bool, state_dir: Path):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.cursor_shapes import ModalCursorShapeConfig
    from prompt_toolkit.history import FileHistory

    state_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {"history": FileHistory(str(state_dir / "history")), "vi_mode": vi_mode}
    if vi_mode:
        kwargs["cursor"] = ModalCursorShapeConfig()
    return PromptSession(**kwargs)


def _bottom_rule(vi_mode: bool) -> str:
    width = shutil.get_terminal_size((80, 24)).columns
    if not vi_mode:
        return "─" * width
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.key_binding.vi_state import InputMode

    mode = get_app().vi_state.input_mode
    if mode == InputMode.NAVIGATION:
        label = " NORMAL "
    elif mode == InputMode.REPLACE:
        label = " REPLACE "
    else:
        label = " INSERT "
    return "─" * 3 + label + "─" * max(0, width - len(label) - 3)


def _toolbar_style():
    from prompt_toolkit.styles import Style

    return Style.from_dict({"bottom-toolbar": "noreverse fg:ansibrightblack"})


def edit_line(initial: str) -> str:
    """Line editing with the command pre-filled."""
    if _prompt_session is not None:
        return _prompt_session.prompt("edit> ", default=initial).strip()
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


def make_approver(ask_all: bool, allow_path: Path, log: SessionLog | None):
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


def read_task(cwd: str, vi_mode: bool = False) -> str:
    """Boxed prompt: cwd on its own line, input between full-width rules.
    The bottom rule is a prompt_toolkit toolbar (shows NORMAL/INSERT in vi
    mode) and tracks the input properly even when it wraps. Piped stdin gets
    a plain prompt instead."""
    home = str(Path.home())
    display = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    if _prompt_session is None:
        return input(f"\naish:{display}> ")

    width = shutil.get_terminal_size((80, 24)).columns
    print(f"\n{DIM}{display}{RESET}")
    print(f"{DIM}{'─' * width}{RESET}")
    return _prompt_session.prompt(
        [("bold", "❯ ")],
        bottom_toolbar=lambda: _bottom_rule(vi_mode),
        style=_toolbar_style(),
    )


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

    global _prompt_session
    if sys.stdin.isatty():
        _prompt_session = make_prompt_session(args.vi_mode, state_dir)

    context = "\n\n".join(
        [
            environment_context(cwd),
            usage_context(args.model, args.vi_mode, allow_path, state_dir, config_path),
            *load_context_files(cwd),
        ]
    )
    agent = Agent(
        model=args.model,
        approve=make_approver(args.ask_all, allow_path, log),
        echo=echo,
        stream=stream_line,
        num_ctx=args.num_ctx,
        max_steps=args.max_steps,
        think=args.think,
        cwd=cwd,
        context=context,
        on_message=log.message,
    )
    if history:
        agent.load_history(history)
        print(f"{DIM}resumed {len(history)} messages from {log.path.name}{RESET}")

    if args.task:
        print(f"{GREEN}{agent.run_task(' '.join(args.task))}{RESET}")
        return 0

    print(f"{DIM}aish — model {args.model} · session {log.path.name} · Ctrl-D to quit{RESET}")
    while True:
        try:
            task = read_task(agent.cwd, args.vi_mode).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if task in ("exit", "quit"):
            return 0
        if not task:
            continue
        if task.startswith("!"):
            command = task[1:].strip()
            if command:
                log.command(command, "user-direct")
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
