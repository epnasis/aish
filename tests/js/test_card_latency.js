// How long the approval card was on screen before it was tapped (#306).
//
// The consent design rests on the claim that SOME cards are worth spending —
// the rare, checkable-at-a-glance ones — and nothing measured it. A card tapped
// blind is worse than no card: it converts a missing control into a RECORDED
// consent. A sub-second tap is a blind tap, so the client has to report the one
// number only it can know, and has to say NOTHING when it does not know it.
//
// Runs the REAL markShown / shownExtra / answerCard / onApprovalRequest out of
// app.js by marker, against a minimal fake DOM and a controllable clock.
//
// Run manually: node tests/js/test_card_latency.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

function makeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    _class: "",
    dataset: {},
    style: {},
    get className() { return this._class; },
    set className(v) { this._class = String(v); },
    classList: {
      add(...names) { el._class = (el._class + " " + names.join(" ")).trim(); },
      contains(name) { return el._class.split(/\s+/).includes(name); },
      remove(...names) {
        el._class = el._class.split(/\s+/).filter((c) => !names.includes(c)).join(" ");
      },
    },
    textContent: "",
    title: "",
    tabIndex: 0,
    append(...kids) { el.children.push(...kids); },
    appendChild(kid) { el.children.push(kid); return kid; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    setAttribute() {},
    focus() {},
    remove() {},
  };
  return el;
}

let clock = 1000;
const sent = [];
const sandbox = {
  performance: { now: () => clock },
  document: { createElement: makeEl },
  cards: new Map(),
  pendingCards: 0,
  messagesEl: makeEl("div"),
  FINE_POINTER: false,
  act: (message) => { sent.push(message); },
  activeApprovalCard: () => null,
  // onApprovalRequest's collaborators, stubbed: this test is about the clock,
  // not about what any one card kind renders.
  closeAnswer: () => {},
  currentTrace: null,
  updateTraceHead: () => {},
  buildCommandCard: () => {},
  buildWriteCard: () => {},
  buildToolCard: () => {},
  buildImportCard: () => {},
  buildReadCard: () => {},
  CARD_SHORTCUTS: [],
  refreshStatusline: () => {},
  scrollToEnd: () => {},
  notify: () => {},
};
vm.createContext(sandbox);
vm.runInContext(
  extract("// [CARD-LATENCY-START]", "// [CARD-LATENCY-END]").replace(/\bconst\b/g, "var"),
  sandbox
);
vm.runInContext(
  extract("function answerCard", "// #13/#34: optional feedback").replace(/\bconst\b/g, "var"),
  sandbox
);
vm.runInContext(
  extract("function onApprovalRequest", "function title(card, html)").replace(/\bconst\b/g, "var"),
  sandbox
);
for (const name of ["markShown", "shownExtra", "answerCard", "onApprovalRequest"]) {
  assert(typeof sandbox[name] === "function", `failed to extract ${name} from app.js`);
}

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}`); console.error(`       ${err.message}`); }
}

function drawCard(id) {
  sandbox.cards.clear();
  sent.length = 0;
  sandbox.onApprovalRequest({ id, kind: "command", command: "touch x" });
  const card = sandbox.cards.get(id);
  assert(card, "onApprovalRequest never registered the card");
  return card;
}

check("drawing a card starts its clock", () => {
  // The stamp has to happen where the card GOES ON SCREEN. If it were done
  // lazily at answer time, every tap would measure zero — the exact reading
  // this feature exists to find, manufactured.
  clock = 5000;
  const card = drawCard("a1");
  assert.strictEqual(card.dataset.shownAt, "5000");
});

check("the answer carries how long it was up", () => {
  clock = 5000;
  drawCard("a2");
  clock = 9200;
  sandbox.answerCard("a2", "approve", {});
  const [message] = sent;
  assert.strictEqual(message.type, "approval");
  assert.strictEqual(message.shown_ms, 4200);
});

check("a sub-second tap reports as a sub-second tap", () => {
  clock = 0;
  drawCard("a3");
  clock = 180;
  sandbox.answerCard("a3", "approve", {});
  assert.strictEqual(sent[0].shown_ms, 180);
});

check("a card this client never drew reports NOTHING, not zero", () => {
  // The page reloaded, or the verdict is being sent for a card whose element
  // is gone. An unknown must travel as an unknown: a zero here would read as
  // the blindest possible tap.
  sandbox.cards.clear();
  sent.length = 0;
  sandbox.answerCard("never-drawn", "deny", {});
  assert.strictEqual(sent.length, 1);
  assert(!("shown_ms" in sent[0]), `sent ${JSON.stringify(sent[0])}`);
});

check("an unstamped card element reports nothing either", () => {
  const unstamped = [
    makeEl("div"), null, undefined,
    { dataset: { shownAt: "later" } }, { dataset: { shownAt: "" } },
  ];
  for (const card of unstamped) {
    assert.strictEqual(Object.keys(sandbox.shownExtra(card)).length, 0,
      `made up a number for ${JSON.stringify(card)}`);
  }
});

check("the comment still rides along with the measurement", () => {
  // #81's semantics are carried by `extra`; adding a number must not displace
  // it, because approve+comment and deny+comment mean opposite things.
  clock = 100;
  drawCard("a4");
  clock = 2600;
  sandbox.answerCard("a4", "deny", { comment: "wrong host" });
  assert.strictEqual(sent[0].comment, "wrong host");
  assert.strictEqual(sent[0].shown_ms, 2500);
});

check("the clock is monotonic, never the wall clock", () => {
  // A phone waking from sleep, an NTP step or a timezone change must not be
  // able to author a plausible-looking number. performance.now() cannot go
  // backwards; Date.now() can.
  const region = extract("// [CARD-LATENCY-START]", "// [CARD-LATENCY-END]");
  assert(region.includes("performance.now()"), "the region stopped using performance.now()");
  assert(!/\bDate\.now\(\)/.test(region), "wall-clock time crept into the measurement");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
