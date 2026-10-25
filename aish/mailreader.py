"""Zone B — the Reader (#142 W5, slice S1).

A toolless role (`aish/charters/mail-reader.md`) that reads ONE mail in its
conversation and answers a typed verdict. Everything around the model is code:

- **The bundle is composed here**, from aish's own thread index (the whole
  conversation, both directions, as of the mail), the correlator's candidate
  cases, and the Gate's trust facts. Every thing in it is NAMED (`m1`, `c2`,
  `t1`) so the answer can refer to it and code can check the reference.
- **The answer is validated here** (`validate_answer`, called by
  `roles.validate` for `shape: verdict`): a fixed skeleton of closed
  vocabularies, every one of which can say `cannot_tell`; citations and
  payment coordinates checked against the bundle; a capped open layer
  (meaning, self-named kind) flattened, de-linked and de-voiced.
- **Asking for more is a CLOSED shape.** The bundle lists what it can expand
  (`available`); an answer may ask for those references and nothing else, and
  code honours at most `READER_ASK_ROUNDS` of them. A free-text query is a validation
  error: an open query would make the Reader a retrieval oracle over the
  owner's mailbox, steerable by whoever wrote the mail it is reading.
- **An answer that will not parse is its own ending** (`invalid`), never a
  guess at what was meant.

The verdict acts on nothing in S1. `docs/mail-assistant.md`.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import mailcases, mailgate, mailindex, roles

READER = "mail-reader"
BUNDLE_INPUT = "bundle"

CANNOT_TELL = "cannot_tell"

DISPOSITIONS = ("surface", "hold", "archive", "expire_after_grace", CANNOT_TELL)
WANTED = ("pay", "reply", "decide", "wait", "nothing", CANNOT_TELL)
SENDER_CASE = ("consistent", "mismatch", CANNOT_TELL)
SUSPICION = ("none", "suspicious", CANNOT_TELL)
CASE_WORDS = ("new", "no_case", CANNOT_TELL)
DEADLINE_WORDS = ("none", CANNOT_TELL)

# The skeleton: every closed axis and its vocabulary. `case` and `deadline`
# are closed too, but over the bundle's refs and over dates.
AXES: dict[str, tuple[str, ...]] = {
    "disposition": DISPOSITIONS,
    "wanted": WANTED,
    "sender_case": SENDER_CASE,
    "suspicion": SUSPICION,
}

# The caps a `verdict` output must declare in the charter's frontmatter.
VERDICT_CAPS = ("max_meaning_chars", "max_kind_chars", "max_citations", "max_coordinates")

# How much of a conversation the first bundle carries. "rest of thread"
# brings the omitted older messages, capped the same way.
READER_CONVERSATION_SHOWN = 6
READER_REVIEWED_CHARS = 6000
READER_EARLIER_CHARS = 1500
READER_REST_SHOWN = 20

# Re-invocations an answer may cause by asking. The pattern is
# `roles.ROLE_ATTEMPTS`: a constant, small, and stated in the docs.
READER_ASK_ROUNDS = 2
READER_ASK_ITEMS = 3

# A conversation the owner wrote in within this many days is LIVE: a verdict
# filing it (archive, expire) is overridden to `surface` by code (`clamp`). A
# CHOICE, not a measurement — the owner's own (2026-10-26: "if I go on vacation,
# 14 days would file things too quickly"), stated like the `TokenBucket`
# numbers. The Reader's prose says "recently"; the number lives here and in the
# bundle fact, never in the charter.
LIVE_CONVERSATION_DAYS = 30
FILING_DISPOSITIONS = ("archive", "expire_after_grace")
CLAMP_TO = "surface"

_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def sanitize(text: str, cap: int) -> str:
    """The open layer's bounds: control-flattened and capped (`roles.capped`),
    every URL replaced — a verdict never carries a loose link; actions refer
    to the mail by id and its own link gate fires at open time — and aish's
    own marker broken so the field cannot wear aish's voice."""
    flat = roles.capped(text or "", cap * 2)
    flat = _URL.sub("[link]", flat)
    flat = flat.replace("[aish:", "(aish:")
    return flat[:cap]


def _compact(value: str) -> str:
    return re.sub(r"[\s-]+", "", value or "").upper()


