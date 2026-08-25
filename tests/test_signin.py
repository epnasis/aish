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
                     wants_code=False, changed="", captcha=(), page_text="",
                     alerts=(), alerts_after=None, invalid_after=False):
            self.url = url
            self._form = form
            self._after = after or {}
            self._has_password_after = has_password_after
            self._wants_code = wants_code
            self._changed = changed
            self._captcha = list(captcha)
            self._page_text = page_text
            self._alerts = list(alerts)
            self._alerts_after = alerts_after
            self._invalid_after = invalid_after
            self._rejection_reads = 0
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
                # The real JS checks the destination only when a <form>
                # exists; a formless login has none to declare.
                target = self._form.get("posts_to")
                if (
                    self._form.get("ok")
                    and self._form.get("form", True)
                    and target != expected
                ):
                    return {"ok": False, "why": f"the form sends to {target}"}
                return self._form
            if script is browser.SIGNIN_STILL_OURS_JS:
                return self._changed
            if script is browser.SECOND_FACTOR_JS:
                return self._wants_code
            if script is browser.CAPTCHA_MARKS_JS:
                return list(self._captcha)
            if script is browser.PAGE_TEXT_JS:
                return self._page_text
            if script is browser.SIGNIN_REJECTION_JS:
                # The real page is read once before the submit and once after,
                # and only the difference counts.
                self._rejection_reads += 1
                if self._rejection_reads == 1:
                    return {"invalid": False, "said": list(self._alerts)}
                said = self._alerts if self._alerts_after is None else self._alerts_after
                return {"invalid": self._invalid_after, "said": list(said)}
            return None

        async def query_selector(self, selector):
            # Only tags the enumeration actually SET can be found — a submit
            # button on a formless page is never tagged, so Playwright would
            # return None here and the code must fall through to Enter.
            if "submit" in selector and not self._form.get("submit", True):
                return None
            if "identifier" in selector and not self._form.get("identifier", True):
                return None
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

    def _run(self, page, record=None, ident="him@x.pl", password="hunter2hunter2",
             statuses=None):
        import asyncio

        from aish import browser
        from aish import signin as signin_mod

        record = record or signin_mod.Record(
            origin="https://eon.pl", url="https://eon.pl/login", saved="d"
        )
        original = browser._has_password_field
        browser._has_password_field = _fake_has_password(page)
        try:
            return asyncio.run(
                browser._sign_in_on(page, record, ident, password, statuses)
            )
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

    def test_the_form_coming_back_is_not_by_itself_a_stale_password(self):
        """It was, and that is #320. The form comes back for a CAPTCHA refusing
        the automation, a submit that never fired, a bot wall and an SPA that
        had not navigated yet — and calling every one of those a wrong password
        destroyed a working credential the owner then had to re-record."""
        page = self._page(url="https://eon.pl/login", form=self.OK_FORM,
                          has_password_after=True)
        result = self._run(page)
        assert not result.ok and result.tried
        assert not result.stale and not result.captcha
        assert "was never judged" in result.why

    def test_a_second_factor_is_not_a_failure(self):
        """The password was almost certainly right; recording it as stale would
        burn a good credential and send him back to a full sign-in."""
        page = self._page(url="https://eon.pl/login", form=self.OK_FORM,
                          wants_code=True)
        result = self._run(page)
        assert result.second_factor and not result.stale and not result.ok


