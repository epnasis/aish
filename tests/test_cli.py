"""Approver flow tests: monkeypatch input() to script the interactive prompt."""

import builtins

from aish.approval import load_prefixes
from aish.cli import make_approver


def scripted_input(monkeypatch, answers):
    answers = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(answers))


def test_yes_approves_once_without_persisting(tmp_path, monkeypatch):
    approve = make_approver(False, tmp_path / "allow.txt", None)
    scripted_input(monkeypatch, ["y"])
    assert approve("brew doctor") == "brew doctor"
    assert load_prefixes(tmp_path / "allow.txt") == []


def test_deny_returns_none(tmp_path, monkeypatch):
    approve = make_approver(False, tmp_path / "allow.txt", None)
    scripted_input(monkeypatch, ["n"])
    assert approve("rm stale.txt") is None


def test_always_persists_per_segment_and_future_auto_approves(tmp_path, monkeypatch):
    allow = tmp_path / "allow.txt"
    approve = make_approver(False, allow, None)
    # 'a', then per-segment: accept 'git status' default, custom for cargo
    scripted_input(monkeypatch, ["a", "", "cargo build"])
    assert approve("git status && cargo build --quiet") == "git status && cargo build --quiet"
    assert load_prefixes(allow) == ["git status", "cargo build"]
    # next time: no prompt at all (input would raise StopIteration if called)
    assert approve("git status && cargo build --release") == "git status && cargo build --release"


def test_always_with_skip_leaves_segment_unvetted(tmp_path, monkeypatch):
    allow = tmp_path / "allow.txt"
    approve = make_approver(False, allow, None)
    scripted_input(monkeypatch, ["a", "s"])
    assert approve("cargo build") == "cargo build"
    assert load_prefixes(allow) == []


def test_edit_returns_edited_command(tmp_path, monkeypatch):
    import aish.cli as cli

    approve = make_approver(False, tmp_path / "allow.txt", None)
    scripted_input(monkeypatch, ["e"])
    monkeypatch.setattr(cli, "edit_line", lambda initial: initial.replace("-x", "-y"))
    assert approve("tool -x") == "tool -y"


def test_ask_all_prompts_even_for_read_only(tmp_path, monkeypatch):
    approve = make_approver(True, tmp_path / "allow.txt", None)
    scripted_input(monkeypatch, ["n"])
    assert approve("ls") is None


def test_read_only_auto_approves_without_input(tmp_path):
    approve = make_approver(False, tmp_path / "allow.txt", None)
    assert approve("ls -la | wc -l && date") == "ls -la | wc -l && date"


def test_load_context_files_reads_cwd_aish_md(tmp_path, monkeypatch):
    from aish.cli import load_context_files

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "no-home")
    (tmp_path / "AISH.md").write_text("host facts here")
    parts = load_context_files(str(tmp_path))
    assert len(parts) == 1
    assert "host facts here" in parts[0]
    assert "AISH.md" in parts[0]


def test_load_context_files_empty_when_absent(tmp_path, monkeypatch):
    from aish.cli import load_context_files

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "no-home")
    assert load_context_files(str(tmp_path)) == []


class TestConfig:
    def test_load_config_parses_toml(self, tmp_path):
        from aish.cli import load_config

        path = tmp_path / "config.toml"
        path.write_text('vi_mode = true\nmodel = "qwen3:8b"\nnum_ctx = 8192\n')
        config = load_config(path)
        assert config["vi_mode"] is True
        assert config["model"] == "qwen3:8b"
        assert config["num_ctx"] == 8192

    def test_missing_config_is_empty(self, tmp_path):
        from aish.cli import load_config

        assert load_config(tmp_path / "nope.toml") == {}

    def test_invalid_toml_warns_and_ignores(self, tmp_path, capsys):
        from aish.cli import load_config

        path = tmp_path / "config.toml"
        path.write_text("vi_mode = [unclosed")
        assert load_config(path) == {}
        assert "ignoring invalid config" in capsys.readouterr().err


