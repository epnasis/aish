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
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

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
    #: False for a list that is INVENTORIED but whose call sites are not yet
    #: instrumented. It must be a field rather than an omission: a list left out
    #: of the catalogue entirely would be invisible, and one listed as counted
    #: while nothing counts it would read as "never consulted" forever — a
    #: confident lie about a list that runs on every command.
    counted: bool = True
    #: Is every consultation a DEMAND — does aish ask this only once it already
    #: needs the answer to carry on, so that a miss aborts something in flight?
    #:
    #: This is the fact no count can derive, and `browse._FORWARD` is why the
    #: field exists. It sat at 7 asked / 0 matched for a month against `breaks`
    #: and `quiet` stayed silent — correctly by its own rule, because at the
    #: rarest working rate in that window 7 asks expect 0.09 matches and the
    #: threshold is 1. It would have needed about 78 asks to trip, and a date
    #: picker is consulted seven times a month. A statistical floor cannot see a
    #: list this rare, ever.
    #:
    #: But it does not have to. `_FORWARD` is asked only from inside a month
    #: walk that has already decided it needs an arrow, so seven asks were seven
    #: walks and zero matches were seven ABORTED walks — every consultation a
    #: failure, at any exposure. `_DOWNLOAD_WORDS` is the opposite and the
    #: reason this is not simply "breaks + zero": it is asked about every link
    #: on every page, so 24 asked / 0 matched means there were no downloads,
    #: which is the correct answer and not a defect.
    #:
    #: `None` is not a default, it is an unanswered question, and `declare`
    #: REFUSES a `breaks` list that leaves it unanswered — the verdict has to be
    #: read off the call site by a person, exactly like `on_miss`.
    demanded: bool | None = None
    note: str = ""


CATALOGUE: dict[str, Vocabulary] = {}

_Entries = TypeVar("_Entries")


def declare(
    name: str,
    entries: _Entries,
    *,
    languages: str,
    on_miss: str,
    structural: str = "",
    counted: bool = True,
    demanded: bool | None = None,
    note: str = "",
) -> _Entries:
    """Register a list in the catalogue and hand back **the same object**.

    Identity, not a copy and not a wrapper: a `frozenset` stays a frozenset (so
    membership stays O(1) and set algebra keeps working) and a tuple stays a
    tuple in its original order. This slice's whole value is a before-picture of
    the matching this repository already shipped, and a declaration that could
    subtly change iteration, ordering or membership would destroy it.
    """
    if on_miss not in VERDICTS:
        raise ValueError(f"{name}: on_miss must be one of {VERDICTS}, got {on_miss!r}")
    # A `breaks` list is the one shape nothing else reports, so leaving the
    # question unanswered is not allowed to be the quiet option.
    if on_miss == BREAKS and demanded is None:
        raise ValueError(
            f"{name}: a 'breaks' list must say whether every consultation is a "
            "DEMAND (demanded=True: aish asks only once it needs the answer, so "
            "a miss aborts something in flight) or speculative (demanded=False: "
            "asked about everything, and matching nothing is often correct). "
            "Read it off the call site — see Vocabulary.demanded."
        )
    entry = Vocabulary(
        name=name,
        size=len(entries),  # type: ignore[arg-type]
        languages=languages,
        on_miss=on_miss,
        structural=structural,
        counted=counted,
        demanded=demanded,
        note=note,
    )
    # Re-declaring the SAME list is a no-op, because a module reload is not a
    # mistake — `importlib.reload` runs the module body again and several tests
    # do exactly that. Re-declaring a DIFFERENT list under a name already taken
    # is, because it would silently merge two lists' counts into one number.
    if (already := CATALOGUE.get(name)) is not None and already != entry:
        raise ValueError(f"{name}: already declared as something else ({already})")
    CATALOGUE[name] = entry
    return entries


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


def looked_up(name: str, table: dict, key: str):
    """`table.get(key)`, counted.

    For the lists that are DICTS keyed on a vocabulary — a lookup is a
    consultation exactly as a substring scan is, and a key that has drifted
    (a standard renaming a value, a site publishing the URL form of an enum)
    is the same silent miss with the same silent cost.
    """
    found = table.get(key)
    note(name, matched=found is not None, candidates=len(table))
    return found


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


