"""Tuta senders become verifiable (#461, #142 W5): the bridge's own first-line
`X-Tutabridge-Authentication` header is the Tuta adapter's declared
authenticator. Authenticated only when it is the raw message's FIRST header
line, the ONLY copy in the header block, `status-source=tuta-server` and
`status=authenticated`; every forged shape is pinned as not authenticated,
with what was observed in words. End to end, an authenticated Tuta mail opens a
case and feeds the ledger exactly like authenticated Gmail. A fake himalaya
runner; no bridge, no process."""

from __future__ import annotations

import pytest
from mail_fixtures import DAY, PASS_AR, T0
from test_mailtuta import TUTA_OWNER, FakeHimalaya, rfc822, run, tuta

from aish import mailcases, mailgate, mailindex, mailverdicts

GOOD = ("X-Tutabridge-Authentication: status=authenticated; status-source=tuta-server; "
        "encryption-auth=none")


def bridged(header_line: str | None = GOOD, *, extra=(), body="hello", **kw) -> str:
    """A raw message as the bridge serves it: its header FIRST, then the rest."""
    raw = rfc822(extra=extra, body=body, **kw)
    return f"{header_line}\r\n{raw}" if header_line is not None else raw


def parse(raw: str, mailbox: str = "tuta") -> mailindex.Header:
    parsed = mailindex.from_rfc822(raw, provider_id="tuta:1:INBOX", in_sent_folder=False,
                                   mailbox=mailbox, first_header=mailgate.TUTA_BRIDGE_HEADER)
    assert parsed is not None
    return parsed.header


def auth_of(raw: str) -> mailgate.Auth:
    return mailgate.authenticate_header(parse(raw))


class TestTheBridgeHeader:
    def test_the_bridges_own_header_authenticates(self):
        auth = auth_of(bridged(frm="shop@shop.example", message_id="a@shop.example"))
        assert auth.authenticated is True
        assert auth.gateway == "tuta" and auth.read_from == "bridge-header"
        assert auth.verdict == mailgate.TUTA_AUTHENTICATED

    def test_a_folded_header_is_still_the_first_one(self):
        folded = ("X-Tutabridge-Authentication: status=authenticated;\r\n"
                  "\tstatus-source=tuta-server; encryption-auth=none")
        assert auth_of(bridged(folded, frm="a@x.example")).authenticated is True

    @pytest.mark.parametrize("status", ["hard_fail", "soft_fail", "invalid_mail_from",
                                        "missing_mail_from", "unknown"])
    def test_every_other_status_is_not_authenticated_and_says_it(self, status):
        line = f"X-Tutabridge-Authentication: status={status}; status-source=tuta-server"
        auth = auth_of(bridged(line, frm="a@x.example"))
        assert auth.authenticated is False
        assert f"status={status}" in auth.why

    def test_no_header_is_not_authenticated(self):
        auth = auth_of(bridged(None, frm="a@x.example"))
        assert auth.authenticated is False
        assert "no X-Tutabridge-Authentication header" in auth.why


