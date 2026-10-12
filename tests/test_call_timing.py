"""How long each acting-loop model call took (#444): `ms` and `first_chunk_ms`
on the `reasoning` record, measured at the backend seam (`backends.CallTiming`).

The clock is `backends._clock`, driven by hand here: every fake backend moves
it by a known amount at a known point, so each figure is asserted exactly.
"""

import json
from types import SimpleNamespace

import pytest

from aish import backends, explain, replay, replay_checks, tooluse, usage
from aish.agent import Agent
from aish.backends import CALL_MS, FIRST_CHUNK_MS, AnthropicBackend, OpenAICompatBackend
from aish.session import SessionLog


class Clock:
    """Reads `now`; moves only when a fake backend says so."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(backends, "_clock", fake)
    return fake


def chunk(content="", prompt=0, output=0):
    return SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=None),
        prompt_eval_count=prompt, eval_count=output,
    )


def ollama_like(clock, *, to_first=2.0, rest=0.5, replies=1):
    """The `ollama.chat` convention: 2 s before the first chunk, 0.5 s more to
    the last; a plain call takes the sum. `replies` is how many calls it serves."""
    calls = []

    def chat(*, model="", messages=None, stream=False, **_kw):
        calls.append(stream)
        if len(calls) > replies:
            raise AssertionError("more model calls than scripted")
        if not stream:
            clock.now += to_first + rest
            return chunk("done", prompt=50, output=5)

        def generate():
            clock.now += to_first
            yield chunk("do")
            clock.now += rest
            yield chunk("ne", prompt=50, output=5)

        return generate()

    return chat


def run_one(chat, tmp_path, *, stream: bool) -> list[dict]:
    """One task, one text-only answer; the `reasoning` records it wrote."""
    records: list[dict] = []
    agent = Agent(
        model="fake", approve=lambda _cmd: True, client_chat=chat, state_dir=tmp_path,
        step_log=records.append, on_token=(lambda _t: None) if stream else None,
    )
    agent.run_task("hello")
    return [r for r in records if r.get("kind") == "reasoning"]


class TestTheSeam:
    def test_nothing_observed_is_nothing_recorded(self):
        assert backends.CallTiming().record() == {}
        # A start with no end (a call still running) gives no duration.
        assert backends.CallTiming(started=1.0).record() == {}

    def test_marks_outside_an_observer_go_nowhere(self, clock):
        backends._mark("started")  # no observer on this thread: must not raise

    def test_the_earliest_mark_wins(self, clock):
        timing = backends.CallTiming()
        with backends.observe_timing(timing):
            backends._mark("first_chunk")
            clock.now += 5
            backends._mark("first_chunk")
        assert timing.first_chunk == 1000.0


class TestTheActingLoop:
    def test_a_streamed_call_records_duration_and_first_chunk(self, clock, tmp_path):
        chat = backends.governed(ollama_like(clock), "ollama")
        (record,) = run_one(chat, tmp_path, stream=True)
        assert record[CALL_MS] == 2500
        assert record[FIRST_CHUNK_MS] == 2000
        assert record["model_call"] == 1

    def test_a_plain_call_has_no_first_chunk_absent_not_zero(self, clock, tmp_path):
        chat = backends.governed(ollama_like(clock), "ollama")
        (record,) = run_one(chat, tmp_path, stream=False)
        assert record[CALL_MS] == 2500
        assert FIRST_CHUNK_MS not in record

    def test_an_unwrapped_chat_is_not_observed_so_nothing_is_written(self, clock, tmp_path):
        (record,) = run_one(ollama_like(clock), tmp_path, stream=True)
        assert CALL_MS not in record and FIRST_CHUNK_MS not in record

    def test_a_wait_for_rate_limit_headroom_is_not_the_models_time(
        self, clock, tmp_path, monkeypatch
    ):
        def reserve(_key, _messages):
            clock.now += 30.0  # queued for headroom
            return SimpleNamespace(settle=lambda _n: None, rejected=lambda: None)

        monkeypatch.setattr(backends.ratelimit, "reserve_for_call", reserve)
        chat = backends.governed(ollama_like(clock), "ollama")
        (record,) = run_one(chat, tmp_path, stream=True)
        assert (record[CALL_MS], record[FIRST_CHUNK_MS]) == (2500, 2000)

    def test_only_the_attempt_that_succeeded_is_timed(self, clock, tmp_path, monkeypatch):
        """A failed first attempt took 7 s; the record is the retry's."""
        inner = ollama_like(clock)
        failed = []

        def flaky(**kw):
            if not failed:
                failed.append(True)
                clock.now += 7.0
                raise ConnectionResetError("reset")
            return inner(**kw)

        monkeypatch.setattr(backends.ratelimit, "wait", lambda *_a, **_k: False)
        (record,) = run_one(backends.governed(flaky, "ollama"), tmp_path, stream=True)
        assert (record[CALL_MS], record[FIRST_CHUNK_MS]) == (2500, 2000)


def _delta(content=None, finish_reason=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
                           usage=usage)


