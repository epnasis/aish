// The seen ledger's client half: unread belongs to the OWNER, not to this
// screen (#232).
//
// It used to be the other way — each browser kept its own map and it never
// left, on the reasoning that "I looked at it" is a fact about a screen. For a
// product with many users that is right. aish has one owner and one pair of
// eyes, so a chat read on the phone whose dot is still on the laptop is the app
// making a false claim about the person using it.
//
// What this file pins is the four properties the sharing rests on, each of
// which has a way of going wrong that the user would see:
//
//   1. MAX, NEVER REPLACE. Merging both directions by max is what makes the
//      ledger conflict-free. A replace anywhere lets an older stamp arriving
//      late flip a chat you just opened back to unread.
//   2. THE OUTBOX. A chat read while offline is a normal thing; the mark has to
//      survive until a server confirms it, and be retired when one does — or
//      it either never reaches the other devices, or grows forever.
//   3. ONE CLOCK. The last-output stamp and the last-look stamp are compared,
//      so they must be in the same frame. A device whose clock runs fast must
//      not stamp its own optimistic mark in its own time and hide an answer.
//   4. THE UPGRADE SEEDS ITSELF. A map written by the old build is unknown to
//      the server, so all of it is offered once — no migration flag to forget.
//
// The REAL [SEEN] block is extracted from app.js and run here.
//
// Run manually: node tests/js/test_seen_sync.js
"use strict";

const { sessionWorld, checks } = require("./harness");

const { ok, report } = checks();

const SEC = 1000;

/** A world holding the REAL seen block, with a socket that records sends. */
function seenWorld({ stored = null, connected = true } = {}) {
  const sent = [];
  const w = sessionWorld({
    globals: {
      ws: connected ? { readyState: 1, send: (text) => sent.push(JSON.parse(text)) } : null,
    },
  });
  w.sandbox.WebSocket = { OPEN: 1 };
  if (stored) w.storage.setItem("aish-seen", JSON.stringify(stored));
  w.load("// [SEEN-START]", "// [SEEN-END]");
  w.sent = sent;
  w.stored = () => JSON.parse(w.storage.getItem("aish-seen"));
  return w;
}

const NOW = Date.now();
const secs = (ms) => ms / SEC;

// ---- 1. reading here tells the other devices ------------------------------
{
  const w = seenWorld();
  const s = w.sandbox;
  s.markSeen("a.jsonl");

  ok("reading a chat hands the mark to the server",
    w.sent.length === 1 && w.sent[0].type === "seen" && "a.jsonl" in w.sent[0].marks);
  ok("…in epoch SECONDS, the ledger's unit",
    Math.abs(w.sent[0].marks["a.jsonl"] - secs(Date.now())) < 5);
  ok("…and the badge moves HERE, not a round trip later", w.called("refreshBadge") === 1);
  ok("a live mark does not ask for the whole ledger back", w.sent[0].full === false);
}

// ---- 2. reading THERE clears the dot here ---------------------------------
// The whole point of the change: no tap on this device, no round trip from it.
{
  const w = seenWorld();
  const s = w.sandbox;
  s.seenSince = NOW - 3600 * SEC;
  const row = { name: "b.jsonl", out: secs(NOW - 60 * SEC) };
  const state = () => ({ seen: s.seenAt, since: s.seenSince, current: null });

  ok("a chat with output since this device's floor is unread",
    s.sessionUnread(row, state()) === true);
  s.applySeenMarks({ "b.jsonl": secs(NOW - 30 * SEC) }); // read on the phone
  ok("the owner reading it elsewhere clears it here",
    s.sessionUnread(row, state()) === false);
  ok("…and the count is told", w.called("refreshBadge") >= 1);
}

// ---- 3. max, never replace ------------------------------------------------
// The property that makes arrival order irrelevant. Without it, a stamp from a
// device that read the chat EARLIER lands late and un-reads what you just read.
{
  const w = seenWorld();
  const s = w.sandbox;
  s.markSeen("c.jsonl");
  const mine = s.seenAt["c.jsonl"];
  s.applySeenMarks({ "c.jsonl": secs(NOW - 600 * SEC) }); // an older look, arriving late
  ok("an older stamp cannot walk a newer one back", s.seenAt["c.jsonl"] === mine);

  const before = s.seenAt["c.jsonl"];
  s.markSeen("c.jsonl");
  ok("nor can this device's own clock, if it has not moved",
    s.seenAt["c.jsonl"] >= before);
}

// ---- 4. the outbox --------------------------------------------------------
{
  const w = seenWorld({ connected: false });
  const s = w.sandbox;
  s.markSeen("d.jsonl");
  ok("a chat read with no socket still counts as read HERE", s.seenAt["d.jsonl"] > 0);
  ok("…nothing is sent, and nothing is TOASTED about it",
    w.sent.length === 0 && w.called("showToast") === 0);
  ok("…and the mark waits in the outbox", w.stored().pending["d.jsonl"] > 0);

  // Reconnect: the flush is what carries an offline read to the other devices.
  s.ws = { readyState: 1, send: (text) => w.sent.push(JSON.parse(text)) };
  s.syncSeen(secs(Date.now()));
  ok("a connect offers what the server has not confirmed",
    w.sent.length === 1 && "d.jsonl" in w.sent[0].marks);
  ok("…and asks for the whole ledger back, both directions in one message",
    w.sent[0].full === true);

  s.applySeenMarks({ "d.jsonl": s.seenAt["d.jsonl"] / SEC });
  ok("a confirmed mark leaves the outbox — it must not grow forever",
    !("d.jsonl" in w.stored().pending));
}

