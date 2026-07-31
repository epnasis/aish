// Node-only, dependency-free checks for the transcript BETWEEN chats
// ([PENDING-VIEW] in app.js), driven through the real resumeSession.
//
// The defect it exists to fix: tapping a chat in the rail sent "switch me" and
// then, on anything but a warm-peek hit, did NOTHING until the server answered
// — no transcript, no title, no URL. The rail slid away over the chat you had
// just left and left it sitting there for the whole round trip, which on a slow
// link is indistinguishable from a tap that never registered. Only the two most
// recently used chats are ever pre-warmed, so that was the COMMON case for
// anything further down the list.
//
// What is pinned here:
//   - a tap with nothing warm STILL leaves the outgoing chat, immediately, and
//     says what it is doing;
//   - the offline mirror this device already holds is what fills the gap — it
//     used to be consulted only when the socket was DOWN, so on the one
//     connection where it helps most (up, but slow) it sat unused;
//   - a mirror paint claims no fingerprint, so the server's replay corrects it;
//   - a mirror read that resolves after the server's replay is DISCARDED — the
//     cached copy must never overpaint an authoritative one;
//   - the replay is what ends the wait, and it really is wired to;
//   - a socket that reports OPEN and is not stops spinning and offers the
//     control that fixes it;
//   - a chat the server says is gone does not spin forever;
//   - a warm peek still short-circuits all of it (no wait, no mirror read).
//
// Run manually: node tests/js/test_pending_view.js
"use strict";

const assert = require("assert");
const { sessionWorld, checks, expectReached } = require("./harness");

const { ok, report } = checks();

// A DOM small enough to read, with the two behaviours the shared fake element
// lacks and this block depends on: remove() really detaches, and querySelector
// really finds a class.
function node(tag) {
  const el = {
    tagName: tag,
    className: "",
    textContent: "",
    type: "",
    dataset: {},
    parent: null,
    children: [],
    onclick: null,
    get childElementCount() { return this.children.length; },
    appendChild(child) { child.parent = el; el.children.push(child); return child; },
    append(...kids) { for (const kid of kids) el.appendChild(kid); },
    replaceChildren(...kids) {
      for (const kid of el.children) kid.parent = null;
      el.children = [];
      el.append(...kids);
    },
    remove() {
      if (!el.parent) return;
      const at = el.parent.children.indexOf(el);
      if (at >= 0) el.parent.children.splice(at, 1);
      el.parent = null;
    },
    querySelector(sel) {
      const want = sel.replace(/^\./, "");
      const hit = (n) => (n.className || "").split(/\s+/).includes(want);
      const walk = (n) => {
        for (const kid of n.children) {
          if (hit(kid)) return kid;
          const deeper = walk(kid);
          if (deeper) return deeper;
        }
        return null;
      };
      return walk(el);
    },
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      contains(c) { return this._set.has(c); },
    },
  };
  return el;
}

const textOf = (el) => el.textContent + el.children.map(textOf).join("");

function switchWorld({ cached = null, prefetch = null, loadPromise = null } = {}) {
  const messagesEl = node("div");
  const replays = [];
  const reconnects = [];
  const w = sessionWorld({
    globals: {
      messagesEl,
      document: { createElement: (tag) => node(tag), querySelectorAll: () => [] },
      knownTitle: () => undefined,
      resetLiveTurn() {},
      clearPendingSends() {},
      stopSpeaking() {},
      removeCwdChip() {},
      scrollToEnd() {},
      addMsg(kind, text) {
        const el = node("div");
        el.className = `msg ${kind}`;
        el.textContent = text;
        messagesEl.appendChild(el);
        return el;
      },
      reconnect: () => reconnects.push(true),
      forgetSession() {},
      freshPrefetch: () => prefetch,
      offlineLoad: () => loadPromise || Promise.resolve(cached),
      // What the real onReplay does FIRST — mirrored here so a scenario can see
      // the wait end. The wiring itself is asserted separately, so this mirror
      // cannot silently drift from the shipped function.
      onReplay: (event) => { w.sandbox.paintLanded(); replays.push(event); },
      openCachedSession: () => {},
    },
  });
  w.load("// [SESSION-ENTER-START]", "// [SESSION-ENTER-END]");
  w.load("function resumeSession(name) {", "// [PENDING-VIEW-START]");
  w.load("// [PENDING-VIEW-START]", "// [PENDING-VIEW-END]");
  w.load("function onSessionGone(name) {", "function onSessionDeleted(event) {");
  w.sandbox.ws = { readyState: w.WebSocket.OPEN };
  w.messagesEl = messagesEl;
  w.replays = replays;
  w.reconnects = reconnects;
  w.loadingRow = () => messagesEl.querySelector(".loading-view");
  // Whatever the old chat had on screen, so "did we actually leave?" is a real
  // question rather than a comparison against an empty pane.
  w.sandbox.currentSession = "old.jsonl";
  const stale = node("div");
  stale.className = "msg user";
  stale.textContent = "the previous conversation";
  messagesEl.appendChild(stale);
  return w;
}

