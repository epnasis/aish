"""Web server tests: the same FakeChat pattern as test_agent.py, driven over
a real WebSocket via Starlette's TestClient (which runs the app's event loop
in a thread, so the worker-thread bridge is exercised for real). No model,
no network; the only real commands executed are harmless touch/ls in tmp dirs.
"""

import contextlib
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import aish.server as server_module
from aish.agent import DENIED_RESULT, WRITE_DENIED
from aish.server import create_app


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


class TestConnect:
    def test_hello_carries_model_session_scope(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, hello, replay):
            assert hello["model"] == "fake"
            assert hello["session"].startswith("session-")
            assert hello["busy"] is False
            assert hello["cwd"] == app_env["cwd"]
            assert replay["events"] == []

    def test_task_streams_and_finishes(self, app_env):
        client, chat = make_client(app_env, [model_says("hi there")])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "say hi"})
            assert recv_until(ws, "user")["text"] == "say hi"
            done = recv_until(ws, "done")
            assert done["result"] == "hi there"
            sent_user = [m for m in chat.calls[0]["messages"] if m["role"] == "user"]
            assert sent_user[-1]["content"] == "say hi"


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

    def test_task_while_busy_rejected(self, app_env, tmp_path):
        client, _ = make_client(app_env, self.pending_responses(tmp_path))
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "task", "text": "run it"})
            request = recv_until(ws, "approval_request")  # agent now blocked → busy
            ws.send_json({"type": "task", "text": "another"})
            error = recv_until(ws, "error")
            assert "busy" in error["text"]
            ws.send_json({"type": "approval", "id": request["id"], "action": "approve"})
            recv_until(ws, "done")


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

            ws.send_json({"type": "resume", "path": listing["sessions"][0]["name"]})
            history = recv_until(ws, "history")
            roles = [m["role"] for m in history["messages"]]
            assert "user" in roles and "assistant" in roles

            ws.send_json({"type": "task", "text": "what animal did I mention?"})
            recv_until(ws, "done")
            contents = json.dumps(chat.calls[-1]["messages"])
            assert "zebra" in contents  # resumed context reached the model

    def test_resume_rejects_path_escape(self, app_env):
        client, _ = make_client(app_env, [])
        with client, connected(client) as (ws, _, _):
            ws.send_json({"type": "resume", "path": "../../../etc/passwd"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "no such session" in error["text"]


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
            assert server.agent.chat is new_chat
            assert server.agent.provider == "gemini"
            config = app_env["config_path"].read_text(encoding="utf-8")
            assert 'model = "gemini:gemini-3-pro"' in config

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
                r for r in server.agent.roots
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

    def test_upload_requires_token_when_set(self, app_env):
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            assert client.post("/upload?name=a.txt", content=b"x").status_code == 403
            assert (
                client.post("/upload?name=a.txt&token=s3cret", content=b"x").status_code
                == 200
            )


class TestTokenGate:
    def test_wrong_token_rejected_right_token_accepted(self, app_env):
        client, _ = make_client(app_env, [], token="s3cret")
        with client:
            with pytest.raises(Exception):  # noqa: B017 — close surfaces per-transport
                with client.websocket_connect("/ws?token=wrong") as ws:
                    ws.receive_json()
            with connected(client, "/ws?token=s3cret") as (_ws, hello, _):
                assert hello["model"] == "fake"
