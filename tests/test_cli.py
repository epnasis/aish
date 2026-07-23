"""Approver flow tests: monkeypatch input() to script the interactive prompt."""

import builtins

from aish.approval import load_prefixes
from aish.cli import make_approver, make_read_approver


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


def test_session_allow_auto_approves_without_persisting(tmp_path, monkeypatch):
    allow = tmp_path / "allow.txt"
    approve = make_approver(False, allow, None)
    # 's', then accept the suggested prefix for each unvetted segment
    scripted_input(monkeypatch, ["s", ""])
    assert approve("cargo build --quiet") == "cargo build --quiet"
    assert load_prefixes(allow) == []  # nothing written to disk
    # same prefix auto-approves for the rest of the session (no prompt)
    assert approve("cargo build --release") == "cargo build --release"


def test_session_allow_is_per_approver_not_global(tmp_path, monkeypatch):
    allow = tmp_path / "allow.txt"
    approve = make_approver(False, allow, None)
    scripted_input(monkeypatch, ["s", ""])
    assert approve("cargo build") == "cargo build"
    # a fresh approver (new session) starts clean and prompts again
    fresh = make_approver(False, allow, None)
    scripted_input(monkeypatch, ["n"])
    assert fresh("cargo build") is None


def test_trust_dir_approves_and_widens_roots(tmp_path, monkeypatch):
    """'t' on a root-escaping command trusts the escaping directory: the
    command runs AND allowlisted commands there auto-approve afterwards."""
    root = tmp_path / "project"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    roots = [root]
    trusted = []

    def trust_dir(path):
        trusted.append(path)
        roots.append(other)
        return f"[trusted for this session: {path}]"

    approve = make_approver(
        False, tmp_path / "allow.txt", None,
        get_scope=lambda: (str(root), roots), trust_dir=trust_dir,
    )
    scripted_input(monkeypatch, ["t"])
    assert approve(f"ls {other}") == f"ls {other}"
    assert trusted == [str(other)]
    # the widened roots now auto-approve without any prompt
    assert approve(f"ls {other}") == f"ls {other}"


def test_trust_option_absent_inside_roots(tmp_path, monkeypatch, capsys):
    """No escape → no 't' offer; a stray 't' answer counts as deny."""
    approve = make_approver(
        False, tmp_path / "allow.txt", None,
        get_scope=lambda: (str(tmp_path), [tmp_path]),
        trust_dir=lambda _p: "unreachable",
    )
    scripted_input(monkeypatch, ["t"])
    assert approve("brew doctor") is None
    assert "t(rust dir)" not in capsys.readouterr().out


def test_read_approver_trust_widens_roots(tmp_path, monkeypatch):
    trusted = []
    approve_read = make_read_approver(
        None, trust_dir=lambda p: trusted.append(p) or f"[trusted: {p}]"
    )
    scripted_input(monkeypatch, ["t"])
    assert approve_read(str(tmp_path / "elsewhere" / "notes.txt"), "outside") is True
    assert trusted == [str(tmp_path / "elsewhere")]
    # sensitive reads never offer trust — 't' is just not-yes
    scripted_input(monkeypatch, ["t"])
    assert approve_read(str(tmp_path / ".env"), "sensitive") is False


def test_slash_prefix_resolves_when_unambiguous(tmp_path, capsys):
    from aish.cli import LogRef, handle_slash
    from aish.session import SessionLog

    log = SessionLog.new(tmp_path)
    logref = LogRef(log)
    agent = type("A", (), {"reset": lambda self: None, "cwd": str(tmp_path),
                           "roots": [tmp_path], "model": "m"})()
    assert handle_slash("/ex", agent, logref, tmp_path) == "exit"
    assert handle_slash("/c", agent, logref, tmp_path) == "handled"  # ambiguous
    assert "ambiguous" in capsys.readouterr().out


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


