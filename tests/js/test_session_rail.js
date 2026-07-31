// Node-only, dependency-free check for the session rail's opening gesture and
// its drag choreography (the #169 working-set deck / swipe pager replacement).
//
// Two things are pinned, and they fail in different ways:
//
//  1. railSwipeOpens — the pure decision. The gesture is the WHOLE transcript,
//     not a left edge: this app is navigated between chats constantly and a
//     24px strip is a fiddly one-handed target. What made an edge tempting —
//     transcript children that scroll sideways — is handled in the wiring by
//     standing down when the pan starts inside one, which is what the old swipe
//     pager did on this same surface.
//
//  2. begin/drag/end — the choreography. A finger-driven panel writes an inline
//     transform, and the stylesheet writes the resting one. If a drag can end
//     without handing the panel back, it is left sitting somewhere no CSS class
//     accounts for: half open, with the chat behind it unreachable and no
//     gesture that recovers it. That is the same shape as the pager's parked
//     transcript, which is exactly the failure class this redesign removes — so
//     the one place it could come back gets a test.
//
// Runs the REAL functions, extracted by marker.
//
// Run manually: node tests/js/test_session_rail.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

let passed = 0;
function ok(what, cond) {
  assert(cond, what);
  passed++;
}

// ---- 1. the decision ------------------------------------------------------
const decide = (() => {
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(extract("// [RAILSWIPE-START]", "// [RAILSWIPE-END]"), sandbox);
  return sandbox;
})();

const drag = (over) => Object.assign(
  { dx: 200, dy: 5, width: 400, ms: 600, keyboardUp: false }, over);

ok("a long rightward drag opens the rail, wherever it started",
  decide.railSwipeOpens(drag({})) === true);
ok("a short slow drag does not: it is a tap or a stray touch",
  decide.railSwipeOpens(drag({ dx: 30 })) === false);
ok("…but a short FAST one does — a flick is a deliberate gesture",
  decide.railSwipeOpens(drag({ dx: 60, ms: 120 })) === true);
ok("…and the same short drag taken slowly does not",
  decide.railSwipeOpens(drag({ dx: 60, ms: 900 })) === false);
ok("a LEFTWARD drag opens nothing — there is nothing to the left of this view",
  decide.railSwipeOpens(drag({ dx: -200 })) === false);
ok("a vertical-dominant drag is a scroll, not an open",
  decide.railSwipeOpens(drag({ dx: 120, dy: 400 })) === false);
ok("nothing opens while the keyboard is up — the composer owns gestures then",
  decide.railSwipeOpens(drag({ keyboardUp: true })) === false);
ok("the commit distance scales with the screen, not a fixed pixel count",
  decide.railSwipeOpens(drag({ dx: 150, width: 400 })) === true
  && decide.railSwipeOpens(drag({ dx: 150, width: 2000, ms: 900 })) === false);

