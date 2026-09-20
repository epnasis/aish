"""The one write-through-temp helper, and the race that put it there (#395).

`evidence.put` named its temp file after the PID. aish-web runs sessions on a
thread pool inside ONE process, so two sessions recording the same blob in the
same instant shared one temp path: A wrote it, B wrote it, A's replace consumed
it, B's replace raised FileNotFoundError and the whole task died. CI caught it
twice on one commit; eight local runs never did.
"""

from __future__ import annotations

import os
import pathlib
import re
import threading

import pytest

from aish import atomic_write, evidence, turns


def _temps_left(directory: pathlib.Path) -> list[pathlib.Path]:
    return [p for p in directory.rglob(".*") if p.is_file()]


class TestPublish:
    def test_writes_the_text_and_leaves_no_temp(self, tmp_path):
        path = tmp_path / "out.txt"
        assert atomic_write.publish(path, "hello") is True
        assert path.read_text() == "hello"
        assert _temps_left(tmp_path) == []

    def test_two_calls_never_share_a_temp_path(self, tmp_path, monkeypatch):
        """The #395 race, made deterministic: the temp name must be unique per
        CALL, not per process. The replace step is intercepted and skipped so
        the second call cannot short-circuit on the first's result."""
        path = tmp_path / "out.txt"
        temps: list[str] = []
        monkeypatch.setattr(os, "replace", lambda src, dst: temps.append(str(src)))
        atomic_write.publish(path, "same")
        atomic_write.publish(path, "same")
        assert len(temps) == 2
        assert temps[0] != temps[1]

    def test_keep_existing_drops_the_temp_when_a_winner_appeared(self, tmp_path, monkeypatch):
        """A concurrent writer published between this call's absence check and
        its write. For a content-addressed store the bytes are identical by
        construction, so the loser drops its temp and reports not-published."""
        path = tmp_path / "blob"
        real_mkstemp = atomic_write.tempfile.mkstemp

        def winner_then_mkstemp(**kwargs):
            path.write_text("same")
            return real_mkstemp(**kwargs)

        monkeypatch.setattr(atomic_write.tempfile, "mkstemp", winner_then_mkstemp)
        assert atomic_write.publish(path, "same", keep_existing=True) is False
        assert path.read_text() == "same"
        assert _temps_left(tmp_path) == []

    def test_without_keep_existing_the_last_writer_wins(self, tmp_path):
        path = tmp_path / "ledger.json"
        atomic_write.publish(path, "first")
        assert atomic_write.publish(path, "second") is True
        assert path.read_text() == "second"
        assert _temps_left(tmp_path) == []

    def test_a_failed_write_leaves_no_temp_and_raises(self, tmp_path, monkeypatch):
        path = tmp_path / "out.txt"
        real_fdopen = os.fdopen

        def broken_fdopen(fd, *args, **kwargs):
            handle = real_fdopen(fd, *args, **kwargs)
            handle.close()
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fdopen", broken_fdopen)
        with pytest.raises(OSError):
            atomic_write.publish(path, "hello")
        assert not path.exists()
        assert _temps_left(tmp_path) == []

    def test_a_failed_replace_leaves_no_temp_and_raises(self, tmp_path, monkeypatch):
        path = tmp_path / "out.txt"

        def broken_replace(src, dst):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(os, "replace", broken_replace)
        with pytest.raises(PermissionError):
            atomic_write.publish(path, "hello")
        assert not path.exists()
        assert _temps_left(tmp_path) == []

    def test_encoding_is_the_callers(self, tmp_path):
        path = tmp_path / "out.txt"
        atomic_write.publish(path, "zażółć", encoding="utf-8")
        assert path.read_bytes() == "zażółć".encode()


class TestContentAddressedStoresUnderConcurrency:
    """The two stores that hit #395 in production shape, driven the way
    aish-web drives them: many threads, one process, the same bytes."""

    @staticmethod
    def _stress(put, expected_path: pathlib.Path, root: pathlib.Path) -> None:
        threads_n, iterations = 8, 20
        barrier = threading.Barrier(threads_n)
        failures: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(iterations):
                    barrier.wait()
                    put()
                    expected_path.unlink(missing_ok=True)  # re-arm the race next round
            except threading.BrokenBarrierError:
                pass
            except BaseException as exc:  # noqa: BLE001 — every failure must be reported
                failures.append(exc)
                barrier.abort()

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert failures == [], failures
        put()
        assert expected_path.read_text() == "the same brief"
        assert _temps_left(root) == []

    def test_evidence_put_survives_a_thread_pool_recording_one_blob(self, tmp_path):
        digest = evidence.digest_of("the same brief")
        self._stress(
            lambda: evidence.put("the same brief", tmp_path),
            evidence.store_dir(tmp_path) / digest[:2] / digest,
            evidence.store_dir(tmp_path),
        )

    def test_turns_put_survives_a_thread_pool_recording_one_blob(self, tmp_path):
        digest = turns.digest_of("the same brief")
        session = tmp_path / "session-1.jsonl"
        self._stress(
            lambda: turns.put("the same brief", tmp_path, session),
            turns.chat_dir(tmp_path, session) / digest,
            turns.store_dir(tmp_path),
        )

    def test_evidence_put_never_shares_a_temp_path(self, tmp_path, monkeypatch):
        """Pinned at the site that failed in CI, not only at the helper."""
        temps: list[str] = []
        monkeypatch.setattr(os, "replace", lambda src, dst: temps.append(str(src)))
        evidence.put("same bytes", tmp_path)
        evidence.put("same bytes", tmp_path)
        assert len(temps) == 2 and temps[0] != temps[1], temps

    def test_evidence_put_yields_to_a_concurrent_winner(self, tmp_path, monkeypatch):
        digest = evidence.digest_of("same bytes")
        blob = evidence.store_dir(tmp_path) / digest[:2] / digest
        real_mkstemp = atomic_write.tempfile.mkstemp

        def winner_then_mkstemp(**kwargs):
            blob.write_text("same bytes")
            return real_mkstemp(**kwargs)

        monkeypatch.setattr(atomic_write.tempfile, "mkstemp", winner_then_mkstemp)
        assert evidence.put("same bytes", tmp_path) == digest
        assert evidence.get(digest, tmp_path) == "same bytes"
        assert _temps_left(evidence.store_dir(tmp_path)) == []


class TestNoTempFileIsKeyedOnThePid:
    """The lint that closes the class: a temp name carrying the PID is unique
    per process, and every store here is written from a thread pool inside
    one process. `atomic_write` is the one place a temp name is made."""

    PATTERN = re.compile(r"getpid\(\)")

    @staticmethod
    def _offenders(root: pathlib.Path) -> list[str]:
        out = []
        for file in sorted(root.glob("*.py")):
            if file.name == "atomic_write.py":
                continue
            for lineno, line in enumerate(file.read_text().splitlines(), 1):
                if TestNoTempFileIsKeyedOnThePid.PATTERN.search(line):
                    out.append(f"{file.name}:{lineno}: {line.strip()}")
        return out

    def test_no_module_names_a_temp_file_after_the_pid(self):
        root = pathlib.Path(atomic_write.__file__).parent
        assert self._offenders(root) == []

    def test_the_sweep_actually_catches_one(self, tmp_path):
        planted = 'tmp = path.with_name(f".{digest}.tmp{os.getpid()}")'
        (tmp_path / "planted.py").write_text(planted + "\n")
        assert self._offenders(tmp_path) == [f"planted.py:1: {planted}"]
