// Choreography pin: a live turn card belongs to the chat whose turn is running
// (#407).
//
// Reported: chat A is working, its live card ticking; a switch to chat B —
// idle, nothing running — showed a LIVE card in B, its clock starting at 0:00.
//
// The interleaving, from the real code: `clientBusy` is a fact about ONE chat
// (the server's `hello` states it for the chat it enters), but it outlived the
// switch. A tap on a warm chat moves identity at once and paints the prefetched
// transcript through onReplay BEFORE B's hello arrives, and the replay landing
// builds a card whenever "the view is busy and no replayed `user` built one"
// (#398) — reading A's busy. B's hello then set busy false, but nothing closed
// the card, and B's authoritative replay no-op'd onto the prefetched DOM (same
// fingerprint), which by design resets nothing.
//
// So what is pinned is the method, not the one site: busy is dropped with the
// chat it describes (enterSession, on a name change), and the landing that
// builds a card for a running turn reads only the busy of the chat on screen —
// including on the noop landing, where a busy chat's card would otherwise
// depend on A's leftover state to exist at all.
//
// Real code throughout: resumeSession, onPeek, enterSession, onHello,
// onReplay/replayLanding, resetLiveTurn, handle, ensureTrace, finishTrace,
// traceStep, updateTraceHead, setBusy. Nothing the runtime computes is supplied
// by the harness: which cards are live, and what their clock says, are read
// back from the DOM the real functions built.
//
// Run manually: node tests/js/test_choreo_idle_switch.js
"use strict";

const { sessionWorld, checks } = require("./harness");

const { ok, report } = checks();

// Descends through real children AND the memoized innerHTML slots, so a
// `.step` row appended under `.trace-inner` is found.
function findByClass(root, cls) {
  const kids = [...root.children, ...(root._found ? root._found.values() : [])];
  for (const child of kids) {
    if (child.classList && child.classList.contains(cls)) return child;
    const deeper = child.children ? findByClass(child, cls) : null;
    if (deeper) return deeper;
  }
  return null;
}

