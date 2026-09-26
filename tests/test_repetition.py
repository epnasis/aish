"""A streamed reply that has become one passage repeated is stopped (#417).

The detector is pure (`aish/repetition.py`); the agent side is exercised with a
scripted streaming chat — no model, no network — shaped like the recorded
case: `session-20260925-204008-294943`, turn 22, call 1, whose reasoning ended
with one 76-character passage 562 times and ran 375 s to the 16,384-token cap.
"""

import itertools
from types import SimpleNamespace

import pytest

from aish import agent as agent_module
from aish import repetition
from aish.agent import AISH_NOTE, Agent

# The four lines that repeated, verbatim from the recorded reasoning.
LOOP = "I'll start now.\nI'll read the skill.\nThen I'll update it.\nThen I'll run it.\n"
# Its opening, abridged: ordinary planning prose before the loop set in.
PREAMBLE = (
    "The user is impatient. I should just provide the solution directly.\n"
    "Steps:\n1. Read the full skill content.\n2. Modify the Python code to use "
    "`aish secret get` to fetch keys.\n3. Update the skill using `create_skill`.\n"
    "4. Run the script.\n\nLet's do this. I'll read the skill, update it, and run it.\n"
) * 20


def _feed(text: str, piece: int = 4) -> repetition.Repetition | None:
    """Stream `text` through a watch the way a server sends tokens: a few
    characters at a time. The first verdict, or None."""
    watch = repetition.RepetitionWatch()
    for start in range(0, len(text), piece):
        if (found := watch.feed(text[start:start + piece])) is not None:
            return found
    return None


class TestRepetitionWatch:
    def test_the_recorded_loop_is_stopped_early(self):
        whole = PREAMBLE + LOOP * 563
        found = _feed(whole)
        assert found is not None
        assert found.period_chars == len(LOOP) == 76
        assert found.repeats >= repetition.MIN_REPEATS
        assert found.span_chars >= repetition.MIN_SPAN_CHARS
        # Early: well before a quarter of what the server generated.
        assert found.chars < len(PREAMBLE) + repetition.MIN_SPAN_CHARS + 2 * 256
        assert found.chars < len(whole) / 4
        assert found.passage in LOOP * 2

    def test_a_long_passage_is_stopped_at_its_tenth_copy(self):
        """The longest loop in the logs: a 1,189-character passage (turn 22,
        call 2), each line different, the block identical each time."""
        block = "".join(
            f"I will check the `rule-{n:02d}` rules before answering.\n" for n in range(24)
        )
        assert len(block) > 1000
        found = _feed(PREAMBLE + block * 50)
        assert found is not None
        assert found.period_chars == len(block)
        assert found.repeats == repetition.MIN_REPEATS

    def test_a_markdown_table_does_not_trip_it(self):
        rows = "\n".join(
            f"| host-{n:03d} | up | {10 + n % 7} ms | 2026-09-26 |" for n in range(800)
        )
        table = "| host | state | latency | checked |\n|---|---|---|---|\n" + rows
        assert len(table) > 3 * repetition.MIN_SPAN_CHARS
        assert _feed(table) is None

    def test_a_repetitive_log_dump_does_not_trip_it(self):
        """The same message on every line, as a log prints it: timestamps and
        sequence numbers make each line different, so the tail has no period."""
        log = "".join(
            f"2026-09-26T13:{n // 60 % 60:02d}:{n % 60:02d} WARN worker[3] retrying "
            f"connection to mi.lan:8080 (attempt {n})\n"
            for n in range(600)
        )
        assert _feed(log) is None

    def test_identical_log_lines_below_the_span_do_not_trip_it(self):
        """Even lines with nothing varying: a hundred of them is 2.6k characters,
        under the span a loop must cover."""
        assert _feed("WARN retrying connection\n" * 100) is None

    def test_code_with_repeated_lines_does_not_trip_it(self):
        body = "".join(
            f"    def test_case_{n}(self):\n        self.assertEqual(run({n}), {n * n})\n\n"
            for n in range(300)
        )
        zeros = "BUFFER = [" + "0x00, " * 500 + "]\n"
        code = "import unittest\n\n\nclass T(unittest.TestCase):\n" + body + zeros
        assert _feed(code) is None

    def test_a_numbered_list_does_not_trip_it(self):
        items = "".join(f"{n}. Then I'll update item {n}.\n" for n in range(1, 800))
        assert _feed(items) is None

    def test_the_verdict_is_a_count_never_a_cause(self):
        found = _feed(PREAMBLE + LOOP * 200)
        assert found is not None
        assert set(found.record()) == {"period_chars", "repeats", "span_chars", "chars", "passage"}


