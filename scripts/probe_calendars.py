#!/usr/bin/env -S uv run python
"""Open real booking sites' date pickers and report what they actually look like.

The calendar support in `browse.py` was designed against fixtures this repo
wrote itself, and one rule in it — never press a cell that says only its day
number — was argued from a two-month range picker nobody had measured. #273
then found, on a real site, that pickers are MIXTURES: dated cells plus bare
furniture. So the fixtures were wrong in a direction the argument did not
anticipate, which is the reason for this script.

It signs into nothing, submits nothing, and presses only the field that opens a
picker. Throwaway profile in a temp state dir, like `verify_browse.py` — this
must never touch the owner's real one.

    uv run python scripts/probe_calendars.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["AISH_STATE_DIR"] = tempfile.mkdtemp(prefix="probe-cal-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aish import browse, browser  # noqa: E402

# (label, url, the field whose name should open a date picker)
SITES = [
    # The POLISH page, deliberately: it is the one the owner books on, and the
    # English one never rendered a date field the probe could find at all — so
    # lot.pl was in this list for a week while its picker went unmeasured, and
    # the arrow shape that defeats the word list was sitting on the other URL.
    ("lot.pl", "https://www.lot.com/pl/pl", "wylot od"),
    ("wizzair", "https://wizzair.com/en-gb", "departure"),
    ("ryanair", "https://www.ryanair.com/gb/en", "depart"),
    ("booking.com", "https://www.booking.com/", "check-in"),
    ("intercity", "https://www.intercity.pl/", "data"),
]

SHOTS = Path(tempfile.mkdtemp(prefix="calendars-"))


def read_calendar(field) -> dict:
    """`CALENDAR_JS` against the live page — the same read `do="date"` makes."""
    async def job(owner):
        session = await browser._session(owner, "", opening=False)
        return await session.page.evaluate(browse.CALENDAR_JS, {"n": field.n}) or {}

    try:
        return browser._submit(job, 30)
    except Exception:  # noqa: BLE001 — a page that will not answer has no picker
        return {}


def shoot(path: Path) -> bool:
    async def job(owner):
        session = await browser._session(owner, "", opening=False)
        await session.page.screenshot(path=str(path), type="jpeg", quality=70)
        return True

    try:
        return bool(browser._submit(job, 30))
    except Exception:  # noqa: BLE001 — a picture is a nicety, not the finding
        return False


# A consent wall arrives AFTER the first read and covers the whole form, so the
# probe reported 'onetrust-pc-dark-filter is sitting on top of it' for lot.pl —
# a finding about a cookie banner, printed where the finding about the picker
# should be. An instrument that cannot get past the front door measures nothing.
CONSENT = ("i agree", "akceptuj", "zgadzam", "accept all", "allow all", "zezwol")


def past_the_consent_wall(snapshot):
    """Press whatever agrees, up to twice — the banner is the probe's problem,
    not the finding. Nothing else on these pages is ever pressed.

    Re-read FIRST, because the wall arrives after the page does: lot.com's
    OneTrust banner is on no control list at load and owns the whole page a
    second later, so a probe that only checked the opening snapshot walked
    straight into it."""
    try:
        snapshot = browser.browse_act("", "read", timeout=60)
    except Exception:  # noqa: BLE001 — a page that will not re-read is data too
        pass
    for _ in range(2):
        one = next(
            (c for c in snapshot.controls
             if any(w in browse.fold(c.name) for w in CONSENT)),
            None,
        )
        if one is None:
            return snapshot
        print(f"  (dismissing {one.address[:40]!r})")
        try:
            snapshot = browser.browse_act(one.address, "click", timeout=60)
        except Exception:  # noqa: BLE001 — a banner that will not go is data too
            return snapshot
    return snapshot


def date_field(snapshot, hint: str):
    """A control that plausibly opens a date picker, by what it SAYS."""
    hint = browse.fold(hint)
    # The hint verbatim first, and only then something date-ish: matching
    # loosely picked lot.pl's "Check-in" button and ryanair's "Travel Updates"
    # link, which is the probe being wrong about the site rather than a finding.
    for wanted in (hint, "wylot", "data wyjazdu"):
        for control in snapshot.controls:
            if browse.fold(wanted) in browse.fold(control.name):
                return control
    return None


def report(label: str, url: str, hint: str) -> None:
    print(f"\n=== {label}  {url}")
    try:
        page = browser.browse_open(url, timeout=90)
    except Exception as exc:  # noqa: BLE001 — a site that will not open is data
        print(f"  could not open: {type(exc).__name__}: {exc}")
        return
    if page.problem:
        print(f"  problem: {page.problem[:120]}")
    page = past_the_consent_wall(page)
    field = date_field(page, hint)
    if field is None:
        print(f"  no date-ish control found among {len(page.controls)}; sample:")
        for control in page.controls[:8]:
            print(f"    {control.line()[:96]}")
        return
    print(f"  pressing {field.line()[:90]}")
    try:
        after = browser.browse_act(field.address, "click", timeout=90)
    except Exception as exc:  # noqa: BLE001
        print(f"  could not press it: {type(exc).__name__}: {exc}")
        return
    if after.problem:
        print(f"  problem: {after.problem[:140]}")

    # The click re-enumerated, so the field's number moved. Its NAME did not.
    live = browse.resolve(after.controls, field.address).control or field
    grid = read_calendar(live)
    if not grid.get("found"):
        print("  NO PICKER aish can find after pressing it")
        return
    cells = [
        browse.Cell(
            tag=int(c.get("tag", 0)), text=str(c.get("text") or ""),
            label=str(c.get("label") or ""), stamp=str(c.get("stamp") or ""),
            disabled=bool(c.get("disabled")),
        )
        for c in (grid.get("cells") or [])
    ]
    heading = str(grid.get("heading") or "")
    stamped = sum(1 for c in cells if c.stamp)
    labelled = sum(1 for c in cells if c.label)
    dated = sum(1 for c in cells if (d := c.day(heading)) and d.month)
    print(f"  heading: {heading[:60]!r}")
    print(f"  cells: {len(cells)}  with data-date: {stamped}  "
          f"with aria-label: {labelled}  resolving to a month: {dated}")
    for c in cells[:4]:
        print(f"    text={c.text[:12]!r} label={c.label[:40]!r} stamp={c.stamp[:16]!r}")
    nav = grid.get("nav") or []
    arrows = [
        n for n in nav
        if browse.month_step(str(n.get("name") or ""), forward=True)
        or browse.month_step(str(n.get("name") or ""), forward=False)
    ]
    # Both halves, separately. The word list matching nothing while the walk
    # still works is exactly what this instrument now has to be able to show:
    # lot.com's arrows are called `October 2026` and no list can ever hold that.
    named = {
        str(n.get("name") or ""): browse.month_arrow(str(n.get("name") or ""))
        for n in nav
    }
    named = {k: v for k, v in named.items() if v}
    print(f"  nav candidates: {len(nav)}; matched by the word list: {len(arrows)}; "
          f"read as a month: {named}")
    # What the PAGE says its months are called, and what aish derives from it.
    said = grid.get("months") or {}
    table = browse.month_table(said)
    print(f"  page locales: {list(said)}; derived stems: "
          f"{[t[0] if t else '' for t in (table or ())]}")
    # What the page actually calls the things that are NOT day cells — this is
    # where a vocabulary that does not match a real site shows up.
    not_days = [
        str(n.get("name") or "") for n in nav
        if not browse.read_date(str(n.get("name") or ""))
    ]
    print(f"  non-day nav names: {not_days[:10]}")
    # Which cell the ladder would aim at, before anything is pressed.
    verdict = browse.pick_day(cells, browse.Day(15, 12, 2026), heading)
    if verdict.tag:
        chosen = next(c for c in cells if c.tag == verdict.tag)
        print(f"  would press: label={chosen.label!r} onscreen={chosen.onscreen} "
              f"disabled={chosen.disabled}")
    # END TO END, not just the cell ladder: a date months out is what exercises
    # the walk, and the walk is where the heading/cells question actually bites.
    #
    # From a FRESH page, because pressing the field above opened the picker and
    # RENAMED the control — lot.com's 'Wylot Od Wybierz datę' becomes 'Wylot
    # Wybierz datę wylot z zakresu od…'. Asking for the old name got "no control
    # on this page is called that", so this leg reported a probe artefact
    # instead of the walk, on the one site whose walk was broken. An instrument
    # that cannot run its own end-to-end leg is how lot.pl sat in SITES for a
    # week while its picker went unmeasured.
    try:
        past_the_consent_wall(browser.browse_open(url, timeout=90))
        out = browser.browse_fill(
            [{"target": field.address, "do": "date", "value": "2026-12-15"}],
            timeout=180,
        )
        print(f"  do=\"date\" 2026-12-15 → {' | '.join(out.ledger)[:180]}")
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"  do=\"date\" 2026-12-15 → {type(exc).__name__}: {exc}")
        traceback.print_exc()
    shot = SHOTS / f"{label.replace('.', '_')}.jpg"
    if shoot(shot):
        print(f"  screenshot: {shot}")


def main() -> int:
    assert "probe-cal-" in str(browser.profile_dir()), "refusing the real profile"
    print(f"profile: {browser.profile_dir()}")
    for label, url, hint in SITES:
        try:
            report(label, url, hint)
        except Exception as exc:  # noqa: BLE001 — one site must not end the probe
            print(f"  !! {type(exc).__name__}: {exc}")
    print(f"\nscreenshots in {SHOTS}")
    browser.browse_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
