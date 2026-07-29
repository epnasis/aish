// Choreography pins for session identity (issue #181, phase 2).
//
// "Which conversation am I looking at" is the one piece of client state every
// other subsystem reads: the pager, the deck, the offline mirror, the pin
// toggle, the URL, the reconnect. It used to change in FOUR hand-rolled
// variants, each writing a different SUBSET of the facts coupled to it — and a
// subset is invisible until the one fact a path forgot is the one something
// else reads. Phase 2 gave it one owner, `enterSession()` in [SESSION-ENTER].
//
// These scenarios drive the REAL owner from the REAL callers (onHello,
// commitPage's prefetch branch, offlineFirstPaint) in the hostile world, at the
// interleavings that produced the bugs:
//
//   (a) a prefetched swipe moves identity AHEAD of the hello on purpose — the
//       hello that follows must not undo it, and a hello for another chat must;
//   (b) the boot mirror paint racing the socket, in both outcomes — including
//       the subset-drift assertion that would have caught the divergence;
//   (c) private mode: a storage that throws must cost the remembered session
//       and NOTHING else;
//   (d) a fork's deck anchor is a bounded promise, not something the next
//       unrelated hello may throw away.
//
// Run manually: node tests/js/test_choreo_session_switch.js
"use strict";

const { sessionWorld, checks, expectReached } = require("./harness");

const { ok, report } = checks();

const hello = (session, extra = {}) => ({
  session,
  title: session,
  model: "test-model",
  pager: [],
  cmd_history: [],
  busy: false,
  log_path: `/logs/${session}`,
  ...extra,
});

// onHello and commitPage are extracted by their own source markers: what runs
// here is the shipped function, not a paraphrase of it.
const ON_HELLO = ["function onHello(event) {", "\n// Multi-connection (#102)"];
const COMMIT_PAGE = ["function commitPage(direction, target, width) {", "\nfunction snapBack"];
const FIRST_PAINT = ["async function offlineFirstPaint() {", "\n// ---- offline: pinning"];

// ---- (a) prefetch, then the hello that must not undo it -------------------
// The prefetched paint is the whole reason identity moves early: the
// authoritative replay then lands on the SAME fingerprint and no-ops, so a
// swipe renders once instead of twice. That is a property of two functions
// agreeing, which is exactly the kind of thing a unit test cannot see.
{
  const w = sessionWorld();
  w.load("// [VIEWCACHE-START]", "// [VIEWCACHE-END]"); // real replayFp/replayLanding
  w.load("// [UNPARK-START]", "// [UNPARK-END]");
  w.load("// [PREFETCH-START]", "// [PREFETCH-END]");
  w.load(...COMMIT_PAGE);
  w.load(...ON_HELLO);
  const s = w.sandbox;

  s.onHello(hello("A"));
  ok("a hello enters its session", s.currentSession === "A");
  const events = [{ type: "user", text: "hi" }, { type: "done", text: "hello" }];
  s.viewFp = s.replayFp({ events, truncated: false }); // A's replay landed
  s.viewDirty = false;

  // The neighbor is warm, and the swipe commits.
  s.onPeek({ name: "B", events, truncated: false });
  s.commitPage(1, { name: "B", title: "B" }, 1100);

  ok("the swipe asked the server for the page",
    JSON.stringify(w.lastCall("send").args[0]) === JSON.stringify({ type: "resume", path: "B" }));
  ok("identity moved before the hello — the point of the prefetch",
    s.currentSession === "B");
  ok("the outgoing chat's fingerprint did not survive the switch", s.viewFp === "");
  ok("…and every coupled fact followed identity in one step",
    w.urlSession() === "B" && w.remembered() === "B" && w.seen.title === "B");
  ok("the prefetched events were painted", w.called("onReplay") === 1);

  // What onReplay does at the end of that paint.
  const fpB = s.replayFp({ events, truncated: false });
  s.viewFp = fpB;
  s.viewDirty = false;

  // The authoritative hello for the SAME session arrives.
  s.onHello(hello("B"));
  ok("a same-session hello leaves the fingerprint alone",
    s.viewFp === fpB && s.viewDirty === false);
  ok("…so its replay lands as a no-op instead of rendering twice",
    s.replayLanding({ fp: fpB, viewFp: s.viewFp, viewDirty: s.viewDirty, hasDom: true })
      === "noop");

  // …and the other direction: a hello for a DIFFERENT chat must reset, or a
  // fingerprint collision could keep the wrong DOM under the new name.
  s.onHello(hello("C"));
  ok("a cross-session hello resets the fingerprint", s.viewFp === "" && s.viewDirty === true);
  ok("…so the same transcript can no longer 'noop' onto another chat",
    s.replayLanding({ fp: fpB, viewFp: s.viewFp, viewDirty: s.viewDirty, hasDom: true })
      === "rebuild");
}

// ---- (c) private mode: the throw costs one fact, not the transition -------
// The unguarded write sat in the MIDDLE of the hello, so a private-mode throw
// took the workspace render, the busy state and the boot-loader hide with it —
// the app looked stuck behind a spinner for a reason nothing announced.
{
  const w = sessionWorld({ storageThrows: true });
  w.load(...ON_HELLO);
  w.sandbox.onHello(hello("A", { busy: true }));

  ok("the session is still entered", w.sandbox.currentSession === "A");
  ok("the URL still follows it", w.urlSession() === "A");
  ok("only the remembered session is lost", w.remembered() === null);
  ok("the rest of the hello still ran: busy state", w.seen.busy === true);
  ok("…the workspace", w.called("renderWorkspace") === 1);
  ok("…the deck", w.called("ensureCurrentInDeck") === 1);
  ok("…and the boot loader is gone", w.seen.booted === true);
}

