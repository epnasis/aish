"""How each tool call came out, and how big a menu each model call held.

The baseline an upcoming tool-menu shrink is judged against, so every number
here must be what the log RECORDED: a field the writer set, or bytes the
evidence store still holds. A purged menu is never a 0 in the median, and a
day with no recorded menu has no median at all.
"""

from __future__ import annotations

import datetime
import json

from aish import cli, evidence, tooluse


def write_log(tmp_path, records, name="session-20260821-191416-521841.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def trace(step, ts="2026-08-21T19:15:00"):
    return {"ts": ts, "kind": "trace", "step": step}


def tool_step(name, ok=True, **extra):
    return trace({"kind": "tool", "name": name, "ok": ok, "call": 1, **extra})


def brief(digest=None, count=None, ts="2026-08-21T19:15:00"):
    tools = {}
    if digest is not None:
        tools["digest"] = digest
    if count is not None:
        tools["count"] = count
    return trace({"kind": "brief", "model_call": 1, "tools": tools}, ts=ts)


def model_call(number=1, ts="2026-08-21T19:15:00"):
    return trace({"kind": "reasoning", "model_call": number, "tokens": [10, 1]}, ts=ts)


def today_stem(n=1):
    return f"session-{datetime.date.today():%Y%m%d}-000000-{n:06d}.jsonl"


class TestPerToolOutcomes:
    def test_ok_failed_refused_and_unknown_calls_each_land_in_their_own_counter(
        self, tmp_path
    ):
        write_log(tmp_path, [
            tool_step("read_file", ok=True),
            tool_step("read_url", ok=False, status="failed", error="ERROR: 404"),
            tool_step("run_command", ok=False, status="failed", decision="denied",
                      verdict_by="gate"),
            tool_step("frobnicate", ok=False, status="failed",
                      error="ERROR: unknown tool 'frobnicate'"),
        ])
        tools = tooluse.scan(root=tmp_path).tools
        assert (tools["read_file"].calls, tools["read_file"].ok) == (1, 1)
        assert (tools["read_url"].failed, tools["read_url"].refused) == (1, 0)
        assert tools["read_url"].unknown == 0
        assert (tools["run_command"].failed, tools["run_command"].refused) == (1, 1)
        assert (tools["frobnicate"].failed, tools["frobnicate"].unknown) == (1, 1)

    def test_a_refusal_is_counted_within_the_failures_not_beside_them(self, tmp_path):
        write_log(tmp_path, [
            tool_step("run_command", ok=False, decision="held"),
            tool_step("run_command", ok=False, decision="blocked"),
            tool_step("run_command", ok=True, decision="approved"),
        ])
        c = tooluse.scan(root=tmp_path).tools["run_command"]
        assert (c.calls, c.failed, c.refused, c.ok) == (3, 2, 2, 1)
        assert c.refused <= c.failed

    def test_an_error_that_merely_mentions_an_unknown_tool_is_not_counted_as_one(
        self, tmp_path
    ):
        """Read off the recorded field's exact prefix, never searched for."""
        write_log(tmp_path, [
            tool_step("read_url", ok=False, error="ERROR: page said unknown tool 'x'"),
        ])
        assert tooluse.scan(root=tmp_path).tools["read_url"].unknown == 0

    def test_the_counts_sum_across_every_log_in_the_window(self, tmp_path):
        write_log(tmp_path, [tool_step("read_file")], name=today_stem(1))
        write_log(tmp_path, [tool_step("read_file", ok=False)], name=today_stem(2))
        c = tooluse.scan(root=tmp_path).tools["read_file"]
        assert (c.calls, c.failed) == (2, 1)


class TestEmptyAndBrokenLogs:
    def test_a_log_with_no_tool_steps_reads_as_an_empty_report_and_not_an_error(
        self, tmp_path
    ):
        write_log(tmp_path, [{"ts": "2026-08-21T19:15:00", "kind": "message",
                              "role": "user", "content": "hi"}])
        report = tooluse.scan(root=tmp_path)
        assert report.tools == {} and report.menu_days == []
        said = tooluse.render(report, 30)
        assert "no chat in this window recorded a tool call" in said

    def test_a_directory_that_does_not_exist_reads_as_an_empty_report(self, tmp_path):
        report = tooluse.scan(root=tmp_path / "nowhere")
        assert report.tools == {} and report.menu_days == []

    def test_one_unreadable_log_does_not_take_the_others_down_with_it(self, tmp_path):
        write_log(tmp_path, [tool_step("read_file")], name=today_stem(1))
        # A directory where a file should be: reading it raises OSError.
        (tmp_path / today_stem(2)).mkdir()
        (tmp_path / today_stem(3)).write_text("{not json\n\x00\n")
        write_log(tmp_path, [tool_step("read_file")], name=today_stem(4))
        assert tooluse.scan(root=tmp_path).tools["read_file"].calls == 2


class TestTheWindow:
    def test_a_log_named_for_a_day_outside_the_window_is_not_read(self, tmp_path):
        write_log(tmp_path, [tool_step("old_tool")], name="session-20200101-000000-000001.jsonl")
        write_log(tmp_path, [tool_step("new_tool")], name=today_stem())
        assert set(tooluse.scan(root=tmp_path, days=7).tools) == {"new_tool"}
        assert set(tooluse.scan(root=tmp_path, days=None).tools) == {"old_tool", "new_tool"}


class TestMenuSizes:
    def test_a_digest_that_resolves_yields_the_bytes_the_model_was_handed(self, tmp_path):
        digest = evidence.put("m" * 1200, tmp_path)
        write_log(tmp_path, [brief(digest, count=40), model_call(1)])
        (day,) = tooluse.scan(root=tmp_path).menu_days
        assert day.day == "2026-08-21"
        assert (day.calls, day.recorded, day.purged, day.missing) == (1, 1, 0, 0)
        assert (day.median_chars, day.max_chars, day.median_tools) == (1200, 1200, 40)

    def test_every_model_call_holds_the_latest_brief_before_it(self, tmp_path):
        """A brief is written only when the menu CHANGES, so three calls under
        one brief are three calls handed that menu — not one."""
        small = evidence.put("s" * 100, tmp_path)
        large = evidence.put("L" * 900, tmp_path)
        write_log(tmp_path, [
            brief(small), model_call(1), model_call(2), model_call(3),
            brief(large), model_call(4),
        ])
        (day,) = tooluse.scan(root=tmp_path).menu_days
        assert (day.calls, day.recorded) == (4, 4)
        assert (day.median_chars, day.max_chars) == (100, 900)

    def test_a_purged_menu_is_its_own_column_and_never_a_zero_in_the_median(self, tmp_path):
        kept = evidence.put("k" * 500, tmp_path)
        gone = evidence.put("g" * 50, tmp_path)
        assert evidence.purge(gone, tmp_path)
        write_log(tmp_path, [
            brief(kept), model_call(1),
            brief(gone, count=12), model_call(2), model_call(3),
        ])
        (day,) = tooluse.scan(root=tmp_path).menu_days
        assert (day.calls, day.recorded, day.purged) == (3, 1, 2)
        assert day.median_chars == 500  # a 0 for each purged call would make this 0
        assert day.median_tools == 12  # the count lives in the log and survives the purge

    def test_a_brief_with_no_digest_and_a_call_with_no_brief_count_as_missing(self, tmp_path):
        write_log(tmp_path, [model_call(1), brief(), model_call(2)])
        (day,) = tooluse.scan(root=tmp_path).menu_days
        assert (day.calls, day.recorded, day.purged, day.missing) == (2, 0, 0, 2)
        assert day.median_chars is None and day.max_chars is None

    def test_a_day_with_no_recorded_menu_has_no_median_in_the_json_rather_than_zero(
        self, tmp_path
    ):
        write_log(tmp_path, [model_call(1)])
        (day,) = tooluse.json_report(tooluse.scan(root=tmp_path), None)["menu_days"]
        assert day["missing"] == 1
        assert "median_chars" not in day and "max_chars" not in day

    def test_calls_are_grouped_by_the_day_their_record_was_written(self, tmp_path):
        digest = evidence.put("x" * 10, tmp_path)
        write_log(tmp_path, [
            brief(digest), model_call(1, ts="2026-08-21T23:59:00"),
            model_call(2, ts="2026-08-22T00:01:00"),
        ])
        days = [d.day for d in tooluse.scan(root=tmp_path).menu_days]
        assert days == ["2026-08-21", "2026-08-22"]

    def test_the_menu_bytes_are_looked_up_once_per_digest(self, tmp_path, monkeypatch):
        """One menu serves hundreds of calls; reading its blob per call would
        re-read and re-hash ~31 KB each time."""
        digest = evidence.put("y" * 10, tmp_path)
        write_log(tmp_path, [brief(digest)] + [model_call(n) for n in range(1, 6)])
        lookups = []
        real_get = evidence.get
        monkeypatch.setattr(
            evidence, "get", lambda d, root: lookups.append(d) or real_get(d, root)
        )
        tooluse.scan(root=tmp_path)
        assert lookups == [digest]


class TestTheReport:
    def test_the_render_names_the_window_and_what_it_cannot_know(self, tmp_path):
        digest = evidence.put("z" * 300, tmp_path)
        write_log(tmp_path, [
            brief(digest, count=7), model_call(1),
            tool_step("read_file"), tool_step("read_file"), tool_step("web_search"),
        ])
        said = tooluse.render(tooluse.scan(root=tmp_path), 30)
        assert "the last 30 days" in said
        assert said.index("read_file") < said.index("web_search")  # most calls first
        assert "2026-08-21" in said and "300" in said
        assert "right one" in said

    def test_the_json_report_is_plain_data(self, tmp_path):
        write_log(tmp_path, [tool_step("read_file", ok=False, decision="denied")])
        report = tooluse.json_report(tooluse.scan(root=tmp_path), 30)
        assert json.loads(json.dumps(report))["tools"]["read_file"] == {
            "calls": 1, "ok": 0, "failed": 1, "refused": 1, "unknown": 0,
        }


class TestTheSubcommand:
    def test_the_subcommand_rejects_an_argument_it_does_not_understand(self, capsys):
        assert cli._tooluse_cli(["--nonsense"]) == 2
        assert "usage: aish tooluse" in capsys.readouterr().out

    def test_a_non_numeric_day_count_is_rejected(self, capsys):
        assert cli._tooluse_cli(["--days", "week"]) == 2
        assert "usage: aish tooluse" in capsys.readouterr().out

    def test_json_output_parses(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        write_log(tmp_path, [tool_step("read_file")], name=today_stem())
        assert cli._tooluse_cli(["--json", "--days", "7"]) == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["days"] == 7
        assert parsed["tools"]["read_file"]["calls"] == 1

    def test_main_dispatches_the_subcommand(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["aish", "tooluse", "--json"])
        assert cli.main() == 0
        assert json.loads(capsys.readouterr().out)["days"] == 30

    def test_the_subcommand_runs_over_a_directory_with_no_logs(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        assert cli._tooluse_cli(["--all"]) == 0
        assert "tool use" in capsys.readouterr().out


class TestOneSpellingOfUnknown:
    def test_the_writer_imports_the_prefix_rather_than_respelling_it(self):
        """The reader counts `unknown` off the recorded error field, so a
        reworded copy in the writer would silently zero the counter for every
        new log. One spelling, owned by tools.py: agent.py must build the
        result from the constant and carry no literal of its own."""
        from pathlib import Path

        from aish import tools

        assert tooluse.UNKNOWN_TOOL_PREFIX is tools.UNKNOWN_TOOL_PREFIX
        source = (Path(__file__).resolve().parent.parent / "aish" / "agent.py").read_text()
        assert "UNKNOWN_TOOL_PREFIX" in source
        assert "unknown tool '" not in source
