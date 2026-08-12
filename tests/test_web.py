"""Web tool tests: HTML extraction and result formatting run against fakes —
no network. One opt-in live test (AISH_LIVE_WEB=1) exercises the real backend.
"""

import email.message
import os
import ssl
import sys
import types
import urllib.error
import urllib.request

import pytest

from aish import web


class TestHtmlToText:
    def test_strips_script_and_style(self):
        html = (
            "<html><head><title>T</title><style>body{color:red}</style></head>"
            "<body><script>var x=1;</script><p>visible text</p></body></html>"
        )
        text = web.html_to_text(html)
        assert "visible text" in text
        assert "var x" not in text
        assert "color:red" not in text

    def test_block_tags_become_newlines(self):
        text = web.html_to_text("<p>one</p><p>two</p><div>three</div>")
        assert text.splitlines()[0] == "one"
        assert "two" in text and "three" in text

    def test_entities_decoded(self):
        assert web.html_to_text("<p>a &amp; b &lt;c&gt;</p>") == "a & b <c>"

    def test_blank_runs_collapsed(self):
        text = web.html_to_text("<div><div><div>deep</div></div></div><p>next</p>")
        assert "\n\n\n" not in text

    def test_nested_skip_tags(self):
        text = web.html_to_text("<script>a<style>b</style>c</script><p>keep</p>")
        assert text == "keep"

    def test_malformed_html_returns_partial(self):
        assert "start" in web.html_to_text("<p>start<b>unclosed")

    def test_title_extracted_not_in_body_text(self):
        html = "<html><head><title>  My   Page </title></head><body><p>body</p></body></html>"
        text, title, _images = web._extract(html)
        assert title == "My Page"
        assert "My Page" not in text
        assert "body" in text

    def test_read_url_stores_page_title(self, monkeypatch):
        page = "<html><head><title>Widget Manual</title></head><body><p>hi</p></body></html>"
        monkeypatch.setattr(web, "_fetch", lambda url: (page, "text/html"))
        monkeypatch.setattr(web, "PAGE_TITLES", {})
        web.read_url("https://example.com/manual")
        assert web.PAGE_TITLES["https://example.com/manual"] == "Widget Manual"


def fake_ddgs(results):
    """Install a fake ddgs module so web_search's deferred import finds it."""
    class FakeDDGS:
        def text(self, query, max_results=None):
            if isinstance(results, Exception):
                raise results
            return results

    module = types.ModuleType("ddgs")
    module.DDGS = FakeDDGS
    return module


class TestWebSearch:
    @pytest.fixture(autouse=True)
    def clean_ddgs(self, monkeypatch):
        self.monkeypatch = monkeypatch

    def install(self, results):
        self.monkeypatch.setitem(sys.modules, "ddgs", fake_ddgs(results))

    def test_formats_numbered_results(self):
        self.install(
            [
                {"title": "Python docs", "href": "https://docs.python.org", "body": "Official."},
                {"title": "Real Python", "href": "https://realpython.com", "body": "Tutorials."},
            ]
        )
        result = web.web_search("python")
        assert "1. Python docs" in result
        assert "https://docs.python.org" in result
        assert "2. Real Python" in result
        assert "read_url" in result  # nudge to open a page next

    def test_no_results(self):
        self.install([])
        assert "NO RESULTS" in web.web_search("zzz")

    def test_backend_error_reported_not_raised(self):
        self.install(RuntimeError("rate limited"))
        result = web.web_search("python")
        assert result.startswith("ERROR")
        assert "rate limited" in result

    def test_empty_query(self):
        assert web.web_search("   ").startswith("ERROR")

    def test_missing_keys_tolerated(self):
        self.install([{"href": "https://x.example"}])
        result = web.web_search("q")
        assert "(untitled)" in result
        assert "https://x.example" in result


PAGE = (
    "<html><body><h1>Widget Manual</h1><p>Widgets frob nicely.</p>"
    + "".join(f"<p>filler paragraph {i}</p>" for i in range(400))
    + "<p>The secret flag is --frobnicate.</p></body></html>"
)


