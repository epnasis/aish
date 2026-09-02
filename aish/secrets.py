"""Local secret store backed by the macOS login Keychain (issue #142).

Secrets for non-CLI integrations (Fastmail JMAP token, Home Assistant token,
ntfy/Pushover keys, webhook secrets) that aish must HOLD — the "auth stays with
the CLI" doctrine covers none of these. Design (decided with the owner, Fable-
reviewed):

- **Pure macOS Keychain** — each secret is a generic-password item under the
  service name "aish", read via ``/usr/bin/security``. Nothing aish writes lands
  on disk as plaintext, and a Keychain item structurally CANNOT be swept into
  the git-backed ``~/.config/aish`` (it is not a file). FileVault + login-unlock
  is the at-rest boundary.
- **Never in args, logs, or model context.** Secrets are resolved at tool-exec
  time and injected into ONLY the declaring wrapper's environment (see
  ``tool_plugins.execute``). A value never enters a tool-call's arguments (which
  are logged) nor the model's messages.

A plaintext index of secret NAMES (not values) is kept in the state dir so the
CLI can list what is set — names are metadata, not secret.

A THIRD namespace lives here too (#343): the owner's **declared personal
values** — his address, phone, date of birth, e-mail, name. They are not
credentials and they are not scrubbed; they exist so the browse gate can ask
before aish types one of them into somebody's form, and before one rides a
composed address. See "declared personal values" below.

The realistic security ceiling is FileVault + OS access control: this protects
against disk theft and accidental leakage (git, logs, model context), NOT a
live attacker already running as the user. That is out of scope by design.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from pathlib import Path

SERVICE = "aish"
# Site sign-ins live in their OWN Keychain service, and the separation is a
# fence rather than tidiness (#280). Two things resolve a name against SERVICE:
# a plugin manifest's `secrets:` field, and `aish secret get`. A site credential
# must be reachable by neither — a wrapper that could declare `secrets: eon_pl`
# would put the owner's password in a subprocess environment aish does not
# control, and `aish secret list` is a different lifecycle with a different UI.
SIGNIN_SERVICE = "aish-signin"
# The owner's declared personal values (#343) are a THIRD namespace, and the
# separation is the same fence SIGNIN_SERVICE is. A plugin manifest's `secrets:`
# field and `aish secret get` both resolve a name against SERVICE; his home
# address must be reachable by neither, or the value this slice exists to keep
# off the wire would be injected into a wrapper's environment by declaring it.
PERSONAL_SERVICE = "aish-personal"
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # env-var-shaped
NAMES_INDEX = Path.home() / ".local" / "state" / "aish" / "secret-names.txt"
PERSONAL_NAMES_INDEX = (
    Path.home() / ".local" / "state" / "aish" / "personal-names.txt"
)
_SECURITY = "/usr/bin/security"

# Below this length a stored "secret" collides with ordinary text — scrubbing
# every occurrence of a 3-character value would corrupt output rather than
# protect anything. A real token clears it by an order of magnitude.
MIN_MATCH = 8


class SecretError(RuntimeError):
    pass


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def _security(args: list[str], value: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_SECURITY, *args], input=value, text=True, capture_output=True
    )


def get(name: str) -> str | None:
    """The secret's value, or None if unset. Trailing newline stripped."""
    if not valid_name(name):
        return None
    proc = _security(["find-generic-password", "-a", name, "-s", SERVICE, "-w"])
    if proc.returncode != 0:
        return None
    # -w prints the value; a trailing newline is added by `security`, not stored.
    return proc.stdout.rstrip("\n")


def put(name: str, value: str) -> None:
    """Store or update a secret. Raises SecretError on failure."""
    if not valid_name(name):
        raise SecretError(f"invalid secret name {name!r} (need [A-Za-z_][A-Za-z0-9_]*)")
    # -U updates in place if the item exists. NOTE: the value passes through the
    # `security` process argv briefly — acceptable on a single-user box (the
    # ceiling is FileVault anyway), and `security` offers no stdin password path.
    proc = _security(
        ["add-generic-password", "-a", name, "-s", SERVICE, "-U", "-w", value]
    )
    if proc.returncode != 0:
        raise SecretError(proc.stderr.strip() or "failed to store secret")
    _index_add(name)
    _invalidate()


def delete(name: str) -> bool:
    """Remove a secret; True if it existed."""
    if not valid_name(name):
        return False
    proc = _security(["delete-generic-password", "-a", name, "-s", SERVICE])
    _index_remove(name)
    _invalidate()
    return proc.returncode == 0


