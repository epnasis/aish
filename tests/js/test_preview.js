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
  const sandbox = { $: (id) => els[id] || null };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(appSource(), "// [PREVIEW-START]", "// [PREVIEW-END]")),
    sandbox,
  );
  return { s: sandbox, els };
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
  const sandbox = { $: () => null };
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

report("test_preview.js");
