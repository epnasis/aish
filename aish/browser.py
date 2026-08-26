"""The browser aish reads with when a fetch is not enough.

`web.read_url` fetches with urllib: fast, cheap, anonymous, and right for most
of the web. It cannot read two kinds of page at all — one rendered entirely by
JavaScript (the fetch returns an empty shell) and one behind a login (the fetch
is a different, logged-out client). This module is the escalation for both: a
REAL Chrome, driven off-screen, with a profile that persists.

**Persistence is the point, not an optimisation.** The profile outlives the
process, so a session the owner established by hand — logging into a portal
once, at a window this module opened for them — is still there next week, and
every later read of that site is made as them. That is why the profile is a
fixed directory rather than a temp dir, and why nothing here ever clears it.

**Where the profile lives is a safety decision.** `~/.local/state/aish/`, NEVER
`~/.config/aish/`: the config tree is auto-committed and pushed to a private
GitHub repo by the knowledge-git agent, and this directory is made of live
session cookies. A profile under config would push the owner's logins to a git
remote on a timer.

**One thread owns the browser, and it runs an event loop.** Playwright binds
its objects to whatever created them, and `read_url` runs on a pool
(`_execute_tool_calls` fans read-only tools out concurrently), so a context
touched from a second thread errors out. Everything is therefore marshalled to
one long-lived owner thread — which also gives single-ownership of the profile
directory for free, since Chrome locks the user-data-dir and a second launch
against a live profile fails.

That thread owns an **asyncio loop** rather than a job queue, and the
difference is the concurrency aish has everywhere else. Read-only tools fan
out because they are network-bound; routed through a serial browser they took
the SUM of their times instead of the slowest (measured live: 7.0s, 11.5s and
14.3s for three pages in one turn — and the third was slow enough that a
half-painted page was mistaken for a block wall). More browsers is not the fix,
because Chrome locks the profile and the profile is the point: one set of the
owner's sessions, shared by every read. So it is one browser with many TABS.
Callers still block; the WORK overlaps.

The context is kept warm between reads (a launch costs ~2s) but closed after
`IDLE_SECONDS`, because this box runs a Home Assistant VM and Colima beside a
16 GB ceiling and an idle Chrome is not free.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from . import browse as browse_mod
from . import media, notify
from . import signin as signin_mod

# A launch is ~2s, so the context is kept warm; an idle Chrome is ~400 MB, so
# it does not stay warm for long. See the module docstring on the memory
# ceiling this box actually runs against.
IDLE_SECONDS = 180.0
NAV_TIMEOUT_MS = 45_000
# What an ELEMENT gets to become actionable, which is a different question
# from how long a page may take to load. One 45-second click was the whole
# of the act path, and a control that would never become clickable cost the
# lot — six times across two sessions (#244). What 5 seconds cannot do, 45
# will not do either; the escalation in `_press` is what covers the rest.
ACT_TIMEOUT_MS = 5_000
SETTLE_MS = 2_500
LOGIN_WINDOW_TIMEOUT = 15 * 60.0
# An open view suppresses the idle reaper, so it needs its own ceiling: a client
# that vanishes without closing (a phone backgrounding the PWA, a dropped
# socket) would otherwise hold Chrome and the profile lock forever. Generous,
# because a paused 2FA login is the case the reaper must not interrupt.
VIEW_MAX_IDLE = 15 * 60.0
# And an open browse session needs its own, for the same reason (#248). The
# reaper protected a live view and not the model's page, so three minutes of the
# owner READING an answer collected the browser, and his next message — "Provide
# pdf for each" — got "nothing is open to act on". Shorter than the view's
# ceiling because the wait is different: a paused login is a human at a keyboard
# part-way through a task, and this is a human reading a paragraph.
BROWSE_MAX_IDLE = 10 * 60.0

# How many chats may hold a browse tab at once (#272). Every chat gets its own
# page — that is the fix — but a chat costs one tap to start, so the count
# needs a ceiling that is not the number of chats the owner has ever opened.
# Six is judged against the same 16 GB roof as the idle timers: enough that the
# owner driving two or three flows in parallel never notices it, small enough
# that a forgotten tab cannot accumulate into a second Chrome's worth of
# memory. Over the line, the least recently touched page is closed and its chat
# is told `NOTHING_OPEN` — the answer the reaper has always given.
MAX_BROWSE_PAGES = 6

# Off the visible desktop. The window is REAL — that is the whole reason this
# works where headless does not — it simply is not parked where the owner is
# looking. A login window is launched on-screen instead (`_LOGIN_ARGS`).
# The last two are not cosmetic. macOS reports a fully off-screen window as
# OCCLUDED, and Chrome backgrounds an occluded window: rAF throttles to about a
# frame a second or stops. A menu that opens through a JS animation then freezes
# part-way — permanently half-open, permanently off-canvas — which is one of the
# ways a control can be listed and unpressable (#244).
_OFFSCREEN_ARGS = [
    "--window-position=-4000,-4000",
    "--window-size=1440,900",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]
_LOGIN_ARGS = ["--window-position=80,60", "--window-size=1280,900"]

# Chrome advertises itself as automated by default, and sites that block
# automation outright act on it: measured against allegro.pl, a cold profile's
# FIRST read returned 200 with real prices and every later read on that same
# profile returned 403 — no captcha ever offered, so there was nothing a human
# could have solved. Suppressing these two is what makes repeat reads work.
#
# This is anti-detection, it is likely contrary to those sites' terms, and the
# owner turned it on knowing that (asked and answered, 2026-08-14). It is one
# switch so the decision stays visible and reversible: AISH_BROWSER_STEALTH=0.
_STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
_STEALTH_OMIT = ["--enable-automation"]

# Clicked at most once per page, best-effort, failures ignored. An EU consent
# wall is not anti-bot — it is an overlay that buries the page's actual text
# under boilerplate, and on the listing this module was built for it cost about
# a third of the extracted characters. Deliberately a short list of
# unambiguous accept controls: a wrong click here acts as the owner.
#
# **A FLOOR, NEVER THE MECHANISM** (#295 P4, #321). This is a word list, and a
# word list fails by missing: it held `Akceptuj wszystkie` while eon.pl's
# button says *Akceptuję wszystkie cookies*, and `has-text` is a substring
# match — so it missed by the `ę`, the banner stayed over the login button, and
# a site the owner uses weekly quietly stopped working with no signal anywhere.
# What generalises is the structural half — `_COVERED_JS` naming whatever is on
# top of the control, in any language and on any site — and this list is what
# sits UNDER it: still run speculatively when a page opens (cheap, and it keeps
# a banner out of the extracted text), but no longer the only thing that can
# notice one. Its HIT RATE is counted where the structural check has already
# found an obstruction (`browse.CONSENT_TALLY`), so a list that has stopped
# matching is a number on `/browser` rather than silence.
#
# Growing it is deliberately conservative, and dismissing more aggressively is
# NOT the fix here: taking a banner down is a click on a page, and "accept all
# cookies" is a consequence for the owner. Report first; dismiss only where it
# is already dismissing today.
_CONSENT_SELECTORS = (
    "[data-role='accept-consent']",
    "button#onetrust-accept-btn-handler",
    "button[aria-label='Accept all']",
    "button:has-text('Akceptuj wszystkie')",
    "button:has-text('Zaakceptuj wszystkie')",
    "button:has-text('Accept all')",
)


# A profile Chrome died inside keeps these; every later launch then fails.
_LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")
_LOCK_MARKERS = ("singletonlock", "processsingleton", "profile appears to be in use")


def _clear_stale_lock(exc: BaseException) -> bool:
    """Remove a dead Chrome's profile lock. True when something was cleared.

    Deliberately narrow: only for errors that NAME a lock, and only the lock
    files — never the profile, which is the owner's logins."""
    if not any(m in str(exc).lower() for m in _LOCK_MARKERS):
        return False
    cleared = False
    for name in _LOCK_FILES:
        path = profile_dir() / name
        try:
            path.unlink()
            cleared = True
        except OSError:
            continue
    return cleared


# A block page HAS text, which is exactly why "judge it on text" is not enough
# on its own. The original session drowned in these: Jina returned
# "This page maybe requiring CAPTCHA" and the model read it as the page. Letting
# one through here would re-introduce that failure one layer up — the model
# would report a challenge screen's contents as the shop's, and invent from it.
CHALLENGE_MAX_CHARS = 3000
_CHALLENGE_MARKERS = (
    "verify you are human",
    "are you a human",
    "checking your browser",
    "enable javascript and cookies",
    "captcha",
    "datadome",
    "cf-browser-verification",
    "access to this page has been denied",
    "zweryfikuj, że jesteś człowiekiem",
    "potwierdź, że nie jesteś robotem",
    # The two SEARCH ENGINES word it their own way, and neither says captcha.
    # Measured 2026-08-21 while probing whether the browser could read a
    # results page: bing.com served 105 characters — "One last step. Please
    # solve the challenge below to continue" — with a 200, and `is_challenge`
    # called it CONTENT. Google's /sorry page is the same shape, and it is the
    # one that matters: the day it decides against us, a search fallback would
    # hand the model a wall as if it were the results.
    "solve the challenge",
    "unusual traffic from your computer network",
    "nietypowy ruch z twojej sieci",
)
# The statuses that mean "refused", not "not here" — the ONE authority on that
# question, shared with `web.read_url`, whose escalation to this browser fires
# on exactly this set. It was two lists for a while, and they drifted: this one
# had 401, the fetch side had only 403/429/503, and ticketmaster.pl's event
# pages answer a plain fetch with `401 {"response":"identify"}` — a bot wall
# wearing an auth code. So the fetch failed in 0.2s, the renderer never ran, and
# eight event pages came back as ERROR while the very same browser rendered them
# in full when the owner pasted one in by hand (#257). A status is a wall or it
# is not; there is no version of that fact that differs by which half of the
# read path is asking. 404 and friends stay out on purpose — nothing is there
# to render, and a Chrome launch to prove it costs seconds.
BLOCK_STATUS = (401, 403, 405, 429, 503)


def is_challenge(text: str, status: int | None) -> bool:
    """Does this look like a wall rather than the page?

    Conservative BY LENGTH first: a real listing runs to tens of thousands of
    characters, so anything long is content whatever its status code (the
    measured allegro.pl case is 403 + 23 000 chars of real prices, and must
    stay a success). Only a SHORT body is then judged on its status or its
    wording."""
    body = (text or "").strip()
    if len(body) >= CHALLENGE_MAX_CHARS:
        return False
    lowered = body.lower()
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return True
    return status in BLOCK_STATUS


# A page that wants a PASSWORD is asking who you are, which means what came back
# is not the account. This is the login-wall twin of `is_challenge`, and it
# exists because a login wall is indistinguishable from a good read by every
# signal `read_url` had: HTTP 200, a full page of text, no error at all. On
# 2026-08-18 the owner approved a card saying his E.ON read would carry his
# session, was handed the logged-out login form, and the model concluded the
# portal was inaccessible and asked him to upload the invoices by hand (#236).
#
# The evidence is the FIELD, never the wording. "Zaloguj się" / "Sign in" sits
# in the navigation of half the logged-in web, so matching words would flag real
# account pages — and would then need maintaining per language, on a corpus
# where the owner's sites are Polish. A password input is the same markup in
# every language, and it appears where a site is actually asking.
_PASSWORD_INPUT = re.compile(r"""<input\b[^>]*\btype\s*=\s*["']?password""", re.I)


def asks_to_sign_in(html: str) -> bool:
    """Does this HTML put a password field in front of the reader?

    The fetch path's version of the question `Page.signin` answers from the live
    DOM. Weaker on purpose, and knowingly: an SPA that builds its form in
    JavaScript serves HTML this cannot see. But a fetch is precisely where a
    server-rendered login wall arrives fully formed in the markup, which is the
    case a plain fetcher meets and the case this has to catch."""
    return bool(_PASSWORD_INPUT.search(html or ""))


# Anti-bot REPUTATION cookies. These are not the site's session and never the
# owner's login — they are the scoring token a bot-management vendor issues,
# and once it has decided against you it keeps deciding against you: measured
# on allegro.pl, a page returning 7 833 characters on a cold profile returned
# ZERO on a warm one, and dropping `datadome` alone took it straight back to
# 7 874. The score is the block, so the score is what gets discarded.
#
# NEVER widen this to "clear the cookies for this host". The same jar holds the
# sessions the owner signed in for by hand, which is the entire reason the
# profile persists; clearing those to fix a scrape would trade the feature for
# the workaround. Deletion is BY NAME, one cookie at a time.
#
# `cf_clearance` is deliberately absent: it is a PASS token, evidence a
# challenge was already solved. Dropping it would throw away a good thing.
_REPUTATION_COOKIES = (
    "datadome",        # DataDome — what allegro.pl uses
    "__cf_bm",         # Cloudflare bot management (not cf_clearance)
    "_px", "_pxhd", "_pxvid",   # PerimeterX
    "ak_bmsc", "bm_sz", "bm_sv",  # Akamai Bot Manager
)


async def _shed_reputation(context: Any, url: str) -> bool:
    """Drop this host's bot-scoring cookies. True if anything went.

    Targeted deletion by name — never `clear_cookies()`, which would take the
    owner's logins with it."""
    host = host_of(url)
    if not host:
        return False
    shed = False
    try:
        present = {c.get("name") for c in await context.cookies()}
    except Exception:  # noqa: BLE001 — no jar, nothing to shed
        return False
    for name in _REPUTATION_COOKIES:
        if name not in present:
            continue
        for domain in (host, f".{host}"):
            try:
                await context.clear_cookies(name=name, domain=domain)
                shed = True
            except Exception:  # noqa: BLE001 — best effort, per name
                continue
    return shed


class BrowserUnavailable(RuntimeError):
    """Playwright or Chrome is not installed — the caller falls back."""


@dataclass
class Page:
    """What a browser read hands back: the RENDERED page, already read.

    Text is taken from the live DOM rather than from serialized HTML, and that
    is not a shortcut — it is the only thing that works. `page.content()`
    serializes the light DOM only, so a site that renders into shadow DOM
    hands back a document with none of its content in it: measured on an
    allegro.pl listing, 362 KB of HTML containing the word "zł" exactly zero
    times, while the rendered text held 23 000 characters and every price. The
    HTML parser cannot recover what the serialization left out, so the browser
    reads what a person would see and hands over that.

    `status` is diagnostic ONLY. A site that dislikes automation may answer 403
    and still serve the whole listing (measured on the same page). Callers
    judge a read by whether it produced TEXT, never by the code.

    `links` is (first line of the anchor's text, absolute href) in DOM order.
    Text alone is not the page on a SHOP: a listing's whole point is which
    offer and at what URL, and rendering it to plain text threw the URL away —
    see `_LINKS_JS`.

    `declared` is the page's own schema.org JSON-LD, raw. It is the half of the
    page written for indexers rather than for people, so `inner_text` cannot
    see it — and it is the only statement of what a page is ABOUT that does not
    have to be inferred from where things sit on it."""

    text: str
    title: str
    images: list[str]
    url: str
    status: int | None
    links: list[tuple[str, str]] = field(default_factory=list)
    declared: list[str] = field(default_factory=list)
    # The page asked for a password. Read off the DOM rather than guessed at
    # from the text, and the caller's only way to tell a signed-in read that
    # WORKED from one whose session has lapsed — the two are identical
    # otherwise, right down to the status code.
    signin: bool = False
    # Files this read produced, as local paths. A URL whose response is a
    # DOCUMENT rather than a page renders as nothing at all, and aish used to
    # conclude "the browser rendered an empty page", refetch anonymously, and
    # tell the owner it could not get his invoices — while holding all seven of
    # them (#246). A navigation that becomes a download is a read that
    # succeeded, with the answer in a file.
    downloads: list[str] = field(default_factory=list)


_OWNER: threading.Thread | None = None
_OWNER_LOCK = threading.Lock()


def state_dir() -> Path:
    return Path(
        os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish"))
    )


def frames_dir() -> Path:
    """Where an evidence frame's bytes land (#289) — its OWN store (#318).

    A store of its own, outside every workspace root, and that location is a
    security property rather than housekeeping. Frames used to go into the
    media store, which is inside `Agent.workspace_roots`, and that boundary's
    whole justification is that reading back what the process already wrote
    UNPROMPTED grants nothing new. Everything else in that store is a picture
    the model asked for; a frame is a picture of a page from outside, written
    unprompted. So `show_image(source=<a frame>)` read the file back through
    `_read_local_image`, `media.store` re-adopted it, and the bytes rode the
    result envelope into the conversation as native image content —
    bannerless, unattributed, and untainted, since a local path carries no
    host for `_brings_outside_content` to see. The store also outlives the
    task, so a page driven in one chat was reachable from the next.

    Moving the bytes out is the whole fix, and it is the same shape #317 used
    for the tool-output cache: the boundary does not grow an exception and the
    file layer does not learn a second provenance path. What a frame needs
    from a store — raw bytes, content addressing, a bounded LRU — is
    `media.py`'s mechanics, which this keeps; what it must not inherit is the
    media store's ADDRESS. (The evidence store next door still cannot hold it:
    that one is text-only by construction, addressed by the sha256 of UTF-8
    and read with `read_text`.)

    Being outside the roots means the web UI cannot serve a frame through
    `/file`, so it has an authorised route of its own (`WebServer.handle_frame`
    → `/frame`). A frame is a RECORD and a record the owner cannot see is not
    one; what changed here is what the MODEL may read, never what he may.

    Resolved here rather than taken from the Agent because the capture happens
    on the browser's own thread, several layers below any agent. Both sides
    read `AISH_STATE_DIR`, and `aish-web` exports it at startup precisely so
    every module that resolves it itself resolves it the same — which is why
    `Agent._is_evidence_frame` asks THIS function rather than deriving a
    second answer from its own state dir.

    Its caps are its own too (`media.FRAME_MAX_*`), which settles the question
    the shared store left open: a frame can no longer evict a picture from one
    of the owner's chats. `docs/browser.md`.
    """
    return state_dir() / "frames"


def downloads_dir() -> Path:
    """Where a file the model downloaded lands.

    Beside the profile, under the state dir, and NOT in the config tree for the
    same reason the profile is not: that tree is auto-committed and pushed. It is
    listed in `Agent.workspace_roots` so `read_pdf` can open what `browse_act`
    just named — otherwise the tool tells the model to read a file the model is
    not allowed to read, which is the #220 asymmetry all over again."""
    return state_dir() / "browser" / "downloads"


def profile_dir() -> Path:
    return state_dir() / "browser" / "profile"


def search_profile_dir() -> Path:
    """Where the SEARCH browser lives — beside the owner's profile, never it.

    A separate directory is the whole mechanism behind reading a results page
    with no gate and no card. `_login_gate` asks one question — does this read
    carry the owner's session? — and a profile that has never signed into
    anything answers it `no` by construction, which is a stronger answer than
    any allowlist could give: an exemption is a claim about a URL, checked
    before navigation and false the moment Google 302s to `accounts.google.com`,
    whereas an empty cookie jar stays empty down every redirect.

    It is ONE extra profile and it stays one. Chrome is not free on a box also
    running a Home Assistant VM and Colima under 16 GB, and "a profile per
    situation" is the fingerprint-rotation arms race `docs/browser.md` already
    refuses. Two identities, each with a reason to exist: the owner's, and
    nobody's."""
    return state_dir() / "browser" / "search-profile"


def seen_file() -> Path:
    """Where the observed-sign-in hint lives. A new NAME on purpose: the file
    it replaces held assertions, this one holds observations, and reusing the
    name would have let the old meaning survive the change."""
    return state_dir() / "browser" / "signed-in-seen.txt"


def search_logins_file() -> Path:
    """What the SEARCH profile is signed into — a separate file, deliberately.

    `logins.txt` is not a note, it is what `is_logged_in` answers from and
    therefore what makes `_login_gate` fire on a `read_url`. The search profile
    is never used by `read_url`, so a sign-in there must not be able to change
    what the owner's reads do — and equally, his sign-ins must not make the
    search browser look signed in when it is not. Two profiles, two records,
    and this one is READ FOR DISPLAY ONLY: nothing gates on it."""
    return state_dir() / "browser" / "search-logins.txt"


# Where the view has BEEN, so reopening something is one tap rather than
# retyping an address. Server-side, in the state dir, deliberately: this is a
# fact about the BROWSER — one Chrome, on one Mac — not about the screen it is
# being driven from, so it must read the same from his phone and his laptop.
# The localStorage version it replaces was per-device by accident.
RECENT_MAX = 20


def recent_file() -> Path:
    return state_dir() / "browser" / "recent.json"