def test_edited_command_is_re_checked_against_denylist(tmp_path, monkeypatch):
    import aish.cli as cli
    from aish.approval import Blocked

    approve = make_approver(False, tmp_path / "allow.txt", None, tmp_path / "deny.txt")
    scripted_input(monkeypatch, ["e"])
    # user edits a benign (non-auto-approved) command into an unrecoverable one
    monkeypatch.setattr(cli, "edit_line", lambda _initial: "rm -rf /")
    result = approve("brew doctor")
    assert isinstance(result, Blocked)


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
        assert self.completions("/re") == ["/rename", "/resume"]
        assert self.completions("/res") == ["/resume"]
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

    def test_new_session_records_current_model(self, tmp_path):
        import json

        from aish.cli import handle_slash

        agent, logref = self.fake_agent(), self.logref(tmp_path)
        handle_slash("/new", agent, logref, tmp_path)
        # The model note is lazy — it lands with the first real activity.
        logref.message({"role": "user", "content": "hi"})
        records = [json.loads(line) for line in logref.log.path.read_text().splitlines()]
        assert records[0]["kind"] == "model" and records[0]["model"] == "fake"

    def test_session_row_shows_model_and_omits_when_absent(self, tmp_path):
        from aish.cli import session_row
        from aish.session import SessionInfo

        with_model = SessionInfo(
            path=tmp_path, when="2026-01-01 00:00", count=3, title="fix build", model="qwen3:8b"
        )
        assert session_row(with_model) == "2026-01-01 00:00 ·   3 msgs · qwen3:8b · fix build"
        legacy = SessionInfo(path=tmp_path, when="2026-01-01 00:00", count=3, title="fix build")
        assert session_row(legacy) == "2026-01-01 00:00 ·   3 msgs · fix build"

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


def test_resume_search_loads_single_match_directly(tmp_path, capsys):
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    handle_slash("/resume january", agent, logref, tmp_path, set())
    out = capsys.readouterr().out
    assert "resume which?" not in out  # one match: no picker needed
    assert "resumed" in out and "session-20260101" in out


def test_resume_search_picker_defaults_to_best_match(tmp_path, capsys, monkeypatch):
    from aish.cli import handle_slash
    from aish.session import SessionLog

    agent, logref = two_session_setup(tmp_path)
    third = SessionLog(tmp_path / "session-20260301-000000-000000.jsonl")
    third.message({"role": "user", "content": "from january too"})

    scripted_input(monkeypatch, [""])  # Enter = best match
    handle_slash("/resume from january", agent, logref, tmp_path, set())
    out = capsys.readouterr().out
    # ranked picker: exact title first despite being oldest, then title phrase
    assert out.index("from january\n") < out.index("from january too")
    assert "resumed" in out and "session-20260101" in out  # Enter took the best match


def test_resume_search_no_match(tmp_path, capsys):
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    handle_slash("/resume nonexistent topic", agent, logref, tmp_path, set())
    assert "no session matches" in capsys.readouterr().out
    assert len(agent.messages) == 1  # nothing loaded


def test_delete_picker_confirms_and_unlinks(tmp_path, capsys, monkeypatch):
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    scripted_input(monkeypatch, ["2", "y"])  # pick the older session, confirm
    handle_slash("/delete", agent, logref, tmp_path, set())
    out = capsys.readouterr().out
    assert "1." in out and "2." in out  # same numbered picker as /resume
    assert "deleted session-20260101" in out
    assert not (tmp_path / "session-20260101-000000-000000.jsonl").exists()
    assert (tmp_path / "session-20260201-000000-000000.jsonl").exists()


def test_delete_default_answer_keeps_file(tmp_path, capsys, monkeypatch):
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    scripted_input(monkeypatch, ["1", ""])  # Enter at the y/N confirm = No
    handle_slash("/delete", agent, logref, tmp_path, set())
    assert "cancelled" in capsys.readouterr().out
    assert (tmp_path / "session-20260201-000000-000000.jsonl").exists()


def test_delete_prefix_and_numeric_arg_skip_picker(tmp_path, capsys, monkeypatch):
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    scripted_input(monkeypatch, ["y"])
    handle_slash("/del 2", agent, logref, tmp_path, set())
    out = capsys.readouterr().out
    assert "1." not in out  # numeric arg: no numbered list shown
    assert "deleted session-20260101" in out
    assert not (tmp_path / "session-20260101-000000-000000.jsonl").exists()


def test_delete_excludes_current_session(tmp_path, capsys):
    from aish.agent import Agent
    from aish.cli import LogRef, handle_slash
    from aish.session import SessionLog

    agent = Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)
    logref = LogRef(SessionLog.new(tmp_path))
    logref.message({"role": "user", "content": "live conversation"})
    handle_slash("/delete", agent, logref, tmp_path, set())
    assert "no earlier session to delete" in capsys.readouterr().out
    assert logref.log.path.exists()


