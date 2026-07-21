"""Claude-subscription backend: aish's brain runs through the Claude Agent SDK.

`--model claude-max[:opus|sonnet|<model-id>]` routes tasks through the local
`claude` CLI's login, so usage draws on a Claude Pro/Max subscription instead
of per-token API billing (Anthropic officially supports Agent SDK usage on
those plans).

The SDK normally supplies the whole Claude Code harness; here it is stripped
to bare inference: `tools=[]` removes every built-in tool (Read/Write/Bash/…)
and aish's own tools are re-exposed as in-process MCP tools whose handlers
call the same dispatch path the local Agent uses — so command approval, the
denylist, and file-diff review all still gate every action.

Unlike the chat backends this cannot be a `client_chat`: the SDK owns the
agent loop. ClaudeMaxAgent therefore mirrors the parts of Agent's surface the
CLI drives (run_task, run_user_command, reset, cwd, model, provider) and keeps
multi-turn context by resuming the SDK session id between tasks.
"""

import asyncio
import os
from typing import Any

from . import tools
from .agent import Agent, ModelUnavailable, compose_system_content, format_secs

SESSION_NOTE = (
    "[note for the model: the user ran `{command}` directly; output:]\n{output}"
)


class ClaudeMaxAgent:
    provider = "claude-max"

    def __init__(
        self,
        model: str = "",
        approve=None,
        approve_write=None,
        approve_read=None,
        echo=lambda _t: None,
        stream=None,
        max_steps: int = 25,
        cwd: str | None = None,
        context: str = "",
        on_message=None,
        on_token=None,
        job_log_dir=None,
        lessons_path=None,
        status=None,
        state_dir=None,
        current_session=None,
        state_log=None,
        on_state=None,
        aliases=None,
        **_ignored,
    ):
        # The inner Agent supplies tool dispatch (approval, denylist, file
        # diffs, cd tracking); its chat client is never invoked. Workspace
        # sinks flow to it so /cd + dir-trust persist and emit for claude-max
        # too (rebase/add_root/trust_root all delegate to the inner Agent).
        self.inner = Agent(
            model="unused",
            approve=approve or (lambda _c: None),
            approve_write=approve_write or (lambda _p: False),
            approve_read=approve_read or (lambda _p, _r: True),
            echo=echo,
            stream=stream,
            client_chat=self._never_called,
            cwd=cwd,
            job_log_dir=job_log_dir,
            lessons_path=lessons_path,
            state_dir=state_dir,
            current_session=current_session,
            state_log=state_log,
            on_state=on_state,
            aliases=aliases,
        )
        self.model = model  # "" = the claude CLI's configured default
        self.echo = echo
        self.max_steps = max_steps
        self.on_message = on_message
        self.on_token = on_token
        self.status = status
        self.base_context = context
        self.messages: list[dict] = []  # display-only history (replay/logs)
        self._session_id: str | None = None
        self._pending_notes: list[str] = []
        self._sdk = self._load_sdk()
        self._server, self._tool_names = self._build_server()

    # ------------------------------------------------------------ plumbing

    @staticmethod
    def _never_called(**_kwargs):
        raise AssertionError("claude-max drives the SDK loop; no chat client")

    @property
    def cwd(self) -> str:
        return self.inner.cwd

    @property
    def scratch_dir(self):
        return self.inner.scratch_dir

    def close(self) -> None:
        self.inner.close()

    @property
    def roots(self):
        return self.inner.roots

    @property
    def aliases(self):
        return self.inner.aliases

    @property
    def lessons_path(self):
        return self.inner.lessons_path

    def rebase(self, target: str) -> str:
        result = self.inner.rebase(target)
        if not result.startswith("ERROR"):
            self._pending_notes.append(
                f"[I moved the session to {self.cwd} with /cd — this directory "
                "is the project now]"
            )
        return result

    def add_root(self, target: str) -> str:
        result = self.inner.add_root(target)
        if result.startswith("[added"):
            self._pending_notes.append(
                f"[I added {self.roots[-1]} as a session root with /add-dir — "
                "you may work there too]"
            )
        return result

    def trust_root(self, target: str) -> str:
        return self.inner.trust_root(target)

    def restore_workspace(self, cwd: str | None, trusted: list[str]) -> None:
        self.inner.restore_workspace(cwd, trusted)

    @staticmethod
    def _load_sdk():
        try:
            import claude_agent_sdk
        except ModuleNotFoundError as exc:
            raise ModelUnavailable(
                "the 'claude-agent-sdk' package is missing — reinstall aish "
                "(uv tool install --force --reinstall /path/to/aish)"
            ) from exc
        return claude_agent_sdk

    def _build_server(self):
        sdk = self._sdk
        sdk_tools = []
        names = []

        def make_handler(name: str):
            async def handler(args: dict[str, Any]):
                # Tool handlers run on the SDK's event loop; aish approval
                # prompts block on stdin, so push them to a worker thread.
                try:
                    result = await asyncio.to_thread(
                        self.inner._dispatch, name, args or {}
                    )
                except Exception as exc:  # noqa: BLE001 — never kill the loop
                    result = f"ERROR: tool '{name}' failed internally: {exc!r}"
                    self.echo(result)
                return {"content": [{"type": "text", "text": result}]}

            return handler

        for schema in tools.TOOL_SCHEMAS:
            function = schema["function"]
            name = function["name"]
            names.append(name)
            sdk_tools.append(
                sdk.tool(name, function["description"], function["parameters"])(
                    make_handler(name)
                )
            )
        return sdk.create_sdk_mcp_server("aish", "1.0.0", sdk_tools), names

    # ------------------------------------------------------------- surface

    def reset(self) -> None:
        self._session_id = None
        self._pending_notes.clear()
        self.messages.clear()

    def load_history(self, _messages: list[dict]) -> None:
        self.echo(
            "(claude-max keeps its own session state — resuming aish session "
            "history into it is not supported; showing it for reference only)"
        )

    def run_user_command(self, command: str) -> str:
        """! escape: run locally now, tell the model on the next task."""
        command = self.inner.expand_alias(command)
        cd_target = self.inner._parse_cd(command)
        if cd_target is not None:
            return self.rebase(cd_target)  # !cd aliases /cd: root moves too
        result = tools.run_command(
            command,
            cwd=self.cwd,
            on_line=self.inner.stream,
            allow_detach=True,
            log_dir=self.inner.job_log_dir,
        )
        self._pending_notes.append(SESSION_NOTE.format(command=command, output=result))
        return result

    def run_task(self, task: str) -> str:
        prompt = task
        if self._pending_notes:
            prompt = "\n\n".join([*self._pending_notes, task])
            self._pending_notes.clear()
        self._record({"role": "user", "content": task})
        try:
            result = asyncio.run(self._run(prompt))
        except KeyboardInterrupt:
            raise
        except ModelUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — subprocess/transport errors
            raise ModelUnavailable(str(exc)) from exc
        self._record({"role": "assistant", "content": result})
        return result

    def _record(self, message: dict) -> None:
        self.messages.append(message)
        if self.on_message:
            self.on_message(message)

    # ------------------------------------------------------------ SDK loop

    def _options(self):
        sdk = self._sdk
        return sdk.ClaudeAgentOptions(
            # Recomposed every query: the skills index follows live cwd and
            # picks up skills created mid-session. Mid-session skill edits do
            # invalidate the API prompt cache for the changed prefix — an
            # accepted cost, skills change rarely.
            system_prompt=compose_system_content(
                self.base_context,
                self.cwd,
                self.inner.lessons_path,
                scratch_dir=self.inner.scratch_dir,
            ),
            model=self.model or None,
            tools=[],  # no Claude Code built-ins — aish tools only
            mcp_servers={"aish": self._server},
            allowed_tools=[f"mcp__aish__{name}" for name in self._tool_names],
            cwd=self.cwd,
            max_turns=self.max_steps,
            setting_sources=[],  # ignore the user's Claude Code config/CLAUDE.md
            include_partial_messages=self.on_token is not None,
            resume=self._session_id,
        )

    async def _run(self, prompt: str) -> str:
        sdk = self._sdk
        final = ""
        streamed = False
        if self.status:
            self.status.start("thinking")
        try:
            async for message in sdk.query(prompt=prompt, options=self._options()):
                if isinstance(message, sdk.StreamEvent):
                    text = _delta_text(message)
                    if text and self.on_token:
                        if self.status:
                            self.status.stop()
                        if not streamed:
                            self.on_token("\n")
                        streamed = True
                        self.on_token(text)
                elif isinstance(message, sdk.SystemMessage):
                    if getattr(message, "subtype", "") == "init" and not self.model:
                        # Surface which model the CLI's default resolved to.
                        self.model = (message.data or {}).get("model") or self.model
                elif isinstance(message, sdk.AssistantMessage):
                    for block in message.content:
                        if isinstance(block, sdk.TextBlock) and block.text:
                            final = block.text
                            if self.on_token is None:
                                self.echo(block.text)
                elif isinstance(message, sdk.ResultMessage):
                    self._session_id = message.session_id or self._session_id
                    if message.result:
                        final = message.result
                    self._report(message)
        finally:
            if self.status:
                self.status.stop()
        if streamed:
            self.on_token("\n")
        return final or "(the model returned no text)"

    def _report(self, result) -> None:
        note = f"∑ total {format_secs((result.duration_ms or 0) / 1000)}"
        usage = result.usage or {}
        tokens_out = usage.get("output_tokens")
        if tokens_out:
            note += f" · ↓ {tokens_out} tokens"
        # The SDK computes a nominal cost either way; billing mode depends on
        # auth — an API key in the environment outbills the subscription.
        if result.total_cost_usd:
            if os.environ.get("ANTHROPIC_API_KEY"):
                note += f" · ${result.total_cost_usd:.4f} (API-key billing)"
            else:
                note += f" · subscription (≈${result.total_cost_usd:.4f} API-equivalent)"
        self.echo(note)


def _delta_text(event) -> str:
    raw = getattr(event, "event", None) or {}
    if raw.get("type") != "content_block_delta":
        return ""
    delta = raw.get("delta") or {}
    if delta.get("type") != "text_delta":
        return ""
    return delta.get("text") or ""


def api_key_warning() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return (
            "ANTHROPIC_API_KEY is set — the claude CLI will bill that key "
            "instead of your subscription; unset it to use your plan"
        )
    return None
