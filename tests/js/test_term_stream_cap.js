// Node-only, dependency-free regression check for issue #109 (the live web
// terminal block growing unboundedly during a command with huge output, then
// freezing the tab). Exercises the REAL appendTermLines/capTermLines from
// app.js (pulled out by marker and run in a vm context against a tiny fake
// DOM), so the shipped trimming logic is tested, not a hand-copied duplicate.
//
// Run manually: node tests/js/test_term_stream_cap.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

// --- minimal fake DOM: only the operations the extracted functions touch ----

const TEXT = 3;
class FakeNode {
  constructor() { this.parent = null; this.children = []; }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() {
    if (!this.parent) return null;
    const i = this.parent.children.indexOf(this);
    return this.parent.children[i + 1] || null;
  }
  _detach(node) { if (node.parent) node.remove(); node.parent = this; }
  appendChild(node) {
    if (node.nodeType === 11) { // document fragment: hoist its children in order
      for (const child of node.children.slice()) this.appendChild(child);
      node.children = [];
      return node;
    }
    this._detach(node); this.children.push(node); return node;
  }
  insertBefore(node, ref) {
    if (ref == null) return this.appendChild(node);
    if (node.nodeType === 11) {
      for (const child of node.children.slice()) this.insertBefore(child, ref);
      node.children = [];
      return node;
    }
    this._detach(node);
    this.children.splice(this.children.indexOf(ref), 0, node);
    return node;
  }
  remove() {
    if (!this.parent) return;
    const i = this.parent.children.indexOf(this);
    if (i !== -1) this.parent.children.splice(i, 1);
    this.parent = null;
  }
}
class FakeText extends FakeNode {
  constructor(data) { super(); this.nodeType = TEXT; this._data = data; }
  get textContent() { return this._data; }
}
function hasClass(node, cls) {
  return node.className && node.className.split(/\s+/).includes(cls);
}
class FakeElement extends FakeNode {
  constructor(tag) { super(); this.nodeType = 1; this.tagName = tag; this.className = ""; }
  set textContent(v) { this.children = []; this.appendChild(new FakeText(v)); }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  _walk(out) {
    for (const c of this.children) {
      if (c.nodeType === 1) { out.push(c); c._walk(out); }
    }
  }
  querySelectorAll(sel) {
    const cls = sel.replace(/^\./, "");
    const all = []; this._walk(all);
    return all.filter((n) => hasClass(n, cls));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
class FakeFragment extends FakeNode {
  constructor() { super(); this.nodeType = 11; }
}
const fakeDocument = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (data) => new FakeText(data),
  createDocumentFragment: () => new FakeFragment(),
};

// --- pull the shipped logic and run it against the fake DOM -----------------

const sandbox = { document: fakeDocument };
vm.createContext(sandbox);
const snippet = (
  extract("function appendTermLines", "function capTermLines") +
  extract("function capTermLines", "// One scroll + pin per animation frame") +
  extract("function ansiFragment", "function applySgr") +
  extract("function applySgr", "// ---- syntax highlighting")
).replace(/\bconst\b/g, "var");
vm.runInContext("var TERM_LIVE_LINE_CAP = 5;\n" + snippet, sandbox);
const { appendTermLines } = sandbox;
assert(typeof appendTermLines === "function", "failed to extract appendTermLines");

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}\n       ${err.message}`); }
}

const CAP = 5; // matches the override injected above
const lines = (body) => body.querySelectorAll(".tol").map((n) => n.textContent);

check("each output line becomes its own .tol span", () => {
  const body = new FakeElement("div");
  appendTermLines(body, "alpha");
  appendTermLines(body, "beta");
  assert.deepStrictEqual(lines(body), ["alpha", "beta"]);
});

check("a coalesced multi-line chunk is split into one span per line", () => {
  const body = new FakeElement("div");
  appendTermLines(body, "a\nb\nc");
  assert.deepStrictEqual(lines(body), ["a", "b", "c"]);
});

check("output over the cap keeps only the last N lines", () => {
  const body = new FakeElement("div");
  for (let i = 0; i < 20; i++) appendTermLines(body, `L${i}`);
  const kept = lines(body);
  assert.strictEqual(kept.length, CAP, "line count must be pinned at the cap");
  assert.deepStrictEqual(kept, ["L15", "L16", "L17", "L18", "L19"]);
});

check("a single trim marker is prepended once, not per trim", () => {
  const body = new FakeElement("div");
  for (let i = 0; i < 50; i++) appendTermLines(body, `L${i}`);
  const notes = body.querySelectorAll(".term-trim-note");
  assert.strictEqual(notes.length, 1, "exactly one trim marker");
  assert.strictEqual(body.firstChild, notes[0], "marker sits at the top");
  assert.ok(/trimmed/.test(notes[0].textContent));
  assert.strictEqual(lines(body).length, CAP);
});

check("under the cap, nothing is trimmed and no marker appears", () => {
  const body = new FakeElement("div");
  for (let i = 0; i < CAP; i++) appendTermLines(body, `L${i}`);
  assert.strictEqual(body.querySelectorAll(".term-trim-note").length, 0);
  assert.strictEqual(lines(body).length, CAP);
});

check("ANSI colour codes render as styled spans without breaking the line count", () => {
  const body = new FakeElement("div");
  appendTermLines(body, "\x1b[31mred\x1b[0m plain");
  const tols = body.querySelectorAll(".tol");
  assert.strictEqual(tols.length, 1);
  assert.strictEqual(tols[0].textContent, "red plain");
  assert.ok(body.querySelectorAll(".a-fg31").length === 1, "colour span emitted");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
