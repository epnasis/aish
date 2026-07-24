// Node-only, dependency-free check that the swipe pager stays inside one lane
// (Recent vs Automated, #160). Pulls the real pagerLane/laneNeighbor out of
// app.js by marker and runs them in an isolated `vm` context — the shipped
// functions, not copies.
//
// Run manually: node tests/js/test_pager_lanes.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

const start = src.indexOf("// PAGER_LANE_START");
const end = src.indexOf("// PAGER_LANE_END", start);
assert(start !== -1 && end !== -1, "pager lane markers not found in app.js");

const sandbox = {};
vm.createContext(sandbox);
// vm only surfaces `var` (not const/function-scoped decls) as sandbox props.
vm.runInContext(
  src
    .slice(start, end)
    .replace(/\bfunction pagerLane\b/, "var pagerLane = function pagerLane")
    .replace(/\bfunction laneNeighbor\b/, "var laneNeighbor = function laneNeighbor"),
  sandbox
);
const { pagerLane, laneNeighbor } = sandbox;
assert(typeof pagerLane === "function", "failed to extract pagerLane");
assert(typeof laneNeighbor === "function", "failed to extract laneNeighbor");

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}\n       ${err.message}`); }
}

// Interleaved on disk (recency order), as they really arrive from hello.pager.
const pages = [
  { name: "u1", origin: "user" },
  { name: "a1", origin: "schedule" },
  { name: "u2" },                      // no origin = user chat
  { name: "a2", origin: "email" },
  { name: "u3", origin: "user" },
];
const nameOf = (page) => (page ? page.name : null);

check("lane classification follows the Sessions tabs", () => {
  assert.strictEqual(pagerLane({ origin: "user" }), "recent");
  assert.strictEqual(pagerLane({}), "recent");
  assert.strictEqual(pagerLane(undefined), "recent");
  assert.strictEqual(pagerLane({ origin: "webhook" }), "automated");
});

check("a user chat pages only through user chats", () => {
  assert.strictEqual(nameOf(laneNeighbor(pages, "u2", 1)), "u3");
  assert.strictEqual(nameOf(laneNeighbor(pages, "u2", -1)), "u1");
});

check("a triggered chat pages only through triggered chats", () => {
  assert.strictEqual(nameOf(laneNeighbor(pages, "a1", 1)), "a2");
  assert.strictEqual(nameOf(laneNeighbor(pages, "a2", -1)), "a1");
});

check("each lane ends on its own edges, not the other lane's", () => {
  assert.strictEqual(laneNeighbor(pages, "u1", -1), null);
  assert.strictEqual(laneNeighbor(pages, "u3", 1), null);
  assert.strictEqual(laneNeighbor(pages, "a1", -1), null);
  assert.strictEqual(laneNeighbor(pages, "a2", 1), null);
});

check("an unknown current session has no neighbours", () => {
  assert.strictEqual(laneNeighbor(pages, "nope", 1), null);
  assert.strictEqual(laneNeighbor(pages, "nope", -1), null);
});

process.exit(failures ? 1 : 0);
