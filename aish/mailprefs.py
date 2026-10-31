"""The owner's inbox preferences (#142 W5): standing feedback the daily report
obeys — "just like feedback I would give a real assistant."

Five kinds, each a closed set of TYPED fields (`docs/mail-assistant.md`
§Inbox preferences):

- `hide` — mail from a sender or a domain, addressed to a recipient, of a
  Reader kind, or with a subject phrase: left out of the report's sections;
- `show_first` — mail from a sender or domain, or of a kind: first in its section;
- `not_a_follow_up` — a counterparty, or one conversation (until a new message
  arrives in it): left out of Follow-ups;
- `remind` — a note shown in the report's Follow-ups on a date, every given
  weekday, or when a counterparty has not answered for N days. Shown in the
  report only; nothing is pushed for it;
- `correction` — "this needed action" / "this was junk" on one mail: an owner
  ground-truth label in a ledger the review file reads. NOT applied to the
  Reader; a later, measured charter round may adopt them.

**The model never writes the stored text.** `plan` validates the typed fields
and composes the one sentence (`sentence`) that is the card, the stored
`text` and the list line. **Hiding can never suppress risk** (`floor`): mail
the Reader judged suspicious, whose sender does not match its case, whose
payment account changed, or that failed authentication from a sender that
passed it before is shown anyway, marked with why.

The store lives in the STATE tree (it names his correspondents; the config
tree is pushed to a backup): one JSON file per preference under
`<state>/mail/preferences/`, written through `atomic_write` by this module
only, under its own lock; one unreadable file costs that preference and is
counted where he reads it.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atomic_write, mailcases, mailindex, mailreport

HIDE = "hide"
SHOW_FIRST = "show_first"
NOT_A_FOLLOW_UP = "not_a_follow_up"
REMIND = "remind"
CORRECTION = "correction"
KINDS = (HIDE, SHOW_FIRST, NOT_A_FOLLOW_UP, REMIND, CORRECTION)
#: The kinds the report APPLIES; corrections are recorded, never applied.
APPLIED_KINDS = (HIDE, SHOW_FIRST, NOT_A_FOLLOW_UP, REMIND)

LOOK_ACTIONS = ("list",)
CHANGE_ACTIONS = ("add", "edit", "remove")
ACTIONS = (*LOOK_ACTIONS, *CHANGE_ACTIONS)

#: Which typed field says WHAT a preference is about. Exactly one per entry.
MATCHERS: dict[str, tuple[str, ...]] = {
    HIDE: ("sender", "domain", "recipient", "mail_kind", "subject"),
    SHOW_FIRST: ("sender", "domain", "mail_kind"),
    NOT_A_FOLLOW_UP: ("counterparty", "conversation"),
}
REMIND_TARGETS = ("case", "counterparty")
REMIND_WHEN = ("due", "every", "no_answer_days")
LABELS = ("needed_action", "junk")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
#: Every field a preference may carry; anything else the model sends is dropped.
FIELDS = ("sender", "domain", "recipient", "mail_kind", "subject", "counterparty",
          "conversation", "case", "due", "every", "no_answer_days", "note", "mail", "label")

PHRASE_MIN = 3
PHRASE_MAX = 80
PREF_NOTE_MAX = 200
NO_ANSWER_MAX_DAYS = 365
#: A dated reminder stays in the report this many days from its due date, so a
#: report that failed on the day does not lose it. A CHOICE.
DUE_SHOWN_DAYS = 7
MAIL_REF_CHARS = 12
PREFS_LOCK_WAIT_S = 10.0

PREFS_DIRNAME = "preferences"
ACTS_NAME = "preference-acts.jsonl"
LOCK_NAME = "preferences.lock"

# aish's heading above a listing. The values he approved on a card, but a
# phrase or address in one may have been read off a mail, so it is outside
# content under the banner like the case store.
LOOK_NOTE = (
    "[aish: the owner's saved inbox preferences, as aish's code read them. Each line is the "
    "sentence he approved on a card; addresses and phrases in them may have come from mail. "
    "Changing one (add, edit, remove) is his act: call inbox_preferences with that action, "
    "and he approves it on a card.]\n"
)
DONE_NOTE = "[aish: the owner approved this preference change and aish's code saved it.]\n"
#: The one sentence that may confirm memory, in the tool's answer after a save.
SAVED_WORDS = "SAVED"

# The false promise (owner session 2026-10-31: "Understood … should be ignored
# in inbox reviews", which nothing enforced). Rides the report chat's kept note
# heading, which is aish's voice in every later turn of that chat.
MEMORY_RULE = (
    "Never tell the owner you will remember, ignore, hide or stop listing something in "
    "future reports unless an inbox_preferences answer in this chat began with SAVED as "
    "or SAVED: deleted; otherwise say plainly that it is not saved, and offer to save it"
)
_PREF_ID = re.compile(r"pref-[0-9a-f]{10}")

_ADDRESS = re.compile(r"[a-z0-9._%+'-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
_DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
_HEX = re.compile(r"[0-9a-f]{8,64}")


class PrefError(ValueError):
    """An argument or store state the call cannot proceed with — aish's words."""


