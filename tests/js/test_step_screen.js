// The step screen (#352 slice 1) — the REAL [STEP-SCREEN] block out of app.js,
// run against a fixture shaped like /explain's JSON in a hostile world: no
// requestAnimationFrame at all (a hidden page runs no frames, and a landing
// that needs one is a landing that sometimes does not happen), no layout, a
// fake DOM that records what was written to it.
//
// What is pinned, and why none of it is visible in a screenshot:
//
//   1. Every pane is TEXT. The panes show the untrusted half of the machine —
//      reasoning quotes fetched pages, a result is whatever a command printed,
//      the page pane is a site's own console — and `renderMarkdown` mints a
//      <button> that submits a turn. No innerHTML is ever written and no
//      interactive node is minted from content.
//   2. A swipe keeps the pane BY NAME where the next step has it and lands on
//      the first pane otherwise; the recogniser claims only a near-horizontal
//      move and yields everything else (L5).
//   3. Find counts every match, marks them, and says so.
//   4. A worth-a-look tap lands on the cited step AND pane on its first
//      synchronous pass.
//   5. The states — running, inferred, not recorded, purged, not matched,
//      offline, a failed read — are said in words, never as a blank.
//   6. The EXACT request (#352 slice 2): where the record has the bytes that
//      left aish, the Context pane shows them by digest from /evidence — each
//      provider message its own payload node, the reconstruction caveat gone,
//      an absent blob said in words, the delta an exact set difference on
//      digests, and a fetch that lands after the view moved on dropped.
//
// Run manually: node tests/js/test_step_screen.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface } = require("./harness");

let failures = 0;
const pending = [];
function check(name, fn) {
  const run = async () => {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      failures++;
      console.error(`FAIL - ${name}`);
      console.error(`       ${err.stack || err.message}`);
    }
  };
  pending.push(run);
}

// ---- a fake DOM rich enough to read back what the block did ----------------

function fakeEl(tag) {
  const el = {
    tagName: String(tag).toLowerCase(),
    className: "",
    textContent: "",
    innerHTML: "",
    hidden: false,
    disabled: false,
    value: "",
    type: "",
    dataset: {},
    style: {},
    attrs: {},
    children: [],
    parentNode: null,
    onclick: null,
    oninput: null,
    onkeydown: null,
    offsetTop: 0,
    offsetParent: null,
    scrollTop: 0,
    append(...nodes) { for (const n of nodes) this.appendChild(n); },
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
    blur() {},
    focus() {},
    querySelector(sel) { return walk(this).slice(1).find((n) => matches(n, sel)) || null; },
    querySelectorAll(sel) { return walk(this).slice(1).filter((n) => matches(n, sel)); },
    classList: null,
  };
  Object.defineProperty(el, "nextSibling", {
    get() {
      if (!el.parentNode) return null;
      const i = el.parentNode.children.indexOf(el);
      return i === -1 ? null : (el.parentNode.children[i + 1] || null);
    },
  });
  // textContent = "" on a real node drops its children; the block relies on it.
  let text = "";
  Object.defineProperty(el, "textContent", {
    get() { return text || (el.children.length ? el.children.map((c) => c.textContent).join("") : ""); },
    set(v) { text = String(v); el.children = []; },
  });
  const set = new Set();
  el.classList = {
    add: (...cs) => cs.forEach((c) => set.add(c)),
    remove: (...cs) => cs.forEach((c) => set.delete(c)),
    contains: (c) => set.has(c),
    toggle: (c) => (set.has(c) ? set.delete(c) : set.add(c)),
    has: (c) => set.has(c),
  };
  return el;
}

function matches(n, sel) {
  if (!sel.startsWith(".")) return false;
  const classes = sel.slice(1).split(".");
  const own = new Set((n.className || "").split(/\s+/).filter(Boolean));
  for (const c of n.classList ? [...(n.classList._set || [])] : []) own.add(c);
  return classes.every((c) => own.has(c) || (n.classList && n.classList.has(c)));
}

function walk(node, out = []) {
  out.push(node);
  for (const child of node.children || []) if (child && child.children) walk(child, out);
  return out;
}

function textNode(t) {
  return { tagName: "#text", textContent: String(t), children: [], classList: null, className: "" };
}

const IDS = ["step-screen", "ss-count", "ss-title", "ss-prev", "ss-next", "ss-facts", "ss-panes",
  "ss-tools", "ss-find", "ss-find-count", "ss-find-prev", "ss-find-next", "ss-whole", "ss-copy",
  "ss-save", "ss-note", "ss-body", "ss-wrap", "ss-close", "ss-font-dec", "ss-font-inc", "ss-scroll-down", "ss-scroll-top", "ss-head"];

function world(overrides = {}) {
  const ids = new Map();
  for (const id of IDS) ids.set(id, fakeEl("div"));
  ids.get("step-screen").hidden = true;
  const toasts = [];
  const saved = [];
  const sandbox = {
    document: {
      createElement: fakeEl,
      createTextNode: textNode,
      activeElement: null,
    },
    $: (id) => ids.get(id) || null,
    JSON, Math, Set, Map, console, String, Object, Number, Array, Promise, Error, URL,
    Blob: class { constructor(parts, opts) { this.parts = parts; this.opts = opts; } },
    AbortController: class { constructor() { this.signal = {}; } abort() { this.aborted = true; } },
    currentSession: "session-x.jsonl",
    offlineViewing: false,
    BASE: "/", token: "", location: { href: "http://x/" },
    fetch: async () => { throw new Error("no fetch in this world"); },
    framePicture: () => null,
    FRAME_ABSENT: { hands: "no picture — you were driving the browser yourself" },
    copyText: async () => true,
    saveBlob: (blob, name) => saved.push({ blob, name }),
    showToast: (text) => toasts.push(text),
    setTimeout: () => 0,
    clearTimeout: () => {},
    // Deliberately ABSENT: requestAnimationFrame. A landing that needs a frame
    // throws here, which is the point.
  };
  Object.assign(sandbox, overrides);
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(surface(extract(appSource(), "// [STEP-SCREEN-START]", "// [STEP-SCREEN-END]")), sandbox);
  return { sandbox, ids, toasts, saved, el: (id) => ids.get(id) };
}

const HOSTILE = "<img src=x onerror=alert(1)><script>boom()</script> [Sign in](aish-reply://steal)";

