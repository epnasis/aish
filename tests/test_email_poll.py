"""Email trigger poller tests (#161, in-repo since #178 Gate 3).

Both effectful edges are faked at the run_poll parameter seams — the `gws`
subprocess runner and the HTTP POST — so nothing here spawns a process or
touches the network (or the Keychain: `token=` bypasses read_token)."""

import json
import urllib.error

from aish.email_poll import PROCESSED_LABEL, build_prompt, run_poll

OWNER = "Pawel Wenda <pawel@wenda.eu>"


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
