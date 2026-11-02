"""Settler vouches (#142 W5): the owner's word that mail from ONE exact domain
may settle ONE kind of item in his inbox reports.

The tier rule (`mailitems.Context.check`) lets a third party settle an item
only with transactional history in his case store — and a courier or a
payment provider he never writes to has none, so on 2026-11-02 InPost's
"your parcel from Uncico was picked up" could never settle the UNCICO item
(F4). Loosening the history rule would let any authenticated stranger with a
name in its text settle items; instead the missing fact is HIS: once per
domain and meaning, he answers a card that code composed.

- **His act, or nothing.** Written only by `mailitemtool.apply` after his
  card in a task he sent (`inbox_items` action=vouch / revoke). Code OFFERS the
  card — the report's "later mail aish did not weigh" line carries a one-tap
  chip `aish-reply://vouch settler <domain> <meaning>` — but never answers it.
- **Exact domain.** `paczkomaty.pl` vouched does not vouch
  `paczkomaty.pl.example`, `inpost.paczkomaty.pl` or a lookalike: membership
  is an exact string compare (the `vouches.py` law).
- **A closed meaning** (`MEANINGS`) — what kind of ITEM, decided by code over
  recorded facts on the item's own mails (`mailitems.Context.settle_meanings`):
  never a word the model chose, and never a property of the later mail.
- **Permanent until revoked**, visible (`inbox_items` list), every act a line
  in `<state>/mail/settler-vouch-acts.jsonl`; the current set is the newest act
  per (domain, meaning).

A vouch grants ELIGIBILITY only: per item the candidate must still be
authenticated, read, tied by code, free of any caution, later on both clocks —
and the settlement judge must still read it as settling the item.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from . import mailindex

VOUCH, REVOKE = "vouch", "revoke"
VERBS = (VOUCH, REVOKE)
#: What kind of ITEM a vouched domain's mail may settle — each a predicate of
#: the item over recorded facts on its own mails
#: (`mailitems.Context.settle_meanings`): `delivery` — one of them is on a case
#: whose recorded keys include an order or tracking key; `payment` — one of them
#: carries a recorded money fact (the Reader read a pay, the case store recorded
#: an amount or an account).
MEANINGS = ("delivery", "payment")
ACTS_NAME = "settler-vouch-acts.jsonl"

_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}"
                     r"[a-z0-9])?)+")


class VouchError(ValueError):
    """An argument the call cannot proceed with — aish's words."""


def domain(value: Any) -> str:
    """An exact domain, lowercased, or VouchError. No normalisation beyond
    lowercasing and a leading '@' — exactness is the point."""
    text = str(value or "").strip().lower().lstrip("@")
    if not _DOMAIN.fullmatch(text) or len(text) > 253:
        raise VouchError("domain must be an exact domain like example.com")
    return text


def meaning(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in MEANINGS:
        raise VouchError(f"meaning must be one of {', '.join(MEANINGS)}")
    return text


def acts_path(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / ACTS_NAME


def read_acts(state_dir: os.PathLike | str) -> list[dict[str, Any]]:
    """His vouch / revoke acts, oldest first; a line that does not parse, or
    that names no valid domain and meaning, costs that line."""
    try:
        text = acts_path(state_dir).read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("verb") not in VERBS:
                continue
            domain(entry.get("domain"))
            meaning(entry.get("meaning"))
        except (ValueError, VouchError):
            continue
        out.append(entry)
    return out


def current(state_dir: os.PathLike | str | None) -> frozenset[tuple[str, str]]:
    """Every (domain, meaning) he has vouched and not revoked."""
    if state_dir is None:
        return frozenset()
    latest: dict[tuple[str, str], str] = {}
    for act in sorted(read_acts(state_dir), key=lambda a: float(a.get("at") or 0)):
        latest[(str(act["domain"]), str(act["meaning"]))] = str(act["verb"])
    return frozenset(k for k, verb in latest.items() if verb == VOUCH)


def append_act(state_dir: os.PathLike | str, act: dict[str, Any]) -> None:
    path = acts_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(act, sort_keys=True, ensure_ascii=False) + "\n")


def chip_text(vouch_domain: str, vouch_meaning: str) -> str:
    """The words the report's one-tap chip sends — code-built from a validated
    domain and a closed meaning, or "" when either is not."""
    try:
        return f"vouch settler {domain(vouch_domain)} {meaning(vouch_meaning)}"
    except VouchError:
        return ""
