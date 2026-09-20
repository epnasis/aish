"""Write a file whole, so a reader never sees half of one — and never see two
writers share the scratch file either (#395).

Four stores wrote through a temp file named after the PID: `evidence`,
`turns`, `roles`' admission ledger and `vouches`. The PID is unique per
PROCESS, and aish-web runs every session on a thread pool inside one process.
Two sessions recording the same content-addressed blob in the same instant —
two triggered chats with an identical brief — both saw the blob absent, both
wrote the ONE temp path, the first `replace` consumed it and the second raised
`FileNotFoundError`, which killed that session's whole task. CI reproduced it
twice on one commit; eight local runs never did.

So the scratch name is made by `tempfile.mkstemp`, which is unique per CALL by
construction (O_EXCL against a random suffix), and this is the only module
that makes one: a temp name spelled at a call site is the class of bug this
exists to close, and `tests/test_atomic_write.py` sweeps for the PID spelling.

The scratch file lives in the target's own directory so the final `os.replace`
is a same-filesystem rename, which is what makes it atomic. It is removed on
every failure path: nothing downstream (`turns.usage`, `evidence.purge`, a
`ls` of the state dir) has to know what a stray `.tmp` file is.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def publish(
    path: Path, text: str, *, encoding: str | None = None, keep_existing: bool = False
) -> bool:
    """Write `text` to `path` through a per-call temp file; True if it landed.

    `keep_existing` is for a content-addressed store, where a file already at
    `path` holds these exact bytes by construction: if a concurrent writer
    published between the caller's absence check and this call's rename, the
    temp is dropped and False comes back — the store still says what it should,
    it just was not this call that made it so. The check is a courtesy rather
    than a lock: two callers can both pass it and rename in turn, and the
    second rename lays identical bytes over identical bytes.

    Without it the last writer wins, which is what a ledger written whole on
    every change wants.

    `encoding` is passed through untouched, so each caller keeps the reader it
    already pairs with (`None` is `open`'s default, as `Path.write_text` has).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp")
    moved = False
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
        if keep_existing and path.exists():
            return False
        os.replace(tmp_name, path)
        moved = True
        return True
    finally:
        # On any exit but a completed rename — a full disk mid-write, a refused
        # rename, a winner already in place — the temp is still here and must
        # not be. Guarded by the flag rather than by `missing_ok`: once the
        # rename has freed the name, an unlink of it could only hit a file that
        # is no longer ours.
        if not moved:
            os.unlink(tmp_name)
