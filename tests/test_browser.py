"""The persistent browser reader, and the gate on reading as a signed-in owner.

Nothing here launches Chrome: `browser.read` / `open_for_login` are patched,
and conftest's `no_real_browser` makes any escape from that fail loudly.
"""

import urllib.error

import pytest

from aish import browser
from aish import web as web_module


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
                text="37,80 zl", title="Allegro", images=[], url=url, status=403
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
                text="prices", title="", images=[], url=url, status=403
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
                text="Zawiesie wężowe\n37,80 zł\n", title="", images=[],
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
