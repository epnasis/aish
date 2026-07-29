// Node-only, dependency-free regression check for transcript text selection
// while the composer is focused (the "cannot copy part of an answer while
// composing" bug).
//
// Two iOS facts drive the design under test. (1) WebKit will not establish a
// document selection while an editable element holds focus — the console's
// select mode blurs its textarea for exactly this reason — so a long-press on
// an answer while composing could never select. (2) The blur that fixes (1)
// dismisses the keyboard, whose visualViewport events used to reflow
// (syncKeyboardInset), window-scroll (snapViewportHome) and auto-scroll
// (scrollToEnd) the transcript UNDER the still-held finger, cancelling the
// long-press anyway. So [TOUCH-FREEZE] blurs at transcript touchstart and
// freezes all viewport settling until the finger lifts; endSwipe then runs
// the deferred settle (without scrollToEnd — it would scroll the just-made
// selection out of view).
//
// (3) Freezing is not enough: the dismissal makes iOS animate
// visualViewport.offsetTop back to 0, and a fixed body still pinned at the
// old offset slides DOWN the screen by exactly that much — losing the held
// text anyway. While frozen, trackViewportPan rides the pan (body top
// follows offsetTop; nothing reflows or scrolls).
//
// Run manually: node tests/js/test_transcript_select_guard.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

const freezeCode = extract("// [TOUCH-FREEZE-START]", "// [TOUCH-FREEZE-END]");
const settleCode = extract("// [VV-SETTLE-START]", "// [VV-SETTLE-END]");

// The real endSwipe wiring line matters too: the deferred settle must run
// syncKeyboardInset + snapViewportSoon and must NOT run scrollToEnd. Pin the
// shipped wiring textually rather than duplicating a hand-rolled copy.
assert(
  /endTranscriptTouch\(\(\) => \{ syncKeyboardInset\(\); snapViewportSoon\(\); \}\)/.test(src),
  "endSwipe must wire the deferred settle as syncKeyboardInset + snapViewportSoon"
);
const touchstartWiring = src.indexOf("beginTranscriptTouch(document.activeElement, event.target)");
assert(touchstartWiring !== -1, "transcript touchstart must call beginTranscriptTouch");

function makeWorld({ selection = false, kbOpen = true, offsetTop = 0 } = {}) {
  const calls = { sync: 0, snapHome: 0, scrollEnd: 0, report: 0 };
  const vvHandlers = {};
  const bodyClasses = new Set(kbOpen ? ["kb-open"] : []);
  const sandbox = {
    window: {},
    Date,
    syncKeyboardInset: () => calls.sync++,
    snapViewportHome: () => calls.snapHome++,
    scrollToEnd: () => calls.scrollEnd++,
    snapViewportSoon: () => { calls.sync++; calls.snapHome++; }, // settle proxy
    selectionActive: () => selection,
    reportViewport: () => calls.report++,
    visualViewport: {
      height: 844,
      offsetTop,
      addEventListener: (type, fn) => { vvHandlers[type] = fn; },
    },
    document: {
      body: {
        classList: { contains: (c) => bodyClasses.has(c) },
        style: { top: kbOpen ? `${offsetTop}px` : "" },
      },
    },
  };
  sandbox.window.visualViewport = sandbox.visualViewport;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(freezeCode, sandbox);
  // The settle block reads `lastVvReport` declared just above its marker;
  // provide it in the sandbox.
  vm.runInContext("let lastVvReport = 0;\n" + settleCode, sandbox);
  return { sandbox, calls, vvHandlers };
}

function editableEl(containsTarget) {
  let blurred = 0;
  return {
    el: {
      tagName: "TEXTAREA",
      isContentEditable: false,
      contains: () => containsTarget,
      blur: () => blurred++,
    },
    blurCount: () => blurred,
  };
}

// --- 1. Touching the transcript while the composer is focused blurs it ------
{
  const { sandbox } = makeWorld();
  const active = editableEl(false);
  sandbox.beginTranscriptTouch(active.el, {});
  assert.strictEqual(active.blurCount(), 1, "focused composer must be blurred");
  assert.strictEqual(sandbox.viewportSettleAllowed(), false, "settle frozen during touch");
}

