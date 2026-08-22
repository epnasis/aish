"""Append-only JSONL session logs: the conversation (for --resume) and an
audit trail of every command decision, in one file per session."""

import datetime
import difflib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple, TextIO

TITLE_MAX = 60
SNIPPET_MAX = 90  # preview line under the title in the web sessions drawer
# Characters of run-up before the hit in a search excerpt. Deliberately short:
# a rail row is one truncated line, so a long run-up spends the entire visible
# width on context and the word you typed never makes it onto the screen (#266).
SNIPPET_LEAD = 15
# What separates one turn from the next in the searchable text. Not a space: a
# phrase query would otherwise match across the boundary between what was asked
# and what was answered — words nobody said in that order — and an excerpt
# quoting the match would read as one sentence spoken by nobody.
TURN_SEP = " · "
FUZZY_THRESHOLD = 0.55  # whole query vs whole title
FUZZY_WORD_CUTOFF = 0.75  # single query word vs single session word
# A typo keeps a word roughly its own length, so a candidate that is much
# shorter is a DIFFERENT word, not a misspelling of this one. Without this,
# 0.75 is a length artifact: "tel" scores 0.75 against "tefal" (2*3/8) and so
# does "tea", which is how one five-letter query reached most of the archive.
FUZZY_LEN_SLACK = 1
CLOSEST_MAX = 10  # rows the "nothing matched — here are the closest" fallback shows
_PUNCT = ".,;:!?()[]{}<>'\"`"
# DOTALL: a user-direct command can be multi-line (e.g. `gh issue create` with a
# multi-line --body). Without it the capture stops at the first newline, the
# annotation isn't recognized on replay, and it rehydrates as a plain blue user
# bubble instead of its terminal block (#154).
_BANG_RE = re.compile(r"^\[I ran `(.+?)` myself", re.DOTALL)

# Synthetic user turns (#171). aish writes text into the conversation as
# role:"user" that the human never typed — the model must read it as the turn's
# input, so it is logged exactly like a real message and the text itself is the
# only thing a cold replay can classify it by. Every producer builds its note
# from (or is pinned by test to) one of these markers, so the live UI and
# reconstruct_events agree by construction — and logs written before #171
# classify correctly too.
RESUME_MARKER = "[automatic resume]"  # server.RESUME_NOTE — a real turn, system-styled
# Notes appended around a turn that start no task at all: the internal nudges
# (loop/stall/step-limit), the /cd and /add-dir announcements (the live UI shows
# those as workspace markers), and console text shared into context (#148).
# Live they never reach the transcript, so a replay must not invent a bubble —
# and one landing mid-turn would also split that turn in two.
# Public because a producer must be able to BUILD a note that classifies here,
# not just be recognized by luck — agent.py's media deliveries (#215) open with
# it, and the trim that later drops their pixels identifies them by it too.
NOTE_MARKER = "[aish: "
_NOTE_MARKERS = (
    NOTE_MARKER,  # agent.LOOP_WARNING / STEP_LIMIT_NOTE / LOOP_STOP_NOTE / STALL_NOTE
    "[I moved the session to ",  # Agent.rebase announce (/cd)
    "[I added ",  # Agent.add_root announce (/add-dir)
    "[Shared from my interactive terminal:]",  # console share
)


def record_epoch(record: dict) -> int | None:
    """A log record's own timestamp as epoch SECONDS, for the `at` a replayed
    transcript renders (#200).

    Every record has carried an ISO stamp since the first version of this file,
    so a chat written years ago gets its timestamps back with no migration —
    which is what made this cheap. Sent as epoch rather than the ISO string
    because that string has no timezone: a browser would read it as ITS OWN
    local time, so a phone away from the server's timezone would silently
    display the wrong hour. Unparseable stamps return None and simply render
    nothing, never a wrong time.
    """
    raw = record.get("ts")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return int(datetime.datetime.fromisoformat(raw).timestamp())
    except ValueError:
        return None


# Redaction (#202). A chat had no eraser: the log is append-only and replayed in
# full, so anything that landed in it — a probe fired at the wrong chat, a secret
# pasted into the composer, an answer that quoted what it should not have — was
# permanent short of deleting the whole chat or hand-editing JSONL with the
# server stopped.
#
# The unit removed is the TURN, not one bubble: the prompt, everything it made
# the model do, and the answer. Half a turn is not a removal — an answer repeats
# what the prompt said, a command echoes the argument it was given, and the
# tool output holds both.
#
# Removal is REAL, not a hidden flag: the turn's records leave the file and a
# tombstone takes their place, AT THEIR POSITION, so the log keeps a dated,
# auditable record that something was removed and where — while the text itself
# is gone from disk. A tombstone that only hid the turn would be the wrong
# answer for the pasted-secret case, which is the whole point of the feature.
#
# A redaction IS activity (`_is_activity` does not exempt it), unlike the
# renderless records #201 took out of that count. It has to be: every device
# holds its own offline copy of the transcript, and the mirror only refetches a
# session whose activity stamp MOVED — so an unmoved stamp would leave the
# removed text sitting in IndexedDB on each of them, which is the one outcome
# this feature exists to prevent. The cost is one unread dot, cleared by opening
# the chat; #201's bug was different in kind, an unread state that re-armed
# itself on every read and could never be cleared at all.
REDACT_KIND = "redact"


def _turn_id(record: dict, index: int) -> str:
    """The name a message record answers to when something wants to point at
    it — a user turn for a removal (#202), an assistant answer for a fork
    (#229).

    Written records carry their own `turn` id, minted at write time — a random
    one rather than a counter, because a resumed log is appended to by a fresh
    SessionLog that would have to reconstruct any counter, and because redaction
    rewrites the file underneath it. Logs written before ids existed fall back to
    the record's LINE INDEX, which identifies a record perfectly well in an
    append-only file (appending never moves an existing line); a redaction
    rewrites the file and the client is handed the recomputed stream immediately
    after, so a shifted index is never left in anyone's hands.
    """
    turn = record.get("turn")
    return turn if isinstance(turn, str) and turn else f"@{index}"


class Redaction(NamedTuple):
    """What a completed redaction removed — enough for the caller to drop the
    same turn from the live model's context and to repaint the view."""

    turn: str
    text: str  # the removed user message, for the in-memory context drop
    occurrence: int  # 1-based, among user messages with identical text
    records: int  # log lines removed
    title: str | None  # re-derived chat title when an AUTO one was invalidated


def synthetic_kind(content: str) -> str:
    """Classify a user message aish wrote itself: `"resume"` for a synthetic
    turn that really did start a task (rendered as a system row, never as a
    blue user bubble), `"note"` for an annotation the live UI never showed at
    all, `""` for text the human actually typed."""
    text = content.lstrip()
    if text.startswith(RESUME_MARKER):
        return "resume"
    return "note" if text.startswith(_NOTE_MARKERS) else ""


# ---------------------------------------------------------------------------
# How a turn says "a file came with this message" (#231).
#
# THE STORED FORM IS AN EMBED: `![[cat.png]]`, wiki-link style. Name only for a
# file aish holds (the uploads folder is the one place they go, and names there
# are made unique on write, so the name IS the address); full path for a file
# that lives anywhere else, where nothing could derive it back.
#
# AN EMBED MEANS THE SAME THING WHEREVER IT SITS (#233). Alone on a line it is
# the file as a block, which is what an attached photo looks like. Inside a
# sentence it is the file in that position — "the error in ![[shot.png]], the fix
# like ![[patch.txt]]" — and the position is information the model gets no other
# way. A notation that only worked at the end of a message was a footer, not a
# representation.
#
# The whole-line rule it replaced was there to stop a `![[…]]` the owner TYPED
# from being eaten, and that mattered because a matched line was REMOVED from the
# bubble and redrawn as a thumbnail: a false match deleted their words. An embed
# rendered in place cannot do that, so the rule is now about the outcome instead
# of the syntax — A REFERENCE THAT RESOLVES TO NOTHING STAYS AS PLAIN TEXT.
# Prose about wiki-links survives because the file it names does not exist, which
# is the same test the frontend applies and the same one `Agent.load_history`
# applies before it rewrites anything.
#
# WHAT THE MODEL IS TOLD IS NOT THIS. It gets a sentence per file — "you can see
# it", "you can read it", or a bare path meaning "go and open this yourself" —
# built fresh at the moment the message is handed over, and again whenever a
# stored conversation is loaded back into a model. That guidance depends on
# which backend is answering right now, so it is transactional by nature and is
# never written down. `attachment_guidance` below is the one place it is built.
#
# The two used to be the same string, and that was the fault. A sentence written
# for a model was the ONLY record that a message had a photo, so everything
# facing the owner had to undo it: the bubble hid it, copy stripped it, the
# title ignored it, reuse re-parsed it to find the file again — two parsers, one
# per language, kept in step by tests. All of it re-deriving from prose a fact
# the record already held as a list.
#
# The three prose forms below are still READ, forever: they are what every chat
# already on disk says, and a log is never rewritten. They are never WRITTEN
# again. The frontend has the same split ([ATTACHMENT-NOTES] in
# docs/web-frontend.md), and both languages are pinned to this format by
# tests/test_server.py::TestAttachmentNoteFormat.
_ATTACHMENT_NOTE_RE = re.compile(
    r"^\[(?:image attached|document attached|attached file):.*\]$"
)


_ATTACHMENT_NAME_RE = re.compile(
    r"^\[(?:image|document) attached: (.+?) — you can (?:see|read) it; file at (.*)\]$"
)
_ATTACHMENT_PATH_RE = re.compile(r"^\[attached file: (.+)\]$")

# The stored form, matched ANYWHERE (#233). No newline inside, so a stray `![[`
# can never swallow the rest of a message; no `]` inside, so the first closer
# ends it.
_EMBED_RE = re.compile(r"!\[\[([^\]\n]+)\]\]")
# …and the same thing anchored, for "is this line nothing but a file?" — which
# is what decides block or inline, and nothing else.
_EMBED_LINE_RE = re.compile(r"^!\[\[([^\]\n]+)\]\]$")


def attachment_embed(path: str, held: bool) -> str:
    """The stored line for one attached file.

    `held` means aish has the file in its own uploads folder, where the name is
    unique and the folder is fixed — so the name alone addresses it, and storing
    a machine-specific absolute path would be noise that also breaks the moment
    the state directory moves. A file from anywhere else keeps its full path,
    because nothing could reconstruct it."""
    return f"![[{Path(path).name if held else path}]]"


def _embedded(line: str) -> str | None:
    """The target of a line that is NOTHING BUT an embed, or None. That is the
    block case — an attached photo on its own line — and the only thing this
    distinction decides is how it renders."""
    match = _EMBED_LINE_RE.match(line.strip())
    return match.group(1) if match else None


# What a rendered chat bubble shows, from what the log stores: markdown is
# formatting, not words. Conservative on purpose — `**` and backticks go because
# they are noise in an excerpt, single `*` and `_` stay because they live inside
# identifiers (`run_command`) far more often than they mean italics.
_MD_LINE_PREFIX_RE = re.compile(r"^\s{0,3}(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>\s?)+")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_MARKS_RE = re.compile(r"\*\*|`")


def plain_text(content: str) -> str:
    """One message as WORDS: markdown syntax removed, its text kept.

    A link becomes its label, which is what the bubble displays — the href is
    machinery, and a search excerpt that quoted one showed a row reading
    "…ual-zone-7w1-17379918218) * **…" for a query about a toaster (#266)."""
    lines = [_MD_LINE_PREFIX_RE.sub("", line) for line in (content or "").split("\n")]
    text = "\n".join(lines)
    text = _MD_IMAGE_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    return _MD_MARKS_RE.sub("", text)


