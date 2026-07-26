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
  removeItem: (k) => { delete store[k]; },
};

const padInput = { value: "", focus() {} };
const historyBox = {
  hidden: true,
  children: [],
  scrollTop: 0,
  scrollHeight: 999,
  replaceChildren() { this.children = []; },
  appendChild(node) { this.children.push(node); },
};
let sent = [];
let toasts = [];
let closed = 0;
let closedDiscard = null; // the discard flag padSend passes to closeConsolePad

const sandbox = {
  localStorage,
  $: (id) => {
    if (id === "pad-history-list") return historyBox;
    assert.strictEqual(id, "pad-input");
    return padInput;
  },
  document: { createElement: (tag) => ({ tag, appendChild() {} }) },
  consoleSend: (data) => sent.push(data),
  showToast: (text) => toasts.push(text),
  closeConsolePad: (discard) => { closed++; closedDiscard = discard; },
  resizePadInput: () => {},
};
vm.createContext(sandbox);
vm.runInContext(
  extract("// [PAD-DRAFT-START]", "// [PAD-DRAFT-END]") + "\n" +
  extract("// [PAD-HISTORY-START]", "// [PAD-HISTORY-END]") + "\n" +
  extract("// [PAD-SEND-START]", "// [PAD-SEND-END]") + "\n" +
  extract("// [PAD-HISTORY-RENDER-START]", "// [PAD-HISTORY-RENDER-END]") + "\n" +
  "this.padHistory = padHistory; this.padHistoryPush = padHistoryPush;" +
  "this.padSend = padSend; this.PAD_HISTORY_MAX = PAD_HISTORY_MAX;" +
  "this.togglePadHistory = togglePadHistory; this.savePadDraft = savePadDraft;" +
  "this.padDraft = padDraft; this.PAD_DRAFT_KEY = PAD_DRAFT_KEY;",
  sandbox
);
const { padHistory, padHistoryPush, padSend, PAD_HISTORY_MAX, togglePadHistory } = sandbox;
const { savePadDraft, padDraft, PAD_DRAFT_KEY } = sandbox;

// padHistory() builds its array inside the vm realm, so its prototype isn't
// this realm's Array — copy through JSON before any deepStrictEqual.
const hist = () => JSON.parse(JSON.stringify(padHistory()));

const reset = () => {
  store = {}; sent = []; toasts = []; closed = 0; closedDiscard = null; padInput.value = "";
};

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

// ---- history rendering ----------------------------------------------------
// Stored newest-first, but rendered OLDEST-first so the newest entry sits at
// the bottom of the list — nearest the textarea and your thumb.
reset();
padHistoryPush("oldest");
padHistoryPush("middle");
padHistoryPush("newest");
historyBox.hidden = true;
togglePadHistory();
assert.strictEqual(historyBox.hidden, false, "opens the list");
assert.deepStrictEqual(
  historyBox.children.map((c) => c.textContent),
  ["oldest", "middle", "newest"],
  "newest rendered last (bottom of the list)"
);
assert.strictEqual(historyBox.scrollTop, historyBox.scrollHeight, "scrolled to the newest");

// Clicking an entry LOADS it for review — it never sends by itself.
historyBox.children[2].onclick();
assert.strictEqual(padInput.value, "newest", "entry loads into the pad");
assert.deepStrictEqual(sent, [], "loading an entry sends nothing");
assert.strictEqual(historyBox.hidden, true, "picking an entry closes the list");

togglePadHistory();
togglePadHistory();
assert.strictEqual(historyBox.hidden, true, "a second tap closes the list");

// ---- draft survival -------------------------------------------------------
// An unsent pad must survive the reload the app performs on itself after an
// update, and a PWA relaunch — that is the whole point of staging the text.
reset();
padInput.value = "half a spoken sentence";
savePadDraft();
assert.strictEqual(store[PAD_DRAFT_KEY], "half a spoken sentence", "draft persisted");
padInput.value = "";
assert.strictEqual(padDraft(), "half a spoken sentence", "…and readable back after a reload");

savePadDraft(); // an empty pad has no draft to keep
assert.strictEqual(PAD_DRAFT_KEY in store, false, "emptying the box clears the draft");

// Sending is the one thing that discards it — the text is out the door, so
// padSend closes the pad with the discard flag set (closeConsolePad, which owns
// the clearing, keeps the draft on every other close).
reset();
padInput.value = "echo hi";
savePadDraft();
padSend(true);
assert.strictEqual(closedDiscard, true, "a send discards the draft");

// Recalling a history entry re-arms the draft, so a reload keeps the recalled
// line too.
reset();
padHistoryPush("git status");
historyBox.hidden = true;
togglePadHistory();
historyBox.children[0].onclick();
assert.strictEqual(padDraft(), "git status", "a recalled entry is itself a draft");

// ---- Enter is a newline, not a send ---------------------------------------
// Multi-line composing only works if Return stays a Return; a stray Return must
// never fire a half-finished command at the terminal. Only the Send button
// submits, so the pad's keydown handler must not reach padSend.
const keydown = src.slice(src.indexOf('$("pad-input").addEventListener("keydown"'));
const handler = keydown.slice(0, keydown.indexOf("});") + 3);
assert(handler.includes("Escape"), "Esc still closes the pad");
assert(!handler.includes("padSend"), "Enter must not send — only the Send button does");

// The ✕ must NOT discard: a mis-tapped close losing a spoken sentence is the
// failure this pad exists to prevent, so it closes with no discard flag.
const closeWiring = src.slice(src.indexOf('$("pad-close").onclick'));
assert(
  /\$\("pad-close"\)\.onclick = \(\) => closeConsolePad\(\);/.test(closeWiring.slice(0, 200)),
  "the pad's ✕ closes without discarding the draft"
);

console.log("dictation pad: all checks passed");
