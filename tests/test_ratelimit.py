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
        assert seen and "retrying in" in seen[0]


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
