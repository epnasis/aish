// Node-only, dependency-free check: the transcript's delete chip asks before it
// deletes, names the right turn, and leaves a visible gap where the turn was.
//
// Why each of those matters:
//   - It asks, because the deletion is irreversible — the text leaves the log
//     file — and the chip sits in a row whose other controls (copy, reuse) are
//     tapped constantly and without thinking. The asking itself is pinned in
//     test_confirm_modal.js; what matters here is that the chip cannot bypass
//     it. This chip's first shipped guard was an arm-then-confirm that turned a
//     17px glyph red UNDER the thumb covering it, which is why the bar is now
//     "a modal, from the chip, every time".
//   - The right turn, because the id is the ONLY thing naming what goes; a chip
//     that sent the wrong one would delete someone else's exchange.
//   - A visible gap, because a chat that silently loses a turn leaves the answer
//     above it reading as a reply to nothing, and a deletion you cannot see is
//     indistinguishable from data quietly going missing.
//
// Runs the REAL block from app.js (extracted by marker) against a minimal fake
// DOM, so it tests the shipped code rather than a copy.
//
// Run manually: node tests/js/test_redact_turn.js
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

let checks = 0;
function ok(label, cond) { assert(cond, label); checks += 1; }

function fakeElement(tag) {
  const el = {
    tagName: tag,
    className: "",
    title: "",
    type: "",
    textContent: "",
    attrs: {},
    children: [],
    _classes: new Set(),
    classList: {
      add(c) { el._classes.add(c); },
      remove(c) { el._classes.delete(c); },
      contains(c) { return el._classes.has(c); },
    },
    setAttribute(k, v) { el.attrs[k] = v; },
    append(...kids) { el.children.push(...kids); },
    appendChild(kid) { el.children.push(kid); return kid; },
  };
  return el;
}

function world() {
  const sent = [];
  const asked = [];
  const messagesEl = fakeElement("div");
  const sandbox = {
    document: { createElement: fakeElement },
    messagesEl,
    send: (m) => { sent.push(m); return true; },
    act: (m) => { sent.push(m); return true; }, // [ACK-LEDGER]; nothing is claimed here
    scrollToEnd() {},
    trashIcon: () => fakeElement("svg"),
    messageStamp: (at) => (at ? "14:32" : ""),
    // The shared modal is pinned by test_confirm_modal.js; here it is a spy, so
    // this test can prove the chip never routes AROUND it.
    askConfirm: (opts) => { asked.push(opts); },
  };
  vm.createContext(sandbox);
  // vm contexts don't expose top-level const/let, so the extracted declarations
  // are switched to var — plain declarations, no block-scoping dependency.
  vm.runInContext(
    extract("// [REDACT-START]", "// [REDACT-END]").replace(/\bconst\b/g, "var"),
    sandbox,
  );
  return { sandbox, sent, asked, messagesEl };
}

// 1. THE REGRESSION SHAPE: tapping the chip must not delete anything.
{
  const w = world();
  const chip = w.sandbox.redactChip("turn-abc");
  chip.onclick();
  ok("tapping the chip sends nothing", w.sent.length === 0);
  ok("it asks first", w.asked.length === 1);
}

// 2. The question says what is lost — a verb and a red button never do.
{
  const w = world();
  w.sandbox.redactChip("turn-abc").onclick();
  const { title, body, verb } = w.asked[0];
  ok("the title names the unit: the whole exchange", /exchange/i.test(title));
  ok("the body says the model forgets it too", /model/i.test(body));
  ok("…and that the offline copies go", /offline/i.test(body));
  ok("…and that it is final", /cannot be undone/i.test(body));
  ok("the button says DELETE, not something softer", verb === "Delete");
}

// 3. Confirming deletes the turn the chip was built for.
{
  const w = world();
  w.sandbox.redactChip("turn-abc").onclick();
  w.asked[0].action();
  ok("confirming deletes", w.sent.length === 1);
  ok("it names the right turn",
    w.sent[0].type === "redact" && w.sent[0].turn === "turn-abc");
}

// 4. Each chip carries its OWN turn — a shared modal must not blur which one
//    asked. Two chips, and the second one's answer must not delete the first.
{
  const w = world();
  const first = w.sandbox.redactChip("turn-one");
  const second = w.sandbox.redactChip("turn-two");
  first.onclick();
  second.onclick();
  w.asked[1].action();
  ok("the answer belongs to the chip that asked last", w.sent[0].turn === "turn-two");
  ok("…and only that one was deleted", w.sent.length === 1);
}

// 5. The gap is visible — and UNDATED. A time on it reads as "deleted at",
//    which is not what the only available number means (it is the deleted
//    message's own time), so one number would claim two different things.
{
  const w = world();
  const row = w.sandbox.addRedactedMsg();
  ok("a row is added where the turn was", w.messagesEl.children.length === 1);
  const labels = row.children.flatMap((c) => c.children || []).map((c) => c.textContent);
  ok("it says what happened", labels.includes("Message deleted"));
  ok("and nothing else — no timestamp", labels.filter(Boolean).length === 1);
}

// 6. Exactly ONE place can send a deletion, and it is behind the modal. A
//    second, unconfirmed sender anywhere would defeat all of the above.
{
  const senders = src.match(/(?:send|act)\(\{ type: "redact"/g) || [];
  ok("only one code path sends redact", senders.length === 1);
  const block = extract("// [REDACT-START]", "// [REDACT-END]");
  ok("…and it is inside the confirmation's action", block.includes('act({ type: "redact"'));
  // Through the ledger, never bare: a destructive request handed to a dead
  // socket is #210.
  ok("…as an ACT, not a bare send", !/send\(\{ type: "redact"/.test(src));
  ok("the chip only ever asks", /btn\.onclick = \(\) => askDeleteTurn\(turn\)/.test(block));
}

// 7. The control is only offered for a turn the server can name. A chip built
//    from an undefined id would send a removal that matches nothing.
{
  ok("addUserMsg gates the chip on having a turn id",
    /if \(turn\) tools\.append\(redactChip\(turn\)\)/.test(src));
}

console.log(`${checks} ok — all checks passed`);
