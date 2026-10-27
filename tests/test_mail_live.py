"""The live-conversation fact and clamp, and charter v4 (#142 W5 S1.6).

Pins: the fact is past-facing and counted over the whole conversation before
the bundle's slice is cut; the clamp turns a filing disposition into `surface`
within `LIVE_CONVERSATION_DAYS` and never touches the stored verdict; a hostile
mail joining a live thread by References earns the clamp (visibility) and
nothing else — no act, no grade; `/mail` marks a clamped row; charter v4 loads
and states its sentences. Fixtures only: no mail, no model.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from mail_fixtures import DAY, OWNER, T0, FakeMailbox, ScriptedReader, gmail, verdict_json
from test_mailwatch import records, run

from aish import mailcases, mailindex, mailreader, mailverdicts, mailview, mailwatch, roles

LIMIT = mailreader.LIVE_CONVERSATION_DAYS


def file_everything(bundle):
    return verdict_json(disposition="archive", citations=[bundle["mail"]])


def owner_thread(gap_days: float) -> FakeMailbox:
    """He writes to the insurer's agent; the agent answers `gap_days` later."""
    return FakeMailbox([
        gmail("o", frm=OWNER, to="agent@insure.example", subject="Policy renewal",
              body="Could you send the renewed policy?", at=T0, message_id="o@example.org",
              labels=("SENT",)),
        gmail("a", frm="Agent <agent@insure.example>", subject="Re: Policy renewal",
              body="Attached is the renewed policy.", at=T0 + gap_days * DAY,
              in_reply_to="o@example.org", labels=("INBOX",), attachments=("policy.pdf",)),
    ])


class TestTheFact:
    def test_it_is_past_facing(self, tmp_path):
        """A reply he sends LATER does not make the earlier mail live."""
        box = FakeMailbox([
            gmail("q", frm="Ania <ania@friends.example>", subject="Car?", body="Yours or mine?",
                  at=T0, message_id="q@friends.example", labels=("INBOX",)),
            gmail("r", frm=OWNER, to="ania@friends.example", subject="Re: Car?", body="Mine.",
                  at=T0 + DAY, in_reply_to="q@friends.example", labels=("SENT",)),
        ])
        reader = ScriptedReader(file_everything)
        run(tmp_path, box, reader)
        record = records(tmp_path)["q"]
        assert record["owner_wrote_days"] is None and record["clamped"] is None
        assert reader.bundles[0]["owner_wrote_in_this_conversation"] == "never"

    def test_it_is_counted_before_the_slice_is_cut(self):
        """His message is older than the shown window; the fact still sees it."""
        index = mailindex.MailIndex()
        owners = frozenset({OWNER})
        msgs = [gmail("o", frm=OWNER, to="b@builder.example", subject="Roof", body="Quote?",
                      at=T0, message_id="o@example.org", labels=("SENT",))]
        parent = "o@example.org"
        for n in range(mailreader.READER_CONVERSATION_SHOWN + 2):
            mid = f"b{n}@builder.example"
            msgs.append(gmail(f"b{n}", frm="b@builder.example", subject="Re: Roof",
                              body=f"update {n}", at=T0 + (n + 1) * 3600, message_id=mid,
                              in_reply_to=parent))
            parent = mid
        for msg in msgs:
            parsed = mailindex.from_gmail(msg, owners)
            assert parsed is not None
            index.add(parsed.header, parsed.body)
        store = mailcases.CaseStore()
        header = index.get(mailindex.from_gmail(msgs[-1], owners).header.digest)
        facts = mailwatch.gate_facts(index, store, header, index.body(header.digest) or "")
        bundle = mailreader.compose(index, store, header, facts)
        assert bundle.data["omitted_messages"] > 0
        assert all(m["direction"] == "to_owner" for m in bundle.data["messages"])
        assert bundle.data["owner_wrote_in_this_conversation"] == "0 days ago"
        assert bundle.owner_wrote_days is not None and bundle.owner_wrote_days < 1

    def test_the_words(self):
        assert mailreader.owner_wrote_words(None) == "never"
        assert mailreader.owner_wrote_words(1.5) == "1 day ago"
        assert mailreader.owner_wrote_words(29.9) == "29 days ago"