def _safe(value: Any, cap: int) -> str:
    from .mailcasetool import clean  # one sanitiser for the case and preference stores
    return mailreport.safe(clean, value, cap)


def _q(value: Any, cap: int = PHRASE_MAX) -> str:
    # The quote marks are aish's: a value cannot carry its own and close them.
    return "“" + _safe(value, cap).replace("“", "\"").replace("”", "\"") + "”"


def _day(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "no date"


# ---------------------------------------------------------------- the entry


@dataclass
class Preference:
    id: str
    kind: str
    fields: dict[str, Any]
    text: str  # code's sentence, composed at plan time from `fields`
    created_at: float
    source: dict[str, Any]
    updated_at: float = 0.0

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "fields": self.fields, "text": self.text,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "source": self.source}

    @classmethod
    def from_json(cls, raw: Any) -> Preference:
        if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
            raise ValueError("not a preference")
        fields = raw.get("fields")
        if not isinstance(fields, dict):
            raise ValueError("no fields")
        # A file is re-validated on read: a hand edit or corrupt bytes must not
        # widen what a preference hides (an empty phrase would match every mail).
        problem = _shape_problem(str(raw["kind"]), fields)
        if problem:
            raise ValueError(problem)
        # The id is printed in the report and the listing: only aish's shape.
        if not _PREF_ID.fullmatch(str(raw.get("id") or "")):
            raise ValueError("not a preference id")
        return cls(id=str(raw.get("id") or ""), kind=str(raw["kind"]), fields=dict(fields),
                   text=str(raw.get("text") or ""), created_at=float(raw.get("created_at") or 0),
                   source=dict(raw.get("source") or {}),
                   updated_at=float(raw.get("updated_at") or 0))

    def key(self) -> tuple:
        return (self.kind, tuple(sorted((k, str(v)) for k, v in self.fields.items())))


def _shape_problem(kind: str, fields: dict[str, Any]) -> str:
    """'' when the typed fields make a whole preference of `kind`."""
    if kind in MATCHERS:
        given = [f for f in MATCHERS[kind] if fields.get(f) not in (None, "")]
        if len(given) != 1:
            return (f"a {kind} preference names exactly one of "
                    f"{', '.join(MATCHERS[kind])}")
        value = str(fields[given[0]])
        if given[0] in ("mail_kind", "subject") and len(value) < PHRASE_MIN:
            return f"{given[0]} must be at least {PHRASE_MIN} characters"
        if given[0] == "conversation" and not _HEX.fullmatch(value):
            return "conversation must be a mail id"
        return ""
    if kind == REMIND:
        targets = [f for f in REMIND_TARGETS if fields.get(f) not in (None, "")]
        whens = [f for f in REMIND_WHEN if fields.get(f) not in (None, "")]
        if len(targets) != 1:
            return "a reminder is about exactly one of case or counterparty"
        if len(whens) != 1:
            return "a reminder has exactly one of due, every or no_answer_days"
        if whens[0] == "no_answer_days" and targets[0] != "counterparty":
            return "no_answer_days needs a counterparty"
        if not str(fields.get("note") or "").strip():
            return "a reminder needs a note saying what to remind him of"
        return ""
    if kind == CORRECTION:
        if not _HEX.fullmatch(str(fields.get("mail") or "")):
            return "a correction names one mail id"
        if fields.get("label") not in LABELS:
            return f"label must be one of {', '.join(LABELS)}"
        return ""
    return f"kind must be one of {', '.join(KINDS)}"


# ---------------------------------------------------------------- the store


