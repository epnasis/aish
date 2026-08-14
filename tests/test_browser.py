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


class TestVisitingIsNotSigningIn:
    """Closing the remote view used to mark every host visited as signed in, so
    browsing to allegro.pl claimed an account there — and every later read of
    the site the feature exists for then wanted approval. Friction on the main
    path, and a claim about the owner's account that was untrue."""

    def test_visiting_records_nothing(self, state):
        owner = browser._Owner()
        browser._note_visit(owner, "https://allegro.pl/oferta/x")
        assert owner.view_hosts == {"allegro.pl"}
        assert browser.logged_in_hosts() == set()   # nothing written

    def test_the_owner_saying_so_records_it(self, state):
        assert browser.record_logins(["allegro.pl"]) == ["allegro.pl"]
        assert browser.is_logged_in("https://allegro.pl/moje") == "allegro.pl"

    def test_recording_normalises_what_the_client_sends(self, state):
        assert browser.record_logins(["https://www.X-KOM.pl/"]) == ["x-kom.pl"]

    def test_junk_is_dropped_rather_than_recorded(self, state):
        assert browser.record_logins(["", "   "]) == []
        assert browser.logged_in_hosts() == set()


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

            async def evaluate(self, js, *args):
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


class TestLinksSurviveTheRender:
    """A rendered page used to arrive as TEXT ONLY, and on a shop that throws
    away the answer: the model could read that an offer costs 34,99 zl and had
    no way to say where it was. It fell back to web_search'ing `site:allegro.pl`
    for the URL of a title it had already read — 66 of 113 searches in one
    session (session-20260814-131203), against a system prompt that forbids
    exactly that. Measured on that listing: 72 cards, 0 URLs recovered."""

    def _read(self, monkeypatch, *, body, links=(), main=""):
        class FakePage:
            url = "https://allegro.pl/listing?string=x"

            async def goto(self, *a, **k):
                return type("R", (), {"status": 403})()

            async def wait_for_timeout(self, ms):
                pass

            async def inner_text(self, sel):
                return body

            async def title(self):
                return "Allegro"

            async def evaluate(self, js, *args):
                if js is browser._MAIN_JS:
                    return main
                if js is browser._LINKS_JS:
                    return links
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
        return browser.read("https://allegro.pl/listing?string=x")

    def test_an_offers_url_arrives_on_its_own_line(self, monkeypatch):
        page = self._read(
            monkeypatch,
            body=page_sized("ZAWIESIE CZARNE WEZOWE\n34,99 zl"),
            links=[["ZAWIESIE CZARNE WEZOWE", "https://allegro.pl/oferta/zawiesie-1"]],
        )
        out = web_module.merge_links(page.text, page.links)
        assert "ZAWIESIE CZARNE WEZOWE → https://allegro.pl/oferta/zawiesie-1" in out

    def test_a_click_tracker_is_reduced_to_the_offer(self, monkeypatch):
        """Every sponsored card on the measured listing linked to
        allegro.pl/events/clicks?…&redirect=<the offer>&sig=… — citing the ad
        system instead of the product, at 250 characters a link."""
        tracker = (
            "https://allegro.pl/events/clicks?emission_id=abc&type=OFFER"
            "&redirect=https%3A%2F%2Fallegro.pl%2Foferta%2Fzawiesie-r1-14486087002"
            "%3Fbi_s%3Dads%26bi_m%3Dproductlisting&sig=2a79c17b81"
        )
        page = self._read(
            monkeypatch, body=page_sized("ZAWIESIE R1"), links=[["ZAWIESIE R1", tracker]]
        )
        out = web_module.merge_links(page.text, page.links)
        assert "ZAWIESIE R1 → https://allegro.pl/oferta/zawiesie-r1-14486087002" in out
        assert "events/clicks" not in out
        assert "bi_s" not in out

    def test_main_narrows_the_page_so_the_links_fit_the_budget(self, monkeypatch):
        """A read is capped, and on a shop the leading kilobytes are category
        navigation — so the cap fell inside the chrome. Measured: body 13 473
        chars vs <main> 10 966, which is 25 linked offers in budget, not 15."""
        main = page_sized("oferta")
        body = "NAV JUNK " * 80 + "\n" + main    # ~19% chrome, as measured
        page = self._read(monkeypatch, body=body, main=main)
        assert "NAV JUNK" not in page.text
        assert "oferta" in page.text

    def test_a_fragmentary_main_is_refused_rather_than_losing_the_page(self, monkeypatch):
        """A <main> holding a sliver means the site puts its content elsewhere.
        Preferring it would DROP content silently, which costs more than
        carrying some chrome."""
        body = page_sized("the whole listing")
        page = self._read(monkeypatch, body=body, main="a crumb")
        assert page.text == body

    def test_the_wall_check_still_sees_the_whole_body(self, monkeypatch):
        """<main> is applied to what is handed back, never to what is JUDGED.
        Narrowing the text a wall is detected in would move thresholds this
        module measured on whole bodies, and a page wrongly called a wall is
        the expensive failure here."""
        body = page_sized("real prices")
        page = self._read(monkeypatch, body=body, main="short")
        assert browser.is_challenge(page.text, page.status) is False

    def test_a_page_with_no_links_is_unchanged(self, monkeypatch):
        page = self._read(monkeypatch, body=page_sized("an article"), links=[])
        assert page.links == []
        assert "→" not in web_module.merge_links(page.text, page.links)

    def test_an_unusable_evaluate_never_breaks_the_read(self, monkeypatch):
        """Link extraction is an upgrade to a read, never a dependency of one:
        an old Chrome or a hostile page must still yield its text."""

        class Boom:
            async def evaluate(self, js, *args):
                raise RuntimeError("no")

        assert asyncio.new_event_loop().run_until_complete(
            browser._content_links(Boom())
        ) == []
        assert asyncio.new_event_loop().run_until_complete(
            browser._main_text(Boom(), "body")
        ) == ""

    def test_a_walled_page_is_never_annotated(self, monkeypatch):
        """Annotating a challenge screen's links would only make a block page
        look more like a page."""
        monkeypatch.setattr(web_module, "_fetch", _http_error(403))
        monkeypatch.setattr(
            browser,
            "read",
            lambda url, **kw: browser.Page(
                text="Verify you are human", title="", images=[], url=url, status=403,
                links=[("Verify you are human", "https://allegro.pl/oferta/x")],
            ),
        )
        out = web_module.read_url("https://allegro.pl/listing?string=x")
        assert out.startswith("ERROR")
        assert "oferta" not in out


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


