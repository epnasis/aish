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
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aish import invoicefacts, paynotes, tool_plugins

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
    """The three plugin tools over an in-memory folder.

    `obsidian_attachment` is mirrored from its wrapper: the copy lands in
    `AISH_TOOL_DOWNLOADS_DIR` (which `execute` sets from `downloads_dir`)
    under the attachment's basename, and stdout is attachlib.saved_entry's
    row plus `from_vault` and `vault`."""

    def __init__(self) -> None:
        self.hits: list[dict] = []
        self.notes: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.list_error = ""
        self.files: dict[tuple[str, str], bytes] = {}
        self.file_errors: dict[tuple[str, str], str] = {}
        self.downloads: list[Path | None] = []
        self.on_fetch = None

    def add(self, name: str, frontmatter: dict, content: str = "", **hit) -> None:
        h = _hit(name, **hit)
        self.hits = [x for x in self.hits if x["note"] != h["note"]] + [h]
        self.notes[h["note"]] = {"frontmatter": frontmatter, "content": content}

    def reads(self) -> list[str]:
        return [a["note"] for n, a in self.calls if n == paynotes.READ_TOOL]

    def fetches(self) -> list[tuple[str, str]]:
        return [(a["note"], a["name"]) for n, a in self.calls if n == paynotes.INVOICE_TOOL]

    def attach(self, note: str, name: str, data: bytes) -> None:
        self.files[(f"Payments/{note}.md", name)] = data

    def _fetch(self, name: str, args: dict, downloads_dir: Path | None) -> str:
        self.downloads.append(downloads_dir)
        if self.on_fetch is not None:
            self.on_fetch(args)
        key = (args["note"], args["name"])
        if key in self.file_errors:
            return _err(name, self.file_errors[key])
        if key not in self.files:
            return _err(name, f"no attachment named {args['name']!r} found anywhere in the vault")
        directory = Path(downloads_dir) if downloads_dir else Path(tempfile.mkdtemp())
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / Path(args["name"]).name
        target.write_bytes(self.files[key])
        return _ok(name, {"filename": target.name, "path": str(target),
                          "bytes": target.stat().st_size, "mime": None,
                          "from_vault": f"Payments/attachments/{args['name']}",
                          "vault": "Main"})

    def __call__(self, name: str, args: dict, downloads_dir: Path | None = None,
                 should_stop=None) -> str:
        self.calls.append((name, dict(args)))
        if name == paynotes.INVOICE_TOOL:
            return self._fetch(name, args, downloads_dir)
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


# ------------------------------------------------------------ invoice facts

INVOICE_TEXT = (
    "Faktura VAT nr FV/123/2026\n"
    f"Numer rachunku: {ACCOUNT_SPACED}\n"
    "Razem do zapłaty: 496,33 zł\n"
    "Termin płatności: 24.11.2026\n"
)


class FakeReader:
    """The reader child's seam: `invoicereader.run_reader`'s answer shape,
    per-page facts and never text. Answers by the file's bytes (a dict, or a
    callable of the `skip` set); `calls` records what it was handed."""

    def __init__(self) -> None:
        self.answers: dict = {}
        self.calls: list[tuple[Path, Path, set, float]] = []
        self.active = 0
        self.max_active = 0
        self.raise_on: set[bytes] = set()
        self.on_read = None

    def __call__(self, source: Path, store: Path, skip: set, seconds: float,
                 should_stop) -> dict:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((source, store, set(skip), seconds))
            data = source.read_bytes()
            if self.on_read is not None:
                self.on_read(source)
            if data in self.raise_on:
                raise RuntimeError("the pipeline broke on " + source.name)
            answer = self.answers.get(data)
            if callable(answer):
                return answer(skip)
            return answer or text_answer(INVOICE_TEXT)
        finally:
            self.active -= 1


def text_answer(*pages: str) -> dict:
    return {"kind": "pdf", "pages_total": len(pages),
            "pages": {i: {"how": "text", "facts": invoicefacts.page_facts(t)}
                      for i, t in enumerate(pages, start=1)}}


def _invoice_vault(*names: str) -> FakeVault:
    """Notes in listing order (newest first), each with one PDF attached."""
    v = FakeVault()
    for i, name in enumerate(names):
        attachment = f"{name}.pdf"
        v.add(name, _fm(attachments=[f"[[{attachment}]]"]),
              modified=f"2026-11-{12 - i:02d} 10:00")
        v.attach(name, attachment, f"%PDF-1.7 {name}".encode())
    return v


def _invoices(state_dir: Path) -> dict[str, dict]:
    lines = paynotes.invoices_path(state_dir).read_text().splitlines()
    return {r["name"]: r for r in map(json.loads, lines)}


def _both(v, state_dir: Path, reader=None, **kwargs) -> dict:
    paynotes.refresh(v, state_dir, NOW)
    return paynotes.refresh_invoices(v, state_dir, NOW, read=reader or FakeReader(), **kwargs)


