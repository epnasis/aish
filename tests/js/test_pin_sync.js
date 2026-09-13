// The pin ledger's client half: the pin belongs to the OWNER, not to this
// screen — [SEEN]'s finding applied to a toggle. A chat pinned on the laptop
// used to sit in the date buckets on the phone, because the pin lived only in
// each device's IndexedDB mirror.
//
// What this file pins is the properties the sharing rests on, each of which
// has a way of going wrong the user would see:
//
//   1. NO CLIENT TIME. Under last-writer-wins a client clock has no safe
//      direction to be wrong in, so the wire carries no `at` — the server
//      stamps every accepted toggle as it lands.
//   2. THE OUTBOX. A pin toggled offline survives until the server states a
//      verdict, and is retired when one arrives — agreement retires it, a
//      seed retires on ANY verdict, and a pending toggle outranks a
//      DIFFERENT verdict while its own flush is still in flight.
//   3. THE UPGRADE SEEDS ITSELF, and a seed stays a seed: re-offered after a
//      failed connect it must not turn into a fresh action, or it could
//      resurrect a pin the owner deliberately removed on another device.
//   4. ONE AUTHORITY. The map answers everywhere a pin is read; the store's
//      flag is its write-through, and gone-is-gone drops entry AND offer.
//
// The REAL [PIN-SYNC] block is extracted from app.js and run here.
//
// Run manually: node tests/js/test_pin_sync.js
"use strict";

const { sessionWorld, checks, expectReached } = require("./harness");

const { ok, report } = checks();
const done = expectReached("the async pin-sync checks never ran");

const settle = () => new Promise((resolve) => setImmediate(resolve));

/** A world holding the REAL pin block, with a socket that records sends. */
function pinWorld({ stored = null, connected = true, metas = [] } = {}) {
  const sent = [];
  const store = new Map(metas.map((m) => [m.name, m]));
  const puts = [];
  const w = sessionWorld({
    globals: {
      ws: connected ? { readyState: 1, send: (text) => sent.push(JSON.parse(text)) } : null,
      offlineSafe: (promise, fallback = null) => promise.catch(() => fallback),
      idbGet: (_which, key) => Promise.resolve(store.get(key)),
      idbPut: (_which, value) => { puts.push(value); store.set(value.name, value); return Promise.resolve(); },
      idbAll: () => Promise.resolve([...store.values()]),
      offlineMeta: new Map(store),
    },
  });
  w.sandbox.WebSocket = { OPEN: 1 };
  if (stored) w.storage.setItem("aish-pins", JSON.stringify(stored));
  w.load("// [PIN-SYNC-START]", "// [PIN-SYNC-END]");
  w.sent = sent;
  w.puts = puts;
  w.stored = () => JSON.parse(w.storage.getItem("aish-pins"));
  return w;
}

