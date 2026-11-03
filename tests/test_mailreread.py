"""Readings made WITHOUT the mail's body, read again (#142 W5) —
`mailwatch.blind_readings`, `reread_blind`, `--reread-blind`.

The premise, from the 2026-11-02 CRLF bug: a reading whose recorded input
carried "[body not recorded]" for the reviewed mail did not judge that mail.
Only when the evidence store NOW yields the body is it superseded — kept whole
under `supersedes`, never deleted — and read again, oldest mail first,
bounded. A body genuinely absent is never retried. Scripted Reader only."""

from __future__ import annotations

from mail_fixtures import ScriptedReader, admit, reader_charter, verdict_json
from test_mailreport import NOW, add_mail

from aish import evidence, mailcases, mailindex, mailreport, mailverdicts, mailwatch

MODEL = "gemini:test"
DAY = 86400.0


def read_blind(state, gid: str, at: float, *, restore: bool = True):
    """A mail read while its body did not come back from the store."""
    header = add_mail(state, gid, frm="tenant@flat.example", subject="Utilities",
                      at=at, body=f"I paid the top-up ({gid}).")
    header = mailindex.MailIndex(state).get(header.digest)
    body = mailindex.MailIndex(state).body(header.digest)
    assert evidence.purge(header.body_digest, state)
    index = mailindex.MailIndex(state)
    record = mailwatch.assess(index, mailcases.CaseStore(state), header, charter=reader_charter(),
                              model_spec=MODEL, chat=ScriptedReader([verdict_json(
                                  wanted="cannot_tell")]), state_dir=state, now=at + 60)
    mailverdicts.VerdictStore(state).write(record)
    if restore:
        evidence.put(body or "", state)
    return header


def reread(state, chat, limit: int = 10) -> int:
    return mailwatch.reread_blind(state, index=mailindex.MailIndex(state),
                                  store=mailcases.CaseStore(state), charter=reader_charter(),
                                  model_spec=MODEL, now=NOW, limit=limit, chat=chat)


class TestBlindReadings:
    def test_only_a_reading_whose_body_the_store_now_yields(self, tmp_path):
        admit(tmp_path, reader_charter(), MODEL)
        blind = read_blind(tmp_path, "b1", NOW - 5 * DAY)
        read_blind(tmp_path, "b2", NOW - 4 * DAY, restore=False)  # genuinely absent
        seen = add_mail(tmp_path, "b3", frm="x@y.example", subject="Seen", at=NOW - 3 * DAY,
                        body="readable")
        mailverdicts.VerdictStore(tmp_path).write(mailwatch.assess(
            mailindex.MailIndex(tmp_path), mailcases.CaseStore(tmp_path), seen,
            charter=reader_charter(), model_spec=MODEL,
            chat=ScriptedReader([verdict_json()]), state_dir=tmp_path, now=NOW - 3 * DAY))
        found = mailwatch.blind_readings(tmp_path, mailindex.MailIndex(tmp_path), MODEL)
        assert [h.digest for h in found] == [blind.digest]

    def test_reread_supersedes_keeps_the_old_record_and_the_first_read_clock(self, tmp_path):
        admit(tmp_path, reader_charter(), MODEL)
        blind = read_blind(tmp_path, "b1", NOW - 5 * DAY)
        first = mailreport.load_readings(tmp_path)[0][blind.digest].first_seen
        scripted = ScriptedReader([verdict_json(wanted="nothing", disposition="archive")])
        assert reread(tmp_path, scripted) == 1
        assert "I paid the top-up (b1)." in scripted.bundles[0]["messages"][-1]["text"]
        record = mailverdicts.VerdictStore(tmp_path).read(blind.digest)["by_model"][MODEL]
        assert record["verdict"]["wanted"] == "nothing"
        assert record["supersedes"]["record"]["verdict"]["wanted"] == "cannot_tell"
        assert "[body not recorded]" in record["supersedes"]["why"]
        assert mailreport.load_readings(tmp_path)[0][blind.digest].first_seen == first
        assert reread(tmp_path, scripted) == 0  # read once

    def test_bounded_oldest_first(self, tmp_path):
        admit(tmp_path, reader_charter(), MODEL)
        older = read_blind(tmp_path, "b1", NOW - 9 * DAY)
        read_blind(tmp_path, "b2", NOW - 2 * DAY)
        scripted = ScriptedReader(lambda _bundle: verdict_json())
        assert reread(tmp_path, scripted, limit=1) == 1
        assert scripted.bundles[0]["messages"][-1]["text"].endswith("(b1).")
        assert "supersedes" in mailverdicts.VerdictStore(tmp_path).read(
            older.digest)["by_model"][MODEL]

    def test_no_answer_leaves_the_old_reading_standing(self, tmp_path):
        admit(tmp_path, reader_charter(), MODEL)
        blind = read_blind(tmp_path, "b1", NOW - 5 * DAY)

        def down(**kwargs):
            raise ConnectionError("no route")

        assert reread(tmp_path, down) == 0
        record = mailverdicts.VerdictStore(tmp_path).read(blind.digest)["by_model"][MODEL]
        assert "supersedes" not in record and record["verdict"]["wanted"] == "cannot_tell"


