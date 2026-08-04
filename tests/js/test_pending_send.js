// Node-only, dependency-free checks for the optimistic send bubble
// ([PENDING-SEND] in app.js).
//
// The defect it exists to fix: the composer cleared the instant the text was
// handed to the socket, but the blue bubble was drawn only when the SERVER
// echoed the turn back. On a slow link the message existed nowhere the user
// could see for seconds — composer empty, transcript unchanged — and a long
// message read as lost.
//
// What is pinned here:
//   - a send draws its bubble immediately, with no server event at all;
//   - the server's own version of the turn REPLACES it (one bubble, not two),
//     and so does a `queued` event, which is the other way a send is accounted
//     for;
//   - two sends inside one round trip resolve by TEXT, in whatever order the
//     server actually took them;
//   - the fallback still resolves when the server's text differs from ours
//     (it appends attachment notes);
//   - a bubble destroyed while un-acknowledged HOLDS its text rather than
//     handing it straight back: "the DOM is going away" is not "it did not
//     arrive", and a rebuild is usually a reconnect whose replay is about to
//     say which. The replay adjudicates; a deadline releases anything no
//     replay ever settled, because losing what you typed is the one
//     unacceptable outcome — and recovery never duplicates text the composer
//     already holds;
//   - a resolved send hands nothing back;
//   - a socket that reports OPEN and is not stops claiming "Sending…".
//
// Run manually: node tests/js/test_pending_send.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, checks, surface } = require("./harness");

const { ok, report } = checks();
const src = appSource();

// Each case is a whole scenario rather than a single expression, so it runs in
// a function and counts once it returns without throwing.
const scenario = (label, fn) => { fn(); ok(label, true); };

function slice(startMark, endMark) {
  const start = src.indexOf(startMark);
  const end = src.indexOf(endMark);
  assert(start !== -1 && end !== -1, `${startMark} markers not found in app.js`);
  return src.slice(start, end);
}

// A DOM small enough to read, with the one behaviour fakeElement lacks: remove()
// really detaches, which is exactly what resolution and teardown are.
function node(tag) {
  return {
    tagName: tag,
    className: "",
    textContent: "",
    type: "",
    dataset: {},
    parent: null,
    children: [],
    onclick: null,
    get childElementCount() { return this.children.length; },
    appendChild(child) { child.parent = this; this.children.push(child); return child; },
    append(...kids) { for (const kid of kids) this.appendChild(kid); },
    replaceChildren(...kids) {
      for (const kid of this.children) kid.parent = null;
      this.children = [];
      this.append(...kids);
    },
    remove() {
      if (!this.parent) return;
      const at = this.parent.children.indexOf(this);
      if (at >= 0) this.parent.children.splice(at, 1);
      this.parent = null;
    },
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      contains(c) { return this._set.has(c); },
    },
  };
}

// Own text plus every descendant's — the stalled status puts its Reconnect
// control INSIDE the status line, so a shallow read would miss it.
const textOf = (el) => el.textContent + el.children.map(textOf).join("");

function world() {
  const timers = [];
  const messagesEl = node("div");
  const input = { value: "" };
  const toasts = [];
  const reconnects = [];
  const sandbox = {
    messagesEl,
    input,
    document: { createElement: (tag) => node(tag) },
    scrollToEnd() {},
    saveDraft() {},
    resizeInput() {},
    showToast: (text) => toasts.push(text),
    reconnect: () => reconnects.push(true),
    // Sending is seeing ([SEEN]): your own message is output, so the stamp has
    // to move past it. Recorded here so the scenario below can pin that it does.
    currentSession: "session-under-test.jsonl",
    seenCalls: [],
    markSeen(name) { sandbox.seenCalls.push(name); },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (id) timers[id - 1] = null; },
  };
  vm.createContext(sandbox);
  // The real code: the block under test plus the two helpers it leans on.
  vm.runInContext(slice("function addMsg(kind, text) {", "// The prompt that started"), sandbox);
  vm.runInContext(slice("function stripAttachmentNotes(text) {", "function rememberPrompt("), sandbox);
  // surface(): a vm context hides top-level const/let, and this block's held
  // store is one — the scenarios below read it to tell "held" from "handed
  // back", which is the whole distinction the duplicate came from.
  vm.runInContext(surface(slice("// [PENDING-SEND-START]", "// [PENDING-SEND-END]")), sandbox);

  return {
    sandbox,
    input,
    toasts,
    reconnects,
    seenCalls: sandbox.seenCalls,
    // The bubbles a reader would actually see, in order.
    bubbles: () =>
      messagesEl.children
        .filter((c) => c.className.startsWith("msg user"))
        .map((c) => c.textContent),
    statuses: () =>
      messagesEl.children
        .filter((c) => (c.className || "").includes("pending-send"))
        .map(textOf),
    fire: () => { for (const t of timers) if (t) t.fn(); },
  };
}

