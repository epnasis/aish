// Narration is its own bubble (#212) — [DELIVERY].
//
// A turn says several things: what it found, what it will do next, and finally
// the answer. `delivery` ends one of them. Three properties are pinned here,
// against the REAL onToken / closeAnswer / onDelivery lifted out of app.js:
//
//   * consecutive deliveries are separate bubbles, not one paragraph that grew
//     for four minutes;
//   * an interim bubble gets NO tool row — that row names the deliverable
//     (fork ordinal, regenerate, 👍/👎), and spreading it across narration
//     would fragment the rating corpus #207 exists to build;
//   * the event's own text is the fallback, because on a rule-bound turn the
//     tokens never streamed at all (Verify buffers them) and on a mid-stream
//     replay the live tail was dropped.
//
// Run manually: node tests/js/test_delivery_bubble.js
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

// The transcript, reduced to what these three functions touch: bubbles in
// order, each remembering its classes and whether a tool row was hung on it.
function world() {
  const bubbles = [];
  const sandbox = {
    answerEl: null,
    answerText: "",
    answerStableLen: 0,
    answerStableNodes: 0,
    answerRenderQueued: false,
    sawAnswer: false,
    answerAbandoned: false,
    replaying: false,
    turnAnchorEl: null,
    currentTrace: null,
    lastUserPrompt: "what does it look like?",
    bubbles,
    requestAnimationFrame() {},           // frames never run; closeAnswer flushes
    renderAnswerFrame() {},               // …but it must still be nameable
    collapseTimelineForAnswering() {},
    updateTraceHead() {},
    anchorAnswer() {},
    highlightFences() {},
    addMsg(kind) {
      const el = {
        kind,
        text: "",
        tools: false,
        classes: new Set(kind.split(" ")),
        classList: {
          add: (c) => el.classes.add(c),
          contains: (c) => el.classes.has(c),
        },
      };
      bubbles.push(el);
      return el;
    },
    // Stands in for the markdown renderer: what matters here is WHICH bubble
    // the text landed in, not how it was marked up.
    renderAnswerNow() {
      if (sandbox.answerEl) sandbox.answerEl.text = sandbox.answerText;
    },
    attachAnswerTools(el) {
      el.tools = true;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("function onToken(text) {", "function renderAnswerFrame"), sandbox);
  vm.runInContext(slice("// [ANSWER-CLOSE-START]", "// [ANSWER-CLOSE-END]"), sandbox);
  vm.runInContext(slice("// [DELIVERY-START]", "// [DELIVERY-END]"), sandbox);
  assert(typeof sandbox.onToken === "function", "onToken not extracted");
  assert(typeof sandbox.closeAnswer === "function", "closeAnswer not extracted");
  assert(typeof sandbox.onDelivery === "function", "onDelivery not extracted");
  return sandbox;
}

const SAID_FIRST = "Let me search for the iPhone 18.";
const SAID_NEXT = "There are leaks about a fold — digging into that.";
const ANSWER = "It folds, and the screen is 7.8 inches.";

check("each delivery is its own bubble, and only the answer keeps the tool row", () => {
  const s = world();

  // Step one: prose streams, then the delivery closes it.
  s.onToken(SAID_FIRST);
  s.onDelivery({ text: SAID_FIRST });
  // Step two: the same again — this is the case that used to append into the
  // bubble already on screen.
  s.onToken(SAID_NEXT);
  s.onDelivery({ text: SAID_NEXT });
  // The answer: streams and closes the ordinary way.
  s.onToken(ANSWER);
  s.closeAnswer();

  assert.strictEqual(s.bubbles.length, 3, "the turn arrived as one bubble");
  assert.deepStrictEqual(
    s.bubbles.map((b) => b.text), [SAID_FIRST, SAID_NEXT, ANSWER]
  );
  assert.deepStrictEqual(
    s.bubbles.map((b) => b.classes.has("interim")), [true, true, false]
  );
  assert.deepStrictEqual(
    s.bubbles.map((b) => b.tools), [false, false, true],
    "a tool row was hung on narration, or withheld from the answer"
  );
});

check("a delivery whose tokens never streamed paints from the event", () => {
  // The bound turn: Verify holds every token, because whether a turn is the
  // ANSWER is knowable only once its tool calls arrive. The whole delivery
  // arrives at once, and this event is the only copy of it there is.
  const s = world();
  s.onDelivery({ text: SAID_FIRST });
  assert.strictEqual(s.bubbles.length, 1);
  assert.strictEqual(s.bubbles[0].text, SAID_FIRST);
  assert.ok(s.bubbles[0].classes.has("interim"));
});

check("a streamed delivery is not painted twice", () => {
  const s = world();
  s.onToken(SAID_FIRST);
  s.onDelivery({ text: SAID_FIRST });
  assert.strictEqual(s.bubbles.length, 1);
  assert.strictEqual(s.bubbles[0].text, SAID_FIRST);
});

check("an abandoned turn still gets the delivery, because the text is complete", () => {
  // resetLiveTurn dropped the half-streamed tail (a replay replaced the
  // transcript under it). A lone live token stays dropped — it would be a
  // fragment — but the delivery carries the whole thing.
  const s = world();
  s.answerAbandoned = true;
  s.onToken("half a sen");                    // dropped by the guard
  assert.strictEqual(s.bubbles.length, 0);
  s.onDelivery({ text: SAID_FIRST });
  assert.strictEqual(s.bubbles.length, 1);
  assert.strictEqual(s.bubbles[0].text, SAID_FIRST);
  assert.strictEqual(s.answerAbandoned, false, "the flag outlived the bubble it described");
});

check("the flags describe the bubble, not the turn", () => {
  // Both go back to false so the NEXT delivery — and `done`, whose render is
  // gated on sawAnswer — start from a clean bubble.
  const s = world();
  s.onToken(SAID_FIRST);
  assert.strictEqual(s.sawAnswer, true);
  s.onDelivery({ text: SAID_FIRST });
  assert.strictEqual(s.sawAnswer, false, "done would refuse to render the answer");
  assert.strictEqual(s.answerEl, null, "the bubble was left open");
});

check("an empty delivery closes the bubble without drawing one", () => {
  const s = world();
  s.onDelivery({ text: "" });
  assert.strictEqual(s.bubbles.length, 0);
});

process.exit(failures ? 1 : 0);
