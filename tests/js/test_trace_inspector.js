// The inspector on a finished trace card (#352 slice 1) — the REAL
// ensureTrace / traceStep / toolStart / toolFinish / finishTrace and the
// [STEP-SCREEN] block out of app.js, driven live and cold.
//
// What is pinned:
//
//   1. Rows are joined to the record by what the steps wrote on them: a tool
//      row by its call id, a thinking row by the model call the tool rows
//      under it are stamped with, the between-round rows counted per kind in
//      card order. Live and replayed cards carry the SAME ids (L2).
//   2. The record is fetched on the first tap, once per card — never at
//      finish, which a replay does for every card on screen.
//   3. A tapped row expands in place to one line per pane with sizes, and a
//      line opens the step screen on that pane. The worth-a-look strip lands
//      on the cited step and pane.
//   4. Offline, a failed read and a running turn are said in words, in the
//      row itself.
//   5. The ordinary card — a plain answer with no tools — is unchanged.
//
// Run manually: node tests/js/test_trace_inspector.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface } = require("./harness");

let failures = 0;
const pending = [];
function check(name, fn) {
  pending.push(async () => {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      failures++;
      console.error(`FAIL - ${name}`);
      console.error(`       ${err.stack || err.message}`);
    }
  });
}

// ---- a fake DOM ---------------------------------------------------------------
// ensureTrace builds its skeleton with innerHTML and then queries it, so the
// structural selectors it asks for are memoized as real CHILDREN — a stand-in
// that is not a descendant would hide every row from querySelectorAll(".step").
const STRUCTURAL = new Set([".trace-inner", ".trace-rail", ".trace-status", ".trace-stop", ".trace-headtext", ".trace-title", ".trace-sub", ".trace-chev"]);

function fakeEl(tag) {
  const memo = new Map();
  const el = {
    tagName: String(tag).toLowerCase(),
    className: "", innerHTML: "", hidden: false, disabled: false, value: "", type: "",
    dataset: {}, style: {}, attrs: {}, children: [], parentNode: null,
    onclick: null, oninput: null, onkeydown: null, offsetTop: 0, offsetParent: null, scrollTop: 0,
    // traceRow appends a bare string as a title; a real node makes it text.
    append(...nodes) { for (const n of nodes) this.appendChild(typeof n === "string" ? textNode(n) : n); },
    appendChild(node) { node.parentNode = this; this.children.push(node); return node; },
    insertBefore(node, ref) {
      node.parentNode = this;
      const i = this.children.indexOf(ref);
      if (i === -1) this.children.push(node); else this.children.splice(i, 0, node);
      return node;
    },
    remove() {
      if (!this.parentNode) return;
      const i = this.parentNode.children.indexOf(this);
      if (i !== -1) this.parentNode.children.splice(i, 1);
      this.parentNode = null;
    },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {},
    blur() {}, focus() {},
    closest() { return null; },
    querySelector(sel) {
      const found = walk(this).slice(1).find((n) => matches(n, sel));
      if (found) return found;
      if (!STRUCTURAL.has(sel)) return null;
      if (!memo.has(sel)) { const stand = fakeEl("div"); stand.className = sel.slice(1); this.appendChild(stand); memo.set(sel, stand); }
      return memo.get(sel);
    },
    querySelectorAll(sel) { return walk(this).slice(1).filter((n) => matches(n, sel)); },
    scrollIntoView() {},
  };
  let text = "";
  Object.defineProperty(el, "textContent", {
    get() { return text || (el.children.length ? el.children.map((c) => c.textContent).join("") : ""); },
    set(v) { text = String(v); el.children = []; },
  });
  const set = new Set();
  el.classList = {
    _set: set,
    add: (...cs) => cs.forEach((c) => set.add(c)),
    remove: (...cs) => cs.forEach((c) => set.delete(c)),
    contains: (c) => set.has(c),
    toggle: (c) => (set.has(c) ? set.delete(c) : set.add(c)),
    has: (c) => set.has(c),
  };
  return el;
}

function textNode(t) {
  return { tagName: "#text", textContent: String(t), children: [], className: "", classList: null };
}

function matches(n, sel) {
  if (!sel.startsWith(".") || !n.classList) return false;
  const classes = sel.slice(1).split(".");
  const own = new Set((n.className || "").split(/\s+/).filter(Boolean));
  for (const c of n.classList._set) own.add(c);
  return classes.every((c) => own.has(c));
}

