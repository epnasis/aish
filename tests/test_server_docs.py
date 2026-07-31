"""Keep docs/web-server.md a description of the server, not a changelog.

It was 33k and named 8 of the 58 `Test*` classes in tests/test_server.py:
whole subsystems — fork, rename, retry, upload, the folder browser, model
switching, @-completion — had no mention at all, because the file grew a
paragraph per issue and nobody ever compared it to the thing it describes.

server.py has no fenced regions to key on the way app.js does, so the two
axes here are the ones the code actually exposes: the routes it registers
and the test classes that guard its behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER_PY = REPO / "aish" / "server.py"
SERVER_TESTS = REPO / "tests" / "test_server.py"
DOCS = REPO / "docs"

_ROUTE = re.compile(r'(?:WebSocket)?Route\(\s*"([^"]+)"')
_TEST_CLASS = re.compile(r"^class (Test\w+)", re.M)

# Served by the same handler as "/" and documented as one row.
_ALIASES = {"/index.html": "/"}


def _docs_text() -> str:
    return "\n".join(p.read_text() for p in sorted(DOCS.glob("*.md")))


def test_every_route_is_documented() -> None:
    routes = {
        _ALIASES.get(r, r) for r in _ROUTE.findall(SERVER_PY.read_text())
    }
    docs = _docs_text()
    # A path with a placeholder is documented by its literal prefix.
    missing = sorted(r for r in routes if r.split("{")[0].rstrip("/") not in docs)
    assert not missing, (
        f"routes registered in server.py but named nowhere in docs/: {missing}"
    )


def test_every_server_test_class_is_named() -> None:
    """A subsystem with a test class and no doc entry is an undocumented subsystem.

    This is the check that found the hole: the class names track what the
    server actually does far better than the issue numbers the doc was
    organised by.
    """
    classes = set(_TEST_CLASS.findall(SERVER_TESTS.read_text()))
    docs = _docs_text()
    missing = sorted(c for c in classes if c not in docs)
    assert not missing, (
        f"{len(missing)} test classes in test_server.py named nowhere in docs/: "
        f"{missing}. Each one guards behaviour that should be described in "
        "docs/web-server.md — name it on the section it belongs to."
    )
