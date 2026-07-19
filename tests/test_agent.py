"""Agent-loop tests using a scripted fake in place of ollama.chat.

FakeChat returns pre-scripted responses shaped like the ollama library's
(message with .content / .tool_calls), so we can test the loop and the
approval gate with no model, no network, and full determinism.
"""

from types import SimpleNamespace

import pytest

from aish.agent import DENIED_RESULT, Agent


def tool_call(name: str, **arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def model_says(content: str = "", tool_calls: list | None = None, tokens: tuple | None = None):
    response = SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls or None)
    )
    if tokens:
        response.prompt_eval_count, response.eval_count = tokens
    return response


class FakeChat:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def make_agent(responses, approve=lambda _cmd: True, **kwargs):
    chat = FakeChat(responses)
    agent = Agent(model="fake", approve=approve, client_chat=chat, **kwargs)
    return agent, chat


def tool_messages(messages):
    return [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]


class TestApprovalGate:
    def test_approved_command_runs(self):
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="echo hi")]),
                model_says("done"),
            ]
        )
        assert agent.run_task("say hi") == "done"
        tool_results = tool_messages(agent.messages)
        assert len(tool_results) == 1
        assert "hi" in tool_results[0]["content"]

    def test_denied_command_never_executes(self, tmp_path):
        """The proof: a denied command with an observable side effect leaves no trace."""
        marker = tmp_path / "pwned"
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
                model_says("ok, not running it"),
            ],
            approve=lambda _cmd: False,
        )
        agent.run_task("touch a file")
        assert not marker.exists()
        assert tool_messages(agent.messages)[0]["content"] == DENIED_RESULT

    def test_approver_sees_exact_command(self):
        seen = []
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="uname -a")]),
                model_says("done"),
            ],
            approve=lambda cmd: (seen.append(cmd), True)[1],
        )
        agent.run_task("what OS?")
        assert seen == ["uname -a"]

    def test_read_docs_does_not_ask_approval(self):
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="ls")]),
                model_says("done"),
            ],
            approve=lambda _cmd: pytest.fail("read_docs must not hit the approval gate"),
        )
        assert agent.run_task("check ls docs") == "done"


class TestLoop:
    def test_plain_text_response_ends_task(self):
        agent, chat = make_agent([model_says("just an answer")])
        assert agent.run_task("hello") == "just an answer"
        assert len(chat.calls) == 1

    def test_tool_result_fed_back_to_model(self):
        agent, chat = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="echo marker42")]),
                model_says("done"),
            ]
        )
        agent.run_task("run it")
        tool_msgs = tool_messages(chat.calls[1]["messages"])
        assert any("marker42" in m["content"] for m in tool_msgs)

    def test_max_steps_stops_runaway_loop(self):
        endless = model_says(tool_calls=[tool_call("read_docs", command="ls")])
        agent, chat = make_agent([endless] * 10, max_steps=3)
        result = agent.run_task("loop forever")
        assert "max-steps" in result
        # 3 budgeted turns + 1 no-tools wrap-up turn; its tool calls never run
        assert len(chat.calls) == 4

    def test_unknown_tool_reported_not_crashed(self):
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("format_disk", disk="/dev/disk0")]),
                model_says("done"),
            ]
        )
        assert agent.run_task("hack") == "done"
        assert "unknown tool" in tool_messages(agent.messages)[0]["content"]

    def test_system_prompt_is_first_message(self):
        agent, chat = make_agent([model_says("hi")])
        agent.run_task("hi")
        first = chat.calls[0]["messages"][0]
        assert first["role"] == "system"
        assert "read_docs" in first["content"]


class TestContextCompaction:
    def big_output_agent(self, responses, monkeypatch, **kwargs):
        import aish.agent as agent_module

        monkeypatch.setattr(agent_module.tools, "run_command", lambda cmd, **_kw: "X" * 5000)
        return make_agent(responses, **kwargs)

    def test_previous_task_tool_output_trimmed_on_new_task(self, monkeypatch):
        agent, _ = self.big_output_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="big")]),
                model_says("task 1 done"),
                model_says("task 2 done"),
            ],
            monkeypatch,
        )
        agent.run_task("first")
        assert len(tool_messages(agent.messages)[0]["content"]) == 5000
        agent.run_task("second")
        old = tool_messages(agent.messages)[0]["content"]
        assert "[trimmed" in old
        assert len(old) < 300

    def test_system_prompt_never_trimmed(self, monkeypatch):
        agent, _ = self.big_output_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="big")]),
                model_says("done"),
                model_says("done again"),
            ],
            monkeypatch,
        )
        agent.run_task("first")
        agent.run_task("second")
        assert "read_docs" in agent.messages[0]["content"]

    def test_budget_trims_oldest_within_task_keeps_recent_two(self, monkeypatch):
        run = model_says(tool_calls=[tool_call("run_command", command="big")])
        agent, _ = self.big_output_agent(
            [run, run, run, run, model_says("done")],
            monkeypatch,
            num_ctx=100,  # tiny budget: forces trimming mid-task
        )
        agent.run_task("lots of output")
        contents = [m["content"] for m in tool_messages(agent.messages)]
        assert len(contents) == 4
        assert "[trimmed" in contents[0]
        assert "[trimmed" in contents[1]
        assert contents[2] == "X" * 5000
        assert contents[3] == "X" * 5000

    def test_topic_passed_through_to_read_docs(self, monkeypatch):
        import aish.agent as agent_module

        seen = {}

        def fake_read_docs(command, topic=None):
            seen.update(command=command, topic=topic)
            return "docs"

        monkeypatch.setattr(agent_module.tools, "read_docs", fake_read_docs)
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="find", topic="maxdepth")]),
                model_says("done"),
            ]
        )
        agent.run_task("check find docs")
        assert seen == {"command": "find", "topic": "maxdepth"}


