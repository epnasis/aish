// Node-only, dependency-free check: attaching a file puts it WHERE YOU ARE
// TYPING ([EMBED-AT-CARET] in app.js, #233).
//
// Attachments used to join at the end of the message, always, because the end
// was the only place one could be. Now that a reference means the same thing
// anywhere, ＋ writes it under the cursor: you type "the error in ", attach the
// screenshot, and carry on with " and the fix looks like".
//
// THE CORRECTION THIS FILE EXISTS FOR (#234): "at the caret" must not swallow
// the ordinary case. Writing a question and then tapping ＋ leaves the caret at
// the END, which is how every attachment has ever been made — and the first
// version put the reference on that same line, so a plain photo came out
// rendered at text height instead of as the thumbnail that had always been
// there. The end of a message is not "inside the sentence"; it is the end, and
// it gets a block on its own line.
//
// What is pinned here:
//   - a caret at the END appends a BLOCK, on its own line (the old behaviour,
//     and the one almost every attachment takes);
//   - a caret genuinely INSIDE the text inserts inline, in place;
//   - inline never lands mid-word — a space is added on whichever side lacks
//     one — and does not double up spacing already there;
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

// 1. THE REGRESSION: type a question, tap ＋. The caret is at the end, and what
//    that has always meant is a photo on its own line — a real thumbnail, not a
//    picture shrunk to the height of the text around it.
{
  const w = world("what is on this terrace?");
  w.sandbox.insertEmbedAtCaret("taras.png");
  ok("a file attached at the end gets its own line",
    w.input.value === "what is on this terrace?\n![[taras.png]]");
}
{
  const w = world("what is on this terrace?   ");
  w.sandbox.insertEmbedAtCaret("taras.png");
  ok("…trailing whitespace is still the end",
    w.input.value === "what is on this terrace?\n![[taras.png]]");
}

// 2. THE FEATURE: a caret genuinely inside the text inserts in place.
{
  const w = world("the error in  and the fix is obvious", 13);
  w.sandbox.insertEmbedAtCaret("shot.png");
  ok("inserted at the caret",
    w.input.value === "the error in ![[shot.png]] and the fix is obvious");
}

// 3. Inline never lands mid-word: a space appears on the side that needs one.
{
  const w = world("look atplease", 7);
  w.sandbox.insertEmbedAtCaret("cat.png");
  ok("a space is added on both sides when the word is split",
    w.input.value === "look at ![[cat.png]] please");
}

// 4. Spacing already there is not doubled — the sentence must not gain gaps.
{
  const w = world("look at  please", 8);
  w.sandbox.insertEmbedAtCaret("cat.png");
  ok("existing spaces are left alone", w.input.value === "look at ![[cat.png]] please");
}

// 5. An empty composer gets nothing: the photo IS the message, and the server
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

// 6. The caret follows the insertion, so typing carries on where the sentence
//    does — not back at the point the file interrupted.
{
  const w = world("the error in  is here", 13);
  w.sandbox.insertEmbedAtCaret("shot.png");
  ok("the caret sits after what was inserted",
    w.input.value.slice(0, w.input.selectionStart) === "the error in ![[shot.png]]");
}

console.log(`test_embed_at_caret.js: ${checks} checks passed`);
