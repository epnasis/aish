// Choreography pin: the live activity trace must not outlive its transcript
// (issue #181 phase 3).
//
// `currentTrace` is the open "Working…" timeline of the turn on screen — steps
// and command output are appended to whatever object it points at. Only
// finishTrace ever cleared it, and the replay landing that swaps a STASHED view
// back in skips the rebuild loop entirely (nothing calls finishTrace on that
// path), so a trace opened in chat A survived a switch to chat B and kept
// receiving A's steps and stream output — drawn into a transcript that is no
// longer on screen, with its 1-second header ticker still running.
//
// The counterweight is the "noop" landing: the re-replay every phone unlock
// produces lands there while a turn may genuinely still be running, so it must
// change NOTHING. What makes that exemption safe is pinned here too — a session
// switch can never produce a noop landing, because enterSession resets the
// fingerprint whenever the name changes.
//
// Real code throughout: ensureTrace, finishTrace, traceStep, traceRow,
// traceStream, resetLiveTurn, onReplay and enterSession, over harness.js's
// hostile world with a DOM rich enough for the trace box.
//
// Run manually: node tests/js/test_choreo_trace_switch.js
"use strict";

const { sessionWorld, fakeElement, checks } = require("./harness");

const { ok, report } = checks();

// The trace box queries its own children by selector (".trace-inner",
// ".trace-stop", ".trace-status"). One memoized element per selector is enough
// to be a faithful stand-in — and keeps identity stable across lookups.
function richElement(tag) {
  const el = fakeElement(tag);
  const found = new Map();
  el.querySelector = (sel) => {
    if (!found.has(sel)) found.set(sel, richElement("div"));
    return found.get(sel);
  };
  el.querySelectorAll = () => [];
  el.insertBefore = () => {};
  return el;
}

function traceWorld() {
  const streamed = []; // {term, text} — where output actually landed
  const inline = [];   // stream text with no block to land in
  const document_ = {
    visibilityState: "visible",
    get hidden() { return false; },
    createElement: (tag) => richElement(tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: text }),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    head: richElement("head"),
    body: richElement("body"),
  };
  const w = sessionWorld({
    visible: true,
    globals: {
      document: document_,
      FINE_POINTER: false,
      replaying: false,
      turnStart: 0, // the live card's clock origin (0 = derive it from now)
      currentTrace: null,
      turnAnchorEl: null,
      lastUserPrompt: "",
      // trace collaborators that need a real browser (or are the hot path)
      updateTraceHead() {},
      updateScrollHints() {},
      measurePinnedTrace() {},   // needs offsetHeight + ResizeObserver
      releasePinnedTrace() {},
      refreshStatusline() {},
      scrollToEnd() {},
      scheduleStreamRender() {},
      appendTermLines: (term, text) => streamed.push({ term, text }),
      addStreamLine: (text) => inline.push(text),
      // replay collaborators
      stopSpeaking() {},
      removeCwdChip() {},
      clearQueueChips() {},
      snapViewportSoon() {},
      reportViewport() {},
      handle() {}, // the rebuild loop is not what this scenario is about
      renderMarkdown: (text) => ({ md: text }),
      highlightFences() {},
      attachAnswerTools() {},
      finalizeAnswerRow() {},
      traceSvg: () => "",
      SPINNER: "",
      TOOL_META: {},
      send() {},
    },
  });

  w.load("// [VIEWCACHE-START]", "// [VIEWCACHE-END]");
  w.load("let answerEl = null;", "function handle(event) {");
  w.load("// [REPLAY-LANDING-START]", "// [REPLAY-LANDING-END]");
  w.load("// [TRACE-OPEN-START]", "// [TRACE-OPEN-END]");
  w.load("// [TRACE-CLOSE-START]", "// [TRACE-CLOSE-END]");
  w.load("function pinTrace(t) {", "function appendTermLines");

  const s = w.sandbox;
  s.currentSession = "a.jsonl";
  w.streamed = streamed;
  w.inline = inline;
  // A live trace with a running command, exactly as a turn builds it: the step
  // opens the trace, the command block becomes its pending output target.
  w.openTrace = () => {
    s.traceStep({ kind: "thinking_start" });
    const term = richElement("div");
    s.currentTrace.pending = { name: "run_command", term };
    return s.currentTrace;
  };
  w.tickers = () => w.armed((t) => t.repeating).length;
  return w;
}

