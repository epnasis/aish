// Choreography pin: a share marked `chat=new` lands AFTER the chat it is
// landing on is known (issue #393).
//
// The share sheet's normal case is a share arriving while nothing is
// connected: the phone posts, the app is closed, the app is opened later. The
// server then sends `hello` (which carries the inbox) and, as a SEPARATE later
// message, the `replay` that paints the transcript (server.py `_show`). The
// `chat=new` decision used to be taken inside the hello, against a transcript
// that had not arrived yet: `transcriptIsEmpty()` answered about an empty DOM,
// the "nothing to leave behind" branch returned early, the old chat painted over
// the top — and because the item was already ledgered as honoured, nothing later
// recovered it. The intent was spent, not delayed.
//
// The unit pins in test_shares.js could not see this: they handed the code its
// answer (`transcriptIsEmpty: () => …`), so the moment at which the runtime asks
// the question was never in the test. This file drives the REAL entry point —
// `handle()` — with the real hello-then-replay ordering, the real onHello, the
// real [SHARES], the real [REPLAY-LANDING] and the real transcriptIsEmpty over a
// fake DOM, and asserts on what was SENT to the server.
//
// What is pinned:
//   1. the closed-app case: hello carrying a fresh share, then the replay of a
//      chat with content → exactly one `new` is sent, and only after the replay;
//   2. landing on an unused chat → nothing is sent, the share stays there;
//   3. the already-open case (a `shared` broadcast on a painted transcript)
//      opens a chat at once, and the new chat's own hello does not open another;
//   4. a broadcast arriving BETWEEN hello and replay waits for the replay too;
//   5. a mirror paint is not an answer: the socket's replay is;
//   6. a replay that never comes leaves the intent LATE, not lost — the next
//      hello and its replay honour it;
//   7. `freshHonoured` is written only when the decision has been acted on.
//
// Run manually: node tests/js/test_choreo_share_landing.js
"use strict";

const { sessionWorld, fakeElement, checks } = require("./harness");

const { ok, report } = checks();

// onHello, requestNewChat and transcriptIsEmpty are extracted by their own
// source markers: what runs here is the shipped code, not a paraphrase of it.
const ON_HELLO = ["function onHello(event) {", "\n// Multi-connection (#102)"];
const REQUEST_NEW = ["function requestNewChat() {", "\nlet appVersion = null;"];
const IS_EMPTY = ["function transcriptIsEmpty() {", "\n// Empty-state welcome hero"];

