"""The keeper clamp (#142 W5, owner decision 2026-10-27, option A).

Pins: a keeper document — a mail carrying a case key or a document attachment
from an authenticated sender, or one the Reader placed on a case — is never
filed: an `archive` / `expire_after_grace` verdict reads `surface`, written
BESIDE the untouched verdict; a newsletter-gated mail never gets it; an
unauthenticated mail gets the visibility from the judged signal and nothing
else — no case, no grade; closing every case the mail is on releases it, and
with no closure source it holds; `/mail`, the CSV and the backtest report show
it. Fixtures only: no mail, no model.
"""

from __future__ import annotations

import csv
import io

from mail_fixtures import (
    DAY,
    OWNER,
    T0,
    FakeMailbox,
    ScriptedReader,
    admit,
    gmail,
    reader_charter,
    verdict_json,
)
from test_mailwatch import records, run

from aish import (
    cli,
    mailbacktest,
    mailexport,
    mailindex,
    mailreader,
    mailverdicts,
    mailview,
    vocab,
)

ORDER = "Order LS-48213 confirmed"


def filing(disposition="archive", case="no_case"):
    def reply(bundle):
        return verdict_json(disposition=disposition, case=case, citations=[bundle["mail"]])
    return reply


def one(**kwargs) -> FakeMailbox:
    kwargs.setdefault("frm", "Lamp Shop <orders@lampshop.example>")
    kwargs.setdefault("at", T0)
    kwargs.setdefault("labels", ("INBOX",))
    return FakeMailbox([gmail("k", **kwargs)])


def keeper(tmp_path, box, reply) -> dict:
    run(tmp_path, box, ScriptedReader(reply))
    return records(tmp_path)["k"]


class TestItFires:
    def test_a_case_key_from_an_authenticated_sender(self, tmp_path):
        for disposition in mailreader.FILING_DISPOSITIONS:
            state = tmp_path / disposition
            record = keeper(state, one(subject=ORDER, body="Thanks."), filing(disposition))
            assert record["keeper_clamped"]["signals"] == ["case_key"]
            assert record["keeper_clamped"]["from"] == disposition
            assert mailverdicts.effective_disposition(record) == "surface"

    def test_a_document_attachment_from_an_authenticated_sender(self, tmp_path):
        record = keeper(tmp_path, one(subject="Your policy", body="See attached.",
                                      attachments=("Policy.PDF",)), filing())
        assert record["keeper_clamped"]["signals"] == ["document_attachment"]
        assert mailverdicts.effective_disposition(record) == "surface"

    def test_the_reader_placing_it_on_a_case(self, tmp_path):
        record = keeper(tmp_path, one(subject="Hello", body="Hi."), filing(case="new"))
        assert record["keeper_clamped"]["signals"] == ["reader_case"]

    def test_it_never_rewrites_the_verdict(self, tmp_path):
        record = keeper(tmp_path, one(subject=ORDER, attachments=("invoice.pdf",)),
                        filing("expire_after_grace", case="new"))
        assert record["verdict"]["disposition"] == "expire_after_grace"
        assert record["keeper_clamped"]["signals"] == [
            "case_key", "document_attachment", "reader_case"]
        assert mailverdicts.live_disposition(record) == "expire_after_grace"

    def test_a_mail_that_is_no_keeper_is_filed_as_the_reader_said(self, tmp_path):
        record = keeper(tmp_path, one(subject="Hello", body="Hi.",
                                      attachments=("photo.jpg", "policy.pdf.exe")), filing())
        assert record["keeper_clamped"] is None
        assert mailverdicts.effective_disposition(record) == "archive"

    def test_it_only_ever_moves_toward_his_attention(self):
        for keep in ("surface", "hold", "cannot_tell"):
            assert mailreader.keeper_clamp(
                keep, authenticated=True, gated=False, keys={"LS-48213"},
                attachments=(mailindex.Attachment("a.pdf"),), case_choice="new", cases=(),
            ) is None

    def test_a_mail_with_no_attachment_asks_the_list_nothing(self):
        vocab.drain()
        assert not mailreader.has_document(())
        assert "mailreader._DOCUMENT_SUFFIXES" not in vocab.drain()
        assert not mailreader.has_document((mailindex.Attachment("photo.jpg"),))
        tally = vocab.drain()["mailreader._DOCUMENT_SUFFIXES"]
        assert (tally["asked"], tally["matched"]) == (1, 0)

    def test_the_document_types(self):
        def doc(name, mime=""):
            return mailreader.has_document((mailindex.Attachment(name, mime),))
        assert doc("Polisa.pdf") and doc("offer.DOCX") and doc("calc.ods")
        assert doc("scan", "application/pdf")
        assert not doc("policy.pdf.exe") and not doc("photo.jpg") and not doc("pdf")


