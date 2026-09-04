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
    addEventListener(type, fn) { (this._on || (this._on = {})); (this._on[type] || (this._on[type] = [])).push(fn); },
    blur() {}, focus() {},
    contains(node) { return walk(this).includes(node); },
    closest(sel) {
      const toks = String(sel).split(",").map((x) => x.trim()).filter(Boolean);
      let n = this;
      while (n) {
        if (n.classList && toks.some((tk) => tk.startsWith(".") ? matches(n, tk) : n.tagName === tk)) return n;
        n = n.parentNode;
      }
      return null;
    },
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
  "ss-save", "ss-note", "ss-body", "ss-wrap", "ss-close", "ss-font-dec", "ss-font-inc", "ss-scroll-down", "ss-scroll-top", "ss-head", "ss-read", "ss-findings", "ss-findings-toggle", "ss-findings-cur", "ss-findings-prev", "ss-findings-pos", "ss-findings-next", "ss-findings-list"];

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
  load("// [READABLE-START]", "// [READABLE-END]");
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

function fire(target) {
  let n = target;
  while (n) {
    const hs = n._on && n._on.click;
    if (hs) { for (const h of hs) h({ target, stopPropagation() {} }); return; }
    n = n.parentNode;
  }
}
const tap = (row) => fire(row);

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
  // Every joined row is inspectable (the tap is delegated, not per-row); the
  // door is there; no strip yet — the record is fetched only when the card is
  // OPENED, never at finish (a replay finishes every card on screen).
  assert(rows(lt).every((r) => r.classList.has("inspectable")));
  assert(!lt.el.querySelector(".trace-explain"), "no Full record door — rows open the detail directly");
  assert(!lt.inner.querySelector(".trace-notes"), "no worth-a-look strip on the card — it lives in the detail now");
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

check("one tap on a row opens the step screen on that step, and the record is fetched once for a finished card", async () => {
  const w = world();
  const s = w.sandbox;
  let fetches = 0;
  const d = docFor();
  s.fetchDossier = async (ref) => { fetches += 1; assert.equal(ref, "turn-a"); return d; };
  s.currentTurnId = "turn-a";
  liveTurn(s);
  const t = s.currentTrace;
  s.finishTrace();
  // A tool row opens the step screen ON that step — no in-place accordion.
  const c1 = rows(t).find((r) => r.dataset.inspect === "c1");
  fire(c1);
  await settle();
  assert(s.ssIsOpen(), "the step screen opened on one tap");
  assert.equal(d.steps[s.ssView.index].id, "c1");
  assert(!c1.querySelector(".step-xp"), "no in-place expansion box any more");
  // A model row opens its step; the answer row resolves to the LAST model call.
  s.ssClose();
  const m1 = rows(t).find((r) => r.dataset.inspect === "m1");
  fire(m1);
  await settle();
  assert.equal(d.steps[s.ssView.index].id, "m1");
  s.ssClose();
  const answer = rows(t).find((r) => r.dataset.inspect === "m:last");
  fire(answer);
  await settle();
  assert.equal(d.steps[s.ssView.index].id, "m3", "the answer row resolves to the last model call");
  // A finished card is immutable: the record is fetched once (for the strip)
  // and cached for every tap after.
  assert.equal(fetches, 1, "one fetch per finished card");
  // No worth-a-look strip and no dots on the CARD — they moved into the
  // full-screen detail (the findings navigator).
  assert(!t.inner.querySelector(".trace-notes"), "no strip on the card");
  assert(!rows(t).some((r) => r.querySelector(".step-flag")), "no flag dots on the card");
  assert(!t.el.querySelector(".trace-explain"), "no Full record door on the card");
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
  const btn = s.document.createElement("button");
  c1.appendChild(btn);
  fire(btn);
  await settle();
  assert(!s.ssIsOpen(), "a tap on a button inside the row must not open the step screen");
});

// ---- 4. the states, in the row --------------------------------------------------------

check("offline and a failed read toast; a running turn is reviewable step by step", async () => {
  const w = world();
  const s = w.sandbox;
  s.offlineViewing = true;
  s.currentTurnId = "turn-a";
  liveTurn(s);
  const t = s.currentTrace;
  s.finishTrace();
  assert(!t.el.querySelector(".trace-explain"), "no door");
  const c1 = rows(t).find((r) => r.dataset.inspect === "c1");
  fire(c1);
  await settle();
  assert(w.toasts.some((x) => x.includes("the server is needed to read this turn's record")), "offline tap toasts");
  assert(!s.ssIsOpen());
  // Back online but the read fails: the next tap toasts the error, and nothing
  // was cached.
  s.offlineViewing = false;
  s.fetchDossier = async () => { throw new Error("the record could not be read (500)"); };
  fire(c1);
  await settle();
  assert(w.toasts.some((x) => x.includes("could not be read (500)")));

  // A RUNNING turn: rows completed so far are reviewable BEFORE it ends. The
  // card is not finished, so the record is re-fetched each tap (steps so far).
  const w2 = world();
  const s2 = w2.sandbox;
  const running = docFor();
  running.running = true;
  running.steps = running.steps.slice(0, 4); // recorded so far: up to c1
  let liveFetches = 0;
  s2.fetchDossier = async () => { liveFetches += 1; return running; };
  s2.currentTurnId = "turn-b";
  liveTurn(s2); // builds the card but does NOT finish it
  const t2 = s2.currentTrace;
  const liveRows = rows(t2);
  const liveC1 = liveRows.find((r) => r.classList.has("step-tool") || (r.dataset && r.dataset.call === "1"))
    || liveRows[3];
  fire(liveC1);
  await settle();
  assert(s2.ssIsOpen(), "a completed step opens while the turn runs");
  assert.equal(running.steps[s2.ssView.index].id, "c1");
  // A step not yet in the record toasts rather than opening the wrong one.
  const liveC2 = liveRows.find((r) => r.dataset && r.dataset.call === "2") || liveRows[liveRows.length - 2];
  s2.ssClose();
  fire(liveC2);
  await settle();
  assert(w2.toasts.some((x) => x.includes("still running")), "a not-yet-recorded step toasts");
  assert(liveFetches >= 2, "a running turn re-fetches to reflect the latest steps");
});

// ---- 5. the ordinary card is unchanged ------------------------------------------------

check("a plain answer keeps its card, and a card with no turn to open is still dropped", () => {
  const w = world();
  const s = w.sandbox;
  s.currentTurnId = "turn-a";
  s.traceStep({ kind: "thinking_start" });
  s.currentTrace.thinkingRow.isAnswer = true;
  s.traceStep({ kind: "thinking_cancel", secs: 2, tokens: [5, 9] });
  const t = s.currentTrace;
  s.finishTrace();
  assert.deepEqual(idsOf(t), ["m:last"]);
  assert(!t.el.querySelector(".trace-explain"), "no Full record door");
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
