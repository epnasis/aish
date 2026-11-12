"""The bank as a settlement source (#142 W5, slice 1c — the owner's decision of
2026-11-12: "Paid bills should match in bank history").

Pins: the sync seam's every ending (ok, no tool, failed, time limit, secrets
unreadable, stale) in the brief's exact words and the ledger's `bank` record;
the oldest account's date; the ledger read read-only, and absent, foreign or
locked as "could not check"; amounts recorded at ingest only for
authenticated pay-wanted mail, once, and the backfill; the match (amount AND
a tie — strong by case key or creditor account, named by entity token with
diacritics folded — never amount alone; both date edges); what is never
bank-settled (cautioned, restored before the payment, Reply, Decide); the
settlement order; the pairing ledger (spent once, a restore passes nothing
on, fixed order, re-verified when a transaction disappears); evidence in date
and amount only, hostile bank text never rendered; the watcher agreeing with
the report. A fixture ledger with the plugin's schema in a temp dir; the sync
is a seam; no real plugin, bank, mailbox or model is touched."""

from __future__ import annotations

import datetime
import json
import sqlite3
import time

import pytest
from mail_fixtures import DAY
from test_mailitems import his_sent, judged
from test_mailreport import (
    HOSTILE,
    NOW,
    FakePost,
    add_mail,
    add_verdict,
    build,
    fresh_health,
    identity,
    ok,
    tree,
)

from aish import (
    cli,
    mailbank,
    mailcases,
    mailindex,
    mailitems,
    mailitemtool,
    mailreport,
    mailsettle,
    tool_plugins,
    tools,
)

TODAY = datetime.date.fromtimestamp(NOW)
# The real function, taken before the suite's guard (`no_real_bank_sync`)
# replaces it: its discovery and runner are replaced in each test instead.
REAL_PLUGIN_SYNC = mailbank.plugin_sync


def day(offset: int) -> str:
    return (TODAY + datetime.timedelta(days=offset)).isoformat()


def short(iso: str) -> str:
    when = datetime.date.fromisoformat(iso)
    return f"{when.day} {when.strftime('%b')}"


# ---------------------------------------------------------------- fixtures


