"""PDF → a text rendition that reads like a file (#219).

A PDF is not text, it is a page-description program, and the four kinds of
page it can hold each need a different read:

- flowing text — plain extraction is right;
- columns or tables — naive extraction interleaves the columns and shreds the
  table into a word salad that still LOOKS like prose;
- a scan — no text layer at all, so extraction returns nothing usable;
- a figure — the text extracts fine and the meaning is in the picture.

The dangerous ones are the middle two, because they fail *quietly*: the caller
gets something that reads like a complete answer. That is the `youtube_analyze`
failure this repo already has a scar from (`docs/tools-layer.md`) — a hollow
extraction with a confident summary built on top. So the rule here is that
**every page is classified before it is read**, the classification travels with
the text, and a page that cannot be read honestly says so instead of
contributing silence to a summary.

The design is `convert once, then read normally`. Conversion is the expensive,
fallible half; reading is not. So conversion produces a **rendition** — one
markdown file, keyed on the SOURCE's content hash, with an explicit
`[page N of T]` marker before every page — and everything after that is a file
read, a page slice, or a grep. The model does not learn a new way to read a
document; it learns one way to obtain one.

Renditions are content-addressed and the store is a bounded LRU, exactly like
`media.py`: converting the same PDF twice is free, next week's session reuses
this week's rendition, and eviction only costs a re-convert.

Layout handling, in the order it is applied to each page:

1. **Tables are found first** and their regions claimed, so their text is never
   also emitted as loose prose. They render as real markdown tables.
2. **Columns are detected by gutter**, not guessed: text-block x-extents are
   merged and a gap wide enough not to be word spacing is a column boundary.
   Full-width blocks (titles, rules, footers) are excluded from that analysis
   and then used to cut the page into horizontal strips, which is what makes
   "banner headline above two columns" come out in reading order.
3. **Figures** become a placeholder naming their size and page, so the model
   knows something visual is there rather than silently reading around it.
4. **A page with no text layer is declared a scan**, never rendered as an empty
   page — the caller escalates it to an image.

Tables that span pages are ANNOTATED, not silently merged: a continuation is
marked at both ends and its repeated header suppressed. Merging across the page
marker would make page numbers lie, and the page marker is the addressing
scheme the whole design rests on.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Bumped whenever conversion output changes. It is part of the cache key, so an
# improvement to the layout logic invalidates old renditions instead of serving
# a stale one that a test would then pass against.
CONVERTER_VERSION = 2

STORE_MAX_BYTES = 200 * 1024 * 1024
STORE_MAX_FILES = 300

# A page with fewer than this many characters has no usable text layer. Set
# above zero deliberately: a scanned page routinely carries a handful of stray
# characters from a header stamp or an OCR-ed page number, and treating those
# as "text" is exactly how a scan gets summarised as if it had been read.
SCAN_CHAR_FLOOR = 60

# A block at least this wide (fraction of page width) spans the columns rather
# than living in one, so it is a strip separator, not a column member.
SPAN_FRACTION = 0.70
# A horizontal gap at least this wide (fraction of page width) is a column
# gutter rather than word spacing.
GUTTER_FRACTION = 0.045
# An image covering at least this fraction of the page is worth telling the
# model about on its own; below it we are looking at bullets, rules and logos.
FIGURE_MIN_FRACTION = 0.06
# Combined image + vector coverage at which a page is carrying a DIAGRAM, even
# though no single element was big enough to be a figure. An illustrated manual
# is built this way — eight small images and forty vector paths making one
# picture — and reporting nothing for such a page is how a document full of
# diagrams reads as a document with no pictures in it.
DIAGRAM_MIN_FRACTION = 0.10
# Visual coverage at which a text-less page IS a picture. Measured on the
# combined area, not the largest single element: a scan split into strips is
# still a scan.
SCAN_IMAGE_FRACTION = 0.50
# Vector shapes at the extremes carry no meaning: a full-page background fill
# would make every page a diagram, and a hairline is a rule or an underline.
BACKGROUND_FRACTION = 0.90
HAIRLINE_POINTS = 3.0
# Resolution of the coverage grid. Fine enough to distinguish a diagram from a
# scattering of bullets, coarse enough that overlapping shapes are not counted
# twice — which is the whole reason for a grid over summed areas.
COVERAGE_GRID = 40

# How close to a page edge a table must sit to be a candidate for continuing
# onto (or from) the neighbouring page.
EDGE_FRACTION = 0.12
# Table x-extents this close (fraction of page width) count as the same columns.
COLUMN_MATCH_TOLERANCE = 0.03

PAGE_RASTER_DPI = 150
# Below this, a rasterised page is illegible to a vision model; above the byte
# cap we re-render smaller rather than fail.
PAGE_RASTER_MIN_DPI = 72
PAGE_RASTER_MAX_BYTES = 4 * 1024 * 1024

_HEADER_PREFIX = "<!-- aish-document "
_HEADER_SUFFIX = " -->"
_PAGE_MARKER_RE = re.compile(r"^\[page (\d+) of (\d+)\]$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 40


class DocumentError(Exception):
    """Conversion failed for a reason the model should be told in a sentence."""


def page_marker(number: int, total: int) -> str:
    return f"[page {number} of {total}]"


@dataclass(frozen=True)
class PageFacts:
    """What a page IS, decided before its text is read."""

    number: int
    chars: int
    tables: int = 0
    figures: int = 0
    columns: int = 1
    has_diagram: bool = False
    is_scan: bool = False
    is_blank: bool = False
    continues_table: bool = False
    table_continues: bool = False

    @property
    def readable(self) -> bool:
        return not self.is_scan and self.chars >= SCAN_CHAR_FLOOR

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "chars": self.chars,
            "tables": self.tables,
            "figures": self.figures,
            "columns": self.columns,
            "has_diagram": self.has_diagram,
            "is_scan": self.is_scan,
            "is_blank": self.is_blank,
            "continues_table": self.continues_table,
            "table_continues": self.table_continues,
        }


@dataclass(frozen=True)
class Rendition:
    """A converted document: the markdown file plus what each page is."""

    source: str
    path: Path
    pages: tuple[PageFacts, ...]

    @property
    def total_pages(self) -> int:
        return len(self.pages)

    @property
    def scans(self) -> tuple[int, ...]:
        return tuple(p.number for p in self.pages if p.is_scan)

    def text(self) -> str:
        """The rendition body, without the machine-readable header line."""
        raw = self.path.read_text(encoding="utf-8", errors="replace")
        _, _, body = raw.partition("\n")
        return body


# ----------------------------------------------------------------- geometry


@dataclass
class _Item:
    """One positioned thing on a page, in the order it will be emitted."""

    x0: float
    y0: float
    x1: float
    y1: float
    kind: str  # "text" | "table" | "figure"
    text: str
    meta: dict = field(default_factory=dict)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def centre_x(self) -> float:
        return (self.x0 + self.x1) / 2


def _column_bands(items: list[_Item], width: float) -> list[tuple[float, float]]:
    """Left-to-right x-ranges separated by gutters no text block crosses.

    Merging the blocks' x-extents first and only THEN splitting on wide gaps is
    what makes this robust: a single block straddling the gutter (a stray
    footnote rule, an over-wide caption) collapses the two bands into one and we
    fall back to single-column reading — the safe direction, since mis-ordering
    a single-column page is worse than reading a two-column page linearly.
    """
    if len(items) < 2:
        return [(0.0, width)]
    merged: list[list[float]] = []
    for item in sorted(items, key=lambda i: i.x0):
        if merged and item.x0 <= merged[-1][1] + 1.0:
            merged[-1][1] = max(merged[-1][1], item.x1)
        else:
            merged.append([item.x0, item.x1])
    min_gutter = GUTTER_FRACTION * width
    bands: list[list[float]] = [merged[0]]
    for low, high in merged[1:]:
        if low - bands[-1][1] >= min_gutter:
            bands.append([low, high])
        else:
            bands[-1][1] = max(bands[-1][1], high)
    return [(low, high) for low, high in bands]


def _band_of(item: _Item, bands: list[tuple[float, float]]) -> int:
    """Which column an item belongs to — by centre, falling back to nearest, so
    an item overhanging its gutter is still placed rather than dropped."""
    centre = item.centre_x
    for index, (low, high) in enumerate(bands):
        if low <= centre <= high:
            return index
    return min(
        range(len(bands)),
        key=lambda i: min(abs(centre - bands[i][0]), abs(centre - bands[i][1])),
    )


def _side_by_side(items: list[_Item], bands: list[tuple[float, float]]) -> bool:
    """True when two bands genuinely run alongside each other.

    Columns are defined by sharing vertical space, not merely by a horizontal
    gap. Without this check any page with a right-aligned figure, a hanging
    indent or a page number in the corner reads as two columns and comes out
    reordered — a far worse failure than reading a real two-column page
    linearly, because it is invisible in the output.
    """
    spans: dict[int, tuple[float, float]] = {}
    for item in items:
        index = _band_of(item, bands)
        low, high = spans.get(index, (item.y0, item.y1))
        spans[index] = (min(low, item.y0), max(high, item.y1))
    populated = sorted(spans.items())
    for i in range(len(populated)):
        for j in range(i + 1, len(populated)):
            (a_low, a_high), (b_low, b_high) = populated[i][1], populated[j][1]
            overlap = min(a_high, b_high) - max(a_low, b_low)
            shorter = min(a_high - a_low, b_high - b_low)
            if shorter > 0 and overlap / shorter >= 0.25:
                return True
    return False


def _reading_order(items: list[_Item], width: float) -> tuple[list[_Item], int]:
    """(items in reading order, column count).

    Full-width items cut the page into horizontal strips; within a strip the
    remaining items are read column by column. Every item lands in exactly one
    strip — assignment is by top edge through a bisect, so nothing can be
    skipped by an item that overlaps a separator.
    """
    if len(items) < 2:
        return list(items), 1
    span_cut = SPAN_FRACTION * width
    spanning = sorted((i for i in items if i.width >= span_cut), key=lambda i: i.y0)
    body = [i for i in items if i.width < span_cut]
    bands = _column_bands(body, width)
    if len(bands) > 1 and not _side_by_side(body, bands):
        # A wide gap that no band shares vertical space with is an indent, a
        # figure caption or a right-aligned page number — not a column. Reading
        # such a page column-wise would scramble it, so fall back.
        bands = [(0.0, width)]
    if len(bands) < 2:
        return sorted(items, key=lambda i: (round(i.y0, 1), i.x0)), 1

    cuts = [item.y0 for item in spanning]
    strips: dict[int, list[_Item]] = {}
    for item in body:
        strips.setdefault(bisect.bisect_right(cuts, item.y0), []).append(item)

    ordered: list[_Item] = []
    for index in range(len(spanning) + 1):
        strip = strips.get(index, [])
        ordered.extend(
            sorted(strip, key=lambda i: (_band_of(i, bands), round(i.y0, 1), i.x0))
        )
        if index < len(spanning):
            ordered.append(spanning[index])
    return ordered, len(bands)


# ------------------------------------------------------------------- tables


def _cell(value) -> str:
    """One markdown table cell. Newlines inside a cell would end the row, so
    they collapse to spaces; a literal pipe is escaped."""
    return " ".join(str(value if value is not None else "").split()).replace("|", "\\|")


def _markdown_table(rows: list[list], header: tuple[str, ...] | None = None) -> str:
    """Rows as a markdown table. `header` carries the column names in from the
    previous page for a continuation, so the second half of a split table is
    still self-describing without its own header row being invented."""
    grid = [[_cell(c) for c in row] for row in rows]
    grid = [row for row in grid if any(cell for cell in row)]
    if not grid:
        return ""
    width = max(len(row) for row in grid + ([list(header)] if header else []))
    grid = [row + [""] * (width - len(row)) for row in grid]
    if header is not None:
        head = [_cell(c) for c in header] + [""] * (width - len(header))
        body = grid
    else:
        head, body = grid[0], grid[1:]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * width) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


@dataclass(frozen=True)
class _TableEdge:
    """Enough of a table's shape to recognise its other half on the next page."""

    columns: int
    x0: float
    x1: float
    header: tuple[str, ...]

    def matches(self, other: _TableEdge) -> bool:
        return (
            self.columns == other.columns
            and abs(self.x0 - other.x0) <= COLUMN_MATCH_TOLERANCE
            and abs(self.x1 - other.x1) <= COLUMN_MATCH_TOLERANCE
        )


