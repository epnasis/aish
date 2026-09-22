// Hot/cold parity of the answer step (#403).
//
// A turn's last model call writes the answer. Live, the answer streams as
// `token` events BEFORE that call's `thinking_cancel`, so onToken relabels the
// Thinking… row "Answering…" and marks it, and the cancel finalizes it in place
// as "Answered in Xs" (`step-answer`, which the inspector opens as the last
// model call). Cold, reconstruct_events lifts the answer out to `done`, so the
// stream is `thinking_start → thinking_cancel → done` with no token between —
// and the cancel used to retire the row and take one off the step count: the
// card read "2 steps" over a timeline whose answer step was gone, and its
// reasoning could not be opened.
//
// The fix puts the fact on the record: the cancel says `answered` (the agent
// stamps it; reconstruct_events derives it for logs written before the stamp),
// and the ONE thinking_cancel handler reads it, so both paths render through the
// same code. This file feeds both orderings through the REAL dispatcher —
// handle, traceStep, onToken, onDelivery, onDone, finalizeAnswerRow,
// retireThinkingRow, finishTrace, updateTraceHead — and reads the result back
// off the DOM those functions built.
//
// Run manually: node tests/js/test_answer_row_replay.js
"use strict";

const vm = require("vm");
const { appSource, extract, checks } = require("./harness");

const { ok, report } = checks();
const src = appSource();

