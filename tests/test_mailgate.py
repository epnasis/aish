"""Zone A's deterministic half: authentication, red flags, newsletters, and the
owner-acts-only relationship ledger (#142 W5 S1). Synthetic mail only."""

from __future__ import annotations

from mail_fixtures import DAY, OWNER, T0, gmail
from test_mailindex import digest, index_of, parsed

from aish import mailgate, mailindex


def auth_of(msg: dict) -> mailgate.Auth:
    header = parsed(msg).header
    return mailgate.authenticate(header.auth_results, mailindex.domain_of(header.from_addr))


class TestAuthentication:
    def test_gmails_own_dmarc_pass_authenticates(self):
        auth = auth_of(gmail("a", frm="billing@utility.example"))
        assert auth.verdict == mailgate.PASS and auth.authenticated

    def test_a_pass_written_lower_down_by_the_sender_is_never_read(self):
        """Only the TOP-MOST header is Gmail's; a sender can add any number below."""
        msg = gmail("a", frm="ceo@bank.example", auth="fail",
                    extra_ar=("mx.google.com; dmarc=pass header.from=bank.example",))
        auth = auth_of(msg)
        assert auth.verdict == mailgate.FAIL and not auth.authenticated

    def test_the_authserv_id_is_matched_exactly_never_as_a_prefix(self):
        """Found in delivery review: a prefix test read this server as Gmail."""
        forged = ("mx.google.com.evil.example; dmarc=pass header.from=bank.example",)
        assert mailgate.authenticate(forged, "bank.example").verdict == mailgate.UNKNOWN
        versioned = ("mx.google.com 1; dmarc=pass header.from=bank.example",)
        assert mailgate.authenticate(versioned, "bank.example").authenticated

    def test_a_top_header_from_another_authserv_is_unknown(self):
        msg = gmail("a", frm="a@x.example", auth="relay.evil.example; dmarc=pass")
        assert auth_of(msg).verdict == mailgate.UNKNOWN
        assert not auth_of(gmail("b", frm="a@x.example", auth=None)).authenticated

    def test_an_aligned_dkim_pass_under_no_dmarc_policy_authenticates(self):
        msg = gmail("a", frm="news@mail.shop.example",
                    auth="mx.google.com; dkim=pass header.i=@shop.example; dmarc=none")
        assert auth_of(msg).authenticated

    def test_a_dkim_pass_by_somebody_elses_domain_does_not(self):
        msg = gmail("a", frm="ceo@bank.example",
                    auth="mx.google.com; dkim=pass header.i=@bulkmailer.example; dmarc=none")
        assert not auth_of(msg).authenticated


class TestRedFlags:
    def flags(self, msg: dict, admitted=frozenset()) -> set[str]:
        return {f.what for f in mailgate.red_flags(parsed(msg).header, frozenset(admitted))}

    def test_link_text_showing_another_domain(self):
        msg = gmail("a", frm="a@x.example",
                    html='<a href="https://collect.evil.example/x">bank.example</a>')
        assert "link_text_mismatch" in self.flags(msg)

    def test_structural_link_flags_need_no_words(self):
        msg = gmail("a", frm="a@x.example", html=(
            '<a href="https://bank.example@evil.example/">a</a>'
            '<a href="http://203.0.113.9/pay">b</a>'
            '<a href="https://xn--bnk-sna.example/">c</a>'
            '<a href="https://shop.example/account/login?next=1">d</a>'))
        assert {"link_userinfo", "link_ip_host", "link_punycode",
                "link_credential_shaped"} <= self.flags(msg)

    def test_a_lookalike_of_an_admitted_domain(self):
        msg = gmail("a", frm="office@bui1der.example")
        assert "sender_lookalike" in self.flags(msg, {"builder.example"})
        assert "sender_lookalike" not in self.flags(
            gmail("b", frm="office@builder.example"), {"builder.example"})
        assert "sender_lookalike" not in self.flags(
            gmail("c", frm="noreply@mail.builder.example"), {"builder.example"})

    def test_attachment_types(self):
        msg = gmail("a", frm="a@x.example", attachments=("invoice.pdf.exe", "data.json", "x.scr"))
        flags = mailgate.red_flags(parsed(msg).header, frozenset())
        risky = {f.detail for f in flags if f.what == "attachment_risky_type"}
        assert risky == {"invoice.pdf.exe", "x.scr"}  # ".js" is not ".json"
        assert any(f.what == "attachment_double_extension" for f in flags)
        tarball = mailgate.red_flags(
            parsed(gmail("t", frm="a@x.example", attachments=("backup.tar.gz",))).header,
            frozenset())
        assert tarball == ()


