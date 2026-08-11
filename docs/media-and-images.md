# Media and images — `show_image`, the store, the one boundary

`media.py`, `Agent._show_image`, `Agent.workspace_roots()`, `term_image.py`.

---

## Why it exists

"Show me what X looks like" was a routine request with **no capability**. Five unrelated primitives — text search, text fetch, shell + curl, markdown image syntax, a roots-gated `/file` — had to be reassembled blind by the model, every failure landed in the browser AFTER the turn was over, and the model compensated by writing memories about it (six of them, one instructing itself to litter the project tree). A capability gap that produces memories about the gap is the signal to build the capability.

---

## Three properties carry the design

**1 · The fetch is server-side**, through `web.py`'s SSRF guard. That is what lets arbitrary image sources work while the browser still loads only same-origin bytes — the CSP and the renderer's own `IMG_FETCH_HOSTS` whitelist stay exactly as strict, and the zero-click `![](https://attacker/…)` channel stays closed. Security is preserved by **relocating** the fetch, not by loosening policy.

**2 · Format detection is magic bytes, never the extension** (`sniff`). The failure it exists to catch is a WAF or hotlink block, or the page itself, served under a `.jpg` URL — which the extension agrees with and no decoder does. `TestSniff`.

**3 · Files are content-addressed and the store is bounded LRU** (`store` / `prune`, `MEDIA_MAX_*`). A retry is free, duplicates are impossible, and a re-fetch restores anything evicted. `TestStore`, `TestPrune`.

The store is `state_dir/media`, deliberately **not** the scratch workspace: scratch is deleted at session end and a transcript is permanent, so a picture left there is a broken image on every reopen. Different lifetimes, different directories.

---

## `Agent._show_image`

Orchestrates the above and returns a **ready-built markdown line** — aish composes it, so a caption containing a bracket or a newline cannot silently break the image parser. Every failure returns a sentence naming what went wrong, so the model learns **during** the turn rather than from the user reporting it afterwards. `TestShowImage`.

**A video's still comes back as the player, not as a picture beside a link to it.** When the source was one of YouTube's thumbnail hosts (`web.thumbnail_video_id`), the line handed back is the composed `[![caption](file)](watch url)` — which the web UI renders as ONE card: the local still, with a play button on it. The reason it must happen here is provenance: the fetcher is the only thing that knows the stored bytes ARE a video's thumbnail, and a content-addressed path cannot say so afterwards. Without it the model wrote the picture and the link separately, they rendered as two cards, and the answer opened with the same image twice (#219 — the client-side backstop is `[ONE-CARD]` in `docs/web-frontend.md`). The instruction attached to it is imperative and says what NOT to do as well ("do not write a separate link to the same video"), per the prompt-hints convention. `TestShowImage`.

`show_image` is in `READ_ONLY_TOOLS` — its only write is an image into aish's own store, never user state, the same reasoning as the scratch dir's auto-approval, and content-addressed writes make it thread-safe on the parallel read path. It is also in `EGRESS_TOOLS`, because its URL form is an outbound GET at a model-chosen host; its local-path form reaches no host and is never gated.

---

## One boundary, three renderers

`Agent.workspace_roots()` = `roots` + the directories the process owns (media store, scratch, tool-output cache, document store). Consumed by `/file` (`WebServer._workspace_roots`), the PDF exporter, the CLI's `term_image`, `read_file`'s prompt rule, and the approver's path scoping — see the workspace-boundary section of `docs/documents-and-pdf.md` for why the read side was added (#212).

They disagreed before #188: the exporter trusted the scratch dir and `/file` did not, so a file the model wrote **where it is told to write throwaway files** printed fine in a PDF and 403'd in the chat with nothing saying why. `TestImageRoots`, `TestImageRootsAgreement`.

It is deliberately DISTINCT from `roots`, which is the auto-approval scope and is rebuilt authoritatively per session: the process-owned directories must be displayable without becoming directories the model may run commands in, and without being dropped on a session switch.

---

## The terminal renderer — `term_image.py`

The CLI's half of the same capability: `supports_images()` detects an inline-image-capable terminal, local paths are resolved against the same `workspace_roots()` boundary, and `emit()` writes the terminal's own image protocol. `TestSupportsImages`, `TestLocalImagePaths`, `TestEmit`.

---

## When it fails to render

The browser is the only place that knows an image did not appear, and telling the model is a separate mechanism with its own live-only rule — `[RENDERERR]` in `docs/web-frontend.md`, the record side in `docs/trace-records.md`.
