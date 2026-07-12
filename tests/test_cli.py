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