class TestAPasswordBoxIsNotAVerdict:
    """#320. Marking a credential suspect is destructive and the owner cannot
    undo it except by re-recording — which, on the site this happened on, fails
    identically. So it now needs a POSITIVE signal that the SITE judged the
    VALUE, and the absence of success is not one."""

    _page = TestTheReplayItself._page
    _run = TestTheReplayItself._run
    FakePage = TestTheReplayItself.FakePage
    OK_FORM = TestTheReplayItself.OK_FORM

    def _failed(self, **kw):
        return self._page(
            url="https://eon.pl/login", form=self.OK_FORM,
            has_password_after=True, **kw,
        )

    def test_a_captcha_page_that_does_not_sign_in_never_blames_the_password(self):
        result = self._run(self._failed(
            captcha=["https://www.google.com/recaptcha/api.js?render=abc"],
        ))
        assert not result.ok and not result.stale
        assert result.captcha == "reCAPTCHA"
        assert "cannot sign in to this site automatically" in result.why
        assert "untouched" in result.why

    def test_the_declaration_is_matched_in_the_owners_own_language(self):
        """eon.pl says it in Polish. The brand is the only token in that
        sentence that survives translation, so it is the only one matched."""
        result = self._run(self._failed(
            page_text="Ta strona chroniona jest przez reCAPTCHA.",
        ))
        assert result.captcha == "reCAPTCHA" and not result.stale

    def test_the_other_spellings_are_recognised_too(self):
        for mark, vendor in (
            ("https://js.hcaptcha.com/1/api.js", "hCaptcha"),
            ("https://challenges.cloudflare.com/turnstile/v0/api.js",
             "Cloudflare Turnstile"),
            ("https://client-api.arkoselabs.com/v2/api.js", "Arkose Labs"),
        ):
            result = self._run(self._failed(captcha=[mark]))
            assert result.captcha == vendor, mark
            assert not result.stale

    def test_an_invisible_widget_is_seen_by_what_the_page_LOADS(self):
        """reCAPTCHA v3 renders nothing at all — the script tag is the whole
        declaration, and it is what eon.pl actually has."""
        result = self._run(self._failed(
            captcha=["https://www.gstatic.com/recaptcha/releases/x/recaptcha__pl.js"],
            page_text="Zaloguj się",
        ))
        assert result.captcha == "reCAPTCHA"

    def test_a_password_box_with_no_reason_given_does_not_set_stale(self):
        result = self._run(self._failed())
        assert not result.stale and not result.captcha and result.tried
        assert "gave no reason" in result.why

    def test_the_site_saying_no_in_its_own_markup_still_sets_stale(self):
        """An alert region that APPEARED in answer to the submit is the page's
        own statement that it judged what was typed."""
        result = self._run(self._failed(
            alerts=[], alerts_after=["Nieprawidłowy login lub hasło"],
        ))
        assert result.stale and result.tried and not result.captcha
        assert "refused the saved password" in result.why

    def test_a_field_the_page_marks_invalid_still_sets_stale(self):
        result = self._run(self._failed(invalid_after=True))
        assert result.stale

    def test_an_alert_that_was_already_showing_is_not_an_answer(self):
        """A cookie notice or a maintenance banner in a live region is not a
        verdict on a password — it was there before anything was sent."""
        result = self._run(self._failed(
            alerts=["Używamy plików cookie"],
            alerts_after=["Używamy plików cookie"],
        ))
        assert not result.stale

    def test_a_refusal_status_on_the_credential_request_still_sets_stale(self):
        assert self._run(self._failed(), statuses=[200, 401]).stale is True

    def test_a_bot_wall_status_is_not_a_rejected_password(self):
        """403 is what a bot wall answers, and reading it as a wrong password
        rebuilds the exact conflation this fixes."""
        assert self._run(self._failed(), statuses=[403, 429]).stale is False

    def test_a_captcha_outranks_a_reason_the_page_gave(self):
        """A CAPTCHA refusal often renders an error of its own. Nothing was
        learned about the password either way, so the credential survives."""
        result = self._run(self._failed(
            captcha=["https://www.google.com/recaptcha/api.js"],
            alerts_after=["Nie udało się zalogować"],
        ))
        assert result.captcha == "reCAPTCHA" and not result.stale


class TestWhatCountsAsTheSiteRefusingTheValue:
    """The pure halves, pinned where they can be read without a browser."""

    def test_a_vendor_is_found_in_an_address_a_class_or_the_text(self):
        assert signin.captcha_vendor(
            ["https://www.google.com/recaptcha/api.js"]
        ) == "reCAPTCHA"
        assert signin.captcha_vendor(["g-recaptcha  "]) == "reCAPTCHA"
        assert signin.captcha_vendor(["cf-turnstile "]) == "Cloudflare Turnstile"
        assert signin.captcha_vendor(["h-captcha "]) == "hCaptcha"
        assert signin.captcha_vendor(
            [], "Ta strona chroniona jest przez reCAPTCHA"
        ) == "reCAPTCHA"

    def test_an_ordinary_login_page_declares_nothing(self):
        assert signin.captcha_vendor(
            ["https://eon.pl/static/app.js", "https://www.google-analytics.com/g"],
            "Zaloguj się\nHasło\nNie pamiętasz hasła?",
        ) == ""

    def test_only_a_refusal_status_counts(self):
        assert signin.refused_the_credential([401]) is True
        assert signin.refused_the_credential([200, 302, 403, 429, 500]) is False
        assert signin.refused_the_credential([]) is False


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


