"""Run the dependency-free Node frontend checks under tests/js/ as part of the
pytest suite. Skipped when node isn't installed so the suite still runs with no
JS toolchain; where node exists (dev machines, CI) these guard the app.js
regex/rendering primitives that have no Python-side coverage."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TEST_DIR = Path(__file__).parent / "js"
_scripts = sorted(JS_TEST_DIR.glob("test_*.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("script", _scripts, ids=[s.name for s in _scripts])
def test_frontend_js(script: Path) -> None:
    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_parses_as_one_file() -> None:
    """The checks above extract MARKED BLOCKS and run them in isolation, so
    every one of them can pass while the shipped page does not parse at all.

    That is not hypothetical: a helper added for #233 was named `previewGroup`,
    which was already a module-level variable further down the file. Every
    block-level test passed. The browser threw
    `Identifier 'previewGroup' has already been declared` on load, the whole
    script died, and the app painted nothing — a blank page behind a spinner.
    Only driving a real browser found it.

    This is the cheap half of that lesson: whatever else is true, the file has
    to be a valid program.
    """
    app_js = Path(__file__).parent.parent / "aish" / "static" / "app.js"
    result = subprocess.run(
        ["node", "--check", str(app_js)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