scenario("a send is on screen before any server event", () => {
  const w = world();
  w.sandbox.addPendingSend("write the report");
  assert.deepStrictEqual(w.bubbles(), ["write the report"]);
  assert.deepStrictEqual(w.statuses(), ["Sending…"]);
});

scenario("sending is seeing — the chat you typed into is not unread for it", () => {
  const w = world();
  w.sandbox.addPendingSend("write the report");
  // Your own message IS output (a message in the chat), so without this stamp
  // the chat flags itself unread for a sentence you wrote, for as long as the
  // turn runs — the seen stamp otherwise only moves on entry and at `done`.
  assert.deepStrictEqual(w.seenCalls, ["session-under-test.jsonl"]);
});

scenario("the server's own turn replaces the bubble rather than doubling it", () => {
  const w = world();
  w.sandbox.addPendingSend("write the report");
  w.sandbox.resolvePendingSend("write the report");
  assert.deepStrictEqual(w.bubbles(), [], "the pending bubble is gone");
  assert.deepStrictEqual(w.statuses(), [], "and so is its status row");
  // The real `user` handler appends the authoritative bubble right after, so
  // the reader sees exactly one — the point of resolving rather than confirming.
});

scenario("a queued message resolves the same way (the chip takes over)", () => {
  const w = world();
  w.sandbox.addPendingSend("later, please");
  w.sandbox.resolvePendingSend("later, please");
  assert.deepStrictEqual(w.bubbles(), []);
});

scenario("two sends in one round trip resolve by text, in the server's order", () => {
  const w = world();
  w.sandbox.addPendingSend("first");
  w.sandbox.addPendingSend("second");
  // The server took the SECOND one first (it queued the first behind a task).
  w.sandbox.resolvePendingSend("second");
  assert.deepStrictEqual(w.bubbles(), ["first"], "the right bubble was resolved");
  w.sandbox.resolvePendingSend("first");
  assert.deepStrictEqual(w.bubbles(), []);
});

scenario("an echo carrying attachment notes still resolves its send", () => {
  const w = world();
  w.sandbox.addPendingSend("look at this");
  // What the server echoes back is our text plus notes it appended itself.
  w.sandbox.resolvePendingSend("look at this\n[attached file: /tmp/up/x.png]");
  assert.deepStrictEqual(w.bubbles(), []);
});

scenario("an unrelated echo still clears the oldest rather than stranding it", () => {
  const w = world();
  w.sandbox.addPendingSend("mine");
  // A turn we did not start (a resume note, another device's message): there is
  // no text match, and leaving a bubble stuck on "Sending…" forever is worse
  // than resolving the oldest one.
  w.sandbox.resolvePendingSend("[automatic resume]");
  assert.deepStrictEqual(w.bubbles(), []);
});

scenario("a bubble destroyed un-acknowledged HOLDS its text — it does not hand it back yet", () => {
  const w = world();
  w.sandbox.addPendingSend("a long message worth not losing");
  w.sandbox.clearPendingSends();
  assert.deepStrictEqual(w.bubbles(), [], "the bubble goes with the DOM");
  assert.strictEqual(w.input.value, "", "the words are held, not returned on a guess");
  assert.strictEqual(w.toasts.length, 0, "nothing to say until something knows");
  assert.strictEqual(w.sandbox.heldSends.length, 1, "…but they are kept");
});

