"""Email trigger poller tests (#161, in-repo since #178 Gate 3).

The effectful edges are faked at the run_poll parameter seams — the `gws`
subprocess runner, the HTTP POST, the escalation notifier and the clock — so
nothing here spawns a process, sleeps, or touches the network (or the
Keychain: `token=` bypasses read_token)."""

import inspect
import json
import time
import urllib.error

import pytest

from aish import notify as notify_module
from aish.email_poll import (
    FAILURE_STREAK_NOTIFY_AT,
    PROCESSED_LABEL,
    _health_path,
    build_prompt,
    run_poll,
)

OWNER = "Pawel Wenda <pawel@wenda.eu>"


@pytest.fixture(autouse=True)
def _poll_state_in_tmp(tmp_path, monkeypatch):
    """The #362 streak ledger resolves AISH_STATE_DIR at call time; every test
    gets its own tmp dir so nothing reads or writes the developer's real
    state (conftest redirects the variable too — this makes it local and
    deliberate rather than inherited from the browser guard)."""
    monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))


def gmail_msg(from_hdr=OWNER, subject="Hello", auth="dmarc=pass"):
    return {"payload": {"headers": [
        {"name": "From", "value": from_hdr},
        {"name": "Subject", "value": subject},
        {"name": "Authentication-Results", "value": auth},
    ]}}


class FakeGws:
    """Scripted gws runner. Records every call and (optionally) a shared
    timeline so ordering against the POST seam can be asserted."""

    def __init__(self, messages, labels_fail=False, timeline=None):
        self.messages = dict(messages)  # id -> gmail_msg dict
        self.labels_fail = labels_fail
        self.calls = []
        self.timeline = timeline if timeline is not None else []

    def processed_ids(self):
        ids = []
        for args in self.calls:
            if args[1:4] == ["users", "messages", "modify"]:
                ids.append(json.loads(args[args.index("--params") + 1])["id"])
        return ids

    def __call__(self, args, timeout=30):
        self.calls.append(args)
        self.timeline.append(("gws", *args[1:4]))
        verb = tuple(args[1:4])
        if verb == ("users", "messages", "list"):
            return {"messages": [{"id": k} for k in self.messages]}
        if verb == ("users", "labels", "list"):
            if self.labels_fail:
                return None
            return {"labels": [{"name": PROCESSED_LABEL, "id": "L1"}]}
        if verb == ("users", "labels", "create"):
            return None if self.labels_fail else {"id": "L1"}
        if verb == ("users", "messages", "get"):
            msg_id = json.loads(args[args.index("--params") + 1])["id"]
            return self.messages.get(msg_id)
        if verb == ("users", "messages", "modify"):
            return {}
        raise AssertionError(f"unexpected gws call: {args}")


class FakePost:
    def __init__(self, status=200, headers=None, body=b'{"session": "s1"}',
                 exc=None, timeline=None):
        self.status, self.headers, self.body, self.exc = status, headers or {}, body, exc
        self.calls = []
        self.timeline = timeline if timeline is not None else []

    def __call__(self, url, body, timeout=30):
        self.calls.append((url, body))
        self.timeline.append(("post",))
        if self.exc is not None:
            raise self.exc
        return self.status, self.headers, self.body


class DeadGws:
    """gws whose every call fails — the shape of the 2026-08 launchd
    credential outage (#362): the listing itself never comes back."""

    def __call__(self, args, timeout=30):
        return None


class Clock:
    """Injected time, advanced by hand — no sleeps."""

    def __init__(self, t=1_000_000.0):
        self.t = t

    def advance(self, seconds):
        self.t += seconds

    def __call__(self):
        return self.t


class FakeNotify:
    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def __call__(self, title, message):
        self.calls.append((title, message))
        if self.exc is not None:
            raise self.exc
        return True


def poll(gws, post, **kwargs):
    kwargs.setdefault("env", {})
    kwargs.setdefault("token", "t0k")
    return run_poll(gws=gws, post=post, **kwargs)


