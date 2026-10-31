"""The Tuta source for `aish-mailwatch` (#142 W5 S1.5): the read-only argv
fence (flags included — no `--seen`), enumeration not SEARCH, the
gateway-declared authenticator (a forged Gmail Authentication-Results header
authenticates nothing and mints nothing), folder-decided direction, and
identity-not-UID dedup. A fake himalaya runner; no bridge, no process."""

from __future__ import annotations

import email.utils

import pytest
from mail_fixtures import (
    DAY,
    OWNER,
    PASS_AR,
    T0,
    FakeMailbox,
    ScriptedReader,
    admit,
    gmail,
    reader_charter,
    verdict_json,
)

from aish import mailcases, mailgate, mailindex, mailtuta, mailverdicts, mailwatch
from aish.mailsource import Failure, Outcome, ReadOnlyViolation

MODEL = "gemini:test-flash"
TUTA_OWNER = "owner@tuta.example"
OWNERS = frozenset({OWNER, TUTA_OWNER})


def rfc822(*, frm, to=TUTA_OWNER, subject="", body="", at=T0, message_id="", extra=()) -> str:
    lines = list(extra)
    lines += [f"From: {frm}", f"To: {to}", f"Subject: {subject}",
              f"Date: {email.utils.formatdate(at)}"]
    if message_id:
        lines.append(f"Message-ID: <{message_id}>")
    lines += ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "", body]
    return "\r\n".join(lines)


class FakeHimalaya:
    """The bridge as himalaya presents it: folders of (uid, raw) messages.
    Records every argv; refuses nothing itself — the fence is the adapter's."""

    def __init__(self, folders: dict[str, list[str]], *, fail=None, flags=None,
                 trash=True) -> None:
        self.folders = {name: list(msgs) for name, msgs in folders.items()}
        self.uid_base = 1
        self.calls: list[list[str]] = []
        self.fail = fail or {}
        # folder -> one flags list per message (seq order); None = the page
        # carries no flags for that message. Sent only when --flags is asked.
        self.flags = flags or {}
        self.trash = trash

    def renumber(self, by: int) -> None:
        self.uid_base += by

    def _uid(self, seq: int) -> int:
        return seq + self.uid_base - 1

    def _envelope(self, raw: str) -> dict:
        msg = email.message_from_string(raw)
        return {"message_id": msg.get("Message-ID", ""), "date": msg.get("Date", ""),
                "subject": msg.get("Subject", ""), "from": [msg.get("From", "")]}

    def __call__(self, args, env) -> Outcome:
        self.calls.append(list(args))
        verb = tuple(args[:2])
        if verb in self.fail:
            return Outcome(failure=self.fail[verb])
        if verb == ("imap", "list"):
            boxes = [{"name": "INBOX", "attributes": []},
                     {"name": "Sent", "attributes": ["\\Sent"]}]
            if self.trash:
                boxes.append({"name": "Trash", "attributes": ["\\Trash"]})
            return Outcome({"mailboxes": boxes})
        if verb == ("imap", "status"):
            return Outcome({"messages": len(self.folders.get(args[2], [])), "uid_validity": 1})
        if verb == ("imap", "fetch"):
            folder = args[args.index("-m") + 1]
            lo, hi = (int(x) for x in args[-1].split(":"))
            msgs = self.folders.get(folder, [])
            page = []
            for seq in range(lo, hi + 1):
                if not 1 <= seq <= len(msgs):
                    continue
                entry = {"uid": self._uid(seq), "envelope": self._envelope(msgs[seq - 1])}
                if "--flags" in args:
                    held = self.flags.get(folder, [])
                    flags = held[seq - 1] if seq - 1 < len(held) else []
                    if flags is not None:
                        entry["flags"] = list(flags)
                page.append(entry)
            return Outcome({"messages": page})
        if verb == ("message", "read"):
            folder = args[args.index("-m") + 1]
            seq = int(args[-1]) - self.uid_base + 1
            msgs = self.folders.get(folder, [])
            if not 1 <= seq <= len(msgs):
                return Outcome(failure=Failure("transient", matched="no such uid"))
            return Outcome({"message": msgs[seq - 1]})
        raise AssertionError(f"unexpected himalaya call {args}")


