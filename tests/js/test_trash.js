// Node-only, dependency-free check: Recently deleted — the way back from a
// delete (#177).
//
// Deleting a chat used to be one unlink with no way back. The server now holds
// the log in a trash it purges after 30 days, and this section is what makes
// that reachable — without it the trash is only a slower delete.
//
// What this pins is not "a section renders" but the four properties that stop
// it from being a second, worse chat list:
//   - a deleted chat is NOT a chat row: it is not tappable into resumeSession,
//     which is the "no such chat" error [FORGET-SESSION] exists to prevent;
//   - the list is the SERVER's, with no local copy and no optimistic edit;
//   - an action names the chat the SHEET was opened about, never whatever the
//     list repainted to in between;
//   - restoring is not destructive and asks nothing, while deleting for good
//     is the one thing here that cannot be undone.
//
// Runs the REAL block from app.js (extracted by marker) against a fake DOM.
//
// Run manually: node tests/js/test_trash.js
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
function ok(label, cond) { assert(cond, label); checks += 1; }

// The block's CODE — comments stripped, since several of them name the very
// things the source-level checks below assert are absent.
function code() {
  return extract("// [TRASH-START]", "// [TRASH-END]")
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n");
}

function node(tag) {
  return {
    tag,
    className: "",
    id: "",
    textContent: "",
    hidden: true,
    dataset: {},
    attrs: {},
    kids: [],
    onclick: null,
    setAttribute(name, value) { this.attrs[name] = value; },
    appendChild(child) { this.kids.push(child); return child; },
    append(...children) { this.kids.push(...children); },
  };
}

function world(entries = [], { keepDays = 30 } = {}) {
  const acted = [];
  const asked = [];
  const opened = [];
  const nodes = {};
  const sandbox = {
    document: { createElement: node },
    DAY_MS: 86400000,
    $: (id) => (nodes[id] = nodes[id] || node("div")),
    act: (message, opts) => { acted.push({ message, label: opts && opts.label }); return true; },
    askConfirm: (spec) => { asked.push(spec); },
    openSheet: (id) => { opened.push(id); },
    closeSheets: () => { opened.push("closed"); },
    closeSessionRail: () => { opened.push("rail-closed"); },
    railIsOpen: () => true,
    renderSessionsFromCache: () => {},
    requestSessions: () => {},
    offlineSyncSoon: () => {},
    rosterBaseline: () => {},
    showToast: () => {},
  };
  vm.createContext(sandbox);
  vm.runInContext(
    extract("// [TRASH-START]", "// [TRASH-END]").replace(/\blet\b|\bconst\b/g, "var"),
    sandbox,
  );
  // AFTER the block, because the block declares it: what a real page has by
  // the time any of this runs, adopted from the hello ([TRASH]).
  sandbox.trashKeepDays = keepDays;
  sandbox.onTrashList({ entries });
  return { sandbox, acted, asked, opened, nodes, list: () => node("div") };
}

const DAY = 86400000;
const NOW = Date.UTC(2026, 8, 19, 12, 0, 0);
const entry = (over = {}) => ({
  entry: "1700000000-session-20260101-000000-000000.jsonl",
  name: "session-20260101-000000-000000.jsonl",
  title: "Flights to the Maldives",
  deleted_at: (NOW - 2 * DAY) / 1000,
  ...over,
});

// ---- 1. The row says how much of the window is gone, not a clock time -----
// `sessionStamp` answers "when did this chat last DO something", which on a
// deleted chat reads as activity. The only question here is how long it has
// left.
{
  const w = world();
  const stamp = (days) => w.sandbox.trashStamp((NOW - days * DAY) / 1000, NOW);
  ok("deleted moments ago reads as today", stamp(0) === "Deleted today");
  ok("yesterday is named, not counted", stamp(1) === "Deleted yesterday");
  ok("beyond that it counts days", stamp(9) === "Deleted 9 days ago");
  // A clock a few minutes fast must not produce "Deleted -1 days ago".
  ok("a device running fast cannot count backwards",
    w.sandbox.trashStamp((NOW + 60000) / 1000, NOW) === "Deleted today");
}

// ---- 2. Nothing in the trash renders nothing -----------------------------
// A permanent "Recently deleted (0)" row is a control that never does
// anything, sitting under the list you actually navigate by.
{
  const w = world([]);
  const list = node("div");
  w.sandbox.renderTrashSection(list, w.sandbox.trashRows);
  ok("an empty trash puts no section in the rail", list.kids.length === 0);
}

// ---- 3. Collapsed by default, behind its own count -----------------------
{
  const w = world([entry(), entry({ entry: "1700000001-session-b.jsonl", title: "Tefal pan" })]);
  const list = node("div");
  w.sandbox.renderTrashSection(list, w.sandbox.trashRows);
  ok("one header and nothing else", list.kids.length === 1);
  const head = list.kids[0];
  ok("it says what it is and how much is in it", /Recently deleted \(2\)/.test(head.textContent));
  ok("…and that it is closed", head.attrs["aria-expanded"] === "false");

  head.onclick();
  const opened = node("div");
  w.sandbox.renderTrashSection(opened, w.sandbox.trashRows);
  ok("a tap opens it", opened.kids[0].attrs["aria-expanded"] === "true");
  const rows = opened.kids.filter((k) => k.className.includes("trash-row"));
  ok("…and every deleted chat is a row", rows.length === 2);
  ok("the policy is stated once, from the server's number",
    opened.kids.some((k) => /deleted for good after 30 days/.test(k.textContent)));
}