function doc(overrides = {}) {
  const messages = [
    { at: 0, role: "user", tool_name: "", model_call: 0, chars: 2, text: "go", interim: false, images: 0, documents: 0, superseded: false },
    { at: 1, role: "assistant", tool_name: "", model_call: 1, chars: 4, text: "look", interim: true, images: 0, documents: 0, superseded: false },
    { at: 2, role: "tool", tool_name: "read_url", model_call: 1, chars: 30, text: `page text needle one ${HOSTILE}`, interim: false, images: 0, documents: 0, superseded: false },
    { at: 3, role: "tool", tool_name: "run_command", model_call: 1, chars: 3, text: "a b", interim: false, images: 0, documents: 0, superseded: false },
  ];
  const d = {
    ordinal: 3, counter: 3, ts: "2026-09-02T09:00:00", prompt: `find it ${HOSTILE}`, running: false,
    steering: [], trim: [],
    given: {
      state: "recorded", carried: false,
      briefs: [{
        model_call: 1, written_here: true, in_force_since: null,
        options: { model: "qwen3:8b", num_ctx: 32768, think: true, provider: "ollama", system_role: "all", window: 32768, window_source: "num_ctx" },
        system: { state: "recorded", parts: [{ at: 0, chars: 20, digest: "d", state: "recorded", text: `you are aish ${HOSTILE}` }] },
        tools: { state: "recorded", digest: "t", count: 2, names: ["read_url", "run_command"],
                 entries: [{ function: { name: "read_url", description: "read a page" } }, { function: { name: "run_command", description: "run" } }] },
      }],
      context: { state: "recorded", records: [] }, knowledge: [],
      rules: { state: "recorded", corpus: {}, groups: {}, skipped: [], dropped: 0 }, trims: [],
    },
    thought: {
      state: "recorded", fragments: [],
      calls: [
        { model_call: 1, text: `Rules say read it. ${HOSTILE}`, truncated: 0, cap_source: null, said: "looking", said_truncated: 0, stop: "tool_use", tokens: [100, 20], blocks: [], malformed: [], synthesized: false },
        { model_call: 2, text: "done thinking", truncated: 0, cap_source: null, said: "here it is", said_truncated: 0, stop: "stop", tokens: [200, 30], blocks: [], malformed: [], synthesized: false },
      ],
    },
    did: {
      calls: [
        { call: 1, name: "read_url", summary: "https://x", args: { url: `https://x/${HOSTILE}` }, args_state: "recorded", args_truncated: 0, cap_source: null,
          ok: false, status: "failed", secs: 1.2, command: "", error: `403 ${HOSTILE}`, output: "", decision: null, verdict_by: "prefix", problem: "", unchanged: false,
          truncation: { truncator: "web", kept: 4000, omitted: 2000, offered: true, continuation: "k1" }, read: "", bytes: 6000,
          frame: "", frame_skipped: "hands", console: [`TypeError: boom ${HOSTILE}`],
          signin: { host: "eon.pl", ok: false, console: ["sign-in page said no"] },
          gates: [{ verdict: "allowed" }], refused: [], completed: true, model_call: 1 },
        { call: 2, name: "run_command", summary: "ls", args: { command: "ls" }, args_state: "recorded", args_truncated: 0, cap_source: null,
          ok: true, status: "ok", secs: 0.3, command: "ls", error: "", output: "needle two\nneedle three\nNEEDLE four", decision: "auto-approved", verdict_by: "prefix", problem: "", unchanged: false,
          truncation: {}, read: "", bytes: null, gates: [], refused: [], completed: true, model_call: 1 },
      ],
      orphan_gates: [], verify: [],
    },
    messages,
    flow: { grouping: "recorded", rounds: [{ model_call: 1, thought: 1, calls: [1, 2], before: [] }, { model_call: 2, thought: 2, calls: [], before: [] }], unplaced: [], loose: [] },
    context_cost: {
      state: "recorded", stamped: true,
      calls: [
        { model_call: 1, accounted_chars: 5000, added_chars: 5000, reported: { input: 100, output: 20 }, chars_per_token: 50, by_where: { carried: { chars: 4000, items: 6, trimmed: 0 }, turn: { chars: 2, items: 1, trimmed: 0 }, system: { chars: 20, items: 1, trimmed: 0 }, tools: { chars: 978, items: 2, trimmed: 0 } }, unmeasured: [], image_count: 0, added_by: [] },
        { model_call: 2, accounted_chars: 5040, added_chars: 40, reported: { input: 200, output: 30 }, chars_per_token: 25, by_where: { carried: { chars: 4000, items: 6, trimmed: 0 }, turn: { chars: 42, items: 4, trimmed: 0 }, system: { chars: 20, items: 1, trimmed: 0 }, tools: { chars: 978, items: 2, trimmed: 0 } }, unmeasured: [], image_count: 0, added_by: [] },
      ],
      peak: null, unattributed_chars: 0, steering_chars: 0, unstamped_chars: 0, roles: [],
      failed: [{ model_call: 1, sent_chars: 5000, sent_messages: 3, action: "retry", text: "429" }],
    },
    produced: { answer: "here it is", answer_state: "recorded", status: "ok", error: "", verify: { stopped: [], advised: [], passed: 0 } },
    steps: [
      { id: "retry1", kind: "retry", n: 1, title: "you retried", panes: ["event"], facts: [{ k: "by", v: "owner" }, { k: "attempt", v: "2" }, { k: "the attempt before", v: "failed — rate_limit" }], record: { by: "owner", attempt: 2 } },
      { id: "e1", kind: "model_error", n: 2, title: "model call failed", panes: ["event"], facts: [{ k: "class", v: "rate_limit" }, { k: "action", v: "retry" }], record: { class: "rate_limit", text: `429 ${HOSTILE}` }, model_call: 1 },
      { id: "m1", kind: "model_call", n: 3, model_call: 1, title: "model call 1", panes: ["context", "response"], numbering: "recorded",
        facts: [{ k: "model", v: "ollama · qwen3:8b" }, { k: "tokens", v: "100 input · 20 output" }], ref: { thought: 1, cost: 1, brief: 0 }, fragment: "", errors: ["e1"],
        context: { new: [0], in_front: [0], unstamped: 0, stubbed: [], brief_changed: false, source: "reconstructed" }, is_last: false },
      { id: "c1", kind: "tool_call", n: 4, call: 1, name: "read_url", title: "tool call 1 · read_url", panes: ["call", "result", "page"], model_call: 1, placement: "recorded",
        facts: [{ k: "status", v: "failed" }, { k: "verdict by", v: "prefix" }, { k: "seconds", v: "1.2" }, { k: "bytes", v: "6,000" }], ref: { call: 1, shown: 2, shown_how: "message_by_order" }, continuation_read: false },
      { id: "c2", kind: "tool_call", n: 5, call: 2, name: "run_command", title: "tool call 2 · run_command", panes: ["call", "result"], model_call: 1, placement: "recorded",
        facts: [{ k: "status", v: "ok" }, { k: "seconds", v: "0.3" }], ref: { call: 2, shown: null, shown_how: "step_output" }, continuation_read: null },
      { id: "t1", kind: "trim", n: 6, title: "earlier results were stubbed", panes: ["event"], facts: [{ k: "results stubbed", v: "1" }, { k: "which", v: "read_url (#2)" }], record: { policy: "mid_task_budget", affected: 1, stubbed: [{ at: 2, tool: "read_url" }] }, before: 2 },
      { id: "s1", kind: "steering", n: 7, title: "you typed while it ran", panes: ["event"], facts: [{ k: "chars", v: "5" }], record: { text: "hurry" }, text: "hurry", before: 2 },
      { id: "m2", kind: "model_call", n: 8, model_call: 2, title: "model call 2", panes: ["context", "response"], numbering: "recorded",
        facts: [{ k: "model", v: "ollama · qwen3:8b" }, { k: "tokens", v: "200 input · 30 output" }], ref: { thought: 2, cost: 2, brief: 0 }, fragment: "", errors: [],
        context: { new: [1, 2, 3], in_front: [0, 1, 2, 3], unstamped: 0, stubbed: [{ at: 2, tool: "read_url" }], brief_changed: false, source: "reconstructed" }, is_last: true },
    ],
    notes: {
      rows: [{ check: "tool_failed", text: "read_url failed (failed)", where: { section: "flow", call: 1, step: "c1", pane: "result" } }],
      checks: [{ id: "a" }, { id: "b" }, { id: "c" }],
    },
  };
  return Object.assign(d, overrides);
}

// ---- the exact request: the `sent` block and an /evidence server ----------------
//
// Digests are opaque keys here (the real ones are sha256 hex); what matters is
// that a message is fetched by ITS digest and lands in ITS node. `blobs` maps a
// digest to the bytes, a status (410 evicted / 404 purged) or a thrown error.
const BLOBS = {
  "d-sys": `you are aish, exactly ${HOSTILE}`,
  "d-user": "go",
  "d-asst": "look",
  "d-tool": `page text needle one ${HOSTILE}`,
  "d-tools": JSON.stringify([{ type: "function", function: { name: "read_url", description: "read a page" } }], null, 2),
  "d-evicted": 410,
  "d-purged": 404,
  "d-broken": new Error("Failed to fetch"),
  "d-resp1": JSON.stringify({ content: "look", tool_calls: [{ function: { name: "read_url" } }], raw_blocks: [{ type: "citation", url: "https://ex.com" }] }, null, 2),
  "d-resp2": JSON.stringify({ content: "done" }, null, 2),
};

