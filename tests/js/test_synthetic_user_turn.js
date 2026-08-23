// Node-only, dependency-free regression check for issue #171: a message aish
// injected into the conversation itself — the [automatic resume] note, an
// automation's trigger prompt — rendered as a blue USER bubble, indistinguishable
// from something the human typed.
//
// The subtle half of the fix is what must NOT change: a synthetic turn is still
// a real turn (it starts a task), so every turn-management side effect in
// `handle()`'s `case "user"` has to fire exactly as before — only the rendering
// differs. Getting that wrong silently wedges the busy state and the timeline,
// which no rendering assertion would catch. So this drives the REAL `handle`
// extracted from app.js against a minimal fake DOM.
//
// Run manually: node tests/js/test_synthetic_user_turn.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

// A DOM just rich enough for the two message builders: elements remember their
// class, their text and their children, so the test can read back what was
// actually appended.
// `textContent` AGGREGATES, like the real thing. It used to be a plain string
// that ignored children, and that hid a live defect: a bubble that set its text
// and then appended the same words as a child read back as correct here while
// every ordinary message on screen showed itself twice (#235). A fake that
// lies in the direction of "everything is fine" is worse than no fake.
function fakeElement(tag) {
  let own = "";
  const el = {
    tagName: tag,
    className: "",
    innerHTML: "",
    children: [],
    get textContent() {
      return own + el.children.map((c) => c.textContent || "").join("");
    },
    set textContent(value) {
      own = value == null ? "" : String(value);
      el.children.length = 0;   // assigning text replaces the children, as in the DOM
    },
    append(...nodes) { this.children.push(...nodes); },
    appendChild(node) { this.children.push(node); return node; },
    remove() {},
    classList: { add() {}, remove() {}, contains() { return false; } },
  };
  return el;
}

function makeSandbox() {
  const calls = [];
  const messagesEl = fakeElement("div");
  const sandbox = {
    messagesEl,
    calls,
    // --- turn management: the side effects that must survive ---
    closeAnswer: () => calls.push("closeAnswer"),
    finishTrace: () => calls.push("finishTrace"),
    removeQueueChip: (t) => calls.push("removeQueueChip:" + t),
    retireQuickReplies: () => calls.push("retireQuickReplies"),
    setBusy: (v) => calls.push("setBusy:" + v),
    setTitle: (t) => calls.push("setTitle:" + t),
    rememberPrompt: (t) => calls.push("rememberPrompt:" + t),
    scrollToEnd: () => {},
    // Attachment rendering is [ATTACHMENT-NOTES]' business and has its own
    // file; here a turn is just its text.
    stripAttachmentNotes: (t) => t,
    messageBody: (t) => t,
    messageParts: (t) => (t ? [{ type: "text", text: t }] : []),
    // (a real message with no files is one text run — same as messageParts gives)
    messagePictures: () => [],
    attachmentNode: () => fakeElement("img"),
    recordSource: (t) => t,
    renderShares: () => {},
    // --- the genuine-bubble path, kept real down to addMsg ---
    reuseChip: () => fakeElement("button"),
    redactChip: () => fakeElement("button"),
    copyChip: () => fakeElement("button"),
    // --- icons the system row draws ---
    traceSvg: () => "<svg></svg>",
    RERUN_SVG: "<svg></svg>",
    // --- state `case "user"` writes ---
    sessionTitled: false,
    replaying: false,
    sawAnswer: true,
    userCmdBlock: {},
    taskErrored: true,
    turnStart: null,
    turnAnchorEl: null,
    lastUserPrompt: "",
    currentTrace: null,
    document: { createElement: fakeElement },
    // The turn's timestamp (#200) rides the prompt's tool row.
    dayStart: (ms) => ms,
    DAY_MS: 86400000,
    Date,
    Number,
    offlineSyncSoon: () => {},
  };
  vm.createContext(sandbox);
  // vm contexts don't expose top-level const/let as sandbox properties, so the
  // extracted declarations are switched to var — safe here, they are plain
  // declarations with no block-scoping dependency.
  const snippet =
    extract("// [SYNTHETIC-START]", "// [SYNTHETIC-END]").replace(/\bconst\b/g, "var") +
    "\n" +
    extract("// [MSG-STAMP-START]", "// [MSG-STAMP-END]").replace(/\bconst\b/g, "var") +
    "\n" +
    extract("function addUserMsg(text, at, turn, real) {", "function addAnsiMsg") +
    "\n" +
    extract("function addMsg(kind, text) {", "// The prompt that started") +
    "\n" +
    extract("function handle(event) {", "function onSessionRenamed") +
    "\n" +
    // The `user` case reads a replayed turn's origin off the event.
    extract("function replayedTurnStart(event) {", "function traceStep(step) {");
  vm.runInContext(snippet, sandbox);
  return sandbox;
}

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures += 1;
    console.error(`FAIL - ${name}\n    ${err.message}`);
  }
}

