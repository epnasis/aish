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
      toggle(name) {
        if (el.classList.contains(name)) { el.classList.remove(name); return false; }
        el.classList.add(name);
        return true;
      },
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
  renderDiff: (text) => { const d = makeEl("pre"); d.className = "diff"; d.textContent = text; return d; },
  svgIcon: () => makeEl("svg"),
  relTarget: (t) => t,
  highlightFences: () => {},
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
  extract("const INTENT_FOLD_CHARS", "function buildWriteCard").replace(/\bconst\b/g, "var"),
  sandbox
);
vm.runInContext(
  extract("function buildToolCard", "function toolArgRow").replace(/\bconst\b/g, "var"),
  sandbox
);
vm.runInContext(
  extract("function buildWriteCard", "function buildToolCard").replace(/\bconst\b/g, "var"),
  sandbox
);
vm.runInContext(
  extract("function buildImportCard", "function wrenchIcon").replace(/\bconst\b/g, "var"),
  sandbox
);
vm.runInContext(
  extract("function buildReadCard", "function onApprovalResolved").replace(/\bconst\b/g, "var"),
  sandbox
);
assert(sandbox.intentBlock, "failed to extract intentBlock from app.js");
assert(sandbox.buildCommandCard, "failed to extract buildCommandCard from app.js");
assert(sandbox.buildToolCard, "failed to extract buildToolCard from app.js");
assert(sandbox.buildWriteCard, "failed to extract buildWriteCard from app.js");
assert(sandbox.buildImportCard, "failed to extract buildImportCard from app.js");
assert(sandbox.buildReadCard, "failed to extract buildReadCard from app.js");

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

const BUILDERS = {
  command: (c) => sandbox.buildCommandCard(c, { ...CMD_EVENT, intent: INTENT }),
  tool: (c) => sandbox.buildToolCard(c, { ...TOOL_EVENT, intent: INTENT }),
  write: (c) => sandbox.buildWriteCard(c,
    { verb: "edit", target: "a.py", diff: "+1", added: 1, removed: 0, intent: INTENT }),
  read: (c) => sandbox.buildReadCard(c,
    { path: "/etc/hosts", reason: "sensitive", escapes: [], intent: INTENT }),
  import: (c) => sandbox.buildImportCard(c,
    { skill: "x", description: "d", files: [], skipped: [], flags: [], dest: "/d",
      intent: INTENT }),
};

check("EVERY card kind carries it — the gate is one gate", () => {
  // A reason on some cards and not others is worse than none: the owner cannot
  // tell "it gave no reason" from "this kind of card never shows one".
  for (const [kind, build] of Object.entries(BUILDERS)) {
    const card = makeEl("div");
    build(card);
    assert.strictEqual(intentText(card), INTENT, `the ${kind} card lost the reason`);
  }
});

check("an import card puts the reason ABOVE the file listing", () => {
  // The consolidated review (#139) shows whole files. A reason below them is a
  // reason nobody scrolls back up from.
  const card = makeEl("div");
  sandbox.buildImportCard(card, {
    skill: "x", description: "d", dest: "/d", flags: [], skipped: [],
    files: [{ path: "run.sh", content: "echo hi", executable: true }], intent: INTENT,
  });
  const intentAt = card.children.indexOf(find(card, "card-intent"));
  const firstFileAt = card.children.findIndex((c) => c._class === "import-file-head mono");
  assert(firstFileAt !== -1, "the file listing went missing");
  assert(intentAt < firstFileAt, "the reason is buried under the files");
});

check("a write card puts the reason after the diff, before the comment", () => {
  const card = makeEl("div");
  sandbox.buildWriteCard(card,
    { verb: "edit", target: "a.py", diff: "+1", added: 1, removed: 0, intent: INTENT });
  const diffAt = card.children.findIndex((c) => c._class === "diff");
  const intentAt = card.children.indexOf(find(card, "card-intent"));
  const feedbackAt = card.children.findIndex((c) => c._class === "feedback");
  assert(diffAt !== -1 && intentAt > diffAt, "the claim precedes the diff");
  assert(intentAt < feedbackAt, "the reason is below the comment box");
});

// --- the fold ------------------------------------------------------------
const LONG = (INTENT + " "
  + "It also needs the payment history for the period. ".repeat(12)).trim();

check("a short reason is never folded — including the incident's own", () => {
  assert(INTENT.length < sandbox.INTENT_FOLD_CHARS,
    `the case this feature exists for (${INTENT.length} chars) would need a tap`);
  const card = makeEl("div");
  sandbox.buildToolCard(card, { ...TOOL_EVENT, intent: INTENT });
  const box = find(card, "card-intent");
  assert(!box.classList.contains("folded"), "a short reason grew a fold");
  assert(!box.children.some((c) => c._class === "card-intent-more"), "an unneeded control");
});

check("a long reason folds — and keeps every character in the DOM", () => {
  const card = makeEl("div");
  sandbox.buildToolCard(card, { ...TOOL_EVENT, intent: LONG });
  const box = find(card, "card-intent");
  assert(box.classList.contains("folded"), "a wall of text was left unfolded");
  // The whole point: the clamp is CSS, the text is complete. Cutting the
  // string here would be the truncation bug this feature was built against.
  assert.strictEqual(intentText(card), LONG);
});

check("the fold opens and closes, and says which it will do", () => {
  const card = makeEl("div");
  sandbox.buildToolCard(card, { ...TOOL_EVENT, intent: LONG });
  const box = find(card, "card-intent");
  const more = box.children.find((c) => c._class === "card-intent-more");
  assert(more, "a folded reason has no way to open it");
  assert.strictEqual(more.textContent, "Show more");
  more.onclick();
  assert(box.classList.contains("open"), "tapping it did not open the fold");
  assert.strictEqual(more.textContent, "Show less");
  more.onclick();
  assert(!box.classList.contains("open"), "tapping it again did not close the fold");
  assert.strictEqual(more.textContent, "Show more");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
