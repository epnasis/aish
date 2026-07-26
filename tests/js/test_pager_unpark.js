// Node-only, dependency-free regression check: a committed swipe must never
// leave the transcript parked off-screen.
//
// The bug: commitPage slides the current page away with a transform and waits
// for the landing replay to bring the next page in. onReplay cleared that
// transform inside a requestAnimationFrame — and rAF does not fire while the
// page is hidden. Lock the phone or switch apps between the commit and the
// replay and the callback never runs, so you return to a chat whose content has
// slid entirely off screen. It reads as "the app cleared my conversation",
// especially alongside the red dot a dropped socket puts in the header.
//
// Two properties are pinned here, both about the same failure:
//   1. the landing reset does NOT depend on rAF (forced reflow instead), and
//   2. if the socket closes while a swipe is still parked, the transcript is
//      un-parked — the replay that would have restored it is never coming.
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

// ---- 1. The real unparkTranscript, run against a fake element -------------
function unparkWorld(computed) {
  const el = { style: { transform: "translateX(-1100px)", transition: "none" }, offsetWidth: 1100 };
  const sandbox = { messagesEl: el, getComputedStyle: () => ({ transform: computed }) };
  vm.createContext(sandbox);
  vm.runInContext(extract("// [UNPARK-START]", "// [UNPARK-END]"), sandbox);
  return { el, sandbox };
}
{
  const { el, sandbox } = unparkWorld("none");
  sandbox.unparkTranscript();
  ok("unparkTranscript clears the transform", el.style.transform === "");
  ok("unparkTranscript does not animate the recovery", el.style.transition === "none");
}

// transcriptDisplaced must read the COMPUTED transform, because the whole bug is
// that the inline style reads empty while the element is still displaced.
{
  ok("displaced: a real translate counts",
    unparkWorld("matrix(1, 0, 0, 1, -1100, 0)").sandbox.transcriptDisplaced() === true);
  ok("not displaced: transform none",
    unparkWorld("none").sandbox.transcriptDisplaced() === false);
  ok("not displaced: the identity matrix is at rest",
    unparkWorld("matrix(1, 0, 0, 1, 0, 0)").sandbox.transcriptDisplaced() === false);
}

// ---- 2. The landing reset must not need rAF ------------------------------
// Reproduce the hidden-page condition exactly: requestAnimationFrame exists but
// its callback is NEVER invoked, which is what a hidden page does.
{
  const replayBody = extract("function onReplay(event) {", "  messagesEl.replaceChildren();");
  // Strip line comments first — this function's own WHY comment names rAF.
  const body = replayBody.replace(/^\s*\/\/.*$/gm, "");
  assert(
    !/requestAnimationFrame\s*\(/.test(body),
    "onReplay's swipe-landing reset must not depend on requestAnimationFrame — " +
    "it does not fire while the page is hidden, which is exactly when a phone " +
    "swipe gets interrupted",
  );
  checks += 1;

  // And prove it behaves: run the landing branch with rAF stubbed to a no-op.
  const el = {
    style: { transform: "", transition: "" },
    _reflows: 0,
    get offsetWidth() { this._reflows += 1; return 1100; },
    clientWidth: 1100,
  };
  const sandbox = {
    messagesEl: el,
    swipeInFrom: -1,
    // A HIDDEN page: it runs neither rAF nor CSS transitions, so the recovery
    // must land outright rather than animate to zero.
    document: { visibilityState: "hidden" },
    unparkTranscript() { el.style.transition = "none"; el.style.transform = ""; },
    requestAnimationFrame() { throw new Error("rAF must not be used here"); },
  };
  vm.createContext(sandbox);
  // The landing branch, lifted verbatim from onReplay's own body (not by a
  // whole-file search — ws.onclose contains a similar-looking guard).
  const from = replayBody.indexOf("if (swipeInFrom) {");
  const to = replayBody.indexOf("\n  } else {", from);
  assert(from !== -1 && to !== -1, "could not locate onReplay's swipe-landing branch");
  vm.runInContext(`${replayBody.slice(from, to)} }`, sandbox);
  ok("hidden landing: transform ends cleared", el.style.transform === "");
  ok("hidden landing: NOT animated — a stalled transition is the bug",
    el.style.transition === "none");
  ok("hidden landing consumes swipeInFrom", sandbox.swipeInFrom === 0);
}

// ---- 2b. Visible: it should animate, via a forced reflow, still no rAF ----
{
  const el = {
    style: { transform: "", transition: "" },
    _reflows: 0,
    get offsetWidth() { this._reflows += 1; return 1100; },
    clientWidth: 1100,
  };
  const sandbox = {
    messagesEl: el,
    swipeInFrom: -1,
    document: { visibilityState: "visible" },
    unparkTranscript() { throw new Error("visible landing should animate, not snap"); },
    requestAnimationFrame() { throw new Error("rAF must not be used here"); },
  };
  vm.createContext(sandbox);
  const replayBody2 = extract("function onReplay(event) {", "  messagesEl.replaceChildren();");
  const f = replayBody2.indexOf("if (swipeInFrom) {");
  const t = replayBody2.indexOf("\n  } else {", f);
  vm.runInContext(`${replayBody2.slice(f, t)} }`, sandbox);
  ok("visible landing ends cleared", el.style.transform === "");
  ok("visible landing animates", el.style.transition === "transform 0.18s ease-out");
  ok("visible landing forces a reflow so the animation has a start point", el._reflows >= 1);
}

// ---- 3. A socket close during a parked swipe un-parks --------------------
// The guard is one line in ws.onclose; assert it is present and correctly
// ordered (before the early returns for the deliberate-close codes, so even a
// 4000/4403 close recovers the view).
{
  const onclose = src.slice(src.indexOf("ws.onclose = (event) => {"), src.indexOf("ws.onclose = null"));
  const guard = onclose.indexOf("unparkTranscript()");
  ok("ws.onclose un-parks a pending swipe", guard !== -1);
  ok("the un-park runs before the 4000 detach early-return",
    guard < onclose.indexOf("event.code === 4000"));
  ok("the un-park clears swipeInFrom too",
    /swipeInFrom\s*=\s*0;\s*unparkTranscript\(\)/.test(onclose));
}

console.log(`${checks} ok — all checks passed`);
