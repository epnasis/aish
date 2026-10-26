"""Epic #462 S1: every command that runs with no card runs in aish's sandbox.

Three layers, each pinned where it can be:

- the profile, the env, the log parser and the canary — pure, every platform;
- the agent's routing (which commands are sandboxed, the typed verdict, fail
  closed, the refusal record and note) — through the faked seam
  (`tests/fake_sandbox.py`), every platform, CI included;
- the four exploits an adversarial review found on shipped code, run for real
  under the real profile with `sandbox-exec` — `macos_sandbox`, skipped where
  the sandbox does not exist. They are written against the classifier that
  still waves each of them through, so on main (no sandbox) every one of them
  does its damage and the test fails; here the KERNEL refuses it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fake_sandbox import PassThroughSandbox, RefusingSandbox, UnavailableSandbox
from test_agent import make_agent, model_says, tool_call, tool_messages

from aish import approval, sandbox, tools
from aish.approval import AutoApproved, is_auto_approvable
from aish.cli import make_approver

REAL_SANDBOX = sys.platform == "darwin" and os.access(sandbox.SANDBOX_EXEC, os.X_OK)
macos_sandbox = pytest.mark.skipif(
    not REAL_SANDBOX, reason="needs macOS sandbox-exec (the real Seatbelt profile)"
)


def auto(command: str) -> AutoApproved:
    return AutoApproved(command)


def run_steps(agent) -> list[dict]:
    return [s for s in agent._steps if s.get("kind") == "tool" and s.get("name") == "run_command"]


def agent_with(commands, approve, *, runner, tmp_path, origin="user", steps=None):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    responses = [model_says(tool_calls=[tool_call("run_command", command=c)]) for c in commands]
    responses.append(model_says("done"))
    log: list[dict] = steps if steps is not None else []
    agent, chat = make_agent(
        responses, approve=approve, cwd=str(project), sandbox_runner=runner,
        step_log=log.append, origin=origin,
    )
    agent._steps = log
    return agent, chat


# --------------------------------------------------------------------- profile


class TestTheProfile:
    def paths(self, tmp_path):
        return sandbox.profile_paths(
            [tmp_path / "project"], tmp_path / "scratch", protected=[tmp_path / "state"]
        )

    def test_every_deny_is_tagged_with_the_run_nonce(self, tmp_path):
        text = sandbox.render_profile(self.paths(tmp_path), ["/bin/sh"])
        denies = [line for line in text.splitlines() if line.startswith("(deny")]
        assert denies
        assert all(sandbox.NONCE_SLOT in line for line in denies), denies

    def test_exec_is_an_exact_path_list(self, tmp_path):
        text = sandbox.render_profile(self.paths(tmp_path), ["/bin/sh", "/bin/cat"])
        assert '(allow process-exec* (literal "/bin/sh") (literal "/bin/cat"))' in text

    def test_writes_reach_only_scratch_and_terminal_devices(self, tmp_path):
        text = sandbox.render_profile(self.paths(tmp_path), ["/bin/sh"])
        write_allows = [ln for ln in text.splitlines() if ln.startswith("(allow file-write*")]
        assert len(write_allows) == 1
        assert f'(subpath "{os.path.realpath(tmp_path / "scratch")}")' in write_allows[0]
        assert "project" not in write_allows[0]

    def test_secret_names_are_closed_after_the_roots_are_opened(self, tmp_path):
        """Last match wins in Seatbelt, so the order IS the rule: the
        sensitive-name deny must come after the root allow it carves into,
        and stat() must stay open after it (ls -l in a project with a .env)."""
        lines = sandbox.render_profile(self.paths(tmp_path), ["/bin/sh"]).splitlines()
        roots = next(i for i, ln in enumerate(lines) if ln.startswith("(allow file-read* (subpath"))
        secret = next(i for i, ln in enumerate(lines) if "[eE][nN][vV]" in ln)
        metadata = lines.index("(allow file-read-metadata)")
        assert roots < secret < metadata

    def test_aish_trees_close_again_and_reopen_only_what_lives_in_them(self, tmp_path):
        state = tmp_path / "state"
        paths = sandbox.profile_paths(
            [tmp_path / "project", state / "uploads"], state / "scratch" / "chat",
            protected=[state],
        )
        lines = sandbox.render_profile(paths, ["/bin/sh"]).splitlines()
        real_state = os.path.realpath(state)
        closed = next(i for i, ln in enumerate(lines)
                      if ln.startswith("(deny file-read*") and real_state in ln)
        reopened = lines[closed + 1]
        assert reopened.startswith("(allow file-read*")
        assert "uploads" in reopened and "scratch" in reopened
        assert "project" not in reopened

    def test_a_path_holding_a_control_character_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sandbox.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox.os, "access", lambda *_a: True)
        result = sandbox.SeatbeltSandbox(read_only_names=()).prepare(
            [str(tmp_path / "bad\nname")], tmp_path / "scratch"
        )
        assert not result
        assert "control character" in result.observed

    def test_quotes_and_backslashes_are_escaped(self):
        assert sandbox._sbpl_string('a"b\\c') == '"a\\"b\\\\c"'

    def test_the_sensitive_rules_come_from_files(self):
        """One list, two enforcers: the kernel closes exactly the names
        `files.is_sensitive_path` cards for."""
        import re

        from aish import files

        rules = [re.compile(r) for r in sandbox.sensitive_regexes()]
        for path in ("/p/.env", "/p/.ENV.local", "/p/a/.ssh/config", "/p/id_rsa",
                     "/p/k.pem", "/p/x.KEY", "/p/.netrc", "/p/gcloud/creds.json"):
            assert files.is_sensitive_path(path, "/")
            assert any(r.search(path) for r in rules), path
        for path in ("/p/notes.txt", "/p/envoy.md", "/p/keys.txt"):
            assert not files.is_sensitive_path(path, "/")
            assert not any(r.search(path) for r in rules), path


class TestTheExecList:
    def make(self, bin_dir: Path, name: str, body: bytes, mode: int = 0o755) -> Path:
        path = bin_dir / name
        path.write_bytes(body)
        path.chmod(mode)
        return path

    def test_interpreters_scripts_and_setuid_stay_off(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self.make(bin_dir, "cat", b"\xcf\xfa\xed\xfe binary")
        self.make(bin_dir, "man", b"\xcf\xfa\xed\xfe binary")  # an interpreter by name
        self.make(bin_dir, "shasum", b"#!/usr/bin/perl\n")  # a script
        self.make(bin_dir, "ps", b"\xcf\xfa\xed\xfe binary", 0o4755)  # setuid
        self.make(bin_dir, "which", b"\xcf\xfa\xed\xfe binary")  # answers wrong inside
        listed = sandbox.exec_allow_list(["cat", "man", "shasum", "ps", "which"], str(bin_dir))
        assert os.path.realpath(bin_dir / "cat") in listed
        for name in ("man", "shasum", "ps", "which"):
            assert os.path.realpath(bin_dir / name) not in listed, name
        assert sandbox.SHELL in listed

    def test_a_symlink_is_listed_as_what_it_points_at(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        real = self.make(tmp_path, "real-cat", b"\xcf\xfa\xed\xfe binary")
        (bin_dir / "cat").symlink_to(real)
        listed = sandbox.exec_allow_list(["cat"], str(bin_dir))
        assert os.path.realpath(real) in listed
        assert str(bin_dir / "cat") not in listed

    def test_ps_is_no_longer_a_read_only_command(self):
        """It is setuid, so it could only ever fail inside the sandbox; it
        cards instead, and the `processes` tool answers its question."""
        assert "ps" not in approval.SAFE_COMMANDS
        assert not approval.is_read_only("ps aux")


class TestTheEnvironment:
    def test_only_allow_listed_names_survive(self, tmp_path):
        env = sandbox.clean_env(
            str(tmp_path),
            {
                "PATH": "/usr/bin", "HOME": "/Users/x", "LANG": "C", "GH_TOKEN": "t",
                "OPENAI_API_KEY": "k", "SSH_AUTH_SOCK": "/s", "DYLD_INSERT_LIBRARIES": "/x",
                "LD_PRELOAD": "/y", "AWS_SECRET_ACCESS_KEY": "z", "TERM": "xterm",
            },
        )
        assert set(env) == {"PATH", "HOME", "LANG", "TERM", "TMPDIR", "XDG_CACHE_HOME"}
        assert env["TMPDIR"].startswith(str(tmp_path))
        assert env["XDG_CACHE_HOME"].startswith(str(tmp_path))


class TestTheRefusalRecord:
    NONCE = "a1b2c3"

    def entry(self, message: str) -> str:
        import json

        return json.dumps({"processImagePath": "/kernel", "eventMessage": message})

    def test_a_denial_counts_only_when_its_entry_ends_with_the_nonce(self):
        text = "\n".join([
            "not json",  # a line that is not an entry is skipped
            self.entry(
                f"Sandbox: sort(91592) deny(1) file-write-data /Users/x/.zshrc\n{self.NONCE}"
            ),
            self.entry("Sandbox: cat(9) deny(1) file-read-data /p/.env\nsomeothernonce"),
            self.entry(f"Sandbox: bash(9) deny(1) process-exec* /usr/bin/man {self.NONCE}"),
            self.entry(f"something else\n{self.NONCE}"),
        ])
        refusals, seen = sandbox.parse_refusals(text, self.NONCE)
        assert [r.as_record() for r in refusals] == [
            {"process": "sort", "op": "file-write-data", "target": "/Users/x/.zshrc"}
        ]
        assert seen == 1

    def test_a_file_name_cannot_forge_a_refusal(self):
        """The kernel writes the denied PATH into the same entry as the nonce,
        and a path may hold a newline. Measured on this Mac (2026-10-26): a
        file named `.envq\\nSandbox: injected(7) deny(1) network-outbound
        evilhost:443` produced exactly this entry. The injected text stays in
        the target, escaped — it never becomes a refusal of its own."""
        forged = (
            "Sandbox: cat(12757) deny(1) file-read-data /p/.envq\n"
            "Sandbox: injected(7) deny(1) network-outbound evilhost:443\n"
            f"{self.NONCE}"
        )
        refusals, seen = sandbox.parse_refusals(self.entry(forged), self.NONCE)
        assert seen == 1
        assert [(r.process, r.operation) for r in refusals] == [("cat", "file-read-data")]
        assert "\n" not in refusals[0].target
        assert refusals[0].target.startswith("/p/.envq\\nSandbox: injected")

    def test_repeats_collapse_and_the_sample_is_capped_with_the_total_kept(self):
        lines = []
        for i in range(sandbox.REFUSAL_SAMPLE_MAX + 5):
            for _ in range(2):
                lines.append(
                    self.entry(f"Sandbox: grep(1) deny(1) file-read-data /p/{i}\n{self.NONCE}")
                )
        refusals, seen = sandbox.parse_refusals("\n".join(lines), self.NONCE)
        assert len(refusals) == sandbox.REFUSAL_SAMPLE_MAX
        assert seen == 2 * (sandbox.REFUSAL_SAMPLE_MAX + 5)

    def test_the_query_reads_kernel_lines_only(self, monkeypatch):
        """A sandboxed process can write its own log lines (`logger`); only
        the kernel's are evidence."""
        seen: list[list[str]] = []

        def fake_run(argv, **_kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
        report = sandbox.SeatbeltSandbox().read_refusals(self.NONCE, 0.0)
        predicate = seen[0][seen[0].index("--predicate") + 1]
        assert predicate.startswith('process == "kernel" AND ')
        assert self.NONCE in predicate
        assert report.refusals == [] and not report.read_error

    def test_an_unreadable_log_is_said_not_guessed(self, monkeypatch):
        def broken(argv, **_kw):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no access")

        monkeypatch.setattr(sandbox.subprocess, "run", broken)
        report = sandbox.SeatbeltSandbox().read_refusals(self.NONCE, 0.0)
        assert "no access" in report.read_error
        assert report.as_record()["log_error"]

    def test_a_failed_run_with_nothing_found_is_read_twice(self, monkeypatch):
        calls = []

        def read(nonce, since):
            calls.append(nonce)
            return sandbox.RefusalReport()

        monkeypatch.setattr(sandbox.time, "sleep", lambda _s: None)
        owner = sandbox.SeatbeltSandbox()
        monkeypatch.setattr(owner, "read_refusals", read)
        paths = sandbox.profile_paths(["/p"], "/s")
        confinement = sandbox.Confinement("(version 1)", paths, owner)
        assert confinement.refusals(exit_code=0).reads == 1
        assert confinement.refusals(exit_code=1).reads == 2
        assert len(calls) == 3


class TestTheCanary:
    """It judges by EFFECTS on disk, never by an exit code."""

    def fake_platform(self, monkeypatch):
        monkeypatch.setattr(sandbox.SeatbeltSandbox, "_platform_problem", lambda self: None)

    def test_no_sandbox_at_all_fails_the_canary(self, tmp_path, monkeypatch):
        """A sandbox-exec that applies nothing (here: the probe run by a bare
        shell) lets the canary write outside scratch — that is Unavailable."""
        self.fake_platform(monkeypatch)
        real_run = subprocess.run

        def unconfined(argv, **kw):
            if argv and argv[0] == sandbox.SANDBOX_EXEC:
                argv = argv[3:]  # drop `sandbox-exec -p <profile>`
            return real_run(argv, **kw)

        monkeypatch.setattr(sandbox.subprocess, "run", unconfined)
        result = sandbox.SeatbeltSandbox(read_only_names=()).prepare(
            [tmp_path], tmp_path / "scratch"
        )
        assert not result
        assert "outside scratch" in result.observed

    def test_a_profile_that_runs_nothing_fails_the_canary(self, tmp_path, monkeypatch):
        self.fake_platform(monkeypatch)
        monkeypatch.setattr(
            sandbox.subprocess, "run",
            lambda argv, **_kw: subprocess.CompletedProcess(argv, 65),
        )
        result = sandbox.SeatbeltSandbox(read_only_names=()).prepare(
            [tmp_path], tmp_path / "scratch"
        )
        assert not result
        assert "did not run a command" in result.observed

    def test_the_canary_runs_once_per_profile(self, tmp_path, monkeypatch):
        self.fake_platform(monkeypatch)
        runs = []

        def obedient(argv, **kw):
            runs.append(argv)
            script = argv[-1]
            ran = script.split("echo ok > '", 1)[1].split("'", 1)[0]
            Path(ran).write_text("ok")
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(sandbox.subprocess, "run", obedient)
        box = sandbox.SeatbeltSandbox(read_only_names=())
        first = box.prepare([tmp_path], tmp_path / "scratch")
        second = box.prepare([tmp_path], tmp_path / "scratch")
        assert first and second
        assert first.nonce != second.nonce
        assert len(runs) == 1
        assert not list((tmp_path / "scratch").glob(".aish-canary-*"))

    def test_off_macos_it_is_unavailable_and_says_why(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sandbox.sys, "platform", "linux")
        result = sandbox.SeatbeltSandbox().prepare([tmp_path], tmp_path / "scratch")
        assert not result
        assert "linux" in result.observed


# ---------------------------------------------------------------- the seam


class TestTheSeamHasNoDefault:
    def test_run_command_requires_a_stated_confinement(self):
        with pytest.raises(TypeError):
            tools.run_command("echo hi")  # type: ignore[call-arg]

    def test_start_background_requires_a_stated_confinement(self):
        with pytest.raises(TypeError):
            tools.start_background("echo hi")  # type: ignore[call-arg]

    def test_a_confined_command_always_gets_its_own_session(self, monkeypatch, tmp_path):
        """`(allow signal (target pgrp))` is safe only because the command leads
        its own process group: launched without a new session, the delivery
        review's probe killed a process outside it (2026-10-26)."""
        seen = []

        class Recorded:
            def __init__(self, argv, **kw):
                seen.append(kw)
                raise OSError("recorded, not run")

        class Launches:
            def launch(self, command):
                return ["/bin/sh", "-c", command], {}

        monkeypatch.setattr(tools.subprocess, "Popen", Recorded)
        tools.run_command("true", confinement=Launches())
        tools.start_background("true", confinement=Launches(), log_dir=tmp_path)
        assert len(seen) == 2
        assert all(kw.get("start_new_session") is True for kw in seen)

    def test_anything_but_unsandboxed_is_launched_as_a_confinement(self):
        class Launches:
            def launch(self, command):
                return ["/bin/sh", "-c", f"echo confined:{command}"], dict(os.environ)

        result = tools.run_command("x", confinement=Launches())
        assert "confined:x" in result


# ---------------------------------------------------------------- the routing


class TestWhatRunsInTheSandbox:
    def test_a_read_only_auto_approval_runs_sandboxed(self, tmp_path):
        runner = PassThroughSandbox()
        agent, _ = agent_with(["echo hi"], auto, runner=runner, tmp_path=tmp_path)
        agent.run_task("go")
        assert runner.launched == ["echo hi"]
        step = run_steps(agent)[0]
        assert step["sandbox"]["confined"] is True
        assert step["sandbox"]["deferred"] is True  # exit 0: read off the path

    def test_the_sandbox_is_given_this_chats_roots_scratch_and_aishs_trees(self, tmp_path):
        runner = PassThroughSandbox()
        agent, _ = agent_with(["ls"], auto, runner=runner, tmp_path=tmp_path)
        agent.run_task("go")
        prepared = runner.prepared[0]
        assert prepared["scratch"] == agent.scratch_dir
        assert Path(agent.cwd).resolve() in [Path(r) for r in prepared["roots"]]
        assert len(prepared["protected"]) == 2

    def test_a_prefix_rule_command_runs_as_today_and_says_so(self, tmp_path):
        """Tier T (an allow.txt rule) is S2's: not sandboxed yet, recorded."""
        runner = PassThroughSandbox()
        agent, _ = agent_with(["git status"], lambda c: AutoApproved(c, by_rule=True),
                              runner=runner, tmp_path=tmp_path)
        agent.run_task("go")
        assert runner.launched == []
        assert run_steps(agent)[0]["sandbox"] == {"confined": False, "why": "allowlist"}

    def test_a_cardless_verdict_that_names_no_rule_is_sandboxed(self, tmp_path):
        """The polarity fails closed: `by_rule` must be SAID for a cardless
        command to run unsandboxed, whatever the command is."""
        runner = PassThroughSandbox()
        agent, _ = agent_with(["git status"], auto, runner=runner, tmp_path=tmp_path)
        agent.run_task("go")
        assert runner.launched == ["git status"]
        assert run_steps(agent)[0]["sandbox"]["confined"] is True

    def test_a_rule_claim_on_a_read_only_command_is_still_sandboxed(self, tmp_path):
        runner = PassThroughSandbox()
        agent, _ = agent_with(["ls"], lambda c: AutoApproved(c, by_rule=True),
                              runner=runner, tmp_path=tmp_path)
        agent.run_task("go")
        assert runner.launched == ["ls"]

    def test_the_real_approvers_say_when_a_rule_was_needed(self, tmp_path):
        allow = tmp_path / "allow.txt"
        allow.write_text("git status\n")
        approve = make_approver(False, allow, None, deny_path=tmp_path / "deny.txt")
        assert approve("git status").by_rule is True
        assert approve("ls").by_rule is False

    def test_a_clicked_command_runs_as_today_and_says_so(self, tmp_path):
        runner = PassThroughSandbox()
        agent, _ = agent_with(["echo hi"], lambda _c: True, runner=runner, tmp_path=tmp_path)
        agent.run_task("go")
        assert runner.launched == []
        assert run_steps(agent)[0]["sandbox"] == {"confined": False, "why": "card"}

    def test_scratch_housekeeping_runs_sandboxed(self, tmp_path):
        runner = PassThroughSandbox()
        asked = []
        agent, chat = agent_with([], lambda c: asked.append(c) or True, runner=runner,
                                 tmp_path=tmp_path)
        command = f"mkdir -p {agent.scratch_dir}/out"
        chat.responses[:0] = [
            model_says(tool_calls=[tool_call("run_command", command=command)])
        ]
        agent.run_task("go")
        assert runner.launched == [command]
        assert asked == []
        assert (Path(agent.scratch_dir) / "out").is_dir()

    def test_a_background_cardless_job_runs_sandboxed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tools, "JOBS", [])
        runner = PassThroughSandbox()
        agent, chat = agent_with([], auto, runner=runner, tmp_path=tmp_path)
        agent.job_log_dir = tmp_path / "jobs"
        chat.responses[:0] = [
            model_says(tool_calls=[tool_call("run_command", command="echo bg", background=True)])
        ]
        agent.run_task("go")
        assert runner.launched == ["echo bg"]
        step = run_steps(agent)[0]
        assert step["sandbox"]["confined"] is True
        assert step["sandbox"]["refusals_read"] is False
        tools.JOBS[-1]["proc"].wait(timeout=10)


    def test_a_ctrl_b_detach_says_its_refusals_are_not_read(self, tmp_path, monkeypatch):
        """A detached job keeps running in the same sandbox; reading the log at
        the moment of detach would record a partial sample as the whole run."""
        runner = PassThroughSandbox()

        def detached(command, **kw):
            kw["confinement"].launch(command)
            return f"{tools.DETACHED_PREFIX} pid 1, log: /tmp/x.log]\nStill running."

        monkeypatch.setattr(tools, "run_command", detached)
        agent, _ = agent_with(["find . -name x"], auto, runner=runner, tmp_path=tmp_path)
        agent.run_task("go")
        step = run_steps(agent)[0]
        assert step["sandbox"]["confined"] is True
        assert step["sandbox"]["refusals_read"] is False
        assert runner.read == []
        assert "not read back" in tool_messages(agent.messages)[0]["content"]