def message_body(content: str) -> str:
    """The message with its attachment LINES removed and its inline references
    left exactly where they are.

    A file alone on a line is an attachment, not a sentence, so it goes. A file
    inside a sentence is part of the sentence and stays (#233) — that position
    is the only thing saying which file belongs to which clause, and it is what
    the model is meant to read.

    This is the derivation for anything that has to preserve the message:
    rebuilding a model's view of a stored turn, and the composer's reuse.
    `strip_attachment_notes` below is the DISPLAY derivation and deliberately
    differs."""
    kept = [
        raw for raw in (content or "").split("\n")
        if not _ATTACHMENT_NOTE_RE.match(raw.strip()) and _embedded(raw) is None
    ]
    return "\n".join(kept).strip()


def strip_attachment_notes(content: str) -> str:
    """What the owner actually wrote, for DISPLAY — titles, preview lines,
    search snippets and the offline mirror all read through it.

    Same as `message_body` except that an inline reference becomes its NAME:
    dropping it would leave "the error in , the fix like ", a title with its
    subject taken out, and keeping the brackets would put notation in a chat
    title. The name is what the owner would have written anyway.

    The two derivations differ ON PURPOSE and the difference is the audience: a
    title is read, a message is re-sent."""
    return _EMBED_RE.sub(
        lambda m: m.group(1).rsplit("/", 1)[-1], message_body(content)
    ).strip()


def attachment_names(content: str) -> list[str]:
    """What a turn attached, by name. Used to name a turn that has no words of
    its own — a photo sent with nothing typed is still about something, and
    "IMG_4021.jpg" beats an unnamed chat."""
    return [item.name for item in attachment_refs(content)]


def visible_messages(messages: list[dict]) -> list[tuple[str, str]]:
    """The (role, text) pairs a person can SEE in the chat: their own words and
    aish's answers, an attachment read as its file name.

    Everything else in the log is machinery — tool results (a command's output, a
    fetched page, a file read, a recall excerpt) and aish's own `[aish: …]` notes.
    Reasoning is not here either, because it is a trace record and never enters
    `messages` at all. The log carries all of it since the record has to be honest
    about what ran; the CHAT does not show it, so a search over the chat must not
    match on it (#266): filtering for "tefal" returned chats whose only mention of
    it was inside a results blob the model read and never repeated.

    ONE derivation, read by the preview line and by the search index. Two
    functions answering "what does this chat say" is precisely how a row that
    shows nothing about the query comes back for the query."""
    visible: list[tuple[str, str]] = []
    for message in messages:
        if message.get("role") not in ("user", "assistant"):
            continue
        raw = message.get("content") or ""
        # Notes out BEFORE the whitespace flatten, or the line boundary they are
        # identified by is gone.
        content = " ".join(plain_text(strip_attachment_notes(raw)).split())
        if not content:
            # A wordless turn still said something: a photo sent with nothing
            # typed is about its file, and that name is a thing people search for.
            names = attachment_names(raw)
            if not names:
                continue
            content = ", ".join(names)
        elif synthetic_kind(content) == "note":
            continue  # aish talking to itself; it never reached the transcript
        visible.append((message.get("role") or "", content))
    return visible


def to_record_form(content: str, uploads_dir: Path | None) -> str:
    """A message turned into the form that gets STORED (#231): every attachment
    line becomes `![[…]]`, whatever it was before.

    Two forms of a message exist and the names are worth keeping straight. The
    **record form** is what the log holds and what the owner sees, copies and
    reuses — embeds, no machine prose. The **guidance form** is what a model is
    handed, with a sentence per file saying whether it can look at it or must
    open it; that depends on which backend is answering and is never stored.

    This converts either into the record form, so it is idempotent and it
    SELF-HEALS: any path that hands guidance text back around — retry rewinding
    the model's own context is the live example — lands back on the stored form
    instead of quietly writing prose into a fresh log line. Content with no
    attachment lines comes back byte-identical, which is almost every message."""
    # Files the message already names IN ITS TEXT. A file named inline ALSO gets
    # a guidance line, because the model is told about every file — so without
    # this the record would carry it twice, once in the sentence where it was
    # written and once again in a trailing list, and the bubble would show the
    # same photo two times (#233).
    inline = {
        resolve_attachment(item.ref, uploads_dir)
        for item in attachment_refs(content)
        if item.embed
    }
    out: list[str] = []
    for raw in (content or "").split("\n"):
        line = raw.strip()
        if _EMBED_RE.search(raw):
            # Already the record form, wherever on the line it sits. Left
            # exactly as written — rewriting it would move an inline file out of
            # the sentence it belongs to, which is the whole point of it being
            # there.
            out.append(raw)
            continue
        legacy = _ATTACHMENT_NAME_RE.match(line) or _ATTACHMENT_PATH_RE.match(line)
        if not legacy:
            out.append(raw)
            continue
        path = resolve_attachment(attachment_refs(line)[0].ref, uploads_dir)
        if path in inline:
            continue  # said once, in the place the owner put it
        held = uploads_dir is not None and Path(path).parent == Path(uploads_dir)
        out.append(attachment_embed(path, held))
    return "\n".join(out).strip("\n")


def resolve_attachment(ref: str, uploads_dir: Path | None) -> str:
    """An attachment reference turned back into a path on disk.

    A reference with no directory in it names a file aish holds, so it resolves
    against the uploads folder; anything else already IS a path. With no uploads
    folder known (a bare Agent, a test), a name resolves to itself — wrong as a
    path, but never a crash, and never a path pointing somewhere unintended.

    A BARE NAME CAN ONLY EVER MEAN THAT FOLDER. Since an embed can be typed
    (#233), the reference is input now, not just something the server wrote —
    and `..` in a name would otherwise walk out of the one directory whose
    contents are safe to hand over without asking. A name that escapes resolves
    to itself, which exists nowhere and is therefore treated as plain text."""
    if uploads_dir is None or "/" in ref:
        return ref
    if ref in ("", ".", "..") or ref.startswith("."):
        return ref
    return str(uploads_dir / ref)


