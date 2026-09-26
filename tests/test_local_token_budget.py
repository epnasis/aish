"""`AISH_LOCAL_CTX` bounds the whole request, measured in tokens (#415).

The server here is a fake that COUNTS: it reports `prompt_tokens` as the
characters of the request it actually received (after the adapter's
conversion) over a fixed chars-per-token, the way a tokenizer would. So what
is asserted is the thing the owner cares about — prompt + answer cap as the
server counted them never exceeds the window — not aish's own estimate of it.
"""

import json
import math
from types import SimpleNamespace

import pytest

from aish import agent as agent_module
from aish import backends, token_ratio
from aish.agent import Agent
from aish.backends import BackendError, make_chat

REPO = "mlx-community/Qwen3.6-35B-A3B-8bit"
KEY = f"local:{REPO}"
# The fake tokenizer's density, inside the 3.30-4.05 measured on mi.
CHARS_PER_TOKEN = 3.5


class CountingServer:
    """Stands in for openai.OpenAI on an mlx-lm server: answers from a script
    and reports the prompt it received in tokens."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.counted: list[tuple[int, int]] = []  # (prompt_tokens, max_tokens)
        outer = self

        class _Completions:
            def create(self, **kwargs):
                sent = json.loads(json.dumps(kwargs))
                chars = len(json.dumps(sent["messages"])) + len(json.dumps(sent.get("tools", [])))
                prompt = math.ceil(chars / CHARS_PER_TOKEN)
                outer.counted.append((prompt, sent["max_tokens"]))
                content, calls = outer.replies.pop(0) if outer.replies else ("done", None)
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content=content, tool_calls=calls),
                        finish_reason="stop",
                    )],
                    usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=5),
                )

        self.chat = SimpleNamespace(completions=_Completions())


def _read_docs(*names):
    return [
        SimpleNamespace(function=SimpleNamespace(
            name="read_docs", arguments=json.dumps({"command": name})
        ))
        for name in names
    ]


def _agent(monkeypatch, tmp_path, server, *, ctx, max_tokens, steps):
    monkeypatch.setenv("AISH_LOCAL_URL", "http://mi.lan:8080/v1")
    monkeypatch.setenv("AISH_LOCAL_CTX", str(ctx))
    monkeypatch.setenv("AISH_LOCAL_MAX_TOKENS", str(max_tokens))
    chat, _, _ = make_chat(KEY, client=server)
    agent = Agent(
        model=REPO, approve=lambda _c: True, client_chat=chat, cwd=str(tmp_path),
        state_dir=str(tmp_path / "state"), step_log=steps.append,
    )
    agent.provider = "local"
    return agent


def _fixed_prefix_tokens(monkeypatch, tmp_path) -> int:
    """What the system prompt and tool menu alone cost at the fake's density,
    so a window can be chosen that holds them and not much more."""
    server = CountingServer([("hi", None)])
    agent = _agent(monkeypatch, tmp_path / "probe", server, ctx=10**7, max_tokens=10, steps=[])
    agent.run_task("hi")
    token_ratio.reset()
    return server.counted[0][0]


def _big_page(command, topic=None):
    return f"{command}: " + ("lorem ipsum dolor sit amet " * 1200)  # ~32k chars


class TestTheWindowIsTheWholeRequest:
    def test_prompt_plus_answer_cap_never_exceeds_the_window(self, monkeypatch, tmp_path):
        """Done-when #1, as the server counted it, across tasks whose tool
        results alone are several times the window."""
        prefix = _fixed_prefix_tokens(monkeypatch, tmp_path)
        max_tokens = 2_000
        ctx = prefix + max_tokens + 30_000
        monkeypatch.setattr(agent_module.tools, "read_docs", _big_page)
        replies = []
        for _task in range(5):
            replies += [("", _read_docs("a", "b")), ("", _read_docs("c")), ("done", None)]
        server = CountingServer(replies)
        steps: list[dict] = []
        agent = _agent(monkeypatch, tmp_path, server, ctx=ctx, max_tokens=max_tokens, steps=steps)
        for task in range(5):
            agent.run_task(f"read the docs, round {task}")

        assert len(server.counted) == 15
        for prompt, cap in server.counted:
            assert prompt + cap <= ctx
        # Not vacuous: the pages would have overrun the window many times over.
        assert any(s["kind"] == "trim" for s in steps)
        assert not [s for s in steps if s.get("policy") == "over_budget"]

    def test_each_call_records_its_estimate_beside_the_count(self, monkeypatch, tmp_path):
        """Done-when #3: the `reasoning` record carries both numbers."""
        server = CountingServer([("", _read_docs("ls")), ("done", None)])
        steps: list[dict] = []
        agent = _agent(monkeypatch, tmp_path, server, ctx=10**6, max_tokens=1_000, steps=steps)
        monkeypatch.setattr(agent_module.tools, "read_docs", lambda command, topic=None: "doc")
        agent.run_task("list")

        first, second = [s for s in steps if s["kind"] == "reasoning"]
        assert first["tokens"][0] == server.counted[0][0]
        assert first["prompt_estimate"]["basis"] == "ratio"
        assert first["prompt_estimate"]["ratio_source"].startswith("constant:")
        # The first count anchored the second estimate: exact plus the growth.
        assert second["prompt_estimate"]["basis"] == "anchored"
        assert second["prompt_estimate"]["ratio_source"] == "learned:min_of_1"
        assert second["tokens"][0] <= second["prompt_estimate"]["tokens"]

    def test_the_answer_cap_must_fit_inside_the_window(self, monkeypatch):
        monkeypatch.setenv("AISH_LOCAL_URL", "http://mi.lan:8080/v1")
        monkeypatch.setenv("AISH_LOCAL_CTX", "16384")
        monkeypatch.setenv("AISH_LOCAL_MAX_TOKENS", "16384")
        with pytest.raises(BackendError, match="AISH_LOCAL_MAX_TOKENS=16384"):
            make_chat(KEY, client=CountingServer([]))

    def test_the_budget_reserves_the_answer_and_a_margin(self, monkeypatch):
        monkeypatch.setenv("AISH_LOCAL_CTX", "98304")
        monkeypatch.setenv("AISH_LOCAL_MAX_TOKENS", "16384")
        agent = Agent(model=REPO, approve=lambda _c: True, client_chat=lambda **_: None)
        agent.provider = "local"
        budget, source = agent._history_budget()
        assert budget == int((98_304 - 16_384) * agent_module.LOCAL_PROMPT_SAFETY)
        assert source.startswith("backend:local:98304-max_tokens:16384")


