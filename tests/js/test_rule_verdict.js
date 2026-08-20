// Node-only, dependency-free check that aish's verdict on an answer does not
// render as part of the answer (#250).
//
// When a rule fails, the harness goads the model a bounded number of times and
// then delivers the answer anyway rather than wedging the turn, carrying a line
// it writes itself: "[aish] rule 'links-you-actually-opened' not followed: …".
// That line is the point — a rule that was tried and failed has to be visible to
// the OWNER, not only to automation.
//
// It arrived in the same font, colour and paragraph flow as the answer, so it
// read as though the model had said it: an accusation in the voice of the
// accused. Everything else aish says in its own voice is marked and rendered as
// a system row; this one spells the marker `[aish]` and matched nothing.
//
// Runs the REAL renderMarkdownBlocks out of app.js by marker.
// Run manually: node tests/js/test_rule_verdict.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
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
    dataset: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    append(...c) { this.children.push(...c); },
    appendChild(c) { this.children.push(c); return c; },
    classList: { add(...c) { el._classes.push(...c); },
                 toggle() {}, contains(c) { return el._classes.includes(c); } },
    _classes: [],
  };
  return el;
}

const box = {
  document: {
    createElement: fakeElement,
    createTextNode: (t) => ({ text: t, nodeType: 3 }),
    createDocumentFragment: () => fakeElement("fragment"),
  },
  URLSearchParams,
  token: "tok",
  replaying: false,
  offlineViewing: false,
  noteRenderError() {},
  openPreview() {},
  openPdfPreview() {},
  saveAttachment() {},
  shortName: (n) => n,
  cardScope: null,
};
vm.createContext(box);
vm.runInContext(
  [
    // ONE range: the whole markdown renderer, which already carries INLINE_RE,
    // the image helpers, the quick replies and the file chip. Taking them
    // separately as well would declare each of them twice.
    extract("const FENCE_RE = ", "function stableBoundary"),
    extract("function renderMarkdown(text, scope)", "// ---- read aloud"),
    extract("function attachmentChip(note)", "function addUserMsg"),
    extract("function kindOfFile(name)", "// A reference with no directory"),
    "const ATTACH_IMAGE_RE = /\\.(png|jpe?g|gif|webp)$/i;",
    "function isPdfPath(p) { return /\\.pdf$/i.test(p); }",
    "function issueDraftCard() { return null; }",
    "function copyChip() { return null; }",
    "function claimVideoCard() { return false; }",
  ].join("\n").replace(/\bconst\b/g, "var"),
  box
);

const VERDICT =
  "[aish] rule 'links-you-actually-opened' not followed: The rule " +
  "'links-you-actually-opened' does not allow a link you have not opened.";

function blocks(text) {
  return box.renderMarkdownBlocks(text).children;
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

check("the verdict is its own row, not a paragraph", () => {
  const out = blocks(`Here are your invoices.\n\n${VERDICT}`);
  assert.strictEqual(out.length, 2);
  assert.strictEqual(out[0].tagName, "P");
  assert.strictEqual(out[1].tagName, "DIV");
  assert.strictEqual(out[1].className, "rule-verdict");
});

check("the verdict is not swallowed into the paragraph above it", () => {
  // No blank line: the harness appends it to whatever the answer ended with.
  const out = blocks(`Here are your invoices.\n${VERDICT}`);
  assert.strictEqual(out.length, 2, `got ${out.length} block(s)`);
  assert.strictEqual(out[1].className, "rule-verdict");
});

check("the answer's own words are still the answer", () => {
  const out = blocks(`Here are your invoices.\n${VERDICT}`);
  assert.strictEqual(out[0].tagName, "P");
});

check("a paragraph that merely mentions aish is untouched", () => {
  const out = blocks("I asked aish [aish] is the name of the tool.");
  assert.strictEqual(out.length, 1);
  assert.strictEqual(out[0].tagName, "P");
});

check("two verdicts render as two rows", () => {
  const out = blocks(`${VERDICT}\n[aish] rule 'clickable-links' not followed: x`);
  assert.strictEqual(out.length, 2);
  assert.ok(out.every((b) => b.className === "rule-verdict"));
});

check("the verdict's own text is rendered, not the marker", () => {
  const out = blocks(VERDICT);
  const rendered = JSON.stringify(out[0].children);
  assert.ok(!rendered.includes("[aish]"), rendered.slice(0, 200));
  assert.ok(rendered.includes("links-you-actually-opened"), rendered.slice(0, 200));
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
