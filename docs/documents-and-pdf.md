# Documents — reading a PDF (`read_pdf`, `documents.py`)

`documents.py`, `Agent._read_pdf`, and the workspace boundary it depends on.

---

## Why it exists

Attachments arrived (paste, drag-drop, the iOS share sheet) and the most common attachment turned out to be the one aish could not read. A PDF only reached the model **natively**, and only on Claude and OpenAI, only from the uploads dir, only under the size cap. Everywhere else — the default local backend, a file already on disk, a link — it fell through to an inert `[attached file: …]` path note, and the model improvised.

The improvisation is on record. In one session (`session-20260811-160205`) reading a single user guide cost **four approval prompts**: `pdftotext` into the scratch dir, then the shell builtin `read` twice (which returns nothing useful), then a `grep`. Three of those four were the workspace-boundary bug below; the fourth was the missing capability.

A capability gap that makes the model reach for a shell recipe is the signal to build the capability — the same reasoning that produced `show_image` (`docs/media-and-images.md`).

---

## A PDF is not text

It is a page-description program, and there are four kinds of page. Each needs a different read, and **two of them fail quietly**:

| Page | Naive extraction gives you |
|---|---|
| flowing text | the right answer |
| columns / tables | interleaved columns and shredded rows — **word salad that still reads like prose** |
| a scan | nothing, or a dozen stray ligatures — **silence that reads as "nothing there"** |
| a figure | the surrounding text, with the meaning missing |

The middle two are the danger, because the caller cannot tell. This is `youtube_analyze`'s failure in a new costume (`docs/tools-layer.md`): a hollow extraction with a confident summary built on top.

So the governing rule is **every page is classified before it is read**, the classification travels with the text, and a page that cannot be read honestly says so rather than contributing silence. `TestPageKinds`.

---

## Convert once, then read normally

Conversion is the expensive, fallible half; reading is not. So `convert()` produces a **rendition** — one markdown file with an explicit `[page N of T]` marker before every page — and everything after that is a file read, a page slice, or a grep.

That is the whole design. The model does not learn a new way to read a document; it learns one way to **obtain** one. `pages=` and `search=` exist on the tool so a targeted read is a single call, but both serve the same cached rendition — they are not a second mechanism. `TestReading`, `TestConversion`.

Renditions are content-addressed and the store is a bounded LRU, exactly like the media store. Keyed on the **source bytes only** — the filename appears in the rendition's name for a legible directory listing but is not part of the identity, or the same document downloaded twice under two names would convert twice. `CONVERTER_VERSION` is part of the key, so improving the layout logic invalidates old renditions instead of serving a stale one a test would then pass against. `TestStore`.

**The page marker is the addressing scheme the design rests on.** Nothing may make a page number lie — which is why a table spanning pages is annotated rather than merged (below).

---

## Layout, in the order it is applied

1. **Tables are claimed first.** Their regions are found, and any text block whose centre falls inside one is dropped from the prose flow — otherwise every cell's words appear twice, once in the table and once as loose text. They render as real markdown tables, with pipes escaped and in-cell newlines collapsed (a newline would end the row). `TestTables`, `TestMarkdownTable`.
2. **Columns are detected by gutter, not guessed.** Text-block x-extents are merged and a gap wide enough not to be word spacing is a boundary. Full-width blocks are excluded from that analysis and then used to cut the page into horizontal strips, which is what makes "banner headline above two columns" come out in reading order.
3. **Figures** become a placeholder naming their size and page, so the model knows something visual is there rather than silently reading around it. A page whose picture is many small pieces — a dozen little images, or pure vector paths with no image at all — is caught by **combined visual coverage** rather than per-element size, measured on a grid so overlapping shapes are not double-counted. Without this an illustrated manual reports having no pictures in it: the real 25-page user guide that motivated the feature has 14 such pages and every one of them registered as nothing. Full-page background fills and hairlines are excluded, or every styled document would be a diagram. `TestDiagrams`.
4. **A page with no text layer is declared a scan** and never rendered as an empty page.

**Two guards on the column logic, both erring the same way.** A block straddling the gutter collapses the bands and the page reads linearly; and two bands only count as columns when they **share vertical space** (`_side_by_side`). Without the second, a right-aligned page number or a hanging figure reads as a column and silently reorders the page. Reading a real two-column page linearly is visible and recoverable; reordering a single-column page is neither.

**Blank means nothing on the page, not little on it.** Deciding it by character count discards the content of a title page or a short table — silently, which is the failure this module exists to prevent. The character count only ever decides whether a page is a SCAN, and a sparse page is emitted in full.

### Tables across a page break

Annotated at **both** ends (`*(table continues on page N)*` / `*(table continued from page N)*`), with the repeated header suppressed as data but carried over as the continuation's header, so the second half stays self-describing. A continuation is claimed only when the previous table ended at the page's bottom edge, this one starts at the top edge and is the first thing on the page, and the column count and x-extents match within tolerance — otherwise two unrelated tables would be joined into rows that were never adjacent. `TestTables`.

They are deliberately **not merged into one table**: merging across the page marker would make page numbers lie.

---

## The escalation: a scan is a picture

A page that cannot be read as text is not silence, it is an image — and aish already has a content-addressed store and three renderers for images. So `Agent._read_pdf` rasterises the requested unreadable pages into the media store, returns the markdown lines for display, and **delivers the pages themselves** to the model. No OCR dependency: a modern vision model reads a rasterised page better than Tesseract does, for zero extra install.

