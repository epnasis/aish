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
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import browse as browse_mod

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
_BLOCK_STATUS = (401, 403, 405, 429, 503)


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
    return status in _BLOCK_STATUS


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


def logins_file() -> Path:
    return state_dir() / "browser" / "logins.txt"


def search_logins_file() -> Path:
    """What the SEARCH profile is signed into — a separate file, deliberately.

    `logins.txt` is not a note, it is what `is_logged_in` answers from and
    therefore what makes `_login_gate` fire on a `read_url`. The search profile
    is never used by `read_url`, so a sign-in there must not be able to change
    what the owner's reads do — and equally, his sign-ins must not make the
    search browser look signed in when it is not. Two profiles, two records,
    and this one is READ FOR DISPLAY ONLY: nothing gates on it."""
    return state_dir() / "browser" / "search-logins.txt"


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

def host_of(url: str) -> str:
    """Bare hostname, `www.` stripped, lowercased. "" when unparseable."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def logged_in_hosts() -> set[str]:
    """Hosts the owner signed into at a window aish opened for them.

    Recorded from what they NAVIGATED to during a login session, not from the
    cookie jar: a jar is mostly third-party trackers, and "domains I logged
    into" is a claim only the owner's own navigation can support."""
    try:
        raw = logins_file().read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in raw.splitlines() if line.strip()}


def is_logged_in(url: str) -> str:
    """The recorded login host this URL belongs to, or "".

    Suffix match, so a login at `allegro.pl` also covers `allegro.pl` subdomains
    without this module needing a public-suffix list to find the registrable
    domain (getting that wrong in the lenient direction would gate too little,
    which is the direction that matters)."""
    host = host_of(url)
    if not host:
        return ""
    for known in logged_in_hosts():
        if host == known or host.endswith("." + known):
            return known
    return ""


def _remember_logins(hosts: set[str], *, cold: bool = False) -> None:
    if not hosts:
        return
    path = search_logins_file() if cold else logins_file()
    known = search_logged_in_hosts() if cold else logged_in_hosts()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(known | hosts)) + "\n", encoding="utf-8")


def forget_login(host: str) -> bool:
    """Drop a host from the login record. Does NOT clear its cookies — the
    owner's session is theirs to end, at the site, in a window."""
    host = host_of("https://" + host) or host.strip().lower()
    known = logged_in_hosts()
    if host not in known:
        return False
    logins_file().write_text("\n".join(sorted(known - {host})) + "\n", encoding="utf-8")
    return True


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


async def _settle(page: Any) -> None:
    """Wait until the page stops changing, or SETTLE_MAX_MS, whichever first.

    Three signals, cheapest first: the network going quiet, the document
    reporting `complete`, and finally the DOM itself going still — which is the
    one that catches a page whose skeleton has loaded but whose content is
    still being written in. Every wait is bounded: a page that never settles
    (a live ticker, a spinner) must still produce a frame."""
    try:
        await page.wait_for_load_state("networkidle", timeout=SETTLE_MAX_MS)
    except Exception:  # noqa: BLE001 — a chatty page never goes idle; carry on
        pass
    try:
        await page.wait_for_function(
            """() => new Promise(done => {
                 if (document.readyState !== 'complete') return done(false);
                 let mutations = 0;
                 const observer = new MutationObserver(() => { mutations++; });
                 observer.observe(document.documentElement,
                   { childList: true, subtree: true, characterData: true });
                 setTimeout(() => { observer.disconnect(); done(mutations === 0); },
                   QUIET);
               })""".replace("QUIET", str(SETTLE_QUIET_MS)),
            timeout=SETTLE_MAX_MS,
        )
    except Exception:  # noqa: BLE001 — bounded: an unsettleable page is still shown
        pass


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


