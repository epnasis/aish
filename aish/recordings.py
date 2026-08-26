"""Video and audio → pictures of any moment in them, addressed by time (#216).

aish could show a picture and, since #215, look at one — but it could not
*make* one. So every question about what is ON screen was unanswerable: "who is
the middle one in this clip", "show me what the new phone looked like in the
keynote". Seeing was the missing capability, and no amount of transcript
substitutes for it.

The design is `probe once, then seek`, the moving-picture form of `read_pdf`'s
convert-once-then-read. Probing resolves what the recording IS — duration,
title, chapters, whether it is live — and where its bytes are; after that, any
moment is one seek away and the model chooses its own step size.

**The timestamp is the addressing scheme, and it may never lie.** That is
`read_pdf`'s page marker in another medium, and it is load-bearing for the same
reason: an answer built on "at 12:34 he shows the phone" is worthless if 12:34
is not where the picture came from. A seek lands where the container's
keyframes allow, not necessarily where it was asked to, so every frame is
stamped with the timestamp ffmpeg reports having actually DECODED — never the
one that was requested. `TestFrameTimestamps`.

**Nothing is downloaded.** yt-dlp resolves a direct media URL and ffmpeg range-
seeks into it, so a frame from the middle of a two-hour keynote costs one HTTP
range request and a few seconds — measured at 5.9s for 45:00 of a 113-minute
video, with nothing written to disk. Downloading was the obvious design and it
is strictly worse: minutes of wait and gigabytes on disk to answer a question
about four seconds of footage.

**Two guards, because this hands a URL to somebody else's network stack.**
`web.py`'s SSRF check runs on the source URL BEFORE yt-dlp or ffmpeg sees it —
those have their own network code and none of this repo's guards. And ffmpeg is
run with an explicit protocol whitelist: it natively speaks `file:`, `concat:`
and playlist redirection, so a media URL that redirects into `file:///etc/...`
turns "read a podcast" into an arbitrary-file-read. The whitelist is what makes
that structurally impossible rather than merely unlikely. `TestGuards`.

**Both effectful edges are parameter seams** (`extract` for yt-dlp, `run` for
ffmpeg), so the tests drive this with no network and no subprocess — the same
shape as `email_poll.py`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import provenance, web

# Frames are downscaled before they are ever encoded. A model reads a 640px
# frame as well as a 4K one and the difference is pure context cost, paid again
# on every later request in the task (docs/media-and-images.md).
FRAME_WIDTH = 640
FRAME_QUALITY = 4  # ffmpeg -q:v, 2..5 is visually clean

# How long a resolved media URL is assumed good for when it carries no expiry
# of its own. They are signed and time-limited; re-probing costs ~1-2s.
URL_TTL_SECONDS = 1800

# What ffmpeg may speak. It is a whitelist and not a denylist on purpose: the
# protocols worth having are few and known, and the dangerous ones (file,
# concat, subfile, playlists that redirect into them) are open-ended.
REMOTE_PROTOCOLS = "http,https,tcp,tls,crypto"
LOCAL_PROTOCOLS = "file"

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})\.(\d+)")
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
_CLOCK_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)$")
_SPAN_RE = re.compile(r"(?i)^(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs)?$")
_SPAN_UNITS = {"s": 1, "sec": 1, "secs": 1, "m": 60, "min": 60, "mins": 60,
               "h": 3600, "hr": 3600, "hrs": 3600, None: 1}


class RecordingError(Exception):
    """Anything that stops a recording being read, phrased for the model."""


@dataclass(frozen=True)
class Chapter:
    start: float
    title: str


@dataclass(frozen=True)
class CaptionTrack:
    language: str
    url: str
    is_generated: bool
    # A MACHINE TRANSLATION of the speech, not a transcription of it. YouTube
    # offers ~150 of these per video and they are indistinguishable from the
    # real track by language code alone: ask for Polish on an English video and
    # you get fluent Polish that nobody in the recording ever said.
    is_translation: bool = False


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    """A caption track, converted once, with what is WRONG with it measured.

    The fields after `cues` exist because a caption track can be fluent and
    still not be this recording's words: auto-generated, in another language,
    covering a third of the running time, or belonging to a different edit that
    was re-uploaded. None of that is visible in the text itself, which is
    exactly the shape of failure `read_pdf` classifies pages to prevent — so it
    is MEASURED here and stated wherever the words are used.
    """

    path: Path
    language: str
    is_generated: bool
    is_translation: bool
    original_language: str
    asked_for: str
    cues: tuple[Cue, ...]
    duration: float
    # The request existed but every track matching it was machine-translated,
    # so the original was read instead. Refusing is right; refusing SILENTLY
    # would look like the request was never made.
    asked_only_translated: bool = False

    @property
    def covered(self) -> float:
        """Seconds the cues actually speak for."""
        return sum(max(0.0, cue.end - cue.start) for cue in self.cues)

    @property
    def coverage(self) -> float:
        """Cued time as a fraction of running time; 0.0 when length is unknown."""
        return min(1.0, self.covered / self.duration) if self.duration else 0.0

    @property
    def last_cue_end(self) -> float:
        return self.cues[-1].end if self.cues else 0.0

    @property
    def largest_gap(self) -> tuple[float, float]:
        """The longest stretch with no words in it, as (start, seconds)."""
        widest = (0.0, 0.0)
        previous = 0.0
        for cue in self.cues:
            if cue.start - previous > widest[1]:
                widest = (previous, cue.start - previous)
            previous = max(previous, cue.end)
        if self.duration and self.duration - previous > widest[1]:
            widest = (previous, self.duration - previous)
        return widest


@dataclass(frozen=True)
class Recording:
    """What a recording IS, established before any of it is read.

    `read_pdf` classifies every page before reading it because a hollow read
    otherwise passes as a complete one. The same rule applies here, and the
    fields that carry it are the honest ones: `duration` may be 0.0 (unknown),
    `chapters` may be empty, and `is_live` means there is no fixed timeline to
    address at all. None of those may be silently papered over downstream.
    """

    source: str
    identity: str
    media_url: str
    is_local: bool
    original_language: str = ""
    title: str = ""
    uploader: str = ""
    description: str = ""
    duration: float = 0.0
    is_live: bool = False
    has_video: bool = True
    chapters: tuple[Chapter, ...] = ()
    caption_tracks: tuple[CaptionTrack, ...] = ()
    expires_at: float = 0.0
    notes: tuple[str, ...] = field(default=())

    @property
    def protocols(self) -> str:
        return LOCAL_PROTOCOLS if self.is_local else REMOTE_PROTOCOLS


def parse_time(text: str) -> float:
    """"90" / "90s" / "2m" / "1:30" / "1:02:03" -> seconds.

    Accepts both because the model writes both, and guessing wrong about which
    one it meant would put a frame somewhere the answer then cites.
    """
    raw = str(text).strip()
    if not raw:
        raise RecordingError("no timestamp given")
    clock = _CLOCK_RE.match(raw)
    if clock:
        hours, minutes, seconds = clock.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
    span = _SPAN_RE.match(raw)
    if span:
        return float(span.group(1)) * _SPAN_UNITS[(span.group(2) or "").lower() or None]
    raise RecordingError(
        f"{raw!r} is not a timestamp — use 90, 90s, 2m, 1:30 or 1:02:03"
    )


def format_time(seconds: float) -> str:
    """Seconds -> the h:mm:ss the model and the user both read."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _ffmpeg_exe() -> str:
    """The bundled ffmpeg, falling back to one on PATH.

    Bundled first for the reason `docs/documents-and-pdf.md` rejected poppler:
    a Homebrew install must not be a silent prerequisite. The PATH fallback
    exists so a machine that already has ffmpeg is not forced to download a
    second copy, not as the supported configuration.
    """
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if not found:
            raise RecordingError(
                "ffmpeg is not available — this aish installation is missing "
                "imageio-ffmpeg. Do NOT retry; tell the user to reinstall aish."
            ) from None
        return found