function walk(node, out = []) {
  out.push(node);
  for (const child of node.children || []) if (child && child.children) walk(child, out);
  return out;
}

const SS_IDS = ["step-screen", "ss-count", "ss-title", "ss-prev", "ss-next", "ss-facts", "ss-panes",
  "ss-tools", "ss-find", "ss-find-count", "ss-find-prev", "ss-find-next", "ss-whole", "ss-copy",
  "ss-save", "ss-note", "ss-body", "ss-wrap", "ss-close"];

function world() {
  const src = appSource();
  const ids = new Map();
  for (const id of SS_IDS) ids.set(id, fakeEl("div"));
  const toasts = [];
  const removed = [];
  const sandbox = {
    document: { createElement: fakeEl, createTextNode: (t) => ({ tagName: "#text", textContent: t, children: [], className: "" }), activeElement: null },
    $: (id) => ids.get(id) || null,
    messagesEl: fakeEl("div"),
    replaying: false, turnStart: 0, currentTrace: null, currentTurnId: "", turnAnchorEl: null,
    offlineViewing: false,
    SPINNER: "", TOOL_META: {}, traceSvg: () => "", fmtSecs: (s) => `${s}s`,
    updateTraceHead() {}, updateScrollHints() {}, refreshStatusline() {}, measurePinnedTrace() {},
    releasePinnedTrace() {}, scrollToEnd() {}, removeQueueChip() {},
    renderDiff: () => fakeEl("div"), renderErrorBox() {}, clampNote: () => fakeEl("div"),
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    requestAnimationFrame() {},
    showToast: (t) => toasts.push(t),
    copyText: async () => true, saveBlob() {}, framePicture: () => null, FRAME_ABSENT: {},
    currentSession: "session-x.jsonl", BASE: "/", token: "", location: { href: "http://x/" },
    fetch: async () => { throw new Error("no fetch here"); },
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    Blob: class {}, URL, JSON, Math, Set, Map, console, String, Object, Number, Array, Promise, Error,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  const load = (a, b) => vm.runInContext(surface(extract(src, a, b)), sandbox);
  load("// [TRACE-OPEN-START]", "// [TRACE-OPEN-END]");
  load("function pinTrace(t) {", "const WRAP_SVG");
  load("function finalizeAnswerRow", "// [TRACE-CLOSE-START]");
  load("// [TRACE-CLOSE-START]", "// [TRACE-CLOSE-END]");
  load("// [STEP-SCREEN-START]", "// [STEP-SCREEN-END]");
  assert(typeof sandbox.traceStep === "function" && typeof sandbox.finishTrace === "function");
  assert(typeof sandbox.traceInspector === "function", "the inspector is not in [TRACE-CLOSE]");
  return { sandbox, ids, toasts, removed, el: (id) => ids.get(id) };
}

function rows(t) {
  return t.body.querySelectorAll(".step");
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

// The steps a real two-round turn emits, in emission order, plus the
// between-round kinds.
function liveTurn(s) {
  s.traceStep({ kind: "retry", by: "owner", attempt: 2, previous: { ended: "failed", failure: "rate_limit" } });
  s.traceStep({ kind: "thinking_start" });
  s.traceStep({ kind: "model_error", model_call: 1, class: "rate_limit", action: "retry", attempt: 1, attempts: 8, waited_s: 2 });
  s.traceStep({ kind: "thinking", secs: 1.1, tokens: [10, 2], gist: "look" });
  s.traceStep({ kind: "tool_start", name: "read_url", call: 1, model_call: 1, summary: "https://x" });
  s.traceStep({ kind: "tool", name: "read_url", call: 1, model_call: 1, ok: true, secs: 0.4, summary: "https://x" });
  s.traceStep({ kind: "trim", policy: "mid_task_budget", affected: 1, stubbed: [{ at: 2, tool: "read_url" }] });
  s.traceStep({ kind: "injected", text: "hurry" });
  s.traceStep({ kind: "thinking_start" });
  s.traceStep({ kind: "thinking", secs: 0.9, tokens: [10, 2] });
  s.traceStep({ kind: "tool_start", name: "run_command", call: 2, model_call: 2, summary: "", command: "ls" });
  s.traceStep({ kind: "tool", name: "run_command", call: 2, model_call: 2, ok: true, secs: 0.1, command: "ls", output: "a" });
  s.traceStep({ kind: "thinking_start" });
  s.currentTrace.thinkingRow.isAnswer = true; // tokens streamed into it
  s.traceStep({ kind: "thinking_cancel", secs: 3, tokens: [20, 40] });
}

// The same turn as reconstruct_events replays it: no starts, the same steps.
function coldTurn(s) {
  s.traceStep({ kind: "retry", by: "owner", attempt: 2, previous: { ended: "failed", failure: "rate_limit" } });
  s.traceStep({ kind: "model_error", model_call: 1, class: "rate_limit", action: "retry", attempt: 1, attempts: 8, waited_s: 2 });
  s.traceStep({ kind: "thinking", secs: 1.1, tokens: [10, 2], gist: "look" });
  s.traceStep({ kind: "tool", name: "read_url", call: 1, model_call: 1, ok: true, secs: 0.4, summary: "https://x" });
  s.traceStep({ kind: "trim", policy: "mid_task_budget", affected: 1, stubbed: [{ at: 2, tool: "read_url" }] });
  s.traceStep({ kind: "injected", text: "hurry" });
  s.traceStep({ kind: "thinking", secs: 0.9, tokens: [10, 2] });
  s.traceStep({ kind: "tool", name: "run_command", call: 2, model_call: 2, ok: true, secs: 0.1, command: "ls", output: "a" });
  s.traceStep({ kind: "thinking_start" });
  s.currentTrace.thinkingRow.isAnswer = true;
  s.traceStep({ kind: "thinking_cancel", secs: 3, tokens: [20, 40] });
}

function idsOf(t) {
  return rows(t).map((r) => r.dataset.inspect || null);
}

function docFor() {
  return {
    ordinal: 1, running: false, prompt: "go", flow: { grouping: "recorded", rounds: [], unplaced: [], loose: [] },
    given: { state: "recorded", briefs: [], context: { records: [] }, rules: {}, trims: [] },
    thought: { state: "recorded", calls: [{ model_call: 1, text: "look it up", said: "", tokens: [10, 2], stop: "", blocks: [], malformed: [], truncated: 0 }] },
    did: { calls: [{ call: 1, name: "read_url", args: { url: "https://x" }, args_state: "recorded", args_truncated: 0, ok: true, status: "ok", secs: 0.4, command: "", error: "", output: "", decision: null, verdict_by: "prefix", truncation: { truncator: "web", kept: 10, omitted: 5, offered: true, continuation: "k" }, read: "", bytes: 15, gates: [], refused: [], completed: true, problem: "", unchanged: false }] },
    messages: [{ at: 0, role: "user", tool_name: "", model_call: 0, chars: 2, text: "go", interim: false, images: 0, superseded: false },
               { at: 1, role: "tool", tool_name: "read_url", model_call: 1, chars: 9, text: "page text", interim: false, images: 0, superseded: false }],
    context_cost: { state: "recorded", calls: [{ model_call: 1, accounted_chars: 120, added_chars: 120, reported: {}, by_where: {}, unmeasured: [] }], failed: [] },
    produced: { answer: "done", answer_state: "recorded", status: "ok", error: "", verify: { stopped: [], advised: [], passed: 0 } },
    steps: [
      { id: "retry1", kind: "retry", n: 1, title: "you retried", panes: ["event"], facts: [{ k: "attempt", v: "2" }], record: {} },
      { id: "e1", kind: "model_error", n: 2, title: "model call failed", panes: ["event"], facts: [{ k: "class", v: "rate_limit" }], record: {}, model_call: 1 },
      { id: "m1", kind: "model_call", n: 3, model_call: 1, title: "model call 1", panes: ["context", "response"], numbering: "recorded", facts: [], ref: { thought: 1, cost: 1, brief: null }, fragment: "", errors: ["e1"], context: { new: [0], in_front: [0], unstamped: 0, stubbed: [], brief_changed: false, source: "reconstructed" }, is_last: false },
      { id: "c1", kind: "tool_call", n: 4, call: 1, name: "read_url", title: "tool call 1 · read_url", panes: ["call", "result"], model_call: 1, placement: "recorded", facts: [], ref: { call: 1, shown: 1, shown_how: "message_by_order" }, continuation_read: false },
      { id: "t1", kind: "trim", n: 5, title: "earlier results were stubbed", panes: ["event"], facts: [], record: {}, before: 2 },
      { id: "s1", kind: "steering", n: 6, title: "you typed while it ran", panes: ["event"], facts: [], record: {}, text: "hurry", before: 2 },
      { id: "m2", kind: "model_call", n: 7, model_call: 2, title: "model call 2", panes: ["context", "response"], numbering: "recorded", facts: [], ref: { thought: null, cost: null, brief: null }, fragment: "", errors: [], context: { new: [1], in_front: [0, 1], unstamped: 0, stubbed: [{ at: 2, tool: "read_url" }], brief_changed: false, source: "reconstructed" }, is_last: false },
      { id: "c2", kind: "tool_call", n: 8, call: 2, name: "run_command", title: "tool call 2 · run_command", panes: ["call", "result"], model_call: 2, placement: "recorded", facts: [], ref: { call: 2, shown: null, shown_how: "step_output" }, continuation_read: null },
      { id: "m3", kind: "model_call", n: 9, model_call: 3, title: "model call 3", panes: ["context", "response"], numbering: "recorded", facts: [], ref: { thought: null, cost: null, brief: null }, fragment: "", errors: [], context: { new: [], in_front: [0, 1], unstamped: 0, stubbed: [], brief_changed: false, source: "reconstructed" }, is_last: true },
    ],
    notes: { rows: [{ check: "continuation_unread", text: "5 characters of read_url's result were cut", where: { step: "c1", pane: "result" } }], checks: [{ id: "a" }] },
  };
}

const tap = (row) => row.onclick({ target: { closest: () => null }, stopPropagation() {} });

// ---- 1. rows carry the record's ids, live and cold alike -----------------------

check("a finished card's rows name the steps they are, and live and cold agree", () => {
  const live = world();
  live.sandbox.currentTurnId = "turn-a";
  liveTurn(live.sandbox);
  const lt = live.sandbox.currentTrace;
  live.sandbox.finishTrace();
  const liveIds = idsOf(lt);
  // Live, the Thinking… row is drawn at thinking_start and finalized IN PLACE,
  // so a model_error that arrived while it was thinking sits under it; the
  // join is by what each row is, not by its neighbour, so the order is the
  // card's and the ids are the record's.
  assert.deepEqual(liveIds, ["retry1", "m1", "e1", "c1", "t1", "s1", "m2", "c2", "m:last"], liveIds.join(","));

  const cold = world();
  cold.sandbox.currentTurnId = "turn-a";
  cold.sandbox.replaying = true;
  coldTurn(cold.sandbox);
  const ct = cold.sandbox.currentTrace;
  cold.sandbox.finishTrace();
  assert.deepEqual([...idsOf(ct)].sort(), [...liveIds].sort(),
    "hot and cold disagree about which row is which step");
  // Every joined row is a control; the door is there; no strip yet — the
  // record has not been fetched, and a finish must never fetch it.
  assert(rows(lt).every((r) => r.classList.has("inspectable") && typeof r.onclick === "function"));
  assert(lt.el.querySelector(".trace-explain"), "no Full record door");
  assert(!lt.inner.querySelector(".trace-notes"), "the strip must wait for the record");
  assert.equal(lt.dossierFetch, undefined, "finishTrace must not fetch");
});

check("a thinking row takes its number from the tool rows under it, and counts only where nothing is stamped", () => {
  const w = world();
  const s = w.sandbox;
  s.currentTurnId = "t";
  // A log older than the stamp: no model_call anywhere.
  s.traceStep({ kind: "thinking", secs: 1 });
  s.traceStep({ kind: "tool", name: "read_docs", call: 1, ok: true, secs: 0.1 });
  s.traceStep({ kind: "thinking", secs: 1 });
  s.traceStep({ kind: "tool", name: "read_docs", call: 2, ok: true, secs: 0.1 });
  const t = s.currentTrace;
  s.finishTrace();
  assert.deepEqual(idsOf(t), ["m1", "c1", "m2", "c2"]);
  // A seed trim is not a step of its own: it is filed under model call 1.
  const w2 = world();
  w2.sandbox.currentTurnId = "t";
  w2.sandbox.traceStep({ kind: "trim", policy: "eager_stub", affected: 2 });
  w2.sandbox.traceStep({ kind: "thinking", secs: 1 });
  w2.sandbox.traceStep({ kind: "tool", name: "read_docs", call: 1, model_call: 1, ok: true, secs: 0.1 });
  const t2 = w2.sandbox.currentTrace;
  w2.sandbox.finishTrace();
  assert.deepEqual(idsOf(t2), ["m1", "m1", "c1"]);
});

// ---- 2 & 3. the tap, the summary, the strip -----------------------------------------

check("a tapped row fetches the record once, expands to its panes with sizes, and a line opens the screen", async () => {
  const w = world();
  const s = w.sandbox;
  let fetches = 0;
  const d = docFor();
  s.fetchDossier = async (ref) => { fetches += 1; assert.equal(ref, "turn-a"); return d; };
  s.currentTurnId = "turn-a";
  liveTurn(s);
  const t = s.currentTrace;
  s.finishTrace();
  const c1 = rows(t).find((r) => r.dataset.inspect === "c1");
  tap(c1);
  assert(c1.querySelector(".step-xp"), "no expansion box");
  assert.equal(c1.querySelector(".step-xp").textContent, "reading the record…");
  await settle();
  const box = c1.querySelector(".step-xp");
  assert(box.querySelector(".step-xp-head").textContent.includes("step 4 of 9 · tool call 1 · read_url"));
  const lines = box.querySelectorAll(".step-xp-pane");
  assert.deepEqual(lines.map((l) => l.dataset.pane), ["call", "result"]);
  const brief = lines[1].querySelector(".step-xp-brief").textContent;
  assert(brief.includes("9 chars") && brief.includes("5 omitted by web") && brief.includes("continuation unread"), brief);
  assert(lines[0].querySelector(".step-xp-brief").textContent.includes("args "), "sizes");
  // The line opens the step screen on THAT pane.
  lines[1].onclick({ stopPropagation() {} });
  assert(s.ssIsOpen());
  assert.equal(d.steps[s.ssView.index].id, "c1");
  assert.equal(s.ssView.pane, "result");
  // A second row does not fetch again; a second tap on the first folds it.
  const m1 = rows(t).find((r) => r.dataset.inspect === "m1");
  tap(m1);
  await settle();
  assert.equal(fetches, 1, "the record must be fetched once per card");
  assert(m1.querySelector(".step-xp-head").textContent.includes("model call 1"));
  assert.deepEqual(m1.querySelectorAll(".step-xp-pane").map((l) => l.dataset.pane), ["context", "response"]);
  tap(c1);
  assert(!c1.querySelector(".step-xp"), "a second tap must fold the summary");
  // The answer row resolves to the LAST model call.
  const answer = rows(t).find((r) => r.dataset.inspect === "m:last");
  tap(answer);
  await settle();
  assert(answer.querySelector(".step-xp-head").textContent.includes("model call 3"));
  // …and the strip arrived with the record, at the top of the card, landing
  // on the cited step and pane on its first synchronous pass.
  const strip = t.inner.querySelector(".trace-notes");
  assert(strip, "no worth-a-look strip");
  assert.equal(t.inner.children.indexOf(strip) < t.inner.children.indexOf(rows(t)[0]), true, "the strip is not above the rows");
  const note = strip.querySelector(".trace-note");
  assert.equal(note.textContent, "5 characters of read_url's result were cut");
  s.ssClose();
  note.onclick({ stopPropagation() {} });
  assert.equal(d.steps[s.ssView.index].id, "c1");
  assert.equal(s.ssView.pane, "result");
  // The door opens on the first worth-a-look row too.
  s.ssClose();
  t.el.querySelector(".trace-explain").onclick({ stopPropagation() {} });
  await settle();
  assert.equal(s.ssView.pane, "result");
  assert.equal(fetches, 1);
});

check("a tap on the row's own controls is that control's, not the row's", async () => {
  const w = world();
  const s = w.sandbox;
  s.fetchDossier = async () => docFor();
  s.currentTurnId = "turn-a";
  liveTurn(s);
  const t = s.currentTrace;
  s.finishTrace();
  const c1 = rows(t).find((r) => r.dataset.inspect === "c1");
  c1.onclick({ target: { closest: (sel) => (sel.includes("button") ? {} : null) }, stopPropagation() {} });
  await settle();
  assert(!c1.querySelector(".step-xp"), "a tap on a button inside the row must not expand it");
});

// ---- 4. the states, in the row --------------------------------------------------------

check("offline, a failed read and a running turn are said in the row and on the door", async () => {
  const w = world();
  const s = w.sandbox;
  s.offlineViewing = true;
  s.currentTurnId = "turn-a";
  liveTurn(s);
  const t = s.currentTrace;
  s.finishTrace();
  assert(t.el.querySelector(".trace-explain").textContent.includes("the server is needed"));
  const c1 = rows(t).find((r) => r.dataset.inspect === "c1");
  tap(c1);
  await settle();
  assert(c1.querySelector(".step-xp").textContent.includes("the server is needed to read this turn's record"));
  assert(!t.inner.querySelector(".trace-notes"));
  // Back online, the next tap tries again — a failure was never cached.
  s.offlineViewing = false;
  s.fetchDossier = async () => { throw new Error("the record could not be read (500)"); };
  tap(c1); // folds
  tap(c1);
  await settle();
  assert(c1.querySelector(".step-xp").textContent.includes("could not be read (500)"));
  const running = docFor();
  running.running = true;
  running.steps = running.steps.slice(0, 4); // the record so far: nothing after c1
  s.fetchDossier = async () => running;
  tap(c1); tap(c1);
  await settle();
  assert(c1.querySelector(".step-xp-head"), "a step in the record expands");
  const c2 = rows(t).find((r) => r.dataset.inspect === "c2");
  tap(c2);
  await settle();
  assert(c2.querySelector(".step-xp").textContent.includes("not in the record yet — this turn is still running"));
  assert(t.inner.querySelector(".trace-notes").textContent.includes("still running"));
  // The door on an offline card toasts the same sentence.
  const w2 = world();
  w2.sandbox.offlineViewing = true;
  w2.sandbox.currentTurnId = "turn-b";
  liveTurn(w2.sandbox);
  const t2 = w2.sandbox.currentTrace;
  w2.sandbox.finishTrace();
  t2.el.querySelector(".trace-explain").onclick({ stopPropagation() {} });
  await settle();
  assert.equal(w2.toasts.length, 1);
  assert(w2.toasts[0].includes("the server is needed"));
});

check("an empty notes list names its checks and never says all clear", async () => {
  const w = world();
  const s = w.sandbox;
  const d = docFor();
  d.notes = { rows: [], checks: [{ id: "a" }, { id: "b" }, { id: "c" }] };
  s.fetchDossier = async () => d;
  s.currentTurnId = "turn-a";
  liveTurn(s);
  const t = s.currentTrace;
  s.finishTrace();
  tap(rows(t)[0]);
  await settle();
  const strip = t.inner.querySelector(".trace-notes");
  assert(strip.textContent.includes("nothing flagged by the 3 checks this reader runs"));
  assert(!/nothing unusual/i.test(strip.textContent));
});

// ---- 5. the ordinary card is unchanged ------------------------------------------------

check("a plain answer keeps its card and its door, and a card with no turn to open is still dropped", () => {
  const w = world();
  const s = w.sandbox;
  s.currentTurnId = "turn-a";
  s.traceStep({ kind: "thinking_start" });
  s.currentTrace.thinkingRow.isAnswer = true;
  s.traceStep({ kind: "thinking_cancel", secs: 2, tokens: [5, 9] });
  const t = s.currentTrace;
  s.finishTrace();
  assert.deepEqual(idsOf(t), ["m:last"]);
  assert(t.el.querySelector(".trace-explain").textContent.startsWith("Full record"));
  assert(!t.inner.querySelector(".trace-notes"));
  assert(!t.el.classList.has("live"));
  // No id and nothing on it: removed, as before #243.
  const w2 = world();
  w2.sandbox.traceStep({ kind: "thinking_start" });
  w2.sandbox.traceStep({ kind: "thinking_cancel", secs: 0 });
  const t2 = w2.sandbox.currentTrace;
  let gone = false;
  t2.el.remove = () => { gone = true; };
  w2.sandbox.finishTrace();
  assert(gone, "a card with nothing to open must still be dropped");
});

(async () => {
  for (const run of pending) await run();
  if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
  console.log("trace inspector: all checks passed");
})();
