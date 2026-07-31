// Choreography pin for issue #182 — the session firewall on live deliveries.
//
// Most live events used to carry no session identity. A client that switches
// views CLIENT-SIDE first — commitPage's prefetched swipe paint, or an
// offline-mirror tap that later reconnects — keeps receiving the old session's
// events until the server processes the resume and moves the viewer between
// bridges. In that window the old turn's `done` rendered a bubble into the new
// chat's view; phase 3's answerAbandoned only covers one turn's token/trace
// leftovers, not deliveries in general.
//
// The fix has two halves: the bridge stamps every event with its session name
// (pinned server-side in TestSessionStamp), and the real ws.onmessage drops a
// stamped event whose session is not the one on screen. This test runs the
// shipped [SESSION-FIREWALL] + [CONNECT-WIRE] blocks (plus the real
// [SESSION-ENTER] owner for the client-side switch itself) against harness.js's
// controllable socket, and pins the interleaving: identity moves first, the old
// session's tail keeps arriving, and none of it reaches the dispatcher.
//
// Run manually: node tests/js/test_choreo_session_firewall.js
"use strict";

const { sessionWorld, checks } = require("./harness");

const { ok, report } = checks();

// sessionWorld gives the real [SESSION-ENTER] owner plus URL/storage; layer on
// the socket-lifecycle globals connect() reads (same set as
// test_choreo_socket_replace) and load the firewall + wiring fences.
function firewallWorld() {
  const seen = [];
  const w = sessionWorld({
    globals: {
      // --- socket lifecycle state (declared above [RETIRE] in app.js) ---
      ws: null,
      backoff: 1000,
      reconnectTimer: null,
      connWarnTimer: null,
      CONN_WARN_DELAY: 2000,
      BASE: "/",
      token: "tok",
      connOk: true,
      currentSession: null,
      VIEW_SAFE_EVENTS: new Set(["hello", "session_list", "role", "session_state"]),
      updateDot() {},
      setOfflineMode() {},
      offlineSyncSoon() {},
      checkAppVersion() {},
      handle(event) { seen.push(event); },
    },
  });
  w.load("// [RETIRE-START]", "// [RETIRE-END]");
  w.load("// [SESSION-FIREWALL-START]", "// [SESSION-FIREWALL-END]");
  w.load("// [CONNECT-WIRE-START]", "// [CONNECT-WIRE-END]");
  w.seenEvents = seen;
  w.types = () => seen.map((e) => e.type);
  return w;
}

// ---- 1. The switch window: the old session's tail never reaches handle() ---
{
  const w = firewallWorld();
  w.sandbox.connect();
  const socket = w.sockets[0].open();

  // The server showed chat A; its hello moved identity through the real owner.
  w.sandbox.enterSession("session-a.jsonl");
  socket.deliver({ type: "token", text: "a1", session: "session-a.jsonl" });
  ok("the viewed session's events flow", w.seenEvents.length === 1);

  // The client-side switch: a prefetched swipe commits — commitPage moves
  // identity FIRST (openCachedSession's order), the resume is still in flight.
  w.sandbox.enterSession("session-b.jsonl");

  // A's live tail keeps arriving until the server moves this viewer.
  w.sandbox.viewDirty = false;
  socket.deliver({ type: "token", text: "a2", session: "session-a.jsonl" });
  socket.deliver({ type: "status", state: "idle", session: "session-a.jsonl" });
  socket.deliver({ type: "done", result: "old answer", session: "session-a.jsonl" });
  ok("the old session's tail is dropped whole", w.seenEvents.length === 1);
  ok("a dropped delivery never dirties the NEW view", w.sandbox.viewDirty === false);

  // B's own events (post-resume) flow normally.
  socket.deliver({ type: "token", text: "b1", session: "session-b.jsonl" });
  ok("the new session's events flow", w.types().pop() === "token");
  ok("…and dirty the view as before", w.sandbox.viewDirty === true);
}

// ---- 2. Unstamped and cross-session-by-design events always pass ----------
{
  const w = firewallWorld();
  w.sandbox.connect();
  const socket = w.sockets[0].open();
  w.sandbox.enterSession("session-b.jsonl");

  // Client-direct messages carry no stamp: "not scoped, always deliver".
  socket.deliver({ type: "session_list", sessions: [] });
  socket.deliver({ type: "deck_gone", names: [] });
  // hello is the switch mechanism itself — it must reach onHello to move the
  // view, so it is exempt even when it names another session.
  socket.deliver({ type: "hello", session: "session-c.jsonl" });
  // session_state is sent ONLY to non-viewers ("a background chat finished").
  socket.deliver({ type: "session_state", session: "session-a.jsonl", state: "idle" });
  // session_renamed's handler is by-name and idempotent; dropping it in the
  // window would just desync a drawer/pager label.
  socket.deliver({ type: "session_renamed", name: "session-a.jsonl", title: "t", session: "session-a.jsonl" });
  ok(
    "unstamped + exempt events all reach the dispatcher",
    w.types().join(",") === "session_list,deck_gone,hello,session_state,session_renamed"
  );
}

// ---- 3. Before any session is entered, stamped events are foreign too -----
{
  // currentSession is null only before the first hello/boot paint, and the
  // server never fans live events at a socket before its hello — but if one
  // ever arrived, rendering it into an empty view would be the same bug.
  const w = firewallWorld();
  w.sandbox.connect();
  const socket = w.sockets[0].open();
  socket.deliver({ type: "done", result: "x", session: "session-a.jsonl" });
  ok("a stamped event with no view on screen is dropped", w.seenEvents.length === 0);
  socket.deliver({ type: "session_list", sessions: [] });
  ok("an unstamped one still passes", w.types().join(",") === "session_list");
}

report("test_choreo_session_firewall");
