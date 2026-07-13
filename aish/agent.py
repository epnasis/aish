"""The agent loop: model proposes tool calls, we execute them (gated), repeat.

The model never executes anything itself — Ollama only returns structured
tool_call requests. _dispatch() is the single execution point, and
run_command cannot be reached there unless the approve() callback returns
the command to run (possibly edited by the user).
"""

import datetime
import getpass
import os
import platform
import shlex
import sys
from collections.abc import Callable
from typing import Any

import ollama

from . import files, skills, tools
from .approval import Blocked

_PLATFORM_NOTES = {
    "darwin": (
        "macOS (BSD userland, zsh — NOT GNU/Linux). Your memorized flag "
        "knowledge is often wrong here: BSD sed/date/stat/find differ from GNU."
    ),
    "linux": "Linux (GNU userland). Flag details still vary by distro and version.",
}

SYSTEM_PROMPT_TEMPLATE = """\
You are aish, a CLI agent on {platform_note}

Rules:
1. GROUNDING: before running any command whose flags you are not 100% certain
   of, call read_docs for it first. Never guess flags.
2. If a command fails with a usage or unknown-flag error, call read_docs
   before retrying. If docs come back truncated, call read_docs again with a
   topic (e.g. the flag name) to search the full text.
3. Every command is shown to the user for approval before it runs. The user
   may edit a command before approving; the edited form is what ran. If the
   user denies a command, do not retry it — change approach or ask.
4. After running commands, analyze the output and answer concisely.
5. Prefer read-only commands. Never bundle destructive operations
   (rm, mv, overwrite redirects) into a command unless the user explicitly
   asked for that operation.
6. You have a persistent working directory. To change it, run exactly
   `cd <dir>` as its own command — the new directory is echoed back and all
   later commands run there.
"""

DENIED_RESULT = (
    "USER DENIED this command — it was NOT executed. "
    "Do not propose it again; change approach or ask the user."
)

EMPTY_RESPONSE = (
    "(the model returned an empty response — Ollama may be overloaded or still "
    "loading the model; try again, or check `ollama ps` and system load)"
)


class ModelUnavailable(RuntimeError):
    """The model call failed after a retry (Ollama down, overloaded, or OOM)."""


WRITE_DENIED = (
    "USER DENIED this file change — nothing was written. "
    "Do not retry the same change; adjust it or ask the user what they want."
)

BLOCKED_RESULT = (
    "BLOCKED by the safety denylist ({reason}) — NOT executed, and it cannot "
    "be approved through you at all. If the user truly intends this, they must "
    "run it themselves with the ! prefix. Propose a safer alternative if one exists."
)

TRIM_KEEP_CHARS = 200
TRIMMED_NOTE = "\n[trimmed: full output dropped to save context]"
# Rough tokens→chars margin: ~4 chars/token, keep well under num_ctx so the
# system prompt is never silently evicted by Ollama's own truncation.
CHARS_PER_TOKEN_BUDGET = 3


def system_prompt() -> str:
    note = _PLATFORM_NOTES.get(sys.platform, f"{sys.platform} (verify userland conventions).")
    return SYSTEM_PROMPT_TEMPLATE.format(platform_note=note)


def environment_context(cwd: str) -> str:
    if sys.platform == "darwin":
        os_desc = f"macOS {platform.mac_ver()[0]}"
    else:
        os_desc = platform.platform(terse=True)
    return (
        "Environment:\n"
        f"- today's date: {datetime.date.today().isoformat()}\n"
        f"- initial working directory: {cwd}\n"
        f"- user: {getpass.getuser()}\n"
        f"- OS: {os_desc} ({platform.machine()})"
    )


def _serialize(message: Any) -> dict:
    if isinstance(message, dict):
        return {k: message[k] for k in ("role", "content", "tool_name") if k in message}
    return {
        "role": getattr(message, "role", "assistant"),
        "content": getattr(message, "content", None) or "",
    }


