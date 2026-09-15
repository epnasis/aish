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
//
// Parentage is real: `remove` and `replaceWith` detach, and a node appended
// elsewhere leaves its old parent — retireThinkingRow (#374) lifts nested rows
// out of a row it is about to drop, and a fake whose `remove` was a no-op
// would pass whether or not the rows survived.
function makeElement(tag) {
  const found = new Map();
  const adopt = (node) => {
    if (node && node.parentNode) node.parentNode.detach(node);
    if (node && typeof node === "object") node.parentNode = el;
  };
  const el = {
    tagName: tag, className: "", textContent: "", innerHTML: "",
    children: [], style: {}, dataset: {}, parentNode: null,
    append(...nodes) { nodes.forEach(adopt); el.children.push(...nodes); },
    appendChild(node) { adopt(node); el.children.push(node); return node; },
    detach(node) {
      const i = el.children.indexOf(node);
      if (i >= 0) el.children.splice(i, 1);
      node.parentNode = null;
    },
    remove() { if (el.parentNode) el.parentNode.detach(el); },
    replaceWith(...nodes) {
      const parent = el.parentNode;
      if (!parent) return;
      nodes.forEach((n) => { if (n.parentNode) n.parentNode.detach(n); n.parentNode = parent; });
      parent.children.splice(parent.children.indexOf(el), 1, ...nodes);
      el.parentNode = null;
    },
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
    releasePinnedTrace() {},
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
  // The card's END as well (#374): finalizeAnswerRow and finishTrace are the
  // two places a live thinking row is dropped or finalized, and what happens to
  // the rows drawn under it is decided there. The real ones replace the stub.
  vm.runInContext(slice("function finalizeAnswerRow(t, ref, secs) {", "// [TRACE-CLOSE-END]"), sandbox);
  assert(typeof sandbox.traceStep === "function", "traceStep not extracted");
  assert(typeof sandbox.finishTrace === "function", "finishTrace not extracted");
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

// ---- where the row goes (#374) ------------------------------------------
// A retry is the SAME model call still being made, so its failed attempts
// belong under the live "Thinking…" step. Drawn beside it, the highlighted
// live box covered the timeline rail and the failure read as a detached event
// under a broken line (the owner's screenshot). Two invariants: an attempt that
// arrives while a call is open draws under that call's row, and no attempt is
// ever lost when that row is later finalized or dropped.

// The timeline's shape, as data: every .step in order, with what is under it.
// Classes come from both the string set at creation and the classList.
function classesOf(node) {
  const set = new Set((node.className || "").split(/\s+/).filter(Boolean));
  for (const c of node.classList ? node.classList._set : []) set.add(c);
  return [...set].sort();
}

function shapeOf(container) {
  return (container.children || [])
    .filter((n) => classesOf(n).includes("step"))
    .map((row) => {
      const under = (row.children || []).find((n) => classesOf(n).includes("step-under"));
      return {
        classes: classesOf(row),
        title: titleOf(row),
        sub: subOf(row),
        under: under ? shapeOf(under) : [],
      };
    });
}

function underOf(row) {
  return (row.children || []).find((n) => classesOf(n).includes("step-under"));
}

const RATE_LIMITED = {
  kind: "model_error", class: "rate_limit", status: 429, attempt: 1,
  attempts: 8, action: "retry", waited_s: 5, scope: "short",
};
const RATE_LIMITED_AGAIN = { ...RATE_LIMITED, attempt: 2, waited_s: 10 };
const GAVE_UP = {
  kind: "model_error", class: "rate_limit", status: 429, attempt: 3,
  attempts: 8, action: "give_up", retryable: true, bound: "wait_budget",
  waited_total_s: 35, wait_budget_s: 30,
};

check("an attempt that fails while the call is open draws under its Thinking… row", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "thinking_start" });
  s.traceStep(RATE_LIMITED);
  const top = rows(s);
  assert.strictEqual(top.length, 1, `expected one timeline row, got ${top.length}`);
  const think = top[0];
  assert(classesOf(think).includes("running") && classesOf(think).includes("active-step"),
    "the live row lost its highlight");
  assert(classesOf(think).includes("step-with-under"), classesOf(think).join(" "));
  const under = underOf(think);
  assert(under, "no under-slot on the thinking row");
  assert.strictEqual(under.children.length, 1);
  const err = under.children[0];
  assert(classesOf(err).includes("step-model-error"), classesOf(err).join(" "));
  assert(titleOf(err).startsWith("Model call failed — rate limit (429)"), titleOf(err));
  assert(subOf(err).includes("attempt 1 — retrying in 5s"), subOf(err));
  // The slot is a SIBLING of the row's own main, never inside it: what reads
  // the main (the gist probe, the interrupted note) must not find a nested row.
  const main = findByClass(think, ".step-main");
  assert(!(main.children || []).some((n) => classesOf(n).includes("step-under")),
    "the under-slot was put inside .step-main");
  // The thinking row's own title is still the first one found.
  assert(titleOf(think).startsWith("Thinking…"), titleOf(think));
});

