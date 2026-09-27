// The model chip's short label: `local:mlx-community/Qwen3.6-35B-A3B-8bit`
// reads as `Qwen3.6-35B-A3B-8bit (local)`, the suffix dropping out whole when
// it does not fit (CSS, checked in a real browser). The full spec stays the chip's
// identity — the context meter compares against it, and it is the hover title.
//
// The REAL block ([MODEL-LABEL]) is extracted from app.js and run against a
// minimal fake DOM.
//
// Run manually: node tests/js/test_model_label.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, fakeElement, checks } = require("./harness");

const { ok, report } = checks();

function world() {
  const els = { "model-name": fakeElement("span"), "model-label": fakeElement("span"),
    "model-suffix": fakeElement("span") };
  const sandbox = { $: (id) => els[id] };
  vm.createContext(sandbox);
  const code = extract(appSource(), "function modelChipLabel(", "// [MODEL-LABEL-END]");
  vm.runInContext(surface(code), sandbox);
  return { sandbox, els };
}

// ---- 1. the label drops the publisher and turns `local:` into a suffix
{
  const { sandbox } = world();
  const cases = [
    ["local:mlx-community/Qwen3.6-35B-A3B-8bit", "Qwen3.6-35B-A3B-8bit|(local)"],
    ["local:Qwen3-8B", "Qwen3-8B|(local)"],
    ["qwen3:8b", "qwen3:8b"],
    ["hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M", "Qwen3-8B-GGUF:Q4_K_M"],
    ["claude:claude-sonnet-5", "claude:claude-sonnet-5"],
    ["openai:org/some-model", "openai:some-model"],
    ["gemini", "gemini"],
  ];
  for (const [spec, want] of cases) {
    const label = sandbox.modelChipLabel(spec);
    const got = label.suffix ? `${label.name}|${label.suffix}` : label.name;
    ok(`${spec} -> ${want} (got ${got})`, got === want);
  }
}

// ---- 2. the chip shows the label but keeps the full spec as its identity
{
  const { sandbox, els } = world();
  sandbox.showModelName("local:mlx-community/Qwen3.6-35B-A3B-8bit");
  const el = els["model-name"];
  ok(`name is short: ${els["model-label"].textContent}`, els["model-label"].textContent === "Qwen3.6-35B-A3B-8bit");
  ok(`suffix says local: ${els["model-suffix"].textContent}`, els["model-suffix"].textContent === "(local)");
  ok("dataset keeps the full spec", el.dataset.model === "local:mlx-community/Qwen3.6-35B-A3B-8bit");
  ok("hover title is the full spec", el.title === "local:mlx-community/Qwen3.6-35B-A3B-8bit");
}

// ---- 3. a non-local model carries no suffix, even after a local one
{
  const { sandbox, els } = world();
  sandbox.showModelName("local:mlx-community/Qwen3.6-35B-A3B-8bit");
  sandbox.showModelName("claude:claude-sonnet-5");
  ok(`suffix cleared: "${els["model-suffix"].textContent}"`, els["model-suffix"].textContent === "");
}

// ---- 4. the chip is never written except through showModelName
{
  const src = appSource().replace(/\[MODEL-LABEL-START\][\s\S]*\[MODEL-LABEL-END\]/, "");
  const direct = src.match(/\$\("model-(name|label|suffix)"\)\.textContent\s*=/g) || [];
  ok(`no direct writes of the chip text outside [MODEL-LABEL] (${direct.length})`, direct.length === 0);
}

report("test_model_label");
