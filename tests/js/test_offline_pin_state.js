// Node-only, dependency-free regression check for the "Available offline" pin
// state (#165 follow-up).
//
// The bug this guards: the menu label read the in-memory `offlineMeta` mirror
// while the toggle read IndexedDB. That mirror is empty for a moment after
// every reload, so a genuinely pinned chat showed "Off" — and tapping the item
// to pin it read the REAL state and flipped it, silently unpinning the chat the
// user was trying to protect. It looked exactly like "my pin got reset by the
// update". Both sides must read the same source: the store.
//
// Run manually: node tests/js/test_offline_pin_state.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");
const start = src.indexOf("// [OFFLINE-PIN-STATE-START]");
const end = src.indexOf("// [OFFLINE-PIN-STATE-END]");
assert(start !== -1 && end !== -1, "OFFLINE-PIN-STATE markers not found in app.js");

let failures = 0;
function check(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(
      () => console.log(`ok - ${name}`),
      (err) => { failures += 1; console.error(`FAIL - ${name}\n    ${err.message}`); }
    );
}

// The store's contents, and a deliberately WRONG in-memory mirror standing in
// for the post-reload window.
function makeSandbox(store) {
  const reads = [];
  const sandbox = {
    offlineMeta: new Map(), // empty, as it is right after a reload
    idbGet: (which, key) => { reads.push([which, key]); return Promise.resolve(store[key]); },
    offlineSafe: (p, fallback = null) => p.catch(() => fallback),
  };
  vm.createContext(sandbox);
  vm.runInContext(src.slice(start, end), sandbox);
  return { sandbox, reads };
}

(async () => {
  await check("a pinned chat reads as pinned even with an empty in-memory mirror", async () => {
    // The exact failing condition: mirror empty, store says pinned.
    const { sandbox, reads } = makeSandbox({ "a.jsonl": { name: "a.jsonl", pinned: true } });
    assert.strictEqual(await sandbox.offlineIsPinned("a.jsonl"), true);
    assert.strictEqual(sandbox.offlineMeta.size, 0, "mirror must not have been consulted");
    assert.deepStrictEqual(reads, [["meta", "a.jsonl"]], "must read the meta store");
  });

  await check("an unpinned chat reads as unpinned", async () => {
    const { sandbox } = makeSandbox({ "a.jsonl": { name: "a.jsonl", pinned: false } });
    assert.strictEqual(await sandbox.offlineIsPinned("a.jsonl"), false);
  });

  await check("a chat missing from the store is not pinned", async () => {
    const { sandbox } = makeSandbox({});
    assert.strictEqual(await sandbox.offlineIsPinned("gone.jsonl"), false);
  });

  await check("no session name is not pinned, and touches no store", async () => {
    const { sandbox, reads } = makeSandbox({});
    assert.strictEqual(await sandbox.offlineIsPinned(null), false);
    assert.strictEqual(await sandbox.offlineIsPinned(""), false);
    assert.deepStrictEqual(reads, []);
  });

  await check("a stale mirror can never override the store", async () => {
    // Mirror says pinned, store says it isn't: the store wins in both
    // directions, or the label and the toggle can disagree again.
    const { sandbox } = makeSandbox({ "a.jsonl": { name: "a.jsonl", pinned: false } });
    sandbox.offlineMeta.set("a.jsonl", { name: "a.jsonl", pinned: true });
    assert.strictEqual(await sandbox.offlineIsPinned("a.jsonl"), false);
  });

  await check("the value is always a boolean, never a leaked record", async () => {
    const { sandbox } = makeSandbox({ "a.jsonl": { name: "a.jsonl", pinned: "yes" } });
    assert.strictEqual(await sandbox.offlineIsPinned("a.jsonl"), true);
  });

  await check("the toggle's appearance is not wired to the in-memory mirror", async () => {
    // Structural guard: whatever paints the toggle must not read offlineMeta.
    // This is the wiring that regressed, and a unit test on offlineIsPinned
    // alone would not catch someone reintroducing the shortcut at the call site.
    const ui = src.slice(
      src.indexOf("async function refreshOfflinePinUi"),
      src.indexOf("async function toggleOfflinePin")
    );
    assert.ok(ui, "refreshOfflinePinUi not found");
    assert.ok(
      !/offlineMeta/.test(ui),
      "the pin toggle must not read offlineMeta — read the store via offlineIsPinned"
    );
    assert.ok(/offlineIsPinned/.test(ui), "the pin toggle must go through offlineIsPinned");
    // Until the store answers, the row must claim NEITHER state: an "Off"
    // reading on a pinned chat is what made a tap unpin it.
    assert.ok(/"…"/.test(ui), "the row must show an unknown state until the store answers");
  });

  await check("pinning lives in the chat menu, in one place", async () => {
    // It moved out of the title bar (#rail follow-up): a per-chat setting used
    // rarely, competing for the scarcest space in the app. What must hold is
    // that there is exactly ONE control, wherever it lives.
    const html = fs.readFileSync(
      path.join(__dirname, "..", "..", "aish", "static", "index.html"), "utf8"
    );
    assert.ok(/data-act="pin"/.test(html), "the chat-menu pin row is missing");
    assert.ok(/id="pin-state"/.test(html), "the pin row must show its state");
    assert.ok(
      !/id="offline-btn"/.test(html),
      "the header toggle should be gone — the menu row replaces it"
    );
    assert.ok(
      /case "pin": toggleOfflinePin\(\); break;/.test(src),
      "the menu row must be wired straight to toggleOfflinePin"
    );
    assert.ok(
      (src.match(/toggleOfflinePin\(\)/g) || []).length === 2,
      "exactly one caller (plus the definition) — two pin controls can disagree"
    );
    // There is no bulk "clear offline copies" action: the mirror manages its
    // own size, and the old one also dropped the cached app shell, breaking
    // "the app always opens offline" until the next successful load.
    assert.ok(
      !/data-act="clear-offline"/.test(html),
      "the bulk clear action was removed — the mirror self-manages"
    );
    assert.ok(!/CLEAR_CACHES/.test(src), "no client should still ask the SW to wipe its caches");
  });

  if (failures) {
    console.error(`\n${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("\nall checks passed");
})();
