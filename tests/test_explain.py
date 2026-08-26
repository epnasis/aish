"""The diagnostic reader and the brief writer (#214, #239 slice 1).

Two things under test and one law shared by both: the reader must never present
"not recorded" as "recorded and empty", nor either of those as "recorded, then
removed" — the three states redaction and a purgeable evidence store create.
"""

import json
from types import SimpleNamespace

from aish import agent as agent_module
from aish import evidence
from aish import explain as explain_mod
from aish import session as session_module
from aish.session import RENDERLESS_STEPS, SessionLog


# Local rather than imported from test_agent: no test module in this suite
# imports another, and a scripted model is three lines.
def tool_call(name: str, **arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def model_says(content: str = "", tool_calls: list | None = None, **extra):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or None, **extra)
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

    def test_undecodable_bytes_read_back_as_absent_rather_than_raising(self, tmp_path):
        """The store is text-only by construction, not by enforcement: nothing
        here can WRITE binary, and nothing here refuses to read it either. A
        blob whose bytes are not UTF-8 must land on the same answer as a
        truncated one — what is on disk is not what was recorded — because the
        module's stated law is that unreadable bytes are reportable, not an
        error, and this is a READER."""
        digest = evidence.put("original", tmp_path)
        path = next(p for p in evidence.store_dir(tmp_path).rglob("*") if p.is_file())
        path.write_bytes(b"\xff\xd8\xff\xe0 not text at all")
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
        assert brief["options"]["model"] == "fake"
        assert brief["options"]["num_ctx"] == 4096
        assert brief["options"]["think"] is True
        # How the provider carries the per-task reminder, recorded as a declared
        # fact rather than left for a reader to infer (#241).
        assert brief["options"]["system_role"] == "all_system"
        assert brief["options"]["provider"] == "ollama"

    def test_written_once_per_task_not_once_per_model_call(self, tmp_path):
        """Interning within a task: several model calls that were handed the
        same menu and the same system text name it once."""
        agent, _, log = make_logged_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="ls")]),
                model_says("first"),
            ],
            tmp_path,
        )
        agent.run_task("one")
        assert agent._model_call == 2, "the task must make more than one call"
        assert len(steps(log.path, "brief")) == 1

    def test_the_menu_bytes_are_stored_once_however_many_briefs(self, tmp_path):
        """What interning is FOR. The system text moves every task (the reminder
        carries the current time), so a second task writes a second brief — but
        the menu is ~31 KB and unchanged, and content addressing means it is on
        disk once. Asserting the record count here would pin the clock."""
        agent, _, log = make_logged_agent(
            [model_says("first"), model_says("second")], tmp_path
        )
        agent.run_task("one")
        agent.run_task("two")
        digests = {b["tools"]["digest"] for b in steps(log.path, "brief")}
        assert len(digests) == 1
        digest = digests.pop()
        stored = [p for p in (tmp_path / "evidence").rglob("*") if p.is_file()]
        assert sum(1 for p in stored if p.name == digest) == 1

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

    def test_the_system_text_is_stored_and_resolves(self, tmp_path):
        """The question this exists for: a model acting on a constraint nobody
        can find. The `context` record names WHICH memory was injected; only the
        bytes say what it told the model."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        (brief,) = steps(log.path, "brief")
        texts = [evidence.get(part["digest"], tmp_path) for part in brief["system"]]
        assert all(t is not None for t in texts), "a digest did not resolve"
        joined = "\n".join(texts)
        # The standing prompt and the per-task reminder are both in there.
        assert "aish" in joined
        assert agent_module.TASK_REMINDER_MARK in joined
        assert [part["chars"] for part in brief["system"]] == [len(t) for t in texts]

    def test_system_evidence_is_the_bytes_as_sent(self, tmp_path):
        """Recorded as what went out, not as a list of contributors — so the
        reader never has to reassemble four sources in the right order to answer
        "what was it actually told"."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        (brief,) = steps(log.path, "brief")
        sent = [m for m in agent.messages if m.get("role") == "system"]
        assert len(brief["system"]) == len(sent)
        for part, message in zip(brief["system"], sent, strict=True):
            assert evidence.get(part["digest"], tmp_path) == message["content"]
            assert agent.messages[part["at"]] is message

    def test_a_changed_rule_or_memory_writes_a_new_brief(self, tmp_path):
        """The menu can be identical while what the model was TOLD is not. A
        stamp over only the tools would let a reader conclude the turn was
        handed what the previous one was."""
        agent, _, log = make_logged_agent(
            [model_says("first"), model_says("second")], tmp_path
        )
        agent.run_task("one")
        first = steps(log.path, "brief")[-1]
        agent.base_context = "the shop closes at 18:00"
        agent.run_task("two")
        second = steps(log.path, "brief")[-1]
        assert first["tools"]["digest"] == second["tools"]["digest"]
        assert first["system"] != second["system"]
        assert "18:00" in evidence.get(second["system"][0]["digest"], tmp_path)

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