def _table_rows(table) -> list[list]:
    try:
        return [list(row) for row in table.extract()]
    except Exception:  # a malformed table must not fail the whole document
        return []


# --------------------------------------------------------------- conversion


def _visual_coverage(page, rect) -> float:
    """Fraction of the page covered by pictures — raster images and meaningful
    vector shapes together.

    Measured on a grid rather than by summing areas, because a diagram is
    normally a stack of overlapping shapes: summed areas would report 300%
    coverage for a picture occupying a fifth of the page. The grid counts each
    region once, which is what makes a single threshold meaningful.
    """
    page_area = max(rect.width * rect.height, 1.0)
    boxes: list[tuple[float, float, float, float]] = []
    try:
        boxes.extend(tuple(info["bbox"]) for info in page.get_image_info())
    except Exception:
        pass
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    for drawing in drawings:
        box = drawing.get("rect")
        if box is None:
            continue
        width, height = box.x1 - box.x0, box.y1 - box.y0
        if width < HAIRLINE_POINTS or height < HAIRLINE_POINTS:
            continue  # a rule, an underline, a table border
        if (width * height) / page_area >= BACKGROUND_FRACTION:
            continue  # a background fill is not a picture
        boxes.append((box.x0, box.y0, box.x1, box.y1))
    if not boxes:
        return 0.0

    cells = set()
    step_x = max(rect.width / COVERAGE_GRID, 1e-6)
    step_y = max(rect.height / COVERAGE_GRID, 1e-6)
    for x0, y0, x1, y1 in boxes:
        col0 = max(0, int((x0 - rect.x0) / step_x))
        col1 = min(COVERAGE_GRID - 1, int((x1 - rect.x0) / step_x))
        row0 = max(0, int((y0 - rect.y0) / step_y))
        row1 = min(COVERAGE_GRID - 1, int((y1 - rect.y0) / step_y))
        for col in range(col0, col1 + 1):
            for row in range(row0, row1 + 1):
                cells.add((col, row))
    return len(cells) / (COVERAGE_GRID * COVERAGE_GRID)


