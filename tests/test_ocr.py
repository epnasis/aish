"""Text recognition for scanned pages (`aish/ocr.py`).

The protocol half — a child that streams one JSON line per page, a deadline, a
crash, a Stop — is tested with a FAKE worker on every platform, so Linux CI
exercises exactly the code that decides which pages were read. Only the last
class needs macOS's own recognizer, and skips anywhere it is absent.
"""

import json
import sys
import textwrap
import threading

import pytest

from aish import ocr


def _fake_worker(monkeypatch, body: str) -> None:
    """Make the child a small Python script. It gets the real request on
    stdin, as `{"pages": {"1": path, ...}}`."""
    script = "import json, sys, time\nreq = json.loads(sys.stdin.read())\n" + textwrap.dedent(
        body
    )
    monkeypatch.setattr(ocr, "worker_command", lambda: [sys.executable, "-c", script])


REQUEST = {"1": "/nonexistent/p1.png", "2": "/nonexistent/p2.png"}


class TestTheProtocol:
    def test_pages_stream_back_with_what_read_them(self, monkeypatch):
        _fake_worker(monkeypatch, """
            print(json.dumps({"engine": "fake engine 1"}), flush=True)
            for n in req["pages"]:
                print(json.dumps({"page": int(n), "text": f"words of page {n}"}), flush=True)
        """)
        batch = ocr.run_child(REQUEST, deadline=30)
        assert batch.text == {1: "words of page 1", 2: "words of page 2"}
        assert batch.failed == {} and batch.engine == "fake engine 1"
        assert not batch.stopped

    def test_found_nothing_is_kept_apart_from_not_having_run(self, monkeypatch):
        _fake_worker(monkeypatch, """
            print(json.dumps({"page": 1, "text": ""}), flush=True)
            print(json.dumps({"page": 2, "error": "VNError: no image"}), flush=True)
        """)
        batch = ocr.run_child(REQUEST, deadline=30)
        assert batch.text == {1: ""}
        assert "VNError: no image" in batch.failed[2]

    def test_a_crash_costs_only_the_pages_it_had_not_reached(self, monkeypatch):
        _fake_worker(monkeypatch, """
            print(json.dumps({"page": 1, "text": "kept"}), flush=True)
            print("Segmentation fault in Vision", file=sys.stderr)
            sys.exit(139)
        """)
        batch = ocr.run_child(REQUEST, deadline=30)
        assert batch.text == {1: "kept"}
        assert "stopped before reaching this page" in batch.failed[2]
        assert "Segmentation fault in Vision" in batch.failed[2]

    def test_the_deadline_keeps_what_finished_and_says_what_did_not(self, monkeypatch):
        _fake_worker(monkeypatch, """
            print(json.dumps({"page": 1, "text": "kept"}), flush=True)
            time.sleep(60)
        """)
        batch = ocr.run_child(REQUEST, deadline=1.0)
        assert batch.text == {1: "kept"}
        assert "did not finish within 1 s" in batch.failed[2]

    def test_stop_ends_the_child_and_blames_no_page(self, monkeypatch):
        """A Stop is the owner's, not the page's: nothing is marked failed, so
        the next read simply tries again."""
        _fake_worker(monkeypatch, """
            print(json.dumps({"page": 1, "text": "kept"}), flush=True)
            time.sleep(60)
        """)
        stop = threading.Event()
        threading.Timer(0.6, stop.set).start()
        batch = ocr.run_child(REQUEST, deadline=30, should_stop=stop.is_set)
        assert batch.stopped
        assert batch.text == {1: "kept"} and batch.failed == {}

    def test_garbage_on_stdout_is_not_a_page(self, monkeypatch):
        _fake_worker(monkeypatch, """
            print("not json")
            print(json.dumps([1, 2]))
            print(json.dumps({"page": "x", "text": "?"}))
            print(json.dumps({"page": 2, "text": "real"}), flush=True)
        """)
        batch = ocr.run_child(REQUEST, deadline=30)
        assert batch.text == {2: "real"}
        assert set(batch.failed) == {1}


class TestAvailability:
    def test_the_kill_switch_is_a_stated_reason(self, monkeypatch):
        monkeypatch.setenv("AISH_OCR", "0")
        assert "AISH_OCR=0" in ocr.unavailable_reason()
        with pytest.raises(ocr.OcrUnavailable, match="AISH_OCR=0"):
            ocr.recognise({1: b"png"})

    def test_off_a_mac_it_says_so(self, monkeypatch):
        monkeypatch.delenv("AISH_OCR", raising=False)
        monkeypatch.setattr(ocr.sys, "platform", "linux")
        assert "not a Mac" in ocr.unavailable_reason()

    def test_without_the_binding_it_names_the_package(self, monkeypatch):
        monkeypatch.delenv("AISH_OCR", raising=False)
        monkeypatch.setattr(ocr.sys, "platform", "darwin")
        monkeypatch.setattr(ocr.importlib.util, "find_spec", lambda name: None)
        assert "pyobjc-framework-Vision" in ocr.unavailable_reason()


def _vision_present() -> bool:
    if sys.platform != "darwin":
        return False
    import importlib.util

    return importlib.util.find_spec("Vision") is not None