class TestForgeries:
    """Every shape a sender can write is not the bridge's header."""

    def test_the_header_not_first(self):
        # A sender-written copy below the first line, the bridge's own absent.
        raw = rfc822(frm="a@x.example", extra=(GOOD,)).replace(GOOD + "\r\n", "", 1)
        raw = raw.replace("Subject:", GOOD + "\r\nSubject:", 1)
        auth = auth_of(raw)
        assert auth.authenticated is False
        assert "not the first header line" in auth.why

    def test_two_copies_even_with_the_first_one_genuine(self):
        auth = auth_of(bridged(frm="a@x.example", extra=(GOOD,)))
        assert auth.authenticated is False
        assert "2 copies" in auth.why

    def test_two_copies_in_different_case(self):
        forged = GOOD.replace("X-Tutabridge-Authentication", "x-tutabridge-authentication")
        assert auth_of(bridged(frm="a@x.example", extra=(forged,))).authenticated is False

    def test_a_status_source_other_than_tuta_server(self):
        line = "X-Tutabridge-Authentication: status=authenticated; status-source=sender"
        auth = auth_of(bridged(line, frm="a@x.example"))
        assert auth.authenticated is False and "status-source sender" in auth.why

    def test_no_status_source_at_all(self):
        line = "X-Tutabridge-Authentication: status=authenticated"
        assert auth_of(bridged(line, frm="a@x.example")).authenticated is False

    def test_two_status_values(self):
        line = ("X-Tutabridge-Authentication: status=hard_fail; status=authenticated; "
                "status-source=tuta-server")
        assert auth_of(bridged(line, frm="a@x.example")).authenticated is False

    def test_the_header_inside_the_body(self):
        auth = auth_of(bridged(None, frm="a@x.example", body=GOOD + "\r\n"))
        assert auth.authenticated is False

    def test_a_perfect_gmail_authentication_results_header(self):
        ar = f"Authentication-Results: {PASS_AR.format(d='x.example')}"
        header = parse(bridged(ar, frm="a@x.example"))
        assert header.auth_results == ()  # still dropped at parse time on Tuta
        assert mailgate.authenticate_header(header).authenticated is False

    def test_a_header_line_only_counts_when_the_adapter_declared_it(self):
        parsed = mailindex.from_rfc822(bridged(frm="a@x.example"), provider_id="t",
                                       in_sent_folder=False, mailbox="tuta")
        assert parsed is not None and parsed.header.gateway_auth_observed == ""
        assert mailgate.authenticate_header(parsed.header).authenticated is False

    def test_a_bare_cr_cannot_fold_a_second_line_into_the_value(self):
        line = "X-Tutabridge-Authentication: status=hard_fail; status-source=tuta-server"
        raw = bridged(line, frm="a@x.example").replace(
            line + "\r\n", line + "\rX-Fake: status=authenticated\r\n", 1)
        header = parse(raw)
        assert "status=authenticated" not in header.gateway_auth
        assert mailgate.authenticate_header(header).authenticated is False

    def test_a_leading_blank_line_is_not_the_first_header(self):
        assert auth_of("\r\n" + bridged(frm="a@x.example")).authenticated is False

    def test_obsolete_name_space_colon_needs_the_parser_to_agree(self):
        line = ("X-Tutabridge-Authentication : status=authenticated; "
                "status-source=tuta-server")
        auth = auth_of(bridged(line, frm="a@x.example"))
        # Authenticated only if the email parser ALSO read it as the header.
        parsed = mailindex.email.message_from_string(
            bridged(line, frm="a@x.example"), policy=mailindex.email.policy.default)
        assert auth.authenticated is bool(parsed.get_all(mailgate.TUTA_BRIDGE_HEADER))

    def test_a_later_status_after_a_failing_one(self):
        line = ("X-Tutabridge-Authentication: status=hard_fail; encryption-auth=x; "
                "status=authenticated; status-source=tuta-server")
        assert auth_of(bridged(line, frm="a@x.example")).authenticated is False

    def test_a_quoted_semicolon_cannot_smuggle_a_status(self):
        line = ('X-Tutabridge-Authentication: status=hard_fail; '
                'encryption-auth="a; status=authenticated"; status-source=tuta-server')
        assert auth_of(bridged(line, frm="a@x.example")).authenticated is False

    def test_two_status_sources(self):
        line = ("X-Tutabridge-Authentication: status=authenticated; "
                "status-source=tuta-server; status-source=sender")
        assert auth_of(bridged(line, frm="a@x.example")).authenticated is False

    def test_lf_only_line_endings_still_authenticate(self):
        raw = bridged(frm="a@x.example").replace("\r\n", "\n")
        assert auth_of(raw).authenticated is True

    def test_another_gateway_never_reads_it(self):
        header = parse(bridged(frm="a@x.example"), mailbox="other")
        assert mailgate.authenticate_header(header).authenticated is False

    def test_an_index_file_from_before_reads_as_not_observed(self):
        header = mailindex._replace(parse(bridged(frm="a@x.example")),
                                    gateway_auth="", gateway_auth_observed="")
        auth = mailgate.authenticate_header(header)
        assert auth.authenticated is False and "indexed before" in auth.why


class TestEndToEnd:
    def test_an_authenticated_tuta_mail_opens_a_case_and_records_its_account(self, tmp_path):
        iban = "DE89 3704 0044 0532 0130 00"
        mail = bridged(frm="Lamp Shop <orders@lampshop.example>", subject="Order LS-48213",
                       body=f"Your order number LS-48213: pay 120,00 EUR to {iban}.",
                       message_id="o1@lampshop.example", at=T0 + DAY)
        run(tmp_path, [tuta(FakeHimalaya({"INBOX": [mail], "Sent": []}))])
        (record,) = list(mailverdicts.iter_store(tmp_path))
        assert record["gate"]["auth"]["authenticated"] is True
        (case,) = mailcases.CaseStore(tmp_path).cases.values()
        assert case.created_by == "inbound" and "LS-48213" in case.keys
        assert case.coordinates and case.coordinates[0]["value"] == "DE89370400440532013000"

    def test_the_same_mail_forged_mints_nothing(self, tmp_path):
        line = "X-Tutabridge-Authentication: status=hard_fail; status-source=tuta-server"
        mail = bridged(line, frm="Lamp Shop <orders@lampshop.example>",
                       subject="Order LS-48213", body="Your order number LS-48213.",
                       message_id="o1@lampshop.example", at=T0 + DAY)
        run(tmp_path, [tuta(FakeHimalaya({"INBOX": [mail], "Sent": []}))])
        assert mailcases.CaseStore(tmp_path).cases == {}

    def test_his_reply_admits_an_authenticated_tuta_sender(self, tmp_path):
        inbound = bridged(frm="Lamp Shop <orders@lampshop.example>", subject="Order LS-48213",
                          body="Your order number LS-48213 is confirmed.",
                          message_id="o1@lampshop.example", at=T0 + DAY)
        reply = rfc822(frm=f"Owner <{TUTA_OWNER}>", to="orders@lampshop.example",
                       subject="Re: Order LS-48213", body="Thanks",
                       message_id="r1@tuta.example", at=T0 + DAY + 3600,
                       extra=("In-Reply-To: <o1@lampshop.example>",))
        run(tmp_path, [tuta(FakeHimalaya({"INBOX": [inbound], "Sent": [reply]}))])
        (case,) = mailcases.CaseStore(tmp_path).cases.values()
        assert "lampshop.example" in case.admitted
