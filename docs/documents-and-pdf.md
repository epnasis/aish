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

A page that cannot be read as text is not silence, it is an image — and aish already has a content-addressed store and three renderers for images. So `Agent._read_pdf` rasterises the requested unreadable pages into the media store, returns the markdown lines for display, and **delivers the pages themselves** to the model. This was once the ONLY way into a scan, on the argument that a vision model reads a rasterised page better than Tesseract does. That argument says nothing about a model that cannot look, such as the owner's `local:` model. Scans are now also read by OCR (next section).

**The delivery half was missing until #215, and this doc asserted it anyway.** "On a vision-capable backend the model simply sees them" was the design; what shipped rasterised the page, stored it, described it as readable, and handed the model a **file path**, because a tool result is text on every provider. The escalation therefore turned a page that read as silence into a page that read as a filename — arguably worse, since the first is visibly empty and the second looks like an answer. The pages now ride the result envelope and are delivered as native image parts; the mechanism, its cap and its one gap are in `docs/media-and-images.md`. A doc that describes an intended pipeline in the present tense is how a hole stays invisible for five months.

Capped at `PDF_MAX_PAGE_IMAGES`, and **the cap is always stated** — a 200-page scan must not silently become a partial answer.

**The picture escalation is offered only to a model that can take it.** It used to be offered to every model, and on 2026-10-17 (`session-20261017-184727-510127`) that cost the owner ten minutes. The backend was the text-only local model, and the document was a 12-page scanned insurance policy. The map said *"Use pages= to see them as images"*. The model asked for pages 1-3. They were rasterised and *"attached to this turn"*, and then the delivery note said this model takes no picture input. The model then called `show_image` on a page, whose result again said *"it is attached to this turn, so look at it"*, beside a note saying the opposite. Two sentences in one turn contradicted each other, and the model followed the one that let it keep going. It ran `which tesseract`, then `brew install` tesseract, ImageMagick and Ghostscript, and raised ten approval cards. The task was still unfinished when the owner stopped watching.

So every offer, promise or delivery of a picture now asks one authority, `Agent._takes_pictures()`, which is `backends.media_support(provider)`, the same test `_deliver_tool_media` always used. On a model that takes no pictures:
- the map does not offer `pages=` as a way to look;
- `_pdf_page_images` rasterises nothing;
- `show_image` says the model cannot see the picture (`SHOW_IMAGE_UNSEEN`);
- `read_media` says the frames are not attached (`FRAMES_UNSEEN`).

`TestReadPdfScansByModel`.

**The figure and diagram notes lost their "ask to see this page as an image" too.** That hint was false on every backend, because `pages=` only ever rasterised SCAN pages. The rendition is shared by every backend, so a backend-dependent offer must not live in it.

---

## A scan's words: OCR by the Mac itself

Text is cheaper in context than a picture, and it can be searched and grepped. A text-only model also has no other way in. So a page classified as a scan is read by **OCR**, with macOS's own recognizer: Vision's `VNRecognizeTextRequest` at the accurate level, with language correction on. This happens in `documents.recognise_scans` and `ocr.py`.

**It runs for every scan, on every backend.** That includes vision models, which also keep the picture escalation. Two reasons. The rendition is backend-independent and content-addressed, so it cannot hold one answer for some models and another for the rest. And OCR keeps only the words: ticks, signatures and layout are lost, and a ticked box is exactly what an insurance form turns on. So a model that can look still gets the picture.

**Labelled, never passed off as the document's own.** A recognised page reads like this:

```
[page 3 of 12]

*(scanned page — no usable text layer. Between the two OCR lines below is aish's OCR of the page picture: machine-recognised words, which may contain recognition errors; ticks, signatures and layout are not reproduced.)*

--- OCR text, page 3 ---
…
--- end of OCR text, page 3 ---
```

