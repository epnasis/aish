"""The PDF rendition layer (#219).

Fixtures are BUILT here rather than committed as binaries: the four page kinds
this module exists to tell apart (flowing text, columns, tables, scans) each
need a PDF whose internals are known, and a checked-in sample is a black box
that says nothing about why a test failed. Building them also keeps the suite
honest about what a "scan" is — it is a rasterised page, produced the same way
a real scanner produces one.
"""

import pytest

from aish import documents

pymupdf = pytest.importorskip("pymupdf")


# ------------------------------------------------------------------ fixtures


def _text_page(page, text, rect=(50, 50, 545, 700), size=11):
    page.insert_textbox(pymupdf.Rect(*rect), text, fontsize=size)


def _grid(page, rows, x0=50, y0=100, cell=(90, 22)):
    """A ruled table — find_tables keys off the ruling lines."""
    width, height = cell
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            box = pymupdf.Rect(x0 + c * width, y0 + r * height,
                               x0 + (c + 1) * width, y0 + (r + 1) * height)
            page.draw_rect(box, color=(0, 0, 0), width=0.6)
            page.insert_textbox(box + (3, 5, -3, -3), str(value), fontsize=8)


def _scan_bytes():
    """A page of text, rasterised — i.e. a page with no text layer."""
    doc = pymupdf.open()
    page = doc.new_page()
    _text_page(page, "SCANNED CONTRACT TEXT. " * 80, size=12)
    data = page.get_pixmap(dpi=110).tobytes("png")
    doc.close()
    return data


def _save(doc, tmp_path, name):
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def store(tmp_path):
    return tmp_path / "documents"


class TestConversion:
    def test_plain_text_round_trips_with_page_markers(self, tmp_path, store):
        doc = pymupdf.open()
        for n in (1, 2, 3):
            _text_page(doc.new_page(), f"This is page {n} of the report. " * 10)
        path = _save(doc, tmp_path, "report.pdf")

        rendition = documents.convert(path, store)

        assert rendition.total_pages == 3
        body = rendition.text()
        for n in (1, 2, 3):
            assert documents.page_marker(n, 3) in body
            assert f"This is page {n} of the report." in body
        # Markers appear in order — page addressing depends on it.
        assert body.index("[page 1 of 3]") < body.index("[page 2 of 3]") < body.index(
            "[page 3 of 3]"
        )

    def test_two_columns_are_read_down_not_across(self, tmp_path, store):
        """The failure this exists to catch: a naive extractor interleaves the
        columns line by line, producing prose that reads as complete and says
        something neither column said."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 40, 545, 80), "BANNER HEADLINE", fontsize=16)
        page.insert_textbox(pymupdf.Rect(50, 100, 280, 600), "ALPHA line. " * 30, fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 100, 545, 600), "BRAVO line. " * 30, fontsize=10)
        path = _save(doc, tmp_path, "twocol.pdf")

        body = documents.convert(path, store).text()

        assert body.index("BANNER HEADLINE") < body.index("ALPHA")
        # Every ALPHA precedes every BRAVO: the columns did not interleave.
        assert body.rindex("ALPHA") < body.index("BRAVO")

    def test_single_column_with_a_wide_gap_is_not_split(self, tmp_path, store):
        """A right-aligned page number or a hanging figure leaves a wide
        horizontal gap. Treating it as a gutter reorders the page invisibly,
        which is worse than reading a real two-column page linearly."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 40, 300, 90), "FIRST paragraph here.", fontsize=11)
        page.insert_textbox(pymupdf.Rect(450, 700, 545, 730), "42", fontsize=9)
        page.insert_textbox(pymupdf.Rect(50, 120, 300, 200), "SECOND paragraph here.",
                            fontsize=11)
        path = _save(doc, tmp_path, "gap.pdf")

        rendition = documents.convert(path, store)
        body = rendition.text()

        assert body.index("FIRST") < body.index("SECOND")
        assert rendition.pages[0].columns == 1