class TestFailClosed:
    """No sandbox ⇒ no cardless shell: attended asks, unattended refuses."""

    def test_attended_draws_a_card_that_says_what_was_observed(self, tmp_path):
        runner = UnavailableSandbox()
        reasons = []
        agent = None

        def approve(command):
            reasons.append(agent.shell_card_reason())
            return command  # the owner clicked

        agent, _ = agent_with(["ls"], approve, runner=runner, tmp_path=tmp_path)
        agent.run_task("go")
        assert "not available" in reasons[0]
        assert runner.observed in reasons[0]
        assert agent.shell_card_reason() == ""  # only for the one call
        assert run_steps(agent)[0]["sandbox"] == {"confined": False, "why": "card"}

    def test_unattended_refuses_without_asking(self, tmp_path):
        asked = []
        agent, _ = agent_with(["ls"], lambda c: asked.append(c), runner=UnavailableSandbox(),
                              tmp_path=tmp_path, origin="schedule")
        agent.run_task("go")
        assert asked == []
        step = run_steps(agent)[0]
        assert step["decision"] == "rejected" and step["ok"] is False
        result = tool_messages(agent.messages)[0]["content"]
        assert result.startswith("NOT RUN")

    def test_an_approver_that_ignores_the_card_request_is_overruled(self, tmp_path):
        """An approver not wired to `shell_card_reason` still answers
        AutoApproved; the agent must not run it unconfined."""
        agent, _ = agent_with(["ls"], auto, runner=UnavailableSandbox(), tmp_path=tmp_path)
        agent.run_task("go")
        step = run_steps(agent)[0]
        assert step["decision"] == "rejected"
        assert "NOT RUN" in tool_messages(agent.messages)[0]["content"]

    def test_the_real_cli_approver_cards_when_told_to(self, tmp_path, monkeypatch):
        answers = iter(["y"])
        monkeypatch.setattr("builtins.input", lambda *_a: next(answers))
        approve = make_approver(
            False, tmp_path / "allow.txt", None, deny_path=tmp_path / "deny.txt",
            get_card_reason=lambda: "aish's sandbox is not available (test)",
        )
        assert approve("ls") == "ls"  # a click, not AutoApproved

    def test_the_real_cli_approver_returns_the_typed_verdict(self, tmp_path):
        approve = make_approver(False, tmp_path / "allow.txt", None,
                                deny_path=tmp_path / "deny.txt")
        verdict = approve("ls")
        assert isinstance(verdict, AutoApproved) and verdict.command == "ls"