class TestReadUrl:
    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://x", "javascript:alert(1)", "etc/passwd"):
            assert web.read_url(url).startswith("ERROR"), url

    def test_html_page_extracted_and_truncated_with_hint(self, monkeypatch):
        monkeypatch.setattr(web, "_fetch", lambda url: (PAGE, "text/html"))
        result = web.read_url("https://example.com/manual")
        assert "[https://example.com/manual]" in result
        assert "Widgets frob nicely" in result
        assert "page truncated" in result
        assert "'topic'" in result
        assert result.startswith(web.UNTRUSTED_NOTE)
        assert len(result) < web.DOCS_MAX_CHARS + 300 + len(web.UNTRUSTED_NOTE)

    def test_topic_reaches_past_truncation(self, monkeypatch):
        monkeypatch.setattr(web, "_fetch", lambda url: (PAGE, "text/html"))
        result = web.read_url("https://example.com/manual", topic="frobnicate")
        assert "--frobnicate" in result
        assert "lines matching 'frobnicate'" in result

    def test_topic_no_match_falls_back_to_head(self, monkeypatch):
        monkeypatch.setattr(web, "_fetch", lambda url: (PAGE, "text/html"))
        result = web.read_url("https://example.com/manual", topic="zzznope")
        assert "NO LINES MATCH" in result
        assert "Widget Manual" in result

    def test_plain_text_passed_through(self, monkeypatch):
        monkeypatch.setattr(web, "_fetch", lambda url: ("raw text body", "text/plain"))
        assert "raw text body" in web.read_url("https://example.com/robots.txt")

    def test_json_passed_through(self, monkeypatch):
        monkeypatch.setattr(web, "_fetch", lambda url: ('{"ok": true}', "application/json"))
        assert '"ok"' in web.read_url("https://api.example.com/status")

    def test_binary_content_refused(self, monkeypatch):
        monkeypatch.setattr(web, "_fetch", lambda url: (b"\x00\x01", "application/zip"))
        result = web.read_url("https://example.com/bundle.zip")
        assert result.startswith("ERROR")
        assert "application/zip" in result

    def test_a_pdf_names_the_tool_that_can_read_it(self, monkeypatch):
        """#213: this was the one content type aish routinely met on the web and
        could do nothing at all with. A refusal that names no alternative is
        where the model starts improvising with curl."""
        monkeypatch.setattr(web, "_fetch", lambda url: ("%PDF-1.4", "application/pdf"))
        result = web.read_url("https://example.com/paper.pdf")
        assert result.startswith("ERROR")
        assert 'read_pdf(source="https://example.com/paper.pdf")' in result

    def test_fetch_failure_reported_not_raised(self, monkeypatch):
        def boom(url):
            raise OSError("connection refused")

        monkeypatch.setattr(web, "_fetch", boom)
        result = web.read_url("https://down.example.com")
        assert result.startswith("ERROR")
        assert "connection refused" in result

    def test_empty_page_reported(self, monkeypatch):
        monkeypatch.setattr(web, "_fetch", lambda url: ("<html></html>", "text/html"))
        assert web.read_url("https://example.com/blank").startswith("ERROR")


def http_error_fetch(code, reason):
    def raiser(url):
        raise urllib.error.HTTPError(url, code, reason, None, None)

    return raiser


class TestJinaFallbackHint:
    def test_bot_block_codes_suggest_jina(self, monkeypatch):
        for code, reason in ((403, "Forbidden"), (429, "Too Many Requests"), (503, "Unavailable")):
            monkeypatch.setattr(web, "_fetch", http_error_fetch(code, reason))
            result = web.read_url("https://shop.example.com/price")
            assert result.startswith("ERROR"), result
            assert f"HTTP {code}" in result
            assert "https://r.jina.ai/https://shop.example.com/price" in result
            assert "secrets" in result  # the hint must carry the privacy warning

    def test_other_http_errors_get_no_hint(self, monkeypatch):
        for code, reason in ((404, "Not Found"), (500, "Server Error")):
            monkeypatch.setattr(web, "_fetch", http_error_fetch(code, reason))
            result = web.read_url("https://example.com/gone")
            assert result.startswith("ERROR")
            assert "r.jina.ai" not in result

    def test_empty_page_suggests_jina(self, monkeypatch):
        monkeypatch.setattr(web, "_fetch", lambda url: ("<html></html>", "text/html"))
        result = web.read_url("https://spa.example.com/app")
        assert result.startswith("ERROR")
        assert "https://r.jina.ai/https://spa.example.com/app" in result

    def test_failed_jina_url_not_suggested_again(self, monkeypatch):
        url = "https://r.jina.ai/https://shop.example.com/price"
        monkeypatch.setattr(web, "_fetch", http_error_fetch(403, "Forbidden"))
        assert "r.jina.ai/https://r.jina.ai" not in web.read_url(url)
        monkeypatch.setattr(web, "_fetch", lambda u: ("", "text/plain"))
        assert "r.jina.ai/https://r.jina.ai" not in web.read_url(url)


