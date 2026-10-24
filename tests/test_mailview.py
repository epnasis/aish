"""`/mail` — the owner surface over the shadow pipeline (#142 W5 S1.5).

Pins the verdict↔index join by digest, render safety through `cli._plain`
(applied at READ time, whatever was written), the caps, the health line and
its failure streak, and the slash wiring. Fixtures only: no mail, no model."""

from __future__ import annotations

import re

from mail_fixtures import DAY, T0, ScriptedReader, gmail, verdict_json
from test_mailwatch import builder_mailbox, run

from aish import cli, mailindex, mailverdicts, mailview, mailwatch

HOSTILE = "Pay\x9b2J now‮evil\x1b[31mred\x1b]8;;http://x.example\x07link\x1b\\  end"
NOW = T0 + 3 * DAY


def _identity(text: str) -> str:
    return text


def seed_hostile(state_dir) -> str:
    """One index entry and one verdict record whose every string is hostile,
    written straight to disk — what render must survive is the BYTES, not
    whatever the write path would have produced."""
    parsed = mailindex.from_gmail(
        gmail("h", frm=f"\x1b[2J{HOSTILE} <attacker@evil.example>", subject=HOSTILE * 4,
              body="x", at=T0 + 2 * DAY, attachments=(f"invoice{HOSTILE}.pdf",)),
        frozenset({"owner@example.org"}),
    )
    assert parsed is not None
    index = mailindex.MailIndex(state_dir)
    index.add(parsed.header, parsed.body)
    digest = parsed.header.digest
    record = {
        "kind": "mail_verdict", "mail": digest, "provider_id": "h", "at": T0 + 2 * DAY,
        "model": "gemini:test", "ending": "ok", "why": HOSTILE,
        "gate": {
            "auth": {"authenticated": False, "verdict": HOSTILE},
            "grade": {"grade": "first-contact"},
            "flags": [{"what": "link_text_mismatch", "detail": HOSTILE}],
            "newsletter": {"gated": False, "by": []},
        },
        "case_flags": {"coordinates_changed": ["c1"], "sender_not_admitted": []},
        "verdict": {"disposition": "hold", "wanted": "pay", "deadline": "2026-11-05",
                    "suspicion": "suspicious", "sender_case": "mismatch",
                    "meaning": HOSTILE * 30, "kind": HOSTILE},
        "attachments": {"count": 1, "names": [f"invoice{HOSTILE}.pdf"], "read": False},
        "recorded_at": NOW,
    }
    mailverdicts.VerdictStore(state_dir).write(record)
    return digest


def printed(capsys) -> str:
    return capsys.readouterr().out


UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩"
                    r"  ]")


class TestRenderSafety:
    def test_hostile_bytes_render_stripped_and_marked(self, tmp_path, capsys):
        seed_hostile(tmp_path)
        cli.show_mail("/mail", tmp_path, now=NOW)
        out = printed(capsys)
        # Strip OUR OWN SGR wrapping first: what is left must hold no control.
        text = re.sub(r"\x1b\[[0-9;]*m", "", out).replace("\n", "")
        assert not UNSAFE.search(text)
        assert "\x9b" not in out and "‮" not in out and " " not in out
        assert "\x1b]8" not in out and "\x1b[2J" not in out and "\x1b[31m" not in out
        assert "\N{REPLACEMENT CHARACTER}" in out  # what was removed is marked, not hidden
        assert "attacker@evil.example" in out  # the code-derived address leads

    def test_every_mail_and_model_string_passes_through_the_sanitiser(self, tmp_path):
        seed_hostile(tmp_path)
        seen: list[str] = []

        def recording(text: str) -> str:
            seen.append(text)
            return "«ok»"

        lines = mailview.lines(tmp_path, clean=recording, now=NOW)
        rendered = "\n".join(text for _style, text in lines)
        # No hostile fragment reaches the output except through `clean`.
        for fragment in ("evil", "\x9b", "‮", "invoice", "attacker", "Pay", "hold", "pay"):
            assert fragment not in rendered, fragment
        assert any("invoice" in s for s in seen) and any(HOSTILE in s for s in seen)

    def test_render_time_caps(self, tmp_path):
        seed_hostile(tmp_path)
        lines = mailview.lines(tmp_path, clean=cli._mail_cell, now=NOW)
        head = next(t for s, t in lines if s == "head")
        reading = next(t for s, t in lines if t.startswith("  reading:"))
        subject, box = head.split(" — ", 1)[1].rsplit("  [", 1)
        assert box == "gmail]"
        assert len(subject) <= mailview.SUBJECT_CHARS
        meaning = reading.split(" — ", 1)[1]
        assert len(meaning) <= mailview.MEANING_CHARS and meaning.endswith("…")