class TestNewsletter:
    def test_list_headers_gate_and_say_which(self):
        both = mailgate.newsletter(parsed(gmail("a", frm="n@x.example", list_id="<n.x.example>",
                                                list_unsubscribe="<mailto:u@x.example>")).header)
        only = mailgate.newsletter(parsed(gmail("b", frm="n@x.example",
                                                list_unsubscribe="<mailto:u@x.example>")).header)
        assert both.gated and both.by == ("List-Id", "List-Unsubscribe")
        assert only.gated and only.by == ("List-Unsubscribe",)

    def test_ordinary_mail_is_not_gated(self):
        assert not mailgate.newsletter(parsed(gmail("a", frm="a@x.example")).header).gated


class TestLedger:
    def grade(self, index, msg, **kw) -> mailgate.Grade:
        header = index.get(digest(msg))
        return mailgate.grade(index, header, auth_of(msg), **kw)

    def test_an_inbound_flood_mints_nothing(self):
        flood = [gmail(f"f{n}", frm="promo@loud.example", subject=f"Deal {n}", at=T0 + n * 60)
                 for n in range(100)]
        index = index_of(*flood)
        grade = self.grade(index, flood[-1])
        assert grade.grade == mailgate.FIRST_CONTACT
        assert grade.inbound == 100 and grade.acts == 0

    def test_writing_to_an_address_makes_it_known_then_established(self):
        wrote = [gmail(f"w{n}", frm=OWNER, to="ania@friends.example", subject="hi",
                       at=T0 + n * 20 * DAY, labels=("SENT",)) for n in range(2)]
        early = gmail("e", frm="ania@friends.example", subject="yo", at=T0 + 1 * DAY)
        late = gmail("l", frm="ania@friends.example", subject="yo", at=T0 + 45 * DAY)
        index = index_of(*wrote, early, late)
        assert self.grade(index, early).grade == mailgate.KNOWN
        assert self.grade(index, late).grade == mailgate.ESTABLISHED

    def test_history_does_not_follow_an_unauthenticated_claim(self):
        wrote = gmail("w", frm=OWNER, to="ceo@bank.example", at=T0, labels=("SENT",))
        spoof = gmail("s", frm="ceo@bank.example", at=T0 + DAY, auth="fail")
        index = index_of(wrote, spoof)
        assert self.grade(index, spoof).grade == mailgate.FIRST_CONTACT

    def test_an_owner_act_after_the_mail_is_not_seen_by_it(self):
        mail = gmail("m", frm="ania@friends.example", at=T0)
        wrote = gmail("w", frm=OWNER, to="ania@friends.example", at=T0 + DAY, labels=("SENT",))
        index = index_of(mail, wrote)
        assert self.grade(index, mail).acts == 0

    def test_joining_an_owner_thread_by_references_earns_nothing(self):
        """A hostile mail can put an owner thread's Message-IDs in References.
        His replies there were to somebody else, and stay theirs."""
        friend = gmail("f", frm="ania@friends.example", subject="Trip", at=T0,
                       message_id="t1@friends.example")
        reply = gmail("r", frm=OWNER, to="ania@friends.example", subject="Re: Trip",
                      at=T0 + 60, in_reply_to="t1@friends.example", labels=("SENT",))
        hijack = gmail("h", frm="eve@evil.example", subject="Re: Trip", at=T0 + DAY,
                       references="<t1@friends.example>")
        index = index_of(friend, reply, hijack)
        assert self.grade(index, hijack).grade == mailgate.FIRST_CONTACT

    def test_a_reply_to_the_addresss_own_mail_counts(self):
        ask = gmail("a", frm="desk@club.example", subject="Membership", at=T0,
                    message_id="m@club.example")
        reply = gmail("r", frm=OWNER, to="members@club.example", subject="Re: Membership",
                      at=T0 + 60, in_reply_to="m@club.example", labels=("SENT",))
        later = gmail("l", frm="desk@club.example", subject="Welcome", at=T0 + DAY)
        index = index_of(ask, reply, later)
        grade = self.grade(index, later)
        assert grade.grade == mailgate.KNOWN and grade.how == ("replied",)

    def test_a_case_he_opened_counts_as_an_act_toward_its_parties(self):
        mail = gmail("m", frm="parcels@courier.example", at=T0 + DAY)
        index = index_of(mail)
        grade = self.grade(index, mail, owner_case_parties={"parcels@courier.example": [T0]})
        assert grade.grade == mailgate.KNOWN and grade.how == ("case",)