def fake_resolver(*addresses):
    """getaddrinfo stand-in returning the given literal addresses."""
    import socket

    def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses]

    return resolve


class TestSsrfGuard:
    BLOCKED_LITERALS = [
        "http://127.0.0.1:8787/",  # loopback
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://10.0.0.1/",  # RFC1918
        "http://192.168.1.1/admin",  # RFC1918
        "http://0.0.0.0/",  # unspecified
        "http://[::1]/",  # IPv6 loopback
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
    ]

    def test_blocks_non_public_ip_literals(self):
        for url in self.BLOCKED_LITERALS:
            with pytest.raises(web.BlockedURLError):
                web._require_public(url)

    def test_blocks_hostname_resolving_to_private(self, monkeypatch):
        monkeypatch.setattr(web.socket, "getaddrinfo", fake_resolver("192.168.10.20"))
        with pytest.raises(web.BlockedURLError):
            web._require_public("https://innocent.example.com/")

    def test_blocks_if_any_resolved_address_is_private(self, monkeypatch):
        monkeypatch.setattr(web.socket, "getaddrinfo", fake_resolver("93.184.216.34", "127.0.0.1"))
        with pytest.raises(web.BlockedURLError):
            web._require_public("https://dual.example.com/")

    def test_allows_public_hostname(self, monkeypatch):
        monkeypatch.setattr(web.socket, "getaddrinfo", fake_resolver("93.184.216.34"))
        web._require_public("https://example.com/")  # must not raise

    def test_dns_failure_blocked(self, monkeypatch):
        import socket as socket_module

        def fail(host, port, **kwargs):
            raise socket_module.gaierror("NXDOMAIN")

        monkeypatch.setattr(web.socket, "getaddrinfo", fail)
        with pytest.raises(web.BlockedURLError):
            web._require_public("https://nonexistent.example.com/")

    def test_redirect_to_private_refused(self):
        import urllib.request

        req = urllib.request.Request("https://public.example.com/page")
        handler = web._PublicOnlyRedirects()
        with pytest.raises(web.BlockedURLError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/steal")

    def test_redirect_to_public_followed(self, monkeypatch):
        import email.message
        import urllib.request

        monkeypatch.setattr(web.socket, "getaddrinfo", fake_resolver("93.184.216.34"))
        req = urllib.request.Request("https://public.example.com/page")
        handler = web._PublicOnlyRedirects()
        new_req = handler.redirect_request(
            req, None, 302, "Found", email.message.Message(), "https://public.example.com/other"
        )
        assert new_req.full_url == "https://public.example.com/other"

    def test_read_url_reports_blocked_with_alternative(self):
        result = web.read_url("http://127.0.0.1:8787/token")
        assert result.startswith("ERROR")
        assert "run_command" in result


class TestWireUrl:
    """A URL with non-ASCII in it must still be fetchable (#213).

    Browsers show the decoded form and send the encoded one; urllib sends what
    it is given, and http.client encodes the request line as ASCII and the Host
    header as latin-1 — so a link a human copied out of the address bar died
    with UnicodeEncodeError before a byte left the machine, and the model read
    "could not fetch" as a dead source."""

    def test_non_ascii_path_percent_encoded(self):
        assert web._wire_url("https://www.filmweb.pl/film/Krzyżacy-1960-1204") == (
            "https://www.filmweb.pl/film/Krzy%C5%BCacy-1960-1204"
        )

    def test_the_regression_url_is_now_ascii_and_requestable(self):
        for url in (
            "https://www.filmweb.pl/film/Krzyżacy-1960-1204",
            "https://pl.wikipedia.org/wiki/Krzyżacy_(powieść)",
        ):
            wire = web._wire_url(url)
            assert wire.isascii(), wire
            urllib.request.Request(wire).full_url.encode("ascii")  # what http.client does

    def test_parentheses_and_underscores_survive_wikipedia_style_paths(self):
        assert web._wire_url("https://pl.wikipedia.org/wiki/Krzyżacy_(powieść)") == (
            "https://pl.wikipedia.org/wiki/Krzy%C5%BCacy_(powie%C5%9B%C4%87)"
        )

    def test_already_encoded_url_unchanged(self):
        """Idempotence: re-encoding %C5%BC into %25C5%25BC would 404 a URL that
        works — and search results arrive already encoded."""
        encoded = "https://www.filmweb.pl/film/Krzy%C5%BCacy-1960-1204"
        assert web._wire_url(encoded) == encoded
        assert web._wire_url(web._wire_url("https://x.example/Krzyżacy")) == (
            web._wire_url("https://x.example/Krzyżacy")
        )

    def test_plain_ascii_url_passes_through_untouched(self):
        for url in (
            "https://example.com/",
            "https://example.com/a/b?x=1&y=2#frag",
            "https://example.com/path;p=1,2/@user!$&'()*+=~",
            "http://user:pw@example.com:8080/x",
            "https://api.example.com/v1?filter[]=a&filter[]=b",
        ):
            assert web._wire_url(url) == url, url

    def test_space_encoded(self):
        assert web._wire_url("https://example.com/my file.txt") == (
            "https://example.com/my%20file.txt"
        )

    def test_query_and_fragment_encoded_without_breaking_separators(self):
        assert web._wire_url("https://example.com/s?q=żółw&n=2#Ćma") == (
            "https://example.com/s?q=%C5%BC%C3%B3%C5%82w&n=2#%C4%86ma"
        )

    def test_internationalized_host_punycoded(self):
        assert web._wire_url("https://żółw.example/a") == (
            "https://xn--w-uga1v8h.example/a"
        )

    def test_internationalized_host_keeps_port_and_credentials(self):
        assert web._wire_url("https://user:pw@żółw.example:8443/a") == (
            "https://user:pw@xn--w-uga1v8h.example:8443/a"
        )

    def test_unencodable_host_left_alone_rather_than_raising(self):
        """An empty or over-long label is a broken host; failing with the
        request's own error beats inventing one here."""
        for url in ("https://ż..example/a", "https://" + "ż" * 300 + ".example/a"):
            web._wire_url(url)  # must not raise

    def test_fetch_checks_the_same_url_it_requests(self, monkeypatch):
        """The SSRF guard and the request must see byte-for-byte the same URL —
        encoding between them would leave a hop unchecked."""
        checked, opened = [], []
        monkeypatch.setattr(web, "_require_public", lambda url: checked.append(url))
        monkeypatch.setattr(
            web._opener, "open", lambda request, timeout=None: opened.append(request.full_url)
            or _FakeTextResponse()
        )
        web._fetch("https://www.filmweb.pl/film/Krzyżacy-1960-1204")
        assert checked == opened == ["https://www.filmweb.pl/film/Krzy%C5%BCacy-1960-1204"]

    def test_binary_fetch_encodes_too(self, monkeypatch):
        """show_image fetches remote images server-side; an image URL is as
        likely to carry a non-ASCII filename as a page URL."""
        checked, opened = [], []
        monkeypatch.setattr(web, "_require_public", lambda url: checked.append(url))
        monkeypatch.setattr(
            web._opener, "open", lambda request, timeout=None: opened.append(request.full_url)
            or _FakeTextResponse()
        )
        web.fetch_binary("https://example.com/zdjęcie.png", 1000)
        assert checked == opened == ["https://example.com/zdj%C4%99cie.png"]


class _FakeTextResponse:
    headers = email.message.Message()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=None):
        return b"ok"


