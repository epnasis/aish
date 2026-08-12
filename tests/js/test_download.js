// Node-only, dependency-free checks for [ATTACH-SAVE] — getting an attachment
// off the chat and onto the device.
//
// Looking at a file is not having it: the preview could pinch a photo and page
// through a PDF, and there was still no way to put either in Files. What is
// pinned here is mostly what "it" means and what happens when it fails:
//
//   - the FILE is saved, never what is on screen. For a PDF that is the
//     document; long-pressing a page would save that page's PNG, which is the
//     wrong object and silently so.
//   - the name comes from the CHAT, not from the response header, whose ASCII
//     fallback is a transliteration — "Zażółć.pdf" must not land as
//     "Za______.pdf".
//   - a refusal SAYS SO. A tap that quietly does nothing is indistinguishable
//     from a tap that missed.
//
// Run manually: node tests/js/test_download.js
"use strict";

const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();
const src = appSource();

function world(answer) {
  const saved = [];
  const toasts = [];
  const asked = [];
  const sandbox = {
    token: "t0ken",
    URLSearchParams,
    String,
    fetch: (url) => { asked.push(url); return Promise.resolve(answer); },
    saveBlob: (blob, name) => saved.push([blob, name]),
    showToast: (text) => toasts.push(text),
  };
  vm.createContext(sandbox);
  vm.runInContext(
    surface(extract(src, "// [ATTACH-SAVE-START]", "// [ATTACH-SAVE-END]")),
    sandbox,
  );
  return { s: sandbox, saved, toasts, asked };
}

const okResponse = (body = "BYTES") => ({
  ok: true,
  status: 200,
  headers: { get: () => 'attachment; filename="Za______.pdf"' },
  blob: () => Promise.resolve(body),
  json: () => Promise.resolve({}),
});

// ---- the URL ---------------------------------------------------------------
{
  const w = world(okResponse());
  ok("the path is asked for by path, through the token gate",
    w.s.downloadSrc("/u/uploads/guide.pdf") ===
      "/download?path=%2Fu%2Fuploads%2Fguide.pdf&token=t0ken");
}

// ---- saving ----------------------------------------------------------------
(async () => {
  {
    const w = world(okResponse());
    const done = await w.s.saveAttachment("/u/uploads/guide.pdf", "guide.pdf");
    ok("a successful save reports success", done === true);
    ok("…having asked for that file",
      w.asked[0] === "/download?path=%2Fu%2Fuploads%2Fguide.pdf&token=t0ken");
    ok("…and handed the bytes to the ONE saver the export already uses",
      w.saved.length === 1 && w.saved[0][0] === "BYTES");
    ok("…under the name the chat knows, not the header's ASCII fallback",
      w.saved[0][1] === "guide.pdf");
    ok("…quietly: the browser's own download UI is the feedback",
      w.toasts.length === 0);
  }

  {
    // A file deleted since it was sent, or one the server will not serve.
    const w = world({
      ok: false,
      status: 404,
      headers: { get: () => "" },
      json: () => Promise.resolve({ error: "not found" }),
      blob: () => Promise.resolve("nope"),
    });
    const done = await w.s.saveAttachment("/u/uploads/gone.pdf", "gone.pdf");
    ok("a refusal saves nothing", done === false && w.saved.length === 0);
    ok("…and says so, naming the file and the reason",
      w.toasts.length === 1 &&
      w.toasts[0].includes("gone.pdf") && w.toasts[0].includes("not found"));
  }

  {
    // The offline case, and the one where the tunnel is down.
    const sandbox = {
      token: "",
      URLSearchParams,
      String,
      fetch: () => Promise.reject(new Error("network")),
      saveBlob: () => { throw new Error("must not save"); },
      showToast: (text) => toasts.push(text),
    };
    const toasts = [];
    vm.createContext(sandbox);
    vm.runInContext(
      surface(extract(src, "// [ATTACH-SAVE-START]", "// [ATTACH-SAVE-END]")),
      sandbox,
    );
    ok("with no token the URL simply carries none",
      sandbox.downloadSrc("/u/uploads/a.pdf") === "/download?path=%2Fu%2Fuploads%2Fa.pdf");
    const done = await sandbox.saveAttachment("/u/uploads/a.pdf", "a.pdf");
    ok("a save that cannot reach the server fails loudly, not silently",
      done === false && toasts.length === 1 && toasts[0].includes("a.pdf"));
  }

  {
    const w = world(okResponse());
    await w.s.saveAttachment("/u/uploads/report-final.pdf");
    ok("with no name given, the file names itself from its path",
      w.saved[0][1] === "report-final.pdf");
  }

  // ---- what the preview's Save button saves ---------------------------------
  {
    // previewShow is the one writer of what is on screen, and the button is
    // part of that: it names the FILE this picture came from. A PDF's every
    // page carries the document, so Save on page 7 saves the whole thing.
    const els = {};
    const { fakeElement } = require("./harness");
    for (const id of ["preview", "preview-img", "preview-name", "preview-count",
                      "preview-status", "preview-save"]) {
      els[id] = fakeElement(id === "preview-img" ? "img" : "div");
    }
    els.preview.hidden = true;
    els["preview-img"].removeAttribute = function (attr) { delete this[attr]; };
    const sandbox = {
      $: (id) => els[id] || null,
      token: "t0ken",
      URLSearchParams,
      fetch: () => ({ then: () => ({ then: () => ({ catch() {} }) }) }),
      setTimeout: () => 1,
      clearTimeout: () => {},
      previewReset: () => {},
      previewSnapshot: () => {},
    };
    vm.createContext(sandbox);
    vm.runInContext(
      surface(extract(src, "// [PREVIEW-START]", "// [PREVIEW-END]")),
      sandbox,
    );
    sandbox.openPdfPreview("/u/uploads/guide.pdf", "guide.pdf");
    sandbox.previewAdoptPages("/u/uploads/guide.pdf", "guide.pdf", 9);
    ok("the Save button is offered for a document", els["preview-save"].hidden === false);
    ok("…naming what it would save", els["preview-save"].title === "Save guide.pdf");
    sandbox.previewShow(6);
    ok("…and on page 7 it still saves the DOCUMENT, not the page",
      sandbox.previewSaveTarget().file === "/u/uploads/guide.pdf");

    // A picture opened with no file behind it offers nothing to save, rather
    // than a button that would 404.
    sandbox.openPreview("/file?path=/tmp/x.png", "x.png");
    ok("a picture with no file behind it hides the button",
      els["preview-save"].hidden === true);
    ok("…and has nothing to hand over either", sandbox.previewSaveTarget() === null);
  }

  report("test_download.js");
})();
