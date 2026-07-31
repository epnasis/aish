// Choreography pin for the wake handler (issue #181, phase 1).
//
// Waking the app is where the client's assumptions are weakest: the phone slept,
// so every deferred step the code was counting on simply never ran, and the
// socket may have died without emitting a single event. Two subsystems are
// reconciled in one listener — the offline mirror and the connection.
//
// (A third used to live here: the swipe pager's parked transcript. The pager is
// gone with the working-set deck — switching chats goes through the session rail
// now — and with it the whole class of "the transcript is off screen waiting for
// a landing that never came" failures this handler was the last defence against.
// Nothing replaced it because nothing needs to: the transcript never leaves.)
//
// This drives the REAL [WAKE] block, registered the way it ships (by name,
// through document.addEventListener — the test dispatches the event rather than
// calling the function, so a broken registration fails too), in the hostile
// world: no frame ever fires, no transition ever settles, every timer is data.
//
// Run manually: node tests/js/test_choreo_wake.js
"use strict";

const { hostileWorld, checks } = require("./harness");

const { ok, report } = checks();

function wakeWorld({ visible = true, ws = null } = {}) {
  const log = [];
  const w = hostileWorld({
    visible,
    globals: {
      ws,
      offlineSyncOnce() { log.push("offlineSyncOnce"); },
      connect() { log.push("connect"); },
    },
  });
  w.load("// [WAKE-START]", "// [WAKE-END]");
  w.log = log;
  w.count = (what) => log.filter((c) => c === what).length;
  w.wake = () => w.dispatch("visibilitychange");
  return w;
}

// A socket in a chosen readyState, without the ceremony of a real connect().
const socketIn = (w, state) => {
  const s = new w.WebSocket("wss://aish.test/ws");
  s.readyState = state;
  return s;
};

// ---- 0. The handler really is wired ---------------------------------------
{
  const w = wakeWorld();
  ok("the wake handler is registered as a visibilitychange listener",
    (w.listeners.visibilitychange || []).length === 1);
  ok("it is registered by NAME, not as an anonymous closure",
    typeof w.sandbox.onPageWake === "function"
    && w.listeners.visibilitychange[0] === w.sandbox.onPageWake);
}

// ---- 1. Going away syncs the mirror and does nothing else -----------------
// A hidden page's timers may never fire, which is why this call is direct and
// not debounced — and why nothing else may run on this branch.
{
  const w = wakeWorld({ visible: false });
  w.wake();
  ok("hiding syncs the mirror once", w.count("offlineSyncOnce") === 1);
  ok("hiding connects nothing", w.count("connect") === 0);
}

// ---- 2. The connection: reconnect only when it is provably dead -----------
{
  const dead = wakeWorld({ ws: null });
  dead.sandbox.ws = socketIn(dead, dead.WebSocket.CLOSED);
  dead.wake();
  ok("a CLOSED socket reconnects exactly once", dead.count("connect") === 1);

  const none = wakeWorld({ ws: null });
  none.wake();
  ok("no socket at all reconnects exactly once", none.count("connect") === 1);

  // The stacking cases: an attempt already in flight must not be doubled, and
  // a working socket must not be thrown away. Both would produce the #179
  // shape — more sockets than feeds are wanted — from the wake path instead.
  const connecting = wakeWorld();
  connecting.sandbox.ws = socketIn(connecting, connecting.WebSocket.CONNECTING);
  connecting.wake();
  connecting.wake();
  ok("a CONNECTING socket is left alone, however many wakes arrive",
    connecting.count("connect") === 0);

  const open = wakeWorld();
  open.sandbox.ws = socketIn(open, open.WebSocket.OPEN);
  open.wake();
  ok("an OPEN socket is left alone", open.count("connect") === 0);
}

report("test_choreo_wake.js");
