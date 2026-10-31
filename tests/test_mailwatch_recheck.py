"""The one-time Tuta authentication recheck (#142 W5, #461 follow-up):
`aish-mailwatch --recheck-tuta-auth` re-fetches Tuta mail indexed before aish
read the bridge's `X-Tutabridge-Authentication` header, records the header
observation onto the EXISTING entry (same digest, no duplicate), and lets the
deterministic consequences follow in arrival order — cases opened or joined,
the authenticated facts on cases, admission by his later acts. No Reader, no
verdict, idempotent, resumable. A fake himalaya runner; no bridge, no process."""

from __future__ import annotations

import json

import pytest
from mail_fixtures import DAY, OWNER, T0, gmail
from test_mailtuta import TUTA_OWNER, FakeHimalaya, rfc822, tuta
from test_mailtuta_auth import GOOD

from aish import mailcases, mailgate, mailindex, mailreader, mailtuta, mailverdicts, mailwatch
from aish.mailsource import Failure, Outcome

NOW = T0 + 10 * DAY
OWNERS = frozenset({OWNER, TUTA_OWNER})
IBAN = "DE89370400440532013000"  # checksum-valid


def shop_mail(at: float, mid: str, *, body: str = "", subject: str = "Order ORD-55501") -> str:
    return rfc822(frm="Shop <shop@shop.example>", subject=subject, at=at, message_id=mid,
                  body=body or f"Your order number ORD-55501: 149,00 PLN to {IBAN}.")


def as_bridge_serves(raw: str, line: str | None = GOOD) -> str:
    return f"{line}\r\n{raw}" if line is not None else raw


def seed_old(state_dir, inbox: list[str], sent: list[str] = (), gmail_msgs=()) -> None:
    """The index and case store as a watcher BEFORE the bridge-header slice left
    them: Tuta mail parsed with no header declared (so no observation), observed
    unauthenticated, in arrival order."""
    index = mailindex.MailIndex(state_dir)
    store = mailcases.CaseStore(state_dir)
    parsed = [mailindex.from_rfc822(r, provider_id=f"tuta:{n}:INBOX", in_sent_folder=False,
                                    mailbox="tuta") for n, r in enumerate(inbox, 1)]
    parsed += [mailindex.from_rfc822(r, provider_id=f"tuta:{n}:Sent", in_sent_folder=True,
                                     mailbox="tuta") for n, r in enumerate(sent, 1)]
    parsed += [mailindex.from_gmail(m, OWNERS) for m in gmail_msgs]
    assert all(p is not None for p in parsed)
    mailwatch.ingest(index, store, parsed)  # type: ignore[arg-type]


def recheck(state_dir, bridge: FakeHimalaya) -> mailwatch.Recheck:
    return mailwatch.recheck_tuta_auth(mailbox=tuta(bridge, now=NOW), state_dir=state_dir,
                                       owner_addresses=OWNERS, now=lambda: NOW)


def tree(state_dir, sub: str) -> dict[str, bytes]:
    root = state_dir / "mail" / sub
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*.json"))}


@pytest.fixture(autouse=True)
def no_reader(monkeypatch):
    calls = []

    def refuse(*a, **k):
        calls.append(a)
        raise AssertionError("the recheck called the Reader")

    monkeypatch.setattr(mailreader, "read", refuse)
    return calls