class TestNewslettersNever:
    def test_a_gated_mail_never_gets_it(self, tmp_path):
        record = keeper(tmp_path, one(subject=ORDER, attachments=("invoice.pdf",),
                                      list_unsubscribe="<mailto:u@lampshop.example>"),
                        filing())
        assert record["ending"] == mailreader.GATED
        assert record["keeper_clamped"] is None

    def test_the_rule_refuses_gated_mail_whatever_its_signals(self):
        assert mailreader.keeper_clamp(
            "archive", authenticated=True, gated=True, keys={"LS-48213"},
            attachments=(mailindex.Attachment("a.pdf"),), case_choice="new", cases=(),
        ) is None


class TestUnauthenticated:
    def test_deterministic_signals_need_authentication(self, tmp_path):
        record = keeper(tmp_path, one(subject=ORDER, attachments=("invoice.pdf",), auth="fail"),
                        filing())
        assert record["gate"]["auth"]["authenticated"] is False
        assert record["keeper_clamped"] is None

    def test_the_judged_signal_gives_visibility_and_nothing_else(self, tmp_path):
        box = one(frm="Lamp Shop <orders@lampshop-billing.example>", subject=ORDER,
                  attachments=("invoice.pdf",), auth="fail")
        record = keeper(tmp_path, box, filing(case="new"))
        assert record["keeper_clamped"]["signals"] == ["reader_case"]
        assert mailverdicts.effective_disposition(record) == "surface"
        # No trust minted: no case opened, no grade, nothing admitted.
        assert record["observation"]["created"] == ""
        assert record["observation"]["cases"] == []
        assert record["keeper_clamped"]["cases"] == []
        grade = record["gate"]["grade"]
        assert grade["grade"] == "first-contact" and grade["acts"] == 0
        assert not list((tmp_path / "mail" / "cases").glob("*.json"))


class TestCaseClosureReleases:
    def test_closing_every_case_releases_it(self, tmp_path):
        record = keeper(tmp_path, one(subject=ORDER), filing())
        [case] = record["keeper_clamped"]["cases"]
        assert case.startswith("case-")
        assert mailverdicts.effective_disposition(record, closed_cases={case}) == "archive"
        assert mailverdicts.effective_disposition(record, closed_cases={"case-other"}) == "surface"

    def test_the_readers_chosen_candidate_is_one_of_its_cases(self, tmp_path):
        """A courier notice with no key, placed by the Reader on the store's
        order case: closing THAT case releases it."""
        box = FakeMailbox([
            gmail("o", frm="Lamp Shop <orders@lampshop.example>", subject=ORDER, at=T0,
                  labels=("INBOX",)),
            gmail("k", frm="Courier <notify@courier.example>",
                  subject="Your Lamp Shop parcel is on its way", at=T0 + DAY,
                  labels=("INBOX",)),
        ])

        def reply(bundle):
            refs = [c["ref"] for c in bundle.get("candidates") or ()]
            return verdict_json(disposition="archive" if refs else "surface",
                                case=refs[0] if refs else "no_case",
                                citations=[bundle["mail"]])

        record = keeper(tmp_path, box, reply)
        [case] = record["keeper_clamped"]["cases"]
        assert record["case_choice"] == case
        assert record["keeper_clamped"]["signals"] == ["reader_case"]
        assert mailverdicts.effective_disposition(record) == "surface"
        assert mailverdicts.effective_disposition(record, closed_cases={case}) == "archive"

    def test_one_open_case_still_holds(self):
        clamp = {"from": "archive", "to": "surface", "cases": ["case-a", "case-b"]}
        assert not mailreader.keeper_released(clamp, {"case-a"})
        assert mailreader.keeper_released(clamp, {"case-a", "case-b"})

    def test_a_keeper_on_no_case_holds_for_good(self):
        clamp = {"from": "archive", "to": "surface", "signals": ["document_attachment"],
                 "cases": []}
        assert not mailreader.keeper_released(clamp, {"case-a"})

    def test_with_no_closure_source_it_holds(self, tmp_path):
        """The case store has no closure (S3): the default releases nothing."""
        record = keeper(tmp_path, one(subject=ORDER), filing())
        assert mailverdicts.effective_disposition(record) == "surface"

    def test_the_live_clamp_is_not_released_by_case_closure(self):
        record = {"ending": "ok", "verdict": {"disposition": "archive"},
                  "clamped": {"from": "archive", "to": "surface"},
                  "keeper_clamped": {"from": "archive", "to": "surface", "cases": ["case-a"]}}
        assert mailverdicts.effective_disposition(record, closed_cases={"case-a"}) == "surface"

    def test_a_record_from_before_it_reads_as_its_verdict(self):
        old = {"ending": "ok", "verdict": {"disposition": "archive"}, "clamped": None}
        assert mailverdicts.keeper_clamp_of(old) is None
        assert mailverdicts.effective_disposition(old) == "archive"


