"""Keep the frontend docs complete as app.js grows.

docs/web-frontend.md reached 49k while never mentioning 19 of the 42 fenced
regions in app.js or 17 of its 43 tests: length and coverage had come apart,
because nothing ever failed when a fence went undocumented. These checks are
what make "someone reviews it end to end" a property instead of an intention.

A fence marks code whose invariant is invisible from reading it. If it is worth
fencing it is worth one entry in the doc; if it is not worth an entry, drop the
fence.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "aish" / "static" / "app.js"
JS_TESTS = REPO / "tests" / "js"
DOCS = REPO / "docs"

_FENCE = re.compile(r"\[([A-Z][A-Z-]+)-(START|END)\]")

# Fences that live outside app.js or name no code region of their own.
_NOT_IN_APP_JS = {"SW-ROUTE"}


def _fences(text: str) -> tuple[set[str], list[str]]:
    """Paired fence names, plus any marker left without its partner."""
    open_at: dict[str, int] = {}
    paired: set[str] = set()
    dangling: list[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        m = _FENCE.search(line)
        if not m:
            continue
        name, kind = m.group(1), m.group(2)
        if kind == "START":
            open_at[name] = line_no
        elif name in open_at:
            del open_at[name]
            paired.add(name)
        else:
            dangling.append(f"{name} (END at line {line_no}, no START)")
    dangling += [f"{n} (START at line {ln}, no END)" for n, ln in open_at.items()]
    return paired, dangling


def _docs_text() -> str:
    return "\n".join(p.read_text() for p in sorted(DOCS.glob("*.md")))


def test_every_fence_is_balanced() -> None:
    """An unmatched marker is a region with no boundary — nothing to document."""
    _, dangling = _fences(APP_JS.read_text())
    assert not dangling, f"unbalanced fence markers in app.js: {dangling}"


def test_every_fenced_region_is_documented() -> None:
    paired, _ = _fences(APP_JS.read_text())
    docs = _docs_text()
    missing = sorted(name for name in paired if f"[{name}]" not in docs)
    assert not missing, (
        f"fenced regions in app.js with no entry in docs/: {missing}. "
        "A fence claims an invisible invariant — say what it owns and what "
        "breaks, in docs/web-frontend.md."
    )


def test_every_frontend_test_is_named() -> None:
    """A test nobody can find from the docs is a test nobody knows guards them."""
    docs = _docs_text()
    missing = sorted(
        p.name for p in JS_TESTS.glob("test_*.js") if p.name not in docs
    )
    assert not missing, (
        f"tests/js checks named nowhere in docs/: {missing}. Name each one on "
        "the entry it pins, so the doc says where the invariant is enforced."
    )


def test_documented_fences_still_exist() -> None:
    """The other direction: a doc entry for deleted code is worse than none."""
    paired, _ = _fences(APP_JS.read_text())
    # A doc reference is the bare name — `[VIEWCACHE]`. The `[NAME-START]`
    # form only appears when the prose is explaining the convention itself.
    documented = {
        m.group(1)
        for m in re.finditer(r"`?\[([A-Z][A-Z-]+)\]`?", (DOCS / "web-frontend.md").read_text())
        if not m.group(1).endswith(("-START", "-END"))
    }
    stale = sorted(documented - paired - _NOT_IN_APP_JS)
    assert not stale, (
        f"docs/web-frontend.md documents fences that no longer exist: {stale}"
    )