async def _has_password_field(page: Any) -> bool:
    """Is the RENDERED page asking for a password?

    Playwright's selector engine pierces open shadow roots, so this sees a form
    the serialized HTML does not — the same reason `Page.text` is taken from the
    DOM and not from `page.content()`."""
    try:
        return (await page.query_selector("input[type=password]")) is not None
    except Exception:  # noqa: BLE001 — a page that will not answer is not a wall
        return False


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
_FOCUS_JS = """() => {
  const a = document.activeElement;
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


async def _dismiss_consent(page: Any) -> None:
    for selector in _CONSENT_SELECTORS:
        try:
            button = page.locator(selector).first
            if await button.is_visible(timeout=800):
                await button.click(timeout=2_000)
                await page.wait_for_timeout(1_200)
                return
        except Exception:  # noqa: BLE001 — best effort; a missing banner is the norm
            continue


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
        self.view_hosts: set[str] = set()
        self.view_touched = 0.0
        self.busy = 0
        self.notice = ""   # something native happened that the frame cannot show
        # Counts documents, not URLs. A logout that lands back on a similar
        # address, an SPA route change, or a plain reload all replace the
        # document without necessarily changing `url` — and the owner watched
        # his zoom survive a Google logout because of exactly that.
        self.navigations = 0
        self.pending_signin = ""   # host a password was submitted to
        self.pending_nav = -1      # the navigation count when that happened
        self.last_url = ""         # where the view was, so it can be reopened
        # The page the MODEL is driving (#237). A second page on the same
        # context, never the view's: the view is the owner's hands at a phone
        # viewport, and the two must not fight over one page. Held on the owner
        # because a browse session is exactly the thing that has to outlive a
        # single job — click, read, click again, all on the same document.
        self.browse_page: Any = None
        self.browse_epoch = 0
        self.browse_touched = 0.0
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

    def take_downloads(self, *, read_page: Any = None) -> list[Any]:
        """Drain the downloads belonging to one caller.

        A read takes its own page's; the browse session takes everything that is
        not some read's page, which is what makes an ephemeral popup count."""
        keep: list[tuple[Any, Any]] = []
        mine: list[Any] = []
        for page, download in self.downloads:
            if page is read_page or (read_page is None and page not in self.read_pages):
                mine.append(download)
            else:
                keep.append((page, download))
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
            if self.busy:
                continue
            if not self.held():
                await self._close()

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
        page = self.browse_page
        if page is not None and now - self.browse_touched <= BROWSE_MAX_IDLE:
            try:
                return not page.is_closed()
            except Exception:  # noqa: BLE001 — a page that cannot answer is gone
                return False
        return False

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
        global _LAST_SNAPSHOT
        self.view = None  # a closed context has no page left to drive
        self.browse_page = None
        # The snapshot is what `browse_is_open` answers from, and a page that no
        # longer exists must not read as an open session (#248).
        _LAST_SNAPSHOT = None
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

    return asyncio.run_coroutine_threadsafe(run(), owner.loop).result(timeout=timeout)


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
        if forget_login(rest):
            return (
                f"{rest} is no longer treated as signed in — reads of it stop "
                "asking for approval. Its cookies are untouched; sign out at "
                "the site itself in /browser if you meant to end the session."
            )
        return f"{rest} was not recorded as signed in."

    if verb == "close":
        shutdown()
        return "browser closed."

    if verb == "search":
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
                    "/browser search <url>   sign this profile in (opens the "
                    "remote view on it)",
                ]
            )
        return (
            "opening the search profile at "
            + (rest if rest.startswith(("http://", "https://")) else "https://" + rest)
        )

    if not arg:
        reason = unavailable_reason()
        hosts = sorted(logged_in_hosts())
        lines = [
            f"profile: {profile_dir()}",
            f"status:  {reason or 'ready'}",
            f"stealth: {'on' if stealth() else 'off'}",
            "signed in: " + (", ".join(hosts) if hosts else "(nothing yet)"),
            "",
            "/browser <url>       open a real window there so you can sign in",
            "/browser forget <host>  stop treating a site as signed in",
            "/browser search      the separate profile searches are read with",
            "/browser close       shut the browser down now",
        ]
        return "\n".join(lines)

    url = arg if arg.startswith(("http://", "https://")) else "https://" + arg
    if verb in ("login", "open"):
        url = rest if rest.startswith(("http://", "https://")) else "https://" + rest
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
    return Frame(
        jpeg=await page.screenshot(type="jpeg", quality=VIEW_JPEG_QUALITY),
        url=page.url or "",
        title=(await page.title()) or "",
        width=size["width"],
        height=size["height"],
        focus=await _focus_info(page, click),
        nav=owner.navigations,
    )


