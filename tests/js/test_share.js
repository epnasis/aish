// Node-only, dependency-free checks for [ATTACH-SHARE] — handing an attachment
// to the share sheet instead of only into Files.
//
// Save was the whole answer before this, and on a phone it is the wrong one
// most of the time: what you want after reading a document is to send it on,
// and Files is a detour through a second app that then has to find the file
// again. What is pinned here is what the button is allowed to promise:
//
//   - it is DRAWN only where files can really be shared. `navigator.share`
//     exists on every desktop browser and shares text and URLs; a sheet that
//     opens with no file in it is worse than no button.
//   - the sheet gets the FILE — the chat's name, the response's type — because
//     the receiving app picks itself from both.
//   - dismissing the sheet is a decision and says nothing; every other failure
//     says what happened, and the one iOS timing failure says what to do.
//   - a failed fetch is not remembered: the next tap asks again.
//
// Run manually: node tests/js/test_share.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();

// Let every pending microtask (the fetch → blob → File chain) settle, the way
// the seconds between a finger landing and lifting would.
const settle = () => new Promise((resolve) => setImmediate(resolve));
const src = appSource();

class FakeFile {
  constructor(parts, name, options) {
    this.parts = parts;
    this.name = name;
    this.type = (options && options.type) || "";
  }
}

const okResponse = (body = "BYTES", type = "application/pdf") => ({
  ok: true,
  status: 200,
  blob: () => Promise.resolve({ body, type }),
  json: () => Promise.resolve({}),
});

// A world with a browser in it: `nav` is what navigator answers, `answers` is
// the queue of fetch results (one per call, the last one repeating).
function world({ nav = {}, answers = [okResponse()] } = {}) {
  const asked = [];
  const toasts = [];
  const shared = [];
  const canShareCalls = [];
  let call = 0;
  const navigator = {
    canShare: nav.canShare === undefined
      ? (data) => { canShareCalls.push(data); return true; }
      : nav.canShare && ((data) => { canShareCalls.push(data); return nav.canShare(data); }),
    share: nav.share === undefined
      ? (data) => { shared.push(data); return Promise.resolve(); }
      : nav.share && ((data) => { shared.push(data); return nav.share(data); }),
  };
  const sandbox = {
    navigator,
    File: FakeFile,
    String,
    Promise,
    downloadSrc: (path) => `/download?path=${path}&token=t0ken`,
    showToast: (text) => toasts.push(text),
    fetch: (url) => {
      asked.push(url);
      const answer = answers[Math.min(call++, answers.length - 1)];
      return typeof answer === "function" ? answer() : Promise.resolve(answer);
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(src, "// [ATTACH-SHARE-START]", "// [ATTACH-SHARE-END]")),
    sandbox,
  );
  return { s: sandbox, asked, toasts, shared, canShareCalls };
}

// ---- is the button drawn at all -------------------------------------------
{
  const w = world();
  ok("a browser that can share files says so", w.s.canShareFiles() === true);
  ok("…having probed with a real File, not with a hopeful guess",
    w.canShareCalls[0].files[0] instanceof FakeFile);
  w.s.canShareFiles();
  ok("…and the probe is answered once, not per picture", w.canShareCalls.length === 1);
}
{
  // Every desktop browser has navigator.share for text and URLs. Drawing the
  // button there opens a sheet with nothing in it.
  const w = world({ nav: { canShare: null } });
  ok("share without canShare is text sharing, and does not count",
    w.s.canShareFiles() === false);
}
{
  const w = world({ nav: { canShare: () => false } });
  ok("a browser that shares no files at all hides the button",
    w.s.canShareFiles() === false);
}
{
  const w = world({ nav: { share: null } });
  ok("canShare without share is not a share sheet either",
    w.s.canShareFiles() === false);
}

