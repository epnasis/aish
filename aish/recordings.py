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
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import web

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
    title: str = ""
    uploader: str = ""
    description: str = ""
    duration: float = 0.0
    is_live: bool = False
    has_video: bool = True
    chapters: tuple[Chapter, ...] = ()
    caption_languages: tuple[str, ...] = ()
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
    """
    lowered = message.lower()
    if "sign in" in lowered or "age" in lowered and "confirm" in lowered:
        return (
            "this video is age-restricted or needs a sign-in, so its frames "
            f"cannot be read. Tell the user that. ({message.strip()[:200]})"
        )
    if "private" in lowered or "members-only" in lowered:
        return f"this video is private or members-only. ({message.strip()[:200]})"
    if "not available in your country" in lowered or "geo" in lowered:
        return f"this video is blocked in this region. ({message.strip()[:200]})"
    if "drm" in lowered:
        return f"this video is DRM-protected and cannot be read. ({message.strip()[:200]})"
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
    captions = tuple(
        sorted({*(info.get("subtitles") or {}), *(info.get("automatic_captions") or {})})
    )
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
        duration=float(info.get("duration") or 0.0),
        is_live=bool(info.get("is_live")),
        has_video=(
            bool(info.get("vcodec") and info.get("vcodec") != "none")
            or bool(info.get("height"))
        ),
        chapters=chapters,
        caption_languages=captions,
        expires_at=_url_expiry(media_url),
    )


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
        return (
            "the stream URL has expired (HTTP 403). Call read_media again for "
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

    if recording.caption_languages:
        parts.append(
            "Captions exist in: "
            + ", ".join(recording.caption_languages[:12])
            + ". (Reading them is not built yet — this tool returns pictures.)"
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
