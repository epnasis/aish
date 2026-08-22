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
import threading
from functools import partial
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
        approve_tool=None,
        approve_import=None,
        echo=lambda _t: None,
        stream=None,
        max_steps: int = 25,
        cwd: str | None = None,
        context: str = "",
        on_message=None,
        on_token=None,
        on_delivered=None,
        on_step=None,
        on_command_start=None,
        on_command_end=None,
        step_log=None,
        command_log=None,
        job_log_dir=None,
        lessons_path=None,
        status=None,
        state_dir=None,
        current_session=None,
        state_log=None,
        on_state=None,
        check_pending_cwd=None,
        check_pending_messages=None,
        semantic=None,
        aliases=None,
        origin: str = "user",
    ):
        # NO **kwargs sink here, deliberately (#178 P0-4): every capability an
        # entry point passes must be either kept on this wrapper or forwarded
        # to the inner Agent — an unknown kwarg is a wiring bug and must raise
        # TypeError, not silently no-op a feature for claude-max sessions only.
        #
        # The inner Agent supplies tool dispatch (approval, denylist, file
        # diffs, cd tracking); its chat client is never invoked. Workspace
        # sinks flow to it so /cd + dir-trust persist and emit for claude-max
        # too (rebase/add_root/trust_root all delegate to the inner Agent), the
        # trace/command sinks so a claude-max session logs + renders the same
        # activity trace as every other backend, and the tool/import approvers
        # so plugin tools and skill imports gate identically. on_message stays
        # on this wrapper (display history is claude-max's own; the inner Agent
        # never appends conversation turns), and status drives the SDK loop's
        # "thinking" only — forwarding it would let a tool dispatch stop() the
        # still-running task status.
        self.inner = Agent(
            model="unused",
            approve=approve or (lambda _c: None),
            approve_write=approve_write or (lambda _p: False),
            approve_read=approve_read or (lambda _p, _r: True),
            approve_tool=approve_tool,
            approve_import=approve_import,
            echo=echo,
            stream=stream,
            client_chat=self._never_called,
            cwd=cwd,
            on_step=on_step,
            on_command_start=on_command_start,
            on_command_end=on_command_end,
            step_log=step_log,
            command_log=command_log,
            job_log_dir=job_log_dir,
            lessons_path=lessons_path,
            state_dir=state_dir,
            current_session=current_session,
            state_log=state_log,
            on_state=on_state,
            # Inert under claude-max (the SDK owns the loop, so the inner
            # run_task that polls these never runs) but forwarded so the
            # wrapper keeps zero capability state of its own.
            check_pending_cwd=check_pending_cwd,
            check_pending_messages=check_pending_messages,
            semantic=semantic,
            aliases=aliases,
            # Every origin-scoped gate lives in the inner Agent's _dispatch,
            # which _locked_dispatch is the single path to — so forwarding the
            # session's origin is what makes the egress gate (#178 P0-2) and the
            # knowledge-write gate (#196) hold on this backend too. The server
            # has always passed origin= here; without the parameter the whole
            # constructor raised TypeError, so no claude-max web session
            # survived construction at all.
            origin=origin,
        )
        # The wrapper's `provider` is a class attribute; the INNER agent's
        # defaults to "ollama" and nothing overwrote it. That matters since
        # #192, because the inner agent sizes plugin-output truncation from its
        # own provider — leaving it would cap a Claude session as if it were a
        # 32k local one, which is precisely the "cap describing a window that
        # is not the one in use" this phase exists to remove.
        self.inner.provider = self.provider
        self.model = model  # "" = the claude CLI's configured default
        self.echo = echo
        self.max_steps = max_steps
        self.on_message = on_message
        self.on_token = on_token
        # Narration (#212) is emitted by whichever loop is running, and here
        # that is the SDK's — so this sink stays on the wrapper, like
        # on_message and on_token, rather than being forwarded to an inner
        # run_task that never runs.
        self.on_delivered = on_delivered
        self.status = status
        self.base_context = context
        self.messages: list[dict] = []  # display-only history (replay/logs)
        self._session_id: str | None = None
        self._pending_notes: list[str] = []
        # The SDK may run tool handlers concurrently; the inner Agent is not
        # thread-safe (_run_meta, _pending_skill_reads, cwd) and two approval
        # prompts must never interleave — every entry into it serializes here.
        self._dispatch_lock = threading.Lock()
        self._sdk = self._load_sdk()
        self._tool_defs = self._current_tool_defs()
        self._server, self._tool_names = self._build_server(self._tool_defs)

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
    def session_prefixes(self):
        return self.inner.session_prefixes

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

    def turn_intent(self) -> str:
        # The approvers read this off whichever agent the session holds (#252),
        # and this class delegates by hand — an omission here is not a missing
        # reason on the card, it is an AttributeError inside the gate on every
        # approval this backend ever shows.
        return self.inner.turn_intent()

    def restore_workspace(self, cwd: str | None, trusted: list[str]) -> None:
        self.inner.restore_workspace(cwd, trusted)

    def resume_turns(self, last: int) -> None:
        # The turn counter lives on the inner agent for the same reason the
        # rest of this state does: _locked_dispatch routes through it, so it is
        # the thing that stamps the records.
        self.inner.resume_turns(last)

    def restore_opened_links(self, calls: list[tuple[dict, int]]) -> None:
        # Same reason as resume_turns: the ledger lives on the inner agent
        # because _locked_dispatch routes through it, so that is the object
        # whose verify pass reads it.
        self.inner.restore_opened_links(calls)

    def restore_browse_grants(self, hosts: list[str]) -> None:
        # Same reason as restore_opened_links: the gate runs on the inner agent
        # because _locked_dispatch routes through it, so that is the object
        # whose `_approved_browsing` decides.
        self.inner.restore_browse_grants(hosts)

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

    def _current_tool_defs(self) -> list[dict]:
        """Native tools plus discovered plugin tools (TOOL.md), exactly the set
        Agent._chat_turn exposes — mutating plugin tools appear only when a
        tool approver is wired (fail-closed in dispatch either way)."""
        self.inner._refresh_plugin_tools()
        return tools.TOOL_SCHEMAS + self.inner._plugin_defs

    def _refresh_server(self) -> None:
        """Rebuild the SDK MCP server when the exposed tool set moved (a
        create_tool or a TOOL.md dropped mid-session shows up next task)."""
        defs = self._current_tool_defs()
        if defs != self._tool_defs:
            self._tool_defs = defs
            self._server, self._tool_names = self._build_server(defs)

    def _locked_dispatch(self, name: str, args: dict) -> str:
        """The single execution path for SDK tool calls: serialized entry into
        the inner Agent, routed through _call_result so the same trace steps
        (tool_start/tool) are emitted and exceptions are contained exactly as
        in the native loop."""
        with self._dispatch_lock:
            return self.inner._call_result(
                name,
                partial(self.inner._timed, partial(self.inner._dispatch, name, args)),
                args=args,
            )

    def _build_server(self, defs: list[dict]):
        sdk = self._sdk
        sdk_tools = []
        names = []

        def make_handler(name: str):
            async def handler(args: dict[str, Any]):
                # Tool handlers run on the SDK's event loop; aish approval
                # prompts block on stdin, so push them to a worker thread.
                try:
                    result = await asyncio.to_thread(
                        self._locked_dispatch, name, args or {}
                    )
                except Exception as exc:  # noqa: BLE001 — never kill the loop
                    result = f"ERROR: tool '{name}' failed internally: {exc!r}"
                    self.echo(result)
                return {"content": [{"type": "text", "text": result}]}

            return handler

        for schema in defs:
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
        # The SDK owns the loop, so the inner Agent's run_task — where per-task
        # state normally resets — never runs here. Reset it explicitly or a
        # denial's stop gate (and stale _run_meta/skill gates/cancel) would
        # wedge every later tool call in this AND future tasks (#178 P0-4).
        self.inner._reset_task_state()
        self._refresh_server()  # plugin tools created since last task
        prompt = task
        if self._pending_notes:
            prompt = "\n\n".join([*self._pending_notes, task])
            self._pending_notes.clear()
        # Seed this turn's rule bindings (#191). The SDK owns the loop, so the
        # prose rides the prompt rather than a system reminder — but the gate is
        # the SAME one, because every SDK tool call routes back through
        # inner._dispatch. A rule that governed only local turns would be a rule
        # the model can escape by being asked on a different backend.
        if rules_text := self.inner.seed_rules(task):
            prompt = f"{rules_text}\n\n{prompt}"
            self.inner.mark_rules_seeded()
        self._record({"role": "user", "content": task})
        try:
            result = asyncio.run(self._run(prompt))
        except KeyboardInterrupt:
            raise
        except ModelUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — subprocess/transport errors
            raise ModelUnavailable(str(exc)) from exc
        # The SDK's answer is final by the time it lands here, so Verify runs
        # in its note-only mode: no ask (there is no loop to ask into, and the
        # text has already streamed), but the rules still get their say.
        result = self.inner.verify_final(result)
        self._record({"role": "assistant", "content": result})
        return result

    def _deliver_interim(self, text: str) -> None:
        """Close out one interim delivery on the SDK path (#212).

        The tokens have already streamed (the SDK reports partial messages when
        on_token is wired), so this marks the END of that message and hands the
        complete text over: recorded like any assistant turn, so a cold reload
        replays the narration instead of losing it, and joined to the turn's
        deliverable so `verify_final` grades everything the owner was told.
        """
        text = (text or "").strip()
        if not text or self.inner._delivered:
            # One acknowledgement per task, the same cap as the native loop:
            # the owner asked for a colleague's "I'm on it", not a progress log.
            return
        self.inner._delivered.append(text)
        # `interim` stamped explicitly: this path logs no tool-role records at
        # all (the SDK's tool calls leave trace steps), so the adjacency rule
        # every "was this the answer?" reader used cannot see it — each
        # narration line would count as a final answer and walk the fork
        # ordinal off by one per narrated turn.
        self._record({"role": "assistant", "content": text, "interim": True})
        if self.on_delivered:
            self.on_delivered(text)

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
        # The last assistant text seen. It is only a CANDIDATE answer: what
        # makes it a delivery is something happening after it (#212).
        pending = ""
        result_text = ""
        streamed = False
        tool_use = getattr(sdk, "ToolUseBlock", None)

        def close_pending() -> None:
            """The pending text is now known to be interim — hand it over."""
            nonlocal pending, streamed
            if not pending:
                return
            self._deliver_interim(pending)
            pending = ""
            # The next tokens open a NEW bubble, so they lead with their own
            # newline exactly as the first ones did.
            streamed = False

        if self.status:
            self.status.start("thinking")
        try:
            async for message in sdk.query(prompt=prompt, options=self._options()):
                if isinstance(message, sdk.StreamEvent):
                    text = _delta_text(message)
                    if text and self.on_token:
                        # A delta means a NEW message is being generated, so
                        # anything still pending was not the answer. Closing it
                        # HERE is what keeps the next message's tokens out of
                        # the previous delivery's bubble — the SDK reports
                        # partials for message N+1 after message N completes,
                        # so a delivery closed one message late would glue two
                        # messages together and paint the answer twice.
                        close_pending()
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
                            close_pending()
                            pending = block.text
                            if self.on_token is None:
                                self.echo(block.text)
                    if tool_use is not None and any(
                        isinstance(block, tool_use) for block in message.content
                    ):
                        # This message ACTED, so its words were narration
                        # whatever comes next. Closing on the message rather
                        # than waiting for the following one also puts the log
                        # record BEFORE the tool's trace steps, so a cold
                        # reload replays the narration above the work it
                        # announced instead of below it.
                        #
                        # The gate's copy is taken FIRST and outside the
                        # one-bubble cap (#252): narration suppressed from the
                        # chat must still reach the card of the action it
                        # explains. `pending` is emptied by close_pending, so
                        # a following tool-only message notes an empty intent
                        # and clears this one — the same self-clearing the
                        # native loop gets from assigning on every response.
                        self.inner.note_intent(pending)
                        close_pending()
                elif isinstance(message, sdk.ResultMessage):
                    self._session_id = message.session_id or self._session_id
                    if message.result:
                        result_text = message.result
                    self._report(message)
        finally:
            if self.status:
                self.status.stop()
        if streamed:
            self.on_token("\n")
        return result_text or pending or "(the model returned no text)"

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
