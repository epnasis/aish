"""The per-chat store — the bytes a `sent` record points at (#352).

One directory per chat under `<state_dir>/turns/<chat>/`, holding every blob
that chat's log references, content-addressed WITHIN the chat: the history a
chat repeats across its model calls is on disk once, and a chat is a directory
and nothing else. It is deliberately NOT shared across chats, unlike
`evidence.py`: a pasted secret then lives in exactly one directory rather than
in a deduplicated store reachable from every chat's manifest, which is the
erasability argument with fewer moving parts. Deleting a chat deletes its
directory; redacting a turn unlinks the digests its records referenced.

Three laws, all the owner's (2026-09-02):

- **Whole, never cut.** No blob here has a cap. The only caps are on copies
  resident in the LOG (`reasoning`, `call.args`, `output`).
- **Bounded by chat, never by step.** When the tree exceeds `TURNS_BUDGET_BYTES`
  the sweep evicts a whole chat's evidence, oldest by last activity, and
  never one blob of a chat that is kept. It leaves a dated tombstone so a
  reader says *evidence for this chat was evicted on <date>* — a per-chat
  state distinct from *never recorded* and from *purged*.
- **A blob is re-hashed on read** (the `evidence.py` rule): a file that does
  not hash to its own name is not what was recorded, and reads as absent.

The store is as dumb as `evidence.py`: it knows nothing about what a blob
means. The log record says what it was; this module answers "here are those
bytes, or they are gone, or the whole chat's evidence went on this date".
Text-only by construction — `digest_of` hashes UTF-8 and `put` writes text —
and `get` treats undecodable bytes exactly as it treats a hash mismatch.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from pathlib import Path
from typing import NamedTuple

STORE_DIRNAME = "turns"
TOMBSTONE = ".evicted"

# The whole tree's budget. Measured against the owner's corpus on 2026-09-03:
# 818 logs, 119 MB of JSONL, 43.3 M chars of message content across 1,100
# tasks. Had every model call been recorded here from the first day, with
# dedup within each chat, the tree would hold ~150 MB (message content, plus
# one system text per task and one tool menu per chat at ~55 KB each). 2 GB is
# therefore roughly a decade of this owner's history at today's rate, on a disk
# with 1.5 TB free — large enough that eviction is an exception the reader can
# name rather than a routine the owner meets, and small enough that a runaway
# chat cannot fill the disk.
TURNS_BUDGET_BYTES = 2 * 1024**3

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ChatUsage(NamedTuple):
    key: str
    bytes: int
    blobs: int
    last_activity: float  # epoch seconds of the newest blob, 0.0 when none


def digest_of(text: str) -> str:
    """The content address: full sha256 hex of the UTF-8 bytes."""
    return hashlib.sha256(text.encode()).hexdigest()


def chat_key(session: os.PathLike | str) -> str:
    """The directory name for one chat: its session log's stem, so a chat is
    keyed the same way its scratch workspace is (`agent.chat_scratch_dir`)."""
    name = Path(session).name
    return name[: -len(".jsonl")] if name.endswith(".jsonl") else name


def _safe_key(session: os.PathLike | str | None) -> str | None:
    if session is None:
        return None
    key = chat_key(session)
    if not key or key in (".", "..") or "/" in key or "\\" in key or key.startswith("."):
        return None
    return key


def store_dir(state_dir: os.PathLike | str) -> Path:
    return Path(state_dir) / STORE_DIRNAME


def chat_dir(state_dir: os.PathLike | str, session: os.PathLike | str) -> Path:
    return store_dir(state_dir) / chat_key(session)


def put(text: str, state_dir: os.PathLike | str | None, session: os.PathLike | str | None) -> str:
    """Store `text` in the chat's directory if it is not already there; return
    its digest.

    Returns the digest even with nowhere to write — no state dir, or no
    session log to key the directory on — so the caller still records a
    reference the reader reports as unresolvable rather than as never
    recorded (the `evidence.put` rule).
    """
    digest = digest_of(text)
    key = _safe_key(session)
    if state_dir is None or key is None:
        return digest
    path = store_dir(state_dir) / key / digest
    if path.exists():
        return digest
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Through a temp file, as `evidence.put` does: a reader that finds a
        # half-written blob cannot tell it from a complete one by looking.
        tmp = path.with_name(f".{digest}.tmp{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # A full disk or an unwritable state dir is not a reason for a model
        # call to fail. The record still carries the digest, and the reader
        # reports the bytes as not in the store — never as never recorded.
        pass
    return digest


def get(
    digest: str, state_dir: os.PathLike | str | None, session: os.PathLike | str | None
) -> str | None:
    """The stored bytes, or None when they are not there.

    None is REPORTABLE, not an error, and it is one answer for several
    situations the reader tells apart by other means: purged by a redaction,
    evicted with the chat (`evicted_on` says when), or a store on another
    machine. A digest that is not a digest reads as absent too — this is the
    function an HTTP endpoint reaches, so it must not build a path from
    anything but 64 hex characters.
    """
    key = _safe_key(session)
    if state_dir is None or key is None or not _DIGEST.match(digest or ""):
        return None
    path = store_dir(state_dir) / key / digest
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if digest_of(text) == digest else None


def evicted_on(
    state_dir: os.PathLike | str | None, session: os.PathLike | str | None
) -> str | None:
    """The date the chat's evidence was evicted, or None when it never was.

    The tombstone lives INSIDE the chat's directory rather than in place of
    it, because a chat is reopened long after it was last active — on the web
    every restart reopens the recent ones — and a blob written after an
    eviction has to land somewhere. A reader asking about a digest the
    directory does not hold answers *evicted on <date>* when this returns a
    date, and *purged* otherwise.
    """
    key = _safe_key(session)
    if state_dir is None or key is None:
        return None
    try:
        record = json.loads((store_dir(state_dir) / key / TOMBSTONE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    when = record.get("evicted") if isinstance(record, dict) else None
    return str(when) if when else None


def unlink(
    digests: set[str] | frozenset[str],
    state_dir: os.PathLike | str | None,
    session: os.PathLike | str | None,
) -> int:
    """Remove the named blobs from the chat's directory; how many went."""
    key = _safe_key(session)
    if state_dir is None or key is None:
        return 0
    removed = 0
    for digest in digests:
        if not _DIGEST.match(digest or ""):
            continue
        try:
            (store_dir(state_dir) / key / digest).unlink()
        except OSError:
            continue
        removed += 1
    return removed


