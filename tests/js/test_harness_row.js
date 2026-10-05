// Node-only, dependency-free check of the row for aish's own words to the
// model (contract §3.17): what it is called, what it shows, and that the exact
// text is one tap away. Runs the REAL traceStep/traceRow extracted from app.js.
//
// Run manually: node tests/js/test_harness_row.js
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

function makeSandbox() {
  const sandbox = {
    document: { createElement: makeElement, createTextNode: (t) => ({ textContent: t }) },
    messagesEl: makeElement("div"),
    replaying: false,
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

const NOTE = "[aish: Your answer matches /[?]\\s*$/ (ending), so this rule applies to it. "
  + "The rule 'quick-reply-chips' requires the answer to include the pattern /aish-reply:///.\n"
  + "Add it and give the answer again.]";

check("an answer check draws a row saying what aish did", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "harness", source: "answer_check", role: "user", text: NOTE });
  const row = rows(s).find((r) => titleOf(r).includes("a check was not met"));
  assert(row, "no harness row was drawn");
  assert(row.classList.contains("step-harness-enforce"), "not drawn as enforcement");
  // The sub-line is the note's own first line, without aish's marker.
  assert(subOf(row).startsWith("Your answer matches"), subOf(row));
});

check("a note from before sources existed still draws, as a plain note", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "harness", role: "user", text: "[aish: you have reached the step limit]" });
  const row = rows(s)[0];
  assert.equal(titleOf(row), "aish told the model");
  assert.equal(subOf(row), "you have reached the step limit");
});

check("the sub-line drops the bracket framing of any note", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "harness", source: "cd", role: "user",
                text: "[I moved the session to /p with /cd — this directory is the project now]" });
  assert.equal(subOf(rows(s)[0]), "I moved the session to /p with /cd — this directory is the project now");
});

check("an unknown source is drawn, never dropped", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "harness", source: "something_new", role: "user", text: "[aish: hi]" });
  assert.equal(rows(s).length, 1);
});

check("the row counts toward the turn's steps", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "harness", source: "cd", role: "user", text: "[I moved the session to /p]" });
  assert.equal(s.currentTrace.started, 1);
});

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("harness row: all checks passed");