def test_tool_exception_becomes_result_not_crash(monkeypatch):
    """Regression: an exception inside a tool must not kill the session."""
    import aish.agent as agent_module

    def boom(cmd, **_kw):
        raise UnicodeDecodeError("utf-8", b"\xdf", 0, 1, "invalid continuation byte")

    monkeypatch.setattr(agent_module.tools, "run_command", boom)
    agent, _ = make_agent(
        [
            model_says(tool_calls=[tool_call("run_command", command="cat binary.plist")]),
            model_says("recovered"),
        ]
    )
    assert agent.run_task("read the plist") == "recovered"
    assert "failed internally" in tool_messages(agent.messages)[0]["content"]


def test_read_file_range_passes_through_dispatch(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    echoes = []
    agent, _ = make_agent(
        [
            model_says(tool_calls=[tool_call("read_file", path=str(f), offset=2, limit=1)]),
            model_says("done"),
        ],
        echo=echoes.append,
    )
    assert agent.run_task("read part") == "done"
    content = tool_messages(agent.messages)[0]["content"]
    assert "2  beta" in content
    assert "alpha" not in content and "gamma" not in content.split("[")[0]
    assert any("(from line 2)" in e for e in echoes)


def test_missing_dependency_names_package_and_reinstall_fix(monkeypatch):
    """A ModuleNotFoundError means a broken install: the result must name the
    missing package, tell the model not to retry, and give the reinstall fix."""
    import aish.agent as agent_module

    def boom(query, **_kw):
        raise ModuleNotFoundError("No module named 'ddgs'", name="ddgs")

    monkeypatch.setattr(agent_module.web, "web_search", boom)
    agent, _ = make_agent(
        [
            model_says(tool_calls=[tool_call("web_search", query="latest news")]),
            model_says("told the user"),
        ]
    )
    assert agent.run_task("search the news") == "told the user"
    result = tool_messages(agent.messages)[0]["content"]
    assert "'ddgs'" in result
    assert "Do NOT retry" in result
    assert "uv tool install --force" in result


class TestParallelReadOnlyTools:
    def test_two_searches_run_concurrently_results_in_order(self, monkeypatch):
        """Both fakes block on a barrier that only opens when the two run at
        the same time — a sequential implementation times out and fails."""
        import threading

        import aish.agent as agent_module

        barrier = threading.Barrier(2)

        def fake_search(query, **_kw):
            barrier.wait(timeout=5)
            return f"results for {query}"

        monkeypatch.setattr(agent_module.web, "web_search", fake_search)
        agent, _ = make_agent(
            [
                model_says(tool_calls=[
                    tool_call("web_search", query="alpha"),
                    tool_call("web_search", query="beta"),
                ]),
                model_says("done"),
            ]
        )
        assert agent.run_task("search twice") == "done"
        contents = [m["content"] for m in tool_messages(agent.messages)]
        assert contents == ["results for alpha", "results for beta"]

    def test_mixed_turn_keeps_order_and_approval_still_gates(self, monkeypatch):
        """run_command in the same turn still goes through approve(); results
        land in the model's original call order."""
        import aish.agent as agent_module

        monkeypatch.setattr(
            agent_module.web, "web_search", lambda query, **_kw: f"results for {query}"
        )
        approved = []

        def approve(command):
            approved.append(command)
            return True

        agent, _ = make_agent(
            [
                model_says(tool_calls=[
                    tool_call("web_search", query="alpha"),
                    tool_call("run_command", command="echo hi"),
                    tool_call("web_search", query="beta"),
                ]),
                model_says("done"),
            ],
            approve=approve,
        )
        assert agent.run_task("research then run") == "done"
        assert approved == ["echo hi"]
        contents = [m["content"] for m in tool_messages(agent.messages)]
        assert contents[0] == "results for alpha"
        assert "hi" in contents[1]
        assert contents[2] == "results for beta"

    def test_one_failing_parallel_call_does_not_poison_the_other(self, monkeypatch):
        import aish.agent as agent_module

        def fake_search(query, **_kw):
            if query == "bad":
                raise RuntimeError("boom")
            return f"results for {query}"

        monkeypatch.setattr(agent_module.web, "web_search", fake_search)
        agent, _ = make_agent(
            [
                model_says(tool_calls=[
                    tool_call("web_search", query="bad"),
                    tool_call("web_search", query="good"),
                ]),
                model_says("done"),
            ]
        )
        assert agent.run_task("search twice") == "done"
        contents = [m["content"] for m in tool_messages(agent.messages)]
        assert "failed internally" in contents[0]
        assert contents[1] == "results for good"

    def test_parallel_calls_marked_overlapped_plus_summable_batch_line(self, monkeypatch):
        """Overlapped runtimes print as ⇉ detail; only the batch ✓ wall-time
        line counts toward ∑, so ✓ components always sum to the total."""
        import aish.agent as agent_module

        monkeypatch.setattr(agent_module.web, "web_search", lambda q, **_kw: "results")
        echoes = []
        agent, _ = make_agent(
            [
                model_says(tool_calls=[
                    tool_call("web_search", query="a"),
                    tool_call("web_search", query="b"),
                ]),
                model_says("done"),
            ],
            echo=echoes.append,
        )
        assert agent.run_task("search twice") == "done"
        assert sum(1 for e in echoes if e.startswith("⇉ web_search")) == 2
        assert any(e.startswith("✓ 2 parallel lookups") for e in echoes)


class FakeClock:
    """Deterministic stand-in for time.perf_counter: only advances on demand."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestElapsedTimeReporting:
    def patch_clock(self, monkeypatch):
        import aish.agent as agent_module

        clock = FakeClock()
        monkeypatch.setattr(agent_module, "time", SimpleNamespace(perf_counter=clock))
        return clock

    def test_slow_tool_gets_timing_line(self, monkeypatch):
        import aish.agent as agent_module

        clock = self.patch_clock(monkeypatch)

        def slow_search(query, **_kw):
            clock.advance(2.5)
            return "results"

        monkeypatch.setattr(agent_module.web, "web_search", slow_search)
        echoes = []
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("web_search", query="x")]),
                model_says("done"),
            ],
            echo=echoes.append,
        )
        assert agent.run_task("search") == "done"
        assert "✓ web_search 2.5s" in echoes

    def test_fast_tool_also_reports_time(self, monkeypatch):
        """No threshold: every tool call reports its duration, however quick."""
        import aish.agent as agent_module

        clock = self.patch_clock(monkeypatch)

        def quick_search(query, **_kw):
            clock.advance(0.4)
            return "results"

        monkeypatch.setattr(agent_module.web, "web_search", quick_search)
        echoes = []
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("web_search", query="x")]),
                model_says("done"),
            ],
            echo=echoes.append,
        )
        assert agent.run_task("search") == "done"
        assert "✓ web_search 0.4s" in echoes  # time only: token counts are
        # shown solely where Ollama reports real usage (model-turn lines)

    def test_slow_model_turns_report_thinking_and_answer(self, monkeypatch):
        import aish.agent as agent_module

        clock = self.patch_clock(monkeypatch)
        monkeypatch.setattr(agent_module.web, "web_search", lambda q, **_kw: "results")
        responses = [
            model_says(tool_calls=[tool_call("web_search", query="x")]),
            model_says("done"),
        ]

        def slow_chat(**_kwargs):
            clock.advance(3.0)
            return responses.pop(0)

        echoes = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=slow_chat, echo=echoes.append
        )
        assert agent.run_task("search") == "done"
        assert "✓ thought for 3.0s" in echoes
        assert any(e.startswith("✓ answered in 3.0s") for e in echoes)

    def test_format_secs(self):
        from aish.agent import format_secs

        assert format_secs(2.34) == "2.3s"
        assert format_secs(75) == "1m15s"

    def test_format_tokens(self):
        from aish.agent import format_tokens

        assert format_tokens(999) == "999"
        assert format_tokens(1234) == "1.2k"

    def test_answer_line_includes_task_total_and_tokens(self, monkeypatch):
        """Total spans the whole task — thinking + tools + answering — and
        token counts accumulate across every model turn."""
        import aish.agent as agent_module

        clock = self.patch_clock(monkeypatch)

        def slow_search(query, **_kw):
            clock.advance(2.0)
            return "results"

        monkeypatch.setattr(agent_module.web, "web_search", slow_search)
        responses = [
            model_says(tool_calls=[tool_call("web_search", query="x")], tokens=(1200, 100)),
            model_says("done", tokens=(2000, 250)),
        ]

        def slow_chat(**_kwargs):
            clock.advance(3.0)
            return responses.pop(0)

        echoes = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=slow_chat, echo=echoes.append
        )
        assert agent.run_task("go") == "done"
        assert "✓ thought for 3.0s · ↑ 1.2k ↓ 100 tokens" in echoes
        assert "✓ answered in 3.0s · ↑ 2.0k ↓ 250 tokens" in echoes
        # totals on their own line; components above sum exactly to them
        assert "∑ total 8.0s · ↑ 3.2k ↓ 350 tokens" in echoes  # 3s + 2s + 3s


class RecordingStatus:
    def __init__(self):
        self.events = []

    def start(self, label):
        self.events.append(("start", label))

    def add_tokens(self, count):
        self.events.append(("tokens", count))

    def stop(self):
        self.events.append(("stop",))


class TestLiveStatus:
    def test_model_turn_starts_thinking_timer_and_stops_before_first_token(self):
        events = []

        class Status:
            def start(self, label):
                events.append(("start", label))

            def add_tokens(self, count):
                events.append(("tokens", count))

            def stop(self):
                events.append(("stop",))

        def chat(stream=False, **_kwargs):
            assert stream is True
            return iter([model_says("hi")])

        agent = Agent(
            model="fake",
            approve=lambda _c: True,
            client_chat=chat,
            on_token=lambda t: events.append(("token", t)),
            status=Status(),
        )
        assert agent.run_task("hello") == "hi"
        assert events[0] == ("start", "thinking")
        first_stop = events.index(("stop",))
        first_token = next(i for i, e in enumerate(events) if e[0] == "token")
        assert first_stop < first_token

    def test_sequential_readonly_tool_gets_named_timer(self, monkeypatch):
        import aish.agent as agent_module

        monkeypatch.setattr(agent_module.web, "web_search", lambda q, **_kw: "results")
        status = RecordingStatus()
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("web_search", query="x")]),
                model_says("done"),
            ],
            status=status,
        )
        assert agent.run_task("search") == "done"
        assert ("start", "web_search") in status.events

    def test_parallel_lookups_get_batch_timer(self, monkeypatch):
        import aish.agent as agent_module

        monkeypatch.setattr(agent_module.web, "web_search", lambda q, **_kw: "results")
        status = RecordingStatus()
        agent, _ = make_agent(
            [
                model_says(tool_calls=[
                    tool_call("web_search", query="a"),
                    tool_call("web_search", query="b"),
                ]),
                model_says("done"),
            ],
            status=status,
        )
        assert agent.run_task("search twice") == "done"
        assert ("start", "2 parallel lookups") in status.events
        # every start is eventually stopped (prompts must never race the timer)
        assert status.events.count(("stop",)) >= sum(
            1 for e in status.events if e[0] == "start"
        )

    def test_streamed_chunks_feed_live_token_count(self):
        """Each streamed chunk bumps the ticker's token readout — including
        tool-call chunks, where nothing else is visible on screen."""
        events = []

        class Status:
            def start(self, label):
                events.append(("start", label))

            def add_tokens(self, count):
                events.append(("tokens", count))

            def stop(self):
                events.append(("stop",))

        turns = [
            [model_says(tool_calls=[tool_call("read_docs", command="ls")]) for _ in range(3)],
            [model_says("done")],
        ]

        def chat(stream=False, **_kwargs):
            return iter(turns.pop(0))

        agent = Agent(
            model="fake",
            approve=lambda _c: True,
            client_chat=chat,
            on_token=lambda _t: None,
            status=Status(),
        )
        assert agent.run_task("docs") == "done"
        assert events.count(("tokens", 1)) == 4  # 3 tool-call chunks + 1 answer chunk


class TestCwdAndCd:
    def test_bare_cd_changes_cwd_without_approval(self, tmp_path):
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command=f"cd {tmp_path}")]),
                model_says("done"),
            ],
            approve=lambda _cmd: pytest.fail("bare cd must not hit the approval gate"),
        )
        agent.run_task("go there")
        assert agent.cwd == str(tmp_path)
        assert "working directory is now" in tool_messages(agent.messages)[0]["content"]

    def test_relative_cd_resolves_against_agent_cwd(self, tmp_path):
        (tmp_path / "sub").mkdir()
        agent, _ = make_agent([model_says("hi")], cwd=str(tmp_path))
        assert "sub" in agent._change_dir("sub")
        assert agent.cwd == str(tmp_path / "sub")

    def test_cd_to_missing_dir_errors_and_keeps_cwd(self, tmp_path):
        agent, _ = make_agent([model_says("hi")], cwd=str(tmp_path))
        result = agent._change_dir("nope-xyz")
        assert result.startswith("ERROR")
        assert agent.cwd == str(tmp_path)

    def test_compound_cd_goes_through_approval(self, tmp_path):
        seen = []
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="cd /tmp && ls")]),
                model_says("done"),
            ],
            approve=lambda cmd: (seen.append(cmd), cmd)[1],
        )
        agent.run_task("list tmp")
        assert seen == ["cd /tmp && ls"]

    def test_commands_run_in_agent_cwd(self, tmp_path):
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="pwd")]),
                model_says("done"),
            ],
            cwd=str(tmp_path),
        )
        agent.run_task("where am I")
        assert tmp_path.name in tool_messages(agent.messages)[0]["content"]


class TestApproveContract:
    def test_edited_command_runs_and_is_noted(self):
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="echo wrong")]),
                model_says("done"),
            ],
            approve=lambda _cmd: "echo edited-version",
        )
        agent.run_task("say it")
        content = tool_messages(agent.messages)[0]["content"]
        assert "[user edited the command to: echo edited-version]" in content
        assert "edited-version" in content
        assert "wrong" not in content.split("]", 1)[1]

    def test_none_denies(self):
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="echo hi")]),
                model_says("ok"),
            ],
            approve=lambda _cmd: None,
        )
        agent.run_task("hi")
        assert tool_messages(agent.messages)[0]["content"] == DENIED_RESULT


class TestContextAndHistory:
    def test_context_lands_in_system_prompt(self):
        agent, _ = make_agent([model_says("hi")], context="MAGIC-CONTEXT-42")
        assert "MAGIC-CONTEXT-42" in agent.messages[0]["content"]
        assert "read_docs" in agent.messages[0]["content"]

    def test_environment_context_has_date_and_cwd(self):
        from aish.agent import environment_context

        text = environment_context("/some/dir")
        import datetime

        assert datetime.date.today().isoformat() in text
        assert "/some/dir" in text

    def test_on_message_records_serialized_messages(self):
        records = []
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="echo hi")]),
                model_says("done"),
            ],
            on_message=records.append,
        )
        agent.run_task("say hi")
        roles = [r["role"] for r in records]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert all(isinstance(r["content"], str) for r in records)

    def test_load_history_extends_without_rerecording(self):
        records = []
        agent, chat = make_agent([model_says("hi")], on_message=records.append)
        agent.load_history(
            [
                {"role": "system", "content": "stale — must be skipped"},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
        )
        assert records == []
        agent.run_task("new question")
        sent = chat.calls[0]["messages"]
        assert sent[1]["content"] == "old question"
        dicts = [m for m in sent if isinstance(m, dict)]
        assert all(m.get("content") != "stale — must be skipped" for m in dicts)


class TestBangCommands:
    def test_user_command_skips_approval_and_records_context(self):
        records = []
        agent, _ = make_agent(
            [],
            approve=lambda _cmd: pytest.fail("! commands must not hit the approval gate"),
            on_message=records.append,
        )
        result = agent.run_user_command("echo direct-hit")
        assert "direct-hit" in result
        assert records[0]["role"] == "user"
        assert "I ran `echo direct-hit` myself" in records[0]["content"]
        assert "direct-hit" in records[0]["content"]

    def test_user_cd_changes_persistent_cwd(self, tmp_path):
        agent, _ = make_agent([], approve=lambda _cmd: pytest.fail("no approval for !cd"))
        agent.run_user_command(f"cd {tmp_path}")
        assert agent.cwd == str(tmp_path)
        result = agent.run_user_command("pwd")
        assert tmp_path.name in result


def test_failed_cd_is_echoed_not_silent(tmp_path):
    """Regression: !cd to a missing dir looked like a no-op because only
    successful cd echoed."""
    echoed = []
    agent, _ = make_agent([], cwd=str(tmp_path), echo=echoed.append)
    agent.run_user_command("cd nope-xyz")
    assert agent.cwd == str(tmp_path)
    assert any("ERROR: no such directory" in line for line in echoed)


def test_read_skill_dispatch_no_approval(tmp_path):
    skills_dir = tmp_path / ".aish" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "demo.md").write_text("# demo skill\nuse it wisely")
    call = SimpleNamespace(
        function=SimpleNamespace(name="read_skill", arguments={"name": "demo"})
    )
    agent, _ = make_agent(
        [
            model_says(tool_calls=[call]),
            model_says("done"),
        ],
        approve=lambda _c: pytest.fail("read_skill must not hit the approval gate"),
        cwd=str(tmp_path),
    )
    agent.run_task("how do I use demo?")
    assert "use it wisely" in tool_messages(agent.messages)[0]["content"]


class FakeStreamChat:
    """Scripted streaming responses: each turn is a list of chunks."""

    def __init__(self, turns):
        self.turns = list(turns)

    def __call__(self, **kwargs):
        assert kwargs.get("stream") is True
        return iter(self.turns.pop(0))


def chunk(content=None, tool_calls=None):
    return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))


class TestStreaming:
    def test_tokens_stream_in_order_with_newlines(self):
        tokens = []
        chat = FakeStreamChat([[chunk("Hel"), chunk("lo"), chunk(" world")]])
        agent = Agent(model="fake", approve=lambda _c: True, client_chat=chat,
                      on_token=tokens.append)
        assert agent.run_task("hi") == "Hello world"
        assert tokens == ["\n", "Hel", "lo", " world", "\n"]

    def test_streamed_tool_call_then_answer(self):
        tokens = []
        chat = FakeStreamChat(
            [
                [chunk(tool_calls=[tool_call("run_command", command="echo streamed42")])],
                [chunk("the answer")],
            ]
        )
        agent = Agent(model="fake", approve=lambda c: c, client_chat=chat,
                      on_token=tokens.append)
        assert agent.run_task("run it") == "the answer"
        assert "streamed42" in tool_messages(agent.messages)[0]["content"]
        assistant = [m for m in agent.messages
                     if isinstance(m, dict) and m.get("role") == "assistant"]
        assert assistant[0]["tool_calls"][0]["function"]["name"] == "run_command"

    def test_synthesized_results_still_reach_user(self):
        tokens = []
        endless = [chunk(tool_calls=[tool_call("read_docs", command="ls")])]
        chat = FakeStreamChat([list(endless)] * 3)
        agent = Agent(model="fake", approve=lambda _c: True, client_chat=chat,
                      on_token=tokens.append, max_steps=3)
        result = agent.run_task("loop")
        assert "max-steps" in result
        assert any("max-steps" in t for t in tokens)


class TestBlockedAndBackground:
    def test_blocked_command_never_executes(self, tmp_path):
        from aish.approval import Blocked

        marker = tmp_path / "boom"
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
                model_says("understood"),
            ],
            approve=lambda _c: Blocked("test reason"),
        )
        agent.run_task("do it")
        assert not marker.exists()
        content = tool_messages(agent.messages)[0]["content"]
        assert "BLOCKED" in content and "test reason" in content and "! prefix" in content

    def test_background_arg_starts_job(self, tmp_path, monkeypatch):
        import aish.agent as agent_module

        monkeypatch.setattr(agent_module.tools, "JOBS", [])
        call = SimpleNamespace(function=SimpleNamespace(
            name="run_command", arguments={"command": "echo bg", "background": True}))
        agent, _ = make_agent(
            [model_says(tool_calls=[call]), model_says("started")],
            job_log_dir=tmp_path,
        )
        agent.run_task("start it")
        assert "background job started" in tool_messages(agent.messages)[0]["content"]
        assert len(agent_module.tools.JOBS) == 1


class TestModelResilience:
    def test_empty_response_gives_clear_hint(self):
        from aish.agent import EMPTY_RESPONSE

        agent, _ = make_agent([model_says("")])  # no content, no tool calls
        assert agent.run_task("hi") == EMPTY_RESPONSE

    def test_retries_once_then_succeeds(self):
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("connection refused")
            return model_says("recovered")

        agent = Agent(model="fake", approve=lambda _c: True, client_chat=flaky)
        assert agent.run_task("hi") == "recovered"
        assert calls["n"] == 2

    def test_raises_model_unavailable_after_two_failures(self):
        from aish.agent import ModelUnavailable

        def dead(**kwargs):
            raise ConnectionError("overloaded")

        agent = Agent(model="fake", approve=lambda _c: True, client_chat=dead)
        with pytest.raises(ModelUnavailable, match="overloaded"):
            agent.run_task("hi")


class TestFileTools:
    def call(self, name, **args):
        return SimpleNamespace(function=SimpleNamespace(name=name, arguments=args))

    def test_read_file_no_approval(self, tmp_path):
        (tmp_path / "r.txt").write_text("readable\n")
        agent, _ = make_agent(
            [model_says(tool_calls=[self.call("read_file", path="r.txt")]), model_says("done")],
            approve=lambda _c: pytest.fail("read_file must not hit approval"),
            cwd=str(tmp_path),
        )
        agent.run_task("read it")
        assert "readable" in tool_messages(agent.messages)[0]["content"]

    def test_sensitive_read_prompts_and_denial_blocks_contents(self, tmp_path):
        from aish.agent import READ_DENIED

        secret = tmp_path / ".env"
        secret.write_text("API_KEY=supersecret\n")
        asked = []
        agent, _ = make_agent(
            [model_says(tool_calls=[self.call("read_file", path=".env")]), model_says("ok")],
            approve_read=lambda p, _r: asked.append(p) or False,
            cwd=str(tmp_path),
        )
        agent.run_task("read env")
        assert asked == [".env"]  # the gate was consulted
        result = tool_messages(agent.messages)[0]["content"]
        assert result == READ_DENIED
        assert "supersecret" not in result

    def test_sensitive_read_approved_returns_contents(self, tmp_path):
        secret = tmp_path / ".env"
        secret.write_text("API_KEY=supersecret\n")
        agent, _ = make_agent(
            [model_says(tool_calls=[self.call("read_file", path=".env")]), model_says("ok")],
            approve_read=lambda _p, _r: True,
            cwd=str(tmp_path),
        )
        agent.run_task("read env")
        assert "supersecret" in tool_messages(agent.messages)[0]["content"]

    def test_write_file_approved_writes_and_shows_plan(self, tmp_path):
        seen = {}
        agent, _ = make_agent(
            [model_says(tool_calls=[self.call("write_file", path="new.py", content="x=1\n")]),
             model_says("done")],
            approve_write=lambda plan: seen.update(added=plan.added, is_new=plan.is_new) or True,
            cwd=str(tmp_path),
        )
        agent.run_task("write it")
        assert (tmp_path / "new.py").read_text() == "x=1\n"
        assert seen == {"added": 1, "is_new": True}
        assert "created" in tool_messages(agent.messages)[0]["content"]

    def test_write_file_denied_does_not_write(self, tmp_path):
        from aish.agent import WRITE_DENIED

        agent, _ = make_agent(
            [model_says(tool_calls=[self.call("write_file", path="x.py", content="x=1\n")]),
             model_says("ok")],
            approve_write=lambda _plan: False,
            cwd=str(tmp_path),
        )
        agent.run_task("write it")
        assert not (tmp_path / "x.py").exists()
        assert tool_messages(agent.messages)[0]["content"] == WRITE_DENIED

    def test_edit_file_default_denies(self, tmp_path):
        (tmp_path / "c.py").write_text("a = 1\n")
        # default approve_write denies — Agent constructed without one
        agent, _ = make_agent(
            [model_says(tool_calls=[self.call("edit_file", path="c.py", old_str="a = 1",
                                              new_str="a = 2")]),
             model_says("ok")],
            cwd=str(tmp_path),
        )
        agent.run_task("edit it")
        assert (tmp_path / "c.py").read_text() == "a = 1\n"

    def test_edit_error_skips_approval(self, tmp_path):
        (tmp_path / "c.py").write_text("a = 1\n")
        agent, _ = make_agent(
            [model_says(tool_calls=[self.call("edit_file", path="c.py", old_str="nope",
                                              new_str="x")]),
             model_says("ok")],
            approve_write=lambda _p: pytest.fail("errored plan must not reach approval"),
            cwd=str(tmp_path),
        )
        agent.run_task("edit it")
        assert "not found" in tool_messages(agent.messages)[0]["content"]


class TestWebTools:
    def call(self, name, **args):
        return SimpleNamespace(function=SimpleNamespace(name=name, arguments=args))

    def test_web_search_no_approval_and_query_passed(self, monkeypatch):
        import aish.agent as agent_module

        seen = {}

        def fake_search(query):
            seen["query"] = query
            return "1. Result\n   https://x.example\n   snippet"

        monkeypatch.setattr(agent_module.web, "web_search", fake_search)
        agent, _ = make_agent(
            [model_says(tool_calls=[self.call("web_search", query="latest uv release")]),
             model_says("done")],
            approve=lambda _c: pytest.fail("web_search must not hit approval"),
        )
        agent.run_task("what's new in uv?")
        assert seen["query"] == "latest uv release"
        assert "https://x.example" in tool_messages(agent.messages)[0]["content"]

    def test_read_url_topic_passed_through_and_echoed(self, monkeypatch):
        import aish.agent as agent_module

        seen = {}

        def fake_read(url, topic=None):
            seen.update(url=url, topic=topic)
            return "[page] matching lines"

        monkeypatch.setattr(agent_module.web, "read_url", fake_read)
        echoed = []
        agent, _ = make_agent(
            [model_says(tool_calls=[self.call("read_url", url="https://x.example/doc",
                                              topic="install")]),
             model_says("done")],
            approve=lambda _c: pytest.fail("read_url must not hit approval"),
            echo=echoed.append,
        )
        agent.run_task("read the doc")
        assert seen == {"url": "https://x.example/doc", "topic": "install"}
        assert any("read_url: https://x.example/doc (topic: install)" in e for e in echoed)

    def test_sources_collected_from_read_url(self, monkeypatch):
        import aish.agent as agent_module

        monkeypatch.setattr(
            agent_module.web, "read_url", lambda url, topic=None: f"[{url}] page text"
        )
        monkeypatch.setitem(
            agent_module.web.PAGE_TITLES, "https://a.example/doc", "A Documentation"
        )
        agent, _ = make_agent(
            [
                model_says(tool_calls=[
                    self.call("read_url", url="https://a.example/doc"),
                    self.call("read_url", url="https://b.example/"),
                    self.call("read_url", url="https://a.example/doc"),  # dup dropped
                ]),
                model_says("answer"),
            ]
        )
        agent.run_task("research")
        assert agent.task_sources == [
            {"url": "https://a.example/doc", "title": "A Documentation"},
            {"url": "https://b.example/"},
        ]

    def test_sources_skip_failures_and_reset_per_task(self, monkeypatch):
        import aish.agent as agent_module

        results = {"https://ok.example/": "[page] text", "https://bad.example/": "ERROR: 404"}
        monkeypatch.setattr(
            agent_module.web, "read_url", lambda url, topic=None: results[url]
        )
        agent, _ = make_agent(
            [
                model_says(tool_calls=[
                    self.call("read_url", url="https://ok.example/"),
                    self.call("read_url", url="https://bad.example/"),
                ]),
                model_says("answer"),
                model_says("no web this time"),
            ]
        )
        agent.run_task("research")
        assert [s["url"] for s in agent.task_sources] == ["https://ok.example/"]
        agent.run_task("chat only")
        assert agent.task_sources == []


class TestRememberTool:
    def test_remember_auto_approved_and_appends(self, tmp_path):
        lessons = tmp_path / "lessons.md"
        call = SimpleNamespace(function=SimpleNamespace(
            name="remember", arguments={"note": "macOS ps: use ps aux -m"}))
        agent, _ = make_agent(
            [model_says(tool_calls=[call]), model_says("noted")],
            approve=lambda _c: pytest.fail("remember must not hit approval"),
            lessons_path=lessons,
        )
        agent.run_task("learn it")
        assert "ps aux -m" in lessons.read_text()
        assert "remembered" in tool_messages(agent.messages)[0]["content"]

    def test_remember_without_path_errors_gracefully(self):
        call = SimpleNamespace(function=SimpleNamespace(
            name="remember", arguments={"note": "x"}))
        agent, _ = make_agent([model_says(tool_calls=[call]), model_says("ok")])
        agent.run_task("learn")
        assert "no lessons file" in tool_messages(agent.messages)[0]["content"]


class TestRootScoping:
    """read_file auto-approval is confined to session roots; only the
    user-side rebase/add_root (i.e. /cd and /add-dir) widen or move them."""

    def test_read_outside_root_prompts_with_reason(self, tmp_path):
        from aish.agent import READ_DENIED

        root = tmp_path / "project"
        root.mkdir()
        outside = tmp_path / "elsewhere.txt"
        outside.write_text("private\n")
        asked = []
        agent, _ = make_agent(
            [model_says(tool_calls=[tool_call("read_file", path=str(outside))]),
             model_says("ok")],
            approve_read=lambda p, r: asked.append((p, r)) or False,
            cwd=str(root),
        )
        agent.run_task("read it")
        assert asked == [(str(outside), "outside")]
        result = tool_messages(agent.messages)[0]["content"]
        assert result == READ_DENIED
        assert "private" not in result

    def test_read_inside_root_needs_no_prompt(self, tmp_path):
        (tmp_path / "ok.txt").write_text("fine\n")
        agent, _ = make_agent(
            [model_says(tool_calls=[tool_call("read_file", path="ok.txt")]),
             model_says("ok")],
            approve_read=lambda _p, _r: pytest.fail("in-root read must not prompt"),
            cwd=str(tmp_path),
        )
        agent.run_task("read it")
        assert "fine" in tool_messages(agent.messages)[0]["content"]

    def test_sensitive_beats_outside_as_reason(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        (root / ".env").write_text("KEY=x\n")
        asked = []
        agent, _ = make_agent(
            [model_says(tool_calls=[tool_call("read_file", path=".env")]),
             model_says("ok")],
            approve_read=lambda p, r: asked.append(r) or True,
            cwd=str(root),
        )
        agent.run_task("read env")
        assert asked == ["sensitive"]

    def test_model_cd_moves_cwd_but_not_root(self, tmp_path):
        root = tmp_path / "project"
        elsewhere = tmp_path / "elsewhere"
        root.mkdir()
        elsewhere.mkdir()
        agent, _ = make_agent(
            [model_says(tool_calls=[tool_call("run_command", command=f"cd {elsewhere}")]),
             model_says("moved")],
            cwd=str(root),
        )
        agent.run_task("go elsewhere")
        assert agent.cwd == str(elsewhere)
        assert agent.roots == [root.resolve()]

    def test_rebase_moves_cwd_and_root_and_tells_model(self, tmp_path):
        root = tmp_path / "wrong"
        right = tmp_path / "right"
        root.mkdir()
        right.mkdir()
        agent, _ = make_agent([], cwd=str(root))
        result = agent.rebase(str(right))
        assert "working directory is now" in result
        assert agent.cwd == str(right)
        assert agent.roots == [right.resolve()]
        note = agent.messages[-1]
        assert note["role"] == "user" and "/cd" in note["content"]

    def test_rebase_bad_dir_is_error_and_keeps_root(self, tmp_path):
        agent, _ = make_agent([], cwd=str(tmp_path))
        result = agent.rebase(str(tmp_path / "missing"))
        assert result.startswith("ERROR")
        assert agent.roots == [tmp_path.resolve()]

    def test_rebase_keeps_added_roots(self, tmp_path):
        a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
        for d in (a, b, c):
            d.mkdir()
        agent, _ = make_agent([], cwd=str(a))
        agent.add_root(str(b))
        agent.rebase(str(c))
        assert agent.roots == [c.resolve(), b.resolve()]

    def test_add_root_widens_read_scope(self, tmp_path):
        root = tmp_path / "project"
        other = tmp_path / "other"
        root.mkdir()
        other.mkdir()
        (other / "doc.txt").write_text("shared\n")
        agent, _ = make_agent(
            [model_says(tool_calls=[tool_call("read_file", path=str(other / "doc.txt"))]),
             model_says("ok")],
            approve_read=lambda _p, _r: pytest.fail("added root must not prompt"),
            cwd=str(root),
        )
        agent.add_root(str(other))
        agent.run_task("read it")
        assert "shared" in tool_messages(agent.messages)[0]["content"]

    def test_add_root_rejects_missing_and_dedupes(self, tmp_path):
        agent, _ = make_agent([], cwd=str(tmp_path))
        assert agent.add_root(str(tmp_path / "nope")).startswith("ERROR")
        assert "already" in agent.add_root(str(tmp_path))
        assert agent.roots == [tmp_path.resolve()]


class TestCancel:
    def test_cancel_stops_before_next_model_call(self, tmp_path):
        from aish.agent import CANCELLED_RESULT

        marker = tmp_path / "ran"

        def approve_and_cancel(_cmd):
            agent.cancel()  # user hits Stop while the card is up, then denies
            return False

        agent, chat = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
                model_says("should never be reached"),
            ],
            approve=approve_and_cancel,
        )
        result = agent.run_task("touch it")
        assert result == CANCELLED_RESULT
        assert len(chat.calls) == 1  # no model call after the stop
        assert not marker.exists()
        # history stays model-consumable: cancelled note closes the turn
        assert agent.messages[-1] == {"role": "assistant", "content": CANCELLED_RESULT}

    def test_cancel_before_tool_execution_pairs_results(self, tmp_path):
        from aish.agent import CANCELLED_RESULT, NOT_EXECUTED

        marker = tmp_path / "ran"
        agent, chat = make_agent(
            [model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")])],
            approve=lambda _cmd: pytest.fail("must not reach approval after cancel"),
        )
        original = agent._chat_turn

        def cancel_after_turn():
            out = original()
            agent.cancel()  # stop lands while the model was proposing calls
            return out

        agent._chat_turn = cancel_after_turn
        assert agent.run_task("touch it") == CANCELLED_RESULT
        assert not marker.exists()
        tool_results = tool_messages(agent.messages)
        assert tool_results[-1]["content"] == NOT_EXECUTED

    def test_new_task_clears_stale_cancel(self):
        agent, chat = make_agent([model_says("fresh answer")])
        agent.cancel()  # left over from a previous task
        assert agent.run_task("hello") == "fresh answer"


class TestStepLimitAndLoops:
    """#25: the step limit ends with a self-assessment turn, and running in
    circles (identical call, identical output) warns then stops early."""

    def _docs(self, monkeypatch, fn):
        import aish.agent as agent_module

        monkeypatch.setattr(agent_module.tools, "read_docs", fn)

    def test_step_limit_runs_wrapup_turn(self):
        endless = model_says(tool_calls=[tool_call("read_docs", command="ls")])
        agent, chat = make_agent(
            [endless, endless, model_says("half done; X remains")], max_steps=2
        )
        result = agent.run_task("big task")
        assert "max-steps" in result and "half done; X remains" in result
        assert len(chat.calls) == 3  # 2 budgeted turns + 1 wrap-up
        wrapup_prompt = [
            m for m in chat.calls[2]["messages"]
            if m["role"] == "user" and "step limit" in m["content"]
        ]
        assert wrapup_prompt

    def test_wrapup_tool_calls_are_never_executed(self, monkeypatch):
        from aish.agent import NOT_EXECUTED_LIMIT

        executed = []
        self._docs(monkeypatch, lambda c, topic=None: (executed.append(c), "docs")[1])
        endless = model_says(tool_calls=[tool_call("read_docs", command="ls")])
        agent, chat = make_agent([endless, endless], max_steps=1)
        result = agent.run_task("task")
        assert result.startswith("(stopped")
        assert executed == ["ls"]  # only the in-budget call ran
        assert tool_messages(agent.messages)[-1]["content"] == NOT_EXECUTED_LIMIT

    def test_loop_warning_injected_after_three_identical_results(self, monkeypatch):
        self._docs(monkeypatch, lambda c, topic=None: "same docs")
        same = model_says(tool_calls=[tool_call("read_docs", command="ls")])
        agent, _ = make_agent(
            [same, same, same, model_says("changing approach")], max_steps=10
        )
        assert agent.run_task("loop") == "changing approach"
        warnings = [
            m for m in agent.messages
            if m.get("role") == "user" and "identical output" in (m.get("content") or "")
        ]
        assert len(warnings) == 1  # warned exactly once, at the third repeat

    def test_loop_stops_after_five_identical_results(self, monkeypatch):
        self._docs(monkeypatch, lambda c, topic=None: "same docs")
        same = model_says(tool_calls=[tool_call("read_docs", command="ls")])
        agent, chat = make_agent(
            [same] * 5 + [model_says("stuck because the flag is unsupported")],
            max_steps=25,
        )
        result = agent.run_task("loop")
        assert "no progress" in result and "stuck because" in result
        assert len(chat.calls) == 6  # stopped at 5 repeats, then the diagnostic turn

    def test_changing_output_never_trips_loop_detection(self, monkeypatch):
        ticks = iter(range(100))
        self._docs(monkeypatch, lambda c, topic=None: f"tick {next(ticks)}")
        poll = model_says(tool_calls=[tool_call("read_docs", command="ls")])
        agent, _ = make_agent([poll] * 6 + [model_says("done polling")], max_steps=25)
        assert agent.run_task("poll") == "done polling"

    def test_model_failure_in_wrapup_falls_back_to_headline(self):
        endless = model_says(tool_calls=[tool_call("read_docs", command="ls")])
        agent, chat = make_agent([endless], max_steps=1)  # wrap-up pops empty list
        result = agent.run_task("task")
        assert result.startswith("(stopped: hit the max-steps limit")


class TestSearchSessionsTool:
    """#14: past-session search is read-only, auto-approved, and excludes the
    session being written right now."""

    def _store(self, tmp_path, name="session-20260101-000000-000000.jsonl"):
        from aish.session import SessionLog

        log = SessionLog(tmp_path / name)
        log.message({"role": "user", "content": "the uv fix was pinning the version"})
        return log.path

    def test_runs_without_approval_and_returns_matches(self, tmp_path):
        self._store(tmp_path)
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("search_sessions", query="uv fix")]),
                model_says("found it"),
            ],
            approve=lambda _cmd: pytest.fail("search_sessions must not hit approval"),
            state_dir=tmp_path,
        )
        assert agent.run_task("what did we do about uv?") == "found it"
        result = tool_messages(agent.messages)[0]["content"]
        assert "session-20260101" in result and "uv fix" in result

    def test_without_store_reports_unavailable(self):
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("search_sessions", query="x")]),
                model_says("ok"),
            ]
        )
        agent.run_task("search")
        assert tool_messages(agent.messages)[0]["content"].startswith("ERROR")

    def test_current_session_is_excluded_from_search(self, tmp_path):
        current = self._store(tmp_path, "session-20260102-000000-000000.jsonl")
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("search_sessions", query="uv fix")]),
                model_says("nothing"),
            ],
            state_dir=tmp_path,
            current_session=lambda: current,
        )
        agent.run_task("search")
        assert "No past session matches" in tool_messages(agent.messages)[0]["content"]
