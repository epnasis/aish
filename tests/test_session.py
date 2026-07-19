import json

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
    # the model) must not write — only real activity reorders the lists.
    log = SessionLog.new(tmp_path)
    log.model("qwen3:8b")
    assert log.path.read_text() == ""
    log.message({"role": "user", "content": "hi"})  # note flushes with activity
    kinds = [json.loads(line)["kind"] for line in log.path.read_text().splitlines()]
    assert kinds == ["model", "message"]


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
