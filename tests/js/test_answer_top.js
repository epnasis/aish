// Node-only, dependency-free check for "jump to the start of this answer".
//
// The arithmetic is the whole feature, and it has one trap that reads as
// correct: #messages is a SIBLING below #topbar, not underneath it, so an
// element sitting at the scroller's own top is already clear of the header.
// Subtracting the header's height as "clearance" — which sounds right, and was
// the first version — overshoots by exactly the header and lands you above the
// answer, in the previous turn. On a short transcript it lands at 0, which
// looks like a working "scroll to top" button and is not the feature at all.
//
// Runs the REAL function against a fake scroller, so it pins the maths rather
// than a description of it.
//
// Run manually: node tests/js/test_answer_top.js
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

// A transcript scroller whose viewport starts at y=107 (i.e. below a 107px-tall
// header) — the geometry that made the header-subtracting version look fine on
// a long answer and wrong on everything else.
const SCROLLER_TOP = 107;

function world({ scrollTop, answerTop }) {
  const scrolls = [];
  const sandbox = {
    messagesEl: {
      scrollTop,
      getBoundingClientRect: () => ({ top: SCROLLER_TOP }),
      scrollTo: (opts) => scrolls.push(opts),
    },
    $: () => ({ getBoundingClientRect: () => ({ height: SCROLLER_TOP }) }),
    Math,
  };
  vm.createContext(sandbox);
  vm.runInContext(extract("// [ANSWER-TOP-START]", "// [ANSWER-TOP-END]"), sandbox);
  const el = { getBoundingClientRect: () => ({ top: answerTop }) };
  sandbox.scrollToAnswerTop(el);
  return { scrolls, sandbox };
}

// The answer starts 4000px above the viewport; scrolled to 5000.
{
  const w = world({ scrollTop: 5000, answerTop: SCROLLER_TOP - 4000 });
  const target = w.scrolls[0].top;
  ok("it scrolls exactly to the answer, less a small gap", target === 5000 - 4000 - 8);
  ok("…and NOT to the top of the transcript", target > 0);
  ok("…and not a header's worth further up (the trap)",
    target !== 5000 - 4000 - 8 - SCROLLER_TOP);
  ok("it asks for a smooth scroll", w.scrolls[0].behavior === "smooth");
}

// The answer is already at the top of the viewport: nothing meaningful to do,
// and above all no negative target.
{
  const w = world({ scrollTop: 300, answerTop: SCROLLER_TOP });
  ok("an answer already at the top stays put (bar the gap)",
    w.scrolls[0].top === 292);
}

// The first answer in a short chat: the honest target is the very top, and it
// must never go below zero.
{
  const w = world({ scrollTop: 4, answerTop: SCROLLER_TOP });
  ok("the target is clamped at the top of the transcript", w.scrolls[0].top === 0);
}

// Scrolled ABOVE the answer (you jumped back, then tapped it): it must come
// forward, not refuse.
{
  const w = world({ scrollTop: 100, answerTop: SCROLLER_TOP + 900 });
  ok("an answer below the fold scrolls down to it", w.scrolls[0].top === 992);
}

// ---- when the chip should exist at all ------------------------------------
// A button that visibly does nothing reads as broken, not as "nothing to do".
{
  function fitWorld({ answerHeight, viewportHeight }) {
    const chip = { className: "answer-top", hidden: false };
    const answerEl = {
      offsetHeight: answerHeight,
      querySelector: (sel) => (sel === ".answer-top" ? chip : null),
    };
    const sandbox = {
      messagesEl: {
        clientHeight: viewportHeight,
        scrollTop: 0,
        getBoundingClientRect: () => ({ top: 0 }),
        scrollTo() {},
      },
      Math,
      ResizeObserver: undefined,
      document: { createElement: () => ({ appendChild() {}, setAttribute() {} }) },
      svgIcon: () => ({}),
    };
    vm.createContext(sandbox);
    vm.runInContext(extract("// [ANSWER-TOP-START]", "// [ANSWER-TOP-END]"), sandbox);
    sandbox.syncAnswerTopChip(answerEl);
    return chip;
  }

  ok("an answer taller than the viewport keeps the chip",
    fitWorld({ answerHeight: 4000, viewportHeight: 700 }).hidden === false);
  ok("an answer that fits on screen hides it — its first line is already visible",
    fitWorld({ answerHeight: 300, viewportHeight: 700 }).hidden === true);
  ok("…and one exactly the viewport's height hides it too (nowhere to scroll)",
    fitWorld({ answerHeight: 700, viewportHeight: 700 }).hidden === true);
  ok("a hair taller than the viewport shows it again",
    fitWorld({ answerHeight: 760, viewportHeight: 700 }).hidden === false);
}

console.log(`test_answer_top.js: ${passed} ok — all checks passed`);
