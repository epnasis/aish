// The update card (#467 slice 4c): the import card with what it replaces and
// one diff per changed file.
//
// Properties: the card says it is an update (title, the "replaces … (commit
// a → b)" sentence, an Update button); every changed file's diff is drawn,
// line by line, as TEXT — file names and diff lines are the publisher's, so
// markup in them must never render; and an import card (no `update`) is
// unchanged.
//
// Runs the REAL buildImportCard / importUpdateBlock / renderDiff out of app.js
// by marker, against a minimal fake DOM. Run manually:
// node tests/js/test_import_update.js
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

function makeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    className: "",
    dataset: {},
    textContent: "",
    innerHTML: "",
    classList: {
      add(...names) { el.className = (el.className + " " + names.join(" ")).trim(); },
      contains(name) { return String(el.className).split(/\s+/).includes(name); },
    },
    append(...kids) { el.children.push(...kids); },
    appendChild(kid) { el.children.push(kid); return kid; },
    querySelectorAll() { return []; },
  };
  return el;
}

const sandbox = {
  document: { createElement: makeEl, body: makeEl("body") },
  wrenchIcon: () => makeEl("svg"),
  intentBlock: () => { const d = makeEl("div"); d.className = "card-intent"; return d; },
  highlightFences: () => {},
  feedbackField: () => { const f = makeEl("div"); f.className = "feedback"; return f; },
  feedbackExtra: () => ({}),
  answerCard: () => {},
  copyChip: () => makeEl("button"),
  diffWrapBtn: () => makeEl("button"),
};
vm.createContext(sandbox);
vm.runInContext(
  (extract("function buildImportCard", "function wrenchIcon")
    + extract("function renderDiff", "function diffWrapBtn")).replace(/\bconst\b/g, "var"),
  sandbox
);
assert(sandbox.importUpdateBlock, "failed to extract importUpdateBlock from app.js");
assert(sandbox.renderDiff, "failed to extract renderDiff from app.js");

function walk(el, out = []) {
  out.push(el);
  for (const kid of el.children || []) walk(kid, out);
  return out;
}
const byClass = (el, name) => walk(el).filter((e) =>
  String(e.className).split(/\s+/).includes(name));
const texts = (el) => walk(el).map((e) => e.textContent);

const HOSTILE = "<img src=x onerror=alert(1)>";
const UPDATE = {
  replaces: "myskill",
  summary: "replaces myskill (commit aaaaaaaaaaaa → bbbbbbbbbbbb)",
  changes: "added extra.py; modified SKILL.md",
  packages: ["packages added: requests"],
  binaries: "binary files are skipped, as at import: they are not installed, compared or shown",
  diffs: [
    { path: "SKILL.md", status: "modified", note: "",
      diff: `--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1,2 +1,2 @@\n keep\n-old line\n+${HOSTILE}\n` },
    { path: `${HOSTILE}.py`, status: "added", note: "",
      diff: `--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+print(1)\n` },
    { path: "run.sh", status: "executable bit changed", note: "executable bit set", diff: "" },
  ],
};
const BASE = { skill: "myskill", description: "d", files: [], skipped: [], dest: "/v/myskill",
  flags: [] };

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}`); console.error(`       ${err.message}`); }
}

check("an update card says what it replaces and asks to Update", () => {
  const card = makeEl("div");
  sandbox.buildImportCard(card, { ...BASE, update: UPDATE });
  assert.strictEqual(byClass(card, "card-htitle")[0].textContent, "Update skill");
  assert.strictEqual(byClass(card, "import-update-title")[0].textContent, UPDATE.summary);
  assert(texts(card).includes("packages added: requests"));
  assert(texts(card).includes(UPDATE.binaries));
  assert.strictEqual(byClass(card, "approve")[0].textContent, "Update");
});

check("every changed file's diff is drawn, its lines as text", () => {
  const card = makeEl("div");
  sandbox.buildImportCard(card, { ...BASE, update: UPDATE });
  const lines = byClass(card, "dl-tx").map((e) => e.textContent);
  assert(lines.includes("old line"), lines.join("|"));
  assert(lines.includes(HOSTILE), "the added line is not drawn");
  assert(lines.includes("print(1)"), "the second file's diff is not drawn");
  const heads = byClass(card, "import-file-head").map((e) => e.textContent);
  assert(heads.includes(`added: ${HOSTILE}.py`), heads.join("|"));
  assert(heads.includes("executable bit changed: run.sh  (executable bit set)"), heads.join("|"));
});

check("no publisher text reaches innerHTML", () => {
  const card = makeEl("div");
  sandbox.buildImportCard(card, { ...BASE, update: UPDATE });
  for (const el of walk(card)) {
    assert(!String(el.innerHTML).includes("onerror"), `markup in ${el.className}`);
  }
});

check("an import card draws no update block", () => {
  const card = makeEl("div");
  sandbox.buildImportCard(card, { ...BASE });
  assert.strictEqual(byClass(card, "import-update").length, 0);
  assert.strictEqual(byClass(card, "card-htitle")[0].textContent, "Import skill");
  assert.strictEqual(byClass(card, "approve")[0].textContent, "Install");
});

if (failures) {
  console.error(`${failures} failure(s)`);
  process.exit(1);
}
