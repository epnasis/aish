// Node-only, dependency-free checks for [PREVIEW] — tap a picture, see it.
//
// It opens from three places: a composer attachment chip (which was inert), an
// attachment on a sent message, and an inline image the model produced. The
// last two opened a NEW TAB, which from an installed PWA means leaving the app
// for Safari to look at your own photo.
//
// Two details are load-bearing and easy to "tidy" away:
//
//   - the IMAGE is not a dismiss target. Long-press on it is how iOS offers
//     Save Image, and a tap handler there makes that fiddly. The surround, the
//     ✕ and Escape close it.
//   - closing clears the src. Otherwise a stale picture flashes when the next
//     one opens, and the bytes are held for as long as the page lives.
//
// Run manually: node tests/js/test_preview.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface, fakeElement, checks } = require("./harness");

const { ok, report } = checks();

function world() {
  const els = {
    preview: fakeElement("div"),
    "preview-img": fakeElement("img"),
    "preview-name": fakeElement("span"),
  };
  els.preview.hidden = true;
  els["preview-img"].removeAttribute = function (attr) { delete this[attr]; };
  // The gesture layer resets the transform on open/close; its own maths is
  // test_preview_gesture.js's business, so it is recorded here, not run.
  const resets = [];
  const snapshots = [];
  const sandbox = {
    $: (id) => els[id] || null,
    previewReset: () => resets.push(1),
    // The /debug snapshot is taken on the way out; its own content is
    // reportViewport's business.
    previewSnapshot: () => snapshots.push(1),
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(appSource(), "// [PREVIEW-START]", "// [PREVIEW-END]")),
    sandbox,
  );
  return { s: sandbox, els, resets, snapshots };
}

// ---- opening ---------------------------------------------------------------
{
  const w = world();
  w.s.openPreview("/file?path=/u/uploads/IMG_4021.jpg&token=t", "IMG_4021.jpg");
  ok("opening shows the overlay", w.els.preview.hidden === false);
  ok("…with the picture in it",
    w.els["preview-img"].src === "/file?path=/u/uploads/IMG_4021.jpg&token=t");
  ok("…and the FULL name, which is where a shortened chip name is finally readable",
    w.els["preview-name"].textContent === "IMG_4021.jpg");
  ok("…and an alt for anyone not looking at it",
    w.els["preview-img"].alt === "IMG_4021.jpg");
  ok("previewIsOpen agrees", w.s.previewIsOpen() === true);
  ok("…and the zoom is reset, or a new picture opens halfway into a corner",
    w.resets.length === 1);
}

// ---- closing ---------------------------------------------------------------
{
  const w = world();
  w.s.openPreview("/file?a", "a.png");
  ok("closing reports that it acted, so Escape can stop there",
    w.s.closePreview() === true);
  ok("…and hides the overlay", w.els.preview.hidden === true);
  ok("…and drops the src, or the next open flashes the previous picture",
    w.els["preview-img"].src === undefined);
  // /debug is reached by typing in the composer, which the preview covers and
  // closing resets — so the state has to be kept on the way out or the hook is
  // useless for the one case it exists for.
  ok("…having first kept the state /debug would report", w.snapshots.length === 1);
  ok("previewIsOpen agrees", w.s.previewIsOpen() === false);
}

{
  // The Escape chain reads the return value: a closed preview must NOT swallow
  // the key, or Escape stops dismissing the sheet or modal behind it.
  const w = world();
  ok("closing an already-closed preview reports that it did nothing",
    w.s.closePreview() === false);
}

// ---- opening a second picture ---------------------------------------------
{
  const w = world();
  w.s.openPreview("/file?a", "a.png");
  w.s.openPreview("/file?b", "b.png");
  ok("opening another replaces both the picture and the name",
    w.els["preview-img"].src === "/file?b" && w.els["preview-name"].textContent === "b.png");
}

// ---- a missing overlay is not a crash --------------------------------------
{
  // The offline shell and the tests both build partial DOMs; a preview that
  // throws would take the caller (a chip's onclick) with it.
  const sandbox = { $: () => null, previewReset: () => {}, previewSnapshot: () => {} };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(appSource(), "// [PREVIEW-START]", "// [PREVIEW-END]")),
    sandbox,
  );
  assert.doesNotThrow(() => sandbox.openPreview("/file?a", "a.png"));
  assert.doesNotThrow(() => sandbox.closePreview());
  ok("no overlay in the DOM is a no-op, never a throw", true);
  ok("…and it reports nothing to close", sandbox.closePreview() === false);
}

