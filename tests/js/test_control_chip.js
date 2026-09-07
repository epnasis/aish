// Node-only, dependency-free check for the inline control chip (#364).
//
// A browsed page renders its controls in place as `[label](press:cN·nonce)`.
// The owner's view turns those into READ-ONLY chips — the model presses them,
// so they must never become an anchor (a link to `press:cN·nonce` navigates
// nowhere and leaks the machinery). The REAL renderMarkdown/inlineMd are
// pulled from app.js and run in a vm against a minimal fake DOM.
//
// Run manually: node tests/js/test_control_chip.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8");
function extract(a, b) {
  const s = src.indexOf(a), e = src.indexOf(b, s);
  assert(s !== -1 && e !== -1, `markers not found: ${a} .. ${b}`);
  return src.slice(s, e);
}
function makeElement(tag) {
  const node = {
    tagName: tag ? tag.toUpperCase() : tag,
    nodeType: tag ? 1 : 11,
    children: [], dataset: {}, _attrs: {}, _text: "",
    appendChild(c) { this.children.push(c); return c; },
    append(...k) { for (const x of k) this.children.push(x); },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; },
  };
  const cls = new Set();
  node.classList = {
    add: (c) => { cls.add(c); node.className = [...cls].join(" "); },
    remove: (c) => { cls.delete(c); node.className = [...cls].join(" "); },
    contains: (c) => cls.has(c), toggle: () => {},
  };
  Object.defineProperty(node, "textContent", {
    get() { return this.children.length
      ? this.children.map((c) => c.textContent || "").join("") : this._text; },
    set(v) { this._text = String(v); this.children = []; },
  });
  return node;
}
const documentFake = {
  createElement: (t) => makeElement(t),
  createDocumentFragment: () => makeElement(null),
  createTextNode: (t) => ({ nodeType: 3, _text: String(t), children: [],
    get textContent() { return this._text; } }),
};
const sandbox = { document: documentFake, WRAP_SVG: "", copyChip: () => makeElement("button"), token: "", console };
vm.createContext(sandbox);
const snippet = (
  extract("const FENCE_RE", "function stableBoundary") + "\n" +
  extract("function renderMarkdown", "// ---- read aloud") + "\n" +
  "this.renderMarkdown = renderMarkdown;"
).replace(/\bconst\b/g, "var");
vm.runInContext(snippet, sandbox);
const { renderMarkdown } = sandbox;

function collect(node, tag, out = []) {
  for (const c of node.children || []) {
    if (c.tagName === tag) out.push(c);
    collect(c, tag, out);
  }
  return out;
}
let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (e) { failures++; console.error(`FAIL - ${name}\n       ${e.message}`); }
}

check("an inline control renders as a read-only chip, not an anchor", () => {
  const out = renderMarkdown(
    "Prognoza 249000930656 | 388,25 | [Pobierz e-fakturę](press:c0·ab12)");
  const chips = collect(out, "SPAN").filter((s) => s.className === "ctrl-chip");
  assert(chips.length === 1, "expected one ctrl-chip");
  assert(chips[0].textContent === "Pobierz e-fakturę", "chip shows the label");
  const anchors = collect(out, "A");
  assert(anchors.length === 0, "a control must never become an anchor");
  // the reference/nonce is machinery and never shown
  assert(!out.textContent.includes("press:c0"), "the reference leaked into the view");
});

check("a nav destination after a control stays plain context", () => {
  const out = renderMarkdown("[Wyświetl profil](press:c9·ab12) → linkedin.com/in/x");
  const chips = collect(out, "SPAN").filter((s) => s.className === "ctrl-chip");
  assert(chips.length === 1 && chips[0].textContent === "Wyświetl profil");
  assert(out.textContent.includes("→ linkedin.com/in/x"), "destination context kept");
  assert(collect(out, "A").length === 0, "the destination must not be a live link");
});

check("a real http link still becomes a target=_blank anchor", () => {
  const out = renderMarkdown("see [the site](https://example.com/x)");
  const anchors = collect(out, "A");
  assert(anchors.length === 1 && anchors[0].target === "_blank",
    "an ordinary link must still open in the system browser");
});

if (failures) { console.error(`\n${failures} failure(s)`); process.exit(1); }
console.log("\nall control-chip checks passed");
