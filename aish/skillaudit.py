"""The import auditor (#467 slice 3): a toolless role that reads a staged
third-party skill and may only ADD warnings to its review card.

`aish/charters/skill-auditor.md` asks one model, in a sealed conversation
(`roles.run`), what in the skill's files a reviewer should look at, in a closed
vocabulary of categories. Everything around the model is code:

- **Code composes the bundle** (`bundle_text`): every staged text file, one
  JSON document, capped per file and in total. Anything cut is listed in the
  bundle, and then the only summary the validator accepts is
  `could_not_assess` — a partial audit never reads as a whole one.
- **Code validates the answer** (`validate_answer`): categories and the summary
  from closed vocabularies that can say `cannot_tell` / `could_not_assess`;
  every finding names a file in the bundle, and its quote must be text that
  file actually holds — a model cannot put words in a stranger's mouth.
- **Code composes what the owner sees** (`Audit.headline`): an empty result
  reads "flagged nothing — this is not a verdict", never a green state; a call
  that did not happen says so with the observed reason.

Restrict-only (#467 P5): nothing here approves, refuses, or changes what
installs. Findings go to the card and the lock entry — never into the tool
result the acting model reads. `docs/knowledge-layer.md` §The import auditor.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import provenance, roles

AUDITOR = "skill-auditor"
BUNDLE_INPUT = "bundle"

# The model the import path names for the auditor (`caller` class, the mail
# Reader's precedent): admission binds to this one spec, so a pass recorded
# for it is what lets the auditor run. The owner's local server (reached at
# AISH_LOCAL_URL), by his decision of 2026-11-12 on that day's measurement
# (#467 ledger): this model answered every call of three exam runs, while
# Gemini Flash refused 21 of 32 calls with 503. With no AISH_LOCAL_URL the
# backend refuses, and the card says the auditor did not run, with that
# reason. The environment variable overrides it.
MODEL_ENV = "AISH_SKILL_AUDITOR_MODEL"
DEFAULT_MODEL = "local:mlx-community/Qwen3.6-35B-A3B-8bit"

CANNOT_TELL = "cannot_tell"
CATEGORIES = (
    "sends_data_out",
    "reads_secrets",
    "runs_downloaded_code",
    "hides_or_obfuscates",
    "instructs_the_assistant",
    "installs_packages",
    "tampers_with_assistant_config",
    CANNOT_TELL,
)
CATEGORY_LABELS = {
    "sends_data_out": "sends data out",
    "reads_secrets": "reads secrets",
    "runs_downloaded_code": "runs downloaded code",
    "hides_or_obfuscates": "hides or obfuscates",
    "instructs_the_assistant": "addresses the assistant",
    "installs_packages": "installs packages",
    "tampers_with_assistant_config": "writes aish's own configuration",
    CANNOT_TELL: "the auditor could not tell",
}

FLAGS_RAISED = "flags_raised"
NOTHING_FLAGGED = "nothing_flagged"
COULD_NOT_ASSESS = "could_not_assess"
SUMMARIES = (FLAGS_RAISED, NOTHING_FLAGGED, COULD_NOT_ASSESS)

#: The caps a `findings` output must declare in the charter's frontmatter.
FINDINGS_CAPS = ("max_findings", "max_quote_chars", "max_reason_chars")

# What one audit may read. A CHOICE: the anthropics `pdf` skill, the largest
# real one examined, is well under the total; a skill over it is audited as
# far as it fits and answers `could_not_assess`.
FILE_CHARS = 40_000
TOTAL_CHARS = 120_000

NOT_RUN = "not_run"  # the import path's own status: no role call was made


# ---------------------------------------------------------------- the bundle


def bundle_text(skill: str, files: Iterable[tuple[str, str]]) -> str:
    """The auditor's whole input: every file's text as lines, and a `cut`
    list saying, in aish's words, what it was not shown."""
    shown: list[dict[str, Any]] = []
    cut: list[str] = []
    room = TOTAL_CHARS
    for rel, text in files:
        if room <= 0:
            cut.append(f"{rel}: not shown — the bundle was full")
            continue
        limit = min(FILE_CHARS, room)
        part = text[:limit]
        if len(part) < len(text):
            cut.append(f"{rel}: only the first {len(part)} of {len(text)} characters shown")
        room -= len(part)
        shown.append({"path": rel, "lines": part.split("\n")})
    return json.dumps({"skill": skill, "files": shown, "cut": cut}, ensure_ascii=False)


