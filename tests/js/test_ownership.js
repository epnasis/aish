// The ownership lint (issue #181, pillar 2).
//
// The client's defect class is "several writers, correctness resting on an
// ordering nobody owned". The repairs that stopped producing bugs all did the
// same thing: gave one piece of shared state a single fenced writer. That only
// stays true if it is ENFORCED rather than remembered — the next well-meaning
// fix (human or model) reaches for the variable, not for the owner, and nothing
// today says no until a phone shows it.
//
// So: a manifest of owned variables → the fenced region(s) allowed to assign
// them, checked against the shipped app.js. Adding an owned variable is ONE
// manifest line, and the failure NAMES THE OWNER to call instead of assigning.
//
// Deliberately NOT every variable: over-fencing turns the lint into noise. Only
// the invariants the user can see, growing island by island (#181's phases).
//
// Run manually: node tests/js/test_ownership.js
"use strict";

const assert = require("assert");
const { appSource, checks } = require("./harness");

const { ok, report } = checks();

// ---- the manifest --------------------------------------------------------
// owners:  fence names; an assignment must sit inside one of `// [NAME-START]`
//          … `// [NAME-END]`. Several owners are allowed when the state has a
//          genuine second half (creation vs disposal, live vs reconciliation).
// instead: what a new writer should CALL. This is the whole point of the lint —
//          it must tell you where to go, not merely that you are wrong.
// array:   also flag in-place mutation (push/splice/…), which assignment
//          detection alone would miss.
const OWNED = {
  ws: {
    owners: ["CONNECT-WIRE", "RETIRE"],
    instead: "call connect() (and let retireSocket() dispose the predecessor)",
    why: "a socket assigned without disposing the one it replaces double-feeds the dispatcher (#179)",
  },
  currentSession: {
    owners: ["SESSION-ENTER"],
    instead: "call enterSession(name, {source, title, stash})",
    why: "four hand-rolled variants each wrote a DIFFERENT subset of the facts coupled to identity"
      + " (fingerprint, title, remembered session, URL, mirror bookkeeping), so a switch left the"
      + " screen and the state naming different chats",
  },
  seenAt: {
    owners: ["SEEN"],
    instead: "call markSeen(name) / forgetSeen(name)",
    why: "the seen map is what makes a chat unread, which is what puts it in the rail's"
      + " attention band; a stray writer either hides activity or flags read chats forever",
  },
  viewFp: {
    owners: ["SESSION-ENTER", "REPLAY-LANDING"],
    instead: "call enterSession() (a switch) or let onReplay stamp the landing",
    why: "the fingerprint decides whether an incoming replay repaints, reuses stashed"
      + " nodes, or is a no-op; a stray writer either keeps the PREVIOUS chat's DOM"
      + " or rebuilds the world on every phone unlock",
  },
  viewDirty: {
    owners: ["SESSION-ENTER", "CONNECT-WIRE", "REPLAY-LANDING"],
    instead: "let the socket dispatch mark it (VIEW_SAFE_EVENTS) and onReplay clear it",
    why: "it is the ONLY thing standing between a live event and a stale stashed view"
      + " being served as if it were the transcript",
  },
  answerEl: {
    owners: ["TURN-RESET", "ANSWER-OPEN", "ANSWER-CLOSE"],
    instead: "call closeAnswer() (orderly) or resetLiveTurn(landing) (the transcript is going)",
    why: "three regions, one per lifecycle stage — opened by the token path, closed by"
      + " closeAnswer, abandoned by a replay that lands mid-stream; a fourth writer is"
      + " how a turn ended up with two answer bubbles (#181 phase 3)",
  },
  currentTrace: {
    owners: ["TRACE-OPEN", "TRACE-CLOSE"],
    instead: "call ensureTrace() / finishTrace()",
    why: "a live trace left un-closed by a session switch kept receiving the NEXT chat's"
      + " steps, and its interval timer ran forever",
  },
  // NOT here: answerAbandoned (the flag resetLiveTurn sets when it throws away a
  // half-streamed bubble). Its writers are the owner plus the two turn boundaries
  // that already reset sawAnswer — `user` and `done` — and fencing either of those
  // whole handlers would claim an ownership the file does not have. It rides with
  // sawAnswer by construction; the pin that matters is the behavioural one in
  // test_choreo_midstream_replay.js.
};

// ---- the checker ---------------------------------------------------------
// A lint over our own file, so it favours an exact manifest over parser
// sophistication: line-scan, strip comments, skip the declaration.

/** Line ranges (1-based, inclusive) for each `// [NAME-START]`…`// [NAME-END]`. */
function regions(lines) {
  const found = {};
  const open = {};
  lines.forEach((text, i) => {
    const start = text.match(/^\s*\/\/ \[([A-Z0-9-]+)-START\]\s*$/);
    if (start) { open[start[1]] = i + 1; return; }
    const end = text.match(/^\s*\/\/ \[([A-Z0-9-]+)-END\]\s*$/);
    if (end && open[end[1]]) {
      (found[end[1]] ||= []).push([open[end[1]], i + 1]);
      delete open[end[1]];
    }
  });
  return found;
}

