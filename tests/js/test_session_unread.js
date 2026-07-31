// Node-only, dependency-free check for the rail's organising principle: the
// list is banded by ATTENTION, not by provenance.
//
// It replaces the Recent / Automated tabs, and the reason is worth stating where
// someone will read it before "restoring" them: a split by who STARTED a chat
// put an email-triggered session holding an approval in a room you had to
// remember to visit, while your own chat that finished a long task unattended
// sat in the other one. The design already admitted this — attention counters
// had to be computed for the HIDDEN tab, and search dropped the split entirely.
// Both are the same complaint. Provenance is now a row glyph; the band is
// "does this want me".
//
// Two pure functions carry it, and both are extracted from app.js and run for
// real here: sessionUnread (the decision) and partitionSessions (the bands).
//
// Run manually: node tests/js/test_session_unread.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

let passed = 0;
function ok(what, cond) {
  assert(cond, what);
  passed++;
}

// sessionUnread lives in [SEEN]; partitionSessions in the SESSIONS_PARTITION
// markers. Both are pure, so one sandbox with no DOM at all does.
const s = {};
vm.createContext(s);
vm.runInContext(extract("// PURE: the whole unread decision", "// [SEEN-END]"), s);
vm.runInContext(extract("// SESSIONS_PARTITION_START", "// SESSIONS_PARTITION_END"), s);
const { sessionUnread } = s;

const SEC = 1000;
const NOW = 1_700_000_000_000;      // fixed clock: these are all comparisons
const at = (msAgo) => (NOW - msAgo) / SEC; // a row's ts, in epoch SECONDS

// ---- 1. the unread decision ----------------------------------------------
const state = (over) => Object.assign({ seen: {}, since: NOW - 10_000, current: null }, over);

ok("activity newer than the last look here is unread",
  sessionUnread({ name: "a", ts: at(1_000) }, state({ seen: { a: NOW - 5_000 } })) === true);
ok("activity older than the last look is read",
  sessionUnread({ name: "a", ts: at(9_000) }, state({ seen: { a: NOW - 5_000 } })) === false);

// The floor is what keeps day one sane: on a device that has never seen ANY of
// these chats, the whole archive would otherwise arrive unread and the band —
// whose entire value is being short — would simply be the list again.
ok("a chat that predates this device's first run is read, unseen or not",
  sessionUnread({ name: "old", ts: at(60_000) }, state()) === false);
ok("…but one that moved after that first run is unread",
  sessionUnread({ name: "new", ts: at(1_000) }, state()) === true);

// The chat on screen produces activity constantly while you watch it.
ok("the chat you are looking at is never unread",
  sessionUnread({ name: "a", ts: at(0) }, state({ current: "a" })) === false);
ok("a row with no timestamp at all is not unread (nothing to compare)",
  sessionUnread({ name: "a", ts: 0 }, state()) === false);

// ---- 2. the bands ---------------------------------------------------------
// Provenance deliberately does NOT appear here: an automated row is sorted by
// whether it wants you, exactly like your own.
{
  const rows = [
    { name: "held", ts: at(30_000), state: "waiting", origin: "email" },
    { name: "mine-held", ts: at(30_000), state: "waiting", origin: "user" },
    { name: "fresh", ts: at(1_000), state: "", origin: "schedule" },
    { name: "busy", ts: at(60_000), state: "running", origin: "user" },
    { name: "kept", ts: at(90_000), state: "", origin: "user", pinned: true },
    { name: "plain", ts: at(90_000), state: "", origin: "user" },
    { name: "bot-old", ts: at(90_000), state: "", origin: "email" },
  ];
  const { bands, counts } = partitionAll(rows);

  const names = (b) => b.map((r) => r.name).join(",");
  ok("an approval hold is in the band whoever started it",
    names(bands.needsYou).includes("held") && names(bands.needsYou).includes("mine-held"));
  ok("so is unseen activity", names(bands.needsYou).includes("fresh"));
  ok("running is worth seeing but is not asking for anything",
    names(bands.active) === "busy");
  ok("a pinned chat recency would sink gets its own band", names(bands.pinned) === "kept");
  ok("an old triggered chat is just an old chat", names(bands.rest) === "plain,bot-old");
  ok("every row lands in exactly one band",
    bands.needsYou.length + bands.active.length + bands.pinned.length + bands.rest.length
      === rows.length);
  ok("the counters are computed from the same pass",
    counts.waiting === 2 && counts.running === 1 && counts.unread === 1);
}

// A held approval outranks a pin: the pin says "keep this reachable", the hold
// says "I am blocked on you".
{
  const { bands } = partitionAll([
    { name: "p", ts: at(90_000), state: "waiting", origin: "user", pinned: true },
  ]);
  ok("a pinned chat that needs you is in Needs you, not Pinned",
    bands.needsYou.length === 1 && bands.pinned.length === 0);
}

function partitionAll(rows) {
  return s.partitionSessions(rows, { seen: {}, since: NOW - 10_000, current: null });
}

console.log(`test_session_unread.js: ${passed} ok — all checks passed`);