class TestALoginPageWithNoFormAtAll:
    """The shape the first real site turned out to have. linkedin.com renders
    its login as a React app with no <form> element, and requiring one refused
    the ordinary case — while the message told him his saved sign-in had been
    rejected by a site that never saw it."""

    def _page(self, **kw):
        return TestTheReplayItself.FakePage(**kw)

    def _run(self, page):
        import asyncio

        from aish import browser
        from aish import signin as signin_mod

        record = signin_mod.Record(
            origin="https://www.linkedin.com",
            url="https://www.linkedin.com/login", saved="d",
        )
        original = browser._has_password_field
        browser._has_password_field = _fake_has_password(page)
        try:
            return asyncio.run(
                browser._sign_in_on(page, record, "him@x.pl", "hunter2hunter2")
            )
        finally:
            browser._has_password_field = original

    FORMLESS = {
        "ok": True, "posts_to": "", "page_origin": "https://www.linkedin.com",
        "identifier": False, "submit": False, "form": False,
    }

    def test_a_formless_login_signs_in(self):
        page = self._page(url="https://www.linkedin.com/login", form=self.FORMLESS)
        result = self._run(page)
        assert result.ok
        # No identifier field on a "welcome back" page: only the password.
        assert page.typed == ["hunter2hunter2"]

    def test_with_no_form_the_submit_is_enter_never_a_button(self):
        """Choosing a button by its words on a login page is how the model
        pressed 'Continue with Google'."""
        page = self._page(url="https://www.linkedin.com/login", form=self.FORMLESS)
        self._run(page)
        assert page.submitted is False  # nothing was clicked

    def test_the_origin_is_still_checked_without_a_form(self):
        page = self._page(
            url="https://www.linkedin.com/login",
            form={**self.FORMLESS, "page_origin": "https://evil.test"},
        )
        result = self._run(page)
        assert not result.ok and page.typed == []

    def test_holding_it_back_is_not_the_site_rejecting_it(self):
        """`stale` is what stops a credential being spent again. A harness
        refusal must never set it — the value is fine and the site never saw
        it."""
        page = self._page(
            url="https://www.linkedin.com/login",
            form={"ok": False, "why": "more than one password field"},
        )
        result = self._run(page)
        assert not result.ok
        assert result.stale is False