def prefs_dir(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / PREFS_DIRNAME


@dataclass
class Store:
    """Every saved preference, read whole. Read-only; the writers are below."""

    state_dir: Path
    entries: dict[str, Preference] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, state_dir: os.PathLike | str) -> Store:
        store = cls(Path(state_dir))
        for path in sorted(prefs_dir(state_dir).glob("*.json")):
            try:
                pref = Preference.from_json(json.loads(path.read_text()))
            except (OSError, ValueError, TypeError, AttributeError):
                store.unreadable.append(path.name)
                continue
            store.entries[pref.id or path.stem] = pref
        return store

    def ordered(self) -> list[Preference]:
        return sorted(self.entries.values(), key=lambda p: (p.created_at, p.id))

    def of(self, *kinds: str) -> list[Preference]:
        return [p for p in self.ordered() if p.kind in kinds]

    def find(self, pref_id: str) -> Preference | None:
        return self.entries.get(pref_id.strip())


def corrections(state_dir: os.PathLike | str) -> dict[str, str]:
    """mail digest -> his label (`needed_action` | `junk`), newest wins — the
    ground-truth ledger the review file reads."""
    out: dict[str, str] = {}
    for pref in Store.load(state_dir).of(CORRECTION):
        out[str(pref.fields["mail"])] = str(pref.fields["label"])
    return out


@contextlib.contextmanager
def store_lock(
    state_dir: os.PathLike | str, wait_s: float = PREFS_LOCK_WAIT_S,
    *, sleep: Callable[[float], None] = time.sleep,
) -> Iterator[bool]:
    """The preference store's one writer at a time — its OWN lock, not the
    watcher's: no watcher pass writes preferences, so a long backfill must
    not block a save."""
    path = mailindex.mail_dir(state_dir) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        waited = 0.0
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if waited >= wait_s:
                    yield False
                    return
                sleep(0.5)
                waited += 0.5
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_acts(state_dir: os.PathLike | str) -> list[dict[str, Any]]:
    path = mailindex.mail_dir(state_dir) / ACTS_NAME
    try:
        text = path.read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _append_act(state_dir: os.PathLike | str, act: dict[str, Any]) -> None:
    path = mailindex.mail_dir(state_dir) / ACTS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(act, sort_keys=True, ensure_ascii=False) + "\n")


def _write(state_dir: os.PathLike | str, pref: Preference) -> None:
    atomic_write.publish(prefs_dir(state_dir) / f"{pref.id}.json",
                         json.dumps(pref.as_json(), sort_keys=True, ensure_ascii=False,
                                    indent=1))


def _new_id(kind: str, fields: dict[str, Any], now: float) -> str:
    seed = json.dumps([kind, fields, now, os.urandom(8).hex()], sort_keys=True)
    return "pref-" + hashlib.sha256(seed.encode()).hexdigest()[:10]


# ---------------------------------------------------------------- planning


@dataclass
class Plan:
    action: str
    kind: str
    fields: dict[str, Any]
    sentence: str  # the preference's own text — code's words, typed values quoted
    card: str  # what the card says
    target: str = ""  # edit/remove: the preference id
    was: str = ""  # edit/remove: its text as the card showed it


def _address(value: Any, name: str) -> str:
    text = str(value or "").strip().lower().strip("<>")
    if not _ADDRESS.fullmatch(text):
        raise PrefError(f"{name} must be an e-mail address")
    return text


def _domain(value: Any) -> str:
    text = str(value or "").strip().lower().lstrip("@")
    if not _DOMAIN.fullmatch(text):
        raise PrefError("domain must be a domain like example.com")
    return text


def _phrase(value: Any, name: str) -> str:
    text = mailreport.fold(str(value or ""))[:PHRASE_MAX].strip()
    if len(text) < PHRASE_MIN or not re.search(r"\w", text):
        raise PrefError(f"{name} must be a phrase of at least {PHRASE_MIN} characters")
    return text


def _resolve_mail(state_dir: os.PathLike | str, ref: Any) -> mailindex.Header:
    text = str(ref or "").strip().lower()
    if text.startswith("ref "):
        text = text[4:].strip()
    if not _HEX.fullmatch(text):
        raise PrefError("mail must be the ref the report or /mail printed (hex, 8+ characters)")
    shard = mailindex.mail_dir(state_dir) / mailindex.INDEX_DIRNAME / text[:2]
    names = sorted({p.stem for p in shard.glob(f"{text}*.json")})
    if len(names) != 1:
        raise PrefError(f"{'no' if not names else 'more than one'} mail in the index starts "
                        f"with {text}")
    header = mailindex.load_header(state_dir, names[0])
    if header is None:
        raise PrefError(f"the index entry for mail {text} could not be read")
    return header


