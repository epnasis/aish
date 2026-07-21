"""Local Markdown → PDF export for the web UI (issue #64).

Conversion is fully local: `markdown` renders to HTML and `xhtml2pdf` (pure
Python, reportlab-backed, no system libraries) renders HTML to PDF bytes.
Nothing is sent to any external service.

Two shapes are exported: one answer (the markdown the user is looking at) and
a whole session's FINAL answers only. "Final answer" is a structural property
of the persisted log — an assistant message is final iff it is NOT immediately
followed by a tool result. Tool-calling turns are always followed by a `tool`
message (the loop only returns after a text-only turn), so this cleanly drops
thinking/tool/working steps and keeps just the answers the user saw.
"""

import io
import re
import unicodedata
from datetime import datetime
from pathlib import Path

# The heavy imports (reportlab via xhtml2pdf) are deferred into the render
# function so importing this module — and starting the server — stays cheap.

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

# Letters that survive NFKD normalization intact (no combining-mark
# decomposition), so an explicit ASCII fallback is the only way to keep them.
# ł/Ł are the load-bearing case for Polish ("Zażółć" would otherwise lose it).
_TRANSLIT_MAP = str.maketrans(
    {
        "ł": "l", "Ł": "L",
        "đ": "d", "Đ": "D",
        "ø": "o", "Ø": "O",
        "ß": "ss",
        "æ": "ae", "Æ": "AE",
        "œ": "oe", "Œ": "OE",
        "þ": "th", "Þ": "Th",
        "ð": "d", "Ð": "D",
    }
)

# Bundled Source Sans 3 / Source Code Pro (Adobe, SIL OFL 1.1) — the PDF
# built-ins (Helvetica/Courier) have no Polish/CE glyphs and render them as
# black boxes. These cover Latin Extended-A, arrows, typographic punctuation,
# etc. Embedded via @font-face with the family names below so the glyphs
# actually draw (and the PDF is portable). No italic face ships — italic body
# text falls back to the regular Source Sans 3 face.
_FONT_DIR = Path(__file__).parent / "fonts"


def _font_face_css() -> str:
    faces = [
        ("aishSans", "SourceSans3-Regular.ttf", "normal", "normal"),
        ("aishSans", "SourceSans3-Bold.ttf", "bold", "normal"),
        ("aishMono", "SourceCodePro-Regular.ttf", "normal", "normal"),
        ("aishMono", "SourceCodePro-Bold.ttf", "bold", "normal"),
        # Monochrome emoji fallback (Noto Emoji, OFL): reportlab can't render
        # colour emoji, so this covers pictographs as black outlines. Emoji runs
        # are wrapped in a span with this family (reportlab does no auto-fallback).
        ("aishEmoji", "NotoEmoji-Regular.ttf", "normal", "normal"),
    ]
    return "\n".join(
        f'@font-face {{ font-family: "{fam}"; '
        f'src: url("fonts/{fn}"); '
        f"font-weight: {weight}; font-style: {style}; }}"
        for fam, fn, weight, style in faces
    )


def _link_callback(uri: str, rel: str) -> str:  # noqa: ARG001 — xhtml2pdf signature
    """Resolve the bundled-font URLs (fonts/<name>.ttf) to their real on-disk
    path so reportlab can embed them. Everything the exporter references is
    local and bundled; nothing hits the network."""
    name = uri.rsplit("/", 1)[-1]
    local = _FONT_DIR / name
    return str(local) if local.exists() else uri


