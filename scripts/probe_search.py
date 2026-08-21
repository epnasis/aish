#!/usr/bin/env -S uv run python
"""Measure what a REAL Chrome gets from a search engine's own results page.

The question this answers: when the free scraper behind `web_search` comes back
empty because it was stonewalled, can the browser aish already has read a
results page instead — and does it matter WHICH engine, and whether the profile
is signed in?

Two runs, because the profile is chosen by an environment variable read once per
process and the browser owner thread is a singleton:

    uv run python scripts/probe_search.py cold   # throwaway profile, signed in nowhere
    uv run python scripts/probe_search.py warm   # the REAL profile, signed in as aish

`warm` deliberately touches the owner's profile — that is the measurement. It
only READS public results pages; it signs into nothing and clicks nothing.

NOT part of the pytest suite: it launches Chrome, which conftest forbids.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

MODE = (sys.argv[1] if len(sys.argv) > 1 else "cold").lower()
if MODE not in ("cold", "warm", "compare"):
    sys.exit("usage: probe_search.py [cold|warm|compare]")
if MODE in ("cold", "compare"):
    os.environ["AISH_STATE_DIR"] = tempfile.mkdtemp(prefix="probe-search-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aish import browser, web  # noqa: E402

# The query from the session that filed this (2026-08-21): ten `web_search`
# calls, every one of them reported as a failure. Plus a plain-keyword control,
# so a zero here can be told apart from "this engine is having a bad day".
QUERIES = [
    'site:careers.google.com "product management" Poland',
    "google careers product manager warsaw",
]

ENGINES = {
    "google": "https://www.google.com/search?q={q}",
    "ddg": "https://duckduckgo.com/html/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
}

# Words a results page shows INSTEAD of results when it wants something from you
# first. Not the same thing as `is_challenge` — a cookie wall is not a bot wall,
# and the owner's claim under test is that being signed in removes it.
CONSENT = ("before you continue", "zanim przejdziesz", "accept all", "zaakceptuj wszystko",
           "i agree", "zgadzam się", "cookie", "consent")


def unwrap(href: str) -> str:
    """DuckDuckGo hands back its own redirector, never the result's URL."""
    parts = urllib.parse.urlsplit(href)
    if parts.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parts.query).get("uddg")
        if target:
            return target[0]
    return href


def candidates(page, engine_url: str) -> list[tuple[str, str]]:
    """Links that could be results: not the engine's own chrome, one per URL.

    Compared on the ENGINE'S OWN hostname, not on `host_of`'s registrable-ish
    form — the first run of this probe used the latter and scored a perfect
    Google `site:careers.google.com` page as zero results, because every result
    it was looking for was a google.com subdomain."""
    engine = (urllib.parse.urlsplit(engine_url).hostname or "").lower()
    bare = engine.removeprefix("www.")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, raw in page.links:
        href = unwrap(raw)
        host = (urllib.parse.urlsplit(href).hostname or "").lower()
        if not host or host in (engine, bare) or href in seen or not label.strip():
            continue
        seen.add(href)
        out.append((label.strip(), href))
    return out


def probe(engine: str, template: str, query: str) -> None:
    url = template.format(q=urllib.parse.quote_plus(query))
    print(f"\n--- {MODE}/{engine}: {query!r}")
    try:
        page = browser.read(url, timeout=60.0)
    except Exception as exc:  # noqa: BLE001 — a probe reports, never crashes
        print(f"    FAILED {type(exc).__name__}: {exc}")
        return
    text = page.text or ""
    low = text.lower()
    hits = candidates(page, url)
    print(f"    status={page.status} chars={len(text)} links={len(page.links)} "
          f"off-engine={len(hits)} signin={page.signin}")
    print(f"    challenge={browser.is_challenge(text, page.status)} "
          f"blank={web.is_blank(text)} "
          f"consent-words={[w for w in CONSENT if w in low][:4]}")
    print(f"    landed on {page.url}")
    for label, href in hits[:5]:
        print(f"      · {label[:70]}\n        {href[:110]}")
    if not hits:
        print(f"      text head: {' '.join(text.split())[:300]}")


# --------------------------------------------------------------- compare

# Whether Google should be the FIRST index rather than the fallback is a
# question about quality and about COST, and only one of those is visible from
# a results page. `compare` runs the same queries through both and times them,
# on a throwaway profile, so the trade is measured rather than argued.
COMPARE_QUERIES = [
    'site:careers.google.com "product management" Poland',
    "kurs USD NBP 14 sierpnia 2026",
    "playwright persistent context user_data_dir",
    "filmweb Krzyżacy 1960 obsada",
    "chusta kółkowa dla niemowlaka opinie",
    '"progressive disclosure" agent skills anthropic',
]


def ddgs_results(query: str) -> tuple[float, list[tuple[str, str]]]:
    from ddgs import DDGS

    start = time.monotonic()
    try:
        raw = DDGS().text(query, max_results=5)
    except Exception as exc:  # noqa: BLE001
        return time.monotonic() - start, [("!! " + type(exc).__name__ + ": " + str(exc), "")]
    return time.monotonic() - start, [
        (" ".join((r.get("title") or "").split()), r.get("href") or "") for r in raw
    ]


def google_results(query: str) -> tuple[float, list[tuple[str, str]]]:
    url = ENGINES["google"].format(q=urllib.parse.quote_plus(query))
    start = time.monotonic()
    try:
        page = browser.read(url, timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        return time.monotonic() - start, [("!! " + type(exc).__name__ + ": " + str(exc), "")]
    if browser.is_challenge(page.text or "", page.status):
        return time.monotonic() - start, [("!! WALL", page.url)]
    return time.monotonic() - start, candidates(page, url)[:6]


def compare() -> None:
    for query in COMPARE_QUERIES:
        print(f"\n=== {query!r}")
        for name, fn in (("ddgs", ddgs_results), ("google", google_results)):
            secs, rows = fn(query)
            print(f"  {name:<7} {secs:5.2f}s  {len(rows)} results")
            for label, href in rows[:5]:
                print(f"      · {label[:66]}")
                print(f"        {href[:100]}")


if MODE == "compare":
    compare()
else:
    for engine, template in ENGINES.items():
        for query in QUERIES:
            probe(engine, template, query)

browser.shutdown()
