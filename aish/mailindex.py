"""aish's OWN thread index over the owner's mail (#142 W5, slice S1).

The Reader is handed "the whole conversation, both directions", and that
conversation has to come from somewhere aish controls. Provider threading is
never trusted: the Tuta gateway renumbers UIDs on restart without bumping
UIDVALIDITY and breaks its own threads, and Gmail's `threadId` is a provider
opinion that changes when a subject does. So conversations are reconstructed
here from the headers the mail itself carries — Message-ID, In-Reply-To,
References — with one narrow fallback for mail whose reply headers were
stripped.

**Identity is the Message-ID or the content, never a provider id.** A message
is keyed by `digest`: sha256 of its normalized Message-ID, or — when it has
none — of its sender, Date header, subject and body. Feeding the same message
twice under two provider ids is a no-op, which is what makes re-processing
idempotent and a renumbering gateway harmless.

**Nothing here sees the future.** Every query takes `as_of`, the reviewed
mail's own `(at, digest)` position, and answers from the messages at or before
it only. The backtest builds the whole index first and then asks it questions
"as of" each mail; without this, the Reader for a March mail would read his
April reply.

**Persistence: one file per message, one writer, contained failure.** Under
`<state>/mail/index/`, each message its own JSON file written through
`atomic_write`. Conversations are recomputed in memory at load and never
stored, so there is no thread id on disk to go stale. A file that cannot be
read costs that message and is COUNTED (`unreadable`); it never takes the
index down — the rule one corrupt session log taught aish-web.

Pure: no server imports, no network, no model. `docs/mail-assistant.md`.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import email.policy
import email.utils
import hashlib
import html
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import atomic_write, evidence, vocab

INDEX_DIRNAME = "index"

# Reply-header-stripped mail is linked to an earlier message only inside this
# window, and only when its subject carries a reply prefix. Notification mail
# ("Your order has shipped") repeats subjects across unrelated orders, so a
# subject match WITHOUT a reply prefix links nothing.
FALLBACK_WINDOW_DAYS = 30

# What a stored body may hold. A mail body is the owner's material; the cap
# bounds what one hostile message can make every later bundle carry.
BODY_CHARS = 20000

# How deep and how wide a MIME tree is walked. Real mail nests a handful of
# levels; the caps bound what a hostile one can make the parser do.
MAX_MIME_DEPTH = 32
MAX_MIME_PARTS = 500

# The words mail clients put in front of a reply's or a forward's subject.
# Consulted only for a message with NO reply headers, to decide whether the
# subject fallback may link it at all: a miss BREAKS that fallback quietly (the
# message stands alone), and most such messages are not replies, so it is not
# demanded.
_REPLY_PREFIXES = vocab.declare(
    "mailindex._REPLY_PREFIXES",
    ("re", "aw", "odp", "fwd", "fw", "wg", "tr", "sv", "vs"),
    languages="EN + PL + DE + FR + Nordic (client prefixes)",
    on_miss=vocab.BREAKS,
    demanded=False,
    structural="In-Reply-To / References, read first; the prefix is consulted only "
    "when both are absent",
)
_REPLY_PREFIX = re.compile(
    r"^\s*(?:(?:" + "|".join(_REPLY_PREFIXES) + r")\s*(?:\[\d+\])?\s*:\s*)+", re.I
)
_LIST_TAG = re.compile(r"^\s*\[[^\]]{1,40}\]\s*")
_MSGID = re.compile(r"<([^<>\s]+)>")
_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def mail_dir(state_dir: os.PathLike | str) -> Path:
    return Path(state_dir) / "mail"


def normalize_message_id(raw: str) -> str:
    raw = (raw or "").strip()
    found = _MSGID.search(raw)
    value = found.group(1) if found else raw.strip("<>").strip()
    return value.lower() if value and " " not in value else ""


def message_ids(raw: str) -> tuple[str, ...]:
    """Every Message-ID in a References / In-Reply-To header, in order."""
    out = [m.lower() for m in _MSGID.findall(raw or "")]
    if not out and raw and raw.strip() and " " not in raw.strip():
        out = [raw.strip().strip("<>").lower()]
    return tuple(dict.fromkeys(out))


def normalize_subject(subject: str) -> str:
    """The subject with reply/forward prefixes and list tags removed, casefolded."""
    text = subject or ""
    for _ in range(4):
        before = text
        text = _REPLY_PREFIX.sub("", text)
        text = _LIST_TAG.sub("", text)
        if text == before:
            break
    return " ".join(text.split()).casefold()


def has_reply_prefix(subject: str, *, count: bool = True) -> bool:
    """`count=False` for a RE-decision (an index reload): the counter is about
    how often a message needed the fallback, not how often the index loaded."""
    found = bool(_REPLY_PREFIX.match(subject or ""))
    if count:
        vocab.note("mailindex._REPLY_PREFIXES", matched=found)
    return found


def address_of(value: str) -> str:
    """The bare, lowercased address in a From/To value, or ""."""
    _name, addr = email.utils.parseaddr(value or "")
    if addr and "@" in addr:
        return addr.strip().lower()
    found = _ADDRESS.search(value or "")
    return found.group(0).lower() if found else ""


def display_name(value: str) -> str:
    name, _addr = email.utils.parseaddr(value or "")
    return " ".join(name.split())


def addresses_of(value: str) -> tuple[str, ...]:
    pairs = email.utils.getaddresses([value or ""])
    out = [a.strip().lower() for _n, a in pairs if a and "@" in a]
    return tuple(dict.fromkeys(out))


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


@dataclass(frozen=True)
class Link:
    """One link in a body: where it goes, and what its visible text said."""

    href: str
    text: str = ""


@dataclass(frozen=True)
class Attachment:
    filename: str
    mime: str = ""


@dataclass(frozen=True)
class Header:
    """One message, as the index keeps it.

    `at` is the PROVIDER's receipt time (Gmail `internalDate`) when there is
    one, because the Date header is written by the sender and can say anything;
    `at_source` records which it was. `provider_id` is kept for fetching the
    message again and is never a key.
    """

    digest: str
    message_id: str
    in_reply_to: tuple[str, ...]
    references: tuple[str, ...]
    subject: str
    from_value: str
    from_addr: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    at: float
    at_source: str
    date_header: str
    direction: str  # "in" | "out"
    list_id: str = ""
    list_unsubscribe: str = ""
    precedence: str = ""
    auth_results: tuple[str, ...] = ()
    body_digest: str = ""
    body_chars: int = 0
    links: tuple[Link, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    provider_id: str = ""
    # Which gateway this message came through, DECLARED by the adapter that
    # parsed it (`from_gmail` / `from_rfc822`), never inferred from a header.
    # The Gate reads it to decide whose authentication result, if any, exists
    # (`mailgate.authenticate_header`). Index files written before it existed
    # were all Gmail.
    mailbox: str = "gmail"
    # The gateway's own authentication header, as the ADAPTER asked the parse
    # to observe it (`from_rfc822(first_header=...)`): its value only when it
    # was the raw message's FIRST header line and the only copy in the header
    # block, and code's words for what was seen (`first_header_observation`).
    # Empty on Gmail and on index files written before it existed.
    gateway_auth: str = ""
    gateway_auth_observed: str = ""

    @property
    def correspondents(self) -> tuple[str, ...]:
        """The other party: the sender of inbound mail, the recipients of outbound."""
        if self.direction == "out":
            return tuple(dict.fromkeys(self.to + self.cc))
        return (self.from_addr,) if self.from_addr else ()

    @property
    def position(self) -> tuple[float, str]:
        return (self.at, self.digest)

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def header_from_json(raw: dict[str, Any]) -> Header:
    data = dict(raw)
    for name in ("in_reply_to", "references", "to", "cc", "auth_results"):
        data[name] = tuple(data.get(name) or ())
    data["links"] = tuple(Link(**link) for link in data.get("links") or ())
    data["attachments"] = tuple(Attachment(**a) for a in data.get("attachments") or ())
    data["at"] = float(data["at"])
    return Header(**data)


def header_path(state_dir: os.PathLike | str, digest: str) -> Path:
    """Where the index keeps one message — `MailIndex._path`'s one definition."""
    return mail_dir(state_dir) / INDEX_DIRNAME / digest[:2] / f"{digest}.json"