class TestSkips:
    def test_disallowed_sender_marks_processed_without_trigger(self):
        gws = FakeGws({"m1": gmail_msg(from_hdr="Evil <evil@example.com>")})
        post = FakePost()
        assert poll(gws, post) == 0
        assert post.calls == []  # never triggered…
        assert gws.processed_ids() == ["m1"]  # …but never re-checked either

    def test_dmarc_fail_marks_processed_without_trigger(self):
        gws = FakeGws({"m1": gmail_msg(auth="spf=pass dmarc=fail")})
        post = FakePost()
        assert poll(gws, post) == 0
        assert post.calls == []
        assert gws.processed_ids() == ["m1"]


class TestTrigger:
    def test_success_marks_processed_after_the_post(self):
        timeline = []
        gws = FakeGws({"m1": gmail_msg()}, timeline=timeline)
        post = FakePost(timeline=timeline)
        assert poll(gws, post) == 0
        assert gws.processed_ids() == ["m1"]
        # The label lands strictly AFTER the successful POST: a crash between
        # the two re-fires (deduped server-side), never silently drops.
        assert timeline.index(("post",)) < timeline.index(
            ("gws", "users", "messages", "modify")
        )

    def test_failed_post_leaves_message_unprocessed(self):
        gws = FakeGws({"m1": gmail_msg()})
        post = FakePost(exc=urllib.error.URLError("connection refused"))
        assert poll(gws, post) == 0
        assert len(post.calls) == 1
        assert gws.processed_ids() == []  # retries on the next poll

    def test_429_leaves_unprocessed_and_logs_retry_after(self, capsys):
        gws = FakeGws({"m1": gmail_msg()})
        post = FakePost(status=429, headers={"Retry-After": "30"},
                        body=b'{"error": "rate limit"}')
        assert poll(gws, post) == 0
        assert gws.processed_ids() == []  # a 429 is a FAILED trigger
        err = capsys.readouterr().err
        assert "429" in err
        assert "Retry-After: 30" in err

    def test_post_body_carries_dedup_key_and_origin(self):
        gws = FakeGws({"m1": gmail_msg(subject="Booking")})
        post = FakePost()
        assert poll(gws, post) == 0
        url, raw = post.calls[0]
        assert url.endswith("/trigger?token=t0k")
        body = json.loads(raw)
        assert body["origin"] == "email"
        assert body["meta"]["dedup_key"] == "m1"  # pairs with server idempotency
        assert body["meta"]["id"] == "m1"
        assert body["title"] == "Email: Booking"

    def test_label_ensure_failure_aborts_the_run(self):
        gws = FakeGws({"m1": gmail_msg()}, labels_fail=True)
        post = FakePost()
        assert poll(gws, post) == 1  # abort: triggering without the label re-fires forever
        assert post.calls == []
        assert gws.processed_ids() == []

    def test_missing_token_refuses_to_run(self):
        gws = FakeGws({"m1": gmail_msg()})
        post = FakePost()
        assert poll(gws, post, token="") == 2
        assert gws.calls == []
        assert post.calls == []


