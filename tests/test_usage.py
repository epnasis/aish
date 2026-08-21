"""What was spent, and what filled the context that made it cost that (#262).

A pure scan over recorded evidence — no model, no network, no live state. The
incident behind it: 12.7M input tokens in 40 minutes exhausted a quota, and
nothing in any surface could say what had filled the context. Establishing that
browser-rendered pages were the cause took an ad-hoc script over raw JSONL.
"""

from __future__ import annotations

import json

from aish import backends, usage


def write_log(tmp_path, records, name="session-20260821-191416-521841.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def call_record(number, tokens, ts="2026-08-21T19:15:00", detail=None):
    step = {"kind": "reasoning", "model_call": number, "tokens": list(tokens)}
    if detail:
        step["usage"] = detail
    return {"ts": ts, "kind": "trace", "step": step}


def message(role, content, model_call=None, tool_name=None):
    record = {"ts": "2026-08-21T19:15:00", "kind": "message", "role": role, "content": content}
    if model_call is not None:
        record["model_call"] = model_call
    if tool_name:
        record["tool_name"] = tool_name
    return record


class TestSpendTotals:
    def test_spend_sums_every_call(self, tmp_path):
        path = write_log(tmp_path, [
            {"ts": "2026-08-21T19:14:19", "kind": "model", "model": "gemini:gemini-3.5-flash"},
            call_record(1, (100, 10)),
            call_record(2, (200, 20)),
        ])
        session = usage.scan_session(path)
        assert session.spend == 330
        assert session.provider == "gemini"
        assert session.model == "gemini-3.5-flash"

    def test_context_peak_is_not_the_spend_sum(self, tmp_path):
        """Summing prompt tokens across calls double-counts the resent history,
        so spend reads far bigger than the context ever was. Reporting one as
        the other would make every figure wrong in the same direction."""
        path = write_log(tmp_path, [call_record(1, (100, 0)), call_record(2, (150, 0))])
        session = usage.scan_session(path)
        assert session.spend == 250
        assert session.peak_context == 150

    def test_the_usage_detail_is_authoritative_over_the_two_ints(self, tmp_path):
        """`tokens[0]` means three different things across the three backends;
        only the detail's `semantics` says which."""
        detail = backends.usage_detail(
            backends.INPUT_INCLUDES_CACHE, input=129_623, cached=100_000, output=250
        )
        path = write_log(tmp_path, [call_record(1, (0, 0), detail=detail)])
        (call,) = usage.scan_session(path).calls
        assert (call.input, call.output, call.cached) == (129_623, 250, 100_000)
        assert call.semantics == backends.INPUT_INCLUDES_CACHE

    def test_an_older_log_without_the_split_still_counts(self, tmp_path):
        path = write_log(tmp_path, [call_record(1, (500, 40))])
        (call,) = usage.scan_session(path).calls
        assert (call.input, call.output) == (500, 40)
        assert call.semantics == ""  # …and says the units are unstated

    def test_grouping_by_day_and_week(self, tmp_path):
        path = write_log(tmp_path, [
            call_record(1, (10, 1), ts="2026-08-17T10:00:00"),
            call_record(2, (20, 2), ts="2026-08-21T10:00:00"),
        ])
        calls = usage.scan_session(path).calls
        assert sorted(usage.by_day(calls)) == ["2026-08-17", "2026-08-21"]
        # Both fall in the week beginning Monday 2026-08-17.
        assert list(usage.by_week(calls)) == ["w/c 2026-08-17"]


class TestContextAttribution:
    """The owner's actual question: what drove the cost, not just the total."""

    def _session(self, tmp_path):
        return usage.scan_session(write_log(tmp_path, [
            {"ts": "2026-08-21T19:14:19", "kind": "model", "model": "gemini:gemini-3.5-flash"},
            message("user", "find flights", model_call=0),
            call_record(1, (1_000, 10)),
            message("tool", "x" * 30_000, model_call=1, tool_name="read_url"),
            call_record(2, (11_000, 10)),
            message("tool", "y" * 300, model_call=2, tool_name="web_search"),
            call_record(3, (11_200, 10)),
            call_record(4, (11_300, 10)),
        ]))

    def test_contributors_are_bucketed_by_the_tool_that_produced_them(self, tmp_path):
        origins = {c.origin: c for c in self._session(tmp_path).contributors}
        assert origins["read_url"].chars == 30_000
        assert origins["read_url"].items == 1
        assert origins["web_search"].chars == 300
        assert "user" in origins

    def test_the_biggest_contributor_leads(self, tmp_path):
        assert self._session(tmp_path).contributors[0].origin == "read_url"

    def test_residency_counts_only_the_calls_it_was_actually_present_for(self, tmp_path):
        """A result read at call 1 is resent on calls 2..N — not on call 1, and
        not before it existed."""
        origins = {c.origin: c for c in self._session(tmp_path).contributors}
        # Entered after call 1, present for calls 2, 3 and 4.
        assert origins["read_url"].char_calls == 30_000 * 3
        # Entered after call 2, present for calls 3 and 4.
        assert origins["web_search"].char_calls == 300 * 2

    def test_a_trim_ends_residency(self, tmp_path):
        """Naive injection-to-end overstates precisely the results the system
        already handled well."""
        path = write_log(tmp_path, [
            message("tool", "x" * 1_000, model_call=0, tool_name="read_url"),
            call_record(1, (100, 1)),
            call_record(2, (100, 1)),
            {"ts": "2026-08-21T19:16:00", "kind": "trace", "step": {
                "kind": "trim", "affected": 1,
                "stubbed": [{"at": 1, "tool": "read_url", "continuation": "abc"}]}},
            call_record(3, (100, 1)),
            call_record(4, (100, 1)),
        ])
        (item,) = [c for c in usage.scan_session(path).contributors if c.origin == "read_url"]
        assert item.trimmed == 1
        assert item.char_calls == 1_000 * 2  # calls 1 and 2 only


class TestHonestAboutWhatItCannotSay:
    """A report that omits what the number is NOT is confidently wrong in a
    direction the reader cannot see."""

    def test_a_log_without_call_stamps_cannot_compute_residency(self, tmp_path):
        """Without the stamp, residency degrades to chars x every call in the
        session — a number that LOOKS like attribution and is arithmetic on an
        assumption."""
        path = write_log(tmp_path, [
            message("tool", "x" * 1_000, tool_name="read_url"),  # no model_call
            call_record(1, (100, 1)),
            call_record(2, (100, 1)),
        ])
        session = usage.scan_session(path)
        assert session.stamped is False
        assert all(c.char_calls == 0 for c in session.contributors)
        rendered = usage.render_session(session)
        assert "cannot be computed" in rendered
        assert "resident" not in rendered.split("what filled the context")[1].split("\n")[1]

    def test_json_omits_residency_rather_than_reporting_zero(self, tmp_path):
        """A consumer must not read "not measurable" as "nothing was resident"."""
        path = write_log(tmp_path, [
            message("tool", "x" * 10, tool_name="read_url"), call_record(1, (10, 1)),
        ])
        payload = json.loads(usage.json_report([usage.scan_session(path)]))
        contributor = payload["sessions"][0]["contributors"][0]
        assert payload["sessions"][0]["residency_recorded"] is False
        assert "char_calls" not in contributor

    def test_a_backend_that_reports_no_usage_is_not_recorded_not_zero(self, tmp_path):
        """claude-max drives the SDK's own loop and records no input tokens. A
        day of it reading "0 tokens" would be a confident lie."""
        path = write_log(tmp_path, [
            {"ts": "2026-08-21T19:14:19", "kind": "model", "model": "claude-max"},
            {"ts": "2026-08-21T19:15:00", "kind": "trace", "step": {
                "kind": "model_error", "class": "transport", "attempt": 1,
                "attempts": 3, "action": "give_up"}},
        ])
        session = usage.scan_session(path)
        assert session.saw_model_calls and not session.calls
        assert session.recorded is False
        assert usage.NOT_RECORDED in usage.render_session(session)

    def test_a_session_with_no_model_calls_is_not_flagged_as_unrecorded(self, tmp_path):
        """Nothing happened is a different fact from nothing was recorded."""
        path = write_log(tmp_path, [message("user", "hi", model_call=0)])
        assert usage.scan_session(path).recorded is True

    def test_a_rewritten_log_says_it_undercounts(self, tmp_path):
        """Retry rewinds and redaction deletes — those calls were billed and
        their records are gone."""
        path = write_log(tmp_path, [
            call_record(1, (10, 1)),
            {"ts": "2026-08-21T19:16:00", "kind": "redaction", "turn": "abc"},
        ])
        session = usage.scan_session(path)
        assert session.rewritten
        assert "billed" in usage.render([session])

    def test_the_summary_never_claims_to_be_a_bill(self, tmp_path):
        path = write_log(tmp_path, [call_record(1, (10, 1))])
        rendered = usage.render([usage.scan_session(path)])
        assert "not a billing statement" in rendered
        assert "not counted" in rendered  # retitles, curate

    def test_failed_calls_are_surfaced(self, tmp_path):
        path = write_log(tmp_path, [
            call_record(1, (10, 1)),
            {"ts": "2026-08-21T19:16:00", "kind": "trace", "step": {
                "kind": "model_error", "class": "rate_limit", "status": 429,
                "attempt": 1, "attempts": 3, "action": "retry"}},
        ])
        assert "failed" in usage.render([usage.scan_session(path)])


class TestScanIsContained:
    def test_one_unreadable_log_does_not_take_the_report_down(self, tmp_path):
        """A single corrupt session log once took the whole web server down;
        containment is per file."""
        write_log(tmp_path, [call_record(1, (10, 1))], name="session-20260821-000000-000001.jsonl")
        (tmp_path / "session-20260821-000000-000002.jsonl").write_text("{not json\n\x00\n")
        sessions = usage.scan(tmp_path)
        assert len(sessions) == 2
        assert sum(s.spend for s in sessions) == 11

    def test_a_missing_state_dir_is_empty_not_an_error(self, tmp_path):
        assert usage.scan(tmp_path / "nope") == []

    def test_days_filter_reads_the_date_from_the_name(self, tmp_path):
        """Skipping a whole file without opening it is what keeps `--days 7`
        from parsing a year of logs to discard them."""
        assert usage._within("session-20260821-191416-521841", 100000) is True
        assert usage._within("session-19990101-000000-000000", 7) is False
        # Unnameable: included rather than silently dropped.
        assert usage._within("session-weird", 7) is True

    def test_no_model_and_no_live_state_can_reach_the_scanner(self):
        """Same law as `aish explain`: a report that could consult a live model
        or today's config would stop being a reading of what happened."""
        source = (usage.__file__ and open(usage.__file__).read()) or ""
        for forbidden in ("import ollama", "backends.make_chat", "requests", "urllib"):
            assert forbidden not in source
