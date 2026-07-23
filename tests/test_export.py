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
