// Node-only, dependency-free check on the LIVE trace card's clock.
//
// The card is created by the turn's FIRST STEP, which on a slow local model
// lands long after you hit send, and a mid-turn replay (every phone unlock
// reconnects and replays) rebuilds that card from the log with no idea when the
// turn began. Both used to make the header's elapsed time a lie: it started
// late, and it restarted at 0:00 on a turn already ten minutes old.
//
// Runs the REAL ensureTrace / traceStep / accountStepTime / updateTraceHead /
// onDone extracted from app.js, with a frozen clock.
//
// Run manually: node tests/js/test_trace_clock.js
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

// A named top-level function, brace-matched — for the handlers that carry no
// marker fence of their own.
function fnSource(name) {
  const head = `function ${name}(`;
  const start = src.indexOf(head);
  assert(start !== -1, `function ${name} not found`);
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

// className and classList must be ONE truth here: ensureTrace stamps "trace
// live" as a className string and updateTraceHead branches on
// classList.contains("live") — a fake where those disagree renders every live
// card as a finished one and the clock under test is never even reached.
function makeElement(tag) {
  const found = new Map();
  const classes = new Set();
  const el = {
    tagName: tag, textContent: "", innerHTML: "",
    children: [], style: {}, dataset: {},
    append(...nodes) { el.children.push(...nodes); },
    appendChild(node) { el.children.push(node); return node; },
    remove() {},
    addEventListener() {},
    classList: {
      add(...cs) { cs.forEach((c) => classes.add(c)); },
      remove(...cs) { cs.forEach((c) => classes.delete(c)); },
      contains(c) { return classes.has(c); },
      toggle(c) { classes.has(c) ? classes.delete(c) : classes.add(c); },
    },
    querySelector(sel) {
      if (!found.has(sel)) found.set(sel, makeElement("div"));
      return found.get(sel);
    },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(el, "className", {
    get: () => [...classes].join(" "),
    set: (v) => { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
  });
  return el;
}

const MINUTE = 60000;

function makeSandbox() {
  const state = { clock: 1_700_000_000_000 };
  const sandbox = {
    document: {
      createElement: makeElement,
      createTextNode: (t) => ({ textContent: t }),
      body: { style: { removeProperty() {}, setProperty() {} } },
    },
    Date: { now: () => state.clock },
    messagesEl: makeElement("div"),
    replaying: false,
    turnStart: 0,
    currentTrace: null,
    turnAnchorEl: null,
    sawAnswer: true, // onDone must not try to render a fallback answer bubble
    answerAbandoned: false,
    lastUserPrompt: "",
    answerTiming: 0,
    SPINNER: "",
    TOOL_META: {},
    traceSvg: () => "",
    // Collaborators that need a real browser, or are beside the point here.
    updateScrollHints() {},
    refreshStatusline() {},
    measurePinnedTrace() {},
    releasePinnedTrace() {},
    scrollToEnd() {},
    removeQueueChip() {},
    finalizeAnswerRow() {},
    clearStepTimer() {},
    startStepTimer() {},
    stepOutput() {},
    closeAnswer() {},
    maybeSpeakReply() {},
    addSources() {},
    setBusy() {},
    setStatus() {},
    notify() {},
    anchorAnswer() {},
    finishTrace() { sandbox.currentTrace = null; },
    requestAnimationFrame() {},
    setInterval: () => 0,
    clearInterval() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("// [TRACE-OPEN-START]", "// [TRACE-OPEN-END]"), sandbox);
  vm.runInContext(slice("function pinTrace(t) {", "const WRAP_SVG"), sandbox);
  // mmss + the status-line fence + updateTraceHead, as shipped.
  vm.runInContext(slice("function mmss(sec) {", "// The answer streamed into this"), sandbox);
  vm.runInContext(fnSource("fmtSecs"), sandbox);
  vm.runInContext(fnSource("fmtTokens"), sandbox);
  vm.runInContext(fnSource("onDone"), sandbox);
  assert(typeof sandbox.accountStepTime === "function", "accountStepTime not extracted");
  sandbox.advance = (ms) => { state.clock += ms; };
  sandbox.at = () => state.clock;
  sandbox.elapsedText = () => {
    sandbox.updateTraceHead(sandbox.currentTrace);
    // The live header puts elapsed on the sub line: "m:ss[ · ↑N ↓M]".
    return sandbox.currentTrace.el.querySelector(".trace-sub").textContent.split(" · ")[0];
  };
  return sandbox;
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

check("the clock starts with the TURN, not with the card the first step builds", () => {
  const s = makeSandbox();
  s.turnStart = s.at();
  s.advance(90000); // a slow first model call: no step to draw yet
  s.traceStep({ kind: "thinking_start" });
  assert.strictEqual(s.elapsedText(), "1:30", "the wait before the first step counts");
  s.advance(30000);
  assert.strictEqual(s.elapsedText(), "2:00");
});

check("with no turn origin (a cold card) the clock starts now, never in 1970", () => {
  const s = makeSandbox();
  s.traceStep({ kind: "thinking_start" });
  assert.strictEqual(s.elapsedText(), "0:00");
});

check("a mid-turn REPLAY continues the clock instead of restarting at 0:00", () => {
  const s = makeSandbox();
  // The reconnect: no origin survives the replay, so the steps must supply it.
  s.replaying = true;
  s.traceStep({ kind: "thinking_start" });
  s.traceStep({ kind: "thinking", secs: 230 });
  s.traceStep({ kind: "tool_start", name: "web_search", summary: "bali surf" });
  s.traceStep({ kind: "tool", name: "web_search", secs: 1, ok: true, summary: "bali surf" });
  s.traceStep({ kind: "thinking_start" });
  s.traceStep({ kind: "thinking", secs: 75 });
  s.replaying = false;
  assert.strictEqual(s.elapsedText(), "5:06", "306s of replayed work = where the turn is");
  // …and the live tail carries on from there rather than from the reconnect.
  s.advance(24000);
  s.traceStep({ kind: "tool_start", name: "read_url", summary: "a page" });
  assert.strictEqual(s.elapsedText(), "5:30");
});

check("live steps never book their time twice", () => {
  const s = makeSandbox();
  s.turnStart = s.at();
  s.traceStep({ kind: "thinking_start" });
  s.advance(10000);
  s.traceStep({ kind: "thinking", secs: 8 });
  assert.strictEqual(s.elapsedText(), "0:10", "wall clock, not wall clock + step secs");
  // Parallel read-only tools overlap: their summed durations are not elapsed time.
  s.traceStep({ kind: "tool_start", name: "read_file", summary: "a.py" });
  s.traceStep({ kind: "tool_start", name: "read_file", summary: "b.py" });
  s.advance(2000);
  s.traceStep({ kind: "tool", name: "read_file", secs: 2, ok: true, summary: "a.py" });
  s.traceStep({ kind: "tool", name: "read_file", secs: 2, ok: true, summary: "b.py" });
  assert.strictEqual(s.elapsedText(), "0:12");
});

check("a finished turn releases the clock, so the next card cannot inherit it", () => {
  const s = makeSandbox();
  s.turnStart = s.at();
  s.traceStep({ kind: "thinking_start" });
  s.advance(5 * MINUTE);
  s.onDone({ result: "done" });
  assert.strictEqual(s.turnStart, 0, "onDone must clear the turn's origin");
  s.advance(MINUTE);
  s.traceStep({ kind: "thinking_start" }); // the next turn's first step
  assert.strictEqual(s.elapsedText(), "0:00", "a fresh card starts at zero");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall trace-clock checks passed");
