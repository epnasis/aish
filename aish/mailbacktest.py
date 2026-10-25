"""`aish-mailbacktest` — the shadow pipeline measured on the owner's history
(#142 W5, slice S1).

    aish-mailbacktest --account pawel@wenda.eu --days 90 \\
        --arm gemini:<flash> --arm local:<mi model> --reader-max 300

**Frozen clocks.** The index and the case store are built in arrival order
inside a SCRATCH run directory, and every question the pipeline asks is asked
"as of" the mail under review: its conversation, its sender's grade, its
candidate cases and their history. Nothing on the Reader's side of the line
can see a later message. Only the ground truth reads the future — that is what
ground truth is.

**Deterministic first, models on demand.** The backfill fetches and folds
every mail in the window with no model call. The Reader then runs per ARM
(one model each) over a bounded selection, so the money spent is a number the
driver chose. Verdicts land in the run directory's own verdict store, keyed by
model, so a backtest interrupted half-way resumes where it stopped.

**Ground truth is TIERED by how much an observation can carry.** Strong: he
replied (an outbound message replies into the conversation after it), he
trashed it, he starred it. Weak: he archived it, or read it and left it in the
inbox. Near-null: it sits unread in the inbox. Spam is Gmail's verdict, not
his, and is set apart. Starred and read-and-kept are this slice's ADDITIONS to
the design's three tiers, and are reported under their own names so either can
be dropped by reading past it.

**Raw counts only, and no verdict on the pipeline** (`docs/replay.md` R3, R5):
confusion counts per axis per tier, `cannot_tell` as its own column, and a
headline that is the DESTRUCTIVE direction — mail the pipeline would have
expired that he kept or replied to. The report never prints a rate.

**Two inboxes, one index** (`--source`, default gmail only): Gmail through
`GwsMailbox`, Tuta through the shipped `mailtuta.TutaMailbox` — enumeration,
never SEARCH; no \\Seen; nothing keyed by its unstable UIDs. What a source
cannot show is set apart in its own words, never guessed into a tier.

The mailboxes are seams (`list`/`get`, and the Tuta runner), so the tests
drive this end to end against fakes. `docs/mail-assistant.md`.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    mailcases,
    mailexport,
    mailindex,
    mailreader,
    mailtuta,
    mailverdicts,
    mailwatch,
    paths,
    roles,
)
from .mailsource import Failure

DEFAULT_DAYS = 90
DEFAULT_READER_MAX = 200
LISTING_LIMIT = 20000
DISAGREEMENT_SHOWN = 50

# Gmail empties Trash after this many days, so "he trashed it" is observable
# only for mail newer than this when the backtest runs. Older trashed mail is
# not in any listing at all.
TRASH_RETENTION_DAYS = 30

STRONG, WEAK, NEAR_NULL, SET_APART = "strong", "weak", "near-null", "set-apart"

# Truth label -> tier. Order is precedence: a mail he replied to and later
# trashed is "replied".
TRUTH_TIERS = {
    "replied": STRONG,
    "trashed": STRONG,
    "starred": STRONG,  # addition to the design's tiering
    "archived": WEAK,
    "kept_read": WEAK,  # addition to the design's tiering
    "unread": NEAR_NULL,
    "spam": SET_APART,
    # In a Tuta inbox, but the listing carried no flags for it: read or
    # unread is not known, so it is neither.
    "seen_unknown": SET_APART,
    # A Tuta mail this run's listing did not name in any enumerated folder:
    # where it is now was not observed. Never "archived" — Tuta archiving is
    # a move to a folder the backtest does not enumerate.
    "unobserved": SET_APART,
}
KEPT = frozenset({"replied", "starred", "archived", "kept_read", "unread"})
NO_VERDICT = "no_verdict"

# Labels the Tuta half writes in Gmail's vocabulary, so `truth_of` reads one
# shape: the folder a mail was enumerated in, and its IMAP flags.
SEEN_UNKNOWN = "SEEN_UNKNOWN"
SOURCES = ("gmail", "tuta")


def tuta_labels(role: str, flags: tuple[str, ...] | None) -> tuple[str, ...]:
    """A Tuta mail's folder role and flags as the labels `truth_of` reads.
    `\\Flagged` is STARRED (tutalib's own vocabulary maps "starred" to it);
    no `\\Seen` in a flags list that was listed is UNREAD; no flags list at
    all is SEEN_UNKNOWN, never a guess either way."""
    held = {str(f).lower() for f in flags or ()}
    labels = {{"inbox": "INBOX", "sent": "SENT", "trash": "TRASH"}[role]}
    if "\\flagged" in held:
        labels.add("STARRED")
    if role == "inbox":
        if flags is None:
            labels.add(SEEN_UNKNOWN)
        elif "\\seen" not in held:
            labels.add("UNREAD")
    return tuple(sorted(labels))


@dataclass(frozen=True)
class Truth:
    label: str
    tier: str
    facts: tuple[str, ...]  # every observation, not only the winning one


def truth_of(
    index: mailindex.MailIndex, header: mailindex.Header, labels: tuple[str, ...]
) -> Truth:
    """What the mailbox shows he did with this mail. Reads the future on
    purpose; nothing on the Reader path calls it."""
    held = set(labels)
    facts = []
    if index.later_replies(header.digest):
        facts.append("replied")
    if "TRASH" in held:
        facts.append("trashed")
    if "STARRED" in held:
        facts.append("starred")
    if "SPAM" in held:
        facts.append("spam")
    if "INBOX" in held:
        if SEEN_UNKNOWN in held:
            facts.append("seen_unknown")
        else:
            facts.append("unread" if "UNREAD" in held else "kept_read")
    elif "TRASH" not in held and "SPAM" not in held:
        # On Gmail no INBOX label IS archived. Elsewhere only the enumerated
        # folders were seen, so the absence says nothing.
        facts.append("archived" if header.mailbox == "gmail" else "unobserved")
    if "spam" in facts and "replied" not in facts:
        return Truth("spam", SET_APART, tuple(facts))
    label = next(t for t in TRUTH_TIERS if t in facts)
    return Truth(label, TRUTH_TIERS[label], tuple(facts))


# ---------------------------------------------------------------- the run


@dataclass
class Backfill:
    index: mailindex.MailIndex
    store: mailcases.CaseStore
    labels: dict[str, tuple[str, ...]]
    #: Totals over every source, and the index's own counts. Gmail's
    #: `listing_failed` / `quota_stopped` stay here, as before Tuta joined.
    counts: dict[str, int] = field(default_factory=dict)
    #: Per source: its own counts, and for Tuta what the enumeration observed
    #: (folders, depth, trash exposure).
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)


_SUMMED = ("listed", "fetched", "fetch_failed", "unparseable", "outside_window", "held")


def _union(labels: dict[str, tuple[str, ...]], digest: str, more: tuple[str, ...]) -> None:
    labels[digest] = tuple(sorted(set(labels.get(digest, ())) | set(more)))


def backfill(
    mailbox: Any,
    state_dir: Path,
    owner_addresses: frozenset[str],
    *,
    days: int,
    now: float,
    tuta: Any = None,
) -> Backfill:
    """Fetch the window and fold it, oldest first, into ONE scratch index and
    case store — Gmail (inbox, archive, sent and trash) when `mailbox` is
    given, Tuta (INBOX, Sent, Trash) when `tuta` is. Read-only; no model
    call. One index for both, so a reply he sent from either inbox is his act
    in the conversation, and the frozen-clock order spans both."""
    floor = now - days * 86400
    index = mailindex.MailIndex(state_dir)
    parsed: list[mailindex.Parsed] = []
    labels: dict[str, tuple[str, ...]] = {}
    counts: dict[str, int] = {key: 0 for key in _SUMMED}
    sources: dict[str, dict[str, Any]] = {}
    with _fetched_log(state_dir) as remember:
        if mailbox is not None:
            gmail_counts = _backfill_gmail(mailbox, state_dir, index, owner_addresses, now=now,
                                           floor=floor, parsed=parsed, labels=labels,
                                           remember=remember)
            sources["gmail"] = dict(gmail_counts)
            counts.update({k: v for k, v in gmail_counts.items() if k not in _SUMMED})
        if tuta is not None:
            sources["tuta"] = _backfill_tuta(tuta, index, owner_addresses, floor=floor,
                                             parsed=parsed, labels=labels, remember=remember)
            sources["tuta"]["window_days"] = days
    for source_counts in sources.values():
        for key in _SUMMED:
            counts[key] += int(source_counts.get(key) or 0)
    store = mailcases.CaseStore(state_dir)
    mailwatch.ingest(index, store, parsed)
    counts["in_index"] = len(index.by_digest)
    counts["cases"] = len(store.cases)
    counts["index_unreadable"] = len(index.health.unreadable)
    counts["message_id_collisions"] = len(index.health.collisions)
    return Backfill(index, store, labels, counts, sources)


def _backfill_gmail(
    mailbox: Any,
    state_dir: Path,
    index: mailindex.MailIndex,
    owner_addresses: frozenset[str],
    *,
    now: float,
    floor: float,
    parsed: list[mailindex.Parsed],
    labels: dict[str, tuple[str, ...]],
    remember: Callable[[dict[str, Any]], None],
) -> dict[str, int]:
    since = datetime.datetime.fromtimestamp(floor, datetime.UTC)
    ids = mailbox.list(f"after:{since:%Y/%m/%d}", LISTING_LIMIT, include_trash=True)
    counts = {key: 0 for key in _SUMMED}
    if ids is None:
        counts["listing_failed"] = 1
        ids = []
    counts["listed"] = len(ids)
    # A resumed run re-buys no fetch: an id this run's index already holds,
    # whose labels were recorded when it was fetched, is not fetched again.
    seen = _read_fetched(state_dir)
    held_ids = {h.provider_id: d for d, h in index.by_digest.items()
                if h.provider_id and h.mailbox == "gmail"}
    # One mailbox may serve several runs in a process: an exhaustion from an
    # earlier run is not this one's.
    if hasattr(mailbox, "quota_exhausted"):
        mailbox.quota_exhausted = False
    ordered = list(dict.fromkeys(ids))
    for position, message_id in enumerate(ordered):
        if getattr(mailbox, "quota_exhausted", False):
            # The adapter spent its retries on a quota refusal: every
            # further get would buy the same refusal, each after a full
            # backoff. The rest are counted not fetched; a resumed run
            # into this directory fetches them and re-buys nothing else.
            counts["fetch_failed"] += len(ordered) - position
            counts["quota_stopped"] = 1
            break
        known = seen.get(message_id)
        if known is not None and known.get("outside"):
            # Fetched by an earlier run and outside the window: counted
            # once, as outside, never also as held.
            counts["outside_window"] += 1
            continue
        if known is not None and message_id in held_ids:
            counts["held"] += 1
            _union(labels, held_ids[message_id], tuple(known.get("labels") or ()))
            continue
        msg = mailbox.get(message_id)
        if msg is None:
            counts["fetch_failed"] += 1
            continue
        counts["fetched"] += 1
        item = mailindex.from_gmail(msg, owner_addresses)
        if item is None:
            counts["unparseable"] += 1
            continue
        outside = item.header.at < floor
        remember({"id": message_id, "digest": item.header.digest,
                  "labels": list(item.labels), "outside": outside})
        if outside:
            counts["outside_window"] += 1
            continue
        parsed.append(item)
        _union(labels, item.header.digest, item.labels)
    return counts


def _tuta_log_id(digest: str) -> str:
    # Tuta UIDs renumber on a bridge restart, so the run's log keys a Tuta
    # observation by its digest: a remembered UID would name another message.
    return f"tuta-digest:{digest}"


def _backfill_tuta(
    tuta: Any,
    index: mailindex.MailIndex,
    owner_addresses: frozenset[str],
    *,
    floor: float,
    parsed: list[mailindex.Parsed],
    labels: dict[str, tuple[str, ...]],
    remember: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """The Tuta half: enumerate (never SEARCH) INBOX, Sent and Trash back to
    `floor` through the shipped adapter, skip a fetch only on its identity
    match (never a UID), fetch the rest without \\Seen. Labels come from the
    folder and the flags THIS listing carried, logged per digest."""
    counts: dict[str, Any] = {key: 0 for key in _SUMMED}
    history = tuta.history(floor, LISTING_LIMIT)
    if history is None:
        failure = getattr(tuta, "last_failure", None)
        counts["listing_failed"] = 1
        counts["listing_failure"] = (failure.describe() if isinstance(failure, Failure)
                                     else "the listing failed")
        return counts
    roles_by_ref: dict[str, str] = {}
    for role in ("sent", "trash", "inbox"):
        for ref in getattr(history, role):
            roles_by_ref[ref] = role
    counts["listed"] = len(roles_by_ref)
    counts["listed_by_folder"] = {role: len(getattr(history, role))
                                  for role in ("inbox", "sent", "trash")}
    observed: dict[str, tuple[str, ...]] = {}
    outside: set[str] = set()
    fetch_failed_by_kind: dict[str, int] = {}
    for ref, role in roles_by_ref.items():
        flags = tuta.flags_of(ref)
        held = tuta.held(ref, index, {})
        if held is not None:
            counts["held"] += 1
            _union(observed, held, tuta_labels(role, flags))
            continue
        item, failure = tuta.fetch(ref, owner_addresses)
        if failure is not None:
            counts["fetch_failed"] += 1
            fetch_failed_by_kind[failure.kind] = fetch_failed_by_kind.get(failure.kind, 0) + 1
            continue
        counts["fetched"] += 1
        if item is None:
            counts["unparseable"] += 1
            continue
        if not tuta.is_listed(ref, item):
            # Renumbered between listing and fetch: the folder is still this
            # one, the listed flags are another message's.
            flags = None
        if item.header.at < floor:
            counts["outside_window"] += 1
            outside.add(item.header.digest)
            continue
        parsed.append(item)
        _union(observed, item.header.digest, tuta_labels(role, flags))
    for digest in sorted(outside - set(observed)):
        remember({"id": _tuta_log_id(digest), "digest": digest, "labels": [], "outside": True})
    for digest, held_labels in sorted(observed.items()):
        remember({"id": _tuta_log_id(digest), "digest": digest, "labels": list(held_labels),
                  "outside": False})
        _union(labels, digest, held_labels)
    counts["fetch_failed_by_kind"] = fetch_failed_by_kind
    counts["folders"] = dict(history.folders)
    counts["depth"] = {}
    for name, facts in tuta.depth.items():
        oldest = facts.get("oldest_at")
        # Walked to the folder's first message without meeting the window's
        # start: the folder is shorter than the window. A stated fact.
        shorter = bool(facts.get("reached_beginning") and facts.get("messages")
                       and (oldest is None or oldest >= floor))
        counts["depth"][name] = {**facts, "shorter_than_window": shorter}
    counts["adapter"] = dict(getattr(tuta, "counts", {}) or {})
    return counts


FETCHED_LOG = "fetched.jsonl"


def _read_fetched(state_dir: Path) -> dict[str, dict[str, Any]]:
    """What earlier runs into this directory fetched: id -> {labels, outside}.
    One unreadable line costs that line (it is fetched again)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        text = (Path(state_dir) / FETCHED_LOG).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("id"):
            out[str(entry["id"])] = entry
    return out


@contextlib.contextmanager
def _fetched_log(state_dir: Path) -> Iterator[Callable[[dict[str, Any]], None]]:
    """Append one line per fetch as it happens: the labels of what was
    fetched, which the index never stores (a label has no timestamp). A later
    run into the same directory skips a fetch only when the index holds the
    mail too — so a run killed BEFORE the backfill's ingest finished re-buys
    those fetches; one killed during the arms re-buys none."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    with (Path(state_dir) / FETCHED_LOG).open("a") as handle:
        def remember(entry: dict[str, Any]) -> None:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()

        yield remember


def recorded_labels(state_dir: Path) -> dict[str, tuple[str, ...]]:
    """digest -> labels, re-unioned from the run's `fetched.jsonl` the way
    `backfill` unions them: over the LAST logged fetch of each provider id
    (`_read_fetched` keeps one per id, as `backfill` reads it), unioned per
    digest, outside-window entries skipped. Labels are never stored in the index (a label has no
    timestamp), so this log is their only record, and an export reads them
    from here rather than from anything the backfill kept in memory."""
    labels: dict[str, set[str]] = {}
    for entry in _read_fetched(state_dir).values():
        digest = str(entry.get("digest") or "")
        if not digest or entry.get("outside"):
            continue
        labels.setdefault(digest, set()).update(str(x) for x in entry.get("labels") or ())
    return {digest: tuple(sorted(held)) for digest, held in labels.items()}


def export_truth(state_dir: Path) -> Callable[[str], tuple[str, str] | None]:
    """The CSV's truth columns, recomputed at export exactly as the harness
    computes them (`truth_of` over the run's own index and recorded labels).
    Truths are never persisted, so there is nothing else to read."""
    index = mailindex.MailIndex(state_dir)
    labels = recorded_labels(state_dir)

    def truth(digest: str) -> tuple[str, str] | None:
        header = index.get(digest)
        if header is None:
            return None
        found = truth_of(index, header, labels.get(digest, ()))
        return found.label, found.tier

    return truth


def export_csv(
    run_dir: Path,
    clean: Callable[[str], str],
    *,
    out_file: Path | None = None,
    now: float | None = None,
) -> mailexport.Written:
    """Write the run's review file — into the Downloads folder by default,
    else into the run directory (`mailexport.export` decides, and says why).
    The name carries the run's own stamp (its directory name). Reads the run
    directory only: no mailbox, no model."""
    state_dir = run_dir / "state"
    return mailexport.export(
        state_dir, clean=clean, run=run_dir.resolve().name or "run", fallback_dir=run_dir,
        out_file=out_file, truth=export_truth(state_dir), now=now)


def select(
    run: Backfill, truths: dict[str, Truth], reader_max: int
) -> list[mailindex.Header]:
    """Which inbound mails the arms read: strong tier first, then weak, then
    near-null, each oldest first, up to `reader_max`. Deterministic, so two
    arms always read the same mails."""
    inbound = [
        h for h in run.index.by_digest.values()
        if h.direction == "in" and truths[h.digest].tier != SET_APART
    ]
    order = {STRONG: 0, WEAK: 1, NEAR_NULL: 2}
    inbound.sort(key=lambda h: (order[truths[h.digest].tier], h.position))
    return inbound[:reader_max]


def run_arms(
    run: Backfill,
    selected: list[mailindex.Header],
    arms: dict[str, Callable[..., Any] | None],
    *,
    charter: roles.Charter,
    state_dir: Path,
    check_admission: bool = True,
    admission_state_dir: Path | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """{arm: {mail digest: record}}. Resumable: a final reading already in the
    run's verdict store is reused, not re-bought.

    `state_dir` is the run's SCRATCH tree (index, cases, verdicts, evidence);
    admission is read from `admission_state_dir`, the machine's real state home
    by default — nothing written into a scratch tree can admit a model."""
    ledger = paths.state_home() if admission_state_dir is None else admission_state_dir
    verdicts = mailverdicts.VerdictStore(state_dir)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for spec, chat in arms.items():
        mine = out.setdefault(spec, {})
        for header in selected:
            if verdicts.has(header.digest, spec, charter.digest):
                held = (verdicts.read(header.digest) or {}).get("by_model", {}).get(spec)
                if isinstance(held, dict):
                    mine[header.digest] = held
                    continue
            try:
                record = mailwatch.assess(
                    run.index, run.store, header, charter=charter, model_spec=spec, chat=chat,
                    state_dir=state_dir, propose=False, check_admission=check_admission,
                    admission_state_dir=ledger,
                )
            except Exception as exc:  # noqa: BLE001 — one mail's failure is its own record
                record = mailverdicts.error_record(header, charter, spec, exc)
            verdicts.write(record)
            mine[header.digest] = record
    return out


# ---------------------------------------------------------------- the report


def _cell(record: dict[str, Any], axis: str) -> str:
    """One report cell — the CSV's `verdict_word`, so every count here is a
    filter over the review file's columns."""
    return mailexport.verdict_word(record, axis)


def _effective_cell(record: dict[str, Any]) -> str:
    """The disposition after the live-conversation clamp."""
    return mailverdicts.effective_disposition(record) or _cell(record, "disposition")


def _add(table: dict[str, dict[str, int]], row: str, col: str) -> None:
    table.setdefault(row, {})
    table[row][col] = table[row].get(col, 0) + 1


def report(
    run: Backfill,
    truths: dict[str, Truth],
    selected: list[mailindex.Header],
    results: dict[str, dict[str, dict[str, Any]]],
    *,
    now: float,
) -> dict[str, Any]:
    """Raw counts. Every table is (truth label) x (verdict value); `cannot_tell`,
    `gated` and `no_verdict` are columns like any other."""
    tiers: dict[str, dict[str, int]] = {}
    for header in run.index.by_digest.values():
        if header.direction == "in":
            truth = truths[header.digest]
            _add(tiers, truth.tier, truth.label)
    def trashed_in(mailbox: str) -> list[mailindex.Header]:
        return [h for h in run.index.by_digest.values()
                if h.direction == "in" and h.mailbox == mailbox
                and "trashed" in truths[h.digest].facts]

    def oldest_days(headers: list[mailindex.Header]) -> float | None:
        return round((now - min(h.at for h in headers)) / 86400, 1) if headers else None

    trashed = trashed_in("gmail")
    oldest_trash_days = oldest_days(trashed)
    sources = {name: dict(facts) for name, facts in run.sources.items()}
    if "tuta" in sources:
        tuta_trashed = trashed_in(mailtuta.NAME)
        sources["tuta"]["trashed_inbound"] = len(tuta_trashed)
        sources["tuta"]["oldest_trashed_days"] = oldest_days(tuta_trashed)
    selected_by_mailbox: dict[str, int] = {}
    for header in selected:
        selected_by_mailbox[header.mailbox] = selected_by_mailbox.get(header.mailbox, 0) + 1
    arms: dict[str, Any] = {}
    for spec, records in results.items():
        disposition: dict[str, dict[str, int]] = {}
        disposition_clamped: dict[str, dict[str, int]] = {}
        wanted_vs_reply: dict[str, dict[str, int]] = {}
        destructive: dict[str, int] = {}
        destructive_clamped: dict[str, int] = {}
        archived_replied = 0
        archived_replied_clamped = 0
        clamped = 0
        gated: dict[str, dict[str, int]] = {}
        endings: dict[str, int] = {}
        for header in selected:
            record = records.get(header.digest)
            if record is None:
                continue
            truth = truths[header.digest]
            ending = str(record.get("ending") or "")
            endings[ending] = endings.get(ending, 0) + 1
            value = _cell(record, "disposition")
            _add(disposition, f"{truth.tier}:{truth.label}", value)
            replied = "replied" if "replied" in truth.facts else "not_replied"
            _add(wanted_vs_reply, f"{truth.tier}:{replied}", _cell(record, "wanted"))
            if value == "expire_after_grace" and truth.label in KEPT:
                destructive[truth.label] = destructive.get(truth.label, 0) + 1
            if value == "archive" and "replied" in truth.facts:
                archived_replied += 1
            # The same cells after code's live-conversation clamp, beside the
            # raw ones, so what the TEACHING achieves stays measurable alone.
            effective = _effective_cell(record)
            clamped += mailverdicts.clamp_of(record) is not None
            _add(disposition_clamped, f"{truth.tier}:{truth.label}", effective)
            if effective == "expire_after_grace" and truth.label in KEPT:
                destructive_clamped[truth.label] = destructive_clamped.get(truth.label, 0) + 1
            if effective == "archive" and "replied" in truth.facts:
                archived_replied_clamped += 1
            if ending == mailreader.GATED:
                by = "+".join(((record.get("gate") or {}).get("newsletter") or {}).get("by") or ())
                _add(gated, by or "?", truth.label)
        arms[spec] = {
            "read": len([d for d in records if d in {h.digest for h in selected}]),
            "endings": endings,
            "destructive_expire_on_kept": destructive,
            "archive_on_replied": archived_replied,
            "disposition_by_truth": disposition,
            "clamped": clamped,
            "destructive_expire_on_kept_clamped": destructive_clamped,
            "archive_on_replied_clamped": archived_replied_clamped,
            "disposition_by_truth_clamped": disposition_clamped,
            "wanted_by_reply": wanted_vs_reply,
            "gated_by_truth": gated,
        }
    disagreements = []
    specs = sorted(results)
    if len(specs) >= 2:
        for header in selected:
            cells = {
                spec: {axis: _cell(results[spec].get(header.digest, {}), axis)
                       for axis in ("disposition", "wanted", "case", "sender_case")}
                for spec in specs if header.digest in results[spec]
            }
            if len(cells) < 2:
                continue
            if len({json.dumps(v, sort_keys=True) for v in cells.values()}) > 1:
                disagreements.append({
                    "mail": header.digest,
                    "provider_id": header.provider_id,
                    "date": time.strftime("%Y-%m-%d", time.gmtime(header.at)),
                    "from": header.from_addr,
                    "subject": header.subject[:120],
                    "truth": truths[header.digest].label,
                    "arms": cells,
                })
    return {
        "window": run.counts,
        "sources": sources,
        "selected_by_mailbox": selected_by_mailbox,
        "truth_tiers": tiers,
        "trash_observability": {
            "trashed_inbound": len(trashed),
            "oldest_trashed_days": oldest_trash_days,
            "retention_days": TRASH_RETENTION_DAYS,
        },
        "selected": len(selected),
        "arms": arms,
        "disagreements": disagreements,
    }


def _pairs(table: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(table.items()))


def _day(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(at))


def _tuta_lines(facts: dict[str, Any], days: int | None) -> list[str]:
    """What the Tuta enumeration observed, stated as facts — what each folder
    held, how far back it went, and what is absent and why it is absent."""
    if facts.get("listing_failed"):
        return [f"the Tuta LISTING failed ({facts.get('listing_failure')}): nothing below "
                "describes Tuta's half of the window"]
    by_folder = facts.get("listed_by_folder") or {}
    folders = facts.get("folders") or {}
    lines = [
        f"tuta: {facts.get('listed', 0)} listed (" + ", ".join(
            f"{role} {by_folder.get(role, 0)}" for role in ("inbox", "sent", "trash")
            if folders.get(role)) + f"), {facts.get('fetched', 0)} fetched, "
        f"{facts.get('held', 0)} already held, {facts.get('fetch_failed', 0)} not fetched, "
        f"{facts.get('unparseable', 0)} unparseable, {facts.get('outside_window', 0)} outside "
        "the window",
    ]
    window = f"the {days}-day window" if days else "the window"
    for role in ("inbox", "sent", "trash"):
        name = folders.get(role)
        depth = (facts.get("depth") or {}).get(name or "")
        if not name or not isinstance(depth, dict):
            continue
        oldest = depth.get("oldest_at")
        if depth.get("capped"):
            lines.append(f"tuta {name}: the listing stopped at {LISTING_LIMIT} messages, before "
                         f"the start of {window}")
        elif not depth.get("messages"):
            lines.append(f"tuta {name}: the folder is empty")
        elif depth.get("shorter_than_window"):
            lines.append(f"tuta {name}: the folder holds {depth.get('messages')} message(s), "
                         f"the oldest dated {_day(oldest) if oldest else '(no readable date)'}"
                         f" — shorter than {window}")
    if folders.get("trash"):
        lines.append(
            f"tuta: trashed inbound observed: {facts.get('trashed_inbound', 0)} (oldest "
            f"{facts.get('oldest_trashed_days')} days). How long Tuta keeps Trash is not known "
            "here; mail it has emptied is ABSENT from every count, not counted as kept.")
    else:
        lines.append("tuta: the bridge exposes no Trash folder — mail he trashed in Tuta is "
                     "ABSENT from every count, not counted as kept.")
    lines.append("tuta: only INBOX, Sent and Trash are enumerated — mail he moved to any other "
                 "folder (an archive, spam, his own folders) is ABSENT, never counted archived.")
    return lines


def _table(title: str, table: dict[str, dict[str, int]]) -> list[str]:
    if not table:
        return [f"  {title}: none recorded"]
    cols = sorted({c for row in table.values() for c in row})
    lines = [f"  {title}", "    " + " | ".join(["truth".ljust(22)] + cols)]
    for row in sorted(table):
        cells = [str(table[row].get(c, 0)) for c in cols]
        lines.append("    " + " | ".join([row.ljust(22)] + cells))
    return lines


def render(result: dict[str, Any]) -> str:
    """The report a person reads. Counts as "N of M" — never a rate, never a
    word that grades the pipeline (`docs/replay.md` R3)."""
    w = result["window"]
    lines = [
        "Inbox assistant backtest — shadow pipeline, raw counts",
        f"window: {w.get('listed', 0)} listed, {w.get('fetched', 0)} fetched, "
        f"{w.get('fetch_failed', 0)} not fetched, {w.get('unparseable', 0)} unparseable, "
        f"{w.get('in_index', 0)} in the index, {w.get('cases', 0)} cases built",
    ]
    sources = result.get("sources") or {}
    several = len(sources) > 1
    if w.get("listing_failed"):
        lines.append("the Gmail LISTING failed: nothing below describes "
                     + ("Gmail's half of the window" if several else "the window"))
    if w.get("quota_stopped"):
        lines.append("the Gmail quota refused fetches past its retries: the backfill stopped "
                     "asking, and the mail it did not fetch is ABSENT below — re-run into the "
                     "same --out to fetch only those")
    if "gmail" in sources or not sources:
        trash = result["trash_observability"]
        lines.append(
            f"{'gmail: ' if several else ''}trashed inbound observed: "
            f"{trash['trashed_inbound']} (oldest {trash['oldest_trashed_days']} days). Gmail "
            f"empties Trash after {trash['retention_days']} days, so mail he trashed earlier "
            "than that is in no listing — it is ABSENT from every count here, not counted as "
            "kept."
        )
    if "tuta" in sources:
        lines += _tuta_lines(sources["tuta"], sources["tuta"].get("window_days"))
    if several:
        lines.append("sources: " + ", ".join(
            f"{name} {facts.get('listed', 0)} listed, {facts.get('fetched', 0)} fetched"
            for name, facts in sorted(sources.items())))
    lines.append("truth, by tier (inbound mail in the window):")
    for tier, labels in sorted(result["truth_tiers"].items()):
        lines.append(f"  {tier}: " + ", ".join(f"{k} {v}" for k, v in sorted(labels.items())))
    by_mailbox = result.get("selected_by_mailbox") or {}
    lines.append(f"read by each arm: {result['selected']} selected mail(s)"
                 + (" (" + _pairs(by_mailbox) + ")" if several and by_mailbox else ""))
    for spec, arm in sorted(result["arms"].items()):
        lines.append("")
        lines.append(f"arm {spec}: {arm['read']} of {result['selected']} recorded")
        lines.append("  endings: " + _pairs(arm["endings"]))
        destructive = arm["destructive_expire_on_kept"]
        total = sum(destructive.values())
        lines.append(
            f"  HEADLINE — would have expired mail he kept: {total} of {arm['read']}"
            + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(destructive.items())) + ")"
               if destructive else "")
        )
        lines.append(f"  would have archived mail he replied to: {arm['archive_on_replied']}")
        if "clamped" in arm:
            kept = arm["destructive_expire_on_kept_clamped"]
            lines.append(
                f"  after the live-conversation clamp ({arm['clamped']} reading(s) clamped): "
                f"would have expired mail he kept: {sum(kept.values())} of {arm['read']}; "
                f"would have archived mail he replied to: {arm['archive_on_replied_clamped']}"
            )
        lines += _table("disposition (rows: tier:truth)", arm["disposition_by_truth"])
        if "disposition_by_truth_clamped" in arm:
            lines += _table("disposition after the clamp (rows: tier:truth)",
                            arm["disposition_by_truth_clamped"])
        lines += _table("wanted (rows: tier:replied?)", arm["wanted_by_reply"])
        lines += _table("newsletter-gated, by list header (rows) x truth", arm["gated_by_truth"])
    shown = result["disagreements"][:DISAGREEMENT_SHOWN]
    lines.append("")
    lines.append(
        f"cross-model disagreements: {len(result['disagreements'])}"
        + (f" (first {len(shown)} below; all in report.json)" if shown else "")
    )
    for item in shown:
        arms = "; ".join(
            f"{spec}: " + "/".join(
                cells[axis] for axis in ("disposition", "wanted", "case", "sender_case")
            )
            for spec, cells in sorted(item["arms"].items())
        )
        lines.append(
            f"  {item['date']} {item['from']} — {item['subject']} [{item['truth']}] {arms}"
        )
    lines.append("")
    lines.append("These are counts of what was recorded. They are not a measure of whether the "
                 "pipeline is fit to act.")
    return "\n".join(lines)