class TestTheAdapters:
    def test_openai_stamps_the_raw_first_chunk_before_its_filter(self, clock):
        """A role-only first delta yields nothing to the caller — it still
        arrived, and that is the first chunk."""

        def stream():
            clock.now += 3.0
            yield _delta(content=None)          # role-only: filtered out
            clock.now += 1.0
            yield _delta(content="hi")
            clock.now += 1.0
            yield _delta(finish_reason="stop")

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kw: stream())))
        chat = backends.governed(OpenAICompatBackend(client, backends.LOCAL), backends.LOCAL)
        timing = backends.CallTiming()
        with backends.observe_timing(timing):
            out = list(chat(model="m", messages=[], stream=True))
        assert [c.message.content for c in out if c.message.content] == ["hi"]
        assert timing.record() == {CALL_MS: 5000, FIRST_CHUNK_MS: 3000}

    def test_anthropic_stamps_the_first_event_even_with_no_text(self, clock):
        """A reply that is only a tool call streams no text at all; the first
        EVENT is the first chunk, and text deltas still reach the caller."""
        final = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok",
                                     model_dump=lambda **_k: {"type": "text", "text": "ok"})],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=5, output_tokens=2,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def __iter__(self):
                clock.now += 4.0
                yield SimpleNamespace(type="message_start")
                clock.now += 1.0
                yield SimpleNamespace(type="content_block_delta",
                                      delta=SimpleNamespace(type="text_delta", text="ok"))
                clock.now += 1.0
                yield SimpleNamespace(type="message_stop")

            def get_final_message(self):
                return final

        client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **_kw: Stream()))
        chat = backends.governed(AnthropicBackend(client), "claude")
        timing = backends.CallTiming()
        with backends.observe_timing(timing):
            out = list(chat(model="m", messages=[{"role": "user", "content": "u"}],
                            stream=True))
        assert [c.message.content for c in out if c.message.content] == ["ok"]
        assert timing.record() == {CALL_MS: 6000, FIRST_CHUNK_MS: 4000}


def _log(tmp_path, chat, *, stream=True) -> SessionLog:
    log = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    agent = Agent(
        model="fake", approve=lambda _cmd: True, client_chat=chat, state_dir=tmp_path,
        on_message=log.message, step_log=log.step,
        on_token=(lambda _t: None) if stream else None,
    )
    agent.run_task("hello")
    return log


class TestTheReaders:
    """`aish explain`, `aish usage`, `aish tooluse` and the replay's context
    reader all read a log with the new keys and a log without them."""

    @pytest.mark.parametrize("governed", [True, False], ids=["timed", "untimed"])
    def test_every_reader_parses(self, clock, tmp_path, governed):
        chat = ollama_like(clock)
        log = _log(tmp_path, backends.governed(chat, "ollama") if governed else chat)
        assert explain.explain(log.path, root=tmp_path)
        assert usage.scan_session(log.path).calls
        assert tooluse.scan(root=tmp_path) is not None
        context = replay_checks.read_context(log.path)
        assert context.calls == 1 and context.calls_at == {"first": 1, "later": 0}
        assert context.ms["first"] == ([2500] if governed else [])
        assert context.first_chunk_ms["first"] == ([2000] if governed else [])


def _reasoning(n, **timing) -> dict:
    return {"kind": "trace", "step": {"kind": "reasoning", "model_call": n,
                                      "tokens": [0, 0], **timing}}


def _write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


class TestTheSplit:
    def test_first_call_of_each_task_is_split_from_the_later_ones(self, tmp_path):
        path = _write(tmp_path / "s.jsonl", [
            _reasoning(1, ms=9000, first_chunk_ms=8000),
            _reasoning(2, ms=1000, first_chunk_ms=200),
            _reasoning(3, ms=1200),                 # did not stream
            _reasoning(1, ms=7000, first_chunk_ms=6000),
            _reasoning(2),                          # not observed at all
            {"kind": "trace", "step": {"kind": "reasoning", "tokens": [0, 0], "ms": 5}},
        ])
        context = replay_checks.read_context(path)
        assert context.calls == 6
        assert context.calls_at == {"first": 2, "later": 3}
        assert context.position_absent == 1
        assert context.ms == {"first": [9000, 7000], "later": [1000, 1200]}
        assert context.first_chunk_ms == {"first": [8000, 6000], "later": [200]}
        timing = replay.context_summary([context], 60_000)["timing"]
        assert timing["first"]["ms"] == {"recorded": 2, "not_recorded": 0,
                                         "median": 9000, "p95": 9000}
        assert timing["later"]["first_chunk_ms"] == {"recorded": 1, "not_recorded": 2,
                                                     "median": 200, "p95": 200}
        assert timing["position_not_recorded"] == 1
        line = replay._timing_line(timing, context.calls)
        assert line == ("  time median/p95 · first 2: 9.0s/9.0s, chunk 8.0s/8.0s · "
                        "later 3: 1.2s/1.2s (1 not recorded), chunk 0.2s/0.2s "
                        "(2 not recorded) · 1 unplaced")

    def test_a_log_with_no_timing_says_so_in_words(self, tmp_path):
        path = _write(tmp_path / "s.jsonl", [_reasoning(1), _reasoning(2)])
        context = replay_checks.read_context(path)
        timing = replay.context_summary([context], 60_000)["timing"]
        assert timing["first"]["ms"] == {"recorded": 0, "not_recorded": 1}
        assert replay._timing_line(timing, 2) == "  time not recorded on any of 2 calls"
