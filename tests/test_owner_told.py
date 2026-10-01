"""What the owner and the model are TOLD about things aish decided.

Four seams, each from a real session on a local model (2026-09-29, 2026-10-01):

- the note to a model that cannot see says whose limit it is, so the model
  stops telling the owner his chat cannot show pictures;
- a comment on a card comes back with the reason the card recorded, so the
  model answers "why did I have to approve this?" from it instead of inventing;
- `must_first: answer` lets aish's own guidance be read before the first word;
- a change that ran with NO card is announced by aish itself, live, in the log
  and on a cold replay.

Same harness as tests/test_agent.py: FakeChat scripts the model, plugin tools
are tiny shell wrappers in a tmp project, nothing reaches a network.
"""

import stat
from types import SimpleNamespace

import pytest

from aish import agent as agent_module
from aish import recipients, vault_writes
from aish.approval import Approved, Denied, Licensed
from aish.session import SessionLog
from tests.test_agent import (
    PNG_BYTES,
    make_agent,
    model_says,
    rules_agent,
    tool_call,
    tool_messages,
)

# --------------------------------------------------------------------------
# 1 · the no-vision note
# --------------------------------------------------------------------------


class TestNoVisionNoteSaysWhoseLimitItIs:
    """Twice a no-vision model read "they were NOT delivered" as "the user
    cannot see pictures": it told the owner show_image "won't actually show
    photos in this chat" and listed raw image URLs instead."""

    def _note(self, tmp_path, monkeypatch) -> str:
        monkeypatch.setattr(
            agent_module.web, "fetch_binary", lambda url, max_bytes: (PNG_BYTES, "image/png")
        )
        agent, _ = make_agent([
            model_says(tool_calls=[tool_call("show_image", source="https://ex.com/a.png")]),
            model_says("done"),
        ], state_dir=tmp_path)
        agent.provider = "no-vision-backend"
        agent.run_task("show me")
        [note] = [m for m in agent.messages if "cannot see images" in str(m.get("content"))]
        return note["content"]

    def test_the_limit_is_the_models_and_the_user_does_see_pictures(
        self, tmp_path, monkeypatch
    ):
        note = self._note(tmp_path, monkeypatch)
        assert "YOU cannot see images" in note
        assert "YOURS ONLY" in note
        assert "the user's chat DOES display pictures" in note
        assert "user sees each one where you paste the line show_image returned" in note
        # The phrase that was misread is gone.
        assert "NOT delivered" not in note

    def test_the_obligations_survive(self, tmp_path, monkeypatch):
        note = self._note(tmp_path, monkeypatch)
        assert "MUST paste that line exactly as written" in note
        assert "Do NOT describe what is in them" in note
        assert "do NOT replace the line with a raw image URL" in note
        assert "Do NOT tell the user pictures cannot be shown" in note

    def test_a_producer_with_no_display_line_still_gets_no_paste_instruction(self):
        note = agent_module.TOOL_MEDIA_UNDELIVERABLE.format(tools="read_pdf", count=1, paste="")
        assert "paste" not in note
        assert "YOU cannot see images" in note


# --------------------------------------------------------------------------
# 2 · the card's reason travels with the comment
# --------------------------------------------------------------------------


def _write_tool(cwd, name, *, schema, script="#!/bin/sh\ncat\n", preview=False):
    tdir = cwd / ".aish" / "tools" / name
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "TOOL.md").write_text(
        f"---\nname: {name}\ndescription: d\nexec: ./run.sh\nmutating: yes\n"
        f"returns: text\n{'preview: yes' + chr(10) if preview else ''}"
        f"schema: {schema}\n---\nbody\n"
    )
    wrapper = tdir / "run.sh"
    wrapper.write_text(script)
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


TEXT_SCHEMA = '{"text": {"type": "string", "required": true}}'
PREVIEW_SAYS = "would delete item 42 from the shared list"
PREVIEW_SCRIPT = (
    "#!/bin/sh\n"
    f'if [ -n "$AISH_TOOL_PREVIEW" ]; then echo "{PREVIEW_SAYS}"; exit 0; fi\ncat\n'
)


