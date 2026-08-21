// Node-only, dependency-free parity check for the thinking-row gist subtitle.
// The `thinking` step's `gist` must land on the "Thought for Xs" row on BOTH
// paths — the live finalize-in-place branch and the replay synthesize branch —
// or hot and cold traces diverge. Runs the REAL ensureTrace/traceStep/traceRow
// extracted from app.js by marker.
//
// Run manually: node tests/js/test_trace_thinking_gist.js
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
    refreshStatusline() {},
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

// ---- the trim row -------------------------------------------------------

function rows(sandbox) {
  return (sandbox.currentTrace.inner.children || []).filter((r) => r.children);
}

function titleOf(row) {
  const title = findByClass(row, ".step-title");
  if (!title) return "";
  const parts = [title.textContent || ""];
  for (const child of title.children || []) {
    parts.push(typeof child === "string" ? child : child.textContent || "");
  }
  return parts.join("");
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

check("a trim draws a row on the turn it prepared", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "trim", policy: "budget_oldest_first", affected: 3,
                stubbed: [{ at: 1, tool: "read_url", continuation: "abc" },
                          { at: 2, tool: "web_search", continuation: "def" },
                          { at: 3, tool: "run_command", continuation: "ghi" }] });
  const row = rows(s).find((r) => titleOf(r).startsWith("Shortened"));
  assert(row, "no trim row was drawn");
  assert(titleOf(row).includes("3 earlier results"), titleOf(row));
  assert(subOf(row).includes("read them back"), subOf(row));
});

check("a trim the model cannot undo says so", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "trim", affected: 2,
                stubbed: [{ at: 1, tool: "read_url" }, { at: 2, tool: "web_search" }] });
  const row = rows(s).find((r) => titleOf(r).startsWith("Shortened"));
  assert(subOf(row).includes("cannot get them back"), subOf(row));
});

check("a partly recoverable trim does not overstate itself", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "trim", affected: 2,
                stubbed: [{ at: 1, tool: "read_url", continuation: "abc" },
                          { at: 2, tool: "web_search" }] });
  const row = rows(s).find((r) => titleOf(r).startsWith("Shortened"));
  assert(subOf(row).includes("1 of them"), subOf(row));
});

check("the row counts toward the turn's steps", () => {
  // Every row a card draws must be booked, or the finished header contradicts
  // the timeline printed under it (the turn-clock law).
  const s = makeSandbox();
  s.traceStep({ kind: "trim", affected: 1, stubbed: [{ at: 1, tool: "read_url" }] });
  assert.equal(s.currentTrace.started, 1);
});

check("one result reads as one, not as '1 results'", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "trim", affected: 1, stubbed: [{ at: 1, tool: "read_url" }] });
  const row = rows(s).find((r) => titleOf(r).startsWith("Shortened"));
  assert(titleOf(row).includes("1 earlier result "), titleOf(row));
});

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("trim row: all checks passed");
