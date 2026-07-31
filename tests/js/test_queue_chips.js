// Node-only, dependency-free checks for the queued-message chips.
//
// The defect it exists to fix, reported from a phone: "the queue is moving as I
// change the chats". It was. A chip names a message ONE chat's agent is
// holding, and its Remove button dequeues from whatever session the client is
// VIEWING — but the chips live outside `#messages`, so a session switch (which
// replaces the transcript wholesale) left them exactly where they were. The
// pending-cd card in the same strip was already dropped on a switch; the
// message chips never were, which is how one half of a strip came to behave
// differently from the other.
//
// The consequence is worse than a cosmetic ghost. In the chat you land in,
// Remove dequeues from THAT session — a no-op — while the message it named goes
// on to run in the chat it was queued in, so "cancel" silently cancels nothing;
// and Edit pulls the text into the new chat's composer while the old chat still
// holds its copy, one send away from running it twice in two places.
//
// What is pinned here:
//   - clearing takes the WHOLE strip, message chips and pending-cd card alike;
//   - a chip carries the text its controls act on, so a cancel names the right
//     message;
//   - the two repaint paths that replace a transcript really do clear it
//     (asserted against the shipped functions, since a chip that outlives the
//     view is precisely a wiring bug, not a logic one).
//
// Run manually: node tests/js/test_queue_chips.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, checks } = require("./harness");

const { ok, report } = checks();
const src = appSource();

function slice(startMark, endMark) {
  const start = src.indexOf(startMark);
  const end = src.indexOf(endMark);
  assert(start !== -1 && end !== -1, `${startMark} not found in app.js`);
  return src.slice(start, end);
}

function node(tag) {
  const el = {
    tagName: tag,
    className: "",
    textContent: "",
    innerHTML: "",
    hidden: false,
    dataset: {},
    parent: null,
    children: [],
    onclick: null,
    get firstChild() { return el.children[0] || null; },
    appendChild(child) { child.parent = el; el.children.push(child); return child; },
    insertBefore(child, before) {
      child.parent = el;
      const at = before ? el.children.indexOf(before) : -1;
      if (at < 0) el.children.push(child);
      else el.children.splice(at, 0, child);
      return child;
    },
    replaceChildren(...kids) {
      for (const kid of el.children) kid.parent = null;
      el.children = [];
      for (const kid of kids) el.appendChild(kid);
    },
    remove() {
      if (!el.parent) return;
      const at = el.parent.children.indexOf(el);
      if (at >= 0) el.parent.children.splice(at, 1);
      el.parent = null;
    },
    // The chips build themselves from innerHTML, so the pieces the code then
    // reaches for are registered here rather than parsed.
    querySelector(sel) {
      const want = sel.replace(/^\./, "");
      return el._parts ? el._parts[want] || null : null;
    },
  };
  return el;
}

// innerHTML is a string assignment in the shipped code; give the parts it then
// queries for real objects so the wiring (onclick handlers) is exercised.
const CHIP_PARTS = ["queue-text", "queue-sub", "queue-edit", "queue-remove"];

function world() {
  const list = node("div");
  list.className = "queue-list";
  const sent = [];
  const input = { value: "" };
  const sandbox = {
    $: (id) => (id === "queue-list" ? list : node("div")),
    document: {
      createElement: (tag) => {
        const el = node(tag);
        el._parts = {};
        for (const part of CHIP_PARTS) el._parts[part] = node("span");
        return el;
      },
    },
    send: (message) => { sent.push(message); return true; },
    scrollToEnd() {},
    resizeInput() {},
    abbreviatePath: (path) => path,
    openDirSheet() {},
    input,
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("function addQueueChip(text) {", "async function uploadFile("), sandbox);
  // Objects minted inside the vm carry a different prototype, so compare the
  // data rather than the identity.
  return {
    sandbox, list, input,
    sent: () => JSON.parse(JSON.stringify(sent)),
    texts: () => list.children.map((c) => c.dataset.text),
  };
}

const scenario = (label, fn) => { fn(); ok(label, true); };

scenario("a chip carries the text its controls act on", () => {
  const w = world();
  w.sandbox.addQueueChip("run the report");
  assert.deepStrictEqual(w.texts(), ["run the report"]);
  assert.strictEqual(w.list.hidden, false);
});

scenario("cancelling a chip names the message, not a position", () => {
  const w = world();
  w.sandbox.addQueueChip("first");
  w.sandbox.addQueueChip("second");
  w.list.children[1].querySelector(".queue-remove").onclick();
  assert.deepStrictEqual(w.sent(), [{ type: "dequeue", text: "second" }]);
  assert.deepStrictEqual(w.texts(), ["first"], "and only that one leaves the strip");
});

scenario("clearing takes the WHOLE strip, cd card included", () => {
  const w = world();
  w.sandbox.addQueueChip("waiting message");
  w.sandbox.addCwdChip("/tmp/elsewhere");
  assert.strictEqual(w.list.children.length, 2);
  w.sandbox.clearQueueChips();
  assert.strictEqual(w.list.children.length, 0,
    "a chip left behind acts on the chat you LAND in, not the one it belongs to");
  assert.strictEqual(w.list.hidden, true);
});

scenario("clearing an already-empty strip is harmless", () => {
  const w = world();
  w.sandbox.clearQueueChips();
  assert.strictEqual(w.list.children.length, 0);
});

// The wiring half. A chip surviving a repaint IS the bug, so the two functions
// that replace a transcript are checked against the shipped source — a logic
// test on clearQueueChips alone would pass with nothing ever calling it.
scenario("every path that replaces the transcript clears the strip", () => {
  for (const [name, endMark] of [
    ["function onReplay(event) {", "// [REPLAY-LANDING-END]"],
    ["function renderLoadingTranscript() {", "function markPendingViewStalled("],
  ]) {
    const body = slice(name, endMark);
    assert(/clearQueueChips\(\)/.test(body),
      `${name} replaces the transcript but leaves the queue strip behind`);
  }
});

report("test_queue_chips.js");