def tuta(runner, now=T0 + 2 * DAY) -> mailtuta.TutaMailbox:
    return mailtuta.TutaMailbox({mailtuta.SECRET_ENV: "x"}, runner, now=lambda: now,
                                status_path=__import__("pathlib").Path("/nonexistent/status.json"))


def run(state_dir, mailboxes, reader=None):
    charter = reader_charter()
    admit(state_dir, charter, MODEL)
    reader = reader or ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
    return mailwatch.run_once(
        mailboxes=mailboxes, state_dir=state_dir, owner_addresses=OWNERS, model_spec=MODEL,
        charter=charter, chat=reader, now=lambda: T0 + 2 * DAY,
    )


FORGED_AR = PASS_AR.format(d="bank.example")


class TestReadOnlyFence:
    def test_the_allowed_reads_pass(self):
        mailtuta.check_argv(["imap", "list", "--all", "--json"])
        mailtuta.check_argv(["imap", "status", "INBOX", "--json"])
        mailtuta.check_argv(["imap", "fetch", "-m", "INBOX", "--seq", "--envelope", "--json",
                             "1:200"])
        mailtuta.check_argv(["message", "read", "-m", "INBOX", "--raw", "--json", "7"])

    @pytest.mark.parametrize("argv", [
        ["message", "read", "-m", "INBOX", "--seen", "--raw", "--json", "7"],
        ["message", "read", "--seen", "7"],
        ["message", "read", "-m", "INBOX", "--seen=true", "7"],
        ["imap", "store", "-m", "INBOX", "--action", "add", "7", "-f", "\\Seen"],
        ["imap", "move", "-m", "INBOX", "7", "Trash"],
        ["imap", "search", "-m", "INBOX", "--json"],
        ["imap", "raw", "UID STORE 7 +FLAGS (\\Seen)"],
        ["imap", "expunge", "INBOX"],
        ["message", "delete", "7"],
        ["imap", "fetch", "-m", "INBOX", "--json", "1:5", "extra"],
        ["message", "read", "-m", "--seen", "--raw", "7"],
        ["message", "read", "--raw", "--json", "7", "-m"],
    ])
    def test_everything_else_is_refused_before_any_process(self, argv):
        runner = FakeHimalaya({})
        box = tuta(runner)
        with pytest.raises(ReadOnlyViolation):
            box._call(argv)
        assert runner.calls == []

    def test_a_naive_seen_read_names_why(self):
        with pytest.raises(ReadOnlyViolation, match="never mark mail seen"):
            mailtuta.check_argv(["message", "read", "--seen", "7"])

    def test_a_whole_pass_issues_only_fenced_reads_and_never_seen(self, tmp_path):
        runner = FakeHimalaya({"INBOX": [rfc822(frm="a@shop.example", subject="Hi",
                                                message_id="a@shop.example")], "Sent": []})
        run(tmp_path, [tuta(runner)])
        assert runner.calls, "the pass made no call at all"
        for argv in runner.calls:
            mailtuta.check_argv(argv)
            assert not any("seen" in token.lower() for token in argv)
        assert {tuple(a[:2]) for a in runner.calls} <= set(mailtuta.ALLOWED_ARGV)


