"""`aish-mailwatch`: the shadow pass end to end on a fake mailbox, the
read-only seam, fail-closed credentials, idempotency, and the record
(#142 W5 S1). No network, no process, no model, no sleeps."""

from __future__ import annotations

import json

import pytest
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

from aish import mailverdicts, mailwatch, roles, session, vocab

MODEL = "gemini:test-flash"
OWNERS = frozenset({OWNER})
IBAN_OLD = "PL27 1140 2004 0000 3002 0135 5387"
IBAN_NEW = "DE89 3704 0044 0532 0130 00"


def builder_mailbox() -> FakeMailbox:
    return FakeMailbox([
        gmail("i", frm="Builder Example <office@builder.example>", subject="Invoice 2026/41",
              body=f"Invoice 2026/41: 12 300,00 zł to {IBAN_OLD}, due 2026-10-31.", at=T0,
              message_id="i@builder.example", labels=("INBOX",)),
        gmail("r", frm=OWNER, to="office@builder.example", subject="Re: Invoice 2026/41",
              body="Thanks, will pay.", at=T0 + 60, in_reply_to="i@builder.example",
              labels=("SENT",)),
        gmail("c", frm="Builder Example <office@bui1der.example>",
              subject="Re: Invoice 2026/41",
              body=f"Our account changed. Pay 2026/41 to {IBAN_NEW}.",
              at=T0 + DAY, auth="fail", references="<i@builder.example>"),
        gmail("n", frm="news@gardenstore.example", subject="Bulbs", at=T0 + DAY + 5,
              list_id="<news.gardenstore.example>"),
    ])


def run(state_dir, mailbox, reader, *, model=MODEL, admitted=True):
    charter = reader_charter()
    if admitted:
        admit(state_dir, charter, model)
    return mailwatch.run_once(
        mailbox=mailbox, state_dir=state_dir, owner_addresses=OWNERS, model_spec=model,
        charter=charter, chat=reader, now=lambda: T0 + 2 * DAY,
    )


def records(state_dir) -> dict[str, dict]:
    return {r["provider_id"]: r for r in mailverdicts.iter_store(state_dir)}


class TestReadOnly:
    def test_a_mutating_gws_call_is_refused_before_any_process(self):
        spawned = []
        box = mailwatch.GwsMailbox({}, runner=lambda args, env: spawned.append(args))
        for verb in ("modify", "trash", "send", "delete", "batchModify"):
            with pytest.raises(mailwatch.ReadOnlyViolation):
                box._call(["gmail", "users", "messages", verb, "--params", "{}"])
        assert spawned == []

    def test_a_pass_makes_only_list_and_get_calls(self, tmp_path):
        box = builder_mailbox()
        run(tmp_path, box, ScriptedReader(lambda b: verdict_json(citations=[b["mail"]])))
        assert {call[0] for call in box.calls} == {"list", "get"}


class TestCredentials:
    def test_an_account_with_no_credential_directory_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GWS_ACCOUNTS_ROOT", str(tmp_path))
        env, why = mailwatch.account_env("someone@example.org")
        assert env is None and "no credential directory" in why
        assert mailwatch.main(["--once", "--account", "someone@example.org"]) == 2

    def test_the_env_is_the_per_account_file_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GWS_ACCOUNTS_ROOT", str(tmp_path))
        (tmp_path / OWNER).mkdir()
        env, _why = mailwatch.account_env(OWNER)
        assert env is not None
        assert env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] == "file"
        assert env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == str(tmp_path / OWNER)