class TestSheddingASouredReputation:
    """A wall on a warm profile is usually the SCORE, not the page (#221).

    Measured on allegro.pl: an offer page returning 7 833 characters on a cold
    profile returned ZERO on a warm one, and dropping `datadome` alone took it
    back to 7 874. Without this the browser was WEAKER than it should be, and
    aish gave up on a page it could read — which is what the owner reported."""

    class FakeContext:
        def __init__(self, cookies):
            self._cookies = list(cookies)
            self.cleared = []

        async def cookies(self):
            return self._cookies

        async def clear_cookies(self, *, name=None, domain=None, path=None):
            self.cleared.append((name, domain))
            self._cookies = [
                c for c in self._cookies
                if not (c["name"] == name and c["domain"] == domain)
            ]

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_the_scoring_cookie_is_dropped(self):
        ctx = self.FakeContext([{"name": "datadome", "domain": ".allegro.pl"}])
        assert self._run(browser._shed_reputation(ctx, "https://allegro.pl/oferta/x")) is True
        assert ("datadome", ".allegro.pl") in ctx.cleared

    def test_a_login_cookie_is_NEVER_dropped(self):
        """The same jar holds the sessions the owner signed in for by hand —
        the entire reason the profile persists. Clearing those to fix a scrape
        would trade the feature for the workaround."""
        ctx = self.FakeContext([
            {"name": "datadome", "domain": ".allegro.pl"},
            {"name": "QXLSESSID", "domain": ".allegro.pl"},
            {"name": "wdctx", "domain": ".allegro.pl"},
        ])
        self._run(browser._shed_reputation(ctx, "https://allegro.pl/oferta/x"))
        survivors = {c["name"] for c in self._run(ctx.cookies())}
        assert "QXLSESSID" in survivors
        assert "wdctx" in survivors
        assert "datadome" not in survivors
        assert all(name in browser._REPUTATION_COOKIES for name, _ in ctx.cleared)

    def test_a_pass_token_is_kept(self):
        """`cf_clearance` is EVIDENCE a challenge was already solved. Dropping
        it would throw away a good thing and invite the challenge back."""
        assert "cf_clearance" not in browser._REPUTATION_COOKIES
        ctx = self.FakeContext([{"name": "cf_clearance", "domain": ".shop.example"}])
        assert self._run(browser._shed_reputation(ctx, "https://shop.example/x")) is False

    def test_nothing_is_shed_for_an_unparseable_url(self):
        ctx = self.FakeContext([{"name": "datadome", "domain": ".allegro.pl"}])
        assert self._run(browser._shed_reputation(ctx, "not a url")) is False
        assert ctx.cleared == []

    def test_only_this_host_is_touched(self):
        ctx = self.FakeContext([{"name": "datadome", "domain": ".other.example"}])
        self._run(browser._shed_reputation(ctx, "https://allegro.pl/x"))
        assert all(domain.endswith("allegro.pl") for _, domain in ctx.cleared)


