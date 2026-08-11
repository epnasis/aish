// Node-only, dependency-free check for the YouTube card (#217). Pulls the REAL
// youtubeEmbed / embedForLink / imageLink / claimVideoCard out of app.js by
// marker and runs them against a minimal fake DOM with controllable timers.
//
// What it pins:
//   1. a poster the answer already carries becomes the card's still, instead of
//      re-fetching i.ytimg.com (which is in neither the SW cache nor the mirror)
//   2. the still SURVIVES activation — it is the only fallback left when the
//      player turns out to be a blank frame
//   3. a frame that never loads restores the still + "Open on YouTube", and the
//      card can be tapped again
//   4. a frame that DOES load cancels that watchdog
//   5. one card per video per answer: the second occurrence is a plain link
//   6. and the loser is a LINK, never a second copy of the picture — in either
//      order (the poster form losing must not paint the still again)
//   7. the streaming split hands the prefix the answer's set and the tail a COPY
//
// Run manually: node tests/js/test_video_card.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"),
  "utf8"
);

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

function fakeElement(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    // className and classList are ONE fact here (the real DOM's are): the card
    // sets its base classes with `className =` and its state with classList.add,
    // and a fake where those are two arrays reads the second and misses the first.
    get className() { return el._classes.join(" "); },
    set className(v) { el._classes = String(v).split(/\s+/).filter(Boolean); },
    innerHTML: "",
    textContent: "",
    children: [],
    attrs: {},
    listeners: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { c._parent = el; this.children.push(c); return c; },
    replaceChildren(...c) { this.children = c; },
    remove() {
      const parent = this._parent;
      if (parent) parent.children = parent.children.filter((c) => c !== this);
      this._parent = null;
    },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    fire(type, event) { (this.listeners[type] || []).forEach((fn) => fn(event)); },
    click() { this.fire("click", { stopPropagation() {} }); },
    classList: {
      add(...c) { for (const name of c) if (!el._classes.includes(name)) el._classes.push(name); },
      remove(c) { el._classes = el._classes.filter((x) => x !== c); },
      contains(c) { return el._classes.includes(c); },
    },
    _classes: [],
  };
  return el;
}

// Timers as data: the test fires the watchdog itself, so "the player never
// loaded" is an interleaving under test rather than an 8-second wait.
const timers = new Map();
let nextTimer = 1;

const box = {
  document: { createElement: fakeElement, createTextNode: (t) => ({ text: t }) },
  URLSearchParams,
  encodeURIComponent,
  token: "tok",
  replaying: false,
  offlineViewing: false,
  noteRenderError() {},
  // The connectivity facts the card reads: `offlineMode` is the socket's verdict
  // (declared far above the extracted block), navigator.onLine the accelerator.
  offlineMode: false,
  navigator: { onLine: true },
  setTimeout(fn, ms) { const id = nextTimer++; timers.set(id, { fn, ms }); return id; },
  clearTimeout(id) { timers.delete(id); },
};
vm.createContext(box);
vm.runInContext(
  [
    extract("function externalAnchor", "// [INLINEIMG-START]"),
    extract("const IMG_FETCH_HOSTS", "// Images (#9)"),
    extract("// The src a markdown image target", "// Quick replies (#17)"),
    extract("// Rich embeds (#50)", "function inlineMd"),
    // `let cardScope` must become a var: a top-level `let` in a vm script is
    // lexical, NOT a property of the context, so the test could neither set the
    // per-answer scope nor read the claims made in it.
  ].join("\n").replace(/\b(?:const|let)\b/g, "var"),
  box
);

const WATCH = "https://www.youtube.com/watch?v=lqltp2QaT30";
const OTHER = "https://youtu.be/dQw4w9WgXcQ";
const STILL = "/Users/x/.local/state/aish/media/abc-ukraine-thumb.jpg";

let failures = 0;
function check(name, fn) {
  timers.clear();
  box.cardScope = null;
  box.offlineMode = false;
  box.navigator.onLine = true;
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (err) {
    console.log(`FAIL ${name}\n     ${err.message}`);
    failures++;
  }
}

