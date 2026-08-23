"""Classification of a failed model call, and the wait it earns.

The incident behind these tests: a Gemini 429 was retried instantly with the
same ~120k-token request and left no record at all. Every test here pins one
half of "never do that again" — the verdict, its evidence, or the wait.

Exceptions are hand-built rather than imported from any SDK on purpose. The
classifier duck-types `status_code` / `response.headers` / `body` precisely so
it needs none of them, and a test that imported `openai` would stop testing
that property.
"""

from __future__ import annotations

import time

import pytest

from aish import ratelimit

_REAL_WAIT = ratelimit.wait


class FakeResponse:
    def __init__(self, status=None, headers=None):
        self.status_code = status
        self.headers = headers or {}


class FakeAPIError(Exception):
    """The shape openai/anthropic raise: message, status_code, response, body."""

    def __init__(self, message, status=None, headers=None, body=None):
        super().__init__(message)
        self.status_code = status
        self.response = FakeResponse(status, headers)
        self.body = body


GEMINI_QUOTA_BODY = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "27s"},
        ],
    }
}


def informative_429(message="quota"):
    """A refusal that NAMES the window it hit — the only kind the governor may
    learn a ceiling from. `GEMINI_QUOTA_BODY` carries a per-minute quotaId and a
    RetryInfo, so it is evidence about a RATE. A bare `status=429` is evidence
    only that something was too much, and the governor now treats the two
    differently (`TestAnAnonymousRefusalTeachesNothing`).
    """
    return FakeAPIError(message, status=429, body=GEMINI_QUOTA_BODY)


class TestClassify:
    def test_429_is_a_rate_limit(self):
        failure = ratelimit.classify(FakeAPIError("boom", status=429))
        assert failure.kind == ratelimit.RATE_LIMIT
        assert failure.is_rate_limit
        assert failure.status == 429

    def test_auth_failure_is_never_retried(self):
        """The old loop retried a bad API key, spending a request to relearn it."""
        for status in (401, 403):
            failure = ratelimit.classify(FakeAPIError("nope", status=status))
            assert failure.kind == ratelimit.AUTH
            assert not failure.retryable

    def test_bad_request_is_never_retried(self):
        failure = ratelimit.classify(FakeAPIError("malformed", status=400))
        assert failure.kind == ratelimit.BAD_REQUEST
        assert not failure.retryable

    def test_server_error_is_retried(self):
        failure = ratelimit.classify(FakeAPIError("upstream", status=503))
        assert failure.kind == ratelimit.SERVER
        assert failure.retryable

    def test_transport_error_is_retried(self):
        """A busy local Ollama drops the connection and carries no status at all."""
        failure = ratelimit.classify(OSError("Connection reset by peer"))
        assert failure.kind == ratelimit.TRANSPORT
        assert failure.retryable

    def test_unrecognised_failure_stays_retryable(self):
        """The pre-existing behaviour retried everything once. A classifier that
        silently stopped retrying what it failed to recognise would be a
        regression wearing a refinement's clothes."""
        failure = ratelimit.classify(RuntimeError("something new and strange"))
        assert failure.kind == ratelimit.UNKNOWN
        assert failure.retryable

    def test_rate_limit_recognised_with_no_status(self):
        """A transport wrapper that lost the status still must not be retried
        instantly — the prose is the floor, not the mechanism."""
        failure = ratelimit.classify(RuntimeError("Error code: 429 - RESOURCE_EXHAUSTED"))
        assert failure.kind == ratelimit.RATE_LIMIT

    def test_prose_match_is_recorded_as_the_guess_it_is(self):
        """`matched` separates "the provider sent 429" from "we found the word"."""
        by_status = ratelimit.classify(FakeAPIError("x", status=429))
        by_prose = ratelimit.classify(RuntimeError("rate limit exceeded"))
        assert by_status.matched == "status:429"
        assert by_prose.matched and by_prose.matched != "status:429"


class TestRetryHint:
    def test_header_seconds(self):
        exc = FakeAPIError("x", status=429, headers={"Retry-After": "30"})
        assert ratelimit.retry_hint(exc) == (30.0, "header")

    def test_header_milliseconds(self):
        exc = FakeAPIError("x", status=429, headers={"retry-after-ms": "1500"})
        assert ratelimit.retry_hint(exc) == (1.5, "header_ms")

    def test_header_http_date(self):
        now = time.time()
        exc = FakeAPIError(
            "x", status=429, headers={"Retry-After": "Thu, 21 Aug 2026 20:00:00 GMT"}
        )
        secs, source = ratelimit.retry_hint(exc, now=now)
        assert source == "header_date"
        assert secs is not None and secs >= 0

    def test_google_retry_info_in_the_body(self):
        """Google's compat gateway carries the wait nowhere near a header."""
        exc = FakeAPIError("x", status=429, body=GEMINI_QUOTA_BODY)
        assert ratelimit.retry_hint(exc) == (27.0, "body")

    def test_no_hint_says_so_rather_than_guessing(self):
        """"we waited 5s because nobody told us" and "because it said 5s" are
        different facts; the source field is what keeps them apart."""
        assert ratelimit.retry_hint(FakeAPIError("x", status=429)) == (None, "none")

    def test_header_wins_over_body(self):
        exc = FakeAPIError("x", status=429, headers={"Retry-After": "9"}, body=GEMINI_QUOTA_BODY)
        assert ratelimit.retry_hint(exc) == (9.0, "header")


