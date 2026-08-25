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
- **It carries WHERE THE CREDENTIAL TRAVELLED when it worked** (#296), which is
  what lets the replay fence the credential instead of fencing the connection.
  The same sign-in that proves the password is good is the only authority there
  is on which addresses legitimately receive it, and it is watched at the one
  moment aish can see it: his own hands, in the remote view.

What is NOT here, deliberately: any way for the model to ask for a sign-in to
be saved, replayed, listed or read.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
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
    # Origins the password was WATCHED reaching during the owner's own
    # successful sign-in. Empty on every record saved before #296 and on any
    # sign-in whose credential request aish did not see, which is why
    # `may_receive_credential` has a fallback rather than failing closed.
    destinations: list[str] = field(default_factory=list)
    # A picture of the LAST attempt, in the media store — a path, never bytes,
    # and never a picture borrowed from an earlier attempt (#320). Both fields
    # are rewritten on every attempt, including to empty, for the reason
    # `_Seen.start_call` clears the browse frame: a stale picture presented as
    # this attempt's is worse than no picture. `last_frame_skipped` says WHICH
    # absence, in `browse.NO_FRAME_*`.
    last_frame: str = ""
    last_frame_skipped: str = ""


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


# ------------------------------------------- fencing the credential (#296)
#
# The fence used to be at the CONNECTION: every request with a body going
# anywhere but the login origin was aborted, and a non-empty blocked list was
# read as the sign-in having failed. Nearly every commercial login page carries
# a tracking pixel and a consent call, so that reported failure on most real
# sites — including ones where the session had actually come up — and on the
# sites that legitimately post credentials to a sibling origin it prevented the
# sign-in outright. The owner's own diagnosis is the design: "network
# connections out of the site don't tell you much — the same connections would
# happen if a human would be using the browser."
#
# So the fence reads the VALUE aish is about to send, which is this codebase's
# strongest primitive and the one `types_a_bank_account` already uses: judge
# what is being sent, not what the page claims about itself. Everything on the
# wire that does not carry the credential is none of the fence's business.

# A base64 fragment shorter than this is short enough to turn up by chance in
# an ordinary binary body, and a false positive here retires a good credential.
_BASE64_MIN = 8

_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")


def _base64_fragments(raw: bytes) -> set[str]:
    """Every alignment `raw` can appear at inside a larger base64 blob.

    base64 is computed in three-byte groups, so a value that is not at offset
    zero — a password inside `user:password` in a Basic auth header, or inside
    a JSON object that was encoded whole — encodes to entirely different
    characters. Three prefixes cover all three alignments; the partial group at
    each end is dropped, because only the fully-determined middle is the same
    text wherever the value lands."""
    fragments = set()
    for pad in range(3):
        encoded = base64.b64encode(b"\0" * pad + raw).decode("ascii")
        start = -(-pad // 3) * 4
        end = ((pad + len(raw)) // 3) * 4
        chunk = encoded[start:end]
        if len(chunk) >= _BASE64_MIN:
            fragments.add(chunk)
            fragments.add(chunk.replace("+", "-").replace("/", "_"))
    return fragments


def secret_needles(secret: str) -> tuple[str, ...]:
    """Every spelling of `secret` a real login form puts on the wire.

    Deliberately exact and case-sensitive: a password is a password, and
    loosening the match trades a hypothetical catch for a false positive that
    aborts a request and retires a working credential."""
    if not secret:
        return ()
    raw = secret.encode("utf-8", "surrogatepass")
    needles = {secret}
    # Percent- and form-encoding, in UPPER-case hex, which is what `quote`
    # emits and what `carries_secret` normalises a body to before looking.
    needles.add(urllib.parse.quote(secret, safe=""))
    needles.add(urllib.parse.quote_plus(secret))
    needles.add(json.dumps(secret, ensure_ascii=False)[1:-1])
    needles.add(json.dumps(secret)[1:-1])
    needles |= _base64_fragments(raw)
    return tuple(sorted(n for n in needles if n))


def carries_secret(text: str, needles: Sequence[str]) -> bool:
    """Does this piece of a request carry the credential?

    Takes the needles rather than the secret so the caller computes them once,
    per sign-in, instead of once per request on the owner's live browsing.

    The text is searched as it arrived AND with its percent-escapes folded to
    upper case, because which hex case an encoder emits is its own business and
    a needle per case would still miss one that mixes them. Both, because the
    fold would itself rewrite a secret that contains a literal `%xx`."""
    if any(needle in text for needle in needles):
        return True
    folded = _PERCENT.sub(lambda m: "%" + m.group(1).upper(), text)
    return folded != text and any(needle in folded for needle in needles)


# Enough of a public-suffix rule for the FALLBACK below, and no more. A real
# PSL would be a dependency and a monthly update for a question asked only
# about records saved before destinations were recorded.
_SECOND_LEVEL = frozenset(
    {"co", "com", "net", "org", "edu", "gov", "mil", "ac", "biz", "info"}
)


def registrable_domain(host: str) -> str:
    """`eon.pl` for `api.eon.pl`, `example.co.uk` for `www.example.co.uk`."""
    labels = [part for part in (host or "").lower().split(".") if part]
    if len(labels) < 3:
        return ".".join(labels)
    if labels[-2] in _SECOND_LEVEL and len(labels[-1]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _scheme_and_site(origin: str) -> tuple[str, str]:
    try:
        parts = urllib.parse.urlsplit(origin)
    except ValueError:  # a corrupt record answers nothing, never everything
        return "", ""
    return (parts.scheme or "").lower(), registrable_domain(parts.hostname or "")


def _same_site(origin: str, recorded: str) -> bool:
    scheme, site = _scheme_and_site(recorded)
    return bool(site) and _scheme_and_site(origin) == (scheme, site)


def may_receive_credential(record: Record, url: str) -> bool:
    """May a request carrying this record's password go to `url`?

    The authority is the owner's own successful sign-in: wherever the password
    travelled when it worked is where it may travel again, matched by exact
    origin like everything else here. The login origin itself is always allowed
    — it is the page the credential is being typed into.

    **A record saved before #296 has no destinations, and must not be given a
    fence it cannot satisfy.** Its fallback is the site's own registrable
    domain: for SENDING, unlike for TYPING, the sibling origin is the legitimate
    common case — an API subdomain, a central identity host — so refusing it
    would break the sign-in rather than merely misreport it. That is a weaker
    fence, it applies to exactly the records that predate the recording, and it
    tightens by itself the next time he signs in by hand."""
    origin = origin_of(url)
    if not origin:
        return False
    if origin == record.origin:
        return True
    if record.destinations:
        return origin in record.destinations
    return _same_site(origin, record.origin)


def _origins(values: object) -> list[str]:
    """Normalise a list of addresses to distinct origins, dropping the rest."""
    out: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        origin = origin_of(str(value))
        if origin and origin not in out:
            out.append(origin)
    return out


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
                    destinations=_origins(row.get("destinations")),
                    last_frame=str(row.get("last_frame", "")),
                    last_frame_skipped=str(row.get("last_frame_skipped", "")),
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


def save(
    login_url: str,
    identifier: str,
    password: str,
    *,
    today: str,
    destinations: object = (),
) -> Record:
    """Record a sign-in the owner just made and confirmed.

    `login_url` is where he made it — captured from the live page, never typed
    and never model-supplied, which is what makes it trustworthy as the fence.
    `destinations` is where the password was watched going while it worked, and
    it is trustworthy for the same reason: observed, not declared."""
    origin = origin_of(login_url)
    if not origin:
        raise secrets.SecretError(f"not a usable login address: {login_url!r}")
    if not password:
        raise secrets.SecretError("nothing to save: no password was typed")
    secrets.put_signin(origin, identifier, password)
    kept = [r for r in _load() if r.origin != origin]
    record = Record(
        origin=origin,
        url=login_url.strip(),
        saved=today,
        destinations=_origins(destinations),
    )
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


def note_frame(origin: str, *, path: str, skipped: str) -> None:
    """Point the record at a picture of the attempt that just happened (#320).

    Written on EVERY attempt, including with both values empty. A record that
    kept the last picture it managed to take would present an old page as this
    attempt's — the borrowed-frame failure `_Seen.start_call` exists to stop,
    one store over."""
    _update(origin, last_frame=path, last_frame_skipped="" if path else skipped)


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
