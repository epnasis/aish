"""#280: sign-ins aish may re-establish by itself.

Nothing here touches the real Keychain — the store is stubbed with an in-memory
jar, on the same reasoning that keeps the rest of the suite away from the live
credential store and the real notifier.
"""

import pytest

from aish import approval, signin


@pytest.fixture(autouse=True)
def jar(tmp_path, monkeypatch):
    store: dict[str, tuple[str, str]] = {}
    monkeypatch.setattr(signin, "STATE", tmp_path / "signins.json")
    monkeypatch.setattr(
        signin.secrets, "put_signin",
        lambda origin, ident, pw: store.__setitem__(origin, (ident, pw)),
    )
    monkeypatch.setattr(signin.secrets, "get_signin", store.get)
    monkeypatch.setattr(
        signin.secrets, "delete_signin",
        lambda origin: store.pop(origin, None) is not None,
    )
    return store


class TestAnOriginIsMatchedExactly:
    """The whole safety property is that a credential belongs to ONE origin.
    `browser.is_logged_in` matches on a dot boundary so a login at eon.pl also
    covers its subdomains — deliberately lenient, because gating too much is
    its safe direction. Reusing that here would fire the credential at any
    subdomain an attacker can raise."""

    def test_the_scheme_the_host_and_the_port_all_count(self):
        assert signin.origin_of("https://eon.pl/mojeon/Logowanie") == "https://eon.pl"
        assert signin.origin_of("https://EON.PL/x") == "https://eon.pl"
        assert signin.origin_of("https://eon.pl:443/x") == "https://eon.pl"
        assert signin.origin_of("http://eon.pl/x") == "http://eon.pl"
        assert signin.origin_of("https://eon.pl:8443/x") == "https://eon.pl:8443"

    def test_anything_that_is_not_a_web_origin_is_refused(self):
        for bad in ("", "eon.pl", "file:///etc/passwd", "javascript:alert(1)",
                    "about:blank", "data:text/html,x"):
            assert signin.origin_of(bad) == "", bad

    def test_a_subdomain_is_a_different_account(self):
        signin.save("https://eon.pl/login", "him", "pw", today="2026-08-22")
        assert signin.find("https://eon.pl/faktury") is not None
        assert signin.find("https://evil.eon.pl/login") is None
        assert signin.find("https://eon.pl.evil.test/login") is None

    def test_http_is_not_https(self):
        signin.save("https://eon.pl/login", "him", "pw", today="2026-08-22")
        assert signin.find("http://eon.pl/login") is None


class TestWhatIsStoredAndWhatIsNot:
    def test_the_login_url_is_kept_because_it_is_the_fence(self):
        """Without it the replay rule degrades to 'type the credential wherever
        a password field appears on this host', so any injected form on the
        origin harvests it."""
        record = signin.save(
            "https://eon.pl/mojeon/Logowanie", "him@x.pl", "pw", today="2026-08-22"
        )
        assert record.url == "https://eon.pl/mojeon/Logowanie"
        assert signin.find("https://eon.pl/").url == "https://eon.pl/mojeon/Logowanie"

    def test_no_secret_reaches_the_metadata_file(self, tmp_path):
        signin.save("https://eon.pl/login", "him@x.pl", "hunter2hunter2", today="d")
        written = (tmp_path / "signins.json").read_text(encoding="utf-8")
        assert "hunter2hunter2" not in written
        assert "him@x.pl" not in written
        assert "https://eon.pl" in written  # the map itself is not a secret

    def test_saving_twice_replaces_rather_than_duplicates(self):
        signin.save("https://eon.pl/login", "a", "one", today="d1")
        signin.save("https://eon.pl/login2", "b", "two", today="d2")
        assert len(signin.records()) == 1
        assert signin.find("https://eon.pl/x").url == "https://eon.pl/login2"
        assert signin.credential("https://eon.pl") == ("b", "two")

    def test_an_empty_password_is_never_stored(self):
        with pytest.raises(signin.secrets.SecretError):
            signin.save("https://eon.pl/login", "him", "", today="d")
        assert signin.records() == []

    def test_forgetting_takes_the_credential_and_the_record(self, jar):
        signin.save("https://eon.pl/login", "him", "pw", today="d")
        assert signin.forget("https://eon.pl") is True
        assert signin.records() == []
        assert jar == {}
        assert signin.forget("https://eon.pl") is False


