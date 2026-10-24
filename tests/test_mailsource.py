"""Gmail quota discipline and the top-up (#142 W5 S1.5): typed failures at the
runner seam, the one retry policy in `GwsMailbox._call`, the token bucket,
the listed-but-unfetched ledger, and the Gmail-only skip of a fetch the index
already holds. Fake runners and fake sleeps; no process, no network."""

from __future__ import annotations

import json

import pytest
from mail_fixtures import DAY, T0, FakeMailbox, ScriptedReader, gmail, verdict_json
from test_mailwatch import run

from aish import mailindex, mailsource, mailverdicts, mailwatch
from aish.mailsource import Failure, Outcome, TokenBucket, UnfetchedLedger, classify_gws

QUOTA_429 = json.dumps({"error": {"code": 429, "message": (
    "Quota exceeded for quota metric 'Queries' and limit 'Queries per minute per user'"),
    "status": "RESOURCE_EXHAUSTED"}})
UNITS = "Error: Rate Limit Exceeded: Quota exceeded for quota metric 'Units per minute'"
PERM_403 = json.dumps({"error": {"code": 403, "message": (
    "Request had insufficient authentication scopes."), "status": "PERMISSION_DENIED"}})
RATE_403 = json.dumps({"error": {"code": 403, "message": "User Rate Limit Exceeded",
                                 "errors": [{"reason": "userRateLimitExceeded"}]}})
DAILY = json.dumps({"error": {"code": 429, "message": "Queries per day exceeded"}})


class TestClassification:
    @pytest.mark.parametrize("stderr, kind, retry", [
        (QUOTA_429, "quota", True),
        (UNITS, "quota", True),
        (RATE_403, "quota", True),
        (DAILY, "quota", False),
        (PERM_403, "permanent", False),
        (json.dumps({"error": {"code": 404, "message": "Not Found"}}), "permanent", False),
        (json.dumps({"error": {"code": 500, "message": "Backend Error"}}), "transient", True),
        ("connection reset by peer", "transient", True),
        ("something odd", "unknown", False),
    ])
    def test_a_gws_exit_is_typed(self, stderr, kind, retry):
        failure = classify_gws(1, stderr)
        assert failure.kind == kind and failure.retryable is retry
        assert failure.matched  # what the verdict rests on is recorded

    def test_the_closed_set_is_closed(self):
        with pytest.raises(ValueError):
            Failure("maybe")


def mailbox(outcomes):
    """A GwsMailbox whose runner replays `outcomes` and whose sleeps are
    recorded, with a bucket that never waits."""
    calls, sleeps = [], []
    script = list(outcomes)

    def runner(args, env):
        calls.append(args)
        return script.pop(0)

    bucket = TokenBucket(rate=1e9, burst=1e9, clock=lambda: 0.0, sleep=sleeps.append)
    box = mailwatch.GwsMailbox({}, runner, bucket=bucket, sleep=sleeps.append)
    return box, calls, sleeps


def quota():
    return Outcome(failure=classify_gws(1, UNITS))


class TestRetryPolicy:
    def test_units_per_minute_is_retried_with_backoff(self):
        box, calls, sleeps = mailbox([quota(), quota(), Outcome({"id": "x"})])
        assert box.get("x") == {"id": "x"}
        assert len(calls) == 3 and sleeps == [10.0, 20.0]
        assert box.failures == {"quota": 2} and not box.quota_exhausted

    def test_a_permanent_403_is_never_retried(self):
        box, calls, sleeps = mailbox([Outcome(failure=classify_gws(1, PERM_403))])
        assert box.get("x") is None
        assert len(calls) == 1 and sleeps == []
        assert box.last_failure is not None and box.last_failure.kind == "permanent"

    def test_a_daily_quota_is_not_waited_out(self):
        box, calls, sleeps = mailbox([Outcome(failure=classify_gws(1, DAILY))])
        assert box.get("x") is None and len(calls) == 1 and sleeps == []
        assert box.quota_exhausted

    def test_a_quota_that_outlasts_the_retries_stops_the_pass_asking(self):
        box, calls, _sleeps = mailbox([quota()] * mailsource.MAX_ATTEMPTS["quota"])
        assert box.get("x") is None and box.quota_exhausted
        source = mailsource.GmailSource(box)
        item, failure = source.fetch("y", frozenset())
        assert item is None and failure is not None and failure.kind == "quota"
        assert len(calls) == mailsource.MAX_ATTEMPTS["quota"]  # "y" cost nothing

    def test_an_unknown_failure_is_not_retried(self):
        box, calls, _ = mailbox([Outcome(failure=classify_gws(1, "something odd"))])
        assert box.get("x") is None and len(calls) == 1

    def test_the_default_runner_types_its_failures(self, monkeypatch):
        import subprocess

        class Done:
            returncode, stdout, stderr = 1, "", UNITS

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
        outcome = mailwatch.default_runner(["gmail"], {})
        assert outcome.failure is not None and outcome.failure.kind == "quota"

        def slow(*a, **k):
            raise subprocess.TimeoutExpired("gws", 60)

        monkeypatch.setattr(subprocess, "run", slow)
        assert mailwatch.default_runner(["gmail"], {}).failure.kind == "timeout"


