#!/usr/bin/env python3
"""Run the REAL enumeration JS through a real Chrome, against local fixtures.

Three things about a driven page can only be answered by a browser, and all
three were argued from unit tests with hand-built fakes before this existed
(#348). A fake `Control` is whatever the test author believed the page would
produce, which is exactly the thing in doubt.

  * **Does the page declare a dialog, and is the document scroll-locked?**
    `CONTROLS_JS`'s `openDialog` licenses a sentence aish speaks in its OWN
    voice above the untrusted banner — *close it to reach them* — so a false
    positive here is worse than the wrong sentence it replaced. The inert
    fixture is the one that matters: `[role=dialog]` sitting in the DOM of a
    page that still scrolls must claim NOTHING, and half the web has one.

  * **What KIND is a suggestion?** `_commit_suggestion` could not commit a
    suggestion drawn as a plain `<button>` for any input, because the candidate
    set was `role=option` only. `option` and `submits` are computed in the JS
    from the live DOM; asserting on them from a fake is asserting on the
    assumption.

  * **Do declared options SHADOW an undeclared one?** The first version of the
    widening read `[options] or [everything else]`, which on a panel that
    renders static options beside the real suggestion — lot.com's exact shape —
    offered the static ones and missed the answer entirely.

Outside the pytest suite on purpose, like `check-browser-sheet.py`: conftest's
`no_real_browser` guard makes launching Chrome inside the suite fail loudly, and
that guard is worth more than this check is. Run it by hand after touching
`CONTROLS_JS`, `_commit_suggestion` or the dialog probe:

    uv run scripts/check-browse-fixtures.py
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from aish import browse as browse_mod  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures-348"
OPTS = {"max": 100, "nameMax": 80, "inlineChoices": 8, "offset": 0, "match": ""}
failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        failures.append(f"{name}: got {got!r}, want {want!r}")


async def enumerate_page(page, fixture: str) -> dict:
    await page.goto((FIXTURES / f"{fixture}.html").as_uri())
    await page.wait_for_timeout(300)
    return await page.evaluate(browse_mod.CONTROLS_JS, OPTS)


async def appeared_after_typing(page, fixture: str, text: str) -> list:
    """The controls the page drew IN RESPONSE — the real candidate set."""
    await page.goto((FIXTURES / f"{fixture}.html").as_uri())
    before = browse_mod.controls_from(
        (await page.evaluate(browse_mod.CONTROLS_JS, OPTS))["controls"]
    )
    browse_mod.address_controls(before)
    await page.fill("#q", text)
    await page.wait_for_timeout(600)
    after = browse_mod.controls_from(
        (await page.evaluate(browse_mod.CONTROLS_JS, OPTS))["controls"]
    )
    browse_mod.address_controls(after)
    was = {c.address for c in before}
    return [c for c in after if c.address not in was]


def two_stage(appeared: list, want: str) -> str:
    """The shipped rule from `_commit_suggestion`, mirrored."""
    declared_now = [c for c in appeared if c.option]
    undeclared = [c for c in appeared if not c.option and not c.submits]
    offered = declared_now or undeclared
    if not offered:
        return ""
    declared = all(c.option for c in offered)
    picked = browse_mod.match_option(
        [(c.name, c.address) for c in offered], want, strict=not declared
    )
    if picked.problem and declared and undeclared:
        second = browse_mod.match_option(
            [(c.name, c.address) for c in undeclared], want, strict=True
        )
        if not second.problem:
            picked = second
    return picked.label or ""


def _sentence(found: dict) -> str:
    """The line the MODEL would read, built from a real enumeration."""
    from aish import web as web_mod

    controls = browse_mod.controls_from(found["controls"])
    browse_mod.address_controls(controls)
    snap = browse_mod.Snapshot(
        url="file:///x", title="t", text="x", controls=controls,
        unreachable=found["unreachable"], dialog=found["dialog"],
        reasons=found.get("reasons") or {},
    )
    return next(
        (line for line in web_mod._present_snapshot(snap).splitlines()
         if "more control(s)" in line),
        "",
    )


async def _settle_here(page):
    """The REAL `_settle`, so what is measured is what ships."""
    from aish import browser as browser_mod

    return await browser_mod._settle(page, still_for=5_000, timeout_ms=15_000)


async def main() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()

        print("a declared modal over a scroll-locked page IS reported:")
        found = await enumerate_page(page, "dialog-covering")
        check("dialog", found["dialog"], "Wybierz daty")
        check("unreachable", found["unreachable"] > 0, True)

        print("the reason is the one REACH_JS recorded, not a guess (#350):")
        found = await enumerate_page(page, "dialog-covering")
        check("reasons", found.get("reasons"), {"behind-a-dialog": 4})

        print("…and it fires on an overlay that DECLARES NOTHING (lot.com's shape):")
        found = await enumerate_page(page, "undeclared-overlay")
        # #348's `dialog:modal` condition needed the site's cooperation and so
        # never fired on lot.com. `behind-a-dialog` is per-control and needs none.
        check("dialog declared", found["dialog"], "")
        check("reasons", found.get("reasons"), {"behind-a-dialog": 4})

        print("a reason's NAME can assert more than its test checked (#350):")
        found = await enumerate_page(page, "scroll-locked-shell")
        # No overlay of any kind — and `behind-a-dialog` fires anyway.
        check("reason on a page with nothing open",
              found.get("reasons"), {"behind-a-dialog": 2})
        check("so it must NOT give an order", "CLOSE it to reach them" not in
              _sentence(found), True)
        check("it describes instead", "scrolling LOCKED" in _sentence(found), True)

        print("`inert` is a closed drawer at least as often as a modal:")
        found = await enumerate_page(page, "inert-drawer")
        check("reasons", found.get("reasons"), {"inert": 3})
        check("gets the OPEN-something repair",
              "Press whatever opens them first" in _sentence(found), True)

        print("an inert [role=dialog] on a scrolling page claims NOTHING:")
        found = await enumerate_page(page, "dialog-inert")
        check("dialog", found["dialog"], "")

        print("a suggestion drawn as a plain <button>:")
        appeared = await appeared_after_typing(page, "autocomplete", "NRT")
        kinds = [(c.name, c.option, c.submits) for c in appeared]
        check("appeared", kinds, [("Tokio (NRT) Japonia", False, False)])
        check(
            "the role=option rule finds it",
            [c.name for c in appeared if c.option],
            [],
        )
        check("two-stage commits it", two_stage(appeared, "Tokio (NRT) Japonia"),
              "Tokio (NRT) Japonia")

        print("static declared options must not SHADOW the real suggestion:")
        appeared = await appeared_after_typing(page, "shadow", "NRT")
        check(
            "the page declared two options",
            sorted(c.name for c in appeared if c.option),
            ["Dowolny kierunek", "Siatka połączeń"],
        )
        check("two-stage still commits the button",
              two_stage(appeared, "Tokio (NRT) Japonia"), "Tokio (NRT) Japonia")

        print("counting the page's noise must not make the wait longer (#351):")
        await page.goto((FIXTURES / "noisy.html").as_uri())
        seen = (await _settle_here(page)).record()
        # The fixture lands ONE piece of content at ~2s and then churns an
        # attribute every 200ms forever. Attributes were not observed at all
        # before #351, so they never reset the quiet clock — and the first cut
        # of this change let them, which took this page from 7.2s to the full
        # 15.8s ceiling. A measurement that makes the thing it measures slower
        # is the whole failure mode; this is the check that catches it.
        check("the churn is recorded", seen["kinds"].get("attributes", 0) > 10, True)
        check("content is recorded", seen["kinds"].get("childList"), 1)
        check("but the churn does NOT extend the wait", seen["waited_ms"] < 10_000, True)
        check("released on quiet, not the ceiling", seen["released"], "quiet")
        # …and the wait is the content plus the bar, not the churn.
        check("settled ~5s after the content",
              4_000 < seen["waited_ms"] - seen["last_meaningful_ms"] < 6_500, True)

        await browser.close()

    if failures:
        print(f"\n{len(failures)} FAILED")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
