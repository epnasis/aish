// Node-only, dependency-free check for the composer's dual-purpose slot.
//
// One button, two mutually exclusive jobs: with an EMPTY composer it pastes,
// with text in it it clears. That is the whole reason it exists — the composer
// already carries +, mic and send, and there is no room for two more buttons;
// there is room for one, because you can never want both at once.
//
// What this pins is that the two can't drift apart: the action taken must match
// the face being shown, and the face is derived from the same value the action
// reads. A slot that clears when it shows a clipboard is worse than no slot.
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
  // Empty → paste, and the label says so.
  {
    const w = world();
    w.s.syncComposerSlot();
    ok("an empty composer offers paste", w.slot.attrs["aria-label"] === "paste");
    await w.s.composerSlotAction();
    ok("…and tapping it pastes", w.calls.includes("insert:pasted text"));
    ok("…without touching the draft", !w.calls.includes("saveDraft"));
  }

  // Non-empty → clear, and the label says so.
  {
    const w = world();
    w.s.input.value = "half a message I no longer want";
    w.s.syncComposerSlot();
    ok("a composer with text offers clear", w.slot.attrs["aria-label"] === "clear");
    await w.s.composerSlotAction();
    ok("…and tapping it empties the box", w.s.input.value === "");
    ok("…persists that (or a reload would resurrect it)", w.calls.includes("saveDraft"));
    ok("…re-measures the box, which may have been multi-line",
      w.calls.includes("resizeInput"));
    ok("…and never reads the clipboard", !w.calls.some((c) => c.startsWith("insert:")));
  }

  // The failure that must not look like a no-op: clipboard read refused
  // (insecure origin, permission denied). Tap-and-hold still works, so say so.
  {
    const w = world({ clipboardFails: true });
    await w.s.composerSlotAction();
    ok("a refused clipboard read explains itself",
      w.calls.some((c) => c.startsWith("toast:") && c.includes("tap-and-hold")));
    ok("…and inserts nothing", !w.calls.some((c) => c.startsWith("insert:")));
  }

  // An empty clipboard is not an error, but it is not silence either.
  {
    const w = world({ clipboard: "" });
    await w.s.composerSlotAction();
    ok("an empty clipboard says so rather than doing nothing",
      w.calls.some((c) => c === "toast:clipboard is empty"));
  }

  console.log(`test_composer_slot.js: ${passed} ok — all checks passed`);
})();