class TestUsageContext:
    def test_mentions_all_self_knowledge(self, tmp_path):
        from aish.cli import usage_context

        text = usage_context(
            "qwen3.6:35b-a3b", False, tmp_path / "allow.txt", tmp_path / "state",
            tmp_path / "config.toml",
        )
        for needle in (
            "--resume",
            "!<command>",
            "!cd",
            "always allow",
            "vi_mode = true",
            "AISH.md",
            str(tmp_path / "allow.txt"),
            str(tmp_path / "config.toml"),
            "currently false",
            "qwen3.6:35b-a3b",
        ):
            assert needle in text, needle

    def test_reflects_vi_mode_state(self, tmp_path):
        from aish.cli import usage_context

        text = usage_context("m", True, tmp_path, tmp_path, tmp_path)
        assert "currently true" in text

    def test_grounds_identity_as_local_ollama(self, tmp_path):
        from aish.cli import usage_context

        text = usage_context("qwen3:8b", False, tmp_path, tmp_path, tmp_path)
        lower = text.lower()
        # names its real model, says it's local (not cloud), and that killing
        # the server ends the session
        assert "qwen3:8b" in text
        assert "not a cloud" in lower
        assert "llama-server" in lower
        assert "session ends" in lower


class TestSlashCompleter:
    def completions(self, text):
        from prompt_toolkit.document import Document

        from aish.cli import SLASH_COMMANDS
        from aish.prompt import SlashCompleter

        completer = SlashCompleter(SLASH_COMMANDS)
        return [c.text for c in completer.get_completions(Document(text), None)]

    def test_prefix_completes(self):
        assert self.completions("/r") == ["/resume"]
        assert self.completions("/q") == ["/quit"]

    def test_bare_slash_lists_all(self):
        from aish.cli import SLASH_COMMANDS

        assert self.completions("/") == list(SLASH_COMMANDS)

    def test_only_at_line_start_first_word(self):
        assert self.completions("hello /r") == []
        assert self.completions("/resume extra") == []


class TestSlashCommands:
    def fake_agent(self):
        from aish.agent import Agent

        return Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)

    def logref(self, tmp_path):
        from aish.cli import LogRef
        from aish.session import SessionLog

        return LogRef(SessionLog.new(tmp_path))

    def test_quit_and_exit(self, tmp_path):
        from aish.cli import handle_slash

        agent, logref = self.fake_agent(), self.logref(tmp_path)
        assert handle_slash("/quit", agent, logref, tmp_path) == "exit"
        assert handle_slash("/exit", agent, logref, tmp_path) == "exit"

    def test_clear_resets_conversation_and_swaps_log(self, tmp_path):
        from aish.cli import handle_slash

        agent, logref = self.fake_agent(), self.logref(tmp_path)
        old_path = logref.log.path
        agent.messages.append({"role": "user", "content": "old"})
        assert handle_slash("/clear", agent, logref, tmp_path) == "handled"
        assert len(agent.messages) == 1
        assert agent.messages[0]["role"] == "system"
        assert logref.log.path != old_path

    def test_resume_loads_previous_replays_and_relogs(self, tmp_path, capsys):
        from aish.cli import handle_slash
        from aish.session import SessionLog

        previous = SessionLog(tmp_path / "session-20260101-000000.jsonl")
        previous.message({"role": "user", "content": "old question"})
        previous.message({"role": "assistant", "content": "old answer"})

        agent, logref = self.fake_agent(), self.logref(tmp_path)
        assert handle_slash("/resume", agent, logref, tmp_path) == "handled"
        assert any(
            isinstance(m, dict) and m.get("content") == "old question"
            for m in agent.messages
        )
        out = capsys.readouterr().out
        assert "old question" in out and "old answer" in out  # replayed on screen
        # re-logged: current session file is self-contained
        assert "old question" in logref.log.path.read_text()

    def test_resume_with_no_previous(self, tmp_path, capsys):
        from aish.cli import handle_slash

        agent, logref = self.fake_agent(), self.logref(tmp_path)
        assert handle_slash("/resume", agent, logref, tmp_path) == "handled"
        assert "no earlier session" in capsys.readouterr().out

    def test_unknown_command_suggests_help(self, tmp_path, capsys):
        from aish.cli import handle_slash

        agent, logref = self.fake_agent(), self.logref(tmp_path)
        assert handle_slash("/bogus", agent, logref, tmp_path) == "handled"
        assert "/help" in capsys.readouterr().out

    def test_help_lists_commands(self, tmp_path, capsys):
        from aish.cli import handle_slash

        agent, logref = self.fake_agent(), self.logref(tmp_path)
        handle_slash("/help", agent, logref, tmp_path)
        out = capsys.readouterr().out
        for cmd in ("/resume", "/new", "/clear", "/quit"):
            assert cmd in out