const scenario = (label, fn) => { fn(); ok(label, true); };

// ---- 1. The tap is honoured with no server involvement at all -------------
scenario("a tap with nothing warm still leaves the outgoing chat, at once", () => {
  const w = switchWorld();
  w.sandbox.resumeSession("new.jsonl");
  assert.strictEqual(w.sandbox.currentSession, "new.jsonl", "identity moved");
  assert(!textOf(w.messagesEl).includes("the previous conversation"),
    "the chat you left is off the screen — the whole reported defect");
  assert(w.loadingRow(), "and something says what is happening");
  assert(/Loading/.test(textOf(w.loadingRow())));
  const resumes = w.calls.filter((c) => c.name === "send");
  assert.strictEqual(resumes.length, 1, "the server was still asked");
});

// The mirror read is genuinely async, so let its continuation run before
// asking what it did. A vacuous pass here would look identical to a real one,
// which is what expectReached below is for.
const settle = () => new Promise((r) => setImmediate(r));
const asyncScenarios = [];

// ---- 2. The mirror fills the gap -----------------------------------------
asyncScenarios.push(async () => {
  const events = [{ type: "user", text: "hello" }];
  const w = switchWorld({ cached: { meta: { name: "new.jsonl", title: "Mirrored" }, events } });
  w.sandbox.resumeSession("new.jsonl");
  await settle();
  scenario("the chat this device already holds is painted from the mirror", () => {
    assert.strictEqual(w.replays.length, 1);
    assert.deepStrictEqual(w.replays[0].events, events);
  });
  scenario("and that paint claims no fingerprint, so the replay corrects it", () => {
    // `offlineViewing` is what onReplay reads to refuse the fingerprint (#202).
    assert.strictEqual(w.sandbox.offlineViewing, true);
  });
});

// ---- 3. A cached copy never overpaints an authoritative one ---------------
asyncScenarios.push(async () => {
  let resolve;
  const w = switchWorld({ loadPromise: new Promise((r) => { resolve = r; }) });
  w.sandbox.resumeSession("new.jsonl");
  // The server won the race while IndexedDB was still reading.
  w.sandbox.paintLanded();
  resolve({ meta: { name: "new.jsonl", title: "stale" }, events: [{ type: "user", text: "old" }] });
  await settle();
  scenario("a mirror read that lands after the server's replay is discarded", () => {
    assert.strictEqual(w.replays.length, 0);
  });
});

// ---- 4. The replay is what ends the wait, and really is wired to ----------
scenario("onReplay ends the wait — asserted against the shipped function", () => {
  const src = require("./harness").appSource();
  const from = src.indexOf("function onReplay(event) {");
  assert(from !== -1);
  const body = src.slice(from, src.indexOf("// [REPLAY-LANDING-END]"));
  assert(/paintLanded\(\)/.test(body),
    "onReplay must end the pending-view wait, or a painted chat keeps its spinner");
});

scenario("ending the wait disarms the stall deadline", () => {
  const w = switchWorld();
  w.sandbox.resumeSession("new.jsonl");
  assert.strictEqual(w.armed((t) => t.ms >= 1000).length, 1, "a deadline was armed");
  w.sandbox.paintLanded();
  assert.strictEqual(w.armed((t) => t.ms >= 1000).length, 0);
});

// ---- 5. A socket that reports OPEN and is not -----------------------------
scenario("a stalled switch says so and offers the control that fixes it", () => {
  const w = switchWorld();
  w.sandbox.resumeSession("new.jsonl");
  w.fire((t) => t.ms >= 1000);
  const text = textOf(w.loadingRow());
  assert(/stalled/.test(text), "the placeholder stops pretending to make progress");
  assert(/Reconnect/.test(text));
  const button = w.loadingRow().querySelector(".loading-view-retry");
  button.onclick();
  assert.strictEqual(w.reconnects.length, 1, "and the control works");
});

// ---- 6. Nothing to paint, ever -------------------------------------------
scenario("a chat the server says is gone does not spin forever", () => {
  const w = switchWorld();
  w.sandbox.resumeSession("new.jsonl");
  // What the server actually answers for a chat that is no longer there.
  w.sandbox.onSessionGone("new.jsonl");
  assert(!w.loadingRow(), "the spinner promised a transcript that is not coming");
  assert(/no longer exists/.test(textOf(w.messagesEl)));
});

// ---- 7. The fast path is untouched ---------------------------------------
scenario("a warm peek still short-circuits the wait entirely", () => {
  const w = switchWorld({ prefetch: { events: [{ type: "user", text: "warm" }], truncated: false } });
  w.sandbox.resumeSession("new.jsonl");
  assert.strictEqual(w.replays.length, 1, "painted from the peek");
  assert(!w.loadingRow(), "no placeholder — there was never a gap to fill");
  assert.strictEqual(w.armed((t) => t.ms >= 1000).length, 0, "and no deadline armed");
});

{
  const done = expectReached("the async mirror scenarios never ran");
  (async () => {
    for (const run of asyncScenarios) await run();
    done();
    report("test_pending_view.js");
  })();
}
