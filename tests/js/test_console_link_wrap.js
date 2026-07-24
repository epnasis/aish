// Node-only, dependency-free regression check for the terminal (Quake console)
// URL detection: a long OAuth/gcloud login URL, indented and soft-wrapped across
// several rows, must resolve to ONE clickable link spanning the whole URL.
//
// The vendored xterm web-links addon shipped an over-strict validation — a
// decodeURI(new URL(x).toString()) round-trip — that rejected any URL whose
// query held percent-encoded chars (e.g. the `%20` scope separators in a Google
// auth URL: scope=…/drive%20…/gmail.modify). decodeURI turned `%20` into a space,
// the round-trip no longer matched the source, and the link was dropped. We
// relaxed that check to plain parseability (see the `aish#153` marker in
// aish/static/vendor/xterm-addon-web-links.js). This exercises the REAL shipped
// addon against a fake wrapped buffer, so a re-vendor that loses the patch fails.
//
// Run manually: node tests/js/test_console_link_wrap.js
"use strict";

const fs = require("fs");
const path = require("path");
const assert = require("assert");

// The addon is a UMD bundle whose outer call passes `self`; give it one, then
// take the CommonJS branch by handing it an exports/module pair.
globalThis.self = globalThis;
const addonPath = path.join(
  __dirname, "..", "..", "aish", "static", "vendor", "xterm-addon-web-links.js"
);
const code = fs.readFileSync(addonPath, "utf8");
const mod = { exports: {} };
new Function("exports", "module", code)(mod.exports, mod);
const { WebLinksAddon } = mod.exports;
assert(typeof WebLinksAddon === "function", "WebLinksAddon not exported by the vendored addon");

// A realistic auth URL: literal https inside the scope value, `%20` separators.
const url =
  "https://accounts.google.com/o/oauth2/auth?response_type=code" +
  "&client_id=764086051850-abc.apps.googleusercontent.com" +
  "&redirect_uri=urn:ietf:wg:oauth:2.0:oob" +
  "&scope=https://www.googleapis.com/auth/drive%20" +
  "https://www.googleapis.com/auth/gmail.modify%20" +
  "https://www.googleapis.com/auth/cloud-platform" +
  "&state=xyz&access_type=offline";

// gcloud prints it indented; a narrow mobile terminal soft-wraps it. Row 0 is
// the logical line (isWrapped=false); continuation rows carry isWrapped=true.
const first = "    " + url;
const COLS = 60;
const START = 5; // arbitrary buffer offset
const rows = [];
for (let i = 0; i < first.length; i += COLS) rows.push(first.slice(i, i + COLS));
assert(rows.length >= 5, "test URL should wrap across several rows");

const lines = {};
rows.forEach((text, k) => { lines[START + k] = { text, wrapped: k > 0 }; });

// Minimal xterm buffer: getLine + a reusable null cell that getCell fills. All
// URL chars are ASCII (single width), which is all _mapStrIdx needs here.
const nullCell = { _ch: "", getChars() { return this._ch; }, getWidth() { return 1; } };
const buffer = { active: {
  getNullCell: () => nullCell,
  getLine: (y) => {
    const t = lines[y];
    if (t == null) return null;
    return {
      isWrapped: t.wrapped,
      length: t.text.length,
      translateToString: () => t.text,
      getCell: (n, out) => {
        const c = out || nullCell;
        c._ch = n < t.text.length ? t.text[n] : "";
        return c;
      },
    };
  },
} };

let provider = null;
const term = { buffer, registerLinkProvider: (p) => { provider = p; return { dispose() {} }; } };
let opened = null;
new WebLinksAddon((_ev, uri) => { opened = uri; }).activate(term);
assert(provider, "addon did not register a link provider");

// Click a MIDDLE wrapped row — the worst case for backward+forward reconstruction.
const clicked1Based = START + 3 + 1; // provideLinks takes a 1-based buffer row
let links = [];
provider.provideLinks(clicked1Based, (l) => { links = l || []; });

assert.strictEqual(links.length, 1, `expected exactly one link, got ${links.length}`);
assert.strictEqual(links[0].text, url, "link text is not the full, un-truncated URL");
links[0].activate(null, links[0].text);
assert.strictEqual(opened, url, "activating the link did not open the full URL");

console.log("ok: wrapped indented auth URL resolves to one full clickable link");
