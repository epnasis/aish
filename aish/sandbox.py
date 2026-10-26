"""The kernel boundary for commands nobody approved (epic #462, slice S1).

A cardless shell command used to be decided by the classifier in
`approval.py` alone, and the classifier kept losing: `sort -o<file>` wrote
anywhere, `man -P "<cmd>" ls` ran anything, `cat .en*` read a secret the gate
had refused to read by name. Every command the classifier lets through without
a card (Tier R) now runs under one macOS Seatbelt profile, applied by
`sandbox-exec`, enforced by the kernel whatever the command line turns out to
mean. The classifier still decides card or no card; it stops being the thing
that holds the line.

What the Tier R profile allows, and nothing more:

- **exec** — only the programs on an exact-path list: the shell the command
  line runs in, and the binaries of the classifier's read-only commands as the
  command's own PATH resolves them. Never an interpreter (`INTERPRETERS`), never
  a script (its interpreter would have to be listed), never a setuid program
  (the kernel refuses those inside a sandbox anyway).
- **read** — everything outside the owner's home tree and the shared temp
  dirs, as before; under them, only this chat's roots and its scratch. aish's
  own state and config trees stay closed even under a trusted root (they hold
  every chat), and files that commonly hold secrets are closed everywhere —
  the same names `files.is_sensitive_path` cards for, enforced after the shell
  has expanded the glob, which is exactly where the classifier could not see.
- **write** — the chat's scratch and the terminal devices, nothing else. The
  project is read-only.
- **no network, no Apple events, no signals** beyond the command's own process
  group, and **mach services** only from a short measured list.

Two facts are recorded per run, both from the KERNEL: what it refused, read
from the system log by a per-run nonce the profile tags every denial with
(`(with message …)`), and nothing else — a sandboxed process can write user
log lines, so `process == "kernel"` is part of the query. The kernel collapses
repeats and drops lines under a flood, so the record is a SAMPLE and says so.

Fail closed: no `sandbox-exec`, a profile that does not apply, or a canary
that writes where it must not ⇒ `Unavailable`, and the caller cards (attended)
or refuses (unattended). The state is never inferred from an exit code — 65
and 71 are what both a broken profile and an ordinary command can return.
"""

from __future__ import annotations

import os
import re
import secrets as _random
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
LOG_TOOL = "/usr/bin/log"
SHELL = "/bin/sh"
# What macOS's /bin/sh launcher execs: the shell named here (bash unless the
# owner changed it). It has to be on the exec list or no command line runs.
SH_SELECTOR = "/private/var/select/sh"

# Interpreters and launchers are never on the exec list, whatever a future
# read-only list names: each can run arbitrary code or another program, and the
# list is the only thing standing between a cardless command and that. Shells
# are the exception that makes a command line possible at all; they cannot
# load code the profile does not let them map (see `MAPPABLE`).
INTERPRETERS = frozenset(
    {
        "perl", "python", "python2", "python3", "ruby", "node", "deno", "bun",
        "php", "lua", "awk", "gawk", "nawk", "env", "xargs", "osascript",
        "launchctl", "open", "less", "more", "most", "vi", "vim", "nvim", "man",
        "sudo", "arch", "nice", "timeout", "nohup", "script", "expect", "tclsh",
        "swift", "osacompile", "jsc", "zsh", "ksh", "tcsh", "csh", "fish", "dash",
    }
)

# Left off the exec list because inside the sandbox they would answer WRONG
# with nothing recorded: `/usr/bin/which` asks access(X_OK), and the kernel
# answers "not executable" for every program off the exec list without
# logging a denial — measured 2026-10-26, `which node` exited 1 with node
# installed and no kernel line. Refused at exec instead, which IS recorded,
# so the model is told and can use the shell's own `command -v`, which reads
# the file mode and was measured to answer correctly.
UNTRUTHFUL_INSIDE = frozenset({"which"})

# The scratch-workspace verbs (`approval.is_scratch_housekeeping`) run
# cardless too, so they run here: the profile lets them write only to scratch.
HOUSEKEEPING_PROGRAMS = ("mkdir", "touch", "rm", "true", "echo")

# Writable besides scratch. /dev/tty and /dev/dtracehelper are opened by
# ordinary programs at start-up (measured: bash and sort, 2026-10-26).
WRITABLE_DEVICES = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper",
                    "/dev/stdout", "/dev/stderr")