class TestReasoningCapture:
    """#240. The rendered `thinking` step keeps a snippet for a status ticker —
    26 characters on average across a real month — and that was the only durable
    record of how the model decided."""

    def test_both_new_kinds_are_registered_renderless(self):
        assert {"reasoning", "call"} <= RENDERLESS_STEPS

    def test_full_thinking_is_kept_not_a_snippet(self, tmp_path):
        essay = "I should check the Polish shops first. " * 40
        agent, _, log = make_logged_agent(
            [model_says("here you go", thinking=essay)], tmp_path
        )
        agent.run_task("find me a hammock")
        (record,) = steps(log.path, "reasoning")
        assert record["text"] == essay
        assert "truncated" not in record
        # …and the rendered step still carries only its snippet, unchanged.
        (rendered,) = steps(log.path, "thinking_cancel") or steps(log.path, "thinking")
        assert len(str(rendered.get("gist", ""))) <= 120

    def test_reasoning_never_reaches_a_renderer(self, tmp_path):
        """A quarter megabyte of reasoning hung off the rendered step would
        cross the wire live AND land in replay."""
        rendered = []
        agent, _, log = make_logged_agent([model_says("ok", thinking="lots")], tmp_path)
        agent.on_step = rendered.append
        agent.run_task("hello")
        assert steps(log.path, "reasoning")
        assert not [s for s in rendered if s.get("kind") == "reasoning"]

    def test_the_cap_is_named_and_says_how_much_it_cut(self, tmp_path):
        """Contract §8.5: a named constant, and a truncated record says which
        cap cut it — a silently short record is indistinguishable from a model
        that was silently brief."""
        from aish import agent as agent_module

        monkey = agent_module.REASONING_CHARS
        agent_module.REASONING_CHARS = 50
        try:
            agent, _, log = make_logged_agent([model_says("ok", thinking="x" * 130)], tmp_path)
            agent.run_task("hello")
        finally:
            agent_module.REASONING_CHARS = monkey
        (record,) = steps(log.path, "reasoning")
        assert len(record["text"]) == 50
        assert record["truncated"] == 80
        assert record["cap_source"] == "constant:REASONING_CHARS"

    def test_the_default_cap_cannot_bite_at_the_default_context(self):
        """The cap is a backstop, not a limit: a turn cannot generate more than
        num_ctx tokens, so at 32k context it is unreachable by ~2x."""
        from aish.agent import REASONING_CHARS

        assert REASONING_CHARS >= 32768 * 4

    def test_a_turn_records_one_reasoning_per_model_call(self, tmp_path):
        agent, _, log = make_logged_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="ls")], thinking="first"),
                model_says("done", thinking="second"),
            ],
            tmp_path,
        )
        agent.run_task("go")
        got = steps(log.path, "reasoning")
        assert [r["text"] for r in got] == ["first", "second"]
        assert [r["model_call"] for r in got] == [1, 2]

    def test_the_stop_reason_is_recorded(self, tmp_path):
        agent, _, log = make_logged_agent(
            [model_says("cut off", stop="max_tokens")], tmp_path
        )
        agent.run_task("hello")
        (record,) = steps(log.path, "reasoning")
        assert record["stop"] == "max_tokens"

    def test_ollama_puts_the_reason_somewhere_else_and_it_is_still_found(self, tmp_path):
        """The adapted backends set it on the message; ollama sets `done_reason`
        on the RESPONSE. Reading only one place records nothing for every local
        model, and the absence reads as "the provider did not say" when in fact
        nobody looked."""
        response = model_says("done")
        response.done_reason = "stop"
        agent, _, log = make_logged_agent([response], tmp_path)
        agent.run_task("hello")
        (record,) = steps(log.path, "reasoning")
        assert record["stop"] == "stop"

    def test_aishs_own_sentence_is_not_attributed_to_the_model(self, tmp_path):
        """The Anthropic path fabricates a refusal sentence and logs it as model
        content. Unmarked, a dossier credits the harness's words to the model."""
        agent, _, log = make_logged_agent(
            [model_says("(the model declined this request…)", synthesized=True)], tmp_path
        )
        agent.run_task("hello")
        (record,) = steps(log.path, "reasoning")
        assert record["synthesized"] is True
        assert "aish's sentence" in explain_mod.explain(log.path, root=tmp_path)

    def test_block_types_are_recorded_without_their_content(self, tmp_path):
        """Enough to see a provider-redacted thinking block; not enough to
        store a second copy of the turn."""
        blocks = [{"type": "redacted_thinking", "data": "opaque"}, {"type": "text", "text": "hi"}]
        agent, _, log = make_logged_agent([model_says("hi", raw_blocks=blocks)], tmp_path)
        agent.run_task("hello")
        (record,) = steps(log.path, "reasoning")
        assert record["blocks"] == ["redacted_thinking", "text"]
        assert "opaque" not in json.dumps(record)


class TestEmittedArguments:
    def test_the_exact_arguments_are_recorded(self, tmp_path):
        """The rendered step keeps a per-tool label — the query for a search,
        the path for an edit — and is silent about every other argument."""
        agent, _, log = make_logged_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="tar", topic="--exclude")]),
                model_says("done"),
            ],
            tmp_path,
        )
        agent.run_task("go")
        (record,) = steps(log.path, "call")
        assert record["args"] == {"command": "tar", "topic": "--exclude"}
        assert record["name"] == "read_docs"

    def test_arguments_are_capped_per_value_not_per_blob(self, tmp_path):
        """A huge body on a write must not push the path beside it out."""
        from aish import agent as agent_module

        args, dropped = agent_module._safe_args(
            {"path": "/tmp/x", "content": "y" * 9000}, agent_module.CALL_ARG_CHARS
        )
        assert args["path"] == "/tmp/x"
        assert len(args["content"]) == agent_module.CALL_ARG_CHARS
        assert dropped == 9000 - agent_module.CALL_ARG_CHARS

    def test_a_value_that_cannot_serialise_keeps_its_key(self, tmp_path):
        """Which argument was passed matters even when what it held does not
        survive JSON — dropping the key loses the more important half."""
        from aish import agent as agent_module

        args, _ = agent_module._safe_args({"fn": object()}, 100)
        assert "fn" in args and isinstance(args["fn"], str)

    def test_malformed_arguments_are_distinguished_from_no_arguments(self, tmp_path):
        """A JSON decode failure becomes `{}` downstream, which is exactly what
        a deliberate no-argument call looks like — and the two route to
        different repairs."""
        from aish.backends import _parse_args, _tool_function

        assert _parse_args("{not json") == ({}, True)
        assert _parse_args("") == ({}, False)
        assert _parse_args("[1,2]") == ({}, True)
        assert _tool_function("t", "{bad").malformed is True
        assert _tool_function("t", '{"a":1}').malformed is False

    def test_a_malformed_call_is_named_on_the_turn_that_emitted_it(self, tmp_path):
        broken = SimpleNamespace(
            function=SimpleNamespace(name="read_docs", arguments={}, malformed=True)
        )
        agent, _, log = make_logged_agent(
            [model_says(tool_calls=[broken]), model_says("done")], tmp_path
        )
        agent.run_task("go")
        first = steps(log.path, "reasoning")[0]
        assert first["malformed"] == ["read_docs"]
        assert "did not parse" in explain_mod.explain(log.path, root=tmp_path)

    def test_replay_ignores_both_new_kinds(self, tmp_path):
        agent, _, log = make_logged_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="ls")], thinking="why"),
                model_says("done"),
            ],
            tmp_path,
        )
        agent.run_task("go")
        full = session_module.SessionLog.reconstruct_events(log.path)
        lines = [
            line
            for line in log.path.read_text().splitlines()
            if (json.loads(line).get("step") or {}).get("kind")
            not in ("reasoning", "call", "brief")
        ]
        stripped = tmp_path / "stripped.jsonl"
        stripped.write_text("\n".join(lines) + "\n")
        assert session_module.SessionLog.reconstruct_events(stripped) == full


