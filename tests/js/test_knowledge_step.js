// The "Recalled knowledge" row is a step of the record (#386) — the REAL
// traceStep / finishTrace / inspectKeys / inspectStepClick and the
// [STEP-SCREEN] block out of app.js, driven live and cold.
//
// What is pinned:
//
//   1. The knowledge row is joined to the dossier's `knowledge` step (`k1`),
//      is marked inspectable, and live and cold agree (L2). Its twin, a
//      "Recalled from memory" row (the `recall` tool), is joined by its call
//      id like every tool row.
//   2. A tap anywhere on the row — the row itself or a SKILL/MEM chip — opens
//      the step screen ON that step. No silent dead tap (L7).
//   3. The pane shows each item's name, kind and retrieval numbers as META,
//      and the injected text as PAYLOAD (the model received it), labelled
//      with how it was located. Where the record cannot answer it says which
//      state it is in — not recorded / purged / not located — and shows no
//      text at all: nothing is rebuilt from the item labels.
//   4. The step is on the tape: a swipe forward from it reaches the first
//      model call, a swipe back from that model call lands on it.
//
// Run manually: node tests/js/test_knowledge_step.js
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

// ---- a fake DOM (the trace-test family's) ------------------------------------
const STRUCTURAL = new Set([".trace-inner", ".trace-rail", ".trace-status", ".trace-stop", ".trace-headtext", ".trace-title", ".trace-sub", ".trace-chev"]);

