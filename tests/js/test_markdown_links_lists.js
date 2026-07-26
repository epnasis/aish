// Node-only, dependency-free checks for two renderMarkdown fixes:
//
//   #165 — every external http(s) link in the transcript opens in the system
//          browser: its anchor carries target="_blank" + rel="noopener
//          noreferrer". The in-app aish-reply:// scheme stays a <button> and is
//          never turned into a target=_blank anchor.
//   #166 — the list parser absorbs an item's continuation (blank lines + lines
//          indented under the marker) instead of terminating the list, so a
//          nested paragraph doesn't restart numbering; and an ordered list that
//          doesn't begin at 1 gets a start attribute. A flat list is unchanged.
//   #172 — block markers accept CommonMark's 0-3 leading spaces. The list
//          parser strips exactly the item's content column, so a fence/quote/
//          heading the model indented by 4 under a `2. ` marker still carries a
//          residual space when it reaches the block parser; demanding column 0
//          made those nested blocks leak as literal markdown text.
//
// The REAL renderMarkdown / inlineMd (plus the fence helpers they call) are
// pulled out of app.js by marker and evaluated in a vm against a minimal fake
// DOM — the shipped code is exercised, never a hand-copied duplicate.
//
// Run manually: node tests/js/test_markdown_links_lists.js
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
// Only what renderMarkdown / inlineMd touch: element + text + fragment nodes,
// appendChild/append, setAttribute/getAttribute, dataset, classList, and the
// handful of direct properties the code assigns (href/target/rel/…).
function makeElement(tag) {
  const node = {
    tagName: tag ? tag.toUpperCase() : tag,
    nodeType: tag ? 1 : 11, // 1 = element, 11 = fragment (createElement always tags)
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

const sandbox = {
  document: documentFake,
  // Outside the extracted slice — stubbed so the fenced-code-block branch runs.
  WRAP_SVG: "",
  copyChip: () => makeElement("button"),
  token: "",
  console,
};
vm.createContext(sandbox);

const snippet = (
  extract("const FENCE_RE", "function stableBoundary") + "\n" +
  extract("function renderMarkdown", "// ---- read aloud") + "\n" +
  "this.renderMarkdown = renderMarkdown; this.inlineMd = inlineMd;"
).replace(/\bconst\b/g, "var");
vm.runInContext(snippet, sandbox);
const { renderMarkdown, inlineMd } = sandbox;
assert(typeof renderMarkdown === "function" && typeof inlineMd === "function",
  "failed to extract renderMarkdown/inlineMd from app.js");

// Walk a node tree (fragments included) collecting elements by tag name.
function collect(node, tag, out = []) {
  for (const child of node.children || []) {
    if (child.tagName === tag) out.push(child);
    collect(child, tag, out);
  }
  return out;
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

// ---- #165: external links -------------------------------------------------

check("an external http(s) link opens externally (target/rel set)", () => {
  const frag = renderMarkdown("See [the docs](https://example.com/page) now.");
  const anchors = collect(frag, "A");
  assert.strictEqual(anchors.length, 1, "expected exactly one anchor");
  const a = anchors[0];
  assert.strictEqual(a.href, "https://example.com/page", "href preserved");
  assert.strictEqual(a.target, "_blank", "target=_blank hands the URL to the OS");
  assert.strictEqual(a.rel, "noopener noreferrer", "rel severs opener/referrer");
});

check("an aish-reply:// quick reply stays a button, never a target=_blank anchor", () => {
  const frag = renderMarkdown("[Yes please](aish-reply://yes)");
  const anchors = collect(frag, "A");
  assert.strictEqual(anchors.length, 0, "quick reply must not render as an anchor");
  const buttons = collect(frag, "BUTTON");
  assert.strictEqual(buttons.length, 1, "expected one quick-reply button");
  const btn = buttons[0];
  assert(btn.className.includes("quick-reply"), "button carries the quick-reply class");
  assert.strictEqual(btn.target, undefined, "the in-app scheme never gets target=_blank");
});

// ---- #166: ordered-list numbering -----------------------------------------

check("a numbered list with a nested paragraph keeps sequential numbering", () => {
  const md = [
    "1. First item",
    "2. Second item",
    "",
    "   A nested paragraph under the second item.",
    "3. Third item",
  ].join("\n");
  const frag = renderMarkdown(md);
  const ols = collect(frag, "OL");
  assert.strictEqual(ols.length, 1, "the nested paragraph must not split the list into two <ol>s");
  const items = ols[0].children.filter((c) => c.tagName === "LI");
  assert.strictEqual(items.length, 3, "all three items stay in the one list");
  assert.strictEqual(ols[0].getAttribute("start"), null, "a list starting at 1 needs no start attr");
  // The second item's continuation is rendered as nested block content (a <p>),
  // proving it was absorbed into the <li> rather than terminating the list.
  const paras = collect(items[1], "P");
  assert(paras.length >= 1, "the nested paragraph is rendered inside the second <li>");
});

check("a numbered list starting above 1 gets a start attribute", () => {
  const frag = renderMarkdown("2. two\n3. three");
  const ols = collect(frag, "OL");
  assert.strictEqual(ols.length, 1);
  assert.strictEqual(ols[0].getAttribute("start"), "2", "start attr carries the true first number");
  const items = ols[0].children.filter((c) => c.tagName === "LI");
  assert.strictEqual(items.length, 2);
});

check("a flat numbered list is unchanged (no start attr, inline items, no <p>)", () => {
  const frag = renderMarkdown("1. alpha\n2. beta\n3. gamma");
  const ols = collect(frag, "OL");
  assert.strictEqual(ols.length, 1, "one list");
  const items = ols[0].children.filter((c) => c.tagName === "LI");
  assert.strictEqual(items.length, 3, "three items");
  assert.strictEqual(ols[0].getAttribute("start"), null, "no start attr for a 1-based list");
  // Flat items render their text inline (like before), not wrapped in a <p>.
  assert.strictEqual(collect(frag, "P").length, 0, "flat items must not gain a <p> wrapper");
});

check("a flat bullet list still renders as a <ul> with the right item count", () => {
  const frag = renderMarkdown("- a\n- b\n- c");
  const uls = collect(frag, "UL");
  assert.strictEqual(uls.length, 1);
  const items = uls[0].children.filter((c) => c.tagName === "LI");
  assert.strictEqual(items.length, 3);
});

// ---- #172: nested blocks indented under a list item -----------------------

// The exact shape the model emits: 4-space-indented continuation under a
// `2. ` marker (content column 3), leaving one residual space on each block
// marker after the list parser strips the column.
const NESTED = [
  "1. **First item**",
  "",
  "    A nested paragraph.",
  "",
  "2. **Second item**",
  "",
  "    ```javascript",
  "    function nested() {",
  "        return true;",
  "    }",
  "    ```",
  "",
  "    > A blockquote belonging to item 2.",
  "    > Second quoted line.",
  "",
  "3. **Third item**",
].join("\n");

check("a code fence indented under a list item renders as a real code block", () => {
  const frag = renderMarkdown(NESTED);
  const codes = collect(frag, "CODE");
  assert.strictEqual(codes.length, 1, "expected one fenced code block, not leaked text");
  const code = codes[0];
  assert.strictEqual(code.dataset.lang, "javascript", "info string survives the indentation");
  // The fence's own indentation is stripped from the body, but the code's
  // internal indentation is not.
  assert.strictEqual(code.textContent,
    "function nested() {\n    return true;\n}",
    "fence indent removed from every body line, inner indent kept");
  const pres = collect(frag, "PRE");
  assert.strictEqual(pres.length, 1, "the code block is wrapped in a <pre>");
});

check("a blockquote indented under a list item renders as a real blockquote", () => {
  const frag = renderMarkdown(NESTED);
  const quotes = collect(frag, "BLOCKQUOTE");
  assert.strictEqual(quotes.length, 1, "expected one blockquote, not leaked '>' text");
  assert(quotes[0].textContent.includes("A blockquote belonging to item 2."),
    "the quote body is rendered inside the blockquote");
  assert(!quotes[0].textContent.includes(">"), "the quote marker is stripped");
});

check("nested blocks don't break the parent list's numbering", () => {
  const frag = renderMarkdown(NESTED);
  const ols = collect(frag, "OL");
  assert.strictEqual(ols.length, 1, "one <ol> — the nested blocks must not split it");
  const items = ols[0].children.filter((c) => c.tagName === "LI");
  assert.strictEqual(items.length, 3, "all three items stay in the one list");
  // The code block and quote belong to the second item, not to the document.
  assert.strictEqual(collect(items[1], "PRE").length, 1, "code block sits inside item 2");
  assert.strictEqual(collect(items[1], "BLOCKQUOTE").length, 1, "quote sits inside item 2");
});

check("a heading indented under a list item is still a heading", () => {
  const frag = renderMarkdown("1. Item\n\n   ### Nested heading\n");
  const heads = collect(frag, "H4"); // ### renders one level down (see renderMarkdown)
  assert.strictEqual(heads.length, 1, "the indented heading is not leaked as '### …' text");
  assert.strictEqual(heads[0].textContent, "Nested heading");
});

check("an unindented fence is untouched (no body dedent, no behaviour change)", () => {
  const frag = renderMarkdown("```python\ndef f():\n    return 1\n```");
  const codes = collect(frag, "CODE");
  assert.strictEqual(codes.length, 1);
  assert.strictEqual(codes[0].textContent, "def f():\n    return 1",
    "a column-0 fence keeps its body exactly as written");
  assert.strictEqual(codes[0].dataset.lang, "python");
});

check("a table indented under a list item still renders as a table", () => {
  const frag = renderMarkdown("1. Item\n\n   | a | b |\n   | - | - |\n   | 1 | 2 |\n");
  const tables = collect(frag, "TABLE");
  assert.strictEqual(tables.length, 1, "the indented table is not leaked as pipe text");
  assert.strictEqual(collect(tables[0], "TD").length, 2, "the body row's cells are parsed");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