def _page_items(page, rect) -> tuple[list[_Item], list, list]:
    """(items, table objects, figure infos) for one page.

    Tables are claimed first: any text block whose centre falls inside a table's
    box is dropped from the prose flow, because the table already renders it.
    """
    import pymupdf

    # find_tables prints a one-off "install pymupdf_layout" recommendation to
    # STDOUT. In a CLI that lands in the middle of the answer; in the web server
    # it lands in the log. Neither is ours to emit.
    pymupdf.no_recommend_layout()

    try:
        found = list(page.find_tables().tables)
    except Exception:
        found = []
    table_boxes = [pymupdf.Rect(t.bbox) for t in found]

    items: list[_Item] = []
    for block in page.get_text("blocks"):
        x0, y0, x1, y1, text, _no, btype = block[:7]
        if btype != 0 or not text.strip():
            continue
        centre = ((x0 + x1) / 2, (y0 + y1) / 2)
        if any(box.contains(centre) for box in table_boxes):
            continue
        items.append(_Item(x0, y0, x1, y1, "text", text.strip()))

    for index, table in enumerate(found):
        x0, y0, x1, y1 = table.bbox
        items.append(_Item(x0, y0, x1, y1, "table", "", {"index": index}))

    page_area = max(rect.width * rect.height, 1.0)
    figures = []
    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    for info in infos:
        x0, y0, x1, y1 = info["bbox"]
        area = max((x1 - x0) * (y1 - y0), 0.0)
        if area / page_area < FIGURE_MIN_FRACTION:
            continue
        figures.append((info, area / page_area))
        items.append(
            _Item(
                x0, y0, x1, y1, "figure", "",
                {"w": int(x1 - x0), "h": int(y1 - y0), "coverage": area / page_area},
            )
        )
    return items, found, figures


