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

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

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
    # False when aish declined to make the call. Nothing was sent, so nothing
    # was charged, and a reader must not count it as the provider refusing —
    # the two look identical in a bare error string and mean opposite things
    # about whose budget just moved.
    sent: bool = True

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
        if not self.sent:
            out["sent"] = False
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
    if isinstance(exc, Cancelled):
        return CallFailure(kind=CANCELLED, retryable=False, matched="stopped",
                           sent=False, text=str(exc))
    if isinstance(exc, RateLimited):
        # aish's own refusal, not the provider's. Never retryable here: the
        # governor already decided this cannot be sent, and re-asking it in a
        # tight loop is the behaviour it exists to replace.
        return CallFailure(
            kind=RATE_LIMIT, retryable=False, scope=LONG, matched="governor",
            retry_after_s=exc.retry_after_s,
            retry_after_source="governor" if exc.retry_after_s else "none",
            sent=False, text=str(exc),
        )
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
            note(f"Rate-limited — retrying in {left:.0f}s")
        if stop.wait(min(WAIT_TICK_S, left)):
            return True
    return stop.is_set()


# ---------------------------------------------------------------- the governor

# A rate window is a minute everywhere aish looks; providers express RPM and TPM
# over exactly this span.
WINDOW_S = 60.0

# What a request is worth before anyone has counted its tokens. Requests-per-minute
# and tokens-per-minute are separate ceilings and either can bind first.
IMAGE_TOKENS = 1_400  # a flat stand-in: images are char-invisible and token-huge

# How much of the rate that produced a 429 the governor will allow afterwards.
# Below 1.0 because the observation is "this failed AT this rate" — the true
# ceiling is at or under it, never above.
OBSERVED_MARGIN = 0.9

# Longest a caller will queue for headroom before being told to give up instead.
# An unattended session gets a much shorter one (see `hooks`): it occupies a
# bounded worker thread, and a chat nobody is watching must not park one for
# minutes while a user's own action queues behind it.
DEFAULT_WAIT_CEILING_S = 120.0


class Cancelled(RuntimeError):
    """The user stopped while the call was queued for headroom. Never a provider
    failure and never retryable — the caller translates it to its own cancel
    path so a Stop is not reported as the backend being unavailable."""