class TestInvoiceFetch:
    def test_the_plugins_real_arguments_and_the_scratch_downloads_dir(self, tmp_path):
        v = _invoice_vault("2026-11 E.ON")
        _both(v, tmp_path)
        assert v.fetches() == [("Payments/2026-11 E.ON.md", "2026-11 E.ON.pdf")]
        ((_, args),) = [c for c in v.calls if c[0] == paynotes.INVOICE_TOOL]
        assert args == {"note": "Payments/2026-11 E.ON.md", "name": "2026-11 E.ON.pdf",
                        "wait": paynotes.INVOICE_FETCH_WAIT}
        assert v.downloads == [paynotes.scratch_dir(tmp_path) / "fetch"]

    def test_the_copy_is_read_inside_the_private_scratch_and_deleted(self, tmp_path):
        v = _invoice_vault("2026-11 E.ON")
        seen: list[tuple[str, int]] = []
        reader = FakeReader()
        reader.on_read = lambda source: seen.append(
            (str(source), stat.S_IMODE(source.parent.parent.stat().st_mode)))
        _both(v, tmp_path, reader)
        scratch = paynotes.scratch_dir(tmp_path)
        assert seen == [(str(scratch / "fetch" / "2026-11 E.ON.pdf"), 0o700)]
        assert reader.calls[0][1] == scratch / "renditions"
        assert reader.calls[0][2] == set()  # nothing read before: no page skipped
        assert not scratch.exists()
        assert sorted(os.listdir(paynotes.payments_dir(tmp_path))) == [
            "index.json", "invoices.jsonl", "notes.jsonl"]

    def test_deleted_on_every_failure_path(self, tmp_path, monkeypatch):
        v = _invoice_vault("2026-11 A", "2026-11 B", "2026-11 C", "2026-11 D")
        v.file_errors[("Payments/2026-11 A.md", "2026-11 A.pdf")] = (
            "'2026-11 A.pdf' is offloaded to iCloud and did not finish downloading within 60s.")
        reader = FakeReader()
        reader.raise_on.add(b"%PDF-1.7 2026-11 B")
        reader.answers[b"%PDF-1.7 2026-11 C"] = {
            "kind": "other", "pages_total": 0, "pages": {},
            "reason_class": paynotes.NOT_A_DOCUMENT, "reason": "neither"}
        v.files[("Payments/2026-11 D.md", "2026-11 D.pdf")] = b"x" * 64
        monkeypatch.setattr(paynotes, "MAX_ATTACHMENT_BYTES", 32)
        _both(v, tmp_path, reader)
        recs = _invoices(tmp_path)
        assert (recs["2026-11 A.pdf"]["status"], recs["2026-11 A.pdf"]["reason_class"]) == (
            "unread", "fetch failed")
        assert recs["2026-11 A.pdf"]["reason"].startswith("ERROR: '2026-11 A.pdf' is offloaded")
        assert (recs["2026-11 B.pdf"]["reason_class"], recs["2026-11 B.pdf"]["reason"]) == (
            "error", "RuntimeError")
        assert recs["2026-11 C.pdf"]["reason_class"] == "not a pdf or picture"
        assert recs["2026-11 D.pdf"]["reason_class"] == "too large"
        assert all(r["read_as"] == "not read" for r in recs.values())
        assert not paynotes.scratch_dir(tmp_path).exists()

    def test_a_reported_path_outside_the_scratch_is_never_read_nor_deleted(self, tmp_path,
                                                                         monkeypatch):
        # pytest's tmp_path is itself under the system temp dir, where a stray
        # copy WOULD be removed (TestInvoiceStrayCopy); point that elsewhere.
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "systemp"))
        v = _invoice_vault("2026-11 E.ON")
        elsewhere = tmp_path / "elsewhere.pdf"
        elsewhere.write_bytes(b"%PDF-1.7 elsewhere")

        def lying(name, args, downloads_dir=None, should_stop=None):
            text = v(name, args, downloads_dir)
            if name == paynotes.INVOICE_TOOL:
                reported = str(Path(downloads_dir) / "2026-11 E.ON.pdf")
                text = text.replace(json.dumps(reported)[1:-1], str(elsewhere))
            return text

        reader = FakeReader()
        state = tmp_path / "state"
        paynotes.refresh(v, state, NOW)
        paynotes.refresh_invoices(lying, state, NOW, read=reader)
        rec = _invoices(state)["2026-11 E.ON.pdf"]
        assert (rec["status"], rec["reason_class"]) == ("unread", "fetch failed")
        assert reader.calls == [] and elsewhere.exists()
        assert not paynotes.scratch_dir(state).exists()

    def test_through_execute_the_wrapper_is_handed_the_scratch_dir(self, tmp_path):
        """The real runner: `downloads_dir` reaches the wrapper as
        AISH_TOOL_DOWNLOADS_DIR, which is where obsidian_attachment copies to
        (attachlib.download_dir); without it the copy would go to a temp dir
        nothing deletes."""
        tool_dir = tmp_path / "tools" / paynotes.INVOICE_TOOL
        tool_dir.mkdir(parents=True)
        wrapper = tool_dir / "wrapper"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = json.load(sys.stdin)\n"
            "d = os.environ['AISH_TOOL_DOWNLOADS_DIR']\n"
            "p = os.path.join(d, args['name'])\n"
            "open(p, 'wb').write(b'%PDF-1.7 x')\n"
            "print(json.dumps({'filename': args['name'], 'path': p, 'bytes': 10,\n"
            "                  'mime': None, 'from_vault': 'Payments/attachments/x',\n"
            "                  'vault': 'Main'}, indent=2))\n"
        )
        wrapper.chmod(0o755)
        schema = {k: {"type": t} for k, t in (("note", "string"), ("name", "string"),
                                              ("wait", "number"), ("vault", "string"))}
        tool = tool_plugins.Tool(name=paynotes.INVOICE_TOOL, description="",
                                 executable="./wrapper", mutating=False, schema=schema,
                                 timeout=30, body="", dir=tool_dir)
        runner = paynotes.PluginRunner({paynotes.INVOICE_TOOL: tool}, str(tmp_path))
        fetch_dir, _store = paynotes._fresh_scratch(paynotes.scratch_dir(tmp_path))
        payload, why = paynotes._call_json(
            runner, paynotes.INVOICE_TOOL, {"note": "n", "name": "a.pdf", "wait": 60},
            downloads_dir=fetch_dir)
        assert why == "" and payload is not None
        assert Path(payload["path"]) == fetch_dir / "a.pdf"
        assert (fetch_dir / "a.pdf").read_bytes() == b"%PDF-1.7 x"

    def test_plugin_runner_treats_a_mutating_attachment_tool_as_absent(self, monkeypatch):
        from types import SimpleNamespace

        def tools(mutating: bool):
            return [SimpleNamespace(name=n, mutating=m) for n, m in (
                (paynotes.LIST_TOOL, False), (paynotes.READ_TOOL, False),
                (paynotes.INVOICE_TOOL, mutating))], []

        monkeypatch.setattr(tool_plugins, "discover", lambda cwd: tools(False))
        assert paynotes.INVOICE_TOOL in paynotes.plugin_runner("/x").tools
        monkeypatch.setattr(tool_plugins, "discover", lambda cwd: tools(True))
        runner = paynotes.plugin_runner("/x")
        assert runner is not None and paynotes.INVOICE_TOOL not in runner.tools

    def test_no_attachment_tool_is_an_outcome(self, tmp_path):
        v = _invoice_vault("2026-11 E.ON")
        paynotes.refresh(v, tmp_path, NOW)
        runner = paynotes.PluginRunner({}, str(tmp_path))
        section = paynotes.refresh_invoices(runner, tmp_path, NOW, read=FakeReader())
        assert section["outcome"] == "no tool"
        assert not paynotes.invoices_path(tmp_path).exists()
        assert not paynotes.load_invoices(tmp_path).available

    def test_no_notes_index_is_an_outcome(self, tmp_path):
        section = paynotes.refresh_invoices(FakeVault(), tmp_path, NOW, read=FakeReader())
        assert section["outcome"] == "no notes"


