// Choreography pin for the acknowledgement ledger ([ACK-LEDGER], #210).
//
// The failure this exists for is not a wrong decision, it is a request that
// never arrived. A chat was deleted from a phone, the confirmation closed, and
// the log file was still on the server hours later — the socket reported OPEN
// long after it died, swallowed the request, and nothing was waiting for an
// answer. The audit that followed found the same shape in a dozen places, six
// of which had already told the user it worked: the approval card greyed out,
// the queue chip vanished, the header said "Stopping…", the title changed.
//
// So this drives the REAL ledger in the hostile world, where timers are data (a
// deadline that was never armed is a visible failure) and a socket can accept
// sends forever while answering nothing — the exact interleaving the incident
// took. What it pins is the discipline, not the wording:
//
//   * every act carries a receipt id, and is outstanding until receipted;
//   * a receipt for one act does not clear another;
//   * an unreceipted act UNDOES whatever the UI claimed;
//   * nothing is ever re-sent;
//   * a dead socket reports ONCE, not once per stranded act.
//
// Run manually: node tests/js/test_choreo_ack_ledger.js
"use strict";

const { hostileWorld, checks } = require("./harness");

const { ok, report } = checks();

function ledgerWorld({ socket = "open" } = {}) {
  const toasts = [];
  const log = [];
  // Offline is a DEAD socket plus the mode flag — the mode is only how the app
  // knows to say "offline" rather than "not connected", never a second way down.
  const ws = { readyState: socket === "open" ? 1 : 3, sent: [] };
  const w = hostileWorld({
    visible: true,
    globals: {
      ws,
      offlineMode: socket === "offline",
      showToast: (text) => toasts.push(text),
      reconnect: () => log.push("reconnect"),
    },
  });
  w.load("// [ACK-LEDGER-START]", "// [ACK-LEDGER-END]");
  // The REAL send(), so "the socket took the bytes" means what it means here.
  w.run(`function send(message) {
    if (!ws || ws.readyState !== 1) {
      showToast(offlineMode ? "offline — you can read past chats, not send" : "not connected");
      return false;
    }
    ws.sent.push(JSON.parse(JSON.stringify(message)));
    return true;
  }`);
  w.toasts = toasts;
  w.log = log;
  w.ws = ws;
  w.last = () => toasts[toasts.length - 1] || "";
  // Let the sweep run to a chosen point without a clock: every armed sweep
  // fires, repeatedly, until none re-arms or the budget runs out.
  w.settle = (rounds = 12) => {
    for (let i = 0; i < rounds; i += 1) {
      if (!w.armed((t) => t.ms === w.sandbox.ACK_SWEEP_MS).length) return;
      w.fire((t) => t.ms === w.sandbox.ACK_SWEEP_MS);
    }
  };
  // Push the deadline into the past without touching Date.
  w.expireAll = () => { for (const item of w.sandbox.outstanding.values()) item.due = 0; };
  return w;
}

// ---- 1. An act is outstanding until it is receipted ----------------------
{
  const w = ledgerWorld();
  const sent = w.sandbox.act({ type: "stop" }, { label: "the stop" });
  ok("it went out", sent === true && w.ws.sent.length === 1);
  ok("…carrying a receipt id the server can echo", Boolean(w.ws.sent[0].rid));
  ok("…and it is held open", w.sandbox.outstanding.size === 1);
  ok("…with a sweep armed to notice silence",
    w.armed((t) => t.ms === w.sandbox.ACK_SWEEP_MS).length === 1);

  w.sandbox.onAck(w.ws.sent[0].rid);
  ok("the receipt closes it", w.sandbox.outstanding.size === 0);
  w.settle();
  ok("…and nothing is ever reported about an act that landed",
    !w.toasts.some((t) => /may not have/.test(t)));
}

// ---- 2. A receipt answers ITS act, not whichever is oldest ---------------
{
  const w = ledgerWorld();
  w.sandbox.act({ type: "stop" }, { label: "the stop" });
  w.sandbox.act({ type: "rate", turn: 3 }, { label: "that rating" });
  w.sandbox.onAck(w.ws.sent[1].rid);
  ok("only the acknowledged one is closed", w.sandbox.outstanding.size === 1);
  ok("…and it is the right one still open",
    [...w.sandbox.outstanding.values()][0].label === "the stop");
}

// ---- 3. The incident: OPEN, dead, and nothing comes back -----------------
{
  const w = ledgerWorld();
  let undone = 0;
  w.sandbox.act({ type: "delete_session", name: "a.jsonl" }, {
    label: "deleting that chat",
    lost: () => { undone += 1; },
  });
  ok("a zombie socket takes the request like any other", w.ws.sent.length === 1);
  ok("nothing is said while the answer could still be coming",
    !/may not have/.test(w.last()));

  w.expireAll();
  w.settle();
  ok("silence is reported", /may not have reached aish/.test(w.last()));
  ok("…naming the action, in the user's words", /deleting that chat/.test(w.last()));
  ok("…the UI's claim is withdrawn", undone === 1);
  ok("…the dead socket is rebuilt", w.log.includes("reconnect"));
  ok("…and the act is NEVER re-sent", w.ws.sent.length === 1);
  ok("…and it is not reported twice", w.sandbox.outstanding.size === 0);
}

// ---- 4. A dead socket strands everything — and says so ONCE --------------
{
  const w = ledgerWorld();
  const undone = [];
  for (const [type, label] of [["stop", "the stop"], ["dequeue", "cancelling that queued message"], ["rate", "that rating"]]) {
    w.sandbox.act({ type }, { label, lost: () => undone.push(label) });
  }
  w.expireAll();
  w.settle();
  const reports = w.toasts.filter((t) => /may not have/.test(t));
  ok("one sentence, not three stomping each other in one toast slot",
    reports.length === 1);
  ok("…and it counts them rather than naming only the last",
    /3 actions/.test(reports[0]));
  ok("every claim is withdrawn, not just the reported one", undone.length === 3);
  ok("…and the socket is rebuilt once", w.log.filter((c) => c === "reconnect").length === 1);
}

