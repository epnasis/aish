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
import functools
import hashlib
import hmac
import json
import os
import queue
import re
import secrets  # stdlib — NOT aish.secrets (the Keychain store)
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from email.utils import formataddr, getaddresses
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import backends, dir_ignore, export, notify, tools
from .agent import (
    CANCELLED_RESULT,
    FEEDBACK_SWITCH_NOTE,
    Agent,
    ModelUnavailable,
    environment_context,
)
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
from .session import (
    RATINGS,
    RESUME_MARKER,
    SessionLog,
    attachment_names,
    strip_attachment_notes,
    synthetic_kind,
    title_drifted,
)

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


# ---------------------------------------------------------------------------
# Security headers (#178 P0-2). A model answer containing ![](https://…)
# renders as a live <img> the browser fetches with zero clicks — a prompt
# injection's exfiltration channel needing no tool call at all. The CSP kills
# it at the browser: img-src is limited to same-origin (/file, uploads),
# data: URIs (style.css icons, base64 embeds) and the two remote image
# families aish legitimately embeds (YouTube thumbnails, Google static maps —
# the same whitelist export.py enforces server-side for PDFs). Everything
# else is pinned to 'self': there are no inline <script>s, no eval, and the
# only cross-origin frames are the strictly-matched YouTube/Maps embed cards
# (app.js embedForLink). style-src needs 'unsafe-inline' because index.html
# uses inline style= attributes on its inline-SVG icons (no injected-style
# risk beyond what script-src already prevents). connect-src names the ws://
# and wss:// forms of the request's own host explicitly — CSP3 'self' covers
# same-origin WebSockets in current browsers, but the explicit forms keep the
# socket working behind the wss reverse proxy and on older WebKit.
# Referrer-Policy stops session-bearing URLs (?session=…) leaking to the
# whitelisted image hosts. Applied to EVERY http response — index, static
# files, /file, JSON, errors, sw.js (whose worker-scope CSP this also is).
CSP_IMG_HOSTS = "https://img.youtube.com https://i.ytimg.com https://maps.googleapis.com"
# frame-src must name the REDIRECT TARGET, not just the URL we set on the
# iframe: `maps.google.com/maps?…&output=embed` answers 301 →
# `www.google.com/maps/embed?…`, and CSP re-checks the destination. Listing only
# maps.google.com blocked every map card at the browser with nothing in the UI
# to show for it — an empty box and a console line nobody reads. Path-scoped, so
# this grants the maps embed endpoint rather than all of www.google.com; the
# path is ignored when matching a redirect, which is exactly the case here.
CSP_FRAME_HOSTS = (
    "https://www.youtube-nocookie.com https://maps.google.com "
    "https://www.google.com/maps/"
)
_HOST_OK_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")  # header-injection guard


def content_security_policy(host: str = "") -> str:
    connect = "'self'"
    if host and _HOST_OK_RE.match(host):
        connect += f" ws://{host} wss://{host}"
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: {CSP_IMG_HOSTS}; "
        f"connect-src {connect}; "
        f"frame-src {CSP_FRAME_HOSTS}; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


def origin_allowed(origin: str | None, host: str) -> bool:
    """Same-origin gate for the WS handshake and POST /trigger (#178 P1-2).

    WebSockets are exempt from the same-origin policy and a text/plain POST is
    a CORS simple request (no preflight), so any page the owner visits can fire
    both cross-origin — the drive-by vector. Browsers ALWAYS send an Origin
    header on those, so: a present Origin whose host:port doesn't match the
    request's own Host is rejected; a MISSING Origin (curl, the launchd poller,
    native clients — not browsers) is allowed. Mirrors content_security_policy's
    host discipline (_HOST_OK_RE), including the reverse-proxy case where
    Origin `https://aish.wenda.eu` must match Host `aish.wenda.eu`."""
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    # Rejects garbage and the literal "null" Origin (sandboxed iframe, file://),
    # which is by definition not this origin.
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if not host or not _HOST_OK_RE.match(host):
        return False
    origin_host = parts.netloc.lower()
    own_host = host.lower()
    if origin_host == own_host:
        return True
    # Default-port equivalence: a browser omits :443/:80 in Origin while the
    # Host header (or the Origin, behind some proxies) may spell it out.
    default = ":443" if parts.scheme == "https" else ":80"
    return origin_host == own_host + default or origin_host + default == own_host


class SecurityHeaders:
    """Pure-ASGI middleware stamping the security headers on every HTTP
    response (WebSocket scopes pass through untouched — headers are an HTTP
    concept). Hand-rolled instead of BaseHTTPMiddleware so FileResponse /
    streaming bodies are not re-buffered."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = ""
        for key, value in scope.get("headers") or []:
            if key == b"host":
                host = value.decode("latin-1")
                break
        csp = content_security_policy(host)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", csp)
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Content-Type-Options", "nosniff")
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def serve_index(request):  # noqa: ARG001 — Starlette route signature
    """index.html with cache-busting ?v=<rev> on its assets and no-cache on
    itself: the page then always names the exact JS/CSS revision it runs, so
    a stale-from-HTTP-cache page can be detected (hello.rev mismatch) and a
    reload is guaranteed to fetch the current code."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace('src="app.js"', f'src="app.js?v={STATIC_REV}"')
    html = html.replace('href="style.css"', f'href="style.css?v={STATIC_REV}"')
    # The one remaining vendor script on the critical path (xterm is lazy-loaded
    # by app.js with the same rev). Stamping it makes a device cache it immutably
    # and a deploy bust it — the old unstamped tag was a stale-after-update gap.
    html = html.replace(
        'src="vendor/highlight.min.js"', f'src="vendor/highlight.min.js?v={STATIC_REV}"'
    )
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

# /trigger abuse guards (#178 P1-10). The webhook phase (#162) and any
# scheduler reuse this one ingress, so a flapping check or an HA retry storm
# must not spawn a session, a model run, and a push notification per delivery.
#
# Idempotency: meta.dedup_key → session name, a bounded in-memory LRU with a
# TTL. Deliberately NOT durable: cross-restart dedup stays at the SOURCE (the
# email poller's Gmail label marks a message processed on disk-of-record) —
# the server-side key is defense against retry storms within one process
# lifetime, nothing more.
TRIGGER_DEDUP_TTL = 24 * 3600   # seconds a dedup_key is remembered
TRIGGER_DEDUP_MAX = 512         # keys kept; least-recently-used dropped past this
# Token bucket per `origin` value: capacity is the allowed burst, one token
# earned per TRIGGER_RATE_REFILL_S sustained (~2/min). Injectable via
# create_app for tests.
TRIGGER_RATE_CAPACITY = 6.0
TRIGGER_RATE_REFILL_S = 30.0    # seconds to earn one new token
# Cap on concurrently RUNNING triggered sessions (origin != "user", busy).
# Refusing with 429 is safe by contract: the poller marks a message processed
# only AFTER a successful trigger, so a refused delivery retries on the next
# poll instead of being lost.
MAX_CONCURRENT_TRIGGERED = 3
TRIGGER_RETRY_AFTER_S = 30      # Retry-After hint on either 429

# Dedicated executor for AGENT WORKERS (#178 Gate 3) — the run_task /
# run_user_command calls that can PARK indefinitely on an approval (Bridge.ask
# blocks the worker thread on a queue.Queue until the browser answers). These
# must never share the default to_thread executor (~min(32, cpu+4) threads):
# a burst of triggers plus a few held approvals would exhaust it, at which
# point _show's own to_thread calls queue BEHIND the parked workers and no
# client can attach to answer the approvals that would free them — livelock,
# restart-only recovery. Sizing: a parked approval holds one thread, so the
# pool must exceed any plausible number of simultaneously-held sessions —
# 32 covers MAX_OPEN_SESSIONS (6, exceedable by busy/viewed sessions) plus
# MAX_CONCURRENT_TRIGGERED and restart-resumes several times over, and idle
# threads cost only memory.
WORKER_POOL_SIZE = 32

# Restart recovery (#164). A task killed mid-run (a deploy restart, a crash, an
# OOM) has NOTHING to bring it back: a user chat sits half-answered until
# somebody notices, and an automated one — an email trigger whose message the
# poller has already marked processed — silently never happens at all. At
# startup every session whose log ends on an unmatched task_start is resumed.
# The three bounds keep that safe: only recent work is picked up, a task that
# keeps killing the server is abandoned instead of crash-looping it, and a mass
# restart can't stampede the backend with a dozen concurrent tasks.
RESUME_WINDOW = 12 * 3600  # how far back an interrupted task is still resumed
RESUME_MAX_ATTEMPTS = 3    # interrupted starts before a task is left alone
RESUME_MAX_SESSIONS = 3    # tasks resumed per startup
# Sent as the resumed turn when the interrupted run got far enough to log its
# prompt: the request and the partial work are both already in the history, so
# repeating the prompt would invite the model to redo side effects it may have
# completed (an email already sent) rather than finish what is left.
# The marker is session.py's, not a literal: it is what classifies this turn as
# synthetic on cold replay, so the two must never drift apart (#171).
RESUME_NOTE = (
    f"{RESUME_MARKER} aish restarted while this task was still running, so the "
    "previous attempt was cut off part-way. Everything above is what had already "
    "happened. Do NOT repeat steps that already completed — especially anything "
    "that sent, wrote, or changed something. Check what is actually still "
    "missing, pick up from there, and finish the task."
)

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

# The share inbox (POST /share). iOS cannot register a PWA as a share target —
# Web Share Target is Chromium-only, and Safari implements only the outbound
# half — so "share to aish" from the iPhone share sheet is a Shortcut that
# POSTs here. What arrives is PARKED, never run: it becomes an attachment
# waiting in the composer next time the app is opened, and the owner types the
# prompt. That is the whole security argument for this endpoint — the share
# sheet stages work, it does not become a way to start an unattended session
# from any app on the phone.
SHARE_MAX_ITEMS = 25      # oldest dropped past this; an inbox, not an archive
SHARE_TTL_S = 14 * 24 * 3600  # something shared and never used stops nagging
# Shared TEXT goes into the composer, so it is bounded by what a composer can
# sanely hold. Anything longer is a document and should be shared as a file.
SHARE_TEXT_MAX = 20_000

# Titling an exported answer (#172). The prompt that produced an answer names
# the request ("test it with some difficult nested markdown"), not the document,
# and an answer's own opening line is a conversational lead-in ("You are
# correct.") — so the model that wrote the answer writes its title too.
TITLE_PROMPT = (
    "Write a short title for the document below. It will be the PDF's heading "
    "and filename.\n"
    "Rules: 3-6 words. Name what the document is ABOUT — never echo its opening "
    "words. Write it in the document's own language. No quotes, no markdown, no "
    "trailing period. Reply with the title alone and nothing else.\n\n"
    "---\n{body}\n---"
)
TITLE_SOURCE_CHARS = 4000  # the lead is enough to name a document, and keeps it one quick call
TITLE_TIMEOUT = 25.0  # a slow local model must not hold the export hostage

# Auto-titling a CHAT (#175). The chat title is otherwise the first user
# message, which leaves a fork wearing its parent's name forever (the fork
# copies the parent's log) and leaves a long conversation named after its
# opening line. The model that answers the chat names it too.
SESSION_TITLE_PROMPT = (
    "Name this conversation, the way a chat app labels it in a sidebar.\n"
    "Rules: 3-6 words. Name the SUBJECT the conversation is about — not what "
    "was asked for, not the assistant's reply. Write it in the conversation's "
    "own language. No quotes, no markdown, no trailing period. Reply with the "
    "name alone and nothing else.\n\n"
    "Current name: {current}\n\n"
    "---\n{body}\n---"
)
# Enough to name a conversation: how it opened and where it is now. A pass over
# the whole transcript would cost 50-100x the tokens for no better name.
SESSION_TITLE_CHARS = 1500  # per exchange
# Turns that get a title: 1, 3, 7, 15, 31 … — `(n + 1) & n == 0`. Logarithmic,
# so a long chat is retitled a handful of times and can never thrash. Titles are
# navigation: a name that moves every turn is worse than one slightly stale.
RETITLE_FIRST_TURN = 1

# Offline mirror (#165). The PWA keeps a local copy of every session it can fit,
# so an installed app opens and reads its history with no server reachable at
# all. What the user goes back for is the CONVERSATION — the question, the
# reasoning, the recommendation — while raw command output is what makes a
# transcript big. So bulk output is capped on the way out: the mirror stays a
# tenth of the size and a whole archive fits where a handful of sessions would.
# The cap is applied SERVER-side, not in the browser, so the saving is bandwidth
# too (the point of a mirror you sync over a phone connection).
OFFLINE_OUTPUT_CAP = 8192   # per stream chunk / tool result, chars
OFFLINE_OUTPUT_HEAD = 5500  # kept from the start (the command and how it began)
OFFLINE_OUTPUT_TAIL = 2000  # kept from the end (how it finished — usually the point)
OFFLINE_TRIM_NOTE = "\n… {n} chars trimmed for offline use — reconnect to see it all …\n"

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


def _trim_offline_text(text: str) -> str:
    """Head + tail of an oversized blob, with the gap named. Both ends matter:
    the start says what ran, the end says how it went."""
    if len(text) <= OFFLINE_OUTPUT_CAP:
        return text
    dropped = len(text) - OFFLINE_OUTPUT_HEAD - OFFLINE_OUTPUT_TAIL
    return (
        text[:OFFLINE_OUTPUT_HEAD]
        + OFFLINE_TRIM_NOTE.format(n=dropped)
        + text[-OFFLINE_OUTPUT_TAIL:]
    )