// An element that keeps a real tree (appendChild/remove/replaceChildren move
// nodes between parents), so "is a live card in the transcript?" is answered by
// the DOM and not by a flag. Same shape as test_choreo_turn_card.js's.
function makeElement(tag) {
  const found = new Map();
  const classes = new Set();
  const el = {
    tagName: tag, textContent: "", innerHTML: "", hidden: false, disabled: false,
    children: [], style: {}, dataset: {}, parentNode: null, onclick: null,
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    get childElementCount() { return el.children.length; },
    get firstElementChild() { return el.children[0] || null; },
    get lastElementChild() { return el.children[el.children.length - 1] || null; },
    append(...nodes) { nodes.forEach((n) => el.appendChild(typeof n === "string" ? { textContent: n } : n)); },
    appendChild(n) {
      if (n.remove) n.remove();
      n.parentNode = el;
      el.children.push(n);
      return n;
    },
    insertBefore(n, ref) {
      if (n.remove) n.remove();
      const at = ref ? el.children.indexOf(ref) : -1;
      n.parentNode = el;
      if (at === -1) el.children.push(n); else el.children.splice(at, 0, n);
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
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    scrollIntoView() {},
    getBoundingClientRect: () => ({ top: 0, bottom: 0, height: 0 }),
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

// A named top-level function, brace-matched — for code with no fence.
function fnSource(src, name) {
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

const T0 = 1_700_000_000_000; // A's turn starts here

function world() {
  const messagesEl = makeElement("div");
  const body = makeElement("body");
  body.style = { setProperty() {}, removeProperty() {} };
  const clock = { now: T0 };
  const FakeDate = function (...args) { return args.length ? new Date(...args) : new Date(clock.now); };
  FakeDate.now = () => clock.now;
  const sent = [];
  const w = sessionWorld({
    visible: true,
    globals: {
      messagesEl,
      Date: FakeDate,
      Number,
      document: {
        visibilityState: "visible",
        hidden: false,
        createElement: makeElement,
        createTextNode: (t) => ({ textContent: t }),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() {},
        removeEventListener() {},
        head: makeElement("head"),
        body,
      },
      // An OPEN socket, so a switch takes the server path.
      ws: { readyState: 1 },
      WebSocket: { OPEN: 1 },
      send: (msg) => { sent.push(msg); return true; },
      act: (msg) => { sent.push(msg); return true; },
      offlineMeta: new Map(),
      // --- the turn state the dispatcher writes ---
      replaying: false,
      turnStart: 0,
      answerTiming: 0,
      currentTurnId: "",
      currentTrace: null,
      turnAnchorEl: null,
      userCmdBlock: null,
      sessionTitled: true,
      lastUserPrompt: "",
      connOk: true,
      FINE_POINTER: false,
      CARD_SHORTCUTS: [],
      SPINNER: '<span class="spin"></span>',
      TOOL_META: {},
      OFFLINE_SYNC_AFTER_DONE_MS: 0,
      traceSvg: (name) => `<svg data-icon="${name}"></svg>`,
      // --- collaborators outside this choreography ---
      adjudicateHeldSends() {},
      restoreScrollPos: () => false,
      snapViewportSoon() {},
      reportViewport() {},
      stopSpeaking() {},
      earlierRow: () => makeElement("div"),
      notify() {},
      closeAnswer() {},
      removeQueueChip() {},
      retireQuickReplies() {},
      rememberPrompt() {},
      stripAttachmentNotes: (t) => t,
      scrollToEnd() {},
      offlineSyncSoon() {},
      maybeSpeakReply() {},
      addSources() {},
      anchorAnswer() {},
      onSessionGone() {},
      forgetSession() {},
      markShown() {},
      buildCommandCard() {},
      buildWriteCard() {},
      buildToolCard() {},
      buildImportCard() {},
      buildReadCard() {},
      updateScrollHints() {},
      updateScrollButton() {},
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
      onToken() {}, onDelivery() {},
      addAnsiMsg() {}, traceStream() {}, onCommandStart() {}, onCommandEnd() {},
      addWorkspaceNote() {}, addRedactedMsg() {}, markRating() {}, onAck() {},
      onHistory() {}, renderSessions() {}, renderModels() {}, onModelChanged() {},
      onBrowserView() {}, onBrowserWatch() {}, onFileList() {},
      onSessionState() {}, onSessionDeleted() {}, onTrashList() {}, onSessionRestored() {},
      onSessionChanged() {}, addQueueChip() {}, addCwdChip() {},
      removeCwdChip() {},
    },
  });
  const src = w.src;
  w.load("// [VIEWCACHE-START]", "// [VIEWCACHE-END]");
  w.load("// [PREFETCH-START]", "// [PREFETCH-END]");
  w.load("async function openCachedSession(name) {", "\n// First paint.");
  w.load("function onHello(event) {", "\n// Multi-connection (#102)");
  w.load("let answerEl = null;", "function handle(event) {");
  w.load("function handle(event) {", "function onSessionRenamed(event) {");
  w.load("// [REPLAY-LANDING-START]", "// [REPLAY-LANDING-END]");
  w.load("// [TRACE-OPEN-START]", "// [TRACE-OPEN-END]");
  w.load("// [TRACE-CLOSE-START]", "// [TRACE-CLOSE-END]");
  w.load("function pinTrace(t) {", "const WRAP_SVG");
  w.load("function mmss(sec) {", "// The answer streamed into this");
  for (const name of [
    "fmtSecs", "fmtTokens", "onDone", "onStopped", "onStatus", "syncPendingApproval",
    "updateDot", "setBusy", "markStopping", "unmarkStopping",
    "onApprovalRequest", "onApprovalResolved", "stashCurrentView",
  ]) w.run(fnSource(src, name));

  const s = w.sandbox;
  w.messagesEl = messagesEl;
  w.clock = clock;
  w.sent = sent;
  w.deliver = (event) => { s.handle(event); return w; };
  // Pure DOM reads.
  w.liveCards = () => messagesEl.children.filter((c) => c.classList && c.classList.contains("trace") && c.classList.contains("live"));
  w.sub = (card) => card.querySelector(".trace-sub").textContent;
  // What the server sends a viewer entering `session` (the replay follows it).
  w.hello = (session, busy) => w.deliver({
    type: "hello", session, title: session, model: "m", pager: [], cmd_history: [], busy,
  });
  return w;
}

// Chat B's hot transcript: an idle chat whose last turn answered long ago.
const B_EVENTS = [
  { type: "user", text: "an old question", turn: "b1", at: (T0 - 86_400_000) / 1000 },
  { type: "step", kind: "thinking_start" },
  { type: "step", kind: "thinking_cancel", secs: 2, answered: true },
  { type: "done", result: "an old answer", answer: "b1a" },
];
// Chat A's hot transcript while its turn runs: the live `user` carries `ts`.
const A_RUNNING = [
  { type: "user", text: "a slow question", turn: "a1", ts: T0 / 1000, at: T0 / 1000 },
  { type: "step", kind: "thinking_start" },
];

// Chat A with a turn running and its card ticking — the state the owner was in.
function aIsWorking(w) {
  w.hello("A", false);
  w.deliver({ type: "replay", events: [] });
  w.deliver({ type: "user", text: "a slow question", turn: "a1", ts: T0 / 1000, at: T0 / 1000 });
  w.deliver({ type: "step", kind: "thinking_start" });
  w.clock.now = T0 + 95_000;
}

// ---- 1. The reported path: a WARM switch to an idle chat -------------------
{
  const w = world();
  const s = w.sandbox;
  aIsWorking(w);
  const cardA = w.liveCards()[0];
  ok("(A has one live card)", w.liveCards().length === 1 && s.clientBusy === true);

  // B is warm (the prefetch aimed at the recency head), and the rail tap lands.
  s.recentSessions = [{ name: "B", title: "B" }];
  w.deliver({ type: "peek", name: "B", events: B_EVENTS, truncated: false });
  s.resumeSession("B");
  ok("the switch painted B at once, from the warm peek", s.currentSession === "B"
    && w.messagesEl.children.some((c) => c.textContent === "an old question"));
  ok("B has NO live card after the warm paint", w.liveCards().length === 0);
  ok("…A's card went with A's DOM", cardA.parentNode === null);
  ok("…and nothing is live in this view", s.currentTrace === null);
  ok("…and B does not inherit A's busy (dot, rerun/fork guards)", s.clientBusy === false);

  // B's own hello and authoritative replay (same transcript → noop landing).
  w.hello("B", false);
  w.deliver({ type: "replay", events: B_EVENTS, truncated: false });
  ok("B still has no live card once its hello and replay land", w.liveCards().length === 0 && s.currentTrace === null);

  // Back to A: warm too (the peek that follows B's hello aims at A).
  w.clock.now = T0 + 120_000;
  s.recentSessions = [{ name: "A", title: "A" }];
  w.deliver({ type: "peek", name: "A", events: A_RUNNING, truncated: false });
  s.resumeSession("A");
  w.hello("A", true);
  w.deliver({ type: "replay", events: A_RUNNING, truncated: false });
  ok("A has exactly one live card again", w.liveCards().length === 1);
  ok("…counting A's REAL elapsed time from its turn start, not from 0:00",
    s.currentTrace.startedAt === T0 && w.sub(w.liveCards()[0]).startsWith("2:00"));
}

// ---- 2. A cold switch to B (not prefetched), then back ----------------------
{
  const w = world();
  const s = w.sandbox;
  aIsWorking(w);
  s.resumeSession("B"); // nothing warm: the loading placeholder, then the server
  ok("the pending view has no live card", w.liveCards().length === 0);
  ok("…and does not claim B is busy before B's hello says so", s.clientBusy === false);
  w.hello("B", false);
  w.deliver({ type: "replay", events: B_EVENTS, truncated: false });
  ok("B has no live card after its replay", w.liveCards().length === 0);

  w.clock.now = T0 + 300_000;
  s.resumeSession("A");
  w.hello("A", true);
  w.deliver({ type: "replay", events: A_RUNNING, truncated: false });
  ok("back in A: one live card at A's real elapsed time",
    w.liveCards().length === 1 && w.sub(w.liveCards()[0]).startsWith("5:00"));
}

// ---- 3. A reload of B while A runs ------------------------------------------
{
  const w = world();
  const s = w.sandbox;
  w.hello("B", false);
  w.deliver({ type: "replay", events: B_EVENTS, truncated: false });
  ok("a fresh load of idle B has no live card", w.liveCards().length === 0 && s.clientBusy === false);
}

// ---- 4. A warm switch to a chat that IS busy, its `user` trimmed off --------
// The counterweight: the landing's card for a running turn whose start fell
// outside the window must come from THAT chat's busy, which its hello states
// after the warm paint — the authoritative replay then lands noop, and the card
// must still be built there, not only on a rebuild.
{
  const w = world();
  const s = w.sandbox;
  w.hello("A", false);
  w.deliver({ type: "replay", events: [] });
  // The window ends on an EARLIER turn's `done`: the running turn's `user` and
  // its steps are past the 500-event bound, so no replayed event builds a card.
  const TRIMMED = [{ type: "done", result: "an earlier answer", answer: "c0a" }];
  s.recentSessions = [{ name: "C", title: "C" }];
  w.deliver({ type: "peek", name: "C", events: TRIMMED, truncated: true });
  s.resumeSession("C");
  const before = w.liveCards().length;
  w.hello("C", true);
  w.deliver({ type: "replay", events: TRIMMED, truncated: true });
  ok("a busy chat reached by a warm switch has its one live card",
    w.liveCards().length === 1 && s.currentTrace !== null && before <= 1);
}

// ---- 5. A's turn ending while viewing B -------------------------------------
// A's `done` is stamped with A's session and the firewall drops it before
// handle(); here what matters is that B's view has nothing live for it to end,
// and that A's own replay afterwards shows the turn finished.
{
  const w = world();
  const s = w.sandbox;
  aIsWorking(w);
  s.recentSessions = [{ name: "B", title: "B" }];
  w.deliver({ type: "peek", name: "B", events: B_EVENTS, truncated: false });
  s.resumeSession("B");
  w.hello("B", false);
  w.deliver({ type: "replay", events: B_EVENTS, truncated: false });
  s.resumeSession("A");
  w.hello("A", false);
  w.deliver({ type: "replay", events: [
    ...A_RUNNING.map((e) => (e.type === "user" ? { ...e, ts: undefined } : e)),
    { type: "step", kind: "thinking_cancel", secs: 3, answered: true },
    { type: "done", result: "the slow answer", answer: "a1a" },
  ], truncated: false });
  ok("A finished while away: back in A, no live card", w.liveCards().length === 0 && s.currentTrace === null);
}

report("test_choreo_idle_switch.js");
