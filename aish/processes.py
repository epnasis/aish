"""aish's own read-only answer to "is X running?" (epic #462 S1).

`ps` is setuid, and the kernel refuses a setuid program inside a sandbox, so
the one command a model reached for to answer this question can no longer run
without a card. aish answers it itself instead: it runs `/bin/ps` with a fixed
argv (no shell, nothing the model wrote reaches it) and hands back only what
the question needs.

What it returns is deliberately less than `ps` would print. A process's
arguments and environment can carry tokens (`--api-key=…`, a database URL), and
another program's command line is not something the owner expects a read-only
check to read out. So a match reports the pid, the executable's name and how
long it has been running — never the arguments. Arguments are still MATCHED,
because "is aish-web running" is a question about a Python script whose
executable is `python3`; but only by exact equality with the last path part of
a whole word, case-insensitively. That cannot be used as an oracle to read an
argument out one character at a time: a guess has to be the entire word.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

PS = "/bin/ps"
MAX_MATCHES = 40
NAME_MAX_CHARS = 120
PS_TIMEOUT_S = 10


@dataclass(frozen=True)
class Process:
    pid: int
    elapsed: str
    executable: str  # basename of the executable, as `ps -o comm` names it
    words: tuple[str, ...]  # the command line split on whitespace — matched, never shown


Runner = Callable[[list[str]], str]


def _run_ps(argv: list[str]) -> str:
    done = subprocess.run(
        argv, capture_output=True, text=True, errors="replace",
        timeout=PS_TIMEOUT_S, check=True,
    )
    return done.stdout


def snapshot(run: Runner | None = None) -> list[Process]:
    """Every process `ps` lists, joined by pid across two fixed calls."""
    run = run or _run_ps
    names: dict[int, tuple[str, str]] = {}
    for line in run([PS, "-axo", "pid=,etime=,comm="]).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            names[int(parts[0])] = (parts[1], os.path.basename(parts[2].rstrip()))
    words: dict[int, tuple[str, ...]] = {}
    for line in run([PS, "-axww", "-o", "pid=,args="]).splitlines():
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            words[int(parts[0])] = tuple(parts[1].split()) if len(parts) > 1 else ()
    return [
        Process(pid, elapsed, executable, words.get(pid, ()))
        for pid, (elapsed, executable) in sorted(names.items())
    ]


def _matches(process: Process, needle: str) -> str:
    """How `process` matches, or empty: the executable's name contains the
    needle, or one argument word's last path part IS the needle."""
    if needle in process.executable.lower():
        return "executable name"
    for word in process.words[1:]:
        if os.path.basename(word.rstrip("/")).lower() == needle:
            return "an argument word"
    return ""


def report(name: str, run: Runner | None = None) -> str:
    """The tool's answer for the program `name`, as text the model reads."""
    needle = (name or "").strip().lower()
    if not needle:
        return "ERROR: program is required — the program to look for, e.g. 'ollama'."
    if len(needle) > NAME_MAX_CHARS or any(ord(ch) < 0x20 for ch in needle):
        return "ERROR: program must be a short program name."
    try:
        processes = snapshot(run)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ERROR: aish could not list processes: {exc}"
    found = [(p, how) for p in processes if (how := _matches(p, needle))]
    if not found:
        return (
            f"No running process matches '{name.strip()}' (checked every executable "
            "name, and every argument word exactly)."
        )
    lines = [
        f"pid {p.pid} · {p.executable} · running {p.elapsed} · matched by {how}"
        for p, how in found[:MAX_MATCHES]
    ]
    if len(found) > MAX_MATCHES:
        lines.append(f"… and {len(found) - MAX_MATCHES} more")
    lines.append("(arguments and environment are never shown)")
    return "\n".join(lines)
