"""Text recognition for scanned PDF pages — macOS Vision, in a child process.

A scanned page has no text layer, so the only way to its words is to read the
picture. A vision model can do that itself; a text-only model (the owner's
`local:` model, in the session that motivated this) cannot, and before this
it improvised OCR in the shell: Homebrew installs, a dozen approval cards and
ten minutes, still unfinished (`docs/documents-and-pdf.md`). macOS ships a
text recognizer in the Vision framework, so the capability needs no install
beyond a wheel.

Three properties carry the design:

1 · **A child process, never in-process.** One recognition peaks around
150 MB resident, and aish-web is one long-lived process on a memory-tight
machine; in a child that memory goes back on exit, and a crash inside Vision
ends the child, not the server. Children run one at a time per process, so two
scans read in parallel cannot double the peak.

2 · **Pages stream back as they finish.** The worker prints one JSON line per
page, so a deadline or a Stop that lands mid-batch keeps every page already
read.

3 · **Unavailable is a reason, not an exception the caller swallows.**
`unavailable_reason()` says why (not macOS, the binding missing, `AISH_OCR=0`)
and the caller states it where the page's words would have been. A scan page is
never silently empty.

Run as `python -P -m aish.ocr` with a JSON request on stdin; the parent half is
`recognise`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Pages are rasterised at this resolution for recognition. Measured on a
# 12-page Polish scan: 150, 200 and 300 dpi recognise within ±5% of each other
# in characters, and 300 costs 2.5x the rasterising time of 200.
OCR_DPI = 200

# Bounds on one call. Recognition measured 0.5-1.0 s per page; the allowance is
# generous on purpose, since a deadline that fires on an ordinary page turns a
# readable document into an unread one.
SECONDS_PER_PAGE = 3.0
# The first import of the framework binding after an install compiles its
# bytecode: measured 13.8 s once, 0.15 s after.
STARTUP_SECONDS = 20.0
# How often a running child is checked against Stop.
_POLL_SECONDS = 0.25

_ONE_AT_A_TIME = threading.Lock()


class OcrUnavailable(Exception):
    """Text recognition cannot run here; the message says why."""


@dataclass(frozen=True)
class Batch:
    """What one call recognised. `text[page]` may be "" — the recognizer ran
    and found no words, which is not the same as not having run. `engine`
    names what produced the text, so a cached result can be told apart from
    one a later macOS would give."""

    text: dict[int, str] = field(default_factory=dict)
    failed: dict[int, str] = field(default_factory=dict)
    engine: str = ""
    stopped: bool = False


def unavailable_reason() -> str | None:
    """Why recognition cannot run here, or None when it can."""
    if os.environ.get("AISH_OCR", "").strip() == "0":
        return "text recognition is turned off here (AISH_OCR=0)"
    if sys.platform != "darwin":
        return "text recognition needs macOS's Vision framework, and this machine is not a Mac"
    if importlib.util.find_spec("Vision") is None:
        return (
            "text recognition needs the pyobjc-framework-Vision package, which is "
            "not installed in aish's environment"
        )
    return None


def worker_command() -> list[str]:
    """The child's argv. A seam: tests substitute a fake worker to exercise the
    protocol (streaming, deadline, crash) on any platform."""
    return [sys.executable, "-P", "-m", "aish.ocr"]


def recognise(
    pngs: dict[int, bytes], should_stop: Callable[[], bool] | None = None
) -> Batch:
    """Recognise the text on each page picture (page number -> PNG bytes).

    Raises OcrUnavailable when recognition cannot run at all. A failure on one
    page, a crashed child or the deadline lands in `Batch.failed` for exactly
    the pages it cost; a Stop leaves the unfinished pages out of both maps and
    sets `stopped`."""
    if reason := unavailable_reason():
        raise OcrUnavailable(reason)
    if not pngs:
        return Batch()
    deadline = STARTUP_SECONDS + SECONDS_PER_PAGE * len(pngs)
    with _ONE_AT_A_TIME, tempfile.TemporaryDirectory(prefix="aish-ocr-") as scratch:
        request: dict[str, str] = {}
        for number, data in pngs.items():
            path = Path(scratch) / f"p{number}.png"
            path.write_bytes(data)
            request[str(number)] = str(path)
        return run_child(request, deadline, should_stop)


def run_child(
    request: dict[str, str], deadline: float, should_stop: Callable[[], bool] | None = None
) -> Batch:
    """Run the worker over `request` (page -> picture path) and collect what it
    printed, whatever way it ended."""
    child = subprocess.Popen(
        worker_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        # A kill can land mid-character; strict decoding would then raise and
        # lose every page already finished.
        errors="replace",
    )
    started = time.monotonic()
    payload: str | None = json.dumps({"pages": request})
    ended = ""  # "", "deadline" or "stopped"
    while True:
        try:
            # Retrying communicate() after a timeout loses no output; the
            # request is only written on the first call.
            out, err = child.communicate(payload, timeout=_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            payload = None
            if should_stop is not None and should_stop():
                ended = "stopped"
            elif time.monotonic() - started > deadline:
                ended = "deadline"
            else:
                continue
            child.kill()
            out, err = child.communicate()
            break
    text, failed, engine = _parse(out)
    pending = [int(n) for n in request if int(n) not in text and int(n) not in failed]
    if pending and ended != "stopped":
        if ended == "deadline":
            why = f"text recognition did not finish within {round(deadline)} s"
        else:
            tail = (err or "").strip().splitlines()[-1:] or [f"exit status {child.returncode}"]
            why = f"text recognition stopped before reaching this page ({tail[0][:200]})"
        failed.update({number: why for number in pending})
    return Batch(text=text, failed=failed, engine=engine, stopped=ended == "stopped")


def _parse(out: str) -> tuple[dict[int, str], dict[int, str], str]:
    text: dict[int, str] = {}
    failed: dict[int, str] = {}
    engine = ""
    for line in (out or "").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if "engine" in row:
            engine = str(row["engine"])[:200]
            continue
        try:
            number = int(row["page"])
        except (KeyError, TypeError, ValueError):
            continue
        if "error" in row:
            failed[number] = f"text recognition failed on this page ({str(row['error'])[:200]})"
        else:
            text[number] = str(row.get("text") or "")
    return text, failed, engine


# ------------------------------------------------------------------ the child


def _worker() -> None:
    """Recognise each page with Vision's defaults at the accurate level.

    No language list is set. Measured on Polish pages: the default (en-US),
    pl-PL/en-US and en-US/pl-PL recognised identical text with every
    diacritic, and automatic language detection lost two on one page — so a
    list would be plumbing with nothing measured behind it."""
    import Vision
    from Foundation import NSURL

    pages = json.loads(sys.stdin.read())["pages"]
    request = Vision.VNRecognizeTextRequest.alloc().init()
    print(
        json.dumps(
            {
                "engine": f"macOS {platform.mac_ver()[0]} Vision VNRecognizeTextRequest "
                f"revision {request.revision()}, accurate, language correction"
            }
        ),
        flush=True,
    )
    for number, path in pages.items():
        try:
            request = Vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            request.setUsesLanguageCorrection_(True)
            handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
                NSURL.fileURLWithPath_(path), None
            )
            ok, error = handler.performRequests_error_([request], None)
            if not ok:
                raise RuntimeError(str(error))
            lines = []
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if candidates:
                    lines.append(str(candidates[0].string()))
            row = {"page": int(number), "text": "\n".join(lines)}
        except Exception as exc:  # one bad page must not cost the others
            row = {"page": int(number), "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    _worker()