class TestTokenBucket:
    def test_it_paces_past_the_burst(self):
        clock = [0.0]
        sleeps: list[float] = []

        def sleep(s):
            sleeps.append(s)
            clock[0] += s

        bucket = TokenBucket(rate=2.0, burst=2.0, clock=lambda: clock[0], sleep=sleep)
        for _ in range(4):
            bucket.take()
        assert sleeps == [0.5, 0.5]

    def test_every_gws_call_takes_a_token(self):
        taken = []

        class Counting(TokenBucket):
            def take(self):
                taken.append(1)

        box = mailwatch.GwsMailbox({}, lambda a, e: Outcome({"messages": []}),
                                   bucket=Counting())
        box.list("in:inbox", 10)
        box.get("x")
        assert len(taken) == 2


class FlakyMailbox(FakeMailbox):
    """A FakeMailbox whose `get` fails, typed, for the ids in `failing`."""

    def __init__(self, messages, failing=(), kind="quota", listed=None):
        super().__init__(messages)
        self.failing = set(failing)
        self.kind = kind
        self.listed = listed
        self.last_failure = None

    def list(self, query, limit, include_trash=False):
        ids = super().list(query, limit, include_trash)
        return ids if self.listed is None or ids is None else [i for i in ids if i in self.listed]

    def get(self, gid):
        if gid in self.failing:
            self.calls.append(("get", gid))
            self.last_failure = Failure(self.kind, matched="scripted")
            return None
        return super().get(gid)


def three():
    return [
        gmail("a", frm="a@shop.example", subject="A", at=T0, labels=("INBOX",)),
        gmail("b", frm="b@shop.example", subject="B", at=T0 + 60, labels=("INBOX",)),
        gmail("c", frm="c@shop.example", subject="C", at=T0 + 120, labels=("INBOX",)),
    ]


def gets(box):
    return [c[1] for c in box.calls if c[0] == "get"]


