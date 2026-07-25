// Node-only, dependency-free check for the offline mirror's eviction policy
// (#165). The mirror caches every session it can fit, newest first, and when
// the storage budget runs out something has to go. The rules this pins:
//
//   * a PINNED chat is never evicted — that is the whole promise of the
//     "Available offline" toggle, and it must hold even when pinned chats are
//     the oldest things in the store;
//   * the chat on screen is never evicted out from under the reader;
//   * everything else goes least-valuable first, where value is the more recent
//     of "opened on this device" and "last active anywhere" — the second half
//     is what makes a chat started on another device readable here.
//
// Pulls the REAL functions out of app.js by marker.
//
// Run manually: node tests/js/test_offline_evict.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);
const start = src.indexOf("// [OFFLINE-EVICT-START]");
const end = src.indexOf("// [OFFLINE-EVICT-END]");
assert(start !== -1 && end !== -1, "OFFLINE-EVICT markers not found in app.js");

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(src.slice(start, end), sandbox);
const { offlinePlanEviction, offlineEvictionOrder, offlineOverBudget } = sandbox;
assert(typeof offlinePlanEviction === "function", "offlinePlanEviction not extracted");

// deepStrictEqual compares prototypes, and arrays built inside the vm realm
// have a different Array constructor — compare by JSON instead.
const eq = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);

const meta = (name, opts = {}) => ({
  name,
  bytes: opts.bytes ?? 10,
  pinned: Boolean(opts.pinned),
  openedAt: opts.openedAt || 0,
  ts: opts.ts || 0,
});

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures += 1;
    console.error(`FAIL - ${name}\n    ${err.message}`);
  }
}

check("nothing is evicted while under budget", () => {
  const metas = [meta("a", { ts: 1 }), meta("b", { ts: 2 })];
  eq(offlinePlanEviction(metas, "a", 1000, 100), []);
});

check("the least valuable session goes first", () => {
  const metas = [
    meta("old", { ts: 100 }),
    meta("mid", { ts: 200 }),
    meta("new", { ts: 300 }),
  ];
  // Budget fits two of the three (10 bytes each).
  eq(offlinePlanEviction(metas, "", 25, 100), ["old"]);
});

check("opening a chat outranks pure recency", () => {
  // "stale" was last active long ago but was READ on this device just now;
  // "fresh" is newer on the server but has never been opened here.
  const metas = [
    meta("stale", { ts: 1, openedAt: Date.now() }),
    meta("fresh", { ts: Date.now() / 1000 - 3600, openedAt: 0 }),
    meta("filler", { ts: 2 }),
  ];
  eq(offlinePlanEviction(metas, "", 25, 100), ["filler"]);
  // Squeeze harder: "fresh" goes before the one that was actually read.
  eq(offlinePlanEviction(metas, "", 15, 100), ["filler", "fresh"]);
});

check("a pinned session is never evicted, however old", () => {
  const metas = [
    meta("pinned-ancient", { ts: 1, pinned: true }),
    meta("unpinned-new", { ts: 9999 }),
    meta("unpinned-newer", { ts: 99999 }),
  ];
  // A budget so small nothing fits: only the unpinned ones may be given up.
  const evicted = offlinePlanEviction(metas, "", 1, 100);
  assert.ok(!evicted.includes("pinned-ancient"), "pinned chat was evicted");
  eq(evicted, ["unpinned-new", "unpinned-newer"]);
});

check("the session on screen is never evicted", () => {
  const metas = [meta("viewing", { ts: 1 }), meta("other", { ts: 2 })];
  eq(offlinePlanEviction(metas, "viewing", 1, 100), ["other"]);
});

check("the session count is a budget too, not just bytes", () => {
  const metas = [meta("a", { ts: 1 }), meta("b", { ts: 2 }), meta("c", { ts: 3 })];
  // Bytes are fine; the count is one over.
  eq(offlinePlanEviction(metas, "", 1e9, 2), ["a"]);
});

check("eviction stops as soon as it is back under budget", () => {
  const metas = [
    meta("a", { ts: 1 }), meta("b", { ts: 2 }), meta("c", { ts: 3 }), meta("d", { ts: 4 }),
  ];
  eq(offlinePlanEviction(metas, "", 35, 100), ["a"]);
});

check("the eviction order itself excludes pinned and current", () => {
  const metas = [
    meta("pinned", { ts: 1, pinned: true }),
    meta("current", { ts: 2 }),
    meta("free", { ts: 3 }),
  ];
  eq(Array.from(offlineEvictionOrder(metas, "current"), (m) => m.name), ["free"]);
});

check("overBudget reads bytes and count independently", () => {
  const metas = [meta("a", { bytes: 100 })];
  assert.strictEqual(offlineOverBudget(metas, 50, 10), true, "bytes over");
  assert.strictEqual(offlineOverBudget(metas, 500, 0), true, "count over");
  assert.strictEqual(offlineOverBudget(metas, 500, 10), false, "within both");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