class TestQuotaScope:
    def test_per_minute_quota_is_waitable(self):
        exc = FakeAPIError("quota", status=429, body=GEMINI_QUOTA_BODY)
        failure = ratelimit.classify(exc)
        assert failure.scope == ratelimit.SHORT
        assert failure.retryable
        assert not failure.exhausted

    def test_per_day_quota_is_not_waitable(self):
        """Retrying into a daily quota burns every remaining request of the day
        without ever completing a call."""
        body = {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDay"}]}]}}
        failure = ratelimit.classify(FakeAPIError("quota", status=429, body=body))
        assert failure.scope == ratelimit.LONG
        assert not failure.retryable
        assert failure.exhausted

    def test_day_wins_when_a_quota_id_names_both_windows(self):
        body = {"error": {"message": "per minute and per day limits", "details": []}}
        assert ratelimit.classify(FakeAPIError("q", status=429, body=body)).scope == ratelimit.LONG

    def test_an_absurdly_long_hint_counts_as_exhausted(self):
        """A provider may express a daily quota only as a wait of hours. Sleeping
        on one inside a task means the user watches a spinner instead of reading
        an error."""
        exc = FakeAPIError("x", status=429, headers={"Retry-After": "7200"})
        assert ratelimit.classify(exc).exhausted

    def test_scope_is_empty_when_the_failure_is_not_a_quota(self):
        assert ratelimit.classify(FakeAPIError("x", status=500)).scope == ""


class TestBackoffDelay:
    def test_provider_hint_wins(self):
        failure = ratelimit.classify(FakeAPIError("x", status=429, body=GEMINI_QUOTA_BODY))
        assert ratelimit.backoff_delay(failure, attempt=1) == 27.0

    def test_hint_is_capped_so_a_wild_number_cannot_hang_a_task(self):
        failure = ratelimit.classify(
            FakeAPIError("x", status=429, headers={"Retry-After": "99999"})
        )
        assert ratelimit.backoff_delay(failure, attempt=1) == ratelimit.MAX_WAIT_S

    def test_rate_limit_without_a_hint_never_retries_immediately(self):
        """The whole bug in one assertion: the old path waited 0 seconds."""
        failure = ratelimit.classify(FakeAPIError("x", status=429))
        assert ratelimit.backoff_delay(failure, attempt=1) >= 5.0

    def test_backoff_grows(self):
        failure = ratelimit.classify(FakeAPIError("x", status=503))
        first = ratelimit.backoff_delay(failure, attempt=1)
        second = ratelimit.backoff_delay(failure, attempt=2)
        assert second > first


class TestFailureRecord:
    def test_record_carries_the_evidence_not_the_conclusion(self):
        failure = ratelimit.classify(FakeAPIError("quota", status=429, body=GEMINI_QUOTA_BODY))
        record = failure.record()
        assert record["class"] == ratelimit.RATE_LIMIT
        assert record["status"] == 429
        assert record["retry_after_s"] == 27.0
        assert record["retry_after_source"] == "body"
        assert record["scope"] == ratelimit.SHORT
        assert record["matched"]

    def test_absent_facts_are_absent_rather_than_zero(self):
        """Writing 0 for a wait nobody stated would be a claim, not a record."""
        record = ratelimit.classify(RuntimeError("weird")).record()
        assert "retry_after_s" not in record
        assert "status" not in record

    def test_record_is_json_serialisable(self):
        import json

        json.dumps(ratelimit.classify(FakeAPIError("x", status=429)).record())


@pytest.mark.parametrize("status", [429, 401, 400, 500, None])
def test_every_classification_names_a_known_class(status):
    failure = ratelimit.classify(FakeAPIError("x", status=status))
    assert failure.kind in {
        ratelimit.RATE_LIMIT, ratelimit.AUTH, ratelimit.BAD_REQUEST,
        ratelimit.SERVER, ratelimit.TRANSPORT, ratelimit.UNKNOWN,
    }


class TestInterruptibleWait:
    """The suite-wide guard in conftest patches `ratelimit.wait` out; this class
    restores the real one, which is what keeps the waiting itself covered."""

    @pytest.fixture(autouse=True)
    def real_wait(self, monkeypatch):
        monkeypatch.setattr(ratelimit, "wait", _REAL_WAIT)

    def test_returns_false_after_waiting_out_the_delay(self):
        import threading

        stop = threading.Event()
        started = time.monotonic()
        assert ratelimit.wait(0.05, stop) is False
        assert time.monotonic() - started >= 0.04

    def test_a_stop_ends_the_wait_immediately(self):
        """A Stop pressed during a 27s rate-limit wait must land as fast as one
        pressed mid-stream, or the button is a lie for exactly the stretch the
        user is most likely to press it."""
        import threading

        stop = threading.Event()
        threading.Timer(0.05, stop.set).start()
        started = time.monotonic()
        assert ratelimit.wait(30.0, stop) is True
        assert time.monotonic() - started < 5.0

    def test_an_already_stopped_agent_never_sleeps(self):
        import threading

        stop = threading.Event()
        stop.set()
        started = time.monotonic()
        assert ratelimit.wait(30.0, stop) is True
        assert time.monotonic() - started < 1.0

    def test_the_wait_says_why_it_is_waiting(self):
        """The longest thing a task does without producing a step explains
        itself while it happens, not afterwards."""
        import threading

        seen: list[str] = []
        ratelimit.wait(0.05, threading.Event(), seen.append)
        assert seen and seen[0].startswith("Rate-limited — retrying in")


class TestModelErrorRecord:
    """The failure reaches the LOG, not just the live transport.

    Before #261 this was `self.echo(...)` — a Bridge event that fanned out to
    viewers and the hot transcript and was never written to the session JSONL.
    `grep -c '"echo"'` on the log of the session that motivated this returns 0,
    so a cold reload showed a silent gap where a quota failure had been.
    """

    def agent_with(self, exc, responses=("recovered",)):
        from tests.test_agent import Agent, model_says

        replies = list(responses)
        steps: list[dict] = []
        raised = {"n": 0}

        def chat(**kwargs):
            if raised["n"] < RAISE_TIMES["n"]:
                raised["n"] += 1
                raise exc
            return model_says(replies.pop(0) if replies else "done")

        agent = Agent(model="fake", approve=lambda _c: True, client_chat=chat,
                      step_log=steps.append)
        agent.provider = "gemini"
        return agent, steps

    def test_a_recovered_failure_is_still_recorded(self):
        """The retried-then-recovered path left NO evidence at all: the task
        succeeded, so nothing failed loudly, and the echo reached no log."""
        RAISE_TIMES["n"] = 1
        agent, steps = self.agent_with(FakeAPIError("quota", status=429))
        assert agent.run_task("hi") == "recovered"
        errors = [s for s in steps if s.get("kind") == "model_error"]
        assert len(errors) == 1
        assert errors[0]["class"] == ratelimit.RATE_LIMIT
        assert errors[0]["status"] == 429
        assert errors[0]["action"] == "retry"
        assert errors[0]["waited_s"] > 0

    def test_the_record_joins_to_the_call_around_it(self):
        """`model_call` is what lets a dossier put the failure between the brief
        (what was handed) and the reasoning (what came back)."""
        RAISE_TIMES["n"] = 1
        agent, steps = self.agent_with(FakeAPIError("quota", status=429))
        agent.run_task("hi")
        error = next(s for s in steps if s.get("kind") == "model_error")
        assert error["model_call"] >= 1
        assert error["provider"] == "gemini"
        assert error["model"] == "fake"
        assert "turn" in error  # TURN_STAMPED_STEPS

    def test_the_record_measures_chars_not_estimated_tokens(self):
        """Chars are a measured fact; a token estimate here would wear the same
        unit as the provider's own number and invite a false comparison (#262)."""
        RAISE_TIMES["n"] = 1
        agent, steps = self.agent_with(FakeAPIError("quota", status=429))
        agent.run_task("hi")
        error = next(s for s in steps if s.get("kind") == "model_error")
        assert error["sent_chars"] > 0
        assert error["sent_messages"] > 0
        assert not any("token" in key for key in error)

    def test_a_permanent_failure_spends_one_attempt_not_three(self):
        """Retrying a wrong API key spends a request to relearn a permanent
        answer — and against a quota, spends it from the thing that ran out."""
        from aish.agent import ModelUnavailable

        RAISE_TIMES["n"] = 99
        agent, steps = self.agent_with(FakeAPIError("bad key", status=401))
        with pytest.raises(ModelUnavailable):
            agent.run_task("hi")
        errors = [s for s in steps if s.get("kind") == "model_error"]
        assert len(errors) == 1
        assert errors[0]["action"] == "give_up"

    def test_a_transient_failure_spends_every_attempt(self):
        from aish.agent import MODEL_CALL_ATTEMPTS, ModelUnavailable

        RAISE_TIMES["n"] = 99
        agent, steps = self.agent_with(OSError("connection reset"))
        with pytest.raises(ModelUnavailable):
            agent.run_task("hi")
        errors = [s for s in steps if s.get("kind") == "model_error"]
        assert len(errors) == MODEL_CALL_ATTEMPTS
        assert [e["attempt"] for e in errors] == list(range(1, MODEL_CALL_ATTEMPTS + 1))
        assert errors[-1]["action"] == "give_up"

    def test_a_spent_daily_quota_says_so_instead_of_a_traceback(self):
        """Whether waiting helps is the only thing the reader can act on."""
        from aish.agent import ModelUnavailable

        RAISE_TIMES["n"] = 99
        body = {"error": {"details": [{"violations": [{"quotaId": "RequestsPerDay"}]}]}}
        agent, _ = self.agent_with(FakeAPIError("quota", status=429, body=body))
        with pytest.raises(ModelUnavailable, match="spent rather than busy"):
            agent.run_task("hi")

    def test_the_error_text_is_capped_and_says_which_cap_cut_it(self):
        from aish.agent import MODEL_ERROR_CHARS

        RAISE_TIMES["n"] = 1
        agent, steps = self.agent_with(FakeAPIError("x" * 5000, status=429))
        agent.run_task("hi")
        error = next(s for s in steps if s.get("kind") == "model_error")
        assert len(error["text"]) == MODEL_ERROR_CHARS
        assert error["truncated"] == 5000 - MODEL_ERROR_CHARS
        assert error["cap_source"] == "constant:MODEL_ERROR_CHARS"

    def test_a_stop_during_the_wait_is_a_stop_not_a_provider_failure(self):
        """The user is owed the cancel path, not a ModelUnavailable blaming the
        provider for their own decision."""
        from aish.agent import CANCELLED_RESULT

        RAISE_TIMES["n"] = 99
        agent, _ = self.agent_with(FakeAPIError("quota", status=429))
        ratelimit_wait_calls = {"n": 0}

        def stop_during_wait(delay, stop, note=None):
            ratelimit_wait_calls["n"] += 1
            agent.cancel()
            return True

        import aish.ratelimit as rl

        original, rl.wait = rl.wait, stop_during_wait
        try:
            assert agent.run_task("hi") == CANCELLED_RESULT
        finally:
            rl.wait = original
        assert ratelimit_wait_calls["n"] == 1


RAISE_TIMES = {"n": 0}


class TestEstimate:
    def test_chars_over_three_matches_the_history_budget_convention(self):
        messages = [{"role": "user", "content": "x" * 300}]
        assert ratelimit.estimate_tokens(messages) == 100

    def test_images_are_counted_because_chars_cannot_see_them(self):
        """A vision-heavy history estimated on text alone under-reserves by
        thousands of tokens per image, and every delivered image is re-encoded
        into every later request."""
        text_only = [{"role": "user", "content": "hi"}]
        with_image = [{"role": "user", "content": "hi", "images": ["<blob>"]}]
        assert (
            ratelimit.estimate_tokens(with_image)
            - ratelimit.estimate_tokens(text_only)
            == ratelimit.IMAGE_TOKENS
        )

    def test_tool_calls_are_not_free(self):
        calls = [{"role": "assistant", "content": "", "tool_calls": [{"f": "x" * 300}]}]
        assert ratelimit.estimate_tokens(calls) > 0

    def test_nothing_to_send_estimates_nothing(self):
        assert ratelimit.estimate_tokens(None) == 0
        assert ratelimit.estimate_tokens([]) == 0


class TestGovernorLimits:
    """aish cannot know which billing tier a key is on, so the shipped model of
    the world is "no ceiling known" — throttling a paid key on a guessed
    free-tier number would be a self-inflicted outage. A 429 is the moment the
    world states the ceiling."""

    def test_nothing_is_enforced_until_something_is_known(self):
        governor = ratelimit.Governor()
        assert governor.limits("gemini:flash").source == "none"
        for _ in range(50):
            governor.reserve("gemini:flash", 100_000, ceiling=0).settle(100_000)

    def test_the_owner_can_state_their_tier(self, monkeypatch):
        monkeypatch.setenv("AISH_RATE_LIMIT_GEMINI", "rpm=10,tpm=250000")
        limits = ratelimit.Governor().limits("gemini:gemini-3.5-flash")
        assert (limits.rpm, limits.tpm, limits.source) == (10, 250_000, "env")

    def test_a_model_specific_override_beats_the_provider_one(self, monkeypatch):
        monkeypatch.setenv("AISH_RATE_LIMIT_GEMINI", "rpm=10")
        monkeypatch.setenv("AISH_RATE_LIMIT_GEMINI:FLASH", "rpm=99")
        assert ratelimit.Governor().limits("gemini:flash").rpm == 99

    def test_a_429_that_names_its_quota_teaches_the_ceiling(self):
        """The limit is published, static and contended only by yourself, so it
        is corrected ONCE to just under the rate that produced the failure —
        never nudged, never re-probed upward at a full request's cost."""
        now = [0.0]
        governor = ratelimit.Governor(clock=lambda: now[0])
        for _ in range(10):
            governor.reserve("gemini:flash", 1_000).settle(1_000)
        governor.observe("gemini:flash", ratelimit.classify(informative_429("q")))
        limits = governor.limits("gemini:flash")
        assert limits.source == "observed"
        assert limits.rpm == 9  # 10 requests * 0.9
        assert limits.tpm == 9_000

    def test_a_refusal_only_ever_tightens_what_is_currently_enforced(self):
        """The evidence a refusal carries is an UPPER bound on the rate that
        just failed."""
        now = [0.0]
        governor = ratelimit.Governor(clock=lambda: now[0], wall=lambda: 1000.0)
        for _ in range(10):
            governor.reserve("g:m", 1_000).settle(1_000)
        governor.observe("g:m", ratelimit.classify(informative_429("q")))
        tight = governor.limits("g:m").rpm
        now[0] = ratelimit.WINDOW_S + 1  # a fresh window, well under the ceiling
        for _ in range(3):
            governor.reserve("g:m", 10).settle(10)
        governor.observe("g:m", ratelimit.classify(informative_429("q")))
        assert governor.limits("g:m").rpm <= tight

    def test_tightening_is_a_minimum_not_a_replacement(self):
        assert ratelimit._tightest(9, 30) == 9
        assert ratelimit._tightest(30, 9) == 9
        assert ratelimit._tightest(None, 9) == 9

    def test_a_stated_tier_outranks_an_inferred_one(self, monkeypatch):
        monkeypatch.setenv("AISH_RATE_LIMIT_G", "rpm=5")
        governor = ratelimit.Governor()
        governor.reserve("g:m", 10).settle(10)
        governor.observe("g:m", ratelimit.classify(informative_429("q")))
        assert governor.limits("g:m").source == "env"

    def test_a_non_rate_failure_teaches_nothing(self):
        governor = ratelimit.Governor()
        governor.observe("g:m", ratelimit.classify(FakeAPIError("x", status=500)))
        assert governor.limits("g:m").source == "none"


class TestGovernorAdmission:
    def _governed(self, rpm=None, tpm=None):
        # One list drives both clocks: the rolling window is monotonic and the
        # latch is wall, and a test that moved only one would pass for the
        # wrong reason.
        now = [0.0]
        governor = ratelimit.Governor(clock=lambda: now[0], wall=lambda: now[0])
        governor._limits["g:m"] = ratelimit.Limits(rpm=rpm, tpm=tpm, source="env")
        return governor, now

    def test_requests_per_minute_binds(self):
        governor, _ = self._governed(rpm=2)
        governor.reserve("g:m", 1).settle(1)
        governor.reserve("g:m", 1).settle(1)
        with pytest.raises(ratelimit.RateLimited, match="waited"):
            governor.reserve("g:m", 1, ceiling=0)

    def test_tokens_per_minute_binds_independently(self):
        governor, _ = self._governed(tpm=1_000)
        governor.reserve("g:m", 900).settle(900)
        with pytest.raises(ratelimit.RateLimited, match="waited"):
            governor.reserve("g:m", 900, ceiling=0)

    def test_headroom_returns_when_the_window_rolls(self):
        governor, now = self._governed(rpm=1)
        governor.reserve("g:m", 1).settle(1)
        now[0] = ratelimit.WINDOW_S + 1
        governor.reserve("g:m", 1, ceiling=0)  # no longer blocked

    def test_debited_pessimistically_before_the_call(self):
        """Optimistic accounting lets concurrent calls overshoot together — and
        that overshoot IS the 429 this exists to prevent."""
        governor, now = self._governed(tpm=1_000)
        governor.reserve("g:m", 900)  # never settled
        assert governor._window("g:m").totals(now[0]) == (1, 900)

    def test_the_estimate_is_corrected_to_the_truth(self):
        governor, now = self._governed(tpm=10_000)
        governor.reserve("g:m", 900).settle(4_000)
        assert governor._window("g:m").totals(now[0]) == (1, 4_000)

    def test_a_refusal_keeps_the_request_but_drops_its_tokens(self):
        """The request happened and plausibly counts against RPM; tokens the
        provider never processed must not crowd out the retry."""
        governor, now = self._governed(tpm=10_000)
        governor.reserve("g:m", 900).rejected()
        assert governor._window("g:m").totals(now[0]) == (1, 0)

    def test_an_abandoned_stream_keeps_its_estimate(self):
        """Usage arrives on the final chunk, so a stream nobody finished reports
        nothing — and the server may well have generated the whole response.
        Eating the estimate is the only direction that errs safe."""
        governor, now = self._governed(tpm=10_000)
        governor.reserve("g:m", 900)  # dropped on the floor, never settled
        assert governor._window("g:m").totals(now[0]) == (1, 900)

    def test_a_call_bigger_than_the_whole_budget_is_refused_not_queued(self):
        """A naive "wait until it fits" blocks forever. The caller is told the
        one thing that can work: make the request smaller."""
        governor, _ = self._governed(tpm=1_000)
        with pytest.raises(ratelimit.RateLimited, match="cannot be sent at any rate"):
            governor.reserve("g:m", 50_000)

    def test_a_spent_quota_refuses_immediately_rather_than_queueing(self):
        governor, _ = self._governed(rpm=100)
        body = {"error": {"details": [{"violations": [{"quotaId": "RequestsPerDay"}]}]}}
        governor.observe("g:m", ratelimit.classify(FakeAPIError("q", status=429, body=body)))
        with pytest.raises(ratelimit.RateLimited, match="spent rather than busy"):
            governor.reserve("g:m", 1)

    def test_the_latch_lifts_when_the_quota_resets(self):
        governor, now = self._governed(rpm=100)
        exc = FakeAPIError("q", status=429, headers={"Retry-After": "1800"})
        governor.observe("g:m", ratelimit.classify(exc))
        assert governor.exhausted_for("g:m") == 1800
        now[0] = 1801
        assert governor.exhausted_for("g:m") is None
        governor.reserve("g:m", 1)

    def test_a_stop_while_queued_is_a_cancel_not_a_provider_failure(self):
        governor, _ = self._governed(rpm=1)
        governor.reserve("g:m", 1).settle(1)
        with pytest.raises(ratelimit.Cancelled):
            governor.reserve("g:m", 1, should_stop=lambda: True)

    def test_the_wait_names_the_provider_not_the_model(self):
        """The user is waiting on a provider's quota. `provider:model` is the
        governor's own bookkeeping and reads as noise in a status line."""

        class Escape(Exception):
            """Leaves the wait loop without spending a real tick sleeping."""

        governor, _ = self._governed(rpm=1)
        governor._limits["gemini:gemini-3.5-flash"] = ratelimit.Limits(rpm=1, source="env")
        governor.reserve("gemini:gemini-3.5-flash", 1).settle(1)
        seen = []

        def note(message):
            seen.append(message)
            raise Escape

        with pytest.raises(Escape):
            governor.reserve("gemini:gemini-3.5-flash", 1, on_wait=note, ceiling=1)
        assert seen[0].startswith("Waiting on the gemini rate limit — about")
        assert "gemini-3.5-flash" not in seen[0]

    def test_a_governor_refusal_is_marked_as_never_sent(self):
        """"the provider refused" and "aish declined to ask" look identical in a
        bare error string and mean opposite things about whose budget moved."""
        failure = ratelimit.classify(ratelimit.RateLimited("nope", retry_after_s=60))
        assert failure.is_rate_limit
        assert failure.sent is False
        assert not failure.retryable
        assert failure.record()["sent"] is False


class TestGovernorIsShared:
    """Process-global, not per chat: aish-web runs many sessions as worker
    threads against ONE API key, so a governor per session would be N programs
    each convinced it had the whole budget."""

    def test_one_governor_serves_the_whole_process(self):
        assert ratelimit.governor() is ratelimit.governor()

    def test_two_callers_share_one_budget(self):
        governor = ratelimit.reset_governor()
        governor._limits["g:m"] = ratelimit.Limits(rpm=2, source="env")
        # Two unrelated consumers of the same key — an agent turn and, say, the
        # session retitler — draw from the same window.
        ratelimit.reserve_for_call("g:m", [{"content": "a"}]).settle(1)
        ratelimit.reserve_for_call("g:m", [{"content": "b"}]).settle(1)
        with pytest.raises(ratelimit.RateLimited):
            with ratelimit.hooks(ceiling=0):
                ratelimit.reserve_for_call("g:m", [{"content": "c"}])

    def test_keys_are_per_model_not_per_provider(self):
        """Quotas are per model on the tiers this matters for, and one process
        genuinely mixes models on one key."""
        governor = ratelimit.reset_governor()
        governor._limits["g:flash"] = ratelimit.Limits(rpm=1, source="env")
        governor.reserve("g:flash", 1).settle(1)
        governor.reserve("g:pro", 1, ceiling=0).settle(1)  # a different budget

    def test_an_unattended_session_queues_far_less_than_a_user(self):
        """It holds a thread from the bounded worker pool, which exists so a
        parked session cannot starve short user actions."""
        assert ratelimit.UNATTENDED_WAIT_CEILING_S < ratelimit.DEFAULT_WAIT_CEILING_S

    def test_hooks_do_not_leak_past_their_block(self):
        with ratelimit.hooks(ceiling=3):
            assert ratelimit.current_hooks().ceiling == 3
        assert ratelimit.current_hooks().ceiling is None


class TestSpendBudget:
    """History size is the spend control on a metered backend, and nothing
    modelled it (#261).

    `HISTORY_TOKEN_CEILING` sizes history against the CONTEXT WINDOW — what one
    request may contain. What a MINUTE of requests may contain is a different
    constraint, and in the incident it was the binding one: history sat at 130k
    tokens so a 300k ceiling never fired, while 16 calls of that size spent
    1.91M input tokens in one minute against a far smaller quota.
    """

    def agent(self, provider="gemini", model="gemini-3.5-flash"):
        from tests.test_agent import Agent, model_says

        agent = Agent(model=model, approve=lambda _c: True,
                      client_chat=lambda **kw: model_says("ok"))
        agent.provider = provider
        return agent

    def test_nothing_changes_while_no_limit_is_known(self):
        """A key that never hits a quota must behave exactly as it did — aish
        cannot know its billing tier, and trimming on a guess loses context for
        nothing."""
        agent = self.agent()
        budget, source = agent._history_budget()
        assert "ratelimit" not in source
        assert budget == 300_000 * 3  # the context ceiling, untouched

    def test_a_known_rate_limit_tightens_the_history_budget(self, monkeypatch):
        monkeypatch.setenv("AISH_RATE_LIMIT_GEMINI", "tpm=250000")
        ratelimit.reset_governor()
        agent = self.agent()
        budget, source = agent._history_budget()
        from aish.agent import CHARS_PER_TOKEN_BUDGET, SPEND_BUDGET_CALLS_PER_MINUTE

        assert budget == (250_000 // SPEND_BUDGET_CALLS_PER_MINUTE) * CHARS_PER_TOKEN_BUDGET
        assert source.startswith("ratelimit:tpm/")

    def test_the_provenance_says_which_of_three_bounds_bound(self, monkeypatch):
        """"Why was my page cut?" has three different answers — the window, the
        ceiling, the rate — and the number alone tells them apart from none."""
        monkeypatch.setenv("AISH_RATE_LIMIT_GEMINI", "tpm=250000")
        ratelimit.reset_governor()
        _, source = self.agent()._history_budget()
        assert source.endswith(":env")

    def test_a_limit_learned_from_a_429_says_so(self):
        governor = ratelimit.reset_governor()
        agent = self.agent()
        key = f"{agent.provider}:{agent.model}"
        for _ in range(4):
            governor.reserve(key, 100_000).settle(100_000)
        governor.observe(key, ratelimit.classify(informative_429("q")))
        _, source = agent._history_budget()
        assert source.endswith(":observed")

    def test_a_tiny_quota_does_not_trim_into_uselessness(self, monkeypatch):
        """Below the floor, trimming costs more than it saves: the model loses
        the thread and re-fetches what was cut, which is more calls and more
        tokens than it saved."""
        monkeypatch.setenv("AISH_RATE_LIMIT_GEMINI", "tpm=1000")
        ratelimit.reset_governor()
        from aish.agent import CHARS_PER_TOKEN_BUDGET, MIN_SPEND_BUDGET_TOKENS

        budget, _ = self.agent()._history_budget()
        assert budget == MIN_SPEND_BUDGET_TOKENS * CHARS_PER_TOKEN_BUDGET

    def test_the_window_still_wins_when_it_is_tighter(self, monkeypatch):
        """The spend budget only ever tightens. A generous quota must not be
        read as permission to exceed the model's actual context window."""
        monkeypatch.setenv("AISH_RATE_LIMIT_OLLAMA", "tpm=100000000")
        ratelimit.reset_governor()
        agent = self.agent(provider="ollama", model="qwen3:8b")
        budget, source = agent._history_budget()
        assert "ratelimit" not in source
        assert budget == agent.num_ctx * 3


class TestLimitsAreABeliefNotALaw:
    """A real key's ceiling MOVES.

    A pay-as-you-go key on a shared organisation quota has neighbours, so the
    limit at 23:00 is not the limit at 09:00. Holding a once-observed number
    forever converts a temporary squeeze into a permanent self-imposed one —
    believing your own model over the server. So the belief decays toward
    optimism while the server keeps saying yes, and snaps back when it says no.
    """

    def learned(self, wall):
        governor = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: wall[0])
        for _ in range(10):
            governor.reserve("g:m", 1_000).settle(1_000)
        governor.observe("g:m", ratelimit.classify(informative_429("q")))
        return governor

    def test_the_belief_holds_while_the_refusal_is_fresh(self):
        wall = [1000.0]
        governor = self.learned(wall)
        wall[0] += ratelimit.RELAX_AFTER_S - 1
        assert governor.limits("g:m").rpm == governor.believed("g:m").rpm

    def test_a_quiet_stretch_loosens_it(self):
        """There is no synthetic probe: what tests the loosened ceiling is the
        next call the owner was making anyway."""
        wall = [1000.0]
        governor = self.learned(wall)
        strict = governor.believed("g:m").tpm
        wall[0] += ratelimit.RELAX_AFTER_S
        assert governor.limits("g:m").tpm > strict
        wall[0] += ratelimit.RELAX_AFTER_S * 5
        assert governor.limits("g:m").tpm > strict * 2

    def test_loosening_is_visible_in_the_record(self):
        wall = [1000.0]
        governor = self.learned(wall)
        wall[0] += ratelimit.RELAX_AFTER_S * 2
        assert governor.limits("g:m").record()["relaxed"] > 1.0

    def test_the_stored_belief_never_moves_with_the_clock(self):
        """A file that recorded the relaxed view would disagree with itself the
        moment it was read back."""
        wall = [1000.0]
        governor = self.learned(wall)
        before = governor.believed("g:m").rpm
        wall[0] += ratelimit.RELAX_AFTER_S * 10
        assert governor.believed("g:m").rpm == before

    def test_a_new_refusal_snaps_it_back(self):
        wall = [1000.0]
        governor = self.learned(wall)
        wall[0] += ratelimit.RELAX_AFTER_S * 4
        loose = governor.limits("g:m").rpm
        governor.observe("g:m", ratelimit.classify(informative_429("q")))
        assert governor.limits("g:m").rpm < loose

    def test_a_stated_tier_never_drifts(self, monkeypatch):
        """Relaxation is aish correcting its own guess. An owner who stated
        their tier said something aish has no business loosening."""
        monkeypatch.setenv("AISH_RATE_LIMIT_G", "rpm=10,tpm=1000")
        wall = [1000.0]
        governor = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: wall[0])
        governor.observe("g:m", ratelimit.classify(informative_429("q")))
        wall[0] += ratelimit.RELAX_AFTER_S * 10
        assert governor.limits("g:m").rpm == 10
        assert governor.limits("g:m").source == "env"


