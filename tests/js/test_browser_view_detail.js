// When zooming is worth a round trip ([BROWSER-VIEW-DETAIL], #227).
//
// A frame is sharp only up to `zoom == its own density`; past that the phone is
// magnifying a JPEG, which is what the owner met above 2.5x. The fix is to
// re-capture just the rectangle he is looking at — but a round trip costs 1-3
// seconds, so the decision to spend one is the whole feature, and it is the
// part that is invisible when wrong: too eager and the view is chatty for a
// picture that was already good enough, too shy and it stays blurred.
//
// What this pins:
//   * the visible page rect is read from the LIVE transformed geometry, so it
//     cannot drift from what `browserViewPoint` believes
//   * density is read off the PICTURE, not from a copy of the server's setting
//   * no trip while the frame can already show it, at any devicePixelRatio
//   * no second trip for ground already covered — and one as soon as it is not
//
// Run manually: node tests/js/test_browser_view_detail.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();

/** The REAL functions, sliced out of the shipped app.js and run against a
 *  scripted DOM. `stage` is the box the picture is shown in; `box` is the
 *  frame element's rect with its transform ALREADY applied, which is how the
 *  browser reports it and how the shipped code reads it. */
function world({ frame, stage, box, dpr, natural, sheetHidden = false, open = true }) {
  const sent = [];
  const nodes = {
    "bv-frame": {
      naturalWidth: natural,
      style: { transform: "" },
      parentElement: { getBoundingClientRect: () => stage },
      getBoundingClientRect: () => box,
    },
    "bv-detail-layer": { hidden: true, style: {} },
    "bv-detail": { src: "", style: {}, onload: null },
    "browser-sheet": { hidden: sheetHidden },
  };
  const sandbox = {
    bvFrame: frame,
    bvOpen: open,
    window: { devicePixelRatio: dpr },
    setTimeout: (fn) => { sandbox.__timer = fn; return 1; },
    clearTimeout: () => {},
    $: (id) => nodes[id],
    send: (m) => { sent.push(m); return true; },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(
      extract(
        appSource(),
        "// [BROWSER-VIEW-DETAIL-START]",
        "// [BROWSER-VIEW-DETAIL-END]"
      )
    ),
    sandbox
  );
  return { sandbox, sent, nodes };
}

// A 1280x1950 frame in a 430x655 stage — the real shape. At 1x the picture
// exactly fills the stage, so the transformed box IS the stage.
const FRAME = { width: 1280, height: 1950 };
const STAGE = { left: 0, top: 0, right: 430, bottom: 655, width: 430, height: 655 };

/** The frame element's rect at `zoom`, centred (as bvClamp keeps it when the
 *  pan is zero): the browser scales about the element's centre. */
function zoomed(zoom) {
  const w = STAGE.width * zoom;
  const h = STAGE.height * zoom;
  const left = STAGE.left - (w - STAGE.width) / 2;
  const top = STAGE.top - (h - STAGE.height) / 2;
  return { left, top, right: left + w, bottom: top + h, width: w, height: h };
}

// ---- the visible rectangle ------------------------------------------------
{
  const { sandbox } = world({ frame: FRAME, stage: STAGE, box: zoomed(1),
                              dpr: 3, natural: 2560 });
  const r = sandbox.bvVisiblePageRect();
  ok("at 1x the whole page is visible",
    Math.round(r.x) === 0 && Math.round(r.y) === 0 &&
    Math.round(r.w) === 1280 && Math.round(r.h) === 1950);
  // 430 CSS px of stage across 1280 CSS px of page, on a 3x screen.
  ok("the needed scale is the screen's own pixels per page pixel",
    Math.abs(r.need - (430 / 1280) * 3) < 0.001);
}

{
  const { sandbox } = world({ frame: FRAME, stage: STAGE, box: zoomed(2.5),
                              dpr: 3, natural: 2560 });
  const r = sandbox.bvVisiblePageRect();
  ok("at 2.5x a quarter of the width is visible, centred",
    Math.abs(r.w - 1280 / 2.5) < 1 && Math.abs(r.x - (1280 - 1280 / 2.5) / 2) < 1);
  ok("the needed scale rises with the zoom", Math.abs(r.need - 2.52) < 0.02);
}