class TestTheFenceIsOnTheCredentialNotTheConnection:
    """#296. The fence used to abort every request with a body going to another
    origin, and read a non-empty blocked list as the sign-in having failed.
    Nearly every commercial login page carries a tracking pixel, so it reported
    failure on most real sites — and on the ones that legitimately post
    credentials to a sibling origin it prevented the sign-in outright.

    aish holds the value at replay time, so the only question worth asking is
    whether THIS request is carrying it. Everything else flies unexamined."""

    PASSWORD = "hunter2hunter2"

    class FakeRoute:
        def __init__(self, url, method, body, headers):
            self.request = type(
                "R", (),
                {"url": url, "method": method, "post_data": body,
                 "headers": headers or {}},
            )()
            self.aborted = False
            self.continued = False

        async def abort(self):
            self.aborted = True

        async def continue_(self):
            self.continued = True

    class FakePage:
        def __init__(self):
            self.handler = None

        async def route(self, _pattern, handler):
            self.handler = handler

    def _record(self, destinations=("https://eon.pl", "https://api.eon.pl")):
        return signin.Record(
            origin="https://eon.pl", url="https://eon.pl/login", saved="d",
            destinations=list(destinations),
        )

    def _decide(self, url, *, method="POST", body="", headers=None,
                record=None, secret=None):
        import asyncio

        from aish import browser

        page, incidents = self.FakePage(), []
        asyncio.run(
            browser._fence_the_origin(
                page, record or self._record(),
                self.PASSWORD if secret is None else secret, incidents,
            )
        )
        route = self.FakeRoute(url, method, body, headers)
        asyncio.run(page.handler(route))
        return route, incidents

    # ---- what is NOT the fence's business

    def test_a_tracking_pixel_neither_blocks_nor_is_reported(self):
        """The bug the owner hit: a google.com beacon on the eon.pl login page
        aborted the request and reported the sign-in as failed."""
        route, incidents = self._decide(
            "https://www.google-analytics.com/g/collect?v=2",
            body="en=page_view&dl=https%3A%2F%2Feon.pl%2Flogin",
        )
        assert route.continued and not route.aborted
        assert incidents == []

    def test_a_consent_call_to_a_third_party_flies(self):
        route, incidents = self._decide(
            "https://cdn.cookielaw.org/consent/log",
            body='{"consent":"accepted","domain":"eon.pl"}',
        )
        assert route.continued and incidents == []

    def test_a_cross_origin_font_is_untouched(self):
        route, incidents = self._decide("https://cdn.test/font.woff2", method="GET")
        assert route.continued and incidents == []

    # ---- where the credential may and may not go

    def test_the_credential_goes_where_his_own_sign_in_sent_it(self):
        """The other half of the bug: many sites submit credentials to an API
        subdomain, and aborting that request prevented the sign-in."""
        route, incidents = self._decide(
            "https://api.eon.pl/auth/token",
            body=f'{{"login":"him","password":"{self.PASSWORD}"}}',
        )
        assert route.continued and not route.aborted and incidents == []

    def test_the_login_origin_is_always_allowed(self):
        route, _ = self._decide(
            "https://eon.pl/mojeon/Logowanie",
            body=f"user=him&pass={self.PASSWORD}",
            record=self._record(destinations=["https://sso.eon.pl"]),
        )
        assert route.continued

    def test_the_credential_anywhere_else_is_aborted_and_is_an_incident(self):
        route, incidents = self._decide(
            "https://evil.test/collect", body=f'{{"p":"{self.PASSWORD}"}}',
        )
        assert route.aborted and not route.continued
        assert incidents == ["https://evil.test"]

    def test_a_recorded_record_gets_no_site_wide_licence(self):
        """Once the destinations are known they ARE the fence: a sibling origin
        his own sign-in never used is not one the credential may go to."""
        route, incidents = self._decide(
            "https://telemetry.eon.pl/x", body=self.PASSWORD,
            record=self._record(destinations=["https://eon.pl"]),
        )
        assert route.aborted and incidents == ["https://telemetry.eon.pl"]

    def test_only_the_origin_is_recorded_never_the_address_that_carried_it(self):
        """The incident text is pushed to his phone and written to the store,
        and a credential in a query string would ride along in it."""
        import urllib.parse

        leak = urllib.parse.quote(self.PASSWORD, safe="")
        route, incidents = self._decide(f"https://evil.test/c?p={leak}", method="GET")
        assert route.aborted
        assert incidents == ["https://evil.test"]
        assert self.PASSWORD not in incidents[0] and leak not in incidents[0]

    # ---- the migration

    def test_a_legacy_record_falls_back_to_the_registrable_domain(self):
        """Every record saved before #296 has no destinations and will not have
        any until it is re-captured. For SENDING, unlike for TYPING, the sibling
        origin is the legitimate common case — so the fallback is the site, and
        the alternative is breaking every stored sign-in aish already has."""
        route, incidents = self._decide(
            "https://api.eon.pl/auth", body=self.PASSWORD,
            record=self._record(destinations=[]),
        )
        assert route.continued and incidents == []

    def test_the_fallback_is_still_a_fence(self):
        legacy = self._record(destinations=[])
        for elsewhere in ("https://evil.test/c", "https://eon.pl.evil.test/c",
                          "http://api.eon.pl/auth"):
            route, incidents = self._decide(
                elsewhere, body=self.PASSWORD, record=legacy
            )
            assert route.aborted, elsewhere
            assert incidents and self.PASSWORD not in incidents[0]

    # ---- the encodings

    def test_the_encodings_a_real_login_form_actually_puts_on_the_wire(self):
        import base64
        import json as jsonlib
        import re
        import urllib.parse

        secret = 'pa"ss\\wo+rd żółć'
        percent = urllib.parse.quote(secret, safe="")
        spellings = {
            "raw": secret,
            "percent-encoded": percent,
            "lower-case hex": re.sub(
                r"%([0-9A-F]{2})", lambda m: "%" + m.group(1).lower(), percent
            ),
            "mixed-case hex": percent.replace("%C5", "%c5"),
            "form-encoded": urllib.parse.quote_plus(secret),
            "json-escaped": jsonlib.dumps(secret, ensure_ascii=False)[1:-1],
            "json backslash-u escaped": jsonlib.dumps(secret)[1:-1],
            "base64": base64.b64encode(secret.encode()).decode(),
            "basic auth": base64.b64encode(f"him@x.pl:{secret}".encode()).decode(),
        }
        for name, spelling in spellings.items():
            route, incidents = self._decide(
                "https://evil.test/c", body=f'{{"v":"{spelling}"}}', secret=secret
            )
            assert route.aborted, name
            assert incidents == ["https://evil.test"], name

    def test_a_basic_auth_header_counts_as_carrying_it(self):
        import base64

        token = base64.b64encode(f"him:{self.PASSWORD}".encode()).decode()
        route, incidents = self._decide(
            "https://evil.test/c", method="GET",
            headers={"authorization": f"Basic {token}"},
        )
        assert route.aborted and incidents == ["https://evil.test"]

    def test_an_ordinary_body_that_merely_names_the_field_is_not_a_match(self):
        route, incidents = self._decide(
            "https://evil.test/c", body='{"password":"","user":"him"}'
        )
        assert route.continued and incidents == []

    def test_a_request_that_will_not_answer_is_let_through(self):
        """A route that hangs is a page that hangs. Nothing here may make the
        sign-in worse than not fencing at all."""
        import asyncio

        from aish import browser

        page, incidents = self.FakePage(), []
        asyncio.run(
            browser._fence_the_origin(page, self._record(), self.PASSWORD, incidents)
        )
        route = _DeadRoute()
        asyncio.run(page.handler(route))
        assert route.continued and incidents == []