class TestCoverageHoles:
    """#241. Each of these let a reader reach a confident FALSE conclusion,
    which is worse than the gap it replaced."""

    def test_the_mid_task_trim_is_recorded(self, tmp_path):
        """The third trim site ran mid-task and recorded nothing, so a result the
        model read at step 2 could be a stub by step 7 with no trace. The
        transcript still holds the full text, so the log positively suggested
        the model had something it did not."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path, num_ctx=1)
        agent.messages.extend(
            [
                {"role": "user", "content": "go"},
                {"role": "tool", "tool_name": "read_url", "content": "x" * 4000},
                {"role": "tool", "tool_name": "web_search", "content": "y" * 4000},
                {"role": "tool", "tool_name": "read_file", "content": "z" * 4000},
                {"role": "tool", "tool_name": "recall", "content": "w" * 4000},
            ]
        )
        agent._enforce_budget(1)
        (record,) = [s for s in steps(log.path, "trim") if s["policy"] == "mid_task_budget"]
        assert record["affected"] >= 1
        assert record["budget"] is not None
        assert [s["tool"] for s in record["stubbed"]]

    def test_a_trim_names_which_results_were_stubbed(self, tmp_path):
        """`affected: 3` said something was cut but never WHAT."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.messages.extend(
            [
                {"role": "user", "content": "go"},
                {"role": "tool", "tool_name": "read_url", "content": "x" * 4000},
            ]
        )
        agent.num_ctx = 1024   # a 3072-char budget, so the history overflows
        agent._trim_history_to_budget()
        (record,) = steps(log.path, "trim")
        (stub,) = record["stubbed"]
        assert stub["at"] == 2 and stub["tool"] == "read_url"
        # …and whether the model can fetch it back, which is a different fact
        # about the same turn: with a key the history is bounded, without one
        # it is lossy.
        assert stub["continuation"], stub
        assert agent.messages[2]["content"].endswith("]")
        assert stub["continuation"] in agent.messages[2]["content"]

    def test_the_dossier_names_the_stubbed_results(self, tmp_path):
        path = tmp_path / "session-stub.jsonl"
        rows = [
            {"kind": "task_start", "ts": "t", "prompt": "hi"},
            {"kind": "trace", "step": {"kind": "trim", "policy": "eager_stub", "affected": 2,
                                       "bytes_before": 9000, "bytes_after": 400, "turn": 1,
                                       "stubbed": [{"at": 2, "tool": "read_url"},
                                                   {"at": 4, "tool": "web_search"}]}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        out = explain_mod.explain(path, root=tmp_path)
        assert "stubbed for the model" in out
        assert "#2 read_url" in out and "#4 web_search" in out

    def test_an_old_trim_record_says_which_messages_are_unknown(self, tmp_path):
        """A log written before #241 has `affected` but no `stubbed`. That must
        read as "not recorded", never as "nothing was stubbed"."""
        path = tmp_path / "session-oldtrim.jsonl"
        rows = [
            {"kind": "task_start", "ts": "t", "prompt": "hi"},
            {"kind": "trace", "step": {"kind": "trim", "policy": "eager_stub", "affected": 3,
                                       "bytes_before": 900, "bytes_after": 400, "turn": 1}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        out = explain_mod.explain(path, root=tmp_path)
        assert "which messages" in out and explain_mod.NOT_RECORDED in out

    def test_steering_typed_mid_task_appears_in_the_dossier(self, tmp_path):
        """It is folded into the model's messages without passing through the
        recorder, and is not restored on resume — this trace step is the only
        place it exists."""
        path = tmp_path / "session-steer.jsonl"
        rows = [
            {"kind": "task_start", "ts": "t", "prompt": "find a hammock"},
            {"kind": "trace", "step": {"kind": "injected", "text": "only Polish shops please"}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        out = explain_mod.explain(path, root=tmp_path)
        assert "only Polish shops please" in out
        assert "typed mid-task" in out

    def test_the_reminder_reaching_the_model_as_a_user_message_is_stated(self, tmp_path):
        """On the OpenAI-shaped backends aish's per-task reminder is relabelled
        `user` (#74). A dossier claiming system authority was in force would be
        describing something the model never saw."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.provider = "gemini"
        agent.run_task("hello")
        out = explain_mod.explain(log.path, root=tmp_path)
        assert "reached this model as a USER message" in out

    def test_declared_system_policy_matches_the_code(self):
        """The record carries a DECLARED policy so a reader never has to infer
        it from source. Declaring and doing must therefore be pinned together,
        or the record becomes a confident lie the day the converter changes."""
        from aish import backends

        history = [
            {"role": "system", "content": "base"},
            {"role": "system", "content": "<system-reminder>per-task</system-reminder>"},
            {"role": "user", "content": "hi"},
        ]
        converted = backends.convert_messages(history)
        roles = [m["role"] for m in converted]
        demoted = roles.count("system") == 1 and "user" in roles[1:2]
        assert demoted, roles
        for name in ("gemini", "openai"):
            assert backends.system_role_policy(name) == "first_only"
        assert backends.system_role_policy("ollama") == "all_system"
        # An unknown provider goes through the same converter, so it inherits
        # the demoting policy rather than the safe-sounding one.
        assert backends.system_role_policy("something-new") == "first_only"


class TestReaderIsPureAndHonest:
    def test_no_model_and_no_live_state_can_reach_the_reader(self):
        """§0: an explanation is assembled from recorded evidence, never
        re-derived from source. A reader that can reach a backend, a rule file
        or the live tool table can answer with how aish behaves TODAY.

        Checked against the IMPORT GRAPH, not the source text. A substring scan
        reads a module name in a comment as a violation, which teaches the next
        author to reword the prose — and a test you satisfy by rewording is one
        that will wave the real import through later.
        """
        import ast
        import pathlib

        forbidden = {"backends", "ollama", "rules", "rule_compiler", "tools",
                     "tool_plugins", "skills", "embeddings", "web", "browser"}
        tree = ast.parse(pathlib.Path(explain_mod.__file__).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
                if node.module:
                    imported.add(node.module.split(".")[-1])
        assert not (imported & forbidden), imported & forbidden

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

    def test_an_advised_verdict_is_not_summarised_as_a_refusal(self, tmp_path):
        """`advised` means the answer WAS delivered, carrying a not-followed
        note. The contract records that conflating it with a termination had the
        ledger counting shipped answers as stops."""
        path = tmp_path / "session-advised.jsonl"
        rows = [
            {"kind": "task_start", "ts": "2026-01-01T00:00:00", "prompt": "hi"},
            {"kind": "trace", "step": {"kind": "gate", "at": "verify", "gate": "rule.verify",
                                       "rule": "live-price", "verdict": "advised", "turn": 1}},
            {"kind": "trace", "step": {"kind": "gate", "at": "verify", "gate": "rule.verify",
                                       "rule": "no-flattery", "verdict": "allowed", "turn": 1}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        out = explain_mod.explain(path, root=tmp_path)
        assert "answer delivered, with a note" in out
        assert "1 check(s) passed" in out  # the advised row is not counted as failed

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
        assert "no chat log matching" in capsys.readouterr().out


def _only_first_brief(path):
    """The log as an interned session writes it: one brief, at the turn it
    changed at. `make_logged_agent` writes one per task because the per-task
    reminder carries the clock, so the case has to be made rather than waited
    for."""
    kept, seen = [], False
    for line in path.read_text().splitlines():
        if (json.loads(line).get("step") or {}).get("kind") == "brief":
            if seen:
                continue
            seen = True
        kept.append(line)
    trimmed = path.with_name("interned.jsonl")
    trimmed.write_text("\n".join(kept) + "\n")
    return trimmed


class TestDossier:
    """The single assembly both renderers read (#243)."""

    def test_it_serialises_whole(self, tmp_path):
        """The web panel fetches this as JSON. A field that cannot cross the
        wire is a field the phone silently does not have."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[0], lg, tmp_path)
        assert json.loads(json.dumps(doc))["prompt"] == "hello"

    def test_the_system_text_rides_along_resolved(self, tmp_path):
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[0], lg, tmp_path)
        system = doc["given"]["briefs"][0]["system"]
        assert system["state"] == explain_mod.RECORDED
        assert all(part["state"] == explain_mod.RECORDED for part in system["parts"])
        assert any(agent_module.TASK_REMINDER_MARK in part["text"] for part in system["parts"])

    def test_purged_bytes_are_purged_not_missing(self, tmp_path):
        """The distinction the whole reader exists for. A purged blob must not
        read as a turn that was told nothing."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        (brief,) = steps(log.path, "brief")
        evidence.purge(brief["system"][0]["digest"], tmp_path)
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[0], lg, tmp_path)
        parts = doc["given"]["briefs"][0]["system"]["parts"]
        assert parts[0]["state"] == explain_mod.PURGED
        assert parts[0]["text"] is None
        assert "purged" in explain_mod.explain(log.path, root=tmp_path)

    def test_a_turn_with_no_brief_of_its_own_is_shown_the_one_in_force(self, tmp_path):
        """The brief is interned, so most turns have none of their own. A panel
        that rendered only this turn's brief would show an empty "what it was
        given" on nearly every turn — the one screen the feature exists for."""
        agent, _, log = make_logged_agent(
            [model_says("first"), model_says("second")], tmp_path
        )
        agent.run_task("one")
        agent.run_task("two")
        # The interned case: the second turn's brief is dropped, which is what
        # a session whose tools and system text did not move actually writes.
        lg = explain_mod.load(_only_first_brief(log.path))
        first = explain_mod.dossier(lg.turns[0], lg, tmp_path)
        second = explain_mod.dossier(lg.turns[1], lg, tmp_path)
        assert first["given"]["briefs"][0]["written_here"] is True
        assert second["given"]["briefs"], "the turn was shown no brief at all"
        assert second["given"]["briefs"][0]["written_here"] is False
        assert second["given"]["briefs"][0]["in_force_since"] == 1
        assert second["given"]["carried"] is True
        # …and the tools are actually THERE, which is the point of carrying it.
        assert second["given"]["briefs"][0]["tools"]["count"]

    def test_carried_forward_is_never_confused_with_written_here(self, tmp_path):
        """A reader who cannot tell them apart concludes "the tools changed at
        turn 2" off a record that only says they had not changed since turn 1."""
        agent, _, log = make_logged_agent(
            [model_says("first"), model_says("second")], tmp_path
        )
        agent.run_task("one")
        agent.run_task("two")
        out = explain_mod.explain(_only_first_brief(log.path), 2, root=tmp_path)
        assert "unchanged since turn 1" in out

    def test_a_log_predating_the_record_says_not_recorded(self, tmp_path):
        """Absence must never be the evidence: a log with no brief at all is a
        different answer from a turn whose brief was unchanged."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        stripped = tmp_path / "old.jsonl"
        stripped.write_text(
            "\n".join(
                line
                for line in log.path.read_text().splitlines()
                if (json.loads(line).get("step") or {}).get("kind") != "brief"
            )
            + "\n"
        )
        lg = explain_mod.load(stripped)
        doc = explain_mod.dossier(lg.turns[0], lg, tmp_path)
        assert doc["given"]["state"] == explain_mod.MISSING
        assert doc["given"]["carried"] is False


class TestWorthALook:
    """Facts about a turn, never causes — and never a claim of "all clear"."""

    def _doc(self, log, tmp_path):
        lg = explain_mod.load(log.path)
        return explain_mod.dossier(lg.turns[0], lg, tmp_path)

    def test_a_failed_call_is_flagged_and_cites_the_call(self, tmp_path, monkeypatch):
        agent, _, log = make_logged_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="nope")]),
                model_says("gave up"),
            ],
            tmp_path,
        )
        monkeypatch.setattr(
            agent_module.tools,
            "read_docs",
            lambda *a, **k: agent_module.tools.ToolOutcome(
                "no such page", ok=False, status="not_found"
            ),
        )
        agent.run_task("go")
        rows = self._doc(log, tmp_path)["notes"]["rows"]
        failed = [r for r in rows if r["check"] == "tool_failed"]
        assert failed, rows
        # The section a READER sees, not the record kind it came from: the
        # panel's three parts are given / flow / produced.
        assert failed[0]["where"] == {"section": "flow", "call": 1}
        assert "read_docs" in failed[0]["text"]

    def test_every_row_cites_where_it_came_from(self, tmp_path):
        """A row the reader cannot follow back to a record is an assertion, not
        evidence."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        for row in self._doc(log, tmp_path)["notes"]["rows"]:
            assert row["where"].get("section") in {"given", "flow", "produced"}
            assert row["text"].strip()

    def test_the_empty_case_names_the_checks_rather_than_claiming_all_clear(
        self, tmp_path, monkeypatch
    ):
        """"Nothing unusual in this turn" is a claim a checker is not entitled
        to make: it only knows the classes somebody coded, so on the one turn
        whose cause is an uncoded class it would state the opposite of the
        truth, directly above the evidence."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        monkeypatch.setattr(
            explain_mod,
            "notes",
            lambda doc: {"rows": [], "checks": [{"id": "x", "label": "y"}] * 4},
        )
        text = explain_mod.explain(log.path, root=tmp_path)
        assert "nothing flagged by the 4 checks this reader runs" in text
        assert "nothing unusual" not in text.lower()

    def test_only_a_NEAR_abstention_is_worth_a_look(self, tmp_path):
        """The rule corpus is evaluated in full against every turn, so "this
        rule did not apply" is the ordinary case for almost all of them. Listing
        them all put six rows of noise above the two real findings on the first
        live turn this ran against."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[0], lg, tmp_path)
        doc["given"]["rules"] = {
            "state": explain_mod.RECORDED,
            "corpus": {},
            "skipped": [],
            "dropped": 0,
            "groups": {
                "abstain": [
                    {"rule": "far-off", "evidence": {"sim": 0.10, "floor": 0.62}},
                    {"rule": "near-miss", "evidence": {"sim": 0.58, "floor": 0.62}},
                    {"rule": "no-distance", "evidence": {}},
                ]
            },
        }
        rows = [r for r in explain_mod.notes(doc)["rows"] if r["check"] == "rule_abstained"]
        assert len(rows) == 1, rows
        assert "near-miss" in rows[0]["text"]
        assert "far-off" not in rows[0]["text"]
        assert "no-distance" not in rows[0]["text"]

    def test_an_event_note_names_the_round_it_happened_before(self, tmp_path):
        """A row citing only "the flow" lands the reader on a section header,
        which is indistinguishable from the tap doing nothing."""
        path = tmp_path / "session-ev.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in [
            {"ts": "t", "kind": "task_start", "prompt": "go"},
            {"ts": "t", "kind": "trace", "step": {"kind": "reasoning", "model_call": 1,
                                                  "text": "first"}},
            {"ts": "t", "kind": "trace", "step": {"kind": "injected",
                                                  "text": "only Polish shops please"}},
            {"ts": "t", "kind": "trace", "step": {"kind": "reasoning", "model_call": 2,
                                                  "text": "second"}},
            {"ts": "t", "kind": "task_end", "status": "ok"},
        ]) + "\n")
        lg = explain_mod.load(path)
        doc = explain_mod.dossier(lg.turns[0], lg, tmp_path)
        steering = [r for r in doc["notes"]["rows"] if r["check"] == "steering"]
        assert steering, doc["notes"]["rows"]
        assert steering[0]["where"] == {"section": "flow", "model_call": 2}
        assert "before model call 2" in steering[0]["text"]

    def test_an_event_with_no_round_says_so_instead_of_implying_one(self, tmp_path):
        path = tmp_path / "session-loose.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in [
            {"ts": "t", "kind": "task_start", "prompt": "go"},
            {"ts": "t", "kind": "trace", "step": {"kind": "injected", "text": "wait"}},
            {"ts": "t", "kind": "task_end", "status": "ok"},
        ]) + "\n")
        lg = explain_mod.load(path)
        doc = explain_mod.dossier(lg.turns[0], lg, tmp_path)
        steering = [r for r in doc["notes"]["rows"] if r["check"] == "steering"]
        assert steering, doc["notes"]["rows"]
        assert "model_call" not in steering[0]["where"]
        assert "at some point in this turn" in steering[0]["text"]

    def test_a_seed_trim_still_earns_a_row(self, tmp_path):
        """It is not an event in the flow, but it is the same fact about the
        same turn — the model did not have what the transcript still shows it
        having. Reading events off the flow alone dropped these, and on a real
        turn it was the sharpest row on the list."""
        path = tmp_path / "session-seedtrim.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in [
            {"ts": "t", "kind": "task_start", "prompt": "go"},
            {"ts": "t", "kind": "trace", "step": {
                "kind": "trim", "policy": "eager_stub", "affected": 2,
                "stubbed": [{"at": 3, "tool": "web_search"}]}},
            {"ts": "t", "kind": "trace", "step": {"kind": "reasoning", "model_call": 1,
                                                  "text": "thinking"}},
            {"ts": "t", "kind": "task_end", "status": "ok"},
        ]) + "\n")
        lg = explain_mod.load(path)
        doc = explain_mod.dossier(lg.turns[0], lg, tmp_path)
        stub = [r for r in doc["notes"]["rows"] if r["check"] == "result_stubbed"]
        assert stub, doc["notes"]["rows"]
        assert stub[0]["where"] == {"section": "given"}
        assert "before the model was called at all" in stub[0]["text"]
        assert "web_search" in stub[0]["text"]

    def test_a_full_context_is_measured_against_the_RECORDED_window(self, tmp_path):
        """`num_ctx` is an Ollama option carried on every turn whatever the
        backend. Comparing against it called a Gemini turn at 5% of its
        million-token window "nearly full" — a confident wrong claim sitting at
        the top of the evidence, which is the one thing this pass must not do."""
        base = {
            "prompt": "p", "given": {"briefs": [], "rules": {}, "state": "recorded"},
            "thought": {"state": explain_mod.RECORDED, "calls": []},
            "did": {"calls": [], "orphan_gates": [], "verify": []},
            "produced": {"answer": "", "status": "ok", "error": "",
                         "verify": {"stopped": [], "advised": [], "passed": 0}},
            "steering": [], "trim": [],
            "flow": {"grouping": "none", "rounds": [], "unplaced": [], "loose": []},
        }

        def rows_for(options, tokens):
            doc = json.loads(json.dumps(base))
            doc["given"]["briefs"] = [{"options": options, "written_here": True,
                                       "in_force_since": None, "model_call": 1}]
            doc["thought"]["calls"] = [{"model_call": 1, "text": "", "truncated": 0,
                                        "cap_source": None, "said": "", "said_truncated": 0,
                                        "stop": "stop", "tokens": tokens, "blocks": [],
                                        "malformed": [], "synthesized": False}]
            return [r for r in explain_mod.notes(doc)["rows"] if r["check"] == "context_full"]

        gemini = {"provider": "gemini", "num_ctx": 32768, "window": 1048576}
        assert not rows_for(gemini, [50631, 100]), "a Gemini turn at 5% was called full"
        assert rows_for(gemini, [1_000_000, 100]), "a genuinely full window was not flagged"
        # Ollama: num_ctx IS the window, so an old log is still readable.
        assert rows_for({"provider": "ollama", "num_ctx": 32768}, [31000, 10])
        # Any other backend, with no window recorded: the reader cannot know.
        assert not rows_for({"provider": "gemini", "num_ctx": 32768}, [50631, 100])

    def test_notes_are_a_pure_function_of_the_dossier(self, tmp_path):
        """So the terminal and the panel surface the same list, and neither
        re-reads the log to build it."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        doc = self._doc(log, tmp_path)
        again = explain_mod.notes(doc)
        assert again["rows"] == doc["notes"]["rows"]


class TestTheFlow:
    """A turn read in the order it happened (#243 follow-up).

    Sectioned by record kind, a dossier cannot answer "what did it think after
    it got that result" — which is the question people open one to ask. The
    owner put it plainly: "I don't know which information was retrieved after
    which tool."
    """

    def _flow(self, log, tmp_path, ordinal=0):
        lg = explain_mod.load(log.path)
        return explain_mod.dossier(lg.turns[ordinal], lg, tmp_path)

    def test_each_call_sits_under_the_thinking_that_issued_it(self, tmp_path):
        agent, _, log = make_logged_agent(
            [
                model_says(tool_calls=[tool_call("read_docs", command="ls")],
                           thinking="check the docs first"),
                model_says(tool_calls=[tool_call("read_docs", command="grep")],
                           thinking="now the other one"),
                model_says("done", thinking="ready to answer"),
            ],
            tmp_path,
        )
        agent.run_task("go")
        doc = self._flow(log, tmp_path)
        flow = doc["flow"]
        assert flow["grouping"] == explain_mod.GROUPING_RECORDED
        assert flow["unplaced"] == []
        rounds = {r["model_call"]: r for r in flow["rounds"]}
        assert rounds[1]["calls"] == [1]
        assert rounds[2]["calls"] == [2]
        assert rounds[3]["calls"] == []
        thoughts = {t["model_call"]: t["text"] for t in doc["thought"]["calls"]}
        assert thoughts[1] == "check the docs first"
        assert thoughts[3] == "ready to answer"

    def test_rounds_reference_by_id_and_never_copy(self, tmp_path):
        """Tool output is the bulk of the payload and this is fetched to a
        phone; copying it into the rounds would double the response."""
        agent, _, log = make_logged_agent(
            [model_says(tool_calls=[tool_call("read_docs", command="ls")]), model_says("ok")],
            tmp_path,
        )
        agent.run_task("go")
        flow = self._flow(log, tmp_path)["flow"]
        for rnd in flow["rounds"]:
            assert all(isinstance(c, int) for c in rnd["calls"])
            assert rnd["thought"] is None or isinstance(rnd["thought"], int)

    def test_a_log_without_the_stamp_is_labelled_inferred(self, tmp_path):
        """File order is the real chronology within a turn, so attaching a call
        to the preceding reasoning is right for these files — but it is still an
        inference, and a reader must never see inference wearing a record's
        clothes."""
        agent, _, log = make_logged_agent(
            [model_says(tool_calls=[tool_call("read_docs", command="ls")]), model_says("ok")],
            tmp_path,
        )
        agent.run_task("go")
        stripped = tmp_path / "unstamped.jsonl"
        kept = []
        for line in log.path.read_text().splitlines():
            record = json.loads(line)
            step = record.get("step") or {}
            if step.get("kind") == "call":
                step.pop("model_call", None)
                line = json.dumps(record)
            kept.append(line)
        stripped.write_text("\n".join(kept) + "\n")
        lg = explain_mod.load(stripped)
        flow = explain_mod.dossier(lg.turns[0], lg, tmp_path)["flow"]
        assert flow["grouping"] == explain_mod.GROUPING_INFERRED
        assert flow["rounds"][0]["calls"] == [1], "the fallback did not attach it"
        assert "inferred" in explain_mod.explain(stripped, root=tmp_path)

    def test_a_call_with_no_round_to_belong_to_is_not_folded_into_round_one(self, tmp_path):
        """claude-max routes tool calls straight into _call_result without ever
        entering the model loop, so nothing recorded issued them. Filing them
        under round 1 would be a fabricated join."""
        # The claude-max shape for real: tool calls, and no `reasoning` record
        # anywhere to infer an order from.
        path = tmp_path / "session-sdk.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in [
            {"ts": "t", "kind": "task_start", "prompt": "go"},
            {"ts": "t", "kind": "trace",
             "step": {"kind": "call", "call": 1, "name": "read_docs", "args": {"command": "ls"}}},
            {"ts": "t", "kind": "trace",
             "step": {"kind": "tool", "call": 1, "name": "read_docs", "ok": True, "secs": 0.1}},
            {"ts": "t", "kind": "task_end", "status": "ok"},
        ]) + "\n")
        lg = explain_mod.load(path)
        flow = explain_mod.dossier(lg.turns[0], lg, tmp_path)["flow"]
        assert flow["grouping"] == explain_mod.GROUPING_NONE
        assert flow["unplaced"] == [1], "the call vanished"
        assert flow["rounds"] == [], "a round was invented for a backend that records none"
        assert "records no model calls" in explain_mod.explain(path, root=tmp_path)

    def test_the_claude_max_path_records_no_model_call_at_all(self, tmp_path):
        """A zero would say "round zero"; absence says "this backend records no
        model calls". They route to different repairs."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        agent._call_result("read_docs", lambda: ("out", 0.1), args={"command": "ls"})
        emitted = steps(log.path, "call")
        assert "model_call" not in emitted[-1], emitted[-1]

    def test_a_mid_task_stub_shows_up_where_it_happened(self, tmp_path):
        """The sharpest between-round event: a result the model had already read
        was replaced with a stub before the next call. Sectioned by kind, that
        fact sat nowhere near the thinking it explains."""
        agent, _, log = make_logged_agent(
            [model_says(tool_calls=[tool_call("read_docs", command="ls")]), model_says("ok")],
            tmp_path,
        )
        agent.run_task("go")
        rows = log.path.read_text().splitlines()
        # A mid-task trim, written where the agent writes one: before a call.
        for index, line in enumerate(rows):
            if (json.loads(line).get("step") or {}).get("model_call") == 2:
                rows.insert(index, json.dumps({
                    "ts": "t", "kind": "trace",
                    "step": {"kind": "trim", "policy": "mid_task_budget", "affected": 1,
                             "stubbed": [{"at": 3, "tool": "read_docs"}]},
                }))
                break
        path = tmp_path / "trimmed.jsonl"
        path.write_text("\n".join(rows) + "\n")
        lg = explain_mod.load(path)
        flow = explain_mod.dossier(lg.turns[0], lg, tmp_path)["flow"]
        events = [e for r in flow["rounds"] for e in r["before"]]
        loose = flow["loose"]
        assert events or loose, "the trim disappeared from the flow"
        out = explain_mod.explain(path, root=tmp_path)
        assert "were stubbed for the model" in out

    def test_a_seed_time_trim_stays_out_of_the_flow(self, tmp_path):
        """It happened before the first model call, so it shaped what the turn
        STARTED from. Putting it in a round asserts a causality that is false."""
        path = tmp_path / "session-seed.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in [
            {"ts": "t", "kind": "task_start", "prompt": "go"},
            {"ts": "t", "kind": "trace", "step": {"kind": "trim", "policy": "eager_stub",
                                                  "affected": 2, "bytes_before": 10,
                                                  "bytes_after": 5}},
            {"ts": "t", "kind": "trace", "step": {"kind": "reasoning", "model_call": 1,
                                                  "text": "thinking"}},
            {"ts": "t", "kind": "task_end", "status": "ok"},
        ]) + "\n")
        lg = explain_mod.load(path)
        doc = explain_mod.dossier(lg.turns[0], lg, tmp_path)
        assert doc["given"]["trims"], "the seed trim was dropped"
        assert not [e for r in doc["flow"]["rounds"] for e in r["before"]]
        assert "eager_stub" in explain_mod.explain(path, root=tmp_path)


