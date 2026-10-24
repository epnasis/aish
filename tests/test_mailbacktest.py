"""The backtest harness on a FAKE mailbox (#142 W5 S1): frozen clocks, tiered
ground truth, the destructive-direction headline, raw counts only."""

from __future__ import annotations

import json
import re

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

from aish import mailbacktest, mailindex

ARM_A, ARM_B = "gemini:test-flash", "local:test-35b"
NOW = T0 + 40 * DAY


def history() -> FakeMailbox:
    return FakeMailbox([
        # He replied to this one — strong truth, and its reply is in its FUTURE.
        gmail("q", frm="Ania <ania@friends.example>", subject="Car?", body="Your car or mine?",
              at=T0, message_id="q@friends.example", labels=("INBOX",)),
        gmail("qr", frm=OWNER, to="ania@friends.example", subject="Re: Car?", body="Mine.",
              at=T0 + 2 * DAY, in_reply_to="q@friends.example", labels=("SENT",)),
        # Trashed — strong.
        gmail("t", frm="promo@loud.example", subject="Last chance", at=T0 + DAY,
              labels=("TRASH",)),
        # Archived — weak.
        gmail("a", frm="orders@lampshop.example", subject="Order LS-48213 confirmed",
              at=T0 + 3 * DAY, labels=()),
        # Unread in the inbox — near-null.
        gmail("u", frm="hello@gardenstore.example", subject="Bulbs", at=T0 + 4 * DAY),
        # A newsletter he starred — the gate's own destructive direction.
        gmail("n", frm="digest@club.example", subject="Club digest", at=T0 + 5 * DAY,
              list_unsubscribe="<mailto:u@club.example>", labels=("STARRED",)),
        # Spam is Gmail's verdict, set apart.
        gmail("s", frm="x@spam.example", subject="WIN", at=T0 + 6 * DAY, labels=("SPAM",)),
        # Older than the window.
        gmail("old", frm="a@old.example", subject="Old", at=NOW - 200 * DAY),
    ])


def expire_everything(bundle):
    return verdict_json(disposition="expire_after_grace", citations=[bundle["mail"]])


def careful(bundle):
    subject = bundle["messages"][-1]["subject"]
    disposition = "cannot_tell" if subject == "Bulbs" else "surface"
    return verdict_json(disposition=disposition, citations=[bundle["mail"]])


def backtest(tmp_path, arms, reader_max=50):
    charter = reader_charter()
    state = tmp_path / "run" / "state"
    for spec in arms:
        admit(state, charter, spec)
    return mailbacktest.run_backtest(
        mailbox=history(), owner_addresses=frozenset({OWNER}), arms=arms, charter=charter,
        out_dir=tmp_path / "run", days=90, reader_max=reader_max, now=NOW,
    )


class TestTruth:
    def test_every_observation_lands_in_its_tier(self, tmp_path):
        result = backtest(tmp_path, {})
        assert result["truth_tiers"] == {
            "strong": {"replied": 1, "trashed": 1, "starred": 1},
            "weak": {"archived": 1},
            "near-null": {"unread": 1},
            "set-apart": {"spam": 1},
        }
        assert result["window"]["outside_window"] == 1

    def test_the_backfill_cannot_reach_a_model(self, tmp_path):
        """Structural: the deterministic backfill takes no chat seam and no model
        spec, so there is nothing a model call could ride in on."""
        import inspect

        params = set(inspect.signature(mailbacktest.backfill).parameters)
        assert not params & {"chat", "arms", "model_spec", "charter"}
        run = mailbacktest.backfill(history(), tmp_path, frozenset({OWNER}), days=90, now=NOW)
        assert run.counts["cases"] == 1 and run.counts["in_index"] == 7


class TestFrozenClocks:
    def test_a_mail_never_sees_its_future(self, tmp_path):
        reader = ScriptedReader(careful)
        backtest(tmp_path, {ARM_A: reader})
        car = next(b for b in reader.bundles if b["messages"][-1]["subject"] == "Car?")
        assert [m["subject"] for m in car["messages"]] == ["Car?"]
        assert car["sender"]["grade"] == "first-contact"  # his reply came LATER

    def test_the_index_answers_the_future_only_to_ground_truth(self, tmp_path):
        run = mailbacktest.backfill(history(), tmp_path, frozenset({OWNER}), days=90, now=NOW)
        car = next(h for h in run.index.by_digest.values() if h.subject == "Car?")
        assert len(run.index.conversation(car.digest)) == 1
        assert len(run.index.later_replies(car.digest)) == 1


class TestReport:
    def test_the_headline_is_the_destructive_direction(self, tmp_path):
        result = backtest(tmp_path, {ARM_A: ScriptedReader(expire_everything)})
        arm = result["arms"][ARM_A]
        # replied, archived and unread would have been expired; the trashed one
        # agrees with him and is not destructive; the starred newsletter was gated.
        assert arm["destructive_expire_on_kept"] == {"replied": 1, "archived": 1, "unread": 1}
        assert arm["gated_by_truth"] == {"List-Unsubscribe": {"starred": 1}}

    def test_cannot_tell_and_gated_are_columns_like_any_other(self, tmp_path):
        result = backtest(tmp_path, {ARM_A: ScriptedReader(careful)})
        table = result["arms"][ARM_A]["disposition_by_truth"]
        assert table["near-null:unread"] == {"cannot_tell": 1}
        assert table["strong:starred"] == {"gated": 1}

    def test_two_arms_that_disagree_are_listed_for_him(self, tmp_path):
        result = backtest(tmp_path, {ARM_A: ScriptedReader(careful),
                                     ARM_B: ScriptedReader(expire_everything)})
        subjects = {d["subject"] for d in result["disagreements"]}
        assert subjects == {"Car?", "Last chance", "Order LS-48213 confirmed", "Bulbs"}

    def test_the_report_prints_counts_and_never_a_rate_or_a_grade(self, tmp_path):
        backtest(tmp_path, {ARM_A: ScriptedReader(careful),
                            ARM_B: ScriptedReader(expire_everything)})
        text = (tmp_path / "run" / "report.txt").read_text()
        assert "%" not in text
        assert not re.search(r"\b(accuracy|precision|recall|fixed|passed|works|safe)\b",
                             text, re.I)
        assert "HEADLINE — would have expired mail he kept: 3 of 5" in text
        assert "Gmail empties Trash after 30 days" in text
        assert json.loads((tmp_path / "run" / "report.json").read_text())["selected"] == 5

    def test_the_selection_is_bounded_strong_first(self, tmp_path):
        result = backtest(tmp_path, {ARM_A: ScriptedReader(careful)}, reader_max=2)
        assert result["selected"] == 2
        assert set(result["arms"][ARM_A]["disposition_by_truth"]) <= {
            "strong:replied", "strong:trashed", "strong:starred"}


class TestResume:
    def test_a_second_run_reuses_what_was_bought(self, tmp_path):
        reader = ScriptedReader(careful)
        backtest(tmp_path, {ARM_A: reader})
        calls = reader.calls
        backtest(tmp_path, {ARM_A: reader})
        assert reader.calls == calls

    def test_the_run_never_writes_the_live_mail_store(self, tmp_path):
        backtest(tmp_path, {ARM_A: ScriptedReader(careful)})
        assert not (tmp_path / "mail").exists()
        assert (tmp_path / "run" / "state" / "mail" / "verdicts").is_dir()
        assert mailindex.MailIndex(tmp_path / "run" / "state").health.loaded == 7