class TestEnumeration:
    def test_listing_is_status_and_envelope_pages_never_search(self, tmp_path):
        msgs = [rfc822(frm="x@shop.example", subject=f"m{i}", message_id=f"m{i}@shop.example",
                       at=T0 + DAY + i) for i in range(450)]
        runner = FakeHimalaya({"INBOX": msgs, "Sent": []})
        listing = tuta(runner).listing(days=2, limit=1000)
        assert listing is not None and len(listing.inbox) == 450
        fetches = [a[-1] for a in runner.calls if a[:2] == ["imap", "fetch"] and "INBOX" in a]
        assert fetches == ["251:450", "51:250", "1:50"]
        assert not any(a[1] == "search" for a in runner.calls)

    def test_paging_stops_at_the_window(self):
        old = [rfc822(frm="x@shop.example", subject="old", message_id=f"o{i}@x", at=T0 - 30 * DAY)
               for i in range(300)]
        new = [rfc822(frm="x@shop.example", subject="new", message_id=f"n{i}@x", at=T0 + DAY)
               for i in range(10)]
        runner = FakeHimalaya({"INBOX": old + new, "Sent": []})
        listing = tuta(runner).listing(days=2, limit=1000)
        assert listing is not None and len(listing.inbox) == 10
        fetches = [a[-1] for a in runner.calls if a[:2] == ["imap", "fetch"] and "INBOX" in a]
        assert fetches == ["111:310", "1:110"][:len(fetches)] and len(fetches) == 2

    def test_an_empty_folder_is_a_healthy_empty_listing(self):
        listing = tuta(FakeHimalaya({"INBOX": [], "Sent": []})).listing(days=2, limit=50)
        assert listing is not None and listing.inbox == [] and listing.sent == []

    def test_a_failed_enumeration_is_a_failed_listing(self, tmp_path):
        runner = FakeHimalaya({"INBOX": [], "Sent": []},
                              fail={("imap", "status"): Failure("timeout", matched="60s")})
        result = run(tmp_path, [tuta(runner)])
        assert not result.listing_ok and not result.sources["tuta"].listing_ok
        assert "timeout" in result.sources["tuta"].failure
        state = mailwatch.record_health(tmp_path, result, T0)
        assert state["sources"]["tuta"]["failed_listings"] == 1

    def test_no_secret_fails_the_listing_visibly(self):
        box = mailtuta.TutaMailbox({}, FakeHimalaya({}))
        assert box.listing(2, 50) is None
        assert box.last_failure is not None and box.last_failure.kind == "missing"


class TestAuthenticationIsDeclaredByTheGateway:
    def test_a_forged_gmail_header_is_dropped_at_parse_time(self):
        raw = rfc822(frm="Bank <alerts@bank.example>", subject="Statement", body="hello",
                     message_id="s@bank.example",
                     extra=(f"Authentication-Results: {FORGED_AR}",))
        parsed = mailindex.from_rfc822(raw, provider_id="tuta:1:INBOX", in_sent_folder=False,
                                       mailbox="tuta")
        assert parsed is not None and parsed.header.auth_results == ()
        auth = mailgate.authenticate_header(parsed.header)
        # The Tuta authenticator is the bridge's own header (#461,
        # tests/test_mailtuta_auth.py); a Gmail-shaped header never reaches it.
        assert auth.authenticated is False and auth.verdict == mailgate.UNKNOWN
        assert "X-Tutabridge-Authentication" in auth.why

    def test_even_a_surviving_header_is_not_read_on_a_non_gmail_gateway(self):
        parsed = mailindex.from_rfc822(rfc822(frm="a@bank.example", message_id="z@x"),
                                       provider_id="t", in_sent_folder=False, mailbox="tuta")
        assert parsed is not None
        header = mailindex._replace(parsed.header, auth_results=(FORGED_AR,))
        assert mailgate.authenticate_header(header).authenticated is False
        gmail_copy = mailindex._replace(header, mailbox="gmail")
        assert mailgate.authenticate_header(gmail_copy).authenticated is True  # the contrast

    def test_a_forged_tuta_mail_mints_nothing_end_to_end(self, tmp_path):
        iban = "DE89 3704 0044 0532 0130 00"
        forged = rfc822(
            frm="Lamp Shop <orders@lampshop.example>", subject="Order LS-48213 payment",
            body=f"Your order number LS-48213: pay 120,00 EUR to {iban}.",
            message_id="forged@lampshop.example", at=T0 + DAY,
            extra=(f"Authentication-Results: {PASS_AR.format(d='lampshop.example')}",),
        )
        runner = FakeHimalaya({"INBOX": [forged], "Sent": []})
        run(tmp_path, [tuta(runner)])
        (record,) = list(mailverdicts.iter_store(tmp_path))
        assert record["gate"]["auth"]["authenticated"] is False
        assert record["gate"]["auth"]["gateway"] == "tuta"
        assert record["gate"]["grade"]["grade"] == mailgate.FIRST_CONTACT
        store = mailcases.CaseStore(tmp_path)
        assert store.cases == {}  # no case opened by an unauthenticated mail
        assert store.admitted_domains((T0 + 3 * DAY, "")) == frozenset()

    def test_an_inbox_mail_claiming_his_address_is_no_act_of_his(self, tmp_path):
        """On Gmail a From naming him decides direction; through Tuta only the
        FOLDER does — so a forged 'From: him, To: attacker' in his inbox
        cannot become an owner act that grades the attacker later."""
        forged = rfc822(frm=f"Owner <{TUTA_OWNER}>", to="attacker@evil.example",
                        subject="hello", message_id="f1@evil.example", at=T0)
        box = tuta(FakeHimalaya({"INBOX": [forged], "Sent": []}))
        later = gmail("g1", frm="attacker@evil.example", subject="Re: hello", at=T0 + DAY,
                      auth="pass", labels=("INBOX",))
        run(tmp_path, [box, FakeMailbox([later])])
        index = mailindex.MailIndex(tmp_path)
        held = next(h for h in index.by_digest.values() if h.mailbox == "tuta")
        assert held.direction == "in"
        assert box.counts.get("inbox_from_an_owner_address") == 1
        graded = {r["provider_id"]: r for r in mailverdicts.iter_store(tmp_path)}["g1"]
        assert graded["gate"]["grade"]["grade"] == mailgate.FIRST_CONTACT
        assert graded["gate"]["grade"]["acts"] == 0

    def test_his_sent_folder_is_his(self, tmp_path):
        out = rfc822(frm=f"Owner <{TUTA_OWNER}>", to="friend@friends.example",
                     subject="Saturday", message_id="s1@tuta.example", at=T0)
        box = tuta(FakeHimalaya({"INBOX": [], "Sent": [out]}))
        run(tmp_path, [box])
        (held,) = mailindex.MailIndex(tmp_path).by_digest.values()
        assert held.direction == "out" and held.mailbox == "tuta"
        assert list(mailverdicts.iter_store(tmp_path)) == []  # his sent mail is never judged

    def test_at_is_sender_written_and_counted(self, tmp_path):
        runner = FakeHimalaya({"INBOX": [rfc822(frm="a@shop.example", message_id="a@x",
                                                at=T0 + DAY)], "Sent": []})
        result = run(tmp_path, [tuta(runner)])
        assert result.sources["tuta"].at_from_sender == 1
        (held,) = mailindex.MailIndex(tmp_path).by_digest.values()
        assert held.at_source == "date_header"


