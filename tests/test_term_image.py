"""Terminal inline images (issue #9): protocol detection, markdown path
extraction scoped to session roots, and the escape sequences themselves.
No real terminal needed — stdout/env are patched."""

import base64
import sys

from aish import term_image


def tty(monkeypatch, yes=True):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: yes, raising=False)


class TestSupportsImages:
    def test_not_a_tty_means_none(self, monkeypatch):
        tty(monkeypatch, yes=False)
        monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
        assert term_image.supports_images() is None

    def test_iterm2_detected(self, monkeypatch):
        tty(monkeypatch)
        monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.delenv("TMUX", raising=False)
        assert term_image.supports_images() == "iterm2"

    def test_kitty_family_detected(self, monkeypatch):
        tty(monkeypatch)
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setenv("TERM_PROGRAM", "")
        monkeypatch.setenv("TERM", "xterm-kitty")
        assert term_image.supports_images() == "kitty"
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.setenv("TERM_PROGRAM", "WezTerm")
        assert term_image.supports_images() == "kitty"

    def test_tmux_disables(self, monkeypatch):
        tty(monkeypatch)
        monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
        assert term_image.supports_images() is None

    def test_plain_terminal_none(self, monkeypatch):
        tty(monkeypatch)
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
        monkeypatch.setenv("TERM", "xterm-256color")
        assert term_image.supports_images() is None


class TestLocalImagePaths:
    def test_extracts_only_local_images_inside_roots(self, tmp_path, tmp_path_factory):
        inside = tmp_path / "chart.png"
        inside.write_bytes(b"png")
        outside = tmp_path_factory.mktemp("elsewhere") / "leak.png"
        outside.write_bytes(b"png")
        answer = (
            f"here ![chart]({inside}) and ![web](https://x.test/a.png) "
            f"and ![out]({outside}) and ![missing]({tmp_path / 'gone.png'}) "
            f"and ![dup]({inside}) and ![doc]({tmp_path / 'notes.txt'})"
        )
        paths = term_image.local_image_paths(answer, [tmp_path])
        assert paths == [inside]  # deduped; web/outside/missing/non-image skipped

    def test_plain_text_answer_yields_nothing(self, tmp_path):
        assert term_image.local_image_paths("no images here", [tmp_path]) == []


class TestEmit:
    def test_iterm2_escape_carries_data(self, tmp_path, capsys):
        image = tmp_path / "pic.png"
        image.write_bytes(b"\x89PNG-bytes")
        assert term_image.emit(image, "iterm2") is True
        out = capsys.readouterr().out
        assert out.startswith("\x1b]1337;File=")
        assert "inline=1" in out
        assert f"size={image.stat().st_size}" in out
        assert base64.b64encode(b"\x89PNG-bytes").decode() in out

    def test_kitty_png_chunked_escape(self, tmp_path, capsys):
        image = tmp_path / "pic.png"
        image.write_bytes(b"\x89PNG-bytes")
        assert term_image.emit(image, "kitty") is True
        out = capsys.readouterr().out
        assert out.startswith("\x1b_Ga=T,f=100,m=0;")
        assert out.rstrip("\n").endswith("\x1b\\")

    def test_kitty_rejects_non_png(self, tmp_path, capsys):
        image = tmp_path / "pic.jpg"
        image.write_bytes(b"jpeg-bytes")
        assert term_image.emit(image, "kitty") is False
        assert capsys.readouterr().out == ""

    def test_oversized_file_skipped(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(term_image, "EMIT_MAX_BYTES", 4)
        image = tmp_path / "big.png"
        image.write_bytes(b"12345")
        assert term_image.emit(image, "iterm2") is False
        assert capsys.readouterr().out == ""