def _pdf(path: Path, *, text_pages: list[str], scan_pages: int = 0) -> Path:
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for text in text_pages:
        doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 545, 700), text, fontsize=11)
    if scan_pages:
        src = pymupdf.open()
        page = src.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 545, 700), "SCANNED. " * 200)
        picture = page.get_pixmap(dpi=100).tobytes("png")
        src.close()
        for _ in range(scan_pages):
            doc.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), stream=picture)
    doc.save(path)
    doc.close()
    return path


PDF_TEXT = ("Faktura VAT nr FV/7/2026\nNumer rachunku: " + ACCOUNT_SPACED +
            "\nRazem do zaplaty: 496,33 zl\n" + "Pozycja uslugi " * 10)


class TestInvoiceReading:
    """Through the REAL reader child (aish/invoicereader.py) with OCR off, as
    the suite has it (`AISH_OCR=0`, inherited by the child)."""

    def _through_refresh(self, path: Path, state: Path) -> tuple[dict, dict]:
        v = FakeVault()
        v.add("2026-11 E.ON", _fm(attachments=[f"[[{path.name}]]"]))
        v.attach("2026-11 E.ON", path.name, path.read_bytes())
        paynotes.refresh(v, state, NOW)
        section = paynotes.refresh_invoices(v, state, NOW)
        return _invoices(state)[path.name], section

    def test_a_text_page_beside_an_unread_scan_is_partial(self, tmp_path):
        """The hollow-extraction case: the text page's facts are kept, and the
        record never claims the document was read whole."""
        path = _pdf(tmp_path / "m.pdf", text_pages=[PDF_TEXT], scan_pages=1)
        rec, _ = self._through_refresh(path, tmp_path / "state")
        assert (rec["status"], rec["read_as"], rec["reason_class"]) == (
            "partial", "text layer", "ocr unavailable")
        assert (rec["pages"], rec["pages_text"], rec["pages_unread"]) == (2, 1, 1)
        assert rec["facts"]["accounts"][0]["account"] == ACCOUNT
        assert rec["page_detail"]["2"]["how"] == "unread"

    def test_a_scan_with_nothing_read_keeps_no_facts(self, tmp_path):
        path = _pdf(tmp_path / "s.pdf", text_pages=[], scan_pages=1)
        rec, section = self._through_refresh(path, tmp_path / "state")
        assert (rec["status"], rec["read_as"], rec["facts"]) == ("unread", "not read", None)
        assert section["counts"]["not_read_reasons"] == {"ocr unavailable": 1}


def _pages_answer(read: dict[int, str], total: int, unread_reason: str = "pages not read"):
    """A reader answer for `total` pages of which `read` (number -> text) were
    read; the rest unread."""
    pages: dict[int, dict] = {}
    for number in range(1, total + 1):
        if number in read:
            pages[number] = {"how": "ocr", "facts": invoicefacts.page_facts(read[number])}
        else:
            pages[number] = {"how": "unread", "reason_class": unread_reason,
                             "reason": "the deadline came first"}
    return {"kind": "pdf", "pages_total": total, "pages": pages}