# Kept simple and readable; xhtml2pdf supports only a CSS subset, so this
# leans on what it renders reliably (fonts, colors, table borders, spacing).
_PAGE_CSS = """
@page { size: a4; margin: 2cm 1.8cm; }
body { font-family: "aishSans"; font-size: 11pt;
       line-height: 1.45; color: #1c1c1e; }
h1 { font-size: 20pt; margin: 0 0 10pt; }
h2 { font-size: 15pt; margin: 16pt 0 6pt; }
h3 { font-size: 12.5pt; margin: 14pt 0 5pt; }
p { margin: 0 0 8pt; }
a { color: #0a63c9; text-decoration: none; }
ul, ol { margin: 0 0 8pt; }
li { margin: 0 0 3pt; }
code { font-family: "aishMono"; font-size: 9.5pt;
       background: #f2f2f7; color: #1c1c1e; }
/* -pdf-word-wrap: CJK breaks long unbreakable strings at any character so code
   output / command dumps / wide table cells wrap to the page instead of running
   off the right edge (xhtml2pdf maps it to reportlab's wordWrap='CJK'). */
pre { font-family: "aishMono"; font-size: 9pt; background: #f2f2f7;
      color: #1c1c1e; padding: 8pt; margin: 0 0 8pt;
      white-space: pre-wrap; -pdf-word-wrap: CJK; }
blockquote { color: #6c6c70; margin: 0 0 8pt; padding: 0 0 0 10pt;
             border-left: 2pt solid #d1d1d6; }
table { border-collapse: collapse; margin: 0 0 8pt; }
th, td { border: 0.5pt solid #d1d1d6; padding: 4pt 7pt; font-size: 10pt;
         -pdf-word-wrap: CJK; }
th { background: #f2f2f7; }
hr { border: none; border-top: 0.5pt solid #d1d1d6; margin: 14pt 0; }
.aish-doc-header { color: #8e8e93; font-size: 9pt; margin: 0 0 14pt;
                   border-bottom: 0.5pt solid #e5e5ea; padding-bottom: 8pt; }
.aish-answer + .aish-answer { border-top: 0.5pt solid #e5e5ea;
                             margin-top: 16pt; padding-top: 16pt; }
"""

_MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]


def safe_pdf_filename(title: str, fallback: str = "aish-export") -> str:
    """A download-safe `<slug>.pdf` name derived from a title.

    Non-ASCII letters are transliterated to ASCII rather than stripped, so
    "Zażółć gęślą jaźń" becomes "Zazolc-gesla-jazn" instead of "Za----".
    Most accented letters decompose under NFKD to a base letter + combining
    mark (dropped here); the few that don't (notably ł/Ł) go through an
    explicit map first.
    """
    ascii_text = unicodedata.normalize("NFKD", (title or "").translate(_TRANSLIT_MAP))
    ascii_text = "".join(ch for ch in ascii_text if not unicodedata.combining(ch))
    ascii_text = ascii_text.encode("ascii", "ignore").decode("ascii")
    slug = _UNSAFE_FILENAME.sub("-", ascii_text.strip()).strip("-._")
    slug = slug[:60] or fallback
    return f"{slug}.pdf"


def session_answers(messages: list[dict]) -> list[str]:
    """The FINAL answers from a session's logged messages, in order.

    An assistant message counts as a final answer only when the next message
    is not a tool result — every tool-calling (working) turn is followed by a
    `tool` message, so those, and any thinking, are excluded. Empty answers
    (a turn that produced no visible text) are dropped.
    """
    answers: list[str] = []
    for i, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        content = (message.get("content") or "").strip()
        if not content:
            continue
        following = messages[i + 1] if i + 1 < len(messages) else None
        if following is not None and following.get("role") == "tool":
            continue  # an intermediate tool-calling turn, not a final answer
        answers.append(content)
    return answers


def assemble_session_markdown(messages: list[dict], title: str) -> str:
    """One markdown document of a session's final answers, separated by rules.
    Returns "" when the session has no final answers yet."""
    answers = session_answers(messages)
    if not answers:
        return ""
    return "\n\n---\n\n".join(answers)


# Web-only interactive bits that make no sense in a printed PDF.
_AISH_REPLY_RE = re.compile(r"\[[^\]]*\]\(\s*aish-reply://[^)]*\)")
_NO_CHIPS_RE = re.compile(r"\[no-chips\]", re.IGNORECASE)


_VARIATION_SELECTORS_RE = re.compile(r"[︀-️]")


def _drop_variation_selectors(text: str) -> str:
    """Emoji variation selectors (U+FE0E/FE0F) only pick colour-vs-text
    presentation, which is moot in a monochrome PDF — and left in, they render
    as a stray tofu box after their base glyph. Strip them everywhere."""
    return _VARIATION_SELECTORS_RE.sub("", text or "")