# Closed to reading unless a root or the scratch reopens a part of them: the
# home tree (and every other user's), external volumes, and the shared temp
# dirs where other programs leave their working files.
CLOSED_TO_READ = ("/Users", "/Volumes", "/private/tmp", "/private/var/folders")

# Where code may be mapped executable from. Bash's `enable -f` and zsh's
# `zmodload` load a shared object; outside these trees nothing can be mapped,
# so a library written to scratch or shipped in a project cannot be loaded.
MAPPABLE = ("/System", "/usr/lib", "/usr/libexec", "/Library/Apple",
            "/private/var/db/dyld", "/Library/Preferences/Logging")

# Mach services a read-only command was measured to need (2026-10-26, the
# corpus replay in docs/sandbox.md): name lookups (`id`, `whoami`), the
# notification centre libSystem checks in at start-up, and logging.
MACH_SERVICES = (
    "com.apple.system.notification_center",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.logd",
    "com.apple.system.logger",
    "com.apple.diagnosticd",
)

# Environment a sandboxed command inherits. Everything else — tokens, keys,
# SSH_AUTH_SOCK, DYLD_*/LD_* — is dropped by construction (an allow-list, so a
# new secret variable is dropped without anyone naming it).
PASSED_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "USER", "LOGNAME",
              "SHELL", "COLUMNS", "LINES", "TZ")

REFUSAL_SAMPLE_MAX = 20
TARGET_CHARS = 300
CANARY_TIMEOUT_S = 10
LOG_TIMEOUT_S = 10
REREAD_DELAY_S = 0.5


class Unavailable:
    """The sandbox cannot run commands here, and what was OBSERVED that says
    so. Falsy, so `if confinement:` reads naturally."""

    def __init__(self, observed: str):
        self.observed = observed

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"Unavailable({self.observed!r})"


@dataclass(frozen=True)
class Refusal:
    """One kernel denial line: which process, which operation, on what."""

    process: str
    operation: str
    target: str

    def as_record(self) -> dict:
        return {"process": self.process, "op": self.operation, "target": self.target}


@dataclass
class RefusalReport:
    """What the kernel log said about one run. `read_error` is set when the
    log could not be read at all — then nothing is known either way."""

    refusals: list[Refusal] = field(default_factory=list)
    seen: int = 0
    read_error: str = ""
    log_ms: int = 0
    reads: int = 1

    def as_record(self) -> dict:
        record: dict = {
            "refused": [r.as_record() for r in self.refusals],
            "refused_seen": self.seen,
            "sample": True,
            "log_ms": self.log_ms,
            "log_reads": self.reads,
        }
        if self.read_error:
            record["log_error"] = self.read_error
        return record


@dataclass(frozen=True)
class ProfilePaths:
    """The per-chat inputs of the profile, every one already resolved."""

    roots: tuple[str, ...]
    scratch: str
    protected: tuple[str, ...]  # aish's own trees: closed even under a root


def _real(path: os.PathLike | str) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def profile_paths(
    roots: Iterable[os.PathLike | str],
    scratch: os.PathLike | str,
    protected: Iterable[os.PathLike | str] = (),
) -> ProfilePaths:
    return ProfilePaths(
        roots=tuple(dict.fromkeys(_real(r) for r in roots)),
        scratch=_real(scratch),
        protected=tuple(dict.fromkeys(_real(p) for p in protected)),
    )


