"""The persistent browser reader, and the gate on reading as a signed-in owner.

Nothing here launches Chrome: `browser.read` / `open_for_login` are patched,
and conftest's `no_real_browser` makes any escape from that fail loudly.
"""

import asyncio
import urllib.error

import pytest

from aish import browser
from aish import web as web_module


def run_job(owner):
    """Stand-in for browser._submit: jobs are coroutines on the browser loop,
    so a test drives one directly instead of pretending it is a function."""
    return lambda job, timeout: asyncio.new_event_loop().run_until_complete(job(owner))



# A real listing runs to tens of thousands of characters — the measured
# allegro.pl case is 403 WITH 23 000 chars of prices. Test bodies must be
# page-sized, or they read as a block screen (browser.is_challenge).
def page_sized(text):
    return text + "\n" + "\n".join(
        f"oferta {i} — 42,00 zl" for i in range(browser.CHALLENGE_MAX_CHARS // 20 + 10)
    )


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
    return tmp_path


def _http_error(code):
    def boom(url):
        raise urllib.error.HTTPError(url, code, "Forbidden", {}, None)

    return boom


class TestProfileLocation:
    def test_profile_lives_under_state_not_config(self, state):
        """A profile is live session cookies. `~/.config/aish` is auto-committed
        and PUSHED to a git remote on a timer — a profile there would publish
        the owner's logins."""
        assert browser.profile_dir() == state / "browser" / "profile"
        assert ".config" not in str(browser.profile_dir())

    def test_logins_file_sits_beside_the_profile(self, state):
        assert browser.logins_file() == state / "browser" / "logins.txt"


class TestLoginRecord:
    def test_host_of_strips_www_and_lowercases(self):
        assert browser.host_of("https://WWW.Allegro.PL/oferta/1") == "allegro.pl"
        assert browser.host_of("not a url") == ""

    def test_a_recorded_login_matches_its_subdomains(self, state):
        browser._remember_logins({"allegro.pl"})
        assert browser.is_logged_in("https://allegro.pl/oferta/1") == "allegro.pl"
        assert browser.is_logged_in("https://www.allegro.pl/x") == "allegro.pl"
        assert browser.is_logged_in("https://moje.allegro.pl/x") == "allegro.pl"

    def test_a_lookalike_host_is_not_a_match(self, state):
        """Suffix match must be on a DOT boundary: `evilallegro.pl` is not
        `allegro.pl`, and treating it as one would hand a session away."""
        browser._remember_logins({"allegro.pl"})
        assert browser.is_logged_in("https://evilallegro.pl/x") == ""
        assert browser.is_logged_in("https://allegro.pl.evil.com/x") == ""

    def test_unknown_host_is_not_logged_in(self, state):
        assert browser.is_logged_in("https://example.com") == ""

    def test_forget_drops_one_host_and_keeps_the_rest(self, state):
        browser._remember_logins({"allegro.pl", "x-kom.pl"})
        assert browser.forget_login("allegro.pl") is True
        assert browser.logged_in_hosts() == {"x-kom.pl"}
        assert browser.forget_login("allegro.pl") is False


class TestCommand:
    def test_status_names_the_profile_and_the_logins(self, state):
        browser._remember_logins({"allegro.pl"})
        out = browser.command("")
        assert str(browser.profile_dir()) in out
        assert "allegro.pl" in out

    def test_status_says_when_nothing_is_signed_in(self, state):
        assert "(nothing yet)" in browser.command("")

    def test_forget_reports_what_it_did(self, state):
        browser._remember_logins({"allegro.pl"})
        assert "no longer treated as signed in" in browser.command("forget allegro.pl")

    def test_a_url_records_what_the_owner_visited(self, state, monkeypatch):
        monkeypatch.setattr(browser, "open_for_login", lambda url: ["allegro.pl"])
        assert "allegro.pl" in browser.command("https://allegro.pl")


class TestReadUrlEscalation:
    """The escalation itself: what makes a blocked or empty page readable."""

    def test_a_403_escalates_to_the_browser(self, monkeypatch):
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text=page_sized("37,80 zl"), title="Allegro", images=[], url=url,
                status=403,
            ),
        )
        out = web_module.read_url("https://allegro.pl/listing?string=x")
        assert "37,80 zl" in out
        assert "rendered in the browser" in out

    def test_a_403_that_still_carries_the_page_is_a_success(self, monkeypatch):
        """Measured on allegro.pl: status 403, full listing in the body. The
        read is judged on TEXT, never on the status code — judging on the code
        would throw away the very page this feature exists to get."""
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text=page_sized("prices"), title="", images=[], url=url, status=403
            ),
        )
        assert not web_module.read_url("https://allegro.pl/x").startswith("ERROR")

    def test_a_javascript_shell_escalates_to_the_browser(self, monkeypatch):
        """The commonest win by far: the fetch SUCCEEDS and returns an empty
        shell, which used to be a dead end ('returned no readable text')."""
        monkeypatch.setattr(
            web_module, "_fetch", lambda url: ("<html><body></body></html>", "text/html")
        )
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text="rendered", title="", images=[], url=url, status=200
            ),
        )
        out = web_module.read_url("https://spa.example/x")
        assert "rendered" in out

    def test_no_browser_falls_back_to_the_old_message(self, monkeypatch):
        """Playwright absent must not break read_url — it degrades to exactly
        what it did before."""
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))

        def unavailable(url, **kw):
            raise browser.BrowserUnavailable("not installed")

        monkeypatch.setattr(browser, "read", unavailable)
        out = web_module.read_url("https://allegro.pl/x")
        assert out.startswith("ERROR")
        assert "403" in out

    def test_a_browser_crash_never_escapes_read_url(self, monkeypatch):
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))

        def boom(url, **kw):
            raise RuntimeError("chrome died")

        monkeypatch.setattr(browser, "read", boom)
        assert web_module.read_url("https://allegro.pl/x").startswith("ERROR")

    def test_a_404_does_not_escalate(self, monkeypatch):
        """The browser is for blocks and shells, not for every failure — a
        genuinely missing page must not cost a 2s Chrome launch."""
        calls = []
        monkeypatch.setattr(web_module, "_fetch", _http_error(404))
        monkeypatch.setattr(browser, "read", lambda url, **kw: calls.append(url))
        assert "404" in web_module.read_url("https://example.com/gone")
        assert calls == []

    def test_a_browser_page_with_no_text_is_still_an_error(self, monkeypatch):
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text="   \n  ", title="", images=[], url=url, status=403
            ),
        )
        assert web_module.read_url("https://allegro.pl/x").startswith("ERROR")

    def test_a_rendered_page_keeps_its_images_and_topic_filter(self, monkeypatch):
        """The browser path goes through the SAME presentation as a fetch, so
        og:image and `topic` do not quietly stop working on exactly the pages
        that needed rendering."""
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text="alpha line\nbeta line",
                title="Shop",
                images=["https://allegro.pl/p.jpg"],
                url=url,
                status=200,
            ),
        )
        out = web_module.read_url("https://allegro.pl/x", topic="beta")
        assert "beta line" in out
        assert "https://allegro.pl/p.jpg" in out

    def test_rendered_text_is_used_verbatim_not_reparsed_as_html(self, monkeypatch):
        """The bug that cost a whole listing: `page.content()` serializes the
        LIGHT DOM only, so a site rendering into shadow DOM handed back 362 KB
        of HTML containing the word "zł" zero times while the rendered text
        held 23 000 characters and every price. Feeding browser output back
        through the HTML extractor re-loses exactly that. Text that an HTML
        parser would strip to nothing must survive."""
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text=page_sized("Zawiesie wężowe\n37,80 zł"), title="", images=[],
                url=url, status=403,
            ),
        )
        out = web_module.read_url("https://allegro.pl/listing")
        assert "37,80 zł" in out
        assert "Zawiesie wężowe" in out

    def test_page_title_from_the_browser_is_recorded(self, monkeypatch):
        """Sources are cited by name after a task (agent.task_sources), and a
        rendered page must not lose its title on the way."""
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))
        monkeypatch.setattr(web_module, "PAGE_TITLES", {})
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text="body", title="Niska cena na Allegro", images=[],
                url=url, status=200,
            ),
        )
        web_module.read_url("https://allegro.pl/listing")
        assert web_module.PAGE_TITLES["https://allegro.pl/listing"] == (
            "Niska cena na Allegro"
        )


