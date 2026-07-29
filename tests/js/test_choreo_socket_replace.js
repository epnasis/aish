// Choreography pin for incident #179 — the duplicated live feed (issue #181).
//
// connect() is reached from five places (first load, the backoff timer, the
// manual Reconnect, a foregrounding phone, a rev-check reload) and each assigns
// a fresh socket to `ws`. Assigning the variable does NOT dispose the socket it
// replaced: an orphan left OPEN keeps its onmessage alive and goes on delivering
// live events, so every bubble and every timeline step rendered once PER
// survivor. test_socket_retire.js pins the DECISION (retireSocket neutralizes a
// socket handed to it). This pins the CHOREOGRAPHY — that replacing a socket for
// real, through the shipped connect(), leaves exactly one feed — because the
// decision was never the thing that broke.
//
// Runs the REAL [RETIRE] + [CONNECT-WIRE] blocks and the real reconnect(),
// against harness.js's controllable FakeWebSocket in the hostile world (timers
// are data, so a stray reconnect scheduled by a zombie's onclose is visible
// rather than merely eventual).
//
// Run manually: node tests/js/test_choreo_socket_replace.js
"use strict";

const assert = require("assert");
const { hostileWorld, checks } = require("./harness");

const { ok, report } = checks();

// The globals connect()/reconnect() read that live outside the fenced blocks.
function socketWorld() {
  const seen = [];
  const els = {};
  const el = (id) => (els[id] ||= { hidden: true, focus() {}, textContent: "" });
  const w = hostileWorld({
    globals: {
      // --- socket lifecycle state (declared above [RETIRE] in app.js) ---
      ws: null,
      backoff: 1000,
      reconnectTimer: null,
      connWarnTimer: null,
      CONN_WARN_DELAY: 2000,
      // --- URL construction ---
      BASE: "/",
      location: { protocol: "https:", host: "aish.test", search: "" },
      token: "tok",
      storeSession: (n) => n,
      localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
      // --- everything the handlers poke, recorded or inert ---
      $: el,
      connOk: true,
      offlineMode: false,
      viewDirty: false,
      currentSession: null, // read by the real [SESSION-FIREWALL] fence (#182)
      VIEW_SAFE_EVENTS: new Set(["role"]),
      swipeInFrom: 0,
      updateDot() {},
      setOfflineMode() {},
      offlineSyncSoon() {},
      checkAppVersion() {},
      showToast() {},
      hideBootLoader() {},
      reconcilePager() { seen.push("reconcilePager"); },
      handle(event) { seen.push(event); },
    },
  });
  w.load("// [RETIRE-START]", "// [RETIRE-END]");
  w.load("// [SESSION-FIREWALL-START]", "// [SESSION-FIREWALL-END]"); // ws.onmessage calls it (#182)
  w.load("// [CONNECT-WIRE-START]", "// [CONNECT-WIRE-END]");
  // reconnect() sits just below the fence; extracted by its declaration, the
  // same way the deck/pager tests reach unfenced neighbours.
  w.load("function reconnect() {", "// New chat over a socket");
  w.seen = seen;
  w.live = () => w.sockets.filter((s) => s.live);
  return w;
}

// ---- 1. A replaced socket cannot feed the dispatcher ----------------------
{
  const w = socketWorld();

  w.sandbox.connect();
  ok("connect creates a socket", w.sockets.length === 1);
  ok("`ws` points at it", w.sandbox.ws === w.sockets[0]);
  const first = w.sockets[0].open();

  ok("the first socket feeds handle()", first.deliver({ type: "token", text: "a" }));
  ok("delivered exactly once", w.seen.length === 1);

  // The second connect — a foregrounding phone, say, over a socket that is
  // still perfectly OPEN. This is the #179 interleaving verbatim.
  w.sandbox.connect();
  ok("a second socket exists", w.sockets.length === 2);
  const second = w.sockets[1].open();

  ok("the replaced socket is fully neutralized", first.retired);
  ok("the replaced socket was closed", first.closeCalls === 1);
  ok("exactly ONE socket can still feed handle()", w.live().length === 1);

  const before = w.seen.length;
  first.deliver({ type: "token", text: "ghost" });
  ok("an event on the replaced socket reaches the dispatcher ZERO times",
    w.seen.length === before);
  second.deliver({ type: "token", text: "real" });
  ok("an event on the live socket reaches it exactly once",
    w.seen.length === before + 1 && w.seen[before].text === "real");
}

