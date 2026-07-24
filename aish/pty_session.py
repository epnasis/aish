"""Interactive pseudo-terminal sessions for the web UI (issue #148).

A `!command` runs non-interactively — it cannot feed stdin, so anything that
reads from a TTY (``gcloud auth login``, OAuth prompts, ``ssh`` host-key
questions, a ``sudo`` password) just hangs. A :class:`PtySession` spawns the
command attached to a real pseudo-terminal so the program sees a TTY
(``isatty()`` is true) and can prompt, streaming bytes BOTH ways.

Security invariant (issue #148): this is the USER's own terminal. The only way
bytes reach the PTY master is :meth:`PtySession.write`, and the ONLY caller of
it is the server's ``console_in`` WebSocket handler — the message a user types
into their own terminal. The model/agent holds no reference to a PtySession and
has no code path to it, exactly like the `!` command path is ungated because the
user typing is their own authorization. Do NOT wire PtySession into the agent.

Threading discipline mirrors Bridge: a blocking ``os.read`` on the event-loop
thread would stall EVERY live session, so all reads happen on a dedicated
reader thread, and every callback into the server (output, exit) is marshalled
back onto the loop with ``call_soon_threadsafe``. Callbacks therefore always
run on the loop thread, so the server's per-session bookkeeping needs no lock —
the same rule the rest of server.py relies on.
"""

from __future__ import annotations

import codecs
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
from collections.abc import Callable

# One read buffer; the kernel already coalesces a fast writer into large reads,
# so this plus the idle-flush below keeps per-chunk WS events bounded (#109-style
# intent) without a separate timer thread.
_READ_BYTES = 65536
# Flush the decode buffer once it reaches this size even mid-burst, so a program
# spewing megabytes streams progressively instead of arriving in one lump.
_FLUSH_BYTES = 8192
# select() timeout: an idle PTY (a prompt waiting for input) flushes whatever is
# buffered within this window instead of holding a partial line until more bytes
# arrive. Also bounds how fast the reader notices a close().
_IDLE_FLUSH = 0.05


