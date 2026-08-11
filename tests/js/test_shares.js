// Node-only, dependency-free checks for [SHARES] — what the iPhone share sheet
// handed over, and how it reaches a message.
//
// The defect this file exists to stop coming back, reported from a phone:
// "I can see 'shared from iPhone' on the composer but adding a prompt and
// pressing send does not send the attachment, just the prompt."
//
// It did exactly that. A shared file was a chip you had to TAP first, on the
// theory that a share landing mid-sentence must not join a message you were
// already writing. But the strip sits in the composer's attachment zone, so it
// READS as already attached — and the file stayed in the inbox, correctly, and
// completely uselessly, while the words went on their own.
//
// The safety property was never the tap. It is that a share starts NOTHING —
// no session, no model call — which is enforced on the server. So a shared FILE
// is attached to the next message straight away; shared TEXT stays a chip,
// because there is no equivalent of ✕ for text that has appended itself to a
// half-written sentence.
//
// The other rules pinned here:
//   - the server owns the inbox; this reconciles against its list and keeps no
//     copy, because two devices claim from one inbox;
//   - a share is consumed on SEND, not on attach — a tab closed without sending
//     leaves it waiting rather than silently spent;
//   - ✕ tells the server, or the next repaint puts it straight back;
//   - `chat=new` opens one chat for the batch, once, and never from an already
//     empty one.
//
// Run manually: node tests/js/test_shares.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface, fakeElement, checks } = require("./harness");

const { ok, report } = checks();

function world({ emptyChat = false } = {}) {
  const box = fakeElement("div");
  const newChats = [];
  const empty = { value: emptyChat };
  const acts = [];
  const sent = [];
  const inserted = [];
  const toasts = [];
  const sandbox = {
    $: (id) => (id === "shares" ? box : null),
    document: {
      createElement: (tag) => {
        const el = fakeElement(tag);
        const parts = { "share-from": fakeElement("span"), "share-name": fakeElement("span") };
        el.querySelector = (sel) => parts[sel.replace(/^\./, "")] || null;
        el._parts = parts;
        el.append = (...nodes) => el.children.push(...nodes);
        el.setAttribute = (k, v) => { el[k] = v; };
        return el;
      },
    },
    act: (message, opts) => { acts.push({ message, opts: opts || {} }); return true; },
    send: (message) => { sent.push(message); return true; },
    composerInsert: (text) => inserted.push(text),
    showToast: (text) => toasts.push(text),
    renderAttachments: () => {},
    requestNewChat: () => newChats.push(1),
    transcriptIsEmpty: () => empty.value,
    input: { focus() {} },
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
    box,
    acts,
    newChats,
    empty,
    inserted,
    toasts,
    chips: () => box.children,
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
const textShare = (over = {}) =>
  fileShare({ id: "t1", name: "https://example.com/a", path: "", text: "https://example.com/a", ...over });

// ---- the reported bug ------------------------------------------------------
{
  const w = world();
  w.s.renderShares([fileShare()]);
  assert.deepStrictEqual(w.attachments(), [
    { name: "IMG_4021.jpg", path: "/u/uploads/IMG_4021.jpg", share: "s1" },
  ]);
  ok("a waiting shared FILE is attached to the composer with no tap at all", true);
  ok("…and does not also sit in the strip, which would be the same thing twice",
    w.box.hidden === true && w.chips().length === 0);
  ok("…and nothing is consumed yet: send has not happened", w.sent().length === 0);
}

// ---- consumed on SEND, not on attach --------------------------------------
{
  const w = world();
  w.s.renderShares([fileShare()]);
  w.s.releaseSentShares(w.s.attachments);
  assert.deepStrictEqual(w.sent(), [{ type: "share_drop", id: "s1" }]);
  ok("sending the message is what spends the share", true);
}

{
  const w = world();
  w.s.renderShares([fileShare()]);
  // The tab goes away without sending: nothing was released, so the server
  // still has it and offers it again.
  ok("a share attached but never sent is never dropped", w.sent().length === 0);
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
  // Claimed on the phone, or dismissed there: it leaves this composer too.
  w.s.renderShares([]);
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
  ok("a repeated repaint does not attach the same share twice",
    w.attachments().length === 1);
}

// ---- arriving live is news; the first paint is not ------------------------
{
  const w = world();
  w.s.renderShares([fileShare()]);
  ok("the first paint is silent — a reload must not toast what was already there",
    w.toasts.length === 0);
  w.s.renderShares([fileShare(), fileShare({ id: "s2", name: "b.png", path: "/u/b.png" })]);
  ok("a share arriving while you are looking says so",
    w.toasts.length === 1 && /b\.png/.test(w.toasts[0]) && /iPhone/.test(w.toasts[0]));
}

// ---- shared TEXT stays a chip ---------------------------------------------
{
  const w = world();
  w.s.renderShares([textShare()]);
  ok("shared text does NOT type itself into the composer", w.inserted.length === 0);
  ok("…it waits as a chip", !w.box.hidden && w.chips().length === 1);
  ok("…which says where it came from",
    w.chips()[0].children[0]._parts["share-from"].textContent === "Shared from iPhone");

  w.chips()[0].children[0].onclick();
  assert.deepStrictEqual(w.inserted, ["https://example.com/a"]);
  ok("tapping it inserts the text where the cursor is", true);
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(w.acts.map((a) => a.message))),
    [{ type: "share_drop", id: "t1" }],
  );
  ok("…and asks the server to clear it", true);
}

{
  // A share carrying BOTH is a chip: the text half has to be placed by hand.
  const w = world();
  w.s.renderShares([fileShare({ text: "https://example.com/a" })]);
  ok("a share with text AND a file waits rather than half-attaching",
    !w.box.hidden && w.attachments().length === 0);
  w.chips()[0].children[0].onclick();
  ok("…and tapping takes both halves",
    w.attachments().length === 1 && w.inserted.length === 1);
}

// ---- dismissing ------------------------------------------------------------
{
  const w = world();
  w.s.renderShares([textShare()]);
  w.chips()[0].children[1].onclick();
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(w.acts.map((a) => a.message))),
    [{ type: "share_drop", id: "t1" }],
  );
  ok("dismissing a chip is the same server-side operation as claiming it", true);
  ok("…and attaches nothing on the way out", !w.attachments().length);
}

// ---- the claim that never lands -------------------------------------------
{
  // [ACK-LEDGER]: the item is still in the inbox, so the composer must not go
  // on holding it — the next repaint would re-offer it and attach it twice.
  const w = world();
  w.s.renderShares([fileShare({ text: "note" })]);
  w.chips()[0].children[0].onclick();
  ok("the claim carries a repair", typeof w.acts[0].opts.lost === "function");
  w.acts[0].opts.lost();
  ok("a lost claim takes the attachment back out", !w.attachments().length);
}

// ---- `chat=new`: a share that wants its own conversation -----------------
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
  w.s.renderShares([fileShare()]);
  ok("an ordinary share never moves you", w.newChats.length === 0);
}

{
  // hello repeats the inbox on every connect and every session switch — which
  // is exactly what the new chat causes. Without the ledger this loops.
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
  ok("three photos shared at once means one new chat, not three",
    w.newChats.length === 1);
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
