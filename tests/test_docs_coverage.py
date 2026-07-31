"""Every test class is named in docs/, and every doc is reachable.

The rule this file enforces came out of measuring: docs/web-frontend.md was
49k and never mentioned 19 of app.js's 42 fenced regions; docs/web-server.md
named 8 of 58 test classes. Both files were long AND silent about half their
subject, because they grew a paragraph per issue and nothing ever failed when
a subsystem went undocumented.

A test class is the cheapest honest inventory of what the code does — better
than the issue numbers the docs used to be organised by. So: if behaviour is
worth a test class, it is worth a mention in the doc for its area. The check
is deliberately weak (a name appearing anywhere in docs/) because a strong
one would be gamed by a list; it catches absence, not quality.

Frontend-specific checks (fence balance and coverage) live in
tests/test_frontend_docs.py; route coverage in tests/test_server_docs.py.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
TESTS = REPO / "tests"

_TEST_CLASS = re.compile(r"^class (Test\w+)", re.M)
_DOC_LINK = re.compile(r"`(docs/[\w./-]+\.md)`")


def _docs_text() -> str:
    return "\n".join(p.read_text() for p in sorted(DOCS.glob("*.md")))


def test_every_test_class_is_named_in_docs() -> None:
    docs = _docs_text()
    missing: dict[str, list[str]] = {}
    for path in sorted(TESTS.glob("test_*.py")):
        unnamed = [c for c in _TEST_CLASS.findall(path.read_text()) if c not in docs]
        if unnamed:
            missing[path.name] = unnamed
    assert not missing, (
        "test classes named nowhere in docs/: "
        + "; ".join(f"{f}: {', '.join(cs)}" for f, cs in missing.items())
        + ". Name each on the section of the doc that covers its area — an "
        "undocumented test class is an undocumented subsystem."
    )


def test_docs_cross_links_resolve() -> None:
    """A pointer to a doc that does not exist silently loses the knowledge."""
    broken = []
    for path in sorted(DOCS.glob("*.md")):
        for ref in _DOC_LINK.findall(path.read_text()):
            if not (REPO / ref).is_file():
                broken.append(f"{path.name} → {ref}")
    assert not broken, f"broken cross-links between docs: {broken}"