// ---- density is read off the picture --------------------------------------
{
  const { sandbox } = world({ frame: FRAME, stage: STAGE, box: zoomed(1),
                              dpr: 3, natural: 2560 });
  ok("a 2560px image of a 1280px page is density 2", sandbox.bvFrameDensity() === 2);
}
{
  const { sandbox } = world({ frame: FRAME, stage: STAGE, box: zoomed(1),
                              dpr: 3, natural: 0 });
  ok("an unloaded picture has no density, and asks for nothing",
    sandbox.bvFrameDensity() === 0);
  sandbox.bvRequestDetail();
}

// ---- when a trip is worth it ----------------------------------------------
{
  // Fit. The frame carries 2x and the screen wants ~1x: nothing to gain.
  const { sandbox, sent } = world({ frame: FRAME, stage: STAGE, box: zoomed(1),
                                    dpr: 3, natural: 2560 });
  sandbox.bvRequestDetail();
  ok("no trip at fit — the frame already has the pixels", sent.length === 0);
}
{
  // 1.5x zoom, density 2: still inside what the frame carries.
  const { sandbox, sent } = world({ frame: FRAME, stage: STAGE, box: zoomed(1.5),
                                    dpr: 3, natural: 2560 });
  sandbox.bvRequestDetail();
  ok("no trip while zooming within the frame's own density", sent.length === 0);
}
{
  // The double-tap. 2.5x against a 2x frame is past parity.
  const { sandbox, sent } = world({ frame: FRAME, stage: STAGE, box: zoomed(2.5),
                                    dpr: 3, natural: 2560 });
  sandbox.bvRequestDetail();
  ok("the double-tap asks for detail", sent.length === 1);
  const m = sent[0];
  ok("it asks for the scale its own screen can show",
    Math.abs(m.scale - 2.52) < 0.02);
  ok("it asks for MORE than the screen shows, so a small pan stays sharp",
    m.w > 1280 / 2.5 && m.w <= 1280);
  ok("the padded rect stays inside the page",
    m.x >= 0 && m.y >= 0 && m.x + m.w <= FRAME.width && m.y + m.h <= FRAME.height);
  ok("it is stamped with the frame it was aimed at", m.token === sandbox.bvFrameSeq);
  ok("it does not go through the interaction guard, so it cannot eat a tap",
    m.type === "browser_view" && m.action === "detail");
}
{
  // A 2x phone wants less than a 3x phone at the same zoom — the client asks
  // for what its own screen can show rather than for a number baked in here.
  const { sandbox, sent } = world({ frame: FRAME, stage: STAGE, box: zoomed(2.5),
                                    dpr: 2, natural: 2560 });
  sandbox.bvRequestDetail();
  ok("a 2x screen at 2.5x zoom is still inside a 2x frame, so no trip",
    sent.length === 0);
}
{
  const { sandbox, sent } = world({ frame: FRAME, stage: STAGE, box: zoomed(4),
                                    dpr: 2, natural: 2560 });
  sandbox.bvRequestDetail();
  ok("a 2x screen at 4x zoom does need one", sent.length === 1);
}

// ---- never twice for the same ground --------------------------------------
{
  const { sandbox, sent, nodes } = world({ frame: FRAME, stage: STAGE,
                                           box: zoomed(2.5), dpr: 3, natural: 2560 });
  sandbox.bvRequestDetail();
  const asked = sent[0];
  // The patch comes back covering what was asked for, at the scale captured.
  sandbox.bvOnDetail({
    token: sandbox.bvFrameSeq, jpeg: "AAA",
    x: asked.x, y: asked.y, w: asked.w, h: asked.h, scale: asked.scale,
  });
  ok("the patch is painted", nodes["bv-detail"].src.startsWith("data:image/jpeg"));
  sandbox.bvRequestDetail();
  ok("asking again for covered ground costs nothing", sent.length === 1);
}
{
  const { sandbox, sent } = world({ frame: FRAME, stage: STAGE, box: zoomed(2.5),
                                    dpr: 3, natural: 2560 });
  sandbox.bvRequestDetail();
  const asked = sent[0];
  // The SERVER clamped the scale below what was asked for. Believing the
  // request instead would leave a patch claiming a sharpness it does not have,
  // and no further trip would ever be made.
  sandbox.bvOnDetail({
    token: sandbox.bvFrameSeq, jpeg: "AAA",
    x: asked.x, y: asked.y, w: asked.w, h: asked.h, scale: 1.2,
  });
  sandbox.bvRequestDetail();
  ok("a clamped capture is not treated as covering the request", sent.length === 2);
}