class _DeadRoute:
    class _Request:
        method = "POST"
        post_data = ""
        headers: dict = {}

        @property
        def url(self):
            raise RuntimeError("this request is gone")

    def __init__(self):
        self.request = self._Request()
        self.continued = False

    async def continue_(self):
        self.continued = True

    async def abort(self):
        raise AssertionError("a request aish cannot read is not an incident")


class TestWhereTheCredentialLegitimatelyGoes:
    """The pure half of the fence: no browser, no request, just the rule."""

    def test_the_registrable_domain_of_the_shapes_his_web_is_made_of(self):
        for host, site in (
            ("eon.pl", "eon.pl"),
            ("api.eon.pl", "eon.pl"),
            ("www.example.com", "example.com"),
            ("login.example.co.uk", "example.co.uk"),
            ("sso.bank.com.pl", "bank.com.pl"),
            ("localhost", "localhost"),
            ("", ""),
        ):
            assert signin.registrable_domain(host) == site, host

    def test_a_lookalike_domain_is_not_the_site(self):
        legacy = signin.Record(origin="https://eon.pl", url="https://eon.pl/l", saved="d")
        assert not signin.may_receive_credential(legacy, "https://eon.pl.evil.test/x")
        assert not signin.may_receive_credential(legacy, "https://eonpl.test/x")

    def test_recorded_destinations_are_matched_exactly(self):
        record = signin.Record(
            origin="https://eon.pl", url="https://eon.pl/l", saved="d",
            destinations=["https://sso.eon.pl"],
        )
        assert signin.may_receive_credential(record, "https://sso.eon.pl/token")
        assert not signin.may_receive_credential(record, "https://other.eon.pl/t")
        assert not signin.may_receive_credential(record, "http://sso.eon.pl/t")

    def test_an_unreadable_address_receives_nothing(self):
        record = signin.Record(origin="https://eon.pl", url="https://eon.pl/l", saved="d")
        for bad in ("", "about:blank", "data:text/html,x", "javascript:x"):
            assert not signin.may_receive_credential(record, bad), bad

    def test_an_empty_secret_matches_nothing(self):
        assert signin.secret_needles("") == ()
        assert not signin.carries_secret("anything at all", signin.secret_needles(""))


