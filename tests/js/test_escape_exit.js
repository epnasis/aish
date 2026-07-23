// Node-only, dependency-free regression check for issue #143 (Esc exits
// terminal mode / the interactive PTY overlay). Pulls the real escapeExit()
// out of app.js by marker and runs it in an isolated vm against fake
// dependencies — exercising the shipped branching, not a copy.
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
  ptyOpen: false,
  cmdMode: false,
  input: { focus() { calls.push("input.focus"); } },
  closePty(kill) { calls.push(`closePty(${kill})`); sandbox.ptyOpen = false; },
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

check("PTY overlay open: Esc closes it (killing the process) and focuses composer", () => {
  sandbox.ptyOpen = true;
  sandbox.cmdMode = false;
  assert.strictEqual(sandbox.escapeExit(), true, "should report it acted");
  assert.deepStrictEqual(calls, ["closePty(true)", "input.focus"]);
});

check("terminal mode (no overlay): Esc exits cmdMode", () => {
  sandbox.ptyOpen = false;
  sandbox.cmdMode = true;
  assert.strictEqual(sandbox.escapeExit(), true);
  assert.deepStrictEqual(calls, ["exitCmdMode"]); // exitCmdMode focuses input itself
});

check("PTY overlay wins over cmdMode when both are somehow set", () => {
  sandbox.ptyOpen = true;
  sandbox.cmdMode = true;
  assert.strictEqual(sandbox.escapeExit(), true);
  assert.deepStrictEqual(calls, ["closePty(true)", "input.focus"]);
  assert.ok(!calls.includes("exitCmdMode"), "must not also exit cmdMode");
});

check("neither active: Esc is left alone (not hijacked)", () => {
  sandbox.ptyOpen = false;
  sandbox.cmdMode = false;
  assert.strictEqual(sandbox.escapeExit(), false);
  assert.deepStrictEqual(calls, []);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
