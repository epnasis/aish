// Watch mode: the browser sheet as a read-only window onto the page aish is
// driving in THIS chat ([BROWSER-WATCH], #289 slice 2).
//
// The owner asked for it in one sentence — "I actually want to see what it
// does" — and the thing that makes it safe is not that it declines to do
// anything interesting. It is that every gate decision downstream is made
// against the page as the MODEL was shown it, so a human scrolling changes the
// reachable set and a resize renames controls; the act-time fence would then
// correctly refuse each act and the flow reads as the model flailing.
//
// What this pins, and each is a sentence in the docs that has to be enforced by
// a line rather than by the order things happen to be called in:
//   * READ-ONLY: every gesture and control that can reach the page — click,
//     scroll, goto, back, refresh, resize, the sharpening patch — sends
//     NOTHING while watching, and the same paths still work while driving
//   * the rule has exactly one owner (`bvMayTouchPage`) and every `browser_view`
//     sender in app.js consults it — checked against the SOURCE, so a new
//     sender cannot quietly become a third path
//   * zoom and pan stay, because they are entirely local — including double-tap
//   * a frame paints the picture and never composites a word into it
//   * the two modes are exclusive: entering one leaves the other
//   * a frame for another chat, or after the sheet closed, paints nothing
//
// Run manually: node tests/js/test_browser_watch.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();
const src = appSource();

/** A stage-shaped fake element. Watch mode never reads `bvFrame`, so the
 *  numbers here only have to be self-consistent for the zoom maths. */
function element() {
  const el = {
    hidden: false,
    value: "",
    readOnly: false,
    textContent: "",
    src: "",
    style: {},
    _classes: new Set(),
    classList: {
      add: (c) => el._classes.add(c),
      remove: (c) => el._classes.delete(c),
      toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
      contains: (c) => el._classes.has(c),
    },
    removeAttribute: (name) => { if (name === "src") el.src = ""; },
    getBoundingClientRect: () => ({
      left: 0, top: 0, right: 430, bottom: 655, width: 430, height: 655,
    }),
    parentElement: {
      getBoundingClientRect: () => ({
        left: 0, top: 0, right: 430, bottom: 655, width: 430, height: 655,
      }),
    },
    addEventListener() {},
    focus() {},
  };
  return el;
}

function world({ session = "chat-a" } = {}) {
  const sent = [];
  const nodes = {};
  const $ = (id) => (nodes[id] ||= element());
  const sandbox = {
    currentSession: session,
    bvProfile: "",
    bvOpen: false,
    bvBusy: false,
    bvFrame: { width: 1280, height: 1950 },
    bvZoom: { scale: 1, x: 0, y: 0 },
    bvFocusRect: null,
    bvDetail: null,
    bvDetailTimer: null,
    bvFrameSeq: 0,
    bvBusyTimer: null,
    BV_DETAIL_MARGIN: 1.15,
    BV_DETAIL_PAD: 1.25,
    $,
    send: (m) => { sent.push(m); return true; },
    showToast: () => {},
    openSheet: (name) => { nodes[name] = nodes[name] || element(); $(name).hidden = false; },
    restoreConsoleFocus: () => {},
    bvIdle: () => {},
    bvClearDetail: () => {},
    bvResetZoom: () => {},
    bvPaint: () => {},
    bvPaintDetail: () => {},
    bvPaintFocus: () => {},
    bvVisiblePageRect: () => ({ x: 0, y: 0, w: 400, h: 600, need: 3 }),
    bvFrameDensity: () => 1,
    bvDetailCovers: () => false,
    setTimeout: (fn) => { sandbox.__timer = fn; return 1; },
    clearTimeout: () => {},
    window: { devicePixelRatio: 2 },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(
      // The mode itself…
      extract(src, "// [BROWSER-WATCH-START]", "// [BROWSER-WATCH-END]")
      // …the funnel every sheet dismissal goes through…
      + extract(src, "// [BROWSER-VIEW-END-START]", "// [BROWSER-WATCH-START]")
      // …and BOTH senders that can reach the page: bvSend (every interaction)
      // and bvRequestDetail (which deliberately bypasses it).
      + extract(src, "function bvSend(message) {", "function onBrowserView(event) {")
      + extract(src, "function bvRequestDetail() {", "/** Lay the patch over")
    ),
    sandbox
  );
  return { sandbox, sent, nodes, $ };
}