class TestViewSize:
    """The client's own viewport drives the remote page (#221). Not cosmetic:
    matching the width is what makes a responsive site serve its MOBILE layout,
    so a phone gets the phone page instead of a desktop page shrunk small."""

    def test_a_phone_size_is_used_as_given(self):
        assert browser.view_size(430, 900) == (430, 900)

    def test_absurd_sizes_are_clamped_not_obeyed(self):
        """A hostile or buggy client must not be able to ask for a 40000px
        page — that is a memory bomb on a box with a 16 GB ceiling."""
        assert browser.view_size(40000, 40000) == (browser.VIEW_MAX_W, browser.VIEW_MAX_H)
        assert browser.view_size(1, 1) == (browser.VIEW_MIN_W, browser.VIEW_MIN_H)

    def test_missing_or_junk_falls_back_to_the_default(self):
        """This comes straight off a WebSocket, so it may be any JSON type."""
        for bad in (None, 0, "", "abc", {}, [], True):
            assert browser.view_size(bad, bad) == (browser.VIEW_WIDTH, browser.VIEW_HEIGHT)

    def test_a_string_number_still_works(self):
        assert browser.view_size("430", "900") == (430, 900)


class TestChallengeDetection:
    """A wall HAS text, so "the browser produced text" is not the same as "the
    browser produced the page" (#221).

    This is the original failure rebuilt one layer up: the session that
    prompted this feature drowned in `Warning: This page maybe requiring
    CAPTCHA`, and handing such a screen back as content would have the model
    report a challenge's wording as the shop's, and invent from it."""

    def test_the_measured_403_with_a_full_listing_is_not_a_challenge(self):
        """The case the whole feature exists for: allegro.pl answers 403 and
        still serves 23 000 characters of real prices. Length wins over status,
        or the detector would throw away the exact page this is here to get."""
        listing = "\n".join(f"oferta {i} — 42,00 zl" for i in range(2000))
        assert len(listing) >= browser.CHALLENGE_MAX_CHARS
        assert browser.is_challenge(listing, 403) is False

    def test_a_short_body_with_a_block_status_is_a_challenge(self):
        assert browser.is_challenge("Access denied", 403) is True
        assert browser.is_challenge("slow down", 429) is True

    def test_a_wall_is_caught_by_its_wording_even_on_a_200(self):
        """Anti-bot vendors serve the interstitial with a 200 routinely."""
        for wording in (
            "Please verify you are human",
            "Checking your browser before accessing",
            "zweryfikuj, że jesteś człowiekiem",
            "Powered by DataDome",
        ):
            assert browser.is_challenge(wording, 200) is True, wording

    def test_a_short_ordinary_page_is_not_a_challenge(self):
        """A brief article must not be discarded just for being brief."""
        assert browser.is_challenge("A short but real note about hammocks.", 200) is False

    def test_a_long_page_is_content_whatever_it_says(self):
        """`captcha` appears on plenty of real pages — a help article about
        them, for one. Length is the guard against that false positive."""
        long_page = "how to solve a captcha " * 400
        assert len(long_page) >= browser.CHALLENGE_MAX_CHARS
        assert browser.is_challenge(long_page, 200) is False

    def test_read_url_reports_a_challenge_as_an_error_not_as_the_page(self, monkeypatch):
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text="Verify you are human", title="", images=[], url=url, status=403
            ),
        )
        out = web_module.read_url("https://allegro.pl/x")
        assert out.startswith("ERROR")
        assert "Verify you are human" not in out
        # …and it must say the BROWSER met a wall, not "the site may block
        # simple fetchers — retry via Jina". That message sent the model to a
        # datacenter fetcher with no session; it burned two calls (one a 22s
        # timeout) and it concluded Allegro was unreadable.
        assert "verification wall" in out
        # The reader is NAMED here, but to forbid it — a bare "don't" that does
        # not say what not to do is the hint the model ignores.
        assert "Do NOT retry this through r.jina.ai" in out
        assert "you may retry ONCE via read_url on https://r.jina.ai/" not in out


