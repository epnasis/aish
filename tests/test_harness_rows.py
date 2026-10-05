"""Everything aish itself says to the model is a trace row (contract §3.17).

Before this, an `[aish: …]` note reached the model as a user message, was logged
as a message record, and was drawn NOWHERE: live nothing emitted a step, and
replay skipped the record (#171). The owner watched local models answer "the
user wants me to add chips" to a note he could not see. These tests pin:

- every note becomes a row, live and on replay, from the SAME record;
- a note written between tasks waits for the next task's card on both paths;
- a log written before sources existed still draws its notes;
- nothing the model received that the owner did not type goes unrowed;
- no new path appends a user message around `_append`, the one door the
  row is decided at.
"""

import json
import re
from pathlib import Path

import pytest

from aish import rules as rules_module
from aish.agent import AISH_NOTE, Agent
from aish.session import HARNESS_STEP, SessionLog, harness_step
from tests.test_agent import FakeChat, model_says, tool_call, write_rule

CHIPS_RULE = """---
name: quick-reply-chips
description: If the answer ends with a question, give me tap buttons.
when:
  answer:
    matches: '[?]\\s*$'
    in: ending
then:
  answer_must_include:
    pattern: 'aish-reply://'
---
"""


def wired(tmp_path, responses, monkeypatch, rule_texts=()):
    """An agent writing to a real SessionLog the way both entry points wire
    it, plus the list of steps a live renderer would have received."""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir(exist_ok=True)
    for i, text in enumerate(rule_texts):
        write_rule(rules_dir, f"r{i}", text)
    monkeypatch.setattr(rules_module, "GLOBAL_RULES_DIR", rules_dir)
    log = SessionLog(tmp_path / "session-20261005-000000-000000.jsonl")
    live: list[dict] = []
    chat = FakeChat(responses)
    agent = Agent(
        model="fake", approve=lambda _c: True, client_chat=chat, cwd=str(tmp_path),
        on_message=log.message, step_log=log.step, on_step=live.append,
    )
    return agent, chat, log, live


def run(agent, log, prompt):
    """One turn, logged the way the server brackets it."""
    log.task_start(prompt)
    try:
        return agent.run_task(prompt)
    finally:
        log.task_end()


def harness(steps):
    return [s for s in steps if s.get("kind") == HARNESS_STEP]


def cold_steps(log):
    events = SessionLog.reconstruct_events(log.path) or []
    return [{k: v for k, v in e.items() if k != "type"} for e in events if e.get("type") == "step"]


TIMELINE = ("thinking_start", "thinking_cancel", "thinking", HARNESS_STEP)


def timeline(steps):
    return [s["kind"] for s in steps if s.get("kind") in TIMELINE]


