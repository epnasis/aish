"""The context meter: how full the window was on the last model call.

The agent measures it once per call and stamps it on the step the call already
emits (`ctx` on `thinking` / `thinking_cancel`), which is what the web model
chip reads live and on replay; the terminal reads the same figure off the
agent for the rule under its prompt.
"""

import io
import re

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from aish import backends
from aish.claude_max import _prompt_tokens
from aish.cli import context_meter
from aish.prompt import BoxPrompt
from tests.test_agent import make_agent, model_says, tool_call


def claude_says(content="", tool_calls=None, *, uncached=10, cache_read=0, cache_write=0):
    """A response shaped as the Anthropic adapter returns it: the summed input
    on `prompt_eval_count`, the parts in the labelled detail."""
    response = model_says(content, tool_calls, tokens=(uncached + cache_read + cache_write, 5))
    response.usage = backends.usage_detail(
        backends.INPUT_EXCLUDES_CACHE,
        input=uncached,
        cache_read=cache_read,
        cache_write=cache_write,
        output=5,
    )
    return response


def run(responses, provider="claude", **kwargs):
    steps: list[dict] = []
    agent, chat = make_agent(responses, on_step=steps.append, **kwargs)
    agent.provider = provider
    agent.run_task("go")
    return agent, steps


def call_steps(steps):
    return [s for s in steps if s["kind"] in ("thinking", "thinking_cancel")]


class TestTheAgentMeasuresEveryCall:
    def test_a_reported_count_is_the_fill_cache_included(self):
        agent, steps = run(
            [
                claude_says(tool_calls=[tool_call("run_command", command="echo hi")],
                            cache_read=20_000, cache_write=1_000),
                claude_says("done", cache_read=40_000),
            ]
        )
        first, last = call_steps(steps)
        assert first["ctx"] == {
            "used": 21_010,
            "window": 200_000,
            "window_source": "backend:claude:200000",
            "basis": "reported",
            "model": "claude:fake",
        }
        assert last["ctx"]["used"] == 40_010
        assert agent.current_context_fill() == last["ctx"]

    def test_ollama_is_estimated_from_characters_never_below_its_count(self):
        # Ollama's count skips the prefix it reused from its KV cache, so a
        # count of 7 on a request carrying a whole system prompt is not the fill.
        agent, steps = run([model_says("done", tokens=(7, 3))], provider="ollama")
        (step,) = call_steps(steps)
        fill = step["ctx"]
        assert fill["basis"] == "estimated"
        assert fill["window"] == agent.num_ctx
        assert fill["used"] > 7 * 100  # the system prompt alone is thousands of chars

        # A count above the character estimate is a floor the estimate cannot go under.
        agent, steps = run([model_says("done", tokens=(500_000, 3))], provider="ollama")
        assert call_steps(steps)[0]["ctx"]["used"] == 500_000

    def test_a_call_that_reported_nothing_carries_no_figure(self):
        agent, steps = run(
            [
                claude_says(tool_calls=[tool_call("run_command", command="echo hi")],
                            cache_read=9_000),
                model_says("done"),  # no usage at all
            ]
        )
        first, last = call_steps(steps)
        assert first["ctx"]["used"] == 9_010
        assert "ctx" not in last  # never the previous call's figure
        # The meter keeps the last value that WAS measured.
        assert agent.current_context_fill()["used"] == 9_010

    def test_a_zero_count_is_not_a_figure(self):
        _, steps = run([claude_says("done", uncached=0)])
        assert "ctx" not in call_steps(steps)[0]

    def test_a_figure_against_another_model_is_not_shown(self):
        agent, _ = run([claude_says("done", cache_read=1_000)])
        assert agent.current_context_fill() is not None
        agent.model = "another"
        assert agent.current_context_fill() is None

    def test_a_new_or_loaded_chat_starts_without_one(self):
        agent, _ = run([claude_says("done", cache_read=1_000)])
        agent.reset()
        assert agent.current_context_fill() is None
        agent, _ = run([claude_says("done", cache_read=1_000)])
        agent.load_history([])
        assert agent.current_context_fill() is None


def test_claude_max_counts_the_whole_prompt():
    # The usage one real `claude -p --output-format stream-json` assistant
    # message carried (2026-09-26): input_tokens alone is 10.
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 8641,
        "cache_read_input_tokens": 13689,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 8641},
        "output_tokens": 4,
    }
    assert _prompt_tokens(usage) == 22_340
    assert _prompt_tokens(None) == 0


class TestTheTerminalMeter:
    def test_label(self):
        fill = {"used": 68_000, "window": 200_000, "basis": "reported"}
        assert context_meter(fill) == "ctx 34% · 68.0k/200.0k"
        assert context_meter({**fill, "basis": "estimated"}) == "ctx ~34% · ~68.0k/200.0k"
        assert context_meter({"used": 900, "window": 1_048_576}) == "ctx <1% · 900/1048.6k"
        assert context_meter(None) == ""
        assert context_meter({"used": 0, "window": 200_000}) == ""

    def test_it_is_drawn_on_the_rule_under_the_input(self, tmp_path):
        box = BoxPrompt(False, tmp_path, ())
        box.get_status = lambda: "ctx 34% · 68.0k/200.0k"
        screen = io.StringIO()
        output = Vt100_Output(screen, lambda: Size(rows=24, columns=60), term="xterm")
        with create_pipe_input() as pipe:
            pipe.send_text("hi\r")
            assert box.read_with_io("~", input=pipe, output=output) == "hi"
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", screen.getvalue())
        assert any(line.rstrip().endswith("─ ctx 34% · 68.0k/200.0k") for line in text.splitlines())


def test_claude_max_feeds_the_terminal_meter(monkeypatch, tmp_path):
    from tests.test_claude_max import FakeSDK, make_max_agent

    agent, fake = make_max_agent(monkeypatch, tmp_path)
    message = FakeSDK.AssistantMessage([FakeSDK.TextBlock("hi")])
    message.usage = {"input_tokens": 10, "cache_read_input_tokens": 49_990}
    # A sub-agent's message inside a tool call: its own context, not this one.
    nested = FakeSDK.AssistantMessage([FakeSDK.TextBlock("inner")])
    nested.usage = {"input_tokens": 100}
    nested.parent_tool_use_id = "toolu_1"
    fake.streams.append([message, nested])

    async def script(_sdk):
        return "hi"

    fake.scripts.append(script)
    agent.run_task("hello")
    fill = agent.current_context_fill()
    assert (fill["used"], fill["window"], fill["basis"]) == (50_000, 200_000, "reported")
    assert context_meter(fill) == "ctx 25% · 50.0k/200.0k"