(async () => {
  // ---- 1. pinning here tells the server, with NO time ----------------------
  {
    const w = pinWorld({ metas: [{ name: "a.jsonl", pinned: false }] });
    const s = w.sandbox;
    s.setPin("a.jsonl", true);
    await settle();

    ok("the toggle hands the mark to the server",
      w.sent.length === 1 && w.sent[0].type === "pin"
      && w.sent[0].marks["a.jsonl"].pinned === true);
    ok("…carrying NO time — the server's clock is the only one in the protocol",
      !("at" in w.sent[0].marks["a.jsonl"]));
    ok("the map answers pinned immediately, no round trip (L7)",
      s.pinnedFor("a.jsonl", false) === true);
    ok("the toggle UI refreshes on the tap", w.called("refreshOfflinePinUi") >= 1);
    ok("the store's flag is written through for the eviction planner",
      w.puts.some((m) => m.name === "a.jsonl" && m.pinned === true));
    ok("cache and outbox both survive a reload",
      w.stored().at["a.jsonl"].p === true && w.stored().pending["a.jsonl"].p === true);
  }

  // ---- 2. a pin toggled THERE applies here ---------------------------------
  {
    const w = pinWorld({ metas: [{ name: "b.jsonl", pinned: false }] });
    const s = w.sandbox;
    s.applyPinMarks({ "b.jsonl": { pinned: true, at: 1_700_000_000 } });
    await settle();

    ok("the map now answers pinned", s.pinnedFor("b.jsonl", false) === true);
    ok("…and the store's flag followed",
      w.puts.some((m) => m.name === "b.jsonl" && m.pinned === true));
    ok("nothing was sent back — applying a broadcast never echoes", w.sent.length === 0);
  }

  // ---- 3. a pending toggle outranks a different verdict; agreement retires it
  {
    const w = pinWorld({ connected: false }); // offline: the offer must wait
    const s = w.sandbox;
    s.setPin("c.jsonl", false);
    s.applyPinMarks({ "c.jsonl": { pinned: true, at: 1_700_000_000 } });

    ok("this device's own later toggle stands until its flush lands",
      s.pinnedFor("c.jsonl", true) === false);
    ok("…and the offer is still in the outbox", "c.jsonl" in s.pendingPins);

    s.applyPinMarks({ "c.jsonl": { pinned: false, at: 1_700_000_100 } });
    ok("a verdict stating the same answer retires the offer",
      !("c.jsonl" in s.pendingPins));
    ok("…without flipping the state", s.pinnedFor("c.jsonl", true) === false);
  }

  // ---- 4. an offline toggle survives a reload and re-offers on connect -----
  {
    const w = pinWorld({ stored: { at: { "d.jsonl": { p: true, at: 0 } },
                                   pending: { "d.jsonl": { p: true } } } });
    const s = w.sandbox;
    s.syncPins();
    await settle();

    ok("the connect re-offers the unconfirmed toggle and asks for the ledger",
      w.sent.length >= 1 && w.sent[0].full === true
      && w.sent[0].marks["d.jsonl"].pinned === true);
  }

  // ---- 5. the upgrade seeds itself, and a seed stays a seed ----------------
  {
    const w = pinWorld({
      metas: [
        { name: "old-pin.jsonl", pinned: true },   // pre-sync pin, unknown to the map
        { name: "known.jsonl", pinned: true },     // already in the map: never re-offered
        { name: "plain.jsonl", pinned: false },
      ],
      stored: { at: { "known.jsonl": { p: true, at: 5 } }, pending: {} },
    });
    const s = w.sandbox;
    s.syncPins();
    await settle();

    const seeded = w.sent.find((m) => m.marks["old-pin.jsonl"]);
    ok("a pre-sync local pin is offered once the metas load",
      Boolean(seeded) && seeded.marks["old-pin.jsonl"].pinned === true);
    ok("…flagged as a SEED — it may introduce, never overturn",
      seeded.marks["old-pin.jsonl"].seed === true);
    ok("a name the map already holds is not re-offered",
      !w.sent.some((m) => m.marks["known.jsonl"]));
    ok("an unpinned meta seeds nothing", !w.sent.some((m) => m.marks["plain.jsonl"]));
    ok("the seed flag persists in the outbox, so a re-offer is still a seed",
      w.stored().pending["old-pin.jsonl"].seed === true);

    // The seed LOSES (the owner removed this pin on another device): any
    // verdict retires it — a losing seed produces no broadcast with a newer
    // stamp, so waiting for one would re-offer forever.
    s.applyPinMarks({ "old-pin.jsonl": { pinned: false, at: 3 } });
    ok("a seed retires on any verdict at all", !("old-pin.jsonl" in s.pendingPins));
    ok("…and the ledger's answer stands", s.pinnedFor("old-pin.jsonl", true) === false);
  }

  // ---- 6. the full ledger prunes what the authority no longer holds --------
  {
    const w = pinWorld({ stored: {
      at: { "gone.jsonl": { p: true, at: 5 }, "kept.jsonl": { p: true, at: 5 },
            "mine.jsonl": { p: true, at: 0 } },
      pending: { "mine.jsonl": { p: true } },
    } });
    const s = w.sandbox;
    s.onPinLedger({ pins: { "kept.jsonl": { pinned: true, at: 7 } } });

    ok("an entry the ledger no longer names is dropped",
      s.pinnedFor("gone.jsonl", false) === false);
    ok("one it names stays", s.pinnedFor("kept.jsonl", false) === true);
    ok("a pending offer is never pruned — its flush is the repair",
      s.pinnedFor("mine.jsonl", false) === true && "mine.jsonl" in s.pendingPins);
  }

  // ---- 7. gone is gone, pin included ---------------------------------------
  {
    const w = pinWorld({ stored: { at: { "e.jsonl": { p: true, at: 5 } },
                                   pending: { "e.jsonl": { p: true } } } });
    const s = w.sandbox;
    s.forgetPin("e.jsonl");
    ok("a deleted chat's entry and offer both go",
      s.pinnedFor("e.jsonl", false) === false && !("e.jsonl" in s.pendingPins));
    ok("…durably", w.stored().pending["e.jsonl"] === undefined);
  }

  // ---- 8. private mode degrades, never throws ------------------------------
  {
    const w = pinWorld();
    w.storage.throwOnSet = true;
    const s = w.sandbox;
    s.setPin("f.jsonl", true);
    ok("a storage that refuses to write still pins for this page",
      s.pinnedFor("f.jsonl", false) === true);
  }

  done();
  report("test_pin_sync");
})();
