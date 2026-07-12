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
    assert approve("rm -rf /") is None


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


def test_repeated_resume_walks_back_through_sessions(tmp_path, capsys):
    from aish.agent import Agent
    from aish.cli import LogRef, handle_slash
    from aish.session import SessionLog

    older = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    older.message({"role": "user", "content": "from january"})
    newer = SessionLog(tmp_path / "session-20260201-000000-000000.jsonl")
    newer.message({"role": "user", "content": "from february"})

    agent = Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)
    logref = LogRef(SessionLog.new(tmp_path))
    resumed = set()

    handle_slash("/resume", agent, logref, tmp_path, resumed)
    assert "from february" in capsys.readouterr().out
    handle_slash("/resume", agent, logref, tmp_path, resumed)
    assert "from january" in capsys.readouterr().out
    handle_slash("/resume", agent, logref, tmp_path, resumed)
    assert "no earlier session" in capsys.readouterr().out


def test_clear_clears_screen(tmp_path, capsys):
    from aish.agent import Agent
    from aish.cli import LogRef, handle_slash
    from aish.session import SessionLog

    agent = Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)
    logref = LogRef(SessionLog.new(tmp_path))
    handle_slash("/clear", agent, logref, tmp_path)
    assert "\033[2J" in capsys.readouterr().out