def load_header(state_dir: os.PathLike | str, digest: str) -> Header | None:
    """One message's header, read straight from its own file, or None.

    The owner surface (`/mail`) joins verdicts to the index by digest through
    this, rather than loading the whole index: it reads only the rows it shows.
    Contained per file — an unreadable one is None, never a raise."""
    if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        return None
    try:
        header = header_from_json(json.loads(header_path(state_dir, digest).read_text()))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    return header if header.digest == digest else None


def digest_for(
    message_id: str, from_addr: str, date_header: str, subject: str, body: str,
    *, gateway: str = "gmail",
) -> str:
    """The content address of a message: its Message-ID, else its content.

    **Through a gateway that cannot authenticate (anything but Gmail), the
    Message-ID is not an identity**: a sender writes it, and a Tuta mail
    carrying a real Gmail mail's Message-ID would otherwise BE that mail to
    the index and the verdict store — first copy wins, so an earlier forged
    Date would squat it. So a non-Gmail message is addressed by its gateway,
    Message-ID AND content together, and can never collide with a Gmail
    digest. The cost, stated: a mail delivered to both inboxes is two entries
    (and two readings); the conversation union still joins them by
    Message-ID."""
    if gateway != "gmail":
        blob = json.dumps(
            [gateway, message_id, from_addr, date_header.strip(), subject.strip(),
             evidence.digest_of(body)],
            ensure_ascii=False,
        )
        return hashlib.sha256(f"gateway:{blob}".encode()).hexdigest()
    if message_id:
        return hashlib.sha256(f"mid:{message_id}".encode()).hexdigest()
    blob = json.dumps(
        [from_addr, date_header.strip(), subject.strip(), evidence.digest_of(body)],
        ensure_ascii=False,
    )
    return hashlib.sha256(f"content:{blob}".encode()).hexdigest()


