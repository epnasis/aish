// Node-only, dependency-free check: deleting a chat from the sessions list takes
// TWO taps, never one.
//
// Why this exists: swipe-to-delete sent `delete_session` on a single unconfirmed
// tap. The action is irreversible — the log is unlinked, and the offline mirror
// drops server-deleted sessions on its next sync, so no copy survives — and the
// button sits one row away from the ✕ that merely moves a chat to HISTORY. A
// real chat was lost to it. The CLI's /delete has always required [y/N]; this
// pins the web path to the same standard.
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
  const del = {
    textContent: "Delete",
    _classes: new Set(),
    classList: {
      add(c) { del._classes.add(c); },
      remove(c) { del._classes.delete(c); },
      contains(c) { return del._classes.has(c); },
    },
    onclick: null,
  };
  const sandbox = {
    del,
    info: { name: "session-20260725-205310-414476.jsonl" },
    send: (m) => { sent.push(m); return true; },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (id) timers[id - 1] = null; },
  };
  vm.createContext(sandbox);
  vm.runInContext(extract("  // [DELARM-START]", "  // [DELARM-END]"), sandbox);
  return { del, sent, timers, sandbox, tap: () => del.onclick({ stopPropagation() {} }) };
}

// 1. THE REGRESSION: one tap must not delete anything.
{
  const w = world();
  w.tap();
  ok("first tap sends nothing", w.sent.length === 0);
  ok("first tap arms the control visibly", w.del.textContent === "Confirm");
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
  ok("committing resets the label", w.del.textContent === "Delete");
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
  ok("it disarms itself", w.del.textContent === "Delete");
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

// 5. The row-close path disarms too — an armed control surviving the row
//    snapping shut would make the next swipe's first tap destructive.
{
  const closers = [
    /if \(open\) \{ open = false; disarm\(\); set\(0\); return; \}/,
    /if \(!open\) disarm\(\);/,
  ];
  for (const re of closers) {
    ok(`closing the row disarms (${re.source.slice(0, 28)}…)`, re.test(src));
  }
}

// 6. And the single-tap form must never come back.
{
  ok("no unconfirmed delete_session send survives in the swipe control",
    !/del\.onclick = \(e\) => \{ e\.stopPropagation\(\); send\(\{ type: "delete_session"/.test(src));
}

console.log(`${checks} ok — all checks passed`);
