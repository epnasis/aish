"""ClaudeMaxAgent tests with a scripted fake SDK — no claude CLI, no
claude-agent-sdk package, no network.

Mirrors the FakeChat pattern from test_agent.py: the SDK seam is
`ClaudeMaxAgent._load_sdk` (the one lazy import site), monkeypatched to return
a FakeSDK. The fake captures the MCP tool handlers ClaudeMaxAgent registers,
so tests drive the exact coroutines the real SDK would call, and `query()`
runs a scripted async turn against them.
"""

import asyncio
import json
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

    class ToolUseBlock:
        """What tells a delivery it was narration on the message that produced
        it, rather than one message later (#212)."""

        def __init__(self, name):
            self.name = name

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
        self.streams = []  # per-task messages yielded BEFORE the result

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

        stream = self.streams.pop(0) if self.streams else []

        async def gen():
            for message in stream:
                # A callable stands for "the SDK now runs the tool handlers of
                # the message just yielded" — the only way to interleave a
                # batch of tool calls BETWEEN two assistant messages, which is
                # what a turn boundary actually looks like here.
                if callable(message):
                    await message(sdk)
                    continue
                yield message
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
        "returns: text\n"
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

    def test_the_browse_tab_is_findable_through_the_wrapper(
        self, monkeypatch, tmp_path, project_scope
    ):
        """The inner Agent dispatches `browse`, so its `BrowseView` holds the
        tab this chat drives — and the live watch (#289 slice 2) reads that key
        off `session.agent`. Without the delegation, watching a claude-max chat
        raises an AttributeError the watcher swallows as "stop watching", which
        is a feature quietly missing for one backend: exactly the failure the
        no-**kwargs-sink rule above exists to prevent."""
        agent, _ = make_max_agent(monkeypatch, tmp_path)
        assert agent.browse_key == agent.inner.browse_key
        assert agent.browse_key

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


class TestBackendSizedCaps:
    """#192 sizes plugin-output truncation from the provider actually in use.
    The wrapper's `provider` is a class attribute; the INNER agent — which is
    where truncation is computed, via _locked_dispatch → _dispatch — defaulted
    to "ollama", so a Claude session would have been capped as if it were a 32k
    local one."""

    def test_inner_agent_knows_it_is_on_claude_max(self, monkeypatch, tmp_path):
        agent, _ = make_max_agent(monkeypatch, tmp_path)
        assert agent.inner.provider == "claude-max"

    def test_caps_are_not_the_ollama_default(self, monkeypatch, tmp_path):
        from aish import backends, tool_plugins

        agent, _ = make_max_agent(monkeypatch, tmp_path)
        caps, source = agent.inner._output_caps()
        window, _ = backends.context_window("ollama", agent.inner.num_ctx)
        assert source == "backend:claude-max:200000"
        assert sum(caps) > sum(tool_plugins.output_caps(window))