def test_resume_uses_live_picker_when_interactive(tmp_path, capsys, monkeypatch):
    import aish.cli as cli
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)

    class FakeBox:
        def pick(self, search, initial="", render=str):
            assert initial == "jan"  # /resume <text> pre-fills the filter
            assert [i.title for i in search("")] == ["from february", "from january"]
            assert "from january" in render(search("january")[0])
            return search("january")[0]

    monkeypatch.setattr(cli, "_box", FakeBox())
    handle_slash("/resume jan", agent, logref, tmp_path, set())
    out = capsys.readouterr().out
    assert "resumed" in out and "session-20260101" in out


def test_resume_live_picker_cancel(tmp_path, capsys, monkeypatch):
    import aish.cli as cli
    from aish.cli import handle_slash

    agent, logref = two_session_setup(tmp_path)
    monkeypatch.setattr(
        cli,
        "_box",
        type("Box", (), {"pick": lambda self, s, initial="", render=str: None})(),
    )
    handle_slash("/resume", agent, logref, tmp_path, set())
    assert "cancelled" in capsys.readouterr().out
    assert len(agent.messages) == 1  # nothing loaded


def test_resume_lists_all_sessions_not_just_ten(tmp_path, capsys, monkeypatch):
    from aish.agent import Agent
    from aish.cli import LogRef, handle_slash
    from aish.session import SessionLog

    for day in range(1, 13):
        log = SessionLog(tmp_path / f"session-202601{day:02d}-000000-000000.jsonl")
        log.message({"role": "user", "content": f"task number {day}"})

    agent = Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)
    logref = LogRef(SessionLog.new(tmp_path))
    scripted_input(monkeypatch, ["q"])
    handle_slash("/resume", agent, logref, tmp_path, set())
    out = capsys.readouterr().out
    assert " 12." in out and "task number 1" in out  # oldest still listed
    assert "2026-01-12" in out  # full start date with year


def test_session_info_slug_and_time(tmp_path):
    from aish.session import SessionLog

    log = SessionLog(tmp_path / "session-20260712-232744-000000.jsonl")
    log.message({"role": "user", "content": "  count the .py   files\nunder ~/dev  "})
    info = SessionLog.info(log.path)
    assert info.title == "count the .py files under ~/dev"
    assert info.when == "2026-07-12 23:27"
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

    empty = tmp_path / "session-20260712-000002-000000.jsonl"
    empty.touch()  # legacy blank file from before lazy creation
    assert SessionLog.info(empty) is None


def test_clear_clears_screen(tmp_path, capsys):
    from aish.agent import Agent
    from aish.cli import LogRef, handle_slash
    from aish.session import SessionLog

    agent = Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)
    logref = LogRef(SessionLog.new(tmp_path))
    handle_slash("/clear", agent, logref, tmp_path)
    assert "\033[2J" in capsys.readouterr().out


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


