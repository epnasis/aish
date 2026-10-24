// Node-only, dependency-free check: the Automations sheet (#5) — the web's
// `/tasks`, reachable from the workspace sheet too.
//
// What it pins:
//   - the sheet paints the SERVER's rows and derives no fact about a task; the
//     one mapping it owns is which buttons a stated state offers;
//   - every action names the automation of the ROW it was pressed on, captured
//     when that row was built — a repaint must never retarget a press;
//   - pause and resume ask nothing (reversible, as in the terminal), delete
//     goes through the shared [CONFIRM] modal and says what it costs;
//   - every change is an act() ([ACK-LEDGER]); the listing is a read.
//
// Runs the REAL block from app.js (extracted by marker) against a fake DOM.
//
// Run manually: node tests/js/test_automations_sheet.js
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

let checks = 0;
function ok(label, cond) { assert(cond, label); checks += 1; console.log(`ok - ${label}`); }

function node(tag) {
  return {
    tag,
    type: "",
    className: "",
    textContent: "",
    hidden: true,
    dataset: {},
    kids: [],
    onclick: null,
    appendChild(child) { this.kids.push(child); return child; },
    append(...children) { this.kids.push(...children); },
  };
}

// Setting textContent on a real element drops its children; the fake must too,
// or a repaint would look like it appended to the old list.
function clearable(base) {
  let text = "";
  return Object.defineProperty(base, "textContent", {
    get() { return text; },
    set(value) { text = value; this.kids = []; },
  });
}

function world() {
  const acted = [];
  const sent = [];
  const asked = [];
  const opened = [];
  const toasts = [];
  const nodes = {};
  const sandbox = {
    document: { createElement: node },
    $: (id) => (nodes[id] = nodes[id] || clearable(node("div"))),
    act: (message, opts) => { acted.push({ message, label: opts && opts.label }); return true; },
    send: (message) => { sent.push(message); return true; },
    askConfirm: (spec) => { asked.push(spec); },
    openSheet: (id) => { opened.push(id); },
    closeSessionRail: () => { opened.push("rail-closed"); },
    showToast: (text) => { toasts.push(text); },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    extract("// [AUTOMATIONS-START]", "// [AUTOMATIONS-END]").replace(/\blet\b|\bconst\b/g, "var"),
    sandbox,
  );
  return { sandbox, acted, sent, asked, opened, toasts, nodes };
}

// Every text under a node, depth first.
function texts(n) {
  return [n.textContent, ...n.kids.flatMap(texts)].filter(Boolean);
}
function buttons(n) {
  return n.tag === "button" ? [n] : n.kids.flatMap(buttons);
}
function rowOf(w, id) {
  return w.nodes["automations-list"].kids.find((k) => k.dataset && k.dataset.task === id);
}
function press(row, label) {
  const button = buttons(row).find((b) => b.textContent === label);
  assert(button, `no ${label} button on ${row.dataset.task}`);
  button.onclick();
}

const row = (over = {}) => ({
  id: "daily", note: "daily summary", kind: "task",
  when: "runs on the schedule `0 9 * * *`", state: "enabled",
  stands: "next 09:00 Fri 2026-10-16", ran: "last ran for 09:00 Thu 2026-10-15",
  notify: "always", allow: [], sites: [], ...over,
});

// ---- 1. Opening asks the server, and the rail stands down -----------------
{
  const w = world();
  w.sandbox.openAutomationsSheet();
  ok("opening stands the slide-over rail down, then opens the sheet",
    JSON.stringify(w.opened) === JSON.stringify(["rail-closed", "automations-sheet"]));
  ok("opening sends one listing read", JSON.stringify(w.sent) === JSON.stringify([{ type: "automations" }]));
  ok("the sheet says it is loading until the answer lands",
    w.nodes["automations-status"].textContent === "Loading…" && !w.nodes["automations-status"].hidden);
  ok("the workspace sheet's entry opens the same sheet", typeof w.nodes["ws-automations"].onclick === "function");
  w.nodes["ws-automations"].onclick();
  ok("…and it is a fetch like the slash", w.sent.length === 2);
}

// ---- 2. What a stated state offers ----------------------------------------
{
  const { sandbox } = world();
  const offered = (state) => JSON.stringify(sandbox.automationActions(row({ state })));
  ok("enabled offers pause, run now and delete", offered("enabled") === '["pause","run","delete"]');
  ok("DISABLED offers resume and delete — the server refuses a run while disabled",
    offered("DISABLED") === '["resume","delete"]');
  ok("ENDED offers only delete — the server refuses resume and run past the end",
    offered("ENDED") === '["delete"]');
}