class TestInvoiceRetry:
    def test_a_permanent_failure_is_given_up_after_three_fruitless_visits(self, tmp_path):
        v = _invoice_vault("2026-11 A")
        reader = FakeReader()
        reader.answers[b"%PDF-1.7 2026-11 A"] = {
            "kind": "pdf", "pages_total": 0, "pages": {},
            "reason_class": paynotes.CONVERSION_FAILED, "reason": "broken"}
        for _ in range(5):
            _both(v, tmp_path, reader)
        assert len(reader.calls) == paynotes.MAX_FRUITLESS_VISITS == 3
        rec = _invoices(tmp_path)["2026-11 A.pdf"]
        assert rec["gave_up"] == "gave up after 3 attempts without progress"
        assert rec["reason_class"] == "conversion failed"
        section = json.loads(paynotes.index_path(tmp_path).read_text())["invoices"]
        assert section["counts"]["gave_up"] == 1 and section["outcome"] == "ok"
        # A changed note gives it a fresh start.
        v.add("2026-11 A", _fm(attachments=["[[2026-11 A.pdf]]"]), modified="2026-11-20 10:00")
        _both(v, tmp_path, reader)
        assert len(reader.calls) == 4
        assert "gave_up" not in _invoices(tmp_path)["2026-11 A.pdf"]

    def test_a_long_scan_adds_pages_visit_by_visit_and_never_rereads_one(self, tmp_path):
        v = _invoice_vault("2026-11 A")
        reader = FakeReader()
        texts = {1: "Strona 1", 2: f"Rachunek {ACCOUNT_SPACED}", 3: "Do zapłaty 496,33 zł"}

        def one_more_page(skip: set) -> dict:
            todo = [n for n in sorted(texts) if n not in skip][:1]
            answer = _pages_answer({n: texts[n] for n in todo}, 3)
            for n in skip:
                del answer["pages"][n]  # the child is never asked for them
            return answer

        reader.answers[b"%PDF-1.7 2026-11 A"] = one_more_page
        sections = [_both(v, tmp_path, reader) for _ in range(3)]
        assert [c[2] for c in reader.calls] == [set(), {1}, {1, 2}]
        assert [s["counts"]["read_ok_this_run"] for s in sections] == [1, 1, 1]
        rec = _invoices(tmp_path)["2026-11 A.pdf"]
        assert (rec["status"], rec["pages_read"], rec["fruitless"]) == ("read", 3, 0)
        assert rec["facts"]["accounts"][0]["account"] == ACCOUNT
        assert {"minor": 49633, "currency": "PLN"}.items() <= rec["facts"]["amounts"][0].items()

    def test_a_partial_that_reads_nothing_new_is_not_progress(self, tmp_path):
        v = _invoice_vault("2026-11 A")
        reader = FakeReader()
        reader.answers[b"%PDF-1.7 2026-11 A"] = lambda skip: {
            **_pages_answer({1: "Strona"} if not skip else {}, 2),
            "pages": {} if skip else _pages_answer({1: "Strona"}, 2)["pages"]}
        first = _both(v, tmp_path, reader)
        second = _both(v, tmp_path, reader)
        assert first["counts"]["read_ok_this_run"] == 1
        assert second["counts"]["read_ok_this_run"] == 0
        assert paynotes.next_wait({"outcome": "ok", "invoices": second}) == \
            paynotes.REFRESH_EVERY
        rec = _invoices(tmp_path)["2026-11 A.pdf"]
        assert (rec["status"], rec["pages_read"], rec["fruitless"]) == ("partial", 1, 1)

    def test_a_changed_file_starts_its_pages_afresh(self, tmp_path):
        v = _invoice_vault("2026-11 A")
        reader = FakeReader()
        reader.answers[b"%PDF-1.7 2026-11 A"] = _pages_answer({1: "Strona"}, 2)
        _both(v, tmp_path, reader)
        v.attach("2026-11 A", "2026-11 A.pdf", b"%PDF-1.7 corrected")
        v.add("2026-11 A", _fm(attachments=["[[2026-11 A.pdf]]"]), modified="2026-11-20 10:00")
        _both(v, tmp_path, reader)
        assert reader.calls[-1][2] == set()


class TestInvoiceProvenance:
    @pytest.mark.parametrize("from_vault, elsewhere", [
        ("Payments/attachments/2026-11 A.pdf", False),
        ("Payments/2026-11 A.pdf", False),
        ("attachments/2026-11 A.pdf", False),
        ("Archive/2019/attachments/2026-11 A.pdf", True),
        ("Other/2026-11 A.pdf", True),
    ])
    def test_where_the_plugin_found_the_file_is_kept_and_judged(self, tmp_path, from_vault,
                                                               elsewhere):
        v = _invoice_vault("2026-11 A")

        def answer(name, args, downloads_dir=None, should_stop=None):
            text = v(name, args, downloads_dir)
            return text.replace("Payments/attachments/2026-11 A.pdf", from_vault)

        paynotes.refresh(v, tmp_path, NOW)
        section = paynotes.refresh_invoices(answer, tmp_path, NOW, read=FakeReader())
        rec = _invoices(tmp_path)["2026-11 A.pdf"]
        assert (rec["from_vault"], rec["fetched_elsewhere"]) == (from_vault, elsewhere)
        assert rec["facts"] is not None  # kept, under the flag
        assert section["counts"]["fetched_elsewhere"] == int(elsewhere)

    def test_one_vault_file_behind_two_notes_is_flagged_on_both(self, tmp_path):
        v = _invoice_vault("2026-11 A", "2026-10 B", "2026-09 C")

        def answer(name, args, downloads_dir=None, should_stop=None):
            text = v(name, args, downloads_dir)
            if name == paynotes.INVOICE_TOOL and args["name"] in ("2026-11 A.pdf",
                                                                   "2026-10 B.pdf"):
                text = text.replace(f"Payments/attachments/{args['name']}",
                                    "Payments/attachments/scan.pdf")
            return text

        paynotes.refresh(v, tmp_path, NOW)
        section = paynotes.refresh_invoices(answer, tmp_path, NOW, read=FakeReader())
        recs = _invoices(tmp_path)
        assert [recs[n]["shared_file"] for n in ("2026-11 A.pdf", "2026-10 B.pdf",
                                                 "2026-09 C.pdf")] == [True, True, False]
        assert section["counts"]["shared_file"] == 2


