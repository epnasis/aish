// "Always for this skill" on the use_skill card (#467 slice 2c).
//
// Three properties: the button exists ONLY when the server sent an offer (the
// agent composed it from the skill's reviewed hashes — a card never invents
// one); it sends its own wire action, and it is NOT `button.approve`, so the
// A shortcut keeps meaning "this run, this time"; and the skill name is set as
// text, so a stranger's skill name cannot render markup.
//
// Runs the REAL buildToolCard / skillAlwaysNote out of app.js by marker,
// against a minimal fake DOM. Run manually: node tests/js/test_skill_always.js
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
    classList: { add(...names) { el.className = (el.className + " " + names.join(" ")).trim(); } },
    append(...kids) { el.children.push(...kids); },
    appendChild(kid) { el.children.push(kid); return kid; },
    querySelectorAll() { return []; },
  };
  return el;
}

const answers = [];
const sandbox = {
  document: { createElement: makeEl },
  wrenchIcon: () => makeEl("svg"),
  toolArgRow: () => makeEl("div"),
  intentBlock: () => { const d = makeEl("div"); d.className = "card-intent"; return d; },
  feedbackField: () => { const f = makeEl("div"); f.className = "feedback"; return f; },
  feedbackExtra: () => ({ comment: "" }),
  answerCard: (id, action, extra) => answers.push({ id, action, extra }),
};
vm.createContext(sandbox);
vm.runInContext(
  extract("function buildToolCard", "// One tool argument as a labeled field")
    .replace(/\bconst\b/g, "var"),
  sandbox
);
assert(sandbox.buildToolCard, "failed to extract buildToolCard from app.js");
assert(sandbox.skillAlwaysNote, "failed to extract skillAlwaysNote from app.js");

function walk(el, out = []) {
  out.push(el);
  for (const kid of (el && el.children) || []) walk(kid, out);
  return out;
}
const buttons = (card) => walk(card).filter((e) => e && e.tagName === "BUTTON");
const text = (el) => walk(el).map((e) => (typeof e === "string" ? e : e.textContent)).join("");

const BASE = {
  id: "c1", tool: "use_skill",
  args: { skill: "pdf", brief: "b", inputs: [] }, preview: "Run the third-party skill",
};

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}`); console.error(`       ${err.message}`); }
}

check("no offer, no Always button — the card cannot invent one", () => {
  const card = makeEl("div");
  sandbox.buildToolCard(card, BASE);
  assert.deepStrictEqual(buttons(card).map((b) => b.className), ["approve", "deny"]);
  assert(!text(card).includes("Always for this skill"));
});

check("an offer adds the button, which sends its own action", () => {
  answers.length = 0;
  const card = makeEl("div");
  sandbox.buildToolCard(card, { ...BASE, always: { skill: "pdf", files: 12 } });
  const [always] = buttons(card).filter((b) => b.className === "always-skill");
  assert(always, "no Always for this skill button");
  assert.strictEqual(always.textContent, "Always for this skill");
  always.onclick();
  assert.strictEqual(answers.length, 1);
  assert.strictEqual(answers[0].action, "approve_always_skill");
  assert.strictEqual(answers[0].id, "c1");
});

check("the Always button is never the A-shortcut's button.approve", () => {
  const card = makeEl("div");
  sandbox.buildToolCard(card, { ...BASE, always: { skill: "pdf", files: 3 } });
  const approves = buttons(card).filter((b) => b.className.split(/\s+/).includes("approve"));
  assert.strictEqual(approves.length, 1);
  assert.strictEqual(approves[0].textContent, "Approve");
});

check("the note names what is covered, how it ends, and what a comment does", () => {
  const note = sandbox.skillAlwaysNote({ skill: "pdf", files: 12 });
  const said = text(note);
  assert(said.includes("12 reviewed file(s) are unchanged"), said);
  assert(said.includes("aish skill revoke"), said);
  assert(said.includes("nothing is remembered"), said);
});

check("the skill name is set as text, never markup", () => {
  const evil = "<img src=x onerror=alert(1)>";
  const note = sandbox.skillAlwaysNote({ skill: evil, files: 1 });
  const name = walk(note).find((e) => e && e.className === "mono");
  assert.strictEqual(name.textContent, evil);
  assert(walk(note).every((e) => !e || !e.innerHTML), "innerHTML was used");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
