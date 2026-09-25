// The context meter in the model chip: how full the window was on the chat's
// last model call, from the `ctx` the agent stamps on that call's step.
//
// The REAL block ([CTX-METER]) is extracted from app.js and run against a
// minimal fake DOM.
//
// Run manually: node tests/js/test_ctx_meter.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, fakeElement, checks } = require("./harness");

const { ok, report } = checks();

function world() {
  const els = { "ctx-meter": fakeElement("span"), "model-chip": fakeElement("button"),
    "model-name": fakeElement("span") };
  els["model-name"].textContent = "claude:claude-sonnet-5";
  els["ctx-meter"].hidden = true;
  const sandbox = { $: (id) => els[id] };
  vm.createContext(sandbox);
  const code = extract(appSource(), "function fmtTokens(", "// [CTX-METER-END]");
  vm.runInContext(surface(code), sandbox);
  return { sandbox, els };
}

const reported = { used: 68000, window: 200000, window_source: "backend:claude:200000",
  basis: "reported" };

// ---- 1. a reported fill reads as a plain percentage, the detail on hover
{
  const { sandbox, els } = world();
  sandbox.setCtxFill(reported);
  const meter = els["ctx-meter"];
  ok(`shown as 34%: ${meter.textContent}`, !meter.hidden && meter.textContent === "34%");
  ok(`the title gives the tokens and where the window came from: ${meter.title}`,
    meter.title.includes("68.0k of 200.0k tokens") && meter.title.includes("backend:claude:200000"));
  ok("the chip's own title keeps saying what a tap does",
    els["model-chip"].title.startsWith("switch model · "));
}

// ---- 2. an estimate says so, in the figure and in the title
{
  const { sandbox, els } = world();
  sandbox.setCtxFill({ ...reported, basis: "estimated" });
  ok(`marked ~: ${els["ctx-meter"].textContent}`, els["ctx-meter"].textContent === "~34%");
  ok("the title says why it is an estimate", els["ctx-meter"].title.includes("estimated"));
}

// ---- 3. nothing measured shows nothing, and clearing puts the chip back
{
  const { sandbox, els } = world();
  sandbox.setCtxFill(reported);
  sandbox.setCtxFill(null);
  ok("cleared: hidden", els["ctx-meter"].hidden && els["ctx-meter"].textContent === "");
  ok("cleared: the chip's title is its own again", els["model-chip"].title === "switch model");
  sandbox.setCtxFill({ used: 0, window: 200000 });
  ok("a zero count is not a figure", els["ctx-meter"].hidden);
  sandbox.setCtxFill({ used: 500, window: 1048576, basis: "reported" });
  ok(`a sliver of a big window is <1%, not 0%: ${els["ctx-meter"].textContent}`,
    els["ctx-meter"].textContent === "<1%");
}

// ---- 4. a figure from another model is not shown against this one
{
  const { sandbox, els } = world();
  sandbox.setCtxFill({ ...reported, model: "claude:claude-sonnet-5" });
  ok("measured on the model the chip names: shown", els["ctx-meter"].textContent === "34%");
  sandbox.setCtxFill({ ...reported, model: "gemini:gemini-3.5-flash" });
  ok("measured on another model: hidden", els["ctx-meter"].hidden);
}

report("test_ctx_meter");
