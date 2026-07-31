// Node-only, dependency-free check: removing an exchange (#202) takes TWO taps,
// names the right turn, and leaves a visible gap where the turn was.
//
// Why each of those matters:
//   - Two taps, because the removal is irreversible — the text leaves the log
//     file — and the chip sits in a row whose other controls (copy, reuse) are
//     tapped constantly and without thinking. Same standard as deleting a chat
//     (test_delete_confirm.js).
//   - The right turn, because the id is the ONLY thing naming what goes; a chip
//     that sent the wrong one would remove someone else's exchange.
//   - A visible gap, because a chat that silently loses a turn leaves the answer
//     above it reading as a reply to nothing, and a removal you cannot see is
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
  const timers = [];
  const messagesEl = fakeElement("div");
  const sandbox = {
    document: { createElement: fakeElement },
    messagesEl,
    send: (m) => { sent.push(m); return true; },
    scrollToEnd() {},
    trashIcon: () => fakeElement("svg"),
    messageStamp: (at) => (at ? "14:32" : ""),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (id) timers[id - 1] = null; },
  };
  vm.createContext(sandbox);
  // vm contexts don't expose top-level const/let, so the extracted declarations
  // are switched to var — plain declarations, no block-scoping dependency.
  vm.runInContext(
    extract("// [REDACT-START]", "// [REDACT-END]").replace(/\bconst\b/g, "var"),
    sandbox,
  );
  return { sandbox, sent, timers, messagesEl };
}

// 1. THE REGRESSION SHAPE: one tap must not remove anything.
{
  const w = world();
  const chip = w.sandbox.redactChip("turn-abc");
  chip.onclick();
  ok("first tap sends nothing", w.sent.length === 0);
  ok("first tap arms the chip visibly", chip.classList.contains("armed"));
  ok("…and says what the next tap does", /again/.test(chip.title));
}

// 2. The second tap commits, naming the turn the chip was built for.
{
  const w = world();
  const chip = w.sandbox.redactChip("turn-abc");
  chip.onclick();
  chip.onclick();
  ok("second tap removes", w.sent.length === 1);
  ok("it names the right turn",
    w.sent[0].type === "redact" && w.sent[0].turn === "turn-abc");
  ok("committing disarms the chip", !chip.classList.contains("armed"));
  ok("committing clears the pending disarm", w.timers.filter(Boolean).length === 0);
}

// 3. A half-committed destructive control must not sit there indefinitely,
//    or the SECOND tap becomes the accidental one.
{
  const w = world();
  const chip = w.sandbox.redactChip("turn-abc");
  chip.onclick();
  const armed = w.timers.filter(Boolean);
  ok("arming schedules a disarm", armed.length === 1);
  ok("the disarm window is bounded", armed[0].ms > 0 && armed[0].ms <= 10000);
  armed[0].fn();
  ok("it disarms itself", !chip.classList.contains("armed"));
  chip.onclick();
  ok("after disarming, a tap only re-arms", w.sent.length === 0);
}

// 4. Arming a second chip disarms the first: two half-committed controls on
//    screen at once means a stray tap on either is destructive.
{
  const w = world();
  const first = w.sandbox.redactChip("turn-one");
  const second = w.sandbox.redactChip("turn-two");
  first.onclick();
  second.onclick();
  ok("only one chip is ever armed", !first.classList.contains("armed"));
  ok("…and nothing was sent by the switch", w.sent.length === 0);
  second.onclick();
  ok("the armed one commits its own turn", w.sent[0].turn === "turn-two");
}

// 5. The gap is visible, and carries the removed turn's OWN time — not now, or
//    an old exchange's removal would read as something that just happened.
{
  const w = world();
  const row = w.sandbox.addRedactedMsg(1785400000);
  ok("a row is added where the turn was", w.messagesEl.children.length === 1);
  const labels = row.children.flatMap((c) => c.children || []).map((c) => c.textContent);
  ok("it says what happened", labels.includes("Message removed"));
  ok("it carries the turn's own time", labels.includes("14:32"));
}

// 6. Exactly ONE place can send a removal, and it is behind the arming. A
//    second, unconfirmed sender anywhere would defeat all of the above.
{
  const senders = src.match(/send\(\{ type: "redact"/g) || [];
  ok("only one code path sends redact", senders.length === 1);
  ok("…and it is inside the arming block",
    extract("// [REDACT-START]", "// [REDACT-END]").includes('send({ type: "redact"'));
}

// 7. The control is only offered for a turn the server can name. A chip built
//    from an undefined id would send a removal that matches nothing.
{
  ok("addUserMsg gates the chip on having a turn id",
    /if \(turn\) tools\.append\(redactChip\(turn\)\)/.test(src));
}

console.log(`${checks} ok — all checks passed`);
