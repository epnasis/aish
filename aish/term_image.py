"""Inline images in the terminal (issue #9).

Some terminals can render real images from escape sequences — pure base64
plus escapes, no dependencies:

- iTerm2: the OSC 1337 File protocol (detected via TERM_PROGRAM).
- kitty / WezTerm / ghostty: the kitty graphics protocol, PNG only here
  (other formats would need decoding, i.e. a dependency).

Everything else degrades to today's behavior — the markdown path stays
visible as text. tmux/screen re-wrap escapes unreliably, so they count as
unsupported.
"""

import base64
import os
import re
import sys
from pathlib import Path

from .backends import IMAGE_SUFFIXES

# Above this the base64 dump hurts more than the picture helps.
EMIT_MAX_BYTES = 10 * 1024 * 1024

IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")

KITTY_CHUNK = 4096  # base64 chars per graphics-protocol escape


def supports_images() -> str | None:
    """Which inline-image protocol this terminal speaks, if any."""
    if not sys.stdout.isatty():
        return None
    term = os.environ.get("TERM", "")
    if os.environ.get("TMUX") or "screen" in term or "tmux" in term:
        return None
    program = os.environ.get("TERM_PROGRAM", "")
    if program == "iTerm.app":
        return "iterm2"
    if program in ("WezTerm", "ghostty") or "kitty" in term or "ghostty" in term:
        return "kitty"
    return None


def local_image_paths(answer: str, roots) -> list[Path]:
    """Existing image files the answer references via ![alt](/abs/path),
    kept only when inside the session roots — the same scope the web UI's
    /file endpoint enforces."""
    resolved_roots = [Path(r).resolve() for r in roots]
    found: list[Path] = []
    for target in IMAGE_MD_RE.findall(answer):
        if target.startswith(("http://", "https://")) or not target.startswith(("/", "~")):
            continue
        path = Path(target).expanduser()
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        path = path.resolve()
        if not any(path.is_relative_to(r) for r in resolved_roots):
            continue
        if path.is_file() and path not in found:
            found.append(path)
    return found


def emit(path: Path, protocol: str) -> bool:
    """Write the escape sequence that displays `path` inline. Returns False
    when this file can't be shown (too big, unreadable, or a format the
    protocol doesn't take) — the caller just skips it; the path is already
    visible in the answer text."""
    try:
        if path.stat().st_size > EMIT_MAX_BYTES:
            return False
        data = path.read_bytes()
    except OSError:
        return False
    if protocol == "iterm2":
        payload = base64.b64encode(data).decode("ascii")
        name = base64.b64encode(path.name.encode()).decode("ascii")
        sys.stdout.write(
            f"\x1b]1337;File=name={name};size={len(data)};inline=1:{payload}\x07\n"
        )
    elif protocol == "kitty":
        if path.suffix.lower() != ".png":  # f=100 is PNG-only
            return False
        payload = base64.b64encode(data).decode("ascii")
        chunks = [payload[i:i + KITTY_CHUNK] for i in range(0, len(payload), KITTY_CHUNK)]
        for i, chunk in enumerate(chunks):
            control = "a=T,f=100," if i == 0 else ""
            more = 1 if i < len(chunks) - 1 else 0
            sys.stdout.write(f"\x1b_G{control}m={more};{chunk}\x1b\\")
        sys.stdout.write("\n")
    else:
        return False
    sys.stdout.flush()
    return True