class TestModelPicker:
    def test_rank_models_tiers(self):
        from aish.cli import rank_models

        models = [("qwen3:8b", "local · 5 GB"), ("gemini", "cloud"), ("gemini-embed", "cloud")]
        assert [m[0] for m in rank_models(models, "")] == ["qwen3:8b", "gemini", "gemini-embed"]
        assert rank_models(models, "gemini")[0][0] == "gemini"  # exact beats prefix
        assert rank_models(models, "gem")[0][0] == "gemini"  # prefix, list order on ties
        assert rank_models(models, "local")[0][0] == "qwen3:8b"  # substring in description
        assert rank_models(models, "qwn3")[0][0] == "qwen3:8b"  # fuzzy typo
        assert rank_models(models, "zzz") == []

    def test_rank_models_multi_word_queries(self):
        from aish.cli import rank_models

        models = [
            ("gemini:gemini-3.5-pro", "cloud · Google Gemini"),
            ("gemini:gemini-3.5-flash", "cloud · Google Gemini"),
            ("qwen3:8b", "local · 5 GB"),
        ]
        # words match independently anywhere in name+description
        assert rank_models(models, "gem pro")[0][0] == "gemini:gemini-3.5-pro"
        # each word may be a typo (per-word fuzzy against name tokens)
        assert rank_models(models, "gemni flsh")[0][0] == "gemini:gemini-3.5-flash"
        # a word matching nothing kills the row
        assert all("qwen" not in name for name, _ in rank_models(models, "gem zzz"))

    def test_rank_models_offers_typed_cloud_model(self):
        from aish.cli import rank_models

        models = [("gemini", "cloud · default gemini-3.5-flash"), ("qwen3:8b", "local")]
        results = rank_models(models, "gemini:gemini-3.5-pro")
        assert results[0][0] == "gemini:gemini-3.5-pro"  # exact typed model on top
        assert "Gemini" in results[0][1]
        assert results[0][0] not in [name for name, _ in models]  # synthesized

        # Uppercase provider still yields a switchable name
        assert rank_models(models, "Gemini:pro-x")[0][0] == "gemini:pro-x"
        # claude-max:opus is a valid restart target — offer it too
        assert rank_models(models, "claude-max:opus")[0][0] == "claude-max:opus"
        # bare provider and ollama-style colon names never synthesize
        assert all(":" not in name for name, _ in rank_models(models, "gemini"))
        assert [name for name, _ in rank_models(models, "qwen3:8b")] == ["qwen3:8b"]
        # provider prefix without a model name doesn't synthesize either
        assert rank_models(models, "gemini:")[0][0] == "gemini"

    def test_available_models_merges_local_and_cloud(self, monkeypatch):
        import sys
        from types import SimpleNamespace

        from aish.cli import available_models

        fake_ollama = SimpleNamespace(
            list=lambda: SimpleNamespace(
                models=[SimpleNamespace(model="qwen3:8b", size=5_000_000_000)]
            )
        )
        monkeypatch.setitem(sys.modules, "ollama", fake_ollama)
        agent = SimpleNamespace(model="qwen3:8b", provider="ollama")
        models = dict(available_models(agent))
        assert "current" in models["qwen3:8b"]
        assert "gemini" in models and "openai" in models and "claude" in models
        assert "claude-max" in models

    def test_available_models_when_ollama_down(self, monkeypatch):
        import sys
        from types import SimpleNamespace

        from aish.cli import available_models

        def boom():
            raise ConnectionError("ollama not running")

        monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(list=boom))
        agent = SimpleNamespace(model="x", provider="gemini")
        models = available_models(agent)
        assert [name for name, _ in models] == ["gemini", "openai", "claude", "claude-max"]
        assert "current" in dict(models)["gemini"]

    def test_cloud_model_catalog_fetches_and_caches(self, tmp_path, monkeypatch):
        import aish.cli as cli

        def fake_list(name):
            if name == "gemini":
                return ["gemini-3.5-pro", "gemini-3.5-flash"]
            raise cli.backends.BackendError("no key")

        monkeypatch.setattr(cli.backends, "list_models", fake_list)
        catalog = cli.cloud_model_catalog(tmp_path)
        assert catalog == {"gemini": ["gemini-3.5-pro", "gemini-3.5-flash"]}

        def explode(_name):
            raise AssertionError("second call must be served from cache")

        monkeypatch.setattr(cli.backends, "list_models", explode)
        assert cli.cloud_model_catalog(tmp_path) == catalog

    def test_available_models_includes_fetched_catalog(self, tmp_path, monkeypatch):
        import sys
        from types import SimpleNamespace

        import aish.cli as cli

        def boom():
            raise ConnectionError("ollama not running")

        monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(list=boom))
        monkeypatch.setattr(
            cli, "cloud_model_catalog", lambda _s: {"gemini": ["gemini-3.5-pro"]}
        )
        agent = SimpleNamespace(model="gemini-3.5-pro", provider="gemini")
        models = dict(cli.available_models(agent, tmp_path))
        assert "gemini:gemini-3.5-pro" in models
        assert "current" in models["gemini:gemini-3.5-pro"]

    def test_model_no_arg_opens_picker_and_switches(self, tmp_path, capsys, monkeypatch):
        import aish.cli as cli
        from aish.agent import Agent
        from aish.cli import LogRef, handle_slash
        from aish.session import SessionLog

        agent = Agent(model="old", approve=lambda _c: None, client_chat=lambda **_k: None)
        logref = LogRef(SessionLog.new(tmp_path))
        monkeypatch.setattr(
            cli, "available_models", lambda _a, _s=None: [("qwen3:8b", "local · 5 GB")]
        )

        class FakeBox:
            def pick(self, search, initial="", render=str):
                assert "qwen3:8b" in render(search("")[0])
                return search("")[0]

        monkeypatch.setattr(cli, "_box", FakeBox())
        handle_slash("/model", agent, logref, tmp_path)
        assert agent.model == "qwen3:8b"
        assert "switched" in capsys.readouterr().out

    def test_model_picker_cancel_keeps_model(self, tmp_path, capsys, monkeypatch):
        import aish.cli as cli
        from aish.agent import Agent
        from aish.cli import LogRef, handle_slash
        from aish.session import SessionLog

        agent = Agent(model="old", approve=lambda _c: None, client_chat=lambda **_k: None)
        logref = LogRef(SessionLog.new(tmp_path))
        monkeypatch.setattr(
            cli, "available_models", lambda _a, _s=None: [("qwen3:8b", "local")]
        )
        monkeypatch.setattr(
            cli,
            "_box",
            type("Box", (), {"pick": lambda self, s, initial="", render=str: None})(),
        )
        handle_slash("/model", agent, logref, tmp_path)
        assert agent.model == "old"
        assert "cancelled" in capsys.readouterr().out


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


