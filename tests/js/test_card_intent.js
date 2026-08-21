// The model's stated reason on an approval card (#252).
//
// The card said WHAT and never WHY, so the owner reverse-engineered the purpose
// from a tool name and its arguments. He guessed wrong, refused a legitimate
// verification, and the answer that followed was invented in its place.
//
// Three properties this pins: the reason is rendered WHOLE (the incident's
// reason is its second sentence, which every first-sentence summary drops), it
// is a separate box from the code-computed preview and args (a claim must not
// borrow the authority of a fact), and its ABSENCE is rendered rather than
// silently omitted — "it gave no reason" is what tells the owner he is back to
// guessing.
//
// Runs the REAL intentBlock / buildCommandCard / buildToolCard out of app.js by
// marker, against a minimal fake DOM. Run manually: node tests/js/test_card_intent.js
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
      toggle() {},
      remove(...names) {
        el._class = el._class.split(/\s+/).filter((c) => !names.includes(c)).join(" ");
      },
    },
    textContent: "",
    title: "",
    type: "",
    rows: 0,
    placeholder: "",
    autocomplete: "",
    value: "",
    contentEditable: "false",
    isContentEditable: false,
    append(...kids) { el.children.push(...kids); },
    appendChild(kid) { el.children.push(kid); return kid; },
    querySelector() { return makeEl("span"); },
    querySelectorAll() { return []; },
    addEventListener() {},
    setAttribute() {},
    focus() {},
  };
  return el;
}

