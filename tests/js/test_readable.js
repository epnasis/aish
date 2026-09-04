// The readable layer (#354): the REAL [READABLE] block out of app.js, its pure
// pieces functions exercised for the guarantees that keep it a lens and not a
// rewrite — lossless, safe schemes only, no medium change, no prose-as-code —
// and its one DOM function checked to mint only span/a/mark, never an image or
// any interactive control from content.
//
// Run manually: node tests/js/test_readable.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface } = require("./harness");

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}\n       ${err.stack || err.message}`); }
}

// A fake DOM just rich enough for rdRenderInto: element with children, class,
// href/target/rel, textContent that reads back its children.
function fakeEl(tag) {
  const el = { tagName: String(tag).toLowerCase(), className: "", href: "", target: "", rel: "",
    children: [], attrs: {} };
  let text = "";
  Object.defineProperty(el, "textContent", {
    get() { return text || el.children.map((c) => c.textContent).join(""); },
    set(v) { text = String(v); el.children = []; },
  });
  el.appendChild = (n) => { el.children.push(n); return n; };
  el.setAttribute = (k, v) => { el.attrs[k] = v; };
  return el;
}
function textNode(t) { return { tagName: "#text", textContent: String(t), children: [] }; }

const sandbox = {
  document: { createElement: fakeEl, createTextNode: textNode },
  console, JSON, Math, String, Object, Array, Number, RegExp, Infinity,
};
vm.createContext(sandbox);
vm.runInContext(surface(extract(appSource(), "// [READABLE-START]", "// [READABLE-END]")), sandbox);
const { rdPieces, rdDisplay, rdJson, rdMarkdown, rdYaml, rdSafeHref, rdRenderInto } = sandbox;

function walk(node, out = []) { out.push(node); for (const c of node.children || []) walk(c, out); return out; }

check("lossless languages reproduce the input byte for byte", () => {
  const md = "# Title\n\nSome **bold** and `code`, a [link](https://ex.com/a) and ![i](https://ex.com/i.png).\n- one *x*\nbare https://ex.com/x end\n";
  assert.equal(rdDisplay(rdPieces(md, "markdown")), md);
  const yaml = "# c\nkey: value\nlist:\n  - a\nurl: https://ex.com\n";
  assert.equal(rdDisplay(rdPieces(yaml, "yaml")), yaml);
  const plain = "text https://ex.com/z and /tmp/x\nline two\n";
  assert.equal(rdDisplay(rdPieces(plain, "plain")), plain);
  const broken = '{"a": 1, "b": "unterminated';
  assert.equal(rdDisplay(rdJson(broken)), broken, "malformed json is lossless");
});

check("valid json pretty-prints, keeps every value, decodes string newlines", () => {
  const raw = '{"name":"read_url","note":"l1\\nl2","n":42,"ok":true,"z":null,"a":[1,"two"]}';
  const disp = rdDisplay(rdJson(raw));
  assert(disp.includes("\n"), "reflowed");
  for (const v of ["read_url", "l1", "l2", "42", "true", "null", "two"]) assert(disp.includes(v), v);
  assert(disp.includes("l1\nl2"), "escaped \\n became a real newline in the string");
});

check("only http/https/mailto are linkified; dangerous schemes are text, not links", () => {
  assert(rdSafeHref("https://x") && rdSafeHref("http://x") && rdSafeHref("mailto:a@b"));
  for (const bad of ["javascript:alert(1)", "data:text/html,x", "aish-reply://send"]) assert.equal(rdSafeHref(bad), null);
  const pieces = rdMarkdown("[a](javascript:alert(1)) and [b](https://ok.com)");
  const links = pieces.filter((p) => p.href);
  assert.equal(links.length, 1);
  assert.equal(links[0].href, "https://ok.com");
  assert(rdDisplay(pieces).includes("javascript:alert(1)"), "the dangerous url text is kept");
});

check("an image reference is a link, never embedded", () => {
  const pieces = rdMarkdown("![alt](https://ex.com/p.png)");
  const link = pieces.find((p) => p.href);
  assert(link && link.href === "https://ex.com/p.png");
  for (const p of pieces) assert(!("src" in p) && !("img" in p));
});

check("a json fence inside markdown is highlighted as json; prose stays", () => {
  const md = 'before\n```json\n{"a": 1}\n```\nafter';
  const pieces = rdMarkdown(md);
  assert(pieces.some((p) => p.cls === "tok-key") && pieces.some((p) => p.cls === "tok-num"));
  assert(rdDisplay(pieces).includes("before") && rdDisplay(pieces).includes("after"));
});

check("plain prose is not coerced into code structure", () => {
  const prose = "The quick brown fox: jumps over 42 lazy dogs.";
  const pieces = rdPieces(prose, "plain");
  assert.equal(rdDisplay(pieces), prose);
  assert(!pieces.some((p) => p.cls === "tok-key"));
});

check("auto detects json, else stays plain", () => {
  assert(rdPieces('{"a":1}', "auto").some((p) => p.cls === "tok-key"), "object detected as json");
  const prose = rdPieces("not json at all", "auto");
  assert.equal(rdDisplay(prose), "not json at all");
  assert(!prose.some((p) => p.cls === "tok-key"));
});

check("rdRenderInto mints only span/a/mark, never an image or a control", () => {
  const pre = fakeEl("pre");
  // hostile content: a script tag, an aish-reply link, an event handler.
  const nasty = '{"x": "<img src=x onerror=alert(1)>", "y": "[go](aish-reply://send)", "u": "https://ok.com"}';
  rdRenderInto(pre, rdJson(nasty), "", []);
  const nodes = walk(pre);
  for (const n of nodes) {
    assert(["pre", "span", "a", "mark", "#text"].includes(n.tagName), `unexpected node <${n.tagName}>`);
    if (n.tagName === "a") assert(/^https?:|^mailto:/.test(n.href), "a link only ever has a safe href");
  }
  // the hostile strings survive as TEXT, verbatim.
  assert(pre.textContent.includes("<img src=x onerror=alert(1)>"));
  assert(pre.textContent.includes("aish-reply://send"), "aish-reply text is shown, never a link");
  // and the one safe url did become a link.
  assert(nodes.some((n) => n.tagName === "a" && n.href === "https://ok.com"));
});

check("find marks compose with highlighting and stay within the cap", () => {
  const pre = fakeEl("pre");
  const marks = [];
  rdRenderInto(pre, rdJson('{"k":"needle and needle again"}'), "needle", marks, 5);
  assert.equal(marks.length, 2, "both hits marked");
  assert(pre.textContent.includes("needle and needle again"), "text intact around the marks");
});

if (failures) { console.error(`${failures} failed`); process.exit(1); }
console.log("readable: all checks passed");
