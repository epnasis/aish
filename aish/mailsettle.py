"""The settlement judge (#142 W5, the settlement design of 2026-11-02).

A toolless role (`aish/charters/item-settler.md`) asked ONE question about ONE
pair: here is an open item — something the daily report would put in front of
the owner — and here is a LATER mail code tied to it; does that mail show the
matter is settled? It answers `settled_by k1`, `still_open` or `cannot_tell`,
with a short cited reason.

Everything around the model is code, and code has the last word:

- **Code chooses the pairs** (`mailitems.Context.candidates`): only mail first
  read after the item's anchor, tied to it by a code signal, under the tier
  rule (`mailitems.Context.check`). A pair the tier rule refuses, or whose
  bodies are not recorded, is never sent — it is counted.
- **Code composes the bundle** (`compose`): the item's mails, the candidate,
  and code's words for how they relate. Every part is named (`m1`, `k1`).
- **Code validates the answer** (`validate_answer`): a closed vocabulary that
  can say `cannot_tell`, a settling ref that must be the candidate, citations
  that must be in the bundle, a capped reason. The reason is stored for the
  driver and NEVER rendered: the report's evidence line is code's.
- **Code re-verifies on every read** (`mailitems.Context._judged`): a
  judgement whose candidate later carries a caution, fails the tier rule, or
  is followed by newer mail no longer settles anything.

It runs in the WATCHER pass, never in the 07:30 report (which stays
model-free and counts what is not yet judged). Bounded per pass
(`MAX_JUDGED_PER_PASS`); idempotent per (item, candidate, charter digest).
`docs/mail-assistant.md` §The settlement judge.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from . import mailcases, mailindex, mailitems, mailreader, roles

SETTLER = "item-settler"
BUNDLE_INPUT = "bundle"

CANNOT_TELL = mailreader.CANNOT_TELL
SETTLED_BY = "settled_by"
STILL_OPEN = "still_open"
ANSWERS = (SETTLED_BY, STILL_OPEN, CANNOT_TELL)
CANDIDATE_REF = "k1"

#: The caps a `settlement` output must declare in the charter's frontmatter.
SETTLEMENT_CAPS = ("max_reason_chars", "max_citations")

#: Model calls one watcher pass may spend, newest candidate first. A CHOICE:
#: the pass runs every 15 minutes, so a backlog drains and new mail goes first.
MAX_JUDGED_PER_PASS = 12

ANCHOR_CHARS = 3000
OTHER_CHARS = 1500
CANDIDATE_CHARS = 4000
OTHER_SHOWN = 3

#: Endings that are final for a pair: the model answered (valid), or answered
#: twice and neither validated. A missing model or admission is retried.
FINAL_ENDINGS = (roles.Status.OK, roles.Status.INVALID)


# ---------------------------------------------------------------- the bundle


@dataclass
class Bundle:
    data: dict[str, Any]
    refs: dict[str, str]  # ref -> digest

    @property
    def text(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=1)


_WANTED_WORDS = {"pay": "pay", "reply": "reply to", "decide": "decide on"}


def shown_counterparty(ctx: mailitems.Context, item: mailitems.OpenItem) -> str:
    """The item's counterparty as the judge may read it — relative to the
    item's own mails (`mailindex.shown_party`)."""
    return mailindex.shown_party(
        item.counterparty, [ctx.index.get(d) for d in (item.anchor, *item.rests_on)])


def matter_words(ctx: mailitems.Context, item: mailitems.OpenItem) -> str:
    """What the item IS, in code's words — the question the judge weighs."""
    party = shown_counterparty(ctx, item)
    if item.kind == mailitems.NEEDS:
        reading = ctx.readings.get(item.anchor)
        wanted = str((reading.verdict if reading else {}).get("wanted") or "")
        verb = _WANTED_WORDS.get(wanted, "act on")
        return (f"{party} sent the owner mail m1, which aish reads as asking him to {verb} "
                "something")
    if item.kind == mailitems.YOURS:
        return (f"{party} wrote to the owner (m1) after his last message to them, and aish "
                "counts it as waiting on him")
    return f"the owner wrote to {party} (m1) and has had no answer from them since"


