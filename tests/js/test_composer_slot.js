// Node-only, dependency-free check for the composer's dual-purpose slot.
//
// Two affordances split by WHAT THEY ACT ON: the FIELD owns its content, so
// clearing is a control inside the field, shown only when there is something to
// clear; the ROW owns actions on the message, so pasting is a button beside
// attach/dictate/send and is ALWAYS there.
//
// They were one shared slot first (paste when empty, clear when not), and this
// file exists mostly to stop that coming back: the commonest flow is to type a
// few words AND THEN paste — "review this: <url>" — so a slot that swaps paste
// out the instant you type hides it exactly when it is wanted. Mutually
// exclusive on screen is not the same as mutually exclusive in use.
//
// Run manually: node tests/js/test_composer_slot.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

let passed = 0;
function ok(what, cond) {
  assert(cond, what);
  passed++;
}

function world({ clipboard = "pasted text", clipboardFails = false } = {}) {
  const calls = [];
  const slot = { attrs: {}, title: "", setAttribute(k, v) { this.attrs[k] = v; } };
  const sandbox = {
    input: { value: "", focus: () => calls.push("focus") },
    $: (id) => (id === "composer-slot" ? slot : null),
    saveDraft: () => calls.push("saveDraft"),
    resizeInput: () => calls.push("resizeInput"),
    showToast: (t) => calls.push(`toast:${t}`),
    composerInsert: (t) => calls.push(`insert:${t}`),
    navigator: {
      clipboard: {
        readText: () => (clipboardFails
          ? Promise.reject(new Error("denied"))
          : Promise.resolve(clipboard)),
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(extract("// [COMPOSER-SLOT-START]", "// [COMPOSER-SLOT-END]"), sandbox);
  return { s: sandbox, slot, calls };
}

(async () => {
  // Paste works on an EMPTY composer…
  {
    const w = world();
    await w.s.pasteIntoComposer();
    ok("pasting into an empty composer inserts the clipboard",
      w.calls.includes("insert:pasted text"));
  }

  // …and, the whole point, on one that already has text in it.
  {
    const w = world();
    w.s.input.value = "review this: ";
    await w.s.pasteIntoComposer();
    ok("pasting still works once you have started typing — the flow this exists for",
      w.calls.includes("insert:pasted text"));
    ok("…and it inserts rather than replacing", w.s.input.value === "review this: ");
  }

  // Clear empties the field and everything that mirrors it.
  {
    const w = world();
    w.s.input.value = "half a message I no longer want";
    w.s.clearComposer();
    ok("clearing empties the box", w.s.input.value === "");
    ok("…persists that (or a reload would resurrect it)", w.calls.includes("saveDraft"));
    ok("…re-measures the box, which may have been multi-line",
      w.calls.includes("resizeInput"));
    ok("…and never reads the clipboard", !w.calls.some((c) => c.startsWith("insert:")));
  }

  // Clearing an already-empty composer does nothing at all — no draft write,
  // no focus grab on a control that is not even shown.
  {
    const w = world();
    w.s.clearComposer();
    ok("clearing an empty composer is a no-op", w.calls.length === 0);
  }

  // The failure that must not look like a no-op: clipboard read refused
  // (insecure origin, permission denied). Tap-and-hold still works, so say so.
  {
    const w = world({ clipboardFails: true });
    await w.s.pasteIntoComposer();
    ok("a refused clipboard read explains itself",
      w.calls.some((c) => c.startsWith("toast:") && c.includes("tap-and-hold")));
    ok("…and inserts nothing", !w.calls.some((c) => c.startsWith("insert:")));
  }

  // An empty clipboard is not an error, but it is not silence either.
  {
    const w = world({ clipboard: "" });
    await w.s.pasteIntoComposer();
    ok("an empty clipboard says so rather than doing nothing",
      w.calls.some((c) => c === "toast:clipboard is empty"));
  }

  console.log(`test_composer_slot.js: ${passed} ok — all checks passed`);
})();