class TestInvoiceStrayCopy:
    def _lying(self, v, target: Path):
        def answer(name, args, downloads_dir=None, should_stop=None):
            text = v(name, args, downloads_dir)
            if name == paynotes.INVOICE_TOOL:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"%PDF-1.7 stray")
                reported = str(Path(downloads_dir) / args["name"])
                text = text.replace(json.dumps(reported)[1:-1], str(target))
            return text
        return answer

    def test_a_copy_left_in_the_system_temp_is_deleted_with_its_empty_dir(self, tmp_path,
                                                                          monkeypatch):
        systemp = tmp_path / "systemp"
        systemp.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(systemp))
        v = _invoice_vault("2026-11 A")
        stray = systemp / "aish-obsidian-x1" / "2026-11 A.pdf"
        paynotes.refresh(v, tmp_path / "state", NOW)
        paynotes.refresh_invoices(self._lying(v, stray), tmp_path / "state", NOW,
                                  read=FakeReader())
        rec = _invoices(tmp_path / "state")["2026-11 A.pdf"]
        assert (rec["reason_class"], rec["stray_removed"]) == ("fetch failed", True)
        assert not stray.exists() and not stray.parent.exists() and systemp.exists()

    def test_nothing_outside_the_system_temp_is_ever_deleted(self, tmp_path, monkeypatch):
        systemp = tmp_path / "systemp"
        systemp.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(systemp))
        v = _invoice_vault("2026-11 A")
        elsewhere = tmp_path / "Documents" / "2026-11 A.pdf"
        paynotes.refresh(v, tmp_path / "state", NOW)
        paynotes.refresh_invoices(self._lying(v, elsewhere), tmp_path / "state", NOW,
                                  read=FakeReader())
        assert elsewhere.exists()
        rec = _invoices(tmp_path / "state")["2026-11 A.pdf"]
        assert "stray_removed" not in rec and rec["stray_copy_may_exist"] is True

    @pytest.mark.parametrize("relative", [
        "foo.lock",  # directly in the temp dir
        "other-tool-x1/2026-11 A.pdf",  # a mkdtemp of somebody else's
        "aish-obsidian-x1/deeper/2026-11 A.pdf",  # not directly under it
    ])
    def test_only_the_plugins_own_fallback_shape_is_deleted(self, tmp_path, monkeypatch,
                                                            relative):
        systemp = tmp_path / "systemp"
        systemp.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(systemp))
        v = _invoice_vault("2026-11 A")
        target = systemp / relative
        paynotes.refresh(v, tmp_path / "state", NOW)
        paynotes.refresh_invoices(self._lying(v, target), tmp_path / "state", NOW,
                                  read=FakeReader())
        assert target.exists()
        rec = _invoices(tmp_path / "state")["2026-11 A.pdf"]
        assert "stray_removed" not in rec and rec["stray_copy_may_exist"] is True


class TestInvoiceStaleFacts:
    """A changed attachment whose new visit cannot establish its bytes must not
    keep the OLD bytes' digest, facts and pages beside the new visit."""

    def _changed_then(self, tmp_path, monkeypatch, ending: str) -> dict:
        v = _invoice_vault("2026-11 A")
        reader = FakeReader()
        _both(v, tmp_path, reader)
        assert _invoices(tmp_path)["2026-11 A.pdf"]["facts"]
        v.attach("2026-11 A", "2026-11 A.pdf", b"%PDF-1.7 a different, larger file")
        v.add("2026-11 A", _fm(attachments=["[[2026-11 A.pdf]]"]), modified="2026-11-20 10:00")
        if ending == "too large":
            monkeypatch.setattr(paynotes, "MAX_ATTACHMENT_BYTES", 20)
        elif ending == "fetch failed":
            v.file_errors[("Payments/2026-11 A.md", "2026-11 A.pdf")] = "did not finish"
        else:
            reader.raise_on.add(b"%PDF-1.7 a different, larger file")
        _both(v, tmp_path, reader)
        return _invoices(tmp_path)["2026-11 A.pdf"]

    @pytest.mark.parametrize("ending, reason_class", [
        ("too large", "too large"), ("fetch failed", "fetch failed"), ("error", "error"),
    ])
    def test_old_facts_are_dropped_and_said(self, tmp_path, monkeypatch, ending, reason_class):
        rec = self._changed_then(tmp_path, monkeypatch, ending)
        assert rec["reason_class"] == reason_class and rec["status"] == "unread"
        assert rec["stale_facts_dropped"] is True
        for key in ("sha256", "facts", "page_detail"):
            assert key not in rec

    def test_a_good_read_of_the_new_file_clears_the_mark(self, tmp_path, monkeypatch):
        self._changed_then(tmp_path, monkeypatch, "fetch failed")
        v = _invoice_vault("2026-11 A")
        v.attach("2026-11 A", "2026-11 A.pdf", b"%PDF-1.7 a different, larger file")
        v.add("2026-11 A", _fm(attachments=["[[2026-11 A.pdf]]"]), modified="2026-11-21 10:00")
        _both(v, tmp_path)
        rec = _invoices(tmp_path)["2026-11 A.pdf"]
        assert rec["status"] == "read" and "stale_facts_dropped" not in rec and rec["facts"]


class TestInvoiceCadence:
    @pytest.fixture(autouse=True)
    def paynotes_on(self, monkeypatch):
        monkeypatch.delenv("AISH_PAYNOTES")

    def test_the_backlog_gap_is_fifteen_minutes(self):
        assert paynotes.BACKLOG_EVERY == timedelta(minutes=15)

    def test_no_invoice_run_starts_while_a_chat_is_busy(self, tmp_path, monkeypatch):
        v = _invoice_vault("2026-11 A")
        monkeypatch.setattr(paynotes, "read_document", FakeReader())
        r = paynotes.Refresher(tmp_path, make_runner=lambda: v, clock=lambda: NOW,
                               busy=lambda: True)
        waits = _run_refresher(r, 1)
        assert v.fetches() == [] and v.reads() != []
        section = json.loads(paynotes.index_path(tmp_path).read_text())["invoices"]
        assert (section["outcome"], section["deferred"]) == ("deferred", True)
        assert waits == [paynotes.BACKLOG_EVERY.total_seconds()]

    def test_a_chat_turning_busy_stops_the_run_between_attachments(self, tmp_path):
        v = _invoice_vault("2026-11 A", "2026-10 B", "2026-09 C")
        working = threading.Event()
        reader = FakeReader()
        reader.on_read = lambda source: working.set()
        section = _both(v, tmp_path, reader, busy=working.is_set)
        assert len(reader.calls) == 1
        assert (section["remaining"], section["deferred"]) == (2, True)
        assert paynotes.next_wait({"outcome": "ok", "invoices": section}) == \
            paynotes.BACKLOG_EVERY

    def test_the_server_hands_the_refresher_its_sessions_busy_flags(self, tmp_path,
                                                                  monkeypatch):
        from tests.test_server import connected, make_client

        monkeypatch.setattr(paynotes, "plugin_runner", lambda cwd: None)
        env = _app_env(tmp_path)
        client, _ = make_client(env, [])
        with client, connected(client):
            server = client.app.state.server
            assert server.paynotes.busy() is False
            next(iter(server.sessions.values())).busy = True
            assert server.paynotes.busy() is True
            next(iter(server.sessions.values())).busy = False