class TestANoteIsARow:
    def test_an_answer_check_is_drawn_live_with_the_exact_text_the_model_got(
        self, tmp_path, monkeypatch
    ):
        agent, chat, log, live = wired(
            tmp_path,
            [model_says("Want me to check more?"),
             model_says("Want me to check more?\n[Yes](aish-reply://yes)")],
            monkeypatch, rule_texts=(CHIPS_RULE,),
        )
        run(agent, log, "anything new?")
        rows = harness(live)
        assert len(rows) == 1
        sent = [m for m in chat.snapshots[1] if m.get("role") == "user"][-1]
        assert rows[0]["text"] == sent["content"]  # byte-identical to what it received
        assert rows[0]["text"].startswith(AISH_NOTE)
        assert "quick-reply-chips" in rows[0]["text"]
        assert rows[0]["source"] == "answer_check"
        assert rows[0]["role"] == "user"

    def test_replay_draws_the_same_row_in_the_same_place(self, tmp_path, monkeypatch):
        agent, _, log, live = wired(
            tmp_path,
            [model_says("Want me to check more?"),
             model_says("Want me to check more?\n[Yes](aish-reply://yes)")],
            monkeypatch, rule_texts=(CHIPS_RULE,),
        )
        run(agent, log, "anything new?")
        cold = cold_steps(log)
        assert harness(cold) == harness(live)
        assert timeline(cold) == timeline(live)

    def test_the_text_is_not_copied_into_the_log(self, tmp_path, monkeypatch):
        """One durable copy — the message record — so a redaction or a scrub
        has exactly one thing to act on."""
        agent, _, log, _ = wired(
            tmp_path,
            [model_says("Want me to check more?"),
             model_says("Want me to check more?\n[Yes](aish-reply://yes)")],
            monkeypatch, rule_texts=(CHIPS_RULE,),
        )
        run(agent, log, "anything new?")
        raw = log.path.read_text()
        assert '"kind": "harness"' not in raw
        notes = [r for r in map(json.loads, raw.splitlines())
                 if r.get("kind") == "message" and str(r.get("content", "")).startswith(AISH_NOTE)]
        assert len(notes) == 1

    def test_a_stop_is_drawn_and_named(self, tmp_path, monkeypatch):
        again = tool_call("read_docs", command="ls")
        agent, _, log, live = wired(
            tmp_path, [model_says(tool_calls=[again])] * 8 + [model_says("stopped.")],
            monkeypatch,
        )
        monkeypatch.setattr("aish.agent.tools.read_docs", lambda *a, **k: "same")
        run(agent, log, "look it up")
        sources = [s.get("source") for s in harness(live)]
        assert "loop_stop" in sources
        assert harness(cold_steps(log)) == harness(live)


class TestBetweenTasks:
    def test_a_cd_waits_for_the_next_tasks_card_on_both_paths(self, tmp_path, monkeypatch):
        other = tmp_path / "elsewhere"
        other.mkdir()
        agent, _, log, live = wired(
            tmp_path, [model_says("first"), model_says("second")], monkeypatch
        )
        run(agent, log, "one")
        agent.rebase(str(other))
        assert harness(live) == [], "a row with no task open is a ghost card"
        before = len(live)
        run(agent, log, "two")
        drawn = harness(live[before:])
        assert [s["source"] for s in drawn] == ["cd"]
        assert live[before]["kind"] == HARNESS_STEP, "drawn first thing in the next task"

        events = SessionLog.reconstruct_events(log.path) or []
        users = [i for i, e in enumerate(events) if e.get("type") == "user"]
        notes = [i for i, e in enumerate(events) if e.get("kind") == HARNESS_STEP]
        assert len(users) == 2 and len(notes) == 1
        assert notes[0] > users[1], "replayed into the turn it was drawn in"

    def test_a_note_after_the_last_task_waits_rather_than_inventing_a_turn(
        self, tmp_path, monkeypatch
    ):
        agent, _, log, live = wired(tmp_path, [model_says("done")], monkeypatch)
        run(agent, log, "one")
        agent.add_system_note(AISH_NOTE + "a picture did not display]", source="render_error")
        assert harness(live) == []
        assert harness(cold_steps(log)) == []
        assert agent._held_harness and agent._held_harness[0]["source"] == "render_error"


class TestOlderLogs:
    def test_a_note_logged_before_sources_existed_still_draws(self, tmp_path):
        log = SessionLog(tmp_path / "session-20260901-000000-000000.jsonl")
        log.task_start("hi")
        log.message({"role": "user", "content": "hi"})
        log.step({"kind": "thinking_start"})
        log.message({"role": "user", "content": AISH_NOTE + "you have reached the step limit]"})
        log.step({"kind": "thinking", "secs": 1})
        log.message({"role": "assistant", "content": "ok"})
        log.task_end()
        rows = harness(cold_steps(log))
        assert rows == [{"kind": HARNESS_STEP, "role": "user",
                         "text": AISH_NOTE + "you have reached the step limit]"}]

    def test_the_builder_states_only_what_the_record_holds(self):
        step = harness_step({"role": "user", "content": "[aish: x]", "model_call": 3,
                             "source": "stall_stop", "turn": "abc"})
        assert step == {"kind": HARNESS_STEP, "text": "[aish: x]", "role": "user",
                        "source": "stall_stop", "model_call": 3}


