// Node-only, dependency-free check for the owner's sentence on a trace row and
// the hold → replacement join (#323).
//
// Approve-with-comment never runs what was on the card: it HOLDS that action
// and tells the model to re-propose. The timeline therefore contains a dead row
// followed by a fresh proposal, and until #323 nothing joined them — so the two
// halves of one decision read as two unrelated ideas.
//
// The half that has to be got right is the WORDING. The record asserts exactly
// one thing — the first later call to the same tool while a hold was
// outstanding — and the row may say that and nothing more. "Reworked from" or
// "in response to your comment" would be a claim nothing checked, which is the
// same defect as the field this issue replaced.
//
// Runs the REAL toolFinish / heldJoin / revealStep / clampNote extracted from
// app.js by marker, never a hand-copied duplicate.
//
// Run manually: node tests/js/test_held_join.js
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

// `textContent` AGGREGATES, like the real thing: the join line is a button with
// two child spans, and a fake that ignored children would read it back as empty
// and pass every wording check without looking at a word.
function makeElement(tag) {
  let own = "";
  const found = new Map();
  const el = {
    tagName: tag, className: "", innerHTML: "", src: "", alt: "", type: "",
    loading: "", children: [], style: {}, dataset: {}, onclick: null,
    scrolledIntoView: null,
    get textContent() {
      return own + el.children.map((c) => c.textContent || "").join("");
    },
    set textContent(value) {
      own = value == null ? "" : String(value);
      el.children.length = 0;
    },
    append(...nodes) { el.children.push(...nodes); },
    appendChild(node) { el.children.push(node); return node; },
    remove() {},
    addEventListener() {},
    scrollIntoView(opts) { el.scrolledIntoView = opts || {}; },
    classList: {
      _set: new Set(),
      add(...cs) { cs.forEach((c) => this._set.add(c)); },
      remove(...cs) { cs.forEach((c) => this._set.delete(c)); },
      contains(c) { return this._set.has(c); },
      toggle(c) {
        if (this._set.has(c)) { this._set.delete(c); return false; }
        this._set.add(c); return true;
      },
    },
    querySelector(sel) {
      const real = findByClass(el, sel);
      if (real) return real;
      // Parts of the card are built with innerHTML, which this fake does not
      // parse; hand back a stable stand-in so ensureTrace can wire them.
      if (!found.has(sel)) found.set(sel, makeElement("div"));
      return found.get(sel);
    },
    querySelectorAll() { return []; },
  };
  return el;
}

function findByClass(el, sel) {
  if (!sel.startsWith(".")) return null;
  const cls = sel.slice(1);
  for (const child of el.children || []) {
    if (typeof child.className === "string"
        && child.className.split(/\s+/).includes(cls)) return child;
    const deeper = child.children ? findByClass(child, sel) : null;
    if (deeper) return deeper;
  }
  return null;
}

function makeSandbox() {
  const timers = [];
  const sandbox = {
    document: { createElement: makeElement, createTextNode: (t) => ({ textContent: t }) },
    messagesEl: makeElement("div"),
    replaying: false,
    turnStart: 0,
    currentTrace: null,
    currentTurnId: "",
    turnAnchorEl: null,
    SPINNER: "",
    TOOL_META: {
      run_command: ["Ran command", "command", "--blue"],
      write_file: ["Wrote file", "file", "--blue"],
    },
    traceSvg: () => "",
    fmtSecs: (s) => `${s}s`,
    timers,
    imageSrc: () => null,
    frameSrc: () => null,
    openPreview() {},
    knowledgeTag() {},
    renderDiff: () => makeElement("div"),
    renderErrorBox() {},
    updateTraceHead() {},
    updateScrollHints() {},
    measurePinnedTrace() {},
    scrollToEnd() {},
    removeQueueChip() {},
    finalizeAnswerRow() {},
    refreshStatusline() {},
    setInterval: () => 0,
    clearInterval() {},
    // Held rather than run: the flash has to still be ON the row when the test
    // looks, and firing it lets the test check that it is taken back off.
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    requestAnimationFrame: (fn) => { timers.push(fn); },
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("// [TRACE-OPEN-START]", "// [TRACE-OPEN-END]"), sandbox);
  vm.runInContext(slice("function pinTrace(t) {", "const WRAP_SVG"), sandbox);
  vm.runInContext(slice("function clampNote(text) {", "function renderErrorBox("), sandbox);
  assert(typeof sandbox.toolFinish === "function", "toolFinish not extracted");
  assert(typeof sandbox.heldJoin === "function", "heldJoin not extracted");
  assert(typeof sandbox.revealStep === "function", "revealStep not extracted");
  assert(typeof sandbox.clampNote === "function", "clampNote not extracted");
  return sandbox;
}

// One tool call, start → finish, through the real dispatcher.
function step(s, extra) {
  const name = extra.name || "run_command";
  s.traceStep({ kind: "tool_start", name, summary: extra.summary || "", call: extra.call });
  s.traceStep({ kind: "tool", name, secs: 0.4, ok: true, summary: "", ...extra });
}

