// Node-only, dependency-free check: reuse restores the MESSAGE, not just its
// words ([REUSE-PROMPT] in app.js).
//
// The defect: tapping reuse on a prompt that had been sent with a photo put the
// text back in the composer with no attachment. Pressing send then asked the
// model to look at something that was not there — and the composer looked
// exactly like a correct one, so nothing said otherwise.
//
// The cause is that copy and reuse shared a getter. Reading the body of the
// split ([ATTACHMENT-NOTES]) is right for copy — a clipboard holds text — and
// silently lossy for reuse, which fills a surface that HAS an attachment zone.
//
// What is pinned here:
//   - reuse re-attaches the files the message carried;
//   - it does not duplicate one already in the composer;
//   - a message with attachments and no words still reuses (the photo IS the
//     message — refusing on empty text would drop it entirely);
//   - a composer holding different text is still refused, attachments and all —
//     reuse must never half-apply over something being written;
//   - copy is unchanged: text only.
//
// Runs the REAL block from app.js (extracted by marker) against a minimal fake
// DOM, so it tests the shipped code rather than a copy.
//
// Run manually: node tests/js/test_reuse_prompt.js
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

let checks = 0;
function ok(label, cond) { assert(cond, label); checks += 1; }

function fakeElement(tag) {
  const el = {
    tagName: tag,
    className: "",
    title: "",
    type: "",
    textContent: "",
    attrs: {},
    children: [],
    setAttribute(k, v) { el.attrs[k] = v; },
    append(...kids) { el.children.push(...kids); },
    appendChild(kid) { el.children.push(kid); return kid; },
  };
  return el;
}

const PHOTO =
  "[image attached: IMG_1326.jpeg — you can see it; file at /st/uploads/IMG_1326.jpeg]";
const PDF =
  "[document attached: rules.pdf — you can read it; file at /st/uploads/rules.pdf]";

function world({ draft = "", attached = [] } = {}) {
  const toasts = [];
  const rendered = [];
  const input = {
    value: draft,
    setSelectionRange() {},
    focus() {},
  };
  const sandbox = {
    document: { createElement: fakeElement },
    input,
    attachments: attached.slice(),
    renderAttachments: () => rendered.push(sandbox.attachments.map((a) => a.path)),
    resizeInput() {},
    showToast: (text) => toasts.push(text),
    pencilIcon: () => fakeElement("svg"),
    // The real splitter — the ONE definition of the note format. Reuse must
    // read the same parse the bubble renders from, not a second one.
    splitAttachmentNotes: null,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    extract("// [ATTACHMENT-NOTES-START]", "// [ATTACHMENT-NOTES-END]")
      .replace(/\bconst\b/g, "var"),
    sandbox,
  );
  vm.runInContext(
    extract("// [REUSE-PROMPT-START]", "// [REUSE-PROMPT-END]").replace(/\bconst\b/g, "var"),
    sandbox,
  );
  return { sandbox, input, toasts, rendered };
}

// The message as the transcript holds it: what was typed, plus the server's
// note lines. `addUserMsg` hands reuse the split of exactly this.
function reuse(w, text) {
  const { body, attachments } = w.sandbox.splitAttachmentNotes(text);
  w.sandbox.reuseChip(() => body, () => attachments).onclick();
}

// 1. THE REGRESSION: the photo comes back with the words.
{
  const w = world();
  reuse(w, `zmierz się pod szklanym dachem\n\n${PHOTO}`);
  ok("the words are back", w.input.value === "zmierz się pod szklanym dachem");
  ok("…and so is the photo", w.sandbox.attachments.length === 1);
  ok("named by its path, which is what the send carries",
    w.sandbox.attachments[0].path === "/st/uploads/IMG_1326.jpeg");
  ok("the strip was repainted", w.rendered.length === 1);
  ok("no note line leaked into the composer", !/image attached/.test(w.input.value));
}

// 2. Every attachment, not just the first — a turn can carry several kinds.
{
  const w = world();
  reuse(w, `look at both\n${PHOTO}\n${PDF}`);
  ok("both files came back", w.sandbox.attachments.length === 2);
  ok("the document too",
    w.sandbox.attachments.some((a) => a.path === "/st/uploads/rules.pdf"));
}

// 3. A file already staged is not staged twice — reuse twice, or reuse over a
//    composer already holding the same photo, must not send it doubled.
{
  const w = world({ attached: [{ name: "IMG_1326.jpeg", path: "/st/uploads/IMG_1326.jpeg" }] });
  reuse(w, `again\n${PHOTO}`);
  ok("the duplicate is skipped", w.sandbox.attachments.length === 1);
  ok("…and nothing was repainted for a no-op", w.rendered.length === 0);
}

// 4. A photo sent with no words IS a message. Refusing on empty text would drop
//    exactly the case the strip exists for.
{
  const w = world();
  reuse(w, PHOTO);
  ok("an attachment-only prompt still reuses", w.sandbox.attachments.length === 1);
  ok("with an empty composer", w.input.value === "");
}

// 5. A composer holding something else is still protected, and protected
//    WHOLLY: no text, no attachments, one toast.
{
  const w = world({ draft: "half a thought I was writing" });
  reuse(w, `zmierz się\n${PHOTO}`);
  ok("the draft survives", w.input.value === "half a thought I was writing");
  ok("nothing was attached behind it", w.sandbox.attachments.length === 0);
  ok("and the reader is told why", /clear the input first/.test(w.toasts[0] || ""));
}

console.log(`test_reuse_prompt.js: ${checks} checks passed`);