class TestInvoiceStoppableFetch:
    def _tool(self, tmp_path: Path, body: str, timeout: int = 30) -> tool_plugins.Tool:
        tool_dir = tmp_path / "tools" / "slow"
        tool_dir.mkdir(parents=True)
        wrapper = tool_dir / "wrapper"
        wrapper.write_text("#!/usr/bin/env python3\nimport sys, time\nsys.stdin.read()\n" + body)
        wrapper.chmod(0o755)
        return tool_plugins.Tool(name="slow", description="", executable="./wrapper",
                                 mutating=False, schema={}, timeout=timeout, body="",
                                 dir=tool_dir)

    def test_a_running_wrapper_is_killed_when_stop_is_set(self, tmp_path):
        tool = self._tool(tmp_path, "time.sleep(30)\n")
        started = time.monotonic()
        out = tool_plugins.execute(tool, {}, str(tmp_path),
                                   should_stop=lambda: time.monotonic() - started > 0.5)
        assert time.monotonic() - started < 5
        assert "was stopped" in str(out)

    def test_with_should_stop_the_output_and_timeout_are_as_before(self, tmp_path):
        tool = self._tool(tmp_path, "print('{\"ok\": 1}')\n")
        out = tool_plugins.execute(tool, {}, str(tmp_path), should_stop=lambda: False)
        assert '"ok": 1' in str(out) and "[exit code: 0]" in str(out)
        slow = self._tool(tmp_path / "t", "time.sleep(30)\n", timeout=1)
        started = time.monotonic()
        out = tool_plugins.execute(slow, {}, str(tmp_path), should_stop=lambda: False)
        assert "timed out after 1s" in str(out) and time.monotonic() - started < 5

    def test_the_refresh_hands_its_stop_flag_to_the_fetch(self, tmp_path):
        v = _invoice_vault("2026-11 A")
        handed = []

        def runner(name, args, downloads_dir=None, should_stop=None):
            if name == paynotes.INVOICE_TOOL:
                handed.append(should_stop)
            return v(name, args, downloads_dir)

        stop = threading.Event()
        paynotes.refresh(v, tmp_path, NOW)
        paynotes.refresh_invoices(runner, tmp_path, NOW, read=FakeReader(), stop=stop)
        assert handed == [stop.is_set]