def scan(root=None, days: int | None = None) -> dict[str, Counters]:
    """Every chat log's `vocab` records, summed per list.

    Per LIST across the window and never per chat, for the reason
    `usage._role_counters` gives about charters: a match rate is a property of
    the list, and one chat's two consultations say nothing about it. That is
    also what makes the per-task record's attribution unimportant — a
    consultation made between tasks lands on the next record, and no number
    here depends on which one that was.

    One unreadable file must not take the report down with it (the containment
    rule a single corrupt session log taught the web server)."""
    from .explain import _records, state_dir
    from .usage import _within

    directory = Path(root) if root is not None else state_dir()
    if not directory.is_dir():
        return {}
    out: dict[str, Counters] = {}
    for path in sorted(directory.glob("session-*.jsonl")):
        if days is not None and not _within(path.stem, days):
            continue
        try:
            found = scan_counters(_records(path))
        except OSError:
            continue
        for name, c in found.items():
            into = out.setdefault(name, Counters(vocabulary=name))
            into.records += c.records
            into.asked += c.asked
            into.matched += c.matched
            into.candidates += c.candidates
            into.candidates_asked += c.candidates_asked
    return out


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


def floor(counters: dict[str, Counters], over: int = 0) -> float | None:
    """The rarest match rate achieved by a list observed at least as often as
    `over` — the rate a working list in THIS window managed at comparable
    exposure.

    None when no such list exists, and that is the honest answer rather than a
    default. Two ways to get None, and both mean "no claim can be made":

    - **No list matched anything at all.** A window in which nothing ever
      matched cannot tell you which list is broken; it is as likely to be a
      window with no browsing in it.
    - **Nothing comparable was observed.** Judging a list asked 400 times
      against one asked twice is what over-flagging is made of, and the owner
      has twice rejected reporting that fires on ordinary use. A list that is
      the most-consulted thing in the window therefore has nothing to be
      measured against, and is not flagged — an absent comparison, never a
      free pass invented to fill it.

    Derived from the window's own data rather than chosen, because a constant
    picked here would be the guess this whole file exists to stop making. It is
    still a WEAK instrument, and the weakness is stated rather than smoothed
    over: it is the rate of the least successful list that still works, which is
    a property of whatever happened to be consulted and not of the web.
    """
    rates = [c.rate for c in counters.values() if c.matched and c.asked >= over]
    return min(rates) if rates else None


def quiet(counters: dict[str, Counters]) -> list[Counters]:
    """Lists that matched NOTHING, where the window's own data says they should
    have matched something.

    The test, and it has exactly one number in it: at the rarest rate a
    COMPARABLY-CONSULTED working list in this window achieved, this list would
    have been expected to match **at least once**, and it matched zero. Ordered
    by how many expected matches it is missing, so the list asked 200 times with
    nothing to show is first.

    "Comparably consulted" is what keeps this from crying wolf. A list asked
    twice that happened to match once has a 50% rate and no business setting the
    bar for one asked four hundred times; the floor for each candidate is
    therefore taken over working lists asked at least as often as it was
    (`floor(counters, over=c.asked)`), and a candidate with nothing comparable
    is not flagged at all.

    What this does NOT claim, stated because the temptation is to read it as a
    verdict: it is not "this list is broken". A list can be legitimately silent
    — `_CLOSE_ACCOUNT_PHRASES` should match nothing on almost every page there
    is. What it says is that a list was asked more often than the corpus's own
    worst comparable working rate needs, and did not fire; whether that is
    correct is read off the catalogue's `on_miss` verdict and the code, never
    off this number.
    """
    missing = []
    for c in counters.values():
        if c.matched:
            continue
        rarest = floor(counters, over=c.asked)
        if rarest is not None and c.asked * rarest >= 1:
            missing.append((c.asked * rarest, c))
    return [c for _, c in sorted(missing, key=lambda pair: -pair[0])]