# ---------------------------------------------------------------- Gmail shape


def _gmail_headers(msg: dict) -> list[tuple[str, str]]:
    payload = msg.get("payload") or {}
    return [
        (str(h.get("name") or ""), str(h.get("value") or ""))
        for h in payload.get("headers") or ()
        if isinstance(h, dict)
    ]


def _first(headers: list[tuple[str, str]], name: str) -> str:
    return next((v for n, v in headers if n.lower() == name.lower()), "")


def _all(headers: list[tuple[str, str]], name: str) -> tuple[str, ...]:
    """Every header of this name, TOP-MOST first — the order Gmail returns them,
    which is the order the receiving servers prepended them."""
    return tuple(v for n, v in headers if n.lower() == name.lower())


def _decode(data: str) -> str:
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _walk(root: dict, plain: list[str], htmls: list[str], files: list[Attachment]) -> None:
    """Every MIME part, depth-first, iteratively and bounded: a hostile mail
    nesting thousands of parts must cost its own parse, not the pass."""
    stack: list[tuple[dict, int]] = [(root, 0)]
    seen = 0
    while stack and seen < MAX_MIME_PARTS:
        part, depth = stack.pop()
        seen += 1
        mime = str(part.get("mimeType") or "").lower()
        filename = str(part.get("filename") or "")
        body = part.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        if filename:
            files.append(Attachment(filename, mime))
        elif mime == "text/plain":
            plain.append(_decode(str(body.get("data") or "")))
        elif mime == "text/html":
            htmls.append(_decode(str(body.get("data") or "")))
        if depth >= MAX_MIME_DEPTH:
            continue
        children = part.get("parts") or ()
        if isinstance(children, list):
            stack.extend((c, depth + 1) for c in reversed(children) if isinstance(c, dict))


_ANCHOR = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_BARE_URL = re.compile(r"\bhttps?://[^\s<>\"')\]]+", re.I)


