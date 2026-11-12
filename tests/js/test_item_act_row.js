// Node-only, dependency-free check of the row for the owner's act on an inbox
// item (contract §3.25, slice 1c of 2026-11-12): his resolve / restore applies
// with no card, so what was marked reaches him as this row, composed from the
// act's record — never relayed by the model. Runs the REAL traceStep/traceRow
// extracted from app.js, live and on replay, and compares the two (L2).
//
// Run manually: node tests/js/test_item_act_row.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);

function slice(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker);
  assert(start !== -1 && end !== -1, `markers not found: ${startMarker} … ${endMarker}`);
  return src.slice(start, end);
}

// An element that tracks real children (so rows can be inspected) and, for the
// structural selectors ensureTrace builds via innerHTML, memoizes a stand-in.
// ".step-sub" is deliberately NOT memoized — the live finalize branch probes it
// to avoid double-adding, and a memoized fake would always claim one exists.
function makeElement(tag) {
  const found = new Map();
  const el = {
    tagName: tag, className: "", textContent: "", innerHTML: "",
    children: [], style: {}, dataset: {},
    append(...nodes) { el.children.push(...nodes); },
    appendChild(node) { el.children.push(node); return node; },
    remove() {},
    addEventListener() {},
    classList: {
      _set: new Set(),
      add(...cs) { cs.forEach((c) => this._set.add(c)); },
      remove(...cs) { cs.forEach((c) => this._set.delete(c)); },
      contains(c) { return this._set.has(c); },
      toggle(c) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); },
    },
    querySelector(sel) {
      const byClass = findByClass(el, sel);
      if (byClass) return byClass;
      if (sel === ".step-sub") return null;
      if (!found.has(sel)) found.set(sel, makeElement("div"));
      return found.get(sel);
    },
    querySelectorAll() { return []; },
  };
  return el;
}

function findByClass(el, sel) {
  if (!sel.startsWith(".")) return null;
  const cls = sel.slice(1);
  for (const child of el.children || []) {
    if (typeof child.className === "string"
        && child.className.split(/\s+/).includes(cls)) return child;
    const deeper = child.children ? findByClass(child, sel) : null;
    if (deeper) return deeper;
  }
  return null;
}

function makeSandbox(replaying = false) {
  const sandbox = {
    document: { createElement: makeElement, createTextNode: (t) => ({ textContent: t }) },
    messagesEl: makeElement("div"),
    replaying,
    turnStart: 0, // the live card's clock origin (0 = derive it from now)
    currentTrace: null,
    currentTurnId: "",
    turnAnchorEl: null,
    SPINNER: "",
    TOOL_META: {},
    traceSvg: () => "",
    fmtSecs: (s) => `${s}s`,
    updateTraceHead() {},
    updateScrollHints() {},
    measurePinnedTrace() {}, // needs offsetHeight + ResizeObserver
    scrollToEnd() {},
    removeQueueChip() {},
    finalizeAnswerRow() {},
    setInterval: () => 0,
    clearInterval() {},
    requestAnimationFrame() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("// [TRACE-OPEN-START]", "// [TRACE-OPEN-END]"), sandbox);
  vm.runInContext(slice("function pinTrace(t) {", "const WRAP_SVG"), sandbox);
  assert(typeof sandbox.traceStep === "function", "traceStep not extracted");
  return sandbox;
}

// ---- the harness row ----------------------------------------------------

function rows(sandbox) {
  return (sandbox.currentTrace.inner.children || []).filter((r) => r.children);
}

function textOf(node) {
  if (typeof node === "string") return node;
  return [node.textContent || "", ...(node.children || []).map(textOf)].join("");
}

function titleOf(row) {
  const title = findByClass(row, ".step-title");
  return title ? textOf(title) : "";
}

function subOf(row) {
  const sub = findByClass(row, ".step-sub");
  return sub ? sub.textContent || "" : "";
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

const RESOLVED = {
  kind: "item_act", verb: "resolve", anchor: "3e2ea1e21b1b00", outcome: "applied",
  carded: false, items: ["needs|3e2ea1e21b1b00|club@swim.example"], what: "pay",
  party: "club@swim.example", day: "2026-11-02", ref: "3e2ea1e21b1b", call: 4, turn: 2,
};

function drawn(step, replaying = false) {
  const s = makeSandbox(replaying);
  s.traceStep(step);
  return rows(s);
}

check("an act applied with no card says what was marked, in code's words", () => {
  const [row] = drawn(RESOLVED);
  assert.equal(titleOf(row), "Marked done");
  assert.equal(subOf(row), "Pay — club@swim.example (2026-11-02) · no card — you asked for it"
    + " · say “restore” to bring it back");
  assert(row.classList.contains("step-item-act"));
  assert.equal(row.dataset.call, "4", "a tap opens the call the act belongs to");
});

check("a carded act says it was approved on a card", () => {
  const [row] = drawn({ ...RESOLVED, carded: true });
  assert.equal(subOf(row), "Pay — club@swim.example (2026-11-02) · approved on a card");
});

check("a restore reads as kept open", () => {
  const [row] = drawn({ ...RESOLVED, verb: "restore" });
  assert.equal(titleOf(row), "Kept open");
});

check("an act that did not happen says so, and why, from the record", () => {
  const [row] = drawn({ kind: "item_act", verb: "resolve", outcome: "blocked", carded: false,
                        why: "task not sent by the owner" });
  assert.equal(titleOf(row), "Not changed: marked done");
  assert.equal(subOf(row), "the task was not a message you sent");
});

check("an unknown verb or outcome still draws, as what it is", () => {
  const [row] = drawn({ kind: "item_act", verb: "frobnicate", outcome: "odd" });
  assert(titleOf(row).includes("frobnicate"));
  assert(subOf(row).includes("odd"));
});

check("live and cold draw the same row", () => {
  const live = drawn(RESOLVED, false).map((r) => [titleOf(r), subOf(r)]);
  const cold = drawn(RESOLVED, true).map((r) => [titleOf(r), subOf(r)]);
  assert.deepEqual(live, cold);
});

check("the row counts toward the turn's steps", () => {
  const s = makeSandbox();
  s.traceStep(RESOLVED);
  assert.equal(s.currentTrace.started, 1);
});

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("item act row: all checks passed");
