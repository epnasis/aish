// Node-only, dependency-free check for issue #146 (the /session slash command
// copies the session log path). Pulls the real SLASH_COMMANDS / SLASH_ALL /
// handleSlash out of app.js by marker and runs the /session branch against a
// stub copyLogPath — proving the web case exists (a missing case would fall to
// the "unknown command" default, the exact #146 gotcha).
//
// Run manually: node tests/js/test_session_slash.js
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
  copyLogPath() { calls.push("copyLogPath"); },
  showToast(t) { calls.push(`showToast:${t}`); },
};
vm.createContext(sandbox);
const snippet = (
  extract("const SLASH_COMMANDS = [", "];") + "];\n" +
  extract("const SLASH_ALL", "function handleSlash") +
  extract("function handleSlash", "// ---- attachments")
).replace(/\bconst\b/g, "var");
vm.runInContext(snippet, sandbox);
assert(typeof sandbox.handleSlash === "function", "failed to extract handleSlash from app.js");
assert(sandbox.SLASH_ALL.includes("/session"), "/session must be in SLASH_ALL");

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

check("/session copies the log path and reports handled", () => {
  const handled = sandbox.handleSlash("/session");
  assert.strictEqual(handled, true, "should report the command was handled");
  assert.deepStrictEqual(calls, ["copyLogPath"]);
});

check("an unambiguous prefix (/sess) resolves to /session", () => {
  const handled = sandbox.handleSlash("/sess");
  assert.strictEqual(handled, true);
  assert.deepStrictEqual(calls, ["copyLogPath"]);
});

check("/session is NOT swallowed by the unknown-command default", () => {
  sandbox.handleSlash("/session");
  assert.ok(!calls.some((c) => c.startsWith("showToast:unknown")), "must not hit the default");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
