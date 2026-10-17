"""The owner's browser identity, for requests he made himself (#360).

A plugin tool that must tell the bank "the user is present" (PSD2 PSU
headers) reads `AISH_BANK_PSU_UA` from its env. The rule for who gets one:
a request the owner made — or delegated once, at a moment — carries the
User-Agent of the browser he made it from; background work (recurring
automations, monitors, email and webhook triggers) carries none. The
terminal is him too, but has no browser, so it reuses the last one aish-web
saw, which `remember` keeps in the state tree.

`clean` is applied where a UA ENTERS aish (the web's stamp sites), so every
copy downstream — plugin_env, a `task_start` record, an automation file — is
already valid by construction.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import atomic_write

#: The plugin env key the bank tools read.
ENV_KEY = "AISH_BANK_PSU_UA"
#: Longer than any real browser's UA; a cap so a header can never bloat a
#: record or an automation file.
MAX_UA_CHARS = 512
FILENAME = "owner-user-agent"

# The same class schedule.py refuses in a task file: the value ends up in an
# HTTP header, where a CR/LF would be a second header.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def clean(ua: str) -> str:
    """One canonical form: control characters dropped, stripped, capped."""
    return _CONTROL_CHARS.sub("", ua or "").strip()[:MAX_UA_CHARS].strip()


def path(state_dir: Path) -> Path:
    return Path(state_dir) / FILENAME


def remember(state_dir: Path, ua: str) -> bool:
    """Record `ua` as the last browser the owner used; True if the file
    changed. The caller keeps its own last value, so this is reached only on
    a change and the per-message cost is no file read."""
    ua = clean(ua)
    if not ua:
        return False
    if last(state_dir) == ua:
        return False
    return atomic_write.publish(path(state_dir), ua + "\n", encoding="utf-8")


def last(state_dir: Path) -> str | None:
    """The last browser UA aish-web saw, or None — missing, unreadable and
    empty are all None, and this never raises: a terminal turn without a UA
    is a background-shaped turn, never a failed one."""
    try:
        text = path(state_dir).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return clean(text) or None