The map then says which scan pages fall into each of these groups:
- words read by aish's OCR;
- OCR found no words ("blank, or a picture without words; aish has not established which" — the recognizer cannot tell them apart, so neither does the sentence);
- OCR FAILED, with the reason;
- NOT READ, with the reason.

Search hits on a recognised page are marked `(OCR page)`. The mark is per page, not per line, so a stray text-layer line on that page carries it too. Every search result names the scan pages it could not search, whether it found hits or not. Before, only a search with no hits said so, and a list of hits over part of a document read as a search of all of it. `TestScanOcr`.

**A page cannot forge the structure around it.** The page marker is the addressing scheme, and the OCR delimiters say which words a machine recognised. A scan can print either. `defuse` wraps any line of page content that would parse as one in `‹ ›`. It matches only the exact, case-sensitive forms this module writes, so a footer reading `[Page 3 of 10]` is left alone. It is applied to the text layer as well, which had the same hole: a page printing `[page 5 of 12]` moved every page after it. It splits with `str.splitlines`, the same split every reader uses. The first version split on `\n` alone, and the delivery review showed a U+2028 or a form feed carrying a marker past it. The rendition then had more sections than pages, and a successful recognition was thrown away while the map said it had never run. If a result still cannot be written, the map now says that recognition ran and could not be stored. OCR text is otherwise outside content exactly like a text layer. It lives in the same `.md`, so `Rendition.text()` disarms `[aish:` markers at read time and the `.src` sidecar's outside-content banner applies unchanged.

**A dead end that cannot be acted on invites improvising, so the dead end says what not to do.** A page can stay unread for three reasons: recognition is unavailable here, the per-call cap was hit, or the page failed for good. On every backend, the map then says, imperatively, that it MUST NOT try another way ("no tesseract, no brew install, no convert or any other shell OCR"), and that it must tell the user which pages could not be read and why. The `read_pdf` description says the same once, in one line.

### The cache, and who may write it

A fresh conversion writes every scan page as `SCAN_NOT_READ`. Recognition fills pages in afterwards. Each page's state is held in the rendition's own header (`PageFacts.ocr`, `ocr_chars`, `ocr_attempts`, `ocr_error`), and the header names what produced the text (`ocr_engine`: macOS version, request revision). That makes a result from an older recognizer visible, even though nothing re-runs it.

- **`text` and `none` are final.** A re-read costs nothing.
- **`failed` is retried** on the next read, because a deadline or a crashed child is often transient. After `OCR_MAX_ATTEMPTS` (2) attempts it becomes final, and the page says so. Without that limit, a page that reliably crashed Vision would cost its full deadline on every read, forever.
- **Why a page is NOT READ is never stored.** It is this call's answer (`Rendition.unread_reason`): recognition is unavailable here, the cap was hit, or Stop was pressed. Another process, for example the test suite with `AISH_OCR=0`, answers differently, and a cached reason would be someone else's.

**One read-recognise-write at a time per rendition.** `read_pdf` runs on the parallel read path. Two calls on one scan, such as `pages=1-3` beside `pages=20-25`, each rewrote the whole file from their own copy, and the second discarded the first's pages. This was found in design review. `test_two_parallel_reads_both_keep_their_pages` forces the interleaving and fails without the lock, which was checked by removing it. The lock is per rendition, not per process: the first version used one process-wide lock, and the delivery review measured an unrelated document's read waiting 2.8 s behind a 3 s recognition (`test_a_scan_being_recognised_does_not_hold_up_another_document`, checked to fail under a single lock). A rendition with nothing left to recognise takes no lock at all. Across processes there is no lock. Instead the write re-reads the file just before it publishes, with header and sections taken from that one read, and does not downgrade a page another writer finished (`test_a_stale_writer_never_downgrades_a_finished_page`). This is a best-effort merge, not a guarantee: a writer that publishes between that read and this publish is overwritten. The file is written through `atomic_write.publish`, so a reader never sees half of one.

### What it costs, and the bounds

All figures below were measured on the owner's Mac (M-series, macOS 26.6), on the 12-page Polish scan that motivated this:

