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
    monkeypatch.setattr(secrets, "names_index", lambda p=tmp_path / "names.txt": p)
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


@pytest.fixture
def personal(tmp_path, monkeypatch):
    """A fake Keychain keyed on (SERVICE, account), not on account alone.

    The `store` fixture above collapses the services together, which is exactly
    the thing the third namespace must not do — a test that cannot tell
    `aish` from `aish-personal` cannot check the fence between them."""
    kc: dict[tuple[str, str], str] = {}

    def fake_security(args, value=None):
        cmd = args[0]
        key = (args[args.index("-s") + 1], args[args.index("-a") + 1])
        if cmd == "add-generic-password":
            kc[key] = args[args.index("-w") + 1]
            return subprocess.CompletedProcess(args, 0, "", "")
        if cmd == "find-generic-password":
            if key in kc:
                return subprocess.CompletedProcess(args, 0, kc[key] + "\n", "")
            return subprocess.CompletedProcess(args, 1, "", "not found")
        if cmd == "delete-generic-password":
            existed = key in kc
            kc.pop(key, None)
            return subprocess.CompletedProcess(args, 0 if existed else 1, "", "")
        raise AssertionError(f"unexpected security call: {cmd}")

    monkeypatch.setattr(secrets, "_security", fake_security)
    monkeypatch.setattr(secrets, "names_index", lambda p=tmp_path / "names.txt": p)
    monkeypatch.setattr(
        secrets, "personal_names_index", lambda p=tmp_path / "personal.txt": p
    )
    secrets._invalidate()
    secrets._invalidate_personal()
    yield kc
    secrets._invalidate()
    secrets._invalidate_personal()


class TestDeclaredPersonalValues:
    """#343 — the values aish asks before typing. Stored in a THIRD Keychain
    namespace, matched by value, and never scrubbed."""

    ADDRESS = "ul. Lipowa 3/5, 30-001 Kraków"

    def test_declare_and_read_back(self, personal):
        secrets.put_personal("home_address", self.ADDRESS)
        assert secrets.get_personal("home_address") == self.ADDRESS
        assert secrets.personal_names() == ["home_address"]
        assert secrets.personal_status("home_address") == secrets.FENCED

    def test_a_two_character_value_is_refused_at_declaration(self, personal):
        """What happens if he declares a two-character value. It is refused, he
        is told the floor and the count, and NOTHING is stored — the failure
        this prevents is a class he believes is fenced and a matcher that skips
        it, which is invisible from either end."""
        with pytest.raises(secrets.SecretError) as raised:
            secrets.put_personal("initials", "PW")
        assert "too short to fence" in str(raised.value)
        assert "at least 6" in str(raised.value)
        assert "has 2" in str(raised.value)
        assert secrets.personal_names() == []
        assert secrets.get_personal("initials") is None

    def test_a_short_value_that_reached_the_store_anyway_is_not_a_wildcard(
        self, personal
    ):
        """Defence in depth, because `put_personal` is not the only way bytes
        can land in a Keychain item. The matcher skips it, and the listing says
        so rather than claiming a coverage it does not have."""
        secrets._security([
            "add-generic-password", "-a", "initials",
            "-s", secrets.PERSONAL_SERVICE, "-U", "-w", "PW",
        ])
        secrets._index_add("initials", secrets.personal_names_index())
        secrets._invalidate_personal()
        assert secrets.personal_names() == ["initials"]
        assert secrets.personal_status("initials") == secrets.TOO_SHORT
        assert secrets.personal_matches("okulary PW") == []

    def test_a_declared_value_is_unreachable_through_the_secret_store(self, personal):
        """The namespace separation is a FENCE, exactly as `aish-signin` is.
        Two things resolve a name against SERVICE — a plugin manifest's
        `secrets:` field and `aish secret get` — and his home address must be
        reachable by neither, or the value this whole slice keeps off the wire
        would be injectable into a wrapper's environment by naming it."""
        secrets.put_personal("home_address", self.ADDRESS)
        assert secrets.get("home_address") is None
        assert secrets.names() == []

    def test_a_declared_value_is_never_scrubbed(self, personal):
        """Deliberately NOT in the scrub set. `scrub` is right for a credential
        and wrong for his own name: his name is all over the pages he asks aish
        to read, and redacting it would corrupt the answer rather than protect
        anything."""
        secrets.put_personal("full_name", "Jan Kowalski")
        page = "Zamówienie dla Jan Kowalski"
        assert secrets.scrub(page) == page
        assert secrets.contains(page) is False

    def test_matching_normalises_case_spacing_and_diacritics(self, personal):
        secrets.put_personal("home_address", self.ADDRESS)
        secrets.put_personal("phone", "+48 601 234 567")
        for written in (
            "UL. LIPOWA 3/5, 30-001 KRAKOW",
            "ul  Lipowa 3 5 30 001 Kraków",
            "wysyłka: ul. Lipowa 3/5, 30-001 Krakow, PL",
        ):
            assert "home_address" in secrets.personal_matches(written), written
        for written in ("+48601234567", "48 601-234-567", "(48) 601 234 567"):
            assert "phone" in secrets.personal_matches(written), written

    def test_an_unrelated_value_matches_nothing(self, personal):
        secrets.put_personal("home_address", self.ADDRESS)
        for written in ("okulary", "Tokio", "00159803025860486011", ""):
            assert secrets.personal_matches(written) == [], written

    def test_a_value_changed_under_the_same_name_is_matched_afresh(self, personal):
        """The name set does not move when a value is edited, so a names-only
        cache key would keep fencing the old address for the rest of the
        session. `put_personal` invalidates."""
        secrets.put_personal("home_address", self.ADDRESS)
        assert secrets.personal_matches(self.ADDRESS) == ["home_address"]
        secrets.put_personal("home_address", "Marszalkowska 100, Warszawa")
        assert secrets.personal_matches(self.ADDRESS) == []
        assert secrets.personal_matches("Marszalkowska 100 Warszawa") == [
            "home_address"
        ]

    def test_undeclaring(self, personal):
        secrets.put_personal("home_address", self.ADDRESS)
        assert secrets.delete_personal("home_address") is True
        assert secrets.personal_names() == []
        assert secrets.personal_matches(self.ADDRESS) == []
        assert secrets.delete_personal("home_address") is False

    def test_an_invalid_name_is_refused(self, personal):
        with pytest.raises(secrets.SecretError):
            secrets.put_personal("home address", self.ADDRESS)
        assert secrets.personal_names() == []


