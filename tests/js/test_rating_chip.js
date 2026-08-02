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
    // The real classList reflects whatever `className` was assigned; a fake
    // that only tracks add()/remove() would report false for the classes the
    // code sets as a string, which is how it builds every chip.
    classList: {
      add(c) { el._classes.add(c); },
      remove(c) { el._classes.delete(c); },
      contains(c) { return el._classes.has(c) || el.className.split(/\s+/).includes(c); },
      toggle(c, on) { if (on) el._classes.add(c); else el._classes.delete(c); },
    },
    setAttribute(k, v) { el.attrs[k] = v; },
    scrollIntoView() { el.scrolledIntoView = (el.scrolledIntoView || 0) + 1; },
    append(...kids) { kids.forEach((k) => { k.parentElement = el; el.children.push(k); }); },
    appendChild(kid) { kid.parentElement = el; el.children.push(kid); return kid; },
    remove() {
      const p = el.parentElement;
      if (p) p.children = p.children.filter((c) => c !== el);
    },
    dataset: {},
    // A very small selector matcher — enough for the three lookups the real
    // code makes, and no more: it must not quietly answer a query the shipped
    // code does not actually issue.
    querySelector(sel) {
      const hit = (node) => {
        if (sel.startsWith("[data-turn=")) {
          return node.dataset && node.dataset.turn === sel.slice(12, -2);
        }
        // Compound class selectors (".rating-chip.on"), matched through
        // classList so a class added after construction counts — which is
        // exactly how the lit state is set.
        return sel.split(".").filter(Boolean)
          .every((cls) => node.classList.contains(cls));
      };
      const walk = (node) => {
        for (const kid of node.children) {
          if (hit(kid)) return kid;
          const deep = walk(kid);
          if (deep) return deep;
        }
        return null;
      };
      return walk(el);
    },
    querySelectorAll() { return el.children.filter((c) => c.className.includes("rating-chip")); },
    focus() {},
  };
  return el;
}

// The chip lives in `.msg-tools`, which lives in the answer element — the
// reason box goes on the ANSWER, one row below the strip.
function mount(sandbox, turn, kind) {
  const host = fakeElement("div");
  host.dataset.turn = turn;             // as attachAnswerTools sets it
  const tools = fakeElement("div");
  tools.className = "msg-tools";
  host.appendChild(tools);
  const chip = sandbox.ratingChip(turn, kind);
  tools.appendChild(chip);
  sandbox.messagesEl.appendChild(host); // so markRating can find it, as in the page
  return { host, tools, chip };
}

function world() {
  const sent = [];
  const sandbox = {
    document: { createElement: fakeElement },
    messagesEl: fakeElement("div"),
    send: (m) => { sent.push(m); return true; },
    CSS: { escape: (s) => s },
    // Defined elsewhere in app.js; the chip only needs it to return a node.
    svgIcon: () => fakeElement("svg"),
    // The note keeps itself on screen as the keyboard opens; the listeners are
    // real code and need somewhere to attach.
    visualViewport: { addEventListener() {}, removeEventListener() {} },
    window: { visualViewport: { addEventListener() {}, removeEventListener() {} } },
  };
  vm.createContext(sandbox);
  vm.runInContext(extract("// [RATING]", "function copyChip("), sandbox);
  return { sandbox, sent };
}

// --- the tap records before any comment exists ------------------------------
{
  const { sandbox, sent } = world();
  const { chip } = mount(sandbox, "turn-abc", "down");
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
  const { host, tools, chip } = mount(sandbox, "turn-abc", "down");
  chip.onclick();
  const note = host.children.find((c) => c.className === "rating-note");
  ok("a reason box opens after the tap", !!note);
  ok("and is scrolled into view, not left under the keyboard", note.scrolledIntoView > 0);
  ok("on its own row, not squeezed into the chip strip", !tools.children.includes(note));
  note.value = "the price was stale";
  note.onkeydown({ key: "Enter", preventDefault() {} });
  ok("submitting sends a second record", sent.length === 2);
  ok("carrying the reason", sent[1].comment === "the price was stale");
  ok("still naming the same turn", sent[1].turn === "turn-abc");
  ok("and the box closes", !tools.children.includes(note));
}