class TestTheClamp:
    def test_boundaries(self):
        assert mailreader.clamp("archive", LIMIT) == {
            "from": "archive", "to": "surface", "owner_wrote_days": float(LIMIT), "limit": LIMIT}
        assert mailreader.clamp("expire_after_grace", 0.0)["to"] == "surface"
        assert mailreader.clamp("archive", LIMIT + 0.01) is None
        assert mailreader.clamp("archive", None) is None
        for keep in ("surface", "hold", "cannot_tell"):
            assert mailreader.clamp(keep, 1.0) is None

    def test_the_limit_is_the_owners_thirty_days(self):
        assert LIMIT == 30

    def test_a_filed_continuation_is_surfaced_and_the_verdict_is_untouched(self, tmp_path):
        run(tmp_path, owner_thread(3), ScriptedReader(file_everything))
        record = records(tmp_path)["a"]
        assert record["verdict"]["disposition"] == "archive"
        assert record["clamped"] == {"from": "archive", "to": "surface",
                                     "owner_wrote_days": 3.0, "limit": LIMIT}
        assert mailverdicts.effective_disposition(record) == "surface"

    def test_past_the_limit_nothing_is_clamped(self, tmp_path):
        run(tmp_path, owner_thread(LIMIT + 1), ScriptedReader(file_everything))
        record = records(tmp_path)["a"]
        assert record["clamped"] is None
        assert mailverdicts.live_disposition(record) == "archive"
        # The renewed policy is a keeper document: the OTHER clamp keeps it.
        assert record["keeper_clamped"]["signals"] == ["document_attachment"]
        assert mailverdicts.effective_disposition(record) == "surface"

    def test_a_record_from_before_the_clamp_reads_as_its_verdict(self):
        old = {"ending": "ok", "verdict": {"disposition": "archive"}}
        assert mailverdicts.clamp_of(old) is None
        assert mailverdicts.effective_disposition(old) == "archive"
        assert mailverdicts.effective_disposition({"ending": "gated", "verdict": None}) is None


class TestJoiningByReferences:
    def test_a_joined_hostile_mail_earns_visibility_and_nothing_else(self, tmp_path):
        """The stated residual: membership is not an act. The join earns the
        clamp (it stays visible); the ledger grants it no act and no grade."""
        box = owner_thread(1)
        box.messages["x"] = gmail(
            "x", frm="Insurer Billing <billing@evil.example>", subject="Re: Policy renewal",
            body="Pay the renewal to our new account.", at=T0 + 2 * DAY,
            references="<o@example.org>", labels=("INBOX",))
        reader = ScriptedReader(file_everything)
        run(tmp_path, box, reader)
        record = records(tmp_path)["x"]
        assert record["clamped"]["to"] == "surface"
        grade = record["gate"]["grade"]
        assert grade["grade"] == "first-contact" and grade["acts"] == 0
        assert any(m["direction"] == "from_owner"
                   for b in reader.bundles if b["messages"][-1]["from"].endswith("evil.example>")
                   for m in b["messages"])


class TestTheOwnerSurface:
    def test_a_clamped_row_is_marked_and_shows_the_effective_disposition(self, tmp_path):
        run(tmp_path, owner_thread(3), ScriptedReader(file_everything))
        text = "\n".join(t for _s, t in mailview.lines(tmp_path, clean=lambda s: s,
                                                        now=T0 + 4 * DAY))
        assert "would: surface [CLAMPED from archive: you wrote in this conversation 3.0" in text


class TestCharterV4:
    def charter(self) -> roles.Charter:
        return roles.load_charters()["mail-reader"]

    def test_it_loads_as_v4_with_its_sentences(self):
        charter = self.charter()
        assert charter.version == "4"
        prose = " ".join(charter.path.read_text().split())
        for sentence in (
            "When the owner has written in this conversation recently, it is live: never "
            "`archive` or `expire_after_grace` it.",
            "A direct question or request addressed to him personally wants `reply`; a "
            "no-reply or bulk sender's courtesy \"reply to this email\" is not a request of him.",
        ):
            assert sentence in prose
        guidance = prose.split("## Golden pairs")[0]
        assert f"{LIMIT} days" not in guidance and "14 days" not in guidance  # lives in code

    def test_every_shipped_bundle_carries_the_fact_as_compose_writes_it(self):
        for case in self.charter().cases:
            bundle = json.loads(case.inputs["bundle"])
            assert bundle["owner_wrote_in_this_conversation"] in ("never", "1 day ago"), case.name

    def test_dry_run_lists_shipped_and_owner_cases(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AISH_CONFIG_HOME", str(tmp_path))
        cases = Path(tmp_path / "roles" / "mail-reader")
        cases.mkdir(parents=True)
        shipped = self.charter().cases[0]
        (cases / "cases.yaml").write_text(
            "- name: owner-a\n  input:\n    bundle: |\n      "
            + shipped.inputs["bundle"].replace("\n", "\n      ")
            + "\n  expect:\n    axis_in:\n      suspicion: [\"none\"]\n")
        spec = importlib.util.spec_from_file_location(
            "role_admission", Path(__file__).resolve().parent.parent / "scripts"
            / "role-admission.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.main(["--dry-run", "mail-reader"]) == 0
        out = capsys.readouterr().out
        assert "mail-reader v4 — 10 shipped case(s), 1 of the owner's" in out
        assert "owner   owner-a" in out