class TestNarration:
    """#212 on the backend that owns its own loop.

    The SDK reports each assistant message as it lands, and a LATER one is what
    proves the previous was not the answer — so a delivery is closed one
    message late rather than guessed at from the presence of tool_use blocks.
    Without this the narration streamed live and was never recorded, so a cold
    reload showed a chat that had said four things saying one.
    """

    def _run(self, monkeypatch, tmp_path, texts, **kwargs):
        delivered: list[str] = []
        agent, fake = make_max_agent(
            monkeypatch, tmp_path, on_delivered=delivered.append, **kwargs
        )
        fake.streams.append(
            [FakeSDK.AssistantMessage([FakeSDK.TextBlock(t)]) for t in texts]
        )

        async def script(_sdk):
            return texts[-1]

        fake.scripts.append(script)
        result = agent.run_task("what does it look like?")
        return agent, delivered, result

    def test_only_the_opening_acknowledgement_is_delivered(
        self, monkeypatch, tmp_path
    ):
        """Same cap as the native loop: one per task, not one per message."""
        agent, delivered, result = self._run(
            monkeypatch, tmp_path,
            ["Let me search.", "There are leaks — digging in.", "It folds."],
        )
        assert delivered == ["Let me search."]
        assert result == "It folds."

    def test_narration_is_recorded_so_a_cold_reload_replays_it(
        self, monkeypatch, tmp_path
    ):
        logged: list[dict] = []
        agent, _delivered, _ = self._run(
            monkeypatch, tmp_path,
            ["Let me search.", "It folds."],
            on_message=logged.append,
        )
        said = [m["content"] for m in logged if m.get("role") == "assistant"]
        assert said == ["Let me search.", "It folds."], (
            "an interim delivery never reached the log, so it exists only in a "
            "token stream nobody kept"
        )

    def test_the_deliverable_verify_grades_is_the_whole_turn(
        self, monkeypatch, tmp_path
    ):
        agent, _delivered, _ = self._run(
            monkeypatch, tmp_path, ["Let me search.", "It folds."]
        )
        assert agent.inner._delivered == ["Let me search."]

    def test_a_single_message_turn_delivers_nothing(self, monkeypatch, tmp_path):
        agent, delivered, result = self._run(monkeypatch, tmp_path, ["It folds."])
        assert delivered == []
        assert result == "It folds."


class TestNarrationOrdering:
    """The SDK reports partial messages for message N+1 AFTER message N's
    complete AssistantMessage lands. So closing a delivery when the NEXT
    assistant message arrives is one message too late: the next message's
    tokens stream into the bubble the previous delivery was still holding, and
    `done` then paints the answer a second time. A delivery closes on the
    message that produced it."""

    def _drive(self, monkeypatch, tmp_path, texts, **kwargs):
        """One AssistantMessage per text, each but the last carrying a
        tool_use block — the shape a narrating agentic turn actually has."""
        events, delivered = [], []
        agent, fake = make_max_agent(
            monkeypatch, tmp_path,
            on_token=lambda t: events.append(("token", t)),
            on_delivered=lambda t: (delivered.append(t), events.append(("delivery", t))),
            **kwargs,
        )
        stream = []
        for i, text in enumerate(texts):
            last = i == len(texts) - 1
            # Partials first, then the complete message — the SDK's order.
            stream.append(_text_delta(text))
            blocks = [FakeSDK.TextBlock(text)]
            if not last:
                blocks.append(FakeSDK.ToolUseBlock("web_search"))
            stream.append(FakeSDK.AssistantMessage(blocks))
        fake.streams.append(stream)

        async def script(_sdk):
            return texts[-1]

        fake.scripts.append(script)
        result = agent.run_task("what does it look like?")
        return agent, delivered, events, result, texts

    def test_a_delivery_closes_before_the_next_message_streams(
        self, monkeypatch, tmp_path
    ):
        _agent, delivered, events, result, texts = self._drive(
            monkeypatch, tmp_path,
            ["Let me search.", "There are leaks — digging in.", "It folds."],
        )
        assert delivered == ["Let me search."]
        assert result == "It folds."
        # The load-bearing assertion: every delivery lands BEFORE the tokens of
        # the message that follows it. Reversed, those tokens append to the
        # bubble the delivery was about to close, and `done` repaints the
        # answer as a second bubble.
        # Matched on the (kind, text) PAIR, not the text: a delivery's own
        # tokens stream before it, so looking the text up by value finds the
        # token event and the assertion passes against any ordering at all.
        seen = [(kind, text) for kind, text in events if text.strip()]
        for i, text in enumerate(delivered):
            closed = seen.index(("delivery", text))
            next_streamed = seen.index(("token", texts[i + 1]))
            assert closed < next_streamed, (
                f"delivery {text!r} closed after the next message had streamed: "
                f"its tokens append to the bubble this delivery was holding, "
                f"and `done` then paints the answer twice"
            )

    def test_the_answer_is_never_delivered_as_narration(self, monkeypatch, tmp_path):
        _agent, delivered, _events, result, _texts = self._drive(
            monkeypatch, tmp_path, ["Let me search.", "It folds."]
        )
        assert result == "It folds."
        assert "It folds." not in delivered


