// Node-only, dependency-free check for WHERE a console URL opens: this device's
// own browser, or aish's remote one (the browser that screenshots a page and is
// signed in to sites). Runs the REAL [CONSOLE-LINK-TARGET] block from app.js —
// together with the REAL [CONSOLE-LINKS] provider it asks — against a fake DOM.
//
// Three properties are what make this more than a menu:
//
//  1. The hold is claimed ONLY on a URL. A stationary hold on the console has
//     always meant Select mode (drag to paint, then Copy); taking it for a link
//     menu everywhere would have cost the console its copy gesture.
//  2. The menu and the tap agree on what a link IS, because both ask the same
//     provider. A second URL scanner would offer the fragment on the row that
//     was pressed for any URL wrapped across rows — which is most of them, since
//     the URLs worth opening from a console are auth links.
//  3. The lift after a hold is refused. iOS synthesises a click on whatever was
//     under the finger, so without the guard the link opened in the default
//     browser BEHIND the menu that was still asking where to open it.
//
// Run manually: node tests/js/test_console_link_menu.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

let checks = 0;
function ok(label, cond) { assert(cond, label); checks += 1; }

// ---- the world the block runs in ----------------------------------------

const COLS = 40;
const ROWS = 10;
const CELL = 10; // px per cell, both axes — the screen box is COLS*CELL wide

// A fake xterm: rows of text starting at buffer row `startY`, with the viewport
// scrolled so buffer row `startY` is the first row on screen.
function fakeTerm(rows, startY = 5) {
  const lines = {};
  rows.forEach((r, k) => { lines[startY + k] = r; });
  return {
    cols: COLS,
    rows: ROWS,
    buffer: { active: {
      viewportY: startY,
      getLine: (y) => {
        const r = lines[y];
        if (r == null) return null;
        return { isWrapped: !!r.wrapped, translateToString: () => r.text };
      },
    } },
  };
}

// The point at the centre of a cell, given a row INDEX on screen (0-based) and
// a column index (0-based).
function pointAt(col, row) {
  return { x: col * CELL + CELL / 2, y: row * CELL + CELL / 2 };
}

function world() {
  const nodes = {};
  const opened = [];   // window.open + openBrowserView, in order
  const toasts = [];
  const el = (id) => (nodes[id] = nodes[id] || {
    id, hidden: true, textContent: "", style: {}, offsetWidth: 240, offsetHeight: 210,
  });
  const store = new Map();
  const screen = {
    querySelector: () => null, // no .xterm-screen child in the fake: use the box itself
    getBoundingClientRect: () => ({
      left: 0, top: 0, width: COLS * CELL, height: ROWS * CELL,
      right: COLS * CELL, bottom: ROWS * CELL,
    }),
  };
  const sandbox = {
    URL,
    $: el,
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
    },
    window: { innerWidth: 400, innerHeight: 800, open: (u) => opened.push(`system:${u}`) },
    hideConsole: () => opened.push("hide-console"),
    openBrowserView: (u) => opened.push(`aish:${u}`),
    showToast: (t) => toasts.push(t),
  };
  vm.createContext(sandbox);
  // Both blocks share the context: consoleLinkAt asks consoleLinkProvider, and
  // pinning that they agree is half the point of this file.
  vm.runInContext(extract("// [CONSOLE-LINKS-START]", "// [CONSOLE-LINKS-END]"), sandbox);
  vm.runInContext(
    extract("// [CONSOLE-LINK-TARGET-START]", "// [CONSOLE-LINK-TARGET-END]")
      .replace(/^const |^let /gm, "var "),
    sandbox,
  );
  return { sandbox, el, opened, toasts, screen, store };
}

const URL_TEXT = "https://accounts.example.com/o/oauth2/auth?code=ABCDEF0123456789";

// A URL that wraps across two rows the way tmux repaints one: NOT flagged
// isWrapped, so only the geometry (a row packed to the last column) joins them.
function wrappedRows(prefix, url) {
  const line = prefix + url;
  const rows = [];
  for (let i = 0; i < line.length; i += COLS) {
    rows.push({ text: line.slice(i, i + COLS), wrapped: false });
  }
  return rows;
}

// ---- 1. the hit test ------------------------------------------------------
{
  const w = world();
  const term = fakeTerm([{ text: `see ${URL_TEXT.slice(0, 30)}`, wrapped: false }]);
  const on = pointAt(10, 0); // inside the URL, which starts at column 4
  ok("a point on the URL finds it",
    w.sandbox.consoleLinkAt(term, w.screen, on.x, on.y) === URL_TEXT.slice(0, 30));
  const before = pointAt(1, 0);
  ok("a point on the same row BEFORE the URL finds nothing",
    w.sandbox.consoleLinkAt(term, w.screen, before.x, before.y) === null);
  const otherRow = pointAt(10, 3);
  ok("a point on a row with no URL finds nothing",
    w.sandbox.consoleLinkAt(term, w.screen, otherRow.x, otherRow.y) === null);
  ok("a point outside the screen finds nothing",
    w.sandbox.consoleLinkAt(term, w.screen, -5, 5) === null);
}

// 2. THE AGREEMENT: a hold on the SECOND row of a wrapped URL must offer the
// whole URL, not the fragment printed on the row that was pressed.
{
  const w = world();
  const rows = wrappedRows("visit ", URL_TEXT);
  ok("the fixture really does wrap", rows.length > 1);
  const term = fakeTerm(rows);
  const second = pointAt(3, 1);
  ok("a hold on the continuation row offers the WHOLE url",
    w.sandbox.consoleLinkAt(term, w.screen, second.x, second.y) === URL_TEXT);
}