// THE REGRESSION. A reconnect rebuilds the transcript and then replays it, so
// the recovery used to fire one moment before the answer arrived — and the
// answer was "I have your message". The owner saw a toast saying it was not
// confirmed, with the words back under the cursor, and sent it again.
scenario("a replay that CONTAINS the message proves it arrived — nothing comes back", () => {
  const w = world();
  w.sandbox.addPendingSend("adjust the rule");
  w.sandbox.clearPendingSends();               // the reconnect rebuild
  w.sandbox.adjudicateHeldSends("session-under-test.jsonl", [
    { type: "user", text: "something older" },
    { type: "user", text: "adjust the rule" }, // the server has it
  ]);
  assert.strictEqual(w.input.value, "", "recovering it would duplicate the instruction");
  assert.strictEqual(w.toasts.length, 0, "and there is nothing to warn about");
  assert.strictEqual(w.sandbox.heldSends.length, 0, "the question is settled");
});

scenario("a replay WITHOUT the message hands it back, and says so", () => {
  const w = world();
  w.sandbox.addPendingSend("never arrived");
  w.sandbox.clearPendingSends();
  w.sandbox.adjudicateHeldSends("session-under-test.jsonl", [
    { type: "user", text: "something else entirely" },
  ]);
  assert.strictEqual(w.input.value, "never arrived");
  assert.strictEqual(w.toasts.length, 1);
  assert(/composer/.test(w.toasts[0]), "and the user is told where it went");
});

scenario("another chat's replay cannot settle a send made in this one", () => {
  const w = world();
  w.sandbox.addPendingSend("belongs to this chat");
  w.sandbox.clearPendingSends();
  w.sandbox.adjudicateHeldSends("some-other-session.jsonl", [{ type: "user", text: "unrelated" }]);
  assert.strictEqual(w.input.value, "", "it has no opinion about this chat");
  assert.strictEqual(w.sandbox.heldSends.length, 1, "still waiting for one that does");
});

scenario("a replay is matched on exact text, never on position", () => {
  const w = world();
  w.sandbox.addPendingSend("the one I just sent");
  w.sandbox.clearPendingSends();
  // A replay carries every message the chat ever had. Resolving positionally
  // against those would "confirm" this send using something written last week.
  w.sandbox.adjudicateHeldSends("session-under-test.jsonl", [
    { type: "user", text: "last week's question" },
    { type: "done", text: "the one I just sent" }, // not a user turn
  ]);
  assert.strictEqual(w.input.value, "the one I just sent");
});

scenario("nothing ever settles it, so the deadline hands it back", () => {
  const w = world();
  w.sandbox.addPendingSend("still offline");
  w.sandbox.clearPendingSends();
  assert.strictEqual(w.input.value, "", "held while an answer might still come");
  w.fire(); // the release deadline
  assert.strictEqual(w.input.value, "still offline", "holding forever is losing it slowly");
  assert(/composer/.test(w.toasts[w.toasts.length - 1]));
});

scenario("recovered text is prepended to whatever the composer already holds", () => {
  const w = world();
  w.input.value = "half a thought";
  w.sandbox.addPendingSend("the sent one");
  w.sandbox.clearPendingSends();
  w.fire();
  assert.strictEqual(w.input.value, "the sent one\n\nhalf a thought");
});

scenario("recovery never duplicates text the composer already holds", () => {
  const w = world();
  w.sandbox.addPendingSend("say it once");
  w.input.value = "say it once";
  w.sandbox.clearPendingSends();
  w.fire();
  assert.strictEqual(w.input.value, "say it once");
  assert.strictEqual(w.toasts.length, 0, "nothing was recovered, so say nothing");
});

scenario("a resolved send hands nothing back", () => {
  const w = world();
  w.sandbox.addPendingSend("acknowledged");
  w.sandbox.resolvePendingSend("acknowledged");
  w.sandbox.clearPendingSends();
  w.fire();
  assert.strictEqual(w.input.value, "", "the server has it — recovering would duplicate");
  assert.strictEqual(w.toasts.length, 0);
});

scenario("a socket that reports OPEN and is not stops claiming 'Sending…'", () => {
  const w = world();
  w.sandbox.addPendingSend("into the void");
  w.fire(); // the stall watch
  assert(/Still sending/.test(w.statuses()[0]), "the status admits it");
  assert(/reconnect/.test(w.statuses()[0]), "and offers the control that fixes it");
});

scenario("the stall watch stands down once the send is acknowledged", () => {
  const w = world();
  w.sandbox.addPendingSend("quick one");
  w.sandbox.resolvePendingSend("quick one");
  w.fire();
  assert.deepStrictEqual(w.statuses(), [], "nothing left to mark stalled");
});

report("test_pending_send.js");
