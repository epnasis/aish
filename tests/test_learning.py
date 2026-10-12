"""Learning suggestions (#446): a lesson chip on an answer whose task went the
long way round, decided from the task's own records. No model, no network:
the server and CLI paths run a scripted chat, and tool implementations are
stubbed at the module boundary like everywhere else in the suite.
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import pytest

from aish import learning, replay_checks
from aish.agent import learn_prompt
from aish.session import SessionLog
from tests.test_server import (
    FakeChat,
    TokenClient,
    connected,
    make_client,
    model_says,
    tool_call,
)

# ── records, synthetic ─────────────────────────────────────────────────────


def call(name: str, **args) -> dict:
    return {"kind": "trace", "step": {"kind": "call", "name": name, "args": args}}


def refusal(tool: str = "web_search") -> dict:
    return {"kind": "trace", "step": {"kind": "gate", "at": "gate", "gate": "rule.prohibit",
                                      "verdict": "refused", "tool": tool}}


def commented(comment: str = "not that one") -> dict:
    return {"kind": "trace", "step": {"kind": "tool", "name": "run_command",
                                      "decision": "denied", "comment": comment}}


def note(source: str) -> dict:
    return {"kind": "message", "role": "user", "content": "[aish: …]", "source": source}


class TestTheShape:
    """Each signal trips at its named threshold and not one below it."""

    def test_an_ordinary_task_trips_nothing(self):
        shape = learning.shape_of([call("read_url", url="a"), call("read_url", url="b")])
        assert shape.tripped() == []
        assert shape.counts() == {"calls": 2, "repeats": 0, "refusals": 0, "corrections": 0}

    def test_calls_trip_at_the_long_task_threshold(self):
        below = [call("read_docs", command=str(i)) for i in range(learning.LONG_TASK_CALLS - 1)]
        assert learning.shape_of(below).tripped() == []
        at = below + [call("read_docs", command="last")]
        assert learning.shape_of(at).tripped() == ["calls"]

    def test_repeats_count_calls_equal_to_an_earlier_one(self):
        # K repeats need K + 1 identical calls — the repeat nudge's own count.
        same = [call("run_command", command="trippy search")] * learning.REPEATED_CALLS_AT
        assert learning.shape_of(same).repeats == learning.REPEATED_CALLS_AT - 1
        assert learning.shape_of(same).tripped() == []
        assert learning.shape_of(same + same[:1]).tripped() == ["repeats"]

    def test_argument_order_is_not_a_difference(self):
        a = {"kind": "trace", "step": {"kind": "call", "name": "x", "args": {"p": 1, "q": 2}}}
        b = {"kind": "trace", "step": {"kind": "call", "name": "x", "args": {"q": 2, "p": 1}}}
        assert learning.shape_of([a, b]).repeats == 1

    def test_refusals_count_refused_tool_calls_only(self):
        arm = {"kind": "trace", "step": {"kind": "gate", "at": "gate", "gate": "stop_gate",
                                         "verdict": "refused", "tool": "", "round": 0}}
        allowed = {"kind": "trace", "step": {"kind": "gate", "at": "gate",
                                             "verdict": "allowed", "tool": "x"}}
        verify = {"kind": "trace", "step": {"kind": "gate", "at": "verify",
                                            "verdict": "refused", "tool": "x"}}
        shape = learning.shape_of([arm, allowed, verify, refusal()])
        assert shape.refusals == 1
        assert learning.shape_of([refusal(), refusal()]).tripped() == ["refusals"]

    def test_corrections_are_card_comments(self):
        assert learning.shape_of([commented()]).tripped() == []
        assert learning.shape_of([commented(), commented()]).tripped() == ["corrections"]

    @pytest.mark.parametrize("source", sorted(learning.STOP_SOURCES))
    def test_any_stop_trips(self, source):
        shape = learning.shape_of([note(source)])
        assert shape.stopped == source
        assert shape.tripped() == ["stopped"]

    def test_other_notes_are_not_stops(self):
        assert learning.shape_of([note("repeat_nudge"), note("answer_check")]).tripped() == []

    def test_superseded_records_are_not_read(self):
        dead = {**refusal(), "superseded": {"by": "owner"}}
        assert learning.shape_of([dead, dead]).refusals == 0


# ── the decision, on a real log ─────────────────────────────────────────────


def write_task(log: SessionLog, records: list[dict]) -> int:
    """Append one task's records; return the offset it started at."""
    start = learning.log_size(log.path)
    log.task_start("go")
    for record in records:
        if record["kind"] == "trace":
            log.step(record["step"])
        else:
            log.message({k: v for k, v in record.items() if k != "kind"})
    return start