class TestInvoiceRefresh:
    def test_facts_are_stored_typed_and_counted(self, tmp_path):
        section = _both(_invoice_vault("2026-11 E.ON"), tmp_path)
        assert section["outcome"] == "ok"
        c = section["counts"]
        assert (c["listed"], c["read"], c["by_text_layer"], c["with_account"],
                c["with_payable_amount"]) == (1, 1, 1, 1, 1)
        rec = _invoices(tmp_path)["2026-11 E.ON.pdf"]
        assert rec["path"] == "Payments/2026-11 E.ON.md"
        assert rec["extractor"] == paynotes.invoicefacts.EXTRACTOR_VERSION
        assert rec["note_marker"] == {"modified": "2026-11-12 10:00", "size": 900,
                                      "offloaded": False}
        (inv,) = paynotes.load_invoices(tmp_path, NOW).invoices
        assert isinstance(inv, paynotes.InvoiceRecord)
        assert inv.accounts[0]["account"] == ACCOUNT
        assert {"minor": 49633, "currency": "PLN", "currency_source": "written", "count": 1,
                "payable": "line", "currency_seen": True,
                "ambiguous_grouping": False} in inv.amounts
        assert inv.invoice_numbers == [{"value": "FV/123/2026", "where": "line"}]

    def test_unchanged_attachments_are_not_fetched_again(self, tmp_path):
        v = _invoice_vault("2026-11 A", "2026-11 B")
        _both(v, tmp_path)
        v.calls.clear()
        reader = FakeReader()
        section = _both(v, tmp_path, reader)
        assert v.fetches() == [] and reader.calls == []
        assert section["counts"]["read"] == 2 and section["outcome"] == "ok"

    def test_a_changed_note_refetches_and_an_unchanged_file_is_not_read_again(self, tmp_path):
        v = _invoice_vault("2026-11 A")
        _both(v, tmp_path)
        v.calls.clear()
        v.add("2026-11 A", _fm(attachments=["[[2026-11 A.pdf]]"], paid="true"),
              modified="2026-11-13 08:00", size=950)
        reader = FakeReader()
        _both(v, tmp_path, reader)
        assert v.fetches() == [("Payments/2026-11 A.md", "2026-11 A.pdf")]
        assert reader.calls == []  # same bytes, same extractor: the facts stand
        rec = _invoices(tmp_path)["2026-11 A.pdf"]
        assert rec["status"] == "read" and rec["note_marker"]["size"] == 950
        # A changed file IS read.
        v.attach("2026-11 A", "2026-11 A.pdf", b"%PDF-1.7 corrected")
        v.add("2026-11 A", _fm(attachments=["[[2026-11 A.pdf]]"]), modified="2026-11-14 08:00")
        reader = FakeReader()
        _both(v, tmp_path, reader)
        assert len(reader.calls) == 1

    def test_an_extractor_change_reads_everything_once_more(self, tmp_path, monkeypatch):
        v = _invoice_vault("2026-11 A")
        _both(v, tmp_path)
        monkeypatch.setattr(paynotes.invoicefacts, "EXTRACTOR_VERSION", 99)
        reader = FakeReader()
        _both(v, tmp_path, reader)
        assert len(reader.calls) == 1
        assert _invoices(tmp_path)["2026-11 A.pdf"]["extractor"] == 99

    def test_per_run_bound_newest_first_and_the_rest_pending(self, tmp_path):
        v = _invoice_vault("2026-11 A", "2026-10 B", "2026-09 C")
        section = _both(v, tmp_path, max_reads=2)
        assert v.fetches() == [("Payments/2026-11 A.md", "2026-11 A.pdf"),
                               ("Payments/2026-10 B.md", "2026-10 B.pdf")]
        assert (section["outcome"], section["remaining"]) == ("partial", 1)
        assert _invoices(tmp_path)["2026-09 C.pdf"]["status"] == "pending"
        v.calls.clear()
        section = _both(v, tmp_path, max_reads=2)
        assert v.fetches() == [("Payments/2026-09 C.md", "2026-09 C.pdf")]
        assert section["outcome"] == "ok"

    def test_the_wall_clock_budget_is_checked_between_attachments(self, tmp_path):
        v = _invoice_vault("2026-11 A", "2026-10 B", "2026-09 C")
        clock = [0.0]
        reader = FakeReader()
        reader.on_read = lambda source: clock.__setitem__(0, clock[0] + 400)
        section = _both(v, tmp_path, reader, budget=timedelta(seconds=600),
                        monotonic=lambda: clock[0])
        assert len(reader.calls) == 2 and section["remaining"] == 1

    def test_strictly_one_at_a_time_and_the_stop_flag_between_attachments(self, tmp_path):
        v = _invoice_vault("2026-11 A", "2026-10 B", "2026-09 C")
        stop = threading.Event()
        reader = FakeReader()
        reader.on_read = lambda source: stop.set()
        section = _both(v, tmp_path, reader, stop=stop)
        assert len(reader.calls) == 1 and reader.max_active == 1
        assert section["remaining"] == 2

    def test_a_failed_attachment_is_retried_after_never_tried_ones(self, tmp_path):
        v = _invoice_vault("2026-11 A")
        v.file_errors[("Payments/2026-11 A.md", "2026-11 A.pdf")] = "did not finish downloading"
        _both(v, tmp_path)
        v.add("2026-12 New", _fm(attachments=["[[new.pdf]]"]), modified="2026-12-01 10:00")
        v.attach("2026-12 New", "new.pdf", b"%PDF-1.7 new")
        v.calls.clear()
        _both(v, tmp_path, max_reads=1)
        assert v.fetches() == [("Payments/2026-12 New.md", "new.pdf")]
        assert _invoices(tmp_path)["2026-11 A.pdf"]["status"] == "pending"
        del v.file_errors[("Payments/2026-11 A.md", "2026-11 A.pdf")]
        v.calls.clear()
        _both(v, tmp_path)
        rec = _invoices(tmp_path)["2026-11 A.pdf"]
        assert rec["status"] == "read" and rec["attempts"] == 2

    def test_files_are_owner_only_and_digest_checked(self, tmp_path):
        _both(_invoice_vault("2026-11 A"), tmp_path)
        assert stat.S_IMODE(paynotes.invoices_path(tmp_path).stat().st_mode) == 0o600
        assert stat.S_IMODE(paynotes.payments_dir(tmp_path).stat().st_mode) == 0o700
        with paynotes.invoices_path(tmp_path).open("a") as fh:
            fh.write("{}\n")
        loaded = paynotes.load_invoices(tmp_path)
        assert not loaded.available and "does not match" in loaded.reason

    def test_no_facts_file_is_not_zero_invoices(self, tmp_path):
        assert "no index" in paynotes.load_invoices(tmp_path).reason
        paynotes.refresh(FakeVault(), tmp_path, NOW)
        loaded = paynotes.load_invoices(tmp_path)
        assert not loaded.available and "none collected" in loaded.reason

    def test_the_notes_refresh_keeps_the_invoice_section(self, tmp_path):
        v = _invoice_vault("2026-11 A")
        _both(v, tmp_path)
        paynotes.refresh(v, tmp_path, NOW + timedelta(days=1))
        assert paynotes.load_invoices(tmp_path).available
        v.list_error = "no folder"
        paynotes.refresh(v, tmp_path, NOW + timedelta(days=2))
        assert paynotes.load_invoices(tmp_path).available

    def test_a_raising_run_is_recorded_and_the_scratch_emptied(self, tmp_path, monkeypatch):
        v = _invoice_vault("2026-11 A")
        paynotes.refresh(v, tmp_path, NOW)

        def explode(*a, **k):
            paynotes._fresh_scratch(paynotes.scratch_dir(tmp_path))
            raise OSError("disk full")

        monkeypatch.setattr(paynotes, "refresh_invoices", explode)
        section = paynotes.refresh_invoices_recorded(v, tmp_path, NOW)
        assert (section["outcome"], section["error"]) == ("error", "OSError")
        assert not paynotes.scratch_dir(tmp_path).exists()

    def test_removed_attachments_leave_the_file(self, tmp_path):
        v = _invoice_vault("2026-11 A", "2026-10 B")
        _both(v, tmp_path)
        v.hits.pop()
        section = _both(v, tmp_path)
        assert section["counts"]["removed"] == 1
        assert list(_invoices(tmp_path)) == ["2026-11 A.pdf"]


def _run_refresher(refresher: paynotes.Refresher, ticks: int) -> list[float]:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= ticks:
            raise asyncio.CancelledError

    refresher.sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(refresher.run())
    return waits