class TestApprovalIntentOnTheSdkPath:
    """#252 on the backend that owns its own loop. The chat's one-narration cap
    is the same here, so the gate must get its copy on the same terms: taken
    from the message that ACTED, before the cap can drop it, and cleared by a
    message that acts without saying anything."""

    def _drive(self, monkeypatch, tmp_path, messages):
        """`messages` is a list of (text, acted) — `acted` meaning the message
        carried a tool_use block, which is what makes its words narration."""
        noted, delivered = [], []
        agent, fake = make_max_agent(
            monkeypatch, tmp_path, on_delivered=delivered.append
        )
        real = agent.inner.note_intent

        def spy(said):
            noted.append((said or "").strip())
            real(said)

        agent.inner.note_intent = spy
        stream = []
        for text, acted in messages:
            blocks = [FakeSDK.TextBlock(text)] if text else []
            if acted:
                blocks.append(FakeSDK.ToolUseBlock("web_search"))
            stream.append(FakeSDK.AssistantMessage(blocks))
        fake.streams.append(stream)

        async def script(_sdk):
            return messages[-1][0]

        fake.scripts.append(script)
        agent.run_task("what does it look like?")
        return agent, noted, delivered

    def test_the_step_the_chat_dropped_still_reaches_the_gate(
        self, monkeypatch, tmp_path
    ):
        agent, noted, delivered = self._drive(
            monkeypatch, tmp_path,
            [
                ("Let me search.", True),
                ("There are leaks — digging in.", True),
                ("It folds.", False),
            ],
        )
        # The cap is untouched: the owner is told once, as before.
        assert delivered == ["Let me search."]
        # But the gate is handed BOTH — including the second, which is the one
        # the owner would otherwise have had to reverse-engineer.
        assert noted == ["Let me search.", "There are leaks — digging in."]
        assert agent.inner.turn_intent() == "There are leaks — digging in."

    def test_the_approvers_can_actually_read_it_off_this_wrapper(
        self, monkeypatch, tmp_path
    ):
        """The gate reads `turn_intent()` off whichever agent the session holds,
        and this class delegates to the inner Agent BY HAND — no __getattr__.
        A missing delegate is not a card with no reason on it; it is an
        AttributeError raised inside the approver on every approval this
        backend shows."""
        agent, _fake = make_max_agent(monkeypatch, tmp_path)
        assert agent.turn_intent() == ""
        agent.inner.note_intent("  checking the account balance  ")
        assert agent.turn_intent() == "checking the account balance"

    def test_a_wordless_action_clears_the_previous_reason(
        self, monkeypatch, tmp_path
    ):
        """The SDK gives no per-response boundary to assign on, so the clear
        rides the emptied buffer: an acting message that said nothing notes an
        empty intent, matching the native loop's self-clearing assignment."""
        agent, noted, _delivered = self._drive(
            monkeypatch, tmp_path,
            [("Let me search.", True), ("", True), ("It folds.", False)],
        )
        assert noted == ["Let me search.", ""]
        assert agent.inner.turn_intent() == ""


def _text_delta(text):
    return FakeSDK.StreamEvent({
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": text},
    })


