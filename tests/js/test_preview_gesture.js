// Node-only, dependency-free checks for [PREVIEW-GESTURE] — the transform
// maths behind double-tap zoom, pan, pinch, and drag-to-dismiss.
//
// The interesting failures in a photo viewer are not "does it move" but "does
// it move to the RIGHT place", and those are invisible in a screenshot:
//
//   - a double-tap that does not keep the tapped detail under the finger sends
//     you hunting for the bit you wanted to see;
//   - a pan bounded by the ELEMENT rather than by the picture lets you drag the
//     photo off into the letterbox margin — broken in a way that is hard to
//     name and easy to ship;
//   - a drag that dismisses while zoomed drops the picture out of the window
//     the moment you look at its bottom edge.
//
// So the model is pure functions over {scale, x, y} and they are checked here,
// in numbers. Coordinates are relative to the IMG's box, origin at its centre
// (where a CSS transform scales from).
//
// Run manually: node tests/js/test_preview_gesture.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();

const s = {};
vm.createContext(s);
vm.runInContext(
  surface(extract(appSource(), "// [PREVIEW-GESTURE-START]", "// [PREVIEW-GESTURE-END]")),
  s,
);

const FIT = { scale: 1, x: 0, y: 0 };
// A phone-shaped window and a landscape photo: the letterboxing case, which is
// the one that catches element-vs-picture bounding.
const VIEW = { w: 400, h: 800 };
const WIDE = { w: 1000, h: 500 };
const TALL = { w: 500, h: 1000 };

const near = (a, b, slop = 0.51) => Math.abs(a - b) <= slop;

// Where does the material point currently under screen position `p` end up
// after the transform changes? Under {scale, x}, the screen point p maps to the
// unscaled, centre-relative coordinate q = (p - c - x) / scale; afterwards that
// same q sits at c + x' + scale' * q. Writing it out matters: the naive version
// (treating p - c as q) is only right from an unzoomed, unpanned start, and
// silently "fails" a correct implementation the moment it is not.
function lands(before, after, p, size) {
  const c = size / 2;
  const q = (p - c - before.offset) / before.scale;
  return c + after.offset + after.scale * q;
}

// ---- what is actually on screen -------------------------------------------
{
  const content = s.previewContent(VIEW, WIDE);
  ok("a wide photo letterboxes to the window's width",
    content.w === 400 && content.h === 200);
  const tall = s.previewContent(VIEW, TALL);
  ok("a tall one is bounded by the window's height instead",
    tall.h === 800 && tall.w === 400);
  ok("an image whose size is not known yet falls back to the window",
    s.previewContent(VIEW, { w: 0, h: 0 }).w === 400);
}

// ---- the point under your finger stays there ------------------------------
{
  // Zoom about a point 100px left of centre; that pixel must not move.
  const point = { x: 100, y: 400 };
  const after = s.previewZoomAt(FIT, 2, point, VIEW, WIDE);
  const landed = {
    x: lands({ scale: 1, offset: 0 }, { scale: after.scale, offset: after.x }, point.x, VIEW.w),
    y: lands({ scale: 1, offset: 0 }, { scale: after.scale, offset: after.y }, point.y, VIEW.h),
  };
  ok(`double-tapping a detail enlarges THAT detail (x ${Math.round(landed.x)} ≈ 100)`,
    near(landed.x, point.x));
  ok("…and does not drift vertically either", near(landed.y, point.y));
}

{
  // The same, starting from an already-zoomed, already-panned state — where the
  // naive arithmetic breaks and this has to be right.
  const start = s.previewZoomAt(FIT, 3, { x: 350, y: 300 }, VIEW, WIDE);
  const point = { x: 120, y: 500 };
  const after = s.previewZoomAt(start, 4.5, point, VIEW, WIDE);
  const landed = lands(
    { scale: start.scale, offset: start.x },
    { scale: after.scale, offset: after.x },
    point.x, VIEW.w,
  );
  ok(`zooming again keeps the NEW point fixed (x ${Math.round(landed)} ≈ 120)`,
    near(landed, point.x));
}

// ---- panning cannot wander off --------------------------------------------
{
  const zoomed = { scale: 2, x: 0, y: 0 };
  const far = s.previewClamp({ scale: 2, x: 9999, y: 9999 }, VIEW, WIDE);
  // content 400x200 at 2x = 800x400 in a 400x800 window: 200 of slack across,
  // none at all vertically (400 < 800).
  ok(`panning stops at the picture's edge (x ${far.x}, expected 200)`, far.x === 200);
  ok("…and there is nothing to pan vertically when it does not fill the height",
    far.y === 0);
  ok("the model does not invent movement at fit scale",
    s.previewClamp({ scale: 1, x: 500, y: 500 }, VIEW, WIDE).x === 0);
  assert.strictEqual(zoomed.x, 0); // clamp is pure — it did not mutate the input
}

