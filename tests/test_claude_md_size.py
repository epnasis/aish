"""Keep CLAUDE.md a routing table.

It spent a year as a decision log — a paragraph per issue — and reached 156k
chars, past the 150k limit Claude Code warns at, which is where the whole file
stops being reliable context. The split moved the per-area rationale into
docs/; these tests are what stops it growing back, and what stops a pointer
going stale while the doc it names is renamed or deleted.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO / "CLAUDE.md"

MAX_CHARS = 40_000

_DOC_REF = re.compile(r"`(docs/[\w./-]+\.md)`")


def test_claude_md_stays_a_routing_table() -> None:
    size = len(CLAUDE_MD.read_text())
    assert size <= MAX_CHARS, (
        f"CLAUDE.md is {size} chars, over the {MAX_CHARS} budget. New per-area "
        "rationale belongs in the matching docs/ file, not here — see the "
        '"Where the knowledge lives" section.'
    )


def test_every_referenced_doc_exists() -> None:
    """A pointer to a missing doc silently loses the knowledge it routed to."""
    referenced = sorted(set(_DOC_REF.findall(CLAUDE_MD.read_text())))
    assert referenced, "CLAUDE.md should point at the docs/ files that hold the detail"
    missing = [ref for ref in referenced if not (REPO / ref).is_file()]
    assert not missing, f"CLAUDE.md points at docs that do not exist: {missing}"


def test_every_doc_is_reachable_from_claude_md() -> None:
    """A doc nobody is routed to is a doc nobody reads."""
    text = CLAUDE_MD.read_text()
    orphans = [
        doc.name
        for doc in sorted((REPO / "docs").glob("*.md"))
        if f"docs/{doc.name}" not in text
    ]
    assert not orphans, (
        f"docs/ files unreachable from CLAUDE.md: {orphans}. Add them to the "
        "routing table or the module map."
    )
