"""Where a piece of content came from, and what that permits (#279).

The taint fence (#277) asks a yes/no question — has anything from outside this
machine entered the task? Some outside content needs a sharper answer than
that, and mail is the first: a link that arrived by e-mail is the delivery
mechanism for every account-recovery flow there is, so aish following one by
itself would hand an injected turn the password-reset button for anything the
owner owns.

Two tiers, and only one of them is a judgement:

- **Structural.** A URL that arrived in mail is never navigated by aish alone.
  That needs no classifier and cannot be evaded by wording.
- **Judged, and it may only RESTRICT.** Mail that reads like a sign-in or a
  password reset has its links refused OUTRIGHT rather than offered on a card,
  because "open the sign-in link" is exactly the card a tired owner taps. Under
  #198's law a scored verdict may restrict and may never license, which is the
  direction this runs: a false positive costs one link he opens himself.

The owner's own argument for why the heuristic is worth having, and it is
right: in the case that MATTERS the attacker triggers a genuine reset at a real
service, so the mail is written by that service and is heavily stereotyped. The
attacker-authored fake is the case a classifier loses, and it is also the case
that is useless to an attacker, because a fake link resets nothing.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# A link, as it appears in a mail body or a JSON field holding one. Trailing
# punctuation is stripped because a URL at the end of a sentence collects it.
_URL_RE = re.compile(r"https?://[^\s<>\"'\\\]}]+", re.I)
_TRAILING = ".,;:!?)]}>\"'"

MAIL = "email"  # the one `content_from` value that means anything so far

# What a sign-in or recovery mail says, in the two languages the owner's mail
# arrives in. Matched against ONE message, never the whole search result, so a
# single reset mail among ten hits does not refuse everybody's links.
_SIGN_IN_PHRASES = (
    # reset / recovery
    "reset your password", "reset password", "password reset",
    "resetowanie hasla", "resetuj haslo", "zresetuj haslo", "zmiana hasla",
    "ustaw nowe haslo", "set a new password", "choose a new password",
    "forgot your password", "zapomniales hasla", "odzyskiwanie hasla",
    "recover your account", "odzyskaj konto",
    # magic links and one-time codes
    "magic link", "sign-in link", "sign in link", "login link",
    "link do logowania", "jednorazowy link", "one-time link",
    "one-time code", "kod jednorazowy", "verification code", "kod weryfikacyjny",
    "your login code", "twoj kod",
    # confirmation of identity
    "confirm your email", "potwierdz swoj adres", "potwierdz adres e-mail",
    "verify your email", "zweryfikuj swoj adres", "activate your account",
    "aktywuj konto", "aktywacja konta", "finish setting up your account",
)


def urls_in(text: str) -> list[str]:
    """Every http(s) URL in this text, in order, de-duplicated."""
    seen: dict[str, None] = {}
    for raw in _URL_RE.findall(text or ""):
        seen.setdefault(raw.rstrip(_TRAILING), None)
    return list(seen)


def messages_in(result: str) -> list[str]:
    """The result, split into one string per message where that is knowable.

    A mail search returns many messages in one blob, and classifying the blob
    would let one reset mail among ten hits refuse everybody's links. The mail
    tools return a JSON array of message objects, so the split is exact there;
    anything else is one message, which is the safe reading — it can only group
    links together, never separate a link from the words that condemn it."""
    text = (result or "").strip()
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except ValueError:
            return [result]
        if isinstance(loaded, list) and loaded:
            return [json.dumps(item, ensure_ascii=False) for item in loaded]
    return [result]


def _fold(text: str) -> str:
    """Lowercased, accent-stripped, whitespace-collapsed.

    Deliberately simpler than `browse.fold`: mail arrives as HTML-ish text
    where a phrase is routinely broken across a tag, so collapsing runs of
    whitespace is what makes 'reset your\\n  password' match at all."""
    lowered = (text or "").casefold()
    for accented, plain in (
        ("ą", "a"), ("ć", "c"), ("ę", "e"), ("ł", "l"), ("ń", "n"),
        ("ó", "o"), ("ś", "s"), ("ź", "z"), ("ż", "z"),
    ):
        lowered = lowered.replace(accented, plain)
    return " ".join(re.sub(r"[<>\\\\/|=_*\\-]+", " ", lowered).split())


def looks_like_sign_in_mail(message: str) -> bool:
    """Does this one message read like a sign-in, reset or activation mail?"""
    folded = _fold(message)
    return any(phrase in folded for phrase in _SIGN_IN_PHRASES)


# What each recorded link is: a link that merely arrived by mail, or one from a
# message that reads like a door into an account.
LINK = "link"
SIGN_IN = "sign-in"


def links_in_mail(result: str) -> dict[str, str]:
    """Every URL this mail result carried, mapped to what it is.

    A link inherits its verdict from the message it sat in, never from its own
    spelling: a reset link is routinely a bare tracking redirect with nothing
    readable in the path, and the words around it are what give it away."""
    found: dict[str, str] = {}
    for message in messages_in(result):
        verdict = SIGN_IN if looks_like_sign_in_mail(message) else LINK
        for url in urls_in(message):
            # SIGN_IN wins: the same URL in two messages is the dangerous one.
            if found.get(url) != SIGN_IN:
                found[url] = verdict
    return found


# ----------------------------------------------- outside artefacts (#319)
#
# aish writes files of its OWN from content that came from outside this
# machine: a markdown rendition of a video's caption track, a text rendition of
# a PDF, the PDF it fetched to render. Each lives in a store inside the
# workspace boundary ON PURPOSE — the producing tool NAMES the file and tells
# the model to grep it, seek in it and read a page at a time, which is the
# whole "read it like a file" design and must not be taken away. #317's repair
# (remove the door) is therefore the wrong one here.
#
# So the door stays open and the fact travels with the artefact instead. This
# is #314's sidecar one layer over, for its reason: whether bytes came from
# outside is known when they are WRITTEN, and re-deriving it later from the
# path or the text is the mistake #294 and #313 each paid for once.
#
# Nothing here is ever named to the model. A handle aish hands the model is the
# model's to NAME and not the model's to CHARACTERISE (#314), and a record the
# model could write is a record that says whatever the model wants it to say.
RECORD_SUFFIX = ".src"


@dataclass(frozen=True)
class ArtefactSource:
    """`tool` wrote these bytes; `outside` says they originated off this
    machine (#277); `source` is the URL or path they were made from; `what`
    names the artefact in the words a reader needs before reading it."""

    tool: str
    outside: bool
    source: str = ""
    what: str = ""


# Bytes in one of those stores with no record beside them: written by an older
# aish, written by a producer that has no write site yet, or a record that did
# not survive. Outside content, because that is the direction that fails safe
# — the alternative is a hostile caption track arriving as a file the owner
# owns, which is the whole issue.
UNKNOWN_ARTEFACT = ArtefactSource(
    tool="",
    outside=True,
    source="",
    what="a file aish put here from something it read elsewhere",
)


def record_path(path: os.PathLike | str) -> Path:
    """The record's own path. The suffix is APPENDED rather than substituted so
    a fetched `x.pdf` and the `x.md` rendered from it keep separate records."""
    return Path(f"{path}{RECORD_SUFFIX}")


def is_record(path: os.PathLike | str) -> bool:
    return str(path).endswith(RECORD_SUFFIX)


def record_artefact(path: os.PathLike | str, source: ArtefactSource) -> None:
    """Write down where an artefact's bytes came from, beside the bytes.

    Best-effort by the same reasoning as the continuation store: a store that
    will not take a record must not raise into the middle of a tool call, and
    an artefact with no record reads as outside content anyway.

    A record is never RELAXED. The same rendition is reachable twice — once
    from a URL and once from a local copy of the identical bytes, since the
    store is keyed on those bytes — and the second read must not be able to
    relabel the first read's source as this machine's own. Rewriting outside →
    outside is fine; the fact has not changed."""
    if not source.outside:
        already = artefact_source(path)
        if already is not None and already.outside:
            return
    try:
        record_path(path).write_text(
            json.dumps(
                {
                    "tool": source.tool,
                    "outside": source.outside,
                    "source": source.source,
                    "what": source.what,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def artefact_source(path: os.PathLike | str) -> ArtefactSource | None:
    """What the record beside `path` says, or None when there is none.

    None is not "these are local bytes": the CALLER knows whether the path sits
    in a store aish populates from outside, and that is what turns None into
    `UNKNOWN_ARTEFACT`. A corrupt record answers None for the same reason — it
    is not a claim anybody made."""
    try:
        record = json.loads(record_path(path).read_text(encoding="utf-8"))
        return ArtefactSource(
            tool=str(record["tool"]),
            outside=bool(record["outside"]),
            source=str(record.get("source", "")),
            what=str(record.get("what", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def forget_artefact(path: os.PathLike | str) -> None:
    """Drop the record with the bytes it describes.

    The record is part of its artefact and never an entry of its own (#314):
    an eviction that took one alone would silently un-attribute bytes that are
    still there, which is this issue again."""
    try:
        record_path(path).unlink()
    except OSError:
        pass
