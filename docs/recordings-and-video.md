# Recordings — looking at a video (`read_media`, `recordings.py`)

`recordings.py`, `Agent._read_media`, and the delivery channel it rides (`docs/media-and-images.md`).

---

## Why it exists

aish could **show** a picture, and since #215 **look at** one — but it could not **make** one. So every question about what is on screen was unanswerable, and the failure was total rather than partial: *"No, I cannot watch or analyze the actual video frames, pixels, or visual content."*

The two jobs that define the feature, both the owner's:

- **"Who's the middle one"** — a 28-second Short, three musicians, no captions. Purely visual. Words would not have helped *even if they had existed*.
- **"Show me what the new phone looked like"** — from a two-hour keynote. Find the few moments the object is on screen, and produce **pictures**.

Neither is a transcript question. That matters, because the plan for this feature drifted to transcripts anyway — see *What the build order got wrong* below.

---

## The design: probe once, then seek

`read_pdf`'s "convert once, then read normally" in another medium. **Probing** resolves what the recording IS — length, title, uploader, chapters, whether it is live, whether it even has pictures — and where its bytes are. After that any moment is one seek away, and **the model chooses its own step size**: coarse to find the section, fine once it is close. `TestReadMedia`.

Times are accepted in every shape the model writes them — `90`, `90s`, `2m`, `1:30`, `1:02:03` — because an answer cites whatever comes back, so guessing wrong about which was meant puts a frame somewhere nobody asked about (`TestTimeParsing`).

**The timestamp is the addressing scheme, and it may never lie.** That is the page marker, and it is load-bearing for the same reason: "at 12:34 he shows the phone" is worthless if 12:34 is not where the picture came from. A seek lands on the nearest frame the container allows, so `-copyts` plus ffmpeg's `showinfo` filter report the **decoded presentation timestamp**, and that is what is returned, labelled and cited. Returning the *requested* time would be a page number that lies. With no `showinfo` line to read, the requested time is all there is and is never dressed up as measured. `TestFrameTimestamps`.

**Nothing is downloaded.** yt-dlp resolves a directly-seekable URL and ffmpeg range-seeks into it: a frame from 45:00 of a 113-minute keynote measured **5.9 seconds with nothing written to disk**. Downloading was the obvious design and is strictly worse — minutes of wait and gigabytes on disk to answer a question about four seconds of footage.

---

## Two guards, because this hands a URL to someone else's network stack

**1 · The SSRF check runs before a subprocess sees the URL**, and again on the **resolved stream**. `EGRESS_TOOLS` gates which host the *model* may name; where that host's stream then points is a second question, and an open redirect is how it gets answered. yt-dlp and ffmpeg have their own network code and none of `web.py`'s guards, so the check travels with the URL rather than living at the tool boundary — which is why `web.require_public` exists as a public name.

**2 · ffmpeg runs under an explicit protocol whitelist.** It natively speaks `file:`, `concat:` and playlist redirection, so without one a media URL that redirects into `file:///etc/...` turns "read a podcast" into an arbitrary file read. A whitelist and not a denylist: the protocols worth having are few and known, the dangerous ones open-ended. A local file gets `file` and nothing else. `TestGuards`.

---

## What the map must say, and what it may not claim

Emitted **first and always**, for `read_pdf`'s reason: what is ABSENT has to be as visible as what is present, or the caller reads silence as completeness.

- **Chapters are labelled as the uploader's claim**, never as structure aish verified. They are unverified third-party text, and presenting them as a classification header is the hollow-extraction failure in a new costume. "No chapters" is stated, not left as an empty section.
- **Captions**: which languages exist, and — until slice 2 — that reading them is not built. "None published" is stated too, because it means there are no words to read at all.
- **Audio-only** is declared up front, and asking for pictures says there are none rather than returning nothing.
- **A moment past the end names the length** rather than quietly handing back the last frame, and an unreadable one loses only itself, never the frames around it. `TestFrameRefusals`.
- **A live stream is refused**: it has no fixed timeline, so there is no position to seek to and no honest way to guess one.
- **An extraction failure says what to DO about it.** A bare extractor traceback reads as "this source is unavailable" and sends the model looking somewhere else — the substitution failure `docs/tools-layer.md` already has a scar from. Age-gate, private, geo-block, DRM and *a missing JavaScript runtime* each imply a different action, and the last one names `brew install deno`. The translation lives in `probe`, not in the yt-dlp wrapper, so it applies to whatever resolves the URL rather than to one implementation — a seam that skipped it would test a path nothing runs.
- **The uploader's description is offered**, because a music video's own text routinely NAMES who is in it. Searching that beats searching a description of a face, and it is already in hand.

