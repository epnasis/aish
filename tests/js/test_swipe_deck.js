// Node-only, dependency-free check for the swipe "deck" ordering (#169): the
// horizontal carousel navigates a STABLE deck that is decoupled from the MRU
// order the sessions drawer uses, so replying to an open chat never reshuffles
// spatial memory. Pulls the REAL reconcileDeck out of app.js by marker and runs
// it in an isolated `vm` context — the shipped function, not a copy.
//
// Run manually: node tests/js/test_swipe_deck.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

const start = src.indexOf("// [DECK-START]");
const end = src.indexOf("// [DECK-END]", start);
assert(start !== -1 && end !== -1, "deck markers not found in app.js");

const sandbox = {};
vm.createContext(sandbox);
// vm only surfaces `var` (not const/let/function decls) as sandbox props.
vm.runInContext(
  src.slice(start, end).replace(/\bfunction reconcileDeck\b/, "var reconcileDeck = function reconcileDeck"),
  sandbox,
);
const { reconcileDeck } = sandbox;
assert(typeof reconcileDeck === "function", "failed to extract reconcileDeck");

// The pager delivers oldest→newest by last interaction. A "page" is {name,title,origin}.
const pages = (...names) => names.map((name) => ({ name, title: name.toUpperCase() }));

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}\n       ${err.message}`); }
}
const eq = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);

check("first reconcile adopts the incoming order", () => {
  eq(reconcileDeck([], pages("a", "b", "c")), ["a", "b", "c"]);
});

check("replying to an in-deck chat does NOT reorder the deck", () => {
  // Deck is a,b,c. The user replies to `a`, so the server re-sends the pager in
  // fresh MRU order (a is now newest → last): b,c,a. The deck must stay a,b,c.
  const deck = ["a", "b", "c"];
  eq(reconcileDeck(deck, pages("b", "c", "a")), ["a", "b", "c"]);
});

check("opening a not-in-deck chat from the drawer appends it to the end", () => {
  // Drawer-opening `d` makes it newest; incoming MRU is a,b,c,d. `d` is the only
  // newcomer, so it lands at the END of the deck — never in the middle.
  eq(reconcileDeck(["a", "b", "c"], pages("a", "b", "c", "d")), ["a", "b", "c", "d"]);
});

check("opening an already-in-deck chat navigates in place (no move)", () => {
  // Opening `b` (already in the deck) re-sorts MRU to a,c,b but the deck slot of
  // `b` is unchanged — the carousel navigates to its existing position.
  eq(reconcileDeck(["a", "b", "c"], pages("a", "c", "b")), ["a", "b", "c"]);
});

check("a new chat joins at the end of the deck", () => {
  eq(reconcileDeck(["a", "b"], pages("a", "b", "new-1")), ["a", "b", "new-1"]);
});

check("a chat that fell out of the pager window drops out of the deck", () => {
  // `b` aged past the 30-most-recent window: it is absent from the incoming
  // list, so it leaves the deck without disturbing the survivors' order.
  eq(reconcileDeck(["a", "b", "c"], pages("a", "c")), ["a", "c"]);
});

check("deck order is INDEPENDENT of the drawer's MRU order", () => {
  // The drawer would show this newest-first: c,b,a (its own MRU sort). The deck,
  // reconciled against the same pager, keeps join order a,b,c — proving the two
  // orderings are decoupled and one cannot churn the other.
  const incomingMru = pages("a", "b", "c"); // oldest→newest from the pager
  const deck = reconcileDeck([], incomingMru);
  const drawerOrder = [...incomingMru].reverse().map((p) => p.name); // newest-first
  eq(deck, ["a", "b", "c"]);
  eq(drawerOrder, ["c", "b", "a"]);
  assert.notStrictEqual(JSON.stringify(deck), JSON.stringify(drawerOrder), "deck must not equal drawer order");
});

check("reconcile is idempotent once converged (stable across repeat hellos)", () => {
  const once = reconcileDeck(["a", "b", "c"], pages("b", "c", "a"));
  const twice = reconcileDeck(once, pages("c", "a", "b"));
  eq(once, ["a", "b", "c"]);
  eq(twice, ["a", "b", "c"]);
});

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log("\nall checks passed");
