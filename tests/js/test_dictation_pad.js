// Node-only, dependency-free check for the console dictation scratchpad's
// history + send logic. The pad exists because iOS dictation typed straight at
// the PTY concatenates every interim revision; text is staged in a textarea and
// only written to the PTY on Send. Two behaviours are worth pinning:
//
//   1. padHistory/padHistoryPush — the last-10 ring that lets you re-send a
//      line that landed in the wrong window: newest first, no duplicates, hard
//      cap, and a corrupt/absent store degrades to an empty list (never throws
//      into the send path).
//   2. padSend — sends the text with a trailing CR (a scratchpad line is a
//      terminal line), sends WITHOUT one when asked, records history, trims
//      trailing whitespace, and refuses to send an empty pad.
//
// The real code is pulled out of app.js by marker/eval and run against a
// minimal fake DOM — the shipped logic, never a copy.
//
// Run manually: node tests/js/test_dictation_pad.js
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
  assert(start !== -1 && end !== -1, `markers not found: ${startMarker}`);
  return src.slice(start, end + endMarker.length);
}

// ---- fake DOM / storage ---------------------------------------------------
let store = {};
const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};

const padInput = { value: "" };
let sent = [];
let toasts = [];
let closed = 0;

const sandbox = {
  localStorage,
  $: (id) => {
    assert.strictEqual(id, "pad-input");
    return padInput;
  },
  consoleSend: (data) => sent.push(data),
  showToast: (text) => toasts.push(text),
  closeConsolePad: () => { closed++; },
};
vm.createContext(sandbox);
vm.runInContext(
  extract("// [PAD-HISTORY-START]", "// [PAD-HISTORY-END]") + "\n" +
  extract("// [PAD-SEND-START]", "// [PAD-SEND-END]") + "\n" +
  "this.padHistory = padHistory; this.padHistoryPush = padHistoryPush;" +
  "this.padSend = padSend; this.PAD_HISTORY_MAX = PAD_HISTORY_MAX;",
  sandbox
);
const { padHistory, padHistoryPush, padSend, PAD_HISTORY_MAX } = sandbox;

// padHistory() builds its array inside the vm realm, so its prototype isn't
// this realm's Array — copy through JSON before any deepStrictEqual.
const hist = () => JSON.parse(JSON.stringify(padHistory()));

const reset = () => { store = {}; sent = []; toasts = []; closed = 0; padInput.value = ""; };

// ---- history --------------------------------------------------------------
reset();
assert.deepStrictEqual(hist(), [], "empty store → empty history");

store["aish-pad-history"] = "{not json";
assert.deepStrictEqual(hist(), [], "corrupt store → empty history, no throw");

store["aish-pad-history"] = JSON.stringify(["ok", 42, null, ""]);
assert.deepStrictEqual(hist(), ["ok"], "non-string / empty entries filtered out");

reset();
padHistoryPush("first");
padHistoryPush("second");
assert.deepStrictEqual(hist(), ["second", "first"], "newest first");

padHistoryPush("first"); // re-sending an old line moves it back to the top
assert.deepStrictEqual(hist(), ["first", "second"], "duplicates move, never repeat");

reset();
for (let i = 0; i < PAD_HISTORY_MAX + 5; i++) padHistoryPush(`line ${i}`);
const capped = hist();
assert.strictEqual(capped.length, PAD_HISTORY_MAX, `capped at ${PAD_HISTORY_MAX}`);
assert.strictEqual(capped[0], `line ${PAD_HISTORY_MAX + 4}`, "newest kept");
assert(!capped.includes("line 0"), "oldest dropped");

// ---- send -----------------------------------------------------------------
reset();
padInput.value = "  git status   \n ";
padSend(true);
assert.deepStrictEqual(sent, ["  git status\r"], "trailing whitespace trimmed, CR appended");
assert.deepStrictEqual(hist(), ["  git status"], "the sent text is what gets remembered");
assert.strictEqual(closed, 1, "the pad closes after a send");

reset();
padInput.value = "npm run build";
padSend(false);
assert.deepStrictEqual(sent, ["npm run build"], "hold-send inserts WITHOUT Enter");

reset();
padInput.value = "   \n  ";
padSend(true);
assert.deepStrictEqual(sent, [], "an empty pad sends nothing");
assert.deepStrictEqual(hist(), [], "…and records nothing");
assert.strictEqual(closed, 0, "…and stays open");
assert.strictEqual(toasts.length, 1, "…but says why");

// Multi-line stays multi-line: only the trailing blank is trimmed, so a
// Shift+Enter-composed block reaches the terminal as typed.
reset();
padInput.value = "cd /tmp\nls -la\n";
padSend(true);
assert.deepStrictEqual(sent, ["cd /tmp\nls -la\r"], "internal newlines preserved");

console.log("dictation pad: all checks passed");
