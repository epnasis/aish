// Node-only, dependency-free check that `/browser search <url>` drives the
// SEARCH profile and plain `/browser <url>` does not (#249).
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
const nodes = {};
function node(id) {
  if (!nodes[id]) {
    nodes[id] = {
      value: "", textContent: "", hidden: false, onclick: null,
      removeAttribute() {}, focus() {},
    };
  }
  return nodes[id];
}

const sandbox = {
  $: node,
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
   extract("// Which profile the OPEN view is driving", "/** The SHAPE the remote page")
  ).replace(/\bconst\b/g, "var").replace(/\blet\b/g, "var"),
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

check("/browser search <url> opens the view on the search profile", () => {
  sandbox.handleSlash("/browser search accounts.google.com");
  const message = opened();
  assert.strictEqual(message.profile, "search");
  assert.strictEqual(message.url, "accounts.google.com");
});

check("plain /browser <url> stays on the owner's profile", () => {
  sandbox.handleSlash("/browser eon.pl");
  assert.strictEqual(opened().profile, "");
});

check("the search profile is named on screen, not just in the message", () => {
  sandbox.handleSlash("/browser search accounts.google.com");
  assert(/search profile/.test(node("bv-status").textContent),
         `status said: ${node("bv-status").textContent}`);
});

check("the label survives the first frame, when it starts to matter", () => {
  // Measured against the real page: the frame handler replaced the whole
  // status line with the page title, so by the time there was a sign-in form
  // on screen, nothing said which browser it belonged to.
  sandbox.handleSlash("/browser search accounts.google.com");
  assert(/search profile/.test(sandbox.bvLabel("Sign in")),
         `frame label said: ${sandbox.bvLabel("Sign in")}`);
  sandbox.handleSlash("/browser eon.pl");
  assert.strictEqual(sandbox.bvLabel("Mój E.ON"), "Mój E.ON");
});

check("bare /browser search is a question, and asks it as text", () => {
  const handled = sandbox.handleSlash("/browser search");
  assert.strictEqual(handled, true);
  assert(calls.some((c) => c.startsWith("send:") && /"arg":"search"/.test(c)),
         `expected a text query; calls were ${JSON.stringify(calls)}`);
  assert(!calls.some((c) => c.startsWith("bvSend:")), "must not open a view");
});

check("Resume reopens the profile it was last used on", () => {
  store["aish-bv-last"] = "https://accounts.google.com/";
  store["aish-bv-last-profile"] = "search";
  sandbox.openBrowserView("");
  node("bv-resume").onclick();
  assert.strictEqual(opened().profile, "search");
});

process.exit(failures === 0 ? 0 : 1);
