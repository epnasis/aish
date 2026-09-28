"""Trim fixes (#429): a stub never grows, a futile pass rewrites nothing, and
earlier tool-call arguments are a lever.

Evidence: `session-20260925-204008-294943` (#422's chat), tasks 37-39 on a
local Qwen3.6. Two trims that stubbed `show_image` results made the history
LARGER, nine trims in task 39 freed between -65 and 703 characters each and
still rewrote history, and aish's messages carried 58,701 characters of
tool-call arguments that nothing shortened.
"""

import json
import math

from aish import agent as agent_module
from aish import backends, tool_plugins
from aish.agent import Agent
from aish.session import SessionLog
from tests.test_agent import make_agent, model_says, tool_call
from tests.test_local_token_budget import REPO, CountingServer, _agent

# The shape of the results in #422's chat whose stubs grew the history: a
# `show_image` result longer than the old bound (247) and shorter than the
# stub it got (372 characters, the size of every such stub in that request).
SHOW_IMAGE_RESULT = (
    "Image ready — it is attached to this turn, so look at it and make sure it really "
    "shows what the user asked for. Include this line in your answer EXACTLY as written "
    "(do not alter the path):\n\n![Wielkość opadów](/Users/e/.local/state/aish/media/"
    "5855edb2a1c4-wielkosc-opadow.png)"
)


def _local(monkeypatch, tmp_path, steps):
    return _agent(monkeypatch, tmp_path, CountingServer([]), ctx=10**6, max_tokens=1_000,
                  steps=steps)


def _budget_at(monkeypatch, tokens: int, max_tokens: int = 1_000) -> None:
    """A `local:` window whose prompt budget is exactly `tokens`."""
    ctx = math.ceil(tokens / agent_module.LOCAL_PROMPT_SAFETY) + max_tokens + 1
    monkeypatch.setenv("AISH_LOCAL_CTX", str(ctx))
    monkeypatch.setenv("AISH_LOCAL_MAX_TOKENS", str(max_tokens))


def _trims(steps):
    return [s for s in steps if s.get("kind") == "trim"]


def _earlier_call(agent, command: str, result: str, *, stub: bool = True) -> int:
    """An earlier assistant turn with one run_command call, and its result."""
    agent.messages.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "call_x", "function": {
            "name": "run_command", "arguments": {"command": command, "timeout": 30}}}],
    })
    index = len(agent.messages) - 1
    agent.messages.append({"role": "tool", "tool_name": "run_command", "content": result})
    if stub:
        agent._trim_tool_message(agent.messages[-1])
    return index


