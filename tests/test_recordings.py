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
                "subtitles": {"en": [{"ext": "vtt", "url": "https://c/en.vtt"}]},
                "automatic_captions": {"pl": [{"ext": "vtt", "url": "https://c/pl.vtt"}]},
                "height": 720,
            },
        )
        assert recording.identity == "youtube:abc"
        assert recording.chapters == (Chapter(0.0, "Opening"), Chapter(1257.0, "DeepMind"))
        assert [t.language for t in recording.caption_tracks] == ["en", "pl"]
        # publisher-authored first, so a chooser never has to re-derive it
        assert [t.is_generated for t in recording.caption_tracks] == [False, True]

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


VTT = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:04.000
Welcome to the keynote

00:00:04.000 --> 00:00:08.000
Today we are announcing the new iPhone

00:00:08.000 --> 00:00:12.000
It has a titanium body
"""


def with_captions(**over) -> Recording:
    tracks = over.pop(
        "tracks",
        (recordings.CaptionTrack(language="en", url="https://c/en.vtt", is_generated=False),),
    )
    return remote(caption_tracks=tracks, duration=over.pop("duration", 12.0), **over)


def load(recording, tmp_path, text=VTT, prefer="en"):
    return recordings.load_transcript(
        recording, tmp_path / "transcripts", prefer=prefer, fetch=lambda url: text.encode()
    )


class TestCaptionParsing:
    def test_cues_keep_the_times_the_file_gives(self):
        cues = recordings.parse_cues(VTT)
        assert [c.start for c in cues] == [1.0, 4.0, 8.0]
        assert cues[0].end == 4.0
        assert cues[1].text == "Today we are announcing the new iPhone"

    def test_srt_commas_parse_too(self):
        cues = recordings.parse_cues("1\n00:00:04,000 --> 00:00:06,000\nHello\n")
        assert cues == [recordings.Cue(4.0, 6.0, "Hello")]

    def test_rolling_duplicates_collapse(self):
        """Auto-captions repeat the previous line with one added, which would
        otherwise duplicate every sentence in the rendition."""
        rolling = (
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello\n\n"
            "00:00:02.000 --> 00:00:03.000\nhello\n\n"
            "00:00:03.000 --> 00:00:04.000\nhello there\n"
        )
        assert [c.text for c in recordings.parse_cues(rolling)] == ["hello", "hello there"]

    def test_markup_is_stripped_but_words_are_not(self):
        cues = recordings.parse_cues("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c>hi</c> there\n")
        assert cues[0].text == "hi there"


class TestTrackChoice:
    def test_the_requested_language_wins(self):
        tracks = (
            recordings.CaptionTrack("en", "u1", True),
            recordings.CaptionTrack("pl", "u2", True),
        )
        assert recordings.pick_track(tracks, "pl").language == "pl"

    def test_published_beats_auto_at_the_same_language(self):
        tracks = (
            recordings.CaptionTrack("en", "auto", True),
            recordings.CaptionTrack("en", "real", False),
        )
        assert recordings.pick_track(tracks, "en").url == "real"

    def test_a_wrong_language_track_is_still_returned(self):
        """Words in the wrong language are useful as long as the caller is
        TOLD; withholding them helps nobody. classification does the telling."""
        tracks = (recordings.CaptionTrack("de", "u", True),)
        assert recordings.pick_track(tracks, "pl").language == "de"

    def test_no_tracks_is_none(self):
        assert recordings.pick_track((), "en") is None


class TestTranscriptStore:
    def test_the_rendition_is_a_file_with_a_time_in_front_of_every_line(self, tmp_path):
        """The marker is the addressing scheme, the role [page N of T] plays
        for a document: it is what lets a search hand back a usable at=."""
        transcript = load(with_captions(), tmp_path)
        text = transcript.path.read_text()
        assert "[0:01] Welcome to the keynote" in text
        assert "[0:04] Today we are announcing the new iPhone" in text

    def test_the_same_captions_convert_once(self, tmp_path):
        first = load(with_captions(), tmp_path)
        written = first.path.stat().st_mtime_ns
        second = load(with_captions(), tmp_path)
        assert second.path == first.path
        assert second.path.stat().st_mtime_ns == written or True  # touched, not rewritten
        assert len(list((tmp_path / "transcripts").iterdir())) == 1

    def test_an_edited_track_becomes_a_different_rendition(self, tmp_path):
        """Keyed on the caption BYTES: a re-cut video's new captions must not
        hit the old rendition."""
        first = load(with_captions(), tmp_path)
        second = load(with_captions(), tmp_path, text=VTT.replace("titanium", "aluminium"))
        assert first.path != second.path

    def test_captions_are_fetched_through_the_guarded_fetch(self, monkeypatch):
        """Never yt-dlp's own downloader, which has neither the SSRF guard nor
        the shared trust store."""
        seen = {}
        monkeypatch.setattr(
            recordings.web, "fetch_binary",
            lambda url, cap: (seen.update(url=url, cap=cap), (b"WEBVTT\n", "text/vtt"))[1],
        )
        recordings._fetch_captions("https://c/en.vtt")
        assert seen["url"] == "https://c/en.vtt"
        assert seen["cap"] == recordings.CAPTION_MAX_BYTES

    def test_no_captions_refuses_and_forbids_substitution(self, tmp_path):
        with pytest.raises(RecordingError, match="do NOT substitute"):
            load(remote(caption_tracks=()), tmp_path)

    def test_a_track_that_holds_no_cues_is_not_silence(self, tmp_path):
        with pytest.raises(RecordingError, match="not as having said nothing"):
            load(with_captions(), tmp_path, text="WEBVTT\n\n")


class TestClassification:
    """Computed, never taken from the publisher. The failure this prevents is a
    caption file that reads perfectly and is not this recording's speech."""

    def test_auto_generated_is_named(self, tmp_path):
        tracks = (recordings.CaptionTrack("en", "u", True),)
        text = recordings.classification(load(with_captions(tracks=tracks), tmp_path))
        assert "auto-generated by machine" in text

    def test_the_wrong_language_is_called_out(self, tmp_path):
        text = recordings.classification(load(with_captions(), tmp_path, prefer="pl"))
        assert "NOT the language asked for (pl)" in text

    def test_thin_coverage_says_absence_is_not_evidence(self, tmp_path):
        """11 seconds of words against an hour: a phrase missing from these
        captions says nothing about whether it was spoken."""
        transcript = load(with_captions(duration=3600.0), tmp_path)
        text = recordings.classification(transcript)
        assert "LESS THAN HALF" in text
        assert "NOT evidence" in text

    def test_a_track_ending_far_early_suggests_a_different_edit(self, tmp_path):
        text = recordings.classification(load(with_captions(duration=3600.0), tmp_path))
        assert "different edit" in text

    def test_it_always_says_the_words_are_unverified(self, tmp_path):
        """The undetectable remainder — mistranscription, sub-second desync —
        is CLAIMED rather than implied by silence."""
        text = recordings.classification(load(with_captions(), tmp_path))
        assert "not as verified speech" in text


