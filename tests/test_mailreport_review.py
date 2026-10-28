"""The daily inbox report: the delivery review's findings, each pinned (#142 W5).

B1 a copy in the other inbox coming back the next day; B2 readings in no
section and in no count; M1 a deduped answer recorded as shown; M2 a cold
reopen handing the model a second copy; M4 a squatted Message-ID merging two
mails; M5 a dropped connection called "failed"; M6 an over-cap Needs item
hiding a follow-up; M7 an answer in a new thread read as "no reply"; M8 the
claude-max present path; and an empty report."""

from __future__ import annotations

from test_mailreport import (
    DAY,
    NOW,
    FakePost,
    add_mail,
    add_verdict,
    build,
    conversation,
    fresh_health,
    identity,
    ok,
    run,
    subjects,
)
from test_mailreport_chat import trigger, wait_idle
from test_server import app_env, make_client

from aish import agent as agent_module
from aish import mailreport
from aish.session import SessionLog

__all__ = ["app_env"]  # the fixture, imported for pytest


class TestTheReportFindings:
    def test_a_copy_in_the_other_inbox_does_not_come_back_the_next_day(self, tmp_path):
        """B1: the dropped copy is recorded as shown with the one that was."""
        fresh_health(tmp_path)
        tuta = add_mail(tmp_path, "t", frm="a@x.example", subject="twice", at=NOW - DAY,
                        mid="same@x.example", mailbox="tuta")
        add_verdict(tmp_path, tuta, wanted="pay", disposition="hold")
        gm = add_mail(tmp_path, "g", frm="a@x.example", subject="twice", at=NOW - DAY + 5,
                      mid="same@x.example")
        add_verdict(tmp_path, gm, wanted="pay", disposition="hold")
        run(tmp_path, FakePost([ok()]))
        assert sorted(mailreport.read_ledger(tmp_path)[-1]["digests"]) == sorted(
            [tuta.digest, gm.digest])
        assert build(tmp_path, now=NOW + DAY).needs == []

    def test_one_message_id_from_two_senders_is_two_mails(self, tmp_path):
        """M4: a squatted Message-ID never merges a stranger's mail with a real one."""
        fresh_health(tmp_path)
        real = add_mail(tmp_path, "t", frm="bank@bank.example", subject="real", at=NOW - DAY,
                        mid="same@x.example", mailbox="tuta")
        add_verdict(tmp_path, real, wanted="pay", disposition="hold")
        fake = add_mail(tmp_path, "g", frm="eve@evil.example", subject="fake", at=NOW - DAY,
                        mid="same@x.example")
        add_verdict(tmp_path, fake, wanted="pay", disposition="hold")
        assert sorted(subjects(build(tmp_path).needs)) == ["fake", "real"]

    def test_every_eligible_reading_is_in_a_section_or_counted(self, tmp_path):
        """B2: cannot_tell, wait, and nothing-but-not-surface are counted, never silent."""
        fresh_health(tmp_path)
        rows = (("cannot_tell", "hold"), ("wait", "surface"), ("nothing", "hold"),
                ("nothing", "archive"))
        for n, (wanted, disposition) in enumerate(rows):
            h = add_mail(tmp_path, f"m{n}", frm=f"s{n}@x.example", subject=wanted, at=NOW - DAY)
            add_verdict(tmp_path, h, wanted=wanted, disposition=disposition)
        report = build(tmp_path)
        assert report.excluded == {"undecided": 1, "waiting": 1, "quiet": 2}
        text = mailreport.render(report, clean=identity)
        assert ("1 the Reader could not tell what they want of you, 1 that ask you to wait, "
                "2 that ask nothing and would be held or filed") in text

    def test_a_deduped_answer_records_nothing_as_shown_and_keeps_the_window(self, tmp_path):
        """M1: the day's chat was made by an earlier attempt with its own report."""
        fresh_health(tmp_path)
        h = add_mail(tmp_path, "a", frm="a@x.example", subject="a", at=NOW - DAY)
        add_verdict(tmp_path, h, wanted="pay", disposition="hold")
        assert run(tmp_path, FakePost([ok(deduped=True)]))[0] == 0
        entry = mailreport.read_ledger(tmp_path)[-1]
        assert entry["status"] == "deduped" and "digests" not in entry
        assert build(tmp_path).since_from == "first report"

    def test_a_since_run_has_its_own_dedup_key(self, tmp_path):
        fresh_health(tmp_path)
        post = FakePost([ok()])
        run(tmp_path, post, since=NOW - 3 * DAY)
        assert post.calls[0][1]["meta"]["dedup_key"].endswith(f":since:{int(NOW - 3 * DAY)}")

    def test_a_connection_dropped_mid_exchange_is_unknown(self, tmp_path):
        """M5: only a refused connection or an unresolved name proves it did not arrive."""
        fresh_health(tmp_path)
        post = FakePost([ConnectionResetError(54, "reset by peer")]
                        * (len(mailreport.RETRY_WAITS_S) + 1))
        assert run(tmp_path, post)[0] == 1
        assert mailreport.read_ledger(tmp_path)[-1]["status"] == "unknown"

    def test_an_answer_over_the_needs_cap_is_still_a_follow_up(self, tmp_path):
        """M6: only what Needs SHOWS suppresses a follow-up."""
        fresh_health(tmp_path)
        for n in range(mailreport.NEEDS_SHOWN):
            h = add_mail(tmp_path, f"m{n}", frm=f"s{n}@x.example", subject=f"s{n}",
                         at=NOW - DAY / 2 + n)
            add_verdict(tmp_path, h, wanted="pay", deadline="2026-01-01", disposition="hold")
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=NOW - DAY / 2 - 100)
        report = build(tmp_path)
        assert len(report.needs) == mailreport.NEEDS_SHOWN + 1
        assert [f.counterparty for f in report.yours] == ["bob@client.example"]

    def test_an_answer_in_a_new_thread_is_not_their_turn(self, tmp_path):
        """M7: per counterparty means across the index, not inside one union."""
        fresh_health(tmp_path)
        conversation(tmp_path, his_at=NOW - 10 * DAY, reply_at=None)
        fresh = add_mail(tmp_path, "n", frm="bob@client.example", subject="New topic",
                         at=NOW - 2 * DAY)
        add_verdict(tmp_path, fresh, wanted="nothing", disposition="archive")
        assert build(tmp_path).theirs == []