function fnSource(name) {
  const head = `function ${name}(`;
  const start = src.indexOf(head);
  if (start === -1) throw new Error(`function ${name} not found`);
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

// A real tree: append/remove/replaceWith move nodes between parents, so "is the
// answer row on the timeline?" is answered by the DOM. Only the innerHTML-built
// `.trace-*` slots are memoized stand-ins.
function makeElement(tag) {
  const found = new Map();
  const classes = new Set();
  const el = {
    tagName: tag, textContent: "", innerHTML: "", hidden: false, disabled: false,
    children: [], style: {}, dataset: {}, parentNode: null, onclick: null,
    append(...nodes) {
      nodes.forEach((n) => el.appendChild(typeof n === "string" ? { textContent: n } : n));
    },
    appendChild(n) {
      if (n.remove) n.remove();
      n.parentNode = el;
      el.children.push(n);
      return n;
    },
    replaceChildren(...nodes) {
      el.children.forEach((c) => { c.parentNode = null; });
      el.children = [];
      nodes.forEach((n) => el.appendChild(n));
    },
    remove() {
      if (!el.parentNode) return;
      const siblings = el.parentNode.children;
      siblings.splice(siblings.indexOf(el), 1);
      el.parentNode = null;
    },
    replaceWith(...nodes) {
      const parent = el.parentNode;
      if (!parent) return;
      nodes.forEach((n) => { if (n.remove) n.remove(); });
      parent.children.splice(parent.children.indexOf(el), 1, ...nodes);
      nodes.forEach((n) => { n.parentNode = parent; });
      el.parentNode = null;
    },
    get lastElementChild() { return el.children[el.children.length - 1] || null; },
    addEventListener() {},
    setAttribute() {},
    focus() {},
    classList: {
      add(...cs) { cs.forEach((c) => classes.add(c)); },
      remove(...cs) { cs.forEach((c) => classes.delete(c)); },
      contains(c) { return classes.has(c); },
      toggle(c, on) {
        if (on === undefined) on = !classes.has(c);
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
    },
    querySelector(sel) {
      const cls = (sel.match(/^\.([\w-]+)$/) || [])[1];
      const hit = cls && findByClass(el, cls);
      if (hit) return hit;
      for (const child of el.children) {
        if (child._found && child._found.has(sel)) return child._found.get(sel);
      }
      if (!sel.startsWith(".trace-")) return null;
      if (!found.has(sel)) found.set(sel, makeElement("div"));
      return found.get(sel);
    },
    querySelectorAll() { return []; },
    _found: found,
  };
  Object.defineProperty(el, "className", {
    get: () => [...classes].join(" "),
    set: (v) => {
      classes.clear();
      String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
  });
  return el;
}

function findByClass(root, cls) {
  const kids = [...root.children, ...(root._found ? root._found.values() : [])];
  for (const child of kids) {
    if (child.classList && child.classList.contains(cls)) return child;
    const deeper = child.children ? findByClass(child, cls) : null;
    if (deeper) return deeper;
  }
  return null;
}

function world() {
  const messagesEl = makeElement("div");
  const body = makeElement("body");
  body.style = { setProperty() {}, removeProperty() {} };
  const clock = { now: 1_700_000_000_000 };
  const sandbox = {
    messagesEl,
    document: {
      createElement: makeElement,
      createTextNode: (t) => ({ textContent: t }),
      querySelector: () => null,
      body,
    },
    Date: { now: () => clock.now },
    Number, Math, Set, Map, JSON, console,
    requestAnimationFrame() {},
    setInterval: () => 1,
    clearInterval() {},
    setTimeout: () => 1,
    clearTimeout() {},
    // --- turn and answer state the real handlers write ---
    replaying: false,
    turnStart: 0,
    answerTiming: 0,
    currentTurnId: "",
    currentTrace: null,
    turnAnchorEl: null,
    userCmdBlock: null,
    sawAnswer: false,
    answerAbandoned: false,
    answerEl: null,
    answerText: "",
    answerStableLen: 0,
    answerStableNodes: 0,
    answerCardIds: new Set(),
    answerRenderQueued: false,
    sessionTitled: true,
    taskErrored: false,
    lastUserPrompt: "",
    clientBusy: false,
    pendingCards: 0,
    cards: new Map(),
    connOk: true,
    currentSession: "a.jsonl",
    serverPainted: true,
    backfillFromBottom: -1,
    FINE_POINTER: false,
    CARD_SHORTCUTS: [],
    SPINNER: '<span class="spin"></span>',
    TOOL_META: {},
    OFFLINE_SYNC_AFTER_DONE_MS: 0,
    traceSvg: (name) => `<svg data-icon="${name}"></svg>`,
    // --- collaborators outside this choreography ---
    closeAnswer() { sandbox.answerEl = null; sandbox.answerText = ""; },
    renderAnswerFrame() {},
    act() {},
    notify() {},
    removeQueueChip() {},
    resolvePendingSend() {},
    retireQuickReplies() {},
    rememberPrompt() {},
    stripAttachmentNotes: (t) => t,
    setTitle() {},
    scrollToEnd() {},
    markSeen() {},
    offlineSyncSoon() {},
    maybeSpeakReply() {},
    addSources() {},
    anchorAnswer() {},
    endBackfill() {},
    showToast() {},
    onSessionGone() {},
    markShown() {},
    buildCommandCard() {},
    buildWriteCard() {},
    buildToolCard() {},
    buildImportCard() {},
    buildReadCard() {},
    updateScrollHints() {},
    measurePinnedTrace() {},
    releasePinnedTrace() {},
    inspectStepClick() {},
    traceInspector() {},
    stepOutput() {},
    addUserMsg(text) {
      const b = makeElement("div"); b.className = "msg user"; b.textContent = text;
      messagesEl.appendChild(b); return b;
    },
    addSystemMsg(kind, text) {
      const b = makeElement("div"); b.className = "msg system-note"; b.textContent = text;
      messagesEl.appendChild(b); return b;
    },
    addErrorMsg(text) {
      const b = makeElement("div"); b.className = "msg error"; b.textContent = text;
      messagesEl.appendChild(b); return b;
    },
    addMsg(kind, text) {
      const b = makeElement("div"); b.className = "msg " + kind; b.textContent = text;
      messagesEl.appendChild(b); return b;
    },
    renderMarkdown: (t) => ({ md: t, textContent: t }),
    highlightFences() {},
    attachAnswerTools() {},
    onHello() {}, onReplay() {}, settleFreshShares() {},
    addAnsiMsg() {}, traceStream() {}, onCommandStart() {}, onCommandEnd() {},
    addWorkspaceNote() {}, addRedactedMsg() {}, markRating() {}, onAck() {},
    onHistory() {}, renderSessions() {}, renderModels() {}, onModelChanged() {},
    renderWorkspace() {}, onBrowserView() {}, onBrowserWatch() {}, onFileList() {},
    onSessionState() {}, onSessionDeleted() {}, onTrashList() {}, onSessionRestored() {},
    onSessionChanged() {}, addQueueChip() {}, renderShares() {}, addCwdChip() {},
    removeCwdChip() {},
  };
  vm.createContext(sandbox);
  const load = (code) => vm.runInContext(code, sandbox);
  load(extract(src, "function handle(event) {", "function onSessionRenamed(event) {"));
  load(extract(src, "// [TRACE-OPEN-START]", "// [TRACE-OPEN-END]"));
  load(extract(src, "// [TRACE-CLOSE-START]", "// [TRACE-CLOSE-END]"));
  // pinTrace … traceRow/retireThinkingRow/traceStep, as shipped.
  load(extract(src, "function pinTrace(t) {", "const WRAP_SVG"));
  load(extract(src, "function mmss(sec) {", "// The answer streamed into this"));
  load(extract(src, "// [ANSWERING-COLLAPSE-START]", "// [ANSWERING-COLLAPSE-END]"));
  for (const name of [
    "fmtSecs", "fmtTokens", "onDone", "onStopped", "onStatus", "syncPendingApproval",
    "updateDot", "setBusy", "markStopping", "unmarkStopping",
    "onToken", "onDelivery", "finalizeAnswerRow",
  ]) load(fnSource(name));
  return { sandbox, messagesEl, clock, handle: (event) => sandbox.handle(event) };
}

// What the finished card shows: its head, and every row on its timeline.
function play(events, { replaying }) {
  const w = world();
  const s = w.sandbox;
  s.replaying = replaying;
  let trace = null;
  for (const ev of events) {
    w.handle(ev);
    if (s.currentTrace) trace = s.currentTrace;
  }
  s.replaying = false;
  const rows = trace.inner.children.filter((r) => r.classList.contains("step"));
  return {
    title: trace.el.querySelector(".trace-title").textContent,
    sub: trace.el.querySelector(".trace-sub").textContent,
    started: trace.started,
    rows: rows.map((r) => ({
      title: findByClass(r, "step-title").textContent,
      answer: r.classList.contains("step-answer"),
      running: r.classList.contains("running"),
    })),
    answers: w.messagesEl.children.filter((c) => c.classList.contains("answer")).length,
  };
}

const ANSWER = "The tree is about 12 m tall.";
const USER = { type: "user", text: "Solve", turn: "t1", at: 1_699_999_000 };
const THOUGHT = { type: "step", kind: "thinking", secs: 29, tokens: [35000, 170] };
// The issue's final call: 16.26s and 1330 output tokens of reasoning.
const ANSWERED = { type: "step", kind: "thinking_cancel", secs: 16.26,
                   tokens: [41955, 1330], answered: true };

// The cold stream, as reconstruct_events emits it: the answer is `done.result`,
// and nothing streams into the last call's row.
const COLD = [
  USER,
  { type: "step", kind: "thinking_start" }, THOUGHT,
  { type: "step", kind: "thinking_start" }, ANSWERED,
  { type: "done", result: ANSWER, answer: "a1" },
];
// The live stream: the same records, with the answer streamed into the row
// before the cancel lands.
const LIVE = [
  USER,
  { type: "step", kind: "thinking_start" }, THOUGHT,
  { type: "step", kind: "thinking_start" },
  { type: "token", text: ANSWER },
  ANSWERED,
  { type: "done", result: ANSWER, answer: "a1" },
];

// ---- 1. Cold: the answer step survives replay -------------------------------
{
  const cold = play(COLD, { replaying: true });
  const last = cold.rows[cold.rows.length - 1];
  ok(`cold: the answer step is on the timeline (rows: ${cold.rows.map((r) => r.title).join(" | ")})`,
    cold.rows.length === 2 && last.answer);
  ok(`cold: it reads "Answered in 16s" (got "${last.title}")`, last.title === "Answered in 16s");
  ok("cold: and is no longer running", !last.running);
  ok(`cold: the head's step count equals the rows drawn (${cold.sub})`,
    cold.started === cold.rows.length && cold.sub.startsWith(`${cold.rows.length} steps`));
  ok(`cold: the head books the answer's time (${cold.title})`, cold.title === "Worked for 45s");
  ok("cold: the answer text is drawn exactly once", cold.answers === 1);
}

// ---- 2. Live renders the same card (parity) ---------------------------------
{
  const live = play(LIVE, { replaying: false });
  const cold = play(COLD, { replaying: true });
  ok(`live: "Answered in 16s" is the last row (rows: ${live.rows.map((r) => r.title).join(" | ")})`,
    live.rows.length === 2 && live.rows[1].answer && live.rows[1].title === "Answered in 16s");
  ok("live and cold draw the same rows", JSON.stringify(live.rows) === JSON.stringify(cold.rows));
  ok(`live and cold head the card alike (${live.title} · ${live.sub} vs ${cold.title} · ${cold.sub})`,
    live.title === cold.title && live.sub === cold.sub && live.started === cold.started);
  ok("live: the answer text is drawn exactly once", live.answers === 1);
}

// ---- 3. A server that predates the stamp still answers live -----------------
{
  const unstamped = LIVE.map((e) => (e === ANSWERED ? { ...e, answered: undefined } : e));
  const live = play(unstamped, { replaying: false });
  ok("live without the stamp: the streamed token still makes the row the answer",
    live.rows.length === 2 && live.rows[1].title === "Answered in 16s");
}

// ---- 4. A call that did NOT answer still retires its row --------------------
// A Verify-rejected answer is held and never streamed; its cancel says
// answered: false. The row goes, the count goes with it, on both paths.
{
  const rejected = { type: "step", kind: "thinking_cancel", secs: 2, tokens: [10, 5], answered: false };
  const cold = play([
    USER,
    { type: "step", kind: "thinking_start" }, rejected,
    { type: "step", kind: "thinking_start" }, ANSWERED,
    { type: "done", result: ANSWER, answer: "a1" },
  ], { replaying: true });
  const live = play([
    USER,
    { type: "step", kind: "thinking_start" }, rejected,
    { type: "step", kind: "thinking_start" }, { type: "token", text: ANSWER }, ANSWERED,
    { type: "done", result: ANSWER, answer: "a1" },
  ], { replaying: false });
  ok(`a rejected call's row is retired cold (rows: ${cold.rows.map((r) => r.title).join(" | ")})`,
    cold.rows.length === 1 && cold.rows[0].title === "Answered in 16s");
  ok("…and the count matches the one row", cold.started === 1 && cold.sub.startsWith("1 step "));
  ok("…identically live", JSON.stringify(live.rows) === JSON.stringify(cold.rows) && live.sub === cold.sub);
}

// ---- 5. An unanswered cancel with no stamp retires as it always did ---------
{
  const cold = play([
    USER,
    { type: "step", kind: "thinking_start" }, THOUGHT,
    { type: "step", kind: "thinking_start" },
    { type: "step", kind: "thinking_cancel", secs: 3 },
    { type: "done", result: "" },
  ], { replaying: true });
  ok(`an unstamped cancel with no token retires its row (rows: ${cold.rows.map((r) => r.title).join(" | ")})`,
    cold.rows.length === 1 && cold.rows[0].title.startsWith("Thought for") && cold.started === 1);
}

// ---- 6. A turn that errors mid-call still drops its Thinking… row -----------
{
  const failed = play([
    USER,
    { type: "step", kind: "thinking_start" }, THOUGHT,
    { type: "step", kind: "thinking_start" },
    { type: "error", text: "model unavailable: connection refused" },
  ], { replaying: true });
  ok(`an error closes the card without an answer row (rows: ${failed.rows.map((r) => r.title).join(" | ")})`,
    failed.rows.length === 1 && !failed.rows.some((r) => r.answer));
}

report("test_answer_row_replay.js");
