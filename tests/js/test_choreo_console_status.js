// Choreography pin for the #180 follow-up — the placeholder that outlived the
// real status label (issue #181).
//
// openConsole writes provisional text ("attaching…", then "loading terminal…"
// around the lazy xterm load, then "attaching…" again once the emulator is
// ready), while the server answers the attach with console_started — the ONLY
// source of the real label, fired exactly once per open. Nothing rewrites the
// label afterwards, so a single late placeholder write is PERMANENT: "attaching…"
// sat over a fully working terminal until the next open.
//
// The decision function was never wrong. What was wrong was the ORDER, and the
// order is not the client's to choose: it depends on link latency versus an
// await. So this pins the property over ALL the interleavings that can occur —
// once the real label has landed, no provisional write may replace it, whenever
// it arrives — by driving the REAL setProvisionalConsoleStatus / setConsoleStatus
// / onConsoleStarted from app.js against a fake status element.
//
// (test_xterm_lazy.js pins the structural half: that every provisional write
// goes through the guarded helper rather than straight to setConsoleStatus.)
//
// Run manually: node tests/js/test_choreo_console_status.js
"use strict";

const { hostileWorld, checks } = require("./harness");

const { ok, report } = checks();

const REAL_LABEL = "tmux · aish-console";

function consoleWorld() {
  const label = { textContent: "", classList: { toggle() {} } };
  const w = hostileWorld({
    globals: {
      $: () => label,
      consoleOpen: true,
      consoleTerm: { reset() {}, write() {} },
      consoleFitAndResize() {},
    },
  });
  // The status helpers plus their guard flag, exactly as shipped.
  w.load("const CONSOLE_LOADING", "function setConsoleCtrlMode");
  // …and the one writer of the REAL label.
  w.load("function onConsoleStarted(event) {", "function onConsoleOut");
  w.label = label;
  // The four things that can happen during one open, in any order.
  w.act = (what) => {
    const s = w.sandbox;
    if (what === "open") s.consoleStartedSeen = false; // openConsole arms the guard
    else if (what === "attaching") s.setProvisionalConsoleStatus("attaching…");
    else if (what === "loading") s.setProvisionalConsoleStatus(s.CONSOLE_LOADING);
    else if (what === "started") s.onConsoleStarted({ type: "console_started", command: REAL_LABEL });
    else throw new Error(`unknown action ${what}`);
  };
  w.play = (seq) => { for (const a of seq) w.act(a); return label.textContent; };
  return w;
}

// ---- 1. The two orderings named in the incident --------------------------
{
  // (a) Slow link: the placeholders land first and the reply arrives last —
  //     the happy path, and the one the code was written for.
  const slow = consoleWorld();
  const shown = [];
  slow.act("open");
  slow.act("attaching");
  shown.push(slow.label.textContent);
  slow.act("loading");
  shown.push(slow.label.textContent);
  slow.act("started");
  ok("a placeholder IS shown while the attach is in flight",
    shown[0] === "attaching…" && shown[1] === slow.sandbox.CONSOLE_LOADING);
  ok("the real label lands", slow.label.textContent === REAL_LABEL);

  // (b) Fast link — the incident. console_open goes out first, the reply beats
  //     every provisional write, and the post-await write then clobbered it.
  const fast = consoleWorld();
  fast.act("open");
  fast.act("started");
  ok("the real label can arrive before any placeholder", fast.label.textContent === REAL_LABEL);
  fast.act("attaching");
  fast.act("loading");
  fast.act("attaching"); // the write after the lazy-xterm await resolves
  ok("no late placeholder replaces it", fast.label.textContent === REAL_LABEL);
}

// ---- 2. The property, over every interleaving ----------------------------
// Where the reply falls among the writes is decided by the network and an
// await, not by us. Whatever the order, an open that saw console_started must
// end showing the real label.
{
  const writes = ["attaching", "loading", "attaching"];
  for (let at = 0; at <= writes.length; at += 1) {
    const seq = ["open", ...writes.slice(0, at), "started", ...writes.slice(at)];
    const w = consoleWorld();
    const final = w.play(seq);
    ok(`the real label survives console_started at position ${at} (${seq.join(" → ")})`,
      final === REAL_LABEL);
  }
}

// ---- 3. Real statuses still replace real statuses -------------------------
// The guard suppresses PLACEHOLDERS, not the truth: when the shell exits, that
// message must land over the started label. Suppressing it would trade one
// stale label for another.
{
  const w = consoleWorld();
  w.play(["open", "attaching", "started"]);
  w.sandbox.setConsoleStatus("exited (0) — tap the console button to retry", true);
  ok("an exit notice overwrites the started label",
    w.label.textContent.startsWith("exited (0)"));
}

// ---- 4. A fresh open shows placeholders again -----------------------------
// The guard is per-open: re-arming it is what keeps the SECOND open from
// starting out mute behind a stale "connected" label.
//
// NOTE for phase 3 (#181): openConsole re-arms this flag BEFORE its early
// returns, so an open-while-open call disarms the guard without re-arming it on
// any subsequent reply. That is a latent defect, not current behaviour under
// test — it is deliberately not pinned here.
{
  const w = consoleWorld();
  w.play(["open", "attaching", "started"]);
  w.act("open");
  w.act("attaching");
  ok("a new open may write its placeholder again", w.label.textContent === "attaching…");
  w.act("started");
  ok("…and the second open's real label lands too", w.label.textContent === REAL_LABEL);
}

// ---- 5. console_started before the terminal exists is dropped, not shown ---
// onConsoleStarted returns early when there is no terminal to reset — the reply
// belongs to an open that is gone. It must not stamp a label onto a closed
// overlay, and it must not arm the guard for an open that never happened.
{
  const w = consoleWorld();
  w.act("open");
  w.sandbox.consoleTerm = null;
  w.act("started");
  ok("a reply with no terminal writes no label", w.label.textContent === "");
  ok("…and leaves the guard un-armed", w.sandbox.consoleStartedSeen === false);
  w.sandbox.consoleTerm = { reset() {}, write() {} };
  w.act("attaching");
  ok("so the next open's placeholder still works", w.label.textContent === "attaching…");
}

report("test_choreo_console_status.js");
