"""Synthetic mail for the inbox-assistant tests (#142 W5 S1).

Every sender, order, amount and account number here is INVENTED — the
no-mined-fixtures law (`docs/replay.md`) binds the mail tests exactly as it
binds replay fixtures. The account numbers are the published example IBANs,
valid by checksum and belonging to nobody.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from aish import roles

OWNER = "owner@example.org"
DAY = 86400.0
T0 = 1_780_000_000.0  # a fixed instant; tests never read the wall clock

PASS_AR = (
    "mx.google.com; dkim=pass header.i=@{d}; spf=pass smtp.mailfrom={d}; dmarc=pass header.from={d}"
)
FAIL_AR = (
    "mx.google.com; dkim=fail header.i=@{d}; spf=fail smtp.mailfrom={d}; dmarc=fail header.from={d}"
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail(
    gid: str,
    *,
    frm: str,
    to: str = OWNER,
    subject: str = "",
    body: str = "",
    at: float = T0,
    message_id: str | None = "",
    in_reply_to: str = "",
    references: str = "",
    labels: tuple[str, ...] = ("INBOX", "UNREAD"),
    auth: str | None = "pass",
    extra_ar: tuple[str, ...] = (),
    list_id: str = "",
    list_unsubscribe: str = "",
    html: str = "",
    attachments: tuple[str, ...] = (),
) -> dict:
    """One `gws gmail users messages get --format full` response."""
    domain = frm.rsplit("@", 1)[-1].strip(">")
    headers = []
    if auth == "pass":
        headers.append({"name": "Authentication-Results", "value": PASS_AR.format(d=domain)})
    elif auth == "fail":
        headers.append({"name": "Authentication-Results", "value": FAIL_AR.format(d=domain)})
    elif auth:
        headers.append({"name": "Authentication-Results", "value": auth})
    headers += [{"name": "Authentication-Results", "value": v} for v in extra_ar]
    headers += [
        {"name": "From", "value": frm},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Mon, 1 Jan 2026 10:00:00 +0000"},
    ]
    if message_id is not None:
        headers.append({"name": "Message-ID", "value": f"<{message_id or gid + '@mail.example'}>"})
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": f"<{in_reply_to}>"})
    if references:
        headers.append({"name": "References", "value": references})
    if list_id:
        headers.append({"name": "List-Id", "value": list_id})
    if list_unsubscribe:
        headers.append({"name": "List-Unsubscribe", "value": list_unsubscribe})
    parts = [{"mimeType": "text/plain", "body": {"data": _b64(body)}}]
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    for name in attachments:
        parts.append({"mimeType": "application/octet-stream", "filename": name, "body": {}})
    return {
        "id": gid,
        "threadId": "provider-thread-" + gid,
        "labelIds": list(labels),
        "internalDate": str(int(at * 1000)),
        "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts},
    }


class FakeMailbox:
    """The read-only mailbox seam, over a list of Gmail-shaped messages."""

    def __init__(self, messages: list[dict], *, fail_listing: bool = False) -> None:
        self.messages = {m["id"]: m for m in messages}
        self.fail_listing = fail_listing
        self.calls: list[tuple] = []

    def list(self, query: str, limit: int, include_trash: bool = False):
        self.calls.append(("list", query, include_trash))
        if self.fail_listing:
            return None
        out = []
        for gid, msg in self.messages.items():
            labels = set(msg["labelIds"])
            if not include_trash and labels & {"TRASH", "SPAM"}:
                continue
            if query.startswith("in:inbox") and "INBOX" not in labels:
                continue
            if query.startswith("in:sent") and "SENT" not in labels:
                continue
            out.append(gid)
        return out[:limit]

    def get(self, gid: str):
        self.calls.append(("get", gid))
        msg = self.messages.get(gid)
        return json.loads(json.dumps(msg)) if msg is not None else None


def verdict_json(**overrides) -> str:
    answer = {
        "disposition": "surface",
        "wanted": "nothing",
        "deadline": "none",
        "case": "no_case",
        "sender_case": "consistent",
        "suspicion": "none",
        "payment_coordinates": [],
        "citations": ["m1"],
        "meaning": "A note.",
        "kind": "note",
    }
    answer.update(overrides)
    return json.dumps(answer)


class ScriptedReader:
    """The Reader's model, scripted. `replies` is a list of strings, or a
    callable taking the bundle (decoded) and returning one."""

    def __init__(self, replies) -> None:
        self.replies = replies
        self.bundles: list[dict] = []
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        user = kwargs["messages"][1]["content"]
        start = user.index(">>>\n") + 4
        end = user.rindex("\n<<<END")
        bundle = json.loads(user[start:end])
        self.bundles.append(bundle)
        if callable(self.replies):
            text = self.replies(bundle)
        else:
            text = self.replies.pop(0)
        return SimpleNamespace(
            message=SimpleNamespace(content=text, tool_calls=None),
            usage={"input": 900, "output": 120},
        )


def reader_charter() -> roles.Charter:
    return roles.load_charters()["mail-reader"]


def admit(state_dir, charter: roles.Charter, model: str) -> None:
    """Record a passing exam for `model` — what scripts/role-admission.py
    writes after a real run, written here so the load-time check is real."""
    roles.write_admission(
        state_dir,
        roles.Admission(
            charter=charter.name, version=charter.version, model=model, at="test",
            passed=len(charter.cases), total=len(charter.cases),
            charter_digest=charter.digest, cases_digest=roles.owner_cases_digest(charter),
        ),
    )
