// Node-only, dependency-free check for the [![alt](img)](url) form: an image
// that is also a link. Pulls the REAL IMAGE_LINK_RE / embedForLink / mapsCard /
// imageLink out of app.js by marker and runs them against a minimal fake DOM.
//
// What it pins:
//   1. the nested form parses at all (INLINE_RE used to tear it into 3 nodes)
//   2. an embeddable maps link becomes a POSTER card — image first, no iframe
//      until activation, which is what makes it survive an offline frame
//   3. activation swaps in the sandboxed iframe
//   4. a bare maps link (no poster) still loads its frame eagerly
//   5. a non-embeddable url falls back to a clickable picture, not a card
//   6. a poster on a non-whitelisted remote host never reaches an <img>
//   7. the tap is ANSWERED — the badge goes, a spinner says what is happening,
//      and the poster stays put instead of the card blanking (#217 follow-up)
//
// Run manually: node tests/js/test_image_link_embed.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "aish", "static", "app.js"),
  "utf8"
);

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert(start !== -1, `start marker not found: ${startMarker}`);
  assert(end !== -1, `end marker not found: ${endMarker}`);
  return src.slice(start, end);
}

function fakeElement(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    className: "",
    children: [],
    attrs: {},
    listeners: {},
    textContent: "",
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { c._parent = el; this.children.push(c); return c; },
    replaceChildren(...c) { this.children = c; },
    remove() {
      const parent = this._parent;
      if (parent) parent.children = parent.children.filter((c) => c !== this);
      this._parent = null;
    },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    fire(type) { (this.listeners[type] || []).forEach((fn) => fn()); },
    click() { (this.listeners.click || []).forEach((fn) => fn()); },
    classList: {
      add(...c) { el._classes.push(...c); },
      remove(c) { el._classes = el._classes.filter((x) => x !== c); },
    },
    _classes: [],
  };
  return el;
}

const box = {
  document: { createElement: fakeElement, createTextNode: (t) => ({ text: t }) },
  URLSearchParams,
  encodeURIComponent,
  token: "tok",
  replaying: false,
  offlineViewing: false,
  noteRenderError() {},
  // The acknowledgement holds for a minimum (EMBED_HANDOFF_MIN_MS), so a load
  // inside that window defers its removal instead of blinking it away. Timers
  // as data: the removal runs when this test says so.
  Date: { now: () => box._now },
  _now: 1_000_000,
  setTimeout(fn, ms) { deferred.push({ fn, ms }); return deferred.length; },
  clearTimeout() {},
};
const deferred = [];
vm.createContext(box);
// imageFetchAllowed + IMG_FETCH_HOSTS, then externalAnchor, then the image
// helpers, then the embed block including IMAGE_LINK_RE and imageLink.
vm.runInContext(
  [
    extract("function externalAnchor", "// [INLINEIMG-START]"),
    extract("const IMG_FETCH_HOSTS", "// Images (#9)"),
    extract("// The src a markdown image target", "// Quick replies (#17)"),
    extract("// [EMBED-HANDOFF-START]", "// [EMBED-HANDOFF-END]"),
    extract("// Rich embeds (#50)", "function inlineMd"),
  ].join("\n").replace(/\bconst\b/g, "var"),
  box
);

const POSTER = "/Users/x/.local/state/aish/media/abc-map.png";
const PLACE = "https://www.google.com/maps/search/?api=1&query=Warszawa";
const ROUTE = "https://maps.google.com/maps?saddr=Warszawa&daddr=Radom%20to%3AKrakow";

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (err) {
    console.log(`FAIL ${name}\n     ${err.message}`);
    failures++;
  }
}

const iframeOf = (el) => el.children.find((c) => c.tagName === "IFRAME");
const imgOf = (el) => el.children.find((c) => c.tagName === "IMG");

check("the nested form parses as one token", () => {
  const m = `see [![A map](${POSTER})](${PLACE}) here`.match(box.IMAGE_LINK_RE);
  assert(m, "IMAGE_LINK_RE did not match");
  assert.strictEqual(m[1], "A map");
  assert.strictEqual(m[2], POSTER);
  assert.strictEqual(m[3], PLACE);
});

check("a poster card shows the image and no frame until tapped", () => {
  const card = box.imageLink("A map", POSTER, PLACE);
  assert(card._classes.includes("embed-poster"), "not a poster card");
  assert.strictEqual(card.getAttribute("role"), "button");
  assert(!iframeOf(card), "the frame loaded eagerly — offline would show a black box");
  const img = imgOf(card);
  assert(img, "no poster image");
  assert(img.src.startsWith("/file?"), `poster not routed through /file: ${img.src}`);
  assert(img.src.includes("token=tok"), "poster src is missing the access token");
});

check("tapping swaps in the sandboxed live map", () => {
  const card = box.imageLink("A map", POSTER, ROUTE);
  card.click();
  const frame = iframeOf(card);
  assert(imgOf(card), "the poster vanished, leaving an empty box while Google answers");
  const wait = card.children.find((c) => c.className === "embed-loading");
  assert(wait, "the tap produced nothing visible — it reads as not having registered");
  assert(!card.children.find((c) => c.className === "embed-play embed-open"),
         "the open badge is still up while the map loads");
  frame.fire("load");
  deferred.forEach((t) => t.fn()); // the floor's deferred removal
  assert(!card.children.find((c) => c.className === "embed-loading"),
         "the spinner is still over a loaded map");
  assert(frame, "no iframe after activation");
  assert(frame.src.includes("saddr=Warszawa"), `wrong src: ${frame.src}`);
  assert(frame.src.includes("daddr=Radom%20to%3AKrakow"), "waypoint chain lost");
  assert(frame.src.endsWith("&output=embed"), "not the embed form");
  assert(
    frame.getAttribute("sandbox").startsWith("allow-scripts allow-same-origin"),
    "iframe is not sandboxed as before"
  );
  assert(!card.getAttribute("role"), "still announced as a button after activation");
});

check("a bare maps link (no poster) still loads eagerly", () => {
  const card = box.embedForLink("A map", PLACE);
  assert(card, "bare maps link stopped embedding");
  assert(iframeOf(card), "no iframe — the pre-poster behaviour regressed");
  assert(!card._classes.includes("embed-poster"), "unexpected poster card");
});

check("a non-embeddable url falls back to a clickable picture", () => {
  const el = box.imageLink("Shot", POSTER, "https://example.com/page");
  assert.strictEqual(el.tagName, "A");
  assert.strictEqual(el.href, "https://example.com/page");
  assert.strictEqual(el.target, "_blank");
  assert(imgOf(el), "no image inside the link");
});

check("a poster on a non-whitelisted host never becomes an <img>", () => {
  const el = box.imageLink("Evil", "https://attacker.example/p.png", PLACE);
  assert.strictEqual(el.tagName, "A", "a blocked poster produced a card");
  assert(!imgOf(el), "blocked host still reached an <img> — zero-click fetch");
  assert.strictEqual(el.textContent, "Evil");
});

process.exit(failures ? 1 : 0);