LONG = [call("read_docs", command=str(i)) for i in range(learning.LONG_TASK_CALLS)]


class TestTheDecision:
    def test_an_ordinary_task_records_and_offers_nothing(self, tmp_path):
        log = SessionLog(tmp_path / "s.jsonl")
        start = write_task(log, [call("read_url", url="a")])
        decision = learning.evaluate(log.path, start, owner_sent=True)
        assert decision.record["tripped"] == [] and decision.chip is False
        assert "withheld" not in decision.record
        assert decision.record["counts"]["calls"] == 1
        assert decision.record["at"] == learning.THRESHOLDS

    def test_a_long_task_he_sent_gets_the_chip(self, tmp_path):
        log = SessionLog(tmp_path / "s.jsonl")
        start = write_task(log, LONG)
        decision = learning.evaluate(log.path, start, owner_sent=True)
        assert decision.chip and decision.record["tripped"] == ["calls"]

    def test_records_before_the_span_are_not_this_task(self, tmp_path):
        log = SessionLog(tmp_path / "s.jsonl")
        write_task(log, LONG)  # an earlier long task
        start = write_task(log, [call("read_url", url="a")])
        assert learning.evaluate(log.path, start, owner_sent=True).record["tripped"] == []

    @pytest.mark.parametrize(
        ("kwargs", "withheld"),
        [
            ({"owner_sent": False}, learning.NOT_OWNER_SENT),
            ({"owner_sent": True, "learn_task": True}, learning.LEARN_TASK),
            ({"owner_sent": True, "one_shot": True}, learning.ONE_SHOT),
            # The order is the vocabulary's: aish's own task outranks the rest.
            ({"owner_sent": False, "learn_task": True}, learning.NOT_OWNER_SENT),
        ],
    )
    def test_withheld_says_why(self, tmp_path, kwargs, withheld):
        log = SessionLog(tmp_path / "s.jsonl")
        start = write_task(log, LONG)
        decision = learning.evaluate(log.path, start, **kwargs)
        assert decision.chip is False
        assert decision.record["withheld"] == withheld
        assert decision.record["tripped"] == ["calls"]

    def test_never_twice_in_a_row(self, tmp_path):
        log = SessionLog(tmp_path / "s.jsonl")
        first = write_task(log, LONG)
        assert learning.at_answer(log.path, first, 1, log.step, owner_sent=True)
        second = write_task(log, LONG)
        assert not learning.at_answer(log.path, second, 2, log.step, owner_sent=True)
        third = write_task(log, LONG)
        # The previous evaluation withheld it, so this one may show it again.
        assert learning.at_answer(log.path, third, 3, log.step, owner_sent=True)
        steps = [r["step"] for r in map(json.loads, log.path.read_text().splitlines())
                 if r["kind"] == "trace" and r["step"]["kind"] == "learning"]
        assert [(s["turn"], s["chip"], s.get("withheld")) for s in steps] == [
            (1, True, None), (2, False, learning.COOLDOWN), (3, True, None),
        ]

    def test_an_ordinary_task_between_lifts_the_cooldown(self, tmp_path):
        log = SessionLog(tmp_path / "s.jsonl")
        assert learning.at_answer(log.path, write_task(log, LONG), 1, log.step, owner_sent=True)
        quiet = write_task(log, [call("read_url", url="a")])
        assert not learning.at_answer(log.path, quiet, 2, log.step, owner_sent=True)
        assert learning.at_answer(log.path, write_task(log, LONG), 3, log.step, owner_sent=True)

    def test_a_discarded_offer_does_not_cool_down(self, tmp_path):
        log = SessionLog(tmp_path / "s.jsonl")
        # Retry marks the RECORD (#339), never the step inside it.
        log._record("trace", step={"kind": "learning", "turn": 1, "chip": True},
                    superseded={"by": "owner"})
        start = write_task(log, LONG)
        assert learning.evaluate(log.path, start, owner_sent=True).chip


