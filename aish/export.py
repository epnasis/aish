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
from datetime import datetime

# The heavy imports (reportlab via xhtml2pdf) are deferred into the render
# function so importing this module — and starting the server — stays cheap.

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

# Kept simple and readable; xhtml2pdf supports only a CSS subset, so this
# leans on what it renders reliably (fonts, colors, table borders, spacing).
_PAGE_CSS = """
@page { size: a4; margin: 2cm 1.8cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 11pt;
       line-height: 1.45; color: #1c1c1e; }
h1 { font-size: 20pt; margin: 0 0 10pt; }
h2 { font-size: 15pt; margin: 16pt 0 6pt; }
h3 { font-size: 12.5pt; margin: 14pt 0 5pt; }
p { margin: 0 0 8pt; }
a { color: #0a63c9; text-decoration: none; }
ul, ol { margin: 0 0 8pt; }
li { margin: 0 0 3pt; }
code { font-family: Courier, monospace; font-size: 9.5pt;
       background: #f2f2f7; color: #1c1c1e; }
pre { font-family: Courier, monospace; font-size: 9pt; background: #f2f2f7;
      color: #1c1c1e; padding: 8pt; margin: 0 0 8pt; }
blockquote { color: #6c6c70; margin: 0 0 8pt; padding: 0 0 0 10pt;
             border-left: 2pt solid #d1d1d6; }
table { border-collapse: collapse; margin: 0 0 8pt; }
th, td { border: 0.5pt solid #d1d1d6; padding: 4pt 7pt; font-size: 10pt; }
th { background: #f2f2f7; }
hr { border: none; border-top: 0.5pt solid #d1d1d6; margin: 14pt 0; }
.aish-doc-header { color: #8e8e93; font-size: 9pt; margin: 0 0 14pt;
                   border-bottom: 0.5pt solid #e5e5ea; padding-bottom: 8pt; }
.aish-answer + .aish-answer { border-top: 0.5pt solid #e5e5ea;
                             margin-top: 16pt; padding-top: 16pt; }
"""

_MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]


def safe_pdf_filename(title: str, fallback: str = "aish-export") -> str:
    """A download-safe `<slug>.pdf` name derived from a title."""
    slug = _UNSAFE_FILENAME.sub("-", (title or "").strip()).strip("-._")
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


def _markdown_to_html_fragment(markdown_text: str) -> str:
    import markdown as md

    return md.markdown(markdown_text or "", extensions=_MD_EXTENSIONS)


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
    safe_title = _escape(title or "aish")
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
        f"<style>{_PAGE_CSS}</style></head><body>{body}</body></html>"
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _html_to_pdf(html: str) -> bytes:
    from xhtml2pdf import pisa

    buffer = io.BytesIO()
    result = pisa.CreatePDF(src=html, dest=buffer, encoding="utf-8")
    if result.err:
        raise RuntimeError(f"PDF generation failed ({result.err} error(s))")
    return buffer.getvalue()