def compose(ctx: mailitems.Context, item: mailitems.OpenItem,
            candidate: mailitems.Candidate) -> Bundle:
    """The judge's whole input for one (item, candidate) pair."""
    index = ctx.index
    refs: dict[str, str] = {}
    messages = []
    anchor = index.get(item.anchor)
    assert anchor is not None
    others = [d for d in item.rests_on if d != item.anchor][-OTHER_SHOWN:]
    for n, digest in enumerate([item.anchor, *others], 1):
        header = index.get(digest)
        if header is None:
            continue
        ref = f"m{n}"
        refs[ref] = digest
        cap = ANCHOR_CHARS if digest == item.anchor else OTHER_CHARS
        messages.append(mailreader._message_entry(ref, header, index, cap))
    refs[CANDIDATE_REF] = candidate.header.digest
    entry = mailreader._message_entry(CANDIDATE_REF, candidate.header, index, CANDIDATE_CHARS)
    relation = {"his act": "the owner's own mail",
                "same counterparty": "from the same party as the item, or a party in its "
                                     "conversation",
                "third party": "from a third party with a history in the owner's cases",
                "vouched sender": "from a sender the owner allowed to settle this kind of "
                                  f"item ({candidate.meaning})"}
    data = {
        "item": {
            "matter": matter_words(ctx, item),
            "counterparty": shown_counterparty(ctx, item),
            "messages": messages,
        },
        "candidate": {
            **entry,
            "relation": relation.get(candidate.tier, candidate.tier),
            "aish_saw": list(candidate.ties),
            "arrived": "after the item's first mail",
        },
        "question": ("Does k1 show that the matter is settled — done, paid, answered, "
                     "delivered, or otherwise no longer needing the owner?"),
    }
    return Bundle(data, refs)


# ---------------------------------------------------------------- the answer


@dataclass(frozen=True)
class Settlement:
    """A validated judge answer: the value `roles.run` returns."""

    answer: str
    settled_by: str
    reason: str
    citations: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {"answer": self.answer, "settled_by": self.settled_by, "reason": self.reason,
                "citations": list(self.citations)}


def _bundle_of(inputs: dict[str, str]) -> dict[str, Any]:
    try:
        raw = json.loads(inputs.get(BUNDLE_INPUT, "") or "{}")
    except ValueError as exc:
        raise ValueError(f"the bundle is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the bundle is not a JSON object")
    return raw


def _refs(bundle: dict[str, Any]) -> set[str]:
    raw = bundle.get("item")
    item: dict[str, Any] = raw if isinstance(raw, dict) else {}
    refs = {str(m.get("ref")) for m in (item.get("messages") or ()) if isinstance(m, dict)}
    candidate = bundle.get("candidate")
    if isinstance(candidate, dict) and candidate.get("ref"):
        refs.add(str(candidate["ref"]))
    return refs


def validate_answer(shape: roles.Shape, payload: Any, inputs: dict[str, str]) -> Settlement:
    """A judge answer as a `Settlement`, or `ValueError` with words the model
    can act on (fed back on the one corrective retry)."""
    bundle = _bundle_of(inputs)
    if not isinstance(payload, dict):
        raise ValueError("the reply must be a JSON object")
    answer = str(payload.get("answer") or "").strip().lower()
    if answer not in ANSWERS:
        raise ValueError(f"answer must be one of {', '.join(ANSWERS)} "
                         f"(got {payload.get('answer')!r})")
    refs = _refs(bundle)
    candidate = str((bundle.get("candidate") or {}).get("ref") or CANDIDATE_REF)
    settled_by = str(payload.get("settled_by") or "").strip()
    if answer == SETTLED_BY:
        if settled_by != candidate:
            raise ValueError(f'with answer "settled_by", "settled_by" must be "{candidate}", '
                             "the candidate — the only mail that can settle the item here")
    elif settled_by:
        raise ValueError('"settled_by" names a mail only when answer is "settled_by"')
    reason = mailreader.sanitize(str(payload.get("reason") or ""),
                                 shape.caps["max_reason_chars"])
    if not reason:
        raise ValueError('"reason" is required: say in a sentence what you relied on')
    raw = payload.get("citations")
    if not isinstance(raw, list) or not raw:
        raise ValueError('"citations" must list at least one ref you relied on')
    if len(raw) > shape.caps["max_citations"]:
        raise ValueError(f"at most {shape.caps['max_citations']} citations")
    citations = []
    for ref in raw:
        ref = str(ref).strip()
        if ref not in refs:
            raise ValueError(f"citation {ref!r} names nothing in the bundle ({sorted(refs)})")
        citations.append(ref)
    if answer == SETTLED_BY and candidate not in citations:
        raise ValueError(f'a "settled_by" answer must cite {candidate}')
    return Settlement(answer, settled_by, reason, tuple(dict.fromkeys(citations)))


