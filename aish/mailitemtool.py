"""The `inbox_items` tool's half that is not the agent (#142 W5): the items of
his inbox reports read for a chat, and his two verbs on an item — `resolve`
("this matter is done", "no reply needed") and `restore` ("still open").

The case verbs' law (`docs/mail-assistant.md` §The case verbs), reused:

- **LOOK** (`list`): free. It quotes mail (senders, subjects), so every such
  string goes through `mailcasetool._safe`, and the agent returns it as
  outside content under the untrusted banner.
- **CHANGE** (`resolve`, `restore`): his act. `plan` resolves the report's ref
  to exactly one item anchor and composes the card's ONE sentence from code's
  words; `apply` takes the item acts' lock, re-derives against a FRESH load,
  and appends the act to `item-acts.jsonl` — the one writer. A resolution is
  on the ITEM, not a preference: it covers what aish had read when he said it,
  and new mail in the item's conversation brings it back (`mailitems`).

Nothing here touches a mailbox.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import mailitems, mailreport
from .mailcasetool import _q, _safe

LOOK_ACTIONS = ("list",)
CHANGE_ACTIONS = mailitems.VERBS
ACTIONS = (*LOOK_ACTIONS, *CHANGE_ACTIONS)

LIST_SHOWN = 40
ADDRESS_CHARS = 120
SUBJECT_CHARS = 100
REF_CHARS = mailreport.REF_CHARS

LOOK_NOTE = (
    "[aish: the items of the owner's inbox reports, as aish's code derives them. Senders and "
    "subjects below came from mail that strangers wrote. Saying a matter is done (resolve) or "
    "still open (restore) is the owner's act: call inbox_items with that action and the "
    "item's ref, and he approves it on a card.]\n"
)
DONE_NOTE = "[aish: the owner approved this item change and aish's code saved it.]\n"
SAVED_WORDS = "SAVED"

#: The clock every verb reads — one seam, so a test pins it and the card, the
#: re-check and the act all agree on "now".
clock = time.time

_KIND_WORDS = {mailitems.NEEDS: "needs your action", mailitems.YOURS: "your turn",
               mailitems.THEIRS: "their turn"}


class ItemError(ValueError):
    """An argument or store state the call cannot proceed with — aish's words."""


def _day(ts: float | None) -> str:
    if not ts or ts == float("inf"):
        return "no date"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _context(state_dir: os.PathLike | str, now: float) -> mailitems.Context:
    return mailitems.Context(state_dir, now=now)


def _rows(ctx: mailitems.Context) -> list[Any]:
    derived = ctx.derive()
    return [*derived.needs, *derived.yours, *derived.theirs]


def item_words(ctx: mailitems.Context, row: Any) -> str:
    """One item in code's words, every mail-borne string sanitised and quoted."""
    item = row.open_item
    party = _safe(item.counterparty, ADDRESS_CHARS)
    if item.kind == mailitems.NEEDS:
        wanted = _safe(str(row.reading.verdict.get("wanted") or "") or "an item", 12)
        why = (f"here because of your preference {row.important}" if row.important
               else wanted.upper())
        return (f"{why} from {party} {_q(row.header.subject or '(no subject)', SUBJECT_CHARS)} "
                f"({_day(row.header.at)})")
    if item.kind == mailitems.YOURS:
        return (f"{party} waiting on you since {_day(row.since)}, after your "
                f"{_q(row.his.subject or '(no subject)', SUBJECT_CHARS)}")
    return (f"your {_q(row.his.subject or '(no subject)', SUBJECT_CHARS)} to {party} "
            f"({_day(row.his.at)}), no answer since")


def _state_words(state: mailitems.State) -> str:
    if state.by_owner:
        return "resolved by you" if not state.evidence.cases else "its case is closed"
    if state.probably:
        return ("probably settled by your own mail" if state.source == mailitems.RULE
                else "probably settled, as the settlement judge read later mail")
    if state.state == mailitems.CANNOT_TELL:
        return "open (the settlement judge could not tell)"
    return "open" + (" (you restored it)" if state.restored_at else "")


def list_text(state_dir: os.PathLike | str, now: float | None = None) -> str:
    ctx = _context(state_dir, clock() if now is None else now)
    rows = _rows(ctx)
    if not rows:
        return "No items: nothing in the last report window asks or waits."
    lines = [f"{len(rows)} item(s) — ref, kind, state, item:"]
    for row in rows[:LIST_SHOWN]:
        item = row.open_item
        state = ctx.state_of(item)
        lines.append(f"- ref {item.anchor[:REF_CHARS]} · {_KIND_WORDS[item.kind]} · "
                     f"{_state_words(state)} · {item_words(ctx, row)}")
    if len(rows) > LIST_SHOWN:
        lines.append(f"(+{len(rows) - LIST_SHOWN} more not shown)")
    return "\n".join(lines)