class TestTables:
    def test_table_becomes_markdown(self, tmp_path, store):
        doc = pymupdf.open()
        page = doc.new_page()
        _text_page(page, "Prices below.", rect=(50, 50, 545, 80))
        _grid(page, [["Item", "Qty"], ["Bolt", "4"], ["Nut", "9"]])
        path = _save(doc, tmp_path, "table.pdf")

        rendition = documents.convert(path, store)
        body = rendition.text()

        assert "| Item | Qty |" in body
        assert "| Bolt | 4 |" in body
        assert rendition.pages[0].tables == 1

    def test_table_text_is_not_also_emitted_as_prose(self, tmp_path, store):
        """A cell's words must appear once, in the table. Emitting them again
        as loose text is how a table turns into a word salad that still reads
        like a sentence."""
        doc = pymupdf.open()
        page = doc.new_page()
        _grid(page, [["Widget", "Qty"], ["Sprocket", "7"]])
        path = _save(doc, tmp_path, "once.pdf")

        body = documents.convert(path, store).text()

        assert body.count("Sprocket") == 1

    def test_table_spanning_two_pages_is_annotated_at_both_ends(self, tmp_path, store):
        rows = [["Item", "Qty", "Price"]] + [[f"W{i}", i, f"{i}.00"] for i in range(1, 7)]
        doc = pymupdf.open()
        first = doc.new_page()
        _text_page(first, "Invoice detail follows.", rect=(50, 50, 545, 90))
        _grid(first, rows, y0=620)  # ends at the bottom edge
        second = doc.new_page()
        _grid(second, rows, y0=15)  # starts at the top edge, header repeated
        path = _save(doc, tmp_path, "spanning.pdf")

        rendition = documents.convert(path, store)
        body = rendition.text()

        assert "*(table continues on page 2)*" in body
        assert "*(table continued from page 1)*" in body
        assert rendition.pages[0].table_continues
        assert rendition.pages[1].continues_table
        # The repeated header is suppressed as DATA but carried over as the
        # continuation's header, so the second half is still self-describing.
        assert body.count("| Item | Qty | Price |") == 2
        second_half = body[body.index("[page 2 of 2]"):]
        assert "| W1 | 1 | 1.00 |" in second_half

    def test_two_unrelated_tables_are_not_joined(self, tmp_path, store):
        """A table at the top of page 2 with different columns is its own
        table. Claiming continuity would merge unrelated rows."""
        doc = pymupdf.open()
        first = doc.new_page()
        _grid(first, [["A", "B"], ["1", "2"]], y0=620)
        second = doc.new_page()
        _grid(second, [["X", "Y", "Z"], ["7", "8", "9"]], y0=15)
        path = _save(doc, tmp_path, "unrelated.pdf")

        rendition = documents.convert(path, store)

        assert not rendition.pages[1].continues_table
        assert "*(table continued from page 1)*" not in rendition.text()


class TestPageKinds:
    @pytest.fixture
    def mixed(self, tmp_path, store):
        scan = _scan_bytes()
        doc = pymupdf.open()
        _text_page(doc.new_page(), "Ordinary readable text. " * 20, rect=(50, 50, 545, 300))
        doc.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), stream=scan)
        third = doc.new_page()
        _text_page(third, "Text above a chart. " * 10, rect=(50, 40, 545, 150))
        third.insert_image(pymupdf.Rect(80, 200, 500, 520), stream=scan)
        doc.new_page()  # blank
        path = _save(doc, tmp_path, "mixed.pdf")
        return path, documents.convert(path, store)

    def test_each_page_kind_is_classified(self, mixed):
        _path, rendition = mixed
        text, scan, figure, blank = rendition.pages
        assert text.readable and not text.is_scan
        assert scan.is_scan and not scan.readable
        assert figure.figures == 1 and figure.readable
        assert blank.is_blank and not blank.is_scan

    def test_a_scan_says_so_instead_of_contributing_silence(self, mixed):
        """The whole point. An empty page 2 would read as "nothing there"; the
        summary and the body both have to say the words are unreachable."""
        _path, rendition = mixed
        assert "scanned page" in documents.pages_text(rendition, [2])
        summary = documents.summary(rendition)
        assert "SCANNED, no text layer: page(s) 2" in summary
        assert rendition.scans == (2,)

    def test_a_figure_is_announced_not_skipped(self, mixed):
        _path, rendition = mixed
        assert "figure on page 3" in documents.pages_text(rendition, [3])
        assert "figures: 1 on page(s) 3" in documents.summary(rendition)

    def test_summary_leads_with_the_shape_of_the_document(self, mixed):
        _path, rendition = mixed
        assert documents.summary(rendition).startswith("mixed.pdf — 4 pages,")

    def test_page_can_be_rasterised(self, mixed):
        path, _rendition = mixed
        data = documents.page_png(path, 2)
        assert data.startswith(b"\x89PNG")

    def test_rasterising_a_page_that_does_not_exist_is_an_error(self, mixed):
        path, _rendition = mixed
        with pytest.raises(documents.DocumentError):
            documents.page_png(path, 99)