def _convert_pages(doc) -> tuple[list[str], list[PageFacts]]:
    total = doc.page_count
    chunks: list[str] = []
    facts: list[PageFacts] = []
    pending_edge: _TableEdge | None = None
    # Filled in on the NEXT page, so it is a list we mutate rather than a value.
    continues_flags: list[bool] = [False] * total

    for number in range(1, total + 1):
        page = doc[number - 1]
        rect = page.rect
        width = rect.width or 1.0
        height = rect.height or 1.0
        items, tables, figures = _page_items(page, rect)
        ordered, columns = _reading_order(items, width)

        text_chars = sum(len(i.text) for i in ordered if i.kind == "text")
        table_chars = sum(
            len(str(cell or "")) for t in tables for row in _table_rows(t) for cell in row
        )
        chars = text_chars + table_chars

        visual = _visual_coverage(page, rect)
        is_scan = chars < SCAN_CHAR_FLOOR and visual >= SCAN_IMAGE_FRACTION
        # A page whose pictures are many small pieces rather than one big one:
        # no single element qualified as a figure, but together they are a
        # diagram. Common enough to be the norm in an illustrated manual.
        has_diagram = (
            not is_scan and not figures and visual >= DIAGRAM_MIN_FRACTION
        )
        # Blank means NOTHING on the page, not "little on the page". Deciding it
        # by character count discards the content of a title page, a short table
        # or a one-line cover note — silently, which is the failure this module
        # exists to prevent. A sparse page is still emitted in full below; the
        # character count only ever decides whether a page is a SCAN.
        is_blank = not ordered and not is_scan and not has_diagram

        lines: list[str] = [page_marker(number, total)]
        next_edge: _TableEdge | None = None
        continues_here = False

        if is_blank:
            lines.append("*(blank page)*")
        else:
            if is_scan:
                # The note leads, and whatever stray characters the page does
                # carry (a header stamp, an OCR-ed page number) still follow it.
                # They are real, they are just not the page.
                lines.append(
                    "*(scanned page — no text layer, so it cannot be read as text; "
                    "ask to see it as an image)*"
                )
            for item in ordered:
                if item.kind == "text":
                    lines.append(item.text)
                elif item.kind == "figure":
                    if is_scan:
                        continue  # the scan note above already IS this image
                    lines.append(
                        f"*(figure on page {number}, {item.meta['w']}×{item.meta['h']} pt — "
                        "not readable as text; ask to see this page as an image)*"
                    )
                else:
                    table = tables[item.meta["index"]]
                    rows = _table_rows(table)
                    if not rows:
                        continue
                    header = tuple(_cell(c) for c in rows[0])
                    edge = _TableEdge(
                        columns=len(rows[0]),
                        x0=item.x0 / width,
                        x1=item.x1 / width,
                        header=header,
                    )
                    near_top = item.y0 <= rect.y0 + EDGE_FRACTION * height
                    is_continuation = bool(
                        pending_edge
                        and near_top
                        and not lines[1:]  # nothing preceded it on this page
                        and pending_edge.matches(edge)
                    )
                    if is_continuation:
                        continues_here = True
                        assert pending_edge is not None  # implied by is_continuation
                        repeated = pending_edge.header == header
                        lines.append(f"*(table continued from page {number - 1})*")
                        lines.append(
                            _markdown_table(
                                rows[1:] if repeated else rows, header=pending_edge.header
                            )
                        )
                    else:
                        lines.append(_markdown_table(rows))
                    pending_edge = None
                    if item.y1 >= rect.y1 - EDGE_FRACTION * height:
                        next_edge = edge
            if has_diagram:
                lines.append(
                    f"*(diagram on page {number}, covering about "
                    f"{round(visual * 100)}% of it — not readable as text; ask to see "
                    "this page as an image)*"
                )

        if continues_here:
            continues_flags[number - 1] = True
        pending_edge = next_edge
        chunks.append("\n\n".join(line for line in lines if line))
        facts.append(
            PageFacts(
                number=number,
                chars=chars,
                tables=len(tables),
                figures=len(figures),
                columns=columns,
                has_diagram=has_diagram,
                is_scan=is_scan,
                is_blank=is_blank,
            )
        )

    # A "continues" flag is only knowable one page late, and the page BEFORE it
    # needs the matching "continues on the next page" note, so both are stamped
    # here rather than guessed during the walk.
    resolved: list[PageFacts] = []
    for index, page_facts in enumerate(facts):
        continues_table = continues_flags[index]
        table_continues = index + 1 < total and continues_flags[index + 1]
        resolved.append(
            PageFacts(
                **{
                    **page_facts.as_dict(),
                    "continues_table": continues_table,
                    "table_continues": table_continues,
                }
            )
        )
        if table_continues:
            chunks[index] += f"\n\n*(table continues on page {index + 2})*"
    return chunks, resolved