// ---- 4. A deleted chat is NOT a chat row ---------------------------------
// The trap this section exists next to: rendered as a `sessionRow`, a deleted
// chat is tappable straight into `resumeSession`, and the answer is the "no
// such chat" error [FORGET-SESSION] exists to stop.
{
  const w = world([entry()]);
  w.sandbox.trashExpanded = true;
  const list = node("div");
  w.sandbox.renderTrashSection(list, w.sandbox.trashRows);
  const row = list.kids.find((k) => k.className.includes("trash-row"));
  ok("the row is not a session row", !row.className.includes("session-row"));
  ok("…and carries no chat name for the rail's 'you are here' mark",
    row.dataset.name === undefined);
  row.onclick();
  ok("tapping it opens the sheet rather than the chat",
    w.opened.includes("trash-sheet") && w.acted.length === 0);
  // Source-level, because the wiring is what would rot: no path from this
  // block reaches the resume machinery. Comments are stripped first, or this
  // check is answered by the paragraph explaining why it exists.
  ok("nothing here resumes a chat", !/resumeSession|railRow/.test(code()));
}

// ---- 4b. The sheet is reachable: the rail stands down before it opens -----
// The slide-over rail sits above every sheet and does not stack with one, so a
// sheet raised from a rail row opens BEHIND the rail — a headless-Chrome run at
// phone width found "Restore chat" visible but unreachable, every tap landing on
// the rail. The row does what picking a chat does: closes the rail first.
{
  const w = world([entry()]);
  w.sandbox.openTrashSheet(entry());
  ok("tapping a deleted row stands the rail down, THEN opens the sheet",
    w.opened.join(",") === "rail-closed,trash-sheet");
}

// ---- 5. An action names the chat the question was about ------------------
// The list under the sheet is repainted by every `trash_list` the server
// sends, so reading the entry back at press time would land the action on
// whatever happened to be there.
{
  const w = world([entry()]);
  w.sandbox.openTrashSheet(entry());
  w.sandbox.onTrashList({ entries: [entry({
    entry: "1700009999-session-somewhere-else.jsonl", title: "A different chat",
  })] });

  w.sandbox.$("trash-restore").onclick();
  ok("restoring goes out as an ACT, held open until the server answers",
    w.acted.length === 1 && w.acted[0].message.type === "restore_session"
    && /restor/i.test(w.acted[0].label));
  ok("…naming the chat the sheet was opened about",
    w.acted[0].message.entry === entry().entry);
}

// ---- 6. Restoring asks nothing; deleting for good asks, and says so ------
{
  const w = world([entry()]);
  w.sandbox.openTrashSheet(entry());
  w.sandbox.$("trash-restore").onclick();
  ok("putting a chat back is not a question", w.asked.length === 0);

  w.sandbox.openTrashSheet(entry());
  w.sandbox.$("trash-purge").onclick();
  ok("deleting for good is", w.asked.length === 1);
  ok("…through the one shared modal, naming the chat",
    /Flights to the Maldives/.test(w.asked[0].body));
  ok("…and it is the one place that still says nothing brings it back",
    /cannot be undone/.test(w.asked[0].body));
  ok("asking has sent nothing", w.acted.length === 1); // still just the restore
  w.asked[0].action();
  ok("confirming does", w.acted[1].message.type === "purge_session");
  ok("…as an ACT too", /delet/i.test(w.acted[1].label));
}

// ---- 7. The list is the server's, and emptying it closes the section -----
// A section that vanished while open would come back expanded at the next
// delete, which nobody asked for.
{
  const w = world([entry()]);
  w.sandbox.trashExpanded = true;
  w.sandbox.onTrashList({ entries: [] });
  ok("the section is not left open over nothing", w.sandbox.trashExpanded === false);
  ok("…and holds no stale rows", w.sandbox.trashRows.length === 0);

  // No local copy and no optimistic edit: this is the one place in the rail
  // where a tap does not move the screen ahead of the server (L7 does not
  // apply — the trash is one directory on one machine and every device has to
  // agree about it).
  ok("nothing edits the list locally",
    !/trashRows\.(splice|push|pop|shift|unshift)/.test(code()));
}

// ---- 8. A chat coming back reaches this device ---------------------------
{
  const w = world([entry()]);
  let asked = 0;
  let synced = 0;
  w.sandbox.requestSessions = () => { asked += 1; };
  w.sandbox.offlineSyncSoon = () => { synced += 1; };
  w.sandbox.$("sessions-search").value = "";
  w.sandbox.onSessionRestored({ name: entry().name, seq: 7 });
  ok("the list is re-asked for, so the row is back where it belongs", asked === 1);
  // The mirror dropped this device's copy when the chat was deleted
  // ([MIRROR-FORGET]) — deliberately, because the server is holding the bytes.
  // The repair is to sync, not to have kept a ghost.
  ok("…and the mirror is told to fetch the copy it dropped", synced === 1);
}

console.log(`${checks} ok — all checks passed`);
