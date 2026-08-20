import datetime
import json
import os
import threading
import time
from pathlib import Path

from aish.session import (
    SessionLog,
    attachment_embed,
    attachment_guidance,
    attachment_names,
    files_named,
    message_body,
    real_attachments,
    resolve_attachment,
    strip_attachment_notes,
    title_drifted,
    to_record_form,
)


def test_roundtrip_messages_and_commands(tmp_path):
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hello"})
    log.message({"role": "assistant", "content": "hi"})
    log.message({"role": "tool", "tool_name": "run_command", "content": "output"})
    log.command("ls -la", "auto")

    loaded = SessionLog.load_messages(log.path)
    assert loaded == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "tool_name": "run_command", "content": "output"},
    ]
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    audit = [r for r in records if r["kind"] == "command"]
    assert audit[0]["command"] == "ls -la" and audit[0]["decision"] == "auto"
    assert all("ts" in r for r in records)


def test_custom_title_wins_hot_and_cold(tmp_path):
    # A kind:"title" record overrides the first-user-message derivation on both
    # the loaded path (info) and the cold peek (drawer/pager).
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "derived from this first message"})
    log.message({"role": "assistant", "content": "ok"})
    assert SessionLog.info(log.path).title == "derived from this first message"
    assert SessionLog._peek_title(log.path) == "derived from this first message"

    log.set_title("My Renamed Chat")
    assert SessionLog.info(log.path).title == "My Renamed Chat"
    assert SessionLog._peek_title(log.path) == "My Renamed Chat"
    # pager_titles (cold path) reflects the custom title too. The 4th field is
    # the last-ACTIVITY stamp the client's deck ages by (#201) — a real stamp,
    # since an undated page is discarded as a seed candidate. It comes from the
    # records, so it is whole seconds, and never later than the file itself.
    pages = SessionLog.pager_titles(tmp_path)
    assert [(name, title, origin) for name, title, origin, _, _ in pages] == [
        (log.path.name, "My Renamed Chat", "user")
    ]
    assert pages[0][3] == SessionLog.info(log.path).activity
    assert 0 < pages[0][3] <= log.path.stat().st_mtime


def test_latest_title_record_wins(tmp_path):
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hello"})
    log.set_title("first name")
    log.set_title("second name")
    log.set_title("final name")
    assert SessionLog.info(log.path).title == "final name"
    assert SessionLog._peek_title(log.path) == "final name"


def test_title_record_excluded_from_conversation(tmp_path):
    # A renamed session must --resume identically: the title record is metadata,
    # never a message, so reconstruction ignores it.
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hello"})
    log.set_title("Renamed")
    log.message({"role": "assistant", "content": "hi"})
    assert SessionLog.load_messages(log.path) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    messages, _, custom_title, *_ = SessionLog._parse(log.path)
    assert custom_title == "Renamed"
    assert all(m.get("role") in ("user", "assistant") for m in messages)


def test_empty_title_ignored(tmp_path):
    # A blank/whitespace title record does not shadow the derived title.
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "real title"})
    log.set_title("   ")
    assert SessionLog.info(log.path).title == "real title"
    custom_title = SessionLog._parse(log.path).title
    assert custom_title is None


def test_info_carries_the_last_logged_cwd(tmp_path):
    # The session's directory is metadata, like the title: the LAST recorded
    # move wins, it never enters the conversation, and a chat that never moved
    # reports "" (the reader cannot know the launch dir from the log alone).
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hello"})
    assert SessionLog.info(log.path).cwd == ""
    log.workspace({"kind": "cwd", "cwd": "/tmp/one"})
    log.workspace({"kind": "trust_dir", "path": "/tmp/trusted"})
    log.workspace({"kind": "cwd", "cwd": "/tmp/two"})
    info = SessionLog.info(log.path)
    assert info.cwd == "/tmp/two"
    assert [m["content"] for m in SessionLog.load_messages(log.path)] == ["hello"]
    # The searchable listing carries it too — that is what the drawer reads.
    entries = SessionLog.load_entries(tmp_path)
    assert [e.info.cwd for e in entries] == ["/tmp/two"]


def test_custom_title_is_searchable(tmp_path):
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "unrelated first message"})
    log.set_title("quarterly budget review")
    results = SessionLog.search_sessions(tmp_path, "budget review")
    assert [r.path.name for r in results] == [log.path.name]


def test_trace_steps_excluded_from_conversation(tmp_path):
    # Trace records must not leak into the model-facing conversation.
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hello"})
    log.step({"kind": "tool", "name": "run_command", "ok": True})
    log.message({"role": "assistant", "content": "hi"})
    assert SessionLog.load_messages(log.path) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


def test_reconstruct_events_groups_by_task(tmp_path):
    # One task = one user event, its steps, then a single done carrying the
    # final assistant text — intermediate tool-call turns don't close it.
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "read a file"})
    log.step({"kind": "thinking_start"})
    log.message({"role": "assistant", "content": ""})  # tool-call turn, no text
    log.step({"kind": "tool", "name": "read_file", "ok": True})
    log.message({"role": "tool", "tool_name": "read_file", "content": "..."})
    log.message({"role": "assistant", "content": "read it"})
    log.message({"role": "user", "content": "again"})
    log.step({"kind": "tool", "name": "read_file", "ok": True})
    log.message({"role": "assistant", "content": "read again"})

    events = SessionLog.reconstruct_events(log.path)
    kinds = [(e["type"], e.get("kind")) for e in events]
    assert kinds == [
        ("user", None),
        ("step", "thinking_start"),
        ("step", "tool"),
        ("done", None),
        ("user", None),
        ("step", "tool"),
        ("done", None),
    ]
    dones = [e["result"] for e in events if e["type"] == "done"]
    assert dones == ["read it", "read again"]  # final text per task, not ""


def test_reconstruct_events_interrupted_turn_becomes_error(tmp_path):
    # A turn cut off mid-step (a deploy during a web search) — a tool_start with
    # no matching finish — reconstructs as an ERROR (surfacing Retry), not a
    # `done` that would leave the step spinning forever.
    from aish.session import INTERRUPTED_TASK

    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "co to czarna dziura?"})
    log.step({"kind": "thinking_start"})
    log.step({"kind": "thinking", "secs": 0.1})
    log.step({"kind": "tool_start", "name": "web_search"})  # process died here

    events = SessionLog.reconstruct_events(log.path)
    types = [e["type"] for e in events]
    assert types[0] == "user"
    assert "done" not in types
    errors = [e for e in events if e["type"] == "error"]
    assert errors and errors[0]["text"] == INTERRUPTED_TASK


def test_reconstruct_events_replays_the_recorded_failure(tmp_path):
    # A turn that FAILED knows why (#203). The generic "cut off mid-step" above
    # is an inference from unfinished steps; a recorded failure outranks it, so
    # reopening the chat says what a live viewer was told at the time instead of
    # guessing. Without the record, the reason existed only in that live event.
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "run the nightly job"})
    log.step({"kind": "tool_start", "name": "run_command"})
    log.task_end("failed", "task failed: BackendError('no route to host')")

    events = SessionLog.reconstruct_events(log.path)
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "no route to host" in errors[0]["text"]
    assert "done" not in [e["type"] for e in events]


def test_reconstruct_events_old_logs_are_byte_identical(tmp_path):
    # The additive rule: a task_end written before `status` existed carries no
    # opinion, so every branch reading it must be inert on old logs.
    path = tmp_path / "session-20260101-000000-000000.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(r) for r in [
                {"kind": "message", "role": "user", "content": "hi",
                 "at": "2026-01-01T00:00:00"},
                {"kind": "trace", "step": {"kind": "tool", "name": "read_file", "ok": True}},
                {"kind": "message", "role": "assistant", "content": "hello",
                 "at": "2026-01-01T00:00:01"},
                {"kind": "task_end"},
            ]
        ) + "\n",
        encoding="utf-8",
    )
    events = SessionLog.reconstruct_events(path)
    assert [e["type"] for e in events] == ["user", "step", "done"]
    assert events[-1]["result"] == "hello"


def test_reconstruct_events_replays_the_one_delivery(tmp_path):
    """#212. A turn says several things on its way to the answer; the harness
    delivers only the FIRST (the acknowledgement) and drops the play-by-play,
    so replay shows one too — L1 is about matching what the owner SAW, not what
    the model said. The log still holds every interim message; this is the
    reader choosing. The last message still becomes `done`."""
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "what does it look like?"})
    log.step({"kind": "thinking_start"})
    log.message({"role": "assistant", "content": "Let me search for that."})
    log.step({"kind": "thinking", "secs": 0.1})
    log.step({"kind": "tool", "name": "web_search", "ok": True})
    log.step({"kind": "thinking_start"})
    log.message({"role": "assistant", "content": "There are leaks — digging in."})
    log.step({"kind": "thinking", "secs": 0.1})
    log.step({"kind": "tool", "name": "read_url", "ok": True})
    log.message({"role": "assistant", "content": "It folds."})

    events = SessionLog.reconstruct_events(log.path)
    assert [e["type"] for e in events] == [
        "user",
        "step", "token", "delivery", "step", "step",
        "step", "step", "step",
        "done",
    ]
    said = [e["text"] for e in events if e["type"] == "delivery"]
    assert said == ["Let me search for that."], "the play-by-play came back"
    assert events[-1]["result"] == "It folds."


def test_reconstruct_events_one_answer_replays_as_one_done(tmp_path):
    """The control: a turn that only ever said one thing is untouched — no
    token, no delivery, just the `done` every old log has always replayed."""
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hi"})
    log.step({"kind": "tool", "name": "read_file", "ok": True})
    log.message({"role": "assistant", "content": "hello"})

    events = SessionLog.reconstruct_events(log.path)
    assert [e["type"] for e in events] == ["user", "step", "done"]
    assert events[-1]["result"] == "hello"


def test_reconstruct_events_a_failed_turn_keeps_everything_it_said(tmp_path):
    """A turn ending in a recorded failure has no answer to promote, and the
    failure branch throws `answer` away — so lifting the last delivery out of
    the timeline DELETED it. The owner watched that narration arrive and then
    saw the error; a cold reload showed the error with the words gone, on
    exactly the long flaky turns narration exists for."""
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "what does it look like?"})
    log.message({"role": "assistant", "content": "first thing said"})
    log.step({"kind": "tool", "name": "web_search", "ok": True})
    log.message({"role": "assistant", "content": "second thing said"})
    log.task_end(status="failed", error="model unavailable: boom")

    events = SessionLog.reconstruct_events(log.path)
    assert [e["type"] for e in events] == [
        "user", "token", "delivery", "step", "error",
    ]
    assert [e["text"] for e in events if e["type"] == "delivery"] == [
        "first thing said",
    ]
    assert events[-1]["text"] == "model unavailable: boom"


