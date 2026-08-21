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
function railWorld({ width = 420, stored = {} } = {}) { // phone by default
  const classes = new Set();
  const listeners = {};
  const rail = {
    style: {}, offsetWidth: 320,
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
  };
  const scrim = { style: {} };
  const search = { value: "", focus() {}, closest: () => null };
  const calls = [];
  const resizeHandlers = [];
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
      querySelectorAll: (sel) => { if (sel === ".sheet") calls.push("hid-sheets"); return []; },
    },
    $: (id) => ({
      "session-rail": rail,
      "rail-scrim": scrim,
      "sessions-search": search,
      backdrop: { hidden: true },
    }[id]),
    FINE_POINTER: false,
    localStorage: {
      getItem: (k) => (k in stored ? stored[k] : null),
      setItem: (k, v) => { stored[k] = String(v); },
      removeItem: (k) => { delete stored[k]; },
    },
    requestAnimationFrame() {},
    setTimeout() {},
    requestSessions: (q) => calls.push(`requestSessions:${q}`),
    snapViewportSoon: () => calls.push("snapViewportSoon"),
    reportViewport() {},
    addEventListener: (type, fn) => { if (type === "resize") resizeHandlers.push(fn); },
    dispatchResize: () => resizeHandlers.forEach((fn) => fn()),
  };
  vm.createContext(sandbox);
  vm.runInContext(extract("// [RAIL-START]", "// [RAIL-END]"), sandbox);
  return { s: sandbox, classes, rail, scrim, calls, listeners, stored };
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
// Docked, the rail is not a slide-over, so the slide-over's DISMISSES must not
// touch it: the scrim, Escape, picking a chat and starting a new one all route
// to closeSessionRail, and a sidebar that vanished every time you opened a chat
// would be unusable. A close that "worked" there would also leave the app inset
// for a panel that isn't shown.
{
  const w = railWorld({ width: 1400 });
  ok("a wide viewport docks the rail", w.s.railDocked() === true);
  w.s.openSessionRail("");
  w.s.closeSessionRail();
  ok("dismissing a docked rail does nothing", w.classes.has("rail-open"));

  const narrow = railWorld({ width: 420 });
  ok("a phone does not dock", narrow.s.railDocked() === false);
  narrow.s.openSessionRail("");
  narrow.s.closeSessionRail();
  ok("…and there it really closes", !narrow.classes.has("rail-open"));
}

// ---- 3a. …but the owner can still put the sidebar away --------------------
// The bug this replaces: docked was a pure VIEWPORT fact, and every control that
// could hide the list went through closeSessionRail, which refuses when docked.
// So on the only screens wide enough to dock, the chats button and ⌘O were dead
// — there was no way to give the chat the whole window.
{
  const w = railWorld({ width: 1400 });
  ok("a docked rail starts open", w.classes.has("rail-open"));

  w.s.toggleSessionRail();
  ok("the toggle puts a docked sidebar away", !w.classes.has("rail-open"));
  ok("…and remembers it, because it is a fact about this screen",
    w.stored["aish-rail-hidden"] === "1");
  ok("…while an ordinary dismiss still cannot: only the toggle writes it",
    (w.s.closeSessionRail(), w.stored["aish-rail-hidden"] === "1"));

  w.s.toggleSessionRail();
  ok("the toggle brings it back", w.classes.has("rail-open"));
  ok("…and forgets the preference, so it is not re-hidden next load",
    !("aish-rail-hidden" in w.stored));
  ok("…filled, never brought back empty",
    w.calls.some((c) => c.startsWith("requestSessions")));
}

// A sidebar filed away stays away across a reload and across a window resize.
// Both are how the decision gets silently undone: the class is set at module
// load, and the resize handler used to force it open on any width change.
{
  const cold = railWorld({ width: 1400, stored: { "aish-rail-hidden": "1" } });
  ok("a filed-away sidebar does not come back on load",
    !cold.classes.has("rail-open"));

  const resized = railWorld({ width: 1400, stored: { "aish-rail-hidden": "1" } });
  resized.s.dispatchResize();
  ok("…nor when the window is resized", !resized.classes.has("rail-open"));

  const shown = railWorld({ width: 1400 });
  shown.s.toggleSessionRail();      // put away
  shown.s.dispatchResize();
  ok("…nor when it was put away in THIS session", !shown.classes.has("rail-open"));
}

// An explicit open — /resume, or the ⌘O that means "show me the chats" — is the
// owner asking for the list, so it unfiles it. Otherwise the slash command would
// silently do nothing on a screen where the sidebar happened to be away.
{
  const w = railWorld({ width: 1400, stored: { "aish-rail-hidden": "1" } });
  w.s.openSessionRail("invoice");
  ok("an explicit open brings a filed-away sidebar back",
    w.classes.has("rail-open") && !("aish-rail-hidden" in w.stored));
}

// Docked, the rail overlaps nothing — the sheets are inset beside it — so
// showing it must not tear down whatever else is on screen. On a phone it
// covers them, and there it still stands them down.
{
  const wide = railWorld({ width: 1400 });
  wide.s.openSessionRail("");
  ok("a docked rail closes no sheets: it sits beside them",
    !wide.calls.includes("hid-sheets"));

  const phone = railWorld({ width: 420 });
  phone.s.openSessionRail("");
  ok("…a slide-over still does, because it covers them",
    phone.calls.includes("hid-sheets"));
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
