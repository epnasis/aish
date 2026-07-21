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
