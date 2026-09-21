// Choreography pin: the turn's card exists from the moment the turn does (#398).
//
// The live card used to be built by the turn's FIRST STEP, and the gap before
// that — the whole first model call, minutes on a slow local model — was
// covered by a separate "✻ working…" line above the composer, which then hid
// itself once the card appeared (#10). Two "the model is working" surfaces for
// one turn, one after the other. Now the `user` event builds the card in its
// "Working…" head state and the first step lands INSIDE it; the bottom line is
// gone, so every state it covered has to be on the card, and this file pins
// each of them against the REAL dispatcher:
//
//   1. the card exists after `user` and before any `step`, after the bubble;
//   2. "Waiting for approval…" on the card while a card is pending, with the
//      body class that lets the sticky card yield, and Stop still on the head;
//   3. a turn that fails before its first step closes the card honestly;
//   4. a replayed `user` builds the same card, from the stamp, no step needed;
//   5. the status channel's label and token count land on the card.
//
// Real code throughout: handle (the `user`, `step`, `status`, `error`,
// `stopped`, `done`, `approval_*` cases), ensureTrace, finishTrace, traceStep,
// updateTraceHead, traceStatusLine, onStatus, syncPendingApproval,
// onApprovalRequest, onApprovalResolved, onDone, onStopped, setBusy. The
// harness supplies nothing the runtime computes: the card, its text and the
// body class are read back from the DOM the real functions built.
//
// Run manually: node tests/js/test_choreo_turn_card.js
"use strict";

const vm = require("vm");
const { appSource, extract, checks } = require("./harness");

const { ok, report } = checks();
const src = appSource();

// A named top-level function, brace-matched — for handlers with no fence.
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

function findByClass(root, cls) {
  for (const child of root.children) {
    if (child.classList && child.classList.contains(cls)) return child;
    const deeper = child.children ? findByClass(child, cls) : null;
    if (deeper) return deeper;
  }
  return null;
}

