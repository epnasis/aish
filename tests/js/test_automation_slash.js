// Node-only, dependency-free check for the web half of #5 S4's commands.
// Pulls the REAL SLASH_COMMANDS / SLASH_ALL / handleSlash out of app.js by
// marker (test_session_slash.js's pattern) and runs them against stubs.
//
// What it pins: /tasks and /runs exist on the web only to SAY they are
// terminal-only — never "unknown command"; /run-now sends one `run_now` act
// carrying nothing but which automation (the server reads the rest from the
// owner's file); and a chip can never reach any of it, which is
// test_chip_never_commands.js's property, re-checked here for /run-now.
//
// Run manually: node tests/js/test_automation_slash.js
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
  act(message, options) {
    calls.push(JSON.parse(JSON.stringify({ act: message, label: options && options.label })));
    return true;
  },
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

for (const command of ["/tasks", "/runs"]) {
  check(`${command} answers terminal-only, never "unknown command"`, () => {
    assert.strictEqual(sandbox.handleSlash(command), true);
    assert.deepStrictEqual(calls, [
      { toast: `${command} is a terminal-only command — run it in aish in a terminal` },
    ]);
  });
  check(`${command} with an argument is still terminal-only`, () => {
    sandbox.handleSlash(`${command} enable daily`);
    assert.ok(calls[0].toast.includes("terminal-only"));
  });
}

check("/run-now with no argument asks for the chat on screen's automation", () => {
  assert.strictEqual(sandbox.handleSlash("/run-now"), true);
  assert.deepStrictEqual(calls, [{ act: { type: "run_now" }, label: "starting that run" }]);
});

check("/run-now <id> names the automation and nothing else", () => {
  sandbox.handleSlash("/run-now daily-summary");
  assert.deepStrictEqual(calls, [
    { act: { type: "run_now", task: "daily-summary" }, label: "starting that run" },
  ]);
});

check("/run-now is offered in the composer's list", () => {
  assert.ok(sandbox.SLASH_COMMANDS.some(([cmd]) => cmd === "/run-now"));
  assert.ok(!sandbox.SLASH_COMMANDS.some(([cmd]) => cmd === "/tasks" || cmd === "/runs"),
    "the terminal-only commands are dispatched, not advertised");
});

check("a chip can never send /run-now: submitInput routes only TYPED slashes", () => {
  const body = extract("// [CHIP-NEVER-COMMANDS-START]", "// [CHIP-NEVER-COMMANDS-END]");
  assert.ok(/text\.startsWith\("\/"\) && !fromChip/.test(body));
});

check("run_started is shown to the owner", () => {
  assert.ok(/case "run_started": showToast\(event\.text/.test(src));
});

if (failures) {
  console.error(`test_automation_slash.js: ${failures} failure(s)`);
  process.exit(1);
}
console.log("test_automation_slash.js: all checks passed");