def shape_fields(caps: dict[str, int]) -> tuple[roles.Field, ...]:
    """Declared so the wiring law and the counters can see them: the closed
    answer, and the one capped text field — which no wiring carries anywhere."""
    return (roles.Field("answer", "enum", values=ANSWERS),
            roles.Field("reason", "text", max_chars=caps["max_reason_chars"]))


def contract_text(shape: roles.Shape) -> str:
    caps = shape.caps
    return "\n".join([
        "Reply with ONE JSON object and nothing else. No prose before or after it, no code fence.",
        "",
        "{",
        f'  "answer": one of {", ".join(json.dumps(a) for a in ANSWERS)},',
        f'  "settled_by": "{CANDIDATE_REF}" when answer is "settled_by", otherwise "",',
        f'  "reason": at most {caps["max_reason_chars"]} characters, plain text, saying what '
        "in the mails you relied on,",
        f'  "citations": ["m<n>" or "{CANDIDATE_REF}", ...] — 1 to {caps["max_citations"]} refs '
        f'you relied on (a "settled_by" answer cites {CANDIDATE_REF})',
        "}",
        "",
        "Anything outside the vocabulary, or a ref the bundle does not have, is rejected and you "
        "are asked once more.",
    ])


def tally(value: Settlement) -> dict[str, dict[str, int]]:
    return {"answer": {value.answer: 1}}


# ---------------------------------------------------------------- exam assertions


def _expect_answer(expected: Any, value: Any) -> list[str]:
    allowed = [str(v) for v in expected or ()]
    return [] if value.answer in allowed else [
        f"answer: expected one of {allowed}, got {value.answer!r}"]


def _expect_cites(expected: Any, value: Any) -> list[str]:
    return [f"cites none of {group}" for group in expected or ()
            if not any(str(ref) in value.citations for ref in group)]


def _expect_absent(expected: Any, value: Any) -> list[str]:
    blob = json.dumps(value.as_json(), ensure_ascii=False).casefold()
    return [f"the output still carries {needle!r}" for needle in expected or ()
            if str(needle).casefold() in blob]


ASSERTIONS: dict[str, Callable[[Any, Any], list[str]]] = {
    "answer_in": _expect_answer,
    "cites_any": _expect_cites,
    "absent": _expect_absent,
}


def check_expectation(case_name: str, expect: dict[str, Any]) -> None:
    """Refuse at load time an exam case no answer could meet."""
    for word in expect.get("answer_in") or ():
        if roles._yaml_word(word, case_name) not in ANSWERS:
            raise roles.CharterError(
                f"golden pair {case_name!r} expects answer {word!r}, outside {list(ANSWERS)}")


# ---------------------------------------------------------------- the pass


@dataclass
class SettlePass:
    """What one watcher pass's settlement step did — counted, printed, health."""

    items: int = 0  # open items considered
    candidates: int = 0  # tied later mail found
    not_weighed: int = 0  # refused by the tier rule (code)
    body_not_recorded: int = 0
    unread: int = 0  # authenticated, tied, but no Reader reading yet: read first
    already_judged: int = 0
    judged: int = 0  # model calls made
    endings: dict[str, int] = field(default_factory=dict)
    answers: dict[str, int] = field(default_factory=dict)
    capped: int = 0  # pairs left for a later pass by MAX_JUDGED_PER_PASS
    charter: str = ""  # why the step did not run, when it did not

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def load_settler() -> roles.Charter:
    return roles.load_charters()[SETTLER]


def charter_digest() -> str:
    """The settler charter's content digest, or "" when it does not load — so
    the report can tell a judged pair from one judged under an older charter."""
    try:
        return load_settler().digest
    except (roles.CharterError, KeyError, OSError):
        return ""


