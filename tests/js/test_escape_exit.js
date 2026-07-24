// Node-only, dependency-free regression check for issue #143 (Esc exits
// terminal mode / the global console overlay). Pulls the real escapeExit()
// out of app.js by marker and runs it in an isolated vm against fake
// dependencies — exercising the shipped branching, not a copy.
//
// The global console (#148 follow-up) treats Esc as a REAL terminal key
// (vim/tmux/less): escapeExit does NOT touch it — it's closed via its button or
// Ctrl+\. escapeExit only leaves the old `!` terminal-input mode (cmdMode).
//
// Run manually: node tests/js/test_escape_exit.js
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

const calls = [];
const sandbox = {
  consoleOpen: false,
  cmdMode: false,
  input: { focus() { calls.push("input.focus"); } },
  hideConsole() { calls.push("hideConsole"); sandbox.consoleOpen = false; },
  exitCmdMode() { calls.push("exitCmdMode"); sandbox.cmdMode = false; },
};
vm.createContext(sandbox);
vm.runInContext(extract("function escapeExit", "// [ESC-EXIT-END]"), sandbox);
assert(typeof sandbox.escapeExit === "function", "failed to extract escapeExit from app.js");

let failures = 0;
function check(name, fn) {
  calls.length = 0;
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}`);
    console.error(`       ${err.message}`);
  }
}

check("console overlay open: Esc does NOT close it (real terminal key, passes to PTY)", () => {
  sandbox.consoleOpen = true;
  sandbox.cmdMode = false;
  assert.strictEqual(sandbox.escapeExit(), false, "must not act — Esc belongs to the program");
  assert.deepStrictEqual(calls, []);
});

check("terminal mode (no overlay): Esc exits cmdMode", () => {
  sandbox.consoleOpen = false;
  sandbox.cmdMode = true;
  assert.strictEqual(sandbox.escapeExit(), true);
  assert.deepStrictEqual(calls, ["exitCmdMode"]); // exitCmdMode focuses input itself
});

check("console open + cmdMode: Esc only exits cmdMode, never touches the console", () => {
  sandbox.consoleOpen = true;
  sandbox.cmdMode = true;
  assert.strictEqual(sandbox.escapeExit(), true);
  assert.deepStrictEqual(calls, ["exitCmdMode"]);
  assert.ok(!calls.includes("hideConsole"), "must not close the console");
});

check("neither active: Esc is left alone (not hijacked)", () => {
  sandbox.consoleOpen = false;
  sandbox.cmdMode = false;
  assert.strictEqual(sandbox.escapeExit(), false);
  assert.deepStrictEqual(calls, []);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