class TestPasswordsAreNeverReadBack:
    """The JPEG already carries everything VISIBLE, so pre-filling an ordinary
    field adds nothing. A password field shows dots — the pixels have never
    carried the value — so reading `input[type=password].value` would be
    strictly NEW exposure, of a credential Chrome's own profile may have
    autofilled and aish never saw typed. Rendering it masked on the phone does
    not undo transmitting it."""

    def test_the_probe_refuses_a_password_value(self):
        js = browser._FOCUS_JS
        assert "type === 'password'" in js
        assert "!secret" in js   # value is read only when NOT secret

    def test_a_revealed_password_is_still_refused(self):
        """Sites flip type=password to type=text for their own eye button.
        Keying only on the momentary type would let tapping that first launder
        the value into the read-back path."""
        js = browser._FOCUS_JS
        assert "current-password" in js
        assert "new-password" in js

    def test_the_page_text_is_never_used_as_a_field_value(self):
        """An early probe fell back to innerText for non-fields and returned
        the WHOLE PAGE as a "field value"."""
        assert "innerText" not in browser._FOCUS_JS.split("const value")[1]


class TestEditingUsesRealKeystrokes:
    def test_fill_selects_all_then_types(self):
        """Playwright fill() dispatches ONE input event and no key events, so
        keystroke-listening widgets break — and 2FA code boxes break outright:
        six one-character inputs that advance on each keyup would receive
        "123456" in box one. This feature's primary scenario is logins."""
        import inspect

        source = inspect.getsource(browser.view_act)
        assert "ControlOrMeta+a" in source
        assert "keyboard.type" in source
        assert ".fill(" not in source


class TestNativeDialogsAreDeadEnds:
    """A native dialog is browser CHROME, not page content, so `screenshot`
    cannot see it and the owner has nothing to tap (#221).

    Passkeys are the case that bit: Google's sign-in uses WebAuthn conditional
    UI, which fires the moment an email field is focused. The owner reported
    the page going grey after entering his email, with the password step never
    arriving and Back not recovering it — a prompt he could not see. Measured
    in this browser before the fix: WebAuthn available, conditional mediation
    available."""

    def test_webauthn_is_removed_from_the_view(self):
        script = browser._NO_NATIVE_CREDENTIAL_UI
        assert "PublicKeyCredential" in script
        assert "navigator" in script and "credentials" in script

    def test_the_view_installs_it_before_navigating(self):
        import inspect

        source = inspect.getsource(browser._open_view)
        assert "add_init_script(_NO_NATIVE_CREDENTIAL_UI)" in source
        assert source.index("add_init_script") < source.index("page.goto")

    def test_a_native_dialog_is_reported_rather_than_silently_dismissed(self):
        """Playwright dismisses dialogs by DEFAULT, silently — so a login that
        alerts an error would vanish without trace."""
        import inspect

        assert "filechooser" in inspect.getsource(browser._open_view)
        assert "cannot be shown here" in inspect.getsource(browser._refuse_upload)