function shareWorld() {
  const w = sessionWorld({
    visible: true,
    globals: {
      FINE_POINTER: false,
      replaying: false,
      sessionTitled: false,
      turnStart: 0,
      answerTiming: 0,
      taskErrored: false,
      userCmdBlock: null,
      turnAnchorEl: null,
      lastUserPrompt: "",
      currentTrace: null,
      cmdMode: false,
      attachments: [],
      input: { value: "", dispatchEvent() {} },
      Event: class { constructor(type) { this.type = type; } },
      // A live socket, so requestNewChat takes the direct path and `act` goes
      // through the real `send` recorder.
      ws: { readyState: 1 },
      offlineMode: false,
      adjudicateHeldSends() {},
      renderAttachments() {},
      // collaborators that would need a real browser
      renderMarkdown: (text) => ({ md: text }),
      stripAttachmentNotes: (text) => text,
      highlightFences() {},
      attachAnswerTools() {},
      anchorAnswer() {},
      renderAnswerFrame() {},
      renderAnswerNow() {},
      collapseTimelineForAnswering() {},
      stopSpeaking() {},
      maybeSpeakReply() {},
      addSources() {},
      notify() {},
      scrollToEnd() {},
      snapViewportSoon() {},
      reportViewport() {},
      updateScrollButton() {},
      removeCwdChip() {},
      clearQueueChips() {},
      removeQueueChip() {},
      retireQuickReplies() {},
      rememberPrompt() {},
      addQueueChip() {},
      offlineSyncSoon() {},
      traceSvg: () => "",
      updateTraceHead() {},
      refreshStatusline() {},
      releasePinnedTrace() {},
      finalizeAnswerRow() {},
      reconnect() {},
      restoreScrollPos: () => false,
    },
  });

  // The real code, in dependency order. Anything loaded here shadows the
  // recorder sessionWorld installed under the same name.
  w.load("// [VIEWCACHE-START]", "// [VIEWCACHE-END]");
  w.load("// [ACK-LEDGER-START]", "// [ACK-LEDGER-END]");
  w.load(...REQUEST_NEW);
  w.load("let answerEl = null;", "function handle(event) {");
  w.load("function handle(event) {", "function onSessionRenamed(event) {");
  w.load("function replayedTurnStart(event) {", "function traceStep(step) {");
  w.load(...ON_HELLO);
  w.load("// [REPLAY-LANDING-START]", "// [REPLAY-LANDING-END]");
  w.load("function onToken(text) {", "function renderAnswerFrame");
  w.load("// [ANSWER-CLOSE-START]", "// [ANSWER-CLOSE-END]");
  w.load("function onDone(event) {", "// The server had nothing running");
  w.load("// [TRACE-CLOSE-START]", "// [TRACE-CLOSE-END]");
  w.load("function addMsg(kind, text) {", "// The prompt that started");
  w.load(...IS_EMPTY);
  w.load("// [SHARES-START]", "// [SHARES-END]");

  const s = w.sandbox;
  s.addUserMsg = (text) => s.addMsg("user", text);
  s.addSystemMsg = (kind, text) => s.addMsg("system", text);
  s.setBusy = (busy) => { s.clientBusy = Boolean(busy); };

  // Every message the client asked the server for, parsed.
  w.sentOf = (type) => w.calls
    .filter((c) => c.name === "send" && c.args[0] && c.args[0].type === type)
    .map((c) => c.args[0]);
  w.newChats = () => w.sentOf("new").length;
  w.deliver = (event) => { s.handle(event); return w; };
  w.hello = (session, shares = []) => w.deliver({
    type: "hello",
    session,
    title: session,
    model: "test-model",
    pager: [],
    cmd_history: [],
    busy: false,
    log_path: `/logs/${session}`,
    shares,
  });
  w.replay = (events) => w.deliver({ type: "replay", events, truncated: false });
  return w;
}

const CONTENT = [
  { type: "user", text: "the question" },
  { type: "done", result: "the answer" },
];
const fresh = (id = "s1") => ({
  id,
  name: "IMG_4021.jpg",
  path: `/u/uploads/${id}.jpg`,
  text: "",
  source: "iPhone",
  fresh: true,
});

// ---- 1. The closed-app case: hello carries the share, the replay follows --
// The report's own probe, in order: hello (inbox in hand, DOM empty), then the
// replay that paints eight events. The decision has to wait for the second.
{
  const w = shareWorld();
  w.hello("A", [fresh()]);
  ok("nothing is asked of the server while the landing chat is still unknown",
    w.newChats() === 0);
  ok("…and the intent is not yet spent", !w.sandbox.freshHonoured.has("s1"));
  ok("…while the file is already attached where you are",
    w.sandbox.attachments.length === 1 && w.sandbox.attachments[0].share === "s1");

  w.replay(CONTENT);
  ok("once the transcript has landed with content, a new chat is asked for",
    w.newChats() === 1);
  ok("…exactly once, through the ack ledger",
    w.sentOf("new").every((m) => typeof m.rid === "string"));
  ok("…and only now is the intent ledgered as honoured",
    w.sandbox.freshHonoured.has("s1"));
  ok("…with the attachment still riding along into the new chat",
    w.sandbox.attachments.length === 1);
}

// ---- 2. Landing on an unused chat: nothing to leave behind ---------------
{
  const w = shareWorld();
  w.hello("A", [fresh()]);
  w.replay([]);
  ok("an empty landing chat is the share's own chat — no spare row is opened",
    w.newChats() === 0);
  ok("…and that IS the intent acted on, so it is ledgered",
    w.sandbox.freshHonoured.has("s1"));
  w.replay([]); // the reconnect re-replay
  ok("…and a later replay does not revisit it", w.newChats() === 0);
}