// A mark the server has NOT confirmed stays pending, or an offline read is lost
// the first time the ledger answers about some other chat.
{
  const w = seenWorld();
  const s = w.sandbox;
  s.markSeen("e.jsonl");
  s.applySeenMarks({ "other.jsonl": secs(NOW) });
  ok("an unrelated confirmation does not retire a pending mark",
    w.stored().pending["e.jsonl"] > 0);
}

// ---- 5. one clock ---------------------------------------------------------
// A device five minutes fast would otherwise stamp its own "I read this" in its
// own time and hide an answer that arrived while it was reading.
{
  const w = seenWorld();
  const s = w.sandbox;
  const serverSeconds = secs(Date.now() - 300 * SEC); // this device runs 5 min fast
  s.syncSeen(serverSeconds);
  s.markSeen("f.jsonl");
  ok("an optimistic mark is stamped in the SERVER's frame, not this device's",
    Math.abs(s.seenAt["f.jsonl"] - serverSeconds * SEC) < 5 * SEC);

  const w2 = seenWorld();
  w2.sandbox.markSeen("g.jsonl");
  ok("a server too old to send its clock leaves the device's own — the old behaviour",
    Math.abs(w2.sandbox.seenAt["g.jsonl"] - Date.now()) < 5 * SEC);
}

// The ledger's own answer re-adopts the clock: the stamps and the frame they
// mean anything in have to arrive together.
{
  const w = seenWorld();
  const s = w.sandbox;
  const serverSeconds = secs(Date.now() + 120 * SEC);
  s.onSeenLedger({ now: serverSeconds, seen: { "h.jsonl": serverSeconds - 10 } });
  s.markSeen("i.jsonl");
  ok("the ledger's clock is adopted with its stamps",
    Math.abs(s.seenAt["i.jsonl"] - serverSeconds * SEC) < 5 * SEC);
  ok("…and its stamps land", Math.abs(s.seenAt["h.jsonl"] - (serverSeconds - 10) * SEC) < 5 * SEC);
}

// ---- 6. the upgrade seeds itself ------------------------------------------
// A map written by a build that kept unread to itself is entirely unknown to
// the server. Offering all of it once is what stops the first sync from
// resurrecting every dot the owner had already cleared.
{
  const w = seenWorld({
    stored: { at: { "old1.jsonl": NOW - 100 * SEC, "old2.jsonl": NOW - 200 * SEC },
              since: NOW - 9000 * SEC },
  });
  const s = w.sandbox;
  s.syncSeen(secs(Date.now()));
  ok("every stamp the old build kept locally is offered to the ledger",
    "old1.jsonl" in w.sent[0].marks && "old2.jsonl" in w.sent[0].marks);

  // …but only once. A map that already knows about the ledger offers nothing.
  const w2 = seenWorld({
    stored: { at: { "known.jsonl": NOW - 100 * SEC }, since: NOW - 9000 * SEC, pending: {} },
  });
  w2.sandbox.syncSeen(secs(Date.now()));
  ok("a map already reconciled offers nothing back",
    Object.keys(w2.sent[0].marks).length === 0);
  ok("…but still asks, so a device that missed a broadcast catches up",
    w2.sent[0].full === true);
}

// ---- 7. the floor stays this device's -------------------------------------
// It answers "what has this SCREEN had a chance to show me", which is why it is
// not shared — and it can only ever move something toward read.
{
  const w = seenWorld();
  const s = w.sandbox;
  s.seenSince = NOW - 60 * SEC;
  const old = { name: "ancient.jsonl", out: secs(NOW - 9000 * SEC) };
  ok("a chat that predates this device's first run is read, ledger or no ledger",
    s.sessionUnread(old, { seen: s.seenAt, since: s.seenSince, current: null }) === false);
  ok("the floor is not offered to the server", !("since" in (w.sent[0]?.marks || {})));
}

// ---- 8. nothing ever UNSEES a chat ----------------------------------------
// [FORGET-SESSION] is where this last tried to happen: dropping a stamp is what
// brought a deleted chat back at the TOP of the list under "Needs you".
{
  const w = seenWorld();
  const s = w.sandbox;
  s.markSeen("z.jsonl");
  const stamp = s.seenAt["z.jsonl"];
  s.applySeenMarks({ "z.jsonl": 0 });
  s.applySeenMarks({ "z.jsonl": null });
  s.applySeenMarks({});
  s.applySeenMarks(null);
  ok("no shape of empty answer clears a stamp", s.seenAt["z.jsonl"] === stamp);
}

report("test_seen_sync.js");