class TestTheChatFindings:
    def test_a_cold_reopen_hands_the_model_the_pointer_not_a_second_copy(self, app_env):
        """M2: the log keeps what he saw AND what the model was given."""
        client, _ = make_client(app_env, [], token="secret")
        with client:
            name = trigger(client).json()["session"]
            wait_idle(client.app.state.server, name)
            path = client.app.state.server.state_dir / name
        restored = SessionLog._parse(path).messages
        answers = [m["content"] for m in restored if m.get("role") == "assistant"]
        assert answers == [agent_module.PRESENTED_POINTER]
        cold = SessionLog.reconstruct_events(path)
        assert "Invoice 41" in [e for e in cold if e["type"] == "done"][-1]["result"]

    def test_an_empty_report_is_refused(self, app_env):
        client, _ = make_client(app_env, [], token="secret")
        with client:
            assert trigger(client, report="   ").status_code == 400

    def test_claude_max_presents_with_no_sdk_call_and_hands_the_note_on(
        self, monkeypatch, tmp_path
    ):
        """M8: the wrapper's present records the turn and calls no SDK, and the
        note reaches the model with the next real prompt — recorded once."""
        from test_claude_max import make_max_agent

        agent, fake = make_max_agent(monkeypatch, tmp_path)
        recorded: list[dict] = []
        agent.on_message = recorded.append
        agent.add_outside_note("the report", "mail text 7731", source="inbox_report", kept=True)
        assert agent.present("show it", "REPORT BODY") == "REPORT BODY"
        assert fake.queries == []
        assert [m["role"] for m in recorded] == ["user", "user", "assistant"]
        assert agent.holds_kept_outside_note() and agent.inner._tainted
        agent.run_task("pay the first one")
        prompt = fake.queries[0][0]
        assert "mail text 7731" in prompt and prompt.endswith("pay the first one")
        assert sum("mail text 7731" in str(m.get("content")) for m in recorded) == 1
        assert agent.inner._tainted