class TestViewAndReadShareOneBrowser:
    def test_a_read_refuses_while_the_owner_is_driving(self, monkeypatch):
        """One browser, one profile. A read during a hand-driven view would be
        made at the PHONE's viewport — a mobile layout returned as if it were
        the page — and would steal the tab they are mid-login on."""
        owner = browser._Owner()
        owner.view = object()
        monkeypatch.setattr(browser, "_submit", run_job(owner))
        with pytest.raises(browser.BrowserUnavailable, match="driven by hand"):
            browser.read("https://example.com")


class TestPreviewFence:
    """`scripts/aish-preview.sh` points preview at PROD's state dir on purpose,
    so preview shares this profile — the owner's LIVE signed-in sessions."""

    def test_preview_gets_no_browser(self, monkeypatch):
        monkeypatch.setenv("AISH_PREVIEW", "1")
        assert "preview" in browser.unavailable_reason()

    def test_production_is_unaffected(self, monkeypatch):
        monkeypatch.delenv("AISH_PREVIEW", raising=False)
        assert browser.unavailable_reason() == ""

    def test_a_preview_read_falls_back_instead_of_using_the_profile(self, monkeypatch):
        """The fence must hold at the READ, not merely in a status string —
        preview is exactly where experimental branches meet hostile content."""
        monkeypatch.setenv("AISH_PREVIEW", "1")
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))
        called = []
        monkeypatch.setattr(browser, "_submit", lambda fn, timeout: called.append(fn))
        out = web_module.read_url("https://allegro.pl/x")
        assert out.startswith("ERROR")
        assert called == []


