"""The two facts the browser sheet's layout fix rests on (#226).

Real verification of this column needs a layout engine and a phone viewport —
`scripts/check-browser-sheet.py`, which is not in this suite because the suite
launches no Chrome. What IS checkable here is the pair of preconditions that,
if broken, make the fix evaporate with nothing to see: the sheet keeps rendering,
the node tests keep passing, and the field goes back under the keyboard in
landscape the next time somebody signs in sideways.

Both are structural. `#bv-edit:not([hidden]) ~ .bv-nav` only matches while those
two are siblings, and moving `#bv-edit` (it is already oddly indented, which is
exactly the kind of thing a tidy-up moves) breaks it silently. And the stage's
`min-height` is a floor on every control BELOW it, so a well-meant "the frame
looks too small, give it 150px back" restores the bug.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX = (REPO / "aish" / "static" / "index.html").read_text()
CSS = (REPO / "aish" / "static" / "style.css").read_text()

# A tap on the page is the only way to leave a field without submitting, so the
# stage may shrink to a tap target — and must be able to shrink that far.
MAX_STAGE_FLOOR = 44


def test_the_editor_and_the_nav_row_are_siblings() -> None:
    """`#bv-edit:not([hidden]) ~ .bv-nav` stands the toolbar down while a field
    is open, which is what makes the column fit a landscape keyboard. `~` is a
    SIBLING combinator: nest either one and the rule stops matching, in silence."""
    body = re.search(
        r'<div class="bv-body">(.*?)\n</div>\s*</div>\s*<!-- \[BROWSER-VIEW',
        INDEX, re.S,
    )
    # Fall back to a bounded slice when the sheet's tail comment moves.
    region = body.group(1) if body else INDEX[INDEX.index('<div class="bv-body">'):]
    region = region[: region.index('id="bv-refresh"')]

    depth_at = {}
    depth = 0
    for m in re.finditer(r'<(/?)div\b([^>]*)>', region):
        closing, attrs = m.group(1), m.group(2)
        if closing:
            depth -= 1
            continue
        if 'id="bv-edit"' in attrs:
            depth_at["edit"] = depth
        if "bv-nav" in attrs:
            depth_at["nav"] = depth
        depth += 1

    assert depth_at.get("edit") is not None, "#bv-edit is not inside .bv-body"
    assert depth_at.get("nav") is not None, ".bv-nav is not inside .bv-body"
    assert depth_at["edit"] == depth_at["nav"], (
        f"#bv-edit (depth {depth_at['edit']}) and .bv-nav (depth {depth_at['nav']}) "
        "are no longer siblings, so the CSS that stands the toolbar down while a "
        "field is open matches nothing — and the field goes back under a "
        "landscape keyboard with no symptom until someone signs in sideways"
    )


def test_the_toolbar_stands_down_while_a_field_is_open() -> None:
    """The rule itself, keyed on [hidden] rather than on a class: the attribute
    is what `bvOpenEditor`/`bvCloseEditor` already toggle, so there is no second
    piece of state to fall out of step."""
    assert re.search(r"#bv-edit:not\(\[hidden\]\)\s*~\s*\.bv-nav\s*\{[^}]*display:\s*none",
                     CSS), "the nav row no longer stands down while a field is open"


def test_the_stage_can_shrink_to_a_tap_target() -> None:
    """The stage is the only elastic row in the column, so its floor is a floor
    on the nav row and on the field being typed into. At 150px the password
    input sat 100px below a landscape keyboard."""
    m = re.search(r"\.bv-stage\s*\{(.*?)\}", CSS, re.S)
    assert m, ".bv-stage rule is gone"
    floor = re.search(r"min-height:\s*(\d+)px", m.group(1))
    assert floor, ".bv-stage lost its min-height"
    assert int(floor.group(1)) <= MAX_STAGE_FLOOR, (
        f"the stage floor is {floor.group(1)}px: it is the only row that can "
        f"absorb a squeeze, so anything above {MAX_STAGE_FLOOR}px pushes the "
        "editor and the nav row off the bottom of a landscape keyboard (#226)"
    )


def test_the_sheet_clears_the_status_bar() -> None:
    """Full height reclaimed the dead band under the nav row and put the address
    row under the Dynamic Island. `.sheet` had always padded the BOTTOM inset;
    nothing accounted for the top because no sheet had reached it."""
    m = re.search(r"#browser-sheet\s*\{(.*?)\}", CSS, re.S)
    assert m, "#browser-sheet rule is gone"
    assert "env(safe-area-inset-top)" in m.group(1), (
        "#browser-sheet is full-height again without reserving the top inset — "
        "the top of the browser disappears under the status bar (#226)"
    )


def test_the_desktop_cap_is_the_frame_s_own_width() -> None:
    """On a wide screen the sheet takes the chat column, and stops at the width
    the frame was captured at (#259). Past that the stage magnifies a picture it
    already holds at native size, and `bvRequestDetail` starts spending a round
    trip on every idle glance — so the two numbers are one decision and must not
    drift apart in silence."""
    from aish import browser

    desktop = re.search(
        r"@media \(min-width: 700px\) \{[^}]*#browser-sheet\s*\{([^}]*)\}", CSS, re.S
    )
    assert desktop, (
        "#browser-sheet is back at the 560px picker cap `.sheet` sets — a real "
        "page in a picker-width column with the window empty beside it (#259)"
    )
    cap = re.search(r"min\((\d+)px", desktop.group(1))
    assert cap and int(cap.group(1)) == browser.VIEW_DESKTOP_WIDTH, (
        f"the sheet caps at {cap and cap.group(1)}px while frames are captured "
        f"at {browser.VIEW_DESKTOP_WIDTH}px: below parity it letterboxes what "
        "was rendered, above it magnifies and asks for detail at rest"
    )
