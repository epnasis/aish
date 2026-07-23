// Node-only, dependency-free regression check for the interactive PTY line
// model (issue #148). Exercises the REAL ptyApply / ptyRenderScreen / ansiFragment
// pulled out of app.js by marker and run in a vm against a tiny fake DOM, so the
// shipped rendering logic is tested — not a hand-copied duplicate.
//
// Run manually: node tests/js/test_pty_render.js
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

// --- minimal fake DOM: only what the extracted functions touch ---------------

const TEXT = 3;
class FakeNode {
  constructor() { this.parent = null; this.children = []; }
  appendChild(node) {
    if (node.nodeType === 11) { // fragment: hoist children in order
      for (const child of node.children.slice()) this.appendChild(child);
      node.children = [];
      return node;
    }
    if (node.parent) node.remove();
    node.parent = this; this.children.push(node); return node;
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
  set textContent(v) { this.children = []; if (v) this.appendChild(new FakeText(v)); }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  _walk(out) { for (const c of this.children) { if (c.nodeType === 1) { out.push(c); c._walk(out); } } }
  querySelectorAll(sel) {
    const cls = sel.replace(/^\./, "");
    const all = []; this._walk(all);
    return all.filter((n) => hasClass(n, cls));
  }
}
class FakeFragment extends FakeNode {
  constructor() { super(); this.nodeType = 11; }
}
const fakeDocument = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (data) => new FakeText(data),
  createDocumentFragment: () => new FakeFragment(),
};

// --- pull the shipped logic and run it against the fake DOM ------------------

const sandbox = { document: fakeDocument };
vm.createContext(sandbox);
// The PTY block (ptyNewState..ptyRenderScreen) plus the ANSI helpers it calls
// (ansiFragment/applySgr live just above it). const -> var so re-declares don't
// trip strict mode across the concatenated snippets.
vm.runInContext(
  (
    extract("const PTY_MAX_LINES", "// ---- end interactive PTY line model") +
    extract("function ansiFragment", "function applySgr") +
    extract("function applySgr", "// ---- interactive PTY: line model")
  ).replace(/\bconst\b/g, "var"),
  sandbox
);
const { ptyNewState, ptyApply, ptyRenderScreen } = sandbox;
assert(typeof ptyApply === "function", "failed to extract ptyApply");
assert(typeof ptyRenderScreen === "function", "failed to extract ptyRenderScreen");

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}\n       ${err.message}`); }
}

// Array.from lifts the vm-realm array into this realm so deepStrictEqual (which
// checks prototypes) compares by value rather than rejecting a cross-realm Array.
const feed = (chunk) => { const st = ptyNewState(); ptyApply(st, chunk); return Array.from(st.lines); };

check("plain newlines split into lines", () => {
  assert.deepStrictEqual(feed("a\nb\nc"), ["a", "b", "c"]);
});

check("carriage return overwrites from column 0", () => {
  // A progress-bar style rewrite: the second write lands over the first.
  assert.deepStrictEqual(feed("Downloading 50%\rDownloading 90%"), ["Downloading 90%"]);
});

check("a shorter CR rewrite leaves the tail unless erased", () => {
  assert.deepStrictEqual(feed("abcdef\rXY"), ["XYcdef"]);
});

check("CR + erase-to-end clears the tail (\\x1b[K)", () => {
  assert.deepStrictEqual(feed("abcdef\rXY\x1b[K"), ["XY"]);
});

check("backspace steps the cursor back and overwrites", () => {
  assert.deepStrictEqual(feed("abc\b\bX"), ["aXc"]);
});

check("tab expands to the next 8-column stop", () => {
  assert.deepStrictEqual(feed("ab\tc"), ["ab      c"]); // cols 2->8, then c at 8
});

check("bell and stray control chars are dropped", () => {
  assert.deepStrictEqual(feed("a\x07b\x00c"), ["abc"]);
});

check("CRLF newlines do not double up", () => {
  assert.deepStrictEqual(feed("one\r\ntwo\r\n"), ["one", "two", ""]);
});

check("SGR color survives into the line text and renders as a styled span", () => {
  const st = ptyNewState();
  ptyApply(st, "\x1b[31mred\x1b[0m ok\n");
  assert.deepStrictEqual(Array.from(st.lines), ["\x1b[31mred\x1b[0m ok", ""]);
  const el = new FakeElement("div");
  ptyRenderScreen(el, st);
  const rows = el.querySelectorAll(".ptol");
  assert.strictEqual(rows.length, 2, "one .ptol span per line");
  assert.strictEqual(rows[0].textContent, "red ok", "escape codes are not visible text");
  assert.strictEqual(el.querySelectorAll(".a-fg31").length, 1, "color span emitted");
});

check("a cursor-addressing CSI (not K/m) is dropped without corrupting text", () => {
  // \x1b[2J (clear screen) and \x1b[H (home) are ignored; text still renders.
  assert.deepStrictEqual(feed("\x1b[2J\x1b[Hhello"), ["hello"]);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
