"""Local Markdown → PDF export for the web UI (issue #64).

Conversion runs locally: `markdown` renders to HTML and `xhtml2pdf` (pure
Python, reportlab-backed, no system libraries) renders HTML to PDF bytes. The
markdown itself is never sent to any external service.

Two shapes are exported: one answer (the markdown the user is looking at) and
a whole session's FINAL answers only. "Final answer" is a structural property
of the persisted log — an assistant message is final iff it is NOT immediately
followed by a tool result. Tool-calling turns are always followed by a `tool`
message (the loop only returns after a text-only turn), so this cleanly drops
thinking/tool/working steps and keeps just the answers the user saw.

Media embedding (issue #133): images, map snapshots, and video thumbnails are
inlined into the PDF as base64 data URIs. Local `![](path)` images are read
only when the (symlink-resolved) path stays inside the caller-supplied
`image_roots` — the same trust boundary as the approval model — anything
outside renders as a captioned link card, never read. Remote `![](https://…)`
images, Google static-map snapshots for the whitelisted map links, and YouTube
thumbnails ARE fetched over the network at export time (owner-approved
egress), each with a short timeout, a size cap, and graceful fallback to a
captioned link card on failure/offline. Static maps need `GOOGLE_MAPS_API_KEY`
in the environment; without it map links fall back to the link card.
"""

import base64
import html as html_lib
import io
import os
import re
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Sequence
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
    path so reportlab can embed them. Images arrive pre-inlined as data URIs
    (passed through untouched); by the time pisa runs, nothing else needs
    resolving and nothing hits the network from inside the renderer."""
    if uri.startswith("data:"):
        return uri
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
/* -pdf-word-wrap: CJK breaks long unbreakable strings at any character so
   wide table cells wrap to the page instead of running off the right edge
   (xhtml2pdf maps it to reportlab's wordWrap='CJK'). Code blocks deliberately
   do NOT use it — see .codeblock and _reflow_code_blocks. */
.codeblock { font-family: "aishMono"; font-size: 9pt; background: #f2f2f7;
             color: #1c1c1e; padding: 8pt; margin: 0 0 8pt; }
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
.aish-link-card { border: 0.5pt solid #d1d1d6; background: #f9f9fb;
                  padding: 6pt 9pt; margin: 4pt 0 8pt; }
.aish-link-note { color: #8e8e93; font-size: 8.5pt; }
.aish-embed { margin: 4pt 0 8pt; }
.aish-embed-caption { color: #6c6c70; font-size: 9pt; }
"""

_MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

# Widest run kept on one code line before a hard break is inserted; ~the printable
# A4 width at the 9pt mono size, so a long token wraps instead of overflowing the
# margin (it replaces the CJK word-wrap that _reflow_code_blocks must avoid).
_CODE_MAX_COLS = 90

# A fenced code block: <pre> wrapping an optional <code>, however markdown emits it.
_PRE_BLOCK_RE = re.compile(
    r"<pre\b[^>]*>(?:\s*<code\b[^>]*>)?(.*?)(?:</code>\s*)?</pre>", re.S
)


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


# ---- media embedding (issue #133) -----------------------------------------
# Every fetch is bounded (timeout + byte cap + per-document budget) and every
# failure degrades to a captioned link card — an export can be slow, never hung.
FETCH_TIMEOUT = 5.0
FETCH_MAX_BYTES = 8 * 1024 * 1024
MAX_REMOTE_FETCHES = 12  # per exported document, so N broken links can't stack N timeouts

# Printable A4 width at our margins is ~493pt; images are scaled down to this
# (xhtml2pdf needs explicit width/height — it does no max-width clamping).
_IMG_MAX_WIDTH = 460

# Ports of the app.js embed whitelist (embedForLink): only strictly-matched
# YouTube ids / Google Maps queries ever become a fetch URL.
_YOUTUBE_RE = re.compile(
    r"^https?://(?:www\.)?(?:youtube\.com/watch\?(?:[^#]*&)?v=([a-zA-Z0-9_-]{11})"
    r"|youtu\.be/([a-zA-Z0-9_-]{11}))(?:[#&?/]|$)"
)
_MAPS_RE = re.compile(
    r"^https?://(?:maps\.google\.com/maps|(?:www\.)?google\.[a-z.]+/maps)"
    r"(?:/[^?#\s]*)?\?([^#\s]+)"
)

