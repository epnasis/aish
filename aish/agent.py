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
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

import ollama

from . import files, skills, tools, web
from .approval import Blocked

_PLATFORM_NOTES = {
    "darwin": (
        "macOS (BSD userland, zsh — NOT GNU/Linux). BSD tools differ from GNU and "
        "your memorized flags are often the GNU ones. Common traps: `ps` has NO "
        "`--sort` / `-C` / long options — sort with `ps aux -r` (by CPU) or "
        "`ps aux -m` (by memory), or `ps -A -o pid,rss,comm | sort -k2 -rn`; "
        "`sed -i` REQUIRES a backup-suffix argument (`sed -i ''`); `date` uses "
        "`-v`/`-r`, not `-d`; `stat` uses `-f`, not `-c`; `find` lacks some GNU "
        "predicates. When unsure of a flag, call read_docs first."
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
2b. LEARN FROM MISTAKES: whenever you get a command or approach wrong and then
   find the form that works, call remember() with a one-line lesson holding the
   corrected, ready-to-run command — so next session you get it right the first
   time instead of re-deriving it. Applies to ANY tool or task, not just shell
   flags. Check the "lessons you saved" in your context before guessing.
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
7. WEB: for information not on this machine (current events, releases,
   unfamiliar errors, general facts), call web_search, then read_url the most
   promising result and answer from what the page actually says, citing the
   URL. Search queries and URLs LEAVE THIS MACHINE — never include private
   local data (file contents, key values, personal details) in them.
   read_url only reaches public internet hosts; for a localhost or LAN
   service, propose a curl command instead (it goes through approval).
   When researching, batch independent lookups: issue several web_search /
   read_url calls in a single reply — they run in parallel, which is much
   faster than one per turn.
"""

DENIED_RESULT = (
    "USER DENIED this command — it was NOT executed. "
    "Do not propose it again; change approach or ask the user."
)

EMPTY_RESPONSE = (
    "(the model returned an empty response — the backend may be overloaded or "
    "still loading; try again)"
)


class ModelUnavailable(RuntimeError):
    """The model call failed after a retry (backend down, overloaded, or OOM)."""


class TaskCancelled(Exception):
    """Raised inside the loop when cancel() interrupts a streaming turn."""


CANCELLED_RESULT = "(task stopped by user — any partial work is above)"
NOT_EXECUTED = "(not executed — the user stopped the task)"


WRITE_DENIED = (
    "USER DENIED this file change — nothing was written. "
    "Do not retry the same change; adjust it or ask the user what they want."
)

READ_DENIED = (
    "USER DENIED reading this sensitive file — its contents were NOT read. "
    "Do not retry; proceed without it or ask the user."
)

BLOCKED_RESULT = (
    "BLOCKED by the safety denylist ({reason}) — NOT executed, and it cannot "
    "be approved through you at all. If the user truly intends this, they must "
    "run it themselves with the ! prefix. Propose a safer alternative if one exists."
)

# No side effects and no approval prompt — safe to run concurrently.
READ_ONLY_TOOLS = frozenset({"read_docs", "read_skill", "web_search", "read_url", "read_file"})

def format_secs(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def format_tokens(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def _usage(response: Any) -> tuple[int, int]:
    """(prompt tokens, completion tokens) as Ollama reports them; zeros when
    absent. Note prompt_eval_count skips KV-cache-reused prefix tokens."""
    return (
        getattr(response, "prompt_eval_count", 0) or 0,
        getattr(response, "eval_count", 0) or 0,
    )


def _tokens_note(usage: tuple[int, int]) -> str:
    """' · ↑ 3.2k ↓ 96 tokens' — ↑ fed to the model, ↓ generated by it."""
    tokens_in, tokens_out = usage
    if not (tokens_in or tokens_out):
        return ""
    return f" · ↑ {format_tokens(tokens_in)} ↓ {format_tokens(tokens_out)} tokens"


class _NoStatus:
    """Default live-status sink: aish shows a ticking timer only when the CLI
    injects one (TTY); everywhere else these are no-ops."""

    def start(self, label: str) -> None:
        pass

    def add_tokens(self, count: int) -> None:
        pass

    def stop(self) -> None:
        pass

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


def _serialize(message: dict) -> dict:
    keys = ("role", "content", "tool_name", "images", "documents")
    return {k: message[k] for k in keys if k in message}


class Agent:
    def __init__(
        self,
        model: str,
        approve: Callable[[str], Any],
        approve_write: Callable[[Any], bool] = lambda _plan: False,
        approve_read: Callable[[str, str], bool] = lambda _path, _reason: True,
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
        lessons_path: os.PathLike | str | None = None,
        status: Any = None,
    ):
        self.model = model
        self.provider = "ollama"  # callers overwrite after construction (cli/server)
        self.task_sources: list[dict] = []  # pages read_url fetched for the current task
        self.approve = approve
        self.approve_write = approve_write
        self.approve_read = approve_read
        self.echo = echo
        self.stream = stream
        self.chat = client_chat
        self.num_ctx = num_ctx
        self.max_steps = max_steps
        self.think = think
        self.cwd = cwd or os.getcwd()
        # Session roots: auto-approved reads/commands are confined to these
        # trees. Seeded with the launch dir; only user-typed slash commands
        # (/cd, /add-dir) may change them — model-issued cd moves cwd only.
        self.roots: list[Path] = [Path(self.cwd).resolve()]
        self.on_message = on_message
        self.on_token = on_token
        self.job_log_dir = job_log_dir
        self.lessons_path = lessons_path
        self.status = status if status is not None else _NoStatus()
        self._cancel = threading.Event()
        content = system_prompt() + (f"\n{context}" if context else "")
        self.messages: list[dict] = [{"role": "system", "content": content}]

    def cancel(self) -> None:
        """Stop the running task at the next boundary: mid-stream (the token
        loop), before the next model call, before executing proposed tool
        calls, or by terminating the running shell command. Thread-safe —
        called from the server loop while run_task holds a worker thread."""
        self._cancel.set()

    def reset(self) -> None:
        """Drop the conversation, keep the system prompt."""
        del self.messages[1:]

    def load_history(self, messages: list[dict]) -> None:
        """Adopt messages from a previous session (already logged — appended
        directly so they are not re-recorded)."""
        self.messages.extend(m for m in messages if m.get("role") != "system")

    def _append(self, message: dict) -> None:
        self.messages.append(message)
        if self.on_message:
            self.on_message(_serialize(message))

    def run_task(
        self,
        task: str,
        images: list[str] | None = None,
        documents: list[str] | None = None,
    ) -> str:
        # Old tasks' raw tool outputs are rarely needed verbatim again;
        # shrinking them keeps long REPL sessions inside the context window.
        task_start = len(self.messages)
        for message in self.messages[1:task_start]:
            self._trim_tool_message(message)

        # Media rides on the user message as file paths; each backend encodes
        # them for its API (ollama `images`, data URLs, Anthropic blocks).
        user_message: dict = {"role": "user", "content": task}
        if images:
            user_message["images"] = list(images)
        if documents:
            user_message["documents"] = list(documents)
        self._append(user_message)

        self._cancel.clear()  # a stale stop must not kill the new task
        self.task_sources = []
        task_started = time.perf_counter()
        tokens_in = tokens_out = 0
        for _ in range(self.max_steps):
            if self._cancel.is_set():
                return self._finish_cancelled()
            self._enforce_budget(task_start)
            turn_start = time.perf_counter()
            self.status.start("thinking")
            try:
                content, tool_calls, usage, raw_blocks = self._chat_turn()
            except TaskCancelled:
                return self._finish_cancelled()
            finally:
                self.status.stop()
            turn_secs = time.perf_counter() - turn_start
            tokens_in += usage[0]
            tokens_out += usage[1]
            entry: dict = {"role": "assistant", "content": content}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if raw_blocks:
                # Provider-native content blocks (e.g. Anthropic thinking +
                # tool_use): the backend echoes these verbatim on the next
                # request instead of reconstructing the turn.
                entry["raw_blocks"] = raw_blocks
            self._append(entry)

            if not tool_calls:
                result = content or EMPTY_RESPONSE
                if not content and self.on_token:
                    self.on_token(result + "\n")
                self.echo(f"✓ answered in {format_secs(turn_secs)}{_tokens_note(usage)}")
                total = time.perf_counter() - task_started
                self.echo(
                    f"∑ total {format_secs(total)}{_tokens_note((tokens_in, tokens_out))}"
                )
                return result

            # Ollama buffers tool-call generation and streams nothing until it
            # is done, so live counts are impossible here — report per turn.
            self.echo(f"✓ thought for {format_secs(turn_secs)}{_tokens_note(usage)}")
            if content and self.on_token is None:
                self.echo(content)

            if self._cancel.is_set():
                # Proposed calls must not run after a stop — but every
                # tool_use still needs a paired result or the next request
                # is rejected (Anthropic pairing rules).
                for call in tool_calls:
                    self._append(
                        {
                            "role": "tool",
                            "tool_name": call["function"]["name"],
                            "content": NOT_EXECUTED,
                        }
                    )
                return self._finish_cancelled()

            results = self._execute_tool_calls(tool_calls)
            for call, result in zip(tool_calls, results, strict=True):
                self._append(
                    {"role": "tool", "tool_name": call["function"]["name"], "content": result}
                )
                self._collect_source(call, result)

        stopped = "(stopped: hit the max-steps limit without finishing — try a narrower task)"
        if self.on_token:
            self.on_token(stopped + "\n")
        return stopped

    def _finish_cancelled(self) -> str:
        """History stays model-consumable: an assistant note closes the turn."""
        self._append({"role": "assistant", "content": CANCELLED_RESULT})
        if self.on_token:
            self.on_token(CANCELLED_RESULT + "\n")
        self.echo("✕ task stopped")
        return CANCELLED_RESULT

    def _chat_turn(self) -> tuple[str, list[dict], tuple[int, int], list | None]:
        """One model call; returns (content, normalized tool_calls, token usage,
        provider-native raw blocks or None). Streams content through on_token
        when set. Retries once on a transport error (a busy/overloaded local
        Ollama commonly drops or refuses a request)."""
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
            except TaskCancelled:
                raise  # a user stop is not a transport error — never retry
            except Exception as exc:  # noqa: BLE001 — surface, don't crash the REPL
                last_error = exc
                if attempt == 0:
                    self.echo(f"model call failed ({exc}); retrying once…")
        raise ModelUnavailable(str(last_error)) from last_error

    def _one_chat(self, kwargs: dict) -> tuple[str, list[dict], tuple[int, int], list | None]:
        raw_blocks = None
        if self.on_token is None:
            response = self.chat(**kwargs)
            message = response.message
            content = message.content or ""
            raw_calls = message.tool_calls or []
            usage = _usage(response)
            raw_blocks = getattr(message, "raw_blocks", None)
        else:
            parts: list[str] = []
            raw_calls = []
            usage = (0, 0)
            for chunk in self.chat(stream=True, **kwargs):
                if self._cancel.is_set():
                    # Abandoning the iterator closes the connection, which
                    # stops generation server-side — the fastest stop there is.
                    raise TaskCancelled
                # Ollama streams ~one chunk per generated token, so chunk
                # count drives the live "↓ N tokens" readout on the ticker.
                self.status.add_tokens(1)
                message = chunk.message
                if message.content:
                    if not parts:
                        self.status.stop()  # erase the live timer line first
                        self.on_token("\n")
                    parts.append(message.content)
                    self.on_token(message.content)
                if message.tool_calls:
                    raw_calls.extend(message.tool_calls)
                if getattr(message, "raw_blocks", None):
                    raw_blocks = message.raw_blocks
                if _usage(chunk) != (0, 0):  # counts arrive on the final chunk
                    usage = _usage(chunk)
            content = "".join(parts)
            if content:
                self.on_token("\n")
        return content, [self._normalize_call(c) for c in raw_calls], usage, raw_blocks

    @staticmethod
    def _normalize_call(call: Any) -> dict:
        """Plain-dict tool call: safe to keep in history and send back to the
        backend. extra_content (e.g. Gemini thought signatures) must survive
        the round trip — some providers reject the next request without it."""
        if isinstance(call, dict):
            function = call.get("function") or {}
            name = function.get("name", "")
            arguments = function.get("arguments") or {}
            extra = call.get("extra_content")
        else:
            name = call.function.name
            arguments = call.function.arguments or {}
            extra = getattr(call, "extra_content", None)
        normalized = {"function": {"name": name, "arguments": dict(arguments)}}
        if extra:
            normalized["extra_content"] = extra
        return normalized

    def _trim_tool_message(self, message: dict) -> bool:
        if message.get("role") != "tool":
            return False
        content = message["content"]
        if len(content) <= TRIM_KEEP_CHARS + len(TRIMMED_NOTE):
            return False
        message["content"] = content[:TRIM_KEEP_CHARS] + TRIMMED_NOTE
        return True

    def _total_chars(self) -> int:
        return sum(len(message.get("content") or "") for message in self.messages)

    def _enforce_budget(self, task_start: int) -> None:
        """Trim this task's oldest tool outputs (never the 2 most recent)
        until the conversation fits the character budget."""
        budget = self.num_ctx * CHARS_PER_TOKEN_BUDGET
        if self._total_chars() <= budget:
            return
        tool_indices = [
            i
            for i in range(task_start, len(self.messages))
            if self.messages[i].get("role") == "tool"
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

    def rebase(self, target: str) -> str:
        """User-typed /cd: move cwd AND re-anchor the primary session root.
        Never reachable by the model — that's what keeps root scoping honest."""
        result = self._change_dir(target)
        if result.startswith("ERROR"):
            return result
        self.roots[0] = Path(self.cwd).resolve()
        self.echo(f"[session root re-anchored to {self.roots[0]}]")
        self._append(
            {"role": "user", "content": f"[I moved the session to {self.cwd} with /cd — "
             "this directory is the project now]"}
        )
        return result

    def add_root(self, target: str) -> str:
        """User-typed /add-dir: allow auto-approved reads/commands in another tree."""
        path = Path(os.path.expanduser(target))
        if not path.is_absolute():
            path = Path(self.cwd) / path
        path = path.resolve()
        if not path.is_dir():
            return f"ERROR: no such directory: {path}"
        if path in self.roots:
            return f"[{path} is already a session root]"
        self.roots.append(path)
        note = f"[I added {path} as a session root with /add-dir — you may work there too]"
        self._append({"role": "user", "content": note})
        return f"[added session root {path}]"

    def _execute_tool_calls(self, tool_calls: list[dict]) -> list[str]:
        """Run one model turn's tool calls; results keep the call order.

        Read-only tools (no side effects, no approval prompt) run concurrently
        when the turn has more than one — they are network/disk-bound, so this
        is a pure latency win. Anything that prompts the user or writes stays
        sequential: two interleaved [y/N] prompts would be unanswerable.
        """
        calls = [(c["function"]["name"], c["function"]["arguments"] or {}) for c in tool_calls]
        concurrent = [
            i
            for i, (name, args) in enumerate(calls)
            if name in READ_ONLY_TOOLS and not self._read_needs_prompt(name, args)
        ]
        if len(concurrent) < 2:
            return [
                self._call_result(name, partial(self._timed, partial(self._dispatch, name, args)))
                for name, args in calls
            ]

        results: list[str] = [""] * len(calls)
        with ThreadPoolExecutor(max_workers=min(len(concurrent), 8)) as pool:
            batch_start = time.perf_counter()
            futures = {}
            for i in concurrent:
                label, thunk = self._read_only_call(*calls[i])
                self.echo(label)
                # _timed runs on the worker so the reported duration is the
                # call's true runtime, not how long collection waited for it.
                futures[i] = pool.submit(self._timed, thunk)
            # Collect futures first, under one live timer; future.result()
            # re-raises worker exceptions here, so error echoes stay on the
            # main thread. Tools that may prompt the user run after the timer
            # stops — a [y/N] prompt must never fight the ticking line.
            self.status.start(f"{len(futures)} parallel lookups")
            try:
                for i in futures:
                    # ⇉ marks overlapped runtimes: they exceed wall time when
                    # summed, so only the batch ✓ line below counts toward ∑.
                    results[i] = self._call_result(calls[i][0], futures[i].result, mark="⇉")
            finally:
                self.status.stop()
            self.echo(
                f"✓ {len(futures)} parallel lookups "
                f"{format_secs(time.perf_counter() - batch_start)}"
            )
            for i, (name, args) in enumerate(calls):
                if i not in futures:
                    results[i] = self._call_result(
                        name, partial(self._timed, partial(self._dispatch, name, args))
                    )
        return results

    @staticmethod
    def _timed(fn: Callable[[], str]) -> tuple[str, float]:
        start = time.perf_counter()
        return fn(), time.perf_counter() - start

    def _call_result(
        self, name: str, fn: Callable[[], tuple[str, float]], mark: str = "✓"
    ) -> str:
        try:
            result, elapsed = fn()
        except ModuleNotFoundError as exc:
            # A broken install, not a transient failure: retrying the
            # same call can never succeed, so say so to the model too.
            result = (
                f"ERROR: tool '{name}' is unavailable — this aish "
                f"installation is missing the '{exc.name}' package. "
                "Do NOT retry this tool; it will keep failing. Tell "
                "the user to reinstall aish (uv tool install --force "
                "git+https://github.com/epnasis/aish.git) and restart."
            )
            self.echo(result)
            return result
        except Exception as exc:  # noqa: BLE001 — a tool bug must not kill the session
            result = f"ERROR: tool '{name}' failed internally: {exc!r}"
            self.echo(result)
            return result
        self.echo(f"{mark} {name} {format_secs(elapsed)}")
        return result

    def _read_only_call(self, name: str, args: dict) -> tuple[str, Callable[[], str]]:
        """(echo label, execution thunk) for a READ_ONLY_TOOLS member — split
        so the label prints before the thunk possibly runs on a worker thread."""
        if name == "read_docs":
            command = str(args.get("command", ""))
            topic = args.get("topic") or None
            label = f"→ read_docs: {command}" + (f" (topic: {topic})" if topic else "")
            return label, partial(tools.read_docs, command, topic=str(topic) if topic else None)
        if name == "read_skill":
            skill = str(args.get("name", ""))
            return f"→ read_skill: {skill}", partial(
                skills.load_skill, skill, skills.skill_dirs(self.cwd)
            )
        if name == "web_search":
            query = str(args.get("query", ""))
            return f"→ web_search: {query}", partial(web.web_search, query)
        if name == "read_url":
            url = str(args.get("url", ""))
            topic = args.get("topic") or None
            label = f"→ read_url: {url}" + (f" (topic: {topic})" if topic else "")
            return label, partial(web.read_url, url, topic=str(topic) if topic else None)
        return self._read_file_call(args)  # read_file

    def _collect_source(self, call: dict, result: str) -> None:
        """Track pages actually fetched this task, so answers can cite them.
        Only read_url counts — web_search hits are found-but-maybe-unread."""
        if call["function"]["name"] != "read_url" or result.startswith("ERROR"):
            return
        url = str((call["function"].get("arguments") or {}).get("url", "")).strip()
        if not url or any(s["url"] == url for s in self.task_sources):
            return
        source = {"url": url}
        title = web.PAGE_TITLES.get(url)
        if title:
            source["title"] = title
        self.task_sources.append(source)

    def _read_needs_prompt(self, name: str, args: dict) -> bool:
        path = str(args.get("path", ""))
        return name == "read_file" and self._read_prompt_reason(path) is not None

    def _read_prompt_reason(self, path: str) -> str | None:
        """Why an otherwise auto-approved read_file must prompt, or None."""
        if files.is_sensitive_path(path, self.cwd):
            return "sensitive"
        if files.is_outside_roots(path, self.cwd, self.roots):
            return "outside"
        return None

    @staticmethod
    def _int_arg(args: dict, key: str, default: int) -> int:
        try:
            return int(args.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _read_file_call(self, args: dict) -> tuple[str, Callable[[], str]]:
        path = str(args.get("path", ""))
        offset = self._int_arg(args, "offset", 1)
        limit = self._int_arg(args, "limit", files.READ_MAX_LINES)
        label = f"→ read_file: {path}" + (f" (from line {offset})" if offset > 1 else "")
        return label, partial(files.read_file, path, self.cwd, offset=offset, limit=limit)

    def _dispatch(self, name: str, args: dict) -> str:
        if name == "read_file":
            path = str(args.get("path", ""))
            label, thunk = self._read_file_call(args)
            self.echo(label)
            reason = self._read_prompt_reason(path)
            if reason is not None and not self.approve_read(path, reason):
                return READ_DENIED
            return thunk()

        if name in READ_ONLY_TOOLS:
            label, thunk = self._read_only_call(name, args)
            self.echo(label)
            self.status.start(name)
            try:
                return thunk()
            finally:
                self.status.stop()

        if name == "remember":
            note = str(args.get("note", ""))
            if self.lessons_path is None:
                return "ERROR: no lessons file configured"
            result = tools.remember(note, self.lessons_path)
            self.echo(f"→ {result}")
            return result

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
                should_stop=self._cancel.is_set,
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
