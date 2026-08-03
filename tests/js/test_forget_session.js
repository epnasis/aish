// Node-only, dependency-free check for what this device forgets when a chat is
// gone from the server (#207 follow-up).
//
// The bug this guards. Deleting a chat dropped every trace of it EXCEPT the one
// that keeps it on screen — the offline mirror — and dropped the one that kept
// it quiet: the seen stamp. The rail paints from the mirror (opening it is a
// local act since the roster plane landed), so the row survived the delete; and
// with no seen stamp its last output was newer than this device's last look, so
// the row was UNREAD. A chat you deleted came back at the TOP of the list under
// "Needs you", counted on the attention badge, and tapping it said "that chat no
// longer exists". Pinned, it could never be pruned by a later sync either.
//
// Pulls the REAL functions out of app.js by marker.
//
// Run manually: node tests/js/test_forget_session.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);

function extract(startMarker, endMarker) {
  const from = src.indexOf(startMarker);
  const to = src.indexOf(endMarker, from);
  assert(from !== -1, `marker not found: ${startMarker}`);
  assert(to !== -1, `marker not found: ${endMarker}`);
  return src.slice(from, to);
}

// A world with a mirror (two IndexedDB stores + the in-memory map), the caches
// forgetSession is supposed to drop, and a seen map it must NOT touch.
function makeWorld({ pinned = false } = {}) {
  const stores = {
    meta: new Map([["gone.jsonl", { name: "gone.jsonl", pinned }], ["keep.jsonl", {}]]),
    events: new Map([["gone.jsonl", { events: [] }], ["keep.jsonl", { events: [] }]]),
  };
  const sandbox = {
    stores,
    idbDel: (which, key) => { stores[which].delete(key); return Promise.resolve(); },
    offlineSafe: (promise, fallback = null) => promise.catch(() => fallback),
    offlineMeta: new Map([["gone.jsonl", { name: "gone.jsonl", pinned }], ["keep.jsonl", {}]]),
    prefetched: new Map([["gone.jsonl", {}], ["keep.jsonl", {}]]),
    viewCache: new Map([["gone.jsonl", {}], ["keep.jsonl", {}]]),
    seenAt: { "gone.jsonl": 1000, "keep.jsonl": 2000 },
    forgotAttention: [],
  };
  sandbox.forgetAttention = (name) => sandbox.forgotAttention.push(name);
  vm.createContext(sandbox);
  vm.runInContext(extract("// [MIRROR-FORGET-START]", "// [MIRROR-FORGET-END]"), sandbox);
  vm.runInContext(extract("// [FORGET-SESSION-START]", "// [FORGET-SESSION-END]"), sandbox);
  assert(typeof sandbox.forgetSession === "function", "forgetSession not extracted");
  return sandbox;
}

let failures = 0;
function check(name, fn) {
  return Promise.resolve().then(fn).then(
    () => console.log(`ok - ${name}`),
    (err) => { failures += 1; console.error(`FAIL - ${name}\n    ${err.message}`); }
  );
}

(async () => {
  await check("a deleted chat's cached copy goes, so the rail cannot paint it again", async () => {
    const w = makeWorld();
    await w.forgetSession("gone.jsonl");
    assert(!w.stores.meta.has("gone.jsonl"), "meta row survived — the rail still lists it");
    assert(!w.stores.events.has("gone.jsonl"), "the cached transcript survived");
    assert(!w.offlineMeta.has("gone.jsonl"), "the in-memory mirror still names it");
  });

  await check("a PINNED chat is forgotten too — gone is gone", async () => {
    const w = makeWorld({ pinned: true });
    await w.forgetSession("gone.jsonl");
    assert(!w.stores.meta.has("gone.jsonl"), "a pinned ghost is one no sync can ever prune");
  });

  await check("the seen stamp SURVIVES — a delete must not make a chat unread", async () => {
    const w = makeWorld();
    await w.forgetSession("gone.jsonl");
    assert.strictEqual(w.seenAt["gone.jsonl"], 1000, "the chat was un-seen by deleting it");
  });

  await check("the peek cache, the stashed view and the roster row all go", async () => {
    const w = makeWorld();
    await w.forgetSession("gone.jsonl");
    assert(!w.prefetched.has("gone.jsonl"), "a warm peek for a dead chat");
    assert(!w.viewCache.has("gone.jsonl"), "a stashed DOM for a dead chat");
    assert.deepStrictEqual(w.forgotAttention, ["gone.jsonl"], "still counted on the badge");
  });

  await check("no other chat is touched", async () => {
    const w = makeWorld();
    await w.forgetSession("gone.jsonl");
    assert(w.stores.meta.has("keep.jsonl") && w.stores.events.has("keep.jsonl"));
    assert(w.prefetched.has("keep.jsonl") && w.viewCache.has("keep.jsonl"));
    assert.strictEqual(w.seenAt["keep.jsonl"], 2000);
  });

  await check("an empty name forgets nothing", async () => {
    const w = makeWorld();
    await w.forgetSession("");
    assert.strictEqual(w.stores.meta.size, 2);
    assert.deepStrictEqual(w.forgotAttention, []);
  });

  await check("the sync prunes exactly what the server no longer lists", async () => {
    const w = makeWorld({ pinned: true });
    const server = new Set(["keep.jsonl"]);
    assert.deepStrictEqual(
      Array.from(w.mirrorOrphans(w.offlineMeta, server)), ["gone.jsonl"],
      "a pinned chat the server deleted must still be prunable"
    );
    assert.deepStrictEqual(
      Array.from(w.mirrorOrphans(w.offlineMeta, new Set(["gone.jsonl", "keep.jsonl"]))), [],
      "nothing the server still has may be pruned"
    );
  });

  if (failures) {
    console.error(`\n${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("\nall checks passed");
})();
