"""The invoice reader child (aish/invoicereader.py, docs/payments.md "Invoice facts").

`read_pages` is the child's work, tested in-process; `run_reader` is the
parent half, tested with the REAL child once and with stand-in children for
the deadline, Stop, a crash and the priority. OCR is off suite-wide
(`AISH_OCR=0`); where a test needs it, a fake recogniser stands in.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from aish import documents, invoicereader, ocr

pymupdf = pytest.importorskip("pymupdf")

ACCOUNT = "PL72124069601701080000242857"
TEXT = ("Faktura VAT nr FV/7/2026\nNumer rachunku: 72 1240 6960 1701 0800 0024 2857\n"
        "Razem do zaplaty: 496,33 zl\n" + "Pozycja uslugi " * 10)


def _scan_png() -> bytes:
    src = pymupdf.open()
    page = src.new_page()
    page.insert_textbox(pymupdf.Rect(50, 50, 545, 700), "SCANNED. " * 200)
    data = page.get_pixmap(dpi=60).tobytes("png")
    src.close()
    return data


def _pdf(path: Path, pages: list[str | None], big: set[int] = frozenset()) -> Path:
    """`pages`: text for a text page, None for a scan; `big` page numbers are
    5000 pt square."""
    doc = pymupdf.open()
    scan = _scan_png()
    for number, text in enumerate(pages, start=1):
        size = (5000, 5000) if number in big else (595, 842)
        page = doc.new_page(width=size[0], height=size[1])
        if text is None:
            page.insert_image(pymupdf.Rect(0, 0, *size), stream=scan)
        else:
            page.insert_textbox(pymupdf.Rect(50, 50, 545, 700), text, fontsize=11)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def fake_ocr(monkeypatch):
    calls: list[list[int]] = []

    def recognise(pictures, should_stop=None, seconds_each=None, low_priority=False):
        assert low_priority is True
        calls.append(sorted(pictures))
        return ocr.Batch(text={n: f"Kwota do zapłaty {n}0,00 zł" for n in pictures},
                         engine="fake")

    monkeypatch.setattr(ocr, "unavailable_reason", lambda: None)
    monkeypatch.setattr(ocr, "recognise", recognise)
    return calls


class TestReadPages:
    def read(self, path: Path, tmp_path: Path, skip=frozenset(), seconds=60.0, **kw):
        return invoicereader.read_pages(path, tmp_path / "store", set(skip), seconds, **kw)

    def test_a_text_page_gives_facts_and_no_text(self, tmp_path):
        out = self.read(_pdf(tmp_path / "a.pdf", [TEXT]), tmp_path)
        assert (out["kind"], out["pages_total"]) == ("pdf", 1)
        page = out["pages"][1]
        assert page["how"] == "text" and set(page) == {"how", "facts"}
        assert page["facts"]["accounts"][0]["account"] == ACCOUNT

    def test_a_scan_without_ocr_is_unread_with_the_pipelines_reason(self, tmp_path):
        out = self.read(_pdf(tmp_path / "s.pdf", [None]), tmp_path)
        assert out["pages"][1]["how"] == "unread"
        assert out["pages"][1]["reason_class"] == "ocr unavailable"
        assert "AISH_OCR=0" in out["pages"][1]["reason"]

    def test_more_pages_than_the_cap_are_not_read_at_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(invoicereader, "MAX_PAGES", 2)
        converted = []
        monkeypatch.setattr(documents, "convert", lambda *a, **k: converted.append(a))
        out = self.read(_pdf(tmp_path / "l.pdf", [TEXT, TEXT, TEXT]), tmp_path)
        assert (out["reason_class"], out["pages_total"], out["pages"]) == (
            "too many pages", 3, {})
        assert converted == []

    def test_a_page_too_large_to_render_is_unread_before_any_pixmap(self, tmp_path,
                                                                    monkeypatch, fake_ocr):
        rendered: list[float] = []
        original = pymupdf.Page.get_pixmap

        def watched(self, *a, **k):
            rendered.append(self.rect.width)
            return original(self, *a, **k)

        monkeypatch.setattr(pymupdf.Page, "get_pixmap", watched)
        out = self.read(_pdf(tmp_path / "b.pdf", [None, None], big={2}), tmp_path)
        assert out["pages"][2]["reason_class"] == "page too large to render"
        assert out["pages"][1]["how"] == "ocr"
        assert rendered and max(rendered) < 1000  # the big page was never rendered

    def test_pages_read_before_are_cut_out_before_conversion(self, tmp_path, monkeypatch):
        seen: list[int] = []
        original = documents.convert

        def spy(path, store, *a, **k):
            seen.append(documents.page_count(path))
            return original(path, store, *a, **k)

        monkeypatch.setattr(documents, "convert", spy)
        out = self.read(_pdf(tmp_path / "t.pdf", ["Strona jeden " * 9, TEXT,
                                                  "Strona trzy " * 9]), tmp_path, skip={1, 3})
        assert seen == [1] and set(out["pages"]) == {2}
        assert out["pages"][2]["facts"]["accounts"][0]["account"] == ACCOUNT
        assert out["pages_total"] == 3

    def test_ocr_continues_across_calls_within_one_visit(self, tmp_path, monkeypatch, fake_ocr):
        monkeypatch.setattr(documents, "OCR_PAGES_PER_CALL", 1)
        out = self.read(_pdf(tmp_path / "s.pdf", [None, None, None]), tmp_path)
        assert fake_ocr == [[1], [2], [3]]
        assert [out["pages"][n]["how"] for n in (1, 2, 3)] == ["ocr", "ocr", "ocr"]

    def test_ocr_stops_at_the_deadline_and_keeps_what_was_read(self, tmp_path, monkeypatch,
                                                               fake_ocr):
        monkeypatch.setattr(documents, "OCR_PAGES_PER_CALL", 1)
        now = [0.0]

        def clock() -> float:
            return now[0]

        def tick(*a, **k):
            now[0] += 1.0
            return ocr.Batch(text={n: "Saldo 1,00 zł" for n in a[0]}, engine="fake")

        monkeypatch.setattr(ocr, "recognise", tick)
        out = self.read(_pdf(tmp_path / "s.pdf", [None, None, None]), tmp_path,
                        seconds=invoicereader.ANSWER_MARGIN + 2, clock=clock)
        hows = [out["pages"][n]["how"] for n in (1, 2, 3)]
        assert hows == ["ocr", "ocr", "unread"]
        assert out["pages"][3]["reason_class"] == "pages not read"

    def test_bytes_decide_not_the_name(self, tmp_path):
        fake = tmp_path / "invoice.pdf"
        fake.write_bytes(b"PK\x03\x04 a zip, named like a pdf")
        assert self.read(fake, tmp_path)["reason_class"] == "not a pdf or picture"

    def test_a_broken_pdf_is_a_conversion_failure(self, tmp_path):
        broken = tmp_path / "b.pdf"
        broken.write_bytes(b"%PDF-1.7 not really")
        assert self.read(broken, tmp_path)["reason_class"] == "conversion failed"

    def test_a_picture_is_one_scanned_page(self, tmp_path):
        picture = tmp_path / "scan.png"
        picture.write_bytes(_scan_png())
        out = self.read(picture, tmp_path)
        assert (out["kind"], out["pages_total"]) == ("picture", 1)
        assert out["pages"][1]["reason_class"] == "ocr unavailable"


class TestRunReader:
    def test_the_real_child_returns_facts_never_text(self, tmp_path):
        path = _pdf(tmp_path / "a.pdf", [TEXT])
        out = invoicereader.run_reader(path, tmp_path / "store", set(), 60)
        assert out["pages"][1]["how"] == "text"
        assert set(out["pages"][1]) == {"how", "facts"}
        assert out["pages"][1]["facts"]["accounts"][0]["account"] == ACCOUNT
        assert "Pozycja" not in json.dumps(out)  # the page's words never cross

    def test_the_child_s_vocabulary_tallies_reach_the_parent(self, tmp_path):
        from aish import vocab

        vocab.reset()
        invoicereader.run_reader(_pdf(tmp_path / "a.pdf", [TEXT]), tmp_path / "store",
                                 set(), 60)
        tallies = vocab.drain()
        assert tallies["invoicefacts._PAYABLE_WORDS"] == {"asked": 1, "matched": 1}
        assert tallies["invoicefacts._DUE_WORDS"] == {"asked": 1, "matched": 0}

    def test_the_child_gets_a_scrubbed_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AISH_TEST_SECRET_MARKER", "sk-not-for-the-child")
        monkeypatch.setenv("LC_ALL", "pl_PL.UTF-8")
        self._child(monkeypatch, (
            "import json, os, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'kind': '', 'pages_total': 0, 'pages': {},\n"
            "                  'env': sorted(os.environ)}))\n"))
        out = invoicereader.run_reader(tmp_path / "x.pdf", tmp_path, set(), 10)
        assert "AISH_TEST_SECRET_MARKER" not in out["env"]
        assert {"PATH", "AISH_OCR", "LC_ALL"} <= set(out["env"])
        assert set(out["env"]) <= set(invoicereader._CHILD_ENV) | {"LC_ALL", "LC_CTYPE",
                                                                    "__CF_USER_TEXT_ENCODING"}

    def _child(self, monkeypatch, code: str) -> None:
        monkeypatch.setattr(invoicereader, "reader_command",
                            lambda: [sys.executable, "-c", code])

    def test_a_stuck_child_and_its_grandchild_are_killed_at_the_deadline(self, tmp_path,
                                                                        monkeypatch):
        pidfile = tmp_path / "grandchild.pid"
        self._child(monkeypatch, (
            "import subprocess, sys, time\n"
            "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(g.pid))\n"
            "time.sleep(60)\n"))
        started = time.monotonic()
        out = invoicereader.run_reader(tmp_path / "x.pdf", tmp_path, set(), 1.5)
        assert out["reason_class"] == "timed out" and time.monotonic() - started < 6
        grandchild = int(pidfile.read_text())
        time.sleep(0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild, 0)

    def test_stop_kills_the_child(self, tmp_path, monkeypatch):
        self._child(monkeypatch, "import time; time.sleep(60)")
        started = time.monotonic()
        out = invoicereader.run_reader(tmp_path / "x.pdf", tmp_path, set(), 60,
                                       should_stop=lambda: time.monotonic() - started > 0.5)
        assert out["reason_class"] == "stopped" and time.monotonic() - started < 5

    def test_a_child_with_no_answer_is_a_reader_failure(self, tmp_path, monkeypatch):
        self._child(monkeypatch, "import sys; sys.exit(3)")
        out = invoicereader.run_reader(tmp_path / "x.pdf", tmp_path, set(), 10)
        assert out["reason_class"] == "reader failed" and "exited 3" in out["reason"]

    def test_the_child_runs_at_the_lowest_priority(self, tmp_path, monkeypatch):
        self._child(monkeypatch, (
            "import json, os, sys, time\n"
            "sys.stdin.read()\n"
            "time.sleep(0.5)\n"
            "print(json.dumps({'kind': '', 'pages_total': 0, 'pages': {},\n"
            "                  'nice': os.getpriority(os.PRIO_PROCESS, 0)}))\n"))
        out = invoicereader.run_reader(tmp_path / "x.pdf", tmp_path, set(), 10)
        assert out["nice"] == invoicereader.LOW_PRIORITY
