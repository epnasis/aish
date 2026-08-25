"""Export (Markdown -> PDF) tests. Pure functions — no server, no network."""

import email.message

from aish import export


def _pdf_ok(data: bytes) -> bool:
    return data.startswith(b"%PDF-") and len(data) > 1000


def test_tall_code_block_after_prose_paginates_not_crashes():
    """A code block taller than the remaining page, preceded by prose, used to
    abort the whole PDF: xhtml2pdf made <pre> one unsplittable flowable and
    reportlab's LayoutError surfaced as `sequence item 0: expected str instance,
    list found` (issue #147). It must now render."""
    prose = "\n\n".join(f"Paragraph {i}. " + "word " * 30 for i in range(6))
    code = "\n".join(f'    "key_{i}": {i},' for i in range(60))
    markdown = f"{prose}\n\n```json\n{{\n{code}\n}}\n```\n"
    assert _pdf_ok(export.render_answer_pdf(markdown, "T", ()))


def test_long_unbreakable_code_line_is_wrapped():
    """With CJK word-wrap gone, an over-long token must be hard-wrapped so it
    can't run off the page (and can't reintroduce the unsplittable flowable)."""
    markdown = "text\n\n```\n" + "x" * 400 + "\n```\n"
    assert _pdf_ok(export.render_answer_pdf(markdown, "T", ()))


def test_reflow_preserves_indentation_and_escapes_entities():
    html = export._reflow_code_blocks(
        '<pre><code>def f():\n    return 1 &lt; 2 &amp; 3</code></pre>'
    )
    assert 'class="codeblock"' in html
    assert "<pre" not in html
    assert "&nbsp;&nbsp;&nbsp;&nbsp;return" in html  # indentation kept
    assert "&lt;" in html and "&amp;" in html  # entities intact, not split


def test_reflow_hard_wraps_over_long_line():
    token = "a" * (export._CODE_MAX_COLS + 50)
    html = export._reflow_code_blocks(f"<pre><code>{token}</code></pre>")
    # the single long token is broken by <br/> into <= _CODE_MAX_COLS chunks
    longest_run = max(len(seg) for seg in html.replace("<br/>", "\x00").split("\x00"))
    assert longest_run <= export._CODE_MAX_COLS + len('<div class="codeblock">')


def test_plain_answer_still_renders():
    assert _pdf_ok(export.render_answer_pdf("# Hello\n\nA normal answer.", "T", ()))


# ---- nested blocks in lists (#172, the PDF half) ---------------------------
# The markdown the model writes constantly — a numbered list whose items carry
# a paragraph, a code block, or a sub-list — used to lose BOTH its code blocks
# (Python-Markdown's fenced_code ignores an indented fence) and its numbering
# (xhtml2pdf drops an <li> marker as soon as the item has block content).

_NESTED_LIST_MD = """\
1. **First item**

    A nested paragraph.

2. **Second item**

    ```javascript
    function nested() {
        return true;
    }
    ```

    > A blockquote belonging to item 2.

3. **Third item**

    - a sub-bullet
    - another
"""


def test_fenced_code_indented_under_a_list_item_is_a_code_block():
    html = export._markdown_to_html_fragment(_NESTED_LIST_MD)
    assert 'class="codeblock"' in html
    assert "function nested()" in html
    # The info string leaking into the text is the signature of the old bug:
    # the fence degraded to one run of inline <code> starting "javascript…".
    assert "<code>javascript" not in html
    assert "```" not in html


def test_list_markers_survive_block_content():
    html = export._markdown_to_html_fragment(_NESTED_LIST_MD)
    assert "<li>" not in html  # lists are rewritten; xhtml2pdf can't mark up an <li>
    for n in (1, 2, 3):
        assert f"{n}.{export._NBSP * 2}" in html
    assert "text-indent: -" in html  # markers hang outside the item's text column


def test_blockquote_indented_under_a_list_item_survives():
    html = export._markdown_to_html_fragment(_NESTED_LIST_MD)
    assert "<blockquote>" in html
    assert "A blockquote belonging to item 2." in html


def test_ordered_list_start_is_kept():
    html = export._markdown_to_html_fragment("3. three\n4. four\n")
    assert f"3.{export._NBSP * 2}three" in html
    assert f"4.{export._NBSP * 2}four" in html


def test_nested_list_indents_relative_to_its_parent_item():
    """A sub-list lives inside its item's continuation <div>, so its own hanging
    indent stacks on top of the parent's — xhtml2pdf renders nested <ul>s flat."""
    html = export._markdown_to_html_fragment("1. outer\n\n    - inner\n")
    outer_cont = html.index("margin: 0 0 0 ")  # the item's continuation wrapper
    assert html.index(export._BULLET, outer_cont) > outer_cont


def test_a_markdown_demo_block_is_not_raided_for_nested_fences():
    """A column-0 ```markdown block showing an indented fence must survive
    verbatim: treating its inner fence as a nested block would end the outer one
    early and desync every block after it."""
    md = "```markdown\n1. item\n    ```python\n    inner\n    ```\n```\n\nAfter.\n"
    html = export._markdown_to_html_fragment(md)
    assert html.count('class="codeblock"') == 1  # one block, not two
    assert "```python" in html  # the demo's inner fence stayed literal text
    assert "<p>After.</p>" in html  # nothing after it was swallowed


