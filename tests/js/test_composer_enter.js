// Node-only, dependency-free regression check for issue #170 (the composer's
// send button is the ONLY thing that submits a prose message; Enter inserts a
// newline). Pulls the real onInputKeydown/onInputBeforeInput out of app.js by
// marker and runs them in an isolated vm against fake dependencies — so this
// exercises the shipped branching, not a copy.
//
// The deliberate asymmetry under test: terminal (`!`) mode is a shell prompt,
// where Enter RUNNING the command is the point (#100/#156), on both the desktop
// keydown path and iOS's beforeinput/insertLineBreak path.
//
// Also under test: Cmd/Ctrl+Enter sends from the prose composer. It does not
// weaken #170 — a modifier chord is unreachable by autocorrect, IME and
// dictation, which are what made bare Enter unsafe there.
//
// Run manually: node tests/js/test_composer_enter.js
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

const calls = [];
const suggestEl = { hidden: true };
const sandbox = {
  cmdMode: false,
  input: { value: "", focus() {} },
  suggest: { items: [], index: 0, kind: null, fragment: "" },
  $: (id) => (id === "suggest" ? suggestEl : null),
  paintSuggest() { calls.push("paintSuggest"); },
  hideSuggest() { calls.push("hideSuggest"); suggestEl.hidden = true; },
  acceptSuggestion(chosen) { calls.push(`acceptSuggestion:${chosen[0]}`); },
  recallHistory() { return false; },
  submitInput() { calls.push("submitInput"); },
  submitCommand() { calls.push("submitCommand"); },
};
vm.createContext(sandbox);
vm.runInContext(extract("// [ENTER-START]", "// [ENTER-END]"), sandbox);
assert(typeof sandbox.onInputKeydown === "function", "failed to extract onInputKeydown");
assert(typeof sandbox.onInputBeforeInput === "function", "failed to extract onInputBeforeInput");

function keyEvent(key, extra = {}) {
  return Object.assign(
    { key, shiftKey: false, metaKey: false, ctrlKey: false, altKey: false, prevented: false, stopped: false },
    { preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; } },
    extra,
  );
}

function beforeInputEvent(inputType) {
  return { inputType, prevented: false, preventDefault() { this.prevented = true; } };
}

function reset({ cmdMode = false, value = "", popup = null } = {}) {
  calls.length = 0;
  sandbox.cmdMode = cmdMode;
  sandbox.input.value = value;
  if (popup) {
    suggestEl.hidden = false;
    Object.assign(sandbox.suggest, popup);
  } else {
    suggestEl.hidden = true;
    Object.assign(sandbox.suggest, { items: [], index: 0, kind: null });
  }
}

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

check("prose: Enter does NOT submit and lets the newline through", () => {
  reset({ value: "hello" });
  const e = keyEvent("Enter");
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, [], "nothing may be submitted");
  assert.strictEqual(e.prevented, false, "the newline must not be cancelled");
});

check("prose: Shift+Enter still just inserts a newline", () => {
  reset({ value: "hello" });
  const e = keyEvent("Enter", { shiftKey: true });
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, []);
  assert.strictEqual(e.prevented, false);
});

check("prose: iOS insertLineBreak does NOT submit", () => {
  reset({ value: "hello" });
  const e = beforeInputEvent("insertLineBreak");
  sandbox.onInputBeforeInput(e);
  assert.deepStrictEqual(calls, []);
  assert.strictEqual(e.prevented, false, "the newline must reach the textarea");
});

check("terminal mode: Enter RUNS the command (deliberate #100 asymmetry)", () => {
  reset({ cmdMode: true, value: "ls -la" });
  const e = keyEvent("Enter");
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, ["submitInput"]);
  assert.strictEqual(e.prevented, true, "no stray newline in the shell prompt");
});

check("terminal mode: iOS insertLineBreak RUNS the command", () => {
  reset({ cmdMode: true, value: "ls -la" });
  const e = beforeInputEvent("insertLineBreak");
  sandbox.onInputBeforeInput(e);
  assert.deepStrictEqual(calls, ["submitCommand"]);
  assert.strictEqual(e.prevented, true);
});

check("prose: Enter on an exactly-typed slash closes the popup, never submits", () => {
  reset({ value: "/help", popup: { items: [["/help", "about aish web"]], index: 0, kind: "slash" } });
  const e = keyEvent("Enter");
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, ["hideSuggest"], "close it, don't send it");
  assert.strictEqual(e.prevented, true, "the text is left exactly as typed");
});

check("prose: Enter on a partial slash still accepts the suggestion", () => {
  reset({ value: "/he", popup: { items: [["/help", "about aish web"]], index: 0, kind: "slash" } });
  const e = keyEvent("Enter");
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, ["acceptSuggestion:/help"]);
  assert.strictEqual(e.prevented, true);
});

check("prose: Cmd+Enter sends — a chord no autocorrect/IME/dictation can emit", () => {
  reset({ value: "hello" });
  const e = keyEvent("Enter", { metaKey: true });
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, ["submitInput"]);
  assert.strictEqual(e.prevented, true, "no stray newline alongside the send");
});

check("prose: Ctrl+Enter sends too (the non-Mac desktop chord)", () => {
  reset({ value: "hello" });
  const e = keyEvent("Enter", { ctrlKey: true });
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, ["submitInput"]);
  assert.strictEqual(e.prevented, true);
});

check("prose: Cmd+Shift+Enter still sends", () => {
  reset({ value: "hello" });
  const e = keyEvent("Enter", { metaKey: true, shiftKey: true });
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, ["submitInput"]);
});

check("prose: Cmd+Enter sends past an open suggestion popup, never completes", () => {
  reset({ value: "/he", popup: { items: [["/help", "about aish web"]], index: 0, kind: "slash" } });
  const e = keyEvent("Enter", { metaKey: true });
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, ["submitInput"], "the chord means send, not complete");
  assert.strictEqual(e.prevented, true);
});

check("terminal mode: Cmd+Enter runs the command like bare Enter", () => {
  reset({ cmdMode: true, value: "ls -la" });
  const e = keyEvent("Enter", { metaKey: true });
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, ["submitInput"]);
  assert.strictEqual(e.prevented, true);
});

check("prose: Alt+Enter is NOT the send chord (it is a newline everywhere)", () => {
  reset({ value: "hello" });
  const e = keyEvent("Enter", { altKey: true });
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, []);
  assert.strictEqual(e.prevented, false);
});

check("terminal mode: Enter on an exactly-typed command runs it", () => {
  reset({ cmdMode: true, value: "git", popup: { items: [["git", ""]], index: 0, kind: "cmd" } });
  const e = keyEvent("Enter");
  sandbox.onInputKeydown(e);
  assert.deepStrictEqual(calls, ["submitInput"]);
  assert.strictEqual(e.prevented, true);
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