class TestDiagrams:
    """An illustrated manual builds each picture from many small pieces — a
    dozen little images, or pure vector paths with no image at all. Sized one by
    one they are all below the figure threshold, so a document full of diagrams
    reported having no pictures in it. Coverage is measured on the combined
    area instead, on a grid so overlapping shapes are not counted twice."""

    def _page_of_pieces(self, doc, count=12, vector=False):
        page = doc.new_page()
        _text_page(page, "Follow the diagram below. " * 4, rect=(50, 40, 545, 90))
        tile = None
        if not vector:
            src = pymupdf.open()
            src.new_page().draw_rect(pymupdf.Rect(0, 0, 60, 60), fill=(0.2, 0.4, 0.9))
            tile = src[0].get_pixmap(dpi=40).tobytes("png")
            src.close()
        for i in range(count):
            box = pymupdf.Rect(60 + (i % 4) * 120, 150 + (i // 4) * 130,
                               160 + (i % 4) * 120, 260 + (i // 4) * 130)
            if vector:
                page.draw_rect(box, color=(0, 0, 0), fill=(0.8, 0.8, 0.9), width=1)
            else:
                page.insert_image(box, stream=tile)
        return page

    def test_many_small_images_are_reported_as_a_diagram(self, tmp_path, store):
        doc = pymupdf.open()
        self._page_of_pieces(doc)
        rendition = documents.convert(_save(doc, tmp_path, "manual.pdf"), store)

        assert rendition.pages[0].has_diagram
        assert "diagram on page 1" in rendition.text()
        assert "diagrams (many small pieces" in documents.summary(rendition)

    def test_a_pure_vector_diagram_counts_too(self, tmp_path, store):
        """No raster image at all — the failure this catches is a page whose
        entire illustration is drawing operators and so is invisible to image
        detection."""
        doc = pymupdf.open()
        self._page_of_pieces(doc, vector=True)
        rendition = documents.convert(_save(doc, tmp_path, "vector.pdf"), store)

        assert rendition.pages[0].has_diagram

    def test_ordinary_text_is_not_a_diagram(self, tmp_path, store):
        doc = pymupdf.open()
        _text_page(doc.new_page(), "Just prose, nothing drawn. " * 40)
        rendition = documents.convert(_save(doc, tmp_path, "prose.pdf"), store)

        assert not rendition.pages[0].has_diagram
        assert "diagram" not in documents.summary(rendition)

    def test_rules_and_a_background_fill_are_not_a_diagram(self, tmp_path, store):
        """A table border, an underline and a page-wide background are page
        furniture. Counting them would make every styled document a diagram."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.draw_rect(page.rect, fill=(0.97, 0.97, 0.97))  # background
        _text_page(page, "Styled document body text. " * 30)
        for y in (300, 320, 340, 360, 380):
            page.draw_line(pymupdf.Point(50, y), pymupdf.Point(545, y), width=0.7)
        rendition = documents.convert(_save(doc, tmp_path, "styled.pdf"), store)

        assert not rendition.pages[0].has_diagram

    def test_a_picture_only_page_is_not_called_blank(self, tmp_path, store):
        """It has no text, but it is not empty — saying "blank page" about a
        full-page illustration is the same silence a scan used to produce."""
        doc = pymupdf.open()
        page = doc.new_page()
        for i in range(9):
            page.draw_rect(
                pymupdf.Rect(60 + (i % 3) * 170, 100 + (i // 3) * 220,
                             210 + (i % 3) * 170, 300 + (i // 3) * 220),
                fill=(0.3, 0.5, 0.7),
            )
        rendition = documents.convert(_save(doc, tmp_path, "pictures.pdf"), store)

        assert not rendition.pages[0].is_blank
        assert rendition.pages[0].has_diagram or rendition.pages[0].is_scan


class TestReading:
    @pytest.fixture
    def rendition(self, tmp_path, store):
        doc = pymupdf.open()
        for n in (1, 2, 3, 4):
            _text_page(doc.new_page(), f"Section {n}. The keyword is orange on page {n}. " * 4)
        return documents.convert(_save(doc, tmp_path, "book.pdf"), store)

    def test_pages_slice_returns_only_those_pages(self, rendition):
        text = documents.pages_text(rendition, [2, 4])
        assert "Section 2." in text and "Section 4." in text
        assert "Section 1." not in text and "Section 3." not in text

    def test_search_reports_the_page_each_hit_is_on(self, rendition):
        hits = documents.search(rendition, "orange on page 3")
        assert hits and all(page == 3 for page, _line in hits)

    def test_search_is_case_insensitive_and_never_raises(self, rendition):
        assert documents.search(rendition, "SECTION 1.")
        assert documents.search(rendition, "([unclosed") == []

    @pytest.mark.parametrize(
        "spec,expected",
        [("2", [2]), ("1-3", [1, 2, 3]), ("3,1", [3, 1]), ("2-1", [1, 2]), ("3-99", [3, 4])],
    )
    def test_page_specs(self, rendition, spec, expected):
        assert documents.parse_pages(spec, rendition.total_pages) == expected

    def test_an_unreadable_page_spec_is_an_error_not_a_guess(self, rendition):
        """Silently reading a different part of the document than was asked for
        is the failure mode; an error is recoverable, a wrong page is not."""
        with pytest.raises(documents.DocumentError):
            documents.parse_pages("chapter two", rendition.total_pages)
        with pytest.raises(documents.DocumentError):
            documents.parse_pages("99", rendition.total_pages)


class TestStore:
    def test_conversion_is_cached_by_content(self, tmp_path, store):
        doc = pymupdf.open()
        _text_page(doc.new_page(), "Cache me. " * 20)
        path = _save(doc, tmp_path, "cached.pdf")

        first = documents.convert(path, store)
        stamp = first.path.stat().st_mtime_ns
        copy = tmp_path / "renamed.pdf"
        copy.write_bytes(path.read_bytes())
        second = documents.convert(copy, store)

        assert second.path == first.path  # same bytes, same rendition
        assert second.path.stat().st_mtime_ns >= stamp
        assert len(list(store.glob("*.md"))) == 1

    def test_a_converter_change_invalidates_the_cache(self, tmp_path, store, monkeypatch):
        doc = pymupdf.open()
        _text_page(doc.new_page(), "Version me. " * 20)
        path = _save(doc, tmp_path, "versioned.pdf")
        documents.convert(path, store)

        monkeypatch.setattr(documents, "CONVERTER_VERSION", documents.CONVERTER_VERSION + 1)
        again = documents.convert(path, store)

        assert again.pages  # reconverted rather than served stale
        assert "aish-document" in again.path.read_text().splitlines()[0]

    def test_store_prunes_to_the_file_cap(self, tmp_path, store, monkeypatch):
        monkeypatch.setattr(documents, "STORE_MAX_FILES", 2)
        for n in range(4):
            doc = pymupdf.open()
            _text_page(doc.new_page(), f"Document number {n}. " * 20)
            documents.convert(_save(doc, tmp_path, f"doc{n}.pdf"), store)
        assert len(list(store.iterdir())) <= 2


class TestRefusals:
    def test_a_non_pdf_is_refused_by_its_bytes_not_its_name(self, tmp_path, store):
        fake = tmp_path / "notreally.pdf"
        fake.write_bytes(b"<html>login required</html>")
        with pytest.raises(documents.DocumentError, match="not a PDF"):
            documents.convert(fake, store)

    def test_a_missing_file_is_an_error(self, tmp_path, store):
        with pytest.raises(documents.DocumentError, match="could not read"):
            documents.convert(tmp_path / "nope.pdf", store)

    def test_an_encrypted_pdf_says_so(self, tmp_path, store):
        doc = pymupdf.open()
        _text_page(doc.new_page(), "secret")
        path = tmp_path / "locked.pdf"
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="hunter2")
        doc.close()
        with pytest.raises(documents.DocumentError, match="password-protected"):
            documents.convert(path, store)


class TestMarkdownTable:
    def test_pipes_and_newlines_in_a_cell_cannot_break_the_row(self):
        table = documents._markdown_table([["h1", "h2"], ["a|b", "line\nbreak"]])
        rows = table.splitlines()
        assert len(rows) == 3  # header, rule, one data row
        assert r"a\|b" in rows[2]
        assert "line break" in rows[2]

    def test_a_continuation_carries_the_header_in(self):
        table = documents._markdown_table([["1", "2"]], header=("Item", "Qty"))
        assert table.splitlines()[0] == "| Item | Qty |"
        assert table.splitlines()[2] == "| 1 | 2 |"
