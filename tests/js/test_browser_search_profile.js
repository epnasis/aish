// Node-only, dependency-free check that `/browser anon <url>` drives the
// SEARCH profile and plain `/browser <url>` does not (#264).
//
// The two profiles look identical on screen and mean opposite things: one
// carries every session the owner has, the other carries none. Routing a
// sign-in into the wrong one is a mistake nothing else on the page would
// reveal, so the routing is pinned here — against the REAL handleSlash and the
// REAL openBrowserView pulled out of app.js, never a copy of them.
//
// Run manually: node tests/js/test_browser_search_profile.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

const calls = [];
const store = {};
// A DOM stub small enough to read and real enough to run the shipped renderer:
// elements have children, classes, text and a click that fires its listeners.
function element(tag) {
  const el = {
    tagName: tag, className: "", type: "", hidden: false, value: "",
    children: [], listeners: {}, _text: "",
    removeAttribute() {}, focus() {},
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
    click() { (this.listeners.click || []).forEach((fn) => fn()); },
    get textContent() {
      return this.children.length
        ? this.children.map((c) => c.textContent).join(" ")
        : this._text;
    },
    set textContent(v) { this._text = String(v); this.children = []; },
  };
  return el;
}

const nodes = {};
function node(id) {
  if (!nodes[id]) nodes[id] = element("div");
  return nodes[id];
}

const sandbox = {
  $: node,
  document: { createElement: element },
  openSheet(name) { calls.push(`sheet:${name}`); },
  send(message) { calls.push(`send:${JSON.stringify(message)}`); return true; },
  bvSend(message) { calls.push(`bvSend:${JSON.stringify(message)}`); return true; },
  bvResetZoom() {},
  bvViewportSize() { return { width: 1024, height: 1400 }; },
  openBrowserView: null,
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
  },
};
vm.createContext(sandbox);
vm.runInContext(
  (extract("const SLASH_COMMANDS = [", "];") + "];\n" +
   extract("const SLASH_ALL", "function handleSlash") +
   extract("function handleSlash", "// ---- attachments") +
   extract("// [BROWSER-VIEW-RECENT-START]", "/** The SHAPE the remote page")
  // ONLY top-level declarations, which is why this is anchored to the line
  // start rather than the blunt /\bconst\b/g the older checks use. A `const`
  // inside `for (const item of items)` rewritten to `var` gives every closure
  // in the loop the LAST item — the shipped code was fine and the harness made
  // it fail, which is the worst kind of red.
  ).replace(/^(\s*)(const|let)\b/gm, "$1var"),
  sandbox);

assert(typeof sandbox.handleSlash === "function", "failed to extract handleSlash");
assert(typeof sandbox.openBrowserView === "function", "failed to extract openBrowserView");

let failures = 0;
function check(name, fn) {
  calls.length = 0;
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { failures++; console.error(`FAIL - ${name}\n       ${err.message}`); }
}

function opened() {
  const line = calls.find((c) => c.startsWith("bvSend:"));
  assert(line, `no view was opened; calls were ${JSON.stringify(calls)}`);
  return JSON.parse(line.slice("bvSend:".length));
}

check("/browser anon <url> opens the view on the search profile", () => {
  sandbox.handleSlash("/browser anon accounts.google.com");
  const message = opened();
  assert.strictEqual(message.profile, "search");
  assert.strictEqual(message.url, "accounts.google.com");
});

check("plain /browser <url> stays on the owner's profile", () => {
  sandbox.handleSlash("/browser eon.pl");
  assert.strictEqual(opened().profile, "");
});

check("the search profile is named on screen, not just in the message", () => {
  sandbox.handleSlash("/browser anon accounts.google.com");
  assert(/search profile/.test(node("bv-status").textContent),
         `status said: ${node("bv-status").textContent}`);
});

check("the label survives the first frame, when it starts to matter", () => {
  // Measured against the real page: the frame handler replaced the whole
  // status line with the page title, so by the time there was a sign-in form
  // on screen, nothing said which browser it belonged to.
  sandbox.handleSlash("/browser anon accounts.google.com");
  assert(/search profile/.test(sandbox.bvLabel("Sign in")),
         `frame label said: ${sandbox.bvLabel("Sign in")}`);
  sandbox.handleSlash("/browser eon.pl");
  assert.strictEqual(sandbox.bvLabel("Mój E.ON"), "Mój E.ON");
});

check("bare /browser anon is a question, and asks it as text", () => {
  const handled = sandbox.handleSlash("/browser anon");
  assert.strictEqual(handled, true);
  assert(calls.some((c) => c.startsWith("send:") && /"arg":"anon"/.test(c)),
         `expected a text query; calls were ${JSON.stringify(calls)}`);
  assert(!calls.some((c) => c.startsWith("bvSend:")), "must not open a view");
});

check("an empty browser asks the SERVER what it has open recently", () => {
  // Not localStorage: where aish's browser has been is a fact about that one
  // Chrome, not about the phone looking at it, so it must read the same from
  // every device.
  sandbox.openBrowserView("");
  assert(calls.some((c) => c === 'bvSend:{"action":"recent"}'),
         `expected a recent request; calls were ${JSON.stringify(calls)}`);
});

check("a recent row reopens in the profile it was opened in", () => {
  sandbox.bvRenderRecent([
    { url: "https://accounts.google.com/", host: "accounts.google.com",
      title: "Sign in", profile: "search" },
    { url: "https://eon.pl/mojeon", host: "eon.pl", title: "Mój E.ON", profile: "" },
  ]);
  const rows = nodes["bv-recent"].children;
  assert.strictEqual(rows.length, 2, "both rows should render");
  rows[0].click();
  assert.strictEqual(opened().profile, "search");
  calls.length = 0;
  rows[1].click();
  assert.strictEqual(opened().profile, "");
});

check("the anonymous profile is named on the row that would reopen it", () => {
  sandbox.bvRenderRecent([
    { url: "https://x.pl/", host: "x.pl", title: "X", profile: "search" },
  ]);
  assert(/anonymous profile/.test(nodes["bv-recent"].textContent),
         `row said: ${nodes["bv-recent"].textContent}`);
});

check("no recents means no empty list box", () => {
  sandbox.bvRenderRecent([]);
  assert.strictEqual(nodes["bv-recent"].hidden, true);
});

process.exit(failures === 0 ? 0 : 1);
