// Node-only, dependency-free checks for the trace rows that report a turn's own
// trouble: `model_error` (#261) and `retry` (#339). One sandbox, because they
// share the real ensureTrace/traceStep/traceRow.
//
// The model_error row (#261).
// A failed model call used to be a grey echo bubble: live transport only,
// never written to the session log. So a call that failed and then RECOVERED
// left the trace with an unexplained gap, and a cold reload erased even the
// bubble. The row is what makes the failure visible on both paths — and a
// rendered step kind with no renderer here would open an EMPTY live trace card
// (docs/trace-contract.md §1.2), so the renderer and the record ship together.
// Runs the REAL ensureTrace/traceStep/traceRow extracted from app.js by marker.
//
// Run manually: node tests/js/test_model_error_row.js
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

// ---- the model_error row ------------------------------------------------

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

function errorRow(s, step) {
  s.traceStep(step);
  const row = rows(s).find((r) => titleOf(r).startsWith("Model call failed"));
  assert(row, "no model_error row drawn");
  return row;
}

check("a failed call that RECOVERED still draws a row", () => {
  // The path that recorded nothing at all before #261: the task succeeded, so
  // no error surfaced, and the echo reached no log.
  const s = makeSandbox();
  const row = errorRow(s, {
    kind: "model_error", class: "rate_limit", status: 429, attempt: 1,
    attempts: 3, action: "retry", waited_s: 27, scope: "short",
  });
  assert(titleOf(row).includes("rate limit"), titleOf(row));
  assert(titleOf(row).includes("429"), titleOf(row));
  assert(subOf(row).includes("retrying in 27s"), subOf(row));
});

check("a spent quota is told apart from a busy one", () => {
  // The distinction the record exists for: one is worth a Retry, the other is
  // not, and both arrive as HTTP 429.
  const s = makeSandbox();
  const row = errorRow(s, {
    kind: "model_error", class: "rate_limit", status: 429, attempt: 1,
    attempts: 3, action: "give_up", scope: "long", retryable: false,
  });
  assert(subOf(row).includes("spent, not busy"), subOf(row));
});

check("a permanent failure says retrying cannot help", () => {
  const s = makeSandbox();
  const row = errorRow(s, {
    kind: "model_error", class: "auth", status: 401, attempt: 1,
    attempts: 3, action: "give_up", retryable: false,
  });
  assert(subOf(row).includes("cannot change this"), subOf(row));
});

check("giving up after every attempt says how many were spent", () => {
  const s = makeSandbox();
  const row = errorRow(s, {
    kind: "model_error", class: "transport", attempt: 3, attempts: 3,
    action: "give_up", retryable: true,
  });
  assert(subOf(row).includes("gave up after 3 of 3"), subOf(row));
});

check("a spent wait budget says so instead of an unexplained early stop", () => {
  // Since #337 the retry is bounded by TIME, so the attempt count is usually
  // not what ended it. "gave up after 5 of 8 attempts" reads as a count that
  // stopped three short for no stated reason.
  const s = makeSandbox();
  const row = errorRow(s, {
    kind: "model_error", class: "rate_limit", status: 429, attempt: 5,
    attempts: 8, action: "give_up", retryable: true,
    bound: "wait_budget", waited_total_s: 75, wait_budget_s: 120,
  });
  assert(subOf(row).includes("waited 75s"), subOf(row));
  assert(subOf(row).includes("gave up after 5 attempts"), subOf(row));
  assert(!subOf(row).includes("of 8"), subOf(row));
});

check("a retry with no stated wait does not claim one", () => {
  // "Retry-After: 0" is legal. Rendering "retrying in 0s" would be noise
  // dressed as a fact.
  const s = makeSandbox();
  const row = errorRow(s, {
    kind: "model_error", class: "server", status: 503, attempt: 1,
    attempts: 3, action: "retry", waited_s: 0,
  });
  assert(subOf(row) === "attempt 1 — retrying", subOf(row));
});

check("an unrecognised failure still draws a legible row", () => {
  // A record written by a newer aish must never render as a blank card.
  const s = makeSandbox();
  const row = errorRow(s, { kind: "model_error", attempt: 1, attempts: 3, action: "give_up" });
  assert(titleOf(row).includes("error"), titleOf(row));
  assert(subOf(row).length > 0, "no subtitle");
});

// ---- the retry row (#339) -----------------------------------------------
// The owner pressed Retry four times and the log recorded none of them, which
// is what made the deleted attempts invisible. The press is a step on the turn
// it opened now — and a rendered kind with no renderer here opens an EMPTY live
// trace card (docs/trace-contract.md §1.2), so the row ships with the record.

function retryRow(s, step) {
  s.traceStep(step);
  const row = rows(s).find((r) => titleOf(r).includes("retried"));
  assert(row, "no retry row drawn");
  return row;
}

check("a retry says which attempt, and that the records are kept", () => {
  const s = makeSandbox();
  const row = retryRow(s, {
    kind: "retry", by: "owner", attempt: 2, turn: 4,
    previous: { records: 23, ended: "failed", failure: "rate_limit",
                error: "model unavailable: 429" },
  });
  assert(titleOf(row).includes("attempt 2"), titleOf(row));
  assert(subOf(row).includes("failed — rate limit"), subOf(row));
  assert(subOf(row).includes("kept, superseded"), subOf(row));
});

check("a failure the log did not classify is not given a name", () => {
  const s = makeSandbox();
  const row = retryRow(s, {
    kind: "retry", by: "owner", attempt: 2, previous: { records: 4, ended: "failed" },
  });
  assert(subOf(row).includes("the attempt before it failed"), subOf(row));
  assert(!subOf(row).includes("—  ·"), subOf(row));
});

check("an unrecorded ending says aish does not know", () => {
  // L8: the unknown ending has to be sayable, or the row gets handed a guess.
  const s = makeSandbox();
  const row = retryRow(s, {
    kind: "retry", by: "owner", attempt: 2, previous: { records: 4, ended: "unknown" },
  });
  assert(subOf(row).includes("did not record how"), subOf(row));
});

check("retrying an answer that finished does not call it a failure", () => {
  const s = makeSandbox();
  const row = retryRow(s, {
    kind: "retry", by: "owner", attempt: 3, previous: { records: 9, ended: "ok" },
  });
  assert(subOf(row).includes("had finished"), subOf(row));
  assert(!subOf(row).includes("failed"), subOf(row));
});

check("a record from a newer aish still draws a legible row", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "retry", attempt: 7 });
  const row = rows(s).find((r) => titleOf(r).includes("Retried"));
  assert(row, "no retry row drawn");
  assert(titleOf(row).includes("attempt 7"), titleOf(row));
  assert(subOf(row).length > 0, "no subtitle");
});

process.exit(failures ? 1 : 0);
