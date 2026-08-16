// Node-only, dependency-free check: attaching a file puts it WHERE YOU ARE
// TYPING ([EMBED-AT-CARET] in app.js, #233).
//
// Attachments used to join at the end of the message, always, because the end
// was the only place one could be. Now that a reference means the same thing
// anywhere, ＋ writes it under the cursor: you type "the error in ", attach the
// screenshot, and carry on with " and the fix looks like".
//
// What is pinned here:
//   - the reference lands at the caret, not at the end;
//   - it never lands mid-word — a space is added on whichever side lacks one;
//   - it does not double up spacing that is already there;
//   - an EMPTY composer gets nothing, because a photo sent with no words is a
//     whole message by itself and the server appends the reference on send;
//   - the caret ends up AFTER what was inserted, so typing continues where the
//     sentence continues.
//
// Runs the REAL block from app.js (extracted by marker), so it tests the
// shipped code rather than a copy.
//
// Run manually: node tests/js/test_embed_at_caret.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8",
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

// `value` and `selectionStart` are all the block touches; the rest is spies.
function world(value, caret) {
  const input = {
    value,
    selectionStart: caret === undefined ? value.length : caret,
    setSelectionRange(from) { input.selectionStart = from; },
  };
  const sandbox = { input, resizeInput() {}, saveDraft() {} };
  vm.createContext(sandbox);
  vm.runInContext(
    extract("// [EMBED-AT-CARET-START]", "// [EMBED-AT-CARET-END]")
      .replace(/\bconst\b/g, "var"),
    sandbox,
  );
  return { sandbox, input };
}

// 1. THE POINT: the file lands under the cursor, not at the end.
{
  const w = world("the error in  and the fix is obvious", 13);
  w.sandbox.insertEmbedAtCaret("shot.png");
  ok("inserted at the caret",
    w.input.value === "the error in ![[shot.png]] and the fix is obvious");
}

// 2. Never mid-word: a space appears on the side that needs one.
{
  const w = world("look at", 7);
  w.sandbox.insertEmbedAtCaret("cat.png");
  ok("a space is added before", w.input.value === "look at ![[cat.png]]");
}
{
  const w = world("look atplease", 7);
  w.sandbox.insertEmbedAtCaret("cat.png");
  ok("…and after, when there is text on both sides",
    w.input.value === "look at ![[cat.png]] please");
}

// 3. Spacing already there is not doubled — the sentence must not gain gaps.
{
  const w = world("look at  please", 8);
  w.sandbox.insertEmbedAtCaret("cat.png");
  ok("existing spaces are left alone", w.input.value === "look at ![[cat.png]] please");
}

// 4. An empty composer gets nothing: the photo IS the message, and the server
//    appends the reference on send. Typing one in just to delete it is a step
//    nobody asked for.
{
  const w = world("");
  w.sandbox.insertEmbedAtCaret("cat.png");
  ok("nothing is written into an empty composer", w.input.value === "");
}
{
  const w = world("   \n ");
  w.sandbox.insertEmbedAtCaret("cat.png");
  ok("…nor into one holding only whitespace", w.input.value === "   \n ");
}

// 5. The caret follows the insertion, so typing carries on where the sentence
//    does — not back at the point the file interrupted.
{
  const w = world("the error in  is here", 13);
  w.sandbox.insertEmbedAtCaret("shot.png");
  ok("the caret sits after what was inserted",
    w.input.value.slice(0, w.input.selectionStart) === "the error in ![[shot.png]]");
}

console.log(`test_embed_at_caret.js: ${checks} checks passed`);
