"""The structural invariant behind the step screen's non-springing header.

The header (`#ss-chrome`) is a FIXED OVERLAY over the scroller, deliberately NOT
a child of the scrolling element (`#ss-body`). That is what keeps it still while
the content rubber-bands on overscroll: a sticky child rides the container's
bounce, so the header sprang with the page until it was lifted out. A tidy-up
that nests the chrome back inside the body would silently bring the spring back
(and the layout engine, not this suite, is where you'd finally see it), so the
sibling relationship is pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX = (REPO / "aish" / "static" / "index.html").read_text()
CSS = (REPO / "aish" / "static" / "style.css").read_text()


def test_header_is_a_sibling_of_the_scrolling_body_not_a_child() -> None:
    region = INDEX[INDEX.index('id="step-screen"'):]
    region = region[: region.index("</div>\n</div>", region.index('id="ss-scroll-top"'))]

    depth = 0
    depth_at: dict[str, int] = {}
    parent_of: dict[str, int] = {}
    for m in re.finditer(r"<(/?)div\b([^>]*)>", region):
        closing, attrs = m.group(1), m.group(2)
        if closing:
            depth -= 1
            continue
        for key in ("ss-chrome", "ss-body", "ss-content"):
            if f'id="{key}"' in attrs:
                depth_at[key] = depth
                parent_of[key] = depth  # its own depth; children sit one deeper
        depth += 1

    assert depth_at.get("ss-chrome") is not None, "#ss-chrome missing"
    assert depth_at.get("ss-body") is not None, "#ss-body missing"
    assert depth_at["ss-chrome"] == depth_at["ss-body"], (
        f"#ss-chrome (depth {depth_at.get('ss-chrome')}) and #ss-body "
        f"(depth {depth_at.get('ss-body')}) must be SIBLINGS — the header is an "
        "overlay, not a child of the scrolling body, or it rubber-bands with it."
    )
    # The panes still live inside the scrolling body.
    assert depth_at.get("ss-content", -1) > depth_at["ss-body"], (
        "#ss-content must remain inside #ss-body"
    )


def test_the_header_is_positioned_as_an_overlay() -> None:
    """A `position: sticky`/`static` chrome would scroll (and bounce) with the
    body; the overlay behaviour depends on it being absolutely positioned."""
    block = re.search(r"#ss-chrome\s*\{([^}]*)\}", CSS)
    assert block, "#ss-chrome rule not found"
    assert "position: absolute" in block.group(1), (
        "#ss-chrome must be position: absolute (an overlay), not sticky/static"
    )