def _run_ffmpeg(args: list[str]) -> tuple[int, str]:
    """Run ffmpeg, returning (exit code, stderr). The subprocess seam."""
    process = subprocess.run(
        [_ffmpeg_exe(), *args], capture_output=True, text=True, timeout=180, check=False
    )
    return process.returncode, process.stderr


def _yt_dlp_info(url: str) -> dict:
    """Resolve a remote recording's metadata and a directly-seekable URL.

    The format preference is deliberately modest: frames are downscaled to
    640px anyway, so pulling a 4K stream would buy nothing and cost range
    requests proportional to the bitrate.
    """
    try:
        import yt_dlp
    except ModuleNotFoundError:
        raise RecordingError(
            "yt-dlp is not available — this aish installation is incomplete. "
            "Do NOT retry; tell the user to reinstall aish."
        ) from None
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "bestvideo[height<=720]/best[height<=720]/bestaudio/best",
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise RecordingError(f"nothing could be extracted from {url}")
    if info.get("entries"):  # a search or playlist URL — take the first entry
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RecordingError(f"{url} contains no playable entry")
        info = entries[0]
    return info


def _extraction_problem(message: str) -> str:
    """yt-dlp's failure, translated into what the caller should DO about it.

    A bare extractor traceback reads as "this source is unavailable", and the
    model then goes looking somewhere else — the substitution failure this
    repo already has a scar from. Each of these is a different action.

    Every classified line says "the extractor reports", because that is what
    was observed: a substring in an arbitrary error message, not the video's
    actual state. The raw excerpt rides along so a misclassification can be
    seen for what it is instead of standing as aish's own claim.
    """
    lowered = message.lower()
    if "sign in" in lowered or "age" in lowered and "confirm" in lowered:
        return (
            "the extractor reports this video as age-restricted or needing a "
            f"sign-in, so its frames cannot be read. Tell the user that. "
            f"({message.strip()[:200]})"
        )
    if "private" in lowered or "members-only" in lowered:
        return (
            "the extractor reports this video as private or members-only. "
            f"({message.strip()[:200]})"
        )
    if "not available in your country" in lowered or "geo" in lowered:
        return (
            "the extractor reports this video as not available in this "
            f"region. ({message.strip()[:200]})"
        )
    if "drm" in lowered:
        return (
            "the extractor reports this video as DRM-protected, so it cannot "
            f"be read. ({message.strip()[:200]})"
        )
    if "javascript" in lowered or "js runtime" in lowered or "deno" in lowered:
        return (
            "YouTube extraction needs a JavaScript runtime that is not installed. "
            "Tell the user to run `brew install deno` — this will keep failing "
            f"until they do. ({message.strip()[:200]})"
        )
    return f"the video could not be opened: {message.strip()[:300]}"