def html_to_text(markup: str) -> str:
    text = _SCRIPT.sub(" ", markup)
    text = re.sub(r"<br\s*/?>|</p\s*>|</div\s*>|</tr\s*>", "\n", text, flags=re.I)
    text = html.unescape(_TAG.sub(" ", text))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def extract_links(plain: str, markup: str) -> tuple[Link, ...]:
    links: list[Link] = []
    for href, inner in _ANCHOR.findall(markup or ""):
        visible = " ".join(html.unescape(_TAG.sub(" ", inner)).split())
        links.append(Link(html.unescape(href).strip(), visible[:200]))
    anchored = {link.href for link in links}
    for url in _BARE_URL.findall(plain or ""):
        if url not in anchored:
            links.append(Link(url, ""))
    return tuple(dict.fromkeys(links))


@dataclass(frozen=True)
class Parsed:
    """A Gmail message read into what the index keeps, plus its body text and
    the provider's labels — which the BACKTEST reads as ground truth and the
    index never stores (a label has no timestamp, so it cannot be "as of")."""

    header: Header
    body: str
    labels: tuple[str, ...]


def from_gmail(msg: dict, owner_addresses: frozenset[str] | set[str]) -> Parsed | None:
    """One `gws gmail users messages get` response (format full or metadata),
    or None when it cannot be read — which every caller COUNTS as unparseable.

    Contained per mail: the payload is written by a stranger, and one mail
    whose Date header or MIME tree breaks the parser must cost that mail and
    nothing else — never the pass, and never every later backtest over a
    window it sits in."""
    try:
        return _from_gmail(msg, owner_addresses)
    except Exception:  # noqa: BLE001 — containment is the point
        return None


def _from_gmail(msg: dict, owner_addresses: frozenset[str] | set[str]) -> Parsed | None:
    if not isinstance(msg, dict):
        return None
    headers = _gmail_headers(msg)
    plain: list[str] = []
    htmls: list[str] = []
    files: list[Attachment] = []
    payload = msg.get("payload") or {}
    if isinstance(payload, dict):
        _walk(payload, plain, htmls, files)
    labels = tuple(str(x) for x in msg.get("labelIds") or ())
    internal = str(msg.get("internalDate") or "")
    header = _build_header(
        headers, plain, htmls, files,
        provider_at=int(internal) / 1000.0 if internal.isdigit() else None,
        # Direction is Gmail's OWN evidence: its SENT label. A From header
        # naming him is something any sender can write; on its own it makes
        # nothing his (a forged From: him in his inbox stays "in", and the
        # Gmail source counts it — `mailsource.GmailSource.fetch`).
        direction="out" if "SENT" in labels else "in",
        # Gmail prepends its own result above any the sender wrote; the Gate
        # reads only the top-most and only when Gmail wrote it.
        auth_results=_all(headers, "Authentication-Results"),
        provider_id=str(msg.get("id") or ""),
        mailbox="gmail",
    )
    return Parsed(header, _body_of(plain, htmls), labels)


def date_header_time(value: str) -> float | None:
    """A Date header as a timestamp, or None. RFC 5322's `-0000` zone means
    "UTC, local offset unknown", and `parsedate_to_datetime` returns it NAIVE;
    a naive value is read as UTC here, never as this machine's local time."""
    if not value or not value.strip():
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        # A garbage Date is the sender's to write; it must cost the mail its
        # date (`at_source` "none"), never the mail itself.
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.timestamp()


def _body_of(plain: list[str], htmls: list[str]) -> str:
    body = "\n".join(p for p in plain if p.strip()) or html_to_text("\n".join(htmls))
    return body[:BODY_CHARS]