@pytest.mark.usefixtures("project_scope")
class TestTheCardsReasonTravelsWithTheComment:
    """He approved with "Why do I need to approve it?"; the model was handed his
    comment and nothing else, and told him a reason nothing recorded."""

    def _run(self, tmp_path, verdict, *, preview=True):
        _write_tool(
            tmp_path, "pv", schema=TEXT_SCHEMA,
            script=PREVIEW_SCRIPT if preview else "#!/bin/sh\ncat\n", preview=preview,
        )
        agent, _ = make_agent(
            [model_says(tool_calls=[tool_call("pv", text="x")]), model_says("ok")],
            cwd=str(tmp_path), approve_tool=lambda n, a, p=None: verdict,
        )
        agent.run_task("go")
        return tool_messages(agent.messages)[0]["content"]

    def test_approve_with_comment_carries_the_reason_verbatim(self, tmp_path):
        result = self._run(tmp_path, Approved("Why do I need to approve it?"))
        assert "NOT RUN" in result
        assert f'"{PREVIEW_SAYS}"' in result
        assert "Do NOT give any other reason" in result

    def test_deny_with_comment_carries_the_reason_verbatim(self, tmp_path):
        result = self._run(tmp_path, Denied("why is this asking me?"))
        assert "DENIED" in result
        assert f'"{PREVIEW_SAYS}"' in result

    def test_a_card_with_no_recorded_reason_adds_nothing(self, tmp_path):
        result = self._run(tmp_path, Approved("why?"), preview=False)
        assert "NOT RUN" in result
        assert "approval card the user answered" not in result

    def test_egress_card_reason_reaches_the_model(self, monkeypatch):
        """The gate from the incident: whatever the egress card said is what the
        model gets back, for either verdict."""
        monkeypatch.setattr(
            agent_module.web, "read_url", lambda url, topic=None, **_kw: f"page at {url}"
        )
        for verdict in (Approved("why do I need to approve it?"), Denied("why?")):
            shown: list[str] = []

            def approve_tool(name, args, preview=None, verdict=verdict, shown=shown):
                shown.append(preview)
                return verdict

            agent, _ = make_agent(
                [
                    model_says(tool_calls=[tool_call("read_url", url="https://b.example/")]),
                    model_says("ok"),
                ],
                origin="email", approve_tool=approve_tool,
            )
            agent.run_task("go")
            assert shown and shown[0]
            assert f'"{shown[0]}"' in tool_messages(agent.messages)[0]["content"]

    def test_a_command_card_records_no_reason_so_none_is_given(self):
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call("run_command", command="touch x")]),
                model_says("ok"),
            ],
            approve=lambda _cmd: Approved("why?"),
        )
        agent.run_task("go")
        result = tool_messages(agent.messages)[0]["content"]
        assert "NOT RUN" in result
        assert "approval card the user answered" not in result


# --------------------------------------------------------------------------
# 3 · answer-me-first lets aish read its own guidance
# --------------------------------------------------------------------------

ANSWER_FIRST = """---
name: answer-first
description: Answer me before running anything.
when: always
then:
  must_first: answer
---
"""
REFUSED = "BEFORE running anything"


def read_skill_call(skill: str):
    # `tool_call(name, **arguments)` cannot carry an argument called `name`.
    return SimpleNamespace(
        function=SimpleNamespace(name="read_skill", arguments={"name": skill})
    )


