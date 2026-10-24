"""Zone A, deterministic half — the Gate (#142 W5, slice S1).

Four facts about a mail, each computed by code with no model:

- **Authentication**: what Gmail's OWN Authentication-Results header says. The
  email poller's discipline (`email_poll.dmarc_pass`) carried over and
  tightened: only the TOP-MOST Authentication-Results header is read, and only
  when its authserv-id is Gmail's. A sender can write any number of
  Authentication-Results headers into a message; Gmail prepends its own above
  them, so the top one is the one nobody outside could have written.
- **Structural red flags**: link text that shows one domain over a link to
  another, an IP-literal host, credentials in the URL's userinfo, a punycode
  host, a lookalike of an ADMITTED entity domain, a risky attachment type or a
  double extension. One word list (credential-shaped path words) is matched,
  and it is declared and counted (`docs/vocabularies.md`).
- **Newsletter**: a List-Id or List-Unsubscribe header. A structural fact that
  decides whether the Reader is asked at all.
- **The relationship ledger**: a grade per correspondent computed ONLY from the
  owner's own acts (he wrote to the address, he replied in its conversation, a
  case he opened includes it) plus the current mail's authentication. Inbound
  volume is RECORDED and read by nothing: a sender who mails him a thousand
  times has earned nothing, because volume is the one thing an attacker
  controls completely.

Grades add scrutiny; they never loosen anything (there is nothing to loosen in
S1 — no verdict acts). The JUDGED half of the Gate — a toolless restrict-only
classifier seeing the sender's usual pattern — is S2 work and does not exist
here; in shadow, the Reader's own `suspicion` axis is the only judged signal.

`docs/mail-assistant.md`.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from . import mailindex, vocab

# Gmail's own authserv-id. An Authentication-Results header carrying any other
# id was written by somebody else, and is read by nothing here.
GMAIL_AUTHSERV = "mx.google.com"

PASS = "pass"
FAIL = "fail"
NONE = "none"  # the sender's domain publishes no DMARC policy
UNKNOWN = "unknown"  # no Gmail-written result to read

ESTABLISHED = "established"
KNOWN = "known"
FIRST_CONTACT = "first-contact"
GRADES = (ESTABLISHED, KNOWN, FIRST_CONTACT)

# The two numbers an "established" grade needs. Owner acts on distinct days,
# so one afternoon's back-and-forth is one act; and age, so an address he met
# last week is not yet a relationship.
ESTABLISHED_MIN_ACTS = 2
ESTABLISHED_MIN_AGE_DAYS = 30

# Edit distance at or under which a domain is a lookalike of an admitted one.
LOOKALIKE_DISTANCE = 2

# Path/query words that make a link credential-shaped. Matched against the
# lowercased path+query of every link. A miss PERMITS: in S1 nothing acts, so
# a missed flag costs a verdict record with one fewer flag, and the structural
# flags beside it (text/href mismatch, userinfo, IP host, punycode, lookalike)
# need no words at all.
_CREDENTIAL_WORDS = vocab.declare(
    "mailgate._CREDENTIAL_WORDS",
    (
        "login", "log-in", "signin", "sign-in", "logon", "password", "passwd",
        "verify", "verification", "account/confirm", "reset", "unlock",
        "2fa", "otp", "credential", "webscr", "secure-update",
        "logowanie", "zaloguj", "haslo", "weryfikacja", "potwierdz",
    ),
    languages="EN + PL (URL path words)",
    on_miss=vocab.PERMITS,
    structural="link-text/href domain mismatch, userinfo, IP host, punycode and "
    "lookalike flags — all computed without words",
    note="S1 acts on nothing; a miss is one fewer flag on a shadow verdict.",
)

# Attachment suffixes that execute or wrap something that does. A miss PERMITS
# the same way; the double-extension test beside it needs no list of payloads.
_RISKY_SUFFIXES = vocab.declare(
    "mailgate._RISKY_SUFFIXES",
    (
        ".exe", ".scr", ".com", ".bat", ".cmd", ".pif", ".js", ".jse", ".vbs",
        ".vbe", ".wsf", ".hta", ".msi", ".lnk", ".iso", ".img", ".jar",
        ".ps1", ".docm", ".xlsm", ".pptm", ".html", ".htm", ".svg",
    ),
    languages="file suffixes",
    on_miss=vocab.PERMITS,
    structural="the double-extension test (`invoice.pdf.exe` shape), which "
    "needs no payload list",
)

_DOMAIN_IN_TEXT = re.compile(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})\b", re.I)


# ---------------------------------------------------------------- authentication


@dataclass(frozen=True)
class Auth:
    verdict: str  # pass | fail | none | unknown — DMARC as Gmail reported it
    authenticated: bool
    dmarc: str = ""
    spf: str = ""
    dkim: str = ""
    dkim_domain: str = ""
    header_from: str = ""
    read_from: str = ""  # which header was read: "top" or "" (none usable)
    gateway: str = "gmail"
    why: str = ""  # code-written, for an ending that read no result

    def as_json(self) -> dict[str, Any]:
        out = {
            "verdict": self.verdict,
            "authenticated": self.authenticated,
            "dmarc": self.dmarc,
            "spf": self.spf,
            "dkim": self.dkim,
            "dkim_domain": self.dkim_domain,
            "read_from": self.read_from,
            "gateway": self.gateway,
        }
        if self.why:
            out["why"] = self.why
        return out


# The ending for a mail that came through a gateway with no authentication
# result a reader may trust (`authenticate_header`).
NOT_READABLE = "not_readable_via_gateway"

# Which gateway's own authentication result is readable, and whose authserv-id
# wrote it. A gateway absent here has none: on the Tuta bridge nobody prepends
# a result above the sender's, so the top-most one is the sender's own.
GATEWAY_AUTHSERV = {"gmail": GMAIL_AUTHSERV}


def authenticate_header(header: mailindex.Header) -> Auth:
    """The authentication verdict for one indexed mail, by the gateway its
    ADAPTER declared (`Header.mailbox`) — never inferred from its headers.

    Through a gateway with no readable result the answer is an explicit
    ending, not authenticated, with the reason in words; the headers are not
    looked at even if some survived parsing (`mailindex.from_rfc822` drops
    them). Unauthenticated, the grade is `first-contact` whatever the history,
    and an INBOUND mail opens no case and puts no account in a case's history
    — it mints nothing on its own behalf. His own outbound mail (his Sent
    folder) records his acts whatever the gateway: direction, not
    authentication, decides that (`mailindex.from_rfc822`)."""
    if header.mailbox not in GATEWAY_AUTHSERV:
        return Auth(
            NOT_READABLE, False, gateway=header.mailbox,
            why=f"no authentication result readable via the {header.mailbox} gateway",
        )
    return authenticate(header.auth_results, mailindex.domain_of(header.from_addr))


_RESULT = re.compile(r"\b(dmarc|spf|dkim)\s*=\s*([a-z]+)", re.I)
_DKIM_D = re.compile(r"\bdkim\s*=\s*pass\b[^;]*?header\.(?:d|i)\s*=\s*@?([a-z0-9.-]+)", re.I)
_HEADER_FROM = re.compile(r"\bheader\.from\s*=\s*([a-z0-9.-]+)", re.I)


def aligned(signer: str, from_domain: str) -> bool:
    """Relaxed DKIM alignment: the signer is the From domain or a parent of it.
    A one-label signer (`com`) aligns with nothing."""
    signer, from_domain = signer.lower().strip("."), from_domain.lower().strip(".")
    if not signer or not from_domain or "." not in signer:
        return False
    return from_domain == signer or from_domain.endswith("." + signer)


def authenticate(auth_results: tuple[str, ...], from_domain: str) -> Auth:
    """The authentication verdict from the top-most Authentication-Results.

    `auth_results` is every such header, top-most first (`mailindex._all`).
    Only the first is read, and only when Gmail wrote it.
    """
    if not auth_results:
        return Auth(UNKNOWN, False)
    top = auth_results[0]
    # The authserv-id is the first token before ";" (RFC 8601 lets a version
    # number follow it). EXACT, never a prefix: `mx.google.com.evil.example`
    # is somebody else's server.
    head = top.split(";", 1)[0].split()
    authserv = head[0].lower() if head else ""
    if authserv != GMAIL_AUTHSERV:
        return Auth(UNKNOWN, False)
    found: dict[str, str] = {}
    for method, result in _RESULT.findall(top):
        found.setdefault(method.lower(), result.lower())
    dmarc = found.get("dmarc", "")
    dkim_domain = ""
    match = _DKIM_D.search(top)
    if match:
        dkim_domain = match.group(1).lower().rsplit("@", 1)[-1]
    shown_from = _HEADER_FROM.search(top)
    header_from = shown_from.group(1).lower() if shown_from else ""
    dkim_aligned = found.get("dkim") == PASS and aligned(dkim_domain, from_domain)
    if dmarc == PASS:
        verdict = PASS
    elif dmarc == FAIL:
        verdict = FAIL
    elif dmarc in ("none", ""):
        verdict = NONE
    else:
        verdict = dmarc
    return Auth(
        verdict=verdict,
        # DMARC pass is Gmail's alignment verdict; an aligned DKIM pass under a
        # domain with no DMARC policy authenticates the From domain the same way.
        authenticated=(dmarc == PASS) or (verdict != FAIL and dkim_aligned),
        dmarc=dmarc,
        spf=found.get("spf", ""),
        dkim=found.get("dkim", ""),
        dkim_domain=dkim_domain,
        header_from=header_from,
        read_from="top",
    )


# ---------------------------------------------------------------- red flags


def _host(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return ""
    return (parts.hostname or "").lower()


def _edit_distance(a: str, b: str, cap: int) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _registrable(host: str) -> str:
    """The last two labels — a coarse stand-in for the registrable domain,
    stated as coarse: `shop.co.uk` and `evil.co.uk` both read as `co.uk`. Used
    only for comparing a host against itself (text vs href), never to grant."""
    labels = [x for x in host.lower().strip(".").split(".") if x]
    return ".".join(labels[-2:])


def lookalike_of(domain: str, admitted: set[str] | frozenset[str]) -> str:
    """The admitted domain `domain` imitates, or "". Equal is not a lookalike."""
    domain = domain.lower()
    if not domain or domain in admitted:
        return ""
    for real in sorted(admitted):
        if domain.endswith("." + real):
            continue  # a subdomain of the admitted one is the same owner
        head, real_head = domain.split(".")[0], real.split(".")[0]
        if _edit_distance(domain, real, LOOKALIKE_DISTANCE) <= LOOKALIKE_DISTANCE:
            return real
        if len(real_head) >= 4 and real_head in head and head != real_head:
            return real  # "shop-example-secure" around "example"
    return ""


@dataclass(frozen=True)
class Flag:
    """One structural observation. `what` is a closed name; `detail` is
    code-written (a host, a filename), never mail prose."""

    what: str
    detail: str = ""

    def as_json(self) -> dict[str, str]:
        return {"what": self.what, "detail": self.detail}


def red_flags(header: mailindex.Header, admitted_domains: frozenset[str]) -> tuple[Flag, ...]:
    flags: list[Flag] = []
    from_domain = mailindex.domain_of(header.from_addr)
    imitated = lookalike_of(from_domain, admitted_domains)
    if imitated:
        flags.append(Flag("sender_lookalike", f"{from_domain}~{imitated}"))
    for link in header.links:
        host = _host(link.href)
        if not host:
            continue
        try:
            parts = urllib.parse.urlsplit(link.href)
        except ValueError:
            continue
        if parts.username or parts.password:
            flags.append(Flag("link_userinfo", host))
        try:
            ipaddress.ip_address(host.strip("[]"))
            flags.append(Flag("link_ip_host", host))
        except ValueError:
            pass
        if any(label.startswith("xn--") for label in host.split(".")):
            flags.append(Flag("link_punycode", host))
        shown = _DOMAIN_IN_TEXT.search(link.text or "")
        if shown and _registrable(shown.group(1)) != _registrable(host):
            flags.append(Flag("link_text_mismatch", f"{shown.group(1).lower()}->{host}"))
        path = (parts.path + "?" + parts.query).lower()
        if vocab.hit("mailgate._CREDENTIAL_WORDS", _CREDENTIAL_WORDS, path):
            flags.append(Flag("link_credential_shaped", host))
        imitated = lookalike_of(host, admitted_domains)
        if imitated:
            flags.append(Flag("link_lookalike", f"{host}~{imitated}"))
    for attachment in header.attachments:
        name = attachment.filename.lower().strip()
        suffix = name[name.rfind("."):] if "." in name else ""
        # Exact membership, not `vocab.hit`'s substring test: ".js" is inside ".json".
        risky = suffix in _RISKY_SUFFIXES
        vocab.note("mailgate._RISKY_SUFFIXES", matched=risky)
        if risky:
            flags.append(Flag("attachment_risky_type", attachment.filename[:80]))
        pieces = name.split(".")
        # `.tar.gz` is one compound suffix, not a disguise.
        if (len(pieces) >= 3 and 2 <= len(pieces[-2]) <= 4 and pieces[-2].isalpha()
                and pieces[-2] != "tar"):
            flags.append(Flag("attachment_double_extension", attachment.filename[:80]))
    return tuple(dict.fromkeys(flags))


# ---------------------------------------------------------------- newsletters


@dataclass(frozen=True)
class Newsletter:
    gated: bool
    by: tuple[str, ...] = ()  # which headers said so

    def as_json(self) -> dict[str, Any]:
        return {"gated": self.gated, "by": list(self.by)}


def newsletter(header: mailindex.Header) -> Newsletter:
    """List headers are written by mailing-list software, structurally. Their
    presence gates the Reader (no model call); `Precedence: bulk` alone is
    recorded but does not gate — it is a hint a sender sets by hand."""
    by = []
    if header.list_id.strip():
        by.append("List-Id")
    if header.list_unsubscribe.strip():
        by.append("List-Unsubscribe")
    return Newsletter(bool(by), tuple(by))


# ---------------------------------------------------------------- the ledger


@dataclass(frozen=True)
class Grade:
    grade: str
    acts: int = 0  # owner acts on distinct days, as of
    first_act_at: float | None = None
    age_days: float | None = None
    inbound: int = 0  # RECORDED; no line below reads it
    authenticated: bool = False
    how: tuple[str, ...] = field(default=())  # which act kinds were seen

    def as_json(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "acts": self.acts,
            "first_act_at": self.first_act_at,
            "age_days": None if self.age_days is None else round(self.age_days, 1),
            "inbound": self.inbound,
            "authenticated": self.authenticated,
            "how": list(self.how),
        }


def owner_acts(
    index: mailindex.MailIndex,
    address: str,
    as_of: tuple[float, str],
    owner_case_parties: dict[str, list[float]] | None = None,
) -> list[tuple[float, str]]:
    """Every owner act toward `address` at or before `as_of`, as (at, kind).

    `wrote`: an outbound message addressed to it. `replied`: an outbound
    message in a conversation it took part in. `case`: a case he opened that
    includes it (from the case store, as of). Gmail labels are deliberately
    not acts: a label carries no timestamp, so reading one would let a March
    grade see an April star.
    """
    address = address.lower()
    acts: list[tuple[float, str]] = []
    for header in index.before(as_of):
        if header.direction != "out":
            continue
        if address in header.correspondents:
            acts.append((header.at, "wrote"))
            continue
        # A reply counts only when its OWN In-Reply-To names a message that
        # came from this address. Membership of the same conversation is not
        # enough: a hostile mail can put an owner thread's Message-IDs in its
        # References and join that conversation, and his earlier replies there
        # must not become acts toward it.
        for parent_id in header.in_reply_to:
            parent = index.reply_parent(parent_id, header.position)
            if (
                parent is not None
                and parent.direction == "in"
                and parent.from_addr == address
                and parent.position < header.position
            ):
                acts.append((header.at, "replied"))
                break
    for at in (owner_case_parties or {}).get(address, ()):
        if at <= as_of[0]:
            acts.append((at, "case"))
    return sorted(acts)


def grade(
    index: mailindex.MailIndex,
    header: mailindex.Header,
    auth: Auth,
    owner_case_parties: dict[str, list[float]] | None = None,
) -> Grade:
    """The sender's grade as of this mail. Owner acts and authentication only."""
    address = header.from_addr
    as_of = header.position
    inbound = sum(
        1 for h in index.before(as_of) if h.direction == "in" and h.from_addr == address
    )
    acts = owner_acts(index, address, as_of, owner_case_parties)
    days = sorted({int(at // 86400) for at, _kind in acts})
    first = acts[0][0] if acts else None
    age = (header.at - first) / 86400 if first is not None else None
    how = tuple(sorted({kind for _at, kind in acts}))
    if not auth.authenticated or not days:
        # Unauthenticated, the From address is a claim, and a claim cannot
        # inherit somebody else's history.
        level = FIRST_CONTACT
    elif (
        len(days) >= ESTABLISHED_MIN_ACTS
        and age is not None
        and age >= ESTABLISHED_MIN_AGE_DAYS
    ):
        level = ESTABLISHED
    else:
        level = KNOWN
    return Grade(level, len(days), first, age, inbound, auth.authenticated, how)