class TestCorruptRecords:
    def test_a_clamp_naming_anything_but_surface_is_no_clamp(self):
        record = {"ending": "ok", "verdict": {"disposition": "surface"}, "clamped": None,
                  "keeper_clamped": {"from": "surface", "to": "archive", "cases": []}}
        assert mailverdicts.keeper_clamp_of(record) is None
        assert mailverdicts.effective_disposition(record) == "surface"

    def test_odd_shapes_neither_raise_nor_release(self):
        clamp = {"from": "archive", "to": "surface", "signals": 7, "cases": "case-a"}
        assert not mailreader.keeper_released(clamp, {"case-a"})


class TestTheSurfaces:
    def test_mail_marks_the_row(self, tmp_path):
        keeper(tmp_path, one(subject=ORDER, attachments=("invoice.pdf",)), filing())
        text = "\n".join(t for _s, t in mailview.lines(tmp_path, clean=lambda s: s,
                                                        now=T0 + DAY))
        assert ("would: surface [KEEPER-CLAMPED from archive: case key, document attachment"
                in text)

    def test_the_new_fields_go_through_the_sanitiser(self, tmp_path):
        keeper(tmp_path, one(subject=ORDER), filing())
        seen: list[str] = []
        mailview.lines(tmp_path, clean=lambda s: seen.append(s) or s, now=T0 + DAY)
        assert "case_key" in seen
        seen.clear()
        mailexport.render(tmp_path, clean=lambda s: seen.append(s) or s)
        assert "yes (from archive: case_key)" in seen

    def test_the_csv_column(self, tmp_path):
        keeper(tmp_path, one(subject=ORDER), filing())
        text, _rows, _ = mailexport.render(tmp_path, clean=cli._plain)
        [row] = list(csv.DictReader(io.StringIO(text)))
        assert "keeper_clamped" in mailexport.COLUMNS
        assert row["keeper_clamped"] == "yes (from archive: case_key)"
        assert row["disposition_raw"] == "archive"
        assert row["disposition_effective"] == "surface"
        assert row["clamped"] == "no"

    def test_the_report_counts_raw_live_and_keeper(self, tmp_path):
        box = FakeMailbox([
            gmail("k", frm="orders@lampshop.example", subject=ORDER, at=T0, labels=("INBOX",)),
            gmail("h", frm="hello@gardenstore.example", subject="Bulbs", at=T0 + DAY,
                  labels=("INBOX",)),
        ])
        charter = reader_charter()
        spec = "local:scripted"
        admit(tmp_path / "ledger", charter, spec)
        result = mailbacktest.run_backtest(
            mailbox=box, owner_addresses=frozenset({OWNER}),
            arms={spec: ScriptedReader(filing())}, charter=charter, out_dir=tmp_path / "run",
            days=90, now=T0 + 10 * DAY, admission_state_dir=tmp_path / "ledger")
        arm = result["arms"][spec]
        assert arm["clamped"] == 0 and arm["keeper_clamped"] == 1
        assert arm["disposition_by_truth"] == {"weak:kept_read": {"archive": 2}}
        assert arm["disposition_by_truth_clamped"] == {"weak:kept_read": {"archive": 2}}
        assert arm["disposition_by_truth_keeper"] == {
            "weak:kept_read": {"archive": 1, "surface": 1}}
        text = (tmp_path / "run" / "report.txt").read_text()
        assert "after the keeper clamp too (1 reading(s) keeper-clamped)" in text
        assert "disposition after both clamps" in text
