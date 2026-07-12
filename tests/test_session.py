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


def test_load_skips_garbage_lines_and_system(tmp_path):
    path = tmp_path / "session-x.jsonl"
    path.write_text(
        'not json\n'
        '{"kind":"message","role":"system","content":"stale"}\n'
        '{"kind":"message","role":"user","content":"q"}\n'
    )
    assert SessionLog.load_messages(path) == [{"role": "user", "content": "q"}]
