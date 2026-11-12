"""One invoice file's pages read in a CHILD process, and only facts returned.

The background invoice refresh (`paynotes.refresh_invoices`,
docs/payments.md "Invoice facts") hands each fetched file to this module in a
child process: `python -P -m aish.invoicereader`, request on stdin, one JSON
result on stdout. A child, for the same reasons OCR runs in one
(docs/documents-and-pdf.md): a pathological PDF can take unbounded time and
memory in PyMuPDF, and in aish-web's own process it would wedge the refresher
thread and grow the server. In a child the parent can kill it on a deadline or
a Stop, its memory goes back on exit, and the whole child runs at the lowest
CPU priority (its OCR grandchild inherits it).

**Only facts cross the process boundary.** Per page: how its words were
obtained, and `invoicefacts.page_facts` of them — never the text.

**Bounded before anything is allocated.** A PDF with more than `MAX_PAGES`
pages is not read at all. A page whose MediaBox at OCR resolution exceeds
`MAX_RENDER_PIXELS` is excluded BEFORE any pixmap exists, and is recorded as
unread. Pages already read in an earlier visit (`skip`) are cut out of the
document before conversion, so they are never converted or recognised again.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: A PDF longer than this is not an invoice this refresh reads.
MAX_PAGES = 60
#: A page larger than this at OCR resolution is not rendered: ~25 MP is an A1
#: sheet at 200 dpi (an A4 page is ~3.9 MP), ~75 MB as RGB.
MAX_RENDER_PIXELS = 25_000_000
#: Seconds the child keeps for writing its answer after its own OCR deadline.
ANSWER_MARGIN = 5.0
_POLL_SECONDS = 0.25
LOW_PRIORITY = 19

# Outcome words (also paynotes').
TOO_MANY_PAGES = "too many pages"
PAGE_TOO_LARGE = "page too large to render"
NOT_A_DOCUMENT = "not a pdf or picture"
CONVERSION_FAILED = "conversion failed"
OCR_UNAVAILABLE = "ocr unavailable"
OCR_FAILED = "ocr failed"
PAGES_NOT_READ = "pages not read"
TIMED_OUT = "timed out"
STOPPED = "stopped"
READER_FAILED = "reader failed"


# --------------------------------------------------------------- the child


def read_pages(
    source: Path,
    store: Path,
    skip: set[int],
    seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """In the child: the per-page facts of `source`, leaving out `skip`.

    Returns {"kind", "pages_total", "pages": {number: {"how", "facts"?,
    "reason_class"?, "reason"?}}, "reason_class"?, "reason"?}. `how` is
    `text`, `ocr`, `ocr_empty` or `unread`."""
    from . import documents, invoicefacts, ocr

    deadline = clock() + max(1.0, seconds - ANSWER_MARGIN)
    with open(source, "rb") as fh:
        head = fh.read(64)
    if head.startswith(b"%PDF"):
        kind = "pdf"
    elif ocr.picture_format(head):
        kind = "picture"
    else:
        return {"kind": "other", "pages_total": 0, "pages": {},
                "reason_class": NOT_A_DOCUMENT,
                "reason": "neither a PDF nor a picture, by its bytes"}

    pages: dict[int, dict[str, Any]] = {}
    try:
        if kind == "pdf":
            import pymupdf

            with pymupdf.open(str(source)) as doc:
                total = int(doc.page_count)
                if total > MAX_PAGES:
                    return {"kind": kind, "pages_total": total, "pages": {},
                            "reason_class": TOO_MANY_PAGES,
                            "reason": f"{total} pages, above the {MAX_PAGES} read"}
                scale = ocr.OCR_DPI / 72
                wanted: list[int] = []
                for number in range(1, total + 1):
                    if number in skip:
                        continue
                    rect = doc[number - 1].rect
                    pixels = (rect.width * scale) * (rect.height * scale)
                    if pixels > MAX_RENDER_PIXELS:
                        pages[number] = {"how": "unread", "reason_class": PAGE_TOO_LARGE,
                                         "reason": f"{int(pixels)} pixels at {ocr.OCR_DPI} dpi"}
                    else:
                        wanted.append(number)
                if not wanted:
                    return {"kind": kind, "pages_total": total, "pages": pages}
                doc.select([n - 1 for n in wanted])
                subset = store / "subset.pdf"
                store.mkdir(parents=True, exist_ok=True)
                doc.save(str(subset))
            rendition = documents.convert(subset, store)
            read_from: Path = subset
            numbering = dict(enumerate(wanted, start=1))
        else:
            total = 1
            if 1 in skip:
                return {"kind": kind, "pages_total": total, "pages": {}}
            rendition = documents.convert_picture(source, store)
            read_from = source
            numbering = {1: 1}
    except documents.DocumentError as exc:
        return {"kind": kind, "pages_total": 0, "pages": {},
                "reason_class": CONVERSION_FAILED, "reason": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 — PyMuPDF's own errors on a broken file
        return {"kind": kind, "pages_total": 0, "pages": {},
                "reason_class": CONVERSION_FAILED, "reason": type(exc).__name__}

    # OCR in as many calls as the deadline allows, each call at most the
    # pipeline's own per-call cap; stop when a call makes no progress.
    while rendition.unread_scans and clock() < deadline:
        before = len(rendition.unread_scans)
        rendition = documents.recognise_scans(
            rendition, read_from, should_stop=lambda: clock() >= deadline,
            low_priority=True,
        )
        if len(rendition.unread_scans) >= before:
            break

    unavailable = ocr.unavailable_reason()
    for facts_page in rendition.pages:
        number = numbering[facts_page.number]
        if facts_page.is_scan and facts_page.ocr not in ("text", "none"):
            if unavailable:
                word, why = OCR_UNAVAILABLE, unavailable
            elif facts_page.ocr == "failed":
                word, why = OCR_FAILED, facts_page.ocr_error
            else:
                word, why = PAGES_NOT_READ, rendition.unread_reason or "the deadline came first"
            pages[number] = {"how": "unread", "reason_class": word, "reason": why[:300]}
            continue
        if facts_page.is_scan:
            how = "ocr" if facts_page.ocr == "text" else "ocr_empty"
        else:
            how = "text"
        text = documents.pages_text(rendition, [facts_page.number])
        pages[number] = {"how": how, "facts": invoicefacts.page_facts(text)}
    return {"kind": kind, "pages_total": total, "pages": pages}


def _child_main() -> None:
    request = json.loads(sys.stdin.read() or "{}")
    try:
        answer = read_pages(Path(request["source"]), Path(request["store"]),
                            {int(n) for n in request.get("skip") or []},
                            float(request.get("seconds") or 60))
    except Exception as exc:  # noqa: BLE001 — the TYPE only: a message can quote the file
        answer = {"kind": "", "pages_total": 0, "pages": {},
                  "reason_class": READER_FAILED, "reason": type(exc).__name__}
    from . import vocab

    # The keyword lists were consulted HERE; their tallies would die with
    # this process, so they travel back with the answer (counts, no words).
    answer["vocab"] = vocab.drain()
    sys.stdout.write(json.dumps(answer))
    sys.stdout.flush()


# -------------------------------------------------------------- the parent


def reader_command() -> list[str]:
    """The child's argv. A seam: tests substitute a stuck or crashing child."""
    return [sys.executable, "-P", "-m", "aish.invoicereader"]


