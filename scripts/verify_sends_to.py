"""Drive the REAL control enumeration against real Chrome, and read back where
each submit says it would send (#346).

The unit tests hand `controls_from` a dict with `sends_to` already in it, which
pins the GATE and pins nothing about the enumeration that fills it. This is the
other half: a page whose forms are written the six ways a real page writes them,
enumerated by the shipped `CONTROLS_JS`, with the destination read off the
result. Everything here is a `data:`/served scratch page in a throwaway profile
— it never touches the owner's profile, and it opens no aish session.

    uv run --with playwright python scripts/verify_sends_to.py
"""

from __future__ import annotations

import asyncio
import sys

from aish import browse

#: The page is SERVED at a real origin rather than `set_content`-ed, because the
#: first run of this script showed why it matters: `form.action` with no action
#: attribute reports the DOCUMENT's URL, and on an `about:blank` document that
#: is `about:blank` — no host, which the gate reads as unvouched. The question
#: this script answers is what a real navigation produces.
ORIGIN = "https://shop.test/cart/"

PAGE = """<!doctype html>
<html><head><title>forms</title></head><body>
  <form id="a"><input name="q"><button id="none">no action</button></form>
  <form id="b" action="/checkout"><input name="q">
    <button id="relative">relative</button></form>
  <form id="c" action="https://collector.example/collect"><input name="q">
    <button id="cross">cross-origin</button></form>
  <form id="d" action="https://shop.test/results"><input name="q">
    <button id="same">same-origin</button></form>
  <form id="e"><input name="q">
    <button id="formaction" formaction="https://collector.example/x">
      button formaction</button></form>
  <form id="f" action="mailto:someone@evil.example"><input name="q">
    <button id="mailto">mailto</button></form>
  <input id="loose" name="loose">
</body></html>
"""

#: What each named control must report. `""` is *no readable destination*, which
#: the gate reads as unvouched.
EXPECTED = {
    "no action": "https://shop.test/cart/",
    "relative": "https://shop.test/checkout",
    "cross-origin": "https://collector.example/collect",
    "same-origin": "https://shop.test/results",
    "button formaction": "https://collector.example/x",
    "mailto": "mailto:someone@evil.example",
}


async def main() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        await page.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=PAGE
            ),
        )
        await page.goto(ORIGIN)
        raw = await page.evaluate(
            browse.CONTROLS_JS,
            {
                "max": browse.MAX_CONTROLS,
                "nameMax": browse.NAME_MAX_CHARS,
                "inlineChoices": browse.CHOICE_INLINE_MAX,
                "offset": 0,
                "match": "",
            },
        )
        await browser.close()

    found = {c.name: c.sends_to for c in browse.controls_from(raw["controls"])}
    bad = []
    for name, want in EXPECTED.items():
        got = found.get(name, "<missing>")
        print(f"{'ok ' if got == want else 'BAD'} {name!r}: {got!r}")
        if got != want:
            bad.append((name, want, got))

    # The loose field is outside every form: nothing to read, so nothing is
    # claimed. `_driven_host` turns that into UNREADABLE_DESTINATION.
    loose = found.get("loose", "<missing>")
    print(f"{'ok ' if loose == '' else 'BAD'} loose field (no form): {loose!r}")
    if loose != "":
        bad.append(("loose", "", loose))

    print("FAILED" if bad else "all destinations read as written")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
