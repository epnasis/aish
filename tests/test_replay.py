"""The replay harness (#441): checks over a recorded run, the runner's isolation,
the card policy, and the report's wording — all offline.

The check fixtures are GENERATED from invented data by
`tests/fixtures/replay/make_fixture.py` — the real server and agent driven by a
scripted model, no session of the owner's anywhere — and smaller hand-built
logs pin each verdict state. No model, no network, no real command runs here.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import types
from pathlib import Path

import pytest

from aish import backends, evidence, replay, replay_checks, replay_driver
from aish.replay_checks import RunRecord, Verdict
from tests.fixtures.replay.make_fixture import generate

SCENARIO = Path(__file__).resolve().parent.parent / "evals" / "example-trippy"


def _trippy_checks():
    return replay.load_scenario(SCENARIO).checks


def _verdicts(path: Path) -> dict[str, Verdict]:
    run = replay_checks.load_run(path)
    return {v.check: v for v in replay_checks.apply(run, _trippy_checks())}


def write_log(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = [{"ts": "2026-10-01T10:00:00", **r} for r in records]
    path.write_text("".join(json.dumps(r) + "\n" for r in stamped), encoding="utf-8")
    return path


def call(n: int, name: str, args: dict, turn: int = 1) -> dict:
    return {"kind": "trace", "step": {"kind": "call", "call": n, "turn": turn,
                                      "name": name, "args": args}}


def result(n: int, name: str, status: str = "ok", turn: int = 1, **extra) -> dict:
    return {"kind": "trace", "step": {"kind": "tool", "call": n, "turn": turn, "name": name,
                                      "status": status, **extra}}


def output(text: str, tool: str = "run_command") -> dict:
    return {"kind": "message", "role": "tool", "tool_name": tool, "content": text}


def answer(text: str) -> dict:
    return {"kind": "message", "role": "assistant", "content": text}


TASK = {"kind": "task_start", "prompt": "q"}
END = {"kind": "task_end", "status": "ok"}


_GENERATED: dict[str, Path] = {}


@pytest.fixture
def generated(tmp_path_factory, monkeypatch) -> dict[str, Path]:
    """The two invented runs, generated once per session INSIDE a test, so the
    suite's isolation fixtures are active while the real server writes them."""
    for variant in ("good", "bad"):
        if variant not in _GENERATED:
            with monkeypatch.context() as patch:
                _GENERATED[variant] = generate(
                    variant, tmp_path_factory.mktemp(f"generated-{variant}"), patch)
    return dict(_GENERATED)


class TestChecksOnGeneratedRuns:
    """`make_fixture.py` scripts a passing and a failing case for every check,
    through the real server, agent and card policy, on invented data."""

    @pytest.mark.parametrize("check", sorted(_trippy_checks()))
    def test_the_good_run_passes_every_check(self, generated, check):
        verdict = _verdicts(generated["good"])[check]
        assert verdict.passed is True, verdict.evidence

    @pytest.mark.parametrize("check, evidence", [
        ("trippy_party", "--adults 1 --children 9,5"),
        ("single_unit_only", "3 of 3 searches without single_unit_only"),
        ("details_max_three_ids", "(5 ids)"),
        ("no_failed_trippy", "first (exit 2): trippy search --site booking --location Porto "
                             "--check-in-date"),
        ("no_pipes_or_redirects", "| head -5"),
        ("no_unapproved_shell", "first (denied): trippy search --site booking --location Porto"
                                " | head -5"),
        ("links_from_evidence", "invented-never-opened.html"),
        ("images_from_evidence", "4040404.jpg"),
        ("repeated_calls", "read_url x3"),
    ])
    def test_the_bad_run_fails_every_check_with_its_evidence(self, generated, check, evidence):
        verdict = _verdicts(generated["bad"])[check]
        assert verdict.passed is False and evidence in verdict.evidence, verdict.evidence

    def test_counts_are_raw(self, generated):
        counts = replay_checks.counts(replay_checks.load_run(generated["bad"]))
        assert counts["tasks"] == 4 and counts["answered"] == 4
        assert counts["commands"] == 4 and counts["cards"] == 4 and counts["cards_denied"] == 1


class TestChecksThreeStates:
    def test_an_image_source_nobody_printed_fails(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, output("nothing here"), call(1, "show_image", {"source": "https://x/a.jpg"}),
            result(1, "show_image"), answer("see"), END])
        verdict = replay_checks.images_from_evidence(replay_checks.load_run(log))
        assert verdict.passed is False and "https://x/a.jpg" in verdict.evidence

    def test_an_image_source_missing_from_cut_output_could_not_tell(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, output("head\n[... 5,000 characters omitted ...]\ntail"),
            call(1, "show_image", {"source": "https://x/a.jpg"}),
            result(1, "show_image"), answer("see"), END])
        assert replay_checks.images_from_evidence(replay_checks.load_run(log)).passed is None

    def test_a_source_printed_only_after_the_call_does_not_count(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "show_image", {"source": "https://x/a.jpg"}),
            result(1, "show_image"), output("https://x/a.jpg"), answer("see"), END])
        assert replay_checks.images_from_evidence(replay_checks.load_run(log)).passed is False

    def test_a_pasted_picture_show_image_never_ran_fails(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, answer("![room](https://x/b.jpg)"), END])
        verdict = replay_checks.images_from_evidence(replay_checks.load_run(log))
        assert verdict.passed is False and "pasted" in verdict.evidence

    def test_a_link_never_opened_fails_and_an_opened_one_passes(self, tmp_path):
        records = [TASK, call(1, "read_url", {"url": "https://a.example/p"}),
                   result(1, "read_url"),
                   answer("[A](https://a.example/p) and [B](https://b.example/q)"), END]
        verdict = replay_checks.links_from_evidence(
            replay_checks.load_run(write_log(tmp_path / "s.jsonl", records)))
        assert verdict.passed is False and "https://b.example/q" in verdict.evidence
        assert "1 of 2" in verdict.evidence

    def test_a_link_on_the_owners_network_is_not_checked_and_says_so(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, answer("mi serves at `http://10.99.0.2:8080`"), END])
        verdict = replay_checks.links_from_evidence(replay_checks.load_run(log))
        assert verdict.passed is True and "1 of them on the owner's network" in verdict.evidence

    def test_a_failed_fetch_does_not_count_as_opened(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "read_url", {"url": "https://a.example/p"}),
            result(1, "read_url", status="failed"), answer("[A](https://a.example/p)"), END])
        assert replay_checks.links_from_evidence(replay_checks.load_run(log)).passed is False

    def test_nothing_ran_could_not_tell(self, tmp_path):
        run = replay_checks.load_run(write_log(tmp_path / "s.jsonl", [TASK, answer("hi"), END]))
        assert replay_checks.no_failed_commands(run).passed is None
        assert replay_checks.no_unapproved_shell(run).passed is None

    def test_an_undated_answer_with_links_could_not_tell(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "read_url", {"url": "https://a.example/p"}), result(1, "read_url"),
            {**answer("[A](https://a.example/p)"), "ts": "not a time"}, END])
        assert replay_checks.links_from_evidence(replay_checks.load_run(log)).passed is None

    def test_no_answer_could_not_tell(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [TASK])
        assert replay_checks.links_from_evidence(replay_checks.load_run(log)).passed is None

    def test_a_superseded_attempt_is_not_read(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, {**call(1, "run_command", {"command": "trippy search --adults 4"}),
                   "superseded": True},
            call(2, "run_command", {"command": "trippy search --adults 2 --children 9,5"}),
            result(2, "run_command"), answer("ok"), END])
        assert _trippy_checks()["trippy_party"](replay_checks.load_run(log)).passed is True

    def test_unpairable_command_framing_could_not_tell(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, {"kind": "cmd_start", "command": "a"}, {"kind": "cmd_start", "command": "b"},
            {"kind": "cmd_end", "status": "exit", "exit_code": 0}, END])
        assert replay_checks.no_failed_commands(replay_checks.load_run(log)).passed is None

    def test_a_check_that_raises_could_not_tell(self, tmp_path):
        def broken(run: RunRecord) -> Verdict:
            raise KeyError("x")
        log = write_log(tmp_path / "s.jsonl", [TASK, END])
        [verdict] = replay_checks.apply(replay_checks.load_run(log), {"broken": broken})
        assert verdict.passed is None and "KeyError" in verdict.evidence

    @pytest.mark.parametrize("command, piped", [
        ("trippy search --x 1", False),
        ('trippy search --location "a|b"', False),
        ("trippy search | jq .", True),
        ("trippy search > out.txt", True),
        ("trippy search 2>&1", True),
        ("trippy search && trippy details", False),
    ])
    def test_pipes_and_redirects(self, command, piped):
        assert replay_checks.has_pipe_or_redirect(command) is piped


class TestTrippyScenarioChecks:
    @pytest.mark.parametrize("party", [
        "--adults 2 --children 9,5",
        "--adults 2 --child 9 --child 5",
        "--adults=2 --child 5,9",
    ])
    def test_every_documented_party_spelling_passes(self, tmp_path, party):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": f"trippy search --site booking {party}"}),
            result(1, "run_command"), END])
        assert _trippy_checks()["trippy_party"](replay_checks.load_run(log)).passed is True

    def test_the_single_unit_flag_and_filter_both_count(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": "trippy search --single-unit"}),
            call(2, "run_command", {"command": "trippy search --filter a=1,single_unit_only=true"}),
            call(3, "run_command", {"command": "trippy search --filter single_unit_only=false"}),
            END])
        verdict = _trippy_checks()["single_unit_only"](replay_checks.load_run(log))
        assert verdict.passed is False and verdict.evidence.startswith("1 of 3")

    def test_repeated_property_id_options_are_summed(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command",
                       {"command": "trippy details --property-id 1,2 --property-id 3,4"}), END])
        assert _trippy_checks()["details_max_three_ids"](
            replay_checks.load_run(log)).passed is False

    def test_no_search_could_not_tell(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [TASK, END])
        assert _trippy_checks()["trippy_party"](replay_checks.load_run(log)).passed is None


class TestCardPolicy:
    """Deny is the default for every card a replay can raise."""

    APPROVE = (["trippy "], ["show_image", "read_url"])

    @pytest.mark.parametrize("event, approved", [
        ({"kind": "command", "command": "trippy search --site booking"}, True),
        ({"kind": "command", "command": "trippy search; rm -rf ~"}, False),
        ({"kind": "command", "command": "trippy search | sh"}, False),
        ({"kind": "command", "command": "trippy search $(whoami)"}, False),
        ({"kind": "command", "command": "trippy search > ~/.zshrc"}, False),
        ({"kind": "command", "command": "trippyx search"}, False),
        ({"kind": "command", "command": "curl https://example.com"}, False),
        ({"kind": "tool", "tool": "show_image"}, True),
        ({"kind": "tool", "tool": "gmail_send"}, False),
        ({"kind": "write", "target": "/tmp/x"}, False),
        ({"kind": "read", "path": "/etc/passwd"}, False),
        ({"kind": "import", "skill": "x"}, False),
        ({"kind": "something-new"}, False),
    ])
    def test_policy(self, event, approved):
        decision, why = replay_driver.card_decision(event, *self.APPROVE)
        assert decision is approved and why


