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

import os
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A launch is ~2s, so the context is kept warm; an idle Chrome is ~400 MB, so
# it does not stay warm for long. See the module docstring on the memory
# ceiling this box actually runs against.
IDLE_SECONDS = 180.0
NAV_TIMEOUT_MS = 45_000
SETTLE_MS = 2_500
LOGIN_WINDOW_TIMEOUT = 15 * 60.0
# An open view suppresses the idle reaper, so it needs its own ceiling: a client
# that vanishes without closing (a phone backgrounding the PWA, a dropped
# socket) would otherwise hold Chrome and the profile lock forever. Generous,
# because a paused 2FA login is the case the reaper must not interrupt.
VIEW_MAX_IDLE = 15 * 60.0

# Off the visible desktop. The window is REAL — that is the whole reason this
# works where headless does not — it simply is not parked where the owner is
# looking. A login window is launched on-screen instead (`_LOGIN_ARGS`).
_OFFSCREEN_ARGS = ["--window-position=-4000,-4000", "--window-size=1440,900"]
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
    judge a read by whether it produced TEXT, never by the code."""

    text: str
    title: str
    images: list[str]
    url: str
    status: int | None


_OWNER: threading.Thread | None = None
_OWNER_LOCK = threading.Lock()


def state_dir() -> Path:
    return Path(
        os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish"))
    )


def profile_dir() -> Path:
    return state_dir() / "browser" / "profile"


def logins_file() -> Path:
    return state_dir() / "browser" / "logins.txt"


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


def _remember_logins(hosts: set[str]) -> None:
    if not hosts:
        return
    path = logins_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(logged_in_hosts() | hosts)) + "\n", encoding="utf-8")


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
    viewport: dict | None = None,
    device_scale_factor: float | None = None,
) -> Any:
    profile = profile_dir()
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
        accept_downloads=False,
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


async def _body_text(page: Any) -> str:
    try:
        return await page.inner_text("body")
    except Exception:  # noqa: BLE001 — no body is a real answer: no text
        return ""


async def _declared_images_async(page: Any) -> list[str]:
    try:
        return list(await page.evaluate(_IMAGES_JS))[:3]
    except Exception:  # noqa: BLE001 — no images is a fine answer
        return []


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
        self.loop: Any = None
        self._ready = threading.Event()
        self._lock: Any = None  # created on the loop: guards context setup
        # The page a remote view is driving, when one is open. Held here
        # because it must outlive a single job: the whole point of the view is
        # that tap, type and tap again land on the SAME page.
        self.view: Any = None
        self.view_hosts: set[str] = set()
        self.view_touched = 0.0
        self.busy = 0

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
            idle_for = time.monotonic() - self.view_touched
            if self.view is None or idle_for > VIEW_MAX_IDLE:
                await self._close()

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
            if self._context is not None:
                return self._context
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
                    viewport=viewport,
                    device_scale_factor=device_scale_factor,
                )

            try:
                self._context = await launch()
            except Exception as exc:  # noqa: BLE001 — one specific, recoverable cause
                # Chrome leaves a SingletonLock in the profile when it dies
                # badly. Every later launch then fails, so every read AND every
                # view fails until somebody kills Chrome by hand — on a headless
                # server with nobody in front of it.
                if not _clear_stale_lock(exc):
                    raise
                self._context = await launch()
            return self._context

    async def _close(self) -> None:
        self.view = None  # a closed context has no page left to drive
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001 — a dead browser is already closed
                pass
            self._context = None

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

def read(url: str, *, timeout: float = 90.0) -> Page:
    """Render `url` in the persistent browser and hand back its HTML.

    Raises BrowserUnavailable when there is no browser to use; every other
    failure arrives as the underlying Playwright error."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)

    async def job(owner: _Owner) -> Page:
        if owner.view is not None:
            # The owner is driving the browser by hand. Reusing that context
            # would read the site at their PHONE's viewport and hand back a
            # mobile layout as if it were the page — quietly different results
            # for a reason nothing in the answer would show. It would also
            # steal the page they are mid-login on.
            raise BrowserUnavailable(
                "the browser is being driven by hand right now (/browser) — "
                "the page will be readable again once that window is closed"
            )
        context = await owner.context()
        page = await context.new_page()

        async def attempt() -> tuple[Any, str]:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            )
            await page.wait_for_timeout(SETTLE_MS)
            await _dismiss_consent(page)
            return response, await _body_text(page)

        try:
            response, text = await attempt()
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
            return Page(
                text=text,
                title=(await page.title()) or "",
                images=await _declared_images_async(page),
                url=page.url or url,
                status=response.status if response is not None else None,
            )
        finally:
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
VIEW_JPEG_QUALITY = 50
# Rendered at 2x so a zoomed-in frame stays sharp. The owner zooms to hit a
# small password field, and a 1x capture blown up is exactly where that fails.
# Coordinates stay in CSS pixels — only the image has more of them.
VIEW_SCALE = 2
VIEW_MIN_W, VIEW_MAX_W = 320, 1920
VIEW_MIN_H, VIEW_MAX_H = 400, 2400