def test_reconstruct_events_deliveries_do_not_cross_turns(tmp_path):
    """The buffer is per turn: a delivery from turn one must not be lifted out
    as turn two's answer, nor left in turn two's timeline."""
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "first"})
    log.message({"role": "assistant", "content": "narrating"})
    log.step({"kind": "tool", "name": "web_search", "ok": True})
    log.message({"role": "assistant", "content": "first answer"})
    log.message({"role": "user", "content": "second"})
    log.message({"role": "assistant", "content": "second answer"})

    events = SessionLog.reconstruct_events(log.path)
    assert [e["type"] for e in events] == [
        "user", "token", "delivery", "step", "done", "user", "done",
    ]
    assert [e["result"] for e in events if e["type"] == "done"] == [
        "first answer", "second answer",
    ]


def test_reconstruct_events_finished_tool_is_not_interrupted(tmp_path):
    # The control: a tool_start WITH its matching finish closes normally (done).
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hi"})
    log.step({"kind": "thinking_start"})
    log.step({"kind": "thinking", "secs": 0.1})
    log.step({"kind": "tool_start", "name": "web_search"})
    log.step({"kind": "tool", "name": "web_search", "ok": True})
    log.message({"role": "assistant", "content": "done"})

    types = [e["type"] for e in SessionLog.reconstruct_events(log.path)]
    assert "error" not in types and types[-1] == "done"


def test_reconstruct_events_run_command_framing(tmp_path):
    # A run_command reconstructs into the SAME command_start → stream →
    # command_end → tool sequence a live session emits, so the terminal block
    # rebuilds identically (not a plain fallback box).
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "list files"})
    log.step({"kind": "tool_start", "name": "run_command", "command": "ls"})
    log.command_event({"kind": "cmd_start", "cwd": "/proj", "command": "ls"})
    log.command_event({"kind": "cmd_end", "status": "exit", "exit_code": 0})
    # run_command's real output carries a trailing "[exit code: N]" marker.
    log.step({"kind": "tool", "name": "run_command", "ok": True,
              "command": "ls", "output": "a.txt\nb.txt\n[exit code: 0]"})
    log.message({"role": "assistant", "content": "two files"})

    events = SessionLog.reconstruct_events(log.path)
    seq = [e["type"] for e in events]
    assert seq == ["user", "step", "command_start", "stream", "command_end", "step", "done"]
    cs = next(e for e in events if e["type"] == "command_start")
    assert cs["cwd"] == "/proj" and cs["command"] == "ls"
    # The exit marker is stripped so the terminal body matches the live stream,
    # where the code arrives via command_end, not as an output line.
    assert next(e for e in events if e["type"] == "stream")["text"] == "a.txt\nb.txt"
    ce = next(e for e in events if e["type"] == "command_end")
    assert ce["status"] == "exit" and ce["exit_code"] == 0
    assert "kind" not in cs and "kind" not in ce  # framing records' kind is stripped


def test_reconstruct_events_synthesizes_framing_for_legacy_command(tmp_path):
    # A run_command logged before framing persistence (tool step only) still
    # gets a synthesized command block, so the frontend needs no fallback path.
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "list files"})
    log.step({"kind": "tool", "name": "run_command", "ok": True,
              "command": "ls", "output": "a.txt"})
    log.message({"role": "assistant", "content": "one file"})

    events = SessionLog.reconstruct_events(log.path)
    seq = [e["type"] for e in events]
    assert seq == ["user", "command_start", "stream", "command_end", "step", "done"]
    ce = next(e for e in events if e["type"] == "command_end")
    assert ce["exit_code"] == 0  # ok=True → synthesized exit 0


def test_reconstruct_events_multiline_user_command_replays_as_terminal(tmp_path):
    # A user-direct (!) command with a MULTI-LINE body — e.g. `gh issue create`
    # with a multi-line --body — must replay as its terminal block, NOT a plain
    # blue user bubble showing the raw "[I ran … myself]" annotation. Regression
    # for #154: the annotation regex needed re.DOTALL to span the newlines.
    log = SessionLog.new(tmp_path)
    cmd = "gh issue create --title X --body '### Problem\nline two\nline three'"
    log.command_event({"kind": "cmd_start", "cwd": "/proj", "command": cmd})
    log.command_event({"kind": "cmd_end", "status": "exit", "exit_code": 0})
    log.message(
        {"role": "user",
         "content": f"[I ran `{cmd}` myself; output:]\nhttps://x/y/issues/1\n[exit code: 0]"}
    )

    events = SessionLog.reconstruct_events(log.path)
    user_ev = next(e for e in events if e["type"] == "user")
    assert user_ev["text"].startswith("!gh issue create")  # the ! command, not the annotation
    assert "[I ran" not in user_ev["text"]
    types = [e["type"] for e in events]
    assert "command_start" in types and "command_end" in types  # terminal block, not a bubble


def test_reconstruct_events_command_no_output_emits_no_stream(tmp_path):
    # A command with no output emits no stream event (matches the live path,
    # where zero output lines stream) — the block collapses its middle zone.
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "touch f"})
    log.command_event({"kind": "cmd_start", "cwd": "/proj", "command": "touch f"})
    log.command_event({"kind": "cmd_end", "status": "exit", "exit_code": 0})
    log.step({"kind": "tool", "name": "run_command", "ok": True,
              "command": "touch f", "output": ""})
    log.message({"role": "assistant", "content": "done"})

    seq = [e["type"] for e in SessionLog.reconstruct_events(log.path)]
    assert seq == ["user", "command_start", "command_end", "step", "done"]


def test_reconstruct_events_marks_the_resume_note_synthetic(tmp_path):
    # #171: the restart-recovery note is aish talking to itself. It IS a real
    # turn (it started a task), so it still opens one — but it carries the
    # marker the frontend renders as a system row instead of a user bubble.
    from aish.server import RESUME_NOTE

    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "answer the mail"})
    log.step({"kind": "tool", "name": "gmail_send", "ok": True})
    log.message({"role": "user", "content": RESUME_NOTE})
    log.step({"kind": "tool", "name": "gmail_search", "ok": True})
    log.message({"role": "assistant", "content": "already sent"})

    users = [e for e in SessionLog.reconstruct_events(log.path) if e["type"] == "user"]
    assert [e.get("synthetic") for e in users] == [None, "resume"]
    assert users[1]["text"] == RESUME_NOTE  # verbatim; only the framing changes


def test_reconstruct_events_skips_aishs_own_notes(tmp_path):
    # A nudge, a /cd announcement and shared console text are appended to the
    # conversation as user turns but never shown live. Replaying them as
    # bubbles both invented text the user never typed AND split the turn they
    # sat inside — so they are dropped, which is exactly what live does (#171).
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "fix the tests"})
    log.step({"kind": "tool", "name": "read_file", "ok": True})
    log.message({"role": "user", "content": "[aish: you have issued this exact call…]"})
    log.step({"kind": "tool", "name": "read_file", "ok": True})
    log.message({"role": "assistant", "content": "fixed"})
    log.message({"role": "user", "content": "[I moved the session to /proj with /cd — …]"})
    log.message({"role": "user", "content": "[Shared from my interactive terminal:]\nkey=x"})

    events = SessionLog.reconstruct_events(log.path)
    assert [e["type"] for e in events] == ["user", "step", "step", "done"]
    assert events[0]["text"] == "fix the tests"  # one turn, not three
    assert events[-1]["result"] == "fixed"


def test_reconstruct_events_marks_a_triggered_sessions_opening_prompt(tmp_path):
    # An automation's prompt has no marker to match — arbitrary text — so it is
    # identified by position + provenance: the opening turn of a non-user
    # session. Anything the owner types INTO that chat afterwards is theirs.
    log = SessionLog.new(tmp_path)
    log.origin("email")
    log.message({"role": "user", "content": "new mail from the bank — triage it"})
    log.step({"kind": "tool", "name": "gmail_search", "ok": True})
    log.message({"role": "assistant", "content": "triaged"})
    log.message({"role": "user", "content": "reply to it"})
    log.step({"kind": "tool", "name": "gmail_send", "ok": True})
    log.message({"role": "assistant", "content": "replied"})

    users = [e for e in SessionLog.reconstruct_events(log.path) if e["type"] == "user"]
    assert [e.get("synthetic") for e in users] == ["trigger", None]


def test_reconstruct_events_leaves_a_user_chats_first_prompt_alone(tmp_path):
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hello"})
    log.step({"kind": "tool", "name": "read_file", "ok": True})
    log.message({"role": "assistant", "content": "hi"})
    users = [e for e in SessionLog.reconstruct_events(log.path) if e["type"] == "user"]
    assert "synthetic" not in users[0]


def test_derived_title_and_snippet_skip_aishs_own_notes(tmp_path):
    # A chat opened with a /cd was titled with the announcement THAT produced,
    # and the drawer showed it as "You: [I moved the session to …]" (#171).
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "[I moved the session to /proj with /cd — …]"})
    log.message({"role": "user", "content": "what does this repo do?"})
    log.message({"role": "assistant", "content": "it is a shell agent"})
    log.message({"role": "user", "content": "[aish: you have reached the step limit…]"})
    info = SessionLog.info(log.path)
    assert info.title == "what does this repo do?"
    assert "I moved the session" not in info.snippet
    assert "aish:" not in info.snippet


IMAGE_NOTE = "[image attached: cat.png — you can see it; file at /u/uploads/cat.png]"


def test_derived_title_and_snippet_skip_attachment_notes(tmp_path):
    """Send a photo and the chat was NAMED with the sentence aish wrote to the
    model about it, absolute uploads path and all — in the header, in the rail,
    on the PWA's tab, and in the drawer's preview line."""
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": f"what is this?\n\n{IMAGE_NOTE}"})
    info = SessionLog.info(log.path)
    assert info.title == "what is this?"
    assert info.snippet == "You: what is this?"
    assert "you can see it" not in info.title + info.snippet
    assert "/uploads/" not in info.title + info.snippet


def test_a_wordless_turn_is_named_by_what_it_carried(tmp_path):
    """A photo sent with nothing typed still says what the chat is about."""
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": IMAGE_NOTE})
    info = SessionLog.info(log.path)
    assert info.title == "cat.png"
    assert info.snippet == "You: cat.png"


def test_a_wordless_turn_with_several_files_names_them_all(tmp_path):
    log = SessionLog.new(tmp_path)
    log.message({
        "role": "user",
        "content": f"{IMAGE_NOTE}\n[attached file: /tmp/notes/report.pdf]",
    })
    assert SessionLog.info(log.path).title == "cat.png, report.pdf"


def test_text_that_merely_looks_like_a_note_is_still_the_users_words(tmp_path):
    """Being wrong in this direction would delete something they typed."""
    log = SessionLog.new(tmp_path)
    typed = "[attached file: this is not really a note"  # no closing bracket
    log.message({"role": "user", "content": typed})
    assert SessionLog.info(log.path).title == typed


def test_reconstruct_events_none_for_legacy_log(tmp_path):
    # A session logged before trace records falls back to flat history.
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hi"})
    log.message({"role": "assistant", "content": "hello"})
    assert SessionLog.reconstruct_events(log.path) is None