class TestLearnedLimitsSurviveRestart:
    """A ceiling costs a 429 to learn, so learning it once per RESTART instead
    of once is paying repeatedly for the same information — and `make ship`
    restarts the server often."""

    def test_what_was_learned_is_reloaded(self, tmp_path):
        store = tmp_path / "rate-limits.json"
        first = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: 1000.0, store=store)
        for _ in range(10):
            first.reserve("g:m", 1_000).settle(1_000)
        first.observe("g:m", ratelimit.classify(informative_429("q")))
        learned = first.believed("g:m")

        second = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: 1000.0, store=store)
        assert second.believed("g:m").rpm == learned.rpm
        assert second.believed("g:m").tpm == learned.tpm
        assert second.believed("g:m").source == "observed"

    def test_the_age_of_the_evidence_survives_too(self, tmp_path):
        """Otherwise a restart would look like a fresh refusal and re-freeze a
        ceiling that had spent an hour earning its way back up."""
        store = tmp_path / "rate-limits.json"
        first = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: 1000.0, store=store)
        first.reserve("g:m", 1_000).settle(1_000)
        first.observe("g:m", ratelimit.classify(informative_429("q")))

        later = ratelimit.Governor(
            clock=lambda: 0.0, wall=lambda: 1000.0 + ratelimit.RELAX_AFTER_S * 3, store=store
        )
        assert later.limits("g:m").relaxed > 1.0

    def test_a_spent_quota_is_still_spent_after_a_restart(self, tmp_path):
        """Restart recovery re-runs interrupted triggered sessions, so an
        in-memory latch meant a spent daily quota was re-slammed on every
        restart by the very sessions it had already refused."""
        store = tmp_path / "rate-limits.json"
        body = {"error": {"details": [{"violations": [{"quotaId": "RequestsPerDay"}]}]}}
        first = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: 1000.0, store=store)
        first.observe("g:m", ratelimit.classify(FakeAPIError("q", status=429, body=body)))

        second = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: 1100.0, store=store)
        with pytest.raises(ratelimit.RateLimited, match="spent rather than busy"):
            second.reserve("g:m", 1)

    def test_the_latch_still_lifts_on_schedule_across_a_restart(self, tmp_path):
        store = tmp_path / "rate-limits.json"
        exc = FakeAPIError("q", status=429, headers={"Retry-After": "1800"})
        first = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: 1000.0, store=store)
        first.observe("g:m", ratelimit.classify(exc))

        second = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: 3000.0, store=store)
        assert second.exhausted_for("g:m") is None
        second.reserve("g:m", 1)

    def test_an_unreadable_store_reads_as_nothing_learned(self, tmp_path):
        """Which is exactly the state a first run is in — nothing here is worth
        failing a model call over."""
        store = tmp_path / "rate-limits.json"
        store.write_text("{ not json")
        governor = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: 1.0, store=store)
        assert governor.believed("g:m").source == "none"
        governor.reserve("g:m", 10_000_000).settle(1)

    def test_nothing_is_written_when_there_is_no_state_dir(self, monkeypatch):
        monkeypatch.delenv("AISH_STATE_DIR", raising=False)
        governor = ratelimit.Governor(clock=lambda: 0.0, wall=lambda: 1.0)
        governor.reserve("g:m", 1).settle(1)
        governor.observe("g:m", ratelimit.classify(informative_429("q")))
        assert governor.believed("g:m").source == "observed"  # in memory only