def _bundle_of(inputs: dict[str, str]) -> dict[str, Any]:
    try:
        raw = json.loads(inputs.get(BUNDLE_INPUT, "") or "{}")
    except ValueError as exc:
        raise ValueError(f"the bundle is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the bundle is not a JSON object")
    return raw


def _files_of(bundle: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in bundle.get("files") or ():
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            lines = entry.get("lines") or []
            out[entry["path"]] = "\n".join(str(line) for line in lines)
    return out


def _spaced(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------- the answer


@dataclass(frozen=True)
class Finding:
    file: str
    category: str
    quote: str  # text the named file holds, flattened, capped, aish's markers broken
    reason: str

    def as_json(self) -> dict[str, str]:
        return {"file": self.file, "category": self.category, "quote": self.quote,
                "reason": self.reason}


@dataclass(frozen=True)
class Findings:
    """A validated auditor answer: the value `roles.run` returns."""

    summary: str
    findings: tuple[Finding, ...]

    def as_json(self) -> dict[str, Any]:
        return {"summary": self.summary, "findings": [f.as_json() for f in self.findings]}


def validate_answer(shape: roles.Shape, payload: Any, inputs: dict[str, str]) -> Findings:
    """An auditor answer as `Findings`, or `ValueError` with words the model
    can act on (fed back on the one corrective retry)."""
    bundle = _bundle_of(inputs)
    files = _files_of(bundle)
    if not isinstance(payload, dict):
        raise ValueError("the reply must be a JSON object")
    summary = str(payload.get("summary") or "").strip().lower()
    if summary not in SUMMARIES:
        raise ValueError(f"summary must be one of {', '.join(SUMMARIES)} "
                         f"(got {payload.get('summary')!r})")
    raw = payload.get("findings")
    if not isinstance(raw, list):
        raise ValueError('"findings" must be a list (empty when you flag nothing)')
    if len(raw) > shape.caps["max_findings"]:
        raise ValueError(f"at most {shape.caps['max_findings']} findings — keep the ones "
                         "a reviewer most needs to see")
    findings: list[Finding] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("every finding must be an object")
        path = str(entry.get("file") or "")
        if path not in files:
            raise ValueError(f"finding file {path!r} is not a file in the bundle "
                             f"({', '.join(sorted(files)) or 'none'})")
        category = str(entry.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(CATEGORIES)} "
                             f"(got {entry.get('category')!r})")
        quote = roles.capped(str(entry.get("quote") or ""), shape.caps["max_quote_chars"])
        if not quote:
            raise ValueError(f"a finding in {path} needs a quote: the text you point at")
        # Whitespace runs compare as one space on both sides: a model re-flows
        # indentation, and that is not words it put in the file's mouth.
        if quote not in _spaced(files[path]):
            raise ValueError(f"the quote {quote[:60]!r} is not text that {path} holds — "
                             "copy it from the file character for character")
        reason = roles.capped(str(entry.get("reason") or ""), shape.caps["max_reason_chars"])
        if not reason:
            raise ValueError(f"a finding in {path} needs a reason")
        findings.append(Finding(path, category, provenance.disarm_markers(quote),
                                provenance.disarm_markers(reason)))
    if summary == NOTHING_FLAGGED and findings:
        raise ValueError('"nothing_flagged" goes with an empty findings list')
    if summary == FLAGS_RAISED and not findings:
        raise ValueError('"flags_raised" needs at least one finding')
    if bundle.get("cut") and summary != COULD_NOT_ASSESS:
        raise ValueError('the bundle says some of the skill was not shown ("cut"), so the '
                         'summary must be "could_not_assess"')
    return Findings(summary, tuple(findings))


def shape_fields(caps: dict[str, int]) -> tuple[roles.Field, ...]:
    """Declared so the wiring law and the counters can see them: the two
    closed vocabularies and the two capped text fields — which no wiring
    carries anywhere."""
    return (
        roles.Field("summary", "enum", values=SUMMARIES),
        roles.Field("category", "enum", values=CATEGORIES),
        roles.Field("quote", "text", max_chars=caps["max_quote_chars"]),
        roles.Field("reason", "text", max_chars=caps["max_reason_chars"]),
    )


def contract_text(shape: roles.Shape) -> str:
    caps = shape.caps
    return "\n".join([
        "Reply with ONE JSON object and nothing else. No prose before or after it, no code fence.",
        "",
        "{",
        f'  "summary": one of {", ".join(json.dumps(s) for s in SUMMARIES)},',
        f'  "findings": [ ... ] — at most {caps["max_findings"]}, each:',
        '    {"file": a "path" from the bundle\'s files,',
        f'     "category": one of {", ".join(json.dumps(c) for c in CATEGORIES)},',
        f'     "quote": at most {caps["max_quote_chars"]} characters copied from that file, '
        "character for character,",
        f'     "reason": at most {caps["max_reason_chars"]} characters, plain text, saying '
        "what the quoted text does}",
        "}",
        "",
        '"nothing_flagged" goes with an empty findings list; "flags_raised" with at least one '
        'finding. When the bundle\'s "cut" list is not empty, the summary is '
        '"could_not_assess". A quote the named file does not hold, a file the bundle does '
        "not have, or a word outside a vocabulary is rejected and you are asked once more.",
    ])


def tally(value: Findings) -> dict[str, dict[str, int]]:
    categories: dict[str, int] = {}
    for finding in value.findings:
        categories[finding.category] = categories.get(finding.category, 0) + 1
    return {"summary": {value.summary: 1}, "category": categories}


# ---------------------------------------------------------------- exam assertions


def _expect_summary(expected: Any, value: Any) -> list[str]:
    allowed = [str(v) for v in expected or ()]
    return [] if value.summary in allowed else [
        f"summary: expected one of {allowed}, got {value.summary!r}"]


def _categories(value: Any) -> set[str]:
    return {f.category for f in value.findings}


def _expect_categories_include(expected: Any, value: Any) -> list[str]:
    got = _categories(value)
    return [f"no finding in category {c!r} (got {sorted(got)})"
            for c in expected or () if str(c) not in got]


def _expect_categories_absent(expected: Any, value: Any) -> list[str]:
    got = _categories(value)
    return [f"a finding in category {c!r}, which this case must not raise"
            for c in expected or () if str(c) in got]


def _expect_files_flagged(expected: Any, value: Any) -> list[str]:
    named = {f.file for f in value.findings}
    return [f"no finding names {p!r}" for p in expected or () if str(p) not in named]


def _expect_files_not_flagged(expected: Any, value: Any) -> list[str]:
    named = {f.file for f in value.findings}
    return [f"a finding names {p!r}, which this case must not flag"
            for p in expected or () if str(p) in named]


def _expect_absent(expected: Any, value: Any) -> list[str]:
    blob = json.dumps(value.as_json(), ensure_ascii=False).casefold()
    return [f"the output still carries {needle!r}" for needle in expected or ()
            if str(needle).casefold() in blob]


ASSERTIONS: dict[str, Callable[[Any, Any], list[str]]] = {
    "summary_in": _expect_summary,
    "categories_include": _expect_categories_include,
    "categories_absent": _expect_categories_absent,
    "files_flagged": _expect_files_flagged,
    "files_not_flagged": _expect_files_not_flagged,
    "absent": _expect_absent,
}


def check_expectation(case_name: str, expect: dict[str, Any]) -> None:
    """Refuse at load time an exam case no answer could meet."""
    for word in expect.get("summary_in") or ():
        if roles._yaml_word(word, case_name) not in SUMMARIES:
            raise roles.CharterError(
                f"golden pair {case_name!r} expects summary {word!r}, outside {list(SUMMARIES)}")
    for key in ("categories_include", "categories_absent"):
        for word in expect.get(key) or ():
            if roles._yaml_word(word, case_name) not in CATEGORIES:
                raise roles.CharterError(
                    f"golden pair {case_name!r} names category {word!r} in {key}, outside "
                    f"{list(CATEGORIES)}")
    clash = set(expect.get("categories_include") or ()) & set(
        expect.get("categories_absent") or ())
    if clash:
        raise roles.CharterError(
            f"golden pair {case_name!r} both requires and forbids {sorted(clash)}")


# ---------------------------------------------------------------- the import path


def model_spec() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def file_digests(files: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """sha256 of each audited file's text, as read and as aish's own write
    stores it (`files.plan_write` adds a final newline), so a later install
    can say whether the files it installed are the ones that were audited."""
    out: dict[str, list[str]] = {}
    for rel, text in files:
        written = text if text.endswith("\n") or not text else text + "\n"
        out[rel] = sorted({_sha256(text), _sha256(written)})
    return out


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Audit:
    """What one import's audit did, for the card, the lock and the record."""

    status: str  # a `roles.Status`, or NOT_RUN
    why: str = ""
    model: str = ""
    summary: str = ""
    findings: tuple[Finding, ...] = ()
    charter_version: str = ""
    charter_digest: str = ""
    input_digest: str = ""
    files: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ran(self) -> bool:
        return self.status == roles.Status.OK

    def headline(self) -> str:
        """The one sentence the owner reads about the audit — aish's words,
        never the model's, and never a green state."""
        by = f"the auditor ({self.model})" if self.model else "the auditor"
        if self.status == roles.Status.OK:
            if self.summary == NOTHING_FLAGGED:
                return f"{by} flagged nothing — this is not a verdict"
            if self.summary == COULD_NOT_ASSESS:
                return (f"{by} could not assess this skill"
                        f"{' and flagged what it could' if self.findings else ''}"
                        " — this is not a verdict")
            count = len(self.findings)
            return (f"{by} raised {count} finding{'s' if count != 1 else ''} — a model's "
                    "reading of the files, not a verdict")
        if self.status == roles.Status.INVALID:
            return (f"{by} answered, and neither of its two answers passed aish's checks "
                    f"({self.why}) — nothing it said is shown")
        return f"{by} did not run: {self.why}"

    def card(self) -> dict[str, Any]:
        """The review card's audit block. Quotes are a stranger's text: they
        are already flattened, capped and stripped of aish's markers."""
        return {
            "status": self.status,
            "headline": self.headline(),
            "findings": [
                {**f.as_json(), "label": CATEGORY_LABELS.get(f.category, f.category)}
                for f in self.findings
            ],
        }

    def lock_entry(self, covers: str) -> dict[str, Any]:
        """The lock's copy — a convenience; the durable record is the role's
        own (R7). `covers` says which bytes the audit read."""
        entry: dict[str, Any] = {
            "charter": AUDITOR,
            "version": self.charter_version,
            "charter_digest": self.charter_digest,
            "model": self.model,
            "status": self.status,
            "covers": covers,
        }
        if self.why:
            entry["why"] = self.why
        if self.status == roles.Status.OK:
            entry["summary"] = self.summary
            entry["findings"] = [f.as_json() for f in self.findings]
            entry["input_digest"] = self.input_digest
        return entry

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status, "why": self.why, "model": self.model,
            "summary": self.summary, "findings": [f.as_json() for f in self.findings],
            "charter_version": self.charter_version, "charter_digest": self.charter_digest,
            "input_digest": self.input_digest, "files": dict(self.files),
        }

    @classmethod
    def from_json(cls, raw: Any) -> Audit | None:
        if not isinstance(raw, dict) or not raw.get("status"):
            return None
        findings = tuple(
            Finding(str(f.get("file", "")), str(f.get("category", "")),
                    str(f.get("quote", "")), str(f.get("reason", "")))
            for f in raw.get("findings") or () if isinstance(f, dict)
        )
        files = raw.get("files") if isinstance(raw.get("files"), dict) else {}
        return cls(
            status=str(raw["status"]), why=str(raw.get("why") or ""),
            model=str(raw.get("model") or ""), summary=str(raw.get("summary") or ""),
            findings=findings, charter_version=str(raw.get("charter_version") or ""),
            charter_digest=str(raw.get("charter_digest") or ""),
            input_digest=str(raw.get("input_digest") or ""),
            files={str(k): [str(d) for d in v] for k, v in (files or {}).items()
                   if isinstance(v, list)},
        )


def covers(audit: Audit, installed: dict[str, str]) -> str:
    """Which bytes the audit read, said against what was installed: every
    installed file must be one that was audited, with the same bytes (or
    those bytes plus the final newline aish's write adds), and nothing
    audited may be missing."""
    if audit.status != roles.Status.OK:
        return "nothing — the auditor did not answer"
    differ = sorted(
        rel for rel in set(audit.files) | set(installed)
        if installed.get(rel) not in audit.files.get(rel, [])
    )
    if not differ:
        return "the installed files"
    return ("the files as staged for review; these differ from what was installed: "
            + ", ".join(differ))


def load_auditor() -> roles.Charter:
    return roles.load_charters()[AUDITOR]


def audit(
    skill: str,
    files: list[tuple[str, str]],
    *,
    state_dir: os.PathLike | str | None,
    model: str | None = None,
    chat: Callable[..., Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[Audit, roles.Charter | None, roles.Result | None]:
    """Run the auditor over a staged skill's text files. Never raises for an
    operational reason: a charter that does not load, a model that is not
    admitted, down or unreachable, all come back as an `Audit` whose
    headline says so. The charter and the role's result come back too, for
    the caller's R7 record (None when the charter did not load)."""
    spec = model if model is not None else model_spec()
    digests = file_digests(files)
    try:
        charter = load_auditor()
    except (roles.CharterError, KeyError, OSError) as exc:
        return Audit(NOT_RUN, why=f"the auditor's charter does not load ({exc})",
                     model=spec, files=digests), None, None
    result = roles.run(charter, {BUNDLE_INPUT: bundle_text(skill, files)}, (),
                       model_spec=spec, chat=chat, state_dir=state_dir,
                       should_stop=should_stop)
    value = result.value if isinstance(result.value, Findings) else None
    found = Audit(
        status=result.status,
        why=result.why,
        model=result.model or spec,
        summary=value.summary if value else "",
        findings=value.findings if value else (),
        charter_version=charter.version,
        charter_digest=charter.digest,
        input_digest=result.input_digest,
        files=digests,
    )
    return found, charter, result


def role_record(charter: roles.Charter, result: roles.Result) -> dict[str, Any]:
    """The §D7 `role` record, in `Agent._record_role`'s shape, for a caller
    with no session (`aish skill import`)."""
    record: dict[str, Any] = {
        "kind": "role",
        "charter": charter.name,
        "version": charter.version,
        "role_kind": charter.kind,
        "status": result.status,
        "model": result.model,
        "attempts": result.attempts,
        "ms": result.ms,
        "degradation": charter.degradation,
        "input": {"name": result.input_name, "trust": result.input_trust,
                  "chars": result.input_chars, "digest": result.input_digest},
    }
    if result.why:
        record["why"] = result.why
    if result.usage:
        record["usage"] = result.usage
    if result.value is not None:
        record["output"] = result.value.as_json()
        record["flags"] = roles.tally_flags(charter.output, result.value)
    return record


def records_path(state_dir: os.PathLike | str) -> Path:
    """Where `aish skill import` keeps its role records: no session log
    exists on that path, and R7 says every call leaves one."""
    return roles.roles_state_dir(state_dir) / f"{AUDITOR}.jsonl"


def append_record(state_dir: os.PathLike | str, record: dict[str, Any]) -> None:
    """Append-only. Raises OSError; the caller says so."""
    path = records_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"step": record}, ensure_ascii=False) + "\n")
