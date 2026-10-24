"""`aish-mailwatch` — the inbox assistant in SHADOW mode (#142 W5, slice S1).

Every new mail in the owner's personal inbox flows Gate → Reader → a recorded
verdict, and the verdict acts on NOTHING: no label is changed, nothing is
archived or trashed, nobody is notified. What it produces is the verdict store
(`mailverdicts`) and the counters over it, for the driver to read.

    aish-mailwatch --once --account pawel@wenda.eu --model <spec>
    aish-mailwatch --counters        # what the store holds; no mail, no model

**Read-only by construction, not by care.** Every call to the mailbox goes
through `GwsMailbox`, whose runner refuses any `gws` invocation outside
`READ_ONLY_CALLS` (list and get) before a process is spawned. There is no code
here that could modify, label, send or delete.

**Fail closed on credentials.** The account must have its own credential
directory (`~/.config/gws-accounts/<email>/`, the per-account pattern the
owner's gmail tools use); a missing one refuses the run rather than falling
back to another account's mailbox.

The effectful edges — the gws runner, the clock, the model's chat callable —
are parameter seams, so the tests use a fake mailbox, a scripted role and no
sleeps, in the shape `email_poll.run_poll` already proved.

`docs/mail-assistant.md`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
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

READ_ONLY_CALLS = frozenset({
    ("gmail", "users", "messages", "list"),
    ("gmail", "users", "messages", "get"),
})

DEFAULT_DAYS = 2
DEFAULT_MAX = 50
GWS_TIMEOUT = 60

Runner = Callable[[list[str], dict[str, str]], "dict | list | None"]


def log(msg: str) -> None:
    print(f"[mailwatch] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- the mailbox


class ReadOnlyViolation(RuntimeError):
    """A gws call outside the read-only allowlist. Raised before any process
    is spawned: this is the enforcing line for "S1 makes read-only calls"."""


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


def default_runner(args: list[str], env: dict[str, str]) -> dict | list | None:
    try:
        done = subprocess.run(
            ["gws", *args], capture_output=True, text=True, timeout=GWS_TIMEOUT, env=env
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log(f"gws {args[:4]} failed: {exc}")
        return None
    if done.returncode != 0:
        log(f"gws {args[:4]} rc={done.returncode}: {done.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        return None


class GwsMailbox:
    """The owner's Gmail through `gws`, read-only."""

    def __init__(self, env: dict[str, str], runner: Runner = default_runner) -> None:
        self.env = env
        self.runner = runner

    def _call(self, args: list[str]) -> dict | list | None:
        if tuple(args[:4]) not in READ_ONLY_CALLS:
            raise ReadOnlyViolation(f"refused: gws {' '.join(args[:4])} is not read-only")
        return self.runner(args, self.env)

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
    auth = mailgate.authenticate(header.auth_results, mailindex.domain_of(header.from_addr))
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
        auth = mailgate.authenticate(header.auth_results, mailindex.domain_of(header.from_addr))
        observed[header.digest] = store.observe(header, item.body, auth.authenticated, index)
    return observed


# ---------------------------------------------------------------- one pass


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

    def as_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def health_path(state_dir: os.PathLike | str) -> Path:
    return mailindex.mail_dir(state_dir) / "watch_health.json"


def record_health(state_dir: os.PathLike | str, result: Pass, now: float) -> dict[str, Any]:
    """Consecutive failed listings, the last good pass and the last pass's
    counts. Counted only: S1 pushes nothing to the owner (#362's escalation
    is the pattern a later slice inherits, not something this one does)."""
    path = health_path(state_dir)
    try:
        prior = json.loads(path.read_text())
        streak = int(prior.get("failed_listings") or 0)
        last_ok = prior.get("last_ok_at")
    except (OSError, ValueError, TypeError, AttributeError):
        streak, last_ok = 0, None
    if result.listing_ok:
        streak, last_ok = 0, now
    else:
        streak += 1
    state = {"failed_listings": streak, "last_ok_at": last_ok, "last_pass_at": now,
             "last_pass": result.as_json()}
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


def run_once(
    *,
    mailbox: Any,
    state_dir: os.PathLike | str,
    owner_addresses: frozenset[str],
    model_spec: str,
    charter: roles.Charter,
    chat: Callable[..., Any] | None = None,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_MAX,
    now: Callable[[], float] = time.time,
) -> Pass:
    """One shadow pass: list, fetch, index, assess the new inbound, record."""
    result = Pass()
    index = mailindex.MailIndex(state_dir)
    store = mailcases.CaseStore(state_dir)
    verdicts = mailverdicts.VerdictStore(state_dir)
    inbox = mailbox.list(f"in:inbox newer_than:{days}d", limit)
    sent = mailbox.list(f"in:sent newer_than:{days}d", limit)
    if inbox is None or sent is None:
        result.listing_ok = False
    inbox_ids = list(dict.fromkeys(inbox or ()))
    ids = list(dict.fromkeys(inbox_ids + list(sent or ())))
    result.listed = len(ids)
    parsed: list[mailindex.Parsed] = []
    for message_id in ids:
        msg = mailbox.get(message_id)
        if msg is None:
            result.fetch_failed += 1
            continue
        result.fetched += 1
        item = mailindex.from_gmail(msg, owner_addresses)
        if item is None:
            result.unparseable += 1
            continue
        parsed.append(item)
    observed = ingest(index, store, parsed)
    result.new_in_index = len(observed)
    inbox_set = set(inbox_ids)
    for item in sorted(parsed, key=lambda p: p.header.position):
        header = index.get(item.header.digest)
        if header is None or header.direction != "in" or item.header.provider_id not in inbox_set:
            continue
        if verdicts.has(header.digest, model_spec, charter.digest):
            result.already_recorded += 1
            continue
        record = assess(
            index, store, header, charter=charter, model_spec=model_spec, chat=chat,
            state_dir=state_dir, observation=observed.get(header.digest), now=now(),
        )
        verdicts.write(record)
        result.assessed += 1
        result.endings[record["ending"]] = result.endings.get(record["ending"], 0) + 1
    return result


def load_reader() -> roles.Charter:
    catalogue = roles.load_charters()
    roles.check_wirings(catalogue)
    return catalogue[mailreader.READER]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the inbox assistant, shadow mode (#142 W5 S1)")
    parser.add_argument("--once", action="store_true", help="run one shadow pass")
    parser.add_argument("--counters", action="store_true",
                        help="print what the verdict store holds; no mail, no model")
    parser.add_argument("--account", default=os.environ.get("AISH_MAIL_ACCOUNT", ""))
    parser.add_argument("--owner", action="append", default=[],
                        help="an address that is the owner's own (repeatable; the account is one)")
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
    env, why = account_env(args.account)
    if env is None:
        log(f"refusing to run: {why}")
        return 2
    try:
        charter = load_reader()
    except roles.CharterError as exc:
        log(f"the Reader's charter does not load: {exc}")
        return 2
    started = time.time()
    result = run_once(
        mailbox=GwsMailbox(env),
        state_dir=state_dir,
        owner_addresses=frozenset({args.account.lower(), *(a.lower() for a in args.owner)}),
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