const iframeOf = (el) => el.children.find((c) => c.tagName === "IFRAME");
const imgOf = (el) => el.children.find((c) => c.tagName === "IMG");
const stalledOf = (el) => el.children.find((c) => c.className === "embed-stalled");
const playOf = (el) => el.children.find((c) => c.className === "embed-play");
const fireWatchdog = () => {
  assert.strictEqual(timers.size, 1, `expected one pending watchdog, got ${timers.size}`);
  const [[id, timer]] = [...timers];
  assert.strictEqual(timer.ms, box.EMBED_FRAME_SLOW_MS);
  timers.delete(id);
  timer.fn();
};

check("the answer's own still becomes the poster, not YouTube's copy", () => {
  const card = box.imageLink("Ukraine hit Wildberries", STILL, WATCH);
  assert(card._classes.includes("embed-youtube"), "not a video card");
  const img = imgOf(card);
  assert(img, "no still");
  assert(img.src.startsWith("/file?"), `still not served locally: ${img.src}`);
  assert(img.src.includes("token=tok"), "still src is missing the access token");
  assert(!iframeOf(card), "the player loaded eagerly");
});

check("a bare video link still falls back to YouTube's thumbnail", () => {
  const card = box.embedForLink("A video", WATCH);
  assert.strictEqual(
    imgOf(card).src,
    "https://img.youtube.com/vi/lqltp2QaT30/hqdefault.jpg"
  );
});

check("the still survives activation — the frame lies over it", () => {
  const card = box.embedForLink("A video", WATCH, "/file?path=x");
  card.click();
  const frame = iframeOf(card);
  assert(frame, "no iframe after activation");
  assert(frame.src.includes("youtube-nocookie.com/embed/lqltp2QaT30"), `wrong src: ${frame.src}`);
  assert(
    frame.getAttribute("sandbox").startsWith("allow-scripts allow-same-origin"),
    "iframe is not sandboxed as before"
  );
  assert(imgOf(card), "the still was thrown away — nothing is left if the player is blank");
  assert(!playOf(card), "the play badge is still over a playing video");
  assert(card._classes.includes("embed-active"), "not marked active");
  assert(!card.getAttribute("role"), "still announced as a button while playing");
});

check("a second tap while playing does not restack the player", () => {
  const card = box.embedForLink("A video", WATCH);
  card.click();
  card.click();
  assert.strictEqual(card.children.filter((c) => c.tagName === "IFRAME").length, 1);
});

check("a player that loads cancels the watchdog", () => {
  const card = box.embedForLink("A video", WATCH);
  card.click();
  iframeOf(card).fire("load");
  assert.strictEqual(timers.size, 0, "the watchdog is still armed after a successful load");
});

check("a player that never loads gives the still back, with a way out", () => {
  const card = box.embedForLink("A video", WATCH, "/file?path=x");
  card.click();
  fireWatchdog();
  assert(!iframeOf(card), "the dead frame is still there");
  assert(imgOf(card), "no still to fall back to");
  assert(playOf(card), "the play badge did not come back");
  assert(!card._classes.includes("embed-active"), "still marked active after the stall");
  assert.strictEqual(card.getAttribute("role"), "button", "not tappable again");
  const bar = stalledOf(card);
  assert(bar, "nothing says it could not play");
  const out = bar.children.find((c) => c.tagName === "A");
  assert(out, "no way to reach the video at all");
  assert.strictEqual(out.href, WATCH);
  assert.strictEqual(out.target, "_blank");
});

check("the way-out link does not re-arm the player under the new tab", () => {
  const card = box.embedForLink("A video", WATCH);
  card.click();
  fireWatchdog();
  const out = stalledOf(card).children.find((c) => c.tagName === "A");
  let stopped = false;
  out.fire("click", { stopPropagation() { stopped = true; } });
  assert(stopped, "the tap bubbles to the card, which would start playing again");
});

check("retrying after a stall clears the message and plays", () => {
  const card = box.embedForLink("A video", WATCH);
  card.click();
  fireWatchdog();
  card.click();
  assert(iframeOf(card), "no player on the retry");
  assert(!stalledOf(card), "the failure message is still up over a playing video");
});