class TestDriverOverTheRealServer:
    """The driver against the real `create_app` and WebSocket, with a scripted
    model and the command implementation stubbed — so a card the policy
    approves is shown to reach the run path, and one it denies is not."""

    def test_cards_are_answered_from_the_policy(self, tmp_path, monkeypatch):
        import aish.agent as agent_module
        from tests.test_server import connected, make_client, model_says, tool_call

        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path / "state"))
        ran: list[str] = []
        monkeypatch.setattr(agent_module.tools, "run_command",
                            lambda cmd, **_kw: ran.append(cmd) or "ok")
        allow = tmp_path / "allow.txt"
        allow.write_text("", encoding="utf-8")
        env = {"state_dir": tmp_path / "state", "allow_path": allow,
               "deny_path": tmp_path / "deny.txt", "config_path": tmp_path / "config.toml",
               "lessons_path": tmp_path / "lessons.md", "cwd": str(tmp_path)}
        client, _ = make_client(env, [
            model_says(tool_calls=[tool_call("run_command", command="trippy search --a 1")]),
            model_says("searched"),
            model_says(tool_calls=[tool_call("run_command", command="trippy x; touch pwned")]),
            model_says("stopped"),
        ])
        events: list[dict] = []
        with client, connected(client) as (ws, hello, _):
            # `connected` consumed hello+replay; hand the driver a socket that
            # replays them so it reads the same opening a fresh one would.
            opening = iter([hello, {"type": "replay"}])

            class Socket:
                def receive_json(self):
                    return next(opening, None) or ws.receive_json()

                def send_json(self, data):
                    ws.send_json(data)

            replay_driver.drive(Socket(), ["one", "two"], ["trippy "], [], events.append,
                                limit=400)
        cards = [e for e in events if e["type"] == "card"]
        assert [(c["what"], c["approved"]) for c in cards] == [
            ("trippy search --a 1", True), ("trippy x; touch pwned", False)]
        assert ran == ["trippy search --a 1"]
        assert [e["result"] for e in events if e["type"] == "done"] == ["searched", "stopped"]
        log = tmp_path / "state" / hello["session"]
        verdict = replay_checks.no_unapproved_shell(replay_checks.load_run(log))
        assert verdict.passed is False and "touch pwned" in verdict.evidence

    def _spec(self, tmp_path):
        spec = {"events_path": str(tmp_path / "events.jsonl"), "config_path": str(tmp_path / "c"),
                "model": "fake", "state_dir": str(tmp_path), "allow_path": str(tmp_path / "a"),
                "deny_path": str(tmp_path / "d"), "lessons_path": str(tmp_path / "l"),
                "cwd": str(tmp_path), "turns": [], "approve_commands": [],
                "approve_tools": []}
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def test_the_driver_switches_the_copied_automations_scheduler_off(self, tmp_path,
                                                                     monkeypatch):
        """#460: a run's config is a copy of the owner's tree, automations
        included; its scheduler must be off before the app starts it."""
        import starlette.testclient

        import aish.server
        server = types.SimpleNamespace(scheduler="live")
        monkeypatch.setattr(aish.server, "create_app", lambda *a, **k: types.SimpleNamespace(
            state=types.SimpleNamespace(server=server)))
        seen = []

        class Started(Exception):
            pass

        class Client:
            def __init__(self, app):
                seen.append(app.state.server.scheduler)

            def __enter__(self):
                raise Started

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(starlette.testclient, "TestClient", Client)
        with pytest.raises(Started):
            replay_driver.main([str(self._spec(tmp_path))])
        assert seen == [None]
        events = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
        assert '"scheduler_off"' in events

    def test_a_code_root_it_cannot_switch_off_does_not_run(self, tmp_path, monkeypatch):
        import starlette.testclient

        import aish.server
        monkeypatch.setattr(aish.server, "create_app", lambda *a, **k: types.SimpleNamespace(
            state=types.SimpleNamespace()))
        monkeypatch.setattr(starlette.testclient, "TestClient",
                            lambda app: pytest.fail("the app was started"))
        with pytest.raises(SystemExit):
            replay_driver.main([str(self._spec(tmp_path))])

    def test_the_driver_imports_no_harness_module_at_load(self):
        """It runs against a baseline tree that predates the harness."""
        tree = ast.parse(Path(replay_driver.__file__).read_text(encoding="utf-8"))
        top = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
        names = [getattr(n, "module", None) or n.names[0].name for n in top]
        assert not [n for n in names if n and n.startswith("aish")]


def _opening_socket(ws, hello):
    """`connected` consumed hello+replay; a socket that replays them, so the
    driver reads the same opening a fresh connection would."""
    opening = iter([hello, {"type": "replay"}])

    class Socket:
        def receive_json(self):
            return next(opening, None) or ws.receive_json()

        def send_json(self, data):
            ws.send_json(data)

    return Socket()


def _server_env(tmp_path, monkeypatch) -> dict:
    monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path / "state"))
    allow = tmp_path / "allow.txt"
    allow.write_text("", encoding="utf-8")
    return {"state_dir": tmp_path / "state", "allow_path": allow,
            "deny_path": tmp_path / "deny.txt", "config_path": tmp_path / "config.toml",
            "lessons_path": tmp_path / "lessons.md", "cwd": str(tmp_path)}


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _held_steps(path: Path) -> list[dict]:
    return [r["step"] for r in _records(path) if r.get("kind") == "trace"
            and r["step"].get("kind") == "tool" and r["step"].get("decision") == "held"]


class TestCommentRules:
    """`[[cards.comment]]`: a card answered with the owner's comment, verbatim
    from the scenario — the only way a replay can reproduce an approval he
    steered (docs/replay.md, Cards answered with a comment)."""

    TMP = {"command_re": "/tmp/", "action": "approve", "comment": "use your scratch dir"}

    def test_the_first_matching_rule_with_uses_left_answers(self):
        rules = [self.TMP, {"command_re": "&&", "action": "deny", "comment": "stop"}]
        used = [0, 0]
        card = {"kind": "command", "command": "mkdir /tmp/x && convert a b"}
        assert replay_driver.comment_rule_for(card, rules, used) == 0
        used[0] = 1  # rule 1 is spent: the same card now reaches rule 2
        assert replay_driver.comment_rule_for(card, rules, used) == 1
        used[1] = 1
        assert replay_driver.comment_rule_for(card, rules, used) is None

    def test_times_lets_a_rule_answer_more_than_once(self):
        rule = {**self.TMP, "times": 2}
        card = {"kind": "command", "command": "ls /tmp/"}
        assert replay_driver.comment_rule_for(card, [rule], [1]) == 0
        assert replay_driver.comment_rule_for(card, [rule], [2]) is None

    def test_a_tool_rule_matches_the_tool_name_exactly(self):
        rule = {"tool": "gmail_send", "action": "deny", "comment": "not now"}
        assert replay_driver.comment_rule_for(
            {"kind": "tool", "tool": "gmail_send"}, [rule], [0]) == 0
        assert replay_driver.comment_rule_for(
            {"kind": "tool", "tool": "gmail_send_all"}, [rule], [0]) is None
        assert replay_driver.comment_rule_for(
            {"kind": "command", "command": "gmail_send"}, [rule], [0]) is None

    @pytest.mark.parametrize("kind", ["read", "import", "write", "something-new"])
    def test_only_command_and_tool_cards_are_ever_answered(self, kind):
        """A read card drops the comment and an approve RUNS the read; an
        import approved with a comment installs the skill (agent.py checks only
        `Denied` and truthiness there). Neither may be reachable."""
        rules = [{"command_re": ".", "action": "approve", "comment": "c"},
                 {"tool": "x", "action": "approve", "comment": "c"}]
        card = {"kind": kind, "command": "anything", "tool": "x", "path": "/etc/passwd"}
        assert replay_driver.comment_rule_for(card, rules, [0, 0]) is None

    @pytest.mark.parametrize("comment", ["", "   ", "　 ", 0, False, None])
    def test_a_comment_the_server_would_not_see_never_answers(self, comment):
        """The server reads `str(comment or "").strip()`: any of these would
        turn a commented approve into a plain approve that runs."""
        rule = {"command_re": ".", "action": "approve", "comment": comment}
        assert replay_driver.comment_rule_for(
            {"kind": "command", "command": "rm x"}, [rule], [0]) is None

    def _load(self, tmp_path, rules: str):
        return replay.load_scenario(_scenario(tmp_path, extra="\n" + rules))

    def test_the_loader_reads_rules_in_order_with_times_defaulting_to_one(self, tmp_path):
        scenario = self._load(tmp_path, (
            '[[cards.comment]]\ncommand_re = "/tmp/"\naction = "approve"\n'
            'comment = "you have your own private dir for temp"\n'
            '[[cards.comment]]\ntool = "gmail_send"\naction = "deny"\ncomment = "no"\n'
            'times = 2\n'))
        assert scenario.comment_rules == [
            {"command_re": "/tmp/", "action": "approve",
             "comment": "you have your own private dir for temp", "times": 1},
            {"tool": "gmail_send", "action": "deny", "comment": "no", "times": 2}]

    @pytest.mark.parametrize("rule, says", [
        ('command_re = "x"\naction = "approve"\ncomment = 0', "non-empty string"),
        ('command_re = "x"\naction = "approve"\ncomment = false', "non-empty string"),
        ('command_re = "x"\naction = "approve"\ncomment = "　 "', "non-empty string"),
        ('command_re = "x"\naction = "approve"', "non-empty string"),
        ('command_re = "x"\naction = "edit"\ncomment = "c"', "approve"),
        ('command_re = "x"\naction = "approve_session"\ncomment = "c"', "approve"),
        ('command_re = "x"\ncomment = "c"', "approve"),
        ('command_re = "x"\ntool = "t"\naction = "deny"\ncomment = "c"', "exactly one"),
        ('action = "deny"\ncomment = "c"', "exactly one"),
        ('command_re = "("\naction = "deny"\ncomment = "c"', "regular expression"),
        ('command_re = ""\naction = "deny"\ncomment = "c"', "non-empty string"),
        ('command_re = "x"\naction = "deny"\ncomment = "c"\ntimes = 0', "positive integer"),
        ('command_re = "x"\naction = "deny"\ncomment = "c"\ntimes = true', "positive integer"),
        ('command_re = "x"\naction = "deny"\ncomment = "c"\nkind = "read"', "unknown"),
    ])
    def test_the_loader_refuses_what_the_driver_could_misread(self, tmp_path, rule, says):
        with pytest.raises(replay.ReplayError, match=says):
            self._load(tmp_path, f"[[cards.comment]]\n{rule}\n")

    def test_the_rules_reach_the_runs_spec(self, tmp_path):
        rules = '[[cards.comment]]\ncommand_re = "/tmp/"\naction = "approve"\ncomment = "c"\n'
        launch = FakeLaunch({"baseline": _failing_log(tmp_path),
                             "candidate": _failing_log(tmp_path)})
        _batch(tmp_path, launch, runs=1, extra="\n" + rules)
        assert {c["arm"]: c["spec"]["comment_rules"] for c in launch.calls} == {
            "baseline": [{"command_re": "/tmp/", "action": "approve", "comment": "c",
                          "times": 1}],
            "candidate": [{"command_re": "/tmp/", "action": "approve", "comment": "c",
                           "times": 1}]}


