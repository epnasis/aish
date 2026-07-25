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
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import secrets

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

# Kill switch: AISH_NOTIFY=0 silences every push without touching the stored
# credentials, so notifications can be stopped (and restored) with a restart
# instead of deleting and re-entering a Keychain secret.
_OFF_VALUES = {"0", "false", "no", "off"}


def enabled() -> bool:
    return os.environ.get("AISH_NOTIFY", "1").strip().lower() not in _OFF_VALUES


def configured() -> bool:
    """Whether Pushover creds are present AND notifications are enabled. Cheap
    gate so callers can skip work."""
    return enabled() and bool(secrets.get("PUSHOVER_TOKEN") and secrets.get("PUSHOVER_USER"))


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
    if not enabled():
        return False
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
        ok = bool(body.get("status") == 1)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        _log(f"FAILED p{priority} {title!r}: {exc}")
        return False
    # Every send is logged: without this, "why did my phone buzz?" is
    # unanswerable after the fact — the push leaves no other trace.
    _log(f"{'sent' if ok else 'rejected'} p{priority} {title!r}")
    return ok


def _log(text: str) -> None:
    print(f"[notify] {text}", file=sys.stderr, flush=True)