def _local_recording(path: Path, run) -> Recording:
    """Probe a file on disk. ffmpeg is the only prober — `imageio-ffmpeg` ships
    ffmpeg but NOT ffprobe, and adding a second binary to read a duration that
    ffmpeg already prints would be a dependency bought for nothing."""
    _, stderr = run(["-hide_banner", "-i", str(path), "-f", "null", "-"])
    match = _DURATION_RE.search(stderr)
    duration = 0.0
    if match:
        hours, minutes, seconds, frac = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + int(seconds) + float(f"0.{frac}")
    if "Invalid data found" in stderr or ("Duration" not in stderr and "Stream" not in stderr):
        raise RecordingError(f"{path.name} is not a video or audio file ffmpeg can read")
    stat = path.stat()
    digest = hashlib.sha256(
        f"{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}".encode()
    ).hexdigest()[:16]
    return Recording(
        source=str(path),
        identity=f"local:{digest}",
        media_url=str(path),
        is_local=True,
        title=path.name,
        duration=duration,
        has_video="Video:" in stderr,
    )


def probe(source: str, *, extract=_yt_dlp_info, run=_run_ffmpeg) -> Recording:
    """What this recording is, and where its bytes are.

    A local path is probed with ffmpeg; a URL goes through the SSRF guard and
    then yt-dlp. The guard runs HERE, before the URL reaches a subprocess with
    its own network stack, because `EGRESS_TOOLS` gates which host the model
    may choose and says nothing about where the resolved stream then points.
    """
    raw = str(source).strip()
    if not raw:
        raise RecordingError("no source given")
    # Before the guard, the extractor and the cache key: a share link carries a
    # token identifying whoever shared it (`?si=…` from the Share button and
    # the iOS share sheet), which has no business being forwarded to the host —
    # and which would otherwise make one video look like a different recording
    # each time it is shared, paying for a fresh probe every time.
    raw = web.strip_tracking(raw)
    if not raw.lower().startswith(("http://", "https://")):
        path = Path(raw).expanduser()
        if not path.is_file():
            raise RecordingError(f"{raw} is not a file on this machine, and not a URL")
        return _local_recording(path, run)

    try:
        web.require_public(raw)
    except web.BlockedURLError as exc:
        raise RecordingError(f"blocked: {exc}. Use a normal public media URL.") from exc

    # Translation lives HERE, not inside the yt-dlp wrapper, so it applies to
    # whatever resolves the URL rather than to one implementation of it — a
    # seam that skips the error handling would test a path nothing runs.
    try:
        info = extract(raw)
    except RecordingError:
        raise
    except Exception as exc:  # yt-dlp raises its own hierarchy; all are fatal here
        raise RecordingError(_extraction_problem(str(exc))) from exc
    media_url = str(info.get("url") or "")
    if not media_url:
        # A manifest-only result (DASH/HLS split into separate audio and video)
        # has no single seekable URL on the info dict itself.
        formats = [f for f in (info.get("formats") or []) if f.get("url")]
        if not formats:
            raise RecordingError(f"no playable stream was found for {raw}")
        media_url = str(formats[-1]["url"])
    try:
        web.require_public(media_url)
    except web.BlockedURLError as exc:
        raise RecordingError(f"the resolved stream is blocked: {exc}") from exc

    chapters = tuple(
        Chapter(start=float(c.get("start_time") or 0.0), title=str(c.get("title") or "").strip())
        for c in (info.get("chapters") or [])
        if c
    )
    captions = _caption_tracks(info)
    extractor = str(info.get("extractor_key") or info.get("extractor") or "url").lower()
    ident = str(info.get("id") or hashlib.sha256(raw.encode()).hexdigest()[:16])
    return Recording(
        source=raw,
        identity=f"{extractor}:{ident}",
        media_url=media_url,
        is_local=False,
        title=str(info.get("title") or ""),
        uploader=str(info.get("uploader") or info.get("channel") or ""),
        description=str(info.get("description") or ""),
        original_language=_spoken_language(info, captions),
        duration=float(info.get("duration") or 0.0),
        is_live=bool(info.get("is_live")),
        has_video=(
            bool(info.get("vcodec") and info.get("vcodec") != "none")
            or bool(info.get("height"))
        ),
        chapters=chapters,
        caption_tracks=captions,
        expires_at=_url_expiry(media_url),
    )