def run_backtest(
    *,
    mailbox: Any,
    tuta: Any = None,
    owner_addresses: frozenset[str],
    arms: dict[str, Callable[..., Any] | None],
    charter: roles.Charter,
    out_dir: Path,
    days: int = DEFAULT_DAYS,
    reader_max: int = DEFAULT_READER_MAX,
    now: float | None = None,
    check_admission: bool = True,
    admission_state_dir: Path | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    state_dir = out_dir / "state"
    run = backfill(mailbox, state_dir, owner_addresses, days=days, now=now, tuta=tuta)
    truths = {
        d: truth_of(run.index, h, run.labels.get(d, ())) for d, h in run.index.by_digest.items()
    }
    selected = select(run, truths, reader_max)
    results = run_arms(run, selected, arms, charter=charter, state_dir=state_dir,
                       check_admission=check_admission,
                       admission_state_dir=admission_state_dir)
    result = report(run, truths, selected, results, now=now)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(result, indent=1, sort_keys=True))
    (out_dir / "report.txt").write_text(render(result) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="backtest the shadow inbox pipeline (#142 W5)")
    parser.add_argument("--account", default=os.environ.get("AISH_MAIL_ACCOUNT", ""))
    parser.add_argument("--owner", action="append", default=[])
    parser.add_argument("--source", action="append", choices=SOURCES, default=[],
                        help="an inbox to backfill (repeatable; default: gmail only — a Tuta "
                             "backfill fetches every Tuta mail in the window too, and its "
                             "inbound mail joins the one --reader-max selection)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--arm", action="append", default=[],
                        help="a Reader model spec; repeat for a cross-model comparison")
    parser.add_argument("--reader-max", type=int, default=DEFAULT_READER_MAX)
    parser.add_argument("--out", default="")
    parser.add_argument("--csv", default="", metavar="RUN_DIR",
                        help="export-only: write the review file from a finished run's store "
                             "and exit — no mailbox, no model. Lands in your Downloads folder "
                             "when run from a terminal, else in RUN_DIR")
    parser.add_argument("--out-file", default="", metavar="PATH",
                        help="with --csv: write the review file here instead (a directory "
                             "gets the generated name inside it)")
    args = parser.parse_args(argv)
    if args.out_file and not args.csv:
        parser.error("--out-file needs --csv")
    if args.csv:
        return _export_only(Path(args.csv), Path(args.out_file) if args.out_file else None)
    wanted = list(dict.fromkeys(args.source or ["gmail"]))
    owners = {a.lower() for a in args.owner}
    gmail_box: Any = None
    if "gmail" in wanted:
        env, why = mailwatch.account_env(args.account)
        if env is None:
            print(f"refusing to run: {why}", file=sys.stderr)
            return 2
        owners.add(args.account.lower())
        gmail_box = mailwatch.GwsMailbox(env)
    tuta_box: Any = None
    if "tuta" in wanted:
        tuta_env, address = mailwatch.tuta_env()
        if address:
            owners.add(address)
        tuta_box = mailtuta.TutaMailbox(tuta_env)
    try:
        charter = mailwatch.load_reader()
    except roles.CharterError as exc:
        print(f"the Reader's charter does not load: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else (
        paths.state_home() / "mail-backtests" / time.strftime("%Y%m%d-%H%M%S")
    )
    result = run_backtest(
        mailbox=gmail_box,
        tuta=tuta_box,
        owner_addresses=frozenset(owners),
        arms={spec: None for spec in args.arm},
        charter=charter,
        out_dir=out,
        days=args.days,
        reader_max=args.reader_max,
    )
    print(render(result))
    print(f"\nwritten to {out}", file=sys.stderr)
    print(f"review file: aish-mailbacktest --csv {out}", file=sys.stderr)
    return 0


def _export_only(run_dir: Path, out_file: Path | None = None) -> int:
    from .cli import _plain  # the terminal's sanitiser: mailview's law, one sanitiser

    if not (run_dir / "state").is_dir():
        print(f"refusing to export: {run_dir} has no state/ — not a backtest run directory",
              file=sys.stderr)
        return 2
    written = export_csv(run_dir, _plain, out_file=out_file)
    print(f"{written.rows} row(s), one per (mail, model); truth recomputed at export")
    if written.unreadable:
        print(f"{written.unreadable} verdict file(s) could not be read and are not in the file")
    if written.note:
        print(written.note)
    print(f"written to {written.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
