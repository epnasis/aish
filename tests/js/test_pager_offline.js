// Node-only, dependency-free check for where the swipe pager gets its pages
// (#165 follow-up). The pager normally pages through `hello.pager`, but that
// list only exists once a hello has landed — so a cold OFFLINE launch had no
// pages at all and every swipe silently rubber-banded. The fix derives the
// pages from the local mirror instead; this pins the source-selection rules:
//
//   * online with a server list  -> use the server's list (authoritative)
//   * offline                    -> use the mirror, even if a stale server
//                                   list is still in memory
//   * before the first hello     -> use the mirror
//   * offline with an empty mirror -> fall back, never worse than before
//
// Pulls the REAL functions out of app.js by marker.
//
// Run manually: node tests/js/test_pager_offline.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);
const start = src.indexOf("// [PAGER-SOURCE-START]");
const end = src.indexOf("// [PAGER-SOURCE-END]");
assert(start !== -1 && end !== -1, "PAGER-SOURCE markers not found in app.js");

// vm only surfaces `var` as sandbox properties; top-level function
// declarations land on it as-is.
const snippet = src.slice(start, end).replace(/\bconst PAGER_LIMIT\b/, "var PAGER_LIMIT");

function makeSandbox({ offlineMode = false, pagerSessions = [], mirror = [] } = {}) {
  const sandbox = {
    offlineMode,
    pagerSessions,
    offlineMeta: new Map(mirror.map((m) => [m.name, m])),
  };
  vm.createContext(sandbox);
  vm.runInContext(snippet, sandbox);
  return sandbox;
}

const meta = (name, ts, origin = "user") => ({ name, title: name, ts, origin });
const page = (name, origin = "user") => ({ name, title: name, origin });
const names = (rows) => Array.from(rows, (r) => r.name);
const eq = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures += 1;
    console.error(`FAIL - ${name}\n    ${err.message}`);
  }
}

check("online, the server's list wins", () => {
  const s = makeSandbox({
    pagerSessions: [page("from-server")],
    mirror: [meta("from-mirror", 10)],
  });
  eq(names(s.pagerPages()), ["from-server"]);
});

check("a cold offline launch pages through the mirror", () => {
  // The exact regression: no hello ever arrived, so pagerSessions is empty.
  const s = makeSandbox({
    offlineMode: true,
    pagerSessions: [],
    mirror: [meta("a", 100), meta("b", 200)],
  });
  eq(names(s.pagerPages()), ["a", "b"]);
});

check("before the first hello, the mirror fills in even while online", () => {
  const s = makeSandbox({ pagerSessions: [], mirror: [meta("a", 1)] });
  eq(names(s.pagerPages()), ["a"]);
});

check("offline, the mirror beats a stale in-memory server list", () => {
  // Connected, then went offline and browsed to a chat the old hello never
  // listed — the mirror is the more complete source at that point.
  const s = makeSandbox({
    offlineMode: true,
    pagerSessions: [page("stale-only")],
    mirror: [meta("a", 1), meta("b", 2), meta("c", 3)],
  });
  eq(names(s.pagerPages()), ["a", "b", "c"]);
});

check("offline with nothing mirrored falls back instead of going blank", () => {
  const s = makeSandbox({ offlineMode: true, pagerSessions: [page("x")], mirror: [] });
  eq(names(s.pagerPages()), ["x"]);
  const empty = makeSandbox({ offlineMode: true, pagerSessions: [], mirror: [] });
  eq(empty.pagerPages(), []);
});

check("mirror pages are oldest-to-newest, matching the server's order", () => {
  const s = makeSandbox({
    offlineMode: true,
    mirror: [meta("newest", 300), meta("oldest", 100), meta("middle", 200)],
  });
  eq(names(s.pagerPages()), ["oldest", "middle", "newest"]);
});

check("the mirror list is capped at the server's 30, keeping the newest", () => {
  const mirror = [];
  for (let i = 0; i < 45; i += 1) mirror.push(meta(`s${i}`, i));
  const s = makeSandbox({ offlineMode: true, mirror });
  const got = names(s.pagerPages());
  assert.strictEqual(got.length, 30);
  assert.strictEqual(got[0], "s15", "should drop the oldest, not the newest");
  assert.strictEqual(got[29], "s44");
});

check("origin rides along so the pager keeps its Recent/Automated lanes", () => {
  const s = makeSandbox({
    offlineMode: true,
    mirror: [meta("chat", 1), meta("cron", 2, "schedule")],
  });
  eq(s.pagerPages().map((p) => p.origin), ["user", "schedule"]);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
