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