class TestWhereTheCredentialWentIsRecordedAndMigrates:
    def test_the_destinations_are_saved_beside_the_record(self):
        record = signin.save(
            "https://eon.pl/login", "him", "pw", today="d",
            destinations=["https://api.eon.pl/auth", "https://api.eon.pl/again", ""],
        )
        assert record.destinations == ["https://api.eon.pl"]
        assert signin.find("https://eon.pl/x").destinations == ["https://api.eon.pl"]

    def test_the_destination_list_carries_no_secret_either(self, tmp_path):
        signin.save(
            "https://eon.pl/login", "him@x.pl", "hunter2hunter2", today="d",
            destinations=["https://api.eon.pl/auth?p=hunter2hunter2"],
        )
        written = (tmp_path / "signins.json").read_text(encoding="utf-8")
        assert "hunter2hunter2" not in written

    def test_a_record_written_before_this_existed_reads_as_empty(self, tmp_path):
        (tmp_path / "signins.json").write_text(
            '[{"origin": "https://eon.pl", "url": "https://eon.pl/login"}]',
            encoding="utf-8",
        )
        record = signin.find("https://eon.pl/x")
        assert record.destinations == []
        assert signin.may_receive_credential(record, "https://api.eon.pl/auth")

    def test_a_corrupt_destination_list_costs_the_list_not_the_record(self, tmp_path):
        (tmp_path / "signins.json").write_text(
            '[{"origin": "https://eon.pl", "url": "https://eon.pl/login",'
            ' "destinations": "not-a-list"}]',
            encoding="utf-8",
        )
        assert signin.find("https://eon.pl/x").destinations == []

    def test_the_watcher_records_only_where_the_password_actually_went(self):
        from aish import browser

        owner = _FakeOwner(_WatchedPage())
        browser._hold_credential(owner, "https://eon.pl/login", "hunter2hunter2", True)
        for url, body in (
            ("https://www.google-analytics.com/collect", "en=page_view"),
            ("https://api.eon.pl/auth", '{"p":"hunter2hunter2"}'),
            ("https://eon.pl/login", "pass=hunter2hunter2"),
            ("https://api.eon.pl/auth", '{"p":"hunter2hunter2"}'),
        ):
            owner.view.fire(url, body)
        assert owner.pending_credential["destinations"] == [
            "https://api.eon.pl", "https://eon.pl",
        ]

    def test_nothing_is_watched_until_he_asks_for_it_to_be_saved(self):
        from aish import browser

        owner = _FakeOwner(_WatchedPage())
        browser._hold_credential(owner, "https://eon.pl/login", "hunter2hunter2", False)
        assert owner.view.handlers == []

    def test_clearing_the_held_credential_disarms_the_watcher(self):
        from aish import browser

        owner = _FakeOwner(_WatchedPage())
        browser._hold_credential(owner, "https://eon.pl/login", "hunter2hunter2", True)
        owner.pending_credential = {}
        owner.view.fire("https://evil.test/c", "hunter2hunter2")
        assert owner.pending_credential == {}


class _WatchedPage:
    def __init__(self):
        self.handlers = []

    def on(self, event, handler):
        assert event == "request"
        self.handlers.append(handler)

    def fire(self, url, body):
        request = type(
            "R", (), {"url": url, "method": "POST", "post_data": body, "headers": {}}
        )()
        for handler in self.handlers:
            handler(request)


class _FakeOwner:
    def __init__(self, page):
        self.view = page
        self.pending_credential: dict = {}
        self.credential_watch = None