def _caption_tracks(info: dict) -> tuple[CaptionTrack, ...]:
    """Caption tracks off the metadata, publisher-authored ones first.

    yt-dlp offers several formats per language; VTT and SRT are the ones with
    cue timings in them, and the timings are the entire point — a format
    without them would give words that cannot be turned back into a moment.
    """
    tracks: list[CaptionTrack] = []
    blocks = ((False, info.get("subtitles")), (True, info.get("automatic_captions")))
    for generated, block in blocks:
        for language, formats in (block or {}).items():
            best = next(
                (
                    f
                    for f in (formats or [])
                    if f.get("url") and str(f.get("ext", "")).lower() in ("vtt", "srt")
                ),
                None,
            )
            if best:
                url = str(best["url"])
                tracks.append(
                    CaptionTrack(
                        # `en-orig` is YouTube's marker for "the source track",
                        # not a language. Normalised HERE so the suffix cannot
                        # reach a label, a comparison or an answer.
                        language=str(language).split("-orig")[0],
                        url=url,
                        is_generated=generated,
                        # `tlang=` is YouTube's own marker for "translate the
                        # ASR into this language", and it is the ONLY reliable
                        # signal — the language code says nothing.
                        is_translation="tlang=" in url,
                    )
                )
    return tuple(tracks)


def _spoken_language(info: dict, tracks) -> str:
    """What language this recording is actually SPOKEN in.

    `info["language"]` is authoritative but frequently empty. The fallback is
    exact rather than a guess: YouTube's auto-caption track that is NOT a
    `tlang=` translation is speech recognition run on the audio, so its
    language IS the spoken one. Without this, a request that cannot be met
    falls back on dict order — which picked Chinese subtitles for a Polish
    request on an English keynote.
    """
    declared = str(info.get("language") or "").strip()
    if declared:
        return declared
    asr = next((t for t in tracks if t.is_generated and not t.is_translation), None)
    return asr.language.split("-orig")[0] if asr else ""


def _url_expiry(url: str) -> float:
    """When the resolved URL stops working, from its own `expire=` parameter.

    Reading the expiry the signer put there beats a guessed TTL: too short and
    every read re-probes, too long and the model meets an opaque HTTP 403 that
    looks like the video is gone.
    """
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        for key in ("expire", "expires"):
            if key in query:
                return float(query[key][0])
    except (ValueError, TypeError):
        pass
    return 0.0


def frame(
    recording: Recording, seconds: float, *, width: int = FRAME_WIDTH, run=_run_ffmpeg
) -> tuple[bytes, float]:
    """One frame, and the timestamp it ACTUALLY came from.

    `-ss` before `-i` is a seek into the container rather than a decode from
    the start — the difference between six seconds and six minutes on a long
    recording. It lands on what the container allows, so `-copyts` plus the
    `showinfo` filter report the decoded presentation timestamp and THAT is
    what is returned. Returning the requested time instead would be a page
    number that lies.
    """
    if recording.is_live:
        raise RecordingError(
            "this is a live stream: it has no fixed timeline, so there is no "
            "position to seek to. Say so rather than guessing at a moment."
        )
    if not recording.has_video:
        raise RecordingError(
            f"{recording.title or recording.source} has no video track — there "
            "are no pictures in it to look at."
        )
    if seconds < 0:
        raise RecordingError("a timestamp cannot be negative")
    if recording.duration and seconds >= recording.duration:
        raise RecordingError(
            f"{format_time(seconds)} is past the end — this recording is "
            f"{format_time(recording.duration)} long."
        )
    with tempfile.TemporaryDirectory(prefix="aish-frame-") as tmp:
        out = Path(tmp) / "frame.jpg"
        code, stderr = run(
            [
                "-nostdin",
                "-hide_banner",
                "-protocol_whitelist",
                recording.protocols,
                "-ss",
                f"{seconds:.3f}",
                "-copyts",
                "-i",
                recording.media_url,
                "-frames:v",
                "1",
                "-vf",
                f"scale={width}:-2,showinfo",
                "-q:v",
                str(FRAME_QUALITY),
                "-f",
                "image2",
                "-y",
                str(out),
            ]
        )
        if code != 0 or not out.is_file() or not out.stat().st_size:
            raise RecordingError(_frame_problem(stderr, seconds))
        data = out.read_bytes()
    match = _PTS_RE.search(stderr)
    # No pts_time means the filter chain did not report one; the requested time
    # is then all there is, and the caller is told it is approximate rather
    # than being handed a precise-looking number nothing verified.
    return data, float(match.group(1)) if match else float(seconds)


