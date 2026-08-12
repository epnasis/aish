"""Reading a recording: probing, seeking, and the guards (#216).

Both effectful edges are parameter seams, so nothing here touches the network
or spawns ffmpeg — the same rule as every other suite in this repo.
"""

import pytest

from aish import recordings
from aish.recordings import Chapter, Recording, RecordingError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40

# One frame's worth of ffmpeg stderr: the showinfo line is what carries the
# truth about WHERE the frame came from.
STDERR_OK = (
    "Input #0, mov,mp4\n  Duration: 01:53:23.45, start: 0.000000\n"
    "[Parsed_showinfo_1 @ 0x7f] n:0 pts:64801 pts_time:2700.03 duration:1\n"
)


def fake_run(stderr=STDERR_OK, code=0, payload=PNG):
    """An ffmpeg that writes `payload` to the -y path and reports `stderr`."""
    calls: list[list[str]] = []

    def run(args):
        calls.append(args)
        if payload is not None and "-y" in args:
            out = args[args.index("-y") + 1]
            with open(out, "wb") as handle:
                handle.write(payload)
        return code, stderr

    run.calls = calls  # type: ignore[attr-defined]
    return run


def remote(**over) -> Recording:
    base = dict(
        source="https://youtube.com/watch?v=abc",
        identity="youtube:abc",
        media_url="https://rr2.googlevideo.com/videoplayback?expire=99",
        is_local=False,
        title="Google Keynote",
        duration=6763.0,
    )
    base.update(over)
    return Recording(**base)  # type: ignore[arg-type]


class TestTimeParsing:
    """The model writes times four different ways and an answer cites whatever
    comes back, so guessing wrong about which one it meant puts a frame
    somewhere nobody asked about."""

    @pytest.mark.parametrize(
        "text,seconds",
        [
            ("90", 90.0),
            ("90s", 90.0),
            ("2m", 120.0),
            ("1h", 3600.0),
            ("1:30", 90.0),
            ("12:34", 754.0),
            ("1:02:03", 3723.0),
            ("0:05.5", 5.5),
        ],
    )
    def test_accepted_forms(self, text, seconds):
        assert recordings.parse_time(text) == seconds

    def test_nonsense_names_the_forms_it_takes(self):
        with pytest.raises(RecordingError, match="1:02:03"):
            recordings.parse_time("halfway through")

    def test_format_round_trips_past_an_hour(self):
        assert recordings.format_time(3723) == "1:02:03"
        assert recordings.format_time(754) == "12:34"


class TestFrameTimestamps:
    """The addressing scheme the design rests on. A seek lands on the nearest
    frame the container allows, so what is REPORTED must be what was decoded —
    an answer citing 12:34 for a picture taken at 12:31 is the page number
    lying, which is the one thing this may not do."""

    def test_the_decoded_time_is_returned_not_the_requested_one(self):
        data, actual = recordings.frame(remote(), 2699.0, run=fake_run())
        assert data == PNG
        assert actual == 2700.03  # from showinfo, not the 2699 asked for

    def test_a_missing_showinfo_line_falls_back_to_the_request(self):
        """No invented precision: with nothing decoded to report, the requested
        time is all there is, and it is not dressed up as measured."""
        _, actual = recordings.frame(remote(), 42.0, run=fake_run(stderr="Duration: 00:10:00.0"))
        assert actual == 42.0

    def test_input_seek_is_used_so_a_long_video_costs_one_range_request(self):
        run = fake_run()
        recordings.frame(remote(), 2700.0, run=run)
        args = run.calls[0]
        # -ss BEFORE -i is the seek; after -i it would decode from the start.
        assert args.index("-ss") < args.index("-i")
        assert "-copyts" in args  # or showinfo reports output-relative time


class TestFrameRefusals:
    """An honest dead end beats a fluent hallucination: each of these says what
    is wrong instead of returning a picture from somewhere else."""

    def test_past_the_end_names_the_length(self):
        with pytest.raises(RecordingError, match="1:52:43 long"):
            recordings.frame(remote(), 9999.0, run=fake_run())

    def test_a_live_stream_is_refused_because_it_has_no_timeline(self):
        with pytest.raises(RecordingError, match="live stream"):
            recordings.frame(remote(is_live=True), 5.0, run=fake_run())

    def test_audio_only_says_there_are_no_pictures(self):
        with pytest.raises(RecordingError, match="no video track"):
            recordings.frame(remote(has_video=False), 5.0, run=fake_run())

    def test_an_expired_url_says_how_to_recover_rather_than_dying(self):
        run = fake_run(stderr="HTTP error 403 Forbidden", code=1, payload=None)
        with pytest.raises(RecordingError, match="expired"):
            recordings.frame(remote(), 10.0, run=run)

    def test_a_negative_time_is_refused(self):
        with pytest.raises(RecordingError, match="negative"):
            recordings.frame(remote(), -1.0, run=fake_run())