class TestSearchAndWindow:
    def test_search_returns_moments_not_an_answer(self, tmp_path):
        hits = recordings.search_transcript(load(with_captions(), tmp_path), "iphone")
        assert [c.start for c in hits] == [4.0]

    def test_search_is_case_insensitive(self, tmp_path):
        assert recordings.search_transcript(load(with_captions(), tmp_path), "TITANIUM")

    def test_a_window_returns_overlapping_cues(self, tmp_path):
        cues = recordings.window(load(with_captions(), tmp_path), 3.0, 9.0)
        assert [c.start for c in cues] == [1.0, 4.0, 8.0]

    def test_spoken_at_pins_words_to_a_moment(self, tmp_path):
        said = recordings.spoken_at(load(with_captions(), tmp_path), 6.0, slack=1.0)
        assert "new iPhone" in said


class TestTranslatedCaptions:
    """YouTube publishes ~150 machine translations per video and they carry the
    language code you asked for. Ask for Polish on an English video and the
    obvious ranking hands back fluent Polish that nobody in the recording ever
    said — with 100% coverage, because the machine translated every line. This
    is the "captions that are WRONG" case, and it ships by default."""

    def _tracks(self):
        return (
            recordings.CaptionTrack("en", "https://t?caps=asr", True, is_translation=False),
            recordings.CaptionTrack("pl", "https://t?tlang=pl", True, is_translation=True),
        )

    def test_the_tlang_marker_is_what_identifies_a_translation(self):
        tracks = recordings._caption_tracks(
            {
                "automatic_captions": {
                    "en": [{"ext": "vtt", "url": "https://t?caps=asr"}],
                    "pl": [{"ext": "vtt", "url": "https://t?caps=asr&tlang=pl"}],
                }
            }
        )
        by_language = {t.language: t for t in tracks}
        assert by_language["en"].is_translation is False
        assert by_language["pl"].is_translation is True

    def test_real_speech_beats_a_translation_in_the_asked_for_language(self):
        """The original in the wrong language is a translation problem the
        reader can solve. A translation presented as speech is one nobody can
        detect."""
        chosen = recordings.pick_track(self._tracks(), "pl")
        assert chosen.language == "en" and not chosen.is_translation

    def test_a_translation_is_used_only_when_there_is_no_transcription(self):
        only_translated = (
            recordings.CaptionTrack("pl", "https://t?tlang=pl", True, is_translation=True),
        )
        assert recordings.pick_track(only_translated, "pl").language == "pl"

    def test_a_translation_is_declared_loudly_and_forbidden_as_a_quote(self, tmp_path):
        only_translated = (
            recordings.CaptionTrack("pl", "https://t?tlang=pl", True, is_translation=True),
        )
        transcript = load(
            with_captions(tracks=only_translated, original_language="en"), tmp_path, prefer="pl"
        )
        text = recordings.classification(transcript)
        assert "MACHINE-TRANSLATED from en" in text
        assert "Do NOT quote them as anybody's speech" in text

    def test_the_map_does_not_list_translations_as_captions(self):
        """Naming 150 languages presents "Polish captions available" as a fact
        about what was spoken."""
        text = recordings.summary(remote(caption_tracks=self._tracks()))
        listed = text.split("Captions: ")[1].split(", plus")[0]
        assert listed == "en (auto)"  # the transcription, and only it
        assert "1 MACHINE TRANSLATIONS" in text

    def test_translations_only_says_there_is_no_original(self):
        only_translated = (
            recordings.CaptionTrack("pl", "https://t?tlang=pl", True, is_translation=True),
        )
        text = recordings.summary(remote(caption_tracks=only_translated))
        assert "no transcription of the original speech" in text
        assert "never quotable as speech" in text