def test_lessons_surface_in_memory_index_not_bulk_context(tmp_path, monkeypatch):
    """Lessons are no longer dumped wholesale into the prompt: they reach the
    model through the capped Memory index (and recall) instead."""
    from aish.cli import load_context_files
    from aish.skills import knowledge_index

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "no-home")
    lessons = tmp_path / "lessons.md"
    lessons.write_text("- macOS ps: ps aux -m\n")
    assert load_context_files(str(tmp_path)) == []
    index = knowledge_index(str(tmp_path), lessons)
    assert "Memory" in index
    assert "ps aux -m" in index


def test_darwin_note_has_ps_guidance():
    import sys

    from aish.agent import system_prompt

    if sys.platform == "darwin":
        assert "ps aux -r" in system_prompt() or "ps aux -m" in system_prompt()


class TestLiveTimer:
    def test_paints_immediately_and_erases_on_stop(self, capsys):
        from aish.cli import LiveTimer

        timer = LiveTimer()
        timer.start("thinking")
        timer.stop()
        out = capsys.readouterr().out
        assert "✻ thinking… 0s" in out
        assert out.endswith("\r\033[K")  # line erased: safe to print after stop()

    def test_stop_is_idempotent_and_start_replaces(self, capsys):
        from aish.cli import LiveTimer

        timer = LiveTimer()
        timer.stop()  # no-op before any start
        timer.start("web_search")
        timer.start("read_url")  # implicit stop of the previous phase
        timer.stop()
        timer.stop()
        out = capsys.readouterr().out
        assert "✻ web_search… 0s" in out
        assert "✻ read_url… 0s" in out

    def test_token_count_appears_after_add_tokens(self, capsys):
        from aish.cli import LiveTimer

        timer = LiveTimer()
        timer.start("thinking")
        timer.add_tokens(8100)
        timer._paint()  # deterministic repaint; the ticker thread does this every 0.25s
        timer.stop()
        out = capsys.readouterr().out
        assert "✻ thinking… 0s · ↓ 8.1k tokens" in out

    def test_println_erases_ticker_frame_first(self, capsys):
        from aish.cli import LiveTimer

        timer = LiveTimer()
        timer.start("2 parallel lookups")
        timer.println("  ✓ web_search 2.8s")
        timer.stop()
        out = capsys.readouterr().out
        assert "\r\033[K  ✓ web_search 2.8s\n" in out  # never glued to the ✻ frame


