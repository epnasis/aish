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