class Agent:
    def __init__(
        self,
        model: str,
        approve: Callable[[str], Any],
        approve_write: Callable[[Any], bool] = lambda _plan: False,
        echo: Callable[[str], None] = lambda _: None,
        stream: Callable[[str], None] | None = None,
        client_chat: Callable[..., Any] = ollama.chat,
        num_ctx: int = 32768,
        max_steps: int = 25,
        think: bool = False,
        cwd: str | None = None,
        context: str = "",
        on_message: Callable[[dict], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        job_log_dir: os.PathLike | str | None = None,
    ):
        self.model = model
        self.approve = approve
        self.approve_write = approve_write
        self.echo = echo
        self.stream = stream
        self.chat = client_chat
        self.num_ctx = num_ctx
        self.max_steps = max_steps
        self.think = think
        self.cwd = cwd or os.getcwd()
        self.on_message = on_message
        self.on_token = on_token
        self.job_log_dir = job_log_dir
        content = system_prompt() + (f"\n{context}" if context else "")
        self.messages: list[Any] = [{"role": "system", "content": content}]

    def reset(self) -> None:
        """Drop the conversation, keep the system prompt."""
        del self.messages[1:]

    def load_history(self, messages: list[dict]) -> None:
        """Adopt messages from a previous session (already logged — appended
        directly so they are not re-recorded)."""
        self.messages.extend(m for m in messages if m.get("role") != "system")

    def _append(self, message: Any) -> None:
        self.messages.append(message)
        if self.on_message:
            self.on_message(_serialize(message))

    def run_task(self, task: str) -> str:
        # Old tasks' raw tool outputs are rarely needed verbatim again;
        # shrinking them keeps long REPL sessions inside the context window.
        task_start = len(self.messages)
        for message in self.messages[1:task_start]:
            self._trim_tool_message(message)

        self._append({"role": "user", "content": task})

        for _ in range(self.max_steps):
            self._enforce_budget(task_start)
            content, tool_calls = self._chat_turn()
            entry: dict = {"role": "assistant", "content": content}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            self._append(entry)

            if not tool_calls:
                result = content or EMPTY_RESPONSE
                if not content and self.on_token:
                    self.on_token(result + "\n")
                return result

            if content and self.on_token is None:
                self.echo(content)

            for call in tool_calls:
                name = call["function"]["name"]
                args = call["function"]["arguments"] or {}
                try:
                    result = self._dispatch(name, args)
                except Exception as exc:  # noqa: BLE001 — a tool bug must not kill the session
                    result = f"ERROR: tool '{name}' failed internally: {exc!r}"
                    self.echo(result)
                self._append({"role": "tool", "tool_name": name, "content": result})

        stopped = "(stopped: hit the max-steps limit without finishing — try a narrower task)"
        if self.on_token:
            self.on_token(stopped + "\n")
        return stopped

    def _chat_turn(self) -> tuple[str, list[dict]]:
        """One model call; returns (content, normalized tool_calls). Streams
        content through on_token when set. Retries once on a transport error
        (a busy/overloaded local Ollama commonly drops or refuses a request)."""
        kwargs = dict(
            model=self.model,
            messages=self.messages,
            tools=tools.TOOL_SCHEMAS,
            options={"num_ctx": self.num_ctx},
            think=self.think,
        )
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self._one_chat(kwargs)
            except Exception as exc:  # noqa: BLE001 — surface, don't crash the REPL
                last_error = exc
                if attempt == 0:
                    self.echo(f"model call failed ({exc}); retrying once…")
        raise ModelUnavailable(str(last_error)) from last_error

    def _one_chat(self, kwargs: dict) -> tuple[str, list[dict]]:
        if self.on_token is None:
            message = self.chat(**kwargs).message
            content = message.content or ""
            raw_calls = message.tool_calls or []
        else:
            parts: list[str] = []
            raw_calls = []
            for chunk in self.chat(stream=True, **kwargs):
                message = chunk.message
                if message.content:
                    if not parts:
                        self.on_token("\n")
                    parts.append(message.content)
                    self.on_token(message.content)
                if message.tool_calls:
                    raw_calls.extend(message.tool_calls)
            content = "".join(parts)
            if content:
                self.on_token("\n")
        return content, [self._normalize_call(c) for c in raw_calls]

    @staticmethod
    def _normalize_call(call: Any) -> dict:
        """Plain-dict tool call: safe to keep in history and send back to Ollama."""
        if isinstance(call, dict):
            function = call.get("function") or {}
            name = function.get("name", "")
            arguments = function.get("arguments") or {}
        else:
            name = call.function.name
            arguments = call.function.arguments or {}
        return {"function": {"name": name, "arguments": dict(arguments)}}

    def _trim_tool_message(self, message: Any) -> bool:
        if not (isinstance(message, dict) and message.get("role") == "tool"):
            return False
        content = message["content"]
        if len(content) <= TRIM_KEEP_CHARS + len(TRIMMED_NOTE):
            return False
        message["content"] = content[:TRIM_KEEP_CHARS] + TRIMMED_NOTE
        return True

    def _total_chars(self) -> int:
        total = 0
        for message in self.messages:
            if isinstance(message, dict):
                total += len(message.get("content") or "")
            else:
                total += len(getattr(message, "content", None) or "")
        return total

    def _enforce_budget(self, task_start: int) -> None:
        """Trim this task's oldest tool outputs (never the 2 most recent)
        until the conversation fits the character budget."""
        budget = self.num_ctx * CHARS_PER_TOKEN_BUDGET
        if self._total_chars() <= budget:
            return
        tool_indices = [
            i
            for i in range(task_start, len(self.messages))
            if isinstance(self.messages[i], dict) and self.messages[i].get("role") == "tool"
        ]
        for i in tool_indices[:-2]:
            if self._trim_tool_message(self.messages[i]) and self._total_chars() <= budget:
                return

    def run_user_command(self, command: str) -> str:
        """A command the user typed directly (! prefix): no approval needed,
        but recorded in the conversation so the model has the context."""
        cd_target = self._parse_cd(command)
        if cd_target is not None:
            result = self._change_dir(cd_target)
        else:
            result = tools.run_command(
                command,
                cwd=self.cwd,
                on_line=self.stream,
                allow_detach=True,
                log_dir=self.job_log_dir,
            )
        self._append(
            {"role": "user", "content": f"[I ran `{command}` myself; output:]\n{result}"}
        )
        return result

    def _dispatch(self, name: str, args: dict) -> str:
        if name == "read_docs":
            command = str(args.get("command", ""))
            topic = args.get("topic") or None
            label = f"→ read_docs: {command}" + (f" (topic: {topic})" if topic else "")
            self.echo(label)
            return tools.read_docs(command, topic=str(topic) if topic else None)

        if name == "read_skill":
            skill = str(args.get("name", ""))
            self.echo(f"→ read_skill: {skill}")
            return skills.load_skill(skill, skills.skill_dirs(self.cwd))

        if name == "read_file":
            path = str(args.get("path", ""))
            self.echo(f"→ read_file: {path}")
            return files.read_file(path, self.cwd)

        if name in ("write_file", "edit_file"):
            return self._dispatch_write(name, args)

        if name == "run_command":
            command = str(args.get("command", ""))

            cd_target = self._parse_cd(command)
            if cd_target is not None:
                return self._change_dir(cd_target)

            decision = self.approve(command)
            if isinstance(decision, Blocked):
                return BLOCKED_RESULT.format(reason=decision.reason)
            if decision is None or decision is False:
                return DENIED_RESULT
            final = command if decision is True else str(decision)
            if args.get("background"):
                result = tools.start_background(final, cwd=self.cwd, log_dir=self.job_log_dir)
                self.echo(result)
                return result
            result = tools.run_command(
                final,
                cwd=self.cwd,
                on_line=self.stream,
                allow_detach=True,
                log_dir=self.job_log_dir,
            )
            if self.stream is None:
                self.echo(result)
            if final != command:
                result = f"[user edited the command to: {final}]\n{result}"
            return result

        return f"ERROR: unknown tool '{name}'"

    def _dispatch_write(self, name: str, args: dict) -> str:
        if name == "write_file":
            plan = files.plan_write(
                str(args.get("path", "")), str(args.get("content", "")), self.cwd
            )
        else:
            plan = files.plan_edit(
                str(args.get("path", "")),
                str(args.get("old_str", "")),
                str(args.get("new_str", "")),
                self.cwd,
            )
        if plan.error:
            return f"ERROR: {plan.error}"
        if not self.approve_write(plan):
            return WRITE_DENIED
        result = files.commit(plan)
        self.echo(result)
        return result

    def _parse_cd(self, command: str) -> str | None:
        """A bare `cd <dir>` changes agent state instead of spawning a shell
        (where it would be a no-op). Compound forms (cd x && ...) run normally."""
        if any(ch in command for ch in ";&|<>`$(){}"):
            return None
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None
        if not tokens or tokens[0] != "cd" or len(tokens) > 2:
            return None
        return tokens[1] if len(tokens) == 2 else "~"

    def _change_dir(self, target: str) -> str:
        path = os.path.expanduser(target)
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(self.cwd, path))
        if not os.path.isdir(path):
            note = f"ERROR: no such directory: {path}"
            self.echo(note)
            return note
        self.cwd = path
        note = f"[working directory is now {path}]"
        self.echo(note)
        return note