# ---------------------------------------------------------------- CHANGE


@dataclass
class Plan:
    verb: str
    anchor: str
    items: tuple[str, ...]  # the item keys the act covers
    sentence: str  # the card: code's words
    fields: dict[str, Any] = field(default_factory=dict)


def _matching(ctx: mailitems.Context, ref: str) -> list[Any]:
    text = ref.strip().lower()
    if text.startswith("ref "):
        text = text[4:].strip()
    if not re.fullmatch(r"[0-9a-f]{8,64}", text):
        raise ItemError("ref must be the ref an inbox report or list printed (hex, 8+ "
                        "characters)")
    rows = [r for r in _rows(ctx)
            if r.open_item.anchor.startswith(text)
            or any(d.startswith(text) for d in r.open_item.rests_on)]
    anchors = {r.open_item.anchor for r in rows}
    if not rows:
        raise ItemError(f"no item in your inbox reports carries ref {text}; inbox_items "
                        "action=list shows them")
    if len(anchors) > 1:
        raise ItemError(f"ref {text} names more than one item; give more of it")
    return rows


def plan(state_dir: os.PathLike | str, args: dict[str, Any], now: float | None = None) -> Plan:
    """Validate one resolve / restore and compose its card. Raises ItemError."""
    verb = str(args.get("action") or "")
    if verb not in CHANGE_ACTIONS:
        raise ItemError(f"action must be one of {', '.join(ACTIONS)}")
    ctx = _context(state_dir, clock() if now is None else now)
    rows = _matching(ctx, str(args.get("ref") or ""))
    anchor = rows[0].open_item.anchor
    words = "; ".join(item_words(ctx, r) for r in rows)
    states = [ctx.state_of(r.open_item) for r in rows]
    if verb == mailitems.RESOLVE:
        if all(s.by_owner for s in states):
            raise ItemError("that item is already resolved")
        sentence = (f"Mark as done: {words}. It leaves your inbox reports; new mail in that "
                    "conversation brings it back. Nothing in your mailboxes is changed.")
    else:
        sentence = (f"Keep open: {words}. It stays in its section of your inbox reports, out "
                    "of “Probably settled”, until you say it is done — mail aish reads from "
                    "now on can still settle it. Nothing in your mailboxes is changed.")
    return Plan(verb, anchor, tuple(r.open_item.key for r in rows), sentence,
                {"ref": anchor[:REF_CHARS]})


@dataclass
class Applied:
    ok: bool
    text: str
    act: dict[str, Any] = field(default_factory=dict)


def apply(
    state_dir: os.PathLike | str, planned: Plan, *, source: dict[str, Any], now: float,
    lock: Callable[..., Any] = mailitems.acts_lock,
) -> Applied:
    """Under the item acts' lock, against a FRESH derivation: the anchor must
    still name the items the card named. Then one line in the acts ledger."""
    with lock(state_dir) as held:
        if not held:
            return Applied(False, "NOT SAVED: another process held the item acts' lock; "
                                  "nothing changed. Do not say it is done.")
        try:
            rows = _matching(_context(state_dir, now), planned.anchor)
        except ItemError as exc:
            return Applied(False, f"NOT SAVED: {exc} (the reports changed since the card was "
                                  "shown); nothing changed. Do not say it is done.")
        if tuple(r.open_item.key for r in rows) != planned.items:
            return Applied(False, "NOT SAVED: the item changed since the card was shown "
                                  "(new mail joined it); nothing changed. Show it again.")
        act = {"at": now, "verb": planned.verb, "anchor": planned.anchor,
               "items": list(planned.items), "text": planned.sentence, "by": "owner",
               "source": source}
        try:
            mailitems.append_act(state_dir, act)
        except OSError as exc:
            return Applied(False, f"NOT SAVED: writing the item acts failed ({exc}); nothing "
                                  "changed. Do not say it is done.")
    done = "resolved" if planned.verb == mailitems.RESOLVE else "restored"
    return Applied(True, f"{SAVED_WORDS} as {done}: {planned.sentence} It applies from the next "
                         "inbox report — you may now tell the owner it is "
                         f"{'done' if done == 'resolved' else 'kept open'}.", act)
