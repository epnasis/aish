// Node-only, dependency-free check for the evidence frame on a trace row (#289).
//
// aish drives real pages the owner cannot see. A frame that is captured and
// stored but never drawn answers a smaller question than the one asked, so the
// rendering is the feature — and it has to render the SAME on a chat redrawn
// from its log as it did live (L2), which is what makes a pure function of the
// step the only acceptable shape.
//
// Runs the REAL traceFrame/toolFinish/traceStep extracted from app.js by
// marker, never a hand-copied duplicate.
//
// Run manually: node tests/js/test_trace_frame_row.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);

function slice(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker);
  assert(start !== -1 && end !== -1, `markers not found: ${startMarker} … ${endMarker}`);
  return src.slice(start, end);
}

function makeElement(tag) {
  const found = new Map();
  const el = {
    tagName: tag, className: "", textContent: "", innerHTML: "", src: "", alt: "",
    loading: "", children: [], style: {}, dataset: {},
    append(...nodes) { el.children.push(...nodes); },
    appendChild(node) { el.children.push(node); return node; },
    remove() {},
    addEventListener() {},
    classList: {
      _set: new Set(),
      add(...cs) { cs.forEach((c) => this._set.add(c)); },
      remove(...cs) { cs.forEach((c) => this._set.delete(c)); },
      contains(c) { return this._set.has(c); },
      toggle(c) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); },
    },
    querySelector(sel) {
      const byClass = findByClass(el, sel);
      if (byClass) return byClass;
      if (sel === ".step-sub") return null;
      if (!found.has(sel)) found.set(sel, makeElement("div"));
      return found.get(sel);
    },
    querySelectorAll() { return []; },
  };
  return el;
}

function findByClass(el, sel) {
  if (!sel.startsWith(".")) return null;
  const cls = sel.slice(1);
  for (const child of el.children || []) {
    if (typeof child.className === "string"
        && child.className.split(/\s+/).includes(cls)) return child;
    const deeper = child.children ? findByClass(child, sel) : null;
    if (deeper) return deeper;
  }
  return null;
}

function makeSandbox() {
  const opened = [];
  const sandbox = {
    document: { createElement: makeElement, createTextNode: (t) => ({ textContent: t }) },
    messagesEl: makeElement("div"),
    replaying: false,
    turnStart: 0,
    currentTrace: null,
    currentTurnId: "",
    turnAnchorEl: null,
    SPINNER: "",
    TOOL_META: { browse: ["Browsed", "world", "--blue"] },
    traceSvg: () => "",
    fmtSecs: (s) => `${s}s`,
    opened,
    // The real policy function lives elsewhere in app.js; what matters here is
    // that traceFrame goes THROUGH it rather than building a URL itself.
    imageSrc: (target) => (target.startsWith("/") ? `/file?path=${target}&token=t` : null),
    openPreview: (...args) => opened.push(args),
    knowledgeTag() {},
    clampNote: (text) => ({ className: "step-note", textContent: text, children: [] }),
    renderDiff: () => makeElement("div"),
    renderErrorBox() {},
    updateTraceHead() {},
    updateScrollHints() {},
    refreshStatusline() {},
    measurePinnedTrace() {},
    scrollToEnd() {},
    removeQueueChip() {},
    finalizeAnswerRow() {},
    setInterval: () => 0,
    clearInterval() {},
    requestAnimationFrame() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("// [TRACE-OPEN-START]", "// [TRACE-OPEN-END]"), sandbox);
  vm.runInContext(slice("function pinTrace(t) {", "const WRAP_SVG"), sandbox);
  assert(typeof sandbox.traceFrame === "function", "traceFrame not extracted");
  assert(typeof sandbox.traceStep === "function", "traceStep not extracted");
  return sandbox;
}

