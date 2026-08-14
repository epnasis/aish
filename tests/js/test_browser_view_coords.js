// Where a tap actually lands ([BROWSER-VIEW-COORDS], #221).
//
// The remote view shows the Mac's browser as a letterboxed <img> and turns a
// tap into a click at the same point on the real page. Everything about that
// is invisible when it is slightly wrong: the frame still looks right, the
// click just lands somewhere else — and the place it matters most is a small
// login field on a phone, which is the entire reason the view exists.
//
// The failure mode this pins is mapping against the ELEMENT box instead of the
// RENDERED image. `object-fit: contain` letterboxes, so on any aspect ratio
// that does not match, the element is bigger than the picture in one axis and
// every coordinate is off by the margin — increasingly so toward the edges.
//
// Run manually: node tests/js/test_browser_view_coords.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();

/** The REAL function, run against a frame of `frame` shown in an element of
 *  `box`. Nothing is copied — the source is sliced out of the shipped app.js. */
function mapper(frame, box) {
  const sandbox = { bvFrame: frame };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(
      extract(
        appSource(),
        "// [BROWSER-VIEW-COORDS-START]",
        "// [BROWSER-VIEW-COORDS-END]"
      )
    ),
    sandbox
  );
  const img = { getBoundingClientRect: () => box };
  return (clientX, clientY) => sandbox.browserViewPoint(img, clientX, clientY);
}

// A 1024x1400 frame in a 512x700 element: exact half scale, no letterbox.
{
  const at = mapper({ width: 1024, height: 1400 }, { left: 0, top: 0, width: 512, height: 700 });
  const centre = at(256, 350);
  ok("exact-fit centre maps to the frame centre",
    centre.x === 512 && centre.y === 700);
  const corner = at(0, 0);
  ok("exact-fit origin maps to the origin", corner.x === 0 && corner.y === 0);
}

// The letterbox case. A 1024x1400 frame (tall) in a 512x900 element (taller
// still): `contain` fits by WIDTH, so scale is 0.5, the picture is 700px tall,
// and there are 100px bars top and bottom.
{
  const at = mapper({ width: 1024, height: 1400 }, { left: 0, top: 0, width: 512, height: 900 });

  const top = at(256, 100);
  ok("a tap on the picture's top edge maps to y=0", top && top.y === 0);

  const middle = at(256, 450);
  ok("a tap at the element's middle maps to the frame's middle",
    middle && middle.x === 512 && middle.y === 700);

  const bottom = at(256, 800);
  ok("a tap on the picture's bottom edge maps to the last row",
    bottom && bottom.y === 1400);

  // The regression itself: mapping against the element box would put this at
  // y = 50/900*1400 ≈ 78 — inside the page. It is in the letterbox bar.
  ok("a tap in the top bar is rejected, not mapped into the page",
    at(256, 50) === null);
  ok("a tap in the bottom bar is rejected", at(256, 870) === null);
}

// Horizontal letterbox: a wide frame in a narrow-but-tall element.
{
  const at = mapper({ width: 1000, height: 500 }, { left: 0, top: 0, width: 1000, height: 800 });
  ok("wide frame: vertical bars are rejected", at(500, 100) === null);
  const inside = at(500, 400);
  ok("wide frame: centre still maps to the frame centre",
    inside && inside.x === 500 && inside.y === 250);
}

// The element is not always at the viewport origin — the sheet has a header
// above it and padding beside it, so a non-zero left/top must be subtracted.
{
  const at = mapper({ width: 1024, height: 1400 }, { left: 40, top: 120, width: 512, height: 700 });
  const centre = at(40 + 256, 120 + 350);
  ok("an offset element still maps its centre to the frame centre",
    centre.x === 512 && centre.y === 700);
  ok("a tap above an offset element is rejected", at(40 + 256, 100) === null);
}

report("browser view coordinates");

// ---- zoom must not break aiming ([BROWSER-VIEW-ZOOM-START]) --------------
//
// Zoom exists so a small password field can be hit accurately, so the one
// thing it must never do is move where a tap lands. The transform is applied
// to the <img>, and `getBoundingClientRect()` reports the TRANSFORMED box —
// which is exactly why the mapper reads the box rather than tracking zoom
// state itself. These pin that the two stay consistent.
{
  // 1024x1400 frame, stage 512x700 at 1x (exact fit, no letterbox).
  const at1 = mapper({ width: 1024, height: 1400 }, { left: 0, top: 0, width: 512, height: 700 });
  const before = at1(128, 175);   // a quarter in from the top-left

  // Zoomed 2x about the centre: the box doubles and shifts by half its growth,
  // which is what `translate(...) scale(2)` about centre origin produces.
  const at2 = mapper(
    { width: 1024, height: 1400 },
    { left: -256, top: -350, width: 1024, height: 1400 }
  );
  // The same PAGE point is now at twice the distance from the (moved) origin.
  const after = at2(-256 + 256, -350 + 350);
  ok("a page point keeps its coordinates when zoomed 2x",
    before.x === after.x && before.y === after.y);

  ok("zoomed, the frame centre still maps to the frame centre",
    (() => { const c = at2(256, 350); return c.x === 512 && c.y === 700; })());
}

// Panned as well as zoomed: the box moves, and nothing about the mapping
// changes — the whole point of reading geometry back instead of recomputing it.
{
  const at = mapper(
    { width: 1000, height: 1000 },
    { left: 120, top: -80, width: 2000, height: 2000 }
  );
  const p = at(120 + 1000, -80 + 1000);
  ok("panned and zoomed, the box centre is still the frame centre",
    p.x === 500 && p.y === 500);
  ok("panned, a point outside the picture is still rejected", at(50, 50) === null);
}

report("browser view coordinates under zoom");

// ---- a swipe must move the page by what it looks like it moves ----------
//
// The frame is `contain`-fitted, so a finger travelling 100px across a
// shrunken frame is more than 100px of page. Getting this wrong is not
// visible as a bug — the page just scrolls the wrong amount, every time.
{
  const sandbox = { bvFrame: { width: 1000, height: 2000 } };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(appSource(), "// [BROWSER-VIEW-ZOOM-START]", "// [BROWSER-VIEW-ZOOM-END]")),
    sandbox
  );
  // Half-size frame: 1px of finger is 2px of page.
  sandbox.$ = () => ({ getBoundingClientRect: () => ({ width: 500, height: 1000 }) });
  ok("a shrunken frame scales the swipe up", sandbox.bvPageScale() === 2);

  // Shown at natural size: 1:1.
  sandbox.$ = () => ({ getBoundingClientRect: () => ({ width: 1000, height: 2000 }) });
  ok("a frame at natural size scrolls 1:1", sandbox.bvPageScale() === 1);

  // A collapsed stage must not produce Infinity/NaN scroll deltas.
  sandbox.$ = () => ({ getBoundingClientRect: () => ({ width: 0, height: 0 }) });
  ok("a zero-sized stage falls back to 1:1 rather than NaN",
    sandbox.bvPageScale() === 1);
}

report("browser view swipe scaling");