SCHEMA = """
CREATE TABLE accounts (alias TEXT PRIMARY KEY, stable_hash TEXT UNIQUE, session_uid TEXT,
    name TEXT, iban TEXT, currency TEXT, raw TEXT NOT NULL, linked_at TEXT NOT NULL,
    label TEXT, user TEXT, kind TEXT);
CREATE TABLE transactions (account TEXT NOT NULL, entry_id TEXT NOT NULL, booking_date TEXT,
    value_date TEXT, amount TEXT, amount_num REAL, currency TEXT, direction TEXT,
    counterparty TEXT, description TEXT, status TEXT, raw TEXT NOT NULL,
    search_blob TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (account, entry_id));
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def bank_db(state, *, synced: dict[str, float] | None = None, wal: bool = True):
    """The bank plugin's ledger schema (`banklib.connect`), with linked
    accounts last synced at `synced` (alias -> epoch)."""
    path = mailbank.ledger_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for alias, at in (synced if synced is not None else {"a1": NOW - 3600}).items():
        conn.execute("INSERT INTO accounts(alias, session_uid, raw, linked_at) "
                     "VALUES (?, 's', '{}', 'x')", (alias,))
        conn.execute("INSERT INTO meta VALUES (?, ?)", (f"last_sync_at:{alias}", str(int(at))))
    conn.commit()
    conn.close()
    return path


def add_tx(state, entry: str, *, booked: str, amount: str, currency: str = "PLN",
           direction: str = "out", status: str = "BOOK", counterparty: str = "",
           description: str = "", creditor: str | None = None, account: str = "a1") -> str:
    raw = {"entry_reference": entry}
    if creditor:
        raw["creditor_account"] = {"iban": creditor}
    conn = sqlite3.connect(mailbank.ledger_path(state))
    num = -float(amount) if direction == "out" else float(amount)
    conn.execute(
        "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'x')",
        (account, entry, booked, booked, amount, num, currency, direction, counterparty,
         description, status, json.dumps(raw)))
    conn.commit()
    conn.close()
    return f"{account}|{entry}"


def drop_tx(state, tx_id: str) -> None:
    account, entry = tx_id.split("|", 1)
    conn = sqlite3.connect(mailbank.ledger_path(state))
    conn.execute("DELETE FROM transactions WHERE account = ? AND entry_id = ?", (account, entry))
    conn.commit()
    conn.close()


def bill(state, gid: str = "n1", *, frm: str = "club@swim.example", subject: str = "Pay for March",
         body: str = "Pay 640,00 zł for March.", at: float = NOW - 3 * DAY, wanted: str = "pay",
         observe: bool = False, record: bool = True, **verdict) -> mailindex.Header:
    """A pay-wanted mail; its amounts recorded at ingest unless `record` is
    False; on a case (key, amount, account) when `observe`."""
    header = add_mail(state, gid, frm=frm, subject=subject, at=at, body=body)
    if observe:
        index = mailindex.MailIndex(state)
        mailcases.CaseStore(state).observe(header, index.body(header.digest) or "", True, index)
    add_verdict(state, header, wanted=wanted, recorded_at=at + 60, **verdict)
    if record:
        mailbank.record_amounts(state, mailindex.MailIndex(state), [
            {"mail": header.digest, "ending": "ok", "verdict": {"wanted": wanted}}],
            now=at + 60, by="ingest")
    return header


def ctx(state, now: float = NOW) -> mailitems.Context:
    return mailitems.Context(state, now=now)


def item_of(state, header) -> mailitems.OpenItem:
    return next(i for i in ctx(state).derive().items() if i.anchor == header.digest)


def state_of(state, header, now: float = NOW) -> mailitems.State:
    c = ctx(state, now)
    return c.state_of(next(i for i in c.derive().items() if i.anchor == header.digest))


def resolve_act(state, verb: str, anchor: str, at: float) -> None:
    mailitems.append_act(state, {"at": at, "verb": verb, "anchor": anchor, "items": [],
                                 "by": "owner"})


# ---------------------------------------------------------------- the ledger


class TestTheLedger:
    def test_no_ledger_is_could_not_check_with_its_reason(self, tmp_path):
        ledger = mailbank.read_ledger(tmp_path, NOW)
        assert not ledger.readable and not ledger.checked
        assert "no bank ledger" in ledger.reason

    def test_an_unexpected_schema_is_could_not_check(self, tmp_path):
        path = mailbank.ledger_path(tmp_path)
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE something (x TEXT)")
        conn.commit()
        conn.close()
        ledger = mailbank.read_ledger(tmp_path, NOW)
        assert not ledger.readable and "expected tables" in ledger.reason

    def test_a_locked_ledger_is_could_not_check(self, tmp_path, monkeypatch):
        bank_db(tmp_path)
        monkeypatch.setattr(mailbank, "LEDGER_BUSY_WAIT_S", 0.1)
        holder = sqlite3.connect(mailbank.ledger_path(tmp_path), isolation_level=None)
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("INSERT INTO meta VALUES ('x', 'y')")
        try:
            ledger = mailbank.read_ledger(tmp_path, NOW)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert not ledger.readable and "locked" in ledger.reason

    def test_the_date_is_the_oldest_account_and_it_decides_freshness(self, tmp_path):
        bank_db(tmp_path, synced={"a1": NOW - 3600, "a2": NOW - 3 * DAY})
        ledger = mailbank.read_ledger(tmp_path, NOW)
        assert ledger.synced_to == day(-3) and not ledger.checked
        assert "over 2 days" in ledger.reason
        fresh = tmp_path / "fresh"
        bank_db(fresh, synced={"a1": NOW - 3600, "a2": NOW - DAY})
        ledger = mailbank.read_ledger(fresh, NOW)
        assert ledger.checked and ledger.synced_to == day(-1)

    def test_an_account_never_synced_is_could_not_check(self, tmp_path):
        path = bank_db(tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO accounts(alias, session_uid, raw, linked_at) "
                     "VALUES ('a2', 's', '{}', 'x')")
        conn.commit()
        conn.close()
        ledger = mailbank.read_ledger(tmp_path, NOW)
        assert ledger.readable and not ledger.checked and "never synced" in ledger.reason

    @pytest.mark.parametrize("wal", [True, False])
    def test_reading_never_writes_the_ledger(self, tmp_path, wal):
        path = bank_db(tmp_path, wal=wal)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00")
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        mailbank.read_ledger(tmp_path, NOW)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        assert sorted(p.name for p in path.parent.iterdir()) == ["bank.db"]

    def test_only_outgoing_booked_transactions_with_a_real_date_and_currency(self, tmp_path):
        bank_db(tmp_path)
        add_tx(tmp_path, "in", booked=day(-1), amount="1.00", direction="in")
        add_tx(tmp_path, "pdng", booked=day(-1), amount="2.00", status="PDNG")
        add_tx(tmp_path, "cur", booked=day(-1), amount="3.00", currency="<b>")
        add_tx(tmp_path, "date", booked="soon", amount="4.00")
        add_tx(tmp_path, "good", booked=day(-1), amount="5.00")
        ledger = mailbank.read_ledger(tmp_path, NOW)
        assert [t.id for t in ledger.transactions] == ["a1|good"]
        assert ledger.transactions[0].cents == 500


# ---------------------------------------------------------------- the sync seam


def seam(outcome: mailbank.SyncOutcome, calls: list | None = None, touch=None):
    def sync(limit_s: float) -> mailbank.SyncOutcome:
        if calls is not None:
            calls.append(limit_s)
        if touch is not None:
            touch()
        return outcome
    return sync


def run_report(state, sync, *, dry_run: bool = False, now: float = NOW):
    post = FakePost([ok()])
    mailreport.run(state, clean=identity, now=now, dry_run=dry_run, since=None,
                   account="me@example.org", base="http://aish.test", token="tok", post=post,
                   sleep=lambda _s: None, out=lambda _t: None, bank_sync=sync)
    return post


class TestTheSyncSeam:
    def stale_bill(self, state):
        fresh_health(state)
        bank_db(state, synced={"a1": NOW - 5 * DAY})
        bill(state)

    def brief_and_entry(self, post, state):
        brief = post.calls[0][1]["brief"]
        return brief, mailreport.read_ledger(state)[-1]["bank"]

    def test_ok_and_fresh_says_checked_to_the_date(self, tmp_path):
        self.stale_bill(tmp_path)

        def synced_now():
            conn = sqlite3.connect(mailbank.ledger_path(tmp_path))
            conn.execute("UPDATE meta SET value = ? WHERE key = 'last_sync_at:a1'",
                         (str(int(NOW - 60)),))
            conn.commit()
            conn.close()

        calls: list = []
        post = run_report(tmp_path, seam(mailbank.SyncOutcome(mailbank.SYNC_OK), calls,
                                         synced_now))
        brief, entry = self.brief_and_entry(post, tmp_path)
        assert calls == [mailbank.SYNC_LIMIT_S]
        assert f"Bank checked to {short(day(0))}." in brief.splitlines()[-1]
        assert entry["checked"] is True and entry["sync"] == "ok" and entry["synced_to"] == day(0)

    @pytest.mark.parametrize("status,reason", [
        (mailbank.SYNC_NO_TOOL, "no bank_sync plugin tool is installed"),
        (mailbank.SYNC_FAILED, "error: the consent has expired"),
        (mailbank.SYNC_SECRETS, "ERROR: tool 'bank_sync' needs secret 'X' but it is not set."),
        (mailbank.SYNC_OK, ""),  # it ran, but the ledger is still old: stale
    ])
    def test_every_other_ending_is_could_not_check_with_its_reason(self, tmp_path, status,
                                                                     reason):
        self.stale_bill(tmp_path)
        post = run_report(tmp_path, seam(mailbank.SyncOutcome(status, reason)))
        brief, entry = self.brief_and_entry(post, tmp_path)
        assert (f"Could not check the bank (last synced {short(day(-5))})."
                in brief.splitlines()[-1])
        assert "unpaid" not in brief
        assert entry["checked"] is False and entry["sync"] == status
        assert entry["sync_reason"] == reason and "over 2 days" in entry["reason"]

    def test_the_time_limit_says_so(self, tmp_path):
        self.stale_bill(tmp_path)
        post = run_report(tmp_path, seam(mailbank.SyncOutcome(
            mailbank.SYNC_TIME_LIMIT, "the sync did not finish in 120 s")))
        brief, entry = self.brief_and_entry(post, tmp_path)
        assert ("Could not check the bank (the sync did not finish in 120 s; last synced "
                f"{short(day(-5))}).") in brief
        assert entry["sync"] == "time_limit"

    def test_no_ledger_at_all(self, tmp_path):
        fresh_health(tmp_path)
        bill(tmp_path)
        post = run_report(tmp_path, seam(mailbank.SyncOutcome(mailbank.SYNC_NO_TOOL, "none")))
        brief, entry = self.brief_and_entry(post, tmp_path)
        assert "Could not check the bank (no sync recorded)." in brief
        assert entry["synced_to"] == "" and "no bank ledger" in entry["reason"]

    def test_a_raising_seam_is_a_failed_sync_named_by_its_type(self, tmp_path):
        self.stale_bill(tmp_path)

        def boom(_limit):
            raise RuntimeError("secret-looking text")

        post = run_report(tmp_path, boom)
        _brief, entry = self.brief_and_entry(post, tmp_path)
        assert entry["sync"] == "failed" and entry["sync_reason"] == "the sync raised RuntimeError"

    def test_at_most_once_a_day(self, tmp_path):
        self.stale_bill(tmp_path)
        mailreport.append_ledger(tmp_path, {"at": NOW - 600, "day": time.strftime(
            "%Y-%m-%d", time.localtime(NOW)), "status": "failed",
            "bank": {"sync": "failed"}})
        calls: list = []
        post = run_report(tmp_path, seam(mailbank.SyncOutcome(mailbank.SYNC_OK), calls))
        _brief, entry = self.brief_and_entry(post, tmp_path)
        assert calls == [] and entry["sync"] == mailbank.SYNC_DONE_TODAY

    def test_a_dry_run_never_syncs(self, tmp_path):
        self.stale_bill(tmp_path)
        calls: list = []
        run_report(tmp_path, seam(mailbank.SyncOutcome(mailbank.SYNC_OK), calls), dry_run=True)
        assert calls == []

    def test_the_bank_clause_only_when_a_pay_line_exists(self, tmp_path):
        fresh_health(tmp_path)
        bank_db(tmp_path, synced={"a1": NOW - 5 * DAY})
        h = add_mail(tmp_path, "r", frm="a@x.example", subject="Question", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="reply", deadline="2026-12-01")
        report = build(tmp_path)
        assert "bank" not in mailreport.render_brief(report, clean=identity).lower()


class TestThePluginSync:
    """`plugin_sync` through `tool_plugins` — with discovery and the runner
    replaced: no test executes the owner's plugin."""

    def tool(self, tmp_path, timeout=300):
        return tool_plugins.Tool(name="bank_sync", description="d", executable="./wrapper",
                                 mutating=False, schema={}, timeout=timeout, body="",
                                 dir=tmp_path, secrets=("ENABLE_BANKING_APP_ID",))

    def test_no_tool_installed(self, monkeypatch):
        monkeypatch.setattr(tool_plugins, "discover", lambda cwd: ([], []))
        assert REAL_PLUGIN_SYNC(120).status == mailbank.SYNC_NO_TOOL

    @pytest.mark.parametrize("outcome,status", [
        (tools.ToolOutcome("{}", status="ok"), mailbank.SYNC_OK),
        (tools.ToolOutcome("ERROR: tool timed out", status="failed", error="timeout"),
         mailbank.SYNC_TIME_LIMIT),
        (tools.ToolOutcome("ERROR: needs secret", status="failed", error="unset_secret"),
         mailbank.SYNC_SECRETS),
        (tools.ToolOutcome("error: consent expired\nmore", status="failed"),
         mailbank.SYNC_FAILED),
    ])
    def test_each_ending_is_named(self, tmp_path, monkeypatch, outcome, status):
        ran = []
        monkeypatch.setattr(tool_plugins, "discover", lambda cwd: ([self.tool(tmp_path)], []))
        monkeypatch.setattr(tool_plugins, "execute",
                            lambda tool, args, cwd: ran.append(tool.timeout) or outcome)
        got = REAL_PLUGIN_SYNC(120)
        assert got.status == status and ran == [120]
        if status == mailbank.SYNC_FAILED:
            assert got.reason == "error: consent expired"