class TestDetailIsFetchedForWhatHeIsLookingAt:
    """A frame is an OVERVIEW; sharpness past its density is fetched (#227).

    The owner met a blurred page above a 2.5x zoom, and no density setting fixes
    it: zoom goes to 4x, and a frame is sharp only to `zoom == density`, so
    serving 4x from the frame would mean a 1.3 MB, 228 ms capture on EVERY
    glance and scroll. A patch of the visible rectangle is 90 KB and 18 ms at
    that zoom — and gets cheaper the further in he goes, because the region
    shrinks as fast as the scale grows. Detail is O(screen); density is O(page).
    """

    def test_a_frame_is_still_dense_enough_for_ordinary_zooming(self):
        """Density was dropped to 1.5 to save bytes, which was the wrong target
        — this JPEG never reaches the model, so its only cost is Mac -> phone,
        about 90 ms of a 1-3 s trip. It buys sharpness he noticed at once."""
        assert browser.VIEW_SCALE >= 2

    def test_the_scale_is_capped_at_what_a_screen_can_show(self):
        _, _, _, _, scale = browser.detail_request(0, 0, 400, 600, 99, 1280, 1950)
        assert scale == browser.VIEW_DETAIL_MAX_SCALE
        _, _, _, _, floor = browser.detail_request(0, 0, 400, 600, 0.1, 1280, 1950)
        assert floor == 1.0

    def test_a_rect_off_the_page_is_pulled_BACK_not_refused(self):
        """A rounding error at the edge of a zoomed page should cost a few
        pixels of coverage, not the capture."""
        x, y, w, h, _ = browser.detail_request(2000, 3000, 400, 600, 2, 1280, 1950)
        assert (x, y, w, h) == (880, 1350, 400, 600)
        assert x + w <= 1280 and y + h <= 1950

    def test_a_rect_bigger_than_the_page_becomes_the_page(self):
        x, y, w, h, _ = browser.detail_request(-50, -50, 9999, 9999, 2, 1280, 1950)
        assert (x, y, w, h) == (0, 0, 1280, 1950)

    def test_junk_from_the_socket_does_not_reach_chrome(self):
        x, y, w, h, scale = browser.detail_request(
            {"x": 1}, None, "abc", [], "nope", 1280, 1950
        )
        assert (x, y) == (0, 0)
        assert 16 <= w <= 1280 and 16 <= h <= 1950
        assert 1.0 <= scale <= browser.VIEW_DETAIL_MAX_SCALE

    def test_an_oversized_ask_loses_SCALE_and_never_coverage(self):
        """Shrinking the rect would silently cover less of what he is looking
        at; shrinking the scale only means the patch is less sharp than his
        screen could show, which he can see past."""
        x, y, w, h, scale = browser.detail_request(0, 0, 1280, 1950, 4, 1280, 1950)
        assert (w, h) == (1280, 1950)
        assert w * h * scale * scale <= browser.VIEW_DETAIL_MAX_PIXELS
        assert scale < 4

    def test_a_screenful_stays_a_screenful_however_far_he_zooms(self):
        """The reason this scales where density does not: at 2x the visible
        region is half the page, at 4x a quarter — and the pixel count of the
        capture barely moves, because scale rises exactly as the region falls."""
        counts = []
        for zoom in (2, 2.5, 3, 4):
            w = 1280 / zoom
            h = 1950 / zoom
            _, _, cw, ch, s = browser.detail_request(0, 0, w, h, zoom, 1280, 1950)
            counts.append(cw * s * ch * s)
        assert max(counts) / min(counts) < 1.05


class TestTheViewIsDesktopSoOneFrameCarriesMore:
    """ROUND TRIPS ARE THE SCARCE RESOURCE; zoom is free.

    The view was briefly given a mobile identity, because serving the phone's
    web to a phone-shaped viewport looked obviously right. It was wrong for
    this UI, and the owner's screenshot settled it: allegro.pl's mobile home
    page filled the whole frame with an app-install coupon, a logo, a promo
    strip and a nav bar — no content at all — so reaching anything cost scroll
    after scroll, one round trip each.

    Measured on allegro.pl: a 430-wide viewport yields ~7 000 characters and 61
    prices; 1280-wide yields ~16 400 and 114. Nearly triple the page per round
    trip, and the owner zooms into it locally for nothing."""

    def test_the_page_is_asked_for_at_desktop_width(self):
        assert browser.VIEW_DESKTOP_WIDTH >= 1024
        width, _height = browser.view_size(430, 717)
        assert width == browser.VIEW_DESKTOP_WIDTH

    def test_the_stage_SHAPE_is_preserved_so_nothing_is_letterboxed(self):
        """object-fit: contain wastes whatever does not match. A frame shaped
        like the stage is all page."""
        for stage_w, stage_h in ((430, 717), (390, 560), (820, 500)):
            width, height = browser.view_size(stage_w, stage_h)
            assert abs((height / width) - (stage_h / stage_w)) < 0.02

    def test_there_is_no_mobile_identity_left_to_split_the_session(self):
        """Reads must stay desktop (allegro.pl answers ANY mobile identity with
        403 and zero text), so a mobile view meant a session created as a phone
        and read as a desktop — the mismatch bot-scoring exists to catch."""
        assert not hasattr(browser, "MOBILE_UA")
        assert not hasattr(browser, "view_identity")

    def test_junk_still_falls_back_rather_than_dividing_by_zero(self):
        assert browser.view_size(0, 0) == browser.view_size(None, None)
        assert browser.view_size("x", "y")[0] == browser.VIEW_DESKTOP_WIDTH


