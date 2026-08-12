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

**A video's still comes back as the player, not as a picture beside a link to it.** When the source was one of YouTube's thumbnail hosts (`web.thumbnail_video_id`), the line handed back is the composed `[![caption](file)](watch url)` — which the web UI renders as ONE card: the local still, with a play button on it. The reason it must happen here is provenance: the fetcher is the only thing that knows the stored bytes ARE a video's thumbnail, and a content-addressed path cannot say so afterwards. Without it the model wrote the picture and the link separately, they rendered as two cards, and the answer opened with the same image twice (#217 — the client-side backstop is `[ONE-CARD]` in `docs/web-frontend.md`). The instruction attached to it is imperative and says what NOT to do as well ("do not write a separate link to the same video"), per the prompt-hints convention. `TestShowImage`.

`show_image` is in `READ_ONLY_TOOLS` — its only write is an image into aish's own store, never user state, the same reasoning as the scratch dir's auto-approval, and content-addressed writes make it thread-safe on the parallel read path. It is also in `EGRESS_TOOLS`, because its URL form is an outbound GET at a model-chosen host; its local-path form reaches no host and is never gated.

---

## Delivery — the model looks at it too (#215)

**Vision used to be one-way.** A picture the OWNER attached reached the model natively (`_classify_attachments` → `images` on the user message → each backend's encoder). A picture a **tool** made reached it as a *file path in prose*, because a tool result is a string on every provider aish speaks to. So aish could fetch a photo, verify it, store it, hand back a line to display it — and not see it. `read_pdf`'s scan escalation had been quietly resting on the same hole since #219: the page was rasterised and described as readable, and what arrived was its filename.

That is the shape of gap that produces confident nonsense rather than an error. Nothing failed; the model simply answered about a picture it never saw — from the caption, the filename, or the page it came from.

**A tool now returns its pictures in the `ToolOutcome` envelope** (`images=(path, …)`), and `Agent._deliver_tool_media` hands them over. Three properties carry it:

**1 · The envelope, not the text.** The paths ride the result's metadata (L7), never a markdown line parsed back out of prose — re-deriving structure from a rendered string is exactly the guess the envelope exists to replace.

**2 · One follow-up user message per turn, after every result.** It is the only shape all four backends already encode as native media, and the tool-result slot itself is a plain string on two of the three cloud APIs — putting the pixels there would work on one provider and vanish on the others. It lands *after the whole turn's results*, never between two of them: on Anthropic one assistant turn's results share a single message, so a media message spliced into the middle would break the pairing. The converter there **joins** the media into that same open user entry rather than opening a second one, since consecutive user entries are not a shape the API takes.

**3 · It is written as `role:"user"` and must never look like one.** Every note opens with `session.NOTE_MARKER` (`[aish: …]`), the #171 marker that keeps a synthetic turn out of the transcript live *and* on replay. `NOTE_MARKER` is public for exactly this reason: a producer has to be able to **build** a note that classifies, not be recognized by luck.

**Capped, stated, and expiring.** At most `TOOL_IMAGES_PER_TURN` (8) images per turn, with the overflow named — a silent cap reads as "you have seen everything". And because pixels are re-encoded into **every later request**, an earlier task's delivery is dropped whole by the same trim that stubs old tool output (`_trim_tool_message`), leaving the note behind so the model can tell it once looked and ask again — the store is content-addressed, so a second look costs nothing. The owner's own attachment is deliberately exempt: it is not a tool output, they may refer back to it tasks later, and the marker is what tells the two apart.

**A backend with no vision gets a sentence, not silence** — it must know it is answering without having looked, the same rule the unreadable scan page follows. **claude-max is the known gap**: the SDK owns that conversation, so a delivery has nowhere to go and the model still gets only the path.

`TestToolMedia`, and the converter half in `tests/test_backends.py`.

---

## One boundary, three renderers

`Agent.workspace_roots()` = `roots` + the directories the process owns (media store, scratch, tool-output cache, document store). Consumed by `/file` (`WebServer._workspace_roots`), the PDF exporter, the CLI's `term_image`, `read_file`'s prompt rule, and the approver's path scoping — see the workspace-boundary section of `docs/documents-and-pdf.md` for why the read side was added (#220).

They disagreed before #188: the exporter trusted the scratch dir and `/file` did not, so a file the model wrote **where it is told to write throwaway files** printed fine in a PDF and 403'd in the chat with nothing saying why. `TestImageRoots`, `TestImageRootsAgreement`.

It is deliberately DISTINCT from `roots`, which is the auto-approval scope and is rebuilt authoritatively per session: the process-owned directories must be displayable without becoming directories the model may run commands in, and without being dropped on a session switch.

---

## The terminal renderer — `term_image.py`

The CLI's half of the same capability: `supports_images()` detects an inline-image-capable terminal, local paths are resolved against the same `workspace_roots()` boundary, and `emit()` writes the terminal's own image protocol. `TestSupportsImages`, `TestLocalImagePaths`, `TestEmit`.

---

## When it fails to render

The browser is the only place that knows an image did not appear, and telling the model is a separate mechanism with its own live-only rule — `[RENDERERR]` in `docs/web-frontend.md`, the record side in `docs/trace-records.md`.
