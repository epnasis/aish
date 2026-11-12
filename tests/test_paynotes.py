"""The payment notes index (docs/payments.md).

No real plugin runs here: `run_tool` is faked, and its answers are built in
the plugin's REAL shapes — the wrapper's stdout is `json.dumps(payload,
ensure_ascii=False, indent=2)` (obsidianlib.emit), a failure is `ERROR: …` on
stderr with exit 2 (obsidianlib.main), and both go through the runtime's own
`tool_plugins.envelope`, so the text parsed is the text `execute` returns.
Frontmatter values are what the plugin's YAML-subset parser yields: strings
with quotes stripped, and `[]` for an empty or bare key.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aish import paynotes, tool_plugins

NOW = datetime(2026, 11, 12, 9, 0, 0)
ACCOUNT_SPACED = "72 1240 6960 1701 0800 0024 2857"
ACCOUNT = "PL72124069601701080000242857"


def _ok(name: str, payload: dict) -> str:
    out = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return str(tool_plugins.envelope(name, out, 0))


def _err(name: str, message: str) -> str:
    return str(tool_plugins.envelope(name, f"ERROR: {message}\n", 2))


def _hit(name: str, modified: str = "2026-11-01 10:00", size: int = 900,
         offloaded: bool = False) -> dict:
    hit: dict = {"note": f"Payments/{name}.md", "title": name, "modified": modified,
                 "size": size}
    if offloaded:
        hit["offloaded"] = True
    else:
        hit["tags"] = ["pay"]
        hit["preview"] = "# " + name
    return hit


class FakeVault:
    """The two plugin tools over an in-memory folder."""

    def __init__(self) -> None:
        self.hits: list[dict] = []
        self.notes: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.list_error = ""

    def add(self, name: str, frontmatter: dict, content: str = "", **hit) -> None:
        h = _hit(name, **hit)
        self.hits = [x for x in self.hits if x["note"] != h["note"]] + [h]
        self.notes[h["note"]] = {"frontmatter": frontmatter, "content": content}

    def reads(self) -> list[str]:
        return [a["note"] for n, a in self.calls if n == paynotes.READ_TOOL]

    def __call__(self, name: str, args: dict) -> str:
        self.calls.append((name, dict(args)))
        if name == paynotes.LIST_TOOL:
            if self.list_error:
                return _err(name, self.list_error)
            limit = args.get("max", 20)
            return _ok(name, {"hits": self.hits[:limit], "total_matching": len(self.hits)})
        note = args["note"]
        if note in self.errors:
            return _err(name, self.errors[note])
        data = self.notes[note]
        fm = data["frontmatter"]
        tags = fm.get("tags") or []
        attachments = [a.strip("[]") for a in fm.get("attachments") or []]
        return _ok(name, {
            "note": note, "vault": "Main", "frontmatter": fm, "tags": tags,
            "aish_editable": "aish" in tags, "attachments": attachments,
            "content": data["content"],
        })


def _fm(**overrides) -> dict:
    base = {
        "category": "utilities", "paid_to": "E.ON", "paid_for": "Wyspowa 8/27",
        "amount": "496.33", "currency": "PLN", "month": "2026-11",
        "date_due": "2026-11-10", "paid": "false", "attachments": [],
        "tags": ["pay", "recurring", "aish", "eon"],
        "created": "2026-10-15 20:52:13", "updated": "2026-10-15 20:52:13",
    }
    base.update(overrides)
    return base


def _records(state_dir: Path) -> dict[str, dict]:
    lines = paynotes.notes_path(state_dir).read_text().splitlines()
    return {r["file"]: r for r in map(json.loads, lines)}


# ------------------------------------------------------------------ parsing


class TestPaynotesAmounts:
    @pytest.mark.parametrize("raw, minor, currency", [
        ("496.33", 49633, None),
        ("496,33", 49633, None),
        ("3 264,00", 326400, None),
        ("3 264,00", 326400, None),
        ("3264,00 PLN", 326400, "PLN"),
        ("12", 1200, None),
        ("12,5", 1250, None),
        ("80 zł", 8000, "PLN"),
    ])
    def test_spellings_parse(self, raw, minor, currency):
        assert paynotes.parse_amount(raw) == (minor, currency)

    @pytest.mark.parametrize("raw", ["TBD", "", "1,234", "ok. 500", "-5", "1.2.3"])
    def test_ambiguous_or_absent_is_none_never_a_guess(self, raw):
        assert paynotes.parse_amount(raw) is None


class TestPaynotesNoteParsing:
    def parse(self, fm, body="", name="2026-11 E.ON Wyspowa.md"):
        return paynotes.parse_note(f"Payments/{name}", fm, body, [], fm.get("tags") or [])

    @pytest.mark.parametrize("raw, want", [
        ("true", "true"), ("True", "true"), ("false", "false"), ("False", "false"),
        ([], "unknown"), ("", "unknown"),
    ])
    def test_paid_spellings(self, raw, want):
        assert self.parse(_fm(paid=raw))["paid"] == want

    def test_amount_shapes_and_currency_default(self):
        rec = self.parse(_fm(amount="496.33"))
        assert rec["amount"] == {"minor": 49633, "currency": "PLN",
                                 "currency_source": "frontmatter", "source": "frontmatter"}
        fm = _fm(amount="12,50")
        del fm["currency"]
        assert self.parse(fm)["amount"]["currency_source"] == "default"

    @pytest.mark.parametrize("raw", [[], ""])
    def test_empty_amount_is_null_not_unparsed(self, raw):
        rec = self.parse(_fm(amount=raw))
        assert rec["amount"] is None and "amount" not in rec["unparsed"]

    def test_tbd_amount_is_recorded_unparsed_with_its_raw_text(self):
        rec = self.parse(_fm(amount="TBD"))
        assert rec["amount"] is None and rec["unparsed"]["amount"] == "TBD"

    def test_date_due_iso_empty_and_garbage(self):
        assert self.parse(_fm(date_due="2026-11-10"))["date_due"] == "2026-11-10"
        assert self.parse(_fm(date_due=[]))["date_due"] is None
        rec = self.parse(_fm(date_due="soon"))
        assert rec["date_due"] is None and rec["unparsed"]["date_due"] == "soon"

    def test_account_is_normalised_and_checked(self):
        rec = self.parse(_fm(account_number=ACCOUNT_SPACED))
        assert rec["account_number"] == ACCOUNT
        assert rec["account_valid"] is True

    def test_invoice_is_a_string(self):
        assert self.parse(_fm(invoice_number="245750201495"))["invoice_number"] == "245750201495"

    def test_missing_frontmatter_takes_month_from_the_file_name(self):
        rec = self.parse({}, "", name="2026-09 PGNiG Gas.md")
        assert rec["has_frontmatter"] is False
        assert (rec["month"], rec["month_source"]) == ("2026-09", "file_name")
        assert rec["paid"] == "unknown" and rec["amount"] is None

    def test_body_fills_frontmatter_gaps(self):
        body = (
            "### ✅ Zapłacono 2026-10-04 — 3 264,00 PLN\n"
            "- **Kwota:** 496,33 PLN\n"
            "- **Termin płatności:** 2026-11-10\n"
            f"- **Rachunek bankowy (Pekao S.A.):** `{ACCOUNT_SPACED}`\n"
            "- **Faktura / Prognoza:** nr 245750201495\n"
        )
        rec = self.parse(_fm(amount=[], date_due=[], paid=[]), body)
        assert rec["amount"]["minor"] == 49633 and rec["amount"]["source"] == "body"
        assert (rec["date_due"], rec["date_due_source"]) == ("2026-11-10", "body")
        assert (rec["account_number"], rec["account_number_source"]) == (ACCOUNT, "body")
        assert rec["invoice_number"] == "245750201495"
        assert (rec["paid"], rec["paid_source"]) == ("true", "body")
        assert rec["paid_lines"] == [{"date": "2026-10-04", "minor": 326400, "currency": "PLN"}]
        assert rec["conflicts"] == []

    def test_frontmatter_and_body_disagreeing_is_recorded_never_resolved(self):
        body = "- **Kwota:** 500,00 PLN\n- **Termin płatności:** 2026-11-20\n"
        rec = self.parse(_fm(amount="496.33", date_due="2026-11-10"), body)
        assert rec["amount"] is None and rec["date_due"] is None
        fields = {c["field"]: c for c in rec["conflicts"]}
        assert fields["amount"]["frontmatter"]["minor"] == 49633
        assert fields["amount"]["body"]["minor"] == 50000
        assert fields["date_due"] == {"field": "date_due", "frontmatter": "2026-11-10",
                                      "body": "2026-11-20"}

    def test_agreeing_values_are_not_a_conflict(self):
        rec = self.parse(_fm(amount="496.33"), "- **Kwota:** 496,33 PLN\n")
        assert rec["conflicts"] == [] and rec["amount"]["minor"] == 49633

    def test_paid_false_with_a_paid_line_is_a_conflict(self):
        rec = self.parse(_fm(paid="false"), "### ✅ Zapłacono 2026-10-04 — 496,33 PLN\n")
        assert rec["paid"] == "conflict"
        assert {"field": "paid", "frontmatter": "false", "body": "paid line"} in rec["conflicts"]

    def test_month_frontmatter_vs_file_name_disagreeing(self):
        rec = self.parse(_fm(month="2026-10"))
        assert rec["month"] is None and rec["conflicts"][0]["field"] == "month"

    def test_unparseable_paid_line_amount_keeps_raw(self):
        rec = self.parse(_fm(), "### ✅ Zapłacono 2026-10-04 — przelewem\n")
        assert rec["paid_lines"] == [{"date": "2026-10-04", "minor": None, "currency": None,
                                      "raw": "przelewem"}]

    def test_body_invoice_must_be_the_whole_value(self):
        rec = self.parse(_fm(), "- **Faktura:** FV 123/2026\n")
        assert rec["invoice_number"] is None
        assert rec["unparsed"]["body.invoice_number"] == "FV 123/2026"
        rec = self.parse(_fm(), "- **Faktura / Prognoza:** nr FV/123/2026\n")
        assert rec["invoice_number"] == "FV/123/2026"

    def test_frontmatter_invoice_must_be_the_whole_value(self):
        rec = self.parse(_fm(invoice_number="FV 123/2026"))
        assert rec["invoice_number"] is None
        assert rec["unparsed"]["invoice_number"] == "FV 123/2026"
        assert self.parse(_fm(invoice_number="FV/123/2026"))["invoice_number"] == "FV/123/2026"

    @pytest.mark.parametrize("line", [
        "### Zapłacono 2026-10-04 — 496,33 PLN",
        "## zapłacono 2026-10-04 — 496,33 PLN",
        "✅ ZAPŁACONO 2026-10-04 — 496,33 PLN",
    ])
    def test_paid_line_spellings(self, line):
        rec = self.parse(_fm(paid=[]), line + "\n")
        assert rec["paid_lines"] == [{"date": "2026-10-04", "minor": 49633, "currency": "PLN"}]
        assert rec["paid"] == "true"

    def test_plain_text_mentioning_zaplacono_is_not_a_paid_line(self):
        assert self.parse(_fm(), "Zapłacono 2026-10-04 przez bank\n")["paid_lines"] == []

    def test_unparseable_account_never_keeps_its_raw_text(self):
        rec = self.parse(_fm(account_number="see PDF 1240"))
        assert rec["account_number"] is None
        assert "1240" not in rec["unparsed"]["account_number"]


# ------------------------------------------------------------------ refresh


class TestPaynotesRefresh:
    def vault(self) -> FakeVault:
        v = FakeVault()
        v.add("2026-11 E.ON Wyspowa", _fm(account_number=ACCOUNT_SPACED))
        v.add("2026-10 Tax PIT", _fm(category="tax", paid="true", amount="3 264,00",
                                     month="2026-10"),
              "### ✅ Zapłacono 2026-10-04 — 3264,00 PLN\n")
        v.add("2026-11 School Lunch", _fm(amount="TBD", paid=[]))
        return v

    def test_lists_once_with_the_plugins_real_arguments(self, tmp_path):
        v = self.vault()
        paynotes.refresh(v, tmp_path, NOW)
        name, args = v.calls[0]
        assert name == "obsidian_find"
        assert args == {"folder": "Payments", "max": paynotes.LIST_MAX,
                        "preview_lines": paynotes.LIST_PREVIEW_LINES}
        assert [n for n, _ in v.calls].count("obsidian_find") == 1

    def test_full_refresh_writes_typed_records_and_counts(self, tmp_path):
        index = paynotes.refresh(self.vault(), tmp_path, NOW)
        assert index["outcome"] == "ok"
        c = index["counts"]
        assert (c["listed"], c["read"], c["unread"], c["pending"]) == (3, 3, 0, 0)
        assert (c["paid_true"], c["paid_false"], c["paid_unknown"]) == (1, 1, 1)
        assert c["unparsed_amount"] == 1
        recs = _records(tmp_path)
        eon = recs["2026-11 E.ON Wyspowa.md"]
        assert eon["path"] == "Payments/2026-11 E.ON Wyspowa.md"
        assert eon["marker"] == {"modified": "2026-11-01 10:00", "size": 900,
                                 "offloaded": False}
        assert eon["account_number"] == ACCOUNT and eon["status"] == "read"

    def test_files_are_owner_only(self, tmp_path):
        paynotes.refresh(self.vault(), tmp_path, NOW)
        for path in (paynotes.notes_path(tmp_path), paynotes.index_path(tmp_path)):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(paynotes.payments_dir(tmp_path).stat().st_mode) == 0o700

    def test_unchanged_notes_are_not_read_again(self, tmp_path):
        v = self.vault()
        paynotes.refresh(v, tmp_path, NOW)
        v.calls.clear()
        v.add("2026-11 E.ON Wyspowa", _fm(amount="500.00"), modified="2026-11-12 08:00",
              size=950)
        index = paynotes.refresh(v, tmp_path, NOW + timedelta(days=1))
        assert v.reads() == ["Payments/2026-11 E.ON Wyspowa.md"]
        assert index["counts"]["read_this_run"] == 1
        assert _records(tmp_path)["2026-11 E.ON Wyspowa.md"]["amount"]["minor"] == 50000
        assert _records(tmp_path)["2026-10 Tax PIT.md"]["status"] == "read"

    def test_an_offloaded_note_already_read_is_not_downloaded_again(self, tmp_path):
        v = self.vault()
        paynotes.refresh(v, tmp_path, NOW)
        v.calls.clear()
        v.hits[1] = _hit("2026-10 Tax PIT", modified="2026-11-12 07:00", size=180,
                         offloaded=True)
        index = paynotes.refresh(v, tmp_path, NOW)
        assert v.reads() == []
        rec = _records(tmp_path)["2026-10 Tax PIT.md"]
        assert rec["listed"]["offloaded"] is True
        # Its marker moved, so the parse is flagged — never silently current.
        assert rec["stale"] is True and rec["status"] == "read"
        assert index["counts"]["stale"] == 1

    def test_an_offloaded_note_with_an_unmoved_marker_is_not_stale(self, tmp_path):
        v = self.vault()
        paynotes.refresh(v, tmp_path, NOW)
        v.hits[1] = {**v.hits[1], "offloaded": True}
        paynotes.refresh(v, tmp_path, NOW)
        assert _records(tmp_path)["2026-10 Tax PIT.md"]["stale"] is False

    def test_a_read_that_resolves_to_another_note_is_unread(self, tmp_path):
        v = self.vault()

        def elsewhere(name, args):
            text = v(name, args)
            if name == paynotes.READ_TOOL and args["note"].endswith("Tax PIT.md"):
                text = text.replace('"note": "Payments/', '"note": "Archive/Old/')
            return text

        paynotes.refresh(elsewhere, tmp_path, NOW)
        rec = _records(tmp_path)["2026-10 Tax PIT.md"]
        assert rec["status"] == "unread"
        assert rec["unread_reason"] == "read resolved to a different note"
        assert "amount" not in rec

    def test_never_tried_notes_are_read_before_failed_ones(self, tmp_path):
        v = self.vault()
        v.errors["Payments/2026-11 E.ON Wyspowa.md"] = "boom"
        paynotes.refresh(v, tmp_path, NOW)  # E.ON fails; the others are read
        v.add("2026-12 New Bill", _fm(month="2026-12"))
        v.calls.clear()
        paynotes.refresh(v, tmp_path, NOW, max_reads=1)
        assert v.reads() == ["Payments/2026-12 New Bill.md"]
        assert _records(tmp_path)["2026-11 E.ON Wyspowa.md"]["status"] == "pending"

    def test_an_unreadable_note_is_recorded_with_the_plugins_reason_and_retried(
        self, tmp_path
    ):
        v = self.vault()
        why = ("'2026-11 School Lunch.md' is offloaded to iCloud and did not finish "
               "downloading within 20s.")
        v.errors["Payments/2026-11 School Lunch.md"] = why
        index = paynotes.refresh(v, tmp_path, NOW)
        rec = _records(tmp_path)["2026-11 School Lunch.md"]
        assert rec["status"] == "unread" and rec["unread_reason"] == "ERROR: " + why
        assert index["counts"]["unread"] == 1
        del v.errors["Payments/2026-11 School Lunch.md"]
        v.calls.clear()
        paynotes.refresh(v, tmp_path, NOW)
        assert v.reads() == ["Payments/2026-11 School Lunch.md"]
        assert _records(tmp_path)["2026-11 School Lunch.md"]["status"] == "read"

    def test_reads_per_run_are_bounded_and_the_rest_counted(self, tmp_path):
        v = self.vault()
        index = paynotes.refresh(v, tmp_path, NOW, max_reads=2)
        assert index["outcome"] == "partial" and index["reads_remaining"] == 1
        assert len(v.reads()) == 2
        assert [r["status"] for r in _records(tmp_path).values()].count("pending") == 1
        v.calls.clear()
        index = paynotes.refresh(v, tmp_path, NOW, max_reads=2)
        assert len(v.reads()) == 1 and index["outcome"] == "ok"

    def test_a_failed_listing_leaves_the_previous_index_intact(self, tmp_path):
        v = self.vault()
        paynotes.refresh(v, tmp_path, NOW)
        before = paynotes.notes_path(tmp_path).read_bytes()
        v.list_error = "no folder 'Payments' in the vault"
        index = paynotes.refresh(v, tmp_path, NOW + timedelta(days=1))
        assert index["outcome"] == "list failed"
        assert "no folder" in index["error"]
        assert paynotes.notes_path(tmp_path).read_bytes() == before
        loaded = paynotes.load(tmp_path, NOW + timedelta(days=1))
        assert loaded.available and len(loaded.notes) == 3
        assert loaded.age == timedelta(days=1)

    def test_an_exception_mid_run_leaves_the_previous_index_intact(self, tmp_path):
        v = self.vault()
        paynotes.refresh(v, tmp_path, NOW)
        before = paynotes.notes_path(tmp_path).read_bytes()

        def exploding(name, args):
            if name == paynotes.READ_TOOL:
                raise RuntimeError("the vault went away")
            return v(name, args)

        v.add("2026-12 New Bill", _fm(month="2026-12"))
        index = paynotes.refresh_recorded(exploding, tmp_path, NOW)
        assert index["outcome"] == "error" and index["error"] == "RuntimeError"
        assert paynotes.notes_path(tmp_path).read_bytes() == before
        assert paynotes.load(tmp_path, NOW).available

    def test_no_tool_is_an_outcome_and_writes_no_notes(self, tmp_path):
        index = paynotes.refresh(None, tmp_path, NOW)
        assert index["outcome"] == "no tool"
        assert not paynotes.notes_path(tmp_path).exists()
        loaded = paynotes.load(tmp_path)
        assert not loaded.available and "no tool" in loaded.reason

    def test_plugin_runner_is_none_without_the_tools(self, tmp_path):
        assert paynotes.plugin_runner(str(tmp_path)) is None

    def test_a_cut_listing_is_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paynotes, "LIST_MAX", 2)
        index = paynotes.refresh(self.vault(), tmp_path, NOW)
        assert index["listing_truncated"] is True

    def test_removed_notes_leave_the_index(self, tmp_path):
        v = self.vault()
        paynotes.refresh(v, tmp_path, NOW)
        v.hits.pop()
        index = paynotes.refresh(v, tmp_path, NOW)
        assert index["counts"]["removed"] == 1 and index["counts"]["listed"] == 2


class TestPaynotesLoad:
    def test_no_index_is_not_zero_bills(self, tmp_path):
        loaded = paynotes.load(tmp_path)
        assert loaded.available is False and loaded.notes == []
        assert "no index" in loaded.reason

    def test_an_empty_folder_is_an_index_of_zero(self, tmp_path):
        index = paynotes.refresh(FakeVault(), tmp_path, NOW)
        assert index["outcome"] == "ok" and index["counts"]["listed"] == 0
        loaded = paynotes.load(tmp_path, NOW)
        assert loaded.available is True and loaded.notes == []

    def test_unreadable_index_says_so(self, tmp_path):
        paynotes.payments_dir(tmp_path).mkdir()
        paynotes.index_path(tmp_path).write_text("{not json")
        loaded = paynotes.load(tmp_path)
        assert not loaded.available and "unreadable" in loaded.reason

    def test_notes_that_do_not_match_the_index_are_refused(self, tmp_path):
        v = FakeVault()
        v.add("2026-11 E.ON Wyspowa", _fm())
        paynotes.refresh(v, tmp_path, NOW)
        with paynotes.notes_path(tmp_path).open("a") as fh:
            fh.write("{}\n")
        loaded = paynotes.load(tmp_path)
        assert not loaded.available and "does not match" in loaded.reason

    def test_records_load_typed(self, tmp_path):
        v = FakeVault()
        v.add("2026-11 E.ON Wyspowa", _fm(account_number=ACCOUNT_SPACED))
        paynotes.refresh(v, tmp_path, NOW)
        (note,) = paynotes.load(tmp_path, NOW).notes
        assert isinstance(note, paynotes.PaymentNote)
        assert (note.amount_minor, note.currency, note.paid, note.month) == (
            49633, "PLN", "false", "2026-11")
        assert note.account_number == ACCOUNT


# ---------------------------------------------------------------- the loop


class TestPaynotesRefresher:
    def _run(self, refresher: paynotes.Refresher, ticks: int) -> list[float]:
        waits: list[float] = []

        async def sleep(seconds: float) -> None:
            waits.append(seconds)
            if len(waits) >= ticks:
                raise asyncio.CancelledError

        refresher.sleep = sleep
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(refresher.run())
        return waits

    def test_daily_after_a_complete_run_sooner_while_reads_remain(self, tmp_path,
                                                                  monkeypatch):
        v = FakeVault()
        for i in range(3):
            v.add(f"2026-0{i + 1} Bill", _fm(month=f"2026-0{i + 1}"))
        monkeypatch.setattr(paynotes, "MAX_READS_PER_RUN", 2)
        r = paynotes.Refresher(tmp_path, make_runner=lambda: v, clock=lambda: NOW)
        waits = self._run(r, 2)
        assert waits == [paynotes.BACKLOG_EVERY.total_seconds(),
                         paynotes.REFRESH_EVERY.total_seconds()]

    def test_a_backlog_that_makes_no_progress_waits_a_day(self, tmp_path, monkeypatch):
        v = FakeVault()
        for i in range(3):
            name = f"2026-0{i + 1} Bill"
            v.add(name, _fm(month=f"2026-0{i + 1}"), offloaded=True)
            v.errors[f"Payments/{name}.md"] = "did not finish downloading within 20s"
        monkeypatch.setattr(paynotes, "MAX_READS_PER_RUN", 2)
        r = paynotes.Refresher(tmp_path, make_runner=lambda: v, clock=lambda: NOW)
        waits = self._run(r, 2)
        assert waits == [paynotes.REFRESH_EVERY.total_seconds()] * 2
        index = json.loads(paynotes.index_path(tmp_path).read_text())
        assert index["outcome"] == "partial" and index["counts"]["read_ok_this_run"] == 0

    def test_next_wait(self):
        day, soon = paynotes.REFRESH_EVERY, paynotes.BACKLOG_EVERY
        assert paynotes.next_wait({"outcome": "partial",
                                   "counts": {"read_ok_this_run": 1}}) == soon
        assert paynotes.next_wait({"outcome": "partial",
                                   "counts": {"read_ok_this_run": 0}}) == day
        assert paynotes.next_wait({"outcome": "ok", "counts": {"read_ok_this_run": 5}}) == day
        assert paynotes.next_wait({}) == day

    def test_a_failing_refresh_is_recorded_and_the_loop_survives(self, tmp_path):
        def boom():
            raise RuntimeError("discover broke")

        r = paynotes.Refresher(tmp_path, make_runner=boom, clock=lambda: NOW)
        waits = self._run(r, 2)
        assert len(waits) == 2
        index = json.loads(paynotes.index_path(tmp_path).read_text())
        assert (index["outcome"], index["error"]) == (
            "error", "plugin discovery failed: RuntimeError")

    def test_a_raising_refresh_never_escapes(self, tmp_path, monkeypatch):
        def explode(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(paynotes, "refresh", explode)
        r = paynotes.Refresher(tmp_path, make_runner=lambda: None, clock=lambda: NOW)
        assert len(self._run(r, 2)) == 2
        index = json.loads(paynotes.index_path(tmp_path).read_text())
        assert index == {"schema": 1, "refreshed_at": NOW.isoformat(), "outcome": "error",
                         "error": "OSError"}


class TestPaynotesServer:
    def test_kill_switch_starts_nothing(self, tmp_path):
        from tests.test_server import connected, make_client

        env = _app_env(tmp_path)
        client, _ = make_client(env, [])
        with client, connected(client) as (_ws, hello, _):
            assert hello["type"] == "hello"
            assert client.app.state.server.paynotes_task is None
        assert not paynotes.payments_dir(env["state_dir"]).exists()

    def test_inert_without_the_plugin_tools(self, tmp_path, monkeypatch):
        from tests.test_server import connected, make_client

        monkeypatch.delenv("AISH_PAYNOTES")
        env = _app_env(tmp_path)
        client, _ = make_client(env, [])
        with client, connected(client):
            index = _wait_for(paynotes.index_path(env["state_dir"]))
        assert index["outcome"] == "no tool"

    def test_a_refresh_that_blocks_or_raises_never_holds_the_server(self, tmp_path,
                                                                   monkeypatch):
        from tests.test_server import connected, make_client, recv_until

        monkeypatch.delenv("AISH_PAYNOTES")
        release = threading.Event()
        entered = threading.Event()

        def stuck_then_raise(cwd):
            entered.set()
            release.wait(10)
            raise RuntimeError("plugin discovery exploded")

        monkeypatch.setattr(paynotes, "plugin_runner", stuck_then_raise)
        env = _app_env(tmp_path)
        client, _ = make_client(env, [])
        try:
            with client, connected(client) as (ws, hello, _):
                assert hello["type"] == "hello"
                assert entered.wait(5)
                # Answered while the refresh is still stuck inside its thread.
                ws.send_json({"type": "trash"})
                recv_until(ws, "trash_list")
                assert not release.is_set()
                release.set()
                index = _wait_for(paynotes.index_path(env["state_dir"]))
                assert (index["outcome"], index["error"]) == (
                    "error", "plugin discovery failed: RuntimeError")
                ws.send_json({"type": "trash"})  # and after it failed
                recv_until(ws, "trash_list")
        finally:
            release.set()


def _app_env(tmp_path: Path) -> dict:
    allow, deny = tmp_path / "allow.txt", tmp_path / "deny.txt"
    allow.write_text("ls\n")
    deny.write_text("rm -rf\n")
    return {"state_dir": tmp_path / "state", "allow_path": allow, "deny_path": deny,
            "config_path": tmp_path / "config.toml", "lessons_path": tmp_path / "lessons.md",
            "cwd": str(tmp_path)}


def _wait_for(path: Path, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


# ---------------------------------------------------------------------- CLI


class TestPaynotesCli:
    def test_prints_counts_only_and_says_it_cannot_refresh(self, tmp_path, monkeypatch,
                                                           capsys):
        from aish import cli

        v = FakeVault()
        v.add("2026-11 E.ON Wyspowa", _fm(account_number=ACCOUNT_SPACED,
                                          invoice_number="245750201495"))
        paynotes.refresh(v, tmp_path, datetime.now())
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        assert cli._paynotes_cli([]) == 0
        out = capsys.readouterr().out
        assert "listed: 1" in out and "outcome: ok" in out
        assert "cannot open the vault" in out
        for secret in ("E.ON", "Wyspowa", "1240", "245750201495", "496", "utilities"):
            assert secret not in out

    def test_prints_the_outcome_word_never_the_error_text(self, tmp_path, monkeypatch,
                                                         capsys):
        from aish import cli

        v = FakeVault()
        v.add("2026-11 E.ON Wyspowa", _fm())
        paynotes.refresh(v, tmp_path, datetime.now())
        v.list_error = "no folder 'Payments/E.ON Wyspowa' in the vault"
        paynotes.refresh(v, tmp_path, datetime.now())
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        cli._paynotes_cli([])
        out = capsys.readouterr().out
        assert "outcome: list failed" in out and "Wyspowa" not in out
        # And before any success: still only the word.
        fresh = tmp_path / "fresh"
        paynotes.refresh(v, fresh, datetime.now())
        monkeypatch.setenv("AISH_STATE_DIR", str(fresh))
        cli._paynotes_cli([])
        out = capsys.readouterr().out
        assert "list failed" in out and "Wyspowa" not in out

    def test_no_index_is_said_plainly(self, tmp_path, monkeypatch, capsys):
        from aish import cli

        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        assert cli._paynotes_cli([]) == 0
        assert "no index" in capsys.readouterr().out

    def test_never_writes(self, tmp_path, monkeypatch):
        from aish import cli

        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        cli._paynotes_cli([])
        assert os.listdir(tmp_path) == []