// ---- a patch that arrives after the page moved on -------------------------
{
  const { sandbox, sent, nodes } = world({ frame: FRAME, stage: STAGE,
                                           box: zoomed(2.5), dpr: 3, natural: 2560 });
  sandbox.bvRequestDetail();
  const asked = sent[0];
  sandbox.bvFrameSeq += 1;             // a frame arrived while it was in flight
  sandbox.bvOnDetail({
    token: asked.token, jpeg: "AAA",
    x: asked.x, y: asked.y, w: asked.w, h: asked.h, scale: asked.scale,
  });
  ok("a stale patch is DROPPED, not painted over a page that has changed",
    nodes["bv-detail"].src === "" && sandbox.bvDetail === null);
}

// ---- letting go of it -----------------------------------------------------
{
  const { sandbox, sent, nodes } = world({ frame: FRAME, stage: STAGE,
                                           box: zoomed(2.5), dpr: 3, natural: 2560 });
  sandbox.bvRequestDetail();
  const asked = sent[0];
  sandbox.bvOnDetail({
    token: sandbox.bvFrameSeq, jpeg: "AAA",
    x: asked.x, y: asked.y, w: asked.w, h: asked.h, scale: asked.scale,
  });
  sandbox.bvClearDetail();
  ok("clearing hides the layer", nodes["bv-detail-layer"].hidden === true);
  ok("and forgets what was covered", sandbox.bvDetail === null);
}
{
  // Back at fit with a patch still on screen: it is not merely useless, it is
  // covering the whole page at the wrong resolution.
  const { sandbox, nodes } = world({ frame: FRAME, stage: STAGE, box: zoomed(1),
                                     dpr: 3, natural: 2560 });
  sandbox.bvDetail = { x: 0, y: 0, w: 512, h: 780, scale: 2.5 };
  nodes["bv-detail"].src = "data:image/jpeg;base64,AAA";
  sandbox.bvRequestDetail();
  ok("zooming back out drops the patch", sandbox.bvDetail === null);
}
{
  const { sandbox, sent } = world({ frame: FRAME, stage: STAGE, box: zoomed(2.5),
                                    dpr: 3, natural: 2560, sheetHidden: true });
  sandbox.bvRequestDetail();
  ok("a closed sheet asks for nothing", sent.length === 0);
}
{
  const { sandbox, sent } = world({ frame: FRAME, stage: STAGE, box: zoomed(2.5),
                                    dpr: 3, natural: 2560, open: false });
  sandbox.bvRequestDetail();
  ok("no browser running, no request", sent.length === 0);
}

// ---- the patch sits where the page is, not where the screen is ------------
{
  const { sandbox, nodes } = world({ frame: FRAME, stage: STAGE, box: zoomed(2.5),
                                     dpr: 3, natural: 2560 });
  nodes["bv-frame"].style.transform = "translate(10px, 20px) scale(2.5)";
  sandbox.bvDetail = { x: 320, y: 480, w: 640, h: 975, scale: 2.5 };
  nodes["bv-detail"].src = "data:image/jpeg;base64,AAA";
  sandbox.bvPaintDetail();
  const s = nodes["bv-detail"].style;
  // `contain`, computed here independently of the code under test — the frame
  // is asked for at the stage's shape but rounds to whole pixels, so the two
  // aspects are close rather than equal and there is a hair of letterbox.
  const fit = Math.min(STAGE.width / FRAME.width, STAGE.height / FRAME.height);
  const originX = (STAGE.width - FRAME.width * fit) / 2;
  const originY = (STAGE.height - FRAME.height * fit) / 2;
  ok("the patch is placed in PAGE coordinates, untransformed",
    s.left === `${originX + 320 * fit}px` && s.top === `${originY + 480 * fit}px`);
  ok("and sized in them too",
    s.width === `${640 * fit}px` && s.height === `${975 * fit}px`);
  ok("the layer carries the FRAME's transform, so the two can never diverge",
    nodes["bv-detail-layer"].style.transform === "translate(10px, 20px) scale(2.5)");
  ok("and it is shown", nodes["bv-detail-layer"].hidden === false);
}

report("browser view: detail on zoom");