class TestAStubNeverGrows:
    def test_a_result_barely_over_the_old_bound_is_left_whole(self, tmp_path):
        """The bound counted the plain note; the stub carries the recoverable
        one with a real key, ~150 characters longer."""
        agent = Agent(model=REPO, approve=lambda _c: True, client_chat=lambda **_: None,
                      cwd=str(tmp_path), state_dir=str(tmp_path / "state"))
        message = {"role": "tool", "tool_name": "show_image", "content": SHOW_IMAGE_RESULT}
        assert len(SHOW_IMAGE_RESULT) > agent_module.TRIM_KEEP_CHARS + len(
            agent_module.TRIMMED_NOTE
        ), "the old bound would have stubbed it"
        assert agent._trim_tool_message(message) is None
        assert message["content"] == SHOW_IMAGE_RESULT
        assert "_stub" not in message

    def test_the_growth_in_422s_chat_does_not_recur(self, monkeypatch, tmp_path):
        """Task 39's boundary trim: two `show_image` results were the only
        whole outputs left, and stubbing them took the history from 136,449
        to 136,513 characters. Now no trim record grows, on either lever."""
        steps: list[dict] = []
        agent = _local(monkeypatch, tmp_path, steps)
        for i in range(30):
            agent.messages.append({"role": "user", "content": f"q{i} " + "w" * 300})
            agent.messages.append({"role": "assistant", "content": f"a{i} " + "v" * 330})
            agent.messages.append({"role": "tool", "tool_name": "show_image",
                                   "content": SHOW_IMAGE_RESULT})
        _budget_at(monkeypatch, agent._history_size() - 50)

        agent._trim_history_to_budget(protect_from=len(agent.messages))

        assert all(t["bytes_after"] <= t["bytes_before"] for t in _trims(steps))
        assert all(m.get("content") == SHOW_IMAGE_RESULT
                   for m in agent.messages if m.get("tool_name") == "show_image")
        assert not any(m.get("_stub") for m in agent.messages), "every stub would have grown"

    def test_the_turn_lever_measures_with_the_real_key(self, tmp_path):
        agent = Agent(model=REPO, approve=lambda _c: True, client_chat=lambda **_: None,
                      cwd=str(tmp_path), state_dir=str(tmp_path / "state"))
        short = {"role": "assistant", "content": "x" * 340}
        assert agent._plan_turn_stub(short) is not None
        assert not agent._plan_turn_stub(short).shrinks
        long = {"role": "assistant", "content": "x" * 2_000}
        planned = agent._plan_turn_stub(long)
        assert planned.shrinks and planned.key in planned.fields["content"]

    def test_skipped_stubs_are_counted_on_the_record(self, monkeypatch, tmp_path):
        steps: list[dict] = []
        agent = _local(monkeypatch, tmp_path, steps)
        agent.messages.append({"role": "tool", "tool_name": "show_image",
                               "content": SHOW_IMAGE_RESULT})
        agent.messages.append({"role": "tool", "tool_name": "read_url", "content": "p" * 60_000})
        _budget_at(monkeypatch, agent._history_size() - 50)

        agent._trim_history_to_budget(protect_from=len(agent.messages))

        (trim,) = [t for t in _trims(steps) if t["affected"]]
        assert trim["skipped_longer"] == 1
        assert [ref["at"] for ref in trim["stubbed"]] == [2]


