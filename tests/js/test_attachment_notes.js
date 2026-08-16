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
// Since #231 there are TWO shapes, and both are checked here.
//
// The RECORD form `![[cat.png]]` is what a message holds now — wiki-link style,
// as in Obsidian, name only for a file aish keeps. It is what the log stores,
// what the bubble renders as a thumbnail, and what copy writes back out.
//
// The GUIDANCE form is the bracketed prose above. Nothing writes it to disk any
// more — it is built fresh each time a message is handed to a model, because
// what a model may do with a file depends on which model it is — but every
// message written before #231 IS in that shape and always will be, since a chat
// log is never rewritten. So it must keep parsing forever.
//
// Both are cross-language contracts with no shared code to enforce them. The
// Python half is pinned in tests/test_server.py::TestAttachmentNoteFormat
// against the SAME literals — change either in one language and both fail.
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

const UPLOADS = "/Users/me/.local/state/aish/uploads";

// The block reads two globals it does not own: where aish keeps its files (sent
// once in the hello) and which suffixes are pictures.
const sandbox = { uploadsDir: UPLOADS, ATTACH_IMAGE_RE: /\.(png|jpe?g|gif|webp)$/i };
vm.createContext(sandbox);
vm.runInContext(
  surface(extract(appSource(), "// [ATTACHMENT-NOTES-START]", "// [ATTACHMENT-NOTES-END]")),
  sandbox,
);
const { splitAttachmentNotes, stripAttachmentNotes, recordSource, messageParts, messageBody } = sandbox;

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

// ---- the record form: what a message actually holds now (#231) -------------

// A name with no path: the file is one aish keeps, and the uploads folder is
// the only place it can be, so the name IS the address.
{
  const split = splitAttachmentNotes("what is this?\n\n![[cat.png]]");
  ok("the bubble shows what was typed", split.body === "what is this?");
  same(split.attachments, [
    { kind: "image", name: "cat.png", path: `${UPLOADS}/cat.png`, ref: "cat.png" },
  ]);
  ok("…and the bare name resolved to something /file can serve", true);
}

// A full path: the file lives somewhere aish does not manage, and nothing could
// derive the location back from a name.
{
  const split = splitAttachmentNotes("look\n\n![[/tmp/archive.zip]]");
  same(split.attachments, [
    { kind: "file", name: "archive.zip", path: "/tmp/archive.zip", ref: "/tmp/archive.zip" },
  ]);
}

// The kind comes from the NAME, because an embed does not record one — what a
// model could do with a file is not a property of the file.
{
  const pdf = splitAttachmentNotes("![[paper.pdf]]");
  ok("a pdf reads as a document", pdf.attachments[0].kind === "document");
  const other = splitAttachmentNotes("![[notes.txt]]");
  ok("anything else is a plain file", other.attachments[0].kind === "file");
}

// An attachment-only message: the photo IS the message.
{
  const split = splitAttachmentNotes("![[IMG_4021.jpg]]");
  ok("an embed-only turn leaves an EMPTY body", split.body === "");
  ok("…with the photo still accounted for", split.attachments.length === 1);
}

// Text that merely LOOKS like an embed is the owner's own words — the same
// conservatism the prose forms have. Being wrong here deletes what they wrote.
{
  const typed = [
    "![[]]",           // empty target
    "[[cat.png]]",     // a link, not an embed
    "![[a\nb]]",       // a newline can never be inside one
  ];
  for (const line of typed) {
    const split = splitAttachmentNotes(line);
    ok(`kept as text: ${JSON.stringify(line)}`,
      split.body === line.trim() && !split.attachments.length);
  }
}

// ---- a file INSIDE a sentence (#233) ---------------------------------------

// The parse is a SEQUENCE, so the bubble can draw the file where it was
// written. That position is the only thing saying which file goes with which
// clause, and the model gets it no other way.
{
  const parts = messageParts("the error in ![[shot.png]], fix like ![[patch.txt]]");
  same(parts.map((p) => (p.type === "text" ? ["text", p.text] : ["file", p.note.name, p.block])), [
    ["text", "the error in "],
    ["file", "shot.png", false],
    ["text", ", fix like "],
    ["file", "patch.txt", false],
  ]);
  ok("both files are found inside the sentence", true);
}

// A file ALONE on its line is the block case — an attached photo — and is what
// an ordinary ＋ attachment still looks like.
{
  const parts = messageParts("look at this\n\n![[cat.png]]");
  ok("the trailing file is a block", parts[1].type === "file" && parts[1].block === true);
}

// ---- what "inside a sentence" actually means (#234) -------------------------