class TestItNeverReachesTheAnswer:
    def test_a_failure_to_evaluate_is_recorded_not_raised(self, tmp_path, monkeypatch):
        log = SessionLog(tmp_path / "s.jsonl")
        start = write_task(log, LONG)

        def broken(*_a, **_k):
            raise OSError("disk")

        monkeypatch.setattr(learning, "read_span", broken)
        assert learning.at_answer(log.path, start, 4, log.step, owner_sent=True) is False
        last = json.loads(log.path.read_text().splitlines()[-1])["step"]
        assert last == {"kind": "learning", "turn": 4, "chip": False, "unevaluated": "OSError"}

    def test_an_unwritable_log_shows_no_chip(self, tmp_path):
        log = SessionLog(tmp_path / "s.jsonl")
        start = write_task(log, LONG)

        def refuse(_record):
            raise OSError("moved")

        assert learning.at_answer(log.path, start, 1, refuse, owner_sent=True) is False

    def test_the_kill_switch_writes_and_offers_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv(learning.DISABLE_ENV, "0")
        log = SessionLog(tmp_path / "s.jsonl")
        start = write_task(log, LONG)
        size = log.path.stat().st_size
        assert learning.at_answer(log.path, start, 1, log.step, owner_sent=True) is False
        assert log.path.stat().st_size == size


class TestTheLearnTask:
    def test_every_expansion_is_recognised(self, tmp_path):
        assert learning.is_learn_prompt(learn_prompt(""))
        assert learning.is_learn_prompt(learn_prompt("the hotel search"))
        assert learning.is_learn_prompt(learn_prompt("lessons", tmp_path / "lessons.md"))

    def test_ordinary_text_is_not(self):
        assert not learning.is_learn_prompt("/learn")  # the typed form, before expansion
        assert not learning.is_learn_prompt("Review this conversation, please")
        assert not learning.is_learn_prompt("")


# ── the web: the real task path ─────────────────────────────────────────────


@pytest.fixture
def app_env(tmp_path):
    """The server suite's isolated files (tests/test_server.py `app_env`)."""
    allow, deny = tmp_path / "allow.txt", tmp_path / "deny.txt"
    allow.write_text("ls\n", encoding="utf-8")
    deny.write_text("rm -rf\n", encoding="utf-8")
    return {
        "state_dir": tmp_path / "state",
        "allow_path": allow,
        "deny_path": deny,
        "config_path": tmp_path / "config.toml",
        "lessons_path": tmp_path / "lessons.md",
        "cwd": str(tmp_path),
    }


def long_turn(n: int = learning.LONG_TASK_CALLS) -> list:
    """One model call issuing `n` distinct read-only calls, then an answer."""
    return [
        model_says(tool_calls=[tool_call("read_docs", command=f"cmd{i}") for i in range(n)]),
        model_says("All done."),
    ]


@pytest.fixture
def docs(monkeypatch):
    from aish import server as server_module

    counter = iter(range(10_000))
    # A distinct result per call, so the loop detector sees progress.
    monkeypatch.setattr(server_module.tools, "read_docs",
                        lambda *a, **k: f"docs {next(counter)}")


def learning_records(path: Path) -> list[dict]:
    return [r["step"] for r in map(json.loads, path.read_text().splitlines())
            if r.get("kind") == "trace" and r["step"].get("kind") == "learning"]


def run_task(ws, text: str) -> tuple[dict, str]:
    ws.send_json({"type": "task", "text": text})
    tokens = []
    for _ in range(2000):
        event = ws.receive_json()
        if event["type"] == "token":
            tokens.append(event["text"])
        if event["type"] == "done":
            return event, "".join(tokens)
        if event["type"] == "error":
            raise AssertionError(event["text"])
    raise AssertionError("no done")