class PtySession:
    """A subprocess attached to a pseudo-terminal, streamed over a callback.

    `on_output(text)` and `on_exit(code)` are ALWAYS invoked on the event loop
    thread (via the provided loop's call_soon_threadsafe); callers pass plain
    synchronous functions and never worry about thread safety. `on_exit` fires
    exactly once.
    """

    def __init__(
        self,
        command: str,
        cwd: str,
        on_output: Callable[[str], None],
        on_exit: Callable[[int], None],
        loop,
        env: dict[str, str] | None = None,
    ) -> None:
        self._loop = loop
        self._on_output = on_output
        self._on_exit = on_exit
        self._closed = False
        self._exit_sent = False
        self._master_closed = False
        self._reap_lock = threading.Lock()

        # openpty gives a (master, slave) pair. The child gets the slave as its
        # controlling terminal (start_new_session=True → setsid); we keep the
        # master to read its output and write its input.
        self._master, slave = pty.openpty()
        # The slave's device path — the child's controlling TTY. Captured before
        # the slave fd is closed below because a caller (the tmux-backed global
        # console) needs it to name this exact client to tmux, e.g.
        # `tmux refresh-client -t <tty>` to force a repaint for a new viewer.
        self.tty = os.ttyname(slave)
        child_env = dict(os.environ if env is None else env)
        # A sane terminal type so curses-lite programs emit standard SGR/erase
        # sequences the frontend's small parser understands.
        child_env.setdefault("TERM", "xterm-256color")
        try:
            self._proc = subprocess.Popen(  # noqa: S602 — user-direct shell, ungated by design
                command,
                shell=True,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=child_env,
                start_new_session=True,  # setsid: the slave becomes the controlling TTY
                close_fds=True,
            )
        finally:
            # The child now owns the slave; the parent must not keep it open or
            # EOF on the master would never arrive when the child exits.
            os.close(slave)

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @property
    def pid(self) -> int:
        return self._proc.pid

    def write(self, data: str) -> None:
        """Feed keystrokes to the PTY. THE ONLY input path — invoked solely by
        the server's ``console_in`` handler (the user's own socket). No model or
        agent code reaches this method (issue #148 security invariant)."""
        if self._closed:
            return
        try:
            os.write(self._master, data.encode("utf-8", "replace"))
        except OSError:
            # The child closed its end between the check and the write; the
            # reader thread will observe EOF and emit the exit.
            pass

    def resize(self, cols: int, rows: int) -> None:
        """Set the window size so full-width prompts and progress lines wrap
        where the program expects (TIOCSWINSZ)."""
        if self._closed:
            return
        cols = max(1, min(cols, 1000))
        rows = max(1, min(rows, 1000))
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self._master, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def kill(self) -> None:
        """Terminate the child and release fds. Idempotent — safe to call on
        disconnect, session close/evict, a new PTY, or an explicit kill even if
        the process already exited. Signals the whole process group so children
        the command spawned die too (start_new_session gave it its own group)."""
        if self._closed:
            return
        self._closed = True
        try:
            pgid = os.getpgid(self._proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        # Closing the master unblocks the reader thread's select/read so it can
        # reap and emit the exit; the SIGTERM above asks the child to leave.
        self._close_master()

    def _close_master(self) -> None:
        """Close the master fd exactly once (kill and the reader's normal exit
        both reach here — no double close, no leaked fd on a natural exit)."""
        if not self._master_closed:
            self._master_closed = True
            try:
                os.close(self._master)
            except OSError:
                pass

    # -- reader thread ------------------------------------------------------

    def _read_loop(self) -> None:
        # Incremental decoder so a multibyte UTF-8 character split across two
        # reads is not mangled — the trailing bytes carry into the next decode.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buf: list[str] = []
        pending = 0

        def flush() -> None:
            nonlocal pending
            if buf:
                self._emit_output("".join(buf))
                buf.clear()
                pending = 0

        while not self._closed:
            try:
                ready, _, _ = select.select([self._master], [], [], _IDLE_FLUSH)
            except (OSError, ValueError):
                break  # master closed under us (kill)
            if not ready:
                flush()  # idle: don't hold a partial prompt line
                continue
            try:
                data = os.read(self._master, _READ_BYTES)
            except OSError:
                break  # slave hung up
            if not data:
                break  # EOF: child exited
            text = decoder.decode(data)
            if text:
                buf.append(text)
                pending += len(text)
                if pending >= _FLUSH_BYTES:
                    flush()

        flush()
        tail = decoder.decode(b"", final=True)
        if tail:
            self._emit_output(tail)
        self._finish()

    def _post(self, callback: Callable[..., object], *args: object) -> None:
        # The reader thread can outlive the event loop on shutdown; a callback
        # that can no longer be delivered is simply dropped (we're tearing down).
        try:
            self._loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            pass

    def _emit_output(self, text: str) -> None:
        self._post(self._on_output, text)

    def _finish(self) -> None:
        self._close_master()  # release the fd on a natural exit too, not just kill()
        code = self._reap()
        # Guard so on_exit fires exactly once even if kill() and the reader race.
        with self._reap_lock:
            if self._exit_sent:
                return
            self._exit_sent = True
        self._post(self._on_exit, code)

    def _reap(self) -> int:
        """Wait for the child and return its exit code — reaping prevents a
        zombie. A killed process reports a negative (signal) code; normalize a
        SIGTERM we sent to the shell's conventional 128+N so the UI shows a
        tidy number."""
        try:
            code = self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # It ignored SIGTERM; escalate and reap for real.
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                code = self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = -signal.SIGKILL
        # Popen reports a signal death as -N; present it as the shell's 128+N.
        return code if code >= 0 else 128 + (-code)
