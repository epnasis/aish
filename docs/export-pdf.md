# PDF export

`export.py`: local Markdown → PDF for the web UI (#64). `markdown` → HTML → `xhtml2pdf` — pure Python, no system libraries, and **the text never leaves the machine**. Heavy imports are deferred into the render function so server startup stays cheap.

---

## Two shapes

One answer, or a whole session's **final** answers. "Final" is structural — an assistant message NOT immediately followed by a tool result — which cleanly drops thinking and tool turns without any heuristic about content. `TestExportAssembly` tests that boundary as pure markdown assembly, without touching a PDF.

---

## Media embedding (#133)

A local `![](path)` image inlines as base64 **only when the symlink-resolved path stays inside the caller's workspace boundary** — the single boundary `/file` serves under (see `docs/media-and-images.md`). Anything outside becomes a captioned link card and is never read. That containment is `files.contains`, the one everything else asks too (#309) — this module used to resolve its own way, `/file` another, and the approval gate a third; `docs/agent-core.md`, *One path-containment function*. The `src.startswith("/")` test in front of it is not part of it: it is the decision that a relative or `~` path in an exported document has no trusted anchor to resolve against at all.

Remote images, whitelisted Google static-map snapshots (needs `GOOGLE_MAPS_API_KEY`) and YouTube thumbnails ARE fetched at export time, each with a timeout and a size cap and a link-card fallback — and each **through `web.py`'s one guarded fetcher** (#178 P1-4, #308). `fetch_image` calls `web.fetch_binary`, so a model-written `![](http://169.254.169.254/…)` cannot fire a server-side GET at metadata or LAN targets when the owner taps export. A blocked URL degrades to the same link card as any other fetch failure. `TestExportMedia` fakes all network at `export.fetch_image`.

It **calls** that fetch rather than repeating it, and the distinction is the whole of #308. This module used to build its own `Request`, open `web._opener` itself and apply its own timeout and cap — three lines away from `_fetch`, and free to drift back to the version that had no guard at all. Export's differences are real but small, so they are ARGUMENTS: an 8 MB cap (a page of prose is not a photograph), a 5 s timeout (an export is a foreground tap, not a research read), and `user_agent="aish-export"` so a server log can tell an export apart from a read. Nothing in this module opens a socket now, which is what `TestOneGuardedFetcher` in `tests/test_web.py` enforces for every module under `aish/`; the rationale is in `docs/agent-core.md`, *One guarded fetcher*.

---

## Nested blocks in lists (#172)

Neither library handles the markdown the model actually writes, so `_pdf_markdown_extension()` — built lazily, keeping `markdown` deferred — adds two pieces.

**`IndentedFence`** is a preprocessor ahead of `fenced_code` (priority 26 vs 25), which matches a fence ONLY at column 0: a code block indented under a list item was never recognised and leaked out as one run of inline `<code>` with its info string in the text. It stashes indented fences as ready-made `<pre>` HTML *at their original indentation*, so the block parser still attaches them to their item and the page-splitting reflow still applies inside a list. Its closing-fence regex allows the opening indent **+3 spaces and no more** — accepting any indentation let the inner close of a ` ```markdown ` demo end the outer block early and desync everything after it. A column-0 block is skipped wholesale as the stock extension's job.

**`FlattenLists`** is a treeprocessor rewriting every `<ul>`/`<ol>` into `<p>`s carrying a hand-drawn marker with a hanging indent, plus a `margin-left` continuation `<div>`. xhtml2pdf silently DROPS an `<li>`'s marker as soon as the item holds a block child — exactly what a loose or multi-paragraph item is — and renders sub-lists flat, so correct HTML still printed unnumbered. Continuation indents are RELATIVE, so a nested list simply stacks its own hang on the parent's. A `<p>` holding only a raw-HTML placeholder is never merged into a marker line, because the postprocessor only substitutes a bare `<p>placeholder</p>` and merging would leak the placeholder into the PDF.

---

## Titling an answer export (#172)

The title is also the download name (`safe_pdf_filename`), and it used to be the PROMPT the frontend sent along — a request ("test it with some difficult nested markdown"), not a description of the document.

`handle_export_answer` now takes no title at all. `WebServer._answer_title` asks the session's OWN model for a 3–6 word title in the document's own language, calling `agent.chat` directly — no `run_task`, so the turn never enters the conversation or the log — off-loop under `TITLE_TIMEOUT`.

`clean_title` unwraps what models pad a one-liner with (a `Title:` label, quotes, a `##`, a sentence of preamble — the LAST non-empty line is the title) and returns None for anything that is not a title. That is the fail-safe: every failure path — claude-max, which exposes no `chat`; a transport error; a timeout; a rambling reply — falls back to `derive_title`, a deterministic local read of the answer's lead heading or first sentence. **A title is a nicety and must never fail or block an export.** The whole-session export uses the chat's own title instead. `TestExportEndpoints`.