// ---- 3. The row paints the server's words --------------------------------
{
  const w = world();
  w.sandbox.onAutomations({
    type: "automations",
    rows: [row({ allow: ["touch", "gws gmail"], sites: ["eon.pl"] }), row({ id: "bare", note: "", ran: "", state: "DISABLED" })],
    problems: [{ file: "broken.json", id: "broken", error: "Expecting value: line 1" }],
    notes: ["aish cannot read its scheduler state: boom"],
  });
  const daily = texts(rowOf(w, "daily"));
  ok("header: id — note, and the state as stated", daily.includes("daily — daily summary") && daily.includes("enabled"));
  ok("when, kind and notify on one line", daily.includes("task · runs on the schedule `0 9 * * *` · notify always"));
  ok("where it stands, then when it last ran", daily.includes("next 09:00 Fri 2026-10-16 · last ran for 09:00 Thu 2026-10-15"));
  ok("the shell grants are on the row", daily.some((t) => t.includes("commands starting with touch, gws gmail")));
  ok("the site grants are on the row", daily.some((t) => t.includes("on: eon.pl")));
  const bare = texts(rowOf(w, "bare"));
  ok("no note: the id alone, no dangling dash", bare.includes("bare"));
  ok("never ran: the stands line alone", bare.includes("next 09:00 Fri 2026-10-16"));
  ok("no grants: no grant lines", !bare.some((t) => t.startsWith("Runs without asking") || t.startsWith("Presses things")));
  const problem = texts(rowOf(w, "broken"));
  ok("a problem file is listed AS a problem, with its error",
    problem.includes("broken.json") && problem.includes("PROBLEM") && problem.includes("Expecting value: line 1"));
  ok("a problem offers only Delete", JSON.stringify(buttons(rowOf(w, "broken")).map((b) => b.textContent)) === '["Delete"]');
  ok("the server's notes are shown", texts(w.nodes["automations-list"]).includes("aish cannot read its scheduler state: boom"));
  ok("a plain listing clears the status line", w.nodes["automations-status"].hidden);
}

// ---- 4. Each press names its own row --------------------------------------
{
  const w = world();
  w.sandbox.onAutomations({ rows: [row({ id: "a" }), row({ id: "b", state: "DISABLED" })], problems: [] });
  const a = rowOf(w, "a");
  const b = rowOf(w, "b");
  press(a, "Pause");
  press(b, "Resume");
  ok("Pause sends enabled: false for its own row, as an act",
    JSON.stringify(w.acted[0]) === JSON.stringify({
      message: { type: "automation_enable", task: "a", enabled: false }, label: "pausing that automation",
    }));
  ok("Resume sends enabled: true for its own row, as an act",
    JSON.stringify(w.acted[1]) === JSON.stringify({
      message: { type: "automation_enable", task: "b", enabled: true }, label: "resuming that automation",
    }));
  ok("pause and resume ask nothing first", w.asked.length === 0);
  // A repaint lands under the finger: the old row's button still names "a".
  w.sandbox.onAutomations({ rows: [row({ id: "z" })], problems: [] });
  press(a, "Run now");
  ok("Run now sends run_now for the row it was built for, even after a repaint",
    JSON.stringify(w.acted[2]) === JSON.stringify({ message: { type: "run_now", task: "a" }, label: "starting that run" }));
  ok("…and refetches the listing", JSON.stringify(w.sent.at(-1)) === JSON.stringify({ type: "automations" }));
  ok("a repaint replaces the rows rather than appending", w.nodes["automations-list"].kids.length === 1);
}

// ---- 5. Delete goes through [CONFIRM] and says what it costs --------------
{
  const w = world();
  w.sandbox.onAutomations({ rows: [row()], problems: [] });
  press(rowOf(w, "daily"), "Delete");
  ok("nothing is sent before the modal is answered", w.acted.length === 0 && w.asked.length === 1);
  const spec = w.asked[0];
  ok("the title names the automation", spec.title === "Delete automation daily?");
  ok("the verb is Delete", spec.verb === "Delete");
  ok("the body states the consequence and the only way back",
    spec.body.includes("stops running") && spec.body.includes("no way back") &&
    spec.body.includes("its own approval card"));
  ok('it says "delete", never "remove"', !/remov/i.test(spec.title + spec.body));
  spec.action();
  ok("confirming sends automation_delete for that row, as an act",
    JSON.stringify(w.acted[0]) === JSON.stringify({
      message: { type: "automation_delete", task: "daily" }, label: "deleting that automation",
    }));
}

// ---- 6. Outcomes and refusals are said ------------------------------------
{
  const w = world();
  w.sandbox.onAutomations({ rows: [row({ state: "DISABLED" })], problems: [], outcome: "disabled daily — it will not start again until it is resumed" });
  ok("an outcome is shown in the sheet's status line, not a toast",
    w.nodes["automations-status"].textContent.startsWith("disabled daily") &&
    w.nodes["automations-status"].className === "auto-status" && w.toasts.length === 0);
  w.sandbox.onAutomations({ rows: [], problems: [], error: "there is no automation 'ghost' (/tasks lists them)" });
  ok("a refusal is shown as an error",
    w.nodes["automations-status"].className === "auto-status error" &&
    w.nodes["automations-status"].textContent.includes("no automation 'ghost'"));
  ok("an error does not also claim the folder is empty",
    !texts(w.nodes["automations-list"]).some((t) => t.startsWith("No automations")));
  w.sandbox.onAutomations({ rows: [], problems: [] });
  ok("an empty folder says how one is made",
    texts(w.nodes["automations-list"]).some((t) => t.startsWith("No automations") && t.includes("approval card")));
}

// ---- 7. The block's buttons are DOM, never model output -------------------
{
  const code = extract("// [AUTOMATIONS-START]", "// [AUTOMATIONS-END]");
  ok("the sheet never renders markdown or HTML", !/innerHTML|renderMarkdown/.test(code));
  ok("no bespoke confirmation: deletes go through askConfirm", !/window\.confirm|confirm\(/.test(code.replace(/askConfirm\(/g, "")));
}

console.log(`test_automations_sheet.js: ${checks} ok — all checks passed`);
