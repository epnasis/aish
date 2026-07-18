"""Web UI server: the same agent core behind a WebSocket instead of a TTY.

The browser is a thin client. Every callback the CLI wires to print()/input()
is wired here to JSON events over one WebSocket: tokens/echo/status stream
out, and approvals block the agent's worker thread on a queue until the
browser answers — the approval gate is identical to the terminal's, only the
transport differs.

Process model: one process owns one Agent and one SessionLog; one task runs
at a time, and one client is active — a new connection replaces the old one
and receives the buffered transcript, which is what makes phone lock/unlock
mid-task lossless.
"""

import argparse
import asyncio
import contextlib
import os
import queue
import sys
import time
import uuid
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import backends, tools
from .agent import Agent, ModelUnavailable, environment_context
from .approval import (
    DEFAULT_ALLOWLIST,
    DEFAULT_DENYLIST,
    Blocked,
    check_denied,
    is_auto_approvable,
    load_prefixes,
    looks_destructive,
    suggest_prefix,
    unvetted_segments,
)
from .cli import (
    DEFAULT_LESSONS,
    LogRef,
    _backend_hint,
    available_models,
    identity_context,
    load_config,
    load_context_files,
    model_spec,
    rank_models,
    save_default_model,
    skills_context,
)
from .prompt import ATFILE_IGNORED_DIRS, ATFILE_MAX_RESULTS, ATFILE_SCAN_CAP
from .session import SessionLog

STATIC_DIR = Path(__file__).parent / "static"

UPLOAD_MAX_BYTES = 25 * 1024 * 1024

# Replay buffer bounds: enough for a long task's worth of events; beyond it
# the oldest are dropped and the client shows a truncation marker.
TRANSCRIPT_MAX = 600
TRANSCRIPT_KEEP = 500

CLOSE_REPLACED = 4000  # another device connected; this socket is superseded
CLOSE_BAD_TOKEN = 4403


class Bridge:
    """Bridges the agent's worker thread to the event loop.

    Outbound events go through call_soon_threadsafe into an asyncio queue a
    sender coroutine drains; approval requests additionally block the worker
    on a plain queue.Queue slot until the client's answer fills it. The
    transcript buffer is only ever touched on the loop thread (inside _put),
    so replay snapshots need no locking.
    """

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.pending: dict[str, queue.Queue] = {}
        self.transcript: list[dict] = []
        self.truncated = False

    def emit(self, event: dict, record: bool = True) -> None:
        if self.loop is None:  # before startup: nothing listening yet
            self._put(event, record)
            return
        self.loop.call_soon_threadsafe(self._put, event, record)

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
        self.outbox.put_nowait(event)

    def ask(self, event: dict) -> dict:
        """Emit an approval_request (id added in place) and block the calling
        worker thread until answer() delivers the client's decision. No
        timeout by design: an unanswered approval simply waits — the request
        stays in the transcript and reappears on reconnect."""
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

    def clear(self) -> None:
        self.transcript.clear()
        self.truncated = False


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


