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


# --- staleness -------------------------------------------------------------
#
# The checks above catch ABSENCE — code with no doc. These catch the other
# direction, a doc describing code that no longer exists, which is the failure
# a reader cannot detect: a name that was renamed or a threshold that moved
# reads exactly like one that is still true.

CODE_FILES = [
    *(REPO / "aish").rglob("*.py"),
    *(REPO / "aish" / "static").glob("*.js"),
    *(REPO / "tests").rglob("*.py"),
    *(REPO / "tests" / "js").glob("*.js"),
]

# A forward SPEC, not a description: it names record fields for work that has
# not shipped. Drop this exemption when #193/#194/#197 land.
_SPEC_DOCS = {"trace-contract.md"}

# Symbols that legitimately live outside this repo. Kept empty on purpose —
# add a name here only when it is genuinely a third-party symbol the docs must
# mention, never to silence a rename.
_EXTERNAL_SYMBOLS: set[str] = {
    # urllib.request.HTTPRedirectHandler.http_error_302 — #213 names it to
    # record WHY redirect targets need no URL encoding of ours: the stdlib
    # already quotes them there. Naming the method is the evidence.
    "http_error_302",
    # claude-agent-sdk symbols — #352 names them in docs/agent-core.md as the
    # EVIDENCE that the SDK has no pre-request hook: its `HookEvent` literals
    # (checked in the installed 0.2.121) are all tool- or session-lifecycle
    # events, the CLI is spawned with `anyio.open_process`, and hooks arrive
    # as `hook_callback` control requests. Naming them is the finding.
    "HookEvent",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "SubagentStart",
    "SubagentStop",
    "UserPromptSubmit",
    "open_process",
    "hook_callback",
}

# The trailing `()` is optional because docs spell a function both ways, and
# skipping the `foo()` form silently exempted most function references — the
# first version of this check did exactly that and a mutation test caught it.
_BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(?:\(\))?`")
_CONST_DEF = re.compile(r"^\s*(?:const |let )?([A-Z][A-Z0-9_]{3,})\s*=\s*(-?\d+(?:\.\d+)?)\b", re.M)


def _looks_like_a_symbol(name: str) -> bool:
    """snake_case, ALL_CAPS, or CamelCase — not an English word in backticks."""
    if "_" in name and name.islower():
        return True
    if name.isupper() and len(name) > 3:
        return True
    return (
        name[:1].isupper()
        and any(c.islower() for c in name)
        and any(c.isupper() for c in name[1:])
    )


def _code_text() -> str:
    return "\n".join(p.read_text(errors="ignore") for p in CODE_FILES)


def test_documented_symbols_exist() -> None:
    """A doc naming a symbol that no longer exists is a doc telling a lie."""
    code = _code_text()
    stale = []
    for path in sorted(DOCS.glob("*.md")):
        if path.name in _SPEC_DOCS:
            continue
        for token in sorted(set(_BACKTICKED.findall(path.read_text()))):
            base = token.split(".")[-1]
            if not _looks_like_a_symbol(base) or base in _EXTERNAL_SYMBOLS:
                continue
            if base not in code:
                stale.append(f"{path.name}: `{token}`")
    assert not stale, (
        f"docs name symbols that exist nowhere in the tree: {stale}. Rename or "
        "remove the reference — or, if it is genuinely a third-party symbol, "
        "add it to _EXTERNAL_SYMBOLS with a reason."
    )


def test_documented_constants_match_the_code() -> None:
    """The calibrated numbers are the point; a stale one reads as still true.

    PREFLIGHT_MIN_SIM, MERGE_MIN_SIM, TOOL_BUDGET, WORKER_POOL_SIZE and the
    rest were measured, and a doc quoting the old value is worse than one
    quoting none.
    """
    values: dict[str, set[str]] = {}
    for path in CODE_FILES:
        if REPO / "tests" in path.parents:
            continue
        for name, value in _CONST_DEF.findall(path.read_text(errors="ignore")):
            values.setdefault(name, set()).add(value)

    wrong = []
    for path in sorted(DOCS.glob("*.md")):
        text = path.read_text()
        for name, defined in values.items():
            for m in re.finditer(re.escape(name) + r"`?[^\n]{0,30}?(\d+(?:\.\d+)?)", text):
                if not any(float(v) == float(m.group(1)) for v in defined):
                    wrong.append(
                        f"{path.name}: {name} documented as {m.group(1)}, "
                        f"code says {sorted(defined)}"
                    )
    assert not wrong, "documented constants no longer match the code: " + "; ".join(wrong)


# The original CLAUDE.md held a single 57,810-character line. That shape — one
# paragraph everything gets appended to — is what made it unreadable and
# unfileable, so it is the shape to fail on. There is deliberately NO cap on a
# doc's total SIZE: docs/ is read on demand, the measured problem was never
# length but missing coverage, and a size cap pushes toward under-documenting.
MAX_LINE = 2500


def test_docs_have_no_blob_paragraphs() -> None:
    blobs = []
    for path in sorted(DOCS.glob("*.md")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if len(line) > MAX_LINE:
                blobs.append(f"{path.name}:{n} ({len(line)} chars)")
    assert not blobs, (
        f"paragraphs over {MAX_LINE} chars: {blobs}. Split it into its own "
        "section — a paragraph that keeps growing is how these files became "
        "unreadable the first time."
    )
