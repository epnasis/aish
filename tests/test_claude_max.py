"""ClaudeMaxAgent tests with a scripted fake SDK — no claude CLI, no
claude-agent-sdk package, no network.

Mirrors the FakeChat pattern from test_agent.py: the SDK seam is
`ClaudeMaxAgent._load_sdk` (the one lazy import site), monkeypatched to return
a FakeSDK. The fake captures the MCP tool handlers ClaudeMaxAgent registers,
so tests drive the exact coroutines the real SDK would call, and `query()`
runs a scripted async turn against them.
"""

import asyncio
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from aish import tools
from aish.agent import STOP_GATE_REFUSAL
from aish.approval import Denied
from aish.claude_max import ClaudeMaxAgent


class FakeSDK:
    """Just enough of claude_agent_sdk for ClaudeMaxAgent: tool registration,
    server construction, options, and a scripted query() async generator."""

    class StreamEvent:
        def __init__(self, event=None):
            self.event = event or {}

    class SystemMessage:
        def __init__(self, subtype="", data=None):
            self.subtype = subtype
            self.data = data or {}

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        def __init__(self, result="done", session_id="sess-1"):
            self.result = result
            self.session_id = session_id
            self.duration_ms = 5
            self.usage = {}
            self.total_cost_usd = None

    def __init__(self):
        self.handlers = {}  # tool name -> async handler (what the SDK would call)
        self.servers = []
        self.queries = []  # (prompt, options) per task
        self.scripts = []  # per-task `async def script(sdk) -> str` turn drivers

    def tool(self, name, description, schema):
        def deco(fn):
            self.handlers[name] = fn
            return SimpleNamespace(
                name=name, description=description, schema=schema, handler=fn
            )

        return deco

    def create_sdk_mcp_server(self, name, version, sdk_tools):
        server = SimpleNamespace(name=name, version=version, tools=sdk_tools)
        self.servers.append(server)
        return server

    def ClaudeAgentOptions(self, **kwargs):  # noqa: N802 — mirrors the SDK class name
        return SimpleNamespace(**kwargs)

    async def call(self, name, args):
        """Invoke a registered handler the way the SDK does; unwrap the MCP
        content envelope to the result text."""
        reply = await self.handlers[name](args)
        return reply["content"][0]["text"]

    def query(self, prompt, options):
        self.queries.append((prompt, options))
        script = self.scripts.pop(0) if self.scripts else None
        sdk = self

        async def gen():
            text = "done"
            if script is not None:
                text = await script(sdk)
            yield sdk.ResultMessage(result=text)

        return gen()


def make_max_agent(monkeypatch, tmp_path, **kwargs):
    fake = FakeSDK()
    monkeypatch.setattr(ClaudeMaxAgent, "_load_sdk", staticmethod(lambda: fake))
    kwargs.setdefault("cwd", str(tmp_path))
    kwargs.setdefault("approve", lambda _cmd: True)
    agent = ClaudeMaxAgent(model="fake", **kwargs)
    return agent, fake


