// Node-only, dependency-free regression check for the swipe-up wiring bug:
// an up-swipe on the Sessions view's bottom search bar dismissed the view and
// then IMMEDIATELY reopened it, so it appeared never to close at all.
//
// Why a wiring test and not a seam test: `swipeUpOpensDrawer` (the pure
// predicate) was always correct, and its unit test passed throughout. The bug
// lived in WHEN the window-level listener read the overlay state. That listener
// is on the window, so it runs last — after the search bar's own touchend has
// already called closeSheets(). Reading `sessions-sheet.hidden` at touchend
// therefore saw the sheet the previous handler had just hidden, concluded no
// overlay owned the screen, and reopened it. The fix decides eligibility at
// touchSTART, which is the state the user's intent was formed against.
//
// Desktop hid this: openSessionsSheet auto-focuses the search field only on
// FINE_POINTER, so `keyboardUp` short-circuited the reopen there. On a phone
// nothing takes focus, so it fired every time — which is why it reproduced only
// on the device.
//
// Run manually: node tests/js/test_swipe_up_wiring.js
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

// Build a sandbox holding the REAL predicate plus the REAL window wiring.
function makeWorld({ sheetHiddenAtStart, keyboardUp = false, consoleOpen = false }) {
  const state = {
    sheetHidden: sheetHiddenAtStart,
    backdropHidden: true,
    opened: 0,
  };
  const handlers = {};
  const sandbox = {
    consoleOpen,
    innerWidth: 390,
    innerHeight: 844,
    editingNow: () => keyboardUp,
    openSessionsSheet() { state.opened += 1; state.sheetHidden = false; },
    $: (id) => {
      if (id === "sessions-sheet") return { get hidden() { return state.sheetHidden; } };
      if (id === "backdrop") return { get hidden() { return state.backdropHidden; } };
      throw new Error(`unexpected $(${id})`);
    },
    addEventListener(type, fn) { handlers[type] = fn; },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  // The predicate the wiring calls, then the wiring itself — both real.
  vm.runInContext(extract("// [SWIPEUP-START]", "// [SWIPEUP-END]"), sandbox);
  vm.runInContext(extract("// [SWIPEUPWIRE-START]", "// [SWIPEUPWIRE-END]"), sandbox);
  assert(handlers.touchstart && handlers.touchend, "wiring did not register both listeners");
  return { state, handlers };
}

// A committed up-swipe from the bottom zone: starts 30px above the bottom edge,
// travels 90px up (> SWIPE_UP_MIN), no horizontal drift.
function swipeUp({ handlers }, { dismissMidGesture = null } = {}) {
  const startY = 844 - 30;
  handlers.touchstart({ touches: [{ clientX: 200, clientY: startY }] });
  // Whatever an element-level touchend already did lands here — the window
  // listener always runs after it.
  if (dismissMidGesture) dismissMidGesture();
  handlers.touchend({ changedTouches: [{ clientX: 200, clientY: startY - 90 }] });
}

let checks = 0;
function ok(label, cond) { assert(cond, label); checks += 1; }

// 1. THE REGRESSION. Gesture starts while the Sessions view is OPEN; the search
//    bar's own touchend dismisses it mid-gesture. The window listener must NOT
//    treat the now-hidden sheet as "no overlay" and reopen it.
{
  const w = makeWorld({ sheetHiddenAtStart: false });
  swipeUp(w, { dismissMidGesture: () => { w.state.sheetHidden = true; } });
  ok("dismiss-then-reopen: sheet stays closed", w.state.sheetHidden === true);
  ok("dismiss-then-reopen: never reopened", w.state.opened === 0);
}

// 2. The feature still works: from a chat with nothing open, an up-swipe from
//    the bottom zone opens the Sessions view.
{
  const w = makeWorld({ sheetHiddenAtStart: true });
  swipeUp(w);
  ok("plain up-swipe opens the drawer", w.state.opened === 1);
}

// 3. Sheet open and NOT dismissed: still must not reopen/stack.
{
  const w = makeWorld({ sheetHiddenAtStart: false });
  swipeUp(w);
  ok("sheet already open: no reopen", w.state.opened === 0);
}

// 4. The console owns the screen at gesture start.
{
  const w = makeWorld({ sheetHiddenAtStart: true, consoleOpen: true });
  swipeUp(w);
  ok("console open: no drawer", w.state.opened === 0);
}

// 5. Keyboard up: the composer owns vertical gestures.
{
  const w = makeWorld({ sheetHiddenAtStart: true, keyboardUp: true });
  swipeUp(w);
  ok("keyboard up: no drawer", w.state.opened === 0);
}

// 6. A short flick from the bottom zone is a tap/micro-scroll, not a swipe.
{
  const w = makeWorld({ sheetHiddenAtStart: true });
  const startY = 844 - 30;
  w.handlers.touchstart({ touches: [{ clientX: 200, clientY: startY }] });
  w.handlers.touchend({ changedTouches: [{ clientX: 200, clientY: startY - 20 }] });
  ok("short flick: no drawer", w.state.opened === 0);
}

// 7. A long up-swipe that starts high on the screen is a scroll, not the
//    bottom-zone gesture.
{
  const w = makeWorld({ sheetHiddenAtStart: true });
  w.handlers.touchstart({ touches: [{ clientX: 200, clientY: 300 }] });
  w.handlers.touchend({ changedTouches: [{ clientX: 200, clientY: 180 }] });
  ok("swipe from mid-screen: no drawer", w.state.opened === 0);
}

// 8. Multi-touch is never this gesture (pinch-zoom).
{
  const w = makeWorld({ sheetHiddenAtStart: true });
  const startY = 844 - 30;
  w.handlers.touchstart({ touches: [{ clientX: 200, clientY: startY }, { clientX: 100, clientY: startY }] });
  w.handlers.touchend({ changedTouches: [{ clientX: 200, clientY: startY - 90 }] });
  ok("two fingers: no drawer", w.state.opened === 0);
}

console.log(`${checks} ok — all checks passed`);
