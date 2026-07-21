"""Web server tests: the same FakeChat pattern as test_agent.py, driven over
a real WebSocket via Starlette's TestClient (which runs the app's event loop
in a thread, so the worker-thread bridge is exercised for real). No model,
no network; the only real commands executed are harmless touch/ls in tmp dirs.
"""

import contextlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import aish.server as server_module
from aish.agent import DENIED_RESULT, WRITE_DENIED
from aish.server import create_app
from aish.session import SessionLog


def tool_call(name: str, **arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def model_says(content: str = "", tool_calls: list | None = None):
    return SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls or None)
    )


class FakeChat:
    """Scripted backend. The web server always streams (on_token is wired),
    so stream=True returns the response as a one-chunk iterator — the same
    shape ollama's streaming yields."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
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


def make_client(app_env, responses, **kwargs):
    chat = FakeChat(responses)
    app = create_app("fake", client_chat=chat, **app_env, **kwargs)
    return TestClient(app), chat


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
    "queued", "approval_request", "approval_resolved", "cwd_changed",
    "model_changed", "session_state", "file_list", "job_list",
    "model_list", "session_list", "session_deleted", "session_renamed",
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
        else:
            shape.append((kind,))
    return shape


class TestConnect:
    def test_hello_carries_model_session_scope(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, replay):
            assert hello["model"] == "fake"
            assert hello["session"].startswith("session-")
            assert hello["busy"] is False
            assert hello["cwd"] == app_env["cwd"]
            assert hello["rev"]  # static-files fingerprint for staleness checks
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
            agent_module.web, "read_url", lambda url, topic=None: f"[{url}] text"
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
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "list it"})
            events = self._drain(ws)
            starts = [e for e in events if e["type"] == "command_start"]
            ends = [e for e in events if e["type"] == "command_end"]
            assert len(starts) == 1
            assert starts[0]["cwd"] == app_env["cwd"]
            assert starts[0]["command"] == "ls"
            assert ends == [{"type": "command_end", "status": "exit", "exit_code": 0}]

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
        with client, connected(client) as (ws, _, _):
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
            assert ends and ends[0] == {"type": "command_end", "status": "exit", "exit_code": 0}
            # The output rode the terminal block; done carries no answer bubble.
            assert events[-1] == {"type": "done", "result": ""}
            assert chat.calls == []  # the model was never asked

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
        # queued and applied when the task finishes — not rejected.
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")  # agent now blocked → busy
            ws.send_json({"type": "cd", "path": str(tmp_path)})
            echo = recv_until(ws, "echo")
            assert "will switch" in echo["text"]
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
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

    def test_message_while_busy_queues_and_runs_next(self, app_env, tmp_path):
        client, _ = make_client(
            app_env,
            [
                model_says(tool_calls=[tool_call("run_command", command=f"touch {tmp_path}/a")]),
                model_says("first answer"),
                model_says("second answer"),
            ],
        )
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "first task"})
            request = recv_until(ws, "approval_request")

            ws.send_json({"type": "task", "text": "second task"})
            queued = recv_until(ws, "queued")
            assert queued["position"] == 1

            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            first = recv_until(ws, "done")
            assert first["result"] == "first answer"
            # the queued message starts on its own
            user = recv_until(ws, "user")
            assert user["text"] == "second task"
            second = recv_until(ws, "done")
            assert second["result"] == "second answer"


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
        client = TestClient(create_app("fake", client_chat=RaisingChat(), **app_env))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "do it"})
            error = recv_any(ws, "error")
            assert "model unavailable" in error["text"]
            # Server-side truth: the busy flag cleared with the error.
            assert client.app.state.server.active.busy is False
            assert client.app.state.server.active.state() == "idle"

    def test_stop_after_error_is_graceful_noop(self, app_env):
        client = TestClient(create_app("fake", client_chat=RaisingChat(), **app_env))
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
        client = TestClient(create_app("fake", client_chat=RaisingChat(), **app_env))
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
        client = TestClient(create_app("fake", client_chat=RaisingChat(), **app_env))
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
            with connected(client) as (_ws, _, replay):
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
            # and the client gets a session_state heads-up.
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            notice = recv_until(ws, "session_state")
            assert notice["session"] == session_a
            assert notice["state"] == "idle"

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
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "no such session" in error["text"]

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
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "still running" in error["text"]
            assert (app_env["state_dir"] / name).is_file()

            # The pending approval survived the refused delete untouched.
            ws.send_json({"type": "approval", "id": request["id"], "action": "deny"})
            recv_until(ws, "done")

    def test_delete_rejects_path_escape_and_unknown_names(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            for name in ("../../../etc/passwd", "session-nonexistent.jsonl"):
                ws.send_json({"type": "delete_session", "name": name})
                error = ws.receive_json()
                assert error["type"] == "error"
                assert "no such session" in error["text"]


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
        messages, _, custom_title = SessionLog._parse(old)
        assert custom_title == "Archived"
        assert messages == [{"role": "user", "content": "old topic"}]

    def test_rename_rejects_empty_title(self, app_env):
        client, _ = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, hello, _):
            name = hello["session"]
            ws.send_json({"type": "task", "text": "hi"})
            recv_until(ws, "done")
            ws.send_json({"type": "rename_session", "name": name, "title": "   "})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "empty" in error["text"]

    def test_rename_rejects_path_escape_and_unknown_names(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            for name in ("../../../etc/passwd", "session-nonexistent.jsonl"):
                ws.send_json({"type": "rename_session", "name": name, "title": "x"})
                error = ws.receive_json()
                assert error["type"] == "error"
                assert "no such session" in error["text"]


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
            assert "out of range" in error["text"]

    def test_fork_empty_conversation_refused(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "fork"})
            error = ws.receive_json()
            assert error["type"] == "error"
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
            assert "can't fork while this session is working" in error["text"]
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")


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
            error = ws.receive_json()
            assert error["type"] == "error"
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

    def test_attached_image_goes_native_pdf_falls_back(self, app_env):
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
                assert "you can see it" in user["text"]  # image went native
                assert f"[attached file: {pdf['path']}]" in user["text"]  # pdf fell back
                recv_until(ws, "done")
            sent = [m for m in chat.calls[0]["messages"] if m["role"] == "user"][-1]
            # test provider is "ollama": images native, pdf stays a path note
            assert sent.get("images") == [image["path"]]
            assert "documents" not in sent

    def test_attachment_outside_uploads_never_goes_native(self, app_env, tmp_path):
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"\x89PNG-private")
        client, chat = make_client(app_env, [model_says("ok")])
        with client, connected(client) as (ws, _, _):
            ws.send_json(
                {"type": "task", "text": "look", "attachments": [str(secret)]}
            )
            user = recv_until(ws, "user")
            assert f"[attached file: {secret}]" in user["text"]
            recv_until(ws, "done")
            sent = [m for m in chat.calls[0]["messages"] if m["role"] == "user"][-1]
            assert "images" not in sent  # nothing base64'd from outside uploads

    def test_upload_requires_token_when_set(self, app_env):
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.post("/upload?name=a.txt", content=b"x").status_code == 403
            assert (
                client.post("/upload?name=a.txt&token=s3cret", content=b"x").status_code
                == 200
            )


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


class TestDirListing:
    def make_tree(self, tmp_path):
        base = tmp_path / "tree"
        for d in ("alpha", "beta/nested", "beta/.hidden", ".git/objects", "projects/aish"):
            (base / d).mkdir(parents=True)
        (base / "file.txt").write_text("not a dir", encoding="utf-8")
        return base

    def test_dirs_lists_folders_with_counts_and_files(self, app_env, tmp_path):
        base = self.make_tree(tmp_path)
        client, _ = make_client(app_env, [])
        with client:
            body = client.get(f"/dirs?path={base}").json()
            assert body["path"] == str(base)
            # child-count per folder: alpha is empty, beta has 2 (incl. hidden),
            # .git and projects each have 1.
            assert body["dirs"] == [
                {"name": ".git", "items": 1},
                {"name": "alpha", "items": 0},
                {"name": "beta", "items": 2},
                {"name": "projects", "items": 1},
            ]
            assert body["files"] == ["file.txt"]

    def test_dirs_child_count_survives_unreadable_subfolder(self, app_env, tmp_path):
        if os.name != "posix" or os.geteuid() == 0:
            pytest.skip("permission bits aren't enforced for root or on non-POSIX")
        base = self.make_tree(tmp_path)
        locked = base / "beta" / "nested"
        locked.mkdir(exist_ok=True)
        os.chmod(locked, 0o000)
        try:
            client, _ = make_client(app_env, [])
            with client:
                body = client.get(f"/dirs?path={base}").json()
                assert body["path"] == str(base)  # handler didn't crash
                beta = next(d for d in body["dirs"] if d["name"] == "beta")
                assert beta["items"] == 2  # unreadable child still counted, just uncounted itself
        finally:
            os.chmod(locked, 0o755)

    def test_dirs_requires_token_when_set(self, app_env, tmp_path):
        base = self.make_tree(tmp_path)
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.get(f"/dirs?path={base}").status_code == 403
            assert client.get(f"/dirs?path={base}&token=s3cret").status_code == 200
            assert client.get(f"/dirs/search?q=x&base={base}").status_code == 403

    def test_dirs_rejects_bad_paths(self, app_env, tmp_path):
        client, _ = make_client(app_env, [])
        with client:
            assert client.get("/dirs?path=relative/path").status_code == 400
            assert client.get(f"/dirs?path={tmp_path}/nope").status_code == 404
            assert client.get(f"/dirs?path={tmp_path}/tree/file.txt").status_code == 404

    def test_search_finds_nested_dirs_skips_hidden(self, app_env, tmp_path):
        base = self.make_tree(tmp_path)
        client, _ = make_client(app_env, [])
        with client:
            body = client.get(f"/dirs/search?q=nested&base={base}").json()
            assert body["results"] == [str(base / "beta" / "nested")]
            body = client.get(f"/dirs/search?q=hidden&base={base}").json()
            assert body["results"] == []  # dotfolders never surface

    def test_search_ranks_prefix_before_substring(self, app_env, tmp_path):
        base = tmp_path / "rank"
        for d in ("aish", "my-aish-fork", "unrelated"):
            (base / d).mkdir(parents=True)
        client, _ = make_client(app_env, [])
        with client:
            results = client.get(f"/dirs/search?q=aish&base={base}").json()["results"]
            assert results == [str(base / "aish"), str(base / "my-aish-fork")]


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


class TestSkillsRefresh:
    def test_skill_added_after_boot_is_advertised(self, app_env):
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


class TestExportEndpoints:
    def test_export_answer_returns_pdf_attachment(self, app_env):
        client, _ = make_client(app_env, [])
        with client:
            response = client.post(
                "/export/answer?title=my+answer",
                content="# Answer\n\nBody text — with unicode →.".encode(),
            )
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/pdf"
            assert 'attachment; filename="my-answer.pdf"' in (
                response.headers["content-disposition"]
            )
            assert response.content.startswith(b"%PDF")

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
