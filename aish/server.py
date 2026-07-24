"""Web UI server: the same agent core behind a WebSocket instead of a TTY.

The browser is a thin client. Every callback the CLI wires to print()/input()
is wired here to JSON events over one WebSocket: tokens/echo/status stream
out, and approvals block the agent's worker thread on a queue until the
browser answers — the approval gate is identical to the terminal's, only the
transport differs.

Process model: one process holds MANY open sessions (each its own Agent +
SessionLog + transcript + busy flag) and serves MANY connections at once
(#102). Each connection is a Client with its own socket, outbox, and
independently-chosen session view; a session's events fan out to every Client
currently viewing it. Connections coexist without preempting — same token, one
user, many tabs/devices. Tasks keep running in background sessions; switching a
Client's view just replays the target's transcript, so a task started in one
session finishes while you work in another, and a reconnect (or a second tab)
receives the buffered transcript — which is what makes phone lock/unlock
mid-task lossless. Control is last-actor-drives: acting on a session stamps that
Client as its controller (a `role` event tells the other viewers); there is no
locked role and no disabled UI — the approval gate itself is unchanged, only
fanned out.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import backends, dir_ignore, export, tools
from .agent import FEEDBACK_SWITCH_NOTE, Agent, ModelUnavailable, environment_context
from .approval import (
    DEFAULT_ALLOWLIST,
    DEFAULT_DENYLIST,
    Approved,
    Blocked,
    Denied,
    check_denied,
    escaping_dirs,
    is_auto_approvable,
    load_prefixes,
    looks_destructive,
    save_prefix,
    suggest_prefix,
    unvetted_segments,
)
from .cli import (
    DEFAULT_LESSONS,
    LogRef,
    _backend_hint,
    available_models,
    default_workspace,
    identity_context,
    load_config,
    load_context_files,
    model_spec,
    parse_feedback,
    parse_learn,
    rank_models,
    save_default_model,
)
from .embeddings import SemanticIndex
from .prompt import ATFILE_MAX_RESULTS, ATFILE_SCAN_CAP
from .pty_session import PtySession
from .session import SessionLog

if TYPE_CHECKING:
    from .claude_max import ClaudeMaxAgent

STATIC_DIR = Path(__file__).parent / "static"


def _static_rev() -> str:
    """Fingerprint of the served frontend, sent in hello. The client compares
    it to the rev it was loaded with and reloads on mismatch — an installed
    iOS PWA resumed from the app switcher never reloads the page on its own,
    so deployed frontend fixes would otherwise not reach the device."""
    try:
        stats = sorted(
            (str(p.relative_to(STATIC_DIR)), s.st_mtime_ns, s.st_size)
            for p in STATIC_DIR.rglob("*")
            if p.is_file() and (s := p.stat())
        )
        return hashlib.md5(repr(stats).encode()).hexdigest()[:12]
    except OSError:
        return "0"


STATIC_REV = _static_rev()


async def serve_index(request):  # noqa: ARG001 — Starlette route signature
    """index.html with cache-busting ?v=<rev> on its assets and no-cache on
    itself: the page then always names the exact JS/CSS revision it runs, so
    a stale-from-HTTP-cache page can be detected (hello.rev mismatch) and a
    reload is guaranteed to fetch the current code."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace('src="app.js"', f'src="app.js?v={STATIC_REV}"')
    html = html.replace('href="style.css"', f'href="style.css?v={STATIC_REV}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# Optional user-provided fonts (e.g. a licensed terminal font) live in the CONFIG
# dir, NOT the repo/wheel — so licensed files are never committed or bundled, and
# they survive reinstalls. Absent → 404, and the CSS @font-face falls back to the
# system font stack. (#148)
CONFIG_FONT_DIR = Path.home() / ".config" / "aish" / "fonts"
_FONT_MEDIA = {".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf"}


async def serve_config_font(request):
    name = request.path_params["name"]
    if "/" in name or ".." in name or Path(name).suffix.lower() not in _FONT_MEDIA:
        return Response(status_code=404)
    path = CONFIG_FONT_DIR / name
    # "mono" is the app's opinionated role font: serve whatever font the user
    # dropped in the config dir (first woff2, else any font file) so a link to
    # /fonts/mono.woff2 works regardless of the file's real name.
    if not path.is_file() and Path(name).stem == "mono":
        fonts = sorted(CONFIG_FONT_DIR.glob("*.woff2")) or sorted(
            p for p in CONFIG_FONT_DIR.glob("*") if p.suffix.lower() in _FONT_MEDIA
        )
        if fonts:
            path = fonts[0]
    if not path.is_file():
        return Response(status_code=404)  # no font installed — @font-face falls back
    return FileResponse(
        path,
        media_type=_FONT_MEDIA[path.suffix.lower()],
        headers={"Cache-Control": "public, max-age=604800"},
    )

# Replay buffer bounds: enough for a long task's worth of events; beyond it
# the oldest are dropped and the client shows a truncation marker.
TRANSCRIPT_MAX = 600
TRANSCRIPT_KEEP = 500

# Open sessions kept in memory at once; beyond this the longest-idle one is
# closed (its file persists — reopening it later just reloads the history).
MAX_OPEN_SESSIONS = 6

# The global "Quake console" (issue #148 follow-up). ONE interactive PTY for
# the whole server — not per-session — openable from any chat and surviving
# chat-switches, disconnects, and (tmux-backed) aish-web restarts. When tmux is
# present the console PTY runs `tmux new-session -A -s <name>`: attach-or-create,
# so the shell lives in tmux's DETACHED server process and outlives aish-web;
# our PTY is merely a tmux client. Without tmux we spawn $SHELL directly (global
# + cross-chat + cross-disconnect, but NOT restart-surviving).
TMUX_CONSOLE_SESSION = "aish-console"

UPLOAD_MAX_BYTES = 25 * 1024 * 1024
EXPORT_MAX_BYTES = 5 * 1024 * 1024  # a single answer's markdown; generous ceiling

# Quick-reply safety net (issue #46). The model is told to end a question with
# aish-reply:// chips, but small local models forget — so on the WEB surface a
# final answer that ends in a question yet carries no chip gets a deterministic
# fallback set appended. It is a guarantee, not a guess: no extra model call,
# no latency. The model opts out by ending with the literal [no-chips] tag,
# which is stripped from the shown answer (the frontend also strips it live).
NO_CHIPS_TAG_RE = re.compile(r"\[no-chips\]", re.IGNORECASE)
_REPLY_SCHEME = "aish-reply://"
FALLBACK_CHIPS = (
    "[Yes](aish-reply://yes)",
    "[No](aish-reply://no)",
    "[Tell me more](aish-reply://tell me more)",
)


def _ends_with_question(text: str) -> bool:
    """The trailing non-whitespace character is a question mark — the signal
    that the turn ended by asking something. Deliberately simple: a false
    negative just skips the net, a false positive appends harmless chips."""
    return text.rstrip().endswith("?")


def quick_reply_suffix(text: str) -> str | None:
    """Web-only post-processing verdict for a final answer. Returns the chip
    block to APPEND when the answer ends in a question and has no chip and no
    opt-out; an empty string when the [no-chips] opt-out fired (caller strips
    the tag, appends nothing); or None to leave the answer untouched."""
    if NO_CHIPS_TAG_RE.search(text):
        return ""
    if _REPLY_SCHEME in text:
        return None
    if not _ends_with_question(text):
        return None
    return "\n".join(FALLBACK_CHIPS)


def apply_quick_reply_net(result: str) -> tuple[str, str | None]:
    """Map a final answer to (shown_answer, streamed_suffix). shown_answer is
    the canonical text for done.result / export / replay; streamed_suffix, when
    not None, is the chip block to also emit as a live token so it lands in an
    already-streamed answer. [no-chips] strips the tag and streams nothing."""
    suffix = quick_reply_suffix(result)
    if suffix is None:
        return result, None
    if suffix == "":  # opt-out: drop the tag from the stored answer
        return NO_CHIPS_TAG_RE.sub("", result).rstrip(), None
    return f"{result.rstrip()}\n\n{suffix}", f"\n\n{suffix}"


# Backend-owned issue creation (#110). A text-only /feedback draft comes back as
# one ```aish-issue fenced block — the single source of truth: the frontend
# renders it as a review card, and on confirm the backend files it VERBATIM.
# The repo is hard-pinned; the model never runs `gh issue create` in this flow.
ISSUE_REPO = "epnasis/aish"
ISSUE_BLOCK_RE = re.compile(r"```aish-issue[^\n]*\n(.*?)```", re.DOTALL)
# `gh issue create` prints the new issue's URL to stdout; pull it out so the
# confirmation can show a clickable link instead of leaving it as plain terminal
# text (#110 follow-up).
ISSUE_URL_RE = re.compile(r"https://github\.com/\S+/issues/\d+")


def parse_issue_block(text: str) -> tuple[dict[str, str] | None, str]:
    """Extract the first ```aish-issue block. Returns ({title, body}, cleaned)
    where cleaned is `text` with the raw fence removed; ({}, text) is signalled
    as (None, text) when no block is present. Parsing rule (defined once, used
    everywhere — mirrored in app.js issueDraftCard): strip the fence; line 1 is
    `title: <text>`; if the next line is exactly `---` it's an optional
    separator and the body starts after it, else the body starts on line 2; the
    remainder verbatim is the body (so a `---` deeper in the body is kept)."""
    match = ISSUE_BLOCK_RE.search(text)
    if match is None:
        return None, text
    lines = match.group(1).split("\n")
    first = lines[0].strip()
    title = first[len("title:"):].strip() if first.lower().startswith("title:") else first
    rest = lines[1:]
    if rest and rest[0].strip() == "---":  # optional separator
        rest = rest[1:]
    body = "\n".join(rest).strip()
    cleaned = (text[: match.start()] + text[match.end():]).strip()
    return {"title": title, "body": body}, cleaned

# /file serves ONLY these — raster images the browser renders inertly in an
# <img>. SVG is deliberately excluded: opened full-size it executes scripts
# in the server's origin.
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MEDIA_MAX_BYTES = 20 * 1024 * 1024  # inline base64 limit; larger files fall back to a path
MAX_QUEUE = 5  # messages waiting behind a busy session
RENAME_MAX = 200  # custom chat-title length cap (a title, not a message)

CLOSE_REPLACED = 4000  # another device connected; this socket is superseded
CLOSE_BAD_TOKEN = 4403


class Bridge:
    """Bridges one session's agent worker thread to the event loop.

    Outbound events go through call_soon_threadsafe into `_put`, which appends
    to the transcript and fans the event out to EVERY client currently viewing
    this session (each Client drains its own outbox to its own socket). A
    background session with no viewers still records into its transcript alone
    and surfaces on the next switch. Approval requests additionally block the
    worker on a plain queue.Queue slot until a viewer's answer fills it.

    The transcript buffer and the viewer set are only ever touched on the loop
    thread (inside _put, or in _show which runs on the loop) — every
    viewer-outbox push therefore happens on the loop thread too, so replay
    snapshots and fan-out both need no locking.
    """

    def __init__(self, get_loop):
        self._get_loop = get_loop
        self.viewers: set = set()  # Clients currently viewing this session
        self.pending: dict[str, queue.Queue] = {}
        self.transcript: list[dict] = []
        self.truncated = False

    def emit(self, event: dict, record: bool = True) -> None:
        loop = self._get_loop()
        if loop is None:  # before startup: nothing listening yet
            self._put(event, record)
            return
        loop.call_soon_threadsafe(self._put, event, record)

    def record(self, event: dict) -> None:
        """Loop-thread-only synchronous record (emit() would defer a tick and
        race the replay snapshot taken right after)."""
        self._put(event, True)

    def _put(self, event: dict, record: bool) -> None:
        if record:
            last = self.transcript[-1] if self.transcript else None
            if event["type"] == "token" and last and last["type"] == "token":
                last["text"] += event["text"]
            else:
                self.transcript.append(dict(event))
                if len(self.transcript) > TRANSCRIPT_MAX:
                    del self.transcript[: len(self.transcript) - TRANSCRIPT_KEEP]
                    self.truncated = True
        # Fan the event out to every attached viewer. Runs on the loop thread
        # (call_soon_threadsafe / _show), so mutating a Client's asyncio queue
        # is safe and the viewer set can't change underfoot.
        for client in self.viewers:
            client.outbox.put_nowait(event)

    def ask(self, event: dict) -> dict:
        """Emit an approval_request (id added in place) and block the calling
        worker thread until answer() delivers the client's decision. No
        timeout by design: an unanswered approval simply waits — the request
        stays in the transcript and reappears when this session is shown."""
        event["id"] = uid = uuid.uuid4().hex
        slot: queue.Queue = queue.Queue(maxsize=1)
        self.pending[uid] = slot
        self.emit(event)
        try:
            return slot.get()
        finally:
            self.pending.pop(uid, None)

    def answer(self, uid: str, value: dict) -> bool:
        slot = self.pending.get(uid)
        if slot is None:
            return False  # stale/duplicate answer (already answered, or gone)
        try:
            slot.put_nowait(value)
        except queue.Full:
            return False
        return True


class StreamCoalescer:
    """Batches a run_command's per-line output into fewer, larger `stream`
    events for the browser (issue #109). A command with tens of thousands of
    lines otherwise emits one WebSocket event — and one frontend DOM append +
    reflow — per line, locking the tab up.

    LIVE-ONLY: this changes only the granularity of live `stream` events. It
    never touches what is logged (the `tool` step's truncated output) or what
    `SessionLog.reconstruct_events` replays cold (that splices the tool output
    as a single `stream`), so hot/cold trace parity is preserved — the frontend
    re-splits joined text on '\\n', rendering identically either way.

    Flushes on whichever comes first: MAX_LINES buffered, MAX_BYTES buffered,
    MAX_DELAY since the first buffered line, or an explicit flush() at command
    end. The time-based flush keeps slow output responsive (a lone line still
    lands within MAX_DELAY instead of waiting for the next line)."""

    MAX_LINES = 50
    MAX_BYTES = 16 * 1024
    MAX_DELAY = 0.1  # seconds

    def __init__(self, emit_text: Callable[[str], None]) -> None:
        self._emit_text = emit_text
        self._buf: list[str] = []
        self._bytes = 0
        self._timer: threading.Timer | None = None
        # on_line runs on the worker thread, the delay flush on a Timer thread —
        # guard the buffer so they can't interleave.
        self._lock = threading.Lock()

    def line(self, text: str) -> None:
        with self._lock:
            self._buf.append(text)
            self._bytes += len(text) + 1  # +1 for the joining newline
            if len(self._buf) >= self.MAX_LINES or self._bytes >= self.MAX_BYTES:
                self._flush_locked()
            elif self._timer is None:
                self._timer = threading.Timer(self.MAX_DELAY, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        """Emit any buffered remainder — called at command end so no trailing
        output is lost."""
        with self._lock:
            self._flush_locked()

    def _flush(self) -> None:  # Timer-thread entry point
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._buf:
            return
        text = "\n".join(self._buf)
        self._buf = []
        self._bytes = 0
        self._emit_text(text)


class WebStatus:
    """Live-ticker sink: phase changes forwarded as unrecorded status events
    (they describe the current moment, so replay skips them)."""

    THROTTLE_SECS = 0.5

    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self._label = ""
        self._tokens = 0
        self._last = 0.0

    def start(self, label: str) -> None:
        self._label = label
        self._tokens = 0
        self._last = time.monotonic()
        self.bridge.emit({"type": "status", "state": "working", "label": label}, record=False)

    def add_tokens(self, count: int) -> None:
        self._tokens += count
        now = time.monotonic()
        if now - self._last >= self.THROTTLE_SECS:
            self._last = now
            self.bridge.emit(
                {
                    "type": "status",
                    "state": "working",
                    "label": self._label,
                    "tokens": self._tokens,
                },
                record=False,
            )

    def stop(self) -> None:
        self.bridge.emit({"type": "status", "state": "idle"}, record=False)


def make_web_approvers(bridge, logref, allow_path, deny_path, ask_all, get_scope, trust_dir):
    """The three approval callbacks, backed by browser round trips. Mirrors
    cli.make_approver semantics exactly: denylist first (also on edited
    commands), then auto-approval scoped to the live session roots, then a
    blocking approval card. session_prefixes backs the card's "Allow this
    session" button — in-memory only, forgotten when the session closes;
    "Always allow" saves the card's shown prefixes to the persistent
    allowlist, same file as the CLI's 'a' answer.
    trust_dir(path) -> note widens the live roots when the card's "Trust
    directory" button answers a command or read escaping them."""
    session_prefixes: set[str] = set()

    def known_prefixes() -> frozenset:
        return frozenset(load_prefixes(allow_path)) | session_prefixes

    def record(command: str, decision: str) -> None:
        logref.command(command, decision)

    def resolve(uid: str, decision: str, comment: str = "") -> None:
        event = {"type": "approval_resolved", "id": uid, "decision": decision}
        if comment:
            event["comment"] = comment
        bridge.emit(event)

    def blocked(command: str, reason: str) -> Blocked:
        bridge.emit({"type": "echo", "text": f"✗ blocked ({reason}): {command}"})
        return Blocked(reason)

    def ask_approval(command: str):
        reason = check_denied(command, load_prefixes(deny_path))
        if reason:
            record(command, f"blocked: {reason}")
            return blocked(command, reason)

        cwd, roots = get_scope()
        if not ask_all and is_auto_approvable(
            command, known_prefixes(), cwd=cwd, roots=roots
        ):
            bridge.emit({"type": "echo", "text": f"✓ auto-approved: {command}"})
            record(command, "auto")
            return command

        suggestions = [
            suggest_prefix(segment)
            for segment in unvetted_segments(command, known_prefixes()) or [command]
        ]
        escapes = escaping_dirs(command, cwd, roots) if cwd and roots else []
        request: dict[str, Any] = {
            "type": "approval_request",
            "kind": "command",
            "command": command,
            "destructive": looks_destructive(command),
            "prefixes": suggestions,
            "escapes": escapes,
        }
        answer = bridge.ask(request)
        action = answer.get("action")
        # Feedback is button-agnostic: on deny it explains the refusal, on any
        # approval it rides along as guidance the model applies going forward.
        comment = str(answer.get("comment") or "").strip()

        def tagged(decision: str) -> str:
            return f"{decision} (feedback: {comment})" if comment else decision

        def granted(final: str = command):
            return Approved(comment, final) if comment else final

        if action == "approve":
            record(command, tagged("approved"))
            resolve(request["id"], "approved", comment)
            return granted()
        if action == "approve_trust" and escapes:
            notes = [trust_dir(directory) for directory in escapes]
            bridge.emit({"type": "echo", "text": "✓ " + "; ".join(notes)})
            record(command, tagged(f"approved+trusted:{','.join(escapes)}"))
            resolve(request["id"], "approved", comment)
            return granted()
        if action == "approve_session":
            session_prefixes.update(suggestions)
            bridge.emit(
                {"type": "echo", "text": f"✓ session-allowed: {', '.join(suggestions)}"}
            )
            record(command, tagged("approved+session"))
            resolve(request["id"], "approved", comment)
            return granted()
        if action == "approve_always":
            for prefix in suggestions:
                save_prefix(allow_path, prefix)
            bridge.emit(
                {"type": "echo", "text": f"✓ always-allowed: {', '.join(suggestions)}"}
            )
            record(command, tagged(f"approved+always:{','.join(suggestions)}"))
            resolve(request["id"], "approved", comment)
            return granted()
        if action == "edit":
            edited = str(answer.get("command") or "").strip()
            if edited:
                # The denylist stays authoritative even for an edit — otherwise
                # `ls` could be edited into `rm -rf /` and run unchecked.
                reason = check_denied(edited, load_prefixes(deny_path))
                if reason:
                    record(f"{command} => {edited}", f"blocked: {reason}")
                    resolve(request["id"], "denied")
                    return blocked(edited, reason)
                record(f"{command} => {edited}", tagged("edited"))
                resolve(request["id"], "edited", comment)
                return granted(edited)
        record(command, tagged("denied"))
        resolve(request["id"], "denied", comment)
        return Denied(comment) if comment else None

    def approve_write(plan) -> "bool | Approved | Denied":
        verb = "create" if plan.is_new else "edit"
        request: dict[str, Any] = {
            "type": "approval_request",
            "kind": "write",
            "verb": verb,
            "target": str(plan.target),
            "diff": plan.diff,
            "added": plan.added,
            "removed": plan.removed,
        }
        answer = bridge.ask(request)
        approved = answer.get("action") == "approve"
        comment = str(answer.get("comment") or "").strip()
        decision = "approved" if approved else "denied"
        record(
            f"{verb} {plan.target}",
            f"{decision} (feedback: {comment})" if comment else decision,
        )
        resolve(request["id"], decision, comment)
        if approved:
            return Approved(comment) if comment else True
        return Denied(comment) if comment else False

    def approve_read(path: str, reason: str = "sensitive") -> bool:
        directory = os.path.dirname(os.path.expanduser(path)) or "."
        escapes = [directory] if reason == "outside" else []
        request: dict[str, Any] = {
            "type": "approval_request",
            "kind": "read",
            "path": path,
            "reason": reason,
            "escapes": escapes,
        }
        answer = bridge.ask(request)
        action = answer.get("action")
        if action == "approve_trust" and escapes:
            bridge.emit({"type": "echo", "text": f"✓ {trust_dir(directory)}"})
            record(f"read {path}", f"approved+trusted:{directory}")
            resolve(request["id"], "approved")
            return True
        approved = action == "approve"
        record(f"read {path}", "approved" if approved else "denied")
        resolve(request["id"], "approved" if approved else "denied")
        return approved

    def approve_tool(name: str, args: dict) -> "bool | Approved | Denied":
        # Reuses the command card verbatim (issue #141): same approve/deny +
        # comment verdicts, no denylist/auto-approval — a mutating tool always
        # prompts. Comment semantics match commands: deny+comment = STOP,
        # approve+comment = HOLD-and-adjust.
        request: dict[str, Any] = {
            "type": "approval_request",
            "kind": "tool",
            "tool": name,
            "args": args,
        }
        answer = bridge.ask(request)
        approved = answer.get("action") == "approve"
        comment = str(answer.get("comment") or "").strip()
        decision = "approved" if approved else "denied"
        shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
        record(
            f"tool {name}({shown})",
            f"{decision} (feedback: {comment})" if comment else decision,
        )
        resolve(request["id"], decision, comment)
        if approved:
            return Approved(comment) if comment else True
        return Denied(comment) if comment else False

    def approve_import(name, description, files, skipped, flags, dest):
        # One consolidated review of the WHOLE skill (#139): every file's
        # contents (rendered syntax-highlighted by app.js) + risk flags, one
        # decision. Untrusted code is reviewed as whole files, not diffs.
        request: dict[str, Any] = {
            "type": "approval_request",
            "kind": "import",
            "skill": name,
            "description": description,
            "files": files,
            "skipped": skipped,
            "flags": flags,
            "dest": dest,
        }
        answer = bridge.ask(request)
        approved = answer.get("action") == "approve"
        comment = str(answer.get("comment") or "").strip()
        decision = "approved" if approved else "denied"
        record(f"import skill {name}", f"{decision} (feedback: {comment})" if comment else decision)
        resolve(request["id"], decision, comment)
        if approved:
            return Approved(comment) if comment else True
        return Denied(comment) if comment else False

    return ask_approval, approve_write, approve_read, approve_tool, approve_import


def list_files(cwd: str, query: str, ignore: Sequence[str] | None = None) -> list[str]:
    """Project paths for @-mention completion — the same walk, cap, and scoring
    as the TUI's AtFileCompleter. The junk-dir skiplist is the SAME configurable
    directory-picker ignore list (#87), so there's one place to edit; defaults
    apply when no list is passed."""
    patterns = list(ignore) if ignore is not None else list(dir_ignore.DEFAULT_IGNORE)
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(cwd, onerror=lambda _e: None):
        dirnames[:] = sorted(d for d in dirnames if not dir_ignore.matches(d, patterns, True))
        rel = os.path.relpath(dirpath, cwd)
        prefix = "" if rel == "." else rel + "/"
        paths.extend(prefix + d + "/" for d in dirnames)
        paths.extend(prefix + f for f in sorted(filenames))
        if len(paths) >= ATFILE_SCAN_CAP:
            del paths[ATFILE_SCAN_CAP:]
            break
    needle = query.casefold()
    scored = []
    for path in paths:
        name = os.path.basename(path.rstrip("/")).casefold()
        if not needle:
            score = 1
        elif name.startswith(needle):
            score = 3
        elif needle in name:
            score = 2
        elif needle in path.casefold():
            score = 1
        else:
            continue
        scored.append((-score, path))
    scored.sort()
    return [path for _, path in scored[:ATFILE_MAX_RESULTS]]


def web_usage_context(model, provider, allow_path, deny_path, state_dir) -> str:
    """Self-knowledge for the system prompt, web-UI edition — aish should
    describe the interface the user is actually looking at."""
    return f"""\
About aish (you) — use this to answer questions about your own usage:
{identity_context(model, provider)}
- The user talks to you through the aish WEB UI in a browser (often a phone), \
not a terminal. Every command you propose appears as an approval card with \
Approve / Allow this session / Always allow / Deny buttons and a pencil \
icon beside the command to edit it before running; file writes show a \
unified diff before approval. Cards also carry an optional \
comment field whose text arrives with WHICHEVER button the user presses, and \
approve vs deny then mean opposite things. APPROVE + comment = continue, but \
ADJUST: the original command is NOT run — adjust it to what the user asked and \
propose the adjusted command (it is approved again before it runs). DENY + \
comment = STOP: reply in plain text addressing the concern, then wait — run \
nothing else first. Read-only commands auto-approve within the \
session roots (allowlist: {allow_path}). "Allow this session" auto-approves \
that command's prefixes until the session closes — in memory only. "Always \
allow" saves those same prefixes to the persistent allowlist file. When a \
command or file read reaches outside the session roots, its card warns about \
the escape and offers "Trust directory": one tap adds that directory to the \
session roots, so allowlisted work there auto-approves afterwards — also in \
memory only.
- A message the user prefixes with ! runs directly as a shell command — their \
own action, without you and without an approval card (just as in the terminal); \
!cd <dir> is the /cd alias that moves the project directory. For commands that \
need to READ input (gcloud auth, ssh host-key prompts, sudo passwords), the ＋ \
menu's "Interactive shell" opens a real pseudo-terminal the user types into \
directly — you have NO access to it: its input and output stay private to that \
terminal unless the user explicitly taps "Share" to inject a selection into \
your context. A message starting \
with /learn \
distills the conversation into saved skills/memory (an optional hint \
follows, e.g. "/learn the gh flow"; "/learn lessons" migrates the legacy \
lessons file); the composer also accepts /model /resume /delete /new /fork \
/cd /add-dir /jobs /help. To branch the conversation and explore a tangent \
without touching the current thread, the user types /fork (or /branch): it \
copies the whole conversation so far into a NEW session and switches there, \
leaving this one untouched — tell them to use it when they want to try an \
alternative approach or a side question and keep the main chat clean. Header \
controls: a "‹ Sessions" back button (top left, \
with a badge when a background session needs attention) opens the sessions \
drawer; the centered session title opens a menu (new chat, rename this chat, \
switch model, change directory, line wrap, export the chat to PDF, delete \
this chat, workspace & jobs); the compose pencil (top right) starts a new \
chat. Every \
finished answer has a row of chips beneath it — copy, export that one answer \
to PDF, and (where available) read-aloud. Both PDF exports render markdown \
locally and download the file; the whole-chat export includes only your final \
answers, not thinking or intermediate steps. Exported PDFs embed pictures: \
local image paths inside the session's directories, web images, Google Maps \
snapshots (needs GOOGLE_MAPS_API_KEY set), and YouTube thumbnails are inlined; \
anything unavailable becomes a captioned link card. A \
context bar under the title shows the working directory \
(tap to open a folder picker) and the model (tap to switch). In the composer, \
the ＋ button opens attach file / reference a path (@) / slash command (/) / \
photo / send feedback / terminal mode (multi-command shell, prefix !) / \
interactive shell (a real TTY for programs that prompt for input). \
Your tool activity (thinking, recalled knowledge, commands and their \
output) is grouped into one collapsible activity trace per turn. Swiping the \
transcript sideways pages through recent chats.
- Several sessions can be open at once; a task keeps running when the user \
switches to another session and its result is there when they switch back. \
While you work, messages the user sends are QUEUED and run one after \
another; the user can also press Stop to cancel your current task — a \
"(task stopped by user)" note means exactly that, so do not treat it as an \
error.
- QUICK REPLIES: you CAN turn a question into tap buttons, and the user \
EXPECTS them — the web UI renders them the same on phone and desktop. \
Whenever you end a message with a question whose \
likely answers are a few short options (yes/no, pick-one, a short menu), you \
MUST append one markdown link per option, each on its own line, formatted \
[Label](aish-reply://answer text) — the UI renders each as a tap button that \
sends "answer text" as the user's reply IMMEDIATELY on tap (one-tap, no extra \
send press), so write each payload as a complete, ready-to-send message. If you \
instead want a chip that only PRE-FILLS the box for the user to finish typing, \
end its payload with a colon or trailing space (e.g. "add details: "). Asking \
in prose alone does NOT \
create buttons; you must add the link lines too. Example: after \
"Proceed with the deploy?" end with [Yes, deploy](aish-reply://yes, deploy \
now) and [No, hold off](aish-reply://no, hold off). If you end on a question \
with NO chips, a safety net appends generic Yes/No/Tell-me-more buttons — so \
add your own tailored chips to do better. When the question is genuinely \
open-ended (no small set of options fits), you MUST end the message with the \
literal tag [no-chips] to suppress the net; the tag is hidden from the user. \
NEVER generate a chip whose only purpose is to end the conversation — the \
user can end the chat anytime without your help, so a chip that just says \
goodbye wastes the space. Bad: [Thanks, that's all!](aish-reply://thanks, \
that's all), [Finish this chat](aish-reply://finish this chat), \
[Dzięki, to wszystko!](aish-reply://dzięki, to wszystko). Good: every chip \
MUST offer a useful next step — a continuation of the task, an alternative \
pathway, or a concrete next action, e.g. after finishing a deploy end with \
[Run the smoke tests](aish-reply://run the smoke tests) or \
[Show me the logs](aish-reply://show me the logs) instead of a sign-off.
- SHOWING IMAGES: you CAN display images — markdown image syntax renders \
inline in the chat, and the user EXPECTS to see pictures this way. Whenever \
your answer involves an image the user would want to look at — a chart or \
diagram you just generated, a plot, a downloaded picture — you MUST embed \
it: ![caption](/absolute/path.png) for a local file (png/jpg/gif/webp \
inside the session roots), ![caption](https://…) for a web image. \
Mentioning the file path in prose does NOT show the picture; always add \
the image line too. Example: after saving /tmp/work/plot.png, end with \
![plot](/tmp/work/plot.png).
- Safety denylist: unrecoverable command classes are blocked outright and \
cannot be approved here at all (extendable in {deny_path}); suggest a safer \
alternative when blocked.
- Sessions: conversation + command audit trail logged to {state_dir} — the \
same format as terminal aish, so sessions are interchangeable between both. \
Each drawer row has a trash icon: tap it, then its "Delete?" confirm, to \
permanently delete that session (conversation and audit log; refused while \
the session is running; deleting the current chat lands on a fresh one). \
The session-title menu also has a "Delete chat" item (same two-tap \
"Confirm delete") that deletes the chat you are currently in, and a \
"Rename chat…" item that gives the current chat a custom title (an inline \
field; the terminal equivalent is the /rename <title> command). A custom \
title overrides the one auto-derived from the first message and shows in the \
drawer, the /resume picker, and this header. \
When the user refers to earlier work ("the fix from yesterday", "what went \
wrong last time"), use the recall tool to find and read the \
relevant past conversation instead of asking them to repeat it.
- File tools: prefer read_file/write_file/edit_file over cat/sed/heredocs; \
the user approves a diff card before any write. Do NOT use sed -i or > \
redirects to edit files.
- Scratch workspace: you MUST stage throwaway files (a gh issue or PR body, a \
commit message, an intermediate patch or artifact) in the private scratch \
directory named in your system-prompt rules — writing, editing, and deleting \
there is AUTO-APPROVED (no card) and the whole directory is wiped when the \
session ends. Everything OUTSIDE it still needs approval as usual.
- Attachments: the web UI can upload files. Images (and PDFs, when your \
backend supports them) are delivered to you NATIVELY — a "[image attached: \
… — you can see it]" note means the image itself is in the message: look at \
it directly (describe it, read text in it, use what you see to search the \
web); do NOT write scripts to parse it. Files that arrive as plain \
"[attached file: <path>]" lines were NOT delivered natively: read text \
files with read_file, process binaries with shell tools — in that mode you \
cannot see image contents, and should say so if asked to describe one."""


class Client:
    """One WebSocket connection. Per-connection state that used to live on
    WebServer (the socket, its sender task, and which session it shows) lives
    here so N connections coexist without preempting each other. Each Client
    drains its OWN outbox to its OWN socket, so a session's events fan out to
    every viewer independently."""

    def __init__(self, ws: WebSocket):
        self.id = uuid.uuid4().hex
        self.ws = ws
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.viewing: Session | None = None
        self.sender: asyncio.Task | None = None


class Session:
    """One open conversation: its own agent, log, transcript, and busy flag."""

    def __init__(self, agent, logref: LogRef, bridge: Bridge):
        self.agent = agent
        self.logref = logref
        self.bridge = bridge
        self.busy = False
        self.runner: asyncio.Task | None = None
        self.queue: list[tuple[str, list[str]]] = []  # (text, attachments) waiting
        self.pending_cwd: str | None = None  # a /cd requested while busy; applied after
        self.pending_retry: str | None = None  # a retry requested while busy; run after stop
        # A pre-reviewed issue draft ({title, body}) from a text-only /feedback,
        # stashed for a {type:create_issue} confirm (#110). Never model-derived
        # at click time — this is the exact text the user reviewed in the card.
        self.pending_issue: dict[str, str] | None = None
        # True while a text-only /feedback (block flow) is being drafted or
        # adjusted: an attachment arriving in that window auto-switches the
        # feedback to the classic upload-capable flow (#130). Cleared on the
        # switch and when the drafted issue is filed.
        self.feedback_block = False
        self.last_shown = time.monotonic()
        self.custom_title: str | None = None  # a user-set name; overrides the derived title
        # last-actor-drives (#102): whoever last performed a session-affecting
        # action. Observers viewing this session see a "another tab is active"
        # hint; acting claims control. Never persisted — replay re-derives it.
        self.controller: Client | None = None

    @property
    def viewers(self) -> set:
        """Clients currently viewing this session. Owned by the bridge (it fans
        events out to them); exposed here so callers read it off the Session."""
        return self.bridge.viewers

    @property
    def name(self) -> str:
        return self.logref.log.path.name

    def state(self) -> str:
        if self.busy:
            return "waiting" if self.bridge.pending else "running"
        return "idle"

    def close(self) -> None:
        # The interactive console is GLOBAL (WebServer.console), not owned by any
        # session — a session going away never touches it (issue #148 follow-up).
        self.logref.log.close()
        self.agent.close()  # best-effort scratch-workspace cleanup (issue #70)


class WebServer:
    """Per-process SHARED state: open sessions and the connected clients.

    Multi-connection (#102): N sockets (phone, laptop, headless test) coexist
    without preempting. Per-connection state — the socket, its sender, and which
    session it shows — lives on each Client; the WebServer holds only what is
    shared across them. A default session is opened at startup and is where a
    bare (no ?session=) connection lands."""

    def __init__(
        self,
        open_session,
        state_dir,
        config_path,
        token,
        dir_ignore_patterns=None,
        console_command=None,
    ):
        self.open_session = open_session  # (path | None) -> Session
        self.state_dir = state_dir
        self.uploads_dir = state_dir / "uploads"
        self.config_path = config_path
        self.token = token
        # gitignore-style names hidden in the folder browser + @-file index (#87);
        # user-editable via config.toml [directory_picker], defaults otherwise.
        self.dir_ignore = list(dir_ignore_patterns or dir_ignore.DEFAULT_IGNORE)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sessions: dict[str, Session] = {}
        self.clients: set[Client] = set()
        self._default: Session | None = None  # bare-connection landing session
        # The single GLOBAL interactive console (issue #148 follow-up), shared by
        # every connection. Held here, NEVER on a Session — the model has no
        # reference and no write path (the load-bearing security invariant).
        self.console: PtySession | None = None
        self.console_viewers: set[Client] = set()  # clients with the overlay open
        self.console_tmux = False  # True once spawned tmux-backed (restart-surviving)
        # Injectable spawn command (tests pass a trivial echo loop so no tmux/shell
        # is needed); None → auto-detect tmux-or-$SHELL at first open.
        self.console_command = console_command

    @property
    def active(self) -> Session:
        """A representative session for HTTP endpoints (no socket context) and
        for tests. With one connection this is that client's view; otherwise it
        falls back to the default startup session. Never None after startup."""
        for client in self.clients:
            if client.viewing is not None:
                return client.viewing
        assert self._default is not None, "no active session yet"
        return self._default

    async def startup(self) -> None:
        self.loop = asyncio.get_running_loop()

    async def shutdown(self) -> None:
        """Unblock everything so Ctrl-C exits promptly: workers parked on an
        approval slot would otherwise wait forever and keep the interpreter
        alive. Denials are recorded in the audit log like any other deny."""
        for session in self.sessions.values():
            for uid in list(session.bridge.pending):
                session.bridge.answer(uid, {"action": "deny"})
        # Kill ONLY the console PTY (the tmux CLIENT) — this detaches; the tmux
        # SESSION and everything running in it survive on the tmux server, so it
        # reattaches on the next aish-web start (#148 follow-up). Without tmux the
        # $SHELL child just dies with the server. NEVER `tmux kill-session` here.
        if self.console is not None:
            self.console.kill()
            self.console = None
        for client in list(self.clients):
            with contextlib.suppress(Exception):
                await client.ws.close()

    def add_session(self, session: Session, default: bool = True) -> None:
        self.sessions[session.name] = session
        if default:
            self._default = session

    def _evict_idle(self) -> None:
        """Close the longest-idle background session past the cap. Sessions that
        are busy, have a viewer, or are the default landing session are never
        closed — the cap can be exceeded by work or by open views (#102)."""
        while len(self.sessions) >= MAX_OPEN_SESSIONS:
            idle = [
                s for s in self.sessions.values()
                if not s.busy and not s.viewers and s is not self._default
            ]
            if not idle:
                return
            oldest = min(idle, key=lambda s: s.last_shown)
            oldest.close()
            del self.sessions[oldest.name]

    def _claim(self, client: Client) -> None:
        """last-actor-drives (#102): an action from `client` stamps it as the
        controller of the session it is viewing. Broadcasts a fresh `role` to
        that session's viewers when control actually changes, so every tab
        learns whether IT is now the driver. A no-op if it was already driving
        (idempotent — most actions in a row come from the same tab)."""
        session = client.viewing
        if session is None or session.controller is client:
            return
        session.controller = client
        self._broadcast_role(session)

    @staticmethod
    def _broadcast_role(session: Session) -> None:
        """Tell each viewer who currently drives `session`. NON-recorded (like
        status/cwd_changed): control is live membership state, so a cold replay
        re-derives it rather than resurrecting a stale controller. Pushed
        straight to each viewer's outbox — callers run on the loop thread."""
        controller = session.controller
        cid = controller.id if controller is not None else None
        for viewer in session.viewers:
            viewer.outbox.put_nowait(
                {
                    "type": "role",
                    "session": session.name,
                    "controller": cid,
                    "you": controller is viewer,
                }
            )

    @staticmethod
    def _title(session: Session) -> str:
        """The conversation title, same derivation as the sessions drawer
        (SessionLog._derive_title) so a ! command reads as '! <cmd>' rather than
        its internal '[I ran … myself]' annotation. A custom name (rename)
        overrides the derivation. '' while still empty and never renamed."""
        if session.custom_title:
            return session.custom_title[:80]
        messages = session.agent.messages[1:]
        if not any(m.get("role") == "user" for m in messages):
            return ""
        return SessionLog._derive_title(messages)[:80]

    def _hello(
        self,
        session: Session,
        pager: list[tuple[str, str]] | None = None,
        cmd_history: list[str] | None = None,
    ) -> dict:
        # The swipe pager pages through recent chats oldest→newest by last
        # interaction — open or not; resume loads cold ones from disk. The
        # current session is always a page even before its first message
        # (a fresh chat is the newest thing by definition).
        pages = [{"name": name, "title": title} for name, title in pager or []]
        if all(page["name"] != session.name for page in pages):
            pages.append({"name": session.name, "title": self._title(session)})
        return {
            "type": "hello",
            "model": model_spec(session.agent),
            "session": session.name,
            "title": self._title(session),
            "log_path": str(session.logref.log.path),  # /session + "Copy log path" (#146)
            "busy": session.busy,
            "cwd": session.agent.cwd,
            "roots": [str(root) for root in session.agent.roots],
            "home": str(Path.home()),  # client abbreviates paths to ~
            "rev": STATIC_REV,
            "pager": pages,
            # The user's own successful ! commands, cross-session, most-run first:
            # terminal-mode autocomplete draws from this personal palette (#104).
            "cmd_history": cmd_history or [],
        }

    @staticmethod
    def _cwd_event(session: Session) -> dict:
        return {
            "type": "cwd_changed",
            "cwd": session.agent.cwd,
            "roots": [str(root) for root in session.agent.roots],
        }

    async def handle_ws(self, websocket: WebSocket) -> None:
        if self.token and websocket.query_params.get("token") != self.token:
            # Accept, THEN close: refusing the handshake would reach the
            # browser as a generic 1006 and the client couldn't tell a bad
            # token from a dead server (it would just retry forever).
            await websocket.accept()
            await websocket.close(code=CLOSE_BAD_TOKEN)
            return
        await websocket.accept()
        # A new connection NEVER preempts an existing one (#102): it becomes its
        # own Client and coexists. The old single-client CLOSE_REPLACED path is
        # gone — many tabs/devices share the same token and drive by acting.
        client = Client(websocket)
        self.clients.add(client)
        await self._attach(client)
        try:
            while True:
                message = await websocket.receive_json()
                if isinstance(message, dict):
                    await self._handle(client, message)
        except WebSocketDisconnect:
            pass
        finally:
            self._detach(client)

    def _detach(self, client: Client) -> None:
        """Tear a disconnected client down: stop its sender, drop it from the
        session it viewed, and hand off control if it was the driver."""
        if client.sender:
            client.sender.cancel()
            client.sender = None
        self.clients.discard(client)
        # Stop fanning console output at this dead socket. The console itself is
        # NEVER killed on disconnect — it's global and keeps running (#148).
        self.console_viewers.discard(client)
        self._leave(client)

    def _leave(self, client: Client) -> None:
        """Remove `client` from its viewed session's viewer set. If it was that
        session's controller, control is released (controller = None) and a
        fresh `role` tells the remaining viewers nobody is driving now. A plain
        observer leaving changes no one's role, so it emits nothing."""
        session = client.viewing
        if session is None:
            return
        session.viewers.discard(client)
        client.viewing = None
        if session.controller is client:
            session.controller = None
            self._broadcast_role(session)

    async def _attach(self, client: Client) -> None:
        # A reconnecting client names the session it was on (?session=...).
        # Without this, a server restart lands every client in the fresh
        # startup session — silently moving the user out of their chat.
        wanted = client.ws.query_params.get("session", "")
        session = self._default
        assert session is not None, "no default session yet"
        if wanted and wanted != session.name:
            session = await self._open_by_name(wanted) or session
        await self._show(client, session)

    async def _show(self, client: Client, session: Session) -> None:
        """Point `client` at `session`: hello + full transcript replay, then
        live events from its own outbox. Does NOT affect other clients — each
        views independently (#102)."""
        # Disk scan for the pager happens before the attach block below: it
        # must not sit between joining the viewer set and the transcript
        # snapshot.
        pager = await asyncio.to_thread(SessionLog.pager_titles, self.state_dir)
        # Cross-session command palette for terminal-mode autocomplete (#104),
        # scanned off-thread alongside the pager before the attach block.
        cmd_history = await asyncio.to_thread(
            SessionLog.user_command_history, self.state_dir
        )
        if client.sender:
            client.sender.cancel()
            client.sender = None
        # Leave whatever this client was viewing before pointing it at the new
        # session (drops it from the old viewer set; releases control there).
        self._leave(client)
        client.viewing = session
        session.last_shown = time.monotonic()
        bridge = session.bridge
        # Fresh outbox so events buffered for the previous view don't leak into
        # this one; join the viewer set and snapshot in the SAME synchronous
        # block — no _put can land between join and snapshot (single loop
        # thread), so replay + live stream never duplicate or drop an event.
        client.outbox = asyncio.Queue()
        session.viewers.add(client)
        snapshot = list(bridge.transcript)
        await client.ws.send_json(self._hello(session, pager, cmd_history))
        await client.ws.send_json(
            {"type": "replay", "events": snapshot, "truncated": bridge.truncated}
        )
        # The queued-cwd card is backend-authoritative (single pending_cwd), so
        # it's reconstructed on attach rather than replayed from the transcript
        # (#92) — this survives reconnects and session switches. Sent after the
        # replay so it lands on top of the freshly-rebuilt queue list.
        if session.pending_cwd:
            await client.ws.send_json({"type": "cwd_queued", "path": session.pending_cwd})
        client.sender = asyncio.ensure_future(self._send_loop(client))
        # A viewer joined: announce role ONLY when someone is already driving,
        # so the fresh tab learns it's an observer. With no controller yet
        # (the common single-connection case) the frontend's default is already
        # "no indicator", so an all-null role would be pure noise.
        if session.controller is not None:
            self._broadcast_role(session)

    async def _send_loop(self, client: Client) -> None:
        try:
            while True:
                event = await client.outbox.get()
                await client.ws.send_json(event)
        except Exception:  # noqa: BLE001 — a dead socket ends the loop; replay recovers
            pass

    async def _handle(self, client: Client, message: dict) -> None:
        # ACTION messages (#102) claim control of the client's viewed session
        # before executing (last-actor-drives); VIEW messages — switching which
        # session is shown, file/jobs queries, dequeue, reconnect — never do.
        kind = message.get("type")
        if kind == "task":
            attachments = [
                str(p) for p in (message.get("attachments") or []) if isinstance(p, str)
            ]
            self._claim(client)
            await self._start_task(
                client, str(message.get("text", "")).strip(), attachments
            )
        elif kind == "approval":
            uid = str(message.get("id", ""))
            for session in self.sessions.values():
                if session.bridge.answer(uid, message):
                    break
            # Answering the gate is an action — claim control (the card lives in
            # the client's own view). The event loop serializes all incoming
            # messages, so exactly one answer() ever fills the blocked slot;
            # answer()'s pending-slot guard drops any duplicate.
            self._claim(client)
        elif kind == "sessions":
            await self._send_sessions(client, str(message.get("query", "")))
        elif kind == "resume":
            await self._resume(client, str(message.get("path", "")))
        elif kind == "new":
            await self._new_session(client)
        elif kind == "fork":
            after = message.get("after")
            self._claim(client)
            await self._fork_session(client, after if isinstance(after, int) else None)
        elif kind == "delete_session":
            await self._delete_session(client, str(message.get("name", "")))
        elif kind == "rename_session":
            self._claim(client)
            await self._rename_session(
                client, str(message.get("name", "")), str(message.get("title", ""))
            )
        elif kind == "models":
            await self._send_models(client, str(message.get("query", "")))
        elif kind == "set_model":
            self._claim(client)
            await self._set_model(client, message)
        elif kind == "cd":
            self._claim(client)
            await self._cd(client, str(message.get("path", "")).strip())
        elif kind == "add_dir":
            self._claim(client)
            await self._add_dir(client, str(message.get("path", "")).strip())
        elif kind == "jobs":
            await client.ws.send_json({"type": "job_list", "text": tools.jobs_table()})
        elif kind == "files":
            await self._send_files(client, str(message.get("query", "")))
        elif kind == "stop":
            self._claim(client)
            await self._stop_task(client)
        elif kind == "retry":
            self._claim(client)
            await self._retry_task(client, str(message.get("text", "")).strip())
        elif kind == "create_issue":
            self._claim(client)
            await self._create_issue(client)
        elif kind == "console_open":
            # Open/attach the GLOBAL console (spawns it if not already running).
            # Viewing the console is not a session action, so it does NOT claim
            # control of whatever chat this client happens to show.
            await self._console_open(client)
        elif kind == "console_in":
            # Keystrokes from the USER's own socket → the console PTY. This is the
            # ONLY path to console input (issue #148): no model/agent code reaches
            # it.
            self._console_in(client, message.get("data", ""))
        elif kind == "console_resize":
            self._console_resize(
                client, message.get("cols", 80), message.get("rows", 24)
            )
        elif kind == "console_close":
            # Hide/detach only: stop this client viewing; the console keeps
            # running (the Quake-console lifetime is server-scoped, not per-tab).
            self.console_viewers.discard(client)
        elif kind == "console_kill":
            # Explicit "kill the console" (distinct from Close). Actually destroys
            # it — for tmux that means the surviving session too.
            await self._console_kill(client)
        elif kind == "console_share":
            # Explicit "share this selection to the model" (issue #148): console
            # I/O is private by default; only this user action injects a slice of
            # it into the CURRENTLY-VIEWED chat's context, via the same
            # user-message path as `!`. Claims control of that chat like any edit.
            self._claim(client)
            await self._console_share(client, str(message.get("text", "")))
        elif kind == "dequeue":
            self._dequeue(client, str(message.get("text", "")))
        elif kind == "dequeue_cwd":
            viewed = client.viewing
            if viewed is not None:
                viewed.pending_cwd = None
                viewed.bridge.emit({"type": "cwd_dequeued"}, record=False)
        elif kind == "client_debug":
            # Device-side diagnostics (viewport state on iOS, etc.) — printed
            # to the server log because the phone has no reachable console.
            print(f"CLIENT_DEBUG: {message.get('text', '')}", flush=True)
        else:
            await client.ws.send_json(
                {"type": "error", "text": f"unknown message type {kind!r}"}
            )

    async def _reject_busy(self, client: Client, session: Session) -> bool:
        if session.busy:
            await client.ws.send_json(
                {
                    "type": "error",
                    "text": "this session is busy — wait, or start a new "
                    "session (＋) and work there in parallel",
                }
            )
            return True
        return False

    def _classify_attachments(
        self, agent, paths: list[str]
    ) -> tuple[list[str], list[str], list[str]]:
        """(native images, native documents, text notes). Only files inside
        the uploads dir qualify for native delivery — an arbitrary client
        path must never be silently base64'd off the machine. Everything
        else (unsupported type/backend, oversized, outside uploads) becomes
        a path note the agent handles through the normal gated tools."""
        support = backends.media_support(getattr(agent, "provider", "ollama"))
        uploads = self.uploads_dir.resolve()
        images: list[str] = []
        documents: list[str] = []
        notes: list[str] = []
        for raw in paths:
            path = Path(raw)
            try:
                in_uploads = path.resolve().is_relative_to(uploads)
                size_ok = path.is_file() and path.stat().st_size <= MEDIA_MAX_BYTES
            except OSError:
                in_uploads = size_ok = False
            suffix = path.suffix.lower()
            if in_uploads and size_ok and suffix in backends.IMAGE_SUFFIXES and "image" in support:
                images.append(str(path))
                notes.append(f"[image attached: {path.name} — you can see it]")
            elif in_uploads and size_ok and suffix == ".pdf" and "pdf" in support:
                documents.append(str(path))
                notes.append(f"[document attached: {path.name} — you can read it]")
            else:
                notes.append(f"[attached file: {path}]")
        return images, documents, notes

    async def _stop_task(self, client: Client) -> None:
        session = client.viewing
        if session is None:
            return
        if not session.busy:
            # Nothing is running server-side, but the foreground may be wedged
            # showing "working" — e.g. a terminal event that never reached this
            # client. Stop must never dead-end (#48): reconcile the view to the
            # authoritative idle state instead of erroring. A plain `stopped`
            # sync clears busy/working WITHOUT the red "task failed" treatment a
            # real error carries. Sent only to the requesting client (its own
            # foreground); other viewers already track busy via status events.
            session.bridge.emit({"type": "status", "state": "idle"}, record=False)
            await client.ws.send_json({"type": "stopped"})
            return
        if not hasattr(session.agent, "cancel"):
            await client.ws.send_json(
                {"type": "error", "text": "stop is not supported on this backend"}
            )
            return
        session.agent.cancel()
        # A worker parked on an approval card must be unblocked to notice.
        for uid in list(session.bridge.pending):
            session.bridge.answer(uid, {"action": "deny"})
        session.bridge.emit({"type": "echo", "text": "✕ stop requested"})

    async def _retry_task(self, client: Client, text: str) -> None:
        """Regenerate the last answer from scratch (#60): the previous attempt is
        discarded from the model's context, the on-disk log, AND the transcript
        so the rerun is not informed by it. While a turn is still running (or
        wedged on an approval), the rollback can't touch agent.messages under the
        worker thread — cancel first and defer the rerun to _finish_turn, exactly
        how Retry already recovers a stuck turn."""
        session = client.viewing
        if session is None:
            return
        if session.busy:
            session.pending_retry = text
            await self._stop_task(client)
            return
        await self._launch_retry(session, text)

    async def _launch_retry(self, session: Session, client_text: str) -> None:
        # Roll the last user turn out of the model's context and the log; run_task
        # re-adds and re-logs the prompt fresh, so neither the model nor a later
        # cold replay sees the discarded answer. The transcript keeps the user
        # bubble and drops only the answer/trace after it, then a fresh replay
        # re-renders the shortened transcript so the browser matches.
        prompt = session.agent.rewind_last_task() or client_text
        if not prompt:
            return
        session.logref.rewind_last_turn()
        self._rollback_transcript_to_last_user(session)
        # Routed through the outbox (not a direct ws send) so it serializes behind
        # any still-draining events from the cancelled turn — the replay wipes
        # their transient render — and ahead of the rerun's fresh events.
        session.bridge.emit(
            {
                "type": "replay",
                "events": list(session.bridge.transcript),
                "truncated": session.bridge.truncated,
            },
            record=False,
        )
        session.busy = True
        session.runner = asyncio.ensure_future(self._run_task(session, prompt))

    @staticmethod
    def _rollback_transcript_to_last_user(session: Session) -> None:
        """Drop everything after the last `user` event (the discarded answer and
        its trace), keeping the user bubble — the visual half of a retry."""
        transcript = session.bridge.transcript
        for i in range(len(transcript) - 1, -1, -1):
            if transcript[i].get("type") == "user":
                del transcript[i + 1:]
                return

    def _dequeue(self, client: Client, text: str) -> None:
        """Drop the first still-waiting message matching `text` (the client's
        queued-chip remove button). A running task is never affected."""
        session = client.viewing
        if session is None:
            return
        for i, (queued, _attachments) in enumerate(session.queue):
            if queued == text:
                del session.queue[i]
                return

    async def _start_task(
        self, client: Client, text: str, attachments: list[str] | None = None
    ) -> None:
        if not text and not attachments:
            return
        session = client.viewing
        if session is None:
            return
        if session.busy:
            if len(session.queue) >= MAX_QUEUE:
                await client.ws.send_json(
                    {"type": "error", "text": f"queue full ({MAX_QUEUE} waiting)"}
                )
                return
            session.queue.append((text, attachments or []))
            # The queued chip is the requesting client's own composer echo — the
            # message hasn't run yet, so only that client needs it.
            await client.ws.send_json(
                {"type": "queued", "position": len(session.queue), "text": text}
            )
            return
        self._launch(session, text, attachments or [])

    def _launch(self, session: Session, text: str, attachments: list[str]) -> None:
        # A ! prefix runs the typed text directly as a shell command — the
        # user's own action, no model and no approval gate — mirroring the CLI's
        # ! escape (cli.main). It is checked before the / slash handling and the
        # model task path so a general !command never reaches the model; !cd
        # stays the /cd alias and is dispatched below inside _run_user_command.
        if text.startswith("!"):
            session.busy = True
            session.bridge.emit({"type": "user", "text": text})
            session.runner = asyncio.ensure_future(
                self._run_user_command(session, text[1:].strip())
            )
            return
        # Attachments classify at start time so a model switch while queued
        # is honored (vision support is per-backend).
        images, documents, notes = self._classify_attachments(session.agent, attachments)
        if notes:
            text = f"{text}\n\n" + "\n".join(notes) if text else "\n".join(notes)
        session.busy = True
        session.bridge.emit({"type": "user", "text": text})
        if text.startswith("/"):
            # /learn and /feedback are the task-expanding slash commands on web:
            # the transcript shows what the user typed, the model gets the
            # expanded prompt (distillation, or the feedback issue-filing flow).
            # Attachments gate the feedback flavour (#110): text-only feedback
            # uses the backend-owned aish-issue block flow (block_flow=True);
            # feedback WITH attachments keeps the classic model-driven flow so
            # the model runs `gh issue create` (gated) with the asset-upload
            # workflow the text path doesn't handle — its draft lists the
            # assets for confirm/deselect before any public upload (#130).
            expanded = parse_learn(text, getattr(session.agent, "lessons_path", None))
            if expanded is None:
                expanded = parse_feedback(
                    text, block_flow=not attachments, attachments=bool(attachments)
                )
                if expanded is not None:
                    # Remember the flavour: a block-flow draft still being
                    # adjusted switches to classic if attachments arrive (#130).
                    session.feedback_block = not attachments
            if expanded is not None:
                text = expanded
        elif attachments and session.feedback_block:
            # Auto-switch (#130): text-only feedback gained attachments while
            # the draft was being adjusted. The aish-issue block flow cannot
            # upload assets, so withdraw the stashed draft and steer the model
            # onto the classic flow (draft + chips + gated `gh issue create`),
            # whose draft lists the assets for confirm/deselect before any
            # public upload. Appended after the user echo, so it is model-only.
            session.feedback_block = False
            session.pending_issue = None
            text += FEEDBACK_SWITCH_NOTE
        session.runner = asyncio.ensure_future(
            self._run_task(session, text, images, documents)
        )

    async def _run_task(
        self,
        session: Session,
        text: str,
        images: list[str] | None = None,
        documents: list[str] | None = None,
    ) -> None:
        try:
            if images or documents:
                result = await asyncio.to_thread(
                    session.agent.run_task, text, images, documents
                )
            else:
                result = await asyncio.to_thread(session.agent.run_task, text)
            # Backend-owned issue creation (#110): a text-only feedback draft
            # returns as one aish-issue block. Stash it (the pre-reviewed source
            # of truth for a later {type:create_issue}) and strip the raw fence
            # from the stored/replayed answer — the frontend renders the review
            # card from the live stream, so the fenced source never shows.
            issue, result = parse_issue_block(result)
            if issue is not None:
                session.pending_issue = issue
            # Web-only quick-reply safety net (issue #46): a question with no
            # chip gets fallback chips. The suffix also streams as a token so it
            # lands in the already-streamed answer block, not just done.result.
            result, suffix = apply_quick_reply_net(result)
            if suffix is not None:
                session.bridge.emit({"type": "token", "text": suffix})
            done: dict[str, Any] = {"type": "done", "result": result}
            # Riding on `done` (not a new event type) makes replay correctness
            # automatic and keeps the answer↔sources association explicit.
            sources = getattr(session.agent, "task_sources", [])
            if sources:
                done["sources"] = list(sources)
            session.bridge.emit(done)
        except ModelUnavailable as exc:
            session.bridge.emit(
                {
                    "type": "error",
                    "text": f"model unavailable: {exc}{_backend_hint(session.agent)}",
                }
            )
        except Exception as exc:  # noqa: BLE001 — a task bug must not kill the server
            session.bridge.emit({"type": "error", "text": f"task failed: {exc!r}"})
        finally:
            await self._finish_turn(session)

    async def _run_user_command(self, session: Session, command: str) -> None:
        """A ! command: run the typed text directly as the user's own action —
        no model, no approval gate — exactly like the CLI's ! escape. !cd is the
        /cd alias, so it moves cwd + re-anchors the root AND refreshes the UI
        cwd (like the / slash /cd path); any other command streams into a
        terminal block. Nothing here is model-driven, so the approval gate is
        untouched — the user typing a command is its own authorization."""
        try:
            if not command:
                return
            session.logref.command(command, "user-direct")
            cd_target = session.agent._parse_cd(command)
            if cd_target is not None:
                # rebase fires on_state → the unified cwd chip + card refresh
                # (issue #95); no manual _cwd_event send needed.
                await asyncio.to_thread(session.agent.rebase, cd_target)
            else:
                await asyncio.to_thread(session.agent.run_user_command, command)
            # The output already streamed into its terminal block; an empty
            # `done` just clears the busy state without a duplicate answer bubble.
            session.bridge.emit({"type": "done", "result": ""})
        except Exception as exc:  # noqa: BLE001 — a bad command must not kill the server
            session.bridge.emit({"type": "error", "text": f"command failed: {exc!r}"})
        finally:
            await self._finish_turn(session)

    # -- global interactive console (issue #148 follow-up) -----------------
    # ONE real pseudo-terminal for the whole server (the "Quake console") so
    # TTY-reading programs (gcloud auth, ssh, sudo) work interactively from any
    # chat. It is the USER's own terminal: ungated like the `!` path, and —
    # crucially — the model has NO write path to it. Bytes reach the PTY only
    # through _console_in, driven solely by the user's socket. Output is private
    # to the terminal (never recorded, never in model context) and fans out to
    # every client with the overlay open; only an explicit _console_share slices
    # a selection into the currently-viewed chat's context.

    def _resolve_console_command(self) -> tuple[str, bool]:
        """The console spawn command and whether it is tmux-backed. An injected
        command (tests) wins verbatim and gets no tmux semantics. Otherwise a
        tmux `new-session -A` (attach-or-create → survives aish-web restarts) when
        tmux is on PATH, else the login $SHELL directly (no restart survival)."""
        if self.console_command:
            return self.console_command, False
        if shutil.which("tmux"):
            return f"tmux new-session -A -s {shlex.quote(TMUX_CONSOLE_SESSION)}", True
        return (os.environ.get("SHELL") or "/bin/bash"), False

    def _console_cwd(self) -> str:
        """Starting dir for a fresh console — the default session's workspace so
        it opens somewhere sensible. (tmux ignores it when reattaching to an
        existing session; it only applies on first creation.)"""
        if self._default is not None:
            return self._default.agent.cwd
        return os.getcwd()

    def _console_out(self, text: str) -> None:
        """Fan console output to every viewer. Runs on the loop thread (PtySession
        marshals via call_soon_threadsafe), so touching the outboxes is safe.
        Pushed straight to outboxes, never through a bridge — console I/O is
        global and NEVER recorded into any session's transcript (issue #148)."""
        for client in self.console_viewers:
            client.outbox.put_nowait({"type": "console_out", "data": text})

    def _console_exit(self, code: int) -> None:
        """The console PTY ended (the shell exited, or the tmux client detached).
        Forget it — the next open respawns/reattaches — and tell every viewer."""
        self.console = None
        self.console_tmux = False
        for client in self.console_viewers:
            client.outbox.put_nowait({"type": "console_exit", "code": code})

    async def _console_open(self, client: Client) -> None:
        """Attach `client` to the global console, spawning it on first open (or
        after a restart/exit — a tmux spawn then REATTACHES to the surviving
        session and tmux redraws current state). A second viewer of an
        already-running console gets a `tmux refresh-client` poke so its fresh,
        blank terminal is repainted with the current screen."""
        loop = self.loop
        assert loop is not None, "no event loop yet"
        self.console_viewers.add(client)
        if self.console is None:
            command, tmux = self._resolve_console_command()
            self.console_tmux = tmux
            cwd = self._console_cwd()
            # Audit trail on the default session's log: the user's own action,
            # same decision tag as `!`. The I/O itself stays unrecorded.
            if self._default is not None:
                self._default.logref.command(f"[console] {command}", "user-direct")
            # Announce BEFORE spawning so the client resets its screen before any
            # console_out can arrive (both reach the loop in emit order).
            await client.ws.send_json(
                {"type": "console_started", "command": self._console_label(), "cwd": cwd}
            )
            self.console = PtySession(
                command, cwd, self._console_out, self._console_exit, loop
            )
        else:
            # Already running: reset just THIS newcomer's screen, then repaint it.
            await client.ws.send_json(
                {
                    "type": "console_started",
                    "command": self._console_label(),
                    "cwd": self._console_cwd(),
                }
            )
            self._console_refresh()

    def _console_label(self) -> str:
        """A short human label for the console header (the raw tmux command is
        noise). Reflects the actual backing so 'tmux' signals restart-survival."""
        if self.console_command:
            return self.console_command
        if self.console_tmux:
            return f"tmux · {TMUX_CONSOLE_SESSION}"
        return os.path.basename(os.environ.get("SHELL") or "/bin/bash")

    def _console_refresh(self) -> None:
        """Force tmux to repaint the console for a newly-attached viewer whose
        xterm is blank. Only one tmux CLIENT exists (our PTY); `refresh-client`
        targeted at its tty redraws the current screen. Best-effort, off the loop
        (a short subprocess), and a no-op without tmux — new output repaints
        anyway, this just avoids a blank wait until then."""
        if not self.console_tmux or self.console is None:
            return
        tty = self.console.tty

        def _poke() -> None:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["tmux", "refresh-client", "-t", tty],
                    timeout=5,
                    capture_output=True,
                )

        threading.Thread(target=_poke, daemon=True).start()

    def _console_in(self, client: Client, data: object) -> None:
        # THE ONLY console-input path (issue #148): only the user's own socket
        # reaches it; no model/agent code does.
        if self.console is None or not isinstance(data, str):
            return
        self.console.write(data)

    def _console_resize(self, client: Client, cols: object, rows: object) -> None:
        # One PTY, possibly many viewers of different sizes: tmux has a single
        # client (our PTY) and sizes the pane to it, so this is last-resize-wins.
        # A viewer whose window differs sees a mis-sized pane until it (or another
        # viewer) resizes; acceptable for a shared console.
        if self.console is None:
            return
        try:
            self.console.resize(int(cols), int(rows))  # type: ignore[call-overload]
        except (TypeError, ValueError):
            pass

    async def _console_kill(self, client: Client) -> None:
        """Explicit user "kill" — actually destroy the console (unlike Close,
        which merely hides). For a tmux-backed console the SURVIVING session must
        also go, else a later open would silently reattach to the very thing the
        user asked to kill; run `tmux kill-session` off the loop first, then kill
        the PTY (which _console_exit clears + broadcasts)."""
        if self.console_tmux:
            await asyncio.to_thread(self._tmux_kill_session)
        if self.console is not None:
            self.console.kill()  # reader thread observes EOF → _console_exit

    def _tmux_kill_session(self) -> None:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["tmux", "kill-session", "-t", TMUX_CONSOLE_SESSION],
                timeout=5,
                capture_output=True,
            )

    async def _console_share(self, client: Client, text: str) -> None:
        session = client.viewing
        if session is None:
            return
        text = text.strip()
        if not text:
            return
        if session.busy:
            # Appending to the model's messages while the worker thread iterates
            # them would race; sharing is a between-tasks action anyway.
            await client.ws.send_json(
                {
                    "type": "console_error",
                    "text": "finish the current task first, then share to context",
                }
            )
            return
        # Reuse the user-message path: append a user turn the model sees on its
        # NEXT task (no answer forced now), logged so it survives --resume. Echo
        # a transcript marker so every viewer of THIS chat sees what was shared.
        session.agent.add_user_context(f"[Shared from my interactive terminal:]\n{text}")
        session.bridge.emit({"type": "console_shared", "text": text})

    async def _create_issue(self, client: Client) -> None:
        """File the stashed feedback draft on the pinned repo (#110). This is a
        USER-DIRECT action: the title/body were reviewed in the card and are used
        verbatim — never re-derived by the model at click time — the repo is
        hard-pinned, and creation runs through the same ungated `!`-command path
        as any user-typed command (`run_user_command`), so no approval gate is
        needed or bypassed and NO model call happens on confirm. The argv is
        built safely — every field is shlex.quote'd, so user/model text is never
        shell-interpolated raw."""
        session = client.viewing
        if session is None:
            return
        issue = session.pending_issue
        if issue is None:
            await client.ws.send_json(
                {"type": "error", "text": "no issue draft to file — start with /feedback"}
            )
            return
        if await self._reject_busy(client, session):
            return
        command = (
            f"gh issue create --repo {ISSUE_REPO} "
            f"--title {shlex.quote(issue['title'])} --body {shlex.quote(issue['body'])}"
        )
        session.pending_issue = None  # consumed; a re-tap can't double-file
        session.feedback_block = False  # filed — the adjust window is over (#130)
        session.busy = True
        session.bridge.emit({"type": "user", "text": "Create the issue"})
        session.runner = asyncio.ensure_future(self._file_issue(session, command))

    async def _file_issue(self, session: Session, command: str) -> None:
        """Run the pinned `gh issue create` as a user-direct command (ungated,
        streams into a terminal block like any ! command) and then surface the
        new issue as a CLICKABLE link — gh prints the URL to stdout, which would
        otherwise sit as plain, unclickable text in the terminal block (#110)."""
        try:
            session.logref.command(command, "user-direct")
            output = await asyncio.to_thread(session.agent.run_user_command, command)
            match = ISSUE_URL_RE.search(output)
            # A rendered-markdown confirmation carrying a clickable link to the
            # filed issue; empty (no answer bubble) if gh emitted no URL.
            if match:
                url = match.group(0)
                result = f"✅ Issue [#{url.rsplit('/', 1)[-1]}]({url}) filed."
            else:
                result = ""
            session.bridge.emit({"type": "done", "result": result})
        except Exception as exc:  # noqa: BLE001 — a filing error must not kill the server
            session.bridge.emit({"type": "error", "text": f"issue filing failed: {exc!r}"})
        finally:
            await self._finish_turn(session)

    async def _finish_turn(self, session: Session) -> None:
        """Shared end-of-turn drain for both the model task and ! command paths:
        clear busy, apply a /cd requested mid-turn, then start the next queued
        message or signal a background session's return to idle."""
        session.busy = False
        if session.pending_cwd:  # a /cd that arrived after the last step's poll
            target, session.pending_cwd = session.pending_cwd, None
            # rebase fires on_state, which retires the #92 queue card and
            # refreshes the top-bar cwd chip — the SAME unified path as the
            # mid-task apply, so post-task and mid-task moves render identically.
            session.agent.rebase(target)
        if session.pending_retry is not None:  # a Retry that had to cancel a stuck turn
            text, session.pending_retry = session.pending_retry, None
            await self._launch_retry(session, text)
            return
        if session.queue:
            text, attachments = session.queue.pop(0)
            self._launch(session, text, attachments)
            return
        # A background session returned to idle: nudge every client NOT viewing
        # it (a viewer already saw the `done`). The drawer badge is the durable
        # signal; this toast is a heads-up.
        notice = {
            "type": "session_state",
            "session": session.name,
            "title": self._title(session),
            "state": "idle",
        }
        for client in list(self.clients):
            if client.viewing is session:
                continue
            try:
                await client.ws.send_json(notice)
            except Exception:  # noqa: BLE001 — a dead socket is dropped on its own disconnect
                pass

    async def _send_sessions(self, client: Client, query: str) -> None:
        # The active session is listed too (marked "current" in the drawer) —
        # its log is flushed per record, so reading it live is safe; a brand
        # new chat has no messages yet and drops out naturally.
        state_dir = self.state_dir

        def load():
            entries = SessionLog.load_entries(state_dir)
            return SessionLog.rank(entries, query)

        infos = await asyncio.to_thread(load)
        open_states = {name: s.state() for name, s in self.sessions.items()}
        # The working directory is known for sessions currently open in memory;
        # the drawer shows it so parallel agents are legible at a glance.
        open_cwds = {name: s.agent.cwd for name, s in self.sessions.items()}
        current = client.viewing.name if client.viewing is not None else ""
        await client.ws.send_json(
            {
                "type": "session_list",
                "current": current,
                "sessions": [
                    {
                        "name": info.path.name,
                        "title": info.title,
                        "snippet": info.snippet,
                        "ts": info.mtime,
                        "state": open_states.get(info.path.name, ""),
                        "cwd": open_cwds.get(info.path.name, ""),
                    }
                    for info in infos
                ],
            }
        )

    async def _open_by_name(self, name: str) -> Session | None:
        """The open session called `name`, or loaded cold from disk; None when
        no such session exists (or the name fails the path-safety checks)."""
        existing = self.sessions.get(name)
        if existing is not None:
            return existing
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or not path.is_file():
            return None
        self._evict_idle()
        session, history = await asyncio.to_thread(self.open_session, path)
        # Recorded synchronously so the _show snapshot right below includes it.
        # A session logged with trace records reconstructs into the SAME
        # user/step/done event stream a live one replays — rebuilding the
        # collapsed "Worked for Xs" timeline. Older logs (no trace records)
        # fall back to the flat conversation history.
        events = await asyncio.to_thread(SessionLog.reconstruct_events, path)
        if events:
            for event in events:
                session.bridge.record(event)
        else:
            session.bridge.record({"type": "history", "messages": history})
        self.add_session(session, default=False)
        return session

    async def _resume(self, client: Client, name: str) -> None:
        if client.viewing is not None and name == client.viewing.name:
            await self._show(client, client.viewing)
            return
        session = await self._open_by_name(name)
        if session is None:
            await client.ws.send_json({"type": "error", "text": f"no such session: {name}"})
            return
        await self._show(client, session)

    async def _new_session(self, client: Client) -> None:
        self._evict_idle()
        session, _ = await asyncio.to_thread(self.open_session, None)
        # A new chat inherits the model this client is currently using (like
        # ChatGPT/Claude apps); the saved default applies only at server start.
        source = client.viewing.agent if client.viewing is not None else None
        if (
            source is not None
            and getattr(source, "provider", "ollama") != "claude-max"
            and getattr(session.agent, "provider", "ollama") != "claude-max"
        ):
            session.agent.chat = source.chat
            session.agent.model = source.model
            session.agent.provider = getattr(source, "provider", "ollama")
            session.logref.model(model_spec(session.agent))
        self.add_session(session, default=False)
        await self._show(client, session)

    async def _fork_session(self, client: Client, after: int | None = None) -> None:
        """Branch the current conversation into a NEW session seeded with the
        history so far, leaving the original untouched — the "explore a tangent
        without polluting the main thread" move (issue #47).

        The fork is a SNAPSHOT: the source's append-only log (flushed on every
        record) is copied to a fresh session file, then reopened along the
        resume path (`_open_by_name` → `reconstruct_events`), so it replays
        identically to any resumed session — hot or later cold. The source's
        Agent and log are only read, never mutated.

        `after` (1-based) forks "from here": the copy is truncated to include up
        to and including that answer, so a per-answer Fork button branches from
        an earlier point. `None` forks the whole conversation.

        Refused while the source is busy (a mid-task snapshot would capture a
        half-finished turn) and when there's nothing to fork yet."""
        source = client.viewing
        if source is None:
            return
        if source.busy:
            await client.ws.send_json(
                {
                    "type": "error",
                    "text": "can't fork while this session is working — wait for "
                    "the current task to finish, then fork",
                }
            )
            return
        src_path = source.logref.log.path
        has_history = any(
            m.get("role") in ("user", "assistant") for m in source.agent.messages[1:]
        )
        if not has_history or not src_path.is_file():
            await client.ws.send_json(
                {
                    "type": "error",
                    "text": "nothing to fork yet — send a message first, then fork "
                    "to branch the conversation into a new session",
                }
            )
            return

        def copy_log() -> Path | None:
            # message + model + trace + terminal-framing records all carry over,
            # so the fork reconstructs the same transcript (and --resume history)
            # as the original up to the fork point. `after` truncates to that
            # answer; None copies the whole log.
            src_text = src_path.read_text(encoding="utf-8")
            forked_text = (
                src_text if after is None
                else SessionLog.truncate_at_answer(src_text, after)
            )
            if forked_text is None:  # `after` out of range
                return None
            new_path = SessionLog.new(self.state_dir).path
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_text(forked_text, encoding="utf-8")
            return new_path

        new_path = await asyncio.to_thread(copy_log)
        if new_path is None:
            await client.ws.send_json(
                {"type": "error", "text": "can't fork from there — that answer is out of range"}
            )
            return
        session = await self._open_by_name(new_path.name)
        if session is None:  # pragma: no cover — we just wrote a valid session file
            await client.ws.send_json({"type": "error", "text": "fork failed"})
            return
        # Continue in the fork with the source's LIVE model/backend (it may have
        # switched model since the last logged record); mirrors _new_session.
        src_agent = source.agent
        if (
            getattr(src_agent, "provider", "ollama") != "claude-max"
            and getattr(session.agent, "provider", "ollama") != "claude-max"
        ):
            session.agent.chat = src_agent.chat
            session.agent.model = src_agent.model
            session.agent.provider = getattr(src_agent, "provider", "ollama")
            session.logref.model(model_spec(session.agent))
        session.bridge.record(
            {
                "type": "echo",
                "text": "✓ forked into a new session — the original chat is "
                "untouched; continue the tangent here",
            }
        )
        await self._show(client, session)

    async def _delete_session(self, client: Client, name: str) -> None:
        """Delete a session permanently: its conversation AND its command
        audit trail — explicit and confirmed client-side, never bulk. Replies
        with a refreshed session_list so the drawer re-renders."""
        session = self.sessions.get(name)
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or (session is None and not path.is_file()):
            await client.ws.send_json({"type": "error", "text": f"no such session: {name}"})
            return
        if session is not None and session.state() != "idle":
            # Never kill work as a side effect of a delete.
            await client.ws.send_json(
                {"type": "error", "text": "task still running in that session — "
                 "stop it (or let it finish) before deleting"}
            )
            return
        if session is not None:
            # Any client viewing the doomed session lands on a fresh empty one
            # (the ChatGPT/Claude-app mental model) — move each viewer first so
            # nobody is left pointing at a closed session. Snapshot the set: each
            # _new_session → _show → _leave mutates it.
            for viewer in list(session.viewers):
                await self._new_session(viewer)
            session.close()
            self.sessions.pop(name, None)
        # POSIX unlink only detaches the name: a terminal aish holding this
        # file open via --resume keeps appending to the unlinked inode until
        # it exits — harmless, the data just vanishes with the last handle.
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))
        await client.ws.send_json({"type": "session_deleted", "name": name})
        await self._send_sessions(client, "")

    async def _rename_session(self, client: Client, name: str, title: str) -> None:
        """Give a chat a custom title. Persisted as an append-only
        `kind:"title"` record (no rewrite of the log). If the session is open
        its in-memory title is updated too, so the drawer AND the header both
        reflect the new name at once."""
        title = title.strip()[:RENAME_MAX]
        if not title:
            await client.ws.send_json(
                {"type": "error", "text": "a chat title can't be empty"}
            )
            return
        session = self.sessions.get(name)
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or (session is None and not path.is_file()):
            await client.ws.send_json({"type": "error", "text": f"no such session: {name}"})
            return
        if session is not None:
            # Append through the session's own open handle so a single writer
            # touches the file; mirror the name into memory for the hot path.
            await asyncio.to_thread(session.logref.log.set_title, title)
            session.custom_title = title
        else:
            # A cold session: append with a transient log handle, then release
            # it so the file isn't held open by a background writer.
            def write_cold() -> None:
                log = SessionLog(path)
                try:
                    log.set_title(title)
                finally:
                    log.close()

            await asyncio.to_thread(write_cold)
        await client.ws.send_json(
            {"type": "session_renamed", "name": name, "title": title}
        )
        await self._send_sessions(client, "")

    async def _send_models(self, client: Client, query: str) -> None:
        session = client.viewing
        if session is None:
            return
        agent, state_dir = session.agent, self.state_dir

        def load():
            return rank_models(available_models(agent, state_dir), query)

        ranked = await asyncio.to_thread(load)
        await client.ws.send_json(
            {
                "type": "model_list",
                "current": model_spec(session.agent),
                "models": [{"name": name, "desc": desc} for name, desc in ranked],
            }
        )

    async def _set_model(self, client: Client, message: dict) -> None:
        session = client.viewing
        if session is None or await self._reject_busy(client, session):
            return
        spec = str(message.get("spec", "")).strip()
        if not spec:
            return
        crossing_max = spec.startswith("claude-max") or (
            getattr(session.agent, "provider", "ollama") == "claude-max"
        )
        if crossing_max:
            await client.ws.send_json(
                {
                    "type": "error",
                    "text": "claude-max runs a different agent loop — restart with "
                    f"`aish-web --model {spec}` to switch",
                }
            )
            return
        try:
            chat, provider, name = await asyncio.to_thread(backends.make_chat, spec)
        except backends.BackendError as exc:
            await client.ws.send_json({"type": "error", "text": str(exc)})
            return
        session.agent.chat = chat
        session.agent.model = name
        session.agent.provider = provider
        session.logref.model(model_spec(session.agent))
        saved = False
        if message.get("save"):
            if self.config_path is None:
                await client.ws.send_json(
                    {"type": "error", "text": "no config path available — cannot save"}
                )
            else:
                error = save_default_model(self.config_path, spec)
                if error:
                    await client.ws.send_json({"type": "error", "text": error})
                else:
                    saved = True
        session.bridge.emit({"type": "echo", "text": f"model switched to {spec}"})
        await client.ws.send_json(
            {"type": "model_changed", "model": model_spec(session.agent), "saved": saved}
        )

    async def _cd(self, client: Client, path: str) -> None:
        session = client.viewing
        if session is None or not path:
            return
        # Changing cwd mid-task would move the ground under the running agent —
        # queue it and apply the moment the task finishes, instead of failing.
        if session.busy:
            # Surface the pending change as a single deduplicated queue card
            # (#92): the backend keeps at most one pending_cwd, so overwriting
            # and re-emitting updates the existing card in place. record=False —
            # the card is reconstructed from pending_cwd on attach (see _show),
            # not from transcript noise.
            session.pending_cwd = path
            session.bridge.emit({"type": "cwd_queued", "path": path}, record=False)
            return
        result = await asyncio.to_thread(session.agent.rebase, path)
        if result.startswith("ERROR"):
            await client.ws.send_json({"type": "error", "text": result})
            return
        # rebase fired on_state → the top-bar chip + queue-card refresh; no
        # manual _cwd_event needed (issue #95 unified that path).

    async def _add_dir(self, client: Client, path: str) -> None:
        session = client.viewing
        if session is None or await self._reject_busy(client, session) or not path:
            return
        result = await asyncio.to_thread(session.agent.add_root, path)
        if result.startswith("ERROR"):
            await client.ws.send_json({"type": "error", "text": result})
            return
        session.bridge.emit({"type": "echo", "text": result})
        await client.ws.send_json(self._cwd_event(session))

    async def _send_files(self, client: Client, query: str) -> None:
        session = client.viewing
        if session is None:
            return
        cwd = session.agent.cwd
        paths = await asyncio.to_thread(list_files, cwd, query, self.dir_ignore)
        await client.ws.send_json({"type": "file_list", "query": query, "files": paths})

    # Directory picker backend (top-bar cwd control). Deliberately NOT scoped
    # to session roots: /cd already accepts any path the server user can
    # reach, so listing adds no capability — but it stays names-only and
    # token-gated.
    _DIRS_TIMEOUT_S = 5.0  # kill a stuck listing after this and return 504

    # The listing runs in a SEPARATE process (see handle_dirs). Everything that
    # touches the filesystem — resolve(), is_dir(), scandir() — lives here so a
    # blocking call can never touch the server's own interpreter. Stdlib only.
    _DIRS_LIST_SCRIPT = r"""
import fnmatch, json, os, sys
from pathlib import Path
CAP = 1000
# gitignore-style ignore patterns are passed in as a JSON array (argv[2]) from
# self.dir_ignore — the user-editable [directory_picker] list (#87). The matcher
# is inlined (duplicating dir_ignore.matches) because this runs in an isolated
# `python -I` child that can't import the aish package. A pure name filter over
# the already-scanned entries — NO extra scandir/stat per entry (#86).
try:
    PATTERNS = json.loads(sys.argv[2]) if len(sys.argv) > 2 else []
except (ValueError, IndexError):
    PATTERNS = []
def ignored(name, is_dir):
    for pat in PATTERNS:
        if pat.endswith("/"):
            if not is_dir:
                continue
            pat = pat[:-1]
        if pat and fnmatch.fnmatchcase(name, pat):
            return True
    return False
raw = (sys.argv[1] if len(sys.argv) > 1 else "").strip() or str(Path.home())
try:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        print(json.dumps({"status": 400, "error": "path must be absolute"})); sys.exit(0)
    p = p.resolve()
    if not p.is_dir():
        print(json.dumps({"status": 404, "error": "not a directory"})); sys.exit(0)
    dirs, files = [], []
    with os.scandir(p) as entries:
        for e in sorted(entries, key=lambda x: x.name.lower()):
            try:
                is_dir = e.is_dir(follow_symlinks=True)
            except OSError:
                continue
            if ignored(e.name, is_dir):
                continue
            if is_dir:
                dirs.append(e.name)
            else:
                files.append(e.name)
    print(json.dumps({
        "status": 200, "path": str(p),
        "dirs": [{"name": n, "items": None} for n in dirs[:CAP]],
        "files": files[:CAP],
        "truncated": len(dirs) > CAP or len(files) > CAP,
    }))
except PermissionError:
    print(json.dumps({"status": 403, "error": "permission denied"}))
except Exception as ex:  # noqa: BLE001 - report any listing failure as 500
    print(json.dumps({"status": 500, "error": str(ex)}))
"""

    async def handle_dirs(self, request) -> JSONResponse:
        """GET /dirs?path=<abs> — folders and files (names only) of the browsed
        directory, both capped.

        All filesystem work runs in a SEPARATE, killable subprocess. A blocking
        stat/scandir — a TCC-gated path (Desktop/Documents/iCloud) can *hang* a
        headless launchd process rather than deny, and a blocking readdir holds
        the GIL — would otherwise freeze the whole server, not just the request.
        Isolating it means a stuck listing is killed and returns 504 while the
        server stays fully responsive (#86)."""
        if self.token and request.query_params.get("token") != self.token:
            return JSONResponse({"error": "bad token"}, status_code=403)
        raw = request.query_params.get("path", "").strip()
        data, status = await self._run_fs_child(
            self._DIRS_LIST_SCRIPT, raw, json.dumps(self.dir_ignore)
        )
        return JSONResponse(data, status_code=status)

    async def _run_fs_child(self, script: str, *args: str) -> tuple[dict, int]:
        """Run a stdlib-only filesystem script in a separate, killable process,
        so a blocking scandir/stat there can never touch this interpreter's GIL
        or event loop. On timeout the child is killed and the server stays
        responsive. The script must print one JSON object with a ``status`` key.
        Returns ``(payload, http_status)`` (#86)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-c",
                script,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            return {"error": f"cannot list: {exc}"}, 500
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self._DIRS_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            # Reap in the background: a child hard-stuck in uninterruptible I/O
            # may not die until its syscall returns, but it's a separate process
            # and no longer affects the server's responsiveness.
            asyncio.create_task(proc.wait())
            return {"error": "timed out"}, 504
        try:
            data = json.loads(out or b"{}")
        except (ValueError, TypeError):
            return {"error": "listing failed"}, 500
        if not isinstance(data, dict):
            return {"error": "listing failed"}, 500
        return data, data.pop("status", 200)

    async def handle_upload(self, request) -> JSONResponse:
        """POST /upload?name=<filename>, raw body — no multipart, so no extra
        dependency. Files land in <state_dir>/uploads (a session root, so the
        agent's read_file auto-approves them)."""
        if self.token and request.query_params.get("token") != self.token:
            return JSONResponse({"error": "bad token"}, status_code=403)
        name = os.path.basename(request.query_params.get("name", "").strip())
        if not name or name.startswith(".") or name in ("..",):
            return JSONResponse({"error": "invalid file name"}, status_code=400)
        body = await request.body()
        if not body:
            return JSONResponse({"error": "empty upload"}, status_code=400)
        if len(body) > UPLOAD_MAX_BYTES:
            return JSONResponse(
                {"error": f"file too large (max {UPLOAD_MAX_BYTES // (1024 * 1024)} MB)"},
                status_code=413,
            )
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        target = self.uploads_dir / name
        stem, suffix = target.stem, target.suffix
        counter = 1
        while target.exists():
            target = self.uploads_dir / f"{stem}-{counter}{suffix}"
            counter += 1
        target.write_bytes(body)
        return JSONResponse({"path": str(target)})

    async def handle_file(self, request) -> FileResponse | JSONResponse:
        """GET /file?path=<abs> — serves an image file so the transcript can
        render model-generated charts/diagrams inline (issue #9). Scoped like
        approval: symlinks resolved BEFORE the containment check, and anything
        outside the roots of ANY open session is refused. (This HTTP endpoint
        has no socket context, so with many concurrent viewers it must accept a
        path in scope for the session that produced it — the union of open
        sessions' roots, which for a single session is exactly that session.)"""
        if self.token and request.query_params.get("token") != self.token:
            return JSONResponse({"error": "bad token"}, status_code=403)
        raw = request.query_params.get("path", "").strip()
        if not raw:
            return JSONResponse({"error": "missing path"}, status_code=400)
        path = Path(raw).expanduser()
        if not path.is_absolute():
            return JSONResponse({"error": "path must be absolute"}, status_code=400)
        media_type = IMAGE_TYPES.get(path.suffix.lower())
        if media_type is None:
            return JSONResponse({"error": "unsupported file type"}, status_code=415)
        path = path.resolve()
        roots = {
            Path(r).resolve()
            for session in self.sessions.values()
            for r in session.agent.roots
        }
        if not any(path.is_relative_to(r) for r in roots):
            return JSONResponse({"error": "outside session roots"}, status_code=403)
        if not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(
            path, media_type=media_type,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @staticmethod
    def _pdf_response(data: bytes, filename: str) -> Response:
        return Response(
            content=data,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    def _image_roots(self) -> list[Path]:
        """The trusted directories the exporter may inline local images from:
        every open session's roots (which already include the uploads dir) plus
        their scratch workspaces — the same union-of-roots boundary /file
        serves under. Local `![](path)` images outside this set render as link
        cards, never read (issue #133)."""
        roots = [self.uploads_dir.resolve()]
        for session in self.sessions.values():
            roots.extend(Path(r).resolve() for r in session.agent.roots)
            roots.append(Path(session.agent.scratch_dir).resolve())
        return roots

    async def handle_export_answer(self, request) -> Response | JSONResponse:
        """POST /export/answer?title=<title>, raw markdown body — renders one
        answer to a PDF the browser downloads. Conversion is local (see
        export.py); embedded media (remote images, map snapshots, video
        thumbnails) may be fetched at export time, each bounded by a timeout
        with link-card fallback."""
        if self.token and request.query_params.get("token") != self.token:
            return JSONResponse({"error": "bad token"}, status_code=403)
        raw = await request.body()
        if not raw:
            return JSONResponse({"error": "empty answer"}, status_code=400)
        if len(raw) > EXPORT_MAX_BYTES:
            return JSONResponse({"error": "answer too large to export"}, status_code=413)
        markdown_text = raw.decode("utf-8", errors="replace")
        title = request.query_params.get("title", "").strip() or "aish answer"
        image_roots = self._image_roots()

        def build() -> bytes:
            return export.render_answer_pdf(markdown_text, title, image_roots)

        try:
            data = await asyncio.to_thread(build)
        except Exception as exc:  # noqa: BLE001 — a render failure is a 500, not a crash
            return JSONResponse({"error": f"export failed: {exc}"}, status_code=500)
        return self._pdf_response(data, export.safe_pdf_filename(title, "aish-answer"))

    async def handle_export_session(self, request) -> Response | JSONResponse:
        """GET /export/session?session=<name> — renders a session's FINAL
        answers (thinking/tool steps excluded) to a downloadable PDF, sourced
        from the persisted JSONL log. Embedded media follows the same rules as
        the answer export (see handle_export_answer)."""
        if self.token and request.query_params.get("token") != self.token:
            return JSONResponse({"error": "bad token"}, status_code=403)
        name = request.query_params.get("session", "").strip()
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or not path.is_file():
            return JSONResponse({"error": f"no such session: {name}"}, status_code=404)
        image_roots = self._image_roots()

        def build() -> tuple[bytes, str]:
            messages, _, custom_title = SessionLog._parse(path)
            title = custom_title or SessionLog._derive_title(messages) or "aish session"
            return export.render_session_pdf(messages, title, image_roots), title

        try:
            data, title = await asyncio.to_thread(build)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"export failed: {exc}"}, status_code=500)
        return self._pdf_response(data, export.safe_pdf_filename(title, "aish-session"))


def create_app(
    model: str,
    *,
    client_chat=None,
    state_dir: Path | None = None,
    allow_path: Path | None = None,
    deny_path: Path | None = None,
    config_path: Path | None = None,
    lessons_path: Path | None = None,
    num_ctx: int = 32768,
    max_steps: int = 25,
    think: bool = False,
    ask_all: bool = False,
    token: str | None = None,
    cwd: str | None = None,
    aliases: dict[str, str] | None = None,
    console_command: str | None = None,
) -> Starlette:
    """The Starlette app; client_chat injects a scripted backend (tests).

    `console_command` injects the global console's spawn command (tests pass a
    trivial echo loop so the console needs neither tmux nor a real shell)."""
    if cwd is None:
        cwd = default_workspace(os.getcwd())
        if cwd != os.getcwd():
            print(f"started from the home directory — working in {cwd} instead "
                  "to keep personal files out of scope")
    state_dir = Path(
        state_dir
        or os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish"))
    )
    allow_path = Path(allow_path or os.environ.get("AISH_ALLOWLIST", str(DEFAULT_ALLOWLIST)))
    deny_path = Path(deny_path or os.environ.get("AISH_DENYLIST", str(DEFAULT_DENYLIST)))
    lessons_path = Path(lessons_path or os.environ.get("AISH_LESSONS", str(DEFAULT_LESSONS)))
    uploads_dir = state_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    if client_chat is not None:
        chat, provider, model_name = client_chat, "ollama", model
    elif model == "claude-max" or model.startswith("claude-max:"):
        chat, provider, model_name = None, "claude-max", model.partition(":")[2]
    else:
        chat, provider, model_name = backends.make_chat(model)

    context = "\n\n".join(
        part
        for part in [
            environment_context(cwd),
            web_usage_context(model_name, provider, allow_path, deny_path, state_dir),
            *load_context_files(cwd),
        ]
        if part
    )

    server_ref: list = []

    def get_loop():
        return server_ref[0].loop if server_ref else None

    def open_session(path: Path | None) -> tuple[Session, list[dict]]:
        """Build one Session: fresh agent wired to its own bridge/log. For an
        existing path the conversation is reloaded into the agent (the file
        keeps growing in place — same semantics as `aish --resume`)."""
        history: list[dict] = []
        recorded_spec = ""
        custom_title: str | None = None
        if path is not None:
            # Parse BEFORE anything is appended: the last model record in
            # the file is the model this session must resume with.
            history, recorded_spec, custom_title = SessionLog._parse(path)
        log = SessionLog(path) if path is not None else SessionLog.new(state_dir)
        logref = LogRef(log)
        bridge = Bridge(get_loop)

        agent_holder: list = []
        session_holder: list = []

        def get_scope():
            if agent_holder:
                return agent_holder[0].cwd, agent_holder[0].roots
            return cwd, [Path(cwd).resolve()]

        def check_pending_cwd() -> str | None:
            """Get-and-clear the /cd queued while this task runs (issue #95), so
            run_task applies it between steps. Lock-free, matching the rest of
            pending_cwd (#92): the event loop is the only setter, the agent
            worker thread the only clearer, and each attribute access is atomic
            under CPython — the worst a race could do is defer one move to
            _finish_turn, which applies the same rebase."""
            if not session_holder:
                return None
            session = session_holder[0]
            target, session.pending_cwd = session.pending_cwd, None
            return target

        def check_pending_messages() -> list[str]:
            """Drain the text the user typed while this task runs (issue #95) so
            run_task can inject it mid-task as steering. Only text-only items are
            taken; a queued message carrying attachments stays in the queue and
            runs as a normal follow-up task at _finish_turn (native attachment
            delivery needs a fresh task, not a mid-turn user line). Consume-once:
            an item is injected here OR relaunched by _finish_turn, never both."""
            if not session_holder:
                return []
            session = session_holder[0]
            drained: list[str] = []
            kept: list[tuple[str, list[str]]] = []
            for text, attachments in session.queue:
                # A queued ! command is the user's own shell action, not model
                # steering — keep it for _finish_turn/_launch (which routes ! →
                # _run_user_command) instead of injecting it as a mid-task user
                # line, where it would run as a plain model prompt (issue #105).
                if attachments or not text or text.startswith("!"):
                    kept.append((text, attachments))
                else:
                    drained.append(text)
            session.queue[:] = kept
            return drained

        def on_state(ev: dict) -> None:
            # Every workspace change surfaces as a timeline marker (issue #94).
            bridge.emit({"type": "workspace", **ev})
            # A cwd move — mid-task (#95), immediate /cd, or the post-task drain —
            # always flows through rebase → here, so this is the ONE place that
            # retires the #92 queue card and refreshes the top-bar cwd chip.
            # record=False: both are transient UI state (the card is rebuilt from
            # pending_cwd on attach, the chip from the hello cwd).
            if ev.get("change") == "cwd" and agent_holder:
                agent = agent_holder[0]
                bridge.emit({"type": "cwd_dequeued"}, record=False)
                bridge.emit(
                    {
                        "type": "cwd_changed",
                        "cwd": agent.cwd,
                        "roots": [str(root) for root in agent.roots],
                    },
                    record=False,
                )

        def trust_dir(path: str) -> str:
            if agent_holder:
                return agent_holder[0].trust_root(path)
            return "ERROR: agent not ready"

        approve, approve_write, approve_read, approve_tool, approve_import = make_web_approvers(
            bridge, logref, allow_path, deny_path, ask_all, get_scope, trust_dir
        )
        # Coalesce a command's per-line output into fewer, larger `stream`
        # events (issue #109) — huge output otherwise emits one WS event + one
        # frontend reflow per line. Live-only: on_command_end flushes the
        # remainder so nothing trails; logging/replay are untouched.
        stream_coalescer = StreamCoalescer(
            lambda text: bridge.emit({"type": "stream", "text": text})
        )

        def on_command_end(ev: dict) -> None:
            stream_coalescer.flush()  # drain buffered lines before the exit line
            bridge.emit({"type": "command_end", **ev})

        common = dict(
            model=model_name,
            approve=approve,
            approve_write=approve_write,
            approve_read=approve_read,
            approve_tool=approve_tool,
            approve_import=approve_import,
            echo=lambda text: bridge.emit({"type": "echo", "text": text}),
            stream=stream_coalescer.line,
            max_steps=max_steps,
            cwd=cwd,
            context=context,
            aliases=aliases,
            on_message=logref.message,
            on_token=lambda text: bridge.emit({"type": "token", "text": text}),
            # Structured activity-trace steps; recorded so a resumed/switched
            # session replays the whole trace like every other event.
            on_step=lambda step: bridge.emit({"type": "step", **step}),
            # ...and persisted to disk so the trace survives eviction/restart
            # and cold-loads back into the same timeline (reconstruct_events).
            step_log=logref.step,
            command_log=logref.command_event,
            # Workspace changes (issue #94): persisted so resume/cold-open
            # restores cwd + trusted dirs, and emitted live as a timeline marker
            # identical to the one reconstruct_events replays.
            state_log=logref.workspace,
            on_state=on_state,
            # Between-steps steering (issue #95): a /cd or a message typed while
            # a task runs is applied/injected mid-task instead of only after it,
            # so a long task stays responsive. Both are get/drain callbacks the
            # agent's step loop polls; the event loop fills them from _cd /
            # _start_task's queue.
            check_pending_cwd=check_pending_cwd,
            check_pending_messages=check_pending_messages,
            # Terminal-block framing: command_start (cwd + command) and
            # command_end (exit code / detached / interrupted). Emitted live and
            # persisted (command_log) so a cold replay rebuilds the bounded
            # block identically instead of falling back to a plain output box.
            on_command_start=lambda ev: bridge.emit({"type": "command_start", **ev}),
            on_command_end=on_command_end,
            job_log_dir=state_dir / "jobs",
            lessons_path=lessons_path,
            status=WebStatus(bridge),
            state_dir=state_dir,
            current_session=lambda: logref.log.path,
            semantic=SemanticIndex(state_dir),
        )
        agent: Agent | ClaudeMaxAgent
        if provider == "claude-max":
            # aliased so the annotation above binds the TYPE_CHECKING import,
            # not this function-local one (F823)
            from .claude_max import ClaudeMaxAgent as _ClaudeMaxAgent

            agent = _ClaudeMaxAgent(**common)
            agent.provider = "claude-max"  # media_support must not default to ollama
        else:
            agent = Agent(client_chat=chat, num_ctx=num_ctx, think=think, **common)
            agent.provider = provider
        agent.roots.append(uploads_dir.resolve())
        agent_holder.append(agent)

        if path is not None:
            agent.load_history(history)
            # Restore the workspace the session left off in (issue #94), set
            # directly (not via rebase/trust_root) so restoring logs no fresh
            # record — a missing cwd falls back to the default, missing trusted
            # dirs are skipped.
            restored_cwd, trusted = SessionLog.restore_state(path)
            agent.restore_workspace(restored_cwd, trusted)
            # Resume with the model this session last used (the drawer shows
            # it); fall back to the startup model when it can't be built.
            if (
                recorded_spec
                and recorded_spec != model_spec(agent)
                and isinstance(agent, Agent)  # claude-max keeps its own session state
                and not recorded_spec.startswith("claude-max")
            ):
                try:
                    chat2, provider2, name2 = backends.make_chat(recorded_spec)
                    agent.chat, agent.model, agent.provider = chat2, name2, provider2
                except backends.BackendError:
                    pass
        logref.model(model_spec(agent))  # record what this session actually runs
        session = Session(agent, logref, bridge)
        session.custom_title = custom_title  # a renamed chat keeps its name hot
        session_holder.append(session)  # #95: the mid-task get/drain callbacks read it
        return session, history

    # The folder-browser / @-file ignore list is read from the same config.toml
    # the CLI uses (#87); a missing/malformed config degrades to defaults.
    dir_ignore_patterns = dir_ignore.load_patterns(load_config(config_path) if config_path else {})
    server = WebServer(
        open_session, state_dir, config_path, token, dir_ignore_patterns, console_command
    )
    server_ref.append(server)
    first, _ = open_session(None)
    server.add_session(first)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await server.startup()
        yield
        await server.shutdown()

    app = Starlette(
        routes=[
            WebSocketRoute("/ws", server.handle_ws),
            Route("/upload", server.handle_upload, methods=["POST"]),
            Route("/file", server.handle_file, methods=["GET"]),
            Route("/export/answer", server.handle_export_answer, methods=["POST"]),
            Route("/export/session", server.handle_export_session, methods=["GET"]),
            Route("/dirs", server.handle_dirs, methods=["GET"]),
            Route("/fonts/{name}", serve_config_font, methods=["GET"]),
            Route("/", serve_index, methods=["GET"]),
            Route("/index.html", serve_index, methods=["GET"]),
            Mount("/", StaticFiles(directory=STATIC_DIR, html=True)),
        ],
        lifespan=lifespan,
    )
    app.state.server = server
    return app


def main() -> int:
    config_path = Path(
        os.environ.get("AISH_CONFIG", str(Path.home() / ".config" / "aish" / "config.toml"))
    )
    config = load_config(config_path)
    # Seed config.toml with the default folder-browser ignore list on first use,
    # so the user can see and edit it (#87). Best-effort; never blocks startup.
    dir_ignore.seed_config(config_path)

    parser = argparse.ArgumentParser(
        prog="aish-web",
        description="aish web UI: the same approval-gated agent, served to a browser.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; 0.0.0.0 exposes the UI to your LAN (default: 127.0.0.1)",
    )
    parser.add_argument("--port", type=int, default=8787, help="port (default: 8787)")
    parser.add_argument(
        "--model",
        default=os.environ.get("AISH_MODEL") or config.get("model") or "qwen3.6:35b-a3b",
        help="model spec, same forms as aish --model",
    )
    parser.add_argument(
        "--num-ctx", type=int, default=int(config.get("num_ctx", 32768)),
        help="context window tokens",
    )
    parser.add_argument(
        "--max-steps", type=int, default=int(config.get("max_steps", 25)),
        help="max model turns per task",
    )
    parser.add_argument("--think", action="store_true", help="enable model thinking (slow)")
    parser.add_argument(
        "--ask-all",
        action="store_true",
        help="prompt for every command, including read-only ones",
    )
    args = parser.parse_args()

    token = os.environ.get("AISH_WEB_TOKEN") or None
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        print(
            "warning: serving without a token — anyone who can reach "
            f"{args.host}:{args.port} can drive this agent (approvals included). "
            "Set AISH_WEB_TOKEN to require one.",
            file=sys.stderr,
        )
    try:
        app = create_app(
            args.model,
            config_path=config_path,
            num_ctx=args.num_ctx,
            max_steps=args.max_steps,
            think=args.think,
            ask_all=args.ask_all,
            token=token,
            aliases=config.get("aliases"),
        )
    except backends.BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    query = f"/?token={token}" if token else "/"
    print(f"aish-web · model {args.model} · http://{args.host}:{args.port}{query}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    # uvicorn has finished its graceful shutdown (connections closed, lifespan
    # ran, pending approvals denied). A worker thread still inside a model
    # call is not interruptible and would block interpreter exit — end the
    # process now; session logs flush on every write, so nothing is lost.
    print("aish-web stopped")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