class TestUnresponsiveHostEscalates:
    """The bug that cost the feature its first live test (2026-08-14).

    Escalation was wired only to HTTPError 403/429/503. Allegro answers a plain
    fetch with a prompt 403 *usually* — but after a hand-rolled script hammered
    the address it simply stopped answering, the read died on a socket timeout,
    and the generic handler returned the error without ever trying the browser.
    The whole session shows zero browser renders. A host that stops ANSWERING a
    plain fetcher is the same problem as one that refuses out loud."""

    def _timeout_fetch(self, exc):
        def boom(url):
            raise exc

        return boom

    def _rendered(self, monkeypatch):
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text=page_sized("37,80 zl"), title="Allegro", images=[], url=url,
                status=200,
            ),
        )

    def test_a_read_timeout_escalates(self, monkeypatch):
        monkeypatch.setattr(web_module, "_fetch", self._timeout_fetch(TimeoutError("timed out")))
        self._rendered(monkeypatch)
        out = web_module.read_url("https://allegro.pl/listing?string=x")
        assert "rendered in the browser" in out
        assert "37,80 zl" in out

    def test_a_dropped_connection_escalates(self, monkeypatch):
        monkeypatch.setattr(
            web_module, "_fetch", self._timeout_fetch(ConnectionResetError("reset"))
        )
        self._rendered(monkeypatch)
        assert not web_module.read_url("https://allegro.pl/x").startswith("ERROR")

    def test_a_urlerror_wrapping_a_timeout_escalates(self, monkeypatch):
        """What urllib actually raises in the wild — the reason is nested."""
        monkeypatch.setattr(
            web_module,
            "_fetch",
            self._timeout_fetch(urllib.error.URLError(TimeoutError("timed out"))),
        )
        self._rendered(monkeypatch)
        assert not web_module.read_url("https://allegro.pl/x").startswith("ERROR")

    def test_a_dns_failure_does_NOT_launch_a_browser(self, monkeypatch):
        """There is no host to render. Launching Chrome to prove a typo is a
        typo costs seconds for a certainty."""
        import socket

        monkeypatch.setattr(
            web_module,
            "_fetch",
            self._timeout_fetch(urllib.error.URLError(socket.gaierror("no such host"))),
        )
        calls = []
        monkeypatch.setattr(browser, "read", lambda url, **kw: calls.append(url))
        assert web_module.read_url("https://nope.invalid/x").startswith("ERROR")
        assert calls == []



    def test_a_refused_connection_does_NOT_launch_a_browser(self, monkeypatch):
        """Nothing is listening, so Chrome meets the same closed door. The
        match is a short allowlist rather than OSError, which is broad enough
        to swallow this and every DNS failure with it."""
        monkeypatch.setattr(
            web_module, "_fetch", self._timeout_fetch(ConnectionRefusedError("refused"))
        )
        calls = []
        monkeypatch.setattr(browser, "read", lambda url, **kw: calls.append(url))
        assert web_module.read_url("https://down.example/x").startswith("ERROR")
        assert calls == []


