// The navigation chords (#384) — the REAL [NAV-CHORDS] block and the REAL
// document keydown handler out of app.js, on a macOS and on a Windows/Linux
// platform.
//
// What is pinned:
//
//   1. New chat is ⌘⇧O and ⌘O on macOS (Ctrl+Shift+O / Ctrl+O elsewhere);
//      search chats is ⌘K and ⌘⇧K (Ctrl+K / Ctrl+Shift+K); the chat list
//      shows/hides on ⌘B (Ctrl+B). The platform is DETECTED and the other
//      modifier is not accepted: on macOS Ctrl+K is the text field's own
//      kill-line, and on Linux Ctrl+K in the terminal is the shell's.
//   2. Nothing fires while the confirmation modal is up or while the terminal
//      holds the keyboard; the composer is not such a field — the chords
//      work from it.
//   3. The ONE keydown handler is the registration point: ⌘K reaches
//      searchChats and preventDefault, ⌘⇧O reaches requestNewChat (the header
//      button's own path), and a modal or the terminal stops both there too.
//   4. The tooltips name the chord in the platform's glyph.
//
// Run manually: node tests/js/test_nav_chords.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface } = require("./harness");

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}`); console.error(`       ${err.stack || err.message}`); }
}

const src = appSource();

function chords(platform) {
  const sandbox = { navigator: { platform, userAgent: "" }, Boolean, String };
  vm.createContext(sandbox);
  vm.runInContext(surface(extract(src, "// [NAV-CHORDS-START]", "// [NAV-CHORDS-END]")), sandbox);
  assert(typeof sandbox.navChord === "function", "navChord is not in [NAV-CHORDS]");
  return sandbox;
}

const key = (k, mods = {}) => ({ key: k, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...mods });

// ---- 1. the chords, per platform --------------------------------------------------

check("macOS: ⌘⇧O and ⌘O are new chat, ⌘K and ⌘⇧K are search, ⌘B is the chat list; Ctrl is not accepted", () => {
  const s = chords("MacIntel");
  assert.equal(s.IS_MAC, true);
  assert.equal(s.navChord(key("O", { metaKey: true, shiftKey: true })), "new");
  assert.equal(s.navChord(key("o", { metaKey: true, shiftKey: true })), "new");
  assert.equal(s.navChord(key("o", { metaKey: true })), "new", "⌘O is new chat, not the rail");
  assert.equal(s.navChord(key("k", { metaKey: true })), "search");
  assert.equal(s.navChord(key("K", { metaKey: true, shiftKey: true })), "search-up");
  assert.equal(s.navChord(key("b", { metaKey: true })), "rail");
  assert.equal(s.navChord(key("B", { metaKey: true })), "rail");
  // The Emacs bindings of a macOS text field stay with the field.
  assert.equal(s.navChord(key("k", { ctrlKey: true })), null, "Ctrl+K is kill-line on macOS");
  assert.equal(s.navChord(key("o", { ctrlKey: true, shiftKey: true })), null);
  assert.equal(s.navChord(key("o", { ctrlKey: true })), null, "Ctrl+O is open-line on macOS");
  assert.equal(s.navChord(key("b", { ctrlKey: true })), null, "Ctrl+B is back-a-char on macOS");
  assert.equal(s.navChord(key("n", { ctrlKey: true })), null, "Ctrl+N is next-line on macOS");
  assert.equal(s.navChord(key("p", { ctrlKey: true })), null, "Ctrl+P is previous-line on macOS");
  // Both modifiers at once is neither.
  assert.equal(s.navChord(key("k", { metaKey: true, ctrlKey: true })), null);
  // Alt is never a navigation chord.
  assert.equal(s.navChord(key("k", { metaKey: true, altKey: true })), null);
  // The older combos: ⌘P still exports and ⌘N is still new; ⌘⇧P (the rail's
  // former double) and ⌘⇧B are nothing — one toggle, one chord.
  assert.equal(s.navChord(key("p", { metaKey: true })), "export");
  assert.equal(s.navChord(key("n", { metaKey: true })), "new");
  assert.equal(s.navChord(key("p", { metaKey: true, shiftKey: true })), null);
  assert.equal(s.navChord(key("b", { metaKey: true, shiftKey: true })), null);
  // A plain key is nothing.
  assert.equal(s.navChord(key("k")), null);
  assert.equal(s.navChord(key("k", { shiftKey: true })), null);
  assert.equal(s.navChord(key("b")), null);
  // ⌘⇧M is the model list, ⌘M local ⇄ cloud; Ctrl+M is not either.
  assert.equal(s.navChord(key("M", { metaKey: true, shiftKey: true })), "models");
  assert.equal(s.navChord(key("m", { metaKey: true })), "model-toggle");
  assert.equal(s.navChord(key("m", { ctrlKey: true })), null);
  assert.equal(s.navChord(key("m", { ctrlKey: true, shiftKey: true })), null);
  assert.deepEqual(s.CHORD_HINTS, { new: "⌘⇧O", search: "⌘K", rail: "⌘B", models: "⌘⇧M", modelToggle: "⌘M" });
});

check("Windows/Linux: Ctrl+Shift+O / Ctrl+O are new chat, Ctrl+K and Ctrl+Shift+K are search, Ctrl+B is the chat list; ⌘ (Win key) is not accepted", () => {
  for (const platform of ["Win32", "Linux x86_64"]) {
    const s = chords(platform);
    assert.equal(s.IS_MAC, false, platform);
    assert.equal(s.navChord(key("o", { ctrlKey: true, shiftKey: true })), "new", platform);
    assert.equal(s.navChord(key("o", { ctrlKey: true })), "new", platform);
    assert.equal(s.navChord(key("k", { ctrlKey: true })), "search", platform);
    assert.equal(s.navChord(key("k", { ctrlKey: true, shiftKey: true })), "search-up", platform);
    assert.equal(s.navChord(key("b", { ctrlKey: true })), "rail", platform);
    assert.equal(s.navChord(key("k", { metaKey: true })), null, `${platform}: the Win key is not the primary modifier`);
    assert.equal(s.navChord(key("o", { metaKey: true, shiftKey: true })), null, platform);
    assert.equal(s.navChord(key("b", { metaKey: true })), null, platform);
    // Ctrl+Alt is AltGr on many layouts: never a chord.
    assert.equal(s.navChord(key("k", { ctrlKey: true, altKey: true })), null, platform);
    assert.equal(s.navChord(key("M", { ctrlKey: true, shiftKey: true })), "models", platform);
    assert.equal(s.navChord(key("m", { ctrlKey: true })), "model-toggle", platform);
    // Ctrl+M is the terminal's carriage return there: the terminal keeps it.
    assert.equal(s.navChord(key("m", { ctrlKey: true }), { terminal: true }), null, platform);
    assert.deepEqual(s.CHORD_HINTS, { new: "Ctrl+Shift+O", search: "Ctrl+K", rail: "Ctrl+B", models: "Ctrl+Shift+M", modelToggle: "Ctrl+M" }, platform);
  }
});

check("iOS with a hardware keyboard is ⌘ too", () => {
  const s = chords("iPad");
  assert.equal(s.IS_MAC, true);
  assert.equal(s.navChord(key("k", { metaKey: true })), "search");
});

// ---- 2. where they must not fire ----------------------------------------------------

check("a confirmation modal or the terminal holding the keyboard stops every chord", () => {
  const s = chords("MacIntel");
  const search = key("k", { metaKey: true });
  const fresh = key("o", { metaKey: true, shiftKey: true });
  assert.equal(s.navChord(search, { modal: true }), null);
  assert.equal(s.navChord(fresh, { modal: true }), null);
  assert.equal(s.navChord(search, { terminal: true }), null);
  assert.equal(s.navChord(fresh, { terminal: true }), null);
  assert.equal(s.navChord(search, {}), "search");
  assert.equal(s.navChord(search), "search");
});

// ---- 3. the one handler, wired -----------------------------------------------------

function handlerWorld(platform, { modal = false, terminal = false } = {}) {
  const calls = [];
  const els = new Map();
  const el = (id) => { if (!els.has(id)) els.set(id, { id, hidden: true, focus: () => calls.push(`focus:${id}`), select: () => calls.push(`select:${id}`), disabled: false, value: "" }); return els.get(id); };
  let handler = null;
  const active = terminal ? { closest: (sel) => (sel === "#pty-overlay" ? {} : null) } : null;
  const doc = { addEventListener: (type, fn) => { if (type === "keydown") handler = fn; }, activeElement: active, querySelectorAll: () => [] };
  const sandbox = {
    navigator: { platform, userAgent: "" }, Boolean, String, Set, Map, Array, Object,
    document: doc,
    $: el,
    closePreview: () => false, previewKey: () => false, ssIsOpen: () => false, ssClose() {}, ssTape() {}, ssScrollPage() {},
    confirmIsOpen: () => modal, closeConfirm() {}, consoleLinkMenuOpen: () => false, closeConsoleLinkMenu() {},
    escapeExit: () => false, railIsOpen: () => false, closeSheets: () => calls.push("closeSheets"),
    editingNow: () => false, CARD_SHORTCUTS: [], activeApprovalCard: () => null,
    requestNewChat: () => calls.push("requestNewChat"), openSessionRail: (q) => calls.push(`openSessionRail:${q}`),
    toggleSessionRail: () => calls.push("toggleSessionRail"), exportSessionPdf: () => calls.push("exportSessionPdf"),
    toggleConsole: () => calls.push("toggleConsole"),
    openModelSheet: (q) => { calls.push(`openModelSheet:${q}`); el("model-sheet").hidden = false; },
    stepListRow: (list, step) => calls.push(`stepListRow:${list.id}:${step}`),
    act: (message) => calls.push(`act:${message.type}`),
    startRailCursor: (q) => calls.push(`startRailCursor:${q}`),
    stepRailCursor: (step) => calls.push(`stepRailCursor:${step}`),
    requestAnimationFrame: (fn) => fn(),
  };
  vm.createContext(sandbox);
  vm.runInContext(surface(extract(src, "// [NAV-CHORDS-START]", "// Desktop only: auto-focusing on a phone")), sandbox);
  assert(typeof handler === "function", "the keydown handler was not registered");
  return { calls, el, doc, fire: (e) => { let prevented = false; handler({ ...e, preventDefault: () => { prevented = true; } }); return prevented; } };
}

check("⌘K opens the chat list and focuses its search field through the one handler; ⌘⇧O goes the header button's way", () => {
  const w = handlerWorld("MacIntel");
  assert.equal(w.fire(key("k", { metaKey: true })), true, "the browser's own ⌘K is prevented");
  assert.deepEqual(w.calls, ["openSessionRail:", "startRailCursor:", "focus:sessions-search", "select:sessions-search"]);
  w.calls.length = 0;
  assert.equal(w.fire(key("O", { metaKey: true, shiftKey: true })), true);
  assert.deepEqual(w.calls, ["requestNewChat", "closeSheets"], "new chat is the reconnect-aware path the button uses");
  w.calls.length = 0;
  // ⌘O is the same new chat — and the browser's own Open dialog is prevented.
  assert.equal(w.fire(key("o", { metaKey: true })), true);
  assert.deepEqual(w.calls, ["requestNewChat", "closeSheets"]);
  w.calls.length = 0;
  // ⌘B is the chats button's own tap.
  assert.equal(w.fire(key("b", { metaKey: true })), true);
  assert.deepEqual(w.calls, ["toggleSessionRail"]);
  w.calls.length = 0;
  // Ctrl+K on macOS falls through untouched.
  assert.equal(w.fire(key("k", { ctrlKey: true })), false);
  assert.deepEqual(w.calls, []);
  // Ctrl+\ is still the console's own toggle, either modifier.
  assert.equal(w.fire(key("\\", { ctrlKey: true })), true);
  assert.deepEqual(w.calls, ["toggleConsole"]);
});

check("⌘⇧M opens the model list, and each further press moves one row down; ⌘M asks the server to toggle", () => {
  const w = handlerWorld("MacIntel");
  assert.equal(w.fire(key("M", { metaKey: true, shiftKey: true })), true);
  assert.deepEqual(w.calls, ["openModelSheet:"]);
  w.calls.length = 0;
  assert.equal(w.fire(key("M", { metaKey: true, shiftKey: true })), true);
  assert.equal(w.fire(key("M", { metaKey: true, shiftKey: true })), true);
  assert.deepEqual(w.calls, ["stepListRow:model-list:1", "stepListRow:model-list:1"]);
  w.calls.length = 0;
  assert.equal(w.fire(key("m", { metaKey: true })), true, "the browser's own ⌘M (minimise) is prevented");
  assert.deepEqual(w.calls, ["act:toggle_model"]);
});

check("stepListRow is ↓/↑: from no highlight down is the first row, up the last, and it wraps", () => {
  const rows = [0, 1, 2].map(() => ({ active: false, classList: null, scrollIntoView() {} }));
  for (const row of rows) {
    row.classList = { contains: (c) => c === "active" && row.active, toggle: (c, on) => { row.active = on; } };
  }
  const list = { querySelectorAll: () => rows };
  const sandbox = { Array };
  vm.createContext(sandbox);
  vm.runInContext(surface(extract(src, "function setActiveRow(rows, index) {", "function attachListNav(")), sandbox);
  const at = () => rows.findIndex((r) => r.active);
  sandbox.stepListRow(list, 1); assert.equal(at(), 0);
  sandbox.stepListRow(list, 1); assert.equal(at(), 1);
  sandbox.stepListRow(list, 1); sandbox.stepListRow(list, 1); assert.equal(at(), 0, "wraps");
  sandbox.stepListRow(list, -1); assert.equal(at(), 2);
  rows[2].active = false;
  sandbox.stepListRow(list, -1); assert.equal(at(), 2, "up from nothing is the last row");
});

check("once the search field has the keyboard, ⌘K is one row down and ⌘⇧K one row up", () => {
  const w = handlerWorld("MacIntel");
  w.doc.activeElement = w.el("sessions-search");
  assert.equal(w.fire(key("k", { metaKey: true })), true);
  assert.equal(w.fire(key("k", { metaKey: true })), true);
  assert.equal(w.fire(key("K", { metaKey: true, shiftKey: true })), true);
  assert.deepEqual(w.calls, ["stepRailCursor:1", "stepRailCursor:1", "stepRailCursor:-1"]);
});

// ---- the chat list's cursor ([RAIL-CURSOR]) -----------------------------------------

function railWorld(names) {
  const list = { id: "sessions-list", rows: [], querySelectorAll: () => list.rows,
    querySelector: () => list.rows.find((r) => r.active) || null };
  const paint = (ns) => {
    list.rows = ns.map((name) => {
      const row = { active: false, dataset: { name }, scrollIntoView() {} };
      row.classList = { contains: (c) => c === "active" && row.active, toggle: (c, on) => { row.active = on; } };
      return row;
    });
  };
  paint(names);
  const sandbox = { Array, $: () => list, currentSession: "c" };
  vm.createContext(sandbox);
  vm.runInContext(surface(
    extract(src, "function setActiveRow(rows, index) {", "function attachListNav(")), sandbox);
  const at = () => list.rows.filter((r) => r.active).map((r) => r.dataset.name);
  return { s: sandbox, paint, at };
}

check("⌘K with nothing typed starts on the chat you are in; with a search, on its top result", () => {
  const w = railWorld(["a", "b", "c", "d"]);
  w.s.startRailCursor("");
  assert.deepEqual(w.at(), ["c"]);
  w.s.stepRailCursor(1); assert.deepEqual(w.at(), ["d"]);
  w.s.stepRailCursor(-1); w.s.stepRailCursor(-1); assert.deepEqual(w.at(), ["b"]);
  w.s.startRailCursor("gem");
  assert.deepEqual(w.at(), ["a"]);
});

check("the highlight follows its CHAT across a repaint, and a search waits for its rows", () => {
  const w = railWorld(["a", "b", "c"]);
  w.s.startRailCursor("");
  w.s.stepRailCursor(1);                   // on "a" (wrapped past the end)
  w.paint(["x", "a", "b", "c"]);           // a chat started working: the roster moved
  w.s.applyRailCursor();
  assert.deepEqual(w.at(), ["a"], "still the chat it was on, not the index");
  w.paint([]);
  w.s.startRailCursor("q");                // nothing to highlight yet
  assert.deepEqual(w.at(), []);
  w.paint(["r1", "r2"]);
  w.s.applyRailCursor();
  assert.deepEqual(w.at(), ["r1"]);
});

check("a chat the list does not show (a fresh one) starts the cursor on the top row", () => {
  const w = railWorld(["a", "b"]);
  w.s.startRailCursor("");               // currentSession "c" is not listed
  assert.deepEqual(w.at(), ["a"]);
  w.s.stepRailCursor(1); assert.deepEqual(w.at(), ["b"]);
});

check("renderSessions re-applies the cursor after every paint", () => {
  const body = extract(src, "function renderSessions(event) {", "function paintSessionList(event) {");
  assert(/paintSessionList\(event\);\s*applyRailCursor\(\);/.test(body), body);
});

check("on Windows/Linux the same handler answers to Ctrl", () => {
  const w = handlerWorld("Win32");
  assert.equal(w.fire(key("k", { ctrlKey: true, shiftKey: true })), true);
  assert.deepEqual(w.calls, ["openSessionRail:", "startRailCursor:", "focus:sessions-search", "select:sessions-search"]);
  w.calls.length = 0;
  assert.equal(w.fire(key("k", { metaKey: true })), false, "the Win key is not the modifier");
  assert.deepEqual(w.calls, []);
});

check("with the confirmation modal up, or the terminal focused, the handler does nothing with the chords", () => {
  for (const opts of [{ modal: true }, { terminal: true }]) {
    const w = handlerWorld("MacIntel", opts);
    assert.equal(w.fire(key("k", { metaKey: true })), false, JSON.stringify(opts));
    assert.equal(w.fire(key("o", { metaKey: true, shiftKey: true })), false, JSON.stringify(opts));
    assert.deepEqual(w.calls, [], JSON.stringify(opts));
  }
});

// ---- 4. the tooltips -------------------------------------------------------------------

check("the buttons' tooltips name the chord in the platform's glyph, the console button's convention", () => {
  const html = require("fs").readFileSync(require("path").join(__dirname, "..", "..", "aish", "static", "index.html"), "utf8");
  assert(html.includes('id="console-btn" class="bar-btn icon" title="console (⌘/Ctrl+\\)"'), "the convention being mirrored");
  // The titles are written where the send tooltip is, on a fine pointer.
  const block = extract(src, "if (FINE_POINTER) {", "// Grabber: drag down to dismiss");
  assert(block.includes('$("new-chip").title = `new chat (${CHORD_HINTS.new})`'), block);
  assert(block.includes('$("sessions-new").title = `new chat (${CHORD_HINTS.new})`'), block);
  assert(block.includes('$("model-chip").title = `switch model (${CHORD_HINTS.models} · local ⇄ cloud: ${CHORD_HINTS.modelToggle})`'), block);
});

check("the chats button names the toggle chord — its tap is the toggle in every state", () => {
  // The REAL syncRailToggle, with the hints present (as in the app) and absent
  // (as test_session_rail.js loads it).
  const run = (docked, showing, hints, fine) => {
    const chip = { title: "", attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } };
    const sandbox = { $: (id) => (id === "back-chip" ? chip : null), railDocked: () => docked, railIsOpen: () => showing };
    if (hints) { sandbox.CHORD_HINTS = hints; sandbox.FINE_POINTER = fine; }
    vm.createContext(sandbox);
    vm.runInContext(surface(extract(src, "function syncRailToggle() {", "function clearRailDragStyles")), sandbox);
    sandbox.syncRailToggle();
    return chip;
  };
  const mac = chords("MacIntel").CHORD_HINTS;
  // Every state names ⌘B, because ⌘B does what the tap does in every state.
  assert.equal(run(false, false, mac, true).title, "Chats (⌘B)");
  assert.equal(run(false, true, mac, true).title, "Chats (⌘B)");
  assert.equal(run(true, false, mac, true).title, "Show chats (⌘B)");
  assert.equal(run(true, true, mac, true).title, "Hide chats (⌘B)");
  const win = chords("Win32").CHORD_HINTS;
  assert.equal(run(true, true, win, true).title, "Hide chats (Ctrl+B)");
  assert.equal(run(false, false, win, true).title, "Chats (Ctrl+B)");
  // aria-label never carries the chord; a coarse pointer gets no hint; the
  // block loaded alone (no hints) keeps the bare titles the rail test pins.
  assert.equal(run(true, true, mac, true).attrs["aria-label"], "Hide chats");
  assert.equal(run(true, true, mac, false).title, "Hide chats");
  assert.equal(run(true, true, null).title, "Hide chats");
  // And ⌘B really is the chord that hides a docked, showing rail.
  const s = chords("MacIntel");
  assert.equal(s.navChord(key("b", { metaKey: true })), "rail");
});

if (failures) { console.error(`nav chords: ${failures} check(s) failed`); process.exit(1); }
console.log("nav chords: all checks passed");