class TestCommentRulesOverTheRealServer:
    """The driver's comment answers against the real `create_app`: an approval
    WITH a comment is held and never reaches the run path; the rule, spent,
    hands the re-proposal to the deny-by-default policy."""

    def test_approve_with_a_comment_holds_then_the_policy_approves(self, tmp_path,
                                                                   monkeypatch):
        import aish.agent as agent_module
        from tests.test_server import connected, make_client, model_says, tool_call

        env = _server_env(tmp_path, monkeypatch)
        ran: list[str] = []
        monkeypatch.setattr(agent_module.tools, "run_command",
                            lambda cmd, **_kw: ran.append(cmd) or "ok")
        client, _ = make_client(env, [
            model_says(tool_calls=[tool_call("run_command", command="trippy go; touch x")]),
            model_says(tool_calls=[tool_call("run_command", command="trippy go")]),
            model_says("done"),
        ])
        rules = [{"command_re": "^trippy", "action": "approve", "comment": "split it",
                  "times": 1}]
        events: list[dict] = []
        with client, connected(client) as (ws, hello, _):
            replay_driver.drive(_opening_socket(ws, hello), ["go"], ["trippy "], [],
                                events.append, limit=400, comment_rules=rules)
        cards = [e for e in events if e["type"] == "card"]
        assert [(c["what"], c["action"], c.get("comment"), c.get("rule"), c["approved"])
                for c in cards] == [
            ("trippy go; touch x", "approve", "split it", 1, False),
            ("trippy go", "approve", None, None, True)]
        assert ran == ["trippy go"]  # the commented card never ran, shell operator and all
        log = tmp_path / "state" / hello["session"]
        decisions = [r["decision"] for r in _records(log) if r.get("kind") == "command"]
        assert decisions[0] == "approved (feedback: split it)"
        (held,) = _held_steps(log)
        assert held["comment"] == "split it"
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
        assert verdict.passed is True and "1 re-proposed" in verdict.evidence

    def test_deny_with_a_comment_arms_the_stop(self, tmp_path, monkeypatch):
        import aish.agent as agent_module
        from tests.test_server import connected, make_client, model_says, tool_call

        env = _server_env(tmp_path, monkeypatch)
        ran: list[str] = []
        monkeypatch.setattr(agent_module.tools, "run_command",
                            lambda cmd, **_kw: ran.append(cmd) or "ok")
        client, _ = make_client(env, [
            model_says(tool_calls=[tool_call("run_command", command="curl x")]),
            model_says("Understood, stopping."),
        ])
        rules = [{"command_re": "curl", "action": "deny", "comment": "not that site",
                  "times": 1}]
        events: list[dict] = []
        with client, connected(client) as (ws, hello, _):
            replay_driver.drive(_opening_socket(ws, hello), ["go"], [], [], events.append,
                                limit=400, comment_rules=rules)
        assert ran == []
        log = tmp_path / "state" / hello["session"]
        decisions = [r["decision"] for r in _records(log) if r.get("kind") == "command"]
        assert decisions == ["denied (feedback: not that site)"]

    def test_a_tool_card_approved_with_a_comment_never_runs(self, tmp_path, monkeypatch,
                                                            project_scope):
        from tests.test_server import (
            TestToolApproval,
            connected,
            make_client,
            model_says,
            tool_call,
        )

        env = _server_env(tmp_path, monkeypatch)
        marker = tmp_path / "toolran"
        TestToolApproval._write_tool(None, env["cwd"], "writer", marker)
        client, _ = make_client(env, [
            model_says(tool_calls=[tool_call("writer", text="hi")]),
            model_says("Not going to, then."),
            model_says("Not going to, then."),
        ])
        rules = [{"tool": "writer", "action": "approve", "comment": "shorter", "times": 1}]
        events: list[dict] = []
        with client, connected(client) as (ws, hello, _):
            replay_driver.drive(_opening_socket(ws, hello), ["go"], [], [], events.append,
                                limit=400, comment_rules=rules)
        assert not marker.exists()
        log = tmp_path / "state" / hello["session"]
        (held,) = _held_steps(log)
        assert held["name"] == "writer" and held["comment"] == "shorter"


class TestHeldCommentAnswered:
    """The hold check's behaviour, read off a log (d0e938e): a task must not
    end with an action approved WITH a comment held and nothing said."""

    HELD = {"decision": "held", "comment": "split it"}

    def test_the_line_it_looks_for_is_the_line_aish_writes(self):
        from aish import agent

        assert agent.HOLD_UNANSWERED_NOTE.startswith(replay_checks.HOLD_LINE)

    def test_a_hold_left_open_with_nothing_said_fails(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": "a && b"}),
            result(1, "run_command", "failed", **self.HELD),
            answer("Now step by step:"), END])
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
        assert verdict.passed is False
        assert "1 left open" in verdict.evidence and "Now step by step:" in verdict.evidence

    def test_a_re_proposal_answers_it(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": "a && b"}),
            result(1, "run_command", "failed", **self.HELD),
            call(2, "run_command", {"command": "a"}), result(2, "run_command"),
            answer("done"), END])
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
        assert verdict.passed is True and "1 re-proposed" in verdict.evidence

    ADVISED = {"kind": "trace", "step": {"kind": "gate", "gate": "hold.verify", "at": "verify",
                                         "verdict": "advised", "turn": 1,
                                         "evidence": {"held_call": 1, "tool": "run_command"}}}

    def test_aishs_line_answers_it(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": "a && b"}),
            result(1, "run_command", "failed", **self.HELD), self.ADVISED,
            answer("I will not.\n\n" + replay_checks.HOLD_LINE + ", and no later call"), END])
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
        assert verdict.passed is True and "1 closed by aish's line" in verdict.evidence

    def test_the_line_without_its_record_is_an_echo(self, tmp_path):
        """A model can repeat an earlier task's line; only aish's own record
        for THIS held call says aish wrote it."""
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": "a && b"}),
            result(1, "run_command", "failed", **self.HELD),
            answer("Done. " + replay_checks.HOLD_LINE), END])
        assert replay_checks.held_comment_answered(
            replay_checks.load_run(log)).passed is False

    def test_an_earlier_call_or_another_tool_does_not_answer_it(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": "ls"}), result(1, "run_command"),
            call(2, "run_command", {"command": "a && b"}),
            result(2, "run_command", "failed", **self.HELD),
            call(3, "read_file", {"path": "x"}), result(3, "read_file"),
            answer("Now:"), END])
        assert replay_checks.held_comment_answered(
            replay_checks.load_run(log)).passed is False

    def test_a_call_in_the_next_task_does_not_answer_it(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": "a && b"}),
            result(1, "run_command", "failed", **self.HELD), answer("Now:"), END,
            {"kind": "task_start", "prompt": "why stopped?"},
            call(1, "run_command", {"command": "a"}, turn=2), result(1, "run_command", turn=2),
            answer("done"), END])
        assert replay_checks.held_comment_answered(
            replay_checks.load_run(log)).passed is False

    def test_a_rules_hold_without_a_comment_is_not_his(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": "a"}),
            result(1, "run_command", "failed", decision="held"), answer("ok"), END])
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
        assert verdict.passed is None and "never given" in verdict.evidence

    def test_a_task_the_loop_detector_ended_could_not_tell(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": "a && b"}),
            result(1, "run_command", "failed", **self.HELD),
            {"kind": "message", "role": "user", "content": "[aish: stop]",
             "source": "loop_stop"},
            answer("Stopped."), END])
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
        assert verdict.passed is None and "loop_stop" in verdict.evidence

    def test_no_delivered_answer_could_not_tell(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, call(1, "run_command", {"command": "a && b"}),
            result(1, "run_command", "failed", **self.HELD)])
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
        assert verdict.passed is None and "no delivered answer" in verdict.evidence

    def test_the_agent_says_the_hold_and_the_check_reads_it(self, tmp_path, monkeypatch):
        """End to end on the code under test: the model answers text-only
        twice; aish asks once, then ships the answer with its line."""
        import aish.agent as agent_module
        from tests.test_server import connected, make_client, model_says, tool_call

        env = _server_env(tmp_path, monkeypatch)
        monkeypatch.setattr(agent_module.tools, "run_command", lambda cmd, **_kw: "ok")
        client, _ = make_client(env, [
            model_says(tool_calls=[tool_call("run_command", command="a && b")]),
            model_says("Now step by step:"),
            model_says("Now step by step:"),
        ])
        rules = [{"command_re": "&&", "action": "approve", "comment": "split it", "times": 1}]
        events: list[dict] = []
        with client, connected(client) as (ws, hello, _):
            replay_driver.drive(_opening_socket(ws, hello), ["go"], [], [], events.append,
                                limit=400, comment_rules=rules)
        log = tmp_path / "state" / hello["session"]
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
        assert verdict.passed is True and "1 closed by aish's line" in verdict.evidence


