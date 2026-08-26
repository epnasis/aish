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
    // The real URL builders live elsewhere in app.js; what matters here is that
    // traceFrame goes THROUGH one rather than building a URL itself — and that
    // it is `frameSrc`, not `imageSrc`. A frame left the workspace boundary in
    // #318, so `/file` does not serve one any more.
    imageSrc: (target) => (target.startsWith("/") ? `/file?path=${target}&token=t` : null),
    frameSrc: (target) => (target.startsWith("/") ? `/frame?path=${target}&token=t` : null),
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
  const rows = browsed(s, { frame: "/state/frames/abc-browse-eon-pl.jpg" });
  const frame = findByClass(rows[0], ".step-frame");
  assert(frame, "no frame was drawn on the row");
  const img = frame.children[0];
  assert.equal(img.src, "/frame?path=/state/frames/abc-browse-eon-pl.jpg&token=t");
});

check("the src comes from the frame endpoint, never the workspace one", () => {
  // #318. The bytes moved to a store of their own so the MODEL could no longer
  // name a frame to show_image and read a hostile page into its own context;
  // /file is scoped to the boundary that store left, so a row still asking it
  // for the picture would draw a 403 in every browsed step.
  const s = makeSandbox();
  s.imageSrc = () => { throw new Error("a frame must not go through /file"); };
  const rows = browsed(s, { frame: "/state/frames/abc.jpg" });
  assert.equal(
    findByClass(rows[0], ".step-frame").children[0].src,
    "/frame?path=/state/frames/abc.jpg&token=t"
  );
});

check("the src is built by the shared policy function, never by hand", () => {
  // One place decides which pictures may load and with what token; a row that
  // built its own URL would be a second answer.
  const s = makeSandbox();
  s.frameSrc = () => null;
  const rows = browsed(s, { frame: "/state/frames/abc.jpg" });
  assert(!findByClass(rows[0], ".step-frame"), "policy refused the src and it drew anyway");
});

check("tapping the picture opens the real viewer", () => {
  const s = makeSandbox();
  const rows = browsed(s, { frame: "/state/frames/abc.jpg" });
  findByClass(rows[0], ".step-frame").children[0].onclick();
  assert.equal(s.opened.length, 1);
  assert.equal(s.opened[0][0], "/frame?path=/state/frames/abc.jpg&token=t");
  // The viewer's Save button saves the FILE, so it carries the path and not the
  // display URL — /download knows a frame for the same reason /frame does.
  assert.equal(s.opened[0][2][0].file, "/state/frames/abc.jpg");
});

check("a frame that will not load says so, not that none was taken", () => {
  // The frame store is a bounded LRU cache: a record outliving its picture is
  // the ordinary end of a frame's life, and it must never read as an absence.
  // The wording claims only what a failed load proves — from here an evicted
  // file and a stale token are the same event, and `aish explain` is the
  // surface that can tell them apart.
  const s = makeSandbox();
  const rows = browsed(s, { frame: "/state/frames/abc.jpg" });
  const frame = findByClass(rows[0], ".step-frame");
  frame.children[0].onerror();
  assert(/could not be loaded/.test(frame.textContent), frame.textContent);
  assert(!/purged/.test(frame.textContent), "the browser cannot know it was purged");
});

check("a page with a password box says why it has no picture", () => {
  // The row says what was SEEN — a password box — not that the page WAS a
  // sign-in. The check behind it cannot tell the second thing.
  const s = makeSandbox();
  const rows = browsed(s, { frame_skipped: "password" });
  const note = findByClass(rows[0], ".step-frame-none");
  assert(note, "no reason was drawn");
  assert(/showing a password box/.test(note.textContent), note.textContent);
  assert(!findByClass(rows[0], ".step-frame"), "such a page must never be pictured");
});

