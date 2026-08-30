"""How often each word list was asked, and how often it matched (#322).

aish decides a lot by matching words against text a page or a person wrote: is
this the login button, is this thing covering the control a cookie banner, is
this command safe to run unasked, does this label say the button spends money.
Each of those is a tuple of a few dozen strings, and **not one of them counted**
— so a list that had silently stopped matching was indistinguishable from a page
that had nothing to match. On 2026-08-26 `_CONSENT_SELECTORS` missed a real
banner by one letter (`Akceptuj` against the page's `Akceptuję`), the sign-in it
was covering failed for days, and nothing anywhere said a list had found
nothing. `docs/vocabularies.md`.

**This module MEASURES. It decides nothing.** Nothing reads a counter to change
behaviour, and no list here has been widened, narrowed or reordered by its
arrival — a record is detection and never protection (#295 P2), and a counter
even less so. That is also what makes the numbers a usable before-picture: they
were taken against the matching this repository shipped, not against matching
this module changed.

Three pieces, and they are deliberately separate:

- **The catalogue** — every list that counts, its size, and the ENGINEER'S
  verdict on what a miss costs. That verdict is authored, not measured, and is
  labelled as such everywhere it is printed.
- **The tallies** — asked, matched, and (where the call site already knows it)
  how many candidates the consultation was choosing among. Process-lifetime,
  drained onto the session log once per task by `Agent._flush_vocab`.
- **The reader** — `scan_counters` over the recorded `vocab` records, in the
  same pure-pass shape `roles.scan_counters` and `curate.scan_ledger` already
  use (`docs/trace-contract.md` §7). No model call, no live state; it reports
  what was RECORDED.

**Absence is not zero, and the three states are kept apart** (the discipline
`usage.NOT_RECORDED` and the sign-in verdict tri-state already follow). A list
in the catalogue with no record in the window was NOT CONSULTED — a different
fact from one consulted and never matching, routing to a different repair: the
first says the code path never ran, the second says the words are wrong.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

# ------------------------------------------------------------------ verdicts
#
# What an unmatched item costs. Three outcomes rather than two, because the
# failure that started this (#321) is neither of the usual pair: it permitted
# nothing and it cost no friction, it silently disabled a feature, and that is
# the one nobody goes looking for.

#: An unmatched item is NOT gated — something happens that a match would have
#: stopped or slowed. Fail-open toward consequence.
PERMITS = "permits"
#: An unmatched item costs a prompt, a refusal or an extra step. Fail-closed:
#: being wrong here is paid in the owner's time, never in his money.
FRICTION = "friction"
#: An unmatched item quietly disables a feature. Neither of the above, and the
#: #321 shape — the one a counter exists for, because nothing else reports it.
BREAKS = "breaks"

VERDICTS = (PERMITS, FRICTION, BREAKS)


@dataclass(frozen=True)
class Vocabulary:
    """One declared word list.

    `on_miss` and `structural` are the ENGINEER'S reading, recorded here so the
    reader can print it beside the count — a list asked 200 times and matching
    nothing means something very different depending on which of the three it
    is. They are dated by the commit that wrote them and are NOT measurements;
    every renderer that prints them says so.
    """

    name: str
    size: int
    languages: str
    on_miss: str
    #: The check that needs no words and would still hold if every string here
    #: were deleted, or "" where there is none. `is_challenge`'s `BLOCK_STATUS`
    #: is the worked example the epic points at.
    structural: str = ""
    note: str = ""


CATALOGUE: dict[str, Vocabulary] = {}


def declare(
    name: str,
    entries: Sequence[str],
    *,
    languages: str,
    on_miss: str,
    structural: str = "",
    note: str = "",
) -> tuple[str, ...]:
    """Register a list and hand back its entries unchanged.

    Returns the SAME strings, as a tuple, so a declaration cannot alter what is
    matched — that is the whole point of this slice, and a wrapper object that
    could subtly change iteration or membership would destroy the before-picture
    it exists to take.
    """
    if on_miss not in VERDICTS:
        raise ValueError(f"{name}: on_miss must be one of {VERDICTS}, got {on_miss!r}")
    if name in CATALOGUE:
        raise ValueError(f"{name}: already declared")
    CATALOGUE[name] = Vocabulary(
        name=name,
        size=len(entries),
        languages=languages,
        on_miss=on_miss,
        structural=structural,
        note=note,
    )
    return tuple(entries)


# -------------------------------------------------------------------- tallies


@dataclass
class _Tally:
    asked: int = 0
    matched: int = 0
    #: Summed only over the consultations that reported one. Kept with its own
    #: denominator so a mean is never taken over consultations that could not
    #: say — an average candidate count computed over a zero for every silent
    #: call site would be a number the log cannot support.
    candidates: int = 0
    candidates_asked: int = 0


_lock = threading.Lock()
_tallies: dict[str, _Tally] = {}


def note(name: str, *, matched: bool, candidates: int | None = None) -> None:
    """One consultation of `name`, and whether it matched.

    For call sites whose matching is not a plain substring scan — a selector
    list handed to Chrome, a cookie jar walked by name, a list comprehension
    that needs the hits themselves.
    """
    with _lock:
        tally = _tallies.get(name)
        if tally is None:
            tally = _tallies[name] = _Tally()
        tally.asked += 1
        tally.matched += int(bool(matched))
        if candidates is not None:
            tally.candidates += int(candidates)
            tally.candidates_asked += 1


def hit(
    name: str,
    needles: Sequence[str],
    text: str,
    *,
    candidates: int | None = None,
) -> bool:
    """`any(needle in text for needle in needles)`, counted.

    Byte-for-byte the expression it replaces at every call site, including the
    short-circuit: the first needle that matches ends the scan, exactly as the
    generator did. The counting is two integer increments behind one lock, and
    the lock is what makes a browser thread's consultation and the loop
    thread's not lose each other's updates.
    """
    matched = any(needle in text for needle in needles)
    note(name, matched=matched, candidates=candidates)
    return matched


def drain() -> dict[str, dict[str, int]]:
    """Everything tallied since the last drain, and reset.

    Only lists that were actually consulted appear. A list nobody asked is
    ABSENT rather than a row of zeros — the record then says what happened, and
    "never consulted" stays distinguishable from "consulted and never matched"
    all the way to the reader.
    """
    with _lock:
        out = {
            name: {
                "asked": tally.asked,
                "matched": tally.matched,
                **(
                    {"candidates": tally.candidates, "candidates_asked": tally.candidates_asked}
                    if tally.candidates_asked
                    else {}
                ),
            }
            for name, tally in _tallies.items()
            if tally.asked
        }
        _tallies.clear()
        return out


def reset() -> None:
    """Drop everything tallied so far. For tests, which must not inherit
    another test's consultations."""
    with _lock:
        _tallies.clear()