// ---- 2. the choreography --------------------------------------------------
// A minimal DOM: what matters is which classes are set and whether an inline
// transform survives the gesture.
function railWorld({ width = 420 } = {}) { // phone by default
  const classes = new Set();
  const listeners = {};
  const rail = {
    style: {}, offsetWidth: 320,
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
  };
  const scrim = { style: {} };
  const search = { value: "", focus() {}, closest: () => null };
  const calls = [];
  const sandbox = {
    innerWidth: width,
    document: {
      body: {
        classList: {
          add: (c) => classes.add(c),
          remove: (...cs) => cs.forEach((c) => classes.delete(c)),
          contains: (c) => classes.has(c),
        },
      },
      activeElement: null,
      querySelectorAll: () => [],
    },
    $: (id) => ({
      "session-rail": rail,
      "rail-scrim": scrim,
      "sessions-search": search,
      backdrop: { hidden: true },
    }[id]),
    FINE_POINTER: false,
    requestAnimationFrame() {},
    setTimeout() {},
    requestSessions: (q) => calls.push(`requestSessions:${q}`),
    snapViewportSoon: () => calls.push("snapViewportSoon"),
    reportViewport() {},
    addEventListener() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(extract("// [RAIL-START]", "// [RAIL-END]"), sandbox);
  return { s: sandbox, classes, rail, scrim, calls, listeners };
}

// A drag that is released past the threshold leaves the rail OPEN and, crucially,
// under the stylesheet's control — no inline transform left behind.
{
  const w = railWorld();
  w.s.beginRailDrag();
  ok("a drag makes the rail visible immediately", w.classes.has("rail-dragging"));
  ok("…and asks for content, so it is never dragged in empty",
    w.calls.some((c) => c.startsWith("requestSessions")));
  w.s.dragRailTo(0.5);
  ok("the panel follows the finger", w.rail.style.transform === "translateX(-160px)");
  ok("…and so does the scrim", w.scrim.style.opacity === "0.5");

  w.s.endRailDrag(true);
  ok("released open, the rail is open", w.classes.has("rail-open"));
  ok("…the drag state is gone", !w.classes.has("rail-dragging"));
  ok("…and the inline transform is handed back to CSS",
    w.rail.style.transform === "" && w.scrim.style.opacity === "");
}

// The other release, and the one that matters: abandoned. Every failure path
// must end at a resting state the stylesheet knows.
{
  const w = railWorld();
  w.s.beginRailDrag();
  w.s.dragRailTo(0.9);
  w.s.endRailDrag(false);
  ok("released short, the rail is closed", !w.classes.has("rail-open"));
  ok("…with no drag state and no inline transform stranding it half open",
    !w.classes.has("rail-dragging")
    && w.rail.style.transform === "" && w.scrim.style.opacity === "");
}

// A drag that never began cannot move the panel: dragRailTo is guarded, so a
// stray touchmove after a cancelled gesture writes nothing.
{
  const w = railWorld();
  w.s.dragRailTo(0.5);
  ok("a drag that never began moves nothing", w.rail.style.transform === undefined);
}

// ---- 3. docked ------------------------------------------------------------
// At a docked width the rail is not a mode, so nothing may close it — a close
// that "worked" there would leave the app inset for a panel that isn't shown.
{
  const w = railWorld({ width: 1400 });
  ok("a wide viewport docks the rail", w.s.railDocked() === true);
  w.s.openSessionRail("");
  w.s.closeSessionRail();
  ok("closing a docked rail does nothing", w.classes.has("rail-open"));

  const narrow = railWorld({ width: 420 });
  ok("a phone does not dock", narrow.s.railDocked() === false);
  narrow.s.openSessionRail("");
  narrow.s.closeSessionRail();
  ok("…and there it really closes", !narrow.classes.has("rail-open"));
}

// ---- 3b. leftward on the panel closes it ---------------------------------
// The inverse of the gesture that opened it, on the surface it opened onto.
// This is only available because rows gave up their horizontal gesture: while
// they carried swipe-to-delete, this very drag deleted a chat — the destructive
// reading of an ambiguous gesture, which is the wrong way round.
{
  const w = railWorld();
  ok("the panel listens for its own close gesture",
    (w.listeners.touchstart || []).length === 1
    && (w.listeners.touchmove || []).length === 1
    && (w.listeners.touchend || []).length === 1);
}

// ---- 4. the wiring's guards ----------------------------------------------
// These are the checks that make a transcript-wide gesture safe, plus the one
// that only a real browser caught: the wiring once armed on `editingNow()` —
// "an editable has focus" — which the composer satisfies permanently on a
// desktop, so the gesture never armed there at all. `kb-open` is the class that
// means the keyboard is physically covering the screen.
{
  const wiring = extract("const railSwipe = {", "// ---- a chat that no longer exists");
  ok("the gesture stands down on the keyboard, not on focus",
    wiring.includes('classList.contains("kb-open")') && !wiring.includes("editingNow"));
  // The whole reason a transcript-wide gesture is safe. Without this the swipe
  // would fight every unwrapped command output and code block on screen.
  ok("…and inside a horizontally scrolling child, which owns its own pan",
    wiring.includes("scrollsHorizontally(event.target)"));
  ok("…and while text is selected", wiring.includes("selectionActive()"));
  ok("the listener stays PASSIVE (a blocking touchmove on the scroller is the"
    + " phone's biggest jank source)",
    !wiring.includes("preventDefault()") && wiring.includes("{ passive: true }"));
}

console.log(`test_session_rail.js: ${passed} ok — all checks passed`);