# -------------------------------------------------------------- the store


def slug(hint: str) -> str:
    return _SLUG_RE.sub("-", hint.lower()).strip("-")[:_SLUG_MAX].strip("-")


def _cache_path(store_dir: Path, digest: str, name: str) -> Path:
    """Where a rendition of these bytes goes. The digest is the identity and
    the name is only there to make a directory listing legible — so lookup
    globs on the digest, or the same document downloaded twice under two names
    would be converted twice."""
    stem = slug(Path(name).stem)
    return Path(store_dir) / (f"{digest}-{stem}.md" if stem else f"{digest}.md")


def _existing_path(store_dir: Path, digest: str) -> Path | None:
    try:
        return next(iter(sorted(Path(store_dir).glob(f"{digest}*.md"))), None)
    except OSError:
        return None


def _read_cached(path: Path, source: str) -> Rendition | None:
    try:
        head = path.open("r", encoding="utf-8", errors="replace").readline()
    except OSError:
        return None
    if not head.startswith(_HEADER_PREFIX):
        return None
    try:
        meta = json.loads(head[len(_HEADER_PREFIX) :].rstrip().removesuffix(_HEADER_SUFFIX))
    except json.JSONDecodeError:
        return None
    if meta.get("converter") != CONVERTER_VERSION:
        return None
    path.touch()  # refresh recency; this is an LRU store
    pages = tuple(PageFacts(**page) for page in meta.get("pages", []))
    return Rendition(source=source, path=path, pages=pages)


