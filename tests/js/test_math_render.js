// Node-only checks for mathematical notation in the transcript (#391).
//
// The REAL renderMarkdown / inlineMd / findMath / mathNode are pulled out of
// app.js by marker and run against the REAL vendored katex.min.js (see
// math_dom.js) — the shipped renderer over the shipped library, never a stub of
// either.
//
// Two corpora pin the single-`$` rule from both sides:
//   - tests/fixtures/latex_answer_391.md is the exact answer from the session
//     that filed the issue (Gemini, 128 `$`, inline `$h$` and `$30\text{
//     meters}$`, single-line `$$…$$` displays indented under list items);
//   - prose with dollar prices, shell variables and currency codes, which must
//     come out byte-identical.
//
// Run manually: node tests/js/test_math_render.js
"use strict";

const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { buildSandbox, collectClass, collectTag, visibleText, checker, root } = require("./math_dom");

const corpus = fs.readFileSync(path.join(root, "tests", "fixtures", "latex_answer_391.md"), "utf8");
const sandbox = buildSandbox();
const { renderMarkdown, findMath } = sandbox;
const { check, done } = checker();

// ---- the corpus that filed the issue ----------------------------------------
check("the real answer: every $ delimiter is consumed and KaTeX nodes appear", () => {
  const frag = renderMarkdown(corpus);
  const rendered = collectClass(frag, "katex");
  const shown = visibleText(frag);
  const dollars = (shown.match(/\$/g) || []).length;
  assert.strictEqual((corpus.match(/\$/g) || []).length, 128, "fixture drifted");
  assert.strictEqual(dollars, 0, `visible $ left: ${dollars}`);
  assert(!/\\frac|\\circ|\\angle/.test(shown), "a control sequence survived into the visible text");
  // 128 `$` = 13 displays (four each) + 38 inline spans (two each).
  assert.strictEqual(rendered.length, 51, `rendered spans: ${rendered.length}`);
  assert.strictEqual(collectClass(frag, "math-source").length, 0, "some span fell back to source");
});

check("the real answer: every $$…$$ is a display block, inline spans are not", () => {
  const frag = renderMarkdown(corpus);
  const displays = collectClass(frag, "math-display");
  assert.strictEqual(displays.length, 13);
  assert.strictEqual((corpus.match(/\$\$/g) || []).length, 26, "fixture drifted");
  for (const d of displays) {
    assert.strictEqual(collectClass(d, "katex-display").length, 1, "display span without katex-display");
  }
  const inline = collectClass(frag, "math").filter((m) => !m.classList.contains("math-display"));
  assert.strictEqual(inline.length, 38);
  for (const m of inline) assert.strictEqual(collectClass(m, "katex-display").length, 0);
});

check("a display indented under a list item renders inside that item", () => {
  const md = "1. **Step:**\n   * The angle is:\n     $$\\angle TBP_1 = 80^\\circ$$\n   * next";
  const frag = renderMarkdown(md);
  const items = collectTag(frag, "LI");
  assert.strictEqual(items.length, 3, "outer item + two inner items");
  assert.strictEqual(collectClass(frag, "math-display").length, 1);
  const angle = items.find((li) => visibleText(li).includes("The angle is:"));
  const next = items.find((li) => visibleText(li).trim() === "next");
  assert(angle && next, "items not found by text");
  assert.strictEqual(collectClass(angle, "math-display").length, 1, "the display is not inside its item");
  assert.strictEqual(collectClass(next, "math-display").length, 0);
});

check("bold holding maths and italic holding maths both render the maths", () => {
  const frag = renderMarkdown("is **$72.08\\text{ meters}$** and *(where $d_1$ is)*");
  const strong = collectTag(frag, "STRONG");
  const em = collectTag(frag, "EM");
  assert.strictEqual(strong.length, 1);
  assert.strictEqual(em.length, 1);
  assert.strictEqual(collectClass(strong[0], "katex").length, 1, "no maths inside strong");
  assert.strictEqual(collectClass(em[0], "katex").length, 1, "no maths inside em");
  assert(visibleText(em[0]).startsWith("(where "), visibleText(em[0]));
});

check("the unambiguous delimiters: \\(…\\), \\[…\\], $$ over line breaks", () => {
  const frag = renderMarkdown("a \\(x^2\\) b\n\\[\\frac{1}{2}\\]\nc $$\nE = mc^2\n$$ d");
  const maths = collectClass(frag, "math");
  assert.strictEqual(maths.length, 3, `got ${maths.length}`);
  assert(!maths[0].classList.contains("math-display"));
  assert(maths[1].classList.contains("math-display"));
  assert(maths[2].classList.contains("math-display"));
  assert(!visibleText(frag).includes("$$"));
});

check("an unclosed $$ mid-stream stays literal until it closes", () => {
  const partial = renderMarkdown("Then:\n$$\\frac{h}{\\sin(45^\\circ)} = ");
  assert.strictEqual(collectClass(partial, "math").length, 0);
  assert.strictEqual(visibleText(partial), "Then:\n$$\\frac{h}{\\sin(45^\\circ)} = ");
  const closed = renderMarkdown("Then:\n$$\\frac{h}{\\sin(45^\\circ)} = 1$$");
  assert.strictEqual(collectClass(closed, "math-display").length, 1);
});