class TestJoin:
    def test_verdicts_join_the_index_by_digest_newest_first(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]], kind="invoice",
                                                       meaning="An invoice to pay."))
        run(tmp_path, builder_mailbox(), reader)
        view = mailview.collect(tmp_path)
        assert [r.header.provider_id for r in view.rows if r.header] == ["n", "c", "i"]
        assert all(r.header is not None for r in view.rows)
        lines = [t for _s, t in mailview.lines(tmp_path, clean=cli._mail_cell, now=NOW)]
        assert any("office@builder.example (Builder Example) — Invoice 2026/41" in t
                   for t in lines)
        assert any("reading: invoice — An invoice to pay." in t for t in lines)
        assert any("newsletter-gated (List-Id)" in t for t in lines)
        assert any("NOT authenticated" in t and "sender_lookalike" in t for t in lines)

    def test_a_verdict_with_no_index_entry_is_shown_not_dropped(self, tmp_path):
        mailverdicts.VerdictStore(tmp_path).write(
            {"mail": "e" * 64, "provider_id": "gone", "model": "m", "ending": "error",
             "why": "KeyError: boom", "at": T0})
        lines = [t for _s, t in mailview.lines(tmp_path, clean=cli._mail_cell, now=NOW)]
        assert any("not in the index: provider id gone" in t for t in lines)
        assert any("ending: error — KeyError: boom" in t for t in lines)

    def test_an_unreadable_verdict_file_is_counted(self, tmp_path):
        bad = mailverdicts.store_dir(tmp_path) / "ab" / ("ab" + "0" * 62 + ".json")
        bad.parent.mkdir(parents=True)
        bad.write_text("{not json")
        lines = mailview.lines(tmp_path, clean=_identity, now=NOW)
        assert ("warn", "1 verdict file(s) could not be read") in lines

    def test_limit(self, tmp_path):
        reader = ScriptedReader(lambda b: verdict_json(citations=[b["mail"]]))
        run(tmp_path, builder_mailbox(), reader)
        assert len(mailview.collect(tmp_path, limit=1).rows) == 1
        assert mailview.parse_limit([]) == mailview.MAIL_SHOWN
        assert mailview.parse_limit(["5"]) == 5
        assert mailview.parse_limit(["9999"]) == mailview.MAIL_LIMIT_MAX
        assert mailview.parse_limit(["x"]) is None


class TestHealth:
    def test_no_pass_recorded_is_a_warning(self, tmp_path):
        (style, text), *_ = mailview.lines(tmp_path, clean=_identity, now=NOW)
        assert style == "warn" and "no pass recorded" in text

    def test_a_failed_listing_streak_is_shown_prominently(self, tmp_path):
        failed = mailwatch.Pass(listing_ok=False)
        failed.sources = {"gmail": mailwatch.SourcePass(listing_ok=False,
                                                        failure="quota: Units per minute")}
        for i in range(3):
            mailwatch.record_health(tmp_path, failed, NOW - 60 * (3 - i))
        lines = mailview.lines(tmp_path, clean=_identity, now=NOW)
        warn = [t for s, t in lines if s == "warn"]
        assert any("gmail listing FAILED on the last 3 pass(es)" in t
                   and "Units per minute" in t for t in warn)

    def test_a_watcher_that_stopped_is_visible(self, tmp_path):
        mailwatch.record_health(tmp_path, mailwatch.Pass(), NOW - 5 * 3600)
        (style, text), *_ = mailview.lines(tmp_path, clean=_identity, now=NOW)
        assert style == "warn" and "no pass recorded since" in text

    def test_a_recent_good_pass_is_quiet(self, tmp_path):
        mailwatch.record_health(tmp_path, mailwatch.Pass(listed=4, assessed=3), NOW - 120)
        (style, text), *_ = mailview.lines(tmp_path, clean=_identity, now=NOW)
        assert style == "ok" and "4 listed, 3 assessed" in text


class TestWiring:
    def test_the_command_is_registered_and_dispatched(self, tmp_path, capsys):
        assert "/mail" in cli.SLASH_COMMANDS and "/mail" in cli.SLASH_HELP
        cli.show_mail("/mail nonsense", tmp_path, now=NOW)
        assert "usage: /mail" in printed(capsys)

    def test_handle_slash_reaches_it_and_ma_resolves(self, tmp_path, capsys):
        from aish.agent import Agent
        from aish.session import SessionLog

        agent = Agent(model="fake", approve=lambda _c: None, client_chat=lambda **_k: None)
        logref = cli.LogRef(SessionLog.new(tmp_path))
        assert cli.handle_slash("/mail", agent, logref, tmp_path) == "handled"
        assert "no pass recorded" in printed(capsys)
        assert cli.handle_slash("/ma", agent, logref, tmp_path) == "handled"
        assert "no shadow results recorded yet" in printed(capsys)