class TestDeliveriesAreNotFinalAnswers:
    """This path logs no tool-role records — the SDK's tool calls leave trace
    steps — so the adjacency rule every "was this the answer?" reader uses is
    blind here. Each narration line counted as a final answer, walking the fork
    ordinal off by one per narrated turn."""

    def test_an_interim_record_is_stamped(self, monkeypatch, tmp_path):
        logged: list[dict] = []
        agent, fake = make_max_agent(
            monkeypatch, tmp_path, on_message=logged.append
        )
        fake.streams.append([
            FakeSDK.AssistantMessage([FakeSDK.TextBlock("narrating")]),
            FakeSDK.AssistantMessage([FakeSDK.TextBlock("the answer")]),
        ])

        async def script(_sdk):
            return "the answer"

        fake.scripts.append(script)
        agent.run_task("go")

        said = [m for m in logged if m.get("role") == "assistant"]
        assert [(m["content"], m.get("interim", False)) for m in said] == [
            ("narrating", True), ("the answer", False),
        ]

    def test_fork_and_export_skip_the_narration(self, monkeypatch, tmp_path):
        from aish.export import session_answers
        from aish.session import SessionLog

        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "first"})
        log.message({"role": "assistant", "content": "narrating", "interim": True})
        log.message({"role": "assistant", "content": "answer one"})
        log.message({"role": "user", "content": "second"})
        log.message({"role": "assistant", "content": "answer two"})

        text = log.path.read_text(encoding="utf-8")
        # The UI counts answer bubbles, and narration gets none — so "the 2nd
        # answer" must be `answer two`, not `answer one`.
        assert "answer two" in SessionLog.truncate_at_answer(text, 2)
        assert SessionLog.truncate_at_answer(text, 3) is None
        assert session_answers(
            [json.loads(line) for line in text.splitlines()]
        ) == ["answer one", "answer two"]


