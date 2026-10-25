"""The history backtest over Tuta (2026-10-26 directive): enumeration never
SEARCH, no \\Seen, identity-not-UID; flags and folders as tiers; his Sent
folder's in-thread reply as `replied`, past-facing; a missing Trash or flags
list stated, never guessed; a folder shorter than the window stated; and one
tier-weighted selection across both inboxes under one --reader-max. A fake
bridge (`FakeHimalaya`) and a fake Gmail; no process, no network."""

from __future__ import annotations

from mail_fixtures import DAY, OWNER, T0, ScriptedReader, admit, reader_charter
from test_mailbacktest import ARM_A, NOW, careful, history
from test_mailtuta import TUTA_OWNER, FakeHimalaya, rfc822, tuta

from aish import cli, mailbacktest, mailtuta
from aish.mailsource import Outcome

OWNERS = frozenset({OWNER, TUTA_OWNER})


def tuta_folders() -> dict[str, list[str]]:
    return {
        "INBOX": [
            rfc822(frm="Ola <ola@friends.example>", subject="Bike?", body="Your bike or mine?",
                   at=T0, message_id="tq@friends.example"),
            rfc822(frm="hello@seeds.example", subject="Seeds", at=T0 + 4 * DAY,
                   message_id="ts@seeds.example"),
            rfc822(frm="notes@club.example", subject="Flagged note", at=T0 + 5 * DAY,
                   message_id="tf@club.example"),
            rfc822(frm="orders@pots.example", subject="Kept", at=T0 + 6 * DAY,
                   message_id="tk@pots.example"),
        ],
        "Sent": [
            rfc822(frm=TUTA_OWNER, to="ola@friends.example", subject="Re: Bike?", body="Mine.",
                   at=T0 + 2 * DAY, message_id="tr@tuta.example",
                   extra=("In-Reply-To: <tq@friends.example>",)),
        ],
        "Trash": [
            rfc822(frm="promo@shout.example", subject="Final hours", at=T0 + DAY,
                   message_id="tt@shout.example"),
        ],
    }


def tuta_flags(kept=("\\Seen",)) -> dict[str, list]:
    return {"INBOX": [["\\Seen"], [], ["\\Seen", "\\Flagged"], list(kept) if kept is not None
                      else None], "Sent": [["\\Seen"]], "Trash": [["\\Seen"]]}


def bridge(**kwargs) -> FakeHimalaya:
    kwargs.setdefault("flags", tuta_flags())
    return FakeHimalaya(tuta_folders(), **kwargs)


def go(tmp_path, *, mailbox=None, himalaya=None, reader_max=50, days=90, arms=None, out="run"):
    charter = reader_charter()
    ledger = tmp_path / "real-state"
    arms = {ARM_A: ScriptedReader(careful)} if arms is None else arms
    for spec in arms:
        admit(ledger, charter, spec)
    return mailbacktest.run_backtest(
        mailbox=mailbox, tuta=tuta(himalaya) if himalaya is not None else None,
        owner_addresses=OWNERS, arms=arms, charter=charter, out_dir=tmp_path / out,
        days=days, reader_max=reader_max, now=NOW, admission_state_dir=ledger)


def tuta_truths(result) -> dict[str, int]:
    return {label: n for labels in result["truth_tiers"].values() for label, n in labels.items()}


