// Node-only, dependency-free checks for the ONE invariant the swipe pager must
// never break: **an idle transcript is on screen.**
//
// A committed swipe parks the transcript off-screen on the promise that a landing
// replay brings the next page in. Three separate defects shipped because that
// promise was maintained imperatively by whichever callback happened to fire:
//   - the reset lived in a requestAnimationFrame, which a hidden page never runs;
//   - clearing the inline transform only STARTS a transition, which a hidden page
//     also never runs — so the element sat at the animation's start value while
//     `style.transform` already read empty (the DOM lied);
//   - and a zombie socket (readyState OPEN, dead underneath) accepts the send and
//     answers nothing, so no close event ever arrived to trigger recovery and the
//     park had no deadline at all.
//
// So this tests the OWNER (`reconcilePager`) rather than any one mechanism, and it
// tests the invariant as a property over event sequences. The design rule being
// pinned: every failure path degrades to "no animation", never to "no content".
//
// Run manually: node tests/js/test_pager_unpark.js
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

let checks = 0;
function ok(label, cond) { assert(cond, label); checks += 1; }

// A HOSTILE fake element+environment by default: transitions never settle and
// rAF never fires, which is precisely the phone-locked condition. `computed` is
// tracked separately from the inline style so the test can see the gap that made
// defect 5 invisible — writing `style.transform = ""` does NOT move the element.
function world({ visible = false } = {}) {
  const el = {
    style: { _t: "", _tr: "",
      get transform() { return this._t; },
      set transform(v) {
        this._t = v;
        // No transition declared, or explicitly none => the change lands at once.
        // WITH a transition it only STARTS one, and in hostile mode (a hidden
        // page) it never advances — so the computed value keeps the old position
        // while the inline style already reads the new one. That gap is defect 5.
        if (!this._tr || this._tr === "none") el._computed = v || "none";
      },
      get transition() { return this._tr; },
      set transition(v) { this._tr = v; } },
    _computed: "none",
    _reflows: 0,
    get offsetWidth() { this._reflows += 1; return 1100; },
    clientWidth: 1100,
  };
  const timers = [];
  const rafs = []; // rAF callbacks — hostile mode never fires them
  const sandbox = {
    messagesEl: el,
    swipeInFrom: 0,
    document: { visibilityState: visible ? "visible" : "hidden" },
    getComputedStyle: () => ({ transform: el._computed }),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (id) timers[id - 1] = null; },
    requestAnimationFrame: (fn) => { rafs.push(fn); return rafs.length; },
  };
  vm.createContext(sandbox);
  vm.runInContext(extract("// [UNPARK-START]", "// [UNPARK-END]"), sandbox);
  // Settling a transition is what a VISIBLE page does; hostile mode never calls it.
  sandbox._settle = () => { el._computed = el.style.transform || "none"; };
  sandbox._timers = timers;
  sandbox._rafs = rafs;
  sandbox._el = el;
  return sandbox;
}

const AT_REST = (w) => !w.transcriptDisplaced();

// ---- the invariant, over every sequence that ends idle --------------------
// After any sequence leaving no swipe in flight, the transcript must be at rest.

// 1. commit → landing replay, on a HIDDEN page (defects 4 and 5 together).
{
  const w = world({ visible: false });
  w.parkTranscript(-1, 1100);
  ok("parked: off screen while in flight", w.transcriptDisplaced());
  w.reconcilePager(-1);                       // the landing
  ok("hidden landing ends at rest — no animation to stall on", AT_REST(w));
  ok("hidden landing clears swipeInFrom", w.swipeInFrom === 0);
}

// 2. commit → landing replay, VISIBLE: the landing is STAGED static at the
//    entry side first (the caller builds content and sets scrollTop while
//    NOTHING transitions — moving scrollTop mid-transition left iOS painting
//    blank tiles), the slide starts on the next frame, and a settle timer
//    owns the resting state whatever became of the animation.
{
  const w = world({ visible: true });
  w.parkTranscript(1, 1100);
  w.reconcilePager(1);
  ok("landing stages static at the entry side — no transition running",
    w._el.style.transition === "none" && w.transcriptDisplaced());
  ok("the slide is deferred to the next frame", w._rafs.length === 1);
  w._rafs[0]();
  ok("the next frame starts the slide", w._el.style.transition === "transform 0.18s ease-out");
  w._settle();
  ok("visible landing ends at rest once settled", AT_REST(w));
  // The settle timer is the guarantee the animation is not: firing it after a
  // clean slide is a no-op that leaves the transcript at rest.
  const settle = w._timers.filter(Boolean).find((t) => t.ms <= 500);
  ok("a landing arms a settle", Boolean(settle));
  settle.fn();
  ok("settle after a clean slide keeps the rest position", AT_REST(w));
}

