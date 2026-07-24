// Node-only, dependency-free check for the approval-card keyboard shortcuts
// (A = approve, D = deny). The global keydown handler resolves the *last still
// pending* card via activeApprovalCard(), so stacked cards clear bottom-up and
// an already-answered card (its Approve button disabled by answerCard) is
// skipped. Pulls the real activeApprovalCard() out of app.js by marker and runs
// it against a minimal fake DOM.
//
// Run manually: node tests/js/test_approval_shortcut.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

const start = src.indexOf("// The last approval card still awaiting");
const end = src.indexOf("// [ACTIVE-APPROVAL-CARD-END]", start);
assert(start !== -1 && end !== -1, "markers for activeApprovalCard not found");

// Fake card: querySelector("button.approve") returns an object whose `disabled`
// flag models answerCard() having resolved (or not) the card.
function fakeCard(id, disabled) {
  return {
    id,
    querySelector(sel) {
      if (sel === "button.approve") return { disabled, click() {} };
      return null;
    },
  };
}

let cards = [];
const sandbox = {
  messagesEl: {
    querySelectorAll(sel) {
      assert.strictEqual(sel, ".card");
      return cards;
    },
  },
};
vm.createContext(sandbox);
vm.runInContext(src.slice(start, end), sandbox);
assert(typeof sandbox.activeApprovalCard === "function", "failed to extract activeApprovalCard");

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

check("no cards: nothing to act on", () => {
  cards = [];
  assert.strictEqual(sandbox.activeApprovalCard(), null);
});

check("a card without an approve button is ignored", () => {
  cards = [{ id: "x", querySelector: () => null }];
  assert.strictEqual(sandbox.activeApprovalCard(), null);
});

check("single pending card is returned", () => {
  const c = fakeCard("only", false);
  cards = [c];
  assert.strictEqual(sandbox.activeApprovalCard(), c);
});

check("stacked cards clear newest-first (bottom-up)", () => {
  const older = fakeCard("older", false);
  const newer = fakeCard("newer", false);
  cards = [older, newer];
  assert.strictEqual(sandbox.activeApprovalCard(), newer);
});

check("resolved (disabled) newest card is skipped for the still-pending one", () => {
  const pending = fakeCard("pending", false);
  const resolved = fakeCard("resolved", true);
  cards = [pending, resolved];
  assert.strictEqual(sandbox.activeApprovalCard(), pending);
});

check("all resolved: none active", () => {
  cards = [fakeCard("a", true), fakeCard("b", true)];
  assert.strictEqual(sandbox.activeApprovalCard(), null);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall approval-shortcut checks passed");