class TestAFutilePassRewritesNothing:
    def _over_by_a_little(self, monkeypatch, tmp_path, steps):
        """A history over its budget where all that is left to cut frees a
        few hundred tokens: the shape of every trim in task 39."""
        agent = _local(monkeypatch, tmp_path, steps)
        # Bulk nothing may cut (a system message), and one small output.
        agent.messages.append({"role": "system", "content": "a reminder " + "r" * 60_000})
        agent.messages.append({"role": "tool", "tool_name": "run_command", "content": "o" * 900})
        agent.run_task("anchor")  # the server's count, as the live chat had
        steps.clear()
        _budget_at(monkeypatch, agent._history_size() - 2_000)
        return agent

    def test_it_is_planned_recorded_and_not_applied(self, monkeypatch, tmp_path):
        steps: list[dict] = []
        agent = self._over_by_a_little(monkeypatch, tmp_path, steps)
        before = json.dumps(agent.messages)
        anchor = agent._token_anchor

        agent._enforce_budget(len(agent.messages))

        assert json.dumps(agent.messages) == before, "history was rewritten"
        assert agent._token_anchor == anchor is not None, "the server's count was dropped"
        (skipped,) = [t for t in _trims(steps) if "could_free" in t]
        assert skipped["affected"] == 0 and skipped["fits"] is False
        assert 0 < skipped["could_free"] < agent_module.MIN_TRIM_YIELD_TOKENS
        assert skipped["min_yield"] == agent_module.MIN_TRIM_YIELD_TOKENS
        assert skipped["estimate_after"] == skipped["estimate_before"]

    def test_it_is_said_once_per_task(self, monkeypatch, tmp_path):
        steps: list[dict] = []
        agent = self._over_by_a_little(monkeypatch, tmp_path, steps)
        for _ in range(3):
            agent._enforce_budget(len(agent.messages))
        assert len([t for t in _trims(steps) if "could_free" in t]) == 1

    def test_a_small_pass_that_brings_the_request_under_budget_still_runs(
        self, monkeypatch, tmp_path
    ):
        steps: list[dict] = []
        agent = self._over_by_a_little(monkeypatch, tmp_path, steps)
        # Unanchored, so the estimate before and the projection after are in
        # the same measure: the whole request at the learned ratio.
        agent._history_rewritten()
        output = next(m for m in agent.messages if m.get("role") == "tool")
        planned = agent._plan_output_stub(output)
        estimate = agent._prompt_estimate()
        ratio = agent_module.token_ratio.ratio(f"local:{REPO}")
        after = ratio.tokens(estimate["chars"] + planned.payload_delta)
        assert estimate["tokens"] - after < agent_module.MIN_TRIM_YIELD_TOKENS
        _budget_at(monkeypatch, after + 1)
        assert agent._history_size() > after + 1

        agent._enforce_budget(len(agent.messages))

        assert not [t for t in _trims(steps) if "could_free" in t]
        (trim,) = [t for t in _trims(steps) if t["affected"]]
        assert trim["fits"] is True

    def test_an_overflow_whose_only_cut_would_grow_promises_no_retry(self):
        """#422's chat, task 39: the refused request's shrink stubbed two
        `show_image` results, sent a LARGER request, and failed again."""
        from aish.agent import ModelUnavailable
        from tests.test_ratelimit import OVERFLOW_400

        steps: list[dict] = []
        sent: list[int] = []

        def chat(**kwargs):
            sent.append(len(json.dumps(kwargs["messages"], default=str)))
            raise OVERFLOW_400

        agent = Agent(model="fake", approve=lambda _c: True, client_chat=chat,
                      step_log=steps.append)
        agent.provider = "claude"
        agent.messages.append({"role": "tool", "tool_name": "show_image",
                               "content": SHOW_IMAGE_RESULT})
        try:
            agent.run_task("hi")
        except ModelUnavailable:
            pass
        errors = [s for s in steps if s.get("kind") == "model_error"]
        assert len(sent) == 1, "the same or a larger request was sent again"
        assert errors[-1]["bound"] == "trim_exhausted"
        assert not [t for t in _trims(steps) if t["affected"]]


