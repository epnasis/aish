// Node-only, dependency-free check for #192's first release blocker: NO GHOST
// TRACE CARDS.
//
// This test pins the MECHANICAL FACT that makes the Python-side discipline
// load-bearing, rather than the discipline itself (which tests/test_agent.py's
// TestRenderlessRecords pins from both sides). The fact:
//
//   traceStep() calls ensureTrace() BEFORE it dispatches on step.kind
//
// so a record kind with no renderer does NOT degrade to "renders nothing" — it
// opens an empty live trace card with a running ticker. That is why a
// renderless kind must never reach the frontend at all, by either path (live
// emit or cold replay), and why "just don't give it a renderer" is not a
// design.
//
// If someone ever makes traceStep return early for unknown kinds, this test
// fails — and that is the right moment to reconsider, because the two Python
// mechanisms were sized against exactly this behaviour.
//
// Run manually: node tests/js/test_renderless_ghost_card.js
"use strict";

const fs = require("fs");
const path = require("path");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}`);
    console.error("  " + (err && err.message));
  }
}

function bodyOf(fnName) {
  const start = src.indexOf(`function ${fnName}(`);
  assert(start !== -1, `${fnName} not found in app.js`);
  // Walk braces from the signature's opening { to its match.
  const open = src.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced braces in ${fnName}`);
}

check("traceStep opens a card BEFORE it knows the kind", () => {
  const body = bodyOf("traceStep");
  const ensure = body.indexOf("ensureTrace()");
  assert(ensure !== -1, "traceStep no longer calls ensureTrace()");
  const firstKindTest = body.search(/step\.kind/);
  assert(firstKindTest !== -1, "traceStep no longer dispatches on step.kind");
  assert(
    ensure < firstKindTest,
    "traceStep now inspects step.kind before opening a card — the ghost-card " +
      "hazard this test documents may be gone; re-read docs/trace-contract.md §1.2",
  );
});

check("traceStep has no unknown-kind early return", () => {
  // The absence of a bail-out is the whole hazard. If one is added, the two
  // Python mechanisms stop being the only thing standing between a new record
  // kind and an empty card with a running ticker.
  const body = bodyOf("traceStep");
  assert(
    !/RENDERLESS|renderless/i.test(body),
    "traceStep now knows about renderless kinds — that is a THIRD mechanism; " +
      "the contract (§1.2/§1.3) specifies exactly two, deliberately",
  );
});

check("every renderless kind is unhandled by traceStep", () => {
  // Mirrors session.RENDERLESS_STEPS. Read from the Python source so the two
  // cannot drift silently.
  const sessionPy = fs.readFileSync(
    path.join(__dirname, "..", "..", "aish", "session.py"),
    "utf8",
  );
  const block = sessionPy.match(/RENDERLESS_STEPS = frozenset\(\s*\{([\s\S]*?)\}\s*\)/);
  assert(block, "RENDERLESS_STEPS not found in session.py");
  const kinds = [...block[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  assert(kinds.length >= 7, `expected the full renderless set, got ${kinds}`);

  const body = bodyOf("traceStep");
  for (const kind of kinds) {
    assert(
      !body.includes(`"${kind}"`),
      `traceStep handles "${kind}" — it is in RENDERLESS_STEPS, so it must ` +
        "never reach the frontend and needs no renderer",
    );
  }
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