class TestGuards:
    """This hands a URL to somebody else's network stack — yt-dlp's and
    ffmpeg's — so the guards have to travel with the URL rather than live at
    the tool boundary."""

    def test_the_source_url_passes_the_ssrf_guard_before_yt_dlp_sees_it(self, monkeypatch):
        seen = []
        monkeypatch.setattr(recordings.web, "require_public", lambda url: seen.append(url))
        recordings.probe(
            "https://ex.com/v",
            extract=lambda url: {"id": "x", "url": "https://cdn.ex.com/s.mp4", "duration": 10},
        )
        assert seen[0] == "https://ex.com/v"

    def test_a_blocked_source_never_reaches_the_extractor(self, monkeypatch):
        def blocked(url):
            raise recordings.web.BlockedURLError("resolves to non-public address 127.0.0.1")

        monkeypatch.setattr(recordings.web, "require_public", blocked)
        called = []
        with pytest.raises(RecordingError, match="blocked"):
            recordings.probe("http://localhost/v", extract=lambda url: called.append(url) or {})
        assert not called

    def test_the_RESOLVED_stream_is_guarded_too(self, monkeypatch):
        """The gate at the tool boundary vouches for the host the MODEL named.
        Where that host's stream then points is a second question, and it is
        the one an open redirect answers."""
        checked = []

        def guard(url):
            checked.append(url)
            if "169.254" in url:
                raise recordings.web.BlockedURLError("non-public address")

        monkeypatch.setattr(recordings.web, "require_public", guard)
        with pytest.raises(RecordingError, match="resolved stream is blocked"):
            recordings.probe(
                "https://ex.com/v",
                extract=lambda url: {"id": "x", "url": "http://169.254.169.254/latest"},
            )
        assert len(checked) == 2

    def test_ffmpeg_runs_under_a_protocol_whitelist(self):
        """ffmpeg speaks file:, concat: and playlist redirection natively.
        Without this a media URL that redirects into file:///etc/passwd turns
        "read a podcast" into an arbitrary file read."""
        run = fake_run()
        recordings.frame(remote(), 10.0, run=run)
        args = run.calls[0]
        whitelist = args[args.index("-protocol_whitelist") + 1]
        assert "file" not in whitelist
        assert whitelist == recordings.REMOTE_PROTOCOLS

    def test_a_local_file_may_use_file_and_nothing_else(self, tmp_path):
        clip = tmp_path / "memo.mp4"
        clip.write_bytes(b"x")
        run = fake_run()
        recordings.frame(
            Recording(
                source=str(clip),
                identity="local:1",
                media_url=str(clip),
                is_local=True,
                duration=60,
            ),
            5.0,
            run=run,
        )
        args = run.calls[0]
        assert args[args.index("-protocol_whitelist") + 1] == "file"


class TestProbe:
    def test_a_local_file_reads_its_duration_from_ffmpeg(self, tmp_path):
        clip = tmp_path / "memo.mp4"
        clip.write_bytes(b"x" * 10)
        run = fake_run(stderr="  Duration: 00:02:03.50, start: 0.0\n  Stream #0:0 Video: h264\n")
        recording = recordings.probe(str(clip), run=run)
        assert recording.duration == pytest.approx(123.5)
        assert recording.is_local and recording.has_video

    def test_a_file_ffmpeg_cannot_read_says_so(self, tmp_path):
        junk = tmp_path / "notes.txt"
        junk.write_text("hello")
        run = fake_run(stderr="Invalid data found when processing input", code=1)
        with pytest.raises(RecordingError, match="not a video or audio file"):
            recordings.probe(str(junk), run=run)

    def test_a_missing_path_is_not_mistaken_for_a_url(self):
        with pytest.raises(RecordingError, match="not a file on this machine"):
            recordings.probe("/tmp/nope.mp4")

    def test_chapters_and_captions_come_off_the_metadata(self, monkeypatch):
        monkeypatch.setattr(recordings.web, "require_public", lambda url: None)
        recording = recordings.probe(
            "https://ex.com/v",
            extract=lambda url: {
                "id": "abc",
                "extractor_key": "Youtube",
                "url": "https://cdn/s.mp4",
                "duration": 6763,
                "title": "Keynote",
                "chapters": [
                    {"start_time": 0, "title": "Opening"},
                    {"start_time": 1257, "title": "DeepMind"},
                ],
                "subtitles": {"en": [{}]},
                "automatic_captions": {"pl": [{}]},
                "height": 720,
            },
        )
        assert recording.identity == "youtube:abc"
        assert recording.chapters == (Chapter(0.0, "Opening"), Chapter(1257.0, "DeepMind"))
        assert recording.caption_languages == ("en", "pl")

    def test_the_signed_urls_own_expiry_is_read(self, monkeypatch):
        monkeypatch.setattr(recordings.web, "require_public", lambda url: None)
        recording = recordings.probe(
            "https://ex.com/v",
            extract=lambda url: {"id": "a", "url": "https://cdn/s.mp4?expire=1786497670"},
        )
        assert recording.expires_at == 1786497670.0

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("Sign in to confirm your age", "age-restricted"),
            ("This video is private", "private"),
            ("No supported JavaScript runtime could be found", "brew install deno"),
            ("Video unavailable due to DRM", "DRM-protected"),
        ],
    )
    def test_an_extraction_failure_says_what_to_DO(self, message, expected, monkeypatch):
        """A bare extractor traceback reads as 'this source is unavailable' and
        the model goes looking elsewhere — the substitution failure this repo
        already has a scar from. Each of these implies a different action."""
        monkeypatch.setattr(recordings.web, "require_public", lambda url: None)

        def boom(url):
            raise RuntimeError(message)

        with pytest.raises(RecordingError, match=expected):
            recordings.probe("https://ex.com/v", extract=boom)