| | measured |
|---|---|
| rasterise one page (pymupdf) | 86 ms at 150 dpi, 97 ms at 200, 241 ms at 300 |
| recognise one page | 0.5–1.0 s; ~0.1 s for a blank page |
| the whole 12-page scan, end to end through the child | 10.9 s on the first run after install, 5.1 s after |
| characters recognised, 150 vs 200 vs 300 dpi | within ±5% of each other, so `OCR_DPI` = 200 |
| peak memory of one recognition | ~150 MB resident |

Polish diacritics came out identical with Vision's default language list (en-US only), with pl-PL/en-US, and with en-US/pl-PL. Automatic language detection lost two on one page. So no language list is set. A list built from the user's locale was in the first design and was cut in review: nothing measured stood behind it.

**At most `OCR_PAGES_PER_CALL` (12) pages per call.** Pages the call asked for (`pages=`, `section=`) go first, so the cap can never leave the very page asked for unread. The rest wait for the next call, and the map says so and names the next range. The child's deadline is `STARTUP_SECONDS` (20, which covers the first import after install, measured at 13.8 s once) plus `SECONDS_PER_PAGE` (3) per page: 56 s at the cap in the worst case, against about 10 s typical. Stop ends the child: the running child is polled against the agent's cancel event every 0.25 s.

### Why a child process, and why pyobjc

`ocr.recognise` writes the page pictures to a temp directory and runs `python -P -m aish.ocr`, which prints one JSON line per page as it finishes. A deadline, a crash or a Stop therefore keeps every page already read.

- **Not in-process.** aish-web is one long-lived process on a memory-tight machine, and one recognition peaks around 150 MB. In a child that memory goes back on exit, and a crash inside Vision ends the child, not the server. Children run one at a time per process (`_ONE_AT_A_TIME`), so two scans read in parallel cannot double the peak. The import costs ~0.15 s per call, once for all pages.
- **pyobjc-framework-Vision**, declared with `sys_platform == 'darwin'`. It is the binding to the framework, and it follows the rule this doc's dependency section already set: wheels only (about 14 MB with pyobjc-core and the Cocoa, Quartz and CoreML bindings it pulls in), no toolchain, no Homebrew. Its output was byte-identical to a compiled Swift helper on the same page (78 of 78 lines), at the same request revision (3).

Rejected:
- **A Swift helper.** Compiled once with `swiftc -O`, it takes 3.6 s and runs with about 10 ms of overhead. Run as a script with `swift`, it adds 0.23 s per call warm and 1.6 s cold. Both need Xcode's command-line tools at run time, which makes them a silent toolchain prerequisite: the argument that rejected poppler. A precompiled binary committed to a public repo is unreviewable.
- **Tesseract.** It is a Homebrew install, and its output measured worse: on the same scan it read `@` as `Q` in every e-mail address.
- **pyobjc in-process.** Rejected for the resident memory and the crash blast radius described above.

**Off a Mac, or without the binding, it degrades and says so.** `ocr.unavailable_reason()` covers three cases: this machine is not a Mac, the package is missing, or `AISH_OCR=0` (the kill switch). The reason appears in the map's NOT READ line. The test suite sets `AISH_OCR=0` for every test (`no_real_ocr` in `tests/conftest.py`), so macOS and Linux CI take the same path. `TestTheProtocol` drives the parent half with a FAKE worker on every platform: streaming, a per-page error, a crash, the deadline, Stop, garbage on stdout. `TestAvailability` pins each reason. `TestMacOSVision` runs the real recognizer on a generated page, and skips wherever the framework is absent.

### Not built, and known

