// Node-only, dependency-free checks for previewing a PDF (#218).
//
// A PDF opens in the SAME viewer as a photograph, because its pages are the
// pictures. What that buys is every gesture already built — swipe turns a page,
// pinch reads the small print, the counter is the page number — and what it
// costs is the one thing a photo never has: the picture does not exist until
// the server renders it.
//
// So the interesting behaviour is all about that gap:
//
//   - page 1 goes up on the TAP, before the page COUNT is known, because
//     waiting for a round trip to look at an attachment is the failure L7 is
//     about;
//   - the count therefore lands LATE, into a preview that may since have been
//     closed or pointed at another file — a fetch has no way to cancel itself;
//   - and while a page renders the overlay says so, or a black screen reads as
//     a preview that opened onto nothing.
//
// Run manually: node tests/js/test_preview_pdf.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, fakeElement, checks } = require("./harness");

const { ok, report } = checks();
const src = appSource();

function world() {
  const els = {
    preview: fakeElement("div"),
    "preview-img": fakeElement("img"),
    "preview-name": fakeElement("span"),
    "preview-count": fakeElement("span"),
    "preview-status": fakeElement("div"),
  };
  els.preview.hidden = true;
  els["preview-img"].removeAttribute = function (attr) { delete this[attr]; };
  const fetched = [];
  const timers = [];
  const prefetched = [];
  const sandbox = {
    $: (id) => els[id] || null,
    token: "t0ken",
    URLSearchParams,
    // The count is asked for, never awaited here: previewAdoptPages is the
    // decision the answer lands in, and it is called directly so the LATE
    // arrival can be interleaved on purpose.
    fetch: (url) => { fetched.push(url); return { then: () => ({ then: () => ({ catch() {} }) }) }; },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].cancelled = true; },
    Image: function Image() { const self = this; Object.defineProperty(self, "src", {
      set(value) { prefetched.push(value); },
    }); },
    // The gesture layer's own business ([PREVIEW-GESTURE]); recorded, not run.
    previewReset: () => {},
    previewSnapshot: () => {},
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(src, "// [PREVIEW-START]", "// [PREVIEW-END]")),
    sandbox,
  );
  const fire = () => {
    for (const timer of timers) {
      if (!timer.cancelled && !timer.done) { timer.done = true; timer.fn(); }
    }
  };
  return { s: sandbox, els, fetched, timers, prefetched, fire };
}

// ---- opening does not wait for the server ---------------------------------
{
  const w = world();
  w.s.openPdfPreview("/u/uploads/guide.pdf", "guide.pdf");
  ok("the overlay is up on the tap", w.els.preview.hidden === false);
  ok("…showing page 1, before anything is known about the document",
    w.els["preview-img"].src === "/pdf/page?path=%2Fu%2Fuploads%2Fguide.pdf&page=1&token=t0ken");
  ok("…named by the file, which is what the owner tapped",
    w.els["preview-name"].textContent === "guide.pdf");
  ok("…with no counter yet: '1 / 1' would say a swipe does nothing",
    w.els["preview-count"].hidden === true);
  ok("…and the count is asked for afterwards",
    w.fetched.length === 1 &&
    w.fetched[0] === "/pdf/info?path=%2Fu%2Fuploads%2Fguide.pdf&token=t0ken");
}

// ---- the count arrives -----------------------------------------------------
{
  const w = world();
  w.s.openPdfPreview("/u/uploads/guide.pdf", "guide.pdf");
  ok("adopting the pages reports that it acted",
    w.s.previewAdoptPages("/u/uploads/guide.pdf", "guide.pdf", 12) === true);
  ok("…so the counter says which page this is",
    w.els["preview-count"].textContent === "1 / 12" && w.els["preview-count"].hidden === false);
  ok("…and the pages are the set a swipe moves through",
    w.s.previewGroupState().count === 12);
  ok("…without re-loading the page already on screen",
    w.els["preview-img"].src === "/pdf/page?path=%2Fu%2Fuploads%2Fguide.pdf&page=1&token=t0ken");
}

{
  // A one-page document is a lone picture, and its counter would be the "1 / 1"
  // that implies a swipe does something.
  const w = world();
  w.s.openPdfPreview("/u/uploads/receipt.pdf", "receipt.pdf");
  ok("a single page adopts nothing",
    w.s.previewAdoptPages("/u/uploads/receipt.pdf", "receipt.pdf", 1) === false);
  ok("…and shows no counter", w.els["preview-count"].hidden === true);
}

// ---- the count arrives LATE ------------------------------------------------
{
  const w = world();
  w.s.openPdfPreview("/u/uploads/guide.pdf", "guide.pdf");
  w.s.closePreview();
  ok("a count landing after the preview closed is dropped",
    w.s.previewAdoptPages("/u/uploads/guide.pdf", "guide.pdf", 12) === false);
  ok("…and does not reopen it", w.els.preview.hidden === true);
}

{
  const w = world();
  w.s.openPdfPreview("/u/uploads/guide.pdf", "guide.pdf");
  w.s.openPdfPreview("/u/uploads/contract.pdf", "contract.pdf");
  ok("a count for the document you LEFT is dropped",
    w.s.previewAdoptPages("/u/uploads/guide.pdf", "guide.pdf", 12) === false);
  ok("…so the counter never describes the wrong file",
    w.els["preview-count"].hidden === true);
  ok("the one for what is on screen is taken",
    w.s.previewAdoptPages("/u/uploads/contract.pdf", "contract.pdf", 4) === true);
  ok("…and its name stayed with it",
    w.els["preview-name"].textContent === "contract.pdf" &&
    w.els["preview-count"].textContent === "1 / 4");
}