class TestTheArgumentLever:
    def _history(self, monkeypatch, tmp_path, steps, *, stub_results=True):
        agent = _local(monkeypatch, tmp_path, steps)
        agent.messages.append({"role": "user", "content": "earlier task"})
        long_command = "python3 -c '" + "print(1); " * 400 + "'"
        calls = [
            _earlier_call(agent, long_command + f" # {i}", "r" * 3_000, stub=stub_results)
            for i in range(12)
        ]
        agent.messages.append({"role": "assistant", "content": "done"})
        protect = len(agent.messages)
        agent.messages.append({"role": "user", "content": "this task"})
        current = _earlier_call(agent, long_command + " # now", "r" * 3_000, stub=False)
        return agent, calls, protect, current

    def test_earlier_arguments_are_cut_and_the_pairing_holds(self, monkeypatch, tmp_path):
        steps: list[dict] = []
        agent, calls, protect, current = self._history(monkeypatch, tmp_path, steps)
        before_ids = [
            [c.get("id") for c in m["tool_calls"]] for m in agent.messages if m.get("tool_calls")
        ]
        _budget_at(monkeypatch, agent._history_size() - 2_000)

        agent._enforce_budget(protect, protect_from=protect)

        (trim,) = [t for t in _trims(steps) if t["policy"] == "mid_task_args"]
        assert trim["affected"] >= 1
        assert trim["arg_chars_after"] < trim["arg_chars_before"]
        assert trim["bytes_after"] == trim["bytes_before"], "no content was touched"
        stubbed_at = [ref["at"] for ref in trim["stubbed"]]
        assert stubbed_at == sorted(stubbed_at) and set(stubbed_at) <= set(calls)
        for at in stubbed_at:
            call = agent.messages[at]["tool_calls"][0]
            assert call["id"] == "call_x" and call["function"]["name"] == "run_command"
            arguments = call["function"]["arguments"]
            assert set(arguments) == {"command", "timeout"}
            assert arguments["timeout"] == 30, "a short value stays whole"
            assert arguments["command"].startswith("python3 -c 'print(1);")
            assert "read_tool_output" in arguments["command"]
            assert agent.messages[at]["_args_stub"] is True
        # This task's call is never touched.
        assert "_args_stub" not in agent.messages[current]
        assert [
            [c.get("id") for c in m["tool_calls"]] for m in agent.messages if m.get("tool_calls")
        ] == before_ids
        # Every result still answers a call: none relabelled as the user's words.
        converted = backends.convert_messages(agent.messages)
        assert not [m for m in converted if " result]\n" in str(m.get("content", ""))[:40]]
        ids = {c["id"] for m in converted for c in m.get("tool_calls") or []}
        assert {m["tool_call_id"] for m in converted if m["role"] == "tool"} <= ids

    def test_the_full_arguments_can_be_read_back(self, monkeypatch, tmp_path):
        steps: list[dict] = []
        agent, calls, protect, _ = self._history(monkeypatch, tmp_path, steps)
        original = agent.messages[calls[0]]["tool_calls"][0]["function"]["arguments"]["command"]
        _budget_at(monkeypatch, agent._history_size() - 2_000)

        agent._enforce_budget(protect, protect_from=protect)

        (trim,) = [t for t in _trims(steps) if t["policy"] == "mid_task_args"]
        key = trim["stubbed"][0]["continuation"]
        page = tool_plugins.read_continuation(key, agent.tool_output_dir, 1, 10**6, 0)
        assert page is not None and original in page

    def test_arguments_wait_until_their_results_are_spent(self, monkeypatch, tmp_path):
        """A call whose result is still whole keeps its arguments: the result
        lever goes first, and the arguments follow it, never lead it."""
        steps: list[dict] = []
        agent, calls, protect, _ = self._history(
            monkeypatch, tmp_path, steps, stub_results=False
        )
        for i in calls:
            planned = agent._plan_args_stub(agent.messages[i], [agent.messages[i + 1]])
            assert planned is None

    def test_a_result_that_can_never_be_stubbed_does_not_hold_its_arguments(
        self, monkeypatch, tmp_path
    ):
        """A 227-character `edit_file` result, as in #422's chat: longer than
        a stub keeps, shorter than a stub. It will never be cut, so it must
        not keep its call's 2,972 characters of arguments whole forever."""
        steps: list[dict] = []
        agent = _local(monkeypatch, tmp_path, steps)
        call = {"role": "assistant", "content": "", "tool_calls": [{"function": {
            "name": "edit_file", "arguments": {"path": "/x.py", "new": "n" * 2_972}}}]}
        result = {"role": "tool", "tool_name": "edit_file", "content": "e" * 227}
        assert not agent._plan_output_stub(result).shrinks
        planned = agent._plan_args_stub(call, [result])
        assert planned is not None and planned.shrinks

    def test_a_stubbed_call_is_never_stubbed_again(self, monkeypatch, tmp_path):
        steps: list[dict] = []
        agent, calls, protect, _ = self._history(monkeypatch, tmp_path, steps)
        _budget_at(monkeypatch, agent._history_size() - 2_000)
        agent._enforce_budget(protect, protect_from=protect)
        (trim,) = [t for t in _trims(steps) if t["policy"] == "mid_task_args"]
        for ref in trim["stubbed"]:
            at = ref["at"]
            assert agent._plan_args_stub(agent.messages[at], [agent.messages[at + 1]]) is None

    def test_the_sent_record_marks_a_call_whose_arguments_were_cut(self, monkeypatch, tmp_path):
        steps: list[dict] = []
        agent, calls, protect, _ = self._history(monkeypatch, tmp_path, steps)
        _budget_at(monkeypatch, agent._history_size() - 2_000)
        agent._enforce_budget(protect, protect_from=protect)
        (trim,) = [t for t in _trims(steps) if t["policy"] == "mid_task_args"]
        at = trim["stubbed"][0]["at"]
        _budget_at(monkeypatch, 10**7)
        agent.run_task("next")
        sent = [s for s in steps if s.get("kind") == "sent"][-1]
        entry = next(e for e in sent["messages"] if e.get("origin") == at)
        assert entry.get("stub") is True

    def test_a_plan_measures_exactly_what_applying_it_frees(self, monkeypatch, tmp_path):
        """The yield floor decides on the plan's arithmetic, so the plan must
        be the request `request_chars` measures after it, to the character."""
        steps: list[dict] = []
        agent, calls, _, _ = self._history(monkeypatch, tmp_path, steps, stub_results=False)
        menu = []
        before = backends.request_chars("local", agent.messages, menu)
        result = agent.messages[calls[0] + 1]
        output = agent._plan_output_stub(result)
        agent._apply_stub(output, result)
        after_output = backends.request_chars("local", agent.messages, menu)
        assert after_output - before == output.payload_delta
        call = agent.messages[calls[0]]
        arguments = agent._plan_args_stub(call, [result])
        agent._apply_stub(arguments, call)
        assert backends.request_chars("local", agent.messages, menu) - after_output == (
            arguments.payload_delta
        )

    def test_other_providers_do_not_cut_arguments(self, tmp_path):
        """Only `local:` measures arguments at all; elsewhere the budget counts
        message content, so cutting arguments would free nothing it sees."""
        agent = Agent(model="fake", approve=lambda _c: True, client_chat=lambda **_: None,
                      cwd=str(tmp_path), state_dir=str(tmp_path / "state"))
        agent.provider = "claude"
        levers = agent._levers("budget_oldest_first", [1], protect_from=5, boundary=True)
        assert [lever for lever, _, _ in levers] == ["outputs"]