// ---- 3. The already-open case: a broadcast onto a painted transcript ------
// The path test_shares.js pins, now driven end to end: the decision is
// immediate, and the new chat's own hello (which repeats the inbox) must not
// open a second one.
{
  const w = shareWorld();
  w.hello("A");
  w.replay(CONTENT);
  ok("no share, no new chat", w.newChats() === 0);

  w.deliver({ type: "shared", items: [fresh()] });
  ok("a fresh share arriving on a painted chat with content opens one at once",
    w.newChats() === 1);

  // The server answers with the new chat: a hello that carries the SAME inbox
  // (the file is not consumed until sent), then its empty replay.
  w.hello("B", [fresh()]);
  w.replay([]);
  ok("the new chat's own hello does not open another", w.newChats() === 1);
  ok("…nor does the reconnect re-replay", (w.replay([]), w.newChats() === 1));
}

// ---- 4. A broadcast between hello and replay waits for the replay too -----
// The same window as (1), entered through the other door.
{
  const w = shareWorld();
  w.hello("A");
  w.deliver({ type: "shared", items: [fresh()] });
  ok("a share broadcast before the landing chat is painted asks nothing yet",
    w.newChats() === 0 && !w.sandbox.freshHonoured.has("s1"));
  w.replay(CONTENT);
  ok("…and is honoured once the replay says there is a chat worth leaving",
    w.newChats() === 1 && w.sandbox.freshHonoured.has("s1"));
}

// ---- 4b. …and the inbox that arrives last is the one honoured ------------
// The server's list is the truth: an item claimed on another device while the
// replay was in flight must not open a chat for nobody.
{
  const w = shareWorld();
  w.hello("A", [fresh()]);
  w.deliver({ type: "shared", items: [] }); // claimed elsewhere
  w.replay(CONTENT);
  ok("a share the server no longer lists opens nothing", w.newChats() === 0);
}

// ---- 5. A mirror paint is not an answer ------------------------------------
// The boot paint puts the remembered chat on screen before the socket says a
// word ([SESSION-ENTER] source "boot"), and it claims no fingerprint (L4). A
// hello landing on it must still wait for the server's own transcript.
{
  const w = shareWorld();
  const s = w.sandbox;
  s.enterSession("A", { source: "boot", title: "A" });
  s.onReplay({ events: CONTENT, truncated: false });
  ok("the mirror painted something", w.el.children.length > 0);
  ok("…and claimed nothing", s.viewFp === "");

  w.hello("A", [fresh()]);
  ok("a hello over a mirror paint decides nothing yet", w.newChats() === 0);
  w.replay(CONTENT);
  ok("…the socket's replay does", w.newChats() === 1);
}

// ---- 6. Late, not lost -----------------------------------------------------
// The replay never arrives (the socket died between the two messages). The
// intent must survive to the next connection instead of being spent by the
// hello that could not act on it.
{
  const w = shareWorld();
  w.hello("A", [fresh()]);
  // …silence. The client reconnects; the server repeats hello and replay.
  w.hello("A", [fresh()]);
  ok("a second hello with no replay between still spends nothing",
    w.newChats() === 0 && !w.sandbox.freshHonoured.has("s1"));
  w.replay(CONTENT);
  ok("the first replay to land honours it", w.newChats() === 1);
}

// ---- 7. A batch is one chat, and a repeat is none ---------------------------
{
  const w = shareWorld();
  w.hello("A", [fresh("a"), fresh("b")]);
  w.replay(CONTENT);
  ok("three photos shared together open ONE chat", w.newChats() === 1);
  ok("…all of them ledgered", ["a", "b"].every((id) => w.sandbox.freshHonoured.has(id)));
  w.deliver({ type: "shared", items: [fresh("a"), fresh("b")] });
  ok("…and a repaint of the same items opens none", w.newChats() === 1);
}

// The fake DOM is what transcriptIsEmpty reads; make sure it is being read at
// all, or every "empty" verdict above was vacuous.
{
  const w = shareWorld();
  w.hello("A");
  w.replay(CONTENT);
  ok("the replayed content is in the transcript the decision reads",
    !w.sandbox.transcriptIsEmpty() && w.el.children.length === CONTENT.length);
  w.replay([]);
  ok("…and an empty replay empties it", w.sandbox.transcriptIsEmpty());
}

void fakeElement;
report("test_choreo_share_landing.js");
