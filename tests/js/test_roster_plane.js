// The roster plane: "chat X is now …", for every chat, whatever this client
// is looking at (#204).
//
// It replaces a pull, and the pull is why four repairs in a row did not hold.
// The list of chats and what they were doing refreshed only when this client
// ASKED — which it did on opening the rail — so between opens the roster
// decayed, and each fix widened when it re-asks rather than making the server
// tell it. Never announced at all: a chat STARTING, an approval ANSWERED
// somewhere else, a chat renamed or deleted elsewhere.
//
// What is pinned here is the part that cannot be taken on faith — a stream is
// only as good as its ability to notice it dropped something:
//   - a row applied from a delta reaches the count with no request at all;
//   - a GAP in the sequence asks for a snapshot instead of applying over rows
//     that may already be wrong;
//   - no gap asks for nothing (a stream that re-snapshots constantly is a poll
//     wearing a costume);
//   - a snapshot re-baselines the sequence, so deltas already folded into it
//     do not read as a gap;
//   - the heads-up is suppressed for the chat you are looking at, while the
//     row still travels — every list needs the row, only a reader elsewhere
//     needs interrupting;
//   - a deleted chat leaves the count with it.
//
// Run manually: node tests/js/test_roster_plane.js
"use strict";

const { sessionWorld, checks } = require("./harness");

const { ok, report } = checks();
const SEC = 1000;

function rosterWorld() {
  const w = sessionWorld({ globals: {
    ws: { readyState: 1 },
    WebSocket: { OPEN: 1 },
    // OPEN — the case that matters. A docked rail repaints on every delta, and
    // repainting must not ASK: doing that turned the plane into a poll
    // triggered by its own events, which the count below is what caught.
    railIsOpen: () => true,
    requestSessions() {},
    renderOfflineSessions() {},
    notify() {},
  } });
  w.load("// PURE: the whole unread decision", "// [SEEN-END]");
  w.load("// SESSIONS_PARTITION_START", "// SESSIONS_PARTITION_END");
  w.load("// [ATTENTION-START]", "// [ATTENTION-END]");
  w.load("// [ROSTER-START]", "// [ROSTER-END]");
  const s = w.sandbox;
  s.seenAt = {};
  s.seenSince = Date.now() - 3600 * SEC;
  s.currentSession = "here.jsonl";
  return w;
}

const changed = (seq, name, state, extra = {}) =>
  ({ type: "session_changed", seq, row: { name, state, title: name }, ...extra });

const snapshots = (w) =>
  w.calls.filter((c) => c.name === "send" && c.args[0] && c.args[0].type === "sessions").length;

// ---- a delta lands, with nobody asking for it -----------------------------
{
  const w = rosterWorld();
  const s = w.sandbox;
  s.setAttentionRows([{ name: "bg.jsonl", ts: (Date.now() - 3600 * SEC) / SEC, state: "" }]);
  s.onRosterSeq(0);

  s.onSessionChanged(changed(1, "bg.jsonl", "waiting"));
  ok("a chat that stopped for approval reaches the count with no request",
    [...s.attentionSessions].join() === "bg.jsonl");
  ok("…and nothing was asked of the server to learn it", snapshots(w) === 0);

  s.onSessionChanged(changed(2, "bg.jsonl", "running"));
  ok("the approval being answered elsewhere clears it again — the case that"
    + " left a phone showing Needs approval for a card cleared on the laptop",
    s.attentionSessions.size === 0);
}

// ---- a gap is the one thing a stream must notice --------------------------
{
  const w = rosterWorld();
  const s = w.sandbox;
  s.onRosterSeq(4);

  s.onSessionChanged(changed(5, "a.jsonl", "running"));
  ok("an in-order delta asks for nothing", snapshots(w) === 0);

  s.onSessionChanged(changed(9, "b.jsonl", "waiting"));
  ok("a gap asks for a snapshot rather than trusting what it still holds",
    snapshots(w) === 1);

  s.onSessionChanged(changed(10, "b.jsonl", "running"));
  ok("and the stream carries on from there without re-asking",
    snapshots(w) === 1);
}

// ---- a snapshot re-baselines it -------------------------------------------
{
  const w = rosterWorld();
  const s = w.sandbox;
  s.onRosterSeq(0);
  // The server answered a snapshot taken at 12; deltas 1..12 are already in it.
  s.onRosterSeq(12);
  s.onSessionChanged(changed(13, "a.jsonl", "running"));
  ok("the delta after a snapshot is not a gap", snapshots(w) === 0);
}

// ---- the heads-up is not the row ------------------------------------------
{
  const w = rosterWorld();
  const s = w.sandbox;
  s.onRosterSeq(0);

  s.onSessionChanged(changed(1, "here.jsonl", "idle", { notice: "finished" }));
  ok("no interruption about the chat in front of you",
    w.calls.filter((c) => c.name === "showToast").length === 0);
  ok("…but its row travelled all the same — every list needs it",
    s.attentionRows.some((r) => r.name === "here.jsonl"));

  s.onSessionChanged(changed(2, "bg.jsonl", "idle", { notice: "finished" }));
  ok("a chat that finished elsewhere does interrupt",
    w.calls.filter((c) => c.name === "showToast").length === 1);
}

// ---- a chat that is gone leaves the count with it -------------------------
{
  const w = rosterWorld();
  const s = w.sandbox;
  s.onRosterSeq(0);
  s.onSessionChanged(changed(1, "doomed.jsonl", "idle", { notice: "finished" }));
  ok("counted while it existed", [...s.attentionSessions].join() === "doomed.jsonl");

  s.forgetAttention("doomed.jsonl");
  ok("deleting it elsewhere takes it out of the count too",
    s.attentionSessions.size === 0);
}

report("test_roster_plane.js");
