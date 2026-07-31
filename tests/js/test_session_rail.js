// Node-only, dependency-free check for the session rail's opening gesture and
// its drag choreography (the #169 working-set deck / swipe pager replacement).
//
// Two things are pinned, and they fail in different ways:
//
//  1. railEdgeOpens — the pure decision. The rail may only be opened by a drag
//     that STARTS at the left edge, because the transcript legitimately contains
//     horizontally scrolling children (code blocks, tables, terminal output) and
//     a full-surface swipe-right would compete with every one of them.
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
  { startX: 4, endX: 90, dy: 5, keyboardUp: false }, over);

ok("a clean edge drag opens the rail", decide.railEdgeOpens(drag({})) === true);
ok("a drag starting mid-screen does not — that surface belongs to the transcript",
  decide.railEdgeOpens(drag({ startX: 120, endX: 260 })) === false);
ok("a short drag does not: it is a tap or a stray touch",
  decide.railEdgeOpens(drag({ endX: 24 })) === false);
ok("a LEFTWARD edge drag does not open anything",
  decide.railEdgeOpens(drag({ endX: -30 })) === false);
ok("a vertical-dominant drag is a scroll, not an open",
  decide.railEdgeOpens(drag({ endX: 60, dy: 200 })) === false);
ok("nothing opens while the keyboard is up — the composer owns gestures then",
  decide.railEdgeOpens(drag({ keyboardUp: true })) === false);
ok("the armed edge really is narrow — a wide one would eat transcript gestures",
  decide.railEdgeOpens(drag({ startX: 40, endX: 200 })) === false);

// ---- 2. the choreography --------------------------------------------------
// A minimal DOM: what matters is which classes are set and whether an inline
// transform survives the gesture.
function railWorld({ width = 420 } = {}) { // phone by default
  const classes = new Set();
  const rail = { style: {}, offsetWidth: 320 };
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
  return { s: sandbox, classes, rail, scrim, calls };
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

// ---- 4. the keyboard guard reads the right signal -------------------------
// The wiring (not the pure decision) once armed on `editingNow()` — "an editable
// has focus" — which on a desktop the composer satisfies permanently, so the
// edge gesture never armed there at all. On a phone the two coincide, which is
// exactly why only driving the real browser found it. `kb-open` is the class
// that means the keyboard is physically covering the screen.
{
  const wiring = extract("(function attachRailEdgeSwipe() {", "// ---- the session rail");
  ok("the edge gesture stands down on the keyboard, not on focus",
    wiring.includes('classList.contains("kb-open")') && !wiring.includes("editingNow"));
}

console.log(`test_session_rail.js: ${passed} ok — all checks passed`);