class TestCommentRulesDeliveryReview:
    """Findings of the delivery review of 5176b92, each pinned."""

    def test_a_twin_from_the_same_response_is_not_a_re_proposal(self, tmp_path, monkeypatch):
        """Two identical calls in one model response: the first is held by the
        rule, the twin runs through the policy. aish's join closes the hold on
        the twin and says nothing, but no call came after the model could read
        the comment — so the check FAILS and names the twin."""
        import aish.agent as agent_module
        from tests.test_server import connected, make_client, model_says, tool_call

        env = _server_env(tmp_path, monkeypatch)
        ran: list[str] = []
        monkeypatch.setattr(agent_module.tools, "run_command",
                            lambda cmd, **_kw: ran.append(cmd) or "ok")
        client, _ = make_client(env, [
            model_says(tool_calls=[tool_call("run_command", command="trippy go /tmp/x"),
                                   tool_call("run_command", command="trippy go /tmp/x")]),
            model_says("done"),
        ])
        rules = [{"command_re": "/tmp/", "action": "approve", "comment": "use scratch",
                  "times": 1}]
        events: list[dict] = []
        with client, connected(client) as (ws, hello, _):
            replay_driver.drive(_opening_socket(ws, hello), ["go"], ["trippy "], [],
                                events.append, limit=400, comment_rules=rules)
        assert ran == ["trippy go /tmp/x"]  # the twin, through the policy
        log = tmp_path / "state" / hello["session"]
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
        assert verdict.passed is False, verdict.evidence
        assert "same model response" in verdict.evidence

    def test_a_call_from_a_later_model_call_still_answers(self, tmp_path):
        log = write_log(tmp_path / "s.jsonl", [
            TASK, _mc(call(1, "run_command", {"command": "a && b"}), 1),
            result(1, "run_command", "failed", decision="held", comment="split"),
            _mc(call(2, "run_command", {"command": "a"}), 1), result(2, "run_command"),
            _mc(call(3, "run_command", {"command": "b"}), 2), result(3, "run_command"),
            answer("done"), END])
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
        assert verdict.passed is True and "1 re-proposed" in verdict.evidence

    def test_a_task_the_owner_stopped_could_not_tell(self, tmp_path):
        from aish.session import STOPPED_ANSWERS

        for stopped in STOPPED_ANSWERS:
            log = write_log(tmp_path / "s.jsonl", [
                TASK, call(1, "run_command", {"command": "a && b"}),
                result(1, "run_command", "failed", decision="held", comment="split"),
                answer(stopped), END])
            verdict = replay_checks.held_comment_answered(replay_checks.load_run(log))
            assert verdict.passed is None and "stopped by the owner" in verdict.evidence

    def test_the_agents_stop_text_is_one_the_check_knows(self):
        from aish import agent
        from aish.session import STOPPED_ANSWERS

        assert agent.CANCELLED_RESULT in STOPPED_ANSWERS

    def test_no_hold_says_whether_any_comment_was_given(self, tmp_path):
        never = write_log(tmp_path / "a.jsonl", [
            TASK, {"kind": "command", "command": "x", "decision": "denied"},
            answer("ok"), END])
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(never))
        assert verdict.passed is None and "never given" in verdict.evidence
        denied = write_log(tmp_path / "b.jsonl", [
            TASK, {"kind": "command", "command": "x", "decision": "denied (feedback: no)"},
            answer("ok"), END])
        verdict = replay_checks.held_comment_answered(replay_checks.load_run(denied))
        assert verdict.passed is None and "1 cards answered with a comment, none recorded held" \
            in verdict.evidence

    @pytest.mark.parametrize("tool, says", [
        ("run_command", "use command_re"), ("write_file", "write card"),
        ("schedule_task", "write card"), ("read_file", "read card"),
        ("import_skill", "import card"),
    ])
    def test_a_tool_rule_that_could_never_match_is_refused(self, tmp_path, tool, says):
        with pytest.raises(replay.ReplayError, match=says):
            replay.load_scenario(_scenario(tmp_path, extra=(
                f'\n[[cards.comment]]\ntool = "{tool}"\naction = "deny"\ncomment = "c"\n')))

    def test_the_comment_is_recorded_as_the_server_records_it(self, tmp_path, monkeypatch):
        import aish.agent as agent_module
        from tests.test_server import connected, make_client, model_says, tool_call

        env = _server_env(tmp_path, monkeypatch)
        monkeypatch.setattr(agent_module.tools, "run_command", lambda cmd, **_kw: "ok")
        client, _ = make_client(env, [
            model_says(tool_calls=[tool_call("run_command", command="curl x")]),
            model_says("Stopping."),
        ])
        rules = [{"command_re": "curl", "action": "deny", "comment": "  not that  ",
                  "times": 1}]
        events: list[dict] = []
        with client, connected(client) as (ws, hello, _):
            replay_driver.drive(_opening_socket(ws, hello), ["go"], [], [], events.append,
                                limit=400, comment_rules=rules)
        (card,) = [e for e in events if e["type"] == "card"]
        log = tmp_path / "state" / hello["session"]
        decisions = [r["decision"] for r in _records(log) if r.get("kind") == "command"]
        assert card["comment"] == "not that"
        assert decisions == [f"denied (feedback: {card['comment']})"]

    def test_the_driver_never_sends_an_action_but_approve_or_deny(self):
        """The loader refuses them; the driver refuses them again, in case a
        spec was written by hand."""
        card = {"kind": "command", "command": "rm x"}
        for action in ("approve_always", "approve_session", "edit", None):
            rule = {"command_re": ".", "action": action, "comment": "c"}
            assert replay_driver.comment_rule_for(card, [rule], [0]) is None

    def test_the_fence_is_the_kinds_table(self, monkeypatch):
        """Removing a kind from COMMENTABLE_KINDS stops it being answered."""
        rule = {"command_re": ".", "action": "approve", "comment": "c"}
        card = {"kind": "command", "command": "x"}
        assert replay_driver.comment_rule_for(card, [rule], [0]) == 0
        monkeypatch.delitem(replay_driver.COMMENTABLE_KINDS, "command")
        assert replay_driver.comment_rule_for(card, [rule], [0]) is None

    def test_a_bang_turn_is_refused(self, tmp_path):
        directory = _scenario(tmp_path)
        text = (directory / "scenario.toml").read_text(encoding="utf-8")
        (directory / "scenario.toml").write_text(text.replace(
            'text = "Show prices in EUR please"', 'text = " !touch pwned"'), encoding="utf-8")
        with pytest.raises(replay.ReplayError, match="no approval card"):
            replay.load_scenario(directory)

    def test_the_loader_keeps_the_comment_as_sent(self, tmp_path):
        scenario = replay.load_scenario(_scenario(tmp_path, extra=(
            '\n[[cards.comment]]\ncommand_re = "x"\naction = "deny"\ncomment = " c "\n')))
        assert scenario.comment_rules[0]["comment"] == "c"

    def test_the_report_counts_each_rule_and_names_one_that_never_fired(self, tmp_path):
        rules = ('\n[[cards.comment]]\ncommand_re = "/tmp/"\naction = "approve"\ncomment = "a"\n'
                 '[[cards.comment]]\ncommand_re = "&&"\naction = "approve"\ncomment = "b"\n')
        launch = FakeLaunch({"baseline": _failing_log(tmp_path),
                             "candidate": _failing_log(tmp_path)},
                            card_rules=(1,))
        batch = _batch(tmp_path, launch, runs=1, extra=rules)
        text = replay.render_report(*replay.read_batch(batch), batch)
        assert "cards answered by comment rule 1: baseline 1 · candidate 1" in text
        assert "cards answered by comment rule 2: baseline 0 · candidate 0" in text
        assert "comment rule 2 never fired in any run shown" in text


class TestCommentRulesNeedTheHold:
    """Before #81 (7a6cc12) an approval with a comment RAN the action. Comment
    rules are refused against code without that commit — by git ancestry."""

    @staticmethod
    def _repo(tmp_path) -> tuple[Path, str, str]:
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                  text=True, check=True).stdout.strip()

        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
            "-m", "old")
        old = git("rev-parse", "HEAD")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
            "-m", "hold")
        return repo, old, git("rev-parse", "HEAD")

    def test_code_with_the_hold_passes(self, tmp_path):
        repo, _old, hold = self._repo(tmp_path)
        replay.require_comment_holds(hold, "baseline", repo=repo, since=hold)

    def test_code_before_the_hold_is_refused(self, tmp_path):
        repo, old, hold = self._repo(tmp_path)
        with pytest.raises(replay.ReplayError, match="still RAN the action"):
            replay.require_comment_holds(old, "baseline", repo=repo, since=hold)

    def test_a_history_that_cannot_say_is_refused(self, tmp_path):
        repo, old, _hold = self._repo(tmp_path)
        with pytest.raises(replay.ReplayError, match="cannot tell"):
            replay.require_comment_holds(old, "baseline", repo=repo, since="f" * 40)

    def test_run_checks_both_arms_only_when_the_scenario_has_rules(self, tmp_path,
                                                                    monkeypatch):
        corpus = _corpus(tmp_path)
        monkeypatch.setattr(replay, "config_home", lambda: corpus)
        monkeypatch.setattr(replay, "state_home", lambda: tmp_path / "no-state")
        monkeypatch.setenv("AISH_REPLAY_HOME", str(tmp_path / "replays"))
        checked: list[str] = []

        def refuse(sha, arm, **_kw):
            checked.append(arm)
            raise replay.ReplayError(f"{arm} predates the hold")

        monkeypatch.setattr(replay, "require_comment_holds", refuse)
        launch = FakeLaunch({"baseline": _failing_log(tmp_path),
                             "candidate": _failing_log(tmp_path)})
        real = replay.run_batch
        monkeypatch.setattr(replay, "run_batch", lambda *a, **kw: real(*a, **kw, launch=launch))
        # An overlay, so the arms differ whether or not this tree is committed.
        overlay = tmp_path / "new.md"
        overlay.write_text("candidate", encoding="utf-8")
        differ = ["--runs", "1", "--baseline-ref", "HEAD",
                  "--candidate-overlay", f"skills/s.md={overlay}"]
        plain = _scenario(tmp_path / "plain")
        assert replay.main(["run", str(plain), *differ]) == 0
        assert checked == []
        ruled = _scenario(tmp_path / "ruled", extra=(
            '\n[[cards.comment]]\ncommand_re = "x"\naction = "deny"\ncomment = "c"\n'))
        assert replay.main(["run", str(ruled), *differ]) == 2
        assert checked == ["baseline"]
        assert not list((tmp_path / "replays").rglob(".*-baseline-code"))  # cleaned up


def _mc(record: dict, model_call: int) -> dict:
    record["step"]["model_call"] = model_call
    return record


# --- the runner ----------------------------------------------------------------

def _corpus(root: Path) -> Path:
    corpus = root / "owner-config"
    for sub in ("memory", "rules", "skills", "tools/gmail_send", "tools/google_maps", ".git",
                "evals/mined"):
        (corpus / sub).mkdir(parents=True)
    (corpus / "memory" / "notes.md").write_text("notes", encoding="utf-8")
    (corpus / "skills" / "s.md").write_text("baseline skill", encoding="utf-8")
    (corpus / "tools" / "gmail_send" / "TOOL.md").write_text("send", encoding="utf-8")
    (corpus / "tools" / "google_maps" / "TOOL.md").write_text("maps", encoding="utf-8")
    (corpus / "allow.txt").write_text("gh issue create\n", encoding="utf-8")
    (corpus / "config.toml").write_text('model = "x"\n', encoding="utf-8")
    return corpus


def _scenario(root: Path, *, target: str | None = "trippy_party", extra: str = "",
              kind: str | None = None) -> Path:
    directory = root / "scenario"
    shutil.copytree(SCENARIO, directory)
    text = (directory / "scenario.toml").read_text(encoding="utf-8")
    text = text.replace('target = ["trippy_party"]', f'target = ["{target}"]' if target else "")
    if kind is not None:  # a top-level key, so before the first table
        text = text.replace('name = "example-trippy"\n',
                            f'name = "example-trippy"\nkind = "{kind}"\n')
    (directory / "scenario.toml").write_text(text + extra, encoding="utf-8")
    return directory