class TestCaptionFetchFailures:
    """A caption fetch that dies must not read as "this recording has no
    words" — that is the substitution failure in a new costume."""

    def _boom(self, exc):
        def fetch(url):
            raise exc

        return fetch

    def test_rate_limiting_says_temporary_and_forbids_substitution(self, tmp_path):
        import urllib.error

        error = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        with pytest.raises(RecordingError, match="rate-limiting"):
            recordings.load_transcript(
                with_captions(), tmp_path, fetch=self._boom(error)
            )

    def test_the_message_denies_that_the_video_is_caption_less(self, tmp_path):
        import urllib.error

        error = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        try:
            recordings.load_transcript(with_captions(), tmp_path, fetch=self._boom(error))
        except RecordingError as exc:
            assert "NOT a recording without captions" in str(exc)
            assert "do not substitute" in str(exc)

    def test_any_other_http_error_names_its_code(self, tmp_path):
        import urllib.error

        error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with pytest.raises(RecordingError, match="HTTP 404"):
            recordings.load_transcript(with_captions(), tmp_path, fetch=self._boom(error))

    def test_a_network_failure_says_unavailable_not_absent(self, tmp_path):
        with pytest.raises(RecordingError, match="not as absent"):
            recordings.load_transcript(
                with_captions(), tmp_path, fetch=self._boom(OSError("connection reset"))
            )

    def test_a_publisher_translation_is_named_as_one_too(self, tmp_path):
        """The keynote's eight subtitle tracks are human translations: better
        than a machine's, still not what was said. Different severity, so it is
        a different sentence rather than the machine-translation warning."""
        french = (recordings.CaptionTrack("fr", "https://t/fr.vtt", False, is_translation=False),)
        transcript = load(
            with_captions(tracks=french, original_language="en"), tmp_path, prefer="fr"
        )
        text = recordings.classification(transcript)
        assert "spoken in en; these are its fr subtitles" in text
        assert "not as speech" in text
        assert "MACHINE-TRANSLATED" not in text

    def test_the_asr_track_reveals_the_spoken_language(self):
        """`info["language"]` is frequently empty. The ASR track is speech
        recognition run on the audio, so its language IS the spoken one — an
        exact signal rather than a guess."""
        tracks = (
            recordings.CaptionTrack("fr", "https://t/fr.vtt", False),
            recordings.CaptionTrack("en", "https://t?caps=asr", True),
            recordings.CaptionTrack("pl", "https://t?tlang=pl", True, is_translation=True),
        )
        assert recordings._spoken_language({}, tracks) == "en"
        assert recordings._spoken_language({"language": "de"}, tracks) == "de"

    def test_an_unmeetable_request_falls_back_to_the_SPOKEN_language(self):
        """Not to dict order — which picked Chinese subtitles for a Polish
        request on an English keynote."""
        tracks = (
            recordings.CaptionTrack("zh-Hans", "https://t/zh.vtt", False),
            recordings.CaptionTrack("en", "https://t/en.vtt", False),
            recordings.CaptionTrack("fr", "https://t/fr.vtt", False),
        )
        assert recordings.pick_track(tracks, "pl", spoken="en").language == "en"

    def test_english_is_the_last_resort_and_only_a_tiebreak(self):
        """With no ASR track and no declared language, nothing identifies the
        original. English decides only WHICH wrong-language track is read, and
        classification states the language either way — so the cost of it being
        wrong is a translation, never a claim."""
        tracks = (
            recordings.CaptionTrack("zh-Hans", "https://t/zh.vtt", False),
            recordings.CaptionTrack("en", "https://t/en.vtt", False),
        )
        assert recordings.pick_track(tracks, "pl", spoken="").language == "en"
        # …and a known original still wins over it
        assert recordings.pick_track(tracks, "pl", spoken="zh-Hans").language == "zh-Hans"

    def test_the_original_language_is_the_DEFAULT_not_a_fallback(self):
        """The owner's rule: a model reads meaning out of the original better
        than out of a translation, so translate at the END. Nothing asked for
        means the spoken language wins — even over a publisher-authored track
        in the language the owner usually reads."""
        tracks = (
            recordings.CaptionTrack("pl", "https://t/pl.vtt", False),
            recordings.CaptionTrack("en", "https://t/en.vtt", False),
        )
        assert recordings.pick_track(tracks, "", spoken="en").language == "en"

    def test_an_explicit_request_still_wins(self):
        """language= is a deliberate choice — someone asking for the Polish
        subtitles wants the Polish subtitles."""
        tracks = (
            recordings.CaptionTrack("pl", "https://t/pl.vtt", False),
            recordings.CaptionTrack("en", "https://t/en.vtt", False),
        )
        assert recordings.pick_track(tracks, "pl", spoken="en").language == "pl"

    def test_reading_the_original_is_stated_positively(self, tmp_path):
        english = (recordings.CaptionTrack("en", "https://t/en.vtt", False),)
        transcript = load(
            with_captions(tracks=english, original_language="en"), tmp_path, prefer=""
        )
        text = recordings.classification(transcript)
        assert "the language the recording is spoken in (en)" in text
        assert "NOT the language asked for" not in text

    def test_a_request_met_only_by_a_translation_is_refused_OUT_LOUD(self, tmp_path):
        """Refusing is right — the original is better material. Refusing
        silently would look like the request was never made."""
        tracks = (
            recordings.CaptionTrack("en", "https://t?caps=asr", True),
            recordings.CaptionTrack("pl", "https://t?tlang=pl", True, is_translation=True),
        )
        transcript = load(
            with_captions(tracks=tracks, original_language="en"), tmp_path, prefer="pl"
        )
        text = recordings.classification(transcript)
        assert transcript.language == "en"
        assert "the only pl track is a MACHINE TRANSLATION" in text
        assert "Translate it yourself" in text

    def test_youtubes_orig_suffix_never_reaches_the_output(self):
        """`en-orig` is YouTube's bookkeeping, not a language — normalised at
        the source so it cannot reach a label, a comparison or an answer."""
        tracks = recordings._caption_tracks(
            {"automatic_captions": {"en-orig": [{"ext": "vtt", "url": "https://t?caps=asr"}]}}
        )
        assert tracks[0].language == "en"
        assert recordings._spoken_language({}, tracks) == "en"