class TestThePersonalCommand:
    """`aish personal <set|get|list|rm>` — the same command surface as
    `aish secret`, one namespace over. Driven through the real entry point,
    because a store with no way in is a fence nobody can arm."""

    ADDRESS = "ul. Lipowa 3/5, 30-001 Kraków"

    def _typed(self, monkeypatch, *entries):
        """`_personal_cli` imports getpass locally, so the MODULE is what has
        to be patched — patching an attribute on `cli` would be patching
        something the command never reads."""
        import getpass

        from aish import cli

        answers = list(entries)
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": answers.pop(0))
        return cli

    def test_set_list_get_rm(self, personal, monkeypatch, capsys):
        cli = self._typed(monkeypatch, self.ADDRESS, self.ADDRESS)
        assert cli._personal_cli(["set", "home_address"]) == 0
        assert "declared home_address" in capsys.readouterr().out

        assert cli._personal_cli(["list"]) == 0
        assert capsys.readouterr().out.strip() == "home_address"

        assert cli._personal_cli(["get", "home_address"]) == 0
        assert capsys.readouterr().out.strip() == self.ADDRESS

        assert cli._personal_cli(["rm", "home_address"]) == 0
        assert cli._personal_cli(["list"]) == 0
        assert "(no values declared)" in capsys.readouterr().out

    def test_a_mistyped_confirmation_stores_nothing(
        self, personal, monkeypatch, capsys
    ):
        """A mistyped API token fails loudly the first time it is used; a
        mistyped home address fails SILENTLY — the fence simply never matches
        and he is left believing a class is covered. The confirmation is what
        turns that into an error he sees."""
        cli = self._typed(monkeypatch, self.ADDRESS, "ul. Lipowa 3/6")
        assert cli._personal_cli(["set", "home_address"]) == 1
        assert "the two entries differ" in capsys.readouterr().out
        assert secrets.personal_names() == []

    def test_a_too_short_value_is_reported_not_stored(
        self, personal, monkeypatch, capsys
    ):
        cli = self._typed(monkeypatch, "PW", "PW")
        assert cli._personal_cli(["set", "initials"]) == 1
        assert "too short to fence" in capsys.readouterr().out
        assert secrets.personal_names() == []

    def test_the_listing_marks_a_class_the_matcher_skips(
        self, personal, monkeypatch, capsys
    ):
        """The listing must not claim coverage the matcher does not give."""
        from aish import cli

        secrets._security([
            "add-generic-password", "-a", "initials",
            "-s", secrets.PERSONAL_SERVICE, "-U", "-w", "PW",
        ])
        secrets._index_add("initials", secrets.personal_names_index())
        secrets._invalidate_personal()
        assert cli._personal_cli(["list"]) == 0
        assert "NOT fenced" in capsys.readouterr().out