class TestFailureEscalation:
    """#362: the poller failed every 180s cycle for 20 days and the only
    evidence was its own log. A persistent streak of failed polls must reach
    the owner — once at the threshold, then at most daily — and a clean poll
    resets it. State is on disk (the poller is one-shot under launchd), kept
    in each test's own tmp state dir by the autouse fixture."""

    def fail(self, notify, clock, times=1):
        for _ in range(times):
            assert poll(DeadGws(), FakePost(), notify=notify, now=clock) == 0
            clock.advance(180)

    def test_streak_notifies_once_at_the_threshold(self):
        clock, notify = Clock(), FakeNotify()
        self.fail(notify, clock, times=FAILURE_STREAK_NOTIFY_AT - 1)
        assert notify.calls == []  # nine failures: not yet persistent
        self.fail(notify, clock)
        assert len(notify.calls) == 1
        title, message = notify.calls[0]
        assert "email poller" in title
        assert str(FAILURE_STREAK_NOTIFY_AT) in message
        # The push states the observation, never a cause no line checked.
        assert "aish-email-poll.log" in message

    def test_renotifies_at_most_daily_while_the_streak_continues(self):
        clock, notify = Clock(), FakeNotify()
        self.fail(notify, clock, times=FAILURE_STREAK_NOTIFY_AT)
        assert len(notify.calls) == 1
        clock.advance(23 * 3600)
        self.fail(notify, clock)  # 23h into the streak: still the one push
        assert len(notify.calls) == 1
        clock.advance(2 * 3600)
        self.fail(notify, clock)  # past a day since the last push: one more
        assert len(notify.calls) == 2
        self.fail(notify, clock)  # and the daily gate re-arms
        assert len(notify.calls) == 2

    def test_clean_poll_resets_streak_and_notified_state(self):
        clock, notify = Clock(), FakeNotify()
        self.fail(notify, clock, times=FAILURE_STREAK_NOTIFY_AT)
        assert len(notify.calls) == 1
        # A successful listing of a quiet mailbox is a CLEAN poll.
        assert poll(FakeGws({}), FakePost(), notify=notify, now=clock) == 0
        # A new streak earns the threshold again from zero…
        self.fail(notify, clock, times=FAILURE_STREAK_NOTIFY_AT - 1)
        assert len(notify.calls) == 1
        # …and then notifies even though a day has not passed since the last.
        self.fail(notify, clock)
        assert len(notify.calls) == 2

    def test_raising_notifier_never_breaks_the_poll(self):
        clock = Clock()
        notify = FakeNotify(exc=RuntimeError("pushover exploded"))
        self.fail(notify, clock, times=FAILURE_STREAK_NOTIFY_AT)
        assert len(notify.calls) == 1  # attempted at the threshold, poll survived
        # The ATTEMPT is stamped, so a raise cannot turn into per-poll retries.
        self.fail(notify, clock)
        assert len(notify.calls) == 1

    @pytest.mark.parametrize("ledger", ["not json", "[1, 2]", '{"failures": "x"}',
                                        '{"failures": 3, "notified_at": "later"}'])
    def test_corrupt_ledger_costs_a_late_escalation_never_a_poll(self, ledger):
        clock, notify = Clock(), FakeNotify()
        _health_path().parent.mkdir(parents=True, exist_ok=True)
        _health_path().write_text(ledger)
        self.fail(notify, clock)  # survives, and starts a fresh count
        assert json.loads(_health_path().read_text()) == {"failures": 1, "notified_at": None}
        assert notify.calls == []

    def test_production_seams_are_pushover_and_the_wall_clock(self):
        # The seams exist so tests can inject; production must still reach the
        # real notifier (a silent no-op when unconfigured) and real time.
        defaults = inspect.signature(run_poll).parameters
        assert defaults["notify"].default is notify_module.pushover
        assert defaults["now"].default is time.time


class TestPrompt:
    def test_prompt_pins_the_owner_address_and_auto_approve_contract(self):
        prompt = build_prompt("m1", OWNER, "Trip plan")
        # The recipient-scoped autonomy contract (#160): answer the OWNER via
        # a NEW gmail_send (auto-approved), never a threaded reply.
        assert "to=pawel@wenda.eu" in prompt
        assert "AUTO-APPROVED" in prompt
        assert "aish@wenda.eu" in prompt
        assert "Re: Trip plan" in prompt
        assert "Do NOT use a threaded reply" in prompt
        # Everything beyond the owner is draft-and-hold.
        assert "HELD for the owner to approve" in prompt
        assert "message id: m1" in prompt
        # The gmail tools have no default mailbox: the trigger names the bot's.
        assert prompt.count("account='aish@wenda.eu'") == 2
        assert "gws-gmail-read" not in prompt