class TestHysteresis:
    def test_a_trim_cuts_to_low_water_and_small_growth_does_not_trim_again(
        self, monkeypatch, tmp_path
    ):
        """Every trim rewrites the prompt prefix, which on Qwen3.6 re-reads
        the whole conversation — so a trim goes well under the budget and the
        next few messages fit without another one."""
        prefix = _fixed_prefix_tokens(monkeypatch, tmp_path)
        max_tokens = 2_000
        ctx = prefix + max_tokens + 30_000
        server = CountingServer([("", _read_docs("a")), ("done", None), ("done", None)])
        steps: list[dict] = []
        agent = _agent(monkeypatch, tmp_path, server, ctx=ctx, max_tokens=max_tokens, steps=steps)
        monkeypatch.setattr(agent_module.tools, "read_docs", _big_page)
        agent.run_task("read one page")  # anchors, and leaves real tool history
        for name in "bcdefg":
            agent.messages.append({"role": "user", "content": f"and {name}?"})
            agent.messages.append({"role": "tool", "tool_name": "read_docs",
                                   "content": _big_page(name)})
        steps.clear()
        agent.run_task("summarise")

        (trim,) = [s for s in steps if s["kind"] == "trim" and s["affected"]]
        assert trim["policy"] == "budget_oldest_first"
        assert trim["unit"] == "tokens"
        assert trim["estimate_before"] > trim["budget"]
        assert trim["low_water"] == int(trim["budget"] * agent_module.LOCAL_LOW_WATER)
        assert trim["estimate_after"] <= trim["low_water"]

        steps.clear()
        agent.run_task("one more small thing " * 20)
        assert not [s for s in steps if s["kind"] == "trim" and s["affected"]]
        for prompt, cap in server.counted:
            assert prompt + cap <= ctx


