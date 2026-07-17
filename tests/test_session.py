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
