// Node-only, dependency-free check on the 👍/👎 chip (#207).
//
// Two properties, and both are the reason the feature exists rather than
// details of it:
//
//   - THE TAP RECORDS IMMEDIATELY, before any comment is typed. A rating that
//     waits for a reason is a rating that mostly does not happen, and the count
//     is the part the rules engine needs: it is how "is he still correcting
//     turns that passed every rule they were subject to" gets answered at all.
//     If this regresses to send-on-submit, the metric quietly measures only the
//     turns he felt like explaining.
//
//   - IT NAMES THE RIGHT TURN. The id is the only thing tying a verdict to the
//     answer it judges and to the rules that governed it. A chip that sent the
//     wrong one would attribute his complaint to someone else's exchange.
//
// Runs the REAL block from app.js (extracted by marker) against a minimal fake
// DOM, so it tests the shipped code rather than a copy.
//
// Run manually: node tests/js/test_rating_chip.js
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
    value: "",
    placeholder: "",
    textContent: "",
    attrs: {},
    children: [],
    parentElement: null,
    _classes: new Set(),
    classList: {
      add(c) { el._classes.add(c); },
      remove(c) { el._classes.delete(c); },
      contains(c) { return el._classes.has(c); },
      toggle(c, on) { if (on) el._classes.add(c); else el._classes.delete(c); },
    },
    setAttribute(k, v) { el.attrs[k] = v; },
    append(...kids) { kids.forEach((k) => { k.parentElement = el; el.children.push(k); }); },
    appendChild(kid) { kid.parentElement = el; el.children.push(kid); return kid; },
    remove() {
      const p = el.parentElement;
      if (p) p.children = p.children.filter((c) => c !== el);
    },
    querySelector(sel) {
      return el.children.find((c) => sel.includes("rating-note") && c.className === "rating-note")
        || null;
    },
    querySelectorAll() { return el.children.filter((c) => c.className.includes("rating-chip")); },
    focus() {},
  };
  return el;
}

function world() {
  const sent = [];
  const sandbox = {
    document: { createElement: fakeElement },
    messagesEl: fakeElement("div"),
    send: (m) => { sent.push(m); return true; },
    CSS: { escape: (s) => s },
  };
  vm.createContext(sandbox);
  vm.runInContext(extract("// [RATING]", "function copyChip("), sandbox);
  return { sandbox, sent };
}

// --- the tap records before any comment exists ------------------------------
{
  const { sandbox, sent } = world();
  const tools = fakeElement("div");
  const chip = sandbox.ratingChip("turn-abc", "down");
  tools.appendChild(chip);
  chip.onclick();
  ok("the tap sends immediately", sent.length === 1);
  ok("it is a rate message", sent[0].type === "rate");
  ok("it names the turn it was built for", sent[0].turn === "turn-abc");
  ok("it carries the verdict", sent[0].rating === "down");
  ok("and no comment is required for it to count", !("comment" in sent[0]));
}

// --- the comment is a SECOND record, not a precondition ---------------------
{
  const { sandbox, sent } = world();
  const tools = fakeElement("div");
  const chip = sandbox.ratingChip("turn-abc", "down");
  tools.appendChild(chip);
  chip.onclick();
  const note = tools.children.find((c) => c.className === "rating-note");
  ok("a reason box opens after the tap", !!note);
  note.value = "the price was stale";
  note.onkeydown({ key: "Enter", preventDefault() {} });
  ok("submitting sends a second record", sent.length === 2);
  ok("carrying the reason", sent[1].comment === "the price was stale");
  ok("still naming the same turn", sent[1].turn === "turn-abc");
  ok("and the box closes", !tools.children.includes(note));
}

// --- an empty reason writes nothing extra -----------------------------------
{
  const { sandbox, sent } = world();
  const tools = fakeElement("div");
  const chip = sandbox.ratingChip("turn-abc", "up");
  tools.appendChild(chip);
  chip.onclick();
  const note = tools.children.find((c) => c.className === "rating-note");
  note.value = "   ";
  note.onkeydown({ key: "Enter", preventDefault() {} });
  ok("an empty reason adds no record", sent.length === 1);
}

// --- a second tap does not stack a second box -------------------------------
{
  const { sandbox } = world();
  const tools = fakeElement("div");
  const chip = sandbox.ratingChip("turn-abc", "down");
  tools.appendChild(chip);
  chip.onclick();
  chip.onclick();
  const boxes = tools.children.filter((c) => c.className === "rating-note");
  ok("only one reason box exists", boxes.length === 1);
}

console.log(`ok - rating chip (${checks} checks)`);