// A fetch that serves /evidence from BLOBS and nothing else. `log` records
// every URL asked for; `gate` (optional) is awaited before each answer so a
// check can hold a fetch back and move the view under it.
function evidenceFetch(log, gate) {
  return async (url) => {
    const m = String(url).match(/\/evidence\/([^/]+)\/([^?]+)\?(.*)$/);
    if (!m) throw new Error("no fetch in this world: " + url);
    log.push({ session: decodeURIComponent(m[1]), digest: m[2], query: m[3] });
    if (gate) await gate;
    const blob = BLOBS[m[2]];
    if (blob instanceof Error) throw blob;
    if (blob === undefined) return { ok: false, status: 403, text: async () => "bad sig" };
    if (typeof blob === "number") return { ok: false, status: blob, text: async () => `${blob} body` };
    return { ok: true, status: 200, text: async () => blob };
  };
}

// The dossier's `sent` block for the two calls of doc(): call 1 sent the
// system text and the prompt; call 2 re-sent both and added the assistant's
// message, the read_url result (a secret scrubbed from the stored copy), a
// stubbed second result whose bytes were purged, and a third whose bytes were
// evicted with the chat.
function sentBlock() {
  const msg = (at, role, digest, chars, extra = {}) => ({ at, role, digest, chars, sig: `sig-${digest}`, state: "recorded", ...extra });
  return {
    state: "recorded", coverage: "seam", provider: "ollama", evicted_on: null,
    calls: [
      { model_call: 1, provider: "ollama", model: "qwen3:8b", options: { model: "qwen3:8b", options: { num_ctx: 32768 }, think: true },
        request: "req-1-abcdef0123456789", chars: 900, state: "recorded",
        messages: [msg(0, "system", "d-sys", 40, { origin: 0 }), msg(1, "user", "d-user", 2, { origin: 1 })],
        tools: { digest: "d-tools", sig: "sig-d-tools", chars: 120, count: 1, state: "recorded" }, system: null },
      { model_call: 2, provider: "ollama", model: "qwen3:8b", options: { model: "qwen3:8b", options: { num_ctx: 32768 }, think: true },
        request: "req-2-abcdef0123456789", chars: 1400, state: "purged",
        messages: [
          msg(0, "system", "d-sys", 40, { origin: 0 }),
          msg(1, "user", "d-user", 2, { origin: 1 }),
          msg(2, "assistant", "d-asst", 4, { origin: 2 }),
          msg(3, "tool", "d-tool", 30, { origin: 3, tool_name: "read_url", scrubbed: 1 }),
          msg(4, "tool", "d-purged", 12, { origin: 4, tool_name: "run_command", stub: true, state: "purged" }),
          msg(5, "tool", "d-evicted", 9, { origin: 5, tool_name: "run_command", state: "evicted",
            media: [{ path: "/tmp/shot.png", bytes: 20480, state: "never_stored" }] }),
        ],
        tools: { digest: "d-tools", sig: "sig-d-tools", chars: 120, count: 1, state: "recorded" }, system: null },
    ],
  };
}

// doc() with the exact request on record: the model steps say `source: sent`
// and point at their call, exactly as explain._model_step does.
function sentDoc(overrides = {}) {
  const d = doc(overrides);
  d.sent = sentBlock();
  for (const step of d.steps) {
    if (step.kind !== "model_call") continue;
    step.ref.sent = step.model_call;
    step.context.source = "sent";
  }
  return d;
}