class TestIdentityNotUid:
    def test_a_renumbered_bridge_refetches_nothing_already_held(self, tmp_path):
        msgs = [rfc822(frm="a@shop.example", subject=f"m{i}", message_id=f"m{i}@shop.example",
                       at=T0 + DAY) for i in range(3)]
        runner = FakeHimalaya({"INBOX": msgs, "Sent": []})
        first = run(tmp_path, [tuta(runner)])
        assert first.sources["tuta"].fetched == 3
        runner.renumber(40)  # the bridge restarted: every UID moved, UIDVALIDITY did not
        runner.calls.clear()
        again = run(tmp_path, [tuta(runner)])
        assert again.sources["tuta"].held == 3 and again.sources["tuta"].fetched == 0
        assert not any(a[:2] == ["message", "read"] for a in runner.calls)
        assert again.assessed == 0 and again.already_recorded == 3

    def test_a_tuta_fetch_failure_is_not_written_to_the_ledger(self, tmp_path):
        runner = FakeHimalaya({"INBOX": [rfc822(frm="a@x.example", message_id="a@x")],
                               "Sent": []},
                              fail={("message", "read"): Failure("timeout", matched="60s")})
        result = run(tmp_path, [tuta(runner)])
        assert result.sources["tuta"].fetch_failed == {"timeout": 1}
        assert result.sources["tuta"].unfetched == 0
        assert not (mailindex.mail_dir(tmp_path) / "unfetched.json").read_text().count("tuta:")

    def test_one_mail_in_both_inboxes_is_two_entries_in_one_conversation(self, tmp_path):
        """A non-Gmail digest folds in content (`mailindex.digest_for`), so a
        cross-delivered mail is two entries — the stated cost — joined by
        Message-ID into one conversation."""
        tuta_copy = rfc822(frm="friend@friends.example", subject="Plans", message_id="p@f.example",
                           at=T0 + DAY + 30)
        gmail_copy = gmail("gp", frm="friend@friends.example", subject="Plans",
                           message_id="p@f.example", at=T0 + DAY, labels=("INBOX",))
        result = run(tmp_path, [FakeMailbox([gmail_copy]),
                                tuta(FakeHimalaya({"INBOX": [tuta_copy], "Sent": []}))])
        index = mailindex.MailIndex(tmp_path)
        assert len(index.by_digest) == 2 and result.assessed == 2
        a, b = index.by_digest.values()
        assert index.conversation(a.digest, as_of=(T0 + 3 * DAY, "")) == \
            index.conversation(b.digest, as_of=(T0 + 3 * DAY, ""))