def _frame_problem(stderr: str, seconds: float) -> str:
    tail = " ".join(stderr.strip().splitlines()[-3:])[:300]
    if "403" in tail or "Forbidden" in tail:
        # A 403 here is USUALLY a signed CDN URL past its expiry — but that is
        # the likely reading of a substring in ffmpeg's stderr, not something
        # observed, so the sentence keeps the observation and the remedy and
        # says the cause as the guess it is.
        return (
            "ffmpeg reported a 403/Forbidden from the stream — the resolved "
            "URL has probably expired. Call read_media again for "
            "this source — it will resolve a fresh one."
        )
    return f"no frame could be read at {format_time(seconds)}: {tail}"


def summary(recording: Recording) -> str:
    """The structural map, emitted FIRST and always.

    `read_pdf`'s header is the only thing standing between a partly-readable
    document and a confident summary of it, and the same is true here: what is
    absent (no chapters, no captions, unknown duration) has to be as visible as
    what is present, or a caller reads silence as completeness.
    """
    parts: list[str] = []
    label = recording.title or Path(recording.source).name or recording.source
    if recording.is_live:
        head = f"{label} — LIVE, no fixed timeline"
    elif recording.duration:
        head = f"{label} — {format_time(recording.duration)} long"
    else:
        head = f"{label} — length unknown"
    if recording.uploader:
        head += f", by {recording.uploader}"
    if not recording.has_video:
        head += ", AUDIO ONLY (no pictures in it)"
    parts.append(head)

    if recording.chapters:
        lines = "\n".join(
            f"  {i}. {format_time(c.start)} {c.title}"
            for i, c in enumerate(recording.chapters, start=1)
        )
        parts.append(
            "Chapters, as PUBLISHED BY THE UPLOADER (nothing has verified them "
            f"against the recording):\n{lines}"
        )
    else:
        parts.append("Chapters: none published.")

    real = [t for t in recording.caption_tracks if not t.is_translation]
    translated = len(recording.caption_tracks) - len(real)
    if real:
        # Only the tracks that are a TRANSCRIPTION of this recording are named.
        # Listing the machine translations alongside them (YouTube publishes
        # ~150 per video) would present "Polish captions available" as a fact
        # about what was spoken, which is the confusion this whole section
        # exists to prevent.
        named = ", ".join(
            f"{t.language}{' (auto)' if t.is_generated else ''}" for t in real[:8]
        )
        line = f"Captions: {named}"
        if translated:
            line += (
                f", plus {translated} MACHINE TRANSLATIONS of those words into "
                "other languages (not what anyone said)"
            )
        parts.append(
            line + ". Search them with search= to find WHERE something is said, "
            "then look at that moment with at=."
        )
    elif translated:
        parts.append(
            f"Captions: only {translated} machine translations, with no "
            "transcription of the original speech. The words are a machine's, "
            "not the speaker's — searchable, but never quotable as speech."
        )
    else:
        parts.append("Captions: none published, so there are no words to read — only pictures.")
    return "\n\n".join(parts)


def describe(recording: Recording) -> str:
    """The uploader's own words about the recording, capped.

    Worth its context: a music video's title and description routinely NAME the
    people who are in it, which answers "who is that" far more reliably than a
    description of a face — and costs one already-fetched string.
    """
    text = re.sub(r"\n{3,}", "\n\n", recording.description).strip()
    if not text:
        return ""
    if len(text) > 800:
        text = text[:800].rstrip() + " …"
    return f"What the uploader says about it:\n{text}"


# ------------------------------------------------------ captions (#216 slice 2)
#
# Speech is the INDEX that makes seeing affordable, not a parallel feature.
# Blind-scanning a two-hour keynote at one frame every two minutes is ~60
# frames and 60-90k tokens; one search over the words finds the four moments
# worth rendering. The deliverable is still pictures.

# Part of the rendition's cache key, so improving the conversion invalidates
# old renditions instead of serving a stale one a test would then pass against.
CAPTION_VERSION = 1

CAPTION_STORE_MAX_BYTES = 40 * 1024 * 1024
CAPTION_STORE_MAX_FILES = 300

# One caption file. Generous — a three-hour auto-captioned stream is a few MB —
# but not unbounded, because this is fetched whole by design.
CAPTION_MAX_BYTES = 8 * 1024 * 1024

# Below this, the words cover so little of the running time that treating them
# as "the transcript" would be the hollow read this module exists to prevent.
COVERAGE_FLOOR = 0.5

_CUE_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
)
_CUE_HEADER = ("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", "REGION")
_TRANSCRIPT_HEADER = "<!-- aish-transcript "


def _cue_seconds(hours, minutes, secs, millis) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(millis.ljust(3, "0")) / 1000.0


