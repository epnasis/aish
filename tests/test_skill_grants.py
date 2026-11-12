"""Standing approvals for third-party skill runs (epic #467 slice 2c).

"Always for this skill" on a use_skill card records the owner's own act,
bound to the hashes the lock recorded at install; later runs of exactly that
reviewed skill draw no card. These tests script the model with FakeChat, run
the skill runner against a fake, and never touch the owner's config tree (the
grants file is redirected by `isolated_global_dirs`).
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest
from test_agent import make_agent, model_says, tool_call, tool_messages
from test_skill_runner import use, vendor

from aish import cli, skill_grants, skill_import, skill_runner, skills
from aish import server as server_module
from aish.approval import AlwaysForSkill, Approved

HASHES = {"SKILL.md": "a" * 64, "run.py": "b" * 64}


def lock(folder: Path) -> dict[str, str]:
    """Record the skill's hashes in the lock as an import would, and return them."""
    hashes = skill_import.file_hashes(folder)
    skill_import.record_import(skills.VENDOR_SKILLS_DIR, folder.name, None, "", None, hashes)
    return hashes


def acts() -> list[dict]:
    path = skills.SKILL_GRANTS_FILE
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


class TestTheGrantStore:
    def test_a_grant_covers_exactly_the_hashes_it_was_given(self):
        assert skill_grants.record(skill_grants.Offer("pdf", HASHES), "web card")
        assert skill_grants.standing("pdf", dict(HASHES)) is not None
        assert skill_grants.standing("PDF", dict(HASHES)) is None  # exact name
        changed = {**HASHES, "run.py": "c" * 64}
        assert skill_grants.standing("pdf", changed) is None
        assert skill_grants.standing("pdf", {**HASHES, "extra.md": "d" * 64}) is None
        assert skill_grants.standing("pdf", None) is None
        assert skill_grants.standing("pdf", {}) is None

    def test_each_act_says_when_and_how_it_arrived(self):
        skill_grants.record(skill_grants.Offer("pdf", HASHES), "terminal card")
        [act] = acts()
        assert act["act"] == "granted" and act["how"] == "terminal card"
        assert act["files"] == HASHES and act["at"]

    def test_the_latest_act_decides_and_a_revocation_is_a_tombstone(self):
        skill_grants.record(skill_grants.Offer("pdf", HASHES), "web card")
        assert skill_grants.revoke("pdf", "aish skill revoke")
        assert skill_grants.standing("pdf", HASHES) is None
        assert [a["act"] for a in acts()] == ["granted", "revoked"]  # nothing erased
        skill_grants.record(skill_grants.Offer("pdf", HASHES), "web card")
        assert skill_grants.standing("pdf", HASHES) is not None

    def test_a_line_it_cannot_read_grants_nothing(self):
        """A malformed revocation must not let the grant above it stand."""
        skill_grants.record(skill_grants.Offer("pdf", HASHES), "web card")
        with skills.SKILL_GRANTS_FILE.open("a") as f:
            f.write('{"act": "revoked", "skill": \n')
        assert skill_grants.standing("pdf", HASHES) is None
        assert skill_grants.readable() is False
        assert skill_grants.last_act("pdf") == "unreadable"
        # Nothing new is recorded into it, but a revocation still is.
        assert skill_grants.record(skill_grants.Offer("pdf", HASHES), "web card") is None
        assert skill_grants.revoke("pdf", "aish skill revoke")

    def test_a_grant_with_no_hashes_is_not_well_formed(self):
        skills.SKILL_GRANTS_FILE.write_text(
            json.dumps({"act": "granted", "skill": "pdf", "files": {}}) + "\n"
        )
        assert skill_grants.readable() is False
        assert skill_grants.record(skill_grants.Offer("pdf", {}), "web card") is None