class TestTheSecondLever:
    def _history_of_talk(self, agent, turns, chars):
        for i in range(turns):
            agent.messages.append({"role": "user", "content": f"q{i} " + "w" * chars})
            agent.messages.append({"role": "assistant", "content": f"a{i} " + "v" * chars})

    def test_earlier_turns_are_cut_when_no_tool_output_is_left(self, monkeypatch, tmp_path):
        """The session that filed #415: every tool output was already a stub
        and the owner's messages and the replies alone outgrew the budget."""
        prefix = _fixed_prefix_tokens(monkeypatch, tmp_path)
        max_tokens = 2_000
        ctx = prefix + max_tokens + 20_000
        server = CountingServer([("done", None)])
        steps: list[dict] = []
        agent = _agent(monkeypatch, tmp_path, server, ctx=ctx, max_tokens=max_tokens, steps=steps)
        agent.messages.append({"role": "system", "content": "an earlier reminder " + "r" * 4000})
        self._history_of_talk(agent, turns=12, chars=12_000)
        task_start = len(agent.messages)

        agent.run_task("and now?")

        (prompt, cap), = server.counted
        assert prompt + cap <= ctx
        trims = [s for s in steps if s["kind"] == "trim" and s["policy"] == "turns_oldest_first"]
        assert trims and trims[0]["affected"] >= 1
        stubbed_at = [ref["at"] for ref in trims[0]["stubbed"]]
        assert stubbed_at == sorted(stubbed_at), "oldest first"
        assert all(0 < at < task_start for at in stubbed_at)
        for at in stubbed_at:
            message = agent.messages[at]
            assert message["_stub"] is True
            assert "read_tool_output" in message["content"]  # the text is cached
        system = [m for m in agent.messages if m["role"] == "system"]
        assert all(not m.get("_stub") for m in system), "system messages are never cut"
        assert all(not m.get("_stub") for m in agent.messages[task_start:])
        assert any(m.get("content") == "and now?" for m in agent.messages[task_start:])

    def test_a_window_nothing_can_fit_says_so_once_and_still_sends(self, monkeypatch, tmp_path):
        """The system prompt and menu alone over the budget: nothing is left
        to cut, the call goes out, and the record states the estimate."""
        server = CountingServer([("", _read_docs("a")), ("done", None)])
        steps: list[dict] = []
        agent = _agent(monkeypatch, tmp_path, server, ctx=4_000, max_tokens=1_000, steps=steps)
        monkeypatch.setattr(agent_module.tools, "read_docs", lambda command, topic=None: "doc")
        assert agent.run_task("hi") == "done"

        over = [s for s in steps if s.get("policy") == "over_budget"]
        assert len(over) == 1, "once per task, not once per call"
        assert over[0]["fits"] is False
        assert over[0]["estimate_after"] > over[0]["budget"]
        assert len(server.counted) == 2