def _typed(state_dir: os.PathLike | str, kind: str, args: dict[str, Any]) -> dict[str, Any]:
    """The typed fields of `kind`, normalised; the rest of `args` is dropped."""
    given = {k: args[k] for k in FIELDS if args.get(k) not in (None, "")}
    out: dict[str, Any] = {}
    for name in ("sender", "recipient", "counterparty"):
        if name in given:
            out[name] = _address(given[name], name)
    if "domain" in given:
        out["domain"] = _domain(given["domain"])
    for name in ("mail_kind", "subject"):
        if name in given:
            out[name] = _phrase(given[name], name)
    if "conversation" in given:
        out["conversation"] = _resolve_mail(state_dir, given["conversation"]).digest
    if "mail" in given:
        out["mail"] = _resolve_mail(state_dir, given["mail"]).digest
    if "case" in given:
        case_id = str(given["case"]).strip()
        case = mailcases.CaseStore(state_dir).find(case_id)
        if case is None:
            raise PrefError(f"there is no case {case_id!r}; cases action=list shows them")
        out["case"] = case.id
    if "due" in given:
        try:
            out["due"] = datetime.date.fromisoformat(str(given["due"]).strip()).isoformat()
        except ValueError as exc:
            raise PrefError("due must be a date as YYYY-MM-DD") from exc
    if "every" in given:
        day = str(given["every"]).strip().lower()
        if day not in WEEKDAYS:
            raise PrefError(f"every must be a weekday: {', '.join(WEEKDAYS)}")
        out["every"] = day
    if "no_answer_days" in given:
        try:
            days = int(given["no_answer_days"])
        except (TypeError, ValueError) as exc:
            raise PrefError("no_answer_days must be a whole number of days") from exc
        if not 1 <= days <= NO_ANSWER_MAX_DAYS:
            raise PrefError(f"no_answer_days must be 1 to {NO_ANSWER_MAX_DAYS}")
        out["no_answer_days"] = days
    if "note" in given:
        out["note"] = " ".join(str(given["note"]).split())[:PREF_NOTE_MAX]
    if "label" in given:
        label = str(given["label"]).strip().lower()
        if label not in LABELS:
            raise PrefError(f"label must be one of {', '.join(LABELS)}")
        out["label"] = label
    # Only the fields this kind uses: a stray field must not ride into the store.
    allowed = {*MATCHERS.get(kind, ()),
               *((*REMIND_TARGETS, *REMIND_WHEN, "note") if kind == REMIND else ()),
               *(("mail", "label") if kind == CORRECTION else ())}
    out = {k: v for k, v in out.items() if k in allowed}
    problem = _shape_problem(kind, out)
    if problem:
        raise PrefError(problem)
    return out


def _mail_words(state_dir: os.PathLike | str, digest: str) -> str:
    header = mailindex.load_header(state_dir, digest)
    if header is None:
        return f"mail {digest[:MAIL_REF_CHARS]}"
    return (f"{_q(header.subject or '(no subject)', mailreport.SUBJECT_CHARS)} from "
            f"{_safe(header.from_addr or '(no address)', mailreport.ADDRESS_CHARS)} "
            f"({_day(header.at)})")


def _mail_matcher_words(f: dict[str, Any]) -> str:
    if "sender" in f:
        return f"mail from {_safe(f['sender'], 120)}"
    if "domain" in f:
        return f"mail from anyone at {_safe(f['domain'], 120)}"
    if "recipient" in f:
        return f"mail addressed to {_safe(f['recipient'], 120)}"
    if "mail_kind" in f:
        return f"mail the Reader calls {_q(f['mail_kind'])}"
    return f"mail whose subject contains {_q(f.get('subject'))}"