// 2b. The slide's frame NEVER fires (page hidden mid-landing): the settle
//     alone must still put the staged transcript back on screen.
{
  const w = world({ visible: true });
  w.parkTranscript(1, 1100);
  w.reconcilePager(1);
  ok("staged off screen while waiting on a frame", w.transcriptDisplaced());
  const settle = w._timers.filter(Boolean).find((t) => t.ms <= 500);
  settle.fn(); // the rAF never ran — only the settle can recover
  ok("settle recovers a landing whose frame never fired", AT_REST(w));
}

// 2c. A NEWER committed swipe owns the transform: a stale settle stands down.
{
  const w = world({ visible: true });
  w.parkTranscript(1, 1100);
  w.reconcilePager(1);
  const settle = w._timers.filter(Boolean).find((t) => t.ms <= 500);
  w.parkTranscript(-1, 1100); // user swiped again before the settle fired
  settle.fn();
  ok("a stale settle never unparks a newly-committed swipe", w.transcriptDisplaced());
}

// 3. commit → socket close: no landing is coming, recover now.
{
  const w = world({ visible: false });
  w.parkTranscript(-1, 1100);
  w.reconcilePager();                          // what ws.onclose calls
  ok("socket close ends at rest", AT_REST(w));
  ok("socket close clears swipeInFrom", w.swipeInFrom === 0);
}

// 4. THE ZOMBIE SOCKET — the failure no event announces. Nothing at all happens
//    after the commit; only the deadline can save the view.
{
  const w = world({ visible: false });
  w.parkTranscript(-1, 1100);
  const armed = w._timers.filter(Boolean);
  ok("parking arms a deadline", armed.length === 1);
  ok("the deadline is bounded and not absurd", armed[0].ms > 0 && armed[0].ms <= 10000);
  armed[0].fn();                               // the watchdog fires
  ok("the deadline recovers a chat nothing else would have", AT_REST(w));
  ok("the deadline clears swipeInFrom", w.swipeInFrom === 0);
}

// 5. A landing that DOES arrive must disarm the deadline, or a later firing
//    would yank a legitimately animating page. (The short settle it arms
//    instead is not a deadline — firing it after a clean slide is a no-op.)
{
  const w = world({ visible: true });
  w.parkTranscript(-1, 1100);
  w.reconcilePager(-1);
  ok("a landing disarms the deadline",
    w._timers.filter(Boolean).every((t) => t.ms <= 500));
}

// 6. Re-parking replaces the old deadline rather than stacking them.
{
  const w = world({ visible: false });
  w.parkTranscript(-1, 1100);
  w.parkTranscript(1, 1100);
  ok("re-parking leaves exactly one deadline armed",
    w._timers.filter(Boolean).length === 1);
}

// ---- transcriptDisplaced reads the COMPUTED value ------------------------
// The whole of defect 5 was the inline style reading empty while the element was
// still displaced, so this reader must never consult style.transform.
{
  const w = world();
  w._el._computed = "matrix(1, 0, 0, 1, -1100, 0)";
  w._el.style._t = "";                          // inline says "at rest" — it lies
  ok("displaced: a real translate counts, even with an empty inline style",
    w.transcriptDisplaced() === true);
  w._el._computed = "matrix(1, 0, 0, 1, 0, 0)";
  ok("not displaced: the identity matrix is at rest", w.transcriptDisplaced() === false);
  w._el._computed = "none";
  ok("not displaced: transform none", w.transcriptDisplaced() === false);
}

// ---- the owner really is the only owner ----------------------------------
// If a second place starts writing swipeInFrom or the resting transform, the
// invariant is no longer enforceable and these tests stop meaning anything.
{
  const assignments = (src.match(/swipeInFrom = /g) || []).length;
  ok(`swipeInFrom has one declaration + two writers, both in the owner (found ${assignments})`,
    assignments === 3);
  const owner = extract("// [UNPARK-START]", "// [UNPARK-END]");
  ok("both swipeInFrom writers live inside the owner block",
    (owner.match(/swipeInFrom = /g) || []).length === 2);
  // onReplay, ws.onclose and commitPage must delegate, not hand-roll a reset.
  for (const [label, fn] of [
    ["onReplay", extract("function onReplay(event) {", "  messagesEl.replaceChildren();")],
    ["commitPage", extract("function commitPage(direction, target, width) {", "\nfunction snapBack")],
  ]) {
    ok(`${label} delegates to the owner`, /reconcilePager\(|parkTranscript\(/.test(fn));
    ok(`${label} does not hand-roll the resting transform`,
      !/messagesEl\.style\.transform = ""/.test(fn));
  }
}

console.log(`${checks} ok — all checks passed`);
