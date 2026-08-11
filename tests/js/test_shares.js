// Node-only, dependency-free checks for [SHARES] — what the iPhone share sheet
// handed over, and how it reaches a message.
//
// This file has been rewritten twice by the same defect, reported from a phone
// both times: a shared item sat in a strip above the composer, looking attached,
// and pressing send left it behind. First for files ("adding a prompt and
// pressing send does not send the attachment, just the prompt"), then for links.
//
// The chip was the mistake, not the wiring. Every app that shares INTO a compose
// surface — Messages, Mail, WhatsApp, Slack — pre-fills the field; nobody taps a
// chip first. So there is no strip any more:
//
//   - a shared FILE is attached to the composer, and
//   - shared TEXT is appended to it, at the END, without stealing focus.
//
// The safety property was never the tap. It is that a share starts NOTHING —
// no session, no model call — which is enforced on the server.
//
// The rest of what is pinned here:
//   - the server owns the inbox; this reconciles against its list and keeps no
//     copy, because two devices claim from one inbox;
//   - a FILE is consumed on send and TEXT on arrival — one principle (consume it
//     once it is somewhere it cannot be lost), two answers, because the draft is
//     persisted and an attachment chip is not;
//   - ✕ on an attachment tells the server, or the next repaint puts it back;
//   - `chat=new` opens one chat for the batch, once, never from an empty one.
//
// Run manually: node tests/js/test_shares.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface, fakeElement, checks } = require("./harness");

const { ok, report } = checks();

function world({ emptyChat = false, draft = "", cmdMode = false } = {}) {
  const newChats = [];
  const empty = { value: emptyChat };
  const acts = [];
  const sent = [];
  const toasts = [];
  const sandbox = {
    $: () => fakeElement("div"),
    document: { createElement: (tag) => fakeElement(tag) },
    act: (message, opts) => { acts.push({ message, opts: opts || {} }); return true; },
    send: (message) => { sent.push(message); return true; },
    showToast: (text) => toasts.push(text),
    renderAttachments: () => {},
    requestNewChat: () => newChats.push(1),
    transcriptIsEmpty: () => empty.value,
    cmdMode,
    // The real composer is a <textarea>; the input event is what saves the
    // draft and re-measures it, so the code only needs value + dispatchEvent.
    input: { value: draft, dispatchEvent() {} },
    Event: class { constructor(type) { this.type = type; } },
    attachments: [],
    Set,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(appSource(), "// [SHARES-START]", "// [SHARES-END]")),
    sandbox,
  );
  return {
    s: sandbox,
    acts,
    newChats,
    toasts,
    composer: () => sandbox.input.value,
    sent: () => JSON.parse(JSON.stringify(sent)),
    attachments: () => JSON.parse(JSON.stringify(sandbox.attachments)),
  };
}

const fileShare = (over = {}) => ({
  id: "s1",
  name: "IMG_4021.jpg",
  path: "/u/uploads/IMG_4021.jpg",
  text: "",
  source: "iPhone",
  ...over,
});
const LINK = "https://example.com/x?a=1&b=2";
const textShare = (over = {}) =>
  fileShare({ id: "t1", name: LINK, path: "", text: LINK, source: "Safari", ...over });

// ---- files: attached, no tap ----------------------------------------------
{
  const w = world();
  w.s.renderShares([fileShare()]);
  assert.deepStrictEqual(w.attachments(), [
    { name: "IMG_4021.jpg", path: "/u/uploads/IMG_4021.jpg", share: "s1" },
  ]);
  ok("a waiting shared FILE is attached to the composer with no tap at all", true);
  ok("…and nothing is consumed yet: send has not happened", w.sent().length === 0);
}

// ---- text: appended, no tap ------------------------------------------------
{
  const w = world();
  w.s.renderShares([textShare()]);
  ok("a shared LINK is put into the composer with no tap either",
    w.composer() === LINK, w.composer());
  ok("…and is consumed immediately, because the draft is persisted",
    w.acts.some((a) => a.message.type === "share_drop" && a.message.id === "t1"));
}

{
  // The reason it appends rather than inserting at the cursor: it is an
  // ARRIVAL, not something the owner just asked for with a keystroke. And on
  // its own LINE, because it is somebody else's words arriving in the middle
  // of yours — a long prompt with a URL spliced in at the end of a sentence is
  // hard to read back.
  const w = world({ draft: "summarise this for me" });
  w.s.renderShares([textShare()]);
  ok("a live arrival never destroys what was already typed",
    w.composer() === `summarise this for me\n${LINK}`, JSON.stringify(w.composer()));
}

{
  const w = world({ draft: "look at " });
  w.s.renderShares([textShare()]);
  ok("…and the trailing space goes with it, leaving no invisible tail",
    w.composer() === `look at\n${LINK}`, JSON.stringify(w.composer()));
}

{
  const w = world({ draft: "notes:\n" });
  w.s.renderShares([textShare()]);
  ok("…and a line break already there is not doubled",
    w.composer() === `notes:\n${LINK}`, JSON.stringify(w.composer()));
}

{
  const w = world();
  w.s.renderShares([textShare()]);
  ok("an empty composer gets the link with no leading blank line",
    w.composer() === LINK, JSON.stringify(w.composer()));
}