# ---------------------------------------------------------------- the bundle


@dataclass
class Bundle:
    """The Reader's whole input, and the refs that name its parts."""

    data: dict[str, Any]
    messages: dict[str, str]  # ref -> digest
    cases: dict[str, str]  # ref -> case id
    expandable: dict[str, tuple[str, str]] = field(default_factory=dict)
    owner_wrote_days: float | None = None

    @property
    def text(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=1)


def _day(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(at))


def _owner_wrote_days(
    conversation: list[mailindex.Header], header: mailindex.Header
) -> float | None:
    """Days from his latest message in `conversation` at or before `header`
    to `header`, or None when he wrote none there.

    PAST-facing by construction: only members at or before the mail's own
    position count, so a reply he sends later never makes an earlier mail
    "live". THREAD-scoped: it says he wrote in this conversation, not that he
    knows the sender — that is the ledger's grade, which counts his acts toward
    an address; joining a thread by References earns this fact, never a grade.
    """
    mine = [
        h.at for h in conversation
        if h.direction == "out" and h.position <= header.position and h.digest != header.digest
    ]
    return (header.at - max(mine)) / 86400 if mine else None


def owner_wrote_days(index: mailindex.MailIndex, header: mailindex.Header) -> float | None:
    """`_owner_wrote_days` over the WHOLE conversation as of the mail — never
    the slice a bundle shows, which a long thread truncates."""
    return _owner_wrote_days(index.conversation(header.digest, header.position), header)


def owner_wrote_words(days: float | None) -> str:
    """The bundle's wording of the fact: `never`, or whole days ago."""
    if days is None:
        return "never"
    whole = int(days)
    return "1 day ago" if whole == 1 else f"{whole} days ago"


def clamp(disposition: str, days: float | None) -> dict[str, Any] | None:
    """The live-conversation clamp: code's override of a filing disposition on
    a conversation he wrote in within `LIVE_CONVERSATION_DAYS`, or None.

    It never rewrites the verdict — the result is stored BESIDE it, so the
    Reader's own answer stays measurable — and it only ever moves toward his
    attention (`surface`), never away from it."""
    if days is None or days > LIVE_CONVERSATION_DAYS or disposition not in FILING_DISPOSITIONS:
        return None
    return {"from": disposition, "to": CLAMP_TO, "owner_wrote_days": round(days, 1),
            "limit": LIVE_CONVERSATION_DAYS}


def _message_entry(
    ref: str, header: mailindex.Header, index: mailindex.MailIndex, cap: int
) -> dict[str, Any]:
    body = index.body(header.digest)
    return {
        "ref": ref,
        "direction": "from_owner" if header.direction == "out" else "to_owner",
        "from": header.from_value[:200],
        "to": list(header.to[:6]),
        "date": _day(header.at),
        "subject": header.subject[:300],
        "text": (body if body is not None else "[body not recorded]")[:cap],
        "attachments": [a.filename[:120] for a in header.attachments[:10]],
    }


@dataclass(frozen=True)
class Facts:
    """The Gate's and the correlator's facts about one mail — code-written,
    shown to the Reader as trusted framing and recorded beside its answer."""

    auth: mailgate.Auth
    grade: mailgate.Grade
    flags: tuple[mailgate.Flag, ...]
    newsletter: mailgate.Newsletter
    candidates: tuple[mailcases.Candidate, ...]


