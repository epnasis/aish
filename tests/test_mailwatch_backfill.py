"""The live backfill (#142 W5): `aish-mailwatch --backfill DAYS` — one
deterministic pass over INBOX and Sent of both inboxes: index + case
observation only. NO Reader call, no verdict, the watcher's unfetched ledger
untouched, idempotent, resumable. Fakes only."""

from __future__ import annotations

import json

import pytest
from mail_fixtures import DAY, OWNER, T0, FakeMailbox, gmail
from test_mailtuta import TUTA_OWNER, FakeHimalaya, rfc822, tuta

from aish import mailcases, mailindex, mailreader, mailreport, mailsource, mailverdicts, mailwatch

NOW = T0 + 40 * DAY
OWNERS = frozenset({OWNER, TUTA_OWNER})


def fill(state_dir, boxes, days=60):
    return mailwatch.backfill(mailboxes=boxes, state_dir=state_dir, owner_addresses=OWNERS,
                              days=days, now=lambda: NOW)


def history():
    return [
        gmail("o1", frm=OWNER, to="shop@shop.example", subject="Order SH-90210",
              body="About my order number SH-90210", at=T0, labels=("SENT",)),
        gmail("i1", frm="Shop <shop@shop.example>", subject="Re: Order SH-90210",
              body="Your order number SH-90210 ships today.", at=T0 + DAY,
              in_reply_to="o1@mail.example"),
        gmail("o2", frm=OWNER, to="friend@friends.example", subject="Saturday?",
              body="Are we on?", at=T0 + 2 * DAY, labels=("SENT",)),
    ]


@pytest.fixture(autouse=True)
def no_reader(monkeypatch):
    calls = []

    def refuse(*a, **k):
        calls.append(a)
        raise AssertionError("the backfill called the Reader")

    monkeypatch.setattr(mailreader, "read", refuse)
    return calls


class TestTheBackfill:
    def test_it_indexes_and_observes_both_inboxes_with_no_reader(self, tmp_path, no_reader):
        tuta_sent = rfc822(frm=f"Owner <{TUTA_OWNER}>", to="agent@insure.example",
                           subject="Policy renewal", body="Policy question", at=T0 + 3 * DAY,
                           message_id="t1@tuta.example")
        boxes = [FakeMailbox(history()), tuta(FakeHimalaya({"INBOX": [], "Sent": [tuta_sent]}),
                                              now=NOW)]
        result = fill(tmp_path, boxes)
        assert no_reader == []
        assert result.indexed == 4 and result.observed == 4
        index = mailindex.MailIndex(tmp_path)
        assert sum(1 for h in index.by_digest.values() if h.direction == "out") == 3
        (case,) = mailcases.CaseStore(tmp_path).cases.values()
        assert case.created_by == "owner" and "SH-90210" in case.keys
        assert len(case.mails) == 2
        assert list(mailverdicts.iter_store(tmp_path)) == []  # no verdict recorded

    def test_it_is_idempotent(self, tmp_path):
        box = FakeMailbox(history())
        fill(tmp_path, [box])
        before = sorted(p.read_bytes() for p in (tmp_path / "mail" / "cases").glob("*.json"))
        box.calls.clear()
        again = fill(tmp_path, [box])
        assert again.indexed == 0
        assert [c for c in box.calls if c[0] == "get"] == []  # nothing fetched twice
        after = sorted(p.read_bytes() for p in (tmp_path / "mail" / "cases").glob("*.json"))
        assert before == after

    def test_an_interrupted_run_is_finished_by_the_next(self, tmp_path, monkeypatch):
        box = FakeMailbox(history())
        real = mailcases.CaseStore.observe

        def crash(self, *a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(mailcases.CaseStore, "observe", crash)
        with pytest.raises(KeyboardInterrupt):
            fill(tmp_path, [box])
        assert len(mailindex.MailIndex(tmp_path).by_digest) == 3  # kept what it fetched
        monkeypatch.setattr(mailcases.CaseStore, "observe", real)
        box.calls.clear()
        result = fill(tmp_path, [box])
        assert result.indexed == 0 and result.observed == 3
        assert [c for c in box.calls if c[0] == "get"] == []
        assert len(mailcases.CaseStore(tmp_path).cases) == 1

    def test_the_watchers_ledger_is_neither_read_nor_written(self, tmp_path):
        ledger = mailsource.UnfetchedLedger(tmp_path)
        ledger.missed("gmail", "owed", "inbox",
                      mailsource.Failure(mailsource.TRANSIENT, matched="x"), T0)
        ledger.save()
        before = ledger.path.read_bytes()
        box = FakeMailbox(history())
        fill(tmp_path, [box])
        assert ("get", "owed") not in box.calls
        assert ledger.path.read_bytes() == before

    def test_a_failed_listing_is_reported_and_the_other_inbox_still_runs(self, tmp_path):
        result = fill(tmp_path, [FakeMailbox([], fail_listing=True),
                                 tuta(FakeHimalaya({"INBOX": [], "Sent": []}), now=NOW)])
        assert not result.listing_ok
        assert not result.sources["gmail"].listing_ok and result.sources["tuta"].listing_ok

    def test_mail_outside_the_window_is_not_observed(self, tmp_path):
        old = gmail("old", frm=OWNER, to="x@shop.example", subject="Order OLD-12345",
                    body="order number OLD-12345", at=NOW - 90 * DAY, labels=("SENT",))
        result = fill(tmp_path, [FakeMailbox([old])])
        assert result.indexed == 1 and result.observed == 0

    def test_followups_then_have_sent_mail_to_work_from(self, tmp_path):
        fill(tmp_path, [FakeMailbox(history())])
        report = mailreport.build(tmp_path, now=NOW, since=None, clean=lambda s: s)
        assert report.sent_in_window == 2
        assert any(f.counterparty == "friend@friends.example" for f in report.theirs)


class TestTheCommand:
    def test_backfill_needs_no_model_and_takes_the_lock(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        accounts = tmp_path / "accounts"
        (accounts / OWNER).mkdir(parents=True)
        monkeypatch.setenv("GWS_ACCOUNTS_ROOT", str(accounts))
        box = FakeMailbox(history())
        monkeypatch.setattr(mailwatch, "GwsMailbox", lambda env: box)
        monkeypatch.setattr(mailwatch, "load_reader",
                            lambda: (_ for _ in ()).throw(AssertionError("loaded a Reader")))
        assert mailwatch.main(["--backfill", "60", "--account", OWNER]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["days"] == 60 and out["indexed"] == 3
        runs = list((tmp_path / "mail").glob("runs-*.jsonl"))
        kinds = [json.loads(line)["step"]["kind"]
                 for p in runs for line in p.read_text().splitlines()]
        assert "mail_backfill" in kinds
        with mailwatch.pass_lock(tmp_path) as held:
            assert held
            assert mailwatch.main(["--backfill", "60", "--account", OWNER]) == 3