class TestNothingTheModelGotGoesUnrowed:
    """The suite-level form of the guarantee: across scripted paths, every
    user-role message any model call received is either what the owner typed
    or a row the owner could see."""

    SCENARIOS = {
        "answer_check": (
            [model_says("More?"), model_says("More?\n[Yes](aish-reply://yes)")],
            (CHIPS_RULE,),
        ),
        "empty_reply": ([model_says(""), model_says("here it is")], ()),
        "repeating_calls": (
            [model_says(tool_calls=[tool_call("read_docs", command="ls")])] * 8
            + [model_says("stopped.")],
            (),
        ),
    }

    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    def test_every_user_message_sent_is_typed_or_drawn(self, name, tmp_path, monkeypatch):
        responses, rule_texts = self.SCENARIOS[name]
        agent, chat, log, live = wired(tmp_path, responses, monkeypatch, rule_texts=rule_texts)
        monkeypatch.setattr("aish.agent.tools.read_docs", lambda *a, **k: "same")
        typed = "do the thing"
        run(agent, log, typed)
        drawn = {s["text"] for s in harness(live)}
        assert drawn, f"{name}: the scenario never nudged — it tests nothing"
        for snapshot in chat.snapshots:
            for message in snapshot:
                if message.get("role") != "user":
                    continue
                content = message.get("content") or ""
                assert content == typed or content in drawn, (
                    f"{name}: the model received a user message the owner neither "
                    f"typed nor could see: {content[:120]!r}"
                )


class TestOneDoor:
    """`_append` is where a note becomes a row. A message written into the
    conversation around it is one no row can follow — so every direct write
    is listed here with the reason it may exist, and a new one fails until
    someone decides what draws it."""

    ALLOWED = {
        # `_append` itself: the door.
        "self.messages.append(message)": 1,
        # Reopening a stored chat: history the log already holds, not new words.
        "self.messages.extend(": 1,
        # Steering: the owner's own words, drawn by their own `injected` row.
        'self.messages.append({"role": "user", "content": msg})': 1,
        # The per-task reminder. KNOWN GAP (slice 1b): drawn only as the
        # `knowledge` row, and only when something was preloaded.
        'self.messages.append({"role": "system", "content": reminder})': 1,
        # A held proposal — the model's own words, logged on release.
        "self.messages.append(entry)": 2,
    }

    def test_every_direct_write_to_the_conversation_is_accounted_for(self):
        source = Path("aish/agent.py").read_text(encoding="utf-8")
        found: dict[str, int] = {}
        for match in re.finditer(r"self\.messages\.(?:append|insert|extend)\([^\n]*", source):
            found[match.group(0).strip()] = found.get(match.group(0).strip(), 0) + 1
        assert found == self.ALLOWED, (
            "a message written around Agent._append can never become a trace row; "
            f"decide what draws it before adding it here: {found}"
        )