class TestTheScanIsBounded:
    def test_after_the_backlog_drains_a_pass_reads_no_evidence(self, tmp_path, monkeypatch):
        import os

        admit(tmp_path, reader_charter(), MODEL)
        read_blind(tmp_path, "b1", NOW - 5 * DAY)
        assert reread(tmp_path, ScriptedReader(lambda _b: verdict_json())) == 1
        # Every verdict file is now older than the marker.
        for path in mailverdicts.store_dir(tmp_path).glob("*/*.json"):
            os.utime(path, (NOW - DAY, NOW - DAY))
        calls = []
        real_get = evidence.get
        monkeypatch.setattr(evidence, "get", lambda d, s: calls.append(d) or real_get(d, s))
        assert reread(tmp_path, ScriptedReader([])) == 0
        assert calls == []

    def test_a_reading_written_after_the_marker_is_still_checked(self, tmp_path):
        import os

        admit(tmp_path, reader_charter(), MODEL)
        assert reread(tmp_path, ScriptedReader([])) == 0  # empty: marks clean at NOW
        blind = read_blind(tmp_path, "b2", NOW - DAY)
        path = next(mailverdicts.store_dir(tmp_path).glob(f"*/{blind.digest}.json"))
        os.utime(path, (NOW + 60, NOW + 60))
        assert mailwatch.blind_readings(tmp_path, mailindex.MailIndex(tmp_path), MODEL,
                                        since=NOW) == [mailindex.MailIndex(tmp_path).get(
                                            blind.digest)]

    def test_the_one_shot_scans_everything(self, tmp_path):
        import os

        admit(tmp_path, reader_charter(), MODEL)
        blind = read_blind(tmp_path, "b1", NOW - 5 * DAY)
        mailwatch._mark_clean(tmp_path, MODEL, NOW + 3600)  # a marker newer than the file
        for path in mailverdicts.store_dir(tmp_path).glob("*/*.json"):
            os.utime(path, (NOW, NOW))
        assert reread(tmp_path, ScriptedReader([])) == 0  # the pass trusts the marker
        done = mailwatch.reread_blind(tmp_path, index=mailindex.MailIndex(tmp_path),
                                      store=mailcases.CaseStore(tmp_path),
                                      charter=reader_charter(), model_spec=MODEL, now=NOW,
                                      limit=5, full=True,
                                      chat=ScriptedReader(lambda _b: verdict_json()))
        assert done == 1
        assert "supersedes" in mailverdicts.VerdictStore(tmp_path).read(
            blind.digest)["by_model"][MODEL]

    def test_an_invalid_reread_supersedes_an_ok_one(self, tmp_path):
        """Stated edge: a blind `ok` re-read as `invalid` leaves the mail with
        no verdict — the conservative direction (counted, never a guess)."""
        admit(tmp_path, reader_charter(), MODEL)
        blind = read_blind(tmp_path, "b1", NOW - 5 * DAY)
        assert reread(tmp_path, ScriptedReader(lambda _b: "not json")) == 1
        record = mailverdicts.VerdictStore(tmp_path).read(blind.digest)["by_model"][MODEL]
        assert record["ending"] == "invalid" and record["supersedes"]["record"]["ending"] == "ok"
