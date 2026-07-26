// Node-only, dependency-free checks for the working-set "deck" model (#169):
// the swipe carousel + drawer RECENT section navigate a STABLE, explicit ordered
// set, decoupled from the server's MRU order. Interacting with an open chat never
// reshuffles it; stale pruning happens ONLY at cold start. Pulls the REAL pure
// functions out of app.js by marker and runs them in an isolated `vm` context —
// the shipped code, not a copy.
//
// Run manually: node tests/js/test_swipe_deck.js
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
  assert(start !== -1 && end !== -1, `${startMarker} markers not found in app.js`);
  const sandbox = {};
  vm.createContext(sandbox);
  // vm surfaces top-level `var` as sandbox props but not const/function decls in
  // strict scripts, so rewrite the function declarations we need to read out.
  const snippet = src.slice(start, end).replace(/\nfunction (\w+)/g, "\nvar $1 = function $1");
  vm.runInContext(snippet, sandbox);
  return sandbox;
}

const deckApi = extract("// [DECK-START]", "// [DECK-END]");
const {
  addToDeck, removeFromDeck, touchDeck, recomputeWorkingSet,
  sweepWorkingSet, seedWorkingSet, partitionRecent, deckToPages,
} = deckApi;
const { swipeUpOpensDrawer } = extract("// [SWIPEUP-START]", "// [SWIPEUP-END]");
const { searchbarSwipeDismisses } = extract("// [SEARCHDISMISS-START]", "// [SEARCHDISMISS-END]");

for (const [name, fn] of Object.entries({ addToDeck, removeFromDeck, touchDeck,
  recomputeWorkingSet, sweepWorkingSet, seedWorkingSet, partitionRecent, deckToPages,
  swipeUpOpensDrawer, searchbarSwipeDismisses })) {
  assert(typeof fn === "function", `failed to extract ${name}`);
}