class TestIdentity:
    def test_the_old_entry_gains_the_observation_under_the_same_digest(self, tmp_path):
        raw = shop_mail(T0, "a@shop.example", body="hello", subject="Hello")
        seed_old(tmp_path, [raw])
        before = tree(tmp_path, "index")
        ((name, old_bytes),) = before.items()
        old = json.loads(old_bytes)
        assert old["gateway_auth_observed"] == ""
        # The digest the bridge's message re-parses to is the stored one: the
        # header line is no input of a Tuta digest.
        served = as_bridge_serves(raw)
        reparsed = mailindex.from_rfc822(served, provider_id="tuta:9:INBOX",
                                         in_sent_folder=False, mailbox="tuta",
                                         first_header=mailtuta.AUTH_HEADER)
        assert reparsed is not None and reparsed.header.digest == old["digest"]

        result = recheck(tmp_path, FakeHimalaya({"INBOX": [served], "Sent": []}))
        after = tree(tmp_path, "index")
        assert list(after) == [name]  # same file, no duplicate entry
        new = json.loads(after[name])
        assert new["digest"] == old["digest"]
        assert new["gateway_auth_observed"] == mailindex.FIRST_AND_ONLY
        changed = {k for k in old if old[k] != new[k]}
        assert changed == {"gateway_auth", "gateway_auth_observed"}
        header = mailindex.MailIndex(tmp_path).get(old["digest"])
        assert header is not None and mailgate.authenticate_header(header).authenticated
        assert (result.needing, result.rechecked, result.now_authenticated) == (1, 1, 1)
        assert result.unmatched == result.failed == result.still_not == 0

    def test_a_message_that_reparses_to_another_digest_is_unmatched_and_untouched(
            self, tmp_path):
        seed_old(tmp_path, [shop_mail(T0, "a@shop.example", body="original body")])
        before = tree(tmp_path, "index")
        # Same Message-ID, sender and subject — the envelope matches — but the
        # content is not what was indexed.
        other = as_bridge_serves(shop_mail(T0, "a@shop.example", body="different body"))
        result = recheck(tmp_path, FakeHimalaya({"INBOX": [other], "Sent": []}))
        assert result.fetched == 1 and result.fetched_not_matching == 1
        assert result.unmatched == 1 and result.rechecked == 0
        assert tree(tmp_path, "index") == before

    def test_an_observation_is_never_overwritten(self, tmp_path):
        raw = shop_mail(T0, "a@shop.example")
        seed_old(tmp_path, [raw])
        recheck(tmp_path, FakeHimalaya({"INBOX": [as_bridge_serves(raw)], "Sent": []}))
        before = tree(tmp_path, "index")
        index = mailindex.MailIndex(tmp_path)
        (digest,) = index.by_digest
        assert not index.record_gateway_auth(digest, "status=hard_fail", "2 copies of it")
        assert tree(tmp_path, "index") == before

    def test_gmail_and_already_observed_tuta_entries_are_not_targets(self, tmp_path):
        seed_old(tmp_path, [], gmail_msgs=[gmail("g1", frm="x@x.example", at=T0)])
        bridge = FakeHimalaya({"INBOX": [], "Sent": []})
        result = recheck(tmp_path, bridge)
        assert result.needing == 0 and bridge.calls == []


