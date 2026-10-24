"""The inbox assistant's own thread index (#142 W5 S1). Synthetic mail only."""

from __future__ import annotations

import json

from mail_fixtures import DAY, OWNER, T0, gmail

from aish import evidence, mailindex

OWNERS = frozenset({OWNER})


def parsed(msg: dict) -> mailindex.Parsed:
    item = mailindex.from_gmail(msg, OWNERS)
    assert item is not None
    return item


def index_of(*messages: dict, state_dir=None) -> mailindex.MailIndex:
    index = mailindex.MailIndex(state_dir)
    for item in sorted((parsed(m) for m in messages), key=lambda p: p.header.position):
        index.add(item.header, item.body)
    return index


def digest(msg: dict) -> str:
    return parsed(msg).header.digest


def subjects(conversation) -> list[str]:
    return [h.subject for h in conversation]


class TestThreadReconstruction:
    def test_a_reply_chain_is_one_conversation_both_directions(self):
        first = gmail("a", frm="Ania <ania@friends.example>", subject="Saturday", at=T0,
                      message_id="m-a@friends.example")
        reply = gmail("b", frm=f"Owner <{OWNER}>", to="ania@friends.example",
                      subject="Re: Saturday", at=T0 + 3600, message_id="m-b@example.org",
                      in_reply_to="m-a@friends.example", labels=("SENT",))
        again = gmail("c", frm="Ania <ania@friends.example>", subject="Re: Saturday",
                      at=T0 + 7200, message_id="m-c@friends.example",
                      references="<m-a@friends.example> <m-b@example.org>")
        index = index_of(first, reply, again)
        conversation = index.conversation(digest(again))
        assert subjects(conversation) == ["Saturday", "Re: Saturday", "Re: Saturday"]
        assert [h.direction for h in conversation] == ["in", "out", "in"]

    def test_a_header_stripped_reply_links_by_prefix_subject_and_correspondent(self):
        sent = gmail("a", frm=f"Owner <{OWNER}>", to="marek@example.net", subject="The boat",
                     at=T0, labels=("SENT",))
        stripped = gmail("b", frm="Marek <marek@example.net>", subject="Odp: The boat",
                         at=T0 + DAY)
        index = index_of(sent, stripped)
        assert len(index.conversation(digest(stripped))) == 2
        assert index.linked_by[digest(stripped)] == "subject_fallback"

    def test_a_repeated_notification_subject_without_a_reply_prefix_links_nothing(self):
        """Stores reuse subjects across separate orders; a subject match with no
        reply prefix must not merge them."""
        one = gmail("a", frm="shop@lampshop.example", subject="Your order has shipped", at=T0)
        two = gmail("b", frm="shop@lampshop.example", subject="Your order has shipped",
                    at=T0 + DAY)
        index = index_of(one, two)
        assert len(index.conversation(digest(two))) == 1

    def test_the_fallback_needs_a_shared_correspondent_and_the_window(self):
        sent = gmail("a", frm=f"Owner <{OWNER}>", to="marek@example.net", subject="Plans",
                     at=T0, labels=("SENT",))
        stranger = gmail("b", frm="eve@other.example", subject="Re: Plans", at=T0 + DAY)
        late = gmail("c", frm="marek@example.net", subject="Re: Plans",
                     at=T0 + (mailindex.FALLBACK_WINDOW_DAYS + 1) * DAY)
        index = index_of(sent, stranger, late)
        assert len(index.conversation(digest(stranger))) == 1
        assert len(index.conversation(digest(late))) == 1


class TestIdentity:
    def test_the_same_message_under_two_provider_ids_is_one_entry(self):
        """The Tuta gateway renumbers UIDs; identity is the Message-ID."""
        one = gmail("uid-17", frm="a@shop.example", subject="Hello",
                    message_id="same@shop.example")
        two = gmail("uid-902", frm="a@shop.example", subject="Hello",
                    message_id="same@shop.example")
        index = mailindex.MailIndex()
        assert index.add(parsed(one).header, parsed(one).body) is True
        assert index.add(parsed(two).header, parsed(two).body) is False
        assert len(index.by_digest) == 1

    def test_a_message_with_no_message_id_is_keyed_by_its_content(self):
        one = gmail("uid-1", frm="a@shop.example", subject="Hi", body="x", message_id=None)
        two = gmail("uid-2", frm="a@shop.example", subject="Hi", body="x", message_id=None)
        other = gmail("uid-3", frm="a@shop.example", subject="Hi", body="y", message_id=None)
        assert digest(one) == digest(two) != digest(other)

    def test_two_messages_claiming_one_message_id_are_counted_not_silently_merged(self):
        real = gmail("a", frm="bank@bank.example", subject="Statement", message_id="x@bank.example")
        forged = gmail("b", frm="eve@evil.example", subject="Urgent", message_id="x@bank.example",
                       at=T0 + 60)
        index = index_of(real, forged)
        assert len(index.by_digest) == 1
        assert index.health.collisions == [digest(forged)]


