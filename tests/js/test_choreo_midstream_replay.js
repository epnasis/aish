// Choreography pin: a replay that lands MID-STREAM (issue #181 phase 3).
//
// A replay is the app's universal repair — rendering is a pure function of the
// event array — and it is reachable from three paths that never asked whether a
// turn was running: the socket's own `replay` (a reconnect, a resume), the
// offline mirror tap, and the speculative paint of a committed swipe. All three
// used to hand-roll the same zeroing of the answer accumulators, which left the
// half-streamed bubble behind while its TAIL kept arriving: the rest of the
// answer painted into a fresh bubble, and `done` — the only carrier of the
// complete text — then saw `sawAnswer` and rendered nothing. The turn ended up
// showing whatever happened to arrive after the replay.
//
// The count of bubbles was usually right; WHICH TEXT was in them was not. So the
// property pinned here is the strong one — after any interleaving, the turn owns
// exactly ONE answer bubble and it holds the COMPLETE answer — plus its
// counterweight: a "noop" landing (the reconnect re-replay on every phone
// unlock) must not disturb a stream at all.
//
// Everything under test is the real app.js code: onReplay, resetLiveTurn,
// onToken, closeAnswer, onDone and handle's turn boundary, driven over
// harness.js's hostile world. Only collaborators that need a real browser are
// stubbed — notably renderAnswerNow, the incremental markdown renderer (the
// streaming hot path is deliberately untouched by this phase); its stub writes
// the accumulated text into the bubble, which is what it does.
//
// Run manually: node tests/js/test_choreo_midstream_replay.js
"use strict";

const { sessionWorld, fakeElement, checks } = require("./harness");

const { ok, report } = checks();

const ANSWER = "Hello world, this is the whole answer.";
const CHUNKS = ["Hello ", "world, ", "this is ", "the whole ", "answer."];

// A world that can run one full turn: user → tokens → done, with a replay
// insertable at any point.
function turnWorld() {
  const w = sessionWorld({
    visible: true,
    globals: {
      FINE_POINTER: false,
      OFFLINE_SYNC_AFTER_DONE_MS: 2000,
      replaying: false,
      sessionTitled: false,
      turnStart: 0,
      answerTiming: 0,
      taskErrored: false,
      userCmdBlock: null,
      turnAnchorEl: null,
      lastUserPrompt: "",
      answerFilling: false,
      currentTrace: null,
      // collaborators that would need a real browser
      renderMarkdown: (text) => ({ md: text }),
      stripAttachmentNotes: (text) => text,
      highlightFences() {},
      attachAnswerTools() {},
      anchorAnswer() {},
      renderAnswerFrame() {},
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
      removeQueueChip() {},
      retireQuickReplies() {},
      rememberPrompt() {},
      addQueueChip() {},
      offlineSyncSoon() {},
      unparkTranscript() {},
      reconcilePager() {},
      traceSvg: () => "",
      updateTraceHead() {},
      refreshStatusline() {},
      finalizeAnswerRow() {},
    },
  });

  // The real code, in dependency order. Anything loaded here shadows the
  // recorder sessionWorld installed under the same name (onReplay especially).
  w.load("// [VIEWCACHE-START]", "// [VIEWCACHE-END]");
  w.load("let answerEl = null;", "function handle(event) {");
  w.load("function handle(event) {", "function onSessionRenamed(event) {");
  w.load("// [REPLAY-LANDING-START]", "// [REPLAY-LANDING-END]");
  w.load("function onToken(text) {", "function renderAnswerFrame");
  w.load("// [ANSWER-CLOSE-START]", "// [ANSWER-CLOSE-END]");
  w.load("function onDone(event) {", "// The server had nothing running");
  w.load("// [TRACE-CLOSE-START]", "// [TRACE-CLOSE-END]");
  w.load("function addMsg(kind, text) {", "// The prompt that started");

  const s = w.sandbox;
  s.addUserMsg = (text) => s.addMsg("user", text);
  s.addSystemMsg = (kind, text) => s.addMsg("system", text);
  // What renderAnswerNow does, minus the incremental machinery: the bubble on
  // screen shows the text accumulated so far.
  s.renderAnswerNow = () => {
    if (s.answerEl) s.answerEl.children = [{ md: s.answerText }];
  };
  s.setBusy = (busy) => { s.clientBusy = Boolean(busy); };
  s.currentSession = "a.jsonl";

  w.answers = () => w.el.children.filter((c) => (c.className || "").includes("answer"));
  w.answerTexts = () =>
    w.answers().map((el) => (el.children || []).map((c) => c.md || "").join(""));
  w.deliver = (event) => { s.handle(event); return w; };
  w.startTurn = () => w.deliver({ type: "user", text: "the question" });
  w.stream = (chunks) => { for (const c of chunks) w.deliver({ type: "token", text: c }); return w; };
  w.finish = () => w.deliver({ type: "done", result: ANSWER });
  // A replay of the transcript as the server has it: the question, nothing else
  // (the answer is still streaming, so it is not in the log yet).
  w.replayEvent = () => ({ type: "replay", events: [{ type: "user", text: "the question" }] });
  return w;
}

