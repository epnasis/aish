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
    ("lot.pl", "https://www.lot.com/pl/en/", "travel date"),
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
    print(f"  nav candidates: {len(nav)}; matched as month arrows: {len(arrows)}")
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
    try:
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