def sentence(state_dir: os.PathLike | str, kind: str, fields: dict[str, Any]) -> str:
    """The preference in plain words, from the typed fields alone."""
    f = fields
    if kind == HIDE:
        return (f"hide {_mail_matcher_words(f)} in your inbox reports (mail flagged as a "
                "risk is shown anyway)")
    if kind == SHOW_FIRST:
        return f"show {_mail_matcher_words(f)} first in each section of your inbox reports"
    if kind == NOT_A_FOLLOW_UP:
        if "counterparty" in f:
            return (f"do not list {_safe(f['counterparty'], 120)} under Follow-ups in your "
                    "inbox reports (mail flagged as a risk is shown anyway)")
        return (f"do not list the conversation of {_mail_words(state_dir, f['conversation'])} "
                "under Follow-ups — until a new message arrives in it (mail flagged as a risk "
                "is shown anyway)")
    if kind == REMIND:
        if "case" in f:
            case = mailcases.CaseStore(state_dir).find(str(f["case"]))
            about = (f"case {_q(case.display_title())}" if case is not None
                     else f"case {_safe(f['case'], 40)}")
        else:
            about = _safe(f["counterparty"], 120)
        note = _q(f["note"], PREF_NOTE_MAX)
        if "due" in f:
            when = f"on {f['due']} (and for {DUE_SHOWN_DAYS} days after)"
        elif "every" in f:
            when = f"every {str(f['every']).capitalize()}"
        else:
            when = (f"when {about} has not answered your last message to them for "
                    f"{f['no_answer_days']} day(s)")
            return (f"remind you in your inbox report {when}: {note} — shown in the report "
                    "only, no separate notification")
        tail = (" — ends when the case closes" if "case" in f else "")
        return (f"remind you in your inbox report {when} about {about}: {note}{tail} — shown "
                "in the report only, no separate notification")
    if kind == CORRECTION:
        what = ("needed your action" if f["label"] == "needed_action" else "was junk")
        return (f"record that the mail {_mail_words(state_dir, f['mail'])} {what} — kept as "
                "your ground truth for measuring the mail Reader; it changes no reading and "
                "no report")
    raise PrefError(f"kind must be one of {', '.join(KINDS)}")


def plan(state_dir: os.PathLike | str, args: dict[str, Any]) -> Plan:
    """Validate one add / edit / remove and compose its card. Raises PrefError."""
    action = str(args.get("action") or "")
    if action not in CHANGE_ACTIONS:
        raise PrefError(f"action must be one of {', '.join(ACTIONS)}")
    store = Store.load(state_dir)
    if action in ("edit", "remove"):
        pref_id = str(args.get("id") or "").strip()
        held = store.find(pref_id)
        if held is None:
            raise PrefError(f"there is no preference {pref_id!r}; action=list shows them")
        if action == "remove":
            return Plan(action, held.kind, dict(held.fields), held.text,
                        f"Delete your preference: {_q(held.text, 400)}. What it did stops "
                        "from the next report.", target=held.id, was=held.text)
        kind = str(args.get("kind") or held.kind)
    else:
        kind = str(args.get("kind") or "")
        held = None
    if kind not in KINDS:
        raise PrefError(f"kind must be one of {', '.join(KINDS)}")
    fields = _typed(state_dir, kind, args)
    text = sentence(state_dir, kind, fields)
    probe = Preference("", kind, fields, text, 0.0, {})
    for other in store.entries.values():
        if other.key() == probe.key() and (held is None or other.id != held.id):
            raise PrefError(f"that is already saved as {other.id}: {_safe(other.text, 400)}")
    if held is not None:
        card = (f"Change your preference {_q(held.text, 400)} to: from now on, {text}. Save?")
        return Plan(action, kind, fields, text, card, target=held.id, was=held.text)
    lead = "Record" if kind == CORRECTION else "From now on"
    return Plan(action, kind, fields, text, f"{lead}: {text}. Save?")


@dataclass
class Applied:
    ok: bool
    text: str
    pref_id: str = ""


