import json
import os

from aish.session import SessionLog


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
    # pager_titles (cold path) reflects the custom title too.
    assert SessionLog.pager_titles(tmp_path) == [(log.path.name, "My Renamed Chat")]


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
    messages, _, custom_title = SessionLog._parse(log.path)
    assert custom_title == "Renamed"
    assert all(m.get("role") in ("user", "assistant") for m in messages)


def test_empty_title_ignored(tmp_path):
    # A blank/whitespace title record does not shadow the derived title.
    log = SessionLog.new(tmp_path)
    log.message({"role": "user", "content": "real title"})
    log.set_title("   ")
    assert SessionLog.info(log.path).title == "real title"
    _, _, custom_title = SessionLog._parse(log.path)
    assert custom_title is None


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
    assert [name for name, _ in pages] == [old.name]


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
