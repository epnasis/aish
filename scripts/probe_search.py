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
import urllib.parse
from pathlib import Path

MODE = (sys.argv[1] if len(sys.argv) > 1 else "cold").lower()
if MODE not in ("cold", "warm"):
    sys.exit("usage: probe_search.py [cold|warm]")
if MODE == "cold":
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


for engine, template in ENGINES.items():
    for query in QUERIES:
        probe(engine, template, query)

browser.shutdown()
