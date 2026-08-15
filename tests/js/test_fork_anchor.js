// Node-only, dependency-free check: the Fork chip NAMES the answer it branches
// from ([FORK-ANCHOR] in app.js).
//
// The defect, reported with two forks to prove it: Fork was tapped on the
// twentieth answer of a long chat and both forks began from the sixth. Fourteen
// answers of context, and the photos in them, silently did not come along.
//
// The cause was that the fork point was a POSITION counted by the browser as it
// rendered — `++renderedAnswers` — and counted AGAIN by the server over the
// whole log on disk. Those two agree only if the browser rendered every answer,
// and it had not: the replay is capped ([BACKFILL]), so the view began at the
// log's fifteenth answer and called it the first. 20 tapped, 6 sent, 6 cut.
//
// What is pinned here:
//   - an answer that carries an id forks BY THAT ID, never by its ordinal;
//   - the ordinal survives only where there is no id at all (a pre-trace log
//     replayed as a flat blob) — the fallback must not rot;
//   - two answers rendered in the same view send their own ids, not a shared
//     one, which is the shape of the bug being fixed;
//   - forking is still refused while the chat is working.
//
// Runs the REAL block from app.js (extracted by marker) against a minimal fake
// DOM, so it tests the shipped code rather than a copy.
//
// Run manually: node tests/js/test_fork_anchor.js
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

function fakeElement(tag) {
  const el = {
    tagName: tag,
    className: "",
    title: "",
    type: "",
    textContent: "",
    attrs: {},
    children: [],
    setAttribute(k, v) { el.attrs[k] = v; },
    append(...kids) { el.children.push(...kids); },
    appendChild(kid) { el.children.push(kid); return kid; },
  };
  return el;
}

function world({ busy = false } = {}) {
  const sent = [];
  const toasts = [];
  const sandbox = {
    document: { createElement: fakeElement },
    clientBusy: busy,
    forkIcon: () => fakeElement("svg"),
    showToast: (text) => toasts.push(text),
    act: (m) => { sent.push(m); return true; },
  };
  vm.createContext(sandbox);
  // vm contexts don't expose top-level const/let, so the extracted declarations
  // are switched to var — plain declarations, no block-scoping dependency.
  vm.runInContext(
    extract("// [FORK-ANCHOR-START]", "// [FORK-ANCHOR-END]").replace(/\bconst\b/g, "var"),
    sandbox,
  );
  return { sandbox, sent, toasts };
}

// 1. THE REGRESSION: an answer with an id forks by that id, and the ordinal —
//    which is what was wrong — is not sent at all.
{
  const w = world();
  w.sandbox.forkChip(6, "a1b2c3d4e5f6").onclick();
  ok("one fork was requested", w.sent.length === 1);
  ok("it names the answer", w.sent[0].answer === "a1b2c3d4e5f6");
  ok("and sends no ordinal to be miscounted", w.sent[0].after === undefined);
  ok("it is still a fork", w.sent[0].type === "fork");
}

// 2. The ordinal is the fallback, and only that: a transcript with no ids (a
//    pre-trace log, replayed as one flat history blob) still forks.
{
  const w = world();
  w.sandbox.forkChip(3).onclick();
  ok("no id → the ordinal is sent", w.sent[0].after === 3);
  ok("…and nothing claims to name an answer", w.sent[0].answer === undefined);
}

// 3. Two answers in one view carry their OWN ids. This is the exact shape the
//    ordinal got wrong: two chips, one view, and what distinguishes them must
//    come from the record rather than from a counter the client keeps.
{
  const w = world();
  w.sandbox.forkChip(1, "first-answer").onclick();
  w.sandbox.forkChip(2, "second-answer").onclick();
  ok("each chip forks from its own answer",
    w.sent[0].answer === "first-answer" && w.sent[1].answer === "second-answer");
}

// 4. Forking mid-task would snapshot a half-written turn — still refused, and
//    the refusal is local, so nothing reaches the socket.
{
  const w = world({ busy: true });
  w.sandbox.forkChip(1, "an-answer").onclick();
  ok("nothing is sent while working", w.sent.length === 0);
  ok("and the reader is told why", /can't fork while working/.test(w.toasts[0] || ""));
}

console.log(`test_fork_anchor.js: ${checks} checks passed`);