check("with no call open the row stays on the timeline itself", () => {
  // The final give-up lands after the row closed on some paths, and a log
  // written before thinking_start existed never opens one: the row must not
  // vanish for want of a parent.
  const s = makeSandbox();
  s.traceStep(GAVE_UP);
  const top = rows(s);
  assert.strictEqual(top.length, 1);
  assert(classesOf(top[0]).includes("step-model-error"));
  assert(!underOf(top[0]), "a root-level error row grew an under-slot");
});

check("the attempts stay under the call once it finalizes to Thought for…", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "thinking_start" });
  s.traceStep(RATE_LIMITED);
  s.traceStep(RATE_LIMITED_AGAIN);
  s.traceStep({ kind: "thinking", secs: 7.5, gist: "Initiating Email Search", tokens: [10, 2] });
  const top = rows(s);
  assert.strictEqual(top.length, 1);
  const think = top[0];
  assert(classesOf(think).includes("step-think"));
  assert(!classesOf(think).includes("running"), "finalized row still running");
  assert(titleOf(think).startsWith("Thought for 7.5s"), titleOf(think));
  // The gist still lands: the finalize branch probes the main for an existing
  // sub-line, and a nested row's sub must not satisfy that probe.
  assert.strictEqual(subOf(think), "Initiating Email Search");
  const under = underOf(think);
  assert.strictEqual(under.children.length, 2);
  assert(subOf(under.children[1]).includes("attempt 2 — retrying in 10s"));
});

check("a Thinking… row dropped by a plain answer leaves its attempts on the timeline", () => {
  // thinking_cancel removes a row the turn turned out not to need. What was
  // drawn under it is evidence (#261) and steps out into the row's place.
  const s = makeSandbox();
  s.traceStep({ kind: "thinking_start" });
  s.traceStep(RATE_LIMITED);
  s.traceStep(RATE_LIMITED_AGAIN);
  s.traceStep({ kind: "thinking_cancel", secs: 12, tokens: [10, 2] });
  const top = rows(s);
  assert.strictEqual(top.length, 2, shapeOf(s.currentTrace.inner).map((r) => r.title).join(" | "));
  assert(top.every((r) => classesOf(r).includes("step-model-error")));
  assert(top.every((r) => r.parentNode === s.currentTrace.inner), "lifted rows not re-parented");
  assert(subOf(top[0]).includes("attempt 1") && subOf(top[1]).includes("attempt 2"), "order lost");
  assert.strictEqual(s.currentTrace.thinkingRow, null);
});

