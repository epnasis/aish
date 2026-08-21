"""Provider rate limits: classify a failed model call, and pace calls so the
next one does not fail the same way.

Two things live here, and they are one module because the second is a function
of the first.

**Classification.** `agent._chat_turn` used to catch every exception from a
model call identically — echo "retrying once…", re-issue the identical request
immediately, and if that failed too, raise. A busy local Ollama, an exhausted
cloud quota, a malformed request and a wrong API key were the same event. For a
429 that is worse than doing nothing: the retry re-sends the same (in the
incident that named this module, ~120k-token) request microseconds later, so it
spends more of the quota it just ran out of, and the SDK underneath was ALSO
retrying, so one visible "retrying once…" was six HTTP requests. Nothing about
any of that reached the log — see `docs/rate-limits.md`.

Classification is deliberately structural and provider-agnostic: every SDK aish
speaks to (openai, anthropic, ollama) hangs a `status_code` on its exceptions
and a `response.headers` mapping behind it, so duck-typing those reaches all
three without importing any of them. String matching is the FLOOR, not the
mechanism — it catches a transport wrapper that lost the status, and it records
what it matched on so a reader can see the verdict was a guess.

**The retry hint, and its source.** A 429 may carry a wait in three places: a
`Retry-After` header (seconds or an HTTP date), a `retry-after-ms` header, or
Google's `RetryInfo` in the error body. They do not always agree and are often
all absent, and "we waited 2s because nobody told us anything" and "we waited
2s because the provider said 2s" are different facts about the same wait. So
`retry_after_source` is recorded next to the number, per the trace contract's
evidence-not-conclusions rule.

**Scope is the field that matters most and is easiest to miss.** A per-minute
quota can be waited out; a per-day one cannot. Retrying into a daily quota for
the rest of a task is a way to burn every remaining request without ever
completing a call. The distinction is not in the status code — both are 429 —
only in the quota id or the prose, so it is parsed, marked as the guess it is
(`matched`), and treated conservatively: a wait longer than `LONG_WAIT_S` is
handled as a stop-worthy limit even when nothing named a day.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

# What went wrong, in the only vocabulary the retry policy needs. Closed set:
# a value outside it is a bug, not a new case to handle downstream.
RATE_LIMIT = "rate_limit"
AUTH = "auth"
BAD_REQUEST = "bad_request"
SERVER = "server"
TRANSPORT = "transport"
CANCELLED = "cancelled"
UNKNOWN = "unknown"

# Which quota ran out. "" when the failure is not a quota at all.
SHORT = "short"  # per-second/per-minute: waiting it out works
LONG = "long"  # per-day/per-project: waiting it out does not work

# A hint above this is not a pause, it is a closed door. Providers express a
# daily quota as a retry hint of hours; sleeping on one inside a task means the
# task never ends and the user watches a spinner instead of reading an error.
LONG_WAIT_S = 300.0

# Ceiling on any hint aish will actually honour. A provider is free to say
# "24h"; aish still has to answer the user this minute.
MAX_WAIT_S = 60.0

_DAY_QUOTA = re.compile(r"per[\s_-]*day|daily|perday", re.I)
_MINUTE_QUOTA = re.compile(r"per[\s_-]*(?:minute|second)|perminute|persecond", re.I)
_RATE_WORDS = re.compile(
    r"\b429\b|rate[\s_-]*limit|resource_exhausted|quota|too many requests", re.I
)
_AUTH_WORDS = re.compile(r"\b401\b|\b403\b|unauthorized|forbidden|invalid[\s_-]*api", re.I)
_TRANSPORT_WORDS = re.compile(
    r"timed?[\s_-]*out|timeout|connection|econnreset|broken pipe|temporarily unavailable"
    r"|remote end closed|ssl",
    re.I,
)


@dataclass(frozen=True)
class CallFailure:
    """One failed model call, classified. Every field is evidence a reader can
    re-examine — including `matched`, which says what the verdict was a
    function of, so a string-matched guess is never mistaken for a status the
    provider actually sent."""

    kind: str
    retryable: bool
    status: int | None = None
    retry_after_s: float | None = None
    retry_after_source: str = "none"  # header | header_ms | header_date | body | none
    scope: str = ""
    matched: str = ""
    text: str = ""

    @property
    def is_rate_limit(self) -> bool:
        return self.kind == RATE_LIMIT

    @property
    def exhausted(self) -> bool:
        """The quota cannot be waited out inside this task: a named daily quota,
        or a hint so long that honouring it is indistinguishable from hanging."""
        if self.kind != RATE_LIMIT:
            return False
        if self.scope == LONG:
            return True
        return (self.retry_after_s or 0) > LONG_WAIT_S

    def record(self) -> dict:
        """The evidence half of a `model_error` trace record. Only fields that
        were actually established appear — an absent `status` means nobody
        looked or nobody said, and writing 0 there would be a claim."""
        out: dict = {"class": self.kind, "retryable": self.retryable}
        if self.status is not None:
            out["status"] = self.status
        if self.retry_after_s is not None:
            out["retry_after_s"] = round(self.retry_after_s, 3)
            out["retry_after_source"] = self.retry_after_source
        if self.scope:
            out["scope"] = self.scope
        if self.matched:
            out["matched"] = self.matched
        return out


def _status_of(exc: BaseException) -> int | None:
    """Every SDK aish speaks to puts the status somewhere in this chain; none of
    them is imported to read it."""
    for holder in (exc, getattr(exc, "response", None)):
        if holder is None:
            continue
        for attr in ("status_code", "status"):
            value = getattr(holder, attr, None)
            if isinstance(value, int) and 100 <= value < 600:
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None


def _headers_of(exc: BaseException) -> dict:
    raw = getattr(getattr(exc, "response", None), "headers", None)
    if raw is None:
        raw = getattr(exc, "headers", None)
    if raw is None:
        return {}
    try:
        return {str(k).lower(): str(v) for k, v in dict(raw).items()}
    except (TypeError, ValueError):
        return {}


def _walk(value: object, depth: int = 0):
    """Every dict nested anywhere in an error body. Google buries `RetryInfo`
    under `error.details[]`, and the OpenAI-compat gateway re-wraps it at a
    depth that is not worth hard-coding a path to."""
    if depth > 6:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item, depth + 1)


def _duration(text: str) -> float | None:
    """Google durations are strings like `27s` or `1.5s`."""
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*s?\s*", str(text))
    return float(match.group(1)) if match else None


def retry_hint(exc: BaseException, now: float | None = None) -> tuple[float | None, str]:
    """(seconds to wait, where that came from). Header first — it is the one the
    HTTP layer guarantees — then the body, which is the only place Google's
    compat gateway sometimes puts it."""
    headers = _headers_of(exc)
    if (raw := headers.get("retry-after-ms")) is not None:
        try:
            return max(0.0, float(raw) / 1000.0), "header_ms"
        except ValueError:
            pass
    if (raw := headers.get("retry-after")) is not None:
        try:
            return max(0.0, float(raw)), "header"
        except ValueError:
            try:  # the other legal Retry-After spelling: an HTTP date
                when = parsedate_to_datetime(raw)
                return max(0.0, when.timestamp() - (now or time.time())), "header_date"
            except (TypeError, ValueError):
                pass
    for node in _walk(getattr(exc, "body", None)):
        for key in ("retryDelay", "retry_delay"):
            if key in node and (secs := _duration(node[key])) is not None:
                return secs, "body"
    return None, "none"


def _scope_of(haystack: str) -> tuple[str, str]:
    """(scope, what matched). Day is checked first: a Gemini quota id names both
    windows in one string often enough that minute-first would mislabel a daily
    quota as waitable — the one error here that costs a whole task."""
    if match := _DAY_QUOTA.search(haystack):
        return LONG, match.group(0)
    if match := _MINUTE_QUOTA.search(haystack):
        return SHORT, match.group(0)
    return "", ""


def classify(exc: BaseException, now: float | None = None) -> CallFailure:
    """What kind of failure this was, and whether re-issuing it could help.

    Status first, prose second, and the prose match is recorded so the two are
    never confused for each other by a later reader.
    """
    text = str(exc)
    status = _status_of(exc)
    body_text = repr(getattr(exc, "body", "")) if getattr(exc, "body", None) else ""
    haystack = f"{text} {body_text}"
    retry_after, source = retry_hint(exc, now)

    if status == 429 or (status is None and _RATE_WORDS.search(haystack)):
        scope, matched = _scope_of(haystack)
        return CallFailure(
            kind=RATE_LIMIT,
            # A daily quota is not retryable in any sense the loop can act on:
            # the caller must stop and say so, not sleep for six hours.
            retryable=scope != LONG,
            status=status,
            retry_after_s=retry_after,
            retry_after_source=source,
            scope=scope,
            matched=matched
            or ("status:429" if status == 429 else _match_text(_RATE_WORDS, haystack)),
            text=text,
        )
    if status in (401, 403) or (status is None and _AUTH_WORDS.search(haystack)):
        return CallFailure(
            kind=AUTH, retryable=False, status=status,
            matched=f"status:{status}" if status else _match_text(_AUTH_WORDS, haystack),
            text=text,
        )
    if status is not None and 400 <= status < 500 and status not in (408, 409, 429):
        # A request the provider will reject identically forever. Retrying it
        # spends a request against the quota to learn nothing.
        return CallFailure(kind=BAD_REQUEST, retryable=False, status=status,
                           matched=f"status:{status}", text=text)
    if status is not None and status >= 500 or status in (408, 409):
        return CallFailure(kind=SERVER, retryable=True, status=status,
                           retry_after_s=retry_after, retry_after_source=source,
                           matched=f"status:{status}", text=text)
    if _TRANSPORT_WORDS.search(haystack):
        return CallFailure(kind=TRANSPORT, retryable=True, status=status,
                           matched=_match_text(_TRANSPORT_WORDS, haystack), text=text)
    # Unknown is retryable ON PURPOSE: the pre-existing behaviour was to retry
    # everything once, and a classifier that silently stopped retrying a case it
    # failed to recognise would be a regression disguised as a refinement.
    return CallFailure(kind=UNKNOWN, retryable=True, status=status, text=text)


def _match_text(pattern: re.Pattern, haystack: str) -> str:
    match = pattern.search(haystack)
    return match.group(0) if match else ""


def backoff_delay(failure: CallFailure, attempt: int, base: float = 1.0) -> float:
    """How long to wait before attempt N+1, in seconds.

    The provider's own hint wins whenever it gave one — it is the only number
    derived from the real quota window rather than from a guess — capped at
    MAX_WAIT_S so a wild hint cannot hang a task. Otherwise exponential from
    `base`, which for a rate limit starts an order of magnitude above the SDK
    default: the old immediate retry was not merely useless against a 429, it
    was actively harmful.
    """
    if failure.retry_after_s is not None:
        return min(max(0.0, failure.retry_after_s), MAX_WAIT_S)
    if failure.kind == RATE_LIMIT:
        base = max(base, 5.0)
    return min(base * (2 ** max(0, attempt - 1)), MAX_WAIT_S)


# How often an interruptible wait re-checks the stop flag and re-draws the
# countdown. Short enough that Stop feels immediate, long enough that a
# minute-long wait is not a thousand wake-ups.
WAIT_TICK_S = 0.5


def wait(delay: float, stop: threading.Event, note: Callable[[str], None] | None = None) -> bool:
    """Sleep `delay` seconds, interruptibly. True if `stop` was set.

    THE ONE PLACE aish sleeps between model attempts, which is what makes it
    patchable suite-wide: a test that exercises the retry policy must not spend
    real seconds proving the policy waits, and a per-test patch would be
    per-test discipline over a module that really sleeps — the shape the
    notifier guard already rejected (`tests/conftest.py`).

    `stop` is the agent's own cancel Event, so a Stop pressed during a
    27-second rate-limit wait lands as fast as one pressed mid-stream. A bare
    `time.sleep` would make the Stop button a lie for exactly the stretch a
    user is most likely to press it.
    """
    if delay <= 0:
        return stop.is_set()
    deadline = time.monotonic() + delay
    while (left := deadline - time.monotonic()) > 0:
        if note is not None:
            # The wait is the longest thing a task does without producing a
            # step, so a chat that goes quiet for half a minute says why while
            # it is happening rather than afterwards.
            note(f"rate-limited — retrying in {left:.0f}s")
        if stop.wait(min(WAIT_TICK_S, left)):
            return True
    return stop.is_set()
