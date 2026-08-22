"""The two desktop-layout facts that regress in silence.

Both are CSS, both were shipped broken for as long as the docked rail has
existed, and neither shows up in a node check or a phone-sized screenshot:

  1. The docked sidebar's rules used to be unconditional inside the width query,
     so `body.rail-open` meant nothing there and the only controls that could
     hide the list (the topbar chats button, ⌘O) were dead on exactly the
     screens wide enough to want it hidden. "Tidying" the class back out of the
     query restores that, and every test still passes.

  2. Sheets are `position: fixed`, so they never saw the shell's inset and
     centred themselves on the WINDOW. On a window narrower than roughly
     1300px that put the sheet under the docked rail, which is painted above it
     (z-index 30 vs 20) — reported as the full-record panel sitting "in the
     middle and behind the session list".

Real verification of either needs a layout engine at a desktop viewport; what is
checkable here is that the rules still say what the fix rests on.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CSS = (REPO / "aish" / "static" / "style.css").read_text()
APP_JS = (REPO / "aish" / "static" / "app.js").read_text()


def _docked_query() -> str:
    """The body of `@media (min-width: 900px)` — where docking is defined."""
    start = CSS.index("@media (min-width: 900px) {")
    depth = 0
    for i in range(start, len(CSS)):
        if CSS[i] == "{":
            depth += 1
        elif CSS[i] == "}":
            depth -= 1
            if depth == 0:
                return CSS[start : i + 1]
    raise AssertionError("the docked media query is never closed")


def test_the_docked_sidebar_can_be_put_away() -> None:
    """Its showing rules hang off `body.rail-open`, so removing that class
    hides it. Unconditional rules there make the toggle a no-op on screen while
    the JS goes on flipping a class nothing reads."""
    query = _docked_query()
    inset = re.search(r"^\s*(\S[^{]*)\{\s*padding-left:", query, re.M)
    assert inset, "the docked query no longer insets the shell for the rail"
    assert "rail-open" in inset.group(1), (
        "the shell's inset is unconditional again, so a put-away sidebar leaves "
        f"a {inset.group(1).strip()} gap where no panel is shown"
    )
    shows = re.search(r"([^{}]*)\{[^{}]*transform:\s*none", query, re.S)
    assert shows and "rail-open" in shows.group(1), (
        "the docked rail is shown unconditionally, so nothing can hide it"
    )


def test_a_sheet_is_inset_beside_the_docked_rail() -> None:
    """A sheet belongs to the chat, not the window: without this it centres over
    the whole viewport and slides under the rail on any ordinary laptop."""
    query = _docked_query()
    inset = re.search(r"([^{}]*\.sheet[^{}]*)\{([^{}]*)\}", query, re.S)
    assert inset, "sheets are no longer inset inside the docked query"
    assert "rail-w" in inset.group(2), (
        "the sheet inset no longer uses the rail's width, so it cannot line up "
        "with the shell's"
    )


def test_the_full_record_is_sized_as_a_document() -> None:
    """Every other sheet is a short list, capped at 560px on a wide screen. The
    dossier carries a turn's reasoning, arguments and command output, and at
    picker width it reads through a letterbox."""
    match = re.search(r"#explain-sheet\s*\{([^}]*)\}", CSS)
    assert match, "#explain-sheet has no desktop size of its own"
    body = match.group(1)
    width = re.search(r"max-width:[^;]*?(\d{3,4})px", body)
    assert width and int(width.group(1)) > 560, (
        "the full record is back at picker width on a desktop"
    )
    assert "max-height" in body, "the dossier no longer claims extra height"


def test_one_glyph_at_a_time_can_actually_win() -> None:
    """The chats button carries BOTH glyphs and lets CSS pick (design Screen 0).

    Picking is `display`, and the shared sizing rule `.back-ico > svg` already
    sets `display: block` at specificity (0,1,1) — so a bare `.ico-menu` /
    `.ico-sidebar` rule (0,1,0) loses and hides nothing. That failure is
    invisible everywhere except a real browser: the node checks read app.js,
    the vocabulary check reads text, and both glyphs painting side by side in
    the top-left corner passed every one of them. Measured, then pinned.
    """
    sizing = re.search(r"\.back-ico\s*>\s*svg\s*\{([^}]*)\}", CSS)
    assert sizing and "display" in sizing.group(1), (
        "the sizing rule no longer sets display — re-check whether the glyph "
        "rules below still need their extra specificity"
    )
    for glyph in (".ico-menu", ".ico-sidebar"):
        # (?![\w-]) so `.ico-sidebar-fill` — a <rect>, not a direct svg child,
        # and therefore not in this contest — does not match `.ico-sidebar`.
        rules = re.findall(
            rf"([^{{}};]*{re.escape(glyph)}(?![\w-])[^{{}}]*)\{{([^}}]*display[^}}]*)\}}", CSS)
        assert rules, f"nothing sets display on {glyph}"
        for selector, _ in rules:
            assert ".back-ico" in selector, (
                f"`{selector.strip()}` sets display on {glyph} at lower specificity "
                f"than `.back-ico > svg`, so it is a no-op and both glyphs paint"
            )


def test_only_the_toggle_files_the_sidebar_away() -> None:
    """`closeSessionRail` is the slide-over's dismiss — the scrim, Escape,
    picking a chat, starting a new one. If it wrote the preference, opening any
    chat on a desktop would close the sidebar for good."""
    body = APP_JS[APP_JS.index("function closeSessionRail()") :]
    body = body[: body.index("\n}\n") + 2]
    assert "setRailFiledAway" not in body, (
        "closeSessionRail now writes the owner's preference, so every tap on a "
        "chat row files the desktop sidebar away"
    )


class TestACardThatListsValuesRendersAsAList:
    """#251. A batch approval card is a LIST — one line per value going into
    the form — and its whole claim to be more oversight than the keystrokes it
    replaces is that the owner can read those values before he taps.

    With the default `white-space` the newlines collapsed and it rendered as
    one run-on line. Measured in real Chrome at 390px before the fix:
    `fill in this form on lot.com and send it: 'Skąd' ← 'WAW' 'Dokąd' ← …`.
    A JS test would not have caught it: the string was always correct, and it
    was the stylesheet that threw the shape away."""

    def _rule(self) -> str:
        start = CSS.index(".tool-preview {")
        return CSS[start:CSS.index("}", start)]

    def test_the_preview_keeps_the_line_breaks_it_was_given(self):
        assert "white-space: pre-wrap" in self._rule()

    def test_it_still_wraps_a_long_unbroken_value(self):
        """pre-wrap must not become pre: a URL or a long value has to fold
        inside the card rather than push it sideways off a phone."""
        assert "overflow-wrap: anywhere" in self._rule()