check("a page that would not say is told apart from one that had no box", () => {
  // The two are opposite facts: one is a page aish read and photographed a
  // decision about, the other is a page it could not read at all.
  const s = makeSandbox();
  const rows = browsed(s, { frame_skipped: "unknown" });
  assert(/would not say/.test(
    findByClass(rows[0], ".step-frame-none").textContent));
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

check("a reason this client does not know draws nothing, not a blank note", () => {
  // A later aish adding a fifth reason must degrade to silence here, never to
  // an empty italic line that reads as a bug.
  const s = makeSandbox();
  assert.equal(s.traceFrame({ frame_skipped: "something a later aish wrote" }), null);
});

check("the row renders identically live and on replay", () => {
  // L2. traceFrame is a pure function of the step, so the only way hot and
  // cold can diverge is if something else on the row is consulted.
  const live = makeSandbox();
  const cold = makeSandbox();
  cold.replaying = true;
  const step = { frame: "/state/frames/abc.jpg" };
  const a = live.traceFrame(step);
  const b = cold.traceFrame(step);
  assert.equal(a.className, b.className);
  assert.equal(a.children[0].src, b.children[0].src);
  assert.equal(a.children[0].alt, b.children[0].alt);
});

// ---------------------------------------------------------------------------
// The picture as NAVIGATION EVIDENCE, and the two things that were captured
// and drawn nowhere: the page's own console, and the sign-in attempt's page.

check("the picture says where it was taken and what the press did to get there", () => {
  // "Watching it happen" is a poor instrument — you have to be looking at the
  // right second. The reviewable record is the answer, and a frame with no
  // caption answers "what did this page look like" rather than "what did this
  // press do".
  const s = makeSandbox();
  const rows = browsed(s, {
    frame: "/state/frames/abc.jpg",
    frame_url: "https://eon.pl/mojeon/faktury",
    frame_from: "https://eon.pl/mojeon",
  });
  const where = findByClass(rows[0], ".step-frame-where");
  assert(where, "the picture carried no caption");
  assert(/eon\.pl\/mojeon\/faktury/.test(where.textContent), where.textContent);
  // Worded to what the writer knows: `frame_from` is the page the chat was
  // LAST SHOWN. "Navigated here from X" would claim nothing sat between them.
  assert(/aish was on https:\/\/eon\.pl\/mojeon before this/.test(where.textContent),
         where.textContent);
});

check("a press that did not move the page says so, rather than nothing", () => {
  // The dead-control signal, delivered on the FIRST press: an address that did
  // not change is a fact about what the press did, not an absence.
  const s = makeSandbox();
  const rows = browsed(s, {
    frame: "/state/frames/abc.jpg", frame_url: "https://eon.pl/mojeon",
  });
  assert(/the address did not change/.test(
    findByClass(rows[0], ".step-frame-where").textContent));
});

check("a frame from a log written before this grows no caption", () => {
  // The renderer must never claim the writer tried and failed to record an
  // address. No key, no caption.
  const s = makeSandbox();
  const rows = browsed(s, { frame: "/state/frames/abc.jpg" });
  assert(!findByClass(rows[0], ".step-frame-where"));
});

check("the console is drawn on a step aish itself observed going wrong", () => {
  // The whole point of the record: a day of guessing at eon.pl because a
  // handler's ReferenceError went to a console nobody kept. `unchanged` is
  // that exact shape — the press landed, the page did not move, and the
  // thrown handler is the other half of the answer.
  const s = makeSandbox();
  const rows = browsed(s, {
    unchanged: true,
    console: ["uncaught: ReferenceError: grecaptcha is not defined"],
  });
  const box = findByClass(rows[0], ".step-console");
  assert(box, "the console was not drawn");
  assert(/the page's words, not aish's/.test(box.children[0].textContent),
         box.children[0].textContent);
  assert(/grecaptcha is not defined/.test(box.children[1].textContent));
});

check("a clean step's console is recorded but not drawn", () => {
  // The owner's finding, hours after this shipped: the number of sites that
  // log errors on every healthy page is enormous, and a successful sign-in
  // rendered covered in error blocks. The criterion is aish's OWN observation
  // of anomaly, never the page's noisiness — a clean step draws nothing, and
  // the record keeps the lines for `aish explain`.
  const s = makeSandbox();
  const rows = browsed(s, {
    frame: "/state/frames/abc.jpg",
    console: ["error: the site's everyday noise", "warning: more of it"],
  });
  assert(!findByClass(rows[0], ".step-console"),
         "a clean step painted the page's noise");
});

check("every recorded anomaly on the step surfaces the console", () => {
  // Enumerable from the step alone, so hot and cold cannot disagree: the tool
  // failed, aish said it could not act, the delta came back empty, or a click
  // could not land and was never got through.
  for (const extra of [
    { ok: false },
    { problem: "aish could not click 'Zaloguj'" },
    { unchanged: true },
    { covered: { by: "clb clb-container", dismissed: false } },
  ]) {
    const s = makeSandbox();
    const rows = browsed(s, { console: ["error: boom"], ...extra });
    assert(findByClass(rows[0], ".step-console"),
           `no console for ${JSON.stringify(extra)}`);
  }
});

check("a cover that was dismissed is a repaired step, not an anomaly", () => {
  // Consent banners are everywhere; a dismissed one whose retry went through
  // is the action working. Surfacing the console on those would rebuild the
  // noise this rule exists to remove.
  const s = makeSandbox();
  const rows = browsed(s, {
    console: ["error: boom"],
    covered: { by: "cookie-bar", dismissed: true },
  });
  assert(findByClass(rows[0], ".step-covered"), "the cover row still draws");
  assert(!findByClass(rows[0], ".step-console"));
});

check("aish's own problem sentence is drawn, in aish's un-boxed style", () => {
  // It is what explains why a console follows on a row whose tool-level
  // status still reads ok — a browse whose action failed still returns a
  // page, so the row shows no error of its own.
  const s = makeSandbox();
  const rows = browsed(s, {
    problem: "aish could not click 'Zaloguj' — the control may be inert",
  });
  const box = findByClass(rows[0], ".step-problem");
  assert(box, "the problem sentence was not drawn");
  assert(/could not click 'Zaloguj'/.test(box.textContent), box.textContent);
  assert.equal(box.innerHTML, "");
});

check("a covered step draws the cover's wording, not the model's problem", () => {
  // COVERED_STUCK and the cover are one fact worded twice — the problem text
  // is written TO THE MODEL ("press whatever closes it and try again"), and
  // the covered block is the owner's wording of the same thing.
  const s = makeSandbox();
  const rows = browsed(s, {
    problem: "something the page calls 'clb' is sitting on top of it",
    covered: { by: "clb", dismissed: false },
  });
  assert(findByClass(rows[0], ".step-covered"));
  assert(!findByClass(rows[0], ".step-problem"));
});

check("an action that changed nothing says so on the row", () => {
  // The "did that click work" fact, as recorded: a fact and not a failure —
  // worded to exactly what the writer computed.
  const s = makeSandbox();
  const rows = browsed(s, { unchanged: true });
  const box = findByClass(rows[0], ".step-unchanged");
  assert(box, "the unchanged fact was not drawn");
  assert(/nothing on the page changed/.test(box.textContent), box.textContent);
});

check("console lines are drawn as TEXT, never as markup", () => {
  // Page-authored, so the residual attack is a line that looks like part of
  // the app. textContent is what makes that structurally impossible.
  const s = makeSandbox();
  const rows = browsed(s, {
    ok: false, console: ['error: <img src=x onerror="alert(1)">'],
  });
  const box = findByClass(rows[0], ".step-console");
  assert.equal(box.children[1].innerHTML, "");
  assert(/onerror/.test(box.children[1].textContent));
});

check("a healthy action costs nothing at all", () => {
  // Empty is the ordinary case. A row that said "the console was clean" on
  // every step is the noise that hides the one step where it was not.
  const s = makeSandbox();
  const rows = browsed(s, { console: [], frame: "/state/frames/abc.jpg" });
  assert(!findByClass(rows[0], ".step-console"));
  assert(!findByClass(rows[0], ".step-signin"));
});

check("the sign-in attempt's own page is shown, labelled as a different page", () => {
  // Captured since #320 and rendered nowhere: he went looking for it and could
  // not find it. It is NOT hung on the step's `frame` key — that key claims
  // "the page the model was SHOWN", and the model is never shown this one.
  const s = makeSandbox();
  const rows = browsed(s, {
    frame: "/state/frames/page.jpg",
    signin: { host: "eon.pl", frame: "/state/frames/login.jpg" },
  });
  const box = findByClass(rows[0], ".step-signin");
  assert(box, "the sign-in evidence was not drawn");
  assert(/attempted an automatic sign-in at eon\.pl/.test(box.children[0].textContent),
         box.children[0].textContent);
  assert(/not the one above/.test(box.children[0].textContent));
  const shot = findByClass(box, ".step-frame");
  assert.equal(shot.children[0].src, "/frame?path=/state/frames/login.jpg&token=t");
  // …and the page frame above it is still the page frame, unborrowed.
  assert.equal(
    findByClass(rows[0], ".step-frame").children[0].src,
    "/frame?path=/state/frames/page.jpg&token=t"
  );
});

check("a sign-in with no picture says which absence, not nothing", () => {
  const s = makeSandbox();
  const rows = browsed(s, {
    signin: { host: "eon.pl", frame_skipped: "hands" },
  });
  const box = findByClass(rows[0], ".step-signin");
  assert(/driving the browser yourself/.test(
    findByClass(box, ".step-frame-none").textContent));
});

check("the sign-in page's console is drawn and attributed to THAT page", () => {
  // No `ok` on the block — an older log, or a failed attempt — errs toward
  // showing: an attempt with an unknown ending is exactly one worth reading.
  const s = makeSandbox();
  const rows = browsed(s, {
    signin: { host: "eon.pl", console: ["error: grecaptcha failed to load"] },
  });
  const box = findByClass(findByClass(rows[0], ".step-signin"), ".step-console");
  assert(/the sign-in page wrote this/.test(box.children[0].textContent),
         box.children[0].textContent);
});

check("a sign-in whose session came up keeps its console off the row", () => {
  // The owner's first successful automatic sign-in rendered covered in the
  // login page's everyday errors. `ok` is the session seen to come up, read
  // afresh — recorded by the writer, so the renderer may finally say it.
  const s = makeSandbox();
  const rows = browsed(s, {
    signin: { host: "eon.pl", ok: true, console: ["error: everyday noise"] },
  });
  const box = findByClass(rows[0], ".step-signin");
  assert(/and the session came up/.test(box.children[0].textContent),
         box.children[0].textContent);
  assert(!findByClass(box, ".step-console"));
});

check("a sign-in not seen to come up shows its console and says so", () => {
  const s = makeSandbox();
  const rows = browsed(s, {
    signin: { host: "eon.pl", ok: false, console: ["error: boom"] },
  });
  const box = findByClass(rows[0], ".step-signin");
  assert(/the session was not seen to come up/.test(box.children[0].textContent),
         box.children[0].textContent);
  assert(findByClass(box, ".step-console"), "the failure's console was hidden");
});

check("a block from an older log claims no outcome either way", () => {
  // Absent is a third fact: a log written before the outcome was recorded.
  // The renderer may not say more than the record does.
  const s = makeSandbox();
  const rows = browsed(s, { signin: { host: "eon.pl" } });
  const head = findByClass(rows[0], ".step-signin").children[0];
  assert(!/session/.test(head.textContent), head.textContent);
});

check("a step with a sign-in block but no host draws nothing", () => {
  // `host` is what says an attempt happened at all. An empty block must read
  // as "no sign-in", never as "an attempt with nothing to show".
  const s = makeSandbox();
  assert.equal(s.traceFrame({ signin: {} }), null);
  assert.equal(s.traceFrame({ signin: null }), null);
});

// ---------------------------------------------------------------------------
// #321: what was SITTING ON TOP of the control this step pressed.
//
// A press that never landed writes nothing to a console — nothing ran — so the
// driver is the only witness. Every one of the four wrong diagnoses of the
// eon.pl sign-in was argued in a session where Chrome knew the click was
// intercepted and named the element, and it reached nobody.

check("what covered a control is named on the row", () => {
  const s = makeSandbox();
  const rows = browsed(s, {
    covered: { by: "clb clb-container", dismissed: false },
  });
  const box = findByClass(rows[0], ".step-covered");
  assert(box, "the cover was not drawn");
  assert(/clb clb-container/.test(box.textContent), box.textContent);
  assert(/could not land/.test(box.textContent), box.textContent);
});

check("a cover that was taken down says so", () => {
  const s = makeSandbox();
  const rows = browsed(s, { covered: { by: "cookie-bar", dismissed: true } });
  assert(/dismissed it and clicked again/.test(
    findByClass(rows[0], ".step-covered").textContent));
});

check("the element name is drawn as TEXT, never as markup", () => {
  // It is the PAGE's word for itself — an id or a class the site chose — so
  // the residual attack is the same one console lines have.
  const s = makeSandbox();
  const rows = browsed(s, {
    covered: { by: '<img src=x onerror="alert(1)">', dismissed: false },
  });
  const box = findByClass(rows[0], ".step-covered");
  assert(box.innerHTML === "", "the name was set as markup");
  assert(/onerror/.test(box.textContent));
});

check("a step with nothing covering anything draws nothing", () => {
  // Absent rather than empty: a press nothing obstructed and a step written
  // before any of this existed are different facts, and neither grows a row.
  const s = makeSandbox();
  const rows = browsed(s, { covered: {}, frame: "/state/frames/abc.jpg" });
  assert(!findByClass(rows[0], ".step-covered"));
  assert.equal(s.traceFrame({ covered: { by: "" } }), null);
  assert.equal(s.traceFrame({ covered: null }), null);
});

check("the sign-in page's own cover is drawn under its heading", () => {
  const s = makeSandbox();
  const rows = browsed(s, {
    signin: { host: "eon.pl", covered: "clb clb-container" },
  });
  const box = findByClass(findByClass(rows[0], ".step-signin"), ".step-covered");
  assert(/clb clb-container/.test(box.textContent), box.textContent);
});

check("all of it renders identically live and on replay", () => {
  // L2, over the whole enlarged row: still a pure function of the step.
  const step = {
    frame: "/state/frames/abc.jpg",
    frame_url: "https://eon.pl/x", frame_from: "https://eon.pl/",
    console: ["error: boom"],
    covered: { by: "clb clb-container", dismissed: false },
    problem: "aish could not click 'Zaloguj'",
    unchanged: true,
    signin: {
      host: "eon.pl",
      ok: false,
      frame: "/state/frames/login.jpg",
      covered: "clb clb-container",
      console: ["error: grecaptcha failed to load"],
    },
  };
  const live = makeSandbox();
  const cold = makeSandbox();
  cold.replaying = true;
  const a = live.traceFrame(step);
  const b = cold.traceFrame(step);
  const shape = (node) => [
    node.className,
    node.textContent,
    node.src || "",
    (node.children || []).map(shape),
  ];
  assert.deepEqual(shape(a), shape(b));
});

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("trace frame row: all checks passed");