class FakeLaunch:
    """Stands in for the subprocess: records what it was given and writes the
    run's artifacts the way the driver would."""

    def __init__(self, logs: dict[str, Path], mutate_corpus: bool = False,
                 exit_code: int = 0, menus: tuple[str, ...] = (),
                 card_rules: tuple[int, ...] = ()):
        self.logs, self.mutate, self.exit_code = logs, mutate_corpus, exit_code
        self.card_rules = card_rules  # the comment rule each extra card was answered by
        self.menus = menus  # bytes the run's evidence store holds, as the agent puts them
        self.calls: list[dict] = []

    def __call__(self, argv, env, cwd, timeout_s, log):
        spec = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        run_dir = Path(argv[-1]).parent
        arm = run_dir.parent.name
        self.calls.append({"argv": argv, "env": env, "cwd": cwd, "run_dir": run_dir,
                           "spec": spec, "arm": arm,
                           "config": sorted(str(p.relative_to(run_dir / "config"))
                                            for p in (run_dir / "config").rglob("*")),
                           "state": sorted(p.name for p in (run_dir / "state").iterdir())})
        if self.mutate:
            (run_dir / "config" / "memory" / "written-by-run.md").write_text("x")
        code_root = Path(env["PYTHONPATH"])
        events = [{"type": "start", "aish_file": str(code_root / "aish" / "__init__.py")},
                  {"type": "card", "kind": "command"},
                  *({"type": "card", "kind": "command", "rule": n} for n in self.card_rules),
                  {"type": "end"}]
        Path(spec["events_path"]).write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        shutil.copyfile(self.logs[arm], Path(spec["state_dir"]) / "session-1.jsonl")
        for menu in self.menus:
            evidence.put(menu, spec["state_dir"])
        log.write_text("", encoding="utf-8")
        return replay.LaunchResult(self.exit_code, False)


def _batch(tmp_path, launch, *, target="trippy_party", runs=2, overlays=None,
           environ=None, extra="", kind=None):
    scenario = replay.load_scenario(_scenario(tmp_path, target=target, extra=extra, kind=kind))
    code = tmp_path / "code"
    (code / "aish").mkdir(parents=True, exist_ok=True)
    arms = [replay.Arm("baseline", code, "main abc", {}),
            replay.Arm("candidate", code, "this tree", overlays or {})]
    embeddings = tmp_path / "owner-state" / "embeddings.json"
    embeddings.parent.mkdir(exist_ok=True)
    embeddings.write_text("{}", encoding="utf-8")
    (embeddings.parent / "egress-vouches.json").write_text("{}", encoding="utf-8")
    batch_dir = tmp_path / "out" / "b"
    replay.run_batch(scenario, runs=runs, arms=arms, corpus_source=_corpus(tmp_path),
                     embeddings_source=embeddings, batch_dir=batch_dir, launch=launch,
                     environ=environ or {"PATH": "/bin", "HOME": "/h"}, log=lambda _m: None)
    return batch_dir


def _failing_log(tmp_path) -> Path:
    """A run that fails the target (`trippy_party`) and nothing else it can tell."""
    return write_log(tmp_path / "bad.jsonl", [
        TASK, call(1, "run_command",
                   {"command": "trippy search --adults 1 --children 9,5 --single-unit"}),
        result(1, "run_command"), answer("fine"), END])


def _both_failing(tmp_path) -> dict[str, Path]:
    return {"baseline": _failing_log(tmp_path), "candidate": _failing_log(tmp_path)}


def _passing_log(tmp_path) -> Path:
    return write_log(tmp_path / "good.jsonl", [
        TASK, call(1, "run_command",
                   {"command": "trippy search --adults 2 --children 9,5 --single-unit"}),
        result(1, "run_command"), answer("fine"), END])


class TestRunnerIsolation:
    def test_runs_are_interleaved_one_process_each(self, tmp_path):
        launch = FakeLaunch(_both_failing(tmp_path))
        _batch(tmp_path, launch, runs=2)
        assert [(c["arm"], c["run_dir"].name) for c in launch.calls] == [
            ("baseline", "1"), ("candidate", "1"), ("baseline", "2"), ("candidate", "2")]
        assert all(c["argv"][1] == "-P" and c["argv"][2].endswith("replay_driver.py")
                   for c in launch.calls)

    def test_tools_git_and_his_allowlist_are_not_copied(self, tmp_path):
        launch = FakeLaunch(_both_failing(tmp_path))
        _batch(tmp_path, launch, runs=1)
        for c in launch.calls:
            assert "memory/notes.md" in c["config"]
            excluded = ("tools", ".git", "allow.txt", "evals")
            assert not [p for p in c["config"] if p.startswith(excluded)]
            assert Path(c["spec"]["allow_path"]).read_text() == ""

    def test_an_allowlisted_plugin_and_nothing_else_is_copied(self, tmp_path):
        launch = FakeLaunch(_both_failing(tmp_path))
        _batch(tmp_path, launch, runs=1, extra="")
        scenario_dir = tmp_path / "scenario"
        text = (scenario_dir / "scenario.toml").read_text().replace(
            "tools = []", 'tools = ["google_maps"]')
        (scenario_dir / "scenario.toml").write_text(text)
        launch2 = FakeLaunch(_both_failing(tmp_path))
        scenario = replay.load_scenario(scenario_dir)
        code = tmp_path / "code"
        replay.run_batch(scenario, runs=1, arms=[replay.Arm("baseline", code, "x", {})],
                         corpus_source=tmp_path / "owner-config", embeddings_source=None,
                         batch_dir=tmp_path / "out" / "c", launch=launch2, environ={},
                         log=lambda _m: None)
        tools = [p for p in launch2.calls[0]["config"] if p.startswith("tools/")]
        assert tools == ["tools/google_maps", "tools/google_maps/TOOL.md"]

    def test_the_state_dir_gets_embeddings_and_never_the_vouches(self, tmp_path):
        launch = FakeLaunch(_both_failing(tmp_path))
        _batch(tmp_path, launch, runs=1)
        assert all(c["state"] == ["embeddings.json"] for c in launch.calls)

    def test_every_run_gets_a_fresh_corpus(self, tmp_path):
        launch = FakeLaunch(_both_failing(tmp_path), mutate_corpus=True)
        _batch(tmp_path, launch, runs=2)
        assert not any("memory/written-by-run.md" in c["config"] for c in launch.calls)
        assert not (tmp_path / "owner-config" / "memory" / "written-by-run.md").exists()

    def test_the_environment_is_the_runs_own(self, tmp_path):
        launch = FakeLaunch(_both_failing(tmp_path))
        _batch(tmp_path, launch, runs=1, environ={
            "PATH": "/bin", "AISH_LOCAL_URL": "http://mi:8080", "AISH_STATE_DIR": "/owner",
            "AISH_CONFIG_HOME": "/owner-config", "AISH_WEB_TOKEN": "t", "PYTHONPATH": "/x"})
        for c in launch.calls:
            env, run_dir = c["env"], c["run_dir"]
            assert env["AISH_STATE_DIR"] == str(run_dir / "state")
            assert env["AISH_CONFIG_HOME"] == str(run_dir / "config")
            assert env["AISH_NOTIFY"] == "0"
            assert env["AISH_LOCAL_URL"] == "http://mi:8080"
            assert "AISH_WEB_TOKEN" not in env
            assert env["PYTHONPATH"] == str(tmp_path / "code")
            assert env["TRIPPY_DATA_DIR"] == f"{run_dir}/trippy"

    def test_an_overlay_reaches_the_candidate_only(self, tmp_path):
        new = tmp_path / "candidate-skill.md"
        new.write_text("candidate skill", encoding="utf-8")
        launch = FakeLaunch(_both_failing(tmp_path))
        batch = _batch(tmp_path, launch, runs=1, overlays={"skills/s.md": new})
        assert (batch / "baseline/1/config/skills/s.md").read_text() == "baseline skill"
        assert (batch / "candidate/1/config/skills/s.md").read_text() == "candidate skill"
        manifest = json.loads((batch / "candidate/1/manifest.json").read_text())
        assert manifest["overlays"][0]["path"] == "skills/s.md"

    @pytest.mark.parametrize("rel", ["../escape.md", "/abs.md", "tools/gmail_send/TOOL.md"])
    def test_an_overlay_may_not_leave_the_corpus_or_add_a_tool(self, tmp_path, rel):
        new = tmp_path / "f.md"
        new.write_text("x", encoding="utf-8")
        with pytest.raises(replay.ReplayError):
            _batch(tmp_path, FakeLaunch(_both_failing(tmp_path)),
                   runs=1, overlays={rel: new})

    def test_a_scenario_may_not_set_aish_env(self, tmp_path):
        directory = _scenario(tmp_path)
        text = (directory / "scenario.toml").read_text().replace(
            "[env]", '[env]\nAISH_STATE_DIR = "/x"')
        (directory / "scenario.toml").write_text(text)
        with pytest.raises(replay.ReplayError, match="AISH_"):
            replay.load_scenario(directory)

    def test_unknown_keys_and_checks_are_refused(self, tmp_path):
        directory = _scenario(tmp_path, extra='\nrunz = 3\n')
        with pytest.raises(replay.ReplayError, match="checks.runz"):
            replay.load_scenario(directory)
        text = (directory / "scenario.toml").read_text().replace("runz = 3", "")
        (directory / "scenario.toml").write_text(text.replace('"repeated_calls",',
                                                              '"no_such_check",'))
        with pytest.raises(replay.ReplayError, match="no_such_check"):
            replay.load_scenario(directory)

    def test_the_code_under_test_is_what_the_subprocess_imports(self, tmp_path):
        """PYTHONPATH must beat the editable install of THIS tree, and `-P` must
        keep the script's own directory (aish/, which holds a `secrets.py`) off
        sys.path. Run for real: it is only the interpreter, no aish code."""
        code = tmp_path / "baseline-code"
        (code / "aish").mkdir(parents=True)
        (code / "aish" / "__init__.py").write_text("", encoding="utf-8")
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "secrets.py").write_text("SHADOW = True\n", encoding="utf-8")
        probe = scripts / "probe.py"
        probe.write_text("import aish, secrets\nprint(aish.__file__)\n"
                         "print(hasattr(secrets, 'SHADOW'))\n", encoding="utf-8")
        log = tmp_path / "out.log"
        env = replay.run_env({"PATH": "/usr/bin:/bin"}, tmp_path / "run", code, {})
        outcome = replay.launch_subprocess([replay.sys.executable, "-P", str(probe)], env,
                                           tmp_path, 60, log)
        printed = log.read_text(encoding="utf-8").split()
        assert outcome.exit_code == 0, printed
        assert printed == [str(code / "aish" / "__init__.py"), "False"]

    def test_the_baseline_is_an_archive_of_the_ref(self, tmp_path):
        sha = replay.baseline_code("HEAD", tmp_path / "code")
        assert len(sha) == 40
        assert (tmp_path / "code" / "aish" / "server.py").is_file()
        with pytest.raises(replay.ReplayError, match="does not resolve"):
            replay.baseline_code("no-such-ref-441", tmp_path / "other")

    def test_the_output_root_is_beside_the_state_tree(self, monkeypatch):
        monkeypatch.delenv("AISH_REPLAY_HOME", raising=False)
        assert replay.replay_home() == Path.home() / ".local" / "state" / "aish-replay"
        monkeypatch.setenv("AISH_REPLAY_HOME", "/tmp/x")
        assert replay.replay_home() == Path("/tmp/x")