class TestOneAttemptNeverARetry:
    """Retrying a wrong password is how accounts lock, and unattended it locks
    them silently."""

    def test_a_failed_attempt_stops_the_credential_being_spent_again(self):
        signin.save("https://eon.pl/login", "him", "pw", today="d")
        assert signin.credential("https://eon.pl") == ("him", "pw")
        signin.note_failed("https://eon.pl", why="the site did not accept it")
        assert signin.credential("https://eon.pl") is None
        assert signin.find("https://eon.pl").suspect

    def test_the_record_survives_so_the_settings_row_can_explain_itself(self):
        signin.save("https://eon.pl/login", "him", "pw", today="d")
        signin.note_failed("https://eon.pl", why="rejected")
        assert len(signin.records()) == 1

    def test_a_fresh_capture_clears_the_suspicion(self):
        signin.save("https://eon.pl/login", "him", "old", today="d")
        signin.note_failed("https://eon.pl", why="rejected")
        signin.save("https://eon.pl/login", "him", "new", today="d2")
        assert signin.credential("https://eon.pl") == ("him", "new")

    def test_a_successful_use_is_counted_and_clears_suspicion(self):
        signin.save("https://eon.pl/login", "him", "pw", today="d")
        signin.note_used("https://eon.pl", when="2026-08-22T14:02")
        record = signin.find("https://eon.pl")
        assert record.used == 1 and record.last_used == "2026-08-22T14:02"
        assert record.suspect == ""


class TestTheKeychainIsNotReadableThroughTheShell:
    """A model that can propose `security find-generic-password -s aish -w` can
    read every password aish holds, on a card the owner has said he will tap
    through. Unapprovable, and seen through the wrappers the denylist already
    unwraps."""

    def test_both_namespaces_are_refused_however_they_are_wrapped(self):
        for command in (
            "security find-generic-password -a eon -s aish -w",
            "security find-generic-password -s aish-signin -a https://eon.pl -w",
            'sh -c "security find-generic-password -s aish -w"',
            "env X=1 security find-generic-password -s aish-signin -w",
            "echo hi && security find-generic-password -s aish -w",
        ):
            assert approval.check_denied(command), command

    def test_ordinary_security_use_and_the_bare_word_are_untouched(self):
        for command in (
            "security list-keychains",
            "grep -r aish ~/notes",
            "echo aish-signin",
        ):
            assert approval.check_denied(command) is None, command


class TestTheStoreNeverThrowsIntoARead:
    def test_a_missing_or_corrupt_state_file_reads_as_empty(self, tmp_path):
        assert signin.records() == []
        (tmp_path / "signins.json").write_text("{not json", encoding="utf-8")
        assert signin.records() == []
        assert signin.find("https://eon.pl") is None

    def test_a_row_missing_its_origin_or_url_is_dropped(self, tmp_path):
        (tmp_path / "signins.json").write_text(
            '[{"origin": "https://a.test"}, {"url": "https://b.test/l"},'
            ' {"origin": "https://c.test", "url": "https://c.test/l"}]',
            encoding="utf-8",
        )
        assert [r.origin for r in signin.records()] == ["https://c.test"]


