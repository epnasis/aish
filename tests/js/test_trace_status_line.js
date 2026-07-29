// Node-only, dependency-free check of the live trace-header status line. Pulls
// the REAL [TRACE-STATUS] region out of app.js by marker and runs it in an
// isolated vm — exercising the shipped priority chain, not a copy.
//
// The collapsed trace header must always say what is happening NOW:
//   Stopping… > Waiting for approval… > Answering… > running tool
//   (model's own words win over the deterministic line, parallel tools count)
//   > streaming thinking gist > Thinking… > last turn's words > Working…
//
// Run manually: node tests/js/test_trace_status_line.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);
const start = src.indexOf("// [TRACE-STATUS-START]");
const end = src.indexOf("// [TRACE-STATUS-END]");
assert(start !== -1 && end !== -1, "TRACE-STATUS markers not found in app.js");

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(src.slice(start, end), sandbox);
assert(typeof sandbox.traceStatusLine === "function", "traceStatusLine not extracted");

// A trace state object exactly as ensureTrace() initializes the status fields.
function state(overrides) {
  return Object.assign({
    turnSay: null, turnGist: null, liveGist: null, running: [],
    waitingApproval: false, answering: false, stopping: false,
    thinkingRow: null,
  }, overrides);
}
const line = (overrides) => sandbox.traceStatusLine(state(overrides));

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}\n       ${err.message}`);
  }
}

check("idle turn start says Working…", () => {
  assert.strictEqual(line({}), "Working…");
});

check("a live thinking row says Thinking…", () => {
  assert.strictEqual(line({ thinkingRow: {} }), "Thinking…");
});

check("streaming thinking gist wins over the bare Thinking… — no label", () => {
  assert.strictEqual(
    line({ thinkingRow: {}, liveGist: "comparing the two configs" }),
    "comparing the two configs"
  );
});

check("a running command shows the command itself", () => {
  assert.strictEqual(
    line({ running: [{ name: "run_command", summary: "git status", command: "git status" }] }),
    "Running: git status"
  );
});

check("known tools get human phrasing from their summary", () => {
  assert.strictEqual(
    line({ running: [{ name: "read_file", summary: "server.py", command: "" }] }),
    "Reading server.py"
  );
  assert.strictEqual(
    line({ running: [{ name: "web_search", summary: "ollama thinking api", command: "" }] }),
    "Searching: ollama thinking api"
  );
});

check("an unknown (plugin) tool falls back to its name", () => {
  assert.strictEqual(
    line({ running: [{ name: "reminders_add", summary: "milk", command: "" }] }),
    "Running reminders_add: milk"
  );
});

check("the model's own words win over the deterministic tool line", () => {
  assert.strictEqual(
    line({
      turnSay: "Checking the config for the port setting.",
      running: [{ name: "run_command", summary: "", command: "grep port config" }],
    }),
    "Checking the config for the port setting."
  );
});

check("thinking gist is the fallback when there was no preamble", () => {
  assert.strictEqual(
    line({
      turnGist: "I should compare both files first.",
      running: [{ name: "read_file", summary: "a.py", command: "" }],
    }),
    "I should compare both files first."
  );
});

check("parallel tools count the extras", () => {
  assert.strictEqual(
    line({
      running: [
        { name: "read_file", summary: "a.py", command: "" },
        { name: "read_file", summary: "b.py", command: "" },
        { name: "read_file", summary: "c.py", command: "" },
      ],
    }),
    "Reading c.py · +2 more"
  );
});

check("model words also carry the parallel counter", () => {
  assert.strictEqual(
    line({
      turnSay: "Reading both halves.",
      running: [
        { name: "read_file", summary: "a.py", command: "" },
        { name: "read_file", summary: "b.py", command: "" },
      ],
    }),
    "Reading both halves. · +1 more"
  );
});

check("after the tools finish, the turn's words still label the gap", () => {
  assert.strictEqual(line({ turnSay: "Now verifying the fix." }), "Now verifying the fix.");
});

check("waiting for approval wins over the running tool", () => {
  assert.strictEqual(
    line({
      waitingApproval: true,
      turnSay: "Deleting the stale branch.",
      running: [{ name: "run_command", summary: "", command: "git branch -D x" }],
    }),
    "Waiting for approval…"
  );
});

check("answering wins over leftover turn words", () => {
  assert.strictEqual(line({ answering: true, turnSay: "old words" }), "Answering…");
});

check("stopping wins over everything (pins the old ticker-overwrite bug)", () => {
  assert.strictEqual(
    line({
      stopping: true, waitingApproval: true, answering: true,
      liveGist: "g", turnSay: "s",
      running: [{ name: "run_command", summary: "", command: "ls" }],
      thinkingRow: {},
    }),
    "Stopping…"
  );
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
