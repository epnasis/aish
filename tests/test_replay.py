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

    def test_the_driver_imports_no_harness_module_at_load(self):
        """It runs against a baseline tree that predates the harness."""
        tree = ast.parse(Path(replay_driver.__file__).read_text(encoding="utf-8"))
        top = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
        names = [getattr(n, "module", None) or n.names[0].name for n in top]
        assert not [n for n in names if n and n.startswith("aish")]


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
                 exit_code: int = 0, menus: tuple[str, ...] = ()):
        self.logs, self.mutate, self.exit_code = logs, mutate_corpus, exit_code
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
                  {"type": "card", "kind": "command"}, {"type": "end"}]
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
        kind's text is byte-identical; its JSON gained only the `kind` key."""
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
        # The evidence lines share the cap; the pointer to them stays whole.
        assert text.rstrip().endswith("more evidence lines: report --full)") or \
            text.rstrip().endswith(f"aish-replay report {batch} --full)")
        # The words of the report law still hold with the new rows in it.
        for word in ("%", "fixed", "improved", "better", "resolved", "rate"):
            assert word not in text.lower()

    def test_full_adds_every_run(self, tmp_path):
        batch = self._batch(tmp_path)
        text = replay.render_report(*replay.read_batch(batch), batch, full=True)
        assert "  b2, 3 calls · tokens median 50.0k p95 70.0k" in text
        assert "  c1, 3 calls · tokens median 30.0k p95 40.0k" in text

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