class TestTheAnchor:
    def test_a_model_switch_re_estimates_the_whole_request(self, monkeypatch, tmp_path):
        server = CountingServer([("one", None), ("two", None)])
        agent = _agent(monkeypatch, tmp_path, server, ctx=10**6, max_tokens=1_000, steps=[])
        agent.run_task("first")
        assert agent._prompt_estimate()["basis"] == "anchored"
        agent.model = "another-model"
        estimate = agent._prompt_estimate()
        assert estimate["basis"] == "ratio"
        assert estimate["ratio_source"].startswith("constant:"), "another tokenizer"

    def test_a_rewrite_of_history_drops_the_anchor(self, monkeypatch, tmp_path):
        server = CountingServer([("one", None)])
        agent = _agent(monkeypatch, tmp_path, server, ctx=10**6, max_tokens=1_000, steps=[])
        agent.messages.append({"role": "tool", "tool_name": "read_docs", "content": "z" * 5000})
        agent.run_task("first")
        assert agent._token_anchor is not None
        agent.messages.append({"role": "user", "content": "q" * 9000})  # net growth
        page = next(m for m in agent.messages if m.get("role") == "tool")
        assert agent._trim_tool_message(page) is not None
        assert agent._token_anchor is None
        assert agent._prompt_estimate()["basis"] == "ratio"


    @pytest.mark.parametrize("rewrite", ["reset", "rewind", "redact", "system_prompt_changed"])
    def test_every_rewrite_of_what_was_sent_drops_the_anchor(
        self, monkeypatch, tmp_path, rewrite
    ):
        """A removal followed by growth would otherwise keep the removed
        text's tokens inside the anchor while subtracting its characters at
        the learned ratio — an under-count whenever it was denser (#415)."""
        server = CountingServer([("one", None)])
        agent = _agent(monkeypatch, tmp_path, server, ctx=10**6, max_tokens=1_000, steps=[])
        agent.run_task("first question")
        assert agent._token_anchor is not None
        if rewrite == "reset":
            agent.reset()
        elif rewrite == "rewind":
            assert agent.rewind_last_task() == "first question"
        elif rewrite == "redact":
            assert agent.redact_turn("first question")
        else:
            agent._set_system_content(agent.messages[0]["content"] + " changed")
        assert agent._token_anchor is None

    def test_an_identical_system_prompt_keeps_the_anchor(self, monkeypatch, tmp_path):
        """Rebuilt every task; only a CHANGED text is a rewrite."""
        server = CountingServer([("one", None)])
        agent = _agent(monkeypatch, tmp_path, server, ctx=10**6, max_tokens=1_000, steps=[])
        agent.run_task("first")
        agent._set_system_content(agent.messages[0]["content"])
        assert agent._token_anchor is not None


class TestAContinuationKeepsItsQuestion:
    def test_the_mid_task_turn_lever_stops_before_the_question(self, monkeypatch, tmp_path):
        """A continuation's question sits before `task_start` (#387); cutting
        it mid-task would leave the model executing a request it can no
        longer read."""
        steps: list[dict] = []
        agent = _agent(monkeypatch, tmp_path, CountingServer([]), ctx=6_000, max_tokens=1_000,
                       steps=steps)
        agent.messages.append({"role": "user", "content": "old talk " + "o" * 20_000})
        question = len(agent.messages)
        agent.messages.append({"role": "user", "content": "the question " + "q" * 20_000})
        agent.messages.append({"role": "assistant", "content": "partial " + "p" * 20_000})
        task_start = len(agent.messages)
        agent.messages.append({"role": "user", "content": "[aish: continue]"})

        agent._enforce_budget(task_start, protect_from=question)

        assert agent.messages[1].get("_stub") is True, "older talk is fair game"
        assert not agent.messages[question].get("_stub")
        assert not agent.messages[question + 1].get("_stub")


class TestAStubIsNeverStubbedAgain:
    def test_a_stub_with_its_key_is_not_trimmable(self, tmp_path):
        """A stub (200 chars + the recoverable note) is longer than the bound
        `_trimmable` checked, so every pass re-stubbed it: the cache then held
        the stub and the new key pointed at a 200-char fragment (#415)."""
        agent = Agent(
            model=REPO, approve=lambda _c: True, client_chat=lambda **_: None,
            cwd=str(tmp_path), state_dir=str(tmp_path / "state"),
        )
        message = {"role": "tool", "tool_name": "read_docs", "content": "p" * 5000}
        key = agent._trim_tool_message(message)
        assert key
        assert len(message["content"]) > agent_module.TRIM_KEEP_CHARS + len(
            agent_module.TRIMMED_NOTE
        )
        assert agent._trim_tool_message(message) is None
        assert key in message["content"]