def _is_script(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return True  # unreadable: never list what cannot be inspected


def _is_privileged(path: str) -> bool:
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return True
    return bool(mode & (stat.S_ISUID | stat.S_ISGID))


def exec_allow_list(names: Iterable[str], path_env: str) -> tuple[str, ...]:
    """The exact binaries a Tier R command may start: `/bin/sh`, what it
    execs, and each name as `path_env` resolves it — symlinks resolved,
    because the kernel matches the real path. A name that resolves to a
    script, a setuid/setgid file or an interpreter is left OFF: that command
    is then refused by the kernel and the model is told it may ask for a card.
    """
    allowed: list[str] = [SHELL]
    selected = _real(SH_SELECTOR)
    if os.path.basename(selected) in {"bash", "sh", "zsh", "dash"}:
        allowed.append(selected)
    allowed.append(_real(SHELL))
    for name in sorted(set(names)):
        if name in INTERPRETERS or name in UNTRUTHFUL_INSIDE:
            continue
        found = shutil.which(name, path=path_env)
        if not found:
            continue
        real = _real(found)
        if os.path.basename(real) in INTERPRETERS:
            continue
        if _is_script(real) or _is_privileged(real):
            continue
        allowed.append(real)
    return tuple(dict.fromkeys(allowed))


def _sbpl_string(text: str) -> str:
    """A path as an SBPL string literal. Control characters cannot be spelled
    safely in a profile, so a path holding one makes the profile unbuildable —
    the caller then fails closed rather than guessing an escape."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError(f"path holds a control character: {text!r}")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ci(word: str) -> str:
    """`word` as a case-insensitive SBPL regex fragment."""
    out = []
    for ch in word:
        if ch.isalpha():
            out.append(f"[{ch.lower()}{ch.upper()}]")
        elif ch.isalnum() or ch in "_-":
            out.append(ch)
        else:
            out.append("\\" + ch)
    return "".join(out)


def sensitive_regexes() -> tuple[str, ...]:
    """`files.is_sensitive_path` as kernel rules: the same three lists, read
    from the module that owns them so the two can never disagree."""
    from . import files

    dirs = "|".join(_ci(d) for d in sorted(files._SENSITIVE_DIRS))
    names = "|".join(_ci(n) for n in sorted(files._SENSITIVE_NAMES))
    suffixes = "|".join(_ci(s.lstrip(".")) for s in files._SENSITIVE_SUFFIXES)
    return (
        f"/({dirs})(/|$)",
        f"/({names})$",
        f"/{_ci('.env')}[^/]*$",
        f"\\.({suffixes})$",
    )


NONCE_SLOT = "@@NONCE@@"


def render_profile(paths: ProfilePaths, exec_list: Iterable[str]) -> str:
    """The Tier R profile with `NONCE_SLOT` where each run's nonce goes.

    Seatbelt applies the LAST rule that matches, so each section is a
    deny followed by the narrower allows that reopen part of it, and the
    order of sections is load-bearing — the comments say why each one is
    where it is.
    """
    tag = f'(with message "{NONCE_SLOT}")'
    s = _sbpl_string
    lines = ["(version 1)", "(allow default)"]

    lines.append(f"(deny process-exec* {tag})")
    lines.append(
        "(allow process-exec* " + " ".join(f"(literal {s(p)})" for p in exec_list) + ")"
    )

    lines.append(f"(deny file-map-executable {tag})")
    lines.append(
        "(allow file-map-executable "
        + " ".join(f"(subpath {s(p)})" for p in MAPPABLE)
        + ")"
    )

    lines.append(f"(deny file-write* {tag})")
    lines.append(
        "(allow file-write* "
        f"(subpath {s(paths.scratch)}) "
        + " ".join(f"(literal {s(d)})" for d in WRITABLE_DEVICES)
        # /dev/tty is the command's OWN controlling terminal; the numbered
        # /dev/ttysN are every terminal the owner has open, and writing one is
        # writing escape sequences into another session (design review,
        # 2026-10-26), so none of them is listed.
        + ' (regex #"^/dev/fd/[0-9]+$"))'
    )

    # Reads: close the home tree and the shared temp dirs, reopen the roots
    # and the scratch, close aish's own trees again (a trusted ~ must not hand
    # out every chat's log), reopen what lives inside them on purpose (the
    # scratch, the uploads root), and close the secret-holding names last so
    # they win inside every root.
    lines.append(
        "(deny file-read* " + " ".join(f"(subpath {s(p)})" for p in CLOSED_TO_READ)
        + f" {tag})"
    )
    reopened = (*paths.roots, paths.scratch)
    lines.append(
        "(allow file-read* " + " ".join(f"(subpath {s(p)})" for p in reopened) + ")"
    )
    if paths.protected:
        lines.append(
            "(deny file-read* " + " ".join(f"(subpath {s(p)})" for p in paths.protected)
            + f" {tag})"
        )
        inside = [
            p for p in reopened
            if any(p == q or p.startswith(q.rstrip("/") + "/") for q in paths.protected)
        ]
        if inside:
            lines.append(
                "(allow file-read* " + " ".join(f"(subpath {s(p)})" for p in inside) + ")"
            )
    lines.append(
        "(deny file-read* "
        + " ".join(f'(regex #"{r}")' for r in sensitive_regexes())
        + f" {tag})"
    )
    # stat() anywhere: getcwd and realpath walk the parents of the project,
    # and `ls -l` in a project with a .env would otherwise fail on its size.
    # Listing a directory or reading a file is file-read-data, still closed.
    lines.append("(allow file-read-metadata)")

    lines.append(f"(deny network* {tag})")
    lines.append(f"(deny appleevent-send {tag})")
    lines.append(f"(deny mach-lookup {tag})")
    lines.append(
        "(allow mach-lookup "
        + " ".join(f"(global-name {s(n)})" for n in MACH_SERVICES)
        + ")"
    )
    # The command runs in its own session (start_new_session), so its process
    # group is exactly the command: it may stop its own children, nothing else.
    lines.append(f"(deny signal {tag})")
    lines.append("(allow signal (target self) (target pgrp))")
    return "\n".join(lines) + "\n"


def clean_env(scratch: str, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a sandboxed command: the allow-listed names from
    `source`, HOME as it is (paths the classifier resolved with `~` must mean
    the same thing), and every temp/cache variable pointed into scratch —
    git and the developer tools fail on a TMPDIR they cannot write."""
    source = os.environ if source is None else source
    env = {k: source[k] for k in PASSED_ENV if k in source}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env["HOME"] = source.get("HOME") or str(Path.home())
    tmp = os.path.join(scratch, ".tmp")
    env["TMPDIR"] = tmp + "/"
    env["XDG_CACHE_HOME"] = os.path.join(scratch, ".cache")
    return env


_NONCE_RE = re.compile(r"^[A-Za-z0-9]+$")
_DENY_RE = re.compile(
    r"\ASandbox: (?P<process>[^\n]+?)\((?P<pid>\d+)\) deny\(\d+\) (?P<op>\S+)"
    r"(?: (?P<target>.*))?\Z",
    re.DOTALL,
)


def parse_refusals(ndjson: str, nonce: str) -> tuple[list[Refusal], int]:
    """Kernel denials tagged with `nonce`, from `log show --style ndjson`.

    ONE log entry is one denial: `Sandbox: proc(pid) deny(n) op target`, then
    a newline and the `(with message …)` text — the nonce. The target is a
    path, and a path may hold newlines, so a file named
    `q\\nSandbox: x(7) deny(1) network-outbound evil:443` puts a second
    denial-shaped line INSIDE a genuine kernel entry (found by the design
    review, 2026-10-26). So an entry counts only when it begins with the
    denial and ENDS with `\\n<nonce>`, and everything between the operation
    and that final line is the target — newlines included, shown escaped.
    Distinct (process, op, target) only, first seen first, capped at
    `REFUSAL_SAMPLE_MAX`; the second value is how many tagged entries there
    were in all.
    """
    import json

    seen: dict[tuple[str, str, str], Refusal] = {}
    total = 0
    suffix = "\n" + nonce
    for line in ndjson.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("eventMessage") if isinstance(entry, dict) else None
        if not isinstance(message, str) or not message.endswith(suffix):
            continue
        match = _DENY_RE.match(message[: -len(suffix)])
        if not match:
            continue
        total += 1
        target = (match.group("target") or "").strip().replace("\n", "\\n")
        if len(target) > TARGET_CHARS:
            target = target[:TARGET_CHARS] + "…"
        refusal = Refusal(match.group("process"), match.group("op"), target)
        key = (refusal.process, refusal.operation, refusal.target)
        if key not in seen and len(seen) < REFUSAL_SAMPLE_MAX:
            seen[key] = refusal
    return list(seen.values()), total


class Confinement:
    """One run's sandbox: the profile with its nonce, and the clean env."""

    tier = "read_only"

    def __init__(self, template: str, paths: ProfilePaths, sandbox: SeatbeltSandbox):
        self.nonce = _random.token_hex(12)
        self.profile = template.replace(NONCE_SLOT, self.nonce)
        self.paths = paths
        self._sandbox = sandbox
        self.started = time.time()

    def launch(self, command: str) -> tuple[list[str], dict[str, str]]:
        """argv and env for Popen. `/bin/sh -c` — the shell the classifier
        modelled and `subprocess(shell=True)` would have used."""
        env = clean_env(self.paths.scratch)
        os.makedirs(env["TMPDIR"], exist_ok=True)
        self.started = time.time()
        return [SANDBOX_EXEC, "-p", self.profile, SHELL, "-c", command], env

    def refusals(self, exit_code: int | None = None) -> RefusalReport:
        """Read this run's refusals. A run that FAILED with none recorded is
        read once more after `REREAD_DELAY_S`: under load the kernel's lines
        reach the log store late (measured: a parallel corpus replay missed
        lines a serial rerun of the same commands found). The exit code only
        decides whether to look twice; it is never read as a refusal."""
        report = self._sandbox.read_refusals(self.nonce, self.started)
        if report.refusals or report.read_error or exit_code in (None, 0):
            return report
        time.sleep(REREAD_DELAY_S)
        again = self._sandbox.read_refusals(self.nonce, self.started)
        again.log_ms += report.log_ms
        again.reads = 2
        return again

    def describe(self) -> str:
        return (
            f"it could read this chat's folders, write only to its scratch folder "
            f"{self.paths.scratch}, start only the programs on its list (the shell, "
            "aish's read-only commands, and mkdir/touch/rm for scratch), and had "
            "no network"
        )


class SeatbeltSandbox:
    """The process-wide sandbox: builds profiles, proves each one with a
    canary before its first use, and reads refusals back from the kernel log.
    """

    def __init__(self, read_only_names: Iterable[str] | None = None):
        self._names = read_only_names
        self._lock = threading.Lock()
        self._proven: dict[str, Unavailable | None] = {}

    def _platform_problem(self) -> str | None:
        if sys.platform != "darwin":
            return f"this machine runs {sys.platform}, and aish's sandbox needs macOS"
        for tool in (SANDBOX_EXEC, LOG_TOOL):
            if not os.access(tool, os.X_OK):
                return f"{tool} is not on this machine"
        return None

    def names(self) -> frozenset[str]:
        if self._names is not None:
            return frozenset(self._names)
        from .approval import SAFE_COMMANDS

        return frozenset(SAFE_COMMANDS) | frozenset(HOUSEKEEPING_PROGRAMS)

    def prepare(
        self,
        roots: Iterable[os.PathLike | str],
        scratch: os.PathLike | str,
        protected: Iterable[os.PathLike | str] = (),
        path_env: str | None = None,
    ) -> Confinement | Unavailable:
        problem = self._platform_problem()
        if problem:
            return Unavailable(problem)
        try:
            paths = profile_paths(roots, scratch, protected)
            exec_list = exec_allow_list(
                self.names(), path_env if path_env is not None else os.environ.get("PATH", "")
            )
            template = render_profile(paths, exec_list)
        except (OSError, ValueError) as exc:
            return Unavailable(f"aish could not build the sandbox profile: {exc}")
        with self._lock:
            if template not in self._proven:
                self._proven[template] = self._canary(template, paths)
            verdict = self._proven[template]
        if verdict is not None:
            return verdict
        return Confinement(template, paths, self)

    def _canary(self, template: str, paths: ProfilePaths) -> Unavailable | None:
        """Run the profile once with three probes and check their EFFECTS on
        disk: a write into scratch must land (the command ran under it), a
        write outside must not, and a program off the exec list must not
        start. Exit codes are not consulted."""
        probe = Confinement(template, paths, self)
        token = _random.token_hex(6)
        outside = tempfile.mkdtemp(prefix="aish-sandbox-canary-")
        ran = os.path.join(paths.scratch, f".aish-canary-ran-{token}")
        leaked = os.path.join(outside, "written")
        exec_leak = os.path.join(paths.scratch, f".aish-canary-exec-{token}")
        script = (
            f"echo ok > '{ran}'; echo leak > '{leaked}'; "
            f"/usr/bin/env /bin/sh -c \"echo leak > '{exec_leak}'\""
        )
        try:
            os.makedirs(paths.scratch, exist_ok=True)
            argv, env = probe.launch(script)
            try:
                subprocess.run(
                    argv, env=env, cwd=paths.scratch, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=CANARY_TIMEOUT_S, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return Unavailable(f"the sandbox canary did not run: {exc}")
            if not os.path.exists(ran):
                return Unavailable(
                    "the sandbox canary wrote nothing into scratch, so the profile "
                    "did not run a command"
                )
            if os.path.exists(leaked):
                return Unavailable("the sandbox canary wrote outside scratch")
            if os.path.exists(exec_leak):
                return Unavailable("the sandbox canary started a program off its list")
            return None
        finally:
            for path in (ran, exec_leak, leaked):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            try:
                os.rmdir(outside)
            except OSError:
                pass

    def read_refusals(self, nonce: str, since: float) -> RefusalReport:
        if not _NONCE_RE.match(nonce):
            return RefusalReport(read_error="malformed nonce")
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since - 1))
        began = time.monotonic()
        try:
            done = subprocess.run(
                [
                    LOG_TOOL, "show", "--style", "ndjson", "--start", start,
                    "--predicate", f'process == "kernel" AND eventMessage CONTAINS "{nonce}"',
                ],
                capture_output=True, text=True, errors="replace",
                timeout=LOG_TIMEOUT_S, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return RefusalReport(
                read_error=f"the system log could not be read: {exc}",
                log_ms=int((time.monotonic() - began) * 1000),
            )
        log_ms = int((time.monotonic() - began) * 1000)
        if done.returncode != 0:
            return RefusalReport(
                read_error=f"log show exited {done.returncode}: {done.stderr.strip()[:200]}",
                log_ms=log_ms,
            )
        refusals, seen = parse_refusals(done.stdout, nonce)
        return RefusalReport(refusals=refusals, seen=seen, log_ms=log_ms)


class DeferredRead:
    """One run's refusal read, delivered later and EXACTLY once: by the worker
    when the read ends, or by `settle`/`abandon_all` with `read: false` when
    aish stops waiting — whichever comes first; the other is a no-op."""

    def __init__(self, deliver: Callable[[dict], None]):
        self._deliver = deliver
        self._lock = threading.Lock()
        self.done = False
        self.future: Future | None = None

    def finish(self, record: dict) -> bool:
        with self._lock:
            if self.done:
                return False
            self.done = True
        try:
            self._deliver(record)
        except Exception:  # noqa: BLE001 — a closed log must not kill the worker
            pass
        return True


class DeferredReads:
    """Refusal reads for runs that SUCCEEDED, done off the command's path (the
    owner's decision, 2026-10-26: a sandboxed command takes ~7 ms and its log
    read ~800 ms, and on a successful run the model is not told about
    refusals anyway). Bounded on both axes that could pile up: `WORKERS`
    reads at once, and `MAX_PENDING` waiting — past that a run's record says
    it was not read, at once, rather than queueing without end."""

    WORKERS = 2
    MAX_PENDING = 16

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(self.WORKERS, thread_name_prefix="aish-refusals")
        self._lock = threading.Lock()
        self._jobs: set[DeferredRead] = set()
        self._closed = ""

    def submit(
        self, read: Callable[[], RefusalReport], deliver: Callable[[dict], None]
    ) -> DeferredRead:
        job = DeferredRead(deliver)
        with self._lock:
            refused = self._closed or (
                "too many refusal reads were already waiting"
                if len(self._jobs) >= self.MAX_PENDING else ""
            )
            if not refused:
                self._jobs.add(job)
        if refused:
            job.finish({"read": False, "why": refused})
            return job
        job.future = self._pool.submit(self._run, job, read)
        return job

    def _run(self, job: DeferredRead, read: Callable[[], RefusalReport]) -> None:
        try:
            record = {"read": True, **read().as_record()}
        except Exception as exc:  # noqa: BLE001
            record = {"read": False, "why": f"the read raised {type(exc).__name__}: {exc}"}
        self._finish(job, record)

    def _finish(self, job: DeferredRead, record: dict) -> None:
        job.finish(record)
        with self._lock:
            self._jobs.discard(job)

    def settle(self, jobs: Iterable[DeferredRead], timeout: float, why: str) -> None:
        """Wait up to `timeout` for `jobs`; any still running is recorded as
        not read, with `why`. Its worker may still finish — and then delivers
        nothing, because the job is already done."""
        jobs = [j for j in jobs if not j.done]
        futures = [j.future for j in jobs if j.future is not None]
        try:
            if futures:
                wait(futures, timeout=timeout)
        finally:
            # Even when the wait is interrupted (Ctrl-C): the caller has
            # already forgotten these jobs, and a worker finishing later would
            # deliver through a log callable that may by then point at
            # another chat (the CLI's /resume). Marked here, it delivers
            # nothing (delivery review F2, 2026-10-26).
            for job in jobs:
                self._finish(job, {"read": False, "why": why})

    def abandon_all(self, why: str) -> None:
        """Process shutdown: record every pending read as not read, and take no
        more — the process may exit before a worker could deliver."""
        with self._lock:
            self._closed = why
            jobs = list(self._jobs)
        for job in jobs:
            self._finish(job, {"read": False, "why": why})


_deferred: DeferredReads | None = None


def deferred_reads() -> DeferredReads:
    """The process-wide refusal reader. Tests replace it per test."""
    global _deferred
    with _system_lock:
        if _deferred is None:
            _deferred = DeferredReads()
        return _deferred


_system: SeatbeltSandbox | None = None
_system_lock = threading.Lock()


def system() -> SeatbeltSandbox:
    """The process-wide sandbox. Tests replace it (`tests/conftest.py`)."""
    global _system
    with _system_lock:
        if _system is None:
            _system = SeatbeltSandbox()
        return _system
