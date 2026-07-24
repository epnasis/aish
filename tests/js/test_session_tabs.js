// Node-only, dependency-free check for the Sessions tab split + attention
// counters (Recent vs Automated bottom tabs, web UI). Pulls the real
// partitionSessions out of app.js by marker and runs it in an isolated `vm`
// context — the shipped function, not a copy.
//
// Run manually: node tests/js/test_session_tabs.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

const start = src.indexOf("// SESSIONS_PARTITION_START");
const end = src.indexOf("// SESSIONS_PARTITION_END", start);
assert(start !== -1 && end !== -1, "partition markers not found in app.js");

const sandbox = {};
vm.createContext(sandbox);
// vm only surfaces `var` (not const/function-scoped decls) as sandbox props.
vm.runInContext(src.slice(start, end).replace(/\bfunction partitionSessions\b/, "var partitionSessions = function partitionSessions"), sandbox);
const { partitionSessions } = sandbox;
assert(typeof partitionSessions === "function", "failed to extract partitionSessions");

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}\n       ${err.message}`); }
}
// deepStrictEqual compares prototypes, but values built inside the vm realm
// have a different Array/Object constructor — compare by JSON instead.
const eq = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);

check("user chats land in recent, triggered origins in automated", () => {
  const { groups } = partitionSessions([
    { name: "a", origin: "user", state: "idle" },
    { name: "b", state: "idle" },                 // no origin = user chat
    { name: "c", origin: "schedule", state: "idle" },
    { name: "d", origin: "email", state: "idle" },
  ]);
  eq(groups.recent.map((s) => s.name), ["a", "b"]);
  eq(groups.automated.map((s) => s.name), ["c", "d"]);
});

check("counters tally running / waiting per tab independently", () => {
  const { counts } = partitionSessions([
    { origin: "user", state: "running" },
    { origin: "user", state: "waiting" },
    { origin: "user", state: "idle" },
    { origin: "webhook", state: "waiting" },
    { origin: "webhook", state: "waiting" },
    { origin: "schedule", state: "running" },
  ]);
  eq(counts.recent, { running: 1, waiting: 1 });
  eq(counts.automated, { running: 1, waiting: 2 });
});

check("empty input yields empty groups and zero counts", () => {
  const { groups, counts } = partitionSessions([]);
  eq(groups.recent, []);
  eq(groups.automated, []);
  eq(counts.recent, { running: 0, waiting: 0 });
  eq(counts.automated, { running: 0, waiting: 0 });
});

check("isActive flags exactly running and waiting", () => {
  const { isActive } = partitionSessions([]);
  assert.strictEqual(isActive({ state: "running" }), true);
  assert.strictEqual(isActive({ state: "waiting" }), true);
  assert.strictEqual(isActive({ state: "idle" }), false);
  assert.strictEqual(isActive({}), false);
});

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log("\nall checks passed");