def recent_pages() -> list[dict]:
    try:
        loaded = json.loads(recent_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [row for row in loaded if isinstance(row, dict) and row.get("url")][:RECENT_MAX]


def remember_page(url: str, title: str, *, cold: bool = False) -> None:
    """Record a page the view landed on. Never raises — this is bookkeeping
    alongside a screenshot, and a broken file must not cost the frame.

    ONE ENTRY PER HOST, newest first, which is the owner's own framing: ten
    Google pages in a row are ten rows of the same site and push everything
    else off a list whose whole job is breadth. What he wants back is the
    places he has been, not the steps he took through them."""
    host = host_of(url)
    if not host or not url.startswith(("http://", "https://")):
        return
    row = {
        "url": url,
        "title": " ".join((title or "").split())[:120],
        "host": host,
        "profile": "search" if cold else "",
    }
    kept = [r for r in recent_pages() if r.get("host") != host]
    try:
        path = recent_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([row] + kept[: RECENT_MAX - 1], ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def forget_page(host: str) -> list[dict]:
    kept = [r for r in recent_pages() if r.get("host") != host_of("https://" + host)]
    try:
        recent_file().write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return kept


def search_logged_in_hosts() -> set[str]:
    try:
        raw = search_logins_file().read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in raw.splitlines() if line.strip()}


def enabled() -> bool:
    return os.environ.get("AISH_BROWSER", "1") not in ("0", "false", "no")


def stealth() -> bool:
    return os.environ.get("AISH_BROWSER_STEALTH", "1") not in ("0", "false", "no")


def is_preview() -> bool:
    return os.environ.get("AISH_PREVIEW", "") not in ("", "0", "false", "no")


def unavailable_reason() -> str:
    """"" when a browser read can be attempted, else why it cannot."""
    if not enabled():
        return "the browser reader is switched off (AISH_BROWSER=0)"
    if is_preview():
        # `scripts/aish-preview.sh` points preview at PROD's AISH_STATE_DIR on
        # purpose, so preview would share this profile — the owner's LIVE
        # signed-in sessions. Preview is exactly where half-finished branches
        # and experimental content get exercised, which is the last place that
        # should be able to act as them, or to edit logins.txt and change what
        # production gates. The profile is not branch-safe, so preview does not
        # get one.
        return (
            "the browser is disabled on preview — it would share production's "
            "profile, and with it your live signed-in sessions"
        )
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "Playwright is not installed (uv sync)"
    return ""


# ------------------------------------------------------------------ logins

# Where a bare phrase goes. Owned HERE rather than in `web.py` because this is
# what an address bar does, and `web` already imports this module — the other
# direction would be a cycle. `web.SEARCH_ENGINE_URL` is this constant.
SEARCH_URL = "https://www.google.com/search?q={q}"

# A thing that could be a host: labels, an optional port, an optional path. The
# last label must START WITH A LETTER, which is what keeps `3.14` and `1.5` out
# — Chrome searches for those, and so does this.
_LOOKS_LIKE_HOST = re.compile(
    r"^[\w-]+(\.[\w-]+)*\.[A-Za-z][\w-]*(:\d+)?([/?#].*)?$"
)
_IS_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?([/?#].*)?$")
_IS_LOCAL = re.compile(r"^localhost(:\d+)?([/?#].*)?$", re.I)


def as_address(text: str) -> str:
    """What the owner typed, as the thing to navigate to — address OR search.

    The address bar in every browser he uses takes both, and this one took only
    addresses: typing `krzyżacy 1960 obsada` became `https://krzyżacy 1960
    obsada`, which Chrome refuses, so the view came back blank. Searching from
    aish's browser meant knowing to type the whole google.com/search URL by
    hand.

    Deciding between them is a HEURISTIC and there is no version that is not.
    The rules are Chrome's, in the order that matters: an explicit http(s)
    scheme is an address and nothing else is inspected; anything with
    whitespace in it is a search, because no address has a space; a bare
    `localhost`, an IPv4 address, or something shaped like a dotted host with a
    port or path is an address; everything else is a search. That last default
    is the one that matters — a single unrecognised word is far more often
    something he wants to look up than a hostname he wants to visit."""
    text = (text or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    if not text.split() or len(text.split()) > 1:
        return SEARCH_URL.format(q=urllib.parse.quote_plus(text))
    if _IS_LOCAL.match(text) or _IS_IPV4.match(text):
        # http, not https: a box on the LAN — his Home Assistant, aish's own
        # web app — is almost never served over TLS, and https there fails
        # rather than falling back.
        return "http://" + text
    if _LOOKS_LIKE_HOST.match(text):
        return "https://" + text
    return SEARCH_URL.format(q=urllib.parse.quote_plus(text))


def host_of(url: str) -> str:
    """Bare hostname, `www.` stripped, lowercased. "" when unparseable."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def seen_signed_in() -> set[str]:
    """Hosts aish has WATCHED a sign-in succeed at.

    Not a claim anybody makes — a record of something observed, and the
    difference is the whole point. Its predecessor, `logins.txt`, was a list
    the owner asserted, and it was wrong in both directions at once: it held
    `netflix.com`, `airbnb.com` and a typo'd `imbd.com` he had merely browsed
    past, each costing a Chrome launch and an approval card on every later
    read, while sites he really was signed into were missing — so a read of
    one fetched the logged-out page and handed it back as his account.

    Three separate incidents are recorded above of that list being written
    wrongly (on visit, on close, and as a whole browsing history under one
    batch yes), and they share one cause: **a human fact was being guessed at
    by a heuristic, then stored as though it had been established.** So the
    store stays and the WRITER changes. Every entry here traces to an event
    aish saw happen: a rendered page that stopped asking for a password, a
    credential replay that worked, a sign-in typed in the remote view.

    It is a HINT and never an authority. Nothing gates on it and no claim to
    the owner rests on it; a wrong entry costs one wasted Chrome launch, never
    a false statement about his accounts. The live page is the truth."""
    try:
        raw = seen_file().read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in raw.splitlines() if line.strip()}


def note_signed_in(url: str) -> None:
    """Remember that a page at this host came back SIGNED IN.

    The only writer. Called from the read path when a render produced a page
    that is not asking for a password, and from the view when a sign-in was
    watched happening."""
    host = host_of(url)
    if not host:
        return
    known = seen_signed_in()
    if host in known:
        return
    try:
        path = seen_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(sorted(known | {host})) + "\n", encoding="utf-8")
    except OSError:  # bookkeeping — a read must not fail because a hint did not save
        pass


def note_signed_out(url: str) -> None:
    """Drop a host that has just proved it is NOT signed in.

    The hint demotes itself, which is what keeps it from rotting into the thing
    it replaced. A page that came back asking for a password is proof."""
    host = host_of(url)
    known = seen_signed_in()
    if not host or host not in known:
        return
    try:
        seen_file().write_text(
            "\n".join(sorted(known - {host})) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _remember_logins(hosts: set[str], *, cold: bool = False) -> None:
    if not hosts:
        return
    path = search_logins_file() if cold else seen_file()
    known = search_logged_in_hosts() if cold else seen_signed_in()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(known | hosts)) + "\n", encoding="utf-8")


def forget_login(host: str) -> bool:
    """Forget what aish has observed about a site.

    Drops the observed-sign-in hint and stops routing the host through his
    profile (#283 — forgetting used to remove the approval card and leave the
    capability, which is exactly backwards).

    It does NOT clear cookies: the session is his to end, at the site. Nor does
    it stop aish noticing next time. That is the point of a hint rather than a
    record — if he is still signed in, the very next read will see it and the
    entry comes back, because the page is the truth and this file is only a
    memory of what the page last said."""
    host = host_of("https://" + host) or host.strip().lower()
    known = seen_signed_in()
    forgot = host in known
    if forgot:
        seen_file().write_text("\n".join(sorted(known - {host})) + "\n", encoding="utf-8")
    # Per-process: the running server's routing table, not a file.
    if host in BROWSER_HOSTS:
        BROWSER_HOSTS.discard(host)
        forgot = True
    return forgot


# ------------------------------------------------------- the owner thread

async def _launch(
    playwright: Any,
    *,
    args: list[str],
    profile: Path | None = None,
    viewport: dict | None = None,
    device_scale_factor: float | None = None,
) -> Any:
    profile = profile or profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    launch_args = list(args)
    omit: list[str] = []
    if stealth():
        launch_args += _STEALTH_ARGS
        omit += _STEALTH_OMIT
    return await playwright.chromium.launch_persistent_context(
        str(profile),
        channel="chrome",  # the real Chrome already on this Mac, not a bundled build
        headless=False,  # headless is what these sites actually block
        args=launch_args,
        ignore_default_args=omit,
        viewport=viewport,  # None = the real window size, for reads
        device_scale_factor=device_scale_factor,
        # TRUE since #237: the document at the end of a signed-in flow is
        # frequently the whole point ("Pobierz e-fakturę"), and the anonymous
        # opener behind read_pdf could never fetch one. A download with no
        # handler is written to a temp dir Playwright discards with the context,
        # so this costs a read path nothing.
        accept_downloads=True,
    )


# The image a page DECLARES as its subject, absolutised in the page itself so
# a site-relative og:image arrives usable. Mirrors web._IMAGE_META: show_image
# needs a real URL or the model starts inventing them (docs/agent-core.md).
_IMAGES_JS = """() => {
  const keys = ['og:image', 'og:image:url', 'og:image:secure_url', 'twitter:image'];
  const out = [];
  for (const m of document.querySelectorAll('meta')) {
    const key = (m.getAttribute('property') || m.getAttribute('name') || '').toLowerCase();
    const content = (m.getAttribute('content') || '').trim();
    if (!keys.includes(key) || !content) continue;
    try {
      const full = new URL(content, location.href).href;
      if (full.startsWith('http') && !out.includes(full)) out.push(full);
    } catch (e) { /* an unparseable content attribute is simply not an image */ }
  }
  return out;
}"""


# How long to keep waiting for a page to STOP changing before capturing it.
# The owner proved this one with paired screenshots: a partially-rendered page,
# then the finished page — with no navigation between them, only another frame.
# A fixed sleep cannot work, because "loaded" is not a duration: it is a
# property of the page, and a login step that swaps its whole panel takes as
# long as it takes.
SETTLE_MAX_MS = 6000
SETTLE_QUIET_MS = 350
# What a FIRST frame waits. Long enough to skip the flash of an empty document,
# short enough that the owner sees something happen: waiting the full settle
# before showing anything read as "nothing is happening", and a picture that
# arrives late is worth less than a picture that arrives now and is corrected.
FIRST_FRAME_MS = 450

# --------------------------------------------- watching a page that is not done
#
# `_settle` asks "has the DOM stopped changing?" as a stand-in for "is the page
# finished?", and a SPINNER is exactly where those two part company: the page is
# stating that it is unfinished, while its DOM sits perfectly still and the
# animation runs in CSS. The strongest evidence a page is NOT ready reads to a
# quiescence test as the strongest evidence that it is. One correction was then
# spent on the spinner and nobody looked again, so the owner tapped the page to
# force another frame — which is the whole reason this exists.
#
# The fix is not a better guess at when a page is done. It is to stop guessing
# and keep LOOKING, cheaply, for a bounded while. So the two halves that used to
# be fused into one expensive operation are split:
#
#   the probe    ~5 ms, nothing over the wire   "did anything actually happen?"
#   the capture  ~200 ms, ~40 KB                the picture itself
#
# The probe reports ACTIVITY rather than a verdict; `watch_step` turns activity
# into a decision. Nothing here has to recognise a spinner, which is the part no
# heuristic does reliably — aish's text-based `browse.still_loading` catches one
# with a label and misses a bare CSS donut, which is most of them.
#
# Installed lazily BY the probe rather than through `add_init_script`, so it
# self-heals across a navigation (a new document has no `window.__aish_watch`)
# and works on a view that was already open when this shipped.
_WATCH_JS = """() => {
  let w = window.__aish_watch;
  if (!w) {
    w = window.__aish_watch = { gen: 0, at: Date.now() };
    const bump = () => { w.gen++; w.at = Date.now(); };
    // The same three signals `_settle` calls movement, so "quiet" means one
    // thing in this file and not two.
    new MutationObserver(bump).observe(document.documentElement,
      { childList: true, subtree: true, characterData: true });
    // And the one `_settle` cannot have, because it only ever looked once: a
    // RESPONSE ARRIVING. This is the edge a spinner turns off on, and it is the
    // only signal for a lazily loaded image, which changes an existing `src`
    // rather than adding any node at all — the scroll half of the same bug.
    try {
      new PerformanceObserver(bump).observe({ type: 'resource', buffered: false });
    } catch (e) { /* an engine without it simply watches the DOM alone */ }
  }
  return { gen: w.gen, quiet: Date.now() - w.at,
           ready: document.readyState === 'complete' };
}"""

# The bounds. A page that never goes quiet must cost a KNOWN amount.
WATCH_POLL_MS = 350       # how often to ask; the ask is ~5 ms of work
WATCH_QUIET_MS = 350      # the same stillness `_settle` calls settled
WATCH_MIN_GAP_MS = 1_000  # never capture faster than this
WATCH_MAX_MS = 15_000     # a load slower than this is not an interaction any more
# How long a page must be CONTINUOUSLY still before the watcher believes nothing
# else is coming. Letting go the moment a page is quiet was the first version of
# this and it reintroduced the exact bug: a spinner's contents landed three
# seconds after the frame, by which time the watcher had already quit at its
# first quiet poll. Verified against real Chrome — the whole premise here is
# that arrival is LATE, so stillness now says nothing about stillness later.
# Every arrival resets it, so a page that loads in stages is followed to the
# ceiling; a page that is genuinely done costs a dozen probes and no bytes.
WATCH_SETTLED_MS = 5_000
# What bounds an animated page (#223): a carousel or a ticker mutates forever, so
# the cap — on CAPTURES, not on sends, or identical bytes would loop for free —
# is the difference between a few extra frames and an open-ended stream.
WATCH_MAX_CAPTURES = 3


def page_is_done(
    *, quiet_ms: float, ready: bool, still_for: float = WATCH_SETTLED_MS
) -> bool:
    """Has this page finished changing?

    ONE definition, for both things that need to know (#251). The owner's
    screen and the model's read ask exactly this question and would drift apart
    the moment each answered it for itself — the view would call a page
    finished while a read was still waiting on it, and only one of them could
    be right.

    What differs between them is not the question but the BAR: how long a page
    must have been continuously still before stillness is believed. The view
    can afford a high one, because waiting costs it nothing but polls, and it
    already has a picture on screen. An ordinary read cannot — most pages it
    reads finished long ago, and paying seconds on each of them would be paid
    on every read of the day. So the bar is the parameter, and the rule is not.

    `ready` is load-bearing and not decoration: a document still parsing is not
    a finished page however still it looks."""
    return bool(ready and quiet_ms >= still_for)


def watch_step(
    *,
    moved: bool,
    quiet_ms: float,
    ready: bool,
    elapsed_ms: float,
    since_capture_ms: float,
    captured: int,
    unknown: bool = False,
) -> str:
    """Should the watcher capture again, keep waiting, or let go?

    `"wait"`, `"capture"`, `"last"` (capture, then stop) or `"stop"`. Pure, so
    the whole policy is testable with no browser and no clock — which matters
    more here than usual, because every case it decides is a case that only
    shows up on a real site at a real speed.

    `moved` is relative to the frame the owner is LOOKING AT, not to the last
    poll: that is the only comparison that answers "is the picture on their
    screen still true?", and it is why a frame carries the generation it was
    captured at."""
    if captured >= WATCH_MAX_CAPTURES:
        return "stop"
    if elapsed_ms >= WATCH_MAX_MS:
        # Out of time. One last look if the page is still moving, so what stays
        # on screen is the freshest thing there was.
        if (moved or unknown) and since_capture_ms >= WATCH_MIN_GAP_MS:
            return "last"
        return "stop"
    if unknown:
        # The page would not answer — mid-navigation, or an error document with
        # no scripting. "Cannot tell" must never be read as "nothing happened",
        # so fall back to what the code did before there was a probe: capture.
        return "capture" if since_capture_ms >= WATCH_MIN_GAP_MS else "wait"
    if moved:
        if since_capture_ms < WATCH_MIN_GAP_MS:
            return "wait"
        # Capture once it holds still, so the frame is not caught mid-paint.
        return "capture" if quiet_ms >= WATCH_QUIET_MS else "wait"
    # Nothing has changed since the picture they are looking at. Letting go asks
    # a HARDER question than "is it quiet?" — see WATCH_SETTLED_MS.
    return "stop" if page_is_done(quiet_ms=quiet_ms, ready=ready) else "wait"


def already_finished(*, activity: dict | None, requests_in_flight: int) -> bool:
    """Was the page ALREADY finished at the instant it was photographed? (#223)

    An interaction ships a fast frame and then leaves a watcher looking for
    late arrivals. On a page that was already inert when the shutter fell, that
    watcher has nothing to find — so this is the question that lets the caller
    not start one.

    **What it claims, exactly.** At the moment of capture: the page answered
    the probe, the document was `complete`, it had been continuously still for
    at least `WATCH_SETTLED_MS` — the same window the watcher itself requires
    before it lets go — and no request was in flight. That is an observation,
    not a prediction. A `setTimeout` can still repaint afterwards and this will
    have said `True`; on that same evidence the watcher's own first poll
    returns `"stop"`, so it is a case this code never covered rather than one
    it stops covering. What it genuinely gives up is narrower: a repaint that
    lands in the gap before that first poll, driven by a timer rather than by a
    response — the ordinary version of which is ruled out below.

    So the bar is deliberately the WATCHER'S OWN, and never lower. Believing a
    momentarily quiet page is exactly the bug that shipped once and was caught
    only against real Chrome (see `WATCH_SETTLED_MS`), and skipping the watcher
    on that evidence would rebuild it one layer up.

    **The in-flight count is the half that makes the rest safe**, and it is the
    signal the issue named. Stillness is measured from the page's last
    mutation, which may predate the interaction entirely — a click that fires a
    request and mutates nothing yet reads as quiet-for-ten-seconds. A request
    on the wire is the evidence that something is still coming, and it is read
    from OUTSIDE the page (see `_Owner.view_requests`).

    Wrong in the only direction it can afford to be: anything unknown, unquiet
    or unfinished answers `False`, and `False` is the behaviour that shipped."""
    if activity is None:  # the page would not answer — never read as "nothing"
        return False
    if requests_in_flight > 0:
        return False
    return page_is_done(
        quiet_ms=float(activity.get("quiet") or 0),
        ready=bool(activity.get("ready")),
    )


# How many unanswered probes a read waits through before it stops asking. The
# view reads silence as "capture anyway, never miss a frame"; a READ has the
# opposite fallback, because the page that will not run `_WATCH_JS` is the page
# with no scripting — server-rendered, already whole, and never about to spin.
# A couple of polls covers the case that is really a page mid-navigation.
SETTLE_UNKNOWN_TRIES = 3


async def _settle(
    page: Any,
    *,
    still_for: float = SETTLE_QUIET_MS,
    timeout_ms: float = SETTLE_MAX_MS,
) -> None:
    """Wait until the page stops changing, or `timeout_ms`, whichever first.

    Network idle first, because it is the cheapest signal and settles the
    common case on its own. Then the SAME probe the owner's screen watches
    through (`_activity`), polled, and the same `page_is_done` — a read used to
    answer this question for itself with a one-shot MutationObserver, which is
    two definitions of "finished" in one file and only one of them could be
    right.

    **Looking once is what a spinner defeats**, and that is why this became a
    loop. Quiescence stands in for "finished", and a spinner is precisely where
    those part company: the page is stating that it is unfinished while its DOM
    sits perfectly still and the animation runs in CSS. The probe adds the
    signal a single look cannot have — a RESPONSE ARRIVING — and the loop adds
    the one no signal can replace: asking again, because arrival is late.

    `still_for` is the bar, and the caller sets it because the caller knows what
    it just did. A read of a page that finished long ago must not pay seconds
    for the possibility that it did not; a read that follows pressing *Szukaj*
    must."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:  # noqa: BLE001 — a chatty page never goes idle; carry on
        pass
    waited = 0.0
    unknown = 0
    while waited < timeout_ms:
        state = await _activity(page)
        if state is None:
            unknown += 1
            if unknown >= SETTLE_UNKNOWN_TRIES:
                return
        else:
            unknown = 0
            if page_is_done(
                quiet_ms=float(state.get("quiet") or 0),
                ready=bool(state.get("ready")),
                still_for=still_for,
            ):
                return
        try:
            await page.wait_for_timeout(WATCH_POLL_MS)
        except Exception:  # noqa: BLE001 — a page torn down mid-wait is settled
            return
        waited += WATCH_POLL_MS


async def _activity(page: Any) -> dict | None:
    """Read the page's activity counters, installing them if they are not there.

    None means the page would not answer — mid-navigation, a document with no
    scripting, a view being torn down. That is *cannot tell*, and it is never to
    be read as *nothing happened*: see `watch_step`."""
    try:
        state = await page.evaluate(_WATCH_JS)
    except Exception:  # noqa: BLE001 — a page that will not answer is not an error
        return None
    return state if isinstance(state, dict) else None


async def _body_text(page: Any) -> str:
    try:
        return await page.inner_text("body")
    except Exception:  # noqa: BLE001 — no body is a real answer: no text
        return ""


async def _without_option_floods(page: Any, text: str) -> str:
    """The page text with long dropdowns collapsed to a count.

    Applied to what is HANDED OVER and never to what a judgement is made on:
    `is_challenge` and the thin-page test were measured on whole bodies, and a
    page that shrinks by 3 500 characters here would cross both."""
    if not text:
        return text
    try:
        floods = await page.evaluate(
            browse_mod.FLOOD_JS, {"inlineChoices": browse_mod.CHOICE_INLINE_MAX}
        )
    except Exception:  # noqa: BLE001 — a page that will not answer keeps its text
        return text
    return browse_mod.strip_option_floods(text, list(floods or []))


async def _password_field_state(page: Any) -> bool | None:
    """Does the RENDERED page show a password box? True, False, or **None for
    could-not-tell**.

    Playwright's selector engine pierces open shadow roots, so this sees a form
    the serialized HTML does not — the same reason `Page.text` is taken from the
    DOM and not from `page.content()`.

    `query_selector` raises on a page that is navigating or closing under it,
    which a settled-then-acting flow does produce, and *raised* is not *no*.

    It had two consumers with opposite defaults; since #320 the evidence
    capture no longer asks, so `_has_password_field` is the only one left. The
    third value stays because the resolution is a DECISION and has to be
    visible as one: telling the model a page is a wall when aish could not tell
    would be a false claim about his account, so the unknown is resolved to
    `False` there — explicitly, at the one line that does it, rather than by an
    `except` that happens to return the same thing.
    """
    try:
        return (await page.query_selector("input[type=password]")) is not None
    except Exception:  # noqa: BLE001 — see the docstring: this is not False
        return None


async def _has_password_field(page: Any) -> bool:
    """Is the RENDERED page showing a password box, as the MODEL is told it?

    Unchanged behaviour, stated deliberately: a page that will not answer is
    not a wall. Refusing to call something a sign-in page when aish could not
    tell is the safe direction for everything the model and the owner are told.
    This is the ONE line that resolves the unknown, and it is written as
    `is True` rather than as truthiness so the resolution cannot drift.
    """
    return await _password_field_state(page) is True


# The page's own <main>, when it declares one. Purely a BUDGET decision: a read
# is capped at DOCS_MAX_CHARS, and on a shop the leading kilobytes are category
# navigation — so the cap fell inside the chrome and cut the offers short.
# Measured on the allegro.pl listing this was built for: body 13 473 chars,
# <main> 10 966, and within the SAME cap that is 25 linked offers instead of 15.
#
# It is applied only to what is handed back, never to what `is_challenge`
# judges. Narrowing the text a wall is detected in would move thresholds this
# module measured on whole bodies, and a page wrongly called a wall is the
# expensive failure here (see the reasoning around CHALLENGE_MAX_CHARS).
_MAIN_JS = """() => {
  const m = document.querySelector('main, [role="main"]');
  return m ? m.innerText : '';
}"""

# A <main> that holds most of the page is the page; one that holds a fragment
# means the site puts its content elsewhere, and preferring it would silently
# DROP content. Half is the line: losing text costs more than keeping chrome.
_MAIN_MIN_SHARE = 0.5


async def _main_text(page: Any, body: str) -> str:
    """`body` narrowed to <main>, or "" when that would lose content."""
    try:
        main = (await page.evaluate(_MAIN_JS)) or ""
    except Exception:  # noqa: BLE001 — no <main> is the common case, not a fault
        return ""
    return main if len(main) >= _MAIN_MIN_SHARE * len(body) else ""


# Anchors, with the text they are ON. A rendered page had its hrefs thrown
# away — fine for an article, fatal for a shop, where the URL IS the answer.
# Without it the model could see that an offer costs 34,99 zł and had no way to
# say where it was, so it fell back to web_search'ing `site:allegro.pl` for the
# URL of a title it had already read — 66 times in one session, against a system
# prompt that forbids exactly that. An instruction loses to a missing capability.
#
# `innerText`, not `textContent`: it is what the reader SEES, so a hidden menu
# does not enter the text, and it matches `inner_text('body')` line for line —
# which is what lets the merge attach each URL to its own line rather than
# handing over a separate list the model would have to join by title (the
# error-prone step this whole fix removes).
#
# Shadow roots are walked because `querySelectorAll` does not pierce them and a
# site that renders its cards into one would otherwise look link-free. Site
# chrome is excluded: on the measured listing that is ~30 anchors of category
# navigation, spent inside a budget the offers need.
_LINK_CHROME = 'nav, header, footer, [role="navigation"], [role="banner"], [role="contentinfo"]'
_LINKS_MAX = 150
_LINKS_JS = """(chrome) => {
  const out = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
      if (el.tagName !== 'A' || !el.getAttribute('href')) continue;
      if (el.closest && el.closest(chrome)) continue;
      const href = el.href || '';
      if (!/^https?:/i.test(href)) continue;
      const text = (el.innerText || '').trim();
      if (!text) continue;   // an image-only anchor duplicates its title anchor
      out.push([text.split('\\n')[0].trim(), href]);
    }
  };
  walk(document);
  return out;
}"""


async def _content_links(page: Any) -> list[tuple[str, str]]:
    try:
        found = await page.evaluate(_LINKS_JS, _LINK_CHROME)
    except Exception:  # noqa: BLE001 — no links is a fine answer: a page of text
        return []
    return [(str(t), str(h)) for t, h in found][:_LINKS_MAX]


async def _declared_images_async(page: Any) -> list[str]:
    try:
        return list(await page.evaluate(_IMAGES_JS))[:3]
    except Exception:  # noqa: BLE001 — no images is a fine answer
        return []


# What the page DECLARES about itself, for Google and every other indexer:
# schema.org in JSON-LD. Almost every commercial page on the web carries it,
# because rich results require it — which makes it the one description of a page
# that is language-independent, layout-independent, and not a guess.
#
# It lives in a <script>, so `inner_text` cannot see it and never could: the
# reader takes what a PERSON sees, and this is the half written for machines.
# That blindness is why a price could only ever be inferred from position on the
# page, which is what put a neighbouring advert's figure in an answer.
#
# The raw strings are handed back and interpreted in Python. A page can declare
# a 500 KB @graph, so the read is capped HERE, at the point where the size is
# known and before any of it is carried anywhere.
_LD_JSON_MAX = 60_000
_LD_JSON_BLOCKS = 8
_LD_JSON = """(max) => {
  const out = [];
  for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
    const raw = (el.textContent || '').trim();
    if (raw && raw.length <= max) out.push(raw);
    if (out.length >= 8) break;
  }
  return out;
}"""


async def _declared_data_async(page: Any) -> list[str]:
    try:
        blocks = list(await page.evaluate(_LD_JSON, _LD_JSON_MAX))
    except Exception:  # noqa: BLE001 — a page that declares nothing is normal
        return []
    return blocks[:_LD_JSON_BLOCKS]


# What the page currently has focused, so the phone can show WHICH field it is
# and offer a proper editor for it.
#
# A password's value is NEVER read back. The frame shows dots, so the pixels
# have never carried it — reading `input[type=password].value` would be
# strictly NEW exposure, and the value may be one Chrome's own profile
# autofilled, i.e. a stored credential aish never saw typed. Rendering it
# masked on the phone does not undo transmitting it. Refusal keys on
# autocomplete too, because sites flip type=password to type=text for their own
# reveal button and tapping that first would otherwise launder the value.
# What REALLY has focus, across shadow boundaries.
#
# `document.activeElement` stops at the shadow HOST: focus a field inside an
# open shadow root and the document reports the custom element wrapping it, at
# every level above it. Measured on qatarairways.com, whose booking widget is
# an Angular app inside `<app-nbx-explore>` — focusing the destination box
# leaves `document.activeElement` as APP-NBX-EXPLORE while the shadow root's
# own `activeElement` is the input, focused, ready to type.
#
# Both halves of aish that ask "what has focus" got the wrapper. `_focus`
# concluded focus had not landed and threw away the KEYBOARD rung of the press
# ladder — the rung that exists for exactly the control a real click cannot
# reach — so a field aish had successfully focused was reported as "would not
# take the action, by click, by keyboard, or otherwise" (#273). The owner's
# view asked the same question to decide whether to offer a keyboard, and a
# custom element is not editable, so it offered none.
_DEEP_ACTIVE_JS = """
  const deepActive = () => {
    let node = document.activeElement;
    while (node && node.shadowRoot && node.shadowRoot.activeElement) {
      node = node.shadowRoot.activeElement;
    }
    return node;
  };
"""

_FOCUS_JS = """() => {""" + _DEEP_ACTIVE_JS + """
  const a = deepActive();
  if (!a || a === document.body || a === document.documentElement) return null;
  const tag = a.tagName.toLowerCase();
  const type = (a.getAttribute('type') || 'text').toLowerCase();
  const auto = (a.getAttribute('autocomplete') || '').toLowerCase();
  // date/time inputs open Chrome's NATIVE picker, which screenshots cannot
  // capture — but they accept typed text, so they are treated as editable and
  // the owner types the value rather than facing an invisible calendar.
  const TEXTLIKE = ['text','search','email','url','tel','number','password','',
                    'date','datetime-local','month','week','time'];
  const secret = type === 'password' ||
                 auto === 'current-password' || auto === 'new-password';
  const editable = tag === 'textarea' || a.isContentEditable ||
                   (tag === 'input' && TEXTLIKE.includes(type));
  let label = a.getAttribute('aria-label') || a.getAttribute('placeholder') || '';
  if (!label && a.id) {
    try {
      const l = document.querySelector('label[for="' + CSS.escape(a.id) + '"]');
      if (l) label = l.innerText.trim();
    } catch (e) { /* an id CSS.escape cannot handle is simply unlabelled */ }
  }
  if (!label) label = a.getAttribute('name') || '';
  // A <select> opens native UI too, so its options are sent up and the phone
  // draws its own picker.
  let options = null;
  if (tag === 'select') {
    options = [...a.options].slice(0, 200).map(o => ({
      value: o.value, label: (o.label || o.text || o.value).slice(0, 80),
      chosen: o.selected,
    }));
  }
  const r = a.getBoundingClientRect();
  // value ONLY for a real field, and never for a secret. Falling back to
  // innerText here would ship the whole page as a "field value".
  const value = (editable && !secret && typeof a.value === 'string') ? a.value : '';
  return {
    tag, type, options,
    kind: secret ? 'password' : (editable ? 'text' : (tag === 'select' ? 'select' : 'other')),
    editable, secret, label: label.slice(0, 80),
    value: value.slice(0, 4000),
    rect: { x: r.x, y: r.y, w: r.width, h: r.height },
  };
}"""


async def _focus_info(page: Any, click: tuple[float, float] | None) -> dict | None:
    """The focused field, in FRAME coordinates, or None.

    Probes child frames too — a login form is routinely in an iframe — and
    offsets their rects by the iframe's own box so the client can outline it in
    the one coordinate space it knows."""
    for frame in page.frames:
        try:
            info = await frame.evaluate(_FOCUS_JS)
        except Exception:  # noqa: BLE001 — a cross-origin or dead frame is simply skipped
            continue
        if not info:
            continue
        if frame is not page.main_frame:
            try:
                element = await frame.frame_element()
                box = await element.bounding_box()
                if box:
                    info["rect"]["x"] += box["x"]
                    info["rect"]["y"] += box["y"]
            except Exception:  # noqa: BLE001 — un-offsettable: outline would lie, so drop it
                info["rect"] = {"x": 0, "y": 0, "w": 0, "h": 0}
        # Did the tap land ON this field? Focus also moves as a SIDE EFFECT —
        # pages autofocus their first input, and dismissing a cookie banner can
        # leave focus in one. Opening an editor then is the "surprising popup"
        # complaint rebuilt, so the client only opens one when the owner
        # actually aimed at the field.
        r = info["rect"]
        info["tapped"] = bool(
            click
            and r["w"]
            and r["x"] <= click[0] <= r["x"] + r["w"]
            and r["y"] <= click[1] <= r["y"] + r["h"]
        )
        return info
    return None


async def _dismiss_consent(page: Any) -> str:
    """Take a consent wall down with the word list. The selector that matched,
    or "".

    It used to return `None`, swallow every exception, and be called from six
    places, which meant a banner it failed to dismiss was invisible at all six
    (#322). The return value is what lets a caller say so.

    **It does NOT count itself, and that is the whole design of the counter.**
    Five of the six callers run this speculatively on a page that has just
    opened, where no banner is the overwhelmingly common case — count there and
    "missed" is ~100% forever and says nothing about whether the list works.
    The tally is noted by `_uncover` instead, which asks only when the
    structural check has ALREADY found something on top of a control: *the list
    was handed a known obstruction, and this is whether it cleared it.* That
    number going to zero is a list that has stopped matching.

    Still best-effort by design: a page with no banner is the norm and must
    cost nothing but the probes."""
    for selector in _CONSENT_SELECTORS:
        try:
            button = page.locator(selector).first
            if await button.is_visible(timeout=800):
                await button.click(timeout=2_000)
                await page.wait_for_timeout(1_200)
                return selector
        except Exception:  # noqa: BLE001 — best effort; a missing banner is the norm
            continue
    return ""


def _watch_console(page: Any, log: browse_mod.ConsoleLog) -> None:
    """Record what THIS page writes to its own console.

    Two Playwright events, because they are two different things and the one
    that matters most here is the second: `console` is the page calling
    `console.error`/`warn`, and `pageerror` is a handler THROWING, which writes
    nothing to `console` at all. A login button whose handler died on
    `ReferenceError: grecaptcha is not defined` produces only the second, and
    that is precisely the sentence a day of guessing at eon.pl did not have.

    Attached per PAGE and never per session, because a session's page is
    replaced when a control opens a new tab (`_adopt_new_tab`) — handlers on
    the old document would go on reporting a page nobody is driving.

    Every handler is total: it runs on the owner loop from Playwright's own
    dispatch, where a raised exception is one nothing is waiting for, and a
    console message is an extra that must never cost the action it describes.
    Attaching is suppressed for the same reason the capture is — a page object
    that has no `on` is a page that gets no console, never a failed browse."""

    def field(msg: Any, name: str) -> str:
        # `type` and `text` are PROPERTIES in Playwright's Python API and
        # METHODS in its JavaScript one. Reading them either way costs one
        # `callable` check and removes a whole class of silent failure: a
        # bound method stringifies to something no level matches, so the
        # message would be dropped with nothing anywhere saying why.
        value = getattr(msg, name, "")
        return str((value() if callable(value) else value) or "")

    def message(msg: Any) -> None:
        with contextlib.suppress(Exception):
            log.note(field(msg, "type"), field(msg, "text"))

    def threw(exc: Any) -> None:
        with contextlib.suppress(Exception):
            log.note(browse_mod.CONSOLE_UNCAUGHT, str(exc))

    with contextlib.suppress(Exception):
        page.on("console", message)
        page.on("pageerror", threw)


class _Session:
    """ONE CHAT's browse page, and what is true about it.

    `epoch` counts documents THIS session has driven — it is what a chat
    carries between calls so an act cannot land on a page that changed under
    it (#272). Per-session rather than per-owner now that every chat has its
    own page: a counter shared across chats would make every other chat's act
    look like a page change to this one."""

    def __init__(self, page: Any) -> None:
        # What the page said while the action now running was carried out.
        # It belongs to the SESSION and not to the page, because the page is
        # replaced mid-act when a control opens a tab and the messages either
        # side of that are one action's evidence.
        self.console = browse_mod.ConsoleLog()
        self.adopt(page)
        self.epoch = 0
        self.touched = time.monotonic()

    def adopt(self, page: Any) -> None:
        """Drive `page` from now on, listening to its console.

        The ONE place a session's page is assigned, so a tab this session moves
        to cannot be one whose console nobody is recording."""
        self.page = page
        _watch_console(page, self.console)

    def live(self, now: float) -> bool:
        if now - self.touched > BROWSE_MAX_IDLE:
            return False
        try:
            return not self.page.is_closed()
        except Exception:  # noqa: BLE001 — a page that cannot answer is gone
            return False


class _Owner:
    """The one thread that touches Playwright — and now an event LOOP, not a
    job queue.

    It was a queue, and that quietly cost aish a property it has everywhere
    else: `_execute_tool_calls` fans read-only tools out concurrently because
    they are network-bound, so a turn reading three pages should take as long
    as the slowest. Routed through one serial browser they took the SUM —
    measured live at 7.0s, 11.5s, 14.3s for three Allegro pages, and the third
    was slow enough that a half-painted page got mistaken for a block wall.

    The fix is not more browsers. Chrome locks the profile, and the profile is
    the point: one set of the owner's sessions, shared by every read. So it is
    one browser with many TABS, driven by the async API on a loop this thread
    owns — `read()` still blocks its caller, but N callers now overlap inside
    one Chrome.
    """

    def __init__(self) -> None:
        self._playwright: Any = None
        self._context: Any = None
        # The SEARCH browser: a second Chrome on `search_profile_dir()`, signed
        # into nothing. Held beside the owner's rather than replacing it because
        # the two answer different questions — one reads the web AS HIM, this
        # one reads it as nobody — and a single context could only ever be one
        # of those at a time.
        self._cold: Any = None
        self.loop: Any = None
        self._ready = threading.Event()
        self._lock: Any = None  # created on the loop: guards context setup
        # The page a remote view is driving, when one is open. Held here
        # because it must outlive a single job: the whole point of the view is
        # that tap, type and tap again land on the SAME page.
        self.view: Any = None
        # Which profile the view is driving. Outlives the view itself: the
        # owner confirms a sign-in AFTER closing it, and that confirmation has
        # to land in the right record.
        self.view_cold = False
        self.recorded_nav = -1
        self.view_hosts: set[str] = set()
        self.view_touched = 0.0
        self.busy = 0
        # When a job last FINISHED on this browser. `busy` only answers "is one
        # running right now", which made the reaper reap a context that had been
        # used seconds earlier and charge the next read a ~2s relaunch (#224).
        # 0.0 means nothing has ever run: reaping a context that was never
        # opened is a no-op, so the eager value is the right one.
        self.last_used = 0.0
        self.notice = ""   # something native happened that the frame cannot show
        # Counts documents, not URLs. A logout that lands back on a similar
        # address, an SPA route change, or a plain reload all replace the
        # document without necessarily changing `url` — and the owner watched
        # his zoom survive a Google logout because of exactly that.
        self.navigations = 0
        self.pending_signin = ""   # host a password was submitted to
        # What the owner is part-way through typing into a login form, held so
        # it can be SAVED if the sign-in works (#280). Never written anywhere
        # until then, dropped when the view ends, and never logged or traced —
        # the remote-view input path retains nothing by default and this is the
        # one bounded exception, opened only by his own checkbox.
        self.pending_credential: dict = {}
        # The view page a credential watcher is attached to, so it is attached
        # once per view rather than once per keystroke (#296).
        self.credential_watch: Any = None
        # Hosts whose page ASKED FOR A PASSWORD while he was looking at it.
        # This — not the browsing history — is what `view_close` may ask him
        # about. See `_note_visit`.
        self.password_hosts: set[str] = set()
        # Hosts this view WATCHED a sign-in complete at. Recorded, not asked.
        self.signed_in_here: set[str] = set()
        self.pending_nav = -1      # the navigation count when that happened
        self.last_url = ""         # where the view was, so it can be reopened
        # How many requests the VIEW page has in flight (#223). Counted from
        # Playwright's own request events rather than from anything injected
        # into the page: wrapping `window.fetch` would answer the same question
        # and would also make `fetch.toString()` report non-native code, which
        # is a bot-detection signal on exactly the sites this browser exists to
        # read. It only ever DECIDES TO SKIP work, so every way of being wrong
        # about it — a leaked count, a long-poll that never finishes — leaves
        # the number too high and the behaviour exactly as it was.
        self.view_requests = 0
        # The pages the MODEL is driving (#237, #272), ONE PER CHAT. Each is a
        # page on the same context, never the view's: the view is the owner's
        # hands at a phone viewport, and the two must not fight over one page.
        # Held on the owner because a browse session is exactly the thing that
        # has to outlive a single job — click, read, click again, all on the
        # same document.
        #
        # Keyed by chat, and that is the whole of #272's second half. One slot
        # meant a second chat's `browse(url)` did not open its own page, it
        # NAVIGATED the page the first chat was standing on — measured on
        # 2026-08-22, where a chat searching for flights spent 225 seconds
        # typing into another chat's IMDb ratings page. A tab costs memory; a
        # chat silently driving another chat's session costs the owner's
        # account.
        self.browse_pages: dict[str, _Session] = {}
        # Downloads that arrived, as (page, download). Stashed by a SYNC event
        # handler and saved afterwards inside the job: registering an async
        # handler would need its own task, and wrapping every click in
        # `expect_download` would make every ordinary click pay that timeout.
        #
        # Keyed by PAGE because the tab that downloads is very often not the tab
        # that was clicked: a `target=_blank` download link opens a fresh tab
        # which Chrome closes the instant the transfer starts. Listening on the
        # one tab aish opened missed every invoice on eon.pl — the file arrived
        # in a tab nobody was listening to, and the snapshot faithfully reported
        # that nothing had happened (#246). So the listener goes on the CONTEXT,
        # and who a download belongs to is decided here instead.
        self.downloads: list[tuple[Any, Any]] = []
        # Pages a `read` owns. Everything else on the context is the browse
        # session's — including a popup that lived for half a second.
        self.read_pages: set[Any] = set()

    def watch_downloads(self, page: Any) -> None:
        """Record what this page downloads. Registered for EVERY page."""
        page.on("download", lambda download: self.downloads.append((page, download)))

    def claimable(self, page: Any, *, browse_page: Any) -> bool:
        """Whether `browse_page`'s chat may take `page` as its own.

        A page a `read` opened belongs to the read, which CLOSES it on its way
        out; a page another chat is standing on belongs to that chat. Anything
        else on the context is this session's — including the popup that lived
        for half a second.

        Asked in one place because both callers ask it about the same set and a
        second phrasing is what produced #291: a new tab was adopted with no
        exclusion at all, so a read landing inside a press window moved the
        chat onto the reader's tab and the read then closed it."""
        if page is browse_page:
            return True
        others = {
            session.page
            for session in self.browse_pages.values()
            if session.page is not browse_page
        }
        return page not in self.read_pages and page not in others

    def take_downloads(
        self, *, read_page: Any = None, browse_page: Any = None
    ) -> list[Any]:
        """Drain the downloads belonging to one caller.

        A read takes its own page's. A browse session takes its own page's plus
        anything belonging to no read and to no OTHER chat — which is what
        keeps the ephemeral popup counting (the `target=_blank` download tab
        Chrome closes the instant the transfer starts) without handing it to
        whichever chat happened to snapshot next."""
        keep: list[tuple[Any, Any]] = []
        mine: list[Any] = []
        for page, download in self.downloads:
            if read_page is not None:
                claim = page is read_page
            else:
                claim = self.claimable(page, browse_page=browse_page)
            (mine if claim else keep).append(download if claim else (page, download))
        self.downloads = keep
        return mine

    def run(self) -> None:
        import asyncio

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._lock = asyncio.Lock()
        self._ready.set()
        self.loop.create_task(self._reap())
        self.loop.run_forever()

    async def _reap(self) -> None:
        """Close an idle browser. Same rules as the queue version: never reap a
        live view (a 2FA pause outlasts any sane idle timer), never let an
        abandoned one hold Chrome forever, and never reap mid-read."""
        import asyncio

        while True:
            await asyncio.sleep(IDLE_SECONDS)
            if self.reapable(time.monotonic()):
                await self._close()

    def reapable(self, now: float) -> bool:
        """May the browser be closed at `now`? A pure decision, so the rule can
        be tested without a timer or a Chrome.

        Three reasons to keep it, and the third was missing (#224). A job is
        RUNNING (`busy`). Somebody is part-way through something that outlives
        one job (`held`). Or a job merely FINISHED RECENTLY — which the tick
        alone could not see, because it fired on a wall-clock cadence rather
        than on how long the browser had actually been quiet. A read that
        landed five seconds before the tick met a closed context and paid the
        ~2s relaunch, and on a busy chat that repeats every three minutes.

        The idle window is therefore between one and two ticks, deliberately.
        Sampling more often to tighten it would spend wakeups to reclaim memory
        a few seconds sooner, and the launch it saves is worth more than the
        seconds of Chrome it keeps."""
        if self.busy or self.held():
            return False
        return now - self.last_used >= IDLE_SECONDS

    def held(self) -> bool:
        """Is somebody part-way through something? Then this browser stays.

        Two flows outlive a single job and both die if the context goes: the
        owner's own window, and the model's browse session. Each gets a ceiling
        so an abandoned one cannot hold Chrome forever — this box runs a Home
        Assistant VM and Colima under a 16 GB roof, and an idle Chrome is not
        free."""
        now = time.monotonic()
        if self.view is not None and now - self.view_touched <= VIEW_MAX_IDLE:
            return True
        return any(
            session.live(now) for session in list(self.browse_pages.values())
        )

    async def _open(
        self,
        profile: Path,
        *,
        args: list[str] | None = None,
        viewport: dict | None = None,
        device_scale_factor: float | None = None,
    ) -> Any:
        """Launch one persistent context on `profile`. Caller holds the lock."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover — see unavailable_reason
            raise BrowserUnavailable(str(exc)) from exc
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        async def launch():
            return await _launch(
                self._playwright,
                args=args or _OFFSCREEN_ARGS,
                profile=profile,
                viewport=viewport,
                device_scale_factor=device_scale_factor,
            )

        try:
            context = await launch()
        except Exception as exc:  # noqa: BLE001 — one specific, recoverable cause
            # Chrome leaves a SingletonLock in the profile when it dies badly.
            # Every later launch then fails, so every read AND every view fails
            # until somebody kills Chrome by hand — on a headless server with
            # nobody in front of it.
            if not _clear_stale_lock(exc):
                raise
            context = await launch()
        # On the CONTEXT, so a tab aish never opened still reports what it
        # downloaded — see `_Owner.downloads`.
        context.on("page", self.watch_downloads)
        for page in list(context.pages):
            self.watch_downloads(page)
        return context

    async def context(
        self,
        *,
        args: list[str] | None = None,
        viewport: dict | None = None,
        device_scale_factor: float | None = None,
    ) -> Any:
        # Under the lock: concurrent reads arriving cold would otherwise each
        # launch a Chrome against a profile only one of them can hold.
        async with self._lock:
            if self._context is None:
                self._context = await self._open(
                    profile_dir(),
                    args=args,
                    viewport=viewport,
                    device_scale_factor=device_scale_factor,
                )
            return self._context

    async def cold_context(
        self,
        *,
        args: list[str] | None = None,
        viewport: dict | None = None,
        device_scale_factor: float | None = None,
    ) -> Any:
        """The search browser. The PROFILE is not a parameter and never will be
        — that is the whole fence. Window size is, because signing this profile
        in uses the same remote view the owner's does."""
        async with self._lock:
            if self._cold is None:
                self._cold = await self._open(
                    search_profile_dir(),
                    args=args,
                    viewport=viewport,
                    device_scale_factor=device_scale_factor,
                )
            return self._cold

    async def _close(self) -> None:
        self.view = None  # a closed context has no page left to drive
        # Every chat's page dies with the context. Each will be told
        # NOTHING_OPEN and can reopen — see `_session`.
        self.browse_pages = {}
        for name in ("_context", "_cold"):
            context = getattr(self, name)
            if context is None:
                continue
            try:
                await context.close()
            except Exception:  # noqa: BLE001 — a dead browser is already closed
                pass
            setattr(self, name, None)

    async def close_now(self) -> None:
        await self._close()


def _no_browser_yet() -> bool:
    """Is there no browser thread at all?

    `_submit` STARTS one, which is right for a read and wrong for a watcher: a
    poll that launches the thing it is watching would mean the view's own
    housekeeping could bring Chrome up on a machine nobody asked it to. The
    honest answer to "what is the page doing" when there is no page is nothing.
    """
    with _OWNER_LOCK:
        return _OWNER is None or not _OWNER.is_alive()


def _submit(job: Callable[[_Owner], Any], timeout: float) -> Any:
    """Run `job` (an async callable taking the owner) on the browser loop and
    wait for it. Callers block; the WORK overlaps."""
    import asyncio

    global _OWNER
    with _OWNER_LOCK:
        if _OWNER is None or not _OWNER.is_alive():
            owner = _Owner()
            _OWNER = threading.Thread(target=owner.run, name="aish-browser", daemon=True)
            _OWNER.owner = owner  # type: ignore[attr-defined]
            _OWNER.start()
            owner._ready.wait(10)
        owner = _OWNER.owner  # type: ignore[attr-defined]

    async def run():
        owner.busy += 1
        try:
            return await job(owner)
        finally:
            owner.busy -= 1
            # Stamped on the way OUT, which is the moment that matters: the gap
            # the reaper has to respect starts when a job stops, not when it
            # started. In-flight is already covered by `busy`.
            owner.last_used = time.monotonic()

    return asyncio.run_coroutine_threadsafe(run(), owner.loop).result(timeout=timeout)


# ------------------------------------------------- signing in as him (#280)
#
# The one place aish types a credential without his hands on the keys. Every
# fence here is structural, because the model is not in this path at all: it
# cannot ask for a sign-in, cannot name the host, cannot see the value, and
# cannot see the fields. What it gets is a page that was already readable.
#
# The checks below are in the order that a mistake would be cheapest to make
# and most expensive to have made:
#
#   1. the LIVE origin still equals the recorded one — a redirect to an
#      identity provider is a different origin and stops here;
#   2. exactly ONE password field in the document, in the main frame;
#   3. the form POSTs, and posts to the SAME ORIGIN. This is the check whose
#      absence made an earlier draft leak the credential outright: page origin
#      says nothing about where the form SENDS, so any same-origin page that
#      can render markup — a comment field, a user-content path, an open
#      redirect landing back on the origin — could carry
#      `<form action="https://evil/collect">` and be handed the live password.
#      A GET form is refused for a second reason of its own: it puts the
#      password in the query string, and `remember_page` then writes that URL
#      to recent.json in cleartext, outside every scrubbing path there is.

SIGNIN_FORM_JS = "(expected) => {" + browse_mod.DEEP_JS + """
  // The origin is checked HERE, against the origin the credential was saved
  // for, and in the SAME step that tags the fields. Doing it in Python before
  // this call left a gap: the page could navigate in between, and the tags
  // would land on whatever arrived.
  if (location.origin !== expected) {
    return {ok: false, why: 'the page moved to ' + location.origin};
  }
  const vis = (el) => {
    if (!el || el.disabled) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1 && el.checkVisibility?.({
      checkOpacity: true, checkVisibilityCSS: true,
    }) !== false;
  };
  // Through shadow roots, like `_has_password_field` already is: a login form
  // inside a web component used to read as "no password field on the page",
  // which fails closed — aish refuses to fill a credential it could have
  // filled — but is still a page the owner cannot sign into (#273).
  const pw = deepAll('input[type=password]').filter(vis);
  if (pw.length === 0) return {ok: false, why: 'no password field on the page'};
  if (pw.length > 1) return {ok: false, why: 'more than one password field'};
  const secret = pw[0];
  // A <form> is OPTIONAL, and requiring one is what broke this on the first
  // real site it met: linkedin.com renders its login as a React app with no
  // form element at all, which is the ordinary shape of a modern login page
  // rather than an edge case. Where a form DOES exist its declared
  // destination is still checked — it is a cheap early out — but the fence
  // that actually holds is at the network layer, because a form can be
  // submitted by JavaScript to anywhere regardless of its action.
  const form = secret.form;
  let target = '';
  if (form) {
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') {
      return {ok: false, why: 'the form is a GET, which would put the password in the URL'};
    }
    try { target = new URL(form.action, document.baseURI).origin; }
    catch (e) { return {ok: false, why: 'the form has no readable destination'}; }
    if (target !== expected) return {ok: false, why: 'the form sends to ' + target};
  }
  // Scope for the identifier: the form when there is one, otherwise the
  // nearest ancestor that holds the password field and a button. Never the
  // whole document — a page-wide search finds the newsletter box.
  let scope = form;
  if (!scope) {
    scope = secret.parentElement;
    while (scope && scope !== document.body &&
           !scope.querySelector('button, input[type=submit]')) {
      scope = scope.parentElement;
    }
    // The field's OWN root, never the document body. Inside a shadow root the
    // walk above runs out at the root — a ShadowRoot is not an Element, so
    // `parentElement` is null well before `document.body` — and falling back
    // to the body is precisely the page-wide search the comment above
    // forbids: it would tag the newsletter box as the identifier and type the
    // owner's username into it.
    scope = scope || secret.getRootNode();
  }
  const TEXTY = ['text', 'email', 'tel', 'number', ''];
  const ident = [...scope.querySelectorAll('input')].filter(
    (el) => TEXTY.includes((el.getAttribute('type') || '').toLowerCase()) && vis(el)
  )[0];
  secret.setAttribute('data-aish-signin', 'password');
  // The identifier is OPTIONAL. A "welcome back, <name>, enter your password"
  // page has no e-mail field at all — which is exactly the page linkedin.com
  // served — and refusing it would refuse the easiest sign-in there is.
  if (ident) ident.setAttribute('data-aish-signin', 'identifier');
  // Only a form's own submit button is ever pressed. With no form the submit
  // is the ENTER key, deliberately: choosing a button by its words on a login
  // page is how the model ended up pressing "Continue with Google".
  //
  // A genuine submit control is always preferred. Where the form has NONE,
  // the fallback is COUNTING and never reading: a form whose only visible
  // button is `type="button"` has exactly one thing that can be pressed, and
  // pressing it is the only gesture that submits anything. eon.pl is that
  // shape — `<button type="button" onclick="submitForm()">` is the whole of
  // its `#login-form`'s buttons — and excluding it left the replay with no
  // target at all, falling through to an Enter that is a NO-OP there: a POST
  // form with no default button and two text-entry fields performs no
  // implicit submission (WHATWG). Nothing was ever sent, on any attempt.
  //
  // The "Continue with Google" protection is preserved BY CONSTRUCTION and
  // not by vocabulary: a form carrying an SSO button beside its login button
  // has TWO buttons, so the count fails and the submit stays Enter, exactly
  // as before. No word list, no button text, no test id — those are the
  // things that made the model guess in the first place.
  //
  // And the count runs only where ENTER IS DEMONSTRABLY A NO-OP. WHATWG's
  // implicit submission performs no submission at all when a form has no
  // default button and MORE THAN ONE field that blocks implicit submission —
  // which is the eon.pl shape exactly, and the reason its Enter sent nothing.
  // Where the form has ONE such field, Enter submits it, so there is nothing
  // for a press to fix and the button is left alone: this can only ever
  // SUBTRACT presses, never add one, which is the only direction a heuristic
  // that picks a control without reading it may move in.
  //
  // The blocking set is the spec's own list, and a missing `type` is Text
  // (hence the '' entry) — the same reason TEXTY carries one. Counted through
  // the form's descendants rather than `form.elements`, so a field associated
  // by the `form=` attribute from outside is not counted: that undercounts,
  // which falls back to Enter, which is the safe side of this gate.
  const BLOCKS_IMPLICIT_SUBMISSION = [
    'text', 'search', 'url', 'tel', 'email', 'password', 'date', 'month',
    'week', 'time', 'datetime-local', 'number', '',
  ];
  let submit = null;
  if (form) {
    const pressable = [...form.querySelectorAll('button, input[type=submit]')].filter(
      (el) => vis(el) && (el.getAttribute('type') || '').toLowerCase() !== 'reset'
    );
    submit = pressable.filter(
      (el) => (el.getAttribute('type') || '').toLowerCase() !== 'button'
    )[0] || null;
    if (!submit && pressable.length === 1) {
      const blocking = [...form.querySelectorAll('input')].filter(
        (el) => BLOCKS_IMPLICIT_SUBMISSION.includes(
          (el.getAttribute('type') || '').toLowerCase()
        )
      );
      if (blocking.length > 1) submit = pressable[0];
    }
  }
  if (submit) submit.setAttribute('data-aish-signin', 'submit');
  return {
    ok: true, posts_to: target, page_origin: location.origin,
    identifier: !!ident, submit: !!submit, form: !!form,
  };
}
"""

# Read again immediately before the press. The tag survives a SAME-DOCUMENT
# change, so a page that rewrites `form.action` after it was checked would be
# submitted to the new destination with the credential already typed into it.
# This is the same fence `browse_act` keeps for an approved control: the thing
# that was checked has to be the thing that happens.
SIGNIN_STILL_OURS_JS = "(expected) => {" + browse_mod.DEEP_JS + """
  if (location.origin !== expected) return 'the page moved to ' + location.origin;
  // The SAME reach as the pass that wrote this tag. A writer that can see
  // into a shadow root and a reader that cannot is how the calendar came to
  // lose the field it had itself tagged (#273) — here it would have been
  // worse than useless: aish would tag the password field and then refuse to
  // type into it, reporting that the field was gone.
  const secret = deepOne('[data-aish-signin="password"]');
  if (!secret) return 'the password field is gone';
  const form = secret.form;
  if (form) {
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') {
      return 'the form stopped being a POST';
    }
    let target;
    try { target = new URL(form.action, document.baseURI).origin; }
    catch (e) { return 'the form has no readable destination'; }
    if (target !== expected) return 'the form now sends to ' + target;
  }
  return '';
}
"""

# A second factor is not a failure, and telling them apart is the difference
# between "sign in again" and "burn the one attempt on a good password".
SECOND_FACTOR_JS = "() => {" + browse_mod.DEEP_JS + """
  return deepAll('input').some((el) => {
  const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
  const mode = (el.getAttribute('inputmode') || '').toLowerCase();
  const len = parseInt(el.getAttribute('maxlength') || '0', 10);
  return auto === 'one-time-code' || (mode === 'numeric' && len > 0 && len <= 8);
  });
}"""


# What a login page LOADS is what says it is protected by an anti-automation
# widget, and it says it the same way in every language (#320). This collects
# and judges nothing: addresses, class names and ids go to
# `signin.captcha_vendor`, which owns the vocabulary. Shadow roots are walked
# for the reason `SIGNIN_STILL_OURS_JS` walks them — a widget rendered into one
# is invisible to `querySelectorAll`.
CAPTCHA_MARKS_JS = "() => {" + browse_mod.DEEP_JS + """
  const marks = [];
  for (const el of deepAll('script[src], iframe[src]')) {
    const src = el.getAttribute('src') || '';
    if (src) marks.push(src.slice(0, 300));
  }
  for (const el of deepAll('[class*="captcha" i], [id*="captcha" i], [class*="turnstile" i]')) {
    // getAttribute, not .className: on an SVG element that property is an
    // SVGAnimatedString and stringifies to nothing useful.
    marks.push((el.getAttribute('class') || '') + ' ' + (el.getAttribute('id') || ''));
  }
  return marks.slice(0, 200);
}"""

# The brand name in the page's own words. `innerText`, not `textContent`, for
# the reason the link merge uses it: it is what the reader SEES, so a hidden
# template does not count as a declaration.
PAGE_TEXT_JS = "() => document.body ? document.body.innerText : ''"

# The page's own MACHINE-READABLE statement that it judged what was typed
# (#320). Both signals are ARIA, so both are the same in every language: a
# password box the page has marked `aria-invalid`, and the live regions a form
# announces an error through. Read BEFORE the submit as well as after, because
# a login page carrying an empty (or already-populated) alert region is the
# ordinary case — only what APPEARED can be an answer to what was sent.
SIGNIN_REJECTION_JS = "() => {" + browse_mod.DEEP_JS + """
  const said = [];
  for (const el of deepAll('[role="alert"], [role="alertdialog"], [aria-live="assertive"]')) {
    const text = (el.innerText || '').trim();
    if (text) said.push(text.slice(0, 200));
  }
  return {
    invalid: deepAll('input[type="password"][aria-invalid="true"]').length > 0,
    said: said,
  };
}"""


# Forced back to a PASSWORD box before the shutter, so the picture of the
# sign-in attempt renders the field as dots (#295, after #320).
#
# The danger is real and is visible in the owner's own eon.pl screenshot: a
# great many login pages carry a show-password toggle, and a toggled field is
# `type="text"` — plaintext in the picture. The site's own script may flip it
# whenever it likes. The tag aish wrote survives the flip, so the field is
# found whatever it has become.
#
# **It used to be EMPTIED, and that cost the owner the one thing the picture
# was for.** A blanked field is indistinguishable from a field that was never
# filled, so the picture could not answer *did the password get typed in at
# all* — which is the question he asks of it. Masking keeps the value and
# shows it as dots, so a filled field and an empty one look different again.
# The dot count leaks the length; his passwords are long and generated, so
# that is nothing, and it is a trade he made explicitly.
#
# What this enforces is exactly *the password is not rendered as TEXT in the
# field aish typed it into* — no wider. A page that mirrors the value into its
# own markup (a hand-rolled show-password writing into a <span>) is not a
# field, is not reachable this way, and was not reachable by the blanking
# either. The IDENTIFIER is deliberately left alone; #296 settled that the
# credential fenced here is the password and not the username.
#
# It reports what it did, because the caller REFUSES the shutter unless this
# came back clean. A field that cannot be CONFIRMED masked is an unknown about
# a document aish has typed his password into, which is a different thing from
# the browse path's unknown (there, aish has typed nothing and there is nothing
# to protect — see `_evidence_frame`).
#
# ORDERING, because a reader will wonder whether this can disturb the sign-in:
# it runs at the END of the attempt, after the form has been submitted or not
# and after the outcome has already been judged, on a page that is closed
# immediately afterwards. Setting `type` dispatches no input event and changes
# no value.
SIGNIN_MASK_JS = "() => {" + browse_mod.DEEP_JS + """
  const fields = deepAll('[data-aish-signin="password"]');
  let forced = 0;
  for (const el of fields) {
    if ((el.getAttribute('type') || '').toLowerCase() !== 'password') {
      try { el.type = 'password'; } catch (e) { return {ok: false}; }
      forced++;
    }
    // Read back from the LIVE property, never the attribute just written: an
    // element that is not an <input> at all, or one whose type the page put
    // straight back, answers here with what it really is. Fail closed.
    if ((el.type || '').toLowerCase() !== 'password') return {ok: false};
  }
  return {ok: true, fields: fields.length, forced: forced};
}"""


# Methods that can carry a body at all. A GET has none — but its ADDRESS can
# still carry a credential, so a GET is read, never waved through.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# One request's worth of text to search. A file upload is megabytes and a
# credential is not hiding at the far end of one.
_SCAN_MAX_CHARS = 64_000


def _request_carriers(request: Any) -> list[str]:
    """The parts of a request that could carry a credential: its address, its
    body, its headers.

    Shared by the fence and by the capture-time watcher deliberately. A writer
    and a reader of the same fact must look in the same places — the lesson
    `SIGNIN_STILL_OURS_JS` records about shadow roots, one layer out: if
    capture cannot see a destination the fence can, the fence aborts the very
    request the owner's own sign-in makes."""
    parts: list[str] = []
    for read in (
        lambda: request.url or "",
        lambda: (request.post_data or "") if request.method.upper() in _BODY_METHODS else "",
        lambda: "\n".join(str(v) for v in (request.headers or {}).values()),
    ):
        with contextlib.suppress(Exception):  # a dead request answers nothing
            if piece := read():
                parts.append(piece[:_SCAN_MAX_CHARS])
    return parts


def _carries_the_credential(request: Any, needles: Sequence[str]) -> bool:
    if not needles:
        return False
    return any(signin_mod.carries_secret(part, needles) for part in _request_carriers(request))


@dataclass
class _CredentialWatch:
    """What the fence SAW of the credential itself during one sign-in (#320).

    The fence already recognises a credential-bearing request, so it is the one
    thing in this subsystem that can answer BOTH halves of *was the password
    refused?* — **did it ever leave**, and **what did the site say back**. They
    are different questions and this is deliberately one object, because the
    alternative is two observers with two definitions of "carries the
    credential", which is a second opinion about which request a status belongs
    to.

    `armed` is not decoration. `sent_to` being empty means two different things
    — nothing carrying the credential was let through, or nobody was looking —
    and only the first supports a claim about the password. Nothing may read
    `sent_to` without reading `armed`.
    """

    # Set once `page.route` has returned, so the handler below is installed.
    armed: bool = False
    # Origins of credential-bearing requests aish LET GO. Not "the site
    # received it": aish cannot see that, and this is the strongest true
    # statement available — the password left aish's hands towards this origin.
    sent_to: list[str] = field(default_factory=list)
    # Origins of credential-bearing requests aish ABORTED. A wrong-destination
    # replay: a fact about the credential, whatever the sign-in's own outcome.
    blocked: list[str] = field(default_factory=list)
    # Statuses the site ANSWERED credential-bearing requests with. A refusal
    # here is the one unambiguous "the site judged the value" there is —
    # `signin.refused_the_credential` owns which statuses count.
    answered: list[int] = field(default_factory=list)

    # Open only across the SUBMIT WINDOW — `_sign_in_on` opens it immediately
    # before the press and closes it once the page has settled. Outside it
    # nothing is kept, so this is never a log of a browsing session.
    watching_traffic: bool = False
    # Method, origin and path of requests the fence did NOT recognise as
    # carrying the credential, in that window. **Diagnostic evidence about a
    # failure, and never a verdict** — see `signin.unrecognised_submission`.
    # It reaches the sentence the owner reads, and the one gesture-level brake
    # in `_press_the_button_again`, which it can only ever apply. It is not an
    # argument to `judge_a_failed_sign_in` and it sets neither `stale` nor
    # `tried`. It reaches the STORE only if some later edit makes an ending
    # that composes it also set `stale`, which is what
    # `test_no_summary_can_reach_DURABLE_storage` walks the failure table for.
    traffic: list[str] = field(default_factory=list)

    def note_sent(self, origin: str) -> None:
        if origin and origin not in self.sent_to:
            self.sent_to.append(origin)

    def note_blocked(self, origin: str) -> None:
        if origin and origin not in self.blocked:
            self.blocked.append(origin)

    def note_traffic(self, summary: str) -> None:
        """Keep one request summary, if the window is open and there is room."""
        if not self.watching_traffic or len(self.traffic) >= signin_mod.REQUEST_MAX:
            return
        if summary and summary not in self.traffic:
            self.traffic.append(summary)

    @property
    def was_tried(self) -> bool:
        """Did the password actually go out? Only an ARMED watch may say yes."""
        return self.armed and bool(self.sent_to)

    @property
    def was_refused(self) -> bool:
        """Did the site answer a credential-bearing request with a refusal?"""
        return signin_mod.refused_the_credential(self.answered)


async def _fence_the_origin(
    page: Any, record: Any, secret: str, watch: _CredentialWatch
) -> None:
    """Refuse, at the network, to let THE CREDENTIAL leave where it belongs.

    **This is the fence, and the DOM checks are only an early out.** A form's
    `action` is a declaration, and a login page can submit by JavaScript to
    whatever address it likes regardless of it — so checking the attribute
    answers a question the page is not obliged to answer honestly. Most modern
    login pages have no form at all (linkedin.com is a React app), which made
    the static check simultaneously too weak and, when it was mandatory, strong
    enough to refuse the ordinary case.

    What it fences is the credential and not the connection (#296). The first
    version aborted every request with a body going to another origin, which on
    a real commercial login page means the tracking pixel and the consent call
    — so it fired on virtually every site, and a page that legitimately posts
    credentials to an API subdomain had its sign-in prevented rather than
    merely misreported. aish holds the value here, so it can ask the only
    question that matters: is THIS request carrying it, and is it going
    somewhere the owner's own sign-in sent it? Everything else on the wire is
    none of the fence's business and passes unexamined and unreported.

    Only the ORIGIN of a recorded request is kept, aborted or allowed. The
    address itself may carry the credential in its query string, and these
    lists are pushed to his phone and written to the store.

    **It also reports what it let THROUGH and what came back** (#320). Both
    live here, in one `watch`, because the same look that decides whether to
    abort is the only observation of whether the password was tried at all —
    and because "carries the credential" must have exactly one definition and
    one set of needles. A second observer that matched differently would be a
    second opinion about which request a status belongs to, and it is the
    agreement between the two halves that decides whether a credential is
    retired.

    `watch` is filled in place rather than returned: the handlers outlive this
    call, and every request routed between here and the end of the sign-in is
    part of the answer."""
    needles = signin_mod.secret_needles(secret)

    def carries(request: Any) -> bool:
        """Never raises: a request aish cannot read is a request it has nothing
        to say about, and a fence that hangs the page is worse than no fence."""
        try:
            return _carries_the_credential(request, needles)
        except Exception:  # noqa: BLE001
            return False

    def going_astray(request: Any) -> str:
        """The origin to refuse a credential-bearing request, or ""."""
        try:
            if signin_mod.may_receive_credential(record, request.url):
                return ""
            return signin_mod.origin_of(request.url) or "an unreadable address"
        except Exception:  # noqa: BLE001
            return ""

    def note_other_traffic(request: Any) -> None:
        """A request the fence did NOT recognise as carrying the credential,
        summarised to method, origin and path.

        **Only unrecognised requests, and that is a safety property rather than
        tidiness.** The summary keeps a PATH, and the path is one of the places
        `carries_secret` looks — so summarising a request that DID match could
        write the credential itself into a note. Recognised requests are
        already accounted for by `sent_to` and `blocked`, which keep origins
        and no path at all.

        The needle check afterwards can therefore only ever be redundant, which
        is exactly why it is there: this is the one place in the subsystem
        where being wrong once puts the password in front of the model."""
        with contextlib.suppress(Exception):  # a dead request describes nothing
            line = signin_mod.request_summary(request.method, request.url)
            if line and not signin_mod.carries_secret(line, needles):
                watch.note_traffic(line)

    async def decide(route: Any) -> None:
        # The abort and the continue are kept apart deliberately: a failed
        # abort must never fall through to letting the request go.
        carrying = carries(route.request)
        if not carrying:
            note_other_traffic(route.request)
        if carrying and (where := going_astray(route.request)):
            watch.note_blocked(where)
            with contextlib.suppress(Exception):
                await route.abort()
            return
        # Read while the request is still alive; a dead one answers nothing,
        # and the record.origin fallback keeps a send from going unrecorded
        # just because its address could not be read back.
        heading_for = record.origin
        if carrying:
            with contextlib.suppress(Exception):
                heading_for = signin_mod.origin_of(route.request.url) or record.origin
        with contextlib.suppress(Exception):
            await route.continue_()
            # AFTER the continue returned, never before: a `sent_to` written
            # ahead of it would claim the password left on a request that was
            # never released. This is the whole positive signal, and the
            # ordering is what makes it true rather than probable.
            if carrying:
                watch.note_sent(heading_for)

    def note_status(response: Any) -> None:
        """The status the site gave a request that carried the credential.

        A refusal here is the one unambiguous "the site judged the value" there
        is, and it is the only reason left to retire a stored password without
        the page saying so in words."""
        with contextlib.suppress(Exception):  # a response aish cannot read says nothing
            if _carries_the_credential(response.request, needles):
                watch.answered.append(int(response.status))

    with contextlib.suppress(Exception):  # a page that takes no listener is not an error
        page.on("response", note_status)

    await page.route("**/*", decide)
    # LAST, after both observers are attached: `armed` is what licenses every
    # claim made from this watch afterwards, and a watch that is armed while
    # one of its halves is not up would license a claim nothing was watching.
    watch.armed = True


@dataclass
class SignInResult:
    """How an automatic sign-in went. Carries NO credential, by construction."""

    # THE SESSION CAME UP, and nothing weaker. It used to mean "the login page
    # stopped showing a password box", which a *forgot password* button and a
    # show-password toggle both produce with nothing signed in — so a wrong
    # press ended here, counted a use, and pushed "aish signed in". It is now
    # set only where the URL that walled was read afresh and stopped asking.
    ok: bool = False
    # A reason the owner can act on, or "" — never the model's to interpret
    # loosely: it is rendered verbatim above the untrusted-content banner.
    why: str = ""
    # The site asked for a code. Not a failure, and must never be recorded as
    # one: the password was almost certainly right.
    second_factor: bool = False
    # Should the stored credential stop being spent? Only when BOTH halves are
    # true (#320): aish WATCHED the password leave, and the site POSITIVELY
    # said it judged the value. Either alone was a verdict about something
    # nobody observed — see `_sign_in_on`.
    stale: bool = False
    # The anti-automation widget the login page DECLARES — what it loads and
    # says about itself — when the session did not come up. An observation
    # about the page and nothing more: it is spoken as a declaration, and it
    # never selects the outcome or names the cause. It did both once, and the
    # sentence it produced — "reCAPTCHA refused the sign-in" — was said to the
    # owner for weeks about a widget that had never been given anything to
    # refuse (#321).
    captcha: str = ""
    # The form was FILLED and the submit gesture made. What separates "aish
    # got as far as pressing" from "aish stopped before typing" — the two need
    # different sentences, because "aish did not use it" is true of the second
    # and unknowable for the first once the watch may be blind.
    filled: bool = False
    # Was the credential SPENT? A refusal by the harness before the submit and
    # a refusal by the site after it call for opposite things to be said, and
    # "aish did not use it" is a false statement about the second.
    #
    # Set from what was OBSERVED, not from having reached the press: the fence
    # watched the value leave, or the SESSION CAME UP — which is proof it
    # arrived, whatever the fence saw. That second half is why it rests on the
    # confirmed ending and not on the login page moving on: a page that moved
    # on with no session behind it proves nothing about where the value went,
    # so the unconfirmed ending carries only what the fence watched.
    tried: bool = False
    url: str = ""
    # A picture of the page at the END of the attempt, success or failure
    # (#320), in the evidence-frame store — a REFERENCE, never the bytes. The
    # store is outside every workspace root (#318), which matters most here:
    # these are pictures of LOGIN PAGES. Before this
    # the replay took no snapshot at any point, so a sign-in that did not work
    # left the owner nothing to look at. `frame_skipped` says WHICH absence,
    # in the same closed vocabulary `Snapshot` uses.
    frame: str = ""
    frame_skipped: str = ""
    # What the LOGIN PAGE wrote to its own console during the attempt, bounded
    # and in the page's own words. A submit that never fired is a handler that
    # threw, and this is the only channel that carries the sentence saying so.
    #
    # It goes to the OWNER and never to the model, and that is a fence rather
    # than an oversight. The renewal note is aish's own voice ABOVE the
    # untrusted banner, where page-authored text may never be spoken; and
    # putting it below the banner instead would be a NEW INPUT SURFACE — the
    # model already reads the driven page, so that page's console is the same
    # document, but this is a different one it never asked for and cannot see.
    # A record may not become a channel. Same reasoning that keeps the picture
    # below off the browse `frame` key.
    console: list[str] = field(default_factory=list)
    # What the page was seen doing in the submit window, on a FAILED attempt:
    # method, origin and path, bounded, and only for the endings that CLAIM
    # nothing was seen leaving. Evidence about that claim, never a verdict —
    # it is composed into `why` and reaches nothing else.
    requests: list[str] = field(default_factory=list)
    # What was found sitting on top of the SIGN-IN BUTTON, when the press could
    # not land (#321). The sign-in path never asked this question at all, which
    # is why a banner over eon.pl's login button read as a credential replay
    # that mysteriously did nothing — and why four wrong diagnoses were argued
    # from page text over a day. aish's own observation, not the page's account
    # of itself, so it is spoken in aish's voice with the element's own name
    # quoted; and it rides out to the trace beside the picture.
    covered: str = ""


# What aish says when the form came back and nothing carrying the password was
# ever SEEN leaving the page. It is not a diagnosis of the site; it is the
# narrowest true statement aish can make about its own hands, and it is worded
# to what the fence can observe rather than to what happened — "the site never
# saw it" would be wider than the fence enforces, since a service worker or a
# value transformed in the page is invisible to it. The sentence it replaced
# ("the stored one looks stale") was a false statement about the owner's
# password, written to durable storage (#320).
#
# {gesture} names WHICH gesture was made, because the fact-the-gesture-the-
# absence is the whole report and the gesture is where the eon.pl lead was:
# "aish found no submit button" on a page whose login button the owner can
# SEE points straight at the code's own filter, where a cause-shaped sentence
# pointed away from it.
NEVER_SUBMITTED = (
    "aish filled the form, {gesture}, and never saw the password leave the "
    "page — so it could not confirm the form was submitted at all, and "
    "nothing has been learned about the saved sign-in"
)

# The two gestures a replay can make, said as what aish DID. The Enter one
# also says what aish did NOT find, because on a page whose button is
# `<button type="button">` that absence is aish's own doing and the sentence
# is where the owner can catch it.
GESTURE_CLICKED = "pressed the form's submit button"
GESTURE_ENTER = (
    "found no submit button on the form so it pressed Enter in the password "
    "field"
)

# The two observers disagree: the site answered a request that carried the
# password, and the route handler never recorded letting one go. One of them is
# wrong and there is no way to tell which, so this states the disagreement and
# acts on neither half. Saying it out loud matters — the shape it most likely
# has is a send aish cannot see (a service worker), which is exactly the blind
# spot the never-submitted verdict would otherwise hide behind.
CONTRADICTED = (
    "the site answered as though it had judged the saved password, but aish "
    "never saw the password leave the page — those cannot both be true, so "
    "nothing has been recorded about the saved sign-in"
)

# Appended to the observation when the failed page declares an
# anti-automation widget. The declaration is a real observation — the page
# loads the vendor's script and says so — and the owner needs it: it is a lead
# he can verify. What it may never be again is the stated cause. "The login
# page is protected by reCAPTCHA, which refused the sign-in" was said on every
# eon.pl attempt about a widget that was never given anything to refuse,
# because the submit never fired (#321) — a hypothesis in aish's voice,
# standing exactly where the evidence against it should have been. So the
# clause states the declaration, says in the same breath that the declaration
# is all that was observed, and stops.
DECLARED_WIDGET = (
    "; the page declares it is protected by {vendor} — a declaration aish "
    "observed on the page, not something aish saw act on this attempt"
)

# The blind-matcher case (#295): the page plainly sent a body to the site's own
# origin in the submit window, and nothing in it was recognised as carrying the
# password. Worded to what aish OBSERVED, because the honest sentence here is
# narrow: aish does not know the password was sent, and it no
# longer knows that it was not. A service worker, a WebSocket and a value
# hashed in the page are all invisible to the matcher — the blind spots
# `secret_needles` already declares — and this is the first outcome able to
# notice that one of them may have just happened.
SUBMITTED_UNRECOGNISED = (
    "aish filled the form, {gesture}, and the page then sent something "
    "to the site itself that aish did not recognise as carrying the password — "
    "so it cannot tell whether the password was submitted or not, and nothing "
    "has been learned about the saved sign-in"
)


# How many of the kept requests are SPOKEN. The full list rides on
# `SignInResult.requests` and the whole of it is what `unrecognised_submission`
# reads — a lower storage cap would let a busy SPA crowd the login POST out of
# the detection. This is a separate, smaller number because this sentence ends
# up in a Pushover body on his phone, where a dozen addresses is a wall he will
# not read; the count is named so the prose never claims to be the whole list.
_SPOKEN_REQUESTS = 5


def _submit_window(traffic: Sequence[str]) -> str:
    """The bounded account of the submit window, for the owner to read.

    Composed ONLY for the endings that claim an absence — where the password
    was observed leaving, the absence is not being asserted and this would be
    noise. Every clause names aish as the observer, because the fence sees what
    `page.route` routes and nothing else: "the page made no request" would be
    a statement about the site, and this can only ever be a statement about
    what aish saw."""
    if not traffic:
        return " (aish saw the page make no other request at all in the submit window)"
    spoken = list(traffic[:_SPOKEN_REQUESTS])
    rest = len(traffic) - len(spoken)
    more = f", and {rest} more" if rest > 0 else ""
    return (
        " (in the submit window aish saw the page make: "
        + "; ".join(spoken) + more + ")"
    )


# The fence is also the only witness, so a sign-in aish cannot watch is a
# sign-in aish will not make: with no witness there is no honest verdict
# afterwards, and the credential would be spent for nothing.
UNWATCHED = (
    "aish could not watch what the page sends, so it did not type the saved "
    "sign-in at all — the saved sign-in is untouched"
)

# A REASON CLAUSE for the ending #321 adds: the press never reached the button
# because something was on top of it, so the form was never submitted and the
# site was never asked anything. It is dropped into the HELD notes, which
# already say the saved sign-in is untouched and already name `/browser
# <host>`, so it states the reason and stops — the discipline `why` follows
# everywhere else here.
#
# The element is named because a refusal that names nothing teaches nothing:
# "aish could not press the button" is what the old ladder effectively said,
# and it is what a full day of wrong diagnosis was argued on top of. It is the
# page's own word for the element, so it is quoted and attributed.
COVERED_SUBMIT = (
    "something the page calls {by!r} was sitting on top of the sign-in button, "
    "so the press landed on that instead and the form was never submitted — a "
    "cookie or consent banner is the usual cause. The saved password never "
    "left the page, so nothing has been learned about it"
)


# The attempt ended on a page with no password box on it, and that is NOT a
# session. Two ordinary shapes reach here with nothing signed in:
#
#   THE PRESS NAVIGATED AWAY — *forgot password*, *create account*. The new
#     page has no password field, so the old test ended the attempt `ok=True`,
#     counted a use, and pushed "aish signed in" for a form that was never
#     submitted.
#   THE PRESS WAS A SHOW-PASSWORD TOGGLE — the field flips to `type="text"`,
#     so `input[type=password]` finds nothing on the very page that is still
#     sitting there asking for the password.
#
# So `ok` is earned by the SESSION, not by the login page moving on: the URL
# whose wall triggered this renewal is read again, afresh, and has to stop
# asking. Where no session exists the fresh read walls, and this is what the
# owner is told. Deliberately NOT gated on the fence having seen the credential
# leave — that would report a working sign-in as unconfirmed on every site
# inside the fence's declared blind spots (a service worker, a WebSocket, a
# value hashed in the page), which is the #296 failure by another name.
MOVED_ON_NO_SESSION = (
    "the page moved on, but the session did not come up — the page that was "
    "asking for a password is still asking for one when it is read again, so "
    "nothing has been learned about the saved sign-in"
)

# The same ending where the fresh read could not answer at all: the navigation
# failed, or the page would not say whether it holds a password box. aish saw
# no session and it also saw no wall, so it claims neither. "I do not know why"
# is an ordinary outcome here and gets ordinary words; inventing a cause is
# what this whole area is being repaired from.
COULD_NOT_CONFIRM = (
    "aish could not tell whether the session came up — the page that was "
    "asking for a password did not answer when it was read again, so nothing "
    "has been learned about the saved sign-in"
)


async def _signin_frame(owner: _Owner, page: Any) -> tuple[str, str]:
    """(stored path, reason there is none) for a picture of a sign-in attempt.

    The owner's oldest complaint about this feature is that it is invisible:
    the replay took **no snapshot at any point**, so when it failed there was
    nothing to look at, and two wrong diagnoses were argued from the page text
    alone. Same store, same reference discipline and same absence vocabulary as
    the browse frame — the bytes never enter a log, only a path does.

    **The masking is the whole difference from `_evidence_frame`.** This is the
    one page in aish that has had the owner's password typed into it, and while
    a password box renders as dots, a *field* is not obliged to still be a
    password box: a show-password toggle flips `type` to `text`. So the field
    aish tagged is forced back to `type="password"` first, and the shutter is
    refused if that could not be confirmed. That refusal is the mirror image of
    the one #320 removed from the browse path, and the difference is the point:
    there, aish had typed nothing and the unknown protected nothing; here, the
    unknown is about a document holding his credential.

    The field is masked rather than EMPTIED so the picture can still answer
    *was the password ever filled in* — dots for a filled field, nothing for an
    empty one. That question is why the owner asked for the picture.
    """
    try:
        masked = await page.evaluate(SIGNIN_MASK_JS)
    except Exception:  # noqa: BLE001 — a page that will not answer is an unknown
        masked = None
    if not (masked or {}).get("ok"):
        return "", browse_mod.NO_FRAME_FAILED
    return await _evidence_frame(owner, page, label="sign-in")


async def _press_the_button_again(
    page: Any, record: Any, watch: _CredentialWatch, *, was: str
) -> None:
    """A submit that did not fire gets ONE fallback gesture (#320).

    **This is not a second attempt at the credential — it is a second attempt
    at the BUTTON, and the difference is the whole justification.** The next
    reader will otherwise take it for a breach of *one attempt, never a retry*.
    That rule exists because a wrong password tried twice ticks the site's
    lockout counter; it is about the VALUE reaching the site. Here the fence
    has routed every request this page made and saw NONE of them carrying the
    password, so as far as anything can be observed the site has not seen the
    credential once and this is the first attempt at delivering it rather than
    the second. The value is already typed; only the gesture is repeated. (The
    fence's blind spots are the ones `secret_needles` already declares — a
    service worker, a WebSocket, a value transformed in the page — and on such
    a site this can be a genuine second delivery. It is bounded to one.)

    The lead this exists for is in the owner's own session logs, on this exact
    site: *"the click would not land, so aish pressed it with the keyboard, and
    nothing about that control or the address changed afterwards, so it may not
    have been registered."* Clicks not landing is documented eon.pl behaviour,
    and `element.click()` had no fallback and no verification that anything was
    sent.

    Five conditions, and every one of them is a way of being sure a first
    submission is not in flight — a double submission is the one thing this may
    not risk:

    1. **The submit was a CLICK.** With no form the submit already WAS Enter,
       and pressing it again is a repeat with no new information. Enforced by
       the caller, which only calls this when it pressed a button.

       **And where it IS a click, this gesture is worth less than it looks
       (#321).** Enter in the field submits the form the field belongs to — so
       it is a second, different route only where a form submission is what the
       button would have caused. A `<button type="button" onclick="…">` has no
       form submission behind it at ALL, and eon.pl's login button is exactly
       that: there, Enter in the field is not a weaker second attempt, it is a
       no-op. It costs nothing and is kept for the ordinary case; what actually
       stops that page is the covered report below, not this.
    2. **Nothing carrying the password has been let through, and nothing has
       come BACK.** The route handler records a send before `continue_`
       returns, so a request already on the wire is already in `sent_to` and an
       empty list is not a race. `answered` is the same guard from the other
       end and is not redundant with it: a page that got an answer to a
       credential-bearing request has plainly submitted, however that request
       left, so it closes the one gap `sent_to` has (#320).
    3. **Nothing UNRECOGNISED went to the site either** (#295). The submit
       window is open by the time the first press lands, so a body sent where
       the owner's own sign-in sends the password is visible here — and a page
       that posted to its own login endpoint has submitted, whether or not the
       matcher could read it. It covers condition 2's blind spot from the only
       other angle available. It can only ever suppress a press, never cause
       one, so it cannot make this riskier; and the eon.pl case this whole
       fallback exists for is untouched, because there the click does not land
       and the window is empty.
    4. **The page has not navigated.** A page that moved has acted on
       something, whatever the fence did or did not see.
    5. **It is still the page that was checked**, by the same fence the press
       itself asked — the tagged field present, the origin and the form's
       destination unchanged. A page that changed under us gets nothing.

    Once, by construction: straight-line code with no loop and one caller.
    """
    if watch.was_tried or watch.answered or not watch.armed:
        return
    if signin_mod.unrecognised_submission(
        watch.traffic, [record.origin, *record.destinations]
    ):
        return
    try:
        if str(page.url or "") != was:
            return
        if await page.evaluate(SIGNIN_STILL_OURS_JS, record.origin):
            return
        field = await page.query_selector("[data-aish-signin='password']")
        if field is None:
            return
        # Focus the FIELD, not the button: the button already has focus from
        # the click that did not land, so Enter there would repeat the same
        # gesture. Enter in the field is the other way A FORM is submitted —
        # which is the whole reach of this fallback, and narrower than the
        # sentence that used to stand here (#321). Where the button is a
        # `<button type="button" onclick="…">` there is no form submission
        # behind it, so this is a no-op rather than a second attempt: it is
        # right for an ordinary login form and it can never rescue that one.
        # The failure it CANNOT reach is the one the covered report names.
        await field.focus()
        await page.keyboard.press("Enter")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — an SPA sign-in never navigates
            pass
        await page.wait_for_timeout(SETTLE_MS)
    except Exception:  # noqa: BLE001 — a fallback that failed leaves the ending alone
        return


async def _sign_in_on(
    page: Any,
    record: Any,
    identifier: str,
    password: str,
    watch: _CredentialWatch,
    *,
    session_is_up: Callable[[], Awaitable[bool | None]],
) -> SignInResult:
    """Fill and submit the recorded login form on an already-open page.

    `session_is_up` is the only thing that may end this `ok`: it reads the URL
    that walled, afresh, and answers True (no password asked for), False (still
    walled) or None (it could not tell). It is handed in rather than done here
    because it needs a page of its OWN — this function has one page, the login
    page, and a login page that moved on says nothing about a session.

    `watch` is the fence's live account of the credential on the wire, and it
    is what makes the ending honest (#320). A password box on the screen
    afterwards is equally true of a rejected password, a submit that never
    fired, a bot wall, a page that has not navigated yet and a second-factor
    step that keeps the field on screen — so it decides nothing on its own.

    It carries both halves of the verdict: whether the value ever LEFT
    (`was_tried`) and what the site ANSWERED the requests that carried it
    with (`answered`). Neither alone retires a credential."""
    refused = (
        f"aish only ever types a credential at {record.origin}, the exact origin "
        "it was saved for"
    )
    if not watch.armed:
        # Before anything is typed. An unwatched sign-in cannot be judged
        # afterwards — the form coming back would be unattributable — and a
        # credential spent for an unattributable outcome is spent for nothing.
        return SignInResult(why=UNWATCHED, url=page.url or "")
    live = signin_mod.origin_of(page.url or "")
    if live != record.origin:
        # A cheap early out. It is NOT the fence — the fence is inside the
        # evaluate below, which checks and tags in one step.
        return SignInResult(
            why=f"the sign-in page went to {live or 'somewhere else'} — {refused}",
            url=page.url or "",
        )
    try:
        found = await page.evaluate(SIGNIN_FORM_JS, record.origin)
    except Exception as exc:  # noqa: BLE001 — a page that will not answer
        return SignInResult(why=f"could not read the sign-in form ({exc})", url=page.url)
    if not found.get("ok"):
        return SignInResult(
            why=f"{found.get('why', 'no usable sign-in form')} — {refused}",
            url=page.url,
        )
    # REAL keystrokes, for the reason `view_act` uses them: a site that listens
    # for key events (and a great many login forms do) sees nothing from fill().
    # The identifier is skipped when the page has no field for it — a "welcome
    # back, enter your password" page is a sign-in, not a broken one.
    steps = []
    if found.get("identifier") and identifier:
        steps.append(("[data-aish-signin='identifier']", identifier))
    steps.append(("[data-aish-signin='password']", password))
    for selector, value in steps:
        element = await page.query_selector(selector)
        if element is None:
            return SignInResult(
                why=f"the sign-in page changed while it was being filled — {refused}",
                url=page.url,
            )
        await element.click(timeout=ACT_TIMEOUT_MS)
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.type(value, delay=12)

    # Nothing has been SENT yet — typing commits nothing. So the last thing
    # before the press is to ask the live page whether it is still the page
    # that was checked. A refusal here costs an unsent form; not asking costs
    # the credential.
    try:
        changed = await page.evaluate(SIGNIN_STILL_OURS_JS, record.origin)
    except Exception as exc:  # noqa: BLE001
        changed = f"the page stopped answering ({exc})"
    if changed:
        return SignInResult(
            why=f"{changed} while the form was being filled — {refused}, and "
            "nothing was sent",
            url=page.url,
        )

    # Read BEFORE the press, so the comparison after it is about the submit and
    # nothing else: a login page that always carries an alert region — a cookie
    # notice, a maintenance banner — would otherwise read as a rejection the
    # first time it was looked at.
    before = await _rejection_marks(page)

    # Only a control `SIGNIN_FORM_JS` tagged is ever pressed, and it tags one
    # only inside a form: a genuine submit control, or — where the form has
    # none — its single visible button, counted and never read. With no form,
    # or with more than one button to choose between, the submit is Enter,
    # which is the gesture that cannot land on "Continue with Google".
    submit = await page.query_selector("[data-aish-signin='submit']")
    was = page.url
    # The SUBMIT WINDOW opens here and closes once the page has settled, so
    # what is kept is the account of one gesture and not of a browsing session
    # (#295). It is opened before the press rather than after, because the
    # request the whole thing exists to notice is the one the press makes.
    watch.watching_traffic = True
    if submit is not None:
        try:
            await submit.click(timeout=ACT_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — a press that would not land
            # **The press did not reach the button, and asking WHY is the whole
            # of #321.** This path never asked: `_uncover` existed one screen
            # over on the browse ladder and the credential replay did not call
            # it, so a consent banner over eon.pl's login button surfaced as a
            # sign-in that filled the form and then did nothing, with nothing
            # anywhere naming the cause. Four confident diagnoses were argued
            # on top of that silence and all four were wrong.
            #
            # A covered control is a REPORTED FAILURE and never a licence to
            # press something else: the only thing tried again is the same
            # button, and only after the consent list has actually cleared it.
            cover = await _uncover(page, submit)
            if cover.by and not cover.dismissed:
                watch.watching_traffic = False
                return SignInResult(
                    why=COVERED_SUBMIT.format(by=cover.by),
                    covered=cover.by,
                    filled=True,
                    url=page.url,
                )
            # Either nothing was covering it — in which case this failed for a
            # reason aish cannot name and the endings below say exactly that —
            # or the cover came down and the same button gets its one retry.
            with contextlib.suppress(Exception):
                await submit.click(timeout=ACT_TIMEOUT_MS)
    else:
        await page.keyboard.press("Enter")
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — an SPA sign-in never navigates
        pass
    await page.wait_for_timeout(SETTLE_MS)

    if submit is not None:
        await _press_the_button_again(page, record, watch, was=was)
    watch.watching_traffic = False

    if await _has_password_field(page):
        # The form came back. That means THE SESSION DID NOT COME UP, and
        # nothing more (#320) — it is equally the shape of a CAPTCHA refusing
        # the automation, a submit that never fired, a bot wall and an SPA that
        # had not navigated yet. Reading it as "the site refused the value" is
        # what wrote a false statement about the owner's password onto durable
        # storage and destroyed a working credential, twice.
        #
        # Two independent observations decide what happened, and the
        # composition of them lives in `signin.judge_a_failed_sign_in` so that
        # nobody can quietly collapse it back to one test:
        #
        #   DID IT LEAVE   — the fence watched every request this page made and
        #                    knows which of them carried the value.
        #   DID THE SITE JUDGE IT — a refusal status on one of those requests,
        #                    or the page's own machine-readable error.
        #
        # A credential is retired only when BOTH are true.
        after = await _rejection_marks(page)
        vendor = await _captcha_vendor(page)
        # Three observations of THE CREDENTIAL plus the page's widget
        # declaration, which can only disqualify one of them — and the
        # submit-window traffic is deliberately not among any of it (#295).
        # Letting unrelated traffic decide whether a sign-in worked is the
        # #296 bug. Its only reach into this function is the WORDING chosen
        # below, on the endings that claim an absence; the verdict itself is
        # decided without it.
        said = _said_no(before, after)
        verdict = signin_mod.judge_a_failed_sign_in(
            sent=watch.was_tried,
            refused_status=watch.was_refused,
            said_no=said,
            captcha=vendor,
        )
        # Asked only when the fence saw nothing, because it is a statement
        # about that silence and nothing else: the page sent a body where the
        # owner's own sign-in sends the password, and none of it matched. It
        # sets no field on the result — not `stale`, not `tried`.
        unrecognised = not watch.was_tried and signin_mod.unrecognised_submission(
            watch.traffic, [record.origin, *record.destinations]
        )
        if verdict == signin_mod.FAILED_REFUSED:
            # Both halves. The value went out and something said no to it —
            # one attempt, never a retry, because retrying is how an account
            # locks.
            return SignInResult(
                why="the site refused the saved password, so aish will not try it again",
                stale=True,
                tried=True,
                filled=True,
                url=page.url,
            )
        if verdict == signin_mod.FAILED_CONTRADICTION:
            # The two observers disagree, and a disagreement is not a verdict.
            # `tried` is True because the answering half is the one that saw
            # something arrive: a request that got an answer was sent, however
            # it left. Nothing is written to the record either way.
            return SignInResult(why=CONTRADICTED, tried=True, filled=True, url=page.url)
        # The widget the page declares is APPENDED to the observation on the
        # two endings that end without a verdict, never made into one. There
        # used to be a FAILED_CAPTCHA verdict standing right here, selected by
        # a script tag, and its sentence — "the login page is protected by
        # reCAPTCHA, which refused the sign-in" — was a hypothesis in aish's
        # own voice: on eon.pl the widget was never given anything to refuse,
        # because the submit never fired (#321). Worse, the confident cause
        # replaced the observations (nothing left the page; the site said
        # nothing) that would have pointed at the true one. The declaration is
        # still worth saying — it is a real page fact and a lead the owner can
        # verify — so it rides along as exactly that, in `DECLARED_WIDGET`'s
        # words, which state its own evidentiary status.
        declared = DECLARED_WIDGET.format(vendor=vendor) if vendor else ""
        if verdict == signin_mod.FAILED_NEVER_SENT:
            # The verdict is the same either way — nothing carrying the
            # password was recognised leaving, so the credential is untouched.
            # What changes is the sentence: a page that sent NOTHING and a page
            # that sent something aish could not read demand opposite next
            # steps, and until now both read as "never sent".
            gesture = GESTURE_CLICKED if submit is not None else GESTURE_ENTER
            return SignInResult(
                why=(SUBMITTED_UNRECOGNISED if unrecognised else NEVER_SUBMITTED)
                .format(gesture=gesture)
                + _submit_window(watch.traffic)
                + declared,
                captcha=vendor,
                filled=True,
                requests=list(watch.traffic),
                url=page.url,
            )
        # FAILED_UNEXPLAINED: it left, and nothing aish trusts judged it.
        # Saying "no reason given" plainly — rather than reaching for the most
        # plausible cause in view — is what keeps the blind spot visible: if
        # this ending is common somewhere, the instrumentation needs to say
        # more, not the sentence. Two wordings, because the observations
        # differ: `said` can be true here only on a widget-declaring page
        # (anywhere else it is a refusal), and there a fresh error message is
        # a real observation that "gave no reason" would erase — but not one
        # aish can read as being about the password, and the sentence says
        # both halves rather than picking one.
        if said:
            why = (
                "the sign-in did not go through — the page showed a new "
                "message after the submit, but aish cannot tell whether it "
                "was about the password, so the saved password was not judged "
                "and is untouched"
            )
        else:
            why = (
                "the sign-in did not go through and the site gave no reason for "
                "it — the saved password was never judged and is untouched"
            )
        return SignInResult(
            why=why + declared,
            captcha=vendor,
            tried=True,
            filled=True,
            url=page.url,
        )
    try:
        wants_code = bool(await page.evaluate(SECOND_FACTOR_JS))
    except Exception:  # noqa: BLE001
        wants_code = False
    if wants_code:
        return SignInResult(second_factor=True, tried=True, filled=True, url=page.url)

    # The page moved past the password. That is NOT the session coming up, and
    # treating it as such is what a wrong press turns into a false success:
    # *forgot password* navigates to a page with no password box, and a
    # show-password toggle leaves the same page with none either. So the claim
    # is checked where it is actually about — the URL whose wall triggered this
    # renewal, read AFRESH. No session, no unwalled read.
    #
    # `tried` follows the same evidence rule it always did, and only the
    # CONFIRMED ending may set it unconditionally: "the site advanced past the
    # password" is proof the credential arrived only once something is known to
    # be signed in. Unconfirmed, all that is known is what the fence watched.
    confirmed = await session_is_up()
    if confirmed is True:
        return SignInResult(ok=True, tried=True, filled=True, url=page.url)
    return SignInResult(
        why=MOVED_ON_NO_SESSION if confirmed is False else COULD_NOT_CONFIRM,
        tried=watch.was_tried,
        filled=True,
        url=page.url,
    )


async def _the_session_is_up(owner: _Owner, url: str) -> bool | None:
    """Read `url` on a FRESH page: True = it no longer asks for a password.

    The one question a sign-in's `ok` is allowed to rest on, asked where it is
    actually about. `url` is the address whose wall triggered the renewal — the
    model chose which URL triggers one, and it still chooses nothing about
    where the credential goes, so this adds no reach: it is the very read the
    caller is about to make anyway, made one moment earlier and thrown away.

    A page of its own, deliberately. The login page is where the wrong press
    landed, so anything read off it is a statement about that press; a new page
    at the walled URL carries the profile's cookies and nothing else, and if no
    session was established it walls exactly as it did before the attempt. It
    is also the only reader here with no blind spot to declare: a page that
    stopped answering after a press cannot make this one answer wrongly,
    because this one is a different document.

    Three values, and the third is not folded into the second: `None` is *aish
    could not tell*, which supports no claim in either direction and gets its
    own sentence at the ending. Resolving it to False would put the words "the
    session did not come up" in front of the owner on the evidence of a
    navigation timeout.

    The fence is NOT on this page and does not need to be: nothing is typed
    here, so there is no credential to fence.
    """
    try:
        context = await owner.context()
        page = await context.new_page()
    except Exception:  # noqa: BLE001 — a browser that died is not a wall either
        # And never a raise: this runs after the attempt, so throwing here
        # would cost the outcome, the picture and the console that go with it.
        return None
    # A page a read OWNS, like the login page beside it: a browse session must
    # not be able to adopt this tab, and its downloads are nobody's chat's.
    owner.read_pages.add(page)
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(SETTLE_MS)
        except Exception:  # noqa: BLE001 — a read that did not happen is not a verdict
            return None
        asks = await _password_field_state(page)
        return None if asks is None else not asks
    finally:
        owner.read_pages.discard(page)
        with contextlib.suppress(Exception):
            await page.close()


async def _captcha_vendor(page: Any) -> str:
    """The anti-automation widget this live page declares, or "".

    Structural on purpose: what the page LOADS and the class names it carries,
    plus the brand name in its own text. eon.pl declares reCAPTCHA in Polish,
    and the owner's pages are frequently not in English, so nothing here reads
    prose — `signin.captcha_vendor` owns the vocabulary and matches only tokens
    that survive translation."""
    marks: list[str] = []
    text = ""
    with contextlib.suppress(Exception):  # a page that will not answer declares nothing
        marks = [str(mark) for mark in (await page.evaluate(CAPTCHA_MARKS_JS)) or []]
    with contextlib.suppress(Exception):
        text = str((await page.evaluate(PAGE_TEXT_JS)) or "")
    return signin_mod.captcha_vendor(marks, text)


async def _rejection_marks(page: Any) -> Any:
    """The page's machine-readable error state, or None when it will not say."""
    try:
        return await page.evaluate(SIGNIN_REJECTION_JS)
    except Exception:  # noqa: BLE001 — silence is not a rejection
        return None


def _said_no(before: Any, after: Any) -> bool:
    """Did the page declare, in answer to the submit, that it refused the value?

    Only what CHANGED counts. An alert region that was already showing was not
    provoked by the credential, and a page that will not answer either read
    says nothing at all — the safe direction, because the cost of a wrong yes
    is a working credential the owner has to re-record."""
    if not isinstance(after, dict):
        return False
    was = before if isinstance(before, dict) else {}
    if after.get("invalid") and not was.get("invalid"):
        return True
    already = {str(said) for said in was.get("said") or []}
    return any(str(said) not in already for said in after.get("said") or [])


def _signin_lines() -> list[str]:
    """The sign-ins aish can re-establish, for `/browser`.

    What it reports is what the store actually OWNS. It deliberately does not
    claim whether he is signed in right NOW: cookie presence is not session
    validity — eon.pl expires server-side in about fifteen minutes — and
    actually proving it would cost a Chrome launch and a navigation per row on
    a box that evicts the browser for memory. A row that lies is the failure
    #236 was about; a row that says less is not.

    `used Nx` therefore says what it counts and no more: sign-ins whose
    SESSION was seen to come up, since that is what `note_used` is now called
    on. It used to include attempts where a wrong button merely moved the
    login page along.

    Since #320 it also names the PICTURE of the last attempt, which is this
    subsystem's only door onto a sign-in that did not work: the replay runs
    unattended, on a Mac the owner is never sitting at, and until now it left
    nothing to look at."""
    records = signin_mod.records()
    if not records:
        return []
    lines = ["", "aish can sign in to:"]
    for record in records:
        host = host_of(record.origin)
        used = (
            f"used {record.used}x, last {record.last_used}"
            if record.used
            else "never used"
        )
        note = f" — NOT WORKING: {record.suspect}" if record.suspect else ""
        lines.append(f"  {host}  saved {record.saved} - {used}{note}")
        if picture := _last_attempt_line(record):
            lines.append(f"    {picture}")
    return lines


def _last_attempt_line(record: Any) -> str:
    """What to say about the picture of the last attempt, or "".

    The frame store is a bounded LRU, so a path that no longer resolves is the
    ORDINARY end of a frame's life and says **purged** — never "there was never
    a picture". Same three states `aish explain` reports for a browse frame,
    and for the same reason: absence has to say which absence."""
    if record.last_frame:
        try:
            there = Path(record.last_frame).exists()
        except OSError:  # an unreadable path is not a claim either way
            there = False
        return (
            f"picture of the last attempt: {record.last_frame}"
            if there
            else "picture of the last attempt: purged from the frame store"
        )
    if record.last_frame_skipped:
        return f"no picture of the last attempt — {record.last_frame_skipped}"
    return ""


def _hold_credential(owner: Any, url: str, password: str, remember: object) -> None:
    """Hold what he just typed into a password field, if he asked us to.

    **Consent is STICKY for the origin, for the life of this view**, and that
    is not laziness — it is the retry. A first attempt with the box ticked
    fails, the editor reopens with the box back at its default OFF, he types
    the correct password, and it would be dropped: the one attempt that
    actually worked is the one aish forgot to keep. Sticky consent makes the
    checkbox a statement about this sign-in rather than about one keystroke.

    Nothing is written here. `_save_held_credential` is the only writer, and it
    runs only once the sign-in has been seen to work."""
    origin = signin_mod.origin_of(url)
    if not origin:
        owner.pending_credential = {}
        return
    held = owner.pending_credential
    if held.get("origin") != origin:
        held = {"origin": origin, "identifier": "", "remember": False}
    held["url"] = url
    held["password"] = password
    if remember:
        held["remember"] = True
    owner.pending_credential = held
    if held.get("remember"):
        _watch_where_the_credential_goes(owner)


def _watch_where_the_credential_goes(owner: Any) -> None:
    """Record the origins his OWN sign-in sends the password to (#296).

    The replay fence needs to know where the credential legitimately goes, and
    the only authority on that is the sign-in that worked — a login page may
    post to an API subdomain or a central identity host, and nothing the page
    declares about itself is obliged to say so. So it is watched, here, at the
    one moment aish can see it happen.

    Armed only once he has ticked the box, and it reads `pending_credential`
    on every request rather than closing over the value, so clearing that
    dict — which `_save_held_credential` does — disarms it. Nothing is written
    or logged: the origins ride along with the value already being held, and
    only the origins are ever persisted."""
    page = owner.view
    if page is None or owner.credential_watch is page:
        return

    def note(request: Any) -> None:
        held = owner.pending_credential
        secret = held.get("password") or ""
        if not held.get("remember") or not secret:
            return
        if not _carries_the_credential(request, signin_mod.secret_needles(secret)):
            return
        origin = signin_mod.origin_of(request.url)
        seen = held.setdefault("destinations", [])
        if origin and origin not in seen:
            seen.append(origin)

    with contextlib.suppress(Exception):  # a watcher that will not attach is not an error
        page.on("request", note)
        owner.credential_watch = page


def _hold_identifier(owner: Any, url: str, value: str) -> None:
    """Remember the last ordinary field he filled at this origin.

    A password alone re-establishes nothing — the form wants an identifier, and
    in a two-step flow it was typed on a page that no longer exists by the time
    the password is. Reading it back is allowed where reading a password is
    not: the frame already ships everything visible, so an ordinary field's
    value is not new exposure."""
    origin = signin_mod.origin_of(url)
    if not origin or not value.strip():
        return
    held = owner.pending_credential
    if held.get("origin") != origin:
        held = {"origin": origin, "remember": False}
    held["identifier"] = value
    owner.pending_credential = held


def _save_held_credential(owner: Any) -> str:
    """Write the held sign-in, now that it has been seen to work. Returns the
    origin saved, or "" — never raises into a frame."""
    held, owner.pending_credential = owner.pending_credential, {}
    if not held.get("remember") or not held.get("password"):
        return ""
    try:
        record = signin_mod.save(
            held.get("url", ""),
            held.get("identifier", ""),
            held["password"],
            today=time.strftime("%Y-%m-%d"),
            destinations=held.get("destinations", []),
        )
    except Exception:  # noqa: BLE001 — a failed save must not cost him the frame
        return ""
    return record.origin


def sign_in(url: str, *, timeout: float = 120.0) -> SignInResult | None:
    """Re-establish the owner's session at this URL's origin, or None when
    there is nothing stored for it.

    Never called by the model, and it takes no model-supplied argument beyond
    the URL that was already being read. The page it drives is the one the
    OWNER recorded, not the one that was asked for.

    **`url` is also what the outcome is judged against.** A sign-in's outcome
    is whether the session came up (#296), so the ending that claims one reads
    this URL again on a fresh page and requires that it has stopped asking for
    a password — see `_the_session_is_up`. Every consumer of `ok` therefore
    inherits one honest claim rather than repeating the test: `note_used`, the
    push, both renewal notes in `web.py` and the `/browser` listing's counter.
    """
    record = signin_mod.find(url)
    if record is None:
        return None
    pair = signin_mod.credential(record.origin)
    if pair is None:
        # The record says WHY it was marked, so that is what is said — "was
        # not accepted last time" was this line's own words for every suspect
        # record, including ones marked for reasons that were not the site
        # refusing anything (a blocked destination, for one). And a record
        # that is not suspect at all reaches here only when the Keychain
        # answered nothing, which is its own fact and not a refusal by anyone.
        marked = record.suspect or (
            "aish could not read the saved password back from the Keychain"
        )
        return SignInResult(
            why=(
                f"the saved sign-in for {record.origin} is not being tried again — "
                f"{marked}. Sign in at /browser {host_of(url)} and it "
                "will be saved afresh"
            )
        )
    identifier, password = pair
    # Held out here so the outcome can be recorded after the job returns. What
    # it BLOCKED is deliberately NOT consulted for whether the sign-in worked
    # (see `_record_the_outcome`); what it let THROUGH and what the site
    # ANSWERED are the two halves of whether the password may be called stale
    # at all (#320).
    watch = _CredentialWatch()
    # Cleared at the TOP, not only written at the bottom — the same reason
    # `_Seen.start_call` clears the browse frame (#320). An attempt that never
    # returns (a navigation timeout, a browser that died) never reaches
    # `_record_the_outcome`, and a record still holding the last picture it
    # managed to take would present an older page as this attempt's.
    signin_mod.note_frame(record.origin, path="", skipped="")

    async def job(owner: _Owner) -> SignInResult:
        if owner.view is not None:
            # No page is ever opened here, so the absence is named for the
            # reason it would have been named at the shutter.
            return SignInResult(
                why=DRIVEN_BY_HAND, frame_skipped=browse_mod.NO_FRAME_HANDS
            )
        context = await owner.context()
        page = await context.new_page()
        # Armed BEFORE the navigation, so a script that throws while the login
        # page is still loading is caught too — on this flow that is the most
        # likely place for it, since the widget the form depends on is what
        # fails to arrive.
        console = browse_mod.ConsoleLog()
        _watch_console(page, console)
        owner.read_pages.add(page)
        try:
            await page.goto(record.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(SETTLE_MS)
            await _dismiss_consent(page)
            # Armed BEFORE anything is typed and left up through the submit and
            # the settle, so a delayed exfiltration is caught too.
            await _fence_the_origin(page, record, password, watch)
            # The confirmation is a page of its own at the URL that walled, so
            # it is handed in as a capability rather than done inside: opening
            # it costs one navigation and is spent ONLY on the ending that
            # would otherwise claim a session, never on a failure.
            outcome = await _sign_in_on(
                page, record, identifier, password, watch,
                session_is_up=partial(_the_session_is_up, owner, url),
            )
            # Photographed HERE and not inside `_sign_in_on` (#320): that
            # function returns from a dozen places, and a capture per return
            # is a capture somebody forgets on the thirteenth. One shutter,
            # after every ending, on the page as it actually finished — and
            # before the `finally` below closes it.
            outcome.frame, outcome.frame_skipped = await _signin_frame(owner, page)
            # Drained beside the shutter and for the same reason: one place,
            # after whichever of a dozen endings happened, rather than a
            # capture per return that somebody forgets on the thirteenth.
            outcome.console = console.drain()
            return outcome
        finally:
            owner.read_pages.discard(page)
            with contextlib.suppress(Exception):
                await page.close()

    result = _submit(job, timeout)
    incident = _record_the_outcome(
        record, result, watch, when=time.strftime("%Y-%m-%dT%H:%M")
    )
    _announce(record, result, incident=incident)
    return result


def _record_the_outcome(
    record: Any, result: SignInResult, watch: _CredentialWatch, *, when: str
) -> str:
    """Write what happened to the store, and return the incident text, if any.

    **The sign-in's outcome is whether the session came up, and nothing else**
    (#296). What the fence saw on the wire used to decide it, so a blocked
    tracking beacon reported a working sign-in as a failure and sent the owner
    off to do by hand a thing that was already done. That link is severed: the
    result travels back untouched.

    An incident is a fact about the CREDENTIAL, so it lands where facts about
    the credential land — the record is marked suspect, which is what stops the
    value being spent again, and he is pushed the address it was headed for.
    That happens whether or not the session came up, and it takes precedence
    over `note_used`, which would clear the very mark being set.

    **A password box is not a verdict** (#320). `result.stale` arrives set only
    when the fence WATCHED the password go out AND the site positively said it
    judged the value, so that is what gets written here. A submit that never
    fired, a press something was COVERING (#321), and one that simply did not
    get in all
    leave the record completely alone: nothing was learned about the password,
    and marking it destroys a credential whose only repair — re-recording it —
    fixes nothing that was observed to be wrong. This function does not
    re-derive the verdict; it writes what it is handed.

    The picture of the attempt is pointed at FIRST and unconditionally, so an
    ending that returns early below still replaces whatever the previous
    attempt left behind."""
    signin_mod.note_frame(
        record.origin, path=result.frame, skipped=result.frame_skipped
    )
    if watch.blocked:
        text = (
            f"the page tried to send the saved password to {', '.join(watch.blocked)}, "
            "which is not where it goes when he signs in himself"
        )
        signin_mod.note_failed(record.origin, why=text)
        return text
    if result.ok:
        signin_mod.note_used(record.origin, when=when)
    elif result.stale:
        signin_mod.note_failed(record.origin, why=result.why)
    return ""


def _announce(record: Any, result: SignInResult, *, incident: str = "") -> None:
    """Tell him aish used his credential — on the channel that needs no answer.

    A NOTICE, not a card. He has said he will not read a card per action, and
    he is right that a card tapped blind is worse than none; a push demands no
    decision and cannot be tapped through by accident. It is also rare by
    construction — once per lapse, not once per act. Never raises: a renewal
    must not fail because a phone is unreachable."""
    host = host_of(record.origin)
    if incident:
        # The loudest thing that can happen here, and it outranks whether the
        # session came up: the credential is retired either way, so the message
        # he needs is the one that tells him to replace it.
        title = f"aish held your {host} password back"
        body = (
            f"{incident}. aish stopped that request"
            + (" — the session did come up" if result.ok else "")
            + f". Sign in at /browser {host} to save it afresh"
        )
    elif result.ok:
        title, body = f"aish signed in to {host}", "the session had lapsed"
    elif result.second_factor:
        title, body = f"{host} wants a code", "aish got as far as the second factor"
    else:
        # Every failure is pushed as the OBSERVATION `why` carries, plus the
        # one step that is his either way. A dedicated CAPTCHA push used to
        # sit here titled "{host} refuses an automatic sign-in" — a claim
        # about the site that nothing checked, sent to his phone on every
        # eon.pl attempt while the actual failure was a submit that never
        # fired (#321). A cause aish verified (the 401 ending) arrives here
        # too, inside `why`, because there the code checked it.
        title = f"aish could not sign in to {host}"
        body = f"{result.why} — open /browser {host} to sign in yourself"
    if result.frame and not result.ok:
        # Appended AFTER the branches, not inside one: the picture exists for
        # every ending, and the endings that most need looking at are exactly
        # the ones somebody would forget to add it to — the CAPTCHA push was
        # written without it. It names the DOOR rather than the path, because a
        # filesystem path on a phone is nothing he can act on (#320).
        body += " — there is a picture of the attempt; run /browser to find it"
    with contextlib.suppress(Exception):
        notify.pushover(title, body)


# -------------------------------------------------------------- the reads

def read_cold(url: str, *, timeout: float = 60.0) -> Page:
    """Render `url` as NOBODY: the search profile, signed into nothing.

    The identity is the point, not an optimisation. `_login_gate` exists to stop
    a model-proposed URL being read with the owner's session attached, and this
    is how a results page is read without ever raising that question — see
    `search_profile_dir()`.

    Measured 2026-08-21, and the cost is real: same IP, same minute, the signed-in
    profile got 200 and full results while this one got 429 and `/sorry`. Google
    scores the IDENTITY, and reading as nobody is the identity it likes least. So
    a wall here is an ordinary outcome rather than a surprise, and the caller is
    expected to have somewhere to fall back to."""
    return read(url, timeout=timeout, cold=True)


def read(url: str, *, timeout: float = 90.0, cold: bool = False) -> Page:
    """Render `url` in the persistent browser and hand back its HTML.

    `cold` reads on the search profile instead of the owner's — prefer the named
    `read_cold`, which is the only thing that should ever pass it.

    Raises BrowserUnavailable when there is no browser to use; every other
    failure arrives as the underlying Playwright error."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)

    async def job(owner: _Owner) -> Page:
        if owner.view is not None and not cold:
            # The owner is driving the browser by hand. Reusing that context
            # would read the site at their PHONE's viewport and hand back a
            # mobile layout as if it were the page — quietly different results
            # for a reason nothing in the answer would show. It would also
            # steal the page they are mid-login on.
            raise BrowserUnavailable(
                "the browser is being driven by hand right now (/browser) — "
                "the page will be readable again once that window is closed"
            )
        context = await (owner.cold_context() if cold else owner.context())
        page = await context.new_page()
        owner.read_pages.add(page)

        async def attempt() -> tuple[Any, str]:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            )
            await page.wait_for_timeout(SETTLE_MS)
            await _dismiss_consent(page)
            return response, await _body_text(page)

        try:
            try:
                response, text = await attempt()
            except Exception:  # noqa: BLE001 — a download aborts the navigation
                # Chrome does not render a `Content-Disposition: attachment`; it
                # downloads it and leaves the navigation aborted. That is not a
                # failed read, so give the transfer a moment to register and
                # judge on whether a FILE arrived.
                await page.wait_for_timeout(SETTLE_MS)
                saved = await _save_downloads(owner, read_page=page)
                if not saved:
                    raise
                return Page(
                    text="", title="", images=[], url=page.url or url,
                    status=None, downloads=saved,
                )
            # A wall on a profile that has been reading for a while is usually
            # the SCORE, not the page: the vendor's token has soured. Shed it
            # and ask once more. This is what makes a real browser strictly
            # better than a session-less third-party reader rather than merely
            # different — without it, aish gave up on a page it could read.
            if is_challenge(text, response.status if response else None):
                if await _shed_reputation(context, url):
                    response, text = await attempt()
            # A SHORT page is ambiguous: a wall is short, but so is a page that
            # has not finished painting. Reads serialise through this one
            # thread, so three in a turn means the third starts on a busy
            # machine — and a half-painted listing looked exactly like a
            # challenge, was rejected as one, and sent the model off to Jina
            # reporting that the site could not be read at all. Give a thin
            # page one more chance to fill in before judging it.
            if len(text) < CHALLENGE_MAX_CHARS:
                await page.wait_for_timeout(SETTLE_MS)
                text = max(text, await _body_text(page), key=len)
            # Narrowing happens AFTER every judgement above, so <main> can never
            # move a threshold that was measured on a whole body.
            return Page(
                text=await _without_option_floods(
                    page, (await _main_text(page, text)) or text
                ),
                title=(await page.title()) or "",
                images=await _declared_images_async(page),
                url=page.url or url,
                status=response.status if response is not None else None,
                links=await _content_links(page),
                declared=await _declared_data_async(page),
                signin=await _has_password_field(page),
                downloads=await _save_downloads(owner, read_page=page),
            )
        finally:
            owner.read_pages.discard(page)
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    return _submit(job, timeout)


def open_for_login(url: str, *, timeout: float = LOGIN_WINDOW_TIMEOUT) -> list[str]:
    """Put a REAL, on-screen Chrome window in front of the owner and wait.

    Returns when they close it. Every host they visited at the top level is
    recorded as a login, so later reads of those sites are gated rather than
    made silently as them. The window is the point: aish never types the
    owner's credentials, it hands them a browser and stays out of it."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)

    async def job(owner: _Owner) -> list[str]:
        await owner.close_now()  # release the profile lock before relaunching
        context = await owner.context(args=_LOGIN_ARGS)
        visited: set[str] = set()
        page = await context.new_page()

        def note(frame: Any) -> None:
            try:
                if frame == page.main_frame:
                    host = host_of(frame.url)
                    if host:
                        visited.add(host)
            except Exception:  # noqa: BLE001
                pass

        page.on("framenavigated", note)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — they can still drive it by hand
            pass
        deadline = timeout
        step = 0.5
        while deadline > 0:
            if page.is_closed() or not context.pages:
                break
            try:
                await page.wait_for_timeout(int(step * 1000))
            except Exception:  # noqa: BLE001 — closed mid-wait IS the exit condition
                break
            deadline -= step
        _remember_logins(visited)
        await owner.close_now()  # next read relaunches off-screen at full size
        return sorted(visited)

    return _submit(job, timeout + 30.0)


def command(arg: str) -> str:
    """`/browser` — the owner's own door into the persistent profile.

    Shared verbatim by the CLI and the web so both surfaces say the same
    thing, and so this is testable without either."""
    arg = (arg or "").strip()
    verb, _, rest = arg.partition(" ")
    verb, rest = verb.lower(), rest.strip()

    if verb in ("forget", "logout"):
        if not rest:
            return "usage: /browser forget <host>"
        # Forget takes EVERYTHING aish knows about the site, which is now three
        # separate facts with three lifetimes: that he has an account there,
        # that aish may sign in for him, and the live session. It can take the
        # first two. It cannot take the third — the session is his to end, at
        # the site — so it says so rather than implying otherwise.
        dropped = forget_login(rest)
        credential = signin_mod.forget(signin_mod.origin_of("https://" + rest)) or (
            signin_mod.forget(signin_mod.origin_of("http://" + rest))
        )
        if not dropped and not credential:
            return f"{rest} was not recorded as signed in."
        lines = []
        if dropped:
            lines.append(
                f"{rest} is no longer treated as signed in — reads of it stop "
                "asking for approval."
            )
        if credential:
            lines.append(
                f"the saved sign-in for {rest} is deleted — aish can no longer "
                "sign in there for you."
            )
        lines.append(
            "Its cookies are untouched; sign out at the site itself in "
            "/browser if you meant to end the session."
        )
        return " ".join(lines)

    if verb == "close":
        shutdown()
        return "browser closed."

    if verb in ("anon", "search"):
        signed = sorted(search_logged_in_hosts())
        if not rest:
            return "\n".join(
                [
                    f"search profile: {search_profile_dir()}",
                    "signed in: " + (", ".join(signed) if signed else "nothing — "
                    "searches are read as an anonymous browser"),
                    "",
                    "This is the browser web_search reads results pages with. It "
                    "is separate from the one above on purpose: it never carries "
                    "your other sessions, and nothing it is signed into can "
                    "change what a read_url of a site is allowed to do.",
                    "",
                    "/browser anon <url>   sign this profile in (opens the "
                    "remote view on it)",
                ]
            )
        return "opening the anonymous profile at " + as_address(rest)

    if not arg:
        reason = unavailable_reason()
        hosts = sorted(seen_signed_in())
        lines = [
            f"profile: {profile_dir()}",
            f"status:  {reason or 'ready'}",
            f"stealth: {'on' if stealth() else 'off'}",
            # Deliberately worded as an OBSERVATION and not a claim about his
            # accounts. aish knows what the page last said; it does not know
            # whether he is signed in right now, and the file it reads is a
            # hint that the next read will correct either way.
            "last seen signed in: " + (", ".join(hosts) if hosts else "(nothing yet)"),
            # The consent word list, counted (#295 P4, #321). It is here rather
            # than nowhere because a vocabulary that has stopped matching is
            # otherwise perfectly silent: eon.pl's banner went unmatched for a
            # year and the only symptom was a site that quietly did not work.
            # Nothing reads this number to decide anything — it is an
            # instrument for noticing, and it says nothing until the list has
            # actually been asked.
            *([line] if (line := browse_mod.CONSENT_TALLY.line()) else []),
            *_signin_lines(),
            "",
            "/browser <url>       open a real window there so you can sign in",
            "/browser forget <host>  forget a site: the record AND any saved sign-in",
            "/browser anon        the separate profile searches are read with",
            "/browser close       shut the browser down now",
        ]
        return "\n".join(lines)

    url = as_address(rest if verb in ("login", "open") else arg)
    reason = unavailable_reason()
    if reason:
        return f"cannot open a browser: {reason}"
    try:
        visited = open_for_login(url)
    except BrowserUnavailable as exc:
        return f"cannot open a browser: {exc}"
    except Exception as exc:  # noqa: BLE001 — a launch failure is the owner's to see
        return f"browser failed: {exc}"
    if not visited:
        return "window closed; nothing recorded."
    return (
        "signed-in sites recorded: " + ", ".join(visited) + "\n"
        "Their cookies persist, so later reads are made as you — and each one "
        "asks first."
    )


# ------------------------------------------------------------- remote view
#
# The owner runs this Mac headless — they are never sitting at it, and they
# reach aish as a PWA from a phone. So `open_for_login`'s on-screen window,
# which assumes somebody is in front of the machine, is unreachable for them
# in practice. This is the same act done remotely: aish screenshots the page,
# they tap and type in the PWA, and each action returns a fresh frame.
#
# Pixels, not a proxy. Rewriting a site's URLs to pass them through aish
# breaks cookies (wrong domain), runtime-built SPA URLs, CSP and OAuth
# redirects, and would be blocked anyway. Shipping the rendered page sidesteps
# all of it, and — the part that actually matters — the session lands in
# THIS profile, which is the one that later reads must use.
#
# A frame per interaction rather than a video stream: a login is about six
# round trips, and a phone on a mobile connection would rather send six JPEGs
# than a screencast.

# The default when a client says nothing; every real one sends its own size.
VIEW_WIDTH = 1024
VIEW_HEIGHT = 1400
# BYTES ARE THE WRONG TARGET HERE, and #227 was investigated in those terms
# before anyone checked. This JPEG never reaches the model — `read_url` hands it
# extracted TEXT, and `is_challenge` judges text — so a frame costs no context,
# no tokens and no model time. Its only cost is Mac -> phone, on a trip that
# already runs 1-3 seconds. Measured on an allegro.pl listing, one 1280x1950
# frame at q50:
#
#   density   jpeg      capture   transfer @20Mbps
#   1.5x      331 KB     71 ms     136 ms
#   2x        497 KB     94 ms     204 ms
#   3x        909 KB    128 ms     373 ms
#   4x       1295 KB    228 ms     531 ms
#
# Density 1.5 was shipped to save bytes and cost about 90 ms of the trip to undo
# — three percent — in exchange for sharpness the owner noticed immediately.
# So the frame is dense again. Quality stays at 50: +12% for visibly fewer
# artifacts is the same bargain read the right way round (#225).
#
# The WIDTH is a different question and the answer there has not changed: 1280
# carries ~2.7x the page of a phone-width viewport per round trip, which is the
# resource that is actually scarce. See `view_size`.
VIEW_JPEG_QUALITY = 50
VIEW_SCALE = 2

# Density has a ceiling that no setting reaches, though, and that is what the
# owner hit: a frame is sharp only up to `zoom == density`, and zoom goes to 4x.
# Even 2x is already magnifying at the 2.5x double-tap. Serving 4x from the
# frame would mean a 1.3 MB, 228 ms capture on EVERY frame — glance, scroll,
# tap — to serve the one moment he stops and reads.
#
# So a frame is an OVERVIEW, and detail is fetched for the rectangle he is
# actually looking at. `Page.captureScreenshot` takes a clip with its own
# `scale`, independent of the context's device_scale_factor, so this needs no
# second context and no reload. The economics are the whole argument:
#
#   detail patch at 2.5x zoom    178 KB    38 ms
#   detail patch at 4x zoom       90 KB    18 ms
#
# It gets CHEAPER as he zooms further in, because the region shrinks as fast as
# the scale grows — the patch is always about one screenful. Detail is O(screen)
# where density is O(page), which is why this scales and raising VIEW_SCALE
# never will.
#
# The client asks for the scale ITS screen can show — stage CSS width x its own
# devicePixelRatio, over the page width it can see — so a lesser phone asks for
# less and nothing here has to know about anybody's hardware. Past parity the
# extra pixels are invisible, so the cap is real and not a guess.
VIEW_DETAIL_MAX_SCALE = 4.0
# Higher than a frame's: this is the one capture whose entire job is being read,
# it is a fraction of a frame's area, and it arrives when he has stopped moving.
VIEW_DETAIL_QUALITY = 60
# A backstop on the request, not a tuning knob: clip x scale is under the
# client's control, and a bad pair should not ask Chrome for a 100-megapixel
# JPEG on a box that also runs a VM.
VIEW_DETAIL_MAX_PIXELS = 12_000_000
# A NATIVE dialog is a dead end in the remote view, by construction: it is
# browser chrome, not page content, so `page.screenshot` cannot see it and the
# owner has nothing to tap. Passkeys are the case that bit — Google's sign-in
# uses WebAuthn conditional UI, which fires the moment an email field is
# focused: the page dims behind a prompt the owner cannot see, the password
# step never arrives, and Back does not recover it. Measured in this browser:
# WebAuthn available = True, conditional mediation available = True.
#
# So the capability is REMOVED rather than attempted, and sites fall back to a
# password — which is the flow the owner can actually complete. This is not a
# preference about passkeys; it is that offering one here can only ever produce
# a dead end. If native dialogs are ever surfaced, this goes.
_NO_NATIVE_CREDENTIAL_UI = """
try {
  delete window.PublicKeyCredential;
  Object.defineProperty(navigator, 'credentials', { get: () => undefined });
} catch (e) { /* a site that froze navigator keeps its passkeys; nothing to do */ }
"""

# The view is DESKTOP, and briefly it was not. Serving the phone's web to a
# phone-shaped viewport looked obviously right and was wrong for this UI: sites
# spend the whole first mobile screen on app-install banners and navigation, so
# a frame arrives carrying nothing and every scroll to reach content costs
# another round trip. A desktop page carries ~2.7x the content per frame
# (measured on allegro.pl: 16 400 characters and 114 prices against 7 000 and
# 61), and the owner zooms into it locally for free.
#
# Dropping it also ends an identity split that was never comfortable: allegro.pl
# answers ANY mobile identity with 403 and zero text, so reads had to stay
# desktop while the view went mobile, and a session created as a phone but read
# as a desktop is the mismatch bot-scoring exists to catch. One identity again.
VIEW_DESKTOP_WIDTH = 1280

# Hosts that needed the browser to be READ (owned here; see web.BROWSER_HOSTS).
BROWSER_HOSTS: set[str] = set()

VIEW_MIN_W, VIEW_MAX_W = 320, 1920
VIEW_MIN_H, VIEW_MAX_H = 400, 2400


def view_size(width: object, height: object) -> tuple[int, int]:
    """A DESKTOP-width page, in the SHAPE of the client's stage.

    The client sends the stage it will display in; this scales that shape up to
    `VIEW_DESKTOP_WIDTH`. Two things follow, and both are the point.

    **Round trips are the scarce resource; zoom is free.** A frame costs 1-3
    seconds, while zooming and panning happen on the phone and cost nothing. So
    the job is to maximise information per FRAME, not legibility per pixel —
    the owner zooms into whatever he wants once it has arrived. Measured on
    allegro.pl: a 430-wide viewport yields 7 000 characters and 61 prices, a
    1280-wide one yields 16 400 and 114. Nearly triple the page for one round
    trip.

    **And a phone-shaped viewport gets the phone's WEB, which is worse here.**
    Sites spend the first mobile screen on app-install banners and navigation:
    the owner's screenshot of allegro.pl's mobile home page is a coupon banner,
    a logo, a promo strip and a bottom nav bar, with no content at all. Reaching
    anything then costs scroll after scroll, one round trip each.

    Keeping the stage's ASPECT means `object-fit: contain` has nothing to
    letterbox, so the whole frame is page."""
    try:
        # str() first: this arrives straight off a WebSocket, so it may be any
        # JSON type at all, including a dict.
        stage_w = int(float(str(width or 0))) or VIEW_WIDTH
        stage_h = int(float(str(height or 0))) or VIEW_HEIGHT
    except (TypeError, ValueError):
        stage_w, stage_h = VIEW_WIDTH, VIEW_HEIGHT
    stage_w = max(1, stage_w)
    w = VIEW_DESKTOP_WIDTH
    h = round(w * (stage_h / stage_w))
    return (
        max(VIEW_MIN_W, min(VIEW_MAX_W, w)),
        max(VIEW_MIN_H, min(VIEW_MAX_H, h)),
    )


@dataclass
class Frame:
    """One rendered look at the page the owner is driving.

    `width`/`height` are CSS pixels — the coordinate space a tap maps into.
    The JPEG itself is VIEW_SCALE times that in each axis; the client scales
    it to fit, and the extra pixels are what survive a zoom."""

    jpeg: bytes
    url: str
    title: str
    width: int = VIEW_WIDTH
    height: int = VIEW_HEIGHT
    error: str = ""  # a navigation that failed, so a blank page is never silent
    focus: dict | None = None  # the field the page has focused, if any
    nav: int = 0     # documents loaded so far; a change means "reset the zoom"
    signin: str = ""  # a host a password was just submitted to
    saved: str = ""   # an origin whose sign-in was just stored (#280)
    asks_password: bool = False  # is this page putting a password box in front of him?
    # The page's activity generation AT CAPTURE, so the watcher can ask "has
    # anything happened since this picture?" rather than "since I last looked".
    # Paired with `nav` because a navigation resets the counter to zero, which
    # is a change that would otherwise read as no change at all.
    gen: int = -1
    # Was the page ALREADY FINISHED at the moment of capture (#223)? See
    # `already_finished` for exactly what that claims — it is an observation
    # made at the shutter, never a promise about the future.
    settled: bool = False


async def _frame(
    owner: _Owner,
    click: tuple[float, float] | None = None,
    *,
    settle: bool = True,
) -> Frame:
    """One captured look at the page.

    `settle=False` is the FAST first frame. Waiting for a page to go quiet
    before showing anything made every interaction feel dead — and it still
    missed late repaints, because a page that changes AFTER the capture never
    got another one. So the caller sends a quick frame and then ONE corrected
    frame if the page moved, which is the shape the owner asked for: "it's fine
    to show two screenshots… needs to be just once"."""
    page = owner.view
    if settle:
        await _settle(page)
    else:
        await page.wait_for_timeout(FIRST_FRAME_MS)
    size = page.viewport_size or {"width": VIEW_WIDTH, "height": VIEW_HEIGHT}
    # BEFORE the screenshot, deliberately. A mutation landing between the two is
    # then counted as "not in the picture", which costs a redundant capture that
    # the byte-compare throws away; reading it after would count that mutation as
    # already shown and lose the frame. Err toward the wasted compare.
    gen = await _activity(page)
    # Asked once per frame and reused: `view_act` needs it to tell a successful
    # sign-in from a failed one, and `view_close` needs it to know which sites
    # are even candidates for the question.
    asks_for_a_password = await _has_password_field(page)
    if asks_for_a_password and (host := host_of(page.url or "")):
        owner.password_hosts.add(host)
    frame = Frame(
        jpeg=await page.screenshot(type="jpeg", quality=VIEW_JPEG_QUALITY),
        url=page.url or "",
        title=(await page.title()) or "",
        width=size["width"],
        height=size["height"],
        focus=await _focus_info(page, click),
        nav=owner.navigations,
        gen=-1 if gen is None else int(gen.get("gen", -1)),
        asks_password=asks_for_a_password,
        # Read at the SHUTTER, from the same probe the generation comes from,
        # so the answer describes the picture that is about to be sent and not
        # the page a moment later.
        settled=already_finished(
            activity=gen, requests_in_flight=owner.view_requests
        ),
    )
    # Recorded per NAVIGATION, not per frame: a frame is sent for every tap,
    # scroll and keystroke, and rewriting the file on each of those would spend
    # a disk write on a screenshot that went nowhere.
    if owner.navigations != owner.recorded_nav:
        owner.recorded_nav = owner.navigations
        remember_page(frame.url, frame.title, cold=owner.view_cold)
    return frame


def _note_visit(owner: _Owner, url: str) -> None:
    """Remember a host the owner VISITED. Visiting is not signing in.

    This used to write straight to logins.txt, on the reasoning that gating a
    merely-visited site errs safe. It does not: browsing to allegro.pl and
    closing the sheet marked it signed-in, so every later read of the site the
    whole feature exists for asked for approval — friction on the main path,
    and a claim about the owner's account that was simply untrue.

    A login is a thing only the owner can confirm, so `view_close` hands back
    the ones that plausibly WERE one and the UI asks. Nothing here writes the
    record.

    What it hands back used to be this set — everything visited — and that was
    the same over-recording mistake in a third costume. Closing a session in
    which he had read eight sites asked him about eight sites in one batch with
    a single yes, and the yes wrote all of them: measured, it put netflix.com,
    airbnb.com, imdb.com and a typo'd imbd.com into `logins.txt`, each of which
    then costs a Chrome launch and an approval card on every later read of it.
    Friction on the main path, bought with a false claim about his accounts.

    So this set stays what it is — where the view HAS BEEN, which the recents
    list wants — and the question is asked from `password_hosts` instead."""
    host = host_of(url)
    if host:
        owner.view_hosts.add(host)


def view_open(
    url: str,
    *,
    width: object = None,
    height: object = None,
    cold: bool = False,
    timeout: float = 120.0,
) -> Frame:
    """Start a remote view at `url`, sized to the client, and return a frame.

    `cold` drives the SEARCH profile instead of the owner's, which is the only
    way to sign that profile in: rule 3 of this design is that a session must be
    created in the browser that will later use it, so copying cookies across
    from the owner's profile is not an alternative — it is the thing the rule
    forbids."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)
    # Address OR search, the same as the location box (`as_address`). Before
    # this the web app sent the typed line straight here and Chrome refused
    # anything schemeless, so `/browser eon.pl` from the PWA NEVER opened — it
    # came back `about:blank` with "could not open eon.pl (Error)" while
    # `/browser https://eon.pl` worked, and only because `command()` normalised
    # first. The one surface the owner actually uses was the broken one.
    url = as_address(url)
    w, h = view_size(width, height)

    async def job(owner: _Owner) -> Frame:
        return await _open_view(owner, url, w, h, cold=cold)

    return _submit(job, timeout)


def _count_navigation(owner: _Owner, page: Any, frame: Any) -> None:
    """A MAIN-frame navigation replaced the document."""
    try:
        if frame == page.main_frame:
            owner.navigations += 1
    except Exception:  # noqa: BLE001 — a torn-down page counts nothing
        pass


def _count_requests(owner: _Owner, page: Any) -> None:
    """Keep a running count of what the view page has on the wire (#223).

    Registered once per view, on the PAGE, which covers its child frames too.
    Nothing is injected into the document: the count comes from Chrome by way
    of Playwright, so a page cannot see that it is being counted and a
    fingerprinting check cannot notice a wrapped `fetch`.

    Deliberately un-bounded and un-timed. The only thing that reads it decides
    whether to SKIP a watcher, so a count stuck above zero — a long poll, an
    event stream, a request neither finished nor failed — costs the watcher
    that would have run anyway, which is exactly today's behaviour."""
    owner.view_requests = 0

    def started(_request: Any) -> None:
        owner.view_requests += 1

    def ended(_request: Any) -> None:
        owner.view_requests = max(0, owner.view_requests - 1)

    page.on("request", started)
    page.on("requestfinished", ended)
    page.on("requestfailed", ended)


async def _note_dialog(owner: _Owner, dialog: Any) -> None:
    owner.notice = f"the page said: {dialog.message[:200]}"
    try:
        await dialog.dismiss()
    except Exception:  # noqa: BLE001 — already gone
        pass


async def _refuse_upload(owner: _Owner, chooser: Any) -> None:
    owner.notice = (
        "this page asked to upload a file — the picker is a native dialog on "
        "the Mac and cannot be shown here, so it was cancelled"
    )
    try:
        await chooser.set_files([])
    except Exception:  # noqa: BLE001 — cancelling a dead chooser is fine
        pass


async def _open_view(
    owner: _Owner, url: str, w: int, h: int, *, cold: bool = False
) -> Frame:
    # A KNOWN viewport, so a tap at (x, y) in the PWA means that point
    # here. The read context uses the real window size and cannot give
    # that, so the view gets its own — and Chrome locks the profile, so
    # the read context has to let go first.
    await owner.close_now()
    args = [f"--window-size={w},{h}", "--window-position=-4000,-4000"]
    viewport = {"width": w, "height": h}
    open_context = owner.cold_context if cold else owner.context
    context = await open_context(
        args=args, viewport=viewport, device_scale_factor=VIEW_SCALE
    )
    owner.view_cold = cold
    page = await context.new_page()
    owner.navigations = 0
    page.on("framenavigated", lambda f: _count_navigation(owner, page, f))
    await page.add_init_script(_NO_NATIVE_CREDENTIAL_UI)
    # Native dialogs the owner cannot see either. Playwright DISMISSES these by
    # default, silently — so a login that asks "leave site?" or alerts an error
    # would vanish without trace. Accepting and reporting at least leaves the
    # words on screen.
    page.on("dialog", lambda d: asyncio.ensure_future(_note_dialog(owner, d)))
    # A file chooser opens a native picker on a Mac nobody is sitting at, which
    # would simply hang. Refuse it and say so.
    page.on("filechooser", lambda c: asyncio.ensure_future(_refuse_upload(owner, c)))
    _count_requests(owner, page)
    owner.view = page
    owner.view_hosts = set()
    owner.view_touched = time.monotonic()
    failed = ""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001 — reported, never silently blank
        # A goto that throws navigates NOWHERE, so the page is still
        # about:blank — and screenshotting that presented a white rectangle
        # and an empty address bar as though the view had opened. The owner
        # reasonably read it as the feature being broken.
        failed = f"could not open {url} ({type(exc).__name__})"
    owner.view_touched = time.monotonic()
    _note_visit(owner, page.url)
    frame = await _frame(owner)
    frame.error = failed
    return frame


def view_activity(timeout: float = 10.0) -> dict | None:
    """How active the open view is RIGHT NOW — cheap enough to poll.

    A few milliseconds and not one byte over the wire, which is the whole point:
    it lets the server ask often and capture rarely. Carries `nav` so the caller
    can tell a reset counter (a navigation) from a still one.

    None when there is no view or the page will not answer."""

    if _no_browser_yet():
        return None

    async def job(owner: _Owner) -> dict | None:
        page = owner.view
        if page is None:
            return None
        state = await _activity(page)
        if state is None:
            return None
        state["nav"] = owner.navigations
        owner.view_touched = time.monotonic()
        return state

    try:
        return _submit(job, timeout)
    except Exception:  # noqa: BLE001 — a probe that fails is a probe that cannot tell
        return None


def view_settled_frame(timeout: float = 30.0) -> Frame | None:
    """Capture again once the page has gone quiet, or None if there is no view.

    The WATCHER's capture. Its job is to be right rather than prompt; the caller
    only forwards it if it actually differs from what was shown. It still
    settles even though the watcher asks for it only once the probe has said the
    page is quiet, because the probe cannot answer DURING a navigation — and
    that is exactly when an unsettled capture would return a white rectangle and
    present it as the page."""

    if _no_browser_yet():
        return None

    async def job(owner: _Owner) -> Frame | None:
        if owner.view is None:
            return None
        return await _frame(owner)

    try:
        return _submit(job, timeout)
    except Exception:  # noqa: BLE001 — a follow-up that fails just does not arrive
        return None


@dataclass
class Detail:
    """A sharp re-capture of ONE rectangle of the page already on screen.

    Carries the rect it covers, in the same CSS pixels a tap maps into, because
    the client positions it by page coordinates rather than by screen ones —
    which is what keeps it aligned while he goes on panning."""

    jpeg: bytes
    x: int
    y: int
    width: int
    height: int
    # The scale actually CAPTURED, after clamping — not the one asked for. The
    # client decides from this whether a later, deeper zoom still needs a trip,
    # and it must not decide that from its own request.
    scale: float = 1.0
    nav: int = 0


def detail_request(
    x: object, y: object, width: object, height: object, scale: object,
    view_w: int, view_h: int,
) -> tuple[int, int, int, int, float]:
    """Clamp a detail request to the page and to what is worth capturing.

    Pure, so the clamping is testable without a browser. The rect is pulled
    inside the viewport rather than rejected: a rounding error at the edge of a
    zoomed page should cost a few pixels of coverage, not the whole capture."""
    def num(v: object, fallback: float = 0.0) -> float:
        try:
            return float(str(v))
        except (TypeError, ValueError):
            return fallback

    s = max(1.0, min(VIEW_DETAIL_MAX_SCALE, num(scale, 1.0)))
    w = max(16.0, min(float(view_w), num(width, view_w)))
    h = max(16.0, min(float(view_h), num(height, view_h)))
    rx = max(0.0, min(float(view_w) - w, num(x)))
    ry = max(0.0, min(float(view_h) - h, num(y)))
    # Shrink the SCALE, never the rect: a smaller rect would silently cover
    # less of what he is looking at, while a smaller scale only means the patch
    # is less sharp than his screen could show — degradation he can see past.
    if w * h * s * s > VIEW_DETAIL_MAX_PIXELS:
        s = max(1.0, (VIEW_DETAIL_MAX_PIXELS / (w * h)) ** 0.5)
    return int(rx), int(ry), int(w), int(h), s


def view_detail(
    x: object, y: object, width: object, height: object, scale: object,
    timeout: float = 30.0,
) -> Detail | None:
    """Re-capture one rectangle at the resolution the client's screen can show.

    None when there is no view — a missing detail patch is a blurry patch, not
    an error: the frame underneath it is still the page. Deliberately does NOT
    settle. The page has not been touched; this is the same paint at more
    pixels, and waiting would turn a sharpening into an interaction."""

    async def job(owner: _Owner) -> Detail | None:
        page = owner.view
        if page is None:
            return None
        size = page.viewport_size or {"width": VIEW_WIDTH, "height": VIEW_HEIGHT}
        rx, ry, w, h, s = detail_request(
            x, y, width, height, scale, size["width"], size["height"]
        )
        owner.view_touched = time.monotonic()
        # Straight to CDP: Playwright's screenshot() has no per-clip scale, and
        # the scale is the entire point — the context's own density is what was
        # not enough. Detached rather than cached, so a view that is torn down
        # underneath leaves nothing attached to a dead target.
        session = await page.context.new_cdp_session(page)
        try:
            shot = await session.send(
                "Page.captureScreenshot",
                {
                    "format": "jpeg",
                    "quality": VIEW_DETAIL_QUALITY,
                    "clip": {"x": rx, "y": ry, "width": w, "height": h, "scale": s},
                },
            )
        finally:
            with contextlib.suppress(Exception):
                await session.detach()
        return Detail(
            jpeg=base64.b64decode(shot["data"]),
            x=rx, y=ry, width=w, height=h, scale=s,
            nav=owner.navigations,
        )

    try:
        return _submit(job, timeout)
    except Exception:  # noqa: BLE001 — see the docstring: no patch, no error
        return None


def view_act(action: str, **kwargs: Any) -> Frame:
    """Apply one interaction to the open view and return the resulting frame.

    Keystrokes are NEVER logged or traced anywhere in this path — the owner
    types real passwords through it."""

    async def job(owner: _Owner) -> Frame:
        page = owner.view
        if page is None:
            # NOTHING the owner can ask for should answer "no remote view is
            # open". That is a statement about aish's bookkeeping, not about
            # what he wanted — and he met it after the 15-minute idle reaper
            # collected a view he was still looking at, on a page that was
            # still on his screen. Whatever he asked for, reopen and do it.
            target = str(kwargs.get("url") or owner.last_url or "")
            if not target:
                raise BrowserUnavailable(
                    "the browser has nothing open — type an address above"
                )
            if not target.startswith(("http://", "https://")):
                target = "https://" + target
            vw, vh = view_size(kwargs.get("width"), kwargs.get("height"))
            frame = await _open_view(owner, target, vw, vh)
            # A tap or a scroll aimed at the OLD page would land somewhere
            # arbitrary on the freshly loaded one, so reopening is where it
            # stops: he sees the page again and acts on what he can see.
            return frame
        clicked: tuple[float, float] | None = None
        if action == "click":
            clicked = (float(kwargs["x"]), float(kwargs["y"]))
            await page.mouse.click(*clicked)
        elif action in ("fill", "clear"):
            # REAL KEYSTROKES, not Playwright fill(). fill() dispatches one
            # `input` event and no key events at all, which breaks
            # keystroke-listening widgets — and breaks 2FA code boxes outright,
            # the six one-character inputs that advance focus on each keyup:
            # fill() would drop "123456" into box one. Typing over a selection
            # fires exactly the events typing always fires, in any iframe,
            # against whatever is focused — no element handle to go stale.
            await page.keyboard.press("ControlOrMeta+a")
            if action == "clear":
                await page.keyboard.press("Delete")
            else:
                text = str(kwargs.get("text", ""))
                if text:
                    await page.keyboard.type(text, delay=12)
                else:
                    await page.keyboard.press("Delete")
                # Recorded BEFORE the submit keystroke: a fast navigation would
                # otherwise increment `navigations` first, making the
                # after-the-password test false and silently losing the
                # question.
                if kwargs.get("secret"):
                    # A password went into THIS host — remember which, so the
                    # sign-in question can name it, here and now.
                    owner.pending_signin = host_of(page.url)
                    owner.pending_nav = owner.navigations
                    _hold_credential(owner, page.url, text, kwargs.get("remember"))
                else:
                    # The identifier is typed on a different keystroke, and in
                    # a two-step form on a different PAGE. Whatever ordinary
                    # field he filled last at this origin is the candidate.
                    _hold_identifier(owner, page.url, text)
                if kwargs.get("submit"):
                    await page.keyboard.press("Enter")
        elif action == "type":
            await page.keyboard.type(str(kwargs.get("text", "")), delay=12)
        elif action == "key":
            await page.keyboard.press(str(kwargs.get("key", "Enter")))
        elif action == "choose":
            # select_option fires `change`, which is what a site listens for —
            # typing into a <select> would do nothing at all.
            await page.select_option(":focus", str(kwargs.get("value", "")))
        elif action == "resize":
            # The sheet changed shape — a rotation, or a keyboard opening. The
            # page is re-laid-out rather than the frame being stretched, so a
            # responsive site can switch layout with it.
            rw, rh = view_size(kwargs.get("width"), kwargs.get("height"))
            await page.set_viewport_size({"width": rw, "height": rh})
        elif action == "scroll":
            await page.mouse.wheel(0, float(kwargs.get("dy", 600)))
        elif action == "back":
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 — nothing to go back to is not an error
                pass
        elif action == "goto":
            target = as_address(str(kwargs.get("url", "")))
            await page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        elif action == "refresh":
            # RELOAD, not merely re-capture. It was a no-op that just took a
            # fresh screenshot, so the button labelled reload did not reload —
            # and, since nothing navigated, the zoom never reset either.
            await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        else:
            raise ValueError(f"unknown view action {action!r}")
        owner.view_touched = time.monotonic()
        owner.last_url = page.url or owner.last_url
        _note_visit(owner, page.url)
        frame = await _frame(owner, clicked, settle=False)
        # The page moved after a password went in AND is no longer asking for
        # one. Both halves matter: navigation alone is nearly always true —
        # a failed login redirects back to the form with ?error=1, and so does
        # a reload, a Back, or him giving up and browsing somewhere else twenty
        # minutes later, since nothing clears the flag until it fires. That was
        # tolerable while this only ASKED a human a question they could
        # dismiss; it is not tolerable now that it also decides whether a
        # password is written to the Keychain, where a wrong one would burn the
        # one permitted attempt at every lapse.
        if (
            owner.pending_signin
            and owner.navigations > owner.pending_nav
            and not frame.asks_password
        ):
            frame.signin = owner.pending_signin
            # OBSERVED, not asked. A password went in, the page moved, and it
            # has stopped asking for one — that is a sign-in, and there is
            # nothing left for a human to confirm about it.
            note_signed_in(page.url)
            owner.signed_in_here.add(owner.pending_signin)
            owner.password_hosts.discard(owner.pending_signin)
            owner.pending_signin = ""
            frame.saved = _save_held_credential(owner)
        if owner.notice:
            frame.error = owner.notice
            owner.notice = ""
        return frame

    return _submit(job, 120.0)


# --------------------------------------------------- the model's own session

# There is no module-level "current snapshot" here, and there must not be one
# again. It existed so the approval gate could name the control before the act
# ran — but a global is one page for every chat, and that is precisely how a
# chat about flights came to draw the card `drive www.imdb.com … AS YOU`
# (#272). The picture belongs to the chat: `web.BrowseView`, held by the Agent.

# aish types the owner's credentials NOWHERE, and a model-driven session is the
# last place that could start. A page asking for one is handed back to him.
NO_PASSWORDS = (
    "aish never types passwords. Open /browser {host} and sign in yourself — "
    "the session persists, and this page will be readable afterwards"
)

DRIVEN_BY_HAND = (
    "the browser is being driven by hand right now (/browser) — it will be "
    "available again once that window is closed"
)

NOTHING_OPEN = "nothing is open to act on — call browse(url) first"

# There is ONE page for the whole process, so another chat's `browse(url)`
# navigates it out from under this one (#272). The epoch counts documents this
# session has driven; a chat carries the epoch of the snapshot it was handed,
# and a mismatch means the page it is looking at is not the page that is
# loaded.
#
# **The refusal deliberately carries no page.** Every other ending here is a
# snapshot, because a bare string leaves the model holding nothing — but the
# page on the other side of this fence belongs to a DIFFERENT CHAT and may be
# any account the owner is signed into. Handing it over to say "this is not
# yours" would be the disclosure the fence exists to prevent.
PAGE_TAKEN = (
    "another chat navigated this browser to a different page while you were "
    "on it, so what you were looking at is gone and its controls no longer "
    "mean anything. Nothing was pressed. Call browse(url) to open the page "
    "you need again"
)


async def _settled_text(
    page: Any,
    *,
    tries: int = 3,
    still_for: float = SETTLE_QUIET_MS,
    timeout_ms: float = SETTLE_MAX_MS,
) -> str:
    """The page's text, once it stops saying it is still fetching it.

    A page mid-load HAS text — "Wczytywanie danych", a spinner's label, a
    skeleton — so it passes the emptiness test and the thin-page retry both.
    Measured on eon.pl/mojeon/Umowy-i-dane/Moje-Umowy, which came back as its own
    loading message twice in one session while the owner watched (#237). Reads do
    not pay this cost; a browse action is one of a handful in a flow, and the
    thing being waited for is the answer.

    `_settle` first, and that is the load-bearing half (#247). A driven page used
    to wait a flat SETTLE_MS and then judge, while an ordinary read waits for the
    network to go quiet — so a table filled by a later request read as an EMPTY
    TABLE, and an empty table was reported to the owner as "this property has no
    invoices". Twice, on two of five properties, and a second run twenty minutes
    later found them all. A page that has not said anything is the case the
    loading-word test cannot see; the mutation observer inside `_settle` can."""
    await _settle(page, still_for=still_for, timeout_ms=timeout_ms)
    text = await _body_text(page)
    for _ in range(tries - 1):
        if text and not browse_mod.still_loading(text):
            break
        await page.wait_for_timeout(SETTLE_MS)
        text = await _body_text(page) or text
    return text


async def _save_downloads(
    owner: _Owner, *, read_page: Any = None, browse_page: Any = None
) -> list[str]:
    """Write whatever this caller downloaded, and say where it went."""
    pending = owner.take_downloads(read_page=read_page, browse_page=browse_page)
    if not pending:
        return []
    directory = downloads_dir()
    directory.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for download in pending:
        try:
            name = browse_mod.safe_filename(download.suggested_filename or "")
            target = directory / name
            stem, suffix = target.stem, target.suffix
            bump = 1
            while target.exists():
                target = directory / f"{stem}-{bump}{suffix}"
                bump += 1
            await download.save_as(str(target))
        except Exception:  # noqa: BLE001 — a failed download is not a failed click
            continue
        if target.stat().st_size > browse_mod.DOWNLOAD_MAX_BYTES:
            # Refused AFTER the write because the size is not known before it:
            # Playwright streams to its own temp file and reports no length.
            with contextlib.suppress(OSError):
                target.unlink()
            continue
        saved.append(str(target))
    browse_mod.prune_downloads(directory)
    return saved


# How many documents one page may be enumerated across. A page is often several:
# a consent wall, a login form, a card field and a chat widget are all iframes,
# and `page.evaluate` reaches only the main one — so browse reported "no controls
# found" on pages visibly full of them (#244). Bounded because an ad-heavy page
# can carry dozens of frames and each is a round trip.
MAX_FRAMES = 12


async def _enumerate(
    page: Any, match: str = ""
) -> tuple[list[dict], int, int, int, str]:
    """Every control on the page, across its frames, in one numbering.

    The count continues across frames rather than restarting, so `[14]` means
    one thing on this page however many documents it is made of — and acting
    finds it by searching the frames for the tag.

    `match` decides which controls the cap BUYS, not which ones exist. It has
    to be applied here and not on the way out: a control the cap dropped was
    never tagged, and an untagged control cannot be acted on however cleverly
    Python filters the list afterwards (#270)."""
    raw: list[dict] = []
    matched = unreached = matching = 0
    commit = ""
    options = {
        "max": browse_mod.MAX_CONTROLS,
        "nameMax": browse_mod.NAME_MAX_CHARS,
        "inlineChoices": browse_mod.CHOICE_INLINE_MAX,
        "offset": 0,
        "match": match or "",
    }
    try:
        frames = list(page.frames)[:MAX_FRAMES]
    except Exception:  # noqa: BLE001 — a page that will not list frames has one
        frames = [page.main_frame]
    for frame in frames:
        options["offset"] = len(raw)
        try:
            found = await frame.evaluate(browse_mod.CONTROLS_JS, options)
        except Exception:  # noqa: BLE001 — a frame that will not answer has none
            continue
        raw += list(found.get("controls") or [])
        matched += int(found.get("matched") or 0)
        unreached += int(found.get("unreachable") or 0)
        matching += int(found.get("matching") or 0)
        commit = commit or str(found.get("commit") or "")
    return raw, matched, unreached, matching, commit


async def _find(page: Any, n: int) -> tuple[Any, bool]:
    """(locator, is_in_the_main_frame) for control `n`, or (None, False).

    Whether it is the main frame decides one thing: the link fallback in
    `_press` navigates the TOP page, which would be the wrong surface for a link
    inside a consent or payment iframe."""
    try:
        frames = list(page.frames)[:MAX_FRAMES]
    except Exception:  # noqa: BLE001
        frames = [page.main_frame]
    for frame in frames:
        target = frame.locator(f'[data-aish-n="{int(n)}"]').first
        try:
            if await target.count():
                return target, frame is page.main_frame
        except Exception:  # noqa: BLE001 — an unusable frame carries nothing
            continue
    return None, False


# The evidence frame (#289) — a stored picture of the page as the model was
# shown it, for the owner who cannot see the browser aish is driving.
#
# Quality 50 for the same reason the remote view uses it, and the VIEWPORT
# rather than the whole document: this is the browse context, whose window is
# 1440x900 and whose device_scale_factor is 1. Measured on a dense 60-tile
# shop page at 1440x820, this machine, headless Chrome:
#
#   q40  60 KB   q50  67 KB   q60  73 KB   q70  84 KB
#   capture ~27 ms at every quality; the SAME page full-height is 311 KB
#
# So quality is nearly free and HEIGHT is what costs — which is why a frame is
# one screenful. It is not a claim about how much of the page the model read:
# the model reads the whole text, and the frame shows what the page looked like
# where a person would start reading it.
FRAME_JPEG_QUALITY = 50
# A capture is an EXTRA and must never cost the snapshot. Bounded well above the
# ~27 ms measured above and well under anything a caller would notice; on the
# far side of it the model still gets its page and the record says the capture
# failed, which is a true statement either way.
FRAME_TIMEOUT_MS = 5_000


async def _evidence_frame(
    owner: _Owner, page: Any, *, label: str = "browse"
) -> tuple[str, str]:
    """(stored path, reason there is none) for a picture of this page.

    Called at snapshot time, on the owner loop, after the page has settled — so
    this is one more short job on a page that has already stopped moving, not a
    new wait, a new thread or a poll.

    **One refusal: never while the owner's hands are on the browser.**
    `_session` already refuses a browse outright while `owner.view` is set, so
    nothing reaches here in that state today; the check is written where the
    capture is because that is where the rule belongs, and because the later
    slices of #289 are the ones that make a page reachable with a viewer on it.

    **There used to be two more, and both were wrong** (#320). A page showing a
    password box was refused on the grounds that "a screenshot of a login form
    is precisely the artifact that must not exist" — but **aish never types a
    password on the browse path**, structurally: the credential replay is
    `sign_in`, on its own page, and the model cannot ask for one. So the
    refused frame was an EMPTY login form. It protected nothing, and it cost
    exactly the picture that matters most, because a sign-in that did not work
    is precisely when the owner needs to see the screen. The could-not-tell
    refusal existed only to resolve that question safely; with the question
    gone it had no job of its own, so it went too rather than being left in
    place looking load-bearing.

    Nothing else is judged. In particular this makes no claim about the page
    being safe to look at — a frame is a RECORD, and a record is detection, not
    protection. Nothing anywhere may be permitted, widened or checked less
    carefully on the grounds that a frame was captured.

    The one page aish HAS typed a credential into gets its own caller,
    `_signin_frame`, which blanks the field before the shutter rather than
    declining to look.
    """
    if owner.view is not None:
        return "", browse_mod.NO_FRAME_HANDS
    try:
        jpeg = await page.screenshot(
            type="jpeg", quality=FRAME_JPEG_QUALITY, timeout=FRAME_TIMEOUT_MS
        )
        stored = media.store(
            bytes(jpeg),
            frames_dir(),
            f"{label} {host_of(str(page.url or ''))}",
            max_bytes=media.FRAME_MAX_BYTES,
            max_files=media.FRAME_MAX_FILES,
        )
    except Exception:  # noqa: BLE001 — an extra that failed must not cost the page
        return "", browse_mod.NO_FRAME_FAILED
    return str(stored), ""


async def _snapshot(
    owner: _Owner,
    session: _Session,
    *,
    problem: str = "",
    notice: str = "",
    asked: str = "",
    match: str = "",
    started_work: bool = False,
    covered: browse_mod.Cover | None = None,
) -> browse_mod.Snapshot:
    """The page as the model receives it: what it says, and what it can press.

    `started_work` is the caller saying it just did something that may have set
    the page going — pressed *Szukaj*, sent a form. It buys the patient bar in
    `_settle`: a read of a page that finished long ago must not pay seconds for
    the possibility that it did not, and a read that follows a search must.

    `covered` is what the press found sitting on top of the control, and it is
    the CALLER's because only the caller pressed anything: a snapshot taken for
    a read, an open or a refusal has nothing to say about it and says nothing
    (#321)."""
    page = session.page
    text = await _without_option_floods(
        page,
        await _settled_text(
            page,
            still_for=WATCH_SETTLED_MS if started_work else SETTLE_QUIET_MS,
            timeout_ms=WATCH_MAX_MS if started_work else SETTLE_MAX_MS,
        ),
    )
    raw, matched, unreached, matching, commit = await _enumerate(page, match)
    controls = browse_mod.controls_from(raw)
    # What the MODEL is told about the page being a wall. The capture no longer
    # consults it (#320): a browse-path login form is an EMPTY login form, and
    # refusing to photograph it cost the picture and protected nothing.
    signin = await _has_password_field(page)
    frame, frame_skipped = await _evidence_frame(owner, page)
    # Deliberately NOT narrowed to <main>: reads narrow for budget, but the
    # control the model is looking for is very often in the header the narrowing
    # would drop — "Przełącz lokal" sits beside the account name, not in <main>.
    snapshot = browse_mod.Snapshot(
        url=str(page.url or ""),
        title=str(await page.title() or ""),
        text=text,
        controls=controls,
        hidden=max(0, matched - len(controls)),
        narrowed=match or "",
        matching=matching,
        unreachable=unreached,
        epoch=session.epoch,
        signin=signin,
        frame=frame,
        # Drained, not read: a snapshot is taken once per call, and this is
        # what ties a message to the press that provoked it instead of leaving
        # it to be reported again under the next one.
        console=session.console.drain(),
        covered=covered or browse_mod.Cover(),
        frame_skipped=frame_skipped,
        problem=problem,
        notice=notice,
        downloads=await _save_downloads(owner, browse_page=page),
        asked=asked if browse_mod.landed_elsewhere(asked, str(page.url or "")) else "",
        commit_evidence=commit,
    )
    session.touched = time.monotonic()
    return snapshot


async def _session(owner: _Owner, key: str, *, opening: bool) -> _Session:
    """THIS CHAT's browse session, opening one if it may.

    The key is the chat. A chat that has none — the CLI, `verify_browse.py` —
    passes `""` and shares one session with every other keyless caller, which
    is exactly the single-session behaviour this used to have for everybody."""
    if owner.view is not None:
        # The owner's hands outrank the model's. Reusing his page would steal the
        # login he is mid-way through, and his viewport would silently change
        # what the model reads (the same reasoning as `read`).
        raise BrowserUnavailable(DRIVEN_BY_HAND)
    session = owner.browse_pages.get(key)
    if session is not None:
        try:
            closed = session.page.is_closed()
        except Exception:  # noqa: BLE001 — a page that cannot answer is gone
            closed = True
        if not closed:
            # A new call begins here, so this is where the console starts over
            # — one place rather than a rule each of the three entry points has
            # to remember, since all three obtain their session through here.
            session.console.begin()
            return session
        del owner.browse_pages[key]
    if not opening:
        # The idle reaper can collect the context between turns. Nothing about
        # that is the model's fault or the owner's business — but an index from
        # the old document means nothing on a fresh one, so this is a refusal
        # with instructions rather than a silent reopen at a guessed URL.
        raise BrowserUnavailable(NOTHING_OPEN)
    await _evict_stale(owner)
    context = await owner.context()
    session = _Session(await context.new_page())
    owner.browse_pages[key] = session
    return session  # a fresh session's console is already empty


async def _evict_stale(owner: _Owner) -> None:
    """Keep the number of open tabs bounded before adding another.

    A tab per chat is the point, but a chat is cheap to start and this box runs
    a Home Assistant VM and Colima under a 16 GB roof. Dead and idle sessions
    go first; if every session is live, the least recently touched is closed —
    and the chat that owned it gets `NOTHING_OPEN` on its next act, which is
    the same answer the idle reaper has always given."""
    now = time.monotonic()
    for key, session in list(owner.browse_pages.items()):
        if not session.live(now):
            await _drop(owner, key)
    while len(owner.browse_pages) >= MAX_BROWSE_PAGES:
        oldest = min(owner.browse_pages, key=lambda k: owner.browse_pages[k].touched)
        await _drop(owner, oldest)


async def _drop(owner: _Owner, key: str) -> None:
    """Close one chat's page and forget it. Never raises: a page that will not
    close is still a page this session no longer owns."""
    session = owner.browse_pages.pop(key, None)
    if session is None:
        return
    with contextlib.suppress(Exception):
        if not session.page.is_closed():
            await session.page.close()


async def _adopt_new_tab(
    owner: _Owner, key: str, page: Any, before: list[Any]
) -> Any:
    """A control that opened a new tab moves the session to it.

    Otherwise the model presses "Pobierz e-fakturę", the document opens beside
    it, and the snapshot faithfully reports the page it was already on — which
    reads as "nothing happened".

    Only a tab this session may CLAIM (#291). A concurrent `read` opens its own
    page on the same context and closes it in its `finally`, so adopting one
    left the chat holding a page that no longer existed and answering
    NOTHING_OPEN on its next act."""
    try:
        opened = [
            p
            for p in page.context.pages
            if p not in before
            and not p.is_closed()
            and owner.claimable(p, browse_page=page)
        ]
    except Exception:  # noqa: BLE001 — no context to ask is no new tab
        return page
    if not opened:
        return page
    fresh = opened[-1]
    try:
        await fresh.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — a tab that never settles is still the tab
        pass
    session = owner.browse_pages.get(key)
    if session is not None:
        session.adopt(fresh)
    return fresh


def browse_open(
    url: str, *, key: str = "", topic: str = "", timeout: float = 120.0
) -> browse_mod.Snapshot:
    """Open `url` in the model's session and describe what is there.

    `topic` narrows BOTH halves of the answer — the page text on the way out,
    and which controls the cap buys on the way in."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async def job(owner: _Owner) -> browse_mod.Snapshot:
        session = await _session(owner, key, opening=True)
        page = session.page
        session.epoch += 1
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await _dismiss_consent(page)
        return await _snapshot(owner, session, asked=url, match=topic)

    return _submit(job, timeout)


class Stuck(Exception):
    """Listed, still on the page, and it will not take the action.

    Carries what the ladder found COVERING it, when anything was (#321), and
    the caller words the refusal from that. *Press whatever closes the thing on
    top of this* and *this control is inert, find another route* are opposite
    instructions, and the sentence that used to cover both — "something may be
    covering it", said on every stuck control — could only ever be a guess."""

    def __init__(self, cover: browse_mod.Cover | None = None) -> None:
        self.cover = cover or browse_mod.Cover()
        super().__init__(self.cover.by)


async def _reachable_now(target: Any) -> str:
    """Why this control cannot be pressed AT THIS MOMENT, or ''.

    The tag outlives the reachability. `_settled_text` waits, the model thinks,
    and a menu that closes on scroll or on a timer leaves its entries tagged and
    unpressable — which used to be discovered by spending 45 seconds. A control
    aish cannot ask about gets the benefit of the doubt: the ladder below is
    bounded anyway, and refusing on a failed question would be a new way to
    refuse a control that is perfectly fine."""
    try:
        return str(await target.evaluate(browse_mod.REACHABLE_JS) or "")
    except Exception:  # noqa: BLE001 — an unanswerable element is not a verdict
        return ""


async def _centre(target: Any) -> None:
    with contextlib.suppress(Exception):
        await target.evaluate(browse_mod.CENTRE_JS)


# Focus, then ask whether it landed ON this element — piercing shadow roots,
# because that is where it lands and not where the document says (#273).
_FOCUS_LANDED_JS = "(el) => {" + _DEEP_ACTIVE_JS + """
  el.focus();
  const active = deepActive();
  return active === el || (!!active && !!el.contains && el.contains(active));
}"""


async def _focus(target: Any) -> bool:
    """Focus it, and say whether focus actually landed.

    Never assumed: a blind `keyboard.press("Enter")` after a focus that did not
    take goes to the document, and on a page with a form that is a submit
    nobody asked for."""
    try:
        return bool(await target.evaluate(_FOCUS_LANDED_JS))
    except Exception:  # noqa: BLE001 — an element that will not focus has not
        return False


# What is sitting ON TOP of this control, or "" if nothing is.
#
# Shadow-aware in both directions: `elementFromPoint` returns the shadow HOST
# for anything inside an open shadow root, and `Node.contains` does not cross a
# shadow boundary either — so the naive "is the top element my ancestor" test
# calls a control covered by its own host. The ancestor chain is walked through
# hosts instead.
_COVERED_JS = """(el) => {
  const b = el.getBoundingClientRect();
  if (!b.width || !b.height) return "";
  const top = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
  if (!top) return "";
  const chain = new Set();
  let node = el;
  while (node) {
    chain.add(node);
    const root = node.getRootNode && node.getRootNode();
    node = node.parentElement || (root && root.host) || null;
  }
  if (chain.has(top) || el.contains(top)) return "";
  return String(top.id || top.className || top.tagName || "").slice(0, 60);
}"""


async def _what_covers(target: Any) -> str:
    """The element sitting on top of this control, in the page's own words, or
    "" when nothing is.

    One `evaluate`, and the whole structural half of #321: it needs no
    vocabulary, so it is true of every consent wall, modal, cookie bar and
    sticky footer in every language. An element that will not answer is not
    coverable — the safe direction, since claiming a cover aish did not see
    would be the wide guarantee this file keeps producing."""
    try:
        return browse_mod.covering_name(str(await target.evaluate(_COVERED_JS) or ""))
    except Exception:  # noqa: BLE001 — an element that will not answer
        return ""


async def _uncover(page: Any, target: Any) -> browse_mod.Cover:
    """What is over this control, and whether the consent list could clear it.

    **It returns the NAME, and that is the fix in #321.** It used to reduce the
    answer to a bool and throw the element away, so the ladder above could only
    ever say "something may be covering it" — which is what a stuck control and
    a covered one both said, indistinguishably, for a year. eon.pl's banner
    covers its login button, the word list misses its label by one letter, and
    the owner spent a day on four wrong diagnoses of a fact this function had
    already computed.

    **The wall does not have to be there when the page opens.** OneTrust,
    Cookiebot and their kind load asynchronously, so on qatarairways.com the
    banner arrives AFTER `browse_open` has already looked for one and found
    nothing — and from then on it eats every click on the page for the rest of
    the session. Every press fell through to the keyboard, and pressing Enter
    on a date field that never opened its picker is a press that reports
    success and does nothing (#273).

    Only ever consulted when a real click has already failed, because that is
    the only evidence worth spending four seconds of selector probing on. A
    page with nothing over it pays one `evaluate`.

    `dismissed` is the CONTROL coming clear, not the banner being clicked: a
    consent button that was pressed and left the overlay in place has dismissed
    nothing for any purpose here."""
    covered = await _what_covers(target)
    if not covered:
        return browse_mod.Cover()
    # The selector makes "nothing matched" a cheap early out; the second
    # `_what_covers` is what keeps `dismissed` honest when one did.
    cleared = bool(await _dismiss_consent(page)) and not await _what_covers(target)
    # Counted HERE and nowhere else: this is the one place the word list is
    # handed an obstruction that is known to exist, so it is the one place its
    # hit rate means anything (#295 P4).
    browse_mod.CONSENT_TALLY.note(dismissed=cleared)
    return browse_mod.Cover(by=covered, dismissed=cleared)


# What is observably true about a control, for telling a press that WORKED
# from one that only happened.
#
# Deliberately only things this press can be held responsible for: the
# control's own state, whether it is still in the document, and the address.
# A node count or a mutation observer would be a better detector and a worse
# one — an animation, an ad slot or a polling widget mutates the page every
# second on a live site, so "something changed" would read as success on every
# page that moves by itself. A false "it worked" is the dangerous direction:
# the model would report a form as sent.
_ACTIVATION_JS = r"""(el) => {
  const said = [];
  for (const attr of ['aria-expanded', 'aria-pressed', 'aria-selected',
                      'aria-checked', 'aria-hidden', 'class', 'hidden',
                      'disabled', 'open']) {
    said.push(attr + '=' + ((el.getAttribute && el.getAttribute(attr)) || ''));
  }
  if ('checked' in el) said.push('checked=' + el.checked);
  if ('value' in el) said.push('value=' + el.value);
  // Its OWN words. A button that reports itself — "Wyślij" becoming "Wysłano"
  // — is the commonest proof a press was taken, and it is still this control's
  // doing and not the page's: an animation elsewhere cannot rewrite it.
  said.push('says=' + (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80));
  said.push('connected=' + !!el.isConnected);
  said.push('at=' + location.href);
  return said.join('|');
}"""


async def _activation(target: Any) -> str | None:
    try:
        return str(await target.evaluate(_ACTIVATION_JS))
    except Exception:  # noqa: BLE001 — a control that will not answer is unreadable
        return None


async def _took(page: Any, target: Any, before: str | None) -> str:
    """The caveat a press that was not a real click has to carry, or "".

    Every rung below the real click used to assert its own success — "aish
    pressed it with the keyboard" — and on qatarairways.com Enter on a date
    field that never opened its picker was reported exactly that way (#273).
    The dispatch rung had the opposite fault: it hedged unconditionally, *"the
    page may not have registered it as a real press"*, on presses that plainly
    HAD registered.

    Neither is a fact. This is: the control is read before and after, and what
    comes back says which of the three things happened — it reacted, it did
    not, or aish could not tell. Same posture as the date step's readback, and
    the same reason: everything about the ladder is a heuristic, nothing about
    the RESULT may be."""
    await page.wait_for_timeout(SETTLE_MS)
    after = await _activation(target)
    if before is None or after is None:
        return ", though aish could not check whether the page took it"
    if before != after:
        return ""
    return (
        ", and nothing about that control or the address changed afterwards, so "
        "it may not have been registered — check the page before relying on it"
    )


async def _press(
    page: Any, target: Any, *, mutating: bool, href: str
) -> browse_mod.Pressed:
    """Press it, escalating cheaply. Returns how it went, or raises Stuck.

    One 45-second click used to be the whole of this, and a control that would
    never become clickable cost the full timeout — three times in one session,
    on three unrelated sites (#244). Every stage here is bounded and every
    failure falls through in seconds, so the worst case is about ten.

    The order is not arbitrary. A real click first, because it is the real
    thing. Then the KEYBOARD, which is not a workaround at all — it is the other
    first-class way people press things, it fires trusted events, and `focus()`
    scrolls natively even inside a container Playwright's scroller cannot move.
    Then, for a LINK, the destination the page itself declared: the href was
    read off the DOM at enumeration and is the same fact the gate used to
    classify this control as a plain navigation, so following it is the approved
    act by another route — and it is not the URL GUESSING this project forbids,
    because nothing here was remembered or composed.

    `force=True` appears nowhere, at any stage, ever. It clicks a COORDINATE and
    presses whatever happens to be on top of it, which is the one press that can
    land on a control the owner never approved. A dispatched event is the
    opposite: it activates exactly the element the model named and the gate saw.
    It is still a lie about physics, so it is last, it is never used on
    something that spends or deletes, and the snapshot says it happened.

    **What was IN THE WAY travels back beside the note (#321).** Every rung
    below the first exists because a real click did not land, and until now the
    one fact that says why — the element sitting on top of the control — was
    computed here, reduced to a bool, and dropped. It is carried out on
    `Pressed.cover` so it reaches the trace as well as the model: a press that
    never landed is precisely the failure nobody can reconstruct afterwards."""
    with contextlib.suppress(Exception):
        await target.click(timeout=ACT_TIMEOUT_MS)
        return browse_mod.Pressed()
    # A real click is worth trying TWICE if the reason it failed was something
    # lying over the page that aish is allowed to remove. Asked once, and the
    # answer is kept whatever the rest of the ladder does with it.
    cover = await _uncover(page, target)
    if cover.dismissed:
        with contextlib.suppress(Exception):
            await target.click(timeout=ACT_TIMEOUT_MS)
            return browse_mod.Pressed(
                note=browse_mod.COVERED_DISMISSED.format(by=cover.by), cover=cover
            )
    if await _focus(target):
        before = await _activation(target)
        with contextlib.suppress(Exception):
            await page.keyboard.press("Enter")
            return browse_mod.Pressed(
                note="the click would not land, so aish pressed it with the keyboard"
                + await _took(page, target, before),
                cover=cover,
            )
    if href:
        await page.goto(href, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        return browse_mod.Pressed(
            note="the link would not click, so aish opened the destination the "
            f"page declares for it ({href})",
            cover=cover,
        )
    if not mutating:
        before = await _activation(target)
        with contextlib.suppress(Exception):
            await target.dispatch_event("click")
            return browse_mod.Pressed(
                note="the click would not land, so aish dispatched the event "
                "straight to the control"
                + (await _took(page, target, before) or ", which the page reacted to"),
                cover=cover,
            )
    raise Stuck(cover)


class Refused(Exception):
    """The action was understood and NOT performed — an ambiguous choice, a
    control that is not what the model took it for. The message is for the
    model, and it carries what it needs to try again."""


async def _type(
    page: Any, target: Any, *, text: str, submit: bool
) -> browse_mod.Pressed:
    """REAL KEYSTROKES, for the reason `view_act` records: fill() fires one input
    event and no key events, which breaks widgets that listen for typing — and
    breaks a 2FA box outright. Only the way focus is obtained escalates.

    Carries the cover out for the same reason `_press` does: a field under a
    banner and a field that is simply inert are different problems."""
    note = ""
    cover = browse_mod.Cover()
    try:
        await target.click(timeout=ACT_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — a field that will not click may still focus
        clicked = False
        cover = await _uncover(page, target)
        if cover.dismissed:
            with contextlib.suppress(Exception):
                await target.click(timeout=ACT_TIMEOUT_MS)
                clicked = True
                note = browse_mod.COVERED_DISMISSED.format(by=cover.by)
        if not clicked:
            if not await _focus(target):
                raise Stuck(cover) from None
            note = "the field would not click, so aish focused it with the keyboard"
    await page.keyboard.press("ControlOrMeta+a")
    if text:
        await page.keyboard.type(text, delay=12)
    else:
        await page.keyboard.press("Delete")
    if submit:
        await page.keyboard.press("Enter")
    return browse_mod.Pressed(note=note, cover=cover)


async def _choose(target: Any, value: str) -> str:
    """Pick one option, having matched it HERE rather than on the snapshot.

    The options are read at choose time because carrying 312 of them on every
    snapshot is what pushed 51 real controls off the end of a page (#245)."""
    try:
        options = [
            (str(pair[0]), str(pair[1]))
            for pair in await target.evaluate(browse_mod.OPTIONS_JS)
        ]
    except Exception:  # noqa: BLE001 — an element with no options answers as one
        options = []
    if not options:
        # A `role=combobox` with no <option> children is a search box wearing a
        # dropdown's clothes. Saying so beats two select_option timeouts.
        raise Refused(
            "this is a search box, not a fixed list of options — type into it "
            "instead, and press the option that appears"
        )
    picked = browse_mod.match_option(options, value)
    if picked.problem:
        raise Refused(picked.problem)
    await target.select_option(value=picked.value, timeout=ACT_TIMEOUT_MS)
    return f"chose {picked.label!r}"


def browse_act(
    address: str,
    action: str,
    *,
    text: str = "",
    value: str = "",
    submit: bool = False,
    href: str = "",
    mutating: bool = False,
    topic: str = "",
    expect_download: bool = False,
    expect_epoch: int | None = None,
    key: str = "",
    timeout: float = 120.0,
) -> browse_mod.Snapshot:
    """Do one thing to the control the model NAMED, and hand back the page.

    **The name is re-resolved against the live page immediately before it is
    pressed.** The tag survives a re-render, which is what makes it a good
    handle — but a framework that reuses a row's DOM node for a different row
    hands the same tag to different content, and pressing it would be the right
    element and the wrong flight. Re-reading the controls costs one enumeration
    and turns that into a refusal with a fresh page.

    That re-read is also what enforces the card. `href` and `mutating` are what
    the GATE classified, from the snapshot the owner was shown; if the live
    control disagrees — it needs approval now and did not then, it has become a
    password field, its destination has changed — the action does not run. The
    thing the owner approved has to be the thing that happens.

    Nothing in here raises for a page reason. Every ending is a snapshot with a
    line saying what happened — a bare error string used to leave the model
    holding no page at all, which is how one stuck control turned into a lost
    session."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)

    async def job(owner: _Owner) -> browse_mod.Snapshot:
        session = await _session(owner, key, opening=False)
        page = session.page
        # Before anything is read or pressed, and INSIDE the owner loop: the
        # gate ran in the caller's thread, and another chat can navigate this
        # page in the gap between the card and here.
        if expect_epoch is not None and session.epoch != expect_epoch:
            raise BrowserUnavailable(PAGE_TAKEN)
        # Bound once so every ending below — refusal, stuck, stale name, or the
        # act itself — describes the page the same way. An error path that
        # silently un-narrowed the list would hand back a DIFFERENT selection
        # of controls from the one the model asked for, on the page it is about
        # to pick its next move from.
        shot = partial(_snapshot, match=topic)
        if action == "read":
            # Looking is not acting. The model needs a way to see the whole page
            # again without navigating to it — a `goto` to the same URL resets
            # an SPA's state, so "let me look properly" must not be a round trip
            # through the address bar.
            #
            # It is also the model's way to say GIVE IT A MOMENT. A read that
            # came back mid-spinner has exactly one honest next move, and it
            # must not be to keep re-reading until the loop detector stops the
            # task — so this one waits for a finished page.
            session.epoch += 1
            return await shot(owner, session, started_work=True)
        if action not in ("click", "type", "choose"):
            return await shot(owner, session, problem=f"unknown action {action!r}")
        # Narrowed the same way the listing the model read was, or the act
        # would go looking for the control on an UNNARROWED page and not find
        # the very one the narrowing existed to reach (#270). Falling back to
        # the address costs nothing: a name the matcher cannot see simply
        # leaves the selection in document order, which is what it was before.
        raw, _, _, _, _ = await _enumerate(page, topic or address)
        live = browse_mod.controls_from(raw)
        found = browse_mod.resolve(live, address)
        if found.control is None:
            session.epoch += 1
            return await shot(
                owner,
                session,
                problem=f"{found.problem} Here is the page as it is now.",
            )
        control = found.control
        n = control.n
        if control.kind == browse_mod.PASSWORD or (control.mutating and not mutating):
            # The page moved between the card and the press. Whatever the owner
            # said yes to, it was not this.
            session.epoch += 1
            return await shot(
                owner,
                session,
                problem=(
                    f"{control.address!r} is not the control that was approved "
                    "— the page changed under it and it now needs approval of "
                    "its own. Here is the page as it is now; ask again for what "
                    "you want."
                ),
            )
        # The destination the gate checked has to still be this control's
        # destination, or the link fallback in `_press` has nothing safe to aim
        # at. Pressing the element itself is still fine.
        approved_href = (
            href if control.kind == browse_mod.LINK and control.detail == href else ""
        )
        target, top = await _find(page, n)
        if target is None:
            # The document changed under the numbering — an SPA re-render, a
            # redirect, a timed refresh. Re-describing beats guessing.
            session.epoch += 1
            return await shot(
                owner,
                session,
                problem=(
                    f"{control.address!r} is not on this page any more — it "
                    "changed since you last saw it. Here it is as it is now; "
                    "act on something from THIS list."
                ),
            )
        gone = await _reachable_now(target)
        if gone:
            session.epoch += 1
            return await shot(
                owner,
                session,
                # The parenthesis is the OBSERVATION (`unreachable`'s closed
                # vocabulary) and it is all that is claimed. This used to add
                # "the menu or panel holding it has closed" — a cause none of
                # those reason codes establishes, the same retired guess as
                # `STUCK_NOT_COVERED`'s "something may be covering it".
                problem=(
                    f"{control.address!r} is still on the page but cannot be "
                    f"pressed now ({gone}). Here is the page as it is "
                    "now; act on something from THIS list."
                ),
            )
        await _centre(target)
        before = list(page.context.pages)
        session.epoch += 1
        pressed = browse_mod.Pressed()
        try:
            if action == "click":
                pressed = await _press(
                    page, target, mutating=mutating, href=approved_href if top else ""
                )
            elif action == "type":
                pressed = await _type(page, target, text=text, submit=submit)
            else:
                pressed = browse_mod.Pressed(note=await _choose(target, value))
        except Refused as exc:
            return await shot(owner, session, problem=str(exc))
        except Stuck as stuck:
            # The refusal NAMES what was in the way, or says plainly that
            # nothing was found in the way (#321). Those are different facts
            # with different repairs, and the one sentence that covered both
            # — "something may be covering it" — was a guess offered on every
            # stuck control, so it taught nothing on the pages where it was
            # right and misdirected on the pages where it was wrong.
            return await shot(
                owner,
                session,
                problem=(
                    browse_mod.COVERED_STUCK.format(
                        action=action, address=control.address, by=stuck.cover.by
                    )
                    if stuck.cover.by
                    else browse_mod.STUCK_NOT_COVERED.format(
                        action=action, address=control.address
                    )
                ),
                covered=stuck.cover,
            )
        except Exception as exc:  # noqa: BLE001 — a page reason, not a crash
            return await shot(
                owner,
                session,
                problem=(
                    f"could not {action} {control.address!r}: "
                    f"{type(exc).__name__}"
                ),
            )
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — nothing navigated, which is common
            pass
        await _dismiss_consent(page)
        page = await _adopt_new_tab(owner, key, page, before)
        # An action is exactly the moment a page is most likely to be BUSY
        # rather than finished — this is the read that lands on the results
        # page a search is still fetching.
        snapshot = await shot(
            owner,
            session,
            notice=pressed.note,
            started_work=True,
            covered=pressed.cover,
        )
        if expect_download and not snapshot.downloads:
            # Said HERE rather than at the gate, because it is only true here:
            # the press that works says nothing, and the press that produced no
            # file is the only one that needs the sentence (#271).
            snapshot.notice = (
                f"{snapshot.notice} {browse_mod.NO_FILE_YET}".strip()
            )
        return snapshot

    return _submit(job, timeout)


def browse_fill(
    steps: list[dict],
    *,
    mutating: bool = False,
    topic: str = "",
    expect_epoch: int | None = None,
    key: str = "",
    timeout: float = 240.0,
) -> browse_mod.Snapshot:
    """Fill in a form — several controls, then at most one press — as ONE act.

    A person searching for a flight sets origin, destination, both dates,
    passengers and cabin, then presses search. Doing that one call at a time
    cost the model six round trips and the owner six echo lines, and on a form
    where every field is a combobox it did not finish at all.

    **`fill` is the compound verb, and it is the reason this exists.** On these
    forms a destination box is not a text field: typing opens a list that did
    not exist when the batch was composed, so the model CANNOT name the option
    it will need. `fill` types, waits for the page to answer, and presses the
    option that matches — using the same ladder as `choose`, and only ever
    pressing something the page opened IN RESPONSE (`option`), never a control
    that merely happened to appear.

    **It stops at the first step it cannot carry out, and never skips one.**
    Order on these pages is a dependency statement, so "did 7, skipped 3, did
    the rest" is unreviewable against the card the owner approved — and the
    approved press is made only if every step before it verified. A batch that
    could not establish its values ends UNSENT, saying so."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)

    async def job(owner: _Owner) -> browse_mod.Snapshot:
        session = await _session(owner, key, opening=False)
        page = session.page
        # Before anything is read or pressed, and INSIDE the owner loop: the
        # gate ran in the caller's thread, and another chat can navigate this
        # page in the gap between the card and here.
        if expect_epoch is not None and session.epoch != expect_epoch:
            raise BrowserUnavailable(PAGE_TAKEN)
        ledger: list[str] = []
        dated = False   # a day cell was pressed: see `_stop`
        last = len(steps) - 1
        started_at = str(page.url or "")
        for index, step in enumerate(steps):
            asked = str(step.get("target", "") or "")
            verb = str(step.get("do") or step.get("action") or browse_mod.FILL).lower()
            value = str(step.get("value", "") or step.get("text", "") or "")
            # Narrowed the same way the listing the model read was — see
            # browse_act, same reasoning, and each step names its own control.
            raw, _, _, _, _ = await _enumerate(page, topic or asked)
            live = browse_mod.controls_from(raw)
            found = browse_mod.resolve(live, asked)
            control = found.control
            if control is None:
                return await _stop(
                    owner, session, ledger, index, len(steps),
                    f"{found.problem}", dated=dated,
                )
            if control.kind == browse_mod.PASSWORD:
                return await _stop(
                    owner, session, ledger, index, len(steps),
                    "it is a password field, and aish never types passwords",
                    dated=dated,
                )
            if control.mutating and not (index == last and mutating):
                return await _stop(
                    owner, session, ledger, index, len(steps),
                    f"{control.address!r} needs approval of its own and this "
                    "batch was not approved for it — the page changed under it",
                    dated=dated,
                )
            session.epoch += 1
            try:
                said = await _run_step(page, control, verb, value, live)
            except _StepFailed as exc:
                return await _stop(
                    owner, session, ledger, index, len(steps), str(exc), dated=dated
                )
            except Stuck as stuck:
                # A consent wall rendering mid-batch is a case this file
                # already names, so the batch gets the same account of it a
                # single action does (#321) — otherwise a covered control
                # arrives here as a bare `Exception` with an empty message.
                return await _stop(
                    owner, session, ledger, index, len(steps),
                    browse_mod.COVERED_STUCK.format(
                        action=verb, address=control.address, by=stuck.cover.by
                    ) if stuck.cover.by
                    else browse_mod.STUCK_NOT_COVERED.format(
                        action=verb, address=control.address
                    ),
                    dated=dated, covered=stuck.cover,
                )
            except Exception as exc:  # noqa: BLE001 — a page reason, not a crash
                return await _stop(
                    owner, session, ledger, index, len(steps),
                    f"could not {verb} it ({type(exc).__name__}: {exc})",
                    dated=dated,
                )
            ledger.append(f"{index + 1}. {said}")
            dated = dated or verb == "date"
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 — nothing navigated, which is common
                pass
            if str(page.url or "") != started_at and index != last:
                # The remaining steps were composed against a document that no
                # longer exists. The delta machinery sends the whole page on a
                # URL change, which is the right reply to standing somewhere new.
                return await _stop(
                    owner, session, ledger, index + 1, len(steps),
                    "the page navigated, so the rest of the batch was composed "
                    "against a page you are no longer on",
                    after=True, dated=dated,
                )
        # The last step of a form-fill is usually the press that sends it, so
        # this is the read that lands on a results page still being fetched.
        snapshot = await _snapshot(owner, session, match=topic, started_work=True)
        snapshot.ledger = ledger
        return snapshot

    return _submit(job, timeout)


async def _read_calendar(page: Any, n: int) -> dict:
    """The open picker, as the page describes it — or {"found": False}."""
    try:
        return await page.evaluate(browse_mod.CALENDAR_JS, {"n": int(n)}) or {}
    except Exception:  # noqa: BLE001 — a page that will not answer has no picker
        return {}


def _cells_of(grid: dict) -> list:
    return [
        browse_mod.Cell(
            tag=int(raw.get("tag", 0)),
            text=str(raw.get("text") or ""),
            label=str(raw.get("label") or ""),
            stamp=str(raw.get("stamp") or ""),
            disabled=bool(raw.get("disabled")),
            onscreen=bool(raw.get("onscreen")),
        )
        for raw in (grid.get("cells") or [])
    ]


def _grid_signature(grid: dict) -> tuple:
    """Enough of a picker to tell whether pressing an arrow changed it."""
    return (
        str(grid.get("heading") or ""),
        tuple(sorted((c.get("label") or c.get("text") or "") for c in grid.get("cells") or [])),
    )


async def _press_in_picker(page: Any, tag: int) -> None:
    target = page.locator(f'[data-aish-cell="{int(tag)}"]').first
    await _centre(target)
    await _press(page, target, mutating=False, href="")


async def _pick_date(page: Any, control: Any, value: str, before: list) -> str:
    """Set a date by pressing the day in the picker the field opens.

    **The cells are pressed sight-unseen, so the fence has to be narrow.** A
    date step may press only what is INSIDE the grid its own field opened, only
    a cell matching the date the owner's step named, plus that picker's own
    month arrows — and never anything that submits. A calendar sits inside the
    search form on most booking sites, and a `<button>` with no type attribute
    IS a submit button, so a next-month arrow can be a form submit nobody
    approved.

    Everything about the walk is a heuristic; nothing about the RESULT is. The
    field is read back afterwards, and a value that cannot be confirmed says so
    rather than being reported as done."""
    wanted = browse_mod.read_date(value)
    if wanted is None or not wanted.month:
        raise _StepFailed(
            f"{value!r} is not a date aish can read — give it as 2026-09-07"
        )
    grid = await _read_calendar(page, control.n)
    if not grid.get("found"):
        # Not open yet: pressing the field is what opens it. A range picker
        # that is ALREADY open must not be pressed again — that closes it, and
        # on most range widgets it resets the half-made selection.
        target, _top = await _find(page, control.n)
        if target is None:
            raise _StepFailed(f"{control.address!r} left the page mid-batch")
        await _centre(target)
        await _press(page, target, mutating=False, href="")
        await _settle(page)
        grid = await _read_calendar(page, control.n)
    if not (grid.get("cells") or []) and control.kind == browse_mod.FIELD:
        # NOT every date field is a picker. intercity.pl's asks for the date in
        # words — "podaj datę w formacie cztery cyfry roku - dwie cyfry
        # miesiąca…" — and is an ordinary text box: pressing it opens nothing,
        # and refusing was aish declining to do the easiest thing on the page.
        # Typed as ISO, then read back like any other value. The test is NO
        # DAY CELLS rather than no container: on intercity the field's own
        # wrapper matches a picker-ish selector, so "a grid was found" is true
        # and empty.
        box, _top = await _find(page, control.n)
        if box is None:
            raise _StepFailed(f"{control.address!r} left the page mid-batch")
        await _type(page, box, text=value, submit=False)
        await _settle(page)
        raw, _, _, _, _ = await _enumerate(page)
        said = _readback(
            browse_mod.controls_from(raw), control, value, kind="typed"
        )
        return f"{said} (no picker opened; typed the date instead)"
    if not grid.get("found"):
        raise _StepFailed(
            f"pressing {control.address!r} opened no date picker aish can read"
        )

    hops = 0
    seen: set = set()
    while True:
        cells = _cells_of(grid)
        heading = str(grid.get("heading") or "")
        pick = browse_mod.pick_day(cells, wanted, heading)
        if pick.tag is not None:
            await _press_in_picker(page, pick.tag)
            await _settle(page)
            raw, _, _, _, _ = await _enumerate(page)
            said = _readback(
                browse_mod.controls_from(raw), control, value, kind="picked"
            )
            walked = f", after walking {hops} month(s)" if hops else ""
            return f"{said} (pressed {pick.label or wanted.day!r}{walked})"
        if "not in the month" not in pick.problem:
            raise _StepFailed(f"{control.address!r}: {pick.problem}")
        # The heading first; failing that, the span the CELLS themselves state.
        on_show = browse_mod.months_on_show(cells, heading)
        if not on_show:
            raise _StepFailed(
                f"{control.address!r}: that date is not on the picker's open "
                "month, and the picker does not say which month it is showing, "
                "so aish will not walk it blind"
            )
        want = (wanted.year or on_show[-1][0], wanted.month)
        forward = on_show[-1] < want
        if not forward and want > on_show[0]:
            # Inside the span already and still not found: the day is simply
            # not offered (a sold-out date, a past date), and walking would
            # leave the month that DOES contain it.
            raise _StepFailed(
                f"{control.address!r}: {value!r} is on a month the picker is "
                "already showing, but that day cannot be chosen on this page"
            )
        shown_year, shown_month = on_show[0] if not forward else on_show[-1]
        arrow = next(
            (
                one for one in (grid.get("nav") or [])
                if browse_mod.month_step(str(one.get("name") or ""), forward=forward)
            ),
            None,
        )
        if arrow is None:
            raise _StepFailed(
                f"{control.address!r}: the picker is showing "
                f"{heading or 'another month'} and aish cannot find its "
                f"{'next' if forward else 'previous'}-month arrow"
            )
        if arrow.get("submits"):
            raise _StepFailed(
                f"{control.address!r}: this picker's "
                f"{'next' if forward else 'previous'}-month control "
                f"({str(arrow.get('name'))!r}) is a form submit button, so "
                "pressing it could send the form. Move the month yourself"
            )
        hops += 1
        if hops > browse_mod.MONTH_HOPS:
            raise _StepFailed(
                f"{control.address!r}: walked {browse_mod.MONTH_HOPS} months "
                "without reaching that date"
            )
        signature = _grid_signature(grid)
        if signature in seen:
            raise _StepFailed(
                f"{control.address!r}: the picker stopped changing at "
                f"{heading or 'this month'} — it does not go that far"
            )
        seen.add(signature)
        await _press_in_picker(page, int(arrow["tag"]))
        await _settle(page)
        grid = await _read_calendar(page, control.n)
        if not grid.get("found"):
            raise _StepFailed(
                f"{control.address!r}: the picker closed while moving months"
            )


class _StepFailed(Exception):
    """A batch step the page would not take. Carries what to tell the model."""


async def _run_step(
    page: Any, control: Any, verb: str, value: str, before: list
) -> str:
    """Carry out one step and say what the control HOLDS afterwards."""
    if verb == "date":
        # It finds and opens its own widget, and must NOT re-press a field
        # whose picker is already standing open.
        return await _pick_date(page, control, value, before)
    target, _top = await _find(page, control.n)
    if target is None:
        raise _StepFailed(f"{control.address!r} left the page mid-batch")
    gone = await _reachable_now(target)
    if gone:
        raise _StepFailed(f"{control.address!r} cannot be pressed now ({gone})")
    await _centre(target)
    if verb == "choose":
        await _choose(target, value)
        return f"{control.address!r} ← {value!r} (from its list)"
    if verb in ("click", "check"):
        await _press(page, target, mutating=control.mutating, href="")
        return f"pressed {control.address!r}"
    await _type(page, target, text=value, submit=False)
    return await _commit_suggestion(page, control, value, before)


async def _commit_suggestion(
    page: Any, control: Any, value: str, before: list
) -> str:
    """Typing is not choosing. If the page answered with a list, press the
    entry that matches; if it did not, the text stands on its own."""
    # The list is fetched, not rendered locally, so reading straight after the
    # keystrokes reads the page BEFORE it answered — and "no suggestions
    # appeared" would then be a race reported as a fact. Snapshots have always
    # settled; a batch's intermediate reads did not, which is precisely where
    # the readback this design rests on would have been raced.
    await _settle(page)
    raw, _, _, _, _ = await _enumerate(page)
    after = browse_mod.controls_from(raw)
    was = {c.address for c in before}
    offered = [c for c in after if c.option and c.address not in was]
    if not offered:
        return _readback(after, control, value)
    picked = browse_mod.match_option([(c.name, c.address) for c in offered], value)
    if picked.problem:
        raise _StepFailed(
            f"{control.address!r} offered {len(offered)} suggestions and "
            f"{picked.problem} Nothing was chosen"
        )
    chosen = next(c for c in after if c.address == picked.value)
    if chosen.mutating:
        raise _StepFailed(
            f"the suggestion {chosen.address!r} needs approval of its own, so "
            "it was not pressed"
        )
    element, _top = await _find(page, chosen.n)
    if element is None:
        raise _StepFailed(f"the suggestion {chosen.address!r} vanished before it could be pressed")
    await _centre(element)
    await _press(page, element, mutating=False, href="")
    await _settle(page)
    raw, _, _, _, _ = await _enumerate(page)
    said = _readback(
        browse_mod.controls_from(raw), control, picked.label, kind="picked"
    )
    return f"{said} (picked {picked.label!r} from {len(offered)} suggestions)"


def _held(controls: list, address: str) -> str | None:
    """What the control actually holds now — read back, never assumed.

    None means UNREADABLE, which is a third outcome and not a synonym for
    empty: only a `field` carries `currently:`, and a great many date and
    passenger "fields" on booking sites are a button or a div showing text. A
    caller that folds None into the asked value states a value nobody verified,
    in aish's own voice, above the untrusted banner."""
    for control in controls:
        if control.address == address:
            if control.detail.startswith("currently: "):
                return control.detail[len("currently: "):]
            return None
    return None


def _readback(controls: list, control: Any, value: str, *, kind: str = "typed") -> str:
    """One ledger line, saying whether the value was VERIFIED or merely done.

    The distinction is the point: "the field holds X" and "aish did X and the
    control says nothing back" are different claims, and only the first is
    evidence. Collapsing them is how a ledger states a value nobody checked."""
    held = _held(controls, control.address)
    if held:
        return f"{control.address!r} ← {held!r}"
    return (
        f"{control.address!r} ← {value!r} ({kind}; the control shows nothing "
        "readable back)"
    )


async def _stop(
    owner: _Owner,
    session: _Session,
    ledger: list[str],
    done: int,
    total: int,
    why: str,
    *,
    after: bool = False,
    dated: bool = False,
    covered: browse_mod.Cover | None = None,
) -> browse_mod.Snapshot:
    """End a batch part-way and say exactly where it got to.

    The unspent approval is stated out loud: a card said "send this form", and
    if that press was never made the model must not report the form as sent —
    nor may the next batch inherit the yes."""
    step = done + (0 if after else 1)
    ledger = list(ledger)
    ledger.append(f"{step}. STOPPED: {why}")
    if step < total:
        ledger.append(
            f"steps {step + 1}–{total} were not attempted, and any approved "
            "press in them was NOT made"
        )
    if dated:
        # Re-running a fill is one fill; re-running a DATE is not. A range
        # picker takes the first press as the start and the second as the end,
        # so a retry composed as though nothing had happened silently sets the
        # wrong half of somebody's trip.
        ledger.append(
            "a date was already set before this stopped, so the picker may be "
            "holding half a range — check what the date fields hold before "
            "setting them again, and clear them if they are wrong"
        )
    session.epoch += 1
    snapshot = await _snapshot(
        owner,
        session,
        problem=(
            f"the batch stopped at step {step} of {total} — {why}. Here is the "
            "page as it is now; carry on from what the steps below report."
        ),
        covered=covered,
    )
    snapshot.ledger = ledger
    return snapshot


# --------------------------------------------- watching a chat's own tab (#289)
#
# A window onto the page the MODEL is driving, for an owner who otherwise has
# no way to see it. Read-only, and deliberately NOT the remote view.
#
# **It is not the remote view because it cannot be.** `_open_view` tears the
# whole read context down and relaunches it view-shaped: a fixed viewport and
# `device_scale_factor` are LAUNCH arguments on a persistent context, and Chrome
# locks the profile directory, so there is no way to point the existing sheet at
# a browse tab. `close_now()` empties `browse_pages`, so opening `/browser`
# destroys every chat's page. Watch mode must never do that — it is a window
# onto a tab that already exists, so it photographs that tab where it stands.
#
# **What it does to the page, in full: it takes a screenshot, and it reads the
# address, the title, and whether the tab is still open.** Nothing else, and
# `TestWatchingTheTabTheModelIsDriving` pins that as a LIST rather than as an
# intention — its fake page raises on every other attribute, so a verb this
# ever grows fails there first. No resize, no scroll, no click, no
# keyboard, and nothing injected — not even the activity probe `_WATCH_JS`
# installs for the remote view, which is why this polls on a plain interval
# rather than reusing `watch_step`. The reason is semantic and must not be
# "improved" on: every gate decision downstream is made against the page as the
# MODEL was shown it, so a human scrolling changes the reachable set and a
# resize crosses a responsive breakpoint and renames controls. The act-time
# fence would then correctly refuse each act, and the flow becomes a refusal
# storm that reads as the model flailing. Temporal separation is the only fix,
# and stepping in is a later slice with its own approval.
#
# **A picture is a VIEW, never a control.** Nothing in the browse gate, the
# act-time fence, the irreversible refusals or the host grant may be relaxed,
# widened or checked less carefully because the owner can now watch — #295 P2
# is explicit that "he will see it" is not a safety argument, and this is
# exactly the place someone would be tempted to make it.
#
# **Nothing is stored.** `_evidence_frame` writes one file per snapshot into a
# store with its own cap (#318); a watch stream at one frame a second would
# turn that store over in minutes. These bytes go to the socket and nowhere
# else — no `media.store` call exists on this path.
#
# **And it never keeps a tab alive.** `session.touched` is deliberately not
# updated here: watching consents to nothing and changes nothing, including how
# long the reaper leaves the page open.

# Why a picture is not being sent. A closed vocabulary because the client turns
# each one into a different sentence, and they route to different repairs.
WATCH_NO_BROWSER = "browser-off"   # disabled, or unavailable on preview
WATCH_HANDS = "hands"              # the owner's own /browser view has the browser
WATCH_NO_PAGE = "no-page"          # this chat is not on a page right now
WATCH_FAILED = "failed"            # the capture did not come back


@dataclass
class WatchFrame:
    """One live look at the page a chat is driving. Never stored, never logged.

    Deliberately carries no width/height: the remote view needs those to map a
    tap into page coordinates, and there is no tap here. Not carrying them is
    the cheapest possible statement that nothing on this path can address a
    point on the page."""

    jpeg: bytes
    url: str
    title: str


def browse_watch_frame(
    key: str = "", *, timeout: float = 20.0
) -> tuple[WatchFrame | None, str]:
    """(a picture of this chat's tab, or the reason there is none).

    **The one refusal that is a safety property is the first branch:** never
    while the owner's own hands are on the browser. `/browser` outranks watch
    mode — it takes the whole context — and while `owner.view` is set there is
    nothing of the model's left to photograph anyway. Written here, at the
    capture, so it is one line rather than an ordering the callers have to keep.

    A watcher NEVER launches Chrome. A poll that started the thing it polls
    would bring a browser up on a machine nobody asked, which is the same rule
    `_no_browser_yet` already keeps for the remote view's probe."""
    if unavailable_reason():
        return None, WATCH_NO_BROWSER
    if _no_browser_yet():
        return None, WATCH_NO_PAGE

    async def job(owner: _Owner) -> tuple[WatchFrame | None, str]:
        if owner.view is not None:
            return None, WATCH_HANDS
        session = owner.browse_pages.get(key)
        if session is None:
            return None, WATCH_NO_PAGE
        page = session.page
        try:
            if page.is_closed():
                return None, WATCH_NO_PAGE
        except Exception:  # noqa: BLE001 — a page that cannot answer is gone
            return None, WATCH_NO_PAGE
        jpeg = await page.screenshot(
            type="jpeg", quality=FRAME_JPEG_QUALITY, timeout=FRAME_TIMEOUT_MS
        )
        return (
            WatchFrame(
                jpeg=bytes(jpeg),
                url=str(page.url or ""),
                title=str(await page.title() or ""),
            ),
            "",
        )

    try:
        return _submit(job, timeout)
    except Exception:  # noqa: BLE001 — a look that failed is a look that failed
        return None, WATCH_FAILED


def browse_tab_count() -> int:
    """How many chats hold a browse tab right now.

    Only ever used to SAY WHAT IS ABOUT TO BE CLOSED: opening `/browser` tears
    the context down and takes every one of these with it, and doing that
    silently is the thing #289 asks it not to do. Never launches Chrome to
    answer — with no browser running there is nothing to close."""
    if _no_browser_yet():
        return 0

    async def job(owner: _Owner) -> int:
        alive = 0
        for session in owner.browse_pages.values():
            try:
                if not session.page.is_closed():
                    alive += 1
            except Exception:  # noqa: BLE001 — a page that cannot answer is gone
                continue
        return alive

    try:
        return int(_submit(job, 10.0))
    except Exception:  # noqa: BLE001 — a count that cannot be taken claims nothing
        return 0


def browse_fields(*, key: str = "", timeout: float = 20.0) -> list:
    """The controls on the live page, without the page — for a card that has to
    say what a form currently HOLDS.

    Deliberately a fresh read rather than the snapshot the gate already has.
    The whole reason the card needs this is that filling needs no approval, so
    values are set in one call and submitted in another, and a page is free to
    reset a date in the gap; a card drawn from the last picture would show
    exactly the stale values it exists to catch. No text, no settle — this is
    an enumeration and nothing else, because it runs inside the gate with the
    owner waiting.

    Returns [] rather than raising: a card that cannot read the form is worse
    without its button than with it, so the caller falls back to what it knows
    and says which it is showing."""
    if unavailable_reason():
        return []

    async def job(owner: _Owner) -> list:
        session = await _session(owner, key, opening=False)
        raw, _, _, _, _ = await _enumerate(session.page)
        return browse_mod.controls_from(raw)

    try:
        return _submit(job, timeout)
    except Exception:  # noqa: BLE001 — no page, no session, a torn-down browser
        return []


def browse_close(key: str = "") -> None:
    """End ONE CHAT's session. The context and the profile stay."""

    async def job(owner: _Owner) -> None:
        await _drop(owner, key)

    with contextlib.suppress(Exception):
        _submit(job, 30.0)


def view_close() -> list[str]:
    """End the view. Returns the hosts it watched a sign-in happen at.

    Nothing is ASKED any more, and that is the change. Whether he is signed in
    stopped being a fact anybody asserts the moment aish started reading it off
    the page: a page that has stopped asking for a password, after one went in,
    is a sign-in — observed, not claimed. Three versions of the question were
    wrong before this (inferred from a visit, inferred from a close, and then
    offered as the whole browsing history under one batch yes), and the fourth
    version of a question nobody can answer reliably is not the fix.

    The list comes back only so the caller can say what it saw."""

    async def job(owner: _Owner) -> list[str]:
        watched = sorted(owner.signed_in_here)
        owner.view = None
        owner.view_hosts = set()
        owner.password_hosts = set()
        owner.signed_in_here = set()
        await owner.close_now()  # next read relaunches off-screen at full size
        return watched

    return _submit(job, 60.0)


def _view_was_cold() -> bool:
    global _OWNER
    if _OWNER is None or not _OWNER.is_alive():
        return False
    return bool(getattr(_OWNER.owner, "view_cold", False))  # type: ignore[attr-defined]


def view_is_open() -> bool:
    global _OWNER
    if _OWNER is None or not _OWNER.is_alive():
        return False
    try:
        async def check(owner):
            return owner.view is not None

        return bool(_submit(check, timeout=15.0))
    except Exception:  # noqa: BLE001 — a wedged browser is not an open view
        return False


def shutdown() -> None:
    """Close the browser now (tests, and `/browser close`)."""
    if _OWNER is None or not _OWNER.is_alive():
        return
    try:
        async def close(owner):
            await owner.close_now()

        _submit(close, timeout=30.0)
    except Exception:  # noqa: BLE001 — shutdown is best effort
        pass