class TestScenarioResolution:
    """Mined scenarios live in the private config tree; the repo holds examples."""

    def test_a_bare_name_resolves_to_the_config_tree_first(self, tmp_path, monkeypatch):
        private = tmp_path / "config" / "evals" / "example-trippy"
        shutil.copytree(SCENARIO, private)
        monkeypatch.setattr(replay, "config_home", lambda: tmp_path / "config")
        assert replay.resolve_scenario("example-trippy") == private

    def test_then_the_repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replay, "config_home", lambda: tmp_path / "config")
        assert replay.resolve_scenario("example-trippy") == replay.REPO_ROOT / "evals" / \
            "example-trippy"

    def test_a_path_is_a_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replay, "config_home", lambda: tmp_path / "config")
        assert replay.resolve_scenario(str(SCENARIO)) == SCENARIO
        with pytest.raises(replay.ReplayError, match="no scenario"):
            replay.resolve_scenario("no-such-scenario")

    def test_the_repo_holds_only_synthetic_scenarios(self):
        """Every repo scenario says so in its source table — a mined one names
        a real session file, and that belongs in the config tree."""
        for toml in sorted((replay.REPO_ROOT / "evals").glob("*/scenario.toml")):
            directory = toml.parent
            scenario = replay.load_scenario(directory)
            assert "synthetic" in str(scenario.source.get("session", "")), directory


class TestTheRunCommand:
    def test_run_end_to_end_with_the_launch_stubbed(self, tmp_path, monkeypatch, capsys):
        corpus = _corpus(tmp_path)
        monkeypatch.setattr(replay, "config_home", lambda: corpus)
        monkeypatch.setattr(replay, "state_home", lambda: tmp_path / "no-state")
        monkeypatch.setenv("AISH_REPLAY_HOME", str(tmp_path / "replays"))
        launch = FakeLaunch({"baseline": _failing_log(tmp_path),
                             "candidate": _passing_log(tmp_path)})
        real = replay.run_batch
        monkeypatch.setattr(replay, "run_batch", lambda *a, **kw: real(*a, **kw, launch=launch))
        overlay = tmp_path / "new.md"
        overlay.write_text("candidate", encoding="utf-8")
        code = replay.main(["run", str(SCENARIO), "--runs", "1", "--baseline-ref", "HEAD",
                            "--candidate-overlay", f"skills/s.md={overlay}"])
        assert code == 0
        roots = {c["arm"]: Path(c["env"]["PYTHONPATH"]) for c in launch.calls}
        assert roots["candidate"] == replay.REPO_ROOT
        assert not roots["baseline"].exists()  # the extracted archive is cleaned up
        out = capsys.readouterr().out
        assert "baseline failed 1 of 1 (reproduced)" in out
        assert "candidate not observed in 1 run" in out

    def test_identical_arms_are_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AISH_REPLAY_HOME", str(tmp_path / "replays"))
        monkeypatch.setattr(replay, "candidate_code", lambda repo=None: "abc")
        monkeypatch.setattr(replay, "baseline_code", lambda ref, dest: dest.mkdir(
            parents=True) or "abc")
        assert replay.main(["run", str(SCENARIO), "--baseline-ref", "HEAD"]) == 2


    def test_a_tool_build_reaches_the_candidate_only(self, tmp_path, monkeypatch, capsys):
        corpus = _corpus(tmp_path)
        monkeypatch.setattr(replay, "config_home", lambda: corpus)
        monkeypatch.setattr(replay, "state_home", lambda: tmp_path / "no-state")
        monkeypatch.setenv("AISH_REPLAY_HOME", str(tmp_path / "replays"))
        monkeypatch.setattr(replay, "candidate_code", lambda repo=None: "abc")
        monkeypatch.setattr(replay, "baseline_code", lambda ref, dest: dest.mkdir(
            parents=True) or "abc")
        launch = FakeLaunch({"baseline": _failing_log(tmp_path),
                             "candidate": _passing_log(tmp_path)})
        real = replay.run_batch
        monkeypatch.setattr(replay, "run_batch", lambda *a, **kw: real(*a, **kw, launch=launch))
        build = tmp_path / "trippy-build-bin"
        build.mkdir()
        # Same code and corpus on both arms: only the tool differs, and that is enough.
        code = replay.main(["run", str(SCENARIO), "--runs", "1", "--baseline-ref", "HEAD",
                            "--candidate-path", str(build),
                            "--candidate-env", "TRIPPY_FLAVOUR={run}/x"])
        assert code == 0
        env = {c["arm"]: c["env"] for c in launch.calls}
        assert env["candidate"]["PATH"].split(os.pathsep)[0] == str(build.resolve())
        assert str(build.resolve()) not in env["baseline"].get("PATH", "")
        assert env["candidate"]["TRIPPY_FLAVOUR"].endswith("/candidate/1/x")
        assert "TRIPPY_FLAVOUR" not in env["baseline"]
        assert f"PATH first {build.resolve()}" in capsys.readouterr().out

    @pytest.mark.parametrize("pair", ["AISH_STATE_DIR=/x", "PATH=/x", "PYTHONPATH=/x",
                                      "NOEQUALS"])
    def test_an_arm_may_not_set_what_the_runner_owns(self, tmp_path, monkeypatch, pair):
        monkeypatch.setenv("AISH_REPLAY_HOME", str(tmp_path / "replays"))
        assert replay.main(["run", str(SCENARIO), "--candidate-env", pair]) == 2

    def test_an_arm_path_must_exist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AISH_REPLAY_HOME", str(tmp_path / "replays"))
        assert replay.main(["run", str(SCENARIO), "--candidate-path",
                            str(tmp_path / "missing")]) == 2


class TestReport:
    def _report(self, tmp_path, launch, **kw) -> str:
        batch = _batch(tmp_path, launch, **kw)
        return replay.render_report(*replay.read_batch(batch), batch)

    def test_raw_counts_and_no_claims_about_the_change(self, tmp_path):
        text = self._report(tmp_path, FakeLaunch({"baseline": _failing_log(tmp_path),
                                                  "candidate": _passing_log(tmp_path)}))
        assert len(text) <= replay.REPORT_CAP
        assert "baseline failed 2 of 2 (reproduced)" in text
        assert "candidate not observed in 2 runs" in text
        lowered = text.lower()
        for word in ("%", "fixed", "improved", "better", "resolved", "rate"):
            assert word not in lowered
        assert replay.STILL_SHARED in text

    def test_candidate_numbers_withheld_when_baseline_did_not_reproduce(self, tmp_path):
        good = _passing_log(tmp_path)
        bad = _failing_log(tmp_path)
        text = self._report(tmp_path, FakeLaunch({"baseline": good, "candidate": bad}))
        assert "candidate numbers withheld: the baseline did not reproduce the target " \
               "failure (trippy_party: not observed in 2 runs)" in text
        assert "candidate failed" not in text and "candidate not observed" not in text

    def test_candidate_numbers_withheld_without_a_target(self, tmp_path):
        text = self._report(tmp_path, FakeLaunch(_both_failing(tmp_path)),
                            target=None)
        assert "candidate numbers withheld: scenario.toml declares no target" in text

    def test_an_incomplete_run_vouches_for_nothing(self, tmp_path):
        good = _passing_log(tmp_path)
        bad = _failing_log(tmp_path)
        text = self._report(tmp_path, FakeLaunch({"baseline": bad, "candidate": good},
                                                 exit_code=1))
        assert "incomplete: b1: driver exited 1" in text
        assert "candidate could not tell in 2 of 2" in text

    def test_a_batch_reads_back_after_the_baseline_code_is_deleted(self, tmp_path):
        """`cmd_run` deletes the extracted baseline once the batch ends; the
        import evidence must still read as the arm's own code afterwards."""
        batch = _batch(tmp_path, FakeLaunch(_both_failing(tmp_path)))
        shutil.rmtree(tmp_path / "code")
        _, _, results = replay.read_batch(batch)
        assert [r.problem for r in results] == [None] * 4

    def test_aish_imported_from_elsewhere_marks_the_run_incomplete(self, tmp_path):
        batch = _batch(tmp_path, FakeLaunch(_both_failing(tmp_path)))
        events = batch / "baseline" / "1" / "events.jsonl"
        events.write_text(events.read_text().replace(str(tmp_path / "code"), "/elsewhere"))
        _, _, results = replay.read_batch(batch)
        assert results[0].problem.startswith("imported aish from /elsewhere")

    def test_an_approved_command_without_an_env_row_is_named(self, tmp_path):
        scenario = replay.load_scenario(SCENARIO)
        assert replay.unisolated_commands(scenario) == []
        scenario.env = {}
        assert replay.unisolated_commands(scenario) == ["trippy"]
        text = self._report(tmp_path, FakeLaunch(_both_failing(tmp_path)),
                            extra="")
        assert "no [env] row isolates" not in text
        directory = tmp_path / "scenario"
        toml = (directory / "scenario.toml").read_text().replace(
            'TRIPPY_DATA_DIR = "{run}/trippy"', "")
        (directory / "scenario.toml").write_text(toml)
        _, batch, results = replay.read_batch(tmp_path / "out" / "b")
        text = replay.render_report(replay.load_scenario(directory), batch, results,
                                    tmp_path / "out" / "b")
        assert "no [env] row isolates approved command(s) trippy" in text

    def test_full_lists_every_verdict(self, tmp_path):
        batch = _batch(tmp_path, FakeLaunch(_both_failing(tmp_path)))
        text = replay.render_report(*replay.read_batch(batch), batch, full=True)
        assert "b1 images_from_evidence (pass)" in text


GOLDEN = Path(__file__).resolve().parent / "fixtures" / "replay" / "golden"