def parse_cues(text: str) -> list[Cue]:
    """VTT or SRT -> cues, with the times the FILE gives.

    The timings are the whole reason a caption file is not just a text file:
    they are what makes a word searchable to a MOMENT, which is what the index
    is for. A parser that drops them and reconstructs positions from word
    counts produces timestamps that look exactly like facts and are fiction —
    `youtube_analyze` shipped that bug for months.
    """
    cues: list[Cue] = []
    start = end = 0.0
    buffer: list[str] = []
    open_cue = False

    def flush() -> None:
        if not open_cue or not buffer:
            return
        line = re.sub(r"\s+", " ", " ".join(buffer)).strip()
        # Rolling auto-captions repeat the previous cue's words with one line
        # added, which would otherwise duplicate every sentence in the store.
        if line and (not cues or cues[-1].text != line):
            cues.append(Cue(start=start, end=max(end, start), text=line))

    for raw in text.splitlines():
        line = raw.strip()
        match = _CUE_TIME.search(line)
        if match:
            flush()
            values = match.groups()
            start, end = _cue_seconds(*values[:4]), _cue_seconds(*values[4:])
            buffer, open_cue = [], True
            continue
        if not line or line.isdigit() or line.startswith(_CUE_HEADER):
            continue
        cleaned = re.sub(r"<[^>]+>", "", line).strip()
        if cleaned:
            buffer.append(cleaned)
    flush()
    return cues


def pick_track(tracks, prefer: str = "", spoken: str = "") -> CaptionTrack | None:
    """The caption track to read: **the language the recording is SPOKEN in**,
    unless a language was explicitly asked for.

    The owner's rule, and it is about comprehension rather than politeness: a
    model reads meaning out of the original far better than out of a
    translation of it, because a translation has already thrown away the
    ambiguity, the idiom and the register that the context turns on. Reading
    English captions on an English video and translating at the END, once the
    meaning is settled, beats reading somebody else's Polish rendering of it —
    even for a Polish speaker asking in Polish.

    So `prefer` is an OVERRIDE, not the default. Empty (the normal case) means
    "the original", and the ranking puts the spoken language first.

    **A real track always beats a machine translation** regardless. YouTube
    publishes ~150 of those per video carrying the requested language code, so
    an exact-language-first ranking hands back fluent Polish that nobody said,
    at 100% coverage because the machine translated every line. Publisher-
    authored over auto-generated within the same language; a translation is
    read only when there is no transcription at all, and `classification` then
    says so loudly.
    """
    if not tracks:
        return None
    wanted = (prefer or "").lower()
    source = (spoken or "").lower().split("-")[0]

    def rank(track: CaptionTrack) -> tuple[int, int, int, int, int]:
        language = track.language.lower()
        # Only when a language was explicitly named. Otherwise this term is
        # constant and the ORIGINAL wins, which is the point.
        exact = bool(wanted) and (language == wanted or language.startswith(f"{wanted}-"))
        # The recording's own words. Ranked above everything but an explicit
        # request, because it is the only track that is definitely what was
        # said, and because meaning survives translation worse than it
        # survives being read in a language you then translate yourself.
        original = bool(source) and language.split("-")[0] == source
        # Last tiebreak, and openly a pragmatic one rather than a principled
        # one: with no ASR track and no declared language there is nothing that
        # identifies the original, and English is the likeliest source for
        # material that has been subtitled into several languages. It only
        # decides which of several wrong-language tracks is used, and
        # `classification` states the language that was actually read either
        # way — so the cost of it being wrong is a translation, never a claim.
        english = language.split("-")[0] == "en"
        # An explicit request outranks the original; with no request, the
        # original outranks everything. Same tuple, two orders — rather than
        # two ranking functions that could drift apart.
        first, second = (exact, original) if wanted else (original, exact)
        return (
            1 if track.is_translation else 0,
            0 if first else 1,
            0 if second else 1,
            0 if english else 1,
            0 if not track.is_generated else 1,
        )

    return sorted(tracks, key=rank)[0]


def _transcript_path(store_dir: Path, digest: str, hint: str) -> Path:
    stem = re.sub(r"[^a-z0-9]+", "-", hint.lower()).strip("-")[:40].strip("-")
    return Path(store_dir) / (f"{digest}-{stem}.md" if stem else f"{digest}.md")


def render_transcript(cues, recording: Recording, track: CaptionTrack) -> str:
    """The rendition: one markdown file with an explicit `[h:mm:ss]` marker in
    front of every line.

    The marker is the addressing scheme, the same role `[page N of T]` plays
    for a document — it is what lets a search hand back a time that can be fed
    straight to `at=`, and what makes the file readable with `read_file` and
    greppable like anything else on disk.
    """
    meta = json.dumps(
        {
            "converter": CAPTION_VERSION,
            "language": track.language,
            "generated": track.is_generated,
            "source": recording.source,
        },
        ensure_ascii=False,
    )
    lines = [f"{_TRANSCRIPT_HEADER}{meta} -->", f"# {recording.title or recording.source}", ""]
    lines += [f"[{format_time(cue.start)}] {cue.text}" for cue in cues]
    return "\n".join(lines) + "\n"