def _strip_web_only(markdown_text: str) -> str:
    """Drop quick-reply chips ([label](aish-reply://…)) and the [no-chips] tag,
    then tidy the whitespace they leave behind."""
    text = _AISH_REPLY_RE.sub("", markdown_text or "")
    text = _NO_CHIPS_RE.sub("", text)
    text = _drop_variation_selectors(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_EMOJI_JOINERS = frozenset({0x200D, 0xFE0E, 0xFE0F})  # ZWJ + variation selectors
_TAG_SPLIT_RE = re.compile(r"(<[^>]*>)")
_emoji_cps: frozenset[int] | None = None


def _emoji_codepoints() -> frozenset[int]:
    """Codepoints to route to the emoji font: those the body font (Source Sans)
    lacks but the emoji font has, plus skin-tone modifiers. Text symbols Source
    Sans already draws (→ ± ✓ …) keep their normal look. Cached (font cmaps are
    read once, lazily, so import stays cheap)."""
    global _emoji_cps
    if _emoji_cps is None:
        from reportlab.pdfbase.ttfonts import TTFont  # reportlab is a hard dep

        def cmap(name: str) -> set[int]:
            return set(TTFont(name, str(_FONT_DIR / name)).face.charToGlyph)

        sans = cmap("SourceSans3-Regular.ttf")
        emoji = cmap("NotoEmoji-Regular.ttf")
        modifiers = set(range(0x1F3FB, 0x1F400))
        _emoji_cps = frozenset((emoji - sans) | modifiers)
    return _emoji_cps


def _wrap_emoji(html: str) -> str:
    """Wrap emoji runs in a span naming the emoji font — reportlab does no
    per-glyph fallback, so a run must explicitly select the font that has it.
    Only text between tags is processed, so tags/attributes stay intact."""
    emoji = _emoji_codepoints()

    def emojiish(ch: str) -> bool:
        cp = ord(ch)
        return cp in emoji or cp in _EMOJI_JOINERS

    def wrap_text(text: str) -> str:
        out: list[str] = []
        i, n = 0, len(text)
        while i < n:
            if emojiish(text[i]) and ord(text[i]) not in _EMOJI_JOINERS:
                j = i + 1
                while j < n and emojiish(text[j]):
                    j += 1
                out.append(f'<span style="font-family: aishEmoji">{text[i:j]}</span>')
                i = j
            else:
                out.append(text[i])
                i += 1
        return "".join(out)

    parts = _TAG_SPLIT_RE.split(html)
    for k in range(0, len(parts), 2):  # even indices are text between tags
        if parts[k]:
            parts[k] = wrap_text(parts[k])
    return "".join(parts)


def _markdown_to_html_fragment(markdown_text: str) -> str:
    import markdown as md

    html = md.markdown(_strip_web_only(markdown_text), extensions=_MD_EXTENSIONS)
    return _wrap_emoji(html)


def render_answer_pdf(markdown_text: str, title: str) -> bytes:
    """PDF bytes for a single answer's markdown."""
    body = _markdown_to_html_fragment(markdown_text)
    return _html_to_pdf(_document(title, [body]))


def render_session_pdf(messages: list[dict], title: str) -> bytes:
    """PDF bytes for a whole session — final answers only, in order. Each
    answer is its own block so a page break never fuses two answers."""
    answers = session_answers(messages)
    blocks = (
        [_markdown_to_html_fragment(answer) for answer in answers]
        if answers
        else ["<p><em>This conversation has no answers to export yet.</em></p>"]
    )
    return _html_to_pdf(_document(title, blocks))


def _document(title: str, answer_html_blocks: list[str]) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    # The title comes from the prompt, so it can carry emoji too — wrap them and
    # drop variation selectors like the body, or the H1 shows tofu boxes.
    safe_title = _wrap_emoji(_escape(_drop_variation_selectors(title or "aish")))
    body_parts = [
        f'<h1>{safe_title}</h1>',
        f'<div class="aish-doc-header">Exported from aish · {stamp}</div>',
    ]
    body_parts += [
        f'<div class="aish-answer">{block}</div>' for block in answer_html_blocks
    ]
    body = "\n".join(body_parts)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_font_face_css()}\n{_PAGE_CSS}</style></head><body>{body}</body></html>"
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _html_to_pdf(html: str) -> bytes:
    from xhtml2pdf import pisa

    buffer = io.BytesIO()
    result = pisa.CreatePDF(
        src=html, dest=buffer, encoding="utf-8", link_callback=_link_callback
    )
    if result.err:
        raise RuntimeError(f"PDF generation failed ({result.err} error(s))")
    return buffer.getvalue()