def compose(
    index: mailindex.MailIndex,
    store: mailcases.CaseStore,
    header: mailindex.Header,
    facts: Facts,
    expand: tuple[str, ...] = (),
    offer: bool = True,
) -> Bundle:
    """The bundle for `header`, as of its own position.

    `expand` is the set of references a previous answer asked for and code
    agreed to honour; `offer=False` builds the last bundle, which offers
    nothing more.
    """
    as_of = header.position
    conversation = index.conversation(header.digest, as_of)
    # Over the full conversation, BEFORE the shown slice is cut below.
    wrote_days = _owner_wrote_days(conversation, header)
    earlier = [h for h in conversation if h.digest != header.digest]
    whole = "rest of thread t1" in expand
    shown = earlier[-READER_REST_SHOWN:] if whole else earlier[-READER_CONVERSATION_SHOWN:]
    omitted = len(earlier) - len(shown)
    messages: dict[str, str] = {}
    entries = []
    for n, h in enumerate(shown + [header], 1):
        ref = f"m{n}"
        messages[ref] = h.digest
        cap = READER_REVIEWED_CHARS if h.digest == header.digest else READER_EARLIER_CHARS
        entries.append(_message_entry(ref, h, index, cap))
    reviewed = f"m{len(entries)}"
    cases: dict[str, str] = {}
    candidate_entries = []
    for n, cand in enumerate(facts.candidates, 1):
        ref = f"c{n}"
        cases[ref] = cand.case_id
        case = store.cases.get(cand.case_id)
        entry: dict[str, Any] = {"ref": ref, "why": list(cand.reasons)}
        if case is not None:
            entry["keys"] = sorted(case.keys_before(as_of))[:8]
            entry["admitted_senders"] = sorted(case.admitted_before(as_of))
            last = case.last_before(as_of)
            entry["last_activity"] = _day(last[0]) if last else ""
            entry["owner_transacted"] = case.transacted_before(as_of)
            if f"case file {ref}" in expand:
                entry["case_file"] = mailcases.case_file(store, cand.case_id, index, as_of)
        candidate_entries.append(entry)
    available: list[str] = []
    if offer:
        if omitted and not whole:
            available.append("rest of thread t1")
        available += [
            f"case file {ref}" for ref in cases if f"case file {ref}" not in expand
        ]
    data = {
        "mail": reviewed,
        "thread": "t1",
        "messages": entries,
        "omitted_messages": omitted,
        "owner_wrote_in_this_conversation": owner_wrote_words(wrote_days),
        "candidates": candidate_entries,
        "sender": {
            "address": header.from_addr,
            "grade": facts.grade.grade,
            "authenticated": facts.auth.authenticated,
            "flags": [f.what for f in facts.flags],
        },
        "available": available,
    }
    return Bundle(data, messages, cases, owner_wrote_days=wrote_days)


# ---------------------------------------------------------------- the answer


@dataclass(frozen=True)
class Coordinate:
    value: str
    cite: str
    kind: str  # "iban" (checksum-valid) | "account"

    def as_json(self) -> dict[str, str]:
        return {"value": self.value, "cite": self.cite, "kind": self.kind}


@dataclass(frozen=True)
class Verdict:
    """A validated Reader answer: the value `roles.run` returns."""

    disposition: str
    wanted: str
    deadline: str
    case: str
    sender_case: str
    suspicion: str
    coordinates: tuple[Coordinate, ...]
    citations: tuple[str, ...]
    meaning: str
    kind: str

    def as_json(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "wanted": self.wanted,
            "deadline": self.deadline,
            "case": self.case,
            "sender_case": self.sender_case,
            "suspicion": self.suspicion,
            "payment_coordinates": [c.as_json() for c in self.coordinates],
            "citations": list(self.citations),
            "meaning": self.meaning,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class Ask:
    """An answer that asked for more, from the bundle's own offer."""

    more: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {"more": list(self.more)}


def _bundle_of(inputs: dict[str, str]) -> dict[str, Any]:
    try:
        raw = json.loads(inputs.get(BUNDLE_INPUT, "") or "{}")
    except ValueError as exc:
        raise ValueError(f"the Reader's bundle is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the Reader's bundle is not a JSON object")
    return raw


def _word(payload: dict, name: str, allowed: tuple[str, ...]) -> str:
    value = str(payload.get(name) or "").strip().lower()
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)} (got {payload.get(name)!r})")
    return value