function rows(s) {
  return (s.currentTrace.inner.children || []).filter((r) => r.className === "step");
}

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}`);
    console.error(`       ${err.message}`);
  }
}

// A held action and the proposal that followed it, as the agent records them.
function heldThenReplacement(s, replacement) {
  step(s, { call: 1, command: "rm -rf build", decision: "held", comment: "use trash, not rm",
            ok: false });
  step(s, { call: 2, command: "trash build", decision: "approved", replaces: 1, ...replacement });
  return rows(s);
}

// ---------------------------------------------------------------------------
// The join

check("the replacement says it was proposed after the step that was held", () => {
  const s = makeSandbox();
  const r = heldThenReplacement(s);
  const join = findByClass(r[1], ".step-after-held");
  assert(join, "the replacement carried no join back to the held step");
  assert(/Proposed after the held step/.test(join.textContent), join.textContent);
});

check("the join claims an ORDERING and never a rework", () => {
  // The whole point of the row. `replaces` is set by "the first later call to
  // the same tool while a hold was outstanding" — the harness never compared
  // the two actions, never read the comment, and never asked the model what it
  // did. Any of these words would state a finding nothing produced.
  const s = makeSandbox();
  const join = findByClass(heldThenReplacement(s)[1], ".step-after-held");
  for (const claim of [
    /rework/i, /corrected/i, /revis/i, /instead of/i, /replaces/i,
    /in response/i, /your comment/i, /as you asked/i, /adjusted/i, /fixed/i,
  ]) {
    assert(!claim.test(join.textContent),
           `the row claims more than the record: ${claim} in "${join.textContent}"`);
  }
});

check("tapping the join reveals the held step", () => {
  // "What was held?" is the reader's next question and the answer is already
  // on the record — one row up, with its own struck command.
  const s = makeSandbox();
  const r = heldThenReplacement(s);
  const join = findByClass(r[1], ".step-after-held");
  assert.equal(join.tagName, "button");
  join.onclick({ stopPropagation() {} });
  assert(r[0].scrolledIntoView, "the held row was never brought into view");
  // Minimal movement: a row already on screen must not scroll at all, and the
  // jump is a few rows inside one card, not a long animated hop.
  assert.equal(r[0].scrolledIntoView.block, "nearest");
  assert(r[0].classList.contains("step-flash"), "nothing said WHICH row it landed on");
});

check("the flash is taken back off", () => {
  const s = makeSandbox();
  const r = heldThenReplacement(s);
  findByClass(r[1], ".step-after-held").onclick({ stopPropagation() {} });
  s.timers.forEach((fn) => fn());
  assert(!r[0].classList.contains("step-flash"), "the row kept its highlight for good");
});

check("a join with nothing to jump to states the fact and offers nothing", () => {
  // A partial replay, or a log whose head the bounded first paint did not
  // reach. A control that navigates nowhere reads as broken (L7).
  const s = makeSandbox();
  step(s, { call: 9, command: "trash build", decision: "approved", replaces: 4 });
  const join = findByClass(rows(s)[0], ".step-after-held");
  assert(join, "the fact was dropped along with the offer");
  assert.equal(join.tagName, "span");
  assert.equal(join.onclick, null);
  assert(/Proposed after the held step/.test(join.textContent), join.textContent);
});

check("a step with no hold behind it draws no join", () => {
  const s = makeSandbox();
  step(s, { call: 1, command: "ls", decision: "approved" });
  assert(!findByClass(rows(s)[0], ".step-after-held"));
  assert.equal(s.heldJoin({ heldRows: new Map() }, { call: 1 }), null);
});

check("the join asserts no problem: no error styling, no error voice", () => {
  // The console block's lesson, one week old: a red block on a step nothing
  // was observed to be wrong with paints a working flow as a failure. A hold
  // is a pause the OWNER caused, and the replacement is the flow continuing.
  const s = makeSandbox();
  const join = findByClass(heldThenReplacement(s)[1], ".step-after-held");
  assert.equal(join.className, "step-after-held");
  assert(!/error|fail|denied|bad|problem/i.test(join.className), join.className);
});

check("the join is only ever drawn on the row that carries the key", () => {
  // The held row itself must not grow one, or the pair points at each other.
  const s = makeSandbox();
  const r = heldThenReplacement(s);
  assert(!findByClass(r[0], ".step-after-held"), "the held row pointed back at itself");
});

check("a hold and its replacement render identically live and on replay", () => {
  // L2: a chat redrawn from its log shows what the live turn showed.
  const live = makeSandbox();
  const cold = makeSandbox();
  cold.replaying = true;
  const a = findByClass(heldThenReplacement(live)[1], ".step-after-held");
  const b = findByClass(heldThenReplacement(cold)[1], ".step-after-held");
  assert.equal(a.tagName, b.tagName);
  assert.equal(a.className, b.className);
  assert.equal(a.textContent, b.textContent);
});

// ---------------------------------------------------------------------------
// The owner's sentence, and the reason line under it

check("his comment renders on a run_command, in his voice", () => {
  // It used to arrive in `output` and render as a machine sub-line, so the
  // sentence he typed was styled as something aish generated — the report
  // this issue came from.
  const s = makeSandbox();
  step(s, { call: 1, command: "rm -rf build", decision: "denied", ok: false,
            comment: "not what i asked", output: "" });
  const row = rows(s)[0];
  const note = findByClass(row, ".step-note");
  assert(note, "his sentence was not drawn");
  assert(/not what i asked/.test(note.textContent), note.textContent);
  // Quoted, so it reads as reported speech rather than as aish's own account.
  assert(/“not what i asked”/.test(note.textContent), note.textContent);
});

check("his note says whose words they are", () => {
  // A trace row is aish's account of its own step, and every other dim line on
  // it is machine-written — a skipped frame's reason is italic too — so quote
  // marks alone left his sentence reading as aish quoting itself. Earned:
  // `comment` is defined as what the owner typed on the card.
  const s = makeSandbox();
  step(s, { call: 1, command: "rm -r build", decision: "held", ok: false,
            comment: "use trash, not rm" });
  const who = findByClass(rows(s)[0], ".step-note-who");
  assert(who, "the note claimed no speaker");
  assert.equal(who.textContent, "you said");
  // The label is not inside the quotation, or it reads as words he typed.
  const note = findByClass(rows(s)[0], ".step-note");
  assert.equal(note.textContent, "you said“use trash, not rm”");
});

check("the join sits on the title, never under the row's output", () => {
  // command_start draws the terminal block into the row body BEFORE the step
  // finishes, so appending there puts the provenance of an action below its
  // output — arbitrarily far from the row it is about, and past a scroll on a
  // long one. Measured in a real browser at 430px before it was moved.
  const s = makeSandbox();
  const r = heldThenReplacement(s);
  const title = findByClass(r[1], ".step-title");
  assert(title, "the row has no title element");
  assert(title.children.some((c) => c.className === "step-after-held"),
         "the join is not on the title");
});

check("the empty why-line of a post-#323 denial draws nothing at all", () => {
  // `output` is "" on these rows now. The reason is above, in his voice; a
  // blank sub-line here would be a gap where a reason used to be.
  const s = makeSandbox();
  step(s, { call: 1, command: "rm -rf build", decision: "denied", ok: false,
            comment: "not what i asked", output: "" });
  const subs = [];
  (function walk(el) {
    for (const c of el.children || []) {
      if (c.className === "step-sub") subs.push(c);
      walk(c);
    }
  })(rows(s)[0]);
  assert.deepEqual(subs.map((x) => x.textContent), [],
                   `a bare sub-line was drawn: ${JSON.stringify(subs.map((x) => x.textContent))}`);
});

check("a log written BEFORE #323 still renders its reason", () => {
  // Verified against real logs on this machine: pre-#323 rows carry the
  // sentence in `output` with no `comment` at all. Parsing in the renderer is
  // what keeps those sessions readable, so this is not allowed to regress.
  const s = makeSandbox();
  step(s, { call: 1, command: "uv pip install x", decision: "denied", ok: false,
            output: "you have uv for packages" });
  const row = rows(s)[0];
  const why = findByClass(row, ".step-sub");
  assert(why, "an old log lost its reason");
  assert(/you have uv for packages/.test(why.textContent), why.textContent);
});

check("an old log's reason keeps the machine voice, because nothing knows whose it is", () => {
  // These same logs carry gate wording in this key — "rm -rf: recursive force
  // delete is unrecoverable" is in one of them. Promoting `output` into his
  // voice would attribute a sentence aish wrote to the owner, which is the
  // defect #323 exists to end rather than a cosmetic one.
  const s = makeSandbox();
  step(s, { call: 1, command: "rm -rf /", decision: "blocked", ok: false,
            output: "rm -rf: recursive force delete is unrecoverable" });
  assert(!findByClass(rows(s)[0], ".step-note"),
         "a gate's own wording was rendered as the owner's sentence");
});

check("a row carrying both never says it twice", () => {
  const s = makeSandbox();
  step(s, { call: 1, command: "rm -rf build", decision: "denied", ok: false,
            comment: "not what i asked", output: "not what i asked" });
  const row = rows(s)[0];
  const hits = (row.textContent.match(/not what i asked/g) || []).length;
  assert.equal(hits, 1, `the sentence appeared ${hits} times`);
});

check("a held write still says the change was not applied", () => {
  // The pre-existing behaviour on the write path, which carried `comment` all
  // along — it must not have moved when run_command joined it.
  const s = makeSandbox();
  step(s, { name: "write_file", call: 1, decision: "held", ok: false,
            comment: "wrong directory" });
  const row = rows(s)[0];
  assert(/Change not applied/.test(row.textContent), row.textContent);
  assert(/wrong directory/.test(findByClass(row, ".step-note").textContent));
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