class TestASignInIsJudgedByWhetherTheSessionCameUp:
    """The third defect, and the one the owner actually saw: a blocked beacon
    converted a working sign-in into a reported failure, so aish had him signed
    in and told him to go and do it by hand."""

    def _drive(self, monkeypatch, beacons=(), pushes=None, page=None):
        import asyncio

        from aish import browser

        pushes = [] if pushes is None else pushes
        signin.save("https://eon.pl/login", "him", "hunter2hunter2", today="d")
        page = page or _SignInPage(beacons)
        owner = _SignInOwner(page)
        monkeypatch.setattr(browser, "_has_password_field", _fake_has_password(page))
        monkeypatch.setattr(browser, "notify", _SilentNotifier(pushes))
        monkeypatch.setattr(
            browser, "_submit", lambda job, timeout: asyncio.run(job(owner))
        )
        return browser.sign_in("https://eon.pl/faktury"), page

    def test_a_third_party_beacon_never_turns_a_success_into_a_failure(
        self, monkeypatch
    ):
        result, page = self._drive(
            monkeypatch,
            [("https://www.google-analytics.com/collect", "en=page_view")],
            [],
        )
        assert result.ok and not result.stale and not result.why
        assert page.beacons_continued == 1
        record = signin.find("https://eon.pl")
        assert record.suspect == "" and record.used == 1

    def test_the_credential_going_elsewhere_is_an_incident_even_on_a_success(
        self, monkeypatch
    ):
        pushes: list = []
        result, page = self._drive(
            monkeypatch, [("https://evil.test/c", "p=hunter2hunter2")], pushes
        )
        # The session came up, so that is what is reported...
        assert result.ok
        assert page.beacons_continued == 0
        # ...and the credential is retired anyway, loudly.
        record = signin.find("https://eon.pl")
        assert "https://evil.test" in record.suspect and record.used == 0
        assert signin.credential("https://eon.pl") is None
        assert pushes and "evil.test" in pushes[0][1]
        assert "hunter2hunter2" not in "".join(t + b for t, b in pushes)

    def test_the_store_is_told_the_outcome_and_never_the_wire(self):
        from aish import browser

        signin.save("https://eon.pl/login", "him", "pw", today="d")
        record = signin.find("https://eon.pl")

        assert browser._record_the_outcome(
            record, browser.SignInResult(ok=True), [], when="w"
        ) == ""
        assert signin.find("https://eon.pl").used == 1

        text = browser._record_the_outcome(
            record, browser.SignInResult(ok=True), ["https://evil.test"], when="w"
        )
        assert "https://evil.test" in text
        # note_used would have cleared the very mark being set.
        assert signin.find("https://eon.pl").suspect == text
        assert signin.find("https://eon.pl").used == 1


class _SilentNotifier:
    def __init__(self, sink):
        self.sink = sink

    def pushover(self, title, body):
        self.sink.append((title, body))


class _SignInPage(TestTheReplayItself.FakePage):
    """The replay page, plus the network the real one sits on."""

    def __init__(self, beacons=(), *, answers=(), **kw):
        super().__init__(
            url=kw.pop("url", "https://eon.pl/login"),
            form=kw.pop("form", TestTheReplayItself.OK_FORM),
            after=kw.pop("after", {"url": "https://eon.pl/mojeon"}),
            **kw,
        )
        self._beacons = list(beacons)
        self._answers = list(answers)
        self._handler = None
        self._listeners: dict = {}
        self.beacons_continued = 0
        self.closed = False

    async def goto(self, *_a, **_kw):
        return None

    async def route(self, _pattern, handler):
        self._handler = handler

    def on(self, event, handler):
        self._listeners[event] = handler

    async def close(self):
        self.closed = True

    async def wait_for_timeout(self, _ms):
        # The beacons land WHILE the sign-in settles, which is the real
        # interleaving: the fence is up, the form has been sent, and the page's
        # own analytics fire alongside it.
        if self._handler is None:
            return
        for url, body in self._beacons:
            route = _SignInRoute(url, body)
            await self._handler(route)
            self.beacons_continued += int(route.continued)
        self._beacons = []
        # ...and the site's own answers come back on the same settle.
        listener = self._listeners.get("response")
        for url, body, status in self._answers if listener else []:
            listener(_SignInResponse(url, body, status))
        self._answers = []


class _SignInRoute:
    def __init__(self, url, body):
        self.request = type(
            "R", (), {"url": url, "method": "POST", "post_data": body, "headers": {}}
        )()
        self.continued = False
        self.aborted = False

    async def continue_(self):
        self.continued = True

    async def abort(self):
        self.aborted = True


class _SignInResponse:
    def __init__(self, url, body, status):
        self.request = type(
            "R", (), {"url": url, "method": "POST", "post_data": body, "headers": {}}
        )()
        self.status = status