# ------------------------------------------------------------------ the agent


class Streams:
    """A streaming chat: each call yields the next scripted stream, and every
    stream records whether it was closed — the connection it stands for."""

    def __init__(self, *streams):
        self.streams = list(streams)
        self.calls: list[dict] = []
        self.closed: list[bool] = []
        self.pulled: list[int] = []

    def __call__(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages") or [])})
        chunks = self.streams.pop(0)
        index = len(self.closed)
        self.closed.append(False)
        self.pulled.append(0)

        def generate():
            try:
                for chunk in chunks:
                    self.pulled[index] += 1
                    yield chunk
            finally:
                self.closed[index] = True

        return generate()


def _chunk(content: str = "", thinking: str = ""):
    message = SimpleNamespace(content=content, tool_calls=None)
    if thinking:
        message.thinking = thinking
    return SimpleNamespace(message=message)


def _looping(channel: str):
    """An endless stream in one channel: the preamble, then the loop forever."""
    def pieces():
        yield PREAMBLE
        while True:
            yield LOOP
    for piece in pieces():
        for start in range(0, len(piece), 5):
            yield _chunk(**{channel: piece[start:start + 5]})


def _answer(text: str):
    return [_chunk(content=text)]


def _agent(chat: Streams, steps: list, tokens: list | None = None):
    return Agent(
        model="fake", approve=lambda _c: True, client_chat=chat,
        on_token=(tokens.append if tokens is not None else lambda _t: None),
        step_log=steps.append,
    )


class TestARepeatingReplyIsStopped:
    def test_the_call_ends_and_the_model_is_asked_again_saying_what_was_seen(self):
        chat = Streams(_looping("thinking"), _answer("here is the answer"))
        steps: list[dict] = []
        agent = _agent(chat, steps)
        assert agent.run_task("update the skill") == "here is the answer"

        # The endless stream was stopped, and its connection closed.
        assert chat.closed[0] is True
        assert chat.pulled[0] < (len(PREAMBLE) + 2 * repetition.MIN_SPAN_CHARS) / 5
        # The re-ask says what aish observed and did — never why.
        asked = chat.calls[1]["messages"][-1]
        assert asked["role"] == "user" and asked["content"].startswith(AISH_NOTE)
        assert "aish stopped your last reply" in asked["content"]
        assert "76-character passage repeated" in asked["content"]
        # The request that looped is never sent again unchanged.
        assert chat.calls[1]["messages"] != chat.calls[0]["messages"]

    def test_the_reasoning_record_carries_the_observation(self):
        chat = Streams(_looping("thinking"), _answer("ok"))
        steps: list[dict] = []
        _agent(chat, steps).run_task("go")
        first = [s for s in steps if s.get("kind") == "reasoning"][0]
        seen = first["repetition"]
        assert seen["period_chars"] == 76
        assert seen["repeats"] >= repetition.MIN_REPEATS
        assert seen["chars"] >= seen["span_chars"]
        assert seen["chunks"] > 0 and seen["secs"] >= 0
        # No finish reason was received, and none is made up.
        assert "stop" not in first
        second = [s for s in steps if s.get("kind") == "reasoning"][1]
        assert "repetition" not in second

    def test_a_second_loop_ends_the_turn_with_what_was_observed(self):
        chat = Streams(_looping("thinking"), _looping("thinking"))
        steps: list[dict] = []
        result = _agent(chat, steps).run_task("go")
        assert len(chat.calls) == 2  # one re-ask, never a third call
        assert all(chat.closed)
        assert "aish stopped it after" in result
        assert "aish stopped that reply too after" in result
        assert "76-character passage" in result

    @pytest.mark.parametrize("answer", [
        "| 0 | 0 | 0 | 0 |\n" * 1000,        # identical table rows
        "[" + "0, " * 4000 + "]",            # a zero matrix
        "-" * 8000,                          # a rule the owner asked for
        "INSERT INTO t VALUES (1);\n" * 500,  # identical statements
    ])
    def test_an_answer_that_repeats_itself_is_never_stopped(self, answer):
        """Answer text is not watched: it streams to the owner, who has Stop,
        and it may repeat itself exactly on purpose."""
        pieces = [_chunk(content=answer[i:i + 5]) for i in range(0, len(answer), 5)]
        chat = Streams(pieces)
        steps: list[dict] = []
        result = _agent(chat, steps).run_task("go")
        assert result == answer
        assert chat.pulled[0] == len(pieces)
        assert not any("repetition" in s for s in steps if s.get("kind") == "reasoning")

    def test_a_close_that_fails_on_stop_does_not_become_a_model_error(self):
        """The owner's Stop stays a Stop: a failing close must not replace it."""
        steps: list[dict] = []

        class BadClose:
            def __init__(self):
                self.pulled = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.pulled += 1
                return _chunk(thinking=LOOP)

            def close(self):
                raise RuntimeError("close blew up")

        stream = BadClose()
        agent = Agent(model="fake", approve=lambda _c: True, client_chat=lambda **_: stream,
                      on_token=lambda _t: None, step_log=steps.append)
        original = agent.status.add_tokens

        def stop_after_some(n):
            if stream.pulled > 3:
                agent._cancel.set()
            original(n)

        agent.status.add_tokens = stop_after_some
        assert agent.run_task("go") == agent_module.CANCELLED_RESULT
        assert not [s for s in steps if s.get("kind") == "model_error"]

    def test_a_close_that_fails_after_a_whole_reply_is_recorded_not_raised(self):
        class BadClose:
            def __init__(self):
                self.chunks = iter([_chunk(content="fine")])

            def __iter__(self):
                return self

            def __next__(self):
                return next(self.chunks)

            def close(self):
                raise RuntimeError("close blew up")

        steps: list[dict] = []
        agent = Agent(model="fake", approve=lambda _c: True,
                      client_chat=lambda **_: BadClose(), on_token=lambda _t: None,
                      step_log=steps.append)
        assert agent.run_task("go") == "fine"
        (record,) = [s for s in steps if s.get("kind") == "reasoning"]
        assert record["close_error"] == "RuntimeError: close blew up"

    def test_ordinary_streams_are_untouched(self):
        chat = Streams(_answer("| a | b |\n|---|---|\n| 1 | 2 |\n"))
        steps: list[dict] = []
        assert "aish stopped" not in _agent(chat, steps).run_task("table please")
        assert not any("repetition" in s for s in steps if s.get("kind") == "reasoning")

    def test_a_stop_pressed_mid_stream_closes_the_connection_too(self):
        chat = Streams(_looping("content"))
        steps: list[dict] = []
        agent = _agent(chat, steps)
        original = agent.status.add_tokens

        def stop_after_some(n):
            if chat.pulled[0] > 50:
                agent._cancel.set()
            original(n)

        agent.status.add_tokens = stop_after_some
        assert agent.run_task("go") == agent_module.CANCELLED_RESULT
        assert chat.closed[0] is True


    def test_explain_names_it_on_the_call_it_stopped(self, tmp_path):
        """What `aish explain` says is the record's counts, cited on the
        response pane of the call that was stopped."""
        import json

        from aish import explain

        seen = {"period_chars": 76, "repeats": 79, "span_chars": 6076, "chars": 10752,
                "chunks": 2150, "secs": 60.1, "passage": LOOP}
        path = tmp_path / "session-loop.jsonl"
        records = [
            {"ts": "t", "kind": "task_start", "prompt": "go"},
            {"ts": "t", "kind": "trace",
             "step": {"kind": "reasoning", "model_call": 1, "text": "x", "tokens": [0, 0],
                      "repetition": seen}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        log = explain.load(path)
        doc = explain.dossier(log.turns[0], log, tmp_path)
        (row,) = [r for r in doc["notes"]["rows"] if r["check"] == "reply_repeating"]
        assert (row["where"]["step"], row["where"]["pane"]) == ("m1", "response")
        assert "76-character passage repeated 79 times" in row["text"]




@pytest.mark.parametrize("piece", [1, 3, 64])
def test_the_verdict_does_not_depend_on_how_the_text_was_chunked(piece):
    found = _feed(PREAMBLE + LOOP * 300, piece=piece)
    assert found is not None and found.period_chars == 76


def test_the_detector_is_cheap_on_ordinary_text():
    """Checked every CHECK_EVERY_CHARS, over a bounded window: a long reply of
    ordinary prose costs a handful of substring searches per check."""
    prose = "".join(
        f"Paragraph {n}: the weather in city {n} is {n % 30} degrees. " for n in range(5000)
    )
    watch = repetition.RepetitionWatch()
    for piece in itertools.batched(prose, 4):
        assert watch.feed("".join(piece)) is None
    assert watch.chars == len(prose)
