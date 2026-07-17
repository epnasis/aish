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
