// Node-only, dependency-free check: deleting a chat from the sessions list takes
// TWO taps, never one.
//
// Why this exists: an earlier swipe-to-delete sent `delete_session` on a single
// unconfirmed tap, on a button a swipe had just slid under your thumb, and a
// real chat was lost to it. The action is irreversible — the log is unlinked,
// and the offline mirror drops server-deleted sessions on its next sync, so no
// copy survives. The CLI's /delete has always required [y/N]; this pins the web
// path to the same standard. The gesture is gone (leftward on the rail closes
// it now) and the chat menu is the only delete path, so this follows it there —
// the invariant belongs to the ACTION, not to whichever control offers it.
//
// Runs the REAL arming block from app.js (extracted by marker) against a minimal
// fake button, so it tests the shipped branching rather than a copy.
//
// Run manually: node tests/js/test_delete_confirm.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

let checks = 0;
function ok(label, cond) { assert(cond, label); checks += 1; }

function world() {
  const sent = [];
  const timers = [];
  const label = { textContent: "Delete chat" };
  const del = {
    _classes: new Set(),
    classList: {
      add(c) { del._classes.add(c); },
      remove(c) { del._classes.delete(c); },
      contains(c) { return del._classes.has(c); },
    },
    querySelector: () => label,
    get textContent() { return label.textContent; },
  };
  const sandbox = {
    currentSession: "session-20260725-205310-414476.jsonl",
    send: (m) => { sent.push(m); return true; },
    closeSheets() {},
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (id) timers[id - 1] = null; },
  };
  vm.createContext(sandbox);
  vm.runInContext(extract("// [DELARM-START]", "// [DELARM-END]"), sandbox);
  return { del, sent, timers, sandbox, tap: () => sandbox.armDeleteChat(del) };
}

// 1. THE REGRESSION: one tap must not delete anything.
{
  const w = world();
  w.tap();
  ok("first tap sends nothing", w.sent.length === 0);
  ok("first tap arms the control visibly", w.del.textContent === "Confirm delete");
  ok("first tap marks the control armed", w.del.classList.contains("armed"));
}

// 2. The second tap commits, with the right name.
{
  const w = world();
  w.tap();
  w.tap();
  ok("second tap deletes", w.sent.length === 1);
  ok("it deletes the right chat",
    w.sent[0].type === "delete_session"
    && w.sent[0].name === "session-20260725-205310-414476.jsonl");
  ok("committing resets the label", w.del.textContent === "Delete chat");
  ok("committing clears the armed style", !w.del.classList.contains("armed"));
}

// 3. A half-committed destructive control must not sit there indefinitely —
//    otherwise the SECOND tap becomes the accidental one.
{
  const w = world();
  w.tap();
  const armed = w.timers.filter(Boolean);
  ok("arming schedules a disarm", armed.length === 1);
  ok("the disarm window is bounded", armed[0].ms > 0 && armed[0].ms <= 10000);
  armed[0].fn();
  ok("it disarms itself", w.del.textContent === "Delete chat");
  ok("and stays disarmed visually", !w.del.classList.contains("armed"));
  w.tap();
  ok("after disarming, a tap only re-arms — it does not delete", w.sent.length === 0);
}

// 4. Committing cancels the pending disarm rather than leaving it to fire later.
{
  const w = world();
  w.tap();
  w.tap();
  ok("commit clears the disarm timer", w.timers.filter(Boolean).length === 0);
}

// 5. Reopening the menu must never find it still armed — a half-committed
//    destructive control surviving a dismissal makes the NEXT visit's first tap
//    the destructive one.
{
  ok("opening the chat menu resets the delete row",
    /openSessionMenu\(\)[\s\S]{0,400}?resetDeleteChat\(del\)/.test(src));
}

// 6. There is exactly ONE place that can send a delete, and it is behind the
//    arming. A second, unconfirmed sender anywhere would defeat all of the above.
{
  const senders = src.match(/send\(\{ type: "delete_session"/g) || [];
  ok("only one code path sends delete_session", senders.length === 1);
  const armed = extract("// [DELARM-START]", "// [DELARM-END]");
  ok("…and it is inside the arming block",
    armed.includes('send({ type: "delete_session"'));
}

console.log(`${checks} ok — all checks passed`);
