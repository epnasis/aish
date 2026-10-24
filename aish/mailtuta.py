"""The owner's PRIMARY mailbox — Tutanota, through tutabridge and himalaya —
as a second source for `aish-mailwatch` (#142 W5 S1.5).

The same seam as the Gmail source (`mailsource.GmailSource`): a listing of
inbox and sent, a fetch per mail, a parser. What differs is what the gateway
can vouch for, and each difference is enforced here, not hoped for:

- **No authentication result is readable through this gateway.** Nothing
  prepends one above the sender's, so the parser drops every
  Authentication-Results header (`mailindex.from_rfc822`) and the Gate gives
  the explicit ending (`mailgate.authenticate_header`). A Tuta mail is never
  authenticated, so it can never mint a grade, admit an alias, open a case or
  enter a case's account history.
- **Reading never sets \\Seen.** His unread markers are both his mailbox
  state and the shadow's ground truth. `message read` runs WITHOUT `--seen`
  — the discipline of `tuta_read` / `tutalib.read_body` — and `check_argv`
  refuses, before any process starts, any argv outside an allowlist of
  subcommands AND flags. A naive `message read --seen` is refused, not run.
- **Listing is envelope ENUMERATION, never SEARCH.** tutabridge answers some
  SEARCHes with a silently empty set; a SEARCH-based listing would read as a
  healthy, quiet mailbox forever — the email-poll outage's shape. Here a
  listing pages the folder by sequence number from its STATUS count, and a
  listing is a failure only when the enumeration itself failed.
- **Provider ids are not stable.** tutabridge renumbers UIDs on restart
  without bumping UIDVALIDITY, so nothing is keyed by UID across passes: no
  top-up ledger and no skip-by-id. A fetch is skipped only when the
  envelope's own Message-ID maps to a digest the index already holds — an
  identity the renumbering cannot change. Everything else is refetched and
  digest dedup absorbs it.

Stdlib only at the seam: the default runner shells out to himalaya with the
config tutalib generates; the tests inject a fake runner.
`docs/mail-assistant.md` §Ingress.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import mailindex, paths
from .mailsource import (
    MISSING,
    PARSE,
    TIMEOUT,
    TRANSIENT,
    UNKNOWN,
    Failure,
    Listing,
    Outcome,
    ReadOnlyViolation,
)

NAME = "tuta"
SECRET_ENV = "TUTA_BRIDGE_PASSWORD"
HIMALAYA_TIMEOUT = 60
# Sequence numbers per envelope page. Every himalaya call is a fresh TLS
# connection to the bridge, which "falls over under a burst of them"
# (tutalib); a page is one connection.
ENVELOPE_PAGE = 200

BRIDGE_STATUS = Path.home() / "Library/Application Support/tutabridge/status.json"
DISCONNECT_GRACE_SECONDS = 300

# The read-only allowlist: (subcommand) -> (flags allowed, positional count,
# whether `-m <mailbox>` may appear). A flag not listed — `--seen` above all —
# is refused, as is any subcommand not listed (store, move, copy, append,
# expunge, raw, ...).
ALLOWED_ARGV: dict[tuple[str, str], tuple[frozenset[str], int, bool]] = {
    ("imap", "list"): (frozenset({"--all", "--json"}), 0, False),
    ("imap", "status"): (frozenset({"--json"}), 1, False),
    ("imap", "fetch"): (frozenset({"--seq", "--envelope", "--flags", "--json"}), 1, True),
    ("message", "read"): (frozenset({"--raw", "--json"}), 1, True),
}

Runner = Callable[[list[str], dict[str, str]], Outcome]


def check_argv(args: list[str]) -> None:
    """Refuse any himalaya argv outside `ALLOWED_ARGV`, flags included —
    BEFORE a process is spawned."""
    key = tuple(args[:2])
    allowed = ALLOWED_ARGV.get(key)  # type: ignore[arg-type]
    if allowed is None:
        raise ReadOnlyViolation(f"refused: himalaya {' '.join(args[:2])} is not read-only")
    flags, positionals, mailbox_ok = allowed
    seen_positionals = 0
    rest = iter(args[2:])
    for token in rest:
        if token == "-m" and mailbox_ok:
            if next(rest, None) is None:
                raise ReadOnlyViolation("refused: -m with no mailbox")
            continue
        if token in flags:
            continue
        if token.startswith("-"):
            raise ReadOnlyViolation(
                f"refused: himalaya {' '.join(args[:2])} {token} — not an allowed read-only flag"
                + (" (reading must never mark mail seen)" if "seen" in token.lower() else "")
            )
        seen_positionals += 1
    if seen_positionals != positionals:
        raise ReadOnlyViolation(
            f"refused: himalaya {' '.join(args[:2])} takes {positionals} argument(s)"
        )


def _load_tutalib() -> Any:
    """The private tutalib (`~/.config/aish/tools/_tuta/tutalib.py`): the ONE
    place the himalaya config for the bridge is generated. Loaded by path
    because it lives in the owner's tools tree, not in this package."""
    path = paths.config_home() / "tools" / "_tuta" / "tutalib.py"
    spec = importlib.util.spec_from_file_location("aish_mailwatch_tutalib", path)
    if spec is None or spec.loader is None or not path.is_file():
        raise FileNotFoundError(f"tutalib not found at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def default_runner(args: list[str], env: dict[str, str]) -> Outcome:
    """One himalaya call. Typed failures; no retry here — the next pass is
    the retry, and the bridge is local (there is no quota to wait out)."""
    try:
        lib = _load_tutalib()
        argv = [lib._himalaya_bin(), "-c", str(lib.config_path()), *args]
    except Exception as exc:  # noqa: BLE001 — a missing tool is a typed failure
        return Outcome(failure=Failure(MISSING, matched=type(exc).__name__, text=str(exc)))
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=HIMALAYA_TIMEOUT,
                              env=env)
    except FileNotFoundError as exc:
        return Outcome(failure=Failure(MISSING, matched="FileNotFoundError", text=str(exc)))
    except subprocess.TimeoutExpired:
        return Outcome(failure=Failure(TIMEOUT, matched=f"{HIMALAYA_TIMEOUT}s", text=""))
    if done.returncode != 0:
        return Outcome(failure=Failure(TRANSIENT, matched=f"rc={done.returncode}",
                                       text=(done.stderr or done.stdout or "").strip()))
    try:
        return Outcome(json.loads(done.stdout or "null"))
    except json.JSONDecodeError:
        return Outcome(failure=Failure(PARSE, matched="not JSON", text=done.stdout[:200]))