check("a Thinking… row that became the answer keeps its attempts under Answered in…", () => {
  // Live, the tokens stream into the thinking row before thinking_cancel
  // lands ([ANSWER-OPEN] relabels it and marks isAnswer); the row is then
  // finalized in place, and the attempts stay where the owner watched them.
  const s = makeSandbox();
  s.traceStep({ kind: "thinking_start" });
  s.traceStep(RATE_LIMITED);
  s.currentTrace.thinkingRow.titleEl.textContent = "Answering…";
  s.currentTrace.thinkingRow.isAnswer = true;
  s.traceStep({ kind: "thinking_cancel", secs: 23, tokens: [10, 2] });
  const top = rows(s);
  assert.strictEqual(top.length, 1);
  assert(classesOf(top[0]).includes("step-answer"), classesOf(top[0]).join(" "));
  assert(titleOf(top[0]).startsWith("Answered in 23s"), titleOf(top[0]));
  assert.strictEqual(underOf(top[0]).children.length, 1);
});

check("a turn that gave up keeps the failure when the card is closed on it", () => {
  // The give-up path writes no thinking_cancel: the loop raises out of the
  // model call and the turn's `error` closes the card with the row still
  // open. finishTrace drops the row; the attempts must not go with it.
  const s = makeSandbox();
  s.traceStep({ kind: "thinking_start" });
  s.traceStep(RATE_LIMITED);
  s.traceStep(GAVE_UP);
  const t = s.currentTrace;
  s.finishTrace(true);
  const top = shapeOf(t.inner);
  assert.strictEqual(top.length, 2, JSON.stringify(top));
  assert(top.every((r) => r.classes.includes("step-model-error")));
  assert(top[1].sub.includes("waited 35s"), top[1].sub);
  assert.strictEqual(s.currentTrace, null);
});

// Hot and cold render identically (L2). Replay walks thinking_start in file
// order exactly as the live path received it — verified against the owner's
// own logs: thinking_start → model_error × N → thinking | thinking_cancel —
// so the same event list through the same code must give the same shape. The
// only thing that differs between the paths is the `replaying` flag.
function shapeAfter(events, replaying) {
  const s = makeSandbox();
  s.replaying = replaying;
  const cards = [];
  for (const ev of events) {
    if (ev === "ERROR" || ev === "DONE") {
      // The card AFTER it closed: finishTrace is where an open row is dropped.
      const t = s.currentTrace;
      s.finishTrace(ev === "ERROR");
      cards.push(shapeOf(t.inner));
      continue;
    }
    s.traceStep(ev);
  }
  return cards;
}

check("hot and cold agree: a retried call that went on to call tools", () => {
  const events = [
    { kind: "thinking_start" }, RATE_LIMITED, RATE_LIMITED_AGAIN,
    { kind: "thinking", secs: 7.5, gist: "Initiating Email Search", tokens: [10, 2] },
    "DONE",
  ];
  const hot = shapeAfter(events, false);
  const cold = shapeAfter(events, true);
  assert.deepStrictEqual(cold, hot);
  assert.strictEqual(hot[0][0].under.length, 2, JSON.stringify(hot));
});

check("hot and cold agree: thinking → failure → give up → you retried → answer", () => {
  // Two cards: the attempt that gave up (closed by its `error`), then the
  // rerun the Retry press opened — the press is the first row of the second
  // card on both paths (reconstruct_events holds it until the turn it opens).
  const events = [
    { kind: "thinking_start" }, RATE_LIMITED, GAVE_UP, "ERROR",
    { kind: "retry", by: "owner", attempt: 2, previous: { ended: "failed", failure: "rate_limit" } },
    { kind: "thinking_start" }, RATE_LIMITED,
    { kind: "thinking_cancel", secs: 9, tokens: [10, 2] },
    "DONE",
  ];
  const hot = shapeAfter(events, false);
  const cold = shapeAfter(events, true);
  assert.deepStrictEqual(cold, hot);
  assert.strictEqual(hot.length, 2);
  // Card 1: both attempts on the timeline, the row that held them gone.
  assert.deepStrictEqual(hot[0].map((r) => r.classes.includes("step-model-error")), [true, true]);
  // Card 2: the press first, then the lifted attempt; no Thinking… row left.
  assert(hot[1][0].classes.includes("step-retry"), JSON.stringify(hot[1]));
  assert(hot[1][1].classes.includes("step-model-error"), JSON.stringify(hot[1]));
  assert(!hot[1].some((r) => r.title === "Thinking…"));
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
