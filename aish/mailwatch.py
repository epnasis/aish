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
    mailreader,
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


def _case_ids_by_key(store: mailcases.CaseStore, header: mailindex.Header, body: str) -> list[str]:
    keys = set(mailcases.extract_keys(f"{header.subject}\n{body}"))
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
        grade=mailgate.grade(index, header, auth, store.owner_case_parties(as_of)),
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
    linked = _case_ids_by_key(store, header, body)
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
    return mailverdicts.build_record(
        header=header, facts=facts, reading=reading, charter=charter, model=model_spec,
        observation=observation, flags=flags, code_coordinates=code_coordinates, now=now,
    )


def ingest(
    index: mailindex.MailIndex,
    store: mailcases.CaseStore,
    parsed: Iterable[mailindex.Parsed],
) -> dict[str, mailcases.Observation]:
    """Fold messages into the index and the case store, oldest first. Only a
    message the index did not already hold is observed, so re-running is a
    no-op."""
    observed: dict[str, mailcases.Observation] = {}
    for item in sorted(parsed, key=lambda p: p.header.position):
        if not index.add(item.header, item.body):
            continue
        header = index.get(item.header.digest) or item.header
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
    ledger: UnfetchedLedger,
    owners: frozenset[str],
    days: int,
    limit: int,
    now: float,
) -> tuple[list[mailindex.Parsed], list[str]]:
    """List one source, fetch what the index does not already hold, and say
    which digests are INBOX mail. Returns (parsed, inbox digests)."""
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
    if source.stable_ids:
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
            if source.stable_ids:
                ledger.done(source.name, ref)
            continue
        item, failure = source.fetch(ref, owners)
        if failure is not None:
            sp.fetch_failed[failure.kind] = sp.fetch_failed.get(failure.kind, 0) + 1
            if source.stable_ids:
                ledger.missed(source.name, ref, role, failure, now)
            continue
        if source.stable_ids:
            ledger.done(source.name, ref)
        sp.fetched += 1
        if item is None:
            sp.unparseable += 1
            continue
        if item.header.at_source != "provider":
            sp.at_from_sender += 1
        parsed.append(item)
        if role == "inbox":
            inbox_digests.append(item.header.digest)
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
) -> Pass:
    """One shadow pass over every source: list, fetch what is new, index,
    assess the new inbound, record. One state tree for all of them — the
    ledger must see every act of his, whichever inbox it is in — and digest
    dedup absorbs a mail delivered to both."""
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
    for header in sorted((h for h in inbox_headers if h is not None), key=lambda h: h.position):
        if header.direction != "in":
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
    return result


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


def tuta_env() -> tuple[dict[str, str], str]:
    """The environment for himalaya: this one plus the bridge secret from
    aish's own store when it is not already set. The bridge's own address is
    returned too — it is his, so it joins the owner addresses."""
    from . import mailtuta, secrets

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the inbox assistant, shadow mode (#142 W5)")
    parser.add_argument("--once", action="store_true", help="run one shadow pass")
    parser.add_argument("--counters", action="store_true",
                        help="print what the verdict store holds; no mail, no model")
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
    parser.add_argument("--max", type=int, default=DEFAULT_MAX)
    args = parser.parse_args(argv)
    state_dir = paths.state_home()
    if args.counters:
        print(mailverdicts.render_counters(
            mailverdicts.scan_records(mailverdicts.iter_store(state_dir))))
        return 0
    if not args.once:
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
        from . import mailtuta

        env, address = tuta_env()
        if address:
            owners.add(address)
        mailboxes.append(mailtuta.TutaMailbox(env))
    try:
        charter = load_reader()
    except roles.CharterError as exc:
        log(f"the Reader's charter does not load: {exc}")
        return 2
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
            limit=args.max,
        )
        record_health(state_dir, result, started)
        flush_counters(state_dir, result, started)
    print(json.dumps(result.as_json(), sort_keys=True))
    return 0 if result.listing_ok else 1


if __name__ == "__main__":
    sys.exit(main())
