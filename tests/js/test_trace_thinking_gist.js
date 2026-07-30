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

// The thinking row is the one titled "Thought for …" — find its .step-sub.
// traceRow appends the title as a string child (`titleEl.append(title)`), the
// live finalize branch writes textContent — read both.
function titleText(el) {
  const parts = [el.textContent || ""];
  for (const child of el.children || []) {
    parts.push(typeof child === "string" ? child : child.textContent || "");
  }
  return parts.join("");
}

function gistSub(sandbox) {
  const inner = sandbox.currentTrace.inner;
  for (const row of inner.children) {
    if (!row.children) continue;
    const title = findByClass(row, ".step-title");
    if (title && titleText(title).startsWith("Thought for")) {
      return findByClass(row, ".step-sub");
    }
  }
  return null;
}

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}\n       ${err.message}`);
  }
}

check("LIVE path: finalize-in-place carries the gist as the row subtitle", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "thinking_start" });
  s.traceStep({ kind: "thinking", secs: 2, gist: "comparing the two configs" });
  const sub = gistSub(s);
  assert(sub, "the finalized thinking row must have a .step-sub");
  assert.strictEqual(sub.textContent, "comparing the two configs");
});

check("REPLAY path: the synthesized row carries the same subtitle", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "thinking", secs: 2, gist: "comparing the two configs" });
  const sub = gistSub(s);
  assert(sub, "the synthesized thinking row must have a .step-sub");
  assert.strictEqual(sub.textContent, "comparing the two configs");
});

check("no gist → no subtitle, on both paths", () => {
  const live = makeSandbox();
  live.traceStep({ kind: "thinking_start" });
  live.traceStep({ kind: "thinking", secs: 2 });
  assert.strictEqual(gistSub(live), null);
  const replay = makeSandbox();
  replay.traceStep({ kind: "thinking", secs: 2 });
  assert.strictEqual(gistSub(replay), null);
});

check("the thinking step stashes the model's words as header data", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "thinking_start" });
  s.traceStep({ kind: "thinking", secs: 1, say: "Checking the config.", gist: "why" });
  assert.strictEqual(s.currentTrace.turnSay, "Checking the config.");
  assert.strictEqual(s.currentTrace.turnGist, "why");
  s.traceStep({ kind: "thinking_start" });
  assert.strictEqual(s.currentTrace.turnSay, null, "a new turn clears the old words");
  assert.strictEqual(s.currentTrace.turnGist, null);
});

check("tool_start/tool maintain the running list for the header", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "tool_start", name: "read_file", summary: "a.py" });
  s.traceStep({ kind: "tool_start", name: "read_file", summary: "b.py" });
  assert.strictEqual(s.currentTrace.running.length, 2);
  s.traceStep({ kind: "tool", name: "read_file", secs: 1, ok: true, summary: "a.py" });
  assert.strictEqual(s.currentTrace.running.length, 1);
  s.traceStep({ kind: "tool", name: "read_file", secs: 1, ok: true, summary: "b.py" });
  assert.strictEqual(s.currentTrace.running.length, 0);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