class TestGuardScenarios:
    """`kind = "guard"` (#444): R4 mirrored. A guard's baseline must PASS what it
    guards, or the candidate is withheld; with a green baseline the candidate is
    always shown and its failures are the headline. The fix kind is unchanged."""

    def _text_and_json(self, tmp_path, capsys, logs, **kw) -> tuple[str, dict]:
        batch = _batch(tmp_path, FakeLaunch(logs, **kw.pop("launch", {})), kind="guard", **kw)
        text = replay.render_report(*replay.read_batch(batch), batch)
        assert replay.main(["report", str(batch), "--json"]) == 0
        return text, json.loads(capsys.readouterr().out)

    def test_a_green_baseline_and_a_regressing_candidate_lead_with_the_regression(
            self, tmp_path, capsys):
        text, payload = self._text_and_json(tmp_path, capsys, {
            "baseline": _passing_log(tmp_path), "candidate": _failing_log(tmp_path)})
        headline = "guard: regression — trippy_party failed in 2 of 2 candidate runs"
        assert headline in text and "withheld" not in text
        assert ("trippy_party          * baseline passed 2 of 2 · candidate failed 2 of 2 "
                "(regression)") in text
        assert "(* = guarded check)" in text
        assert "not observed" not in text.split("\n\n")[1].splitlines()[0]
        assert text.split("evidence (first failures):\n")[1].startswith("trippy_party [c1,c2]")
        for word in ("%", "fixed", "improved", "better", "resolved", "rate"):
            assert word not in text.lower()
        # --json says what the text says, and withholds nothing the text shows.
        assert payload["kind"] == "guard" and payload["withheld"] is None
        assert payload["headline"] == headline
        assert list(payload["arms"]) == ["baseline", "candidate"]
        assert payload["arms"]["candidate"]["checks"]["trippy_party"] == {
            "passed": 0, "failed": 2, "could_not_tell": 0}

    def test_a_green_candidate_is_shown_as_no_regression_observed(self, tmp_path, capsys):
        text, payload = self._text_and_json(tmp_path, capsys, {
            "baseline": _passing_log(tmp_path), "candidate": _passing_log(tmp_path)})
        line = "guard: no regression observed — trippy_party passed 2 of 2"
        assert line in text and payload["headline"] == line
        assert "candidate passed 2 of 2" in text

    def test_a_red_baseline_withholds_the_candidate_and_says_the_guard_is_broken(
            self, tmp_path, capsys):
        text, payload = self._text_and_json(tmp_path, capsys, {
            "baseline": _failing_log(tmp_path), "candidate": _passing_log(tmp_path)})
        reason = ("guard not green on baseline — trippy_party failed in 2 of 2 baseline runs: "
                  "the guard itself is broken, and a regression verdict against a broken "
                  "guard is noise")
        assert f"candidate numbers withheld: {reason}" in text
        assert "baseline failed 2 of 2 (guard not green)" in text
        for shown in ("candidate passed", "candidate failed", "candidate 2 runs", "c1", "c2"):
            assert shown not in text
        assert "guard: " not in text.replace("withheld: guard not green", "")
        assert payload["withheld"] == reason
        assert list(payload["arms"]) == ["baseline"] and "headline" not in payload

    def test_a_baseline_never_seen_green_withholds_too(self, tmp_path, capsys):
        """An incomplete run's pass is not a pass; a guard nobody saw green is
        no more judgeable than one seen red."""
        text, payload = self._text_and_json(tmp_path, capsys, {
            "baseline": _passing_log(tmp_path), "candidate": _failing_log(tmp_path)},
            launch={"exit_code": 1})
        assert ("candidate numbers withheld: guard not green on baseline — trippy_party passed "
                "in none of 2 baseline runs (could not tell in 2)") in text
        assert payload["withheld"].startswith("guard not green on baseline")
        assert list(payload["arms"]) == ["baseline"]

    def test_a_guard_without_a_target_is_withheld(self, tmp_path, capsys):
        text, _ = self._text_and_json(tmp_path, capsys, _both_failing(tmp_path), target=None)
        assert "candidate numbers withheld: scenario.toml declares no guarded check" in text

    def test_context_rows_are_the_same_for_a_guard(self, tmp_path):
        logs = {"baseline": _arm_log(tmp_path, "base", BIG_MENU, [70_000, 50_000, 20_000]),
                "candidate": _arm_log(tmp_path, "cand", SMALL_MENU, [40_000, 30_000, 10_000])}
        guard = _batch(tmp_path / "g", FakeLaunch(logs, menus=(BIG_MENU, SMALL_MENU)),
                       kind="guard", target="single_unit_only")
        fix = _batch(tmp_path / "f", FakeLaunch(logs, menus=(BIG_MENU, SMALL_MENU)))

        def context(batch: Path) -> str:
            text = replay.render_report(*replay.read_batch(batch), batch, full=True)
            return text.split("context per model call")[1].split("\nevidence:")[0]
        assert context(guard) == context(fix)
        assert "candidate 2 runs, 6 calls · tokens median 30.0k" in context(guard)

    def test_an_unknown_kind_is_refused(self, tmp_path):
        with pytest.raises(replay.ReplayError, match="unknown scenario kind 'regression': "
                                                     'kind is "fix" \\(the default\\) or "guard"'):
            replay.load_scenario(_scenario(tmp_path, kind="regression"))

    def test_check_is_the_same_for_a_guard(self, tmp_path, capsys):
        log = _failing_log(tmp_path)
        assert replay.main(["check", str(log), "--scenario", str(SCENARIO)]) == 0
        fix = capsys.readouterr().out
        guard = _scenario(tmp_path, kind="guard")
        assert replay.main(["check", str(log), "--scenario", str(guard)]) == 0
        assert capsys.readouterr().out == fix

    @pytest.mark.parametrize("name, baseline, candidate", [
        ("fix_reproduced", _failing_log, _passing_log),
        ("fix_withheld", _passing_log, _failing_log)])
    def test_the_fix_kind_report_is_unchanged(self, tmp_path, name, baseline, candidate):
        """Pinned from the report as rendered BEFORE guards existed: the default
        kind's text is byte-identical; its JSON gained only the `kind` key —
        and, with the call-timing slice (#444), an additive `timing` in each
        context summary (goldens regenerated; equal to the old ones with that
        key stripped)."""
        batch = _batch(tmp_path, FakeLaunch({"baseline": baseline(tmp_path),
                                             "candidate": candidate(tmp_path)}))
        loaded = replay.read_batch(batch)
        golden = (GOLDEN / f"{name}.txt").read_text(encoding="utf-8")
        assert replay.render_report(*loaded, batch).replace(str(batch), "<batch>") == golden
        if name == "fix_reproduced":
            full = (GOLDEN / f"{name}.full.txt").read_text(encoding="utf-8")
            assert replay.render_report(*loaded, batch, full=True) == full
        payload = replay.json_report(*loaded, batch)
        assert payload.pop("kind") == "fix"
        assert json.dumps(payload, indent=2).replace(str(batch), "<batch>") == \
            (GOLDEN / f"{name}.json").read_text(encoding="utf-8")


# --- context per model call (#444) ------------------------------------------------

BIG_MENU, SMALL_MENU = "m" * 3000, "s" * 1000
WRONG_PARTY = call(1, "run_command",
                   {"command": "trippy search --adults 1 --children 9,5 --single-unit"})


def brief(system: list[int] | None, menu: str = "", count: int = 26) -> dict:
    """A `brief` as `Agent._record_brief` writes it; `system=None` is the #239
    stratum that recorded no system half, `menu=""` a brief with no digest."""
    step: dict = {"kind": "brief", "model_call": 1,
                  "tools": {"digest": evidence.digest_of(menu) if menu else "",
                            "count": count, "names": []}}
    if system is not None:
        step["system"] = [{"at": i, "chars": c, "digest": f"d{i}"} for i, c in enumerate(system)]
    return {"kind": "trace", "step": step}


def reasoning(n: int, prompt: int | None = None,
              semantics: str | None = backends.INPUT_INCLUDES_CACHE) -> dict:
    """`prompt=None` is a provider that reported nothing: the writer's zeros."""
    step: dict = {"kind": "reasoning", "model_call": n, "tokens": [prompt or 0, 40]}
    if prompt and semantics:
        step["usage"] = backends.usage_detail(semantics, input=prompt, output=40)
    return {"kind": "trace", "step": step}


def _context_log(tmp_path, name: str, records: list[dict], menus: tuple[str, ...] = ()
                 ) -> Path:
    state = tmp_path / name
    for menu in menus:
        evidence.put(menu, state)
    return write_log(state / "session-1.jsonl", records)


def _arm_log(tmp_path, name: str, menu: str, prompts: list[int]) -> Path:
    """A run that fails the target, so both arms are shown, with one brief and
    one reasoning record per prompt count."""
    return write_log(tmp_path / f"{name}.jsonl", [
        TASK, brief([1000, 200], menu), *[reasoning(i + 1, p) for i, p in enumerate(prompts)],
        WRONG_PARTY, result(1, "run_command"), answer("fine"), END])


