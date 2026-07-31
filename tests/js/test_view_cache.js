// Node-only, dependency-free checks for the rendered-transcript reuse cache
// ([VIEWCACHE] in app.js): a session switch replays the full transcript and
// used to rebuild the whole DOM every time, which made swiping back and forth
// between chats feel slow. The cache stashes a clean idle view's rendered
// nodes on the way out and swaps them back in when a replay with an IDENTICAL
// transcript fingerprint lands.
//
// What is pinned here:
//   - the fingerprint moves whenever the transcript does (append, different
//     last event, truncation) and stays put for an identical replay — the
//     match can make a switch faster, never wronger;
//   - a megabyte legacy `history` blob can't bloat the fingerprint (capped);
//   - only a CLEAN view stashes: anything dirty, busy, holding pending
//     approval cards, or painted from the offline mirror is never reused;
//   - the cache is bounded and MRU-ordered, so it can't grow with the deck.
//
// Run manually: node tests/js/test_view_cache.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);
const start = src.indexOf("// [VIEWCACHE-START]");
const end = src.indexOf("// [VIEWCACHE-END]");
assert(start !== -1 && end !== -1, "VIEWCACHE markers not found in app.js");

const sandbox = {
  // Globals the block reads when stashing — the app state around the view.
  currentSession: "",
  pendingCards: 0,
  clientBusy: false,
  pendingSends: [], // un-acknowledged send bubbles ([PENDING-SEND])
  offlineViewing: false,
  renderedAnswers: 0,
  messagesEl: { children: [] },
};
vm.createContext(sandbox);
vm.runInContext(src.slice(start, end), sandbox);
assert(typeof sandbox.replayFp === "function", "replayFp not extracted");
assert(typeof sandbox.viewStashable === "function", "viewStashable not extracted");
assert(typeof sandbox.stashCurrentView === "function", "stashCurrentView not extracted");