{
  // hello repeats the inbox on every connect, and a lost share_drop leaves the
  // item exactly where it was. Without the ledger the link appends twice.
  const w = world();
  w.s.renderShares([textShare()]);
  w.s.renderShares([textShare()]);
  w.s.renderShares([textShare()]);
  ok("three repaints of the same link append it once", w.composer() === LINK, w.composer());
}

{
  // Terminal mode's composer is a shell command line.
  const w = world({ cmdMode: true });
  w.s.renderShares([textShare()]);
  ok("a link is not appended to a shell command line", w.composer() === "");
  ok("…and is left in the inbox rather than swallowed",
    !w.acts.some((a) => a.message.type === "share_drop"));
}

{
  // Both halves of one share.
  const w = world();
  w.s.renderShares([fileShare({ text: LINK })]);
  ok("a share with a file AND text delivers both",
    w.attachments().length === 1 && w.composer() === LINK);
}

// ---- consumed on SEND, for files ------------------------------------------
{
  const w = world();
  w.s.renderShares([fileShare()]);
  w.s.releaseSentShares(w.s.attachments);
  assert.deepStrictEqual(w.sent(), [{ type: "share_drop", id: "s1" }]);
  ok("sending the message is what spends a shared FILE", true);
}

{
  const w = world();
  w.s.renderShares([fileShare()]);
  // The tab goes away without sending: nothing was released, so the server
  // still has it and offers it again. An attachment chip is not persisted.
  ok("a file attached but never sent is never dropped", w.sent().length === 0);
}

{
  const w = world();
  w.s.attachments.push({ name: "mine.pdf", path: "/u/uploads/mine.pdf" }); // picked by hand
  w.s.renderShares([fileShare()]);
  w.s.releaseSentShares(w.s.attachments);
  ok("a file the owner attached themselves is not a share and drops nothing",
    w.sent().length === 1 && w.sent()[0].id === "s1");
}

// ---- the server owns the list ---------------------------------------------
{
  const w = world();
  w.s.renderShares([fileShare()]);
  w.s.renderShares([]); // claimed or dismissed on the phone
  ok("an item the server no longer lists leaves the composer", !w.attachments().length);
}

{
  const w = world();
  w.s.attachments.push({ name: "mine.pdf", path: "/u/uploads/mine.pdf" });
  w.s.renderShares([fileShare()]);
  w.s.renderShares([]);
  assert.deepStrictEqual(w.attachments(), [{ name: "mine.pdf", path: "/u/uploads/mine.pdf" }]);
  ok("…and takes nothing else with it", true);
}

{
  const w = world();
  w.s.renderShares([fileShare()]);
  w.s.renderShares([fileShare()]); // a repaint for an unrelated arrival
  ok("a repeated repaint does not attach the same file twice",
    w.attachments().length === 1);
}

// ---- arriving live is news; the first paint is not ------------------------
{
  const w = world();
  w.s.renderShares([fileShare()]);
  ok("the first paint is silent — a reload must not toast what was already there",
    w.toasts.length === 0);
  w.s.renderShares([fileShare(), fileShare({ id: "s2", name: "b.png", path: "/u/b.png" })]);
  ok("a file arriving while you are looking says so",
    w.toasts.length === 1 && /b\.png/.test(w.toasts[0]) && /iPhone/.test(w.toasts[0]));
}

{
  const w = world();
  w.s.renderShares([]);            // first paint, empty inbox
  w.s.renderShares([textShare()]); // then a link arrives
  ok("a link arriving while you are looking says where it came from",
    w.toasts.some((t) => /Safari/.test(t)), w.toasts.join(" | "));
}

// ---- `chat=new`: a share that wants its own conversation -------------------
// It rides on the ITEM, not on the launch URL, because iOS will not open an
// installed web app at an address of your choosing — `webapp://…/?new` starts
// the app and drops the query. This has to work however the app was opened.
{
  const w = world();
  w.s.renderShares([fileShare({ fresh: true })]);
  ok("a share marked chat=new opens a chat for itself", w.newChats.length === 1);
  ok("…and is still attached, because attachments survive the switch",
    w.attachments().length === 1);
}

{
  const w = world();
  w.s.renderShares([textShare({ fresh: true })]);
  ok("a shared link marked chat=new opens a chat too", w.newChats.length === 1);
  ok("…and the link is in the composer there", w.composer() === LINK);
}

{
  const w = world();
  w.s.renderShares([fileShare()]);
  ok("an ordinary share never moves you", w.newChats.length === 0);
}

{
  // hello repeats the inbox on every connect — and the new chat itself causes
  // one. Without the ledger this loops.
  const w = world();
  w.s.renderShares([fileShare({ fresh: true })]);
  w.s.renderShares([fileShare({ fresh: true })]);
  w.s.renderShares([fileShare({ fresh: true })]);
  ok("three repaints of the same item open ONE chat", w.newChats.length === 1);
}

{
  const w = world();
  w.s.renderShares([
    fileShare({ id: "a", name: "a.png", path: "/u/a.png", fresh: true }),
    fileShare({ id: "b", name: "b.png", path: "/u/b.png", fresh: true }),
  ]);
  ok("photos shared together mean one new chat, not one each", w.newChats.length === 1);
  ok("…holding all of them", w.attachments().length === 2);
}

{
  const w = world({ emptyChat: true });
  w.s.renderShares([fileShare({ fresh: true })]);
  ok("a chat with nothing in it is not worth leaving — no spare empty chat",
    w.newChats.length === 0);
  ok("…and the share is attached right here", w.attachments().length === 1);
}

report("test_shares.js");