// --- typing and tapping AWAY still saves ------------------------------------
// The bug the owner hit twice: blur kept the text and sent nothing, so the log
// held his tap with an empty comment and the words were gone. On a phone,
// tapping away is how you finish typing.
{
  const { sandbox, sent } = world();
  const { host, chip } = mount(sandbox, "turn-abc", "down");
  chip.onclick();
  const note = host.children.find((c) => c.className === "rating-note");
  note.value = "it never read the page";
  note.onblur();
  ok("blurring commits the reason", sent.length === 2);
  ok("with the words typed", sent[1].comment === "it never read the page");
  ok("the box closes", !host.children.includes(note));
  ok("and the reason is shown", host.children.some((c) => c.className === "rating-said"));
}

// --- committing happens once, whichever way you leave -----------------------
{
  const { sandbox, sent } = world();
  const { host, chip } = mount(sandbox, "turn-abc", "down");
  chip.onclick();
  const note = host.children.find((c) => c.className === "rating-note");
  note.value = "once";
  note.onkeydown({ key: "Enter", preventDefault() {} });
  note.onblur();                      // the browser fires blur after removal too
  ok("Return then blur writes one record, not two", sent.length === 2);
}

// --- an empty reason writes nothing extra -----------------------------------
{
  const { sandbox, sent } = world();
  const { host, chip } = mount(sandbox, "turn-abc", "up");
  chip.onclick();
  const note = host.children.find((c) => c.className === "rating-note");
  note.value = "   ";
  note.onkeydown({ key: "Enter", preventDefault() {} });
  ok("an empty reason adds no record", sent.length === 1);
}

// --- a second tap does not stack a second box -------------------------------
{
  const { sandbox } = world();
  const { host, chip } = mount(sandbox, "turn-abc", "down");
  chip.onclick();
  const boxes = host.children.filter((c) => c.className === "rating-note");
  ok("only one reason box exists", boxes.length === 1);
}


// --- an opinion can be taken back -------------------------------------------
{
  const { sandbox, sent } = world();
  const { host, chip } = mount(sandbox, "turn-abc", "down");
  chip.onclick();
  ok("the first tap records the verdict", sent[0].rating === "down");
  ok("and lights the chip", chip.classList.contains("on"));
  chip.onclick();
  ok("tapping the lit thumb withdraws it", sent[1].rating === "none");
  ok("naming the same turn", sent[1].turn === "turn-abc");
  ok("the chip goes dark", !chip.classList.contains("on"));
  ok("and the reason box closes with it",
     !host.children.some((c) => c.className === "rating-note"));
}

// --- withdrawal is a record, never a deletion --------------------------------
{
  const { sandbox, sent } = world();
  const { chip } = mount(sandbox, "turn-abc", "up");
  chip.onclick();
  chip.onclick();
  chip.onclick();
  ok("every tap writes", sent.length === 3);
  ok("alternating opinion and withdrawal",
     sent.map((m) => m.rating).join(",") === "up,none,up");
}

// --- the reason is read back on render --------------------------------------
{
  const { sandbox } = world();
  const { host } = mount(sandbox, "turn-abc", "down");
  sandbox.markRating("turn-abc", "down", "it never read the page");
  const said = host.children.find((c) => c.className === "rating-said");
  ok("a replayed reason is shown", !!said);
  ok("with the words the owner wrote", said.textContent === "it never read the page");
}

// --- withdrawing takes the words with it ------------------------------------
{
  const { sandbox } = world();
  const { host } = mount(sandbox, "turn-abc", "down");
  sandbox.markRating("turn-abc", "down", "it never read the page");
  sandbox.markRating("turn-abc", "none", "");
  ok("a withdrawn rating shows no reason",
     !host.children.some((c) => c.className === "rating-said"));
}

// --- a rating with no reason shows no empty line ----------------------------
{
  const { sandbox } = world();
  const { host } = mount(sandbox, "turn-abc", "up");
  sandbox.markRating("turn-abc", "up", "");
  ok("no blank line for a bare thumb",
     !host.children.some((c) => c.className === "rating-said"));
}

// --- the reason can be corrected --------------------------------------------
{
  const { sandbox, sent } = world();
  const { host, chip } = mount(sandbox, "turn-abc", "down");
  sandbox.markRating("turn-abc", "down", "first try");
  const said = host.children.find((c) => c.className === "rating-said");
  said.onclick();
  const note = host.children.find((c) => c.className === "rating-note");
  ok("tapping the reason reopens the box", !!note);
  ok("pre-filled with what was written", note.value === "first try");
  note.value = "second try";
  note.onkeydown({ key: "Enter", preventDefault() {} });
  ok("and the correction is sent", sent[sent.length - 1].comment === "second try");
}

console.log(`ok - rating chip (${checks} checks)`);
