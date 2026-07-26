// Node-only, dependency-free checks for "phantom pages": working-set deck
// members whose session file no longer exists (deleted from the drawer, or from
// another device entirely — the deck is per-device localStorage and outlives
// both). A phantom used to swallow the swipe aimed at it: the committed resume
// answered a bare error, no replay ever landed, the watchdog restored the view
// and the gesture looked simply ignored, forever, on every swipe.
//
// What is pinned here:
//   - a resume that fails with code "no_such_session" prunes the phantom AND
//     leaves the transcript ON SCREEN (the pager's one invariant), and the next
//     swipe reaches a real chat;
//   - pruning consumes ONLY server ground truth — never absence from the
//     30-item pager window, which a 478-file state dir overflows 15x over (the
//     cure-worse-than-disease case);
//   - deleting a chat drops it from the deck (where phantoms were born);
//   - the ✕ (removeFromWorkingSet) still means "to HISTORY", not delete, and
//     still records curation;
//   - offline, nothing prunes: a mirror-only chat stays swipe-reachable.
//
// Same hostile harness as test_pager_unpark.js: transitions never settle, rAF
// never fires — recovery may not depend on an animation running.
//
// Run manually: node tests/js/test_deck_phantom.js
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
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

// vm surfaces top-level `var` as sandbox props but not function decls in strict
// scripts — same rewrite as test_swipe_deck.js, so the REAL functions ship.
const surface = (code) => code.replace(/\nfunction (\w+)/g, "\nvar $1 = function $1");

let checks = 0;
function ok(label, cond) { assert(cond, label); checks += 1; }

// Hostile element + full app surface the deck/pager code touches. `computed`
// tracked apart from the inline style, exactly as in test_pager_unpark.js.
function world({ visible = false, offline = false, mirror = [] } = {}) {
  const el = {
    style: { _t: "", _tr: "",
      get transform() { return this._t; },
      set transform(v) {
        this._t = v;
        if (!this._tr || this._tr === "none") el._computed = v || "none";
      },
      get transition() { return this._tr; },
      set transition(v) { this._tr = v; } },
    _computed: "none",
    get offsetWidth() { return 1100; },
    clientWidth: 1100,
  };
  const timers = [];
  const mirrored = new Set(mirror);
  const sandbox = {
    messagesEl: el,
    swipeInFrom: 0,
    document: { visibilityState: visible ? "visible" : "hidden" },
    getComputedStyle: () => ({ transform: el._computed }),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (id) timers[id - 1] = null; },
    // app state + recorders
    deck: [],
    deckCurated: false,
    currentSession: null,
    lastSessionEvent: null,
    offlineMode: offline,
    _sent: [], _toasts: [], _saves: 0, _resumes: [], _newChats: 0,
    send(m) { sandbox._sent.push(m); return true; },
    showToast(t) { sandbox._toasts.push(t); },
    saveDeck() { sandbox._saves += 1; },
    renderSessions() {},
    resumeSession(name) { sandbox._resumes.push(name); },
    requestNewChat() { sandbox._newChats += 1; },
    openCachedSession: (name) => Promise.resolve(mirrored.has(name)),
    $: () => ({ hidden: true }),
  };
  vm.createContext(sandbox);
  for (const [a, b] of [
    ["// [UNPARK-START]", "// [UNPARK-END]"],
    ["// [DECK-START]", "// [DECK-END]"],
    ["// PAGER_LANE_START", "// PAGER_LANE_END"],
    ["function removeFromWorkingSet(name) {", "// [DECK-GONE-START]"],
    ["// [DECK-GONE-START]", "// [DECK-GONE-END]"],
    ["function commitPage(direction, target, width) {", "\nfunction snapBack"],
  ]) vm.runInContext(surface(extract(a, b)), sandbox);
  sandbox._settle = () => { el._computed = el.style.transform || "none"; };
  sandbox._el = el;
  return sandbox;
}

const AT_REST = (w) => !w.transcriptDisplaced();
const member = (name) => ({ name, ts: Date.now(), title: name, origin: "user" });
const page = (name) => ({ name, title: name, origin: "user" });
const names = (rows) => rows.map((r) => r.name);
const eq = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);

