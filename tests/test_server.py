"""Web server tests: the same FakeChat pattern as test_agent.py, driven over
a real WebSocket via Starlette's TestClient (which runs the app's event loop
in a thread, so the worker-thread bridge is exercised for real). No model,
no network; the only real commands executed are harmless touch/ls in tmp dirs.
"""

import asyncio
import base64
import contextlib
import datetime
import inspect
import json
import logging
import os
import pathlib
import re
import shlex
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected

import aish.browse as browse_module
import aish.browser as browser_module
import aish.notify as notify_module
import aish.server as server_module
import aish.session as session_module
from aish.agent import DENIED_RESULT, WRITE_DENIED
from aish.server import (
    SESSION_TITLE_PROMPT,
    TITLE_PROMPT,
    create_app,
    safe_session_path,
)
from aish.session import SessionLog, synthetic_kind


def tool_call(name: str, **arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def model_says(content: str = "", tool_calls: list | None = None):
    return SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls or None)
    )


# Titling calls (#172 answer export, #175 chat auto-title) are one-shot side
# calls — a bare prompt, no tools, outside the conversation. FakeChat answers
# them WITHOUT consuming the script, so a test's response list stays a script of
# its task turns and adding a titler didn't mean rewriting every test.
_TITLE_PROMPTS = (
    SESSION_TITLE_PROMPT.split("\n")[0],
    TITLE_PROMPT.split("\n")[0],
)


def _is_title_call(kwargs: dict) -> bool:
    messages = kwargs.get("messages") or []
    if len(messages) != 1:
        return False
    return str(messages[0].get("content", "")).startswith(_TITLE_PROMPTS)


class FakeChat:
    """Scripted backend. The web server always streams (on_token is wired),
    so stream=True returns the response as a one-chunk iterator — the same
    shape ollama's streaming yields.

    `title` is the canned answer to a titling call. It defaults to None — the
    backend declining to name things — so every test that doesn't care about
    titles behaves exactly as it did before there was a titler."""

    def __init__(self, responses: list, title: str | None = None):
        self.responses = list(responses)
        self.calls: list[dict] = []  # conversation turns only
        self.title_calls: list[dict] = []
        self.title = title

    def __call__(self, **kwargs):
        if _is_title_call(kwargs):
            self.title_calls.append(kwargs)
            return model_says(self.title or "")
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if kwargs.get("stream"):
            return iter([response])
        return response


@pytest.fixture
def app_env(tmp_path):
    """Isolated state/allow/deny files so tests never touch real config."""
    allow = tmp_path / "allow.txt"
    deny = tmp_path / "deny.txt"
    allow.write_text("ls\n", encoding="utf-8")
    deny.write_text("rm -rf\n", encoding="utf-8")
    return {
        "state_dir": tmp_path / "state",
        "allow_path": allow,
        "deny_path": deny,
        "config_path": tmp_path / "config.toml",
        "lessons_path": tmp_path / "lessons.md",
        "cwd": str(tmp_path),
    }


# The access token is unconditional now (#178 P1-2): an app built without one
# generates its own at startup. Tests get a fixed one so URLs stay printable.
TEST_TOKEN = "test-token"


class TokenClient(TestClient):
    """TestClient that appends the app's token to any request not already
    carrying one — the fixture-side answer to the unconditional token (#178
    P1-2), so the hundreds of pre-existing bare `/ws`, `/upload?…` calls keep
    working WITHOUT weakening the server-side requirement. Tests that pass an
    explicit token= to make_client get `auto_token=None` (plain TestClient
    behaviour), so their deliberate token omissions still hit the gate."""

    def __init__(self, app, auto_token=None, **kwargs):
        super().__init__(app, **kwargs)
        self.auto_token = auto_token

    def _tokened(self, url):
        url = str(url)
        if not self.auto_token or "token=" in url:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}token={self.auto_token}"

    def request(self, method, url, **kwargs):
        params = kwargs.get("params")
        if params is not None:
            # httpx REPLACES the URL query with `params`, so the token must
            # ride inside params here, not on the URL.
            if self.auto_token and "token" not in params:
                kwargs["params"] = {**dict(params), "token": self.auto_token}
            return super().request(method, url, **kwargs)
        return super().request(method, self._tokened(url), **kwargs)

    def websocket_connect(self, url, *args, **kwargs):
        return super().websocket_connect(self._tokened(url), *args, **kwargs)


def make_client(app_env, responses, title=None, **kwargs):
    chat = FakeChat(responses, title=title)
    explicit_token = "token" in kwargs
    kwargs.setdefault("token", TEST_TOKEN)
    app = create_app("fake", client_chat=chat, **app_env, **kwargs)
    auto = None if explicit_token else kwargs["token"]
    return TokenClient(app, auto_token=auto), chat


@contextlib.contextmanager
def connected(client, path="/ws"):
    """(ws, hello, replay) with the socket ALWAYS closed on exit — a failing
    assertion mid-test must not leave the session open, or TestClient's
    shutdown would wait on it forever."""
    with client.websocket_connect(path) as ws:
        hello = ws.receive_json()
        replay = ws.receive_json()
        assert hello["type"] == "hello"
        assert replay["type"] == "replay"
        yield ws, hello, replay


def recv_until(ws, wanted: str, limit: int = 200) -> dict:
    """Drain events until one of type `wanted` arrives (tokens etc. skipped).
    An unexpected error event fails fast instead of hanging the receive."""
    for _ in range(limit):
        event = ws.receive_json()
        if event["type"] == wanted:
            return event
        if event["type"] == "error":
            raise AssertionError(f"error while waiting for {wanted!r}: {event['text']}")
    raise AssertionError(f"no {wanted!r} event within {limit} events")


def recv_until_row(ws, name: str, state: str, limit: int = 200) -> dict:
    """Drain until the roster publishes `name` in `state` (#204). The plane is
    chatty by design — every transition of every session travels — so a test
    waits for the row it means rather than the next event of a type."""
    for _ in range(limit):
        event = ws.receive_json()
        if event["type"] == "session_changed" and event["row"]["name"] == name:
            if event["row"]["state"] == state:
                return event
        if event["type"] == "error":
            raise AssertionError(f"error while waiting for {name} {state}: {event['text']}")
    raise AssertionError(f"no session_changed for {name} in {state!r} within {limit} events")


def recv_until_refusal(ws, limit: int = 200) -> dict:
    """The next `error`, without recv_until's fail-fast on one — a refusal IS
    an error event, and here it is what the test is waiting for."""
    for _ in range(limit):
        event = ws.receive_json()
        if event["type"] == "error":
            return event
    raise AssertionError("no error event")


def drain_done(client, name, token=TEST_TOKEN):
    """Wait for a background session's task to finish: attach and return once
    `done` is seen — LIVE or already in the replay. A triggered task races the
    attach, so `connected(...)` + recv_until(ws, "done") hangs whenever the
    task finished first (its done is then replayed, never re-sent)."""
    with client.websocket_connect(f"/ws?token={token}&session={name}") as ws:
        hello = ws.receive_json()
        replay = ws.receive_json()
        assert hello["type"] == "hello" and replay["type"] == "replay"
        if any(e.get("type") == "done" for e in replay["events"]):
            return
        recv_until(ws, "done")


def recv_step(ws, kind: str, limit: int = 200) -> dict:
    """Drain events until a trace `step` of the given kind arrives (#95: the
    mid-task injected-message note is emitted as a step)."""
    for _ in range(limit):
        event = ws.receive_json()
        if event["type"] == "step" and event.get("kind") == kind:
            return event
        if event["type"] == "error":
            raise AssertionError(f"error while waiting for step {kind!r}: {event['text']}")
    raise AssertionError(f"no step {kind!r} within {limit} events")


def tool_results(chat, call_index=1):
    return [m for m in chat.calls[call_index]["messages"] if m["role"] == "tool"]


def find_tool_step(events, name):
    """The finished `tool` trace step for a given tool, from a live transcript
    or a cold reconstruction (both spread the step dict onto a `step` event)."""
    return next(
        e for e in events
        if e.get("type") == "step" and e.get("kind") == "tool" and e.get("name") == name
    )


# Event types that are live-only chrome or control-flow, reconstructed
# differently (or not at all) by design — excluded from the hot/cold guard.
# Everything NOT listed is a DURABLE trace event the cold path must reproduce,
# so a new event type added to the live stream but not persisted/reconstructed
# (or vice versa) fails the guard until it is handled on both paths.
_EPHEMERAL_EVENTS = {
    "token", "echo", "status", "error", "hello", "replay", "history",
    "queued", "dequeued", "cwd_queued", "cwd_dequeued", "approval_request",
    "approval_resolved", "cwd_changed",
    "model_changed", "session_state", "file_list", "job_list",
    "model_list", "session_list", "session_deleted", "session_renamed",
    "browser_view",
}


def trace_shape(events):
    """The durable-trace projection of an event stream: (type, discriminator)
    per event, with consecutive stream chunks coalesced (the live path streams
    N output lines; the cold path replays one) and ephemeral chrome dropped.
    Hot (bridge.transcript) and cold (reconstruct_events) must project equal."""
    shape = []
    for event in events:
        kind = event["type"]
        if kind in _EPHEMERAL_EVENTS:
            continue
        if kind == "stream":
            if not (shape and shape[-1] == ("stream",)):
                shape.append(("stream",))
        elif kind == "step":
            shape.append(("step", event.get("kind"), event.get("name")))
        elif kind == "command_end":
            shape.append(("command_end", event.get("status")))
        elif kind == "workspace":
            shape.append(("workspace", event.get("change"), event.get("path")))
        else:
            shape.append((kind,))
    return shape


class TestStreamCoalescer:
    """Issue #109: the live per-line output of a huge command is batched into
    fewer, larger `stream` events before it reaches the browser. This is a
    live-only transport optimization — the logged tool output and cold replay
    are untouched (see test_large_bang_output_batches_stream_events for the
    end-to-end batching, and the hot/cold parity guard elsewhere)."""

    def _coalescer(self, sink):
        from aish.server import StreamCoalescer

        c = StreamCoalescer(sink)
        c.MAX_DELAY = 3600  # keep the delay-flush thread from firing mid-test
        return c

    def test_batches_lines_and_flushes_remainder(self):
        emitted = []
        c = self._coalescer(emitted.append)
        for i in range(120):
            c.line(f"line-{i}")
        # 120 lines, MAX_LINES=50 → two full 50-line batches emitted; the last
        # 20 stay buffered until the explicit command-end flush.
        assert len(emitted) == 2
        assert emitted[0] == "\n".join(f"line-{i}" for i in range(50))
        assert emitted[1] == "\n".join(f"line-{i}" for i in range(50, 100))
        c.flush()
        assert len(emitted) == 3
        assert emitted[2] == "\n".join(f"line-{i}" for i in range(100, 120))
        # No output is lost: the batches rejoin to the original line stream.
        assert "\n".join(emitted).split("\n") == [f"line-{i}" for i in range(120)]

    def test_flush_of_empty_buffer_emits_nothing(self):
        emitted = []
        c = self._coalescer(emitted.append)
        c.flush()
        assert emitted == []

    def test_byte_cap_flushes_a_single_large_line(self):
        emitted = []
        c = self._coalescer(emitted.append)
        big = "x" * (17 * 1024)  # over MAX_BYTES on its own
        c.line(big)
        assert emitted == [big]


class TestReceipts:
    """`rid` → `ack` (#210). The client cannot tell a request that was handled
    from one that was never heard — a WebSocket reports OPEN long after it died
    — so anything it must not guess about carries a receipt id and gets it
    echoed back. A delivery fact, never a claim of success."""

    def test_any_request_carrying_a_rid_is_receipted(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "jobs", "rid": "r7"})
            ack = recv_until(ws, "ack")
            assert ack["rid"] == "r7"

    def test_a_request_without_a_rid_is_not_receipted(self, app_env):
        """Reads opt out — receipting a per-keystroke query buys nothing."""
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "jobs"})
            ws.send_json({"type": "jobs", "rid": "r1"})
            # The FIRST ack seen is r1's: nothing was minted for the bare one.
            assert recv_until(ws, "ack")["rid"] == "r1"

    def test_the_receipt_follows_the_work_it_is_for(self, app_env):
        """Stamped after the handler, so a client that hears the receipt knows
        the events it was owed have already been sent."""
        client, _ = make_client(app_env, [model_says("noted")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "remember the zebra"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            recv_until(ws, "hello")

            ws.send_json({"type": "delete_session", "name": name, "rid": "r9"})
            seen = []
            for _ in range(80):
                event = ws.receive_json()
                seen.append(event["type"])
                if event["type"] == "ack":
                    break
            assert seen[-1] == "ack"
            assert "session_deleted" in seen, "the receipt outran the work it receipts"
            assert not (app_env["state_dir"] / name).exists()

    def test_a_refused_request_is_still_receipted(self, app_env, tmp_path):
        """A refusal IS an answer — the client must stop waiting on it. Only a
        request that never arrived (or blew up) goes unreceipted."""
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command="touch never")]),
                model_says("gave up"),
            ],
        )
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "touch it"})
            request = recv_until(ws, "approval_request")

            ws.send_json({"type": "delete_session", "name": name, "rid": "r3"})
            refusal = recv_until_refusal(ws)
            assert refusal["code"] == "refused"
            assert recv_until(ws, "ack")["rid"] == "r3"

            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")


class TestConnect:
    def test_hello_carries_model_session_scope(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, replay):
            assert hello["model"] == "fake"
            assert hello["session"].startswith("session-")
            assert hello["busy"] is False
            assert hello["cwd"] == app_env["cwd"]
            assert hello["rev"]  # static-files fingerprint for staleness checks
            assert hello["log_path"].endswith(hello["session"])  # #146: /session + copy
            assert replay["events"] == []

    def test_index_stamps_asset_revision(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            response = client.get("/")
            assert response.status_code == 200
            assert 'src="app.js?v=' in response.text
            assert 'href="style.css?v=' in response.text
            assert response.headers["cache-control"] == "no-cache"

    def test_hello_title_is_first_user_message(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        with client:
            with connected(client) as (ws, hello, _):
                assert hello["title"] == ""  # fresh session: client shows "New chat"
                ws.send_json({"type": "task", "text": "rename all the photos"})
                recv_until(ws, "done")
            with connected(client) as (_ws, hello, _):
                assert hello["title"] == "rename all the photos"

    def test_task_streams_and_finishes(self, app_env):
        client, chat = make_client(app_env, [model_says("hi there")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "say hi"})
            assert recv_until(ws, "user")["text"] == "say hi"
            done = recv_until(ws, "done")
            assert done["result"] == "hi there"
            assert "sources" not in done  # no web use, no sources field
            sent_user = [m for m in chat.calls[0]["messages"] if m["role"] == "user"]
            assert sent_user[-1]["content"] == "say hi"

    def test_done_carries_sources_after_read_url(self, app_env, monkeypatch):
        from types import SimpleNamespace

        import aish.agent as agent_module

        monkeypatch.setattr(
            agent_module.web, "read_url", lambda url, topic=None, **_kw: f"[{url}] text"
        )
        read_call = SimpleNamespace(
            function=SimpleNamespace(name="read_url", arguments={"url": "https://x.example/"})
        )
        client, _ = make_client(
            app_env,
            [model_says(tool_calls=[read_call]), model_says("answer")],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "research"})
            done = recv_until(ws, "done")
            assert done["sources"] == [{"url": "https://x.example/"}]


class TestSessionStamp:
    """#182: every event a bridge fans out (or records for replay) names its
    session, so the client can drop live deliveries for a chat that is no
    longer on screen — the client-side switch window of a prefetched swipe or
    an offline-mirror tap, where the old session's events kept arriving until
    the server processed the resume."""

    class _Viewer:
        def __init__(self):
            self.items: list = []
            self.outbox = SimpleNamespace(put_nowait=self.items.append)

    def test_bridge_stamps_fanout_but_not_the_transcript(self):
        bridge = server_module.Bridge(lambda: None, session="session-x.jsonl")
        viewer = self._Viewer()
        bridge.viewers.add(viewer)
        bridge.emit({"type": "token", "text": "hi"})
        assert viewer.items[0]["session"] == "session-x.jsonl"
        # The stamp rides only the live delivery: the recorded transcript stays
        # byte-identical to reconstruct_events (hot/cold parity), and replayed
        # events bypass the client's firewall anyway.
        assert "session" not in bridge.transcript[0]

    def test_stamp_never_overwrites_an_events_own_session(self):
        # An event that names its session itself (session_state) keeps it.
        bridge = server_module.Bridge(lambda: None, session="session-x.jsonl")
        viewer = self._Viewer()
        bridge.viewers.add(viewer)
        bridge.emit({"type": "session_state", "session": "session-other.jsonl"})
        assert viewer.items[0]["session"] == "session-other.jsonl"

    def test_unnamed_bridge_stamps_nothing(self):
        # Back-compat for bridges built without a session (tests): events pass
        # through untouched, which the client reads as "not scoped".
        bridge = server_module.Bridge(lambda: None)
        viewer = self._Viewer()
        bridge.viewers.add(viewer)
        bridge.emit({"type": "token", "text": "hi"})
        assert "session" not in viewer.items[0]

    def test_live_task_events_carry_the_sessions_name(self, app_env):
        client, _ = make_client(app_env, [model_says("hi there")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "say hi"})
            for _ in range(200):
                event = ws.receive_json()
                # The roster plane is cross-session by design and unstamped —
                # it is about the LIST, not about this chat's transcript (#204).
                if event["type"] in ("session_changed", "session_deleted"):
                    assert "session" not in event, event
                    continue
                assert event["session"] == hello["session"], event
                if event["type"] == "done":
                    break
            else:
                raise AssertionError("no done event")


class TestTurnOrigin:
    """Every live `user` event carries when its turn began (epoch seconds).

    The live trace card is built by the turn's FIRST step and rebuilt by every
    replay, so a browser landing mid-turn — a reconnect, or a swipe to another
    chat and back — has to be told the origin. Without it the client can only
    re-derive one by summing the steps it replays, which measures the WORK and
    misses everything between it (the step still in flight, an approval waiting,
    the answer streaming), so the clock came back short every time."""

    def _user_events(self, session):
        return [e for e in session.bridge.transcript if e["type"] == "user"]

    def test_a_typed_turn_carries_its_start(self, app_env):
        client, _ = make_client(app_env, [model_says("hi")], token="secret")
        with client, connected(client, "/ws?token=secret") as (ws, hello, _):
            before = int(time.time())
            ws.send_json({"type": "task", "text": "say hi"})
            recv_until(ws, "done")
            session = client.app.state.server.sessions[hello["session"]]
            (event,) = self._user_events(session)
            assert before <= event["ts"] <= int(time.time())

    def test_a_bang_command_turn_carries_its_start(self, app_env):
        client, _ = make_client(app_env, [], token="secret")
        with client, connected(client, "/ws?token=secret") as (ws, hello, _):
            before = int(time.time())
            ws.send_json({"type": "task", "text": "!echo hi"})
            recv_until(ws, "done")
            session = client.app.state.server.sessions[hello["session"]]
            (event,) = self._user_events(session)
            assert before <= event["ts"] <= int(time.time())

    def test_a_triggered_turn_carries_its_start(self, app_env):
        # The unattended case is where a mid-turn landing is the NORM: the owner
        # opens the automated chat while it is still working.
        client, _ = make_client(app_env, [model_says("triaged")], token="secret")
        with client:
            before = int(time.time())
            r = client.post("/trigger?token=secret",
                            json={"prompt": "new mail", "origin": "email"})
            name = r.json()["session"]
            session = client.app.state.server.sessions[name]
            for _ in range(100):
                if self._user_events(session):
                    break
                time.sleep(0.02)
            (event,) = self._user_events(session)
            assert event["synthetic"] == "trigger"
            assert before <= event["ts"] <= int(time.time())

    def test_a_cold_replay_needs_no_origin(self, app_env):
        # Deliberately live-only: reconstruct_events closes every turn it replays
        # (a cut-off one becomes an `error`), so a cold transcript can never land
        # a reader inside a running turn — and stamping it would rewrite every
        # mirrored session's offline prefix for nothing.
        client, _ = make_client(app_env, [model_says("hi")], token="secret")
        with client, connected(client, "/ws?token=secret") as (ws, hello, _):
            ws.send_json({"type": "task", "text": "say hi"})
            recv_until(ws, "done")
            path = client.app.state.server.state_dir / hello["session"]
        cold = SessionLog.reconstruct_events(path)
        assert [e["text"] for e in cold if e["type"] == "user"] == ["say hi"]
        assert all("ts" not in e for e in cold if e["type"] == "user")
        assert cold[-1]["type"] == "done"


class TestSecurityHeaders:
    """#178 P0-2: CSP + Referrer-Policy stamped on EVERY http response class —
    index, static assets, the service worker, JSON endpoints, and errors —
    with img-src limited to self/data:/the whitelisted embed hosts (the
    zero-click ![](https://attacker/…) exfiltration channel)."""

    PATHS = ("/", "/app.js", "/sw.js", "/manifest.json", "/offline/index",
             "/style.css", "/no-such-path-xyz")

    def test_headers_on_every_response_class(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            for path in self.PATHS:
                response = client.get(path)
                csp = response.headers.get("content-security-policy")
                assert csp, f"no CSP on {path}"
                assert "default-src 'self'" in csp
                assert "script-src 'self'" in csp
                assert (
                    "img-src 'self' data: https://img.youtube.com "
                    "https://i.ytimg.com https://maps.googleapis.com" in csp
                )
                assert "frame-ancestors 'none'" in csp
                assert "object-src 'none'" in csp
                assert response.headers.get("referrer-policy") == "no-referrer"

    def test_connect_src_names_websocket_forms_of_own_host(self, app_env):
        # 'self' plus the explicit ws/wss forms of the request's Host header,
        # so the socket keeps working behind the wss reverse proxy.
        client, _ = make_client(app_env, [])
        with client:
            csp = client.get("/").headers["content-security-policy"]
            assert "connect-src 'self' ws://testserver wss://testserver" in csp

    def test_hostile_host_header_never_reaches_the_policy(self):
        # Header-injection guard: a Host that could smuggle extra CSP sources
        # (spaces/semicolons) degrades to bare 'self', not into the policy.
        csp = server_module.content_security_policy("evil.com; img-src *")
        assert "evil.com" not in csp and "img-src *" not in csp
        assert "connect-src 'self';" in csp

    def test_index_has_no_inline_scripts(self):
        # script-src 'self' only holds because index.html loads all JS from
        # files — an inline <script> added later would silently break the app.
        html = (server_module.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag, f"inline script would violate CSP: {tag}"


class TestQuickReplyPromptGuidance:
    """Issue #78: the system prompt must forbid terminating quick-reply chips
    ("Thanks, that's all") — the user can end the chat anytime, so chips must
    only offer useful next steps."""

    def test_forbids_terminating_chips(self):
        context = server_module.web_usage_context(
            "model", "ollama", "/allow", "/deny", "/state"
        )
        assert "NEVER generate a chip whose only purpose is to end the conversation" in context
        assert "Thanks, that's all" in context
        assert "useful next step" in context


class TestQuickReplyNet:
    """Issue #46: a web final answer that ends in a question with no chip gets
    a deterministic fallback set; [no-chips] opts out and is stripped."""

    def test_suffix_appends_for_bare_question(self):
        result, suffix = server_module.apply_quick_reply_net("Ready to deploy?")
        assert suffix == "\n\n" + "\n".join(server_module.FALLBACK_CHIPS)
        assert result.endswith("\n".join(server_module.FALLBACK_CHIPS))
        assert "aish-reply://yes" in result

    def test_existing_chip_left_untouched(self):
        answer = "Deploy?\n[Go](aish-reply://go)"
        assert server_module.apply_quick_reply_net(answer) == (answer, None)

    def test_non_question_left_untouched(self):
        answer = "All done — the build passed."
        assert server_module.apply_quick_reply_net(answer) == (answer, None)

    def test_no_chips_tag_strips_and_suppresses(self):
        result, suffix = server_module.apply_quick_reply_net(
            "What should we build next? [no-chips]"
        )
        assert suffix is None
        assert "no-chips" not in result
        assert "aish-reply://" not in result
        assert result == "What should we build next?"

    def test_task_appends_fallback_chips_over_socket(self, app_env):
        client, _ = make_client(app_env, [model_says("Shall I proceed?")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "go"})
            events = []
            for _ in range(200):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "done":
                    break
            done = next(e for e in events if e["type"] == "done")
            assert "aish-reply://tell me more" in done["result"]
            # the suffix also streams as a token so an already-streamed answer
            # gains the chips live, not only in done.result.
            token_text = "".join(e["text"] for e in events if e["type"] == "token")
            assert "aish-reply://yes" in token_text

    def test_task_strips_no_chips_tag_over_socket(self, app_env):
        client, _ = make_client(
            app_env, [model_says("How would you like to approach this? [no-chips]")]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "advise"})
            done = recv_until(ws, "done")
            assert "no-chips" not in done["result"]
            assert "aish-reply://" not in done["result"]

    def test_task_keeps_model_supplied_chips(self, app_env):
        answer = "Proceed?\n[Yes, go](aish-reply://yes)\n[Hold](aish-reply://hold)"
        client, _ = make_client(app_env, [model_says(answer)])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "go"})
            done = recv_until(ws, "done")
            assert done["result"] == answer
            assert "tell me more" not in done["result"].lower()


class TestApprovalIntentEndToEnd:
    """The wiring, through the REAL app: agent -> approver -> card -> log.

    Unit-testing the approvers against a fake log missed that the web server
    reaches the session log through `cli.LogRef`, not `SessionLog` directly —
    so widening only the inner signature turned every gated command into
    `TypeError: LogRef.command() takes 3 positional arguments but 4 were
    given`, surfaced to the model as a failed tool and to the owner as nothing
    at all. Both halves were green. Driving the real page is what found it, and
    this is that check as a test."""

    def _intent_records(self, app_env):
        records = []
        for log in sorted((app_env["state_dir"]).glob("session-*.jsonl")):
            for line in log.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("kind") == "command":
                    records.append(record)
        return records

    def test_the_reason_rides_the_card_and_lands_in_the_log(self, app_env, tmp_path):
        marker = tmp_path / "checked"
        client, _chat = make_client(app_env, [
            model_says(INCIDENT_INTENT,
                       tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
            model_says("finished"),
        ])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "why the difference?"})
            request = recv_until(ws, "approval_request")
            assert request["intent"] == INCIDENT_INTENT
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")
            assert marker.exists(), "the approved command never ran"
        [record] = self._intent_records(app_env)
        assert record["decision"] == "approved"
        assert record["intent"] == INCIDENT_INTENT

    def test_a_silent_step_records_no_reason_at_all(self, app_env, tmp_path):
        """Absent, not empty: the key's absence is what keeps an older log
        replaying byte-identically."""
        marker = tmp_path / "quiet"
        client, _chat = make_client(app_env, [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
            model_says("finished"),
        ])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "just do it"})
            request = recv_until(ws, "approval_request")
            assert "intent" not in request
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")
        [record] = self._intent_records(app_env)
        assert "intent" not in record


class TestCardLatency:
    """How long the card was there before it was decided (#306).

    Every design in this space leans on the claim that SOME cards are worth
    spending — the rare, checkable-at-a-glance ones. Nothing measured it. What
    the record now carries is the pair that makes it checkable: `held_ms`, the
    gate's own wait, which is always knowable and is only a FLOOR; and
    `shown_ms`, how long the browser had the card RENDERED when it was tapped,
    which is the number a sub-second value indicts. A card tapped blind is
    worse than no card — it turns a missing control into a recorded consent."""

    def _command_records(self, app_env):
        records = []
        for log in sorted((app_env["state_dir"]).glob("session-*.jsonl")):
            for line in log.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("kind") == "command":
                    records.append(record)
        return records

    def _decide(self, app_env, tmp_path, **answer):
        marker = tmp_path / "latency"
        client, _chat = make_client(app_env, [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
            model_says("finished"),
        ])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {"type": "approval", "id": request["id"], "action": "approve", **answer}
            )
            recv_until(ws, "done")
        [record] = self._command_records(app_env)
        return record

    def test_the_gates_own_wait_is_always_written_down(self, app_env, tmp_path):
        """`held_ms` needs no cooperation from anyone — the gate measured it."""
        record = self._decide(app_env, tmp_path)
        assert isinstance(record["held_ms"], int)
        assert record["held_ms"] >= 0

    def test_the_browser_says_how_long_the_card_was_on_screen(
        self, app_env, tmp_path
    ):
        record = self._decide(app_env, tmp_path, shown_ms=4200)
        assert record["shown_ms"] == 4200

    def test_a_sub_second_tap_is_recorded_as_one(self, app_env, tmp_path):
        """The whole point: this is the value that says the card was not read,
        and it must survive to the log as itself, not be rounded into nothing."""
        record = self._decide(app_env, tmp_path, shown_ms=180)
        assert record["shown_ms"] == 180

    def test_a_client_that_reports_nothing_records_no_number(
        self, app_env, tmp_path
    ):
        """Absent, never zero. Zero is the reading this field exists to detect,
        so inventing one would manufacture the exact finding it looks for."""
        record = self._decide(app_env, tmp_path)
        assert "shown_ms" not in record
        assert record["held_ms"] >= 0  # the half that IS knowable still lands

    def test_the_browser_cannot_author_the_gates_own_number(
        self, app_env, tmp_path
    ):
        """`held_ms` is stamped over whatever arrived. The client is trusted for
        what only it can see; it is not an authority on the server's clock."""
        record = self._decide(app_env, tmp_path, held_ms=99999999)
        assert record["held_ms"] < 99999999

    def test_a_report_that_is_not_a_number_is_not_recorded_as_one(self):
        """Trusting the owner's own browser is fine; parsing whatever it sent as
        a measurement is not. A field the code cannot check is left out."""
        for bogus in ("soon", None, True, [], {"ms": 5}):
            assert "shown_ms" not in server_module.card_latency({"shown_ms": bogus})

    def test_time_that_cannot_have_been_measured_leaves_no_number(self):
        """A negative is DROPPED, not clamped, and the distinction is the whole
        point of the field. Clamping writes a zero, and zero is the most damning
        value this record can carry — the instant tap it exists to detect. An
        unmeasurable value must leave the same absence a string leaves."""
        assert "shown_ms" not in server_module.card_latency({"shown_ms": -50})
        assert "held_ms" not in server_module.card_latency({"held_ms": -1})
        # A REAL zero survives: that one is the finding, not an artefact.
        assert server_module.card_latency({"shown_ms": 0})["shown_ms"] == 0

    def test_a_decision_nobody_was_asked_for_records_no_latency(
        self, app_env, tmp_path
    ):
        """An auto-approval draws no card, so there is no time on screen to
        report. A denylist block and a triggered-session auto-run are the same
        case — a duration here would be a duration for a question never put."""
        app_env["allow_path"].write_text("ls\ntouch\n", encoding="utf-8")
        client, _chat = make_client(app_env, [
            model_says(tool_calls=[
                tool_call("run_command", command=f"touch {tmp_path}/auto")
            ]),
            model_says("finished"),
        ])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            recv_until(ws, "done")
        [record] = self._command_records(app_env)
        assert record["decision"] == "auto"
        assert "held_ms" not in record and "shown_ms" not in record

    def test_a_card_the_server_denied_by_itself_reports_no_screen_time(self):
        """Shutdown and Stop force-deny every held card. Nobody looked at
        those, and the record says so instead of scoring them as instant."""
        assert server_module.card_latency({"action": "deny", "held_ms": 61000}) == {
            "held_ms": 61000
        }

    def test_a_stopped_turn_denies_its_card_without_inventing_a_look(
        self, app_env, tmp_path
    ):
        """The same claim, driven through the real force-deny path rather than
        asserted about it. Stop unparks the worker with a bare deny — there was
        no tap, so there is no screen time, and the wait is still recorded."""
        marker = tmp_path / "never"
        client, _chat = make_client(app_env, [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
            model_says("stopped"),
        ])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            recv_until(ws, "approval_request")
            ws.send_json({"type": "stop"})
            recv_until(ws, "done")
            assert not marker.exists()
        [record] = self._command_records(app_env)
        assert record["decision"] == "denied"
        assert "shown_ms" not in record
        assert isinstance(record["held_ms"], int)


class TestCommandApproval:
    def responses(self, command):
        return [
            model_says(tool_calls=[tool_call("run_command", command=command)]),
            model_says("finished"),
        ]

    def test_approve_runs_command(self, app_env, tmp_path):
        marker = tmp_path / "ran42"
        client, _ = make_client(app_env, self.responses(f"touch {marker}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            assert request["kind"] == "command"
            assert request["command"] == f"touch {marker}"
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")
            assert marker.exists()

    def test_deny_never_executes(self, app_env, tmp_path):
        marker = tmp_path / "pwned"
        client, chat = make_client(app_env, self.responses(f"touch {marker}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")
            assert not marker.exists()
            assert tool_results(chat)[-1]["content"] == DENIED_RESULT

    def test_deny_with_comment_reaches_model(self, app_env, tmp_path):
        """#13: feedback typed into the card comes back as model guidance."""
        marker = tmp_path / "pwned2"
        client, chat = make_client(app_env, self.responses(f"touch {marker}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "deny",
                    "comment": "wrong flag on macOS, use -f",
                }
            )
            resolved = recv_until(ws, "approval_resolved")
            assert resolved["decision"] == "denied"
            assert resolved["comment"] == "wrong flag on macOS, use -f"
            recv_until(ws, "done")
            assert not marker.exists()
            result = tool_results(chat)[-1]["content"]
            assert result.startswith(DENIED_RESULT)
            assert "wrong flag on macOS, use -f" in result

    def test_approve_with_comment_holds_original_for_adjustment(self, app_env, tmp_path):
        """#81: APPROVE + comment = continue but ADJUST — the original command
        is HELD (never run), and the model is told to adjust and re-propose."""
        marker = tmp_path / "ran43"
        client, chat = make_client(app_env, self.responses(f"touch {marker}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "approve",
                    "comment": "run it verbosely instead",
                }
            )
            resolved = recv_until(ws, "approval_resolved")
            assert resolved["decision"] == "approved"
            assert resolved["comment"] == "run it verbosely instead"
            recv_until(ws, "done")
            assert not marker.exists()  # HELD — the original never ran
            result = tool_results(chat)[-1]["content"]
            assert result.startswith("NOT RUN")
            assert "run it verbosely instead" in result
            assert "ADJUSTED" in result

    def test_always_allow_persists_prefix_and_skips_future_prompts(
        self, app_env, tmp_path
    ):
        """#34: "Always allow" writes the shown prefix to the allowlist file,
        so the rule outlives the session and later calls auto-approve."""
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
            model_says("first done"),
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/b")]),
            model_says("second done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch a"})
            request = recv_until(ws, "approval_request")
            assert request["prefixes"] == ["touch"]
            ws.send_json(
                {"type": "approval", "id": request["id"], "action": "approve_always"}
            )
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")
            assert (tmp_path / "a").exists()
            allowed = app_env["allow_path"].read_text(encoding="utf-8").splitlines()
            assert "touch" in allowed

            ws.send_json({"type": "task", "text": "touch b"})
            auto = None
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "done":
                    break
                assert event["type"] != "approval_request"
                if event["type"] == "echo" and "auto-approved" in event["text"]:
                    auto = event
            assert auto is not None
            assert (tmp_path / "b").exists()

    def test_allowlisted_binary_auto_approves_a_quoted_filter(self, app_env, tmp_path):
        """#265, replayed end to end: `jq` in allow.txt, a real jq filter from
        the session that asked 118 times — and NO card. The parser used to cut
        the command inside its own filter and give up before reading the file."""
        app_env["allow_path"].write_text("ls\njq\n", encoding="utf-8")
        command = "jq '.listings[] | {title, photos: .photos[:2]}' data.json"
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=command)]),
            model_says("done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "read the listings"})
            auto = None
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "done":
                    break
                assert event["type"] != "approval_request", "asked despite the allowlist"
                if event["type"] == "echo" and "auto-approved" in event["text"]:
                    auto = event
            assert auto is not None and command in auto["text"]

    def test_card_offers_no_prefix_when_no_prefix_could_work(self, app_env, tmp_path):
        """A command the parser cannot model is refused before the allowlist is
        consulted, so the card must not offer to save a rule for it — the
        frontend hides Session/Always on an empty `prefixes` (#265)."""
        responses = [
            model_says(tool_calls=[tool_call("run_command", command="echo $(whoami)")]),
            model_says("done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "who am i"})
            request = recv_until(ws, "approval_request")
            assert request["prefixes"] == []
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")

    def test_edit_with_comment_holds_for_adjustment(self, app_env, tmp_path):
        """#81: an edit that ALSO carries a comment is still a commented
        approval, so it holds — neither the original nor the edited form runs;
        the model adjusts and re-proposes. (Edit WITHOUT a comment runs the
        edit — see test_edit_runs_edited_command.)"""
        original, edited = tmp_path / "orig43", tmp_path / "edited43"
        client, chat = make_client(app_env, self.responses(f"touch {original}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "edit",
                    "command": f"touch {edited}",
                    "comment": "always use the edited name",
                }
            )
            recv_until(ws, "done")
            assert not edited.exists() and not original.exists()  # HELD
            result = tool_results(chat)[-1]["content"]
            assert result.startswith("NOT RUN")
            assert "always use the edited name" in result

    def test_edit_runs_edited_command(self, app_env, tmp_path):
        original, edited = tmp_path / "original", tmp_path / "edited42"
        client, chat = make_client(app_env, self.responses(f"touch {original}"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "edit",
                    "command": f"touch {edited}",
                }
            )
            assert recv_until(ws, "approval_resolved")["decision"] == "edited"
            recv_until(ws, "done")
            assert edited.exists() and not original.exists()
            assert "user edited the command" in tool_results(chat)[-1]["content"]

    def test_edited_command_still_hits_denylist(self, app_env, tmp_path):
        target = tmp_path / "precious"
        target.mkdir()
        client, chat = make_client(app_env, self.responses(f"touch {tmp_path}/harmless"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "list"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "edit",
                    "command": f"rm -rf {target}",
                }
            )
            recv_until(ws, "done")
            assert target.exists()
            assert "BLOCKED by the safety denylist" in tool_results(chat)[-1]["content"]

    def test_denylisted_command_never_prompts(self, app_env):
        client, chat = make_client(app_env, self.responses("rm -rf /tmp/x"))
        with client, connected(client) as (ws, _, _):
            recv_done = None
            ws.send_json({"type": "task", "text": "nuke"})
            for _ in range(200):
                event = ws.receive_json()
                assert event["type"] != "approval_request"
                if event["type"] == "done":
                    recv_done = event
                    break
            assert recv_done is not None
            assert "BLOCKED by the safety denylist" in tool_results(chat)[-1]["content"]

    def test_allow_this_session_skips_future_prompts(self, app_env, tmp_path):
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
            model_says("first done"),
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/b")]),
            model_says("second done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch a"})
            request = recv_until(ws, "approval_request")
            assert request["prefixes"] == ["touch"]
            ws.send_json(
                {"type": "approval", "id": request["id"], "action": "approve_session"}
            )
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")
            assert (tmp_path / "a").exists()

            ws.send_json({"type": "task", "text": "touch b"})
            auto = None
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "done":
                    break
                assert event["type"] != "approval_request"
                if event["type"] == "echo" and "auto-approved" in event["text"]:
                    auto = event
            assert auto is not None
            assert (tmp_path / "b").exists()

    def _session_allow_responses(self, tmp_path):
        return [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
            model_says("first done"),
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/b")]),
            model_says("second done"),
        ]

    def test_allow_this_session_does_not_leak_into_another_session(self, app_env, tmp_path):
        """The allowance belongs to the chat that granted it, not to the process
        (#176) — it lives on that session's own agent beside its roots, so a
        second chat in the same server is still asked."""
        client, _ = make_client(app_env, self._session_allow_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch a"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {"type": "approval", "id": request["id"], "action": "approve_session"}
            )
            recv_until(ws, "done")

            ws.send_json({"type": "new"})
            recv_until(ws, "hello")
            recv_until(ws, "replay")

            ws.send_json({"type": "task", "text": "touch b"})
            second = recv_until(ws, "approval_request")  # asked again, not inherited
            assert second["prefixes"] == ["touch"]
            ws.send_json({"type": "approval", "id": second["id"], "action": "deny"})
            recv_until(ws, "done")
            assert not (tmp_path / "b").exists()

    def test_allow_this_session_does_not_survive_a_cold_reopen(
        self, app_env, tmp_path, monkeypatch
    ):
        """Prefixes are never written to disk, so a session evicted from memory
        and reopened cold from its log re-grants nothing — the gate asks again
        rather than a persistence the log cannot back (#176)."""
        monkeypatch.setattr(server_module, "MAX_OPEN_SESSIONS", 2)
        client, _ = make_client(app_env, self._session_allow_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "new"})
            name = recv_until(ws, "hello")["session"]
            recv_until(ws, "replay")

            ws.send_json({"type": "task", "text": "touch a"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {"type": "approval", "id": request["id"], "action": "approve_session"}
            )
            recv_until(ws, "done")

            # Move off it (viewerless), then churn past the cap so it is evicted.
            server = client.app.state.server
            for _ in range(2):
                ws.send_json({"type": "new"})
                recv_until(ws, "hello")
                recv_until(ws, "replay")
            assert name not in server.sessions

            ws.send_json({"type": "resume", "path": name})  # reopened cold from disk
            assert recv_until(ws, "hello")["session"] == name
            recv_until(ws, "replay")

            ws.send_json({"type": "task", "text": "touch b"})
            second = recv_until(ws, "approval_request")
            assert second["prefixes"] == ["touch"]
            ws.send_json({"type": "approval", "id": second["id"], "action": "deny"})
            recv_until(ws, "done")
            assert not (tmp_path / "b").exists()

    def test_trust_directory_widens_roots_for_session(self, app_env, tmp_path_factory):
        """The card's "Trust directory" on a root-escaping command: the command
        runs, and allowlisted commands in that directory auto-approve after."""
        outside = tmp_path_factory.mktemp("elsewhere")
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"ls {outside}")]),
            model_says("first done"),
            model_says(tool_calls=[tool_call("run_command", command=f"ls {outside}")]),
            model_says("second done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "look elsewhere"})
            request = recv_until(ws, "approval_request")
            assert request["escapes"] == [str(outside)]
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve_trust"})
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")

            ws.send_json({"type": "task", "text": "look again"})
            auto = None
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "done":
                    break
                assert event["type"] != "approval_request"
                if event["type"] == "echo" and "auto-approved" in event["text"]:
                    auto = event
            assert auto is not None

    def test_in_root_command_card_has_no_escapes(self, app_env, tmp_path):
        client, _ = make_client(app_env, self.responses(f"touch {tmp_path}/plain"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            request = recv_until(ws, "approval_request")
            assert request["escapes"] == []
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")

    def test_allowlisted_readonly_auto_approves(self, app_env):
        client, _ = make_client(app_env, self.responses("ls"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "list"})
            auto = None
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "done":
                    break
                assert event["type"] != "approval_request"
                if event["type"] == "echo" and "auto-approved" in event["text"]:
                    auto = event
            assert auto is not None and auto["text"] == "✓ auto-approved: ls"


class TestTerminalFraming:
    """#52: run_command is framed by recorded command_start / command_end
    events so the browser can draw a bounded terminal block, and a reconnect
    replays the frame identically."""

    def _drain(self, ws) -> list[dict]:
        events = []
        for _ in range(200):
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "done":
                return events
            if event["type"] == "error":
                raise AssertionError(f"error: {event['text']}")
        raise AssertionError("no done within 200 events")

    def test_framing_events_emitted_live_and_recorded(self, app_env, tmp_path):
        marker = tmp_path / "framed"
        # `ls` is allowlisted, so it auto-approves and streams without a card.
        responses = [
            model_says(tool_calls=[tool_call("run_command", command="ls")]),
            model_says("done"),
        ]
        _ = marker  # keep tmp_path scoping obvious
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "list it"})
            events = self._drain(ws)
            starts = [e for e in events if e["type"] == "command_start"]
            ends = [e for e in events if e["type"] == "command_end"]
            assert len(starts) == 1
            assert starts[0]["cwd"] == app_env["cwd"]
            assert starts[0]["command"] == "ls"
            # Live deliveries carry the session stamp (#182); the recorded
            # frame (checked below via the reconnect replay) does not.
            assert ends == [{"type": "command_end", "status": "exit",
                             "exit_code": 0, "session": hello["session"]}]

        # A reconnect replays the recorded frame identically (phone lock/unlock,
        # session switch) — the block must reconstruct from the transcript.
        with client, connected(client) as (_ws, _hello, replay):
            kinds = [e["type"] for e in replay["events"]]
            assert "command_start" in kinds and "command_end" in kinds
            start = next(e for e in replay["events"] if e["type"] == "command_start")
            end = next(e for e in replay["events"] if e["type"] == "command_end")
            assert start["command"] == "ls" and start["cwd"] == app_env["cwd"]
            assert end["status"] == "exit" and end["exit_code"] == 0

    def test_denied_command_has_no_framing(self, app_env, tmp_path):
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("ok"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            events = []
            for _ in range(200):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "done":
                    break
            assert not any(e["type"].startswith("command_") for e in events)


class TestBangCommands:
    """A user-typed ! command runs directly as the user's own action — no model,
    no approval gate — mirroring the CLI's ! escape (cli.main). !cd is the /cd
    alias. The empty responses list means the model is never consulted: a stray
    model call would IndexError and surface as an error event, failing the test."""

    def _drain(self, ws) -> list[dict]:
        events = []
        for _ in range(200):
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "done":
                return events
            if event["type"] == "error":
                raise AssertionError(f"error: {event['text']}")
        raise AssertionError("no done within 200 events")

    def test_bang_command_runs_and_streams_without_model_or_approval(self, app_env):
        client, chat = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "!echo direct-hit"})
            assert recv_until(ws, "user")["text"] == "!echo direct-hit"
            events = self._drain(ws)
            # No approval card: a ! command is the user's own action (CLI parity).
            assert not any(e["type"] == "approval_request" for e in events)
            starts = [e for e in events if e["type"] == "command_start"]
            assert starts and starts[0]["command"] == "echo direct-hit"
            assert starts[0]["cwd"] == app_env["cwd"]
            # user=True so the web renders it inline in the transcript, not
            # inside the model's activity trace (it's a direct user action).
            assert starts[0].get("user") is True
            streamed = " ".join(e["text"] for e in events if e["type"] == "stream")
            assert "direct-hit" in streamed
            ends = [e for e in events if e["type"] == "command_end"]
            assert ends and ends[0] == {"type": "command_end", "status": "exit",
                                        "exit_code": 0, "session": hello["session"]}
            # The output rode the terminal block; done carries no answer bubble.
            assert events[-1] == {"type": "done", "result": "", "session": hello["session"]}
            assert chat.calls == []  # the model was never asked

    def test_large_bang_output_batches_stream_events(self, app_env):
        """Issue #109: a command producing hundreds of output lines must not
        emit one `stream` event per line (the frontend then reflows per line and
        the tab freezes). The coalescer batches them — far fewer events — while
        delivering every line intact."""
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            # awk is portable and streams one line per iteration through on_line.
            ws.send_json({"type": "task", "text": "!awk 'BEGIN{for(i=1;i<=300;i++)print i}'"})
            events = self._drain(ws)
            streams = [e for e in events if e["type"] == "stream"]
            # 300 lines batched at ~50/chunk → an order of magnitude fewer events.
            assert 0 < len(streams) < 50
            lines = [ln for ln in "\n".join(e["text"] for e in streams).split("\n") if ln]
            assert lines == [str(n) for n in range(1, 301)]

    def test_bang_mutating_command_bypasses_approval_like_cli(self, app_env, tmp_path):
        """A ! command that mutates state still runs without a card — exactly as
        the CLI's ! runs `touch` directly. The gate guards the model, not the
        user typing their own command."""
        marker = tmp_path / "bang-made-me"
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": f"!touch {marker}"})
            events = self._drain(ws)
            assert not any(e["type"] == "approval_request" for e in events)
            assert marker.exists()

    def test_bang_cd_moves_cwd_and_reanchors_root(self, app_env, tmp_path):
        """!cd is the /cd alias: it must not be shadowed by the general !command
        path — it moves cwd, re-anchors roots[0], and refreshes the UI cwd."""
        project = tmp_path / "bang-project"
        project.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": f"!cd {project}"})
            changed = recv_until(ws, "cwd_changed")
            assert changed["cwd"] == str(project)
            assert changed["roots"][0] == str(project)
            recv_until(ws, "done")

    def test_bang_session_title_shows_command_not_annotation(self, app_env):
        """The reconnect hello title uses the same bang-aware derivation as the
        drawer, so a ! session reads as '! <cmd>' — never the internal
        '[I ran … myself]' conversation annotation."""
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "!echo titled"})
            self._drain(ws)
        with client, connected(client) as (_ws, hello, _):
            assert hello["title"] == "! echo titled"

    def test_bang_command_replays_as_terminal_block_when_cold(self, app_env):
        """A ! command survives eviction/restart: reopened cold from its log it
        reconstructs into the same user → terminal-block → done event stream a
        live client saw, not the internal "[I ran … myself]" annotation."""
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            old_name = hello["session"]
            ws.send_json({"type": "task", "text": "!echo cold-hit"})
            self._drain(ws)
        client2, _ = make_client(app_env, [])
        with client2, connected(client2) as (ws, _, _):
            ws.send_json({"type": "resume", "path": old_name})
            recv_until(ws, "hello")
            replay = recv_until(ws, "replay")
            kinds = [e["type"] for e in replay["events"]]
            assert "command_start" in kinds and "command_end" in kinds
            user_ev = next(e for e in replay["events"] if e["type"] == "user")
            assert user_ev["text"] == "!echo cold-hit"
            start = next(e for e in replay["events"] if e["type"] == "command_start")
            assert start["command"] == "echo cold-hit"
            assert start.get("user") is True  # inline transcript block on cold replay too
            streamed = " ".join(
                e["text"] for e in replay["events"] if e["type"] == "stream"
            )
            assert "cold-hit" in streamed
            # No raw internal annotation leaks into the transcript.
            assert not any("I ran `" in json.dumps(e) for e in replay["events"])

    def test_bang_command_is_interruptible_by_stop(self, app_env):
        """A long-running ! command is cancellable (issue #76): Stop signals its
        whole process group — the `sh -c` shell AND its child `sleep` — the
        terminal block renders an interrupted status, and the session returns to
        idle promptly (not after the 30s sleep) with no hung worker."""
        client, chat = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            # sh -c keeps a child `sleep` alive, so the stop must reach the whole
            # group, not just the shell it launched.
            ws.send_json({"type": "task", "text": "!sh -c 'sleep 30'"})
            recv_until(ws, "command_start")
            started = time.monotonic()
            ws.send_json({"type": "stop"})
            end = recv_until(ws, "command_end")
            elapsed = time.monotonic() - started
            assert end["status"] == "interrupted"
            assert elapsed < 10  # terminated on the stop, not after the sleep
            recv_until(ws, "done")
            # The worker cleared: the session is idle again, never wedged busy.
            assert client.app.state.server.active.busy is False
            # And a stale cancel didn't leak — a follow-up ! command still runs.
            ws.send_json({"type": "task", "text": "!echo recovered"})
            events = []
            for _ in range(200):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "done":
                    break
            streamed = " ".join(e["text"] for e in events if e["type"] == "stream")
            assert "recovered" in streamed
            assert chat.calls == []  # a ! command never touches the model


class TestWriteApproval:
    def responses(self, path, content):
        return [
            model_says(tool_calls=[tool_call("write_file", path=path, content=content)]),
            model_says("written"),
        ]

    def test_approve_commits(self, app_env, tmp_path):
        target = tmp_path / "note.txt"
        client, _ = make_client(app_env, self.responses(str(target), "hello\n"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "write it"})
            request = recv_until(ws, "approval_request")
            assert request["kind"] == "write"
            assert request["verb"] == "create"
            assert "+hello" in request["diff"]
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")
            assert target.read_text(encoding="utf-8") == "hello\n"

    def test_deny_leaves_disk_untouched(self, app_env, tmp_path):
        target = tmp_path / "note.txt"
        client, chat = make_client(app_env, self.responses(str(target), "hello\n"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "write it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")
            assert not target.exists()
            assert tool_results(chat)[-1]["content"] == WRITE_DENIED

    def test_approve_write_with_comment_holds_for_adjustment(self, app_env, tmp_path):
        """#81: APPROVE + comment holds the write — nothing lands; the model
        adjusts to the comment and re-proposes."""
        target = tmp_path / "note.txt"
        client, chat = make_client(app_env, self.responses(str(target), "hello\n"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "write it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "approve",
                    "comment": "keep future notes under docs/",
                }
            )
            recv_until(ws, "done")
            assert not target.exists()  # HELD — nothing was written
            result = tool_results(chat)[-1]["content"]
            assert result.startswith("NOT WRITTEN")
            assert "keep future notes under docs/" in result
            assert "ADJUSTED" in result

    def test_deny_write_with_comment_reaches_model(self, app_env, tmp_path):
        target = tmp_path / "note.txt"
        client, chat = make_client(app_env, self.responses(str(target), "hello\n"))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "write it"})
            request = recv_until(ws, "approval_request")
            ws.send_json(
                {
                    "type": "approval",
                    "id": request["id"],
                    "action": "deny",
                    "comment": "wrong file — put it in docs/",
                }
            )
            recv_until(ws, "done")
            assert not target.exists()
            result = tool_results(chat)[-1]["content"]
            assert result.startswith(WRITE_DENIED)
            assert "wrong file — put it in docs/" in result

    def test_approved_edit_step_carries_diff(self, app_env, tmp_path):
        """#55: an applied edit's trace step carries the diff the approval card
        computed, so the web timeline renders WHAT changed — live AND cold."""
        target = tmp_path / "note.txt"
        target.write_text("old line\n", encoding="utf-8")
        responses = [
            model_says(tool_calls=[tool_call(
                "edit_file", path=str(target), old_str="old line", new_str="new line")]),
            model_says("edited"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "edit it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")
            server = client.app.state.server
            hot = list(server.active.bridge.transcript)
            path = server.active.logref.log.path
        assert target.read_text(encoding="utf-8") == "new line\n"
        for events in (hot, SessionLog.reconstruct_events(path)):
            step = find_tool_step(events, "edit_file")
            assert step["decision"] == "approved"
            assert "+new line" in step["diff"]
            assert "-old line" in step["diff"]

    def test_denied_edit_step_carries_diff_and_reason(self, app_env, tmp_path):
        """#55/#67: a denied edit stays in the timeline marked denied, with the
        proposed (not-applied) diff and the user's feedback — live AND cold."""
        target = tmp_path / "note.txt"
        target.write_text("old line\n", encoding="utf-8")
        responses = [
            model_says(tool_calls=[tool_call(
                "edit_file", path=str(target), old_str="old line", new_str="new line")]),
            model_says("understood"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "edit it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"],
                          "action": "deny", "comment": "leave it as is"})
            recv_until(ws, "done")
            server = client.app.state.server
            hot = list(server.active.bridge.transcript)
            path = server.active.logref.log.path
        assert target.read_text(encoding="utf-8") == "old line\n"  # never touched disk
        for events in (hot, SessionLog.reconstruct_events(path)):
            step = find_tool_step(events, "edit_file")
            assert step["decision"] == "denied"
            assert step["ok"] is False
            assert "+new line" in step["diff"]
            assert step["comment"] == "leave it as is"


class TestBrowseApproval:
    """Driving a page, over the wire (#290).

    There was no server-side case for the browse gate at all, and the run that
    went looking for one blocked with no card — read as the transport failing
    to deliver a browse card. It was not the transport. `browse` OPENS free
    (a read is a read whichever tool performs it, agent-core), so a scripted
    `browse` never reaches a gate and goes straight to a REAL Chrome; the
    `call` trace record is written BEFORE the whole dispatch and the `gate`
    step is a rules-engine record, so "a call with no gate step" says nothing
    about whether a card was asked for.

    The card is spent on the first PRESS, and these are what that looks like
    from a socket. Browser functions are stubbed at the module boundary, the
    same way tool implementations are everywhere else in this suite — the
    gate, the card, the bridge and the agent loop are all real.
    """

    URL = "https://eon.pl/mojeon"

    def _snapshot(self, text="your invoices"):
        return browse_module.Snapshot(
            url=self.URL, title="eOn", text=text,
            controls=browse_module.controls_from(
                [{"n": 0, "kind": "button", "name": "Faktury"}]
            ),
        )

    def _stub_browser(self, monkeypatch) -> list:
        """What actually reached the page — [] means nothing was pressed."""
        pressed: list = []
        monkeypatch.setattr(
            browser_module, "browse_open", lambda url, **kw: self._snapshot()
        )
        monkeypatch.setattr(
            browser_module, "browse_act",
            lambda address, action, **kw: (
                pressed.append((address, action)), self._snapshot("invoice list")
            )[1],
        )
        return pressed

    def _responses(self, *, press=True):
        script = [model_says(tool_calls=[tool_call("browse", url=self.URL)])]
        if press:
            script.append(
                model_says(tool_calls=[
                    tool_call("browse_act", target="Faktury", action="click")
                ])
            )
        script.append(model_says("opened the invoices"))
        return script

    def test_opening_a_page_asks_nothing(self, app_env, monkeypatch):
        """The scenario the issue scripted: a bare `browse` runs to completion.

        No card, and — the half that was actually missing — a `done`, so a
        harness driving this path has something to wait for."""
        self._stub_browser(monkeypatch)
        client, _ = make_client(app_env, self._responses(press=False))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "open my eon account"})
            events = []
            for _ in range(200):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "done":
                    break
            assert events[-1]["type"] == "done"
            assert not any(e["type"] == "approval_request" for e in events)

    def test_the_press_draws_the_grant_card_and_proceeds(self, app_env, monkeypatch):
        pressed = self._stub_browser(monkeypatch)
        client, _ = make_client(app_env, self._responses())
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "open eon and press Faktury"})
            request = recv_until(ws, "approval_request")
            assert request["kind"] == "tool"
            assert request["tool"] == "browse_act"
            # The card the whole grant rests on says WHOSE hands are on the
            # page — the sentence the owner is agreeing to, not the tool name.
            assert "eon.pl" in request["preview"]
            assert "as you" in request["preview"]
            assert pressed == []  # nothing touched the page before the answer
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")
        assert pressed == [("Faktury", "click")]

    def test_denying_the_grant_never_presses(self, app_env, monkeypatch):
        pressed = self._stub_browser(monkeypatch)
        client, chat = make_client(app_env, self._responses())
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "open eon and press Faktury"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")
        assert pressed == []
        assert "eon.pl" in tool_results(chat, call_index=2)[-1]["content"]

    def test_the_grant_is_asked_once_and_the_log_records_it(self, app_env, monkeypatch):
        """One card per site, and the trace says what he was shown (#284)."""
        pressed = self._stub_browser(monkeypatch)
        script = self._responses()
        script.insert(2, model_says(tool_calls=[
            tool_call("browse_act", target="Faktury", action="click")
        ]))
        client, _ = make_client(app_env, script)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "press it twice"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")
            server = client.app.state.server
            path = server.active.logref.log.path
        assert len(pressed) == 2  # the second press rode the same grant
        records = [json.loads(line) for line in path.read_text().splitlines()]
        cards = [r for r in records if r["kind"] == "command"]
        assert len(cards) == 1
        assert cards[0]["decision"] == "approved"
        # The sentence he was shown, not just the call (#284).
        assert "eon.pl" in cards[0]["preview"]
        # The grant outlives the agent holding it, so it is on disk too.
        assert [r["host"] for r in records if r["kind"] == "site_grant"] == ["eon.pl"]

    def test_the_apps_state_dir_owns_the_browser_profile(self, app_env):
        """A server given its own `state_dir` must not drive the OWNER's Chrome.

        `browser.profile_dir()` hangs off `AISH_STATE_DIR`, which `create_app`
        resolved but never published — so an "isolated" harness on port 8899
        opened the owner's real, signed-in profile, and a browse that blocked
        on it looked like a card that never arrived. Same shape as #254: the
        argument said isolated and the environment decided."""
        make_client(app_env, [model_says("hi")])
        assert browser_module.profile_dir().is_relative_to(app_env["state_dir"])


class TestReconnect:
    def pending_responses(self, tmp_path):
        return [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("finished"),
        ]

    def test_replay_carries_pending_approval(self, app_env, tmp_path):
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({"type": "task", "text": "run it"})
                request = recv_until(ws, "approval_request")
            # phone locked: socket gone, agent still waiting on the approval
            with connected(client) as (ws2, hello, replay):
                assert hello["busy"] is True
                replayed = [
                    e for e in replay["events"] if e["type"] == "approval_request"
                ]
                assert replayed and replayed[0]["id"] == request["id"]
                ws2.send_json(
                    {"type": "approval", "id": request["id"], "action": "approve"}
                )
                recv_until(ws2, "done")

    def test_cd_queued_while_busy(self, app_env, tmp_path):
        # A /cd mid-task can't move state under the running agent, so it's
        # queued and applied when the task finishes — not rejected. It surfaces
        # as a deduplicated queue card (#92), not an invisible echo.
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")  # agent now blocked → busy
            ws.send_json({"type": "cd", "path": str(tmp_path)})
            queued = recv_until(ws, "cwd_queued")
            assert queued["path"] == str(tmp_path)
            assert client.app.state.server.active.pending_cwd == str(tmp_path)
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_second_cd_overwrites_pending_and_re_emits(self, app_env, tmp_path):
        # Dedup (#92): a second cd while one is queued overwrites pending_cwd
        # (single card) and re-emits so the frontend updates in place.
        sub = tmp_path / "sub"
        sub.mkdir()
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "cd", "path": str(tmp_path)})
            recv_until(ws, "cwd_queued")
            ws.send_json({"type": "cd", "path": str(sub)})
            second = recv_until(ws, "cwd_queued")
            assert second["path"] == str(sub)
            assert client.app.state.server.active.pending_cwd == str(sub)
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_dequeue_cwd_clears_pending(self, app_env, tmp_path):
        # Remove (#92): dequeue_cwd clears the pending change and tells the
        # frontend to drop the card.
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "cd", "path": str(tmp_path)})
            recv_until(ws, "cwd_queued")
            ws.send_json({"type": "dequeue_cwd"})
            recv_until(ws, "cwd_dequeued")
            assert client.app.state.server.active.pending_cwd is None
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_cd_and_message_applied_mid_task_between_steps(self, app_env, tmp_path):
        # #95: a /cd AND a message queued while the task runs are BOTH consumed
        # between steps of the SAME task — the cd rebases (card retired via
        # cwd_dequeued, top bar refreshed via cwd_changed) and the message is
        # injected as a steering note — all before the task's own `done`, not
        # deferred to _finish_turn as separate follow-ups.
        sub = tmp_path / "work"
        sub.mkdir()
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("first done"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "first"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "cd", "path": str(sub)})
            recv_until(ws, "cwd_queued")
            ws.send_json({"type": "task", "text": "second"})
            recv_until(ws, "queued")
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "cwd_dequeued")  # cd applied mid-task → card retired
            changed = recv_until(ws, "cwd_changed")  # top bar updated immediately
            assert changed["cwd"] == str(sub)
            injected = recv_step(ws, "injected")  # message injected as steering
            assert injected["text"] == "second"
            recv_until(ws, "done")
            server = client.app.state.server
            assert server.active.pending_cwd is None
            assert str(server.active.agent.cwd) == str(sub)  # rebased mid-task
            assert server.active.queue == []  # message injected once, not relaunched
            # the model saw the steering line in the SAME task
            assert any(
                m.get("content") == "second" for m in server.active.agent.messages
            )

    def test_pending_cwd_card_replays_on_reconnect(self, app_env, tmp_path):
        # The card is backend-authoritative (#92): a reconnect while a cd is
        # pending re-emits cwd_queued so the card reappears.
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({"type": "task", "text": "run it"})
                request = recv_until(ws, "approval_request")
                ws.send_json({"type": "cd", "path": str(tmp_path)})
                recv_until(ws, "cwd_queued")
            with connected(client) as (ws2, hello, _):
                assert hello["busy"] is True
                requeued = recv_until(ws2, "cwd_queued")
                assert requeued["path"] == str(tmp_path)
                ws2.send_json({"type": "approval", "id": request["id"], "action": "approve"})
                recv_until(ws2, "done")

    def test_cd_applied_mid_task_updates_bar_before_done(self, app_env, tmp_path):
        # #95: a /cd queued while a multi-step task runs is applied at the next
        # step boundary — the top bar (cwd_changed) and card (cwd_dequeued)
        # update BEFORE the task's own `done`, so a long task stays responsive.
        sub = tmp_path / "work"
        sub.mkdir()
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("done"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "go"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "cd", "path": str(sub)})
            recv_until(ws, "cwd_queued")
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "cwd_dequeued")  # applied mid-task, card retired
            changed = recv_until(ws, "cwd_changed")
            assert changed["cwd"] == str(sub)
            server = client.app.state.server
            assert server.active.agent.cwd == str(sub)
            assert server.active.pending_cwd is None
            recv_until(ws, "done")


class TestStopAndQueue:
    def test_stop_cancels_task_waiting_on_approval(self, app_env, tmp_path):
        from aish.agent import CANCELLED_RESULT

        marker = tmp_path / "never"
        client, chat = make_client(
            app_env,
            [model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")])],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            recv_until(ws, "approval_request")
            ws.send_json({"type": "stop"})
            done = recv_until(ws, "done")
            assert done["result"] == CANCELLED_RESULT
            assert not marker.exists()
            assert len(chat.calls) == 1  # no model call after the stop
            # Stop with nothing running must not dead-end (#48): it reconciles
            # the foreground to idle with a benign `stopped` sync, never an
            # `error` the UI would render as a task failure.
            ws.send_json({"type": "stop"})  # nothing running anymore
            stopped = recv_until(ws, "stopped")
            assert stopped["type"] == "stopped"

    def test_message_while_busy_injected_into_running_task(self, app_env, tmp_path):
        # #95: a message typed while a task runs still queues (chip appears), but
        # is now DRAINED and INJECTED into the running task between steps —
        # steering, not a deferred separate task. Consumed exactly once.
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
                model_says("first answer"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "first task"})
            request = recv_until(ws, "approval_request")

            ws.send_json({"type": "task", "text": "second task"})
            queued = recv_until(ws, "queued")  # still queues as a chip
            assert queued["position"] == 1

            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            injected = recv_step(ws, "injected")  # drained + injected mid-task
            assert injected["text"] == "second task"
            done = recv_until(ws, "done")
            assert done["result"] == "first answer"  # same task, no second `done`
            server = client.app.state.server
            assert server.active.queue == []  # consumed once, not relaunched
            assert any(
                m.get("content") == "second task" for m in server.active.agent.messages
            )

    def test_bang_command_queued_while_busy_runs_as_shell_not_injected(
        self, app_env, tmp_path
    ):
        # #105: a ! command queued while busy must run as the user's OWN shell
        # command (via _finish_turn/_launch → _run_user_command), not be drained
        # mid-task and injected as a plain model prompt.
        bang_file = tmp_path / "bang"
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
                model_says("first answer"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "first task"})
            request = recv_until(ws, "approval_request")

            ws.send_json({"type": "task", "text": f"!touch {bang_file}"})
            assert recv_until(ws, "queued")["position"] == 1

            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            # First task finishes with NO mid-task injection of the ! item.
            assert recv_until(ws, "done")["result"] == "first answer"
            # _finish_turn relaunches it as a user-direct command: it echoes as a
            # `user` event carrying the ! text (never an `injected` steering step).
            assert recv_until(ws, "user")["text"] == f"!touch {bang_file}"
            recv_until(ws, "done")  # the ! command's own (empty) done

        # It actually ran as a shell command (the file exists) and was never
        # injected verbatim as a model steering message.
        assert bang_file.exists()
        server = client.app.state.server
        assert server.active.queue == []
        assert not any(
            m.get("content") == f"!touch {bang_file}"
            for m in server.active.agent.messages
        )


class TestMultiConnection:
    """#102: N connections (phone, laptop, headless test) share one token and
    coexist WITHOUT preempting — each views a session independently, events fan
    out to every viewer, and control is last-actor-drives."""

    def test_second_connection_does_not_preempt_and_both_get_events(self, app_env):
        # Two sockets viewing the same session both receive its events, and the
        # first is NOT closed when the second connects (the old CLOSE_REPLACED
        # behaviour is gone).
        client, _ = make_client(app_env, [model_says("shared answer")])
        with client, connected(client) as (ws_a, hello_a, _):
            name = hello_a["session"]
            with connected(client, f"/ws?session={name}") as (ws_b, hello_b, _):
                assert hello_b["session"] == name  # B joined the SAME session
                # A is still alive (not preempted): its action drives both views.
                ws_a.send_json({"type": "task", "text": "go"})
                assert recv_until(ws_a, "user")["text"] == "go"
                assert recv_until(ws_b, "user")["text"] == "go"  # fanned to B too
                assert recv_until(ws_a, "done")["result"] == "shared answer"
                assert recv_until(ws_b, "done")["result"] == "shared answer"

    def test_action_stamps_control_and_broadcasts_role_to_both(self, app_env, tmp_path):
        # An action from B claims control; both viewers get a `role` event so
        # each tab knows whether IT drives.
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws_a, hello_a, _):
            name = hello_a["session"]
            with connected(client, f"/ws?session={name}") as (ws_b, _, _):
                # B acts (a /cd — no model needed) → B becomes the controller.
                ws_b.send_json({"type": "cd", "path": str(tmp_path)})
                role_b = recv_until(ws_b, "role")
                role_a = recv_until(ws_a, "role")
                assert role_b["you"] is True  # B drives
                assert role_a["you"] is False  # A is now an observer
                # Same controller id reported to both tabs.
                assert role_a["controller"] == role_b["controller"]
                assert role_b["controller"] is not None
                server = client.app.state.server
                assert server.sessions[name].controller is not None

    def test_either_client_can_answer_approval_exactly_once(self, app_env, tmp_path):
        # The approval card fans out to both viewers; the NON-initiating client
        # answers it and the command runs exactly once (the event loop
        # serializes messages, so only one answer() reaches the blocked worker).
        marker = tmp_path / "shared-ran"
        client, chat = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
                model_says("finished"),
            ],
        )
        with client, connected(client) as (ws_a, hello_a, _):
            name = hello_a["session"]
            with connected(client, f"/ws?session={name}") as (ws_b, _, _):
                ws_a.send_json({"type": "task", "text": "run it"})
                req_a = recv_until(ws_a, "approval_request")
                req_b = recv_until(ws_b, "approval_request")
                assert req_a["id"] == req_b["id"]  # same card on both
                # B (not the initiator) approves.
                ws_b.send_json({"type": "approval", "id": req_b["id"], "action": "approve"})
                assert recv_until(ws_a, "done")["result"] == "finished"
                assert recv_until(ws_b, "done")["result"] == "finished"
                assert marker.exists()
                # Exactly two model calls (initial + post-tool) proves the
                # command ran once — a double answer would have re-run it and
                # over-run the scripted responses into an error.
                assert len(chat.calls) == 2
                # A stale duplicate answer from A is a harmless no-op: the slot
                # was consumed, so it neither errors nor re-runs anything.
                ws_a.send_json({"type": "approval", "id": req_a["id"], "action": "approve"})
                ws_a.send_json({"type": "jobs"})
                assert recv_until(ws_a, "job_list")  # A's stream stays healthy

    def test_viewers_of_different_sessions_are_isolated(self, app_env):
        # A client viewing session X receives nothing from activity in session Y.
        client, _ = make_client(app_env, [model_says("beta answer")])
        with client, connected(client) as (ws_a, _, _):
            # B opens a brand-new session and runs a whole task there.
            with connected(client) as (ws_b, _, _):
                ws_b.send_json({"type": "new"})
                recv_until(ws_b, "hello")
                recv_until(ws_b, "replay")
                ws_b.send_json({"type": "task", "text": "beta"})
                assert recv_until(ws_b, "done")["result"] == "beta answer"
                # A viewed the original session throughout. It may get a
                # cross-session `session_state` heads-up (the drawer badge), but
                # NONE of Y's transcript events (user/token/step/done/command)
                # leak into A's stream. Drain up to A's own jobs reply.
                ws_a.send_json({"type": "jobs"})
                leaked = {"user", "token", "step", "done", "command_start",
                          "command_end", "stream", "approval_request"}
                for _ in range(50):
                    ev = ws_a.receive_json()
                    if ev["type"] == "job_list":
                        break
                    assert ev["type"] not in leaked, f"leaked Y event: {ev['type']}"
                else:
                    raise AssertionError("A never got its jobs reply")

    def test_disconnect_clears_viewer_and_releases_control(self, app_env, tmp_path):
        # When the controller disconnects, control is released and the remaining
        # viewer is told (role → controller null); the viewer set drops it. B is
        # the OUTER (surviving) connection so it can observe A leaving.
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws_b, hello_b, _):
            name = hello_b["session"]
            with connected(client, f"/ws?session={name}") as (ws_a, _, _):
                ws_a.send_json({"type": "cd", "path": str(tmp_path)})  # A claims control
                recv_until(ws_a, "role")  # A: you=true
                recv_until(ws_b, "role")  # B: you=false, controller = A
                server = client.app.state.server
                assert len(server.sessions[name].viewers) == 2
            # A disconnected (inner scope exited). B, still open, is told control
            # was released — a deterministic signal that _detach ran.
            released = recv_until(ws_b, "role")
            assert released["controller"] is None
            assert released["you"] is False
            sess = client.app.state.server.sessions[name]
            assert sess.controller is None
            assert len(sess.viewers) == 1  # only B remains

    def test_eviction_skips_sessions_with_viewers(self, app_env, monkeypatch):
        # A non-default session that still has a viewer is never evicted, even as
        # other viewerless sessions are churned past the cap.
        monkeypatch.setattr(server_module, "MAX_OPEN_SESSIONS", 3)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws_a, _, _):
            # A moves onto its own non-default session and stays there.
            ws_a.send_json({"type": "new"})
            held = recv_until(ws_a, "hello")["session"]
            recv_until(ws_a, "replay")
            server = client.app.state.server
            with connected(client) as (ws_b, _, _):
                # B churns sessions to drive eviction. Capture the first one it
                # abandons — it is the viewerless candidate that must be evicted.
                ws_b.send_json({"type": "new"})
                churned = recv_until(ws_b, "hello")["session"]
                recv_until(ws_b, "replay")
                ws_b.send_json({"type": "new"})  # abandons `churned` (now viewerless)
                recv_until(ws_b, "hello")
                recv_until(ws_b, "replay")
                ws_b.send_json({"type": "new"})  # cap hit → eviction runs
                recv_until(ws_b, "hello")
                recv_until(ws_b, "replay")
                assert held in server.sessions  # kept: A still views it
                assert churned not in server.sessions  # evicted: viewerless


class RaisingChat:
    """A backend that always raises, simulating a model/transport failure. The
    agent retries once then surfaces ModelUnavailable, so run_task raises and
    the server must emit a terminal error that clears the foreground."""

    def __init__(self, message: str = "boom: model exploded"):
        self.message = message
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError(self.message)


def raising_client(app_env) -> TokenClient:
    app = create_app("fake", client_chat=RaisingChat(), token=TEST_TOKEN, **app_env)
    return TokenClient(app, auto_token=TEST_TOKEN)


def recv_any(ws, wanted: str, limit: int = 200) -> dict:
    """Like recv_until but does NOT treat an `error` event as fatal — used
    when the error IS the event under test."""
    for _ in range(limit):
        event = ws.receive_json()
        if event["type"] == wanted:
            return event
    raise AssertionError(f"no {wanted!r} event within {limit} events")


class TestModelError:
    """#48: a mid-task model error must leave the session and its foreground
    consistent — a terminal event clears busy, the busy flag is false, Stop
    afterward is a graceful no-op, and a cold re-attach shows it finished."""

    def test_error_emits_terminal_and_clears_busy(self, app_env):
        client = raising_client(app_env)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "do it"})
            error = recv_any(ws, "error")
            assert "model unavailable" in error["text"]
            # Server-side truth: the busy flag cleared with the error.
            assert client.app.state.server.active.busy is False
            assert client.app.state.server.active.state() == "idle"

    def test_stop_after_error_is_graceful_noop(self, app_env):
        client = raising_client(app_env)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "do it"})
            recv_any(ws, "error")
            # The wedged-foreground reconciliation: Stop never dead-ends.
            ws.send_json({"type": "stop"})
            stopped = recv_any(ws, "stopped")
            assert stopped["type"] == "stopped"

    def test_reattached_errored_session_shows_finished(self, app_env):
        # Re-attaching an errored session (switch away and back, or phone
        # lock/unlock) must report idle (not running) and replay the recorded
        # error — never a stuck "working" foreground.
        client = raising_client(app_env)
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "do it"})
            recv_any(ws, "error")
            # Re-show the same session: hello reports its authoritative state
            # and the transcript replay carries the terminal error.
            ws.send_json({"type": "resume", "path": name})
            hello2 = recv_any(ws, "hello")
            replay = recv_any(ws, "replay")
            assert hello2["session"] == name
            assert hello2["busy"] is False
            assert any(e["type"] == "error" for e in replay["events"])

    def test_errored_session_is_deletable(self, app_env):
        # busy cleared → state() == "idle" → the delete guard allows removal.
        client = raising_client(app_env)
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "do it"})
            recv_any(ws, "error")
            ws.send_json({"type": "delete_session", "name": name})
            deleted = recv_any(ws, "session_deleted")
            assert deleted["name"] == name


class TestSessions:
    def test_new_session_swaps_log_and_clears_transcript(self, app_env):
        client, _ = make_client(app_env, [model_says("answer one")])
        with client:
            with connected(client) as (ws, hello, _):
                first = hello["session"]
                ws.send_json({"type": "task", "text": "task one"})
                recv_until(ws, "done")
                ws.send_json({"type": "new"})
                fresh = recv_until(ws, "hello")
                assert fresh["session"] != first
                # The empty replay is the client's clear-screen signal.
                cleared = recv_until(ws, "replay")
                assert cleared["events"] == []
            # A reconnect naming the fresh session (as the real client does via
            # ?session=) replays it empty. A BARE reconnect now lands on the
            # default startup session instead (#102), not the last-shown one.
            with connected(client, f"/ws?session={fresh['session']}") as (_ws, _, replay):
                assert replay["events"] == []

    def test_session_list_reports_waiting_state_for_pending_approval(
        self, app_env, tmp_path
    ):
        # The drawer's "Active now" grouping keys off this per-session state:
        # a session blocked on an approval must surface as "waiting".
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("done"),
            ],
        )
        with client, connected(client) as (ws, hello, _):
            current = hello["session"]
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")  # agent now blocked
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            row = next(s for s in listing["sessions"] if s["name"] == current)
            assert row["state"] == "waiting"
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_session_list_includes_and_names_current(self, app_env):
        # The drawer lists the active session too (MRU: it sorts first) and
        # names it in "current" so the UI can mark "you are here" (#29).
        client, _ = make_client(app_env, [model_says("alpha done")])
        with client, connected(client) as (ws, hello, _):
            session_a = hello["session"]
            ws.send_json({"type": "task", "text": "alpha task"})
            recv_until(ws, "done")

            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert listing["current"] == session_a
            row = listing["sessions"][0]
            assert row["name"] == session_a
            # The drawer's preview line and day-grouping timestamp.
            assert row["snippet"] == "alpha done"
            assert row["ts"] > 0

            # A brand-new chat is current but has no messages yet, so it is
            # not listed — nothing carries the current mark.
            ws.send_json({"type": "new"})
            fresh = recv_until(ws, "hello")
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert listing["current"] == fresh["session"]
            names = [s["name"] for s in listing["sessions"]]
            assert fresh["session"] not in names
            assert session_a in names

    def test_a_searched_row_quotes_its_match_and_says_when_it_is_only_close(
        self, app_env
    ):
        # #266: the rail's answer to a search has to show WHY each row is in it,
        # and say so when it is showing the closest chats rather than matches.
        client, _ = make_client(app_env, [
            model_says("Of the ones still sold, the Tefal SW852D is closest to yours."),
            model_says("A simple moka pot."),
        ])
        with client, connected(client) as (ws, _hello, _):
            ws.send_json({"type": "task", "text": "which sandwich toaster should I buy"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "and for coffee"})
            recv_until(ws, "done")

            ws.send_json({"type": "sessions", "query": "tefal"})
            listing = recv_until(ws, "session_list")
            assert "approx" not in listing
            assert "Tefal SW852D" in listing["sessions"][0]["snippet"]

            ws.send_json({"type": "sessions", "query": "tefla"})  # typo: nothing matches
            listing = recv_until(ws, "session_list")
            assert listing["approx"] is True
            assert len(listing["sessions"]) == 1

            ws.send_json({"type": "sessions", "query": ""})  # unsearched: where it got to
            listing = recv_until(ws, "session_list")
            assert "approx" not in listing
            assert listing["sessions"][0]["snippet"] == "A simple moka pot."

    def test_session_list_labels_directory_from_the_log_when_cold(self, app_env):
        # The row's directory used to come ONLY from sessions still open in
        # memory, so it showed on a handful of rows and silently vanished from
        # the rest as the eviction sweep closed them. It now falls back to the
        # cwd recorded in the log, and a session sitting in the server's own
        # workspace shows none at all — the same path on every row is noise.
        state_dir = app_env["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        elsewhere = Path(app_env["cwd"]) / "worktree-a"
        elsewhere.mkdir()
        moved = state_dir / "session-20200101-000000-000000.jsonl"
        moved.write_text(
            '{"kind": "message", "role": "user", "content": "in the worktree"}\n'
            + json.dumps({"kind": "cwd", "cwd": str(elsewhere)})
            + '\n{"kind": "message", "role": "assistant", "content": "worktree answer"}\n',
            encoding="utf-8",
        )

        client, _ = make_client(app_env, [model_says("home answer")])
        with client, connected(client) as (ws, hello, _):
            here = hello["session"]
            ws.send_json({"type": "task", "text": "at home"})
            recv_until(ws, "done")

            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            rows = {s["name"]: s["cwd"] for s in listing["sessions"]}
            assert rows[moved.name] == str(elsewhere)  # cold — read from its log
            assert rows[here] == ""  # open, but never left the baseline workspace

            # A live session outranks its log: it may have moved since the last
            # recorded /cd, so the in-memory agent answers for it.
            ws.send_json({"type": "cd", "path": str(elsewhere)})
            recv_until(ws, "cwd_changed")
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            rows = {s["name"]: s["cwd"] for s in listing["sessions"]}
            assert rows[here] == str(elsewhere)

    def test_reviewing_old_session_keeps_order_until_new_message(self, app_env):
        # Resuming an older session only READS it: the file keeps its mtime,
        # so the MRU order (drawer + swipe pager) is unchanged. Only a new
        # message makes the session "latest" again.
        state_dir = app_env["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        old = state_dir / "session-20200101-000000-000000.jsonl"
        old.write_text(
            '{"kind": "message", "role": "user", "content": "old topic"}\n'
            '{"kind": "message", "role": "assistant", "content": "old answer"}\n',
            encoding="utf-8",
        )
        stale = time.time() - 3600
        os.utime(old, (stale, stale))

        client, _ = make_client(app_env, [model_says("fresh done"), model_says("revived")])
        with client, connected(client) as (ws, hello, _):
            fresh = hello["session"]
            ws.send_json({"type": "task", "text": "fresh topic"})
            recv_until(ws, "done")

            ws.send_json({"type": "resume", "path": old.name})
            recv_until(ws, "hello")
            assert os.path.getmtime(old) == pytest.approx(stale, abs=1)
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert [s["name"] for s in listing["sessions"]] == [fresh, old.name]
            assert listing["current"] == old.name

            ws.send_json({"type": "task", "text": "revive it"})
            recv_until(ws, "done")
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert [s["name"] for s in listing["sessions"]] == [old.name, fresh]

    def test_list_and_resume_previous_session(self, app_env):
        client, chat = make_client(
            app_env, [model_says("first answer"), model_says("second answer")]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "remember the zebra"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            recv_until(ws, "hello")

            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert listing["sessions"], "previous session missing from list"
            assert "zebra" in listing["sessions"][0]["title"]

            resumed_name = listing["sessions"][0]["name"]
            ws.send_json({"type": "resume", "path": resumed_name})
            hello = recv_until(ws, "hello")
            assert hello["session"] == resumed_name  # switched, not merged
            replay = recv_until(ws, "replay")
            # Still open in memory: the live transcript replays as-is.
            users = [e for e in replay["events"] if e["type"] == "user"]
            assert users and "zebra" in users[0]["text"]

            ws.send_json({"type": "task", "text": "what animal did I mention?"})
            recv_until(ws, "done")
            contents = json.dumps(chat.calls[-1]["messages"])
            assert "zebra" in contents  # resumed context reached the model

    def test_hello_pager_pages_recent_chats_oldest_first(self, app_env):
        # The swipe pager pages through hello["pager"]: recent chats by last
        # interaction, oldest→newest (back = older, forward = newer). Chats
        # with no user input are not pages — except the current one.
        client, _ = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, hello, _):
            first = hello["session"]
            assert [p["name"] for p in hello["pager"]] == [first]
            ws.send_json({"type": "task", "text": "remember the yak"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            hello = recv_until(ws, "hello")
            second = hello["session"]
            assert [p["name"] for p in hello["pager"]] == [first, second]
            assert hello["pager"][0]["title"] == "remember the yak"
            recv_until(ws, "replay")
            # Back on the first chat, the still-empty new one is not a page.
            ws.send_json({"type": "resume", "path": first})
            hello = recv_until(ws, "hello")
            assert [p["name"] for p in hello["pager"]] == [first]

    def test_every_pager_page_carries_a_usable_timestamp(self, app_env):
        # The hello's recency list is what the client orders rows and warms
        # prefetches from, so an undated row is unusable. Every row must carry a
        # real stamp, including the current chat's synthesized one (which has no
        # file activity behind it yet).
        client, _ = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, hello, _):
            first = hello["session"]
            # The synthesized page for a brand-new, still-empty chat.
            assert [p["name"] for p in hello["pager"]] == [first]
            assert hello["pager"][0]["ts"] > 0
            ws.send_json({"type": "task", "text": "remember the yak"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            hello = recv_until(ws, "hello")
            # Now a real on-disk page plus the synthesized one: both stamped,
            # and ordered oldest→newest by that stamp, matching the page order.
            stamps = [p["ts"] for p in hello["pager"]]
            assert len(stamps) == 2
            assert all(isinstance(t, (int, float)) and t > 0 for t in stamps)
            assert stamps == sorted(stamps)

    def test_pager_orders_by_last_interaction_and_spans_restarts(self, app_env):
        # Interacting with an old chat moves it to the newest end, and a
        # fresh server lists chats it never opened (swipe loads them from
        # disk via resume) — same recency order as the sessions drawer.
        responses = [model_says("a"), model_says("b"), model_says("a2")]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello, _):
            session_a = hello["session"]
            ws.send_json({"type": "task", "text": "alpha task"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            hello = recv_until(ws, "hello")
            session_b = hello["session"]
            ws.send_json({"type": "task", "text": "beta task"})
            recv_until(ws, "done")
            ws.send_json({"type": "resume", "path": session_a})
            recv_until(ws, "replay")
            ws.send_json({"type": "task", "text": "alpha again"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            hello = recv_until(ws, "hello")
            assert [p["name"] for p in hello["pager"]] == [
                session_b, session_a, hello["session"]
            ]
        client2, _ = make_client(app_env, [])
        with client2, connected(client2) as (_ws, hello, _):
            names = [p["name"] for p in hello["pager"]]
            assert names[:2] == [session_b, session_a]  # never opened here
            assert names[-1] == hello["session"]

    def test_resume_from_disk_replays_history(self, app_env):
        # First server instance writes a session to disk…
        client, _ = make_client(app_env, [model_says("noted the walrus")])
        with client, connected(client) as (ws, hello, _):
            old_name = hello["session"]
            ws.send_json({"type": "task", "text": "remember the walrus"})
            recv_until(ws, "done")
        # …a fresh instance (nothing in memory) reopens it from the file.
        client2, chat2 = make_client(app_env, [model_says("the walrus")])
        with client2, connected(client2) as (ws, _, _):
            ws.send_json({"type": "resume", "path": old_name})
            hello = recv_until(ws, "hello")
            assert hello["session"] == old_name
            replay = recv_until(ws, "replay")
            # A logged session reconstructs into the same user/step/done event
            # stream a live one replays — not a flat history blob.
            user_ev = next(e for e in replay["events"] if e["type"] == "user")
            assert "walrus" in user_ev["text"]
            done_ev = next(e for e in replay["events"] if e["type"] == "done")
            assert "noted the walrus" in done_ev["result"]

            ws.send_json({"type": "task", "text": "what animal?"})
            recv_until(ws, "done")
            assert "walrus" in json.dumps(chat2.calls[-1]["messages"])

    def test_cold_reconstruction_matches_live_transcript(self, app_env):
        # The guard for the hot/cold invariant. A live run's canonical event
        # record (bridge.transcript) and the cold reconstruction from its log
        # must project to the SAME durable trace shape. This is the single test
        # that keeps the two paths from drifting: add a new trace event type to
        # the live stream without persisting + reconstructing it (as command
        # framing once was) and this fails immediately.
        client, _ = make_client(app_env, [
            model_says(tool_calls=[tool_call("run_command", command="ls")]),
            model_says("listed the directory"),
        ])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "list files"})
            recv_until(ws, "done")
            server = client.app.state.server
            hot = list(server.active.bridge.transcript)
            path = server.active.logref.log.path

        cold = SessionLog.reconstruct_events(path)
        assert cold is not None
        # A run_command must survive the round-trip as its full terminal-block
        # sequence, not a bare tool step — the whole point of the framing work.
        assert ("command_start",) in trace_shape(cold)
        assert trace_shape(hot) == trace_shape(cold)

    def test_cold_reconstruction_matches_live_for_held_command(self, app_env, tmp_path):
        # #81: an approve+comment HOLD never runs, so it emits no terminal block
        # (like a denial). Cold replay must match — the None-framing synthesize
        # path must NOT fabricate a command_start for a command that never ran.
        # A mutating command (not read-only) so it actually prompts.
        client, _ = make_client(app_env, [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/z")]),
            model_says("acknowledged"),
        ])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "make a file"})
            request = recv_until(ws, "approval_request")
            ws.send_json({
                "type": "approval", "id": request["id"],
                "action": "approve", "comment": "put it under tmp/ instead",
            })
            recv_until(ws, "done")
            server = client.app.state.server
            hot = list(server.active.bridge.transcript)
            path = server.active.logref.log.path

        cold = SessionLog.reconstruct_events(path)
        assert cold is not None
        assert ("command_start",) not in trace_shape(cold)  # held → no terminal block
        assert trace_shape(hot) == trace_shape(cold)

    def test_cwd_and_trust_changes_log_and_reconstruct(self, app_env, tmp_path):
        # #94: a /cd and a /add-dir emit live `workspace` timeline markers AND
        # persist, so the cold reconstruction projects the identical shape —
        # the same hot/cold invariant the trace and command framing obey.
        elsewhere, shared = tmp_path / "elsewhere", tmp_path / "shared"
        elsewhere.mkdir()
        shared.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "cd", "path": str(elsewhere)})
            live_cd = recv_until(ws, "workspace")
            assert live_cd["change"] == "cwd" and live_cd["path"] == str(elsewhere)
            ws.send_json({"type": "add_dir", "path": str(shared)})
            live_trust = recv_until(ws, "workspace")
            assert live_trust["change"] == "trust"
            server = client.app.state.server
            hot = list(server.active.bridge.transcript)
            path = server.active.logref.log.path

        cold = SessionLog.reconstruct_events(path)
        assert cold is not None
        shape = trace_shape(cold)
        assert ("workspace", "cwd", str(elsewhere)) in shape
        assert ("workspace", "trust", str(shared.resolve())) in shape
        # The consistency invariant: the live `workspace` events and the ones
        # reconstruct_events replays are byte-identical. (The full-transcript
        # shape differs only by the /cd + /add-dir context notes the agent
        # injects into the conversation, which predate #94.)
        ws_hot = [e for e in hot if e["type"] == "workspace"]
        ws_cold = [e for e in cold if e["type"] == "workspace"]
        assert ws_hot == ws_cold

    def test_cold_open_restores_cwd_and_trusted_roots(self, app_env, tmp_path):
        # #94: reopening a session cold restores where it left off (cwd + the
        # dirs it trusted), not the server's launch dir.
        elsewhere, shared = tmp_path / "elsewhere", tmp_path / "shared"
        elsewhere.mkdir()
        shared.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "cd", "path": str(elsewhere)})
            recv_until(ws, "workspace")
            ws.send_json({"type": "add_dir", "path": str(shared)})
            recv_until(ws, "workspace")

        # Fresh server over the same state dir → the session is cold-loaded.
        client2, _ = make_client(app_env, [])
        with client2, connected(client2, f"/ws?session={name}") as (_, hello2, _):
            assert hello2["cwd"] == str(elsewhere)
            assert str(shared.resolve()) in hello2["roots"]

    def test_cold_open_keeps_the_servers_uploads_root(self, app_env, tmp_path):
        # #176: restoring a workspace is AUTHORITATIVE — it rebuilds roots to be
        # exactly that chat's own, so a dir from another chat can't ride along.
        # The uploads dir belongs to the SERVER, not to any one chat, and is
        # re-added afterwards; without that a cold-opened session would lose
        # read access to its own attachments.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "cd", "path": str(elsewhere)})
            recv_until(ws, "workspace")

        client2, _ = make_client(app_env, [])
        with client2, connected(client2, f"/ws?session={name}") as (_, hello2, _):
            uploads = client2.app.state.server.uploads_dir.resolve()
            assert hello2["cwd"] == str(elsewhere)
            assert str(uploads) in hello2["roots"]

    def test_cold_open_skips_vanished_cwd(self, app_env, tmp_path):
        # #94: a restored cwd that no longer exists falls back to the default
        # instead of crashing the cold open.
        gone = tmp_path / "gone"
        gone.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "cd", "path": str(gone)})
            recv_until(ws, "workspace")
        gone.rmdir()  # the directory disappears before the session is reopened

        client2, _ = make_client(app_env, [])
        with client2, connected(client2, f"/ws?session={name}") as (_, hello2, _):
            assert hello2["cwd"] == app_env["cwd"]  # gracefully back to default

    def test_connect_with_session_param_reattaches_after_restart(self, app_env):
        # The client names its session on (re)connect so a server restart
        # doesn't strand it in the fresh startup session.
        client, _ = make_client(app_env, [model_says("noted the walrus")])
        with client, connected(client) as (ws, hello, _):
            old_name = hello["session"]
            ws.send_json({"type": "task", "text": "remember the walrus"})
            recv_until(ws, "done")
        client2, _ = make_client(app_env, [])
        with client2, connected(client2, f"/ws?session={old_name}") as (_, hello, replay):
            assert hello["session"] == old_name
            user_evs = [e for e in replay["events"] if e["type"] == "user"]
            assert user_evs and any("walrus" in e["text"] for e in user_evs)

    def test_connect_with_unknown_session_falls_back_to_active(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client, "/ws?session=session-gone.jsonl") as (_, hello, _):
            assert hello["session"].startswith("session-")
            assert hello["session"] != "session-gone.jsonl"

    def test_parallel_sessions_run_and_finish_independently(self, app_env, tmp_path):
        # Session A blocks on an approval; session B runs a full task while A
        # is still waiting; switching back to A replays the pending card and
        # approving it finishes A's task.
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
            model_says("B says hi"),  # session B's whole task
            model_says("A finished"),  # session A resumes after approval
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello_a, _):
            session_a = hello_a["session"]
            ws.send_json({"type": "task", "text": "touch a file"})
            request = recv_until(ws, "approval_request")

            ws.send_json({"type": "new"})
            hello_b = recv_until(ws, "hello")
            assert hello_b["session"] != session_a
            recv_until(ws, "replay")

            ws.send_json({"type": "task", "text": "say hi"})
            done_b = recv_until(ws, "done")
            assert done_b["result"] == "B says hi"

            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            state_by_name = {s["name"]: s["state"] for s in listing["sessions"]}
            assert state_by_name[session_a] == "waiting"

            ws.send_json({"type": "resume", "path": session_a})
            back = recv_until(ws, "hello")
            assert back["session"] == session_a and back["busy"] is True
            replay = recv_until(ws, "replay")
            pending = [e for e in replay["events"] if e["type"] == "approval_request"]
            assert pending and pending[0]["id"] == request["id"]

            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            done_a = recv_until(ws, "done")
            assert done_a["result"] == "A finished"
            assert (tmp_path / "a").exists()

    def test_background_finish_sends_notice(self, app_env, tmp_path):
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("A done in background"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello_a, _):
            session_a = hello_a["session"]
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "new"})
            recv_until(ws, "hello")
            recv_until(ws, "replay")
            # Approve A's card while B is shown: A finishes in the background
            # and the client gets a roster heads-up (#204).
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            notice = recv_until_row(ws, session_a, "idle")
            assert notice["notice"] == "finished"

    def test_background_hold_sends_waiting_notice(self, app_env, tmp_path):
        # The mirror image of the idle notice (#203). A chat that STOPS on an
        # approval while you are looking elsewhere is the most literal thing
        # there is that "needs you", and it waits indefinitely — so it cannot be
        # left for the next time the rail happens to be opened.
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/y")]),
            model_says("both done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello_a, _):
            session_a = hello_a["session"]
            ws.send_json({"type": "task", "text": "run them"})
            first = recv_until(ws, "approval_request")
            ws.send_json({"type": "new"})
            recv_until(ws, "hello")
            recv_until(ws, "replay")
            # Approving from B lets A run on and stop at its SECOND approval,
            # now with nobody viewing it.
            ws.send_json({"type": "approval", "id": first["id"], "action": "approve"})
            notice = recv_until_row(ws, session_a, "waiting")
            assert notice["notice"] == "held"

    def test_a_hold_someone_is_watching_is_not_announced(self, app_env, tmp_path):
        # Scoped deliberately: a held card on a screen someone is already
        # looking at is not a chat that wants a user who is somewhere else.
        # Without this, every command approved on the laptop would nudge the
        # phone in your pocket — approvals are the most frequent event there is.
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (phone, hello_a, _):
            session_a = hello_a["session"]
            phone.send_json({"type": "new"})  # the phone moves to a second chat
            recv_until(phone, "hello")
            recv_until(phone, "replay")
            # A bare connect lands on the DEFAULT session — still A, since a new
            # chat never becomes the default.
            with client.websocket_connect("/ws") as laptop:
                assert laptop.receive_json()["session"] == session_a
                laptop.receive_json()  # replay
                laptop.send_json({"type": "task", "text": "run it"})
                request = recv_until(laptop, "approval_request")
                laptop.send_json(
                    {"type": "approval", "id": request["id"], "action": "approve"}
                )
                recv_until(laptop, "done")
            # A finishing DOES reach the phone (that notice is unconditional),
            # so this drain is bounded — and what it must not contain is a
            # heads-up for the hold the laptop was looking at all along.
            notices = []
            for _ in range(200):
                event = phone.receive_json()
                if event["type"] != "session_changed":
                    continue
                if event.get("notice"):
                    notices.append(event["notice"])
                if event["row"]["state"] == "idle" and event.get("notice"):
                    break
            # The hold's ROW still travelled (every list needs it); what it must
            # not carry is the interruption, since the laptop was looking at it.
            assert notices == ["finished"]

    def test_answering_a_card_publishes_the_chat_as_running_again(self, app_env, tmp_path):
        # The RELEASE edge, which the row could not state (#204 follow-up). The
        # publish was made by the message handler the instant the answer
        # arrived — on the loop thread, while the worker that owns the hold had
        # not yet woken to drop it — so the row it built still said `waiting`,
        # the diff dropped it as unchanged, and nothing else republishes
        # mid-turn. The chat you had just approved therefore sat in `Needs you`
        # behind a card that no longer existed, for the whole rest of the turn.
        release = threading.Event()

        class HoldsThenWorks:
            """One approval, then a turn that keeps running until released."""

            def __init__(self):
                self.turns = 0

            def __call__(self, **kwargs):
                if _is_title_call(kwargs):
                    return model_says("")
                self.turns += 1
                if self.turns == 1:
                    response = model_says(
                        tool_calls=[
                            tool_call("run_command", command=f"touch {tmp_path}/x")
                        ]
                    )
                else:
                    release.wait(timeout=10)
                    response = model_says("done")
                return iter([response]) if kwargs.get("stream") else response

        app = create_app("fake", client_chat=HoldsThenWorks(), **app_env, token=TEST_TOKEN)
        client = TokenClient(app, auto_token=TEST_TOKEN)
        try:
            with client, connected(client) as (ws, hello, _):
                name = hello["session"]
                server = client.app.state.server
                ws.send_json({"type": "task", "text": "run it"})
                request = recv_until(ws, "approval_request")
                recv_until_row(ws, name, "waiting")
                ws.send_json(
                    {"type": "approval", "id": request["id"], "action": "approve"}
                )
                # WHILE THE TURN IS STILL GOING, not when it ends: the model
                # call below is parked on `release`, so a row that only catches
                # up at `finished` has left the list wrong for as long as the
                # work takes.
                for _ in range(300):
                    if server._roster.get(name, {}).get("state") == "running":
                        break
                    time.sleep(0.01)
                assert server._roster.get(name, {}).get("state") == "running"
                release.set()
                # …and it TRAVELLED: this row alone is what clears "Needs
                # approval" from a list on some other device.
                recv_until_row(ws, name, "running")
                recv_until(ws, "done")
        finally:
            release.set()

    def test_a_failed_turn_is_recorded_and_flags_the_chat(self, app_env, tmp_path):
        # A turn that dies used to leave nothing durable: the failure text went
        # out as a live event and was never written, so "why did last night's
        # job fail?" was unanswerable and the chat raised no unread mark for a
        # client that had not been connected (#203).
        class Exploding:
            def __call__(self, **kwargs):
                raise RuntimeError("no route to host")

        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            client.app.state.server.sessions[name].agent.chat = Exploding()
            ws.send_json({"type": "task", "text": "run the nightly job"})
            error = recv_until(ws, "error")
            assert "no route to host" in error["text"]

        records = [
            json.loads(line)
            for line in (app_env["state_dir"] / name).read_text().splitlines()
        ]
        ends = [r for r in records if r.get("kind") == "task_end"]
        assert ends and ends[-1]["status"] == "failed"
        assert "no route to host" in ends[-1]["error"]

        # …and it is OUTPUT, so the chat has something to flag.
        info = SessionLog.info(app_env["state_dir"] / name)
        assert info.output == info.activity, "the failure IS the last thing that happened"

        # Reopened cold, the chat says what happened rather than guessing.
        events = SessionLog.reconstruct_events(app_env["state_dir"] / name)
        errors = [e for e in events if e["type"] == "error"]
        assert errors and "no route to host" in errors[-1]["text"]

    def test_a_published_row_carries_where_the_conversation_left_off(self, app_env):
        # The preview was left off the roster row at first on the theory that it
        # costs a file read (#204). It does not — it derives from the
        # conversation the server is already holding, exactly like the title —
        # and leaving it out meant a row that was fresh about its state and
        # stale about its content until the next full list.
        client, _ = make_client(app_env, [model_says("the answer you wanted")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "ask something"})
            recv_until(ws, "done")
            row = recv_until_row(ws, name, "idle")["row"]
            assert row["snippet"] == "the answer you wanted"
            assert row["title"]  # derived from the same in-memory conversation

    def test_rows_carry_the_output_stamp_apart_from_activity(self, app_env, tmp_path):
        # Unread is decided by OUTPUT, ordering by activity (#203), so a row has
        # to carry both — otherwise the client is back to one number doing two
        # jobs and a chat that is merely thinking marks itself unread.
        responses = [model_says("the answer")]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "ask something"})
            recv_until(ws, "done")
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            row = next(r for r in listing["sessions"] if r["name"] == name)
            assert row["out"] > 0
            assert row["ts"] >= row["out"]

    def test_hello_pager_rows_carry_liveness(self, app_env, tmp_path):
        # The client's attention count re-derives from the rows every hello
        # already carries, so those rows have to say which chats are holding
        # (#203) — otherwise a reload starts the count empty however many chats
        # are waiting, and only opening the rail fixes it.
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello_a, _):
            session_a = hello_a["session"]
            assert hello_a["pager"][0]["state"] == "idle"  # its own, doing nothing yet
            ws.send_json({"type": "task", "text": "run it"})
            recv_until(ws, "approval_request")
            ws.send_json({"type": "new"})
            hello_b = recv_until(ws, "hello")
            states = {p["name"]: p["state"] for p in hello_b["pager"]}
            assert states[session_a] == "waiting"
            assert states[hello_b["session"]] == "idle"

    def test_resume_from_disk_restores_recorded_model(self, app_env, monkeypatch):
        switched = FakeChat([model_says("hi from gemini"), model_says("still gemini")])
        monkeypatch.setattr(
            server_module.backends,
            "make_chat",
            lambda spec: (switched, "gemini", "gemini-3-pro"),
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "set_model", "spec": "gemini:gemini-3-pro"})
            recv_until(ws, "model_changed")
            ws.send_json({"type": "task", "text": "hello there"})
            recv_until(ws, "done")
        # Fresh server instance: nothing in memory, must restore from the log.
        client2, _ = make_client(app_env, [])
        with client2, connected(client2) as (ws, _, _):
            ws.send_json({"type": "resume", "path": name})
            hello2 = recv_until(ws, "hello")
            assert hello2["model"] == "gemini:gemini-3-pro"  # sticky, not reset

    def test_resume_rejects_path_escape(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "resume", "path": "../../../etc/passwd"})
            error = recv_until(ws, "error")
            assert error["type"] == "error"
            assert "no such chat" in error["text"]

    def test_delete_background_session_removes_file_and_list_entry(self, app_env):
        client, _ = make_client(app_env, [model_says("noted")])
        with client, connected(client) as (ws, hello, _):
            first = hello["session"]
            ws.send_json({"type": "task", "text": "remember the zebra"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            recv_until(ws, "hello")

            ws.send_json({"type": "delete_session", "name": first})
            recv_until(ws, "session_deleted")
            listing = recv_until(ws, "session_list")
            assert first not in [s["name"] for s in listing["sessions"]]
            assert not (app_env["state_dir"] / first).exists()

    def test_delete_leaves_sibling_session_untouched(self, app_env):
        # The title-menu "Delete chat" only ever names ONE session; a second
        # real session (its file and its open in-memory entry) must survive.
        client, _ = make_client(app_env, [model_says("a"), model_says("b")])
        with client, connected(client) as (ws, hello, _):
            first = hello["session"]
            ws.send_json({"type": "task", "text": "first topic"})
            recv_until(ws, "done")
            ws.send_json({"type": "new"})
            second = recv_until(ws, "hello")["session"]
            ws.send_json({"type": "task", "text": "second topic"})
            recv_until(ws, "done")

            ws.send_json({"type": "delete_session", "name": first})
            recv_until(ws, "session_deleted")
            listing = recv_until(ws, "session_list")
            names = [s["name"] for s in listing["sessions"]]
            assert first not in names
            assert second in names
            assert not (app_env["state_dir"] / first).exists()
            assert (app_env["state_dir"] / second).is_file()

    def test_delete_active_session_lands_on_new_chat(self, app_env):
        client, _ = make_client(app_env, [model_says("noted")])
        with client, connected(client) as (ws, hello, _):
            first = hello["session"]
            ws.send_json({"type": "task", "text": "remember the zebra"})
            recv_until(ws, "done")
            assert (app_env["state_dir"] / first).is_file()

            ws.send_json({"type": "delete_session", "name": first})
            # Client is moved to a fresh chat BEFORE the delete happens.
            fresh = recv_until(ws, "hello")
            assert fresh["session"] != first
            cleared = recv_until(ws, "replay")
            assert cleared["events"] == []
            recv_until(ws, "session_deleted")
            listing = recv_until(ws, "session_list")
            assert listing["current"] == fresh["session"]
            assert first not in [s["name"] for s in listing["sessions"]]
            assert not (app_env["state_dir"] / first).exists()

    def test_delete_cold_session_straight_from_disk(self, app_env):
        state_dir = app_env["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        old = state_dir / "session-20200101-000000-000000.jsonl"
        old.write_text(
            '{"kind": "message", "role": "user", "content": "old topic"}\n',
            encoding="utf-8",
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "delete_session", "name": old.name})
            recv_until(ws, "session_deleted")
            recv_until(ws, "session_list")
            assert not old.exists()

    def test_delete_running_session_refused(self, app_env, tmp_path):
        marker = tmp_path / "never"
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
                model_says("gave up"),
            ],
        )
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "touch it"})
            request = recv_until(ws, "approval_request")

            ws.send_json({"type": "delete_session", "name": name})
            error = recv_until(ws, "error")
            assert error["type"] == "error"
            assert "still running" in error["text"]
            # The refusal NAMES the chat it refused. Without that the client
            # can only render it as prose and cannot tell this from an
            # unrelated failure — the confusion #210 was reported as.
            assert error["code"] == "refused"
            assert error["name"] == name
            assert (app_env["state_dir"] / name).is_file()

            # The pending approval survived the refused delete untouched.
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")

    def test_a_reopened_chat_lands_on_the_same_workspace(self, app_env):
        """#258 end to end: the chat is rebuilt (eviction, restart, model
        switch) and must come back to the SAME scratch dir. When it did not,
        the model kept writing to the path its history named — the previous
        agent's — and aish raised an approval card for its own throwaway file
        mid-task."""
        client, _ = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "hello"})
            recv_until(ws, "done")

            server = client.app.state.server
            before = server.sessions[name].agent
            staged = before.scratch_dir / "fares.py"
            staged.write_text("x")
            server.sessions.pop(name).close()  # evicted

            session, _history = server.open_session(server.state_dir / name)
            assert session.agent.scratch_dir == before.scratch_dir
            assert staged.read_text() == "x"  # what it staged is still there
            # …and the prompt it is given names the dir the gate will scope to.
            assert str(session.agent.scratch_dir) in session.agent.messages[0]["content"]

    def test_delete_takes_the_chat_scratch_workspace_with_it(self, app_env):
        """#258: the workspace is keyed on the chat's log and outlives every
        agent built behind it, so deleting the chat is the only thing left
        that collects it."""
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            scratch = app_env["state_dir"] / "scratch" / name[: -len(".jsonl")]
            assert scratch.is_dir()
            (scratch / "probe.py").write_text("x")

            ws.send_json({"type": "delete_session", "name": name})
            recv_until(ws, "session_deleted")
            recv_until(ws, "session_list")
            assert not scratch.exists()

    def test_startup_sweeps_workspaces_whose_chat_is_gone(self, app_env):
        """Orphans predate the fix (every rebuilt agent leaked its
        predecessor's dir) and outlive a kill -9. The log is the owner, so a
        workspace with no log is collectable — read once at start, while no
        chat is open to be mistaken for one."""
        state_dir = app_env["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        orphan = state_dir / "scratch" / "session-20200101-000000-000000"
        orphan.mkdir(parents=True)
        (orphan / "leftover.py").write_text("x")
        kept = state_dir / "scratch" / "session-20200102-000000-000000"
        kept.mkdir(parents=True)
        (state_dir / "session-20200102-000000-000000.jsonl").write_text(
            '{"kind": "message", "role": "user", "content": "still here"}\n',
            encoding="utf-8",
        )

        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            assert not orphan.exists()
            assert kept.is_dir()

    def test_delete_rejects_path_escape_and_unknown_names(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            for name in ("../../../etc/passwd", "session-nonexistent.jsonl"):
                ws.send_json({"type": "delete_session", "name": name})
                error = recv_until(ws, "error")
                assert error["type"] == "error"
                assert "no such chat" in error["text"]


class TestSeenLedger:
    """Unread belongs to the OWNER, not to a screen (#232).

    The server half: it merges what a device offers, tells the OTHER devices,
    and answers a connect with the whole ledger. The unit properties are in
    `tests/test_seen.py`; what is pinned here is the wire — that a chat read on
    one socket reaches the other one, and that the stamps every device compares
    are all in the server's clock.
    """

    def test_reading_here_reaches_the_other_device(self, app_env):
        # The whole point. Two sockets are two devices; nothing about this
        # depends on the second one asking.
        client, _ = make_client(app_env, [model_says("hi")])
        with client, connected(client) as (a, hello_a, _):
            with client.websocket_connect("/ws") as b:
                recv_until(b, "hello")
                a.send_json({"type": "seen", "marks": {"chat.jsonl": None}})
                event = recv_until(b, "seen_marked")
                assert set(event["seen"]) == {"chat.jsonl"}
                assert event["seen"]["chat.jsonl"] > 0
            assert hello_a["type"] == "hello"

    def test_the_sender_hears_its_own_mark_too(self, app_env):
        # Deliberately not skipped: the merge is a max, so hearing it back
        # cannot hurt, and remembering who to skip is the bookkeeping that goes
        # wrong the first time a device has two tabs open.
        client, _ = make_client(app_env, [model_says("hi")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "seen", "marks": {"chat.jsonl": None}})
            assert "chat.jsonl" in recv_until(ws, "seen_marked")["seen"]

    def test_a_connect_gets_the_whole_ledger_back(self, app_env):
        # One message, both directions: the device offers what it read while it
        # was away and takes back what the owner read on the other one.
        client, _ = make_client(app_env, [model_says("hi")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "seen", "marks": {"read-here.jsonl": None}})
            recv_until(ws, "seen_marked")
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "seen", "marks": {}, "full": True})
            ledger = recv_until(ws, "seen_ledger")
            assert "read-here.jsonl" in ledger["seen"]
            assert ledger["now"] > 0

    def test_an_unchanged_mark_publishes_nothing(self, app_env):
        # What makes re-offering on every connect free — and therefore what
        # makes a lost broadcast self-healing without a receipt.
        client, _ = make_client(app_env, [model_says("hi")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "seen", "marks": {"chat.jsonl": None}})
            stamp = recv_until(ws, "seen_marked")["seen"]["chat.jsonl"]
            ws.send_json({"type": "seen", "marks": {"chat.jsonl": stamp - 60}, "full": True})
            # The ledger answer arrives; a second seen_marked never does.
            assert recv_until(ws, "seen_ledger")["seen"]["chat.jsonl"] == stamp

    def test_it_survives_a_restart(self, app_env):
        client, _ = make_client(app_env, [model_says("hi")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "seen", "marks": {"chat.jsonl": None}})
            recv_until(ws, "seen_marked")
        # A fresh app over the same state dir is what a launchd restart is.
        again, _ = make_client(app_env, [model_says("hi")])
        with again, connected(again) as (ws, _, _):
            ws.send_json({"type": "seen", "marks": {}, "full": True})
            assert "chat.jsonl" in recv_until(ws, "seen_ledger")["seen"]

    def test_a_garbled_seen_message_is_ignored_not_fatal(self, app_env):
        client, _ = make_client(app_env, [model_says("hi")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "seen"})
            ws.send_json({"type": "seen", "marks": "nonsense"})
            ws.send_json({"type": "seen", "marks": {}, "full": True})
            assert recv_until(ws, "seen_ledger")["seen"] == {}

    def test_hello_carries_the_server_clock(self, app_env):
        # The stamps a device compares — a row's last output, the owner's last
        # look — are all written here now, so a device has to be able to
        # correct its own clock against this one.
        client, _ = make_client(app_env, [model_says("hi")])
        with client, connected(client) as (_, hello, _):
            assert hello["now"] > 0

    def test_a_roster_transition_carries_the_time_it_happened(self, app_env, tmp_path):
        # And it rides the EVENT, never the row: the row is what `_touch`
        # diffs, so a clock inside it would differ every time and suppress
        # nothing — the plane would republish every unchanged row forever.
        responses = [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("done"),
        ]
        client, _ = make_client(app_env, responses)
        with client, connected(client) as (ws, hello, _):
            session = hello["session"]
            ws.send_json({"type": "task", "text": "go"})
            event = recv_until_row(ws, session, "waiting")
            assert event["at"] > 0
            assert "at" not in event["row"]


class TestPeek:
    """`peek` is the swipe-neighbor prefetch: a VIEW message answering another
    session's transcript snapshot WITHOUT switching to it, so a committed
    swipe can paint instantly. It must not move the client's view, must not
    record anything, and a miss answers `gone` on the peek itself — never the
    resume path's error event, whose toast would blame the user for a request
    they never made."""

    @staticmethod
    def _write_session(state_dir, stamp, content="peek target topic"):
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / f"session-20200101-{stamp:06d}-000000.jsonl"
        path.write_text(
            json.dumps({"kind": "message", "role": "user", "content": content}) + "\n"
            + json.dumps({"kind": "message", "role": "assistant", "content": "the answer"})
            + "\n",
            encoding="utf-8",
        )
        return path.name

    def test_peek_returns_events_without_switching_view(self, app_env):
        name = self._write_session(app_env["state_dir"], 1)
        client, _ = make_client(app_env, [model_says("hi there")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "peek", "path": name})
            peek = recv_until(ws, "peek")
            assert peek["name"] == name
            assert peek["events"], "peek must carry the target's transcript"
            texts = json.dumps(peek["events"])
            assert "peek target topic" in texts
            # The client's view did NOT move: a task typed now still runs in
            # the original session (no hello/replay was pushed by the peek).
            ws.send_json({"type": "task", "text": "still here"})
            done = recv_until(ws, "done")
            assert done is not None
            assert hello["session"] != name

    def test_peek_is_idempotent_and_records_nothing(self, app_env):
        name = self._write_session(app_env["state_dir"], 2)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "peek", "path": name})
            first = recv_until(ws, "peek")
            ws.send_json({"type": "peek", "path": name})
            second = recv_until(ws, "peek")
            # Peeking must not grow the peeked transcript (nothing recorded).
            assert len(first["events"]) == len(second["events"])

    def test_peek_of_missing_session_answers_gone_not_error(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "peek", "path": "session-19990101-000000-000000.jsonl"}
            )
            peek = recv_until(ws, "peek")
            assert peek["gone"] is True
            assert peek["name"] == "session-19990101-000000-000000.jsonl"


class TestWarmingIsNotNews:
    """Loading a chat is not a transition (#275).

    Every restart put two chats on the rail with unread dots and a "just now"
    stamp, for chats the owner had already read and that had said nothing since.
    The two chats were the ones the client WARMS on reconnect so a tap paints
    instantly — and warming cold-opens them, which registered them, which
    published a roster row apiece. The roster cache is empty in a fresh process,
    so those first rows always diffed as new; the client, whose only evidence
    of when a chat last spoke was when the row arrived, read each of them as an
    answer it had not seen.

    Both halves are pinned here: a load announces nothing, and a row that IS
    published states when the chat last spoke rather than leaving the client to
    infer it from the delivery."""

    @staticmethod
    def _write_session(state_dir, stamp, spoke_at):
        """A chat whose last word was said at `spoke_at` (a datetime)."""
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / f"session-20200101-{stamp:06d}-000000.jsonl"
        when = spoke_at.isoformat(timespec="seconds")
        path.write_text(
            json.dumps({"ts": when, "kind": "message", "role": "user",
                        "content": "warmed chat topic"}) + "\n"
            + json.dumps({"ts": when, "kind": "message", "role": "assistant",
                          "content": "an answer you already read"}) + "\n",
            encoding="utf-8",
        )
        return path.name

    def test_warming_a_chat_announces_nothing(self, app_env):
        an_hour_ago = datetime.datetime.now() - datetime.timedelta(hours=1)
        name = self._write_session(app_env["state_dir"], 11, an_hour_ago)
        client, _ = make_client(app_env, [model_says("hi there")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "peek", "path": name})
            # A real transition right behind it bounds the drain: everything
            # before the task's own row is everything the warm produced.
            ws.send_json({"type": "task", "text": "meanwhile, over here"})
            announced, warmed = [], False
            for _ in range(300):
                event = ws.receive_json()
                if event["type"] == "session_changed":
                    announced.append(event["row"]["name"])
                elif event["type"] == "peek":
                    warmed = True
                elif event["type"] == "done":
                    break
            assert warmed, "the warm itself must still answer"
            assert name not in announced, "warming a chat announced it as news"
            assert hello["session"] in announced, "a real transition still travels"

    def test_a_warmed_chat_still_announces_its_next_transition(self, app_env):
        # Nothing is seeded into the roster cache by the load, so the first
        # thing that REALLY happens to a warmed chat is not diffed away.
        an_hour_ago = datetime.datetime.now() - datetime.timedelta(hours=1)
        name = self._write_session(app_env["state_dir"], 12, an_hour_ago)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "peek", "path": name})
            recv_until(ws, "peek")
            ws.send_json({"type": "rename_session", "name": name, "title": "Renamed"})
            row = recv_until_row(ws, name, "idle")["row"]
            assert row["title"] == "Renamed"

    def test_a_published_row_says_when_the_chat_last_spoke(self, app_env):
        an_hour_ago = datetime.datetime.now() - datetime.timedelta(hours=1)
        name = self._write_session(app_env["state_dir"], 13, an_hour_ago)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "peek", "path": name})
            recv_until(ws, "peek")
            ws.send_json({"type": "rename_session", "name": name, "title": "Renamed"})
            row = recv_until_row(ws, name, "idle")["row"]
            # Read back from the chat's own log, not from this moment: a rename
            # is not something the chat said, and a row that claimed otherwise
            # is what the client used to mark unread.
            assert row["out"] == pytest.approx(an_hour_ago.timestamp(), abs=2)

    def test_the_stamp_moves_when_the_chat_actually_speaks(self, app_env):
        client, _ = make_client(app_env, [model_says("the answer")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            asked = time.time()
            ws.send_json({"type": "task", "text": "ask something"})
            row = recv_until_row(ws, name, "idle")["row"]
            spoke = row["out"]
            assert spoke >= int(asked), "the answer must move the stamp"
            # …and a rename after it republishes the row without moving it.
            ws.send_json({"type": "rename_session", "name": name, "title": "Named"})
            after = recv_until_row(ws, name, "idle")["row"]
            assert after["title"] == "Named"
            assert after["out"] == spoke, "a rename is not something the chat said"


class TestRename:
    def test_rename_active_session_updates_header_and_list(self, app_env):
        client, _ = make_client(app_env, [model_says("noted")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "the original derived title"})
            recv_until(ws, "done")

            ws.send_json({"type": "rename_session", "name": name, "title": "My Custom Name"})
            renamed = recv_until(ws, "session_renamed")
            assert renamed["name"] == name
            assert renamed["title"] == "My Custom Name"
            listing = recv_until(ws, "session_list")
            row = next(s for s in listing["sessions"] if s["name"] == name)
            assert row["title"] == "My Custom Name"

    def test_latest_rename_wins_across_reconnect(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "first message"})
            recv_until(ws, "done")
            for title in ("one", "two", "three"):
                ws.send_json({"type": "rename_session", "name": name, "title": title})
                recv_until(ws, "session_renamed")
                recv_until(ws, "session_list")

        # A fresh server (nothing in memory) must show the LATEST title on hello.
        client2, _ = make_client(app_env, [])
        with client2, client2.websocket_connect(f"/ws?session={name}") as ws:
            hello2 = ws.receive_json()
            assert hello2["type"] == "hello"
            assert hello2["title"] == "three"

    def test_rename_cold_session_from_disk(self, app_env):
        state_dir = app_env["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        old = state_dir / "session-20200101-000000-000000.jsonl"
        old.write_text(
            '{"kind": "message", "role": "user", "content": "old topic"}\n',
            encoding="utf-8",
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "rename_session", "name": old.name, "title": "Archived"})
            recv_until(ws, "session_renamed")
            listing = recv_until(ws, "session_list")
            row = next(s for s in listing["sessions"] if s["name"] == old.name)
            assert row["title"] == "Archived"
        # The renamed cold session still reconstructs its conversation cleanly.
        messages, _, custom_title, *_ = SessionLog._parse(old)
        assert custom_title == "Archived"
        assert messages == [{"role": "user", "content": "old topic"}]

    def test_rename_rejects_empty_title(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "hi"})
            recv_until(ws, "done")
            ws.send_json({"type": "rename_session", "name": name, "title": "   "})
            error = recv_until(ws, "error")
            assert error["type"] == "error"
            assert "empty" in error["text"]

    def test_rename_rejects_path_escape_and_unknown_names(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            for name in ("../../../etc/passwd", "session-nonexistent.jsonl"):
                ws.send_json({"type": "rename_session", "name": name, "title": "x"})
                # rename stamps control first (a `role` event) before rejecting.
                error = recv_until(ws, "error")
                assert "no such chat" in error["text"]


class TestAutoTitle:
    """#175 — a chat is named after its subject, not after its first prompt.

    The stored name is the same append-only `kind:"title"` record a rename
    writes, so the cold read path (drawer, pager) never makes a model call.
    """

    def _run(self, ws, text="tell me about the Bali eSIM options"):
        ws.send_json({"type": "task", "text": text})
        return recv_until(ws, "done")

    def test_first_answer_names_the_chat_and_persists_it(self, app_env):
        client, chat = make_client(
            app_env, [model_says("Airalo is the one.")], title="Bali eSIM data plans"
        )
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            self._run(ws)
            renamed = recv_until(ws, "session_renamed")
            assert renamed["title"] == "Bali eSIM data plans"
            assert renamed["name"] == name
        parsed = SessionLog._parse(app_env["state_dir"] / name)
        assert parsed.title == "Bali eSIM data plans"
        assert parsed.title_auto is True  # an auto name, replaceable by a later one

    def test_the_titler_sees_the_conversation_not_the_whole_transcript(self, app_env):
        client, chat = make_client(app_env, [model_says("Airalo.")], title="Bali eSIM plans")
        with client, connected(client) as (ws, _, _):
            self._run(ws)
            recv_until(ws, "session_renamed")
        prompt = chat.title_calls[-1]["messages"][0]["content"]
        assert "Bali eSIM" in prompt and "Airalo." in prompt
        assert len(chat.title_calls[-1]["messages"]) == 1  # never the real message list

    def test_a_hand_typed_rename_is_never_overwritten(self, app_env):
        """Even at a backoff slot the chat has plainly drifted to — naming is
        the user's call the moment they make it, permanently."""
        client, chat = make_client(
            app_env,
            [model_says("one"), model_says("two"), model_says("three")],
            title="A Model Chosen Name",
        )
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            self._run(ws)
            recv_until(ws, "session_renamed")
            ws.send_json({"type": "rename_session", "name": name, "title": "My own name"})
            recv_until(ws, "session_renamed")
            calls_after_rename = len(chat.title_calls)
            self._run(ws, "completely unrelated subject now")  # turn 2
            self._run(ws, "postgres index tuning please")  # turn 3 — a slot, drifted
            assert len(chat.title_calls) == calls_after_rename  # never even asked
        parsed = SessionLog._parse(app_env["state_dir"] / name)
        assert parsed.title == "My own name"
        assert parsed.title_auto is False

    def test_a_stopped_turn_is_not_named(self, app_env, tmp_path):
        """A cancel produced no content — naming a chat after it is noise."""
        from aish.agent import CANCELLED_RESULT

        marker = tmp_path / "never"
        client, chat = make_client(
            app_env,
            [model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")])],
            title="Should Not Appear",
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "touch it"})
            recv_until(ws, "approval_request")
            ws.send_json({"type": "stop"})
            assert recv_until(ws, "done")["result"] == CANCELLED_RESULT
        assert not chat.title_calls

    def test_a_fork_earns_its_own_name_at_its_first_turn(self, app_env):
        """The fork copies the parent's log, so without this it wears the
        parent's name forever however far the tangent goes."""
        client, chat = make_client(
            app_env,
            [model_says("zebra"), model_says("giraffes are taller")],
            title="Parent chat name",
        )
        with client, connected(client) as (ws, hello, _):
            source = hello["session"]
            self._run(ws, "remember the zebra")
            recv_until(ws, "session_renamed")
            ws.send_json({"type": "fork"})
            forked = recv_until(ws, "hello")["session"]
            recv_until(ws, "replay")
            assert forked != source
            # The fork's FIRST turn retitles even though it is turn 2 of the
            # copied conversation (no backoff slot, no drift gate).
            chat.title = "Giraffe heights"
            self._run(ws, "what about giraffes?")
            renamed = recv_until(ws, "session_renamed")
            assert renamed["name"] == forked
            assert renamed["title"] == "Giraffe heights"
        assert SessionLog._parse(app_env["state_dir"] / source).title == "Parent chat name"

    def test_an_on_topic_second_turn_costs_no_model_call(self, app_env):
        """Turn 3 is a backoff slot, but a chat still on its subject skips the
        call entirely — the drift gate is free."""
        client, chat = make_client(
            app_env,
            [model_says("a"), model_says("b"), model_says("c")],
            title="Bali eSIM data plans",
        )
        with client, connected(client) as (ws, _, _):
            self._run(ws, "which eSIM for Bali?")
            recv_until(ws, "session_renamed")
            assert len(chat.title_calls) == 1
            self._run(ws, "and the data plans on Bali?")  # turn 2 — not a slot
            self._run(ws, "cheapest Bali eSIM data plans?")  # turn 3 — slot, no drift
            assert len(chat.title_calls) == 1  # still just the first

    def test_a_conversation_that_moves_on_is_renamed(self, app_env):
        client, chat = make_client(
            app_env,
            [model_says("a"), model_says("b"), model_says("c")],
            title="Bali eSIM data plans",
        )
        with client, connected(client) as (ws, _, _):
            self._run(ws, "which eSIM for Bali?")
            recv_until(ws, "session_renamed")
            chat.title = "Postgres index tuning"
            self._run(ws, "different topic entirely now")
            self._run(ws, "explain postgres index tuning please")  # turn 3 — drifted
            renamed = recv_until(ws, "session_renamed")
            assert renamed["title"] == "Postgres index tuning"
        assert len(chat.title_calls) == 2

    def test_a_backend_that_declines_leaves_the_name_alone(self, app_env):
        """Empty/garbage reply, timeout, claude-max — every failure path keeps
        the existing derived title. A name is never worth failing a turn."""
        client, chat = make_client(app_env, [model_says("hi")], title=None)
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            assert self._run(ws, "a question about nothing")["result"] == "hi"
        assert chat.title_calls  # it tried
        assert SessionLog._parse(app_env["state_dir"] / name).title is None


class TestFork:
    def test_fork_seeds_new_session_and_leaves_source_untouched(self, app_env):
        # /fork copies the whole conversation into a NEW session, switches
        # there, replays the prior transcript, and leaves the original intact.
        client, chat = make_client(
            app_env,
            [model_says("the answer is zebra"), model_says("still zebra")],
        )
        with client, connected(client) as (ws, hello, _):
            source = hello["session"]
            ws.send_json({"type": "task", "text": "remember the zebra"})
            recv_until(ws, "done")
            source_bytes = (app_env["state_dir"] / source).read_bytes()

            ws.send_json({"type": "fork"})
            forked = recv_until(ws, "hello")
            assert forked["session"] != source  # a genuinely new session
            replay = recv_until(ws, "replay")
            users = [e for e in replay["events"] if e["type"] == "user"]
            assert users and "zebra" in users[0]["text"]  # history seeded
            assert any(e["type"] == "done" for e in replay["events"])

            # The source file is byte-for-byte unchanged (read-only snapshot).
            assert (app_env["state_dir"] / source).read_bytes() == source_bytes

            # Both sessions are listed; the fork is current.
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            names = [s["name"] for s in listing["sessions"]]
            assert source in names and forked["session"] in names
            assert listing["current"] == forked["session"]

            # Continuing in the fork carries the seeded context to the model.
            ws.send_json({"type": "task", "text": "what animal?"})
            recv_until(ws, "done")
            assert "zebra" in json.dumps(chat.calls[-1]["messages"])

    def test_fork_from_here_truncates_to_that_answer(self, app_env):
        # A per-answer Fork (after=N) branches up to and including that answer,
        # dropping later turns — the "from here" case.
        client, _ = make_client(
            app_env,
            [model_says("first answer alpha"), model_says("second answer beta")],
        )
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "one"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "two"})
            recv_until(ws, "done")

            ws.send_json({"type": "fork", "after": 1})
            forked = recv_until(ws, "hello")
            assert forked["session"] != hello["session"]
            replay = recv_until(ws, "replay")
            users = [e["text"] for e in replay["events"] if e["type"] == "user"]
            dumped = json.dumps(replay["events"])
            assert users == ["one"]  # only the first turn carried over
            assert "alpha" in dumped and "beta" not in dumped

    def test_fork_after_out_of_range_errors(self, app_env):
        client, _ = make_client(app_env, [model_says("only answer")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "hi"})
            recv_until(ws, "done")
            ws.send_json({"type": "fork", "after": 5})
            error = recv_until(ws, "error")
            assert "can't fork from there" in error["text"]

    def test_a_live_answer_names_itself(self, app_env):
        # #229: the id of the record just written rides on `done`, so the Fork
        # button on an answer the owner WATCHED arrive names the same record a
        # replayed one does. Without it a live chat would have no anchor at all
        # until it was reloaded.
        client, _ = make_client(app_env, [model_says("live answer")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "hi"})
            done = recv_until(ws, "done")
            assert done.get("answer"), "a live answer carries its record id"
            replay_id = done["answer"]

            ws.send_json({"type": "fork", "answer": replay_id})
            recv_until(ws, "hello")
            replay = recv_until(ws, "replay")
            assert "live answer" in json.dumps(replay["events"])

    def test_fork_from_here_by_answer_id(self, app_env):
        # The per-answer Fork, addressed by name: branch up to and including
        # that answer, dropping later turns.
        client, _ = make_client(
            app_env,
            [model_says("first answer alpha"), model_says("second answer beta")],
        )
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "one"})
            first = recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "two"})
            recv_until(ws, "done")

            ws.send_json({"type": "fork", "answer": first["answer"]})
            forked = recv_until(ws, "hello")
            assert forked["session"] != hello["session"]
            replay = recv_until(ws, "replay")
            users = [e["text"] for e in replay["events"] if e["type"] == "user"]
            dumped = json.dumps(replay["events"])
            assert users == ["one"]
            assert "alpha" in dumped and "beta" not in dumped

    def test_fork_point_survives_a_partial_transcript(self, app_env, monkeypatch):
        # THE REGRESSION (#229 × #228). The fork used to be an ordinal counted by
        # the browser over what it had RENDERED, and the first paint is capped —
        # so on a chat trimmed to its tail, "the answer I tapped" and "the Nth
        # answer in the log" were different records, and the fork branched from
        # the wrong one with nothing reporting a mismatch.
        #
        # Squeezed here to a two-event window: the replay this client receives
        # cannot contain the first turn at all, and the fork must still land on
        # the answer it names.
        monkeypatch.setattr(server_module, "TRANSCRIPT_MAX", 2)
        monkeypatch.setattr(server_module, "TRANSCRIPT_KEEP", 2)
        client, _ = make_client(
            app_env,
            [model_says("answer alpha"), model_says("answer beta"), model_says("answer gamma")],
        )
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "one"})
            first = recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "two"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "three"})
            recv_until(ws, "done")

            # What a fresh viewer of this chat is given: a trimmed tail.
            ws.send_json({"type": "resume", "path": hello["session"]})
            recv_until(ws, "hello")
            replay = recv_until(ws, "replay")
            assert replay["truncated"], "the view really is short of the whole chat"
            assert "alpha" not in json.dumps(replay["events"])

            # The id was minted when the answer was written, so it still names
            # that record however little of the chat this client holds.
            ws.send_json({"type": "fork", "answer": first["answer"]})
            recv_until(ws, "hello")
            forked = recv_until(ws, "replay")
            dumped = json.dumps(forked["events"])
            assert "alpha" in dumped, "the fork branched from the answer that was named"
            assert "beta" not in dumped and "gamma" not in dumped

    def test_fork_from_an_answer_that_names_nothing_is_refused(self, app_env):
        # A stale id (an answer since deleted, a page from another chat) must be
        # refused, never resolved to "something near there".
        client, _ = make_client(app_env, [model_says("only answer")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "hi"})
            recv_until(ws, "done")
            ws.send_json({"type": "fork", "answer": "no-such-answer"})
            error = recv_until(ws, "error")
            assert "can't fork from there" in error["text"]
            # And nothing was created for a fork that did not happen.
            ws.send_json({"type": "sessions", "query": ""})
            listing = recv_until(ws, "session_list")
            assert listing["current"] == hello["session"]

    def test_fork_carries_the_photos_attached_before_the_cut(self, app_env, tmp_path):
        # The owner reported forks arriving without their attachments; that was
        # the wrong fork POINT (the turns carrying them were behind the cut), and
        # this is the property that had to hold once the point was right.
        photo = app_env["state_dir"] / "uploads" / "IMG_1326.jpeg"
        photo.parent.mkdir(parents=True, exist_ok=True)
        photo.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
        client, _ = make_client(
            app_env, [model_says("a terrace"), model_says("something later")]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "task", "text": "look at this", "attachments": [str(photo)]}
            )
            first = recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "and now something else"})
            recv_until(ws, "done")

            ws.send_json({"type": "fork", "answer": first["answer"]})
            recv_until(ws, "hello")
            replay = recv_until(ws, "replay")
            users = [e["text"] for e in replay["events"] if e["type"] == "user"]
            assert any("IMG_1326.jpeg" in t for t in users), "the attachment came along"


class TestHistoryMore:
    """Reading further back than the first paint reaches (#228).

    The replay is capped at `TRANSCRIPT_KEEP` events because a replay is one
    frame on a socket and a long chat is megabytes. That cap was the whole
    story, though: above it the client showed "… earlier events trimmed …" and
    there was nothing to tap. A 1314-event chat opened at its 815th event, and
    its first two thirds — six of its answers and three of its photos — could
    not be reached from the app at all.
    """

    def _long_chat(self, app_env, monkeypatch, turns=3):
        monkeypatch.setattr(server_module, "TRANSCRIPT_MAX", 2)
        monkeypatch.setattr(server_module, "TRANSCRIPT_KEEP", 2)
        client, _ = make_client(
            app_env, [model_says(f"answer {n}") for n in range(turns)]
        )
        return client

    def test_a_trimmed_replay_says_how_much_it_is_missing(self, app_env, monkeypatch):
        client = self._long_chat(app_env, monkeypatch)
        with client, connected(client) as (ws, hello, _):
            for n in range(3):
                ws.send_json({"type": "task", "text": f"turn {n}"})
                recv_until(ws, "done")
            ws.send_json({"type": "resume", "path": hello["session"]})
            recv_until(ws, "hello")
            replay = recv_until(ws, "replay")
            assert replay["truncated"]
            assert replay["total"] > len(replay["events"]), (
                "the client must be able to say what is missing, not just that "
                "something is"
            )

    def test_asking_for_more_returns_the_whole_chat(self, app_env, monkeypatch):
        client = self._long_chat(app_env, monkeypatch)
        with client, connected(client) as (ws, hello, _):
            for n in range(3):
                ws.send_json({"type": "task", "text": f"turn {n}"})
                recv_until(ws, "done")
            ws.send_json({"type": "resume", "path": hello["session"]})
            recv_until(ws, "hello")
            first = recv_until(ws, "replay")
            assert "turn 0" not in json.dumps(first["events"])

            ws.send_json({"type": "history_more", "window": 500})
            full = recv_until(ws, "replay")
            dumped = json.dumps(full["events"])
            assert "turn 0" in dumped and "answer 0" in dumped
            assert not full["truncated"], "the beginning of the chat is reachable"
            assert full["total"] == len(full["events"])
            # Reading back through a chat you are looking at is not new activity.
            assert full.get("seen") is True

    def test_the_ceiling_is_reported_never_silent(self, app_env, monkeypatch):
        # The reply is a WINDOW, and past the per-request ceiling there is
        # genuinely nothing more to hand over. That must be SAID: a control that
        # keeps offering "N more" and does nothing is the same dead end #228 was,
        # in a friendlier font.
        monkeypatch.setattr(server_module, "TRANSCRIPT_WINDOW_MAX", 3)
        client = self._long_chat(app_env, monkeypatch)
        monkeypatch.setattr(server_module, "TRANSCRIPT_KEEP", 1)
        with client, connected(client) as (ws, _, _):
            for n in range(3):
                ws.send_json({"type": "task", "text": f"turn {n}"})
                recv_until(ws, "done")
            ws.send_json({"type": "history_more", "window": 500})
            reply = recv_until(ws, "replay")
            assert len(reply["events"]) == 3
            assert reply["truncated"] and reply["total"] > 3
            assert reply["more"] is False, "asking again would get no further"

    def test_a_reachable_beginning_says_there_is_more_on_the_way(self, app_env, monkeypatch):
        client = self._long_chat(app_env, monkeypatch)
        with client, connected(client) as (ws, hello, _):
            for n in range(3):
                ws.send_json({"type": "task", "text": f"turn {n}"})
                recv_until(ws, "done")
            ws.send_json({"type": "resume", "path": hello["session"]})
            recv_until(ws, "hello")
            recv_until(ws, "replay")
            ws.send_json({"type": "history_more", "window": 3})
            reply = recv_until(ws, "replay")
            assert reply["truncated"] and reply["more"] is True

    def test_refused_while_the_chat_is_working(self, app_env, tmp_path, monkeypatch):
        # A running turn's tokens are not on disk yet, so rebuilding from the
        # log would paint the answer being written straight out of the view.
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("done"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "history_more", "window": 5000})
            error = recv_until(ws, "error")
            assert "can't load earlier messages" in error["text"]
            assert error["code"] == "refused", "a refusal must not read as a turn failure"
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_reading_further_back_claims_no_control(self, app_env, monkeypatch):
        # A VIEW message: looking at more of a chat is not acting on it, so it
        # must not take the controller role away from another device.
        client = self._long_chat(app_env, monkeypatch)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "turn 0"})
            recv_until(ws, "done")
            ws.send_json({"type": "history_more", "window": 500})
            reply = recv_until(ws, "replay")
            assert reply["events"], "the read still answered"

    def test_fork_empty_conversation_refused(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "fork"})
            # fork stamps control first (a `role` event) before refusing (#102).
            error = recv_until(ws, "error")
            assert "nothing to fork" in error["text"]

    def test_fork_while_busy_refused(self, app_env, tmp_path):
        # A task blocked on an approval keeps the session busy; forking then
        # would snapshot a half-finished turn, so it is refused.
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("done"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")  # now busy, blocked
            ws.send_json({"type": "fork"})
            error = recv_until(ws, "error")
            assert "can't fork while this chat is working" in error["text"]
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")


class TestRetry:
    def test_retry_discards_previous_answer_everywhere(self, app_env):
        # #60: retry re-runs the prompt AND erases the previous attempt from the
        # model's context, the transcript, and the on-disk log — so nothing about
        # the regeneration is anchored to the discarded answer.
        client, chat = make_client(
            app_env,
            [model_says("first wrong answer"), model_says("second clean answer")],
        )
        with client, connected(client) as (ws, hello, _):
            session_name = hello["session"]
            ws.send_json({"type": "task", "text": "what is 2+2?"})
            done1 = recv_until(ws, "done")
            assert done1["result"] == "first wrong answer"

            ws.send_json({"type": "retry", "text": "what is 2+2?"})
            # The transcript is re-sent rolled back: the discarded answer is gone
            # but the user's prompt bubble stays.
            replay = recv_until(ws, "replay")
            dumped = json.dumps(replay["events"])
            assert "first wrong answer" not in dumped
            assert sum(1 for e in replay["events"] if e["type"] == "user") == 1
            assert not any(e["type"] == "done" for e in replay["events"])
            done2 = recv_until(ws, "done")
            assert done2["result"] == "second clean answer"

        # Model context on the rerun: the discarded answer must not be present,
        # and the prompt must be (the rerun really happened, from scratch).
        rerun_messages = json.dumps(chat.calls[1]["messages"])
        assert "first wrong answer" not in rerun_messages
        assert "what is 2+2?" in rerun_messages

        # Persistence: a cold reload must not resurrect the discarded answer.
        client2, _ = make_client(app_env, [])
        with client2, connected(client2) as (ws2, _, _):
            ws2.send_json({"type": "resume", "path": session_name})
            recv_until(ws2, "hello")
            replay = recv_until(ws2, "replay")
            dumped = json.dumps(replay["events"])
            assert "first wrong answer" not in dumped
            assert "second clean answer" in dumped
            assert sum(1 for e in replay["events"] if e["type"] == "user") == 1

    def test_retry_keeps_earlier_turns(self, app_env):
        # Only the LAST turn is rolled back; earlier answers stay in context.
        client, chat = make_client(
            app_env,
            [model_says("alpha"), model_says("bravo"), model_says("charlie")],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "first"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "second"})
            recv_until(ws, "done")

            ws.send_json({"type": "retry", "text": "second"})
            recv_until(ws, "replay")
            done = recv_until(ws, "done")
            assert done["result"] == "charlie"

        rerun_messages = json.dumps(chat.calls[2]["messages"])
        assert "alpha" in rerun_messages  # the first turn's answer survives
        assert "bravo" not in rerun_messages  # the retried turn's answer is gone
        assert "second" in rerun_messages

    def test_retry_while_busy_cancels_then_reruns_clean(self, app_env, tmp_path):
        # Retry fired while a turn is wedged on an approval: cancel first, then
        # roll back and rerun so the discarded (unexecuted) attempt leaves no
        # trace in the model's context.
        marker = tmp_path / "never"
        client, chat = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {marker}")]),
                model_says("clean answer"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            recv_until(ws, "approval_request")  # now busy, blocked on the card
            ws.send_json({"type": "retry", "text": "run it"})
            replay = recv_until(ws, "replay")
            assert not marker.exists()  # the cancelled command never ran
            assert sum(1 for e in replay["events"] if e["type"] == "user") == 1
            done = recv_until(ws, "done")
            assert done["result"] == "clean answer"

        rerun_messages = json.dumps(chat.calls[1]["messages"])
        assert "run it" in rerun_messages
        assert str(marker) not in rerun_messages  # discarded tool call is not replayed


class TestModels:
    def test_model_list_ranked(self, app_env, monkeypatch):
        monkeypatch.setattr(
            server_module,
            "available_models",
            lambda agent, state_dir: [
                ("qwen3:8b", "local · 5 GB"),
                ("gemini", "cloud · default gemini-3-flash"),
            ],
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "models", "query": "gem"})
            listing = recv_until(ws, "model_list")
            assert listing["current"] == "fake"
            assert listing["models"][0]["name"] == "gemini"

    def test_set_model_swaps_backend_and_saves(self, app_env, monkeypatch):
        new_chat = FakeChat([])
        monkeypatch.setattr(
            server_module.backends,
            "make_chat",
            lambda spec: (new_chat, "gemini", "gemini-3-pro"),
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "set_model", "spec": "gemini:gemini-3-pro", "save": True}
            )
            changed = recv_until(ws, "model_changed")
            assert changed["model"] == "gemini:gemini-3-pro"
            assert changed["saved"] is True
            server = client.app.state.server
            assert server.active.agent.chat is new_chat
            assert server.active.agent.provider == "gemini"
            config = app_env["config_path"].read_text(encoding="utf-8")
            assert 'model = "gemini:gemini-3-pro"' in config

    def test_new_chat_inherits_current_model(self, app_env, monkeypatch):
        new_chat = FakeChat([])
        monkeypatch.setattr(
            server_module.backends,
            "make_chat",
            lambda spec: (new_chat, "gemini", "gemini-3-pro"),
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "set_model", "spec": "gemini:gemini-3-pro"})
            recv_until(ws, "model_changed")
            ws.send_json({"type": "new"})
            hello = recv_until(ws, "hello")
            assert hello["model"] == "gemini:gemini-3-pro"  # sticky, not reset

    def test_set_model_claude_max_needs_restart(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "set_model", "spec": "claude-max"})
            # set_model is an action → it stamps control first (a `role` event),
            # so skip to the error rather than reading the raw next frame (#102).
            error = recv_until(ws, "error")
            assert "restart" in error["text"]


class TestWorkspace:
    def test_cd_moves_cwd_and_reanchors_root(self, app_env, tmp_path):
        project = tmp_path / "other-project"
        project.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "cd", "path": str(project)})
            changed = recv_until(ws, "cwd_changed")
            assert changed["cwd"] == str(project)
            assert changed["roots"][0] == str(project)

    def test_add_dir_appends_root(self, app_env, tmp_path):
        extra = tmp_path / "extra"
        extra.mkdir()
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "add_dir", "path": str(extra)})
            changed = recv_until(ws, "cwd_changed")
            assert str(extra) in changed["roots"]

    def test_cd_bad_path_errors(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "cd", "path": "/definitely/not/here"})
            error = recv_until(ws, "error")
            assert error["text"].startswith("ERROR")


class TestFilesAutocomplete:
    def test_file_list_matches_tui_scoring(self, app_env, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "readme.md").write_text("x", encoding="utf-8")
        (tmp_path / "main.py").write_text("x", encoding="utf-8")
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "files", "query": "read"})
            listing = recv_until(ws, "file_list")
            assert listing["query"] == "read"
            assert "docs/readme.md" in listing["files"]
            assert "main.py" not in listing["files"]


class TestUpload:
    def test_upload_saves_and_lands_in_roots(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            response = client.post("/upload?name=notes.txt", content=b"hello upload")
            assert response.status_code == 200
            path = response.json()["path"]
            with open(path, "rb") as fh:
                assert fh.read() == b"hello upload"
            server = client.app.state.server
            assert server.uploads_dir.resolve() in [
                r for r in server.active.agent.roots
            ]

    def test_upload_rejects_bad_names(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            assert client.post("/upload?name=.hidden", content=b"x").status_code == 400
            assert client.post("/upload", content=b"x").status_code == 400
            # Path components are stripped, never traversed.
            response = client.post("/upload?name=../../evil.txt", content=b"x")
            assert response.status_code == 200
            assert response.json()["path"].endswith("uploads/evil.txt")

    def test_attached_image_goes_native_pdf_is_readable_either_way(self, app_env):
        """The test provider is "ollama", which has no document channel — so
        the PDF is NOT delivered natively. It is still announced as readable
        (#219): read_pdf reads the same file on every backend, and only the
        DELIVERY is conditional. Before, a PDF attached to a local model was
        announced as an inert path and the model went hunting for pdftotext."""
        client, chat = make_client(app_env, [model_says("I see it")])
        with client:
            image = client.post("/upload?name=photo.png", content=b"\x89PNG-fake").json()
            pdf = client.post("/upload?name=paper.pdf", content=b"%PDF-fake").json()
            with connected(client) as (ws, _, _):
                ws.send_json(
                    {
                        "type": "task",
                        "text": "what is this?",
                        "attachments": [image["path"], pdf["path"]],
                    }
                )
                user = recv_until(ws, "user")
                # What the OWNER is shown is the record form (#231) — the files
                # by name, no machine prose. This assertion used to read the
                # model's guidance out of this event, which was only possible
                # while one string served both.
                assert user["text"] == "what is this?\n\n![[photo.png]]\n![[paper.pdf]]"
                recv_until(ws, "done")
            sent = [m for m in chat.calls[0]["messages"] if m["role"] == "user"][-1]
            # …and what the MODEL got is the guidance, asserted where it lives.
            assert "you can see it" in sent["content"]  # image went native
            assert f"[document attached: paper.pdf — you can read it; " \
                   f"file at {pdf['path']}]" in sent["content"]
            assert sent.get("images") == [image["path"]]
            assert "documents" not in sent  # not native on this backend

    def test_attachment_outside_uploads_never_goes_native(self, app_env, tmp_path):
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"\x89PNG-private")
        client, chat = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "task", "text": "look", "attachments": [str(secret)]}
            )
            user = recv_until(ws, "user")
            # A file aish does NOT hold keeps its full path in the record (#231):
            # nothing could derive it back from the name alone.
            assert f"![[{secret}]]" in user["text"]
            recv_until(ws, "done")
            sent = [m for m in chat.calls[0]["messages"] if m["role"] == "user"][-1]
            assert f"[attached file: {secret}]" in sent["content"]  # go and open it
            assert "images" not in sent  # nothing base64'd from outside uploads

    def test_upload_requires_token_when_set(self, app_env):
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.post("/upload?name=a.txt", content=b"x").status_code == 403
            assert (
                client.post("/upload?name=a.txt&token=s3cret", content=b"x").status_code
                == 200
            )


class TestShareInbox:
    """POST /share (#213) — the iPhone share sheet's way in.

    iOS cannot register a PWA as a share target, so this is what an iOS
    Shortcut posts to. The property every test here defends is that a share
    is PARKED: it stores a file and announces it, and starts nothing. If a
    change ever makes a share run something, `test_a_share_starts_nothing`
    is the one that should stop it.
    """

    def test_share_parks_a_file_and_announces_it(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            assert hello["shares"] == []  # nothing shared yet
            response = client.post("/share?name=photo.png&source=iPhone", content=b"\x89PNG")
            assert response.status_code == 200
            announced = recv_until(ws, "shared")
            assert len(announced["items"]) == 1
            item = announced["items"][0]
            assert item["name"] == "photo.png"
            assert item["source"] == "iPhone"
            # Stored where an uploaded file goes, so it is already inside a
            # session root and classifies identically.
            server = client.app.state.server
            assert Path(item["path"]).parent == server.uploads_dir
            assert Path(item["path"]).read_bytes() == b"\x89PNG"

    def test_a_share_starts_nothing(self, app_env):
        """The security property: the share sheet stages work, it does not
        become a way for any app on the phone to run an agent."""
        client, chat = make_client(app_env, [model_says("should never run")])
        with client:
            before = list(client.app.state.server.sessions)
            client.post("/share?name=photo.png", content=b"\x89PNG")
            client.post("/share?text=https://example.com/article")
            assert list(client.app.state.server.sessions) == before  # no new session
            assert chat.calls == []  # and no model call

    def test_shared_text_needs_no_file(self, app_env):
        """iOS shares a URL far more often than a file."""
        client, _ = make_client(app_env, [])
        with client:
            response = client.post("/share?text=https://example.com/thing")
            assert response.status_code == 200
            assert response.json()["path"] == ""
            item = client.app.state.server.shares[0]
            assert item["text"] == "https://example.com/thing"
            assert item["name"] == "https://example.com/thing"  # what the chip shows

    def test_chat_new_marks_the_item_not_the_launch_url(self, app_env):
        """iOS will not open an installed web app at an address of your
        choosing — `webapp://…/?new` launches the app and drops the query — so
        "put this in its own chat" has to ride on the ITEM, where the client
        will still find it however the app was opened. Advisory: the server
        opens nothing, it only records the intent."""
        client, chat = make_client(app_env, [model_says("never runs")])
        with client:
            client.post("/share?name=a.png&chat=new", content=b"\x89PNG")
            client.post("/share?name=b.png", content=b"\x89PNG")
            fresh, plain = client.app.state.server.shares
            assert fresh["fresh"] is True
            assert plain["fresh"] is False
            # Still inert: recording the intent must not start anything.
            assert chat.calls == []

    def test_a_body_with_no_name_is_text(self, app_env):
        """Safari shares a URL, and percent-encoding one into a query string
        inside Shortcuts works right up until the link contains an `&`. So the
        body carries it, and `name` is what says whether a body is a file."""
        client, _ = make_client(app_env, [])
        with client:
            link = "https://example.com/x?a=1&b=2"
            response = client.post("/share?source=Safari", content=link.encode())
            assert response.status_code == 200
            assert response.json()["path"] == ""  # nothing was stored as a file
            item = client.app.state.server.shares[0]
            assert item["text"] == link  # the `&` survived intact

    def test_oversized_shared_text_says_it_was_cut(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            client.post("/share", content=b"x" * (server_module.SHARE_TEXT_MAX + 500))
            text = client.app.state.server.shares[0]["text"]
            assert len(text) < server_module.SHARE_TEXT_MAX + 100
            assert text.endswith("… (truncated by aish)")  # never a silent cut

    def test_share_rejects_empty_bad_name_and_bad_token(self, app_env):
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.post("/share?name=a.txt", content=b"x").status_code == 403
            assert client.post("/share?token=s3cret").status_code == 400  # nothing in it
            assert (
                client.post("/share?token=s3cret&name=.hidden", content=b"x").status_code
                == 400
            )
            # A name is data from the network: components are stripped, never walked.
            response = client.post(
                "/share?token=s3cret&name=../../evil.txt", content=b"x"
            )
            assert response.status_code == 200
            assert response.json()["path"].endswith("uploads/evil.txt")

    def test_hello_carries_unclaimed_shares(self, app_env):
        """The normal case is a share arriving with nothing connected, so the
        broadcast alone would only ever reach a tab that happened to be open."""
        client, _ = make_client(app_env, [])
        with client:
            client.post("/share?name=note.txt", content=b"hi")
            with connected(client) as (_, hello, _):
                assert [s["name"] for s in hello["shares"]] == ["note.txt"]

    def test_drop_clears_it_for_every_device(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            client.post("/share?name=note.txt", content=b"hi")
            item = recv_until(ws, "shared")["items"][0]
            with client.websocket_connect(f"/ws?token={TEST_TOKEN}") as other:
                other.receive_json(), other.receive_json()  # hello + replay
                ws.send_json({"type": "share_drop", "id": item["id"]})
                assert recv_until(ws, "shared")["items"] == []
                # The other device's chip has to go too — one inbox, two viewers.
                assert recv_until(other, "shared")["items"] == []
            assert client.app.state.server.shares == []
            # The file stays: a claimed share is now an attachment the composer
            # holds by path, and a dismissed one is just a file in uploads.
            assert Path(item["path"]).exists()

    def test_share_drop_never_claims_control(self, app_env):
        """A VIEW message: it acts on the server's inbox, not on any chat."""
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            client.post("/share?name=note.txt", content=b"hi")
            item = recv_until(ws, "shared")["items"][0]
            session = client.app.state.server.active
            session.controller = None
            ws.send_json({"type": "share_drop", "id": item["id"]})
            recv_until(ws, "shared")
            assert session.controller is None

    def test_inbox_survives_a_restart(self, app_env):
        """aish-web is a launchd job; a share made at lunchtime is claimed in
        the evening, with a restart in between."""
        client, _ = make_client(app_env, [])
        with client:
            client.post("/share?name=note.txt", content=b"hi")
        again, _ = make_client(app_env, [])
        with again, connected(again) as (_, hello, _):
            assert [s["name"] for s in hello["shares"]] == ["note.txt"]

    def test_a_corrupt_inbox_does_not_stop_the_server(self, app_env):
        state = app_env["state_dir"]
        state.mkdir(parents=True, exist_ok=True)
        (state / "shares.json").write_text("{not json at all")
        client, _ = make_client(app_env, [])
        with client, connected(client) as (_, hello, _):
            assert hello["shares"] == []

    def test_old_and_excess_shares_are_pruned(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            server = client.app.state.server
            expired = time.time() - server_module.SHARE_TTL_S - 1
            server.shares = [
                {"id": "ancient", "name": "old.txt", "at": expired},
                {"id": "fresh", "name": "new.txt", "at": time.time()},
            ]
            assert [s["id"] for s in server.shares_snapshot()] == ["fresh"]
            server.shares = [
                {"id": str(i), "name": f"{i}.txt", "at": time.time()}
                for i in range(server_module.SHARE_MAX_ITEMS + 5)
            ]
            server._prune_shares()
            assert len(server.shares) == server_module.SHARE_MAX_ITEMS
            assert server.shares[0]["id"] == "5"  # oldest dropped, newest kept

    def test_a_shared_image_is_attachable_like_any_upload(self, app_env):
        """The point of storing shares in the uploads dir: once claimed, a
        shared photo goes native exactly as a picked one does."""
        client, chat = make_client(app_env, [model_says("I see it")])
        with client:
            shared = client.post("/share?name=photo.png", content=b"\x89PNG-fake").json()
            with connected(client) as (ws, _, _):
                ws.send_json(
                    {"type": "task", "text": "what is this?", "attachments": [shared["path"]]}
                )
                # Indistinguishable from a picked file everywhere downstream:
                # the owner sees the record form, the model gets the guidance.
                assert "![[photo.png]]" in recv_until(ws, "user")["text"]
                recv_until(ws, "done")
            sent = [m for m in chat.calls[0]["messages"] if m["role"] == "user"][-1]
            assert "you can see it" in sent["content"]
            assert sent.get("images") == [shared["path"]]


class TestAttachmentNoteFormat:
    """The two shapes a message uses to say a file came with it (#231).

    The RECORD form is `![[cat.png]]`: what the log keeps, what the owner sees,
    copies and reuses. Python writes it and reads it back; the frontend parses
    it to draw the thumbnail (`[ATTACHMENT-NOTES]` in docs/web-frontend.md) and
    writes it again when you copy a message.

    The GUIDANCE form is the bracketed prose telling the model what it may do
    with each file. Nothing writes it to disk any more, but every message
    written before #231 IS in that shape and always will be — a chat log is
    never rewritten — so both languages must keep reading it forever.

    Both are cross-language contracts with no shared code to enforce them, so
    both are pinned from both sides: here, and in tests/js/test_attachment_notes.js.
    Change one and this fails.
    """

    EMBED = "![[{ref}]]"
    NATIVE_IMAGE = "[image attached: {name} — you can see it; file at {path}]"
    NATIVE_DOC = "[document attached: {name} — you can read it; file at {path}]"
    PLAIN = "[attached file: {path}]"

    def test_guidance_matches_the_shapes_the_frontend_still_parses(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            server = client.app.state.server
            image = Path(client.post("/upload?name=cat.png", content=b"\x89PNG").json()["path"])
            outside = Path(app_env["cwd"]) / "elsewhere.txt"
            outside.write_text("x")
            _, _, guidance = server._classify_attachments(server.active.agent, [
                str(image), str(outside),
            ])
            assert guidance == [
                self.NATIVE_IMAGE.format(name=image.name, path=image),
                self.PLAIN.format(path=outside),
            ]

    def test_the_document_shape_is_pinned_too(self, app_env):
        """The test provider has no pdf support, so this shape is asserted
        against the builder directly rather than through a live turn."""
        assert session_module.attachment_guidance(
            "paper.pdf", "/u/paper.pdf", "document"
        ) == self.NATIVE_DOC.format(name="paper.pdf", path="/u/paper.pdf")

    def test_the_record_form_is_a_name_for_a_file_aish_holds(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            server = client.app.state.server
            image = Path(client.post("/upload?name=cat.png", content=b"\x89PNG").json()["path"])
            _, _, guidance = server._classify_attachments(
                server.active.agent, [str(image)]
            )
            assert session_module.to_record_form(
                "\n".join(guidance), server.uploads_dir
            ) == self.EMBED.format(ref="cat.png")

    def test_the_record_form_keeps_the_path_for_a_file_it_does_not(self, app_env):
        outside = Path(app_env["cwd"]) / "elsewhere.txt"
        outside.write_text("x")
        client, _ = make_client(app_env, [])
        with client:
            server = client.app.state.server
            _, _, guidance = server._classify_attachments(
                server.active.agent, [str(outside)]
            )
            assert session_module.to_record_form(
                "\n".join(guidance), server.uploads_dir
            ) == self.EMBED.format(ref=outside)

    def test_converting_to_the_record_form_is_idempotent(self, app_env):
        """Guidance in, record out; record in, the same record out. This is what
        lets any path that hands text back around — a retry rewinding the
        model's own context is the live one — land on the stored shape instead
        of writing prose into a fresh log line."""
        uploads = Path(app_env["state_dir"]) / "uploads"
        once = session_module.to_record_form(
            "look\n" + self.NATIVE_IMAGE.format(name="cat.png", path=uploads / "cat.png"),
            uploads,
        )
        assert once == "look\n![[cat.png]]"
        assert session_module.to_record_form(once, uploads) == once

    def test_text_that_merely_resembles_an_embed_attaches_nothing(self, tmp_path):
        """The owner writing about wiki-links must not have their words eaten.
        Whole-line matching used to guarantee that; since an embed can sit
        inside a sentence (#233) the guarantee is EXISTENCE instead — `note` is
        not a file, so it is not an attachment and the words are untouched."""
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        typed = "in Obsidian you write ![[note]] inline to embed something"
        assert session_module.real_attachments(typed, uploads) == []
        assert session_module.to_record_form(typed, uploads) == typed
        assert session_module.message_body(typed) == typed


class TestFileEndpoint:
    """GET /file (issue #9): images the model generated render inline in the
    transcript — scoped to the active session's roots, like approval."""

    def test_serves_image_inside_roots(self, app_env, tmp_path):
        chart = tmp_path / "chart.png"
        chart.write_bytes(b"\x89PNG-fake-chart")
        client, _ = make_client(app_env, [])
        with client:
            response = client.get("/file", params={"path": str(chart)})
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.content == b"\x89PNG-fake-chart"

    def test_refuses_paths_outside_roots(self, app_env, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside") / "private.png"
        outside.write_bytes(b"\x89PNG-private")
        client, _ = make_client(app_env, [])
        with client:
            response = client.get("/file", params={"path": str(outside)})
            assert response.status_code == 403

    def test_symlink_escaping_roots_refused(self, app_env, tmp_path, tmp_path_factory):
        secret = tmp_path_factory.mktemp("elsewhere") / "secret.png"
        secret.write_bytes(b"\x89PNG-secret")
        link = tmp_path / "innocent.png"
        link.symlink_to(secret)
        client, _ = make_client(app_env, [])
        with client:
            # Resolved BEFORE the containment check, so the link's real
            # target is what gets scoped.
            response = client.get("/file", params={"path": str(link)})
            assert response.status_code == 403

    def test_only_image_types_served(self, app_env, tmp_path):
        notes = tmp_path / "notes.txt"
        notes.write_text("not an image", encoding="utf-8")
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/file", params={"path": str(notes)}).status_code == 415
            missing = tmp_path / "gone.png"
            assert client.get("/file", params={"path": str(missing)}).status_code == 404
            assert client.get("/file", params={"path": "rel.png"}).status_code == 400
            assert client.get("/file").status_code == 400

    def test_requires_token_when_set(self, app_env, tmp_path):
        chart = tmp_path / "chart.png"
        chart.write_bytes(b"\x89PNG")
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.get("/file", params={"path": str(chart)}).status_code == 403
            ok = client.get("/file", params={"path": str(chart), "token": "s3cret"})
            assert ok.status_code == 200


class TestFrameEndpoint:
    """GET /frame (#289, #318): the picture of a page aish drove.

    A frame rendered through `/file` until #318, because it was stored in the
    media store — which is inside `Agent.workspace_roots`, which is what let the
    MODEL name a frame to `show_image` and read a hostile page's pixels into its
    own context. The bytes moved to a store of their own, outside every root, so
    `/file` cannot serve them any more.

    A record the owner cannot see is not a record (#295 P6), so display did not
    move with them: it got a door of its own, authorised for exactly these bytes
    and nothing else. The two endpoints are deliberately NOT supersets of each
    other — this one cannot serve a project file, and `/file` cannot serve a
    frame."""

    JPEG = b"\xff\xd8\xff\xe0-a-page-aish-drove"

    def _frame(self, app_env, name="ab12cd-browse-eon-pl.jpg"):
        frames = Path(app_env["state_dir"]) / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        path = frames / name
        path.write_bytes(self.JPEG)
        return path

    def test_serves_a_frame_that_no_workspace_root_contains(self, app_env,
                                                            tmp_path):
        """The production shape, spelled out because the fixture's default is
        not it: `app_env` puts the session root at the parent of the state
        directory, which swallows every store aish owns. Here the root is a
        project folder beside it, as it is on a real machine — and then `/file`
        genuinely cannot reach a frame and this endpoint is the only way it is
        seen.

        The MODEL's side does not rest on that arrangement: `_is_evidence_frame`
        asks about the store directly, so a root that swallows the state dir is
        refused anyway. This is about which DOOR the owner's picture comes
        through."""
        project = tmp_path / "project"
        project.mkdir()
        env = {**app_env, "cwd": str(project)}
        frame = self._frame(env)
        client, _ = make_client(env, [])
        with client:
            response = client.get("/frame", params={"path": str(frame)})
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/jpeg"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.content == self.JPEG
            assert client.get("/file", params={"path": str(frame)}).status_code == 403

    def test_refuses_anything_that_is_not_a_frame(self, app_env, tmp_path):
        """Narrower than `/file`, not wider. A second door onto the workspace
        would be a second answer to which files leave this machine."""
        chart = tmp_path / "chart.png"
        chart.write_bytes(b"\x89PNG-fake-chart")
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/frame", params={"path": str(chart)}).status_code == 403
            assert client.get("/file", params={"path": str(chart)}).status_code == 200

    def test_symlink_into_the_store_is_resolved_before_the_check(
        self, app_env, tmp_path
    ):
        """Both directions of #309's one containment function: a link OUT of
        the store is outside it, and a link INTO it is inside."""
        frame = self._frame(app_env)
        secret = tmp_path / "secret.jpg"
        secret.write_bytes(b"\xff\xd8\xff-private")
        escaping = Path(app_env["state_dir"]) / "frames" / "innocent.jpg"
        escaping.symlink_to(secret)
        reaching = tmp_path / "shortcut.jpg"
        reaching.symlink_to(frame)
        client, _ = make_client(app_env, [])
        with client:
            assert client.get(
                "/frame", params={"path": str(escaping)}
            ).status_code == 403
            assert client.get(
                "/frame", params={"path": str(reaching)}
            ).status_code == 200

    def test_the_same_rules_as_file_about_handing_bytes_off_the_machine(
        self, app_env
    ):
        frame = self._frame(app_env)
        notes = Path(app_env["state_dir"]) / "frames" / "notes.txt"
        notes.write_text("not an image", encoding="utf-8")
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.get("/frame", params={"path": str(frame)}).status_code == 403
            ok = {"path": str(frame), "token": "s3cret"}
            assert client.get("/frame", params=ok).status_code == 200
            assert client.get(
                "/frame", params={"path": str(notes), "token": "s3cret"}
            ).status_code == 415
            assert client.get(
                "/frame", params={"path": "rel.jpg", "token": "s3cret"}
            ).status_code == 400
            gone = Path(app_env["state_dir"]) / "frames" / "nope.jpg"
            assert client.get(
                "/frame", params={"path": str(gone), "token": "s3cret"}
            ).status_code == 404

    def test_a_chat_written_before_the_move_still_shows_its_pictures(
        self, app_env
    ):
        """Frames landed in the media store until #318 and the trace rows of
        every chat already on disk still point there. Serving one here grants
        nothing `/file` does not already grant the same token — what #318
        changed is what the MODEL may read — and a fix that blanked the record
        in his existing chats would be taking the record away to protect it."""
        legacy = Path(app_env["state_dir"]) / "media"
        legacy.mkdir(parents=True, exist_ok=True)
        old = legacy / "ab12cd-browse-eon-pl.jpg"
        old.write_bytes(self.JPEG)
        client, _ = make_client(app_env, [])
        with client:
            with connected(client):
                assert client.get(
                    "/frame", params={"path": str(old)}
                ).status_code == 200

    def test_the_save_button_on_a_frame_still_saves(self, app_env):
        """The picture viewer a frame opens into has always had Save, and the
        frame is outside the workspace boundary that `/download` scopes to."""
        frame = self._frame(app_env)
        client, _ = make_client(app_env, [])
        with client:
            response = client.get("/download", params={"path": str(frame)})
            assert response.status_code == 200
            assert response.content == self.JPEG
            assert "attachment" in response.headers["content-disposition"]


class TestFileCaching:
    """An upload is fetched twice: once into the composer chip, once by the
    bubble the send produces. The second must cost nothing.

    Before this, pressing send began a multi-megabyte download of the original
    at the exact moment the owner was watching for a result — measured at
    4.2 MB on the wire AFTER the click, which on a phone over a tunnel is the
    reported "3-5 seconds for the image to show".
    """

    def test_an_upload_may_be_cached_hard(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            path = client.post("/upload?name=photo.png", content=b"\x89PNG").json()["path"]
            response = client.get(f"/file?path={path}")
            assert response.status_code == 200
            cache = response.headers["cache-control"]
            assert "immutable" in cache and "max-age=31536000" in cache
            # One owner's files behind a token check: no shared proxy may hold it.
            assert "private" in cache

    def test_model_output_still_revalidates(self, app_env, tmp_path):
        """The opposite case, and the reason this is not a blanket header: a
        regenerated chart.png at the SAME path is exactly what a long max-age
        would leave stale on screen."""
        client, _ = make_client(app_env, [])
        with client:
            chart = Path(app_env["cwd"]) / "chart.png"
            chart.write_bytes(b"\x89PNG-v1")
            response = client.get(f"/file?path={chart}")
            assert response.status_code == 200
            assert "cache-control" not in response.headers
            assert response.headers.get("etag")  # revalidation, not blind reuse

    def test_the_immutability_claim_is_true(self, app_env):
        """The header is only honest because _store_upload never overwrites."""
        client, _ = make_client(app_env, [])
        with client:
            first = client.post("/upload?name=photo.png", content=b"one").json()["path"]
            second = client.post("/upload?name=photo.png", content=b"two").json()["path"]
            assert first != second
            assert Path(first).read_bytes() == b"one"  # untouched by the second


class TestImageRootsAgreement:
    """#188: /file and the PDF exporter must serve from the SAME set. They did
    not — the exporter trusted the session's scratch workspace and /file did
    not, so an image the model wrote where it is TOLD to write throwaway files
    printed fine in a PDF and 403'd in the chat, with nothing anywhere saying
    why. One method now answers for both."""

    def _session(self, app):
        return app.state.server.active

    def test_serves_from_the_scratch_workspace(self, app_env, tmp_path):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client):
                agent = client.app.state.server.active.agent
                shot = Path(agent.scratch_dir) / "shot.png"
                shot.write_bytes(b"\x89PNG-scratch")
                response = client.get("/file", params={"path": str(shot)})
                assert response.status_code == 200
                assert response.content == b"\x89PNG-scratch"

    def test_serves_from_the_media_store(self, app_env, tmp_path):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client):
                agent = client.app.state.server.active.agent
                agent.media_dir.mkdir(parents=True, exist_ok=True)
                pic = agent.media_dir / "abc123-phone.jpg"
                pic.write_bytes(b"\xff\xd8\xff-media")
                assert client.get("/file", params={"path": str(pic)}).status_code == 200

    def test_both_consumers_read_one_definition(self, app_env, tmp_path):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client):
                server = client.app.state.server
                agent = server.active.agent
                roots = server._workspace_roots()
                for own in (agent.media_dir, agent.scratch_dir):
                    assert any(Path(own).resolve().is_relative_to(r) for r in roots)

    def test_still_refuses_everything_else(self, app_env, tmp_path_factory):
        """Widening to aish's OWN directories must not widen to anyone else's."""
        outside = tmp_path_factory.mktemp("outside") / "private.png"
        outside.write_bytes(b"\x89PNG-private")
        client, _ = make_client(app_env, [])
        with client:
            with connected(client):
                assert client.get("/file", params={"path": str(outside)}).status_code == 403


class TestPdfPreview:
    """GET /pdf/info + /pdf/page (#218): an attached PDF is something the owner
    can LOOK at, not only something aish reads out to them.

    The pages are the pictures — rasterised here, shown in the photo viewer the
    frontend already has — so what is pinned is that the endpoints are scoped
    exactly like /file (they hand bytes off the machine over plain HTTP, which
    is the same act) and that a page which cannot be produced says so instead of
    returning something that renders as nothing.
    """

    def _pdf(self, path: Path, pages: int = 3) -> Path:
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        for number in range(pages):
            page = doc.new_page()
            page.insert_textbox(pymupdf.Rect(60, 60, 500, 300), f"PAGE {number + 1}", fontsize=28)
        doc.save(path)
        doc.close()
        return path

    def test_counts_pages_and_serves_one(self, app_env, tmp_path):
        guide = self._pdf(tmp_path / "guide.pdf")
        client, _ = make_client(app_env, [])
        with client:
            info = client.get("/pdf/info", params={"path": str(guide)})
            assert info.status_code == 200
            assert info.json() == {"name": "guide.pdf", "pages": 3}
            page = client.get("/pdf/page", params={"path": str(guide), "page": "2"})
            assert page.status_code == 200
            assert page.headers["content-type"] == "image/png"
            assert page.headers["x-content-type-options"] == "nosniff"
            assert page.content.startswith(b"\x89PNG")

    def test_scoped_exactly_like_the_image_endpoint(self, app_env, tmp_path, tmp_path_factory):
        """Same boundary, same refusals — a second, looser door onto the disk is
        the failure this shares a helper to prevent."""
        outside = self._pdf(tmp_path_factory.mktemp("outside") / "private.pdf")
        link = tmp_path / "innocent.pdf"
        link.symlink_to(outside)  # resolved BEFORE containment, as /file does
        client, _ = make_client(app_env, [])
        with client:
            for path in (outside, link):
                assert client.get("/pdf/info", params={"path": str(path)}).status_code == 403
                assert client.get("/pdf/page", params={"path": str(path)}).status_code == 403
            assert client.get("/pdf/page", params={"path": "rel.pdf"}).status_code == 400
            assert client.get("/pdf/page").status_code == 400
            missing = tmp_path / "gone.pdf"
            assert client.get("/pdf/page", params={"path": str(missing)}).status_code == 404

    def test_requires_the_token(self, app_env, tmp_path):
        guide = self._pdf(tmp_path / "guide.pdf")
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.get("/pdf/info", params={"path": str(guide)}).status_code == 403
            assert client.get("/pdf/page", params={"path": str(guide)}).status_code == 403
            ok = client.get("/pdf/info", params={"path": str(guide), "token": "s3cret"})
            assert ok.status_code == 200

    def test_a_pdf_that_is_not_a_pdf_is_named(self, app_env, tmp_path):
        """Usually a login wall saved under a .pdf name. PyMuPDF's own error for
        it says nothing a reader could act on, so the magic bytes are checked
        first — the same guard read_pdf applies to a fetched file."""
        fake = tmp_path / "invoice.pdf"
        fake.write_text("<html>sign in to continue</html>", encoding="utf-8")
        notes = tmp_path / "notes.txt"
        notes.write_text("not a document", encoding="utf-8")
        client, _ = make_client(app_env, [])
        with client:
            refused = client.get("/pdf/page", params={"path": str(fake)})
            assert refused.status_code == 415
            assert "%PDF" in refused.json()["error"]
            assert client.get("/pdf/page", params={"path": str(notes)}).status_code == 415

    def test_a_page_that_is_not_there_says_so(self, app_env, tmp_path):
        """A 404 with the reason in it, never a 200 carrying nothing: the client
        shows words, where an empty body would render as a broken-image glyph on
        a black screen."""
        guide = self._pdf(tmp_path / "guide.pdf", pages=2)
        client, _ = make_client(app_env, [])
        with client:
            gone = client.get("/pdf/page", params={"path": str(guide), "page": "9"})
            assert gone.status_code == 404
            assert "9" in gone.json()["error"]
            bad = client.get("/pdf/page", params={"path": str(guide), "page": "last"})
            assert bad.status_code == 400

    def test_pages_of_an_upload_may_be_cached_hard(self, app_env, tmp_path):
        """An upload's bytes cannot change, so its pages cannot either — and a
        swipe back through a document must not re-render what was just read."""
        source = self._pdf(tmp_path / "guide.pdf").read_bytes()
        client, _ = make_client(app_env, [])
        with client:
            stored = client.post("/upload?name=guide.pdf", content=source).json()["path"]
            page = client.get("/pdf/page", params={"path": stored, "page": "1"})
            assert page.status_code == 200
            assert "immutable" in page.headers["cache-control"]
            assert "private" in page.headers["cache-control"]
            etag = page.headers["etag"]
            again = client.get(
                "/pdf/page",
                params={"path": stored, "page": "1"},
                headers={"If-None-Match": etag},
            )
            assert again.status_code == 304
            assert not again.content

    def test_each_page_has_its_own_etag(self, app_env, tmp_path):
        """Or page 2 would revalidate as page 1 and the whole document would
        read as its first page."""
        guide = self._pdf(tmp_path / "guide.pdf")
        client, _ = make_client(app_env, [])
        with client:
            first = client.get("/pdf/page", params={"path": str(guide), "page": "1"})
            second = client.get("/pdf/page", params={"path": str(guide), "page": "2"})
            assert first.headers["etag"] != second.headers["etag"]
            assert first.content != second.content

    def test_nothing_is_added_to_the_media_store(self, app_env, tmp_path):
        """The media store is what the MODEL was shown. A page somebody swiped
        past is not that, and storing every page of every document scrolled
        through would evict real media to hold it."""
        guide = self._pdf(tmp_path / "guide.pdf")
        client, _ = make_client(app_env, [])
        with client:
            with connected(client):
                media = Path(client.app.state.server.active.agent.media_dir)
                before = sorted(p.name for p in media.glob("*")) if media.exists() else []
                assert client.get(
                    "/pdf/page", params={"path": str(guide), "page": "1"}
                ).status_code == 200
                after = sorted(p.name for p in media.glob("*")) if media.exists() else []
                assert after == before


class TestDownloadEndpoint:
    """GET /download (#218 follow-up): looking at an attachment is not having
    it. The rule this pins is the narrow one the endpoint states — you may save
    what aish can already SHOW you (an image, a PDF) and what you ATTACHED
    (anything in the uploads dir) — because the roots also contain a whole
    project tree that no chat has ever displayed.
    """

    def test_saves_an_attachment_under_its_own_name(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            stored = client.post("/upload?name=minutes.txt", content=b"agenda").json()["path"]
            response = client.get("/download", params={"path": stored})
            assert response.status_code == 200
            assert response.content == b"agenda"
            disposition = response.headers["content-disposition"]
            assert disposition.startswith("attachment;")
            assert 'filename="minutes.txt"' in disposition
            # nosniff + attachment is what keeps an uploaded .html a download
            # rather than same-origin markup the browser would run.
            assert response.headers["x-content-type-options"] == "nosniff"

    def test_a_non_ascii_name_survives(self):
        """Both forms, because they answer different browsers: `filename*`
        carries the real name, and the quoted ASCII fallback REPLACES rather
        than drops — a name reduced to "----" tells you nothing about which file
        you just saved. The ASCII pass is also what stops a quote or a newline
        in a filename from ending the header early."""
        header = server_module._attachment_disposition("Zażółć.pdf")
        assert header.startswith('attachment; filename="Za____.pdf";')
        assert header.endswith("filename*=UTF-8''Za%C5%BC%C3%B3%C5%82%C4%87.pdf")
        injected = server_module._attachment_disposition('we"ird\nname.txt')
        assert '"we_ird_name.txt"' in injected
        assert "\n" not in injected and injected.count('"') == 2

    def test_serves_what_the_chat_can_already_show(self, app_env, tmp_path):
        client, _ = make_client(app_env, [])
        with client:
            chart = Path(app_env["cwd"]) / "chart.png"
            chart.write_bytes(b"\x89PNG-chart")
            shown = client.get("/download", params={"path": str(chart)})
            assert shown.status_code == 200
            assert shown.content == b"\x89PNG-chart"
            assert shown.headers["content-type"] == "image/png"

    def test_serves_what_the_owner_sent_aish_to_fetch(self, app_env, monkeypatch):
        """The third half of the rule (#237). He asked aish to press the button
        that produced this file, through his own signed-in session — a document
        only aish can open is aish keeping it. A PDF was already servable by
        type; this is what makes the rest of a portal's downloads reachable."""
        downloads = Path(app_env["cwd"]) / "browser" / "downloads"
        downloads.mkdir(parents=True)
        monkeypatch.setattr(browser_module, "downloads_dir", lambda: downloads)
        archive = downloads / "rozliczenie.zip"
        archive.write_bytes(b"PK\x03\x04zip")
        client, _ = make_client(app_env, [])
        with client:
            got = client.get("/download", params={"path": str(archive)})
            assert got.status_code == 200
            assert got.content == b"PK\x03\x04zip"
            assert 'filename="rozliczenie.zip"' in got.headers["content-disposition"]

    def test_the_downloads_folder_is_not_a_hole_in_the_root_scope(
        self, app_env, monkeypatch, tmp_path_factory
    ):
        """It widens WHICH KINDS of file may be saved, never WHERE from: the
        root check runs first and is untouched."""
        downloads = Path(app_env["cwd"]) / "browser" / "downloads"
        downloads.mkdir(parents=True)
        monkeypatch.setattr(browser_module, "downloads_dir", lambda: downloads)
        outside = tmp_path_factory.mktemp("elsewhere") / "downloads"
        outside.mkdir()
        secret = outside / "notes.zip"
        secret.write_bytes(b"PK\x03\x04")
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/download", params={"path": str(secret)}).status_code == 403

    def test_refuses_a_file_no_chat_ever_displayed(self, app_env):
        """The roots hold a project tree. `/file` has always answered 415 for a
        .env or a .py, and a download must not be the looser door beside it."""
        client, _ = make_client(app_env, [])
        with client:
            secret = Path(app_env["cwd"]) / ".env"
            secret.write_text("API_KEY=hunter2", encoding="utf-8")
            refused = client.get("/download", params={"path": str(secret)})
            assert refused.status_code == 415
            source = Path(app_env["cwd"]) / "main.py"
            source.write_text("print('hi')", encoding="utf-8")
            assert client.get("/download", params={"path": str(source)}).status_code == 415

    def test_scoped_and_gated_exactly_like_the_image_endpoint(
        self, app_env, tmp_path, tmp_path_factory
    ):
        outside = tmp_path_factory.mktemp("outside") / "private.png"
        outside.write_bytes(b"\x89PNG-private")
        link = tmp_path / "innocent.png"
        link.symlink_to(outside)
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/download", params={"path": str(outside)}).status_code == 403
            assert client.get("/download", params={"path": str(link)}).status_code == 403
            assert client.get("/download", params={"path": "rel.png"}).status_code == 400
            assert client.get("/download").status_code == 400
            missing = Path(app_env["cwd"]) / "gone.png"
            assert client.get("/download", params={"path": str(missing)}).status_code == 404

    def test_requires_the_token(self, app_env):
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            stored = client.post(
                "/upload?name=notes.txt&token=s3cret", content=b"x"
            ).json()["path"]
            assert client.get("/download", params={"path": stored}).status_code == 403
            ok = client.get("/download", params={"path": stored, "token": "s3cret"})
            assert ok.status_code == 200


class TestRenderErrorReports:
    """#188 layer 2: the browser is the only place that knows an image did not
    render, and it used to keep that to itself — the model's only feedback
    channel was the user typing "images don't show"."""

    def _log_steps(self, app_env):
        state = Path(app_env["state_dir"])
        steps = []
        for path in state.glob("session-*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("kind") == "trace":
                    steps.append(record["step"])
        return steps

    def test_a_live_failure_is_logged_and_told_to_the_model(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({
                    "type": "render_error", "what": "image", "live": True,
                    "items": ["https://images.example.com/phone.jpg"],
                })
                ws.send_json({"type": "jobs"})  # round-trip: the report was processed
                recv_until(ws, "job_list")
                agent = client.app.state.server.active.agent
        notes = [
            m for m in agent.messages
            if m.get("role") == "user" and "did not display" in (m.get("content") or "")
        ]
        assert len(notes) == 1
        assert "images.example.com/phone.jpg" in notes[0]["content"]
        assert "show_image" in notes[0]["content"]
        logged = [s for s in self._log_steps(app_env) if s.get("kind") == "render_error"]
        assert logged and logged[0]["items"] == ["https://images.example.com/phone.jpg"]

    def test_the_note_is_a_synthetic_annotation_not_a_transcript_turn(self, app_env):
        """It must not show as a blue user bubble on replay, and it must not
        become the chat's title (#171 classifies by text)."""
        from aish.session import synthetic_kind

        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({
                    "type": "render_error", "live": True, "items": ["/tmp/x.png"],
                })
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
                agent = client.app.state.server.active.agent
        note = [m for m in agent.messages if "did not display" in (m.get("content") or "")][0]
        assert synthetic_kind(note["content"]) == "note"

    def test_a_replayed_failure_is_logged_but_injects_nothing(self, app_env):
        """Opening an old chat whose pictures were evicted must not grow it a
        turn — that is an unread conversation talking to itself."""
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({
                    "type": "render_error", "live": False, "items": ["/gone/old.png"],
                })
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
                agent = client.app.state.server.active.agent
        assert not [m for m in agent.messages if "did not display" in (m.get("content") or "")]
        logged = [s for s in self._log_steps(app_env) if s.get("kind") == "render_error"]
        assert logged and logged[0]["items"] == ["/gone/old.png"]

    def test_a_repeat_of_the_same_failure_is_recorded_once(self, app_env):
        """The ledger wants to know THAT an image did not render, not that it
        still hasn't on the tenth look (#201). Every duplicate was a log write,
        and a log write is what made merely reading a chat mark it unread."""
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                for _ in range(5):
                    ws.send_json({
                        "type": "render_error", "live": False,
                        "items": ["https://m.example.com/product.jpg"],
                    })
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
        logged = [s for s in self._log_steps(app_env) if s.get("kind") == "render_error"]
        assert len(logged) == 1

    def test_a_genuinely_new_failure_after_a_repeat_is_recorded(self, app_env):
        """Deduping must not silence the next real thing — only the repeat."""
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                for items in (["/a.png"], ["/a.png"], ["/b.png"], ["/a.png"]):
                    ws.send_json({"type": "render_error", "live": False, "items": items})
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
        logged = [s for s in self._log_steps(app_env) if s.get("kind") == "render_error"]
        assert [s["items"] for s in logged] == [["/a.png"], ["/b.png"], ["/a.png"]]

    def test_a_report_does_not_make_the_chat_look_newly_active(self, app_env):
        """The whole point (#201). The row's `ts` is what the client compares
        against its own "seen" stamp, so if reporting a dead image moves it, a
        chat holding one is unread forever and reading it is what re-arms it."""
        client, _ = make_client(app_env, [model_says("here are three mugs")])
        with client, connected(client) as (ws, hello, _):
            here = hello["session"]
            ws.send_json({"type": "task", "text": "find me a mug"})
            recv_until(ws, "done")
            ws.send_json({"type": "sessions", "query": ""})
            before = {s["name"]: s["ts"] for s in recv_until(ws, "session_list")["sessions"]}

            time.sleep(1.1)  # log stamps are whole seconds
            ws.send_json({
                "type": "render_error", "live": False,
                "items": ["https://m.example.com/product.jpg"],
            })
            ws.send_json({"type": "jobs"})
            recv_until(ws, "job_list")

            ws.send_json({"type": "sessions", "query": ""})
            after = {s["name"]: s["ts"] for s in recv_until(ws, "session_list")["sessions"]}

        assert before[here] == after[here], "a glance must not read as new activity"
        state = Path(app_env["state_dir"])
        path = next(p for p in state.glob("session-*.jsonl") if p.name == here)
        assert path.stat().st_mtime > after[here], "the FILE did change — only the row must not"

    def test_the_report_never_widens_egress_provenance(self, app_env):
        """The src is a string the MODEL chose, echoed by the browser. Feeding it
        through the owner-authored path would let an injected image URL vouch for
        its own host on a later egress card (#178 P0-2)."""
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({
                    "type": "render_error", "live": True,
                    "items": ["https://attacker.example/x.png"],
                })
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
                agent = client.app.state.server.active.agent
        assert "attacker.example" not in agent._owner_hosts

    def test_bounded_and_control_stripped(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({
                    "type": "render_error", "live": True,
                    "items": [f"/a{i}.png" for i in range(50)] + ["x\n\rY" + "z" * 500, 7, ""],
                })
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
        logged = [s for s in self._log_steps(app_env) if s.get("kind") == "render_error"][0]
        assert len(logged["items"]) <= server_module.RENDER_ERROR_MAX
        for item in logged["items"]:
            assert len(item) <= server_module.RENDER_ERROR_SRC_CHARS
            assert "\n" not in item and "\r" not in item

    def test_a_junk_report_is_ignored(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, _, _):
                for message in (
                    {"type": "render_error"},
                    {"type": "render_error", "items": "not-a-list"},
                    {"type": "render_error", "items": []},
                    {"type": "render_error", "items": ["   "]},
                ):
                    ws.send_json(message)
                ws.send_json({"type": "jobs"})
                recv_until(ws, "job_list")
        assert not [s for s in self._log_steps(app_env) if s.get("kind") == "render_error"]


class TestDirListing:
    def make_tree(self, tmp_path):
        base = tmp_path / "tree"
        for d in ("alpha", "beta/nested", "beta/.hidden", ".git/objects", "projects/aish"):
            (base / d).mkdir(parents=True)
        (base / "file.txt").write_text("not a dir", encoding="utf-8")
        return base

    def test_dirs_lists_folders_and_files(self, app_env, tmp_path):
        base = self.make_tree(tmp_path)
        client, _ = make_client(app_env, [])
        with client:
            body = client.get(f"/dirs?path={base}").json()
            assert body["path"] == str(base)
            # Folders list with items=None (no per-subfolder count — that extra
            # scandir could block in-kernel and freeze the server; #86). Noise
            # dirs like .git are filtered server-side (#87).
            assert body["dirs"] == [
                {"name": "alpha", "items": None},
                {"name": "beta", "items": None},
                {"name": "projects", "items": None},
            ]
            assert body["files"] == ["file.txt"]
            assert body["truncated"] is False

    def test_dirs_filters_noise_dirs(self, app_env, tmp_path):
        base = tmp_path / "proj"
        for d in ("src", "node_modules", ".git", "venv", "__pycache__"):
            (base / d).mkdir(parents=True)
        (base / ".DS_Store").write_text("", encoding="utf-8")
        (base / "main.py").write_text("", encoding="utf-8")
        client, _ = make_client(app_env, [])
        with client:
            body = client.get(f"/dirs?path={base}").json()
            assert [d["name"] for d in body["dirs"]] == ["src"]  # noise dirs gone
            assert body["files"] == ["main.py"]  # .DS_Store filtered

    def test_dirs_filters_glob_egg_info(self, app_env, tmp_path):
        # fnmatch globbing in the default ignore list (#87).
        base = tmp_path / "proj"
        for d in ("keep", "aish.egg-info"):
            (base / d).mkdir(parents=True)
        client, _ = make_client(app_env, [])
        with client:
            body = client.get(f"/dirs?path={base}").json()
            assert [d["name"] for d in body["dirs"]] == ["keep"]

    def test_dirs_honors_config_ignore_list(self, app_env, tmp_path):
        # A user-edited [directory_picker] ignore list is the source of truth:
        # names it lists are hidden, and defaults it omits are NOT (#87).
        app_env["config_path"].write_text(
            '[directory_picker]\nignore = ["secret", "*.bak"]\n', encoding="utf-8"
        )
        base = tmp_path / "proj"
        for d in ("src", "secret", "node_modules"):
            (base / d).mkdir(parents=True)
        (base / "old.bak").write_text("", encoding="utf-8")
        (base / "keep.txt").write_text("", encoding="utf-8")
        client, _ = make_client(app_env, [])
        with client:
            body = client.get(f"/dirs?path={base}").json()
            # "secret" hidden by config; "node_modules" now shown (not in the
            # user's list); "*.bak" file hidden by the glob.
            assert [d["name"] for d in body["dirs"]] == ["node_modules", "src"]
            assert body["files"] == ["keep.txt"]

    def test_dirs_listing_timeout_returns_504(self, app_env, tmp_path, monkeypatch):
        """A hung listing (blocking scandir/stat on a TCC-gated or networked
        path) is killed and returns 504 rather than freezing the server — the
        reason the listing runs out of process (#86)."""
        import aish.server as server_module

        monkeypatch.setattr(server_module.WebServer, "_DIRS_TIMEOUT_S", 0.3)
        monkeypatch.setattr(
            server_module.WebServer, "_DIRS_LIST_SCRIPT", "import time\ntime.sleep(30)\n"
        )
        base = self.make_tree(tmp_path)
        client, _ = make_client(app_env, [])
        with client:
            assert client.get(f"/dirs?path={base}").status_code == 504

    def test_dirs_requires_token_when_set(self, app_env, tmp_path):
        base = self.make_tree(tmp_path)
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.get(f"/dirs?path={base}").status_code == 403
            assert client.get(f"/dirs?path={base}&token=s3cret").status_code == 200

    def test_dirs_rejects_bad_paths(self, app_env, tmp_path):
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/dirs?path=relative/path").status_code == 400
            assert client.get(f"/dirs?path={tmp_path}/nope").status_code == 404
            assert client.get(f"/dirs?path={tmp_path}/tree/file.txt").status_code == 404


class TestTokenGate:
    def test_wrong_token_rejected_right_token_accepted(self, app_env):
        from starlette.websockets import WebSocketDisconnect

        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            # Accepted then closed with the app code — browsers only expose
            # close codes for accepted sockets, and the client needs 4403 to
            # show "wrong token" instead of looping on "reconnecting…".
            with client.websocket_connect("/ws?token=wrong") as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4403
            with connected(client, "/ws?token=s3cret") as (_ws, hello, _):
                assert hello["model"] == "fake"


class TestOriginGate:
    """#178 P1-2: WebSockets are exempt from the same-origin policy and a
    text/plain POST needs no preflight, so a browser Origin that isn't our own
    Host is rejected BEFORE the token is even considered. A missing Origin
    (curl, the launchd poller, native clients) passes — browsers always send
    one on the cross-origin requests that are the drive-by vector."""

    def test_cross_origin_ws_rejected_even_with_valid_token(self, app_env):
        from starlette.websockets import WebSocketDisconnect

        client, _ = make_client(app_env, [])
        with client:
            # TokenClient appends the VALID token — the origin alone rejects.
            with client.websocket_connect(
                "/ws", headers={"Origin": "https://evil.example"}
            ) as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4405

    def test_same_origin_ws_accepted(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            # TestClient's Host header is "testserver".
            with client.websocket_connect(
                "/ws", headers={"Origin": "http://testserver"}
            ) as ws:
                assert ws.receive_json()["type"] == "hello"

    def test_missing_origin_accepted(self, app_env):
        # curl / the poller / native clients send no Origin header at all.
        client, _ = make_client(app_env, [])
        with client, connected(client) as (_ws, hello, _):
            assert hello["type"] == "hello"

    def test_trigger_cross_origin_rejected(self, app_env):
        client, _ = make_client(app_env, [model_says("x")], token="secret")
        with client:
            r = client.post(
                "/trigger?token=secret",
                json={"prompt": "hi", "origin": "email"},
                headers={"Origin": "https://evil.example"},
            )
            assert r.status_code == 403

    def test_origin_matching_rules(self):
        from aish.server import origin_allowed

        # The reverse-proxy case that must keep working (aish.wenda.eu).
        assert origin_allowed("https://aish.wenda.eu", "aish.wenda.eu")
        assert origin_allowed("https://aish.wenda.eu:443", "aish.wenda.eu")
        assert origin_allowed("http://192.168.10.20:8787", "192.168.10.20:8787")
        assert origin_allowed(None, "aish.wenda.eu")
        assert origin_allowed("", "aish.wenda.eu")
        assert not origin_allowed("null", "aish.wenda.eu")
        assert not origin_allowed("https://evil.example", "aish.wenda.eu")
        assert not origin_allowed("https://aish.wenda.eu.evil.example", "aish.wenda.eu")
        assert not origin_allowed("file://x", "aish.wenda.eu")
        # A Host that fails the header-injection sanity check gates closed.
        assert not origin_allowed("https://aish.wenda.eu", "bad host\r\nX: y")


class TestUnconditionalToken:
    """#178 P1-2: there is no token-less mode. An app built without a token
    generates a random one at startup and every gated surface requires it."""

    def test_no_token_app_still_gates_everything(self, app_env):
        from starlette.websockets import WebSocketDisconnect

        chat = FakeChat([model_says("ok")])
        app = create_app("fake", client_chat=chat, **app_env)  # no token given
        generated = app.state.server.token
        assert generated  # generated, held in memory
        client = TestClient(app)  # deliberately NOT TokenClient: no auto-append
        with client:
            assert client.post("/upload?name=a.txt", content=b"x").status_code == 403
            assert client.get("/offline/index").status_code == 403
            assert client.get("/dirs?path=/tmp").status_code == 403
            assert client.post("/export/answer", content=b"# x").status_code == 403
            # /trigger: the old loopback fallback is gone — token or nothing.
            assert client.post("/trigger", json={"prompt": "x"}).status_code == 403
            with client.websocket_connect("/ws") as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4403
            # The generated token is the way in.
            with connected(client, f"/ws?token={generated}") as (_ws, hello, _):
                assert hello["type"] == "hello"
            r = client.post(
                f"/trigger?token={generated}", json={"prompt": "go", "origin": "email"}
            )
            assert r.status_code == 200


class TestConcurrentColdOpen:
    """#178 P1-5: two callers racing a cold open of the same name (restart
    recovery vs a reconnecting PWA) must share ONE Session — the duplicate
    used to overwrite the first in `sessions`, orphaning a live worker whose
    approval cards could never be answered."""

    def test_racing_cold_opens_share_one_session(self, app_env):
        client, _ = make_client(app_env, [])
        server = client.app.state.server
        # A session on disk but NOT in memory: write a log file directly.
        state_dir = app_env["state_dir"]
        log = SessionLog.new(state_dir)
        log.message({"role": "user", "content": "hello from disk"})
        log.close()
        name = log.path.name
        assert name not in server.sessions

        opens = {"n": 0}
        real_open = server.open_session

        def counted(*args, **kwargs):
            opens["n"] += 1
            return real_open(*args, **kwargs)

        server.open_session = counted

        async def race():
            return await asyncio.gather(
                server._open_by_name(name), server._open_by_name(name)
            )

        a, b = asyncio.run(race())
        assert a is not None
        assert a is b
        assert server.sessions[name] is a
        assert opens["n"] == 1  # one build, shared — not two racing ones

    def test_failed_open_does_not_poison_the_name(self, app_env):
        client, _ = make_client(app_env, [])
        server = client.app.state.server
        state_dir = app_env["state_dir"]
        log = SessionLog.new(state_dir)
        log.message({"role": "user", "content": "hi"})
        log.close()
        name = log.path.name

        real_open = server.open_session
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("disk hiccup")
            return real_open(*args, **kwargs)

        server.open_session = flaky

        async def open_twice():
            with pytest.raises(RuntimeError):
                await server._open_by_name(name)
            assert name not in server._opening  # entry removed on failure
            return await server._open_by_name(name)

        session = asyncio.run(open_twice())
        assert session is not None
        assert server.sessions[name] is session


class TestSkillsRefresh:
    def test_skill_added_after_boot_is_advertised(self, app_env, project_scope):
        """Issue #31: the skills index is rebuilt per task, not captured at
        create_app time — a skill created while the server runs reaches the
        model on the next task without a restart."""
        client, chat = make_client(app_env, [model_says("ok")])
        skills_dir = Path(app_env["cwd"]) / ".aish" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "late.md").write_text(
            "---\nname: late\ndescription: Use when testing hot reload\n---\nbody"
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "anything"})
            recv_until(ws, "done")
        system = chat.calls[0]["messages"][0]
        assert system["role"] == "system"
        assert "- late: Use when testing hot reload" in system["content"]


class TestLearnCommand:
    def test_learn_text_is_rewritten_to_prompt(self, app_env):
        client, chat = make_client(app_env, [model_says("saved nothing")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/learn"})
            assert recv_until(ws, "user")["text"] == "/learn"  # transcript keeps the typed form
            recv_until(ws, "done")
        sent_user = [m for m in chat.calls[0]["messages"] if m["role"] == "user"]
        assert "durable learnings" in sent_user[-1]["content"]

    def test_other_slash_text_goes_through_verbatim(self, app_env):
        client, chat = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/etc/hosts looks odd"})
            recv_until(ws, "done")
        sent_user = [m for m in chat.calls[0]["messages"] if m["role"] == "user"]
        assert sent_user[-1]["content"] == "/etc/hosts looks odd"

    def test_feedback_text_is_rewritten_to_flow_prompt(self, app_env):
        client, chat = make_client(app_env, [model_says("drafted")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback dark mode is broken"})
            assert recv_until(ws, "user")["text"] == "/feedback dark mode is broken"
            recv_until(ws, "done")
        sent_user = [m for m in chat.calls[0]["messages"] if m["role"] == "user"]
        assert "GitHub issue" in sent_user[-1]["content"]
        assert "dark mode is broken" in sent_user[-1]["content"]


class TestExportAssembly:
    """Issue #64: the pure markdown-assembly boundary — 'final answers only'
    is a structural rule, tested here without touching a PDF."""

    def test_session_answers_excludes_thinking_and_tool_steps(self):
        from aish.export import session_answers

        messages = [
            {"role": "user", "content": "do a thing"},
            # a working turn that narrated before calling a tool: it IS followed
            # by a tool result, so it is not a final answer.
            {"role": "assistant", "content": "let me check the files first"},
            {"role": "tool", "tool_name": "run_command", "content": "file1 file2"},
            # the real answer to the first question
            {"role": "assistant", "content": "There are two files."},
            {"role": "user", "content": "and now?"},
            {"role": "assistant", "content": ""},  # empty turn — dropped
            {"role": "assistant", "content": "All done — nothing else to do."},
        ]
        answers = session_answers(messages)
        assert answers == ["There are two files.", "All done — nothing else to do."]
        assert not any("check the files" in a for a in answers)  # working step gone
        assert not any("file1 file2" in a for a in answers)  # tool output gone

    def test_assemble_session_markdown_separates_answers(self):
        from aish.export import assemble_session_markdown

        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "answer one"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "answer two"},
        ]
        doc = assemble_session_markdown(messages, "T")
        assert "answer one" in doc and "answer two" in doc
        assert "---" in doc  # a horizontal rule separates them

    def test_render_answer_pdf_is_valid_pdf(self):
        from aish.export import render_answer_pdf

        data = render_answer_pdf("# Hi\n\nSome **markdown** — with an arrow →.", "t")
        assert data.startswith(b"%PDF")
        assert len(data) > 400

    def test_pdf_embeds_unicode_font_for_polish(self):
        # Regression: the PDF built-in fonts (Helvetica/Courier) have no Polish
        # glyphs and render them as black boxes. The bundled Source Sans 3 /
        # Source Code Pro fonts must be embedded so ą/ć/ę/ł/… actually draw.
        from aish.export import render_answer_pdf

        data = render_answer_pdf("Zażółć gęślą jaźń — → `ąęść`", "t")
        assert data.startswith(b"%PDF")
        assert b"SourceSans3" in data  # embedded body font
        assert b"SourceCodePro" in data  # embedded code font

    def test_export_strips_web_only_bits(self):
        # Quick-reply chips, the [no-chips] tag, and emoji variation selectors
        # are web-only / presentational — they must not reach the PDF markdown.
        from aish.export import _strip_web_only

        out = _strip_web_only(
            "Answer text.\n\n[Yes](aish-reply://Yes) [No](aish-reply://No)\n[no-chips]\n"
            "Heart ❤️ done."
        )
        assert "aish-reply" not in out
        assert "no-chips" not in out.lower()
        assert "️" not in out  # variation selector stripped
        assert "Answer text." in out and "Heart ❤ done." in out

    def test_export_wraps_emoji_and_embeds_emoji_font(self):
        # reportlab can't render colour emoji; the bundled Noto Emoji outline
        # font is embedded and emoji runs are wrapped to select it.
        from aish.export import _wrap_emoji, render_answer_pdf

        wrapped = _wrap_emoji("Ship it \U0001F680 now")
        assert 'font-family: aishEmoji' in wrapped and "\U0001F680" in wrapped
        # a symbol Source Sans already has is NOT rerouted to the emoji font
        assert _wrap_emoji("arrow → here") == "arrow → here"

        data = render_answer_pdf("Launch \U0001F680 and celebrate \U0001F389", "t")
        assert data.startswith(b"%PDF")
        assert b"NotoEmoji" in data

    def test_export_wraps_long_code_to_page(self):
        # A very long unbreakable line in a code block must not error and the
        # page CSS carries the CJK wrap that fits it to the page width.
        from aish.export import _PAGE_CSS, render_answer_pdf

        assert "-pdf-word-wrap: CJK" in _PAGE_CSS
        data = render_answer_pdf("```\n" + ("x" * 400) + "\n```\n", "t")
        assert data.startswith(b"%PDF")

    def test_safe_pdf_filename_slugs_and_defaults(self):
        from aish.export import safe_pdf_filename

        assert safe_pdf_filename("rename all/the photos!") == "rename-all-the-photos.pdf"
        assert safe_pdf_filename("") == "aish-export.pdf"
        assert safe_pdf_filename("   ", "fb") == "fb.pdf"

    def test_safe_pdf_filename_transliterates_non_ascii(self):
        # Non-ASCII letters must transliterate to ASCII, not be stripped to a
        # run of dashes. ł/Ł is the load-bearing case (it doesn't decompose
        # under NFKD), so it needs the explicit map.
        from aish.export import safe_pdf_filename

        name = safe_pdf_filename("Zażółć gęślą jaźń")
        data = name.removesuffix(".pdf")
        assert data.isascii()
        assert "----" not in data  # letters weren't stripped into dash runs
        assert "Zazolc" in data
        assert "jazn" in data
        assert name == "Zazolc-gesla-jazn.pdf"


def _png_bytes(width: int = 8, height: int = 8) -> bytes:
    """A tiny real PNG (Pillow is a hard dep of xhtml2pdf, so it is always
    present in the test environment)."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 90, 200)).save(buffer, "PNG")
    return buffer.getvalue()


class TestExportMedia:
    """Issue #133: pictures, maps, and video thumbnails embedded into the PDF.
    All network is faked by monkeypatching export.fetch_image — the tests
    assert on the HTML-rewrite boundary (_MediaEmbedder) plus one end-to-end
    PDF render per shape."""

    def _process(self, markdown_text, roots=()):
        import aish.export as export

        return export._markdown_to_html_fragment(
            markdown_text, export._MediaEmbedder(list(roots))
        )

    # ---- local images -----------------------------------------------------

    def test_local_image_inside_root_is_inlined(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(_png_bytes())
        html = self._process(f"![my shot]({tmp_path}/shot.png)", [tmp_path])
        assert "data:image/png;base64," in html
        assert 'alt="my shot"' in html

    def test_local_image_outside_roots_is_never_read(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        secret = tmp_path / "secret.png"
        secret.write_bytes(_png_bytes())
        html = self._process(f"![leak]({secret})", [root])
        assert "data:image" not in html
        assert "aish-link-card" in html  # captioned link card instead
        assert str(secret) in html

    def test_dotdot_traversal_is_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        secret = tmp_path / "secret.png"
        secret.write_bytes(_png_bytes())
        html = self._process(f"![leak]({root}/../secret.png)", [root])
        assert "data:image" not in html
        assert "aish-link-card" in html

    def test_symlink_escape_is_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        secret = tmp_path / "secret.png"
        secret.write_bytes(_png_bytes())
        link = root / "inside.png"
        link.symlink_to(secret)  # inside the root, but resolves outside
        html = self._process(f"![leak]({link})", [root])
        assert "data:image" not in html
        assert "aish-link-card" in html

    def test_relative_path_is_not_resolved(self, tmp_path):
        (tmp_path / "rel.png").write_bytes(_png_bytes())
        html = self._process("![r](rel.png)", [tmp_path])
        assert "data:image" not in html
        assert "aish-link-card" in html

    def test_non_image_local_file_becomes_card(self, tmp_path):
        (tmp_path / "notes.png").write_text("not an image at all")
        html = self._process(f"![n]({tmp_path}/notes.png)", [tmp_path])
        assert "data:image" not in html
        assert "aish-link-card" in html

    # ---- remote images ----------------------------------------------------

    def test_remote_image_is_fetched_and_inlined(self, monkeypatch):
        import aish.export as export

        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _png_bytes()

        monkeypatch.setattr(export, "fetch_image", fake_fetch)
        html = self._process("![pic](https://example.com/pic.png)")
        assert fetched == ["https://example.com/pic.png"]
        assert "data:image/png;base64," in html

    def test_remote_image_fetch_failure_falls_back_to_card(self, monkeypatch):
        import aish.export as export

        monkeypatch.setattr(export, "fetch_image", lambda url: None)
        html = self._process("![pic](https://example.com/gone.png)")
        assert "data:image" not in html
        assert "aish-link-card" in html
        assert "https://example.com/gone.png" in html

    def test_remote_fetch_budget_is_bounded(self, monkeypatch):
        import aish.export as export

        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _png_bytes()

        monkeypatch.setattr(export, "fetch_image", fake_fetch)
        links = "\n\n".join(
            f"![i{n}](https://example.com/{n}.png)"
            for n in range(export.MAX_REMOTE_FETCHES + 5)
        )
        self._process(links)
        assert len(fetched) == export.MAX_REMOTE_FETCHES

    # ---- YouTube thumbnails -----------------------------------------------

    def test_youtube_link_becomes_thumbnail_card(self, monkeypatch):
        import aish.export as export

        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _png_bytes()

        monkeypatch.setattr(export, "fetch_image", fake_fetch)
        html = self._process("Watch [Demo](https://youtu.be/dQw4w9WgXcQ) now")
        assert fetched == ["https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg"]
        assert "data:image/png;base64," in html
        assert "YouTube video" in html
        assert 'href="https://youtu.be/dQw4w9WgXcQ"' in html  # card links to the video

    def test_youtube_thumbnail_failure_falls_back_to_card(self, monkeypatch):
        import aish.export as export

        monkeypatch.setattr(export, "fetch_image", lambda url: None)
        html = self._process(
            "[Demo](https://www.youtube.com/watch?v=dQw4w9WgXcQ)"
        )
        assert "data:image" not in html
        assert "aish-link-card" in html
        assert "YouTube video" in html

    def test_plain_link_is_untouched(self, monkeypatch):
        import aish.export as export

        monkeypatch.setattr(
            export, "fetch_image", lambda url: pytest.fail("must not fetch")
        )
        html = self._process("[docs](https://example.com/docs)")
        assert '<a href="https://example.com/docs">docs</a>' in html

    # ---- Google Maps snapshots --------------------------------------------

    def test_map_link_without_api_key_is_a_link_card(self, monkeypatch):
        import aish.export as export

        monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
        monkeypatch.setattr(
            export, "fetch_image", lambda url: pytest.fail("must not fetch without a key")
        )
        html = self._process(
            "[Office](https://www.google.com/maps/search/?api=1&query=Central+Park)"
        )
        assert "aish-link-card" in html
        assert "map" in html

    def test_map_link_with_api_key_fetches_static_map(self, monkeypatch):
        import aish.export as export

        monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "K123")
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _png_bytes()

        monkeypatch.setattr(export, "fetch_image", fake_fetch)
        html = self._process(
            "[Office](https://www.google.com/maps/search/?api=1&query=Central+Park)"
        )
        assert len(fetched) == 1
        assert fetched[0].startswith("https://maps.googleapis.com/maps/api/staticmap?")
        assert "markers=Central+Park" in fetched[0]
        assert "key=K123" in fetched[0]
        assert "data:image/png;base64," in html

    def test_directions_link_maps_both_endpoints(self, monkeypatch):
        import aish.export as export

        monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "K123")
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return _png_bytes()

        monkeypatch.setattr(export, "fetch_image", fake_fetch)
        self._process("[Route](https://maps.google.com/maps?saddr=Kraków&daddr=Warszawa)")
        assert len(fetched) == 1
        assert "label%3AA%7CKrak" in fetched[0] or "label:A" in fetched[0]

    # ---- end to end -------------------------------------------------------

    def test_render_answer_pdf_with_local_image(self, tmp_path):
        from aish.export import render_answer_pdf

        (tmp_path / "shot.png").write_bytes(_png_bytes(600, 200))
        markdown = f"# Report\n\n![shot]({tmp_path}/shot.png)\n"
        with_image = render_answer_pdf(markdown, "t", [tmp_path])
        without_scope = render_answer_pdf(markdown, "t", [])
        assert with_image.startswith(b"%PDF")
        assert without_scope.startswith(b"%PDF")
        assert len(with_image) > len(without_scope)  # the image bytes made it in

    def test_export_answer_endpoint_inlines_session_root_image(self, app_env):
        cwd = Path(app_env["cwd"])
        (cwd / "shot.png").write_bytes(_png_bytes())
        client, _ = make_client(app_env, [])
        with client:
            response = client.post(
                "/export/answer?title=pic",
                content=f"![shot]({cwd}/shot.png)".encode(),
            )
            assert response.status_code == 200
            assert response.content.startswith(b"%PDF")


class TestExportEndpoints:
    def test_export_answer_returns_pdf_attachment(self, app_env):
        """With no usable backend the title falls back to the answer's own lead
        — here its heading — and names the download."""
        client, _ = make_client(app_env, [])
        with client:
            response = client.post(
                "/export/answer",
                content="# Answer\n\nBody text — with unicode →.".encode(),
            )
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/pdf"
            assert 'attachment; filename="Answer.pdf"' in (
                response.headers["content-disposition"]
            )
            assert response.content.startswith(b"%PDF")

    def test_export_answer_title_is_written_by_the_answers_own_model(self, app_env):
        """The prompt names the request, not the document (#172): the session's
        OWN model titles its answer, and that title names the file."""
        client, chat = make_client(app_env, [], title="Bali eSIM data plans")
        with client:
            with connected(client) as (ws, hello, _):
                name = hello["session"]
            response = client.post(
                f"/export/answer?session={name}",
                content=b"You are correct. Airalo is a good choice on Bali.",
            )
            assert response.status_code == 200
            assert 'filename="Bali-eSIM-data-plans.pdf"' in (
                response.headers["content-disposition"]
            )
            # One extra call, tool-free and non-streaming, and it never touched
            # the conversation — a title must not become a turn in the log.
            titling = chat.title_calls[-1]
            assert not titling["tools"] and not titling.get("stream")
            assert len(titling["messages"]) == 1  # a bare prompt, not the conversation
            log = app_env["state_dir"] / name
            if log.exists():
                assert "Bali eSIM data plans" not in log.read_text(encoding="utf-8")

    def test_export_answer_title_falls_back_when_the_model_rambles(self, app_env):
        """A chatty or failed reply must not name the file — the deterministic
        lead takes over."""
        rambling = model_says("Sure! " + "Here is some prose about your document. " * 6)
        client, _ = make_client(app_env, [rambling])
        with client:
            with connected(client) as (ws, hello, _):
                name = hello["session"]
            response = client.post(
                f"/export/answer?session={name}",
                content=b"# Quarterly report\n\nBody.",
            )
            assert 'filename="Quarterly-report.pdf"' in (
                response.headers["content-disposition"]
            )

    def test_export_answer_rejects_empty_body(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            assert client.post("/export/answer", content=b"").status_code == 400

    def test_export_session_returns_final_answers_only(self, app_env):
        # A task that calls a tool (auto-approved `ls`) then answers: the log
        # then holds a tool step whose text must NOT reach the exported PDF.
        responses = [
            model_says(tool_calls=[tool_call("run_command", command="ls")]),
            model_says("The exported final answer."),
        ]
        client, _ = make_client(app_env, responses)
        with client:
            with connected(client) as (ws, hello, _):
                name = hello["session"]
                ws.send_json({"type": "task", "text": "list and answer"})
                recv_until(ws, "done")
            response = client.get(f"/export/session?session={name}")
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/pdf"
            assert response.content.startswith(b"%PDF")
            assert "attachment" in response.headers["content-disposition"]

            # The pure assembly over the same log proves the tool step is gone.
            from aish.export import session_answers
            from aish.session import SessionLog

            messages = SessionLog.load_messages(app_env["state_dir"] / name)
            answers = session_answers(messages)
            assert answers == ["The exported final answer."]

    def test_export_session_unknown_name_404(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/export/session?session=nope").status_code == 404
            assert (
                client.get("/export/session?session=../../etc/passwd").status_code == 404
            )

    def test_export_endpoints_require_token_when_set(self, app_env):
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.post("/export/answer", content=b"x").status_code == 403
            assert (
                client.post("/export/answer?token=s3cret", content=b"# x").status_code
                == 200
            )
            assert client.get("/export/session?session=x").status_code == 403


ISSUE_BLOCK = (
    "Here is your draft:\n\n"
    "```aish-issue\n"
    "title: Dark mode toggle is broken\n"
    "---\n"
    "The toggle does nothing on tap.\n\n"
    "### Steps\n"
    "- open settings\n"
    "- tap the toggle\n\n"
    "label: bug\n"
    "```\n"
)


class TestIssueBlockParsing:
    """The aish-issue block is the single source of truth (#110): parsed once in
    the backend, mirrored in app.js. Title/body must come out exactly."""

    def test_parses_title_and_body_with_separator(self):
        issue, cleaned = server_module.parse_issue_block(ISSUE_BLOCK)
        assert issue == {
            "title": "Dark mode toggle is broken",
            "body": "The toggle does nothing on tap.\n\n"
            "### Steps\n- open settings\n- tap the toggle\n\nlabel: bug",
        }
        # The raw fence is stripped from the stored answer (b): replay/export
        # never show the fenced source, only the surrounding prose survives.
        assert "```aish-issue" not in cleaned
        assert cleaned == "Here is your draft:"

    def test_optional_separator_absent_body_starts_line_two(self):
        text = "```aish-issue\ntitle: A title\nBody line one.\nBody line two.\n```"
        issue, _ = server_module.parse_issue_block(text)
        assert issue == {"title": "A title", "body": "Body line one.\nBody line two."}

    def test_body_may_itself_contain_a_separator_line(self):
        # A --- deeper in the body is a real horizontal rule, not the optional
        # leading separator, so it must be preserved verbatim.
        text = (
            "```aish-issue\n"
            "title: Has a rule\n"
            "---\n"
            "Intro paragraph.\n"
            "---\n"
            "After the rule.\n"
            "```"
        )
        issue, _ = server_module.parse_issue_block(text)
        assert issue["title"] == "Has a rule"
        assert issue["body"] == "Intro paragraph.\n---\nAfter the rule."

    def test_no_block_returns_none_and_unchanged_text(self):
        text = "Just a normal answer with no issue block."
        assert server_module.parse_issue_block(text) == (None, text)


class TestIssueCreation:
    """Backend-owned creation (#110): confirm files the pre-reviewed draft as a
    user-direct action — no model, no approval gate, repo pinned, safe argv."""

    @staticmethod
    def _fake_run_command(captured):
        def fake(command, **kwargs):
            captured.append(command)
            on_line = kwargs.get("on_line")
            if on_line:
                on_line("https://github.com/epnasis/aish/issues/999")
            return "https://github.com/epnasis/aish/issues/999\n[exit code: 0]"

        return fake

    def test_text_feedback_stashes_block_and_strips_from_answer(self, app_env):
        # A text-only /feedback draft: the block is stashed and stripped from the
        # streamed/stored answer (b); no gh call happens during the task.
        client, chat = make_client(app_env, [model_says(ISSUE_BLOCK)])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback dark mode is broken"})
            done = recv_until(ws, "done")
            assert "```aish-issue" not in done["result"]
            # The model was told to EMIT a block, not to run gh issue create.
            user_prompt = next(
                m["content"] for m in reversed(chat.calls[0]["messages"])
                if m["role"] == "user"
            )
            assert "aish-issue" in user_prompt
            assert "Do NOT run `gh issue create`" in user_prompt

    def test_create_issue_files_reviewed_draft_via_user_direct_path(
        self, app_env, monkeypatch
    ):
        captured: list[str] = []
        monkeypatch.setattr(
            "aish.tools.run_command", self._fake_run_command(captured)
        )
        client, _ = make_client(app_env, [model_says(ISSUE_BLOCK)])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback dark mode is broken"})
            recv_until(ws, "done")  # draft stashed
            ws.send_json({"type": "create_issue"})
            # The user-direct path streams into a terminal block — no approval.
            start = recv_until(ws, "command_start")
            assert start.get("user") is True
            recv_until(ws, "done")
        assert len(captured) == 1
        argv = shlex.split(captured[0])
        # Repo hard-pinned; title/body are the EXACT reviewed text, safely quoted.
        assert argv[:5] == ["gh", "issue", "create", "--repo", "epnasis/aish"]
        assert argv[argv.index("--title") + 1] == "Dark mode toggle is broken"
        body = argv[argv.index("--body") + 1]
        assert body.startswith("The toggle does nothing on tap.")
        assert "label: bug" in body

    def test_create_issue_confirmation_carries_clickable_link(
        self, app_env, monkeypatch
    ):
        # gh prints the new issue's URL to stdout; the confirmation surfaces it as
        # a clickable markdown link, not plain terminal text (#110 follow-up).
        captured: list[str] = []
        monkeypatch.setattr(
            "aish.tools.run_command", self._fake_run_command(captured)
        )
        client, _ = make_client(app_env, [model_says(ISSUE_BLOCK)])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback dark mode is broken"})
            recv_until(ws, "done")
            ws.send_json({"type": "create_issue"})
            done = recv_until(ws, "done")  # the filing confirmation
        assert "[#999](https://github.com/epnasis/aish/issues/999)" in done["result"]

    def test_create_issue_clears_pending_so_a_retap_cannot_double_file(
        self, app_env, monkeypatch
    ):
        captured: list[str] = []
        monkeypatch.setattr(
            "aish.tools.run_command", self._fake_run_command(captured)
        )
        client, _ = make_client(app_env, [model_says(ISSUE_BLOCK)])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback broken"})
            recv_until(ws, "done")
            ws.send_json({"type": "create_issue"})
            recv_until(ws, "done")
            # Second tap: the draft was consumed, so it errors instead of re-filing.
            ws.send_json({"type": "create_issue"})
            err = recv_until(ws, "error")
            assert "no issue draft" in err["text"]
        assert len(captured) == 1  # filed exactly once

    def test_create_issue_without_a_draft_errors_gracefully(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "create_issue"})
            err = recv_until(ws, "error")
            assert "no issue draft" in err["text"]

    def test_feedback_with_attachments_keeps_gated_model_flow(self, app_env):
        # Attachments → the classic model-driven flow: the model is told to run
        # gh issue create itself (approval-gated) with the asset workflow. No
        # backend block flow, so no pending_issue is stashed.
        client, chat = make_client(app_env, [model_says("Here is the draft…")])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {
                    "type": "task",
                    "text": "/feedback see the log",
                    "attachments": ["/tmp/does-not-exist.log"],
                }
            )
            recv_until(ws, "done")
            user_prompt = next(
                m["content"] for m in reversed(chat.calls[0]["messages"])
                if m["role"] == "user"
            )
            assert "gh issue create" in user_prompt
            assert "asset workflow" in user_prompt
            assert "aish-issue" not in user_prompt
            # #130: consent — the draft lists the assets with per-file exclude
            # chips before anything is uploaded to the public release.
            assert "aish-reply://Exclude <name> from the issue" in user_prompt
            assert "PUBLIC GitHub release" in user_prompt
            # No draft was stashed, so a create_issue tap errors.
            ws.send_json({"type": "create_issue"})
            err = recv_until(ws, "error")
            assert "no issue draft" in err["text"]


class TestFeedbackAttachmentSwitch:
    """#130: attachments in the /feedback adjust loop. A text-only draft being
    adjusted auto-switches to the classic upload flow when attachments arrive,
    and uploads are consented — the draft lists the assets with per-file
    exclude chips before anything lands on the public release."""

    @staticmethod
    def _last_user_prompt(chat, call: int) -> str:
        return next(
            m["content"] for m in reversed(chat.calls[call]["messages"])
            if m["role"] == "user"
        )

    def test_adjust_turn_attachment_switches_block_to_classic(self, app_env):
        client, chat = make_client(
            app_env, [model_says(ISSUE_BLOCK), model_says("Updated draft…")]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback dark mode is broken"})
            recv_until(ws, "done")  # block draft stashed
            ws.send_json(
                {
                    "type": "task",
                    "text": "here is a screenshot",
                    "attachments": ["/tmp/shot.png"],
                }
            )
            # The switch note is model-only: the user echo stays clean.
            echo = recv_until(ws, "user")
            assert "SWITCH" not in echo["text"]
            assert "![[/tmp/shot.png]]" in echo["text"]  # record form (#231)
            recv_until(ws, "done")
            prompt = self._last_user_prompt(chat, 1)
            # The attachment was detected and the model re-anchored on the
            # classic flow, with the consent listing (confirm/deselect).
            assert "[attached file: /tmp/shot.png]" in prompt
            assert "SWITCH to the classic flow" in prompt
            assert "aish-reply://Create the issue" in prompt
            assert "aish-reply://Exclude <name> from the issue" in prompt
            assert "PUBLIC GitHub release" in prompt
            # The stale block draft was withdrawn: a Create tap can't file it.
            ws.send_json({"type": "create_issue"})
            err = recv_until(ws, "error")
            assert "no issue draft" in err["text"]

    def test_exclude_reply_passes_through_without_a_second_switch(self, app_env):
        # The deselect chip's reply is an ordinary adjust turn: no attachments
        # and the flow already switched, so the model receives it verbatim (no
        # duplicate switch note) and re-drafts without the excluded file.
        client, chat = make_client(
            app_env,
            [
                model_says(ISSUE_BLOCK),
                model_says("Draft listing shot.png"),
                model_says("Draft without shot.png"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback broken"})
            recv_until(ws, "done")
            ws.send_json(
                {"type": "task", "text": "screenshot", "attachments": ["/tmp/shot.png"]}
            )
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "Exclude shot.png from the issue"})
            recv_until(ws, "done")
            assert self._last_user_prompt(chat, 2) == "Exclude shot.png from the issue"

    def test_textonly_adjust_turn_stays_on_block_flow(self, app_env):
        # No attachments → no switch: the refinement loop keeps the fast
        # backend-owned block flow and the re-emitted draft stays filable.
        client, chat = make_client(
            app_env, [model_says(ISSUE_BLOCK), model_says(ISSUE_BLOCK)]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback broken"})
            recv_until(ws, "done")
            ws.send_json(
                {"type": "task", "text": "I'd like to change the draft: mention iOS"}
            )
            recv_until(ws, "done")
            prompt = self._last_user_prompt(chat, 1)
            assert "SWITCH" not in prompt
            assert prompt == "I'd like to change the draft: mention iOS"

    def test_attachment_outside_feedback_does_not_switch(self, app_env):
        # An attachment in a session with no feedback in progress is a plain
        # attachment — never a flow switch.
        client, chat = make_client(app_env, [model_says("looked at it")])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "task", "text": "what is this?", "attachments": ["/tmp/x.log"]}
            )
            recv_until(ws, "done")
            assert "SWITCH" not in self._last_user_prompt(chat, 0)

    def test_filing_the_issue_closes_the_switch_window(self, app_env, monkeypatch):
        # Once the draft is filed the adjust loop is over: a later attachment
        # in the same session must not drag the model back into feedback.
        monkeypatch.setattr(
            "aish.tools.run_command", TestIssueCreation._fake_run_command([])
        )
        client, chat = make_client(
            app_env, [model_says(ISSUE_BLOCK), model_says("looked at it")]
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "/feedback broken"})
            recv_until(ws, "done")
            ws.send_json({"type": "create_issue"})
            recv_until(ws, "done")
            ws.send_json(
                {"type": "task", "text": "unrelated", "attachments": ["/tmp/x.log"]}
            )
            recv_until(ws, "done")
            assert "SWITCH" not in self._last_user_prompt(chat, 1)


class TestToolApproval:
    """Mutating plugin tools reuse the command card verbatim over the WS."""

    @pytest.fixture(autouse=True)
    def _opt_in(self, project_scope):
        """Tools are planted in the session cwd's .aish — explicit opt-in (#178 P0-1)."""

    def _write_tool(self, cwd, name, marker, mutating="yes"):
        import stat
        from pathlib import Path

        tdir = Path(cwd) / ".aish" / "tools" / name
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "TOOL.md").write_text(
            f"---\nname: {name}\ndescription: writer tool\nexec: ./run.sh\n"
            f'mutating: {mutating}\n'
            'returns: text\nschema: {"text": {"type": "string"}}\n---\nb\n'
        )
        p = tdir / "run.sh"
        p.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def responses(self):
        return [
            model_says(tool_calls=[tool_call("writer", text="hi")]),
            model_says("finished"),
        ]

    def test_tool_approve_runs(self, app_env, tmp_path):
        marker = tmp_path / "toolran"
        self._write_tool(app_env["cwd"], "writer", marker)
        client, _ = make_client(app_env, self.responses())
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run tool"})
            request = recv_until(ws, "approval_request")
            assert request["kind"] == "tool"
            assert request["tool"] == "writer"
            assert request["args"] == {"text": "hi"}
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            assert recv_until(ws, "approval_resolved")["decision"] == "approved"
            recv_until(ws, "done")
            assert marker.exists()

    def test_tool_deny_never_runs(self, app_env, tmp_path):
        marker = tmp_path / "toolpwned"
        self._write_tool(app_env["cwd"], "writer", marker)
        client, chat = make_client(app_env, self.responses())
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run tool"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")
            assert not marker.exists()
            assert tool_results(chat)[-1]["content"] == DENIED_RESULT

    def _write_preview_tool(self, cwd, name="pv"):
        import stat
        from pathlib import Path

        tdir = Path(cwd) / ".aish" / "tools" / name
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "TOOL.md").write_text(
            f"---\nname: {name}\ndescription: d\nexec: ./run.sh\nmutating: yes\n"
            "returns: text\npreview: yes\n"
            f'schema: {{"id": {{"type": "string"}}}}\n---\nb\n'
        )
        p = tdir / "run.sh"
        p.write_text(
            "#!/bin/sh\n"
            'if [ -n "$AISH_TOOL_PREVIEW" ]; then echo "would delete 42"; exit 0; fi\ncat\n'
        )
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def test_preview_included_in_request(self, app_env, tmp_path):
        self._write_preview_tool(app_env["cwd"])
        client, _ = make_client(
            app_env,
            [model_says(tool_calls=[tool_call("pv", id="42")]), model_says("done")],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run tool"})
            request = recv_until(ws, "approval_request")
            assert request["kind"] == "tool"
            assert request["preview"] == "would delete 42"
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")


def _alive(pid: int) -> bool:
    """True while `pid` is a live (non-reaped) process. A fully-reaped pid raises
    ProcessLookupError on signal 0; a zombie the parent hasn't wait()ed yet still
    answers, so this is only used against processes the server owns and reaps."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestGlobalConsole:
    """Issue #148 follow-up: ONE persistent GLOBAL console (the "Quake console")
    for the whole server, not per-session. The USER drives it (ungated, like `!`);
    the model has no path to its input. It survives viewer-leave, session close,
    and disconnects. I/O is private unless explicitly shared. Tests inject a
    trivial python echo loop as the console command, so no tmux/shell is needed."""

    _CHILD = (
        "import sys\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write('GOT:' + line)\n"
        "    sys.stdout.flush()\n"
    )

    def _cmd(self):
        import sys as _sys

        return f"{shlex.quote(_sys.executable)} -c {shlex.quote(self._CHILD)}"

    def _client(self, app_env, responses=None, command=None):
        return make_client(
            app_env, responses or [], console_command=command or self._cmd()
        )

    def _drain_out(self, ws, wanted):
        seen = ""
        for _ in range(200):
            event = ws.receive_json()
            if event["type"] == "console_out":
                seen += event["data"]
                if wanted in seen:
                    return seen
        raise AssertionError(f"never saw {wanted!r} in console_out")

    def test_console_is_global_on_the_server_not_the_session(self, app_env):
        client, _ = self._client(app_env)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            server = client.app.state.server
            # It lives on the WebServer, never on a Session.
            assert server.console is not None
            assert not hasattr(server.active, "pty")
            ws.send_json({"type": "console_kill"})

    def test_console_in_and_out_over_the_socket(self, app_env):
        client, _ = self._client(app_env)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            # Bytes IN over the socket → the child → bytes OUT over the socket.
            ws.send_json({"type": "console_in", "data": "hello\n"})
            assert "GOT:hello" in self._drain_out(ws, "GOT:hello")
            # EOF ends the child; the exit is reported over the socket.
            ws.send_json({"type": "console_in", "data": "\x04"})
            assert recv_until(ws, "console_exit")["code"] == 0
        # The server forgot the console once it exited (no dangling handle).
        assert client.app.state.server.console is None

    def test_console_survives_viewer_leave_and_session_close(self, app_env):
        client, _ = self._client(app_env, command="cat")  # blocks on stdin, stays alive
        # Keep the TestClient (and thus the server) open ACROSS the ws disconnect —
        # only the inner `connected` context closes the socket. Exiting `with
        # client` would run lifespan shutdown, which is what detaches the console.
        with client:
            with connected(client) as (ws, _, _):
                ws.send_json({"type": "console_open"})
                recv_until(ws, "console_started")
                server = client.app.state.server
                assert server.console is not None
                pid = server.console.pid
                # Hiding the overlay must NOT kill it.
                ws.send_json({"type": "console_close"})
                assert server.console is not None
            # Socket fully closed (viewer left) → console STILL running (global).
            assert server.console is not None
            assert _alive(pid), "console was killed on disconnect — it must persist"
            # Closing a session must not touch the global console either.
            server.active.close()
            assert server.console is not None
            assert _alive(pid)
            server.console.kill()  # cleanup

    def test_console_output_broadcasts_to_every_viewer(self, app_env):
        client, _ = self._client(app_env)
        with client:
            with connected(client) as (ws1, _, _), connected(client) as (ws2, _, _):
                ws1.send_json({"type": "console_open"})
                recv_until(ws1, "console_started")
                ws2.send_json({"type": "console_open"})  # attaches to the SAME console
                recv_until(ws2, "console_started")
                assert len(client.app.state.server.console_viewers) == 2
                # One PTY: input from either socket, output to BOTH.
                ws1.send_json({"type": "console_in", "data": "hi\n"})
                assert "GOT:hi" in self._drain_out(ws1, "GOT:hi")
                assert "GOT:hi" in self._drain_out(ws2, "GOT:hi")
                ws1.send_json({"type": "console_kill"})

    def test_reopen_attaches_to_the_existing_console(self, app_env):
        client, _ = self._client(app_env, command="cat")
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            first = client.app.state.server.console
            # Hide, then reopen: a NEW console_started, but the SAME PtySession.
            ws.send_json({"type": "console_close"})
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            assert client.app.state.server.console is first
            ws.send_json({"type": "console_kill"})

    def test_console_kill_terminates_and_reports_exit(self, app_env):
        client, _ = self._client(app_env, command="cat")
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            server = client.app.state.server
            pid = server.console.pid
            ws.send_json({"type": "console_kill"})
            recv_until(ws, "console_exit")
            assert server.console is None
            deadline = time.time() + 5
            while time.time() < deadline and _alive(pid):
                time.sleep(0.02)
            assert not _alive(pid), "killed console left a zombie"

    def test_console_output_is_never_recorded_in_the_transcript(self, app_env):
        # Console I/O is private: it must not enter any session transcript (and
        # thus never a cold replay or the model's context) unless explicitly shared.
        client, _ = self._client(app_env)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            ws.send_json({"type": "console_in", "data": "secret\n"})
            self._drain_out(ws, "GOT:secret")
            transcript = client.app.state.server.active.bridge.transcript
            assert not any(e["type"].startswith("console_") for e in transcript)
            assert not any("secret" in json.dumps(e) for e in transcript)
            ws.send_json({"type": "console_kill"})

    def test_share_injects_selection_into_the_viewed_chats_context(self, app_env):
        client, _ = self._client(app_env, command="cat")
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "console_open"})
            recv_until(ws, "console_started")
            ws.send_json({"type": "console_share", "text": "device code ABC-123"})
            recv_until(ws, "console_shared")
            ws.send_json({"type": "console_kill"})
        # The shared text is now a user turn the model will see — via the same
        # user-message path as `!`, not the console stream.
        messages = client.app.state.server.active.agent.messages
        shared = [m for m in messages if "device code ABC-123" in str(m.get("content", ""))]
        assert shared
        # …and it stays out of the transcript on replay too: live it produced no
        # bubble (just the toast), so a reload must not invent one (#171).
        assert synthetic_kind(shared[0]["content"]) == "note"

    def test_model_run_command_never_touches_the_console(self, app_env, tmp_path):
        # A normal model task that runs a command must not create or feed the
        # console: the model has no console path. server.console stays None.
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
                model_says("done"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "make a file"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")
            assert client.app.state.server.console is None


class _FakeBridge:
    """Minimal Bridge stand-in for unit-testing the approvers: records emitted
    events and canned-answers asks. `ask` should never be reached for an
    auto-approved (safe) triggered mutation — the test asserts that."""

    def __init__(self, answer=None):
        self.events: list = []
        self.asked: list = []
        self._answer = answer or {"action": "approve"}

    def emit(self, event, record=True):
        self.events.append(event)

    def ask(self, request):
        request.setdefault("id", "uid-1")
        self.asked.append(request)
        return self._answer


class _FakeLog:
    def __init__(self):
        self.records: list = []
        self.timings: list = []

    def command(self, command, decision, intent="", preview="", **timing):
        self.records.append((command, decision, intent))
        self.timings.append(timing)


# The step refused in the incident behind #252, personal details removed. The
# reason is the SECOND sentence, which is what kills every first-sentence
# summary of it (see tests/test_agent.py::TestApprovalIntent).
INCIDENT_INTENT = (
    'I am going to open the "Faktury i platnosci" page in the browser again. '
    "This will let us see if there is any credit, overpayment, or adjusting "
    "transaction on the account balance that explains why the portal asks for "
    "354.56 while the PDF invoice itself shows 356.46."
)


class TestApprovalIntentOnTheCard:
    """#252. The card carries the model's stated reason beside the action, and
    the decision record keeps it — so a reason that does not match what it rode
    on is a queryable artifact rather than something reconstructed from raw
    JSONL, which is what the incident actually cost."""

    def _approvers(self, intent="", answer=None):
        bridge, log = _FakeBridge(answer), _FakeLog()
        approvers = server_module.make_web_approvers(
            bridge, log, Path("/x/allow"), Path("/x/deny"),
            ask_all=True, get_scope=lambda: (".", []),
            trust_dir=lambda p: "", get_intent=lambda: intent,
        )
        approve, _write, _read, approve_tool, _import = approvers
        return approve, approve_tool, bridge, log

    def test_a_tool_card_carries_the_reason_whole(self):
        _approve, approve_tool, bridge, _log = self._approvers(INCIDENT_INTENT)
        approve_tool("browse", {"url": "https://example.invalid/invoices"})
        [request] = bridge.asked
        assert request["intent"] == INCIDENT_INTENT
        assert "credit, overpayment" in request["intent"], "the reason was cut"

    def test_a_command_card_carries_it_too(self):
        approve, _tool, bridge, _log = self._approvers(INCIDENT_INTENT)
        approve("grep -R nadplata .")
        [request] = bridge.asked
        assert request["intent"] == INCIDENT_INTENT

    def test_the_reason_is_its_own_key_never_the_preview(self):
        """`preview` is ground truth the tool computed (#157); the reason is
        the model's word for it. One slot for both would lend the claim the
        authority of the fact."""
        _approve, approve_tool, bridge, _log = self._approvers(INCIDENT_INTENT)
        approve_tool("remember", {"note": "a fact"}, "preview text")
        [request] = bridge.asked
        assert request["preview"] == "preview text"
        assert request["intent"] == INCIDENT_INTENT

    def test_no_reason_means_no_key(self):
        """Absence is rendered client-side, not wired as an empty string — the
        trace contract's byte-identical replay depends on the key being absent
        when there is nothing to say."""
        _approve, approve_tool, bridge, _log = self._approvers("")
        approve_tool("browse", {"url": "https://example.invalid/"})
        assert "intent" not in bridge.asked[0]

    def test_the_decision_record_keeps_what_was_shown(self):
        for action, expected in (("approve", "approved"), ("deny", "denied")):
            _approve, approve_tool, _bridge, log = self._approvers(
                INCIDENT_INTENT, answer={"action": action}
            )
            approve_tool("browse", {"url": "https://example.invalid/"})
            assert log.records[-1][1] == expected
            assert log.records[-1][2] == INCIDENT_INTENT

    def test_an_auto_approved_command_records_its_reason_too(self):
        """Nothing is shown (no card), but the audit trail should still be able
        to say what the model claimed it was doing when the gate let it past."""
        bridge, log = _FakeBridge(), _FakeLog()
        approve, *_ = server_module.make_web_approvers(
            bridge, log, Path("/x/allow"), Path("/x/deny"),
            ask_all=False, get_scope=lambda: (str(Path.cwd()), [Path.cwd()]),
            trust_dir=lambda p: "", get_intent=lambda: INCIDENT_INTENT,
        )
        approve("ls")
        assert bridge.asked == []  # auto-approved, no human asked
        assert log.records[-1][1] == "auto"
        assert log.records[-1][2] == INCIDENT_INTENT

    def test_every_card_kind_carries_it(self):
        """A reason on some cards and not others is worse than none: the owner
        cannot tell "it gave no reason" from "this kind never shows one"."""
        approve, approve_tool, bridge, _log = self._approvers(INCIDENT_INTENT)
        approvers = server_module.make_web_approvers(
            bridge, _FakeLog(), Path("/x/allow"), Path("/x/deny"),
            ask_all=True, get_scope=lambda: (".", []),
            trust_dir=lambda p: "", get_intent=lambda: INCIDENT_INTENT,
        )
        _approve, approve_write, approve_read, _tool, approve_import = approvers
        plan = SimpleNamespace(
            is_new=False, target=Path("/x/a.py"), diff="+1", note="", rule="",
            rule_verb="", added=1, removed=0,
        )
        approve_write(plan)
        approve_read("/etc/hosts", "sensitive")
        approve_import("skill", "desc", [], [], [], "/dest")
        approve("ls -la /")
        approve_tool("browse", {"url": "https://example.invalid/"})
        kinds = {request["kind"]: request for request in bridge.asked}
        assert set(kinds) == {"write", "read", "import", "command", "tool"}
        for kind, request in kinds.items():
            assert request.get("intent") == INCIDENT_INTENT, f"{kind} card lost it"

    def test_the_reason_never_leaves_the_machine_in_a_push(self):
        """The push body transits a third-party service, and narration carries
        far more incidental detail than the arguments do — the very step this
        exists for named a home address."""
        _approve, approve_tool, bridge, _log = self._approvers(INCIDENT_INTENT)
        approve_tool("browse", {"url": "https://example.invalid/"})
        body = server_module._describe_hold(bridge.asked[0])
        assert INCIDENT_INTENT not in body
        assert "credit" not in body


class TestTriggeredCapabilityPolicy:
    """The #160 draft-and-hold policy, unit-tested on approve_tool directly."""

    def _approver(self, origin, answer=None):
        bridge, log = _FakeBridge(answer), _FakeLog()
        approvers = server_module.make_web_approvers(
            bridge, log, Path("/x/allow"), Path("/x/deny"),
            ask_all=False, get_scope=lambda: (".", []),
            trust_dir=lambda p: "", get_origin=lambda: origin,
        )
        return approvers[3], bridge, log  # approve_tool

    def test_safe_tool_auto_runs_in_triggered_session(self):
        approve_tool, bridge, log = self._approver("email")
        result = approve_tool("gmail_label", {"message_id": "m1", "add": "Receipts"})
        assert result is True
        assert bridge.asked == []  # no card — auto-run, no human needed
        assert log.records and log.records[0][1] == "auto (email)"

    def test_draft_recipients_are_checked_too(self):
        # #178 P0-3: a draft addressed to a third party is a staged
        # exfiltration one tap from sending — it holds like a live send
        # (still draftable, just through the card). Owner-addressed drafts
        # keep their autonomy.
        approve_tool, bridge, _ = self._approver("email")
        assert approve_tool(
            "gmail_send", {"to": "pawel@wenda.eu", "body": "hi", "draft": True}
        ) is True
        assert bridge.asked == []
        approve_tool2, bridge2, _ = self._approver("email")
        approve_tool2("gmail_send", {"to": "x@y.z", "body": "hi", "draft": True})
        assert len(bridge2.asked) == 1 and bridge2.asked[0]["tool"] == "gmail_send"
        # A live (non-draft) send to a third party falls through to the card.
        approve_tool3, bridge3, _ = self._approver("email")
        approve_tool3("gmail_send", {"to": "x@y.z", "body": "hi"})
        assert len(bridge3.asked) == 1 and bridge3.asked[0]["tool"] == "gmail_send"

    def test_unsafe_tool_holds_in_triggered_session(self):
        approve_tool, bridge, _ = self._approver("email")
        approve_tool("gmail_trash", {"message_id": "m1"})
        assert len(bridge.asked) == 1  # trash always prompts, even when triggered

    def test_remember_is_never_triggered_safe(self):
        """#196 routes a triggered session's memory write through this same
        channel, so the capability policy must not shortcut it: a memory
        persists into every future session, which is exactly what the owner
        needs to see. (Deletion never reaches here — it is refused outright.)"""
        assert not server_module._triggered_safe("remember", {"note": "a fact"})
        approve_tool, bridge, _ = self._approver("email")
        approve_tool("remember", {"note": "a fact", "name": "from-mail"}, "preview text")
        assert len(bridge.asked) == 1
        assert bridge.asked[0]["tool"] == "remember"
        assert bridge.asked[0]["preview"] == "preview text"

    def test_user_session_always_prompts_even_for_safe_tools(self):
        # The policy is scoped to NON-user origins; a human-driven session gates
        # every mutation as before (no silent auto-run).
        approve_tool, bridge, _ = self._approver("user")
        approve_tool("gmail_label", {"message_id": "m1", "add": "Receipts"})
        assert len(bridge.asked) == 1

    def test_triggered_safe_helper(self):
        assert server_module._triggered_safe("gmail_label", {})
        # A draft is no longer auto-safe on the draft flag alone (#178 P0-3):
        # its recipients must be verifiably the owner, like a live send.
        assert not server_module._triggered_safe("gmail_send", {"draft": True})
        assert server_module._triggered_safe(
            "gmail_send", {"draft": True, "to": "pawel@wenda.eu"}
        )
        assert not server_module._triggered_safe(
            "gmail_send", {"draft": True, "to": "x@evil.com"}
        )
        assert not server_module._triggered_safe("gmail_send", {})
        assert not server_module._triggered_safe("gmail_trash", {"message_id": "m"})

    def test_owner_scoped_send_auto_runs(self):
        # A live send to the owner needs no approval (recipient-scoped autonomy).
        approve_tool, bridge, _ = self._approver("email")
        assert approve_tool("gmail_send",
                            {"to": "pawel@wenda.eu", "body": "here you go"}) is True
        assert bridge.asked == []

    def test_send_to_third_party_still_holds(self):
        approve_tool, bridge, _ = self._approver("email")
        approve_tool("gmail_send", {"to": "stranger@evil.com", "body": "x"})
        assert len(bridge.asked) == 1

    def test_mixed_recipients_hold(self):
        # Owner + a third party = NOT all-owner, so it holds.
        approve_tool, bridge, _ = self._approver("email")
        approve_tool("gmail_send",
                     {"to": "pawel@wenda.eu, stranger@evil.com", "body": "x"})
        assert len(bridge.asked) == 1

    def test_all_recipients_owner_helper(self):
        f = server_module._all_recipients_owner
        assert f({"to": "pawel@wenda.eu"})
        assert f({"to": "Pawel <pawel@wenda.email>"})  # display-name form
        assert f({"to": "pawel@wenda.eu", "cc": "pawel@wenda.email"})
        assert not f({"to": "pawel@wenda.eu", "bcc": "x@other.com"})
        assert not f({"reply_to_msg_id": "abc"})  # reply recipient unverifiable
        # A reply is unsafe EVEN with an owner `to` — it also hits the original
        # (possibly third-party) sender.
        assert not f({"reply_to_msg_id": "abc", "to": "pawel@wenda.eu"})
        assert not f({})

    def test_adversarial_recipients_never_pass_as_owner(self):
        """#178 P0-3: the old regex FOUND the owner address inside adversarial
        fields instead of validating what the field routes to. Every one of
        these must hold for approval, not auto-send."""
        f = server_module._all_recipients_owner
        # Valid RFC 5322 quoted local-part — routes to evil.com, reads as owner.
        assert not f({"to": '"pawel@wenda.eu"@evil.com'})
        assert not f({"to": "pawel@wenda.eu@evil.com"})  # multi-@, malformed
        assert not f({"to": "pawel@wenda.eu, attacker@evil.com"})
        assert not f({"to": ["pawel@wenda.eu", "attacker@evil.com"]})
        # Owner address as DISPLAY NAME on an attacker address.
        assert not f({"to": "pawel@wenda.eu <attacker@evil.com>"})
        assert not f({"to": '"pawel@wenda.eu" <attacker@evil.com>'})
        # Owner address in an RFC comment beside an attacker address.
        assert not f({"to": "attacker@evil.com (pawel@wenda.eu)"})
        assert not f({"to": "<pawel@wenda.eu> attacker@evil.com"})  # residue
        assert not f({"to": "pawel@wenda.eu; attacker@evil.com"})
        assert not f({"to": ""})
        assert not f({"to": "not-an-address"})
        # An owner-only field in every legitimate spelling still passes.
        assert f({"to": " pawel@wenda.eu "})
        assert f({"to": "PAWEL@WENDA.EU"})  # case-insensitive owner match
        assert f({"to": '"Wenda, Pawel" <pawel@wenda.eu>'})  # comma in quotes
        assert f({"to": ["pawel@wenda.eu", "pawel@wenda.email"]})  # list form

    def test_parse_recipients_rejects_residue_and_quoting(self):
        p = server_module._parse_recipients
        assert p("a@b.co, c@d.co") == ["a@b.co", "c@d.co"]
        assert p("Some One <a@b.co>") == ["a@b.co"]
        assert p('"pawel@wenda.eu"@evil.com') is None  # quoted local-part
        assert p("a@b.co@c.co") is None  # multi-@ local-part / malformed
        assert p("") is None
        assert p("a@b.co extra-junk") is None  # residue escapes the parse


class TestTriggerEndpoint:
    """The loopback/token-gated /trigger ingress and origin plumbing (#160)."""

    def test_trigger_spawns_origin_tagged_session_and_runs(self, app_env):
        client, _ = make_client(app_env, [model_says("triaged")], token="secret")
        with client:
            r = client.post("/trigger?token=secret",
                            json={"prompt": "new mail arrived", "origin": "email",
                                  "meta": {"msg": "abc"}, "title": "Email: booking"})
            assert r.status_code == 200
            name = r.json()["session"]
            assert r.json()["origin"] == "email"
            # Drive the spawned session to completion, then confirm provenance.
            with connected(client, f"/ws?token=secret&session={name}") as (ws, _, _):
                recv_until(ws, "done")
                ws.send_json({"type": "sessions"})
                lst = recv_until(ws, "session_list")
            row = next(s for s in lst["sessions"] if s["name"] == name)
            assert row["origin"] == "email"
            session = client.app.state.server.sessions[name]
            assert session.origin == "email"
            assert session.trigger_meta == {"msg": "abc"}

    def test_trigger_prompt_is_a_system_turn_live_and_on_replay(self, app_env):
        # A schedule/email/webhook prompt is not the owner typing (#171), and
        # what he types into that chat afterwards IS. Live and cold must agree.
        client, _ = make_client(app_env, [model_says("triaged"), model_says("replied")],
                                token="secret")
        with client:
            r = client.post("/trigger?token=secret",
                            json={"prompt": "new mail from the bank", "origin": "email"})
            name = r.json()["session"]
            with connected(client, f"/ws?token=secret&session={name}") as (ws, _, _):
                recv_until(ws, "done")
                ws.send_json({"type": "task", "text": "reply to it"})
                recv_until(ws, "done")
            server = client.app.state.server
            live = [e for e in server.sessions[name].bridge.transcript if e["type"] == "user"]
            path = server.state_dir / name

        assert [(e["text"], e.get("synthetic")) for e in live] == [
            ("new mail from the bank", "trigger"),
            ("reply to it", None),
        ]
        cold = [e for e in SessionLog.reconstruct_events(path) if e["type"] == "user"]
        assert [(e["text"], e.get("synthetic")) for e in cold] == [
            (e["text"], e.get("synthetic")) for e in live
        ]

    def test_pager_pages_carry_origin(self, app_env):
        # The swipe pager pages within one lane (Recent vs Automated), so every
        # page needs its origin — including cold ones read off disk.
        client, _ = make_client(app_env, [model_says("triaged"), model_says("ok")],
                                token="secret")
        with client, connected(client, "/ws?token=secret") as (ws, hello, _):
            mine = hello["session"]
            ws.send_json({"type": "task", "text": "my own chat"})
            recv_until(ws, "done")
            r = client.post("/trigger?token=secret",
                            json={"prompt": "new mail", "origin": "email"})
            auto = r.json()["session"]
            with connected(client, f"/ws?token=secret&session={auto}") as (auto_ws, _, _):
                recv_until(auto_ws, "done")
            ws.send_json({"type": "resume", "path": mine})
            hello = recv_until(ws, "hello")
            origins = {p["name"]: p["origin"] for p in hello["pager"]}
            assert origins[mine] == "user"
            assert origins[auto] == "email"

    def test_cold_open_does_not_rewrite_provenance(self, app_env):
        """Reopening a triggered session must leave its log alone. The origin
        record is already on disk (open_session just parsed it back), and
        re-writing it appended a record — plus the pending model one it flushed
        — on every open, bumping the mtime that orders every recency list."""
        client, _ = make_client(app_env, [model_says("triaged")], token="secret")
        with client:
            r = client.post("/trigger?token=secret",
                            json={"prompt": "new mail", "origin": "email"})
            name = r.json()["session"]
            with connected(client, f"/ws?token=secret&session={name}") as (ws, _, _):
                recv_until(ws, "done")
            server = client.app.state.server
            path = server.state_dir / name
            before, mtime = path.read_bytes(), path.stat().st_mtime_ns

            session, _history = server.open_session(path)  # the cold-open path

            assert session.origin == "email"  # provenance survives the round trip
            assert path.read_bytes() == before  # …without one byte written back
            assert path.stat().st_mtime_ns == mtime

    def test_trigger_rejects_bad_token(self, app_env):
        client, _ = make_client(app_env, [model_says("x")], token="secret")
        with client:
            r = client.post("/trigger?token=nope", json={"prompt": "hi"})
            assert r.status_code == 403

    def test_trigger_rejects_user_origin(self, app_env):
        client, _ = make_client(app_env, [model_says("x")], token="secret")
        with client:
            r = client.post("/trigger?token=secret",
                            json={"prompt": "hi", "origin": "user"})
            assert r.status_code == 400

    def test_trigger_requires_prompt(self, app_env):
        client, _ = make_client(app_env, [model_says("x")], token="secret")
        with client:
            r = client.post("/trigger?token=secret", json={"origin": "email"})
            assert r.status_code == 400

    def test_trigger_does_not_move_the_default_session(self, app_env):
        # #178 P1-6: an overnight trigger must not become the landing spot for
        # the next bare connect (nor eviction-immune via default status).
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        with client:
            server = client.app.state.server
            before = server._default
            r = client.post("/trigger?token=secret",
                            json={"prompt": "go", "origin": "email"})
            assert r.status_code == 200
            name = r.json()["session"]
            assert server._default is before
            assert server._default.name != name
            # Let the background task finish so shutdown is clean.
            with connected(client, f"/ws?token=secret&session={name}") as (ws, _, _):
                recv_until(ws, "done")
            assert server._default is before


class TestTriggerHardening:
    """Idempotency, per-origin rate limit, and concurrency cap on /trigger
    (#178 P1-10) — every refusal happens BEFORE a session is created."""

    def test_dedup_key_reuses_the_session(self, app_env):
        client, _ = make_client(app_env, [model_says("one")], token="secret")
        with client:
            server = client.app.state.server
            before = set(server.sessions)
            body = {"prompt": "mail", "origin": "email",
                    "meta": {"dedup_key": "gmail-m1"}}
            r1 = client.post("/trigger?token=secret", json=body)
            assert r1.status_code == 200
            name = r1.json()["session"]
            drain_done(client, name, token="secret")
            # The retry storm case: the same delivery POSTed again answers 200
            # with the SAME session and opens nothing new.
            r2 = client.post("/trigger?token=secret", json=body)
            assert r2.status_code == 200
            assert r2.json() == {"session": name, "origin": "email", "deduped": True}
            assert set(server.sessions) - before == {name}

    def test_trigger_model_override_runs_the_session_on_it(self, app_env, monkeypatch):
        # #186: a privacy-scoped trigger names its own model; the session runs
        # on it and records it, independent of the server default.
        override = FakeChat([model_says("done locally")])
        monkeypatch.setattr(
            server_module.backends, "make_chat",
            lambda spec: (override, "ollama", spec),
        )
        client, _ = make_client(app_env, [], token="secret")
        with client:
            r = client.post("/trigger?token=secret", json={
                "prompt": "curate", "origin": "schedule", "model": "qwen3:8b",
            })
            assert r.status_code == 200
            name = r.json()["session"]
            drain_done(client, name, token="secret")
            agent = client.app.state.server.sessions[name].agent
            assert agent.model == "qwen3:8b"
            assert override.calls  # the override chat, not the server default

    def test_trigger_model_override_fails_closed(self, app_env, monkeypatch):
        # An unbuildable override must refuse — silently running the prompt on
        # the (possibly cloud) default is the leak the override prevents. And
        # the refusal happens before any session log exists.
        def broken(spec):
            raise server_module.backends.BackendError("no such model")

        monkeypatch.setattr(server_module.backends, "make_chat", broken)
        client, _ = make_client(app_env, [], token="secret")
        with client:
            server = client.app.state.server
            logs_before = set(Path(app_env["state_dir"]).glob("session-*.jsonl"))
            before = set(server.sessions)
            r = client.post("/trigger?token=secret", json={
                "prompt": "curate", "origin": "schedule", "model": "nope:missing",
            })
            assert r.status_code == 503
            assert "model unavailable" in r.json()["error"]
            assert set(server.sessions) == before
            assert set(Path(app_env["state_dir"]).glob("session-*.jsonl")) == logs_before

    def test_no_dedup_key_fires_every_post(self, app_env):
        # Backward compatibility: clients that send no key keep old behavior.
        client, _ = make_client(app_env, [model_says("a"), model_says("b")],
                                token="secret")
        with client:
            names = set()
            for _ in range(2):
                r = client.post("/trigger?token=secret",
                                json={"prompt": "go", "origin": "email"})
                assert r.status_code == 200
                assert "deduped" not in r.json()
                names.add(r.json()["session"])
            assert len(names) == 2
            for name in names:
                drain_done(client, name, token="secret")

    def test_dedup_key_expires(self, app_env, monkeypatch):
        monkeypatch.setattr(server_module, "TRIGGER_DEDUP_TTL", -1)  # everything stale
        client, _ = make_client(app_env, [model_says("a"), model_says("b")],
                                token="secret")
        with client:
            body = {"prompt": "go", "origin": "email", "meta": {"dedup_key": "k"}}
            names = set()
            for _ in range(2):
                r = client.post("/trigger?token=secret", json=body)
                assert r.status_code == 200
                names.add(r.json()["session"])
                drain_done(client, r.json()["session"], token="secret")
            assert len(names) == 2  # an expired key no longer dedupes

    def test_rate_limit_429_before_any_session(self, app_env):
        client, _ = make_client(app_env, [model_says("a"), model_says("b")],
                                token="secret", trigger_rate_capacity=2,
                                trigger_rate_refill_s=3600)
        with client:
            server = client.app.state.server
            names = []
            for _ in range(2):
                r = client.post("/trigger?token=secret",
                                json={"prompt": "go", "origin": "webhook"})
                assert r.status_code == 200
                names.append(r.json()["session"])
            before = set(server.sessions)
            r = client.post("/trigger?token=secret",
                            json={"prompt": "go", "origin": "webhook"})
            assert r.status_code == 429
            assert r.headers["Retry-After"]
            assert "rate limit" in r.json()["error"]
            assert set(server.sessions) == before  # refused BEFORE creation
            for name in names:
                drain_done(client, name, token="secret")

    def test_rate_limit_is_per_origin(self, app_env):
        client, _ = make_client(app_env, [model_says("a"), model_says("b")],
                                token="secret", trigger_rate_capacity=1,
                                trigger_rate_refill_s=3600)
        with client:
            r1 = client.post("/trigger?token=secret",
                             json={"prompt": "go", "origin": "email"})
            assert r1.status_code == 200
            assert client.post("/trigger?token=secret",
                               json={"prompt": "go", "origin": "email"}).status_code == 429
            # A different origin has its own bucket.
            r2 = client.post("/trigger?token=secret",
                             json={"prompt": "go", "origin": "schedule"})
            assert r2.status_code == 200
            for r in (r1, r2):
                drain_done(client, r.json()["session"], token="secret")

    def test_concurrency_cap_429_while_triggered_sessions_run(self, app_env):
        release = threading.Event()

        class BlockingChat:
            """Holds the triggered task mid-run so the session stays busy."""

            def __call__(self, **kwargs):
                if _is_title_call(kwargs):
                    return model_says("")
                release.wait(timeout=10)
                response = model_says("done")
                return iter([response]) if kwargs.get("stream") else response

        app = create_app("fake", client_chat=BlockingChat(), **app_env,
                         token="secret", max_concurrent_triggered=1)
        client = TokenClient(app, auto_token="secret")
        try:
            with client:
                r1 = client.post("/trigger?token=secret",
                                 json={"prompt": "go", "origin": "email"})
                assert r1.status_code == 200  # busy is set before the POST returns
                # Different origin, so the rate limiter is not what refuses it.
                r2 = client.post("/trigger?token=secret",
                                 json={"prompt": "go", "origin": "webhook"})
                assert r2.status_code == 429
                assert r2.headers["Retry-After"]
                assert "concurrent" in r2.json()["error"]
                release.set()
                name = r1.json()["session"]
                drain_done(client, name, token="secret")
                # busy clears in _finish_turn, AFTER the done we just saw —
                # wait for it so the next POST reads the settled count.
                server = client.app.state.server
                for _ in range(200):
                    if not server.sessions[name].busy:
                        break
                    time.sleep(0.01)
                # With the first one finished the cap admits the next.
                r3 = client.post("/trigger?token=secret",
                                 json={"prompt": "go", "origin": "webhook"})
                assert r3.status_code == 200
                drain_done(client, r3.json()["session"], token="secret")
        finally:
            release.set()


class TestWorkerPool:
    """Agent workers run on the DEDICATED executor (#178 Gate 3): a parked
    approval holds a worker thread indefinitely, and on the shared default
    executor a few of those starved the very _show/to_thread calls a client
    needs to attach and answer them — livelock, restart-only recovery."""

    def test_run_task_executes_on_a_worker_pool_thread(self, app_env):
        seen: list[str] = []

        class RecordingChat:
            def __call__(self, **kwargs):
                if _is_title_call(kwargs):
                    return model_says("")
                seen.append(threading.current_thread().name)
                response = model_says("hi")
                return iter([response]) if kwargs.get("stream") else response

        app = create_app("fake", client_chat=RecordingChat(), **app_env,
                         token=TEST_TOKEN)
        client = TokenClient(app, auto_token=TEST_TOKEN)
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "hello"})
            recv_until(ws, "done")
        assert seen and all(name.startswith("aish-worker") for name in seen)

    def test_saturated_worker_pool_does_not_block_attach(self, app_env):
        # With EVERY worker thread parked (as held approvals would), a viewer
        # can still connect and replay — the short ops must never share the
        # pool, or nobody could attach to answer what parked it.
        client, _ = make_client(app_env, [model_says("ok")])
        release = threading.Event()
        try:
            with client:
                server = client.app.state.server
                for _ in range(server_module.WORKER_POOL_SIZE):
                    server.worker_pool.submit(release.wait, 30)
                with connected(client) as (_ws, hello, replay):
                    assert hello["session"]
                    assert replay["events"] == []
                release.set()
        finally:
            release.set()

    def test_shutdown_does_not_wait_on_parked_workers(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        release = threading.Event()
        with client:
            server = client.app.state.server
            server.worker_pool.submit(release.wait, 30)
        # Reaching here means lifespan shutdown returned while the fake parked
        # worker was still waiting (wait=False + cancel_futures).
        release.set()


class TestOriginPersistence:
    """origin survives a cold reopen from the log (#160)."""

    def test_origin_recorded_and_reparsed(self, app_env, tmp_path):
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        with client:
            r = client.post("/trigger?token=secret",
                            json={"prompt": "go", "origin": "schedule"})
            name = r.json()["session"]
            with connected(client, f"/ws?token=secret&session={name}") as (ws, _, _):
                recv_until(ws, "done")
        # Re-parse straight off disk: the origin record round-trips.
        path = Path(app_env["state_dir"]) / name
        origin = SessionLog._parse(path).origin
        assert origin == "schedule"
        # And a fresh, untagged session parses as the default "user".
        assert SessionLog._info_from(path, [{"role": "user", "content": "x"}]).origin == "user"


class TestHoldNotification:
    """Pushover notification when an unattended triggered session holds (#163).
    The notify_hold closure is reachable as bridge.on_wait, so its gating is
    tested directly — no need to deadlock a real blocking approval."""

    def _held_event(self):
        return {"type": "approval_request", "kind": "tool",
                "tool": "gmail_trash", "args": {"message_id": "m1"}, "id": "u1"}

    def _spawn_email_session(self, client, monkeypatch, calls):
        monkeypatch.setattr(server_module.notify, "configured", lambda: True)
        monkeypatch.setattr(server_module.notify, "pushover",
                            lambda *a, **k: calls.append((a, k)) or True)
        r = client.post("/trigger?token=secret",
                        json={"prompt": "go", "origin": "email", "title": "Email: hi"})
        return client.app.state.server.sessions[r.json()["session"]]

    def test_triggered_hold_with_no_viewer_notifies(self, app_env, monkeypatch):
        monkeypatch.setenv("AISH_PUBLIC_URL", "https://aish.test")
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        calls: list = []
        with client:
            session = self._spawn_email_session(client, monkeypatch, calls)
            session.bridge.on_wait(self._held_event(), False)  # no viewers
        assert len(calls) == 1
        (title, body), kw = calls[0]
        assert "approval" in title.lower() and "Email: hi" in title
        assert "gmail_trash" in body
        assert kw["url"].endswith(f"/?session={session.name}")
        assert kw["url"].startswith("https://aish.test")
        # Normal priority: a hold must not bypass quiet hours (see notify_hold).
        assert kw["priority"] == 0

    def test_triggered_hold_with_a_viewer_stays_silent(self, app_env, monkeypatch):
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        calls: list = []
        with client:
            session = self._spawn_email_session(client, monkeypatch, calls)
            session.bridge.on_wait(self._held_event(), True)  # someone is watching
        assert calls == []

    def test_user_session_never_notifies_on_hold(self, app_env, monkeypatch):
        monkeypatch.setattr(server_module.notify, "configured", lambda: True)
        calls: list = []
        monkeypatch.setattr(server_module.notify, "pushover",
                            lambda *a, **k: calls.append(1))
        client, _ = make_client(app_env, [model_says("hi")])
        with client:
            default = client.app.state.server._default
            assert default.origin == "user"
            default.bridge.on_wait(self._held_event(), False)
        assert calls == []

    def test_describe_hold_shapes(self):
        d = server_module._describe_hold
        assert "gmail_trash" in d({"kind": "tool", "tool": "gmail_trash", "args": {}})
        assert d({"kind": "tool", "tool": "t", "preview": "delete X"}).endswith("delete X")
        assert d({"kind": "command", "command": "rm x"}).startswith("run:")
        assert d({"kind": "write", "verb": "edit", "target": "/a"}) == "edit /a"


class TestDoneNotification:
    """Pushover notification when a TRIGGERED session finishes (#163), and its
    viewer gating: a session someone is watching must stay silent, or every
    message typed into an automated chat pushes a phone notification for work
    happening on screen. Called directly — the coroutine is the whole unit."""

    def _server(self, app_env, monkeypatch, calls):
        monkeypatch.setattr(server_module.notify, "configured", lambda: True)
        monkeypatch.setattr(server_module.notify, "pushover",
                            lambda *a, **k: calls.append((a, k)) or True)
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        return client.app.state.server

    def _session(self, origin="email", viewers=()):
        return SimpleNamespace(origin=origin, viewers=set(viewers),
                               custom_title="Email: hi", name="session-x.jsonl")

    def test_triggered_done_with_no_viewer_notifies(self, app_env, monkeypatch):
        calls: list = []
        server = self._server(app_env, monkeypatch, calls)
        server.public_url = "https://aish.test"
        asyncio.run(server._notify_done(self._session(), "sent the reply"))
        assert len(calls) == 1
        (title, body), kw = calls[0]
        assert "finished" in title.lower() and "Email: hi" in title
        assert body == "sent the reply"
        assert kw["url"] == "https://aish.test/?session=session-x.jsonl"
        assert kw["priority"] == 0

    def test_suite_never_reaches_the_real_notifier(self, app_env, monkeypatch):
        # Regression: the restart-recovery tests run an email-origin session to
        # completion with no viewer, so _notify_done called the REAL
        # notify.pushover — which reads the developer's live Keychain and
        # pushed to their actual phone on every `pytest` run. The conftest
        # kill switch must make that structurally impossible, with nothing
        # here stubbed.
        sent: list = []
        monkeypatch.setattr(notify_module.urllib.request, "urlopen",
                            lambda *a, **k: sent.append(1))
        monkeypatch.setattr(notify_module.secrets, "get", lambda name: "real-cred")
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        server = client.app.state.server
        server.public_url = "https://aish.test"
        assert notify_module.configured() is False
        asyncio.run(server._notify_done(self._session(), "sent the reply"))
        assert sent == []  # no POST, real creds present or not

    def test_triggered_done_with_a_viewer_stays_silent(self, app_env, monkeypatch):
        calls: list = []
        server = self._server(app_env, monkeypatch, calls)
        session = self._session(viewers=[object()])
        asyncio.run(server._notify_done(session, "sent the reply"))
        assert calls == []

    def test_user_session_never_notifies_on_done(self, app_env, monkeypatch):
        calls: list = []
        server = self._server(app_env, monkeypatch, calls)
        asyncio.run(server._notify_done(self._session(origin="user"), "hi"))
        assert calls == []


class TestBridgeOnWait:
    """The Bridge.ask hook that powers hold notifications fires before blocking
    and reports whether anyone is viewing."""

    def test_on_wait_fires_with_viewer_flag_and_does_not_block(self):
        seen: list = []
        bridge = server_module.Bridge(lambda: None)

        def hook(event, has_viewers):
            seen.append((event["kind"], has_viewers))
            bridge.answer(event["id"], {"action": "deny"})  # unblock immediately

        bridge.on_wait = hook
        result = bridge.ask({"type": "approval_request", "kind": "tool", "tool": "x"})
        # The verdict, plus the gate's own measurement of how long it waited for
        # it (#306). Every decided card carries one and `ask` stamps it, because
        # that half of the latency is the server's own and the browser is not an
        # authority on it.
        assert result["action"] == "deny"
        assert isinstance(result["held_ms"], int) and result["held_ms"] >= 0
        assert seen == [("tool", False)]  # empty viewers → False

    def test_on_wait_error_never_breaks_the_gate(self):
        bridge = server_module.Bridge(lambda: None)

        def boom(event, has_viewers):
            bridge.answer(event["id"], {"action": "approve"})
            raise RuntimeError("notify blew up")

        bridge.on_wait = boom
        # The gate still returns the answer despite the hook raising.
        answer = bridge.ask({"type": "approval_request", "kind": "tool", "tool": "x"})
        assert answer["action"] == "approve"


class TestRestartResume:
    """Restart recovery (#164): a task killed mid-run by a server restart is
    picked up again at the next startup. Without this an interrupted automated
    session — an email trigger the poller has already marked processed — simply
    never happens, and nothing anywhere would ever notice."""

    @staticmethod
    def _interrupted_log(app_env, prompt="answer the mail", *, with_history=True,
                         attempts=1, origin="email"):
        """A session log in the state the OS leaves behind when the process is
        killed mid-task: a task_start with no task_end."""
        state_dir = Path(app_env["state_dir"])
        log = SessionLog(state_dir / "session-20260101-120000-000000.jsonl")
        log.model("fake")
        if origin != "user":
            log.origin(origin)
        for _ in range(attempts):
            log.task_start(prompt)
            if with_history:
                log.message({"role": "user", "content": prompt})
        log.close()
        return log.path

    @staticmethod
    def _wait(predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _resumed_prompt(self, chat):
        """The text of the resumed turn as the model saw it: the last user
        message of the first model call the resumed task made."""
        for message in reversed(chat.calls[0]["messages"]):
            if message.get("role") == "user":
                return message.get("content", "")
        return ""

    def test_interrupted_task_resumes_at_startup(self, app_env):
        path = self._interrupted_log(app_env)
        client, chat = make_client(app_env, [model_says("sent the reply")])
        with client:
            server = client.app.state.server
            assert self._wait(lambda: path.name in server.sessions), "never resumed"
            session = server.sessions[path.name]
            assert self._wait(lambda: not session.busy), "resumed task never finished"
        # Continues the conversation instead of re-issuing the prompt, so work
        # already done (a sent email) is not repeated.
        assert "[automatic resume]" in self._resumed_prompt(chat)
        assert session.origin == "email"  # provenance survives the resume
        # The completed run closed its bracket: a second restart won't redo it.
        assert SessionLog.pending_task(path) is None

    def test_resume_names_the_step_that_was_in_flight(self, app_env):
        # The one genuinely unknown part of an interrupted run: a step that
        # started and never reported back. The model is told to verify THOSE
        # rather than re-run the task.
        state_dir = Path(app_env["state_dir"])
        log = SessionLog(state_dir / "session-20260101-120000-000000.jsonl")
        log.model("fake")
        log.task_start("answer the mail")
        log.message({"role": "user", "content": "answer the mail"})
        log.step({"kind": "tool_start", "name": "gmail_search", "summary": "id:abc"})
        log.step({"kind": "tool", "name": "gmail_search", "ok": True})
        log.message({"role": "tool", "tool_name": "gmail_search", "content": "the mail"})
        log.step({"kind": "tool_start", "name": "gmail_send", "summary": "to pawel"})
        log.close()  # killed with the send in flight
        client, chat = make_client(app_env, [model_says("verified, already sent")])
        with client:
            server = client.app.state.server
            assert self._wait(lambda: log.path.name in server.sessions)
            assert self._wait(lambda: not server.sessions[log.path.name].busy)
        prompt = self._resumed_prompt(chat)
        assert "gmail_send: to pawel" in prompt
        assert "gmail_search" not in prompt  # it reported back; nothing to verify

    def test_resume_keeps_the_interrupted_output_verbatim(self, app_env):
        # The point of resuming: prior results arrive whole, so the model
        # continues instead of recomputing them.
        state_dir = Path(app_env["state_dir"])
        log = SessionLog(state_dir / "session-20260101-120000-000000.jsonl")
        log.model("fake")
        log.task_start("summarize the page")
        log.message({"role": "user", "content": "summarize the page"})
        log.message({"role": "tool", "tool_name": "read_url", "content": "PAGE" * 2000})
        log.close()
        client, chat = make_client(app_env, [model_says("summarized")])
        with client:
            server = client.app.state.server
            assert self._wait(lambda: log.path.name in server.sessions)
            assert self._wait(lambda: not server.sessions[log.path.name].busy)
        sent = [m for m in chat.calls[0]["messages"] if m.get("role") == "tool"]
        assert sent and len(sent[0]["content"]) == 4 * 2000  # untrimmed

    def test_resume_note_renders_as_a_system_turn_live_and_on_replay(self, app_env):
        # #171: the resume note is aish talking to itself, so it must never look
        # like something the user typed — and it must look the same after a
        # reload, which is why the live event and the cold replay are asserted
        # together. The re-issued original prompt above it stays a real bubble.
        path = self._interrupted_log(app_env, "answer the mail", origin="user")
        client, _chat = make_client(app_env, [model_says("already sent")])
        with client:
            server = client.app.state.server
            assert self._wait(lambda: path.name in server.sessions)
            session = server.sessions[path.name]
            assert self._wait(lambda: not session.busy)
            live = [e for e in session.bridge.transcript if e["type"] == "user"]

        assert [e.get("synthetic") for e in live] == ["resume"]  # the resumed turn
        cold = [e for e in SessionLog.reconstruct_events(path) if e["type"] == "user"]
        assert [(e["text"], e.get("synthetic")) for e in cold] == [
            ("answer the mail", None),  # what the user really asked for
            (live[0]["text"], "resume"),  # …and the same synthetic turn, framed alike
        ]

    def test_resume_reissues_prompt_when_nothing_was_logged(self, app_env):
        # Killed before the user message itself was logged: the history holds
        # nothing to continue from, so the recorded prompt is re-issued.
        path = self._interrupted_log(app_env, "read msg 42", with_history=False)
        client, chat = make_client(app_env, [model_says("read it")])
        with client:
            server = client.app.state.server
            assert self._wait(lambda: path.name in server.sessions)
            assert self._wait(lambda: not server.sessions[path.name].busy)
        assert self._resumed_prompt(chat) == "read msg 42"

    def test_finished_session_is_not_resumed(self, app_env):
        state_dir = Path(app_env["state_dir"])
        log = SessionLog(state_dir / "session-20260101-120000-000000.jsonl")
        log.task_start("all done")
        log.message({"role": "user", "content": "all done"})
        log.task_end()
        log.close()
        client, chat = make_client(app_env, [])
        with client:
            time.sleep(0.3)
            assert log.path.name not in client.app.state.server.sessions
        assert chat.calls == []

    def test_repeatedly_interrupted_task_is_abandoned(self, app_env):
        # A task that keeps killing the server must not crash-loop it.
        path = self._interrupted_log(app_env, attempts=server_module.RESUME_MAX_ATTEMPTS)
        client, chat = make_client(app_env, [])
        with client:
            time.sleep(0.3)
            assert path.name not in client.app.state.server.sessions
        assert chat.calls == []

    def test_stale_interrupted_task_is_not_resumed(self, app_env):
        path = self._interrupted_log(app_env)
        old = time.time() - server_module.RESUME_WINDOW - 60
        os.utime(path, (old, old))
        client, chat = make_client(app_env, [])
        with client:
            time.sleep(0.3)
            assert path.name not in client.app.state.server.sessions
        assert chat.calls == []

    def test_task_markers_bracket_a_normal_run(self, app_env):
        client, _ = make_client(app_env, [model_says("hi there")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "hello"})
            recv_until(ws, "done")
            path = Path(app_env["state_dir"]) / hello["session"]
            assert SessionLog.pending_task(path) is None
            kinds = [json.loads(line)["kind"] for line in path.read_text().splitlines()]
            assert kinds.count("task_start") == 1 and kinds.count("task_end") == 1

    def test_user_command_leaves_no_resume_marker(self, app_env):
        # A ! command is the user's own shell action — re-running it unattended
        # is a risk, not a recovery, so it writes no marker.
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "!echo hi"})
            recv_until(ws, "done")
            path = Path(app_env["state_dir"]) / hello["session"]
            assert SessionLog.pending_task(path) is None
            assert "task_start" not in path.read_text()


class TestOfflineMirror:
    """Issue #165: the read-only endpoints the PWA mirrors conversations
    through. They must serve a session straight off disk — no Agent, no session
    slot — and must not resend what the client already holds."""

    def test_index_lists_sessions_and_gates_on_token(self, app_env):
        client, _ = make_client(app_env, [model_says("mirrored answer")], token="s3cret")
        with client, connected(client, "/ws?token=s3cret") as (ws, hello, _):
            ws.send_json({"type": "task", "text": "a task worth keeping"})
            recv_until(ws, "done")

            assert client.get("/offline/index").status_code == 403
            payload = client.get("/offline/index?token=s3cret").json()

        assert payload["rev"]  # the frontend's staleness check rides along
        row = next(s for s in payload["sessions"] if s["name"] == hello["session"])
        assert row["title"] == "a task worth keeping"
        assert row["origin"] == "user"
        assert row["ts"] > 0

    def test_session_returns_the_same_events_a_client_replays(self, app_env):
        client, _ = make_client(app_env, [model_says("the recommendation")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "what should I do?"})
            recv_until(ws, "done")
            payload = client.get(f"/offline/session?session={hello['session']}").json()

        # Same shape the live `replay` frame carries, which is what lets the
        # cached copy render through the unchanged onReplay path.
        assert payload["base"] == 0
        assert payload["total"] == len(payload["events"])
        types = [e["type"] for e in payload["events"]]
        assert types[0] == "user" and types[-1] == "done"
        assert payload["events"][0]["text"] == "what should I do?"
        assert payload["events"][-1]["result"] == "the recommendation"
        assert payload["title"] == "what should I do?"
        assert payload["sig"]

    def test_unknown_session_is_404_and_traversal_is_refused(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/offline/session?session=nope.jsonl").status_code == 404
            assert client.get(
                "/offline/session?session=session-../../etc/passwd"
            ).status_code == 404

    def test_unchanged_session_answers_304_with_no_body(self, app_env):
        client, _ = make_client(app_env, [model_says("done")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "first"})
            recv_until(ws, "done")
            url = f"/offline/session?session={hello['session']}"
            first = client.get(url)
            etag = first.headers["etag"]
            again = client.get(url, headers={"If-None-Match": etag})

        assert first.status_code == 200 and etag
        assert again.status_code == 304
        assert not again.content

    def test_since_and_sig_return_only_the_new_events(self, app_env):
        client, _ = make_client(
            app_env, [model_says("answer one"), model_says("answer two")]
        )
        with client, connected(client) as (ws, hello, _):
            url = f"/offline/session?session={hello['session']}"
            ws.send_json({"type": "task", "text": "first"})
            recv_until(ws, "done")
            first = client.get(url).json()

            ws.send_json({"type": "task", "text": "second"})
            recv_until(ws, "done")
            delta = client.get(f"{url}&since={first['total']}&sig={first['sig']}").json()
            # A prefix the server can't vouch for falls back to the whole thing
            # rather than silently splicing onto a stream that moved.
            stale = client.get(f"{url}&since={first['total']}&sig=deadbeef").json()

        assert delta["base"] == first["total"]
        opening = delta["events"][0]
        assert opening["type"] == "user" and opening["text"] == "second"
        # Replayed turns carry WHEN they happened (#200), rebuilt from the log
        # record's own stamp — which is why old chats get timestamps too.
        assert opening["at"] > 0
        assert delta["events"][-1]["result"] == "answer two"
        assert delta["total"] == first["total"] + len(delta["events"])

        assert stale["base"] == 0
        assert len(stale["events"]) == stale["total"]

    def test_bulk_command_output_is_trimmed_but_the_answer_is_not(self, app_env, tmp_path):
        # The mirror keeps the conversation verbatim and caps the noise: that
        # asymmetry is what makes a full local archive affordable.
        long_answer = "conclusion. " * 2000
        path = tmp_path / "session-trim.jsonl"
        huge = "x" * (server_module.OFFLINE_OUTPUT_CAP * 3)
        records = [
            {"kind": "message", "role": "user", "content": "run it"},
            {"kind": "trace", "step": {"kind": "tool", "name": "read_file", "output": huge}},
            {"kind": "message", "role": "assistant", "content": long_answer},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in records))

        events = server_module.offline_events(path)
        step = next(e for e in events if e["type"] == "step")
        assert len(step["output"]) < server_module.OFFLINE_OUTPUT_CAP + 200
        assert "trimmed for offline" in step["output"]
        assert step["output"].startswith("x") and step["output"].endswith("x")
        # The answer is the whole point of going back to an old chat.
        assert next(e for e in events if e["type"] == "done")["result"] == long_answer.strip()

    def test_legacy_log_without_traces_still_mirrors(self, app_env, tmp_path):
        # Pre-trace logs can't reconstruct an event stream; they fall back to the
        # flat history blob the frontend already knows how to render, so old
        # conversations are mirrored too instead of being silently skipped.
        path = tmp_path / "session-legacy.jsonl"
        path.write_text(
            json.dumps({"kind": "message", "role": "user", "content": "old question"}) + "\n"
        )
        events = server_module.offline_events(path)
        assert [e["type"] for e in events] == ["history"]
        assert events[0]["messages"][0]["content"] == "old question"


class TestTurnTimestamps:
    """When a turn happened (#200), on BOTH paths.

    The data was always on disk — `_write_line` has stamped every record with an
    ISO timestamp since the first version of session.py — so this is a read, not
    a migration, and chats written long before the feature get their times back.

    It rides `at`, deliberately NOT `ts`: on a live event `ts` means "this turn
    is starting now" and drives the trace card's clock, and cold replay must
    never look like a running turn (see `_user_event`). Two names, two meanings.
    """

    def test_a_live_turn_says_when_it_happened(self, app_env):
        client, _ = make_client(app_env, [model_says("done")])
        with client, connected(client) as (ws, _hello, _):
            before = int(time.time())
            ws.send_json({"type": "task", "text": "what time is it"})
            user = recv_until(ws, "user")
            recv_until(ws, "done")
        assert user["at"] >= before
        # …and the clock origin is still its own field, untouched.
        assert user["ts"] >= before

    def test_a_replayed_turn_says_when_it_happened(self, app_env):
        client, _ = make_client(app_env, [model_says("done")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "what time is it"})
            recv_until(ws, "done")
            name = hello["session"]
        events = SessionLog.reconstruct_events(app_env["state_dir"] / name)
        user = next(e for e in events if e["type"] == "user")
        assert user["at"] > 0
        # A cold transcript must never look like a turn in flight: `ts` is the
        # live clock's origin and replay does not carry it.
        assert "ts" not in user

    def test_an_unparseable_stamp_renders_nothing_rather_than_a_wrong_time(self):
        assert session_module.record_epoch({"ts": "not a date"}) is None
        assert session_module.record_epoch({}) is None
        assert session_module.record_epoch({"ts": ""}) is None
        assert session_module.record_epoch({"ts": "2026-07-31T09:53:27"}) > 0
class TestNoGhostTraceCards:
    """#192's release blocker, verified where the artefact would actually
    appear: at a real client, LIVE and on COLD REPLAY.

    `app.js`'s `traceStep` calls `ensureTrace()` before dispatching on
    `step.kind`, so any renderless record that reaches the frontend opens an
    empty live trace card with a running ticker. The unit halves are pinned in
    test_agent.py; this pins the wiring end to end, because the two mechanisms
    only matter if the server honours both.
    """

    def _renderless_kinds(self):
        return sorted(session_module.RENDERLESS_STEPS)

    def test_no_renderless_step_reaches_a_live_client(self, app_env):
        """The history trim emits a real `trim` record on the second task — a
        genuine producer, not a synthetic probe. `num_ctx` is small so the
        history genuinely overflows: the trim is budget-gated now, and a 400
        character result on a 32k window is nowhere near any limit."""
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command="echo " + "y" * 400)]),
                model_says("first"),
                model_says("second"),
            ],
            num_ctx=128,
        )
        seen: list[dict] = []
        with connected(client) as (ws, _hello, _replay):
            ws.send_json({"type": "task", "text": "make a big result"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "trim it"})
            for _ in range(200):
                event = ws.receive_json()
                seen.append(event)
                if event["type"] == "done":
                    break

        steps = [e for e in seen if e.get("type") == "step"]
        assert steps, "sanity: the turn produced trace steps"
        # …and that renderless kinds were genuinely PRODUCED on this turn, or
        # the assertion below passes by there being nothing to catch. `trim`
        # used to be the producer here and is deliberately rendered now (#243),
        # so the guarantee rests on the ones emitted every turn.
        written = (Path(app_env["state_dir"]) / _hello["session"]).read_text()
        assert '"kind": "brief"' in written and '"kind": "reasoning"' in written
        leaked = [s for s in steps if s.get("kind") in session_module.RENDERLESS_STEPS]
        assert not leaked, f"renderless records reached a live client: {leaked}"

    def test_no_renderless_step_survives_cold_replay(self, app_env, tmp_path):
        """The other path to the same empty card: a session reopened from its
        log, reconstructed into the replay event stream."""
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command="echo " + "y" * 400)]),
                model_says("first"),
                model_says("second"),
            ],
        )
        with connected(client) as (ws, hello, _replay):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "make a big result"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "trim it"})
            recv_until(ws, "done")

        # Reopen cold, from the log alone.
        with client.websocket_connect(f"/ws?session={name}") as ws2:
            hello2 = ws2.receive_json()
            replay = ws2.receive_json()
            assert hello2["type"] == "hello" and replay["type"] == "replay"

        steps = [e for e in replay["events"] if e.get("type") == "step"]
        assert steps, "sanity: the cold session reconstructed its trace"
        leaked = [s for s in steps if s.get("kind") in session_module.RENDERLESS_STEPS]
        assert not leaked, f"renderless records survived cold replay: {leaked}"

    def test_the_record_really_was_written(self, app_env, tmp_path):
        """The negative tests above would also pass if nothing were emitted at
        all. This proves the evidence IS on disk — durable, and invisible."""
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command="echo " + "y" * 400)]),
                model_says("first"),
                model_says("second"),
            ],
            num_ctx=128,
        )
        with connected(client) as (ws, hello, _replay):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "make a big result"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "trim it"})
            recv_until(ws, "done")

        log_path = next(Path(app_env["state_dir"]).glob(f"{name}*.jsonl"), None) or (
            Path(app_env["state_dir"]) / name
        )
        raw = log_path.read_text()
        assert '"kind": "trim"' in raw, "the trim record was never written"


class TestRedactTurn:
    """A chat had no eraser (#202): the transcript is an append-only log
    replayed in full, so a probe fired at the wrong chat, a message sent by an
    autocorrect Return, or a secret pasted into the composer stayed forever —
    the only tools were deleting the whole chat or hand-editing JSONL with the
    server stopped.

    Three copies of a turn exist and all three have to go, or the removal is
    theatre: the log, the live model's context, and the in-memory transcript
    every viewer replays from.
    """

    @staticmethod
    def _turn_ids(events):
        return [e.get("turn") for e in events if e.get("type") == "user"]

    def test_a_message_can_be_removed_the_moment_it_is_sent(self, app_env):
        """The turn someone most wants back is the one they just sent, so the id
        rides the LIVE user event — not only a transcript replayed cold."""
        client, _ = make_client(app_env, [model_says("first answer"), model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "the SECRET is hunter2"})
            live = recv_until(ws, "user")
            assert live["turn"], "a live turn must name itself"
            recv_until(ws, "done")

            ws.send_json({"type": "redact", "turn": live["turn"]})
            replay = recv_until(ws, "replay")
            blob = json.dumps(replay["events"])
            assert "hunter2" not in blob
            assert any(e["type"] == "redacted" for e in replay["events"])

            # …and the model's context lost it too, or it goes on quoting what
            # the user just removed.
            session = client.app.state.server.active
            assert not any(
                "hunter2" in str(m.get("content", "")) for m in session.agent.messages
            )
            # …and the file on disk, which is what a mirror is built from.
            assert "hunter2" not in session.logref.log.path.read_text(encoding="utf-8")

    def test_the_neighbours_survive(self, app_env):
        client, _ = make_client(
            app_env, [model_says("A"), model_says("B"), model_says("C")]
        )
        with client, connected(client) as (ws, _, _):
            for text in ("first", "second", "third"):
                ws.send_json({"type": "task", "text": text})
                recv_until(ws, "done")
            session = client.app.state.server.active
            middle = self._turn_ids(session.bridge.transcript)[1]

            ws.send_json({"type": "redact", "turn": middle})
            replay = recv_until(ws, "replay")
            texts = [e["text"] for e in replay["events"] if e["type"] == "user"]
            assert texts == ["first", "third"], "a removal is not a truncation"

    def test_every_viewer_repaints(self, app_env):
        """A removal made on the phone must not leave the laptop showing the
        text — and the people watching it happen have, by definition, seen it,
        so it must not come back at them as an unread chat either."""
        client, _ = make_client(app_env, [model_says("answer")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "leaked secret"})
            turn = recv_until(ws, "user")["turn"]
            recv_until(ws, "done")
            with client.websocket_connect(
                f"/ws?token={TEST_TOKEN}&session={hello['session']}"
            ) as other:
                other.receive_json()  # hello
                other.receive_json()  # replay
                ws.send_json({"type": "redact", "turn": turn})
                repaint = recv_until(other, "replay")
                assert "leaked secret" not in json.dumps(repaint["events"])
                assert repaint["seen"] is True

    def test_it_is_refused_while_the_chat_is_working(self, app_env):
        """The turn being removed may be the one running: its records are still
        being written and agent.messages belongs to the worker thread."""
        client, _ = make_client(app_env, [model_says("answer")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "a question"})
            turn = recv_until(ws, "user")["turn"]
            recv_until(ws, "done")
            session = client.app.state.server.active
            session.busy = True
            try:
                ws.send_json({"type": "redact", "turn": turn})
                error = recv_until(ws, "error")
                assert error["type"] == "error" and "working" in error["text"]
            finally:
                session.busy = False
            assert "a question" in json.dumps(session.bridge.transcript)

    def test_an_unknown_turn_says_so_and_changes_nothing(self, app_env):
        """Already removed from another tab, or a stale id: both are 'there is
        nothing here to remove', which is the outcome asked for."""
        client, _ = make_client(app_env, [model_says("answer")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "a question"})
            recv_until(ws, "done")
            session = client.app.state.server.active
            before = list(session.bridge.transcript)

            ws.send_json({"type": "redact", "turn": "no-such-turn"})
            error = recv_until(ws, "error")
            assert error["type"] == "error" and "gone" in error["text"]
            assert session.bridge.transcript == before

    def test_a_cold_chat_can_be_cleaned_up_too(self, app_env):
        """The chat holding the message you regret is usually not the one on
        screen when you remember it — it is reopened from its log."""
        client, chat = make_client(app_env, [model_says("answer")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "the SECRET is hunter2"})
            recv_until(ws, "done")
            name = hello["session"]
        # A fresh process-level view of it: reopened from disk, not from memory.
        client2, _ = make_client(app_env, [])
        with client2, client2.websocket_connect(
            f"/ws?token={TEST_TOKEN}&session={name}"
        ) as ws2:
            ws2.receive_json()  # hello
            replay = ws2.receive_json()
            turn = self._turn_ids(replay["events"])[0]
            assert turn, "a cold-loaded turn names itself as well"
            ws2.send_json({"type": "redact", "turn": turn})
            after = recv_until(ws2, "replay")
            assert "hunter2" not in json.dumps(after["events"])
            assert "hunter2" not in (
                app_env["state_dir"] / name
            ).read_text(encoding="utf-8")


class TestRefusalIsNotAFailure:
    """An `error` event says one of two unrelated things, and until #202's
    follow-up the client could only assume the worse one.

    A TURN FAILURE (the model died, a tool blew up) ends the turn: the live
    trace closes, busy clears, Retry appears. A REFUSAL of the request you just
    made says nothing about the turn at all — but it arrived in the same shape,
    so asking to delete a message while the chat was working tore down the
    RUNNING turn's card and took Stop and Retry with it. The refusal was right;
    the collateral was total.

    The split already existed in the code and simply was not carried across the
    wire: a turn failure goes through the BRIDGE (recorded, every viewer), a
    refusal goes down the ONE socket that asked. `code: "refused"` is that fact
    made legible, stamped at the site that knows."""

    def pending_responses(self, tmp_path):
        return [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("finished"),
        ]

    def test_delete_while_working_is_refused_without_ending_the_turn(
        self, app_env, tmp_path
    ):
        """The exact report: the refusal arrived, and the running turn's card
        went with it. The turn must still be there to stop or let finish."""
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            server = client.app.state.server
            assert server.active.busy
            ws.send_json({"type": "redact", "turn": "1"})
            # Not the NEXT event: the roster plane is chatty by design and any
            # session's transition can land in between (#204).
            refusal = recv_until_refusal(ws)
            assert refusal["type"] == "error"
            assert refusal["code"] == "refused", (
                "without this the client ends the turn — closing the live trace, "
                "clearing busy, and stranding a task with no Stop and no Retry"
            )
            assert "working" in refusal["text"]
            # The turn is untouched and still answerable.
            assert server.active.busy
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_every_refusal_is_coded_and_no_turn_failure_is(self, app_env, tmp_path):
        """The rule, not one instance of it: refusals carry the code, the
        errors that really do end a turn must never carry it (they would be
        downgraded to a toast and the turn would hang busy forever)."""
        client, chat = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            refusals = [
                {"type": "rename", "name": "x", "title": "   "},
                {"type": "create_issue"},
                {"type": "banana"},
            ]
            for message in refusals:
                ws.send_json(message)
                event = recv_until(ws, "error")
                assert event.get("code") == "refused", f"uncoded refusal: {message}"
        source = (Path(server_module.__file__)).read_text(encoding="utf-8")
        # A turn failure is emitted through the bridge; grepping for the pairing
        # is what stops a future error site picking the wrong channel silently.
        for line in source.splitlines():
            if '"type": "error"' in line and "bridge.emit" in line:
                assert '"code"' not in line, (
                    f"a turn failure must not be coded as a refusal: {line.strip()}"
                )


class TestQueueIsBackendAuthoritative:
    """A queued-message chip names a message ONE chat's agent is holding, and
    its Remove button dequeues from whatever session the client is viewing.

    It used to be sent only to the client that typed it — "its own composer
    echo" — which holds right until that client looks at a different chat. The
    chip lives outside the transcript, so nothing cleared it: it followed the
    viewer into the next chat, where Remove dequeued from the WRONG session
    (silently failing to cancel anything) while the real message went on
    running. Reconstructed on attach now, like the pending-cd card beside it."""

    def busy_responses(self, tmp_path):
        return [
            model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/x")]),
            model_says("finished"),
        ]

    def test_the_queue_is_re_sent_on_attach(self, app_env, tmp_path):
        client, _ = make_client(app_env, self.busy_responses(tmp_path))
        with client:
            with connected(client) as (ws, hello, _):
                ws.send_json({"type": "task", "text": "run it"})
                request = recv_until(ws, "approval_request")
                ws.send_json({"type": "task", "text": "and then this"})
                queued = recv_until(ws, "queued")
                assert queued["text"] == "and then this"
                name = hello["session"]
            # A second device — or the same one coming back — must be told what
            # is waiting, instead of showing an empty queue over a real one.
            with client.websocket_connect(
                f"/ws?token={TEST_TOKEN}&session={name}"
            ) as ws2:
                ws2.receive_json()  # hello
                ws2.receive_json()  # replay
                requeued = ws2.receive_json()
                assert requeued["type"] == "queued"
                assert requeued["text"] == "and then this"
                assert requeued["position"] == 1
                ws2.send_json(
                    {"type": "approval", "id": request["id"], "action": "approve"}
                )
                recv_until(ws2, "done")

    def test_a_queued_chip_never_reaches_a_viewer_of_another_chat(
        self, app_env, tmp_path
    ):
        """The session firewall (#182) can only drop what it can identify, and
        an event sent straight down one socket carries no session name. Through
        the bridge it does — which is the half of the fix the client cannot
        do for itself."""
        client, _ = make_client(app_env, self.busy_responses(tmp_path))
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "task", "text": "waiting message"})
            queued = recv_until(ws, "queued")
            assert queued["session"] == hello["session"], (
                "unstamped, the client cannot tell this chip from one belonging "
                "to the chat now on screen"
            )
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")

    def test_cancelling_a_queued_message_reaches_every_viewer(
        self, app_env, tmp_path
    ):
        """The chip is a view of what the server is holding, so cancelling on
        one device has to take it off the others."""
        client, _ = make_client(app_env, self.busy_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")
            ws.send_json({"type": "task", "text": "never mind"})
            recv_until(ws, "queued")
            ws.send_json({"type": "dequeue", "text": "never mind"})
            gone = recv_until(ws, "dequeued")
            assert gone["text"] == "never mind"
            server = client.app.state.server
            assert server.active.queue == []
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")


class TestRateAnswer:
    """👍/👎 with a reason (#207). The rules engine is about to start checking
    answers before it delivers them, and the honest question about that
    machinery — *does it change anything?* — is only answerable if there is a
    record of the owner being unhappy with an answer that passed every rule.
    Nothing detected that before this."""

    def test_a_rating_is_recorded_against_the_turn_it_names(self, app_env):
        client, _ = make_client(app_env, [model_says("an answer")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "a question"})
            live = recv_until(ws, "user")
            recv_until(ws, "done")

            ws.send_json({"type": "rate", "turn": live["turn"], "rating": "down",
                          "comment": "the price was stale"})
            echo = recv_until(ws, "rating")
            assert echo["turn"] == live["turn"] and echo["rating"] == "down"
            assert echo["comment"] == "the price was stale"

        log = next(Path(app_env["state_dir"]).glob("session-*.jsonl"))
        records = [json.loads(line) for line in log.read_text().splitlines()]
        [rating] = [r for r in records if r.get("kind") == "rating"]
        assert rating["turn"] == live["turn"]
        assert rating["rating"] == "down" and rating["comment"] == "the price was stale"

    def test_a_rating_survives_a_cold_reopen(self, app_env):
        """Applied by turn id, and replayed LAST — a rating decorates a turn
        rather than being one, and it can be written long after the turn it
        names, so file order would hand the frontend a decoration for a turn it
        has not rendered."""
        client, _ = make_client(app_env, [model_says("first"), model_says("second")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "one"})
            first = recv_until(ws, "user")
            recv_until(ws, "done")
            ws.send_json({"type": "rate", "turn": first["turn"], "rating": "up"})
            recv_until(ws, "rating")
            ws.send_json({"type": "task", "text": "two"})
            recv_until(ws, "done")

        log = next(Path(app_env["state_dir"]).glob("session-*.jsonl"))
        events = SessionLog.reconstruct_events(log)
        assert events[-1]["type"] == "rating", [e["type"] for e in events]
        assert events[-1]["turn"] == first["turn"]

    def test_an_unknown_rating_is_ignored(self, app_env):
        """A closed vocabulary: the record feeds a metric, and a third value
        would silently split every count that reads it."""
        client, _ = make_client(app_env, [model_says("an answer")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "a question"})
            live = recv_until(ws, "user")
            recv_until(ws, "done")
            ws.send_json({"type": "rate", "turn": live["turn"], "rating": "meh"})
            ws.send_json({"type": "sessions"})
            recv_until(ws, "session_list")  # the socket is alive and said nothing

        log = next(Path(app_env["state_dir"]).glob("session-*.jsonl"))
        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert not [r for r in records if r.get("kind") == "rating"]

    def test_rating_puts_nothing_into_the_conversation(self, app_env):
        """Inert by design: it writes a record and nothing else. Making it act
        would turn a feedback control into a lever the model can be steered
        by — and the comment is the owner's words about an answer, which must
        never come back at him as context the model then agrees with."""
        client, _ = make_client(app_env, [model_says("an answer"), model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "a question"})
            live = recv_until(ws, "user")
            recv_until(ws, "done")
            ws.send_json({"type": "rate", "turn": live["turn"], "rating": "down",
                          "comment": "SENTINEL-not-a-prompt"})
            recv_until(ws, "rating")

        log = next(Path(app_env["state_dir"]).glob("session-*.jsonl"))
        records = [json.loads(line) for line in log.read_text().splitlines()]
        messages = [r for r in records if r.get("kind") == "message"]
        assert not [m for m in messages if "SENTINEL" in json.dumps(m)]
        assert len([r for r in records if r.get("kind") == "rating"]) == 1

    def test_a_rating_survives_a_RECONNECT_not_only_a_cold_open(self, app_env):
        """Two paths restore a chat and both must agree: a reconnect replays
        the live transcript, a cold open replays the log. A rating that
        survived one and vanished on the other is the worst of both — and the
        live path is the one someone actually hits, by locking their phone."""
        client, _ = make_client(app_env, [model_says("an answer"), model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "a question"})
            live = recv_until(ws, "user")
            recv_until(ws, "done")
            ws.send_json({"type": "rate", "turn": live["turn"], "rating": "up"})
            recv_until(ws, "rating")

        with client, connected(client) as (_ws, _hello, replay):
            ratings = [e for e in replay["events"] if e["type"] == "rating"]
            assert ratings, "a reconnect lost the rating"
            assert ratings[0]["turn"] == live["turn"] and ratings[0]["rating"] == "up"
            # Hot and cold must be byte-identical: `seen` is a delivery flag and
            # has no business in a recorded event, or replay stops matching the
            # log's own reconstruction.
            assert "seen" not in ratings[0]

    def test_an_opinion_can_be_withdrawn(self, app_env):
        """Tapping the lit thumb takes it back. A verdict you cannot undo is
        one you hesitate to give, and the count is only worth having if a
        mistap is cheap. Withdrawal is a RECORD, never a deletion — the log
        stays append-only and readers take the last one."""
        client, _ = make_client(app_env, [model_says("an answer"), model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "a question"})
            live = recv_until(ws, "user")
            recv_until(ws, "done")
            ws.send_json({"type": "rate", "turn": live["turn"], "rating": "down"})
            recv_until(ws, "rating")
            ws.send_json({"type": "rate", "turn": live["turn"], "rating": "none"})
            withdrawn = recv_until(ws, "rating")
            assert withdrawn["rating"] == "none"

        log = next(Path(app_env["state_dir"]).glob("session-*.jsonl"))
        records = [json.loads(line) for line in log.read_text().splitlines()]
        ratings = [r for r in records if r.get("kind") == "rating"]
        assert [r["rating"] for r in ratings] == ["down", "none"], "a withdrawal must not delete"

    def test_a_rating_is_delivered_exactly_once(self, app_env):
        """Recording and delivering were two calls that both fanned out, so
        every viewer received each rating twice. Harmless on screen — the
        marking is idempotent — and wrong in the transcript, where it becomes
        two entries for one tap."""
        client, _ = make_client(app_env, [model_says("an answer"), model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "a question"})
            live = recv_until(ws, "user")
            recv_until(ws, "done")
            ws.send_json({"type": "rate", "turn": live["turn"], "rating": "up"})
            recv_until(ws, "rating")
            # A second, distinguishable action: if the rating were delivered
            # twice, this would find the duplicate rather than the new event.
            ws.send_json({"type": "rate", "turn": live["turn"], "rating": "down"})
            assert recv_until(ws, "rating")["rating"] == "down"

        with client, connected(client) as (_ws, _hello, replay):
            ratings = [e for e in replay["events"] if e["type"] == "rating"]
            assert [r["rating"] for r in ratings] == ["up", "down"]

    def test_the_reason_is_replayed_with_the_rating(self, app_env):
        """Persisted is only half of it. The comment has to come BACK on
        render, or the owner reopens a chat, sees a lit thumb with no words,
        and cannot tell what he objected to — which reads as the note having
        been lost."""
        client, _ = make_client(app_env, [model_says("an answer"), model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "a question"})
            live = recv_until(ws, "user")
            recv_until(ws, "done")
            ws.send_json({"type": "rate", "turn": live["turn"], "rating": "down",
                          "comment": "it never read the page"})
            recv_until(ws, "rating")

        # Reconnect: the live transcript.
        with client, connected(client) as (_ws, _hello, replay):
            [rating] = [e for e in replay["events"] if e["type"] == "rating"]
            assert rating["comment"] == "it never read the page"

        # Cold open: reconstructed from the log. Both paths must carry it.
        log = next(Path(app_env["state_dir"]).glob("session-*.jsonl"))
        events = SessionLog.reconstruct_events(log)
        [cold] = [e for e in events if e["type"] == "rating"]
        assert cold["comment"] == "it never read the page"
        assert cold == rating, "hot and cold must agree on the whole event"


class TestBrowserCommand:
    """`/browser` over the WebSocket (#221). The wiring is what breaks: a slash
    command needs the app.js case, the WS kind, and the handler, and a missing
    one shows up only as "unknown command" in the app."""

    def test_status_comes_back_as_sheet_text(self, app_env, monkeypatch):
        from aish import browser as browser_module

        monkeypatch.setattr(
            browser_module, "command", lambda arg: f"status for {arg!r}"
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser", "arg": ""})
            assert recv_until(ws, "job_list")["text"] == "status for ''"

    def test_opening_a_window_acks_before_it_blocks(self, app_env, monkeypatch):
        """A login window parks for as long as the owner needs it. The phone
        that asked must be told the window is on the MAC, not left waiting on
        something it cannot see."""
        from aish import browser as browser_module

        monkeypatch.setattr(
            browser_module, "command", lambda arg: "signed-in sites recorded: x.pl"
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser", "arg": "https://x.pl"})
            first = recv_until(ws, "job_list")["text"]
            assert "on the Mac" in first
            assert "x.pl" in recv_until(ws, "job_list")["text"]

    def test_bookkeeping_does_not_ack_a_window_it_never_opens(self, app_env, monkeypatch):
        from aish import browser as browser_module

        monkeypatch.setattr(browser_module, "command", lambda arg: "forgot it")
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser", "arg": "forget x.pl"})
            assert recv_until(ws, "job_list")["text"] == "forgot it"


class TestBrowserView:
    """The remote browser view (#221): the Mac is headless, so this sheet is
    the window. One frame per interaction, over the socket that is already
    authenticated."""

    def _fake_view(self, monkeypatch, calls):
        from aish import browser as browser_module

        def view_open(url, width=None, height=None, cold=False):
            calls.append(("open", url, width, height, cold))
            return browser_module.Frame(jpeg=b"\xff\xd8jpeg", url=url, title="Sign in")

        def view_act(action, **kwargs):
            calls.append((action, kwargs))
            return browser_module.Frame(
                jpeg=b"\xff\xd8jpeg", url="https://x.pl/after", title="After"
            )

        def view_close():
            calls.append(("close", {}))
            return ["x.pl"]

        monkeypatch.setattr(browser_module, "view_open", view_open)
        monkeypatch.setattr(browser_module, "view_act", view_act)
        monkeypatch.setattr(browser_module, "view_close", view_close)
        # Every interaction now leaves a watcher looking for late arrivals. The
        # default here is a page that has finished, so these tests see exactly
        # the one frame they are about; the watcher's own cases are below.
        monkeypatch.setattr(
            browser_module, "view_activity",
            lambda: {"gen": -1, "nav": 0, "quiet": 9_999, "ready": True},
        )

    def _fast_watch(self, monkeypatch):
        """Real policy, compressed clock. The bounds are what make the watcher
        safe on a live site and slow in a suite; the DECISIONS are what these
        tests are about."""
        from aish import browser as browser_module

        monkeypatch.setattr(browser_module, "WATCH_POLL_MS", 5)
        monkeypatch.setattr(browser_module, "WATCH_MIN_GAP_MS", 0)

    def test_a_page_that_finishes_after_its_frame_is_sent_again(self, app_env, monkeypatch):
        """The SPINNER. Its DOM is perfectly still and `readyState` is complete,
        so the settle test called it finished and the one correction was spent
        on a picture of a spinner. The owner then had to tap the page to force
        another frame — which is the bug this exists to end."""
        from aish import browser as browser_module

        calls = []
        self._fake_view(monkeypatch, calls)
        self._fast_watch(monkeypatch)
        monkeypatch.setattr(
            browser_module, "view_activity",
            lambda: {"gen": 7, "nav": 0, "quiet": 9_999, "ready": True},
        )
        monkeypatch.setattr(
            browser_module, "view_settled_frame",
            lambda: browser_module.Frame(
                jpeg=b"\xff\xd8loaded", url="https://x.pl/after",
                title="After", gen=7,
            ),
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_view", "action": "click", "x": 5, "y": 5})
            first = recv_until(ws, "browser_view")
            second = recv_until(ws, "browser_view")
        assert base64.b64decode(first["jpeg"]) == b"\xff\xd8jpeg"
        assert base64.b64decode(second["jpeg"]) == b"\xff\xd8loaded"

    def test_a_scroll_is_watched_too(self, app_env, monkeypatch):
        """The other half of the same bug, and the one that was a plain
        omission: the correction fired for taps and navigations and not for
        scrolls, so lazily loaded images below the fold arrived to nobody."""
        from aish import browser as browser_module

        calls = []
        self._fake_view(monkeypatch, calls)
        self._fast_watch(monkeypatch)
        monkeypatch.setattr(
            browser_module, "view_activity",
            lambda: {"gen": 3, "nav": 0, "quiet": 9_999, "ready": True},
        )
        monkeypatch.setattr(
            browser_module, "view_settled_frame",
            lambda: browser_module.Frame(
                jpeg=b"\xff\xd8images", url="https://x.pl/after", title="After", gen=3,
            ),
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_view", "action": "scroll", "dy": 900})
            recv_until(ws, "browser_view")
            assert base64.b64decode(recv_until(ws, "browser_view")["jpeg"]) == (
                b"\xff\xd8images"
            )

    def test_a_finished_page_is_never_captured_a_second_time(self, app_env, monkeypatch):
        """The #223 inversion, and the reason this is cheaper than what it
        replaces. The old correction paid for a full second capture on EVERY
        interaction and then discarded it when the bytes matched; the probe
        costs milliseconds and no bytes, so a static page now pays once."""
        from aish import browser as browser_module

        calls = []
        self._fake_view(monkeypatch, calls)
        self._fast_watch(monkeypatch)
        captures = []
        monkeypatch.setattr(
            browser_module, "view_settled_frame",
            lambda: captures.append(1) or browser_module.Frame(jpeg=b"\xff\xd8x", url="", title=""),
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_view", "action": "click", "x": 5, "y": 5})
            recv_until(ws, "browser_view")
            # A second interaction round-trips, so every poll the first one was
            # going to make has already happened by the time this returns.
            ws.send_json({"type": "browser_view", "action": "click", "x": 6, "y": 6})
            recv_until(ws, "browser_view")
        assert captures == []

    def test_an_already_finished_page_is_not_watched_at_all(
        self, app_env, monkeypatch
    ):
        """#223, fix 1. Every interaction used to leave a watcher, including on
        a page that had already stopped moving before the shutter fell — where
        it can only find what it already has. The frame reports what the probe
        said AT CAPTURE, and a `settled` one is answered with no task, no polls
        and no second capture.

        The rid/ack pair is what makes this deterministic: the receipt is sent
        after the handler returns, so the watcher has been scheduled or not by
        the time it arrives."""
        from aish import browser as browser_module

        probes = []

        def view_act(action, **kwargs):
            return browser_module.Frame(
                jpeg=b"\xff\xd8done", url="https://x.pl/", title="Done", settled=True,
            )

        monkeypatch.setattr(browser_module, "view_act", view_act)
        monkeypatch.setattr(
            browser_module, "view_activity",
            lambda: probes.append(1) or {"gen": 0, "nav": 0, "quiet": 9, "ready": True},
        )
        self._fast_watch(monkeypatch)
        client, _ = make_client(app_env, [])
        server = client.app.state.server
        with client, connected(client) as (ws, _, _):
            ws.send_json({
                "type": "browser_view", "action": "scroll", "dy": 900, "rid": "w1",
            })
            assert recv_until(ws, "ack")["rid"] == "w1"
            assert server._view_watch is None
        assert probes == []

    def test_a_page_that_had_not_finished_is_still_watched(
        self, app_env, monkeypatch
    ):
        """The other direction, and the one that matters: `settled` is FALSE for
        anything less than certain — a page that would not answer the probe, one
        still parsing, one merely momentarily quiet, one with a request on the
        wire. All of those keep the watcher they had."""
        calls = []
        self._fake_view(monkeypatch, calls)
        self._fast_watch(monkeypatch)
        client, _ = make_client(app_env, [])
        server = client.app.state.server
        with client, connected(client) as (ws, _, _):
            ws.send_json({
                "type": "browser_view", "action": "scroll", "dy": 900, "rid": "w2",
            })
            assert recv_until(ws, "ack")["rid"] == "w2"
            assert server._view_watch is not None

    def test_acting_again_supersedes_the_watcher(self, app_env, monkeypatch):
        """Whatever the old page was about to become, they have moved on — and a
        frame captured for the PREVIOUS action would land on top of the new one,
        which on a page they have just navigated away from is worse than stale.

        Drives the real interleaving rather than a decision function: the
        capture is held open until the next action has been handled, so the
        thing under test is a watcher that is genuinely mid-flight."""
        import threading
        import time

        from aish import browser as browser_module

        gate = threading.Event()
        capturing = threading.Event()

        def view_act(action, **kwargs):
            return browser_module.Frame(
                jpeg=f"frame-{action}".encode(), url="https://x.pl/", title=action,
            )

        def view_settled_frame():
            capturing.set()
            gate.wait(5)
            return browser_module.Frame(jpeg=b"stale", url="", title="", gen=99)

        monkeypatch.setattr(browser_module, "view_act", view_act)
        monkeypatch.setattr(browser_module, "view_settled_frame", view_settled_frame)
        monkeypatch.setattr(
            browser_module, "view_activity",
            lambda: {"gen": 99, "nav": 0, "quiet": 9_999, "ready": True},
        )
        self._fast_watch(monkeypatch)
        client, _ = make_client(app_env, [])
        server = client.app.state.server
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_view", "action": "click", "x": 5, "y": 5})
            first = recv_until(ws, "browser_view")
            assert capturing.wait(5)          # the watcher is mid-capture
            superseded = server._view_watch
            ws.send_json({"type": "browser_view", "action": "goto", "url": "https://y.pl"})
            second = recv_until(ws, "browser_view")
            gate.set()                        # the held capture completes — too late
            for _ in range(500):
                if superseded.done():
                    break
                time.sleep(0.01)
            assert superseded.cancelled(), "the superseded watcher went on watching"
            ws.send_json({"type": "browser_view", "action": "refresh"})
            third = recv_until(ws, "browser_view")
        seen = [base64.b64decode(e["jpeg"]) for e in (first, second, third)]
        assert seen == [b"frame-click", b"frame-goto", b"frame-refresh"]

    def test_detail_sharpens_one_rectangle_and_echoes_its_token(
        self, app_env, monkeypatch
    ):
        """Zooming past what a frame carries fetches THAT rectangle, sharp.

        The token rides back untouched: the client stamps each request with the
        frame it was aiming at, and a patch that arrives after the page moved on
        must be droppable rather than painted over it."""
        from aish import browser as browser_module

        asked = {}

        def view_detail(x, y, w, h, scale):
            asked.update(x=x, y=y, w=w, h=h, scale=scale)
            return browser_module.Detail(
                jpeg=b"\xff\xd8patch", x=320, y=480, width=640, height=975,
                scale=2.5, nav=3,
            )

        monkeypatch.setattr(browser_module, "view_detail", view_detail)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({
                "type": "browser_view", "action": "detail",
                "x": 300, "y": 460, "w": 660, "h": 1000, "scale": 2.52,
                "token": 7,
            })
            event = recv_until(ws, "browser_view")
        assert event["action"] == "detail"
        assert base64.b64decode(event["jpeg"]) == b"\xff\xd8patch"
        assert (event["x"], event["y"], event["w"], event["h"]) == (320, 480, 640, 975)
        assert event["scale"] == 2.5
        assert event["token"] == 7
        assert asked == {"x": 300, "y": 460, "w": 660, "h": 1000, "scale": 2.52}

    def test_a_detail_that_cannot_be_taken_says_nothing(self, app_env, monkeypatch):
        """A sharpening that failed leaves the frame it was sharpening, which is
        still the page. An error line would report a problem on a view that is
        working — and the previous action's spinner is not this one's to clear.

        Asserted by what comes NEXT: a following action's reply must be the
        first browser_view seen, so anything the detail sent would show up here.
        """
        from aish import browser as browser_module

        def boom(*a, **k):
            raise RuntimeError("chrome went away")

        calls = []
        self._fake_view(monkeypatch, calls)
        monkeypatch.setattr(browser_module, "view_detail", boom)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({
                "type": "browser_view", "action": "detail",
                "x": 0, "y": 0, "w": 100, "h": 100, "scale": 2, "token": 1,
            })
            ws.send_json({"type": "browser_view", "action": "close"})
            event = recv_until(ws, "browser_view")
        assert event["action"] == "closed"

    def test_no_view_open_sends_no_patch(self, app_env, monkeypatch):
        """`view_detail` answers None when there is nothing to capture, and a
        patch of nothing is not a message."""
        from aish import browser as browser_module

        calls = []
        self._fake_view(monkeypatch, calls)
        monkeypatch.setattr(browser_module, "view_detail", lambda *a, **k: None)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({
                "type": "browser_view", "action": "detail",
                "x": 0, "y": 0, "w": 100, "h": 100, "scale": 2, "token": 1,
            })
            ws.send_json({"type": "browser_view", "action": "close"})
            event = recv_until(ws, "browser_view")
        assert event["action"] == "closed"

    def test_open_returns_a_frame(self, app_env, monkeypatch):
        calls = []
        self._fake_view(monkeypatch, calls)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({
                "type": "browser_view", "action": "open", "url": "https://x.pl",
                "width": 430, "height": 900,
            })
            event = recv_until(ws, "browser_view")
            assert event["action"] == "frame"
            assert base64.b64decode(event["jpeg"]) == b"\xff\xd8jpeg"
            assert event["title"] == "Sign in"
            assert calls == [("open", "https://x.pl", 430, 900, False)]

    def test_the_client_says_which_profile_the_view_drives(self, app_env, monkeypatch):
        """Sent by the client, never inferred from the URL (#264).

        Signing the search profile in happens at accounts.google.com — the same
        address the owner would use for his own — so the address cannot say
        which browser is meant. Getting this wrong types a password into the
        wrong profile, which nothing else would reveal."""
        calls = []
        self._fake_view(monkeypatch, calls)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({
                "type": "browser_view", "action": "open",
                "url": "https://accounts.google.com", "profile": "search",
            })
            recv_until(ws, "browser_view")
            assert calls[0][-1] is True

    def test_a_click_carries_its_coordinates(self, app_env, monkeypatch):
        calls = []
        self._fake_view(monkeypatch, calls)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_view", "action": "click", "x": 512, "y": 700})
            assert recv_until(ws, "browser_view")["action"] == "frame"
            action, kwargs = calls[0]
            assert action == "click"
            assert (kwargs["x"], kwargs["y"]) == (512, 700)

    def test_typed_text_is_never_logged(self, app_env, monkeypatch):
        """The owner types real passwords through this path. It must leave no
        trace in the session log — not as a message, not as a trace step."""
        calls = []
        self._fake_view(monkeypatch, calls)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "browser_view", "action": "type", "text": "hunter2-secret"}
            )
            recv_until(ws, "browser_view")
        for log in Path(app_env["state_dir"]).glob("session-*.jsonl"):
            assert "hunter2-secret" not in log.read_text()

    def test_closing_reports_what_it_WATCHED_and_asks_nothing(
        self, app_env, monkeypatch
    ):
        """Nothing is asked any more. Whether he is signed in stopped being a
        fact anybody asserts the moment aish started reading it off the page.
        Three versions of the question were wrong before it went — inferred
        from a visit, inferred from a close, then offered as the whole browsing
        history under one batch yes — and the close now REPORTS an observation
        instead of asking for a claim."""
        calls = []
        self._fake_view(monkeypatch, calls)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_view", "action": "close"})
            event = recv_until(ws, "browser_view")
            assert event["action"] == "closed"
            assert event["hosts"] == ["x.pl"]

    def test_a_failed_navigation_is_reported_not_painted_white(
        self, app_env, monkeypatch
    ):
        """A goto that throws navigates NOWHERE, so the frame is a white
        about:blank. Sending it with an empty address bar and no explanation is
        what made the view look broken."""
        from aish import browser as browser_module

        monkeypatch.setattr(
            browser_module,
            "view_open",
            lambda url, width=None, height=None, cold=False: browser_module.Frame(
                jpeg=b"\xff\xd8", url="about:blank", title="",
                error="could not open https://x.pl (TimeoutError)",
            ),
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_view", "action": "open", "url": "https://x.pl"})
            event = recv_until(ws, "browser_view")
            assert event["action"] == "frame"
            assert "could not open" in event["error"]

    def test_a_failure_comes_back_as_an_error_not_a_dead_sheet(self, app_env, monkeypatch):
        from aish import browser as browser_module

        def boom(url, width=None, height=None, cold=False):
            raise browser_module.BrowserUnavailable("Playwright is not installed")

        monkeypatch.setattr(browser_module, "view_open", boom)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_view", "action": "open", "url": "https://x.pl"})
            event = recv_until(ws, "browser_view")
            assert event["action"] == "error"
            assert "Playwright" in event["error"]

    def test_opening_his_own_browser_says_what_it_closes(self, app_env, monkeypatch):
        """`/browser` OUTRANKS the model's tabs and takes the whole Chrome with
        it: `view_open` relaunches the context view-shaped and `close_now`
        empties `browse_pages`. Doing that silently is the thing #289 asks it
        not to do — a chat mid-flow simply discovers, a minute later, that its
        page is gone.

        Counted BEFORE the open, because afterwards there is nothing to count."""
        from aish import browser as browser_module

        calls = []
        self._fake_view(monkeypatch, calls)
        monkeypatch.setattr(browser_module, "browse_tab_count", lambda: 2)
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_view", "action": "open", "url": "https://x.pl"})
            event = recv_until(ws, "browser_view")
        assert event["closed_pages"] == 2

    def test_an_ordinary_frame_claims_no_closures(self, app_env, monkeypatch):
        """Omitted when it is none, so every other frame is byte-identical to
        what it always was — and a zero would read as a claim that the count
        was taken, which on any action but `open` it is not."""
        from aish import browser as browser_module

        calls = []
        counted = []
        self._fake_view(monkeypatch, calls)
        monkeypatch.setattr(
            browser_module, "browse_tab_count", lambda: counted.append(1) or 3
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_view", "action": "click", "x": 1, "y": 2})
            event = recv_until(ws, "browser_view")
        assert "closed_pages" not in event
        assert counted == []   # a click closes nothing, so nothing is counted


class TestBrowseWatch:
    """Live watch (#289 slice 2): a read-only window onto the page THIS CHAT is
    driving, for an owner who otherwise cannot see it.

    It is a mode of the same sheet and emphatically not the remote view — that
    one relaunches the whole context and destroys every chat's tab. The capture
    itself is `browser.browse_watch_frame` (`tests/test_browser.py`); what is
    driven here is the half that decides WHEN one is taken and WHO gets it.
    """

    JPEG = b"\xff\xd8first"
    NEXT = b"\xff\xd8second"

    def _fast(self, monkeypatch):
        """Real loop, compressed clock. The interval is what makes the watcher
        cheap on a live box; the decisions are what these tests are about."""
        monkeypatch.setattr(server_module, "BROWSE_WATCH_INTERVAL_S", 0.005)

    def _script(self, monkeypatch, results):
        """Answer each poll from `results`, repeating the last one forever.
        Records the browse key every poll asked about."""
        from aish import browser as browser_module

        asked = []

        def watch(key):
            asked.append(key)
            i = min(len(asked) - 1, len(results) - 1)
            return results[i]

        monkeypatch.setattr(browser_module, "browse_watch_frame", watch)
        return asked

    def _frame(self, jpeg, url="https://eon.pl/mojeon", title="Moje eON"):
        from aish import browser as browser_module

        return (browser_module.WatchFrame(jpeg=jpeg, url=url, title=title), "")

    def test_the_owner_sees_the_page_this_chat_is_driving(self, app_env, monkeypatch):
        self._fast(monkeypatch)
        asked = self._script(monkeypatch, [self._frame(self.JPEG)])
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "browser_watch", "action": "start"})
            event = recv_until(ws, "browser_watch")
            session = client.app.state.server.sessions[hello["session"]]
        assert event["action"] == "frame"
        assert base64.b64decode(event["jpeg"]) == self.JPEG
        assert event["url"] == "https://eon.pl/mojeon"
        assert event["title"] == "Moje eON"
        # THE CHAT'S OWN TAB. The key is the chat's `BrowseView` key (#272), read
        # off the agent rather than taken from the wire — a chat a client is not
        # viewing is a chat it has no business photographing, and taking a name
        # from the message would be the one way to say otherwise.
        assert asked and asked[0] == session.agent.browse_key
        # And the frame is STAMPED, so app.js's existing session firewall drops
        # one belonging to a chat that is no longer on screen (L5).
        assert event["session"] == hello["session"]

    def test_an_unchanged_page_costs_no_bytes(self, app_env, monkeypatch):
        """A picture a second is affordable because the identical ones are
        dropped before the socket: a page that is not moving costs the owner
        loop a capture and the phone nothing."""
        self._fast(monkeypatch)
        self._script(
            monkeypatch,
            [self._frame(self.JPEG), self._frame(self.JPEG), self._frame(self.NEXT)],
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_watch", "action": "start"})
            first = recv_until(ws, "browser_watch")
            second = recv_until(ws, "browser_watch")
        assert base64.b64decode(first["jpeg"]) == self.JPEG
        assert base64.b64decode(second["jpeg"]) == self.NEXT

    def test_no_page_says_which_absence_and_says_it_once(self, app_env, monkeypatch):
        """A chat between pages is the ordinary case — aish navigates — so it is
        NOT an ending. It says so once, because repeating it every second is a
        stream of its own; then the next real frame lands normally."""
        from aish import browser as browser_module

        self._fast(monkeypatch)
        self._script(
            monkeypatch,
            [
                (None, browser_module.WATCH_NO_PAGE),
                (None, browser_module.WATCH_NO_PAGE),
                self._frame(self.JPEG),
            ],
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_watch", "action": "start"})
            first = recv_until(ws, "browser_watch")
            second = recv_until(ws, "browser_watch")
        assert first == {
            **first,
            "action": "idle",
            "reason": browser_module.WATCH_NO_PAGE,
        }
        # The repeat is swallowed; the next event is the page arriving.
        assert second["action"] == "frame"

    def test_his_own_browser_taking_the_page_is_reported_not_streamed(
        self, app_env, monkeypatch
    ):
        """Never stream a frame while the owner's hands are on the browser.
        The refusal is the capture's own (one line, `owner.view is not None`);
        what this pins is that the watcher reports it rather than going blank,
        and that it PICKS BACK UP — a watch does not die because he looked at
        his browser for a minute."""
        from aish import browser as browser_module

        self._fast(monkeypatch)
        self._script(
            monkeypatch,
            [(None, browser_module.WATCH_HANDS), self._frame(self.JPEG)],
        )
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "browser_watch", "action": "start"})
            first = recv_until(ws, "browser_watch")
            second = recv_until(ws, "browser_watch")
        assert (first["action"], first["reason"]) == ("idle", browser_module.WATCH_HANDS)
        assert second["action"] == "frame"

    def test_stopping_ends_the_watcher(self, app_env, monkeypatch):
        """Watchers run only while a viewer has the sheet open — otherwise every
        background flow pays a screenshot tax on a 16 GB box."""
        self._fast(monkeypatch)
        self._script(monkeypatch, [self._frame(self.JPEG)])
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            server = client.app.state.server
            ws.send_json({"type": "browser_watch", "action": "start"})
            recv_until(ws, "browser_watch")
            assert hello["session"] in server._browse_watchers
            ws.send_json({"type": "browser_watch", "action": "stop", "rid": "1"})
            recv_until(ws, "ack")
            assert not any(c.watching for c in server.clients)
            assert hello["session"] not in server._browse_watchers

    def test_leaving_the_chat_ends_the_watch(self, app_env, monkeypatch):
        """A watch belongs to the chat being LEFT, so it ends in `_leave` — the
        one place both a session switch and a disconnect already go through.
        Without that a client that switched chats would keep a stream running
        that its own session firewall then drops on arrival."""
        self._fast(monkeypatch)
        self._script(monkeypatch, [self._frame(self.JPEG)])
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            server = client.app.state.server
            ws.send_json({"type": "browser_watch", "action": "start"})
            recv_until(ws, "browser_watch")
            ws.send_json({"type": "new"})            # a different chat
            recv_until(ws, "hello")
            assert hello["session"] not in server._browse_watchers
            assert not any(c.watching == hello["session"] for c in server.clients)

    def test_a_disconnect_ends_it_too(self, app_env, monkeypatch):
        self._fast(monkeypatch)
        self._script(monkeypatch, [self._frame(self.JPEG)])
        client, _ = make_client(app_env, [])
        with client:
            with connected(client) as (ws, hello, _):
                ws.send_json({"type": "browser_watch", "action": "start"})
                recv_until(ws, "browser_watch")
            server = client.app.state.server
            assert hello["session"] not in server._browse_watchers

    def test_watching_claims_no_control_of_the_chat(self, app_env, monkeypatch):
        """#295 P1: reading is free and the unit of consent is a consequence.
        Watching has none — it draws no card, and it does not take the chat off
        whoever was driving it."""
        self._fast(monkeypatch)
        self._script(monkeypatch, [self._frame(self.JPEG)])
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, _):
            server = client.app.state.server
            session = server.sessions[hello["session"]]
            session.controller = None
            ws.send_json({"type": "browser_watch", "action": "start"})
            recv_until(ws, "browser_watch")
            assert session.controller is None

    def test_the_watch_channel_has_no_verb_that_touches_a_page(self):
        """The read-only guarantee, stated as what the code enforces rather than
        as an intention: this handler accepts `start` and `stop`, neither of
        which carries a coordinate, a size, a key or a URL — so there is nothing
        here to express a click, a scroll, a keystroke or a resize with. The
        only browser function it can reach is the screenshot.

        Source-level on purpose. The failure this guards against is a verb
        somebody adds later and nobody thinks to drive."""
        source = inspect.getsource(server_module.WebServer._browse_watch)
        assert set(re.findall(r'action [!=]= "(\w+)"', source)) == {"start", "stop"}
        # And the action is the ONLY thing read off the wire: no coordinate, no
        # size, no key, no URL — so there is nothing to express a page touch
        # with even if a branch wanted to. The chat comes from `client.viewing`.
        assert re.findall(r"message\.get\(([^)]*)\)", source) == ['"action", ""']
        loop = inspect.getsource(server_module.WebServer._watch_browse)
        reached = {
            name for name in dir(server_module.browser)
            if not name.startswith("_") and f"browser.{name}" in loop
        }
        assert reached == {"browse_watch_frame"}, reached

    def test_a_device_that_cannot_keep_up_is_skipped_not_queued_behind(
        self, app_env, monkeypatch
    ):
        """A frame is ~67 KB and an outbox is unbounded, so a phone on a link
        slower than the stream would grow a backlog of stale pictures for as
        long as the sheet is left open. Skipping is the right way to be wrong
        about a live view.

        The half that makes it safe rather than a hole is what this drives: the
        watcher advances "what is on screen" only when somebody took the
        picture, so a frame dropped for backlog is offered again — otherwise a
        client that missed one would sit on a blank sheet forever the moment
        the page stopped changing."""
        server = server_module.WebServer.__new__(server_module.WebServer)
        client = server_module.Client.__new__(server_module.Client)
        client.watching = "chat"
        client.outbox = asyncio.Queue()
        server.clients = {client}
        for _ in range(server_module.BROWSE_WATCH_MAX_BACKLOG):
            client.outbox.put_nowait({"type": "token"})
        took = server._to_watchers(
            "chat", {"type": "browser_watch", "action": "frame"}, drop_when_behind=True
        )
        assert took == 0
        # An ABSENCE is one line and always gets through: it is the sentence
        # that says why the picture stopped, which is exactly what a backed-up
        # client most needs.
        assert server._to_watchers("chat", {"type": "browser_watch", "action": "idle"})
        while not client.outbox.empty():
            client.outbox.get_nowait()
        assert server._to_watchers(
            "chat", {"type": "browser_watch", "action": "frame"}, drop_when_behind=True
        ) == 1

    def test_the_model_knows_it_can_be_watched(self):
        """aish answers questions about itself, so a user-visible capability it
        has never heard of is one the owner has to discover on his own. Stated
        as what it IS — a window — because the tempting misreading is that the
        owner can now step in, which is a later slice with its own approval."""
        context = server_module.web_usage_context(
            "model", "ollama", "/allow", "/deny", "/state"
        )
        assert "/watch" in context
        assert "read-only window" in context
        assert "not a way for them to click" in context

    def test_a_live_frame_is_never_recorded(self, app_env, monkeypatch):
        """Live frames go to the socket and nowhere else. Not through the
        session Bridge — which would record them into the transcript and replay
        them to a viewer with no sheet open — and never to disk. Same reason
        console output bypasses the bridge: a picture is not transcript."""
        self._fast(monkeypatch)
        self._script(monkeypatch, [self._frame(self.JPEG), self._frame(self.NEXT)])
        client, _ = make_client(app_env, [model_says("watched")])
        with client, connected(client) as (ws, hello, _):
            server = client.app.state.server
            session = server.sessions[hello["session"]]
            ws.send_json({"type": "browser_watch", "action": "start"})
            recv_until(ws, "browser_watch")
            recv_until(ws, "browser_watch")
            before = len(session.bridge.transcript)
            # A real turn AFTER the frames, so the log exists to be checked and
            # the transcript is known to be growing for other reasons — a
            # never-written log would make the on-disk half vacuously true.
            ws.send_json({"type": "task", "text": "hi"})
            recv_until(ws, "done")
            assert len(session.bridge.transcript) > before
            assert not any(
                e["type"].startswith("browser_") for e in session.bridge.transcript
            )
        text = (server.state_dir / hello["session"]).read_text(encoding="utf-8")
        assert "browser_watch" not in text
        assert base64.b64encode(self.JPEG).decode("ascii") not in text


class TestExplainEndpoint:
    """One turn's dossier over HTTP (#243) — the consumption half of the reader.

    Deliberately not transcript events: the offline mirror writes those into
    IndexedDB on every device, and reasoning quotes fetched pages, file contents
    and mail bodies."""

    def test_it_returns_the_turn_the_id_names(self, app_env):
        client, _ = make_client(app_env, [model_says("the recommendation")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "what should I do?"})
            recv_until(ws, "done")
            doc = client.get(f"/explain?session={hello['session']}").json()

        assert doc["prompt"] == "what should I do?"
        assert doc["produced"]["answer"] == "the recommendation"
        assert doc["session"] == hello["session"]
        assert doc["turns"] == 1
        # Addressable by the id it reports, which is the point of reporting it.
        assert doc["turn_id"]

    def test_the_turn_id_is_what_addresses_a_turn(self, app_env):
        """An ordinal cannot be computed by a browser — its first paint is
        bounded, so "turn 4" on screen is not turn 4 in the log on a long chat.
        An id cannot be counted wrong."""
        client, _ = make_client(app_env, [model_says("first"), model_says("second")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "one"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "two"})
            recv_until(ws, "done")
            base = f"/explain?session={hello['session']}"
            second = client.get(f"{base}&turn=2").json()
            by_id = client.get(f"{base}&turn={second['turn_id']}").json()

        assert second["prompt"] == "two"
        assert by_id["prompt"] == "two"
        assert by_id["turn_id"] == second["turn_id"]

    def test_no_turn_ref_gives_the_LAST_turn(self, app_env):
        """What the panel opens to when you tap the newest answer."""
        client, _ = make_client(app_env, [model_says("first"), model_says("second")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "one"})
            recv_until(ws, "done")
            ws.send_json({"type": "task", "text": "two"})
            recv_until(ws, "done")
            doc = client.get(f"/explain?session={hello['session']}").json()

        assert doc["prompt"] == "two"

    def test_it_is_unreachable_without_the_token(self, app_env):
        client, _ = make_client(app_env, [model_says("done")], token="secret")
        with client, connected(client, "/ws?token=secret") as (ws, hello, _):
            ws.send_json({"type": "task", "text": "hello"})
            recv_until(ws, "done")
            assert client.get(f"/explain?session={hello['session']}").status_code == 403
            ok = client.get(f"/explain?session={hello['session']}&token=secret")
            assert ok.status_code == 200

    def test_nothing_from_it_may_be_cached_on_the_device(self, app_env):
        """Two client caches, and the service worker's NEVER_CACHE list closes
        only one — it makes the SW pass the request through, and the response
        then meets the browser's own HTTP cache."""
        client, _ = make_client(app_env, [model_says("done")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "hello"})
            recv_until(ws, "done")
            response = client.get(f"/explain?session={hello['session']}")

        assert response.headers["cache-control"] == "no-store"
        sw = (pathlib.Path(__file__).resolve().parents[1] / "aish/static/sw.js").read_text()
        never = sw.split("const NEVER_CACHE = ")[1].split("\n")[0]
        assert '"/explain"' in never

    def test_unknown_session_and_traversal_are_refused(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/explain?session=nope.jsonl").status_code == 404
            assert client.get(
                "/explain?session=session-../../etc/passwd"
            ).status_code == 404

    def test_an_unknown_turn_is_404_and_says_how_many_there_are(self, app_env):
        client, _ = make_client(app_env, [model_says("done")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "hello"})
            recv_until(ws, "done")
            missing = client.get(f"/explain?session={hello['session']}&turn=9")

        assert missing.status_code == 404
        assert missing.json()["turns"] == 1

    def test_raw_records_are_opt_in_and_report_what_they_elided(self, app_env):
        """The section exists so a rendering can be checked against its source,
        so a truncated list presented as the whole would defeat it."""
        client, _ = make_client(app_env, [model_says("done")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "hello"})
            recv_until(ws, "done")
            plain = client.get(f"/explain?session={hello['session']}").json()
            raw = client.get(f"/explain?session={hello['session']}&raw=1").json()

        assert "raw" not in plain
        assert raw["raw"]["records"]
        assert raw["raw"]["elided"] == 0

    def test_running_is_stamped_by_the_server_not_the_reader(self, app_env):
        """A turn with no task_end was interrupted OR is still going, and the
        log cannot tell them apart. Only the live server knows, so it rides the
        envelope — teaching the reader to ask would break its purity."""
        client, _ = make_client(app_env, [model_says("done")])
        with client, connected(client) as (ws, hello, _):
            ws.send_json({"type": "task", "text": "hello"})
            recv_until(ws, "done")
            doc = client.get(f"/explain?session={hello['session']}").json()

        assert doc["running"] is False
        assert "running" not in doc["produced"]


class TestOneSessionPathCheck:
    """Five hand-rolled copies of the same name check became one (#178 §11, #309).

    Every by-name endpoint — resume, delete, rename, export, the offline mirror
    — turns a chat name straight off the wire into a path in the state
    directory. Each wrote out `startswith("session-")`, `endswith(".jsonl")`,
    `"/" not in name` and `".." in name` for itself, so a fix to one of them was
    a fix to one of them. `safe_session_path` is the single answer; the
    endpoint-specific part (does the file have to EXIST?) stays at the call site
    because the callers disagree about it on purpose."""

    def test_a_plain_name_resolves_into_the_state_dir(self, tmp_path):
        assert safe_session_path(tmp_path, "session-20200101-000000-000000.jsonl") == (
            tmp_path / "session-20200101-000000-000000.jsonl"
        )

    def test_existence_is_not_this_functions_question(self, tmp_path):
        """A rename or a delete may name an OPEN chat that has not written its
        log yet, so folding existence in here would break three call sites."""
        assert safe_session_path(tmp_path, "session-nope.jsonl") is not None

    def test_traversal_is_refused(self, tmp_path):
        for name in (
            "../../../etc/passwd",
            "session-../../etc/passwd.jsonl",
            "sub/session-x.jsonl",
            "session-x.jsonl/../../y",
        ):
            assert safe_session_path(tmp_path, name) is None, name

    def test_a_name_that_is_not_a_chat_log_is_refused(self, tmp_path):
        for name in ("", "notes.txt", "session-x.txt", "other-x.jsonl", "session-x.jsonl.bak"):
            assert safe_session_path(tmp_path, name) is None, name

    def test_an_absolute_name_is_refused(self, tmp_path):
        assert safe_session_path(tmp_path, "/etc/session-x.jsonl") is None

    def test_a_log_symlinked_out_of_the_state_dir_is_refused(self, tmp_path):
        """New, and in the conservative direction: the containment now runs
        through files.contains, so a name that passes every character rule and
        still lands outside the state directory is refused."""
        state = tmp_path / "state"
        state.mkdir()
        outside = tmp_path / "elsewhere.jsonl"
        outside.write_text("{}\n")
        (state / "session-linked.jsonl").symlink_to(outside)
        assert safe_session_path(state, "session-linked.jsonl") is None


class _FakeSocket:
    """A websocket that answers handle_ws's handshake and can be told to die on
    a given send — the one thing a TestClient socket cannot do deterministically.

    Sends are counted: `fail_at=1` dies on the replay (the hello landed),
    `fail_at=2` completes the whole attach and dies on the first LIVE event, so
    the client stays a viewer with a socket nobody can write to. `error`
    defaults to uvicorn's ClientDisconnected — what production actually raises.
    Incoming messages arrive through `push`, so this drives a real connection.
    """

    def __init__(self, *, session: str = "", fail_at: int | None = None, error=None):
        self.headers: dict[str, str] = {}
        self.query_params = {"token": TEST_TOKEN}
        if session:
            self.query_params["session"] = session
        self.sent: list[dict] = []
        self.fail_at = fail_at
        self.error = error if error is not None else ClientDisconnected()
        self.closed: int | None = None
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def accept(self) -> None:
        pass

    async def close(self, code: int | None = None) -> None:
        self.closed = code

    async def send_json(self, data) -> None:
        if self.fail_at is not None and len(self.sent) >= self.fail_at:
            raise self.error
        self.sent.append(data)

    async def receive_json(self):
        message = await self.inbox.get()
        if message is None:
            raise WebSocketDisconnect(1000)
        return message

    def push(self, server, message) -> None:
        """Deliver a client message (or None to hang up) from the test thread."""
        server.loop.call_soon_threadsafe(self.inbox.put_nowait, message)


def _serve(server, socket):
    """Run one connection through the REAL handle_ws on the app's own loop."""
    return asyncio.run_coroutine_threadsafe(server.handle_ws(socket), server.loop)


def _command_records(app_env) -> list[dict]:
    """Every kind:"command" decision record this app wrote (same scan as
    TestCardLatency, reused here for the card nobody was ever shown)."""
    records = []
    for log in sorted(app_env["state_dir"].glob("session-*.jsonl")):
        for line in log.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("kind") == "command":
                records.append(record)
    return records


def _wait_until(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition never became true")


class TestClientVanishes:
    """#315: a client that goes away mid-send.

    Two halves. The filed one is cosmetic — a closed tab raising
    ClientDisconnected out of the replay logged a full unhandled-ASGI
    traceback. The one that matters is the approval card: a socket dying around
    an open gate must never let the action through, and must never cost the
    other viewers their card.
    """

    def responses(self, command):
        return [
            model_says(tool_calls=[tool_call("run_command", command=command)]),
            model_says("finished"),
        ]

    def test_disconnect_mid_replay_ends_the_attach_quietly(self, app_env, caplog):
        client, _ = make_client(app_env, [])
        with client:
            server = client.app.state.server
            socket = _FakeSocket(fail_at=1)  # hello lands, the replay does not
            with caplog.at_level(logging.DEBUG, logger="aish.web"):
                _serve(server, socket).result(timeout=5)  # returns, does not raise
            ours = [r for r in caplog.records if r.name == "aish.web"]
            assert ours, "a disconnect mid-replay must still say so once"
            assert all(r.exc_info is None for r in ours)  # a line, not a traceback

    def test_a_failed_attach_leaves_nothing_half_registered(self, app_env):
        """The half that made the traceback more than cosmetic: _attach ran
        OUTSIDE the try/finally, so a raise there skipped _detach and left a
        ghost in the viewer set forever — and the viewer set is what decides
        whether the owner is told a chat is holding on an approval."""
        client, _ = make_client(app_env, [])
        with client:
            server = client.app.state.server
            _serve(server, _FakeSocket(fail_at=1)).result(timeout=5)
            assert server.clients == set()
            assert all(not s.viewers for s in server.sessions.values())

    def test_a_genuine_send_failure_still_surfaces(self, app_env):
        """A disconnect is one specific exception. A serialization error is a
        bug in what we are sending and must NOT be swallowed with it — while
        the connection is still torn down cleanly."""
        client, _ = make_client(app_env, [])
        with client:
            server = client.app.state.server
            socket = _FakeSocket(fail_at=1, error=TypeError("not serializable"))
            with pytest.raises(TypeError):
                _serve(server, socket).result(timeout=5)
            assert server.clients == set()

    def test_a_dead_socket_at_the_card_never_runs_the_command(self, app_env, tmp_path):
        """THE one. The gate's outcome must not be decided by a network event:
        the card is emitted into each viewer's OUTBOX, never written to a socket
        by the parked worker, so a client vanishing at that instant cannot
        approve, cannot skip the gate, and cannot lose the request."""
        marker = tmp_path / "unapproved"
        client, chat = make_client(app_env, self.responses(f"touch {marker}"))
        with client:
            server = client.app.state.server
            name = server._default.name
            socket = _FakeSocket(session=name, fail_at=2)  # attaches, then dies
            future = _serve(server, socket)
            _wait_until(lambda: len(socket.sent) == 2)
            socket.push(server, {"type": "task", "text": "run it"})
            # The worker parks on a card nobody can be shown.
            _wait_until(lambda: server._default.bridge.pending)
            assert not marker.exists()
            # A second, live client picks the same card up off the replay and
            # denies it: the gate stayed open, and only an answer moves it.
            with connected(client, f"/ws?session={name}") as (ws, _, replay):
                request = next(
                    e for e in replay["events"] if e["type"] == "approval_request"
                )
                ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
                recv_until(ws, "done")
            assert not marker.exists()
            assert tool_results(chat)[-1]["content"] == DENIED_RESULT
            socket.push(server, None)
            future.result(timeout=5)

    def test_one_dead_socket_does_not_cost_the_others_their_card(
        self, app_env, tmp_path
    ):
        """Three viewers, the middle one's socket dead. The fan-out is a loop
        over OUTBOXES — nothing in it can raise — so the dead one costs the
        other two nothing, and either of them can answer."""
        marker = tmp_path / "approved-elsewhere"
        client, _ = make_client(app_env, self.responses(f"touch {marker}"))
        with client, connected(client) as (first, hello, _):
            server = client.app.state.server
            name = hello["session"]
            socket = _FakeSocket(session=name, fail_at=2)
            future = _serve(server, socket)
            _wait_until(lambda: len(socket.sent) == 2)
            with connected(client, f"/ws?session={name}") as (second, _, _):
                _wait_until(lambda: len(server._default.viewers) == 3)
                assert any(c.ws is socket for c in server._default.viewers)
                first.send_json({"type": "task", "text": "run it"})
                # BOTH live viewers get the card; the dead one is simply skipped
                # by its own sender, not by the fan-out.
                request = recv_until(first, "approval_request")
                mirrored = recv_until(second, "approval_request")
                assert mirrored["id"] == request["id"]
                # ...and the one that did not start the turn can answer it.
                second.send_json(
                    {"type": "approval", "id": request["id"], "action": "approve"}
                )
                recv_until(first, "done")
                assert marker.exists()
            # The dead socket never took its own connection down with it — it is
            # still parked on receive, exactly as a wedged phone would be.
            assert not future.done()
            socket.push(server, None)
            future.result(timeout=5)

    def test_a_card_no_screen_ever_showed_records_no_screen_time(
        self, app_env, tmp_path
    ):
        """#306 meets #315, and this is the axis on which a vanished client
        could make the trace lie. `held_ms` is the gate's own clock and is
        always knowable. `shown_ms` is the BROWSER's, so a card that reached no
        screen must record none at all — a zero is the exact finding that field
        exists to detect, and writing one for a card nobody was shown would say
        the owner tapped it blind."""
        marker = tmp_path / "never-shown"
        client, _ = make_client(app_env, self.responses(f"touch {marker}"))
        with client:
            server = client.app.state.server
            name = server._default.name
            socket = _FakeSocket(session=name, fail_at=2)
            future = _serve(server, socket)
            _wait_until(lambda: len(socket.sent) == 2)
            socket.push(server, {"type": "task", "text": "run it"})
            _wait_until(lambda: server._default.bridge.pending)
            socket.push(server, {"type": "stop"})  # force-denied, never rendered
            _wait_until(lambda: not server._default.bridge.pending)
            _wait_until(lambda: not server._default.busy)
            socket.push(server, None)
            future.result(timeout=5)
        assert not marker.exists()
        records = _command_records(app_env)
        assert records, "a forced deny is still recorded like any other deny"
        for record in records:
            assert isinstance(record["held_ms"], int)  # the gate's own clock
            assert "shown_ms" not in record  # absent, never zero, never inferred

    def test_the_card_never_reaches_a_socket_from_the_gate(self):
        """The pin under the verdict, in the shape tests/test_pty.py uses for
        "the model has no write path to the console".

        `Bridge.ask` emits the card into each viewer's OUTBOX and returns; the
        socket is written later by that Client's own sender task. That
        indirection is the ONLY reason a client vanishing at the instant a card
        is emitted cannot raise into the worker parked inside the gate. If it is
        ever removed — a delivery-coupled emit, an awaited send — the property
        would go quiet rather than fail, so it fails here instead."""
        import inspect

        source = inspect.getsource(server_module.Bridge)
        assert "outbox.put_nowait" in source
        for socket_verb in ("send_json", ".ws", "await ", "async "):
            assert socket_verb not in source, (
                f"Bridge touched {socket_verb!r}: the approval gate must never "
                "depend on a socket write succeeding"
            )
