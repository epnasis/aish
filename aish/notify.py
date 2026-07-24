"""Push notifications for automated (triggered) sessions (#163).

The one job: reach the owner on their phone when an automated workflow needs a
human — a held approval a triggered session can't auto-run — or when one
finishes. Delivery is Pushover; credentials live in the Keychain (aish's own
secret store, service "aish"): PUSHOVER_TOKEN (app token) + PUSHOVER_USER (user
key). Unconfigured or failing delivery is a SILENT no-op — a notification must
NEVER break, delay, or leak into the approval path it rides alongside.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import secrets

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def configured() -> bool:
    """Whether Pushover creds are present. Cheap gate so callers can skip work."""
    return bool(secrets.get("PUSHOVER_TOKEN") and secrets.get("PUSHOVER_USER"))


def pushover(
    title: str,
    message: str,
    *,
    url: str | None = None,
    url_title: str | None = None,
    priority: int = 0,
    timeout: float = 10.0,
) -> bool:
    """Send one Pushover notification. Returns True on delivery, False on any
    failure (missing creds, network, API error) — never raises. `url` becomes a
    tappable supplementary link in the notification (the session deep-link)."""
    token = secrets.get("PUSHOVER_TOKEN")
    user = secrets.get("PUSHOVER_USER")
    if not token or not user:
        return False
    fields = {
        "token": token,
        "user": user,
        "title": title[:250],
        "message": message[:1024],
        "priority": str(priority),
    }
    if url:
        fields["url"] = url[:512]
    if url_title:
        fields["url_title"] = url_title[:100]
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(_PUSHOVER_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read() or b"{}")
        return bool(body.get("status") == 1)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return False
