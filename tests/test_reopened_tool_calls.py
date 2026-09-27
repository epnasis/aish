"""A reopened chat keeps the tool calls it made (#422).

Without them every earlier result reached the model as a `user` message, and on
the replayed turn that stalled, the model announced work instead of doing it.
The live history is the reference: a reopen must hand back the same calls, and
where the log cannot say which call a result answered, it must hand back none
rather than a guess.
"""

import json

from aish import backends
from aish import secrets as secrets_module
from aish.session import SessionLog
from tests.test_agent import make_agent, model_says, tool_call


def relabelled(messages: list[dict]) -> list[str]:
    """Results the OpenAI converter had to turn into user text."""
    return [
        m["content"]
        for m in backends.convert_messages(messages)
        if m["role"] == "user" and str(m.get("content", "")).startswith("[")
        and " result]\n" in str(m.get("content", ""))[:60]
    ]


def calls_of(messages: list[dict]) -> list[list[tuple]]:
    return [
        [(c["function"]["name"], c["function"]["arguments"]) for c in m["tool_calls"]]
        for m in messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]


def write_log(path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


class TestTheLiveHistoryIsWhatAReopenGetsBack:
    def _run(self, tmp_path, responses, **kwargs):
        log = SessionLog(tmp_path / "session-20260927-000000-000000.jsonl")
        agent, _ = make_agent(
            responses,
            approve=lambda _cmd: True,
            cwd=str(tmp_path),
            state_dir=tmp_path,
            on_message=log.message,
            step_log=log.step,
            **kwargs,
        )
        agent.run_task("check the files")
        log.close()
        return agent, SessionLog._parse(log.path).messages

    def test_a_reopened_chat_has_the_calls_the_live_one_made(self, tmp_path):
        (tmp_path / "a.txt").write_text("alpha")
        agent, reopened = self._run(
            tmp_path,
            [
                model_says(
                    "Reading both.",
                    tool_calls=[
                        tool_call("read_file", path=str(tmp_path / "a.txt")),
                        tool_call("run_command", command="echo hi"),
                    ],
                ),
                model_says("done"),
            ],
        )
        live = [m for m in agent.messages if m.get("role") != "system"]
        assert calls_of(reopened) == calls_of(live)
        assert calls_of(reopened), "the task made calls and none came back"
        assert relabelled(reopened) == [], "a result reached the model as the user's words"

    def test_a_secret_in_an_argument_is_not_written_to_the_log(self, monkeypatch, tmp_path):
        token = "awov6ybawmor59a9d7u926vk1yfdsm"
        index = tmp_path / "names.txt"
        index.write_text("PUSHOVER_TOKEN\n", encoding="utf-8")
        monkeypatch.setattr(secrets_module, "names_index", lambda i=index: i)
        monkeypatch.setattr(
            secrets_module, "get", lambda name: token if name == "PUSHOVER_TOKEN" else None
        )
        secrets_module._invalidate()
        try:
            _, reopened = self._run(
                tmp_path,
                [
                    model_says(tool_calls=[tool_call("run_command", command=f"echo {token}")]),
                    model_says("done"),
                ],
            )
        finally:
            secrets_module._invalidate()
        # The message records only: the trace records (`call`, `tool_start`,
        # `tool`) keep a model-emitted argument unscrubbed, as they did before.
        log = next(tmp_path.glob("session-*.jsonl")).read_text(encoding="utf-8")
        messages = [r for r in map(json.loads, log.splitlines()) if r.get("kind") == "message"]
        assert token not in json.dumps(messages)
        [[(name, args)]] = calls_of(reopened)
        assert name == "run_command" and "PUSHOVER_TOKEN" in args["command"]


def assistant(model_call: int, content: str = "", **extra) -> dict:
    return {"kind": "message", "role": "assistant", "content": content,
            "model_call": model_call, **extra}


def result(model_call: int, name: str, content: str = "ok") -> dict:
    return {"kind": "message", "role": "tool", "tool_name": name, "content": content,
            "model_call": model_call}


def call(model_call: int | None, name: str, number: int, **extra) -> dict:
    step = {"kind": "call", "call": number, "name": name, "args": extra.pop("args", {}), **extra}
    if model_call is not None:
        step["model_call"] = model_call
    return {"kind": "trace", "step": step}


USER = {"kind": "message", "role": "user", "content": "go"}


class TestAnOldLogIsRepairedOnlyWhereItCanSay:
    """Logs written before #422 carry no calls on the message; the `call`
    records between a message and its results are the model's own arguments."""

    def test_calls_are_rebuilt_from_the_call_records(self, tmp_path):
        path = tmp_path / "s.jsonl"
        write_log(path, [
            USER,
            assistant(1, "Reading."),
            call(1, "read_file", 1, args={"path": "a"}),
            call(1, "web_search", 2, args={"query": "q"}),
            result(1, "read_file"),
            result(1, "web_search"),
            assistant(2, "done"),
        ])
        messages = SessionLog._parse(path).messages
        assert calls_of(messages) == [
            [("read_file", {"path": "a"}), ("web_search", {"query": "q"})]
        ]
        assert relabelled(messages) == []

    def test_a_split_batch_is_put_back_in_result_order_when_names_are_unique(self, tmp_path):
        # The parallel read path logs its calls first and the gated ones after,
        # while the results keep the order the model made them in.
        path = tmp_path / "s.jsonl"
        write_log(path, [
            USER,
            assistant(1),
            call(1, "read_file", 2, args={"path": "a"}),
            call(1, "run_command", 1, args={"command": "ls"}),
            result(1, "run_command"),
            result(1, "read_file"),
        ])
        [calls] = calls_of(SessionLog._parse(path).messages)
        assert [name for name, _ in calls] == ["run_command", "read_file"]

    def test_a_repeated_name_out_of_order_is_left_unpaired(self, tmp_path):
        # Two read_url calls split across the paths: pairing by name could
        # hand one address's result to the other.
        path = tmp_path / "s.jsonl"
        write_log(path, [
            USER,
            assistant(1),
            call(1, "read_url", 2, args={"url": "b"}),
            call(1, "run_command", 1, args={"command": "ls"}),
            call(1, "read_url", 3, args={"url": "a"}),
            result(1, "run_command"),
            result(1, "read_url"),
            result(1, "read_url"),
        ])
        messages = SessionLog._parse(path).messages
        assert calls_of(messages) == []
        assert len(relabelled(messages)) == 3

    def test_a_truncated_argument_is_never_restored(self, tmp_path):
        path = tmp_path / "s.jsonl"
        write_log(path, [
            USER,
            assistant(1),
            call(1, "write_file", 1, args={"path": "a", "content": "cut…"}, truncated=500),
            result(1, "write_file"),
        ])
        assert calls_of(SessionLog._parse(path).messages) == []

    def test_a_call_that_never_reported_back_is_not_restored(self, tmp_path):
        # A process killed mid-batch: the call record is written before the
        # call runs, and its result never is.
        path = tmp_path / "s.jsonl"
        write_log(path, [
            USER,
            assistant(1),
            call(1, "read_file", 1, args={"path": "a"}),
            call(1, "read_file", 2, args={"path": "b"}),
            result(1, "read_file"),
        ])
        assert calls_of(SessionLog._parse(path).messages) == []

    def test_a_backend_that_stamps_no_model_call_is_not_guessed_at(self, tmp_path):
        # claude-max: its call records omit model_call and its messages carry 0.
        path = tmp_path / "s.jsonl"
        write_log(path, [
            USER,
            assistant(0),
            call(None, "read_file", 1, args={"path": "a"}),
            result(0, "read_file"),
        ])
        assert calls_of(SessionLog._parse(path).messages) == []

    def test_a_discarded_attempt_lends_no_calls(self, tmp_path):
        path = tmp_path / "s.jsonl"
        write_log(path, [
            USER,
            assistant(1),
            {**call(1, "read_file", 1, args={"path": "old"}), "superseded": True},
            call(1, "read_file", 2, args={"path": "new"}),
            result(1, "read_file"),
        ])
        assert calls_of(SessionLog._parse(path).messages) == [[("read_file", {"path": "new"})]]


    def test_a_malformed_record_costs_the_repair_and_never_the_reader(self, tmp_path):
        # The chat list parses every log; one hand-edited record must not
        # take it down (the reader-of-many-files rule).
        path = tmp_path / "s.jsonl"
        nameless = {"kind": "trace", "step": {"kind": "call", "call": 1, "args": {},
                                              "model_call": 1}}
        untagged = {"kind": "message", "role": "tool", "content": "ok", "model_call": 1}
        numeric = call(1, 5, 2)
        write_log(path, [USER, assistant(1), nameless, untagged,
                         assistant(1), numeric, result(1, "5")])
        assert calls_of(SessionLog._parse(path).messages) == []


class TestALoggedCallIsKeptOnlyBesideItsResult:
    def test_logged_calls_come_back(self, tmp_path):
        path = tmp_path / "s.jsonl"
        logged = [{"function": {"name": "read_file", "arguments": {"path": "a"}}}]
        write_log(path, [USER, assistant(1, tool_calls=logged), result(1, "read_file")])
        assert calls_of(SessionLog._parse(path).messages) == [[("read_file", {"path": "a"})]]

    def test_an_unanswered_logged_call_is_dropped(self, tmp_path):
        # An unpaired tool_use is a request the Anthropic API rejects: the
        # reopened chat would fail on every call.
        path = tmp_path / "s.jsonl"
        logged = [{"function": {"name": "read_file", "arguments": {"path": "a"}}}]
        write_log(path, [USER, assistant(1, tool_calls=logged), USER])
        messages = SessionLog._parse(path).messages
        assert calls_of(messages) == []
        backends.convert_messages(messages)  # and nothing dangles for a converter

    def test_a_held_wrap_up_logged_after_its_results_fails_safe(self, tmp_path):
        # `_finish_stopped` on the held path logs the placeholders first and
        # releases the entry after them: neither message may pair with the
        # wrong results.
        path = tmp_path / "s.jsonl"
        earlier = [{"function": {"name": "read_file", "arguments": {"path": "a"}}}]
        wrap_up = [{"function": {"name": "run_command", "arguments": {"command": "ls"}}}]
        write_log(path, [
            USER,
            assistant(1, tool_calls=earlier), result(1, "read_file"),
            result(2, "run_command", "NOT EXECUTED"),
            assistant(2, "stopped", tool_calls=wrap_up),
        ])
        messages = SessionLog._parse(path).messages
        assert calls_of(messages) == []
        backends.convert_messages(messages)
