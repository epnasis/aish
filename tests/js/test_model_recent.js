// The model picker's Recent section (#412): it lists the models the owner's
// chats actually RAN on, as the server reports them, never a per-device memory
// of taps.
//
// Before, Recent was `localStorage["aish-recent-models"]`, written only by a tap
// on a picker row ON THIS DEVICE and then kept only if the name was also in the
// `model_list` the server had just sent. So a local model used on the phone,
// inherited by a new chat, restored by reopening a chat, or chosen in the
// terminal was never Recent anywhere else — the harness showed the MLX model
// used on one device and absent from Recent on the second, while it sat in the
// model list the second device had just been sent.
//
// The REAL picker block (openModelSheet … renderModels) is extracted from app.js
// and run against a minimal fake DOM.
//
// Run manually: node tests/js/test_model_recent.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, fakeElement, fakeStorage, checks } = require("./harness");

const { ok, report } = checks();

const LOCAL = "local:mlx-community/Qwen3.6-35B-A3B-8bit";

function pickerWorld({ search = "", stored = null } = {}) {
  const els = { "model-list": fakeElement("div"), "model-search": fakeElement("input"),
    "model-save": fakeElement("input") };
  els["model-search"].value = search;
  els["model-save"].checked = false;
  const acts = [];
  const storage = fakeStorage();
  if (stored) storage.setItem("aish-recent-models", JSON.stringify(stored));
  const sandbox = {
    $: (id) => els[id] || fakeElement("div"),
    document: { createElement: (tag) => fakeElement(tag) },
    localStorage: storage,
    act: (message) => acts.push(message),
    send() {},
    openSheet() {},
    sectionLabel(text) {
      const el = fakeElement("div");
      el.className = "section-label";
      el.textContent = text;
      return el;
    },
  };
  vm.createContext(sandbox);
  const code = extract(appSource(), "function openModelSheet(", "function onModelChanged(");
  vm.runInContext(surface(code), sandbox);
  return { sandbox, els, acts };
}

/** What the list shows, as the owner reads it: labels and row names in order. */
function shown(els) {
  return els["model-list"].children.map((el) =>
    el.className === "section-label" ? `[${el.textContent}]` : el.textContent);
}

const listed = [
  { name: "qwen3:4b", desc: "local · 3 GB · current" },
  { name: "local", desc: "your server · default default_model" },
  { name: LOCAL, desc: "your server · Local server" },
];

// ---- 1. a model the server says was used is Recent on a device that never tapped it
{
  const { sandbox, els } = pickerWorld();
  sandbox.renderModels({
    current: "qwen3:4b",
    models: listed,
    recent: [{ name: LOCAL, desc: "your server · Local server" }],
  });
  const rows = shown(els);
  ok(`the used local model heads the list on a device that never tapped it: ${JSON.stringify(rows)}`,
    rows[0] === "[Recent]" && rows[1] === LOCAL && rows[2] === "[All models]");
}

// ---- 2. the server's word decides, not this device's leftover taps
{
  const { sandbox, els } = pickerWorld({ stored: ["gemini:gemini-3.5-pro"] });
  sandbox.renderModels({
    current: "qwen3:4b",
    models: [...listed, { name: "gemini:gemini-3.5-pro", desc: "cloud · Gemini" }],
    recent: [],
  });
  ok(`no Recent section when the server reports no used model: ${JSON.stringify(shown(els))}`,
    !shown(els).includes("[Recent]"));
}

// ---- 3. a used model is Recent even when this list does not carry it --------
// A `provider:model` id arrives in the list only via the provider catalog; the
// server vouches it is selectable, so the row is built from what it sent.
{
  const { sandbox, els, acts } = pickerWorld();
  sandbox.renderModels({
    current: "qwen3:4b",
    models: [{ name: "qwen3:4b", desc: "local · 3 GB · current" }],
    recent: [{ name: LOCAL, desc: "your server · Local server" }],
  });
  const rows = shown(els);
  ok(`a used model missing from the catalog is still Recent: ${JSON.stringify(rows)}`, rows[1] === LOCAL);
  const recentRow = els["model-list"].children[1];
  recentRow.onclick();
  ok(`tapping the Recent row switches to exactly that model: ${JSON.stringify(acts)}`,
    acts.length === 1 && acts[0].type === "set_model" && acts[0].spec === LOCAL);
}

// ---- 4. searching hides Recent: a ranked list says why each row is there ----
{
  const { sandbox, els } = pickerWorld({ search: "qwen" });
  sandbox.renderModels({
    current: "qwen3:4b",
    models: listed,
    recent: [{ name: LOCAL, desc: "your server · Local server" }],
  });
  ok("no Recent section while searching", !shown(els).includes("[Recent]"));
}

report("model picker Recent");