- **Ollama is assumed to take pictures, model by model.** Because of this, the map's MUST NOT line is shown on every backend, not only on a no-vision one. `MEDIA_SUPPORT["ollama"]` says `image` for every Ollama model, and the comment beside it says text-only models ignore the images. On a text-only Ollama model, `_takes_pictures()` is therefore wrong. That model is offered `pages=` pictures, and `_deliver_tool_media` tells it to look at pictures it never receives. Ollama reports per-model capabilities (`/api/show`), and the fix belongs in the one authority, not in `read_pdf`. It has not been made. The backend in the motivating session was `local:`, which is correctly no-vision.
- **There is no per-machine lock.** The CLI and aish-web are two processes, so two recognitions can run at once across them. The write merge keeps the results correct. The memory peak can still double.

---

## The tool

`read_pdf` is native, not a droppable plugin: it needs the media store, the document store and the backend-sized output budget, which a subprocess cannot reach. It is in `READ_ONLY_TOOLS` (its only write is into aish's own store — the same argument as `show_image`, and content-addressed writes make it thread-safe on the parallel read path) and in `EGRESS_TOOLS` (its URL form is an outbound GET at a model-chosen host; its local-path form reaches none and is never gated). `TestReadPdf`.

Every result **leads with the structural map** — page count, which pages carry tables, figures, columns, scans. That header is the only thing standing between a partly-unreadable document and a confident summary of it, so it is emitted first and always, including when the caller asked for one page.

A fetched PDF is saved into the document store rather than a temp file, so a re-read costs no second download and no approval. Non-PDF bytes under a `.pdf` URL are refused by their **magic bytes**, naming what actually came back — the same failure `show_image` guards against, and usually a login wall.

`read_url` on a PDF no longer dead-ends with *"not a text page"*; it names `read_pdf` with the URL filled in. A refusal that names no alternative is where the model starts improvising with curl.

---

## Sections: the document's own outline is an address (#361 slice 6)

`read_pdf(source, section="Wyniki finansowe")` serves one section by name. The addressing scheme is the outline the document DECLARES (`doc.get_toc`) — no heading inference, for the reason the browser's sections read only declared structure: identity and declaration are facts, "looks like a heading" is a guess. A section resolves to its page range (its page up to the next entry at the same or higher level) and is served by the **existing pages mechanism** — same rendition, same structural-map header, same scan escalation — so it is a resolver, not a second reader. The result names what it resolved (`[section: … — page(s) 2-4]`).

Three honest endings, none a guess: a hit; a miss answered with the outline (the failure message IS the index the model needed); and a document with no outline, which says so and points at `search=`/`pages=`. The **bare** call — the "what is this document" call — carries the outline when there is one; a pages/search result never does, because the index is pulled, not pushed. The outline is read live from the PDF (milliseconds) rather than keyed into the rendition, where it would invalidate every cached conversion for a feature that does not change the text. Matching is exact → folded → unique folded substring (`browse.fold`, one authority for diacritics). `TestOutlineSections`.

The same argument exists on `read_file` for Markdown, where the declared structure is the ATX headings (`files.markdown_headings` — code fences excluded, so a `#` inside a fence is code, not a phantom index entry). The section is served through the same numbered window as any read, so line numbers, `limit` and the continuation offset keep their meaning. `TestMarkdownSections` in `tests/test_files.py`.

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

**PyMuPDF**, and only PyMuPDF, for the PDF itself. It is the one library covering layout-aware text, table detection and page rasterization in a single binary wheel with no system libraries, and its AGPL licence matches aish's own. The one addition is the Vision binding for OCR on macOS, which reads pictures PyMuPDF has already rasterised (above).

Rejected: `pypdf` (no tables, cannot render, so the scan case stays broken); `pdfplumber` (good tables, cannot render); shelling out to poppler (makes a Homebrew install a silent prerequisite — and the whole point is to stop shelling out); `pymupdf4llm` (does all of this well, but drags in `onnxruntime`, `numpy` and `networkx` — a ~100 MB ML runtime for a PDF reader).

`find_tables` prints a one-off "install pymupdf_layout" recommendation to **stdout**, which in the CLI lands in the middle of an answer. `no_recommend_layout()` is called before use.