# The markdown lib emits attributes in source order with double quotes, so
# these stay simple; labels may carry nested inline markup (kept verbatim).
_A_TAG_RE = re.compile(r'<a href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*/?>")
_ATTR_RES = {
    "src": re.compile(r'\bsrc="([^"]*)"'),
    "alt": re.compile(r'\balt="([^"]*)"'),
}


def fetch_image(url: str) -> bytes | None:
    """GET a remote image with a hard timeout and size cap. Any failure —
    network error, non-2xx, oversize, offline — returns None and the caller
    falls back to a captioned link card. http/https only.

    Module-level (and looked up via the module at call time) so tests
    monkeypatch it; nothing else in this module opens a socket."""
    if not url.startswith(("http://", "https://")):
        return None
    request = urllib.request.Request(url, headers={"User-Agent": "aish-export"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:  # noqa: S310 — scheme-checked above
            data = response.read(FETCH_MAX_BYTES + 1)
    except Exception:
        return None
    if not data or len(data) > FETCH_MAX_BYTES:
        return None
    return data


def _image_info(data: bytes) -> tuple[str, int, int] | None:
    """(mime, width, height) when the bytes are an image the PDF renderer
    handles, else None. Pillow is a hard dependency of xhtml2pdf (it is what
    pisa itself decodes images with), so validating here mirrors exactly what
    would succeed downstream — bad bytes become a link card instead of a
    renderer error."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").lower()
            if fmt not in ("png", "jpeg", "gif", "webp"):
                return None
            return f"image/{fmt}", image.width, image.height
    except Exception:
        return None


def _escape_attr(text: str) -> str:
    return _escape(text).replace('"', "&quot;")


def _img_tag(data: bytes, alt: str) -> str | None:
    """A data-URI <img> scaled to fit the page, or None for non-image bytes."""
    info = _image_info(data)
    if info is None:
        return None
    mime, width, height = info
    if width > _IMG_MAX_WIDTH:
        height = max(1, round(height * _IMG_MAX_WIDTH / width))
        width = _IMG_MAX_WIDTH
    payload = base64.b64encode(data).decode("ascii")
    return (
        f'<img src="data:{mime};base64,{payload}" width="{width}" '
        f'height="{height}" alt="{_escape_attr(alt)}"/>'
    )


def _link_card(label_html: str, url: str, note: str) -> str:
    """The graceful-degradation shape: a bordered card with the link and a
    small note saying what could not be embedded (and why it is a link)."""
    href = _escape_attr(url)
    label = label_html.strip() or _escape(url)
    return (
        f'<div class="aish-link-card"><a href="{href}">{label}</a><br/>'
        f'<span class="aish-link-note">{_escape(note)} — {_escape(url)}</span></div>'
    )


def _embed_card(image_html: str, label_html: str, url: str, note: str) -> str:
    href = _escape_attr(url)
    label = label_html.strip() or _escape(url)
    return (
        f'<div class="aish-embed"><a href="{href}">{image_html}</a><br/>'
        f'<span class="aish-embed-caption"><a href="{href}">{label}</a>'
        f" ({_escape(note)})</span></div>"
    )


def _map_markers(map_query: str) -> list[tuple[str, str]] | None:
    """The Static-Maps marker params for a whitelisted maps link's query
    string, or None when the link carries no renderable query (only view
    params like @lat,lng). Mirrors the web UI's embed parsing: only the
    strictly parsed q/query or saddr+daddr values are used, re-encoded."""
    params = urllib.parse.parse_qs(map_query)

    def first(name: str) -> str:
        values = params.get(name) or [""]
        return values[0].strip()

    saddr, daddr = first("saddr"), first("daddr")
    if saddr and daddr:
        return [("markers", f"label:A|{saddr}"), ("markers", f"label:B|{daddr}")]
    query = first("q") or first("query")
    if query:
        return [("markers", query)]
    return None


def _static_map_url(markers: list[tuple[str, str]], key: str) -> str:
    pairs = [("size", "640x400"), ("scale", "2"), *markers, ("key", key)]
    return "https://maps.googleapis.com/maps/api/staticmap?" + urllib.parse.urlencode(
        pairs, doseq=True
    )


class _MediaEmbedder:
    """Rewrites one exported document's HTML: local images inlined when inside
    the trusted roots, remote images / map snapshots / YouTube thumbnails
    fetched and inlined, everything else (or any failure) a captioned link
    card. One instance per document so the fetch budget spans all blocks."""

    def __init__(self, image_roots: Sequence[os.PathLike | str]) -> None:
        self.roots = [Path(root).resolve() for root in image_roots]
        self.fetches_left = MAX_REMOTE_FETCHES

    def _fetch(self, url: str) -> bytes | None:
        if self.fetches_left <= 0:
            return None
        self.fetches_left -= 1
        return fetch_image(url)  # module-global lookup, so tests monkeypatch export.fetch_image

    def process(self, html: str) -> str:
        # Links first, then images: cards emitted by the link pass contain
        # data-URI <img> tags, which the image pass passes through untouched;
        # the reverse order would rescan card-internal <a> tags.
        html = _A_TAG_RE.sub(self._replace_link, html)
        return _IMG_TAG_RE.sub(self._replace_img, html)

    def _replace_link(self, match: re.Match[str]) -> str:
        url = html_lib.unescape(match.group(1))
        label_html = match.group(2)
        youtube = _YOUTUBE_RE.match(url)
        if youtube:
            return self._youtube_card(youtube.group(1) or youtube.group(2), label_html, url)
        maps = _MAPS_RE.match(url)
        if maps:
            return self._map_card(maps.group(1), label_html, url)
        return match.group(0)

    def _youtube_card(self, video_id: str, label_html: str, url: str) -> str:
        thumb = self._fetch(f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg")
        image = _img_tag(thumb, "video thumbnail") if thumb else None
        if image is None:
            return _link_card(label_html, url, "YouTube video")
        return _embed_card(image, label_html, url, "YouTube video")

    def _map_card(self, map_query: str, label_html: str, url: str) -> str:
        markers = _map_markers(map_query)
        if markers is None:
            # View-only link (no q/query/directions): plain <a>, like the web UI.
            return f'<a href="{_escape_attr(url)}">{label_html}</a>'
        key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if not key:
            return _link_card(label_html, url, "map")
        snapshot = self._fetch(_static_map_url(markers, key))
        image = _img_tag(snapshot, "map") if snapshot else None
        if image is None:
            return _link_card(label_html, url, "map")
        return _embed_card(image, label_html, url, "map")

    def _replace_img(self, match: re.Match[str]) -> str:
        tag = match.group(0)
        src_match = _ATTR_RES["src"].search(tag)
        if not src_match:
            return tag
        src = html_lib.unescape(src_match.group(1))
        alt_match = _ATTR_RES["alt"].search(tag)
        alt = html_lib.unescape(alt_match.group(1)) if alt_match else ""
        if src.startswith("data:"):
            return tag  # already inline (e.g. a card this pass's sibling emitted)
        if src.startswith(("http://", "https://")):
            data = self._fetch(src)
            image = _img_tag(data, alt) if data else None
            return image or _link_card(_escape(alt), src, "image")
        return self._local_image(src, alt)

    def _local_image(self, src: str, alt: str) -> str:
        """Inline a local file ONLY when its symlink-resolved path stays inside
        the trusted roots — the same boundary the approval model and the /file
        endpoint enforce. Everything else (relative paths, ~, .., symlink
        escapes, missing/oversized/non-image files) becomes a link card and is
        never read."""
        card = _link_card(_escape(alt), src, "image not embedded")
        if not src.startswith("/"):
            return card  # relative / ~ / other schemes: no trusted anchor to resolve against
        try:
            path = Path(src).resolve(strict=True)  # resolves .. and symlinks
            if not any(path.is_relative_to(root) for root in self.roots):
                return card
            if not path.is_file() or path.stat().st_size > FETCH_MAX_BYTES:
                return card
            data = path.read_bytes()
        except OSError:
            return card
        return _img_tag(data, alt) or card


def _reflow_code_blocks(html: str) -> str:
    """Render fenced code blocks as a <div> of <br>-separated lines instead of a
    <pre>. xhtml2pdf turns a <pre> (like any -pdf-word-wrap:CJK block) into a
    single UNSPLITTABLE flowable, so a code block taller than the page can't be
    paginated: reportlab fails to place it and aborts the whole PDF (issue #147).
    Hard <br> breaks let the block split across pages; indentation is preserved
    with &nbsp; and an over-long line is wrapped in place of the CJK word-wrap we
    can no longer use. The code text is unescaped before wrapping and re-escaped
    per chunk, so a break never lands inside an HTML entity."""

    def replace(match: re.Match[str]) -> str:
        code = html_lib.unescape(match.group(1)).strip("\n")
        rows = []
        for line in code.split("\n"):
            body = line.lstrip(" ")
            indent = "&nbsp;" * (len(line) - len(body))
            chunks = [
                body[i : i + _CODE_MAX_COLS] for i in range(0, len(body), _CODE_MAX_COLS)
            ] or [""]
            rows.append(indent + "<br/>".join(_escape(chunk) for chunk in chunks))
        return '<div class="codeblock">' + "<br/>".join(rows) + "</div>"

    return _PRE_BLOCK_RE.sub(replace, html)


def _markdown_to_html_fragment(
    markdown_text: str, embedder: _MediaEmbedder | None = None
) -> str:
    import markdown as md

    html = md.markdown(_strip_web_only(markdown_text), extensions=_MD_EXTENSIONS)
    html = _reflow_code_blocks(html)  # splittable code blocks (#147)
    if embedder is not None:
        html = embedder.process(html)
    return _wrap_emoji(html)


def render_answer_pdf(
    markdown_text: str, title: str, image_roots: Sequence[os.PathLike | str] = ()
) -> bytes:
    """PDF bytes for a single answer's markdown. `image_roots` are the trusted
    directories local images may be inlined from (see _MediaEmbedder)."""
    body = _markdown_to_html_fragment(markdown_text, _MediaEmbedder(image_roots))
    return _html_to_pdf(_document(title, [body]))


def render_session_pdf(
    messages: list[dict], title: str, image_roots: Sequence[os.PathLike | str] = ()
) -> bytes:
    """PDF bytes for a whole session — final answers only, in order. Each
    answer is its own block so a page break never fuses two answers."""
    answers = session_answers(messages)
    embedder = _MediaEmbedder(image_roots)  # one budget across the whole document
    blocks = (
        [_markdown_to_html_fragment(answer, embedder) for answer in answers]
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