class TestTutaTruth:
    def test_folders_flags_and_his_sent_folder_become_tiers(self, tmp_path):
        result = go(tmp_path, himalaya=bridge())
        assert result["truth_tiers"] == {
            "strong": {"replied": 1, "trashed": 1, "starred": 1},
            "weak": {"kept_read": 1},
            "near-null": {"unread": 1},
        }

    def test_listing_is_enumeration_with_flags_never_search_never_seen(self, tmp_path):
        himalaya = bridge()
        go(tmp_path, himalaya=himalaya)
        for argv in himalaya.calls:
            mailtuta.check_argv(argv)  # every call the run made is inside the fence
            assert not any("search" in a.lower() or "seen" in a.lower() for a in argv)
        fetches = [c for c in himalaya.calls if c[:2] == ["imap", "fetch"]]
        assert fetches and all("--flags" in c for c in fetches)
        assert {c[c.index("-m") + 1] for c in fetches} == {"INBOX", "Sent", "Trash"}

    def test_the_reply_is_in_the_future_of_the_mail_it_answers(self, tmp_path):
        reader = ScriptedReader(careful)
        go(tmp_path, himalaya=bridge(), arms={ARM_A: reader})
        bike = next(b for b in reader.bundles if b["messages"][-1]["subject"] == "Bike?")
        assert [m["subject"] for m in bike["messages"]] == ["Bike?"]

    def test_a_page_without_flags_is_seen_unknown_and_set_apart(self, tmp_path):
        result = go(tmp_path, himalaya=bridge(flags=tuta_flags(kept=None)))
        assert result["truth_tiers"]["set-apart"] == {"seen_unknown": 1}
        assert "kept_read" not in tuta_truths(result)
        assert result["selected"] == 4  # the unknown one is never judged

    def test_no_trash_folder_is_said_and_its_mail_is_absent(self, tmp_path):
        himalaya = bridge(trash=False)
        result = go(tmp_path, himalaya=himalaya)
        assert "trashed" not in tuta_truths(result)
        assert not any("Trash" in c for c in himalaya.calls)
        text = (tmp_path / "run" / "report.txt").read_text()
        assert "the bridge exposes no Trash folder" in text
        assert "ABSENT from every count" in text

    def test_a_folder_shorter_than_the_window_is_a_stated_fact(self, tmp_path):
        go(tmp_path, himalaya=bridge(), days=90)
        text = (tmp_path / "run" / "report.txt").read_text()
        assert "tuta INBOX: the folder holds 4 message(s)" in text
        assert "shorter than the 90-day window" in text

    def test_a_folder_deeper_than_the_window_is_not_called_shorter(self, tmp_path):
        go(tmp_path, himalaya=bridge(), days=10)
        text = (tmp_path / "run" / "report.txt").read_text()
        assert "shorter than" not in text

    def test_a_failed_tuta_listing_is_stated_and_gmail_still_runs(self, tmp_path):
        himalaya = bridge()
        box = mailtuta.TutaMailbox({}, himalaya, status_path=tmp_path / "none.json")
        charter = reader_charter()
        admit(tmp_path / "ledger", charter, ARM_A)
        result = mailbacktest.run_backtest(
            mailbox=history(), tuta=box, owner_addresses=OWNERS,
            arms={ARM_A: ScriptedReader(careful)}, charter=charter, out_dir=tmp_path / "run",
            days=90, now=NOW, admission_state_dir=tmp_path / "ledger")
        assert result["sources"]["tuta"]["listing_failed"] == 1
        assert result["truth_tiers"]["strong"]["replied"] == 1  # Gmail's half
        text = (tmp_path / "run" / "report.txt").read_text()
        assert "the Tuta LISTING failed" in text and "TUTA_BRIDGE_PASSWORD" in text


class TestUnstableIds:
    def test_a_resumed_run_after_a_renumber_refetches_nothing_and_keeps_its_truth(
            self, tmp_path):
        first = go(tmp_path, himalaya=bridge())
        himalaya = bridge()
        himalaya.renumber(100)
        second = go(tmp_path, himalaya=himalaya)
        assert [c for c in himalaya.calls if c[:2] == ["message", "read"]] == []
        assert second["sources"]["tuta"]["held"] == first["sources"]["tuta"]["fetched"]
        assert second["truth_tiers"] == first["truth_tiers"]

    def test_the_log_keys_tuta_by_digest_never_by_uid(self, tmp_path):
        state = tmp_path / "state"
        run = mailbacktest.backfill(None, state, OWNERS, days=90, now=NOW, tuta=tuta(bridge()))
        logged = mailbacktest._read_fetched(state)
        assert logged and all(key.startswith("tuta-digest:") for key in logged)
        assert mailbacktest.recorded_labels(state) == run.labels

    def test_a_renumber_between_listing_and_fetch_costs_the_flags_never_guesses(
            self, tmp_path):
        himalaya = bridge()

        def renumbering(args, env) -> Outcome:
            if args[:2] == ["message", "read"] and himalaya.uid_base == 1:
                himalaya.renumber(-1)  # the bridge restarted after the listing
            return himalaya(args, env)

        box = mailtuta.TutaMailbox({mailtuta.SECRET_ENV: "x"}, renumbering,
                                   status_path=tmp_path / "none.json")
        run = mailbacktest.backfill(None, tmp_path / "state", OWNERS, days=90, now=NOW,
                                    tuta=box)
        assert box.counts.get("renumbered_between_list_and_fetch", 0) > 0
        inbox_labels = [labels for labels in run.labels.values() if "INBOX" in labels]
        assert inbox_labels
        assert all(mailbacktest.SEEN_UNKNOWN in labels for labels in inbox_labels)


