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

**One thread owns the browser.** Playwright's sync API binds its objects to the
thread that created them, and `read_url` runs on a pool (`_execute_tool_calls`
fans read-only tools out concurrently), so a shared context touched from a
second thread errors out. Everything here is therefore marshalled to one
long-lived owner thread through `_JOBS`; that also gives single-ownership of
the profile directory for free, which Chrome requires — it locks the
user-data-dir and a second launch against a live profile fails.

The context is kept warm between reads (a launch costs ~2s) but closed after
`IDLE_SECONDS`, because this box runs a Home Assistant VM and Colima beside a
16 GB ceiling and an idle Chrome is not free.
"""

from __future__ import annotations

import os
import queue
import threading
import urllib.parse
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A launch is ~2s, so the context is kept warm; an idle Chrome is ~400 MB, so
# it does not stay warm for long. See the module docstring on the memory
# ceiling this box actually runs against.
IDLE_SECONDS = 180.0
NAV_TIMEOUT_MS = 45_000
SETTLE_MS = 2_500
LOGIN_WINDOW_TIMEOUT = 15 * 60.0

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


@dataclass
class _Job:
    fn: Callable[[Any], Any]
    future: Future = field(default_factory=Future)


_JOBS: queue.Queue[_Job] = queue.Queue()
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


def unavailable_reason() -> str:
    """"" when a browser read can be attempted, else why it cannot."""
    if not enabled():
        return "the browser reader is switched off (AISH_BROWSER=0)"
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

def _launch(playwright: Any, *, args: list[str]) -> Any:
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    launch_args = list(args)
    omit: list[str] = []
    if stealth():
        launch_args += _STEALTH_ARGS
        omit += _STEALTH_OMIT
    return playwright.chromium.launch_persistent_context(
        str(profile),
        channel="chrome",  # the real Chrome already on this Mac, not a bundled build
        headless=False,  # headless is what these sites actually block
        args=launch_args,
        ignore_default_args=omit,
        viewport=None,  # use the real window size
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


def _declared_images(page: Any) -> list[str]:
    try:
        return list(page.evaluate(_IMAGES_JS))[:3]
    except Exception:  # noqa: BLE001 — no images is a fine answer
        return []


def _dismiss_consent(page: Any) -> None:
    for selector in _CONSENT_SELECTORS:
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=800):
                button.click(timeout=2_000)
                page.wait_for_timeout(1_200)
                return
        except Exception:  # noqa: BLE001 — best effort; a missing banner is the norm
            continue


class _Owner:
    """The one thread that touches Playwright. Owns the context's lifetime."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._context: Any = None

    def run(self) -> None:
        while True:
            try:
                job = _JOBS.get(timeout=IDLE_SECONDS)
            except queue.Empty:
                self._close()
                continue
            try:
                job.future.set_result(job.fn(self))
            except BaseException as exc:  # noqa: BLE001 — travels to the caller
                job.future.set_exception(exc)

    def context(self, *, args: list[str] | None = None) -> Any:
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover — guarded by unavailable_reason
            raise BrowserUnavailable(str(exc)) from exc
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        self._context = _launch(self._playwright, args=args or _OFFSCREEN_ARGS)
        return self._context

    def _close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001 — a dead browser is already closed
                pass
            self._context = None

    def close_now(self) -> None:
        self._close()


def _submit(fn: Callable[[_Owner], Any], timeout: float) -> Any:
    global _OWNER
    with _OWNER_LOCK:
        if _OWNER is None or not _OWNER.is_alive():
            owner = _Owner()
            _OWNER = threading.Thread(
                target=owner.run, name="aish-browser", daemon=True
            )
            _OWNER.start()
    job = _Job(fn)
    _JOBS.put(job)
    return job.future.result(timeout=timeout)


# -------------------------------------------------------------- the reads

def read(url: str, *, timeout: float = 90.0) -> Page:
    """Render `url` in the persistent browser and hand back its HTML.

    Raises BrowserUnavailable when there is no browser to use; every other
    failure arrives as the underlying Playwright error."""
    reason = unavailable_reason()
    if reason:
        raise BrowserUnavailable(reason)

    def job(owner: _Owner) -> Page:
        context = owner.context()
        page = context.new_page()
        try:
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            )
            page.wait_for_timeout(SETTLE_MS)
            _dismiss_consent(page)
            try:
                text = page.inner_text("body")
            except Exception:  # noqa: BLE001 — no body is a real answer: no text
                text = ""
            return Page(
                text=text,
                title=page.title() or "",
                images=_declared_images(page),
                url=page.url or url,
                status=response.status if response is not None else None,
            )
        finally:
            try:
                page.close()
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

    def job(owner: _Owner) -> list[str]:
        owner.close_now()  # the off-screen context must release the profile lock
        context = owner.context(args=_LOGIN_ARGS)
        visited: set[str] = set()
        page = context.new_page()

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
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — they can still drive it by hand
            pass
        deadline = timeout
        step = 0.5
        while deadline > 0:
            if page.is_closed() or not context.pages:
                break
            try:
                page.wait_for_timeout(int(step * 1000))
            except Exception:  # noqa: BLE001 — closed mid-wait IS the exit condition
                break
            deadline -= step
        _remember_logins(visited)
        owner.close_now()  # back to a cold profile; the next read relaunches off-screen
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


def shutdown() -> None:
    """Close the browser now (tests, and `/browser close`)."""
    if _OWNER is None or not _OWNER.is_alive():
        return
    try:
        _submit(lambda owner: owner.close_now(), timeout=30.0)
    except Exception:  # noqa: BLE001 — shutdown is best effort
        pass
