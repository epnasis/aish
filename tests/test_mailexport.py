"""The CSV review file (#142 W5 S1.6).

Pins: every cell takes sanitiser → formula escape → cap and survives a
spreadsheet as text; QUOTE_ALL; rows for every ending, never a blank cell for
"unknown"; one row per (mail, model); the backtest's truth columns are
recomputed and say so; every per-arm report cell is a filter over the file's
columns; `--csv` makes no mailbox call; `/mail export` is wired. Fixtures only.
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
)
from test_mailbacktest import (
    ARM_A,
    ARM_B,
    NOW,
    backtest,
    careful,
    expire_everything,
    history,
)
from test_mailwatch import run

from aish import cli, mailbacktest, mailexport, mailindex, mailverdicts, mailwatch


def read_csv(path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))


def seed(state_dir, *, digest_of: str, record: dict) -> None:
    mailverdicts.VerdictStore(state_dir).write({"mail": digest_of, **record})


def hostile_state(state_dir) -> str:
    parsed = mailindex.from_gmail(
        gmail("h", frm="\"@SUM(A1)\" <x@evil.example>", subject="=HYPERLINK(\"http://x\")",
              body="b", at=T0), frozenset({OWNER}))
    assert parsed is not None
    mailindex.MailIndex(state_dir).add(parsed.header, parsed.body)
    seed(state_dir, digest_of=parsed.header.digest, record={
        "kind": "mail_verdict", "provider_id": "h", "at": T0, "model": "-cmd", "ending": "ok",
        "gate": {"auth": {"authenticated": True}, "grade": {"grade": "known"},
                 "flags": [], "newsletter": {"gated": False, "by": []}},
        "verdict": {"disposition": "archive", "wanted": "nothing", "deadline": "none",
                    "case": "no_case", "sender_case": "consistent", "suspicion": "none",
                    "meaning": "  =1+1\tand more", "kind": "+cmd"},
        "owner_wrote_days": 2.0,
        "clamped": {"from": "archive", "to": "surface", "owner_wrote_days": 2.0, "limit": 30},
    })
    return parsed.header.digest


class TestCells:
    def test_formula_characters_survive_as_escaped_text(self, tmp_path):
        hostile_state(tmp_path)
        text, rows, _ = mailexport.render(tmp_path, clean=cli._plain)
        [row] = list(csv.DictReader(io.StringIO(text)))
        assert row["subject"] == "'=HYPERLINK(\"http://x\")"
        assert row["display_name"] == "'@SUM(A1)"
        assert row["model"] == "'-cmd"
        assert row["kind"] == "'+cmd"
        assert row["meaning"] == "'=1+1 and more"  # leading whitespace stripped, then escaped

    def test_the_escape_alone_covers_tab_cr_and_leading_whitespace(self):
        for lead in ("=", "+", "-", "@", "\t", "\r"):
            assert mailexport.formula_safe(lead + "x") == "'" + lead + "x"
        assert mailexport.formula_safe("  =x") == "'  =x"
        assert mailexport.formula_safe("plain") == "plain"

    def test_the_sanitiser_runs_first_and_is_required(self, tmp_path):
        hostile_state(tmp_path)
        seen: list[str] = []
        mailexport.render(tmp_path, clean=lambda s: seen.append(s) or s)
        assert "=HYPERLINK(\"http://x\")" in seen and "  =1+1\tand more" in seen
        text, _, _ = mailexport.render(tmp_path, clean=lambda s: s.replace("=", "_"))
        assert "'=HYPERLINK" not in text and "_HYPERLINK" in text

    def test_every_field_is_quoted(self, tmp_path):
        hostile_state(tmp_path)
        text, _, _ = mailexport.render(tmp_path, clean=cli._plain)
        for line in text.splitlines():
            assert line.startswith('"') and line.endswith('"')
        assert '"2.0"' in text

    def test_a_clamped_row_is_marked(self, tmp_path):
        hostile_state(tmp_path)
        text, _, _ = mailexport.render(tmp_path, clean=cli._plain)
        [row] = list(csv.DictReader(io.StringIO(text)))
        assert (row["disposition_raw"], row["disposition_effective"], row["clamped"]) == (
            "archive", "surface", "yes (from archive)")


class TestRows:
    def test_every_ending_is_a_row_and_no_cell_is_blank(self, tmp_path):
        box = FakeMailbox([
            gmail("ok", frm="a@shop.example", subject="Order", at=T0),
            gmail("gated", frm="n@news.example", subject="News", at=T0 + 60,
                  list_id="<news.example>"),
            gmail("bad", frm="b@shop.example", subject="Bad", at=T0 + 120),
        ])

        def reader(bundle):
            subject = bundle["messages"][-1]["subject"]
            return "not json" if subject == "Bad" else (
                '{"disposition": "surface", "wanted": "nothing", "deadline": "none", '
                '"case": "no_case", "sender_case": "consistent", "suspicion": "none", '
                f'"payment_coordinates": [], "citations": ["{bundle["mail"]}"], '
                '"meaning": "", "kind": "order"}')

        run(tmp_path, box, ScriptedReader(reader))
        header = mailindex.from_gmail(box.messages["ok"], frozenset({OWNER})).header
        mailverdicts.VerdictStore(tmp_path).write(
            mailverdicts.error_record(header, mailwatch.load_reader(), "other:model",
                                      RuntimeError("boom")))
        path, rows, _ = mailexport.write(tmp_path / "out.csv", tmp_path, clean=cli._plain)
        table = read_csv(path)
        assert rows == len(table) == 4
        assert sorted(r["ending"] for r in table) == ["error", "gated", "invalid", "ok"]
        assert all(value for r in table for value in r.values())
        by = {r["ending"]: r for r in table}
        assert by["gated"]["disposition_raw"] == "gated"
        assert by["gated"]["newsletter_gated_by"] == "List-Id"
        assert by["invalid"]["wanted"] == "no_verdict"
        assert by["error"]["owner_wrote_days"] == "not recorded"
        assert by["ok"]["meaning"] == "(empty)" and by["ok"]["owner_wrote_days"] == "never"

    def test_a_verdict_with_no_index_entry_says_so(self, tmp_path):
        seed(tmp_path, digest_of="f" * 64, record={"model": "m", "ending": "ok", "at": T0,
                                                    "verdict": {"disposition": "surface"}})
        [row] = read_csv(mailexport.write(tmp_path / "o.csv", tmp_path, clean=cli._plain)[0])
        assert row["subject"] == "(not in the index)"
        # A partial verdict — only corrupt or hand-written bytes make one —
        # still leaves no cell blank.
        assert all(row.values())
        assert row["wanted"] == row["kind"] == "(missing)"
        assert row["disposition_raw"] == "surface"

    def test_a_gated_ending_never_reads_not_gated(self, tmp_path):
        seed(tmp_path, digest_of="e" * 64, record={"model": "m", "ending": "gated", "at": T0,
                                                    "verdict": None})
        [row] = read_csv(mailexport.write(tmp_path / "o.csv", tmp_path, clean=cli._plain)[0])
        assert (row["ending"], row["newsletter_gated_by"]) == ("gated", "?")


class TestBacktestExport:
    def test_one_row_per_mail_and_arm_with_truth_recomputed(self, tmp_path):
        result = backtest(tmp_path, {ARM_A: ScriptedReader(careful),
                                     ARM_B: ScriptedReader(expire_everything)})
        written = mailbacktest.export_csv(tmp_path / "run", cli._plain)
        path, rows = written.path, written.rows
        table = read_csv(path)
        assert rows == 2 * result["selected"]
        assert {r["model"] for r in table} == {ARM_A, ARM_B}
        per_mail: dict[str, int] = {}
        for r in table:
            per_mail[r["digest"]] = per_mail.get(r["digest"], 0) + 1
        assert set(per_mail.values()) == {2}
        assert "truth_recomputed_at_export" in table[0]
        truths = {r["subject"]: r["truth_recomputed_at_export"] for r in table}
        assert truths["Car?"] == "replied" and truths["Last chance"] == "trashed"

    def test_the_labels_are_re_unioned_exactly_as_the_backfill_unions_them(self, tmp_path):
        state = tmp_path / "state"
        run_ = mailbacktest.backfill(history(), state, frozenset({OWNER}), days=90, now=NOW)
        assert mailbacktest.recorded_labels(state) == run_.labels

    def test_every_arm_cell_of_the_report_is_a_filter_over_the_columns(self, tmp_path):
        result = backtest(tmp_path, {ARM_A: ScriptedReader(careful),
                                     ARM_B: ScriptedReader(expire_everything)})
        table = read_csv(mailbacktest.export_csv(tmp_path / "run", cli._plain).path)
        truth, tier = "truth_recomputed_at_export", "tier_recomputed_at_export"

        def count(rows, col):
            out: dict[str, dict[str, int]] = {}
            for r in rows:
                key = f"{r[tier]}:{r[truth]}"
                out.setdefault(key, {})
                out[key][r[col]] = out[key].get(r[col], 0) + 1
            return out

        for spec, arm in result["arms"].items():
            mine = [r for r in table if r["model"] == spec]

            assert arm["read"] == len(mine)
            endings: dict[str, int] = {}
            for r in mine:
                endings[r["ending"]] = endings.get(r["ending"], 0) + 1
            assert arm["endings"] == endings
            assert arm["disposition_by_truth"] == count(mine, "disposition_raw")
            assert arm["disposition_by_truth_clamped"] == count(mine, "disposition_effective")
            replied = [dict(r, **{truth: "replied" if r[truth] == "replied" else "not_replied"})
                       for r in mine]
            assert arm["wanted_by_reply"] == count(replied, "wanted")
            for raw_key, col in (("destructive_expire_on_kept", "disposition_raw"),
                                 ("destructive_expire_on_kept_clamped", "disposition_effective")):
                kept: dict[str, int] = {}
                for r in mine:
                    if r[col] == "expire_after_grace" and r[truth] in mailbacktest.KEPT:
                        kept[r[truth]] = kept.get(r[truth], 0) + 1
                assert arm[raw_key] == kept
            assert arm["archive_on_replied"] == sum(
                r["disposition_raw"] == "archive" and r[truth] == "replied" for r in mine)
            assert arm["archive_on_replied_clamped"] == sum(
                r["disposition_effective"] == "archive" and r[truth] == "replied" for r in mine)
            assert arm["clamped"] == sum(r["clamped"].startswith("yes") for r in mine)
            gated: dict[str, dict[str, int]] = {}
            for r in mine:
                if r["ending"] == "gated":
                    gated.setdefault(r["newsletter_gated_by"], {})
                    gated[r["newsletter_gated_by"]][r[truth]] = (
                        gated[r["newsletter_gated_by"]].get(r[truth], 0) + 1)
            assert arm["gated_by_truth"] == gated

    def test_export_only_makes_no_mailbox_call(self, tmp_path, monkeypatch, capsys):
        backtest(tmp_path, {ARM_A: ScriptedReader(careful)})

        def refuse(*_a, **_k):
            raise AssertionError("export-only touched the mailbox")

        monkeypatch.setattr(mailwatch, "GwsMailbox", refuse)
        monkeypatch.setattr(mailwatch, "account_env", refuse)
        monkeypatch.setattr(mailexport, "interactive", lambda: True)
        assert mailbacktest.main(["--csv", str(tmp_path / "run")]) == 0
        out = capsys.readouterr().out
        assert "truth recomputed at export" in out
        [path] = list(mailexport.downloads_dir().glob("aish-verdicts-run-*.csv"))
        assert f"written to {path}" in out

    def test_export_only_refuses_a_directory_that_is_not_a_run(self, tmp_path):
        assert mailbacktest.main(["--csv", str(tmp_path)]) == 2

    def test_the_report_names_raw_and_clamped_counts(self, tmp_path):
        mailbox = FakeMailbox([
            gmail("o", frm=OWNER, to="agent@insure.example", subject="Policy", body="?",
                  at=T0, message_id="o@example.org", labels=("SENT",)),
            gmail("a", frm="agent@insure.example", subject="Re: Policy", body="Attached.",
                  at=T0 + 3 * DAY, in_reply_to="o@example.org", labels=()),
        ])
        charter = reader_charter()
        admit(tmp_path / "ledger", charter, ARM_A)
        result = mailbacktest.run_backtest(
            mailbox=mailbox, owner_addresses=frozenset({OWNER}),
            arms={ARM_A: ScriptedReader(
                lambda b: '{"disposition": "archive", "wanted": "nothing", "deadline": "none", '
                          '"case": "no_case", "sender_case": "consistent", "suspicion": "none", '
                          f'"payment_coordinates": [], "citations": ["{b["mail"]}"], '
                          '"meaning": "m", "kind": "k"}')},
            charter=charter, out_dir=tmp_path / "run", days=90, now=T0 + 10 * DAY,
            admission_state_dir=tmp_path / "ledger")
        arm = result["arms"][ARM_A]
        assert arm["clamped"] == 1
        assert arm["disposition_by_truth"] == {"weak:archived": {"archive": 1}}
        assert arm["disposition_by_truth_clamped"] == {"weak:archived": {"surface": 1}}
        text = (tmp_path / "run" / "report.txt").read_text()
        assert "after the live-conversation clamp (1 reading(s) clamped)" in text


class TestWiring:
    def test_mail_export_writes_the_live_file_and_prints_its_path(self, tmp_path, capsys,
                                                                  monkeypatch):
        from aish.agent import Agent
        from aish.session import SessionLog

        hostile_state(tmp_path)
        monkeypatch.setattr(mailexport, "interactive", lambda: True)
        agent = Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)
        logref = cli.LogRef(SessionLog.new(tmp_path))
        assert cli.handle_slash("/mail export", agent, logref, tmp_path) == "handled"
        out = capsys.readouterr().out
        [path] = list(mailexport.downloads_dir().glob("aish-verdicts-live-*.csv"))
        assert str(path) in out and "1 row(s)" in out
        assert "truth_recomputed_at_export" not in path.read_text(encoding="utf-8-sig")
        assert "/mail export" in cli.SLASH_HELP

    def test_unknown_words_still_print_usage(self, tmp_path, capsys):
        cli.show_mail("/mail export one two", tmp_path)
        assert "/mail export" in capsys.readouterr().out


class TestWhereItLands:
    """Downloads by default, from a terminal only; every other outcome lands in
    the old place and SAYS what was observed. The real ~/Downloads is never
    resolved here (`no_real_downloads` in conftest points the seam away)."""

    def export(self, tmp_path, **kwargs):
        kwargs.setdefault("now", T0)
        return mailexport.export(tmp_path, clean=cli._plain, run="r1",
                                 fallback_dir=tmp_path / "fallback", **kwargs)

    def test_the_suite_never_resolves_the_real_downloads(self):
        from pathlib import Path

        assert mailexport.downloads_dir() != Path.home() / "Downloads"

    def test_from_a_terminal_it_lands_in_downloads_with_the_run_stamp(self, tmp_path,
                                                                       monkeypatch):
        hostile_state(tmp_path)
        monkeypatch.setattr(mailexport, "interactive", lambda: True)
        written = self.export(tmp_path)
        stamp = mailexport.file_name("r1", T0)
        assert written.path == mailexport.downloads_dir() / stamp
        assert stamp.startswith("aish-verdicts-r1-") and written.note == ""
        assert len(read_csv(written.path)) == 1

    def test_outside_a_terminal_downloads_is_never_touched(self, tmp_path, monkeypatch):
        hostile_state(tmp_path)
        monkeypatch.setattr(mailexport, "interactive", lambda: False)

        def refuse():
            raise AssertionError("a background export resolved the Downloads folder")

        monkeypatch.setattr(mailexport, "downloads_dir", refuse)
        written = self.export(tmp_path)
        assert written.path.parent == tmp_path / "fallback"
        assert "not run from a terminal" in written.note
        assert f"written to {tmp_path / 'fallback'} instead" in written.note

    def test_a_missing_downloads_folder_falls_back_and_says_so(self, tmp_path, monkeypatch):
        hostile_state(tmp_path)
        monkeypatch.setattr(mailexport, "interactive", lambda: True)
        monkeypatch.setenv(mailexport.DOWNLOADS_ENV, str(tmp_path / "no-such-folder"))
        written = self.export(tmp_path)
        assert written.path.parent == tmp_path / "fallback" and written.path.is_file()
        assert f"{tmp_path / 'no-such-folder'} does not exist" in written.note

    def test_an_unwritable_downloads_folder_falls_back_and_says_so(self, tmp_path,
                                                                    monkeypatch):
        hostile_state(tmp_path)
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            monkeypatch.setattr(mailexport, "interactive", lambda: True)
            monkeypatch.setenv(mailexport.DOWNLOADS_ENV, str(locked))
            written = self.export(tmp_path)
        finally:
            locked.chmod(0o700)
        assert written.path.parent == tmp_path / "fallback" and written.path.is_file()
        assert "is not writable" in written.note
        assert list(locked.iterdir()) == []

    def test_a_write_refused_in_downloads_falls_back_and_says_what_was_refused(
            self, tmp_path, monkeypatch):
        hostile_state(tmp_path)
        monkeypatch.setattr(mailexport, "interactive", lambda: True)
        real = mailexport._publish_new

        def refusing(directory, name, payload):
            if directory == mailexport.downloads_dir():
                raise PermissionError(1, "Operation not permitted")
            return real(directory, name, payload)

        monkeypatch.setattr(mailexport, "_publish_new", refusing)
        written = self.export(tmp_path)
        assert written.path.parent == tmp_path / "fallback"
        assert "failed (Operation not permitted)" in written.note

    def test_two_exports_in_one_second_never_overwrite_each_other(self, tmp_path,
                                                                   monkeypatch):
        hostile_state(tmp_path)
        for interactive in (True, False):
            monkeypatch.setattr(mailexport, "interactive", lambda i=interactive: i)
            first = self.export(tmp_path)
            second = self.export(tmp_path)
            third = self.export(tmp_path)
            assert len({first.path, second.path, third.path}) == 3
            assert second.path.name == mailexport.file_name("r1", T0).replace(".csv", "-2.csv")
            assert all(len(read_csv(w.path)) == 1 for w in (first, second, third))

    def test_an_explicit_file_is_written_as_named(self, tmp_path, monkeypatch):
        hostile_state(tmp_path)
        monkeypatch.setattr(mailexport, "interactive", lambda: False)
        target = tmp_path / "mine.csv"
        written = self.export(tmp_path, out_file=target)
        assert written.path == target and written.note == ""

    def test_an_explicit_directory_gets_the_generated_name(self, tmp_path):
        hostile_state(tmp_path)
        (tmp_path / "here").mkdir()
        written = self.export(tmp_path, out_file=tmp_path / "here")
        assert written.path == tmp_path / "here" / mailexport.file_name("r1", T0)

    def test_an_explicit_path_that_cannot_be_written_falls_back_and_says_so(self, tmp_path):
        hostile_state(tmp_path)
        (tmp_path / "a-file").write_text("not a directory")
        target = tmp_path / "a-file" / "x.csv"
        written = self.export(tmp_path, out_file=target)
        assert written.path.parent == tmp_path / "fallback" and written.path.is_file()
        assert f"could not write {target}" in written.note


class TestExportSurfaces:
    def test_mail_export_takes_a_path(self, tmp_path, capsys):
        hostile_state(tmp_path)
        target = tmp_path / "out.csv"
        cli.show_mail(f"/mail export {target}", tmp_path)
        out = capsys.readouterr().out
        assert str(target) in out and target.is_file()

    def test_mail_export_outside_a_terminal_names_where_it_went(self, tmp_path, capsys,
                                                                monkeypatch):
        hostile_state(tmp_path)
        monkeypatch.setattr(mailexport, "interactive", lambda: False)
        cli.show_mail("/mail export", tmp_path)
        out = capsys.readouterr().out
        [path] = list((tmp_path / "mail" / "exports").glob("aish-verdicts-live-*.csv"))
        assert str(path) in out and "not run from a terminal" in out

    def test_csv_out_file_overrides_and_needs_csv(self, tmp_path, capsys):
        import pytest

        backtest(tmp_path, {ARM_A: ScriptedReader(careful)})
        target = tmp_path / "review.csv"
        assert mailbacktest.main(["--csv", str(tmp_path / "run"), "--out-file",
                                  str(target)]) == 0
        assert f"written to {target}" in capsys.readouterr().out and target.is_file()
        with pytest.raises(SystemExit):
            mailbacktest.main(["--out-file", str(target)])

    def test_csv_outside_a_terminal_lands_in_the_run_directory_and_says_so(
            self, tmp_path, capsys, monkeypatch):
        backtest(tmp_path, {ARM_A: ScriptedReader(careful)})
        monkeypatch.setattr(mailexport, "interactive", lambda: False)
        assert mailbacktest.main(["--csv", str(tmp_path / "run")]) == 0
        out = capsys.readouterr().out
        [path] = list((tmp_path / "run").glob("aish-verdicts-run-*.csv"))
        assert f"written to {path}" in out and "not run from a terminal" in out