class TestTheRefusalReachesTheTurn:
    def test_the_model_is_told_what_was_refused_and_how_to_ask(self, tmp_path):
        runner = RefusingSandbox()
        agent, _ = agent_with(["cat no-such-file"], auto, runner=runner,
                              tmp_path=tmp_path)
        agent.run_task("go")
        result = tool_messages(agent.messages)[0]["content"]
        assert "sort file-write-data /etc/hosts" in result
        assert "sample" in result
        assert "propose it again" in result
        step = run_steps(agent)[0]
        assert step["sandbox"]["refused"] == [
            {"process": "sort", "op": "file-write-data", "target": "/etc/hosts"}
        ]
        assert "sort file-write-data /etc/hosts" in step["output"]

    def test_the_same_command_proposed_again_is_carded(self, tmp_path):
        runner = RefusingSandbox()
        reasons = []
        agent = None

        def approve(command):
            reason = agent.shell_card_reason()
            reasons.append(reason)
            return command if reason else AutoApproved(command)

        command = "cat no-such-file"
        agent, _ = agent_with([command, command], approve, runner=runner, tmp_path=tmp_path)
        agent.run_task("go")
        assert reasons[0] == ""
        assert "refused this command earlier" in reasons[1]
        assert run_steps(agent)[1]["sandbox"] == {"confined": False, "why": "card"}

    def test_a_refusal_does_not_follow_into_another_chat(self, tmp_path):
        agent, _ = agent_with(["cat no-such-file"], auto, runner=RefusingSandbox(),
                              tmp_path=tmp_path)
        agent.run_task("go")
        assert agent._sandbox_refused
        agent.restore_workspace(agent.cwd, [])
        assert not agent._sandbox_refused

    def test_an_empty_log_says_nothing(self, tmp_path):
        agent, _ = agent_with(["echo hi"], auto, runner=PassThroughSandbox(), tmp_path=tmp_path)
        agent.run_task("go")
        assert "sandbox" not in tool_messages(agent.messages)[0]["content"]


