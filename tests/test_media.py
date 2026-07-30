"""The media store (#188): content-addressed image files an answer can display.

No network and no model — the store is pure filesystem, and the fetching half is
tested through the agent with web.fetch_binary stubbed (tests/test_agent.py).
"""

from pathlib import Path

import pytest

from aish import media

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 40


class TestSniff:
    @pytest.mark.parametrize(
        "data,expected",
        [(PNG, ".png"), (JPEG, ".jpg"), (GIF, ".gif"), (WEBP, ".webp")],
    )
    def test_recognizes_every_displayable_format(self, data, expected):
        assert media.sniff(data) == expected

    def test_html_served_as_an_image_is_refused(self):
        """The failure this exists for: a WAF challenge or the page the picture
        sits on, delivered under a .jpg URL. The extension agrees; the bytes do
        not, and only the bytes are consulted."""
        assert media.sniff(b"<!DOCTYPE html><html><body>Access denied") is None

    def test_empty_and_truncated_bytes_are_refused(self):
        assert media.sniff(b"") is None
        assert media.sniff(b"\x89PN") is None

    def test_svg_is_not_displayable(self):
        # Deliberate: opened full-size an SVG executes scripts in our origin.
        assert media.sniff(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None

    def test_riff_that_is_not_webp_is_refused(self):
        assert media.sniff(b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 40) is None


class TestStore:
    def test_stores_with_a_readable_name_and_right_suffix(self, tmp_path):
        path = media.store(PNG, tmp_path, "iPhone Fold, open and closed")
        assert path.read_bytes() == PNG
        assert path.suffix == ".png"
        assert "iphone-fold-open-and-closed" in path.name
        assert path.parent == tmp_path

    def test_creates_the_directory(self, tmp_path):
        target = tmp_path / "does" / "not" / "exist"
        assert media.store(PNG, target, "x").is_file()

    def test_same_bytes_stored_twice_is_one_file(self, tmp_path):
        """Content-addressed, so a retry costs nothing and the store cannot fill
        with duplicates of the same picture."""
        first = media.store(JPEG, tmp_path, "phone")
        second = media.store(JPEG, tmp_path, "phone")
        assert first == second
        assert len(list(tmp_path.iterdir())) == 1

    def test_different_bytes_are_different_files(self, tmp_path):
        a = media.store(JPEG, tmp_path, "phone")
        b = media.store(JPEG + b"more", tmp_path, "phone")
        assert a != b
        assert len(list(tmp_path.iterdir())) == 2

    def test_caption_cannot_produce_a_path_markdown_cannot_parse(self, tmp_path):
        """The markdown image regex stops at whitespace and ')' — a caption is
        slugged to [a-z0-9-] so it can never break the link it ends up in."""
        path = media.store(PNG, tmp_path, "a (weird) name: with spaces & ]brackets[")
        assert " " not in path.name
        assert not set(path.name) & set(")(][&:")

    def test_a_caption_with_nothing_usable_still_stores(self, tmp_path):
        path = media.store(PNG, tmp_path, "。。。")
        assert path.is_file() and path.suffix == ".png"

    def test_non_image_bytes_refused_before_anything_is_written(self, tmp_path):
        with pytest.raises(ValueError):
            media.store(b"<html>nope</html>", tmp_path, "x")
        assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


class TestPrune:
    def test_evicts_least_recently_used_over_the_file_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "MEDIA_MAX_FILES", 3)
        stored = []
        for i in range(6):
            path = media.store(PNG + bytes([i]), tmp_path, f"pic{i}")
            # Deterministic recency: oldest first, newest last.
            import os

            os.utime(path, (1000 + i, 1000 + i))
            stored.append(path)
        media.prune(tmp_path)
        survivors = {p.name for p in tmp_path.iterdir()}
        assert survivors == {p.name for p in stored[-3:]}

    def test_evicts_over_the_byte_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "MEDIA_MAX_BYTES", len(PNG) * 2)
        for i in range(5):
            media.store(PNG + bytes([i]) * 1, tmp_path, f"pic{i}")
        total = sum(p.stat().st_size for p in tmp_path.iterdir())
        assert total <= media.MEDIA_MAX_BYTES

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        assert media.prune(tmp_path / "nope") == []

    def test_a_repeat_store_refreshes_recency(self, tmp_path, monkeypatch):
        """Re-showing the same picture must keep it alive, or the LRU would
        evict exactly the images still in use."""
        monkeypatch.setattr(media, "MEDIA_MAX_FILES", 2)
        import os

        old = media.store(PNG, tmp_path, "keep")
        os.utime(old, (1000, 1000))
        for i in range(2):
            newer = media.store(JPEG + bytes([i]), tmp_path, f"other{i}")
            os.utime(newer, (2000 + i, 2000 + i))
        media.store(PNG, tmp_path, "keep")  # touches `old`
        media.prune(tmp_path)
        assert old.exists()


def test_image_cap_matches_what_the_web_ui_will_inline():
    """A picture the store accepts but the UI refuses to inline would be a
    silent failure of exactly the kind #188 exists to remove."""
    from aish import server

    assert media.IMAGE_MAX_BYTES == server.MEDIA_MAX_BYTES


def test_store_formats_match_every_renderer():
    """The store, the /file endpoint, and the terminal renderer must agree on
    which formats exist — a mismatch is a picture that stores and then fails to
    display somewhere."""
    from aish import backends, server

    suffixes = {suffix for _, suffix in media._MAGIC} | {".webp"}
    assert suffixes == set(server.IMAGE_TYPES) - {".jpeg"}
    assert suffixes <= set(backends.IMAGE_SUFFIXES)
    assert Path("x.svg").suffix not in suffixes
