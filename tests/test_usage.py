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


class TestWhatFilledEachCall:
    """The per-turn contributor breakdown inside `aish explain` (#262/#330).

    Per-model-call SPEND was already recorded; the ATTRIBUTION was not. These
    pin what the reader may say from the records and — more importantly — what
    it must refuse to say.
    """

    def _doc(self, tmp_path, records, ordinal=0):
        from aish import explain

        path = write_log(tmp_path, records)
        log = explain.load(path)
        return explain.dossier(log.turns[ordinal], log, tmp_path)["context_cost"]

    @staticmethod
    def _brief(model_call=1, system_chars=1000, digest="", turn=1):
        step = {
            "kind": "brief", "model_call": model_call, "turn": turn,
            "system": [{"at": 0, "chars": system_chars, "digest": "d0"}],
            "tools": {"digest": digest, "count": 3},
            "options": {"provider": "gemini", "model": "g"},
        }
        return {"ts": "2026-08-21T19:14:20", "kind": "trace", "step": step}

    def test_a_result_is_charged_to_every_call_after_the_one_that_fetched_it(self, tmp_path):
        """The stamp says which call a message was first in front of. A tool
        result fetched during call 1 is resent on calls 2, 3, 4… — that is the
        whole reason a loop's context grows, and the reader must show it
        growing rather than showing one flat total."""
        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(),
            message("user", "hi", model_call=0),
            call_record(1, (100, 5)),
            message("tool", "x" * 900, model_call=1, tool_name="read_url"),
            call_record(2, (400, 5)),
            call_record(3, (400, 5)),
        ])
        assert context["stamped"] is True
        sizes = [c["accounted_chars"] for c in context["calls"]]
        # call 1 has the prompt only; calls 2 and 3 both carry the 900-char read.
        assert sizes[1] - sizes[0] == 900
        assert sizes[2] == sizes[1]
        assert context["calls"][1]["added_by"][0] == {
            "origin": "read_url", "where": "turn", "chars": 900
        }

    def test_history_from_earlier_turns_is_counted_and_named_as_such(self, tmp_path):
        """It is resent on every call of this turn and is invisible in a
        per-turn view, which is what made it the biggest resident in the corpus
        and nobody's problem."""
        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "one", "ts": "2026-08-21T19:14:19"},
            self._brief(),
            message("user", "one", model_call=0),
            call_record(1, (100, 5)),
            message("tool", "y" * 500, model_call=1, tool_name="web_search"),
            {"kind": "task_start", "prompt": "two", "ts": "2026-08-21T19:20:19"},
            message("user", "two", model_call=0),
            call_record(1, (300, 5)),
        ], ordinal=1)
        parts = {(p["where"], p["origin"]): p["chars"] for p in context["peak"]["parts"]}
        assert parts[("carried", "web_search")] == 500
        assert parts[("carried", "user")] == 3
        assert parts[("turn", "user")] == 3

    def test_an_unstamped_log_reports_nothing_rather_than_a_positional_guess(self, tmp_path):
        """Without the stamp, membership in a call's context is inferred from
        the order lines happen to sit in the file. A breakdown built on that
        looks like attribution and is arithmetic on an assumption."""
        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(),
            message("user", "hi"),
            call_record(1, (100, 5)),
            message("tool", "z" * 900, tool_name="read_url"),
        ])
        assert context["stamped"] is False
        assert context["calls"] == []
        assert context["peak"] is None

    def test_an_unstamped_turn_inside_a_stamped_log_still_reports_nothing(self, tmp_path):
        """One long chat spans an upgrade: its early turns carry no stamp and
        its later ones do. A file-level answer would read every unstamped
        message as `model_call: 0` — present from the first call — and quietly
        attribute a result to calls that never saw it."""
        records = [
            {"kind": "task_start", "prompt": "old", "ts": "2026-08-21T19:14:19"},
            self._brief(),
            message("user", "old"),
            call_record(1, (100, 5)),
            message("tool", "z" * 900, tool_name="read_url"),
            call_record(2, (400, 5)),
            {"kind": "task_start", "prompt": "new", "ts": "2026-08-21T19:30:19"},
            message("user", "new", model_call=0),
            call_record(1, (500, 5)),
        ]
        assert self._doc(tmp_path, records, ordinal=0)["stamped"] is False
        later = self._doc(tmp_path, records, ordinal=1)
        assert later["stamped"] is True
        # …and the unstamped turn's messages are still counted as CARRIED, which
        # needs no stamp: everything written before this turn was in front of
        # every call of it.
        parts = {(p["where"], p["origin"]): p["chars"] for p in later["peak"]["parts"]}
        assert parts[("carried", "read_url")] == 900

    def test_a_turn_with_no_brief_says_the_system_text_is_unmeasured(self, tmp_path):
        """Zero would be a confident falsehood at the top of the breakdown: a
        turn with no system text has never happened."""
        from aish import explain

        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            message("user", "hi", model_call=0),
            call_record(1, (100, 5)),
        ])
        system = [p for p in context["peak"]["parts"] if p["origin"] == "system text"][0]
        assert system["state"] == explain.MISSING
        assert system["chars"] == 0
        assert "the system text" in context["peak"]["unmeasured"]
        # …and with a part unmeasured, no chars-per-token ratio is offered: it
        # would read as a property of the content rather than of the gap.
        assert context["calls"][0]["chars_per_token"] is None

    def test_a_purged_tool_menu_is_purged_and_never_zero(self, tmp_path):
        from aish import explain

        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(digest="deadbeef"),
            message("user", "hi", model_call=0),
            call_record(1, (100, 5)),
        ])
        menu = [p for p in context["peak"]["parts"] if p["origin"] == "tool menu"][0]
        assert menu["state"] == explain.PURGED
        assert "purged" in " ".join(context["peak"]["unmeasured"])

    def test_the_menu_is_sized_from_the_evidence_store(self, tmp_path):
        from aish import evidence, explain

        digest = evidence.put("[" + "t" * 200 + "]", tmp_path)
        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(digest=digest, system_chars=100),
            message("user", "hi", model_call=0),
            call_record(1, (100, 5)),
        ])
        menu = [p for p in context["peak"]["parts"] if p["origin"] == "tool menu"][0]
        assert menu["state"] == explain.RECORDED
        assert menu["chars"] == 202
        # The fixed floor — system text plus menu — against everything measured.
        assert context["peak"]["fixed_share"] > 0.9

    def test_a_call_whose_backend_reported_nothing_shows_no_numbers(self, tmp_path):
        """claude-max drives its own loop and reports no input tokens. Reading
        that as 0 would be a confident lie about a day of real spend."""
        from aish import explain

        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(),
            message("user", "hi", model_call=0),
            {"ts": "2026-08-21T19:15:00", "kind": "trace",
             "step": {"kind": "reasoning", "model_call": 1}},
        ])
        assert context["calls"][0]["reported"] == {}
        assert context["calls"][0]["reported_state"] == explain.MISSING
        assert context["calls"][0]["chars_per_token"] is None

    def test_the_units_never_mix(self, tmp_path):
        """Parts carry MEASURED chars; the call carries the provider's REPORTED
        tokens. A per-part token figure would be a modelled number in the
        provider's own unit, inviting a reader to sum the parts and contradict
        a number the provider actually reported."""
        from aish import evidence

        digest = evidence.put("[]", tmp_path)
        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(digest=digest),
            message("user", "hi", model_call=0),
            call_record(1, (100, 5), detail={"input": 100, "output": 5,
                                             "semantics": "input_includes_cache"}),
        ])
        for part in context["peak"]["parts"]:
            assert set(part) == {"origin", "where", "chars", "items", "trimmed", "state",
                                 "share"}
        assert context["peak"]["reported"]["semantics"] == "input_includes_cache"
        # The one bridge, and both halves of it are recorded numbers.
        assert context["calls"][0]["chars_per_token"] == round(
            context["calls"][0]["accounted_chars"] / 100, 2
        )

    def test_a_trim_that_named_nothing_is_reported_and_not_attributed(self, tmp_path):
        """A trim written before `stubbed[]` existed says how much text went and
        not what it came from. Folding that into a bucket would make a guess
        look like a measurement."""
        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(),
            message("user", "hi", model_call=0),
            message("tool", "q" * 5000, model_call=0, tool_name="read_url"),
            {"ts": "2026-08-21T19:14:21", "kind": "trace", "step": {
                "kind": "trim", "policy": "eager_stub", "affected": 1,
                "bytes_before": 6000, "bytes_after": 1200, "keep_chars": 200}},
            call_record(1, (100, 5)),
        ])
        assert context["unattributed_chars"] == 4800
        parts = {p["origin"]: p["chars"] for p in context["peak"]["parts"]}
        assert parts["read_url"] == 5000  # untouched: nothing said it was this one

    def test_a_trim_never_cuts_more_than_it_recorded_cutting(self, tmp_path):
        """`delivered_images` replaces a short note with a shorter constant and
        leaves a picture behind, so its `stubbed[]` names a message whose text
        barely moved. Applying `keep_chars` per stub without the recorded total
        as a ceiling threw the reconstruction out by 31% on a real log."""
        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(),
            message("user", "x" * 4000, model_call=0),
            {"ts": "2026-08-21T19:14:21", "kind": "trace", "step": {
                "kind": "trim", "policy": "delivered_images", "affected": 1,
                "stubbed": [{"at": 1, "tool": "user"}],
                "bytes_before": 5000, "bytes_after": 4870, "keep_chars": 200}},
            call_record(1, (100, 5)),
        ])
        parts = {p["origin"]: p["chars"] for p in context["peak"]["parts"]}
        assert parts["user"] == 4000 - 130
        assert context["unattributed_chars"] == 0

    def test_the_reconstruction_matches_a_total_the_agent_measured_itself(self, tmp_path):
        """The decisive check, and the reason this is a reader and not a guess:
        `model_error.sent_chars` is `sum(len(content))` over the live message
        list at the moment of the call, recorded by the agent and by nothing
        this scan does. The reconstruction has to land on it."""
        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(system_chars=1000),
            message("user", "hi", model_call=0),
            call_record(1, (100, 5)),
            message("assistant", "a" * 50, model_call=1),
            message("tool", "b" * 700, model_call=1, tool_name="read_url"),
            call_record(2, (300, 5)),
            {"ts": "2026-08-21T19:16:00", "kind": "trace", "step": {
                "kind": "model_error", "model_call": 3, "sent_chars": 1752,
                "sent_messages": 4, "action": "retry", "text": "boom"}},
        ])
        # 1000 system + 2 prompt + 50 assistant + 700 tool = 1752, the number
        # the agent wrote down. The menu is NOT in it: it travels beside the
        # messages, which is why it is a part of its own here.
        by_call = {c["model_call"]: c for c in context["calls"]}
        assert by_call[2]["accounted_chars"] == 1752
        assert context["failed"][0]["sent_chars"] == 1752

    def test_a_role_reports_what_it_was_given_and_what_it_answered(self, tmp_path):
        """And NOT "what it returned to the acting model" — the block its answer
        is rendered into is what enters the acting context, and nothing records
        that size. Naming it wrongly would make an unjoined guess read as the
        measurement #330 asks for."""
        from aish import explain

        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(),
            message("user", "hi", model_call=0),
            call_record(1, (100, 5)),
            {"ts": "2026-08-21T19:15:10", "kind": "trace", "step": {
                "kind": "role", "charter": "snippet-reader", "turn": 1, "call": 1,
                "model": "gemini:g", "status": "ok",
                "input": {"name": "results", "chars": 2129},
                "usage": {"input": 2513, "output": 427},
                "output": [{"n": 1, "about": "a page"}]}},
        ])
        role = context["roles"][0]
        assert role["input_chars"] == 2129
        assert role["answer_state"] == explain.RECORDED
        assert role["answer_chars"] == len('[{"n": 1, "about": "a page"}]')
        assert role["reported"] == {"input": 2513, "output": 427}

    def test_steering_is_sized_but_never_placed(self, tmp_path):
        """`_inject_pending_messages` appends the text straight to
        `self.messages`, so it writes no `message` record. Folding it in was
        tried and made every anchor in the owner's corpus WORSE — the text was
        not in front of those calls, because a restart rebuilds `self.messages`
        from the log and the log is where this text is not. The record cannot
        say which happened, so the size is stated and not placed."""
        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(),
            message("user", "hi", model_call=0),
            call_record(1, (100, 5)),
            {"ts": "2026-08-21T19:15:30", "kind": "trace",
             "step": {"kind": "injected", "text": "x" * 40}},
            call_record(2, (110, 5)),
        ])
        assert context["steering_chars"] == 40
        # …and it moved no bucket and no total.
        assert all(p["origin"] != "steering" for p in context["peak"]["parts"])
        assert context["calls"][1]["accounted_chars"] == context["calls"][0]["accounted_chars"]

    def test_the_total_is_said_to_be_a_lower_bound(self, tmp_path):
        """Three things reach `self.messages` with no message record: steering,
        a held proposal's answer, and the guidance form of an attachment. On
        every valid anchor in the owner's corpus the reader is SHORT by 0.0-2.1%
        because of them, so the block must not read as exact."""
        from aish import evidence, explain

        digest = evidence.put("[]", tmp_path)
        path = write_log(tmp_path, [
            {"kind": "task_start", "prompt": "hi", "ts": "2026-08-21T19:14:19"},
            self._brief(digest=digest),
            message("user", "hi", model_call=0),
            call_record(1, (100, 5)),
        ])
        log = explain.load(path)
        text = explain.render(log.turns[0], log, tmp_path)
        assert "LOWER bound" in text
        assert "steering, a held proposal's answer" in text

    def test_images_get_a_row_because_they_carry_no_characters(self, tmp_path):
        """Char-invisible and token-huge. A row makes an unaccounted call show a
        visible reason instead of a silent gap."""
        from aish import explain

        record = message("user", "look", model_call=0)
        record["images"] = ["/tmp/a.png", "/tmp/b.png"]
        context = self._doc(tmp_path, [
            {"kind": "task_start", "prompt": "look", "ts": "2026-08-21T19:14:19"},
            self._brief(),
            record,
            call_record(1, (100, 5)),
        ])
        row = [p for p in context["peak"]["parts"] if "image" in p["origin"]][0]
        assert row["state"] == explain.UNREADABLE
        assert row["chars"] == 0
        assert context["calls"][0]["chars_per_token"] is None