class TestGuidanceReadsBeforeTheFirstWord:
    """The rule refused `read_skill(trippy_search)` before any text; the model
    skipped the skill and guessed a CLI command that does not exist."""

    def test_the_named_set_is_exactly_the_guidance_reads(self):
        assert agent_module.rules.GUIDANCE_READS == frozenset({"read_skill", "recall"})

    def test_read_skill_runs_before_any_text_and_the_next_call_is_still_held(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(agent_module.tools, "read_docs", lambda *a, **k: "docs")
        agent, _ = rules_agent(
            tmp_path,
            [
                model_says(tool_calls=[read_skill_call("trippy_search")]),
                model_says(tool_calls=[tool_call("read_docs", command="ls")]),
                model_says("done"),
            ],
            rule_texts=(ANSWER_FIRST,),
        )
        agent.run_task("plan a trip")
        skill_result, docs_result = tool_messages(agent.messages)[:2]
        assert REFUSED not in skill_result["content"]
        assert REFUSED in docs_result["content"]

    def test_a_web_read_is_still_held(self, tmp_path, monkeypatch):
        fetched: list[str] = []
        monkeypatch.setattr(
            agent_module.web, "read_url",
            lambda url, topic=None, **_kw: (fetched.append(url), "page")[1],
        )
        agent, _ = rules_agent(
            tmp_path,
            [
                model_says(tool_calls=[tool_call("read_url", url="https://example.com/")]),
                model_says("done"),
            ],
            rule_texts=(ANSWER_FIRST,),
        )
        agent.run_task("read https://example.com/")
        assert REFUSED in tool_messages(agent.messages)[0]["content"]
        assert fetched == []

    def test_the_rule_prose_says_what_the_gate_lets_through(self):
        """Prose that misdescribes the gate is a bug with no verdict to find it."""
        line = agent_module.rules._obligation_line(
            {"verb": "must_first", "capability": "answer"}
        )
        assert "read_skill" in line and "recall" in line


# --------------------------------------------------------------------------
# 4 · a cardless change is announced by aish
# --------------------------------------------------------------------------

VAULT_SCHEMA = (
    '{"action": {"type": "string", "required": true}, '
    '"title": {"type": "string"}, "folder": {"type": "string"}, '
    '"note": {"type": "string"}, "content": {"type": "string"}}'
)
CREATE = {
    "action": "create",
    "title": "Japonia 2027 — Plan wycieczki rodzinnej",
    "folder": "Japan",
    "content": "# plan",
}
SAID = (
    "[aish] Saved to your vault without asking you: "
    "Japan/Japonia 2027 — Plan wycieczki rodzinnej (created)"
)


@pytest.mark.usefixtures("project_scope")
class TestACardlessChangeIsAnnouncedByAish:
    """Turn 1 of 2026-10-01: a note was created in his vault with no card and
    no request to save anything, and the model's answer never said so."""

    def _agent(self, tmp_path, verdict, *, script="#!/bin/sh\ncat\n", rule_texts=None,
               answer="Here is the plan.", **kw):
        _write_tool(tmp_path, "obsidian_write", schema=VAULT_SCHEMA, script=script)
        responses = [
            model_says(tool_calls=[tool_call("obsidian_write", **CREATE)]),
            model_says(answer),
        ]
        common = dict(cwd=str(tmp_path), approve_tool=lambda n, a, p=None: verdict, **kw)
        if rule_texts is not None:
            return rules_agent(tmp_path, responses, rule_texts=rule_texts, **common)[0]
        return make_agent(responses, **common)[0]

    def test_the_answer_names_what_changed_and_where(self, tmp_path):
        tokens: list[str] = []
        agent = self._agent(
            tmp_path, Licensed(vault_writes.OWNER_OPTED), on_token=tokens.append
        )
        result = agent.run_task("Zaproponuj mi plan wycieczki")
        assert result.startswith("Here is the plan.")
        assert result.endswith(SAID)
        assert SAID in "".join(tokens)  # live, not only in the return value

    def test_a_card_he_answered_announces_nothing(self, tmp_path):
        agent = self._agent(tmp_path, True)
        assert "[aish]" not in agent.run_task("save it")

    def test_a_failed_run_says_it_was_tried_not_saved(self, tmp_path):
        agent = self._agent(
            tmp_path, Licensed(vault_writes.OWNER_OPTED), script="#!/bin/sh\nexit 3\n"
        )
        result = agent.run_task("go")
        assert "Saved to your vault" not in result
        assert "[aish] Tried to save to your vault without asking you" in result
        assert "Japan/Japonia 2027 — Plan wycieczki rodzinnej" in result

    def test_the_models_own_history_keeps_its_own_words(self, tmp_path):
        """The line is aish speaking to the owner; fed back as something the
        model said, the model would defend or repeat a line it never wrote."""
        agent = self._agent(tmp_path, Licensed(vault_writes.OWNER_OPTED))
        agent.run_task("go")
        last = [m for m in agent.messages if m.get("role") == "assistant"][-1]
        assert last["content"] == "Here is the plan."

    @pytest.mark.parametrize("bound", [False, True], ids=["unbound", "rule-bound"])
    def test_it_survives_a_cold_replay(self, tmp_path, bound):
        """Unbound answers are logged before they are released and bound ones
        when they are; both must carry the line, or a reload loses it."""
        log = SessionLog.new(tmp_path / "state")
        rule = """---
name: use-the-vault-tool
description: Use the vault tool.
when: always
then:
  must_first: obsidian_write
---
"""
        tokens: list[str] = []
        agent = self._agent(
            tmp_path, Licensed(vault_writes.OWNER_OPTED), on_message=log.message,
            step_log=log.step, on_token=tokens.append,
            rule_texts=(rule,) if bound else None,
        )
        agent.run_task("Zaproponuj mi plan wycieczki")
        # Which path ran: an unbound answer streams as the model writes it, a
        # bound one is held and released in one piece with the line in it.
        assert ("Here is the plan." in tokens) is not bound
        assert "".join(tokens).count("[aish] Saved to your vault") == 1
        dones = [e["result"] for e in SessionLog.reconstruct_events(log.path)
                 if e["type"] == "done"]
        assert dones and dones[-1].endswith(SAID)
        assert dones[-1].count("[aish] Saved to your vault") == 1

    def test_an_owner_only_mail_is_announced(self, tmp_path):
        _write_tool(
            tmp_path, "gmail_send",
            schema='{"to": {"type": "string", "required": true}, '
                   '"subject": {"type": "string"}, "body": {"type": "string"}}',
        )
        agent, _ = make_agent(
            [
                model_says(tool_calls=[tool_call(
                    "gmail_send", to="pawel@wenda.eu", subject="Plan", body="hi"
                )]),
                model_says("Sent."),
            ],
            cwd=str(tmp_path),
            approve_tool=lambda n, a, p=None: Licensed(recipients.OWNER_ONLY),
        )
        result = agent.run_task("mail me the plan")
        assert result.endswith("[aish] Sent without asking you: mail to pawel@wenda.eu — “Plan”")

    def test_any_other_licence_is_announced_by_name_and_arguments(self, tmp_path):
        _write_tool(tmp_path, "gmail_label", schema=TEXT_SCHEMA)
        agent, _ = make_agent(
            [model_says(tool_calls=[tool_call("gmail_label", text="Inbox")]), model_says("ok")],
            cwd=str(tmp_path),
            approve_tool=lambda n, a, p=None: Licensed("unattended: email"),
        )
        result = agent.run_task("label it")
        assert result.endswith("[aish] Ran without asking you: gmail_label(text='Inbox')")

    def test_the_line_is_one_line_whatever_the_arguments_hold(self):
        line = agent_module._cardless_line(
            "obsidian_write",
            {"action": "append", "note": "a\nb"}, vault_writes.OWNER_OPTED, ran_ok=True,
        )
        assert "\n" not in line and line.startswith("[aish] ")

    def test_set_frontmatter_names_the_keys_it_added(self):
        said = vault_writes.announce(
            {"action": "set_frontmatter", "note": "Pay/eon", "frontmatter": "paid=yes"},
            ran_ok=True,
        )
        assert said == "Saved to your vault without asking you: Pay/eon (properties added: paid)"