def test_model_recorded_last_switch_wins(tmp_path):
    log = SessionLog.new(tmp_path)
    log.model("qwen3:8b")
    log.message({"role": "user", "content": "hi"})
    log.model("gemini:gemini-2.5-pro")  # mid-session switch: last record wins
    log.message({"role": "user", "content": "again"})
    assert SessionLog.info(log.path).model == "gemini:gemini-2.5-pro"
    assert SessionLog.load_entries(tmp_path)[0].info.model == "gemini:gemini-2.5-pro"


def test_model_note_alone_never_touches_the_file(tmp_path):
    # Session order everywhere is file mtime, so opening/resuming (which notes
    # the model) must not write — only real activity reorders the lists. A
    # session with no activity at all must not even create its file.
    log = SessionLog.new(tmp_path)
    log.model("qwen3:8b")
    assert not log.path.exists()
    log.message({"role": "user", "content": "hi"})  # note flushes with activity
    kinds = [json.loads(line)["kind"] for line in log.path.read_text().splitlines()]
    assert kinds == ["model", "message"]


def test_untouched_session_leaves_no_file(tmp_path):
    # Phantom "new chat" sessions (swipe overshoot, server restarts) used to
    # leave a blank file each; blank files must never reach disk.
    log = SessionLog.new(tmp_path)
    log.close()
    assert list(tmp_path.iterdir()) == []


def test_pager_cap_applies_after_skipping_blank_sessions(tmp_path):
    old = make_session(tmp_path, "session-20260101-000000-000000.jsonl", ("user", "real chat"))
    os.utime(old, (1, 1))
    for i in range(5):  # newer blank files (pre-fix debris) must not crowd it out
        (tmp_path / f"session-20260102-00000{i}-000000.jsonl").touch()
    pages = SessionLog.pager_titles(tmp_path, limit=3)
    assert [name for name, _, _, _, _ in pages] == [old.name]


def test_model_empty_for_sessions_without_record(tmp_path):
    path = make_session(tmp_path, "session-20260101-000000-000000.jsonl", ("user", "hi"))
    assert SessionLog.info(path).model == ""


def test_model_records_do_not_pollute_messages(tmp_path):
    log = SessionLog.new(tmp_path)
    log.model("mistral:7b")
    log.message({"role": "user", "content": "hello"})
    assert SessionLog.load_messages(log.path) == [{"role": "user", "content": "hello"}]


def test_search_matches_model_name(tmp_path):
    log = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    log.model("gemini:gemini-2.5-pro")
    log.message({"role": "user", "content": "hello"})
    make_session(tmp_path, "session-20260102-000000-000000.jsonl", ("user", "hello"))

    results = SessionLog.search_sessions(tmp_path, "gemini")
    assert [r.path for r in results] == [log.path]  # modelless session not matched
    assert SessionLog.search_sessions(tmp_path, "gemni")[0].path == log.path  # fuzzy typo


def test_search_model_match_ranks_above_content_match(tmp_path):
    content_hit = make_session(
        tmp_path,
        "session-20260102-000000-000000.jsonl",  # newer, but weaker match
        ("user", "something else"),
        ("assistant", "qwen models are nice"),
    )
    log = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    log.model("qwen3:8b")
    log.message({"role": "user", "content": "hello"})

    results = SessionLog.search_sessions(tmp_path, "qwen")
    assert [r.path for r in results] == [log.path, content_hit]


def test_latest_picks_newest_and_none_when_empty(tmp_path):
    assert SessionLog.latest(tmp_path) is None
    (tmp_path / "session-20260101-000000.jsonl").write_text("")
    (tmp_path / "session-20260201-000000.jsonl").write_text("")
    assert SessionLog.latest(tmp_path).name == "session-20260201-000000.jsonl"


def make_session(tmp_path, name, *messages):
    log = SessionLog(tmp_path / name)
    for role, content in messages:
        log.message({"role": role, "content": content})
    return log.path


def test_info_date_includes_year(tmp_path):
    make_session(tmp_path, "session-20260101-123456-000000.jsonl", ("user", "hi"))
    info = SessionLog.info(tmp_path / "session-20260101-123456-000000.jsonl")
    assert info.when == "2026-01-01 12:34"


def test_snippet_is_last_visible_message(tmp_path):
    make_session(
        tmp_path,
        "session-20260101-000000-000000.jsonl",
        ("user", "fix the build"),
        ("assistant", "Done — the build passes now."),
    )
    info = SessionLog.info(tmp_path / "session-20260101-000000-000000.jsonl")
    assert info.snippet == "Done — the build passes now."
    assert info.mtime > 0


def test_snippet_prefixes_user_and_skips_tool_and_empty(tmp_path):
    log = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    log.message({"role": "user", "content": "run the tests"})
    log.message({"role": "assistant", "content": ""})  # tool-calling turn
    log.message({"role": "tool", "tool_name": "run_command", "content": "3 passed"})
    info = SessionLog.info(log.path)
    assert info.snippet == "You: run the tests"


def test_snippet_truncated(tmp_path):
    make_session(
        tmp_path, "session-20260101-000000-000000.jsonl", ("assistant", "word " * 60)
    )
    info = SessionLog.info(tmp_path / "session-20260101-000000-000000.jsonl")
    assert len(info.snippet) <= 90 and info.snippet.endswith("…")


def test_search_ranks_exact_title_over_phrase_over_content(tmp_path):
    content_hit = make_session(
        tmp_path,
        "session-20260103-000000-000000.jsonl",  # newest, but weakest match
        ("user", "something else"),
        ("assistant", "I will deploy the app for you"),
    )
    phrase_in_title = make_session(
        tmp_path,
        "session-20260102-000000-000000.jsonl",
        ("user", "please Deploy The App now"),
    )
    exact_title = make_session(
        tmp_path,
        "session-20260101-000000-000000.jsonl",  # oldest, but exact
        ("user", "deploy the app"),
    )
    results = SessionLog.search_sessions(tmp_path, "deploy the app")
    assert [r.path for r in results] == [exact_title, phrase_in_title, content_hit]


def test_search_all_words_and_fuzzy_tiers(tmp_path):
    scattered_words = make_session(
        tmp_path,
        "session-20260101-000000-000000.jsonl",
        ("user", "check alpha service"),
        ("assistant", "gamma looks fine"),
    )
    typo_title = make_session(
        tmp_path,
        "session-20260102-000000-000000.jsonl",
        ("user", "alpha gama"),  # fuzzy-close to the query, no exact words
    )
    results = SessionLog.search_sessions(tmp_path, "alpha gamma")
    assert results[0].path == scattered_words  # all words present beats fuzzy
    assert results[1].path == typo_title


def test_search_fuzzy_matches_typoed_words_in_contents(tmp_path):
    hit = make_session(
        tmp_path,
        "session-20260101-000000-000000.jsonl",
        ("user", "restart the server"),
        ("assistant", "I restarted nginx."),  # trailing dot must not block fuzzy
    )
    make_session(
        tmp_path, "session-20260102-000000-000000.jsonl", ("user", "pasta recipe ideas")
    )
    results = SessionLog.search_sessions(tmp_path, "restrat nginz")  # typos in both words
    assert [r.path for r in results] == [hit]


def test_search_ties_break_newest_first(tmp_path):
    older = make_session(
        tmp_path, "session-20260101-000000-000000.jsonl", ("user", "fix the build")
    )
    newer = make_session(
        tmp_path, "session-20260102-000000-000000.jsonl", ("user", "fix the build")
    )
    results = SessionLog.search_sessions(tmp_path, "fix the build")
    assert [r.path for r in results] == [newer, older]


def test_search_no_match_and_empty_query(tmp_path):
    make_session(tmp_path, "session-20260101-000000-000000.jsonl", ("user", "hello"))
    assert SessionLog.search_sessions(tmp_path, "quantum chromodynamics") == []
    assert SessionLog.search_sessions(tmp_path, "   ") == []


def test_rank_empty_query_lists_all_newest_first(tmp_path):
    make_session(tmp_path, "session-20260101-000000-000000.jsonl", ("user", "older"))
    make_session(tmp_path, "session-20260102-000000-000000.jsonl", ("user", "newer"))
    entries = SessionLog.load_entries(tmp_path)
    assert [info.title for info in SessionLog.rank(entries, "")] == ["newer", "older"]
    assert [info.title for info in SessionLog.rank(entries, "  ")] == ["newer", "older"]


def test_search_respects_exclude(tmp_path):
    path = make_session(tmp_path, "session-20260101-000000-000000.jsonl", ("user", "hello"))
    assert SessionLog.search_sessions(tmp_path, "hello", exclude={path}) == []


def test_listing_and_search_never_modify_session_files(tmp_path):
    path = make_session(
        tmp_path, "session-20260101-000000-000000.jsonl", ("user", "keep me forever")
    )
    before = path.read_bytes()
    SessionLog.list_sessions(tmp_path)
    SessionLog.search_sessions(tmp_path, "keep")
    SessionLog.load_messages(path)
    assert path.read_bytes() == before


def test_load_skips_garbage_lines_and_system(tmp_path):
    path = tmp_path / "session-x.jsonl"
    path.write_text(
        'not json\n'
        '{"kind":"message","role":"system","content":"stale"}\n'
        '{"kind":"message","role":"user","content":"q"}\n'
    )
    assert SessionLog.load_messages(path) == [{"role": "user", "content": "q"}]


