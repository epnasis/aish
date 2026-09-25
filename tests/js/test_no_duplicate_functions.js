// app.js is ONE classic-script scope, where a second top-level
// `function name()` does not fail — it silently replaces the first for every
// caller in the file. That is how the chat list's keyboard cursor once took
// the name `railRows` from the offline list builder: each block's own test
// passed, because each loaded its block alone. This pins the whole file.
//
// Run manually: node tests/js/test_no_duplicate_functions.js
"use strict";

const assert = require("assert");
const { appSource } = require("./harness");

const seen = new Map();
const duplicates = [];
appSource().split("\n").forEach((line, i) => {
  const m = /^(?:async\s+)?function\s*\*?\s*([A-Za-z0-9_$]+)\s*\(/.exec(line);
  if (!m) return;
  if (seen.has(m[1])) duplicates.push(`${m[1]} (lines ${seen.get(m[1])} and ${i + 1})`);
  else seen.set(m[1], i + 1);
});
assert.deepEqual(duplicates, [], `top-level functions declared twice in app.js: ${duplicates.join(", ")}`);
assert(seen.size > 100, "the scan found almost nothing — the pattern no longer matches app.js");
console.log(`no duplicate functions: ${seen.size} top-level functions, all unique`);