# --- site sign-ins (#280) ---------------------------------------------------
#
# Keyed by ORIGIN (scheme://host[:port]) rather than by a name the caller picks,
# because the whole safety property is that a credential belongs to exactly one
# origin and can never be typed at another. There is no `put_signin(name, ...)`
# for the same reason there is no name: nothing may choose one.


def put_signin(origin: str, identifier: str, password: str) -> None:
    """Store the sign-in for one origin. Raises SecretError on failure."""
    if not origin.startswith(("http://", "https://")):
        raise SecretError(f"a sign-in is bound to an origin, not to {origin!r}")
    blob = json.dumps({"identifier": identifier, "password": password})
    proc = _security(
        ["add-generic-password", "-a", origin, "-s", SIGNIN_SERVICE, "-U", "-w", blob]
    )
    if proc.returncode != 0:
        raise SecretError(proc.stderr.strip() or "failed to store sign-in")
    _invalidate()


def get_signin(origin: str) -> tuple[str, str] | None:
    """(identifier, password) for this origin, or None.

    NEVER returned to a model: the only caller is the browser's own sign-in
    replay, which types it into a page and reports nothing back."""
    proc = _security(["find-generic-password", "-a", origin, "-s", SIGNIN_SERVICE, "-w"])
    if proc.returncode != 0:
        return None
    try:
        loaded = json.loads(proc.stdout.rstrip("\n"))
    except ValueError:
        return None
    identifier = str(loaded.get("identifier", ""))
    password = str(loaded.get("password", ""))
    return (identifier, password) if password else None


def delete_signin(origin: str) -> bool:
    proc = _security(["delete-generic-password", "-a", origin, "-s", SIGNIN_SERVICE])
    _invalidate()
    return proc.returncode == 0


def names() -> list[str]:
    """Names of stored secrets (from the state-dir index), sorted."""
    return _read_index(NAMES_INDEX)


def _read_index(index: Path) -> list[str]:
    # ValueError as well as OSError: `read_text(encoding="utf-8")` raises
    # UnicodeDecodeError — a ValueError — on an index that is not UTF-8, and
    # that used to propagate through both declared-value gates to `_dispatch`'s
    # generic handler. It was contained by accident rather than by design, and
    # an accident is not a failure direction.
    try:
        return sorted(
            n for n in index.read_text(encoding="utf-8").splitlines() if n.strip()
        )
    except (OSError, ValueError):
        return []


def _index_add(name: str, index: Path | None = None) -> None:
    # Resolved at CALL time, never as a default argument: a default binds the
    # module constant at import, and the suite-wide guard that redirects the
    # index away from the developer's real state dir rebinds that constant.
    index = NAMES_INDEX if index is None else index
    current = set(_read_index(index))
    if name in current:
        return
    current.add(name)
    _write_index(current, index)


def _index_remove(name: str, index: Path | None = None) -> None:
    index = NAMES_INDEX if index is None else index
    current = set(_read_index(index))
    if name in current:
        current.discard(name)
        _write_index(current, index)


def _write_index(name_set: set[str], index: Path | None = None) -> None:
    index = NAMES_INDEX if index is None else index
    try:
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text("\n".join(sorted(name_set)) + "\n", encoding="utf-8")
    except OSError:
        pass


# --- Matching stored values in arbitrary text ------------------------------
#
# Two callers, one on each side of a tool call: the rule gate refuses a COMMAND
# carrying one of his values, and the runtime scrubs a RESULT that printed one.
# Both run on every call, and a Keychain read is a subprocess each — so the
# values are cached rather than re-read per call.
#
# Holding them in process memory for the session does not move the boundary
# this module states: the ceiling is FileVault plus OS access control, and an
# attacker already running as the user can read the Keychain directly.

_CACHE_TTL = 30.0
_cache: dict = {"names": None, "at": 0.0, "pairs": []}


def _invalidate() -> None:
    _cache.update(names=None, at=0.0, pairs=[])


def _matchable() -> list[tuple[str, str]]:
    """(name, value) for every stored secret long enough to match on.

    Refreshed when the NAME set changes or the TTL lapses. The TTL is what
    covers a ROTATED value that kept its name: nothing on disk changes in that
    case, so a names-only key would keep matching the dead value and miss the
    live one for the rest of the session.
    """
    current = tuple(names())
    now = time.monotonic()
    if _cache["names"] != current or now - _cache["at"] > _CACHE_TTL:
        pairs = []
        for name in current:
            value = get(name)
            if value and len(value) >= MIN_MATCH:
                pairs.append((name, value))
        # Site sign-ins are scrubbed on the same terms and for a sharper
        # reason: a login page that echoes a failed password back into its own
        # error text would otherwise carry it into the model's context, which
        # is the one place this design promises it never goes.
        for origin, secret in _signin_values():
            if len(secret) >= MIN_MATCH:
                pairs.append((f"sign-in for {origin}", secret))
        # Longest first: one secret can be a substring of another, and
        # replacing the short one first would leave a fragment of the long one
        # in the text with its placeholder wrapped around the middle.
        pairs.sort(key=lambda pair: len(pair[1]), reverse=True)
        _cache.update(names=current, at=now, pairs=pairs)
    return _cache["pairs"]