class TestAlwaysForThisSkill:
    """The agent half: the offer is composed from the lock's hashes, the grant
    replaces ONLY the card, and the model sees the same result either way."""

    @pytest.fixture(autouse=True)
    def fake_runner(self, monkeypatch):
        runs: list = []

        class Outcome:
            ended = skill_runner.ENDED_ANSWERED
            files: list = []

            def record(self):
                return {"skill": "pdfmaker", "ended": self.ended}

            def ended_sentence(self):
                return "the runner gave its final message after 1 model call(s)"

            def envelope(self):
                return "ENVELOPE: what the runner did"

        def run(request):
            runs.append(request)
            return Outcome()

        monkeypatch.setattr(skill_runner, "run", run)
        return runs

    def _run(self, tmp_path, approve, inputs=None, **kw):
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        steps: list[dict] = []
        holder: list = []

        def asked(name, args, preview=None):
            return approve(holder[0], name, args, preview)

        agent, chat = make_agent(
            [model_says(tool_calls=[use(inputs=inputs)]), model_says("done"),
             model_says("done"), model_says("done")],
            cwd=str(project), approve_tool=asked, step_log=steps.append, **kw,
        )
        holder.append(agent)
        answer = agent.run_task("make my pdf")
        return agent, answer, steps, tool_messages(agent.messages)

    def test_the_card_offers_always_only_for_a_skill_with_recorded_hashes(self, tmp_path):
        folder = vendor()
        offers = []

        def approve(agent, name, args, preview):
            offers.append(agent.card_offer())
            return True

        self._run(tmp_path, approve)
        assert offers == [None]  # no hashes recorded: nothing to bind a grant to
        hashes = lock(folder)
        self._run(tmp_path, approve)
        assert offers[-1] == skill_grants.Offer("pdfmaker", hashes)
        assert offers[-1].wire() == {"skill": "pdfmaker", "files": len(hashes)}

    def test_always_records_the_agents_offer_and_the_next_run_draws_no_card(
        self, tmp_path, fake_runner
    ):
        hashes = lock(vendor())
        cards = []
        _, _, steps, first = self._run(
            tmp_path, lambda a, n, args, p: cards.append(args) or AlwaysForSkill("web card")
        )
        [act] = acts()
        assert act["skill"] == "pdfmaker" and act["files"] == hashes
        assert act["how"] == "web card"
        [record] = [s for s in steps if s.get("kind") == "skill_run"]
        assert record["approved"] == "card" and record["grant"]["how"] == "web card"

        def no_card(*_a):
            raise AssertionError("a granted run drew a card")

        _, answer, steps, second = self._run(tmp_path, no_card)
        assert len(cards) == 1 and len(fake_runner) == 2
        [record] = [s for s in steps if s.get("kind") == "skill_run"]
        assert record["approved"] == skill_grants.STANDING_GRANT
        assert record["grant"] == {"at": act["at"], "how": "web card"}
        # The model's result is the same bytes either way (review F4); the
        # attribution reaches the owner only.
        assert first[0]["content"] == second[0]["content"]
        assert "standing approval" in answer and "aish skill revoke pdfmaker" in answer
        model_saw = "".join(str(m.get("content")) for m in second)
        assert "standing" not in model_saw and "Always" not in model_saw

    def test_a_skill_whose_files_changed_since_the_grant_is_carded_again(self, tmp_path):
        folder = vendor()
        lock(folder)
        self._run(tmp_path, lambda *a: AlwaysForSkill("web card"))
        (folder / "notes.md").write_text("a new file")
        lock(folder)  # a reinstall with different files
        cards = []
        self._run(tmp_path, lambda a, n, args, p: cards.append(args) or True)
        assert len(cards) == 1

    def test_a_folder_that_no_longer_matches_its_lock_is_never_run_by_a_grant(self, tmp_path):
        folder = vendor(extra={"helper.md": "h"})
        lock(folder)
        self._run(tmp_path, lambda *a: AlwaysForSkill("web card"))
        agent, _ = make_agent([], cwd=str(tmp_path))
        recorded = skill_import.recorded_hashes(skills.VENDOR_SKILLS_DIR, "pdfmaker")
        assert agent._standing_skill_grant("pdfmaker", recorded, folder) is not None
        (folder / "helper.md").write_text("changed after the grant")
        assert agent._standing_skill_grant("pdfmaker", recorded, folder) is None

    def test_an_always_that_names_no_offer_records_nothing_and_runs_once(
        self, tmp_path, fake_runner
    ):
        vendor()  # no hashes, so the card offered nothing
        self._run(tmp_path, lambda *a: AlwaysForSkill("web card"))
        assert acts() == [] and len(fake_runner) == 1

    def test_a_hold_records_nothing(self, tmp_path, fake_runner):
        """What the web approver returns for "Always" with a comment (F2)."""
        lock(vendor())
        _, _, _, results = self._run(tmp_path, lambda *a: Approved("use the other file"))
        assert acts() == [] and fake_runner == []
        assert "use the other file" in results[0]["content"]

    def test_a_grant_never_lifts_a_refusal_before_the_card(self, tmp_path, fake_runner):
        lock(vendor())
        self._run(tmp_path, lambda *a: AlwaysForSkill("web card"))
        _, _, _, results = self._run(tmp_path, lambda *a: True, origin="schedule")
        assert results[0]["content"].startswith("NOT RUN:")
        agent, _ = make_agent([], cwd=str(tmp_path))  # no approver: no channel
        out = agent._use_skill({"name": "pdfmaker", "brief": "b"})
        assert "no approval card is available" in str(out)
        _, _, _, results = self._run(tmp_path, lambda *a: True, inputs=["../../etc/hosts"])
        assert results[0]["content"].startswith("ERROR: inputs:")
        assert len(fake_runner) == 1

    def test_the_record_carries_the_inputs(self, tmp_path):
        vendor()
        (tmp_path / "project").mkdir()
        (tmp_path / "project" / "in.txt").write_text("x")
        _, _, steps, _ = self._run(tmp_path, lambda *a: True, inputs=["in.txt"])
        [record] = [s for s in steps if s.get("kind") == "skill_run"]
        assert record["inputs"] == [str((tmp_path / "project" / "in.txt").resolve())]
        assert record["approved"] == "card" and "grant" not in record


