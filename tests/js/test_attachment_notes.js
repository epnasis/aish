// Node-only, dependency-free checks for [ATTACHMENT-NOTES].
//
// The defect: send a photo and your own message bubble read
//
//   what is this?
//   [image attached: cat.png — you can see it; file at /Users/…/state/uploads/cat.png]
//
// That second line is written FOR THE MODEL — it is how a backend without
// native vision is told a file exists — and it was rendered verbatim to the
// person who had just tapped ＋ and picked a photo they can obviously see. A
// photo sent with no words was worse: the note became the whole bubble AND the
// chat's title, absolute path included.
//
// The format is a cross-language contract with no shared code to enforce it:
// server.py writes these strings, this file parses them. The Python half is
// pinned in tests/test_server.py::TestAttachmentNoteFormat against the SAME
// literals — change the note in one language and both fail.
//
// Run manually: node tests/js/test_attachment_notes.js
"use strict";

const assert = require("assert");
const vm = require("vm");
const { appSource, extract, surface, checks } = require("./harness");

const { ok, report } = checks();

// Objects minted inside the vm carry a different prototype, so compare the
// DATA rather than the identity.
const same = (actual, expected, what) =>
  assert.deepStrictEqual(JSON.parse(JSON.stringify(actual)), expected, what);

// The exact strings server.py builds (aish/server.py::_classify_attachments).
const NATIVE_IMAGE = (name, path) =>
  `[image attached: ${name} — you can see it; file at ${path}]`;
const NATIVE_DOC = (name, path) =>
  `[document attached: ${name} — you can read it; file at ${path}]`;
const PLAIN = (path) => `[attached file: ${path}]`;

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(
  surface(extract(appSource(), "// [ATTACHMENT-NOTES-START]", "// [ATTACHMENT-NOTES-END]")),
  sandbox,
);
const { splitAttachmentNotes, stripAttachmentNotes } = sandbox;

// The whole point: the note leaves the bubble, and what it described survives
// as structure rather than as prose.
{
  const path = "/Users/me/.local/state/aish/uploads/cat.png";
  const text = `what is this?\n\n${NATIVE_IMAGE("cat.png", path)}`;
  const split = splitAttachmentNotes(text);
  ok("the bubble shows what was typed, nothing else", split.body === "what is this?");
  same(split.attachments, [
    { kind: "image", name: "cat.png", path },
  ]);
  ok("…and the attachment is recovered as data, not left as a sentence", true);
}

// A document note carries the same three facts under a different verb.
{
  const path = "/Users/me/uploads/paper.pdf";
  const split = splitAttachmentNotes(`read this\n\n${NATIVE_DOC("paper.pdf", path)}`);
  ok("a document note parses too", split.body === "read this");
  same(split.attachments, [
    { kind: "document", name: "paper.pdf", path },
  ]);
}

// The fallback note (unsupported type, oversized, or outside uploads) has only
// a path — the name has to come from it, or the chip would show the whole path.
{
  const split = splitAttachmentNotes(`look\n\n${PLAIN("/tmp/archive.zip")}`);
  same(split.attachments, [
    { kind: "file", name: "archive.zip", path: "/tmp/archive.zip" },
  ]);
  ok("a path-only note is still named by its basename", true);
}

// The case that titled a chat with a machine note: a photo and no words.
{
  const path = "/Users/me/uploads/IMG_4021.jpg";
  const split = splitAttachmentNotes(NATIVE_IMAGE("IMG_4021.jpg", path));
  ok("an attachment-only turn leaves an EMPTY body", split.body === "");
  ok("…with the photo still accounted for", split.attachments.length === 1);
}

// Several files in one turn, in the order they were attached.
{
  const text = [
    "compare these",
    "",
    NATIVE_IMAGE("a.png", "/u/a.png"),
    NATIVE_IMAGE("b.png", "/u/b.png"),
  ].join("\n");
  const split = splitAttachmentNotes(text);
  ok("every attachment is kept, in order",
    split.attachments.map((a) => a.name).join(",") === "a.png,b.png");
  ok("…and the body is untouched by how many there were", split.body === "compare these");
}

// A path containing the very characters the note format uses must not truncate
// the parse — an em dash in a filename is legal, and "]" ends the note.
{
  const path = "/u/holiday — 2026 [best].png";
  const split = splitAttachmentNotes(NATIVE_IMAGE("holiday — 2026 [best].png", path));
  ok("a filename containing an em dash and brackets round-trips",
    split.attachments[0].path === path);
}

// Text that merely LOOKS like a note is the user's own words. Being wrong in
// this direction deletes something they typed, which is the worse failure.
{
  const notes = [
    "[image attached: cat.png]",                       // no file clause
    "[attached file:]",                                // empty path
    "look at [attached file: /u/x.png] please",        // mid-line, not a note
    "[image attached: cat.png — you can smell it; file at /u/x]", // wrong verb
  ];
  for (const line of notes) {
    const split = splitAttachmentNotes(line);
    ok(`kept as text: ${line}`, split.body === line.trim() && !split.attachments.length);
  }
}

// stripAttachmentNotes is what copy/reuse/history/the title all call. It must
// stay exactly "the body", or the two derivations drift apart.
{
  const text = `ship it\n\n${NATIVE_IMAGE("x.png", "/u/x.png")}`;
  ok("strip is the split's body and nothing else",
    stripAttachmentNotes(text) === splitAttachmentNotes(text).body);
  ok("stripping is idempotent (it runs on already-stripped text)",
    stripAttachmentNotes(stripAttachmentNotes(text)) === "ship it");
  ok("null-ish text never throws", stripAttachmentNotes(undefined) === "");
}

report("test_attachment_notes.js");