class BlockingSandbox(PassThroughSandbox):
    """Its log read waits for the test to release it, and says when it began."""

    def __init__(self, report=None):
        super().__init__(report or sandbox.RefusalReport(
            refusals=[sandbox.Refusal("uptime", "mach-lookup", "com.apple.x")], seen=1))
        self.release = threading.Event()
        self.reading = threading.Event()

    def prepare(self, roots, scratch, protected=(), path_env=None):
        confinement = super().prepare(roots, scratch, protected, path_env)
        owner = self

        class Blocking(type(confinement)):
            def refusals(self, exit_code=None):
                owner.reading.set()
                assert owner.release.wait(10), "the test never released the read"
                return super().refusals(exit_code)

        confinement.__class__ = Blocking
        return confinement


def refusal_records(steps):
    return [s for s in steps if s.get("kind") == "sandbox_refusals"]


class TestRefusalsOfASucceededRunAreReadLater:
    """Owner's decision (2026-10-26): a run that SUCCEEDED returns at once and
    its refusals are read in the background; a FAILED run still waits for
    them, because the note may be what tells the model why."""

    def test_the_result_returns_before_the_read_and_the_record_joins_its_step(
        self, tmp_path
    ):
        runner = BlockingSandbox()
        steps: list[dict] = []
        released_at_call = []
        agent, chat = agent_with(["uptime", "echo second"], auto, runner=runner,
                                 tmp_path=tmp_path, steps=steps)
        original = chat.__call__

        class Gate:
            """The model's SECOND call happens while the first read is still
            blocked — the command's result did not wait for it."""

            def __call__(self, **kw):
                if len(chat.calls) == 1:
                    assert runner.reading.wait(5), "the read never started"
                    assert not refusal_records(steps), "the read finished too early"
                    released_at_call.append(len(chat.calls))
                    runner.release.set()
                return original(**kw)

        agent.chat = Gate()
        agent.run_task("go")
        first = run_steps(agent)[0]
        assert first["sandbox"]["deferred"] is True
        assert "refused" not in first["sandbox"]
        assert released_at_call == [1]
        # The model was not told — the run succeeded.
        assert "kernel refused" not in tool_messages(agent.messages)[0]["content"]
        # The record landed before run_task returned, after its step, and is
        # joined to THAT step although the agent had moved on a call.
        records = refusal_records(steps)
        assert len(records) == 2  # one per succeeded run, none lost
        [record] = [r for r in records if r["call"] == first["call"]]
        assert steps.index(record) > steps.index(first)
        assert record["turn"] == first["turn"]
        assert run_steps(agent)[1]["call"] != first["call"]
        assert record["read"] is True
        assert record["refused"] == [
            {"process": "uptime", "op": "mach-lookup", "target": "com.apple.x"}
        ]
        # …and the same command proposed again is carded, as after a failed run.
        assert "uptime" in agent._sandbox_refused

    def test_a_failed_run_is_still_read_before_it_returns(self, tmp_path):
        steps: list[dict] = []
        runner = RefusingSandbox()
        agent, _ = agent_with(["cat no-such-file"], auto, runner=runner,
                              tmp_path=tmp_path, steps=steps)
        agent.run_task("go")
        step = run_steps(agent)[0]
        assert "deferred" not in step["sandbox"]
        assert step["sandbox"]["refused"]
        assert refusal_records(steps) == []

    def test_a_read_not_done_by_the_end_of_the_task_says_so(self, tmp_path, monkeypatch):
        from aish import agent as agent_module

        monkeypatch.setattr(agent_module, "REFUSAL_SETTLE_S", 0.05)
        runner = BlockingSandbox()
        steps: list[dict] = []
        agent, _ = agent_with(["uptime"], auto, runner=runner, tmp_path=tmp_path,
                              steps=steps)
        agent.run_task("go")
        [record] = refusal_records(steps)
        assert record == {**record, "read": False,
                          "why": agent_module.SANDBOX_READ_NOT_SETTLED}
        runner.release.set()  # the worker now finishes — and must add nothing
        sandbox.deferred_reads()._pool.shutdown(wait=True)
        assert len(refusal_records(steps)) == 1

    def test_the_pending_reads_are_bounded(self):
        reads = sandbox.DeferredReads()
        gate = threading.Event()
        delivered: list[dict] = []

        def slow():
            gate.wait(5)
            return sandbox.RefusalReport()

        for _ in range(reads.MAX_PENDING):
            reads.submit(slow, delivered.append)
        reads.submit(slow, delivered.append)  # one too many
        assert delivered == [
            {"read": False, "why": "too many refusal reads were already waiting"}
        ]
        gate.set()
        reads._pool.shutdown(wait=True)
        assert len(delivered) == reads.MAX_PENDING + 1
        assert sum(1 for d in delivered if d["read"]) == reads.MAX_PENDING

    def test_shutdown_records_every_pending_read_as_not_read(self):
        reads = sandbox.DeferredReads()
        gate = threading.Event()
        delivered: list[dict] = []
        reads.submit(lambda: gate.wait(5) and sandbox.RefusalReport(), delivered.append)
        reads.abandon_all("aish stopped before the read finished")
        assert delivered == [{"read": False, "why": "aish stopped before the read finished"}]
        reads.submit(lambda: sandbox.RefusalReport(), delivered.append)
        assert delivered[-1]["read"] is False  # closed: no new reads
        gate.set()
        reads._pool.shutdown(wait=True)
        assert len(delivered) == 2  # the released worker delivered nothing more

    def test_the_server_abandons_pending_reads_on_shutdown(self):
        import inspect

        from aish import server

        source = inspect.getsource(server.WebServer.shutdown)
        assert "deferred_reads().abandon_all(" in source

    def test_the_record_is_renderless_live_and_on_replay(self):
        from aish import session

        assert "sandbox_refusals" in session.RENDERLESS_STEPS