class TestInvoiceRefresher:
    @pytest.fixture(autouse=True)
    def paynotes_on(self, monkeypatch):
        # The suite turns the whole feature off (`no_background_paynotes`);
        # the Refresher itself never reads that switch, the invoice half does.
        monkeypatch.delenv("AISH_PAYNOTES")

    def test_invoices_follow_the_notes_on_the_refreshers_own_thread(self, tmp_path,
                                                                    monkeypatch):
        v = _invoice_vault("2026-11 A")
        threads: list[str] = []
        reader = FakeReader()
        reader.on_read = lambda source: threads.append(threading.current_thread().name)
        monkeypatch.setattr(paynotes, "read_document", reader)
        r = paynotes.Refresher(tmp_path, make_runner=lambda: v, clock=lambda: NOW)
        _run_refresher(r, 1)
        assert [n for n, _ in v.calls] == ["obsidian_find", "obsidian_read",
                                           "obsidian_attachment"]
        assert threads and threads[0].startswith("aish-paynotes")
        assert paynotes.load_invoices(tmp_path).available

    def test_the_invoice_kill_switch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AISH_PAYNOTES_INVOICES", "0")
        v = _invoice_vault("2026-11 A")
        r = paynotes.Refresher(tmp_path, make_runner=lambda: v, clock=lambda: NOW)
        _run_refresher(r, 1)
        assert v.fetches() == [] and v.reads() != []
        assert "invoices" not in json.loads(paynotes.index_path(tmp_path).read_text())
        monkeypatch.setenv("AISH_PAYNOTES_INVOICES", "1")
        assert paynotes.invoices_enabled()
        monkeypatch.setenv("AISH_PAYNOTES", "0")
        assert not paynotes.invoices_enabled()

    def test_an_invoice_backlog_brings_the_next_run_sooner_only_while_progressing(
        self, tmp_path, monkeypatch
    ):
        v = _invoice_vault("2026-11 A", "2026-10 B", "2026-09 C")
        monkeypatch.setattr(paynotes, "MAX_INVOICES_PER_RUN", 2)
        monkeypatch.setattr(paynotes, "read_document", FakeReader())
        r = paynotes.Refresher(tmp_path, make_runner=lambda: v, clock=lambda: NOW)
        assert _run_refresher(r, 2) == [paynotes.BACKLOG_EVERY.total_seconds(),
                                        paynotes.REFRESH_EVERY.total_seconds()]

        stuck = _invoice_vault("2026-11 A", "2026-10 B", "2026-09 C")
        for key in stuck.files:
            stuck.file_errors[key] = "did not finish downloading within 60s"
        r = paynotes.Refresher(tmp_path / "2", make_runner=lambda: stuck, clock=lambda: NOW)
        assert _run_refresher(r, 2) == [paynotes.REFRESH_EVERY.total_seconds()] * 2
        assert len(stuck.fetches()) == 4  # two a run, never a tight loop

    def test_next_wait_reads_both_halves(self):
        soon, day = paynotes.BACKLOG_EVERY, paynotes.REFRESH_EVERY
        done = {"outcome": "ok", "counts": {"read_ok_this_run": 0}}
        assert paynotes.next_wait({**done, "invoices": {
            "outcome": "partial", "counts": {"read_ok_this_run": 3}}}) == soon
        assert paynotes.next_wait({**done, "invoices": {
            "outcome": "partial", "counts": {"read_ok_this_run": 0}}}) == day

    def test_nothing_from_an_invoice_reaches_the_log(self, tmp_path, monkeypatch, capsys):
        v = _invoice_vault("2026-11 E.ON")
        reader = FakeReader()
        reader.raise_on.add(b"%PDF-1.7 2026-11 E.ON")
        monkeypatch.setattr(paynotes, "read_document", reader)
        _run_refresher(paynotes.Refresher(tmp_path, make_runner=lambda: v,
                                          clock=lambda: NOW), 1)
        reader.raise_on.clear()
        _run_refresher(paynotes.Refresher(tmp_path / "2", make_runner=lambda: v,
                                          clock=lambda: NOW), 1)
        err = capsys.readouterr().err
        assert "invoices" in err
        for secret in ("E.ON", "1240", "496", "FV/123", ACCOUNT):
            assert secret not in err


class TestInvoiceServer:
    def test_a_stuck_fetch_never_holds_the_server_or_its_shutdown(self, tmp_path,
                                                                 monkeypatch):
        from tests.test_server import connected, make_client, recv_until

        monkeypatch.delenv("AISH_PAYNOTES")
        release = threading.Event()
        entered = threading.Event()
        v = _invoice_vault("2026-11 A")

        def stuck(args):
            entered.set()
            release.wait(10)

        v.on_fetch = stuck
        monkeypatch.setattr(paynotes, "plugin_runner", lambda cwd: v)
        monkeypatch.setattr(paynotes, "read_document", FakeReader())
        env = _app_env(tmp_path)
        client, _ = make_client(env, [])
        try:
            started = time.monotonic()
            with client, connected(client) as (ws, hello, _):
                assert hello["type"] == "hello"
                assert entered.wait(5)
                ws.send_json({"type": "trash"})
                recv_until(ws, "trash_list")
                assert not release.is_set()
            # The client's exit ran the server's shutdown while the fetch was
            # still stuck in its thread.
            assert not release.is_set() and time.monotonic() - started < 9
        finally:
            release.set()


class TestInvoiceCli:
    def test_prints_counts_and_outcome_words_only(self, tmp_path, monkeypatch, capsys):
        from aish import cli

        v = _invoice_vault("2026-11 E.ON", "2026-10 PGNiG")
        v.file_errors[("Payments/2026-10 PGNiG.md", "2026-10 PGNiG.pdf")] = (
            "'2026-10 PGNiG.pdf' is offloaded to iCloud and did not finish downloading")
        paynotes.refresh(v, tmp_path, datetime.now())
        paynotes.refresh_invoices(v, tmp_path, datetime.now(), read=FakeReader())
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        assert cli._paynotes_cli([]) == 0
        out = capsys.readouterr().out
        assert "attachments listed: 2" in out and "read by text layer: 1" in out
        assert "with a valid account: 1" in out and "with a payable amount: 1" in out
        assert "fetch failed: 1" in out and "remaining: 0" in out
        for secret in ("E.ON", "PGNiG", "1240", "496", "FV/123", "offloaded", "2857"):
            assert secret not in out