def _build_header(
    headers: list[tuple[str, str]],
    plain: list[str],
    htmls: list[str],
    files: list[Attachment],
    *,
    provider_at: float | None,
    direction: str,
    auth_results: tuple[str, ...],
    provider_id: str,
    mailbox: str,
) -> Header:
    """The one place a parsed message becomes a `Header`, whichever gateway it
    came through. What differs by gateway is passed IN by the adapter: the
    receipt time it vouches for, the direction it determined, and which
    authentication results (if any) a reader may trust."""
    body = _body_of(plain, htmls)
    from_value = _first(headers, "From")
    from_addr = address_of(from_value)
    message_id = normalize_message_id(_first(headers, "Message-ID"))
    subject = _first(headers, "Subject")
    date_header = _first(headers, "Date")
    if provider_at is not None:
        at, at_source = provider_at, "provider"
    else:
        stamp = date_header_time(date_header)
        at = stamp if stamp is not None else 0.0
        at_source = "date_header" if stamp is not None else "none"
    return Header(
        digest=digest_for(message_id, from_addr, date_header, subject, body, gateway=mailbox),
        message_id=message_id,
        in_reply_to=message_ids(_first(headers, "In-Reply-To")),
        references=message_ids(_first(headers, "References")),
        subject=subject,
        from_value=from_value,
        from_addr=from_addr,
        to=addresses_of(", ".join(_all(headers, "To"))),
        cc=addresses_of(", ".join(_all(headers, "Cc"))),
        at=at,
        at_source=at_source,
        date_header=date_header,
        direction=direction,
        list_id=_first(headers, "List-Id"),
        list_unsubscribe=_first(headers, "List-Unsubscribe"),
        precedence=_first(headers, "Precedence"),
        auth_results=auth_results,
        body_chars=len(body),
        links=extract_links("\n".join(plain), "\n".join(htmls)),
        attachments=tuple(files),
        provider_id=provider_id,
        mailbox=mailbox,
    )


# ---------------------------------------------------------------- RFC 5322 shape


_FOLD = re.compile(r"\r?\n[ \t]+")

# `first_header_observation`'s one positive word. Anything else it says is a
# reason the header is not the gateway's own.
FIRST_AND_ONLY = "the first header line, the only copy"


def first_header_observation(raw: str, name: str, parsed_count: int) -> tuple[str, str]:
    """(value, observed) for a header a gateway promises to write as the FIRST
    header line of every message, after stripping every copy a sender wrote.

    The promise is checked, not trusted: the value is returned only when the
    raw message's very first line is that header AND exactly one copy exists
    anywhere in the header block — counted on the raw lines (a name followed
    by optional blanks and a colon, any case) and again by the email parser
    (`parsed_count`), so a copy either reading misses is still a second copy.
    A copy in the BODY is not a header and is never read. `observed` says what
    was seen, in code's words; the value is "" whenever it is not
    `FIRST_AND_ONLY`."""
    head = re.split(r"\r?\n\r?\n", raw, maxsplit=1)[0]
    lines = re.split(r"\r?\n", head)
    starts = re.compile(r"^" + re.escape(name) + r"[ \t]*:", re.I)
    copies = [n for n, line in enumerate(lines) if starts.match(line)]
    count = max(len(copies), parsed_count)
    if not copies and not parsed_count:
        return "", f"no {name} header"
    if count > 1:
        return "", f"{count} copies of {name} in the header block"
    if copies != [0]:
        where = f"header line {copies[0] + 1}" if copies else "a place the raw scan did not find"
        return "", f"{name} is not the first header line (it is at {where})"
    value = lines[0].split(":", 1)[1]
    for line in lines[1:]:
        if line[:1] not in (" ", "\t"):
            break
        value += " " + line.strip()
    return " ".join(value.split()), FIRST_AND_ONLY


def _part_text(part: Any) -> str:
    try:
        content = part.get_content()
    except (LookupError, ValueError, AssertionError, KeyError):
        raw = part.get_payload(decode=True) or b""
        content = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    return content if isinstance(content, str) else ""