**The delivery half was missing until #215, and this doc asserted it anyway.** "On a vision-capable backend the model simply sees them" was the design; what shipped rasterised the page, stored it, described it as readable, and handed the model a **file path**, because a tool result is text on every provider. The escalation therefore turned a page that read as silence into a page that read as a filename — arguably worse, since the first is visibly empty and the second looks like an answer. The pages now ride the result envelope and are delivered as native image parts; the mechanism, its cap and its one gap are in `docs/media-and-images.md`. A doc that describes an intended pipeline in the present tense is how a hole stays invisible for five months.

Capped at `PDF_MAX_PAGE_IMAGES`, and **the cap is always stated** — a 200-page scan must not silently become a partial answer. On a text-only local backend the escalation cannot complete, and the result says which pages could not be read. An honest dead end beats a fluent hallucination.

---

## The tool

`read_pdf` is native, not a droppable plugin: it needs the media store, the document store and the backend-sized output budget, which a subprocess cannot reach. It is in `READ_ONLY_TOOLS` (its only write is into aish's own store — the same argument as `show_image`, and content-addressed writes make it thread-safe on the parallel read path) and in `EGRESS_TOOLS` (its URL form is an outbound GET at a model-chosen host; its local-path form reaches none and is never gated). `TestReadPdf`.

Every result **leads with the structural map** — page count, which pages carry tables, figures, columns, scans. That header is the only thing standing between a partly-unreadable document and a confident summary of it, so it is emitted first and always, including when the caller asked for one page.

A fetched PDF is saved into the document store rather than a temp file, so a re-read costs no second download and no approval. Non-PDF bytes under a `.pdf` URL are refused by their **magic bytes**, naming what actually came back — the same failure `show_image` guards against, and usually a login wall.

`read_url` on a PDF no longer dead-ends with *"not a text page"*; it names `read_pdf` with the URL filled in. A refusal that names no alternative is where the model starts improvising with curl.

---

## The owner reads it too (#218)

`page_png` was built so the MODEL could see a page it could not read. The web
preview takes the identical route for the person: `/pdf/info` + `/pdf/page`
rasterise on demand and the pages land in the photo viewer the frontend already
has (`docs/web-server.md`, `[PREVIEW]` in `docs/web-frontend.md`). No second
mechanism, no PDF renderer on the client, and the one gesture set for both a
photograph and a page.

These pages are **not** stored in the media store. That store is what the model
was shown; a page somebody swiped past is not that, and content-addressing
every page of every document scrolled through would evict real media to hold it.

## The workspace boundary (#220)

`Agent.workspace_roots()` — everywhere aish may **read** without asking: the session roots plus the directories the process owns (media store, scratch, document store, transcripts, browser downloads). The tool-output cache is deliberately NOT among them (#317): it is read through `read_tool_output`, which carries the entry's provenance, and the file layer refuses it.

This was the display-only boundary, consumed by nothing that reads. The read side was missing, and the asymmetry was absurd once seen: the scratch dir was auto-approved for **writing** and **deleting** but not for **reading**, so the model could create a file there unprompted, delete it unprompted, and then need a tap to grep the thing it had just written. Three of the four approvals in the motivating session were this.

**It grants no new power.** Scratch holds only what the model put there; the media, output and document stores hold aish's own content. Reading back what you were already allowed to write is strictly less than what was already granted. Sensitivity is checked **first** and is never widened — a credential file inside a directory aish owns still prompts. Writes and mutations are unaffected: they gate on `roots`, not on this. `TestImageRoots`, and the boundary's approval half in `tests/test_approval.py`.

The rule the rename encodes: **every read-side consumer takes this one list**, so a fourth asymmetry cannot open quietly.

**A rendition of a FETCHED document is outside content, and says so beside its bytes (#319).** "It grants no new power" holds for a rendition of a PDF the owner had on disk; it does not hold for one `_resolve_pdf` downloaded, and the store holds both — the fetched `.pdf` and the `.md` rendered from it — inside the boundary. Read through `read_file` those arrived bannerless, untainted and unattributed, which is #317's hole through a door that must stay OPEN: naming the rendition so the model can grep it, page it and search it is the whole convert-once-then-read design. So `convert` takes a `provenance.ArtefactSource` and writes it to a `<rendition>.src` sidecar (on a cache hit too), `_resolve_pdf` records the PDF it saves, and `Agent._pdf_origin` decides local-vs-fetched with `_brings_outside_content`'s existing `DUAL_SOURCE_TOOLS` rule rather than a second one — ORing in the source file's own record, so re-reading a fetched PDF by its local path cannot relabel the rendition. `prune` skips records when counting and unlinks them with the artefact. Rationale in `docs/agent-core.md`; `TestStore`, `TestARenditionCarriesWhereItCameFrom`.

---

## Dependency

**PyMuPDF**, and only PyMuPDF. It is the one library covering layout-aware text, table detection and page rasterization in a single binary wheel with no system libraries, and its AGPL licence matches aish's own.

Rejected: `pypdf` (no tables, cannot render, so the scan case stays broken); `pdfplumber` (good tables, cannot render); shelling out to poppler (makes a Homebrew install a silent prerequisite — and the whole point is to stop shelling out); `pymupdf4llm` (does all of this well, but drags in `onnxruntime`, `numpy` and `networkx` — a ~100 MB ML runtime for a PDF reader).

`find_tables` prints a one-off "install pymupdf_layout" recommendation to **stdout**, which in the CLI lands in the middle of an answer. `no_recommend_layout()` is called before use.
