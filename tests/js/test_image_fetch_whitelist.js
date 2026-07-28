// Node-only, dependency-free check for #178 P1-12 (remote images in model
// markdown are a rendering-layer egress). Pulls the real imageFetchAllowed
// out of app.js by the [INLINEIMG] markers and asserts the render-time fetch
// policy: only the CSP_IMG_HOSTS whitelist (server.py) may be fetched;
// same-origin /file paths and data: URIs stay allowed; everything else —
// notably an attacker host carrying exfil in its query string — is refused.
//
// Run manually: node tests/js/test_image_fetch_whitelist.js
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

// URL is a Node main-realm global, not a JS intrinsic — hand it to the vm.
const sandbox = { URL };
vm.createContext(sandbox);
vm.runInContext(
  extract("// [INLINEIMG-START]", "// [INLINEIMG-END]").replace(/\bconst\b/g, "var"),
  sandbox,
);
const { imageFetchAllowed, IMG_FETCH_HOSTS } = sandbox;
assert(typeof imageFetchAllowed === "function", "failed to extract imageFetchAllowed from app.js");
assert(Array.isArray(IMG_FETCH_HOSTS), "failed to extract IMG_FETCH_HOSTS from app.js");

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

check("an attacker host is never fetched (zero-click exfil channel)", () => {
  assert.strictEqual(imageFetchAllowed("https://attacker.com/x.png?secret=hunter2"), false);
  assert.strictEqual(imageFetchAllowed("http://attacker.com/x.png"), false);
  assert.strictEqual(imageFetchAllowed("https://evil.example/pixel.gif"), false);
});

check("a lookalike of a whitelisted host is refused (match is the WHOLE host)", () => {
  assert.strictEqual(imageFetchAllowed("https://img.youtube.com.evil.com/x.jpg"), false);
  assert.strictEqual(imageFetchAllowed("https://notmaps.googleapis.com/x.png"), false);
  assert.strictEqual(imageFetchAllowed("https://evil.com/img.youtube.com/x.jpg"), false);
});

check("YouTube thumbnail hosts are allowed", () => {
  assert.strictEqual(imageFetchAllowed("https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg"), true);
  assert.strictEqual(imageFetchAllowed("https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"), true);
});

check("Google static maps host is allowed", () => {
  assert.strictEqual(
    imageFetchAllowed("https://maps.googleapis.com/maps/api/staticmap?size=640x400"),
    true,
  );
});

check("same-origin /file paths and data: URIs stay allowed", () => {
  assert.strictEqual(imageFetchAllowed("/file?path=%2Ftmp%2Fa.png&token=t"), true);
  assert.strictEqual(imageFetchAllowed("data:image/png;base64,iVBORw0KGgo="), true);
});

check("host match is case-insensitive; garbage URLs are refused", () => {
  assert.strictEqual(imageFetchAllowed("https://IMG.YOUTUBE.COM/vi/x/hq.jpg"), true);
  assert.strictEqual(imageFetchAllowed("https://%zz%/x.png"), false);
});

check("whitelist mirrors server.py CSP_IMG_HOSTS", () => {
  const serverPy = fs.readFileSync(
    path.join(__dirname, "..", "..", "aish", "server.py"),
    "utf8",
  );
  const m = serverPy.match(/^CSP_IMG_HOSTS = "([^"]+)"/m);
  assert(m, "CSP_IMG_HOSTS not found in server.py");
  const cspHosts = m[1].split(/\s+/).map((u) => new URL(u).hostname).sort();
  assert.deepStrictEqual([...IMG_FETCH_HOSTS].sort(), cspHosts);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
