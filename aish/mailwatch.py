"""`aish-mailwatch` — the inbox assistant in SHADOW mode (#142 W5, S1 + S1.5).

Every new mail in the owner's personal inboxes — Gmail and, with
`--source tuta`, his primary Tutanota mailbox — flows Gate → Reader → a
recorded verdict, and the verdict acts on NOTHING: no label is changed,
nothing is archived or trashed, nobody is notified. What it produces is the
verdict store (`mailverdicts`), the counters over it, and a health file; the
owner reads all of it with `/mail` in the terminal (`mailview`).

    aish-mailwatch --once --account pawel@wenda.eu --owner pawel@wenda.email \\
        --source gmail --source tuta --model <spec>
    aish-mailwatch --counters        # what the store holds; no mail, no model

**Read-only by construction, not by care.** Every Gmail call goes through
`GwsMailbox`, which refuses any `gws` invocation outside `READ_ONLY_CALLS`
(list and get) before a process is spawned; every Tuta call goes through
`mailtuta.check_argv`, which refuses any himalaya argv outside its allowlist
of subcommands AND flags (so `--seen` is refused). There is no code here that
could modify, label, send or delete.

**Fail closed on credentials.** The Gmail account must have its own credential
directory (`~/.config/gws-accounts/<email>/`); a missing one refuses the run.
A missing Tuta secret fails that source's listing, visibly, and the Gmail
half still runs.

**One pass at a time.** `pass_lock` takes an exclusive `flock` on
`<state>/mail/watch.lock`; a second pass started while one runs (launchd's
interval beside a manual `--once`) exits without touching anything. The lock
dies with its process, so there is no stale lock to clear.

The effectful edges — the gws runner, the himalaya runner, the clock, the
sleeps, the model's chat callable — are parameter seams, so the tests use
fakes and no sleeps. `docs/mail-assistant.md`.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import fcntl
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    atomic_write,
    mailcases,
    mailgate,
    mailindex,
    mailitems,
    mailreader,
    mailsettle,
    mailtuta,
    mailverdicts,
    paths,
    roles,
    vocab,
)
from .mailsource import (
    MISSING,
    PARSE,
    QUOTA,
    TIMEOUT,
    Failure,
    GmailSource,
    Outcome,
    ReadOnlyViolation,
    TokenBucket,
    UnfetchedLedger,
    backoff_s,
    classify_gws,
    should_retry,
)

__all__ = ["ReadOnlyViolation", "GwsMailbox", "run_once", "main"]

READ_ONLY_CALLS = frozenset({
    ("gmail", "users", "messages", "list"),
    ("gmail", "users", "messages", "get"),
})

DEFAULT_DAYS = 2
DEFAULT_MAX = 50
#: Answers an open your-turn row rests on that nobody read, read late per pass
#: (`mailitems.gaps`). A CHOICE: bounded like the judge, oldest row first.
LATE_READS_PER_PASS = 5
#: Tied later mail with no reading, read per pass before the judge may weigh
#: it (`mailitems.unread_candidates`): the caution floor needs a record. A CHOICE.
CANDIDATE_READS_PER_PASS = 5
#: Readings made WITHOUT the mail's body that are re-read per pass, oldest mail
#: first (`blind_readings`): the CRLF read bug (2026-11-02) handed the Reader
#: "[body not recorded]" for bodies the store held all along. A CHOICE; the
#: one-shot backlog is `--reread-blind`.
REREAD_PER_PASS = 10
#: The text a bundle carries for a body the store did not yield
#: (`mailreader._message_entry`).
BODY_NOT_RECORDED = "[body not recorded]"
#: Per model: when a scan last found no blind reading left. Verdict files not
#: rewritten since are not evidence-read again (`blind_readings(since=…)`).
REREAD_MARKER = "reread-marker.json"
GWS_TIMEOUT = 60
SOURCES = ("gmail", "tuta")

Runner = Callable[[list[str], dict[str, str]], "Outcome | dict | list | None"]


def log(msg: str) -> None:
    print(f"[mailwatch] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- the Gmail mailbox


def accounts_root() -> Path:
    return Path(os.environ.get("GWS_ACCOUNTS_ROOT") or Path.home() / ".config" / "gws-accounts")


def account_env(account: str) -> tuple[dict[str, str] | None, str]:
    """The environment for a gws call against `account`, or (None, why).

    The same mapping the owner's gmail tools use: the file keyring backend
    (the Keychain one needs a GUI prompt a headless run can never answer) and
    the account's own config directory. An account with no directory is
    refused — never a fallback to whichever account gws would pick.
    """
    if not account or "@" not in account:
        return None, "no account named (--account <email>)"
    directory = accounts_root() / account
    if not directory.is_dir():
        return None, f"no credential directory for {account} under {accounts_root()}"
    env = dict(os.environ)
    env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(directory)
    return env, ""


def default_runner(args: list[str], env: dict[str, str]) -> Outcome:
    """One `gws` call, its failure TYPED (`mailsource.classify_gws`). Never
    retries: the retry policy is `GwsMailbox._call`'s, and only its."""
    try:
        done = subprocess.run(
            ["gws", *args], capture_output=True, text=True, timeout=GWS_TIMEOUT, env=env
        )
    except FileNotFoundError as exc:
        return Outcome(failure=Failure(MISSING, matched="FileNotFoundError", text=str(exc)))
    except subprocess.TimeoutExpired:
        return Outcome(failure=Failure(TIMEOUT, matched=f"{GWS_TIMEOUT}s"))
    if done.returncode != 0:
        return Outcome(failure=classify_gws(done.returncode, done.stderr, done.stdout))
    try:
        return Outcome(json.loads(done.stdout))
    except json.JSONDecodeError:
        return Outcome(failure=Failure(PARSE, matched="not JSON", text=done.stdout[:200]))