class TestFrozenClock:
    def test_a_conversation_as_of_a_mail_excludes_its_future(self):
        ask = gmail("a", frm="ania@friends.example", subject="Car?", at=T0,
                    message_id="q@f.example")
        answer = gmail("b", frm=OWNER, to="ania@friends.example", subject="Re: Car?",
                       at=T0 + DAY, in_reply_to="q@f.example", labels=("SENT",))
        index = index_of(ask, answer)
        assert subjects(index.conversation(digest(ask))) == ["Car?"]
        assert subjects(index.later_replies(digest(ask))) == ["Re: Car?"]

    def test_a_later_message_bridging_two_threads_does_not_join_them_in_the_past(self):
        """A reply whose References name two earlier conversations joins them —
        from its own time on. As of an earlier mail they are still apart."""
        left = gmail("a", frm="x@one.example", subject="Left", at=T0, message_id="l@one.example")
        right = gmail("b", frm="y@two.example", subject="Right", at=T0 + 60,
                      message_id="r@two.example")
        bridge = gmail("c", frm="z@three.example", subject="Both", at=T0 + DAY,
                       references="<l@one.example> <r@two.example>")
        index = index_of(left, right, bridge)
        assert subjects(index.conversation(digest(right))) == ["Right"]
        assert subjects(index.conversation(digest(bridge))) == ["Left", "Right", "Both"]


class TestPersistence:
    def test_a_reload_reproduces_the_conversations(self, tmp_path):
        first = gmail("a", frm="ania@friends.example", subject="Hi", at=T0,
                      message_id="h@f.example")
        reply = gmail("b", frm=OWNER, to="ania@friends.example", subject="Re: Hi", at=T0 + 60,
                      in_reply_to="h@f.example", labels=("SENT",))
        index_of(first, reply, state_dir=tmp_path)
        again = mailindex.MailIndex(tmp_path)
        assert again.health.loaded == 2
        assert subjects(again.conversation(digest(reply))) == ["Hi", "Re: Hi"]

    def test_the_body_lives_in_the_purgeable_evidence_store_not_the_index(self, tmp_path):
        msg = gmail("a", frm="a@shop.example", subject="Hi", body="the secret body text")
        index = index_of(msg, state_dir=tmp_path)
        stored = next((tmp_path / "mail" / "index").glob("*/*.json")).read_text()
        assert "the secret body text" not in stored
        header = index.get(digest(msg))
        assert header is not None
        assert index.body(header.digest) == "the secret body text"
        evidence.purge(header.body_digest, tmp_path)
        assert index.body(header.digest) is None

    def test_one_corrupt_file_costs_that_file_and_is_counted(self, tmp_path):
        index_of(gmail("a", frm="a@x.example", subject="One", at=T0),
                 gmail("b", frm="b@x.example", subject="Two", at=T0 + 1), state_dir=tmp_path)
        victim = next((tmp_path / "mail" / "index").glob("*/*.json"))
        victim.write_text("{not json")
        again = mailindex.MailIndex(tmp_path)
        assert again.health.loaded == 1
        assert again.health.unreadable == [victim.name]


class TestGmailParse:
    def test_the_provider_receipt_time_wins_over_the_date_header(self):
        header = parsed(gmail("a", frm="a@x.example", at=T0 + 5)).header
        assert header.at == T0 + 5 and header.at_source == "provider"

    def test_links_keep_their_visible_text_and_attachments_their_names(self):
        msg = gmail("a", frm="a@x.example", body="plain https://plain.example/x",
                    html='<p><a href="https://evil.example/login">bank.example</a></p>',
                    attachments=("invoice.pdf",))
        header = parsed(msg).header
        assert mailindex.Link("https://evil.example/login", "bank.example") in header.links
        assert [a.filename for a in header.attachments] == ["invoice.pdf"]

    def test_direction_comes_from_the_owner_address_or_the_sent_label(self):
        assert parsed(gmail("a", frm=f"Owner <{OWNER}>", labels=())).header.direction == "out"
        assert parsed(gmail("b", frm="x@y.example", labels=("SENT",))).header.direction == "out"
        assert parsed(gmail("c", frm="x@y.example")).header.direction == "in"

    def test_authentication_results_are_kept_top_most_first(self):
        msg = gmail("a", frm="a@x.example", auth="mx.google.com; dmarc=fail",
                    extra_ar=("evil.example; dmarc=pass",))
        assert parsed(msg).header.auth_results[0].startswith("mx.google.com")

    def test_a_stored_header_round_trips(self):
        header = parsed(gmail("a", frm="a@x.example", html='<a href="https://h.example">t</a>',
                              attachments=("a.pdf",))).header
        assert mailindex.header_from_json(json.loads(json.dumps(header.as_json()))) == header
