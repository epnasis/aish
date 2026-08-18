"""The diagnostic reader and the brief writer (#214, #239 slice 1).

Two things under test and one law shared by both: the reader must never present
"not recorded" as "recorded and empty", nor either of those as "recorded, then
removed" — the three states redaction and a purgeable evidence store create.
"""

import json
from types import SimpleNamespace

from aish import evidence
from aish import explain as explain_mod
from aish import session as session_module
from aish.session import RENDERLESS_STEPS, SessionLog


# Local rather than imported from test_agent: no test module in this suite
# imports another, and a scripted model is three lines.
def tool_call(name: str, **arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def model_says(content: str = "", tool_calls: list | None = None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(message=message)


class FakeChat:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        return iter([response]) if kwargs.get("stream") else response



def make_logged_agent(responses, tmp_path, **kwargs):
    """An Agent wired to a real SessionLog, the way both entry points wire it."""
    from aish.agent import Agent

    log = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    chat = FakeChat(responses)
    agent = Agent(
        model="fake",
        approve=lambda _cmd: True,
        client_chat=chat,
        on_message=log.message,
        step_log=log.step,
        state_dir=tmp_path,
        **kwargs,
    )
    return agent, chat, log


def steps(path, kind):
    out = []
    for line in path.read_text().splitlines():
        record = json.loads(line)
        if record.get("kind") == "trace" and (record.get("step") or {}).get("kind") == kind:
            out.append(record["step"])
    return out


class TestEvidenceStore:
    def test_put_is_content_addressed_and_get_round_trips(self, tmp_path):
        digest = evidence.put("the tool menu", tmp_path)
        assert digest == evidence.digest_of("the tool menu")
        assert evidence.get(digest, tmp_path) == "the tool menu"

    def test_identical_content_is_stored_once(self, tmp_path):
        first = evidence.put("same bytes", tmp_path)
        second = evidence.put("same bytes", tmp_path)
        assert first == second
        blobs = list(evidence.store_dir(tmp_path).rglob("*"))
        assert len([b for b in blobs if b.is_file()]) == 1

    def test_purge_makes_get_report_absence(self, tmp_path):
        """The operation that keeps forget/redact honest once a body is
        referenced from more than one log."""
        digest = evidence.put("a remembered secret", tmp_path)
        assert evidence.purge(digest, tmp_path) is True
        assert evidence.get(digest, tmp_path) is None
        assert evidence.purge(digest, tmp_path) is False  # already gone

    def test_a_tampered_blob_is_not_handed_back(self, tmp_path):
        """Returning bytes that do not hash to their own name would let a
        reader quote evidence that is not what was recorded."""
        digest = evidence.put("original", tmp_path)
        path = next(p for p in evidence.store_dir(tmp_path).rglob("*") if p.is_file())
        path.write_text("substituted")
        assert evidence.get(digest, tmp_path) is None

    def test_no_state_dir_still_yields_a_reference(self, tmp_path):
        """A caller with nowhere to write must still record a digest, so the
        reader reports it as unresolvable rather than as never recorded."""
        digest = evidence.put("orphan", None)
        assert digest == evidence.digest_of("orphan")
        assert evidence.get(digest, None) is None


class TestBriefWriter:
    def test_brief_is_registered_renderless(self):
        """Both halves are required (docs/trace-records.md): log-only emit AND
        the replay skip. Either alone still opens an empty live trace card."""
        assert "brief" in RENDERLESS_STEPS

    def test_brief_never_reaches_a_renderer(self, tmp_path):
        rendered = []
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.on_step = rendered.append
        agent.run_task("hello")
        assert steps(log.path, "brief"), "nothing was logged"
        assert not [s for s in rendered if s.get("kind") == "brief"]

    def test_menu_bytes_land_in_the_store_and_resolve(self, tmp_path):
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        (brief,) = steps(log.path, "brief")
        menu = json.loads(evidence.get(brief["tools"]["digest"], tmp_path))
        names = {entry["function"]["name"] for entry in menu}
        assert "run_command" in names and "read_url" in names
        assert brief["tools"]["count"] == len(menu)
        assert brief["tools"]["names"] == sorted(names)
        # The point of storing the bytes: descriptions and schemas, not names.
        assert all(entry["function"].get("description") for entry in menu)

    def test_options_are_recorded_beside_the_menu(self, tmp_path):
        agent, _, log = make_logged_agent(
            [model_says("done")], tmp_path, num_ctx=4096, think=True
        )
        agent.run_task("hello")
        (brief,) = steps(log.path, "brief")
        assert brief["options"] == {"model": "fake", "num_ctx": 4096, "think": True}

    def test_written_once_per_session_not_once_per_call(self, tmp_path):
        """Interning: an unchanged menu is named once, however many model calls
        and turns the session makes."""
        agent, _, log = make_logged_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="ls")]),
                model_says("first"),
                model_says("second"),
            ],
            tmp_path,
        )
        agent.run_task("one")
        agent.run_task("two")
        assert len(steps(log.path, "brief")) == 1

    def test_a_menu_change_mid_turn_is_recorded_at_the_call_it_changed_at(self, tmp_path):
        """The reason this is per model call and not per turn: the plugin menu
        refreshes at the top of every call, so a tool appearing mid-task must
        not be attributed to the whole turn."""
        agent, _, log = make_logged_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="ls")]),
                model_says("done"),
            ],
            tmp_path,
        )
        extra = {
            "type": "function",
            "function": {"name": "late_tool", "description": "arrived mid-turn", "parameters": {}},
        }
        original = agent._refresh_plugin_tools

        def grow_on_second_call():
            original()
            if agent._model_call >= 1:
                agent._plugin_defs = [extra]

        agent._refresh_plugin_tools = grow_on_second_call
        agent.run_task("go")
        briefs = steps(log.path, "brief")
        assert len(briefs) == 2
        assert briefs[0]["model_call"] == 1 and briefs[1]["model_call"] == 2
        assert "late_tool" not in briefs[0]["tools"]["names"]
        assert "late_tool" in briefs[1]["tools"]["names"]

    def test_replay_ignores_the_brief(self, tmp_path):
        """Renderless on both paths: a log carrying a brief must reconstruct to
        the same events as one without it."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        with_brief = session_module.SessionLog.reconstruct_events(log.path)
        lines = [
            line
            for line in log.path.read_text().splitlines()
            if (json.loads(line).get("step") or {}).get("kind") != "brief"
        ]
        stripped = tmp_path / "stripped.jsonl"
        stripped.write_text("\n".join(lines) + "\n")
        assert session_module.SessionLog.reconstruct_events(stripped) == with_brief


class TestReaderIsPureAndHonest:
    def test_no_model_and_no_live_state_can_reach_the_reader(self):
        """§0: an explanation is assembled from recorded evidence, never
        re-derived from source. A reader that can reach a backend, a rule file
        or the live tool table can answer with how aish behaves TODAY."""
        source = (
            __import__("pathlib").Path(explain_mod.__file__).read_text()
        )
        for forbidden in ("backends", "ollama", "import rules", "from .rules", "tool_plugins",
                          "from .tools", "import tools", "skills"):
            assert forbidden not in source, forbidden

    def test_a_turn_names_the_tools_that_were_on_the_menu(self, tmp_path):
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        out = explain_mod.explain(log.path, root=tmp_path, show_tools=True)
        assert "run_command" in out
        assert "resolved" in out  # the bytes were found, not merely referenced

    def test_purged_bytes_are_reported_as_purged_not_as_unrecorded(self, tmp_path):
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        (brief,) = steps(log.path, "brief")
        evidence.purge(brief["tools"]["digest"], tmp_path)
        out = explain_mod.explain(log.path, root=tmp_path)
        assert "purged" in out
        assert explain_mod.NOT_RECORDED not in out.split("brief")[1].split("\n")[0]

    def test_a_log_that_predates_a_record_says_not_recorded(self, tmp_path):
        """A kind absent from the WHOLE file means the writer did not exist —
        a different answer from a turn that happens to have none."""
        path = tmp_path / "session-old.jsonl"
        path.write_text(
            json.dumps({"kind": "task_start", "ts": "2026-01-01T00:00:00", "prompt": "hi"})
            + "\n"
            + json.dumps({"kind": "message", "role": "assistant", "content": "hello"})
            + "\n"
        )
        out = explain_mod.explain(path, root=tmp_path)
        assert explain_mod.NOT_RECORDED in out

    def test_turns_are_bracketed_by_task_start(self, tmp_path):
        agent, _, log = make_logged_agent([model_says("a"), model_says("b")], tmp_path)
        agent.run_task("first")
        agent.run_task("second")
        assert "turn 1" in explain_mod.explain(log.path, 1, root=tmp_path)
        assert "second" in explain_mod.explain(log.path, 2, root=tmp_path)
        assert "first" not in explain_mod.explain(log.path, 2, root=tmp_path)

    def test_a_missing_turn_is_reported_rather_than_guessed(self, tmp_path):
        agent, _, log = make_logged_agent([model_says("a")], tmp_path)
        agent.run_task("only one")
        assert "no turn 9" in explain_mod.explain(log.path, 9, root=tmp_path)

    def test_a_refused_gate_is_shown_and_allowed_ones_are_counted(self, tmp_path):
        path = tmp_path / "session-gates.jsonl"
        rows = [
            {"kind": "task_start", "ts": "2026-01-01T00:00:00", "prompt": "do it"},
            {"kind": "trace", "step": {"kind": "tool", "name": "run_command", "call": 1,
                                       "ok": False, "status": "failed", "turn": 1,
                                       "decision": "blocked"}},
            {"kind": "trace", "step": {"kind": "gate", "call": 1, "at": "gate",
                                       "gate": "rule", "rule": "no-rm", "verdict": "blocked",
                                       "turn": 1}},
            {"kind": "trace", "step": {"kind": "gate", "call": 1, "at": "gate",
                                       "gate": "rule", "rule": "quiet", "verdict": "allowed",
                                       "turn": 1}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        out = explain_mod.explain(path, root=tmp_path)
        assert "no-rm" in out and "blocked" in out
        assert "1 gate(s) allowed" in out
        assert "quiet" not in out  # collapsed, so the refusal is not buried

    def test_a_verify_verdict_is_not_reported_as_a_broken_join(self, tmp_path):
        """Verify verdicts are about the turn's answer and carry no call id.
        Filing them under 'no tool step for this call' read like a bug."""
        path = tmp_path / "session-verify.jsonl"
        rows = [
            {"kind": "task_start", "ts": "2026-01-01T00:00:00", "prompt": "hi"},
            {"kind": "trace", "step": {"kind": "gate", "at": "verify", "gate": "rule.verify",
                                       "verdict": "allowed", "turn": 1}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        out = explain_mod.explain(path, root=tmp_path)
        assert "no tool step" not in out
        assert "verify" in out

    def test_a_redaction_is_announced(self, tmp_path):
        path = tmp_path / "session-redacted.jsonl"
        rows = [
            {"kind": "redact", "ts": "2026-01-02T00:00:00", "turn": "abc", "at": "2026-01-02",
             "records": 26},
            {"kind": "task_start", "ts": "2026-01-02T00:01:00", "prompt": "what remains"},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        out = explain_mod.explain(path, root=tmp_path)
        assert "redacted 26 record(s)" in out

    def test_a_torn_line_does_not_stop_the_explanation(self, tmp_path):
        """A reader that raises on one bad line cannot explain the session that
        produced it — and a torn line is the failure every other reader of this
        file silently skips."""
        path = tmp_path / "session-torn.jsonl"
        path.write_text(
            json.dumps({"kind": "task_start", "ts": "t", "prompt": "hi"})
            + "\n{not json\n"
            + json.dumps({"kind": "message", "role": "assistant", "content": "answered"})
            + "\n"
        )
        out = explain_mod.explain(path, root=tmp_path)
        assert "answered" in out

    def test_an_ambiguous_name_lists_candidates_instead_of_choosing(self, tmp_path):
        for stamp in ("session-20260101-a.jsonl", "session-20260101-b.jsonl"):
            (tmp_path / stamp).write_text("")
        assert len(explain_mod.resolve("20260101", tmp_path)) == 2


class TestSubcommand:
    def test_explain_is_reachable_from_argv(self, tmp_path, monkeypatch, capsys):
        from aish import cli

        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["aish", "explain", log.path.name])
        assert cli.main() == 0
        assert "run_command" in capsys.readouterr().out

    def test_an_unmatched_name_fails_loudly(self, tmp_path, monkeypatch, capsys):
        from aish import cli

        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["aish", "explain", "nothing-like-this"])
        assert cli.main() == 1
        assert "no session log matching" in capsys.readouterr().out