def write_plugin_tool(project_dir, name, *, mutating):
    """A minimal valid TOOL.md + cat wrapper under <project>/.aish/tools/.

    Project scope is off by default (#178 P0-1), so callers must also request
    the `project_scope` fixture or discovery will simply find nothing.
    """
    tool_dir = project_dir / ".aish" / "tools" / name
    tool_dir.mkdir(parents=True)
    (tool_dir / "TOOL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: test plugin tool\n"
        "exec: ./run.sh\n"
        f"mutating: {'yes' if mutating else 'no'}\n"
        'schema: {"text": {"type": "string", "required": true}}\n'
        "---\nbody\n"
    )
    script = tool_dir / "run.sh"
    script.write_text("#!/bin/sh\ncat\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestSingleExecutionPath:
    def test_every_registered_tool_routes_through_inner_dispatch(
        self, monkeypatch, tmp_path
    ):
        """The SDK handlers have no execution path of their own: each one must
        bottom out in inner._dispatch — the single gated execution point."""
        agent, fake = make_max_agent(monkeypatch, tmp_path)
        seen = []

        def record(name, args):
            seen.append(name)
            return f"via-dispatch:{name}"

        agent.inner._dispatch = record
        assert set(fake.handlers) == set(agent._tool_names)
        native = {schema["function"]["name"] for schema in tools.TOOL_SCHEMAS}
        assert native <= set(fake.handlers)  # every native tool is registered
        for name in sorted(fake.handlers):
            out = asyncio.run(fake.call(name, {}))
            assert out == f"via-dispatch:{name}"
        assert sorted(seen) == sorted(fake.handlers)


class TestStopGateReset:
    def test_denial_with_comment_arms_gate_and_next_task_clears_it(
        self, monkeypatch, tmp_path
    ):
        """The exact #178 P0-4 wedge: under claude-max the inner run_task never
        runs, so a Denied(comment) used to arm the stop gate forever. Within a
        task the gate must hold; the NEXT task must start un-gated."""
        verdicts = [Denied("do not touch prod")]
        agent, fake = make_max_agent(
            monkeypatch,
            tmp_path,
            approve=lambda _cmd: verdicts.pop(0) if verdicts else True,
        )

        out1 = asyncio.run(fake.call("run_command", {"command": "echo hi"}))
        assert "do not touch prod" in out1
        assert agent.inner._pending_comment_response is True

        # Same task: every further tool call is refused (deny means stop) —
        # including read-only tools, not just run_command.
        out2 = asyncio.run(fake.call("run_command", {"command": "echo again"}))
        assert out2 == STOP_GATE_REFUSAL
        out3 = asyncio.run(fake.call("read_file", {"path": "x"}))
        assert out3 == STOP_GATE_REFUSAL

        # New task: run_task resets the inner per-task state, so the first
        # tool call of the next task runs instead of being refused.
        ran = []

        async def script(sdk):
            text = await sdk.call("run_command", {"command": "echo fresh"})
            ran.append(text)
            return "finished"

        fake.scripts.append(script)
        assert agent.run_task("next task") == "finished"
        assert ran and ran[0] != STOP_GATE_REFUSAL
        assert "fresh" in ran[0]
        assert agent.inner._pending_comment_response is False

    def test_run_task_resets_all_per_task_inner_state(self, monkeypatch, tmp_path):
        agent, fake = make_max_agent(monkeypatch, tmp_path)
        agent.inner._pending_comment_response = True
        agent.inner._pending_skill_reads = {"some-skill": 2}
        agent.inner._run_meta = {"command": "stale", "decision": "denied"}
        agent.inner.task_sources = [{"url": "https://stale.example"}]
        agent.inner._cancel.set()

        assert agent.run_task("anything") == "done"
        assert agent.inner._pending_comment_response is False
        assert agent.inner._pending_skill_reads == {}
        assert agent.inner._run_meta is None
        assert agent.inner.task_sources == []
        assert not agent.inner._cancel.is_set()


class TestConstructorWiring:
    def test_unknown_kwarg_raises_type_error(self):
        """No silent **kwargs sink: a capability the wrapper doesn't wire must
        fail loudly at construction, not no-op for claude-max sessions only."""
        with pytest.raises(TypeError):
            ClaudeMaxAgent(model="fake", bogus_capability=lambda: None)

    def test_capability_callbacks_reach_inner_agent(
        self, monkeypatch, tmp_path, project_scope
    ):
        steps = []
        commands = []
        tool_approvals = []
        step_sink = steps.append
        command_sink = commands.append

        def approve_tool(name, args, preview=None):
            tool_approvals.append(name)
            return True

        def approve_import(*args, **kwargs):
            return False

        write_plugin_tool(tmp_path, "marker", mutating=True)
        agent, fake = make_max_agent(
            monkeypatch,
            tmp_path,
            approve_tool=approve_tool,
            approve_import=approve_import,
            step_log=step_sink,
            command_log=command_sink,
        )
        assert agent.inner.approve_tool is approve_tool
        assert agent.inner.approve_import is approve_import
        assert agent.inner.step_log is step_sink
        assert agent.inner.command_log is command_sink

        # The mutating plugin tool registered on the SDK server (it is only
        # exposed because a tool approver is wired) and its call runs through
        # the approval gate.
        assert "marker" in fake.handlers
        out = asyncio.run(fake.call("marker", {"text": "ping"}))
        assert tool_approvals == ["marker"]
        assert "ping" in out  # the cat wrapper echoed the validated args back

        # step_log received the trace for the plugin call: tool_start + tool.
        kinds = [(s.get("kind"), s.get("name")) for s in steps]
        assert ("tool_start", "marker") in kinds
        assert ("tool", "marker") in kinds

        # command_log received the terminal-block framing for run_command.
        asyncio.run(fake.call("run_command", {"command": "echo traced"}))
        assert [c["kind"] for c in commands] == ["cmd_start", "cmd_end"]
        assert commands[0]["command"] == "echo traced"

    def test_origin_reaches_inner_agent_and_gates_knowledge_writes(
        self, monkeypatch, tmp_path
    ):
        """The server has always passed origin= in `common`, but the wrapper had
        no such parameter — so every claude-max web session died with a
        TypeError, and once constructible the origin-scoped gates (#178 P0-2
        egress, #196 knowledge writes) would have been dead on this backend
        because they all live in the inner Agent's _dispatch."""
        agent, fake = make_max_agent(monkeypatch, tmp_path, origin="email")
        assert agent.inner.origin == "email"
        out = asyncio.run(fake.call("forget_memory", {"name": "stale"}))
        assert out.startswith("NOT EXECUTED")
        # ...and an attended claude-max session still writes without a card.
        attended, fake2 = make_max_agent(monkeypatch, tmp_path)
        assert attended.inner.origin == "user"
        assert "remembered" in asyncio.run(fake2.call("remember", {"note": "a fact"}))

    def test_mutating_plugin_tool_hidden_without_tool_approver(
        self, monkeypatch, tmp_path, project_scope
    ):
        write_plugin_tool(tmp_path, "gated", mutating=True)
        agent, fake = make_max_agent(monkeypatch, tmp_path)
        assert "gated" not in fake.handlers  # fail-closed: never offered ungated

    def test_plugin_tool_created_mid_session_registers_next_task(
        self, monkeypatch, tmp_path, project_scope
    ):
        agent, fake = make_max_agent(monkeypatch, tmp_path)
        assert "latecomer" not in fake.handlers
        write_plugin_tool(tmp_path, "latecomer", mutating=False)
        assert agent.run_task("rescan") == "done"  # default no-tool-call script
        assert "latecomer" in fake.handlers


class TestHandlerRobustness:
    def test_handler_exception_returns_tool_error(self, monkeypatch, tmp_path):
        """A tool bug becomes an ERROR result for the model, never an exception
        propagating into the SDK loop."""
        agent, fake = make_max_agent(monkeypatch, tmp_path)

        def boom(name, args):
            raise RuntimeError("boom")

        agent.inner._dispatch = boom
        out = asyncio.run(fake.call("run_command", {"command": "x"}))
        assert out.startswith("ERROR: tool 'run_command' failed internally")

    def test_concurrent_handler_calls_serialize(self, monkeypatch, tmp_path):
        """SDK handlers may run concurrently; the dispatch lock must keep the
        non-thread-safe inner Agent single-file — never two calls inside."""
        agent, fake = make_max_agent(monkeypatch, tmp_path)
        state = {"active": 0, "max": 0}
        guard = threading.Lock()

        def slow_dispatch(name, args):
            with guard:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.05)
            with guard:
                state["active"] -= 1
            return "ok"

        agent.inner._dispatch = slow_dispatch

        async def main():
            await asyncio.gather(
                fake.handlers["run_command"]({"command": "a"}),
                fake.handlers["web_search"]({"query": "b"}),
                fake.handlers["read_docs"]({"command": "c"}),
            )

        asyncio.run(main())
        assert state["max"] == 1