def test_tall_code_block_inside_a_list_item_still_paginates():
    """The rescued fence goes through the same <br>-split reflow as any other,
    so the issue-#147 pagination guarantee holds inside a list too."""
    code = "\n".join(f'    "key_{i}": {i},' for i in range(70))
    markdown = f"1. big one\n\n    ```json\n{code}\n    ```\n\n2. after\n"
    assert _pdf_ok(export.render_answer_pdf(markdown, "T", ()))


def test_nested_list_answer_renders_to_a_pdf():
    assert _pdf_ok(export.render_answer_pdf(_NESTED_LIST_MD, "T", ()))


def test_dedent_line_only_eats_spaces_it_is_allowed_to():
    assert export._dedent_line("        x", 4) == "    x"
    assert export._dedent_line("  x", 4) == "x"  # fewer than asked for is fine
    assert export._dedent_line("\tx", 4) == "\tx"  # never touches other whitespace


# ---- titling an exported answer (#172) -------------------------------------


def test_derive_title_prefers_a_lead_heading():
    assert export.derive_title("# Quarterly report\n\nBody text.") == "Quarterly report"
    assert export.derive_title("### 1. Using a parser\n\nBody.") == "1. Using a parser"


def test_derive_title_uses_the_first_prose_when_there_is_no_lead_heading():
    """A later heading is a section, not the document's title — the opening
    prose is what the reader meets first."""
    md = "You are correct. `gmail_search` filters.\n\n## Details\n\nMore."
    assert export.derive_title(md) == "You are correct. gmail_search filters"


def test_derive_title_trims_a_long_lead_to_its_first_sentence():
    md = "Short opener. " + "Then a much longer second sentence that runs on. " * 3
    assert export.derive_title(md) == "Short opener"


def test_derive_title_skips_code_blocks_and_rules():
    md = "---\n\n```python\n# not a title\nx = 1\n```\n\nThe actual opening line."
    assert export.derive_title(md) == "The actual opening line"


def test_derive_title_strips_inline_markup_and_list_markers():
    md = "- **Bold lead** with a [link](https://example.com) and `code`"
    assert export.derive_title(md) == "Bold lead with a link and code"


def test_derive_title_truncates_on_a_word_boundary():
    md = "Here is a difficult nested markdown test case to see how the renderer copes."
    title = export.derive_title(md)
    assert len(title) <= export.TITLE_MAX + 1  # +1 for the ellipsis
    assert title.endswith("…") and "  " not in title
    assert md.startswith(title[:-1])  # a prefix of the source, never a cut word


def test_derive_title_falls_back_on_empty_content():
    assert export.derive_title("", "aish answer") == "aish answer"
    assert export.derive_title("```\nonly code\n```", "aish answer") == "aish answer"


def test_clean_title_unwraps_what_models_pad_a_title_with():
    assert export.clean_title('  "Bali eSIM data plans"  ') == "Bali eSIM data plans"
    assert export.clean_title("Title: **Quarterly report**") == "Quarterly report"
    assert export.clean_title("## Zmiana lotu na QR960") == "Zmiana lotu na QR960"
    # Models often preamble before the real line; the last line is the title.
    assert export.clean_title("Sure, here you go:\nBali eSIM data plans") == (
        "Bali eSIM data plans"
    )


def test_clean_title_rejects_a_non_title_so_the_caller_can_fall_back():
    assert export.clean_title("") is None
    assert export.clean_title("   \n\n  ") is None
    assert export.clean_title("I'm sorry, but I cannot " + "help with that. " * 12) is None


def _tiny_png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    return buf.getvalue()


def test_youtube_shorts_url_renders_embed_card(monkeypatch):
    # #149: a /shorts/<id> link must produce the same YouTube embed card as a
    # watch/youtu.be link, with the id captured for the thumbnail fetch.
    requested = []

    def fake_fetch(url: str) -> bytes:
        requested.append(url)
        return _tiny_png()

    monkeypatch.setattr(export, "fetch_image", fake_fetch)
    html = '<a href="https://www.youtube.com/shorts/dQw4w9WgXcQ">a short</a>'
    out = export._MediaEmbedder(()).process(html)

    assert "aish-embed" in out and "YouTube video" in out
    assert "data:image/png;base64," in out  # the thumbnail was inlined
    assert requested == ["https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg"]