def validate_answer(shape: roles.Shape, payload: Any, inputs: dict[str, str]) -> Verdict | Ask:
    """A Reader answer as a `Verdict` or an `Ask`, or `ValueError` with a
    message the model can act on (it is fed back on the one corrective retry)."""
    bundle = _bundle_of(inputs)
    if not isinstance(payload, dict):
        raise ValueError("the reply must be a JSON object")
    offered = [str(x) for x in bundle.get("available") or ()]
    if "more" in payload and "disposition" not in payload:
        asked = payload.get("more")
        if not isinstance(asked, list) or not asked:
            raise ValueError('"more" must be a list of references copied from "available"')
        if not offered:
            raise ValueError(
                "nothing more is available — answer the verdict with what you have, "
                'using "cannot_tell" where you cannot tell'
            )
        wanted = [str(x).strip() for x in asked][:READER_ASK_ITEMS]
        unknown = [x for x in wanted if x not in offered]
        if unknown:
            raise ValueError(
                f"{unknown} is not something this bundle offers. You may ask only for "
                f"these, copied exactly: {offered}"
            )
        return Ask(tuple(dict.fromkeys(wanted)))
    message_refs = {
        str(m.get("ref")): str(m.get("text") or "")
        for m in bundle.get("messages") or () if isinstance(m, dict)
    }
    case_refs = {str(c.get("ref")) for c in bundle.get("candidates") or () if isinstance(c, dict)}

    values = {name: _word(payload, name, allowed) for name, allowed in AXES.items()}

    case = str(payload.get("case") or "").strip().lower()
    if case not in case_refs and case not in CASE_WORDS:
        choices = sorted(case_refs) + list(CASE_WORDS)
        raise ValueError(f"case must be one of {', '.join(choices)} (got {payload.get('case')!r})")

    deadline = str(payload.get("deadline") or "").strip().lower()
    if deadline not in DEADLINE_WORDS:
        if not _DATE.fullmatch(deadline):
            raise ValueError('deadline must be a date as YYYY-MM-DD, "none" or "cannot_tell"')
        try:
            datetime.date.fromisoformat(deadline)
        except ValueError as exc:
            raise ValueError(f"deadline {deadline!r} is not a real date") from exc

    raw_coordinates = payload.get("payment_coordinates") or []
    if not isinstance(raw_coordinates, list):
        raise ValueError("payment_coordinates must be a list")
    if len(raw_coordinates) > shape.caps["max_coordinates"]:
        raise ValueError(f"at most {shape.caps['max_coordinates']} payment_coordinates")
    coordinates = []
    for item in raw_coordinates:
        if not isinstance(item, dict):
            raise ValueError('each payment coordinate is {"value": ..., "cite": "m<n>"}')
        value = _compact(str(item.get("value") or ""))[:40]
        cite = str(item.get("cite") or "").strip()
        if cite not in message_refs:
            raise ValueError(f"payment coordinate cite {cite!r} names no message of the bundle")
        if len(value) < 8 or value not in _compact(message_refs[cite]):
            raise ValueError(
                f"payment coordinate {value!r} does not appear in {cite} — copy it exactly "
                "from the message's text"
            )
        kind = "iban" if mailcases.iban_valid(mailcases.normalize_coordinate(value)) else "account"
        coordinates.append(Coordinate(value, cite, kind))

    raw_citations = payload.get("citations")
    if not isinstance(raw_citations, list) or not raw_citations:
        raise ValueError('citations must list at least one ref ("m<n>" or "c<n>") you relied on')
    if len(raw_citations) > shape.caps["max_citations"]:
        raise ValueError(f"at most {shape.caps['max_citations']} citations")
    citations = []
    for ref in raw_citations:
        ref = str(ref).strip()
        if ref not in message_refs and ref not in case_refs:
            raise ValueError(f"citation {ref!r} names nothing in the bundle")
        citations.append(ref)

    return Verdict(
        disposition=values["disposition"],
        wanted=values["wanted"],
        deadline=deadline,
        case=case,
        sender_case=values["sender_case"],
        suspicion=values["suspicion"],
        coordinates=tuple(dict.fromkeys(coordinates)),
        citations=tuple(dict.fromkeys(citations)),
        meaning=sanitize(str(payload.get("meaning") or ""), shape.caps["max_meaning_chars"]),
        kind=sanitize(str(payload.get("kind") or ""), shape.caps["max_kind_chars"]).lower(),
    )


def shape_fields(caps: dict[str, int]) -> tuple[roles.Field, ...]:
    """The fields of a verdict, declared so the wiring law and the counters
    can see them: the closed axes, and the two capped text fields that are
    the only prose that can leave the role."""
    fields = [roles.Field(name, "enum", values=values) for name, values in AXES.items()]
    fields.append(roles.Field("meaning", "text", max_chars=caps["max_meaning_chars"],
                              may_be_empty=True))
    fields.append(roles.Field("kind", "text", max_chars=caps["max_kind_chars"],
                              may_be_empty=True))
    return tuple(fields)