def _envelope_time(envelope: dict[str, Any]) -> float | None:
    raw = str(envelope.get("date") or "").strip()
    if not raw:
        return None
    try:
        return mailindex.date_header_time(raw)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.timestamp()


def _ref(folder: str, uid: int) -> str:
    return f"{NAME}:{uid}:{folder}"


def _parse_ref(ref: str) -> tuple[str, int]:
    _name, uid, folder = ref.split(":", 2)
    return folder, int(uid)


class TutaMailbox:
    """The Tuta source. `runner` is the seam: `(argv, env) -> Outcome`."""

    name = NAME
    stable_ids = False

    def __init__(
        self,
        env: dict[str, str],
        runner: Runner = default_runner,
        *,
        now: Callable[[], float] | None = None,
        status_path: Path = BRIDGE_STATUS,
    ) -> None:
        self.env = env
        self.runner = runner
        self.now = now or (lambda: datetime.datetime.now(datetime.UTC).timestamp())
        self.status_path = status_path
        self.last_failure: Failure | None = None
        self.sent_folder = ""
        self._envelopes: dict[str, dict[str, Any]] = {}
        self.counts: dict[str, int] = {}

    def _bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def _call(self, args: list[str]) -> Any:
        check_argv(args)
        outcome = self.runner(args, self.env)
        if outcome.failure is not None:
            self.last_failure = outcome.failure
            return None
        return outcome.value

    def _folders(self) -> tuple[str, str] | None:
        doc = self._call(["imap", "list", "--all", "--json"])
        boxes = doc.get("mailboxes") if isinstance(doc, dict) else None
        if not isinstance(boxes, list) or not boxes:
            self.last_failure = self.last_failure or Failure(PARSE, matched="no mailboxes")
            return None
        inbox = sent = ""
        for box in boxes:
            if not isinstance(box, dict):
                continue
            name = str(box.get("name") or "")
            attrs = {str(a).lower() for a in box.get("attributes") or ()}
            if name.upper() == "INBOX":
                inbox = name
            if "\\sent" in attrs:
                sent = name
        if not sent:
            sent = next((str(b.get("name")) for b in boxes if isinstance(b, dict)
                         and str(b.get("name") or "").lower() == "sent"), "")
        if not inbox or not sent:
            # Without his Sent folder the ledger cannot see his acts: that is a
            # listing that failed, not one that is merely incomplete.
            self.last_failure = Failure(PARSE, matched="no INBOX or no Sent folder",
                                        text=", ".join(str(b.get("name")) for b in boxes
                                                       if isinstance(b, dict)))
            return None
        return inbox, sent

    def _enumerate(self, folder: str, floor: float, limit: int) -> list[str] | None:
        """Refs of the folder's messages dated at or after `floor`, newest
        first, by sequence-number pages from the STATUS count down. A message
        whose envelope date cannot be read is kept (bounded by `limit`):
        dropping it would be a silent miss."""
        status = self._call(["imap", "status", folder, "--json"])
        if not isinstance(status, dict) or not isinstance(status.get("messages"), int):
            self.last_failure = self.last_failure or Failure(PARSE, matched="no message count")
            return None
        hi = int(status["messages"])
        refs: list[str] = []
        while hi >= 1 and len(refs) < limit:
            lo = max(1, hi - ENVELOPE_PAGE + 1)
            page = self._call(["imap", "fetch", "-m", folder, "--seq", "--envelope", "--json",
                               f"{lo}:{hi}"])
            messages = page.get("messages") if isinstance(page, dict) else None
            if not isinstance(messages, list):
                self.last_failure = self.last_failure or Failure(PARSE, matched="no messages")
                return None
            newest_dated = None
            dated: list[tuple[float, int, dict]] = []
            for message in messages:
                if not isinstance(message, dict) or message.get("uid") is None:
                    continue
                raw_envelope = message.get("envelope")
                envelope: dict[str, Any] = raw_envelope if isinstance(raw_envelope, dict) else {}
                at = _envelope_time(envelope)
                if at is None:
                    self._bump("undated_envelopes")
                    at = float("inf")
                elif newest_dated is None or at > newest_dated:
                    newest_dated = at
                dated.append((at, int(message["uid"]), envelope))
            for at, uid, envelope in sorted(dated, key=lambda d: d[1], reverse=True):
                if at < floor:
                    continue
                ref = _ref(folder, uid)
                self._envelopes[ref] = envelope
                refs.append(ref)
            if newest_dated is not None and newest_dated < floor:
                break  # this whole page is older than the window
            hi = lo - 1
        return refs[:limit]

    def listing(self, days: int, limit: int) -> Listing | None:
        self.last_failure = None
        if not self.env.get(SECRET_ENV):
            self.last_failure = Failure(MISSING, matched=SECRET_ENV,
                                        text=f"secret {SECRET_ENV} is not available")
            return None
        folders = self._folders()
        if folders is None:
            return None
        inbox_folder, self.sent_folder = folders
        floor = self.now() - days * 86400
        inbox = self._enumerate(inbox_folder, floor, limit)
        sent = self._enumerate(self.sent_folder, floor, limit) if inbox is not None else None
        if inbox is None or sent is None:
            return None
        return Listing(inbox, sent)

    def held(self, ref: str, index: mailindex.MailIndex, by_pid: dict[str, str]) -> str | None:
        """The digest the index already holds for this message — by the
        envelope's Message-ID, never by UID."""
        message_id = mailindex.normalize_message_id(
            str((self._envelopes.get(ref) or {}).get("message_id") or "")
        )
        if not message_id:
            return None
        digest = mailindex.digest_for(message_id, "", "", "", "")
        return digest if digest in index.by_digest else None

    def fetch(
        self, ref: str, owners: frozenset[str]
    ) -> tuple[mailindex.Parsed | None, Failure | None]:
        folder, uid = _parse_ref(ref)
        self.last_failure = None
        doc = self._call(["message", "read", "-m", folder, "--raw", "--json", str(uid)])
        if doc is None:
            return None, self.last_failure or Failure(UNKNOWN, text="no message")
        raw = doc.get("message") if isinstance(doc, dict) else doc
        if not isinstance(raw, str):
            return None, Failure(PARSE, matched="no raw message")
        parsed = mailindex.from_rfc822(raw, provider_id=ref, mailbox=NAME,
                                       in_sent_folder=folder == self.sent_folder)
        if parsed is not None:
            listed = mailindex.normalize_message_id(
                str((self._envelopes.get(ref) or {}).get("message_id") or ""))
            if listed and parsed.header.message_id != listed:
                # The bridge renumbered between listing and fetch: this UID now
                # holds another message of the same folder. It is still a real
                # message there, so it is kept; the listed one is met next pass.
                self._bump("renumbered_between_list_and_fetch")
            if parsed.header.direction == "in" and parsed.header.from_addr in owners:
                # Counted, and nothing more: on this gateway a From header
                # naming him makes nothing his.
                self._bump("inbox_from_an_owner_address")
            if parsed.header.direction == "out" and parsed.header.from_addr not in owners:
                # In his Sent folder from an address not passed as --owner: an
                # alias the watcher was not told about.
                self._bump("sent_from_an_unlisted_address")
        return parsed, None

    def note(self) -> str:
        """What tutabridge last said about its link to Tuta, when it is not a
        live "Connected" — the bridge's own words and time; never why."""
        try:
            status = json.loads(self.status_path.read_text())
            state = str(status["tuta_connection"])
            since = int(status["since_unix"])
            pid = int(status["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return (f"tutabridge's status file ({self.status_path}) is missing or unreadable — "
                    "whether the bridge is in sync with Tuta is unknown")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ("the tutabridge process that last reported its state is not running — "
                    "whether the bridge is in sync with Tuta is unknown")
        except PermissionError:
            pass
        if state == "Connected" or self.now() - since < DISCONNECT_GRACE_SECONDS:
            return ""
        when = datetime.datetime.fromtimestamp(since, datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
        return f"tutabridge reports it has not been connected to Tuta since {when} (state: {state})"