const RESUME_TEXT =
  "[automatic resume] aish restarted while this task was still running, so the " +
  "previous attempt was cut off part-way. Everything above is what had already " +
  "happened. Do NOT repeat steps that already completed.\n\nCut off mid-step. " +
  "These had STARTED and never reported a result:\n- read_url: https://x.test/a";

check("a message the user typed still renders as a blue user bubble", () => {
  const s = makeSandbox();
  s.handle({ type: "user", text: "fix the tests" });
  const bubble = s.messagesEl.children[0];
  assert.strictEqual(bubble.className, "msg user");
  assert.strictEqual(bubble.textContent, "fix the tests");
});

check("the [automatic resume] note renders as a system row, not a user bubble", () => {
  const s = makeSandbox();
  s.handle({ type: "user", text: RESUME_TEXT, synthetic: "resume" });
  const row = s.messagesEl.children[0];
  assert.strictEqual(row.className, "msg system-note");
  assert(
    !s.messagesEl.children.some((c) => c.className === "msg user"),
    "no user bubble may be produced for a synthetic turn"
  );
  const text = JSON.stringify(row.children.map((c) => c.children || []));
  assert(text.includes("Automatic resume"), "the row must say what it is");
  assert(!text.includes("[automatic resume]"), "the marker is the label, not body text");
  assert(text.includes("aish restarted"), "the row must say what happened");
});

check("the resume row says what happened, not what the model was told", () => {
  const s = makeSandbox();
  s.handle({ type: "user", text: RESUME_TEXT, synthetic: "resume" });
  const text = JSON.stringify(s.messagesEl.children[0].children.map((c) => c.children || []));
  assert(!text.includes("read_url"), "no tool name reaches the row");
  assert(!text.includes("Do NOT repeat"), "no model instructions reach the row");
  assert(!text.includes("https://x.test/a"), "no raw in-flight URL reaches the row");
  assert(text.includes("picked up where it left off"), "one line, in his language");
});

check("an automation's trigger prompt renders as a system row too", () => {
  const s = makeSandbox();
  s.handle({ type: "user", text: "new mail from the bank", synthetic: "trigger" });
  const row = s.messagesEl.children[0];
  assert.strictEqual(row.className, "msg system-note");
  const labels = row.children.flatMap((c) => (c.children || []).map((n) => n.textContent));
  assert(labels.includes("Triggered request"), `unexpected labels: ${labels}`);
});

check("a synthetic turn still runs every turn-management side effect", () => {
  // The load-bearing half: a synthetic turn IS a real turn. If any of this
  // stops firing, the busy state and the timeline break silently.
  const genuine = makeSandbox();
  genuine.handle({ type: "user", text: "hello" });
  const synthetic = makeSandbox();
  synthetic.handle({ type: "user", text: RESUME_TEXT, synthetic: "resume" });

  for (const s of [genuine, synthetic]) {
    for (const effect of ["closeAnswer", "finishTrace", "retireQuickReplies", "setBusy:true"]) {
      assert(s.calls.includes(effect), `${effect} must fire (calls: ${s.calls})`);
    }
    assert(s.calls.some((c) => c.startsWith("removeQueueChip:")), "queue chip must retire");
    assert.strictEqual(s.sawAnswer, false);
    assert.strictEqual(s.userCmdBlock, null);
    assert.strictEqual(s.taskErrored, false);
    assert(typeof s.turnStart === "number", "the turn clock must start");
  }
});

check("a synthetic turn seeds neither the chat title nor the prompt history", () => {
  // Both are records of what YOU asked; aish's own text belongs in neither.
  const s = makeSandbox();
  s.handle({ type: "user", text: RESUME_TEXT, synthetic: "resume" });
  assert(!s.calls.some((c) => c.startsWith("setTitle:")), `titled anyway: ${s.calls}`);
  assert(!s.calls.some((c) => c.startsWith("rememberPrompt:")), `remembered: ${s.calls}`);

  const typed = makeSandbox();
  typed.handle({ type: "user", text: "fix the tests" });
  assert(typed.calls.includes("setTitle:fix the tests"));
  assert(typed.calls.includes("rememberPrompt:fix the tests"));
});

check("a ! command still skips its bubble (#154 is not disturbed)", () => {
  const s = makeSandbox();
  s.handle({ type: "user", text: "!ls -la" });
  assert.strictEqual(s.messagesEl.children.length, 0, "no bubble for a ! command");
  assert.strictEqual(s.turnAnchorEl, null);
  assert(s.calls.includes("setBusy:true"), "…but it is still a turn");
});

process.exit(failures === 0 ? 0 : 1);
