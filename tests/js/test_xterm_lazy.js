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

// ---- placeholder status must never outlive the real one ------------------
// Regression: openConsole sends console_open FIRST and only then writes its
// provisional "attaching…"/"loading terminal…" text. On a fast link the server's
// console_started reply beats those writes, so they clobbered the real label —
// and console_started fires once per open, so nothing rewrote it: a placeholder
// sat over a fully working terminal. Widened by the lazy xterm load, which adds
// an await between the send and the last placeholder write.
{
  const fs2 = require("fs");
  const path2 = require("path");
  const assert2 = require("assert");
  const vm2 = require("vm");
  const app = fs2.readFileSync(
    path2.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8");

  // Every provisional write goes through the guarded helper, never straight to
  // setConsoleStatus — that is what makes the rule enforceable rather than
  // remembered at each call site.
  for (const text of ["attaching…", "CONSOLE_LOADING"]) {
    assert2(
      new RegExp(`setProvisionalConsoleStatus\\(${text === "CONSOLE_LOADING" ? "CONSOLE_LOADING" : '"attaching…"'}\\)`)
        .test(app),
      `${text} must be written through setProvisionalConsoleStatus`,
    );
  }
  assert2(!/\n  setConsoleStatus\("attaching…"\);/.test(app),
    "no unguarded placeholder write may remain");
  assert2(/consoleStartedSeen = false; \/\/ a new open/.test(app),
    "the flag must reset as an open begins, before console_open is sent");
  assert2(/consoleStartedSeen = true;/.test(app),
    "console_started must record that the real label has landed");

  // And the helper itself behaves: run the REAL function against a fake label.
  const start = app.indexOf("const CONSOLE_LOADING");
  const end = app.indexOf("function setConsoleCtrlMode");
  // Give it a fake #pty-status: the slice defines the REAL setConsoleStatus,
  // so stubbing that would test a copy rather than the shipped function.
  const label = { textContent: "", classList: { toggle() {} } };
  const sandbox = { consoleStartedSeen: false, $: () => label };
  vm2.createContext(sandbox);
  // vm does not surface top-level const/let on the sandbox, so rewrite the two
  // declarations the test drives — the same trick the deck/pager extractors use.
  const slice = app.slice(start, end)
    .replace(/\nfunction (\w+)/g, "\nvar $1 = function $1")
    .replace(/\nlet consoleStartedSeen/, "\nvar consoleStartedSeen");
  vm2.runInContext(slice, sandbox);

  sandbox.setProvisionalConsoleStatus("attaching…");
  assert2.strictEqual(label.textContent, "attaching…", "placeholder shows before the real label");
  sandbox.consoleStartedSeen = true;                       // console_started landed
  sandbox.setConsoleStatus("~/aish · tmux · aish-console"); // with the real label
  sandbox.setProvisionalConsoleStatus("attaching…");        // a late placeholder write
  assert2.strictEqual(label.textContent, "~/aish · tmux · aish-console",
    "a placeholder must never overwrite the real label");
  console.log("ok - placeholder status never overwrites the real one");
}