---

## Frames, and the cap that keeps display honest

Frames are stored in the media store and ride #215's envelope, so the model **sees** them; the same call returns the markdown line so the owner can too.

`MEDIA_FRAMES_PER_CALL` (6) is deliberately **below** `TOOL_IMAGES_PER_TURN` (8), so one call is always delivered whole. A call returning more pictures than the turn can carry would print display lines for frames the model never saw — and it would then describe them. Frames are downscaled to 640px before encoding: a model reads that as well as it reads 4K, and the difference is context cost paid again on every later request.

Conflicting arguments **error rather than resolving to a winner** (`at=` with `chapter=`, `count=` without `every=`): a model that wrote both did not know which it meant, and silently honouring one returns frames from a place nobody asked about, cited as though they were. The opening frame is deliberately not second zero — a video's first moment is routinely black, a title card or a logo, and a blank picture reads as "nothing to see" rather than "you looked too early".

A recording is probed **once per session** and the cache is dropped when the signed URL expires, preferring the signer's own `expire=` over a guessed lifetime. The alternative is an opaque HTTP 403 mid-task, which reads as "the video is gone" rather than "re-resolve me" — so that failure names the recovery.

---

## Dependencies

**yt-dlp** and **imageio-ffmpeg** — ffmpeg as a wheel, not a Homebrew install, for the reason `docs/documents-and-pdf.md` rejected poppler: a system binary must not be a silent prerequisite. A PATH ffmpeg is used if the wheel is missing, as a courtesy rather than the supported configuration. `imageio-ffmpeg` ships ffmpeg but **not ffprobe**, so a local file's duration is parsed from ffmpeg's own output rather than adding a second binary to read a number ffmpeg already prints.

**The unresolved one:** yt-dlp now warns that YouTube extraction wants a JavaScript runtime (deno) and that "some formats may be missing" without it. Vendoring a JS runtime would be the `pymupdf4llm` rejection at triple the weight, and the poppler rule forbids *silent* prerequisites, not named ones — so the failure is detected and names the fix instead. `TestProbe` covers what a probe establishes and every way it can fail; `TestSummary` pins what the map states when something is absent.

---

## What the build order got wrong (#216)

The first plan led with **captions**, and it was wrong in a way worth recording, because the reasoning looked sound at every step.

The `read_pdf` analogy set the frame, and the thing being copied only has machinery for **text** — so once "a recording is a document", the layer with a rendition looked primary and pixels became phase two. Then the order inside that frame was chosen by what fixtures can pin ("deterministic, zero network"), which is **correctness-provable beating worth-building**. A supporting claim — "what did they say about X is the median question" — was asserted rather than drawn from the only evidence on file, which said the opposite.

**The tell: that slice 1 could not replay the session that filed the issue.** A captions-only first slice, on a Short with no captions, reproduces the exact failure the work exists to fix.

So: **a first slice must service the transcript that motivated it, end to end.** A plan that cannot is drifting, however clean it looks. The "zero network" argument was also false uniqueness — parameter seams make frame extraction just as deterministic, which is what `tests/test_recordings.py` does.

What survived the correction, and is still the plan for slice 2: **speech is the INDEX that makes seeing affordable, not a parallel feature.** Blind-scanning a two-hour keynote at one frame per two minutes is ~60 frames and 60–90k tokens; one caption search for "iPhone" locates the moments and ~8–16 frames answer it. The deliverable stays pictures. The index only finds what is *talked about*, so an object shown silently needs the coarse scan — and the result has to say which one it did.