// doc() with the complete response on record (#355): the model steps point at
// their received call, exactly as explain._model_step does.
function receivedDoc(overrides = {}) {
  const d = doc(overrides);
  const call = (mc, digest, state = "recorded") => ({ model_call: mc, provider: "ollama", model: "qwen3:8b",
    digest, sig: `sig-${digest}`, chars: 40, state });
  d.received = { state: "recorded", coverage: "seam", provider: "ollama", evicted_on: null,
    calls: [call(1, "d-resp1"), call(2, "d-resp2", "purged")] };
  for (const step of d.steps) {
    if (step.kind !== "model_call") continue;
    const c = d.received.calls.find((x) => x.model_call === step.model_call);
    step.ref.received = c && c.state !== "not_recorded" ? step.model_call : null;
  }
  return d;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function nodesOf(w) { return w.el("ss-body").children; }
function presOf(w) { return nodesOf(w).filter((n) => (n.className || "").includes("ss-pre") && !(n.className || "").includes("ss-rec")); }
function metasOf(w) { return nodesOf(w).filter((n) => (n.className || "").includes("ss-meta")); }

function preOf(w) {
  return w.el("ss-body").querySelector(".ss-pre");
}

// The pane is SEGMENTS now — meta blocks around verbatim payload blocks — so
// whole-pane assertions read every block's text, in order.
function bodyText(w) {
  return w.el("ss-body").children.map((n) => n.textContent).join("\n");
}

// ---- 1. every kind renders -------------------------------------------------

check("every kind of step renders, with exactly the panes it has", () => {
  const w = world();
  const d = doc();
  assert(w.sandbox.ssOpen(d, "retry1", ""));
  assert.equal(w.el("step-screen").hidden, false);
  const seen = [];
  for (let i = 0; i < d.steps.length; i++) {
    const step = d.steps[i];
    assert.equal(w.el("ss-count").textContent, `Step ${i + 1} of ${d.steps.length}`);
    assert(w.el("ss-title").textContent.includes(step.title), `title: ${w.el("ss-title").textContent}`);
    const tabs = w.el("ss-panes").children.map((t) => t.dataset.pane);
    assert.deepEqual(tabs, step.panes, `panes of ${step.id}`);
    assert(preOf(w), `no body for ${step.id}`);
    assert(preOf(w).textContent.length > 0, `empty body for ${step.id}`);
    assert.equal(w.el("ss-facts").children.length, step.facts.length, `facts of ${step.id}`);
    seen.push(step.kind);
    if (i < d.steps.length - 1) assert(w.sandbox.ssGo(1));
  }
  assert.deepEqual(new Set(seen), new Set(["retry", "model_error", "model_call", "tool_call", "trim", "steering"]));
  assert.equal(w.sandbox.ssGo(1), false, "no step past the last");
  assert.equal(w.el("ss-next").disabled, true);
});

// ---- 2. text, never markup --------------------------------------------------

check("every pane is textContent, and no interactive node is minted from content", () => {
  const w = world();
  const d = doc();
  w.sandbox.ssOpen(d, "retry1", "");
  let onScreen = 0;
  for (let i = 0; i < d.steps.length; i++) {
    const step = d.steps[i];
    for (const pane of step.panes) {
      w.sandbox.ssShow(i, pane);
      if (pane === "context") w.sandbox.ssToggleWhole();
      const nodes = walk(w.el("step-screen"));
      for (const node of nodes) {
        assert(!/[<>]/.test(node.innerHTML || ""), `innerHTML written on ${node.tagName}: ${node.innerHTML}`);
        if (["button", "a", "img", "input"].includes(node.tagName)) {
          const text = node.textContent || "";
          assert(!text.includes("onerror") && !text.includes("aish-reply"),
            `content reached an interactive ${node.tagName}: ${text.slice(0, 60)}`);
        }
      }
      if (preOf(w).textContent.includes(HOSTILE)) onScreen += 1;
      if (pane === "context") w.sandbox.ssToggleWhole();
    }
    w.sandbox.ssGo(1);
  }
  assert(onScreen >= 5, `the hostile string should be on screen as TEXT in many panes (saw ${onScreen})`);
  // …and the segments join to exactly the pane's text (what copy/save carry).
  w.sandbox.ssOpen(d, "c1", "result");
  const joined = w.el("ss-body").children
    .filter((n) => /ss-(pre|meta)/.test(n.className || ""))
    .map((n) => n.textContent).join("\n\n");
  assert.equal(joined, w.sandbox.ssView.text);
});

// ---- 3. the swipe -------------------------------------------------------------

check("a step change keeps the pane by name where the next step has it, else the first", () => {
  const w = world();
  const d = doc();
  w.sandbox.ssOpen(d, "c1", "result");
  assert.equal(w.sandbox.ssView.pane, "result");
  w.sandbox.ssGo(1);                                    // c1 → c2: both have Result
  assert.equal(w.sandbox.ssView.doc.steps[w.sandbox.ssView.index].id, "c2");
  assert.equal(w.sandbox.ssView.pane, "result", "the pane was not kept by name");
  w.sandbox.ssGo(1);                                    // c2 → t1: an event has no Result
  assert.equal(w.sandbox.ssView.doc.steps[w.sandbox.ssView.index].id, "t1");
  assert.equal(w.sandbox.ssView.pane, "event", "did not fall to the first pane");
  w.sandbox.ssGo(-2);                                   // t1 → c1: no Event there, so its first pane
  assert.equal(w.sandbox.ssView.doc.steps[w.sandbox.ssView.index].id, "c1");
  assert.equal(w.sandbox.ssView.pane, "call");
  // The tabs mirror it, and the selected one is marked.
  const on = w.el("ss-panes").children.filter((t) => t.classList.has("on") || / on$/.test(t.className));
  assert.equal(on.length, 1);
  assert.equal(on[0].dataset.pane, "call");
});

check("the recogniser claims only a near-horizontal move and yields the rest (L5)", () => {
  const w = world();
  const d = doc();
  const s = w.sandbox;
  s.ssOpen(d, "c1", "result");
  const at = () => d.steps[s.ssView.index].id;
  const left = () => {
    s.ssTouchStart({ touches: [{ clientX: 200, clientY: 300 }] });
    s.ssTouchMove({ touches: [{ clientX: 150, clientY: 303 }] });
    assert.equal(s.ssGesture, "page", "a near-horizontal move must be claimed");
    s.ssTouchEnd({ changedTouches: [{ clientX: 110, clientY: 306 }] });
  };
  const right = () => {
    s.ssTouchStart({ touches: [{ clientX: 100, clientY: 300 }] });
    s.ssTouchMove({ touches: [{ clientX: 160, clientY: 300 }] });
    s.ssTouchEnd({ changedTouches: [{ clientX: 200, clientY: 300 }] });
  };
  // A flick to the left walks the TAPE: the next pane of THIS step first.
  left();
  assert.equal(at(), "c1");
  assert.equal(s.ssView.pane, "page", "the tape moves to the next pane before the next step");
  // …and past the last pane, the next step's FIRST pane.
  left();
  assert.equal(at(), "c2");
  assert.equal(s.ssView.pane, "call", "past the last pane, the next step's first");
  // A diagonal start is a scroll, decided ONCE: a big horizontal travel later
  // does not turn it into a page. (Scrolled off the top, so the downward pull
  // cannot read as a dismiss — that case has its own check.)
  w.el("ss-body").scrollTop = 50;
  s.ssTouchStart({ touches: [{ clientX: 200, clientY: 300 }] });
  s.ssTouchMove({ touches: [{ clientX: 190, clientY: 320 }] });
  assert.equal(s.ssGesture, "scroll", "a diagonal move must be yielded");
  s.ssTouchMove({ touches: [{ clientX: 40, clientY: 330 }] });
  assert.equal(s.ssGesture, "scroll", "a decision must not be re-made mid-gesture");
  s.ssTouchEnd({ changedTouches: [{ clientX: 30, clientY: 340 }] });
  assert.equal(at(), "c2", "a yielded gesture must not move the step");
  assert.equal(s.ssView.pane, "call");
  w.el("ss-body").scrollTop = 0;
  // A claimed move too short to be a swipe changes nothing.
  s.ssTouchStart({ touches: [{ clientX: 200, clientY: 300 }] });
  s.ssTouchMove({ touches: [{ clientX: 180, clientY: 301 }] });
  s.ssTouchEnd({ changedTouches: [{ clientX: 175, clientY: 301 }] });
  assert.equal(at(), "c2");
  // …and a swipe to the right goes back onto the previous step's LAST pane.
  right();
  assert.equal(at(), "c1");
  assert.equal(s.ssView.pane, "page", "entering a step from the right lands on its last pane");
  // The tape ends where the steps do.
  s.ssOpen(d, d.steps[0].id, "");
  assert.equal(s.ssTape(-1), false, "no pane before the first step's first");
  const last = d.steps[d.steps.length - 1];
  s.ssOpen(d, last.id, last.panes[last.panes.length - 1]);
  assert.equal(s.ssTape(1), false, "no pane past the last step's last");
});

// ---- 4. find ------------------------------------------------------------------

check("find counts every match, marks them, and moves between them", () => {
  const w = world();
  const d = doc();
  const s = w.sandbox;
  s.ssOpen(d, "c2", "result");
  const whole = preOf(w).textContent;
  s.ssSetFind("needle");
  assert.equal(s.ssFind.hits, 3, "case-insensitive count");
  assert.equal(w.el("ss-find-count").textContent, "1 of 3");
  const marks = preOf(w).children.filter((n) => n.tagName === "mark");
  assert.equal(marks.length, 3);
  assert(marks.every((m) => m.textContent.toLowerCase() === "needle"));
  assert(marks[0].classList.has("ss-cur"));
  // The text is whole around the marks: nodes and marks concatenate to it.
  assert.equal(preOf(w).children.map((n) => n.textContent).join(""), whole);
  s.ssFindGo(1);
  assert.equal(w.el("ss-find-count").textContent, "2 of 3");
  assert(marks[1].classList.has("ss-cur") && !marks[0].classList.has("ss-cur"));
  s.ssFindGo(-2);
  assert.equal(w.el("ss-find-count").textContent, "3 of 3", "wraps");
  s.ssSetFind("zzz");
  assert.equal(w.el("ss-find-count").textContent, "no match");
  s.ssSetFind("");
  assert.equal(w.el("ss-find-count").textContent, "");
  assert.equal(preOf(w).textContent, whole);
  // A new step keeps the query and re-counts over the new body.
  s.ssSetFind("needle");
  s.ssGo(-1);
  assert.equal(s.ssView.pane, "result");
  assert.equal(s.ssFind.hits, 1, "one needle in c1's result");
});

// ---- 5. the door lands where it is told ----------------------------------------

check("a worth-a-look row lands on the cited step AND pane on its first synchronous pass", () => {
  const w = world();
  const d = doc();
  w.sandbox.ssOpenDoc(d);
  assert.equal(d.steps[w.sandbox.ssView.index].id, "c1");
  assert.equal(w.sandbox.ssView.pane, "result");
  assert(bodyText(w).includes("WHAT THE MODEL WAS GIVEN"));
  // No notes: the first step.
  const plain = doc({ notes: { rows: [], checks: [] } });
  w.sandbox.ssOpenDoc(plain);
  assert.equal(plain.steps[w.sandbox.ssView.index].id, "retry1");
  // A stale citation still opens, on the first step, rather than nothing.
  w.sandbox.ssOpen(d, "nowhere", "nopane");
  assert.equal(w.sandbox.ssView.index, 0);
  assert.equal(w.sandbox.ssView.pane, "event");
});

// ---- 6. the states, in words ------------------------------------------------------

check("the panes say what the record can and cannot say", () => {
  const w = world();
  const d = doc();
  const s = w.sandbox;
  // The context pane opens on what changed, with the whole context one tap away.
  s.ssOpen(d, "m2", "context");
  let text = bodyText(w);
  assert(text.includes("WHAT CHANGED SINCE MODEL CALL 1"), text.slice(0, 200));
  assert(text.includes("reconstructed from the message records"), "the reconstruction must be labelled");
  assert(text.includes("stubbed before this call: read_url (#2)"));
  assert(text.includes("NEW TO THIS CALL — 3 message(s)"));
  assert(text.includes("message #2 · tool/read_url"));
  assert.equal(w.el("ss-whole").hidden, false);
  s.ssToggleWhole();
  text = bodyText(w);
  assert(text.includes("THE WHOLE CONTEXT OF MODEL CALL 2"));
  assert(text.includes("you are aish"), "the system text is the input of the call");
  assert(text.includes('"read_url"') && text.includes('"read a page"'), "the tool menu is the input of the call, verbatim JSON");
  assert(text.includes("6 message(s), 4,000 chars, carried from earlier turns"));
  // The model-call facts: retries and the context snapshot (#334) are on the strip.
  s.ssOpen(d, "m1", "context");
  text = bodyText(w);
  assert(text.includes("5,000 chars in front of this call"));
  assert(text.includes("100 input · 20 output"));
  // The response pane: reasoning, said, the calls it issued with args, and the
  // answer on the LAST call.
  s.ssOpen(d, "m2", "response");
  text = bodyText(w);
  assert(text.includes("done thinking") && text.includes("here it is"));
  assert(text.includes("WHAT IT ANSWERED"));
  s.ssOpen(d, "m1", "response");
  text = bodyText(w);
  assert(text.includes("TOOL CALLS ISSUED — 2") && text.includes('"url"'));
  assert(!text.includes("WHAT IT ANSWERED"), "the answer belongs to the last call only");
  // The call pane: args, decision, gates collapsed.
  s.ssOpen(d, "c1", "call");
  text = bodyText(w);
  assert(text.includes("GATE VERDICTS — 1 (1 allowed, collapsed)"));
  assert(text.includes("verdict by: prefix"));
  // The result pane says where the text came from, the cut, and what is not
  // recorded yet.
  s.ssOpen(d, "c1", "result");
  text = bodyText(w);
  assert(text.includes("matched to this call by tool name and order"));
  assert(text.includes("page text needle one"));
  assert(text.includes("cut by web: kept 4,000 chars, omitted 2,000"));
  assert(text.includes("nothing read the continuation back in this turn"));
  assert(text.includes("the whole output before any cut: not recorded"));
  assert(text.includes("payload before any cut: 6,000 bytes"));
  // The page pane: the page's words are labelled as the page's.
  s.ssOpen(d, "c1", "page");
  text = bodyText(w);
  assert(text.includes("you were driving the browser yourself"));
  assert(text.includes("THE PAGE'S WORDS") && text.includes("TypeError: boom"));
  // Events: the fact and its numbers.
  s.ssOpen(d, "e1", "event");
  text = bodyText(w);
  assert(text.includes("class: rate_limit") && text.includes("WHAT THE PROVIDER SAID"));
  s.ssOpen(d, "s1", "event");
  assert(bodyText(w).includes("hurry"));
});

check("running, inferred, none, not recorded, purged and not matched are said out loud", () => {
  const w = world();
  const s = w.sandbox;
  const d = doc({ running: true });
  d.flow.grouping = "inferred";
  d.steps[3].placement = "inferred";
  d.steps[2].numbering = "inferred";
  d.thought.state = "not_recorded";
  d.thought.calls = [];
  d.steps[2].ref.thought = null;
  d.given.briefs[0].system.parts[0] = { at: 0, chars: 20, digest: "d", state: "purged", text: null };
  d.steps[3].ref.shown_how = "not_matched";
  d.steps[3].ref.shown = null;
  s.ssOpen(d, "m1", "response");
  let note = w.el("ss-note").textContent;
  assert(note.includes("still running"), note);
  assert(note.includes("inferred from the order the log was written"), note);
  assert(w.el("ss-title").textContent.includes("number inferred"));
  assert(bodyText(w).includes("not recorded — the aish that wrote this log did not keep it"));
  s.ssOpen(d, "c1", "result");
  assert(w.el("ss-title").textContent.includes("round inferred"));
  note = w.el("ss-note").textContent;
  assert(note.includes("attached to its model call by the order"), note);
  assert(bodyText(w).includes("not matched — the record has no text"));
  s.ssOpen(d, "m1", "context");
  s.ssToggleWhole();
  assert(bodyText(w).includes("recorded, then deleted"));
  // A backend that records no rounds.
  const none = doc();
  none.flow.grouping = "none";
  none.steps = none.steps.filter((x) => x.kind === "tool_call").map((x) => ({ ...x, model_call: null, placement: "none" }));
  s.ssOpen(none, "c1", "call");
  assert(w.el("ss-note").textContent.includes("records no model calls"));
  assert(bodyText(w).includes("no recorded model call issued this call"));
  // Nothing recorded at all.
  const empty = doc({ steps: [] });
  s.ssOpen(empty, "", "");
  assert.equal(w.el("ss-count").textContent, "No steps");
  assert(bodyText(w).includes("nothing was recorded for this turn"));
});

check("a long body folds and is never truncated", () => {
  const w = world();
  const d = doc();
  d.did.calls[1].output = Array.from({ length: 3000 }, (_, i) => `line ${i}`).join("\n");
  w.sandbox.ssOpen(d, "c2", "result");
  const pre = preOf(w);
  assert(pre.classList.has("ss-clamped"), "a long body must fold");
  assert(pre.textContent.includes("line 2999"), "the whole text must be in the DOM");
  const more = w.el("ss-body").children.find((n) => n.className === "ss-more");
  assert(more && more.textContent.startsWith("show all ("));
  more.onclick();
  assert(!pre.classList.has("ss-clamped"));
  assert.equal(w.el("ss-save").hidden, false, "a big body offers save as file");
  w.sandbox.ssSave();
  assert.equal(w.saved.length, 1);
  assert(/^turn-3-step-5-result\.txt$/.test(w.saved[0].name), w.saved[0].name);
  assert.equal(w.saved[0].blob.parts[0], w.sandbox.ssView.text);
});

check("close hides the surface, clears the view and drops the body", () => {
  const w = world();
  w.sandbox.ssOpen(doc(), "c1", "call");
  assert(w.sandbox.ssIsOpen());
  assert.equal(w.sandbox.ssClose(), true);
  assert.equal(w.el("step-screen").hidden, true);
  assert.equal(w.sandbox.ssView, null);
  assert.equal(w.el("ss-body").children.length, 0);
  assert.equal(w.sandbox.ssClose(), false, "a second close is a no-op");
  assert.equal(w.sandbox.ssGo(1), false);
});

// ---- 7. the door by id ---------------------------------------------------------

check("openExplain reads the record and lands on the first worth-a-look row", async () => {
  const d = doc();
  const w = world({ fetch: async () => ({ ok: true, status: 200, json: async () => d }) });
  await w.sandbox.openExplain("turn-3");
  assert(w.sandbox.ssIsOpen());
  assert.equal(d.steps[w.sandbox.ssView.index].id, "c1");
  assert.equal(w.sandbox.ssView.pane, "result");
  assert.deepEqual(w.toasts, []);
});

check("a failed read, a missing turn and an offline device each say so in words", async () => {
  let w = world({ fetch: async () => { throw new TypeError("Failed to fetch"); } });
  await w.sandbox.openExplain("");
  assert.deepEqual(w.toasts, ["the record could not be read — this device may be offline"]);
  assert(!w.sandbox.ssIsOpen());
  w = world({ fetch: async () => ({ ok: false, status: 404 }) });
  await w.sandbox.openExplain("gone");
  assert.deepEqual(w.toasts, ["there is no record of that turn in this chat's log"]);
  w = world({ offlineViewing: true, fetch: async () => { throw new Error("must not be called"); } });
  await w.sandbox.openExplain("");
  assert.equal(w.toasts.length, 1);
  assert(w.toasts[0].includes("the server is needed"), w.toasts[0]);
});

check("meta is structure, never concatenated with the exchange's bytes", () => {
  const w = world();
  const d = doc();
  w.sandbox.ssOpen(d, "c1", "result");
  const nodes = w.el("ss-body").children;
  const metas = nodes.filter((n) => (n.className || "").includes("ss-meta"));
  const pres = nodes.filter((n) => (n.className || "").includes("ss-pre"));
  // The provenance sentence is a meta block; the payload is its own pre; and
  // no single node holds both, so the eye can always tell them apart.
  assert(metas.some((n) => n.textContent.includes("WHAT THE MODEL WAS GIVEN")));
  assert(pres.some((n) => n.textContent.includes("page text needle one")));
  for (const n of nodes) {
    assert(!(n.textContent.includes("WHAT THE MODEL WAS GIVEN") && n.textContent.includes("page text needle one")),
      "meta and payload were concatenated into one node");
  }
  // A message's head is meta; its text is payload — in the context pane too.
  w.sandbox.ssOpen(d, "m2", "context");
  const ctxNodes = w.el("ss-body").children;
  assert(ctxNodes.some((n) => (n.className || "").includes("ss-meta") && n.textContent.includes("message #2 · tool/read_url")));
  for (const n of ctxNodes) {
    assert(!(n.textContent.includes("── message #") && (n.className || "").includes("ss-pre")),
      "a message head leaked into a payload node");
  }
  // The tool menu is SENT to the model, so its definitions are payload, not
  // meta: the count line is a meta header, the tool entries are their own pre.
  w.sandbox.ssOpen(d, "m2", "context");
  w.sandbox.ssToggleWhole();
  const wholeNodes = w.el("ss-body").children;
  assert(wholeNodes.some((n) => (n.className || "").includes("ss-meta") && n.textContent.includes("TOOLS ON THE MENU")));
  assert(wholeNodes.some((n) => (n.className || "").includes("ss-pre") && n.textContent.includes('"read_url"')),
    "the tool definitions must be payload JSON, not meta");
  // The definitions are the VERBATIM menu structure, not a bullet reformatting.
  const defsNode = wholeNodes.find((n) => (n.className || "").includes("ss-pre") && n.textContent.includes('"read_url"'));
  assert(defsNode && !defsNode.textContent.includes("\u00b7 read_url \u2014"), "the menu must not be flattened to bullets");
  for (const n of wholeNodes) {
    assert(!(n.textContent.includes("TOOLS ON THE MENU") && n.textContent.includes('"read_url"')),
      "the menu header leaked into the definitions node");
  }
  w.sandbox.ssToggleWhole();
  // A refused gate verdict is a RECORD node, tinted like meta, never plain payload.
  const dd = doc();
  dd.did.calls[0].refused = [{ gate: "rule.x", verdict: "denied" }];
  dd.did.calls[0].gates = [{ gate: "rule.x", verdict: "denied" }];
  w.sandbox.ssOpen(dd, "c1", "call");
  const rec = w.el("ss-body").children.find((n) => (n.className || "").includes("ss-rec"));
  assert(rec && rec.textContent.includes("rule.x"), "a gate verdict must be a record node");
});

check("payload styling tracks what the model received, not merely verbatim bytes", () => {
  const w = world();
  const d = doc();
  w.sandbox.ssOpen(d, "c1", "page");
  const nodes = w.el("ss-body").children;
  // The DRIVEN page's console IS composed into the tool result the model got
  // (console_note), so it is payload — monospace .ss-pre.
  const driven = nodes.find((n) => (n.textContent || "").includes("TypeError: boom"));
  assert(driven && (driven.className || "").includes("ss-pre") && !(driven.className || "").includes("ss-rec"),
    "the driven page's console reaches the model, so it is payload");
  // The SIGN-IN page's console goes to the OWNER and NEVER to the model
  // (browser.py), so it is EVIDENCE — a record node, never plain payload.
  const signin = nodes.find((n) => (n.textContent || "").includes("sign-in page said no"));
  assert(signin && (signin.className || "").includes("ss-rec"),
    "the sign-in console never reached the model, so it must not wear payload styling");
});

// ---- 8. the exact request ------------------------------------------------------

check("an exact request renders each provider message as its own payload node, fetched by its digest", async () => {
  const asked = [];
  const w = world({ fetch: evidenceFetch(asked) });
  const d = sentDoc();
  w.sandbox.ssOpen(d, "m2", "context");
  asked.length = 0; // the "what changed" reading asked for its own two; the whole reading is under test
  w.sandbox.ssToggleWhole();
  // Before anything lands: every recorded blob is a META placeholder saying so,
  // and nothing wears payload styling yet — a placeholder is not the bytes.
  assert.equal(presOf(w).length, 0, "no payload node before the bytes arrive");
  assert(metasOf(w).some((n) => n.textContent === "loading the exact bytes…"));
  // Asked for by digest, in the chat's own directory, with the sig /explain minted.
  assert.deepEqual(asked.map((a) => a.digest), ["d-tools", "d-sys", "d-user", "d-asst", "d-tool"],
    "every RECORDED blob is fetched once, by digest; purged and evicted ones are never asked for");
  assert(asked.every((a) => a.session === "session-x.jsonl" && a.query.includes(`sig=sig-${a.digest}`)), JSON.stringify(asked[0]));
  await flush();
  // Landed: one .ss-pre per provider message, holding exactly the blob's bytes.
  const pres = presOf(w);
  assert.deepEqual(pres.map((n) => n.textContent), [BLOBS["d-tools"], BLOBS["d-sys"], BLOBS["d-user"], BLOBS["d-asst"], BLOBS["d-tool"]],
    "the bytes, verbatim, one node each, in the order sent");
  assert(!metasOf(w).some((n) => n.textContent === "loading the exact bytes…"), "no placeholder survives a landing");
  // The headers are meta, in the provider's role, with the manifest's facts.
  const heads = metasOf(w).map((n) => n.textContent);
  assert(heads.some((t) => t.startsWith("── message 0 · system · 40 chars · recorded")), heads.join("\n"));
  assert(heads.some((t) => t.includes("── message 3 · tool · read_url · 30 chars · recorded") && t.includes("1 secret(s) scrubbed from the stored copy")));
  assert(heads.some((t) => t.includes("── message 4 · tool · run_command · 12 chars · recorded, then deleted") && t.includes("STUB")));
  assert(heads.some((t) => t.includes("TOOLS SENT — 1 · 120 chars · recorded")));
  assert(heads.some((t) => t.includes("MESSAGES — 6, in the order sent")));
  // Meta and payload never share a node; nothing is concatenated across messages.
  for (const n of nodesOf(w)) {
    assert(!(n.textContent.includes("── message") && (n.className || "").includes("ss-pre")), "a header leaked into payload");
  }
  assert(!pres.some((n) => n.textContent.includes("go") && n.textContent.includes("look")), "two messages were joined into one node");
  // Copy/save carry what is on screen — the landed bytes, not the placeholders.
  assert(w.sandbox.ssView.text.includes(BLOBS["d-tool"]));
  assert(!w.sandbox.ssView.text.includes("loading the exact bytes"));
  assert.equal(nodesOf(w).filter((n) => /ss-(pre|meta)/.test(n.className || "")).map((n) => n.textContent).join("\n\n"), w.sandbox.ssView.text);
  // Find runs over the landed text.
  w.sandbox.ssSetFind("needle");
  assert.equal(w.sandbox.ssFind.hits, 1);
  // The pane's one-line brief says it is the exact request.
  assert(w.sandbox.ssPaneBrief(d, d.steps[7], "context").startsWith("exact request · 6 message(s)"));
});

check("the reconstruction caveat is gone when the request is exact, and stays when it is not", async () => {
  const w = world({ fetch: evidenceFetch([]) });
  const d = sentDoc();
  w.sandbox.ssOpen(d, "m2", "context");
  await flush();
  let text = bodyText(w);
  assert(text.includes("the request as it left aish, exact"), text.slice(0, 300));
  assert(!text.includes("reconstructed from the message records"), "the caveat must not be said of an exact request");
  assert(!text.includes("not the request as it left aish"));
  w.sandbox.ssToggleWhole();
  await flush();
  text = bodyText(w);
  assert(text.includes("THE EXACT REQUEST OF MODEL CALL 2"));
  assert(!text.includes("reconstructed from the message records"));
  // A step the record does not cover (source: reconstructed) keeps the caveat,
  // in the same document.
  const mixed = sentDoc();
  mixed.steps[2].context.source = "reconstructed";
  mixed.steps[2].ref.sent = null;
  w.sandbox.ssOpen(mixed, "m1", "context");
  text = bodyText(w);
  assert(text.includes("reconstructed from the message records and the brief"), "the caveat must stay for a reconstruction");
  assert(!text.includes("the request as it left aish, exact"));
  // …and a source that says "sent" but names a call the block does not have
  // falls back to the reconstruction rather than an empty pane.
  const orphan = sentDoc();
  orphan.sent.calls = [];
  w.sandbox.ssOpen(orphan, "m1", "context");
  assert(bodyText(w).includes("reconstructed from the message records and the brief"));
});

check("an evicted, purged or unreadable blob is said in words and never rendered as payload", async () => {
  const w = world({ fetch: evidenceFetch([]) });
  const d = sentDoc();
  d.sent.evicted_on = "2026-09-01";
  // A recorded manifest entry whose fetch comes back 410 / 404 / thrown.
  d.sent.calls[1].messages[2].digest = "d-broken";
  d.sent.calls[1].messages[3].digest = "d-evicted";
  d.sent.calls[1].messages[3].state = "recorded";
  w.sandbox.ssOpen(d, "m2", "context");
  w.sandbox.ssToggleWhole();
  await flush();
  const pres = presOf(w).map((n) => n.textContent);
  assert.deepEqual(pres, [BLOBS["d-tools"], BLOBS["d-sys"], BLOBS["d-user"]], "only bytes that ARRIVED are payload");
  const metas = metasOf(w).map((n) => n.textContent);
  assert(metas.some((t) => t.includes("the bytes are gone: this chat's evidence was evicted")), "410 said in words");
  assert(metas.some((t) => t.includes("the bytes could not be read")), "a thrown fetch said in words");
  // The manifest's own states, never fetched: purged and evicted with the date.
  assert(metas.some((t) => t.includes("no bytes to show — recorded, then deleted")));
  assert(metas.some((t) => t.includes("no bytes to show — evicted on 2026-09-01")));
  // Media the request carried: named, sized, and said to be never stored.
  assert(metas.some((t) => t.includes("carried shot.png (20,480 bytes) — never stored")));
  // No node anywhere presents a status body or a state word as the model's input.
  for (const t of pres) assert(!/410 body|404 body|evicted|deleted|could not be read/.test(t), t);
  // A purged call is said at the head, without inventing bytes.
  assert(metas[0].includes("some of the bytes were recorded, then deleted"));
});

check("what changed is the exact set difference on digests against the previous request", async () => {
  const w = world({ fetch: evidenceFetch([]) });
  const d = sentDoc();
  w.sandbox.ssOpen(d, "m2", "context");
  await flush();
  const metas = metasOf(w).map((n) => n.textContent);
  assert(metas[0].includes("WHAT CHANGED SINCE MODEL CALL 1 — the exact request delta"), metas[0]);
  assert(metas.some((t) => t.includes("NEW TO THIS REQUEST — 4 message(s), by digest against the previous request")), metas.join("\n"));
  const heads = metas.filter((t) => t.startsWith("── message "));
  assert.deepEqual(heads.map((t) => t.split(" · ")[0]), ["── message 2", "── message 3", "── message 4", "── message 5"],
    "exactly the messages whose digest call 1 did not send");
  assert(metas.some((t) => t.includes("the same tool menu")));
  assert(metas.some((t) => t.includes("2 message(s) of this request were sent before, unchanged")));
  assert.deepEqual(presOf(w).map((n) => n.textContent), [BLOBS["d-asst"], BLOBS["d-tool"]], "only the new messages' bytes are fetched and shown");
  // The first call has no previous request: everything is new, and it says so.
  w.sandbox.ssOpen(d, "m1", "context");
  await flush();
  const first = metasOf(w).map((n) => n.textContent);
  assert(first[0].includes("WHAT MODEL CALL 1 SENT — the exact request"));
  assert(first.some((t) => t.includes("no earlier request of this turn is on record")));
  assert(first.some((t) => t.includes("NEW TO THIS REQUEST — 2 message(s)")));
  // A message the previous request had and this one lacks is counted, with no
  // cause asserted.
  const dropped = sentDoc();
  dropped.sent.calls[1].messages = dropped.sent.calls[1].messages.filter((m) => m.digest !== "d-user");
  w.sandbox.ssOpen(dropped, "m2", "context");
  assert(bodyText(w).includes("1 message(s) of the previous request are not in this one byte for byte"));
  // A changed menu is said, and pointed at the whole context.
  const menu = sentDoc();
  menu.sent.calls[1].tools.digest = "d-tools-2";
  w.sandbox.ssOpen(menu, "m2", "context");
  assert(bodyText(w).includes("a CHANGED tool menu — it is in the whole context"));
});

check("a fetch that lands after the view moved on writes nothing into the new pane", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const asked = [];
  const w = world({ fetch: evidenceFetch(asked, gate) });
  const d = sentDoc();
  w.sandbox.ssOpen(d, "m2", "context");
  assert(asked.length > 0, "the fetches were started");
  // The reader swipes on before any blob arrives.
  w.sandbox.ssShow(7, "response");
  const before = nodesOf(w).map((n) => n.textContent);
  const textBefore = w.sandbox.ssView.text;
  release();
  await flush();
  await flush();
  assert.deepEqual(nodesOf(w).map((n) => n.textContent), before, "a stale landing changed the pane");
  assert.equal(w.sandbox.ssView.text, textBefore);
  assert(!presOf(w).some((n) => n.textContent === BLOBS["d-tool"]));
  // The same for the whole/changed toggle: a new reading is a new view.
  let release2;
  const gate2 = new Promise((resolve) => { release2 = resolve; });
  const w2 = world({ fetch: evidenceFetch([], gate2) });
  w2.sandbox.ssOpen(sentDoc(), "m2", "context");
  w2.sandbox.ssToggleWhole();
  const placeholders = () => metasOf(w2).filter((n) => n.textContent === "loading the exact bytes…").length;
  const pending = placeholders();
  release2();
  await flush();
  await flush();
  // The whole reading's own fetches landed (started at its paint); the
  // earlier, changed reading's did not land here — the count of placeholders
  // fell to zero by landings, not by stray writes, so no node holds a blob
  // the whole reading did not ask for twice.
  assert.equal(placeholders(), 0);
  assert(pending > 0);
  const texts = presOf(w2).map((n) => n.textContent);
  assert.equal(texts.length, 5, `one payload node per recorded blob of the whole reading, got ${texts.length}`);
  // …and a closed screen takes nothing either.
  const w3 = world({ fetch: evidenceFetch([]) });
  w3.sandbox.ssOpen(sentDoc(), "m2", "context");
  w3.sandbox.ssClose();
  await flush();
  assert.equal(w3.el("ss-body").children.length, 0);
  assert.equal(w3.sandbox.ssView, null);
});

