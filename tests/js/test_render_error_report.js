// Node-only, dependency-free check for #188 layer 2: the browser reporting
// content it could not render. Pulls the real noteRenderError/flushRenderErrors
// out of app.js by the [RENDERERR] markers.
//
// The properties that matter are about restraint, not about sending. One answer
// with four dead pictures must be ONE report; the access token must never ride
// along; an offline transcript must not blame the model for the network; and a
// dead socket must not queue a diagnostic that arrives after the conversation
// has moved on.
//
// Run manually: node tests/js/test_render_error_report.js
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

// A hostile-ish world: timers are data the test fires by hand, the socket
// records instead of sending, and both the page-state flags start false.
function world({ readyState = 1, offlineViewing = false } = {}) {
  const sent = [];
  const timers = [];
  const sandbox = {
    offlineViewing,
    ws: readyState === null ? null : { readyState, send: (text) => sent.push(JSON.parse(text)) },
    WebSocket: { OPEN: 1, CLOSED: 3, CLOSING: 2, CONNECTING: 0 },
    setTimeout: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimeout: (id) => {
      if (id) timers[id - 1] = null;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    extract("// [RENDERERR-START]", "// [RENDERERR-END]").replace(/\bconst\b/g, "var"),
    sandbox,
  );
  return {
    sent,
    fire: () => {
      const live = timers.filter(Boolean);
      timers.length = 0;
      for (const t of live) t.fn();
    },
    pending: () => timers.filter(Boolean).length,
    note: (target, isLive) => sandbox.noteRenderError(target, isLive),
    sandbox,
  };
}

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures++;
    console.error(`FAIL - ${name}`);
    console.error(`       ${err.message}`);
  }
}

check("four broken pictures in one answer are ONE report", () => {
  const w = world();
  for (const n of [1, 2, 3, 4]) w.note(`/media/pic${n}.jpg`, true);
  assert.strictEqual(w.pending(), 1, "each failure must reset one shared timer");
  w.fire();
  assert.strictEqual(w.sent.length, 1);
  assert.deepStrictEqual(w.sent[0].items, [
    "/media/pic1.jpg", "/media/pic2.jpg", "/media/pic3.jpg", "/media/pic4.jpg",
  ]);
  assert.strictEqual(w.sent[0].type, "render_error");
  assert.strictEqual(w.sent[0].live, true);
});

check("the same target reported twice is named once", () => {
  const w = world();
  w.note("/media/a.jpg", true);
  w.note("/media/a.jpg", true);
  w.fire();
  assert.deepStrictEqual(w.sent[0].items, ["/media/a.jpg"]);
});

// #201: a replayed failure is not an event. Re-reading a chat used to re-report
// its dead images, and each report was a log write that read as fresh activity —
// the chat came back unread the moment you left it, and no amount of reading
// could clear it. Only the turn that WROTE the image can act on the failure.
check("a replayed failure is not reported at all", () => {
  const cold = world();
  cold.note("/media/a.jpg", false);
  assert.strictEqual(cold.pending(), 0, "a replayed failure must not even arm the timer");
  cold.fire();
  assert.strictEqual(cold.sent.length, 0, "a replay must send nothing");
});

check("a live failure alongside replayed ones reports only itself", () => {
  const w = world();
  w.note("/media/a.jpg", false);
  w.note("/media/b.jpg", true);
  w.note("/media/c.jpg", false);
  w.fire();
  assert.strictEqual(w.sent.length, 1);
  assert.deepStrictEqual(w.sent[0].items, ["/media/b.jpg"]);
  assert.strictEqual(w.sent[0].live, true);
});

check("the batch is bounded — a broken transcript cannot flood the socket", () => {
  const w = world();
  for (let i = 0; i < 200; i += 1) w.note(`/media/p${i}.jpg`, true);
  w.fire();
  assert(w.sent[0].items.length <= 6, `batch was ${w.sent[0].items.length}`);
});

check("offline viewing reports nothing (the mirror lacks images by design)", () => {
  const w = world({ offlineViewing: true });
  w.note("/media/a.jpg", true);
  assert.strictEqual(w.pending(), 0, "no timer should even be armed");
  w.fire();
  assert.strictEqual(w.sent.length, 0);
});

check("a dead socket drops the report instead of queueing it", () => {
  for (const readyState of [0, 2, 3]) {
    const w = world({ readyState });
    w.note("/media/a.jpg", true);
    w.fire();
    assert.strictEqual(w.sent.length, 0, `readyState ${readyState} must not send`);
  }
  const none = world({ readyState: null });
  none.note("/media/a.jpg", true);
  none.fire();
  assert.strictEqual(none.sent.length, 0, "a null socket must not throw or send");
});

check("the buffer is cleared by a flush, so the next answer starts fresh", () => {
  const w = world();
  w.note("/media/a.jpg", true);
  w.fire();
  w.note("/media/b.jpg", true);
  w.fire();
  assert.strictEqual(w.sent.length, 2);
  assert.deepStrictEqual(w.sent[1].items, ["/media/b.jpg"]);
});

check("a flush with nothing buffered sends nothing", () => {
  const w = world();
  w.sandbox.flushRenderErrors();
  assert.strictEqual(w.sent.length, 0);
});

// Both checks below read the WHOLE image-render path (imageSrc + markdownImg +
// inlineImage + imageLink), not one function. It was one function until the
// poster-backed map embed gave it a second entry point; scoping the invariant
// to the path rather than to a single body is what keeps it true as the path
// grows another caller.
const IMAGE_PATH = src.slice(
  src.indexOf("// The src a markdown image target"),
  src.indexOf("// Quick replies (#17)")
) + src.slice(src.indexOf("function imageLink("), src.indexOf("function inlineMd"));

check("what is reported is the model's own target, never the tokened /file URL", () => {
  // The renderer rewrites a local path to /file?path=…&token=…; reporting THAT
  // would write the server's access token into the session log.
  const calls = IMAGE_PATH.match(/noteRenderError\(([^)]*)\)/g) || [];
  assert(calls.length >= 2, `expected both failure paths to report, got ${calls.length}`);
  for (const call of calls) {
    assert(/noteRenderError\(target,\s*live\)/.test(call), `reports the wrong value: ${call}`);
  }
  assert(!/noteRenderError\(\s*src/.test(IMAGE_PATH), "must never report the rewritten src");
});

check("liveness is captured at render time, not read inside onerror", () => {
  // onerror fires long after the render; by then `replaying` says nothing about
  // where this particular image came from. Every entry point into the path must
  // therefore capture it up front and hand it to the handler as a value.
  const onerror = IMAGE_PATH.indexOf("img.onerror");
  assert(onerror !== -1, "the image path no longer installs an onerror handler");
  const handler = IMAGE_PATH.slice(onerror, IMAGE_PATH.indexOf("img.src =", onerror));
  assert(
    !/\breplaying\b|\bofflineViewing\b/.test(handler),
    "onerror must not read `replaying`/`offlineViewing` itself"
  );
  const captured = IMAGE_PATH.match(/const live = !replaying && !offlineViewing;/g) || [];
  assert(
    captured.length >= 2,
    `every entry point must capture liveness at render time, found ${captured.length}`
  );
});

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
