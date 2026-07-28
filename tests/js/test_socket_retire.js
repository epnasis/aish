// Node-only, dependency-free regression check for the double-socket live-event
// duplication bug: connect() used to overwrite the module `ws` variable without
// disposing the socket it replaced, so an orphaned socket left OPEN kept its
// onmessage handler alive and went on delivering live events into handle().
// Every user bubble and every timeline step then rendered once PER surviving
// socket (replay is immune — it replaceChildren()s — so only the live "working"
// state doubled). retireSocket is the single-owner teardown connect() now calls
// before overwriting `ws`. Pulls the REAL retireSocket out of app.js by marker
// and runs it in an isolated vm against a fake socket — so this exercises the
// shipped code, not a copy.
//
// Run manually: node tests/js/test_socket_retire.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(extract("// [RETIRE-START]", "// [RETIRE-END]"), sandbox);
assert(typeof sandbox.retireSocket === "function", "failed to extract retireSocket");

function fakeSocket() {
  return {
    onmessage: () => {},
    onopen: () => {},
    onclose: () => {},
    onerror: () => {},
    closed: 0,
    close() { this.closed += 1; },
  };
}

// 1. A live socket is fully neutralized: every handler nulled, close() called.
const s = fakeSocket();
sandbox.retireSocket(s);
assert.strictEqual(s.onmessage, null, "onmessage must be nulled so it can't double-feed handle()");
assert.strictEqual(s.onopen, null, "onopen must be nulled");
assert.strictEqual(s.onclose, null, "onclose must be nulled so no stray reconnect is scheduled");
assert.strictEqual(s.onerror, null, "onerror must be nulled");
assert.strictEqual(s.closed, 1, "the retired socket must be closed exactly once");

// 2. Null/undefined is a no-op (connect() calls it on the very first connect,
//    when `ws` is still null).
sandbox.retireSocket(null);
sandbox.retireSocket(undefined);

// 3. A socket whose close() throws (already CLOSING/CLOSED) is swallowed — the
//    handlers are still cleared, which is the load-bearing half.
const throwing = fakeSocket();
throwing.close = () => { throw new Error("InvalidStateError"); };
sandbox.retireSocket(throwing);
assert.strictEqual(throwing.onmessage, null, "handlers cleared even when close() throws");

// 4. The property the whole fix rests on: after retiring the predecessor, only
//    the NEW socket's onmessage is live, so one server event reaches handle()
//    exactly once. Model connect()'s ordering: retire old, then install new.
let handleCalls = 0;
const handle = () => { handleCalls += 1; };
const oldSock = fakeSocket();
oldSock.onmessage = handle;
// connect() does: retireSocket(ws); ws = new WebSocket(...); ws.onmessage = ...
sandbox.retireSocket(oldSock);
const newSock = fakeSocket();
newSock.onmessage = handle;
// A server event arrives; deliver to whatever sockets still have a live handler.
for (const sock of [oldSock, newSock]) if (sock.onmessage) sock.onmessage();
assert.strictEqual(handleCalls, 1, "exactly one live feed after retiring the old socket");

console.log("test_socket_retire.js: all assertions passed");
