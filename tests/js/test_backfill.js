// Node-only, dependency-free check: the way back into a long chat ([BACKFILL]
// in app.js).
//
// The defect: a 1314-event chat opened at its 815th event. Above the transcript
// sat "… earlier events trimmed …" — a line that reads as damage, when in fact
// the log was whole and only the frame was small — and there was nothing to tap.
// Two thirds of the conversation, six of its answers and three of its photos,
// could not be reached from the app at all.
//
// What is pinned here:
//   - the row SAYS how much is missing and asks for more when tapped;
//   - each tap asks for a strictly wider window, so paging converges on the
//     start instead of re-fetching the same slice;
//   - the reader's place is held by distance from the BOTTOM, never by a child
//     index — a thousand events inserted above shift every index;
//   - the position is consumed ONCE, so an ordinary replay afterwards falls
//     through to the remembered reading position;
//   - a request that is never answered puts the control back (the ledger's
//     `lost`), because a claim about the server that cannot be undone must not
//     be made;
//   - a backfill in flight belongs to the chat being LEFT.
//
// Runs the REAL block from app.js (extracted by marker), so it tests the
// shipped code rather than a copy.
//
// Run manually: node tests/js/test_backfill.js
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
    type: "",
    textContent: "",
    disabled: false,
    children: [],
    onclick: null,
    appendChild(kid) { el.children.push(kid); return kid; },
  };
  return el;
}

function world({ offline = false, sendOk = true } = {}) {
  const sent = [];
  const toasts = [];
  const lost = [];
  // A scroller with real numbers: 4000px of content, parked 1200px from the
  // bottom. A backfill grows the content upward; the distance from the bottom
  // is what must survive.
  const messagesEl = fakeElement("div");
  messagesEl.scrollHeight = 4000;
  messagesEl.scrollTop = 2800;
  const sandbox = {
    document: { createElement: fakeElement },
    messagesEl,
    offlineViewing: offline,
    showToast: (text) => toasts.push(text),
    updateScrollButton() {},
    act: (m, opts) => {
      sent.push(m);
      if (!sendOk) { lost.push(opts); return false; }
      lost.push(opts); // kept so a test can fire `lost` by hand
      return true;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    extract("// [BACKFILL-START]", "// [BACKFILL-END]").replace(/\bconst\b/g, "var"),
    sandbox,
  );
  return { sandbox, sent, toasts, lost, messagesEl };
}

// A replay of `shown` events out of `total` — what the server hands over.
// `more` is its answer to "would asking again get you any further".
function painted(w, shown, total, more) {
  w.sandbox.noteWindow({
    events: new Array(shown).fill({ type: "user" }), total, more,
  });
}

// 1. The row says what is MISSING, not that something is wrong.
{
  const w = world();
  painted(w, 500, 1314);
  const row = w.sandbox.earlierRow();
  ok("it names the shortfall", /814 more/.test(row.textContent));
  ok("it invites a tap", /load earlier/i.test(row.textContent));
  ok("it is a control, not prose", row.tagName === "button" && typeof row.onclick === "function");
}

// 2. THE REGRESSION: tapping asks for a WIDER window of the same chat.
{
  const w = world();
  painted(w, 500, 1314);
  const row = w.sandbox.earlierRow();
  row.onclick();
  ok("one request went out", w.sent.length === 1);
  ok("it asks for history", w.sent[0].type === "history_more");
  ok("…wider than what is on screen", w.sent[0].window === 1500);
  ok("the control says it is working", /loading/i.test(row.textContent) && row.disabled);
}

// 3. Paging converges: the second tap asks for more than the first, because the
//    window is measured against what the LAST replay actually painted.
{
  const w = world();
  painted(w, 500, 4000);
  w.sandbox.earlierRow().onclick();
  painted(w, 1500, 4000);      // the server answered; the view is wider now
  w.sandbox.endBackfill();     // …which every replay reports
  w.sandbox.earlierRow().onclick();
  ok("each tap reaches further back",
    w.sent[0].window === 1500 && w.sent[1].window === 2500);
}

// 4. The reader's place is DISTANCE FROM THE BOTTOM. A child index would be
//    wrong the moment events are inserted above it, which is the whole event.
{
  const w = world();
  painted(w, 500, 1314);
  w.sandbox.earlierRow().onclick();     // 4000 - 2800 = 1200 from the bottom
  w.messagesEl.scrollHeight = 11000;    // the repaint added 7000px above
  ok("the position is applied", w.sandbox.restoreBackfillPos() === true);
  ok("…and it is the same distance from the bottom",
    w.messagesEl.scrollHeight - w.messagesEl.scrollTop === 1200);
}

// 5. Consumed once. A later ordinary replay must fall through to [SCROLLPOS],
//    or every repaint of this chat would drag the reader back to one spot.
{
  const w = world();
  painted(w, 500, 1314);
  w.sandbox.earlierRow().onclick();
  w.sandbox.restoreBackfillPos();
  ok("the next replay claims nothing", w.sandbox.restoreBackfillPos() === false);
}

// 6. Unanswered means UNDONE. The ledger's `lost` fires on a zombie socket, and
//    the control must not be left saying "loading" forever.
{
  const w = world();
  painted(w, 500, 1314);
  const row = w.sandbox.earlierRow();
  row.onclick();
  ok("a claim was made", row.disabled);
  w.lost[0].lost();
  ok("…and taken back", !row.disabled);
  ok("with the row readable again", /load earlier/i.test(row.textContent));
  ok("the ledger was given a name for it", /earlier messages/.test(w.lost[0].label));
}

// 7. A send the socket never took also puts the row back — the failure is the
//    same one, one step earlier.
{
  const w = world({ sendOk: false });
  painted(w, 500, 1314);
  const row = w.sandbox.earlierRow();
  row.onclick();
  ok("the control is usable again", !row.disabled);
  ok("…and no reading position is left armed", w.sandbox.restoreBackfillPos() === false);
}

// 8. Offline is read-only over a MIRROR copy; there is no wider window to ask
//    for, so say so rather than sending into a closed socket.
{
  const w = world({ offline: true });
  painted(w, 500, 1314);
  w.sandbox.earlierRow().onclick();
  ok("nothing is sent", w.sent.length === 0);
  ok("and the reader is told why", /connect/i.test(w.toasts[0] || ""));
}

// 9. One at a time: a second tap while a request is outstanding is ignored, so
//    an impatient double-tap cannot queue two repaints.
{
  const w = world();
  painted(w, 500, 1314);
  const row = w.sandbox.earlierRow();
  row.onclick();
  row.onclick();
  ok("only one request is in flight", w.sent.length === 1);
}

// 10. At the server's ceiling there is nothing more to fetch, and a control
//     that would do nothing is the same dead end in a friendlier font. It
//     becomes a line saying where the rest is.
{
  const w = world();
  painted(w, 20000, 31000, false);
  const row = w.sandbox.earlierRow();
  ok("no control is offered", typeof row.onclick !== "function");
  ok("…and the reader is told where the rest is", /log/.test(row.textContent));
}

// 11. A server that predates the field, and the offline mirror, say nothing
//     about `more` — which must read as "yes", the behaviour from before it
//     existed, never as a dead end.
{
  const w = world();
  painted(w, 500, 1314, undefined);
  ok("the control is still offered", typeof w.sandbox.earlierRow().onclick === "function");
}

console.log(`test_backfill.js: ${checks} checks passed`);