def from_rfc822(
    raw: str,
    *,
    provider_id: str,
    in_sent_folder: bool,
    mailbox: str,
    first_header: str = "",
) -> Parsed | None:
    """One raw RFC 5322 message from a gateway that is NOT Gmail (the Tuta
    bridge, via himalaya), or None — counted unparseable — when it cannot be
    read. Contained per mail, as `from_gmail` is.

    **Every Authentication-Results header is dropped here, at parse time.**
    On Gmail the top-most one is Gmail's own, prepended above anything the
    sender wrote; through this gateway nothing is prepended, so the top-most
    one IS the sender's, and a sender-written exact copy of Gmail's would read
    as authenticated. There is no such result here a reader may trust, so none
    is kept.

    **`first_header` is the adapter's declaration** of the header its gateway
    writes first on every message (tutabridge's `X-Tutabridge-Authentication`).
    Only when the adapter names one is it observed — position and copy count
    on the raw text (`first_header_observation`) — and the Gate's authenticator
    for that gateway reads only the observation (`mailgate.authenticate_header`).
    A header naming itself is never enough: the name comes from the adapter.

    **Direction is the FOLDER**: out iff the mail sits in his Sent folder. A
    From header naming an owner address is something any sender can write;
    on this gateway it makes nothing "his", so it can never become an owner
    act in the ledger (`mailgate.owner_acts`). His addresses are not consulted
    for direction at all here.

    **`at` is the Date header** (no gateway receipt time is read), so it is
    sender-written; `at_source` says so on every record."""
    try:
        return _from_rfc822(raw, provider_id=provider_id, in_sent_folder=in_sent_folder,
                            mailbox=mailbox, first_header=first_header)
    except Exception:  # noqa: BLE001 — containment is the point
        return None


def _from_rfc822(
    raw: str, *, provider_id: str, in_sent_folder: bool, mailbox: str, first_header: str = ""
) -> Parsed | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    msg = email.message_from_string(raw, policy=email.policy.default)
    gateway_auth = gateway_auth_observed = ""
    if first_header:
        gateway_auth, gateway_auth_observed = first_header_observation(
            raw, first_header, len(msg.get_all(first_header) or ()))
    headers = [
        (str(name), _FOLD.sub(" ", str(value)))
        for name, value in msg.items()
        if str(name).lower() != "authentication-results"
    ]
    plain: list[str] = []
    htmls: list[str] = []
    files: list[Attachment] = []
    for seen, part in enumerate(msg.walk()):
        if seen >= MAX_MIME_PARTS:
            break
        if part.is_multipart():
            continue
        mime = part.get_content_type().lower()
        filename = part.get_filename() or ""
        if filename:
            files.append(Attachment(str(filename), mime))
        elif mime == "text/plain":
            plain.append(_part_text(part))
        elif mime == "text/html":
            htmls.append(_part_text(part))
    header = _build_header(
        headers, plain, htmls, files,
        provider_at=None,
        direction="out" if in_sent_folder else "in",
        auth_results=(),
        provider_id=provider_id,
        mailbox=mailbox,
    )
    if first_header:
        header = _replace(header, gateway_auth=gateway_auth,
                          gateway_auth_observed=gateway_auth_observed)
    return Parsed(header, _body_of(plain, htmls), ())


# ---------------------------------------------------------------- the index


class _Union:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:
            self.parent[key], key = root, self.parent[key]
        return root

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass
class Health:
    """What loading the index met. Counted, never silent."""

    loaded: int = 0
    unreadable: list[str] = field(default_factory=list)
    # A message arriving under a digest already held, with a different sender
    # or subject: two messages claiming one Message-ID. The first is kept; the
    # second is counted here rather than dropped without a word.
    collisions: list[str] = field(default_factory=list)
    # Replies whose In-Reply-To names a Message-ID two DIFFERENT senders'
    # messages claim (`MailIndex.reply_parent`); resolved to no parent.
    ambiguous_parents: int = 0


