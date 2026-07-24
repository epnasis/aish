// Node-only, dependency-free regression check for the console terminal URL
// detection (#153). Pulls the REAL consoleLinkProvider() out of app.js by marker
// and runs it in a vm against a fake xterm buffer — exercising the shipped code.
//
// The console runs inside tmux, which repaints its pane with absolute cursor
// moves, so a login URL that wraps arrives as SEPARATE rows NOT flagged
// isWrapped. xterm's stock web-links addon only rejoins isWrapped rows, so those
// URLs went undetected (or resolved to a truncated single-row fragment). Our
// provider joins rows by geometry — a row packed to the last column is treated
// as continuing — so it must reconstruct the FULL URL from non-wrapped rows too.
//
// Run manually: node tests/js/test_console_links.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);
const start = src.indexOf("// [CONSOLE-LINKS-START]");
const end = src.indexOf("// [CONSOLE-LINKS-END]");
assert(start !== -1 && end !== -1, "CONSOLE-LINKS markers not found in app.js");
const sandbox = { URL };
vm.createContext(sandbox);
vm.runInContext(src.slice(start, end), sandbox);
assert(typeof sandbox.consoleLinkProvider === "function", "consoleLinkProvider not extracted");

// Build a fake xterm buffer from an array of {text, wrapped} rows at offset y=5.
function fakeTerm(rows, cols, startY = 5) {
  const lines = {};
  rows.forEach((r, k) => { lines[startY + k] = r; });
  return {
    cols,
    buffer: { active: { getLine: (y) => {
      const r = lines[y];
      if (r == null) return null;
      return { isWrapped: !!r.wrapped, translateToString: () => r.text };
    } } },
  };
}

// Slice a string into rows of `cols`, optionally with leading text on row 0.
// wrapped=true tags continuation rows isWrapped (soft-wrap); false = tmux style.
function wrapRows(text, cols, wrapped) {
  const rows = [];
  for (let i = 0; i < text.length; i += cols) {
    rows.push({ text: text.slice(i, i + cols), wrapped: wrapped && i > 0 });
  }
  return rows;
}

function collect(term, clickedRow1Based, onOpen) {
  const provider = sandbox.consoleLinkProvider(term, onOpen || (() => {}));
  let links;
  provider.provideLinks(clickedRow1Based, (l) => { links = l; });
  return links || [];
}

const url =
  "https://accounts.google.com/o/oauth2/auth?scope=https://www.googleapis.com/auth/drive%20" +
  "https://www.googleapis.com/auth/gmail.modify%20https://www.googleapis.com/auth/calendar%20" +
  "https://www.googleapis.com/auth/cloud-platform%20openid%20" +
  "https://www.googleapis.com/auth/userinfo.email&access_type=offline" +
  "&redirect_uri=http://localhost:61952&response_type=code" +
  "&client_id=350577600993-nicvsdgq2ve9kqes36d4lfjo4adiigu2.apps.googleusercontent.com" +
  "&prompt=select_account+consent";
const COLS = 58, START = 5;

// 1) tmux-style: rows NOT marked wrapped — the reported failure.
{
  const rows = wrapRows(url, COLS, false);
  assert(rows.length >= 6, "URL should span several rows");
  const clicked = START + Math.floor(rows.length / 2) + 1; // a middle row
  let opened = null;
  const links = collect(fakeTerm(rows, COLS), clicked, (e, u) => { opened = u; });
  assert.strictEqual(links.length, 1, `tmux-style: expected 1 link, got ${links.length}`);
  assert.strictEqual(links[0].text, url, "tmux-style: link is not the full URL");
  assert.strictEqual(links[0].range.start.y, START + 1, "tmux-style: wrong start row");
  assert.strictEqual(links[0].range.start.x, 1, "tmux-style: URL should start at column 1");
  links[0].activate(null, links[0].text);
  assert.strictEqual(opened, url, "tmux-style: activate did not open the full URL");
}

// 2) native soft-wrap: rows flagged isWrapped — must still work.
{
  const rows = wrapRows(url, COLS, true);
  const clicked = START + Math.floor(rows.length / 2) + 1;
  const links = collect(fakeTerm(rows, COLS), clicked);
  assert.strictEqual(links.length, 1, "soft-wrap: expected 1 link");
  assert.strictEqual(links[0].text, url, "soft-wrap: link is not the full URL");
}

// 3) leading text on the first row: link text excludes the prefix, coords shift.
{
  const prefix = "Visit: ";
  const rows = wrapRows(prefix + url, COLS, false);
  const clicked = START + 1; // the first row, which holds the prefix
  const links = collect(fakeTerm(rows, COLS), clicked);
  assert.strictEqual(links.length, 1, "prefixed: expected 1 link");
  assert.strictEqual(links[0].text, url, "prefixed: link should be the URL only");
  assert.strictEqual(links[0].range.start.x, prefix.length + 1, "prefixed: wrong start column");
}

// 4) single short URL on one row.
{
  const one = "https://example.com/path?a=1";
  const rows = [{ text: one, wrapped: false }];
  const links = collect(fakeTerm(rows, COLS), START + 1);
  assert.strictEqual(links.length, 1, "single-row: expected 1 link");
  assert.strictEqual(links[0].text, one, "single-row: wrong text");
}

// 5) plain prose, no URL — no links, no crash (even when rows are full width).
{
  const prose = "the quick brown fox jumps over the lazy dog again and again ";
  const rows = wrapRows(prose.repeat(3), COLS, false);
  const links = collect(fakeTerm(rows, COLS), START + 1);
  assert.strictEqual(links.length, 0, "prose: expected no links");
}

console.log("ok: consoleLinkProvider detects wrapped (tmux + soft) URLs and maps coords");
