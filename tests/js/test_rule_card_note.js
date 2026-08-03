// The compiled-meaning note on a write approval card (#205).
//
// A rule's diff is YAML the owner did not write; what he is agreeing to is the
// behaviour it describes. So the harness sends a `note` and the card must show
// it ABOVE the diff — a note rendered below, or not at all, means the owner
// approves the YAML and nothing else.
//
// Runs the REAL buildWriteCard out of app.js by marker, against a minimal fake
// DOM. Run manually: node tests/js/test_rule_card_note.js
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
    _class: "",
    dataset: {},
    style: {},
    get className() { return this._class; },
    set className(v) { this._class = String(v); },
    classList: {
      add(...names) { el._class = (el._class + " " + names.join(" ")).trim(); },
      contains(name) { return el._class.split(/\s+/).includes(name); },
    },
    textContent: "",
    title: "",
    type: "",
    append(...kids) { el.children.push(...kids); },
    appendChild(kid) { el.children.push(kid); return kid; },
    querySelectorAll() { return []; },
    addEventListener() {},
    setAttribute() {},
  };
  return el;
}

const sandbox = {
  document: { createElement: makeEl, createTextNode: (t) => ({ textContent: t }) },
  // The card builder's collaborators, stubbed to identifiable nodes so the
  // ORDER of what it appends is observable.
  pencilIcon: () => makeEl("svg"),
  relTarget: (t) => t,
  renderDiff: (text) => { const d = makeEl("pre"); d.className = "diff"; d.textContent = text; return d; },
  feedbackField: () => { const f = makeEl("div"); f.className = "feedback"; return f; },
  FINE_POINTER: false,
};
vm.createContext(sandbox);
vm.runInContext(
  extract("function buildWriteCard", "function buildImportCard").replace(/\bconst\b/g, "var"),
  sandbox
);
assert(sandbox.buildWriteCard, "failed to extract buildWriteCard from app.js");

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}`); console.error(`       ${err.message}`); }
}

function build(event) {
  const card = makeEl("div");
  sandbox.buildWriteCard(card, event);
  return card;
}

const NOTE = "When your message carries material: answer from the material I gave you.";

check("the note is rendered when the harness sends one", () => {
  const card = build({ verb: "create", target: "rules/x.md", diff: "+x", note: NOTE });
  const note = card.children.find((c) => c._class === "card-note");
  assert(note, "no .card-note in the card");
  assert.strictEqual(note.textContent, NOTE);
});

check("the note comes BEFORE the diff", () => {
  const card = build({ verb: "create", target: "rules/x.md", diff: "+x", note: NOTE });
  const noteAt = card.children.findIndex((c) => c._class === "card-note");
  const diffAt = card.children.findIndex((c) => c._class === "diff");
  assert(noteAt !== -1 && diffAt !== -1, "expected both a note and a diff");
  assert(noteAt < diffAt, `note at ${noteAt} must precede diff at ${diffAt}`);
});

check("an ordinary file write is unchanged — no empty note box", () => {
  const card = build({ verb: "edit", target: "a.py", diff: "+1", added: 1, removed: 0 });
  assert(!card.children.some((c) => c._class === "card-note"), "a bare write grew a note");
  assert(card.children.some((c) => c._class === "diff"), "the diff went missing");
});

check("a rule card is not called a file, and names the rule", () => {
  // The owner read "Create file" + a YAML diff and concluded aish had
  // bypassed its own rule tools to hand-write the file.
  const card = build({
    verb: "create", target: "/Users/x/.config/aish/rules/no-hedging.md",
    diff: "+x", note: NOTE, rule: "no-hedging", rule_verb: "Created",
  });
  const head = card.children[0];
  const texts = head.children.flatMap((c) => (c.children || []).map((k) => k.textContent));
  assert(texts.includes("New rule"), `header said: ${texts.join(" | ")}`);
  assert(texts.includes("no-hedging"), "the rule name is not on the card");
  assert(!texts.some((t) => /file/i.test(t || "")), "the card still calls it a file");
});

check("a rule card folds the file away instead of leading with it", () => {
  const card = build({
    verb: "create", target: "rules/x.md", diff: "+x", note: NOTE,
    rule: "x", rule_verb: "Created",
  });
  const noteAt = card.children.findIndex((c) => c._class === "card-note");
  const fold = card.children.find((c) => c._class === "card-fold");
  assert(fold, "no fold — the YAML is still the first thing shown");
  assert(card.children.indexOf(fold) > noteAt, "the file precedes the meaning");
  assert(!card.children.some((c) => c._class === "diff"), "the diff is not folded");
  // Still reachable: the file is the truth, it is just the second thing.
  assert(fold.children.some((c) => c._class === "diff"), "the diff went missing");
});

check("a rule card drops the line counts", () => {
  // Nobody decides about a rule by how many lines it is.
  const card = build({
    verb: "create", target: "rules/x.md", diff: "+x", added: 9, removed: 0,
    note: NOTE, rule: "x", rule_verb: "Created",
  });
  const head = card.children[0];
  assert(!head.children.some((c) => /card-count/.test(c._class || "")),
    "line counts are still on a rule card");
});

check("an ordinary write keeps its line counts and its plain diff", () => {
  const card = build({ verb: "edit", target: "a.py", diff: "+1", added: 1, removed: 0 });
  const head = card.children[0];
  assert(head.children.some((c) => /card-count/.test(c._class || "")), "lost the counts");
  assert(card.children.some((c) => c._class === "diff"), "the diff got folded away");
});

check("the note is set as TEXT, never as markup", () => {
  // A rule description is the owner's own words and reaches this unescaped.
  const card = build({
    verb: "create", target: "rules/x.md", diff: "+x",
    note: "<img src=x onerror=alert(1)>",
  });
  const note = card.children.find((c) => c._class === "card-note");
  assert(note && note.textContent === "<img src=x onerror=alert(1)>");
  assert(!("innerHTML" in note), "the builder used innerHTML for the note");
});

process.exit(failures === 0 ? 0 : 1);
