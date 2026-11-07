"""The console's terminal-mode tracker: a fresh xterm must learn what tmux told the
first one (mouse reporting, alternate screen, ...), or a wheel over a program that
owns the mouse scrolls an empty scrollback instead of the program."""

from aish.termmodes import REPLAYED_MODES, TerminalModes


def test_last_state_per_mode_wins():
    modes = TerminalModes()
    modes.feed("\x1b[?1049h\x1b[?1000h\x1b[?1006h")
    modes.feed("\x1b[?1000l")
    assert modes.observed() == {1049: True, 1000: False, 1006: True}
    assert modes.replay() == "\x1b[?1000l\x1b[?1006h\x1b[?1049h"


def test_multi_parameter_sets_and_resets_each_mode():
    modes = TerminalModes()
    modes.feed("\x1b[?1000;1002;1006h\x1b[?25;12l")
    assert modes.observed() == {1000: True, 1002: True, 1006: True, 25: False, 12: False}


def test_a_sequence_split_across_chunks_is_still_seen():
    whole = "\x1b[?1003h"
    for cut in range(1, len(whole)):
        modes = TerminalModes()
        modes.feed(whole[:cut])
        modes.feed(whole[cut:])
        assert modes.observed() == {1003: True}, f"lost when split at {cut}"


def test_a_dangling_escape_that_is_not_a_mode_does_not_poison_the_next_chunk():
    modes = TerminalModes()
    modes.feed("abc\x1b")
    modes.feed("[2J\x1b[?2004h")  # a clear, then a real set
    assert modes.observed() == {2004: True}


def test_synchronized_output_is_tracked_but_never_replayed():
    modes = TerminalModes()
    modes.feed("\x1b[?2026h frame \x1b[?2026l\x1b[?2026h")
    assert modes.observed()[2026] is True
    assert 2026 not in REPLAYED_MODES
    assert "2026" not in modes.replay()


def test_unknown_modes_are_remembered_but_not_replayed():
    modes = TerminalModes()
    modes.feed("\x1b[?7727h\x1b[?2031h\x1b[?1h")
    assert modes.replay() == "\x1b[?1h"


def test_nothing_observed_replays_nothing():
    assert TerminalModes().replay() == ""
    modes = TerminalModes()
    modes.feed("plain text \x1b[31mred\x1b[0m \x1b[2;3H")
    assert modes.replay() == ""


def test_the_live_console_fingerprint_round_trips():
    # What tmux sent a fresh client on attach to a pane running Claude Code's
    # fullscreen TUI (observed 2026-11-07), interleaved with repaint noise.
    attach = (
        "\x1b[?1049h\x1b[?1h\x1b=\x1b[?12l\x1b[?25l\x1b[H\x1b[2J"
        "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1004h\x1b[?1005l\x1b[?1006h"
        "\x1b[?2004h\x1b[?2031h\x1b[?7727h"
        "\x1b[1;1Hhello\x1b[?25h"
    )
    modes = TerminalModes()
    for i in range(0, len(attach), 7):  # arbitrary chunking
        modes.feed(attach[i : i + 7])
    assert modes.replay() == (
        "\x1b[?1h\x1b[?12l\x1b[?25h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1004h"
        "\x1b[?1005l\x1b[?1006h\x1b[?1049h\x1b[?2004h"
    )


def test_a_full_reset_forgets_everything_seen_before_it():
    for reset in ("\x1bc", "\x1b[!p"):
        modes = TerminalModes()
        modes.feed("\x1b[?1003h\x1b[?1049h" + reset + "\x1b[?2004h")
        assert modes.observed() == {2004: True}, repr(reset)
        split = TerminalModes()  # the reset itself cut across chunks
        split.feed("\x1b[?1003h" + reset[:-1])
        split.feed(reset[-1:] + "\x1b[?2004h")
        assert split.observed() == {2004: True}, repr(reset)


def test_a_runaway_partial_sequence_is_dropped_not_carried_forever():
    modes = TerminalModes()
    modes.feed("\x1b[?" + "1" * 100)  # binary spew that never closes the sequence
    modes.feed("003h")
    assert modes.observed() == {}
    modes.feed("\x1b[?1003h")  # and the tracker still works afterwards
    assert modes.observed() == {1003: True}
