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
to prevent. While there are few samples even the lowest is not trusted as the
floor: it is discounted (`SPARSE_FLOOR`), less with every sample, until a full
`SAMPLES_KEPT` of them stand behind it (#416).

A model with no sample yet is seeded from the chat's own recorded calls where
there are any (`recorded_samples`): the `sent` record's stored messages and
tool menu, measured as a live request is, beside the count the server reported
for them. Only a model with no such evidence starts at the cautious default.

Best-effort like `rate-limits.json`: a missing, unreadable or garbled file is
"nothing learned yet", and a file that cannot be written is not worth failing
a model call over.
"""

from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import NamedTuple

from . import atomic_write, backends, turns
from .paths import state_home

LEDGER_NAME = "token-ratios.json"
# Recent enough to follow a changed system prompt or tool menu, long enough
# that one unusual request does not decide the next twenty.
SAMPLES_KEPT = 20
# A model never seen before. Below every ratio measured on mi, on purpose.
DEFAULT_CHARS_PER_TOKEN = 2.0
# What ONE sample's ratio is multiplied by before it is handed out; the
# discount shrinks linearly to none at SAMPLES_KEPT samples, the unchanged
# min-of-recent. Measured 2026-09-26 over every `local:` call on this machine
# that has its request stored (387 calls, 37 chats, all
# mlx-community/Qwen3.6-35B-A3B-8bit, in this module's measure rebuilt from
# the `sent` records): 3.416 to 4.169 chars per token, so the lowest is 0.819
# of the highest. One sample taken anywhere in that range, times 0.80, is
# below every ratio measured, which is what "one sample at the top of the
# range must not be trusted as the minimum" (#416) needs. In the order the
# calls happened, the previous call's ratio alone under-counted the next
# request by up to 7.8%, and the min of the previous 20 by up to 5.0%: that
# last is the unchanged behaviour, and what stands against it is the 5%
# `LOCAL_PROMPT_SAFETY` margin, not this.
SPARSE_FLOOR = 0.80
# How many of a chat's recorded calls a cold ledger is seeded with. They are
# correlated (one chat, often one task), so they count as FEW samples under
# the taper however many there were: 5 hands out the densest of them times
# 0.842, below the 0.939 that the previous 3 to 15 calls' minimum reached
# against the next call, in call order, on the 387 measured. The densest is
# always among them, because the newest calls need not hold it: in
# `session-20260925-204008-294943` the newest 20 bottom out at 3.826 and an
# older call at 3.615, 5.5% denser, more than the 5% LOCAL_PROMPT_SAFETY.
SEEDED_SAMPLES = 5
# The most recorded calls rebuilt to find that densest one. Cost, not
# evidence: 90 calls of ~190k characters took 0.4 s on this machine, so the
# first estimate of a very long chat waits about a second at most.
SEED_REBUILD_CAP = 200
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
        source = f"learned:min_of_{len(samples)}"
        trust = sparse_trust(len(samples))
        if trust < 1.0:
            source += f"*SPARSE_FLOOR_taper:{trust:.3f}"
        return Ratio(lowest * trust, source)


def sparse_trust(count: int) -> float:
    """How much of the lowest of `count` samples to use: `SPARSE_FLOOR` for
    one, rising linearly to all of it at `SAMPLES_KEPT`."""
    if count >= SAMPLES_KEPT:
        return 1.0
    step = (1.0 - SPARSE_FLOOR) / (SAMPLES_KEPT - 1)
    return SPARSE_FLOOR + step * (max(1, count) - 1)


def has_samples(key: str) -> bool:
    with _lock:
        _load()
        return bool(_samples.get(key))


def seed(key: str, samples: list[tuple[int, int]]) -> int:
    """Adopt recorded (chars, tokens) pairs, oldest first, for a model with
    none yet; how many were kept. A model that has samples keeps its own.

    At most `SEEDED_SAMPLES` are kept: the DENSEST of all of them, so the
    floor is the chat's whole history and not just its latest calls, and the
    newest others. Kept few on purpose, so they stand as sparse evidence under
    the taper until live calls join them (see `SEEDED_SAMPLES`)."""
    kept = [(chars, tokens) for chars, tokens in samples if _plausible(chars, tokens)]
    if not kept:
        return 0
    densest = min(range(len(kept)), key=lambda i: kept[i][0] / kept[i][1])
    newest = [i for i in range(len(kept)) if i != densest][-(SEEDED_SAMPLES - 1):]
    chosen = [list(kept[i]) for i in sorted([densest, *newest])]
    with _lock:
        _load()
        if _samples.get(key):
            return 0
        _samples[key] = chosen
        snapshot = json.dumps(_samples, indent=1)
        count = len(_samples[key])
    try:
        atomic_write.publish(_path(), snapshot)
    except OSError:
        pass
    return count


def recorded_samples(
    log: Path, state_dir: os.PathLike | str, provider: str, model: str,
    limit: int = SEED_REBUILD_CAP,
) -> list[tuple[int, int]]:
    """(request chars, reported prompt tokens) for the newest `limit` calls in
    one chat log to `provider:model` whose whole request is still in the
    chat's store, in the order they were made. The cap is on cost only: each
    rebuild reads and re-hashes every message of that request.

    The characters are rebuilt from the `sent` record — the messages exactly
    as the adapter handed them to the client, and the tool menu — and measured
    with `backends.payload_chars`, the measure `request_chars` makes live. Not
    the record's own `chars`: that is the canonical serialisation (no spaces,
    unescaped non-ASCII, options included), a different number for the same
    request. The count is the next `reasoning` record's `usage.input` for the
    same model call, and only where the server reports its whole prompt there
    (`INPUT_INCLUDES_CACHE`). A call with a message scrubbed of a secret or
    stripped of media is skipped: what is stored is not what was sent. So is
    any call whose bytes are gone, and a log that cannot be read is no
    evidence at all — never an error.
    """
    try:
        lines = Path(log).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    calls: list[tuple[dict, int]] = []
    sent: dict | None = None
    for line in lines:
        if '"sent"' not in line and '"reasoning"' not in line:
            continue
        try:
            step = json.loads(line).get("step")
        except (ValueError, AttributeError):
            continue
        if not isinstance(step, dict):
            continue
        if step.get("kind") == "sent":
            sent = step
            continue
        if step.get("kind") != "reasoning" or sent is None:
            continue
        request, sent = sent, None
        if request.get("model_call") != step.get("model_call"):
            continue
        if request.get("provider") != provider or request.get("model") != model:
            continue
        usage = step.get("usage")
        if not isinstance(usage, dict) or usage.get("semantics") != backends.INPUT_INCLUDES_CACHE:
            continue
        tokens = usage.get("input")
        if type(tokens) is int and tokens > 0:
            calls.append((request, tokens))
    found: list[tuple[int, int]] = []
    for request, tokens in reversed(calls):
        if len(found) >= limit:
            break
        try:
            chars = _rebuilt_chars(request, log, state_dir)
        except ValueError:
            chars = None
        if chars is not None and _plausible(chars, tokens):
            found.append((chars, tokens))
    return found[::-1]


def _rebuilt_chars(request: dict, log: Path, state_dir: os.PathLike | str) -> int | None:
    messages = []
    for entry in request.get("messages") or []:
        if not isinstance(entry, dict) or entry.get("scrubbed") or entry.get("media"):
            return None
        blob = turns.get(str(entry.get("digest")), state_dir, log)
        if blob is None:
            return None
        messages.append(json.loads(blob))
    if not messages:
        return None
    tools: list = []
    menu = request.get("tools")
    if menu is not None:
        if not isinstance(menu, dict) or menu.get("scrubbed"):
            return None
        blob = turns.get(str(menu.get("digest")), state_dir, log)
        if blob is None:
            return None
        tools = json.loads(blob)
    return backends.payload_chars(messages, tools)


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