class TestTrustStore:
    """One TLS trust store for every outbound fetch (#189).

    Python's default on macOS is Apple's legacy /etc/ssl/cert.pem, not the
    system trust store — years stale and missing newer roots, so fetches to
    whole swathes of the web failed with "unable to get local issuer
    certificate" while other hosts worked. It looked like a broken site rather
    than a broken client, and it silently affected read_url too, not only the
    image fetch that surfaced it."""

    def test_uses_the_current_root_set_not_the_platform_default(self):
        import certifi

        context = web._trust_store()
        loaded = {cert["subject"] for cert in context.get_ca_certs()}
        expected = {
            cert["subject"]
            for cert in ssl.create_default_context(cafile=certifi.where()).get_ca_certs()
        }
        assert loaded == expected
        assert loaded, "an empty trust store would refuse the whole web"

    def test_every_fetch_goes_through_it(self):
        """The store is useless if the opener does not carry it — and BOTH the
        text fetch (read_url) and the binary one (show_image) use this opener,
        which is why the fix belongs here rather than at either call site."""
        handlers = [
            h for h in web._opener.handlers
            if isinstance(h, urllib.request.HTTPSHandler)
        ]
        assert handlers, "no HTTPSHandler on the shared opener"
        contexts = [getattr(h, "_context", None) for h in handlers]
        assert any(c is not None for c in contexts)
        for context in contexts:
            if context is None:
                continue
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname is True

    def test_the_redirect_guard_is_still_wired(self):
        """Adding the HTTPS handler must not displace the SSRF re-check —
        verifying certificates on a fetch aimed at cloud metadata is no win."""
        assert any(
            isinstance(h, web._PublicOnlyRedirects) for h in web._opener.handlers
        )