function browsed(s, extra) {
  s.traceStep({ kind: "tool_start", name: "browse", summary: "eon.pl", call: 1 });
  s.traceStep({ kind: "tool", name: "browse", summary: "eon.pl", call: 1,
                ok: true, secs: 1.2, ...extra });
  return (s.currentTrace.inner.children || []).filter((r) => r.children);
}

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}`);
    console.error(`       ${err.message}`);
  }
}

check("a browse step draws the picture of the page it read", () => {
  const s = makeSandbox();
  const rows = browsed(s, { frame: "/state/media/abc-browse-eon-pl.jpg" });
  const frame = findByClass(rows[0], ".step-frame");
  assert(frame, "no frame was drawn on the row");
  const img = frame.children[0];
  assert.equal(img.src, "/file?path=/state/media/abc-browse-eon-pl.jpg&token=t");
});

check("the src is built by the shared image policy, never by hand", () => {
  // A frame is a picture in the transcript like any other, and the one
  // whitelist/token rule has to govern it too.
  const s = makeSandbox();
  s.imageSrc = () => null;
  const rows = browsed(s, { frame: "/state/media/abc.jpg" });
  assert(!findByClass(rows[0], ".step-frame"), "policy refused the src and it drew anyway");
});

check("tapping the picture opens the real viewer", () => {
  const s = makeSandbox();
  const rows = browsed(s, { frame: "/state/media/abc.jpg" });
  findByClass(rows[0], ".step-frame").children[0].onclick();
  assert.equal(s.opened.length, 1);
  assert.equal(s.opened[0][0], "/file?path=/state/media/abc.jpg&token=t");
  assert.equal(s.opened[0][2][0].file, "/state/media/abc.jpg");
});

check("a frame that will not load says so, not that none was taken", () => {
  // The media store is a bounded LRU cache: a record outliving its picture is
  // the ordinary end of a frame's life, and it must never read as an absence.
  // The wording claims only what a failed load proves — from here an evicted
  // file and a stale token are the same event, and `aish explain` is the
  // surface that can tell them apart.
  const s = makeSandbox();
  const rows = browsed(s, { frame: "/state/media/abc.jpg" });
  const frame = findByClass(rows[0], ".step-frame");
  frame.children[0].onerror();
  assert(/could not be loaded/.test(frame.textContent), frame.textContent);
  assert(!/purged/.test(frame.textContent), "the browser cannot know it was purged");
});

check("a sign-in page says why it has no picture", () => {
  const s = makeSandbox();
  const rows = browsed(s, { frame_skipped: "signin" });
  const note = findByClass(rows[0], ".step-frame-none");
  assert(note, "no reason was drawn");
  assert(/asking for a password/.test(note.textContent), note.textContent);
  assert(!findByClass(rows[0], ".step-frame"), "a sign-in page must never be pictured");
});

check("a failed capture is told apart from a page that had no picture to take", () => {
  const s = makeSandbox();
  const rows = browsed(s, { frame_skipped: "failed" });
  assert(/capture did not come back/.test(
    findByClass(rows[0], ".step-frame-none").textContent));
});

check("a step with no frame at all draws nothing", () => {
  // The third state: a log written before frames existed, and every tool that
  // never had a page. Neither may grow a row claiming a capture was considered.
  const s = makeSandbox();
  const rows = browsed(s, {});
  assert(!findByClass(rows[0], ".step-frame"));
  assert(!findByClass(rows[0], ".step-frame-none"));
  assert.equal(s.traceFrame({ kind: "tool", name: "run_command" }), null);
});

check("an unknown reason draws nothing rather than a blank note", () => {
  const s = makeSandbox();
  assert.equal(s.traceFrame({ frame_skipped: "something a later aish wrote" }), null);
});

check("the row renders identically live and on replay", () => {
  // L2. traceFrame is a pure function of the step, so the only way hot and
  // cold can diverge is if something else on the row is consulted.
  const live = makeSandbox();
  const cold = makeSandbox();
  cold.replaying = true;
  const step = { frame: "/state/media/abc.jpg" };
  const a = live.traceFrame(step);
  const b = cold.traceFrame(step);
  assert.equal(a.className, b.className);
  assert.equal(a.children[0].src, b.children[0].src);
  assert.equal(a.children[0].alt, b.children[0].alt);
});

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("trace frame row: all checks passed");
