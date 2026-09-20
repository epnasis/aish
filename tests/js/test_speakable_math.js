// Node-only checks for read-aloud over rendered maths (#391).
//
// speakableText walks every text node and skipped exactly PRE and .msg-tools;
// it has no aria-hidden check, so a KaTeX span — a MathML mirror carrying the
// TeX source beside the visual HTML — would be read twice and garbled. The
// REAL speakableText runs here over the REAL renderMarkdown + katex.min.js
// (math_dom.js) and must say "formula" once per equation, nothing of the
// internals, while a fallback span (source text) is still read verbatim.
//
// Run manually: node tests/js/test_speakable_math.js
"use strict";

const assert = require("assert");
const { buildSandbox, makeElement, checker } = require("./math_dom");

const sandbox = buildSandbox();
const { renderMarkdown, speakableText } = sandbox;
const { check, done } = checker();

function spoken(markdown) {
  const holder = makeElement("div");
  holder.appendChild(renderMarkdown(markdown));
  return speakableText(holder);
}

check("a rendered equation is spoken as 'formula', once, in place", () => {
  const text = spoken("Let $h$ be the height.\n\n$$h = \\frac{30}{0.41623}$$\n\nSo $h \\approx 72$.");
  assert.strictEqual((text.match(/formula/g) || []).length, 3, text);
  assert(!/frac|approx|\$|\b72\b|0\.41623/.test(text), `KaTeX internals leaked: ${text}`);
  assert.strictEqual(text.split("\n")[0], "Let formula be the height.");
  assert.strictEqual(text.split("\n")[2], "So formula.");
});

check("the real answer is spoken with no $ and no control sequence", () => {
  const fs = require("fs");
  const path = require("path");
  const corpus = fs.readFileSync(
    path.join(__dirname, "..", "fixtures", "latex_answer_391.md"), "utf8");
  const text = spoken(corpus);
  assert.strictEqual((text.match(/formula/g) || []).length, 51, "one per rendered span");
  assert(!/\$|\\frac|\\circ|\\angle|\\sin/.test(text), "LaTeX reached the speech text");
  assert(/Law of Sines/.test(text), "prose around the maths is still read");
});

check("a span that fell back to its source is read verbatim, as before", () => {
  assert.strictEqual(spoken("see $\\frac{a}{$ here"), "see $\\frac{a}{$ here");
});

check("prose dollars are read as written", () => {
  assert.strictEqual(spoken("it costs $5 and the other is $10"), "it costs $5 and the other is $10");
});

done("speakable math");
