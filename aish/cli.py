"""Interactive CLI: one-shot task from argv, or a REPL keeping conversation state."""

import argparse
import os
import shutil
import sys
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


def edit_line(initial: str) -> str:
    """Line editing with the command pre-filled (falls back to blank input
    where readline can't pre-fill)."""
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


def read_task(cwd: str) -> str:
    """Boxed prompt: cwd on its own line, input between full-width rules.

    Cursor trick: draw both rules first, then move back up one line so the
    input line sits between them (a wrapped long input pushes past the bottom
    rule — cosmetic only). Piped stdin gets a plain prompt instead.
    """
    home = str(Path.home())
    display = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    if not sys.stdin.isatty():
        return input(f"\naish:{display}> ")

    width = shutil.get_terminal_size((80, 24)).columns
    rule = f"{DIM}{'─' * width}{RESET}"
    print(f"\n{DIM}{display}{RESET}")
    print(rule)
    print()  # placeholder: the input line
    print(rule, end="", flush=True)
    sys.stdout.write("\x1b[1A\r")  # up to the placeholder, column 0
    try:
        return input(f"{BOLD}❯{RESET} ")
    finally:
        print()  # step below the bottom rule without erasing it


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
    parser = argparse.ArgumentParser(
        prog="aish",
        description="Local LLM agent that runs CLI commands (with your approval).",
    )
    parser.add_argument("task", nargs="*", help="task to perform; omit for interactive mode")
    parser.add_argument(
        "--model",
        default=os.environ.get("AISH_MODEL", "qwen3.6:35b-a3b"),
        help="Ollama model (default: $AISH_MODEL or qwen3.6:35b-a3b)",
    )
    parser.add_argument("--num-ctx", type=int, default=32768, help="context window tokens")
    parser.add_argument("--max-steps", type=int, default=25, help="max model turns per task")
    parser.add_argument("--think", action="store_true", help="enable model thinking (slow)")
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

    context = "\n\n".join([environment_context(cwd), *load_context_files(cwd)])
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
            task = read_task(agent.cwd).strip()
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
