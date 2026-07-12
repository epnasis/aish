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