// An element that keeps a real tree: appendChild/remove move nodes between
// parents, so "is the card still in the transcript?" is answered by the DOM
// and not by a flag. className and classList are ONE truth (ensureTrace writes
// the string, updateTraceHead reads the list). querySelector memoizes per
// selector and looks through the children's memos first, so the Stop control
// the head wired at creation is the same object the card finds later.
function makeElement(tag) {
  const found = new Map();
  const classes = new Set();
  const el = {
    tagName: tag, textContent: "", innerHTML: "", hidden: false, disabled: false,
    children: [], style: {}, dataset: {}, parentNode: null, onclick: null,
    append(...nodes) { nodes.forEach((n) => el.appendChild(typeof n === "string" ? { textContent: n } : n)); },
    appendChild(n) {
      if (n.remove) n.remove();
      n.parentNode = el;
      el.children.push(n);
      return n;
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
      const at = parent.children.indexOf(el);
      parent.children.splice(at, 1, ...nodes);
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
        on ? classes.add(c) : classes.delete(c);
        return on;
      },
    },
    querySelector(sel) {
      // Real nodes first (a `.step` row traceRow appended), so "is there a
      // step?" is answered by the tree and can be NO. Only the slots the head
      // builds as innerHTML (.trace-title, .trace-stop, …) are memoized.
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

function world() {
  const messagesEl = makeElement("div");
  const body = makeElement("body");
  body.style = { setProperty() {}, removeProperty() {} };
  const sent = [];
  const notes = [];
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
    // --- the turn state the dispatcher writes ---
    replaying: false,
    turnStart: 0,
    answerTiming: 0,
    currentTurnId: "",
    currentTrace: null,
    turnAnchorEl: null,
    userCmdBlock: null,
    sawAnswer: false,
    answerAbandoned: false,
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
    act: (msg) => sent.push(msg),
    notify: (title) => notes.push(title),
    closeAnswer() {},
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
    startStepTimer() {},
    clearStepTimer() {},
    stepOutput() {},
    finalizeAnswerRow() {},
    addUserMsg(text) { const b = makeElement("div"); b.className = "msg user"; b.textContent = text; messagesEl.appendChild(b); return b; },
    addSystemMsg(kind, text) { const b = makeElement("div"); b.className = "msg system-note"; b.textContent = text; messagesEl.appendChild(b); return b; },
    addErrorMsg(text) { const b = makeElement("div"); b.className = "msg error"; b.textContent = text; messagesEl.appendChild(b); return b; },
    addMsg(kind, text) { const b = makeElement("div"); b.className = "msg " + kind; b.textContent = text; messagesEl.appendChild(b); return b; },
    renderMarkdown: (t) => ({ md: t }),
    highlightFences() {},
    attachAnswerTools() {},
    // dispatcher cases this file never reaches
    onHello() {}, onReplay() {}, settleFreshShares() {}, onToken() {}, onDelivery() {},
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
  // pinTrace … traceStep/traceRow/accountStepTime/replayedTurnStart, as shipped.
  load(extract(src, "function pinTrace(t) {", "const WRAP_SVG"));
  // mmss + the [TRACE-STATUS] fence + updateTraceHead.
  load(extract(src, "function mmss(sec) {", "// The answer streamed into this"));
  for (const name of [
    "fmtSecs", "fmtTokens", "onDone", "onStopped", "onStatus", "syncPendingApproval",
    "updateDot", "setBusy", "markStopping", "unmarkStopping",
    "onApprovalRequest", "onApprovalResolved",
  ]) load(fnSource(name));

  const w = { sandbox, messagesEl, body, sent, notes, clock };
  w.handle = (event) => sandbox.handle(event);
  w.card = () => messagesEl.children.find((c) => c.classList.contains("trace")) || null;
  w.cards = () => messagesEl.children.filter((c) => c.classList.contains("trace"));
  // Pure DOM reads: what the head SAYS, painted by the real handlers, never
  // re-derived by the test.
  w.title = (card) => card.querySelector(".trace-title").textContent;
  w.sub = (card) => card.querySelector(".trace-sub").textContent;
  w.stop = (card) => card.querySelector(".trace-stop");
  return w;
}

// ---- 1. The card exists after `user` and before any step -------------------
{
  const w = world();
  const s = w.sandbox;
  ok("no card before the turn", w.card() === null);
  w.handle({ type: "user", text: "how big is the log?", turn: "t1", ts: w.clock.now / 1000, at: w.clock.now / 1000 });
  const card = w.card();
  ok("the `user` event alone builds the live card", card !== null && card.classList.contains("live"));
  ok("…owned by [TRACE-OPEN]: currentTrace is that card", s.currentTrace && s.currentTrace.el === card);
  ok("…after the prompt bubble, as the turn's last child",
    w.messagesEl.children[0].className === "msg user" && w.messagesEl.lastElementChild === card);
  ok("…in its Working… head state", w.title(card) === "Working…");
  ok("…with its clock painted at once, not at the ticker's first tick", w.sub(card) === "0:00");
  ok("…stamped with the turn it belongs to", s.currentTrace.turnId === "t1");
  ok("…counting from the turn's own start", s.currentTrace.startedAt === w.clock.now && s.currentTrace.originKnown === true);
  const stop = w.stop(card);
  ok("…with Stop wired on the head", typeof stop.onclick === "function" && stop.disabled === false);
  stop.onclick({ stopPropagation() {} });
  ok("…and pressing it asks the server to stop", w.sent.some((m) => m.type === "stop"));
  ok("…which the head reflects as Stopping…", w.title(card) === "Stopping…");
  s.unmarkStopping(s.currentTrace);
  ok("the connection dot knows the session is busy", s.clientBusy === true);
  ok("no working… line anywhere but the card",
    !w.messagesEl.children.some((c) => (c.textContent || "").includes("working…")));

  // The first step lands INSIDE the card the turn already has.
  w.clock.now += 90_000; // a slow first model call
  w.handle({ type: "step", kind: "thinking_start" });
  ok("the first step joins the same card, it does not build another", w.cards().length === 1 && w.card() === card);
  ok("…and the head now says Thinking…", w.title(card) === "Thinking…");
  ok("…with the wait before the step on the clock", w.sub(card).startsWith("1:30"));
}

// ---- 2. Waiting for approval, on the card, with Stop still on it -----------
{
  const w = world();
  const s = w.sandbox;
  w.handle({ type: "user", text: "delete the stale branch", turn: "t2" });
  w.handle({ type: "step", kind: "thinking_start" });
  w.handle({ type: "step", kind: "thinking", secs: 2, say: "Deleting the stale branch." });
  w.handle({ type: "step", kind: "tool_start", name: "run_command", summary: "git branch -D x", command: "git branch -D x" });
  const card = w.card();
  w.handle({ type: "approval_request", id: "a1", kind: "command", command: "git branch -D x" });
  ok("the head says Waiting for approval… while a card is pending", w.title(card) === "Waiting for approval…");
  ok("…the body carries the class that lets the sticky card yield", w.body.classList.contains("awaiting-approval"));
  ok("…the card is still live, so Stop is still shown (CSS hides it only off a live card)",
    card.classList.contains("live") && w.stop(card).disabled === false && typeof w.stop(card).onclick === "function");
  ok("…and the approval card sits in the transcript", w.messagesEl.children.some((c) => c.className === "card"));
  w.handle({ type: "approval_resolved", id: "a1" });
  ok("resolved: the head goes back to describing the turn", w.title(card) !== "Waiting for approval…");
  ok("…and the body class goes with the card it yielded to", !w.body.classList.contains("awaiting-approval"));
  ok("…pendingCards is back to zero", s.pendingCards === 0);
}

// ---- 3. A turn that fails before its first step closes honestly ------------
{
  const w = world();
  const s = w.sandbox;
  w.handle({ type: "user", text: "hello", turn: "t3" });
  const card = w.card();
  ok("(the early card is live)", card.classList.contains("live"));
  w.handle({ type: "error", text: "model unavailable: connection refused" });
  ok("an error before any step closes the card", s.currentTrace === null && !card.classList.contains("live"));
  ok("…with the failure mark in its status slot", card.querySelector(".trace-status").innerHTML.includes('data-icon="denied"'));
  ok("…kept as the door to the turn's record (it has an id)", card.parentNode === w.messagesEl);
  ok("…the error message follows it", w.messagesEl.lastElementChild.className === "msg error");
  ok("…and the session is no longer busy", s.clientBusy === false && s.taskErrored === true);
}
{
  const w = world();
  const s = w.sandbox;
  w.handle({ type: "user", text: "hello", turn: "t4" });
  const card = w.card();
  w.handle({ type: "stopped" });
  ok("a `stopped` before any step closes the card too", s.currentTrace === null && !card.classList.contains("live") && s.clientBusy === false);
}
{
  const w = world();
  const s = w.sandbox;
  w.handle({ type: "user", text: "!ls", turn: "t5" });
  const card = w.card();
  ok("a ! command turn has the card while it runs — Stop reaches the command", card !== null && card.classList.contains("live"));
  ok("…stamped with NO turn id: a ! turn's `done` names no record (its replay carries none either)", s.currentTrace.turnId === "");
  w.handle({ type: "done", result: "" });
  ok("…and its empty `done` takes the card away again, as the replayed ! turn will", card.parentNode === null && w.card() === null);
}

// ---- 4. A replayed turn builds the same card, no step required --------------
{
  const w = world();
  const s = w.sandbox;
  const began = (w.clock.now - 600_000) / 1000;
  s.replaying = true;
  w.handle({ type: "user", text: "how big is the log?", turn: "t6", ts: began, at: began });
  const card = w.card();
  ok("a replayed `user` builds the card before any replayed step", card !== null && card.classList.contains("live"));
  ok("…from the stamp the server carries, not from the steps", s.currentTrace.startedAt === began * 1000 && s.currentTrace.originKnown === true);
  ok("…counting the whole turn so far", w.sub(card).startsWith("10:00"));
  w.handle({ type: "step", kind: "thinking_start" });
  w.handle({ type: "step", kind: "thinking_cancel", secs: 4, tokens: [100, 20] });
  s.sawAnswer = true;
  w.handle({ type: "done", result: "about 2 MB" });
  s.replaying = false;
  ok("…and finishes as the live turn does: one finished card, not live",
    w.cards().length === 1 && !card.classList.contains("live") && s.currentTrace === null);
  ok("…summarising the same turn (text-only: Answered, with its usage)",
    w.title(card) === "Answered" && w.sub(card) === "↑100 ↓20");
}

// ---- 5. The status channel lands on the card ---------------------------------
{
  const w = world();
  w.handle({ type: "user", text: "sum it up", turn: "t7" });
  const card = w.card();
  w.handle({ type: "status", state: "working", label: "wrapping up" });
  ok("a phase label with nothing else to say becomes the head text", w.title(card) === "Wrapping up…");
  w.handle({ type: "status", state: "working", label: "wrapping up", tokens: 1200 });
  ok("…and the streamed token count rides the sub line", w.sub(card).includes("↓1.2k"));
  w.handle({ type: "status", state: "working", label: "wrapping up", note: "listing the three findings" });
  ok("…while a thinking gist outranks the label", w.title(card) === "listing the three findings");
  w.handle({ type: "status", state: "idle" });
  ok("idle clears the label and the running count", w.title(card) === "listing the three findings" && !w.sub(card).includes("↓"));
  w.handle({ type: "step", kind: "thinking_start" });
  w.handle({ type: "status", state: "working", label: "thinking" });
  ok("the Thinking… row outranks the label that always accompanies it", w.title(card) === "Thinking…");
}

report("test_choreo_turn_card.js");