class TestContextPerModelCall:
    """What each model call was handed, as recorded — never a 0 for an absence,
    never chars and tokens in one figure (`docs/token-accounting.md`)."""

    def test_each_call_is_joined_to_the_brief_in_force(self, tmp_path):
        path = _context_log(tmp_path, "state", [
            TASK, brief([1000, 200], BIG_MENU), reasoning(1, 40_000), reasoning(2, 70_000),
            brief([1000, 400], BIG_MENU), reasoning(1, 65_000), reasoning(2), END,
        ], menus=(BIG_MENU,))
        context = replay_checks.read_context(path)
        assert context.calls == 4
        assert context.prompt_tokens == [40_000, 70_000, 65_000]
        assert context.tokens_absent == 1
        assert context.system_chars == [1200, 1200, 1400, 1400]
        assert context.menu_chars == [3000] * 4
        assert context.tools == [26] * 4

    def test_median_p95_and_the_window_are_over_recorded_counts_only(self, tmp_path):
        path = _context_log(tmp_path, "state", [
            TASK, brief([1000], BIG_MENU), reasoning(1, 40_000), reasoning(2, 70_000),
            reasoning(3, 65_000), reasoning(4), END], menus=(BIG_MENU,))
        summary = replay.context_summary([replay_checks.read_context(path)], 60_000)
        assert summary["prompt_tokens"] == {
            "recorded": 3, "not_recorded": 1, "semantics": [backends.INPUT_INCLUDES_CACHE],
            "median": 65_000, "p95": 70_000}
        assert summary["over_window"] == {"window": 60_000, "calls": 2, "of": 3}
        # Strictly OVER the window: a call at exactly the window fitted.
        tight = replay.context_summary([replay_checks.read_context(path)], 65_000)
        assert tight["over_window"]["calls"] == 1

    def test_a_backend_that_reported_nothing_is_not_recorded_never_zero(self, tmp_path):
        path = _context_log(tmp_path, "state", [
            TASK, brief([1000], BIG_MENU), reasoning(1), reasoning(2), END], menus=(BIG_MENU,))
        summary = replay.context_summary([replay_checks.read_context(path)], 60_000)
        assert summary["prompt_tokens"] == {"recorded": 0, "not_recorded": 2}
        assert "over_window" not in summary  # unknown, not "0 calls over"
        lines = "\n".join(replay._context_line("baseline", summary))
        assert "tokens not recorded on any of 2 calls" in lines
        assert "over window: not recorded" in lines
        # The chars are measured and stand; nothing estimates tokens from them.
        assert "sys 1.0k/1.0k · menu 3.0k/3.0k · tools 26" in lines
        assert "0 of" not in lines

    def test_purged_menu_missing_brief_and_missing_system_are_counted_apart(self, tmp_path):
        path = _context_log(tmp_path, "state", [
            TASK, reasoning(1, 5_000),             # before any brief
            brief(None, BIG_MENU), reasoning(2, 6_000),  # system half not kept; bytes gone
            END])
        context = replay_checks.read_context(path)
        assert (context.calls, context.menu_chars, context.menu_purged,
                context.menu_missing) == (2, [], 1, 1)
        summary = replay.context_summary([context], 60_000)
        assert summary["menu_chars"] == {"recorded": 0, "purged": 1, "not_recorded": 1}
        assert summary["system_chars"] == {"recorded": 0, "not_recorded": 2}
        assert summary["tools"] == {"recorded": 1, "not_recorded": 1, "min": 26, "max": 26}
        line = "\n".join(replay._context_line("b1", summary))
        assert "sys not recorded · menu not recorded · tools 26" in line

    def test_counts_in_two_units_are_not_combined(self, tmp_path):
        path = _context_log(tmp_path, "state", [
            TASK, brief([10]), reasoning(1, 9_000),
            reasoning(2, 70_000, semantics=backends.INPUT_EXCLUDES_KV_REUSE),
            reasoning(3, 8_000, semantics=None), END])
        summary = replay.context_summary([replay_checks.read_context(path)], 60_000)
        assert summary["prompt_tokens"]["semantics"] == sorted([
            backends.INPUT_INCLUDES_CACHE, backends.INPUT_EXCLUDES_KV_REUSE,
            replay_checks.UNLABELLED])
        assert "median" not in summary["prompt_tokens"] and "over_window" not in summary
        line = "\n".join(replay._context_line("b1", summary))
        assert "mixed units" in line and "over window: not combined" in line

    def test_ollama_counts_say_they_are_floors(self, tmp_path):
        path = _context_log(tmp_path, "state", [
            TASK, brief([10]), reasoning(1, 9_000, semantics=backends.INPUT_EXCLUDES_KV_REUSE),
            END])
        summary = replay.context_summary([replay_checks.read_context(path)], 60_000)
        assert "are floors" in "\n".join(replay._context_line("b1", summary))

    def test_the_real_writer_with_a_backend_that_reports_no_usage(self, generated):
        """The generated run is the real server and agent: its scripted model
        reports no usage, so every count is absent, while the brief's chars are
        recorded for every call that `counts` sees."""
        for path in generated.values():
            context = replay_checks.read_context(path)
            run = replay_checks.load_run(path)
            assert context.calls == replay_checks.counts(run)["model_calls"] > 0
            assert context.prompt_tokens == [] and context.tokens_absent == context.calls
            assert len(context.system_chars) == len(context.menu_chars) == context.calls
            assert len(context.tools) == context.calls

    def _batch(self, tmp_path, **launch_kw) -> Path:
        logs = {"baseline": _arm_log(tmp_path, "base", BIG_MENU, [70_000, 50_000, 20_000]),
                "candidate": _arm_log(tmp_path, "cand", SMALL_MENU, [40_000, 30_000, 10_000])}
        return _batch(tmp_path, FakeLaunch(logs, menus=(BIG_MENU, SMALL_MENU), **launch_kw))

    def test_the_report_pools_each_arm_call_by_call(self, tmp_path):
        batch = self._batch(tmp_path)
        text = replay.render_report(*replay.read_batch(batch), batch)
        assert len(text) <= replay.REPORT_CAP
        assert ("baseline 2 runs, 6 calls · tokens median 50.0k p95 70.0k input_includes_cache"
                " · over 60,000: 2 of 6 · sys 1.2k/1.2k · menu 3.0k/3.0k · tools 26") in text
        assert ("candidate 2 runs, 6 calls · tokens median 30.0k p95 40.0k input_includes_cache"
                " · over 60,000: 0 of 6 · sys 1.2k/1.2k · menu 1.0k/1.0k · tools 26") in text
        # At the shipped cap the nine-check, two-arm report carries its evidence
        # whole — what the 2,048 cap cut on real reports this program.
        assert ("evidence (first failures):\ntrippy_party [b1,b2] 1 of 1 searches with another "
                "party") in text
        assert text.rstrip().splitlines()[-1].startswith("trippy_party [c1,c2] ")
        assert "more evidence lines" not in text
        # The words of the report law still hold with the new rows in it.
        for word in ("%", "fixed", "improved", "better", "resolved", "rate"):
            assert word not in text.lower()

    def test_evidence_past_the_cap_is_omitted_with_the_pointer_whole(self, tmp_path,
                                                                    monkeypatch):
        """The reserve logic, exercised at the cap it was first built under:
        the evidence lines share the cap, and the pointer to them is never cut."""
        monkeypatch.setattr(replay, "REPORT_CAP", 2048)
        batch = self._batch(tmp_path)
        text = replay.render_report(*replay.read_batch(batch), batch)
        assert len(text) <= 2048
        assert text.rstrip().endswith("more evidence lines: report --full)") or \
            text.rstrip().endswith(f"aish-replay report {batch} --full)")

    def test_full_adds_every_run(self, tmp_path):
        batch = self._batch(tmp_path)
        text = replay.render_report(*replay.read_batch(batch), batch, full=True)
        assert "  b2, 3 calls · tokens median 50.0k p95 70.0k" in text
        assert "  c1, 3 calls · tokens median 30.0k p95 40.0k" in text

    def _timed_batch(self, tmp_path) -> Path:
        def timed(name, menu):
            return write_log(tmp_path / f"{name}.jsonl", [
                TASK, brief([1000, 200], menu),
                *[{"kind": "trace", "step": {**reasoning(n, 40_000)["step"],
                                             "ms": 1000 * n, "first_chunk_ms": 300 * n}}
                  for n in (1, 2, 3)],
                WRONG_PARTY, result(1, "run_command"), answer("fine"), END])
        return _batch(tmp_path, FakeLaunch(
            {"baseline": timed("base", BIG_MENU), "candidate": timed("cand", SMALL_MENU)},
            menus=(BIG_MENU, SMALL_MENU)))

    def test_time_lines_fit_under_the_shipped_cap(self, tmp_path):
        """The 'trippy time lines' a real report lost under the 2,048 cap."""
        batch = self._timed_batch(tmp_path)
        text = replay.render_report(*replay.read_batch(batch), batch)
        assert len(text) <= replay.REPORT_CAP
        assert replay.TIME_DROPPED not in text
        assert ("  time median/p95 · first 2: 1.0s/1.0s, chunk 0.3s/0.3s · "
                "later 4: 3.0s/3.0s, chunk 0.9s/0.9s") in text

    def test_time_lines_that_would_not_fit_under_the_cap_are_dropped_and_said(
            self, tmp_path, capsys, monkeypatch):
        """Call timing (#444) widens each arm's row; on a nine-check scenario
        under a tighter cap that would run the body into the hard cut. Dropped
        together, said in a line, and still whole in --full and --json."""
        monkeypatch.setattr(replay, "REPORT_CAP", 2048)

        def timed(name, menu):
            return write_log(tmp_path / f"{name}.jsonl", [
                TASK, brief([1000, 200], menu),
                *[{"kind": "trace", "step": {**reasoning(n, 40_000)["step"],
                                             "ms": 1000 * n, "first_chunk_ms": 300 * n}}
                  for n in (1, 2, 3)],
                WRONG_PARTY, result(1, "run_command"), answer("fine"), END])
        batch = _batch(tmp_path, FakeLaunch(
            {"baseline": timed("base", BIG_MENU), "candidate": timed("cand", SMALL_MENU)},
            menus=(BIG_MENU, SMALL_MENU)))
        loaded = replay.read_batch(batch)
        text = replay.render_report(*loaded, batch)
        assert len(text) <= replay.REPORT_CAP
        assert replay.TIME_DROPPED in text and "time median/p95" not in text
        assert text.rstrip().endswith("--full)")
        full = replay.render_report(*loaded, batch, full=True)
        assert ("  time median/p95 · first 2: 1.0s/1.0s, chunk 0.3s/0.3s · "
                "later 4: 3.0s/3.0s, chunk 0.9s/0.9s") in full
        assert replay.TIME_DROPPED not in full
        assert replay.main(["report", str(batch), "--json"]) == 0
        timing = json.loads(capsys.readouterr().out)["arms"]["baseline"]["context"]["timing"]
        assert timing["later"]["ms"] == {"recorded": 4, "not_recorded": 0,
                                         "median": 3000, "p95": 3000}

    def test_json_carries_runs_and_arms_and_takes_the_window(self, tmp_path, capsys):
        batch = self._batch(tmp_path)
        assert replay.main(["report", str(batch), "--json", "--window", "40000"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["window"] == 40_000 and payload["withheld"] is None
        base, cand = payload["arms"]["baseline"], payload["arms"]["candidate"]
        assert base["context"]["over_window"] == {"window": 40_000, "calls": 4, "of": 6}
        assert cand["context"]["over_window"] == {"window": 40_000, "calls": 0, "of": 6}
        assert cand["context"]["menu_chars"]["median"] == 1000
        assert base["checks"]["trippy_party"] == {"passed": 0, "failed": 2, "could_not_tell": 0}
        run = base["runs"][0]
        assert run["context"]["prompt_tokens"]["p95"] == 70_000
        assert run["context"]["calls"] == 3

    def test_an_incomplete_run_is_not_pooled(self, tmp_path, capsys):
        batch = self._batch(tmp_path, exit_code=1)
        text = replay.render_report(*replay.read_batch(batch), batch)
        assert "baseline: no complete run to read" in text
        assert replay.main(["report", str(batch), "--json"]) == 0
        base = json.loads(capsys.readouterr().out)["arms"]["baseline"]
        assert base["complete_runs"] == 0 and "context" not in base
        assert base["runs"][0]["context"]["calls"] == 3  # each run's own record stands

    def test_json_withholds_the_candidate_as_the_text_does(self, tmp_path, capsys):
        logs = {"baseline": _passing_log(tmp_path),
                "candidate": _arm_log(tmp_path, "cand", SMALL_MENU, [1_000])}
        batch = _batch(tmp_path, FakeLaunch(logs))
        assert replay.main(["report", str(batch), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["withheld"].startswith("the baseline did not reproduce")
        assert list(payload["arms"]) == ["baseline"]

    def test_the_window_must_be_positive(self, tmp_path):
        with pytest.raises(SystemExit):
            replay.main(["report", str(tmp_path), "--window", "0"])