/** Drop comments so a variable NAMED in prose is never mistaken for a writer.
 *  `[^:]` before `//` keeps `https://` intact — enough for our own source. */
function code(text) {
  return text.replace(/(^|[^:])\/\/.*$/, "$1");
}

function patterns(name, spec) {
  // Not preceded by `.` or an identifier char: `foo.ws = 1` is a property,
  // not this variable. Not followed by `=` or `>`: `===`, `!==`, `=>`.
  const bare = String.raw`(?<![.\w$])${name}\s*(?:[-+*/%|&^?]{1,2})?=(?![=>])`;
  // `({ members: deck, … } = f())` — a destructuring write, which the bare
  // pattern cannot see. Coarse on purpose: any `{…} =` naming the variable.
  const destructured = String.raw`\{[^}]*(?<![.\w$])${name}\b[^}]*\}\s*=(?![=>])`;
  const list = [new RegExp(bare), new RegExp(destructured)];
  if (spec.array) list.push(new RegExp(String.raw`(?<![.\w$])${name}\.(?:push|pop|shift|unshift|splice|sort|reverse|fill|length\s*=)\s*\(?`));
  return list;
}

const src = appSource();
const lines = src.split("\n");
const found = regions(lines);

function scan(name, spec) {
  const tests = patterns(name, spec);
  const declaration = new RegExp(String.raw`^\s*(?:const|let|var)\s+${name}\b`);
  const hits = [];
  let declarations = 0;
  lines.forEach((raw, i) => {
    const text = code(raw);
    if (declaration.test(text)) { declarations += 1; return; } // where it comes into being, not a writer
    if (tests.some((re) => re.test(text))) hits.push({ line: i + 1, text: raw });
  });
  return { hits, declarations };
}

function inside(line, spec) {
  return spec.owners.some((owner) =>
    (found[owner] || []).some(([from, to]) => line >= from && line <= to));
}

function violation(name, spec, hit) {
  const owners = spec.owners.map((o) => `[${o}]`).join(" or ");
  return `app.js:${hit.line}: ${name} is owned by ${owners}; `
    + `${spec.instead} instead of assigning it directly.\n`
    + `    ${hit.text.trim()}\n`
    + `    why: ${spec.why}\n`
    + `    (if the owner genuinely moved, update the manifest in tests/js/test_ownership.js)`;
}

// Collected across the whole manifest, not thrown at the first hit: someone who
// moved a block wants to see every stray it produced, once.
const violations = [];

for (const [name, spec] of Object.entries(OWNED)) {
  // A manifest entry naming a fence that does not exist is a stale manifest —
  // and would silently pass everything, so it must fail loudly.
  for (const owner of spec.owners) {
    assert(found[owner] && found[owner].length,
      `manifest names [${owner}] as an owner of ${name}, but app.js has no such fenced block`);
  }
  const { hits, declarations } = scan(name, spec);
  assert.strictEqual(declarations, 1,
    `${name} must be declared exactly once at module scope (found ${declarations})`);

  for (const h of hits.filter((x) => !inside(x.line, spec))) {
    violations.push(violation(name, spec, h));
  }
  // A manifest entry that matches nothing is a typo dressed as a guarantee.
  assert(hits.length > 0, `${name} has no writers at all — is the manifest name right?`);
  ok(`${name}: ${hits.length} writer(s), owned by ${spec.owners.map((o) => `[${o}]`).join(" + ")}`,
    true);
}

assert.strictEqual(violations.length, 0,
  `\n\n${violations.join("\n\n")}\n\n${violations.length} ownership violation(s)`);

// ---- the lint must be able to fail --------------------------------------
// A checker that cannot report a violation is worse than none: it reads as a
// guarantee. Run the detector over a fabricated source with a stray writer.
{
  const fake = [
    "// [DEMO-START]",
    "let owned = 0;",
    "function setOwned(v) { owned = v; }",
    "// [DEMO-END]",
    "function elsewhere() { owned = 9; }        // the violation",
    "function innocent() { return owned === 9; } // a read, not a write",
    "// owned = 3 in a comment is not a writer",
  ];
  const spec = { owners: ["DEMO"], instead: "call setOwned()", why: "demo" };
  const demoRegions = regions(fake);
  const tests = patterns("owned", spec);
  const strays = [];
  fake.forEach((raw, i) => {
    const text = code(raw);
    if (/^\s*(?:const|let|var)\s+owned\b/.test(text)) return;
    if (!tests.some((re) => re.test(text))) return;
    const line = i + 1;
    const ownedHere = demoRegions.DEMO.some(([a, b]) => line >= a && line <= b);
    if (!ownedHere) strays.push({ line, text: raw });
  });
  ok("the lint detects a stray writer outside the fence", strays.length === 1);
  ok("…and only that one (a read and a comment are not writes)", strays[0].line === 5);
  const message = violation("owned", spec, strays[0]);
  ok("the failure names the owner to call instead",
    message.includes("[DEMO]") && message.includes("call setOwned()"));
}

report("test_ownership.js");