class TestFramesWaitForThePageToSettle:
    """The owner PROVED this with paired screenshots: a partly-rendered page,
    then the finished one, with no navigation between — only another frame. The
    picture had been wrong, not the page. A fixed sleep cannot fix it, because
    "loaded" is a property of the page, not a duration."""

    def test_a_frame_can_settle_before_capturing(self):
        import inspect

        source = inspect.getsource(browser._frame)
        assert "if settle:" in source
        assert "await _settle(page)" in source

    def test_the_first_frame_does_NOT_wait_for_the_settle(self):
        """Waiting for quiet before showing ANYTHING read as nothing
        happening. The quick frame goes out, then one correction if the page
        moved — "it's fine to show two screenshots… needs to be just once"."""
        import inspect

        source = inspect.getsource(browser._frame)
        assert "FIRST_FRAME_MS" in source
        assert browser.FIRST_FRAME_MS < browser.SETTLE_MAX_MS

    def test_settling_is_bounded(self):
        """A page that never goes quiet — a ticker, a spinner — must still
        produce a frame rather than hanging the view."""
        import inspect

        source = inspect.getsource(browser._settle)
        assert "SETTLE_MAX_MS" in source
        assert source.count("except") >= 2   # every wait degrades, none raises


class TestSelectsAndNativePickers:
    """Chrome draws a <select> with NATIVE UI, which `page.screenshot` cannot
    capture — so a tapped select produced a frame that looked completely inert.
    Same dead end as the passkey prompt, and the same answer: the capability is
    brought into the page rather than left to browser chrome."""

    def test_a_select_reports_its_options(self):
        js = browser._FOCUS_JS
        assert "tag === 'select'" in js
        assert "a.options" in js
        assert "chosen: o.selected" in js

    def test_choosing_uses_select_option_not_typing(self):
        """`select_option` fires `change`, which is what a site listens for.
        Typing into a <select> does nothing at all."""
        import inspect

        source = inspect.getsource(browser.view_act)
        assert "select_option" in source

    def test_date_inputs_are_typed_rather_than_left_to_a_native_calendar(self):
        """A date input opens Chrome's own picker, invisible for the same
        reason — but it accepts typed text, so it is treated as editable."""
        js = browser._FOCUS_JS
        for kind in ("'date'", "'time'", "'month'"):
            assert kind in js


