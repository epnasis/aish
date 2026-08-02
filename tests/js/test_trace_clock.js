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
  vm.runInContext(fnSource("replayedTurnStart"), sandbox);
  assert(typeof sandbox.accountStepTime === "function", "accountStepTime not extracted");
  sandbox.advance = (ms) => { state.clock += ms; };
  sandbox.at = () => state.clock;
  sandbox.elapsedText = () => {
    sandbox.updateTraceHead(sandbox.currentTrace);
    // The live header puts elapsed on the sub line: "m:ss[ · ↑N ↓M]".
    return sandbox.currentTrace.el.querySelector(".trace-sub").textContent.split(" · ")[0];
  };
  // The FINISHED header, which reports the summed work rather than the wall
  // clock (finishTrace drops the "live" class and re-renders — the drop is all
  // updateTraceHead branches on).
  sandbox.workedText = () => {
    sandbox.currentTrace.el.classList.remove("live");
    sandbox.updateTraceHead(sandbox.currentTrace);
    return sandbox.currentTrace.el.querySelector(".trace-title").textContent;
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

// The swipe case: the replayed transcript carries the turn's real start, so the
// rebuilt card must count from THERE — not from the sum of the steps, which is
// the work alone and always less than the turn (the in-flight step, the approval
// wait, the answer streaming). That shortfall is why swiping away and back used
// to wind the clock backwards instead of resetting it.
check("a stamped replay counts the whole turn, not just the work in it", () => {
  const s = makeSandbox();
  const began = s.at() - 10 * MINUTE;
  s.replaying = true;
  s.turnStart = s.replayedTurnStart({ type: "user", text: "hi", ts: began / 1000 });
  s.traceStep({ kind: "thinking_start" });
  s.traceStep({ kind: "thinking", secs: 230 });
  s.traceStep({ kind: "tool_start", name: "run_command", summary: "make" });
  s.traceStep({ kind: "tool", name: "run_command", secs: 40, ok: true, summary: "make" });
  s.traceStep({ kind: "thinking_start" }); // still running: unbooked, and so are the gaps
  s.replaying = false;
  assert.strictEqual(s.elapsedText(), "10:00", "the turn's own start, not 270s of steps");
  s.advance(30000);
  assert.strictEqual(s.elapsedText(), "10:30", "and the live tail carries on from it");
});

check("the same replay landing twice does not double-count", () => {
  const s = makeSandbox();
  const began = s.at() - 5 * MINUTE;
  const replay = () => {
    s.currentTrace = null; // the rebuild drops the card with the DOM
    s.replaying = true;
    s.turnStart = s.replayedTurnStart({ type: "user", text: "hi", ts: began / 1000 });
    s.traceStep({ kind: "thinking_start" });
    s.traceStep({ kind: "thinking", secs: 120 });
    s.replaying = false;
  };
  replay();
  assert.strictEqual(s.elapsedText(), "5:00");
  replay(); // swipe away, swipe back
  assert.strictEqual(s.elapsedText(), "5:00", "the clock is where it was, not 7:00");
});

check("an unusable stamp falls back to reconstructing from the steps", () => {
  const s = makeSandbox();
  const ev = (ts) => s.replayedTurnStart({ type: "user", text: "hi", ts });
  assert.strictEqual(ev(undefined), 0, "a log written before the stamp existed");
  assert.strictEqual(ev(s.at() / 1000 + 3600), 0, "a start in the future would count down");
  assert.strictEqual(ev(0), 0);
  assert.strictEqual(ev("nonsense"), 0);
  // …and with no origin the old reconstruction still runs.
  s.replaying = true;
  s.traceStep({ kind: "thinking_start" });
  s.traceStep({ kind: "thinking", secs: 95 });
  s.replaying = false;
  assert.strictEqual(s.elapsedText(), "1:35");
});

// Writing the answer is a turn like any other and is usually the longest one.
// It arrives as `thinking_cancel` (the terminal, text-only turn emits no
// "thinking" step), and while its seconds went unbooked the finished header
// contradicted the timeline printed directly beneath it: "Worked for 5.8s" over
// a row reading "Answered in 23s".
//
// The durations are the ones the reporting turn actually recorded — the YouTube
// digest in session-20260802-132456-439726.jsonl — rather than round numbers.
// Its task_start (13:25:04) to its answer (13:25:33) is 29s of wall clock, so
// the fixed total is also the first one that agrees with the turn's own length.
const REAL_TURN = [
  { kind: "thinking_start" },
  { kind: "thinking", secs: 3.6145043335855007, tokens: [15807, 43],
    gist: "Analyzing the Video's Content" },
  { kind: "tool_start", name: "youtube_analyze", summary: "" },
  { kind: "tool", name: "youtube_analyze", secs: 2.212645374238491, ok: true, summary: "" },
  { kind: "thinking_start" },
  { kind: "thinking_cancel", secs: 22.97553041577339, tokens: [26247, 1391] },
];

check("the answer turn counts toward the finished total", () => {
  const s = makeSandbox();
  s.turnStart = s.at();
  REAL_TURN.forEach((step) => s.traceStep(step));
  assert.strictEqual(s.workedText(), "Worked for 29s", "3.6 + 2.2 + 23, not 5.8");
});

check("a replayed turn totals the same as the live one", () => {
  const s = makeSandbox();
  s.replaying = true;
  REAL_TURN.forEach((step) => s.traceStep(step));
  s.replaying = false;
  assert.strictEqual(s.workedText(), "Worked for 29s", "cold replay must not report less");
});

check("a replayed done reports no answer timing", () => {
  const s = makeSandbox();
  s.replaying = true;
  s.turnStart = s.replayedTurnStart({ type: "user", text: "hi", ts: (s.at() - MINUTE) / 1000 });
  s.onDone({ result: "an answer from an hour ago" });
  s.replaying = false;
  assert.strictEqual(s.answerTiming, 0, "transcript age is not how long the turn took");
});

check("the user handler is wired to the stamp", () => {
  assert(
    src.includes("turnStart = replaying ? replayedTurnStart(event) : Date.now();"),
    "the `user` case must take a replayed turn's origin from the event"
  );
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