check("the Response pane shows THE COMPLETE RESPONSE from the store, beside the curated view", async () => {
  const log = [];
  const w = world({ fetch: evidenceFetch(log) });
  const d = receivedDoc();
  w.sandbox.ssOpen(d, "m1", "response");
  // the curated view is present and labelled as aish's parse.
  assert(metasOf(w).some((n) => n.textContent.includes("REASONING")), "curated reasoning stays");
  const header = metasOf(w).find((n) => n.textContent.includes("THE COMPLETE RESPONSE"));
  assert(header, "the complete response section is present");
  assert(header.textContent.includes("aish's parse"), "it labels the curated view as the parse");
  // the whole response was fetched by ITS digest and landed as payload.
  await flush();
  assert(log.some((r) => r.digest === "d-resp1"), "the response blob was fetched by digest");
  const payload = presOf(w).find((n) => n.textContent.includes('"citation"'));
  assert(payload, "the complete response (incl. new raw_blocks) is shown verbatim as payload");
  // a purged response says so in words, never fake payload.
  w.sandbox.ssOpen(d, "m2", "response");
  await flush();
  assert(metasOf(w).some((n) => n.textContent.includes("recorded, then deleted")),
    "a purged response is said in words");
  assert(!log.some((r) => r.digest === "d-resp2"), "a purged response is never fetched");
});