# ---------------------------------------------------------------- amounts at ingest


class TestAmountsAtIngest:
    def test_recorded_for_authenticated_pay_wanted_mail_only(self, tmp_path):
        pay = add_mail(tmp_path, "p", frm="a@x.example", at=NOW - DAY, body="Do zapłaty 640,00 zł")
        reply = add_mail(tmp_path, "r", frm="b@x.example", at=NOW - DAY, body="Kwota 10,00 zł")
        forged = mailindex.from_gmail(__import__("mail_fixtures").gmail(
            "f", frm="c@x.example", body="Pay 99,00 zł", at=NOW - DAY, auth="fail"),
            frozenset())
        mailindex.MailIndex(tmp_path).add(forged.header, forged.body)
        records = [{"mail": pay.digest, "ending": "ok", "verdict": {"wanted": "pay"}},
                   {"mail": reply.digest, "ending": "ok", "verdict": {"wanted": "reply"}},
                   {"mail": forged.header.digest, "ending": "ok", "verdict": {"wanted": "pay"}}]
        done = mailbank.record_amounts(tmp_path, mailindex.MailIndex(tmp_path), records,
                                       now=NOW, by="ingest")
        assert done.recorded == 1 and done.unauthenticated == 1
        assert mailbank.read_amounts(tmp_path) == {pay.digest: frozenset({("PLN", 64000)})}

    def test_recorded_once_and_never_reread_from_the_body(self, tmp_path):
        h = bill(tmp_path)
        (path,) = list((tmp_path / "mail").rglob(f"{h.digest}*body*")) or [None]
        if path is not None:
            path.unlink()
        # A second record, from a later reading, writes nothing new.
        mailbank.record_amounts(tmp_path, mailindex.MailIndex(tmp_path), [
            {"mail": h.digest, "ending": "ok", "verdict": {"wanted": "pay"}}], now=NOW, by="x")
        assert mailbank.read_amounts(tmp_path) == {h.digest: frozenset({("PLN", 64000)})}
        assert mailbank.amounts_path(tmp_path).read_text().count(h.digest) == 1

    def test_a_mail_with_no_amount_is_recorded_with_none(self, tmp_path):
        h = bill(tmp_path, body="Please pay the invoice.")
        assert mailbank.read_amounts(tmp_path) == {h.digest: frozenset()}

    def test_the_backfill_records_existing_pay_readings_whose_body_is_held(self, tmp_path):
        h = bill(tmp_path, record=False)
        other = add_mail(tmp_path, "o", frm="b@y.example", at=NOW - DAY, body="12,00 zł")
        add_verdict(tmp_path, other, wanted="reply")
        done = mailbank.backfill_amounts(tmp_path, now=NOW)
        assert done.recorded == 1
        assert mailbank.read_amounts(tmp_path) == {h.digest: frozenset({("PLN", 64000)})}
        assert mailbank.backfill_amounts(tmp_path, now=NOW).recorded == 0  # once