class TestModelSave:
    def _agent(self):
        from types import SimpleNamespace

        from aish.agent import Agent

        agent = Agent(model="old", approve=lambda _c: None, client_chat=lambda **_k: None)
        return agent, SimpleNamespace

    def test_save_creates_config_file(self, tmp_path):
        import tomllib

        from aish.cli import save_default_model

        config = tmp_path / "sub" / "config.toml"
        assert save_default_model(config, "qwen3:8b") is None
        assert tomllib.loads(config.read_text())["model"] == "qwen3:8b"

    def test_save_replaces_line_keeps_comments_and_keys(self, tmp_path):
        import tomllib

        from aish.cli import save_default_model

        config = tmp_path / "config.toml"
        config.write_text(
            "# my config\n"
            'model = "old-model"  # old choice\n'
            "num_ctx = 8192\n"
            "[extras]\n"
            'model = "table-scoped, must survive"\n'
        )
        assert save_default_model(config, "gemini:gemini-3.5-pro") is None
        text = config.read_text()
        assert "# my config" in text
        assert "num_ctx = 8192" in text
        assert 'model = "table-scoped, must survive"' in text
        assert tomllib.loads(text)["model"] == "gemini:gemini-3.5-pro"

    def test_save_inserts_before_first_table(self, tmp_path):
        import tomllib

        from aish.cli import save_default_model

        config = tmp_path / "config.toml"
        config.write_text("num_ctx = 8192\n[extras]\nfoo = 1\n")
        assert save_default_model(config, "qwen3:8b") is None
        parsed = tomllib.loads(config.read_text())
        assert parsed["model"] == "qwen3:8b"
        assert parsed["extras"]["foo"] == 1

    def test_save_rejects_toml_breaking_name(self, tmp_path):
        from aish.cli import save_default_model

        config = tmp_path / "config.toml"
        error = save_default_model(config, 'bad"\nrogue = true')
        assert error is not None
        assert not config.exists()

    def test_model_spec_roundtrip(self):
        from types import SimpleNamespace

        from aish.cli import model_spec

        assert model_spec(SimpleNamespace(model="qwen3:8b", provider="ollama")) == "qwen3:8b"
        assert (
            model_spec(SimpleNamespace(model="gemini-3.5-pro", provider="gemini"))
            == "gemini:gemini-3.5-pro"
        )
        assert model_spec(SimpleNamespace(model="", provider="claude-max")) == "claude-max"

    def test_model_switch_and_save(self, tmp_path, capsys):
        import tomllib

        from aish.cli import LogRef, handle_slash
        from aish.session import SessionLog

        agent, _ = self._agent()
        logref = LogRef(SessionLog.new(tmp_path))
        config = tmp_path / "config.toml"
        handle_slash("/model qwen3:8b --save", agent, logref, tmp_path, config_path=config)
        assert agent.model == "qwen3:8b"
        assert tomllib.loads(config.read_text())["model"] == "qwen3:8b"
        assert "startup default" in capsys.readouterr().out

    def test_model_save_current_without_switch(self, tmp_path, capsys):
        import tomllib

        from aish.cli import LogRef, handle_slash
        from aish.session import SessionLog

        agent, _ = self._agent()
        logref = LogRef(SessionLog.new(tmp_path))
        config = tmp_path / "config.toml"
        handle_slash("/model --save", agent, logref, tmp_path, config_path=config)
        assert agent.model == "old"
        assert tomllib.loads(config.read_text())["model"] == "old"

    def test_model_unknown_flag_shows_usage(self, tmp_path, capsys):
        from aish.cli import LogRef, handle_slash
        from aish.session import SessionLog

        agent, _ = self._agent()
        logref = LogRef(SessionLog.new(tmp_path))
        handle_slash("/model qwen3:8b --bogus", agent, logref, tmp_path)
        assert agent.model == "old"
        assert "usage: /model" in capsys.readouterr().out

    def test_model_save_without_config_path_errors(self, tmp_path, capsys):
        from aish.cli import LogRef, handle_slash
        from aish.session import SessionLog

        agent, _ = self._agent()
        logref = LogRef(SessionLog.new(tmp_path))
        handle_slash("/model --save", agent, logref, tmp_path)
        assert "cannot save" in capsys.readouterr().out

    def test_failed_switch_does_not_save(self, tmp_path, capsys):
        from aish.cli import LogRef, handle_slash
        from aish.session import SessionLog

        agent, _ = self._agent()
        agent.provider = "claude-max"  # switching away is blocked in-session
        logref = LogRef(SessionLog.new(tmp_path))
        config = tmp_path / "config.toml"
        handle_slash("/model qwen3:8b --save", agent, logref, tmp_path, config_path=config)
        assert not config.exists()


