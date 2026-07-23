"""Export (Markdown -> PDF) tests. Pure functions — no server, no network."""

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