{
  // The bug that bounding by the ELEMENT would hide: at 1.5x a letterboxed wide
  // photo is 600x300 in a 400x800 window. There IS slack across (100) and none
  // down — an element-bounded clamp would offer (800*1.5-800)/2 = 200 of
  // vertical pan into the black margin.
  const far = s.previewClamp({ scale: 1.5, x: 9999, y: 9999 }, VIEW, WIDE);
  ok("panning is bounded by the PICTURE, not the element it is drawn in",
    far.x === 100 && far.y === 0);
}

// ---- double-tap toggles ----------------------------------------------------
{
  const zoomed = s.previewToggleZoom(FIT, { x: 200, y: 400 }, VIEW, WIDE);
  ok("a double-tap at fit zooms in", zoomed.scale > 1);
  const back = s.previewToggleZoom(zoomed, { x: 380, y: 700 }, VIEW, WIDE);
  ok("…and a double-tap anywhere while zoomed goes back to fit — no pinching "
    + "out to escape", back.scale === 1);
  ok("…recentred, since at fit there is nowhere to be", back.x === 0 && back.y === 0);
}

// ---- drag: dismiss at fit, pan when zoomed --------------------------------
{
  const dragged = s.previewDrag(FIT, { x: 0, y: 60 }, VIEW, WIDE);
  ok("dragging down at fit carries the picture with the finger", dragged.y === 60);
  ok("…and reports how far along the dismissal is, for the fade",
    dragged.dismissing > 0 && dragged.dismissing < 1);
  ok("…but 60px is not a dismissal yet", !s.previewDragEnds(dragged));
  ok("…and 140px is", s.previewDragEnds(s.previewDrag(FIT, { x: 0, y: 140 }, VIEW, WIDE)));
}

{
  ok("dragging UP at fit does nothing — only down means 'put it away'",
    s.previewDrag(FIT, { x: 0, y: -120 }, VIEW, WIDE).y === 0);
  ok("…and sideways does nothing either, there being no next photo to page to",
    s.previewDrag(FIT, { x: -200, y: 0 }, VIEW, WIDE).x === 0);
}

{
  // The one that would drop a picture out of the window mid-examination.
  const zoomed = { scale: 3, x: 0, y: 0 };
  const panned = s.previewDrag(zoomed, { x: 0, y: 200 }, VIEW, WIDE);
  ok("a drag while ZOOMED pans instead of dismissing", !s.previewDragEnds(panned));
  ok("…and never reports a dismissal in progress", panned.dismissing === 0);
  // A wide photo at 3x is 1200x600 in a 400x800 window: still short of the
  // height, so there is nothing to pan vertically and the drag goes nowhere.
  ok("…and does not move where there is nothing to see", panned.y === 0);
}

{
  // The same drag on a picture that DOES fill the height: 500x1000 fits to
  // 400x800, and at 2x is 800x1600 — 400 of vertical slack.
  const panned = s.previewDrag({ scale: 2, x: 0, y: 0 }, { x: 0, y: 200 }, VIEW, TALL);
  ok("panning a tall picture moves it by the drag", panned.y === 200);
  const far = s.previewDrag({ scale: 2, x: 0, y: 0 }, { x: 0, y: 9999 }, VIEW, TALL);
  ok("…and stops at its edge, not beyond", far.y === 400);
}

// ---- pinch -----------------------------------------------------------------
// Two fingers do TWO things at once and both must be honoured: they scale by
// their spread AND carry the picture by the pair's travel.
{
  const mid = { x: 200, y: 400 };
  const out = s.previewPinch(FIT, 2, mid, mid, VIEW, TALL);
  ok("spreading two fingers scales by how far they spread", out.scale === 2);
  const back = s.previewPinch(out, 0.5, mid, mid, VIEW, TALL);
  ok("…and pinching in scales back down, measured from where the pinch STARTED "
    + "rather than compounding every frame", back.scale === 1);
}

{
  ok("a pinch cannot go below fit — the picture never shrinks into the middle",
    s.previewPinch(FIT, 0.2, { x: 200, y: 400 }, { x: 200, y: 400 }, VIEW, WIDE).scale === 1);
  ok("…nor past the ceiling",
    s.previewPinch(FIT, 99, { x: 200, y: 400 }, { x: 200, y: 400 }, VIEW, WIDE)
      .scale === s.PREVIEW_MAX_ZOOM);
}

{
  // THE REVERSED PAN. Anchoring on the current midpoint alone makes the
  // correction (to - centre)(1 - ratio), which is negative once you are zooming
  // in — so the picture slid the opposite way to the hands moving it. Utterly
  // obvious in the hand, and invisible to a symmetric pinch test where the
  // midpoint never moves, which is exactly why it shipped.
  const start = { scale: 3, x: 0, y: 0 };
  const from = { x: 200, y: 400 };
  const moved = s.previewPinch(start, 1, from, { x: 260, y: 400 }, VIEW, TALL);
  ok(`two fingers moving RIGHT carry the picture right (x ${moved.x})`, moved.x > 0);
  ok("…by exactly how far they travelled, when they are not also spreading",
    moved.x === 60);
}

