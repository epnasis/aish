// Node-only, dependency-free check for the offline session search (#165).
// When the server is unreachable the sessions sheet ranks with offlineRank(),
// a JS port of SessionLog.rank() in session.py. The point of the port is that
// searching offline FEELS the same as searching online, so this pins the tier
// order (exact title > title substring > content phrase > all words > fuzzy)
// and the newest-first tie-break. Pulls the REAL functions out of app.js by
// marker so the shipped implementation is what runs here.
//
// Run manually: node tests/js/test_offline_search.js
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

// vm only surfaces `var` (not const) as sandbox properties; top-level function
// declarations land on it as-is.
const snippet = [
  extract("// [OFFLINE-SEARCH-START]", "// [OFFLINE-SEARCH-END]"),
  "var OFFLINE_SEARCH_CHARS = 200000;",
  extract("// [OFFLINE-INDEX-START]", "// [OFFLINE-INDEX-END]"),
].join("\n").replace(/\bconst\b/g, "var");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(snippet, sandbox);
const { offlineRank, lcsRatio, offlineSearchText } = sandbox;
assert(typeof offlineRank === "function", "offlineRank not extracted");
assert(typeof lcsRatio === "function", "lcsRatio not extracted");
assert(typeof offlineSearchText === "function", "offlineSearchText not extracted");

const meta = (name, title, text, ts) => ({ name, title, text, ts, snippet: "", origin: "user" });

// Newest first, matching the server's recency-ordered input.
const corpus = [
  meta("session-a.jsonl", "deploy the web server", "we discussed nginx and tls certificates", 500),
  meta("session-b.jsonl", "kitchen renovation", "the plumber quoted for the sink and tiles", 400),
  meta("session-c.jsonl", "deploy", "a short chat about shipping", 300),
  meta("session-d.jsonl", "holiday plans", "flights to lisbon, deploy nothing", 200),
];

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

const names = (rows) => Array.from(rows, (r) => r.name);

// deepStrictEqual compares prototypes, and arrays built inside the vm realm
// have a different Array constructor — compare by JSON instead.
const eq = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);

check("an empty query keeps everything, newest first", () => {
  eq(
    names(offlineRank(corpus, "")),
    ["session-a.jsonl", "session-b.jsonl", "session-c.jsonl", "session-d.jsonl"]
  );
  eq(names(offlineRank(corpus, "   ")), names(offlineRank(corpus, "")));
});

check("an exact title match outranks a title substring", () => {
  // "deploy" IS session-c's whole title (tier 5); it is only a substring of
  // session-a's (tier 4) and only appears in session-d's content (tier 3).
  eq(
    names(offlineRank(corpus, "deploy")),
    ["session-c.jsonl", "session-a.jsonl", "session-d.jsonl"]
  );
});

check("a phrase found only in the conversation body still matches", () => {
  // This is the case the offline index exists for: the words are in what was
  // SAID, not in any title.
  eq(names(offlineRank(corpus, "tls certificates")), ["session-a.jsonl"]);
});

check("all-words-present matches when the phrase does not", () => {
  eq(names(offlineRank(corpus, "tiles plumber")), ["session-b.jsonl"]);
});

check("a typo still finds the chat (fuzzy tier)", () => {
  const hit = names(offlineRank(corpus, "certificats"));
  assert.ok(hit.includes("session-a.jsonl"), `expected a fuzzy hit, got ${JSON.stringify(hit)}`);
});

check("no match returns nothing rather than everything", () => {
  eq(offlineRank(corpus, "zzzzqqqq"), []);
});

check("search is case-insensitive", () => {
  eq(names(offlineRank(corpus, "DEPLOY")), names(offlineRank(corpus, "deploy")));
});

check("ties inside a tier stay newest-first", () => {
  const both = [
    meta("older.jsonl", "shared title", "x", 100),
    meta("newer.jsonl", "shared title", "x", 900),
  ];
  eq(names(offlineRank(both, "shared title")), ["newer.jsonl", "older.jsonl"]);
});

check("lcsRatio is 1 for identical strings and 0 against empty", () => {
  assert.strictEqual(lcsRatio("deploy", "deploy"), 1);
  assert.strictEqual(lcsRatio("deploy", ""), 0);
  assert.ok(lcsRatio("deploy", "deplyo") > 0.75, "a transposition should stay close");
  assert.ok(lcsRatio("deploy", "kitchen") < 0.5, "unrelated words should not be close");
});

check("a literal match is never diluted by a close one (#266)", () => {
  // "certificates" IS in session-a; session-b's "tiles" is one letter from
  // "tiles"/"tls"-shaped noise. Once anything matches literally, the closest
  // tier has nothing to add.
  eq(names(offlineRank(corpus, "certificates")), ["session-a.jsonl"]);
});

check("a short query does not match much shorter words (#266)", () => {
  // The bug in one line: difflib-style ratios score "tel" 0.75 against "tefal"
  // on length alone, and every archive holds a three-letter word.
  const rows = [meta("x.jsonl", "phone notes", "call me on the tel or over tea", 1)];
  eq(offlineRank(rows, "tefal"), []);
});

check("the closest fallback is capped", () => {
  const many = [];
  for (let i = 0; i < 15; i += 1) {
    many.push(meta(`s${i}.jsonl`, "restart the server", "restart the server", i));
  }
  assert.strictEqual(offlineRank(many, "restrat").length, 10);
});

check("the offline index holds the chat, not the tool output (#266)", () => {
  const text = offlineSearchText([
    { type: "user", text: "find me a sandwich toaster" },
    { type: "step", tool: "read_url", output: "Tefal SW852D" },
    { type: "done", result: "Here are three options." },
  ]);
  assert.ok(text.includes("sandwich toaster"), "the question is searchable");
  assert.ok(text.includes("three options"), "the answer is searchable");
  assert.ok(!text.includes("tefal"), "a page the model read is not the chat");
});

check("the legacy history blob is filtered by role too (#266)", () => {
  const text = offlineSearchText([
    {
      type: "history",
      messages: [
        { role: "user", content: "find me a sandwich toaster" },
        { role: "tool", content: "Tefal SW852D" },
      ],
    },
  ]);
  assert.ok(text.includes("sandwich toaster"));
  assert.ok(!text.includes("tefal"), "tool records must not enter the index");
});

check("a missing search index degrades to title-only, never throws", () => {
  // A meta row written before the text index existed (or trimmed away) must
  // still rank by title rather than crash the sheet.
  const partial = [{ name: "x.jsonl", title: "deploy", ts: 1 }];
  eq(names(offlineRank(partial, "deploy")), ["x.jsonl"]);
  eq(offlineRank(partial, "nginx"), []);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
