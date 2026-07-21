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
import json
import os
import queue
import re
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

# Replay buffer bounds: enough for a long task's worth of events; beyond it
# the oldest are dropped and the client shows a truncation marker.
TRANSCRIPT_MAX = 600
TRANSCRIPT_KEEP = 500

# Open sessions kept in memory at once; beyond this the longest-idle one is
# closed (its file persists — reopening it later just reloads the history).
MAX_OPEN_SESSIONS = 6

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
!cd <dir> is the /cd alias that moves the project directory. A message starting \
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
        self.last_shown = time.monotonic()
        self.custom_title: str | None = None  # a user-set name; overrides the derived title

    @property
    def name(self) -> str:
        return self.logref.log.path.name

    def state(self) -> str:
        if self.busy:
            return "waiting" if self.bridge.pending else "running"
        return "idle"

    def close(self) -> None:
        self.logref.log.close()
        self.agent.close()  # best-effort scratch-workspace cleanup (issue #70)


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
        # The queued-cwd card is backend-authoritative (single pending_cwd), so
        # it's reconstructed on attach rather than replayed from the transcript
        # (#92) — this survives reconnects and session switches. Sent after the
        # replay so it lands on top of the freshly-rebuilt queue list.
        if session.pending_cwd:
            await websocket.send_json({"type": "cwd_queued", "path": session.pending_cwd})
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
        elif kind == "fork":
            after = message.get("after")
            await self._fork_session(websocket, after if isinstance(after, int) else None)
        elif kind == "delete_session":
            await self._delete_session(websocket, str(message.get("name", "")))
        elif kind == "rename_session":
            await self._rename_session(
                websocket, str(message.get("name", "")), str(message.get("title", ""))
            )
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
        elif kind == "retry":
            await self._retry_task(websocket, str(message.get("text", "")).strip())
        elif kind == "dequeue":
            self._dequeue(str(message.get("text", "")))
        elif kind == "dequeue_cwd":
            self.active.pending_cwd = None
            self.active.bridge.emit({"type": "cwd_dequeued"}, record=False)
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
            # Nothing is running server-side, but the foreground may be wedged
            # showing "working" — e.g. a terminal event that never reached this
            # client. Stop must never dead-end (#48): reconcile the view to the
            # authoritative idle state instead of erroring. A plain `stopped`
            # sync clears busy/working WITHOUT the red "task failed" treatment a
            # real error carries.
            session.bridge.emit({"type": "status", "state": "idle"}, record=False)
            await websocket.send_json({"type": "stopped"})
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

    async def _retry_task(self, websocket: WebSocket, text: str) -> None:
        """Regenerate the last answer from scratch (#60): the previous attempt is
        discarded from the model's context, the on-disk log, AND the transcript
        so the rerun is not informed by it. While a turn is still running (or
        wedged on an approval), the rollback can't touch agent.messages under the
        worker thread — cancel first and defer the rerun to _finish_turn, exactly
        how Retry already recovers a stuck turn."""
        session = self.active
        if session.busy:
            session.pending_retry = text
            await self._stop_task(websocket)
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
                result = await asyncio.to_thread(session.agent.rebase, cd_target)
                ws = self.ws
                if (
                    ws is not None
                    and session is self.active
                    and not result.startswith("ERROR")
                ):
                    with contextlib.suppress(Exception):
                        await ws.send_json(self._cwd_event())
            else:
                await asyncio.to_thread(session.agent.run_user_command, command)
            # The output already streamed into its terminal block; an empty
            # `done` just clears the busy state without a duplicate answer bubble.
            session.bridge.emit({"type": "done", "result": ""})
        except Exception as exc:  # noqa: BLE001 — a bad command must not kill the server
            session.bridge.emit({"type": "error", "text": f"command failed: {exc!r}"})
        finally:
            await self._finish_turn(session)

    async def _finish_turn(self, session: Session) -> None:
        """Shared end-of-turn drain for both the model task and ! command paths:
        clear busy, apply a /cd requested mid-turn, then start the next queued
        message or signal a background session's return to idle."""
        session.busy = False
        if session.pending_cwd:  # a /cd requested mid-turn, applied now
            target, session.pending_cwd = session.pending_cwd, None
            result = session.agent.rebase(target)
            # Retire the queued-cwd card (#92): the change has landed. The
            # header cwd chip is refreshed separately via _cwd_event below.
            session.bridge.emit({"type": "cwd_dequeued"}, record=False)
            ws = self.ws
            if ws is not None and session is self.active and not result.startswith("ERROR"):
                with contextlib.suppress(Exception):
                    await ws.send_json(self._cwd_event())
        if session.pending_retry is not None:  # a Retry that had to cancel a stuck turn
            text, session.pending_retry = session.pending_retry, None
            await self._launch_retry(session, text)
            return
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

    async def _fork_session(self, websocket: WebSocket, after: int | None = None) -> None:
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
        source = self.active
        if source.busy:
            await websocket.send_json(
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
            await websocket.send_json(
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
            await websocket.send_json(
                {"type": "error", "text": "can't fork from there — that answer is out of range"}
            )
            return
        session = await self._open_by_name(new_path.name)
        if session is None:  # pragma: no cover — we just wrote a valid session file
            await websocket.send_json({"type": "error", "text": "fork failed"})
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

    async def _rename_session(self, websocket: WebSocket, name: str, title: str) -> None:
        """Give a chat a custom title. Persisted as an append-only
        `kind:"title"` record (no rewrite of the log). If the session is open
        its in-memory title is updated too, so the drawer AND the header both
        reflect the new name at once."""
        title = title.strip()[:RENAME_MAX]
        if not title:
            await websocket.send_json(
                {"type": "error", "text": "a chat title can't be empty"}
            )
            return
        session = self.sessions.get(name)
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or (session is None and not path.is_file()):
            await websocket.send_json({"type": "error", "text": f"no such session: {name}"})
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
        await websocket.send_json(
            {"type": "session_renamed", "name": name, "title": title}
        )
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
            # Surface the pending change as a single deduplicated queue card
            # (#92): the backend keeps at most one pending_cwd, so overwriting
            # and re-emitting updates the existing card in place. record=False —
            # the card is reconstructed from pending_cwd on attach (see _show),
            # not from transcript noise.
            self.active.pending_cwd = path
            self.active.bridge.emit({"type": "cwd_queued", "path": path}, record=False)
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
    _DIRS_TIMEOUT_S = 5.0  # kill a stuck listing after this and return 504

    # The listing runs in a SEPARATE process (see handle_dirs). Everything that
    # touches the filesystem — resolve(), is_dir(), scandir() — lives here so a
    # blocking call can never touch the server's own interpreter. Stdlib only.
    _DIRS_LIST_SCRIPT = r"""
import json, os, sys
from pathlib import Path
CAP = 1000
# Build/dependency/system noise never worth showing in a directory picker (#87).
IGNORE_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".Trash", ".Spotlight-V100", ".fseventsd",
}
IGNORE_FILES = {".DS_Store", "Thumbs.db", ".localized"}
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
            if is_dir:
                if e.name not in IGNORE_DIRS:
                    dirs.append(e.name)
            elif e.name not in IGNORE_FILES:
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
        data, status = await self._run_fs_child(self._DIRS_LIST_SCRIPT, raw)
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
            messages, _, custom_title = SessionLog._parse(path)
            title = custom_title or SessionLog._derive_title(messages) or "aish session"
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
        custom_title: str | None = None
        if path is not None:
            # Parse BEFORE anything is appended: the last model record in
            # the file is the model this session must resume with.
            history, recorded_spec, custom_title = SessionLog._parse(path)
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
            command_log=logref.command_event,
            # Terminal-block framing: command_start (cwd + command) and
            # command_end (exit code / detached / interrupted). Emitted live and
            # persisted (command_log) so a cold replay rebuilds the bounded
            # block identically instead of falling back to a plain output box.
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
        session = Session(agent, logref, bridge)
        session.custom_title = custom_title  # a renamed chat keeps its name hot
        return session, history

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