// ---- read-only ------------------------------------------------------------

{
  const { sandbox, sent, nodes } = world();
  sandbox.openWatchView();
  ok("entering watch mode asks the server to start one",
    sent.some((m) => m.type === "browser_watch" && m.action === "start"));
  ok("the sheet is opened", nodes["browser-sheet"].hidden === false);
  ok("and it is marked as watching",
    nodes["browser-sheet"].classList.contains("bv-watching"));
  ok("the address bar is an address to READ, not one to type into",
    nodes["bv-url"].readOnly === true);

  sent.length = 0;
  // Every interaction the sheet can produce, by the exact shape wireBrowserView
  // sends them in. `resize` is on the list on purpose: in watch mode it does
  // not exist, because it would re-lay-out the page under the model and cross a
  // responsive breakpoint that renames its controls.
  const gestures = [
    { action: "click", x: 10, y: 20 },
    { action: "scroll", dy: 600 },
    { action: "goto", url: "https://evil.example" },
    { action: "back" },
    { action: "refresh" },
    { action: "resize", width: 430, height: 655 },
    { action: "type", text: "hello" },
    { action: "recent" },
  ];
  for (const g of gestures) {
    ok(`watching: ${g.action} sends nothing`, sandbox.bvSend(g) === false);
  }
  sandbox.bvRequestDetail();
  ok("watching: no sharpening patch is asked for either", sent.length === 0);
  ok("watching: nothing at all reached the socket", sent.length === 0);
}

// The same paths, while DRIVING. A read-only test that never checks the
// ordinary case passes just as well on code that sends nothing ever.
{
  const { sandbox, sent, $ } = world();
  $("browser-sheet").hidden = false;
  sandbox.bvOpen = true;
  ok("driving: an interaction goes out",
    sandbox.bvSend({ action: "click", x: 1, y: 2 }) !== false);
  ok("driving: it really was a browser_view message",
    sent.some((m) => m.type === "browser_view" && m.action === "click"));
  sent.length = 0;
  sandbox.bvRequestDetail();
  ok("driving: a sharpening is asked for",
    sent.some((m) => m.type === "browser_view" && m.action === "detail"));
}