class TestHeldSkip:
    def test_a_second_pass_fetches_nothing_the_index_holds(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        run(tmp_path, FakeMailbox(three()), reader)
        box = FakeMailbox(three())
        again = run(tmp_path, box, reader)
        assert gets(box) == [] and again.sources["gmail"].held == 3
        assert again.already_recorded == 3

    def test_a_held_mail_without_a_final_verdict_is_still_read(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        first = run(tmp_path, FakeMailbox(three()), reader, admitted=False)
        assert first.endings == {"unadmitted": 3}
        box = FakeMailbox(three())
        second = run(tmp_path, box, reader)
        assert gets(box) == [] and second.assessed == 3 and reader.calls == 3


class TestTopUp:
    def test_a_missed_fetch_is_fetched_exactly_once_later(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        first = run(tmp_path, FlakyMailbox(three(), failing={"b"}), reader)
        assert first.sources["gmail"].fetch_failed == {"quota": 1}
        ledger = UnfetchedLedger(tmp_path)
        assert ledger.pending("gmail") == [("b", "inbox")]
        # "b" has left the listing's window; only the ledger remembers it.
        box = FlakyMailbox(three(), listed={"a", "c"})
        second = run(tmp_path, box, reader)
        assert gets(box) == ["b"]
        assert second.sources["gmail"].topped_up == 1 and second.sources["gmail"].unfetched == 0
        assert {r["provider_id"] for r in mailverdicts.iter_store(tmp_path)} == {"a", "b", "c"}
        assert UnfetchedLedger(tmp_path).pending("gmail") == []

    def test_a_permanent_failure_is_abandoned_not_retried_forever(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        result = run(tmp_path, FlakyMailbox(three(), failing={"b"}, kind="permanent"), reader)
        assert result.sources["gmail"].abandoned == 1
        assert UnfetchedLedger(tmp_path).pending("gmail") == []

    def test_the_ledger_counts_reach_the_health_file(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        result = run(tmp_path, FlakyMailbox(three(), failing={"b", "c"}), reader)
        state = mailwatch.record_health(tmp_path, result, T0)
        assert state["sources"]["gmail"]["unfetched"] == 2

    def test_the_ledger_survives_an_unreadable_file(self, tmp_path):
        path = mailindex.mail_dir(tmp_path) / mailsource.LEDGER_NAME
        path.parent.mkdir(parents=True)
        path.write_text("{garbage")
        assert UnfetchedLedger(tmp_path).pending("gmail") == []


class TestPassLock:
    def test_a_second_pass_does_not_run_beside_the_first(self, tmp_path):
        with mailwatch.pass_lock(tmp_path) as first:
            assert first is True
            with mailwatch.pass_lock(tmp_path) as second:
                assert second is False
        with mailwatch.pass_lock(tmp_path) as again:
            assert again is True


class TestHealthPerSource:
    def test_each_inbox_has_its_own_streak(self, tmp_path):
        failed = mailwatch.Pass(listing_ok=False, sources={
            "gmail": mailwatch.SourcePass(),
            "tuta": mailwatch.SourcePass(listing_ok=False, failure="timeout (60s)")})
        mailwatch.record_health(tmp_path, failed, T0)
        state = mailwatch.record_health(tmp_path, failed, T0 + DAY)
        assert state["failed_listings"] == 2
        assert state["sources"]["tuta"]["failed_listings"] == 2
        assert state["sources"]["gmail"]["failed_listings"] == 0
        assert state["sources"]["gmail"]["last_ok_at"] == T0 + DAY

    def test_an_inbox_not_read_this_pass_is_marked_not_current(self, tmp_path):
        both = mailwatch.Pass(sources={"gmail": mailwatch.SourcePass(),
                                       "tuta": mailwatch.SourcePass(listing_ok=False)})
        mailwatch.record_health(tmp_path, both, T0)
        only_gmail = mailwatch.Pass(sources={"gmail": mailwatch.SourcePass()})
        state = mailwatch.record_health(tmp_path, only_gmail, T0 + 60)
        assert state["sources"]["tuta"]["last_run_at"] == T0
        assert state["sources"]["gmail"]["last_run_at"] == T0 + 60

    def test_unparseable_mail_reaches_the_health_file(self, tmp_path):
        result = mailwatch.Pass(sources={"gmail": mailwatch.SourcePass(unparseable=2)})
        state = mailwatch.record_health(tmp_path, result, T0)
        assert state["sources"]["gmail"]["unparseable"] == 2


class TestGmailDirection:
    def test_a_forged_from_him_in_his_inbox_is_inbound_no_act_and_read(self, tmp_path):
        """Delivery review (blocking): on Gmail a From header naming him used
        to make a mail his — minting acts toward its recipient, opening
        owner-created cases, and skipping assessment. Only SENT is his now."""
        from mail_fixtures import OWNER

        from aish import mailcases, mailgate

        forged = gmail("f", frm=f"Owner <{OWNER}>", to="attacker@evil.example",
                       subject="Order LS-48213", body="Order LS-48213 confirmed.",
                       at=T0, labels=("INBOX",), auth="fail")
        later = gmail("l", frm="attacker@evil.example", subject="Re: Order LS-48213",
                      at=T0 + 40 * DAY, labels=("INBOX",), auth="pass")
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        result = run(tmp_path, FakeMailbox([forged, later]), reader)
        index = mailindex.MailIndex(tmp_path)
        held = next(h for h in index.by_digest.values() if h.provider_id == "f")
        assert held.direction == "in"
        assert mailgate.owner_acts(index, "attacker@evil.example", (T0 + 41 * DAY, "")) == []
        records = {r["provider_id"]: r for r in mailverdicts.iter_store(tmp_path)}
        assert "f" in records  # assessed like any inbound mail
        assert records["l"]["gate"]["grade"]["grade"] == mailgate.FIRST_CONTACT
        # The forged mail opened no OWNER case (the attacker's own authenticated
        # mail may open an inbound one — S1's rule, unchanged).
        cases = mailcases.CaseStore(tmp_path).cases.values()
        assert not [c for c in cases if c.created_by == "owner"]
        assert result.sources["gmail"].counts == {"inbox_from_an_owner_address": 1}


class TestExhaustedThisPass:
    def test_a_refusal_never_sent_spends_no_ledger_attempt(self, tmp_path):
        ledger = UnfetchedLedger(tmp_path)
        ledger.missed("gmail", "x", "inbox", Failure("quota", sent=False), T0)
        assert ledger.entries["gmail"]["x"]["attempts"] == 0
        ledger.missed("gmail", "x", "inbox", Failure("quota"), T0)
        assert ledger.entries["gmail"]["x"]["attempts"] == 1

    def test_a_new_pass_resets_the_exhaustion(self):
        box, _calls, _ = mailbox([Outcome({"messages": []}), Outcome({"messages": []})])
        box.quota_exhausted = True
        assert mailsource.GmailSource(box).listing(2, 10) is not None
        assert box.quota_exhausted is False

    def test_a_failed_inbox_listing_spends_nothing_on_sent(self):
        box, calls, _ = mailbox([Outcome(failure=classify_gws(1, PERM_403))])
        assert mailsource.GmailSource(box).listing(2, 10) is None
        assert len(calls) == 1