def delete_chat(state_dir: os.PathLike | str | None, session: os.PathLike | str | None) -> bool:
    """The chat's whole directory, tombstone included. Deleting a chat is
    what deletes its evidence (#177); nothing else does. True if it existed."""
    key = _safe_key(session)
    if state_dir is None or key is None:
        return False
    return _remove_dir(store_dir(state_dir) / key)


def _remove_dir(path: Path) -> bool:
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    for entry in entries:
        try:
            entry.unlink()
        except OSError:
            continue
    try:
        path.rmdir()
    except OSError:
        return True  # something survived; the blobs are gone either way
    return True


def usage(state_dir: os.PathLike | str) -> list[ChatUsage]:
    """Every chat's footprint, from the filesystem alone. A tombstone is a
    file like any other and counts; it is a few dozen bytes."""
    out: list[ChatUsage] = []
    try:
        chats = [entry for entry in os.scandir(store_dir(state_dir)) if entry.is_dir()]
    except OSError:
        return out
    for chat in chats:
        size = 0
        blobs = 0
        newest = 0.0
        try:
            for blob in os.scandir(chat.path):
                if not blob.is_file():
                    continue
                stat = blob.stat()
                size += stat.st_size
                if blob.name != TOMBSTONE and not blob.name.startswith("."):
                    blobs += 1
                    newest = max(newest, stat.st_mtime)
        except OSError:
            continue
        out.append(ChatUsage(chat.name, size, blobs, newest))
    return out


def sweep(state_dir: os.PathLike | str | None, budget: int = TURNS_BUDGET_BYTES) -> list[str]:
    """Evict whole chats, oldest by last activity, until the tree fits the
    budget; return the keys that went. Runs at server start and after a turn
    ends, never inside a tool call.

    Each evicted chat keeps a dated tombstone where its blobs were, so the
    reader can say when rather than merely that. A chat with no blobs left
    (already evicted, or emptied by redaction) is not a candidate — evicting
    it would free nothing and re-date a tombstone.
    """
    if state_dir is None:
        return []
    chats = usage(state_dir)
    total = sum(chat.bytes for chat in chats)
    if total <= budget:
        return []
    evicted: list[str] = []
    for chat in sorted(chats, key=lambda c: (c.last_activity, c.key)):
        if total <= budget:
            break
        if chat.blobs == 0:
            continue
        freed = _evict(store_dir(state_dir) / chat.key)
        if freed is None:
            continue
        total -= freed
        evicted.append(chat.key)
    return evicted


def _evict(path: Path) -> int | None:
    """Drop every blob in one chat's directory and write the tombstone.
    Returns the bytes freed, or None when the directory could not be read."""
    try:
        entries = [entry for entry in os.scandir(path) if entry.is_file()]
    except OSError:
        return None
    freed = 0
    blobs = 0
    for entry in entries:
        if entry.name == TOMBSTONE:
            continue
        try:
            size = entry.stat().st_size
            os.unlink(entry.path)
        except OSError:
            continue
        freed += size
        blobs += 1
    tombstone = {
        "evicted": datetime.datetime.now().isoformat(timespec="seconds"),
        "bytes": freed,
        "blobs": blobs,
    }
    try:
        (path / TOMBSTONE).write_text(json.dumps(tombstone), encoding="utf-8")
    except OSError:
        pass
    return freed