// MEASURED, not reasoned (tests/js does not run a browser, so this is the note
// that carries it): in Chrome an iframe pointed at a blocked or unreachable host
// fires `load` exactly as a working one does, having painted its own error page.
// So the watchdog above cannot see the black box, and the app's own offline
// verdict is what has to — checked BEFORE a frame is ever created.
check("offline, the player is not opened at all — the still and a way out", () => {
  box.offlineMode = true;
  const card = box.embedForLink("A video", WATCH, "/file?path=x");
  card.click();
  assert(!iframeOf(card), "opened a frame that cannot load — that is the black box");
  assert(imgOf(card), "lost the still, which is the only thing that CAN show offline");
  const bar = stalledOf(card);
  assert(bar, "nothing says why the video did not play");
  assert(/Offline/.test(bar.children.map((c) => c.textContent).join(" ")), "does not say offline");
  assert.strictEqual(timers.size, 0, "armed a watchdog for a frame that was never created");
});

check("navigator.onLine only accelerates the same conclusion", () => {
  box.navigator.onLine = false;
  const card = box.embedForLink("A video", WATCH);
  card.click();
  assert(!iframeOf(card), "opened a frame with no network at all");
  assert(stalledOf(card), "said nothing");
});

check("back online, the same card plays on the next tap", () => {
  box.offlineMode = true;
  const card = box.embedForLink("A video", WATCH);
  card.click();
  assert(stalledOf(card), "expected the offline state first");
  box.offlineMode = false;
  card.click();
  assert(iframeOf(card), "still refusing to play after the connection came back");
  assert(!stalledOf(card), "the offline message is still up over a playing video");
});

check("one card per video in an answer: the repeat is a plain link", () => {
  box.cardScope = new Set();
  const card = box.imageLink("The still", STILL, WATCH);
  assert(card._classes.includes("embed-youtube"), "the first occurrence is not a card");
  const repeat = box.embedForLink("Professor Gerdes' video", WATCH);
  assert.strictEqual(repeat, null, "the same video carded twice in one answer");
  const different = box.embedForLink("Another video", OTHER);
  assert(different, "a DIFFERENT video was refused a card");
});

check("the loser renders as a link, never as the picture again", () => {
  box.cardScope = new Set();
  // Reverse order: the prose link cards first, then the poster form arrives.
  assert(box.embedForLink("Professor Gerdes' video", WATCH), "the prose link got no card");
  const el = box.imageLink("The still", STILL, WATCH);
  assert.strictEqual(el.tagName, "A", "the poster form produced a second card");
  assert(!imgOf(el), "the still was painted a second time — the duplicate is back");
  assert.strictEqual(el.textContent, "The still");
  assert.strictEqual(el.href, WATCH);
});

check("an unscoped render (a tool result, a draft) claims freely", () => {
  box.cardScope = null;
  assert(box.embedForLink("A video", WATCH), "first card refused");
  assert(box.embedForLink("A video", WATCH), "an unscoped render deduped across itself");
});

// Source-level, because the bug it guards is in the STREAMING SPLIT rather than
// in any decision function: the frozen prefix must commit its claims into the
// answer's set and the re-rendered tail must get a throwaway copy. Handing the
// tail the real set makes a card demote itself to a link one token later.
check("renderAnswerNow commits the prefix and copies for the tail", () => {
  const body = extract("function renderAnswerNow", "\n}\n");
  const calls = body.match(/renderMarkdown\([^;]*\)/g) || [];
  assert.strictEqual(calls.length, 2, `expected two renders, got ${calls.length}`);
  assert(
    /answerStableLen, boundary\), answerCardIds\)/.test(calls[0]),
    `the stable prefix does not commit into answerCardIds: ${calls[0]}`
  );
  assert(
    /new Set\(answerCardIds\)/.test(calls[1]),
    `the live tail was handed the real set, not a copy: ${calls[1]}`
  );
});

process.exit(failures ? 1 : 0);