def prune(store_dir: Path) -> list[Path]:
    """Evict least-recently-used renditions until under both caps. Best-effort:
    a file that vanishes under us is skipped rather than raising into a call."""
    try:
        files = [p for p in Path(store_dir).iterdir() if p.is_file()]
    except OSError:
        return []
    entries = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
    entries.sort()
    total = sum(size for _, size, _ in entries)
    removed: list[Path] = []
    for _, size, path in entries:
        if len(entries) - len(removed) <= STORE_MAX_FILES and total <= STORE_MAX_BYTES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed.append(path)
    return removed


def convert(pdf_path: Path | str, store_dir: Path | str) -> Rendition:
    """Convert (or reuse a conversion of) `pdf_path` into a markdown rendition.

    Keyed on the SOURCE bytes, so the same document is converted once ever —
    across sessions, and regardless of where the file happens to sit or what it
    is called this time.
    """
    source = Path(pdf_path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise DocumentError(f"could not read {source}: {exc}") from exc
    if not data.startswith(b"%PDF"):
        raise DocumentError(f"{source.name} is not a PDF (no %PDF header)")

    store = Path(store_dir)
    store.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    existing = _existing_path(store, digest)
    if existing is not None:
        cached = _read_cached(existing, source.name)
        if cached is not None:
            return cached
    path = existing or _cache_path(store, digest, source.name)

    try:
        import pymupdf

    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DocumentError(f"PDF support is unavailable: {exc}") from exc

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise DocumentError(f"{source.name} could not be opened as a PDF: {exc}") from exc
    with doc:
        if doc.needs_pass:
            raise DocumentError(f"{source.name} is password-protected, so it cannot be read")
        if doc.page_count == 0:
            raise DocumentError(f"{source.name} has no pages")
        chunks, pages = _convert_pages(doc)

    meta = {
        "converter": CONVERTER_VERSION,
        "source": source.name,
        "digest": digest,
        "pages": [page.as_dict() for page in pages],
    }
    header = _HEADER_PREFIX + json.dumps(meta, separators=(",", ":")) + _HEADER_SUFFIX
    path.write_text(header + "\n\n" + "\n\n".join(chunks) + "\n", encoding="utf-8")
    prune(store)
    return Rendition(source=source.name, path=path, pages=tuple(pages))


def _open(pdf_path: Path | str):
    """The document, or a DocumentError naming the file. Every caller that
    touches the PDF itself comes through here, so "PyMuPDF is missing" and
    "this file is not openable" read the same wherever they surface."""
    try:
        import pymupdf

    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DocumentError(f"PDF support is unavailable: {exc}") from exc
    try:
        return pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise DocumentError(f"could not open {Path(pdf_path).name}: {exc}") from exc


def page_count(pdf_path: Path | str) -> int:
    """How many pages, without converting anything. The preview addresses pages
    by number, so it needs the count before it can offer them — and a count is
    the one question about a PDF that costs nothing to answer."""
    with _open(pdf_path) as doc:
        return int(doc.page_count)


def page_png(pdf_path: Path | str, number: int, dpi: int = PAGE_RASTER_DPI) -> bytes:
    """One page rasterised as PNG — how a scan or a figure-bearing page reaches
    a vision model, and how the web preview shows a document at all. Steps the
    resolution down rather than failing when a dense page would exceed the
    image cap."""
    with _open(pdf_path) as doc:
        if not 1 <= number <= doc.page_count:
            raise DocumentError(f"page {number} is outside {Path(pdf_path).name}")
        page = doc[number - 1]
        while True:
            data = page.get_pixmap(dpi=dpi).tobytes("png")
            if len(data) <= PAGE_RASTER_MAX_BYTES or dpi <= PAGE_RASTER_MIN_DPI:
                return data
            dpi = max(PAGE_RASTER_MIN_DPI, dpi // 2)


# ----------------------------------------------------------------- reading


def parse_pages(spec: str, total: int) -> list[int]:
    """'1-3,7' -> [1,2,3,7], clamped to the document and de-duplicated.
    Raises DocumentError on anything unparseable, rather than silently reading
    a different part of the document than was asked for."""
    wanted: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part.lstrip("-"):
            low, _, high = part.partition("-")
            try:
                start, end = int(low), int(high)
            except ValueError as exc:
                raise DocumentError(f"could not read page range {part!r}") from exc
            if start > end:
                start, end = end, start
            wanted.extend(range(start, end + 1))
        else:
            try:
                wanted.append(int(part))
            except ValueError as exc:
                raise DocumentError(f"could not read page number {part!r}") from exc
    seen: list[int] = []
    for number in wanted:
        if 1 <= number <= total and number not in seen:
            seen.append(number)
    if not seen:
        raise DocumentError(f"no pages in range — this document has {total}")
    return seen


def _page_chunks(rendition: Rendition) -> dict[int, str]:
    """Page number -> that page's rendered text, marker included. Parsed from
    the file rather than tracked alongside it: one authority for where a page
    starts, and it survives the file being read, copied or evicted."""
    chunks: dict[int, str] = {}
    current: int | None = None
    buffer: list[str] = []
    for line in rendition.text().splitlines():
        match = _PAGE_MARKER_RE.match(line.strip())
        if match:
            if current is not None:
                chunks[current] = "\n".join(buffer).strip()
            current = int(match.group(1))
            buffer = [line]
        elif current is not None:
            buffer.append(line)
    if current is not None:
        chunks[current] = "\n".join(buffer).strip()
    return chunks


def pages_text(rendition: Rendition, numbers: list[int]) -> str:
    chunks = _page_chunks(rendition)
    return "\n\n".join(chunks[n] for n in numbers if n in chunks)


def search(rendition: Rendition, query: str, limit: int = 40) -> list[tuple[int, str]]:
    """(page number, matching line) for a case-insensitive substring match.
    Substring, not regex: the query comes from a model and a bad pattern should
    find nothing, never raise."""
    needle = query.lower()
    hits: list[tuple[int, str]] = []
    page = 0
    for line in rendition.text().splitlines():
        match = _PAGE_MARKER_RE.match(line.strip())
        if match:
            page = int(match.group(1))
            continue
        if needle in line.lower():
            hits.append((page, line.strip()))
            if len(hits) >= limit:
                break
    return hits


# ----------------------------------------------------------------- summary


def _ranges(numbers: list[int]) -> str:
    """[1,2,3,7] -> '1-3, 7'."""
    if not numbers:
        return ""
    spans: list[tuple[int, int]] = []
    for number in sorted(numbers):
        if spans and number == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], number)
        else:
            spans.append((number, number))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