def showable(ctx: mailitems.Context, since: float | None = None) -> list[mailitems.OpenItem]:
    """The items the next report could show — the only ones worth a call:
    every follow-up (they are state, recomputed each morning), and the needs
    items that earn a line (`Context.line_facts`) inside the window — the
    report shows such an item every morning while it is open, so its state
    and not its news decides; none his hide preferences take out of the
    report. `since` is accepted for the callers' signature and no longer
    narrows anything: a line is not news-gated."""
    from . import mailprefs

    del since
    hides = (mailprefs.Store.load(ctx.state_dir).of(mailprefs.HIDE)
             if ctx.state_dir is not None else [])
    derived = ctx.derive()

    def hidden(header: mailindex.Header | None, reading: Any, party: str) -> bool:
        kind = str((reading.verdict if reading else {}).get("kind") or "")
        if header is not None:
            return any(mailprefs.matches_mail(p, header, kind) for p in hides)
        return any(mailprefs.matches_party(p, party) for p in hides)

    out = []
    for item in derived.needs:
        if item.open_item is None or hidden(item.header, item.reading, ""):
            continue
        if ctx.line_facts(item):
            out.append(item.open_item)
    for row in (*derived.yours, *derived.theirs):
        if row.open_item is not None and not hidden(row.latest, row.reading, row.counterparty):
            out.append(row.open_item)
    return out


def pairs(ctx: mailitems.Context, digest: str, result: SettlePass | None = None,
          since: float | None = None) -> list[tuple[mailitems.OpenItem, mailitems.Candidate]]:
    """Every (open item, candidate) pair that is ready to judge under the
    charter `digest` and has not been: the report counts these as not yet
    judged; the watcher judges them. Counted into `result` when given."""
    out = []
    for item in showable(ctx, since):
        state = ctx.state_of(item)
        if state.by_owner or state.probably:
            continue
        if result is not None:
            result.items += 1
        for candidate in ctx.candidates(item, restored_at=state.restored_at):
            if result is not None:
                result.candidates += 1
            if not candidate.tier:
                if result is not None:
                    result.not_weighed += 1
                continue
            if candidate.body_missing:
                if result is not None:
                    result.body_not_recorded += 1
                continue
            if candidate.unread:
                if result is not None:
                    result.unread += 1
                continue
            judged = ctx.judged.get(mailitems.pair_key(item.key, candidate.header.digest, digest))
            if judged is not None and judged.get("ending") in FINAL_ENDINGS:
                if result is not None:
                    result.already_judged += 1
                continue
            out.append((item, candidate))
    out.sort(key=lambda p: ctx.first_read(p[1].header.digest), reverse=True)
    return out


def settle_pass(
    state_dir: os.PathLike | str,
    *,
    charter: roles.Charter,
    model_spec: str,
    now: float,
    chat: Callable[..., Any] | None = None,
    index: mailindex.MailIndex | None = None,
    store: mailcases.CaseStore | None = None,
    limit: int = MAX_JUDGED_PER_PASS,
    admission_state_dir: Any = None,
    since: float | None = None,
) -> SettlePass:
    """Judge the pairs that are ready, newest candidate first, up to `limit`
    calls. Model down, unadmitted or capped: nothing is written for that pair,
    the item stays open, and the report counts it as not yet judged."""
    result = SettlePass()
    ctx = mailitems.Context(state_dir, now=now, index=index, store=store)
    ready = pairs(ctx, charter.digest, result, since)
    result.capped = max(0, len(ready) - limit)
    for position, (item, candidate) in enumerate(ready[:limit]):
        bundle = compose(ctx, item, candidate)
        called = roles.run(charter, {BUNDLE_INPUT: bundle.text}, (), model_spec=model_spec,
                           chat=chat, state_dir=state_dir,
                           admission_state_dir=admission_state_dir)
        result.endings[called.status] = result.endings.get(called.status, 0) + 1
        if called.status == roles.Status.UNADMITTED:
            # Admission does not change inside a pass: every later pair would
            # end the same way, without a call.
            result.capped += min(len(ready), limit) - position - 1
            break
        if called.status not in FINAL_ENDINGS:
            continue
        result.judged += 1
        value = called.value if isinstance(called.value, Settlement) else None
        if value is not None:
            result.answers[value.answer] = result.answers.get(value.answer, 0) + 1
        mailitems.write_judgement(state_dir, item.anchor, {
            "item": item.key,
            "kind": item.kind,
            "candidate": candidate.header.digest,
            "charter": charter.name,
            "charter_version": charter.version,
            "charter_digest": charter.digest,
            "model": model_spec,
            "at": now,
            "ending": called.status,
            "why": called.why,
            "answer": value.answer if value else "",
            "reason": value.reason if value else "",
            "citations": list(value.citations) if value else [],
            "tier": candidate.tier,
            "ties": list(candidate.ties),
            "input_digest": called.input_digest,
            "attempts": called.attempts,
            "usage": called.usage,
        })
    return result
