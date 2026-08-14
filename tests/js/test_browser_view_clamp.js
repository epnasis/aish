// The pan clamp: an out-of-bounds pan must be unwritable, not merely unwritten.
"use strict";
const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");
const { ok, report } = checks();

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(
  surface(extract(appSource(), "// [BROWSER-VIEW-ZOOM-START]", "// [BROWSER-VIEW-ZOOM-END]")),
  sandbox
);
const clamp = sandbox.bvClamp;
const stage = { width: 400, height: 600 };

ok("scale below 1 is pulled back to 1", clamp({ scale: 0.2, x: 0, y: 0 }, stage).scale === 1);
ok("scale above the max is capped", clamp({ scale: 99, x: 0, y: 0 }, stage).scale === 4);

// At 1x the picture exactly fills the stage, so there is no slack to pan into:
// a stray drag must not be able to slide the page off screen.
const at1 = clamp({ scale: 1, x: 500, y: -500 }, stage);
ok("at 1x a pan is pinned to centre", at1.x === 0 && at1.y === 0);

// At 2x the picture is twice the stage, so half a stage of slack each way.
const at2 = clamp({ scale: 2, x: 10_000, y: 10_000 }, stage);
ok("at 2x panning stops at the picture's edge, not beyond",
  at2.x === 200 && at2.y === 300);
const at2neg = clamp({ scale: 2, x: -10_000, y: -10_000 }, stage);
ok("the opposite edge clamps symmetrically",
  at2neg.x === -200 && at2neg.y === -300);
ok("a pan inside the slack is left alone",
  clamp({ scale: 2, x: 50, y: -60 }, stage).x === 50);

report("browser view pan clamp");