class TestAReopenedChatTrimsTheSameWay:
    """#422 put a chat's tool calls back on reopen; the argument lever reads
    them. A reopened chat must be cut exactly as the live one would be."""

    def test_the_same_history_is_cut_the_same_way(self, monkeypatch, tmp_path):
        log = SessionLog(tmp_path / "session-20260928-000000-000000.jsonl")
        long = "x" * 9_000
        agent, _ = make_agent(
            [
                model_says("Looking.", tool_calls=[tool_call("run_command",
                                                             command=f"echo {long}")]),
                model_says(tool_calls=[tool_call("run_command", command=f"echo {long}y")]),
                model_says("done"),
            ],
            cwd=str(tmp_path), state_dir=tmp_path, on_message=log.message, step_log=log.step,
        )
        agent.run_task("echo things")
        log.close()
        live = [m for m in agent.messages if m.get("role") != "system"]
        reopened = SessionLog._parse(log.path).messages

        def trimmed(history, label):
            steps: list[dict] = []
            trimmer = _local(monkeypatch, tmp_path / label, steps)
            trimmer.messages.extend(json.loads(json.dumps(history)))
            protect = len(trimmer.messages)
            _budget_at(monkeypatch, 1)
            trimmer._trim_history_to_budget(protect_from=protect)
            return [
                (m.get("role"), m.get("content"),
                 [(c["function"]["name"], c["function"]["arguments"])
                  for c in m.get("tool_calls") or []])
                for m in trimmer.messages[1:]
            ], [(t["policy"], [r["at"] for r in t["stubbed"]]) for t in _trims(steps)]

        live_after, live_trims = trimmed(live, "live")
        reopened_after, reopened_trims = trimmed(reopened, "reopened")
        assert any(policy == "args_oldest_first" for policy, _ in live_trims), live_trims
        assert reopened_after == live_after
        assert reopened_trims == live_trims