class TestSearchExcerpts:
    """#14: the model-facing search_sessions tool output."""

    def test_search_mode_lists_sessions_with_snippets(self, tmp_path):
        make_session(
            tmp_path,
            "session-20260101-000000-000000.jsonl",
            ("user", "the uv sync kept failing"),
            ("assistant", "the fix was to pin uv to 0.5 in pyproject"),
        )
        make_session(tmp_path, "session-20260102-000000-000000.jsonl", ("user", "pasta"))
        out = SessionLog.search_excerpts(tmp_path, "uv sync failing")
        assert "session-20260101-000000-000000.jsonl" in out
        assert "uv sync kept failing" in out
        assert "session=" in out  # tells the model how to drill down
        assert "pasta" not in out

    def test_search_mode_no_match(self, tmp_path):
        make_session(tmp_path, "session-20260101-000000-000000.jsonl", ("user", "hello"))
        assert "No past session matches" in SessionLog.search_excerpts(tmp_path, "zzz")

    def test_search_mode_requires_query(self, tmp_path):
        assert SessionLog.search_excerpts(tmp_path, "   ").startswith("ERROR")

    def test_search_excludes_current_session(self, tmp_path):
        path = make_session(
            tmp_path, "session-20260101-000000-000000.jsonl", ("user", "unique needle")
        )
        out = SessionLog.search_excerpts(tmp_path, "unique needle", exclude={path})
        assert "No past session matches" in out

    def test_detail_mode_returns_matching_messages(self, tmp_path):
        make_session(
            tmp_path,
            "session-20260101-000000-000000.jsonl",
            ("user", "why does uv sync fail?"),
            ("assistant", "because the lock file is stale"),
            ("user", "unrelated chatter"),
        )
        out = SessionLog.search_excerpts(
            tmp_path, "uv sync", session="session-20260101-000000-000000.jsonl"
        )
        assert "[user] why does uv sync fail?" in out
        assert "unrelated chatter" not in out

    def test_detail_mode_empty_query_shows_tail(self, tmp_path):
        make_session(
            tmp_path,
            "session-20260101-000000-000000.jsonl",
            ("user", "first message"),
            ("assistant", "final answer here"),
        )
        out = SessionLog.search_excerpts(
            tmp_path, "", session="session-20260101-000000-000000.jsonl"
        )
        assert "final answer here" in out and "most recent" in out

    def test_detail_mode_rejects_bad_names(self, tmp_path):
        for bad in ("../etc/passwd", "session-1/../x.jsonl", "notes.txt", "session-¤.jsonl"):
            assert SessionLog.search_excerpts(tmp_path, "x", session=bad).startswith("ERROR")

    def test_detail_mode_missing_file(self, tmp_path):
        out = SessionLog.search_excerpts(
            tmp_path, "x", session="session-20990101-000000-000000.jsonl"
        )
        assert out.startswith("ERROR: no such session")

    def test_detail_mode_output_is_capped(self, tmp_path):
        from aish.session import DETAIL_MAX_CHARS

        big = [("assistant", f"needle block {i} " + "x" * 800) for i in range(30)]
        make_session(tmp_path, "session-20260101-000000-000000.jsonl", *big)
        out = SessionLog.search_excerpts(
            tmp_path, "needle", session="session-20260101-000000-000000.jsonl"
        )
        assert len(out) < DETAIL_MAX_CHARS + 500
        assert "more messages omitted" in out


# --- Terminal-mode command history (#104) ---------------------------------

def _user_cmd(log, command, exit_code=0):
    """Log a user-direct ! command exactly as server._run_user_command does:
    the user-direct audit record, then its terminal-block cmd_start/cmd_end."""
    log.command(command, "user-direct")
    log.command_event({"kind": "cmd_start", "cwd": "/x", "command": command, "user": True})
    log.command_event({"kind": "cmd_end", "status": "exit", "exit_code": exit_code})


def _model_cmd(log, command, exit_code=0):
    """Log a model tool-loop command: an approval decision, not user-direct."""
    log.command(command, "approved")
    log.command_event({"kind": "cmd_start", "cwd": "/x", "command": command})
    log.command_event({"kind": "cmd_end", "status": "exit", "exit_code": exit_code})


def test_user_command_history_excludes_model_and_failures(tmp_path):
    log = SessionLog.new(tmp_path)
    _user_cmd(log, "ls -la")           # user, ok → included
    _user_cmd(log, "grep foo", 1)      # user, failed → excluded
    _model_cmd(log, "rm -rf build")    # model command → never included
    log.close()
    assert SessionLog.user_command_history(tmp_path) == ["ls -la"]


def test_user_command_history_ranks_by_frequency_then_recency(tmp_path):
    log = SessionLog.new(tmp_path)
    _user_cmd(log, "git status")   # run 3x → most frequent
    _user_cmd(log, "ls")
    _user_cmd(log, "git status")
    _user_cmd(log, "pwd")
    _user_cmd(log, "git status")
    _user_cmd(log, "ls")           # ls at 2, pwd at 1
    log.close()
    # git status:3, ls:2, pwd:1 → frequency descending.
    assert SessionLog.user_command_history(tmp_path) == ["git status", "ls", "pwd"]


def test_user_command_history_recency_tiebreak(tmp_path):
    log = SessionLog.new(tmp_path)
    _user_cmd(log, "make test")   # each run once
    _user_cmd(log, "make lint")   # more recent than make test
    log.close()
    # Equal frequency (1 each): most-recent first.
    assert SessionLog.user_command_history(tmp_path) == ["make lint", "make test"]


def test_user_command_history_keeps_alias_verbatim(tmp_path):
    log = SessionLog.new(tmp_path)
    _user_cmd(log, "ll")   # an alias the user typed — stored/suggested as-is
    log.close()
    assert SessionLog.user_command_history(tmp_path) == ["ll"]


def test_user_command_history_case_preserved_for_dedup(tmp_path):
    # Stored verbatim: differently-cased strings are distinct commands (the
    # frontend prefix-matches case-insensitively, but we suggest what was typed).
    log = SessionLog.new(tmp_path)
    _user_cmd(log, "Git status")
    _user_cmd(log, "git status")
    log.close()
    assert set(SessionLog.user_command_history(tmp_path)) == {"Git status", "git status"}


def test_user_command_history_cd_excluded(tmp_path):
    # !cd goes through rebase and emits NO cmd_end, so it never qualifies as a
    # confirmed exit-0 command and drops out of the palette.
    log = SessionLog.new(tmp_path)
    log.command("cd /tmp", "user-direct")  # no cmd_start/cmd_end
    _user_cmd(log, "ls")
    log.close()
    assert SessionLog.user_command_history(tmp_path) == ["ls"]


def test_user_command_history_aggregates_across_sessions(tmp_path):
    older = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    _user_cmd(older, "ls")
    _user_cmd(older, "git status")
    older.close()
    newer = SessionLog(tmp_path / "session-20260102-000000-000000.jsonl")
    _user_cmd(newer, "git status")   # 2 total, and most recent
    newer.close()
    # Distinct mtimes so cross-session recency is deterministic.
    os.utime(older.path, (1_700_000_000, 1_700_000_000))
    os.utime(newer.path, (1_700_000_100, 1_700_000_100))
    assert SessionLog.user_command_history(tmp_path) == ["git status", "ls"]


def test_user_command_history_capped(tmp_path):
    log = SessionLog.new(tmp_path)
    for i in range(10):
        _user_cmd(log, f"cmd{i}")
    log.close()
    assert len(SessionLog.user_command_history(tmp_path, limit=3)) == 3


# --- restart recovery (#164) ------------------------------------------------
# An interrupted task is one whose task_start never got its task_end: a killed
# process writes nothing, so absence of the end marker IS the evidence.


def test_pending_task_none_when_task_finished(tmp_path):
    log = SessionLog.new(tmp_path)
    log.task_start("do the thing")
    log.message({"role": "user", "content": "do the thing"})
    log.message({"role": "assistant", "content": "done"})
    log.task_end()
    log.close()
    assert SessionLog.pending_task(log.path) is None


def test_pending_task_reports_unfinished_task(tmp_path):
    log = SessionLog.new(tmp_path)
    log.task_start("first")
    log.task_end()
    log.task_start("second")  # killed mid-run
    log.message({"role": "user", "content": "second"})
    log.close()
    pending = SessionLog.pending_task(log.path)
    assert pending["prompt"] == "second"
    assert pending["attempts"] == 1


def test_pending_task_counts_attempts_since_last_end(tmp_path):
    log = SessionLog.new(tmp_path)
    for _ in range(3):
        log.task_start("keeps dying")
    assert SessionLog.pending_task(log.path)["attempts"] == 3
    log.task_end()
    assert SessionLog.pending_task(log.path) is None
    log.close()


def test_pending_task_ignores_logs_without_markers(tmp_path):
    # A CLI session (which never writes task markers) is never resumable.
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hi"})
    log.close()
    assert SessionLog.pending_task(log.path) is None


def test_interrupted_sessions_newest_first_within_window(tmp_path):
    stale = SessionLog(tmp_path / "session-20260101-000000-000000.jsonl")
    stale.task_start("old and abandoned")
    stale.close()
    fresh = SessionLog(tmp_path / "session-20260102-000000-000000.jsonl")
    fresh.task_start("interrupted just now")
    fresh.close()
    finished = SessionLog(tmp_path / "session-20260103-000000-000000.jsonl")
    finished.task_start("ran fine")
    finished.task_end()
    finished.close()
    now = time.time()
    os.utime(stale.path, (now - 86400, now - 86400))
    os.utime(fresh.path, (now - 60, now - 60))
    os.utime(finished.path, (now - 30, now - 30))

    found = SessionLog.interrupted_sessions(tmp_path, max_age_secs=3600)
    assert [p.name for p, _ in found] == [fresh.path.name]
    assert found[0][1]["prompt"] == "interrupted just now"
    # A wider window reaches the stale one too, newest first.
    wide = SessionLog.interrupted_sessions(tmp_path, max_age_secs=7 * 86400)
    assert [p.name for p, _ in wide] == [fresh.path.name, stale.path.name]


def test_interrupted_sessions_empty_state_dir(tmp_path):
    assert SessionLog.interrupted_sessions(tmp_path, max_age_secs=3600) == []


def test_pending_task_names_in_flight_steps(tmp_path):
    # A tool_start with no matching tool is a step whose effect is unknown —
    # exactly what the resumed run must verify rather than blindly repeat.
    log = SessionLog.new(tmp_path)
    log.task_start("answer the mail")
    log.step({"kind": "tool_start", "name": "gmail_search", "summary": "id:abc"})
    log.step({"kind": "tool", "name": "gmail_search", "ok": True})
    log.step({"kind": "tool_start", "name": "gmail_send", "summary": "to pawel@wenda.eu"})
    log.close()  # killed here: the send never reported back
    pending = SessionLog.pending_task(log.path)
    assert pending["in_flight"] == ["gmail_send: to pawel@wenda.eu"]


def test_pending_task_in_flight_empty_when_every_step_finished(tmp_path):
    log = SessionLog.new(tmp_path)
    log.task_start("go")
    log.step({"kind": "tool_start", "name": "read_url", "summary": "https://x"})
    log.step({"kind": "tool", "name": "read_url", "ok": True})
    log.close()
    assert SessionLog.pending_task(log.path)["in_flight"] == []


def test_pending_task_in_flight_resets_per_attempt(tmp_path):
    # Steps from an earlier interrupted attempt are not reported again.
    log = SessionLog.new(tmp_path)
    log.task_start("go")
    log.step({"kind": "tool_start", "name": "web_search", "summary": "old"})
    log.task_start("go")  # attempt 2, nothing started yet
    log.close()
    pending = SessionLog.pending_task(log.path)
    assert pending["attempts"] == 2 and pending["in_flight"] == []


def test_pending_task_in_flight_uses_command_for_shell_steps(tmp_path):
    log = SessionLog.new(tmp_path)
    log.task_start("build")
    log.step({"kind": "tool_start", "name": "run_command", "command": "make release"})
    log.close()
    assert SessionLog.pending_task(log.path)["in_flight"] == ["run_command: make release"]


# ---- auto-titling (#175) ---------------------------------------------------


