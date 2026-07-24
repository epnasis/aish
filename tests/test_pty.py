"""Interactive PTY tests (issue #148).

Two layers:

1. `PtySession` in isolation — a real subprocess on a real pseudo-terminal,
   proving bytes flow BOTH ways and that fds/processes are cleaned up. The
   feature IS subprocess execution, so a short-lived, controlled child in a
   tmp dir is the honest thing to exercise (per the acceptance criteria).

2. The web wiring — the `console_open`/`console_in`/`console_out`/`console_exit`
   round trip over the same TestClient WebSocket the rest of test_server uses,
   plus the load-bearing security invariant: the model/agent has NO path to write
   PTY input. The console is now GLOBAL (issue #148 follow-up) — those web tests
   live in test_server.py::TestGlobalConsole.
"""

import asyncio
import os
import shlex
import sys
import threading
import time
from pathlib import Path

import pytest

from aish.pty_session import PtySession

# A child that PROVES it received stdin (not mere TTY echo): it prefixes each
# line it reads with GOT:, so seeing GOT:hello means the bytes reached the
# process, round-tripped, and came back over the master.
_ECHO_CHILD = (
    "import sys\n"
    "for line in sys.stdin:\n"
    "    sys.stdout.write('GOT:' + line)\n"
    "    sys.stdout.flush()\n"
)


def _child_command() -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(_ECHO_CHILD)}"


class LoopThread:
    """A real asyncio loop on a background thread — PtySession marshals its
    callbacks onto it with call_soon_threadsafe, exactly as the server does."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


@pytest.fixture
def loop_thread():
    lt = LoopThread()
    yield lt
    lt.stop()


def _wait(predicate, timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_pty_roundtrip_both_ways(loop_thread, tmp_path):
    out: list[str] = []
    exits: list[int] = []
    done = threading.Event()
    pty = PtySession(
        _child_command(),
        cwd=str(tmp_path),
        on_output=out.append,
        on_exit=lambda code: (exits.append(code), done.set()),
        loop=loop_thread.loop,
    )
    try:
        # Write flows IN; the child's GOT: prefix flows back OUT.
        pty.write("hello\n")
        assert _wait(lambda: "GOT:hello" in "".join(out)), "child stdin→stdout did not round-trip"
    finally:
        # EOF on stdin ends the child's for-loop → it exits 0.
        pty.write("\x04")
    assert done.wait(5), "process never reported exit"
    assert exits == [0]


def test_pty_kill_reaps_child_and_closes_fd(loop_thread, tmp_path):
    exits: list[int] = []
    done = threading.Event()
    # `cat` blocks forever on stdin — the classic thing that "hangs" today.
    pty = PtySession(
        "cat",
        cwd=str(tmp_path),
        on_output=lambda _s: None,
        on_exit=lambda code: (exits.append(code), done.set()),
        loop=loop_thread.loop,
    )
    pid = pty.pid
    master = pty._master
    pty.kill()
    assert done.wait(5), "kill did not reap the child"
    # The child process is gone (reaped — no zombie).
    assert _wait(lambda: not _alive(pid)), "child survived kill"
    # The master fd is closed (no leak): reading it raises.
    with pytest.raises(OSError):
        os.read(master, 1)


def test_pty_kill_is_idempotent(loop_thread, tmp_path):
    pty = PtySession(
        "cat",
        cwd=str(tmp_path),
        on_output=lambda _s: None,
        on_exit=lambda _c: None,
        loop=loop_thread.loop,
    )
    pty.kill()
    pty.kill()  # must not raise (double close / double signal guarded)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # It may be a reaped zombie the parent already wait()ed — signal 0 to a
    # fully-reaped pid raises ProcessLookupError, so reaching here means alive.
    return True


# --- the model/agent has NO write path to the PTY (security invariant) -------

_SRC = Path(__file__).resolve().parent.parent / "aish"


def test_agent_and_tools_never_reference_pty():
    """The whole point of the gate is that the model can't execute ungated. PTY
    input must be user-socket-only: agent.py / tools.py (the model's execution
    surface) must not import or touch the PTY layer at all."""
    for name in ("agent.py", "tools.py", "claude_max.py"):
        src = (_SRC / name).read_text(encoding="utf-8")
        assert "pty_session" not in src, f"{name} must not import the PTY layer"
        assert "PtySession" not in src, f"{name} must not reference PtySession"


def test_only_the_user_socket_handler_writes_to_the_console():
    """`.console.write(` (PTY input) must appear in exactly ONE place — the
    server's `_console_in` handler, driven solely by the user's socket. If a
    future edit routes PTY input from anywhere else this fails, flagging the
    invariant. The console is a single GLOBAL PtySession, so exactly one
    construction site too."""
    src = (_SRC / "server.py").read_text(encoding="utf-8")
    write_sites = src.count(".console.write(")
    assert write_sites == 1, f"expected 1 console-input site, found {write_sites}"
    construct_sites = src.count("PtySession(")
    assert construct_sites == 1, f"expected 1 PtySession construction, found {construct_sites}"