def _signin_values() -> list[tuple[str, str]]:
    """(origin, password) for every stored sign-in.

    Imported lazily: `signin` owns the origin list and depends on this module
    for the Keychain, so a module-level import would be a cycle."""
    try:
        from . import signin

        found = []
        for origin in signin.origins():
            pair = get_signin(origin)
            if pair:
                found.append((origin, pair[1]))
        return found
    except Exception:  # noqa: BLE001 — scrubbing must never raise into a tool
        return []


def contains(text: str) -> bool:
    """Does this text carry one of his stored secrets, verbatim?"""
    if not text or not text.strip():
        return False
    try:
        return any(value in text for _, value in _matchable())
    except Exception:  # noqa: BLE001 — no keychain is "no match", never a crash
        return False


def scrub(text: str) -> str:
    """`text` with every stored secret replaced by a placeholder naming it.

    Naming the secret is deliberate: a bare `***` tells a reader that something
    was removed but not that aish already HOLDS the thing, which is the fact
    that stops the next attempt to go and fetch it by hand.
    """
    if not text:
        return text
    try:
        pairs = _matchable()
    except Exception:  # noqa: BLE001 — a broken keychain must not break a tool
        return text
    for name, value in pairs:
        if value in text:
            text = text.replace(value, f"[secret {name} — redacted by aish]")
    return text


# --- the owner's declared personal values (#343) ----------------------------
#
# The third tier of the typing fence, between NEVER (an IBAN, a card number, a
# password) and FREE (everything else): **ask, by value**. He names the classes
# once — home_address, phone, date_of_birth, email, full_name, whatever set he
# wants — and the browse gate asks before aish types one of them into somebody
# else's form, or lets one ride a composed address.
#
# **It reads the VALUE, never a label.** That is the same ground the two
# refusals stand on: every label check reads what the page WROTE and the page
# can lie about it, while this reads what aish is about to SEND and the page
# cannot touch that. No placeholder, no autocomplete attribute and no page
# language is consulted anywhere in this section.
#
# **These are NOT scrubbed, and that is deliberate.** `scrub` replaces a stored
# value wherever it appears in a tool result, which is right for a credential
# and wrong for his own name: his name is all over the pages he asks aish to
# read, and redacting it would corrupt the answer rather than protect anything.
# So `_matchable` above is untouched by this section — a declared value never
# joins the scrub set, and the fence is the whole of what these buy.

# Latin letters that carry no combining form, so NFKD leaves them whole. Without
# this, "Paweł" folds to "pawe" while "Pawel" folds to "pawel", and a value he
# declared with the diacritic would miss the same value typed without it — the
# exact miss the normalisation exists to prevent.
_TRANSLITERATE = str.maketrans({
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "þ": "th", "Þ": "TH", "ð": "d", "Ð": "D", "ı": "i",
})

# Below this many folded characters a declared value stops being his and starts
# being ordinary text: a four-letter fragment occurs in page text, in a search
# term and in a city name, and every one of those would draw a card. It is a
# chosen floor, not a measured one — the measured fact is the other direction,
# that his own classes clear it comfortably (a date of birth folds to 8, a
# phone to 11, an e-mail past 15).
#
# **Enforced in two places on purpose.** `put_personal` REFUSES a short value,
# so he is told at declaration time instead of keeping a class he believes is
# fenced; and `personal_matches` skips one anyway, so a value stored by any
# other route can never widen the match to everything.
MIN_PERSONAL_MATCH = 6

# The shortest typed field that may count as a PIECE of a declared value when
# the form splits one across several boxes (`personal_tiled`). Two, because a
# house number is two characters and a form that takes it separately is
# ordinary. It is safe where `MIN_PERSONAL_MATCH` would not be, and the reason
# is the tiling: a piece proves nothing on its own — the whole declared value
# still has to be reproduced before anything fires.
MIN_PERSONAL_PIECE = 2

