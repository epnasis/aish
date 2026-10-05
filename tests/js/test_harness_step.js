// A row for aish's own words to the model (contract §3.17) is a step of the
// record — the REAL traceStep / finishTrace / inspectKeys / inspectStepClick and
// the [STEP-SCREEN] block out of app.js, driven live and cold.
//
// What is pinned:
//
//   1. The row is joined to the dossier's `harness` step (`h1`), and live and
//      cold agree about which row is which step (L2).
//   2. A tap on it opens the step screen ON that step — never the "nothing to
//      open" toast the first cut of this row produced in a real browser.
//   3. The pane shows the note as PAYLOAD, byte for byte, with the icon its
//      source maps to.
//
// Run manually: node tests/js/test_harness_step.js
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
    updateTraceHead() {}, updateScrollHints() {}, measurePinnedTrace() {},
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

const NOTE = "[aish: The rule 'quick-reply-chips' requires the answer to include the "
  + "pattern /aish-reply:///, and it does not. If the answer ends with a question, give me "
  + "tap buttons.\nAdd it and give the answer again.]";
const HARNESS = { kind: "harness", source: "answer_check", role: "user", text: NOTE, model_call: 1 };

function liveTurn(s) {
  s.traceStep({ kind: "thinking_start" });
  s.traceStep({ kind: "thinking", secs: 1, tokens: [10, 2], gist: "answer" });
  s.traceStep(HARNESS);
  s.traceStep({ kind: "thinking_start" });
  s.currentTrace.thinkingRow.isAnswer = true;
  s.traceStep({ kind: "thinking_cancel", secs: 2, tokens: [20, 40] });
}
function coldTurn(s) {
  s.traceStep({ kind: "thinking", secs: 1, tokens: [10, 2], gist: "answer" });
  s.traceStep(HARNESS);
  s.traceStep({ kind: "thinking_start" });
  s.currentTrace.thinkingRow.isAnswer = true;
  s.traceStep({ kind: "thinking_cancel", secs: 2, tokens: [20, 40] });
}

function doc() {
  return {
    ordinal: 1, running: false, prompt: "go", flow: { grouping: "recorded", rounds: [], unplaced: [], loose: [] },
    given: { state: "recorded", briefs: [], context: { records: [] }, rules: {}, trims: [] },
    thought: { state: "recorded", calls: [] },
    did: { calls: [] },
    messages: [{ at: 0, role: "user", tool_name: "", model_call: 0, chars: 2, text: "go", interim: false, images: 0, superseded: false }],
    context_cost: { state: "recorded", calls: [], failed: [] },
    produced: { answer: "done", answer_state: "recorded", status: "ok", error: "", verify: { stopped: [], advised: [], passed: 0 } },
    steps: [
      { id: "m1", kind: "model_call", n: 1, model_call: 1, title: "model call 1", panes: ["context", "response"], numbering: "recorded", facts: [], ref: { thought: null, cost: null, brief: null }, fragment: "", errors: [], context: { new: [0], in_front: [0], unstamped: 0, stubbed: [], brief_changed: false, source: "reconstructed" }, is_last: false },
      { id: "h1", kind: "harness", n: 2, title: "aish told the model", panes: ["event"],
        facts: [{ k: "added as", v: "a user message" }, { k: "written by", v: "answer_check" }],
        record: HARNESS, text: NOTE, before: 3 },
      { id: "m2", kind: "model_call", n: 3, model_call: 2, title: "model call 2", panes: ["context", "response"], numbering: "recorded", facts: [], ref: { thought: null, cost: null, brief: null }, fragment: "", errors: [], context: { new: [], in_front: [0], unstamped: 0, stubbed: [], brief_changed: false, source: "reconstructed" }, is_last: true },
    ],
    notes: { rows: [], checks: [] },
  };
}

function nodesOf(w) { return w.el("ss-content").children; }
function payloadText(w) { return nodesOf(w).filter((n) => (n.className || "").includes("ss-pre") && !(n.className || "").includes("ss-rec")).map((n) => n.textContent).join("\n"); }

check("the row is joined to the record as h1 — live and cold alike", () => {
  const live = world();
  live.sandbox.currentTurnId = "turn-a";
  liveTurn(live.sandbox);
  const lt = live.sandbox.currentTrace;
  live.sandbox.finishTrace();
  assert.deepEqual(idsOf(lt), ["m1", "h1", "m:last"], idsOf(lt).join(","));
  assert(rows(lt)[1].classList.has("inspectable"), "the row is inspectable");

  const cold = world();
  cold.sandbox.currentTurnId = "turn-a";
  cold.sandbox.replaying = true;
  coldTurn(cold.sandbox);
  const ct = cold.sandbox.currentTrace;
  cold.sandbox.finishTrace();
  assert.deepEqual(idsOf(ct), idsOf(lt), "hot and cold disagree about which row is which step");
});