def view_size(width: object, height: object) -> tuple[int, int]:
    """The client's own viewport, clamped. NOT cosmetic: matching the page to
    the device is what makes a responsive site serve its MOBILE layout, so a
    phone gets the phone page — tappable targets, one column — instead of a
    desktop page shrunk to illegibility. It is also why no user-agent is
    spoofed to ask for it: width is what responsive CSS keys on, and a UA that
    disagreed with the profile's own would risk the session that later reads
    depend on."""
    try:
        # str() first: this arrives straight off a WebSocket, so it may be any
        # JSON type at all, including a dict.
        w = int(float(str(width or 0))) or VIEW_WIDTH
        h = int(float(str(height or 0))) or VIEW_HEIGHT
    except (TypeError, ValueError):
        w, h = VIEW_WIDTH, VIEW_HEIGHT
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


async def _frame(owner: _Owner) -> Frame:
    page = owner.view
    await page.wait_for_timeout(400)  # let a click's repaint land before capture
    size = page.viewport_size or {"width": VIEW_WIDTH, "height": VIEW_HEIGHT}
    return Frame(
        jpeg=await page.screenshot(type="jpeg", quality=VIEW_JPEG_QUALITY),
        url=page.url or "",
        title=(await page.title()) or "",
        width=size["width"],
        height=size["height"],
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
    url: str, *, width: object = None, height: object = None, timeout: float = 120.0
) -> Frame:
    """Start a remote view at `url`, sized to the client, and return a frame."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)
    w, h = view_size(width, height)

    async def job(owner: _Owner) -> Frame:
        return await _open_view(owner, url, w, h)

    return _submit(job, timeout)


async def _open_view(owner: _Owner, url: str, w: int, h: int) -> Frame:
    # A KNOWN viewport, so a tap at (x, y) in the PWA means that point
    # here. The read context uses the real window size and cannot give
    # that, so the view gets its own — and Chrome locks the profile, so
    # the read context has to let go first.
    await owner.close_now()
    context = await owner.context(
        args=[f"--window-size={w},{h}", "--window-position=-4000,-4000"],
        viewport={"width": w, "height": h},
        device_scale_factor=VIEW_SCALE,
    )
    page = await context.new_page()
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


def view_act(action: str, **kwargs: Any) -> Frame:
    """Apply one interaction to the open view and return the resulting frame.

    Keystrokes are NEVER logged or traced anywhere in this path — the owner
    types real passwords through it."""

    async def job(owner: _Owner) -> Frame:
        page = owner.view
        if page is None:
            # Navigating with nothing open OPENS it. `/browser` with no URL
            # shows the sheet without starting a browser, so the owner's first
            # Go landed on "no remote view is open" — a true statement and a
            # useless one, since asking to go somewhere IS asking to open it.
            if action in ("goto", "refresh") and kwargs.get("url"):
                target = str(kwargs["url"])
                if not target.startswith(("http://", "https://")):
                    target = "https://" + target
                vw, vh = view_size(kwargs.get("width"), kwargs.get("height"))
                return await _open_view(owner, target, vw, vh)
            raise BrowserUnavailable("no remote view is open")
        if action == "click":
            await page.mouse.click(float(kwargs["x"]), float(kwargs["y"]))
        elif action == "type":
            await page.keyboard.type(str(kwargs.get("text", "")), delay=12)
        elif action == "key":
            await page.keyboard.press(str(kwargs.get("key", "Enter")))
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
        elif action != "refresh":
            raise ValueError(f"unknown view action {action!r}")
        owner.view_touched = time.monotonic()
        _note_visit(owner, page.url)
        return await _frame(owner)

    return _submit(job, 120.0)


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
    _remember_logins(clean)
    return sorted(clean)


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