# --------------------------------------------------------------- the counters


@dataclass
class Counters:
    """What one list actually did, from the log alone.

    A pure scan in the shape `roles.scan_counters` already uses: no model call,
    no live state, reading only records written at the time. It reports what was
    RECORDED — a list with no records reads as no consultations, never as a list
    that behaved well.
    """

    vocabulary: str = ""
    asked: int = 0
    matched: int = 0
    candidates: int = 0
    candidates_asked: int = 0
    #: How many separate task records contributed. A list asked 200 times in one
    #: task and one asked twice in a hundred are different corpora, and a single
    #: session's browsing of a single broken site should not read as a verdict
    #: on the list.
    records: int = 0

    @property
    def rate(self) -> float:
        return self.matched / self.asked if self.asked else 0.0

    @property
    def mean_candidates(self) -> float | None:
        """None, never 0, when no call site reported a candidate count."""
        if not self.candidates_asked:
            return None
        return self.candidates / self.candidates_asked


def scan_counters(records) -> dict[str, Counters]:
    """`records` is any iterable of decoded log lines (see `explain._records`)."""
    out: dict[str, Counters] = {}
    for record in records:
        step = record.get("step") if isinstance(record, dict) else None
        if not isinstance(step, dict) or step.get("kind") != "vocab":
            continue
        for name, counted in (step.get("lists") or {}).items():
            if not isinstance(counted, dict):
                continue
            counters = out.setdefault(str(name), Counters(vocabulary=str(name)))
            counters.records += 1
            counters.asked += int(counted.get("asked") or 0)
            counters.matched += int(counted.get("matched") or 0)
            counters.candidates += int(counted.get("candidates") or 0)
            counters.candidates_asked += int(counted.get("candidates_asked") or 0)
    return out