class MailIndex:
    """The thread index. One instance is the one code writer of its directory.

    `state_dir=None` keeps it in memory only (the backtest's scratch arm and
    the tests); the conversation logic is the same either way.

    **Threads are rebuilt as of the question, never filtered after it.** A
    later reply whose References name messages from two earlier conversations
    joins them; an index that unioned everything and then hid later messages
    from the RESULT would still hand an earlier mail the joined structure —
    structure only the future created. So the links each message carries are
    stored, and a conversation is the union over messages at or before `as_of`
    only. The subject fallback is decided at admission against the messages
    earlier than the one admitted — so it depends on nothing later — and is
    re-decided the same way at every load (see `_load`).
    """

    def __init__(self, state_dir: os.PathLike | str | None = None) -> None:
        self.directory = mail_dir(state_dir) / INDEX_DIRNAME if state_dir is not None else None
        self.state_dir = state_dir
        self.by_digest: dict[str, Header] = {}
        # Message-ID -> every digest that claims it. Not a key: through a
        # gateway that cannot authenticate, two messages may claim one id
        # (`digest_for`), so a reply's parent is resolved by `reply_parent`.
        self.mid_members: dict[str, list[str]] = {}
        self.linked_by: dict[str, str] = {}
        self.fallback: dict[str, str] = {}  # digest -> the earlier digest it was linked to
        # With no state dir there is no evidence store to hold bodies, so an
        # in-memory index keeps them itself (keyed by body digest).
        self._bodies: dict[str, str] = {}
        self.health = Health()
        self._cache: dict[tuple[float, str], _Union] = {}
        if self.directory is not None and self.directory.is_dir():
            self._load()

    # -- persistence

    def _path(self, digest: str) -> Path:
        assert self.state_dir is not None
        return header_path(self.state_dir, digest)

    def _load(self) -> None:
        assert self.directory is not None
        loaded: list[Header] = []
        for path in sorted(self.directory.glob("*/*.json")):
            try:
                header = header_from_json(json.loads(path.read_text()))
            except (OSError, ValueError, TypeError, KeyError):
                self.health.unreadable.append(path.name)
                continue
            if header.digest != path.stem:
                self.health.unreadable.append(path.name)
                continue
            loaded.append(header)
        # Re-admitted in position order. The subject fallback is RE-decided
        # here against the messages earlier than each one — so a mail fetched
        # in a later pass but older than others can change a link. A verdict
        # already recorded keeps the bundle it was given (its bytes are in the
        # evidence store); it is never recomputed from today's index.
        for header in sorted(loaded, key=lambda h: h.position):
            self._admit(header, count=False)
        self.health.loaded = len(loaded)

    # -- writing

    def add(self, header: Header, body: str = "") -> bool:
        """Admit one message. False when it was already here (idempotent).

        The body goes to the evidence store, which is purgeable by contract;
        the index keeps only its digest.
        """
        held = self.by_digest.get(header.digest)
        if held is not None:
            if (held.from_addr, held.subject) != (header.from_addr, header.subject):
                self.health.collisions.append(header.digest)
            return False
        if body and not header.body_digest:
            digest = evidence.put(body, self.state_dir)
            if self.state_dir is None:
                self._bodies[digest] = body
            header = _replace(header, body_digest=digest, body_chars=len(body))
        if self.directory is not None:
            atomic_write.publish(
                self._path(header.digest),
                json.dumps(header.as_json(), ensure_ascii=False, sort_keys=True),
            )
        self._admit(header)
        return True

    def _admit(self, header: Header, count: bool = True) -> None:
        self.by_digest[header.digest] = header
        self._cache.clear()
        if header.message_id:
            members = self.mid_members.setdefault(header.message_id, [])
            if header.digest not in members:
                members.append(header.digest)
        if header.in_reply_to or header.references:
            self.linked_by[header.digest] = "headers"
        elif has_reply_prefix(header.subject, count=count):
            match = self._fallback_parent(header)
            if match is not None:
                self.fallback[header.digest] = match.digest
                self.linked_by[header.digest] = "subject_fallback"
        self.linked_by.setdefault(header.digest, "alone")

    def _fallback_parent(self, header: Header) -> Header | None:
        """The latest earlier message with the same normalized subject that
        shares a correspondent, inside the window — or None."""
        subject = normalize_subject(header.subject)
        if not subject:
            return None
        parties = set(header.correspondents)
        window = FALLBACK_WINDOW_DAYS * 86400
        best: Header | None = None
        for other in self.by_digest.values():
            if other.digest == header.digest or other.position >= header.position:
                continue
            if header.at - other.at > window:
                continue
            if normalize_subject(other.subject) != subject:
                continue
            if not parties & set(other.correspondents):
                continue
            if best is None or other.position > best.position:
                best = other
        return best

    def _union_as_of(self, as_of: tuple[float, str]) -> _Union:
        cached = self._cache.get(as_of)
        if cached is not None:
            return cached
        union = _Union()
        for header in self.by_digest.values():
            if header.position > as_of:
                continue
            node = header.digest
            union.find(node)
            if header.message_id:
                union.join(node, f"mid:{header.message_id}")
            for mid in header.in_reply_to + header.references:
                union.join(node, f"mid:{mid}")
            parent = self.fallback.get(node)
            if parent is not None:
                union.join(node, parent)
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[as_of] = union
        return union

    # -- reading, always as of a position

    def get(self, digest: str) -> Header | None:
        return self.by_digest.get(digest)

    def reply_parent(self, message_id: str, before: tuple[float, str]) -> Header | None:
        """The message a reply's In-Reply-To names, among those before
        `before` — or None when there is none or the claimants DISAGREE about
        who sent it.

        Two claimants of one Message-ID that agree on the sender are one mail
        delivered twice; the Gmail copy is preferred (its sender is the one
        that can be authenticated). Claimants that disagree mean somebody
        squatted the id, and a reply to it is then nobody's act: the ledger,
        the case store and alias admission all read this, so an ambiguity can
        only cost an act, never mint one. Counted in `Health.ambiguous_parents`."""
        claimants = [
            h for h in (self.by_digest.get(d) for d in self.mid_members.get(message_id, ()))
            if h is not None and h.position < before
        ]
        if not claimants:
            return None
        if len({h.from_addr for h in claimants}) > 1:
            self.health.ambiguous_parents += 1
            return None
        gmail = [h for h in claimants if h.mailbox == "gmail"]
        return min(gmail or claimants, key=lambda h: h.position)

    def body(self, digest: str) -> str | None:
        """The stored body, or None when it is not (or no longer) recorded."""
        header = self.by_digest.get(digest)
        if header is None or not header.body_digest:
            return None
        if self.state_dir is None:
            return self._bodies.get(header.body_digest)
        return evidence.get(header.body_digest, self.state_dir)

    def conversation(self, digest: str, as_of: tuple[float, str] | None = None) -> list[Header]:
        """Every message in this message's conversation as it stood at `as_of`
        (default: the message's own position), oldest first."""
        header = self.by_digest.get(digest)
        if header is None:
            return []
        cutoff = header.position if as_of is None else as_of
        if header.position > cutoff:
            return []
        union = self._union_as_of(cutoff)
        root = union.find(digest)
        members = [
            h for h in self.by_digest.values()
            if h.position <= cutoff and union.find(h.digest) == root
        ]
        return sorted(members, key=lambda h: h.position)

    def conversations(self) -> list[list[Header]]:
        """Every conversation as it stands NOW (the union at the latest
        position), each oldest first. One pass over the index, for readers
        that need all of them at once (the daily report's follow-ups) rather
        than `conversation` per message, which would rebuild per call."""
        if not self.by_digest:
            return []
        latest = max(h.position for h in self.by_digest.values())
        union = self._union_as_of(latest)
        groups: dict[str, list[Header]] = {}
        for header in self.by_digest.values():
            groups.setdefault(union.find(header.digest), []).append(header)
        return [sorted(members, key=lambda h: h.position) for members in groups.values()]

    def before(self, as_of: tuple[float, str]) -> list[Header]:
        """Every message at or before `as_of`, oldest first."""
        return sorted(
            (h for h in self.by_digest.values() if h.position <= as_of),
            key=lambda h: h.position,
        )

    def later_replies(self, digest: str) -> list[Header]:
        """Outbound messages AFTER this one that reply into its conversation.
        This reads the future on purpose: it is the backtest's ground truth,
        and nothing on the Reader path calls it."""
        header = self.by_digest.get(digest)
        if header is None:
            return []
        latest = max(h.position for h in self.by_digest.values())
        union = self._union_as_of(latest)
        root = union.find(digest)
        return sorted(
            (
                h for h in self.by_digest.values()
                if h.direction == "out" and h.position > header.position
                and union.find(h.digest) == root
            ),
            key=lambda h: h.position,
        )


def _replace(header: Header, **changes: Any) -> Header:
    data = header.as_json()
    data.update(changes)
    return header_from_json(data)