class TestTheStepScreenHasTheStep:
    """A tap on the row opens its step (`h<n>`) — the dossier must list one,
    in the turn the card drew the row in, or the tap is a dead control."""

    def test_a_note_is_a_step_with_its_exact_text(self, tmp_path, monkeypatch):
        from aish import explain

        agent, chat, log, _ = wired(
            tmp_path,
            [model_says("More?"), model_says("More?\n[Yes](aish-reply://yes)")],
            monkeypatch, rule_texts=(CHIPS_RULE,),
        )
        run(agent, log, "anything new?")
        lg = explain.load(log.path)
        steps = explain.dossier(lg.turns[0], lg, tmp_path)["steps"]
        notes = [s for s in steps if s["kind"] == explain.STEP_HARNESS]
        assert [s["id"] for s in notes] == ["h1"]
        sent = [m for m in chat.snapshots[1] if m.get("role") == "user"][-1]["content"]
        assert notes[0]["text"] == sent
        assert {"k": "written by", "v": "answer_check"} in notes[0]["facts"]
        # "The record" is the log's own line, not the derived step.
        assert notes[0]["record"]["kind"] == "message"
        assert notes[0]["record"]["content"] == sent
        # Filed between the two model calls it sat between.
        kinds = [s["kind"] for s in steps]
        assert kinds.index(explain.STEP_HARNESS) > kinds.index("model_call")
        assert "harness" not in lg.kinds_present, "a derived step is not what the writer logged"

    def test_a_between_task_note_is_filed_in_the_next_turn(self, tmp_path, monkeypatch):
        from aish import explain

        other = tmp_path / "elsewhere"
        other.mkdir()
        agent, _, log, _ = wired(tmp_path, [model_says("one"), model_says("two")], monkeypatch)
        run(agent, log, "first")
        agent.rebase(str(other))
        run(agent, log, "second")
        lg = explain.load(log.path)
        first, second = (explain.dossier(t, lg, tmp_path)["steps"] for t in lg.turns)
        assert [s["kind"] for s in first if s["kind"] == explain.STEP_HARNESS] == []
        assert [s["record"].get("source") for s in second
                if s["kind"] == explain.STEP_HARNESS] == ["cd"]


class TestDeliveryReviewFindings:
    """Three paths the delivery review traced to a false row or a wrong step."""

    def test_a_note_before_the_first_turn_keeps_every_tap_on_its_own_step(
        self, tmp_path, monkeypatch
    ):
        """A /cd before the first message: the card draws it first on turn 1,
        so the step list must too — or every later row opens its neighbour's
        text as "what aish told the model"."""
        from aish import explain

        other = tmp_path / "elsewhere"
        other.mkdir()
        agent, _, log, _ = wired(tmp_path, [model_says(""), model_says("here")], monkeypatch)
        agent.rebase(str(other))
        run(agent, log, "go")
        cards = [s["text"] for s in harness(cold_steps(log))]
        lg = explain.load(log.path)
        steps = [s for s in explain.dossier(lg.turns[0], lg, tmp_path)["steps"]
                 if s["kind"] == explain.STEP_HARNESS]
        assert len(cards) == 2, "the scenario must draw a /cd row and a nudge row"
        assert [s["text"] for s in steps] == cards, "row h<n> would open another note"
        assert [s["id"] for s in steps] == ["h1", "h2"]

    def test_a_new_chat_does_not_inherit_a_held_note(self, tmp_path, monkeypatch):
        other = tmp_path / "elsewhere"
        other.mkdir()
        agent, _, log, live = wired(tmp_path, [model_says("one"), model_says("two")], monkeypatch)
        run(agent, log, "first")
        agent.rebase(str(other))
        agent.reset()
        before = len(live)
        run(agent, log, "second")
        assert harness(live[before:]) == [], "a row for a note this chat's model never got"

    def test_retry_draws_the_held_note_on_the_attempt_that_replaces_it(
        self, tmp_path, monkeypatch
    ):
        other = tmp_path / "elsewhere"
        other.mkdir()
        agent, _, log, live = wired(
            tmp_path, [model_says("one"), model_says("two"), model_says("two again")],
            monkeypatch,
        )
        run(agent, log, "first")
        agent.rebase(str(other))
        run(agent, log, "second")
        assert agent.rewind_last_task() == "second"
        assert any(m.get("content", "").startswith("[I moved") for m in agent.messages), (
            "the note stays in front of the question — which is why it is drawn again"
        )
        before = len(live)
        agent.run_task("second")
        assert [s.get("source") for s in harness(live[before:])] == ["cd"]