def offline_events(path: Path) -> list[dict]:
    """A session's replay event stream, sized for the offline mirror.

    Identical to what a live client replays (same `reconstruct_events`, so the
    cached transcript renders through the unchanged `onReplay` path) except
    that bulk command output is capped — see OFFLINE_OUTPUT_CAP. A log too old
    to reconstruct falls back to the flat history blob, exactly as
    `_open_by_name` does, so every session mirrors rather than only modern ones.
    """
    events = SessionLog.reconstruct_events(path)
    if events is None:
        messages = SessionLog._parse(path).messages
        return [{"type": "history", "messages": messages}]
    trimmed: list[dict] = []
    for event in events:
        kind = event.get("type")
        # `stream` is the terminal panel's body; a tool step's `output` is the
        # same bytes carried on the trace step. Cap both or the saving is halved.
        if kind == "stream" and len(event.get("text") or "") > OFFLINE_OUTPUT_CAP:
            event = {**event, "text": _trim_offline_text(event["text"])}
        elif kind == "step" and len(event.get("output") or "") > OFFLINE_OUTPUT_CAP:
            event = {**event, "output": _trim_offline_text(event["output"])}
        trimmed.append(event)
    return trimmed


def _prefix_sig(events: list[dict], count: int) -> str:
    """Fingerprint of the first `count` events — the delta protocol's proof
    that the client's cached prefix is still the server's prefix.

    Needed because reconstruction is NOT purely append-only: a command that was
    still running when the client last synced reconstructs later as
    `command_start → stream → command_end`, splicing events into the middle of
    the stream. Comparing the prefix catches that and forces a full refetch;
    the client never has to hash anything (it echoes back the sig it was given),
    so there is no canonical-JSON agreement to get wrong across languages.
    """
    blob = json.dumps(events[:count], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _turn_event(text: str) -> dict[str, Any]:
    """The `user` transcript event for text the HUMAN typed — never classified as
    synthetic, because what you type is yours (#171). See `_user_event` for what
    `ts` is for; it is the one thing both flavours share."""
    now = int(time.time())
    # `ts` is the turn's clock origin (see _user_event); `at` is when it
    # happened, which the transcript renders. Same number here, different
    # meanings — and `at` is the one cold replay also carries (#200), which is
    # why they are not one field.
    #
    # `turn` is what a removal names (#202), and it is minted HERE — before the
    # turn runs — rather than by the log record it will end up on. The message
    # someone most wants to take back is the one they just sent, so the control
    # has to exist on a LIVE turn, not only after the chat is replayed cold;
    # Session.open_turn hands this id to the log so both halves answer to the
    # same name.
    return {
        "type": "user", "text": text, "ts": now, "at": now, "turn": uuid.uuid4().hex[:12],
    }


def _user_event(text: str, synthetic: str = "") -> dict[str, Any]:
    """The `user` transcript event, tagged when aish — not the human — wrote
    the turn (#171). It is still a real turn (it starts a task, so the frontend
    runs the whole turn-management path); only the rendering differs. `synthetic`
    is passed explicitly only where the text carries no marker to classify by —
    a trigger's prompt is arbitrary — and everything else is classified by the
    same function `reconstruct_events` uses on replay, so the two agree.

    `ts` (epoch seconds) is when the turn began, and it is the ONLY authoritative
    record of that: the live trace card is built by the turn's first STEP and is
    rebuilt from the transcript by every replay, so without it a browser landing
    mid-turn — a reconnect, or a swipe away and back — can only guess the origin
    by summing the steps it replays, which counts the work and misses every gap
    between it (the in-flight step, an approval wait, the answer streaming) and
    therefore always guesses SHORT. Only live-emitted events carry it, which is
    the whole population that needs it: `reconstruct_events` closes every turn it
    replays (a cut-off one becomes an `error`), so a cold transcript never lands
    a reader inside a running turn."""
    event: dict[str, Any] = _turn_event(text)
    synthetic = synthetic or synthetic_kind(text)
    if synthetic:
        event["synthetic"] = synthetic
    return event


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

# Render-error reports (#188 layer 2). Until this existed, every way an image
# could fail to display failed IN THE BROWSER after the turn was over: the
# renderer wrote a small "unavailable" note into the DOM and told nobody, so the
# model's only feedback channel was the user typing "images don't show". The
# browser now reports what it could not render; the report is logged as a trace
# step (so the retrieval/curate ledger can count it like any other step) and,
# when the failure was in a LIVE turn, handed to the model as a note on its next
# one. Bounded and control-stripped because the strings come off a socket.
RENDER_ERROR_MAX = 6  # failures one report may name
RENDER_ERROR_SRC_CHARS = 200
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
RENDER_NOTE = (
    "[aish: {count} {noun} in your last answer did not display for the user: "
    "{items}. Never paste an image link straight into an answer — call "
    "show_image and use the markdown line it returns.]"
)
MAX_QUEUE = 5  # messages waiting behind a busy session
RENAME_MAX = 200  # custom chat-title length cap (a title, not a message)

CLOSE_REPLACED = 4000  # another device connected; this socket is superseded
CLOSE_BAD_TOKEN = 4403
CLOSE_BAD_ORIGIN = 4405  # cross-origin WS handshake refused (#178 P1-2)


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

    def __init__(self, get_loop, on_wait=None, session: str | None = None):
        self._get_loop = get_loop
        # The session name stamped onto every event this bridge fans out
        # (#182): most live events carried no session identity, so a client
        # that switched views client-side (prefetched swipe, offline-mirror
        # tap) kept rendering the OLD session's events into the new view until
        # the server processed the resume. The stamp lets the client drop
        # deliveries whose session is not the one on screen — generalizing the
        # `session` field hello/role already carry. None (tests, or a bridge
        # with no session yet) stamps nothing, which the client reads as
        # "not scoped, always deliver".
        self.session = session
        self.viewers: set = set()  # Clients currently viewing this session
        self.pending: dict[str, queue.Queue] = {}
        self.transcript: list[dict] = []
        self.truncated = False
        # Fired on the worker thread just before an approval blocks, with the
        # request event + whether anyone is viewing (#163): the hook decides
        # whether to push a notification (only for an unattended triggered
        # session). Wrapped in try/except so a notify failure never blocks the
        # gate. None = no notification wiring (CLI, tests).
        self.on_wait = on_wait
        # Fired on the LOOP thread when this session starts holding on an
        # approval (#203), so the server can tell the clients that are NOT
        # viewing it. The mirror image of the idle notice `_finish_turn`
        # already sends: without it a background chat could stop and wait
        # indefinitely while every other tab's attention count said nothing.
        # None = no wiring (CLI, tests).
        self.on_hold: Callable[[], Any] | None = None

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
        # Fan the event out to every attached viewer, stamped with this
        # session's name (#182) so the client can drop deliveries for a chat
        # no longer on screen. The stamp rides only the LIVE delivery — the
        # recorded transcript stays unstamped, keeping it byte-identical to
        # SessionLog.reconstruct_events (hot/cold parity), and replayed events
        # bypass the client's firewall anyway (the replay loop feeds handle()
        # directly). An event that already names a session (session_state)
        # keeps its own; an unnamed bridge (tests) stamps nothing. Runs on the
        # loop thread (call_soon_threadsafe / _show), so mutating a Client's
        # asyncio queue is safe and the viewer set can't change underfoot.
        if self.session is not None and "session" not in event:
            event = {**event, "session": self.session}
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
        # Announce the hold to non-viewers. Hops to the loop thread like emit()
        # does — this runs on the agent worker — and is registered in `pending`
        # above, so state() already reads "waiting" by the time it lands.
        if self.on_hold is not None:
            loop = self._get_loop()
            if loop is not None:
                loop.call_soon_threadsafe(self.on_hold)
        if self.on_wait is not None:
            # has_viewers snapshot: the set is mutated on the loop thread, but a
            # benign race (notify a moment after a viewer appears, or skip as
            # one leaves) is harmless. Never let a notify error reach the gate.
            try:
                self.on_wait(event, bool(self.viewers))
            except Exception:  # noqa: BLE001 — notification must not break approval
                pass
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
        self._note_text = ""
        self._note_last = 0.0

    def start(self, label: str) -> None:
        self._label = label
        self._tokens = 0
        self._last = time.monotonic()
        self._note_text = ""
        self._note_last = 0.0
        self.bridge.emit({"type": "status", "state": "working", "label": label}, record=False)

    def note(self, text: str) -> None:
        """A one-line gist of what the model is doing right now (streaming
        thinking text). Live-only like every status event; deduped + throttled
        because the snippet is recomputed on every streamed chunk."""
        now = time.monotonic()
        if text == self._note_text or now - self._note_last < self.THROTTLE_SECS:
            return
        self._note_text = text
        self._note_last = now
        self.bridge.emit(
            {"type": "status", "state": "working", "label": self._label, "note": text},
            record=False,
        )

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


# Triggered-session capability policy (#160): a session NOT started by the user
# (origin email/schedule/webhook) runs with no human at the keyboard. These
# mutating tools are SAFE to auto-run there — reversible and non-exfiltrating:
# relabeling mail, and mail (draft or live) whose every recipient is verifiably
# the owner. Everything else (any send or draft addressed beyond the owner,
# trashing, creating filters, sharing to Drive) falls through to the normal
# approval card, which simply HOLDS pending until the owner opens the automated
# session and answers — the draft-and-hold model, built out of the existing
# gate rather than a bespoke hold flow.
TRIGGERED_SAFE_TOOLS = frozenset({"gmail_label"})

# Recipient-scoped autonomy (#160 follow-up): a triggered session may send a
# real email WITHOUT approval as long as EVERY recipient is the owner — the whole
# prompt-injection risk is aish being steered into mailing a third party, and a
# send that can only ever land in the owner's own inbox removes it. A send to
# anyone else still holds. Override the address set with AISH_OWNER_ADDRESSES
# (comma-separated); defaults to the wenda owner addresses.
OWNER_ADDRESSES = frozenset(
    a.strip().lower()
    for a in os.environ.get(
        "AISH_OWNER_ADDRESSES", "pawel@wenda.eu,pawel@wenda.email"
    ).split(",")
    if a.strip()
)

def _parse_recipients(field: str) -> list[str] | None:
    """Every address a recipient header field routes to, or None when the field
    does not parse CLEANLY — the security rule (#178 P0-3): a decision about
    what a string IS must never be made by a regex that finds things IN it.
    The old `findall` approach saw the owner inside `"pawel@wenda.eu"@evil.com`
    (a valid RFC 5322 quoted local-part routed to evil.com) and concluded the
    send was owner-only. This parses with email.utils.getaddresses and then
    rejects anything exotic: a parse-failure pair, a quoted local-part, a
    local-part containing `@`, or a field that re-serializing the parsed
    addresses does not reproduce (residue = something escaped the parse, which
    lenient/older parsers are known to do). Rejection is safe — the caller
    falls through to the approval card, never to an auto-send."""
    pairs = getaddresses([field])
    if not pairs:
        return None
    addrs: list[str] = []
    for _display, addr in pairs:
        if not addr:
            return None  # ('', '') is getaddresses' malformed-input marker
        local, sep, domain = addr.rpartition("@")
        if not sep or not local or not domain:
            return None
        if "@" in local or '"' in addr or "'" in addr:
            return None  # quoted/multi-@ local-parts route elsewhere than they read
        addrs.append(addr)
    # Residue check: rebuilding the field from what we parsed must reproduce it
    # (whitespace/comma spacing normalized). Anything dropped or reinterpreted
    # by the parser fails the comparison and the send holds for approval.
    normalize = lambda s: re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", s.strip()))  # noqa: E731
    rebuilt = ", ".join(formataddr(pair) for pair in pairs)
    if normalize(field) != normalize(rebuilt):
        return None
    return addrs


def _all_recipients_owner(args: dict) -> bool:
    """True iff the send has at least one recipient and every address across
    to/cc/bcc is an owner address. A reply (reply_to_msg_id) is NEVER auto-safe,
    even with an explicit owner `to`: a threaded reply ALSO goes to the original
    message's sender, which is not verifiable from args (if the replied-to
    message is from a third party, an owner-looking `to` would still exfiltrate
    to them). Autonomous owner answers must therefore be a NEW email addressed
    explicitly to an owner address — the model is told this in the trigger.
    Any field that does not parse cleanly (see _parse_recipients) counts as
    not-owner, so a malformed/adversarial recipient holds instead of sending."""
    if args.get("reply_to_msg_id"):
        return False
    recips: list[str] = []
    for field in ("to", "cc", "bcc"):
        val = args.get(field)
        if val is None or val == "" or val == []:
            continue
        text = ", ".join(str(v) for v in val) if isinstance(val, list) else str(val)
        parsed = _parse_recipients(text)
        if parsed is None:
            return False
        recips += parsed
    if not recips:
        return False
    return all(addr.lower() in OWNER_ADDRESSES for addr in recips)


def _describe_hold(event: dict) -> str:
    """A short human phrase for a held approval_request, for the notification
    body (#163) — so the push says WHAT needs approving, not just 'something'."""
    kind = event.get("kind")
    if kind == "tool":
        tool = event.get("tool", "a tool")
        if event.get("preview"):
            return f"{tool}: {event['preview']}"
        args = event.get("args") or {}
        detail = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])
        return f"{tool}({detail})" if detail else tool
    if kind == "command":
        return f"run: {event.get('command', '')}"
    if kind == "write":
        return f"{event.get('verb', 'write')} {event.get('target', 'a file')}"
    if kind == "import":
        return f"import skill {event.get('skill', '')}"
    return "an action"


def _triggered_safe(name: str, args: dict) -> bool:
    """Whether tool `name` with `args` may auto-run in a triggered session."""
    if name in TRIGGERED_SAFE_TOOLS:
        return True
    if name == "gmail_send":
        # Safe only when it can never leave the owner's own control: every
        # recipient verifiably the owner (recipient-scoped autonomy — aish can
        # answer YOU without approval; mailing anyone else still holds). The
        # recipient check applies to DRAFTS too (#178 P0-3): a draft addressed
        # to a third party is a fully-staged exfiltration one mistaken tap from
        # sending, so it stays draftable but through the card, never silently.
        return _all_recipients_owner(args)
    return False


