"""Web browsing tools: search the web and read pages as plain text.

Both tools are read-only and auto-approved, but their input LEAVES THE
MACHINE (the query goes to DuckDuckGo, the URL to its host), so every call
is echoed to the user and the system prompt forbids putting private local
data into them. Fetching is restricted to http/https so read_url can never
be steered at file:// or other local schemes.
"""

import urllib.error
import urllib.request
from html.parser import HTMLParser

from .tools import DOCS_MAX_CHARS, _filter_topic, truncate

SEARCH_MAX_RESULTS = 5
SNIPPET_MAX_CHARS = 300
FETCH_TIMEOUT = 15
FETCH_MAX_BYTES = 2_000_000
# Some sites reject urllib's default UA outright; a browser-ish one is enough.
USER_AGENT = "Mozilla/5.0 (compatible; aish/0.1; +https://github.com/epnasis/aish)"

PAGE_TRUNCATION_HINT = (
    "\n[page truncated — call read_url again with a 'topic' (a word or phrase) "
    "to search the full page text]"
)

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "ul", "ol", "table", "section", "article",
    "header", "footer", "nav", "blockquote", "pre", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


class _TextExtractor(HTMLParser):
    """Visible text only: skips script/style subtrees, newlines at block tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # noqa: BLE001 — real-world HTML is hostile; keep what we got
        pass
    lines = [" ".join(line.split()) for line in "".join(extractor.parts).splitlines()]
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1]:
            out.append("")  # collapse blank runs to a single separator
    return "\n".join(out).strip()


def web_search(query: str, max_results: int = SEARCH_MAX_RESULTS) -> str:
    query = query.strip()
    if not query:
        return "ERROR: empty search query"
    from ddgs import DDGS  # deferred: keeps aish startup fast when unused

    try:
        results = DDGS().text(query, max_results=max_results)
    except Exception as exc:  # noqa: BLE001 — network/rate-limit errors are routine
        return f"ERROR: web search failed ({exc}) — retry once, or answer without the web"
    if not results:
        return f"NO RESULTS for {query!r} — try fewer or different keywords."

    lines = []
    for i, hit in enumerate(results, 1):
        title = " ".join((hit.get("title") or "(untitled)").split())
        url = hit.get("href") or hit.get("url") or ""
        snippet = " ".join((hit.get("body") or "").split())[:SNIPPET_MAX_CHARS]
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    lines.append("[call read_url on the most promising URL to read the page]")
    return truncate("\n".join(lines))


def read_url(url: str, topic: str | None = None) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return f"ERROR: read_url only fetches http(s) URLs (got {url!r})"

    try:
        text, content_type = _fetch(url)
    except urllib.error.HTTPError as exc:
        return f"ERROR: {url} returned HTTP {exc.code} {exc.reason}"
    except Exception as exc:  # noqa: BLE001 — DNS, TLS, timeouts: report, don't crash
        return f"ERROR: could not fetch {url}: {exc}"

    if content_type in ("text/html", "application/xhtml+xml"):
        text = html_to_text(text)
    elif not (content_type.startswith("text/") or content_type.endswith(("json", "xml"))):
        return f"ERROR: {url} is {content_type}, not a text page — cannot read it"
    if not text.strip():
        return f"ERROR: {url} returned no readable text"

    if topic:
        matched = _filter_topic(text, topic)
        if matched:
            return truncate(
                f"[{url} — lines matching {topic!r}]\n{matched}", head=DOCS_MAX_CHARS, tail=0
            )
        return truncate(
            f"[{url}] NO LINES MATCH {topic!r}; start of page instead:\n{text}",
            head=DOCS_MAX_CHARS,
            tail=0,
        )

    result = f"[{url}]\n{text}"
    if len(result) > DOCS_MAX_CHARS:
        return truncate(result, head=DOCS_MAX_CHARS, tail=0) + PAGE_TRUNCATION_HINT
    return result


def _fetch(url: str) -> tuple[str, str]:
    """Decoded body text and its content type, size-capped."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read(FETCH_MAX_BYTES)
    return raw.decode(charset, errors="replace"), content_type
