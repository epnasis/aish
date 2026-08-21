// Node-only, dependency-free checks for the two meanings of an `error` event
// ([ERROR-KIND] in app.js).
//
// The defect it exists to fix, reported from a phone: asking to delete a
// message while the chat was WORKING produced the correct refusal — and tore
// down the running turn's card with it. Stop went, Retry went, and a task was
// left running with nothing on screen to reach it. The refusal was right; the
// collateral was total.
//
// The cause is that one event type says two unrelated things. "Your TURN
// failed" ends the turn. "I will not do THAT" says nothing about the turn at
// all. The server knows which — a turn failure goes through the bridge, a
// refusal down the one socket that asked — and now stamps `code: "refused"`
// so the client stops having to assume the worse one.
//
// What is pinned here:
//   - a refusal touches NO turn state and is shown as a toast;
//   - an uncoded error still ends the turn, exactly as before (this is the
//     path #48 built, and the fix must not weaken it);
//   - a refusal never fires a push notification ("a chat title can't be
//     empty" is not something to buzz a phone for);
//   - `no_such_session` keeps its own handling, ahead of both.
//
// Run manually: node tests/js/test_error_kind.js
"use strict";

const assert = require("assert");
const { hostileWorld, checks } = require("./harness");

const { ok, report } = checks();

function errorWorld() {
  const log = [];
  const w = hostileWorld({
    globals: {
      // Turn state the failure path clears — a refusal must leave every one
      // of these exactly as it found it.
      turnStart: 12345,
      taskErrored: false,
      replaying: false,
      clientBusy: true,
      closeAnswer: () => log.push("closeAnswer"),
      finishTrace: () => log.push("finishTrace"),
      addErrorMsg: (text) => log.push(`addErrorMsg:${text}`),
      setBusy: (busy) => { log.push(`setBusy:${busy}`); w.sandbox.clientBusy = busy; },
      setStatus: () => log.push("setStatus"),
      notify: (title) => log.push(`notify:${title}`),
      showToast: (text) => log.push(`toast:${text}`),
      onSessionGone: (name) => log.push(`gone:${name}`),
      resolveDelete: (name) => log.push(`resolveDelete:${name}`),
      // Everything else the dispatcher can reach but this test never triggers.
      onHello() {}, onReplay() {}, onToken() {}, onDone() {}, onHistory() {},
      onStopped() {}, onStatus() {}, onApprovalRequest() {}, onApprovalResolved() {},
      renderSessions() {}, renderModels() {}, onModelChanged() {}, renderWorkspace() {},
      onFileList() {}, onSessionState() {}, onSessionDeleted() {}, onPeek() {},
      onSessionRenamed() {}, onRole() {}, onConsoleStarted() {}, onConsoleOut() {},
      onConsoleExit() {}, onCommandStart() {}, onCommandEnd() {}, traceStep() {},
      traceStream() {}, addWorkspaceNote() {}, addRedactedMsg() {}, addQueueChip() {},
      removeQueueChip() {}, addCwdChip() {}, removeCwdChip() {}, addAnsiMsg() {},
      resolvePendingSend() {}, retireQuickReplies() {}, rememberPrompt() {},
      addUserMsg() {}, addSystemMsg() {}, scrollToEnd() {}, markSeen() {},
      offlineSyncSoon() {}, currentTrace: null, serverPainted: false,
    },
  });
  w.load("function handle(event) {", "function onSessionRenamed(event) {");
  w.log = log;
  w.did = (what) => log.some((entry) => entry.startsWith(what));
  return w;
}

// ---- 1. A refusal is inert --------------------------------------------------
{
  const w = errorWorld();
  w.sandbox.handle({
    type: "error",
    code: "refused",
    text: "can't delete a message while this chat is working — stop the task "
      + "(or let it finish) and try again",
  });
  ok("a refusal closes no answer", !w.did("closeAnswer"));
  ok("a refusal does NOT close the live trace — this is the reported bug",
    !w.did("finishTrace"));
  ok("a refusal leaves the turn running", w.sandbox.clientBusy === true && !w.did("setBusy"));
  ok("a refusal keeps the turn's clock", w.sandbox.turnStart === 12345);
  ok("a refusal raises no red dot / Retry", w.sandbox.taskErrored === false);
  ok("a refusal writes nothing into the transcript", !w.did("addErrorMsg"));
  ok("a refusal does not buzz the phone", !w.did("notify"));
  ok("a refusal IS shown — as a toast",
    w.log.some((entry) => entry.startsWith("toast:") && /delete a message/.test(entry)));
}

// ---- 2. A real turn failure still ends the turn -----------------------------
{
  const w = errorWorld();
  w.sandbox.handle({ type: "error", text: "task failed: RuntimeError()" });
  ok("an uncoded error closes the live trace (#48)", w.did("finishTrace"));
  ok("an uncoded error clears busy", w.did("setBusy:false"));
  ok("an uncoded error zeroes the turn clock", w.sandbox.turnStart === 0);
  ok("an uncoded error raises the red dot", w.sandbox.taskErrored === true);
  ok("an uncoded error is written into the transcript", w.did("addErrorMsg"));
  ok("an uncoded error notifies", w.did("notify:aish — task failed"));
}

// ---- 3. A replayed failure keeps the connection dot green -------------------
{
  const w = errorWorld();
  w.sandbox.replaying = true;
  w.sandbox.handle({ type: "error", text: "task failed: RuntimeError()" });
  ok("a REPLAYED failure still shows, without claiming the connection broke",
    w.did("addErrorMsg") && w.sandbox.taskErrored === false);
}

// ---- 4. The by-name navigation failure keeps its own handling ---------------
{
  const w = errorWorld();
  w.sandbox.handle({ type: "error", code: "no_such_session", name: "gone.jsonl", text: "no such chat" });
  ok("a missing session prunes the phantom rather than toasting or failing",
    w.did("gone:gone.jsonl") && !w.did("toast:") && !w.did("finishTrace"));
}

// ---- 5. The server really does stamp its refusals ---------------------------
// The client's half is worthless if the server sends the code from only some of
// its refusal sites, and that is a Python-side fact — so it is asserted there
// (TestRefusalIsNotAFailure in tests/test_server.py, which walks every emit
// site). Here we only pin that the client reads `code` and not the prose.
{
  const w = errorWorld();
  w.sandbox.handle({ type: "error", text: "this session is busy — wait" });
  ok("prose is never the discriminator — an uncoded 'busy' still ends the turn",
    w.did("finishTrace"));
}

report("test_error_kind.js");