const HOUR = 60 * 60 * 1000;
const NOW = 1_000 * HOUR;
const member = (name, ageHours = 0) => ({ name, ts: NOW - ageHours * HOUR, title: name, origin: "user" });
const names = (rows) => rows.map((r) => r.name);

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}\n       ${err.message}`); }
}
const eq = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);

// ---- insertion / removal rules -------------------------------------------

check("new chat appends to the far right of the deck", () => {
  const deck = [member("a"), member("b")];
  eq(names(addToDeck(deck, member("new-1"), null)), ["a", "b", "new-1"]);
});

check("fork inserts immediately RIGHT of its parent (parent stays put)", () => {
  const deck = [member("a"), member("b"), member("c")];
  eq(names(addToDeck(deck, member("b-fork"), "b")), ["a", "b", "b-fork", "c"]);
});

check("opening a not-in-deck chat from history appends far right", () => {
  const deck = [member("a"), member("b")];
  eq(names(addToDeck(deck, member("d"), null)), ["a", "b", "d"]);
});

check("opening an already-in-deck chat navigates in place (no move, no dupe)", () => {
  const deck = [member("a"), member("b"), member("c")];
  eq(names(addToDeck(deck, member("b"), null)), ["a", "b", "c"]);
  // even a fork-style insert of an existing member is a no-op
  eq(names(addToDeck(deck, member("a"), "c")), ["a", "b", "c"]);
});

check("fork with a missing parent falls back to far right", () => {
  const deck = [member("a")];
  eq(names(addToDeck(deck, member("x"), "ghost")), ["a", "x"]);
});

check("removing a member drops it without disturbing the rest", () => {
  const deck = [member("a"), member("b"), member("c")];
  eq(names(removeFromDeck(deck, "b")), ["a", "c"]);
});

check("touch bumps a member's ts WITHOUT reordering (reply = no reshuffle)", () => {
  const deck = [member("a", 10), member("b", 5), member("c", 1)];
  const bumped = touchDeck(deck, "a", NOW);
  eq(names(bumped), ["a", "b", "c"]);               // order untouched
  assert.strictEqual(bumped[0].ts, NOW, "a's ts should be refreshed");
});

// ---- cold-start lifecycle -------------------------------------------------

check("cold-start recompute evicts members idle > 48h, keeps < 48h", () => {
  const deck = [member("fresh", 5), member("stale", 60), member("edge", 47)];
  eq(names(recomputeWorkingSet(deck, NOW)), ["fresh", "edge"]);
});

check("cold-start recompute NEVER adds — it only prunes", () => {
  const deck = [member("a", 1), member("b", 2)];
  // Recompute takes ONLY the current members; a session absent from the deck
  // (a ✕-removed one) has no way back in here.
  eq(names(recomputeWorkingSet(deck, NOW)), ["a", "b"]);
});

check("a ✕-removed session stays out across a cold-start recompute", () => {
  let deck = [member("a", 1), member("keep", 1), member("b", 1)];
  deck = removeFromDeck(deck, "keep");             // user ✕-removed it
  deck = recomputeWorkingSet(deck, NOW);           // next cold start
  eq(names(deck), ["a", "b"]);                      // never reappears (no auto-add)
});

check("seed adopts <48h sessions oldest-active first, drops the stale", () => {
  // A session's .ts is epoch SECONDS (the server's unit); seedWorkingSet converts
  // to ms internally. NOW is ms, so session stamps are (ms age)/1000.
  const sec = (ageHours) => (NOW - ageHours * HOUR) / 1000;
  const sessions = [
    { name: "old", ts: sec(40), title: "old", origin: "user" },
    { name: "newest", ts: sec(1), title: "newest", origin: "user" },
    { name: "mid", ts: sec(10), title: "mid", origin: "user" },
    { name: "ancient", ts: sec(100), title: "ancient", origin: "user" },
  ];
  const seeded = seedWorkingSet(sessions, NOW);
  eq(names(seeded), ["old", "mid", "newest"]); // stale dropped, oldest→newest
  // seeded members carry ms stamps, so a same-instant recompute keeps them all
  eq(names(recomputeWorkingSet(seeded, NOW)), ["old", "mid", "newest"]);
});

// ---- drawer split: RECENT (working set) vs HISTORY (MRU rest) --------------

check("partitionRecent puts deck members in RECENT (deck order), rest in HISTORY", () => {
  const deck = [member("b"), member("a")];          // deliberately not MRU order
  const sessions = [                                // server MRU (newest first)
    { name: "c", ts: 3 }, { name: "a", ts: 2 }, { name: "b", ts: 1 }, { name: "d", ts: 0 },
  ];
  const { recent, history } = partitionRecent(deck, sessions);
  eq(names(recent), ["b", "a"]);                    // RECENT follows the deck, not MRU
  eq(names(history), ["c", "d"]);                   // HISTORY keeps the server's MRU order
});

check("no duplication: a session is in exactly one of RECENT / HISTORY", () => {
  const deck = [member("a")];
  const sessions = [{ name: "a", ts: 2 }, { name: "b", ts: 1 }];
  const { recent, history } = partitionRecent(deck, sessions);
  const all = [...names(recent), ...names(history)];
  eq(all.slice().sort(), ["a", "b"]);
  assert.strictEqual(new Set(all).size, all.length, "a name must not appear twice");
});

check("drawer HISTORY order is the server MRU, independent of deck order", () => {
  const deck = [member("a"), member("b")];          // deck order a,b
  const mru = [{ name: "z", ts: 9 }, { name: "y", ts: 8 }, { name: "a", ts: 2 }];
  const { history } = partitionRecent(deck, mru);
  eq(names(history), ["z", "y"]);                   // untouched MRU; deck can't churn it
});

// ---- pager mapping: deck order drives the swipe, metadata stays live ------

check("deckToPages returns deck order with metadata refreshed from the source", () => {
  const deck = [member("b"), member("a")];
  const source = [{ name: "a", title: "A!", origin: "user" }, { name: "b", title: "B!", origin: "user" }];
  const pages = deckToPages(deck, source);
  eq(names(pages), ["b", "a"]);                     // deck order
  eq(pages.map((p) => p.title), ["B!", "A!"]);      // titles refreshed from the live source
});

check("deckToPages falls back to the raw source when the deck is empty", () => {
  const source = [{ name: "x", title: "x", origin: "user" }];
  eq(deckToPages([], source), source);              // unseeded/offline: never worse than before
});

// ---- swipe-up decision seam ----------------------------------------------

const upGesture = (over = {}) => Object.assign({
  startY: 780, endY: 700, dx: 4, viewportH: 800, keyboardUp: false,
}, over);

check("an upward swipe from the bottom zone opens the drawer", () => {
  assert.strictEqual(swipeUpOpensDrawer(upGesture()), true);
});

check("a swipe starting too high (outside the bottom zone) does NOT open it", () => {
  assert.strictEqual(swipeUpOpensDrawer(upGesture({ startY: 400 })), false);
});

check("a mostly-horizontal swipe does NOT open it (pager's axis wins)", () => {
  assert.strictEqual(swipeUpOpensDrawer(upGesture({ dx: 120 })), false);
});

check("a downward swipe does NOT open it", () => {
  assert.strictEqual(swipeUpOpensDrawer(upGesture({ endY: 790 })), false);
});

check("a tiny flick does NOT open it", () => {
  assert.strictEqual(swipeUpOpensDrawer(upGesture({ endY: 760 })), false);
});

check("keyboard up suppresses it (composer owns the gesture)", () => {
  assert.strictEqual(swipeUpOpensDrawer(upGesture({ keyboardUp: true })), false);
});

check("still opens when the transcript is scrolled up — the composer zone is the disambiguator, not scroll position", () => {
  // regression: an earlier atBottom gate made this fire only at the very bottom
  assert.strictEqual(swipeUpOpensDrawer(upGesture()), true);
});

check("an up-swipe on the search bar dismisses the Sessions view", () => {
  assert.strictEqual(searchbarSwipeDismisses({ startY: 700, endY: 640, dx: 4 }), true);
});

check("a downward swipe on the search bar does NOT dismiss", () => {
  assert.strictEqual(searchbarSwipeDismisses({ startY: 700, endY: 760, dx: 4 }), false);
});

check("a short up-flick on the search bar does NOT dismiss", () => {
  assert.strictEqual(searchbarSwipeDismisses({ startY: 700, endY: 680, dx: 4 }), false);
});

check("a mostly-horizontal swipe on the search bar does NOT dismiss", () => {
  assert.strictEqual(searchbarSwipeDismisses({ startY: 700, endY: 640, dx: 90 }), false);
});

// ---- the sweep must not strand the deck ----------------------------------
// Regression: coming back after longer than the eviction window aged out every
// member at once. The deck emptied but kept `seeded`, so maybeSeedDeck returned
// early forever — the working set then only ever held chats you happened to
// open, and swiping between chats did nothing until you opened the drawer.
check("sweeping away every member re-opens seeding", () => {
  const out = sweepWorkingSet([member("a", 70), member("b", 90)], NOW, true);
  assert.deepStrictEqual(out.members, []);
  assert.strictEqual(out.seeded, false, "an all-evicted deck must be re-seedable");
});

check("a partial sweep keeps the deck seeded", () => {
  const out = sweepWorkingSet([member("a", 70), member("fresh", 1)], NOW, true);
  assert.deepStrictEqual(out.members.map((m) => m.name), ["fresh"]);
  assert.strictEqual(out.seeded, true, "survivors mean the working set is still yours");
});

check("an already-empty deck keeps its flag — ✕ to zero is a choice, not staleness", () => {
  const out = sweepWorkingSet([], NOW, true);
  assert.deepStrictEqual(out.members, []);
  assert.strictEqual(out.seeded, true);
});

check("sweeping never sets seeded on an unseeded deck", () => {
  assert.strictEqual(sweepWorkingSet([member("a", 1)], NOW, false).seeded, false);
  assert.strictEqual(sweepWorkingSet([], NOW, false).seeded, false);
});

check("the sweep still evicts exactly what recomputeWorkingSet evicts", () => {
  const members = [member("a", 70), member("fresh", 1), member("edge", 47)];
  assert.deepStrictEqual(
    sweepWorkingSet(members, NOW, true).members,
    recomputeWorkingSet(members, NOW),
  );
});

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log("\nall checks passed");