class TestOnTheWeb:
    def test_a_long_task_he_sent_carries_the_chip(self, app_env, docs):
        client, _ = make_client(app_env, long_turn())
        with client, connected(client) as (ws, hello, _):
            done, streamed = run_task(ws, "find it")
            path = app_env["state_dir"] / hello["session"]
        assert done["result"] == "All done.\n\n" + learning.CHIP
        # Streamed too, so an answer already on screen gains it live.
        assert streamed.endswith("\n\n" + learning.CHIP)
        (record,) = learning_records(path)
        assert record["tripped"] == ["calls"] and record["chip"] is True
        assert record["counts"]["calls"] == learning.LONG_TASK_CALLS
        assert isinstance(record["turn"], int) and record["turn"] >= 1

    def test_the_record_sits_inside_the_turns_brackets(self, app_env, docs):
        client, _ = make_client(app_env, long_turn())
        with client, connected(client) as (ws, hello, _):
            run_task(ws, "find it")
            path = app_env["state_dir"] / hello["session"]
        kinds = [r["kind"] if r["kind"] != "trace" else r["step"]["kind"]
                 for r in map(json.loads, path.read_text().splitlines())]
        assert kinds.index("task_start") < kinds.index("learning") < kinds.index("task_end")

    def test_an_ordinary_task_gets_no_chip_but_a_record(self, app_env):
        client, _ = make_client(app_env, [model_says("Hello.")])
        with client, connected(client) as (ws, hello, _):
            done, _ = run_task(ws, "hi")
            path = app_env["state_dir"] / hello["session"]
        assert done["result"] == "Hello."
        (record,) = learning_records(path)
        assert record["tripped"] == [] and record["chip"] is False

    def test_the_chip_never_reaches_the_model(self, app_env, docs):
        client, chat = make_client(app_env, [*long_turn(), model_says("ok")])
        with client, connected(client) as (ws, _hello, _):
            run_task(ws, "find it")
            run_task(ws, "thanks")
        sent = json.dumps(chat.calls[-1]["messages"])
        assert learning.CHIP_LABEL not in sent

    def test_an_open_question_keeps_its_fallback_replies(self, app_env, docs):
        turns = long_turn()
        turns[-1] = model_says("Shall I book it?")
        client, _ = make_client(app_env, turns)
        with client, connected(client) as (ws, _hello, _):
            done, _ = run_task(ws, "find it")
        assert "aish-reply://yes" in done["result"]
        assert done["result"].endswith(learning.CHIP)

    def test_a_loop_stop_trips_it(self, app_env, monkeypatch):
        from aish import server as server_module

        monkeypatch.setattr(server_module.tools, "read_docs", lambda *a, **k: "same")
        same = model_says(tool_calls=[tool_call("read_docs", command="ls")])
        # The first call is progress and clears the streak; five dead retries
        # after it reach LOOP_STOP_REPEATS.
        client, _ = make_client(app_env, [same] * 6 + [model_says("Stopped: looping.")])
        with client, connected(client) as (ws, hello, _):
            done, _ = run_task(ws, "loop")
            path = app_env["state_dir"] / hello["session"]
        (record,) = learning_records(path)
        assert record["stopped"] == "loop_stop"
        assert "stopped" in record["tripped"]
        assert done["result"].endswith(learning.CHIP)

    def test_a_reload_shows_what_the_live_turn_showed(self, app_env, docs):
        client, _ = make_client(app_env, [*long_turn(), model_says("Hello.")])
        with client, connected(client) as (ws, hello, _):
            live_long, _ = run_task(ws, "find it")
            live_short, _ = run_task(ws, "hi")
            path = app_env["state_dir"] / hello["session"]
        cold = [e for e in SessionLog.reconstruct_events(path) if e["type"] == "done"]
        assert [e["result"] for e in cold] == [live_long["result"], live_short["result"]]

    def test_a_learn_task_is_never_offered_a_lesson(self, app_env, docs):
        client, _ = make_client(app_env, [*long_turn(), *long_turn()])
        with client, connected(client) as (ws, hello, _):
            run_task(ws, "/learn")
            path = app_env["state_dir"] / hello["session"]
        (record,) = learning_records(path)
        assert record["withheld"] == learning.LEARN_TASK and record["chip"] is False

    def test_not_twice_in_a_row(self, app_env, docs):
        client, _ = make_client(app_env, [*long_turn(), *long_turn()])
        with client, connected(client) as (ws, hello, _):
            first, _ = run_task(ws, "find it")
            second, _ = run_task(ws, "and again")
            path = app_env["state_dir"] / hello["session"]
        assert first["result"].endswith(learning.CHIP)
        assert second["result"] == "All done."
        assert [r.get("withheld") for r in learning_records(path)] == [None, learning.COOLDOWN]

    def test_a_triggered_task_is_recorded_and_offered_nothing(self, app_env, docs):
        chat = FakeChat(long_turn())
        from aish.server import create_app

        app = create_app("fake", client_chat=chat, **app_env, token="secret")
        client = TokenClient(app, auto_token="secret")
        with client:
            r = client.post(
                "/trigger?token=secret", json={"prompt": "nightly", "origin": "schedule"}
            )
            server = client.app.state.server
            session = server.sessions[r.json()["session"]]
            deadline = time.time() + 10
            while session.busy and time.time() < deadline:
                time.sleep(0.02)
            assert not session.busy
            path = session.logref.log.path
            done = [e for e in session.bridge.transcript if e.get("type") == "done"]
        (record,) = learning_records(path)
        assert record["tripped"] == ["calls"]
        assert record["chip"] is False and record["withheld"] == learning.NOT_OWNER_SENT
        assert learning.CHIP not in done[-1]["result"]

    def test_the_kill_switch_leaves_the_turn_as_it_was(self, app_env, docs, monkeypatch):
        monkeypatch.setenv(learning.DISABLE_ENV, "0")
        client, _ = make_client(app_env, long_turn())
        with client, connected(client) as (ws, hello, _):
            done, _ = run_task(ws, "find it")
            path = app_env["state_dir"] / hello["session"]
        assert done["result"] == "All done."
        assert learning_records(path) == []