{
  // The other direction, and the reason previewDoc is cleared rather than left
  // to be overwritten: a photo opened after a document must not be treated as a
  // page that needs rendering.
  const w = world();
  w.s.openPdfPreview("/u/uploads/guide.pdf", "guide.pdf");
  w.s.openPreview("/file?path=/u/uploads/cat.png", "cat.png");
  ok("a photo opened after a document adopts no pages",
    w.s.previewAdoptPages("/u/uploads/guide.pdf", "guide.pdf", 12) === false);
  w.fire();
  ok("…and waits on no renderer", w.els["preview-status"].hidden === true);
}

// ---- what the overlay says while it waits ---------------------------------
{
  const w = world();
  w.s.openPdfPreview("/u/uploads/guide.pdf", "guide.pdf");
  ok("nothing is said immediately — a page that renders fast shows no spinner",
    w.els["preview-status"].hidden === true);
  w.fire();
  ok("…and once it is slow, it says which page it is waiting for",
    w.els["preview-status"].textContent === "Rendering page 1…" &&
    w.els["preview-status"].hidden === false);
  w.s.previewPageLoaded();
  ok("…and stops saying it the moment the page is there",
    w.els["preview-status"].hidden === true && w.els["preview-status"].textContent === "");
}

{
  const w = world();
  w.s.openPdfPreview("/u/uploads/guide.pdf", "guide.pdf");
  w.s.previewAdoptPages("/u/uploads/guide.pdf", "guide.pdf", 12);
  w.s.previewPageFailed();
  ok("a page that could not be rendered says so, rather than leaving black",
    w.els["preview-status"].textContent === "Page 1 couldn't be rendered");
  // Seen in Chrome: a broken <img> is not blank. It draws the browser's own
  // glyph WITH the alt text, in the top corner over the bar, so the file name
  // appeared twice and the message read as a third thing on an empty screen.
  ok("…and the broken image itself is taken off the screen",
    w.els["preview-img"].classList.contains("broken"));
  w.s.previewShow(1);
  ok("the next page starts un-broken", !w.els["preview-img"].classList.contains("broken"));
  w.s.previewPageFailed();
  w.s.previewPageLoaded();
  ok("…and a page that arrives clears it too",
    !w.els["preview-img"].classList.contains("broken"));
  w.s.previewPageFailed();
  w.s.closePreview();
  ok("…and closing takes the message with it",
    w.els["preview-status"].hidden === true);
}

{
  const w = world();
  w.s.openPreview("/file?path=/u/uploads/cat.png", "cat.png");
  w.s.previewPageFailed();
  ok("a photo that will not load is a different sentence — it is not a render",
    w.els["preview-status"].textContent === "This picture couldn't be loaded");
}

// ---- prefetching neighbours ------------------------------------------------
{
  const w = world();
  w.s.openPdfPreview("/u/uploads/guide.pdf", "guide.pdf");
  ok("nothing to prefetch before the document has pages", w.prefetched.length === 0);
  w.s.previewAdoptPages("/u/uploads/guide.pdf", "guide.pdf", 12);
  ok("page 2 is fetched while page 1 is being read",
    w.prefetched.length === 1 &&
    w.prefetched[0] === "/pdf/page?path=%2Fu%2Fuploads%2Fguide.pdf&page=2&token=t0ken");
}

{
  const w = world();
  w.s.openPreview("/file?a", "a.png", [{ src: "/file?a", name: "a.png" }, { src: "/file?b", name: "b.png" }], 0);
  ok("a photo's neighbour is NOT prefetched — those bytes are already here",
    w.prefetched.length === 0);
}

// ---- which paths are documents --------------------------------------------
{
  const w = world();
  ok("a .pdf is one", w.s.isPdfPath("/u/uploads/guide.pdf") === true);
  ok("…whatever case it was saved in", w.s.isPdfPath("/u/uploads/GUIDE.PDF") === true);
  ok("a photo is not", w.s.isPdfPath("/u/uploads/cat.png") === false);
  ok("and a missing path is not a crash", w.s.isPdfPath(null) === false);
}

// ---- the chip in a sent message -------------------------------------------
{
  const opened = [];
  const saved = [];
  const sandbox = {
    document: { createElement: (tag) => fakeElement(tag) },
    shortName: (name) => name,
    isPdfPath: (path) => /\.pdf$/i.test(String(path || "")),
    openPdfPreview: (path, name) => opened.push([path, name]),
    saveAttachment: (path, name) => saved.push([path, name]),
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(src, "function attachmentChip", "function addUserMsg")),
    sandbox,
  );
  // The note the server wrote for a PDF in the uploads dir…
  const doc = sandbox.attachmentChip({
    kind: "document", name: "guide.pdf", path: "/u/uploads/guide.pdf",
  });
  ok("a PDF's chip is openable", doc.classList.contains("attachment-openable"));
  doc.onclick();
  ok("…and opens THAT file, by path",
    opened.length === 1 && opened[0][0] === "/u/uploads/guide.pdf");

  // …and the note for a PDF that was never uploaded, which is logged as a
  // plain file and is exactly as readable.
  const plain = sandbox.attachmentChip({
    kind: "file", name: "report.pdf", path: "/home/me/report.pdf",
  });
  ok("a PDF outside uploads opens too — the PATH decides, not the note's kind",
    plain.classList.contains("attachment-openable"));

  const other = sandbox.attachmentChip({
    kind: "file", name: "notes.txt", path: "/home/me/notes.txt",
  });
  other.onclick();
  ok("a chip with nothing to show it in saves instead — never nothing",
    opened.length === 1 && saved.length === 1 && saved[0][0] === "/home/me/notes.txt");
}

report("test_preview_pdf.js");