// ---- the corpus that must NOT change ------------------------------------------
const PROSE = [
  "it costs $5 and the other is $10",
  "prices range from $5-$10 per unit, or $5 to $7.50 with tax",
  "set $HOME/$PATH first, then compare $A:$B and $X/$Y",
  "US$5 and A$3 are not the same; US$5 to US$10 either",
  "paid $5. Then $6, then $ 7, then $8",
  "the $ sign alone, and a trailing $",
  "$5 for A$ and B$ each",
  "(costs $2x$?) no marker, digit start",
];
for (const line of PROSE) {
  check(`prose stays prose: ${JSON.stringify(line)}`, () => {
    assert.strictEqual(findMath(line), null, "findMath matched");
    const frag = renderMarkdown(line);
    assert.strictEqual(collectClass(frag, "math").length, 0);
    assert.strictEqual(collectClass(frag, "math-source").length, 0);
    assert.strictEqual(visibleText(frag), line);
  });
}

check("a $ inside inline code or a link target is never maths", () => {
  const md = "run `echo $HOME` then `$PATH` and [x](https://h/?a=$1&b=$2) $h$";
  const frag = renderMarkdown(md);
  assert.strictEqual(collectTag(frag, "CODE").length, 2);
  assert.strictEqual(collectTag(frag, "A").length, 1);
  assert.strictEqual(collectClass(frag, "math").length, 1, "only the trailing $h$ is maths");
  assert.strictEqual(collectTag(frag, "CODE")[0].textContent, "echo $HOME");
});

check("the single-$ rule, case by case", () => {
  const yes = ["$h$", "$B$ be", "($h$)", "so $TB = h$.", "$P_1$", "$30\\text{ meters}$ down",
    "of $10^\\circ$ to", "**$72.08\\text{ meters}$**", "$x$-axis", "$\\alpha$"];
  const no = ["$5 for A$ and", "$n$th", "x$y$", "$ x$", "$x $", "$2x$", "$$", "$ $", "a$b$c"];
  for (const s of yes) assert(findMath(s) !== null, `should be maths: ${s}`);
  for (const s of no) assert(findMath(s) === null, `should be prose: ${s}`);
});

// ---- lossless on error, inert on hostile input --------------------------------
check("a parse error puts the exact source back, delimiters included", () => {
  const md = "before $\\frac{a}{$ after and $$\\left( x $$ end";
  const frag = renderMarkdown(md);
  const fallbacks = collectClass(frag, "math-source");
  assert.strictEqual(fallbacks.length, 2, `got ${fallbacks.length}`);
  assert.strictEqual(fallbacks[0].textContent, "$\\frac{a}{$");
  assert.strictEqual(fallbacks[1].textContent, "$$\\left( x $$");
  assert(/KaTeX parse error/.test(fallbacks[0].title), fallbacks[0].title);
  assert.strictEqual(collectClass(frag, "katex").length, 0);
  assert.strictEqual(visibleText(frag), md, "the paragraph is not byte-identical");
});

check("with the vendor script absent every span is its source, unchanged", () => {
  const saved = sandbox.window.katex;
  sandbox.window.katex = undefined;
  try {
    const frag = renderMarkdown("a $h$ b $$x$$ c");
    assert.strictEqual(collectClass(frag, "katex").length, 0);
    assert.strictEqual(collectClass(frag, "math-source").length, 2);
    assert.strictEqual(visibleText(frag), "a $h$ b $$x$$ c");
  } finally {
    sandbox.window.katex = saved;
  }
});

check("hostile input: \\href, \\url, \\includegraphics, \\htmlData never become live nodes", () => {
  const md = [
    "$\\href{javascript:alert(1)}{click}$",
    "$\\url{https://evil.example/x}$",
    "$\\includegraphics[height=1em]{https://evil.example/pixel.png}$",
    "$\\htmlData{x=1}{y}$",
  ].join(" and ");
  const frag = renderMarkdown(md);
  assert.strictEqual(collectTag(frag, "A").length, 0, "an anchor was minted");
  assert.strictEqual(collectTag(frag, "IMG").length, 0, "an image was minted");
  assert.strictEqual(collectTag(frag, "SCRIPT").length, 0);
  // The trust function throws, so every untrusted span takes the verbatim
  // path: the exact source is on screen, nothing of it acts. (KaTeX's own
  // trust:false rendering was measured to show `\href` alone in red, with the
  // arguments dropped — inert, but not lossless.)
  const refused = collectClass(frag, "math-source");
  assert.strictEqual(refused.length, 4, "expected every hostile span refused");
  assert(/\\href is not rendered/.test(refused[0].title), refused[0].title);
  assert.strictEqual(collectClass(frag, "katex").length, 0);
  assert.strictEqual(visibleText(frag), md);
});

check("a \\rule cannot grow past maxSize and \\def cannot expand forever", () => {
  const frag = renderMarkdown("$\\rule{1000em}{1000em}$ and $\\def\\a{\\a\\a}\\a$");
  let ruleStyles = 0;
  for (const el of collectTag(frag, "SPAN")) {
    for (const v of Object.values(el.style)) {
      assert(!/1000em/.test(String(v)), `1000em survived: ${v}`);
      if (/^10em$/.test(String(v))) ruleStyles++;
    }
  }
  assert(ruleStyles > 0, "the rule was not capped to maxSize (10em)");
  const fallbacks = collectClass(frag, "math-source");
  assert.strictEqual(fallbacks.length, 1, "the runaway \\def must fall back");
  assert.strictEqual(fallbacks[0].textContent, "$\\def\\a{\\a\\a}\\a$");
});

done("math render");