class TestEveryActionActuallyRuns:
    """EXECUTES each view action against a fake page.

    A total interaction outage shipped while 68 tests passed: a new `if` block
    was inserted mid-chain, splitting one if/elif/else into two, so `click`,
    non-secret `fill` and `clear` fell through to `raise ValueError("unknown
    view action")` — after the click had already been performed on the page.
    Every tap would have errored.

    It passed because the input-contract tests read `inspect.getsource` and
    never call anything. Source inspection cannot see control flow; this can."""

    class FakePage:
        url = "https://example.com/"

        def __init__(self):
            self.did = []
            self.mouse = self._Mouse(self)
            self.keyboard = self._Keyboard(self)

        class _Mouse:
            def __init__(self, page):
                self.page = page

            async def click(self, x, y):
                self.page.did.append(("click", x, y))

            async def wheel(self, dx, dy):
                self.page.did.append(("wheel", dy))

        class _Keyboard:
            def __init__(self, page):
                self.page = page

            async def press(self, key):
                self.page.did.append(("press", key))

            async def type(self, text, delay=None):
                self.page.did.append(("type", text))

        async def goto(self, *a, **k):
            self.did.append(("goto", a[0] if a else None))
            return type("R", (), {"status": 200})()

        async def reload(self, **k):
            self.did.append(("reload",))

        async def go_back(self, **k):
            self.did.append(("back",))

        async def set_viewport_size(self, size):
            self.did.append(("viewport", size["width"]))

        async def select_option(self, sel, value):
            self.did.append(("select", value))

        async def wait_for_timeout(self, ms):
            pass

        async def screenshot(self, **k):
            return b"\xff\xd8"

        async def title(self):
            return "T"

        async def inner_text(self, sel):
            return "text"

        async def evaluate(self, js):
            return [] if "meta" in js else None

        @property
        def viewport_size(self):
            return {"width": 1280, "height": 2134}

        @property
        def frames(self):
            return []

        @property
        def main_frame(self):
            return None

    def _owner(self):
        owner = browser._Owner()
        owner.view = self.FakePage()
        return owner

    def _run(self, owner, monkeypatch, action, **kwargs):
        import asyncio

        def drive(job, timeout):
            return asyncio.new_event_loop().run_until_complete(job(owner))

        monkeypatch.setattr(browser, "_submit", drive)
        monkeypatch.setattr(browser, "_settle", lambda page: asyncio.sleep(0))
        return browser.view_act(action, **kwargs)

    @pytest.mark.parametrize(
        "action,kwargs",
        [
            ("click", {"x": 10, "y": 20}),
            ("fill", {"text": "hello", "submit": False}),
            ("fill", {"text": "secret", "secret": True, "submit": True}),
            ("clear", {}),
            ("key", {"key": "Enter"}),
            ("scroll", {"dy": 400}),
            ("back", {}),
            ("refresh", {}),
            ("goto", {"url": "https://example.com/x"}),
            ("choose", {"value": "two"}),
            ("resize", {"width": 430, "height": 717}),
        ],
    )
    def test_the_action_runs_and_returns_a_frame(self, action, kwargs, monkeypatch):
        owner = self._owner()
        frame = self._run(owner, monkeypatch, action, **kwargs)
        assert isinstance(frame, browser.Frame)
        assert owner.view.did, f"{action} did nothing to the page"

    def test_the_detail_capture_really_reaches_chrome(self, monkeypatch):
        """EXECUTED, not read. `view_detail` goes straight to CDP because
        Playwright's screenshot() has no per-clip scale — a path nothing else
        here uses, so nothing else here would notice it breaking."""
        import asyncio
        import base64

        sent = []

        class FakeCDP:
            async def send(self, method, params):
                sent.append((method, params))
                return {"data": base64.b64encode(b"\xff\xd8patch").decode()}

            async def detach(self):
                sent.append(("detach", None))

        class FakeContext:
            async def new_cdp_session(self, page):
                return FakeCDP()

        owner = self._owner()
        owner.view.context = FakeContext()

        def drive(job, timeout):
            return asyncio.new_event_loop().run_until_complete(job(owner))

        monkeypatch.setattr(browser, "_submit", drive)
        patch = browser.view_detail(300, 460, 512, 780, 2.5)

        assert isinstance(patch, browser.Detail)
        assert patch.jpeg == b"\xff\xd8patch"
        method, params = sent[0]
        assert method == "Page.captureScreenshot"
        assert params["clip"] == {
            "x": 300, "y": 460, "width": 512, "height": 780, "scale": 2.5
        }
        assert params["format"] == "jpeg"
        # The scale CAPTURED rides back, so the client can tell a clamped patch
        # from the one it asked for.
        assert patch.scale == 2.5
        assert ("detach", None) in sent, "a CDP session was left on the target"

    def test_a_detail_with_no_view_open_is_nothing_rather_than_an_error(
        self, monkeypatch
    ):
        """A missing patch is a blurry patch, not a failure: the frame under it
        is still the page."""
        import asyncio

        owner = browser._Owner()
        owner.view = None
        monkeypatch.setattr(
            browser, "_submit",
            lambda job, timeout: asyncio.new_event_loop().run_until_complete(job(owner)),
        )
        assert browser.view_detail(0, 0, 100, 100, 2) is None

    def test_a_detail_capture_never_waits_for_the_page_to_settle(self, monkeypatch):
        """The page has not been touched — this is the same paint at more
        pixels. Settling would turn a sharpening into an interaction, and the
        owner is sitting there having stopped moving."""
        import inspect

        source = inspect.getsource(browser.view_detail)
        assert "_settle" not in source

    def test_a_click_really_clicks(self, monkeypatch):
        owner = self._owner()
        self._run(owner, monkeypatch, "click", x=11, y=22)
        assert ("click", 11.0, 22.0) in owner.view.did

    def test_a_password_fill_is_remembered_before_it_submits(self, monkeypatch):
        """Recorded after the Enter press, a fast navigation could increment the
        counter first and silently lose the sign-in question."""
        owner = self._owner()
        self._run(owner, monkeypatch, "fill", text="pw", secret=True, submit=True)
        assert owner.pending_signin == "example.com"
        assert owner.pending_nav == owner.navigations