// --- 2. Touching INTO the focused editable (card feedback field) keeps focus
{
  const { sandbox } = makeWorld();
  const active = editableEl(true);
  sandbox.beginTranscriptTouch(active.el, {});
  assert.strictEqual(active.blurCount(), 0, "touch inside the editable must not blur it");
  assert.strictEqual(sandbox.viewportSettleAllowed(), false, "still freezes settling");
}

// --- 3. Non-editable focus: no blur, but the freeze still applies -----------
{
  const { sandbox } = makeWorld();
  sandbox.beginTranscriptTouch({ tagName: "DIV", isContentEditable: false, contains: () => false, blur: () => { throw new Error("must not blur"); } }, {});
  assert.strictEqual(sandbox.viewportSettleAllowed(), false);
}

// --- 4. The interleaving: vv events mid-press must not move anything --------
{
  const { sandbox, calls, vvHandlers } = makeWorld();
  const active = editableEl(false);
  sandbox.beginTranscriptTouch(active.el, {}); // long-press begins, keyboard starts dismissing
  vvHandlers.resize();  // keyboard dismissal animation
  vvHandlers.scroll();
  vvHandlers.resize();
  assert.strictEqual(calls.sync, 0, "no reflow under a live touch");
  assert.strictEqual(calls.snapHome, 0, "no window snap under a live touch");
  assert.strictEqual(calls.scrollEnd, 0, "no auto-scroll under a live touch");
  assert(calls.report > 0, "telemetry keeps flowing while frozen");

  // Finger lifts: endSwipe runs the deferred settle exactly once.
  let settles = 0;
  sandbox.endTranscriptTouch(() => settles++);
  assert.strictEqual(settles, 1, "the suppressed settle runs at touchend");
  sandbox.endTranscriptTouch(() => settles++);
  assert.strictEqual(settles, 1, "a second touchend (touchcancel replay) settles nothing");
  assert.strictEqual(sandbox.viewportSettleAllowed(), true, "settling resumes after the touch");

  // Later vv events settle normally again.
  vvHandlers.resize();
  assert.strictEqual(calls.sync, 1);
  assert.strictEqual(calls.snapHome, 1);
  assert.strictEqual(calls.scrollEnd, 1);
}

// --- 5. An established selection is never yanked to the bottom --------------
{
  const { calls, vvHandlers } = makeWorld({ selection: true });
  vvHandlers.resize(); // e.g. keyboard reopening while text stays selected
  assert.strictEqual(calls.sync, 1, "layout still syncs with a selection present");
  assert.strictEqual(calls.snapHome, 1, "window snap still allowed with a selection");
  assert.strictEqual(calls.scrollEnd, 0, "scrollToEnd must stand down for a selection");
}

// --- 6. Mid-press, the body rides the browser's un-pan -----------------------
// Keyboard was up with the page panned (offsetTop 38, body pinned at 38px).
// The blur makes iOS animate offsetTop back to 0; each vv event while frozen
// must move body.style.top in lockstep — and reflow/scroll nothing.
{
  const { sandbox, calls, vvHandlers } = makeWorld({ offsetTop: 38 });
  const active = editableEl(false);
  sandbox.beginTranscriptTouch(active.el, {});
  for (const top of [20, 7, 0]) {
    sandbox.visualViewport.offsetTop = top;
    vvHandlers.scroll();
    assert.strictEqual(sandbox.document.body.style.top, `${top}px`, "body top must follow the pan");
  }
  assert.strictEqual(calls.sync + calls.snapHome + calls.scrollEnd, 0, "tracking must not reflow or scroll");

  // offsetTop dips negative mid-animation: clamp, same as syncKeyboardInset.
  sandbox.visualViewport.offsetTop = -3;
  vvHandlers.scroll();
  assert.strictEqual(sandbox.document.body.style.top, "0px", "negative offsetTop clamps to 0");
  sandbox.endTranscriptTouch(() => {});
}

// --- 7. No keyboard up (no kb-open pin): frozen events touch nothing ---------
{
  const { sandbox, vvHandlers } = makeWorld({ kbOpen: false, offsetTop: 12 });
  sandbox.beginTranscriptTouch({ tagName: "DIV", isContentEditable: false, contains: () => false }, {});
  vvHandlers.scroll();
  assert.strictEqual(sandbox.document.body.style.top, "", "an unpinned body is never given a top");
  sandbox.endTranscriptTouch(() => {});
}

console.log("transcript select guard: all assertions passed");
