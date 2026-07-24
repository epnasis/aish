"""Pushover notification layer (#163). No network, no real Keychain: secrets
and urlopen are monkeypatched. The load-bearing property is that delivery is
best-effort — a missing cred or a network error is a silent False, never a
raise (a notification must never break the approval path it rides with)."""
import io
import json

import pytest

from aish import notify


@pytest.fixture
def creds(monkeypatch):
    vals = {"PUSHOVER_TOKEN": "app-tok", "PUSHOVER_USER": "usr-key"}
    monkeypatch.setattr(notify.secrets, "get", lambda name: vals.get(name))
    return vals


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_unconfigured_is_silent_false(monkeypatch):
    monkeypatch.setattr(notify.secrets, "get", lambda name: None)
    assert notify.configured() is False
    # Must not even attempt a request when creds are missing.
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("should not POST without creds"))
    assert notify.pushover("t", "m") is False


def test_success_posts_expected_fields(creds, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data.decode()
        return _Resp(json.dumps({"status": 1}).encode())

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    ok = notify.pushover("Title", "Body", url="https://x/?session=s1",
                         url_title="Open", priority=1)
    assert ok is True
    assert captured["url"] == notify._PUSHOVER_URL
    body = captured["data"]
    for fragment in ("token=app-tok", "user=usr-key", "priority=1",
                     "url_title=Open", "session%3Ds1"):
        assert fragment in body


def test_network_error_is_false_not_raise(creds, monkeypatch):
    def boom(*a, **k):
        raise notify.urllib.error.URLError("down")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.pushover("t", "m") is False  # swallowed


def test_api_reject_is_false(creds, monkeypatch):
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(json.dumps({"status": 0}).encode()))
    assert notify.pushover("t", "m") is False