// ---- 5. Never handed over at all -----------------------------------------
{
  const w = ledgerWorld({ socket: "closed" });
  let undone = 0;
  const sent = w.sandbox.act({ type: "delete_session" }, {
    label: "deleting that chat",
    lost: () => { undone += 1; },
  });
  ok("it reports failure to the caller", sent === false);
  ok("nothing went out", w.ws.sent.length === 0);
  ok("the answer names the ACTION, not just the connection",
    /deleting that chat/.test(w.last()) && /didn't happen/.test(w.last()));
  ok("the claim is withdrawn immediately — there is nothing to wait for",
    undone === 1);
  ok("and nothing is left outstanding", w.sandbox.outstanding.size === 0);

  const off = ledgerWorld({ socket: "offline" });
  off.sandbox.act({ type: "stop" }, { label: "the stop" });
  ok("offline says which of the two it is", /offline/.test(off.last()));
}

// ---- 6. A slow act is not a lost one -------------------------------------
{
  const w = ledgerWorld();
  w.sandbox.act({ type: "stop" }, { label: "the stop" });
  w.settle(3); // sweeps run, but the deadline has not passed
  ok("a sweep before the deadline reports nothing",
    !w.toasts.some((t) => /may not have/.test(t)));
  ok("…and keeps watching", w.sandbox.outstanding.size === 1);
  ok("…with the sweep re-armed", w.armed((t) => t.ms === w.sandbox.ACK_SWEEP_MS).length === 1);
}

// ---- 7. THE MANIFEST: nothing state-changing may go out bare -------------
// The audit behind #210 found a dozen requests sent on faith and six that told
// the user they had worked. A discipline nothing enforces decays back to that
// one call site at a time, so the exceptions are named HERE, each with the
// reason it does not need a receipt. A new message type on bare `send()` fails
// this check — which is the point: it forces the question to be answered once,
// by the person adding it, instead of never.
{
  const fs = require("fs");
  const path = require("path");
  const src = fs.readFileSync(
    path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
  );

  const BARE_OK = {
    // READS. A lost query paints nothing and asking again is free — there is
    // no claim to be wrong about, and receipting them would buy toasts nobody
    // needs (`sessions` and `files` fire on every keystroke).
    sessions: "read", files: "read", models: "read", jobs: "read",
    peek: "read", client_debug: "read",
    // ALREADY GUARDED, more strictly than a receipt: the message is on screen
    // pending until the server's own version lands, and the text goes back to
    // the composer if it never does.
    task: "[PENDING-SEND]",
    // Same: the transcript shows a placeholder until the replay arrives, and
    // says so with a retry control when it does not.
    resume: "[PENDING-VIEW]",
    // THE CONSOLE speaks for itself. A keystroke that did not arrive does not
    // echo, a terminal that did not open shows no prompt — the PTY's own
    // output is a continuous receipt, and one per keystroke would be absurd.
    console_open: "self-evident", console_in: "self-evident",
    console_resize: "self-evident", console_close: "self-evident",
    // FAILS SAFE, and the safe direction is the visible one. This is
    // releaseSentShares ([SHARES]) telling the server that a shared item went
    // out with a message. If it is the request that goes missing, the item
    // stays in the inbox and is offered again — which is exactly what "we do
    // not know whether it was used" should look like. The opposite failure,
    // quietly spending a share that never went anywhere, is the one with
    // nothing on screen to notice. The other share_drop callers — dismissing a
    // chip, and claiming one by hand — DO go through act(), because those two
    // claim something on screen that has to be taken back.
    share_drop: "fails safe",
    // ANSWERS INTO AN EMPTY SHEET. /browser opens the workspace sheet first
    // and every outcome — the profile status, a forgotten host, the window
    // that opened on the Mac — arrives as text to fill it. A request that goes
    // missing therefore shows as a sheet that never fills, which is the
    // absence being visible rather than hidden. Nothing is claimed on screen
    // ahead of the reply, so there is nothing for a receipt to take back.
    browser: "fails safe",
    // THE FRAME LOOP speaks for itself, like the console. A lost interaction
    // simply fails to repaint, and the sheet says so on its own timer ("no
    // answer — tap again") rather than pretending it worked. Recording a
    // SIGN-IN is deliberately not here: it arms the approval gate, so it goes
    // through act() as `browser_login`.
    browser_view: "self-evident",
  };

  const bare = [...src.matchAll(/send\(\{ type: "([a-z_]+)"/g)].map((m) => m[1]);
  const unlisted = [...new Set(bare)].filter((type) => !BARE_OK[type]);
  ok(`every bare send() is a declared exception (found: ${unlisted.join(", ") || "none"})`,
    unlisted.length === 0);

  // And the other direction: a listed exception that no longer exists is a
  // stale licence for the next person to copy.
  const stale = Object.keys(BARE_OK).filter((type) => !bare.includes(type));
  ok(`no stale exceptions in the manifest (found: ${stale.join(", ") || "none"})`,
    stale.length === 0);

  // The ledger is the only thing that may stamp a receipt id, or "outstanding"
  // stops meaning what it says.
  const stampers = (src.match(/\brid\s*[:=]/g) || []).length;
  ok("only the ledger mints receipt ids", stampers === 1);
}

report("test_choreo_ack_ledger.js");