def contract_text(shape: roles.Shape) -> str:
    """The output contract, generated from this module's vocabularies and the
    charter's caps, so the words the model reads and the rules code enforces
    cannot drift apart."""

    def words(values: tuple[str, ...]) -> str:
        return ", ".join(json.dumps(v) for v in values)

    caps = shape.caps
    return "\n".join([
        "Reply with ONE JSON object and nothing else. No prose before or after it, no code fence.",
        "",
        "Either the verdict:",
        "{",
        f'  "disposition": one of {words(DISPOSITIONS)},',
        f'  "wanted": one of {words(WANTED)},',
        '  "deadline": a date "YYYY-MM-DD", or "none", or "cannot_tell",',
        '  "case": the "ref" of one candidate ("c1", ...), or one of '
        f"{words(CASE_WORDS)},",
        f'  "sender_case": one of {words(SENDER_CASE)},',
        f'  "suspicion": one of {words(SUSPICION)},',
        '  "payment_coordinates": [{"value": "<account number copied exactly>", '
        f'"cite": "m<n>"}}, ...] — at most {caps["max_coordinates"]}, [] when there are none,',
        f'  "citations": ["m<n>" or "c<n>", ...] — 1 to {caps["max_citations"]} refs '
        "you relied on,",
        f'  "meaning": at most {caps["max_meaning_chars"]} characters, plain text,',
        f'  "kind": at most {caps["max_kind_chars"]} characters naming what kind of mail this is',
        "}",
        "",
        'Or, ONLY if the bundle\'s "available" list is not empty and you cannot answer without '
        'it: {"more": ["<an item copied exactly from available>", ...]}',
        "",
        "Anything outside a vocabulary, a ref the bundle does not have, or a payment coordinate "
        "that is not in the cited message is rejected and you are asked once more.",
    ])


def tally(value: Verdict | Ask) -> dict[str, dict[str, int]]:
    """Per-call counts for the role record's `flags` — the counters."""
    if isinstance(value, Ask):
        return {"asked": {str(len(value.more)): 1}}
    out = {name: {getattr(value, name): 1} for name in AXES}
    out["case"] = {value.case if value.case in CASE_WORDS else "candidate": 1}
    out["deadline"] = {value.deadline if value.deadline in DEADLINE_WORDS else "date": 1}
    return out


# ---------------------------------------------------------------- exam assertions


def _expect_axis(expected: Any, value: Any) -> list[str]:
    if isinstance(value, Ask):
        return [f"asked for {list(value.more)} instead of answering"]
    problems = []
    for name, allowed in (expected or {}).items():
        got = getattr(value, str(name))
        if got not in [str(v) for v in allowed]:
            problems.append(f"{name}: expected one of {list(allowed)}, got {got!r}")
    return problems


def _expect_case(expected: Any, value: Any) -> list[str]:
    if isinstance(value, Ask):
        return [f"asked for {list(value.more)} instead of answering"]
    allowed = [str(v) for v in expected or ()]
    return [] if value.case in allowed else [f"case: expected one of {allowed}, got {value.case!r}"]


def _expect_deadline(expected: Any, value: Any) -> list[str]:
    if isinstance(value, Ask):
        return [f"asked for {list(value.more)} instead of answering"]
    allowed = [str(v) for v in expected or ()]
    if value.deadline in allowed:
        return []
    return [f"deadline: expected one of {allowed}, got {value.deadline!r}"]


def _expect_coordinates(expected: Any, value: Any) -> list[str]:
    if isinstance(value, Ask):
        return [f"asked for {list(value.more)} instead of answering"]
    got = {mailcases.normalize_coordinate(c.value) for c in value.coordinates}
    return [
        f"payment_coordinates does not include {needle!r}"
        for needle in expected or ()
        if mailcases.normalize_coordinate(str(needle)) not in got
    ]


def _expect_cites(expected: Any, value: Any) -> list[str]:
    if isinstance(value, Ask):
        return [f"asked for {list(value.more)} instead of answering"]
    return [
        f"cites none of {group}"
        for group in expected or ()
        if not any(str(ref) in value.citations for ref in group)
    ]


