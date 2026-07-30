// The live status card is the TAIL of the turn, and the answer is the reading
// anchor. Two mechanisms, both real code out of app.js:
//
//   keepTraceLast()  — the card is created BEFORE the answer bubble exists
//                      (steps arrive first) and every later append lands after
//                      it, so "prompt → text → status" is an invariant that has
//                      to be re-established, not an order chosen once. Driven
//                      here through scrollToEnd, the funnel it is wired into, so
//                      the wiring is pinned and not just the function.
//
//   anchorAnswer()   — "scroll while it fills, then stop" with no follow flag
//                      and no mode: the clamp means the view rises only until
//                      the answer's top reaches the top of the screen, and since
//                      content is only ever appended BELOW that top, nothing
//                      scrolls again for the rest of the turn.
//
// Run manually: node tests/js/test_trace_tail.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  assert(a !== -1 && b !== -1, `markers not found: ${from} … ${to}`);
  return src.slice(a, b);
}

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

// A node list that behaves like the real thing in the one way that matters:
// appendChild on a node that is ALREADY a child MOVES it to the end.
function fakeNode(name, classes) {
  const set = new Set(classes || []);
  return {
    name,
    parentNode: null,
    classList: {
      contains: (c) => set.has(c),
      add: (c) => set.add(c),
      remove: (c) => set.delete(c),
    },
  };
}

function fakeTranscript() {
  const kids = [];
  const el = {
    children: kids,
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    get lastElementChild() { return kids.length ? kids[kids.length - 1] : null; },
    appendChild(node) {
      const at = kids.indexOf(node);
      if (at !== -1) kids.splice(at, 1);
      kids.push(node);
      node.parentNode = el;
      return node;
    },
    getBoundingClientRect: () => ({ top: 0 }),
  };
  return el;
}

function order(msgs) {
  return msgs.children.map((c) => c.name);
}

function tailWorld() {
  const sandbox = {
    currentTrace: null,
    pinTrace() {},
    updateScrollButton() {},
    updateEmptyHint() {},
    messagesEl: fakeTranscript(),
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("// [TRACE-TAIL-START]", "// [TRACE-TAIL-END]"), sandbox);
  vm.runInContext(slice("function nearBottom() {", "// Empty-state welcome hero"), sandbox);
  assert(typeof sandbox.keepTraceLast === "function", "keepTraceLast not extracted");
  assert(typeof sandbox.scrollToEnd === "function", "scrollToEnd not extracted");
  return sandbox;
}

// A turn that narrates, runs a command, then answers — the interleaving that
// makes the position of the card a moving target.
check("the live card stays the last child through an interleaved turn", () => {
  const s = tailWorld();
  const msgs = s.messagesEl;

  msgs.appendChild(fakeNode("prompt"));
  const trace = fakeNode("trace", ["trace", "live"]);
  msgs.appendChild(trace);            // ensureTrace
  s.currentTrace = { el: trace };
  s.scrollToEnd();
  assert.deepStrictEqual(order(msgs), ["prompt", "trace"]);

  for (const name of ["answer1", "terminal", "answer2"]) {
    msgs.appendChild(fakeNode(name)); // addMsg / a command block / more text
    s.scrollToEnd();
    assert.strictEqual(msgs.lastElementChild, trace, `card lost the tail after ${name}`);
  }
  assert.deepStrictEqual(
    order(msgs), ["prompt", "answer1", "terminal", "answer2", "trace"]
  );
});

check("a finished card keeps its place in history", () => {
  // Once the turn ends the card is a footnote under the answer, not a tail that
  // chases the next turn's content — finishTrace drops `live`, and that is the
  // whole signal keepTraceLast reads.
  const s = tailWorld();
  const msgs = s.messagesEl;
  const trace = fakeNode("trace", ["trace"]); // no "live"
  msgs.appendChild(fakeNode("answer"));
  msgs.appendChild(trace);
  s.currentTrace = { el: trace };
  msgs.appendChild(fakeNode("next-prompt"));
  s.scrollToEnd();
  assert.deepStrictEqual(order(msgs), ["answer", "trace", "next-prompt"]);
});

check("a card already removed from the transcript is never re-inserted", () => {
  // finishTrace deletes an empty trace box outright (#84); resurrecting it here
  // would put a stray "Working…" row back under the answer.
  const s = tailWorld();
  const msgs = s.messagesEl;
  const trace = fakeNode("trace", ["trace", "live"]);
  s.currentTrace = { el: trace }; // parentNode stays null — never appended
  msgs.appendChild(fakeNode("answer"));
  s.scrollToEnd();
  assert.deepStrictEqual(order(msgs), ["answer"]);
});

