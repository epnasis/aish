// Node-only, dependency-free regression check for the console's OSC 52 clipboard
// decode (#153). With tmux `set-clipboard on`, a copy on the remote terminal
// emits OSC 52 (`ESC ] 52 ; c ; <base64> BEL`); the frontend decodes it and puts
// the text on the local clipboard. Pulls the REAL oscClipboardText() out of
// app.js by marker and checks decode, UTF-8, and the read-request / malformed
// guards.
//
// Run manually: node tests/js/test_osc52_clipboard.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);
const start = src.indexOf("// [OSC52-DECODE-START]");
const end = src.indexOf("// [OSC52-DECODE-END]");
assert(start !== -1 && end !== -1, "OSC52-DECODE markers not found in app.js");

// atob / escape / decodeURIComponent are browser globals the handler relies on;
// Node exposes all three, so hand them to the vm context.
const sandbox = { atob, escape, decodeURIComponent };
vm.createContext(sandbox);
vm.runInContext(src.slice(start, end), sandbox);
assert(typeof sandbox.oscClipboardText === "function", "oscClipboardText not extracted");

const b64 = (s) => Buffer.from(s, "utf8").toString("base64");

// ASCII round-trip with the usual "c" (clipboard) selection parameter.
assert.strictEqual(sandbox.oscClipboardText("c;" + b64("hello world")), "hello world");
// A multi-line selection (tmux copy of several rows).
assert.strictEqual(sandbox.oscClipboardText("c;" + b64("line1\nline2")), "line1\nline2");
// UTF-8 must survive (tasks arrive in Polish; terminals show unicode).
assert.strictEqual(sandbox.oscClipboardText("c;" + b64("zażółć 你好")), "zażółć 你好");
// Other selection params (primary, etc.) decode the same way.
assert.strictEqual(sandbox.oscClipboardText("p;" + b64("primary")), "primary");

// A clipboard-READ request ("?") must NOT be answered — never leak the clipboard.
assert.strictEqual(sandbox.oscClipboardText("c;?"), null);
// Malformed / empty payloads return null (never throw).
assert.strictEqual(sandbox.oscClipboardText("c;"), null);
assert.strictEqual(sandbox.oscClipboardText(""), null);
assert.strictEqual(sandbox.oscClipboardText("c;@@not base64@@"), null);

console.log("ok: oscClipboardText decodes clipboard sets and refuses read requests");
