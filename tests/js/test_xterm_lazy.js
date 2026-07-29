// Node-only, dependency-free check for the lazy-xterm URL builder (#180). xterm
// (~285 KB) is loaded on demand only when the console opens, stamped with the
// page rev so a device caches it immutably and a deploy busts it. Pulls the REAL
// xtermAssetUrls out of app.js by marker — so this pins the shipped URL shape
// (base-rooted, rev-stamped), which the service worker's "immutable" route and
// the offline-shell cache both depend on.
//
// Run manually: node tests/js/test_xterm_lazy.js
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

// The block references BASE/PAGE_REV/window/document at call time only, so we
// can extract just the helper by evaluating the function declaration in a bare
// context (nothing runs at define time).
const sandbox = { window: {}, document: { querySelector: () => null } };
vm.createContext(sandbox);
// Only the pure helper is needed; grab from its declaration to the memo var.
const block = extract("function xtermAssetUrls", "let xtermReady");
vm.runInContext(block, sandbox);
assert(typeof sandbox.xtermAssetUrls === "function", "failed to extract xtermAssetUrls");

// Element-wise, not deepStrictEqual: arrays returned across the vm boundary
// carry the vm realm's Array prototype, which deepStrictEqual rejects.
function sameList(actual, expected, msg) {
  assert.strictEqual(actual.length, expected.length, `${msg}: length`);
  expected.forEach((v, i) => assert.strictEqual(actual[i], v, `${msg}: [${i}]`));
}

// 1. Root-mounted deploy, real rev.
let urls = sandbox.xtermAssetUrls("/", "abc123");
assert.strictEqual(urls.css, "/vendor/xterm.css?v=abc123");
sameList(urls.js, ["/vendor/xterm.js?v=abc123", "/vendor/xterm-addon-fit.js?v=abc123"], "root");

// 2. Subpath-mounted deploy (BASE like "/preview/") keeps the base prefix.
urls = sandbox.xtermAssetUrls("/preview/", "def456");
assert.strictEqual(urls.css, "/preview/vendor/xterm.css?v=def456");
assert.strictEqual(urls.js[0], "/preview/vendor/xterm.js?v=def456");

// 3. No rev (PAGE_REV null — a page served without ?v=): no query, still valid URLs.
urls = sandbox.xtermAssetUrls("/", null);
assert.strictEqual(urls.css, "/vendor/xterm.css");
sameList(urls.js, ["/vendor/xterm.js", "/vendor/xterm-addon-fit.js"], "no-rev");

// 4. The fit addon must load AFTER xterm.js (it references the Terminal global).
urls = sandbox.xtermAssetUrls("/", "x");
assert.ok(
  urls.js[0].includes("xterm.js") && urls.js[1].includes("addon-fit"),
  "xterm.js must be ordered before the fit addon",
);

console.log("test_xterm_lazy.js: all assertions passed");
