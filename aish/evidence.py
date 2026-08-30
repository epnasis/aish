"""The evidence store — the bytes a log record points at.

A diagnostic record has to be self-contained against later edits: a record
naming only `tool menu` forces the reader to open the tool directory *as it
reads today*, which is re-derivation with extra steps (docs/trace-contract.md
§0, corollary 1). So the bytes have to be kept. But keeping them *inside* the
session log is wrong twice over, and both reasons are why this module exists:

**Erasability.** The log records injected knowledge by NAME only, and the
contract says so explicitly — it is the one text an audit record must not
duplicate (§3.10). Verbatim bodies in every log would break that: a secret
pasted once and then remembered would be written into every later session that
preloads that memory, so redacting the original turn would leave copies
elsewhere forever and `forget_memory` would stop being a deletion. Content
addressing makes removal a real operation — `purge(digest)` drops the bytes
once, everywhere that referenced them, and every reader then honestly says
"purged" instead of "not recorded".

**The log is not append-only.** `redact_turn` rewrites it in place and removes
records, so a blob written inside one turn can be deleted while later turns
still reference it. Blobs kept outside the rewritten file cannot be lost that
way.

The other in-place rewrite, `supersede_last_turn` (web Retry), no longer takes
anything away: since #339 it MARKS the discarded turn's records `superseded`
and nothing leaves the file, replacing `rewind_last_turn`, which deleted them.
So Retry is not a reason for this store — `redact_turn` alone is, and one
in-place deleter is reason enough. Retry does still say what the store may not
be keyed on: `session._is_superseded` states the rule as *a superseded record
is read by evidence, and by nothing that reconstructs the chat's current
state*, so a blob's lifetime cannot follow whether its turn is still live. It
follows the digest, and `purge` is what ends it.

Deduplication is the third benefit and the least important one: the tool menu
is ~31 KB and near-constant, so one copy serves hundreds of sessions.

The store is deliberately dumb — it knows nothing about what a blob means.
The log record says what it was; this module only answers "here are those
bytes, or they are gone".
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

STORE_DIRNAME = "evidence"


def digest_of(text: str) -> str:
    """The content address: full sha256 hex of the UTF-8 bytes."""
    return hashlib.sha256(text.encode()).hexdigest()


def store_dir(state_dir: os.PathLike | str) -> Path:
    return Path(state_dir) / STORE_DIRNAME


def _blob_path(state_dir: os.PathLike | str, digest: str) -> Path:
    # Sharded by the first two hex chars: one directory per store would hold a
    # growing flat list that every `ls` of the state dir has to walk.
    return store_dir(state_dir) / digest[:2] / digest


def put(text: str, state_dir: os.PathLike | str | None) -> str:
    """Store `text` if it is not already there; return its digest.

    Returns the digest even when there is nowhere to write it, so a caller with
    no state dir (a test, a CLI run with logging off) still records a reference
    that a later reader reports as unresolvable rather than as absent.
    """
    digest = digest_of(text)
    if state_dir is None:
        return digest
    path = _blob_path(state_dir, digest)
    if path.exists():
        return digest
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written through a temp file: a reader that finds a half-written blob
    # cannot tell it from a complete one by looking, and the whole point of the
    # store is that what it hands back is what was recorded.
    tmp = path.with_name(f".{digest}.tmp{os.getpid()}")
    tmp.write_text(text)
    tmp.replace(path)
    return digest


def get(digest: str, state_dir: os.PathLike | str | None) -> str | None:
    """The stored bytes, or None if they are not there any more.

    None is REPORTABLE, not an error: a purged blob and a blob from another
    machine's state dir both land here, and the reader's job is to say so
    rather than to imply nothing was ever recorded.

    The digest is re-verified on read. A blob that does not hash to its own
    name has been truncated or tampered with, and returning it would let a
    reader quote evidence that is not what was recorded.

    `UnicodeDecodeError` is caught beside `OSError` because this store is
    text-only by CONSTRUCTION and not by enforcement: `digest_of` hashes UTF-8
    bytes and `put` writes with `write_text`, so nothing here can produce a
    blob that `read_text` chokes on — but nothing here refuses one either. A
    digest arriving from anywhere else (a hand-made file under the store, a
    future writer, a state dir carried between machines) whose bytes are binary
    would raise straight out of a READER whose stated law is that unreadable
    bytes are reportable, not an error. Undecodable and truncated are the same
    answer for the same reason: what is on disk is not what was recorded.
    """
    if state_dir is None or not digest:
        return None
    path = _blob_path(state_dir, digest)
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    return text if digest_of(text) == digest else None


def purge(digest: str, state_dir: os.PathLike | str | None) -> bool:
    """Remove the bytes for `digest`. True if something was removed.

    This is the operation that keeps `forget_memory` and `redact_turn` honest
    once their content is referenced from more than one session log.
    """
    if state_dir is None or not digest:
        return False
    path = _blob_path(state_dir, digest)
    try:
        path.unlink()
    except OSError:
        return False
    return True
