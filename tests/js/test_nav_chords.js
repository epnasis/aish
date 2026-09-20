// The navigation chords (#384) — the REAL [NAV-CHORDS] block and the REAL
// document keydown handler out of app.js, on a macOS and on a Windows/Linux
// platform.
//
// What is pinned:
//
//   1. New chat is ⌘⇧O on macOS and Ctrl+Shift+O elsewhere; search chats is
//      ⌘K and ⌘⇧K (Ctrl+K / Ctrl+Shift+K). The platform is DETECTED and the
//      other modifier is not accepted: on macOS Ctrl+K is the text field's own
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

check("macOS: ⌘⇧O is new chat, ⌘K and ⌘⇧K are search; Ctrl is not accepted", () => {
  const s = chords("MacIntel");
  assert.equal(s.IS_MAC, true);
  assert.equal(s.navChord(key("O", { metaKey: true, shiftKey: true })), "new");
  assert.equal(s.navChord(key("o", { metaKey: true, shiftKey: true })), "new");
  assert.equal(s.navChord(key("k", { metaKey: true })), "search");
  assert.equal(s.navChord(key("K", { metaKey: true, shiftKey: true })), "search");
  // The Emacs bindings of a macOS text field stay with the field.
  assert.equal(s.navChord(key("k", { ctrlKey: true })), null, "Ctrl+K is kill-line on macOS");
  assert.equal(s.navChord(key("o", { ctrlKey: true, shiftKey: true })), null);
  assert.equal(s.navChord(key("n", { ctrlKey: true })), null, "Ctrl+N is next-line on macOS");
  assert.equal(s.navChord(key("p", { ctrlKey: true })), null, "Ctrl+P is previous-line on macOS");
  // Both modifiers at once is neither.
  assert.equal(s.navChord(key("k", { metaKey: true, ctrlKey: true })), null);
  // Alt is never a navigation chord.
  assert.equal(s.navChord(key("k", { metaKey: true, altKey: true })), null);
  // The older combos still resolve.
  assert.equal(s.navChord(key("o", { metaKey: true })), "rail");
  assert.equal(s.navChord(key("p", { metaKey: true, shiftKey: true })), "rail");
  assert.equal(s.navChord(key("p", { metaKey: true })), "export");
  assert.equal(s.navChord(key("n", { metaKey: true })), "new");
  // A plain key is nothing.
  assert.equal(s.navChord(key("k")), null);
  assert.equal(s.navChord(key("k", { shiftKey: true })), null);
  assert.deepEqual(s.CHORD_HINTS, { new: "⌘⇧O", search: "⌘K" });
});

check("Windows/Linux: Ctrl+Shift+O is new chat, Ctrl+K and Ctrl+Shift+K are search; ⌘ (Win key) is not accepted", () => {
  for (const platform of ["Win32", "Linux x86_64"]) {
    const s = chords(platform);
    assert.equal(s.IS_MAC, false, platform);
    assert.equal(s.navChord(key("o", { ctrlKey: true, shiftKey: true })), "new", platform);
    assert.equal(s.navChord(key("k", { ctrlKey: true })), "search", platform);
    assert.equal(s.navChord(key("k", { ctrlKey: true, shiftKey: true })), "search", platform);
    assert.equal(s.navChord(key("k", { metaKey: true })), null, `${platform}: the Win key is not the primary modifier`);
    assert.equal(s.navChord(key("o", { metaKey: true, shiftKey: true })), null, platform);
    // Ctrl+Alt is AltGr on many layouts: never a chord.
    assert.equal(s.navChord(key("k", { ctrlKey: true, altKey: true })), null, platform);
    assert.deepEqual(s.CHORD_HINTS, { new: "Ctrl+Shift+O", search: "Ctrl+K" }, platform);
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
  const sandbox = {
    navigator: { platform, userAgent: "" }, Boolean, String, Set, Map, Array, Object,
    document: { addEventListener: (type, fn) => { if (type === "keydown") handler = fn; }, activeElement: active, querySelectorAll: () => [] },
    $: el,
    closePreview: () => false, previewKey: () => false, ssIsOpen: () => false, ssClose() {}, ssTape() {}, ssScrollPage() {},
    confirmIsOpen: () => modal, closeConfirm() {}, consoleLinkMenuOpen: () => false, closeConsoleLinkMenu() {},
    escapeExit: () => false, railIsOpen: () => false, closeSheets: () => calls.push("closeSheets"),
    editingNow: () => false, CARD_SHORTCUTS: [], activeApprovalCard: () => null,
    requestNewChat: () => calls.push("requestNewChat"), openSessionRail: (q) => calls.push(`openSessionRail:${q}`),
    toggleSessionRail: () => calls.push("toggleSessionRail"), exportSessionPdf: () => calls.push("exportSessionPdf"),
    toggleConsole: () => calls.push("toggleConsole"),
    requestAnimationFrame: (fn) => fn(),
  };
  vm.createContext(sandbox);
  vm.runInContext(surface(extract(src, "// [NAV-CHORDS-START]", "// Desktop only: auto-focusing on a phone")), sandbox);
  assert(typeof handler === "function", "the keydown handler was not registered");
  return { calls, fire: (e) => { let prevented = false; handler({ ...e, preventDefault: () => { prevented = true; } }); return prevented; } };
}

check("⌘K opens the chat list and focuses its search field through the one handler; ⌘⇧O goes the header button's way", () => {
  const w = handlerWorld("MacIntel");
  assert.equal(w.fire(key("k", { metaKey: true })), true, "the browser's own ⌘K is prevented");
  assert.deepEqual(w.calls, ["openSessionRail:", "focus:sessions-search", "select:sessions-search"]);
  w.calls.length = 0;
  assert.equal(w.fire(key("O", { metaKey: true, shiftKey: true })), true);
  assert.deepEqual(w.calls, ["requestNewChat", "closeSheets"], "new chat is the reconnect-aware path the button uses");
  w.calls.length = 0;
  // Ctrl+K on macOS falls through untouched.
  assert.equal(w.fire(key("k", { ctrlKey: true })), false);
  assert.deepEqual(w.calls, []);
  // Ctrl+\ is still the console's own toggle, either modifier.
  assert.equal(w.fire(key("\\", { ctrlKey: true })), true);
  assert.deepEqual(w.calls, ["toggleConsole"]);
});

check("on Windows/Linux the same handler answers to Ctrl", () => {
  const w = handlerWorld("Win32");
  assert.equal(w.fire(key("k", { ctrlKey: true, shiftKey: true })), true);
  assert.deepEqual(w.calls, ["openSessionRail:", "focus:sessions-search", "select:sessions-search"]);
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
  // The chats button's title is syncRailToggle's (one writer), and it carries the search chord.
  const rail = extract(src, "function syncRailToggle() {", "function clearRailDragStyles");
  assert(rail.includes("CHORD_HINTS.search"), rail);
});

if (failures) { console.error(`nav chords: ${failures} check(s) failed`); process.exit(1); }
console.log("nav chords: all checks passed");