# ---------------------------------------------------------------- the match


class TestTheMatch:
    def test_named_by_an_entity_token_with_diacritics_folded(self, tmp_path):
        bank_db(tmp_path)
        h = bill(tmp_path, frm="Pływalnia Łódź <club@swim.example>")
        tx = add_tx(tmp_path, "e1", booked=day(-1), amount="640.00",
                    counterparty="PLYWALNIA LODZ SP Z O O")
        state = state_of(tmp_path, h)
        assert state.probably and state.source == mailitems.BANK
        assert state.evidence.bank_tie == mailbank.TIE_NAMED and state.evidence.bank_tx == tx
        assert state.evidence.bank_amount == ("PLN", 64000)

    def test_strong_by_a_case_key_in_the_description(self, tmp_path):
        bank_db(tmp_path)
        h = bill(tmp_path, frm="billing@utility.example", subject="Bill",
                 body="Faktura nr FV-2026-123 do zapłaty 640,00 zł", observe=True)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SOMEONE",
               description="oplata FV-2026-123")
        state = state_of(tmp_path, h)
        assert state.source == mailitems.BANK and state.evidence.bank_tie == mailbank.TIE_STRONG

    def test_strong_by_the_creditor_account_in_the_raw_record(self, tmp_path):
        bank_db(tmp_path)
        h = bill(tmp_path, frm="billing@utility.example", subject="Bill",
                 body="Rachunek nr RA/2026/77 kwota 640,00 zł na konto "
                      "PL61 1090 1014 0000 0712 1981 2874", observe=True)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SOMEONE",
               creditor="PL61109010140000071219812874")
        state = state_of(tmp_path, h)
        assert state.source == mailitems.BANK and state.evidence.bank_tie == mailbank.TIE_STRONG

    def test_an_amount_alone_is_rejected(self, tmp_path):
        bank_db(tmp_path)
        h = bill(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SOMEONE ELSE",
               description="transfer")
        assert state_of(tmp_path, h).state == mailitems.OPEN

    def test_the_amount_must_be_a_recorded_one_in_the_same_currency(self, tmp_path):
        bank_db(tmp_path)
        h = bill(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.01", counterparty="SWIM")
        add_tx(tmp_path, "e2", booked=day(-1), amount="640.00", currency="EUR",
               counterparty="SWIM")
        assert state_of(tmp_path, h).state == mailitems.OPEN
        unrecorded = tmp_path / "u"
        bank_db(unrecorded)
        h2 = bill(unrecorded, record=False)
        add_tx(unrecorded, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        assert state_of(unrecorded, h2).state == mailitems.OPEN

    @pytest.mark.parametrize("offset,settles", [(-7, True), (-8, False)])
    def test_the_early_edge_is_seven_days_before_the_mail(self, tmp_path, offset, settles):
        bank_db(tmp_path)
        arrived = datetime.datetime.combine(TODAY - datetime.timedelta(days=3),
                                            datetime.time(12)).timestamp()
        h = bill(tmp_path, at=arrived)
        add_tx(tmp_path, "e1", booked=day(-3 + offset), amount="640.00", counterparty="SWIM")
        assert (state_of(tmp_path, h).source == mailitems.BANK) is settles

    @pytest.mark.parametrize("offset,settles", [(0, True), (1, False)])
    def test_the_late_edge_is_today(self, tmp_path, offset, settles):
        bank_db(tmp_path)
        h = bill(tmp_path)
        add_tx(tmp_path, "e1", booked=day(offset), amount="640.00", counterparty="SWIM")
        assert (state_of(tmp_path, h).source == mailitems.BANK) is settles

    def test_a_cautioned_item_is_never_bank_settled(self, tmp_path):
        bank_db(tmp_path)
        h = bill(tmp_path, suspicion="suspicious")
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        assert state_of(tmp_path, h).state == mailitems.OPEN

    @pytest.mark.parametrize("wanted", ["reply", "decide"])
    def test_reply_and_decide_are_never_bank_settled(self, tmp_path, wanted):
        bank_db(tmp_path)
        h = bill(tmp_path, wanted=wanted, deadline=day(5))
        mailbank.record_amounts(tmp_path, mailindex.MailIndex(tmp_path), [
            {"mail": h.digest, "ending": "ok", "verdict": {"wanted": "pay"}}], now=NOW, by="t")
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        assert state_of(tmp_path, h).state == mailitems.OPEN

    def test_his_restore_outranks_a_payment_booked_at_or_before_it(self, tmp_path):
        bank_db(tmp_path)
        h = bill(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        restore = datetime.datetime.combine(TODAY - datetime.timedelta(days=1),
                                            datetime.time(20)).timestamp()
        resolve_act(tmp_path, "restore", h.digest, restore)
        assert state_of(tmp_path, h).state == mailitems.OPEN
        add_tx(tmp_path, "e2", booked=day(0), amount="640.00", counterparty="SWIM")
        assert state_of(tmp_path, h).evidence.bank_tx == "a1|e2"

    def test_the_settlement_order_his_word_judgement_his_reply_then_the_bank(self, tmp_path):
        bank_db(tmp_path)
        h = bill(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        assert state_of(tmp_path, h).source == mailitems.BANK
        his_sent(tmp_path, "o1", to="club@swim.example", at=NOW - DAY, subject="Paid")
        assert state_of(tmp_path, h).source == mailitems.RULE
        thanks = add_mail(tmp_path, "k1", frm="club@swim.example", subject="Thank you",
                          at=NOW - DAY / 2, body="We booked your payment.")
        add_verdict(tmp_path, thanks, wanted="nothing", recorded_at=NOW - DAY / 2)
        judged(tmp_path, item_of(tmp_path, h), thanks, "settled_by", at=NOW - DAY / 3)
        assert state_of(tmp_path, h).source == mailitems.JUDGE
        resolve_act(tmp_path, "resolve", h.digest, NOW - 60)
        assert state_of(tmp_path, h).source == mailitems.OWNER

    def test_a_stale_ledger_makes_no_new_pair_but_reverifies_a_recorded_one(self, tmp_path):
        bank_db(tmp_path, synced={"a1": NOW - 5 * DAY})
        h = bill(tmp_path)
        tx = add_tx(tmp_path, "e1", booked=day(-6), amount="640.00", counterparty="SWIM")
        assert state_of(tmp_path, h).state == mailitems.OPEN
        mailbank.record_pairs(tmp_path, {item_of(tmp_path, h).key: mailbank.Pairing(
            mailbank.read_ledger(tmp_path, NOW).transactions[0], "named", False)}, now=NOW,
            by="test")
        state = state_of(tmp_path, h)
        assert state.source == mailitems.BANK and state.evidence.bank_tx == tx


# ---------------------------------------------------------------- the pairing


def two_bills(state):
    first = bill(state, "n1", subject="Pay for March", at=NOW - 4 * DAY)
    second = bill(state, "n2", subject="Pay for April", at=NOW - 2 * DAY)
    return first, second


def record_now(state, now: float = NOW) -> int:
    c = ctx(state, now)
    return mailbank.record_pairs(state, {k: p for k, p in c.bank_pairings().items()
                                         if not p.recorded}, now=now, by="test")


class TestThePairing:
    def test_a_payment_settles_one_item_the_oldest(self, tmp_path):
        bank_db(tmp_path)
        first, second = two_bills(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        assert state_of(tmp_path, first).source == mailitems.BANK
        assert state_of(tmp_path, second).state == mailitems.OPEN

    def test_items_oldest_first_each_take_the_earliest_booked(self, tmp_path):
        bank_db(tmp_path)
        first, second = two_bills(tmp_path)
        late = add_tx(tmp_path, "late", booked=day(-1), amount="640.00", counterparty="SWIM")
        early = add_tx(tmp_path, "early", booked=day(-2), amount="640.00", counterparty="SWIM")
        assert state_of(tmp_path, first).evidence.bank_tx == early
        assert state_of(tmp_path, second).evidence.bank_tx == late

    def test_restoring_a_paid_bill_passes_its_payment_to_no_other(self, tmp_path):
        bank_db(tmp_path)
        first, second = two_bills(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        assert record_now(tmp_path) == 1
        resolve_act(tmp_path, "restore", first.digest, NOW - 60)
        assert state_of(tmp_path, first).state == mailitems.OPEN
        assert state_of(tmp_path, second).state == mailitems.OPEN

    def test_his_restore_through_the_tool_records_the_pairing_first(self, tmp_path):
        bank_db(tmp_path)
        first, second = two_bills(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        assert mailbank.read_pairs(tmp_path) == []
        planned = mailitemtool.plan(tmp_path, {"action": "restore", "ref": first.digest[:12]},
                                    now=NOW)
        assert mailitemtool.apply(tmp_path, planned, source={}, now=NOW, carded=False).ok
        assert [tx for tx, _item in mailbank.read_pairs(tmp_path)] == ["a1|e1"]
        assert state_of(tmp_path, first).state == mailitems.OPEN
        assert state_of(tmp_path, second).state == mailitems.OPEN

    def test_a_recorded_pair_is_reverified_and_a_vanished_payment_reopens(self, tmp_path):
        bank_db(tmp_path)
        h = bill(tmp_path)
        tx = add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        record_now(tmp_path)
        assert state_of(tmp_path, h).source == mailitems.BANK
        drop_tx(tmp_path, tx)
        assert state_of(tmp_path, h).state == mailitems.OPEN

    def test_one_item_takes_at_most_one_payment(self, tmp_path):
        bank_db(tmp_path)
        h = bill(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-2), amount="640.00", counterparty="SWIM")
        add_tx(tmp_path, "e2", booked=day(-1), amount="640.00", counterparty="SWIM")
        assert record_now(tmp_path) == 1
        assert [tx for tx, _ in mailbank.read_pairs(tmp_path)] == ["a1|e1"]
        assert state_of(tmp_path, h).evidence.bank_tx == "a1|e1"

    def test_the_report_run_records_new_pairs_and_a_dry_run_does_not(self, tmp_path):
        fresh_health(tmp_path)
        bank_db(tmp_path)
        bill(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        before = tree(tmp_path)
        run_report(tmp_path, seam(mailbank.SyncOutcome(mailbank.SYNC_OK)), dry_run=True)
        assert tree(tmp_path) == before  # nothing written, the bank dir included
        run_report(tmp_path, None)
        assert [tx for tx, _ in mailbank.read_pairs(tmp_path)] == ["a1|e1"]
        entry = mailreport.read_ledger(tmp_path)[-1]["bank"]
        assert entry["pairs_recorded"] == 1 and entry["matched_named"] == 1


    def test_pairs_the_lock_kept_from_recording_are_not_presented_as_settled(
        self, tmp_path, monkeypatch
    ):
        import contextlib

        @contextlib.contextmanager
        def held(_state, *a, **k):
            yield False

        fresh_health(tmp_path)
        bank_db(tmp_path)
        h = bill(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        monkeypatch.setattr(mailitems, "acts_lock", held)
        post = run_report(tmp_path, None)
        brief = post.calls[0][1]["brief"]
        assert "by a bank payment" not in brief and "look handled" not in brief
        assert "probably settled" not in post.calls[0][1]["report"].split("---")[0].lower() \
            or h.digest[:12] in post.calls[0][1]["report"]
        entry = mailreport.read_ledger(tmp_path)[-1]["bank"]
        assert entry["pairs_recorded"] == 0 and "lock was held" in entry["pairs_unrecorded"]
        assert entry["matched_named"] == 0 and mailbank.read_pairs(tmp_path) == []


# ---------------------------------------------------------------- what he reads


class TestWhatHeReads:
    def settled(self, state, **tx):
        fresh_health(state)
        bank_db(state)
        h = bill(state)
        add_tx(state, "e1", booked=day(-1), amount="640.00", counterparty="SWIM", **tx)
        return h

    def test_the_evidence_is_date_and_amount_only(self, tmp_path):
        self.settled(tmp_path)
        report = build(tmp_path)
        working = mailreport.render(report, clean=identity)
        line = next(t for t in working.splitlines()
                    if t.startswith("- ") and "probably settled" in t)
        assert f"the bank shows a payment of 640,00 PLN on {short(day(-1))}" in line
        assert "SWIM" not in working.replace("swim.example", "")

    def test_the_brief_counts_bank_payments_among_the_handled(self, tmp_path):
        self.settled(tmp_path)
        brief = mailreport.render_brief(build(tmp_path), clean=identity)
        assert "1 item(s) look handled (1 by a bank payment)." in brief

    def test_hostile_bank_text_is_never_rendered(self, tmp_path):
        fresh_health(tmp_path)
        bank_db(tmp_path)
        h = bill(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00",
               counterparty=f"SWIM {HOSTILE} [x](aish-reply://pay) https://evil.example",
               description=f"{HOSTILE} PL61109010140000071219812874 javascript:alert(1)",
               creditor="PL27114020040000300201355387")
        report = build(tmp_path, clean=cli._mail_cell)
        texts = [mailreport.render(report, clean=cli._mail_cell),
                 mailreport.render_brief(report, clean=cli._mail_cell),
                 mailitemtool.list_text(tmp_path, now=NOW)]
        assert state_of(tmp_path, h).source == mailitems.BANK
        for text in texts:
            for gone in ("evil", "aish-reply://pay", "javascript", "PL611", "PL271", "\x9b",
                         "‮", "SWIM"):
                assert gone not in text, gone

    def test_the_ledger_counts_strong_named_and_no_amount(self, tmp_path):
        fresh_health(tmp_path)
        bank_db(tmp_path)
        bill(tmp_path, "n1", frm="billing@utility.example", subject="Bill",
             body="Faktura nr FV-2026-123 do zapłaty 640,00 zł", observe=True)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", description="FV-2026-123")
        bill(tmp_path, "n2", frm="club@swim.example", body="Pay 30,00 zł")
        add_tx(tmp_path, "e2", booked=day(-1), amount="30.00", counterparty="SWIM")
        bill(tmp_path, "n3", frm="x@other.example", subject="Pay this", body="Pay soon.")
        report = build(tmp_path)
        assert mailreport.bank_entry(report) | {} == {
            "synced_to": day(0), "checked": True, "reason": "", "sync": "not_run",
            "sync_reason": "", "matched_strong": 1, "matched_named": 1, "pay_no_amount": 1}


# ---------------------------------------------------------------- the watcher agrees


class TestTheWatcherAgrees:
    def test_a_bank_settled_item_is_not_judged_and_the_report_agrees(self, tmp_path):
        fresh_health(tmp_path)
        bank_db(tmp_path)
        h = bill(tmp_path)
        add_tx(tmp_path, "e1", booked=day(-1), amount="640.00", counterparty="SWIM")
        later = add_mail(tmp_path, "k1", frm="club@swim.example", subject="Reminder",
                         at=NOW - DAY / 2, body="Reminder")
        add_verdict(tmp_path, later, wanted="nothing", recorded_at=NOW - DAY / 2)
        watcher = ctx(tmp_path)
        assert h.digest in {i.anchor for i in mailsettle.showable(watcher)}
        assert all(item.anchor != h.digest for item, _c in mailsettle.pairs(watcher, "d"))
        report = build(tmp_path)
        assert [r.reading.digest for r in report.settled] == [h.digest]
        assert watcher.state_of(item_of(tmp_path, h)).source == mailitems.BANK