class TestTrackingParams:
    """A share link carries a token identifying whoever shared it. It must not
    be forwarded to the host, must not make one video look like two, and must
    not be what gets quoted back into an answer."""

    def test_a_share_token_never_reaches_the_extractor(self, monkeypatch):
        monkeypatch.setattr(recordings.web, "require_public", lambda url: None)
        seen = []
        recordings.probe(
            "https://youtube.com/shorts/ORziFM6lseY?si=vyM8z4tmg27lRix4",
            extract=lambda url: (seen.append(url), {"id": "a", "url": "https://cdn/s.mp4"})[1],
        )
        assert seen == ["https://youtube.com/shorts/ORziFM6lseY"]

    def test_the_meaningful_parameters_survive(self, monkeypatch):
        """A denylist, because an unrecognized parameter may be load-bearing —
        `v` is the video and `t` is where to start."""
        monkeypatch.setattr(recordings.web, "require_public", lambda url: None)
        seen = []
        recordings.probe(
            "https://www.youtube.com/watch?v=abc&t=90&si=xyz&utm_source=news&list=PL1",
            extract=lambda url: (seen.append(url), {"id": "a", "url": "https://cdn/s.mp4"})[1],
        )
        assert seen == ["https://www.youtube.com/watch?v=abc&t=90&list=PL1"]

    def test_a_url_with_no_query_is_untouched(self):
        assert (
            recordings.web.strip_tracking("https://youtu.be/ORziFM6lseY")
            == "https://youtu.be/ORziFM6lseY"
        )

    def test_a_signed_stream_url_is_never_stripped(self, monkeypatch):
        """It is nothing BUT opaque parameters; dropping one turns a working
        link into a 403. Stripping applies to what was GIVEN, never to what was
        resolved."""
        monkeypatch.setattr(recordings.web, "require_public", lambda url: None)
        signed = "https://cdn/s.mp4?expire=99&sig=abc&si=notatrackingtoken"
        recording = recordings.probe(
            "https://ex.com/v", extract=lambda url: {"id": "a", "url": signed}
        )
        assert recording.media_url == signed


class TestSummary:
    """The structural map, emitted first and always. What is ABSENT has to be
    as visible as what is present, or a caller reads silence as completeness."""

    def test_absent_chapters_are_stated_not_omitted(self):
        assert "Chapters: none published." in recordings.summary(remote())

    def test_absent_captions_say_there_are_no_words(self):
        assert "no words to read" in recordings.summary(remote())

    def test_chapters_are_labelled_as_the_uploaders_claim(self):
        """They are unverified third-party text. Presenting them as though the
        recording had been checked against them is the hollow-extraction
        failure wearing a classification header's costume."""
        text = recordings.summary(remote(chapters=(Chapter(0.0, "Intro"),)))
        assert "PUBLISHED BY THE UPLOADER" in text
        assert "1. 0:00 Intro" in text

    def test_a_live_stream_says_it_has_no_timeline(self):
        assert "LIVE, no fixed timeline" in recordings.summary(remote(is_live=True))

    def test_audio_only_is_declared_up_front(self):
        assert "AUDIO ONLY" in recordings.summary(remote(has_video=False))

    def test_the_uploaders_description_is_offered_because_it_names_people(self):
        """A music video's description routinely names who is in it, which
        answers "who is that" more reliably than a description of a face — and
        it is already in hand."""
        text = recordings.describe(remote(description="Cover by A, B and C"))
        assert "Cover by A, B and C" in text
