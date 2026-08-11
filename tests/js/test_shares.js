// Node-only, dependency-free checks for [SHARES] — the iPhone share sheet's
// landing strip.
//
// iOS cannot register a PWA as a share target, so a share arrives over HTTP
// from a Shortcut and is PARKED server-side. Two properties matter here, and
// both are about who owns the list:
//
//   - the server owns it. This renders what the last hello/`shared` said and
//     keeps no local copy, because two devices claim from one inbox (the phone
//     shares, the laptop uses) and a local list drifts the moment the other one
//     claims something. So removal is a REQUEST, never a splice.
//   - claiming is a TAP. A share landing mid-sentence must not attach itself to
//     the message you are about to send — parking it is precisely so you choose
//     when, and which chat.
//
// Run manually: node tests/js/test_shares.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface, fakeElement, checks } = require("./harness");

const { ok, report } = checks();

function world() {
  const box = fakeElement("div");
  const acts = [];
  const inserted = [];
  const sandbox = {
    $: (id) => (id === "shares" ? box : null),
    document: {
      createElement: (tag) => {
        const el = fakeElement(tag);
        // The chip builds its two labels via innerHTML and then queries for
        // them; give those queries real nodes so the wiring is exercised.
        const parts = { "share-from": fakeElement("span"), "share-name": fakeElement("span") };
        el.querySelector = (sel) => parts[sel.replace(/^\./, "")] || null;
        el._parts = parts;
        el.append = (...nodes) => el.children.push(...nodes);
        el.setAttribute = (k, v) => { el[k] = v; };
        return el;
      },
    },
    act: (message, opts) => { acts.push({ message, opts: opts || {} }); return true; },
    composerInsert: (text) => inserted.push(text),
    renderAttachments: () => {},
    input: { focus() {} },
    attachments: [],
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
    inserted,
    chips: () => box.children,
    attachments: () => JSON.parse(JSON.stringify(sandbox.attachments)),
  };
}

const share = (over = {}) => ({
  id: "s1",
  name: "IMG_4021.jpg",
  path: "/u/uploads/IMG_4021.jpg",
  text: "",
  source: "iPhone",
  ...over,
});

// ---- rendering ------------------------------------------------------------
{
  const w = world();
  w.s.renderShares([]);
  ok("an empty inbox shows no strip at all", w.box.hidden === true);

  w.s.renderShares([share()]);
  ok("…and one waiting item shows one", w.box.hidden === false && w.chips().length === 1);
  const chip = w.chips()[0];
  ok("the chip says where it came from",
    chip.children[0]._parts["share-from"].textContent === "Shared from iPhone");
  ok("…and what it is", chip.children[0]._parts["share-name"].textContent === "IMG_4021.jpg");
}

{
  // A share with no file is a URL or a note — the commonest thing iOS shares.
  const w = world();
  w.s.renderShares([share({ path: "", name: "", text: "https://example.com/article" })]);
  ok("a text-only share is still named on the chip",
    w.chips()[0].children[0]._parts["share-name"].textContent === "https://example.com/article");
}

// ---- claiming -------------------------------------------------------------
{
  const w = world();
  w.s.renderShares([share()]);
  ok("a rendered share attaches NOTHING until it is tapped", w.attachments().length === 0);

  w.chips()[0].children[0].onclick();
  assert.deepStrictEqual(w.attachments(), [
    { name: "IMG_4021.jpg", path: "/u/uploads/IMG_4021.jpg" },
  ]);
  ok("tapping moves it into the composer as an attachment", true);
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(w.acts.map((a) => a.message))),
    [{ type: "share_drop", id: "s1" }],
  );
  ok("…and asks the SERVER to clear it, rather than splicing a local list", true);
  ok("the strip is not repainted locally — the broadcast does that",
    w.chips().length === 1);
}

{
  // Shared text is text: making the owner open a file to read a link they
  // shared would be absurd.
  const w = world();
  w.s.renderShares([share({ path: "", text: "https://example.com/article" })]);
  w.chips()[0].children[0].onclick();
  assert.deepStrictEqual(w.inserted, ["https://example.com/article"]);
  ok("shared text goes into the composer as text", true);
  ok("…and attaches no phantom file", w.attachments().length === 0);
}

{
  // A share can carry both (a page shared as URL + screenshot).
  const w = world();
  w.s.renderShares([share({ text: "https://example.com/article" })]);
  w.chips()[0].children[0].onclick();
  ok("both halves of one share are taken",
    w.attachments().length === 1 && w.inserted.length === 1);
}

// ---- dismissing -----------------------------------------------------------
{
  const w = world();
  w.s.renderShares([share()]);
  w.chips()[0].children[1].onclick();
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(w.acts.map((a) => a.message))),
    [{ type: "share_drop", id: "s1" }],
  );
  ok("dismissing is the same server-side operation as claiming", true);
  ok("…and attaches nothing on the way out", w.attachments().length === 0);
}

// ---- the claim that never lands -------------------------------------------
{
  // [ACK-LEDGER]: if the drop is not receipted the item is still in the inbox,
  // so the composer must not go on holding it — the next repaint would re-offer
  // it and it would be attached twice.
  const w = world();
  w.s.renderShares([share()]);
  w.chips()[0].children[0].onclick();
  ok("the claim carries a repair", typeof w.acts[0].opts.lost === "function");
  ok("…and it is named in the user's words, for the toast",
    /shared item/.test(w.acts[0].opts.label || ""));
  w.acts[0].opts.lost();
  ok("a lost claim takes the attachment back out of the composer",
    w.attachments().length === 0);
}

{
  // The repair must remove the RIGHT one: a composer holding a file the owner
  // picked themselves must not lose it because an unrelated claim went missing.
  const w = world();
  w.s.attachments.push({ name: "notes.pdf", path: "/u/uploads/notes.pdf" });
  w.s.renderShares([share()]);
  w.chips()[0].children[0].onclick();
  w.acts[0].opts.lost();
  assert.deepStrictEqual(w.attachments(), [
    { name: "notes.pdf", path: "/u/uploads/notes.pdf" },
  ]);
  ok("…and leaves everything else the composer was holding alone", true);
}

// ---- the repaint ----------------------------------------------------------
{
  // The server's list is the truth, including "the other device claimed it".
  const w = world();
  w.s.renderShares([share(), share({ id: "s2", name: "b.png", path: "/u/b.png" })]);
  ok("two waiting items, two chips", w.chips().length === 2);
  w.s.renderShares([share({ id: "s2", name: "b.png", path: "/u/b.png" })]);
  ok("a broadcast that drops one repaints to exactly what the server said",
    w.chips().length === 1);
  w.s.renderShares([]);
  ok("…and an emptied inbox takes the strip away", w.box.hidden === true);
}

report("test_shares.js");