class TestDefaultWorkspace:
    """Launching from $HOME must not make the whole home tree the session
    root — it re-anchors to ~/aish; every other launch dir is respected."""

    def _fake_home(self, tmp_path, monkeypatch):
        from pathlib import Path

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        return home

    def test_home_launch_reanchors_to_aish_subdir(self, tmp_path, monkeypatch):
        from aish.cli import default_workspace

        home = self._fake_home(tmp_path, monkeypatch)
        assert default_workspace(str(home)) == str(home / "aish")
        assert (home / "aish").is_dir()  # created on first use

    def test_existing_workspace_is_reused(self, tmp_path, monkeypatch):
        from aish.cli import default_workspace

        home = self._fake_home(tmp_path, monkeypatch)
        (home / "aish").mkdir()
        assert default_workspace(str(home)) == str(home / "aish")

    def test_non_home_launch_dir_is_respected(self, tmp_path, monkeypatch):
        from aish.cli import default_workspace

        self._fake_home(tmp_path, monkeypatch)
        project = tmp_path / "project"
        project.mkdir()
        assert default_workspace(str(project)) == str(project)

    def test_subdir_of_home_is_respected(self, tmp_path, monkeypatch):
        from aish.cli import default_workspace

        home = self._fake_home(tmp_path, monkeypatch)
        sub = home / "dev"
        sub.mkdir()
        assert default_workspace(str(sub)) == str(sub)


class TestParseLearn:
    def test_learn_returns_distillation_prompt(self):
        from aish.cli import parse_learn

        prompt = parse_learn("/learn")
        assert prompt is not None
        assert "recall" in prompt and "skills" in prompt

    def test_hint_is_embedded(self):
        from aish.cli import parse_learn

        prompt = parse_learn("/learn the gh issue flow")
        assert "the gh issue flow" in prompt

    def test_lessons_hint_switches_to_migration(self, tmp_path):
        from aish.cli import parse_learn

        lessons = tmp_path / "lessons.md"
        prompt = parse_learn("/learn lessons", lessons)
        assert str(lessons) in prompt
        assert "lessons.md.bak" in prompt

    def test_prefix_resolves_and_other_commands_pass_through(self):
        from aish.cli import parse_learn

        assert parse_learn("/lea") is not None
        assert parse_learn("/resume") is None
        assert parse_learn("/model gemini") is None
        assert parse_learn("/feedback") is None  # feedback is not learn


class TestParseFeedback:
    def test_feedback_returns_flow_prompt(self):
        from aish.cli import parse_feedback

        prompt = parse_feedback("/feedback")
        assert prompt is not None
        assert "gh_issue" in prompt and "GitHub issue" in prompt
        assert "aish-reply://Create the issue" in prompt  # approval chip

    def test_initial_details_are_embedded(self):
        from aish.cli import parse_feedback

        prompt = parse_feedback("/feedback the dark mode toggle is broken")
        assert "the dark mode toggle is broken" in prompt

    def test_attachments_add_public_upload_consent_rules(self):
        # #130: with attachments the classic prompt orders the draft to list
        # the assets with per-file exclude chips — confirm/deselect happens
        # before anything is uploaded to the public release.
        from aish.cli import parse_feedback

        prompt = parse_feedback("/feedback broken", attachments=True)
        assert "PUBLIC GitHub release" in prompt
        assert "aish-reply://Exclude <name> from the issue" in prompt
        assert "asset workflow" in prompt

    def test_without_attachments_no_consent_section(self):
        # The CLI's plain /feedback has nothing to upload: no Attachments
        # section, no exclude chips, no upload instruction.
        from aish.cli import parse_feedback

        prompt = parse_feedback("/feedback broken")
        assert "Exclude <name>" not in prompt
        assert "asset workflow" not in prompt

    def test_block_flow_emits_aish_issue_block_not_gh_create(self):
        # Web text-only feedback (#110): the model emits an aish-issue block and
        # must NOT run gh issue create — the backend files it on confirm.
        from aish.cli import parse_feedback

        prompt = parse_feedback("/feedback broken", block_flow=True)
        assert "```aish-issue" in prompt
        assert "Do NOT run `gh issue create`" in prompt
        assert "aish-reply://Create the issue" not in prompt  # no old chip

    def test_prefix_resolves_and_other_commands_pass_through(self):
        from aish.cli import parse_feedback

        assert parse_feedback("/feed") is not None
        assert parse_feedback("/f") is not None  # unambiguous: only /feedback
        assert parse_feedback("/learn") is None
        assert parse_feedback("/model gemini") is None
