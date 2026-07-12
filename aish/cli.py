"""Interactive CLI: one-shot task from argv, or a REPL keeping conversation state."""

import argparse
import os
import sys

from .agent import Agent
from .approval import is_read_only

BOLD = "\033[1m"
DIM = "\033[2m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RESET = "\033[0m"

ECHO_PREVIEW_LINES = 12


def make_approver(ask_all: bool):
    def ask_approval(command: str) -> bool:
        if not ask_all and is_read_only(command):
            print(f"\n{GREEN}✓ read-only, auto-approved:{RESET} {BOLD}{command}{RESET}")
            return True
        print(f"\n{YELLOW}{BOLD}▶ run command?{RESET}\n  {BOLD}{command}{RESET}")
        try:
            answer = input(f"{YELLOW}[y/N]{RESET} ").strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")

    return ask_approval


def echo(text: str) -> None:
    lines = text.splitlines()
    shown = lines[:ECHO_PREVIEW_LINES]
    print(DIM + "\n".join(f"  {line}" for line in shown) + RESET)
    if len(lines) > ECHO_PREVIEW_LINES:
        print(f"{DIM}  … ({len(lines) - ECHO_PREVIEW_LINES} more lines fed to model){RESET}")


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
    args = parser.parse_args()

    agent = Agent(
        model=args.model,
        approve=make_approver(args.ask_all),
        echo=echo,
        num_ctx=args.num_ctx,
        max_steps=args.max_steps,
        think=args.think,
    )

    if args.task:
        print(f"{GREEN}{agent.run_task(' '.join(args.task))}{RESET}")
        return 0

    print(f"{DIM}aish — model {args.model}. Ctrl-D or 'exit' to quit.{RESET}")
    while True:
        try:
            task = input(f"\n{BOLD}aish>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if task in ("exit", "quit"):
            return 0
        if not task:
            continue
        try:
            print(f"\n{GREEN}{agent.run_task(task)}{RESET}")
        except KeyboardInterrupt:
            print(f"\n{YELLOW}(task interrupted){RESET}")


if __name__ == "__main__":
    sys.exit(main())