{
  const start = { scale: 3, x: 0, y: 0 };
  const from = { x: 200, y: 400 };
  const down = s.previewPinch(start, 1, from, { x: 200, y: 520 }, VIEW, TALL);
  ok("…and moving DOWN carries it down, not up", down.y > 0);
}

{
  // Travelling and spreading at once: still the same direction of travel.
  const start = { scale: 2, x: 0, y: 0 };
  const from = { x: 200, y: 400 };
  const both = s.previewPinch(start, 1.5, from, { x: 300, y: 400 }, VIEW, TALL);
  ok("a pinch that also travels still moves WITH the fingers", both.x > 0);
}

{
  // And the anchor still holds: the content under the fingers when they landed
  // is under them when they stop.
  const start = { scale: 2, x: 30, y: 0 };
  const from = { x: 150, y: 400 };
  const to = { x: 260, y: 400 };
  const after = s.previewPinch(start, 1.8, from, to, VIEW, TALL);
  const q = (from.x - VIEW.w / 2 - start.x) / start.scale;
  const landed = VIEW.w / 2 + after.x + after.scale * q;
  ok(`what was under the fingers is still under them (x ${Math.round(landed)} ≈ ${to.x})`,
    near(landed, to.x, 1));
}

// ---- paging between the pictures in one message ---------------------------
{
  const mid = { count: 3, index: 1 };
  ok("a sideways drag carries the picture with the finger",
    s.previewSwipe({ x: -90, y: 4 }, mid).x === -90);
  ok("…and past the threshold it steps on", s.previewSwipeStep(-90, mid) === 1);
  ok("…the other way, back", s.previewSwipeStep(90, mid) === -1);
  ok("a short drag is not a swipe", s.previewSwipeStep(-40, mid) === 0);
}

{
  // No wrap: the ends resist instead. Wrapping saves a swipe and costs you the
  // knowledge of where you are in a set of three.
  const first = { count: 3, index: 0 };
  const last = { count: 3, index: 2 };
  ok("the first picture resists being pulled backwards",
    Math.abs(s.previewSwipe({ x: 100, y: 0 }, first).x) < 100);
  ok("…and does not step", s.previewSwipeStep(100, first) === 0);
  ok("the last resists being pulled onwards",
    Math.abs(s.previewSwipe({ x: -100, y: 0 }, last).x) < 100);
  ok("…and does not step", s.previewSwipeStep(-100, last) === 0);
  ok("but a middle one gives the full travel, so the ends FEEL like ends",
    s.previewSwipe({ x: 100, y: 0 }, { count: 3, index: 1 }).x === 100);
}

{
  // The axis is committed once. A drag that re-decides mid-gesture wobbles
  // between paging and dismissing and does neither cleanly.
  ok("a mostly-sideways drag is a page", s.previewAxis({ x: 40, y: 9 }) === "x");
  ok("a mostly-downward one is a dismissal", s.previewAxis({ x: 9, y: 40 }) === "y");
  ok("and a touch that has barely moved commits to neither yet",
    s.previewAxis({ x: 3, y: 3 }) === null);
}

// ---- the trap the maths above CANNOT catch --------------------------------
// Every function here is handed a `view`. The bug that let the picture escape
// was in the code that MEASURES it: getBoundingClientRect() on the image
// reports the box AFTER the transform, so at 6x the bounds were six times too
// generous and a pinch could fling the photo off screen, leaving a screen of
// black. No unit test over the model can see that — the model was never wrong —
// and it reads as the obvious way to get an element's size.
//
// So this is a source check: the measurement must come from LAYOUT geometry.
{
  const src = appSource();
  const from = src.indexOf("function previewBox()");
  const body = src.slice(from, src.indexOf("\n}", from));
  ok("previewBox measures the image with offsetWidth/offsetHeight",
    /offsetWidth/.test(body) && /offsetHeight/.test(body));
  // Whatever the image is bound to, its rect must never be asked for.
  const imageVar = /(?:const|let|var)\s+(\w+)\s*=\s*\$\("preview-img"\)/.exec(body);
  ok("previewBox binds the image to a variable", !!imageVar);
  ok("…and never asks THAT for its rect, which is post-transform",
    !new RegExp(`\\b${imageVar[1]}\\.getBoundingClientRect`).test(body));
  ok("…taking its origin from the untransformed overlay instead",
    /\$\("preview"\)/.test(body) && /getBoundingClientRect/.test(body));
}

report("test_preview_gesture.js");