def test_replay_history_shows_roles_and_truncates_tools(capsys):
    from aish.cli import replay_history

    replay_history(
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": ""},  # tool-call-only: skipped
            {"role": "tool", "content": "\n".join(f"l{i}" for i in range(10))},
            {"role": "assistant", "content": "answer"},
        ]
    )
    out = capsys.readouterr().out
    assert "question" in out and "answer" in out
    assert "l0" in out and "l9" not in out
    assert "more lines" in out


def test_resume_skips_empty_sessions(tmp_path, capsys):
    from aish.agent import Agent
    from aish.cli import LogRef, handle_slash
    from aish.session import SessionLog

    old = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    old.message({"role": "user", "content": "real content"})
    SessionLog(tmp_path / "session-20260102-000000-000000.jsonl")  # newer but empty

    agent = Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)
    logref = LogRef(SessionLog.new(tmp_path))
    handle_slash("/resume", agent, logref, tmp_path)
    assert "real content" in capsys.readouterr().out


def two_session_setup(tmp_path):
    from aish.agent import Agent
    from aish.cli import LogRef
    from aish.session import SessionLog

    older = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    older.message({"role": "user", "content": "from january"})
    newer = SessionLog(tmp_path / "session-20260201-000000-000000.jsonl")
    newer.message({"role": "user", "content": "from february"})

    agent = Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)
    return agent, LogRef(SessionLog.new(tmp_path))


def test_repeated_resume_walks_back_through_sessions(tmp_path, capsys, monkeypatch):
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    resumed = set()

    scripted_input(monkeypatch, [""])  # Enter = latest
    handle_slash("/resume", agent, logref, tmp_path, resumed)
    assert "from february" in capsys.readouterr().out
    handle_slash("/resume", agent, logref, tmp_path, resumed)  # one left: auto-loads
    assert "from january" in capsys.readouterr().out
    handle_slash("/resume", agent, logref, tmp_path, resumed)
    assert "no earlier session" in capsys.readouterr().out


def test_resume_picker_lists_slugs_and_selects_by_number(tmp_path, capsys, monkeypatch):
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    scripted_input(monkeypatch, ["2"])
    handle_slash("/resume", agent, logref, tmp_path, set())
    out = capsys.readouterr().out
    assert "1." in out and "2." in out  # numbered picker
    assert "from february" in out and "from january" in out  # slugs listed
    assert "resumed" in out and "session-20260101" in out  # picked #2 = older


def test_resume_with_numeric_arg_skips_picker(tmp_path, capsys):
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    handle_slash("/resume 2", agent, logref, tmp_path, set())
    out = capsys.readouterr().out
    assert "resume which?" not in out
    assert "session-20260101" in out


def test_resume_picker_cancel(tmp_path, capsys, monkeypatch):
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    scripted_input(monkeypatch, ["q"])
    handle_slash("/resume", agent, logref, tmp_path, set())
    assert "cancelled" in capsys.readouterr().out
    assert len(agent.messages) == 1  # nothing loaded


def test_resume_invalid_number(tmp_path, capsys):
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    handle_slash("/resume 9", agent, logref, tmp_path, set())
    assert "no such session" in capsys.readouterr().out


def test_session_info_slug_and_time(tmp_path):
    from aish.session import SessionLog

    log = SessionLog(tmp_path / "session-20260712-232744-000000.jsonl")
    log.message({"role": "user", "content": "  count the .py   files\nunder ~/dev  "})
    info = SessionLog.info(log.path)
    assert info.title == "count the .py files under ~/dev"
    assert info.when == "Jul 12 23:27"
    assert info.count == 1


def test_session_info_bang_first_and_truncation(tmp_path):
    from aish.session import SessionLog

    log = SessionLog(tmp_path / "session-20260712-000000-000000.jsonl")
    log.message({"role": "user", "content": "[I ran `git status` myself; output:]\nclean"})
    assert SessionLog.info(log.path).title == "! git status"

    long_log = SessionLog(tmp_path / "session-20260712-000001-000000.jsonl")
    long_log.message({"role": "user", "content": "x" * 200})
    title = SessionLog.info(long_log.path).title
    assert len(title) == 60 and title.endswith("…")