// let/const from the block live in the context's global lexical scope, not on
// the sandbox object — reach them with runInContext.
const inCtx = (code) => vm.runInContext(code, sandbox);

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}`);
    console.error(`       ${err.message}`);
  }
}

const { replayFp, viewStashable } = sandbox;
const ev = (type, extra) => Object.assign({ type }, extra);

check("identical replays share a fingerprint", () => {
  const a = { events: [ev("user", { text: "hi" }), ev("done", { result: "yo" })] };
  const b = { events: [ev("user", { text: "hi" }), ev("done", { result: "yo" })] };
  assert.strictEqual(replayFp(a), replayFp(b));
});

check("an appended event moves the fingerprint", () => {
  const base = [ev("user", { text: "hi" }), ev("done", { result: "yo" })];
  const grown = base.concat([ev("user", { text: "more" })]);
  assert.notStrictEqual(replayFp({ events: base }), replayFp({ events: grown }));
});

check("a different last event moves the fingerprint", () => {
  const a = { events: [ev("user", { text: "hi" }), ev("done", { result: "yes" })] };
  const b = { events: [ev("user", { text: "hi" }), ev("done", { result: "no" })] };
  assert.notStrictEqual(replayFp(a), replayFp(b));
});

check("truncation state is part of the fingerprint", () => {
  const events = [ev("done", { result: "yo" })];
  assert.notStrictEqual(
    replayFp({ events, truncated: true }),
    replayFp({ events, truncated: false })
  );
});

check("a huge legacy history blob can't bloat the fingerprint", () => {
  const blob = { events: [ev("history", { messages: [{ content: "x".repeat(2_000_000) }] })] };
  assert(replayFp(blob).length < 3000);
  // ...while total length still disambiguates beyond the cap.
  const other = { events: [ev("history", { messages: [{ content: "x".repeat(2_000_001) }] })] };
  assert.notStrictEqual(replayFp(blob), replayFp(other));
});

check("only a clean idle view is stashable", () => {
  const clean = {
    name: "s1", fp: "fp", dirty: false, pendingCards: 0,
    busy: false, offlineViewing: false,
  };
  assert(viewStashable(clean));
  assert(!viewStashable({ ...clean, name: "" }));           // nothing on screen
  assert(!viewStashable({ ...clean, fp: "" }));             // never replayed
  assert(!viewStashable({ ...clean, dirty: true }));        // events since replay
  assert(!viewStashable({ ...clean, pendingCards: 1 }));    // card mid-decision
  assert(!viewStashable({ ...clean, busy: true }));         // task running
  assert(!viewStashable({ ...clean, offlineViewing: true })); // mirror truncates
  // An un-acknowledged send bubble ([PENDING-SEND]) is DOM the server never
  // sent — filing it under a server fingerprint would cache a message that may
  // never have arrived as though it were transcript.
  assert(!viewStashable({ ...clean, pendingSends: 1 }));
});

check("stash stores the live nodes and the cache stays bounded, MRU first out", () => {
  inCtx("viewDirty = false");
  for (let i = 1; i <= 5; i++) {
    sandbox.currentSession = `s${i}`;
    sandbox.messagesEl = { children: [{ id: `node-${i}` }] };
    sandbox.renderedAnswers = i;
    inCtx(`viewFp = "fp-${i}"`);
    sandbox.stashCurrentView();
  }
  assert.strictEqual(inCtx("viewCache.size"), 4);
  assert(!inCtx("viewCache.has('s1')"), "oldest stash evicted");
  const s5 = inCtx("viewCache.get('s5')");
  assert.strictEqual(s5.fp, "fp-5");
  assert.strictEqual(s5.renderedAnswers, 5);
  assert.strictEqual(s5.nodes[0].id, "node-5");
});

check("a dirty view never stashes", () => {
  inCtx("viewCache.clear()");
  sandbox.currentSession = "dirty-one";
  inCtx("viewFp = 'fp-x'; viewDirty = true");
  sandbox.stashCurrentView();
  assert.strictEqual(inCtx("viewCache.size"), 0);
});

check("re-stashing a session replaces its entry and refreshes MRU order", () => {
  inCtx("viewCache.clear(); viewDirty = false");
  for (const name of ["a", "b", "a"]) {
    sandbox.currentSession = name;
    sandbox.messagesEl = { children: [] };
    inCtx(`viewFp = "fp-${name}"`);
    sandbox.stashCurrentView();
  }
  // JSON compare: the array comes from the vm realm (different Array proto).
  assert.strictEqual(JSON.stringify(inCtx("[...viewCache.keys()]")), '["b","a"]');
});

check("replayLanding: identical clean transcript no-ops (the reconnect case)", () => {
  const { replayLanding } = sandbox;
  const base = { fp: "f1", viewFp: "f1", viewDirty: false, hasDom: true, cachedFp: undefined };
  assert.strictEqual(replayLanding(base), "noop");
  // Any live event since the paint forces a rebuild — the dirty gate.
  assert.strictEqual(replayLanding({ ...base, viewDirty: true }), "rebuild");
  // An empty pane never no-ops, whatever the fingerprints say.
  assert.strictEqual(replayLanding({ ...base, hasDom: false }), "rebuild");
  // A different transcript rebuilds (or reuses a matching stash).
  assert.strictEqual(replayLanding({ ...base, fp: "f2" }), "rebuild");
  assert.strictEqual(replayLanding({ ...base, fp: "f2", cachedFp: "f2" }), "reuse");
  // A stash with a stale fingerprint is never reused.
  assert.strictEqual(replayLanding({ ...base, fp: "f2", cachedFp: "f1" }), "rebuild");
  // A bubble the server has never confirmed ([PENDING-SEND]) means the DOM is
  // NOT this replay's transcript however well the fingerprint matches — and the
  // fingerprint cannot see it, because nothing about it came off the socket.
  assert.strictEqual(replayLanding({ ...base, pendingSends: 1 }), "rebuild");
  // No fingerprint at all (empty replay edge) always rebuilds.
  assert.strictEqual(
    replayLanding({ fp: "", viewFp: "", viewDirty: false, hasDom: true }), "rebuild"
  );
});

check("transcript-affecting events are not in the safe list", () => {
  for (const type of ["user", "step", "done", "stream", "error", "token",
                      "command_start", "approval_request"]) {
    assert(!inCtx(`VIEW_SAFE_EVENTS.has(${JSON.stringify(type)})`),
      `${type} must dirty the view`);
  }
  for (const type of ["session_list", "model_list", "role", "console_out"]) {
    assert(inCtx(`VIEW_SAFE_EVENTS.has(${JSON.stringify(type)})`),
      `${type} must not dirty the view`);
  }
});

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("all view-cache checks passed");
