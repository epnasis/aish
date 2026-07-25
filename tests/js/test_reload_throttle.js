// Node-only, dependency-free check for the update-reload throttle (#165).
//
// The page reloads itself when the server reports a different code revision.
// Once a service worker sits in front of index.html, one bad cache entry turns
// that into an infinite loop (reload → stale HTML → same stale rev → reload),
// which bricks the installed app on the device it happens to. Two guards, both
// pinned here: purge the cached shell BEFORE reloading, and refuse to reload
// more than RELOAD_MAX times inside RELOAD_WINDOW_MS.
//
// Pulls the REAL reloadThrottled() out of app.js by marker.
//
// Run manually: node tests/js/test_reload_throttle.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);
const start = src.indexOf("// [OFFLINE-RELOAD-START]");
const end = src.indexOf("// [OFFLINE-RELOAD-END]");
assert(start !== -1 && end !== -1, "OFFLINE-RELOAD markers not found in app.js");

function makeSandbox({ controlled }) {
  const calls = [];
  const store = new Map();
  const posted = [];
  let messageListener = null;
  const sandbox = {
    JSON,
    Date,
    setTimeout: (fn, ms) => calls.push(["setTimeout", ms]) && undefined,
    sessionStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, v),
    },
    showToast: (text) => calls.push(["toast", text]),
    location: { reload: () => calls.push(["reload"]) },
    navigator: {
      serviceWorker: controlled
        ? {
            controller: { postMessage: (m) => posted.push(m) },
            addEventListener: (_type, fn) => { messageListener = fn; },
          }
        : { controller: null, addEventListener: () => {} },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(src.slice(start, end), sandbox);
  return { sandbox, calls, posted, fire: (data) => messageListener && messageListener({ data }) };
}

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

check("with no service worker it reloads straight away", () => {
  const { sandbox, calls } = makeSandbox({ controlled: false });
  assert.strictEqual(sandbox.reloadThrottled("rev"), true);
  assert.deepStrictEqual(calls, [["reload"]]);
});

check("with a service worker it purges the shell BEFORE reloading", () => {
  const { sandbox, calls, posted, fire } = makeSandbox({ controlled: true });
  assert.strictEqual(sandbox.reloadThrottled("rev"), true);
  // The purge is requested and the reload waits for it — reloading first would
  // just re-serve the stale shell and start the loop.
  // JSON-compare: the object was built inside the vm realm, so its prototype
  // differs and deepStrictEqual would reject an otherwise identical value.
  assert.strictEqual(
    JSON.stringify(posted), JSON.stringify([{ type: "PURGE_SHELL", reason: "rev" }])
  );
  assert.deepStrictEqual(calls.filter((c) => c[0] === "reload"), []);
  fire({ type: "SHELL_PURGED" });
  assert.deepStrictEqual(calls.filter((c) => c[0] === "reload"), [["reload"]]);
});

check("a purge that never answers still reloads on the fallback timer", () => {
  const { sandbox, calls } = makeSandbox({ controlled: true });
  sandbox.reloadThrottled("rev");
  // A reload that reuses the cache still beats no reload at all, so the timer
  // is armed unconditionally.
  const timers = calls.filter((c) => c[0] === "setTimeout");
  assert.strictEqual(timers.length, 1, "expected exactly one fallback timer");
  assert.ok(timers[0][1] > 0 && timers[0][1] <= 5000, `implausible fallback delay: ${timers[0][1]}`);
});

check("a reload storm is cut off and the user is told", () => {
  const { sandbox, calls } = makeSandbox({ controlled: false });
  assert.strictEqual(sandbox.reloadThrottled("rev"), true);
  assert.strictEqual(sandbox.reloadThrottled("rev"), true);
  assert.strictEqual(sandbox.reloadThrottled("rev"), true);
  // The fourth inside the window is refused: one revision behind is a
  // papercut, an app that reboots forever is unusable.
  assert.strictEqual(sandbox.reloadThrottled("rev"), false);
  assert.strictEqual(calls.filter((c) => c[0] === "reload").length, 3);
  assert.ok(
    calls.some((c) => c[0] === "toast" && /loop/i.test(c[1])),
    "expected a toast explaining why it stopped reloading"
  );
});

check("reloads outside the window don't count against the budget", () => {
  const { sandbox, calls } = makeSandbox({ controlled: false });
  const realNow = Date.now;
  try {
    let now = 1_000_000;
    sandbox.Date = { now: () => now };
    sandbox.reloadThrottled("rev");
    sandbox.reloadThrottled("rev");
    sandbox.reloadThrottled("rev");
    now += 120000; // two minutes later — a genuinely new update
    assert.strictEqual(sandbox.reloadThrottled("rev"), true);
    assert.strictEqual(calls.filter((c) => c[0] === "reload").length, 4);
  } finally {
    Date.now = realNow;
  }
});

check("a corrupt counter is treated as empty, not fatal", () => {
  const { sandbox, calls } = makeSandbox({ controlled: false });
  sandbox.sessionStorage.setItem("aish-reloads", "{not json");
  assert.strictEqual(sandbox.reloadThrottled("rev"), true);
  assert.deepStrictEqual(calls, [["reload"]]);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
