// Node-only, dependency-free regression check for issue #80 (quick-reply
// chips rendering as raw markdown text).
//
// There is no JS test runner in this project (aish/static/ ships vanilla JS,
// no build step) and app.js can't be `require`d directly — it touches
// `document`/`WebSocket` at module scope. So this pulls the specific,
// DOM-free parsing primitives (the fence tracker and the inline-syntax
// regex) straight out of the real source file by marker and evaluates just
// that slice in an isolated `vm` context — exercising the shipped
// implementation itself, not a hand-copied duplicate that could drift from
// it.
//
// Run manually: node tests/js/test_quick_reply_render.js
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

const sandbox = {};
vm.createContext(sandbox);
// vm contexts don't reflect top-level `const`/`let` as properties of the
// sandbox object (only `var` does) — swap to `var` so the extracted
// bindings are actually reachable below. Safe here: this snippet is just
// declarations, none of them rely on block scoping.
const snippet = (
  extract("const FENCE_RE", "function stableBoundary") +
  extract("const INLINE_RE", "// Images (#9)")
).replace(/\bconst\b/g, "var");
vm.runInContext(snippet, sandbox);
const { fenceOpen, fenceCloses, INLINE_RE } = sandbox;
assert(fenceOpen && fenceCloses && INLINE_RE, "failed to extract parsing primitives from app.js");

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}`);
    console.error(`       ${err.message}`);
  }
}

// --- cause #1: nested/mismatched code fences must not desync fence state ---

check("a longer outer fence survives a same-length nested fence", () => {
  // The model showing a fenced example THAT ITSELF contains a fenced block
  // must use a longer (or different-char) outer delimiter per CommonMark;
  // when it does, the inner ``` must not be mistaken for the outer close.
  const outer = fenceOpen("````markdown");
  assert(outer, "```` should open a fence");
  assert.strictEqual(fenceCloses("```", outer), false, "a shorter ``` must not close a ```` fence");
  assert.strictEqual(fenceCloses("````", outer), true, "```` closes a ```` fence");
});

check("a same-length inner fence closes only its own (single-level) block", () => {
  // Well-formed single-level fences (the overwhelmingly common case) still
  // close on the very next matching marker.
  const f = fenceOpen("```");
  assert(f);
  assert.strictEqual(fenceCloses("```", f), true);
});

check("a bare fence after a closed block doesn't retroactively reopen", () => {
  const f = fenceOpen("```js");
  assert(f);
  assert.strictEqual(fenceCloses("```", f), true);
  // Once closed, a later stray ``` is a NEW open, evaluated independently —
  // fenceOpen/fenceCloses have no notion of "already closed", that's the
  // caller's (renderMarkdown's) loop state, so this just checks the fence
  // descriptor itself carries the right character/length for later reuse.
  const reopened = fenceOpen("```");
  assert(reopened && reopened.ch === "`" && reopened.len === 3);
});

check("tilde fences are recognized (not just backticks)", () => {
  const f = fenceOpen("~~~~");
  assert(f && f.ch === "~" && f.len === 4);
  assert.strictEqual(fenceCloses("```", f), false, "different fence char must not close it");
  assert.strictEqual(fenceCloses("~~~~", f), true);
});

check("a hyphenated info string is captured whole (aish-issue block, #110)", () => {
  // \w* would stop at the hyphen and leave "-issue" trailing, so the fence
  // wouldn't match at all and the feedback card would render as raw text.
  const f = fenceOpen("```aish-issue");
  assert(f, "```aish-issue should open a fence");
  assert.strictEqual(f.lang, "aish-issue");
});

// --- cause #2/#3: quick-reply label/payload robustness -------------------

check("a single-line quick reply matches with label and payload captured", () => {
  INLINE_RE.lastIndex = 0;
  const m = "[Yes please](aish-reply://yes)".match(INLINE_RE);
  assert(m, "expected a match");
  assert.strictEqual(m[7], "Yes please");
  assert.strictEqual(m[8], "yes");
});

check("two consecutive quick-reply lines each match independently", () => {
  // Simulates two chip-only lines merged into one paragraph blob (no blank
  // line between them) — the scenario the paragraph-grouping cause (#3)
  // describes. The \n exclusion in the label/payload groups must stop a
  // greedy match on line 1 from bleeding into line 2's syntax.
  const text = "[Yes](aish-reply://yes)\n[No](aish-reply://no)";
  let rest = text;
  const found = [];
  for (let guard = 0; rest && guard < 10; guard++) {
    const m = rest.match(INLINE_RE);
    if (!m) break;
    if (m[7] !== undefined) found.push([m[7], m[8]]);
    rest = rest.slice(m.index + m[0].length);
  }
  assert.deepStrictEqual(found, [["Yes", "yes"], ["No", "no"]]);
});

check("an unmatched '[' earlier in the blob doesn't swallow a later real chip", () => {
  // A stray "[" with no closing "]" on its own line, followed by a genuine
  // quick reply on the next line — the label group must not cross the "\n"
  // hunting for a "]" three lines away.
  const text = "See [reference\n[Yes](aish-reply://yes)";
  const m = text.match(INLINE_RE);
  assert(m, "expected the real chip to still match");
  assert.strictEqual(m[7], "Yes");
  assert.strictEqual(m[8], "yes");
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