class TestBothInboxes:
    def test_one_selection_tier_weighted_across_both_under_one_cap(self, tmp_path):
        result = go(tmp_path, mailbox=history(), himalaya=bridge(), reader_max=4)
        assert result["selected"] == 4
        assert set(result["selected_by_mailbox"]) == {"gmail", "tuta"}
        rows = result["arms"][ARM_A]["disposition_by_truth"]
        assert {row.split(":")[0] for row in rows} == {"strong"}
        # The window totals are both inboxes' together.
        sources = result["sources"]
        assert result["window"]["listed"] == sources["gmail"]["listed"] + \
            sources["tuta"]["listed"]
        text = (tmp_path / "run" / "report.txt").read_text()
        assert "4 selected mail(s) (gmail 2, tuta 2)" in text
        assert "gmail: trashed inbound observed" in text and "tuta: trashed inbound" in text

    def test_the_review_file_recomputes_tuta_truth_like_gmail_truth(self, tmp_path,
                                                                     monkeypatch):
        from aish import mailexport

        monkeypatch.setattr(mailexport, "interactive", lambda: False)
        go(tmp_path, mailbox=history(), himalaya=bridge())
        written = mailbacktest.export_csv(tmp_path / "run", cli._plain)
        from test_mailexport import read_csv

        table = {r["subject"]: r for r in read_csv(written.path)}
        assert table["Bike?"]["truth_recomputed_at_export"] == "replied"
        assert table["Bike?"]["mailbox"] == "tuta"
        assert table["Final hours"]["truth_recomputed_at_export"] == "trashed"
        assert table["Seeds"]["truth_recomputed_at_export"] == "unread"
        assert table["Car?"]["truth_recomputed_at_export"] == "replied"


class TestSources:
    def _stub(self, monkeypatch, *, tuta_allowed: bool):
        captured: dict = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return {}

        monkeypatch.setattr(mailbacktest, "run_backtest", fake_run)
        monkeypatch.setattr(mailbacktest, "render", lambda _r: "")
        monkeypatch.setattr(mailbacktest.mailwatch, "load_reader", reader_charter)

        def no_tuta(*_a, **_k):
            raise AssertionError("the Tuta bridge was opened")

        if not tuta_allowed:
            monkeypatch.setattr(mailtuta, "TutaMailbox", no_tuta)
            monkeypatch.setattr(mailbacktest.mailwatch, "tuta_env", no_tuta)
        return captured

    def test_the_default_is_gmail_only(self, tmp_path, monkeypatch):
        captured = self._stub(monkeypatch, tuta_allowed=False)
        monkeypatch.setattr(mailbacktest.mailwatch, "account_env", lambda a: ({}, ""))
        monkeypatch.setattr(mailbacktest.mailwatch, "GwsMailbox", lambda env: "gmail-box")
        assert mailbacktest.main(["--account", OWNER, "--out", str(tmp_path / "r")]) == 0
        assert captured["mailbox"] == "gmail-box" and captured["tuta"] is None

    def test_tuta_alone_needs_no_gmail_account(self, tmp_path, monkeypatch):
        captured = self._stub(monkeypatch, tuta_allowed=True)

        def refuse(*_a, **_k):
            raise AssertionError("a Gmail account was resolved")

        monkeypatch.setattr(mailbacktest.mailwatch, "account_env", refuse)
        monkeypatch.setattr(mailbacktest.mailwatch, "tuta_env",
                            lambda: ({mailtuta.SECRET_ENV: "x"}, TUTA_OWNER))
        assert mailbacktest.main(["--source", "tuta", "--out", str(tmp_path / "r")]) == 0
        assert captured["mailbox"] is None
        assert isinstance(captured["tuta"], mailtuta.TutaMailbox)
        assert TUTA_OWNER in captured["owner_addresses"]

    def test_both_sources_repeatable(self, tmp_path, monkeypatch):
        captured = self._stub(monkeypatch, tuta_allowed=True)
        monkeypatch.setattr(mailbacktest.mailwatch, "account_env", lambda a: ({}, ""))
        monkeypatch.setattr(mailbacktest.mailwatch, "GwsMailbox", lambda env: "gmail-box")
        monkeypatch.setattr(mailbacktest.mailwatch, "tuta_env", lambda: ({}, ""))
        assert mailbacktest.main(["--account", OWNER, "--source", "gmail", "--source", "tuta",
                                  "--out", str(tmp_path / "r")]) == 0
        assert captured["mailbox"] == "gmail-box" and captured["tuta"] is not None
