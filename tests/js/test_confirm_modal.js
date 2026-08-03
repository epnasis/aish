// Node-only, dependency-free check: the ONE confirmation modal every
// irreversible action goes through (#202).
//
// It replaced a two-tap arm-then-confirm guard that was real but unreadable: an
// armed control communicates by changing ITSELF, which on a phone happens under
// the finger covering it — and for an icon-only control there was not even a
// label to rewrite. The confirming tap also landed on the same pixel as the
// arming one, so a double-tap habit committed the action.
//
// What this pins is therefore not "a dialog exists" but the properties that
// make it a real guard: nothing happens until Confirm, every dismissal is a NO,
// a dismissed dialog leaves nothing half-committed, and the question always
// carries the consequences rather than just a verb.
//
// Runs the REAL block from app.js (extracted by marker) against a fake DOM.
//
// Run manually: node tests/js/test_confirm_modal.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appJsPath = path.join(__dirname, "..", "..", "aish", "static", "app.js");
const src = fs.readFileSync(appJsPath, "utf8");
const htmlPath = path.join(__dirname, "..", "..", "aish", "static", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

let checks = 0;
function ok(label, cond) { assert(cond, label); checks += 1; }

function world() {
  const sent = [];
  const awaited = [];
  const nodes = {};
  const el = (id) => (nodes[id] = nodes[id] || {
    id, textContent: "", hidden: true, focused: false,
    onclick: null, focus() { this.focused = true; },
  });
  const sandbox = {
    $: el,
    currentSession: "session-20260731-115636-049572.jsonl",
    send: (m) => { sent.push(m); return true; },
    closeSheets() {},
    // The confirm action hands the delete to the ledger ([ACK-LEDGER]) —
    // whether it ARRIVES is that block's subject, not this one's.
    act: (message, opts) => { sent.push(message); awaited.push(opts && opts.label); return true; },
    showToast() {},
    offlineMode: false,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    extract("// [CONFIRM-START]", "// [CONFIRM-END]").replace(/\blet\b|\bconst\b/g, "var"),
    sandbox,
  );
  return { sandbox, sent, el, awaited };
}

// 1. THE REGRESSION SHAPE: asking must not do anything.
{
  const w = world();
  let ran = false;
  w.sandbox.askConfirm({ title: "T", body: "B", verb: "Delete", action: () => { ran = true; } });
  ok("asking runs nothing", !ran);
  ok("the modal is up", w.el("confirm-modal").hidden === false);
  ok("it states the question", w.el("confirm-title").textContent === "T");
  ok("…and the consequences", w.el("confirm-body").textContent === "B");
  ok("the destructive button is labelled by the caller", w.el("confirm-ok").textContent === "Delete");
  ok("the resting choice holds the keyboard, not the destructive one",
    w.el("confirm-cancel").focused && !w.el("confirm-ok").focused);
}

// 2. Confirm runs it, exactly once, and closes.
{
  const w = world();
  let runs = 0;
  w.sandbox.askConfirm({ title: "T", body: "B", action: () => { runs += 1; } });
  w.el("confirm-ok").onclick();
  ok("confirming runs the action", runs === 1);
  ok("the modal closes", w.el("confirm-modal").hidden === true);
  // The pending action must be DROPPED, or a second confirm re-fires an
  // irreversible thing that already happened.
  w.el("confirm-ok").onclick();
  ok("a stale confirm cannot re-fire it", runs === 1);
}

// 3. Every dismissal is a NO, and leaves nothing half-committed.
for (const [name, dismiss] of [
  ["cancel", (w) => w.el("confirm-cancel").onclick()],
  ["tapping outside the card", (w) => w.el("confirm-modal").onclick({ target: w.el("confirm-modal") })],
]) {
  const w = world();
  let ran = false;
  w.sandbox.askConfirm({ title: "T", body: "B", action: () => { ran = true; } });
  dismiss(w);
  ok(`${name} does not run the action`, !ran);
  ok(`${name} closes the modal`, w.el("confirm-modal").hidden === true);
  w.el("confirm-ok").onclick();
  ok(`${name} leaves nothing pending`, !ran);
}

// 4. A tap INSIDE the card is not a dismissal (it would eat the buttons).
{
  const w = world();
  let ran = false;
  w.sandbox.askConfirm({ title: "T", body: "B", action: () => { ran = true; } });
  w.el("confirm-modal").onclick({ target: { id: "modal-card" } });
  ok("a tap on the card itself leaves the question open", w.el("confirm-modal").hidden === false);
  w.el("confirm-ok").onclick();
  ok("…and Confirm still works after it", ran);
}

// 5. Deleting a chat goes through it, and names the chat asked about — not
//    whatever the view moved to before the answer came.
{
  const w = world();
  w.sandbox.askDeleteChat();
  ok("asking sends nothing", w.sent.length === 0);
  ok("the question says what is lost",
    /log file/.test(w.el("confirm-body").textContent)
    && /cannot be undone/.test(w.el("confirm-body").textContent));
  w.sandbox.currentSession = "session-somewhere-else.jsonl";
  w.el("confirm-ok").onclick();
  ok("confirming deletes", w.sent.length === 1 && w.sent[0].type === "delete_session");
  ok("…the chat the question was about",
    w.sent[0].name === "session-20260731-115636-049572.jsonl");
  // Sending is not deleting: the request is held open until the server
  // receipts it ([ACK-LEDGER]), under a label the user would recognise.
  ok("…and it goes out as an ACT, held open until the server answers",
    w.awaited.length === 1 && /delet/i.test(w.awaited[0]));
}

// 6. Escape answers the question rather than dismissing whatever is behind it —
//    the modal can be raised over a sheet, a menu, or the console overlay.
{
  const handler = src.slice(src.indexOf('document.addEventListener("keydown"'));
  const confirmAt = handler.indexOf("confirmIsOpen()");
  const escapeExitAt = handler.indexOf("escapeExit()");
  ok("the modal owns Escape first", confirmAt !== -1 && confirmAt < escapeExitAt);
}

// 7. There is exactly ONE confirmation modal, and both destructive paths use
//    it. A second bespoke dialog is how two of them end up disagreeing about
//    how serious the same action is.
{
  ok("the markup exists once", (html.match(/id="confirm-modal"/g) || []).length === 1);
  const senders = src.match(/(?:send|act)\(\{ type: "delete_session"/g) || [];
  ok("only one code path deletes a chat", senders.length === 1);
  ok("…and it is inside the shared modal's block",
    extract("// [CONFIRM-START]", "// [CONFIRM-END]").includes('act({ type: "delete_session"'));
  // Deleting a chat must go through the ledger, never bare send(): a delete
  // handed to a dead socket is the whole of #210.
  ok("…and it is an ACT, not a bare send",
    !/send\(\{ type: "delete_session"/.test(src));
  ok("the old two-tap guard is gone", !/armDeleteChat|DELARM/.test(src));
}

console.log(`${checks} ok — all checks passed`);
