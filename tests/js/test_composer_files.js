// Node-only, dependency-free checks for [COMPOSER-FILES] and [FILE-DROP].
//
// Three ways a file reaches the composer — the ＋ picker, a PASTE, a DROP —
// and one destination. What is pinned here is mostly the two things that made
// paste look unsupported rather than broken:
//
//   - a pasted screenshot arrives as a Blob with an EMPTY name, and /upload
//     refuses an empty name, so the paste failed with "invalid file name";
//   - Chrome names every one of them "image.png", so a day of pasted
//     screenshots is one name and a pile of -1, -2, -3 suffixes.
//
// And the rule that keeps paste from taking something away: it is ADDITIVE.
// Copying a chart out of a spreadsheet puts an image AND text on the clipboard,
// and swallowing the event to grab the image silently drops the text.
//
// Run manually: node tests/js/test_composer_files.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const {
  appSource, extract, surface, fakeElement, checks, expectReached,
} = require("./harness");

const { ok, report } = checks();
const src = appSource();

function world() {
  const uploaded = [];
  const listeners = {};
  const body = fakeElement("body");
  const sandbox = {
    // The real uploadFile is exercised by the server tests; here we only care
    // WHICH files reach it and under WHAT name.
    uploadFile: (file) => { uploaded.push(file); return Promise.resolve(); },
    Date,
    File,
    Array,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(src, "// [COMPOSER-FILES-START]", "// [COMPOSER-FILES-END]")),
    sandbox,
  );
  vm.runInContext(
    surface(extract(src, "// [FILE-DROP-START]", "// [FILE-DROP-END]")),
    sandbox,
  );
  const target = {
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  };
  sandbox.installFileDrop(target, body);
  const fire = (type, event) => {
    for (const fn of listeners[type] || []) fn(event);
  };
  return { s: sandbox, uploaded, fire, body, listeners };
}

// Let every queued microtask run: the paste and drop handlers await each
// upload in turn, so the second file lands one tick after the event returns.
const settle = () => new Promise((resolve) => setImmediate(resolve));

const file = (name, type = "image/png") => ({ name, type });
const drag = (types, files) => {
  let prevented = false;
  return {
    dataTransfer: { types, files },
    preventDefault() { prevented = true; },
    get prevented() { return prevented; },
  };
};

// ---- naming ---------------------------------------------------------------
{
  const { s } = world();
  ok("a real filename is kept as-is", s.uploadName(file("holiday.jpg")) === "holiday.jpg");

  // The two shapes that made a paste fail or collide.
  const nameless = s.uploadName(file("", "image/png"));
  ok("a nameless paste is given a name", /^pasted-[\d-]+\.png$/.test(nameless));
  const chrome = s.uploadName(file("image.png", "image/png"));
  ok("Chrome's generic image.png is replaced, not kept", /^pasted-/.test(chrome));

  // /upload rejects a dot-leading name outright.
  ok("a dot-leading name never reaches the server",
    !s.uploadName(file(".hidden", "image/png")).startsWith("."));

  // An unknown or absent type must still produce something storable.
  ok("no type at all still yields a usable name",
    /^pasted-[\d-]+\.bin$/.test(s.uploadName({ name: "" })));
  ok("a hostile type cannot smuggle path separators into the extension",
    !s.uploadName({ name: "", type: "image/../../etc" }).includes("/"));
}

// ---- what counts as a file ------------------------------------------------
{
  const { s } = world();
  const f = file("a.png");
  assert.deepStrictEqual(s.transferFiles({ files: [f] }).length, 1);
  ok("files are read from .files where the browser populates it", true);

  // Older Safari populates only .items.
  const viaItems = s.transferFiles({
    files: [],
    items: [
      { kind: "string", getAsFile: () => null },
      { kind: "file", getAsFile: () => f },
    ],
  });
  ok("…and from .items where it does not", viaItems.length === 1);
  ok("no clipboard at all is not a crash", s.transferFiles(null).length === 0);
}

// ---- paste is additive ----------------------------------------------------
// Everything below is async; without this a harness change that let the process
// exit early would assert nothing and still pass.
const done = expectReached("the async paste/drop checks never ran");
(async () => {
{
  const w = world();
  let prevented = false;
  await w.s.onInputPaste({
    clipboardData: { files: [file("shot.png")] },
    preventDefault() { prevented = true; },
  });
  ok("a pasted image is uploaded", w.uploaded.length === 1);
  ok("…and the event is NEVER swallowed, so text on the same clipboard still pastes",
    prevented === false);
}

{
  const w = world();
  await w.s.onInputPaste({ clipboardData: { files: [] }, preventDefault() {} });
  ok("a plain text paste uploads nothing", w.uploaded.length === 0);
}

// ---- drop -----------------------------------------------------------------
{
  const w = world();
  const over = drag(["Files"], []);
  w.fire("dragover", over);
  ok("a dragover carrying files is prevented — or the browser NAVIGATES to the "
    + "file and the chat, draft and all, is gone", over.prevented);

  const text = drag(["text/plain"], []);
  w.fire("dragover", text);
  ok("dragging selected text over the page is left alone", !text.prevented);
}

{
  const w = world();
  w.fire("dragenter", drag(["Files"], []));
  ok("dragging files in shows where they will land", w.body.classList.contains("dropping"));

  // dragenter/leave fire per element crossed, not per window: a single leave
  // while still inside the page must not clear the highlight.
  w.fire("dragenter", drag(["Files"], []));
  w.fire("dragleave", {});
  ok("crossing an inner element does not flicker the overlay off",
    w.body.classList.contains("dropping"));
  w.fire("dragleave", {});
  ok("…and leaving for real clears it", !w.body.classList.contains("dropping"));
}

{
  const w = world();
  w.fire("dragenter", drag(["Files"], []));
  const dropped = drag(["Files"], [file("a.png"), file("b.pdf", "application/pdf")]);
  w.fire("drop", dropped);
  await settle();
  ok("a drop uploads every file in it", w.uploaded.length === 2);
  ok("…is prevented (see above)", dropped.prevented);
  ok("…and takes the overlay down", !w.body.classList.contains("dropping"));
}

{
  const w = world();
  w.fire("dragenter", drag(["Files"], []));
  w.fire("drop", drag(["text/plain"], []));
  await settle();
  ok("dropping non-file content uploads nothing", w.uploaded.length === 0);
  ok("…but still clears the overlay, which must never get stuck on",
    !w.body.classList.contains("dropping"));
}

done();
report("test_composer_files.js");
})();