// ---- 1. A replay lands mid-stream, same session ---------------------------
// The reconnect/resume case: the socket re-sends the transcript while tokens are
// still arriving. The bubble those first tokens went into is destroyed with the
// old DOM, so the tail must NOT be painted on its own — `done` carries the whole
// answer and is the one that renders it.
{
  const w = turnWorld();
  w.startTurn();
  w.stream(CHUNKS.slice(0, 2));
  ok("a bubble is streaming before the replay", w.answers().length === 1);

  w.deliver(w.replayEvent());
  ok("the replay rebuilt the transcript (no answer bubble left)", w.answers().length === 0);

  w.stream(CHUNKS.slice(2)); // the tail of the same answer keeps arriving
  ok("the tail alone paints no half-written bubble", w.answers().length === 0);

  w.finish();
  ok("exactly one answer bubble for the turn", w.answers().length === 1);
  ok("…and it holds the COMPLETE answer, not the tail",
    w.answerTexts()[0] === ANSWER);
}

// ---- 2. …and at every position in the stream ------------------------------
// Where a reconnect falls among the tokens is the network's choice. The property
// must hold for all of them, including "after the last token, before done".
{
  for (let at = 0; at <= CHUNKS.length; at += 1) {
    const w = turnWorld();
    w.startTurn();
    w.stream(CHUNKS.slice(0, at));
    w.deliver(w.replayEvent());
    w.stream(CHUNKS.slice(at));
    w.finish();
    ok(`replay after ${at} token(s): one bubble, complete text`,
      w.answers().length === 1 && w.answerTexts()[0] === ANSWER);
  }
}

// ---- 3. A "noop" landing must not disturb a live stream -------------------
// The counterweight, and the reason resetLiveTurn takes the landing rather than
// firing on every replay: "noop" means the DOM already IS this transcript. That
// is the re-replay every phone unlock produces, and a turn may genuinely still
// be running under it. Tearing the answer down there would turn a fix into the
// same bug with a different trigger.
{
  const w = turnWorld();
  const s = w.sandbox;
  w.startTurn();
  w.stream(CHUNKS.slice(0, 2));
  const streaming = s.answerEl;

  // The reconnect re-replay: same fingerprint as the paint on screen, nothing
  // arrived since (the socket dispatch, not handle(), is what marks dirty).
  const event = w.replayEvent();
  s.viewFp = s.replayFp(event);
  s.viewDirty = false;
  w.deliver(event);

  ok("the live bubble survives a noop landing", s.answerEl === streaming);
  ok("…with its text intact", s.answerText === CHUNKS.slice(0, 2).join(""));
  w.stream(CHUNKS.slice(2));
  ok("…and the rest of the stream keeps filling the SAME bubble",
    s.answerEl === streaming && s.answerText === ANSWER);
  w.finish();
  ok("one bubble, complete text, nothing re-rendered",
    w.answers().length === 1 && w.answerTexts()[0] === ANSWER);
}

// ---- 4. A session switch mid-stream ---------------------------------------
// enterSession moves identity (and resets the fingerprint), then the landing
// replay repaints. The outgoing turn's leftovers must not paint into the chat
// you just switched to — neither its tail nor a second copy of its answer.
{
  const w = turnWorld();
  const s = w.sandbox;
  w.startTurn();
  w.stream(CHUNKS.slice(0, 2));

  s.enterSession("b.jsonl", { source: "hello", title: "the other chat", stash: true });
  ok("the switch reset the fingerprint", s.viewFp === "");
  w.deliver({ type: "replay", events: [{ type: "user", text: "b's question" }] });

  ok("chat B paints without A's answer", w.answers().length === 0);
  w.stream(CHUNKS.slice(2)); // A's stream has not caught up with the switch yet
  ok("A's tail paints nothing into B", w.answers().length === 0);
}

// ---- 5. …including onto a REUSED (stashed) view ---------------------------
// The view-cache landing skips the rebuild loop entirely, so it is the landing
// most easily forgotten. A stashed transcript is swapped back in whole; the
// abandoned turn must not append to it.
{
  const w = turnWorld();
  const s = w.sandbox;
  w.startTurn();
  w.stream(CHUNKS.slice(0, 2));

  const event = { type: "replay", events: [{ type: "user", text: "b's question" }] };
  const stashed = fakeElement("div");
  stashed.className = "msg user";
  s.enterSession("b.jsonl", { source: "hello", stash: true });
  s.viewCache.set("b.jsonl", { nodes: [stashed], fp: s.replayFp(event), renderedAnswers: 0 });

  w.deliver(event);
  ok("the stashed nodes came back", w.el.children.length === 1 && w.el.children[0] === stashed);
  w.stream(CHUNKS.slice(2));
  ok("and A's tail added nothing to them", w.el.children.length === 1);
}

report("test_choreo_midstream_replay.js");
