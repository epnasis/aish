// Node-only, dependency-free check for the service worker's routing table
// (#165). Which cache strategy a request gets is the whole safety story of
// offline mode, and two of the rules are load-bearing:
//
//   * live data (/ws, /offline/*, /upload, /trigger, /export/*, /dirs) must
//     NEVER be served from a cache — a stale session list or a replayed upload
//     is worse than an honest failure;
//   * a navigation must be network-first, because index.html carries the
//     ?v=<rev> the page compares against the server's rev. Serving a stale
//     index to an online client is exactly the reload loop the comment at the
//     top of sw.js exists to prevent.
//
// Pulls the REAL routeFor() out of sw.js by marker.
//
// Run manually: node tests/js/test_sw_routes.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "sw.js"), "utf8"
);
const start = src.indexOf("// [SW-ROUTE-START]");
const end = src.indexOf("// [SW-ROUTE-END]");
assert(start !== -1 && end !== -1, "SW-ROUTE markers not found in sw.js");

// routeFor closes over NEVER_CACHE, which lives outside the markers (it is
// shared with the message handlers) — pull that one declaration in too.
const neverCache = src.match(/^const NEVER_CACHE = .*$/m);
assert(neverCache, "NEVER_CACHE declaration not found in sw.js");

const sandbox = { URL };
vm.createContext(sandbox);
vm.runInContext(
  neverCache[0].replace("const", "var") + "\n" + src.slice(start, end),
  sandbox
);
const { routeFor } = sandbox;
assert(typeof routeFor === "function", "routeFor not extracted");

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

// Both a root deploy and the subpath-mounted branch preview (/preview/), since
// the prefix matching is re-rooted at the scope.
const ROOT = new URL("https://aish.example/");
const SUB = new URL("https://aish.example/preview/");

function route(url, { method = "GET", mode = "no-cors", scope = ROOT } = {}) {
  return routeFor({ method, mode }, new URL(url, scope), scope);
}

check("a navigation is network-first", () => {
  assert.strictEqual(route("https://aish.example/", { mode: "navigate" }), "navigate");
  assert.strictEqual(
    route("https://aish.example/preview/", { mode: "navigate", scope: SUB }), "navigate"
  );
});

check("live data endpoints are never cached", () => {
  for (const p of ["/ws", "/offline/index", "/offline/session", "/upload", "/trigger",
                   "/export/session", "/export/answer", "/dirs"]) {
    assert.strictEqual(route(`https://aish.example${p}`), "pass", p);
    assert.strictEqual(route(`https://aish.example/preview${p}`, { scope: SUB }), "pass", p);
  }
});

check("a query string cannot smuggle live data into the immutable cache", () => {
  // /offline/session?...&v=... must still be "pass", not "immutable".
  assert.strictEqual(route("https://aish.example/offline/session?session=x&v=abc"), "pass");
});

check("rev-stamped assets are immutable", () => {
  assert.strictEqual(route("https://aish.example/app.js?v=abc123"), "immutable");
  assert.strictEqual(route("https://aish.example/style.css?v=abc123"), "immutable");
  assert.strictEqual(route("https://aish.example/preview/app.js?v=abc", { scope: SUB }), "immutable");
});

check("unversioned static assets revalidate", () => {
  assert.strictEqual(route("https://aish.example/manifest.json"), "revalidate");
  assert.strictEqual(route("https://aish.example/vendor/xterm.js"), "revalidate");
  assert.strictEqual(route("https://aish.example/icon-192.png"), "revalidate");
});

check("transcript images get the bounded image cache", () => {
  assert.strictEqual(route("https://aish.example/file?path=/tmp/a.png&token=t"), "image");
});

check("non-GET requests are never intercepted", () => {
  // checkAppVersion() HEADs app.js to detect a new build; intercepting it would
  // answer from cache and the app would never notice an update.
  assert.strictEqual(route("https://aish.example/app.js", { method: "HEAD" }), "pass");
  assert.strictEqual(route("https://aish.example/upload", { method: "POST" }), "pass");
});

check("cross-origin and out-of-scope requests are left alone", () => {
  assert.strictEqual(route("https://other.example/app.js"), "pass");
  assert.strictEqual(route("https://aish.example/elsewhere/app.js", { scope: SUB }), "pass");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