def test_set_title_records_whether_the_model_or_the_user_named_it(tmp_path):
    log = SessionLog.new(tmp_path)
    log.set_title("Model chose this", auto=True)
    log.close()
    parsed = SessionLog._parse(log.path)
    assert parsed.title == "Model chose this" and parsed.title_auto is True


def test_a_hand_typed_rename_after_an_auto_title_wins(tmp_path):
    log = SessionLog.new(tmp_path)
    log.set_title("Model chose this", auto=True)
    log.set_title("I chose this")  # a rename defaults to manual
    log.close()
    parsed = SessionLog._parse(log.path)
    assert parsed.title == "I chose this" and parsed.title_auto is False


def test_a_title_record_from_before_auto_titling_reads_as_manual(tmp_path):
    """Every title record written before #175 was a hand-typed rename, so a
    missing `auto` flag must never be read as "the model can replace this"."""
    log = SessionLog.new(tmp_path)
    log._record("title", title="An old rename")  # no auto key at all
    log.close()
    assert SessionLog._parse(log.path).title_auto is False


def test_title_drifted_is_false_while_the_subject_words_are_still_used():
    assert not title_drifted(
        "Bali eSIM data plans", "user: which Bali eSIM has the best data plans?"
    )


def test_title_drifted_is_true_once_the_conversation_moves_on():
    assert title_drifted("Bali eSIM data plans", "user: explain postgres index tuning")


def test_title_drifted_ignores_short_words_that_carry_no_subject():
    """'the', 'a', 'is' match everything — a gate built on them never fires."""
    assert title_drifted("The cost of a trip", "user: is it the one that a person is on")


def test_title_drifted_on_a_title_with_nothing_to_match():
    assert title_drifted("", "anything at all")
    assert title_drifted("a b c", "a b c")  # all words below the length floor


# ---- parse cache (perf: session-list / pager scans) -------------------------


def test_cached_parse_serves_unchanged_files_and_notices_appends(tmp_path):
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "first"})
    first = SessionLog._cached_parse(log.path)
    assert SessionLog._cached_parse(log.path) is first  # same object = cache hit
    log.message({"role": "assistant", "content": "reply"})
    fresh = SessionLog._cached_parse(log.path)
    assert fresh is not first
    assert [m["content"] for m in fresh.messages] == ["first", "reply"]


def test_load_entries_reflects_new_messages_and_renames(tmp_path):
    """The entry cache must never serve a stale search vocabulary: appending a
    message or renaming the chat changes the stat key, so the entry rebuilds."""
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "talk about xylophones"})
    assert "xylophones" in SessionLog.load_entries(tmp_path)[0].content_cf
    log.message({"role": "assistant", "content": "and now quokkas"})
    log.set_title("Renamed chat")
    entry = SessionLog.load_entries(tmp_path)[0]
    assert "quokkas" in entry.content_cf
    assert entry.info.title == "Renamed chat"


def test_recency_scan_prunes_caches_for_deleted_sessions_only(tmp_path):
    from aish import session as session_module

    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "hello"})
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    other = SessionLog.new(other_dir)
    other.message({"role": "user", "content": "other dir"})
    SessionLog.load_entries(tmp_path)
    SessionLog.load_entries(other_dir)
    assert str(log.path) in session_module._PARSE_CACHE
    log.close()
    log.path.unlink()
    SessionLog._by_recency(tmp_path)
    assert str(log.path) not in session_module._PARSE_CACHE
    assert str(log.path) not in session_module._ENTRY_CACHE
    # Another state dir's entries are not this dir's to prune.
    assert str(other.path) in session_module._PARSE_CACHE


def test_user_command_history_reflects_commands_run_after_a_cached_scan(tmp_path):
    log = SessionLog.new(tmp_path)
    _user_cmd(log, "ls")
    assert SessionLog.user_command_history(tmp_path) == ["ls"]
    _user_cmd(log, "git status")
    assert set(SessionLog.user_command_history(tmp_path)) == {"ls", "git status"}