def _expect_absent(expected: Any, value: Any) -> list[str]:
    blob = json.dumps(value.as_json(), ensure_ascii=False).casefold()
    return [
        f"the output still carries {needle!r}"
        for needle in expected or ()
        if str(needle).casefold() in blob
    ]


def _expect_mentions_any(expected: Any, value: Any) -> list[str]:
    if isinstance(value, Ask):
        return [f"asked for {list(value.more)} instead of answering"]
    text = value.meaning.casefold()
    return [
        f"the meaning mentions none of {group}"
        for group in expected or ()
        if not any(str(word).casefold() in text for word in group)
    ]


ASSERTIONS: dict[str, Callable[[Any, Any], list[str]]] = {
    "axis_in": _expect_axis,
    "case_in": _expect_case,
    "deadline_in": _expect_deadline,
    "coordinates_include": _expect_coordinates,
    "cites_any": _expect_cites,
    "absent": _expect_absent,
    "meaning_mentions_any": _expect_mentions_any,
}


def check_expectation(case_name: str, expect: dict[str, Any]) -> None:
    """Refuse at load time an exam case no answer could meet: an axis that
    does not exist, a word outside its vocabulary, or a deadline that is
    neither a date nor one of the deadline words."""
    for wanted in expect.get("deadline_in") or ():
        word = roles._yaml_word(wanted, case_name)
        if word not in DEADLINE_WORDS and not _DATE.fullmatch(word):
            raise roles.CharterError(
                f"golden pair {case_name!r} expects deadline {word!r}, which is not "
                f"YYYY-MM-DD or one of {list(DEADLINE_WORDS)}"
            )
    for name, allowed in (expect.get("axis_in") or {}).items():
        if name not in AXES:
            raise roles.CharterError(f"golden pair {case_name!r} names unknown axis {name!r}")
        outside = [
            v for v in (roles._yaml_word(v, case_name) for v in allowed) if v not in AXES[name]
        ]
        if outside:
            raise roles.CharterError(
                f"golden pair {case_name!r} expects {name}={outside}, outside "
                f"{list(AXES[name])}"
            )


# ---------------------------------------------------------------- one reading


# How a reading ended. `gated` and the four `roles.Status` values: an answer
# that never came is a recorded ending, never a default verdict.
GATED = "gated"


@dataclass
class Reading:
    status: str
    verdict: Verdict | None = None
    asks: list[tuple[str, ...]] = field(default_factory=list)
    calls: list[roles.Result] = field(default_factory=list)
    why: str = ""
    bundle: Bundle | None = None


def read(
    charter: roles.Charter,
    index: mailindex.MailIndex,
    store: mailcases.CaseStore,
    header: mailindex.Header,
    facts: Facts,
    *,
    model_spec: str,
    chat: Callable[..., Any] | None = None,
    state_dir: Any = None,
    check_admission: bool = True,
    admission_state_dir: Any = None,
) -> Reading:
    """Gate-aware reading of one mail: no call for a newsletter-gated mail;
    otherwise one role call, plus at most `READER_ASK_ROUNDS` re-invocations when the
    answer asks for something the bundle offered."""
    if facts.newsletter.gated:
        return Reading(GATED, why="list headers: " + ", ".join(facts.newsletter.by))
    reading = Reading(roles.Status.UNAVAILABLE)
    expand: tuple[str, ...] = ()
    for ask_round in range(READER_ASK_ROUNDS + 1):
        bundle = compose(index, store, header, facts, expand, offer=ask_round < READER_ASK_ROUNDS)
        reading.bundle = bundle
        result = roles.run(
            charter, {BUNDLE_INPUT: bundle.text}, (), model_spec=model_spec, chat=chat,
            state_dir=state_dir, check_admission=check_admission,
            admission_state_dir=admission_state_dir,
        )
        reading.calls.append(result)
        reading.status, reading.why = result.status, result.why
        if result.status != roles.Status.OK:
            return reading
        if isinstance(result.value, Ask):
            reading.asks.append(result.value.more)
            expand = tuple(dict.fromkeys(expand + result.value.more))
            continue
        reading.verdict = result.value
        return reading
    # Unreachable by construction: the last bundle offers nothing, and an ask
    # against an empty offer fails validation. Recorded rather than assumed.
    reading.status, reading.why = roles.Status.INVALID, "asked past the last bundle"
    return reading