class RateLimited(RuntimeError):
    """The call was not made: the quota is spent, or waiting for it would take
    longer than anyone is prepared to wait.

    Distinct from a provider 429 on purpose — nothing was sent, so nothing was
    charged, and the caller must not report this as the provider refusing.
    """

    def __init__(self, message: str, *, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


def estimate_tokens(messages: list | None) -> int:
    """What a request is about to cost, before the provider counts it.

    chars/3 is the same convention `agent._history_budget` uses. Images are
    added separately because they are CHAR-INVISIBLE and token-huge — a
    vision-heavy history estimated on text alone under-reserves by thousands of
    tokens per image, and every delivered image is re-encoded into every later
    request.

    Deliberately an over-estimate where it is unsure. Reserving too much costs
    some throughput; reserving too little is the 429 this exists to prevent.
    """
    chars = 0
    images = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        chars += len(content) if isinstance(content, str) else 0
        images += len(message.get("images") or [])
        for call in message.get("tool_calls") or []:
            chars += len(str(call))
    return chars // 3 + images * IMAGE_TOKENS


# How long a key must go without a refusal before aish starts loosening what it
# believes, and by how much per quiet stretch.
#
# A published per-tier number would justify learning once and holding it. A real
# key does not behave like that: a pay-as-you-go key on a shared organisation
# quota has neighbours, so the ceiling genuinely MOVES between one hour and the
# next, and a limit learned at 23:00 is not a fact about 09:00. Holding a
# once-observed number forever converts a temporary squeeze into a permanent
# self-imposed one, which is the failure mode of believing your own model over
# the server.
#
# So the belief decays toward optimism while the server keeps saying yes, and
# snaps back the moment it says no. There is no synthetic probe and there is no
# cost to this: the thing that tests the loosened ceiling is the next call the
# owner was making anyway. Occasionally that costs a 429 — which is now paced,
# recorded and legible, rather than the invisible slam it used to be.
RELAX_AFTER_S = 600.0
RELAX_FACTOR = 1.25


@dataclass
class Limits:
    """A key's ceilings, where they came from, and how old the evidence is.

    `source` is not decoration. aish cannot know which billing tier a key is on,
    so the honest default is NO ceiling — throttling a paid key on a guess would
    be a self-inflicted outage. A 429 is the moment the world tells us, and a
    reader has to be able to tell a limit we were given from one we inferred
    from a failure. Mirrors `context_window`'s provenance discipline.

    `refused_at` is wall-clock on purpose: it has to survive a restart, because
    the whole point of learning a ceiling is not paying to learn it twice.
    """

    rpm: int | None = None
    tpm: int | None = None
    source: str = "none"  # none | env | observed
    refused_at: float = 0.0
    relaxed: float = 1.0  # how far the belief has loosened since that refusal

    def record(self) -> dict:
        out: dict = {"limit_source": self.source}
        if self.rpm is not None:
            out["rpm"] = self.rpm
        if self.tpm is not None:
            out["tpm"] = self.tpm
        if self.relaxed > 1.0:
            out["relaxed"] = round(self.relaxed, 2)
        return out

    def as_json(self) -> dict:
        return {"rpm": self.rpm, "tpm": self.tpm, "source": self.source,
                "refused_at": self.refused_at}

    def relax(self, now: float) -> Limits:
        """This belief, loosened for however long the server has not refused.

        Stepwise rather than continuous so the value is stable between steps: a
        ceiling that crept up every second would make two calls a second apart
        answerable differently for no reason a reader could reconstruct.
        """
        if self.source != "observed" or not self.refused_at:
            return self  # stated by the owner, or nothing learned yet
        quiet = max(0.0, now - self.refused_at)
        steps = int(quiet // RELAX_AFTER_S)
        if steps <= 0:
            return self
        factor = RELAX_FACTOR**steps
        return Limits(
            rpm=None if self.rpm is None else max(1, int(self.rpm * factor)),
            tpm=None if self.tpm is None else max(1, int(self.tpm * factor)),
            source=self.source,
            refused_at=self.refused_at,
            relaxed=factor,
        )


class _Window:
    """Requests and tokens spent in the last `WINDOW_S`, and nothing older.

    Entries are mutable after the fact: a reservation is admitted on an ESTIMATE
    and corrected to the provider's actual when the call returns. Keeping the
    entry (rather than debiting and refunding) is what makes an abandoned stream
    err safe — usage arrives on the final chunk, so a stream nobody finished
    never reports, and its estimate must stand rather than silently vanish.
    """

    def __init__(self) -> None:
        self.entries: list[list[float]] = []  # [when, tokens]

    def prune(self, now: float) -> None:
        cutoff = now - WINDOW_S
        self.entries = [e for e in self.entries if e[0] > cutoff]

    def totals(self, now: float) -> tuple[int, int]:
        self.prune(now)
        return len(self.entries), int(sum(e[1] for e in self.entries))

    def add(self, now: float, tokens: int) -> list[float]:
        entry = [now, float(tokens)]
        self.entries.append(entry)
        return entry


class Reservation:
    """Headroom granted for one call, and the correction owed afterwards.

    Admitted on an estimate, corrected on the way out. The three outcomes are
    NOT symmetric, and the asymmetry is the point:

    - `settle(actual)` — the call returned and the provider counted it. Replace
      the estimate with the truth.
    - `rejected()` — the provider refused (429). Keep the REQUEST debit, drop
      the tokens: the request happened and plausibly counts against RPM/RPD, but
      tokens it never processed should not crowd out the retry.
    - neither — a stream nobody finished, or a call that timed out. The estimate
      STANDS. Usage arrives on the final chunk, so an abandoned stream reports
      nothing, and the server may well have generated the whole response; eating
      the estimate is the only direction that errs safe.
    """

    def __init__(self, governor: Governor, key: str, entry: list[float], estimate: int):
        self.key = key
        self.estimate = estimate
        self._governor = governor
        self._entry = entry
        self._closed = False

    def settle(self, actual: int | None) -> None:
        with self._governor._lock:
            if not self._closed and actual:
                self._entry[1] = float(actual)
            self._closed = True
            self._governor._cond.notify_all()

    def rejected(self) -> None:
        with self._governor._lock:
            if not self._closed:
                self._entry[1] = 0.0
            self._closed = True
            self._governor._cond.notify_all()


class Governor:
    """One process's model-call pacing, shared by every consumer of a key.

    PROCESS-GLOBAL, not per chat, and that is the whole design. aish-web runs
    many sessions as worker threads in one process against ONE API key, so the
    quota is shared whether or not the code modelling it is; a governor per
    session would be N independent programs each convinced it had the whole
    budget. The same argument reaches further than the agent loop —
    `server._model_session_title` calls `agent.chat` directly, outside
    `run_task`, and swallowed its 429s — which is why enforcement lives at the
    backend adapter seam rather than in `_chat_turn`.

    Keyed by `provider:model`, not provider: quotas are per model on the tiers
    this matters for, and one process genuinely mixes models on one key.

    **FIFO.** Waiters are served in arrival order. This is a choice with a cost:
    one greedy 120k-token reservation delays everyone behind it. The alternative
    starves it forever — a stream of small calls (retitles, a second short
    session) can drain a bucket indefinitely while a large request never finds
    headroom — and on one owner's machine, head-of-line blocking is the better
    failure.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        store: Path | None = None,
    ):
        # Two clocks, deliberately. The rolling window needs a monotonic one (a
        # clock jump must not appear to spend or refund a minute of quota); the
        # learned ceilings need a wall one, because they outlive the process and
        # "how long since the server last refused" is a question about the world
        # rather than about this run.
        self._clock = clock
        self._wall = wall
        self._store = store
        self._loaded = False
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._windows: dict[str, _Window] = {}
        self._limits: dict[str, Limits] = {}
        # Per key: when a quota that CANNOT be waited out inside a task frees up.
        # In monotonic seconds; None once it has passed.
        self._exhausted_until: dict[str, float] = {}
        self._next_ticket = 0
        self._waiting: set[int] = set()

    # -- what we believe the ceilings are ---------------------------------

    def limits(self, key: str) -> Limits:
        """What to enforce right now: the belief, loosened for however long the
        server has not refused."""
        return self.believed(key).relax(self._wall())

    def believed(self, key: str) -> Limits:
        """The stored belief, unrelaxed. Separate from `limits` so a reader (and
        the persisted file) sees what was actually learned rather than a number
        that moves with the clock."""
        with self._lock:
            self._load()
            if key not in self._limits:
                self._limits[key] = _limits_from_env(key)
            return self._limits[key]

    def observe(self, key: str, failure: CallFailure) -> None:
        """Learn from a refusal. The only moment the world states the ceiling.

        A 429 means the model of the world is wrong, so it is corrected ONCE, to
        just under the rate that produced the failure — not nudged, and never
        re-probed upward. TCP's additive increase is right when the limit is
        unknown, dynamic and contended by strangers; here it is published,
        static, per-model, and contended only by yourself, and every re-probe
        costs a full request against the quota it is probing.
        """
        if not failure.is_rate_limit:
            return
        with self._lock:
            now = self._clock()
            if failure.exhausted:
                # Not a pause. Nothing sent between now and the reset can
                # succeed, so later callers are refused immediately rather than
                # queueing behind a wait nobody would sit through.
                #
                # Wall time, and persisted: `make ship` restarts the server and
                # restart recovery re-runs interrupted triggered sessions, so an
                # in-memory latch meant a spent daily quota was re-slammed on
                # every restart by the very sessions it had already refused.
                wait = failure.retry_after_s or LONG_WAIT_S
                self._exhausted_until[key] = self._wall() + wait
                self._save()
                self._cond.notify_all()
                return
            requests, tokens = self._window(key).totals(now)
            current = self.believed(key)
            if current.source == "env":
                # The owner stating their tier outranks aish inferring one.
                return
            self._limits[key] = Limits(
                # Against the EFFECTIVE ceiling, not the stored one: if the
                # belief had loosened to 1.5x and that is what just got refused,
                # the new evidence is about the loosened number. Tightening
                # against the stale stored value would ratchet down forever and
                # never converge on what the server is actually allowing.
                rpm=_tightest(self.limits(key).rpm, max(1, int(requests * OBSERVED_MARGIN))),
                tpm=_tightest(self.limits(key).tpm, max(1, int(tokens * OBSERVED_MARGIN))),
                source="observed",
                refused_at=self._wall(),
            )
            self._save()
            self._cond.notify_all()

    def exhausted_for(self, key: str) -> float | None:
        """Seconds until this key's spent quota resets, or None."""
        with self._lock:
            self._load()
            return self._exhausted_left(self._wall(), key)

    # -- admission --------------------------------------------------------

    def reserve(
        self,
        key: str,
        estimate: int,
        should_stop: Callable[[], bool] | None = None,
        on_wait: Callable[[str], None] | None = None,
        ceiling: float | None = None,
    ) -> Reservation:
        """Block until this call fits, then debit it.

        Raises `RateLimited` when it never will — a spent quota, a request
        larger than the whole ceiling, or a wait longer than the caller's
        patience — and `Cancelled` when the user stopped.

        Debited PESSIMISTICALLY, at the estimate, before the call. Optimistic
        accounting lets a burst of concurrent calls overshoot together — and
        that overshoot IS the 429 this exists to prevent.
        """
        ceiling = DEFAULT_WAIT_CEILING_S if ceiling is None else ceiling
        deadline = self._clock() + ceiling
        with self._cond:
            self._load()  # a spent quota learned by the LAST run still counts
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiting.add(ticket)
            try:
                while True:
                    if should_stop is not None and should_stop():
                        raise Cancelled(f"{key}: stopped while waiting for headroom")
                    now = self._clock()
                    if (left := self._exhausted_left(now, key)) is not None:
                        raise RateLimited(
                            f"{key}: this quota is spent rather than busy — it does not "
                            f"reset for about {left / 60:.0f} min, so nothing was sent",
                            retry_after_s=left,
                        )
                    wait_for = None
                    if ticket == min(self._waiting):
                        wait_for = self._headroom_wait(now, key, estimate)
                        if wait_for is None:
                            return self._grant(now, key, estimate)
                    if now >= deadline:
                        raise RateLimited(
                            f"{key}: waited {ceiling:.0f}s for rate-limit headroom and "
                            "gave up rather than queue any longer — nothing was sent"
                        )
                    if on_wait is not None and wait_for:
                        # The provider, not `provider:model`: the model name is
                        # the governor's bookkeeping, and the user is waiting on
                        # a quota they think of by provider.
                        provider = key.split(":", 1)[0]
                        on_wait(f"Waiting on the {provider} rate limit — about {wait_for:.0f}s")
                    self._cond.wait(min(WAIT_TICK_S, max(0.01, deadline - now)))
            finally:
                self._waiting.discard(ticket)
                self._cond.notify_all()

    # -- internals --------------------------------------------------------

    def _window(self, key: str) -> _Window:
        return self._windows.setdefault(key, _Window())

    def _exhausted_left(self, _now: float, key: str) -> float | None:
        """Wall time, not the window's monotonic clock: this deadline is
        persisted and outlives the process that set it."""
        until = self._exhausted_until.get(key)
        if until is None:
            return None
        left = until - self._wall()
        if left <= 0:
            del self._exhausted_until[key]
            return None
        return left

    def _headroom_wait(self, now: float, key: str, estimate: int) -> float | None:
        """None if the call fits right now; otherwise roughly how long until it
        might. Raises if it can never fit.
        """
        limits = self.limits(key)
        if limits.rpm is None and limits.tpm is None:
            return None  # no ceiling is known, so nothing is being enforced
        if limits.tpm is not None and estimate > limits.tpm:
            # A naive "wait until it fits" would block forever here. The caller
            # is told to make the request SMALLER, which is the one thing that
            # can work — and is the seam where pacing meets the real fix.
            raise RateLimited(
                f"{key}: this call is {estimate:,} tokens and the whole per-minute "
                f"budget is {limits.tpm:,} — it cannot be sent at any rate. "
                "Shorten the conversation."
            )
        window = self._window(key)
        requests, tokens = window.totals(now)
        over_rpm = limits.rpm is not None and requests + 1 > limits.rpm
        over_tpm = limits.tpm is not None and tokens + estimate > limits.tpm
        if not over_rpm and not over_tpm:
            return None
        # The window is rolling, so headroom returns when the OLDEST entry ages
        # out. That is the earliest moment worth re-checking.
        oldest = window.entries[0][0] if window.entries else now
        return max(0.0, oldest + WINDOW_S - now)

    def _grant(self, now: float, key: str, estimate: int) -> Reservation:
        entry = self._window(key).add(now, estimate)
        return Reservation(self, key, entry, estimate)

    # -- what survives a restart ------------------------------------------

    def _path(self) -> Path | None:
        if self._store is not None:
            return self._store
        root = os.environ.get("AISH_STATE_DIR")
        return Path(root) / "rate-limits.json" if root else None

    def _load(self) -> None:
        """Learned ceilings and the spent-quota latch, from the last run.

        A ceiling costs a 429 to learn, so learning it once per RESTART rather
        than once is paying repeatedly for the same information — and `make
        ship` restarts the server often. Best-effort by design: a missing or
        unreadable file means "nothing learned yet", which is exactly the state
        a first run is in, so there is nothing here worth failing a model call
        over.
        """
        if self._loaded:
            return
        self._loaded = True
        path = self._path()
        if path is None or not path.is_file():
            return
        try:
            stored = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        for key, value in (stored.get("limits") or {}).items():
            if not isinstance(value, dict) or key in self._limits:
                continue
            self._limits[key] = Limits(
                rpm=value.get("rpm"), tpm=value.get("tpm"),
                source=str(value.get("source") or "observed"),
                refused_at=float(value.get("refused_at") or 0.0),
            )
        for key, until in (stored.get("exhausted_until") or {}).items():
            self._exhausted_until[key] = float(until)

    def _save(self) -> None:
        path = self._path()
        if path is None:
            return
        payload = {
            # The BELIEF, never the relaxed view: writing a number that moves
            # with the clock would make the file disagree with itself the moment
            # it was read back.
            "limits": {k: v.as_json() for k, v in self._limits.items() if v.source == "observed"},
            "exhausted_until": dict(self._exhausted_until),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2))
        except OSError:
            pass  # a report aish cannot write is never worth failing a call for


def _tightest(current: int | None, observed: int) -> int:
    """The lower of what we believed and what we just saw. A second 429 must
    never RAISE a ceiling — the only evidence a refusal carries is an upper
    bound."""
    return observed if current is None else min(current, observed)


def _limits_from_env(key: str) -> Limits:
    """`AISH_RATE_LIMIT_GEMINI=rpm=10,tpm=250000` — per provider or per
    `provider:model`, the more specific winning.

    Empty by default ON PURPOSE. aish cannot know which billing tier a key is
    on, and throttling a paid key on a guessed free-tier number would be a
    self-inflicted outage. So the shipped model of the world is "no ceiling
    known", the owner can state theirs, and a 429 corrects it either way.
    """
    provider = key.split(":", 1)[0]
    for name in (f"AISH_RATE_LIMIT_{key}", f"AISH_RATE_LIMIT_{provider}"):
        raw = os.environ.get(name.upper().replace("-", "_").replace(".", "_"), "").strip()
        if not raw:
            continue
        values: dict[str, int] = {}
        for part in raw.split(","):
            field, _, number = part.partition("=")
            if field.strip().lower() in ("rpm", "tpm") and number.strip().isdigit():
                values[field.strip().lower()] = int(number)
        if values:
            return Limits(rpm=values.get("rpm"), tpm=values.get("tpm"), source="env")
    return Limits()


_GOVERNOR: Governor | None = None
_GOVERNOR_LOCK = threading.Lock()


def governor() -> Governor:
    """The process's one governor. Lazily built so importing this module costs
    nothing and tests can replace it."""
    global _GOVERNOR
    with _GOVERNOR_LOCK:
        if _GOVERNOR is None:
            _GOVERNOR = Governor()
        return _GOVERNOR


def reset_governor(instance: Governor | None = None) -> Governor:
    """Replace the process governor. Tests only — a shared rolling window that
    outlived one test would make the next one's behaviour depend on its
    neighbours."""
    global _GOVERNOR
    with _GOVERNOR_LOCK:
        _GOVERNOR = instance if instance is not None else Governor()
        return _GOVERNOR


# ------------------------------------------------------- per-call UX wiring

# What an UNATTENDED session will queue for. Far shorter than a user's own, and
# not for politeness: a background or triggered session occupies a thread from
# the server's bounded worker pool, and that pool exists precisely so a session
# parked on an approval cannot starve short user actions. A session parked on
# rate-limit headroom would re-create the same hazard inside the pool.
UNATTENDED_WAIT_CEILING_S = 20.0

_HOOKS = threading.local()


@dataclass(frozen=True)
class Hooks:
    """Cancel and status wiring for whatever call this thread is about to make."""

    should_stop: Callable[[], bool] | None = None
    on_wait: Callable[[str], None] | None = None
    ceiling: float | None = None


class hooks:  # noqa: N801 — used as a context manager, reads as one
    """Attach cancel/status wiring to this thread's model calls.

    Thread-local rather than a parameter because the parameter would have to
    travel through the chat callable, and every backend is adapted to the EXACT
    `ollama.chat` calling convention so `agent.py` never learns which provider
    it is on. Adding a keyword for the governor's benefit would break the one
    invariant that keeps the backends interchangeable.

    A thread is the right scope by construction: the agent's worker thread is
    exactly the span of one session's calls. A caller that sets nothing — the
    retitler, `curate`, a test — gets bounded default behaviour rather than
    blocking forever, which is the correct answer for work nobody is watching.
    """

    def __init__(self, should_stop=None, on_wait=None, ceiling=None):
        self._hooks = Hooks(should_stop, on_wait, ceiling)
        self._previous: Hooks | None = None

    def __enter__(self) -> Hooks:
        self._previous = getattr(_HOOKS, "current", None)
        _HOOKS.current = self._hooks
        return self._hooks

    def __exit__(self, *_exc) -> None:
        _HOOKS.current = self._previous


def current_hooks() -> Hooks:
    return getattr(_HOOKS, "current", None) or Hooks()


def reserve_for_call(key: str, messages: list | None) -> Reservation:
    """Estimate, then reserve, using whatever wiring this thread supplied."""
    wiring = current_hooks()
    return governor().reserve(
        key,
        estimate_tokens(messages),
        should_stop=wiring.should_stop,
        on_wait=wiring.on_wait,
        ceiling=wiring.ceiling,
    )
