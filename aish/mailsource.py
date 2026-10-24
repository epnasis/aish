"""The mailbox seam behind `aish-mailwatch` (#142 W5 S1.5): typed failures,
quota discipline, the top-up ledger, and the Gmail source.

**Failures are TYPED at the runner seam.** Before this, every failed `gws`
call came back as `None` — a quota refusal, a revoked grant, a timeout and a
garbled reply were one event, so a retry policy had nothing to decide on.
`Outcome` carries a `Failure` whose `kind` is one of a closed set
(`FAILURE_KINDS`), and `classify_gws` is the one place a `gws` exit becomes
one. The words it matches are recorded in `matched`, so a string-matched
guess is never mistaken for a status the provider sent (`docs/rate-limits.md`
§2, the same discipline).

**One retry policy, and the adapter owns it.** `GwsMailbox._call`
(`mailwatch`) is the only place a `gws` call is re-issued, after a
`TokenBucket` wait and a classified backoff. A quota refusal is retried; a
permanent refusal (an auth failure, a 404) never is — retrying it spends a
quota unit to relearn the answer. An unknown failure is NOT retried: that is
the behaviour before this file existed, and an unknown is not evidence that
another call helps.

**The top-up ledger** (`UnfetchedLedger`) records every LISTED mail whose
fetch failed, so a later pass fetches exactly those — before it, the misses
were counted and forgotten, and a mail outside the next pass's window was
never read at all. It is keyed by provider id, so it is GMAIL-ONLY: the Tuta
gateway renumbers its UIDs on restart (`mailtuta`), and a remembered UID
would name a different message.

`docs/mail-assistant.md` §Ingress.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atomic_write, mailindex

# ---------------------------------------------------------------- typed failures

QUOTA = "quota"  # 429 / a rate or quota refusal: waiting can help
PERMANENT = "permanent"  # 4xx: the same call is refused the same way forever
TIMEOUT = "timeout"  # the call did not finish in time
TRANSIENT = "transient"  # 5xx / a transport failure: a re-issue may succeed
PARSE = "parse"  # it exited 0 and its output was not what the seam reads
MISSING = "missing"  # the binary, a credential or a config is not there
UNKNOWN = "unknown"  # failed, and nothing above matched
FAILURE_KINDS = frozenset({QUOTA, PERMANENT, TIMEOUT, TRANSIENT, PARSE, MISSING, UNKNOWN})


class ReadOnlyViolation(RuntimeError):
    """A mailbox call outside a read-only allowlist. Raised before any process
    is spawned: this is the enforcing line for "the watcher makes read-only
    calls", for `gws` (`mailwatch.READ_ONLY_CALLS`) and for himalaya
    (`mailtuta.check_argv`)."""


@dataclass(frozen=True)
class Failure:
    kind: str
    status: int | None = None
    matched: str = ""
    text: str = ""
    scope: str = ""  # for QUOTA: "minute" (waitable) or "day" (not), else ""

    def __post_init__(self) -> None:
        if self.kind not in FAILURE_KINDS:
            raise ValueError(f"unknown failure kind {self.kind!r}")

    @property
    def retryable(self) -> bool:
        if self.kind == QUOTA:
            return self.scope != "day"
        return self.kind in (TIMEOUT, TRANSIENT)

    def describe(self) -> str:
        """What was observed, in one line: the kind, the status if one was
        read, and the words it was matched on. Never a cause."""
        out = self.kind
        if self.status is not None:
            out += f" {self.status}"
        if self.matched:
            out += f" ({self.matched})"
        snippet = " ".join(self.text.split())[:160]
        return f"{out}: {snippet}" if snippet else out


@dataclass(frozen=True)
class Outcome:
    """What one runner call returned: a parsed value, or a typed failure."""

    value: Any = None
    failure: Failure | None = None


_STATUS = re.compile(
    r'"code"\s*:\s*(\d{3})\b|\bHTTP[/\s]*(?:\d\.\d\s+)?(\d{3})\b|\bstatus\W{0,3}(\d{3})\b', re.I
)
# Gmail says a rate refusal as 429 OR as 403 with a rate reason
# (`rateLimitExceeded`, `userRateLimitExceeded`); the words win over the 403.
_QUOTA_WORDS = re.compile(
    r"\b429\b|rate[\s_-]*limit|ratelimitexceeded|quota|units\s+per\s+minute|per[\s_-]*minute"
    r"|too\s+many\s+requests|resource[\s_-]*exhausted|dailylimitexceeded",
    re.I,
)
_DAY_WORDS = re.compile(r"per[\s_-]*day|daily", re.I)
_PERMANENT_WORDS = re.compile(
    r"unauthori[sz]ed|forbidden|invalid[_\s-]*grant|not\s+found|permission|invalid\s+argument"
    r"|no\s+credentials|insufficient",
    re.I,
)
_TRANSIENT_WORDS = re.compile(
    r"backend\s+error|internal\s+error|unavailable|connection|reset\s+by\s+peer|timed?\s*out",
    re.I,
)


def _status_in(text: str) -> int | None:
    found = _STATUS.search(text)
    if not found:
        return None
    return int(next(g for g in found.groups() if g))


def classify_gws(returncode: int, stderr: str, stdout: str = "") -> Failure:
    """A failed `gws` exit, typed. Status first where one can be read; the
    words second, with what matched recorded."""
    haystack = f"{stderr}\n{stdout[:2000]}"
    status = _status_in(haystack)
    if status == 429 or (match := _QUOTA_WORDS.search(haystack)):
        matched = "status:429" if status == 429 else match.group(0)  # type: ignore[union-attr]
        day = _DAY_WORDS.search(haystack)
        return Failure(QUOTA, status, matched, stderr, "day" if day else "minute")
    if status is not None and 400 <= status < 500 and status != 408:
        return Failure(PERMANENT, status, f"status:{status}", stderr)
    if status is not None and (status >= 500 or status == 408):
        return Failure(TRANSIENT, status, f"status:{status}", stderr)
    if match := _PERMANENT_WORDS.search(haystack):
        return Failure(PERMANENT, None, match.group(0), stderr)
    if match := _TRANSIENT_WORDS.search(haystack):
        return Failure(TRANSIENT, None, match.group(0), stderr)
    return Failure(UNKNOWN, None, f"rc={returncode}", stderr)


# ---------------------------------------------------------------- quota discipline

# Gmail's documented per-user ceiling is far above this. The watcher's real
# limit was met as a per-minute refusal during the 2026-10-24 backtest (116 of
# 862 listed mails not fetched), with the gws OAuth client shared by every
# gmail tool on the machine. These numbers are CONSERVATIVE CHOICES, not
# measurements: two calls a second sustained, five in a burst.
GMAIL_CALLS_PER_S = 2.0
GMAIL_BURST = 5.0

# Attempts per call, per failure kind. A quota refusal gets the most: a
# per-minute window reopens inside the backoff below. A timeout or a 5xx gets
# one re-issue.
MAX_ATTEMPTS = {QUOTA: 4, TIMEOUT: 2, TRANSIENT: 2}
QUOTA_BACKOFF_S = 10.0
OTHER_BACKOFF_S = 2.0
BACKOFF_CAP_S = 60.0


def backoff_s(failure: Failure, attempt: int) -> float:
    """How long to wait before attempt `attempt + 1`."""
    base = QUOTA_BACKOFF_S if failure.kind == QUOTA else OTHER_BACKOFF_S
    return min(base * (2 ** max(0, attempt - 1)), BACKOFF_CAP_S)


def should_retry(failure: Failure, attempt: int) -> bool:
    return failure.retryable and attempt < MAX_ATTEMPTS.get(failure.kind, 1)


class TokenBucket:
    """A plain token bucket. `take` blocks (through the `sleep` seam) until a
    token is there, so every caller of the adapter is paced, not just the
    careful ones."""

    def __init__(
        self,
        rate: float = GMAIL_CALLS_PER_S,
        burst: float = GMAIL_BURST,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate, self.burst = rate, burst
        self.clock, self.sleep = clock, sleep
        self.tokens = burst
        self.stamp = clock()
        self.waited_s = 0.0

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now

    def take(self) -> None:
        self._refill()
        if self.tokens < 1.0:
            wait = (1.0 - self.tokens) / self.rate
            self.waited_s += wait
            self.sleep(wait)
            self._refill()
            self.tokens = max(self.tokens, 1.0)
        self.tokens -= 1.0


# ---------------------------------------------------------------- the top-up ledger

LEDGER_NAME = "unfetched.json"
# A listed mail that fails this many fetches is dropped from the ledger and
# counted as abandoned: a mail that cannot be fetched twenty times running is
# not going to be, and an unbounded ledger is a slow pass forever.
LEDGER_MAX_ATTEMPTS = 20
LEDGER_MAX_ENTRIES = 5000


class UnfetchedLedger:
    """Listed-but-unfetched mail, per source, persisted between passes. One
    writer (the pass); published whole through `atomic_write`."""

    def __init__(self, state_dir: os.PathLike | str) -> None:
        self.path = mailindex.mail_dir(state_dir) / LEDGER_NAME
        self.entries: dict[str, dict[str, dict[str, Any]]] = {}
        self.abandoned: dict[str, int] = {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            for source, held in (raw.get("sources") or {}).items():
                if isinstance(held, dict):
                    self.entries[str(source)] = {
                        str(k): v for k, v in held.items() if isinstance(v, dict)
                    }

    def pending(self, source: str) -> list[tuple[str, str]]:
        """(provider id, role) still owed a fetch, oldest first."""
        held = self.entries.get(source, {})
        ordered = sorted(held.items(), key=lambda kv: float(kv[1].get("first_at") or 0))
        return [(pid, str(entry.get("role") or "inbox")) for pid, entry in ordered]

    def missed(self, source: str, pid: str, role: str, failure: Failure, now: float) -> None:
        held = self.entries.setdefault(source, {})
        entry = held.setdefault(pid, {"role": role, "first_at": now, "attempts": 0})
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["last"] = failure.describe()[:200]
        entry["last_at"] = now
        if failure.kind == PERMANENT or entry["attempts"] >= LEDGER_MAX_ATTEMPTS:
            del held[pid]
            self.abandoned[source] = self.abandoned.get(source, 0) + 1
        elif len(held) > LEDGER_MAX_ENTRIES:
            oldest = min(held, key=lambda k: float(held[k].get("first_at") or 0))
            del held[oldest]
            self.abandoned[source] = self.abandoned.get(source, 0) + 1

    def done(self, source: str, pid: str) -> None:
        self.entries.get(source, {}).pop(pid, None)

    def count(self, source: str) -> int:
        return len(self.entries.get(source, {}))

    def save(self) -> None:
        atomic_write.publish(
            Path(self.path), json.dumps({"sources": self.entries}, sort_keys=True)
        )


# ---------------------------------------------------------------- the Gmail source


@dataclass
class Listing:
    inbox: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)


class GmailSource:
    """The pass's view of a Gmail mailbox: any object with `list` / `get` in
    the `GwsMailbox` shape (the tests' `FakeMailbox` included).

    Provider ids are STABLE here, which is what licenses both the top-up
    ledger and skipping a fetch for an id the index already holds."""

    name = "gmail"
    stable_ids = True

    def __init__(self, mailbox: Any) -> None:
        self.mailbox = mailbox
        self.last_failure: Failure | None = None

    def _failure(self, fallback: str) -> Failure:
        failure = getattr(self.mailbox, "last_failure", None)
        return failure if isinstance(failure, Failure) else Failure(UNKNOWN, text=fallback)

    def listing(self, days: int, limit: int) -> Listing | None:
        inbox = self.mailbox.list(f"in:inbox newer_than:{days}d", limit)
        sent = self.mailbox.list(f"in:sent newer_than:{days}d", limit)
        if inbox is None or sent is None:
            self.last_failure = self._failure("the listing returned nothing")
            return None
        return Listing(list(inbox), list(sent))

    def held(self, ref: str, index: mailindex.MailIndex, by_pid: dict[str, str]) -> str | None:
        return by_pid.get(ref)

    def fetch(
        self, ref: str, owners: frozenset[str]
    ) -> tuple[mailindex.Parsed | None, Failure | None]:
        if getattr(self.mailbox, "quota_exhausted", False):
            # The adapter already spent its retries on a quota refusal this
            # pass: every further call would buy the same refusal. The rest go
            # to the ledger for the next pass.
            return None, Failure(QUOTA, matched="exhausted earlier this pass")
        msg = self.mailbox.get(ref)
        if msg is None:
            return None, self._failure("the fetch returned nothing")
        return mailindex.from_gmail(msg, owners), None

    def note(self) -> str:
        return ""