class TestAnAnonymousRefusalTeachesNothing:
    """A 429 that names no window is evidence that SOMETHING was too much, and
    nothing about which dimension or how much.

    The first design learned a ceiling from every 429, snapping it to whatever
    the last minute happened to contain. Measured on this owner's logs that was
    the wrong trade by a wide margin: 21 rate-limit 429s across every session
    ever recorded, all recovered on the FIRST 5s retry and none ever needing a
    second — against 30 calls of ~55s each in one session (23% of all time spent
    in model calls) spent waiting behind an inferred number that swung
    987k → 511k → 1076k tokens/min inside 45 minutes.

    So an anonymous refusal now buys a brief SPACING and no belief at all.
    """

    def _governor(self):
        now = [0.0]
        return ratelimit.Governor(clock=lambda: now[0], wall=lambda: now[0]), now

    def anonymous(self):
        """What Gemini actually sends: RESOURCE_EXHAUSTED with no quotaId, no
        RetryInfo and no Retry-After. Every 429 in this owner's logs records
        `scope=None, retry_after=None`."""
        return ratelimit.classify(FakeAPIError("You exceeded your current quota", status=429))

    def test_it_is_still_a_rate_limit_and_still_backed_off(self):
        """The fork is about what may be BELIEVED, not about whether to wait.
        Backing off is the part that has a 21-for-21 record."""
        failure = self.anonymous()
        assert failure.is_rate_limit and failure.retryable
        assert not failure.names_a_quota
        assert ratelimit.backoff_delay(failure, attempt=1) == 5.0

    def test_a_refusal_that_names_its_window_is_told_apart(self):
        assert ratelimit.classify(
            FakeAPIError("q", status=429, body=GEMINI_QUOTA_BODY)).names_a_quota
        assert ratelimit.classify(
            FakeAPIError("q", status=429, headers={"Retry-After": "30"})).names_a_quota

    def test_a_number_the_governor_invented_is_not_a_provider_hint(self):
        """`RateLimited` carries a `retry_after_s` of aish's own. Reading that
        back as the provider having stated a window would let the governor
        teach itself its own guess."""
        failure = ratelimit.classify(ratelimit.RateLimited("spent", retry_after_s=42))
        assert failure.retry_after_s == 42
        assert not failure.names_a_quota

    def test_no_ceiling_is_learned_from_it(self):
        governor, _ = self._governor()
        governor.reserve("g:m", 100_000).settle(100_000)
        governor.observe("g:m", self.anonymous())
        believed = governor.believed("g:m")
        assert believed.source == "none"
        assert believed.tpm is None and believed.rpm is None

    def test_but_the_key_is_paced_while_the_refusal_is_fresh(self):
        """The hazard that survives without a number: many worker threads in one
        process discovering the same limit at the same moment."""
        governor, now = self._governor()
        governor.observe("g:m", self.anonymous())
        governor.reserve("g:m", 1).rejected()  # the call that drew the 429
        with pytest.raises(ratelimit.RateLimited, match="waited"):
            governor.reserve("g:m", 1, ceiling=0)
        now[0] = ratelimit.COOLDOWN_SPACING_S
        governor.reserve("g:m", 1, ceiling=0)  # spaced, not blocked

    def test_the_pacing_expires_by_itself(self):
        governor, now = self._governor()
        governor.observe("g:m", self.anonymous())
        governor.reserve("g:m", 1).rejected()
        now[0] = ratelimit.COOLDOWN_S + 1
        governor.reserve("g:m", 1, ceiling=0)
        assert "g:m" not in governor._cooldown_until

    def test_a_response_lifts_it_immediately(self):
        """The only direct evidence the squeeze is over, and free: unlike a
        probe it is a call the owner was making anyway."""
        governor, _ = self._governor()
        governor.observe("g:m", self.anonymous())
        governor.reserve("g:m", 1).settle(12_000)
        assert "g:m" not in governor._cooldown_until
        governor.reserve("g:m", 1, ceiling=0)  # unthrottled again

    def test_a_failure_that_reported_no_usage_does_not_lift_it(self):
        """`settle(None)` is how `backends._settle_failure` closes a NON-rate
        failure — a 500, a transport blip. Nothing came back, so it is not a
        response, so it must not release the brake. `settle(0)` is the other
        thing that looks like it: a real response whose provider reported no
        usage. Truthiness cannot tell them apart; `is not None` can."""
        governor, now = self._governor()
        governor.observe("g:m", self.anonymous())
        governor.reserve("g:m", 1).settle(None)
        assert "g:m" in governor._cooldown_until
        now[0] = ratelimit.COOLDOWN_SPACING_S  # past the spacing, not the cooldown
        governor.reserve("g:m", 1, ceiling=0).settle(0)
        assert "g:m" not in governor._cooldown_until

    def test_a_refusal_does_not_lift_it(self):
        """`rejected()` is the 429 path. Clearing the cooldown there would make
        the brake release on exactly the evidence that set it."""
        governor, _ = self._governor()
        governor.observe("g:m", self.anonymous())
        governor.reserve("g:m", 1).rejected()
        assert "g:m" in governor._cooldown_until

    def test_a_named_daily_quota_is_still_latched(self):
        """The fork must not weaken the case it does not cover: an exhausted
        quota names a day, so it is informative, so it still refuses fast."""
        body = {"error": {"details": [{"violations": [{"quotaId": "RequestsPerDay"}]}]}}
        governor, _ = self._governor()
        governor.observe("g:m", ratelimit.classify(FakeAPIError("q", status=429, body=body)))
        with pytest.raises(ratelimit.RateLimited, match="spent rather than busy"):
            governor.reserve("g:m", 1)

    def test_the_history_budget_never_moves_because_of_one(self):
        """The second-order damage, and the reason this is not only about speed.

        The history budget is sized at `tpm / SPEND_BUDGET_CALLS_PER_MINUTE`, so
        a ceiling inferred from an anonymous refusal did not merely slow the
        loop down — on 2026-08-23 at 21:13 it halved a live conversation, from
        822k chars to 383k, and restored it 16 minutes later. A transient brake
        must never size what the model is allowed to remember.
        """
        from tests.test_agent import Agent, model_says

        governor = ratelimit.reset_governor()
        agent = Agent(model="gemini-3.5-flash", approve=lambda _c: True,
                      client_chat=lambda **kw: model_says("ok"))
        agent.provider = "gemini"
        before = agent._history_budget()
        governor.reserve("gemini:gemini-3.5-flash", 300_000).settle(300_000)
        governor.observe("gemini:gemini-3.5-flash", self.anonymous())
        assert agent._history_budget() == before
        assert "ratelimit" not in before[1]