# ---------------------------------------------------------------- the anomaly
#
# "Recorded always, surfaced on anomaly." The owner has twice rejected reporting
# that fires on ordinary use, and a counter that warns on every browse is one he
# learns to skip — which would cost more than no counter at all.


def floor(counters: dict[str, Counters]) -> float | None:
    """The rarest match rate any list in this window actually achieved.

    None when NO list matched anything, and that is the honest answer rather
    than a default: a window in which nothing ever matched cannot tell you which
    list is broken — it might be a window with no browsing in it. Nothing is
    flagged from a floor that could not be derived.

    Derived from the window's own data rather than chosen, because a constant
    picked here would be the guess this whole file exists to stop making. It IS
    a weak instrument and the docstring says so: it is the rate of the least
    successful list that still works, which is a property of whatever happened
    to be consulted, not of the web.
    """
    rates = [c.rate for c in counters.values() if c.matched]
    return min(rates) if rates else None


def quiet(counters: dict[str, Counters]) -> list[Counters]:
    """Lists that matched NOTHING, where the window's own data says they should
    have matched something.

    The test, and it has exactly one number in it: at the rarest rate any
    working list in this window achieved, this list would have been expected to
    match **at least once**, and it matched zero. Ordered by how many expected
    matches it is missing, so the list asked 200 times with nothing to show is
    first.

    What this does NOT claim, stated because the temptation is to read it as a
    verdict: it is not "this list is broken". A list can be legitimately silent
    — `_CLOSE_ACCOUNT_PHRASES` should match nothing on almost every page there
    is. What it says is that a list was asked more often than the corpus's own
    worst working rate needs, and did not fire; whether that is correct is read
    off the catalogue's `on_miss` verdict and the code, never off this number.
    """
    rarest = floor(counters)
    if rarest is None:
        return []
    missing = [
        (c.asked * rarest, c)
        for c in counters.values()
        if not c.matched and c.asked * rarest >= 1
    ]
    return [c for _, c in sorted(missing, key=lambda pair: -pair[0])]


def expected_at_floor(counters: dict[str, Counters], one: Counters) -> float | None:
    """How many matches `one` would have had at the window's rarest working
    rate. None when no floor could be derived."""
    rarest = floor(counters)
    return None if rarest is None else one.asked * rarest


def never_consulted(counters: dict[str, Counters]) -> list[Vocabulary]:
    """Declared lists with no record in this window at all.

    Not zero — absent. The repair is different: a list nobody asked has a code
    path that did not run (or a window with none of that work in it), and no
    amount of editing its strings would change the number."""
    return [v for name, v in sorted(CATALOGUE.items()) if name not in counters]


def summary_line(counters: dict[str, Counters]) -> str:
    """One line for a report the owner reads for another reason, or "".

    Silent unless something is anomalous, for the reason `ConsentTally.line`
    already gives: a status row that always says nothing happened is one the eye
    stops reading. It names the lists and points at the full table rather than
    printing it — this is a pointer, not the report.
    """
    quieted = quiet(counters)
    if not quieted:
        return ""
    named = ", ".join(f"{c.vocabulary} ({c.asked} asked)" for c in quieted[:3])
    more = f" +{len(quieted) - 3} more" if len(quieted) > 3 else ""
    return (
        f"{len(quieted)} word list{'' if len(quieted) == 1 else 's'} matched nothing "
        f"where this window's rarest working rate expected a match: {named}{more} "
        "— `aish vocab`"
    )


# ----------------------------------------------------------------- the report

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"

_MISS_WORDS = {
    PERMITS: "a miss PERMITS something",
    FRICTION: "a miss costs friction",
    BREAKS: "a miss silently breaks a feature",
}


