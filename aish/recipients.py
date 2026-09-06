"""Who an outbound message can REACH, and what that licenses.

The one question behind **recipient-scoped autonomy** (#160 follow-up, #377):
a mail whose every recipient is verifiably the owner cannot leave his own
control, so it carries none of the exfiltration risk the approval card exists
for, and it runs with no card.

**That safety is a property of the ACTION, not of who is watching** — which is
why this lives here and not in `server.py`. It began as one conjunct of the
triggered-session policy (`origin != "user" and _triggered_safe(...)`), so
mailing the owner needed no approval when the email poller composed it and DID
need one when the owner asked for it in his own chat (#377). The exemption's
justification was "there is no human to answer a card", but that is an argument
about the card, never about the send. Worse, it pointed the wrong way: prompt
injection lives on the UNATTENDED path, and that was the path being trusted.
A card on a zero-consequence action is not free — it is what teaches the owner
to tap through the cards that matter.

Both surfaces (`server.py`, `cli.py`) import this, so there is ONE definition of
what an owner address is and one parser deciding what a field routes to. The
strictness is the whole asset: everything here fails CLOSED, because the caller's
fallback is the approval card and never a silent send.
"""

from __future__ import annotations

import os
import re
from email.utils import formataddr, getaddresses

# The OWNER's own addresses. Note what is NOT here: `aish@wenda.eu` and
# `bot@wenda.eu` are the BOT's mailbox — the one `email_poll.py` reads back in —
# so mail addressed there is aish talking to itself, not aish answering him, and
# it holds for a card like any other recipient. Override with
# AISH_OWNER_ADDRESSES (comma-separated).
OWNER_ADDRESSES = frozenset(
    a.strip().lower()
    for a in os.environ.get(
        "AISH_OWNER_ADDRESSES", "pawel@wenda.eu,pawel@wenda.email"
    ).split(",")
    if a.strip()
)


def parse(field: str) -> list[str] | None:
    """Every address a recipient header field routes to, or None when the field
    does not parse CLEANLY — the security rule (#178 P0-3): a decision about
    what a string IS must never be made by a regex that finds things IN it.
    The old `findall` approach saw the owner inside `"pawel@wenda.eu"@evil.com`
    (a valid RFC 5322 quoted local-part routed to evil.com) and concluded the
    send was owner-only. This parses with email.utils.getaddresses and then
    rejects anything exotic: a parse-failure pair, a quoted local-part, a
    local-part containing `@`, or a field that re-serializing the parsed
    addresses does not reproduce (residue = something escaped the parse, which
    lenient/older parsers are known to do). Rejection is safe — the caller
    falls through to the approval card, never to an auto-send."""
    pairs = getaddresses([field])
    if not pairs:
        return None
    addrs: list[str] = []
    for _display, addr in pairs:
        if not addr:
            return None  # ('', '') is getaddresses' malformed-input marker
        local, sep, domain = addr.rpartition("@")
        if not sep or not local or not domain:
            return None
        if "@" in local or '"' in addr or "'" in addr:
            return None  # quoted/multi-@ local-parts route elsewhere than they read
        addrs.append(addr)
    # Residue check: rebuilding the field from what we parsed must reproduce it
    # (whitespace/comma spacing normalized). Anything dropped or reinterpreted
    # by the parser fails the comparison and the send holds for approval.
    normalize = lambda s: re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", s.strip()))  # noqa: E731
    rebuilt = ", ".join(formataddr(pair) for pair in pairs)
    if normalize(field) != normalize(rebuilt):
        return None
    return addrs


def all_owner(args: dict) -> bool:
    """True iff the send has at least one recipient and every address across
    to/cc/bcc is an owner address. A reply (reply_to_msg_id) is NEVER auto-safe,
    even with an explicit owner `to`: a threaded reply ALSO goes to the original
    message's sender, which is not verifiable from args (if the replied-to
    message is from a third party, an owner-looking `to` would still exfiltrate
    to them). An autonomous owner answer must therefore be a NEW email addressed
    explicitly to an owner address — the model is told this in the trigger.
    Any field that does not parse cleanly (see `parse`) counts as not-owner, so
    a malformed/adversarial recipient holds instead of sending."""
    if args.get("reply_to_msg_id"):
        return False
    recips: list[str] = []
    for field in ("to", "cc", "bcc"):
        val = args.get(field)
        if val is None or val == "" or val == []:
            continue
        text = ", ".join(str(v) for v in val) if isinstance(val, list) else str(val)
        parsed = parse(text)
        if parsed is None:
            return False
        recips += parsed
    if not recips:
        return False
    return all(addr.lower() in OWNER_ADDRESSES for addr in recips)


# The audit reason a caller records when this licenses a run. It names the
# POLICY, not the origin: `auto (user)` would say a human decided, which is the
# one thing that did not happen.
OWNER_ONLY = "owner-only recipients"


def owner_scoped_send(name: str, args: dict) -> bool:
    """Whether this tool call is a mail that can only ever reach the owner —
    auto-safe in EVERY session, attended or triggered (#377).

    The recipient check applies to DRAFTS too (#178 P0-3): a draft addressed to
    a third party is a fully-staged exfiltration one mistaken tap from sending,
    so it stays draftable but through the card, never silently."""
    return name == "gmail_send" and all_owner(args)
