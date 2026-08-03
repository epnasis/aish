// Choreography pin for the delete acknowledgement ([DELETE-ACK], #210).
//
// The failure this exists for is not a wrong decision, it is a message that
// never arrived. A chat was deleted from the phone, the confirmation closed,
// and the log file was still on the server hours later — because the socket
// reported OPEN long after it died, swallowed the request, and no event ever
// came back to say otherwise. Nothing in the client was waiting for one.
//
// So this drives the REAL confirm action and the REAL ack window in the hostile
// world, where timers are data (a deadline that was never armed is a visible
// failure) and a socket can go zombie (OPEN, accepting sends, answering
// nothing) — the exact interleaving the incident took.
//
// Run manually: node tests/js/test_choreo_delete_ack.js
"use strict";

const { hostileWorld, checks } = require("./harness");

const { ok, report } = checks();

const CHAT = "session-20260802-203450-623975.jsonl";

function deleteWorld({ socket = "open" } = {}) {
  const toasts = [];
  const log = [];
  // Offline is a DEAD socket plus the mode flag — the mode is how the app knows
  // to say "offline" rather than "reconnecting", never a second way to be down.
  const ws = { readyState: socket === "open" ? 1 : 3, sent: [] };
  const w = hostileWorld({
    visible: true,
    globals: {
      ws,
      offlineMode: socket === "offline",
      currentSession: CHAT,
      showToast: (text) => toasts.push(text),
      reconnect: () => log.push("reconnect"),
      // Collaborators the confirm block and the resolve paths reach for.
      $: (id) => {
        if (!w.els[id]) {
          w.els[id] = w.sandbox.document.createElement("div");
          w.els[id].focus = () => {};
        }
        return w.els[id];
      },
      rosterBaseline() {},
      forgetSession: () => Promise.resolve(log.push("forgetSession")),
      forgetAttention() {},
      offlineForget: () => Promise.resolve(),
      railIsOpen: () => false,
      renderSessionsFromCache() {},
      abandonPendingView() {},
      awaitingPaint: null,
      prefetched: new Map(),
      viewCache: new Map(),
      send: (message) => {
        if (ws.readyState !== 1) {
          toasts.push(w.sandbox.offlineMode ? "offline — you can read past chats, not send" : "not connected");
          return false;
        }
        ws.sent.push(message);
        return true;
      },
    },
  });
  w.els = {};
  w.load("// [DELETE-ACK-START]", "// [DELETE-ACK-END]");
  w.load("// [CONFIRM-START]", "// [CONFIRM-END]");
  // The REAL handlers for the server's two answers, so their wiring to the
  // wait is part of what is pinned and not something the test re-states.
  w.load("function onSessionGone(name) {", "// [DELETE-ACK-START]");
  w.toasts = toasts;
  w.log = log;
  w.ws = ws;
  w.lastToast = () => toasts[toasts.length - 1] || "";
  // Answer the modal the way a finger does.
  w.confirmDelete = () => { w.sandbox.askDeleteChat(); w.sandbox.runConfirmed(); };
  w.ackTimer = () => w.armed((t) => t.ms === w.sandbox.DELETE_ACK_MS);
  return w;
}

// ---- 1. The happy path closes the wait -----------------------------------
{
  const w = deleteWorld();
  w.confirmDelete();
  ok("the request went out", w.ws.sent.length === 1 && w.ws.sent[0].name === CHAT);
  ok("and a deadline is watching it", w.ackTimer().length === 1);

  w.sandbox.onSessionDeleted({ name: CHAT, seq: 7 });
  ok("the server's answer disarms the deadline", w.ackTimer().length === 0);
  ok("…and nothing warns about a delete that worked",
    !w.toasts.some((t) => /NOT deleted/.test(t)));
}

// ---- 2. The incident: a socket that is OPEN and dead ----------------------
{
  const w = deleteWorld();
  w.confirmDelete();
  ok("a zombie socket accepts the request like any other", w.ws.sent.length === 1);
  ok("nothing has been said yet — the answer may still be coming",
    !/NOT deleted/.test(w.lastToast()));

  w.fireOne((t) => t.ms === w.sandbox.DELETE_ACK_MS); // no answer ever comes
  ok("silence is reported, in the words that matter", /NOT deleted/.test(w.lastToast()));
  ok("…and the dead socket is rebuilt so a retry has something to send on",
    w.log.includes("reconnect"));
  ok("the delete is NEVER resent on its own", w.ws.sent.length === 1);
}

// ---- 3. A refusal is an answer, not silence ------------------------------
{
  const w = deleteWorld();
  w.confirmDelete();
  // What the server sends for delete-while-busy: refused, and NAMING the chat.
  w.sandbox.resolveDelete(CHAT);
  ok("a refusal that names the chat disarms the deadline", w.ackTimer().length === 0);

  const other = deleteWorld();
  other.confirmDelete();
  other.sandbox.resolveDelete("session-somebody-else.jsonl");
  ok("…but a refusal about ANOTHER chat does not", other.ackTimer().length === 1);
  other.sandbox.resolveDelete("");
  ok("…nor does an unnamed error", other.ackTimer().length === 1);
}

// ---- 4. Already gone is also an answer -----------------------------------
{
  const w = deleteWorld();
  w.confirmDelete();
  w.sandbox.onSessionGone(CHAT);
  ok("deleting a chat that was already gone ends the wait", w.ackTimer().length === 0);
}

// ---- 5. No socket at all: say so, and arm nothing -------------------------
{
  const w = deleteWorld({ socket: "closed" });
  w.confirmDelete();
  ok("nothing was sent", w.ws.sent.length === 0);
  ok("the answer names the ACTION that failed, not just the connection",
    /not deleted/i.test(w.lastToast()));
  ok("and no deadline waits for an answer to a request never made",
    w.ackTimer().length === 0);

  const off = deleteWorld({ socket: "offline" });
  off.confirmDelete();
  ok("offline says which of the two it is", /offline/.test(off.lastToast()));
}

// ---- 6. Two deletes in a row leave exactly one wait -----------------------
{
  const w = deleteWorld();
  w.confirmDelete();
  w.sandbox.currentSession = "session-second.jsonl";
  w.confirmDelete();
  ok("the second replaces the first, never stacks", w.ackTimer().length === 1);
  w.sandbox.onSessionDeleted({ name: "session-second.jsonl", seq: 8 });
  ok("…and it is the second one that gets answered", w.ackTimer().length === 0);
}

report("test_choreo_delete_ack.js");
