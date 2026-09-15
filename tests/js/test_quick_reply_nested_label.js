// Node-only, dependency-free checks for issue #373: a quick-reply chip whose
// label carries its own bracketed run —
//
//     [Pokaż transakcje z konta OPERACYJNE [a4]](aish-reply://…)
//
// — must render as a button, and the button must submit the chip's payload.
// INLINE_RE's label group used to be [^\]\n]+, so the inner "]" ended the
// label one character early and the whole line fell through as literal text.
//
// The REAL renderMarkdown / inlineMd / quickReplyChip are pulled out of app.js
// by marker and evaluated in a vm against a minimal fake DOM — the shipped code
// is exercised, never a hand-copied duplicate. The composer and submit path
// are stubbed so a click can be observed: a chip that renders but fires with
// the wrong reply text would be worse than the original failure.
//
// Run manually: node tests/js/test_quick_reply_nested_label.js
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

// ---- minimal fake DOM -----------------------------------------------------
function makeElement(tag) {
  const node = {
    tagName: tag ? tag.toUpperCase() : tag,
    nodeType: tag ? 1 : 11,
    children: [],
    dataset: {},
    _attrs: {},
    _text: "",
    appendChild(child) { this.children.push(child); return child; },
    append(...kids) { for (const k of kids) this.children.push(k); },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; },
  };
  const cls = new Set();
  node.classList = {
    add: (c) => { cls.add(c); node.className = [...cls].join(" "); },
    remove: (c) => { cls.delete(c); node.className = [...cls].join(" "); },
    contains: (c) => cls.has(c),
    toggle: (c) => { cls.has(c) ? cls.delete(c) : cls.add(c); node.className = [...cls].join(" "); },
  };
  Object.defineProperty(node, "textContent", {
    get() {
      if (this.children.length) return this.children.map((c) => c.textContent || "").join("");
      return this._text;
    },
    set(v) { this._text = String(v); this.children = []; },
  });
  return node;
}

const documentFake = {
  createElement: (tag) => makeElement(tag),
  createDocumentFragment: () => makeElement(null),
  createTextNode: (t) => ({ nodeType: 3, _text: String(t), children: [], get textContent() { return this._text; } }),
};

// The composer and the submit path, as quickReplyChip's onclick sees them.
const submitted = [];
const inputFake = {
  value: "",
  setSelectionRange() {},
  focus() {},
};

const sandbox = {
  document: documentFake,
  WRAP_SVG: "",
  copyChip: () => makeElement("button"),
  token: "",
  console,
  input: inputFake,
  showToast: (msg) => { throw new Error(`unexpected toast: ${msg}`); },
  resizeInput() {},
  submitInput: (opts) => { submitted.push({ text: inputFake.value, opts }); },
};
vm.createContext(sandbox);

const snippet = (
  extract("const FENCE_RE", "function stableBoundary") + "\n" +
  extract("function renderMarkdown", "// ---- read aloud") + "\n" +
  "this.renderMarkdown = renderMarkdown; this.inlineMd = inlineMd;"
).replace(/\bconst\b/g, "var");
vm.runInContext(snippet, sandbox);
const { renderMarkdown } = sandbox;
assert(typeof renderMarkdown === "function", "failed to extract renderMarkdown from app.js");

function collect(node, tag, out = []) {
  for (const child of node.children || []) {
    if (child.tagName === tag) out.push(child);
    collect(child, tag, out);
  }
  return out;
}

function chips(md) {
  const frag = renderMarkdown(md);
  const buttons = collect(frag, "BUTTON").filter((b) => (b.className || "").includes("quick-reply"));
  return { frag, buttons };
}

// Click a chip with an empty composer and return what it submitted.
function press(btn) {
  submitted.length = 0;
  inputFake.value = "";
  btn.onclick();
  assert.strictEqual(submitted.length, 1, "one tap submits exactly one turn");
  assert.strictEqual(submitted[0].opts.fromChip, true, "sent as a message, never a command");
  return submitted[0].text;
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

check("a plain label still renders as one button that submits its payload", () => {
  const { buttons } = chips("[Yes please](aish-reply://yes)");
  assert.strictEqual(buttons.length, 1);
  assert.strictEqual(buttons[0].textContent, "Yes please");
  assert.strictEqual(press(buttons[0]), "yes");
});

check("the #373 label — one nested [a4] — renders as a button and fires its payload", () => {
  const md = "[Pokaż transakcje z konta OPERACYJNE [a4]](aish-reply://pokaż transakcje z konta a4)";
  const { frag, buttons } = chips(md);
  assert.strictEqual(buttons.length, 1, "expected one quick-reply button");
  assert.strictEqual(buttons[0].textContent, "Pokaż transakcje z konta OPERACYJNE [a4]");
  assert.strictEqual(press(buttons[0]), "pokaż transakcje z konta a4");
  assert(!frag.textContent.includes("aish-reply://"), "no raw chip syntax left in the paragraph");
});

check("text after the nested bracket stays in the label", () => {
  const { buttons } = chips("[Show [a4] transactions now](aish-reply://show a4)");
  assert.strictEqual(buttons.length, 1);
  assert.strictEqual(buttons[0].textContent, "Show [a4] transactions now");
  assert.strictEqual(press(buttons[0]), "show a4");
});

check("several chips on one line each render and each fires its own payload", () => {
  const md = "[Konto [a4]](aish-reply://a4) [Konto [b7]](aish-reply://b7) [Wszystkie](aish-reply://all)";
  const { buttons } = chips(md);
  assert.deepStrictEqual(buttons.map((b) => b.textContent), ["Konto [a4]", "Konto [b7]", "Wszystkie"]);
  assert.deepStrictEqual(buttons.map(press), ["a4", "b7", "all"]);
});

check("chip lines with nested labels merged into one paragraph still match independently", () => {
  const md = "Które konto?\n[OPERACYJNE [a4]](aish-reply://a4)\n[OSZCZĘDNOŚCIOWE [b7]](aish-reply://b7)";
  const { buttons } = chips(md);
  assert.deepStrictEqual(buttons.map((b) => b.textContent), ["OPERACYJNE [a4]", "OSZCZĘDNOŚCIOWE [b7]"]);
  assert.deepStrictEqual(buttons.map(press), ["a4", "b7"]);
});

check("a stray '[' before a chip does not get swallowed into its label", () => {
  // Before #373 the label class admitted a bare "[" and this read as one chip
  // labelled "ref [Yes". A bare "[" is now only the start of a closed inner
  // pair — the choice that keeps the regex unambiguous — so the stray text
  // stays prose and the chip is the one the model actually wrote.
  const { frag, buttons } = chips("See [ref [Yes](aish-reply://yes)");
  assert.strictEqual(buttons.length, 1);
  assert.strictEqual(buttons[0].textContent, "Yes");
  assert.strictEqual(press(buttons[0]), "yes");
  assert(frag.textContent.startsWith("See [ref "), "the stray text is kept as text");
});

check("an ordinary http link with a nested bracket is unaffected: no button, text kept", () => {
  const md = "[Docs [v2]](https://example.com/docs)";
  const { frag, buttons } = chips(md);
  assert.strictEqual(buttons.length, 0, "an http link must never become a quick-reply button");
  assert(frag.textContent.includes("[Docs [v2]]"), "the source text survives as text");
});

check("a doubly nested label is not read as a chip (bounded on purpose, stays text)", () => {
  const { frag, buttons } = chips("[A [B [C]]](aish-reply://deep)");
  assert.strictEqual(buttons.length, 0);
  assert(frag.textContent.includes("[A [B [C]]]"), "nothing is destroyed — the line is kept literally");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