def apply(
    state_dir: os.PathLike | str, planned: Plan, *, source: dict[str, Any], now: float,
    lock: Callable[..., Any] | None = None,
) -> Applied:
    """Under the store's lock, against a FRESH load: write, and append the act."""
    with (lock or store_lock)(state_dir) as held:
        if not held:
            return Applied(False, "NOT SAVED: another process held the preference store's lock; "
                                  "nothing changed and nothing will be remembered.")
        store = Store.load(state_dir)
        try:
            if planned.target:
                current = store.find(planned.target)
                if current is None:
                    raise PrefError("that preference is gone")
                if current.text != planned.was:
                    raise PrefError("that preference was changed after the card was composed")
            if planned.action == "remove":
                (prefs_dir(state_dir) / f"{planned.target}.json").unlink()
                pref_id = planned.target
            else:
                probe = Preference("", planned.kind, planned.fields, planned.sentence, 0.0, {})
                for other in store.entries.values():
                    if other.key() == probe.key() and other.id != planned.target:
                        raise PrefError(f"that is already saved as {other.id}")
                if planned.action == "edit":
                    old = store.find(planned.target)
                    if old is None:
                        raise PrefError("that preference is gone")
                    pref = Preference(old.id, planned.kind, planned.fields, planned.sentence,
                                      old.created_at, {**old.source, "edited_by": source},
                                      updated_at=now)
                else:
                    pref = Preference(_new_id(planned.kind, planned.fields, now), planned.kind,
                                      planned.fields, planned.sentence, now, source)
                _write(state_dir, pref)
                pref_id = pref.id
        except PrefError as exc:
            return Applied(False, f"NOT SAVED: {exc} (the store changed since the card was "
                                  "shown); nothing changed and nothing will be remembered.")
        except OSError as exc:
            return Applied(False, f"NOT SAVED: writing the preference store failed ({exc}); "
                                  "list the preferences to see what is there.")
        ledger_note = ""
        try:
            _append_act(state_dir, {"at": now, "action": planned.action, "id": pref_id,
                                    "kind": planned.kind, "fields": planned.fields,
                                    "text": planned.sentence, "source": source})
        except OSError as exc:
            # The change itself landed; only its history line did not.
            ledger_note = f" (its line in the acts ledger could not be written: {exc})"
    if planned.action == "remove":
        return Applied(True, f"{SAVED_WORDS}: deleted {pref_id}. It no longer applies from the "
                             f"next inbox report.{ledger_note}", pref_id)
    return Applied(True, f"{SAVED_WORDS} as {pref_id}: {planned.sentence}. It applies from the "
                         "next inbox report — you may now tell the owner it is remembered."
                         + ledger_note, pref_id)


# ---------------------------------------------------------------- LOOK


def list_text(state_dir: os.PathLike | str) -> str:
    store = Store.load(state_dir)
    prefs = store.ordered()
    lines = [f"{len(prefs)} saved inbox preference(s):" if prefs else
             "No saved inbox preferences."]
    for pref in prefs:
        origin = ("seeded" if pref.source.get("seed") else "you approved it")
        lines.append(f"- {pref.id} · {pref.kind} · {_safe(pref.text, 400)} · saved "
                     f"{_day(pref.created_at)} ({origin})"
                     + (f", edited {_day(pref.updated_at)}" if pref.updated_at else ""))
    if store.unreadable:
        lines.append(f"({len(store.unreadable)} preference file(s) could not be read and are "
                     "not applied)")
    return "\n".join(lines)


# ---------------------------------------------------------------- applying


def _domain_of(address: str) -> str:
    return address.rpartition("@")[2]


def _at_domain(address: str, domain: str) -> bool:
    host = _domain_of(address)
    return host == domain or host.endswith("." + domain)


