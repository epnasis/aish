"""The media store: durable, aish-owned image files an answer can display (#188).

Why a store rather than "wherever the model put the file":

- The scratch workspace is auto-approved for writing but **dies with its
  chat** (#258; before that, with the session — an image left there was a
  broken picture the moment you reopened), and it is private to one chat,
  while a picture is displayed by a transcript that is permanent and
  exportable. Different lifetimes, different scope.
- The project tree is the wrong place for a picture of a phone. Writing there
  also needs approval, which turns "show me what it looks like" into a diff
  review.
- Remote `![](https://…)` cannot be displayed at all: the browser only fetches
  images from the whitelisted hosts (#178 P1-12), because a render-time fetch of
  a model-chosen URL is a zero-click exfiltration channel. Fetching server-side
  into this store keeps that policy exactly as strict — the browser still only
  ever loads same-origin bytes — while making arbitrary image sources work.

Files are **content-addressed**: the same picture fetched twice is one file, so
a retry is free and the store cannot fill up with duplicates. Bounded by bytes
and count, evicting least-recently-used — a cache, not an archive, and a
re-fetch restores anything evicted.

Format detection is by **magic bytes, never the extension**: the failure this
exists to catch is a WAF block page or an HTML error page served as `.jpg`,
which the extension happily agrees with and no image decoder does.

These are the MECHANICS of a store, and since #318 there are two of them: this
one, inside `Agent.workspace_roots` because a picture an answer displays must be
displayable, and the evidence-frame store (`browser.frames_dir`), outside every
root because a frame is a picture of a page from OUTSIDE. Same content
addressing, same LRU, different directory and different caps — the location is
the security property and it belongs to the caller, not here.
"""

import hashlib
import re
from pathlib import Path

# Exactly the formats the web UI serves (server.IMAGE_TYPES) and the terminal
# renderer accepts (backends.IMAGE_SUFFIXES). SVG is deliberately absent: it
# executes scripts when opened full-size.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

MEDIA_MAX_BYTES = 200 * 1024 * 1024  # whole store, across sessions
MEDIA_MAX_FILES = 500
IMAGE_MAX_BYTES = 20 * 1024 * 1024  # one image; matches the web UI's inline cap

# The evidence-frame store's own caps (#318). Frames used to share the two
# above, and the store they shared was the media store, which put a picture of
# a hostile page inside `workspace_roots` — the hole #318 closed by moving them
# out (`browser.frames_dir`). A separate store needs a separate budget anyway,
# and having one settles the open question the shared caps left behind: frames
# arrive one per snapshot, so seventeen browsing flows turned the whole store
# over and the oldest file evicted was plausibly a picture from one of the
# owner's own chats — a broken image in an old transcript with nothing saying
# why. Nothing a frame does can evict one of those any more.
#
# Measured, not guessed: a frame is the browse viewport (1440x820) at JPEG
# quality 50, **67 KB** (`docs/browser.md` has the table). So a thousand of
# them is about 67 MB, and the FILE COUNT is what binds — the same relationship
# the media store has, and deliberately so, because "how many pages back can I
# look?" is the question an owner actually asks. One browse flow is ~30
# snapshots, so a thousand frames is roughly thirty flows: a week of heavy
# driving, against a picture in an answer which is meant to outlive the chat.
# The byte ceiling is the backstop for a page that photographs larger than the
# measurement, and 100 MB keeps both stores together under a third of a
# gigabyte on a box that also runs a VM.
FRAME_MAX_BYTES = 100 * 1024 * 1024
FRAME_MAX_FILES = 1000

_SLUG_RE = re.compile(r"[^a-z0-9]+")
SLUG_MAX = 40


def sniff(data: bytes) -> str | None:
    """The file extension these bytes really are, or None if they are not an
    image aish can display. WebP needs the container check (RIFF….WEBP) rather
    than a fixed prefix, so it is handled apart from the table."""
    for magic, suffix in _MAGIC:
        if data.startswith(magic):
            return suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def slug(hint: str) -> str:
    """A short filesystem-safe stem from a caption or URL, for a filename a
    human can recognise in a directory listing. Purely cosmetic — identity is
    the content hash, so an empty result is fine."""
    text = _SLUG_RE.sub("-", hint.lower()).strip("-")
    return text[:SLUG_MAX].strip("-")


def store(
    data: bytes,
    media_dir: Path,
    hint: str = "",
    *,
    max_bytes: int | None = None,
    max_files: int | None = None,
) -> Path:
    """Write these image bytes into the store and return the path to display.

    Content-addressed, so a repeat call returns the existing file untouched
    apart from its mtime — which is what makes the LRU prune meaningful.
    Raises ValueError when the bytes are not a displayable image; the caller
    turns that into a message the model can act on.

    The caps are ARGUMENTS because there is more than one store now (#318) and
    each holds a different kind of picture with a different worth-keeping-for:
    the media store holds pictures an answer displays, which outlive the chat,
    and the frame store holds diagnostics that are stale within days. Passing
    `None` means the media store's own caps, so the caller that has always been
    here reads exactly as it did.
    """
    suffix = sniff(data)
    if suffix is None:
        raise ValueError("not a displayable image")
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:12]
    stem = slug(hint)
    path = media_dir / (f"{digest}-{stem}{suffix}" if stem else f"{digest}{suffix}")
    if path.exists():
        path.touch()  # refresh recency; the bytes are already right
    else:
        path.write_bytes(data)
    prune(media_dir, max_bytes=max_bytes, max_files=max_files)
    return path


def prune(
    media_dir: Path, *, max_bytes: int | None = None, max_files: int | None = None
) -> list[Path]:
    """Evict least-recently-used files until the store is under both caps.
    Returns what was removed. Best-effort: a file that vanishes under us (or
    that we may not delete) is skipped rather than raising into a tool call.

    The caps are resolved HERE rather than in the signature's defaults, so the
    module constants stay the single answer: a default argument would bind the
    value at import and quietly ignore anything that changed it afterwards."""
    max_bytes = MEDIA_MAX_BYTES if max_bytes is None else max_bytes
    max_files = MEDIA_MAX_FILES if max_files is None else max_files
    try:
        files = [p for p in Path(media_dir).iterdir() if p.is_file()]
    except OSError:
        return []
    entries = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
    entries.sort()  # oldest first
    total = sum(size for _, size, _ in entries)
    removed: list[Path] = []
    for _, size, path in entries:
        if len(entries) - len(removed) <= max_files and total <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed.append(path)
    return removed
