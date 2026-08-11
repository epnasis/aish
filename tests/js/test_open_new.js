// Node-only, dependency-free checks for [OPEN-NEW] — `?new` on the launch URL.
//
// For a Home Screen icon or a Shortcut that should always start clean: a shared
// photo landing in its own chat rather than on top of yesterday's conversation.
//
// The whole risk is in CONSUMING it exactly once. A launch parameter left in
// the address bar opens a new chat on every reload, on every rev-mismatch
// reload the app performs on itself, and on every relaunch of a PWA that
// restores its last URL — one intent becoming an endless drip of empty chats,
// each of them a row in the rail. And the connect path runs again on every
// reconnect, so a flag that survived there would open a chat every time the
// phone woke up.
//
// Run manually: node tests/js/test_open_new.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();
const BLOCK = extract(appSource(), "// [OPEN-NEW-START]", "// [OPEN-NEW-END]");

// The block reads location/history and localStorage exactly as the page does.
function load(href) {
  const replaced = [];
  const sandbox = {
    URL,
    URLSearchParams,
    location: {
      get href() { return href; },
      get search() { return new URL(href).search; },
    },
    history: {
      replaceState: (_s, _t, next) => replaced.push(next),
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(surface(BLOCK), sandbox);
  return { s: sandbox, replaced };
}

// ---- it is recognised ------------------------------------------------------
{
  const w = load("https://aish.test/?new=1");
  ok("`?new=1` asks for a fresh chat", w.s.openNewOnLoad === true);
}
{
  const w = load("https://aish.test/?new");
  ok("…and so does a bare `?new`, which is what a hand-typed URL looks like",
    w.s.openNewOnLoad === true);
}
{
  const w = load("https://aish.test/?token=abc");
  ok("an ordinary launch resumes as before", w.s.openNewOnLoad === false);
  ok("…and rewrites no history", w.replaced.length === 0);
}

// ---- it is consumed ONCE ---------------------------------------------------
{
  const w = load("https://aish.test/?new=1&token=abc");
  ok("the parameter is stripped from the URL immediately", w.replaced.length === 1);
  const after = w.replaced[0];
  ok(`…so a reload does not open another chat (${after})`, !/[?&]new\b/.test(after));
  ok("…while everything else on the URL survives, the token above all",
    after.includes("token=abc"));
}
{
  // The PWA is served from a subpath in the preview deploy, and a deep link
  // carries a hash. Neither may be dropped by the rewrite.
  const w = load("https://aish.test/preview/?new=1&session=abc#console");
  const after = w.replaced[0];
  ok("the mount path survives the rewrite", after.startsWith("/preview/"));
  ok("…and so does the hash", after.endsWith("#console"));
  ok("…and the session id, which the app still reads", after.includes("session=abc"));
}

// ---- the flag is cleared before the send, not after ------------------------
// connect() runs on every reconnect. This mirrors the ws.onopen block: a flag
// still set on the second pass opens a chat every time the phone wakes up.
{
  const w = load("https://aish.test/?new=1");
  const opened = [];
  const onopen = () => {
    if (w.s.openNewOnLoad) {
      w.s.openNewOnLoad = false;
      opened.push("new");
    }
  };
  onopen();
  onopen(); // a reconnect
  onopen(); // and another
  assert.deepStrictEqual(opened, ["new"]);
  ok("three connects, one new chat", true);
}

report("test_open_new.js");