def make_web_approvers(bridge, logref, allow_path, deny_path, ask_all, get_scope):
    """The three approval callbacks, backed by browser round trips. Mirrors
    cli.make_approver semantics exactly: denylist first (also on edited
    commands), then auto-approval scoped to the live session roots, then a
    blocking approval card. session_prefixes backs the card's "Allow this
    session" button — in-memory only, forgotten when the server stops."""
    session_prefixes: set[str] = set()

    def known_prefixes() -> frozenset:
        return frozenset(load_prefixes(allow_path)) | session_prefixes

    def record(command: str, decision: str) -> None:
        logref.command(command, decision)

    def resolve(uid: str, decision: str) -> None:
        bridge.emit({"type": "approval_resolved", "id": uid, "decision": decision})

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
        request = {
            "type": "approval_request",
            "kind": "command",
            "command": command,
            "destructive": looks_destructive(command),
            "prefixes": suggestions,
        }
        answer = bridge.ask(request)
        action = answer.get("action")
        if action == "approve":
            record(command, "approved")
            resolve(request["id"], "approved")
            return command
        if action == "approve_session":
            session_prefixes.update(suggestions)
            bridge.emit(
                {"type": "echo", "text": f"✓ session-allowed: {', '.join(suggestions)}"}
            )
            record(command, "approved+session")
            resolve(request["id"], "approved")
            return command
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
                record(f"{command} => {edited}", "edited")
                resolve(request["id"], "edited")
                return edited
        record(command, "denied")
        resolve(request["id"], "denied")
        return None

    def approve_write(plan) -> bool:
        verb = "create" if plan.is_new else "edit"
        request = {
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
        record(f"{verb} {plan.target}", "approved" if approved else "denied")
        resolve(request["id"], "approved" if approved else "denied")
        return approved

    def approve_read(path: str, reason: str = "sensitive") -> bool:
        request = {
            "type": "approval_request",
            "kind": "read",
            "path": path,
            "reason": reason,
        }
        answer = bridge.ask(request)
        approved = answer.get("action") == "approve"
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
Approve / Edit / Deny buttons; file writes show a unified diff before \
approval. Read-only commands auto-approve within the session roots \
(allowlist: {allow_path}).
- There are NO slash commands and NO ! direct commands here. Model switching, \
resuming earlier sessions, new chats, changing the working directory, and \
adding session roots all happen through the UI's header controls — if the \
user asks how, point them at the model chip, the session title (sessions \
drawer), and the ⋯ menu (workspace panel).
- The "always allow" allowlist cannot be grown from the web UI — that is \
terminal-only by design. Approving a card runs the command once.
- Safety denylist: unrecoverable command classes are blocked outright and \
cannot be approved here at all (extendable in {deny_path}); suggest a safer \
alternative when blocked.
- Sessions: conversation + command audit trail logged to {state_dir} — the \
same format as terminal aish, so sessions are interchangeable between both.
- File tools: prefer read_file/write_file/edit_file over cat/sed/heredocs; \
the user approves a diff card before any write. Do NOT use sed -i or > \
redirects to edit files.
- Attachments: the web UI can upload files; they arrive as "[attached file: \
<path>]" lines in the user's message. Read them with read_file (text) or \
process them with shell tools. You cannot SEE image contents — for images, \
work with metadata/shell tools (sips, exiftool) and say so if asked to \
describe one."""


class WebServer:
    """Per-process server state: one agent, one session log, one active client."""

    def __init__(self, agent, logref, bridge, state_dir, config_path, token):
        self.agent = agent
        self.logref = logref
        self.bridge = bridge
        self.state_dir = state_dir
        self.uploads_dir = state_dir / "uploads"
        self.config_path = config_path
        self.token = token
        self.busy = False
        self.ws: WebSocket | None = None
        self.sender: asyncio.Task | None = None
        self.runner: asyncio.Task | None = None

    async def startup(self) -> None:
        self.bridge.loop = asyncio.get_running_loop()

    def _hello(self) -> dict:
        return {
            "type": "hello",
            "model": model_spec(self.agent),
            "session": self.logref.log.path.name,
            "busy": self.busy,
            "cwd": self.agent.cwd,
            "roots": [str(root) for root in self.agent.roots],
        }

    def _cwd_event(self) -> dict:
        return {
            "type": "cwd_changed",
            "cwd": self.agent.cwd,
            "roots": [str(root) for root in self.agent.roots],
        }

    async def handle_ws(self, websocket: WebSocket) -> None:
        if self.token and websocket.query_params.get("token") != self.token:
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
        if self.sender:
            self.sender.cancel()
            self.sender = None
        old = self.ws
        self.ws = websocket
        if old is not None:
            try:
                await old.close(code=CLOSE_REPLACED)
            except Exception:  # noqa: BLE001 — the old socket may already be dead
                pass
        # Same synchronous block: no _put callback can land between the queue
        # swap and the snapshot, so replay + live stream never duplicate.
        self.bridge.reset_outbox()
        snapshot = list(self.bridge.transcript)
        await websocket.send_json(self._hello())
        await websocket.send_json(
            {"type": "replay", "events": snapshot, "truncated": self.bridge.truncated}
        )
        self.sender = asyncio.ensure_future(self._send_loop(websocket))

    async def _send_loop(self, websocket: WebSocket) -> None:
        try:
            while True:
                event = await self.bridge.outbox.get()
                await websocket.send_json(event)
        except Exception:  # noqa: BLE001 — a dead socket ends the loop; replay recovers
            pass

    async def _handle(self, websocket: WebSocket, message: dict) -> None:
        kind = message.get("type")
        if kind == "task":
            await self._start_task(websocket, str(message.get("text", "")).strip())
        elif kind == "approval":
            self.bridge.answer(str(message.get("id", "")), message)
        elif kind == "sessions":
            await self._send_sessions(websocket, str(message.get("query", "")))
        elif kind == "resume":
            await self._resume(websocket, str(message.get("path", "")))
        elif kind == "new":
            await self._new_session(websocket)
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
        else:
            await websocket.send_json(
                {"type": "error", "text": f"unknown message type {kind!r}"}
            )

    async def _reject_busy(self, websocket: WebSocket) -> bool:
        if self.busy:
            await websocket.send_json(
                {"type": "error", "text": "busy — wait for the current task to finish"}
            )
            return True
        return False

    async def _start_task(self, websocket: WebSocket, text: str) -> None:
        if not text or await self._reject_busy(websocket):
            return
        self.busy = True
        self.bridge.emit({"type": "user", "text": text})
        self.runner = asyncio.ensure_future(self._run_task(text))

    async def _run_task(self, text: str) -> None:
        try:
            result = await asyncio.to_thread(self.agent.run_task, text)
            self.bridge.emit({"type": "done", "result": result})
        except ModelUnavailable as exc:
            self.bridge.emit(
                {"type": "error", "text": f"model unavailable: {exc}{_backend_hint(self.agent)}"}
            )
        except Exception as exc:  # noqa: BLE001 — a task bug must not kill the server
            self.bridge.emit({"type": "error", "text": f"task failed: {exc!r}"})
        finally:
            self.busy = False

    async def _send_sessions(self, websocket: WebSocket, query: str) -> None:
        state_dir, exclude = self.state_dir, {self.logref.log.path}

        def load():
            entries = SessionLog.load_entries(state_dir, exclude=exclude)
            return SessionLog.rank(entries, query)

        infos = await asyncio.to_thread(load)
        await websocket.send_json(
            {
                "type": "session_list",
                "sessions": [
                    {
                        "name": info.path.name,
                        "when": info.when,
                        "count": info.count,
                        "model": info.model,
                        "title": info.title,
                    }
                    for info in infos
                ],
            }
        )

    async def _resume(self, websocket: WebSocket, name: str) -> None:
        if await self._reject_busy(websocket):
            return
        safe = name.startswith("session-") and name.endswith(".jsonl") and "/" not in name
        path = self.state_dir / name
        if not safe or ".." in name or not path.is_file():
            await websocket.send_json({"type": "error", "text": f"no such session: {name}"})
            return
        messages = await asyncio.to_thread(SessionLog.load_messages, path)
        # Same semantics as the terminal's /resume: merge into the current
        # conversation, re-logging so the current session file stays
        # self-contained.
        self.agent.load_history(messages)
        for message in messages:
            self.logref.message(message)
        self.bridge.emit(
            {"type": "echo", "text": f"resumed {len(messages)} messages from {name}"}
        )
        self.bridge.emit({"type": "history", "messages": messages})

    async def _new_session(self, websocket: WebSocket) -> None:
        if await self._reject_busy(websocket):
            return
        self.agent.reset()
        self.logref.log = SessionLog.new(self.state_dir)
        self.logref.model(model_spec(self.agent))
        self.bridge.clear()
        await websocket.send_json(self._hello())
        # An empty replay is the client's "clear the screen" signal.
        await websocket.send_json({"type": "replay", "events": [], "truncated": False})

    async def _send_files(self, websocket: WebSocket, query: str) -> None:
        cwd = self.agent.cwd
        paths = await asyncio.to_thread(list_files, cwd, query)
        await websocket.send_json({"type": "file_list", "query": query, "files": paths})

    async def _send_models(self, websocket: WebSocket, query: str) -> None:
        agent, state_dir = self.agent, self.state_dir

        def load():
            return rank_models(available_models(agent, state_dir), query)

        ranked = await asyncio.to_thread(load)
        await websocket.send_json(
            {
                "type": "model_list",
                "current": model_spec(self.agent),
                "models": [{"name": name, "desc": desc} for name, desc in ranked],
            }
        )

    async def _set_model(self, websocket: WebSocket, message: dict) -> None:
        if await self._reject_busy(websocket):
            return
        spec = str(message.get("spec", "")).strip()
        if not spec:
            return
        crossing_max = spec.startswith("claude-max") or (
            getattr(self.agent, "provider", "ollama") == "claude-max"
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
        self.agent.chat = chat
        self.agent.model = name
        self.agent.provider = provider
        self.logref.model(model_spec(self.agent))
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
        self.bridge.emit({"type": "echo", "text": f"model switched to {spec}"})
        await websocket.send_json(
            {"type": "model_changed", "model": model_spec(self.agent), "saved": saved}
        )

    async def _cd(self, websocket: WebSocket, path: str) -> None:
        if await self._reject_busy(websocket) or not path:
            return
        result = await asyncio.to_thread(self.agent.rebase, path)
        if result.startswith("ERROR"):
            await websocket.send_json({"type": "error", "text": result})
            return
        await websocket.send_json(self._cwd_event())

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

    async def _add_dir(self, websocket: WebSocket, path: str) -> None:
        if await self._reject_busy(websocket) or not path:
            return
        result = await asyncio.to_thread(self.agent.add_root, path)
        if result.startswith("ERROR"):
            await websocket.send_json({"type": "error", "text": result})
            return
        self.bridge.emit({"type": "echo", "text": result})
        await websocket.send_json(self._cwd_event())


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
    cwd = cwd or os.getcwd()
    state_dir = Path(
        state_dir
        or os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish"))
    )
    allow_path = Path(allow_path or os.environ.get("AISH_ALLOWLIST", str(DEFAULT_ALLOWLIST)))
    deny_path = Path(deny_path or os.environ.get("AISH_DENYLIST", str(DEFAULT_DENYLIST)))
    lessons_path = Path(lessons_path or os.environ.get("AISH_LESSONS", str(DEFAULT_LESSONS)))

    if client_chat is not None:
        chat, provider, model_name = client_chat, "ollama", model
    elif model == "claude-max" or model.startswith("claude-max:"):
        chat, provider, model_name = None, "claude-max", model.partition(":")[2]
    else:
        chat, provider, model_name = backends.make_chat(model)

    log = SessionLog.new(state_dir)
    log.model(model)
    logref = LogRef(log)
    bridge = Bridge()

    # The approvers need the agent's live cwd/roots, but the agent is built
    # with the approvers — the scope binds late through this holder.
    agent_holder: list = []

    def get_scope():
        if agent_holder:
            return agent_holder[0].cwd, agent_holder[0].roots
        return cwd, [Path(cwd).resolve()]

    approve, approve_write, approve_read = make_web_approvers(
        bridge, logref, allow_path, deny_path, ask_all, get_scope
    )

    context = "\n\n".join(
        part
        for part in [
            environment_context(cwd),
            web_usage_context(model_name, provider, allow_path, deny_path, state_dir),
            skills_context(cwd),
            *load_context_files(cwd, lessons_path),
        ]
        if part
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
        job_log_dir=state_dir / "jobs",
        lessons_path=lessons_path,
        status=WebStatus(bridge),
    )
    if provider == "claude-max":
        from .claude_max import ClaudeMaxAgent

        agent = ClaudeMaxAgent(**common)
    else:
        agent = Agent(client_chat=chat, num_ctx=num_ctx, think=think, **common)
        agent.provider = provider
    agent_holder.append(agent)

    server = WebServer(agent, logref, bridge, state_dir, config_path, token)
    # Uploaded files must be readable without an approval round trip.
    server.uploads_dir.mkdir(parents=True, exist_ok=True)
    agent.roots.append(server.uploads_dir.resolve())

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await server.startup()
        yield

    app = Starlette(
        routes=[
            WebSocketRoute("/ws", server.handle_ws),
            Route("/upload", server.handle_upload, methods=["POST"]),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