# ── the terminal ─────────────────────────────────────────────────────────────


class TestInTheTerminal:
    def launch(self, monkeypatch, tmp_path, inputs: list[str], responses: list, argv=()):
        from aish import backends, cli
        from aish import tools as tools_module

        state, work = tmp_path / "state", tmp_path / "work"
        for path in (state, work):
            path.mkdir(exist_ok=True)
        monkeypatch.chdir(work)
        monkeypatch.setenv("AISH_STATE_DIR", str(state))
        monkeypatch.setenv("AISH_CONFIG", str(tmp_path / "absent.toml"))
        monkeypatch.setenv("AISH_ALLOWLIST", str(tmp_path / "allow.txt"))
        monkeypatch.setenv("AISH_DENYLIST", str(tmp_path / "deny.txt"))
        monkeypatch.setenv("AISH_LESSONS", str(tmp_path / "lessons.md"))
        monkeypatch.delenv("AISH_MODEL", raising=False)
        monkeypatch.setattr(cli, "_box", None)
        monkeypatch.setattr(sys, "stdin", io.StringIO())
        chat = FakeChat(responses)
        monkeypatch.setattr(backends, "make_chat", lambda spec: (chat, "ollama", "fake"))
        counter = iter(range(10_000))
        monkeypatch.setattr(tools_module, "read_docs", lambda *a, **k: f"docs {next(counter)}")
        typed = iter(inputs)

        def read_task(_cwd):
            try:
                return next(typed)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr(cli, "read_task", read_task)
        monkeypatch.setattr(sys, "argv", ["aish", *argv])
        assert cli.main() == 0
        (log,) = state.glob("session-*.jsonl")
        return chat, log

    def test_the_chip_joins_the_numbered_menu_and_sends_learn(
        self, monkeypatch, tmp_path, capsys
    ):
        chat, log = self.launch(
            monkeypatch, tmp_path, ["find it", "1"],
            [*long_turn(), model_says("Nothing worth saving.")],
        )
        out = capsys.readouterr().out
        assert "Quick replies" in out and "Save this as a lesson?" in out
        # Its number sent /learn, which the REPL expanded like a typed one.
        typed = [m for m in chat.calls[-1]["messages"] if m.get("role") == "user"]
        assert learning.is_learn_prompt(str(typed[-1]["content"]))
        records = learning_records(log)
        assert [r["chip"] for r in records] == [True, False]
        assert records[1]["tripped"] == []

    def test_an_ordinary_task_shows_no_menu(self, monkeypatch, tmp_path, capsys):
        _, log = self.launch(monkeypatch, tmp_path, ["hi"], [model_says("Hello.")])
        assert "Save this as a lesson?" not in capsys.readouterr().out
        (record,) = learning_records(log)
        assert record["tripped"] == []

    def test_a_one_shot_run_records_and_offers_nothing(self, monkeypatch, tmp_path, capsys):
        _, log = self.launch(monkeypatch, tmp_path, [], long_turn(), argv=["find", "it"])
        assert "Save this as a lesson?" not in capsys.readouterr().out
        (record,) = learning_records(log)
        assert record["withheld"] == learning.ONE_SHOT


