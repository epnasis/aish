// The dossier panel (#243) — the REAL render functions out of app.js, run
// against a fixture shaped like /explain's JSON.
//
// Three properties are load-bearing and none of them is visible in a screenshot:
//
//   1. NOTHING becomes markup. This panel renders the untrusted half of the
//      machine — reasoning quotes fetched pages, tool results are whatever a
//      command printed, and the system text contains the owner's own rules. A
//      renderer that reached for innerHTML would execute a page aish read.
//   2. The three states are never a blank. "Not recorded", "recorded and empty"
//      and "recorded, then deleted" route to three different repairs, and a
//      blank cell reads as the first whatever the truth was.
//   3. The empty notes list never claims the turn is fine. It names the checks
//      it ran instead — a checker knows only the classes someone coded.
//
// Run manually: node tests/js/test_explain_panel.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface } = require("./harness");

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}`);
    console.error(`       ${err.message}`);
  }
}
function report() {
  if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
  console.log("explain panel: all checks passed");
}

function fakeEl(tag) {
  const el = {
    tagName: tag,
    className: "",
    textContent: "",
    innerHTML: "",
    type: "",
    placeholder: "",
    value: "",
    dataset: {},
    style: {},
    children: [],
    onclick: null,
    oninput: null,
    append(...nodes) { this.children.push(...nodes); },
    appendChild(node) { this.children.push(node); return node; },
    remove() {},
    querySelector() { return null; },
    scrollIntoView() {},
    classList: (() => {
      const set = new Set();
      return {
        add: (c) => set.add(c),
        remove: (c) => set.delete(c),
        contains: (c) => set.has(c),
        toggle: (c) => (set.has(c) ? set.delete(c) : set.add(c)),
      };
    })(),
  };
  return el;
}

/** Everything the render put on screen, flattened. */
function walk(node, out = []) {
  out.push(node);
  for (const child of node.children || []) walk(child, out);
  return out;
}

function textOf(node) {
  return walk(node).map((n) => n.textContent || "").join("\n");
}

function world() {
  const body = fakeEl("div");
  const title = fakeEl("strong");
  const sandbox = {
    document: { createElement: fakeEl, querySelector: () => null },
    $: (id) => (id === "xp-body" ? body : title),
    JSON, Math, Set, Map, console, String, Object,
    openSheet() {},
    currentSession: "session-x.jsonl",
    BASE: "/", token: "", location: { href: "http://x/" },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(appSource(), "// [EXPLAIN-START]", "// [EXPLAIN-END]")),
    sandbox
  );
  return { sandbox, body, title };
}

function doc(overrides = {}) {
  return Object.assign(
    {
      prompt: "find me the recipe from that video",
      ts: "2026-08-20T09:00:00",
      running: false,
      steering: [],
      trim: [],
      given: {
        state: "recorded",
        carried: false,
        briefs: [
          {
            model_call: 1,
            options: { model: "gemini", num_ctx: 32768, think: false, provider: "gemini",
                       system_role: "first_only" },
            system: {
              state: "recorded",
              parts: [{ at: 0, chars: 12, digest: "d", state: "recorded", text: "only YouTube" }],
            },
            tools: {
              state: "recorded", digest: "t", count: 1, names: ["web_search"],
              entries: [{ function: { name: "web_search", description: "search the web" } }],
            },
          },
        ],
        context: { state: "recorded", records: [] },
        knowledge: [],
        rules: { state: "recorded", corpus: {}, groups: {}, skipped: [], dropped: 0 },
      },
      thought: {
        state: "recorded",
        fragments: [],
        calls: [{ model_call: 1, text: "Rules say YouTube only.", truncated: 0, cap_source: null,
                  said: "", said_truncated: 0, stop: "stop", tokens: [10, 5], blocks: [],
                  malformed: [], synthesized: false }],
      },
      did: { calls: [], orphan_gates: [], verify: [] },
      produced: { answer: "here it is", answer_state: "recorded", status: "ok", error: "",
                  verify: { stopped: [], advised: [], passed: 0 } },
      notes: { rows: [], checks: [{ id: "a" }, { id: "b" }, { id: "c" }] },
    },
    overrides
  );
}

check("nothing the model or a page produced becomes markup", () => {
  const { sandbox, body } = world();
  const hostile = "<img src=x onerror=alert(1)><script>boom()</script>";
  const d = doc();
  d.prompt = hostile;
  d.thought.calls[0].text = hostile;
  d.given.briefs[0].system.parts[0].text = hostile;
  sandbox.xpRender(d);
  // Open every section, since the bodies are built lazily.
  for (const node of walk(body)) if (node.className === "xp-sechead") node.onclick();
  const nodes = walk(body);
  assert(nodes.some((n) => (n.textContent || "").includes("<script>")),
    "the hostile string should be on screen — as TEXT");
  for (const node of nodes) {
    assert(!/[<>]/.test(node.innerHTML || ""),
      `innerHTML was written on a ${node.tagName}: ${node.innerHTML}`);
  }
});

check("section bodies are built on first open, not on render", () => {
  const { sandbox, body } = world();
  sandbox.xpRender(doc());
  const sections = walk(body).filter((n) => n.className === "xp-sec");
  assert.equal(sections.length, 4);
  const given = sections[0];
  const secbody = given.children.find((c) => c.className === "xp-secbody");
  assert.equal(secbody.children.length, 0, "built before it was opened");
  given.children.find((c) => c.className === "xp-sechead").onclick();
  assert(secbody.children.length > 0, "opening did not build it");
  assert(given.classList.contains("open"));
});

check("purged bytes say deleted, and never render as a blank", () => {
  const { sandbox, body } = world();
  const d = doc();
  d.given.briefs[0].system.parts[0] = { at: 0, chars: 12, digest: "d", state: "purged", text: null };
  sandbox.xpRender(d);
  walk(body).find((n) => n.className === "xp-sechead").onclick();
  assert(textOf(body).includes("recorded, then deleted"));
});

check("a log predating the record says so rather than showing nothing", () => {
  const { sandbox, body } = world();
  const d = doc();
  d.thought = { state: "not_recorded", fragments: [], calls: [] };
  sandbox.xpRender(d);
  for (const node of walk(body)) if (node.className === "xp-sechead") node.onclick();
  assert(textOf(body).includes("not recorded"));
});

check("an empty notes list names its checks and never says all clear", () => {
  const { sandbox, body } = world();
  sandbox.xpRender(doc());
  const text = textOf(body);
  assert(text.includes("nothing flagged by the 3 checks this reader runs"), text.slice(0, 300));
  assert(!/nothing unusual/i.test(text));
});

check("a note is a control that opens the section it came from", () => {
  const { sandbox, body } = world();
  const d = doc();
  d.notes.rows = [{ check: "tool_failed", text: "read_url failed (403)",
                    where: { section: "did", call: 1 } }];
  sandbox.xpRender(d);
  const note = walk(body).find((n) => n.className === "xp-note");
  assert(note, "no note rendered");
  assert.equal(note.textContent, "read_url failed (403)");
  assert.equal(typeof note.onclick, "function");
});

check("a demoted per-task reminder is called out where the rules are", () => {
  const { sandbox, body } = world();
  sandbox.xpRender(doc());
  walk(body).find((n) => n.className === "xp-sechead").onclick();
  assert(textOf(body).includes("not as a system instruction"));
});

check("a running turn says what it is showing", () => {
  const { sandbox, body } = world();
  sandbox.xpRender(doc({ running: true }));
  assert(textOf(body).includes("still running"));
});

check("long text is folded, never truncated", () => {
  const { sandbox, body } = world();
  const d = doc();
  d.thought.calls[0].text = Array.from({ length: 40 }, (_, i) => `line ${i}`).join("\n");
  sandbox.xpRender(d);
  for (const node of walk(body)) if (node.className === "xp-sechead") node.onclick();
  const pre = walk(body).find(
    (n) => n.className === "xp-pre" && n.classList.contains("xp-clamped")
  );
  assert(pre, "long text was not folded");
  assert(pre.textContent.includes("line 39"), "the whole text must be present, just clamped");
  const more = walk(body).find((n) => n.className === "xp-more");
  assert(more && more.textContent.startsWith("show all"));
});

report();