class TestMessageIdSquatting:
    def test_a_tuta_mail_cannot_squat_a_gmail_mails_message_id(self, tmp_path):
        """A Tuta mail carrying a real Gmail mail's Message-ID and an EARLIER
        forged Date must not suppress the Gmail copy's indexing or reading."""
        squatter = rfc822(frm="attacker@evil.example", subject="Invoice", body="pay me",
                          message_id="real@bank.example", at=T0)
        real = gmail("g1", frm="alerts@bank.example", subject="Statement",
                     message_id="real@bank.example", at=T0 + DAY, labels=("INBOX",))
        run(tmp_path, [tuta(FakeHimalaya({"INBOX": [squatter], "Sent": []})),
                       FakeMailbox([real])])
        index = mailindex.MailIndex(tmp_path)
        gmail_held = [h for h in index.by_digest.values() if h.mailbox == "gmail"]
        assert len(gmail_held) == 1 and gmail_held[0].from_addr == "alerts@bank.example"
        assert index.health.collisions == []
        by_pid = {r["provider_id"]: r for r in mailverdicts.iter_store(tmp_path)}
        assert by_pid["g1"]["ending"] == "ok"
        assert by_pid["g1"]["gate"]["auth"]["authenticated"] is True

    def test_a_reply_to_a_squatted_id_is_no_act_toward_the_squatter(self, tmp_path):
        squatter = rfc822(frm="attacker@evil.example", subject="Hi", message_id="x@friends.example",
                          at=T0)
        real = gmail("g1", frm="ania@friends.example", subject="Hi",
                     message_id="x@friends.example", at=T0 + 60, labels=("INBOX",))
        reply = gmail("g2", frm=OWNER, to="ania@friends.example", subject="Re: Hi",
                      in_reply_to="x@friends.example", at=T0 + 120, labels=("SENT",))
        run(tmp_path, [tuta(FakeHimalaya({"INBOX": [squatter], "Sent": []})),
                       FakeMailbox([real, reply])])
        index = mailindex.MailIndex(tmp_path)
        acts = mailgate.owner_acts(index, "attacker@evil.example", (T0 + DAY, ""))
        assert acts == []
        assert index.reply_parent("x@friends.example", (T0 + DAY, "")) is None  # disagree
        assert index.health.ambiguous_parents >= 1

    def test_two_copies_that_agree_on_the_sender_resolve_to_the_gmail_one(self):
        index = mailindex.MailIndex()
        for item in (
            mailindex.from_rfc822(rfc822(frm="ania@friends.example", message_id="y@f", at=T0),
                                  provider_id="t", in_sent_folder=False, mailbox="tuta"),
            mailindex.from_gmail(gmail("g", frm="ania@friends.example", message_id="y@f",
                                       at=T0 + 5), OWNERS),
        ):
            assert item is not None
            index.add(item.header, item.body)
        parent = index.reply_parent("y@f", (T0 + DAY, ""))
        assert parent is not None and parent.mailbox == "gmail"


class TestMalformedDate:
    def test_a_garbage_date_costs_the_date_never_the_assessment(self, tmp_path):
        raw = rfc822(frm="a@shop.example", subject="Hello", message_id="d@shop.example").replace(
            "Date: " + email.utils.formatdate(T0), "Date: not a date at all")
        runner = FakeHimalaya({"INBOX": [raw], "Sent": []})
        result = run(tmp_path, [tuta(runner)])
        assert result.sources["tuta"].unparseable == 0 and result.assessed == 1
        (held,) = mailindex.MailIndex(tmp_path).by_digest.values()
        assert held.at_source == "none"