class TestUsageOnTheReasoningRecord:
    """The provider's usage report reaches the log with its units intact (#262).

    Without it, "how many tokens did I spend yesterday" sums three different
    quantities: input INCLUDING cache on OpenAI-shaped providers, EXCLUDING it
    on Anthropic, and excluding KV-cache reuse on Ollama. And the cache split —
    what actually decides whether a 120k-token resend was expensive or nearly
    free — was discarded at the adapter, recoverable only from the provider's
    documentation as it reads today. Evidence that decays.
    """

    def test_the_report_lands_on_the_record_that_already_carries_tokens(self, tmp_path):
        from aish import backends

        detail = backends.usage_detail(
            backends.INPUT_INCLUDES_CACHE, input=129_623, cached=100_000, output=250
        )
        reply = model_says("done")
        reply.usage = detail
        reply.prompt_eval_count, reply.eval_count = 129_623, 250
        agent, _, log = make_logged_agent([reply], tmp_path)
        agent.run_task("hi")
        (record,) = steps(log.path, "reasoning")
        assert record["usage"] == detail
        # The two-int summary stays put beside it — everything downstream sizes
        # context from it, and a scanner needs both to show the residual.
        assert record["tokens"] == [129_623, 250]

    def test_ollama_is_labelled_rather_than_assumed(self, tmp_path):
        """Ollama attaches no report, so the meaning of its two counts has to
        travel with them or a reader supplies the wrong one."""
        from aish import backends

        reply = model_says("done")
        reply.prompt_eval_count, reply.eval_count = 900, 40
        agent, _, log = make_logged_agent([reply], tmp_path)
        agent.run_task("hi")
        (record,) = steps(log.path, "reasoning")
        assert record["usage"]["semantics"] == backends.INPUT_EXCLUDES_KV_REUSE
        assert record["usage"]["input"] == 900

    def test_a_call_that_reported_nothing_records_no_usage(self, tmp_path):
        """Absent, not zeroed: a provider that said nothing and a turn that cost
        nothing are different facts."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hi")
        (record,) = steps(log.path, "reasoning")
        assert "usage" not in record


class TestTheEvidenceFrameOnTheRecord:
    """#289 slice 1. aish drives pages the owner cannot see, so a frame is the
    only way to check what one said. The record must be complete enough to be
    worth trusting: a reference that RESOLVES, or an honest statement that the
    bytes are gone.

    The frame proves what happened. It prevents nothing, and nothing anywhere
    is permitted or widened on the strength of it.
    """

    def _browsed(self, tmp_path, monkeypatch, outcome):
        agent, _, log = make_logged_agent(
            [
                model_says(tool_calls=[tool_call("browse", url="https://eon.pl/x")]),
                model_says("done"),
            ],
            tmp_path,
        )
        agent._approved_sites.add("eon.pl")
        monkeypatch.setattr(agent_module.web, "browse", lambda *a, **kw: outcome)
        agent.run_task("what does the portal say")
        return log

    def _stored(self, tmp_path):
        path = tmp_path / "media" / "abc123-browse-eon-pl.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xd8\xff\xe0 a picture of the page")
        return path

    def test_the_tool_step_points_at_the_bytes_and_never_carries_them(
        self, tmp_path, monkeypatch
    ):
        from aish import tools

        picture = self._stored(tmp_path)
        log = self._browsed(
            tmp_path, monkeypatch, tools.ToolOutcome("the page", frame=str(picture))
        )
        (step,) = [s for s in steps(log.path, "tool") if s["name"] == "browse"]
        assert step["frame"] == str(picture)
        # Bulk bytes never enter the log — the record only POINTS at them, and
        # that is what keeps them purgeable on their own schedule. None of the
        # picture's own content is in the file, escaped or not.
        written = log.path.read_text()
        assert "a picture of the page" not in written
        assert "\\ud8" not in written and "\\u00ff" not in written

    def test_explain_points_at_a_frame_that_is_still_there(self, tmp_path, monkeypatch):
        from aish import tools

        picture = self._stored(tmp_path)
        log = self._browsed(
            tmp_path, monkeypatch, tools.ToolOutcome("the page", frame=str(picture))
        )
        out = explain_mod.explain(log.path, root=tmp_path)
        assert str(picture) in out
        assert "purged" not in out.split("browse")[-1]

    def test_a_frame_the_store_evicted_reads_as_gone_not_as_never_taken(
        self, tmp_path, monkeypatch
    ):
        """The media store is a bounded LRU cache, so a record outliving its
        picture is the NORMAL end of a frame's life. It must never read as
        though nothing was captured — those route to different repairs."""
        from aish import tools

        picture = self._stored(tmp_path)
        log = self._browsed(
            tmp_path, monkeypatch, tools.ToolOutcome("the page", frame=str(picture))
        )
        picture.unlink()
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert call["frame"] == str(picture)
        assert call["frame_state"] == explain_mod.PURGED
        assert "purged" in explain_mod.explain(log.path, root=tmp_path)

    def test_a_page_nobody_could_picture_says_which_page_that_was(
        self, tmp_path, monkeypatch
    ):
        from aish import browse as browse_mod
        from aish import tools

        log = self._browsed(
            tmp_path,
            monkeypatch,
            tools.ToolOutcome(
                "a page with a password box",
                frame_skipped=browse_mod.NO_FRAME_PASSWORD,
            ),
        )
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert call["frame"] == ""
        assert call["frame_skipped"] == browse_mod.NO_FRAME_PASSWORD
        assert "frame_state" not in call
        assert browse_mod.NO_FRAME_PASSWORD in explain_mod.explain(log.path, root=tmp_path)

    def test_a_call_that_recorded_nothing_claims_no_capture_was_considered(
        self, tmp_path
    ):
        """A log written before frames existed, and every tool that has no
        page. Absence of the key is the third state, and it must not be
        manufactured into an empty one."""
        agent, _, log = make_logged_agent([model_says("done")], tmp_path)
        agent.run_task("hello")
        lg = explain_mod.load(log.path)
        for call in explain_mod.dossier(lg.turns[-1], lg, tmp_path)["did"]["calls"]:
            assert "frame" not in call
            assert "console" not in call and "signin" not in call

    def test_the_picture_is_captioned_with_what_the_action_did(
        self, tmp_path, monkeypatch
    ):
        """A frame on its own answers "what did this page look like"; the
        question asked of a browse step is "what did this press do". The
        caption is carried, never re-derived here — a reader guessing at a
        navigation would be a second answer to a question with one."""
        from aish import tools

        picture = self._stored(tmp_path)
        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome(
                "the page",
                frame=str(picture),
                frame_url="https://eon.pl/mojeon/faktury",
                frame_from="https://eon.pl/mojeon",
            ),
        )
        out = explain_mod.explain(log.path, root=tmp_path)
        assert "taken at https://eon.pl/mojeon/faktury" in out
        assert "aish was on https://eon.pl/mojeon before this" in out

    def test_a_frame_from_an_older_log_grows_no_caption(self, tmp_path, monkeypatch):
        """No key, no sentence. Inventing "unknown" would be this reader
        claiming the writer tried and failed to record an address."""
        from aish import tools

        picture = self._stored(tmp_path)
        log = self._browsed(
            tmp_path, monkeypatch, tools.ToolOutcome("the page", frame=str(picture))
        )
        out = explain_mod.explain(log.path, root=tmp_path)
        assert "taken at" not in out and "the address did not change" not in out


class TestTheConsoleAndTheSignInOnTheRecord:
    """Reading back the two things a whole day of eon.pl diagnosis did not
    have: what the page said, and what the sign-in attempt's own page looked
    like. `aish explain` assembles from recorded evidence only — it may never
    re-derive either from source."""

    # Borrowed rather than subclassed: inheriting the class above would re-run
    # every one of its tests for nothing.
    _browsed = TestTheEvidenceFrameOnTheRecord._browsed
    _stored = TestTheEvidenceFrameOnTheRecord._stored

    def test_the_pages_console_is_read_back_as_the_pages_words(
        self, tmp_path, monkeypatch
    ):
        from aish import tools

        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome(
                "the page",
                console=["uncaught: ReferenceError: grecaptcha is not defined"],
            ),
        )
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert call["console"] == [
            "uncaught: ReferenceError: grecaptcha is not defined"
        ]
        out = explain_mod.explain(log.path, root=tmp_path)
        assert "grecaptcha is not defined" in out
        # A dossier is read by a person and can be pasted to a model, so the
        # same discipline applies here as in the tool result: these are the
        # page's words and must never read as aish's account of itself.
        assert "the page's words" in out

    def test_the_sign_in_attempt_is_read_back_as_a_different_page(
        self, tmp_path, monkeypatch
    ):
        from aish import tools

        picture = self._stored(tmp_path)
        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome(
                "the signed-out page",
                signin={
                    "host": "eon.pl",
                    "frame": str(picture),
                    "console": ["uncaught: ReferenceError: grecaptcha is not defined"],
                },
            ),
        )
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert call["signin"]["host"] == "eon.pl"
        assert call["signin"]["frame_state"] == explain_mod.RECORDED
        out = explain_mod.explain(log.path, root=tmp_path)
        assert "attempted an automatic sign-in at eon.pl" in out
        assert "picture of the sign-in page" in out
        assert "the sign-in page wrote to its own console" in out

    def test_a_sign_in_picture_the_store_dropped_reads_as_purged(
        self, tmp_path, monkeypatch
    ):
        from aish import tools

        picture = self._stored(tmp_path)
        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome(
                "the signed-out page",
                signin={"host": "eon.pl", "frame": str(picture)},
            ),
        )
        picture.unlink()
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert call["signin"]["frame_state"] == explain_mod.PURGED
        assert "purged" in explain_mod.explain(log.path, root=tmp_path)

    def test_what_covered_a_control_is_read_back_as_aishs_own_observation(
        self, tmp_path, monkeypatch
    ):
        """#321. A press that never landed writes nothing to a console, because
        nothing ran — so the driver is the only witness, and if that witness is
        heard only by the acting model it is one restart from being lost."""
        from aish import tools

        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome(
                "the page",
                covered={"by": "clb clb-container", "dismissed": False},
            ),
        )
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert call["covered"] == {"by": "clb clb-container", "dismissed": False}
        out = explain_mod.explain(log.path, root=tmp_path)
        assert "a click could not land" in out
        assert "'clb clb-container'" in out
        # It is aish's own account of its own hands, so it does NOT arrive
        # under the label a console line does — a press that never landed
        # produces no console line to be labelled.
        assert "wrote to its own console" not in out

    def test_a_dismissed_cover_says_it_was_dismissed(self, tmp_path, monkeypatch):
        from aish import tools

        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome(
                "the page", covered={"by": "cookie-bar", "dismissed": True}
            ),
        )
        out = explain_mod.explain(log.path, root=tmp_path)
        assert "aish dismissed it and clicked again" in out

    def test_a_cover_block_with_no_element_is_no_cover(self, tmp_path, monkeypatch):
        """Absence must never be the evidence. A step that names nothing says
        nothing about coverage — which includes every step written before
        this."""
        from aish import tools

        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome("the page", covered={"by": "", "dismissed": False}),
        )
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert "covered" not in call
        assert "a click could not land" not in explain_mod.explain(
            log.path, root=tmp_path
        )

    def test_the_sign_in_pages_cover_is_read_back_too(self, tmp_path, monkeypatch):
        from aish import tools

        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome(
                "the signed-out page",
                signin={"host": "eon.pl", "covered": "clb clb-container"},
            ),
        )
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert call["signin"]["covered"] == "clb clb-container"
        assert "a click could not land" in explain_mod.explain(log.path, root=tmp_path)

    def test_a_block_with_no_host_is_no_attempt(self, tmp_path, monkeypatch):
        """`host` is what says an attempt happened at all — an empty block must
        read as "no sign-in", never as an attempt with nothing to show."""
        from aish import tools

        log = self._browsed(
            tmp_path, monkeypatch, tools.ToolOutcome("the page", signin={})
        )
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert "signin" not in call

    def test_the_console_is_read_back_even_for_a_clean_step(
        self, tmp_path, monkeypatch
    ):
        """The OPPOSITE rule from the chat timeline, on purpose. The timeline
        is a stream the owner scrolls past, so a clean step's console is
        surfaced nowhere there; a dossier is opened deliberately, about one
        turn, and hiding recorded evidence from it would re-create the eon.pl
        gap — a load-time error on a clean open whose damage only shows on a
        later press is readable ONLY here."""
        from aish import tools

        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome(
                "the page",
                status=tools.STATUS_OK,
                verdict_by=tools.VERDICT_EXIT_CODE,
                console=["error: the site's everyday noise"],
            ),
        )
        out = explain_mod.explain(log.path, root=tmp_path)
        assert "the site's everyday noise" in out
        assert "the page's words" in out

    def test_aishs_own_problem_sentence_is_read_back(
        self, tmp_path, monkeypatch
    ):
        from aish import tools

        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome(
                "the page", problem="could not click 'Zaloguj': it is inert"
            ),
        )
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert call["problem"] == "could not click 'Zaloguj': it is inert"
        out = explain_mod.explain(log.path, root=tmp_path)
        assert "problem: could not click 'Zaloguj': it is inert" in out

    def test_an_empty_delta_is_read_back_as_the_fact_it_is(
        self, tmp_path, monkeypatch
    ):
        from aish import tools

        log = self._browsed(
            tmp_path, monkeypatch,
            tools.ToolOutcome("the page", unchanged=True),
        )
        lg = explain_mod.load(log.path)
        doc = explain_mod.dossier(lg.turns[-1], lg, tmp_path)
        call = next(c for c in doc["did"]["calls"] if c["name"] == "browse")
        assert call["unchanged"] is True
        assert "nothing on the page changed when aish did this" in (
            explain_mod.explain(log.path, root=tmp_path)
        )

    def test_the_sign_in_outcome_is_read_back_in_three_states(
        self, tmp_path, monkeypatch
    ):
        """True, false and absent are three facts: the session came up, it was
        not seen to, and a log written before the outcome was recorded. The
        dossier may say exactly as much as each one earns and no more."""
        from aish import tools

        for name, block, said, unsaid in (
            ("up", {"host": "eon.pl", "ok": True},
             "the session came up", "not seen to come up"),
            ("down", {"host": "eon.pl", "ok": False},
             "the session was not seen to come up", None),
            ("older", {"host": "eon.pl"},
             "attempted an automatic sign-in at eon.pl", "the session"),
        ):
            root = tmp_path / name
            root.mkdir()
            log = self._browsed(
                root, monkeypatch,
                tools.ToolOutcome("the page", signin=dict(block)),
            )
            out = explain_mod.explain(log.path, root=root)
            assert said in out
            if unsaid:
                assert unsaid not in out
