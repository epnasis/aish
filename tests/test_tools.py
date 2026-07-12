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


class TestReadDocsTopic:
    def test_topic_filters_real_man_page(self):
        result = tools.read_docs("find", topic="maxdepth")
        assert "lines matching 'maxdepth'" in result
        assert "-maxdepth" in result
        assert len(result) < 6500

    def test_no_topic_match_falls_back_to_head(self):
        result = tools.read_docs("ls", topic="zzznotinthedocs")
        assert "NO LINES MATCH" in result

    def test_big_man_page_gets_truncation_hint(self):
        result = tools.read_docs("find")
        assert "docs truncated" in result
        assert "'topic'" in result

    def test_topic_on_help_fallback(self, fake_path):
        body = "\\n".join(f"line {i}" for i in range(50)) + "\\n--frob does the thing"
        make_fake_command(fake_path, "helpme2", f'printf "{body}\\n"')
        result = tools.read_docs("helpme2", topic="frob")
        assert "--frob does the thing" in result
        assert "line 0" not in result

    def test_gap_marker_between_matches(self):
        text = "\n".join(f"row {i}" for i in range(100))
        text = text.replace("row 10", "needle A").replace("row 90", "needle B")
        filtered = tools._filter_topic(text, "needle")
        assert "[...]" in filtered
        assert "needle A" in filtered and "needle B" in filtered


class TestBinaryOutput:
    def test_non_utf8_output_does_not_crash(self):
        """Regression: cat on a binary plist emitted 0xdf and killed the REPL."""
        result = tools.run_command(r"printf 'ok\337end'")
        assert "ok" in result and "end" in result
        assert "�" in result  # replacement char, not an exception
        assert "[exit code: 0]" in result

    def test_binary_help_output_does_not_crash(self, fake_path):
        make_fake_command(fake_path, "binhelp", r"printf 'usage\337: binhelp\n'")
        result = tools.read_docs("binhelp")
        assert "usage" in result