// THE REPORTED BUBBLE. A message that merely ENDS with a picture is one with an
// attachment, whatever line the reference sits on — and it gets the thumbnail
// an attachment has always had. This one was sent BEFORE the composer stopped
// writing references onto the same line, and a log is never rewritten, so
// deciding it here is the only thing that repairs it.
{
  const parts = messageParts("The url from notification does not open ![[shot.png]]");
  const file = parts.find((p) => p.type === "file");
  ok("a picture at the end of a message is an attachment", file.block === true);
  ok("…and the words before it are still there",
    parts[0].type === "text" && /does not open/.test(parts[0].text));
}

// Words AFTER it are what make it inline: that is a file in a sentence.
{
  const parts = messageParts("the error in ![[shot.png]] is here");
  ok("a picture with words after it stays inline",
    parts.find((p) => p.type === "file").block === false);
}

// A message WRITTEN in the inline style keeps its trailing picture inline too.
// "compare A with B" is one thought; showing B at four times the size of A
// would read as two different kinds of thing.
{
  const files = messageParts("compare ![[a.png]] with ![[b.png]]")
    .filter((p) => p.type === "file");
  ok("siblings in one sentence are sized alike",
    files.length === 2 && files.every((f) => f.block === false));
}

// Two attached photos and no sentence: both are attachments, as always.
{
  const files = messageParts("![[a.png]]\n![[b.png]]").filter((p) => p.type === "file");
  ok("plain attachments stay blocks", files.every((f) => f.block === true));
}

// Two derivations, and the difference is the audience. `messageBody` preserves
// the message (reuse re-sends it); `stripAttachmentNotes` is for reading (a
// title must not show notation, and must not have its subject cut out either).
{
  const text = "the error in ![[shot.png]] is here";
  ok("the body keeps the reference verbatim", messageBody(text) === text);
  ok("the display reads it as the file name",
    stripAttachmentNotes(text) === "the error in shot.png is here");
}

// Prose about wiki-links keeps its words. WHICH references are real is the
// server's answer — it can see the disk and the browser cannot — and it rides
// on the event as `files`. Given that list, a reference naming nothing stays
// exactly the words it is.
{
  const typed = "in Obsidian you write ![[note]] inline";
  const real = [];                       // the server found no such file
  ok("the words survive intact", messageBody(typed, real) === typed);
  same(messageParts(typed, real), [{ type: "text", text: typed }]);
  ok("…and nothing is drawn as an attachment", true);
}

// A whole LINE naming nothing is words too — dropping it would silently lose a
// line from a copied message.
{
  const typed = "![[note]]";
  ok("a lone unreal reference is kept", messageBody(typed, []) === typed);
}

// With a list present, the real ones are still files and the rest are not.
{
  const text = "compare ![[a.png]] with ![[nope]]";
  const real = [{ name: "a.png", path: `${UPLOADS}/a.png` }];
  const parts = messageParts(text, real);
  same(parts.map((p) => (p.type === "text" ? ["text", p.text] : ["file", p.note.name])), [
    ["text", "compare "],
    ["file", "a.png"],
    ["text", " with ![[nope]]"],
  ]);
}

// No list at all — an older server, or a mirror written before this — takes
// every reference at face value. That is the behaviour from before, never worse.
{
  const parts = messageParts("see ![[a.png]]", undefined);
  ok("a missing list changes nothing", parts.some((p) => p.type === "file"));
}

// Copying a message whose photo sits inside a sentence must not hand it back
// twice — once in place and once appended.
{
  const text = "the error in ![[shot.png]] is here";
  const split = splitAttachmentNotes(text);
  ok("an inline file is not appended again",
    recordSource(messageBody(text), split.attachments) === text);
}

// …while a file that had no place in the text still gets appended.
{
  const text = "look at this";
  const notes = [{ kind: "image", name: "cat.png", path: `${UPLOADS}/cat.png` }];
  ok("a trailing file is appended",
    recordSource(text, notes) === "look at this\n\n![[cat.png]]");
}

// ---- copy writes the SOURCE back out ---------------------------------------

// The round trip that makes copy/paste mean something: a message copied out
// carries its pictures, and pastes back in the shape it left.
{
  const text = "compare these\n\n![[a.png]]\n![[/tmp/b.png]]";
  const split = splitAttachmentNotes(text);
  ok("copy round-trips a message unchanged",
    recordSource(split.body, split.attachments) === text);
}

// …including one read from an OLD message. One format leaves this app, whatever
// shape it was read in.
{
  const old = `look\n\n${NATIVE_IMAGE("cat.png", `${UPLOADS}/cat.png`)}`;
  const split = splitAttachmentNotes(old);
  ok("prose in, embed out",
    recordSource(split.body, split.attachments) === "look\n\n![[cat.png]]");
}

// A message with nothing attached copies as itself — no stray blank lines.
{
  ok("plain text is untouched", recordSource("just words", []) === "just words");
}

report("test_attachment_notes.js");
