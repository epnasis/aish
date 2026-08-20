// Node-only, dependency-free check for a link to a file ON THIS MACHINE (#237).
// Pulls the REAL INLINE_RE, fileChip, attachmentChip and kindOfFile out of
// app.js by marker and runs them against a minimal fake DOM.
//
// The gap being closed: aish drives the owner's signed-in portal, clicks
// "Pobierz e-fakturę", and the invoice lands in its downloads folder — and the
// answer could only say WHERE it was. `/download` would have served it the
// whole time; nothing in an answer could ask.
//
// What it pins:
//   1. `[faktura.pdf](/abs/path.pdf)` is recognised at all — INLINE_RE's plain
//      link branch is http-only, so before this it matched nothing and rendered
//      as literal text
//   2. a SPACE in the path survives: "faktura 09-2026.pdf" is what a real
//      invoice is called, and [^)\s] would have thrown it away
//   3. a PDF's chip opens onto its pages; anything else SAVES — one answer to
//      what a file is, shared with the attachment strip
//   4. a picture written as an ordinary link is still a picture
//   5. a site-relative link the model wrote by hand (`[docs](/help)`) is NOT
//      turned into a chip for a file that does not exist
//   6. an http link is still an http link
//
// Run manually: node tests/js/test_file_link_chip.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"),
  "utf8"
);

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

function fakeElement(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    className: "",
    children: [],
    attrs: {},
    textContent: "",
    title: "",
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    classList: {
      add(...c) { el._classes.push(...c); },
      contains(c) { return el._classes.includes(c); },
    },
    _classes: [],
  };
  return el;
}

const opened = [];
const saved = [];
const box = {
  document: { createElement: fakeElement, createTextNode: (t) => ({ text: t }) },
  URLSearchParams,
  token: "tok",
  replaying: false,
  offlineViewing: false,
  noteRenderError() {},
  openPdfPreview(p, name) { opened.push([p, name]); },
  saveAttachment(p, name) { saved.push([p, name]); },
  openPreview() {},
  shortName: (n) => n,
};
vm.createContext(box);
vm.runInContext(
  [
    extract("const INLINE_RE = new RegExp(", "// Every external http(s) link"),
    extract("// The src a markdown image target", "// Quick replies (#17)"),
    extract("// [FILE-LINK-START]", "// [FILE-LINK-END]"),
    extract("function attachmentChip(note)", "function addUserMsg"),
    extract("function kindOfFile(name)", "// A reference with no directory"),
    "const ATTACH_IMAGE_RE = /\\.(png|jpe?g|gif|webp)$/i;",
    "function isPdfPath(p) { return /\\.pdf$/i.test(p); }",
  ].join("\n").replace(/\bconst\b/g, "var"),
  box
);

const INVOICE = "/Users/x/.local/state/aish/browser/downloads/faktura 09-2026.pdf";

// The dispatch `inlineMd` does, reduced to the branch under test.
function render(source) {
  const m = source.match(box.INLINE_RE);
  if (!m) return null;
  if (m[12] === undefined) return { kind: "not-a-file", url: m[6] };
  const node = box.fileChip(m[11], m[12]);
  return node ? { kind: "file", node } : { kind: "text" };
}

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (err) {
    console.log(`FAIL ${name}\n     ${err.message}`);
    failures++;
  }
}

check("a link to a local file is recognised at all", () => {
  const out = render(`[faktura.pdf](/Users/x/faktura.pdf)`);
  assert.strictEqual(out.kind, "file");
});

check("a space in the file name survives", () => {
  const out = render(`[faktura 09-2026.pdf](${INVOICE})`);
  assert.strictEqual(out.kind, "file");
  assert.strictEqual(out.node.textContent, "faktura 09-2026.pdf");
});

check("a file:// link the model invented renders as the file", () => {
  // Seven of these in one real answer, every one inert. A chat log is never
  // rewritten, so the old answer has to render as the files it named.
  const out = render(`[dokument.pdf](file://${INVOICE})`);
  assert.strictEqual(out.kind, "file");
  assert.strictEqual(out.node.textContent, "faktura 09-2026.pdf");
});

check("a pdf opens onto its pages", () => {
  opened.length = 0;
  const out = render(`[faktura 09-2026.pdf](${INVOICE})`);
  out.node.onclick();
  assert.deepStrictEqual(opened, [[INVOICE, "faktura 09-2026.pdf"]]);
});

check("anything else saves", () => {
  saved.length = 0;
  const csv = "/Users/x/.local/state/aish/browser/downloads/rozliczenie.csv";
  const out = render(`[rozliczenie.csv](${csv})`);
  out.node.onclick();
  assert.deepStrictEqual(saved, [[csv, "rozliczenie.csv"]]);
});

check("a picture written as a plain link is still a picture", () => {
  // `inlineImage` hands back the wrapper it previews from, not the bare <img>.
  const out = render(`[shot](/Users/x/shot.png)`);
  assert.strictEqual(out.kind, "file");
  assert.strictEqual(out.node.className, "img-link");
  const img = out.node.children[0];
  assert.strictEqual(img.tagName, "IMG");
  assert.ok(img.src.startsWith("/file?"), img.src);
});

check("a site-relative link is not a file", () => {
  const out = render(`[the docs](/help)`);
  assert.strictEqual(out.kind, "text");
});

check("an http link is still an http link", () => {
  const out = render(`[E.ON](https://eon.pl/mojeon)`);
  assert.strictEqual(out.kind, "not-a-file");
  assert.strictEqual(out.url, "https://eon.pl/mojeon");
});

check("an image markdown line is still an image, not a chip", () => {
  const m = `![a cat](/Users/x/cat.png)`.match(box.INLINE_RE);
  assert.strictEqual(m[10], "/Users/x/cat.png");
  assert.strictEqual(m[12], undefined);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
