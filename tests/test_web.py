"""Web tool tests: HTML extraction and result formatting run against fakes —
no network. One opt-in live test (AISH_LIVE_WEB=1) exercises the real backend.
"""

import os
import sys
import types

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
        monkeypatch.setattr(web, "_fetch", lambda url: ("%PDF-1.4", "application/pdf"))
        result = web.read_url("https://example.com/paper.pdf")
        assert result.startswith("ERROR")
        assert "application/pdf" in result

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


@pytest.mark.skipif(
    not os.environ.get("AISH_LIVE_WEB"), reason="set AISH_LIVE_WEB=1 to hit the network"
)
class TestLive:
    def test_search_and_read(self):
        result = web.web_search("python programming language")
        assert "1. " in result and "http" in result
        page = web.read_url("https://example.com")
        assert "Example Domain" in page