@pytest.mark.skipif(
    not os.environ.get("AISH_LIVE_WEB"), reason="set AISH_LIVE_WEB=1 to hit the network"
)
class TestLive:
    def test_search_and_read(self):
        result = web.web_search("python programming language")
        assert "1. " in result and "http" in result
        page = web.read_url("https://example.com")
        assert "Example Domain" in page

    def test_live_fetch_of_a_modern_root_host(self):
        """The regression itself: this host chains to GlobalSign Root R46, which
        Apple's legacy bundle lacks. It failed before #189 and must not again."""
        data, content_type = web.fetch_binary(
            "https://images.unsplash.com/photo-1502680390469-be75c86b636f?w=200", 5_000_000
        )
        assert content_type.startswith("image/") and len(data) > 1000



class TestPageImages:
    """#212 follow-up. A rule requires a picture; `read_url` returned the
    article's text with every image URL stripped out; so the model had nothing
    to hand `show_image` but a GUESS — a filename invented from the headline
    that matched the site's URL pattern. It 404'd, and show_image's own advice
    ("read_url the page again for a working one") sent it back to the reader
    that had removed them. Seven of eight show_image calls failed that way in
    one real session, and the tail of that run is the loop.

    Measured on the two live pages behind it: the real URLs were there and
    returned HTTP 200, one of them behind a hashed CDN path
    (`/t/GEiBu2iaA9GrrmRRMXVC3ULLfZo=/2500x/…`) that no model could ever guess.
    """

    PAGE = (
        '<html><head><title>Leak</title>'
        '<meta property="og:image" content="/img/hero.jpg">'
        '<meta name="twitter:image" content="https://cdn.test/t/HASH=/2500x/a.jpg">'
        "</head><body><p>The phone folds.</p>"
        '<img src="https://cdn.test/logo.svg"></body></html>'
    )

    def test_declared_images_are_returned_and_absolutised(self):
        text, title, images = web._extract(self.PAGE, base_url="https://site.test/a/b")
        assert title == "Leak"
        assert "The phone folds." in text
        # og:image is site-relative on a great many sites; a relative URL is
        # exactly as useless to show_image as no URL at all.
        assert images == [
            "https://site.test/img/hero.jpg",
            "https://cdn.test/t/HASH=/2500x/a.jpg",
        ]

    def test_the_img_soup_is_not_offered(self):
        """Only what the page DECLARES as its subject. The raw <img> list is
        logos, avatars and tracking pixels — 44 and 26 of them on the two real
        articles, both led by the site logo."""
        _text, _title, images = web._extract(self.PAGE, base_url="https://site.test/")
        assert not any("logo" in u for u in images)

    def test_a_page_with_no_declared_image_says_nothing(self):
        _t, _ti, images = web._extract("<html><body><p>hi</p></body></html>", base_url="https://x.test/")
        assert images == []
        assert web.image_note(images) == ""

    def test_the_note_names_the_urls_and_forbids_inventing_one(self):
        note = web.image_note(["https://cdn.test/a.jpg"])
        assert "https://cdn.test/a.jpg" in note
        assert "VERBATIM" in note and "invent" in note

    def test_read_url_appends_the_note_after_truncation(self, monkeypatch):
        """The image URLs are the POINT of the read on a "show me" task, so the
        page cap must not be able to cut the one thing that ends the guessing."""
        big = self.PAGE.replace("The phone folds.", "x " * 200_000)
        monkeypatch.setattr(web, "_fetch", lambda _u: (big, "text/html"))
        out = web.read_url("https://site.test/a")
        assert "[page truncated" in out, "expected this fixture to exceed the cap"
        assert "https://site.test/img/hero.jpg" in out