def summary(rendition: Rendition) -> str:
    """The structural map: what this document IS, before any of it is read.

    Always emitted, and emitted FIRST, because it is the only thing standing
    between a partly-unreadable document and a confident summary of it.
    """
    pages = rendition.pages
    total = len(pages)
    chars = sum(p.chars for p in pages)
    lines = [f"{rendition.source} — {total} page{'s' if total != 1 else ''}, "
             f"{chars:,} characters of text."]

    scans = [p.number for p in pages if p.is_scan]
    blanks = [p.number for p in pages if p.is_blank]
    tables = [p.number for p in pages if p.tables]
    figures = [p.number for p in pages if p.figures and not p.is_scan]
    diagrams = [p.number for p in pages if p.has_diagram]
    columned = [p.number for p in pages if p.columns > 1]
    spanning = [p.number for p in pages if p.table_continues]

    if tables:
        count = sum(p.tables for p in pages)
        lines.append(f"  tables: {count} on page(s) {_ranges(tables)}")
    for number in spanning:
        lines.append(f"  a table spans pages {number}-{number + 1}")
    if columned:
        lines.append(f"  multi-column: page(s) {_ranges(columned)}")
    if figures:
        count = sum(p.figures for p in pages if not p.is_scan)
        lines.append(f"  figures: {count} on page(s) {_ranges(figures)}")
    if diagrams:
        lines.append(
            f"  diagrams (many small pieces, not readable as text): "
            f"page(s) {_ranges(diagrams)}"
        )
    if blanks:
        lines.append(f"  blank: page(s) {_ranges(blanks)}")
    if scans:
        lines.append(
            f"  SCANNED, no text layer: page(s) {_ranges(scans)} — these are NOT in the "
            "text below and cannot be read as text. Use pages= to see them as images."
        )
    return "\n".join(lines)
