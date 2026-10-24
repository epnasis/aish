"""The verdict store and the `mail_verdict` record (#142 W5, slice S1).

One file per MAIL, content-addressed by the mail's digest (`mailindex`), under
`<state>/mail/verdicts/<dd>/<digest>.json`, holding one record per model that
read it. Re-processing is idempotent: a mail already recorded for this model
AND this exact charter text is skipped — a charter edit (a new digest) is a
new reading, never a silent reuse of the old one.

The record is `docs/trace-contract.md` §3.20's `mail_verdict`. In S1 it is
written HERE and nowhere else: no session log carries one, and nothing reads
one to act. The counters below are a pure scan over these files, in the shape
`roles.scan_counters` and `vocab.scan_counters` use — they report what was
RECORDED, and a store with no records reads as no records, never as healthy.

`docs/mail-assistant.md`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atomic_write, mailcases, mailindex, mailreader, roles, vocab

VERDICTS_DIRNAME = "verdicts"
RECORD_KIND = "mail_verdict"
RECORD_VERSION = 1
FINAL_ENDINGS = frozenset({roles.Status.OK, roles.Status.INVALID, mailreader.GATED})

# The Reader's closed axes, declared as vocabularies so their consultation is
# counted beside every other word list aish matches (`docs/vocabularies.md`).
# They are matched against MODEL output, like `schedule.VERDICT_MARKERS`.
# "Matched" means a definite answer; a miss is `cannot_tell`. In S1 a miss
# acts on nothing; for the slices that act, an undecided axis is a mail held
# or surfaced for him, never one acted on — friction, never consequence.
_AXIS_NOTE = (
    "matched = a definite answer, miss = cannot_tell. In S1 nothing acts; "
    "downstream an undecided axis is held or surfaced, never acted on."
)
for _name in ("DISPOSITIONS", "WANTED", "SENDER_CASE", "SUSPICION"):
    vocab.declare(
        f"mailreader.{_name}",
        getattr(mailreader, _name),
        languages="aish's own verdict words, matched against MODEL output",
        on_miss=vocab.FRICTION,
        structural="`mailreader.validate_answer` refuses any word outside the "
        "vocabulary, so an answer is a word or no answer at all",
        note=_AXIS_NOTE,
    )


def count_verdict(verdict: mailreader.Verdict) -> None:
    """One consultation per axis per verdict: the cannot-tell counters."""
    vocab.note("mailreader.DISPOSITIONS", matched=verdict.disposition != mailreader.CANNOT_TELL)
    vocab.note("mailreader.WANTED", matched=verdict.wanted != mailreader.CANNOT_TELL)
    vocab.note("mailreader.SENDER_CASE", matched=verdict.sender_case != mailreader.CANNOT_TELL)
    vocab.note("mailreader.SUSPICION", matched=verdict.suspicion != mailreader.CANNOT_TELL)


def store_dir(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / VERDICTS_DIRNAME


class VerdictStore:
    """The one code writer of the verdict directory."""

    def __init__(self, state_dir: os.PathLike | str) -> None:
        self.directory = store_dir(state_dir)

    def _path(self, digest: str) -> Path:
        return self.directory / digest[:2] / f"{digest}.json"

    def read(self, digest: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(self._path(digest).read_text())
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def has(self, digest: str, model: str, charter_digest: str) -> bool:
        """Is there a FINAL reading of this mail by this model and charter text?

        An ending that made no model call (`unadmitted`, `unavailable`) is not
        final: once the charter is admitted, or the provider is back, the mail
        is read. An `invalid` ending IS final — a real call answered twice and
        neither answer validated, and asking again would spend money on the
        same question."""
        held = (self.read(digest) or {}).get("by_model", {}).get(model)
        return (
            isinstance(held, dict)
            and held.get("charter_digest") == charter_digest
            and held.get("ending") in FINAL_ENDINGS
        )

    def write(self, record: dict[str, Any]) -> Path:
        digest = str(record["mail"])
        current = self.read(digest) or {"mail": digest, "by_model": {}}
        if not isinstance(current.get("by_model"), dict):
            current = {"mail": digest, "by_model": {}}
        current["by_model"][str(record.get("model") or "")] = record
        path = self._path(digest)
        atomic_write.publish(path, json.dumps(current, ensure_ascii=False, sort_keys=True))
        return path


# ---------------------------------------------------------------- the record


def call_summary(result: roles.Result) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": result.status,
        "attempts": result.attempts,
        "ms": result.ms,
        "input": {"name": result.input_name, "trust": result.input_trust,
                  "chars": result.input_chars, "digest": result.input_digest},
    }
    if result.usage:
        out["usage"] = result.usage
    if result.why:
        out["why"] = result.why[:400]
    return out


def build_record(
    *,
    header: mailindex.Header,
    facts: mailreader.Facts,
    reading: mailreader.Reading,
    charter: roles.Charter,
    model: str,
    observation: mailcases.Observation | None,
    flags: mailcases.CaseFlags,
    code_coordinates: tuple[str, ...],
    now: float | None = None,
) -> dict[str, Any]:
    """The `mail_verdict` record — every field code-written except `verdict`,
    which is the validated, capped Reader answer."""
    bundle = reading.bundle
    refs = bundle.cases if bundle is not None else {}
    verdict = reading.verdict
    chosen = ""
    if verdict is not None:
        chosen = refs.get(verdict.case, verdict.case)
    return {
        "kind": RECORD_KIND,
        "v": RECORD_VERSION,
        "mail": header.digest,
        "provider_id": header.provider_id,
        "at": header.at,
        "at_source": header.at_source,
        "direction": header.direction,
        "model": model,
        "charter": charter.name,
        "version": charter.version,
        "charter_digest": charter.digest,
        "ending": reading.status,
        "why": reading.why[:400],
        "gate": {
            "auth": facts.auth.as_json(),
            "grade": facts.grade.as_json(),
            "flags": [f.as_json() for f in facts.flags],
            "newsletter": facts.newsletter.as_json(),
        },
        "candidates": [
            {"ref": f"c{n}", **cand.as_json()} for n, cand in enumerate(facts.candidates, 1)
        ],
        "verdict": verdict.as_json() if verdict is not None else None,
        "case_choice": chosen,
        "case_flags": flags.as_json(),
        "coordinates": {
            "reader": [c.value for c in verdict.coordinates] if verdict is not None else [],
            "code": list(code_coordinates),
        },
        "attachments": {
            "count": len(header.attachments),
            "names": [a.filename[:120] for a in header.attachments[:10]],
            # Declared, not implied: the Reader is toolless and is shown names only.
            "read": False,
        },
        "asks": [list(a) for a in reading.asks],
        "calls": [call_summary(r) for r in reading.calls],
        "observation": None if observation is None else {
            "keys": sorted(observation.keys),
            "cases": list(observation.cases),
            "created": observation.created,
            "admitted": list(observation.admitted),
        },
        "recorded_at": time.time() if now is None else now,
    }


# ---------------------------------------------------------------- the counters


@dataclass
class Counters:
    """What the shadow pipeline recorded, per model, from the store alone."""

    model: str = ""
    records: int = 0
    endings: dict[str, int] = field(default_factory=dict)
    axes: dict[str, dict[str, int]] = field(default_factory=dict)
    kinds: dict[str, int] = field(default_factory=dict)  # self-named kinds: proposals
    flags: dict[str, int] = field(default_factory=dict)
    case_flags: dict[str, int] = field(default_factory=dict)
    gated_by: dict[str, int] = field(default_factory=dict)
    asks: int = 0

    def cannot_tell(self, axis: str) -> int:
        return self.axes.get(axis, {}).get(mailreader.CANNOT_TELL, 0)


def _bump(table: dict[str, int], key: str, by: int = 1) -> None:
    table[key] = table.get(key, 0) + by


def scan_records(records) -> dict[str, Counters]:
    """`records` is any iterable of `mail_verdict` records."""
    out: dict[str, Counters] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("kind") != RECORD_KIND:
            continue
        model = str(record.get("model") or "")
        c = out.setdefault(model, Counters(model=model))
        c.records += 1
        _bump(c.endings, str(record.get("ending") or ""))
        gate = record.get("gate") or {}
        for flag in gate.get("flags") or ():
            _bump(c.flags, str((flag or {}).get("what") or ""))
        newsletter = gate.get("newsletter") or {}
        if newsletter.get("gated"):
            _bump(c.gated_by, "+".join(newsletter.get("by") or ()) or "?")
        for name, cases in (record.get("case_flags") or {}).items():
            if cases:
                _bump(c.case_flags, str(name))
        c.asks += len(record.get("asks") or ())
        verdict = record.get("verdict")
        if not isinstance(verdict, dict):
            continue
        for axis in (*mailreader.AXES, "case", "deadline"):
            value = str(verdict.get(axis) or "")
            if axis == "case" and value not in mailreader.CASE_WORDS:
                value = "candidate"
            if axis == "deadline" and value not in mailreader.DEADLINE_WORDS:
                value = "date"
            _bump(c.axes.setdefault(axis, {}), value)
        kind = str(verdict.get("kind") or "").strip()
        if kind:
            _bump(c.kinds, kind)
    return out


def iter_store(state_dir: os.PathLike | str):
    """Every record in the store. One unreadable file costs that file."""
    for path in sorted(store_dir(state_dir).glob("*/*.json")):
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            yield from (raw.get("by_model") or {}).values()


def render_counters(counters: dict[str, Counters]) -> str:
    """Raw counts only. No rate is printed: a rate over a shadow sample says
    more than the sample can carry (`docs/replay.md` R3)."""
    if not counters:
        return "no mail verdicts recorded"
    lines = []
    for model, c in sorted(counters.items()):
        lines.append(f"{model or '(no model)'}: {c.records} record(s)")
        lines.append("  endings: " + _pairs(c.endings))
        for axis, values in sorted(c.axes.items()):
            lines.append(f"  {axis}: " + _pairs(values))
        if c.gated_by:
            lines.append("  newsletter-gated by: " + _pairs(c.gated_by))
        if c.flags:
            lines.append("  gate flags: " + _pairs(c.flags))
        if c.case_flags:
            lines.append("  case flags: " + _pairs(c.case_flags))
        if c.kinds:
            top = sorted(c.kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
            lines.append("  self-named kinds (proposals): " + _pairs(dict(top)))
        lines.append(f"  asks for more: {c.asks}")
    return "\n".join(lines)


def _pairs(table: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(table.items(), key=lambda kv: (-kv[1], kv[0])))