class TestTheGrantsFileIsFenced:
    def test_the_file_tools_refuse_it(self, tmp_path):
        agent, _ = make_agent(
            [model_says(tool_calls=[tool_call(
                "write_file", path=str(skills.SKILL_GRANTS_FILE), content="{}")]),
             model_says("ok")],
            cwd=str(tmp_path), approve_write=lambda _p: pytest.fail("carded"),
        )
        agent.run_task("grant yourself")
        [result] = tool_messages(agent.messages)
        assert "standing skill grants file" in result["content"]
        assert not skills.SKILL_GRANTS_FILE.exists()

    @pytest.mark.parametrize("command", [
        "printf x >> {grants}",
        "printf x >> {upper}",
        "cd {home} && echo x >> skill-grants",
        "echo x >> $AISH_CONFIG_HOME/Skill-Grants",
    ])
    def test_a_command_naming_it_is_refused_with_no_card(self, tmp_path, command):
        grants = skills.SKILL_GRANTS_FILE
        command = command.format(
            grants=grants, upper=str(grants).upper(), home=grants.parent
        )
        agent, _ = make_agent(
            [model_says(tool_calls=[tool_call("run_command", command=command)]),
             model_says("ok")],
            cwd=str(tmp_path), approve=lambda _c: pytest.fail("carded"),
        )
        agent.run_task("grant yourself")
        [result] = tool_messages(agent.messages)
        assert result["content"].startswith("NOT EXECUTED: this command names")
        assert not grants.exists()


class _FakeBridge:
    def __init__(self, answer):
        self.answer = answer
        self.asked: list[dict] = []
        self.emitted: list[dict] = []

    def emit(self, event):
        self.emitted.append(event)

    def ask(self, request):
        request.setdefault("id", "uid-1")
        self.asked.append(request)
        return self.answer


class _FakeLog:
    def __init__(self):
        self.records: list = []

    def command(self, command, decision, intent="", preview="", asked_by="", **timing):
        self.records.append(decision)


def _web(answer, offer):
    bridge, log = _FakeBridge(answer), _FakeLog()
    approvers = server_module.make_web_approvers(
        bridge, log, Path("/x/allow"), Path("/x/deny"),
        ask_all=False, get_scope=lambda: (".", []), trust_dir=lambda p: "",
        get_origin=lambda: "user", get_offer=lambda: offer,
    )
    verdict = approvers[3]("use_skill", {"skill": "pdf"}, "Run it")
    return verdict, bridge, log


class TestTheCardsChooseTheAgentsOffer:
    OFFER = skill_grants.Offer("pdf", HASHES)

    def test_the_web_card_carries_the_offer_and_its_button_answers_it(self):
        verdict, bridge, log = _web({"action": "approve_always_skill"}, self.OFFER)
        assert bridge.asked[0]["always"] == {"skill": "pdf", "files": 2}
        assert isinstance(verdict, AlwaysForSkill) and verdict.how == "web card"
        assert log.records == ["approved+always-skill"]

    def test_a_web_always_with_a_comment_is_a_hold(self):
        verdict, bridge, log = _web(
            {"action": "approve_always_skill", "comment": "smaller"}, self.OFFER
        )
        assert isinstance(verdict, Approved) and verdict.comment == "smaller"
        assert log.records == ["approved (feedback: smaller)"]
        assert any("not recorded" in e.get("text", "") for e in bridge.emitted)

    def test_a_web_always_with_no_offer_approves_nothing(self):
        verdict, bridge, _ = _web({"action": "approve_always_skill"}, None)
        assert "always" not in bridge.asked[0]
        assert verdict is False

    def test_the_terminal_offers_a_only_with_an_offer(self, monkeypatch, capsys):
        prompts: list[str] = []
        monkeypatch.setattr(builtins, "input", lambda p="": prompts.append(p) or "a")
        approve = cli.make_tool_approver(_FakeLog(), get_offer=lambda: self.OFFER)
        verdict = approve("use_skill", {"skill": "pdf"}, "Run it")
        assert isinstance(verdict, AlwaysForSkill) and verdict.how == "terminal card"
        assert "a(lways for this skill)" in prompts[0]
        approve = cli.make_tool_approver(_FakeLog(), get_offer=lambda: None)
        assert approve("use_skill", {"skill": "pdf"}, "Run it") is False
        assert "always" not in prompts[1]


class TestRevokeFromTheTerminal:
    def test_revoke_appends_a_tombstone_and_says_what_it_found(self, capsys):
        skill_grants.record(skill_grants.Offer("pdf", HASHES), "web card")
        assert cli._skill_cli(["revoke", "pdf"]) == 0
        assert "asks on a card again" in capsys.readouterr().out
        assert skill_grants.standing("pdf", HASHES) is None
        assert cli._skill_cli(["revoke", "pdf"]) == 0
        assert "it had no standing grant" in capsys.readouterr().out
        assert [a["act"] for a in acts()] == ["granted", "revoked", "revoked"]