const REPLAY = { type: "replay", events: [{ type: "user", text: "b's question" }] };

// ---- 1. A switch that REUSES a stashed view -------------------------------
// The landing that skips the rebuild loop — the one where nothing else would
// have closed the trace.
{
  const w = traceWorld();
  const s = w.sandbox;
  const traceA = w.openTrace();
  ok("chat A has a live trace", s.currentTrace === traceA && traceA.started === 1);
  ok("…with its header ticker running", w.tickers() === 1);
  s.traceStream("output from A");
  ok("…and its command output lands in A's block", w.streamed.length === 1);

  // Switch to B, whose rendered view is stashed: the replay swaps the nodes in
  // and never walks the events.
  const stashed = fakeElement("div");
  s.enterSession("b.jsonl", { source: "hello", stash: true });
  s.viewCache.set("b.jsonl", { nodes: [stashed], fp: s.replayFp(REPLAY), renderedAnswers: 0 });
  s.onReplay(REPLAY);
  ok("the stashed view came back (the reuse landing)",
    w.el.children.length === 1 && w.el.children[0] === stashed);

  ok("A's trace is no longer the live one", s.currentTrace === null);
  ok("…and its ticker was cleared, not left running forever", w.tickers() === 0);

  s.traceStream("late output from A");
  ok("A's late output cannot reach A's command block", w.streamed.length === 1);
  ok("…it falls through to the inline path instead", w.inline.length === 1);

  s.traceStep({ kind: "thinking_start" });
  ok("a late step opens a NEW trace instead of joining A's",
    s.currentTrace !== null && s.currentTrace !== traceA);
  ok("…and A's step count is untouched", traceA.started === 1);
}

// ---- 2. A switch that REBUILDS ------------------------------------------
// The common landing. It replays events (which would eventually call
// finishTrace on the first `user` turn), but a replay carrying no user turn —
// an empty or trace-only transcript — used to leave the trace open just the same.
{
  const w = traceWorld();
  const s = w.sandbox;
  const traceA = w.openTrace();
  s.enterSession("b.jsonl", { source: "hello", stash: true });
  s.onReplay({ type: "replay", events: [] });
  ok("an empty rebuild still closes the outgoing trace", s.currentTrace === null);
  s.traceStream("late output from A");
  ok("…so A's block receives nothing further", w.streamed.length === 0);
  ok("…and A kept its own steps", traceA.started === 1);
}

// ---- 3. The reconnect: a noop landing keeps the running turn --------------
// Same session, same fingerprint, nothing arrived since the paint. This is the
// re-replay a phone unlock produces on a chat that is still working — killing
// its trace here would be the same bug wearing a different trigger.
{
  const w = traceWorld();
  const s = w.sandbox;
  const traceA = w.openTrace();
  s.viewFp = s.replayFp(REPLAY);
  s.viewDirty = false;
  s.onReplay(REPLAY);

  ok("the live trace survives a noop landing", s.currentTrace === traceA);
  ok("…with its ticker still running", w.tickers() === 1);
  s.traceStream("more output");
  ok("…and its command output keeps flowing", w.streamed.length === 1);
  s.traceStep({ kind: "thinking_start" });
  ok("…and a further step joins the SAME trace", s.currentTrace === traceA);
}

// ---- 4. Why the noop exemption is safe ------------------------------------
// It rests entirely on a switch never landing there. enterSession resets the
// fingerprint on every name change (#181 phase 2), so a replay for another chat
// cannot match — pinned here so the two halves can't drift apart.
{
  const w = traceWorld();
  const s = w.sandbox;
  w.openTrace();
  s.viewFp = s.replayFp(REPLAY); // the outgoing chat's own fingerprint
  s.viewDirty = false;
  s.enterSession("b.jsonl", { source: "hello" });
  ok("a switch drops the fingerprint a noop landing would need", s.viewFp === "");
  ok("…so the landing is never a noop",
    s.replayLanding({
      fp: s.replayFp(REPLAY),
      viewFp: s.viewFp,
      viewDirty: s.viewDirty,
      hasDom: true,
      cachedFp: undefined,
    }) !== "noop");
}

report("test_choreo_trace_switch.js");
