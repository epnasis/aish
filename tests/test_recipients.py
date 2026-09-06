"""Recipient-scoped autonomy as its own contract (#377).

`tests/test_server.py` covers the web approver's use of it and
`tests/test_cli.py` the terminal's. What can only be tested here is the reason
the module exists: the two surfaces must give the SAME answer, because the
question is what the mail can reach and not who is watching.
"""

import builtins
from pathlib import Path

import pytest

from aish import recipients
from aish import server as server_module
from aish.cli import make_tool_approver


class _FakeBridge:
    def __init__(self):
        self.asked: list = []

    def emit(self, event, record=True):
        pass

    def ask(self, request):
        request.setdefault("id", "uid-1")
        self.asked.append(request)
        return {"action": "approve"}


class _FakeLog:
    def __init__(self):
        self.records: list = []

    def command(self, command, decision, intent="", preview="", asked_by="", **timing):
        self.records.append((command, decision, intent))


def _web_cards(name, args):
    """Whether the WEB approver puts this call in front of the owner, in the
    owner's own session (origin `user`)."""
    bridge = _FakeBridge()
    approvers = server_module.make_web_approvers(
        bridge, _FakeLog(), Path("/x/allow"), Path("/x/deny"),
        ask_all=False, get_scope=lambda: (".", []),
        trust_dir=lambda p: "", get_origin=lambda: "user",
    )
    approvers[3](name, args)
    return bool(bridge.asked)


def _cli_cards(name, args, monkeypatch):
    """The same question of the TERMINAL approver. A prompt is detected by the
    approver reaching for `input`, which is the terminal's whole card."""
    asked = []
    monkeypatch.setattr(builtins, "input", lambda _p="": asked.append(_p) or "n")
    make_tool_approver(_FakeLog())(name, args)
    return bool(asked)


# Each row is (label, args) for gmail_send, with the answer BOTH surfaces owe.
_SENDS = [
    ("plain self-send", {"to": "pawel@wenda.eu", "body": "hi"}, False),
    ("other owner address", {"to": "pawel@wenda.email", "body": "hi"}, False),
    ("display-name form", {"to": "Pawel <pawel@wenda.eu>", "body": "hi"}, False),
    ("self + self on cc", {"to": "pawel@wenda.eu", "cc": "pawel@wenda.email"}, False),
    ("draft to self", {"to": "pawel@wenda.eu", "draft": True}, False),
    ("third party", {"to": "stranger@evil.com", "body": "hi"}, True),
    ("owner + third party", {"to": "pawel@wenda.eu, x@evil.com"}, True),
    ("third party on bcc", {"to": "pawel@wenda.eu", "bcc": "x@evil.com"}, True),
    ("the bot's own mailbox", {"to": "aish@wenda.eu", "body": "hi"}, True),
    ("a reply", {"reply_to_msg_id": "m1", "to": "pawel@wenda.eu"}, True),
    ("no recipient at all", {"body": "hi"}, True),
    ("adversarial quoted local-part", {"to": '"pawel@wenda.eu"@evil.com'}, True),
]


@pytest.mark.parametrize("label,args,cards", _SENDS, ids=[r[0] for r in _SENDS])
def test_both_surfaces_answer_a_send_identically(label, args, cards, monkeypatch):
    assert _web_cards("gmail_send", args) is cards, f"web disagrees on: {label}"
    assert _cli_cards("gmail_send", args, monkeypatch) is cards, f"cli: {label}"


def test_only_gmail_send_is_consequence_scoped(monkeypatch):
    """The exemption is one tool's, not a general 'mentions the owner' rule:
    trashing his mail is irreversible however it is addressed."""
    assert not recipients.owner_scoped_send("gmail_trash", {"to": "pawel@wenda.eu"})
    assert _web_cards("gmail_trash", {"message_id": "m1"})
    assert _cli_cards("gmail_trash", {"message_id": "m1"}, monkeypatch)


def test_the_bot_mailbox_is_never_an_owner_address():
    """`aish@`/`bot@` is the mailbox `email_poll` reads back in — a send there is
    aish talking to itself, and it must card. Pinned because the addresses look
    like the owner's own and the next reader will be tempted to add them."""
    for addr in ("aish@wenda.eu", "bot@wenda.eu"):
        assert addr not in recipients.OWNER_ADDRESSES
        assert not recipients.all_owner({"to": addr})