// ---- 2. A retired socket's late onclose is inert --------------------------
// The orphan's death arrives AFTER its replacement is live. If its onclose were
// still wired it would schedule another connect on the backoff ladder — a third
// socket nobody asked for, and the double-feed grows rather than heals.
{
  const w = socketWorld();
  w.sandbox.connect();
  const first = w.sockets[0].open();
  w.sandbox.connect();
  w.sockets[1].open();

  const armedBefore = w.armed().length;
  ok("the retired socket's onclose no longer exists", first.fireClose({ code: 1006 }) === false);
  ok("so no stray reconnect is scheduled", w.armed().length === armedBefore);
  ok("and no third socket appears", w.sockets.length === 2);
}

// ---- 3. The zombie: OPEN, silent, and only a manual reconnect saves it ----
// readyState stays OPEN and close() changes nothing observable, so NOTHING
// announces the death — the failure mode #18/#179 both bottom out in. What must
// hold is that reconnect() still ends with exactly one live feed.
{
  const w = socketWorld();
  w.sandbox.connect();
  const dead = w.sockets[0].open().zombie();

  ok("a zombie still looks OPEN", dead.readyState === w.WebSocket.OPEN);
  ok("…and answers nothing", dead.deliver({ type: "token", text: "x" }) === false);

  w.sandbox.reconnect();
  ok("reconnect built a new socket", w.sockets.length === 2);
  const fresh = w.sockets[1].open();
  ok("the zombie can no longer feed the dispatcher", !dead.live);
  ok("exactly one live socket after reconnecting over a zombie", w.live().length === 1);

  const before = w.seen.length;
  dead.deliver({ type: "done", result: "ghost" });
  fresh.deliver({ type: "done", result: "real" });
  ok("only the fresh socket's event lands", w.seen.length === before + 1);
}

// ---- 4. Repeated reconnects never accumulate feeds ------------------------
// The backoff ladder plus a wake plus a manual tap can all fire within a second
// of each other. However many sockets get built, the invariant is one feed.
{
  const w = socketWorld();
  for (let i = 0; i < 5; i += 1) {
    w.sandbox.connect();
    w.sockets[w.sockets.length - 1].open();
  }
  ok("five connects built five sockets", w.sockets.length === 5);
  ok("…and left exactly one live feed", w.live().length === 1);
  ok("every predecessor was closed",
    w.sockets.slice(0, 4).every((s) => s.closeCalls === 1 && s.retired));

  const before = w.seen.length;
  for (const s of w.sockets) s.deliver({ type: "token", text: "once" });
  ok("one server event renders once, not five times", w.seen.length === before + 1);
}

// ---- 5. Structural: `ws` is assigned nowhere else -------------------------
// The choreography above only holds while connect() is the sole writer. The
// general form of this check (with the owner named in the failure) lives in
// test_ownership.js; this is the local restatement so the pin is self-contained.
{
  const { appSource, extract } = require("./harness");
  const src = appSource();
  const wire = extract(src, "// [CONNECT-WIRE-START]", "// [CONNECT-WIRE-END]");
  const all = (src.match(/^\s*ws = (?!=)/gm) || []).length;
  const owned = (wire.match(/^\s*ws = (?!=)/gm) || []).length;
  assert.strictEqual(all, owned,
    `every assignment to \`ws\` must live in [CONNECT-WIRE] (${owned}/${all} do)`);
  ok("`ws` has exactly one writer, inside [CONNECT-WIRE]", owned === 1);
}

report("test_choreo_socket_replace.js");
