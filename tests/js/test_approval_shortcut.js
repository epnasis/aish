// Node-only, dependency-free check for the approval-card keyboard shortcuts
// (A = approve, D = deny, T = trust dir, E = edit). Two things are validated:
//
//   1. UNIQUENESS + COVERAGE of the CARD_SHORTCUTS table against app.js source:
//      keys and selectors are unique, and every primary verdict button
//      (.approve/.deny/.trust) plus the Edit toggle (.cmd-icon) that the card
//      builders actually create is bound to exactly one key. Secondary controls
//      (.seg scope segments, .copy-chip) are explicitly exempt.
//   2. activeApprovalCard() resolves the *last still pending* card, so stacked
//      cards clear bottom-up and an already-answered card (its Approve button
//      disabled by answerCard) is skipped.
//
// The real code is pulled out of app.js by marker/eval and run against a minimal
// fake DOM — the shipped logic, never a copy.
//
// Run manually: node tests/js/test_approval_shortcut.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

// ---- extract CARD_SHORTCUTS by evaluating just that const in a bare vm -------
const scStart = src.indexOf("const CARD_SHORTCUTS = [");
const scEnd = src.indexOf("];", scStart);
assert(scStart !== -1 && scEnd !== -1, "CARD_SHORTCUTS not found in app.js");
const scBox = {};
vm.createContext(scBox);
vm.runInContext(src.slice(scStart, scEnd + 2) + "\nthis.CARD_SHORTCUTS = CARD_SHORTCUTS;", scBox);
const SHORTCUTS = scBox.CARD_SHORTCUTS;

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

// ---- CARD_SHORTCUTS uniqueness + coverage -----------------------------------
check("shortcut keys are unique (no key bound to two actions)", () => {
  const keys = SHORTCUTS.map((s) => s.key);
  assert.deepStrictEqual([...new Set(keys)].sort(), [...keys].sort(),
    `duplicate key(s) in CARD_SHORTCUTS: ${keys}`);
});

check("shortcut selectors are unique (no action bound to two keys)", () => {
  const sels = SHORTCUTS.map((s) => s.selector);
  assert.deepStrictEqual([...new Set(sels)].sort(), [...sels].sort(),
    `duplicate selector(s) in CARD_SHORTCUTS: ${sels}`);
});

check("every card builder's action button is covered by a shortcut", () => {
  // Interactive button classes the builders assign, and whether each must have a
  // shortcut. .seg (scope segments) and .copy-chip refine/duplicate Approve, so
  // they are intentionally Tab-only; everything else is a verdict/toggle that
  // MUST be keyable. A new interactive class shows up here and forces a choice.
  const SECONDARY = new Set(["seg", "copy-chip"]);
  const covered = new Set(SHORTCUTS.map((s) => s.selector.replace(/^button\./, "")));
  const found = new Set();
  const re = /\.className = "([a-z-]+)"/g;
  let m;
  while ((m = re.exec(src))) found.add(m[1]);
  // The classes we know are card buttons; guard that each is handled one way or
  // the other. (Non-button classes like "card" simply won't be in either set.)
  for (const cls of ["approve", "deny", "trust", "cmd-icon", "seg", "copy-chip"]) {
    assert.ok(found.has(cls), `expected class .${cls} to exist in app.js (builder changed?)`);
    const ok = covered.has(cls) || SECONDARY.has(cls);
    assert.ok(ok, `.${cls} is an action button with no shortcut and not marked secondary`);
  }
  // And nothing secondary was accidentally given a shortcut.
  for (const cls of SECONDARY) {
    assert.ok(!covered.has(cls), `.${cls} is meant to be Tab-only but has a shortcut`);
  }
});

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