# ── the replay check ─────────────────────────────────────────────────────────


def recorded(app_env, responses, texts) -> Path:
    client, _ = make_client(app_env, responses)
    with client, connected(client) as (ws, hello, _):
        for text in texts:
            run_task(ws, text)
        return app_env["state_dir"] / hello["session"]


def without_learning_records(path: Path, tmp_path: Path) -> Path:
    """The same log as a build before the detector would have written it."""
    kept = [line for line in path.read_text().splitlines()
            if '"kind": "learning"' not in line]
    out = tmp_path / "baseline.jsonl"
    out.write_text("\n".join(kept) + "\n")
    return out


class TestTheCheck:
    def test_a_long_task_with_its_record_passes(self, app_env, docs):
        path = recorded(app_env, [*long_turn(), model_says("Hello.")], ["find it", "hi"])
        verdict = replay_checks.learning_suggested(replay_checks.load_run(path))
        assert verdict.passed is True, verdict.evidence
        assert "1 of 2" in verdict.evidence

    def test_the_baseline_shape_fails_for_want_of_a_record(self, app_env, docs, tmp_path):
        path = recorded(app_env, long_turn(), ["find it"])
        baseline = without_learning_records(path, tmp_path)
        verdict = replay_checks.learning_suggested(replay_checks.load_run(baseline))
        assert verdict.passed is False
        assert "['calls']" in verdict.evidence

    def test_an_ordinary_chat_passes_with_or_without_records(self, app_env, tmp_path):
        path = recorded(app_env, [model_says("Hello.")], ["hi"])
        for log in (path, without_learning_records(path, tmp_path)):
            verdict = replay_checks.learning_suggested(replay_checks.load_run(log))
            assert verdict.passed is True, verdict.evidence

    def test_a_record_that_disagrees_with_the_log_fails(self, app_env, tmp_path):
        path = recorded(app_env, [model_says("Hello.")], ["hi"])
        lines = path.read_text().splitlines()
        forged = [line.replace('"tripped": []', '"tripped": ["calls"]') for line in lines]
        out = tmp_path / "forged.jsonl"
        out.write_text("\n".join(forged) + "\n")
        verdict = replay_checks.learning_suggested(replay_checks.load_run(out))
        assert verdict.passed is False and "1 records tripped" in verdict.evidence

    def test_a_log_with_no_bracketed_task_cannot_tell(self, tmp_path):
        log = SessionLog(tmp_path / "cli.jsonl")
        log.message({"role": "user", "content": "hi"})
        verdict = replay_checks.learning_suggested(replay_checks.load_run(log.path))
        assert verdict.passed is None

    def test_it_is_in_the_library(self):
        assert replay_checks.LIBRARY["learning_suggested"] is replay_checks.learning_suggested


class TestTheRecordKind:
    def test_renderless_and_not_activity(self):
        from aish.session import RENDERLESS_STEPS

        assert learning.LEARNING_STEP in RENDERLESS_STEPS
        assert not SessionLog._is_activity(
            {"kind": "trace", "step": {"kind": "learning", "chip": True}}
        )

    def test_a_log_without_it_replays_as_before(self, tmp_path):
        log = SessionLog(tmp_path / "old.jsonl")
        log.task_start("hi")
        log.message({"role": "user", "content": "hi"})
        log.step({"kind": "thinking_cancel", "secs": 1, "tokens": [1, 1], "answered": True})
        log.message({"role": "assistant", "content": "Hello."})
        log.task_end()
        before = SessionLog.reconstruct_events(log.path)
        with_record = tmp_path / "new.jsonl"
        lines = log.path.read_text().splitlines()
        record = json.dumps({"ts": "2026-10-12T00:00:00", "kind": "trace", "step": {
            "kind": "learning", "turn": 1, "tripped": [], "chip": False}})
        with_record.write_text("\n".join([*lines[:-1], record, lines[-1]]) + "\n")
        assert SessionLog.reconstruct_events(with_record) == before
