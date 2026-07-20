"""Web UI server: the same agent core behind a WebSocket instead of a TTY.

The browser is a thin client. Every callback the CLI wires to print()/input()
is wired here to JSON events over one WebSocket: tokens/echo/status stream
out, and approvals block the agent's worker thread on a queue until the
browser answers — the approval gate is identical to the terminal's, only the
transport differs.

Process model: one process holds MANY open sessions (each its own Agent +
SessionLog + transcript + busy flag) but shows ONE to the single connected
client. Tasks keep running in background sessions; switching sessions just
replays the target's transcript, so a task started in one session finishes
while you work in another. A new connection replaces the old one and receives
the active session's buffered transcript, which is what makes phone
lock/unlock mid-task lossless.
"""

import argparse
import asyncio
import contextlib
import hashlib
import os
import queue
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import backends, export, tools
from .agent import Agent, ModelUnavailable, environment_context
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
    parse_learn,
    rank_models,
    save_default_model,
)
from .embeddings import SemanticIndex
from .prompt import ATFILE_IGNORED_DIRS, ATFILE_MAX_RESULTS, ATFILE_SCAN_CAP
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
            (p.name, s.st_mtime_ns, s.st_size)
            for p in STATIC_DIR.iterdir()
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

# Replay buffer bounds: enough for a long task's worth of events; beyond it
# the oldest are dropped and the client shows a truncation marker.
TRANSCRIPT_MAX = 600
TRANSCRIPT_KEEP = 500

# Open sessions kept in memory at once; beyond this the longest-idle one is
# closed (its file persists — reopening it later just reloads the history).
MAX_OPEN_SESSIONS = 6

UPLOAD_MAX_BYTES = 25 * 1024 * 1024
EXPORT_MAX_BYTES = 5 * 1024 * 1024  # a single answer's markdown; generous ceiling

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

CLOSE_REPLACED = 4000  # another device connected; this socket is superseded
CLOSE_BAD_TOKEN = 4403


class Bridge:
    """Bridges one session's agent worker thread to the event loop.

    Outbound events go through call_soon_threadsafe into an asyncio queue a
    sender coroutine drains — but only while this session is the one shown
    (`attached`); a background session's events land in its transcript alone
    and surface on the next switch. Approval requests additionally block the
    worker on a plain queue.Queue slot until the client's answer fills it.
    The transcript buffer is only ever touched on the loop thread (inside
    _put), so replay snapshots need no locking.
    """

    def __init__(self, get_loop):
        self._get_loop = get_loop
        self.attached = False
        self.outbox: asyncio.Queue = asyncio.Queue()
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
        if self.attached:
            self.outbox.put_nowait(event)

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
            return False  # stale/duplicate answer (e.g. from a replaced tab)
        try:
            slot.put_nowait(value)
        except queue.Full:
            return False
        return True

    def reset_outbox(self) -> None:
        self.outbox = asyncio.Queue()


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

    return ask_approval, approve_write, approve_read