class TestConsequences:
    def test_authenticated_mail_opens_a_case_and_joins_it_in_arrival_order(self, tmp_path):
        first, second = shop_mail(T0, "a@shop.example"), shop_mail(T0 + DAY, "b@shop.example")
        seed_old(tmp_path, [first, second])
        assert mailcases.CaseStore(tmp_path).cases == {}  # unauthenticated: no case
        result = recheck(tmp_path, FakeHimalaya({"INBOX": [as_bridge_serves(first),
                                                           as_bridge_serves(second)],
                                                 "Sent": []}))
        index = mailindex.MailIndex(tmp_path)
        a, b = sorted(index.by_digest.values(), key=lambda h: h.position)
        (case,) = mailcases.CaseStore(tmp_path).cases.values()
        assert case.created == a.position and case.created_by == "inbound"
        assert case.mails == [a.position, b.position]
        assert case.domains == {"shop.example": a.position}
        assert {c["at"] for c in case.coordinates} == {a.position, b.position}
        assert sorted(x["at"] for x in case.amounts) == [a.position, b.position]
        assert (result.cases_before, result.cases_after) == (0, 1)
        assert result.observed == 2 and result.now_authenticated == 2

    def test_a_member_gains_its_sighting_and_his_later_reply_admits_the_sender(
            self, tmp_path):
        # He wrote first (an owner case), the shop answered through Tuta
        # (joined unauthenticated: no domain, no amount, no account), then he
        # replied to the shop. Had the shop's mail been authenticated, his reply
        # would have admitted shop.example.
        his_first = gmail("o1", frm=OWNER, to="shop@shop.example", subject="Order ORD-55501",
                          body="About my order number ORD-55501", at=T0, labels=("SENT",))
        answer = shop_mail(T0 + DAY, "a@shop.example")
        his_reply = gmail("o2", frm=OWNER, to="shop@shop.example",
                          subject="Re: Order ORD-55501", body="Thanks", at=T0 + 2 * DAY,
                          labels=("SENT",), in_reply_to="a@shop.example")
        seed_old(tmp_path, [answer], gmail_msgs=[his_first, his_reply])
        (case,) = mailcases.CaseStore(tmp_path).cases.values()
        assert case.domains == {} and case.admitted == {} and len(case.mails) == 3

        result = recheck(tmp_path, FakeHimalaya({"INBOX": [as_bridge_serves(answer)],
                                                 "Sent": []}))
        (case,) = mailcases.CaseStore(tmp_path).cases.values()
        index = mailindex.MailIndex(tmp_path)
        shop = next(h for h in index.by_digest.values() if h.mailbox == "tuta")
        reply = next(h for h in index.by_digest.values() if h.provider_id == "o2")
        assert case.domains == {"shop.example": shop.position}
        assert [c["at"] for c in case.coordinates] == [shop.position]
        assert case.admitted == {"shop.example": {"at": reply.position,
                                                  "by": "owner_act_toward_sender"}}
        assert result.gained_facts == 1 and result.admitted == 1 and result.observed == 0

    def test_later_mail_on_no_case_joins_the_case_the_recheck_opened(self, tmp_path):
        opener = shop_mail(T0, "a@shop.example")
        later = gmail("g1", frm="Courier <c@courier.example>", subject="Parcel",
                      body="Your order number ORD-55501 is on its way", at=T0 + DAY,
                      auth=None)  # unauthenticated: opened nothing on its own
        earlier = gmail("g0", frm="Courier <c@courier.example>", subject="Parcel",
                        body="Your order number ORD-55501 is coming", at=T0 - DAY, auth=None)
        seed_old(tmp_path, [opener], gmail_msgs=[later, earlier])
        recheck(tmp_path, FakeHimalaya({"INBOX": [as_bridge_serves(opener)], "Sent": []}))
        (case,) = mailcases.CaseStore(tmp_path).cases.values()
        index = mailindex.MailIndex(tmp_path)
        joined = {index.get(d).provider_id for _at, d in case.mails}  # type: ignore[union-attr]
        # The later mail joins; the EARLIER one does not — at its position the
        # case did not exist.
        assert joined == {"tuta:1:INBOX", "g1"}
        assert "c@courier.example" in case.parties
        assert "courier.example" not in case.domains  # it is still unauthenticated

    def test_a_forged_or_second_header_stays_unauthenticated(self, tmp_path):
        raw = shop_mail(T0, "a@shop.example")
        seed_old(tmp_path, [raw])
        forged = as_bridge_serves(raw.replace("Subject:", GOOD + "\r\nSubject:", 1))
        result = recheck(tmp_path, FakeHimalaya({"INBOX": [forged], "Sent": []}))
        assert result.rechecked == 1 and result.still_not == 1
        assert result.now_authenticated == 0
        (why,) = result.still_not_why
        assert "2 copies" in why
        (header,) = mailindex.MailIndex(tmp_path).by_digest.values()
        assert "2 copies" in header.gateway_auth_observed
        assert not mailgate.authenticate_header(header).authenticated
        assert mailcases.CaseStore(tmp_path).cases == {}

    def test_a_message_served_with_no_header_records_that_and_is_final(self, tmp_path):
        raw = shop_mail(T0, "a@shop.example")
        seed_old(tmp_path, [raw])
        bridge = FakeHimalaya({"INBOX": [raw], "Sent": []})  # no bridge line
        result = recheck(tmp_path, bridge)
        assert result.rechecked == 1 and result.still_not == 1
        (header,) = mailindex.MailIndex(tmp_path).by_digest.values()
        assert header.gateway_auth_observed == f"no {mailtuta.AUTH_HEADER} header"
        assert not mailgate.authenticate_header(header).authenticated
        assert mailcases.CaseStore(tmp_path).cases == {}
        bridge.calls.clear()
        assert recheck(tmp_path, bridge).needing == 0 and bridge.calls == []

    def test_an_older_mail_joins_a_case_a_later_mail_opened(self, tmp_path):
        """The stated limit, pinned: observation matches keys over the whole
        store, so the older mail joins as a member; the case keeps the later
        creation position, and the as-of key view does not see the key before it."""
        old = shop_mail(T0, "a@shop.example")
        opener = gmail("g5", frm="shop@shop.example", subject="Order ORD-55501",
                       body="order number ORD-55501 confirmed", at=T0 + 5 * DAY)
        seed_old(tmp_path, [old], gmail_msgs=[opener])
        (before,) = mailcases.CaseStore(tmp_path).cases.values()
        recheck(tmp_path, FakeHimalaya({"INBOX": [as_bridge_serves(old)], "Sent": []}))
        (case,) = mailcases.CaseStore(tmp_path).cases.values()
        tuta_mail = next(h for h in mailindex.MailIndex(tmp_path).by_digest.values()
                         if h.mailbox == "tuta")
        assert case.id == before.id and case.created == before.created
        assert tuta_mail.position in case.mails
        assert "ORD-55501" not in case.keys_before(tuta_mail.position)

    def test_a_bridge_status_other_than_authenticated_mints_nothing(self, tmp_path):
        raw = shop_mail(T0, "a@shop.example")
        seed_old(tmp_path, [raw])
        line = "X-Tutabridge-Authentication: status=hard_fail; status-source=tuta-server"
        result = recheck(tmp_path, FakeHimalaya({"INBOX": [as_bridge_serves(raw, line)],
                                                 "Sent": []}))
        assert result.still_not == 1 and mailcases.CaseStore(tmp_path).cases == {}

    def test_mail_he_moved_is_not_observed_again(self, tmp_path):
        his = gmail("o1", frm=OWNER, to="shop@shop.example", subject="Order ORD-55501",
                    body="About my order number ORD-55501", at=T0, labels=("SENT",))
        answer = shop_mail(T0 + DAY, "a@shop.example")
        seed_old(tmp_path, [answer], gmail_msgs=[his])
        store = mailcases.CaseStore(tmp_path)
        (case,) = store.cases.values()
        other = store.act("create", {"title": "Elsewhere"}, by="owner", now=NOW)["case"]
        moved = next(d for _at, d in case.mails
                     if mailindex.MailIndex(tmp_path).get(d).mailbox == "tuta")  # type: ignore[union-attr]
        mailcases.CaseStore(tmp_path).act("move", {"case": case.id, "into": other,
                                                    "mail": moved}, by="owner", now=NOW)
        recheck(tmp_path, FakeHimalaya({"INBOX": [as_bridge_serves(answer)], "Sent": []}))
        cases = mailcases.CaseStore(tmp_path).cases
        assert all(p[1] != moved for p in cases[case.id].mails)  # his move stands
        assert any(p[1] == moved for p in cases[other].mails)
        assert "shop.example" in cases[other].domains  # its sighting is where he put it