def render(counters: dict[str, Counters], days: int | None) -> str:
    """Which lists matched, which went quiet, and which were never asked."""
    window = "all recorded chats" if days is None else f"the last {days} days"
    lines = [f"{BOLD}word lists{RESET}  {DIM}{window}{RESET}", ""]
    if not counters:
        lines += [
            "  no chat in this window recorded a word-list consultation.",
            f"{DIM}  That is not the same as no list matching: a log written before"
            f" #322 carries no `vocab` record at all.{RESET}",
        ]
    else:
        rarest = floor(counters)
        quieted = {c.vocabulary for c in quiet(counters)}
        lines.append(
            f"  {'':<34} {'asked':>8} {'matched':>8} {'rate':>7}  what a miss costs"
        )
        for name, c in sorted(counters.items(), key=lambda kv: (kv[1].matched > 0, kv[0])):
            declared = CATALOGUE.get(name)
            verdict = _MISS_WORDS.get(declared.on_miss, "not declared") if declared else (
                "not in the catalogue"
            )
            mark = " ⚑" if name in quieted else ""
            lines.append(
                f"  {name:<34} {c.asked:>8} {c.matched:>8} {c.rate:>6.0%}  {verdict}{mark}"
            )
            if declared and declared.structural:
                lines.append(f"    {DIM}structural check under it: {declared.structural}{RESET}")
            if (mean := c.mean_candidates) is not None:
                lines.append(
                    f"    {DIM}chose among {mean:.1f} candidate(s) on average, over "
                    f"{c.candidates_asked} consultation(s){RESET}"
                )
        if quieted and rarest is not None:
            lines += [
                "",
                f"  {BOLD}⚑ matched nothing{RESET}, where the rarest rate any working list in "
                f"this window achieved ({rarest:.1%})",
                f"    expected at least one match:",
            ]
            for c in quiet(counters):
                expected = c.asked * rarest
                lines.append(
                    f"      {c.vocabulary}: {c.asked} consultation(s), "
                    f"~{math.floor(expected)} expected, 0 matched"
                )
            lines.append(
                f"{DIM}    This is a pointer, not a verdict. A list can be correctly silent"
                f" — read what a miss{RESET}"
            )
            lines.append(
                f"{DIM}    costs above, then the code. docs/vocabularies.md.{RESET}"
            )
        elif rarest is None:
            lines += [
                "",
                f"{DIM}  No list matched anything in this window, so there is no observed rate"
                f" to judge{RESET}",
                f"{DIM}  a silent list against. Nothing is flagged — a window with no matches"
                f" at all is{RESET}",
                f"{DIM}  as likely to be a window with no browsing in it.{RESET}",
            ]
    absent = never_consulted(counters)
    if absent:
        lines += [
            "",
            f"  {BOLD}not consulted{RESET} in this window {DIM}(absent, never zero — the code"
            f" path did not run){RESET}",
        ]
        for v in absent:
            lines.append(f"    {v.name:<34} {DIM}{v.size} entries · {v.languages}{RESET}")
    lines += [
        "",
        f"{DIM}  asked/matched are MEASURED. 'what a miss costs' is an engineer's verdict"
        f" recorded{RESET}",
        f"{DIM}  in the catalogue, not a measurement — docs/vocabularies.md carries the"
        f" reasoning.{RESET}",
    ]
    return "\n".join(lines)


def json_report(counters: dict[str, Counters], days: int | None) -> dict:
    rarest = floor(counters)
    return {
        "days": days,
        # Absent rather than 0 when no list matched: a consumer must not read
        # "no rate could be derived" as "the rarest rate is zero".
        **({"floor_rate": rarest} if rarest is not None else {}),
        "lists": {
            name: {
                "asked": c.asked,
                "matched": c.matched,
                "records": c.records,
                **(
                    {"candidates": c.candidates, "candidates_asked": c.candidates_asked}
                    if c.candidates_asked
                    else {}
                ),
                **(
                    {"declared": {"size": d.size, "languages": d.languages,
                                  "on_miss": d.on_miss, "structural": d.structural}}
                    if (d := CATALOGUE.get(name))
                    else {}
                ),
            }
            for name, c in sorted(counters.items())
        },
        "quiet": [c.vocabulary for c in quiet(counters)],
        "not_consulted": [v.name for v in never_consulted(counters)],
    }