class GwsMailbox:
    """The owner's Gmail through `gws`, read-only, paced and retried.

    Every caller — the watcher and the backtest — inherits the bucket and the
    backoff, because both live in `_call`. After a quota refusal that
    survives its retries, `quota_exhausted` is set and the sources stop
    asking for the rest of the pass (`mailsource.GmailSource.fetch`)."""

    def __init__(
        self,
        env: dict[str, str],
        runner: Runner = default_runner,
        *,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.env = env
        self.runner = runner
        self.sleep = sleep
        self.bucket = bucket if bucket is not None else TokenBucket(sleep=sleep)
        self.last_failure: Failure | None = None
        self.quota_exhausted = False
        self.failures: dict[str, int] = {}
        self.retries = 0

    def _call(self, args: list[str]) -> dict | list | None:
        if tuple(args[:4]) not in READ_ONLY_CALLS:
            raise ReadOnlyViolation(f"refused: gws {' '.join(args[:4])} is not read-only")
        attempt = 0
        while True:
            attempt += 1
            self.bucket.take()
            raw = self.runner(args, self.env)
            outcome = raw if isinstance(raw, Outcome) else Outcome(raw)
            if outcome.failure is None and outcome.value is not None:
                self.last_failure = None
                return outcome.value
            failure = outcome.failure or Failure(PARSE, matched="empty reply")
            self.failures[failure.kind] = self.failures.get(failure.kind, 0) + 1
            if not should_retry(failure, attempt):
                self.last_failure = failure
                if failure.kind == QUOTA:
                    self.quota_exhausted = True
                return None
            self.retries += 1
            self.sleep(backoff_s(failure, attempt))

    def list(self, query: str, limit: int, include_trash: bool = False) -> list[str] | None:
        """Message ids matching `query`, up to `limit`; None if the listing failed."""
        ids: list[str] = []
        token = ""
        while len(ids) < limit:
            params: dict[str, Any] = {
                "userId": "me", "q": query, "maxResults": min(500, limit - len(ids)),
            }
            if include_trash:
                params["includeSpamTrash"] = True
            if token:
                params["pageToken"] = token
            page = self._call(
                ["gmail", "users", "messages", "list", "--params", json.dumps(params)]
            )
            if not isinstance(page, dict):
                return None if not ids else ids
            ids += [str(m["id"]) for m in page.get("messages") or () if m.get("id")]
            token = str(page.get("nextPageToken") or "")
            if not token:
                break
        return ids[:limit]

    def get(self, message_id: str) -> dict | None:
        msg = self._call([
            "gmail", "users", "messages", "get", "--params",
            json.dumps({"userId": "me", "id": message_id, "format": "full"}),
        ])
        return msg if isinstance(msg, dict) else None


def as_source(mailbox: Any) -> Any:
    """A mailbox in the pass's source shape. A bare `list`/`get` object (a
    `GwsMailbox`, the tests' `FakeMailbox`) is Gmail."""
    return mailbox if hasattr(mailbox, "listing") else GmailSource(mailbox)


# ---------------------------------------------------------------- one assessment


def _case_ids_by_key(
    store: mailcases.CaseStore, header: mailindex.Header, keys: set[str]
) -> list[str]:
    return sorted(
        c.id for c in store.cases.values() if keys & c.keys_before(header.position)
    )


def gate_facts(
    index: mailindex.MailIndex, store: mailcases.CaseStore, header: mailindex.Header, body: str
) -> mailreader.Facts:
    """Zone A's deterministic half and the correlator, as of the mail."""
    as_of = header.position
    auth = mailgate.authenticate_header(header)
    return mailreader.Facts(
        auth=auth,
        grade=mailgate.grade(index, header, auth, store.owner_case_parties(as_of, index)),
        flags=mailgate.red_flags(header, store.admitted_domains(as_of)),
        newsletter=mailgate.newsletter(header),
        candidates=mailcases.candidates(store, header, body),
    )


def assess(
    index: mailindex.MailIndex,
    store: mailcases.CaseStore,
    header: mailindex.Header,
    *,
    charter: roles.Charter,
    model_spec: str,
    chat: Callable[..., Any] | None = None,
    state_dir: Any = None,
    observation: mailcases.Observation | None = None,
    propose: bool = True,
    check_admission: bool = True,
    admission_state_dir: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Gate → Reader → the `mail_verdict` record, for one inbound mail."""
    body = index.body(header.digest) or ""
    facts = gate_facts(index, store, header, body)
    if not model_spec and not facts.newsletter.gated:
        reading = mailreader.Reading(
            roles.Status.UNAVAILABLE,
            why="no Reader model named (--model or AISH_MAIL_READER_MODEL)",
        )
    else:
        reading = mailreader.read(
            charter, index, store, header, facts, model_spec=model_spec, chat=chat,
            state_dir=state_dir, check_admission=check_admission,
            admission_state_dir=admission_state_dir,
        )
    verdict = reading.verdict
    code_coordinates = mailcases.extract_coordinates(f"{header.subject}\n{body}")
    # The verdict is in hand here: the one-time-code rule reads its kind too.
    keys = set(mailcases.mail_keys(header.subject, f"{header.subject}\n{body}",
                                   verdict.kind if verdict is not None else ""))
    linked = _case_ids_by_key(store, header, keys)
    if verdict is not None and reading.bundle is not None:
        mailverdicts.count_verdict(verdict)
        chosen = reading.bundle.cases.get(verdict.case)
        if chosen:
            linked.append(chosen)
            case = store.cases.get(chosen)
            domain = mailindex.domain_of(header.from_addr)
            if propose and case is not None and domain not in case.admitted_before(
                header.position
            ):
                # The Reader placed a mail from a sender not admitted on this
                # case: a PROPOSAL, counted on the case, admitting nothing.
                store.propose(chosen, domain)
    coordinates = set(code_coordinates)
    if verdict is not None:
        coordinates |= {mailcases.normalize_coordinate(c.value) for c in verdict.coordinates}
    flags = mailcases.case_flags(store, header, coordinates, linked)
    # The cases the mail is ON — those it joined or opened at ingest, and the
    # key-linked ones — which a keeper clamp holds until they close.
    on = {c.id for c in store.cases.values() if header.position in c.mails} | set(linked)
    return mailverdicts.build_record(
        header=header, facts=facts, reading=reading, charter=charter, model=model_spec,
        observation=observation, flags=flags, code_coordinates=code_coordinates,
        owner_wrote_days=mailreader.owner_wrote_days(index, header), keys=keys,
        mail_cases=on, now=now,
    )


def ingest(
    index: mailindex.MailIndex,
    store: mailcases.CaseStore,
    parsed: Iterable[mailindex.Parsed],
) -> dict[str, mailcases.Observation]:
    """Fold messages into the index and the case store, oldest first. Only a
    message the index did not already hold is observed, so re-running is a
    no-op. Every message is indexed BEFORE any is observed, so a copy of his
    own sent mail is recognised (`mailitems.own_copies`) whichever of the pair
    came first — and a copy mints nothing: it is never observed into a case.
    A copy whose original arrives in a LATER pass was observed when it came;
    the derivation still excludes it from every item."""
    added = [item for item in sorted(parsed, key=lambda p: p.header.position)
             if index.add(item.header, item.body)]
    copies = mailitems.own_copies(index) if added else {}
    observed: dict[str, mailcases.Observation] = {}
    for item in added:
        header = index.get(item.header.digest) or item.header
        if header.digest in copies:
            continue
        auth = mailgate.authenticate_header(header)
        observed[header.digest] = store.observe(header, item.body, auth.authenticated, index)
    return observed


# ---------------------------------------------------------------- one pass


@dataclass
class SourcePass:
    """What one pass did with one inbox."""

    listing_ok: bool = True
    failure: str = ""
    listed: int = 0
    topped_up: int = 0  # ledger entries tried this pass
    held: int = 0  # not fetched: the index already holds it
    fetched: int = 0
    fetch_failed: dict[str, int] = field(default_factory=dict)
    unparseable: int = 0
    at_from_sender: int = 0  # `at` came from the Date header, not the provider
    unfetched: int = 0  # still in the ledger after this pass
    abandoned: int = 0
    note: str = ""
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class Pass:
    """What one `--once` pass did — counted, printed, and kept as health."""

    listed: int = 0
    fetched: int = 0
    fetch_failed: int = 0
    unparseable: int = 0
    new_in_index: int = 0
    assessed: int = 0
    already_recorded: int = 0
    endings: dict[str, int] = field(default_factory=dict)
    listing_ok: bool = True
    sources: dict[str, SourcePass] = field(default_factory=dict)
    own_copies: int = 0  # his own sent mail in his other inbox: not read
    reread: int = 0  # readings made without the body, superseded and read again
    late_read: int = 0  # answers an open follow-up rested on, read late
    settlement: dict[str, Any] = field(default_factory=dict)  # `mailsettle.SettlePass`

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def health_path(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / "watch_health.json"


def record_health(state_dir: os.PathLike | str, result: Pass, now: float) -> dict[str, Any]:
    """Consecutive failed listings — for the pass and per inbox — the last good
    pass and the last pass's counts. Counted only; S1 pushes nothing. The owner
    reads it with `/mail`, where a streak or a stale pass is shown as a
    warning (`mailview.health_lines`)."""
    path = health_path(state_dir)
    try:
        prior = json.loads(path.read_text())
        if not isinstance(prior, dict):
            raise TypeError("not a mapping")
    except (OSError, ValueError, TypeError):
        prior = {}
    streak = int(prior.get("failed_listings") or 0)
    last_ok = prior.get("last_ok_at")
    if result.listing_ok:
        streak, last_ok = 0, now
    else:
        streak += 1
    sources = dict(prior.get("sources") or {}) if isinstance(prior.get("sources"), dict) else {}
    for name, sp in result.sources.items():
        held = sources.get(name)
        before: dict[str, Any] = held if isinstance(held, dict) else {}
        entry = {
            "failed_listings": 0 if sp.listing_ok else int(before.get("failed_listings") or 0) + 1,
            "last_ok_at": now if sp.listing_ok else before.get("last_ok_at"),
            "last_failure": sp.failure or (before.get("last_failure") if not sp.listing_ok
                                           else ""),
            "note": sp.note,
            "unfetched": sp.unfetched,
            "unparseable": sp.unparseable,
            # A source not read on a pass keeps its old entry with an older
            # `last_run_at`, which `/mail` shows as not current.
            "last_run_at": now,
        }
        sources[name] = entry
    state = {"failed_listings": streak, "last_ok_at": last_ok, "last_pass_at": now,
             "last_pass": result.as_json(), "sources": sources}
    atomic_write.publish(path, json.dumps(state, sort_keys=True))
    return state


def flush_counters(state_dir: os.PathLike | str, result: Pass, now: float) -> None:
    """The pass's vocabulary tallies, in the `vocab` record shape, appended to
    a dated run log `vocab.scan` reads beside the session logs."""
    lists = vocab.drain()
    day = datetime.datetime.fromtimestamp(now).strftime("%Y%m%d")
    path = mailindex.mail_dir(state_dir) / f"runs-{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"at": now, "step": {"kind": "mail_run", **result.as_json()}}]
    if lists:
        lines.append({"at": now, "step": {"kind": "vocab", "lists": lists}})
    with path.open("a") as handle:
        for line in lines:
            handle.write(json.dumps(line, sort_keys=True) + "\n")


def _collect(
    source: Any,
    sp: SourcePass,
    *,
    index: mailindex.MailIndex,
    by_pid: dict[str, str],
    ledger: UnfetchedLedger | None,
    owners: frozenset[str],
    days: int,
    limit: int,
    now: float,
    keep: Callable[[mailindex.Parsed], None] | None = None,
) -> tuple[list[mailindex.Parsed], list[str]]:
    """List one source, fetch what the index does not already hold, and say
    which digests are INBOX mail. Returns (parsed, inbox digests).

    `ledger` None (the backfill) neither tops up from nor records into the
    watcher's unfetched ledger: a mail the ledger names is assessed when the
    watcher next fetches it, and the backfill must buy no Reader call.
    `keep` sees each parsed mail as soon as it is fetched (the backfill adds
    it to the index there, so an interrupted run keeps what it fetched)."""
    parsed: list[mailindex.Parsed] = []
    inbox_digests: list[str] = []
    listing = source.listing(days, limit)
    if listing is None:
        sp.listing_ok = False
        failure = getattr(source, "last_failure", None)
        sp.failure = failure.describe() if isinstance(failure, Failure) else "listing failed"
        return parsed, inbox_digests
    roles_by_ref: dict[str, str] = {}
    for ref in listing.sent:
        roles_by_ref.setdefault(ref, "sent")
    for ref in listing.inbox:
        roles_by_ref[ref] = "inbox"
    sp.listed = len(roles_by_ref)
    if source.stable_ids and ledger is not None:
        # The top-up: listed earlier, never fetched. Gmail only — on Tuta a
        # remembered UID names a different message after a bridge restart.
        for ref, role in ledger.pending(source.name)[:limit]:
            if ref not in roles_by_ref:
                roles_by_ref[ref] = role
                sp.topped_up += 1
    for ref, role in roles_by_ref.items():
        held = source.held(ref, index, by_pid)
        if held is not None:
            sp.held += 1
            if role == "inbox":
                inbox_digests.append(held)
            if source.stable_ids and ledger is not None:
                ledger.done(source.name, ref)
            continue
        item, failure = source.fetch(ref, owners)
        if failure is not None:
            sp.fetch_failed[failure.kind] = sp.fetch_failed.get(failure.kind, 0) + 1
            if source.stable_ids and ledger is not None:
                ledger.missed(source.name, ref, role, failure, now)
            continue
        if source.stable_ids and ledger is not None:
            ledger.done(source.name, ref)
        sp.fetched += 1
        if item is None:
            sp.unparseable += 1
            continue
        if item.header.at_source != "provider":
            sp.at_from_sender += 1
        parsed.append(item)
        if keep is not None:
            keep(item)
        if role == "inbox":
            inbox_digests.append(item.header.digest)
    if ledger is not None:
        sp.unfetched = ledger.count(source.name)
        sp.abandoned = ledger.abandoned.get(source.name, 0)
    sp.counts = dict(getattr(source, "counts", {}) or {})
    return parsed, inbox_digests


def run_once(
    *,
    mailbox: Any = None,
    mailboxes: list[Any] | None = None,
    state_dir: os.PathLike | str,
    owner_addresses: frozenset[str],
    model_spec: str,
    charter: roles.Charter,
    chat: Callable[..., Any] | None = None,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_MAX,
    now: Callable[[], float] = time.time,
    settler: roles.Charter | None = None,
    settle_chat: Callable[..., Any] | None = None,
) -> Pass:
    """One shadow pass over every source: list, fetch what is new, index,
    assess the new inbound, record. One state tree for all of them — the
    ledger must see every act of his, whichever inbox it is in — and digest
    dedup absorbs a mail delivered to both.

    Then, when a settlement charter is given (`settle`): the answers open
    follow-ups rest on that nobody read are read (bounded), and the settlement
    judge weighs the later mail tied to open items (`mailsettle.settle_pass`)."""
    sources = [as_source(m) for m in (mailboxes if mailboxes is not None else [mailbox])]
    result = Pass()
    index = mailindex.MailIndex(state_dir)
    store = mailcases.CaseStore(state_dir)
    verdicts = mailverdicts.VerdictStore(state_dir)
    ledger = UnfetchedLedger(state_dir)
    # Gmail's provider ids are stable, so an id the index holds is that mail.
    by_pid = {h.provider_id: d for d, h in index.by_digest.items()
              if h.mailbox == "gmail" and h.provider_id}
    parsed: list[mailindex.Parsed] = []
    inbox_digests: list[str] = []
    for source in sources:
        sp = result.sources.setdefault(source.name, SourcePass())
        got, inbox = _collect(source, sp, index=index, by_pid=by_pid, ledger=ledger,
                              owners=owner_addresses, days=days, limit=limit, now=now())
        sp.note = source.note()
        parsed += got
        inbox_digests += inbox
        result.listing_ok = result.listing_ok and sp.listing_ok
        result.listed += sp.listed
        result.fetched += sp.fetched
        result.fetch_failed += sum(sp.fetch_failed.values())
        result.unparseable += sp.unparseable
    ledger.save()
    observed = ingest(index, store, parsed)
    result.new_in_index = len(observed)
    inbox_headers = [index.get(d) for d in dict.fromkeys(inbox_digests)]
    copies = mailitems.own_copies(index)
    for header in sorted((h for h in inbox_headers if h is not None), key=lambda h: h.position):
        if header.direction != "in":
            continue
        if header.digest in copies:
            # His own sent mail, as delivered to his other inbox: the act is
            # the Gmail original's, and there is nothing to read.
            result.own_copies += 1
            continue
        if verdicts.has(header.digest, model_spec, charter.digest):
            result.already_recorded += 1
            continue
        try:
            record = assess(
                index, store, header, charter=charter, model_spec=model_spec, chat=chat,
                state_dir=state_dir, observation=observed.get(header.digest), now=now(),
            )
        except Exception as exc:  # noqa: BLE001 — one mail's failure is its own record
            record = mailverdicts.error_record(header, charter, model_spec, exc, now())
        verdicts.write(record)
        result.assessed += 1
        result.endings[record["ending"]] = result.endings.get(record["ending"], 0) + 1
    if model_spec:
        result.reread = reread_blind(state_dir, index=index, store=store, charter=charter,
                                     model_spec=model_spec, chat=chat, now=now(),
                                     limit=REREAD_PER_PASS)
    if settler is not None:
        settle(state_dir, index=index, store=store, charter=charter, settler=settler,
               model_spec=model_spec, chat=chat, settle_chat=settle_chat, now=now(),
               result=result)
    return result


def _reread_marker(state_dir: os.PathLike | str) -> dict[str, Any]:
    try:
        raw = json.loads((mailindex.mail_dir(state_dir) / REREAD_MARKER).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _mark_clean(state_dir: os.PathLike | str, model_spec: str, at: float) -> None:
    marker = _reread_marker(state_dir)
    marker[model_spec] = {"none_found_at": at}
    atomic_write.publish(mailindex.mail_dir(state_dir) / REREAD_MARKER,
                         json.dumps(marker, sort_keys=True))


def blind_readings(
    state_dir: os.PathLike | str, index: mailindex.MailIndex, model_spec: str,
    *, since: float | None = None,
) -> list[mailindex.Header]:
    """The mails whose final reading by `model_spec` was made WITHOUT their
    body although the evidence store yields it now — oldest mail first.

    Decided from evidence, never guessed: the reading's recorded input (its
    first call's bundle, in the evidence store by digest) carried exactly
    `BODY_NOT_RECORDED` as the reviewed message's text, AND `index.body` now
    returns the body. A body genuinely absent from the store is never a
    candidate, so it is never retried; a bundle no longer in the store cannot
    be checked, and its reading stands."""
    from . import evidence

    out: list[mailindex.Header] = []
    for path in sorted(mailverdicts.store_dir(state_dir).glob("*/*.json")):
        # Files not rewritten since the last clean scan were all checked then.
        try:
            if since is not None and path.stat().st_mtime <= since:
                continue
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        record = ((raw.get("by_model") or {}).get(model_spec)
                  if isinstance(raw, dict) else None)
        if not isinstance(record, dict):
            continue
        if record.get("model") != model_spec or record.get("ending") not in (
                mailverdicts.FINAL_ENDINGS - {mailreader.GATED}):
            continue
        header = index.get(str(record.get("mail") or ""))
        calls = record.get("calls") if isinstance(record.get("calls"), list) else []
        if header is None or not calls or not isinstance(calls[0], dict):
            continue
        digest = str((calls[0].get("input") or {}).get("digest") or "")
        text = evidence.get(digest, state_dir) if digest else None
        try:
            bundle = json.loads(text) if text else None
        except ValueError:
            bundle = None
        if not isinstance(bundle, dict):
            continue
        reviewed = next((m for m in bundle.get("messages") or ()
                         if isinstance(m, dict) and m.get("ref") == bundle.get("mail")), None)
        if reviewed is None or reviewed.get("text") != BODY_NOT_RECORDED:
            continue
        if index.body(header.digest) is None:
            continue
        out.append(header)
    return sorted(out, key=lambda h: h.position)


def reread_blind(
    state_dir: os.PathLike | str,
    *,
    index: mailindex.MailIndex,
    store: mailcases.CaseStore,
    charter: roles.Charter,
    model_spec: str,
    now: float,
    limit: int,
    chat: Callable[..., Any] | None = None,
    full: bool = False,
) -> int:
    """Re-read up to `limit` blind readings (`blind_readings`), in arrival
    order. The old record is never deleted: the new one carries it whole under
    `supersedes`, with code's sentence for why. Returns how many were read."""
    verdicts = mailverdicts.VerdictStore(state_dir)
    since = None if full else (_reread_marker(state_dir).get(model_spec) or {}).get(
        "none_found_at")
    found = blind_readings(state_dir, index, model_spec,
                           since=float(since) if since is not None else None)
    done = 0
    for header in found[:limit]:
        old = (verdicts.read(header.digest) or {}).get("by_model", {}).get(model_spec)
        try:
            record = assess(index, store, header, charter=charter, model_spec=model_spec,
                            chat=chat, state_dir=state_dir, propose=False, now=now)
        except Exception as exc:  # noqa: BLE001 — one mail's failure is its own record
            record = mailverdicts.error_record(header, charter, model_spec, exc, now)
        if record.get("ending") not in mailverdicts.FINAL_ENDINGS:
            continue  # no model, no admission: the old reading stands until one answers
        record["supersedes"] = {
            "at": now, "record": old,
            "why": "its reading's input carried [body not recorded] for this mail, and the "
                   "evidence store now yields the body",
        }
        verdicts.write(record)
        done += 1
    if done == len(found):
        # Nothing blind is left as of `now`: later passes check only what is
        # written after it. A body restored to the store later for an older
        # reading is the one-shot `--reread-blind`'s (it scans everything).
        _mark_clean(state_dir, model_spec, now)
    return done


def settle(
    state_dir: os.PathLike | str,
    *,
    charter: roles.Charter,
    settler: roles.Charter,
    model_spec: str,
    now: float,
    result: Pass,
    index: mailindex.MailIndex | None = None,
    store: mailcases.CaseStore | None = None,
    chat: Callable[..., Any] | None = None,
    settle_chat: Callable[..., Any] | None = None,
    since: float | None = None,
    late_reads: int = LATE_READS_PER_PASS,
) -> None:
    """The pass's settlement step. First the GAPS: an open your-turn row that
    is "your turn" only because nobody read the answer gets that answer read
    by the Reader, marked `late_read` on its record so the report names a row
    the reading closes. Then the judge, over the pairs that are ready."""
    index = index if index is not None else mailindex.MailIndex(state_dir)
    store = store if store is not None else mailcases.CaseStore(state_dir)
    verdicts = mailverdicts.VerdictStore(state_dir)
    if model_spec:
        ctx = mailitems.Context(state_dir, now=now, index=index, store=store)
        unread = mailitems.unread_candidates(ctx, mailsettle.showable(ctx, since),
                                             CANDIDATE_READS_PER_PASS)
        for digest in [*mailitems.gaps(ctx, late_reads), *unread]:
            header = index.get(digest)
            if header is None or verdicts.has(digest, model_spec, charter.digest):
                continue
            try:
                record = assess(index, store, header, charter=charter, model_spec=model_spec,
                                chat=chat, state_dir=state_dir, propose=False, now=now)
            except Exception as exc:  # noqa: BLE001 — one mail's failure is its own record
                record = mailverdicts.error_record(header, charter, model_spec, exc, now)
            record[mailitems.LATE_READ] = (
                "a later mail tied to an open item had no reading" if digest in unread
                else "an open follow-up rested on it and it had no reading")
            verdicts.write(record)
            result.late_read += 1
    settled = mailsettle.settle_pass(state_dir, charter=settler, model_spec=model_spec,
                                     now=now, chat=settle_chat, index=index, store=store,
                                     since=since)
    result.settlement = settled.as_json()


# ---------------------------------------------------------------- the backfill

# Per folder and listing: the backfill reaches further than a pass's `--max`.
BACKFILL_LIMIT = 5000


@dataclass
class Backfill:
    """What one `--backfill` run did — counted and printed, never health."""

    days: int = 0
    listing_ok: bool = True
    sources: dict[str, SourcePass] = field(default_factory=dict)
    indexed: int = 0  # new to the index this run
    observed: int = 0  # index mail in the window folded through the case store
    cases_before: int = 0
    cases_after: int = 0
    admitted: int = 0

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def backfill(
    *,
    mailboxes: list[Any],
    state_dir: os.PathLike | str,
    owner_addresses: frozenset[str],
    days: int,
    limit: int = BACKFILL_LIMIT,
    now: Callable[[], float] = time.time,
) -> Backfill:
    """A one-time deterministic pass over INBOX and Sent of every source for
    `days`: the thread index, case observation, and through them the facts
    the ledger and the report's Follow-ups read. **No Reader**: this takes no
    model and no chat seam, so it cannot call one, and it records no verdict.

    Paced by each adapter (Gmail's token bucket and retry policy live in
    `GwsMailbox._call`; one bridge connection per Tuta call). Idempotent and
    resumable: a mail the index holds is never fetched again (Gmail by
    provider id, Tuta by Message-ID AND sender AND subject), each fetched mail
    is in the index as soon as it is fetched, and the case observation then
    runs over EVERY index mail in the window in position order — idempotent
    per mail (`CaseStore.observe`), so a run interrupted before it is simply
    finished by the next. A Gmail quota exhaustion stops that source asking
    (the adapter's flag); a re-run fetches the rest. The watcher's unfetched
    ledger is neither read nor written."""
    sources = [as_source(m) for m in mailboxes]
    result = Backfill(days=days)
    index = mailindex.MailIndex(state_dir)
    by_pid = {h.provider_id: d for d, h in index.by_digest.items()
              if h.mailbox == "gmail" and h.provider_id}

    def keep(item: mailindex.Parsed) -> None:
        if index.add(item.header, item.body):
            result.indexed += 1

    for source in sources:
        sp = result.sources.setdefault(source.name, SourcePass())
        _collect(source, sp, index=index, by_pid=by_pid, ledger=None, owners=owner_addresses,
                 days=days, limit=limit, now=now(), keep=keep)
        sp.note = source.note()
        result.listing_ok = result.listing_ok and sp.listing_ok
    # Observed from a fresh load, so the index's links are the ones every
    # later reader rebuilds (`MailIndex._load`), oldest first.
    index = mailindex.MailIndex(state_dir)
    store = mailcases.CaseStore(state_dir)
    result.cases_before = len(store.cases)
    result.observed, admitted, _joined = observe_unplaced(
        state_dir, index, store, floor=now() - days * 86400)
    result.admitted = len(admitted)
    result.cases_after = len(store.cases)
    return result


def observe_unplaced(
    state_dir: os.PathLike | str,
    index: mailindex.MailIndex,
    store: mailcases.CaseStore,
    *,
    floor: float | None = None,
) -> tuple[int, list[str], int]:
    """Fold every index mail that is on NO case through `CaseStore.observe`,
    oldest first — the one observation loop the backfill and the
    re-observation share. (observed, the admissions minted as "case:domain",
    mails that joined an existing case).

    Mail already on a case was observed, and mail he MOVED is where he put it:
    re-observing either would append it again to the case its key names —
    undoing his move, and adding its amounts and accounts twice. A copy of his
    own sent mail mints nothing. Every law of `observe` applies: only his own
    or authenticated mail records a fact; admission only by his act toward a
    sender."""
    placed = {p[1] for c in (*store.cases.values(), *store.merged.values()) for p in c.mails}
    placed |= {str(a.get("mail")) for a in mailcases.read_acts(state_dir)
               if a.get("verb") == mailcases.MOVE}
    placed |= set(mailitems.own_copies(index))
    verdicts = mailverdicts.VerdictStore(state_dir)
    observed = joined = 0
    admitted: list[str] = []
    for header in sorted(index.by_digest.values(), key=lambda h: h.position):
        if (floor is not None and header.at < floor) or header.digest in placed:
            continue
        body = index.body(header.digest) or ""
        auth = mailgate.authenticate_header(header)
        observation = store.observe(header, body, auth.authenticated, index,
                                    recorded_kind(verdicts, header.digest))
        observed += 1
        admitted.extend(observation.admitted)
        if observation.cases and not observation.created:
            joined += 1
    return observed, admitted, joined


@dataclass
class Reobserve:
    """What one `--reobserve` run did — counted and printed, never health."""

    observed: int = 0  # index mail on no case, folded through the case store
    joined: int = 0  # of those, mails that joined an EXISTING case
    opened: int = 0  # cases this run opened
    # Senders admitted by HIS act addressed to them (`_admit_toward`) — the
    # law the live path and the backfill run, so the fold grants exactly what
    # watching live would have; each as "case:domain".
    admitted: list[str] = field(default_factory=list)
    cases_before: int = 0
    cases_after: int = 0
    new_multi_mail_cases: list[dict[str, Any]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def reobserve(state_dir: os.PathLike | str) -> Reobserve:
    """Re-observe the backlog under TODAY's laws (key stems, alternations, the
    one-time-code rule, trusted facts): every indexed mail on no case is folded
    through `observe_unplaced`, the backfill's own loop — no mailbox is read, no
    Reader is called, and a sender is admitted only by the live law (his act
    addressed to its domain, `CaseStore._admit_toward`).
    Idempotent: a mail placed on a case is never observed again, so a second run
    joins and opens nothing. Reports the cases it opened that hold more than one
    mail."""
    result = Reobserve()
    index = mailindex.MailIndex(state_dir)
    store = mailcases.CaseStore(state_dir)
    before = set(store.cases)
    result.cases_before = len(before)
    result.observed, result.admitted, result.joined = observe_unplaced(state_dir, index, store)
    result.cases_after = len(store.cases)
    result.opened = result.cases_after - result.cases_before
    for case_id in sorted(set(store.cases) - before):
        case = store.cases[case_id]
        if len(case.mails) > 1:
            result.new_multi_mail_cases.append(
                {"case": case_id, "title": case.display_title(), "mails": len(case.mails)})
    return result


@dataclass
class Recheck:
    """What one `--recheck-tuta-auth` run did — counted and printed, never health."""

    needing: int = 0  # Tuta index mail with no bridge-header observation, at the start
    listing_ok: bool = True
    failure: str = ""
    listed: int = 0
    fetched: int = 0
    rechecked: int = 0  # matched by digest; observation recorded onto the stored entry
    now_authenticated: int = 0
    still_not: int = 0
    still_not_why: dict[str, int] = field(default_factory=dict)
    failed: int = 0  # fetch failed; the entry is untouched and a re-run tries it again
    failed_kinds: dict[str, int] = field(default_factory=dict)
    unmatched: int = 0  # no fetched message had its digest; the entry is untouched
    deferred: int = 0  # matched, but a fetch failed this run: nothing applied, nothing recorded
    fetched_not_matching: int = 0  # envelope matched, the fetched digest named no entry
    unparseable: int = 0
    gained_facts: int = 0  # (mail, case) pairs given the authenticated sighting it lacked
    observed: int = 0  # mail folded through `CaseStore.observe` by this run
    admitted: int = 0
    cases_before: int = 0
    cases_after: int = 0
    depth: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def recheck_tuta_auth(
    *,
    mailbox: Any,
    state_dir: os.PathLike | str,
    owner_addresses: frozenset[str],
    limit: int = BACKFILL_LIMIT,
    now: Callable[[], float] = time.time,
) -> Recheck:
    """The one-time recheck of Tuta mail indexed before aish read the bridge's
    header: re-fetch each such mail through the fenced read path (never
    `--seen`), record the header observation onto the EXISTING entry, and let
    the deterministic consequences of the now-known authentication follow, in
    arrival order. **No Reader**: no model and no chat seam are taken, and no
    verdict is recorded or recomputed.

    **Identity is the digest, unchanged.** The bridge's line is in the header
    block, and none of a Tuta digest's inputs (`mailindex.digest_for`) is, so
    the re-fetched message re-parses to the digest it was indexed under. That
    equality is the match — the envelope's Message-ID, sender and subject only
    choose what to fetch — and an entry no fetched message re-parses to is
    counted `unmatched` and left untouched. Nothing is ever added to the index.

    **The consequences, as of each mail** (index order, oldest first):
    - a newly authenticated INBOUND mail on no case is observed as the
      watcher would have (`CaseStore.observe`): it may open a case or join one;
    - on every case it is on, it gains the facts observation records only for
      authenticated mail, at its own position (`record_authenticated`);
    - a LATER owner mail on a case a newly authenticated mail is on re-asks
      admission as of itself (`admit_toward`) — his reply toward that sender
      now admits it;
    - a LATER mail on no case that carries a key of an earlier newly
      authenticated mail now on a case is observed, and joins it.
    Grades and the report read the stores and the header at read time, so
    nothing else is written. Header observations are recorded LAST and the
    later steps are derived from where each target mail IS, so a run
    interrupted before then is redone whole by the next. A failed fetch
    defers the whole run (`deferred`). A mail already rechecked is never a
    target again; once every target is, a second run lists nothing."""
    result = Recheck()
    index = mailindex.MailIndex(state_dir)
    targets = {d: h for d, h in index.by_digest.items()
               if h.mailbox == mailtuta.NAME and not h.gateway_auth_observed}
    result.needing = len(targets)
    store = mailcases.CaseStore(state_dir)
    result.cases_before = result.cases_after = len(store.cases)
    if not targets:
        return result
    # Back to the oldest target, a day of slack for the envelope's own date.
    floor = min(h.at for h in targets.values())
    days = max(1, int((now() - floor) // 86400) + 2)
    listing = mailbox.listing(days, limit)
    result.depth = dict(getattr(mailbox, "depth", {}) or {})
    if listing is None:
        result.listing_ok = False
        failure = getattr(mailbox, "last_failure", None)
        result.failure = failure.describe() if isinstance(failure, Failure) else "listing failed"
        result.unmatched = len(targets)
        return result
    wanted: dict[tuple[str, str, str], list[str]] = {}
    for digest, header in targets.items():
        wanted.setdefault(mailtuta.identity_of(header), []).append(digest)
    found: dict[str, mailindex.Parsed] = {}
    failed: set[str] = set()
    refs = [(r, "in") for r in listing.inbox] + [(r, "out") for r in listing.sent]
    result.listed = len(refs)
    for ref, direction in refs:
        candidates = [d for d in wanted.get(mailbox.envelope_identity(ref), ())
                      if d not in found and targets[d].direction == direction]
        if not candidates:
            continue
        parsed, fetch_failure = mailbox.fetch(ref, owner_addresses)
        if fetch_failure is not None:
            result.failed_kinds[fetch_failure.kind] = (
                result.failed_kinds.get(fetch_failure.kind, 0) + 1)
            failed.update(candidates)
            continue
        result.fetched += 1
        if parsed is None:
            result.unparseable += 1
            continue
        digest = parsed.header.digest
        if digest not in targets or digest in found or targets[digest].direction != direction:
            result.fetched_not_matching += 1
            continue
        found[digest] = parsed
    result.failed = len(failed - set(found))
    result.unmatched = len(set(targets) - set(found) - failed)
    if result.failed:
        # Consequences follow in ARRIVAL order across runs too: applying a
        # later mail's now while an earlier one's fetch failed would let the
        # earlier one, next run, meet cases the later one opened. Nothing is
        # applied and nothing recorded; the next run redoes the whole set.
        result.deferred = len(found)
        return result
    _follow_authentication(index, store, found, result, state_dir)
    for digest, parsed in found.items():
        if index.record_gateway_auth(digest, parsed.header.gateway_auth,
                                     parsed.header.gateway_auth_observed):
            result.rechecked += 1
    result.cases_after = len(store.cases)
    return result


def recorded_kind(verdicts: mailverdicts.VerdictStore, digest: str) -> str:
    """The Reader's `kind` on the newest recorded verdict of this mail, or ""."""
    held = (verdicts.read(digest) or {}).get("by_model") or {}
    records = [r for r in held.values()
               if isinstance(r, dict) and isinstance(r.get("verdict"), dict)]
    newest = max(records, key=lambda r: float(r.get("recorded_at") or 0), default=None)
    return str((newest or {}).get("verdict", {}).get("kind") or "")


def _follow_authentication(
    index: mailindex.MailIndex,
    store: mailcases.CaseStore,
    found: dict[str, mailindex.Parsed],
    result: Recheck,
    state_dir: os.PathLike | str,
) -> None:
    """`recheck_tuta_auth`'s consequences, one mail at a time in position
    order, every fact this run writes stamped with the mail's own position.
    `CaseStore.observe` matches keys against the whole store, so an older mail
    can join a case a later mail opened (the backfill's stated limit); every
    other step here asks only about positions before the mail."""
    verdicts = mailverdicts.VerdictStore(state_dir)

    def kind_of(digest: str) -> str:
        return recorded_kind(verdicts, digest)

    authenticated: dict[str, mailindex.Header] = {}
    for digest, parsed in found.items():
        held = index.get(digest)
        assert held is not None  # found keys are index digests
        header = dataclasses.replace(
            held, gateway_auth=parsed.header.gateway_auth,
            gateway_auth_observed=parsed.header.gateway_auth_observed)
        auth = mailgate.authenticate_header(header)
        if auth.authenticated:
            result.now_authenticated += 1
            if header.direction == "in":
                authenticated[digest] = header
        else:
            result.still_not += 1
            result.still_not_why[auth.why] = result.still_not_why.get(auth.why, 0) + 1
    if not authenticated:
        return
    earliest = min(h.position for h in authenticated.values())
    moved = {str(a.get("mail")) for a in mailcases.read_acts(state_dir)
             if a.get("verb") == mailcases.MOVE}
    # A copy of his own sent mail mints nothing, authenticated or not.
    moved |= set(mailitems.own_copies(index))
    changed: set[str] = set()  # cases a newly authenticated mail is on
    run_keys: set[str] = set()  # keys of newly authenticated mail on a case, earlier

    def cases_on(position: tuple[float, str]) -> list[mailcases.Case]:
        return [c for c in store.cases.values() if position in c.mails]

    for header in sorted(index.by_digest.values(), key=lambda h: h.position):
        if header.position < earliest:
            continue
        on = cases_on(header.position)
        if header.digest in authenticated:
            header = authenticated[header.digest]
            body = found[header.digest].body
            if not on and header.digest not in moved:
                observation = store.observe(header, body, True, index,
                                            kind_of(header.digest))
                result.observed += 1
                result.admitted += len(observation.admitted)
                changed.update(observation.cases)
                on = cases_on(header.position)
            # Derived from where the mail IS, never from what this run changed:
            # a target stays a target until its header is recorded (last), so a
            # run interrupted after its case writes rebuilds both on the redo.
            for case in on:
                if store.record_authenticated(case, header, body, kind_of(header.digest)):
                    result.gained_facts += 1
                changed.add(case.id)
            if on:
                # Later mail on no case carrying one of its keys would have
                # joined, had this mail arrived authenticated.
                run_keys.update(mailcases.mail_keys(header.subject,
                                                    f"{header.subject}\n{body}",
                                                    kind_of(header.digest)))
            continue
        if on:
            if header.direction == "out":
                for case in on:
                    if case.id in changed:
                        result.admitted += len(store.admit_toward(case, header))
            continue
        if header.digest in moved or not run_keys:
            continue
        body = index.body(header.digest) or ""
        keys = mailcases.mail_keys(header.subject, f"{header.subject}\n{body}",
                                   kind_of(header.digest))
        # Every `run_keys` entry was put there by a mail EARLIER in this
        # position-ordered loop, so a key in it is a key on a case before this mail.
        if any(k in run_keys for k in keys):
            auth = mailgate.authenticate_header(header)
            observation = store.observe(header, body, auth.authenticated, index,
                                        kind_of(header.digest))
            result.observed += 1
            result.admitted += len(observation.admitted)
            changed.update(observation.cases)


def flush_run(
    state_dir: os.PathLike | str, kind: str, record: dict[str, Any], now: float
) -> None:
    """One run and its vocabulary tallies, in the dated run log."""
    lists = vocab.drain()
    day = datetime.datetime.fromtimestamp(now).strftime("%Y%m%d")
    path = mailindex.mail_dir(state_dir) / f"runs-{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"at": now, "step": {"kind": kind, **record}}]
    if lists:
        lines.append({"at": now, "step": {"kind": "vocab", "lists": lists}})
    with path.open("a") as handle:
        for line in lines:
            handle.write(json.dumps(line, sort_keys=True) + "\n")


def flush_backfill(state_dir: os.PathLike | str, result: Backfill, now: float) -> None:
    """The run and its vocabulary tallies, in the dated run log."""
    flush_run(state_dir, "mail_backfill", result.as_json(), now)


@contextlib.contextmanager
def pass_lock(state_dir: os.PathLike | str) -> Iterator[bool]:
    """Yield True while this process holds the one pass lock, False when
    another pass holds it. `flock` is released by the kernel when its holder
    exits, however it exits."""
    path = mailindex.mail_dir(state_dir) / "watch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_reader() -> roles.Charter:
    catalogue = roles.load_charters()
    roles.check_wirings(catalogue)
    return catalogue[mailreader.READER]


def load_settler() -> roles.Charter | None:
    """The settlement judge's charter, or None when it does not load — said,
    and the pass runs without the step (items stay open; the report counts the
    candidates as not yet judged)."""
    try:
        return mailsettle.load_settler()
    except (roles.CharterError, KeyError) as exc:
        log(f"the settlement judge's charter does not load; no settlement step: {exc}")
        return None


def _main_reread(state_dir: Path, model_spec: str, limit: int | None) -> int:
    """`--reread-blind`: the backlog of readings made without their body, in
    one run, under the one pass lock. Records a `mail_reread` run line."""
    if not model_spec:
        log("--reread-blind needs --model")
        return 2
    try:
        charter = load_reader()
    except roles.CharterError as exc:
        log(f"the Reader's charter does not load: {exc}")
        return 2
    started = time.time()
    with pass_lock(state_dir) as held:
        if not held:
            log("another pass holds the lock — the re-read does nothing")
            return 3
        index = mailindex.MailIndex(state_dir)
        found = len(blind_readings(state_dir, index, model_spec))
        done = reread_blind(state_dir, index=index, store=mailcases.CaseStore(state_dir),
                            charter=charter, model_spec=model_spec, now=started,
                            limit=limit or found, full=True)
        flush_run(state_dir, "mail_reread", {"blind": found, "reread": done}, started)
    print(json.dumps({"blind": found, "reread": done}, sort_keys=True))
    return 0


def _main_settle(state_dir: Path, model_spec: str, since_text: str) -> int:
    """`--settle`: the settlement step alone, over what is indexed — no
    mailbox read, under the one pass lock. Records a `mail_settle` run line."""
    from . import mailreport

    try:
        charter = load_reader()
    except roles.CharterError as exc:
        log(f"the Reader's charter does not load: {exc}")
        return 2
    settler = load_settler()
    if settler is None:
        return 2
    started = time.time()
    try:
        since = mailreport.parse_since(since_text, started) if since_text else None
    except ValueError:
        log(f"--since must be YYYY-MM-DD or <N>d, got {since_text!r}")
        return 2
    result = Pass()
    with pass_lock(state_dir) as held:
        if not held:
            log("another pass holds the lock — the settlement step does nothing")
            return 3
        settle(state_dir, charter=charter, settler=settler, model_spec=model_spec,
               now=started, result=result, since=since)
        flush_run(state_dir, "mail_settle", {"late_read": result.late_read,
                                             "settlement": result.settlement}, started)
    print(json.dumps({"late_read": result.late_read, "settlement": result.settlement},
                     sort_keys=True))
    return 0


def tuta_env() -> tuple[dict[str, str], str]:
    """The environment for himalaya: this one plus the bridge secret from
    aish's own store when it is not already set. The bridge's own address is
    returned too — it is his, so it joins the owner addresses."""
    from . import secrets

    env = dict(os.environ)
    if not env.get(mailtuta.SECRET_ENV):
        try:
            value = secrets.get(mailtuta.SECRET_ENV)
        except Exception as exc:  # noqa: BLE001 — a missing secret fails the listing, visibly
            log(f"the Tuta secret could not be read: {type(exc).__name__}")
            value = None
        if value:
            env[mailtuta.SECRET_ENV] = value
    address = ""
    try:
        address = str(mailtuta._load_tutalib()._bridge_settings()["email"]).lower()
    except Exception:  # noqa: BLE001 — the listing reports the missing bridge itself
        pass
    return env, address


def _main_recheck(state_dir: Path, owners: set[str], limit: int) -> int:
    """`--recheck-tuta-auth`: Tuta only (no Gmail account is needed), no
    charter, no model, under the one pass lock."""
    env, address = tuta_env()
    if address:
        owners = owners | {address}
    with pass_lock(state_dir) as held:
        if not held:
            log("another pass holds the lock — the recheck does nothing")
            return 3
        started = time.time()
        result = recheck_tuta_auth(mailbox=mailtuta.TutaMailbox(env), state_dir=state_dir,
                                   owner_addresses=frozenset(owners), limit=limit)
        flush_run(state_dir, "mail_tuta_recheck", result.as_json(), started)
    print(json.dumps(result.as_json(), sort_keys=True))
    return 0 if result.listing_ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the inbox assistant, shadow mode (#142 W5)")
    parser.add_argument("--once", action="store_true", help="run one shadow pass")
    parser.add_argument("--counters", action="store_true",
                        help="print what the verdict store holds; no mail, no model")
    parser.add_argument("--backfill", type=int, metavar="DAYS", default=0,
                        help="one-time deterministic pass over INBOX and Sent for DAYS: "
                        "index and case observation only, no Reader, no verdict")
    parser.add_argument("--recheck-tuta-auth", action="store_true",
                        help="one-time: re-fetch Tuta mail indexed before aish read the "
                        "bridge's authentication header, record what the header says onto "
                        "the existing entry, and follow it through the case store; no "
                        "Reader, no verdict, never --seen")
    parser.add_argument("--settle", action="store_true",
                        help="only the settlement step, over the mail already indexed: read "
                        "the answers open follow-ups rest on, then judge tied later mail; no "
                        "mailbox is read")
    parser.add_argument("--since", default="",
                        help="with --settle: YYYY-MM-DD or <N>d — weigh the needs items the "
                        "report would show with the same --since (default: its own window)")
    parser.add_argument("--reobserve", action="store_true",
                        help="one-shot: fold every indexed mail on no case through the case "
                        "store under today's laws (the backfill's observe loop); no mailbox, no "
                        "Reader; idempotent")
    parser.add_argument("--reread-blind", action="store_true",
                        help="one-shot: re-read every reading made without its body although "
                        "the evidence store now holds it (oldest first, up to --max); no "
                        "mailbox is read")
    parser.add_argument("--account", default=os.environ.get("AISH_MAIL_ACCOUNT", ""),
                        help="the Gmail account (needs ~/.config/gws-accounts/<email>/)")
    parser.add_argument("--owner", action="append", default=[],
                        help="an address that is the owner's own (repeatable). List every "
                        "alias he sends from: the counters that make a forged From: him, "
                        "or an unlisted alias in his Sent folder, visible read it")
    parser.add_argument("--source", action="append", choices=SOURCES, default=[],
                        help="an inbox to read (repeatable; default: gmail)")
    parser.add_argument("--model", default=os.environ.get("AISH_MAIL_READER_MODEL", ""))
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--max", type=int, default=None,
                        help=f"per listing (default {DEFAULT_MAX}; {BACKFILL_LIMIT} with "
                        "--backfill)")
    args = parser.parse_args(argv)
    state_dir = paths.state_home()
    if args.counters:
        print(mailverdicts.render_counters(
            mailverdicts.scan_records(mailverdicts.iter_store(state_dir))))
        return 0
    if args.recheck_tuta_auth:
        return _main_recheck(state_dir, {a.lower() for a in args.owner},
                             args.max or BACKFILL_LIMIT)
    if args.settle:
        return _main_settle(state_dir, args.model, args.since)
    if args.reread_blind:
        return _main_reread(state_dir, args.model, args.max)
    if args.reobserve:
        with pass_lock(state_dir) as held:
            if not held:
                log("another pass holds the lock — the re-observation does nothing")
                return 3
            started = time.time()
            done = reobserve(state_dir)
            flush_run(state_dir, "mail_reobserve", done.as_json(), started)
        print(json.dumps(done.as_json(), sort_keys=True, ensure_ascii=False))
        return 0
    if not args.once and args.backfill <= 0:
        parser.print_help()
        return 2
    wanted = list(dict.fromkeys(args.source or ["gmail"]))
    owners = {a.lower() for a in args.owner}
    mailboxes: list[Any] = []
    if "gmail" in wanted:
        env, why = account_env(args.account)
        if env is None:
            log(f"refusing to run: {why}")
            return 2
        owners.add(args.account.lower())
        mailboxes.append(GmailSource(GwsMailbox(env)))
    if "tuta" in wanted:
        env, address = tuta_env()
        if address:
            owners.add(address)
        mailboxes.append(mailtuta.TutaMailbox(env))
    if args.backfill > 0:
        # No charter, no model: the backfill has no Reader to load.
        with pass_lock(state_dir) as held:
            if not held:
                log("another pass holds the lock — the backfill does nothing")
                return 3
            started = time.time()
            filled = backfill(mailboxes=mailboxes, state_dir=state_dir,
                              owner_addresses=frozenset(owners), days=args.backfill,
                              limit=args.max or BACKFILL_LIMIT)
            flush_backfill(state_dir, filled, started)
        print(json.dumps(filled.as_json(), sort_keys=True))
        return 0 if filled.listing_ok else 1
    try:
        charter = load_reader()
    except roles.CharterError as exc:
        log(f"the Reader's charter does not load: {exc}")
        return 2
    settler = load_settler()
    with pass_lock(state_dir) as held:
        if not held:
            log("another pass holds the lock — this one does nothing")
            return 3
        started = time.time()
        result = run_once(
            mailboxes=mailboxes,
            state_dir=state_dir,
            owner_addresses=frozenset(owners),
            model_spec=args.model,
            charter=charter,
            days=args.days,
            limit=args.max or DEFAULT_MAX,
            settler=settler,
        )
        record_health(state_dir, result, started)
        flush_counters(state_dir, result, started)
    print(json.dumps(result.as_json(), sort_keys=True))
    return 0 if result.listing_ok else 1


if __name__ == "__main__":
    sys.exit(main())