class TestRunDiscipline:
    def test_no_reader_no_verdict(self, tmp_path, no_reader):
        raw = shop_mail(T0, "a@shop.example")
        seed_old(tmp_path, [raw])
        recheck(tmp_path, FakeHimalaya({"INBOX": [as_bridge_serves(raw)], "Sent": []}))
        assert no_reader == []
        assert list(mailverdicts.iter_store(tmp_path)) == []

    def test_only_fenced_reads_and_never_seen(self, tmp_path):
        raw = shop_mail(T0, "a@shop.example")
        seed_old(tmp_path, [raw])
        bridge = FakeHimalaya({"INBOX": [as_bridge_serves(raw)], "Sent": []})
        recheck(tmp_path, bridge)
        assert bridge.calls
        for argv in bridge.calls:
            mailtuta.check_argv(argv)  # raises on anything outside the allowlist
            assert not any("seen" in token.lower() for token in argv)

    def test_a_second_run_is_a_no_op(self, tmp_path):
        first, second = shop_mail(T0, "a@shop.example"), shop_mail(T0 + DAY, "b@shop.example")
        seed_old(tmp_path, [first, second])
        bridge = FakeHimalaya({"INBOX": [as_bridge_serves(first), as_bridge_serves(second)],
                               "Sent": []})
        recheck(tmp_path, bridge)
        index, cases = tree(tmp_path, "index"), tree(tmp_path, "cases")
        bridge.calls.clear()
        again = recheck(tmp_path, bridge)
        assert again.needing == 0 and bridge.calls == []  # nothing listed, nothing fetched
        assert (tree(tmp_path, "index"), tree(tmp_path, "cases")) == (index, cases)

    def test_a_failed_fetch_is_counted_and_finished_by_the_next_run(self, tmp_path):
        raw = shop_mail(T0, "a@shop.example")
        seed_old(tmp_path, [raw])
        before = tree(tmp_path, "index")
        broken = FakeHimalaya({"INBOX": [as_bridge_serves(raw)], "Sent": []},
                              fail={("message", "read"): Failure("transient", matched="x")})
        result = recheck(tmp_path, broken)
        assert result.failed == 1 and result.unmatched == 0 and result.rechecked == 0
        assert tree(tmp_path, "index") == before
        again = recheck(tmp_path, FakeHimalaya({"INBOX": [as_bridge_serves(raw)], "Sent": []}))
        assert again.rechecked == 1 and again.now_authenticated == 1

    def test_an_interrupted_run_ends_where_an_uninterrupted_one_does(
            self, tmp_path, monkeypatch):
        mails = [shop_mail(T0, "a@shop.example"), shop_mail(T0 + DAY, "b@shop.example")]
        served = {"INBOX": [as_bridge_serves(m) for m in mails], "Sent": []}
        clean, broken = tmp_path / "clean", tmp_path / "broken"
        for where in (clean, broken):
            seed_old(where, mails)
        recheck(clean, FakeHimalaya(served))
        real = mailindex.MailIndex.record_gateway_auth

        def crash(self, *a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(mailindex.MailIndex, "record_gateway_auth", crash)
        with pytest.raises(KeyboardInterrupt):
            recheck(broken, FakeHimalaya(served))
        monkeypatch.setattr(mailindex.MailIndex, "record_gateway_auth", real)
        recheck(broken, FakeHimalaya(served))
        assert tree(broken, "cases") == tree(clean, "cases")
        assert tree(broken, "index") == tree(clean, "index")

    @staticmethod
    def _member_and_opener(where):
        """A Tuta answer joined his case unauthenticated (gains a sighting, his
        later reply then admits it), and a Tuta opener on no case that a later
        unauthenticated courier mail then joins: every case step there is."""
        his = gmail("o1", frm=OWNER, to="shop@shop.example", subject="Order ORD-55501",
                    body="About my order number ORD-55501", at=T0, labels=("SENT",))
        answer = shop_mail(T0 + DAY, "a@shop.example")
        reply = gmail("o2", frm=OWNER, to="shop@shop.example", subject="Re: Order ORD-55501",
                      body="Thanks", at=T0 + 2 * DAY, labels=("SENT",),
                      in_reply_to="a@shop.example")
        opener = shop_mail(T0 + 3 * DAY, "c@other.example", subject="Booking BK-77001",
                           body="Your booking reference BK-77001 is confirmed")
        courier = gmail("g1", frm="c@courier.example", subject="Booking",
                        body="booking reference BK-77001 updated", at=T0 + 4 * DAY, auth=None)
        seed_old(where, [answer, opener], gmail_msgs=[his, reply, courier])
        return {"INBOX": [as_bridge_serves(answer), as_bridge_serves(opener)], "Sent": []}

    def test_a_crash_at_any_case_write_is_finished_by_the_next_run(
            self, tmp_path, monkeypatch):
        clean = tmp_path / "clean"
        served = self._member_and_opener(clean)
        recheck(clean, FakeHimalaya(served))
        (case_clean,) = [c for c in mailcases.CaseStore(clean).cases.values()
                         if "ORD-55501" in c.keys]
        assert "shop.example" in case_clean.admitted  # the scenario reaches admission
        real = mailcases.CaseStore._save
        writes = []
        monkeypatch.setattr(mailcases.CaseStore, "_save",
                            lambda self, case: (writes.append(1), real(self, case)))
        probe = tmp_path / "probe"
        self._member_and_opener(probe)
        writes.clear()
        recheck(probe, FakeHimalaya(served))
        total = len(writes)
        assert total >= 3
        for crash_at in range(total):
            where = tmp_path / f"crash{crash_at}"
            self._member_and_opener(where)
            count = [0]

            def save(self, case, count=count, crash_at=crash_at):
                if count[0] == crash_at:
                    raise KeyboardInterrupt
                count[0] += 1
                real(self, case)

            monkeypatch.setattr(mailcases.CaseStore, "_save", save)
            with pytest.raises(KeyboardInterrupt):
                recheck(where, FakeHimalaya(served))
            monkeypatch.setattr(mailcases.CaseStore, "_save", real)
            recheck(where, FakeHimalaya(served))
            assert tree(where, "cases") == tree(clean, "cases"), crash_at
            assert tree(where, "index") == tree(clean, "index"), crash_at

    def test_a_failed_earlier_fetch_defers_the_later_ones(self, tmp_path):
        early, late = shop_mail(T0, "a@shop.example"), shop_mail(T0 + DAY, "b@shop.example")
        seed_old(tmp_path, [early, late])
        before = tree(tmp_path, "index")

        class FailFirstRead(FakeHimalaya):
            def __call__(self, args, env):
                if tuple(args[:2]) == ("message", "read") and args[-1] == "1":
                    self.calls.append(list(args))
                    return Outcome(failure=Failure("transient", matched="x"))
                return super().__call__(args, env)

        served = {"INBOX": [as_bridge_serves(early), as_bridge_serves(late)], "Sent": []}
        result = recheck(tmp_path, FailFirstRead(served))
        assert result.failed == 1 and result.deferred == 1 and result.rechecked == 0
        assert tree(tmp_path, "index") == before
        assert mailcases.CaseStore(tmp_path).cases == {}
        clean = tmp_path / "clean"
        seed_old(clean, [early, late])
        recheck(clean, FakeHimalaya(served))
        again = recheck(tmp_path, FakeHimalaya(served))
        assert again.rechecked == 2
        assert tree(tmp_path, "cases") == tree(clean, "cases")

    def test_an_unmatched_entry_stays_a_target(self, tmp_path):
        seed_old(tmp_path, [shop_mail(T0, "a@shop.example")])
        bridge = FakeHimalaya({"INBOX": [], "Sent": []})  # he deleted it
        assert recheck(tmp_path, bridge).unmatched == 1
        bridge.calls.clear()
        again = recheck(tmp_path, bridge)
        assert again.needing == 1 and again.unmatched == 1 and bridge.calls

    def test_a_failed_listing_touches_nothing(self, tmp_path):
        seed_old(tmp_path, [shop_mail(T0, "a@shop.example")])
        before = tree(tmp_path, "index")
        result = recheck(tmp_path, FakeHimalaya(
            {"INBOX": [], "Sent": []}, fail={("imap", "list"): Failure("transient", matched="x")}))
        assert not result.listing_ok and result.unmatched == 1
        assert tree(tmp_path, "index") == before


class TestTheCommand:
    def test_it_needs_no_gmail_and_no_model_and_takes_the_lock(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        raw = shop_mail(T0, "a@shop.example")
        seed_old(tmp_path, [raw])
        bridge = FakeHimalaya({"INBOX": [as_bridge_serves(raw)], "Sent": []})
        monkeypatch.setattr(mailwatch, "tuta_env", lambda: ({mailtuta.SECRET_ENV: "x"}, ""))
        box = tuta(bridge, now=NOW)
        monkeypatch.setattr(mailtuta, "TutaMailbox", lambda env: box)
        monkeypatch.setattr(mailwatch, "load_reader",
                            lambda: (_ for _ in ()).throw(AssertionError("loaded a Reader")))
        assert mailwatch.main(["--recheck-tuta-auth"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["rechecked"] == 1 and out["now_authenticated"] == 1
        assert out["cases_before"] == 0 and out["cases_after"] == 1
        kinds = [json.loads(line)["step"]["kind"]
                 for p in (tmp_path / "mail").glob("runs-*.jsonl")
                 for line in p.read_text().splitlines()]
        assert "mail_tuta_recheck" in kinds
        with mailwatch.pass_lock(tmp_path) as held:
            assert held
            assert mailwatch.main(["--recheck-tuta-auth"]) == 3