def _note_visit(owner: _Owner, url: str) -> None:
    """Remember a host the owner VISITED. Visiting is not signing in.

    This used to write straight to logins.txt, on the reasoning that gating a
    merely-visited site errs safe. It does not: browsing to allegro.pl and
    closing the sheet marked it signed-in, so every later read of the site the
    whole feature exists for asked for approval — friction on the main path,
    and a claim about the owner's account that was simply untrue.

    A login is a thing only the owner can confirm, so `view_close` hands these
    back and the UI ASKS. Nothing here writes the record."""
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
    # The scheme, which `view_act(goto)` and `browse_open` both add and this did
    # not. `/browser eon.pl` from the web app therefore NEVER opened: the web
    # sends the typed line straight here, Chrome refuses a schemeless address,
    # and the view came back `about:blank` with "could not open eon.pl (Error)".
    # Only the CLI worked, because `command()` normalises before it calls this
    # — so the one surface the owner actually uses was the broken one.
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
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


def view_settled_frame(timeout: float = 30.0) -> Frame | None:
    """Capture again once the page has gone quiet, or None if there is no view.

    The FOLLOW-UP to a fast frame. Its job is to be right rather than prompt;
    the caller only forwards it if it actually differs from what was shown."""

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
            target = str(kwargs.get("url", ""))
            if not target.startswith(("http://", "https://")):
                target = "https://" + target
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
        # The page MOVED after a password went in, which is what a successful
        # sign-in looks like from out here. Ask now, about this host.
        if owner.pending_signin and owner.navigations > owner.pending_nav:
            frame.signin = owner.pending_signin
            owner.pending_signin = ""
        if owner.notice:
            frame.error = owner.notice
            owner.notice = ""
        return frame

    return _submit(job, 120.0)


# --------------------------------------------------- the model's own session

# The last snapshot handed to the model, module-level so the APPROVAL GATE can
# name the control before it runs — "click 'Zapłać' on eon.pl" is the review the
# owner needs, and "click element 7" is not. Written on the owner thread, read
# from the agent's; a stale read costs a less specific card, never a wrong act.
_LAST_SNAPSHOT: browse_mod.Snapshot | None = None

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


async def _settled_text(page: Any, *, tries: int = 3) -> str:
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
    await _settle(page)
    text = await _body_text(page)
    for _ in range(tries - 1):
        if text and not browse_mod.still_loading(text):
            break
        await page.wait_for_timeout(SETTLE_MS)
        text = await _body_text(page) or text
    return text


async def _save_downloads(owner: _Owner, *, read_page: Any = None) -> list[str]:
    """Write whatever this caller downloaded, and say where it went."""
    pending = owner.take_downloads(read_page=read_page)
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


