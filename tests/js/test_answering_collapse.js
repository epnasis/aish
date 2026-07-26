// Node-only, dependency-free regression check for issue #168 (the blue progress
// timeline collapses when the task enters the "Answering…" phase). Pulls the
// REAL collapseTimelineForAnswering() out of app.js by marker and runs it in an
// isolated vm against a fake trace whose `el.classList` mimics the DOM token
// list — exercising the shipped branching, not a copy.
//
// The trace element carries `live` while the turn runs and `open` while
// expanded; collapsing to the summary is `classList.remove("open")`. Entering
// Answering must collapse an open, live trace exactly once and never touch a
// trace that is already collapsed, no longer live, or absent.
//
// Run manually: node tests/js/test_answering_collapse.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);
const start = src.indexOf("function collapseTimelineForAnswering");
const end = src.indexOf("// [ANSWERING-COLLAPSE-END]");
assert(start !== -1 && end !== -1, "ANSWERING-COLLAPSE markers not found in app.js");

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(src.slice(start, end), sandbox);
assert(
  typeof sandbox.collapseTimelineForAnswering === "function",
  "collapseTimelineForAnswering not extracted"
);

// Minimal DOMTokenList stand-in: a Set behind contains/add/remove.
function fakeTrace(classes) {
  const set = new Set(classes);
  return {
    el: {
      classList: {
        contains: (c) => set.has(c),
        add: (c) => set.add(c),
        remove: (c) => set.delete(c),
      },
    },
    has: (c) => set.has(c),
  };
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

check("open + live trace collapses (removes `open`, keeps `live`)", () => {
  const t = fakeTrace(["trace", "live", "open"]);
  assert.strictEqual(sandbox.collapseTimelineForAnswering(t), true);
  assert.ok(!t.has("open"), "should have collapsed to the summary");
  assert.ok(t.has("live"), "must stay live — the turn hasn't finished");
});

check("already-collapsed live trace is left alone (no-op)", () => {
  const t = fakeTrace(["trace", "live"]);
  assert.strictEqual(sandbox.collapseTimelineForAnswering(t), false);
  assert.ok(!t.has("open"));
});

check("collapse is idempotent — a second call does nothing", () => {
  const t = fakeTrace(["trace", "live", "open"]);
  assert.strictEqual(sandbox.collapseTimelineForAnswering(t), true);
  assert.strictEqual(sandbox.collapseTimelineForAnswering(t), false,
    "second call must not re-fight the user if they re-expanded and this ran again");
});

check("a finished (non-live) but open trace is not touched", () => {
  const t = fakeTrace(["trace", "open"]);
  assert.strictEqual(sandbox.collapseTimelineForAnswering(t), false);
  assert.ok(t.has("open"), "only a LIVE trace auto-collapses on Answering");
});

check("no trace (pure-answer turn, null) is safe", () => {
  assert.strictEqual(sandbox.collapseTimelineForAnswering(null), false);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