class TestLastTurn:
    """The turn counter is the trace contract's join key, and it lives on the
    agent — so a reopened chat has to be told where its own log left off."""

    def test_it_reports_the_highest_turn_the_log_used(self, tmp_path):
        log = SessionLog.new(tmp_path)
        log.step({"kind": "tool", "turn": 1, "name": "run_command"})
        log.step({"kind": "rule_eval", "turn": 4, "evaluated": []})
        log.step({"kind": "tool", "turn": 2, "name": "read_url"})
        assert SessionLog.last_turn(log.path) == 4

    def test_a_pre_contract_log_reports_zero(self, tmp_path):
        """Old logs carry no stamps at all; the resumed agent then starts at 1
        exactly as it does today, which is right — nothing to collide with."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "hello"})
        log.step({"kind": "tool", "name": "run_command"})
        assert SessionLog.last_turn(log.path) == 0

    def test_a_ratings_turn_id_is_a_different_namespace(self, tmp_path):
        """`rating` records carry a top-level `turn` that is the CLIENT's event
        id — a string naming a different thing. Reading it as the counter would
        make the high-water mark depend on whether the owner tapped a thumb."""
        log = SessionLog.new(tmp_path)
        log.step({"kind": "tool", "turn": 2, "name": "run_command"})
        log._record("rating", turn="t-abc123", rating="up")
        assert SessionLog.last_turn(log.path) == 2


class TestWriteLock:
    """SessionLog._record is reached from more than one thread (the agent
    worker logs the conversation while the event loop writes a rename or a
    console-open audit record). An interleaved write on the buffered handle is
    a torn JSONL line, and every reader `continue`s past garbage — silent
    corruption. One per-instance lock serializes all file writes (#178 P2)."""

    # Big enough that a single record exceeds the io buffer (8 KB), i.e. the
    # multi-writer pattern the finding describes: a write that the handle
    # cannot land in one buffered chunk.
    PAYLOAD = "x" * 64 * 1024

    def _hammer(self, log, n_threads=8, n_records=25):
        """N threads × M mixed-kind records through one SessionLog; returns
        the exceptions the writer threads raised (must be none)."""
        barrier = threading.Barrier(n_threads)
        errors = []

        def writer(tid):
            try:
                # All threads race the very first record, so the lazy
                # handle-open races too — the widest lock-less window.
                barrier.wait(timeout=10)
                for i in range(n_records):
                    marker = f"payload-{tid}-{i}:"
                    if i % 3 == 0:
                        log.set_title(f"title {tid}-{i} {self.PAYLOAD}")
                    elif i % 3 == 1:
                        log.step({"type": "tool", "output": marker + self.PAYLOAD})
                    else:
                        log.message({"role": "user", "content": marker + self.PAYLOAD})
            except Exception as exc:  # pragma: no cover - only without the lock
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not any(t.is_alive() for t in threads), "writer threads hung"
        return errors

    def test_concurrent_writers_never_tear_a_line(self, tmp_path):
        # Integrity guard: catches tearing wherever the platform lets it
        # happen. On CPython/macOS the GIL + BufferedWriter's own lock make
        # single-handle appends line-atomic, so the deterministic lock-less
        # failure lives in test_rewind_racing_writers_never_errors_or_tears
        # (verified: it fails every run with the lock removed).
        n_threads, n_records = 8, 25
        log = SessionLog.new(tmp_path)
        errors = self._hammer(log, n_threads, n_records)
        log.close()

        assert errors == []
        lines = log.path.read_text(encoding="utf-8").splitlines()
        # Every line is valid JSON — no interleaving artifacts.
        records = [json.loads(line) for line in lines]
        assert len(records) == n_threads * n_records
        # Every record arrived whole: each non-title payload appears exactly
        # once, intact, in the record kind its writer chose.
        seen = set()
        for record in records:
            body = record.get("content") or record.get("step", {}).get("output") or ""
            if body.startswith("payload-"):
                marker, _, payload = body.partition(":")
                assert payload == self.PAYLOAD
                seen.add(marker)
        expected = {
            f"payload-{tid}-{i}"
            for tid in range(n_threads)
            for i in range(n_records)
            if i % 3 != 0
        }
        assert seen == expected

    def test_rewind_racing_writers_never_errors_or_tears(self, tmp_path):
        # The other write shape: rewind_last_turn closes and rewrites the file
        # while another thread appends. Lock-less this raises ("I/O operation
        # on closed file") or tears; locked it just serializes.
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "seed"})
        stop = threading.Event()
        errors = []

        def appender():
            try:
                while not stop.is_set():
                    log.message({"role": "user", "content": "turn " + self.PAYLOAD})
            except Exception as exc:  # pragma: no cover - only without the lock
                errors.append(exc)

        thread = threading.Thread(target=appender)
        thread.start()
        try:
            for _ in range(50):
                log.rewind_last_turn()
        finally:
            stop.set()
            thread.join(timeout=30)
        assert not thread.is_alive()
        log.close()
        assert errors == []
        for line in log.path.read_text(encoding="utf-8").splitlines():
            json.loads(line)  # every surviving line is whole

    def test_writers_do_not_deadlock(self, tmp_path):
        # No-deadlock sanity: a plain record from one thread completes while
        # another thread writes — generous timeout, no nesting tricks.
        log = SessionLog.new(tmp_path)
        done = threading.Event()

        def other():
            for i in range(200):
                log.step({"type": "note", "text": f"bg {i}"})
            done.set()

        thread = threading.Thread(target=other)
        thread.start()
        for i in range(200):
            log.message({"role": "user", "content": f"fg {i}"})
        assert done.wait(timeout=30), "background writer never finished"
        thread.join(timeout=30)
        assert not thread.is_alive()
        log.close()
        records = [
            json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(records) == 400


class TestActivityIsNotAFileTouch:
    """"Last interaction" used to be the file's mtime, which cannot tell a chat
    that DID something from one that was merely looked at (#201).

    The chat that surfaced it held three remote images off the fetch whitelist.
    Every replay re-reported them, the report was written to the log, and the
    write landed a second or so AFTER the client had stamped "seen" — so the
    chat came back unread the moment you left it, forever, and opening it to
    check was what re-armed the signal. 53 identical records had accumulated.

    The rule: a record that renders nowhere is not activity.
    """

    def _activity(self, log):
        return SessionLog.info(log.path).activity

    def test_a_renderless_record_does_not_count_as_activity(self, tmp_path):
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "find me a thermal mug"})
        log.message({"role": "assistant", "content": "here are three"})
        after_talking = self._activity(log)

        # What merely LOOKING at the chat writes: a lazily-flushed model record
        # and the browser reporting images that will never render.
        time.sleep(1.1)  # ISO stamps are whole seconds
        log.model("gemini:gemini-3.5-flash")
        log.step({"kind": "render_error", "what": "image", "items": ["https://x/y.jpg"]})
        log.close()

        assert self._activity(log) == after_talking, "a glance is not activity"
        assert log.path.stat().st_mtime > after_talking, "the file DID change"

    def test_a_real_message_after_a_glance_does_count(self, tmp_path):
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "hello"})
        glanced = self._activity(log)
        time.sleep(1.1)
        log.step({"kind": "render_error", "what": "image", "items": ["https://x/y.jpg"]})
        log.message({"role": "user", "content": "and one more thing"})
        log.close()
        assert self._activity(log) > glanced

    def test_every_renderless_kind_is_covered_by_the_registry(self, tmp_path):
        """Not just render_error: the #192 governance records are the same
        shape, and a future one must inherit this without being re-taught."""
        from aish.session import RENDERLESS_STEPS

        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "hello"})
        quiet = self._activity(log)
        time.sleep(1.1)
        for kind in RENDERLESS_STEPS:
            log.step({"kind": kind})
        log.close()
        assert self._activity(log) == quiet

    def test_a_rendering_trace_step_is_activity(self, tmp_path):
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "hello"})
        quiet = self._activity(log)
        time.sleep(1.1)
        log.step({"kind": "tool", "name": "run_command"})
        log.close()
        assert self._activity(log) > quiet, "a tool step is something happening"

    def test_a_log_with_no_usable_stamps_falls_back_to_mtime(self, tmp_path):
        """Nothing may lose its place in the list. A record with an unparseable
        stamp still has to sort somewhere, and mtime is the old answer."""
        path = tmp_path / "session-20260101-000000-000000.jsonl"
        path.write_text(
            json.dumps({"kind": "message", "role": "user", "content": "hi"}) + "\n",
            encoding="utf-8",
        )
        info = SessionLog.info(path)
        assert info.activity == info.mtime == path.stat().st_mtime

    def test_the_pager_ages_pages_by_activity_too(self, tmp_path):
        """The deck evicts members on this stamp; a glance must not refresh a
        chat's standing there either."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "hello"})
        log.set_title("Kubek")
        talked = SessionLog.pager_titles(tmp_path)[0][3]
        time.sleep(1.1)
        log.step({"kind": "render_error", "what": "image", "items": ["https://x/y.jpg"]})
        log.close()
        assert SessionLog.pager_titles(tmp_path)[0][3] == talked


class TestOutputStamp:
    """Unread means there is something to READ (#203).

    One stamp was doing two jobs. `activity` answers "did anything happen
    here", which is the right way to ORDER a list — a chat mid-turn is the most
    recent thing there is. Unread was reading the same number, so every
    thinking step a chat took moved it past the device's last look and the row
    marked itself unread with nothing new behind it. Three consumer-side
    patches went in before the fact itself was split.

    The rule: output is what `reconstruct_events` turns into transcript
    content — a user bubble, or the assistant text that becomes a turn's
    answer. Everything else it emits is a trace step or a marker, and
    everything it ignores was never on screen at all.
    """

    def _stamps(self, log):
        info = SessionLog.info(log.path)
        return info.activity, info.output

    def test_thinking_is_activity_but_not_output(self, tmp_path):
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "count the files"})
        log.message({"role": "assistant", "content": "there are 12"})
        _, answered = self._stamps(log)

        time.sleep(1.1)  # ISO stamps are whole seconds
        log.step({"kind": "thinking", "text": "hmm"})
        log.step({"kind": "tool", "name": "run_command"})
        log.close()

        activity, output = self._stamps(log)
        assert activity > answered, "a working chat IS the most recent thing"
        assert output == answered, "…but it has produced nothing to read"

    def test_the_answer_that_follows_is_output(self, tmp_path):
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "count the files"})
        _, asked = self._stamps(log)
        time.sleep(1.1)
        log.step({"kind": "tool", "name": "run_command"})
        log.message({"role": "assistant", "content": "there are 12"})
        log.close()
        assert self._stamps(log)[1] > asked

    def test_a_tool_result_is_not_output(self, tmp_path):
        """It renders inside the trace card, never as a message."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "go"})
        _, asked = self._stamps(log)
        time.sleep(1.1)
        log.message({"role": "tool", "content": "12", "tool_name": "run_command"})
        log.close()
        assert self._stamps(log)[1] == asked

    def test_an_empty_assistant_turn_is_not_output(self, tmp_path):
        """A tool-calling turn carries no visible text — reconstruct_events
        keeps only the last NON-EMPTY answer, so neither may this."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "go"})
        _, asked = self._stamps(log)
        time.sleep(1.1)
        log.message({"role": "assistant", "content": ""})
        log.close()
        assert self._stamps(log)[1] == asked

    def test_aish_own_note_is_not_output(self, tmp_path):
        """`[aish: …]` never reached the transcript live and is skipped on
        replay (#171), so it cannot be something you have not read."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "go"})
        _, asked = self._stamps(log)
        time.sleep(1.1)
        log.message({"role": "user", "content": "[aish: resumed after a restart]"})
        log.close()
        assert self._stamps(log)[1] == asked

    def test_a_trigger_prompt_is_output(self, tmp_path):
        """A triggered session's own prompt IS a message in the chat — it is
        the one thing an overnight job has to show you before it answers."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "reply to the invoice email"})
        assert self._stamps(log)[1] > 0

    def test_a_rename_is_activity_but_not_output(self, tmp_path):
        """Renaming on the laptop must not mark the chat unread on the phone."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "hello"})
        _, said = self._stamps(log)
        time.sleep(1.1)
        log.set_title("Invoices")
        log.close()
        activity, output = self._stamps(log)
        assert activity > said and output == said

    def test_a_removal_is_activity_but_not_output(self, tmp_path):
        """Deleting a turn elsewhere changes the chat (#202) — the list must
        re-sort — but nothing new arrived to read."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "first question"})
        log.message({"role": "assistant", "content": "first answer"})
        log.message({"role": "user", "content": "second question"})
        log.message({"role": "assistant", "content": "second answer"})
        _, said = self._stamps(log)
        records = [json.loads(line) for line in log.path.read_text().splitlines()]
        turn = next(r["turn"] for r in records if r.get("turn"))
        time.sleep(1.1)
        log.redact_turn(turn)
        log.close()
        activity, output = self._stamps(log)
        assert activity > said and output == said

    def test_no_output_reports_zero_rather_than_guessing(self, tmp_path):
        """A zero reads downstream as "fall back to the activity stamp". An
        mtime fallback here would instead claim the chat just spoke, on every
        log the process happens to touch."""
        path = tmp_path / "session-20260101-000000-000000.jsonl"
        path.write_text(
            json.dumps({"kind": "message", "role": "user", "content": "hi"}) + "\n",
            encoding="utf-8",
        )
        info = SessionLog.info(path)
        assert info.output == 0.0 and info.activity == info.mtime

    def test_a_failed_turn_is_output(self, tmp_path):
        """A turn that dies produces no assistant message, and its failure text
        went out as a live event only — so a background job that died at 3am
        had nothing in the log to raise a dot with."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "run the nightly job"})
        _, asked = self._stamps(log)
        time.sleep(1.1)
        log.task_start("run the nightly job")
        log.step({"kind": "tool_start", "name": "run_command"})
        log.task_end("failed", "task failed: BackendError('no route to host')")
        log.close()
        assert self._stamps(log)[1] > asked

    def test_a_turn_that_ended_fine_is_not_output_by_itself(self, tmp_path):
        """Its ANSWER is the output; the marker must not double as one, or
        every completed turn would stamp twice for the same event."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "go"})
        log.message({"role": "assistant", "content": "done"})
        _, answered = self._stamps(log)
        time.sleep(1.1)
        log.task_end()
        log.close()
        assert self._stamps(log)[1] == answered

    def test_a_task_end_with_no_status_predates_the_field(self, tmp_path):
        """Old logs say nothing about how their turns ended, and silence must
        stay silence rather than becoming a retroactive failure."""
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "hi"})
        log.close()
        _, spoke = self._stamps(log)
        time.sleep(1.1)
        # A task_end in the shape written before `status` existed.
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with log.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": stamp, "kind": "task_end"}) + "\n")
        activity, output = self._stamps(log)
        assert activity > spoke, "it is still something that happened"
        assert output == spoke, "…but says nothing about how the turn ended"

    def test_the_pager_carries_both_stamps(self, tmp_path):
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "hello"})
        log.set_title("Kubek")
        time.sleep(1.1)
        log.step({"kind": "tool", "name": "run_command"})
        log.close()
        name, title, origin, ts, out = SessionLog.pager_titles(tmp_path)[0]
        assert ts > out > 0, "the pager ages by activity and flags unread by output"


class TestAnswerIdentity:
    """A fork branches from a NAMED answer, not from a counted one (#229).

    The fork point used to be an ordinal: the browser counted answers as it
    rendered them, the server counted them again over the whole log, and the two
    agreed only when the browser had rendered all of them. It had not — the
    replay is capped (#228) — so a 25-answer chat whose view began at answer 15
    sent "6" for the answer the owner tapped as 20, and the server cut at ITS
    sixth. Fourteen answers of context, and the photos in them, gone with no
    error anywhere.

    Two counts of one thing is the defect; these pin the single name that
    replaced them. Note the second divergence underneath, which the ordinal
    could never have survived either: `reconstruct_events` and
    `truncate_at_answer` do not even agree on WHICH records are answers.
    """

    def _log(self, tmp_path):
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "one"})
        log.message({"role": "assistant", "content": "answer one"})
        log.message({"role": "user", "content": "two"})
        log.message({"role": "assistant", "content": "narrating", "interim": True})
        log.message({"role": "assistant", "content": "answer two"})
        log.message({"role": "user", "content": "three"})
        log.message({"role": "assistant", "content": "answer three"})
        log.step({"kind": "tool", "name": "read_docs"})  # a trace record makes it replayable
        log.close()
        return log

    def test_message_returns_the_id_it_wrote(self, tmp_path):
        log = SessionLog.new(tmp_path)
        answer = log.message({"role": "assistant", "content": "hi"})
        log.close()
        assert answer, "an assistant record is named, like a user turn is"
        records = [json.loads(line) for line in log.path.read_text().splitlines()]
        assert records[0]["turn"] == answer

    def test_every_answer_event_carries_its_record_id(self, tmp_path):
        log = self._log(tmp_path)
        events = SessionLog.reconstruct_events(log.path)
        answers = [e for e in events if e["type"] == "done"]
        ids = [e.get("answer") for e in answers]
        assert len(ids) == 3 and all(ids), "every replayed answer names its record"
        assert len(set(ids)) == 3, "and no two answers share a name"

    def test_forking_by_id_cuts_at_that_answer(self, tmp_path):
        log = self._log(tmp_path)
        text = log.path.read_text(encoding="utf-8")
        events = SessionLog.reconstruct_events(log.path)
        second = [e for e in events if e["type"] == "done"][1]

        forked = SessionLog.truncate_at_answer_id(text, second["answer"])
        assert "answer two" in forked
        assert "answer three" not in forked
        assert "three" not in forked, "the turn after the fork point is not carried"
        # The narration on the way to that answer belongs to the turn and stays.
        assert "narrating" in forked

    def test_an_id_that_names_nothing_is_refused_not_guessed(self, tmp_path):
        log = self._log(tmp_path)
        text = log.path.read_text(encoding="utf-8")
        assert SessionLog.truncate_at_answer_id(text, "no-such-answer") is None
        assert SessionLog.truncate_at_answer_id(text, "") is None

    def test_a_log_written_before_ids_still_forks_by_id(self, tmp_path):
        # Records with no `turn` fall back to their LINE INDEX, which names a
        # record perfectly well in an append-only file — so the mechanism covers
        # every chat already on disk, not only the ones written from now on.
        path = tmp_path / "session-20260101-000000.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {"kind": "message", "role": "user", "content": "one"},
                    {"kind": "message", "role": "assistant", "content": "old answer"},
                    {"kind": "trace", "step": {"kind": "tool", "name": "read_docs"}},
                    {"kind": "message", "role": "user", "content": "two"},
                    {"kind": "message", "role": "assistant", "content": "later answer"},
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        events = SessionLog.reconstruct_events(path)
        first = [e for e in events if e["type"] == "done"][0]
        assert first["answer"] == "@1", "a nameless record answers to its line"
        forked = SessionLog.truncate_at_answer_id(path.read_text(), first["answer"])
        assert "old answer" in forked and "later answer" not in forked

    def test_the_two_counts_the_ordinal_relied_on_can_disagree(self, tmp_path):
        # WHY the ordinal could not simply be repaired: the two sides do not
        # count the same records. `truncate_at_answer` calls an assistant record
        # final when no `tool` message follows it; `reconstruct_events` — what
        # the browser renders, and therefore what it counts — promotes the last
        # thing said in each TURN, whatever came after it.
        #
        # A turn that spoke, called a tool and was then stopped is where they
        # part: the browser shows an answer bubble for it, the cutter does not
        # count it at all. So ordinal 1 means two different records, and the
        # divergence grows by one for every such turn in the chat.
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "one"})
        log.message({"role": "assistant", "content": "let me look"})
        log.message({"role": "tool", "tool_name": "read_docs", "content": "docs"})
        log.message({"role": "user", "content": "two"})
        log.message({"role": "assistant", "content": "the second answer"})
        log.step({"kind": "tool", "name": "read_docs"})
        log.close()

        text = log.path.read_text(encoding="utf-8")
        events = SessionLog.reconstruct_events(log.path)
        rendered = [e for e in events if e["type"] == "done"]
        assert [e["result"] for e in rendered] == ["let me look", "the second answer"]

        # The first Fork button in that chat sits on "let me look" — and sending
        # its ordinal would have branched past the whole second turn.
        assert "the second answer" in SessionLog.truncate_at_answer(text, 1)
        assert "the second answer" not in SessionLog.truncate_at_answer_id(
            text, rendered[0]["answer"]
        )


class TestAttachmentForms:
    """A message has two forms and they are not the same string (#231).

    The RECORD form is what the log keeps and the owner sees: `![[cat.png]]`.
    The GUIDANCE form is what a model is handed — a sentence per file saying
    whether it can look at it or must open it — and depends on which backend is
    answering, so it is built at hand-over and never stored.

    They used to be one string, written for the model, which meant it was the
    ONLY record that a message had a photo. So everything facing the owner had
    to undo it: the bubble hid it, copy stripped it, the title ignored it, reuse
    re-parsed it — two parsers, one per language, kept in step by tests, all
    re-deriving from prose a fact the record already held as a list.
    """

    UPLOADS = "/state/uploads"
    GUIDANCE = "[image attached: cat.png — you can see it; file at /state/uploads/cat.png]"

    def test_a_held_file_is_stored_by_name_alone(self):
        uploads = Path(self.UPLOADS)
        assert attachment_embed("/state/uploads/cat.png", held=True) == "![[cat.png]]"
        assert to_record_form(f"look\n{self.GUIDANCE}", uploads) == "look\n![[cat.png]]"

    def test_a_file_elsewhere_keeps_its_path(self):
        # Nothing could derive /tmp back from "shot.png", so the path stays.
        assert attachment_embed("/tmp/shot.png", held=False) == "![[/tmp/shot.png]]"
        assert to_record_form(
            "look\n[attached file: /tmp/shot.png]", Path(self.UPLOADS)
        ) == "look\n![[/tmp/shot.png]]"

    def test_converting_to_the_record_form_is_idempotent(self):
        uploads = Path(self.UPLOADS)
        once = to_record_form(f"look\n{self.GUIDANCE}", uploads)
        assert to_record_form(once, uploads) == once

    def test_a_bare_name_resolves_against_the_uploads_folder(self):
        assert resolve_attachment("cat.png", Path(self.UPLOADS)) == "/state/uploads/cat.png"
        assert resolve_attachment("/tmp/x.png", Path(self.UPLOADS)) == "/tmp/x.png"
        # No folder known: a name resolves to itself. Wrong as a path, but never
        # a crash and never a path pointing somewhere unintended.
        assert resolve_attachment("cat.png", None) == "cat.png"

    def test_a_file_inside_a_sentence_is_the_same_attachment(self, tmp_path):
        """An embed means the same thing wherever it sits (#233) — that is what
        makes it a representation rather than a footer."""
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        (uploads / "shot.png").write_bytes(b"x")
        (uploads / "patch.txt").write_text("x")
        text = "the error in ![[shot.png]], the fix like ![[patch.txt]]"
        assert [n for n, _ in real_attachments(text, uploads)] == [
            "shot.png", "patch.txt",
        ]
        # Stored exactly as written: the position is the information.
        assert to_record_form(text, uploads) == text

    def test_a_file_named_twice_is_delivered_once(self, tmp_path):
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        (uploads / "a.png").write_bytes(b"x")
        text = "compare ![[a.png]] with ![[a.png]] again"
        assert len(real_attachments(text, uploads)) == 1

    def test_a_typed_name_cannot_walk_out_of_the_uploads_folder(self, tmp_path):
        """A reference is INPUT now, so a name is the one thing standing between
        typed text and a directory whose contents are handed over unasked."""
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("x")
        assert resolve_attachment("../secret.txt", uploads) == "../secret.txt"
        assert real_attachments("![[../secret.txt]]", uploads) == []

    def test_looked_and_found_nothing_is_not_the_same_as_nobody_looked(self, tmp_path):
        """The distinction a client draws messages by (#233), and the one I got
        wrong first: `[]` means "these reference nothing" and prose renders as
        prose; `None` means "nobody classified this" — an older server, a mirror
        written before — and the client takes references at face value, which is
        the behaviour from before and never worse. Collapsing the two drew an
        attachment chip over the words of anyone writing about wiki-links."""
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        (uploads / "a.png").write_bytes(b"x")
        assert files_named("just words", uploads) is None
        assert files_named("write ![[note]] inline", uploads) == []
        assert files_named("see ![[a.png]]", uploads) == [
            {"name": "a.png", "path": str(uploads / "a.png")}
        ]

    def test_a_replayed_turn_says_the_same_as_the_live_one(self, tmp_path):
        """The browser draws a bubble from this list, so a chat reopened cold
        has to be handed the same answer a live turn was."""
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        (uploads / "a.png").write_bytes(b"x")
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "see ![[a.png]] and ![[note]]"})
        log.step({"kind": "tool", "name": "read_docs"})
        log.message({"role": "assistant", "content": "ok"})
        log.close()
        user = [e for e in SessionLog.reconstruct_events(log.path) if e["type"] == "user"][0]
        assert user["files"] == [{"name": "a.png", "path": str(uploads / "a.png")}]

    def test_both_forms_read_the_same_way(self):
        record = "look\n![[cat.png]]"
        legacy = f"look\n{self.GUIDANCE}"
        assert strip_attachment_notes(record) == strip_attachment_notes(legacy) == "look"
        assert attachment_names(record) == attachment_names(legacy) == ["cat.png"]

    def test_a_title_can_be_derived_from_an_embed_alone(self, tmp_path):
        # A photo sent with nothing typed is still about something.
        log = SessionLog.new(tmp_path)
        log.message({"role": "user", "content": "![[IMG_4021.jpg]]"})
        log.close()
        assert SessionLog.info(log.path).title == "IMG_4021.jpg"

    def test_words_that_look_like_an_embed_attach_nothing(self, tmp_path):
        """The rule that replaced whole-line matching (#233). An embed can be
        typed now, so what separates "attached a file" from "wrote about the
        notation" is whether the thing named is on disk. `note` is not."""
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        typed = "in Obsidian you write ![[note]] inline to embed something"
        assert real_attachments(typed, uploads) == []
        assert to_record_form(typed, uploads) == typed
        # …and the words survive verbatim wherever the message is preserved.
        assert message_body(typed) == typed

    def test_the_display_derivation_reads_a_reference_as_its_name(self):
        """A chat title must not show notation, and must not have its subject
        cut out either — so `strip_attachment_notes` (titles, previews, search)
        reads an inline reference as the file name. `message_body` is the
        derivation that preserves the message; these two differ on purpose."""
        typed = "the error in ![[shot.png]] is here"
        assert strip_attachment_notes(typed) == "the error in shot.png is here"
        assert message_body(typed) == typed

    def test_the_guidance_says_what_the_model_may_do(self):
        assert "you can see it" in attachment_guidance("c.png", "/u/c.png", "image")
        assert "you can read it" in attachment_guidance("p.pdf", "/u/p.pdf", "document")
        # No kind means the bytes did not go: the model must open the file.
        assert attachment_guidance("x.zip", "/u/x.zip", "") == "[attached file: /u/x.zip]"


class TestRedaction:
    """A chat had no eraser (#202): the log is append-only and replayed whole,
    so a probe fired at the wrong chat, a message sent by an autocorrect Return,
    or a secret pasted into the composer was permanent — the only tools were
    deleting the whole chat or hand-editing JSONL with the server stopped.

    Two properties carry the design, and both are pinned here: the removal is
    REAL (the text leaves the file, so a mirror of it cannot be reconstructed),
    and it leaves a dated tombstone AT THE TURN'S POSITION, so the removal is
    itself auditable and the transcript does not silently lose an exchange.
    """

    def _turns(self, log):
        """The ids of the logged user turns, in order — what a client points at
        when it asks for one to be removed."""
        records = [json.loads(line) for line in log.path.read_text().splitlines()]
        return [
            r["turn"]
            for r in records
            if r.get("kind") == "message" and r.get("role") == "user"
        ]

    def _conversation(self, log):
        """Two turns with a tool step and terminal framing in the second."""
        log.message({"role": "user", "content": "first question"})
        log.step({"kind": "tool", "name": "read_file", "ok": True})
        log.message({"role": "assistant", "content": "first answer"})
        log.task_start("the SECRET token is hunter2")
        log.message({"role": "user", "content": "the SECRET token is hunter2"})
        log.command_event({"kind": "cmd_start", "cwd": "/x", "command": "echo hunter2"})
        log.command_event({"kind": "cmd_end", "status": "exit", "exit_code": 0})
        log.step({"kind": "tool", "name": "run_command", "command": "echo hunter2",
                  "output": "hunter2\n[exit code: 0]", "ok": True})
        log.message({"role": "assistant", "content": "I saw hunter2"})
        log.task_end()
        log.message({"role": "user", "content": "third question"})
        log.message({"role": "assistant", "content": "third answer"})

    def test_the_text_actually_leaves_the_file(self, tmp_path):
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        target = self._turns(log)[1]

        removed = log.redact_turn(target)
        log.close()

        assert removed is not None
        assert removed.text == "the SECRET token is hunter2"
        raw = log.path.read_text(encoding="utf-8")
        assert "hunter2" not in raw, "a hidden turn is not a removed turn"
        assert "I saw hunter2" not in raw
        # The neighbours are untouched — a removal is not a truncation.
        assert "first question" in raw and "third question" in raw

    def test_the_prompt_copy_in_task_start_goes_too(self, tmp_path):
        """task_start is written BEFORE the user message and carries the prompt
        verbatim, so cutting from the message alone would leave a copy of
        exactly what was being removed — and would strand an unmatched
        task_start, which restart recovery reads as a task to resume."""
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        log.redact_turn(self._turns(log)[1])
        log.close()
        assert "hunter2" not in log.path.read_text(encoding="utf-8")
        assert SessionLog.pending_task(log.path) is None

    def test_the_next_turn_keeps_its_own_opening_records(self, tmp_path):
        """The cut ends where the NEXT turn begins, which is its task_start —
        written before its user message, not after. Ending at the message
        instead swallowed the following turn's prompt record."""
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        log.task_start("fourth question")
        log.message({"role": "user", "content": "fourth question"})
        log.message({"role": "assistant", "content": "fourth answer"})
        log.task_end()

        log.redact_turn(self._turns(log)[1])
        log.close()
        starts = [
            r["prompt"] for r in
            (json.loads(line) for line in log.path.read_text().splitlines())
            if r["kind"] == "task_start"
        ]
        assert starts == ["fourth question"], "only the removed turn's start goes"

    def test_a_dated_tombstone_marks_the_gap(self, tmp_path):
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        target = self._turns(log)[1]
        original = [
            json.loads(line) for line in log.path.read_text().splitlines()
            if json.loads(line).get("turn") == target
        ][0]

        log.redact_turn(target)
        log.close()
        records = [json.loads(line) for line in log.path.read_text().splitlines()]
        stones = [r for r in records if r["kind"] == "redact"]
        assert len(stones) == 1
        assert stones[0]["turn"] == target
        assert stones[0]["at"] == original["ts"], "the marker keeps the turn's own time"
        assert stones[0]["records"] > 1
        # AT THE POSITION, not appended: it must close the first turn, not the last.
        kinds = [r["kind"] for r in records]
        assert kinds.index("redact") < len(kinds) - 1

    def test_replay_shows_a_gap_where_the_turn_was(self, tmp_path):
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        log.redact_turn(self._turns(log)[1])
        log.close()

        events = SessionLog.reconstruct_events(log.path)
        types = [ev["type"] for ev in events]
        assert types.count("redacted") == 1
        assert [ev["text"] for ev in events if ev["type"] == "user"] == [
            "first question", "third question",
        ]
        # No orphaned halves: the removed turn's terminal block and its answer
        # are gone with it, and the surviving turns still close properly.
        assert not any("hunter2" in json.dumps(ev) for ev in events)
        assert types.count("done") == 2
        # Undated: the marker renders without a time, so the event carries
        # none — the record's own `at` stays on disk as the audit trail.
        assert events[types.index("redacted")] == {"type": "redacted"}
        # Every surviving turn still names itself, so the control exists on a
        # chat reopened cold — which is most of them.
        assert all(ev.get("turn") for ev in events if ev["type"] == "user")

    def test_it_names_which_identically_worded_turn_it_was(self, tmp_path):
        """The live Agent's messages hold no ids, so the caller finds the same
        turn there by text — and two turns saying 'ok' would otherwise be
        indistinguishable, dropping the wrong one from the model's context."""
        log = SessionLog.new(tmp_path)
        for _ in range(3):
            log.message({"role": "user", "content": "ok"})
            log.message({"role": "assistant", "content": "sure"})
        turns = self._turns(log)
        removed = log.redact_turn(turns[1])
        log.close()
        assert removed.occurrence == 2

    def test_an_unknown_turn_removes_nothing(self, tmp_path):
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        before = log.path.read_text(encoding="utf-8")
        assert log.redact_turn("no-such-turn") is None
        assert log.redact_turn("") is None
        log.close()
        assert log.path.read_text(encoding="utf-8") == before

    def test_removing_the_same_turn_twice_is_a_miss_not_a_second_cut(self, tmp_path):
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        target = self._turns(log)[1]
        assert log.redact_turn(target) is not None
        assert log.redact_turn(target) is None, "already gone is not an error"
        log.close()
        assert "third question" in log.path.read_text(encoding="utf-8")

    def test_the_log_keeps_working_afterwards(self, tmp_path):
        """The rewrite closes the append handle; the next record must reopen it
        and land at the end, not clobber what survived."""
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        log.redact_turn(self._turns(log)[1])
        log.message({"role": "user", "content": "fourth question"})
        log.close()
        assert [m["content"] for m in SessionLog.load_messages(log.path)] == [
            "first question", "first answer", "third question", "third answer",
            "fourth question",
        ]

    def test_a_log_written_before_turn_ids_is_still_removable(self, tmp_path):
        """Ids are minted at write time, so every existing chat has none — and
        those are exactly the chats holding the messages someone wants gone."""
        path = tmp_path / "session-20260101-000000-000000.jsonl"
        path.write_text(
            "\n".join(
                json.dumps({"ts": f"2026-01-01T00:00:0{i}", "kind": "message", **m})
                for i, m in enumerate([
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "leaked secret"},
                    {"role": "assistant", "content": "about that secret"},
                ])
            ) + "\n",
            encoding="utf-8",
        )
        log = SessionLog(path)
        target = [
            ev["turn"]
            for ev in SessionLog.reconstruct_events(path) or []
            if ev.get("type") == "user"
        ]
        # A pre-trace log reconstructs as None, so fall back to the line index
        # ids _turn_id mints — the same names the parse hands out.
        if not target:
            target = ["@2"]
        assert log.redact_turn(target[-1]) is not None
        log.close()
        assert "secret" not in path.read_text(encoding="utf-8")

    def test_an_auto_title_describing_the_removed_turn_is_re_derived(self, tmp_path):
        """An auto title is a description of content; when the content goes, a
        model-written summary of it must not stay on the sessions row — that is
        the leak walking out through the list."""
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        log.set_title("Handling the hunter2 token", auto=True)
        removed = log.redact_turn(self._turns(log)[1])
        log.close()
        assert removed.title == "first question"
        assert SessionLog.info(log.path).title == "first question"
        assert "hunter2" not in log.path.read_text(encoding="utf-8")

    def test_a_hand_typed_title_is_left_alone(self, tmp_path):
        """The same rule the auto-titler already follows: a name you typed is
        yours, and renaming it out from under you is not this feature's call."""
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        log.set_title("Tuesday debugging")
        removed = log.redact_turn(self._turns(log)[1])
        log.close()
        assert removed.title is None
        assert SessionLog.info(log.path).title == "Tuesday debugging"

    def test_a_removal_counts_as_activity(self, tmp_path):
        """Unlike the renderless records #201 took out of the count: every
        device mirrors this transcript and only refetches a session whose
        activity stamp MOVED, so an unmoved stamp would leave the removed text
        in IndexedDB on each of them."""
        log = SessionLog.new(tmp_path)
        self._conversation(log)
        quiet = SessionLog.info(log.path).activity
        time.sleep(1.1)  # ISO stamps are whole seconds
        log.redact_turn(self._turns(log)[1])
        log.close()
        assert SessionLog.info(log.path).activity > quiet


class TestOneBadLogCannotTakeTheAppDown:
    """A log line that PARSES but is not a record.

    Every reader here tolerated a torn line and none of them tolerated this,
    because they all call `.get()` on whatever came back. On 2026-08-20 a single
    reformatted log raised inside `_parse`, which `pager_titles` calls for EVERY
    session, so the socket closed on attach and every client sat on the boot
    spinner with no chat list at all. A bad log must cost its own chat only.
    """

    def _mangled(self, tmp_path):
        path = tmp_path / "session-20260820-000000-000000.jsonl"
        path.write_text(
            json.dumps({"ts": "2026-08-20T00:00:00", "kind": "model", "model": "m"}) + "\n"
            # A bare JSON string on its own line: valid JSON, not a record.
            + '"somewhere quiet to stay with a private pool"\n'
            + "42\n"
            + "[1, 2, 3]\n"
            + "{not json at all\n"
            + json.dumps(
                {"ts": "2026-08-20T00:00:01", "kind": "message", "role": "user",
                 "content": "hello"}
            )
            + "\n"
        )
        return path

    def test_parse_skips_the_bad_lines_and_keeps_the_good_ones(self, tmp_path):
        parsed = SessionLog._parse(self._mangled(tmp_path))
        assert parsed.model == "m"
        assert [m["content"] for m in parsed.messages] == ["hello"]

    def test_the_session_list_survives_one_unreadable_log(self, tmp_path):
        """The actual outage: this is called for every session on attach."""
        self._mangled(tmp_path)
        good = tmp_path / "session-20260820-000001-000000.jsonl"
        good.write_text(
            json.dumps({"ts": "2026-08-20T00:00:02", "kind": "message", "role": "user",
                        "content": "a real chat"}) + "\n"
        )
        titles = SessionLog.pager_titles(tmp_path)
        assert len(titles) == 2, titles

    def test_no_reader_parses_a_log_line_on_its_own(self):
        """Ten call sites, nine with the same bug and one that already had the
        guard — so it is ONE helper, and staying one is the property. A reader
        that grows its own `json.loads` back re-opens the outage silently."""
        source = (Path(__file__).resolve().parents[1] / "aish/session.py").read_text()
        assert source.count("json.loads(") == 1, "a log line is being parsed outside the helper"
        helper = source.split("def _record_or_none")[1].split("\ndef ")[0]
        assert "json.loads(" in helper

    def test_reconstruct_events_survives_it_too(self, tmp_path):
        path = self._mangled(tmp_path)
        # …with a trace record, so this exercises the replay path rather than
        # its documented pre-trace fallback.
        with path.open("a") as fh:
            fh.write(json.dumps({"ts": "2026-08-20T00:00:03", "kind": "trace",
                                 "step": {"kind": "thinking", "secs": 1}}) + "\n")
            fh.write(json.dumps({"ts": "2026-08-20T00:00:04", "kind": "message",
                                 "role": "assistant", "content": "hi back"}) + "\n")
        events = SessionLog.reconstruct_events(path)
        assert events is not None
        assert any(e.get("type") == "user" for e in events), events