#: The only variables the reader child gets: what Python, the temp and locale
#: machinery and the OCR switch need. Never the server's own environment,
#: which holds API keys, while a third party's PDF is being parsed.
#: `PYTHONPATH` only so a from-source run finds the package.
_CHILD_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "USER", "LOGNAME", "AISH_OCR",
              "PYTHONPATH")


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _CHILD_ENV or k.startswith("LC_")}


def _kill(child: subprocess.Popen) -> None:
    try:
        os.killpg(child.pid, signal.SIGKILL)  # the child and its OCR grandchild
    except OSError:
        child.kill()


def run_reader(
    source: Path,
    store: Path,
    skip: set[int],
    seconds: float,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run `read_pages` in a child at the lowest priority; kill it (and its
    OCR grandchild) at `seconds` or on `should_stop`. A killed or crashed
    child is an answer with no pages and an outcome word."""
    child = subprocess.Popen(
        reader_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        start_new_session=True, env=child_env(),
    )
    try:
        os.setpriority(os.PRIO_PROCESS, child.pid, LOW_PRIORITY)
    except OSError:
        pass
    payload: str | None = json.dumps({"source": str(source), "store": str(store),
                                      "skip": sorted(skip), "seconds": seconds})
    started = time.monotonic()
    ended = ""
    while True:
        try:
            out, _err = child.communicate(payload, timeout=_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            payload = None
            if should_stop is not None and should_stop():
                ended = STOPPED
            elif time.monotonic() - started > seconds:
                ended = TIMED_OUT
            else:
                continue
            _kill(child)
            child.communicate()
            break
    if ended:
        return {"kind": "", "pages_total": 0, "pages": {}, "reason_class": ended,
                "reason": f"the reader was {'stopped' if ended == STOPPED else 'killed'} after "
                          f"{round(time.monotonic() - started)} s"}
    try:
        answer = json.loads(out or "")
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("pages"), dict):
        return {"kind": "", "pages_total": 0, "pages": {}, "reason_class": READER_FAILED,
                "reason": f"the reader exited {child.returncode} with no answer"}
    answer["pages"] = {int(k): v for k, v in answer["pages"].items()}
    _replay_tallies(answer.pop("vocab", None))
    return answer


def _replay_tallies(tallies: Any) -> None:
    """The child's vocabulary consultations, counted in this process."""
    from . import vocab

    if not isinstance(tallies, dict):
        return
    for name, tally in tallies.items():
        if name not in vocab.CATALOGUE or not isinstance(tally, dict):
            continue
        asked, matched = int(tally.get("asked") or 0), int(tally.get("matched") or 0)
        for i in range(asked):
            vocab.note(name, matched=i < matched)


if __name__ == "__main__":
    _child_main()