def _phrase_in(phrase: str, text: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", mailreport.fold(text)))


def matches_mail(pref: Preference, header: mailindex.Header | None, kind_word: str) -> bool:
    """Does `pref` (hide / show_first) match this mail?"""
    if header is None:
        return False
    f = pref.fields
    sender = (header.from_addr or "").lower()
    if "sender" in f:
        return sender == f["sender"]
    if "domain" in f:
        return bool(sender) and _at_domain(sender, str(f["domain"]))
    if "recipient" in f:
        return f["recipient"] in {a.lower() for a in (*header.to, *header.cc)}
    if "mail_kind" in f:
        return _phrase_in(str(f["mail_kind"]), kind_word)
    if "subject" in f:
        return _phrase_in(str(f["subject"]), header.subject or "")
    return False


def matches_party(pref: Preference, address: str) -> bool:
    """A sender / domain / counterparty preference against an address."""
    f = pref.fields
    address = address.lower()
    if "sender" in f:
        return address == f["sender"]
    if "domain" in f:
        return _at_domain(address, str(f["domain"]))
    if "counterparty" in f:
        return address == f["counterparty"]
    return False


def floor(record: dict[str, Any] | None, *, authenticated_before: bool) -> list[str]:
    """Why a mail must be shown whatever a hide says — code's words, each a
    recorded fact. The suppression attack: hiding is the dangerous direction
    in a chat whose words a mail may have steered."""
    reasons = list(mailreport.caution_marks(record))
    gate = (record or {}).get("gate")
    auth = gate.get("auth") if isinstance(gate, dict) else None
    if (authenticated_before and isinstance(auth, dict)
            and auth.get("authenticated") is False):
        reasons.append("it failed authentication, and earlier mail from this sender passed it")
    return reasons


def weekday(now: float) -> str:
    return WEEKDAYS[datetime.datetime.fromtimestamp(now).weekday()]


def due_dated(due: str, now: float) -> bool:
    try:
        day = datetime.date.fromisoformat(due)
    except ValueError:
        return False
    today = datetime.date.fromtimestamp(now)
    return day <= today <= day + datetime.timedelta(days=DUE_SHOWN_DAYS)


# ---------------------------------------------------------------- the seed

SEED_SOURCE = {"seed": "epic #142, the owner's review of the first report chat (2026-10-29), "
                       "preferences (a)-(c)"}
#: Three of the owner's four (a)-(d) from the epic comment, as typed entries;
#: the other two are `SEED_NOT_WRITTEN`. Each line says what the comment's
#: words fixed and what they did not.
SEEDS: tuple[tuple[str, str, dict[str, Any], str], ...] = (
    ("a", HIDE, {"sender": "familysafety@microsoft.com"},
     "(a) familysafety@microsoft.com — hidden by sender address. The comment also says "
     "'delete': nothing deletes mail yet (the cleanup lane is not built)."),
    ("b", HIDE, {"subject": "super duo"},
     "(b) Super Duo — the comment names no address, so this hides mail whose SUBJECT "
     "contains 'Super Duo'. Mail that says it only in the sender's name is not hidden; "
     "when one shows, say 'hide mail from <its address>' in the report chat."),
    ("c", HIDE, {"recipient": "adam@wenda.eu"},
     "(c) mail addressed to adam@wenda.eu — hidden by RECIPIENT (To or Cc)."),
)
SEED_NOT_WRITTEN = (
    "(c) kuba@wenda… — the comment truncates the address, so it is NOT seeded (aish does not "
    "guess a domain). In the report chat: 'hide mail addressed to kuba@<the domain>'.",
    "(d) the InPost and DPD sport-food parcels, picked up — that is a CASE CLOSE, not a "
    "preference. In the report chat say 'I picked up the InPost and DPD sport-food parcels' "
    "(the cases tool lists them and proposes close on a card), or call cases action=list and "
    "close each.",
)


def seed(state_dir: os.PathLike | str, *, now: float) -> list[str]:
    """Write the owner's seeded preferences. A re-run writes nothing that is
    already saved, and never re-creates one he deleted (the acts ledger says
    so). Returns what happened, one line each."""
    out: list[str] = []
    with store_lock(state_dir) as held:
        if not held:
            return ["NOT SEEDED: another process held the preference store's lock"]
        store = Store.load(state_dir)
        # A seed he deleted on a card stays deleted: his act outranks the seed.
        removed = {(a.get("kind"), json.dumps(a.get("fields"), sort_keys=True))
                   for a in read_acts(state_dir) if a.get("action") == "remove"}
        for _label, kind, fields, words in SEEDS:
            text = sentence(state_dir, kind, fields)
            probe = Preference("", kind, fields, text, 0.0, {})
            existing = next((p for p in store.entries.values() if p.key() == probe.key()), None)
            if existing is not None:
                out.append(f"already saved as {existing.id}: {words}")
                continue
            if (kind, json.dumps(fields, sort_keys=True)) in removed:
                out.append(f"NOT re-seeded — you deleted it: {words}")
                continue
            pref = Preference(_new_id(kind, fields, now), kind, dict(fields), text, now,
                              dict(SEED_SOURCE))
            _write(state_dir, pref)
            _append_act(state_dir, {"at": now, "action": "add", "id": pref.id, "kind": kind,
                                    "fields": pref.fields, "text": text,
                                    "source": pref.source})
            store.entries[pref.id] = pref
            out.append(f"saved {pref.id}: {words}")
    return out + [f"not a preference: {line}" for line in SEED_NOT_WRITTEN]