class TestAFailedAttemptLeavesTheOwnersRecordAlone:
    """#320, at the level the damage actually happened: the STORE. He recorded
    the sign-in by hand — which only ever succeeds — and the automated replay
    then wrote onto the record that his password looked stale, so the value
    stopped being spent and the only repair offered was to record it again.
    Twice."""

    _drive = TestASignInIsJudgedByWhetherTheSessionCameUp._drive

    # What his own hand sign-in wrote, field for field. A failed automated
    # attempt must leave exactly this behind.
    AS_HE_RECORDED_IT = {
        "origin": "https://eon.pl",
        "url": "https://eon.pl/login",
        "saved": "d",
        "used": 0,
        "last_used": "",
        "suspect": "",
        "destinations": [],
    }

    def test_a_captcha_refusal_costs_him_nothing(self, monkeypatch):
        import dataclasses

        page = _SignInPage(
            has_password_after=True,
            captcha=["https://www.google.com/recaptcha/api.js?render=eon"],
            page_text="Ta strona chroniona jest przez reCAPTCHA.",
        )
        pushes: list = []
        result, _ = self._drive(monkeypatch, pushes=pushes, page=page)

        assert not result.ok and not result.stale
        assert result.captcha == "reCAPTCHA"
        # The whole point: the record he made by hand is untouched, and the
        # credential is still spendable.
        assert dataclasses.asdict(signin.find("https://eon.pl")) == self.AS_HE_RECORDED_IT
        assert signin.credential("https://eon.pl") == ("him", "hunter2hunter2")
        # And he is told the true thing, without being sent to re-record.
        assert pushes and "does not need replacing" in pushes[0][1]
        assert "reCAPTCHA" in pushes[0][1]
        assert "hunter2hunter2" not in "".join(t + b for t, b in pushes)

    def test_a_failure_the_site_gave_no_reason_for_costs_him_nothing_either(
        self, monkeypatch
    ):
        import dataclasses

        result, _ = self._drive(
            monkeypatch, page=_SignInPage(has_password_after=True)
        )
        assert not result.ok and not result.stale
        assert dataclasses.asdict(signin.find("https://eon.pl")) == self.AS_HE_RECORDED_IT
        assert signin.credential("https://eon.pl") == ("him", "hunter2hunter2")

    def test_the_site_answering_the_login_with_a_refusal_still_retires_it(
        self, monkeypatch
    ):
        """The signal that survives: the site answered the request that CARRIED
        the password with 401. Collected by the same fence that already reads
        those requests, so there is one definition of which request it was."""
        page = _SignInPage(
            has_password_after=True,
            answers=[("https://eon.pl/api/login", "p=hunter2hunter2", 401)],
        )
        result, _ = self._drive(monkeypatch, page=page)
        assert result.stale and not result.ok
        record = signin.find("https://eon.pl")
        assert record.suspect and record.used == 0
        assert signin.credential("https://eon.pl") is None

    def test_the_pages_own_words_never_reach_the_record(self, monkeypatch):
        """#296's invariant, across the new signals. A login page that echoes a
        failed password back into its own error text is a real shape, and the
        alert text is READ to decide whether the site answered — it is never
        what gets written. Only the fixed sentence is."""
        page = _SignInPage(
            has_password_after=True,
            alerts_after=["Błędne hasło: hunter2hunter2"],
            answers=[("https://eon.pl/api/login", "p=hunter2hunter2", 401)],
        )
        pushes: list = []
        result, _ = self._drive(monkeypatch, pushes=pushes, page=page)
        assert result.stale
        written = (signin.STATE).read_text(encoding="utf-8")
        assert "hunter2hunter2" not in written
        assert "hunter2hunter2" not in result.why
        assert "hunter2hunter2" not in "".join(t + b for t, b in pushes)
        assert "hunter2hunter2" not in signin.find("https://eon.pl").suspect

    def test_a_beacon_answering_401_says_nothing_about_the_password(
        self, monkeypatch
    ):
        """The status has to be on a request that carried the value. A consent
        endpoint refusing an unrelated call is not the site refusing him."""
        page = _SignInPage(
            has_password_after=True,
            answers=[("https://consent.example/x", "cmp=1", 401)],
        )
        result, _ = self._drive(monkeypatch, page=page)
        assert not result.stale
        assert signin.find("https://eon.pl").suspect == ""


class _SignInOwner:
    def __init__(self, page):
        self.view = None
        self.read_pages: set = set()
        self._page = page

    async def context(self):
        return self

    async def new_page(self):
        return self._page