def test_a_posters_own_picture_is_the_card_and_is_not_fetched_again(monkeypatch, tmp_path):
    """#217, the PDF half. The composed `[![still](file)](video)` form reached the
    link pass as an <a> whose LABEL was an <img>, and that label went out as the
    card's CAPTION — where the image pass then inlined it — beside a freshly
    fetched copy of the same still. Two pictures in the PDF and a network fetch
    for bytes already on disk."""
    requested = []

    def fake_fetch(url: str) -> bytes:
        requested.append(url)
        return _tiny_png()

    monkeypatch.setattr(export, "fetch_image", fake_fetch)
    still = tmp_path / "still.png"
    still.write_bytes(_tiny_png())
    html = (
        f'<a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">'
        f'<img alt="Ukraine hit Wildberries" src="{still}"/></a>'
    )
    out = export._MediaEmbedder((tmp_path,)).process(html)

    assert requested == [], "the still on disk was fetched from YouTube anyway"
    assert out.count("<img") == 1, "the picture is in the PDF twice"
    assert "data:image/png;base64," in out
    assert "aish-embed" in out and "YouTube video" in out
    # The alt text is where the poster form keeps its words, so it becomes the
    # caption — a card captioned with the raw URL loses what the answer called it.
    assert "Ukraine hit Wildberries" in out


def test_a_poster_outside_the_roots_falls_back_to_youtubes_own_thumbnail(monkeypatch, tmp_path):
    """An unembeddable poster must not take the card down with it: the local file
    is refused (never read), and the card is the one it would have been before."""
    monkeypatch.setattr(export, "fetch_image", lambda url: _tiny_png())
    outside = tmp_path / "outside.png"
    outside.write_bytes(_tiny_png())
    html = (
        f'<a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">'
        f'<img alt="a still" src="{outside}"/></a>'
    )
    out = export._MediaEmbedder((tmp_path / "roots",)).process(html)
    assert "aish-embed" in out and "data:image/png;base64," in out


def test_youtube_shorts_shares_regex_with_watch_and_short_host():
    # The three URL shapes must all match and yield the SAME 11-char id via the
    # group(1) or group(2) read (kept in lockstep with app.js YOUTUBE_RE).
    vid = "dQw4w9WgXcQ"
    for url in (
        f"https://www.youtube.com/shorts/{vid}",
        f"https://www.youtube.com/watch?v={vid}",
        f"https://youtu.be/{vid}",
        f"https://www.youtube.com/shorts/{vid}?feature=share",
    ):
        m = export._YOUTUBE_RE.match(url)
        assert m is not None, url
        assert (m.group(1) or m.group(2)) == vid, url


# ---- SSRF guard on export-time image fetches (#178 P1-4) -------------------
# fetch_image runs on URLs the MODEL wrote into an answer, from the trusted
# server host, the moment the owner taps export — so it must refuse private /
# link-local targets exactly like web.py's read_url does, and a refusal must
# degrade to the link card, never crash the export.


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "image/png"

    def read(self, n: int) -> bytes:
        return self._data[:n]

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _no_network(monkeypatch):
    """Fail the test if anything tries to open a socket-backed URL."""

    def boom(*args, **kwargs):  # pragma: no cover - reaching it IS the failure
        raise AssertionError("network fetch attempted for a blocked URL")

    monkeypatch.setattr(export.web.urllib.request, "urlopen", boom)
    monkeypatch.setattr(export.web._opener, "open", boom)


def test_fetch_image_refuses_private_and_link_local_targets(monkeypatch):
    _no_network(monkeypatch)
    for url in (
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://192.168.10.1/admin?exfil=payload",  # LAN
        "http://127.0.0.1:8080/",  # localhost
        "http://[::1]/",  # IPv6 loopback
    ):
        assert export.fetch_image(url) is None, url


def test_fetch_image_refuses_a_public_name_resolving_privately(monkeypatch):
    # DNS-level SSRF: innocent-looking hostname, private A record.
    _no_network(monkeypatch)
    monkeypatch.setattr(
        export.web.socket,
        "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("10.0.0.7", 0))],
    )
    assert export.fetch_image("https://innocent.example.com/x.png") is None


def test_fetch_image_still_fetches_public_hosts(monkeypatch):
    monkeypatch.setattr(
        export.web.socket,
        "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    png = _tiny_png()
    opened = []

    def fake_open(request, timeout=None):
        opened.append(request.full_url)
        return _FakeResponse(png)

    monkeypatch.setattr(export.web._opener, "open", fake_open)
    assert export.fetch_image("https://example.com/x.png") == png
    assert opened == ["https://example.com/x.png"]


def test_fetch_image_encodes_a_non_ascii_url(monkeypatch):
    """An image filename may be non-ASCII; HTTP request lines may not (#213),
    so without encoding the export silently degraded to a link card."""
    monkeypatch.setattr(
        export.web.socket,
        "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    png = _tiny_png()
    opened = []

    def fake_open(request, timeout=None):
        opened.append(request.full_url)
        return _FakeResponse(png)

    monkeypatch.setattr(export.web._opener, "open", fake_open)
    assert export.fetch_image("https://example.com/zdjęcie.png") == png
    assert opened == ["https://example.com/zdj%C4%99cie.png"]


def test_blocked_image_degrades_to_link_card_not_a_crash(monkeypatch):
    _no_network(monkeypatch)
    html = '<img src="http://169.254.169.254/latest/meta-data/" alt="diagram" />'
    out = export._MediaEmbedder(()).process(html)
    assert "aish-link-card" in out  # captioned link card, the export still renders
    assert "169.254.169.254" in out  # target shown as a link, never fetched
    assert "<img" not in out  # no embedded image element survives