// ---- 3. the tap follows the remembered default ---------------------------
{
  const w = world();
  ok("out of the box, links open where they always did (this device's browser)",
    w.sandbox.consoleLinkTarget() === "system");
  w.sandbox.openConsoleLink(URL_TEXT);
  ok("…so a tap hands it to the OS", w.opened.join() === `system:${URL_TEXT}`);

  w.sandbox.setConsoleLinkTarget("aish");
  w.opened.length = 0;
  w.sandbox.openConsoleLink(URL_TEXT);
  ok("with the default switched, a tap opens the remote browser",
    w.opened.join() === `hide-console,aish:${URL_TEXT}`);
  ok("and the console is hidden FIRST — a sheet paints under the console overlay",
    w.opened.indexOf("hide-console") < w.opened.indexOf(`aish:${URL_TEXT}`));
}

// ---- 4. an explicit choice does not rewrite the default ------------------
{
  const w = world();
  w.sandbox.openConsoleLink(URL_TEXT, "aish");
  ok("choosing the aish browser once opens it", w.opened.join().includes(`aish:${URL_TEXT}`));
  ok("…and leaves the tap default alone", w.sandbox.consoleLinkTarget() === "system");

  w.sandbox.setConsoleLinkTarget("aish");
  w.opened.length = 0;
  w.sandbox.openConsoleLink(URL_TEXT, "system");
  ok("and the other way round, too",
    w.opened.join() === `system:${URL_TEXT}` && w.sandbox.consoleLinkTarget() === "aish");
}

// ---- 5. the hold: a link asks, anything else still selects ---------------
{
  const w = world();
  const term = fakeTerm([{ text: `see ${URL_TEXT.slice(0, 30)}`, wrapped: false }]);
  const off = pointAt(1, 0);
  ok("a hold off a URL leaves Select mode to the caller",
    w.sandbox.consoleHoldAt(term, w.screen, off.x, off.y) === "select");
  ok("…and raises nothing", w.el("clink-menu").hidden === true);

  const on = pointAt(10, 0);
  ok("a hold ON a URL claims the gesture",
    w.sandbox.consoleHoldAt(term, w.screen, on.x, on.y) === "link");
  ok("…the menu is up", w.el("clink-menu").hidden === false);
  ok("…with its scrim, which is what a tap outside lands on",
    w.el("clink-scrim").hidden === false);
  ok("…saying which URL it is about, as TEXT",
    w.el("clink-url").textContent === URL_TEXT.slice(0, 30));
  ok("…and showing the current default", w.el("clink-default-state").textContent === "your browser");
  ok("nothing was opened by asking", w.opened.length === 0);

  // 6. THE REGRESSION SHAPE: the lift's synthesised click, while the menu asks.
  ok("the tap that follows the hold is refused",
    w.sandbox.openConsoleLink(URL_TEXT) === null);
  ok("…so nothing opened behind the menu", w.opened.length === 0);
  ok("…and the menu is still asking", w.el("clink-menu").hidden === false);

  // The choice itself is honoured while the menu is open — it IS the menu.
  w.sandbox.openConsoleLink(URL_TEXT, "aish");
  ok("choosing from the menu opens it", w.opened.join().includes(`aish:${URL_TEXT}`));
  ok("…and takes the menu down", w.el("clink-menu").hidden === true);
  ok("…and its scrim with it", w.el("clink-scrim").hidden === true);
}

// ---- 7. the hold is not discoverable, so it is said once -----------------
{
  const w = world();
  w.sandbox.openConsoleLink(URL_TEXT);
  ok("the first link ever opened says how to choose",
    w.toasts.length === 1 && /hold a link/.test(w.toasts[0]));
  w.sandbox.openConsoleLink(URL_TEXT);
  w.sandbox.openConsoleLink(URL_TEXT);
  ok("and never again", w.toasts.length === 1);
}
{
  const w = world();
  w.sandbox.openConsoleLink(URL_TEXT, "system");
  ok("a choice made IN the menu is never told how to open the menu",
    w.toasts.length === 0);
}

// ---- 8. the toggle flips the stored default ------------------------------
{
  const w = world();
  w.sandbox.setConsoleLinkTarget(w.sandbox.consoleLinkTarget() === "aish" ? "system" : "aish");
  ok("the menu's own row flips it", w.sandbox.consoleLinkTarget() === "aish");
  ok("…and says so where it was tapped",
    w.el("clink-default-state").textContent === "aish browser");
  w.sandbox.setConsoleLinkTarget(w.sandbox.consoleLinkTarget() === "aish" ? "system" : "aish");
  ok("…and back", w.sandbox.consoleLinkTarget() === "system");
}

// ---- 9. no storage at all must not break the console ---------------------
{
  const w = world();
  w.sandbox.localStorage = {
    getItem() { throw new Error("denied"); },
    setItem() { throw new Error("denied"); },
  };
  ok("a device that refuses storage still has a default",
    w.sandbox.consoleLinkTarget() === "system");
  w.sandbox.openConsoleLink(URL_TEXT);
  ok("…and still opens links", w.opened.join() === `system:${URL_TEXT}`);
  ok("…without a hint it cannot promise to show only once", w.toasts.length === 0);
}

console.log(`console link menu: ${checks} checks passed`);