class TestTheTrendLivesInOnePlace:
    """#330 asks for statistics over time. `aish usage` already scans across
    sessions and groups by period; `aish explain` reads one turn out of one
    file. Building it in both would give two places to read the same fact, and
    they would disagree the first time one changed."""

    def test_calls_over_the_window_are_counted_per_period(self, tmp_path):
        path = write_log(tmp_path, [
            {"ts": "2026-08-21T19:14:19", "kind": "model", "model": "gemini:g"},
            call_record(1, (10_000, 5)),
            call_record(2, (90_000, 5)),
        ])
        text = usage.render([usage.scan_session(path)], "day", window=60_000)
        assert "against a 60,000-token window" in text
        assert "1 (50%)" in text

    def test_the_window_is_a_default_and_not_a_fact_about_the_backend(self, tmp_path):
        path = write_log(tmp_path, [call_record(1, (30_000, 5))])
        session = usage.scan_session(path)
        assert "0 (0%)" in usage.render([session], "day", window=60_000)
        assert "1 (100%)" in usage.render([session], "day", window=20_000)

    def test_the_fixed_floor_is_measured_where_recorded_and_absent_where_not(self, tmp_path):
        from aish import evidence, explain

        digest = evidence.put("x" * 500, tmp_path)
        path = write_log(tmp_path, [
            {"ts": "2026-08-21T19:14:19", "kind": "model", "model": "gemini:g"},
            {"ts": "2026-08-21T19:14:20", "kind": "trace", "step": {
                "kind": "brief", "model_call": 1, "turn": 1,
                "system": [{"at": 0, "chars": 900, "digest": "d"}],
                "tools": {"digest": digest, "count": 7}, "options": {}}},
            call_record(1, (100, 5)),
        ])
        session = usage.scan_session(path)
        assert session.system_chars == 900
        assert session.menu_chars == 500
        assert session.fixed_measured is True
        report = json.loads(usage.json_report([session]))["sessions"][0]
        assert report["fixed_floor_state"] == explain.RECORDED
        assert report["menu_chars"] == 500

        bare = usage.scan_session(write_log(tmp_path, [call_record(1, (100, 5))],
                                            name="session-20260821-191417-000000.jsonl"))
        assert bare.fixed_measured is False
        naked = json.loads(usage.json_report([bare]))["sessions"][0]
        # Absent, never 0: a consumer must not read "never recorded" as "free".
        assert "system_chars" not in naked and "menu_chars" not in naked
        assert naked["fixed_floor_state"] == explain.MISSING
        assert "not recorded" in usage.render([bare], "day")