// ---- the rule has ONE owner, and every sender asks it ----------------------
//
// The read-only guarantee is exactly as wide as the set of senders that consult
// `bvMayTouchPage`. Checked against the source rather than by exercising the
// paths, because the failure this guards against is a sender nobody thought to
// exercise — the eighth finding on this epic's shape.
{
  const owners = [...src.matchAll(/function bvMayTouchPage\(\)/g)].length;
  ok("bvMayTouchPage is defined exactly once", owners === 1);

  /** The function body a match sits in, back to the nearest `function name(`. */
  function enclosing(index) {
    const start = src.slice(0, index).lastIndexOf("\nfunction ");
    assert(start !== -1, "no enclosing function");
    return src.slice(start, index);
  }
  const senders = [...src.matchAll(/send\(\{\s*type:\s*"browser_view"/g)].map((m) => {
    const body = enclosing(m.index);
    return { name: /\nfunction (\w+)/.exec(body)[1], body };
  });
  // THREE, and the third is the one worth naming rather than waving through.
  // `bvSend` and `bvRequestDetail` are the two that can reach the page.
  // `bvEndIfOpen` sends only `close`, which ends the OWNER's own view and
  // touches no page — and only a view this client has actually seen a frame
  // from (`bvOpen`), which is what keeps it from telling the server to tear
  // down a browser that is not there.
  ok(`browser_view senders are the three known ones (found: ${
    senders.map((s) => s.name).join(", ")})`,
    senders.length === 3
    && senders.map((s) => s.name).sort().join(",")
       === "bvEndIfOpen,bvRequestDetail,bvSend");
  for (const s of senders) {
    if (s.name === "bvEndIfOpen") {
      ok("bvEndIfOpen only ever closes, and only a view it has seen a frame from",
        /action:\s*"close"/.test(src.slice(src.indexOf(s.body) + s.body.length,
                                           src.indexOf(s.body) + s.body.length + 80))
        && /if \(!bvOpen\) return;/.test(s.body));
      continue;
    }
    ok(`${s.name} consults bvMayTouchPage before it can touch the page`,
      s.body.includes("bvMayTouchPage()"));
  }
}

// ---- zoom and pan stay -----------------------------------------------------
//
// They are local: the picture on the phone is enlarged, the page is untouched.
// This is what the invariant means by "zoom and pan stay client-side", and it
// is also why the tap path still ARMS the double-tap timer while watching —
// without that the second tap of a pair takes the first-tap path and a
// double-tap never zooms.
{
  const releaseSrc = extract(
    src, "    if (!bvMayTouchPage()) {", "    const point = browserViewPoint("
  );
  ok("a tap while watching still arms the double-tap timer",
    /bvTapTimer\s*=\s*setTimeout/.test(releaseSrc));
  ok("...and paints no tap marker for a press that will not happen",
    !releaseSrc.includes("bvMarkTap"));
  const zoomBranch = extract(src, "    if (bvTapTimer) {", "    if (!bvMayTouchPage()) {");
  ok("the double-tap zoom branch is reached before the read-only gate, so zoom "
    + "works in both modes",
    zoomBranch.includes("bvZoomAt") && zoomBranch.includes("bvResetZoom"));
}

// ---- painting a frame ------------------------------------------------------

{
  const { sandbox, nodes } = world();
  sandbox.openWatchView();
  sandbox.onBrowserWatch({
    type: "browser_watch", action: "frame", session: "chat-a",
    jpeg: "AAAA", url: "https://eon.pl/mojeon", title: "Moje eON",
  });
  ok("the frame paints as the image's src", nodes["bv-frame"].src.endsWith("AAAA"));
  ok("the address shows where aish is", nodes["bv-url"].value === "https://eon.pl/mojeon");
  // NOTHING aish says is ever rendered inside the frame. The picture is the
  // site's own document as an <img>; every word this mode writes goes to the
  // status line, OUTSIDE it. Stated because the residual attack is the page
  // painting "aish says: enter your card here" in pixels.
  ok("the title is text on the status line, never composited into the picture",
    nodes["bv-status"].textContent === "Moje eON"
    && !nodes["bv-frame"].textContent);

  sandbox.onBrowserWatch({
    type: "browser_watch", action: "idle", session: "chat-a", reason: "no-page",
  });
  ok("no page reads as aish being between pages, not as a broken window",
    /not on a page/.test(nodes["bv-status"].textContent));
  ok("...and the previous picture is taken down rather than left claiming to be now",
    nodes["bv-frame"].src === "");

  sandbox.onBrowserWatch({
    type: "browser_watch", action: "idle", session: "chat-a", reason: "hands",
  });
  ok("his own browser taking the page says exactly that",
    /your own browser/.test(nodes["bv-status"].textContent));

  sandbox.onBrowserWatch({
    type: "browser_watch", action: "idle", session: "chat-a", reason: "wat",
  });
  ok("an unknown reason is still a sentence, never a blank",
    nodes["bv-status"].textContent === "no picture");
}

// A frame that outlives the sheet paints nothing. The session firewall drops
// another chat's; this is the other half — the sheet being closed, or switched
// back to driving, between the server's capture and its delivery.
{
  const { sandbox, nodes } = world();
  sandbox.openWatchView();
  nodes["bv-frame"].src = "";
  sandbox.bvEndWatch();
  sandbox.onBrowserWatch({
    type: "browser_watch", action: "frame", session: "chat-a", jpeg: "AAAA",
    url: "https://evil.example", title: "",
  });
  ok("a frame arriving after the watch ended paints nothing",
    nodes["bv-frame"].src === "");
  ok("...and leaves the address bar typable again", nodes["bv-url"].readOnly === false);
}

{
  const { sandbox, nodes } = world();
  sandbox.openWatchView();
  nodes["browser-sheet"].hidden = true;   // dismissed by the backdrop, say
  sandbox.onBrowserWatch({
    type: "browser_watch", action: "frame", session: "chat-a", jpeg: "BBBB",
    url: "https://eon.pl", title: "",
  });
  ok("a frame arriving after the sheet was dismissed paints nothing",
    nodes["bv-frame"].src === "");
}

// ---- the two modes are exclusive ------------------------------------------

{
  const { sandbox, sent, nodes } = world();
  sandbox.openWatchView();
  sent.length = 0;
  // Every route that hides the sheet funnels through here, which is why the
  // watch hangs off it rather than off each dismissal in turn.
  sandbox.bvEndIfOpen();
  ok("dismissing the sheet stops the watch",
    sent.some((m) => m.type === "browser_watch" && m.action === "stop"));
  ok("the watching mark is gone",
    !nodes["browser-sheet"].classList.contains("bv-watching"));
  ok("and the sheet can touch a page again", sandbox.bvMayTouchPage() === true);

  sent.length = 0;
  sandbox.bvEndIfOpen();
  ok("ending a watch twice says nothing twice",
    !sent.some((m) => m.type === "browser_watch"));
}

{
  const { sandbox, nodes } = world();
  sandbox.openWatchView();
  ok("watch mode is on", sandbox.bvMayTouchPage() === false);
  // His own browser OUTRANKS: openBrowserView calls bvEndWatch on the way in.
  // Simulated here because openBrowserView lives outside this extraction; the
  // call site is pinned by test_browser_search_profile.js's stub.
  sandbox.bvEndWatch();
  ok("opening his own browser leaves watch mode first",
    sandbox.bvMayTouchPage() === true
    && !nodes["browser-sheet"].classList.contains("bv-watching"));
  ok("the source really does end the watch when his browser opens",
    /function openBrowserView\([^)]*\)\s*\{[\s\S]{0,600}?bvEndWatch\(\)/.test(src));
}

// ---- the interleaving: a frame that outlives the chat it belongs to --------
//
// The watcher runs on the server and captures on its own clock, so a frame can
// be in the outbox at the instant the owner switches chats. `_leave` ends the
// watch, but the frame already queued still arrives — and it is a picture of
// ANOTHER chat's page, which is exactly what the session firewall exists for.
// This is the reuse the issue asked to decide about: a stamped event of a NEW
// kind, rather than a new field on `browser_view`, because the firewall is
// kind-agnostic (it drops anything stamped with a name that is not on screen)
// while `browser_view`'s handler is a driving-mode state machine that must not
// be fed a frame nobody asked for.
{
  const sandbox = { SESSION_CROSS_EVENTS: null };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(src, "// [SESSION-FIREWALL-START]", "// [SESSION-FIREWALL-END]")),
    sandbox
  );
  const frame = { type: "browser_watch", action: "frame", session: "chat-b" };
  ok("a watch frame for another chat is dropped at the socket",
    sandbox.foreignSessionEvent(frame, "chat-a") === true);
  ok("...and one for the chat on screen is not",
    sandbox.foreignSessionEvent({ ...frame, session: "chat-a" }, "chat-a") === false);
  ok("browser_watch is not exempted from the firewall",
    !sandbox.SESSION_CROSS_EVENTS.has("browser_watch"));
}

// With no chat on screen there is nothing chat-scoped to watch.
{
  const { sandbox, sent } = world({ session: null });
  sandbox.openWatchView();
  ok("no chat, no watch", !sent.some((m) => m.type === "browser_watch"));
}

report("test_browser_watch.js");
