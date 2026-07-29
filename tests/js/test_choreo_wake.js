// Choreography pin for the wake handler (issue #181, phase 1).
//
// Waking the app is where the client's assumptions are weakest: the phone slept,
// so every deferred step the code was counting on simply never ran, and the
// socket may have died without emitting a single event. Three unrelated
// subsystems are reconciled in one listener — the offline mirror, the pager's
// parked transcript, and the connection — and each of the four #181 incidents
// touched at least one of them.
//
// This drives the REAL [WAKE] block, registered the way it ships (by name,
// through document.addEventListener — the test dispatches the event rather than
// calling the function, so a broken registration fails too), over the REAL
// [UNPARK] block, in the hostile world: no frame ever fires, no transition ever
// settles, and every timer is data. The invariant being pinned is the pager's:
// **an idle transcript is on screen** — and waking up is the last line of
// defence for it, because a swipe committed just before the lock has no other
// path back.
//
// Run manually: node tests/js/test_choreo_wake.js
"use strict";

const { hostileWorld, checks } = require("./harness");

const { ok, report } = checks();

function wakeWorld({ visible = true, ws = null, idleMs = 0 } = {}) {
  const log = [];
  const w = hostileWorld({
    visible,
    globals: {
      swipeInFrom: 0,
      lastActiveAt: Date.now() - idleMs,
      COLD_START_GAP_MS: 45 * 60 * 1000,
      ws,
      offlineSyncOnce() { log.push("offlineSyncOnce"); },
      coldStartRecompute() { log.push("coldStartRecompute"); },
      noteActivity() { log.push("noteActivity"); },
      connect() { log.push("connect"); },
    },
  });
  w.load("// [UNPARK-START]", "// [UNPARK-END]");
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
  ok("hiding does not count as activity", w.count("noteActivity") === 0);
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

// ---- 3. The parked transcript: the incident, end to end -------------------
// Swipe commits (transcript off-screen) → the landing replay arrives and STAGES
// the entry at the far side → the phone locks before the frame that starts the
// slide, so nothing ever moves. swipeInFrom is already 0: no further replay is
// coming, and without this handler the chat stays blank forever.
{
  const w = wakeWorld({ visible: true });
  w.sandbox.parkTranscript(-1, 1100);
  ok("committed: transcript parked off screen", w.sandbox.transcriptDisplaced());

  w.sandbox.reconcilePager(-1); // the landing
  ok("the landing staged the entry side", w.sandbox.transcriptDisplaced());
  ok("no frame fired — the slide never started", w.frames.length === 1);
  ok("no swipe is in flight any more", w.sandbox.swipeInFrom === 0);

  w.setVisible(false);
  w.wake();                                    // phone locks
  w.setVisible(true);
  w.wake();                                    // …and comes back
  ok("waking puts the transcript back on screen", !w.sandbox.transcriptDisplaced());
  ok("recovery does not depend on a frame ever running", w.frames.length === 1);
}

// ---- 3b. A swipe still in flight is NOT yanked ----------------------------
// The landing may yet arrive; unparking here would jump-cut a legitimate
// animation. The park's own deadline owns this case (see test_pager_unpark).
{
  const w = wakeWorld({ visible: true });
  w.sandbox.parkTranscript(1, 1100);
  w.wake();
  ok("a wake mid-swipe leaves the committed park alone",
    w.sandbox.transcriptDisplaced() && w.sandbox.swipeInFrom === 1);
  // …and the deadline armed at park time still recovers it, unaided.
  w.fireOne((t) => t.ms >= 1000);
  ok("the park deadline is still the backstop", !w.sandbox.transcriptDisplaced());
}

// ---- 3c. A transcript already at rest is not touched ----------------------
{
  const w = wakeWorld({ visible: true });
  w.wake();
  ok("an at-rest transcript stays at rest", !w.sandbox.transcriptDisplaced());
}

// ---- 4. Cold start is measured BEFORE activity is noted ------------------
// noteActivity resets the very gap the sweep measures, so the order here is
// load-bearing: reversed, the working set could never be swept on a wake.
{
  const stale = wakeWorld({ idleMs: 60 * 60 * 1000 });
  stale.wake();
  ok("returning after a long absence sweeps the working set",
    stale.count("coldStartRecompute") === 1);
  ok("the sweep runs before activity is noted",
    stale.log.indexOf("coldStartRecompute") < stale.log.indexOf("noteActivity"));

  const fresh = wakeWorld({ idleMs: 1000 });
  fresh.wake();
  ok("a brief glance away sweeps nothing", fresh.count("coldStartRecompute") === 0);
  ok("…but still counts as activity", fresh.count("noteActivity") === 1);
}

// ---- 5. The hostile default really is hostile ----------------------------
// If a harness change ever starts firing frames or settling transitions by
// itself, scenario 3 would pass for the wrong reason. Prove the world withholds
// them: a staged landing that is never woken stays displaced.
{
  const w = wakeWorld({ visible: true });
  w.sandbox.parkTranscript(-1, 1100);
  w.sandbox.reconcilePager(-1);
  ok("nothing recovers a staged landing on its own", w.sandbox.transcriptDisplaced());
}

report("test_choreo_wake.js");