class TestKnownBlockingHostsSkipTheDoomedFetch:
    def test_a_host_that_needed_the_browser_goes_there_first_next_time(self, monkeypatch):
        monkeypatch.setattr(web_module, "BROWSER_HOSTS", set())
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text=page_sized("prices"), title="", images=[], url=url, status=403
            ),
        )
        web_module.read_url("https://allegro.pl/a")
        assert "allegro.pl" in web_module.BROWSER_HOSTS

        # Second read: the plain fetch must not even be attempted.
        def must_not_run(url):
            raise AssertionError("the plain fetch ran for a known-blocking host")

        monkeypatch.setattr(web_module, "_fetch", must_not_run)
        assert not web_module.read_url("https://allegro.pl/b").startswith("ERROR")

    def test_an_ordinary_host_is_never_remembered(self, monkeypatch):
        monkeypatch.setattr(web_module, "BROWSER_HOSTS", set())
        monkeypatch.setattr(web_module, "_fetch", lambda url: ("<p>hello</p>", "text/html"))
        web_module.read_url("https://example.com/x")
        assert web_module.BROWSER_HOSTS == set()


class TestAThinPageGetsASecondChance:
    """Reads serialise through one browser thread, so the third in a turn
    starts on a busy machine. A half-painted listing is SHORT, which is exactly
    what a wall looks like — and it was rejected as one, live, on the owner's
    second test (14.3s, ok=False, while its two siblings rendered fine)."""

    def test_a_page_that_fills_in_late_is_read_not_rejected(self, monkeypatch):
        pages = iter(["thin", "the full listing " * 400])

        class FakePage:
            url = "https://allegro.pl/x"

            async def goto(self, *a, **k):
                return type("R", (), {"status": 403})()

            async def wait_for_timeout(self, ms):
                pass

            async def inner_text(self, sel):
                return next(pages)

            async def title(self):
                return "Allegro"

            async def evaluate(self, js):
                return []

            async def close(self):
                pass

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeOwner:
            view = None

            async def context(self, **k):
                return FakeContext()

        monkeypatch.setattr(browser, "_submit", run_job(FakeOwner()))
        page = browser.read("https://allegro.pl/x")
        assert len(page.text) > browser.CHALLENGE_MAX_CHARS
        assert browser.is_challenge(page.text, page.status) is False


class TestTheReadingContract:
    """What the system prompt must keep saying about reading pages (#221).

    Pinned as text because every clause here was written to stop a SPECIFIC
    thing the model did in the owner's live tests, and a well-meaning tidy-up
    of the prompt would silently bring each one back."""

    def _prompt(self):
        from aish.agent import SYSTEM_PROMPT_TEMPLATE

        return SYSTEM_PROMPT_TEMPLATE

    def test_no_hand_rolled_fetchers_in_any_language(self):
        """It wrote its own Python fetcher and ran it — curl with extra steps,
        on a page read_url handles. The owner has denied that shape three
        times across two sessions."""
        prompt = self._prompt()
        assert "MUST NOT fetch a web page any other way" in prompt
        assert "Python" in prompt

    def test_a_shop_is_read_at_its_own_listing_url(self):
        """Twelve `site:allegro.pl` searches returned the search engine's index
        instead of today's prices."""
        prompt = self._prompt()
        assert "site:allegro.pl" in prompt
        assert "listing?string=" in prompt

    def test_success_must_not_be_reported_as_failure(self):
        """The one that actually cost the owner an answer: it read three
        Allegro pages, then told him Allegro blocks automated reading and
        answered from other shops — throwing away data it already had."""
        prompt = self._prompt()
        assert "REPORT WHAT ACTUALLY HAPPENED" in prompt
        assert "rendered in the browser" in prompt
        assert "blocks automated reading" in prompt