// ---- 1. THE PHANTOM SWIPE: fails visibly once, then never again -----------
// Deck [A, P, B]; P was deleted on another device, so the server's pager only
// lists A and B — yet deckToPages keeps P as a page (the cached fallback that
// makes beyond-the-window chats reachable). currentSession = A.
{
  const w = world({ visible: false });
  w.deck = [member("A"), member("P"), member("B")];
  w.currentSession = "A";
  const source = [page("A"), page("B")];
  const pages = w.deckToPages(w.deck, source);
  const target = w.laneNeighbor(pages, "A", 1);
  ok("the phantom IS the neighbor page (the trap this test exists for)", target.name === "P");

  // The swipe commits: resume sent, transcript parked off-screen.
  w.commitPage(1, target, 1100);
  eq(w._sent, [{ type: "resume", path: "P" }], "commit sends the resume");
  ok("committed: transcript parked in flight", w.transcriptDisplaced());

  // The server answers with the structured miss → the phantom is pruned and
  // the view is restored NOW (owner-delegated), not at the watchdog deadline.
  w.onSessionGone("P");
  ok("phantom pruned from the deck", names(w.deck).join(",") === "A,B");
  ok("prune is persisted", w._saves >= 1);
  ok("the transcript is ON SCREEN — the invariant", AT_REST(w));
  ok("failure surfaced, not silent", w._toasts.length === 1);
  ok("prune is not curation — the ✕'s flag stays untouched", w.deckCurated === false);

  // The SECOND swipe reaches a real chat.
  const target2 = w.laneNeighbor(w.deckToPages(w.deck, source), "A", 1);
  ok("second swipe targets the real neighbor", target2.name === "B");
  w.commitPage(1, target2, 1100);
  eq(w._sent[1], { type: "resume", path: "B" }, "second resume goes out");
  w.reconcilePager(w.swipeInFrom); // the landing replay
  ok("landing ends at rest", AT_REST(w));
}

// ---- 2. Pruning never trusts a capped listing ------------------------------
// 40 deck members, a 30-page pager window (the 478-file state dir in
// miniature): absence from the window is NOT deletion evidence.
{
  const w = world();
  const all = Array.from({ length: 40 }, (_, i) => member(`s${i}`));
  w.deck = all.slice();
  w.currentSession = "s39";
  const source = all.slice(10).map((m) => page(m.name)); // "top 30" only
  ok("cached fallback keeps every member a page",
    w.deckToPages(w.deck, source).length === 40);
  eq(names(w.pruneDeck(w.deck, [], "s39")), names(all),
    "an empty ground-truth verdict prunes nothing");
  // reconcileDeck hands the server the WHOLE deck (minus the page on screen)
  // and lets stat() decide — the client never infers from a listing.
  w.reconcileDeck();
  eq(w._sent, [{ type: "deck_check", names: names(all.slice(0, 39)) }],
    "deck_check asks about every member except the chat on screen");
  w.dropGoneSessions(["s3", "s25"]); // the server's verdict, window-independent
  ok("only the named members go", w.deck.length === 38 &&
    !names(w.deck).includes("s3") && !names(w.deck).includes("s25"));
  const gone = extract("// [DECK-GONE-START]", "// [DECK-GONE-END]");
  ok("pruning code never consults the pager listing",
    !/pagerSource|pagerSessions|PAGER_LIMIT/.test(gone));
  ok("the page on screen is never pruned",
    names(w.pruneDeck(w.deck, ["s39"], "s39")).includes("s39"));
}

// ---- 3. Deleting a chat drops it from the deck (phantom birthplace) --------
{
  const w = world();
  w.deck = [member("A"), member("B"), member("C")];
  w.currentSession = "A";
  w.onSessionDeleted({ name: "B" });
  eq(names(w.deck), ["A", "C"], "the deleted chat leaves the deck");
  ok("deletion is not curation", w.deckCurated === false);
  ok("the delete still announces itself", w._toasts.includes("session deleted"));
}

// ---- 4. The ✕ still means "to HISTORY", never delete -----------------------
{
  const w = world();
  w.deck = [member("A"), member("B")];
  w.currentSession = "A";
  w.removeFromWorkingSet("B");
  eq(names(w.deck), ["A"], "✕ removes from the working set");
  ok("✕ records curation", w.deckCurated === true);
  ok("✕ never sends a delete", !w._sent.some((m) => m.type === "delete_session"));
  const x = extract("function removeFromWorkingSet(name) {", "// [DECK-GONE-START]");
  ok("removeFromWorkingSet cannot delete by construction", !/delete_session/.test(x));
}

// ---- 5. Offline, nothing prunes --------------------------------------------
// The mirror legitimately serves chats the server no longer has; and a chat
// missing from the mirror may simply have never synced. Both stay members.
{
  const w = world({ offline: true, mirror: ["M"] });
  w.deck = [member("A"), member("M"), member("X")];
  w.currentSession = "A";
  // A mirror-only chat pages in from the mirror, deck untouched.
  // openCachedSession resolves asynchronously; setImmediate lets it land.
  w.commitPage(1, page("M"), 1100);
  setImmediate(() => {
    ok("mirror-only page keeps its deck slot", names(w.deck).includes("M"));
    // A chat in neither place: the gesture degrades to "no page", still no prune.
    w.commitPage(1, page("X"), 1100);
    setImmediate(() => {
      ok("offline miss restores the view", AT_REST(w));
      ok("offline miss never prunes — absence from the mirror is not deletion",
        names(w.deck).includes("X"));
      ok("offline sends nothing", w._sent.length === 0);
      finished = true;
      console.log(`${checks} ok — all checks passed`);
    });
  });
}

// The offline checks run in setImmediate callbacks; a harness change that lets
// the process drain the loop before they run must fail loudly, not pass empty.
let finished = false;
process.on("exit", (code) => {
  if (code === 0) assert(finished, "the async offline checks never ran");
});