_PERSONAL_TTL = 30.0
_personal_cache: dict = {"names": None, "at": 0.0, "pairs": [], "unreadable": []}


def _invalidate_personal() -> None:
    _personal_cache.update(names=None, at=0.0, pairs=[], unreadable=[])


def fold_value(text: str) -> str:
    """`text` reduced to the form both sides of the match are compared in.

    Case, spacing and diacritics are exactly what differ between the value he
    declared and the value aish is about to type — "+48 601 234 567" against
    "+48601234567", "ul. Lipowa 3/5" against "ul Lipowa 3/5", "Paweł" against
    "Pawel". So: transliterate the letters NFKD cannot decompose, decompose the
    rest and drop the combining marks, casefold, and keep only alphanumerics.

    Deleting the separators from BOTH sides preserves containment — a value that
    matched with its spaces still matches without them — so this is strictly
    more permissive than a whitespace-collapsing fold, which is the safe
    direction for a fence whose false positive costs one card.

    **The residual, stated rather than left to be found:** with separators gone,
    a declared value can match across a word boundary in the haystack. Six
    characters is where that starts being plausible, which is what
    `MIN_PERSONAL_MATCH` is really guarding.

    Alphanumeric rather than ASCII: a value in a non-Latin script keeps its
    characters and is matched on them, instead of folding to nothing and being
    silently unfenced."""
    decomposed = unicodedata.normalize("NFKD", (text or "").translate(_TRANSLITERATE))
    kept = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in kept.casefold() if c.isalnum())


def personal_names() -> list[str]:
    """Names of the declared value classes, sorted. Names, never values."""
    return _read_index(PERSONAL_NAMES_INDEX)