def test_session_info_none_for_empty(tmp_path):
    from aish.session import SessionLog

    empty = SessionLog(tmp_path / "session-20260712-000002-000000.jsonl")
    assert SessionLog.info(empty.path) is None


def test_clear_clears_screen(tmp_path, capsys):
    from aish.agent import Agent
    from aish.cli import LogRef, handle_slash
    from aish.session import SessionLog

    agent = Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)
    logref = LogRef(SessionLog.new(tmp_path))
    handle_slash("/clear", agent, logref, tmp_path)
    assert "\033[2J" in capsys.readouterr().out


def test_skills_context_lists_skills(tmp_path, monkeypatch):
    from aish import skills as skills_module
    from aish.cli import skills_context

    monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", tmp_path / "none")
    assert skills_context(str(tmp_path)) == ""

    local = tmp_path / ".aish" / "skills"
    local.mkdir(parents=True)
    (local / "demo.md").write_text("---\nname: demo\ndescription: demo things\n---\nbody")
    text = skills_context(str(tmp_path))
    assert "read_skill" in text
    assert "- demo: demo things" in text


class TestDenylistApprover:
    def test_blocked_without_prompting(self, tmp_path, capsys):
        from aish.approval import Blocked
        from aish.cli import make_approver

        approve = make_approver(False, tmp_path / "allow.txt", None, tmp_path / "deny.txt")
        verdict = approve("rm -rf /")  # input() would raise if called
        assert isinstance(verdict, Blocked)
        assert "blocked" in capsys.readouterr().out

    def test_allowlist_cannot_bypass_denylist(self, tmp_path):
        from aish.approval import Blocked, save_prefix
        from aish.cli import make_approver

        allow = tmp_path / "allow.txt"
        save_prefix(allow, "rm")
        approve = make_approver(False, allow, None, tmp_path / "deny.txt")
        assert isinstance(approve("rm -rf /tmp/x"), Blocked)

    def test_destructive_warning_shown(self, tmp_path, capsys, monkeypatch):
        from aish.cli import make_approver

        approve = make_approver(False, tmp_path / "allow.txt", None, tmp_path / "deny.txt")
        scripted_input(monkeypatch, ["n"])
        approve("mv /etc/hosts /tmp/")
        assert "⚠ destructive" in capsys.readouterr().out


class TestModelAndJobs:
    def test_model_switch(self, tmp_path, capsys):
        from aish.agent import Agent
        from aish.cli import LogRef, handle_slash
        from aish.session import SessionLog

        agent = Agent(model="old", approve=lambda _c: None, client_chat=lambda **_k: None)
        logref = LogRef(SessionLog.new(tmp_path))
        handle_slash("/model qwen3:8b", agent, logref, tmp_path)
        assert agent.model == "qwen3:8b"
        handle_slash("/model", agent, logref, tmp_path)
        assert "qwen3:8b" in capsys.readouterr().out

    def test_jobs_empty(self, tmp_path, capsys, monkeypatch):
        from aish import tools
        from aish.agent import Agent
        from aish.cli import LogRef, handle_slash
        from aish.session import SessionLog

        monkeypatch.setattr(tools, "JOBS", [])
        agent = Agent(model="m", approve=lambda _c: None, client_chat=lambda **_k: None)
        handle_slash("/jobs", agent, LogRef(SessionLog.new(tmp_path)), tmp_path)
        assert "no background jobs" in capsys.readouterr().out


def test_lessons_file_loaded_into_context(tmp_path, monkeypatch):
    from aish.cli import load_context_files

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "no-home")
    lessons = tmp_path / "lessons.md"
    lessons.write_text("- macOS ps: ps aux -m\n")
    parts = load_context_files(str(tmp_path), lessons)
    assert any("lessons you saved" in p and "ps aux -m" in p for p in parts)


def test_darwin_note_has_ps_guidance():
    import sys

    from aish.agent import system_prompt

    if sys.platform == "darwin":
        assert "ps aux -r" in system_prompt() or "ps aux -m" in system_prompt()
