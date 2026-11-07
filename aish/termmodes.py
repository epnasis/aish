"""What the console's outer terminal has been TOLD, so a fresh emulator can be told it too.

The web console is one long-lived PTY (a tmux client) viewed by terminal emulators
that come and go: every overlay open builds a new xterm.js instance, and a page
reload or a second device is another one. tmux told the FIRST emulator which
private modes to run in — mouse reporting, the alternate screen, bracketed paste —
and re-asserts them only when the client is resized; a same-size reopen gets a
repaint of the CONTENT and nothing else. The new emulator therefore runs in its
defaults: a wheel over a program that owns the mouse scrolls the emulator's own
empty scrollback and the program never hears of it.

The server is the one party that carried every byte, so it is the one authority on
the terminal's current mode state. `TerminalModes.feed` watches the output stream
for DECSET/DECRST (`ESC [ ? Pm h` / `ESC [ ? Pm l`) and keeps the last state per
mode; `replay` renders the sequences that bring a brand-new emulator to that state.
Pure, no I/O; the server feeds it from the fan-out and replays it to a newcomer.
"""

from __future__ import annotations

import re

# Modes worth replaying: the ones xterm.js implements whose default differs from
# what a running program may have asked for, and whose absence is visible — mouse
# reporting (1000-1006), the alternate screen (47/1047/1049), application cursor
# keys (1), bracketed paste (2004), focus reporting (1004), cursor visibility (25)
# and autowrap (7). Synchronized output (2026) is deliberately NOT here: it is
# toggled around every frame, and replaying a trailing `h` with no `l` would
# freeze the emulator until the next frame happened to end it.
REPLAYED_MODES: frozenset[int] = frozenset(
    {1, 7, 12, 25, 47, 1000, 1002, 1003, 1004, 1005, 1006, 1047, 1048, 1049, 2004}
)

# DECSET/DECRST, or a full reset (RIS `ESC c`, DECSTR `CSI ! p`) that returns every
# private mode to its default — after one, the record must start over or it would
# assert a state the real terminal no longer has.
_DEC_PRIVATE_MODE = re.compile(r"\x1b\[\?([0-9;]+)([hl])|\x1bc|\x1b\[!p")
# A private-mode sequence is short; a carry longer than this is not one.
_MAX_CARRY = 32
# A trailing fragment that could still grow into a DECSET/DECRST once the next
# chunk arrives: carried over instead of being scanned (and lost) now.
_PARTIAL_TAIL = re.compile(r"\x1b(?:\[(?:\?[0-9;]*|!)?)?\Z")  # `ESC [ !` is a split DECSTR


class TerminalModes:
    """Last observed state of each DEC private mode on one output stream."""

    def __init__(self) -> None:
        self._state: dict[int, bool] = {}
        self._carry = ""

    def feed(self, text: str) -> None:
        """Scan one chunk of terminal output. Chunks split a sequence anywhere;
        a dangling prefix is held back and re-scanned with the next chunk."""
        data = self._carry + text
        self._carry = ""
        for match in _DEC_PRIVATE_MODE.finditer(data):
            if match.group(1) is None:
                self._state.clear()  # RIS / DECSTR
                continue
            on = match.group(2) == "h"
            for param in match.group(1).split(";"):
                if param.isdigit():
                    self._state[int(param)] = on
        tail = _PARTIAL_TAIL.search(data)
        if tail and len(tail.group(0)) <= _MAX_CARRY:
            self._carry = tail.group(0)

    def replay(self) -> str:
        """The sequences that take a fresh emulator to the observed state —
        modes seen set AND modes seen reset, so a default the program turned off
        is turned off again. Modes never seen are left to the emulator's default."""
        return "".join(
            f"\x1b[?{mode}{'h' if on else 'l'}"
            for mode, on in sorted(self._state.items())
            if mode in REPLAYED_MODES
        )

    def observed(self) -> dict[int, bool]:
        """A copy of every mode seen, replayed or not (for tests and diagnostics)."""
        return dict(self._state)