check("a tap opens the step screen on it, with the note as payload, byte for byte", async () => {
  const w = world();
  const s = w.sandbox;
  const d = doc();
  s.fetchDossier = async () => d;
  s.currentTurnId = "turn-a";
  liveTurn(s);
  const t = s.currentTrace;
  s.finishTrace();
  fire(rows(t)[1]);
  await settle();
  assert(s.ssIsOpen(), "the tap opened the step screen");
  assert.equal(d.steps[s.ssView.index].id, "h1");
  assert.equal(s.ssView.pane, "event");
  assert(w.el("ss-icon").innerHTML.includes('data-icon="enforce"'), w.el("ss-icon").innerHTML);
  assert.equal(payloadText(w), NOTE, "the text shown is not the text the model got");
  assert.deepEqual(w.toasts, [], `no toast: ${w.toasts.join(" | ")}`);
});

check("a note aish put inside a tool result is drawn under that call's row, and is still a step", () => {
  for (const replaying of [false, true]) {
    const w = world();
    const s = w.sandbox;
    s.currentTurnId = "turn-b";
    s.replaying = replaying;
    if (!replaying) s.traceStep({ kind: "tool_start", name: "run_command", call: 1, model_call: 1 });
    s.traceStep({ kind: "tool", name: "run_command", call: 1, model_call: 1, ok: true, secs: 0.1, output: "hi" });
    s.traceStep({ kind: "harness", source: "prefer_tool", role: "tool", call: 1, model_call: 1,
                  text: "[aish: the 'gh_issue' tool covers this operation]" });
    const t = s.currentTrace;
    s.finishTrace();
    const note = rows(t).find((r) => r.classList.has("step-harness"));
    assert(note, "no row was drawn for the note");
    const tool = rows(t).find((r) => r.dataset.call === "1");
    assert(note.parentNode && (note.parentNode.className || "").includes("step-under"),
      `not nested (replaying=${replaying})`);
    assert(tool.contains(note), `drawn under some other row (replaying=${replaying})`);
    assert.deepEqual(idsOf(t), ["c1", "h1"], idsOf(t).join(","));
  }
});

check("outcome rows say what came of a side decision, and are steps like any row", () => {
  for (const replaying of [false, true]) {
    const w = world();
    const s = w.sandbox;
    s.currentTurnId = "turn-c";
    s.replaying = replaying;
    s.traceStep({ kind: "outcome", of: "rules", checked: 3,
                  bound: [{ rule: "quick-reply-chips", trigger: "always" }] });
    if (!replaying) s.traceStep({ kind: "tool_start", name: "create_rule", call: 1, model_call: 1 });
    s.traceStep({ kind: "tool", name: "create_rule", call: 1, model_call: 1, ok: true, secs: 0.1 });
    s.traceStep({ kind: "outcome", of: "rule_compile", call: 1, status: "partial",
                  dropped: "the tone part", rounds: 2, model: "local:qwen" });
    s.traceStep({ kind: "outcome", of: "answer_check", met: [], not_followed: ["quick-reply-chips"] });
    const t = s.currentTrace;
    s.finishTrace();
    const outs = rows(t).filter((r) => r.classList.has("step-outcome"));
    assert.equal(outs.length, 3, `replaying=${replaying}`);
    const text = outs.map((r) => r.textContent);
    assert(text[0].includes("Rules in force: quick-reply-chips"), text[0]);
    assert(text[1].includes("Turned part of your words into a rule") && text[1].includes("the tone part"), text[1]);
    assert(text[2].includes("without following: quick-reply-chips"), text[2]);
    const tool = rows(t).find((r) => r.dataset.call === "1");
    assert(tool.contains(outs[1]), "the compile result is not under its call");
    assert.deepEqual(idsOf(t), ["o1", "c1", "o2", "o3"], idsOf(t).join(","));
  }
});

check("a result decided after the answer joins the finished card, never a new one", () => {
  const ROW = { kind: "outcome", of: "objective", status: "revised", after_turn: true,
                statement: "Know the EUR/PLN rate every morning" };
  // Live: the turn finished, then the tracker wrote.
  const live = world();
  const s = live.sandbox;
  s.currentTurnId = "turn-d";
  s.traceStep({ kind: "thinking", secs: 1, tokens: [1, 1] });
  s.finishTrace();
  const card = s.lastFinishedTrace;
  assert(card, "finishTrace kept no handle on the card");
  card.dossier = { stale: true };
  s.afterTurnStep(ROW);
  assert.equal(s.currentTrace, null, "an empty live card was opened for it");
  const row = rows(card).find((r) => r.classList.has("step-outcome"));
  assert(row && row.textContent.includes("Rewrote the objective"), "not drawn on the finished card");
  assert.equal(card.dossier, null, "the cached record would miss the new row's step");
  // Cold: the record sits inside its turn, whose card is still open.
  const cold = world();
  const c = cold.sandbox;
  c.currentTurnId = "turn-d";
  c.replaying = true;
  c.traceStep({ kind: "thinking", secs: 1, tokens: [1, 1] });
  const open = c.currentTrace;
  c.afterTurnStep(ROW);
  assert(rows(open).some((r) => r.classList.has("step-outcome")), "cold replay did not draw it");
  // Nothing to attach to: nothing drawn, and no card opened.
  const none = world();
  none.sandbox.afterTurnStep(ROW);
  assert.equal(none.sandbox.currentTrace, null);
});

(async () => {
  for (const run of pending) await run();
  if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
  console.log("harness step: all checks passed");
})();
