"""Sign-ins aish may re-establish by itself (#280).

eon.pl expires its session in about fifteen minutes, so the persistent profile
— the thing the whole browser feature rests on — buys nothing there, and every
later read comes back *the session lapsed*. The only repair was the owner
picking up his phone and signing in by hand, which means he did the hard part.
That is the case this exists for, and it generalises: he wants an assistant
that acts on his behalf, not one energy portal automated.

**The invariant is not "no code in aish types a password"** — the remote view
already types his, from his own hands. It is: *the model is never in the
credential loop, and a credential never enters model context, tool arguments,
logs or traces.* Nothing here is a tool, nothing takes a model-supplied
argument, and `secrets.get_signin` is called only by the browser's own replay.

Three things make a stored sign-in safe to hold, and they are all structural:

- **It is bound to an ORIGIN, exactly** — scheme, host and port, compared as
  equals. Not the dot-boundary suffix match `browser.is_logged_in` uses: that
  one is deliberately lenient because gating too much is its safe direction,
  and reusing it here would fire the credential on any subdomain an attacker
  can raise.
- **It carries the LOGIN URL, and that is a fence, not bookkeeping.** Without
  it the replay rule degrades to "type the credential wherever a password field
  appears on this host", so any injected form on the origin harvests it. The
  credential is typed only at the page the owner recorded.
- **It is written only after the sign-in was seen to WORK.** A password stored
  unverified fails later, unattended, in a way nothing can diagnose — and with
  the one-attempt rule it would burn that attempt on a known-bad value at every
  lapse, ticking the site's lockout counter.

What is NOT here, deliberately: any way for the model to ask for a sign-in to
be saved, replayed, listed or read.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

from . import secrets

# Metadata lives beside the browser profile, not in ~/.config/aish: that tree is
# auto-committed to a git remote on a timer, and while this file holds no
# secret it is a precise map of which accounts aish can open.
STATE = Path.home() / ".local" / "state" / "aish" / "browser" / "signins.json"


@dataclass
class Record:
    """What aish knows about one sign-in. No secret is in here — the identifier
    and the password are in the Keychain, keyed by this origin."""

    origin: str
    url: str  # the login page, and the ONLY page the credential may be typed at
    saved: str  # ISO date, for the settings row
    used: int = 0
    last_used: str = ""
    # Set when an attempt failed. A stale credential must stop being tried:
    # retrying is how accounts lock, and unattended it locks them silently.
    suspect: str = ""


def origin_of(url: str) -> str:
    """`scheme://host[:port]` for this URL, or "".

    Lowercased host, default ports dropped, so the string comparison that
    decides everything downstream cannot be fooled by spelling."""
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return ""
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return ""
    port = parts.port
    if port and port != (443 if scheme == "https" else 80):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _load() -> list[Record]:
    try:
        raw = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for row in raw if isinstance(raw, list) else []:
        if isinstance(row, dict) and row.get("origin") and row.get("url"):
            out.append(
                Record(
                    origin=str(row["origin"]),
                    url=str(row["url"]),
                    saved=str(row.get("saved", "")),
                    used=int(row.get("used", 0) or 0),
                    last_used=str(row.get("last_used", "")),
                    suspect=str(row.get("suspect", "")),
                )
            )
    return out


def _write(records: list[Record]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def records() -> list[Record]:
    """Every stored sign-in, oldest first. Safe to show — no secret in it."""
    return _load()


def origins() -> list[str]:
    return [r.origin for r in _load()]


def find(url: str) -> Record | None:
    """The sign-in for this URL's origin, or None. EXACT origin, never a suffix."""
    origin = origin_of(url)
    if not origin:
        return None
    return next((r for r in _load() if r.origin == origin), None)


def save(login_url: str, identifier: str, password: str, *, today: str) -> Record:
    """Record a sign-in the owner just made and confirmed.

    `login_url` is where he made it — captured from the live page, never typed
    and never model-supplied, which is what makes it trustworthy as the fence."""
    origin = origin_of(login_url)
    if not origin:
        raise secrets.SecretError(f"not a usable login address: {login_url!r}")
    if not password:
        raise secrets.SecretError("nothing to save: no password was typed")
    secrets.put_signin(origin, identifier, password)
    kept = [r for r in _load() if r.origin != origin]
    record = Record(origin=origin, url=login_url.strip(), saved=today)
    _write([*kept, record])
    return record


def forget(origin: str) -> bool:
    """Delete a stored sign-in. The live SESSION is untouched — it is his to
    end, at the site, which is the same fence `/browser forget` keeps."""
    kept = [r for r in _load() if r.origin != origin]
    if len(kept) == len(_load()):
        return False
    secrets.delete_signin(origin)
    _write(kept)
    return True


def _update(origin: str, **fields: object) -> None:
    rows = _load()
    for row in rows:
        if row.origin == origin:
            for key, value in fields.items():
                setattr(row, key, value)
            _write(rows)
            return


def note_used(origin: str, *, when: str) -> None:
    record = find(origin)
    if record is not None:
        _update(origin, used=record.used + 1, last_used=when, suspect="")


def note_failed(origin: str, *, why: str) -> None:
    """Mark a credential stale so nothing tries it again.

    One attempt, never a retry: retrying a wrong password is how accounts lock,
    and unattended it locks them silently. The repair is his — re-capture at the
    next hand sign-in, which clears this."""
    _update(origin, suspect=why or "the site did not accept it")


def credential(origin: str) -> tuple[str, str] | None:
    """(identifier, password), or None when there is nothing usable.

    A suspect record answers None: it is exactly the value that must not be
    spent again."""
    record = next((r for r in _load() if r.origin == origin), None)
    if record is None or record.suspect:
        return None
    return secrets.get_signin(origin)
