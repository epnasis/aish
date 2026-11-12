// Node-only, dependency-free check for the web half of /who (slice 2).
// Pulls the REAL SLASH_COMMANDS / SLASH_ALL / handleSlash out of app.js by
// marker (test_automation_slash.js's pattern) and runs them against stubs.
//
// What it pins: /who opens its sheet and sends ONE `who` view message carrying
// only the words typed — never a task, so nothing reaches a model; the answer
// is painted into the sheet; and a chip can never send it.
//
// Run manually: node tests/js/test_who_slash.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

const calls = [];
const sandbox = {
  send(message) { calls.push({ send: JSON.parse(JSON.stringify(message)) }); return true; },
  act(message) { calls.push({ act: message }); return true; },
  openSheet(id) { calls.push({ sheet: id }); },
  showToast(text) { calls.push({ toast: text }); },
};
vm.createContext(sandbox);
vm.runInContext((
  extract("const SLASH_COMMANDS = [", "];") + "];\n" +
  extract("const SLASH_ALL", "function handleSlash") +
  extract("function handleSlash", "// ---- attachments")
).replace(/\bconst\b/g, "var"), sandbox);

let failures = 0;
function check(name, fn) {
  calls.length = 0;
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}\n       ${err.message}`);
  }
}

check("/who opens its sheet and asks the server with the words only", () => {
  assert.strictEqual(sandbox.handleSlash("/who Anna Kowalska"), true);
  assert.deepStrictEqual(calls, [
    { sheet: "who-sheet" },
    { send: { type: "who", query: "Anna Kowalska" } },
  ]);
});

check("bare /who lists everything", () => {
  sandbox.handleSlash("/who");
  assert.deepStrictEqual(calls[1], { send: { type: "who", query: "" } });
});

check("/who is never a task: nothing it sends can reach a model", () => {
  sandbox.handleSlash("/who anna");
  assert.ok(calls.every((c) => !c.send || c.send.type !== "task"));
});

check("/who is offered in the composer's list", () => {
  assert.ok(sandbox.SLASH_COMMANDS.some(([cmd]) => cmd === "/who"));
});

check("the answer is painted into the sheet as text", () => {
  assert.ok(/case "who_list": \$\("who-text"\)\.textContent = event\.text/.test(src));
});

check("a chip can never send /who: submitInput routes only TYPED slashes", () => {
  const body = extract("// [CHIP-NEVER-COMMANDS-START]", "// [CHIP-NEVER-COMMANDS-END]");
  assert.ok(/text\.startsWith\("\/"\) && !fromChip/.test(body));
});

if (failures) {
  console.error(`test_who_slash.js: ${failures} failure(s)`);
  process.exit(1);
}
console.log("test_who_slash.js: all checks passed");
