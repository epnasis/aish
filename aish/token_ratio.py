"""How many request characters make one token, learned per model (#415).

aish sizes a `local:` request in TOKENS because that is what the server's
memory is spent in, but it can only measure characters before the request
goes out. The bridge is a ratio, and tokenizers differ per model, so it is
learned per `provider:model` from what the server reported — and kept across
restarts, so a new chat on a known model does not start from a guess.

The characters are `backends.request_chars`: the messages as the provider
receives them and the tool menu, as JSON — the same measure the estimate is
made with, so what the chat template adds is inside the ratio rather than a
bias beside it.

The ratio handed out is the LOWEST one among the recent samples, not their
mean. Over-counting tokens trims a little early; under-counting sends a request
larger than the server can hold, which is the out-of-memory crash this exists
to prevent (measured over 368 real calls on mi: 3.30 to 4.05 chars per token,
so a mean would under-count the dense ones).

Best-effort like `rate-limits.json`: a missing, unreadable or garbled file is
"nothing learned yet", and a file that cannot be written is not worth failing
a model call over.
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import NamedTuple

from . import atomic_write
from .paths import state_home

LEDGER_NAME = "token-ratios.json"
# Recent enough to follow a changed system prompt or tool menu, long enough
# that one unusual request does not decide the next twenty.
SAMPLES_KEPT = 20
# A model never seen before. Below every ratio measured on mi, on purpose.
DEFAULT_CHARS_PER_TOKEN = 2.0
# A reported count outside these bounds is not a tokenizer, it is a broken
# report, and one of them would pin the minimum for SAMPLES_KEPT calls.
PLAUSIBLE_CHARS_PER_TOKEN = (1.0, 8.0)


class Ratio(NamedTuple):
    chars_per_token: float
    source: str

    def tokens(self, chars: int) -> int:
        return math.ceil(max(0, chars) / self.chars_per_token)


_lock = threading.Lock()
_samples: dict[str, list[list[int]]] = {}
_loaded = False


def _path() -> Path:
    return state_home() / LEDGER_NAME


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        stored = json.loads(_path().read_text())
    except (OSError, ValueError):
        return
    if not isinstance(stored, dict):
        return
    for key, samples in stored.items():
        if not isinstance(key, str) or not isinstance(samples, list):
            continue
        kept = [
            sample
            for sample in samples
            if isinstance(sample, list)
            and len(sample) == 2
            and all(type(n) is int for n in sample)
            and _plausible(*sample)
        ]
        if kept:
            _samples[key] = kept[-SAMPLES_KEPT:]


def _plausible(chars: int, tokens: int) -> bool:
    low, high = PLAUSIBLE_CHARS_PER_TOKEN
    return tokens > 0 and low <= chars / tokens <= high


def ratio(key: str) -> Ratio:
    """The chars-per-token to estimate `key`'s requests with, and where it came from."""
    with _lock:
        _load()
        samples = _samples.get(key)
        if not samples:
            source = f"constant:DEFAULT_CHARS_PER_TOKEN:{DEFAULT_CHARS_PER_TOKEN}"
            return Ratio(DEFAULT_CHARS_PER_TOKEN, source)
        lowest = min(chars / tokens for chars, tokens in samples)
        return Ratio(lowest, f"learned:min_of_{len(samples)}")


def observe(key: str, chars: int, tokens: int) -> None:
    """One request of `chars` that the server counted as `tokens`."""
    if not _plausible(chars, tokens):
        return
    with _lock:
        _load()
        samples = _samples.setdefault(key, [])
        samples.append([chars, tokens])
        del samples[:-SAMPLES_KEPT]
        snapshot = json.dumps(_samples, indent=1)
    try:
        # Whole or not at all: the server and a CLI share the state tree.
        atomic_write.publish(_path(), snapshot)
    except OSError:
        pass


def reset() -> None:
    """Forget what this process holds; the next read reloads from disk."""
    global _loaded
    with _lock:
        _samples.clear()
        _loaded = False