check("wrap is on by default, the toggle is remembered, and \"0\" turns it off", () => {
  let w = world();
  assert(w.el("ss-body").classList.has("wrap-on"), "wrap must default on");
  const store = new Map();
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  };
  w = world({ localStorage });
  assert(w.el("ss-body").classList.has("wrap-on"));
  w.el("ss-wrap").onclick();
  assert(!w.el("ss-body").classList.has("wrap-on"));
  assert.equal(store.get("aish-ss-wrap"), "0");
  // A fresh world with the same storage opens with wrap off, and back on.
  w = world({ localStorage });
  assert(!w.el("ss-body").classList.has("wrap-on"), "the stored \"0\" must hold");
  w.el("ss-wrap").onclick();
  assert(w.el("ss-body").classList.has("wrap-on"));
  assert.equal(store.get("aish-ss-wrap"), "1");
});

check("the scroll arrows follow the chat's directional rule and the keys page", () => {
  const w = world();
  const s = w.sandbox;
  s.ssOpen(doc(), "c1", "result");
  const body = w.el("ss-body");
  body.scrollHeight = 5000;
  body.clientHeight = 600;
  // Scrolling DOWN far from the bottom shows the down arrow; the up arrow stays hidden.
  body.scrollTop = 800;
  s.ssUpdateScrollArrows();
  assert.equal(w.el("ss-scroll-down").hidden, false, "down arrow while scrolling down");
  assert.equal(w.el("ss-scroll-top").hidden, true);
  // Scrolling UP far from the top swaps them.
  body.scrollTop = 700;
  s.ssUpdateScrollArrows();
  assert.equal(w.el("ss-scroll-down").hidden, true, "down arrow hides on an upward move");
  assert.equal(w.el("ss-scroll-top").hidden, false, "up arrow while scrolling up");
  // Near the top, the up arrow is not worth a button.
  body.scrollTop = 100;
  s.ssUpdateScrollArrows();
  assert.equal(w.el("ss-scroll-top").hidden, true);
  // Tapping an arrow moves the scroller (this world has no smooth scrollTo).
  body.scrollTop = 800;
  w.el("ss-scroll-down").onclick();
  assert.equal(body.scrollTop, 5000);
  w.el("ss-scroll-top").onclick();
  assert.equal(body.scrollTop, 0);
  // Page keys move by a page and never above the top.
  s.ssScrollPage(1);
  assert(body.scrollTop > 0);
  s.ssScrollPage(-1);
  assert.equal(body.scrollTop, 0);
  s.ssScrollPage(-1);
  assert.equal(body.scrollTop, 0, "never scrolls above the top");
  // A new step hides both arrows (the scroller is reset to the top).
  w.el("ss-scroll-down").hidden = false;
  s.ssGo(1);
  assert.equal(w.el("ss-scroll-down").hidden, true);
  assert.equal(w.el("ss-scroll-top").hidden, true);
});