def list_files(cwd: str, query: str) -> list[str]:
    """Project paths for @-mention completion — the same walk, junk-dir
    skiplist, cap, and scoring as the TUI's AtFileCompleter."""
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(cwd, onerror=lambda _e: None):
        dirnames[:] = sorted(d for d in dirnames if d not in ATFILE_IGNORED_DIRS)
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
comment field whose text arrives with WHICHEVER button the user presses: \
on a denial it explains what is wrong — treat it as direct instruction on \
what to do instead; on an approval it is guidance to apply now and to \
future actions. Read-only commands auto-approve within the \
session roots (allowlist: {allow_path}). "Allow this session" auto-approves \
that command's prefixes until the session closes — in memory only. "Always \
allow" saves those same prefixes to the persistent allowlist file. When a \
command or file read reaches outside the session roots, its card warns about \
the escape and offers "Trust directory": one tap adds that directory to the \
session roots, so allowlisted work there auto-approves afterwards — also in \
memory only.
- There are NO ! direct commands here. A message starting with /learn \
distills the conversation into saved skills/memory (an optional hint \
follows, e.g. "/learn the gh flow"; "/learn lessons" migrates the legacy \
lessons file); the composer also accepts /model /resume /delete /new /cd \
/add-dir /jobs /help. Header controls: a "‹ Sessions" back button (top left, \
with a badge when a background session needs attention) opens the sessions \
drawer; the centered session title opens a menu (new chat, switch model, \
change directory, line wrap, export the chat to PDF, delete this chat, \
workspace & jobs); the compose pencil (top right) starts a new chat. Every \
finished answer has a row of chips beneath it — copy, export that one answer \
to PDF, and (where available) read-aloud. Both PDF exports render markdown to \
a file entirely locally (no external service) and download it; the whole-chat \
export includes only your final answers, not thinking or intermediate steps. A \
context bar under the title shows the working directory \
(tap to open a folder picker) and the model (tap to switch). In the composer, \
the ＋ button opens attach file / reference a path (@) / slash command (/) / \
photo. Your tool activity (thinking, recalled knowledge, commands and their \
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
feeds "answer text" into the user's input box, so the reply arrives as an \
ordinary user message (possibly edited). Asking in prose alone does NOT \
create buttons; you must add the link lines too. Skip them only when the \
answer is genuinely open-ended (no small set of options fits). Example: after \
"Proceed with the deploy?" end with [Yes, deploy](aish-reply://yes, deploy \
now) and [No, hold off](aish-reply://no, hold off).
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
"Confirm delete") that deletes the chat you are currently in. \
When the user refers to earlier work ("the fix from yesterday", "what went \
wrong last time"), use the recall tool to find and read the \
relevant past conversation instead of asking them to repeat it.
- File tools: prefer read_file/write_file/edit_file over cat/sed/heredocs; \
the user approves a diff card before any write. Do NOT use sed -i or > \
redirects to edit files.
- Attachments: the web UI can upload files. Images (and PDFs, when your \
backend supports them) are delivered to you NATIVELY — a "[image attached: \
… — you can see it]" note means the image itself is in the message: look at \
it directly (describe it, read text in it, use what you see to search the \
web); do NOT write scripts to parse it. Files that arrive as plain \
"[attached file: <path>]" lines were NOT delivered natively: read text \
files with read_file, process binaries with shell tools — in that mode you \
cannot see image contents, and should say so if asked to describe one."""


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
        self.last_shown = time.monotonic()

    @property
    def name(self) -> str:
        return self.logref.log.path.name

    def state(self) -> str:
        if self.busy:
            return "waiting" if self.bridge.pending else "running"
        return "idle"

    def close(self) -> None:
        self.logref.log.close()


class WebServer:
    """Per-process state: open sessions, the one being shown, one client."""

    def __init__(self, open_session, state_dir, config_path, token):
        self.open_session = open_session  # (path | None) -> Session
        self.state_dir = state_dir
        self.uploads_dir = state_dir / "uploads"
        self.config_path = config_path
        self.token = token
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sessions: dict[str, Session] = {}
        self._active: Session | None = None
        self.ws: WebSocket | None = None
        self.sender: asyncio.Task | None = None

    @property
    def active(self) -> Session:
        """The session shown to the client. Set before the server accepts any
        traffic (a session is opened at startup), so None only during init."""
        assert self._active is not None, "no active session yet"
        return self._active

    @active.setter
    def active(self, session: Session) -> None:
        self._active = session

    async def startup(self) -> None:
        self.loop = asyncio.get_running_loop()

    async def shutdown(self) -> None:
        """Unblock everything so Ctrl-C exits promptly: workers parked on an
        approval slot would otherwise wait forever and keep the interpreter
        alive. Denials are recorded in the audit log like any other deny."""
        for session in self.sessions.values():
            for uid in list(session.bridge.pending):
                session.bridge.answer(uid, {"action": "deny"})
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()

    def add_session(self, session: Session, activate: bool = True) -> None:
        self.sessions[session.name] = session
        if activate:
            self.active = session

    def _evict_idle(self) -> None:
        """Close the longest-idle non-busy background session past the cap.
        Running sessions are never closed — the cap can be exceeded by work."""
        while len(self.sessions) >= MAX_OPEN_SESSIONS:
            idle = [
                s for s in self.sessions.values()
                if not s.busy and s is not self._active
            ]
            if not idle:
                return
            oldest = min(idle, key=lambda s: s.last_shown)
            oldest.close()
            del self.sessions[oldest.name]

    @staticmethod
    def _title(session: Session) -> str:
        """The conversation title, same derivation as the sessions drawer:
        the first user message ('' while the session is still empty)."""
        for message in session.agent.messages[1:]:
            if message.get("role") == "user":
                return " ".join((message.get("content") or "").split())[:80]
        return ""

    def _hello(self, pager: list[tuple[str, str]] | None = None) -> dict:
        session = self.active
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
            "busy": session.busy,
            "cwd": session.agent.cwd,
            "roots": [str(root) for root in session.agent.roots],
            "home": str(Path.home()),  # client abbreviates paths to ~
            "rev": STATIC_REV,
            "pager": pages,
        }

    def _cwd_event(self) -> dict:
        return {
            "type": "cwd_changed",
            "cwd": self.active.agent.cwd,
            "roots": [str(root) for root in self.active.agent.roots],
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
        await self._attach(websocket)
        try:
            while True:
                message = await websocket.receive_json()
                if isinstance(message, dict):
                    await self._handle(websocket, message)
        except WebSocketDisconnect:
            pass
        finally:
            if self.ws is websocket:
                if self.sender:
                    self.sender.cancel()
                    self.sender = None
                self.ws = None

    async def _attach(self, websocket: WebSocket) -> None:
        old = self.ws
        self.ws = websocket
        if old is not None:
            try:
                await old.close(code=CLOSE_REPLACED)
            except Exception:  # noqa: BLE001 — the old socket may already be dead
                pass
        # A reconnecting client names the session it was on (?session=...).
        # Without this, a server restart lands every client in the fresh
        # startup session — silently moving the user out of their chat.
        wanted = websocket.query_params.get("session", "")
        session = self.active
        if wanted and wanted != session.name:
            session = await self._open_by_name(wanted) or self.active
        await self._show(websocket, session)

    async def _show(self, websocket: WebSocket, session: Session) -> None:
        """Point the client at `session`: hello + full transcript replay,
        then live events from its bridge."""
        # Disk scan for the pager happens before the attach block below: it
        # must not sit between the queue swap and the transcript snapshot.
        pager = await asyncio.to_thread(SessionLog.pager_titles, self.state_dir)
        if self.sender:
            self.sender.cancel()
            self.sender = None
        for other in self.sessions.values():
            other.bridge.attached = False
        self.active = session
        session.last_shown = time.monotonic()
        bridge = session.bridge
        # Same synchronous block: no _put callback can land between the queue
        # swap and the snapshot, so replay + live stream never duplicate.
        bridge.attached = True
        bridge.reset_outbox()
        snapshot = list(bridge.transcript)
        await websocket.send_json(self._hello(pager))
        await websocket.send_json(
            {"type": "replay", "events": snapshot, "truncated": bridge.truncated}
        )
        self.sender = asyncio.ensure_future(self._send_loop(websocket, bridge))

    async def _send_loop(self, websocket: WebSocket, bridge: Bridge) -> None:
        try:
            while True:
                event = await bridge.outbox.get()
                await websocket.send_json(event)
        except Exception:  # noqa: BLE001 — a dead socket ends the loop; replay recovers
            pass

    async def _handle(self, websocket: WebSocket, message: dict) -> None:
        kind = message.get("type")
        if kind == "task":
            attachments = [
                str(p) for p in (message.get("attachments") or []) if isinstance(p, str)
            ]
            await self._start_task(
                websocket, str(message.get("text", "")).strip(), attachments
            )
        elif kind == "approval":
            uid = str(message.get("id", ""))
            for session in self.sessions.values():
                if session.bridge.answer(uid, message):
                    break
        elif kind == "sessions":
            await self._send_sessions(websocket, str(message.get("query", "")))
        elif kind == "resume":
            await self._resume(websocket, str(message.get("path", "")))
        elif kind == "new":
            await self._new_session(websocket)
        elif kind == "delete_session":
            await self._delete_session(websocket, str(message.get("name", "")))
        elif kind == "models":
            await self._send_models(websocket, str(message.get("query", "")))
        elif kind == "set_model":
            await self._set_model(websocket, message)
        elif kind == "cd":
            await self._cd(websocket, str(message.get("path", "")).strip())
        elif kind == "add_dir":
            await self._add_dir(websocket, str(message.get("path", "")).strip())
        elif kind == "jobs":
            await websocket.send_json({"type": "job_list", "text": tools.jobs_table()})
        elif kind == "files":
            await self._send_files(websocket, str(message.get("query", "")))
        elif kind == "stop":
            await self._stop_task(websocket)
        elif kind == "dequeue":
            self._dequeue(str(message.get("text", "")))
        elif kind == "client_debug":
            # Device-side diagnostics (viewport state on iOS, etc.) — printed
            # to the server log because the phone has no reachable console.
            print(f"CLIENT_DEBUG: {message.get('text', '')}", flush=True)
        else:
            await websocket.send_json(
                {"type": "error", "text": f"unknown message type {kind!r}"}
            )

    async def _reject_busy(self, websocket: WebSocket) -> bool:
        if self.active.busy:
            await websocket.send_json(
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

    async def _stop_task(self, websocket: WebSocket) -> None:
        session = self.active
        if not session.busy:
            await websocket.send_json({"type": "error", "text": "nothing is running"})
            return
        if not hasattr(session.agent, "cancel"):
            await websocket.send_json(
                {"type": "error", "text": "stop is not supported on this backend"}
            )
            return
        session.agent.cancel()
        # A worker parked on an approval card must be unblocked to notice.
        for uid in list(session.bridge.pending):
            session.bridge.answer(uid, {"action": "deny"})
        session.bridge.emit({"type": "echo", "text": "✕ stop requested"})

    def _dequeue(self, text: str) -> None:
        """Drop the first still-waiting message matching `text` (the client's
        queued-chip remove button). A running task is never affected."""
        queue = self.active.queue
        for i, (queued, _attachments) in enumerate(queue):
            if queued == text:
                del queue[i]
                return

    async def _start_task(
        self, websocket: WebSocket, text: str, attachments: list[str] | None = None
    ) -> None:
        if not text and not attachments:
            return
        session = self.active
        if session.busy:
            if len(session.queue) >= MAX_QUEUE:
                await websocket.send_json(
                    {"type": "error", "text": f"queue full ({MAX_QUEUE} waiting)"}
                )
                return
            session.queue.append((text, attachments or []))
            await websocket.send_json(
                {"type": "queued", "position": len(session.queue), "text": text}
            )
            return
        self._launch(session, text, attachments or [])

    def _launch(self, session: Session, text: str, attachments: list[str]) -> None:
        # Attachments classify at start time so a model switch while queued
        # is honored (vision support is per-backend).
        images, documents, notes = self._classify_attachments(session.agent, attachments)
        if notes:
            text = f"{text}\n\n" + "\n".join(notes) if text else "\n".join(notes)
        session.busy = True
        session.bridge.emit({"type": "user", "text": text})
        if text.startswith("/"):
            # /learn is the one slash command on web: the transcript shows
            # what the user typed, the model gets the distillation prompt.
            learn = parse_learn(text, getattr(session.agent, "lessons_path", None))
            if learn is not None:
                text = learn
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
            session.busy = False
            if session.pending_cwd:  # a /cd requested mid-task, applied now
                target, session.pending_cwd = session.pending_cwd, None
                result = session.agent.rebase(target)
                ws = self.ws
                if ws is not None and session is self.active and not result.startswith("ERROR"):
                    with contextlib.suppress(Exception):
                        await ws.send_json(self._cwd_event())
            if session.queue:
                text, attachments = session.queue.pop(0)
                self._launch(session, text, attachments)
            elif session is not self.active and self.ws is not None:
                try:  # heads-up toast; the drawer badge is the durable signal
                    await self.ws.send_json(
                        {
                            "type": "session_state",
                            "session": session.name,
                            "title": self._title(session),
                            "state": "idle",
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass

    async def _send_sessions(self, websocket: WebSocket, query: str) -> None:
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
        await websocket.send_json(
            {
                "type": "session_list",
                "current": self.active.name,
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
        self.add_session(session, activate=False)
        return session

    async def _resume(self, websocket: WebSocket, name: str) -> None:
        if name == self.active.name:
            await self._show(websocket, self.active)
            return
        session = await self._open_by_name(name)
        if session is None:
            await websocket.send_json({"type": "error", "text": f"no such session: {name}"})
            return
        await self._show(websocket, session)

    async def _new_session(self, websocket: WebSocket) -> None:
        self._evict_idle()
        session, _ = await asyncio.to_thread(self.open_session, None)
        # A new chat inherits the model you're currently using (like ChatGPT/
        # Claude apps); the saved default applies only at server start.
        source = self.active.agent
        if (
            getattr(source, "provider", "ollama") != "claude-max"
            and getattr(session.agent, "provider", "ollama") != "claude-max"
        ):
            session.agent.chat = source.chat
            session.agent.model = source.model
            session.agent.provider = getattr(source, "provider", "ollama")
            session.logref.model(model_spec(session.agent))
        self.add_session(session, activate=False)
        await self._show(websocket, session)

    async def _delete_session(self, websocket: WebSocket, name: str) -> None:
        """Delete a session permanently: its conversation AND its command
        audit trail — explicit and confirmed client-side, never bulk. Replies
        with a refreshed session_list so the drawer re-renders."""
        session = self.sessions.get(name)
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or (session is None and not path.is_file()):
            await websocket.send_json({"type": "error", "text": f"no such session: {name}"})
            return
        if session is not None and session.state() != "idle":
            # Never kill work as a side effect of a delete.
            await websocket.send_json(
                {"type": "error", "text": "task still running in that session — "
                 "stop it (or let it finish) before deleting"}
            )
            return
        if session is self.active:
            # "Delete this chat" lands on a fresh empty one (the ChatGPT/
            # Claude-app mental model) — switch the client first.
            await self._new_session(websocket)
        if session is not None:
            session.close()
            self.sessions.pop(name, None)
        # POSIX unlink only detaches the name: a terminal aish holding this
        # file open via --resume keeps appending to the unlinked inode until
        # it exits — harmless, the data just vanishes with the last handle.
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))
        await websocket.send_json({"type": "session_deleted", "name": name})
        await self._send_sessions(websocket, "")

    async def _send_models(self, websocket: WebSocket, query: str) -> None:
        agent, state_dir = self.active.agent, self.state_dir

        def load():
            return rank_models(available_models(agent, state_dir), query)

        ranked = await asyncio.to_thread(load)
        await websocket.send_json(
            {
                "type": "model_list",
                "current": model_spec(self.active.agent),
                "models": [{"name": name, "desc": desc} for name, desc in ranked],
            }
        )

    async def _set_model(self, websocket: WebSocket, message: dict) -> None:
        if await self._reject_busy(websocket):
            return
        session = self.active
        spec = str(message.get("spec", "")).strip()
        if not spec:
            return
        crossing_max = spec.startswith("claude-max") or (
            getattr(session.agent, "provider", "ollama") == "claude-max"
        )
        if crossing_max:
            await websocket.send_json(
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
            await websocket.send_json({"type": "error", "text": str(exc)})
            return
        session.agent.chat = chat
        session.agent.model = name
        session.agent.provider = provider
        session.logref.model(model_spec(session.agent))
        saved = False
        if message.get("save"):
            if self.config_path is None:
                await websocket.send_json(
                    {"type": "error", "text": "no config path available — cannot save"}
                )
            else:
                error = save_default_model(self.config_path, spec)
                if error:
                    await websocket.send_json({"type": "error", "text": error})
                else:
                    saved = True
        session.bridge.emit({"type": "echo", "text": f"model switched to {spec}"})
        await websocket.send_json(
            {"type": "model_changed", "model": model_spec(session.agent), "saved": saved}
        )

    async def _cd(self, websocket: WebSocket, path: str) -> None:
        if not path:
            return
        # Changing cwd mid-task would move the ground under the running agent —
        # queue it and apply the moment the task finishes, instead of failing.
        if self.active.busy:
            self.active.pending_cwd = path
            self.active.bridge.emit(
                {"type": "echo", "text": f"↪ will switch to {path} when this task finishes"}
            )
            return
        result = await asyncio.to_thread(self.active.agent.rebase, path)
        if result.startswith("ERROR"):
            await websocket.send_json({"type": "error", "text": result})
            return
        await websocket.send_json(self._cwd_event())

    async def _add_dir(self, websocket: WebSocket, path: str) -> None:
        if await self._reject_busy(websocket) or not path:
            return
        result = await asyncio.to_thread(self.active.agent.add_root, path)
        if result.startswith("ERROR"):
            await websocket.send_json({"type": "error", "text": result})
            return
        self.active.bridge.emit({"type": "echo", "text": result})
        await websocket.send_json(self._cwd_event())

    async def _send_files(self, websocket: WebSocket, query: str) -> None:
        cwd = self.active.agent.cwd
        paths = await asyncio.to_thread(list_files, cwd, query)
        await websocket.send_json({"type": "file_list", "query": query, "files": paths})

    # Directory picker backend (top-bar cwd control). Deliberately NOT scoped
    # to session roots: /cd already accepts any path the server user can
    # reach, so listing adds no capability — but it stays names-only and
    # token-gated.
    DIR_SEARCH_MAX = 50
    DIR_SEARCH_DEPTH = 5
    DIR_SEARCH_VISIT_CAP = 20_000
    DIR_SEARCH_SKIP = {".git", "node_modules", "venv", ".venv", "__pycache__", ".Trash"}

    async def handle_dirs(self, request) -> JSONResponse:
        """GET /dirs?path=<abs> — subdirectory names only (browse mode)."""
        if self.token and request.query_params.get("token") != self.token:
            return JSONResponse({"error": "bad token"}, status_code=403)
        raw = request.query_params.get("path", "").strip() or str(Path.home())
        path = Path(raw).expanduser()
        if not path.is_absolute():
            return JSONResponse({"error": "path must be absolute"}, status_code=400)
        path = path.resolve()
        if not path.is_dir():
            return JSONResponse({"error": "not a directory"}, status_code=404)

        def list_dirs() -> list[str]:
            with os.scandir(path) as entries:
                return sorted(
                    (e.name for e in entries if e.is_dir(follow_symlinks=True)),
                    key=str.lower,
                )

        try:
            names = await asyncio.to_thread(list_dirs)
        except PermissionError:
            return JSONResponse({"error": "permission denied"}, status_code=403)
        return JSONResponse({"path": str(path), "dirs": names})

    async def handle_dirs_search(self, request) -> JSONResponse:
        """GET /dirs/search?q=<term>&base=<abs> — bounded fuzzy walk under
        base: depth- and visit-capped, hidden/noise dirs skipped, results
        ranked match-tightness first, then shallowness."""
        if self.token and request.query_params.get("token") != self.token:
            return JSONResponse({"error": "bad token"}, status_code=403)
        query = request.query_params.get("q", "").strip().lower()
        if not query:
            return JSONResponse({"results": []})
        raw = request.query_params.get("base", "").strip() or str(Path.home())
        base = Path(raw).expanduser()
        if not base.is_absolute():
            return JSONResponse({"error": "base must be absolute"}, status_code=400)
        base = base.resolve()
        if not base.is_dir():
            return JSONResponse({"error": "not a directory"}, status_code=404)

        def subsequence(needle: str, haystack: str) -> bool:
            it = iter(haystack)
            return all(ch in it for ch in needle)

        def walk() -> list[str]:
            scored: list[tuple[int, int, int, str]] = []
            queue: list[tuple[Path, int]] = [(base, 0)]
            visited = 0
            while queue and visited < self.DIR_SEARCH_VISIT_CAP:
                current, depth = queue.pop(0)
                try:
                    with os.scandir(current) as entries:
                        children = [
                            e.name for e in entries if e.is_dir(follow_symlinks=False)
                        ]
                except OSError:
                    continue
                visited += 1
                for name in sorted(children, key=str.lower):
                    if name.startswith(".") or name in self.DIR_SEARCH_SKIP:
                        continue
                    lower = name.lower()
                    if lower.startswith(query):
                        tightness = 0
                    elif query in lower:
                        tightness = 1
                    elif subsequence(query, lower):
                        tightness = 2
                    else:
                        tightness = -1
                    child = current / name
                    if tightness >= 0:
                        scored.append((tightness, depth, len(name), str(child)))
                    if depth + 1 < self.DIR_SEARCH_DEPTH:
                        queue.append((child, depth + 1))
            scored.sort()
            return [path for _, _, _, path in scored[: self.DIR_SEARCH_MAX]]

        results = await asyncio.to_thread(walk)
        return JSONResponse({"results": results})

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
        outside the active session's roots is refused."""
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
        roots = [Path(r).resolve() for r in self.active.agent.roots]
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

    async def handle_export_answer(self, request) -> Response | JSONResponse:
        """POST /export/answer?title=<title>, raw markdown body — renders one
        answer to a PDF the browser downloads. Conversion is fully local (see
        export.py); the markdown is what the user is already looking at, so it
        never leaves the machine."""
        if self.token and request.query_params.get("token") != self.token:
            return JSONResponse({"error": "bad token"}, status_code=403)
        raw = await request.body()
        if not raw:
            return JSONResponse({"error": "empty answer"}, status_code=400)
        if len(raw) > EXPORT_MAX_BYTES:
            return JSONResponse({"error": "answer too large to export"}, status_code=413)
        markdown_text = raw.decode("utf-8", errors="replace")
        title = request.query_params.get("title", "").strip() or "aish answer"

        def build() -> bytes:
            return export.render_answer_pdf(markdown_text, title)

        try:
            data = await asyncio.to_thread(build)
        except Exception as exc:  # noqa: BLE001 — a render failure is a 500, not a crash
            return JSONResponse({"error": f"export failed: {exc}"}, status_code=500)
        return self._pdf_response(data, export.safe_pdf_filename(title, "aish-answer"))

    async def handle_export_session(self, request) -> Response | JSONResponse:
        """GET /export/session?session=<name> — renders a session's FINAL
        answers (thinking/tool steps excluded) to a downloadable PDF, sourced
        from the persisted JSONL log. Conversion is fully local."""
        if self.token and request.query_params.get("token") != self.token:
            return JSONResponse({"error": "bad token"}, status_code=403)
        name = request.query_params.get("session", "").strip()
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or not path.is_file():
            return JSONResponse({"error": f"no such session: {name}"}, status_code=404)

        def build() -> tuple[bytes, str]:
            messages = SessionLog.load_messages(path)
            title = SessionLog._derive_title(messages) or "aish session"
            return export.render_session_pdf(messages, title), title

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
) -> Starlette:
    """The Starlette app; client_chat injects a scripted backend (tests)."""
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
        if path is not None:
            # Parse BEFORE anything is appended: the last model record in
            # the file is the model this session must resume with.
            history, recorded_spec = SessionLog._parse(path)
        log = SessionLog(path) if path is not None else SessionLog.new(state_dir)
        logref = LogRef(log)
        bridge = Bridge(get_loop)

        agent_holder: list = []

        def get_scope():
            if agent_holder:
                return agent_holder[0].cwd, agent_holder[0].roots
            return cwd, [Path(cwd).resolve()]

        def trust_dir(path: str) -> str:
            if agent_holder:
                return agent_holder[0].trust_root(path)
            return "ERROR: agent not ready"

        approve, approve_write, approve_read = make_web_approvers(
            bridge, logref, allow_path, deny_path, ask_all, get_scope, trust_dir
        )
        common = dict(
            model=model_name,
            approve=approve,
            approve_write=approve_write,
            approve_read=approve_read,
            echo=lambda text: bridge.emit({"type": "echo", "text": text}),
            stream=lambda text: bridge.emit({"type": "stream", "text": text}),
            max_steps=max_steps,
            cwd=cwd,
            context=context,
            on_message=logref.message,
            on_token=lambda text: bridge.emit({"type": "token", "text": text}),
            # Structured activity-trace steps; recorded so a resumed/switched
            # session replays the whole trace like every other event.
            on_step=lambda step: bridge.emit({"type": "step", **step}),
            # ...and persisted to disk so the trace survives eviction/restart
            # and cold-loads back into the same timeline (reconstruct_events).
            step_log=logref.step,
            # Terminal-block framing: command_start (cwd + command) and
            # command_end (exit code / detached / interrupted). Recorded like
            # steps so replay reconstructs the bounded block identically.
            on_command_start=lambda ev: bridge.emit({"type": "command_start", **ev}),
            on_command_end=lambda ev: bridge.emit({"type": "command_end", **ev}),
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
        return Session(agent, logref, bridge), history

    server = WebServer(open_session, state_dir, config_path, token)
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
            Route("/dirs/search", server.handle_dirs_search, methods=["GET"]),
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
