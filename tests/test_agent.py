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


def model_says(content: str = "", tool_calls: list | None = None):
    return SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls or None)
    )


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
        assert len(chat.calls) == 3

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
