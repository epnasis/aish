#!/usr/bin/env python3
"""Measure the browser sheet's layout at real phone viewports, with real insets.

The sheet is a fixed column where every row is load-bearing, and three times now
a control has ended up off the bottom of the screen while every unit test
passed: the Close button at y=899, then the text field under the keyboard that
types into it, then the password input at y=336 in a 234px landscape viewport
(#226). None of them are visible to `tests/js/`, which runs the shipped
functions against a fake DOM and has no layout engine at all.

So this drives the REAL index.html and the REAL style.css through a real Chrome
at real device metrics, and asserts what the owner can actually reach. Two
things make it faithful rather than a simulation:

  * `Emulation.setSafeAreaInsetsOverride` sets a genuine `env(safe-area-inset-*)`
    — the Dynamic Island's 59px — which cannot otherwise be produced on a desktop
    and which the #226 fix is written entirely against.
  * the keyboard is modelled the way app.js models it, by pinning
    `document.body.style.height` to the visual viewport, because iOS keeps the
    LAYOUT viewport full-height under the keyboard and every `vh` here would lie.

It touches nothing of aish's: a throwaway Chrome, a static file server, no
WebSocket, no state dir, no browser profile. Not part of `uv run pytest` — the
suite launches no Chrome by design (see tests/conftest.py). Run it by hand after
changing anything in the `.bv-*` column:

    uv run python scripts/check-browser-sheet.py            # pass/fail per case
    uv run python scripts/check-browser-sheet.py --shots /tmp/bv   # + screenshots

It also checks the sharpened detail patch (#227), which has the same problem
for the same reason: it is laid over the frame in PAGE coordinates and the
layer then carries the frame's own transform, and no fake DOM has `object-fit`,
a transform or rounding to get that wrong with. A misplaced patch is invisible
— it looks like a sharp picture, in the wrong place — so it is checked through
the SHIPPED tap mapper at several zoom levels.

Exits non-zero if any control is unreachable or the patch does not land.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parent.parent / "aish" / "static"

# (label, screen w, screen h, top inset, bottom inset, visual viewport, editor
#  open, pan) — `pan` is `visualViewport.offsetTop`: iOS sometimes scrolls the
#  page up under the keyboard instead of only shrinking it, and app.js follows
#  by setting `body.style.top`, so the visible band starts there.
#
# The visual viewport is what the keyboard leaves. iPhone portrait keyboards run
# ~336px; landscape ones ~190-200, which is what makes landscape the hard case.
CASES = [
    ("Pro Max portrait", 430, 932, 59, 34, 932, False, 0),
    ("Pro Max portrait, keyboard", 430, 932, 59, 34, 596, True, 0),
    ("Pro Max portrait, keyboard + pan", 430, 932, 59, 34, 596, True, 40),
    ("Pro portrait, keyboard", 393, 852, 59, 34, 516, True, 0),
    ("SE portrait, keyboard", 375, 667, 0, 0, 407, True, 0),
    ("Pro Max landscape", 932, 430, 0, 21, 430, False, 0),
    ("Pro Max landscape, keyboard", 932, 430, 0, 21, 234, True, 0),
    ("Pro landscape, keyboard", 852, 393, 0, 21, 205, True, 0),
    ("16 Pro Max landscape, keyboard", 956, 440, 0, 21, 240, True, 0),
    ("SE landscape, keyboard", 667, 375, 0, 0, 213, True, 0),
]

# A tap on the page is the only gesture that LEAVES a field without submitting,
# so the stage may shrink to a tap target and no further.
MIN_TAPPABLE = 44

_MEASURE = """() => {
  const vis = (el) => el && getComputedStyle(el).display !== 'none' && !el.hidden;
  const r = (el) => { if (!vis(el)) return null;
    const b = el.getBoundingClientRect();
    return {t: Math.round(b.top), b: Math.round(b.bottom), h: Math.round(b.height)}; };
  const s = document.getElementById('browser-sheet');
  return {
    url:    r(document.getElementById('bv-url')),
    close:  r(s.querySelector('.bv-x')),
    stage:  r(s.querySelector('.bv-stage')),
    input:  r(document.getElementById('bv-edit-input')),
    nav:    r(s.querySelector('.bv-nav')),
  };
}"""

_OPEN_SHEET = """([vvh, editing]) => {
  // No WebSocket here, so the boot loader never lifts on its own. It is an
  // opaque overlay: it does not move the sheet's layout, but it hides it from
  // --shots, which is the half a human looks at.
  document.getElementById('boot-loader')?.remove();
  openSheet('browser-sheet');
  document.getElementById('bv-empty').hidden = true;
  // A frame of the shape the server actually returns (desktop width, stage
  // aspect), so the flex column is under the load it carries in use.
  document.getElementById('bv-frame').src = 'data:image/svg+xml;base64,' + btoa(
    '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="1959">' +
    '<rect width="100%" height="100%" fill="#33475a"/></svg>');
  if (editing) {
    document.getElementById('bv-edit').hidden = false;
    document.getElementById('bv-edit-label').textContent = 'Password';
  }
  // What app.js does when the keyboard opens: pin the body to the visual
  // viewport. `vh` would not move, which is the whole trap.
  document.body.style.height = vvh + 'px';
}"""


def _serve() -> tuple[socketserver.TCPServer, int]:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(STATIC)
    )
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a, **k: None
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _problems(g: dict, top: int, bottom: int, vvh: int, pan: int) -> list[str]:
    """What the owner cannot reach. Rects are in LAYOUT coordinates, so the band
    the owner can see runs from `pan` to `pan + vvh`; below `vvh - bottom` is
    under the home indicator, which swallows the touch even though the pixels
    are visible."""
    out = []
    floor, ceil = pan + vvh, pan + top
    for name in ("url", "close"):
        if g[name] and g[name]["t"] < ceil:
            out.append(f"{name} under the status bar (y={g[name]['t']} < {ceil})")
    for name in ("input", "nav", "stage"):
        if g[name] and g[name]["b"] > floor:
            out.append(f"{name} below the keyboard ({g[name]['b']} > {floor})")
        elif g[name] and g[name]["b"] > floor - bottom:
            out.append(f"{name} under the home indicator ({g[name]['b']} > {floor - bottom})")
    if g["stage"] and g["stage"]["h"] < MIN_TAPPABLE:
        out.append(f"stage too small to tap out of a field ({g['stage']['h']}px)")
    return out


# The detail patch is laid over the frame in PAGE coordinates and the layer
# then carries the frame's own transform. That trick is only sound while both
# boxes are identical, and nothing in tests/js/ can see it — a fake DOM has no
# `object-fit`, no transform and no rounding. So it is checked here, through the
# SHIPPED tap mapper: map the patch's rendered corner back to a page point and
# it must be the page point the patch claims to cover. A patch that is off is
# invisible — it looks like a sharp picture, in the wrong place.
_DETAIL_ALIGNMENT = """([zoom, rect]) => {
  const img = document.getElementById('bv-frame');
  bvFrame = { width: 1280, height: 1950 };
  bvZoom = { scale: zoom, x: 0, y: 0 };
  bvPaint(false);
  bvDetail = rect;
  const patch = document.getElementById('bv-detail');
  patch.src = 'data:image/svg+xml;base64,' + btoa(
    '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>');
  bvPaintDetail();
  const box = patch.getBoundingClientRect();
  // Half a pixel in, so a boundary does not round out of the frame.
  const topLeft = browserViewPoint(img, box.left + 0.5, box.top + 0.5);
  const bottomRight = browserViewPoint(img, box.right - 0.5, box.bottom - 0.5);
  return { topLeft, bottomRight, box: {w: box.width, h: box.height} };
}"""


def _check_detail_alignment(port: int, shots: Path | None) -> int:
    """Does the sharpened patch cover the page rectangle it says it covers?"""
    from playwright.sync_api import sync_playwright

    rect = {"x": 320, "y": 480, "w": 640, "h": 975, "scale": 2.5}
    bad = 0
    print()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport={"width": 430, "height": 932},
                                  device_scale_factor=3, is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        page.evaluate(_OPEN_SHEET, [932, False])
        for zoom in (1, 1.8, 2.5, 4):
            g = page.evaluate(_DETAIL_ALIGNMENT, [zoom, rect])
            tl, br = g["topLeft"], g["bottomRight"]
            off = []
            if not tl or abs(tl["x"] - rect["x"]) > 2 or abs(tl["y"] - rect["y"]) > 2:
                off.append(f"top-left maps to {tl}, not ({rect['x']}, {rect['y']})")
            want_br = (rect["x"] + rect["w"], rect["y"] + rect["h"])
            if not br or abs(br["x"] - want_br[0]) > 2 or abs(br["y"] - want_br[1]) > 2:
                off.append(f"bottom-right maps to {br}, not {want_br}")
            bad += bool(off)
            print(f"{'FAIL' if off else 'ok  '}  detail patch at {zoom}x zoom "
                  f"covers the page rect it claims")
            for line in off:
                print(f"        *** {line}")
        if shots:
            page.screenshot(path=str(shots / "detail-alignment.png"))
        ctx.close()
        browser.close()
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", metavar="DIR", help="also write a screenshot per case")
    args = ap.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    srv, port = _serve()
    failures = 0
    try:
        with sync_playwright() as p:
            # channel=chrome: the bundled build may not be installed, and this
            # needs a Chrome new enough for setSafeAreaInsetsOverride anyway.
            browser = p.chromium.launch(channel="chrome")
            for label, w, h, top, bot, vvh, editing, pan in CASES:
                ctx = browser.new_context(
                    viewport={"width": w, "height": h},
                    device_scale_factor=3, is_mobile=True, has_touch=True,
                )
                page = ctx.new_page()
                insets = {"top": top, "bottom": bot}
                if w > h:  # sideways: the island becomes a side inset
                    insets |= {"left": 59, "right": 59}
                ctx.new_cdp_session(page).send(
                    "Emulation.setSafeAreaInsetsOverride", {"insets": insets}
                )
                page.goto(f"http://127.0.0.1:{port}/index.html",
                          wait_until="domcontentloaded")
                page.wait_for_timeout(500)
                page.evaluate(_OPEN_SHEET, [vvh, editing])
                if pan:
                    page.evaluate("y => document.body.style.top = y + 'px'", pan)
                page.wait_for_timeout(250)
                g = page.evaluate(_MEASURE)
                probs = _problems(g, top, bot, vvh, pan)
                failures += bool(probs)
                rows = "  ".join(
                    f"{k}={g[k]['t']}..{g[k]['b']}" if g[k] else f"{k}=hidden"
                    for k in ("url", "stage", "input", "nav")
                )
                print(f"{'FAIL' if probs else 'ok  '}  {label:34} vp={vvh:4}  {rows}")
                for pr in probs:
                    print(f"        *** {pr}")
                if shots:
                    stem = label.replace(", ", "_").replace(" ", "-")
                    page.screenshot(path=str(shots / f"{stem}.png"))
                ctx.close()
            browser.close()
        print(f"\n{len(CASES) - failures}/{len(CASES)} cases reachable")
        # Inside the try: the file server is what serves index.html, and it is
        # shut down in the finally.
        failures += _check_detail_alignment(port, shots)
    finally:
        srv.shutdown()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