class TestTheReplayItself:
    """`_sign_in_on` drives a page with no browser behind it — the checks that
    decide whether a credential is typed at all are pure, and they are the ones
    worth pinning hardest."""

    class FakePage:
        def __init__(self, url, form, *, after=None, has_password_after=False,
                     wants_code=False, changed=""):
            self.url = url
            self._form = form
            self._after = after or {}
            self._has_password_after = has_password_after
            self._wants_code = wants_code
            self._changed = changed
            self.typed: list[str] = []
            self.submitted = False
            self.evaluated = 0

        async def evaluate(self, script, *args):
            from aish import browser

            if script is browser.SIGNIN_FORM_JS:
                self.evaluated += 1
                # The real JS decides this itself, against the expected origin
                # it is handed. The fake honours the same contract.
                expected = args[0] if args else None
                where = self._form.get("page_origin")
                if self._form.get("ok") and where != expected:
                    return {"ok": False, "why": f"the page moved to {where}"}
                if self._form.get("ok") and self._form.get("posts_to") != expected:
                    return {"ok": False, "why": f"the form sends to {self._form.get('posts_to')}"}
                return self._form
            if script is browser.SIGNIN_STILL_OURS_JS:
                return self._changed
            if script is browser.SECOND_FACTOR_JS:
                return self._wants_code
            return None

        async def query_selector(self, selector):
            return _FakeElement(self, selector)

        async def wait_for_load_state(self, *a, **kw):
            self.url = self._after.get("url", self.url)

        async def wait_for_timeout(self, _ms):
            return None

        class _Keyboard:
            def __init__(self, page):
                self.page = page

            async def press(self, _key):
                return None

            async def type(self, text, delay=0):
                self.page.typed.append(text)

        @property
        def keyboard(self):
            return self._Keyboard(self)

    def _page(self, **kw):
        return self.FakePage(**kw)

    def _run(self, page, record=None, ident="him@x.pl", password="hunter2hunter2"):
        import asyncio

        from aish import browser
        from aish import signin as signin_mod

        record = record or signin_mod.Record(
            origin="https://eon.pl", url="https://eon.pl/login", saved="d"
        )
        original = browser._has_password_field
        browser._has_password_field = _fake_has_password(page)
        try:
            return asyncio.run(browser._sign_in_on(page, record, ident, password))
        finally:
            browser._has_password_field = original

    OK_FORM = {
        "ok": True, "posts_to": "https://eon.pl", "page_origin": "https://eon.pl",
        "identifier": True, "submit": True,
    }

    def test_a_form_that_posts_elsewhere_never_gets_the_password(self):
        """The hole an earlier draft had. Page origin says nothing about where
        the form SENDS, so any same-origin page that can render markup could
        carry <form action="https://evil/collect"> and be handed the live
        credential."""
        page = self._page(
            url="https://eon.pl/login",
            form={**self.OK_FORM, "posts_to": "https://evil.test"},
        )
        result = self._run(page)
        assert not result.ok
        assert "evil.test" in result.why
        assert "the exact origin it was saved for" in result.why
        assert page.typed == []

    def test_a_redirect_to_another_origin_stops_before_the_form_is_read(self):
        page = self._page(url="https://accounts.google.com/x", form=self.OK_FORM)
        result = self._run(page)
        assert not result.ok and page.evaluated == 0 and page.typed == []
        assert "the exact origin it was saved for" in result.why

    def test_a_get_form_is_refused(self):
        """A GET login puts the password in the query string, and the recents
        file then records that URL in cleartext, outside every scrub there is."""
        page = self._page(
            url="https://eon.pl/login",
            form={"ok": False, "why": "the form is not a POST"},
        )
        result = self._run(page)
        assert not result.ok and page.typed == []

    def test_two_password_fields_is_refused(self):
        page = self._page(
            url="https://eon.pl/login",
            form={"ok": False, "why": "more than one password field"},
        )
        assert not self._run(page).ok

    def test_a_good_form_is_filled_and_submitted(self):
        page = self._page(url="https://eon.pl/login", form=self.OK_FORM,
                          after={"url": "https://eon.pl/mojeon"})
        result = self._run(page)
        assert result.ok and not result.stale and not result.second_factor
        assert page.typed == ["him@x.pl", "hunter2hunter2"]

    def test_the_form_coming_back_means_stale_never_a_retry(self):
        page = self._page(url="https://eon.pl/login", form=self.OK_FORM,
                          has_password_after=True)
        result = self._run(page)
        assert not result.ok and result.stale
        assert "stale" in result.why

    def test_a_second_factor_is_not_a_failure(self):
        """The password was almost certainly right; recording it as stale would
        burn a good credential and send him back to a full sign-in."""
        page = self._page(url="https://eon.pl/login", form=self.OK_FORM,
                          wants_code=True)
        result = self._run(page)
        assert result.second_factor and not result.stale and not result.ok


class _FakeElement:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    async def click(self, **_kw):
        if "submit" in self.selector:
            self.page.submitted = True


def _fake_has_password(page):
    async def check(_page):
        return page._has_password_after

    return check


