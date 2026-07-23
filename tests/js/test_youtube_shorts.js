// Node-only, dependency-free check for issue #149 (YouTube Shorts links must
// render as the existing YouTube embed). Pulls the real YOUTUBE_RE out of
// app.js by marker and asserts it matches all three URL shapes, keeping the
// group(1) || group(2) id read in lockstep with export.py's _YOUTUBE_RE.
//
// Run manually: node tests/js/test_youtube_shorts.js
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
// Grab just the regex declaration (up to the following function).
vm.runInContext(
  extract("const YOUTUBE_RE", "function ").replace(/\bconst\b/g, "var"),
  sandbox,
);
const { YOUTUBE_RE } = sandbox;
// instanceof would be cross-realm here (the vm has its own RegExp), so check
// for a regex by shape instead.
assert(YOUTUBE_RE && typeof YOUTUBE_RE.test === "function", "failed to extract YOUTUBE_RE from app.js");

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

const VID = "dQw4w9WgXcQ";

check("a Shorts URL matches and captures the 11-char id", () => {
  const m = `https://www.youtube.com/shorts/${VID}`.match(YOUTUBE_RE);
  assert(m, "Shorts URL should match");
  assert.strictEqual(m[1] || m[2], VID);
});

check("Shorts with a trailing query still matches", () => {
  const m = `https://www.youtube.com/shorts/${VID}?feature=share`.match(YOUTUBE_RE);
  assert(m, "Shorts URL with query should match");
  assert.strictEqual(m[1] || m[2], VID);
});

check("watch and youtu.be shapes still match (no regression)", () => {
  const watch = `https://www.youtube.com/watch?v=${VID}`.match(YOUTUBE_RE);
  assert(watch && (watch[1] || watch[2]) === VID);
  const shortHost = `https://youtu.be/${VID}`.match(YOUTUBE_RE);
  assert(shortHost && (shortHost[1] || shortHost[2]) === VID);
});

check("a non-video youtube path does not match", () => {
  assert(!"https://www.youtube.com/feed/subscriptions".match(YOUTUBE_RE));
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