class TestTheRatioLedger:
    def test_an_unseen_model_gets_the_cautious_default(self):
        ratio = token_ratio.ratio("local:never-seen")
        assert ratio.chars_per_token == token_ratio.DEFAULT_CHARS_PER_TOKEN <= 2.0
        assert ratio.source.startswith("constant:DEFAULT_CHARS_PER_TOKEN")

    def test_the_lowest_recent_ratio_is_used(self):
        token_ratio.observe(KEY, 40_000, 10_000)  # 4.0
        token_ratio.observe(KEY, 33_000, 10_000)  # 3.3
        token_ratio.observe(KEY, 38_000, 10_000)  # 3.8
        assert token_ratio.ratio(KEY).chars_per_token == pytest.approx(3.3)
        assert token_ratio.ratio(KEY).source == "learned:min_of_3"

    def test_it_is_per_model(self):
        token_ratio.observe(KEY, 40_000, 10_000)
        assert token_ratio.ratio("local:other").source.startswith("constant:")

    def test_it_survives_a_restart_and_seeds_a_new_chat(self, monkeypatch, tmp_path):
        token_ratio.observe(KEY, 36_000, 10_000)
        token_ratio.reset()  # a new process
        assert token_ratio.ratio(KEY).chars_per_token == pytest.approx(3.6)
        agent = Agent(model=REPO, approve=lambda _c: True, client_chat=lambda **_: None)
        agent.provider = "local"
        assert agent._prompt_estimate()["chars_per_token"] == pytest.approx(3.6)

    def test_only_the_recent_samples_are_kept(self):
        token_ratio.observe(KEY, 20_000, 10_000)  # 2.0, the oldest
        for _ in range(token_ratio.SAMPLES_KEPT):
            token_ratio.observe(KEY, 40_000, 10_000)
        assert token_ratio.ratio(KEY).chars_per_token == pytest.approx(4.0)

    def test_an_implausible_report_is_not_learned(self):
        token_ratio.observe(KEY, 1_000_000, 10)  # 100,000 chars a token
        token_ratio.observe(KEY, 10, 1_000)  # 0.01
        token_ratio.observe(KEY, 1_000, 0)
        assert token_ratio.ratio(KEY).source.startswith("constant:")

    @pytest.mark.parametrize("content", [
        "{not json", "[]", '{"local:x": "nope"}', '{"local:x": [[1, "a"], [3]]}',
    ])
    def test_an_unreadable_ledger_is_nothing_learned(self, content):
        path = token_ratio._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        token_ratio.reset()
        assert token_ratio.ratio("local:x").source.startswith("constant:")
        token_ratio.observe("local:x", 35_000, 10_000)  # and writing still works
        token_ratio.reset()
        assert token_ratio.ratio("local:x").chars_per_token == pytest.approx(3.5)

    def test_an_unwritable_ledger_never_raises(self, monkeypatch, tmp_path):
        blocker = tmp_path / "a-file"
        blocker.write_text("")
        monkeypatch.setenv("AISH_STATE_DIR", str(blocker / "state"))
        token_ratio.reset()
        token_ratio.observe(KEY, 35_000, 10_000)
        assert token_ratio.ratio(KEY).chars_per_token == pytest.approx(3.5)


class TestOtherProvidersAreUnchanged:
    def test_ollama_still_budgets_characters(self):
        agent = Agent(model="qwen3:8b", approve=lambda _c: True, client_chat=lambda **_: None,
                      num_ctx=8192)
        assert agent._history_budget() == (
            8192 * agent_module.CHARS_PER_TOKEN_BUDGET, "num_ctx:8192"
        )
        assert agent._history_size() == agent._total_chars()
        assert agent._low_water(1000) == 1000

    def test_the_default_window_is_the_whole_request(self):
        assert backends.context_window("local") == (98_304, "backend:local:98304")
