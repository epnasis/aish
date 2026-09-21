// Shared sandbox for the maths checks (test_math_render.js,
// test_speakable_math.js): the REAL renderMarkdown / inlineMd / findMath /
// mathNode / speakableText pulled out of app.js by marker, the REAL vendored
// katex.min.js loaded beside them, and a fake DOM rich enough for KaTeX's own
// DOM builder (namespaced elements, a style bag, className assignment).
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(path.join(root, "aish", "static", "app.js"), "utf8");
const katexSrc = fs.readFileSync(path.join(root, "aish", "static", "vendor", "katex.min.js"), "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

const TEXT_NODE = 3;
const ELEMENT_NODE = 1;

function makeElement(tag) {
  const node = {
    tagName: tag ? tag.toUpperCase() : tag,
    nodeType: tag ? ELEMENT_NODE : 11,
    childNodes: [],
    dataset: {},
    style: {},
    _attrs: {},
    _text: "",
    _className: "",
    // Appending a fragment moves its children, as in a browser.
    appendChild(child) {
      if (child.nodeType === 11) { this.childNodes.push(...child.childNodes); child.childNodes = []; }
      else this.childNodes.push(child);
      return child;
    },
    append(...kids) { for (const k of kids) this.appendChild(k); },
    setAttribute(k, v) { this._attrs[k] = String(v); if (k === "class") this.className = String(v); },
    getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(node, "children", { get() { return this.childNodes; } });
  const cls = new Set();
  node.classList = {
    add: (c) => { cls.add(c); node._className = [...cls].join(" "); },
    remove: (c) => { cls.delete(c); node._className = [...cls].join(" "); },
    contains: (c) => cls.has(c),
    toggle: (c) => { cls.has(c) ? cls.delete(c) : cls.add(c); node._className = [...cls].join(" "); },
  };
  Object.defineProperty(node, "className", {
    get() { return this._className; },
    set(v) {
      this._className = String(v);
      cls.clear();
      for (const c of String(v).split(/\s+/)) if (c) cls.add(c);
    },
  });
  // As in a browser: assigning textContent replaces every child with ONE text
  // node (or none for ""), so a walker over childNodes sees what was assigned.
  Object.defineProperty(node, "textContent", {
    get() { return this.childNodes.map((c) => c.textContent || "").join(""); },
    set(v) {
      this.childNodes = [];
      if (String(v) !== "") this.childNodes.push(makeText(v));
    },
  });
  return node;
}

function makeText(t) {
  return {
    nodeType: TEXT_NODE, nodeValue: String(t), childNodes: [],
    get textContent() { return this.nodeValue; },
  };
}

const documentFake = {
  // KaTeX refuses to render in quirks mode (it checks compatMode at load).
  compatMode: "CSS1Compat",
  createElement: (tag) => makeElement(tag),
  createElementNS: (ns, tag) => makeElement(tag),
  createDocumentFragment: () => makeElement(null),
  createTextNode: makeText,
};

function buildSandbox() {
  const sandbox = {
    document: documentFake,
    Node: { TEXT_NODE, ELEMENT_NODE },
    WRAP_SVG: "",
    copyChip: () => makeElement("button"),
    token: "",
    replaying: false,
    offlineViewing: false,
    console,
  };
  vm.createContext(sandbox);
  // The UMD wrapper falls through to `this.katex = factory()` when neither
  // CommonJS nor AMD is present — `this` is the sandbox.
  vm.runInContext(katexSrc, sandbox);
  assert(typeof sandbox.katex === "object" && typeof sandbox.katex.render === "function",
    "vendored katex.min.js did not define katex.render in the sandbox");
  sandbox.window = { katex: sandbox.katex };

  const snippet = (
    extract("const FENCE_RE", "function stableBoundary") + "\n" +
    extract("function renderMarkdown", "// ---- read aloud") + "\n" +
    extract("function speakableText", "function chunkParagraphs") + "\n" +
    "this.renderMarkdown = renderMarkdown; this.inlineMd = inlineMd;" +
    "this.findMath = findMath; this.speakableText = speakableText;"
  ).replace(/\bconst\b/g, "var");
  vm.runInContext(snippet, sandbox);
  assert(typeof sandbox.renderMarkdown === "function" && typeof sandbox.findMath === "function"
    && typeof sandbox.speakableText === "function", "failed to extract the renderer from app.js");
  return sandbox;
}

function collectClass(node, cls, out = []) {
  for (const child of node.childNodes || []) {
    if (child.classList && child.classList.contains(cls)) out.push(child);
    collectClass(child, cls, out);
  }
  return out;
}

function collectTag(node, tag, out = []) {
  for (const child of node.childNodes || []) {
    if (child.tagName === tag) out.push(child);
    collectTag(child, tag, out);
  }
  return out;
}

// The words a reader would see: text nodes, minus KaTeX's hidden MathML mirror
// (which carries the TeX source as an annotation) — a browser hides it by CSS.
function visibleText(node, out = []) {
  for (const child of node.childNodes || []) {
    if (child.nodeType === TEXT_NODE) { out.push(child.nodeValue); continue; }
    if (child.classList && child.classList.contains("katex-mathml")) continue;
    visibleText(child, out);
  }
  return out.join("");
}

function checker() {
  let failures = 0;
  const check = (name, fn) => {
    try {
      fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      failures++;
      console.error(`FAIL - ${name}`);
      console.error(`       ${err.message}`);
    }
  };
  const done = (label) => {
    if (failures) {
      console.error(`\n${failures} check(s) failed`);
      process.exit(1);
    }
    console.log(`\nall ${label} checks passed`);
  };
  return { check, done };
}

module.exports = { buildSandbox, makeElement, collectClass, collectTag, visibleText, checker, root };