check("no live card at all is a no-op", () => {
  const s = tailWorld();
  s.currentTrace = null;
  s.messagesEl.appendChild(fakeNode("answer"));
  s.scrollToEnd();
  assert.deepStrictEqual(order(s.messagesEl), ["answer"]);
});

// ---- the anchor -----------------------------------------------------------

// A one-dimensional layout: blocks stacked from the top of the scroller, so an
// element's viewport top is (its offset) - scrollTop and the container's is 0.
function anchorWorld(clientHeight) {
  const sandbox = {
    turnAnchorEl: null,
    updateScrollButton() {},
    updateEmptyHint() {},
    messagesEl: {
      scrollTop: 0,
      scrollHeight: 0,
      clientHeight,
      getBoundingClientRect: () => ({ top: 0 }),
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("function anchorAnswer(force) {", "function renderAnswerNow"), sandbox);
  assert(typeof sandbox.anchorAnswer === "function", "anchorAnswer not extracted");
  // `before` = everything above the answer (the prompt), `after` = the pinned
  // status card below it.
  sandbox.layout = (before, answerH, after) => {
    sandbox.messagesEl.scrollHeight = before + answerH + after;
    sandbox.turnAnchorEl = {
      isConnected: true,
      getBoundingClientRect: () => ({ top: before - sandbox.messagesEl.scrollTop }),
    };
  };
  return sandbox;
}

check("the view rises as the answer fills, then locks for good", () => {
  const s = anchorWorld(600);

  // Short answer: the whole turn fits, so there is nothing to scroll.
  s.layout(100, 50, 50);
  s.anchorAnswer();
  assert.strictEqual(s.messagesEl.scrollTop, 0, "scrolled a turn that fits");

  // Still shorter than a screen — the clamp holds it at the very bottom.
  s.layout(100, 300, 50);
  s.anchorAnswer();
  assert.strictEqual(s.messagesEl.scrollTop, 0);

  // Past a screenful: the answer's top can now reach the top, and does.
  s.layout(100, 600, 50);
  s.anchorAnswer();
  const locked = s.messagesEl.scrollTop;
  assert.strictEqual(locked, 94, "the answer's top should sit 6px under the top");

  // …and every further token appends BELOW that top, so the view never moves
  // again however long the answer gets. This is the "stop scrolling once it
  // fills the page" requirement, with no flag to get out of sync.
  for (const h of [900, 2000, 8000]) {
    s.layout(100, h, 50);
    s.anchorAnswer();
    assert.strictEqual(s.messagesEl.scrollTop, locked, `chased the tail at ${h}px`);
  }
});

check("reading further down the answer is never yanked back up", () => {
  const s = anchorWorld(600);
  s.layout(100, 2000, 50);
  s.messagesEl.scrollTop = 700; // the reader scrolled down into the answer
  s.anchorAnswer();
  assert.strictEqual(s.messagesEl.scrollTop, 700);
});

check("force brings a brand-new answer into view", () => {
  // The one caller that forces is the answer opening: whatever you were reading,
  // the reply you just asked for comes on screen.
  const s = anchorWorld(600);
  s.layout(100, 2000, 50);
  s.messagesEl.scrollTop = 700;
  s.anchorAnswer(true);
  assert.strictEqual(s.messagesEl.scrollTop, 94);
});

// ---- the answer claims the anchor ----------------------------------------

check("the answer bubble becomes the anchor, not the status card", () => {
  // The card used to claim it, which is what pinned the progress box to the top
  // of the screen for the whole turn.
  const sandbox = {
    answerEl: null,
    answerText: "",
    answerStableLen: 0,
    answerStableNodes: 0,
    answerRenderQueued: false,
    answerAbandoned: false,
    sawAnswer: false,
    replaying: false,
    turnAnchorEl: fakeNode("prompt"),
    currentTrace: null,
    addMsg: (kind) => fakeNode(kind),
    collapseTimelineForAnswering() {},
    updateTraceHead() {},
    requestAnimationFrame() {},
    renderAnswerFrame() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("function onToken(text) {", "function renderAnswerFrame"), sandbox);
  assert(typeof sandbox.onToken === "function", "onToken not extracted");

  sandbox.onToken("hello");
  assert.strictEqual(sandbox.turnAnchorEl, sandbox.answerEl, "the answer must be the anchor");
  assert.strictEqual(sandbox.answerEl.name, "answer md");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall trace-tail checks passed");