class TestProvenanceConformance:
    """#311. `_note_provenance` was reached only from `_execute_tool_calls`,
    which is the native loop's function and nothing else's — so on claude-max
    the task looked untainted for its entire life however much of the open web
    it read, and a link that arrived by mail was not recognised as having done
    so. Both loops are driven over the SAME tool results here and must produce
    the same three records: a claude-max-only test would have to be remembered
    again for the next backend, which is the failure this issue IS.
    """

    HOSTILE = "https://blog.example/post"
    OFFERED = "https://blog.example/offer/42"
    PROBE = "https://probe.example/p"
    RESET_LINK = "https://eon.test/r?t=abc123"
    MAIL = [
        {
            "from": "noreply@eon.pl",
            "subject": "Resetowanie hasla",
            "body": f"Kliknij, aby zresetowac haslo: {RESET_LINK}",
        }
    ]

    def _mail_tool(self):
        """A read-only plugin tool declaring its output is e-mail. conftest
        points GLOBAL_TOOLS_DIR at a temp dir suite-wide, so real discovery
        finds it on both backends — including through the SDK MCP server."""
        from aish import tool_plugins

        tool_dir = tool_plugins.GLOBAL_TOOLS_DIR / "mail_search"
        tool_dir.mkdir(parents=True, exist_ok=True)
        (tool_dir / "TOOL.md").write_text(
            "---\nname: mail_search\ndescription: search mail\n"
            "exec: ./wrapper\nmutating: no\nreturns: text\n"
            "content_from: email\n"
            'schema: {"q": {"type": "string"}}\n---\nbody\n',
            encoding="utf-8",
        )
        wrapper = tool_dir / "wrapper"
        wrapper.write_text(
            "#!/bin/sh\ncat <<'EOF'\n"
            + json.dumps(self.MAIL, ensure_ascii=False)
            + "\nEOF\n",
            encoding="utf-8",
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)

    def _probe(self, monkeypatch, holder):
        """Snapshot the three records AT THE MOMENT the second batch runs —
        which is where they matter, since every gate that reads them runs
        inside a dispatch. Comparing after the run would be the weaker claim."""
        import aish.agent as agent_module

        seen: list[tuple] = []

        def read_url(url, topic=None, **_kw):
            if url == self.PROBE:
                inner = holder["agent"]
                seen.append(
                    (
                        inner._tainted,
                        sorted(inner._offered_links),
                        dict(inner._mail_links),
                    )
                )
                return f"{agent_module.web.UNTRUSTED_NOTE}[{url}]\nnothing here"
            return (
                f"{agent_module.web.UNTRUSTED_NOTE}[{url}]\n"
                f"a listing linking to {self.OFFERED}"
            )

        monkeypatch.setattr(agent_module.web, "read_url", read_url)
        return seen

    def _native(self, monkeypatch):
        from tests.test_agent import make_agent, model_says, tool_call

        holder: dict = {}
        seen = self._probe(monkeypatch, holder)
        agent, _ = make_agent(
            [
                model_says(tool_calls=[
                    tool_call("mail_search", q="invoices"),
                    tool_call("read_url", url=self.HOSTILE),
                ]),
                model_says(tool_calls=[tool_call("read_url", url=self.PROBE)]),
                model_says("done"),
            ],
            approve_tool=lambda *_a, **_k: True,
        )
        holder["agent"] = agent
        agent.run_task("check my mail and read the post")
        return seen

    def _acting(self, fake, name):
        return fake.AssistantMessage(
            [fake.TextBlock("working"), fake.ToolUseBlock(name)]
        )

    def _batch(self, *calls, into=None):
        async def run(sdk):
            for name, args in calls:
                result = await sdk.call(name, args)
                if into is not None:
                    into.append(result)

        return run

    def _claude_max(self, monkeypatch, tmp_path):
        holder: dict = {}
        seen = self._probe(monkeypatch, holder)
        agent, fake = make_max_agent(
            monkeypatch, tmp_path, approve_tool=lambda *_a, **_k: True
        )
        holder["agent"] = agent.inner
        fake.streams.append([
            self._acting(fake, "mail_search"),
            self._batch(
                ("mail_search", {"q": "invoices"}),
                ("read_url", {"url": self.HOSTILE}),
            ),
            self._acting(fake, "read_url"),
            self._batch(("read_url", {"url": self.PROBE})),
        ])
        agent.run_task("check my mail and read the post")
        return seen

    def test_both_backends_record_the_same_provenance(self, monkeypatch, tmp_path):
        from aish import provenance

        self._mail_tool()
        native = self._native(monkeypatch)
        max_side = self._claude_max(monkeypatch, tmp_path)
        expected = [(True, [self.OFFERED], {self.RESET_LINK: provenance.SIGN_IN})]
        # Asserted against a LITERAL, not only against each other: two backends
        # that both record nothing would agree perfectly.
        assert native == expected
        assert max_side == expected

    def test_the_sdk_path_gates_an_emailed_sign_in_link(self, monkeypatch, tmp_path):
        """The consequence, end to end on the backend that lacked it: the mail
        arrived through the SDK's own tool handler, and the reset link it
        carried is refused outright instead of followed."""
        self._mail_tool()
        holder: dict = {}
        self._probe(monkeypatch, holder)
        agent, fake = make_max_agent(
            monkeypatch, tmp_path, approve_tool=lambda *_a, **_k: True
        )
        holder["agent"] = agent.inner
        followed: list[str] = []
        fake.streams.append([
            self._acting(fake, "mail_search"),
            self._batch(("mail_search", {"q": "invoices"})),
            self._acting(fake, "read_url"),
            self._batch(("read_url", {"url": self.RESET_LINK}), into=followed),
        ])
        agent.run_task("check my mail")
        assert "NOT EXECUTED" in followed[-1]
        assert self.RESET_LINK in followed[-1]


class TestSentCoverage:
    """The one path the `sent` seam does not cover (#352, #242): the SDK spawns
    the `claude` CLI and the CLI issues the requests, so the turn says ONCE
    that this backend records none of them."""

    def test_each_task_writes_one_coverage_marker_and_no_manifest(self, monkeypatch, tmp_path):
        steps = []
        agent, fake = make_max_agent(monkeypatch, tmp_path, step_log=steps.append)
        agent.run_task("first")
        agent.run_task("second")
        sent = [s for s in steps if s.get("kind") == "sent"]
        assert [s["coverage"] for s in sent] == ["sdk", "sdk"]
        assert [s["turn"] for s in sent] == [1, 2]
        assert all(s["provider"] == "claude-max" for s in sent)
        assert all("model_call" not in s and "messages" not in s for s in sent)