class TestACredentialIsOnlyEverTypedAtItsOwnOrigin:
    """The question this subsystem lives or dies on. Every check below was a
    real hole at some point in this design, and the last two were found by
    asking it again after it shipped."""

    def _run(self, page, origin="https://eon.pl"):
        import asyncio

        from aish import browser
        from aish import signin as signin_mod

        record = signin_mod.Record(
            origin=origin, url=f"{origin}/login", saved="d"
        )
        original = browser._has_password_field
        browser._has_password_field = _fake_has_password(page)
        try:
            return asyncio.run(
                browser._sign_in_on(page, record, "him@x.pl", "hunter2hunter2")
            )
        finally:
            browser._has_password_field = original

    def _page(self, **kw):
        return TestTheReplayItself.FakePage(**kw)

    OK = {
        "ok": True, "posts_to": "https://eon.pl", "page_origin": "https://eon.pl",
        "identifier": True, "submit": True,
    }

    def test_the_page_moving_between_the_check_and_the_tagging_is_caught(self):
        """The check used to run in Python against page.url, and the TAGGING
        ran a round trip later. A page that navigated in between got its fields
        tagged and filled — and the form check compared the attacker's page to
        the attacker's own form, so it passed. The origin test now runs in the
        same evaluate that tags."""
        page = self._page(
            url="https://eon.pl/login",
            form={**self.OK, "page_origin": "https://evil.test",
                  "posts_to": "https://evil.test"},
        )
        result = self._run(page)
        assert not result.ok and page.typed == []
        assert "evil.test" in result.why
        assert "https://eon.pl, the exact origin it was saved for" in result.why

    def test_the_form_rewriting_its_destination_mid_fill_is_caught(self):
        """The tag survives a SAME-DOCUMENT change, so a page that rewrote
        form.action after it was checked would be submitted to the new
        destination with the credential already in it. Nothing is SENT by
        typing, so refusing here costs an unsent form."""
        page = self._page(
            url="https://eon.pl/login", form=self.OK,
            changed="the form now sends to https://evil.test",
        )
        result = self._run(page)
        assert not result.ok and not page.submitted
        assert "evil.test" in result.why and "nothing was sent" in result.why

    def test_the_password_field_vanishing_mid_fill_is_caught(self):
        page = self._page(
            url="https://eon.pl/login", form=self.OK,
            changed="the password field is gone",
        )
        assert not self._run(page).ok

    def test_a_subdomain_is_not_the_origin(self):
        page = self._page(
            url="https://login.eon.pl/x",
            form={**self.OK, "page_origin": "https://login.eon.pl",
                  "posts_to": "https://login.eon.pl"},
        )
        assert not self._run(page).ok
        assert page.typed == []

    def test_http_is_not_https(self):
        page = self._page(
            url="http://eon.pl/login",
            form={**self.OK, "page_origin": "http://eon.pl",
                  "posts_to": "http://eon.pl"},
        )
        assert not self._run(page).ok and page.typed == []

    def test_a_same_origin_page_posting_off_origin_is_caught(self):
        """The original hole: page origin says nothing about where the form
        SENDS, so any same-origin path that can render markup could carry
        <form action=evil> and be handed the live password."""
        page = self._page(
            url="https://eon.pl/user-content/x",
            form={**self.OK, "posts_to": "https://evil.test"},
        )
        result = self._run(page)
        assert not result.ok and page.typed == []
        assert "evil.test" in result.why

    def test_the_good_case_still_signs_in(self):
        page = self._page(url="https://eon.pl/login", form=self.OK)
        result = self._run(page)
        assert result.ok and page.typed == ["him@x.pl", "hunter2hunter2"]
        assert page.submitted

    def test_the_credential_is_fetched_for_the_recorded_origin_not_the_asked_url(
        self, monkeypatch
    ):
        """The model chooses the URL that TRIGGERS a renewal; it never chooses
        where the credential goes. sign_in navigates to the recorded login
        page, and asks the store for the recorded origin."""
        from aish import browser

        signin.save("https://eon.pl/mojeon/Logowanie", "him", "pw", today="d")
        asked: list = []
        monkeypatch.setattr(
            browser.signin_mod, "credential",
            lambda origin: asked.append(origin) or ("him", "pw"),
        )
        monkeypatch.setattr(browser, "_submit", lambda job, timeout: browser.SignInResult(ok=True))
        browser.sign_in("https://eon.pl/faktury?id=7")
        assert asked == ["https://eon.pl"]

    def test_a_url_with_no_stored_sign_in_never_reaches_the_store(self, monkeypatch):
        from aish import browser

        signin.save("https://eon.pl/login", "him", "pw", today="d")
        assert browser.sign_in("https://linkedin.com/feed") is None