def real_attachments(content: str, uploads_dir: Path | None) -> list[tuple[str, str]]:
    """The files a message names that ACTUALLY EXIST, as (name, path).

    This is the rule that replaced whole-line matching (#233). An embed can now
    be typed, and typed text about wiki-links must survive — so what separates
    "the owner attached a file" from "the owner wrote about the notation" is
    whether the thing named is on disk. Prose about `![[note]]` names no file
    and stays prose.

    The check applies to EMBEDS only. The legacy bracketed forms are not
    something anyone types by accident, and a file since deleted should still
    tell the model it was once attached, so those are taken at their word."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in attachment_refs(content):
        path = resolve_attachment(item.ref, uploads_dir)
        if path in seen:
            continue  # named twice in one message is one file, delivered once
        if item.embed:
            try:
                if not Path(path).is_file():
                    continue
            except OSError:
                continue
        seen.add(path)
        found.append((item.name, path))
    return found


def files_named(content: str, uploads_dir: Path | None) -> list[dict] | None:
    """What a CLIENT needs to draw a message: the references in it that name a
    real file, as `{"name", "path"}` — or None when it holds no `![[…]]` at all
    and the question never arises (#233).

    Empty and absent mean different things and both are needed. Empty is "I
    looked, and none of these is a file", which is what lets prose about the
    notation render as prose. Absent is "nobody looked" — an older server, a
    mirror written before this — where the client takes references at face
    value, the behaviour from before and never worse than it."""
    if not any(item.embed for item in attachment_refs(content)):
        return None
    return [
        {"name": name, "path": path}
        for name, path in real_attachments(content, uploads_dir)
    ]


def attachment_guidance(name: str, path: str, kind: str) -> str:
    """The sentence the MODEL is given about one attached file.

    `kind` is what actually happened to it on the way out: `"image"` or
    `"document"` mean the bytes are in the request and the model may simply look
    (the two backends differ on which they take, which is why this is decided at
    hand-over time and not stored); anything else means only the path went, and
    the model must open the file with a tool if it wants the contents.

    This is the ONE producer. `_classify_attachments` calls it when a message is
    sent, and `Agent.load_history` calls it again when a stored conversation is
    loaded back into a model — so a turn reads identically to the model whether
    it just happened or happened last month. Nothing that comes out of here is
    ever written to the log; the log holds `attachment_embed` instead."""
    if kind == "image":
        return f"[image attached: {name} — you can see it; file at {path}]"
    if kind == "document":
        return f"[document attached: {name} — you can read it; file at {path}]"
    return f"[attached file: {path}]"


class Attachment(NamedTuple):
    """One file a message names.

    `ref` is what the message says — a bare name for a file aish holds, an
    absolute path for one it does not — and is what a reader resolves. `name` is
    the last segment, for anything that only needs to say which file. `embed`
    marks the modern `![[…]]` form, which is the one that can be TYPED and so
    the one that has to prove the file exists before it counts (#233)."""

    ref: str
    name: str
    embed: bool


def attachment_refs(content: str) -> list[Attachment]:
    """Every file a message names, in the order written. Reads the modern embeds
    and all three legacy prose forms, so a chat written years apart parses the
    same way."""
    refs: list[Attachment] = []
    for raw in (content or "").split("\n"):
        line = raw.strip()
        embeds = _EMBED_RE.findall(raw)
        if embeds:
            # Every embed on the line, in the order written — one alone (an
            # attached photo) or several inside a sentence (#233) are the same
            # thing to every reader; only the renderer cares which.
            refs.extend(
                Attachment(t, t.rsplit("/", 1)[-1], True) for t in embeds
            )
            continue
        # Legacy prose carries the path as well as the name, and the PATH is the
        # reference: those notes predate the uploads folder being the only home
        # for an attachment, so the name alone may not resolve.
        named = _ATTACHMENT_NAME_RE.match(line)
        if named:
            refs.append(Attachment(named.group(2) or named.group(1), named.group(1), False))
            continue
        plain = _ATTACHMENT_PATH_RE.match(line)
        if plain:
            target = plain.group(1)
            refs.append(Attachment(target, target.rsplit("/", 1)[-1], False))
    return refs

# Model-facing search (the search_sessions tool): bounded so one call can
# never flood a small context window.
SEARCH_TOP = 5
SNIPPET_CHARS = 200
SNIPPETS_PER_SESSION = 3
DETAIL_MESSAGE_CHARS = 700
DETAIL_MAX_CHARS = 6000
DETAIL_TAIL_MESSAGES = 20
RECALL_SESSIONS_TOP = 3  # sessions shown in the recall tool's fallback section
_SESSION_NAME_RE = re.compile(r"^session-[0-9-]+\.jsonl$")
# _derive_title's placeholder for a chat with no real user message — _peek
# compares against it so such chats stay out of the pager (title None).
_NO_USER_INPUT = "(no user input)"

# Auto-titling (#175): how far a chat has to move before its title is worth a
# model call. Words of 4+ characters carry the subject; if enough of the title's
# words are still being used, the conversation is where the title says it is.
DRIFT_MIN_WORD = 4
DRIFT_KEPT_RATIO = 0.34


class ParsedLog(NamedTuple):
    """One pass over a session log. A NamedTuple rather than a bare tuple so
    metadata can be added without breaking every unpacking call site."""

    messages: list[dict]
    model: str
    title: str | None
    origin: str
    cwd: str
    title_auto: bool
    user_cmds: list[str]  # successful user-direct ! commands, in file order
    activity_ts: int | None  # epoch seconds of the last record that IS activity
    output_ts: int | None  # …and of the last that is OUTPUT (see _is_output)


# Stat-keyed caches for the read-only listing paths (drawer, pager, offline
# index, command palette): those re-scan the whole state dir on every call, and
# with hundreds of session logs the repeated JSONL parsing was the bulk of a
# session-switch's server time — GIL-holding work that also slowed the event
# loop. A (mtime_ns, size) key identifies a version of an append-only log
# without reading it, so an unchanged file costs one stat. The caches serve
# SHARED objects, so only paths that never mutate what they read may use them —
# resume paths hand messages to an Agent that trims tool outputs IN PLACE
# (_trim_tool_message), and those stay on the raw `_parse` by design.
_PARSE_CACHE: dict[str, tuple[tuple[int, int], "ParsedLog"]] = {}
_ENTRY_CACHE: dict[str, tuple[tuple[int, int], "SessionEntry | None"]] = {}


# Trace kinds that are durable governance evidence and render NOWHERE
# (docs/trace-contract.md §1.3). Both halves are required and neither is
# sufficient alone: the agent emits these through `Agent._emit_record`, which
# never reaches `on_step`, and `reconstruct_events` skips them here — so they
# are absent live AND absent cold, which is what makes hot/cold parity hold
# (the argument is #171's for `[aish: …]` notes and #188's for render_error).
#
# The reason this must be a registry rather than a habit: `app.js`'s
# `traceStep` calls `ensureTrace()` BEFORE it dispatches on `step.kind`, so a
# kind with no renderer does not degrade to "renders nothing" — it opens an
# empty live trace card with a running ticker. A new kind reaching the
# frontend is a visible bug, not a no-op.
RENDERLESS_STEPS = frozenset(
    {
        "render_error",  # #188
        "rule_eval",  # #191
        "binding",  # #191
        "gate",  # #191
        # `trim` LEFT this set in #243. It is the one governance record that is
        # about something the reader can SEE: the transcript still shows the
        # full page while the model holds 200 characters of it, and the screen
        # is what he is reading. Everything else here describes a decision he
        # can look up on demand; this one contradicts what is in front of him,
        # so it draws a row on the turn it prepared.
        "tool_check",  # #193
        "admission",  # #194
        "context",  # #208
        "brief",  # #239
        "reasoning",  # #240
        "call",  # #240
    }
)


def _content_words(text: str) -> set[str]:
    stripped = (word.strip(_PUNCT).casefold() for word in (text or "").split())
    return {word for word in stripped if len(word) >= DRIFT_MIN_WORD}


def title_drifted(title: str, recent_text: str) -> bool:
    """Has the conversation moved away from what its title says (#175)?

    A free, deterministic gate in front of the model call that rewrites a
    title: a chat still using the title's subject words does not need renaming.
    Deliberately lexical — no embedding call, no network — because spending a
    model call to decide whether to spend a model call is a bad trade. The cost
    of being wrong is small either way: a false "no drift" leaves a stale title
    until the next backoff step, a false "drift" buys one cheap extra call.
    """
    wanted = _content_words(title)
    if not wanted:
        return True  # a title with nothing to match on is worth replacing
    kept = wanted & _content_words(recent_text)
    return (len(kept) / len(wanted)) < DRIFT_KEPT_RATIO

# Terminal-mode command autocomplete (#104): the personal command palette is
# built from the user's own successful ! commands across sessions. Both caps
# keep the disk scan mtime-cheap and the delivered list small.
USER_HISTORY_MAX = 100  # commands offered to the web terminal-mode composer
USER_HISTORY_SESSIONS = 200  # most-recent sessions scanned for that history
# run_command appends this exit marker to its returned output; the live stream
# never carries it (the code comes via command_end), so reconstruction strips
# it before replaying the output into the terminal block.
_EXIT_MARKER_RE = re.compile(r"\n?\[exit code: -?\d+\]\s*$")

# Shown when a cold-loaded session's last turn was cut off mid-step (server
# restart / crash during a task) so it fails cleanly with a Retry instead of
# spinning forever.
INTERRUPTED_TASK = (
    "This task was interrupted before it finished (the server restarted or the "
    "task was cut off), so its result is unknown. Retry to run it again."
)
# A recorded failure's text, capped like every other free-text field. Long
# enough for a repr of a backend exception, short enough that a crash loop
# cannot grow the log without bound.
TASK_ERROR_CAP = 500

# A rating's reason is a sentence, not an essay — and it is the owner's own
# words, so it is quoted back to him in the weekly pass and must stay readable.
RATING_COMMENT_CAP = 1000
# `none` is a WITHDRAWAL, not a third opinion: tapping a lit thumb again takes
# it back, and the log stays append-only (a `none` written after a `down`)
# rather than growing a delete path. Readers take the last record per turn, so
# a withdrawn rating simply stops counting.
RATING_NONE = "none"
RATINGS = frozenset({"up", "down", RATING_NONE})


@dataclass
class SessionInfo:
    path: Path
    when: str
    count: int
    title: str
    model: str = ""  # last model used; "" for sessions logged before model records
    snippet: str = ""  # last visible message — the drawer's preview line
    mtime: float = 0.0  # when the FILE last changed — the cheap recency pre-sort
    # When the CHAT last did something (#201). Distinct from mtime because
    # merely LOOKING at a chat can append to its log (a renderless render_error),
    # and a row stamped with that reads as new activity to every consumer —
    # unread most of all. Falls back to mtime for logs with no usable stamps.
    activity: float = 0.0
    # When the chat last put something in the CONVERSATION (#203). Ordering
    # reads `activity` (a chat mid-turn is the most recent thing there is);
    # UNREAD reads this, or a chat that was merely thinking marks itself unread
    # with nothing new to show. 0.0 when the chat has no output at all.
    output: float = 0.0
    origin: str = "user"  # who started it: user | schedule | email | webhook (#160)
    cwd: str = ""  # last logged working directory; "" when the chat never moved


class Ranked(NamedTuple):
    """A ranked answer and WHAT KIND of answer it is. Approximate results are a
    different statement — "nothing you typed is in any chat, here are the
    closest" — and a list that cannot say which of the two it is puts rows on
    screen that do not contain the query with nothing to explain them (#266)."""

    sessions: list[SessionInfo]
    approximate: bool


@dataclass
class SessionEntry:
    """A session preloaded for searching: display info plus casefolded
    title/contents/model and a word vocabulary, so ranking never re-reads
    the file."""

    info: SessionInfo
    title_cf: str
    content_cf: str
    words: frozenset
    model_cf: str = ""
    # The same text as `content_cf` in the case it was WRITTEN in. Matching
    # reads the casefold; a result row quotes this, and a row that answered a
    # search in lower case would be the search's own machinery on screen.
    content: str = ""


def _record_or_none(line: str) -> dict | None:
    """One log line as a record, or None — tolerant of BOTH failure modes.

    A torn line does not parse. A line that PARSES but yields a bare JSON value
    (a string, a number) is not a record either, and every reader here calls
    `.get()` on what it gets back. That second mode is the one that hurt: on
    2026-08-20 a single log whose lines had been reformatted raised
    `AttributeError` inside `_parse`, which `pager_titles` calls for EVERY
    session, so the websocket closed on attach and every client — phone
    included — sat on the boot spinner with no chat list at all.

    One unreadable log must cost its own chat and nothing else. Skipping is
    already what a torn line gets, and every reader is written to tolerate a
    missing record; none of them can tolerate a raise.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


class SessionLog:
    def __init__(self, path: Path):
        self.path = path
        self._fh: TextIO | None = None
        self._pending_model: str | None = None
        # When this chat last put something in the CONVERSATION, epoch seconds
        # (#203 / #275). Held in memory so a LIVE session can be asked "when did
        # you last say anything" without re-reading its log — the roster row
        # needs that fact, and a chat that just did something has just changed
        # its file, so a parse there is guaranteed to miss its own cache. Kept
        # by the same predicate the listing uses (`_is_output`), applied to
        # records on the way OUT rather than on the way back in, so the live
        # answer and the parsed one cannot drift. 0.0 until the chat speaks;
        # seeded from the parse when a session is opened cold.
        self.output_at: float = 0.0
        # Serializes every write to the log file — and the shared write state
        # feeding it (_fh, _pending_model). Writers live on more than one
        # thread (the agent worker logs records while the event loop renames /
        # audits), and an interleaved write on the buffered handle is a torn
        # JSONL line every reader silently skips. Any NEW code path that
        # appends to or rewrites the file must hold this lock.
        self._write_lock = threading.Lock()

    def close(self) -> None:
        """Release the append handle; a session that never recorded anything
        has no handle and leaves no file."""
        with self._write_lock:
            if self._fh is not None:
                self._fh.close()

    @classmethod
    def new(cls, state_dir: Path) -> "SessionLog":
        # Microseconds: /new within the same second must not reuse the file.
        name = datetime.datetime.now().strftime("session-%Y%m%d-%H%M%S-%f.jsonl")
        return cls(state_dir / name)

    @staticmethod
    def latest(state_dir: Path) -> Path | None:
        """Most recently interacted-with session (not most recently created)."""
        files = SessionLog._by_recency(state_dir)
        return files[0] if files else None

    @staticmethod
    def _parse(path: Path) -> ParsedLog:
        """One pass over the file: conversation messages (no audit records, no
        stale system prompt — a fresh one is built on resume), the last
        recorded model ("" for sessions that predate model records), the
        latest stored title (None when the chat was never titled) and whether
        that title was auto-generated (#175), the session origin ("user" unless
        a `kind:"origin"` record says otherwise — so triggered sessions cold-load
        with their provenance intact, #160), and the latest working directory
        ("" when the chat never moved). The `kind:"title"`/`"origin"`/`"cwd"`
        records are metadata — they never enter `messages`, so they can't leak
        into a resumed conversation.

        It also carries the chat's last ACTIVITY (#201) — see `_is_activity`."""
        messages: list[dict] = []
        model = ""
        custom_title: str | None = None
        title_auto = False
        origin = "user"
        cwd = ""
        user_cmds: list[str] = []
        activity_ts: int | None = None
        output_ts: int | None = None
        pending_cmd: str | None = None  # a user ! command awaiting its exit status
        for line in path.read_text(encoding="utf-8").splitlines():
            record = _record_or_none(line)
            if record is None:
                continue
            kind = record.get("kind")
            if SessionLog._is_activity(record):
                # The LATEST such stamp, not the last one in the file: a
                # redaction (#202) rewrites the log and leaves its tombstone at
                # the removed turn's position, so file order stopped being
                # chronological order the moment anything could be removed.
                stamp = record_epoch(record)
                if stamp and (activity_ts is None or stamp > activity_ts):
                    activity_ts = stamp
            if SessionLog._is_output(record):
                stamp = record_epoch(record)
                if stamp and (output_ts is None or stamp > output_ts):
                    output_ts = stamp
            if kind == "model":
                model = record.get("model") or model
            elif kind == "title":
                title = (record.get("title") or "").strip()
                if title:  # latest non-empty title wins
                    custom_title = title
                    # Records written before auto-titling (#175) carry no flag,
                    # and every one of those was a hand-typed rename.
                    title_auto = bool(record.get("auto"))
            elif kind == "origin":
                origin = record.get("origin") or origin
            elif kind == "cwd":
                cwd = record.get("cwd") or cwd
            elif kind == "command":
                # Any command record resets the pending one, so a model command
                # can never inherit a preceding user command's exit status.
                pending_cmd = (
                    record.get("command")
                    if record.get("decision") == "user-direct"
                    else None
                )
            elif kind == "cmd_end" and pending_cmd is not None:
                if record.get("status") == "exit" and record.get("exit_code") == 0:
                    user_cmds.append(pending_cmd)
                pending_cmd = None
            elif kind == "message" and record.get("role") != "system":
                keys = ("role", "content", "tool_name", "images", "documents")
                messages.append({k: v for k, v in record.items() if k in keys})
        return ParsedLog(
            messages, model, custom_title, origin, cwd, title_auto, user_cmds,
            activity_ts, output_ts,
        )

    @staticmethod
    def _is_output(record: dict) -> bool:
        """Did this record put something in the CONVERSATION — something a
        person would read?

        `_is_activity` below answers a different question ("did anything happen
        in this chat"), and one stamp was doing both jobs. Ordering wants the
        first: a chat working right now IS the most recent thing there is.
        UNREAD wants the second, and got the first — so every thinking step a
        chat took moved its stamp past your last look and the row went unread
        with nothing new behind it. Splitting the fact is what stops the next
        consumer inheriting the same lie (#203).

        The rule is READ OFF `reconstruct_events`, not invented beside it:
        output is exactly what that function turns into transcript content —
        a `user` bubble or the assistant text that becomes a turn's `done`.
        Everything else it emits is a trace step, a workspace marker or
        framing, and everything it ignores (model, title, origin, the audit
        `command` line, task_start/task_end) was never on screen at all.

        Consequences worth knowing, each a false unread that is now gone: a
        rename or a redaction from another device, a turn CANCELLED with no
        answer, and a chat that is simply thinking. And one gap: a background
        turn that FAILS produces no assistant message, so it is caught by its
        `task_end` instead — see below."""
        if record.get("kind") == "task_end":
            # A turn that died has something to tell you and no message to tell
            # it with: the failure text goes out as a live `error` event, which
            # a client that was not connected never sees. This is what makes an
            # overnight job's death raise a dot rather than waiting to be found.
            # A record with no `status` predates the field and stays silent.
            return record.get("status") not in (None, "ok")
        if record.get("kind") != "message":
            return False
        role = record.get("role")
        content = record.get("content") or ""
        if role == "assistant":
            # An intermediate tool-calling turn carries no visible text, and
            # reconstruct_events keeps only the last NON-EMPTY answer.
            return bool(content.strip())
        if role != "user":
            return False  # tool results and system prompts render nowhere
        # aish's own `[aish: …]` annotations never reached the transcript live
        # and are skipped on replay (#171), so they are not output either. A
        # trigger's prompt IS a user message and counts — the chat really does
        # have something in it you have not read.
        return synthetic_kind(content) != "note"

    @staticmethod
    def _is_activity(record: dict) -> bool:
        """Did this record happen BECAUSE something happened in the chat, or
        merely because someone looked at it? (#201)

        "Last interaction" used to be the file's mtime, which cannot tell those
        apart — every append moved it. So a chat holding an image that fails to
        render (a remote host off the fetch whitelist, an evicted media file)
        wrote a `render_error` record on every replay, a second or so AFTER the
        client had already stamped "seen", and came back unread the moment you
        left it. Opening a chat marked it unread, permanently, and no amount of
        reading could clear it.

        The rule, reusing a distinction this module already draws: **a record
        that renders nowhere is not activity.** `RENDERLESS_STEPS` is the one
        registry of those kinds, so a future governance record inherits this by
        construction instead of re-teaching it. `model` is excluded for the
        reason it is written lazily in the first place (see `model()`): it says
        which model a chat runs, never that it ran.
        """
        kind = record.get("kind")
        if kind == "model":
            return False
        if kind == "trace":
            step = record.get("step")
            step_kind = step.get("kind") if isinstance(step, dict) else None
            return step_kind not in RENDERLESS_STEPS
        return True

    @staticmethod
    def _cached_parse(path: Path) -> ParsedLog:
        """`_parse` behind the stat-keyed cache — for READ-ONLY consumers only
        (the returned object is shared; see the cache comment above ParsedLog).
        A stat/read race can at worst cache newer content under an older key,
        which the next stat corrects — it can never serve content older than
        its key claims."""
        key = None
        try:
            stat = path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass
        spath = str(path)
        if key is not None:
            hit = _PARSE_CACHE.get(spath)
            if hit is not None and hit[0] == key:
                return hit[1]
        parsed = SessionLog._parse(path)
        if key is not None:
            _PARSE_CACHE[spath] = (key, parsed)
        return parsed

    @staticmethod
    def load_messages(path: Path) -> list[dict]:
        return SessionLog._parse(path).messages

    @staticmethod
    def restore_state(path: Path) -> tuple[str | None, list[str]]:
        """Workspace state to reapply on resume/cold-open: the LATEST logged
        cwd (None if the session never moved) and the ACCUMULATED set of
        trusted dirs, in first-seen order. Existence is not checked here — the
        agent's restore_workspace degrades missing paths gracefully."""
        cwd: str | None = None
        trusted: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            record = _record_or_none(line)
            if record is None:
                continue
            kind = record.get("kind")
            if kind == "cwd":
                cwd = record.get("cwd") or cwd
            elif kind == "trust_dir":
                trust_path = record.get("path")
                if trust_path and trust_path not in trusted:
                    trusted.append(trust_path)
        return cwd, trusted

    @staticmethod
    def last_turn(path: Path) -> int:
        """The highest turn counter this log has already used (0 for a log
        written before the trace contract, or by a session that never reached
        the rule engine).

        `Agent._turn` lives on the AGENT, and a chat gets a fresh agent every
        time it is reopened — on the web, every restart of aish-web, which is
        every ship. So a conversation spanning a restart used to start counting
        at 1 again and two different turns answered to the same id. `turn` is
        the join key the whole trace contract rests on: curate's rule ledger
        pairs a `rule_eval` with the `gate` rows sharing its turn, explicitly
        "joined by id, never by position", and a collision silently merges two
        turns into one — losing exactly the property the id was introduced for.

        Read from `step.turn` only. Ratings carry a top-level `turn` that is the
        CLIENT's event id, a string naming a different thing; the isinstance
        check is what keeps the two namespaces apart.
        """
        last = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            record = _record_or_none(line)
            if record is None:
                continue
            step = record.get("step")
            turn = step.get("turn") if isinstance(step, dict) else None
            if isinstance(turn, int) and turn > last:
                last = turn
        return last

    @staticmethod
    def calls_that_ran(path: Path) -> list[tuple[dict, int]]:
        """Every tool call this log says actually RAN, oldest first, as
        (call-record, epoch) — the record shaped the way Verify reads one
        (`{"tool", "args", "status"}`).

        One step is TWO records under the trace contract §2: `call` carries the
        model's own arguments, `tool` carries the runtime's verdict, and they
        are joined by (turn, call) — never by position, because read-only tools
        run in parallel and their steps interleave. A log written before the
        contract has neither id; those steps are skipped rather than guessed
        at, which costs a reopened chat one repeat call and can never
        misattribute a verdict to the wrong arguments.

        It exists so a reopened chat can refill the ledger of what it has
        already opened (`Agent.restore_opened_links`, #267). Deliberately
        knows nothing about URLs: which argument is a link, and what counts as
        opened, is the rule engine's fence and must have one definition.
        """
        args_by_call: dict[tuple[int, int], tuple[str, dict, int]] = {}
        ran: list[tuple[dict, int]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            record = _record_or_none(line)
            if record is None:
                continue
            step = record.get("step")
            if not isinstance(step, dict):
                continue
            turn, call = step.get("turn"), step.get("call")
            if not isinstance(turn, int) or not isinstance(call, int):
                continue
            when = record_epoch(record)
            if step.get("kind") == "call" and isinstance(step.get("args"), dict):
                args_by_call[(turn, call)] = (
                    str(step.get("name") or ""), dict(step["args"]), when or 0
                )
                continue
            if step.get("kind") != "tool":
                continue
            emitted = args_by_call.pop((turn, call), None)
            if emitted is None:
                continue
            name, args, at = emitted
            ran.append((
                {
                    "tool": step.get("name") or name,
                    "args": args,
                    # Mirrors rules._ran: what the runtime said, not a guess at
                    # what the result looked like. `ok` is the pre-envelope
                    # spelling of the same verdict, kept so old logs read alike.
                    "status": step.get("status") or ("ok" if step.get("ok") else "failed"),
                    "decision": step.get("decision"),
                },
                when or at,
            ))
        return ran

    @staticmethod
    def pending_task(path: Path) -> dict | None:
        """The task this session was still running when its process died, or
        None when the last task finished normally (#164). A `task_start` record
        with no matching `task_end` after it IS the interruption signal — a
        killed process cannot write anything, so absence is the evidence.

        `attempts` counts the task_start records since the last task_end, so a
        task that keeps killing the server is resumed a bounded number of times
        instead of crash-looping it forever.

        `in_flight` names the steps that had STARTED but never reported back —
        a `tool_start` trace record with no matching `tool`. That is the one
        genuinely dangerous gap in a resume: a completed step logged its result,
        but an in-flight one may or may not have taken effect (a send, a write),
        and only the model re-checking reality can tell."""
        pending: dict | None = None
        attempts = 0
        started: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            record = _record_or_none(line)
            if record is None:
                continue
            kind = record.get("kind")
            if kind == "task_start":
                attempts += 1
                started = []
                pending = {
                    "prompt": record.get("prompt") or "",
                    "ts": record.get("ts") or "",
                    "attempts": attempts,
                }
            elif kind == "task_end":
                pending, attempts, started = None, 0, []
            elif kind == "trace" and pending is not None:
                step = record.get("step") or {}
                if step.get("kind") == "tool_start":
                    started.append(step)
                elif step.get("kind") == "tool":
                    # Read-only calls run in parallel, so match by name rather
                    # than assuming the last start is the one finishing.
                    for i, pending_step in enumerate(started):
                        if pending_step.get("name") == step.get("name"):
                            del started[i]
                            break
        if pending is not None:
            pending["in_flight"] = [SessionLog._describe_step(s) for s in started]
        return pending

    @staticmethod
    def _describe_step(step: dict) -> str:
        """One short line naming a tool call: the tool plus whatever identifies
        THIS call (a command, a URL, a summary), as the trace already records it."""
        name = step.get("name") or "step"
        detail = (step.get("command") or step.get("summary") or "").strip()
        return f"{name}: {detail[:160]}" if detail else name

    @staticmethod
    def interrupted_sessions(
        state_dir: Path, max_age_secs: float
    ) -> list[tuple[Path, dict]]:
        """Every recently-active session log holding an unfinished task, newest
        first (#164) — what the web server replays after a restart. The age
        window is deliberate: resuming a day-old task would surprise more than
        it helps, and the restarts this recovers from happen within minutes."""
        cutoff = time.time() - max_age_secs
        found: list[tuple[Path, dict]] = []
        for path in SessionLog._by_recency(state_dir):
            try:
                if path.stat().st_mtime < cutoff:
                    break  # _by_recency is newest-first: everything older follows
                pending = SessionLog.pending_task(path)
            except OSError:
                continue
            if pending is not None:
                found.append((path, pending))
        return found

    @staticmethod
    def reconstruct_events(path: Path) -> list[dict] | None:
        """Rebuild the EXACT transcript event stream a rich client replays, so
        a cold-loaded session feeds the frontend the same events a live one
        does — same data, same code, same rendered output. Groups the log by
        task (a user message opens a turn; the turn's final assistant text is
        its `done` answer), and reassembles each run_command into its full
        `command_start → stream → command_end → tool` sequence so the terminal
        block reconstructs identically instead of falling back to a plain box.

        Returns None when the log predates trace records (no `trace` kind), so
        the caller can fall back to a flat conversation history."""
        # A log lives in the state dir, and the uploads folder sits beside it —
        # so a bare `![[cat.png]]` can be resolved from the log's own location,
        # with nothing to pass in and nothing to keep in step (#233).
        uploads = path.parent / "uploads"
        events: list[dict] = []
        ratings: list[dict] = []
        steps: list[dict] = []
        answer = ""
        answer_id = ""
        # Where in `steps` each of this turn's deliveries sits (#212). A turn
        # says things on its way to the answer, and which of them WAS the
        # answer is only knowable at the end — so they are all buffered in
        # place, and `flush` lifts the last one out to become `done`.
        deliveries: list[int] = []
        # The id of the record behind each of those deliveries, in step. The one
        # `flush` promotes to `done` carries its id onto the event, so a fork
        # names the ANSWER and never a count of what a client happened to render
        # (#229).
        delivery_ids: list[str] = []
        open_turn = False
        has_trace = False
        running_steps = 0  # started (thinking/tool) but not finished — a cut-off turn
        pending_start: dict | None = None
        pending_end: dict | None = None
        origin = "user"
        failure = ""  # a recorded task failure, replayed as the turn's `error`
        first_user = True  # the opening turn of a triggered session is its trigger

        def flush() -> None:
            nonlocal steps, answer, open_turn, running_steps, failure, deliveries
            nonlocal delivery_ids, answer_id
            if not open_turn:
                return
            if deliveries and not failure:
                # The LAST thing said is the turn's answer and leaves the
                # timeline to become `done`; everything before it stays where
                # it was said, between the steps it interleaved with. Live
                # those arrived as tokens closed by a `delivery`, which is
                # exactly what is replayed here (L1).
                #
                # `not failure` is load-bearing: a turn that ENDED IN A FAILURE
                # has no answer to promote, and the failure branch below throws
                # `answer` away — so lifting the last delivery out deleted it
                # from the record entirely. The owner watched that narration
                # arrive and then saw the error; a cold reload showed the error
                # with the words gone, on exactly the long flaky turns
                # narration exists for. A failed turn keeps ALL of them.
                last = deliveries[-1]
                answer = steps[last]["text"]
                answer_id = delivery_ids[-1]
                del steps[last : last + 2]
                deliveries = deliveries[:-1]
                delivery_ids = delivery_ids[:-1]
            # ONE acknowledgement per turn reaches the owner, so replay shows
            # one too (L1). The log still records every interim message — that
            # is the honest record of what the model said — but the harness
            # delivers only the first, and a cold reload that replayed all of
            # them would show a play-by-play the live turn never did. Dropped
            # highest-index first, or removing one pair shifts the next.
            for start in reversed(deliveries[1:]):
                del steps[start : start + 2]
            events.extend(steps)
            deliveries = []
            delivery_ids = []
            if failure:
                # The turn's own recorded failure, replayed as the `error` event
                # a live viewer saw (#203). It outranks the inference below: the
                # log knows WHY, and "cut off mid-step" was only ever a guess
                # made from the fact that steps were left unfinished.
                events.append({"type": "error", "text": failure})
                steps, answer, open_turn, running_steps, failure = [], "", False, 0, ""
                answer_id = ""
                return
            if running_steps > 0 and not answer:
                # A step was still running and no final answer was reached — the
                # process was killed mid-turn (e.g. a deploy during a web search).
                # Surface it as a failed turn with a Retry affordance instead of a
                # `done` that leaves the step spinning forever. (A turn that DID
                # answer is complete even if a trailing trace record was clipped.)
                events.append({"type": "error", "text": INTERRUPTED_TASK})
            else:
                done: dict = {"type": "done", "result": answer}
                # Only when this turn really promoted an assistant record. A
                # bang command's empty `done` names nothing, and a fork must
                # refuse rather than guess at a cut point.
                if answer_id:
                    done["answer"] = answer_id
                events.append(done)
            steps = []
            answer = ""
            answer_id = ""
            open_turn = False
            running_steps = 0

        def emit_command(step: dict) -> None:
            """Splice a run_command's terminal-block framing around its `tool`
            step, so the reconstructed stream matches the live one exactly. The
            command's output rides on the step (not duplicated in the framing
            records); it is replayed as one `stream` chunk — the final panel is
            identical whether the live output arrived in one piece or many."""
            nonlocal pending_start, pending_end
            if step.get("decision") in ("denied", "blocked", "held", "rejected"):
                # Never executed → no terminal block, matching the live path
                # (the frontend renders it struck-through from the tool step
                # alone). Without this the None-framing branch below would wrongly
                # synthesize a block, diverging from the live transcript.
                steps.append({"type": "step", **step})
                pending_start = pending_end = None
                return
            start = pending_start
            if start is None:  # legacy log (framing not yet persisted): synthesize
                start = {"cwd": "", "command": step.get("command", "")}
            steps.append({"type": "command_start", **start})
            # The tool step's output carries run_command's trailing
            # "[exit code: N]" marker; the live stream never does (the code
            # arrives via command_end), so strip it for a byte-identical panel.
            output = _EXIT_MARKER_RE.sub("", step.get("output") or "")
            if output:
                steps.append({"type": "stream", "text": output})
            if pending_end is not None:
                steps.append({"type": "command_end", **pending_end})
            else:  # legacy: best-effort exit from the step's ok flag
                steps.append({"type": "command_end", "status": "exit",
                              "exit_code": 0 if step.get("ok") else 1})
            steps.append({"type": "step", **step})
            pending_start = pending_end = None

        def emit_bang(command: str, content: str) -> None:
            """Rebuild a user ! command as its live event sequence: the typed
            `!command`, its terminal block (framing + streamed output), and an
            empty `done`. A ! command runs no model turn, so it leaves no `tool`
            step — its framing would otherwise orphan and corrupt the next
            command's block, and its "[I ran … myself]" annotation would show as
            a bare user bubble. Framing is present for logs written since ! gained
            command_start/end; older logs synthesize a best-effort block."""
            nonlocal pending_start, pending_end
            start = pending_start or {"cwd": "", "command": command}
            # user=True so cold replay renders it inline (transcript), not in a
            # trace — matching the live path; older logs lack it in the record.
            steps.append({"type": "command_start", **start, "user": True})
            _, _, output = content.partition("]\n")  # drop the annotation prefix
            output = _EXIT_MARKER_RE.sub("", output)
            if output:
                steps.append({"type": "stream", "text": output})
            end = pending_end or {"status": "exit", "exit_code": 0}
            steps.append({"type": "command_end", **end})
            pending_start = pending_end = None

        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            record = _record_or_none(line)
            if record is None:
                continue
            kind = record.get("kind")
            if kind == REDACT_KIND:
                # A turn the user removed (#202). It sits where the turn used to
                # be, so it closes the previous turn like a user message does and
                # renders in its place — a chat must not silently lose an
                # exchange, or the answer above it reads as a reply to nothing.
                has_trace = True
                flush()
                # Undated on purpose: the marker renders without a time (see
                # app.js [REDACT] — a stamp there would read as "deleted at",
                # which is not what the only available time means), and the
                # record's own `at` stays on disk as the audit trail rather
                # than riding an event nothing reads.
                events.append({"type": "redacted"})
            elif kind == "cwd":
                # Workspace marker (issue #94): identical to the live `workspace`
                # event on_state emits. Buffered with the open turn's steps so it
                # keeps timeline order (a mid-task trust falls between traces);
                # between tasks it goes straight to the event stream. A workspace
                # record is a modern log, so it counts as a reconstructable
                # trace — a /cd-only session must not fall back to a flat blob.
                has_trace = True
                ev = {"type": "workspace", "change": "cwd", "path": record.get("cwd", "")}
                (steps if open_turn else events).append(ev)
            elif kind == "trust_dir":
                has_trace = True
                ev = {"type": "workspace", "change": "trust", "path": record.get("path", "")}
                (steps if open_turn else events).append(ev)
            elif kind == "rating":
                # Replayed so a reopened chat shows what he already rated —
                # otherwise he re-rates it, or assumes the tap was lost.
                # DEFERRED to the end of the stream, not appended here: a
                # rating DECORATES a turn rather than being one, it is keyed by
                # turn id rather than by position, and it can be written long
                # after the turn it names (he rates when he notices). Replaying
                # it in file order would hand the frontend a decoration for a
                # turn it has not rendered yet.
                ratings.append({
                    "type": "rating",
                    "turn": record.get("turn", ""),
                    "rating": record.get("rating", ""),
                    "comment": record.get("comment", ""),
                })
            elif kind == "origin":
                origin = record.get("origin") or origin
            elif kind == "task_end" and record.get("status") not in (None, "ok"):
                # How the turn ended (#203). Held until `flush`, which is where
                # a turn's closing event is decided — a task_end arrives after
                # the steps it closes, and before the next turn opens.
                failure = record.get("error") or INTERRUPTED_TASK
            elif kind == "cmd_start":
                pending_start = {k: v for k, v in record.items() if k not in ("kind", "ts")}
            elif kind == "cmd_end":
                pending_end = {k: v for k, v in record.items() if k not in ("kind", "ts")}
            elif kind == "trace":
                has_trace = True
                step = record.get("step", {})
                sk = step.get("kind")
                if sk in ("thinking_start", "tool_start"):
                    running_steps += 1
                elif sk in ("thinking", "thinking_cancel", "tool"):
                    running_steps = max(0, running_steps - 1)
                if sk in RENDERLESS_STEPS:
                    # Diagnostic / governance evidence, not transcript content:
                    # the browser telling the server it could not display
                    # something (#188), or a gate recording why it refused
                    # (#191-#194). Live these render nothing at all — they are
                    # emitted through _emit_record, which never reaches on_step
                    # — so skipping them here IS the hot/cold parity, exactly as
                    # for aish's own `[aish: …]` notes below. `has_trace` is
                    # already set above, so a log holding ONLY governance
                    # records still reconstructs rather than falling back to a
                    # flat history blob.
                    continue
                if sk == "tool" and step.get("name") == "run_command":
                    emit_command(step)
                else:
                    steps.append({"type": "step", **step})
            elif kind == "message" and record.get("role") == "user":
                content = record.get("content", "")
                synthetic = synthetic_kind(content)
                if synthetic == "note":
                    # aish's own annotation — live it never reached the transcript
                    # at all, and treating it as a user message would also split
                    # the turn it sits inside. Skipping it IS the parity (#171).
                    continue
                flush()  # close the previous turn before the next one opens
                if not synthetic and origin != "user" and first_user:
                    # A triggered session's opening turn is the trigger's own
                    # prompt (#160): arbitrary text with no marker to match, so
                    # position + provenance is what identifies it — the same
                    # message handle_trigger marks live.
                    synthetic = "trigger"
                first_user = False
                bang = _BANG_RE.match(content)
                if bang:  # a ! command: replay it as its terminal block, not a bubble
                    events.append({"type": "user", "text": "!" + bang.group(1)})
                    # Framing present means a modern ! log (post command_start/end):
                    # it reconstructs faithfully, so a session of only ! commands
                    # must not fall back to a flat history blob.
                    if pending_start is not None:
                        has_trace = True
                    open_turn = True
                    emit_bang(bang.group(1), content)
                    flush()  # a ! command is its own closed turn (no model answer)
                else:
                    event = {"type": "user", "text": content}
                    if synthetic:
                        event["synthetic"] = synthetic
                    # What a per-turn "remove" action names (#202). Carried on
                    # every replayed turn, so the control exists on a chat that
                    # was reopened cold — which is most of them.
                    event["turn"] = _turn_id(record, index)
                    # WHICH references in this turn name a real file (#233) —
                    # the same fact the live event carries, re-derived here so a
                    # chat reopened cold draws the identical bubble. A reader
                    # cannot work this out from the text: only the disk knows.
                    files = files_named(content, uploads)
                    if files is not None:
                        event["files"] = files
                    # WHEN this turn happened (#200), deliberately under `at`
                    # and not `ts`: on a live event `ts` means "this turn is
                    # starting now" and drives the trace card's clock, and cold
                    # replay must never look like a running turn. Two names,
                    # two meanings, no entanglement.
                    at = record_epoch(record)
                    if at:
                        event["at"] = at
                    events.append(event)
                    open_turn = True
            elif kind == "message" and record.get("role") == "assistant":
                # Every non-empty assistant text is a DELIVERY (#212): the
                # prose a step said alongside its tool calls was already shown
                # to the owner live, so a replay that collapsed the turn to its
                # last message rendered a chat he had watched say four things
                # as one that said one. Buffered in position; `flush` decides
                # which of them is the answer.
                content = (record.get("content") or "").strip()
                if content:
                    deliveries.append(len(steps))
                    delivery_ids.append(_turn_id(record, index))
                    steps.append({"type": "token", "text": content})
                    steps.append({"type": "delivery", "text": content})
        flush()
        # Decorations last: every turn they name now exists in the stream.
        events.extend(ratings)
        return events if has_trace else None

    @staticmethod
    def truncate_at_answer(text: str, after: int) -> str | None:
        """Return the log truncated to include everything up to AND INCLUDING
        the `after`-th (1-based) final answer — used to fork a conversation
        "from here". A final answer is an assistant `message` record not
        immediately followed by a `tool` message (same rule the exporter and
        the UI use), so intermediate tool-calling turns don't count. All the
        turn's non-message records (model/trace/cmd framing) precede its
        assistant message, so cutting after that line preserves them and the
        fork replays identically up to that point. Returns None if `after` is
        out of range."""
        lines = text.splitlines()
        # (line index, role, interim) for message records. `interim` marks a
        # DELIVERY — something said on the way to the answer (#212) — and is
        # checked before the adjacency rule below, because that rule cannot see
        # a turn which logged no tool message. Every claude-max turn is one:
        # the SDK's tool calls leave trace steps, not tool-role records, so
        # without the stamp each narration line counted as a final answer and
        # the fork ordinal drifted off by one per narrated turn.
        msgs: list[tuple[int, str, bool]] = []
        for i, line in enumerate(lines):
            record = _record_or_none(line)
            if record is None:
                continue
            if record.get("kind") == "message" and record.get("role") in (
                "user", "assistant", "tool",
            ):
                msgs.append((i, record["role"], bool(record.get("interim"))))
        finals = [
            line_idx
            for j, (line_idx, role, interim) in enumerate(msgs)
            if role == "assistant"
            and not interim
            and (j + 1 >= len(msgs) or msgs[j + 1][1] != "tool")
        ]
        if after < 1 or after > len(finals):
            return None
        return SessionLog._cut_after_answer(lines, finals[after - 1])

    @staticmethod
    def truncate_at_answer_id(text: str, answer: str) -> str | None:
        """Return the log truncated to include everything up to AND INCLUDING
        the assistant record NAMED `answer` — the fork point as an identity
        rather than a position (#229).

        This is the whole reason forks stopped landing on the wrong answer.
        `truncate_at_answer` above takes an ordinal, and the only party who can
        supply one is the browser, counting answers as it renders them — so a
        transcript trimmed to its last N events (#228) made the browser's third
        answer the log's seventeenth, and the fork branched fourteen answers too
        early with nothing anywhere reporting a mismatch. An id cannot be
        counted wrong: it either names a record in this log or it names nothing,
        and naming nothing is a refusal the owner sees.

        Returns None when no assistant record answers to that name."""
        lines = text.splitlines()
        for i, line in enumerate(lines):
            record = _record_or_none(line)
            if record is None:
                continue
            if (
                record.get("kind") == "message"
                and record.get("role") == "assistant"
                and _turn_id(record, i) == answer
            ):
                return SessionLog._cut_after_answer(lines, i)
        return None

    @staticmethod
    def _cut_after_answer(lines: list[str], cut: int) -> str:
        """The log through line `cut` (an assistant answer), plus the rest of
        that turn's trailing records (thinking_cancel, model, framing …) up to
        the NEXT user message — so the fork replays the complete turn instead of
        orphaning a trace record logged after the answer, which would otherwise
        reconstruct as a dangling step."""
        end = len(lines)
        for i in range(cut + 1, len(lines)):
            record = _record_or_none(lines[i])
            if record is None:
                continue
            if record.get("kind") == "message" and record.get("role") == "user":
                end = i
                break
        return "\n".join(lines[:end]) + "\n"

    def rewind_last_turn(self) -> bool:
        """Drop the most recent user turn from the log in place: the last
        `message`/`user` record and every record after it (assistant/tool
        messages, traces, command framing). Web retry (#60) re-runs the prompt,
        which re-logs it and the fresh answer, so a cold replay shows one turn,
        not the discarded attempt. Append-only otherwise, so this is the one
        rewrite: the handle is closed and reopened lazily on the next record.
        Returns False when there is no file yet or no user turn to drop."""
        with self._write_lock:
            if not self.path.exists():
                return False
            lines = self.path.read_text(encoding="utf-8").splitlines()
            cut: int | None = None
            for i, line in enumerate(lines):
                record = _record_or_none(line)
                if record is None:
                    continue
                if record.get("kind") == "message" and record.get("role") == "user":
                    cut = i
            if cut is None:
                return False
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            kept = lines[:cut]
            self.path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
            return True

    def redact_turn(self, turn: str) -> "Redaction | None":
        """Take a turn out of the log for good (#202): its user message, every
        record it produced, and the `task_start` that carries the prompt
        verbatim. A tombstone replaces them AT THEIR POSITION — dated, naming
        the turn, counting what went — so the removal is itself auditable while
        the text is gone from disk.

        The range is [the turn's task_start, the next user message). Walking
        back over the `task_start` matters: it is written before the user
        message and holds the prompt in full, so cutting from the message alone
        would leave a copy of exactly what was being removed — and it would
        strand an unmatched task_start, which restart recovery reads as an
        interrupted task to resume. The knowledge/model records that can also
        precede a user message carry no user text, so a log with no task_start
        (every CLI session) simply cuts from the message and leaves them.

        Returns what was removed, or None when no such turn is in this log —
        an id that named a turn someone else already removed, or a client
        holding a stale line-index id. Idempotent by that miss.

        This is the second in-place rewrite of an otherwise append-only file
        (rewind_last_turn is the first): the handle is closed here and reopened
        lazily on the next record.
        """
        with self._write_lock:
            if not self.path.exists():
                return None
            lines = self.path.read_text(encoding="utf-8").splitlines()
            records: list[dict] = []
            for line in lines:
                # This site alone already guarded the non-dict case; the other
                # nine did not, which is why it is one helper now.
                records.append(_record_or_none(line) or {})

            def is_user(record: dict) -> bool:
                return record.get("kind") == "message" and record.get("role") == "user"

            start = next(
                (i for i, rec in enumerate(records) if is_user(rec) and _turn_id(rec, i) == turn),
                None,
            )
            if start is None:
                return None
            text = records[start].get("content") or ""
            # Which of the identically-worded user turns this is. The live
            # Agent's message list holds no ids (they are a property of the log,
            # and its dicts go straight to the backends), so the caller finds the
            # same turn there by text — and two turns saying "ok" would otherwise
            # be indistinguishable, dropping the wrong one from the model's
            # context, which is the one thing this feature must not do.
            occurrence = sum(
                1
                for i, rec in enumerate(records[: start + 1])
                if is_user(rec) and (rec.get("content") or "") == text
            )
            def turn_opens_at(user_index: int) -> int:
                """Where a turn's records really begin. A turn does not start at
                its user message: `task_start` is written first and carries the
                prompt VERBATIM, with the knowledge/model preamble in between.
                Used at BOTH ends — cutting from the message alone would leave a
                copy of exactly what was removed, and cutting up to the NEXT
                message would take that turn's task_start with it."""
                for i in range(user_index - 1, -1, -1):
                    kind = records[i].get("kind")
                    if kind in ("trace", "model"):
                        continue  # this turn's own preamble, or the last one's tail
                    return i if kind == "task_start" else user_index
                return user_index

            first = turn_opens_at(start)
            next_user = next(
                (i for i in range(start + 1, len(records)) if is_user(records[i])),
                None,
            )
            end = len(records) if next_user is None else turn_opens_at(next_user)

            tombstone = {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "kind": REDACT_KIND,
                "turn": turn,
                # When the removed turn happened, so the marker can sit in the
                # transcript's own timeline rather than claiming to be new.
                "at": records[start].get("ts", ""),
                "records": end - first,
            }
            kept_records = records[:first] + [tombstone] + records[end:]
            kept_lines = (
                lines[:first]
                + [json.dumps(tombstone, ensure_ascii=False)]
                + lines[end:]
            )
            title = self._retitle_after_redaction(kept_records)
            if title is not None:
                # The stale auto titles are DELETED, not merely superseded: a
                # model-written name is a summary of the conversation, so one
                # written while the removed turn was in it can quote the very
                # text being removed — and a later record winning the parse
                # leaves the earlier one's words sitting in the file. Titles the
                # user typed are their own words and are kept.
                kept_lines = [
                    line
                    for line, record in zip(kept_lines, kept_records, strict=True)
                    if not (record.get("kind") == "title" and record.get("auto"))
                ]
                stamped = {
                    "ts": tombstone["ts"],
                    "kind": "title",
                    "title": title,
                    "auto": True,
                }
                kept_lines.append(json.dumps(stamped, ensure_ascii=False))
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            self.path.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
            return Redaction(
                turn=turn,
                text=text if isinstance(text, str) else "",
                occurrence=occurrence,
                records=int(tombstone["records"]),
                title=title,
            )

    @staticmethod
    def _retitle_after_redaction(kept: list[dict]) -> str | None:
        """The chat's new name once a turn is gone, or None to leave it alone.

        An AUTO title is a description of content, so when the content goes the
        description has to be re-derived — otherwise a model-written summary of
        the removed exchange stays on the row, which for the pasted-secret case
        is the leak walking out through the sessions list. A title the user
        TYPED is theirs and is never touched (the same rule the auto-titler
        already follows); nor is a derived title, which re-derives itself.
        """
        stored: str | None = None
        auto = False
        messages: list[dict] = []
        for record in kept:
            if record.get("kind") == "title":
                title = (record.get("title") or "").strip()
                if title:
                    stored, auto = title, bool(record.get("auto"))
            elif record.get("kind") == "message" and record.get("role") != "system":
                messages.append(record)
        if stored is None or not auto:
            return None
        derived = SessionLog._derive_title(messages)
        if derived == _NO_USER_INPUT or derived == stored:
            return None
        return SessionLog._truncate_title(derived)

    @staticmethod
    def _derive_title(messages: list[dict]) -> str:
        """Untruncated title: the first user message — cheap, deterministic,
        and it almost always names the task. Notes aish wrote itself are not
        the user's words and say nothing about the chat, so a session opened
        with a `/cd` isn't titled with the announcement it produced (#171)."""
        for message in messages:
            if message.get("role") != "user":
                continue
            raw = message.get("content") or ""
            # Notes out BEFORE the whitespace flatten, or the line boundary they
            # are identified by is gone.
            content = " ".join(strip_attachment_notes(raw).split())
            if not content:
                # A photo sent with nothing typed is still about something.
                names = attachment_names(raw)
                if names:
                    return ", ".join(names)
                continue
            if synthetic_kind(content) == "note":
                continue
            bang = _BANG_RE.match(content)
            return f"! {bang.group(1)}" if bang else content
        return _NO_USER_INPUT

    @staticmethod
    def _derive_snippet(messages: list[dict]) -> str:
        """Preview line: the last thing SAID in the chat, prefixed with who said
        it. What counts as said is `visible_messages` — the same rule the search
        index reads, so the preview can never advertise something the search
        cannot find, or the other way round."""
        for role, content in reversed(visible_messages(messages)):
            bang = _BANG_RE.match(content)
            if bang:
                content = f"! {bang.group(1)}"
            elif role == "user":
                content = f"You: {content}"
            if len(content) > SNIPPET_MAX:
                content = content[: SNIPPET_MAX - 1] + "…"
            return content
        return ""

    @staticmethod
    def _truncate_title(title: str) -> str:
        if len(title) > TITLE_MAX:
            return title[: TITLE_MAX - 1] + "…"
        return title

    @staticmethod
    def _peek(path: Path) -> tuple[str | None, str, int | None, int | None]:
        """(title, origin, activity_ts) per session for the drawer/pager. A
        custom title (latest `kind:"title"` record) wins; else the first user
        message. A None title = no user input yet and never renamed (an empty
        chat). Served from the parse cache, so an unchanged log costs one stat —
        which is why the activity stamp rides along here rather than being
        stat'd separately: it is already parsed."""
        try:
            parsed = SessionLog._cached_parse(path)
        except OSError:
            return None, "user", None, None
        title = parsed.title
        if title is None:
            derived = SessionLog._derive_title(parsed.messages)
            title = None if derived == _NO_USER_INPUT else derived
        return (
            SessionLog._truncate_title(title) if title is not None else None,
            parsed.origin,
            parsed.activity_ts,
            parsed.output_ts,
        )

    @staticmethod
    def _peek_title(path: Path) -> str | None:
        return SessionLog._peek(path)[0]

    @staticmethod
    def _by_recency(state_dir: Path) -> list[Path]:
        """Session files newest-first by last interaction (file mtime) — the
        one ordering every session list shares (drawer, CLI picker, swipe
        pager), so moving through any of them means the same thing."""
        stamped = []
        for path in state_dir.glob("session-*.jsonl"):
            try:
                stamped.append((path.stat().st_mtime, path))
            except OSError:
                continue
        stamped.sort(reverse=True)
        paths = [path for _, path in stamped]
        # Deleted sessions leave the parse caches with the same sweep that
        # notices them gone. Keys from OTHER state dirs (tests use several)
        # are not this dir's to prune.
        live = {str(path) for path in paths}
        for cache in (_PARSE_CACHE, _ENTRY_CACHE):
            for spath in [
                s for s in cache if s not in live and Path(s).parent == state_dir
            ]:
                cache.pop(spath, None)
        return paths

    @staticmethod
    def pager_titles(
        state_dir: Path, limit: int = 30
    ) -> list[tuple[str, str, str, float, float]]:
        """(name, title, origin, ts) pages for the web UI's swipe pager: the
        `limit` most recent chats that have a title, same recency ordering as the
        rail, flipped oldest→newest. Chats with no user input yet are not rows —
        the cap applies after skipping them, so blank files can never crowd real
        chats out. The origin rides along so a row can show its provenance.

        Carried on every hello, which is what lets the client warm the chats a
        switch is most likely to land on without first opening the rail. `ts` is
        the last ACTIVITY, the same stamp `list_sessions` reports (#201) — the
        deck ages its members on it, and a chat must not look freshly used
        because someone glanced at it. `out` is the last OUTPUT (#203), which is
        what unread compares against; 0.0 when there is none to report."""
        pages = []
        for path in SessionLog._by_recency(state_dir):
            title, origin, activity_ts, output_ts = SessionLog._peek(path)
            if title is not None:
                try:
                    ts = float(activity_ts) if activity_ts else path.stat().st_mtime
                except OSError:  # vanished between the scan and here
                    continue
                pages.append((
                    path.name, title, origin, ts,
                    float(output_ts) if output_ts else 0.0,
                ))
                if len(pages) == limit:
                    break
        pages.reverse()
        return pages

    @staticmethod
    def _started_at(path: Path) -> datetime.datetime:
        try:  # session-YYYYmmdd-HHMMSS[-ffffff].jsonl
            _, day, clock = path.stem.split("-")[:3]
            return datetime.datetime.strptime(f"{day}-{clock}", "%Y%m%d-%H%M%S")
        except ValueError:
            return datetime.datetime.fromtimestamp(path.stat().st_mtime)

    @staticmethod
    def _info_from(
        path: Path,
        messages: list[dict],
        model: str = "",
        custom_title: str | None = None,
        origin: str = "user",
        cwd: str = "",
        activity_ts: int | None = None,
        output_ts: int | None = None,
    ) -> SessionInfo:
        title = custom_title if custom_title else SessionLog._derive_title(messages)
        title = SessionLog._truncate_title(title)
        when = SessionLog._started_at(path).strftime("%Y-%m-%d %H:%M")
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return SessionInfo(
            path=path,
            when=when,
            count=len(messages),
            title=title,
            model=model,
            snippet=SessionLog._derive_snippet(messages),
            mtime=mtime,
            # A log whose records all predate timestamps, or that holds nothing
            # but renderless ones, still has to sort somewhere: mtime is the
            # old answer and remains the honest fallback.
            activity=float(activity_ts) if activity_ts else mtime,
            # NO mtime fallback, deliberately: "we could not tell when this
            # chat last spoke" must not become "it spoke just now", which is
            # what an mtime would claim on every log the process touches. A
            # zero here reads downstream as "fall back to the activity stamp",
            # so a log too old to carry usable stamps behaves exactly as it
            # did before this split existed.
            output=float(output_ts) if output_ts else 0.0,
            origin=origin,
            cwd=cwd,
        )

    @staticmethod
    def info(path: Path) -> SessionInfo | None:
        """Summary line for a session picker; None for empty sessions."""
        parsed = SessionLog._cached_parse(path)
        if not parsed.messages:
            return None
        return SessionLog._info_from(
            path, parsed.messages, parsed.model, parsed.title, parsed.origin,
            parsed.cwd, parsed.activity_ts, parsed.output_ts,
        )

    @staticmethod
    def list_sessions(state_dir: Path, exclude: set | None = None) -> list[SessionInfo]:
        """Non-empty sessions by last interaction, newest first, minus
        excluded paths."""
        exclude = exclude or set()
        infos = []
        for path in SessionLog._by_recency(state_dir):
            if path in exclude:
                continue
            info = SessionLog.info(path)
            if info:
                infos.append(info)
        return infos

    @staticmethod
    def _cached_entry(path: Path) -> "SessionEntry | None":
        """A session's SessionEntry (None for empty sessions), cached by the
        same stat key as the parse — building the search vocabulary casefolds
        every message, which is the expensive half of `load_entries`."""
        key = None
        try:
            stat = path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass
        spath = str(path)
        if key is not None:
            hit = _ENTRY_CACHE.get(spath)
            if hit is not None and hit[0] == key:
                return hit[1]
        parsed = SessionLog._cached_parse(path)
        entry: SessionEntry | None = None
        if parsed.messages:
            messages, model = parsed.messages, parsed.model
            # WHAT THE CHAT SHOWS, not what the log holds (#266) — see
            # `visible_messages`. Tool output is most of the bytes in a busy
            # session and none of what a person searches for.
            content = TURN_SEP.join(text for _, text in visible_messages(messages))
            content_cf = content.casefold()
            model_cf = model.casefold()
            # Model tokens ("gemini", "2.5", "pro") join the fuzzy vocabulary
            # so a typo like "gemni" still filters by model.
            model_words = frozenset(re.split(r"[^a-z0-9.]+", model_cf)) - {""}
            # A renamed chat is findable by its custom name, so the searchable
            # title is the effective one (custom overrides the derived).
            title_cf = (parsed.title or SessionLog._derive_title(messages)).casefold()
            entry = SessionEntry(
                info=SessionLog._info_from(
                    path, messages, model, parsed.title, parsed.origin,
                    parsed.cwd, parsed.activity_ts, parsed.output_ts,
                ),
                title_cf=title_cf,
                content_cf=content_cf,
                content=content,
                words=(
                    frozenset(w.strip(_PUNCT) for w in content_cf.split()) - {""}
                ) | model_words,
                model_cf=model_cf,
            )
        if key is not None:
            _ENTRY_CACHE[spath] = (key, entry)
        return entry

    @staticmethod
    def load_entries(state_dir: Path, exclude: set | None = None) -> list["SessionEntry"]:
        """Searchable sessions by last interaction, newest first, read from
        disk once — so a live picker can re-rank on every keystroke without
        touching files."""
        exclude = exclude or set()
        entries = []
        for path in SessionLog._by_recency(state_dir):
            if path in exclude:
                continue
            entry = SessionLog._cached_entry(path)
            if entry is not None:
                entries.append(entry)
        return entries

    @staticmethod
    def user_command_history(state_dir: Path, limit: int = USER_HISTORY_MAX) -> list[str]:
        """The user's OWN successfully-run commands, verbatim, aggregated across
        sessions and ranked most-run first then most-recent — the source for the
        web terminal-mode autocomplete (#104).

        Only `decision:"user-direct"` command records count (the ! path), never
        the model's tool-loop commands — those carry approval decisions, so the
        AI's activity can't pollute the user's palette. A command qualifies only
        when the terminal-block `cmd_end` that follows it reports a clean exit
        (status "exit", exit_code 0); failures and `!cd` (which emits no cmd_end)
        drop out. Strings are kept exactly as typed so aliases like `ll` are
        suggested verbatim; case-insensitive prefix matching happens at the
        callsite (the frontend), not here."""
        counts: dict[str, int] = {}
        last_seen: dict[str, int] = {}
        order = 0  # monotonic across the whole scan; higher = more recent run
        # Oldest→newest over recent sessions so a later run of the same command
        # wins the recency tie-break. The command/exit pairing happens in
        # `_parse` (ParsedLog.user_cmds), so an unchanged log costs one stat.
        recent = SessionLog._by_recency(state_dir)[:USER_HISTORY_SESSIONS]
        for path in reversed(recent):
            try:
                cmds = SessionLog._cached_parse(path).user_cmds
            except OSError:
                continue
            for cmd in cmds:
                counts[cmd] = counts.get(cmd, 0) + 1
                last_seen[cmd] = order
                order += 1
        ranked = sorted(
            counts, key=lambda cmd: (counts[cmd], last_seen[cmd]), reverse=True
        )
        return ranked[:limit]

    @staticmethod
    def rank(entries: list["SessionEntry"], query: str) -> list[SessionInfo]:
        """The ranked sessions alone, for callers with nowhere to say how they
        were found (the CLI picker, the model-facing search)."""
        return SessionLog.ranked(entries, query).sessions

    @staticmethod
    def ranked(entries: list["SessionEntry"], query: str) -> Ranked:
        """Deterministic ranking over titles, model names and the visible
        conversation — no LLM. Tiers: exact title, phrase in title or model,
        phrase in contents, all words in contents/model. Ties keep newest-first
        order; an empty query keeps everything, newest first.

        A LITERAL SEARCH IS NEVER DILUTED (#266). Approximate matching is a
        separate answer to a different question — "nothing you typed is in any
        chat; here are the closest" — so it runs only when the tiers above found
        nothing at all (`_closest`). Mixed in, it buried three real hits for
        "tefal" under fifty chats whose vocabulary merely contained "tel".

        It is also the fast path: a query that matches anything costs no difflib
        at all, and this runs over every session on every keystroke."""
        query_cf = " ".join(query.split()).casefold()
        words = query_cf.split()
        if not words:
            return Ranked([entry.info for entry in entries], False)
        ranked = []
        for entry in entries:
            if entry.title_cf == query_cf:
                score = 5
            elif query_cf in entry.title_cf or query_cf in entry.model_cf:
                score = 4
            elif query_cf in entry.content_cf:
                score = 3
            elif all(word in entry.content_cf or word in entry.model_cf for word in words):
                score = 2
            else:
                continue
            ranked.append((score, SessionLog._match_row(entry, words)))
        if not ranked:
            return Ranked(SessionLog._closest(entries, query_cf, words), True)
        ranked.sort(key=lambda pair: -pair[0])  # stable: newest first within a tier
        return Ranked([info for _, info in ranked], False)

    @staticmethod
    def _match_row(entry: "SessionEntry", words: list[str]) -> SessionInfo:
        """A result row: the session, with its preview line replaced by the line
        the match is ON.

        A row that answers a search with the chat's LAST message says nothing
        about why that chat is in the list, which on screen is indistinguishable
        from the search being wrong — and it was the other half of what made a
        polluted result set unreadable (#266). A match in the title or the model
        name has nothing to quote, so those keep the preview.

        `replace` and not assignment: the info belongs to a CACHED entry, and
        writing a query's answer onto it would leave the last search's excerpt
        on the row long after the search was cleared."""
        # QUOTE ONLY WHAT THE ROW IS NOT ALREADY SHOWING. A hit inside the chat's
        # own name is on screen already, so when the name is the opening of the
        # conversation (the derived title, i.e. most chats) only a hit PAST it is
        # worth a line; if that is the sole mention, the preview stays and the
        # row says what happened next instead of the same sentence twice.
        title = entry.info.title.rstrip("…")
        content = entry.content
        if title and content.startswith(title):
            content = content[len(title):].removeprefix(TURN_SEP)
        line = SessionLog._snippet(content, words, width=SNIPPET_MAX)
        return entry.info if line is None else replace(entry.info, snippet=line)

    @staticmethod
    def _closest(
        entries: list["SessionEntry"], query_cf: str, words: list[str]
    ) -> list[SessionInfo]:
        """The typo fallback: the chats nearest to a query nothing matched.

        Ordered by how close the match actually is (the weakest word decides,
        since every word has to land) and capped at `CLOSEST_MAX`, because
        "close enough" over an archive-sized vocabulary has no natural end — with
        the tail left in, the one chat the owner meant sat somewhere inside sixty
        rows of coincidence."""
        scored = []
        for entry in entries:
            ratio = SessionLog._closeness(entry, query_cf, words)
            if ratio is not None:
                scored.append((ratio, entry.info))
        scored.sort(key=lambda pair: -pair[0])  # stable: newest first at equal closeness
        return [info for _, info in scored[:CLOSEST_MAX]]

    @staticmethod
    def _closeness(
        entry: "SessionEntry", query_cf: str, words: list[str]
    ) -> float | None:
        """How near this session is to a query it does not contain, or None for
        "not near at all". Every query word must have a length-compatible near
        word in the session (`FUZZY_LEN_SLACK`), or the whole query must read
        like the title."""
        ratios = []
        for word in words:
            candidates = [
                candidate
                for candidate in entry.words
                if abs(len(candidate) - len(word)) <= FUZZY_LEN_SLACK
            ]
            close = difflib.get_close_matches(
                word, candidates, n=1, cutoff=FUZZY_WORD_CUTOFF
            )
            if not close:
                break
            ratios.append(difflib.SequenceMatcher(None, word, close[0]).ratio())
        else:
            return min(ratios) if ratios else None
        title = difflib.SequenceMatcher(None, query_cf, entry.title_cf).ratio()
        return title if title >= FUZZY_THRESHOLD else None

    @staticmethod
    def search_sessions(
        state_dir: Path, query: str, exclude: set | None = None
    ) -> list[SessionInfo]:
        """One-shot ranked search; empty queries match nothing."""
        if not query.split():
            return []
        return SessionLog.rank(SessionLog.load_entries(state_dir, exclude), query)

    @staticmethod
    def _snippet(content: str, words: list[str], width: int = SNIPPET_CHARS) -> str | None:
        """One flattened line of context around the first query-word hit."""
        flat = " ".join(content.split())
        flat_cf = flat.casefold()
        pos = min((p for w in words if (p := flat_cf.find(w)) >= 0), default=-1)
        if pos < 0:
            return None
        # Start close to the hit, and never mid-word: the excerpt has one
        # truncated line to make its case, and it makes it with the match.
        start = max(0, pos - SNIPPET_LEAD)
        if start:
            space = flat.find(" ", start)
            if 0 <= space < pos:
                start = space + 1
        end = min(len(flat), start + width)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(flat) else ""
        return f"{prefix}{flat[start:end]}{suffix}"

    @staticmethod
    def search_excerpts(
        state_dir: Path, query: str, session: str | None = None, exclude: set | None = None
    ) -> str:
        """Model-facing session search (the search_sessions tool).

        Without `session`: ranked sessions with excerpt lines around the
        matches — enough to pick the right one. With `session`: that file's
        matching messages (or its tail when the query is empty), trimmed and
        capped so the result always fits a small context window.
        """
        words = query.casefold().split()
        if session is not None:
            return SessionLog._session_detail(state_dir, session, query, words)
        if not words:
            return (
                "ERROR: search_sessions needs a query (or a session file name "
                "from an earlier result)."
            )
        infos = SessionLog.search_sessions(state_dir, query, exclude=exclude)
        if not infos:
            return f"No past session matches {query!r}."
        lines = [f"{len(infos)} past session(s) match {query!r} (best matches first):"]
        for info in infos[:SEARCH_TOP]:
            model = f" · {info.model}" if info.model else ""
            lines.append(f"\n== {info.path.name} · {info.when} · {info.count} msgs{model}")
            lines.append(f"   title: {info.title}")
            shown = 0
            for message in SessionLog.load_messages(info.path):
                snippet = SessionLog._snippet(message.get("content") or "", words)
                if snippet is None:
                    continue
                lines.append(f"   [{message.get('role', '?')}] {snippet}")
                shown += 1
                if shown >= SNIPPETS_PER_SESSION:
                    break
        if len(infos) > SEARCH_TOP:
            lines.append(f"\n(…and {len(infos) - SEARCH_TOP} more, weaker matches)")
        lines.append(
            '\nCall search_sessions again with session="<file name>" for the full '
            "matching messages from one session."
        )
        return "\n".join(lines)

    @staticmethod
    def recall_sessions(state_dir: Path, query: str, exclude: set | None = None) -> str:
        """Compact sessions section for the recall tool: top matches with a
        title line and one snippet each, or "" when nothing matches — the
        episodic fallback below skills/memory results."""
        words = query.casefold().split()
        if not words:
            return ""
        infos = SessionLog.search_sessions(state_dir, query, exclude=exclude)
        lines = []
        for info in infos[:RECALL_SESSIONS_TOP]:
            lines.append(f"- {info.path.name} · {info.when} · {info.title}")
            for message in SessionLog.load_messages(info.path):
                snippet = SessionLog._snippet(message.get("content") or "", words)
                if snippet is not None:
                    lines.append(f"    [{message.get('role', '?')}] {snippet}")
                    break
        return "\n".join(lines)

    @staticmethod
    def _session_detail(state_dir: Path, session: str, query: str, words: list[str]) -> str:
        if not _SESSION_NAME_RE.match(session):
            return (
                f"ERROR: {session!r} is not a session file name — use a name "
                "returned by search_sessions, like 'session-20260718-213000-000000.jsonl'."
            )
        path = state_dir / session
        if not path.is_file():
            return f"ERROR: no such session: {session}. Search first to find valid names."
        messages = SessionLog.load_messages(path)
        matching = [
            m for m in messages
            if any(w in (m.get("content") or "").casefold() for w in words)
        ]
        if words and matching:
            header = f"Messages matching {query!r} in {session}:"
            picked = matching
        else:
            note = f"no message matches {query!r}; " if words else ""
            header = f"{session}: {note}showing the most recent messages:"
            picked = messages[-DETAIL_TAIL_MESSAGES:]
        lines = [header]
        used = len(header)
        for i, message in enumerate(picked):
            content = (message.get("content") or "").strip()
            snippet = SessionLog._snippet(content, words, width=DETAIL_MESSAGE_CHARS)
            body = snippet if words and snippet else content[:DETAIL_MESSAGE_CHARS]
            entry = f"\n[{message.get('role', '?')}] {body}"
            if used + len(entry) > DETAIL_MAX_CHARS:
                lines.append(
                    f"\n[… {len(picked) - i} more messages omitted — refine the query]"
                )
                break
            lines.append(entry)
            used += len(entry)
        return "\n".join(lines)

    def _record(self, kind: str, **fields) -> None:
        with self._write_lock:
            if self._pending_model is not None and kind != "model":
                pending, self._pending_model = self._pending_model, None
                self._write_line("model", model=pending)
            self._write_line(kind, **fields)

    def _write_line(self, kind: str, **fields) -> None:
        """Append one record. Caller must hold _write_lock."""
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            **fields,
        }
        if SessionLog._is_output(record):
            self.output_at = record_epoch(record) or self.output_at
        if self._fh is None:
            # Created on first record, not in __init__: a chat that never
            # gets a message must leave no file — empty session files crowd
            # every recency-ordered list and pile up across restarts.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def message(self, message: dict) -> str:
        # A user record opens a turn, and a turn is the unit a redaction names
        # (#202); an assistant record IS an answer, and an answer is the unit a
        # fork names (#229). Both get a stable id here — at the one place every
        # logged message passes through — and it is returned so the live server
        # can put the id of the answer it just wrote on the event announcing it.
        # `_parse` keeps only the conversation keys, so the id can never leak
        # into a resumed conversation or the model's context.
        if message.get("role") in ("user", "assistant") and not message.get("turn"):
            message = {**message, "turn": uuid.uuid4().hex[:12]}
        self._record("message", **message)
        return str(message.get("turn") or "")

    def model(self, spec: str) -> None:
        """Note the model in use; written lazily, just before the next real
        record, so the last model record is the session's current model — and
        merely opening/resuming a session never touches its file. Session
        order everywhere is file mtime ("last interaction"), so reviewing an
        old session must not hoist it to most-recent; only new activity does."""
        with self._write_lock:
            self._pending_model = spec

    def rating(self, turn: str, rating: str, comment: str = "") -> None:
        """The owner's verdict on one answer (#207) — 👍/👎 with an optional
        reason, keyed by the same turn id a redaction names (#202), so it is
        stable live AND on replay and needs no new identity scheme.

        This is a RECORD, not a judgement: no model reads it, nothing acts on
        it. It exists so two questions have answers instead of impressions —
        "is he still correcting turns that passed every rule they were subject
        to" (the metric that decides whether turn-end verification earns its
        place) and "which rules were governing the answer he disliked". The
        rule records for that turn sit between this turn's `task_start` and
        `task_end`, so the join is a lookup inside a bracket."""
        self._record(
            "rating", turn=turn, rating=rating, comment=comment[:RATING_COMMENT_CAP]
        )

    def command(self, command: str, decision: str, intent: str = "") -> None:
        """`intent` is what the model SAID it was doing when this decision was
        made (#252) — the same text the card showed. Recorded so a stated
        intent that does not match the action it rode on is a queryable
        artifact rather than something reconstructed by reading raw JSONL,
        which is exactly what the incident behind #252 cost. Omitted when
        empty, so a session that predates it replays byte-identically."""
        extra = {"intent": intent} if intent else {}
        self._record("command", command=command, decision=decision, **extra)

    def set_title(self, title: str, auto: bool = False) -> None:
        """Rename the chat with an append-only `kind:"title"` record — no
        rewrite of the log. The latest such record wins on parse/peek; the
        derived first-user-message title is the fallback when none exists.

        `auto` marks a title the model wrote (#175). It is what lets a hand-
        typed rename be permanent: the auto-titler stands down for good once
        the winning record is a manual one."""
        self._record("title", title=title.strip(), auto=auto)

    def origin(self, origin: str) -> None:
        """Record who started this session (schedule | email | webhook — never
        for the default "user", which needs no record). Append-only metadata,
        read back by _parse so a cold-loaded triggered session keeps its
        provenance and the drawer can group it under "Automated" (#160)."""
        self._record("origin", origin=origin)

    def task_start(self, prompt: str) -> None:
        """Mark a model task as IN FLIGHT (#164). Paired with task_end, this is
        the only durable trace that a task was running when the process died:
        the web server resumes any session whose log ends on an unmatched
        task_start. The prompt rides along for the case where the run died
        before the user message itself was logged.

        Written by the WEB server only — a CLI session dies with its terminal
        and must never be resurrected by an unrelated aish-web start."""
        self._record("task_start", prompt=prompt)

    def task_end(self, status: str = "ok", error: str = "") -> None:
        """The task reached its end — answered, errored, or cancelled. All three
        are 'no longer in flight'; only a killed process leaves this unwritten.

        `status` records WHICH of those it was (#203). Until it existed, a turn
        that failed left nothing durable at all: the failure text went out as a
        live `error` event and was never written, so "why did last night's job
        fail?" was unanswerable from the log, and cold replay could only
        synthesize a generic "cut off mid-step" from the fact that steps were
        left unfinished. It is also what makes a failed turn count as OUTPUT —
        a background job that died is exactly the thing that should be waiting
        for you, and it produces no assistant message to notice.

        Additive: a record written before this carries no `status`, which every
        reader treats as "we do not know", i.e. exactly the old behaviour."""
        if status == "ok":
            self._record("task_end", status=status)
        else:
            self._record("task_end", status=status, error=error[:TASK_ERROR_CAP])

    def step(self, step: dict) -> None:
        """Persist one structured activity-trace step so the trace is
        reconstructable in any UI, long after the in-memory transcript is
        evicted. The step dict is the same one the web renderer receives."""
        self._record("trace", step=step)

    def command_event(self, event: dict) -> None:
        """Persist a terminal-block framing event (cmd_start / cmd_end). The
        event's `kind` names the record; reconstruct_events replays them as the
        command_start / command_end a live session emits."""
        self._record(event["kind"], **{k: v for k, v in event.items() if k != "kind"})

    def workspace(self, record: dict) -> None:
        """Persist a workspace change (kind:"cwd" / kind:"trust_dir") so resume
        restores it and reconstruct_events replays it as a timeline marker. The
        record's `kind` names it; the rest is its single path field."""
        self._record(record["kind"], **{k: v for k, v in record.items() if k != "kind"})
