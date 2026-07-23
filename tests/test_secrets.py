"""Secrets store tests. The macOS `security` binary is mocked with an in-memory
store so tests never touch the real login Keychain.
"""

import subprocess

import pytest

from aish import secrets


@pytest.fixture
def store(tmp_path, monkeypatch):
    kc: dict[str, str] = {}

    def fake_security(args, value=None):
        cmd = args[0]
        if cmd == "add-generic-password":
            kc[args[args.index("-a") + 1]] = args[args.index("-w") + 1]
            return subprocess.CompletedProcess(args, 0, "", "")
        if cmd == "find-generic-password":
            name = args[args.index("-a") + 1]
            if name in kc:
                return subprocess.CompletedProcess(args, 0, kc[name] + "\n", "")
            return subprocess.CompletedProcess(args, 1, "", "not found")
        if cmd == "delete-generic-password":
            name = args[args.index("-a") + 1]
            existed = name in kc
            kc.pop(name, None)
            return subprocess.CompletedProcess(args, 0 if existed else 1, "", "")
        raise AssertionError(f"unexpected security call: {cmd}")

    monkeypatch.setattr(secrets, "_security", fake_security)
    monkeypatch.setattr(secrets, "NAMES_INDEX", tmp_path / "names.txt")
    return kc


class TestSecrets:
    def test_put_get_roundtrip(self, store):
        secrets.put("FASTMAIL_TOKEN", "abc123")
        assert secrets.get("FASTMAIL_TOKEN") == "abc123"

    def test_missing_is_none(self, store):
        assert secrets.get("NOPE") is None

    def test_names_index(self, store):
        secrets.put("A_TOKEN", "1")
        secrets.put("B_TOKEN", "2")
        assert secrets.names() == ["A_TOKEN", "B_TOKEN"]

    def test_delete(self, store):
        secrets.put("X", "y")
        assert secrets.delete("X") is True
        assert secrets.get("X") is None
        assert "X" not in secrets.names()

    def test_delete_absent(self, store):
        assert secrets.delete("GONE") is False

    def test_invalid_name_rejected(self, store):
        assert not secrets.valid_name("bad-name")
        assert not secrets.valid_name("1leading")
        assert secrets.valid_name("GOOD_NAME_1")
        with pytest.raises(secrets.SecretError):
            secrets.put("bad-name", "v")

    def test_value_never_in_names_index(self, store, tmp_path):
        secrets.put("TOK", "supersecret")
        assert "supersecret" not in (tmp_path / "names.txt").read_text()