def get_personal(name: str) -> str | None:
    """The declared value, or None if unset."""
    if not valid_name(name):
        return None
    proc = _security(
        ["find-generic-password", "-a", name, "-s", PERSONAL_SERVICE, "-w"]
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")


def put_personal(name: str, value: str) -> None:
    """Declare a value class. Raises SecretError on an invalid name, or on a
    value too short to fence.

    Refusing the short value is the point rather than tidiness: storing it would
    leave him with a class he believes is fenced and a matcher that skips it,
    which is a capability outrunning its record in the least visible
    direction."""
    if not valid_name(name):
        raise SecretError(
            f"invalid value name {name!r} (need [A-Za-z_][A-Za-z0-9_]*)"
        )
    folded = fold_value(value)
    if len(folded) < MIN_PERSONAL_MATCH:
        raise SecretError(
            f"{name!r} is too short to fence — a declared value needs at least "
            f"{MIN_PERSONAL_MATCH} letters or digits once spacing, case and "
            f"accents are normalised away, and this one has {len(folded)}. "
            "Nothing was stored."
        )
    proc = _security(
        ["add-generic-password", "-a", name, "-s", PERSONAL_SERVICE, "-U", "-w", value]
    )
    if proc.returncode != 0:
        raise SecretError(proc.stderr.strip() or "failed to store value")
    _index_add(name, PERSONAL_NAMES_INDEX)
    _invalidate_personal()


def delete_personal(name: str) -> bool:
    """Undeclare a value class; True if it existed."""
    if not valid_name(name):
        return False
    proc = _security(["delete-generic-password", "-a", name, "-s", PERSONAL_SERVICE])
    _index_remove(name, PERSONAL_NAMES_INDEX)
    _invalidate_personal()
    return proc.returncode == 0


def _personal_matchable() -> list[tuple[str, str]]:
    """(class name, FOLDED value) for every declared value long enough to match.

    Cached on the same terms `_matchable` is and for the same reason: the browse
    gate asks this of every value it is about to type, and a Keychain read is a
    subprocess. The TTL is what covers a value he CHANGED without renaming —
    nothing on disk moves in that case, so a names-only key would keep fencing
    the old address for the rest of the session.

    Longest first, so a class that is a substring of another reports the more
    specific one first.

    **A name the index lists and the Keychain will not hand over is UNREADABLE,
    and it is tracked apart from a short one.** They used to be the same answer:
    `get_personal(name) or ""` folds to nothing, which is "too short", so a
    locked Keychain or a refused TCC prompt meant the address was typed FREE,
    with no card and no record — and `aish personal list` then printed "too
    short to match", a cause nothing checked. The never-list has no external
    dependency and this tier does, so its failure direction must not be open."""
    current = tuple(personal_names())
    now = time.monotonic()
    stale = now - _personal_cache["at"] > _PERSONAL_TTL
    if _personal_cache["names"] != current or stale:
        pairs = []
        unreadable = []
        for name in current:
            raw = get_personal(name)
            if raw is None:
                unreadable.append(name)
                continue
            folded = fold_value(raw)
            if len(folded) >= MIN_PERSONAL_MATCH:
                pairs.append((name, folded))
        pairs.sort(key=lambda pair: len(pair[1]), reverse=True)
        _personal_cache.update(
            names=current, at=now, pairs=pairs, unreadable=unreadable
        )
    return _personal_cache["pairs"]


def personal_unreadable() -> list[str]:
    """Declared classes the Keychain would not hand over.

    Non-empty means aish CANNOT TELL whether a value is one of his, which is a
    different fact from "it is not" — the gate fails closed on it and says so,
    rather than typing the value and recording nothing.

    An exception refreshing the cache is answered with every declared name, for
    the same reason: not knowing is not the same as knowing there is nothing."""
    try:
        _personal_matchable()
    except Exception:  # noqa: BLE001 — cannot check is never "nothing to check"
        return list(personal_names())
    return list(_personal_cache["unreadable"])


FENCED = "fenced"
TOO_SHORT = "short"
UNREADABLE = "unreadable"


def personal_status(name: str) -> str:
    """`fenced`, `short` or `unreadable` — what the matcher will actually do
    with this class.

    Three states and not two, because `aish personal list` must not state a
    cause no line checked: a class the Keychain refused is not a class he wrote
    too briefly, and printing the second for the first is exactly the failure
    this codebase keeps having to remove."""
    if name in personal_unreadable():
        return UNREADABLE
    if any(found == name for found, _ in _personal_matchable()):
        return FENCED
    return TOO_SHORT


def personal_words(classes: list[str]) -> str:
    """The declared classes as one phrase, in HIS words.

    The class name is what he typed at `aish personal set`, so the phrase says
    `home address` because he called it `home_address`. Nothing here translates,
    shortens or prettifies it: a vocabulary sitting between his name for his own
    data and the card describing it is one more thing that can be wrong about
    what he is agreeing to.

    Here rather than in `agent.py` because `browse.Batch.card` masks a declared
    value with the same phrase, and `browse` cannot import `agent`."""
    said = [name.replace("_", " ") for name in classes]
    if len(said) <= 1:
        return said[0] if said else ""
    return ", ".join(said[:-1]) + " and " + said[-1]


def personal_matches(text: str) -> list[str]:
    """The NAMES of every declared value class this text carries.

    Names, never values: the caller puts this on a card and into a record, and a
    record that quoted his address would be the leak the fence exists to
    prevent."""
    if not text or not text.strip():
        return []
    folded = fold_value(text)
    if not folded:
        return []
    try:
        return [name for name, value in _personal_matchable() if value in folded]
    except Exception:  # noqa: BLE001 — no keychain is "no match", never a crash
        return []


def personal_tiled(values: list[str]) -> list[str]:
    """Class names whose declared value these fields between them fully COVER.

    **The order-free half of the match, and the reason the ordered one is not
    enough.** Running the typed fields together and looking for the declared
    value inside works only when the form takes them in the order he wrote it:
    street, postcode, city fires and street, city, postcode does not, and the
    UK/US layout is the second one. A surname box before a forename box does not
    fire either, and neither does anything with another field typed in between.
    The MODEL chooses the step order, so a fence that depends on it is a fence
    for one layout out of several.

    So: fold each typed field, and where a field's fold appears inside the
    declared fold, mark the span it covers — every occurrence, since marking one
    could only ever narrow it. Fire when every character of the declared value
    has been covered.

    **It is exactly as precise as the ordered match and no looser**: the whole
    declared value must still be reproduced before anything fires, so a single
    fragment proves nothing and a form that takes half his address is not a
    match. What it drops is only the assumption about ORDER, and about nothing
    else being typed in between.

    Pieces shorter than `MIN_PERSONAL_PIECE` are ignored — a one-character field
    tiles anything given enough boxes, which is the one way this could become a
    match on nothing."""
    pieces = [
        folded for folded in (fold_value(value) for value in values)
        if len(folded) >= MIN_PERSONAL_PIECE
    ]
    if not pieces:
        return []
    try:
        pairs = _personal_matchable()
    except Exception:  # noqa: BLE001 — no keychain is "no match", never a crash
        return []
    found = []
    for name, declared in pairs:
        covered = bytearray(len(declared))
        for piece in pieces:
            at = declared.find(piece)
            while at != -1:
                for index in range(at, at + len(piece)):
                    covered[index] = 1
                at = declared.find(piece, at + 1)
        if all(covered):
            found.append(name)
    return found
