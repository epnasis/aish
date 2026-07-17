"""Drive the boxed prompt with scripted keystrokes via prompt_toolkit's
pipe input — tests real key bindings without a terminal."""

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from aish.prompt import BoxPrompt


def drive(tmp_path, keys: str, vi_mode: bool = False) -> str:
    box = BoxPrompt(vi_mode, tmp_path, ("/clear", "/resume"))
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return box.read_with_io("~", input=pipe, output=DummyOutput())


def test_enter_submits(tmp_path):
    assert drive(tmp_path, "hello\r") == "hello"


def test_backslash_continuation_makes_newline(tmp_path):
    assert drive(tmp_path, "line one\\\rline two\r") == "line one\nline two"


def test_ctrl_j_makes_newline(tmp_path):
    assert drive(tmp_path, "a\x0ab\r") == "a\nb"


def test_alt_enter_makes_newline(tmp_path):
    assert drive(tmp_path, "a\x1b\rb\r") == "a\nb"


def test_ctrl_c_aborts_input(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        drive(tmp_path, "half typed\x03")


def test_ctrl_d_on_empty_quits(tmp_path):
    with pytest.raises(EOFError):
        drive(tmp_path, "\x04")


def test_vi_mode_esc_then_edit(tmp_path):
    # insert "hello", Esc to NORMAL, 0 to line start, then submit with Enter
    assert drive(tmp_path, "hello\x1b0\r", vi_mode=True) == "hello"


class TestAtFileCompleter:
    def project(self, tmp_path):
        (tmp_path / "notes.md").write_text("n")
        (tmp_path / "domain.py").write_text("d")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("m")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("secret")
        return tmp_path

    def completions(self, root, text):
        from prompt_toolkit.document import Document

        from aish.prompt import AtFileCompleter

        completer = AtFileCompleter(get_cwd=lambda: str(root))
        return [c.text for c in completer.get_completions(Document(text), None)]

    def test_bare_at_lists_project_files(self, tmp_path):
        got = self.completions(self.project(tmp_path), "explain @")
        assert "notes.md " in got and "src/" in got

    def test_fragment_filters_and_ranks_basename_prefix_first(self, tmp_path):
        got = self.completions(self.project(tmp_path), "@ma")
        # main.py's basename starts with 'ma'; domain.py merely contains it
        assert got.index("src/main.py ") < got.index("domain.py ")

    def test_matches_anywhere_in_path(self, tmp_path):
        assert "src/main.py " in self.completions(self.project(tmp_path), "@src/ma")

    def test_ignored_dirs_are_hidden(self, tmp_path):
        got = self.completions(self.project(tmp_path), "@config")
        assert got == []

    def test_at_inside_a_word_does_not_trigger(self, tmp_path):
        assert self.completions(self.project(tmp_path), "mail me@notes") == []

    def test_closed_mention_does_not_trigger(self, tmp_path):
        assert self.completions(self.project(tmp_path), "see @notes.md and fix") == []

    def test_slash_command_line_is_left_alone(self, tmp_path):
        assert self.completions(self.project(tmp_path), "/cd @no") == []

    def test_completion_replaces_only_the_fragment(self, tmp_path):
        from prompt_toolkit.document import Document

        from aish.prompt import AtFileCompleter

        completer = AtFileCompleter(get_cwd=lambda: str(self.project(tmp_path)))
        first = next(completer.get_completions(Document("read @not"), None))
        assert first.start_position == -len("not")


def picker_search(query):
    """Stand-in for SessionLog.rank: substring filter over fake sessions."""
    from pathlib import Path

    from aish.session import SessionInfo

    infos = [
        SessionInfo(Path("session-2.jsonl"), "2026-02-01 00:00", 1, "from february"),
        SessionInfo(Path("session-1.jsonl"), "2026-01-01 00:00", 1, "from january"),
    ]
    if not query.strip():
        return infos
    return [info for info in infos if query.casefold() in info.title.casefold()]


def pick(tmp_path, keys: str, initial: str = ""):
    box = BoxPrompt(False, tmp_path)
    render = lambda info: f"{info.when} · {info.title}"  # noqa: E731
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return box.pick_with_io(picker_search, initial, render, input=pipe, output=DummyOutput())


def test_picker_is_generic_over_plain_items(tmp_path):
    box = BoxPrompt(False, tmp_path)
    search = lambda q: [s for s in ("alpha", "bravo") if q in s]  # noqa: E731
    with create_pipe_input() as pipe:
        pipe.send_text("v\r")
        assert box.pick_with_io(search, "", str, input=pipe, output=DummyOutput()) == "bravo"


def test_picker_enter_takes_top_result(tmp_path):
    assert pick(tmp_path, "\r").title == "from february"


def test_picker_typing_filters(tmp_path):
    assert pick(tmp_path, "january\r").title == "from january"


def test_picker_arrow_moves_selection(tmp_path):
    assert pick(tmp_path, "\x1b[B\r").title == "from january"  # Down, Enter


def test_picker_initial_query_prefills(tmp_path):
    assert pick(tmp_path, "\r", initial="jan").title == "from january"


def test_picker_backspace_widens_filter_again(tmp_path):
    # "janx" matches nothing; deleting the x restores the january match
    assert pick(tmp_path, "janx\x7f\r").title == "from january"


def test_picker_ctrl_c_cancels(tmp_path):
    assert pick(tmp_path, "\x03") is None


def test_picker_enter_with_no_match_returns_none(tmp_path):
    assert pick(tmp_path, "zzz\r") is None