(async () => {
  // ---- what the sheet is handed --------------------------------------------
  {
    const w = world();
    const done = await w.s.shareAttachment("/u/uploads/guide.pdf", "guide.pdf");
    ok("a share reports success", done === true);
    ok("…having fetched the file through the same download gate",
      w.asked.length === 1 && w.asked[0] === "/download?path=/u/uploads/guide.pdf&token=t0ken");
    const file = w.shared[0].files[0];
    ok("…handed the sheet a File, not a URL", file instanceof FakeFile);
    ok("…under the name the chat knows", file.name === "guide.pdf");
    ok("…typed from the response, since the receiving app picks itself from it",
      file.type === "application/pdf");
    ok("…and said nothing: the sheet IS the feedback", w.toasts.length === 0);
  }

  {
    const w = world();
    await w.s.shareAttachment("/u/uploads/report-final.pdf");
    ok("with no name given, the file names itself from its path",
      w.shared[0].files[0].name === "report-final.pdf");
  }

  {
    const w = world({ answers: [okResponse("BYTES", "")] });
    await w.s.shareAttachment("/u/uploads/notes.bin", "notes.bin");
    ok("a typeless response still shares, as bytes",
      w.shared[0].files[0].type === "application/octet-stream");
  }

  // ---- the press that starts the download ----------------------------------
  {
    const w = world();
    w.s.primeShare("/u/uploads/guide.pdf", "guide.pdf");
    await settle();
    const done = await w.s.shareAttachment("/u/uploads/guide.pdf", "guide.pdf");
    ok("the press primes and the release shares the SAME fetch, not a second one",
      done === true && w.asked.length === 1);
  }

  // ---- when it does not work ------------------------------------------------
  {
    const w = world({ nav: { share: () => Promise.reject({ name: "AbortError" }) } });
    const done = await w.s.shareAttachment("/u/uploads/guide.pdf", "guide.pdf");
    ok("dismissing the sheet is a decision, not a failure",
      done === false && w.toasts.length === 0);
  }

  {
    // iOS refuses a sheet the tap no longer covers — which happens on exactly
    // the tap that had to wait for the bytes.
    let attempt = 0;
    const w = world({
      nav: {
        share: () => (attempt++ === 0
          ? Promise.reject({ name: "NotAllowedError" })
          : Promise.resolve()),
      },
    });
    const first = await w.s.shareAttachment("/u/uploads/guide.pdf", "guide.pdf");
    ok("a refused sheet does not pretend to have shared", first === false);
    ok("…and asks for the tap that will work, naming the file",
      w.toasts.length === 1 &&
      w.toasts[0].includes("guide.pdf") && w.toasts[0].includes("tap share again"));
    const second = await w.s.shareAttachment("/u/uploads/guide.pdf", "guide.pdf");
    ok("…which then shares from the bytes already here", second === true);
    ok("…without asking the server twice", w.asked.length === 1);
  }

  {
    // The same refusal on a tap that had nothing to wait for is a real failure
    // and must not send the owner round a loop that already happened.
    const w = world({ nav: { share: () => Promise.reject({ name: "NotAllowedError" }) } });
    w.s.primeShare("/u/uploads/guide.pdf", "guide.pdf");
    await settle();
    await w.s.shareAttachment("/u/uploads/guide.pdf", "guide.pdf");
    ok("a refusal with the file already in hand reports a failure, not a retry",
      w.toasts.length === 1 && !w.toasts[0].includes("tap share again"));
  }

  {
    const w = world({ nav: { canShare: (data) => !data.files[0].name.endsWith(".pdf") } });
    const done = await w.s.shareAttachment("/u/uploads/guide.pdf", "guide.pdf");
    ok("a file this browser will not share says so, rather than opening nothing",
      done === false && w.shared.length === 0);
    ok("…and points at the thing that does work",
      w.toasts.length === 1 && w.toasts[0].includes("Save"));
  }

  {
    const w = world({
      answers: [
        { ok: false, status: 404, json: () => Promise.resolve({ error: "not found" }) },
        okResponse(),
      ],
    });
    const done = await w.s.shareAttachment("/u/uploads/gone.pdf", "gone.pdf");
    ok("a file the server will not serve fails loudly",
      done === false && w.shared.length === 0 &&
      w.toasts[0].includes("gone.pdf") && w.toasts[0].includes("not found"));
    // A rejection must not be cached: the server was unreachable a second ago.
    const again = await w.s.shareAttachment("/u/uploads/gone.pdf", "gone.pdf");
    ok("…and the next tap asks again rather than replaying the failure",
      again === true && w.asked.length === 2);
  }

  {
    const w = world({ answers: [() => Promise.reject(new Error("network"))] });
    const done = await w.s.shareAttachment("/u/uploads/a.pdf", "a.pdf");
    ok("a share that cannot reach the server names the file and the reason",
      done === false && w.toasts.length === 1 &&
      w.toasts[0].includes("a.pdf") && w.toasts[0].includes("network"));
  }

  {
    // The press fires with no one awaiting it: a failure there must be handled
    // where it happens, or Node/the browser reports an unhandled rejection.
    const w = world({ answers: [() => Promise.reject(new Error("network"))] });
    process.once("unhandledRejection", () => {
      ok("priming a file that cannot be fetched stays silent", false);
    });
    w.s.primeShare("/u/uploads/a.pdf", "a.pdf");
    await settle();
    ok("priming a file that cannot be fetched stays silent", w.toasts.length === 0);
  }

  report("test_share.js");
})();