def load_transcript(
    recording: Recording,
    store_dir: Path | str,
    prefer: str = "en",
    *,
    fetch=None,
) -> Transcript:
    """Fetch a caption track WHOLE, convert once, and keep the rendition.

    Whole, because a caption file is a couple of hundred KB of text and the
    thing it is for is SEARCH — "where do they show the phone" cannot be
    answered from a window, and answering it by transcribing windows in a
    binary search is the design this replaced.

    Keyed on the caption BYTES, so the same video reached by two URLs converts
    once and an edited track becomes a different rendition rather than a stale
    hit. The fetch itself is repeated per session (cheap) precisely so an edit
    is noticed.
    """
    track = pick_track(recording.caption_tracks, prefer, recording.original_language)
    if track is None:
        raise RecordingError(
            "this recording publishes no captions, so there are no words to "
            "search. Look at frames instead, or say that the words are not "
            "available — do NOT substitute a transcript from anywhere else."
        )
    fetch = fetch or _fetch_captions
    try:
        data = fetch(track.url)
    except RecordingError:
        raise
    except urllib.error.HTTPError as exc:
        # 429 is routine: the caption endpoint rate-limits, and a raw traceback
        # here reads as "this video has no words" — the substitution failure
        # again. Name it, and say it is temporary, so the model waits or looks
        # instead of inventing a transcript.
        if exc.code == 429:
            raise RecordingError(
                "the caption server is rate-limiting us right now (HTTP 429). "
                "The words are temporarily unavailable — this is NOT a "
                "recording without captions. Look at frames, or try again "
                "shortly; do not substitute a transcript from elsewhere."
            ) from exc
        raise RecordingError(
            f"the {track.language} caption track could not be fetched "
            f"(HTTP {exc.code}). Treat the words as unavailable."
        ) from exc
    except (OSError, web.BlockedURLError) as exc:
        raise RecordingError(
            f"the {track.language} caption track could not be fetched ({exc}). "
            "Treat the words as unavailable, not as absent."
        ) from exc
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    cues = parse_cues(text)
    if not cues:
        raise RecordingError(
            f"the {track.language} caption track downloaded but holds no cues — "
            "treat this recording as having no words, not as having said nothing."
        )
    store = Path(store_dir)
    store.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{CAPTION_VERSION}:{text}".encode()).hexdigest()[:16]
    path = _transcript_path(store, digest, recording.title or recording.identity)
    wanted = (prefer or "").lower()
    matching = [
        t for t in recording.caption_tracks
        if wanted and t.language.lower().split("-")[0] == wanted.split("-")[0]
    ]
    transcript = Transcript(
        asked_only_translated=bool(matching) and all(t.is_translation for t in matching)
        and not track.is_translation,
        path=path,
        language=track.language,
        is_generated=track.is_generated,
        is_translation=track.is_translation,
        original_language=recording.original_language,
        asked_for=prefer,
        cues=tuple(cues),
        duration=recording.duration,
    )
    if path.exists():
        path.touch()  # an LRU store: recency is what eviction reads
    else:
        path.write_text(render_transcript(cues, recording, track), encoding="utf-8")
        prune_transcripts(store)
    # Unconditional, and stated here rather than by the caller, because there
    # is nothing to decide: a caption track is fetched over the network and
    # written by whoever uploaded the recording, so this rendition is outside
    # content however it is reached (#319). Rewritten on a cache hit too — a
    # rendition still on disk whose record was lost would otherwise read as
    # unattributed forever.
    provenance.record_artefact(
        path,
        provenance.ArtefactSource(
            tool="read_media",
            outside=True,
            source=recording.source or recording.identity,
            what="aish's rendition of a caption track published with the recording",
        ),
    )
    return transcript


def _fetch_captions(url: str) -> bytes:
    """The one outbound edge for captions, through the SSRF guard and the
    shared trust store — never yt-dlp's own downloader, which has neither."""
    data, _content_type = web.fetch_binary(url, CAPTION_MAX_BYTES)
    return data


def prune_transcripts(store_dir: Path) -> list[Path]:
    """Evict least-recently-used renditions until under both caps.

    A provenance record is part of its rendition and never an entry of its own
    (#319, #314's lesson): counting one would halve the store's real capacity,
    and evicting one alone would leave bytes on disk that nothing attributes."""
    try:
        files = [
            p
            for p in Path(store_dir).iterdir()
            if p.is_file() and not provenance.is_record(p)
        ]
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
        under_count = len(entries) - len(removed) <= CAPTION_STORE_MAX_FILES
        if under_count and total <= CAPTION_STORE_MAX_BYTES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        provenance.forget_artefact(path)
        total -= size
        removed.append(path)
    return removed