// ---- (d) a fork's deck anchor is a bounded promise ------------------------
// The intent used to be cleared by EVERY hello. A reconnect racing the child's
// hello therefore threw the anchor away silently and the fork landed at the far
// right, next to nothing.
{
  const w = sessionWorld({
    globals: { offlineMeta: new Map(), lastSessionEvent: null, renderSessions() {} },
  });
  w.load("// [DECK-START]", "// [DECK-END]");   // pure ordering
  w.load("// [DECK-STATE-START]", "// [DECK-STATE-END]"); // the live deck + the intent
  w.load(...ON_HELLO);
  const s = w.sandbox;
  const names = () => s.deck.map((m) => m.name).join(",");

  s.deck = [
    { name: "A", ts: Date.now(), title: "A", origin: "user" },
    { name: "Z", ts: Date.now(), title: "Z", origin: "user" },
  ];
  s.deckSeeded = true;
  s.onHello(hello("A"));
  ok("the chat on screen is an existing member", names() === "A,Z");

  // Fork from A. A reconnect for the SAME chat beats the child's hello.
  s.noteForkIntent();
  ok("the intent names the parent", s.pendingForkParent.name === "A");
  s.onHello(hello("A"));
  ok("an unrelated hello no longer clears the fork intent", s.pendingForkParent !== null);
  ok("…and did not reorder the deck", names() === "A,Z");

  // Now the child.
  s.onHello(hello("A-fork"));
  ok("the child lands right of its parent", names() === "A,A-fork,Z");
  ok("the intent is consumed by the adoption it was made for",
    s.pendingForkParent === null);

  // A fork whose child never comes: the promise expires, it is not inherited by
  // whatever chat is adopted next. (Rewinding the stamp IS letting the window
  // pass — the deadline is checked when read, not by a timer.)
  s.noteForkIntent();
  s.pendingForkParent.at -= s.FORK_INTENT_MS + 1000;
  s.onHello(hello("N"));
  ok("an expired intent anchors nothing — the newcomer goes far right",
    names() === "A,A-fork,Z,N");
  ok("…and the expired intent is gone", s.pendingForkParent === null);
}

// ---- (b) the boot race: mirror paint vs. the socket ------------------------
// Async, so it runs last; expectReached fails the file if it never lands.
const done = expectReached("the boot-race scenario never ran");
const settle = () => new Promise((resolve) => setImmediate(resolve));

const MIRRORED = {
  meta: { name: "M.jsonl", title: "Mirror chat" },
  events: [{ type: "user", text: "read offline" }],
};

function bootWorld() {
  const w = sessionWorld({
    globals: {
      offlineLoad: (name) => Promise.resolve(name === "M.jsonl" ? MIRRORED : null),
      offlineList: () => Promise.resolve([]),
    },
  });
  w.load(...FIRST_PAINT);
  w.storage.map.set("aish-session", "M.jsonl"); // the chat this device was on
  return w;
}

(async () => {
  // The socket wins: the authoritative replay is already up, so the mirror must
  // not repaint over it NOR move identity out from under it.
  {
    const w = bootWorld();
    w.sandbox.offlineFirstPaint();
    await settle();
    ok("the first paint waits behind a real deadline",
      w.armed((t) => t.ms === w.sandbox.FIRST_PAINT_GRACE_MS).length === 1);
    w.sandbox.serverPainted = true; // the replay landed inside the grace window
    w.fire((t) => t.ms === w.sandbox.FIRST_PAINT_GRACE_MS);
    await settle();
    ok("a beaten mirror paints nothing", w.called("onReplay") === 0);
    ok("…and moves no identity", w.sandbox.currentSession === null);
    ok("…and touches neither the URL nor the pin toggle",
      w.urlSession() === null && w.called("refreshOfflinePinUi") === 0);
  }

  // The mirror wins: it is now a FULL transition, not a partial one. This is
  // the subset-drift assertion — the boot path used to skip the fingerprint
  // reset and the remembered session, so the state disagreed with the screen.
  {
    const w = bootWorld();
    w.sandbox.viewFp = "stale-fingerprint-from-nowhere";
    w.sandbox.viewDirty = false;
    w.sandbox.offlineFirstPaint();
    await settle();
    w.fire((t) => t.ms === w.sandbox.FIRST_PAINT_GRACE_MS);
    await settle();

    ok("the mirror painted", w.called("onReplay") === 1);
    ok("identity is the chat on screen", w.sandbox.currentSession === "M.jsonl");
    ok("the title is the chat on screen", w.seen.title === "Mirror chat");
    ok("the URL is the chat on screen", w.urlSession() === "M");
    ok("the remembered session is the chat on screen (this used to be skipped)",
      w.remembered() === "M.jsonl");
    ok("the stale fingerprint cannot survive into this view (also skipped)",
      w.sandbox.viewFp === "" && w.sandbox.viewDirty === true);
    ok("the paint is marked as coming from the mirror, so it is never stashed",
      w.sandbox.offlineViewing === true);
    ok("the pin toggle follows the chat on screen", w.called("refreshOfflinePinUi") === 1);
    ok("…and the spinner is gone", w.seen.booted === true);
  }

  done();
  report("test_choreo_session_switch.js");
})();
