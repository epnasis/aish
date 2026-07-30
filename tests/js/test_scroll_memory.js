// The reading position survives a rebuild — the reload after the phone has put
// the app away used to come back at the tail (or, once the answer became the
// scroll anchor, at the top of the last answer).
//
// Two mechanisms, both real code out of app.js:
//
//   [SCROLLPOS]  — remember/restore, keyed on `transcriptFp()`: a fingerprint of
//                  the transcript ON SCREEN, compared rendered-DOM to
//                  rendered-DOM. Deliberately NOT viewFp (the last replay's
//                  fingerprint), which a live turn leaves stale — that is
//                  precisely the flow this exists for, so keying on it would
//                  reject the restore in the only case that matters.
//
//   the replay guard in [ANSWER-OPEN] — a REPLAYED token must not schedule the
//                  forced anchorAnswer that brings a LIVE reply on screen, or a
//                  reload lands at the top of the last answer no matter where
//                  the reader was.
//
// Run manually: node tests/js/test_scroll_memory.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"), "utf8"
);

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  assert(a !== -1 && b !== -1, `markers not found: ${from} … ${to}`);
  return src.slice(a, b);
}

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}`);
    console.error(`       ${err.message}`);
  }
}

function fakeStorage(opts = {}) {
  const map = new Map();
  return {
    map,
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => {
      if (opts.readOnly) throw new Error("QuotaExceededError");
      map.set(k, String(v));
    },
    removeItem: (k) => map.delete(k),
  };
}

// A one-dimensional transcript: blocks stacked from the top of the scroller.
// An element's viewport top is (its content offset) - scrollTop, and the
// container's own top is 0 — the same model anchorAnswer is tested against.
function fakeTranscript(heights, classNames) {
  const kids = [];
  const el = {
    children: kids,
    scrollTop: 0,
    clientHeight: 600,
    get scrollHeight() { return heights.reduce((a, b) => a + b, 0); },
    get childElementCount() { return kids.length; },
    get lastElementChild() { return kids.length ? kids[kids.length - 1] : null; },
    getBoundingClientRect: () => ({ top: 0 }),
  };
  let offset = 0;
  heights.forEach((h, i) => {
    const top = offset;
    kids.push({
      className: (classNames && classNames[i]) || `msg block-${i}`,
      textContent: "x".repeat(10 + i),
      getBoundingClientRect: () => ({ top: top - el.scrollTop, bottom: top + h - el.scrollTop }),
    });
    offset += h;
  });
  return el;
}

function world(opts = {}) {
  const sandbox = {
    currentSession: opts.session === undefined ? "chat-a.jsonl" : opts.session,
    localStorage: opts.storage || fakeStorage(),
    messagesEl: opts.messagesEl || fakeTranscript([200, 400, 3000, 60]),
    updateScrollButton() {},
    Date: { now: () => opts.now || 1000 },
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("// [SCROLLPOS-START]", "// [SCROLLPOS-END]"), sandbox);
  vm.runInContext(slice("function topVisibleAnchor() {", "function toggleWrap()"), sandbox);
  assert(typeof sandbox.rememberScrollPos === "function", "rememberScrollPos not extracted");
  assert(typeof sandbox.restoreScrollPos === "function", "restoreScrollPos not extracted");
  assert(typeof sandbox.transcriptFp === "function", "transcriptFp not extracted");
  return sandbox;
}

// ---- the fingerprint ------------------------------------------------------

check("the fingerprint moves when the transcript does, and only then", () => {
  const s = world();
  const base = s.transcriptFp();

  s.messagesEl.scrollTop = 2000; // scrolling is not a change to the transcript
  assert.strictEqual(s.transcriptFp(), base, "scrolling must not move the fingerprint");

  // A turn arriving appends children — the "something happened while you were
  // away" signal that has to beat a remembered position.
  const grown = world({ messagesEl: fakeTranscript([200, 400, 3000, 60, 400, 60]) });
  assert.notStrictEqual(grown.transcriptFp(), base, "an appended turn must move it");

  // The last child changing in place (a live card finalizing into "Worked for
  // 8s") also has to register: same count, different tail.
  const relabelled = world({
    messagesEl: fakeTranscript([200, 400, 3000, 60], ["a", "b", "c", "trace live"]),
  });
  const settled = world({
    messagesEl: fakeTranscript([200, 400, 3000, 60], ["a", "b", "c", "trace"]),
  });
  assert.notStrictEqual(relabelled.transcriptFp(), settled.transcriptFp());
});

// ---- remember / restore --------------------------------------------------

check("a position saved and rebuilt identically comes back exactly", () => {
  const s = world();
  s.messagesEl.scrollTop = 1200;
  s.rememberScrollPos();

  // The rebuild: a fresh DOM with the same content, scrolled to the top (which
  // is where replaceChildren leaves it).
  const rebuilt = world({ storage: s.localStorage });
  assert.strictEqual(rebuilt.restoreScrollPos(), true, "the position should have applied");
  assert.strictEqual(rebuilt.messagesEl.scrollTop, 1200);
});

check("content that arrived while away wins over the saved position", () => {
  const s = world();
  s.messagesEl.scrollTop = 1200;
  s.rememberScrollPos();

  const grown = world({
    storage: s.localStorage,
    messagesEl: fakeTranscript([200, 400, 3000, 60, 400, 60]),
  });
  assert.strictEqual(grown.restoreScrollPos(), false, "must fall through to the tail");
  assert.strictEqual(grown.messagesEl.scrollTop, 0, "restore must not move it at all");
});

check("a session with nothing remembered falls through", () => {
  const s = world();
  assert.strictEqual(s.restoreScrollPos(), false);
});

check("the position is per session", () => {
  const a = world({ session: "chat-a.jsonl" });
  a.messagesEl.scrollTop = 1200;
  a.rememberScrollPos();
  const b = world({ session: "chat-b.jsonl", storage: a.localStorage });
  assert.strictEqual(b.restoreScrollPos(), false, "chat B must not inherit chat A's position");
});

check("no session (nothing open yet) saves nothing", () => {
  const s = world({ session: "" });
  s.messagesEl.scrollTop = 1200;
  s.rememberScrollPos();
  assert.strictEqual(s.localStorage.getItem("aish-scroll"), null);
});

check("a fingerprint match whose index no longer resolves falls through", () => {
  // Belt and braces: the fingerprint should make this unreachable, but a
  // restore must never throw on the way to painting a transcript.
  const s = world();
  s.messagesEl.scrollTop = 1200;
  s.rememberScrollPos();
  const mem = JSON.parse(s.localStorage.getItem("aish-scroll"));
  mem["chat-a.jsonl"].index = 99;
  s.localStorage.setItem("aish-scroll", JSON.stringify(mem));
  const rebuilt = world({ storage: s.localStorage });
  assert.strictEqual(rebuilt.restoreScrollPos(), false);
});

// ---- durability ----------------------------------------------------------

check("the memory is bounded, oldest-first", () => {
  const storage = fakeStorage();
  for (let i = 0; i < 30; i++) {
    const s = world({ session: `chat-${i}.jsonl`, storage, now: 1000 + i });
    s.messagesEl.scrollTop = 1200;
    s.rememberScrollPos();
  }
  const mem = JSON.parse(storage.getItem("aish-scroll"));
  const names = Object.keys(mem);
  assert.strictEqual(names.length, 24, `kept ${names.length}`);
  assert.ok(!names.includes("chat-0.jsonl"), "the oldest should have fallen off");
  assert.ok(names.includes("chat-29.jsonl"), "the newest must be kept");
});

check("storage that refuses writes is survivable", () => {
  // Private mode: the position is a nicety and must never break a paint.
  const s = world({ storage: fakeStorage({ readOnly: true }) });
  s.messagesEl.scrollTop = 1200;
  s.rememberScrollPos(); // must not throw
  assert.strictEqual(s.restoreScrollPos(), false);
});

check("garbage in storage is survivable", () => {
  const storage = fakeStorage();
  storage.setItem("aish-scroll", "{not json");
  const s = world({ storage });
  assert.strictEqual(s.restoreScrollPos(), false);
  s.messagesEl.scrollTop = 1200;
  s.rememberScrollPos(); // overwrites the garbage rather than throwing
  assert.ok(JSON.parse(storage.getItem("aish-scroll"))["chat-a.jsonl"]);
});

// ---- the replay guard ----------------------------------------------------

function tokenWorld(replaying) {
  const rafs = [];
  const anchorCalls = [];
  const sandbox = {
    answerEl: null,
    answerText: "",
    answerStableLen: 0,
    answerStableNodes: 0,
    answerRenderQueued: false,
    answerAbandoned: false,
    sawAnswer: false,
    replaying,
    turnAnchorEl: null,
    currentTrace: null,
    addMsg: (kind) => ({ className: kind }),
    collapseTimelineForAnswering() {},
    updateTraceHead() {},
    anchorAnswer: (force) => anchorCalls.push(Boolean(force)),
    renderAnswerFrame() {},
    requestAnimationFrame: (fn) => rafs.push(fn),
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("function onToken(text) {", "function renderAnswerFrame"), sandbox);
  return { sandbox, rafs, anchorCalls };
}

check("a live token forces the answer on screen", () => {
  const w = tokenWorld(false);
  w.sandbox.onToken("hello");
  w.rafs.forEach((fn) => fn()); // the hostile-harness habit: callbacks only run when fired
  assert.ok(w.anchorCalls.includes(true), "a live reply must be brought into view");
});

check("a replayed token never forces — onReplay owns the resting position", () => {
  const w = tokenWorld(true);
  w.sandbox.onToken("hello");
  w.rafs.forEach((fn) => fn());
  assert.ok(
    !w.anchorCalls.includes(true),
    "a forced anchor here overrides the reading position a reload just restored"
  );
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall scroll-memory checks passed");