class TestShadowPass:
    def test_every_new_inbound_mail_gets_one_recorded_verdict(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        result = run(tmp_path, builder_mailbox(), reader)
        held = records(tmp_path)
        assert set(held) == {"i", "c", "n"}  # inbound only; his sent mail is never judged
        assert held["n"]["ending"] == "gated" and held["i"]["ending"] == "ok"
        assert result.assessed == 3 and reader.calls == 2

    def test_a_second_pass_reads_nothing_again(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        run(tmp_path, builder_mailbox(), reader)
        calls = reader.calls
        again = run(tmp_path, builder_mailbox(), reader)
        assert reader.calls == calls and again.assessed == 0 and again.already_recorded == 3
        assert again.new_in_index == 0

    def test_a_mail_seen_before_admission_is_read_once_admitted(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        first = run(tmp_path, builder_mailbox(), reader, admitted=False)
        assert first.endings == {"unadmitted": 2, "gated": 1} and reader.calls == 0
        second = run(tmp_path, builder_mailbox(), reader)
        assert second.assessed == 2 and reader.calls == 2

    def test_the_record_carries_the_deterministic_flags(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        run(tmp_path, builder_mailbox(), reader)
        changed = records(tmp_path)["c"]
        assert changed["kind"] == "mail_verdict"
        assert changed["gate"]["auth"]["authenticated"] is False
        assert {"what": "sender_lookalike", "detail": "bui1der.example~builder.example"} in (
            changed["gate"]["flags"])
        assert changed["case_flags"]["coordinates_changed"]
        assert changed["case_flags"]["sender_not_admitted"]
        assert changed["coordinates"]["code"] == ["DE89370400440532013000"]
        assert changed["attachments"]["read"] is False

    def test_the_reader_sees_the_whole_conversation_both_directions(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        run(tmp_path, builder_mailbox(), reader)
        bundle = next(b for b in reader.bundles if len(b["messages"]) == 3)
        assert [m["direction"] for m in bundle["messages"]] == [
            "to_owner", "from_owner", "to_owner"]

    def test_no_model_named_is_a_recorded_ending(self, tmp_path):
        run(tmp_path, builder_mailbox(), None, model="")
        assert records(tmp_path)["i"]["ending"] == roles.Status.UNAVAILABLE

    def test_a_failed_listing_is_counted_toward_a_streak(self, tmp_path):
        result = run(tmp_path, FakeMailbox([], fail_listing=True), ScriptedReader([]))
        assert not result.listing_ok
        mailwatch.record_health(tmp_path, result, T0)
        state = mailwatch.record_health(tmp_path, result, T0 + 60)
        assert state["failed_listings"] == 2 and state["last_ok_at"] is None


class TestCounters:
    def test_the_store_scan_counts_axes_endings_and_kinds(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]],
                                                       wanted="cannot_tell", kind="invoice"))
        run(tmp_path, builder_mailbox(), reader)
        (counters,) = mailverdicts.scan_records(mailverdicts.iter_store(tmp_path)).values()
        assert counters.endings == {"ok": 2, "gated": 1}
        assert counters.cannot_tell("wanted") == 2 and counters.kinds == {"invoice": 2}
        assert counters.gated_by == {"List-Id": 1}
        assert "%" not in mailverdicts.render_counters({MODEL: counters})

    def test_the_passs_vocabulary_tallies_reach_aish_vocab(self, tmp_path):
        vocab.reset()
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]],
                                                       wanted="cannot_tell"))
        result = run(tmp_path, builder_mailbox(), reader)
        mailwatch.flush_counters(tmp_path, result, T0)
        counted = vocab.scan(tmp_path)
        assert counted["mailreader.WANTED"].asked == 2
        assert counted["mailreader.WANTED"].matched == 0
        assert counted["mailcases._KEY_WORDS"].asked >= 3

    def test_the_record_kind_is_renderless(self):
        assert mailverdicts.RECORD_KIND in session.RENDERLESS_STEPS

    def test_counters_read_the_knob_state_dir(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        assert mailwatch.main(["--counters"]) == 0
        assert "no mail verdicts recorded" in capsys.readouterr().out
        assert mailwatch.health_path(tmp_path) == tmp_path / "mail" / "watch_health.json"

    def test_a_record_written_twice_by_two_models_keeps_both(self, tmp_path):
        store = mailverdicts.VerdictStore(tmp_path)
        store.write({"mail": "d" * 64, "model": "a", "ending": "ok"})
        store.write({"mail": "d" * 64, "model": "b", "ending": "ok"})
        assert set(json.loads(next(mailverdicts.store_dir(tmp_path).glob("*/*.json"))
                              .read_text())["by_model"]) == {"a", "b"}
