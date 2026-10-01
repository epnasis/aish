"""The model chip strips a provider prefix only when it is a known one, because
in an Ollama name (`qwen3:8b`) the colon is the tag. The frontend's list must
name exactly the providers the backend routes, or a new provider's prefix shows
up on the chip (or an Ollama tag gets eaten)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from aish import backends

APP_JS = Path(__file__).resolve().parent.parent / "aish" / "static" / "app.js"


def test_chip_provider_list_matches_the_backend() -> None:
    match = re.search(r"const MODEL_PROVIDERS = (\[[^\]]*\]);", APP_JS.read_text())
    assert match, "MODEL_PROVIDERS not found in app.js"
    assert set(json.loads(match.group(1))) == set(backends.PROVIDERS) | {"claude-max"}