check("the header drag acts like a physical sheet: position AND velocity decide", () => {
  const w = world();
  const s = w.sandbox;
  s.ssOpen(doc(), "c1", "result");
  const box = w.el("step-screen");
  // The screen follows the finger, and a slow release past the threshold closes.
  s.ssDragStart({ touches: [{ clientY: 100 }], timeStamp: 0 });
  s.ssDragMove({ touches: [{ clientY: 180 }], timeStamp: 100 });
  assert.equal(box.style.transform, "translateY(80px)", "the sheet follows the finger");
  s.ssDragMove({ touches: [{ clientY: 260 }], timeStamp: 200 });
  s.ssDragEnd({ timeStamp: 700 }); // set down (rested) past the threshold
  assert.equal(s.ssIsOpen(), false, "resting past the threshold, release closes");
  assert.equal(box.style.transform, "", "the transform is reset for the next open");
  // Brought down PAST the threshold and back up: the sheet STAYS — the
  // physical model, and the case the owner named.
  s.ssOpen(doc(), "c1", "result");
  s.ssDragStart({ touches: [{ clientY: 100 }], timeStamp: 0 });
  s.ssDragMove({ touches: [{ clientY: 400 }], timeStamp: 150 });
  s.ssDragMove({ touches: [{ clientY: 250 }], timeStamp: 300 });
  s.ssDragEnd({ timeStamp: 340 });
  assert.equal(s.ssIsOpen(), true, "a sheet brought back up stays");
  assert(box.classList.has("ss-settling"), "the return is animated");
  // A quick flick down closes from any distance (released mid-motion).
  s.ssDragStart({ touches: [{ clientY: 100 }], timeStamp: 0 });
  s.ssDragMove({ touches: [{ clientY: 140 }], timeStamp: 20 });
  s.ssDragMove({ touches: [{ clientY: 180 }], timeStamp: 40 });
  s.ssDragEnd({ timeStamp: 48 });
  assert.equal(s.ssIsOpen(), false, "a flick closes short of the threshold");
  // A quick move DOWN then a HOLD before lifting is NOT a flick: the finger
  // rested, so at release the sheet is set down, not thrown — and short of the
  // threshold it stays. This is the case release-timing exists for.
  s.ssOpen(doc(), "c1", "result");
  s.ssDragStart({ touches: [{ clientY: 100 }], timeStamp: 0 });
  s.ssDragMove({ touches: [{ clientY: 150 }], timeStamp: 15 });
  s.ssDragEnd({ timeStamp: 400 }); // held 385ms at rest before lifting
  assert.equal(s.ssIsOpen(), true, "a move then a hold is not a flick");
  // Below the threshold, slow, it springs back and stays open.
  s.ssOpen(doc(), "c1", "result");
  s.ssDragStart({ touches: [{ clientY: 100 }], timeStamp: 0 });
  s.ssDragMove({ touches: [{ clientY: 160 }], timeStamp: 400 });
  s.ssDragEnd({ timeStamp: 450 });
  assert.equal(s.ssIsOpen(), true, "below the threshold it stays open");
  assert.equal(box.style.transform, "");
  // An upward drag does nothing (the sheet only moves down).
  s.ssDragStart({ touches: [{ clientY: 300 }], timeStamp: 0 });
  s.ssDragMove({ touches: [{ clientY: 100 }], timeStamp: 100 });
  assert.equal(box.style.transform, "");
  s.ssDragEnd({ timeStamp: 120 });
  assert.equal(s.ssIsOpen(), true);
  // A content pull — even from the very top — is a scroll, never a dismissal.
  const body = w.el("ss-body");
  body.scrollTop = 0;
  s.ssTouchStart({ touches: [{ clientX: 200, clientY: 200 }] });
  s.ssTouchMove({ touches: [{ clientX: 202, clientY: 320 }] });
  assert.equal(s.ssGesture, "scroll");
  s.ssTouchEnd({ changedTouches: [{ clientX: 203, clientY: 340 }] });
  assert.equal(s.ssIsOpen(), true, "content touches never close the screen");
});