@pytest.mark.skipif(not _vision_present(), reason="needs macOS's Vision framework binding")
class TestMacOSVision:
    def test_a_generated_page_is_read(self, monkeypatch):
        """The real recognizer, end to end through the real child, on a page
        made here from known words — never a real document."""
        pymupdf = pytest.importorskip("pymupdf")
        monkeypatch.delenv("AISH_OCR", raising=False)
        doc = pymupdf.open()
        page = doc.new_page(width=600, height=300)
        page.insert_text((40, 120), "INVOICE 4721 PAID", fontsize=36)
        png = page.get_pixmap(dpi=ocr.OCR_DPI).tobytes("png")
        doc.close()
        batch = ocr.recognise({1: png})
        assert batch.failed == {}
        assert "INVOICE 4721 PAID" in batch.text[1]
        assert "Vision" in batch.engine

    def test_a_jpeg_picture_is_read_as_it_is(self, monkeypatch):
        """A picture file goes over in its own format, not re-encoded."""
        pymupdf = pytest.importorskip("pymupdf")
        monkeypatch.delenv("AISH_OCR", raising=False)
        doc = pymupdf.open()
        page = doc.new_page(width=600, height=300)
        page.insert_text((40, 120), "RECEIPT 5530 CARD", fontsize=36)
        jpeg = page.get_pixmap(dpi=ocr.OCR_DPI).tobytes("jpeg")
        doc.close()
        assert ocr.picture_format(jpeg) == ("JPEG", ".jpg")
        batch = ocr.recognise({1: jpeg, 2: b"\x89PNG\r\n\x1a\n" + b"garbage" * 40})
        assert "RECEIPT 5530 CARD" in batch.text[1]
        # 600x300 pt at 200 dpi
        assert batch.shape[1] == {"width": 1667, "height": 834, "frames": 1}
        assert batch.failed[2] == ocr.UNDECODABLE and 2 in batch.final


def test_the_request_names_each_page_once(monkeypatch, tmp_path):
    """What the parent writes is what the worker reads: one picture per page,
    under the page's own number."""
    seen = tmp_path / "seen.json"
    _fake_worker(monkeypatch, f"""
        open({str(seen)!r}, "w").write(json.dumps(req))
        for n in req["pages"]:
            words = open(req["pages"][n], "rb").read().decode()
            print(json.dumps({{"page": int(n), "text": words}}))
    """)
    monkeypatch.setattr(ocr, "unavailable_reason", lambda: None)
    batch = ocr.recognise({3: b"three", 7: b"seven"})
    assert batch.text == {3: "three", 7: "seven"}
    assert sorted(json.loads(seen.read_text())["pages"]) == ["3", "7"]


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"x" * 32
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"x" * 32


def test_the_shape_and_an_undecodable_file_come_back(monkeypatch):
    """The worker probes each file first: what it decoded as, or that macOS
    could not decode it at all — a failure no retry will change."""
    _fake_worker(monkeypatch, """
        print(json.dumps({"page": 1, "text": "hi", "width": 1200, "height": 8000,
                          "frames": 1}), flush=True)
        print(json.dumps({"page": 2, "undecodable": True}), flush=True)
    """)
    batch = ocr.run_child(REQUEST, deadline=30)
    assert batch.shape == {1: {"width": 1200, "height": 8000, "frames": 1}}
    assert batch.failed == {2: ocr.UNDECODABLE} and batch.final == {2}


class TestPictureFormat:
    """What the recognizer is handed is judged by its bytes, never its name."""

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (b"\x89PNG\r\n\x1a\n" + b"x" * 8, ("PNG", ".png")),
            (JPEG, ("JPEG", ".jpg")),
            (b"GIF89a" + b"x" * 8, ("GIF", ".gif")),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ("WebP", ".webp")),
            (HEIC, ("HEIC", ".heic")),
            (b"\x00\x00\x00\x1cftypmif1\x00\x00\x00\x00", ("HEIF", ".heif")),
            (b"II*\x00" + b"x" * 8, ("TIFF", ".tif")),
            (b"MM\x00*" + b"x" * 8, ("TIFF", ".tif")),
        ],
    )
    def test_pictures_are_named_by_their_magic(self, data, expected):
        assert ocr.picture_format(data) == expected

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"BMW i3 premium 3 091 zl",
            b"%PDF-1.7",
            b"\x00\x00\x00\x18ftypmp42",  # a video
            b"BM" + b"\x00" * 60,
        ],
    )
    def test_anything_else_is_not_a_picture(self, data):
        """BMP is out on purpose: its magic is "BM", which a note about a BMW
        starts with too."""
        assert ocr.picture_format(data) is None


def test_each_picture_is_handed_over_under_the_suffix_its_bytes_say(monkeypatch, tmp_path):
    seen = tmp_path / "seen.json"
    _fake_worker(monkeypatch, f"""
        open({str(seen)!r}, "w").write(json.dumps(req))
        for n in req["pages"]:
            print(json.dumps({{"page": int(n), "text": "ok"}}))
    """)
    monkeypatch.setattr(ocr, "unavailable_reason", lambda: None)
    ocr.recognise({1: JPEG, 2: HEIC, 3: b"not a picture at all"})
    paths = json.loads(seen.read_text())["pages"]
    assert paths["1"].endswith("p1.jpg") and paths["2"].endswith("p2.heic")
    assert paths["3"].endswith("p3.png")  # a rasterised page has always gone as PNG