def failing(counters: dict[str, Counters]) -> list[Counters]:
    """Lists that were DEMANDED and never once answered — asked at least once,
    matched nothing.

    No threshold, no floor, no comparison to other lists: for a demanded list
    the number that matters is already in the record, because every ask was a
    moment aish had committed to needing an answer. One ask and no match is one
    aborted operation; seven is seven.

    This is the check `quiet` structurally cannot make, and the two are kept
    apart rather than merged. `quiet` asks *"was this list asked often enough
    that silence is surprising"* — a statistical question about exposure, weak
    by its own admission. This asks *"did a list aish leaned on ever hold it"* —
    which needs no statistics at all and is not weakened by rarity. Merging them
    would put a hard fact behind a soft threshold.

    Ordered by asks, most first, so the list that aborted the most work leads.
    """
    out = [
        c for c in counters.values()
        if c.vocabulary
        and (v := CATALOGUE.get(c.vocabulary)) is not None
        and v.demanded
        and c.asked
        and not c.matched
    ]
    return sorted(out, key=lambda c: -c.asked)


def expected_at_floor(counters: dict[str, Counters], one: Counters) -> float | None:
    """How many matches `one` would have had at the rarest rate a comparably
    consulted working list achieved. None when no such list exists."""
    rarest = floor(counters, over=one.asked)
    return None if rarest is None else one.asked * rarest


def never_consulted(counters: dict[str, Counters]) -> list[Vocabulary]:
    """Declared lists with no record in this window at all.

    Not zero — absent. The repair is different: a list nobody asked has a code
    path that did not run (or a window with none of that work in it), and no
    amount of editing its strings would change the number.

    Only COUNTED lists can be here. An inventoried-but-uninstrumented list is
    consulted constantly and simply not counted, and reporting it as "not
    consulted" would be the exact confident lie this module exists to stop —
    `not_counted` is where those are named."""
    return [v for name, v in sorted(CATALOGUE.items()) if v.counted and name not in counters]


def not_counted() -> list[Vocabulary]:
    """Declared lists that are INVENTORIED but not instrumented.

    They are consulted; nothing counts them. Named separately and never mixed
    with the measured rows, because a reader who cannot see which is which
    cannot tell a silent list from an uncounted one."""
    return [v for _, v in sorted(CATALOGUE.items()) if not v.counted]


def summary_line(counters: dict[str, Counters]) -> str:
    """One line for a report the owner reads for another reason, or "".

    Silent unless something is anomalous, for the reason `ConsentTally.line`
    already gives: a status row that always says nothing happened is one the eye
    stops reading. It names the lists and points at the full table rather than
    printing it — this is a pointer, not the report.

    **`failing` leads, because it is the harder claim.** A demanded list that
    never answered is a fact about work that did not happen; `quiet` is a
    statistical eyebrow-raise. Reporting them in the same sentence would rank a
    certainty behind a suspicion — and it is the certainty that went unseen for
    a month.
    """
    broken = failing(counters)
    if broken:
        named = ", ".join(f"{c.vocabulary} ({c.asked} asked)" for c in broken[:3])
        more = f" +{len(broken) - 3} more" if len(broken) > 3 else ""
        return (
            f"{len(broken)} word list{'' if len(broken) == 1 else 's'} aish LEANED ON "
            f"never matched anything: {named}{more} — `aish vocab`"
        )
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
        broken = failing(counters)
        if broken:
            lines += [
                f"  {BOLD}asked for and never answered{RESET} {DIM}(every "
                f"consultation of these is a demand — see Vocabulary.demanded){RESET}",
            ]
            for one in broken:
                lines.append(
                    f"    {one.vocabulary:<34} {one.asked:>8} asked, none matched"
                )
            lines.append("")
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
        if quieted:
            lines += [
                "",
                f"  {BOLD}⚑ matched nothing{RESET}, where a working list consulted at least as "
                "often did match",
                "    — so at that rate, this one expected at least one:",
            ]
            for c in quiet(counters):
                # Per candidate, not one figure for the table: the rate a list
                # is judged against is the rate of the lists observed as often
                # as IT was, and a single headline number would quietly compare
                # a 400-consultation list against a 2-consultation one.
                expected = expected_at_floor(counters, c) or 0.0
                at = floor(counters, over=c.asked) or 0.0
                lines.append(
                    f"      {c.vocabulary}: {c.asked} consultation(s), "
                    f"~{math.floor(expected)} expected at {at:.0%}, 0 matched"
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
        "failing": [
            {"list": c.vocabulary, "asked": c.asked} for c in failing(counters)
        ],
    }