def make_web_approvers(bridge, logref, allow_path, deny_path, ask_all, get_scope, trust_dir,
                       get_origin=None, get_session_prefixes=None):
    """The three approval callbacks, backed by browser round trips. Mirrors
    cli.make_approver semantics exactly: denylist first (also on edited
    commands), then auto-approval scoped to the live session roots, then a
    blocking approval card. get_session_prefixes() -> the live set backing the
    card's "Allow this session" button, owned by the session's AGENT beside its
    roots (#176) so both halves of the gate's session scope have one lifetime;
    in-memory only, forgotten when the session closes. "Always allow" saves the
    card's shown prefixes to the persistent allowlist, same file as the CLI's
    'a' answer.
    trust_dir(path) -> note widens the live roots when the card's "Trust
    directory" button answers a command or read escaping them."""
    own_prefixes: set[str] = set()

    def session_prefixes() -> set[str]:
        return get_session_prefixes() if get_session_prefixes else own_prefixes

    def known_prefixes() -> frozenset:
        return frozenset(load_prefixes(allow_path)) | session_prefixes()

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
            session_prefixes().update(suggestions)
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
            "note": plan.note,
            "rule": plan.rule,
            "rule_verb": plan.rule_verb,
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

    def approve_tool(
        name: str, args: dict, preview: "str | None" = None
    ) -> "bool | Approved | Denied":
        # Reuses the command card verbatim (issue #141): same approve/deny +
        # comment verdicts, no denylist/auto-approval — a mutating tool always
        # prompts. Comment semantics match commands: deny+comment = STOP,
        # approve+comment = HOLD-and-adjust. A ground-truth preview (#157), when
        # the tool provides one, gives the human a legible description of an
        # otherwise-opaque (e.g. id-addressed) action.
        origin = get_origin() if get_origin else "user"
        if origin != "user" and _triggered_safe(name, args):
            # Triggered-session capability policy (#160): a safe mutation runs
            # without a card since there is no human to answer one. Recorded as
            # auto so the audit trail shows it was policy, not a human decision.
            shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
            record(f"tool {name}({shown})", f"auto ({origin})")
            bridge.emit({"type": "echo", "text": f"✓ auto-approved ({origin}): {name}"})
            return True
        request: dict[str, Any] = {
            "type": "approval_request",
            "kind": "tool",
            "tool": name,
            "args": args,
        }
        if preview:
            request["preview"] = preview
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
drawer. The chat's name is written for it automatically — after the first \
answer, when a fork first differs from its parent, and occasionally if the \
subject really changes; if the user renames it by hand that name is final and \
is never rewritten. The centered session title opens a menu (new chat, rename this chat, \
switch model, change directory, line wrap, export the chat to PDF, keep this \
chat, delete \
this chat, workspace & jobs); the compose pencil (top right) starts a new \
chat. Every \
finished answer has a row of chips beneath it — copy, export that one answer \
to PDF, and (where available) read-aloud. Each of the user's OWN prompts has a \
row too — the time it was sent, a trash chip, a pencil that puts the prompt \
back in the composer, and copy. The trash chip DELETES that whole exchange \
(their prompt, your work on it and your answer): it asks first, in a dialog \
naming what is lost, and on confirm the text is deleted from the session log, \
dropped from your context so you can no longer quote it, and gone from every \
device's offline copy at the next sync; a "Message deleted" marker stays where \
it was. Tell them to use it for a message sent to the wrong chat, one \
sent half-typed, or a secret pasted by mistake — it is the ONLY way to take \
something back, and it cannot be undone. \
Both PDF exports render markdown \
locally and download the file; the whole-chat export includes only your final \
answers, not thinking or intermediate steps. A single-answer PDF is titled and \
named after the ANSWER (you write that title yourself when asked), not after \
the prompt that produced it; a whole-chat PDF uses the chat's title. \
Exported PDFs embed pictures: \
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
- The web UI WORKS OFFLINE for READING. Past conversations are mirrored to the \
device automatically (newest first, including chats started on the user's other \
devices), so with no connection the app still opens, past chats still open, and \
SEARCH still works over their contents. The download icon in the header (⤓, \
beside the terminal and new-chat icons) is a ONE-TAP toggle that pins the chat \
being read so it is never dropped from that local copy however old it gets — \
filled means kept. There is nothing else to manage and no cache to clear by \
hand: the mirror caps its own size and drops the least useful copies first. \
SENDING is paused while \
offline — by design, not by accident: a prompt queued for later would run \
commands with nobody there to approve them. If the user asks how to keep a \
conversation for reference while travelling, tell them to pin it that way.
- If a user message starts with "[automatic resume]", aish was RESTARTED while \
your previous task was still running and that same task has been picked up \
again — the conversation above is your OWN interrupted work. You MUST check \
what actually completed before acting (re-read the real state rather than \
assuming from the transcript), you MUST NOT repeat any step that already sent, \
wrote, or changed something, and you then finish whatever is still missing. \
If everything was already done, say so instead of doing it twice.
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
inside the session roots). Remote images are blocked by the browser's \
security policy except YouTube thumbnails and Google static maps — for any \
other web image, download it to a local file first and embed the local \
path. Mentioning the file path in prose does NOT show the picture; always \
add the image line too. Example: after saving /tmp/work/plot.png, end with \
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
- Attachments: the web UI can upload files — the ＋ button, a PASTE into the \
composer (Cmd/Ctrl+V on a desktop, the paste button on a phone), or a DRAG \
onto the window. From an iPhone, the share sheet can hand aish a file or a \
link via a Shortcut (README: "Share to aish from iOS"); a shared item waits \
above the composer until the owner taps it, and shares nothing to you until \
they do. Images (and PDFs, when your \
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

    def __init__(self, agent, logref: LogRef, bridge: Bridge,
                 origin: str = "user", trigger_meta: dict | None = None):
        self.agent = agent
        self.logref = logref
        self.bridge = bridge
        # Provenance (#160): "user" for a human-started chat, else "schedule" /
        # "email" / "webhook". Drives the drawer's "Automated" grouping and the
        # triggered-session capability policy (safe mutations auto-run, the rest
        # hold pending the owner's approval). trigger_meta carries context (e.g.
        # the message id that fired an email trigger).
        self.origin = origin
        self.trigger_meta = trigger_meta or {}
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
        self.custom_title: str | None = None  # a stored name; overrides the derived title
        # Auto-titling (#175). `title_auto` says the stored name was written by
        # the model, so the titler may replace it — a hand-typed rename sets it
        # False and the titler never runs again. `retitle_forced` makes the next
        # completed task retitle unconditionally: set on a fork, whose copied
        # log otherwise carries the PARENT's title forever.
        self.title_auto = False
        self.retitle_forced = False
        # last-actor-drives (#102): whoever last performed a session-affecting
        # action. Observers viewing this session see a "another tab is active"
        # hint; acting claims control. Never persisted — replay re-derives it.
        self.controller: Client | None = None
        # The last render failure recorded here (#201). The ledger wants to know
        # THAT an image did not render, not that it still hasn't on the tenth
        # look — and every duplicate record is a log write, which is what made
        # merely reading a chat mark it unread. The client gate is the primary
        # fix; this is the backstop no client can route around.
        self.last_render_error: tuple | None = None

    def open_turn(self, event: dict) -> None:
        """Emit the `user` event that opens a turn AND hand its id to the log,
        so the record the agent is about to write answers to the same name
        (#202). Every path that starts a turn goes through here — a turn whose
        two halves disagree about its id is a turn the user cannot remove."""
        self.logref.pending_turn = event.get("turn")
        self.bridge.emit(event)

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
        # The share inbox survives a restart, and it has to: a photo shared from
        # the phone at lunchtime is claimed when the app is next opened, and
        # aish-web is a launchd job that restarts in between. Held on disk, not
        # only in this process.
        self.shares_path = state_dir / "shares.json"
        self.shares: list[dict] = self._load_shares()
        self.config_path = config_path
        # The token is UNCONDITIONAL (#178 P1-2): with none configured, a
        # random per-run token is generated here and printed in the launch URL
        # by main(). There is no token-less mode — the `!` path is ungated by
        # design, so an open socket is remote code execution.
        self.token = token or secrets.token_urlsafe(24)
        # gitignore-style names hidden in the folder browser + @-file index (#87);
        # user-editable via config.toml [directory_picker], defaults otherwise.
        self.dir_ignore = list(dir_ignore_patterns or dir_ignore.DEFAULT_IGNORE)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.resumer: asyncio.Task | None = None  # startup restart-recovery (#164)
        self.sessions: dict[str, Session] = {}
        # Per-name in-flight cold opens (#178 P1-5): concurrent callers racing
        # on the same name await ONE build instead of each constructing a
        # Session — the loser's duplicate would overwrite the winner's in
        # `sessions`, orphaning a live worker whose approval cards could never
        # be answered (and double-appending to one log file).
        self._opening: dict[str, asyncio.Future[Session | None]] = {}
        self.clients: set[Client] = set()
        # The roster plane (#204): the last row BROADCAST for each session, and
        # the counter a client uses to notice it missed one. Deliberately "what
        # everyone has been told", not "what is true" — a snapshot sent to one
        # client tells the others nothing, so it must not seed this or the next
        # transition would diff clean and never reach them.
        self._roster: dict[str, dict] = {}
        self._roster_seq = 0
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
        # Base URL for notification deep-links (#163); set by create_app.
        self.public_url = ""
        # The workspace the server launched in; set by create_app. Sessions that
        # never moved live here, so it is the baseline a session row's directory
        # is judged against (see _row_cwd).
        self.base_cwd = ""
        # /trigger abuse guards (#178 P1-10); the knobs are create_app-injectable.
        self.trigger_rate_capacity = TRIGGER_RATE_CAPACITY
        self.trigger_rate_refill_s = TRIGGER_RATE_REFILL_S
        self.max_concurrent_triggered = MAX_CONCURRENT_TRIGGERED
        self._trigger_dedup: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._trigger_buckets: dict[str, tuple[float, float]] = {}
        # Agent workers get their OWN pool so a parked approval can never
        # starve the short ops (replay, log parsing, peeks) that stay on the
        # default to_thread executor — see WORKER_POOL_SIZE.
        self.worker_pool = ThreadPoolExecutor(
            max_workers=WORKER_POOL_SIZE, thread_name_prefix="aish-worker"
        )

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
        # Off the startup path: reopening sessions touches disk and launches
        # model work, and the server must be serving before any of that.
        self.resumer = asyncio.ensure_future(self._resume_interrupted())

    async def _resume_interrupted(self) -> None:
        """Pick up where a killed process left off (#164) — see RESUME_WINDOW.
        Applies to user chats and automated (triggered) sessions alike: neither
        has anything else that would ever restart the task. A resumed session is
        opened in the background exactly like a triggered one; its owner sees it
        in the drawer, and a triggered one still pushes its finish notification.

        Nothing here may propagate: a state dir that can't be read is a reason to
        start without recovery, never a reason not to start."""
        try:
            pending = SessionLog.interrupted_sessions(self.state_dir, RESUME_WINDOW)
        except OSError as exc:
            print(f"[resume] could not scan {self.state_dir}: {exc}", file=sys.stderr)
            return
        resumed = 0
        for path, info in pending:
            if resumed >= RESUME_MAX_SESSIONS:
                break
            if path.name in self.sessions:  # already running (never after a restart)
                continue
            if info["attempts"] >= RESUME_MAX_ATTEMPTS:
                print(f"[resume] {path.name}: left alone after {info['attempts']} "
                      "interrupted attempts", file=sys.stderr)
                continue
            try:
                # The same cold open a user's session-switch performs, so the
                # resumed session replays its full prior transcript when its
                # owner opens it — the resumed turn appends to that history
                # rather than looking like a session that began mid-thought.
                session = await self._open_by_name(path.name)
                history = await asyncio.to_thread(SessionLog.load_messages, path)
            except Exception as exc:  # noqa: BLE001 — one bad log must not stop the rest
                print(f"[resume] {path.name}: reopen failed: {exc!r}", file=sys.stderr)
                continue
            if session is None:
                continue
            # A run that died before its own user message was logged left the
            # model nothing to continue FROM, so that one re-issues the recorded
            # prompt; every other resume continues the conversation (RESUME_NOTE).
            text = RESUME_NOTE if any(m.get("role") == "user" for m in history) else info["prompt"]
            if not text:
                continue
            if text is RESUME_NOTE and info.get("in_flight"):
                # The steps that never reported back are the only ones whose
                # effect is genuinely unknown — name them so the model verifies
                # those specifically instead of re-running the whole task.
                text += "\n\nCut off mid-step. These had STARTED and never " \
                        "reported a result, so whether they took effect is " \
                        "UNKNOWN — check each before repeating it:\n- " \
                        + "\n- ".join(info["in_flight"])
            session.busy = True
            # A resume note is a real turn (it starts a task) that the human
            # never typed, so it renders as a system row rather than a blue user
            # bubble — classified by the SAME function the cold replay uses, so
            # hot and cold cannot drift (#171). A re-issued original prompt is
            # the user's own words and stays a normal bubble.
            session.open_turn(_user_event(text))
            session.runner = asyncio.ensure_future(
                self._run_task(session, text, resume=True)
            )
            resumed += 1
            print(f"[resume] {path.name}: retrying an interrupted task "
                  f"(attempt {info['attempts'] + 1})", file=sys.stderr)

    async def shutdown(self) -> None:
        """Unblock everything so Ctrl-C exits promptly: workers parked on an
        approval slot would otherwise wait forever and keep the interpreter
        alive. Denials are recorded in the audit log like any other deny."""
        if self.resumer is not None and not self.resumer.done():
            self.resumer.cancel()  # a restart mid-recovery just recovers again
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
        # Release the worker pool WITHOUT waiting on parked threads: the deny
        # loop above already unblocked every held approval, and wait=False +
        # cancel_futures (3.9+) drops anything still queued instead of
        # deadlocking shutdown on a thread that never returns.
        self.worker_pool.shutdown(wait=False, cancel_futures=True)
        for client in list(self.clients):
            with contextlib.suppress(Exception):
                await client.ws.close()

    def add_session(self, session: Session, *, default: bool) -> None:
        """`default` is keyword-only with NO default value (#178 P1-6): every
        call site must say whether this session becomes the bare-connection
        landing spot. The old `default=True` let handle_trigger silently
        re-point the default at an overnight automated session."""
        self.sessions[session.name] = session
        # The ONE funnel every session-creation path goes through (new, cold
        # open, /trigger, the server's first session), which is why the hold
        # announcer is wired here rather than four times over (#203) — and why
        # a session appearing is published from here too (#204). Both run on
        # the loop thread; `on_hold` is hopped there by the Bridge.
        session.bridge.on_hold = lambda: self._announce_hold(session)
        self._touch(session)
        if default:
            self._default = session

    def _token_ok(self, supplied: object) -> bool:
        """Constant-time check of the ALWAYS-present access token (#178 P1-2).
        Every gated endpoint requires it unconditionally — there is no
        token-less mode to fall through to."""
        return hmac.compare_digest(str(supplied or ""), self.token)

    async def _in_worker(self, fn, *args, **kwargs):
        """Run an agent-worker call — one that can PARK on an approval — on
        the dedicated pool instead of asyncio's default executor, so held
        approvals can never queue the short ops (replay/_show, parsing, peeks)
        behind them. See WORKER_POOL_SIZE for the livelock this prevents."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.worker_pool, functools.partial(fn, *args, **kwargs)
        )

    # -- /trigger abuse guards (#178 P1-10) ---------------------------------

    def _dedup_hit(self, key: str) -> str | None:
        """The session already opened for this dedup_key, if remembered.
        Expired entries are pruned on the way; a hit renews its LRU slot."""
        now = time.monotonic()
        expired = [
            k for k, (ts, _) in self._trigger_dedup.items()
            if now - ts > TRIGGER_DEDUP_TTL
        ]
        for k in expired:
            del self._trigger_dedup[k]
        hit = self._trigger_dedup.get(key)
        if hit is None:
            return None
        self._trigger_dedup.move_to_end(key)
        return hit[1]

    def _dedup_store(self, key: str, session_name: str) -> None:
        self._trigger_dedup[key] = (time.monotonic(), session_name)
        self._trigger_dedup.move_to_end(key)
        while len(self._trigger_dedup) > TRIGGER_DEDUP_MAX:
            self._trigger_dedup.popitem(last=False)

    def _trigger_rate_ok(self, origin: str) -> bool:
        """Token bucket per origin value: trigger_rate_capacity is the burst,
        one token earned per trigger_rate_refill_s sustained."""
        now = time.monotonic()
        tokens, last = self._trigger_buckets.get(
            origin, (float(self.trigger_rate_capacity), now)
        )
        tokens = min(
            float(self.trigger_rate_capacity),
            tokens + (now - last) / self.trigger_rate_refill_s,
        )
        ok = tokens >= 1.0
        self._trigger_buckets[origin] = ((tokens - 1.0) if ok else tokens, now)
        return ok

    def _running_triggered(self) -> int:
        return sum(
            1 for s in self.sessions.values() if s.origin != "user" and s.busy
        )

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

    @staticmethod
    def _snippet(session: Session) -> str:
        """The rail's preview line, from the SAME in-memory conversation the
        title comes from (#204). It was left off the roster row at first on the
        theory that a preview costs a file read — it does not: `_derive_snippet`
        is a backwards scan of a list this process already holds, returning at
        the first visible message. The one derived field that genuinely IS
        expensive is the SEARCH vocabulary, which casefolds every message of
        every chat, and that stays where it is — in the snapshot."""
        return SessionLog._derive_snippet(session.agent.messages[1:])

    def _hello(
        self,
        session: Session,
        pager: list[tuple[str, str, str, float, float]] | None = None,
        cmd_history: list[str] | None = None,
    ) -> dict:
        # The swipe pager pages through recent chats oldest→newest by last
        # interaction — open or not; resume loads cold ones from disk. Each page
        # carries its origin so the client pages within one lane (Recent vs
        # Automated, #160). The current session is always a page even before its
        # first message (a fresh chat is the newest thing by definition).
        # Each page also carries the chat's liveness, from the same in-memory
        # source `_send_sessions` reports (a chat not open is ""). That is what
        # lets the client's attention badge re-derive itself on every hello —
        # boot, reconnect, switch — instead of only when the rail is opened
        # (#203).
        open_states = {name: s.state() for name, s in self.sessions.items()}
        pages = [
            {
                "name": name,
                "title": title,
                "origin": origin,
                "ts": ts,
                # Last OUTPUT, which is what unread compares against (#203);
                # omitted when there is none, so a client reading an older
                # server falls back to `ts` exactly as it always did.
                **({"out": out} if out else {}),
                "state": open_states.get(name, ""),
            }
            for name, title, origin, ts, out in pager or []
        ]
        if all(page["name"] != session.name for page in pages):
            pages.append(
                {
                    "name": session.name,
                    "title": self._title(session),
                    "origin": session.origin,
                    # Newest thing there is, by definition — stamped like every
                    # other row so the client can order it.
                    "ts": time.time(),
                    # No output yet by construction — a chat that had any would
                    # have been a page already.
                    "state": session.state(),
                }
            )
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
            # Where this client's roster stream starts (#204). Every
            # `session_changed` carries the next number; a gap means something
            # was missed and the client asks for a snapshot rather than going
            # on believing a stale row.
            "roster_seq": self._roster_seq,
            "pager": pages,
            # The user's own successful ! commands, cross-session, most-run first:
            # terminal-mode autocomplete draws from this personal palette (#104).
            "cmd_history": cmd_history or [],
            # Anything shared from the phone and not yet claimed (#213). Carried
            # on hello because a share arrives while nothing is connected — that
            # is the normal case, not the exception — so the `shared` broadcast
            # alone would only ever reach a tab that happened to be open.
            "shares": self.shares_snapshot(),
        }

    @staticmethod
    def _cwd_event(session: Session) -> dict:
        return {
            "type": "cwd_changed",
            "cwd": session.agent.cwd,
            "roots": [str(root) for root in session.agent.roots],
        }

    async def handle_ws(self, websocket: WebSocket) -> None:
        # Origin BEFORE token (#178 P1-2): WebSockets are exempt from the
        # same-origin policy, so a browser page on any site can open this
        # socket — same accept-then-close shape as the token path so the
        # rejection is distinguishable from a dead server.
        if not origin_allowed(
            websocket.headers.get("origin"), websocket.headers.get("host", "")
        ):
            await websocket.accept()
            await websocket.close(code=CLOSE_BAD_ORIGIN)
            return
        if not self._token_ok(websocket.query_params.get("token")):
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
        # Disk scan for the pager and the terminal-mode command palette (#104)
        # happens before the attach block below: it must not sit between
        # joining the viewer set and the transcript snapshot. One thread hop
        # for both — each is stat-cheap behind session.py's parse cache, so
        # the hop itself is most of the cost now.
        def scan() -> tuple[list, list]:
            return (
                SessionLog.pager_titles(self.state_dir),
                SessionLog.user_command_history(self.state_dir),
            )

        pager, cmd_history = await asyncio.to_thread(scan)
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
        # The queue area is backend-authoritative and reconstructed on attach
        # rather than replayed from the transcript — this is what makes it
        # survive a reconnect or a session switch.
        #
        # Waiting MESSAGES used to be the exception, and that was the bug: a
        # `queued` chip was sent only to the client that typed it ("its own
        # composer echo"), which holds right up until that client looks at a
        # different chat. The chip lives outside the transcript, so nothing
        # cleared it, and it followed the viewer into whatever chat they landed
        # on — where its Remove button dequeues from the session now on screen.
        # The message it named went on running in the chat it was queued in.
        # Re-sending the real queue here (and clearing on the client at every
        # repaint) makes the chips say what the SERVER is holding, which also
        # means a second device viewing the chat finally sees them at all.
        for position, (text, _attachments) in enumerate(session.queue, start=1):
            await client.ws.send_json(
                {"type": "queued", "position": position, "text": text}
            )
        # Sent after those so it lands on top of the freshly-rebuilt queue list
        # (#92) — a pending cd applies before any waiting message.
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
        """Dispatch one client message, then RECEIPT it if it asked for one.

        The receipt is a delivery fact, not a semantic one: "this arrived and
        was handled", never "it succeeded" — what happened is already carried
        by the events each feature emits. It exists because the client cannot
        otherwise tell a request that was handled from one that was never
        heard, and a WebSocket gives it no way to find out: a socket reports
        OPEN long after it died, accepting everything and answering nothing
        (a phone that has been asleep). Every action the client took on faith
        was therefore indistinguishable from one that vanished (#210).

        Stamped HERE rather than in each handler for one reason: a handler
        that forgets is a silent hole, and this way a new message type is
        receipted before anyone thinks about it. It is sent AFTER the handler
        so a raise means no receipt — an action that blew up did not happen,
        and the client must hear that as loudly as one that never arrived.
        """
        await self._dispatch_message(client, message)
        rid = message.get("rid")
        if rid:
            await client.ws.send_json({"type": "ack", "rid": str(rid)})

    async def _dispatch_message(self, client: Client, message: dict) -> None:
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
                    # The hold is released: the row goes back to running, and
                    # the OTHER devices need to hear it or they go on showing
                    # "Needs approval" for a card cleared here (#204).
                    self._touch(session)
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
        elif kind == "peek":
            # VIEW message: warming a recent chat claims nothing.
            await self._peek(client, str(message.get("path", "")))
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
        elif kind == "redact":
            self._claim(client)
            await self._redact_turn(client, str(message.get("turn", "")).strip())
        elif kind == "rate":
            # Not a session action in the control sense — rating an answer does
            # not claim the chat or interrupt anything running.
            await self._rate_turn(client, message)
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
        elif kind == "share_drop":
            # A VIEW message: taking a shared file into the composer, or binning
            # it, acts on the server's inbox and not on any chat — so it must
            # not claim control of the session the client happens to be looking
            # at. Claimed and dismissed are the same operation here; what
            # differs is only what the CLIENT does with it first.
            self._drop_share(str(message.get("id", "")))
        elif kind == "dequeue":
            self._dequeue(client, str(message.get("text", "")))
        elif kind == "dequeue_cwd":
            viewed = client.viewing
            if viewed is not None:
                viewed.pending_cwd = None
                viewed.bridge.emit({"type": "cwd_dequeued"}, record=False)
        elif kind == "render_error":
            # VIEW message: the browser reporting what it could not render.
            self._render_error(client, message)
        elif kind == "client_debug":
            # Device-side diagnostics (viewport state on iOS, etc.) — printed
            # to the server log because the phone has no reachable console.
            print(f"CLIENT_DEBUG: {message.get('text', '')}", flush=True)
        else:
            await self._refuse(client, f"unknown message type {kind!r}")

    def _render_error(self, client: Client, message: dict) -> None:
        """The browser reporting transcript content it could not render (#188).

        Two destinations, deliberately different. The trace record is the durable
        evidence, in the shape the curate ledger reads. It renders NOWHERE:
        nothing is emitted live and reconstruct_events skips it on replay, so hot
        and cold agree, and the user's signal is the broken-picture note already
        sitting in the answer. The model-facing note is only for a failure in a
        LIVE turn: merely OPENING an old chat whose images were long since
        evicted must not inject a note into it — that would be an unread
        conversation growing turns.

        The record used to be written ALWAYS, and that was wrong for the same
        reason (#201): a replay re-reported every dead image, so reading a chat
        appended to it, and the append made it look like new activity — the chat
        came back unread the moment you left it, forever. The client now reports
        only live failures; a repeat of the last recorded failure is dropped here
        so no other path can reintroduce the spam.

        The reported strings come off a socket, so they are capped and stripped
        of control characters. They are not treated as owner-authored: the note
        goes through add_system_note, never add_user_context, so a host in an
        src the MODEL chose can never launder itself into egress provenance.
        """
        session = client.viewing
        if session is None:
            return
        raw = message.get("items")
        if not isinstance(raw, list):
            return
        items = []
        for entry in raw[:RENDER_ERROR_MAX]:
            if not isinstance(entry, str):
                continue
            cleaned = _CONTROL_RE.sub(" ", entry).strip()[:RENDER_ERROR_SRC_CHARS]
            if cleaned:
                items.append(cleaned)
        if not items:
            return
        what = "image" if str(message.get("what", "image")) == "image" else "embed"
        live = bool(message.get("live"))
        signature = (what, tuple(items))
        if signature != session.last_render_error:
            session.last_render_error = signature
            session.logref.step({"kind": "render_error", "what": what, "items": items})
        if not live:
            return
        noun = f"{what}s" if len(items) > 1 else what
        note = RENDER_NOTE.format(
            count=len(items), noun=noun, items=", ".join(items)
        )
        with contextlib.suppress(Exception):  # a note must never break a session
            session.agent.add_system_note(note)

    async def _refuse(self, client: Client, text: str, name: str = "") -> None:
        """Tell ONE client that the request it just made will not happen.

        `name` is the session the refusal is ABOUT, when there is one. A
        refusal that does not say what it refused is one the client can only
        render as prose — it cannot tell "the delete you asked for" from "some
        unrelated request", which is exactly the confusion #210 was reported
        as. Every refusal that concerns one chat should carry it.

        An `error` event meant two unrelated things, and the client could only
        assume the worse one. "Your TURN failed" (the model died, the tool
        blew up) ends the turn: the live trace closes, busy clears, Retry
        appears. "I will not do THAT" (delete-while-busy, empty title, queue
        full) changes nothing about the turn — but it arrived in the same
        shape, so refusing to delete a message while the chat was working tore
        down the running turn's card and took Stop and Retry with it, leaving a
        task running with no way to reach it. The refusal was correct; the
        collateral was total.

        The split was already visible in the code and simply not carried across
        the wire: a turn failure goes through `session.bridge.emit` (recorded,
        fanned out to every viewer, part of the transcript), while a refusal
        goes down the ONE socket that asked and is never recorded. `refused`
        makes that legible to the client, which renders it as a toast and
        touches no turn state. Every direct-to-client error is one of these —
        stamp it here, at the site that knows, never guessed at the far end.
        """
        payload = {"type": "error", "code": "refused", "text": text}
        if name:
            payload["name"] = name
        await client.ws.send_json(payload)

    async def _reject_busy(self, client: Client, session: Session) -> bool:
        if session.busy:
            await self._refuse(
                client,
                "this session is busy — wait, or start a new "
                "session (＋) and work there in parallel",
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
                # Full path (not just the name) so /feedback's classic flow can
                # upload the file, not only see it (#152).
                notes.append(f"[image attached: {path.name} — you can see it; file at {path}]")
            elif in_uploads and size_ok and suffix == ".pdf" and "pdf" in support:
                documents.append(str(path))
                notes.append(f"[document attached: {path.name} — you can read it; file at {path}]")
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
            await self._refuse(client, "stop is not supported on this backend")
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

    async def _rate_turn(self, client: Client, message: dict) -> None:
        """Record 👍/👎 (+ an optional reason) against one answer (#207).

        Deliberately inert: it writes a record and echoes it to the other
        viewers so every open tab agrees. Nothing reads it during the session —
        it is evidence for the weekly pass and for the owner, and making it act
        would turn a feedback control into a lever the model can be steered by.
        """
        session = client.viewing
        if session is None:
            return
        turn = str(message.get("turn", "")).strip()
        rating = str(message.get("rating", "")).strip()
        if not turn or rating not in RATINGS:
            return
        comment = str(message.get("comment", "") or "")
        session.logref.rating(turn, rating, comment)
        # Recorded AND emitted, deliberately as two steps.
        #
        # `record` keeps it in the live transcript, so a reconnecting tab still
        # sees what was rated — the log-backed cold path already replays it, and
        # a rating that survived a cold open but vanished on a reconnect is the
        # worst of both. The recorded dict is exactly what
        # `reconstruct_events` emits, keeping hot and cold byte-identical.
        #
        # The DELIVERED copy carries `seen`, which the recorded one must not:
        # the people being shown this are the ones who just tapped it, and a
        # rating must never mark the chat unread for them.
        event = {"type": "rating", "turn": turn, "rating": rating, "comment": comment}
        # Appended to the transcript directly, then delivered ONCE. `record()`
        # would do both — and fan the event out a second time, so every viewer
        # received each rating twice. The recorded dict is exactly what
        # `reconstruct_events` produces (hot/cold parity); the delivered copy
        # carries `seen`, because the person being shown this is the one who
        # just tapped it and it must not mark the chat unread for them.
        session.bridge.transcript.append(dict(event))
        session.bridge.emit({**event, "seen": True}, record=False)

    async def _redact_turn(self, client: Client, turn: str) -> None:
        """Take one exchange out of this chat for good (#202).

        Three copies of a turn exist and all three have to go, or the removal is
        theatre: the on-disk log (SessionLog.redact_turn scrubs it and leaves a
        dated tombstone in its place), the live model's context (or the model
        keeps quoting what the user just removed), and the in-memory transcript
        every viewer replays from.

        That last one is rebuilt from the scrubbed log rather than edited in
        place. reconstruct_events is DEFINED to produce what a live session
        shows — that parity is the invariant the whole cold-open path rests on —
        so replacing the buffer with it repaints correctly AND guarantees no
        residue survives in memory for the next viewer to replay.

        Refused while the chat is working: the turn being removed may be the one
        running, its records are still being written, and agent.messages belongs
        to the worker thread. Stop first, then remove.
        """
        session = client.viewing
        if session is None or not turn:
            return
        if session.busy:
            await self._refuse(
                client, "can't delete a message while this chat is working — "
                    "stop the task (or let it finish) and try again",
                )
            return
        path = session.logref.log.path
        removed = await asyncio.to_thread(session.logref.redact_turn, turn)
        if removed is None:
            # The id named nothing: already removed (from another tab), or a
            # stale line-index id from a log written before turn ids. Both are
            # "there is nothing here to remove", which is the outcome asked for.
            await self._refuse(client, "that message is already gone")
            return
        session.agent.redact_turn(removed.text, removed.occurrence)
        events = await asyncio.to_thread(SessionLog.reconstruct_events, path)
        if events is None:  # pre-trace log: the flat-history fallback, as cold opens use
            history = await asyncio.to_thread(SessionLog.load_messages, path)
            events = [{"type": "history", "messages": history}]
        bridge = session.bridge
        bridge.truncated = len(events) > TRANSCRIPT_MAX
        bridge.transcript[:] = events[-TRANSCRIPT_KEEP:] if bridge.truncated else events
        # Through the bridge, so every viewer of this chat repaints — a removal
        # made on the phone must not leave the laptop showing the text.
        #
        # `seen` is true by construction here: this event reaches only clients
        # currently VIEWING the chat, and what they are being handed is its
        # corrected state. Without it the removal — which counts as activity, so
        # that every device's offline mirror refetches — would mark the chat
        # unread for the very people watching it happen.
        bridge.emit(
            {
                "type": "replay",
                "events": list(bridge.transcript),
                "truncated": bridge.truncated,
                "seen": True,
            },
            record=False,
        )
        if removed.title is not None:
            # An auto title described content that is gone, so it was re-derived
            # to the first surviving message — safe, but a description of where
            # the chat STARTED. Forcing a retitle earns a real name back at the
            # next completed task.
            session.custom_title = removed.title
            session.title_auto = True
            session.retitle_forced = True
            bridge.emit(
                {"type": "session_renamed", "name": session.name, "title": removed.title},
                record=False,
            )
        await self._send_sessions(client, "")

    def _dequeue(self, client: Client, text: str) -> None:
        """Drop the first still-waiting message matching `text` (the client's
        queued-chip remove button). A running task is never affected.

        The removal is announced through the bridge, mirroring `cwd_dequeued`:
        the chip is a view of what the SERVER is holding, so cancelling on the
        phone has to take it off the laptop too."""
        session = client.viewing
        if session is None:
            return
        for i, (queued, _attachments) in enumerate(session.queue):
            if queued == text:
                del session.queue[i]
                session.bridge.emit(
                    {"type": "dequeued", "text": text}, record=False
                )
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
                await self._refuse(client, f"queue full ({MAX_QUEUE} waiting)")
                return
            session.queue.append((text, attachments or []))
            # Through the BRIDGE, not this client's socket. It used to be "the
            # requesting client's own composer echo", and that reasoning is what
            # let the chip outlive the view it was drawn in: an unstamped event
            # sent straight down one socket is invisible to the session firewall
            # (#182), so nothing could tell it apart from an event about the
            # chat now on screen. Through the bridge it carries the session name
            # — dropped on arrival if the viewer has moved on — and every viewer
            # of this chat sees the queue, not just the device that typed it.
            # record=False: a waiting message is live state, not transcript, and
            # reconstruct_events emits no `queued` (hot/cold parity).
            session.bridge.emit(
                {"type": "queued", "position": len(session.queue), "text": text},
                record=False,
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
            session.open_turn(_turn_event(text))
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
        session.open_turn(_turn_event(text))
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

    async def _notify_done(self, session: "Session", result: str) -> None:
        """Ping the owner when a TRIGGERED session finishes (#163) — the
        'observe the outcome' half. The blocking Pushover POST is offloaded via
        to_thread so the event loop never stalls; a no-op for user sessions or
        when Pushover is unconfigured. Best-effort: pushover() never raises.

        Silent while anyone is viewing, mirroring notify_hold: an open tab
        already shows the answer, and without this every message typed INTO an
        automated session pushes a phone notification for work being watched
        live."""
        if session.origin == "user" or session.viewers or not notify.configured():
            return
        link = f"{self.public_url}/?session={session.name}" if self.public_url else None
        title = session.custom_title or "automated session"
        body = (result or "done").strip()[:300] or "done"
        await asyncio.to_thread(
            notify.pushover, f"aish finished — {title}", body,
            url=link, url_title="Open session", priority=0,
        )

    async def _run_task(
        self,
        session: Session,
        text: str,
        images: list[str] | None = None,
        documents: list[str] | None = None,
        resume: bool = False,
    ) -> None:
        # Bracket the run on disk (#164). Only a killed process leaves the
        # task_start unmatched, which is exactly what makes it the restart
        # signal — every ordinary ending (answer, error, cancel) reaches the
        # `finally`. The ! command path deliberately writes no marker: re-running
        # the user's own shell command unattended is not a recovery, it's a risk.
        # Running, and everyone hears it. This transition had NO announcement
        # before the roster plane (#204): a triggered job showed as idle on
        # every other device until something happened to ask.
        self._touch(session)
        session.logref.task_start(text)
        failure = ""  # set by either except arm; recorded on the way out
        try:
            if resume and isinstance(session.agent, Agent):
                # Keep the interrupted task's own tool output verbatim (#164):
                # the normal "old task" trim would stub out exactly the results
                # the resumed run must not recompute. claude-max keeps its own
                # session state and takes no such flag.
                result = await self._in_worker(
                    session.agent.run_task, text, images, documents, keep_history=True
                )
            elif images or documents:
                result = await self._in_worker(
                    session.agent.run_task, text, images, documents
                )
            else:
                result = await self._in_worker(session.agent.run_task, text)
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
            await self._notify_done(session, result)
            if result != CANCELLED_RESULT:  # a stopped turn named nothing
                await self._maybe_retitle(session)  # name it for its subject (#175)
        except ModelUnavailable as exc:
            failure = f"model unavailable: {exc}{_backend_hint(session.agent)}"
            session.bridge.emit({"type": "error", "text": failure})
        except Exception as exc:  # noqa: BLE001 — a task bug must not kill the server
            failure = f"task failed: {exc!r}"
            session.bridge.emit({"type": "error", "text": failure})
        finally:
            # HOW it ended, not just that it did (#203). The failure text was
            # live-only until now: a client that was not connected when a
            # background job died learned nothing, and cold replay could only
            # guess "cut off mid-step" from unfinished steps.
            if failure:
                session.logref.task_end("failed", failure)
            else:
                session.logref.task_end()
            await self._finish_turn(session)

    async def _run_user_command(self, session: Session, command: str) -> None:
        """A ! command: run the typed text directly as the user's own action —
        no model, no approval gate — exactly like the CLI's ! escape. !cd is the
        /cd alias, so it moves cwd + re-anchors the root AND refreshes the UI
        cwd (like the / slash /cd path); any other command streams into a
        terminal block. Nothing here is model-driven, so the approval gate is
        untouched — the user typing a command is its own authorization."""
        self._touch(session)  # running (#204)
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
                # Worker pool, not to_thread: a user command can run long
                # (a build, a watch) and must not occupy the default pool.
                await self._in_worker(session.agent.run_user_command, command)
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
            # `set-clipboard on` (server-global) makes tmux emit an OSC 52
            # clipboard sequence when you copy — including a mouse-drag copy — so
            # the frontend's OSC 52 handler can put a REMOTE selection onto the
            # local desktop clipboard (#153). Runs before new-session so the
            # option is set whether we create or reattach; `\;` is tmux's command
            # separator (sh passes the literal `;` through).
            session = shlex.quote(TMUX_CONSOLE_SESSION)
            return f"tmux set-option -g set-clipboard on \\; new-session -A -s {session}", True
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
            await self._refuse(client, "no issue draft to file — start with /feedback")
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
        session.open_turn(_turn_event("Create the issue"))
        session.runner = asyncio.ensure_future(self._file_issue(session, command))

    async def _file_issue(self, session: Session, command: str) -> None:
        """Run the pinned `gh issue create` as a user-direct command (ungated,
        streams into a terminal block like any ! command) and then surface the
        new issue as a CLICKABLE link — gh prints the URL to stdout, which would
        otherwise sit as plain, unclickable text in the terminal block (#110)."""
        self._touch(session)  # running (#204)
        try:
            session.logref.command(command, "user-direct")
            output = await self._in_worker(session.agent.run_user_command, command)
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
        # Back to idle. The row goes to everyone; the `notice` is what makes it
        # a heads-up rather than silent bookkeeping, and the client suppresses
        # it for the chat it is actually looking at (a viewer already saw the
        # `done`).
        self._touch(session, notice="finished")

    # ---- the roster plane (#204) -----------------------------------------
    # A SECOND event plane, deliberately. The Bridge is the first: events
    # belonging to ONE conversation, delivered to whoever is reading that
    # conversation, and replayable as its transcript. A roster fact — "chat A
    # is running now" — is none of those three. Its audience is every client
    # whatever they are looking at, the session firewall would (correctly) drop
    # it as belonging to another chat, and recording it would put it in a
    # transcript it is not part of.
    #
    # So it gets its own channel. Not a new one, really: the two status pushes
    # this replaces already bypassed the Bridge and wrote straight to each
    # socket. This names that channel and gives it the three things it lacked.
    #
    # 1. ONE PUBLISHER. Every transition calls `_touch`. It was four fixes'
    #    worth of evidence that "announce it at exactly the right moments" is a
    #    rule nobody can keep: a chat STARTING was never announced, nor an
    #    approval being answered elsewhere, so the phone showed "Needs
    #    approval" for a card cleared on the laptop.
    # 2. IT DIFFS. An unchanged row publishes nothing, which makes calling it
    #    too often FREE — so the rule becomes "call it whenever you touched a
    #    session", which is a rule that survives contact with new code.
    # 3. A SEQUENCE NUMBER, so a client can tell "nothing changed" from "I
    #    missed something" and ask for a snapshot. Without it this is the same
    #    silent drift in a new costume.
    #
    # What the row carries is everything the server ALREADY HOLDS about a
    # session it is running — which, since it is the thing that ran the model
    # and wrote the answer, is nearly all of it. Title and preview both derive
    # from the in-memory conversation; state and cwd are attributes.
    #
    # Two things are deliberately absent, for two different reasons. The
    # TIMESTAMPS are the client's to stamp on arrival (see the client's note):
    # deriving them here would mean parsing the session's log, and a chat that
    # just did something has just changed its file, so the parse cache is
    # guaranteed to miss at exactly the moment a transition fires. The SEARCH
    # vocabulary is absent because it is the one derived field that is
    # genuinely expensive — it casefolds every message of every chat — and it
    # belongs to the snapshot, which is where a query is answered anyway.
    def _roster_row(self, session: Session) -> dict:
        return {
            "name": session.name,
            "title": self._title(session),
            "snippet": self._snippet(session),
            "state": session.state(),
            "origin": session.origin,
            "cwd": self._row_cwd(session.agent.cwd),
        }

    def _touch(self, session: Session, notice: str = "") -> None:
        """Publish this session's row if anything a client can see changed.

        Safe to call from anywhere that touched a session, including places
        where nothing changed — that is the point. `notice` marks the two
        transitions that are worth interrupting someone about; it is never part
        of the diff, so a repeat of an unchanged row stays silent."""
        row = self._roster_row(session)
        if self._roster.get(session.name) == row and not notice:
            return
        self._roster[session.name] = row
        self._roster_seq += 1
        event = {"type": "session_changed", "seq": self._roster_seq, "row": row}
        if notice:
            event["notice"] = notice
        self._broadcast(event)

    def _broadcast(self, event: dict) -> None:
        """Send to every connected client, viewer or not. Fire-and-forget on
        the loop thread: a roster fact is never worth blocking a caller, and a
        dead socket is dropped by its own disconnect."""
        for client in list(self.clients):
            try:
                client.outbox.put_nowait(event)
            except Exception:  # noqa: BLE001 — a client mid-teardown is not an error
                pass

    def _announce_hold(self, session: Session) -> None:
        """A chat stopped on an approval. It always publishes its new state —
        every client's list has to know — but the NOTICE is scoped: a hold
        someone is already looking at is a card on their screen, not a chat
        that wants a user who is somewhere else, and marking every approval
        would nudge the phone in your pocket once per command approved on the
        laptop. Runs on the loop thread, so this reads the viewer set without
        the benign race `on_wait` documents."""
        self._touch(session, notice="" if session.viewers else "held")

    def _row_cwd(self, cwd: str) -> str:
        """The directory to label a session row with, or "" for no label. A chat
        that never left the server's own workspace stamps the SAME path on every
        row, which is noise, not information — so the label appears only when the
        session sits somewhere else."""
        if not cwd or cwd == self.base_cwd:
            return ""
        return cwd

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
        # The working directory labels a row so parallel agents are legible at a
        # glance. A session open in memory answers live (it may have moved since
        # its last logged /cd); everything else falls back to the cwd recorded
        # in its log, so a row's directory does not depend on the accident of
        # which few sessions survived the MAX_OPEN_SESSIONS eviction sweep.
        open_cwds = {name: s.agent.cwd for name, s in self.sessions.items()}
        current = client.viewing.name if client.viewing is not None else ""
        await client.ws.send_json(
            {
                "type": "session_list",
                # The snapshot's own place in the stream (#204): deltas at or
                # below it are already reflected here and are dropped, so a
                # snapshot in flight cannot overwrite newer rows.
                "seq": self._roster_seq,
                "current": current,
                "sessions": [
                    {
                        "name": info.path.name,
                        "title": info.title,
                        "snippet": info.snippet,
                        # The chat's last ACTIVITY, not its file's mtime (#201):
                        # this row is what `sessionUnread` compares against the
                        # device's "seen" stamp, and a log touched by merely
                        # LOOKING at the chat must not read as new activity.
                        "ts": info.activity,
                        # Unread compares against OUTPUT, not activity (#203):
                        # a chat that is merely thinking writes trace records,
                        # and those moved `ts` past the device's last look.
                        **({"out": info.output} if info.output else {}),
                        "state": open_states.get(info.path.name, ""),
                        "cwd": self._row_cwd(
                            open_cwds.get(info.path.name) or info.cwd
                        ),
                        "origin": info.origin,
                    }
                    for info in infos
                ],
            }
        )

    async def _open_by_name(self, name: str) -> Session | None:
        """The open session called `name`, or loaded cold from disk; None when
        no such session exists (or the name fails the path-safety checks).

        Concurrent callers racing on the same name — restart recovery and a
        reconnecting PWA's ?session=, the real trigger case — share ONE build
        (#178 P1-5): the memory check and add_session are separated by two
        awaits, so without the guard both would see None, both would construct
        a Session, and the second add_session would orphan the first (its
        viewers and worker kept, its approvals unroutable — parked forever).
        The in-flight entry is removed on success AND failure, so a failed
        open never poisons the name."""
        existing = self.sessions.get(name)
        if existing is not None:
            return existing
        pending = self._opening.get(name)
        if pending is not None:
            return await pending
        future: asyncio.Future[Session | None] = asyncio.get_running_loop().create_future()
        self._opening[name] = future
        try:
            session = await self._cold_open(name)
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # consumed here; waiters (if any) still re-raise
            raise
        else:
            future.set_result(session)
            return session
        finally:
            del self._opening[name]

    async def _cold_open(self, name: str) -> Session | None:
        """Load `name` from its log into a fresh Session. Callers go through
        _open_by_name, whose in-flight guard is what makes this single-flight."""
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

    @staticmethod
    def _gone_error(name: str) -> dict:
        """The one shape every by-name miss answers with. The `code` + `name`
        are the machine-readable half of the contract: a client holding a
        cached transcript or a stale row needs to know the chat is gone rather
        than that some request failed, and matching on the prose text was never
        a contract."""
        return {
            "type": "error",
            "code": "no_such_session",
            "name": name,
            "text": f"no such session: {name}",
        }

    async def _resume(self, client: Client, name: str) -> None:
        if client.viewing is not None and name == client.viewing.name:
            await self._show(client, client.viewing)
            return
        session = await self._open_by_name(name)
        if session is None:
            await client.ws.send_json(self._gone_error(name))
            return
        await self._show(client, session)

    async def _peek(self, client: Client, name: str) -> None:
        """VIEW message: a session's transcript snapshot WITHOUT switching to
        it — the client warms the chats a tap is most likely to land on, so a
        switch paints instantly instead of showing the old chat for a round
        trip. No claim, no hello, no viewer-set change, nothing recorded; the
        answer goes only to the asking client. A miss answers `gone` on the peek
        itself — NOT the resume path's error event, whose toast would blame the
        user for a request they never made."""
        session = await self._open_by_name(name)
        if session is None:
            await client.ws.send_json({"type": "peek", "name": name, "gone": True})
            return
        await client.ws.send_json(
            {
                "type": "peek",
                "name": name,
                "events": list(session.bridge.transcript),
                "truncated": session.bridge.truncated,
            }
        )

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
            await self._refuse(
                client, "can't fork while this session is working — wait for "
                    "the current task to finish, then fork",
                )
            return
        src_path = source.logref.log.path
        has_history = any(
            m.get("role") in ("user", "assistant") for m in source.agent.messages[1:]
        )
        if not has_history or not src_path.is_file():
            await self._refuse(
                client, "nothing to fork yet — send a message first, then fork "
                    "to branch the conversation into a new session",
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
            await self._refuse(client, "can't fork from there — that answer is out of range")
            return
        session = await self._open_by_name(new_path.name)
        if session is None:  # pragma: no cover — we just wrote a valid session file
            await self._refuse(client, "fork failed")
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
        # The copied log carries the PARENT's history, so the derived title (and
        # any inherited auto-title) names the parent. The fork earns its own name
        # at its first completed turn — where it starts to differ (#175).
        session.retitle_forced = True
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
            await client.ws.send_json(self._gone_error(name))
            return
        if session is not None and session.state() != "idle":
            # Never kill work as a side effect of a delete.
            await self._refuse(
                client,
                "task still running in that session — "
                "stop it (or let it finish) before deleting",
                name=name,
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
        # To EVERY client, not just the one that asked (#204): a chat deleted
        # on the laptop used to stay on the phone's list until it happened to
        # refresh, and tapping it opened a chat that no longer existed.
        self._roster.pop(name, None)
        self._roster_seq += 1
        self._broadcast({"type": "session_deleted", "name": name, "seq": self._roster_seq})
        await self._send_sessions(client, "")

    async def _rename_session(self, client: Client, name: str, title: str) -> None:
        """Give a chat a custom title. Persisted as an append-only
        `kind:"title"` record (no rewrite of the log). If the session is open
        its in-memory title is updated too, so the drawer AND the header both
        reflect the new name at once."""
        title = title.strip()[:RENAME_MAX]
        if not title:
            await self._refuse(client, "a chat title can't be empty")
            return
        session = self.sessions.get(name)
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or (session is None and not path.is_file()):
            await client.ws.send_json(self._gone_error(name))
            return
        if session is not None:
            # Append through the session's own open handle so a single writer
            # touches the file; mirror the name into memory for the hot path.
            await asyncio.to_thread(session.logref.log.set_title, title)
            session.custom_title = title
            session.title_auto = False  # hand-typed: the auto-titler stands down for good
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
        # `session_renamed` stays a broadcast of its own — the header of a chat
        # OTHER clients are viewing follows it, which a roster row does not
        # cover. The roster row carries the new title for their LISTS (#204).
        self._broadcast({"type": "session_renamed", "name": name, "title": title})
        if session is not None:
            self._touch(session)
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
            await self._refuse(
                client, "claude-max runs a different agent loop — restart with "
                    f"`aish-web --model {spec}` to switch",
                )
            return
        try:
            chat, provider, name = await asyncio.to_thread(backends.make_chat, spec)
        except backends.BackendError as exc:
            await self._refuse(client, str(exc))
            return
        session.agent.chat = chat
        session.agent.model = name
        session.agent.provider = provider
        session.logref.model(model_spec(session.agent))
        saved = False
        if message.get("save"):
            if self.config_path is None:
                await self._refuse(client, "no config path available — cannot save")
            else:
                error = save_default_model(self.config_path, spec)
                if error:
                    await self._refuse(client, error)
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
            await self._refuse(client, result)
            return
        # rebase fired on_state → the top-bar chip + queue-card refresh; no
        # manual _cwd_event needed (issue #95 unified that path).

    async def _add_dir(self, client: Client, path: str) -> None:
        session = client.viewing
        if session is None or await self._reject_busy(client, session) or not path:
            return
        result = await asyncio.to_thread(session.agent.add_root, path)
        if result.startswith("ERROR"):
            await self._refuse(client, result)
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
        if not self._token_ok(request.query_params.get("token")):
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

    async def handle_trigger(self, request) -> JSONResponse:
        """POST /trigger — programmatic ingress for NON-user origins (#160):
        schedule / email / webhook. Token-gated; the model has no path here
        (only local automation like the email poller calls it). Spawns a
        Session tagged with the origin, launches the prompt as a task, and
        returns the session name so the caller — and a later notification — can
        deep-link straight to the automated session.

        Security: the TOKEN is the gate, and it always exists (#178 P1-2 —
        generated at startup when none is configured), so the old loopback
        fallback is gone: behind a same-host reverse proxy every request looks
        loopback, so it never proved anything. The Origin check runs FIRST —
        a cross-origin text/plain POST is a CORS simple request (no preflight),
        so any page the owner visits could otherwise fire one from the browser
        (#178 P1-2); non-browser callers send no Origin and pass.

        Abuse guards (#178 P1-10), after the gates: meta.dedup_key idempotency
        (a repeat POST answers 200 with the existing session + deduped:true),
        a per-origin token bucket, and a cap on concurrently running
        triggered sessions — both limits answer 429 + Retry-After before any
        session is created."""
        if not origin_allowed(
            request.headers.get("origin"), request.headers.get("host", "")
        ):
            return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        if not self._token_ok(request.query_params.get("token")):
            return JSONResponse({"error": "bad token"}, status_code=403)
        try:
            body = json.loads(await request.body() or b"{}")
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse({"error": "prompt is required"}, status_code=400)
        origin = str(body.get("origin") or "webhook").strip()
        if origin == "user":
            return JSONResponse({"error": "origin must not be 'user'"}, status_code=400)
        raw_meta = body.get("meta")
        meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
        title = str(body.get("title") or "").strip()
        model_override = str(body.get("model") or "").strip()

        # Abuse guards (#178 P1-10), in this order: a dedup hit is an
        # idempotent SUCCESS and consumes nothing (a retry storm re-sending
        # one key must not burn rate tokens), and both 429s come BEFORE any
        # session exists. No dedup_key → every POST fires (existing clients
        # keep working unchanged).
        dedup_key = str(meta.get("dedup_key") or "")
        if dedup_key:
            existing = self._dedup_hit(dedup_key)
            if existing is not None:
                return JSONResponse(
                    {"session": existing, "origin": origin, "deduped": True}
                )
        retry_after = {"Retry-After": str(TRIGGER_RETRY_AFTER_S)}
        if not self._trigger_rate_ok(origin):
            return JSONResponse(
                {"error": f"rate limit exceeded for origin {origin!r}"},
                status_code=429,
                headers=retry_after,
            )
        if self._running_triggered() >= self.max_concurrent_triggered:
            # Refusal is safe by contract: the poller marks a message
            # processed only AFTER a successful trigger, so a 429'd delivery
            # retries on the next poll instead of being lost.
            return JSONResponse(
                {"error": "too many concurrent triggered sessions"},
                status_code=429,
                headers=retry_after,
            )

        self._evict_idle()
        try:
            session, _ = await asyncio.to_thread(
                self.open_session, None, origin, meta, model_override
            )
        except backends.BackendError as exc:
            # Fail CLOSED (#186): a privacy-scoped trigger asked for a model
            # this server cannot build — refusing beats silently running the
            # prompt (which may aggregate private-session excerpts) on the
            # cloud default. 503 so the caller retries after fixing the model.
            return JSONResponse(
                {"error": f"model unavailable: {exc}"}, status_code=503
            )
        # NEVER the default (#178 P1-6): an overnight trigger must not become
        # the landing spot for the next bare connect (nor eviction-immune).
        self.add_session(session, default=False)
        if title:
            session.custom_title = title
            session.logref.log.set_title(title)
        session.busy = True
        # The trigger's own prompt: a schedule, an email, a webhook — not the
        # owner typing (#171). Marked here because the text is arbitrary; the
        # replay identifies the same message by its position in a non-user
        # session, since it is always that session's opening turn.
        session.open_turn(_user_event(prompt, "trigger"))
        session.runner = asyncio.ensure_future(self._run_task(session, prompt))
        if dedup_key:
            # Recorded only once the session actually exists, so a failed
            # open never poisons the key for the retry that would succeed.
            self._dedup_store(dedup_key, session.name)
        return JSONResponse({"session": session.name, "origin": origin})

    def _store_upload(self, name: str, body: bytes) -> Path:
        """Write `body` into the uploads dir under a name that is not already
        taken. The ONE writer for both /upload (the composer) and /share (the
        phone), so a shared file is indistinguishable from a picked one
        everywhere downstream — same directory, same session root, same
        _classify_attachments verdict."""
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        target = self.uploads_dir / name
        stem, suffix = target.stem, target.suffix
        counter = 1
        while target.exists():
            target = self.uploads_dir / f"{stem}-{counter}{suffix}"
            counter += 1
        target.write_bytes(body)
        return target

    @staticmethod
    def _upload_name(raw: str) -> str | None:
        """The stored file name, or None if the client's is unusable. Rejects
        anything that is not a plain basename — a name is data from the network,
        and this is the only thing standing between it and a write."""
        name = os.path.basename((raw or "").strip())
        if not name or name.startswith(".") or name == "..":
            return None
        return name

    async def handle_upload(self, request) -> JSONResponse:
        """POST /upload?name=<filename>, raw body — no multipart, so no extra
        dependency. Files land in <state_dir>/uploads (a session root, so the
        agent's read_file auto-approves them)."""
        if not self._token_ok(request.query_params.get("token")):
            return JSONResponse({"error": "bad token"}, status_code=403)
        name = self._upload_name(request.query_params.get("name", ""))
        if name is None:
            return JSONResponse({"error": "invalid file name"}, status_code=400)
        body = await request.body()
        if not body:
            return JSONResponse({"error": "empty upload"}, status_code=400)
        if len(body) > UPLOAD_MAX_BYTES:
            return JSONResponse(
                {"error": f"file too large (max {UPLOAD_MAX_BYTES // (1024 * 1024)} MB)"},
                status_code=413,
            )
        return JSONResponse({"path": str(self._store_upload(name, body))})

    # ---- the share inbox (#213) -----------------------------------------
    # Everything below is deliberately inert: a share is STORED and ANNOUNCED,
    # and that is all. No session is opened, no model is called, nothing is
    # executed. The share sheet is a way to hand aish a file, not a trigger.

    def _load_shares(self) -> list[dict]:
        """Unclaimed shares from the last run. A file that cannot be read is a
        reason to start with an empty inbox, never a reason not to start."""
        try:
            data = json.loads(self.shares_path.read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("id")]

    def _save_shares(self) -> None:
        try:
            self.shares_path.parent.mkdir(parents=True, exist_ok=True)
            self.shares_path.write_text(json.dumps(self.shares))
        except OSError as exc:  # a full or read-only state dir must not 500
            print(f"[share] could not persist the inbox: {exc}", file=sys.stderr)

    def _prune_shares(self) -> None:
        """Age and size bounds. An inbox nobody empties is a nag, and one that
        grows without limit is a leak — both end with the owner ignoring it."""
        cutoff = time.time() - SHARE_TTL_S
        self.shares = [s for s in self.shares if float(s.get("at") or 0) >= cutoff]
        if len(self.shares) > SHARE_MAX_ITEMS:
            self.shares = self.shares[-SHARE_MAX_ITEMS:]

    def shares_snapshot(self) -> list[dict]:
        """What the composer should be offering, oldest first. Pruned on read
        so an inbox left over a holiday is already correct in the first hello,
        without a timer whose only job is to expire something nobody asked for."""
        before = len(self.shares)
        self._prune_shares()
        if len(self.shares) != before:
            self._save_shares()
        return list(self.shares)

    async def handle_share(self, request) -> JSONResponse:
        """POST /share?name=<filename>&text=<text>&source=<label> — the iOS
        share sheet's way in (a Shortcut; see README).

        `name` is what decides how the raw body is read, and it is the whole
        interface: WITH a name the body is a file, exactly as /upload; WITHOUT
        one it is text. That second form exists because Safari shares a URL, not
        a file, and percent-encoding a shared link into a query string inside
        Shortcuts is the kind of thing that works until someone shares a link
        with an `&` in it. `text=` in the query still works for short things,
        and both a file and text may be sent (a page shared as URL + screenshot).

        Answers 200 with the stored item. It does NOT start anything: the item
        waits in the inbox until the owner claims it in the composer.
        """
        if not self._token_ok(request.query_params.get("token")):
            return JSONResponse({"error": "bad token"}, status_code=403)
        text = (request.query_params.get("text") or "").strip()
        body = await request.body()
        if len(body) > UPLOAD_MAX_BYTES:
            return JSONResponse(
                {"error": f"file too large (max {UPLOAD_MAX_BYTES // (1024 * 1024)} MB)"},
                status_code=413,
            )
        raw_name = request.query_params.get("name", "")
        path: Path | None = None
        if body and raw_name.strip():
            name = self._upload_name(raw_name)
            if name is None:
                return JSONResponse({"error": "invalid file name"}, status_code=400)
            path = self._store_upload(name, body)
        elif body:
            shared = body.decode("utf-8", errors="replace").strip()
            # Truncation is announced, never silent: a shared page's text
            # arriving half-length with nothing saying so is worse than a
            # visible cut the owner can act on.
            if len(shared) > SHARE_TEXT_MAX:
                shared = shared[:SHARE_TEXT_MAX] + "\n… (truncated by aish)"
            text = f"{text}\n{shared}".strip() if text else shared
        if not path and not text:
            return JSONResponse({"error": "share is empty"}, status_code=400)
        item = {
            "id": secrets.token_urlsafe(8),
            "name": path.name if path else (text.splitlines()[0][:60] if text else ""),
            "path": str(path) if path else "",
            "text": text,
            # Free-form label from the Shortcut ("iPhone", "Photos"), shown as
            # the chip's provenance. Bounded because it is arbitrary input.
            "source": (request.query_params.get("source") or "share sheet")[:40],
            "at": time.time(),
        }
        self.shares.append(item)
        self._prune_shares()
        self._save_shares()
        # Every open tab, not just one: which device the owner picks up next is
        # not knowable here.
        self._broadcast({"type": "shared", "items": self.shares_snapshot()})
        return JSONResponse({"id": item["id"], "path": item["path"]})

    def _drop_share(self, share_id: str) -> None:
        """Claimed or dismissed — either way it leaves the inbox and every tab
        hears the same list. The uploaded FILE stays where it is: a claimed
        share is now an attachment the composer is holding by path, and a
        dismissed one is no different from any other file in uploads."""
        before = len(self.shares)
        self.shares = [s for s in self.shares if s.get("id") != share_id]
        if len(self.shares) == before:
            return
        self._save_shares()
        self._broadcast({"type": "shared", "items": self.shares_snapshot()})

    async def handle_file(self, request) -> FileResponse | JSONResponse:
        """GET /file?path=<abs> — serves an image file so the transcript can
        render model-generated charts/diagrams inline (issue #9). Scoped like
        approval: symlinks resolved BEFORE the containment check, and anything
        outside the roots of ANY open session is refused. (This HTTP endpoint
        has no socket context, so with many concurrent viewers it must accept a
        path in scope for the session that produced it — the union of open
        sessions' roots, which for a single session is exactly that session.)"""
        if not self._token_ok(request.query_params.get("token")):
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
        if not any(path.is_relative_to(r) for r in self._image_roots()):
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
        """The ONE boundary a local image may be displayed from — used by both
        /file (the chat) and the PDF exporter (issue #133). Each open session's
        own definition (`Agent.image_roots`: its roots, its media store, its
        scratch workspace) plus the uploads dir.

        These two callers disagreed until #188: the exporter trusted the scratch
        workspace and /file did not, so an image the model wrote where it is
        told to write throwaway files printed fine in a PDF and 403'd in the
        chat. One method, no drift.

        The union across sessions is deliberate: /file is plain HTTP with no
        socket context, so with several viewers it must accept a path in scope
        for whichever session produced it. Outside this set, a local
        `![](path)` renders as a link card and is never read."""
        roots = [self.uploads_dir.resolve()]
        for session in self.sessions.values():
            roots.extend(Path(r).resolve() for r in session.agent.image_roots())
        return roots

    def _model_title(self, session: Session, markdown_text: str) -> str | None:
        """Ask the session's OWN model to title its answer (blocking; runs in a
        thread). Same model that wrote the answer — it already has the subject
        matter, and no other backend gets to see the text. Called directly, not
        through run_task, so nothing lands in the conversation or the log.

        None on anything unusual (no chat callable — claude-max drives its own
        SDK loop and exposes none — a transport error, or a reply that isn't a
        title); the caller falls back to the deterministic lead."""
        agent = getattr(session, "agent", None)
        chat = getattr(agent, "chat", None)
        if agent is None or chat is None:
            return None
        prompt = TITLE_PROMPT.format(body=markdown_text[:TITLE_SOURCE_CHARS])
        try:
            response = chat(
                model=agent.model,
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                options={"num_ctx": getattr(agent, "num_ctx", 8192)},
                think=False,
                stream=False,
            )
            return export.clean_title(response.message.content or "")
        except Exception:  # noqa: BLE001 — a title is never worth failing an export
            return None

    async def _answer_title(self, session_name: str, markdown_text: str) -> str:
        """The exported answer's title. The model writes it; `derive_title` (the
        answer's lead heading or first sentence) is the floor whenever it can't
        — a title is a nicety, so it never blocks or fails an export."""
        fallback = export.derive_title(markdown_text)
        session = self.sessions.get(session_name) or self._default
        if session is None:
            return fallback
        try:
            title = await asyncio.wait_for(
                asyncio.to_thread(self._model_title, session, markdown_text), TITLE_TIMEOUT
            )
        except Exception:  # noqa: BLE001 — timeout or backend blow-up, same answer
            return fallback
        return title or fallback

    # ---- auto-titling a chat (#175) --------------------------------------
    # A title has to be readable off disk with no model call — the drawer lists
    # sessions cold (`list_sessions`) and the pager peeks them (`_peek`). So it
    # is computed at WRITE time and persisted as the same `kind:"title"` record
    # a rename writes; the read path is untouched.

    @staticmethod
    def _title_source(session: Session) -> tuple[str, str, str]:
        """(current title, exchanges to name from, the LATEST exchange alone).

        The namer gets how the chat opened and where it is now — enough to name
        a conversation, and far short of the whole transcript. The drift gate
        gets only the latest exchange: measuring drift against the opening too
        would mean a chat could never drift from a title derived from it.
        """
        messages = [
            m
            for m in getattr(session.agent, "messages", [])[1:]
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
        ]
        if not messages:
            return "", "", ""

        def render(items: list[dict]) -> str:
            # Attachment notes out: the namer needs the conversation, and an
            # absolute uploads path in its source is how one ends up in a title.
            # A wordless turn still says what it carried, which names it fine.
            def body(message: dict) -> str:
                raw = message.get("content") or ""
                clean = strip_attachment_notes(raw)
                return (clean or ", ".join(attachment_names(raw)))[:SESSION_TITLE_CHARS]

            return "\n\n".join(f"{m['role']}: {body(m)}" for m in items)

        # First two messages and last two, never overlapping: for a short chat
        # the tail is simply empty rather than a duplicate of the head.
        head = messages[:2]
        tail = messages[max(2, len(messages) - 2) :]
        return WebServer._title(session), render([*head, *tail]), render(tail or head)

    def _model_session_title(self, session: Session, current: str, body: str) -> str | None:
        """Ask the session's own model to name the conversation (blocking; runs
        in a thread). Same shape as `_model_title` — a bare prompt, no tools, no
        run_task, so the naming turn never enters the conversation or the log."""
        agent = getattr(session, "agent", None)
        chat = getattr(agent, "chat", None)
        if agent is None or chat is None:  # claude-max drives its own SDK loop
            return None
        prompt = SESSION_TITLE_PROMPT.format(current=current or "(none yet)", body=body)
        try:
            response = chat(
                model=agent.model,
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                options={"num_ctx": getattr(agent, "num_ctx", 8192)},
                think=False,
                stream=False,
            )
            return export.clean_title(response.message.content or "")
        except Exception:  # noqa: BLE001 — a name is never worth failing a turn
            return None

    def _retitle_due(self, session: Session, turns: int, recent: str) -> bool:
        """Whether this completed turn earns a model call.

        Never for a hand-typed name (that decision is the user's, permanently).
        Always on a fork's first turn — its copied log wears the parent's title.
        Otherwise at turns 1, 3, 7, 15 … and, past the first, only once the chat
        has actually moved off its current title (a free lexical check)."""
        if session.custom_title and not session.title_auto:
            return False
        if session.retitle_forced:
            return True
        if turns != RETITLE_FIRST_TURN and (turns + 1) & turns:
            return False
        if turns == RETITLE_FIRST_TURN:
            return True  # the title is still the raw prompt — always worth replacing
        return title_drifted(WebServer._title(session), recent)

    async def _maybe_retitle(self, session: Session) -> None:
        """Name the chat after what it is about, once a turn has finished."""
        turns = sum(
            1 for m in getattr(session.agent, "messages", [])[1:] if m.get("role") == "user"
        )
        current, body, recent = self._title_source(session)
        if not body or not self._retitle_due(session, turns, recent):
            return
        try:
            title = await asyncio.wait_for(
                asyncio.to_thread(self._model_session_title, session, current, body),
                TITLE_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 — timeout or backend blow-up: keep the old name
            return
        session.retitle_forced = False  # attempted; don't re-force on the next turn
        if not title or title == current:
            return
        title = title[:RENAME_MAX]
        await asyncio.to_thread(session.logref.log.set_title, title, True)
        session.custom_title = title
        session.title_auto = True
        # record=False: a rename is UI state, not part of the transcript — cold
        # replay re-reads the name from the log's own title record.
        session.bridge.emit(
            {"type": "session_renamed", "name": session.name, "title": title}, record=False
        )

    async def handle_export_answer(self, request) -> Response | JSONResponse:
        """POST /export/answer, raw markdown body — renders one answer to a PDF
        the browser downloads. Conversion is local (see export.py); embedded
        media (remote images, map snapshots, video thumbnails) may be fetched at
        export time, each bounded by a timeout with link-card fallback.

        The document title (and so the download name) comes from the ANSWER, not
        from the prompt that produced it — see `_answer_title`."""
        if not self._token_ok(request.query_params.get("token")):
            return JSONResponse({"error": "bad token"}, status_code=403)
        raw = await request.body()
        if not raw:
            return JSONResponse({"error": "empty answer"}, status_code=400)
        if len(raw) > EXPORT_MAX_BYTES:
            return JSONResponse({"error": "answer too large to export"}, status_code=413)
        markdown_text = raw.decode("utf-8", errors="replace")
        title = await self._answer_title(request.query_params.get("session", ""), markdown_text)
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
        if not self._token_ok(request.query_params.get("token")):
            return JSONResponse({"error": "bad token"}, status_code=403)
        name = request.query_params.get("session", "").strip()
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or not path.is_file():
            return JSONResponse({"error": f"no such session: {name}"}, status_code=404)
        image_roots = self._image_roots()

        def build() -> tuple[bytes, str]:
            messages, _, custom_title, *_ = SessionLog._parse(path)
            title = custom_title or SessionLog._derive_title(messages) or "aish session"
            return export.render_session_pdf(messages, title, image_roots), title

        try:
            data, title = await asyncio.to_thread(build)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"export failed: {exc}"}, status_code=500)
        return self._pdf_response(data, export.safe_pdf_filename(title, "aish-session"))

    # ---- offline mirror (#165) ------------------------------------------
    # Two read-only endpoints, deliberately outside the WebSocket: the PWA's
    # background sync must not need a session slot, and an Agent must never be
    # constructed just because a phone wanted to read old text. Both mirror
    # handle_export_session's shape — token check, name safety, work off-thread,
    # straight off disk — because that is already the proven read-only path.

    def _offline_path(self, request) -> Path | None:
        """The requested session's log, or None when the name fails the same
        path-safety check every by-name endpoint applies."""
        name = request.query_params.get("session", "").strip()
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or not path.is_file():
            return None
        return path

    async def handle_offline_index(self, request) -> JSONResponse:
        """GET /offline/index — the mirror's catalogue: every session's
        identity and last-modified stamp. The client diffs it against what it
        already holds, so a sync that changes nothing costs exactly one small
        request instead of re-downloading the archive."""
        if not self._token_ok(request.query_params.get("token")):
            return JSONResponse({"error": "bad token"}, status_code=403)
        infos = await asyncio.to_thread(SessionLog.list_sessions, self.state_dir)
        return JSONResponse(
            {
                "rev": STATIC_REV,
                "sessions": [
                    {
                        "name": info.path.name,
                        "title": info.title,
                        "snippet": info.snippet,
                        "ts": info.activity,  # last activity, not file mtime (#201)
                        **({"out": info.output} if info.output else {}),  # #203
                        "origin": info.origin,
                    }
                    for info in infos
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    async def handle_offline_session(self, request) -> Response | JSONResponse:
        """GET /offline/session?session=<name>&since=<n>&sig=<s> — a session's
        renderable event stream for the local mirror.

        Three layers of "don't send what the client already has", cheapest
        first: a matching `If-None-Match` returns 304 with no body at all; a
        `since`/`sig` pair whose prefix still checks out returns only the events
        after it; anything else returns the whole session. `sig` is always the
        server's own fingerprint handed back unchanged, so the client stores two
        opaque values and never has to reason about the protocol."""
        if not self._token_ok(request.query_params.get("token")):
            return JSONResponse({"error": "bad token"}, status_code=403)
        path = self._offline_path(request)
        if path is None:
            name = request.query_params.get("session", "").strip()
            return JSONResponse({"error": f"no such session: {name}"}, status_code=404)

        stat = path.stat()
        # Weak validator: mtime+size identifies a version of an append-only log
        # without reading it — the whole point is to answer an unchanged session
        # without parsing megabytes of JSONL.
        etag = f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})

        try:
            since = max(0, int(request.query_params.get("since") or 0))
        except ValueError:
            since = 0
        client_sig = request.query_params.get("sig") or ""

        def build() -> dict:
            events = offline_events(path)
            parsed = SessionLog._parse(path)
            messages, custom_title, origin = (
                parsed.messages, parsed.title, parsed.origin
            )
            title = SessionLog._truncate_title(
                custom_title or SessionLog._derive_title(messages)
            )
            total = len(events)
            base = 0
            if 0 < since <= total and client_sig and _prefix_sig(events, since) == client_sig:
                base = since
            return {
                "session": path.name,
                "title": title,
                "snippet": SessionLog._derive_snippet(messages),
                "origin": origin,
                # Last activity, not the file's mtime (#201): the mirror ages
                # its cache on this, and a glance must not refresh a chat's
                # standing. The ETag above stays mtime-based — that one IS
                # about the bytes on disk.
                "ts": float(parsed.activity_ts or stat.st_mtime),
                # …and the last OUTPUT, which is what the mirror-painted rail
                # decides unread by (#203). Omitted when there is none, so a
                # meta written by an older server keeps falling back to `ts`.
                **({"out": float(parsed.output_ts)} if parsed.output_ts else {}),
                "base": base,       # index the returned events start at
                "total": total,     # events the client should hold afterwards
                "sig": _prefix_sig(events, total),
                "events": events[base:],
            }

        try:
            payload = await asyncio.to_thread(build)
        except Exception as exc:  # noqa: BLE001 — a bad log is a 500, not a dead sync
            return JSONResponse({"error": f"offline read failed: {exc}"}, status_code=500)
        return JSONResponse(payload, headers={"ETag": etag, "Cache-Control": "no-store"})


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
    public_url: str | None = None,
    trigger_rate_capacity: float = TRIGGER_RATE_CAPACITY,
    trigger_rate_refill_s: float = TRIGGER_RATE_REFILL_S,
    max_concurrent_triggered: int = MAX_CONCURRENT_TRIGGERED,
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
    # Base URL for notification deep-links (#163): a push tap must open the
    # session in the real UI, so this is the public origin, not the LAN bind.
    public_url = (public_url or os.environ.get("AISH_PUBLIC_URL", "")).rstrip("/")
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

    def open_session(
        path: Path | None,
        origin: str = "user",
        trigger_meta: dict | None = None,
        model_override: str = "",
    ) -> tuple[Session, list[dict]]:
        """Build one Session: fresh agent wired to its own bridge/log. For an
        existing path the conversation is reloaded into the agent (the file
        keeps growing in place — same semantics as `aish --resume`). `origin`
        tags a NEW session's provenance (#160); for an existing path the
        provenance recorded on disk wins so a cold-reopened triggered session
        keeps its category.

        `model_override` (new sessions only — a resumed session's recorded
        model wins) runs THIS session on a different backend than the
        server's default. It exists for privacy-scoped automation (#186):
        the curation pass aggregates excerpts from every recent session, so
        it must run on a model meeting the strictest bar among them — the
        local one. It FAILS CLOSED: an unbuildable override raises
        BackendError before the session log exists, because silently falling
        back to the (possibly cloud) default is exactly the leak the
        override exists to prevent."""
        history: list[dict] = []
        recorded_spec = ""
        custom_title: str | None = None
        title_auto = False
        override_chat = None
        if model_override and path is None:
            if provider == "claude-max":
                raise backends.BackendError(
                    "model override is unsupported on a claude-max server"
                )
            # Build (and thereby validate) BEFORE SessionLog.new so a bad
            # model can't leave an orphan log file behind.
            override_chat = backends.make_chat(model_override)
        if path is not None:
            # Parse BEFORE anything is appended: the last model record in
            # the file is the model this session must resume with.
            parsed = SessionLog._parse(path)
            history, recorded_spec = parsed.messages, parsed.model
            custom_title, origin, title_auto = parsed.title, parsed.origin, parsed.title_auto
        log = SessionLog(path) if path is not None else SessionLog.new(state_dir)
        logref = LogRef(log)
        bridge = Bridge(get_loop, session=log.path.name)

        agent_holder: list = []
        session_holder: list = []

        def session_title() -> str:
            if session_holder and session_holder[0].custom_title:
                return session_holder[0].custom_title
            return "automated session"

        def notify_hold(event: dict, has_viewers: bool) -> None:
            # Push the owner when an UNATTENDED triggered session holds on an
            # approval it can't auto-run (#163). Scoped tightly: only non-user
            # origins, only when nobody is viewing (an open tab already shows the
            # card), only when Pushover is configured. Runs on the worker thread
            # inside Bridge.ask's try/except — a slow/failed push can't stall the
            # gate (10 s cap, silent on failure).
            if origin == "user" or has_viewers or not notify.configured():
                return
            link = f"{public_url}/?session={log.path.name}" if public_url else None
            notify.pushover(
                f"aish needs approval — {session_title()}",
                _describe_hold(event),
                url=link,
                url_title="Review & approve",
                # Normal priority deliberately: Pushover's high priority (1)
                # is the one level that ignores quiet hours and always sounds,
                # which turns an overnight hold into a 2am alarm. A held worker
                # waits indefinitely, so the notification can wait for morning.
                priority=0,
            )

        bridge.on_wait = notify_hold

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
            bridge, logref, allow_path, deny_path, ask_all, get_scope, trust_dir,
            get_origin=lambda: origin,
            get_session_prefixes=(
                lambda: agent_holder[0].session_prefixes if agent_holder else set()
            ),
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
            # Origin rides on the Agent too (#178 P0-2): a non-user session
            # gates read_url/web_search on conversation provenance and scopes
            # recall to knowledge entries — the agent needs to know it is
            # unattended. claude-max swallows this (its own loop, #P0-4 scope).
            origin=origin,
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
            # Narration (#212): the turn said something on its way to the
            # answer, and this closes that delivery so it gets its own bubble
            # instead of being glued onto whatever is said next. The text
            # rides along for a client whose stream it missed — the same shape
            # `done` uses, and the shape reconstruct_events replays.
            on_delivered=lambda text: bridge.emit({"type": "delivery", "text": text}),
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
        agent_holder.append(agent)

        if path is not None:
            agent.load_history(history)
            # Restore the workspace the session left off in (issue #94), set
            # directly (not via rebase/trust_root) so restoring logs no fresh
            # record — a missing cwd falls back to the launch workspace, missing
            # trusted dirs are skipped.
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
        if override_chat is not None and isinstance(agent, Agent):
            # Per-session backend for a privacy-scoped trigger (#186); the
            # claude-max case was refused before the log existed.
            chat_o, provider_o, name_o = override_chat
            agent.chat, agent.model, agent.provider = chat_o, name_o, provider_o
        # The uploads dir belongs to the SERVER, not to any one chat, so it is
        # added AFTER restore_workspace — which rebuilds roots to be exactly the
        # session's own workspace and would otherwise drop it (#176).
        agent.roots.append(uploads_dir.resolve())
        logref.model(model_spec(agent))  # record what this session actually runs
        if origin != "user" and path is None:
            # Persist provenance ONCE, when the session is created, so a
            # cold-reopened triggered session keeps its "Automated" grouping
            # (#160); "user" is the default and needs none. An existing path
            # already carries the record — _parse read it back a few lines up —
            # and re-writing it on every cold open appended a record (plus the
            # pending model one it flushed) and bumped the file's mtime, which
            # hoists the session to the top of every recency-ordered list.
            # Opening a session must not count as activity in it.
            logref.origin(origin)
        session = Session(agent, logref, bridge, origin=origin, trigger_meta=trigger_meta)
        session.custom_title = custom_title  # a renamed chat keeps its name hot
        session.title_auto = title_auto  # …and a HAND-typed one is never overwritten (#175)
        session_holder.append(session)  # #95: the mid-task get/drain callbacks read it
        return session, history

    # The folder-browser / @-file ignore list is read from the same config.toml
    # the CLI uses (#87); a missing/malformed config degrades to defaults.
    dir_ignore_patterns = dir_ignore.load_patterns(load_config(config_path) if config_path else {})
    server = WebServer(
        open_session, state_dir, config_path, token, dir_ignore_patterns, console_command
    )
    server.public_url = public_url  # notification deep-link base (#163)
    server.base_cwd = cwd  # baseline for the session rows' directory label
    # /trigger guards (#178 P1-10), injectable so tests exercise the limits
    # without hammering the endpoint in real time.
    server.trigger_rate_capacity = trigger_rate_capacity
    server.trigger_rate_refill_s = trigger_rate_refill_s
    server.max_concurrent_triggered = max_concurrent_triggered
    server_ref.append(server)
    first, _ = open_session(None)
    server.add_session(first, default=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await server.startup()
        yield
        await server.shutdown()

    app = Starlette(
        routes=[
            WebSocketRoute("/ws", server.handle_ws),
            Route("/upload", server.handle_upload, methods=["POST"]),
            Route("/share", server.handle_share, methods=["POST"]),
            Route("/trigger", server.handle_trigger, methods=["POST"]),
            Route("/file", server.handle_file, methods=["GET"]),
            Route("/export/answer", server.handle_export_answer, methods=["POST"]),
            Route("/export/session", server.handle_export_session, methods=["GET"]),
            Route("/dirs", server.handle_dirs, methods=["GET"]),
            Route("/offline/index", server.handle_offline_index, methods=["GET"]),
            Route("/offline/session", server.handle_offline_session, methods=["GET"]),
            Route("/fonts/{name}", serve_config_font, methods=["GET"]),
            Route("/", serve_index, methods=["GET"]),
            Route("/index.html", serve_index, methods=["GET"]),
            Mount("/", StaticFiles(directory=STATIC_DIR, html=True)),
        ],
        # GZip outermost: app.js alone is ~420 KB raw and the whole critical
        # path shipped uncompressed — on a weak phone link that was the
        # difference between a boot and an endless spinner. WebSocket scopes
        # pass through both middlewares untouched.
        middleware=[Middleware(GZipMiddleware, minimum_size=512), Middleware(SecurityHeaders)],
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
    # Access always requires the token (#178 P1-2); when none was configured a
    # random one was generated at startup, so the printed URL is the ONLY way
    # in — hence the full URL, token included.
    web_token = app.state.server.token
    print(f"aish-web · model {args.model} · "
          f"http://{args.host}:{args.port}/?token={web_token}")
    if not token:
        print("access token generated for this run — set AISH_WEB_TOKEN for a "
              "stable one", file=sys.stderr)
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