class LateLogSandbox(PassThroughSandbox):
    """Its read before a FAILED run returns finds nothing (the real one reads
    twice; this stand-in's single call stands for both); every later read
    finds the refusal — the kernel's line landing after the run returned."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def prepare(self, roots, scratch, protected=(), path_env=None):
        confinement = super().prepare(roots, scratch, protected, path_env)
        owner = self

        class Late(type(confinement)):
            def refusals(self, exit_code=None):
                owner.calls += 1
                if owner.calls >= 2:
                    return sandbox.RefusalReport(
                        refusals=[sandbox.Refusal("cat", "file-read-data", "/p/.env")], seen=1)
                return sandbox.RefusalReport()

        confinement.__class__ = Late
        return confinement


class TestAFailedRunWhoseRefusalArrivesLate:
    """Delivery review F1: a failed run whose kernel line lagged both reads left
    the model no note, the record empty, and the card unarmed — so the same
    command, proposed again, ran sandboxed again and failed the same way."""

    def test_a_third_read_in_the_background_puts_it_on_record_and_arms_the_card(
        self, tmp_path
    ):
        runner = LateLogSandbox()
        steps: list[dict] = []
        reasons = []
        agent = None

        def approve(command):
            reasons.append(agent.shell_card_reason())
            return command if reasons[-1] else AutoApproved(command)

        command = "cat no-such-file"
        # Two tasks: the late record is settled at the end of the first.
        agent, chat = agent_with([command], approve, runner=runner, tmp_path=tmp_path,
                                 steps=steps)
        agent.run_task("first")
        step = run_steps(agent)[0]
        assert step["sandbox"]["refused"] == []  # both sync reads missed it
        assert step["sandbox"]["deferred"] is True
        assert "kernel refused" not in tool_messages(agent.messages)[0]["content"]
        [late] = refusal_records(steps)
        assert (late["turn"], late["call"]) == (step["turn"], step["call"])
        assert late["refused"] == [
            {"process": "cat", "op": "file-read-data", "target": "/p/.env"}
        ]
        chat.responses[:0] = [
            model_says(tool_calls=[tool_call("run_command", command=command)]),
            model_says("done"),
        ]
        agent.run_task("again")
        assert reasons[0] == ""
        assert "refused this command earlier" in reasons[1]


class TestTheDeferredReaderEdges:
    def test_an_interrupted_settle_still_marks_its_reads_not_read(self, monkeypatch):
        """Delivery review F2: Ctrl-C inside the wait skipped the marking, and
        the worker later delivered through a log callable that may by then
        point at another chat."""
        reads = sandbox.DeferredReads()
        gate = threading.Event()
        delivered: list[dict] = []
        job = reads.submit(lambda: gate.wait(5) and sandbox.RefusalReport(), delivered.append)

        def interrupted(*_a, **_kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(sandbox, "wait", interrupted)
        with pytest.raises(KeyboardInterrupt):
            reads.settle([job], 5, "not settled")
        assert delivered == [{"read": False, "why": "not settled"}]
        gate.set()
        reads._pool.shutdown(wait=True)
        assert len(delivered) == 1  # the worker that finished later added nothing

    def test_a_log_read_that_timed_out_says_how_long_it_tried(self, monkeypatch):
        """Delivery review F4: the except path recorded log_ms 0."""
        clock = iter([100.0, 110.0])
        monkeypatch.setattr(sandbox.time, "monotonic", lambda: next(clock))

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

        monkeypatch.setattr(sandbox.subprocess, "run", slow)
        report = sandbox.SeatbeltSandbox().read_refusals("abc123", 0.0)
        assert report.read_error
        assert report.log_ms == 10_000


class TestTheWebCard:
    def test_the_card_carries_the_reason_and_the_record_keeps_it(self, tmp_path):
        from aish.server import make_web_approvers

        asked, records = [], []

        class Bridge:
            def emit(self, _event):
                pass

            def ask(self, request):
                request["id"] = "1"
                asked.append(request)
                return {"action": "approve"}

        class LogRef:
            def command(self, command, decision, intent, preview, **kw):
                records.append((command, decision, preview))

        approve, *_ = make_web_approvers(
            Bridge(), LogRef(), tmp_path / "allow.txt", tmp_path / "deny.txt", False,
            lambda: (str(tmp_path), [tmp_path]), lambda _p: "",
            get_card_reason=lambda: "aish's sandbox refused this command earlier",
        )
        assert approve("ls") == "ls"
        assert asked[0]["sandbox_note"] == "aish's sandbox refused this command earlier"
        assert records[-1] == ("ls", "approved", "aish's sandbox refused this command earlier")

    def test_with_nothing_to_say_it_auto_approves_with_the_typed_verdict(self, tmp_path):
        from aish.server import make_web_approvers

        class Bridge:
            def emit(self, _event):
                pass

        class LogRef:
            def command(self, *_a, **_kw):
                pass

        approve, *_ = make_web_approvers(
            Bridge(), LogRef(), tmp_path / "allow.txt", tmp_path / "deny.txt", False,
            lambda: (str(tmp_path), [tmp_path]), lambda _p: "",
        )
        assert isinstance(approve("ls"), AutoApproved)


# ------------------------------------------------- the exploits, for real


@macos_sandbox
class TestTheFourExploitsAreRefusedByTheKernel:
    """Each was auto-approved on shipped code (2026-10-17) and still is: the
    classifier is unchanged. What changed is that it now runs in the kernel's
    sandbox, so the damage does not happen and the refusal is recorded."""

    @pytest.fixture
    def world(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "notes.txt").write_text("b\na\n")
        (project / ".env").write_text("SECRET_VALUE=hunter2\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "victim.txt").write_text("untouched\n")
        return project, outside

    def run(self, project, command, tmp_path, classifier_misses=False):
        """Through the real CLI approver — or, for an exploit the classifier
        has since learned to card, through an approver that waves it through
        anyway: the stand-in for the NEXT spelling nobody has found yet,
        which is the case the kernel boundary exists for."""
        steps: list[dict] = []
        if classifier_misses:
            approve = AutoApproved
        else:
            approve = make_approver(False, tmp_path / "allow.txt", None,
                                    deny_path=tmp_path / "deny.txt",
                                    get_scope=lambda: (str(project), [project.resolve()]))
            assert is_auto_approvable(
                command, (), cwd=str(project), roots=[project.resolve()]
            ), "the classifier must still wave this through — the kernel is what stops it"
        responses = [model_says(tool_calls=[tool_call("run_command", command=command)]),
                     model_says("done")]
        agent, _ = make_agent(responses, approve=approve, cwd=str(project),
                              sandbox_runner=sandbox.SeatbeltSandbox(), step_log=steps.append)
        agent.run_task("go")
        step = next(
            s for s in steps if s.get("kind") == "tool" and s.get("name") == "run_command"
        )
        assert step["sandbox"]["confined"] is True
        self.agent, self.steps = agent, steps
        return step, tool_messages(agent.messages)[0]["content"]

    def refused(self, step):
        """What the kernel refused this run. The step's own sample first; when
        it holds nothing, the log is read again by the run's nonce for a few
        seconds — under a loaded suite the kernel's line can reach the log
        store late, and the record says it is a sample, not the whole log."""
        found = [(r["op"], r["target"]) for r in step["sandbox"]["refused"]]
        deadline = time.monotonic() + 5
        while not found and time.monotonic() < deadline:
            time.sleep(0.5)
            report = sandbox.SeatbeltSandbox().read_refusals(
                step["sandbox"]["nonce"], step["sandbox"]["started"]
            )
            found = [(r.operation, r.target) for r in report.refusals]
        return found

    def test_sort_dash_o_cannot_overwrite_a_file_outside(self, world, tmp_path):
        project, outside = world
        victim = outside / "victim.txt"
        command = f"sort -o{victim} notes.txt"
        # The classifier now cards the glued spelling too (the design review's
        # finding 4) — and the kernel holds even where it would not.
        assert not is_auto_approvable(command, (), cwd=str(project), roots=[project])
        step, result = self.run(project, command, tmp_path, classifier_misses=True)
        assert victim.read_text() == "untouched\n"
        assert ("file-write-data", os.path.realpath(victim)) in self.refused(step)
        # Before the result returned, two reads; under a loaded suite the
        # kernel's line can land after both. Then the model gets no note — the
        # run's step says `deferred` and a third read in the background must
        # put the refusal on record and arm the card for a re-proposal.
        target = ("file-write-data", os.path.realpath(victim))
        if step["sandbox"]["refused"]:
            assert "propose it again" in result
        else:
            assert step["sandbox"]["deferred"] is True
            [late] = [r for r in self.steps if r.get("kind") == "sandbox_refusals"]
            # If all three reads missed it there is nothing more to assert: the
            # record says it is a sample, and the kernel's line itself was
            # confirmed above by polling the log by nonce.
            if target in [(r["op"], r["target"]) for r in late.get("refused", [])]:
                assert command in self.agent._sandbox_refused

    def test_man_pager_cannot_run_a_command(self, world, tmp_path):
        project, _ = world
        # An absolute path: man runs its pager from another directory, so a
        # relative `touch pwned` fails on main for a reason that is not a fence.
        step, _ = self.run(project, f'man -P "touch {project}/pwned" ls', tmp_path)
        assert not (project / "pwned").exists()
        assert ("process-exec*", "/usr/bin/man") in self.refused(step)

    def test_file_dash_capital_c_cannot_write_into_the_project(self, world, tmp_path):
        project, _ = world
        step, _ = self.run(project, "file -C -m m", tmp_path)
        assert not list(project.glob("*.mgc"))
        assert any(op == "file-write-create" for op, _ in self.refused(step))

    def test_a_glob_cannot_read_a_dotenv(self, world, tmp_path):
        project, _ = world
        step, result = self.run(project, "cat .en*", tmp_path)
        assert "hunter2" not in result
        assert ("file-read-data", os.path.realpath(project / ".env")) in self.refused(step)

    def test_an_ordinary_read_still_works(self, world, tmp_path):
        project, _ = world
        step, result = self.run(project, "sort notes.txt | head -1", tmp_path)
        assert "a" in result.splitlines()[0]
        assert step["sandbox"]["deferred"] is True  # succeeded: read after
        assert "kernel refused" not in result


@macos_sandbox
class TestTheRealProfile:
    """Properties of the real profile the exploits do not cover."""

    def run(self, tmp_path, command):
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        confinement = sandbox.SeatbeltSandbox().prepare([project], tmp_path / "scratch")
        assert confinement, confinement
        argv, env = confinement.launch(command)
        done = subprocess.run(argv, env=env, cwd=project, capture_output=True, text=True,
                              start_new_session=True, timeout=30)
        return done, confinement.refusals(exit_code=done.returncode)

    def test_a_program_off_the_list_cannot_start(self, tmp_path):
        marker = tmp_path / "scratch" / "started"
        done, _ = self.run(tmp_path, f"/usr/bin/env /bin/sh -c 'echo > {marker}'")
        assert done.returncode == 126
        assert not marker.exists()
        assert "/usr/bin/env: Operation not permitted" in done.stderr

    def test_it_cannot_signal_a_process_outside_its_group(self, tmp_path):
        victim = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        try:
            self.run(tmp_path, f"kill -TERM {victim.pid}")
            assert victim.poll() is None
        finally:
            victim.kill()
            victim.wait()

    def test_the_environment_carries_no_secrets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "should-not-pass")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        done, _ = self.run(tmp_path, "echo token=$GH_TOKEN sock=$SSH_AUTH_SOCK tmp=$TMPDIR")
        assert "should-not-pass" not in done.stdout
        assert "agent.sock" not in done.stdout
        assert str(os.path.realpath(tmp_path / "scratch")) in done.stdout

    def test_it_writes_to_scratch(self, tmp_path):
        done, report = self.run(tmp_path, f"echo hi > {tmp_path / 'scratch' / 'w.txt'}")
        assert done.returncode == 0, done.stderr
        assert (tmp_path / "scratch" / "w.txt").read_text() == "hi\n"
        assert report.refusals == []

    def test_a_library_in_scratch_cannot_be_loaded(self, tmp_path):
        cc = shutil.which("cc")
        if not cc:
            pytest.skip("no C compiler to build the probe library")
        scratch = tmp_path / "scratch"
        scratch.mkdir(exist_ok=True)
        marker = tmp_path / "loaded"
        src = scratch / "x.c"
        src.write_text(
            "#include <stdio.h>\n__attribute__((constructor)) static void hi(void)"
            f'{{FILE*f=fopen("{marker}","w");if(f)fclose(f);}}\n'
        )
        subprocess.run([cc, "-shared", "-o", str(scratch / "x.so"), str(src)], check=True)
        done, _ = self.run(tmp_path, f"/bin/bash -c 'enable -f {scratch / 'x.so'} x'")
        assert not marker.exists()
        assert "file system sandbox blocked mmap()" in done.stderr
