// Node-only, dependency-free check for the console's forced-selection guard.
// Under mouse reporting an Option+drag selection is cleared by xterm the moment
// the forwarded release (user input) goes out; the guard restores it from a
// snapshot and keeps buttonless motion from being forwarded while text is
// selected. Pulls the REAL helpers out of app.js by marker.
//
// Run manually: node tests/js/test_console_selection_guard.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);
const start = src.indexOf("// [CONSOLE-SELECTION-GUARD-START]");
const end = src.indexOf("// [CONSOLE-SELECTION-GUARD-END]");
assert(start !== -1 && end !== -1, "CONSOLE-SELECTION-GUARD markers not found in app.js");

const sandbox = {};
vm.createContext(sandbox);
// Objects built inside the vm have the context's own Object prototype, so compare
// by shape, not by prototype identity.
const same = (a, b) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b));
vm.runInContext(src.slice(start, end), sandbox);
const { consoleSelectionSpan, consoleMotionStaysLocal } = sandbox;
assert(typeof consoleSelectionSpan === "function", "consoleSelectionSpan not extracted");
assert(typeof consoleMotionStaysLocal === "function", "consoleMotionStaysLocal not extracted");

// One row: columns 3..17 on buffer line 40 → select(3, 40, 14).
same(
  consoleSelectionSpan({ start: { x: 3, y: 40 }, end: { x: 17, y: 40 } }, 80),
  { column: 3, row: 40, length: 14 }
);
// Across rows: the span wraps through full 80-column rows in reading order.
same(
  consoleSelectionSpan({ start: { x: 70, y: 10 }, end: { x: 5, y: 12 } }, 80),
  { column: 70, row: 10, length: 2 * 80 - 65 }
);
// Nothing selected, or a collapsed/backwards position, restores nothing.
assert.strictEqual(consoleSelectionSpan(undefined, 80), null);
assert.strictEqual(consoleSelectionSpan({ start: { x: 5, y: 1 }, end: { x: 5, y: 1 } }, 80), null);
assert.strictEqual(consoleSelectionSpan({ start: { x: 9, y: 1 }, end: { x: 5, y: 1 } }, 80), null);

// Motion stays local only while text is selected AND no button is down: a drag
// in progress (button held) must keep reaching xterm's own selection handling,
// and with nothing selected the program keeps its hover.
assert.strictEqual(consoleMotionStaysLocal(0, true), true);
assert.strictEqual(consoleMotionStaysLocal(1, true), false);
assert.strictEqual(consoleMotionStaysLocal(0, false), false);

console.log("test_console_selection_guard: ok");