def classification(transcript: Transcript) -> str:
    """What these words ARE, computed — not what the publisher says they are.

    Everything here is measured from the track itself, because the failure this
    prevents is a caption file that reads perfectly and is not this recording's
    speech. What CANNOT be measured — a mistranscription, a sub-second desync —
    is claimed rather than implied, which is why the last line says unverified
    instead of saying nothing.
    """
    origin = "auto-generated by machine" if transcript.is_generated else "published by the uploader"
    parts = [f"Words: {transcript.language}, {origin}, {len(transcript.cues)} lines."]
    spoken = transcript.original_language or ""
    differs = spoken and transcript.language.lower().split("-")[0] != spoken.lower().split("-")[0]
    if transcript.is_translation:
        parts.append(
            f"THESE ARE MACHINE-TRANSLATED from {spoken or 'another language'} — "
            f"nobody in this recording said these words in {transcript.language}. "
            "Do NOT quote them as anybody's speech; use them to find WHERE "
            "something is said and describe it in your own words."
        )
    elif differs:
        # A publisher-authored subtitle track in another language is a HUMAN
        # translation: better than a machine's, and still not what was said.
        # The distinction matters for quoting, so it is stated separately
        # rather than folded into the machine-translation warning.
        parts.append(
            f"This recording is spoken in {spoken}; these are its {transcript.language} "
            "subtitles, so they are a translation rather than the words as "
            "spoken. Quote them as the published subtitles, not as speech."
        )
    asked = (transcript.asked_for or "").lower()
    if transcript.asked_only_translated:
        parts.append(
            f"You asked for {transcript.asked_for}, and the only {transcript.asked_for} "
            f"track is a MACHINE TRANSLATION of the {transcript.language} speech — so "
            "the original was read instead. Translate it yourself if the user "
            "needs it in another language; you will do that better than the "
            "caption pipeline did."
        )
    elif asked and transcript.language.lower().split("-")[0] != asked:
        parts.append(
            f"NOT the language asked for ({transcript.asked_for}) — say which "
            "language you are quoting, and do not present a translation as the "
            "speaker's own words."
        )
    elif spoken and not differs:
        # Worth stating positively: this is the recording's own language, so
        # nothing has been through a translation before reaching you. Translate
        # at the END if the user needs it in another language — the meaning is
        # yours to carry across, not a caption pipeline's.
        parts.append(f"This is the language the recording is spoken in ({spoken}).")
    if transcript.duration:
        percent = round(transcript.coverage * 100)
        gap_start, gap_length = transcript.largest_gap
        parts.append(f"Covers {percent}% of the running time.")
        if transcript.coverage < COVERAGE_FLOOR:
            parts.append(
                "That is LESS THAN HALF: most of this recording has no words "
                "against it, so absence of a phrase here is NOT evidence it was "
                "never said. Look at frames for the rest."
            )
        if gap_length >= 60:
            parts.append(
                f"Longest stretch with no words: {format_time(gap_length)} from "
                f"{format_time(gap_start)}."
            )
        drift = transcript.duration - transcript.last_cue_end
        if drift > 120:
            parts.append(
                f"The last line is at {format_time(transcript.last_cue_end)} but the "
                f"recording runs to {format_time(transcript.duration)} — these captions "
                "may belong to a different edit of it."
            )
    parts.append(
        "Nothing has checked these words against the audio; treat them as the "
        "published caption track, not as verified speech."
    )
    return " ".join(parts)


def search_transcript(transcript: Transcript, query: str, limit: int = 12) -> list[Cue]:
    """Cues containing `query`, case-insensitively. The index's whole job.

    Returns CUES — times — and never an answer: the point is to hand back
    somewhere to look, which the caller turns into `at=`.
    """
    needle = query.strip().lower()
    if not needle:
        raise RecordingError("search needs something to look for")
    return [cue for cue in transcript.cues if needle in cue.text.lower()][:limit]


def window(transcript: Transcript, start: float, end: float) -> list[Cue]:
    """Cues overlapping [start, end)."""
    return [cue for cue in transcript.cues if cue.end > start and cue.start < end]


# What is being said beside a frame. Capped, because this rides EVERY frame in
# a stepped call and an uncapped quote would cost more than the picture does.
SPOKEN_AT_SLACK = 4.0
SPOKEN_AT_MAX_CHARS = 240


def spoken_at(transcript: Transcript, seconds: float, slack: float = SPOKEN_AT_SLACK) -> str:
    """What is being said at a moment, for pinning beside a frame."""
    cues = window(transcript, seconds - slack, seconds + slack)
    said = " ".join(cue.text for cue in cues).strip()
    if len(said) > SPOKEN_AT_MAX_CHARS:
        said = said[:SPOKEN_AT_MAX_CHARS].rsplit(" ", 1)[0] + " …"
    return said


def to_json(recording: Recording) -> str:
    """Stable serialization, for the session cache and for tests."""
    return json.dumps(
        {
            "identity": recording.identity,
            "title": recording.title,
            "duration": recording.duration,
            "chapters": [[c.start, c.title] for c in recording.chapters],
        },
        ensure_ascii=False,
    )