// ---- the keyboard ----------------------------------------------------------
// A desktop has no fingers: ← / → are the swipe, Cmd/Ctrl+S is the Save button.
// What is pinned is OWNERSHIP (the preview is the topmost layer, so while it is
// up it takes those keys off whatever is behind it) and the two places that
// ownership deliberately stops — a CLOSED preview, and a picture with no file
// behind it, where the chord belongs to the browser.
function keyWorld() {
  const els = {
    preview: fakeElement("div"),
    "preview-img": fakeElement("img"),
    "preview-name": fakeElement("span"),
    "preview-count": fakeElement("span"),
    "preview-save": fakeElement("button"),
  };
  els.preview.hidden = true;
  els["preview-img"].removeAttribute = function (attr) { delete this[attr]; };
  const saved = [];
  const timers = [];
  const sandbox = {
    $: (id) => (id in els ? els[id] : null),
    previewReset: () => {},
    previewSnapshot: () => {},
    // The gesture layer's own maths (test_preview_gesture.js); the pager only
    // needs a view to slide across and a paint to call.
    previewBox: () => ({ view: { w: 400, h: 800 }, natural: { w: 400, h: 800 } }),
    previewPaint: () => {},
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    requestAnimationFrame: (fn) => { timers.push({ fn, ms: 0, frame: true }); },
    saveAttachment: (file, name) => { saved.push([file, name]); },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(appSource(), "// [PREVIEW-START]", "// [PREVIEW-END]")),
    sandbox,
  );
  // The slide lands only when a test says so: a held-down key fires INSIDE this
  // window, which is the whole reason the pager guards itself.
  const land = () => {
    for (const timer of timers) {
      if (!timer.done) { timer.done = true; timer.fn(); }
    }
  };
  return { s: sandbox, els, saved, timers, land };
}

const key = (k, mods) => Object.assign({ key: k, metaKey: false, ctrlKey: false,
  altKey: false, shiftKey: false }, mods || {});

const PAGES = [
  { src: "/file?a", name: "a.png", file: "/u/uploads/a.png" },
  { src: "/file?b", name: "b.png", file: "/u/uploads/b.png" },
  { src: "/file?c", name: "c.png", file: "/u/uploads/c.png" },
];

{
  const w = keyWorld();
  ok("a CLOSED preview claims nothing — the arrows belong to the layer under it",
    w.s.previewKey(key("ArrowRight")) === false && w.s.previewKey(key("s", { metaKey: true })) === false);

  w.s.openPreview(PAGES[0].src, PAGES[0].name, PAGES, 0);
  ok("→ is claimed", w.s.previewKey(key("ArrowRight")) === true);
  w.land();
  ok("…and turns the page", w.els["preview-img"].src === "/file?b");
  ok("…moving the name and the counter with it, like every other writer",
    w.els["preview-name"].textContent === "b.png" &&
    w.els["preview-count"].textContent === "2 / 3");

  w.s.previewKey(key("ArrowLeft"));
  w.land();
  ok("← turns it back", w.els["preview-img"].src === "/file?a");

  ok("← at the first picture is still CONSUMED: falling through would move the "
    + "step screen behind it instead", w.s.previewKey(key("ArrowLeft")) === true);
  w.land();
  ok("…while the picture stays where it is", w.els["preview-img"].src === "/file?a");
}

{
  // A held-down arrow repeats every few milliseconds — inside the 200ms slide.
  const w = keyWorld();
  w.s.openPreview(PAGES[0].src, PAGES[0].name, PAGES, 0);
  w.s.previewKey(key("ArrowRight"));
  w.s.previewKey(key("ArrowRight"));
  w.s.previewKey(key("ArrowRight"));
  w.land();
  ok("a key held down through the slide advances ONE page, not three at once",
    w.els["preview-img"].src === "/file?b");
  w.s.previewKey(key("ArrowRight"));
  w.land();
  ok("…and the pager is free again once it has landed",
    w.els["preview-img"].src === "/file?c");
}

{
  const w = keyWorld();
  w.s.openPreview(PAGES[0].src, PAGES[0].name, PAGES, 0);
  w.s.previewKey(key("ArrowRight"));
  w.land();
  ok("Cmd+S is claimed", w.s.previewKey(key("s", { metaKey: true })) === true);
  ok("…and saves the file of the picture IN FRONT OF YOU, read at the keystroke",
    w.saved.length === 1 && w.saved[0][0] === "/u/uploads/b.png" && w.saved[0][1] === "b.png");
  w.s.previewKey(key("S", { ctrlKey: true }));
  ok("Ctrl+S is the same key on a keyboard that has no Cmd",
    w.saved.length === 2 && w.saved[1][0] === "/u/uploads/b.png");

  w.s.previewKey(key("s", { metaKey: true, shiftKey: true }));
  w.s.previewKey(key("s", { metaKey: true, altKey: true }));
  ok("…but only that chord: Shift and Alt are somebody else's", w.saved.length === 2);

  w.s.previewKey(key("ArrowRight", { metaKey: true }));
  w.land();
  ok("a modified arrow is the browser's (history back/forward), not the pager's",
    w.els["preview-img"].src === "/file?b");
}

{
  // The Save BUTTON is hidden when there is no file; the chord follows it, or it
  // is a shortcut that silently does nothing — indistinguishable from one that
  // missed.
  const w = keyWorld();
  w.s.openPreview("/file?path=/tmp/x.png", "x.png");
  ok("a picture with no file behind it leaves Cmd+S to the browser",
    w.s.previewKey(key("s", { metaKey: true })) === false && w.saved.length === 0);
  ok("…while the arrows are still the preview's",
    w.s.previewKey(key("ArrowRight")) === true);
}

report("test_preview.js");