function fakeEl(tag) {
  const memo = new Map();
  const el = {
    tagName: String(tag).toLowerCase(),
    className: "", innerHTML: "", hidden: false, disabled: false, value: "", type: "",
    dataset: {}, style: {}, attrs: {}, children: [], parentNode: null,
    onclick: null, oninput: null, onkeydown: null, offsetTop: 0, offsetParent: null, scrollTop: 0,
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
    get lastChild() { return this.children[this.children.length - 1] || null; },
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
  "ss-save", "ss-note", "ss-body", "ss-wrap", "ss-close", "ss-font-dec", "ss-font-inc", "ss-scroll-down", "ss-scroll-top", "ss-head", "ss-read", "ss-findings", "ss-findings-toggle", "ss-findings-cur", "ss-findings-prev", "ss-findings-pos", "ss-findings-next", "ss-findings-list", "ss-icon", "ss-chrome", "ss-grab", "ss-content"];

function world() {
  const src = appSource();
  const ids = new Map();
  for (const id of SS_IDS) ids.set(id, fakeEl("div"));
  const toasts = [];
  const sandbox = {
    document: { createElement: fakeEl, createTextNode: (t) => textNode(t), activeElement: null },
    $: (id) => ids.get(id) || null,
    messagesEl: fakeEl("div"),
    replaying: false, turnStart: 0, currentTrace: null, currentTurnId: "", turnAnchorEl: null,
    offlineViewing: false,
    SPINNER: "", TOOL_META: { recall: ["Recalled from memory", "knowledge", "--yellow"] },
    traceSvg: (name) => `<svg data-icon="${name}"></svg>`, fmtSecs: (s) => `${s}s`,
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
  assert(typeof sandbox.inspectKeys === "function", "inspectKeys is not in [TRACE-CLOSE]");
  return { sandbox, ids, toasts, el: (id) => ids.get(id) };
}

const rows = (t) => t.body.querySelectorAll(".step");
const idsOf = (t) => rows(t).map((r) => r.dataset.inspect || null);
const settle = () => new Promise((resolve) => setImmediate(resolve));

function fire(target) {
  let n = target;
  while (n) {
    const hs = n._on && n._on.click;
    if (hs) { for (const h of hs) h({ target, stopPropagation() {} }); return; }
    n = n.parentNode;
  }
}

const ITEMS = [
  { label: "aish-gender-masculine-polish", kind: "memory", sim: 0.36, rail: 3 },
  { label: "gh_issue", kind: "skill", sim: 0.402, rail: 0 },
];

// A turn that recalled knowledge at seed, then recalled more mid-task (the
// `recall` tool), then answered — as the agent emits it, and as it replays.
function liveTurn(s) {
  s.traceStep({ kind: "knowledge", mode: "semantic", items: ITEMS });
  s.traceStep({ kind: "thinking_start" });
  s.traceStep({ kind: "thinking", secs: 1.1, tokens: [10, 2], gist: "look" });
  s.traceStep({ kind: "tool_start", name: "recall", call: 1, model_call: 1, summary: "polish" });
  s.traceStep({ kind: "tool", name: "recall", call: 1, model_call: 1, ok: true, secs: 0.2, summary: "polish" });
  s.traceStep({ kind: "thinking_start" });
  s.currentTrace.thinkingRow.isAnswer = true;
  s.traceStep({ kind: "thinking_cancel", secs: 3, tokens: [20, 40] });
}
function coldTurn(s) {
  s.traceStep({ kind: "knowledge", mode: "semantic", items: ITEMS });
  s.traceStep({ kind: "thinking", secs: 1.1, tokens: [10, 2], gist: "look" });
  s.traceStep({ kind: "tool", name: "recall", call: 1, model_call: 1, ok: true, secs: 0.2, summary: "polish" });
  s.traceStep({ kind: "thinking_start" });
  s.currentTrace.thinkingRow.isAnswer = true;
  s.traceStep({ kind: "thinking_cancel", secs: 3, tokens: [20, 40] });
}

const REMINDER = "<system-reminder>Current local time: 2026-09-14T10:00:00+02:00</system-reminder>\n<system-reminder>Saved knowledge relevant to this task, preloaded for you — follow it over your training data:\n\n[memory: aish-gender-masculine-polish]\nSpeak of aish in the masculine.\n\n[skill: gh_issue]\nUse gh issue create.\n\n</system-reminder>";

function docFor(reminder) {
  return {
    ordinal: 1, running: false, prompt: "go", flow: { grouping: "recorded", rounds: [], unplaced: [], loose: [] },
    given: { state: "recorded", briefs: [], context: { records: [] }, rules: {}, trims: [] },
    thought: { state: "recorded", calls: [{ model_call: 1, text: "look it up", said: "", tokens: [10, 2], stop: "", blocks: [], malformed: [], truncated: 0 }] },
    did: { calls: [{ call: 1, name: "recall", args: { query: "polish" }, args_state: "recorded", args_truncated: 0, ok: true, status: "ok", secs: 0.2, command: "", error: "", output: "found 1", decision: null, verdict_by: "prefix", truncation: null, read: "", bytes: 7, gates: [], refused: [], completed: true, problem: "", unchanged: false }] },
    messages: [{ at: 0, role: "user", tool_name: "", model_call: 0, chars: 2, text: "go", interim: false, images: 0, superseded: false }],
    context_cost: { state: "recorded", calls: [], failed: [] },
    produced: { answer: "done", answer_state: "recorded", status: "ok", error: "", verify: { stopped: [], advised: [], passed: 0 } },
    steps: [
      { id: "k1", kind: "knowledge", n: 1, title: "recalled knowledge", panes: ["event"],
        facts: [{ k: "recalled", v: "2 (1 skill · 1 memory)" }, { k: "mode", v: "semantic" }, { k: "injected text", v: "recorded · 300 chars" }],
        record: { kind: "knowledge", mode: "semantic", items: ITEMS }, items: ITEMS, reminder, before: 1 },
      { id: "m1", kind: "model_call", n: 2, model_call: 1, title: "model call 1", panes: ["context", "response"], numbering: "recorded", facts: [], ref: { thought: 1, cost: null, brief: null }, fragment: "", errors: [], context: { new: [0], in_front: [0], unstamped: 0, stubbed: [], brief_changed: false, source: "reconstructed" }, is_last: false },
      { id: "c1", kind: "tool_call", n: 3, call: 1, name: "recall", title: "tool call 1 · recall", panes: ["call", "result"], model_call: 1, placement: "recorded", facts: [], ref: { call: 1, shown: null, shown_how: "step_output" }, continuation_read: null },
      { id: "m2", kind: "model_call", n: 4, model_call: 2, title: "model call 2", panes: ["context", "response"], numbering: "recorded", facts: [], ref: { thought: null, cost: null, brief: null }, fragment: "", errors: [], context: { new: [], in_front: [0], unstamped: 0, stubbed: [], brief_changed: false, source: "reconstructed" }, is_last: true },
    ],
    notes: { rows: [], checks: [] },
  };
}

const RECORDED = { state: "recorded", located: "brief_position", at: 6, chars: REMINDER.length, digest: "d", text: REMINDER, candidates: 1 };

function nodesOf(w) { return w.el("ss-content").children; }
function metaText(w) { return nodesOf(w).filter((n) => (n.className || "").includes("ss-meta")).map((n) => n.textContent).join("\n"); }
function payloadText(w) { return nodesOf(w).filter((n) => (n.className || "").includes("ss-pre") && !(n.className || "").includes("ss-rec")).map((n) => n.textContent).join("\n"); }

// ---- 1. the row is a step, live and cold; the recall twin is a call ------------

check("the knowledge row is joined to the record as k1, and the recall row by its call id — live and cold alike", () => {
  const live = world();
  live.sandbox.currentTurnId = "turn-a";
  liveTurn(live.sandbox);
  const lt = live.sandbox.currentTrace;
  live.sandbox.finishTrace();
  assert.deepEqual(idsOf(lt), ["k1", "m1", "c1", "m:last"], idsOf(lt).join(","));
  const know = rows(lt)[0];
  assert(know.classList.has("step-knowledge"), "the knowledge row carries its kind");
  assert(know.classList.has("inspectable"), "the knowledge row is inspectable");
  assert.equal(know.dataset.inspect, "k1");
  // The twin: "Recalled from memory" is a tool row with a call id, like any tool.
  const recall = rows(lt)[2];
  assert.equal(recall.dataset.call, "1");
  assert(recall.classList.has("inspectable"));

  const cold = world();
  cold.sandbox.currentTurnId = "turn-a";
  cold.sandbox.replaying = true;
  coldTurn(cold.sandbox);
  const ct = cold.sandbox.currentTrace;
  cold.sandbox.finishTrace();
  assert.deepEqual(idsOf(ct), idsOf(lt), "hot and cold disagree about which row is which step");
  assert(rows(ct)[0].classList.has("inspectable"));
  // Two seed rows would be two steps, counted in card order.
  const two = world();
  two.sandbox.currentTurnId = "t";
  two.sandbox.traceStep({ kind: "knowledge", mode: "lexical", items: [ITEMS[0]] });
  two.sandbox.traceStep({ kind: "knowledge", mode: "lexical", items: [ITEMS[1]] });
  const tt = two.sandbox.currentTrace;
  two.sandbox.finishTrace();
  assert.deepEqual(idsOf(tt), ["k1", "k2"]);
});

// ---- 2. the tap opens the step ------------------------------------------------------

check("a tap on the row, or on one of its chips, opens the step screen on the knowledge step", async () => {
  const w = world();
  const s = w.sandbox;
  const d = docFor(RECORDED);
  let fetches = 0;
  s.fetchDossier = async () => { fetches += 1; return d; };
  s.currentTurnId = "turn-a";
  liveTurn(s);
  const t = s.currentTrace;
  s.finishTrace();
  const know = rows(t)[0];
  fire(know);
  await settle();
  assert(s.ssIsOpen(), "the tap opened the step screen");
  assert.equal(d.steps[s.ssView.index].id, "k1");
  assert.equal(s.ssView.pane, "event");
  assert(w.el("ss-title").textContent.includes("recalled knowledge"), w.el("ss-title").textContent);
  assert(w.el("ss-icon").innerHTML.includes('data-icon="knowledge"'), "the header carries the knowledge icon the card uses");
  s.ssClose();
  // The chip is not a control of its own: a tap on it is a tap on the row.
  const chip = know.querySelector(".know-chip");
  assert(chip, "the row has chips");
  fire(chip);
  await settle();
  assert(s.ssIsOpen(), "a tap on a chip opened the step screen");
  assert.equal(d.steps[s.ssView.index].id, "k1");
  assert.equal(fetches, 1, "a finished card fetches its record once");
  s.ssClose();
  // The twin: a tap on the recall row opens call + result panes.
  fire(rows(t)[2]);
  await settle();
  assert.equal(d.steps[s.ssView.index].id, "c1");
  assert.deepEqual(d.steps[s.ssView.index].panes, ["call", "result"]);
  assert(w.toasts.length === 0, `no toast: ${w.toasts.join(" | ")}`);
});

// ---- 3. the pane: items as meta, the injected text as payload; states said ------------

check("the pane lists every item with its kind and numbers, and shows the injected text verbatim as payload", () => {
  const w = world();
  const d = docFor(RECORDED);
  assert(w.sandbox.ssOpen(d, "k1", ""));
  const meta = metaText(w);
  assert(meta.includes("aish-gender-masculine-polish (memory) · sim 0.36 · rail 3"), meta);
  assert(meta.includes("gh_issue (skill) · sim 0.402 · rail 0"), meta);
  assert(meta.includes("located by position on this turn's brief: system message at 6"), meta);
  assert(meta.includes("recalled: 2 (1 skill · 1 memory)"), "the facts strip rides at the top");
  // The text the model was handed is PAYLOAD, whole, and its own node.
  const payload = payloadText(w);
  assert.equal(payload, REMINDER, "the injected text is shown byte for byte");
  assert(!meta.includes(REMINDER), "the payload never sits in a meta node");
  // Copy carries what is on screen.
  assert(w.sandbox.ssView.text.includes(REMINDER));
});

check("where the record cannot answer, the pane says which state — and shows NO text", () => {
  for (const [state, words] of [
    ["not_recorded", "not recorded"],
    ["purged", "recorded, then deleted"],
    ["not_located", "cannot tell which system part carried it"],
  ]) {
    const w = world();
    const reminder = { state, located: state === "not_located" ? null : null, at: null, chars: null, digest: null, text: null, candidates: state === "not_located" ? 2 : 0 };
    const d = docFor(reminder);
    assert(w.sandbox.ssOpen(d, "k1", ""));
    const meta = metaText(w);
    assert(meta.includes(`THE TEXT INJECTED — ${words}`) || meta.includes(words), `${state}: ${meta}`);
    assert.equal(payloadText(w), "", `${state}: nothing may be shown as the injected text`);
    // The items are still listed: they ARE on record.
    assert(meta.includes("gh_issue (skill)"), `${state}: items still listed`);
  }
  // A step from a record without the field at all (an older document) reads as not recorded.
  const w = world();
  const d = docFor(undefined);
  delete d.steps[0].reminder;
  assert(w.sandbox.ssOpen(d, "k1", ""));
  assert(metaText(w).includes("not recorded"));
  assert.equal(payloadText(w), "");
});

// ---- 4. on the tape --------------------------------------------------------------------

check("the knowledge step is on the tape: forward reaches model call 1, back from it lands on the step", () => {
  const w = world();
  const d = docFor(RECORDED);
  assert(w.sandbox.ssOpen(d, "k1", ""));
  assert.equal(w.sandbox.ssView.index, 0);
  assert.equal(w.sandbox.ssTape(-1), false, "nothing before the first step");
  assert(w.sandbox.ssTape(1));
  assert.equal(d.steps[w.sandbox.ssView.index].id, "m1");
  assert.equal(w.sandbox.ssView.pane, "context");
  assert(w.sandbox.ssTape(-1));
  assert.equal(d.steps[w.sandbox.ssView.index].id, "k1");
  assert.equal(w.sandbox.ssView.pane, "event");
  // The header's ‹ › move whole steps the same way.
  assert(w.sandbox.ssGo(1));
  assert.equal(d.steps[w.sandbox.ssView.index].id, "m1");
  assert(w.sandbox.ssGo(-1));
  assert.equal(d.steps[w.sandbox.ssView.index].id, "k1");
  assert(w.el("ss-count").textContent.startsWith("Step 1 of 4"), w.el("ss-count").textContent);
});

(async () => {
  for (const run of pending) await run();
  if (failures) { console.error(`knowledge step: ${failures} check(s) failed`); process.exit(1); }
  console.log("knowledge step: all checks passed");
})();