check("A−/A+ scale the pane text, clamp at both ends, and survive a reopen", async () => {
  // No localStorage in this world: the default applies and bumps still work.
  let w = world();
  assert.equal(w.sandbox.$("ss-body").style.fontSize, "12px");
  w.sandbox.$("ss-font-inc").onclick();
  assert.equal(w.sandbox.$("ss-body").style.fontSize, "13px");
  // Clamp: bumping past either end sticks at the end and disables the button.
  const store = new Map();
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  };
  w = world({ localStorage });
  for (let i = 0; i < 30; i++) w.sandbox.$("ss-font-inc").onclick();
  assert.equal(w.sandbox.$("ss-body").style.fontSize, "22px");
  assert.equal(w.sandbox.$("ss-font-inc").disabled, true);
  assert.equal(w.sandbox.$("ss-font-dec").disabled, false);
  // Remembered: a fresh world with the same storage opens at the saved size.
  const w2 = world({ localStorage });
  assert.equal(w2.sandbox.$("ss-body").style.fontSize, "22px");
  for (let i = 0; i < 30; i++) w2.sandbox.$("ss-font-dec").onclick();
  assert.equal(w2.sandbox.$("ss-body").style.fontSize, "9px");
  assert.equal(w2.sandbox.$("ss-font-dec").disabled, true);
  // A stored value outside the range is ignored, never applied.
  store.set("aish-ss-font", "99");
  assert.equal(world({ localStorage }).sandbox.$("ss-body").style.fontSize, "12px");
});

(async () => {
  for (const run of pending) await run();
  if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
  console.log("step screen: all checks passed");
})();