const sandbox = {
  document: { createElement: makeEl, createTextNode: (t) => ({ textContent: t }) },
  // The builders' collaborators, stubbed to identifiable nodes so the SHAPE of
  // what they append is observable.
  highlightCommand: () => {},
  pencilIcon: () => makeEl("svg"),
  wrenchIcon: () => makeEl("svg"),
  copyChip: () => makeEl("button"),
  feedbackField: () => { const f = makeEl("div"); f.className = "feedback"; return f; },
  answerCard: () => {},
  feedbackExtra: () => ({}),
  escapeNote: () => makeEl("div"),
  abbreviatePath: (p) => p,
  currentCwd: "/tmp/project",
  scopeExplain: () => makeEl("div"),
  toolArgRow: (k, v) => {
    const row = makeEl("div");
    row.className = "tool-arg";
    row.textContent = `${k}=${v}`;
    return row;
  },
  SCOPE_LABELS: {
    approve: "this once", approve_session: "this chat",
    approve_always: "always", approve_trust: "this folder",
  },
  CARD_SHIELD: "", CARD_TRIANGLE: "", FOLDER_SVG: "",
  FINE_POINTER: false,
};
vm.createContext(sandbox);
// intentBlock + buildCommandCard sit together; buildToolCard is separate.
vm.runInContext(
  extract("function intentBlock", "function buildWriteCard").replace(/\bconst\b/g, "var"),
  sandbox
);
vm.runInContext(
  extract("function buildToolCard", "function toolArgRow").replace(/\bconst\b/g, "var"),
  sandbox
);
assert(sandbox.intentBlock, "failed to extract intentBlock from app.js");
assert(sandbox.buildCommandCard, "failed to extract buildCommandCard from app.js");
assert(sandbox.buildToolCard, "failed to extract buildToolCard from app.js");

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}`); console.error(`       ${err.message}`); }
}

function find(card, cls) {
  return card.children.find((c) => c._class && c._class.split(/\s+/).includes(cls));
}

function intentText(card) {
  const box = find(card, "card-intent");
  assert(box, "no .card-intent on the card");
  const body = box.children.find((c) => c._class === "card-intent-text");
  assert(body, "the intent box has no text node");
  return body.textContent;
}

// The step that was refused, with the owner's personal details removed. The
// reason lives in the SECOND sentence — that is the point of the fixture.
const INTENT =
  'I am going to open the "Faktury i platnosci" page in the browser again. '
  + "This will let us see if there is any credit, overpayment, or adjusting "
  + "transaction on the account balance that explains why the portal asks for "
  + "354.56 while the PDF invoice itself shows 356.46.";

const TOOL_EVENT = { tool: "browse", args: { url: "https://example.invalid/x" } };
const CMD_EVENT = { command: "grep -R nadplata .", prefixes: [], escapes: [] };

check("a tool card shows the reason, whole", () => {
  const card = makeEl("div");
  sandbox.buildToolCard(card, { ...TOOL_EVENT, intent: INTENT });
  assert.strictEqual(intentText(card), INTENT);
  assert(intentText(card).includes("credit, overpayment"),
    "the reason was truncated — the second sentence IS the reason");
});

check("a command card shows it too — the gate is one gate", () => {
  const card = makeEl("div");
  sandbox.buildCommandCard(card, { ...CMD_EVENT, intent: INTENT });
  assert.strictEqual(intentText(card), INTENT);
});

check("the reason is attributed, not stated as fact", () => {
  const card = makeEl("div");
  sandbox.buildToolCard(card, { ...TOOL_EVENT, intent: INTENT });
  const box = find(card, "card-intent");
  const label = box.children.find((c) => c._class === "card-intent-label");
  assert(label && label.textContent.trim(), "the reason carries no attribution");
  assert(/aish/i.test(label.textContent), `label said: ${label.textContent}`);
});

check("it is its own box, never merged into the preview", () => {
  // The preview is ground truth the tool computed (#157). Folding a claim into
  // it would lend the claim the authority of the fact.
  const card = makeEl("div");
  sandbox.buildToolCard(card, {
    ...TOOL_EVENT, preview: 'click "Zaplac" on example.invalid', intent: INTENT,
  });
  const preview = find(card, "tool-preview");
  assert(preview, "the preview went missing");
  assert.strictEqual(preview.textContent, 'click "Zaplac" on example.invalid');
  assert(!preview.textContent.includes("credit"), "the reason leaked into the preview");
  assert.strictEqual(intentText(card), INTENT);
});

check("no reason is SAID to be missing, not silently omitted", () => {
  // Absence is information: it tells the owner he is back to guessing, and it
  // is what makes the silence visible if the model ever stops narrating.
  for (const event of [{ ...TOOL_EVENT }, { ...TOOL_EVENT, intent: "   " }]) {
    const card = makeEl("div");
    sandbox.buildToolCard(card, event);
    const box = find(card, "card-intent");
    assert(box, "a card with no reason grew no box at all");
    assert(box.classList.contains("empty"), "the empty state is not marked as empty");
    assert(/no reason/i.test(intentText(card)), `said: ${intentText(card)}`);
  }
});

check("the facts still come first on both cards", () => {
  // Reading order is the design: what will happen, then what it is claimed for.
  const tool = makeEl("div");
  sandbox.buildToolCard(tool, { ...TOOL_EVENT, intent: INTENT });
  const argsAt = tool.children.findIndex((c) => c._class && c._class.startsWith("tool-args"));
  const toolIntentAt = tool.children.indexOf(find(tool, "card-intent"));
  assert(argsAt !== -1 && toolIntentAt > argsAt,
    `args at ${argsAt}, reason at ${toolIntentAt} — the claim precedes the facts`);

  const cmd = makeEl("div");
  sandbox.buildCommandCard(cmd, { ...CMD_EVENT, intent: INTENT });
  const boxAt = cmd.children.findIndex((c) => c._class === "cmd-box");
  const cmdIntentAt = cmd.children.indexOf(find(cmd, "card-intent"));
  assert(boxAt !== -1 && cmdIntentAt > boxAt,
    `command at ${boxAt}, reason at ${cmdIntentAt} — the claim precedes the command`);
});

check("the reason sits above the comment field, where it can still be read", () => {
  // The comment is how a misread becomes a refusal (#81). The reason has to be
  // on screen BEFORE the box that acts on it.
  const card = makeEl("div");
  sandbox.buildToolCard(card, { ...TOOL_EVENT, intent: INTENT });
  const intentAt = card.children.indexOf(find(card, "card-intent"));
  const feedbackAt = card.children.findIndex((c) => c._class === "feedback");
  assert(feedbackAt !== -1, "the comment field went missing");
  assert(intentAt < feedbackAt, "the reason is below the comment box");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