async def _enumerate(page: Any) -> tuple[list[dict], int, int]:
    """Every control on the page, across its frames, in one numbering.

    The count continues across frames rather than restarting, so `[14]` means
    one thing on this page however many documents it is made of — and acting
    finds it by searching the frames for the tag."""
    raw: list[dict] = []
    matched = unreached = 0
    options = {
        "max": browse_mod.MAX_CONTROLS,
        "nameMax": browse_mod.NAME_MAX_CHARS,
        "inlineChoices": browse_mod.CHOICE_INLINE_MAX,
        "offset": 0,
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
    return raw, matched, unreached


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


async def _snapshot(
    owner: _Owner, page: Any, *, problem: str = "", notice: str = "", asked: str = ""
) -> browse_mod.Snapshot:
    """The page as the model receives it: what it says, and what it can press."""
    global _LAST_SNAPSHOT
    text = await _without_option_floods(page, await _settled_text(page))
    raw, matched, unreached = await _enumerate(page)
    controls = browse_mod.controls_from(raw)
    # Deliberately NOT narrowed to <main>: reads narrow for budget, but the
    # control the model is looking for is very often in the header the narrowing
    # would drop — "Przełącz lokal" sits beside the account name, not in <main>.
    snapshot = browse_mod.Snapshot(
        url=str(page.url or ""),
        title=str(await page.title() or ""),
        text=text,
        controls=controls,
        hidden=max(0, matched - len(controls)),
        unreachable=unreached,
        epoch=owner.browse_epoch,
        problem=problem,
        notice=notice,
        downloads=await _save_downloads(owner),
        asked=asked if browse_mod.landed_elsewhere(asked, str(page.url or "")) else "",
    )
    _LAST_SNAPSHOT = snapshot
    owner.browse_touched = time.monotonic()
    return snapshot


async def _browse_page(owner: _Owner, *, opening: bool) -> Any:
    if owner.view is not None:
        # The owner's hands outrank the model's. Reusing his page would steal the
        # login he is mid-way through, and his viewport would silently change
        # what the model reads (the same reasoning as `read`).
        raise BrowserUnavailable(DRIVEN_BY_HAND)
    page = owner.browse_page
    if page is not None and not page.is_closed():
        return page
    if not opening:
        # The idle reaper can collect the context between turns. Nothing about
        # that is the model's fault or the owner's business — but an index from
        # the old document means nothing on a fresh one, so this is a refusal
        # with instructions rather than a silent reopen at a guessed URL.
        raise BrowserUnavailable(NOTHING_OPEN)
    context = await owner.context()
    page = await context.new_page()
    owner.browse_page = page
    return page


async def _adopt_new_tab(owner: _Owner, page: Any, before: list[Any]) -> Any:
    """A control that opened a new tab moves the session to it.

    Otherwise the model presses "Pobierz e-fakturę", the document opens beside
    it, and the snapshot faithfully reports the page it was already on — which
    reads as "nothing happened"."""
    try:
        opened = [p for p in page.context.pages if p not in before and not p.is_closed()]
    except Exception:  # noqa: BLE001 — no context to ask is no new tab
        return page
    if not opened:
        return page
    fresh = opened[-1]
    try:
        await fresh.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — a tab that never settles is still the tab
        pass
    owner.browse_page = fresh
    return fresh


def browse_open(url: str, *, timeout: float = 120.0) -> browse_mod.Snapshot:
    """Open `url` in the model's session and describe what is there."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async def job(owner: _Owner) -> browse_mod.Snapshot:
        page = await _browse_page(owner, opening=True)
        owner.browse_epoch += 1
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await _dismiss_consent(page)
        return await _snapshot(owner, page, asked=url)

    return _submit(job, timeout)


class Stuck(Exception):
    """Listed, still on the page, and it will not take the action."""


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


async def _focus(target: Any) -> bool:
    """Focus it, and say whether focus actually landed.

    Never assumed: a blind `keyboard.press("Enter")` after a focus that did not
    take goes to the document, and on a page with a form that is a submit
    nobody asked for."""
    try:
        return bool(
            await target.evaluate(
                "(el) => { el.focus(); return document.activeElement === el"
                " || el.contains(document.activeElement); }"
            )
        )
    except Exception:  # noqa: BLE001 — an element that will not focus has not
        return False


async def _press(page: Any, target: Any, *, mutating: bool, href: str) -> str:
    """Press it, escalating cheaply. Returns a note about HOW, or raises Stuck.

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
    something that spends or deletes, and the snapshot says it happened."""
    with contextlib.suppress(Exception):
        await target.click(timeout=ACT_TIMEOUT_MS)
        return ""
    if await _focus(target):
        with contextlib.suppress(Exception):
            await page.keyboard.press("Enter")
            return "the click would not land, so aish pressed it with the keyboard"
    if href:
        await page.goto(href, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        return (
            "the link would not click, so aish opened the destination the page "
            f"declares for it ({href})"
        )
    if not mutating:
        with contextlib.suppress(Exception):
            await target.dispatch_event("click")
            return (
                "the click would not land, so aish dispatched the event straight "
                "to the control — the page may not have registered it as a real "
                "press"
            )
    raise Stuck()


class Refused(Exception):
    """The action was understood and NOT performed — an ambiguous choice, a
    control that is not what the model took it for. The message is for the
    model, and it carries what it needs to try again."""


async def _type(page: Any, target: Any, *, text: str, submit: bool) -> str:
    """REAL KEYSTROKES, for the reason `view_act` records: fill() fires one input
    event and no key events, which breaks widgets that listen for typing — and
    breaks a 2FA box outright. Only the way focus is obtained escalates."""
    note = ""
    try:
        await target.click(timeout=ACT_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — a field that will not click may still focus
        if not await _focus(target):
            raise Stuck() from None
        note = "the field would not click, so aish focused it with the keyboard"
    await page.keyboard.press("ControlOrMeta+a")
    if text:
        await page.keyboard.type(text, delay=12)
    else:
        await page.keyboard.press("Delete")
    if submit:
        await page.keyboard.press("Enter")
    return note


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
    n: int,
    action: str,
    *,
    text: str = "",
    value: str = "",
    submit: bool = False,
    href: str = "",
    mutating: bool = False,
    timeout: float = 120.0,
) -> browse_mod.Snapshot:
    """Do one thing to control `n`, and hand back the page it produced.

    Every action re-enumerates afterwards, so the numbers the model reads are
    always the ones on the document in front of it.

    `href` and `mutating` come from the SNAPSHOT's control, never from the live
    DOM: the thing the gate classified has to be the thing that runs, or the
    fallback in `_press` becomes a way around the card.

    Nothing in here raises for a page reason. Every ending is a snapshot with a
    line saying what happened — a bare error string used to leave the model
    holding no page at all, which is how one stuck control turned into a lost
    session."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)

    async def job(owner: _Owner) -> browse_mod.Snapshot:
        page = await _browse_page(owner, opening=False)
        target, top = await _find(page, n)
        if target is None:
            # The document changed under the numbering — an SPA re-render, a
            # redirect, a timed refresh. Re-describing beats guessing.
            owner.browse_epoch += 1
            return await _snapshot(
                owner,
                page,
                problem=(
                    f"there is no control [{n}] on this page any more — it "
                    "changed since you last saw it. Here it is as it is now; "
                    "pick a number from THIS list."
                ),
            )
        if action not in ("click", "type", "choose"):
            return await _snapshot(owner, page, problem=f"unknown action {action!r}")
        gone = await _reachable_now(target)
        if gone:
            owner.browse_epoch += 1
            return await _snapshot(
                owner,
                page,
                problem=(
                    f"control [{n}] is still on the page but cannot be pressed "
                    f"now ({gone}) — the menu or panel holding it has closed "
                    "since you last saw it. Here is the page as it is now; pick "
                    "a number from THIS list."
                ),
            )
        await _centre(target)
        before = list(page.context.pages)
        owner.browse_epoch += 1
        notice = ""
        try:
            if action == "click":
                notice = await _press(
                    page, target, mutating=mutating, href=href if top else ""
                )
            elif action == "type":
                notice = await _type(page, target, text=text, submit=submit)
            else:
                notice = await _choose(target, value)
        except Refused as exc:
            return await _snapshot(owner, page, problem=str(exc))
        except Stuck:
            return await _snapshot(
                owner,
                page,
                problem=(
                    f"aish could not {action} control [{n}] — it is on the page "
                    "and would not take the action, by click, by keyboard, or "
                    "otherwise. Something may be covering it. Try the control "
                    "that closes whatever is over the page, or another route to "
                    "the same thing."
                ),
            )
        except Exception as exc:  # noqa: BLE001 — a page reason, not a crash
            return await _snapshot(
                owner,
                page,
                problem=f"could not {action} control [{n}]: {type(exc).__name__}",
            )
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — nothing navigated, which is common
            pass
        await _dismiss_consent(page)
        page = await _adopt_new_tab(owner, page, before)
        return await _snapshot(owner, page, notice=notice)

    return _submit(job, timeout)


def browse_close() -> None:
    """End the model's session. The context and the profile stay."""

    async def job(owner: _Owner) -> None:
        global _LAST_SNAPSHOT
        page, owner.browse_page = owner.browse_page, None
        _LAST_SNAPSHOT = None
        if page is not None and not page.is_closed():
            with contextlib.suppress(Exception):
                await page.close()

    with contextlib.suppress(Exception):
        _submit(job, 30.0)


def browse_current() -> browse_mod.Snapshot | None:
    """The last page the model was shown — what the approval gate reads to name
    the control it is asking about."""
    return _LAST_SNAPSHOT


def browse_is_open() -> bool:
    return _LAST_SNAPSHOT is not None


def view_close() -> list[str]:
    """End the view and hand back the hosts visited — WITHOUT recording them.

    Whether a login happened is the owner's fact to state, not one aish may
    infer from a URL having been open."""

    async def job(owner: _Owner) -> list[str]:
        visited = sorted(owner.view_hosts)
        owner.view = None
        owner.view_hosts = set()
        await owner.close_now()  # next read relaunches off-screen at full size
        return visited

    return _submit(job, 60.0)


def record_logins(hosts: list[str]) -> list[str]:
    """Mark hosts as signed in, because the owner said so.

    Takes whatever the client sends — a bare host, a full URL, blank — since
    this arrives over a WebSocket. Prefixing a scheme onto a value that already
    had one silently recorded a host called "https"."""
    clean: set[str] = set()
    for raw in hosts:
        text = str(raw or "").strip()
        if not text:
            continue
        host = host_of(text if "//" in text else "https://" + text)
        if host:
            clean.add(host)
    # WHICH record depends on which profile was being driven, and the view is
    # already closed by the time the owner confirms — so the answer is the one
    # the owner thread kept, never one re-derived from the hosts.
    _remember_logins(clean, cold=_view_was_cold())
    return sorted(clean)


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
