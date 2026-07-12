import os
import stat

import pytest

from aish import tools


def make_fake_command(bin_dir, name: str, script_body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{script_body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fake_path(tmp_path, monkeypatch):
    """Isolated PATH so read_docs fallbacks hit only our fake commands."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


class TestTruncate:
    def test_short_text_unchanged(self):
        assert tools.truncate("hello") == "hello"

    def test_long_text_keeps_head_and_tail(self):
        text = "A" * 5000 + "MIDDLE" + "B" * 3000
        result = tools.truncate(text)
        assert result.startswith("A" * 100)
        assert result.endswith("B" * 100)
        assert "characters omitted" in result
        assert len(result) < len(text)


class TestRunCommand:
    def test_captures_stdout_and_exit_code(self):
        result = tools.run_command("echo hello")
        assert "hello" in result
        assert "[exit code: 0]" in result

    def test_captures_stderr_and_nonzero_exit(self):
        result = tools.run_command("echo oops >&2; exit 3")
        assert "[stderr]" in result
        assert "oops" in result
        assert "[exit code: 3]" in result

    def test_timeout_reported_not_raised(self):
        result = tools.run_command("sleep 5", timeout=0.2)
        assert "timed out" in result


class TestReadDocs:
    def test_rejects_shell_syntax(self):
        result = tools.read_docs("rm -rf /; evil --help")
        assert result.startswith("ERROR")
        assert "bare command name" in result

    def test_rejects_path_traversal(self):
        assert tools.read_docs("../../bin/sh").startswith("ERROR")

    def test_unknown_command(self):
        result = tools.read_docs("definitely-not-a-real-cmd-xyz")
        assert "not found" in result

    def test_man_page_found(self):
        result = tools.read_docs("ls")
        assert result.startswith("[man ls]")
        assert "list" in result.lower()

    def test_falls_back_to_help_flag(self, fake_path):
        make_fake_command(fake_path, "helpme", 'echo "usage: helpme [--frob]"')
        result = tools.read_docs("helpme")
        assert result.startswith("[helpme --help]")
        assert "--frob" in result

    def test_falls_back_to_h_flag(self, fake_path):
        make_fake_command(
            fake_path,
            "shyhelp",
            'if [ "$1" = "-h" ]; then echo "usage: shyhelp"; fi',
        )
        result = tools.read_docs("shyhelp")
        assert result.startswith("[shyhelp -h]")
        assert "usage: shyhelp" in result

    def test_no_docs_at_all(self, fake_path):
        make_fake_command(fake_path, "silent", "true")
        result = tools.read_docs("silent")
        assert "NO DOCUMENTATION FOUND" in result
        assert "caution" in result

    def test_validated_name_never_executed_with_arguments(self, fake_path, tmp_path):
        """A fake command records its args; read_docs must only ever pass --help/-h."""
        log = tmp_path / "args.log"
        make_fake_command(fake_path, "spy", f'echo "$@" >> {log}; echo "usage: spy"')
        tools.read_docs("spy")
        calls = log.read_text().splitlines()
        assert calls == ["--help"]


def test_man_output_is_plain_text():
    """col -b must strip backspace overstrikes man uses for bold."""
    result = tools.read_docs("ls")
    assert "\b" not in result


def test_subprocess_gets_no_stdin():
    """Interactive commands must not hang waiting for input."""
    result = tools.run_command("read line; echo got:$line", timeout=5)
    assert "timed out" not in result
