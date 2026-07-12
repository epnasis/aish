"""The agent loop: model proposes tool calls, we execute them (gated), repeat.

The model never executes anything itself — Ollama only returns structured
tool_call requests. _dispatch() is the single execution point, and
run_command cannot be reached there without the approve() callback
returning True.
"""

from collections.abc import Callable
from typing import Any

import ollama

from . import tools

SYSTEM_PROMPT = """\
You are aish, a CLI agent on macOS (BSD userland, zsh — NOT GNU/Linux).

Rules:
1. GROUNDING: before running any command whose flags you are not 100% certain
   of, call read_docs for it first. Your memorized flag knowledge is often
   wrong on macOS (BSD sed/date/stat/find differ from GNU). Never guess flags.
2. If a command fails with a usage or unknown-flag error, call read_docs
   before retrying.
3. Every command is shown to the user for approval before it runs. If the
   user denies a command, do not retry it — change approach or ask.
4. After running commands, analyze the output and answer concisely.
5. Prefer read-only commands. Never bundle destructive operations
   (rm, mv, overwrite redirects) into a command unless the user explicitly
   asked for that operation.
"""

DENIED_RESULT = (
    "USER DENIED this command — it was NOT executed. "
    "Do not propose it again; change approach or ask the user."
)


class Agent:
    def __init__(
        self,
        model: str,
        approve: Callable[[str], bool],
        echo: Callable[[str], None] = lambda _: None,
        client_chat: Callable[..., Any] = ollama.chat,
        num_ctx: int = 32768,
        max_steps: int = 25,
        think: bool = False,
    ):
        self.model = model
        self.approve = approve
        self.echo = echo
        self.chat = client_chat
        self.num_ctx = num_ctx
        self.max_steps = max_steps
        self.think = think
        self.messages: list[Any] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def run_task(self, task: str) -> str:
        self.messages.append({"role": "user", "content": task})

        for _ in range(self.max_steps):
            response = self.chat(
                model=self.model,
                messages=self.messages,
                tools=tools.TOOL_SCHEMAS,
                options={"num_ctx": self.num_ctx},
                think=self.think,
            )
            message = response.message
            self.messages.append(message)

            if not message.tool_calls:
                return message.content or "(model returned an empty response)"

            if message.content:
                self.echo(message.content)

            for call in message.tool_calls:
                name = call.function.name
                args = call.function.arguments or {}
                result = self._dispatch(name, args)
                self.messages.append(
                    {"role": "tool", "tool_name": name, "content": result}
                )

        return "(stopped: hit the max-steps limit without finishing — try a narrower task)"

    def _dispatch(self, name: str, args: dict) -> str:
        if name == "read_docs":
            command = str(args.get("command", ""))
            self.echo(f"→ read_docs: {command}")
            return tools.read_docs(command)

        if name == "run_command":
            command = str(args.get("command", ""))
            if not self.approve(command):
                return DENIED_RESULT
            result = tools.run_command(command)
            self.echo(result)
            return result

        return f"ERROR: unknown tool '{name}'"
