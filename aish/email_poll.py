"""aish email trigger poller (#161), in-repo since #178 Gate 3.

Polls the bot@wenda.eu mailbox for inbox mail FROM the owner (pawel@wenda.eu /
pawel@wenda.email), verifies each is DMARC-authenticated (anti-spoofing), and
POSTs a draft-and-hold task to aish-web's /trigger endpoint — one automated
(origin=email) session per message, observable in the web UI.

Deduplication is done in Gmail itself: a triggered message is tagged with the
`aish-processed` label and the query excludes it, so this survives restarts and
never double-fires. The trigger POST additionally carries meta.dedup_key (the
Gmail message id), pairing with the server's in-process idempotency (#178
P1-10) so a retry storm within one server lifetime can't double-open a session
either. Nothing here can SEND mail — it only reads, triggers, and labels; the
triggered aish session's own approval gate governs any mutation.

A 429 from /trigger (rate limit or concurrency cap) is a FAILED trigger: the
message is NOT marked processed, so it simply retries on the next poll — that
retry contract is what makes the server's refusal safe.

Config (env):
  AISH_WEB_URL    default http://192.168.10.20:8787  (the LAN bind, not loopback)
  AISH_WEB_TOKEN  required — the /trigger gate
  AISH_POLL_MAX   default 10 — max messages handled per run

Testability: the two effectful edges — the `gws` subprocess runner and the
HTTP POST — are parameter seams (`run_poll(gws=…, post=…)`), so tests fake
both and never spawn a process or touch the network.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

ALLOWED_SENDERS = ("pawel@wenda.eu", "pawel@wenda.email")
PROCESSED_LABEL = "aish-processed"
# Deliberately NOT gated on is:unread: dedupe is the aish-processed label, so
# reading the mail on a phone before the next poll must not silently drop the
# trigger (the notification arrives long before the ~3 min poll does).
QUERY = (
    "in:inbox "
    "from:(pawel@wenda.eu OR pawel@wenda.email) "
    f"-label:{PROCESSED_LABEL}"
)

# (status, headers, body) from an HTTP POST; the injectable seam's shape.
PostResult = tuple[int, Mapping[str, str], bytes]
Gws = Callable[..., "dict | list | None"]


def log(msg: str) -> None:
    print(f"[email-poll] {msg}", file=sys.stderr, flush=True)


def read_token() -> str:
    """The /trigger token. Preferred source is the macOS Keychain (aish's own
    secret store, service "aish"), so it never lives in a plist/file — matching
    the #142 secrets design. Falls back to AISH_WEB_TOKEN env for local runs."""
    try:
        p = subprocess.run(
            ["security", "find-generic-password", "-s", "aish",
             "-a", "AISH_WEB_TOKEN", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return os.environ.get("AISH_WEB_TOKEN", "").strip()


def default_gws(args: list[str], timeout: int = 30) -> dict | list | None:
    """Run a gws command and parse its JSON stdout; None on any failure."""
    try:
        p = subprocess.run(["gws", *args], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log(f"gws {args[:3]} failed: {exc}")
        return None
    if p.returncode != 0:
        log(f"gws {args[:3]} rc={p.returncode}: {p.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def http_post(url: str, body: bytes, timeout: int = 30) -> PostResult:
    """The injectable HTTP seam: POST JSON, return (status, headers, body).
    An HTTP error status is a RESULT here (the 429 handling reads it), not an
    exception; only transport failures raise."""
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status or 200, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        body = b""
        with contextlib.suppress(Exception):
            body = exc.read() or b""
        return exc.code, dict(exc.headers or {}), body


def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def sender_ok(from_hdr: str) -> bool:
    lowered = from_hdr.lower()
    return any(addr in lowered for addr in ALLOWED_SENDERS)


def dmarc_pass(auth_results: str) -> bool:
    # Gmail evaluates DMARC (alignment of SPF/DKIM with the visible From) on
    # inbound and reports it here. dmarc=pass is the real anti-spoof signal;
    # spf=pass alone would not be (it validates the envelope, not From).
    return "dmarc=pass" in auth_results.lower()


def ensure_processed_label(gws: Gws) -> str | None:
    labels = gws(["gmail", "users", "labels", "list", "--params", '{"userId":"me"}'])
    if isinstance(labels, dict):
        for lbl in labels.get("labels", []):
            if lbl.get("name") == PROCESSED_LABEL:
                return lbl.get("id")
    created = gws(["gmail", "users", "labels", "create", "--params", '{"userId":"me"}',
                   "--json", json.dumps({"name": PROCESSED_LABEL})])
    return created.get("id") if isinstance(created, dict) else None


def mark_processed(gws: Gws, msg_id: str, label_id: str) -> None:
    gws(["gmail", "users", "messages", "modify",
         "--params", json.dumps({"userId": "me", "id": msg_id}),
         "--json", json.dumps({"addLabelIds": [label_id]})])


def trigger(base: str, token: str, prompt: str, meta: dict, title: str,
            post: Callable[..., PostResult] = http_post) -> bool:
    url = f"{base.rstrip('/')}/trigger?token={token}"
    body = json.dumps({"prompt": prompt, "origin": "email", "meta": meta,
                       "title": title}).encode()
    try:
        status, headers, raw = post(url, body, 30)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"trigger POST failed: {exc}")
        return False
    if status == 429:
        # Rate limit / concurrency cap (#178 P1-10). A FAILED trigger by
        # design: the caller must not mark the message processed, so it
        # retries on the next poll instead of being lost.
        retry_after = {k.lower(): v for k, v in headers.items()}.get("retry-after", "?")
        log(f"trigger refused (429) for {meta.get('id')} — will retry next poll "
            f"(Retry-After: {retry_after})")
        return False
    if status != 200:
        log(f"trigger POST failed: HTTP {status}")
        return False
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        data = {}
    log(f"triggered session {data.get('session')} for {meta.get('id')}")
    return True


def _bare_address(from_hdr: str) -> str:
    m = re.search(r"[\w.+-]+@[\w.-]+", from_hdr)
    return m.group(0) if m else from_hdr


def build_prompt(msg_id: str, from_hdr: str, subject: str) -> str:
    owner = _bare_address(from_hdr)
    return (
        "A new email arrived in the bot@wenda.eu mailbox from the owner "
        f"(From: {from_hdr}; Subject: {subject!r}; message id: {msg_id}).\n\n"
        "You are running as an AUTOMATED email trigger — no human is watching "
        "right now. Read the message (use gmail_search with full:true on this id, "
        "or the gws-gmail-read skill), understand what the owner wants, and act.\n\n"
        "The owner expects a real answer when he emails you. To ANSWER HIM, send a "
        f"NEW email with gmail_send addressed to={owner} (an owner address) as "
        "aish@wenda.eu — this is AUTO-APPROVED and delivered immediately; put the "
        f"subject as 'Re: {subject}' so he can follow the thread. Do NOT use a "
        "threaded reply (reply_to_msg_id) for the answer — that is held for "
        "approval because it can also reach other recipients.\n\n"
        "Capability rules in this automated session: you may read, label, save "
        f"drafts, and SEND email to the owner ({owner}) freely. Anything else — a "
        "send to a non-owner recipient, a threaded reply, trash, filter, or Drive "
        "share — is HELD for the owner to approve when he opens this session. Do the "
        "useful work now; leave anything that needs a human as a clearly-explained "
        "pending action."
    )


def run_poll(
    *,
    gws: Gws = default_gws,
    post: Callable[..., PostResult] = http_post,
    env: Mapping[str, str] | None = None,
    token: str | None = None,
) -> int:
    """One poll pass. `gws` and `post` are the effectful seams (tests fake
    both); `token` overrides the Keychain/env lookup for tests."""
    env = os.environ if env is None else env
    if token is None:
        token = read_token()
    if not token:
        log("no token (Keychain aish/AISH_WEB_TOKEN or env) — refusing to run")
        return 2
    base = env.get("AISH_WEB_URL", "http://192.168.10.20:8787")
    max_n = int(env.get("AISH_POLL_MAX", "10"))
    # Overridable for testing / tuning; the DMARC + allowed-sender checks below
    # still gate every message, so a broadened query can't bypass anti-spoofing.
    query = env.get("AISH_POLL_QUERY", QUERY)

    listing = gws(["gmail", "users", "messages", "list", "--params",
                   json.dumps({"userId": "me", "q": query, "maxResults": max_n})])
    ids = [m["id"] for m in listing.get("messages", [])] if isinstance(listing, dict) else []
    if not ids:
        return 0
    log(f"{len(ids)} candidate message(s)")

    label_id = ensure_processed_label(gws)
    if not label_id:
        log("could not ensure processed label — aborting to avoid re-fire loop")
        return 1

    for msg_id in ids:
        msg = gws(["gmail", "users", "messages", "get", "--params",
                   json.dumps({"userId": "me", "id": msg_id, "format": "metadata",
                               "metadataHeaders": ["Authentication-Results", "From", "Subject"]})])
        if not isinstance(msg, dict):
            continue
        from_hdr = header(msg, "From")
        subject = header(msg, "Subject") or "(no subject)"
        auth = header(msg, "Authentication-Results")
        if not sender_ok(from_hdr):
            log(f"skip {msg_id}: sender {from_hdr!r} not allowed")
            mark_processed(gws, msg_id, label_id)  # don't re-check forever
            continue
        if not dmarc_pass(auth):
            log(f"skip {msg_id}: DMARC not pass (possible spoof) — {from_hdr!r}")
            mark_processed(gws, msg_id, label_id)
            continue
        prompt = build_prompt(msg_id, from_hdr, subject)
        title = f"Email: {subject[:60]}"
        # dedup_key = the Gmail message id: the server's idempotency key
        # (#178 P1-10), so an in-process retry can't double-open a session.
        meta = {"id": msg_id, "from": from_hdr, "dedup_key": msg_id}
        if trigger(base, token, prompt, meta, title, post=post):
            mark_processed(gws, msg_id, label_id)  # only after a successful trigger
    return 0


def main() -> int:
    return run_poll()


if __name__ == "__main__":
    sys.exit(main())
