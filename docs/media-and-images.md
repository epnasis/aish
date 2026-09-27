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

The store is `state_dir/media`, deliberately **not** the scratch workspace: a picture an answer displays must outlive the chat that displayed it — an exported PDF, a transcript read months later — and scratch dies with its chat (#258; before that it died at session end, which broke the image on every reopen). The store is also shared across chats and content-addressed, which scratch must never be. Different lifetimes, different directories.

---

## The second producer that had to leave (#289, then #318)

For two weeks `show_image` was not the only thing writing here: every driven page was photographed at snapshot time and the JPEG went into this store, `browser.frames_dir()` being literally `Agent.media_dir`. It looked like the right home — content-addressed, bounded, already served, already inside `workspace_roots`.

**That last clause was the defect.** This store is inside `workspace_roots` because a picture the model ASKED for is one it may read back; the boundary's justification is that reading back what the process wrote *unprompted* grants nothing new, and a frame is written unprompted, from a page outside the machine. So `show_image(source=<a frame>)` read one back through `_read_local_image`, and since #215 a tool's pictures are ATTACHED to the conversation — a hostile page's pixels in the model's own context, bannerless, unattributed and untainted. `docs/browser.md` has the chain and the fix; the part that matters here is that **frames have their own store now** (`state_dir/frames`), outside every root, served to the owner by `/frame`.

Two things follow for this file. The first is that the location of a store is a security property and belongs to the CALLER: `media.py` provides mechanics — content addressing, sniffing, the LRU — and both stores use them, while the boundary decides which of them the model may read.

**The second is that the capacity finding this section used to record as *written down, not fixed* is now fixed by construction.** The numbers were real: a browse frame is the browse context's viewport (1440x820, `device_scale_factor` 1) at quality 50 — **67 KB**, ~27 ms to capture — so bytes were never the constraint (500 frames ≈ 33 MB against a 200 MB ceiling) and the FILE count was: `MEDIA_MAX_FILES` is 500, one frame per snapshot, ~30 per driving flow, **seventeen flows to turn the whole store over**. The prune is LRU by mtime, so the first file out was the oldest, which after a browsing week was plausibly a picture the owner had asked for — a broken image in an old chat with nothing saying why.

Separate stores mean **nothing a frame does can evict a picture in an answer**, and the open question the shared caps left (*do frames deserve their own budget, or a shorter retention?*) is answered by the budget itself: `FRAME_MAX_FILES` = 1000 and a ceiling of a hundred megabytes, against `MEDIA_MAX_FILES` = 500 and two hundred megabytes here. `docs/browser.md` says what those were measured against. The two kinds of picture are worth keeping for different lengths of time — a frame is a diagnostic that is stale within days, a picture in an answer is meant to outlive the chat — which is exactly why one cap could not serve both. Both remain **caches**: an evicted `show_image` picture is restored by re-fetching it, and an evicted frame reads back as a picture that is gone rather than as though nothing was captured (`aish explain` calls it purged; the trace row, which cannot tell eviction from a stale token, says only that it could not be loaded).

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

`Agent.workspace_roots()` = `roots` + the directories the process owns (media store, scratch, document store, transcripts, browser downloads) — but NOT the tool-output cache, which left in #317, and NOT the evidence-frame store, which left in #318. Both left for the same reason and it is the sentence the boundary rests on: everything in the list holds something the model asked for or was told to go and look at, and those two hold content from outside that the process wrote unprompted. Consumed by `/file` (`WebServer._workspace_roots`), the PDF exporter, the CLI's `term_image`, `read_file`'s prompt rule, and the approver's path scoping — see the workspace-boundary section of `docs/documents-and-pdf.md` for why the read side was added (#220).

They disagreed before #188: the exporter trusted the scratch dir and `/file` did not, so a file the model wrote **where it is told to write throwaway files** printed fine in a PDF and 403'd in the chat with nothing saying why. `TestImageRoots`, `TestImageRootsAgreement`.

It is deliberately DISTINCT from `roots`, which is the auto-approval scope and is rebuilt authoritatively per session: the process-owned directories must be displayable without becoming directories the model may run commands in, and without being dropped on a session switch.

---

## The terminal renderer — `term_image.py`

The CLI's half of the same capability: `supports_images()` detects an inline-image-capable terminal, local paths are resolved against the same `workspace_roots()` boundary, and `emit()` writes the terminal's own image protocol. `TestSupportsImages`, `TestLocalImagePaths`, `TestEmit`.

---

## The image check at delivery (#430)

**The render-error channel told the model a turn too late.** In `session-20260925-204008-294943` a model with no vision built charts with a script, called `show_image` ONCE, and pasted three picture paths in the shape it had just seen — `media/<12 hex>-<slug>.png`. The twelve characters are a hash of the picture's bytes, so no model can predict them; `ls` of the store found none of the three. The only feedback was the browser's report, delivered on the NEXT turn, after the owner had already looked at broken pictures — and after the last of those turns no report was recorded at all, for a reason still not known.

So the answer's pictures are checked **before delivery**, in the same pass as the rules' Verify (`Agent._check_images`), and one rework answers both. The parser is `media.image_embeds`: a copy of the renderer's own picture grammar (`IMAGE_LINK_RE`, `INLINE_RE`'s image branch, the fence and code-span rules) held equal to app.js by `TestRendererLockstep` — a homegrown reading would fail text the renderer never shows as a picture, or pass one it does. What may DISPLAY is the agent's policy, not the store's (the same split as the store's location, #318): `_image_findings` mirrors `imageSrc` and `/file` — a whitelisted remote host (`RENDERED_IMAGE_HOSTS`, held equal to `IMG_FETCH_HOSTS` and the CSP), or an absolute path to a picture file inside `workspace_roots()`.

**Two thresholds, on purpose.** The model is ASKED about every picture that is not a file in the media store — web addresses and scratch files included, because the owner asked for everything to go through `show_image`, and a scratch chart dies with its chat (#258). But only a picture that WOULD NOT DISPLAY is ever REMOVED: past `IMAGE_MAX_ASKS`, or when asking is not allowed, a working picture is never taken away. Every reason is a test made on the line that states it (`no such file`, `outside the folders the app may show`, …) and the removal note says *would not display*, not *show_image did not produce* — an LRU-evicted `show_image` file is the case where those differ.

**Where it can and cannot rework.**
- **A removal is said only by the pass that delivers** (`_commit_image_removal`): a rule may still send the stripped draft back, and a note about a draft the owner never saw would stream with nothing to attach to and never be logged — found in delivery review, `test_a_removal_from_a_draft_a_rule_then_rejects_is_never_said`.
- **A held answer** (a verify rule binds the turn — for this owner, every turn): asked up to twice, with the failing targets, their reasons and the lines `show_image` actually returned this task (`_shown_images`). Past that, the broken embeds become their alt text (a card keeps its link) and an `[aish]` note says how many went and why. The hold buffer is emptied before release, because `_release_held` streams the BUFFER — the model's own tokens — and live and log would otherwise disagree.
- **A denial's stop gate or a silent model**: never asked (#81 outranks every Verify ask), still stripped.
- **An answer that already streamed** (no hold): left exactly as written — editing it would split the live chat from the log — and the model is told on its next turn by a system note.
- **claude-max** (`verify_final`): the conversation is the SDK's, so there is no next turn to tell; the owner gets the note on the answer.
- **Narration** already shown cannot be edited. Its failing pictures ride the FIRST ask, so the answer can carry them properly; asking about them again would send back an answer that had complied.

**Only the pictures send it back → the rules write nothing.** `_verify_answer(delivering=False)` still lets a rule ask in the same message, but a pass with nothing to ask records no delivering-pass verdicts, since no answer was delivered for them to describe.

**The note to a model that cannot see** used to end "Say so rather than describing what you cannot see", and the model read it as *you cannot show pictures* and left the line out. It now adds `TOOL_MEDIA_PASTE_ANYWAY` — but only when `show_image` is among the producers: `read_media` frames and `read_pdf` scans have no display line, and telling the model to paste one would have it type a path this very check fails.

Trace: `gate{at:"verify", gate:"image.verify"}` (`docs/trace-contract.md` §6.15), logged only when armed (the answer carries a picture, or shown narration did and is being asked about), verdict `refused` (asked) / `advised` (removed, or the model told afterwards) / `allowed`. `TestImageCheck`, `TestNoVisionNote`, `TestImageEmbeds`, `TestRendererLockstep`.

---

## When it fails to render

The browser is the only place that knows an image did not appear, and telling the model is a separate mechanism with its own live-only rule — `[RENDERERR]` in `docs/web-frontend.md`, the record side in `docs/trace-records.md`.
