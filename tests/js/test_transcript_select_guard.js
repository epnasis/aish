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

function makeWorld({ selection = false } = {}) {
  const calls = { sync: 0, snapHome: 0, scrollEnd: 0, report: 0 };
  const vvHandlers = {};
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
      addEventListener: (type, fn) => { vvHandlers[type] = fn; },
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

console.log("transcript select guard: all assertions passed");