class TestVideoIds:
    """The two directions of the same whitelist: what the app can PLAY, and what
    is a video's STILL. Both are parsers, not judgement — the caller decides what
    to do with "" (`show_video` refuses, `show_image` just stays a picture)."""

    def test_the_three_playable_shapes(self):
        assert web.video_id("https://youtube.com/watch?v=lqltp2QaT30") == "lqltp2QaT30"
        assert web.video_id("https://youtu.be/lqltp2QaT30") == "lqltp2QaT30"
        assert web.video_id("https://www.youtube.com/shorts/lqltp2QaT30") == "lqltp2QaT30"

    def test_a_page_about_a_video_is_not_a_video(self):
        assert web.video_id("https://youtube.com/@ProfessorGerdes") == ""
        assert web.video_id("https://example.com/watch?v=lqltp2QaT30") == ""

    def test_both_thumbnail_hosts_and_the_webp_path(self):
        """i.ytimg.com is what the API and `youtube_analyze` hand out;
        img.youtube.com is the older alias the web card falls back to. Any size
        file name — the id is in the path, and every size shares it."""
        for url in (
            "https://i.ytimg.com/vi/lqltp2QaT30/hqdefault.jpg",
            "https://i9.ytimg.com/vi/lqltp2QaT30/maxresdefault.jpg",
            "https://img.youtube.com/vi/lqltp2QaT30/0.jpg",
            "https://i.ytimg.com/vi_webp/lqltp2QaT30/sddefault.webp",
        ):
            assert web.thumbnail_video_id(url) == "lqltp2QaT30", url

    def test_an_ordinary_image_url_is_not_a_thumbnail(self):
        """The composed poster line hangs off this: a false positive would wrap
        somebody's photo in a link to an unrelated video."""
        assert web.thumbnail_video_id("https://example.com/vi/lqltp2QaT30/hq.jpg") == ""
        assert web.thumbnail_video_id("https://i.ytimg.com/an/UCxyz/thumb.jpg") == ""
        assert web.thumbnail_video_id("https://cdn.test/hero.jpg") == ""
        assert web.thumbnail_video_id("") == ""


class TestStripTracking:
    """Share tokens identify the person who shared a link, not the thing it
    points at (#216 follow-up)."""

    def test_youtube_share_token_goes(self):
        assert (
            web.strip_tracking("https://youtu.be/ORziFM6lseY?si=vyM8z4tmg27lRix4")
            == "https://youtu.be/ORziFM6lseY"
        )

    def test_utm_family_goes_by_prefix(self):
        cleaned = web.strip_tracking("https://ex.com/a?utm_source=x&utm_medium=y&id=7")
        assert cleaned == "https://ex.com/a?id=7"

    def test_an_unknown_parameter_is_kept(self):
        """A denylist: an unrecognized parameter may be load-bearing, and a
        signed URL is nothing but unrecognized parameters."""
        assert web.strip_tracking("https://ex.com/a?token=abc") == "https://ex.com/a?token=abc"

    def test_the_fragment_and_path_are_untouched(self):
        assert web.strip_tracking("https://ex.com/a/b?si=1#frag") == "https://ex.com/a/b#frag"

    def test_a_stripped_youtube_link_is_still_playable(self):
        """show_video validates against the app's own pattern, so stripping
        must not produce a link the frontend then refuses."""
        cleaned = web.strip_tracking("https://www.youtube.com/watch?v=ORziFM6lseY&si=abc")
        assert web.video_id(cleaned) == "ORziFM6lseY"
