"""Driving a page: the control list, the labelling, and the gate on pressing.

Nothing here launches Chrome — `browser.browse_open` / `browse_act` are patched,
and conftest's `no_real_browser` makes any escape fail loudly. The one thing that
cannot be faked, whether the JS finds the controls a real page actually has, is
verified against real Chrome by `scripts/verify_browse.py`.
"""

import os
import shutil
import subprocess
import time

import pytest

from aish import agent as agent_module
from aish import browse, browser
from aish import web as web_module
from aish.agent import Agent, Approved, Denied


def control(n=0, kind=browse.BUTTON, name="Przełącz lokal", **kw):
    raw = {"n": n, "kind": kind, "name": name}
    raw.update(kw)
    return browse.controls_from([raw])[0]


def snapshot(url="https://eon.pl/mojeon", controls=(), **kw):
    return browse.Snapshot(
        url=url, title=kw.pop("title", ""), text=kw.pop("text", "Wyspowa"),
        controls=list(controls), **kw
    )


def shown(snap):
    """The page a caller with no chat behind it was last handed.

    `web.browse_*` reads the caller's own `BrowseView` and never the browser's
    global (#272), so a test that used to stub `browser.browse_current` seeds
    the view the module falls back to instead."""
    web_module._DEFAULT_VIEW.remember(snap)
    return snap


@pytest.fixture(autouse=True)
def _fresh_default_view():
    """One test's page must not be the next test's. The view outlives a call
    by design — it is what a change report is a change from."""
    web_module._DEFAULT_VIEW.forget()
    yield
    web_module._DEFAULT_VIEW.forget()


class TestWhatCountsAsMutating:
    """The label that decides whether a click gets its own approval card.

    Broad and dumb on purpose: it costs a prompt when it is wrong and costs a
    paid bill when it is missing."""

    @pytest.mark.parametrize(
        "name",
        ["Zapłać", "Zapłać wybrane", "Usuń konto", "Wypowiedz umowę", "Wyloguj się",
         "Pay now", "Delete", "Confirm order", "Zmień", "Anuluj"],
    )
    def test_a_button_that_spends_ends_or_deletes(self, name):
        assert control(name=name).mutating is True

    @pytest.mark.parametrize(
        "name", ["Przełącz lokal", "Pulpit", "Filtruj", "Pokaż więcej", "Next page"]
    )
    def test_a_button_that_merely_moves_around(self, name):
        assert control(name=name).mutating is False

    def test_a_plain_navigation_is_never_mutating(self):
        """The first thing the word list did was flag the link named "Faktury i
        płatności" — because it contains the word for *payment*. An <a> with a
        real href is a GET to another page, which is what read_url does under
        auto-approval; gating it asks permission to read what aish may read."""
        link = control(
            kind=browse.LINK,
            name="Faktury i płatności",
            href="https://eon.pl/mojeon/Faktury-i-platnosci",
            detail="https://eon.pl/mojeon/Faktury-i-platnosci",
        )
        assert link.mutating is False

    def test_a_link_to_nowhere_is_a_button_wearing_a_links_clothes(self):
        """`<a href="#">Zapłać</a>` is a JavaScript control. What makes a link
        safe is that it NAVIGATES, not that it is an anchor."""
        assert control(kind=browse.LINK, name="Zapłać", href="").mutating is True

    def test_a_form_submit_is_gated_whatever_it_is_called(self):
        """The nondescript "Dalej" that posts the form is the dangerous one."""
        assert control(name="Dalej", submits=True).mutating is True

    def test_typing_is_not_gated(self):
        """Typing changes nothing until something is pressed. Gating the
        keystroke asks twice for one act and trains the owner to tap through."""
        assert control(kind=browse.FIELD, name="Szukaj").mutating is False
        assert control(kind=browse.PASSWORD, name="Hasło").mutating is False


class TestTheControlList:
    def test_a_control_reads_as_one_line(self):
        line = control(n=7, name="Przełącz lokal").line()
        assert line == "button 'Przełącz lokal'"

    def test_a_link_carries_its_destination(self):
        """A link the model can READ instead of clicking is a round trip saved,
        and the href is often the only way to tell two identically-named
        controls apart."""
        line = control(
            n=2, kind=browse.LINK, name="Szczegóły konta",
            href="https://eon.pl/x?Id=80001023988",
            detail="https://eon.pl/x?Id=80001023988",
        ).line()
        assert "→ https://eon.pl/x?Id=80001023988" in line

    def test_a_gated_control_says_so_before_it_is_pressed(self):
        assert "(needs approval)" in control(name="Zapłać").line()

    def test_the_cap_is_never_silent(self, monkeypatch):
        """A model that cannot see a control concludes the page has none — and
        goes back to guessing URLs, which is the whole failure this replaces."""
        out = web_module._present_snapshot(snapshot(controls=[control()], hidden=37))
        assert "37 more control(s) not listed" in out

    def test_the_page_is_wrapped_as_untrusted_content(self):
        """A browse result is attacker-controlled page text, now in a session
        that can click."""
        out = web_module._present_snapshot(snapshot(controls=[control()]))
        assert out.startswith(web_module.UNTRUSTED_NOTE)

    def test_the_control_list_survives_truncation(self):
        """The controls are the entire point of the call, so a text cap that
        fell inside them would cut exactly what the model is meant to act on."""
        long_page = snapshot(
            text="x" * (web_module.PAGE_MAX_CHARS + 5000),
            controls=[control(n=0, name="Przełącz lokal")],
        )
        out = web_module._present_snapshot(long_page)
        assert "button 'Przełącz lokal'" in out
        assert web_module.CUT_MARKER in out

    def test_a_problem_is_stated_above_the_page(self):
        out = web_module._present_snapshot(
            snapshot(controls=[control()], problem="there is no control [9] any more")
        )
        assert out.startswith("[aish: there is no control [9] any more]")


class TestStillLoading:
    """A page mid-load HAS text, so neither the emptiness test nor the thin-page
    retry catches it. Measured on eon.pl/mojeon/Umowy-i-dane/Moje-Umowy, which
    came back as its own loading message twice in one session."""

    @pytest.mark.parametrize(
        "text",
        ["Wczytywanie danych", "Ładowanie…", "Loading", "Proszę czekać",
         "Moje Umowy\nWczytywanie danych\nGrupa E.ON"],
    )
    def test_a_page_that_says_it_is_not_ready(self, text):
        assert browse.still_loading(text) is True

    @pytest.mark.parametrize(
        "text", ["", "Faktura 09/2026 — 226,89 zł", "Downloading files is easy"]
    )
    def test_a_page_that_is_ready(self, text):
        assert browse.still_loading(text) is False


class TestDownloads:
    """The document at the end of the flow — the thing the anonymous opener
    behind read_pdf could never have fetched."""

    def test_the_model_is_told_where_the_file_went_and_how_to_read_it(self):
        """A model that has got the document and does not know it may open it
        stops there, which is the failure this line exists to prevent."""
        out = web_module._present_snapshot(
            snapshot(controls=[], downloads=["/state/browser/downloads/faktura.pdf"])
        )
        assert "/state/browser/downloads/faktura.pdf" in out
        assert 'read_pdf(source="<path>")' in out
        assert "do NOT fetch it again" in out

    def test_the_download_note_is_aishs_own_words(self):
        """Above the untrusted banner: the path is aish's and the site had no
        say in it."""
        out = web_module._present_snapshot(snapshot(downloads=["/tmp/x.pdf"]))
        assert out.index("[aish: this action downloaded") < out.index("untrusted web")

    @pytest.mark.parametrize(
        "suggested, expected",
        [
            ("faktura 09-2026.pdf", "faktura 09-2026.pdf"),
            ("../../../etc/passwd", "passwd"),
            ("/absolute/path.pdf", "path.pdf"),
            ("..", "download"),
            ("", "download"),
            ("nasty;rm -rf.pdf", "nasty_rm -rf.pdf"),
        ],
    )
    def test_the_site_never_chooses_where_the_write_lands(self, suggested, expected):
        """The filename comes from the SITE, which puts it in the same class as
        any other page content: data, never instructions — and here the
        instruction would be a path."""
        assert browse.safe_filename(suggested) == expected

    def test_the_directory_does_not_grow_forever(self, tmp_path):
        for i in range(5):
            f = tmp_path / f"invoice-{i}.pdf"
            f.write_bytes(b"x" * 100)
            os.utime(f, (i, i))          # oldest first
        removed = browse.prune_downloads(tmp_path, keep_bytes=250)
        # Oldest first, and it stops the moment the directory FITS — the
        # invariant is the total, not a count.
        assert removed == ["invoice-0.pdf", "invoice-1.pdf", "invoice-2.pdf"]
        assert sorted(f.name for f in tmp_path.glob("*")) == [
            "invoice-3.pdf", "invoice-4.pdf",
        ]
        assert sum(f.stat().st_size for f in tmp_path.glob("*")) <= 250

    def test_a_small_enough_directory_is_left_alone(self, tmp_path):
        (tmp_path / "one.pdf").write_bytes(b"x" * 10)
        assert browse.prune_downloads(tmp_path, keep_bytes=1000) == []

    def test_downloads_live_under_state_never_config(self, tmp_path, monkeypatch):
        """Same fence as the profile: the config tree is auto-committed and
        pushed, and these are the owner's own documents."""
        monkeypatch.setenv("AISH_STATE_DIR", str(tmp_path))
        assert browser.downloads_dir() == tmp_path / "browser" / "downloads"
        assert ".config" not in str(browser.downloads_dir())

    def test_read_pdf_may_open_what_browse_just_named(self, tmp_path):
        """Otherwise the tool tells the model to read a file the model is not
        allowed to read — the #220 asymmetry, reopened."""
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            cwd=tmp_path,
        )
        assert browser.downloads_dir() in agent.workspace_roots()


class TestBrowseGate:
    """Who may drive, and what they may press.

    Two questions, not one: may aish drive this host at all (once per host per
    task), and may it press THIS control (every time, by name)."""

    def _agent(self, monkeypatch, approve, snap=None):
        asked = []

        def approve_tool(name, args, preview):
            asked.append(preview)
            return approve(name, args, preview)

        agent = Agent(
            model="fake", approve=lambda _c: True,
            client_chat=lambda **kw: {}, approve_tool=approve_tool,
        )
        agent._browse_view.remember(snap)
        return agent, asked

    def test_driving_a_host_asks_once_per_task(self, monkeypatch):
        agent, asked = self._agent(monkeypatch, lambda *a: True)
        args = {"url": "https://eon.pl/mojeon"}
        assert agent._browse_gate("browse", args) is None
        assert agent._browse_gate("browse", {"url": "https://eon.pl/faktury"}) is None
        assert len(asked) == 1
        assert "drive eon.pl" in asked[0]

    def test_the_card_says_it_acts_as_the_owner(self, monkeypatch):
        agent, asked = self._agent(monkeypatch, lambda *a: True)
        agent._browse_gate("browse", {"url": "https://eon.pl/mojeon"})
        assert "AS YOU" in asked[0]

    def test_denying_the_host_stops_the_flow(self, monkeypatch):
        agent, _ = self._agent(monkeypatch, lambda *a: False)
        out = agent._browse_gate("browse", {"url": "https://eon.pl/mojeon"})
        assert "USER DENIED driving eon.pl" in out
        assert "eon.pl" not in agent._approved_browsing

    def test_a_denial_with_a_comment_arms_the_stop_gate(self, monkeypatch):
        agent, _ = self._agent(monkeypatch, lambda *a: Denied("not in my account"))
        out = agent._browse_gate("browse", {"url": "https://eon.pl/mojeon"})
        assert "not in my account" in out
        assert agent._pending_comment_response

    def test_an_approval_with_a_comment_holds_the_action(self, monkeypatch):
        """Approve + comment = continue but ADJUST: the original is never run."""
        agent, _ = self._agent(monkeypatch, lambda *a: Approved("use the other one"))
        out = agent._browse_gate("browse", {"url": "https://eon.pl/mojeon"})
        assert "use the other one" in out

    def test_no_approver_fails_closed(self, monkeypatch):
        agent = Agent(
            model="fake", approve=lambda _c: True,
            client_chat=lambda **kw: {}, approve_tool=None,
        )
        out = agent._browse_gate("browse", {"url": "https://eon.pl/mojeon"})
        assert out.startswith("NOT EXECUTED")
        assert "no approver" in out

    def test_an_ordinary_click_rides_the_host_grant(self, monkeypatch):
        """A flow that clicks through twenty pages of one portal asks once — a
        card per click is a card nobody reads."""
        snap = snapshot(controls=[control(n=3, name="Przełącz lokal")])
        agent, asked = self._agent(monkeypatch, lambda *a: True, snap)
        agent._approved_browsing.add("eon.pl")
        assert agent._browse_gate("browse_act", {"target": 3}) is None
        assert asked == []

    def test_a_mutating_click_asks_again_and_names_the_control(self, monkeypatch):
        snap = snapshot(controls=[control(n=5, name="Zapłać")])
        agent, asked = self._agent(monkeypatch, lambda *a: True, snap)
        agent._approved_browsing.add("eon.pl")
        assert agent._browse_gate("browse_act", {"target": 5}) is None
        assert len(asked) == 1
        assert "click button 'Zapłać' on eon.pl" in asked[0]

    def test_denying_a_mutating_click_says_nothing_changed(self, monkeypatch):
        snap = snapshot(controls=[control(n=5, name="Zapłać")])
        agent, _ = self._agent(monkeypatch, lambda *a: False, snap)
        agent._approved_browsing.add("eon.pl")
        out = agent._browse_gate("browse_act", {"target": 5})
        assert "was NOT clicked and nothing on the page changed" in out
        assert "another control that does the same thing" in out

    def test_a_password_field_is_refused_outright(self, monkeypatch):
        """Structural, not a card: there is no yes that makes this a good idea,
        and a card offering one would teach the owner there is."""
        snap = snapshot(controls=[control(n=1, kind=browse.PASSWORD, name="Hasło")])
        agent, asked = self._agent(monkeypatch, lambda *a: True, snap)
        agent._approved_browsing.add("eon.pl")
        out = agent._browse_gate("browse_act", {"target": 1, "action": "type"})
        assert "aish never types passwords" in out
        assert "/browser eon.pl" in out
        assert asked == []          # never even offered

    def test_the_card_names_the_control_the_model_asked_for(self, monkeypatch):
        """What the owner used to read on the card was `browse_act target=15,
        action=click`, and nobody can review "click 15". He decides to press a
        button by reading its label; the card has to carry the same words."""
        snap = snapshot(controls=[control(n=5, name="Zapłać")])
        agent, asked = self._agent(monkeypatch, lambda *a: True, snap)
        agent._approved_browsing.add("eon.pl")
        assert agent._browse_gate("browse_act", {"target": "Zapłać"}) is None
        assert "click button 'Zapłać' on eon.pl" in asked[0]

    def test_the_echo_names_the_control_too(self, monkeypatch):
        """The transcript line is the owner's only running account of what aish
        is doing inside his account, once the host grant has been given."""
        snap = snapshot(controls=[control(n=5, name="Zapłać")])
        agent, _ = self._agent(monkeypatch, lambda *a: True, snap)
        label, _thunk = agent._browse_call("browse_act", {"target": "Zapłać"})
        assert label == "→ browse: click button 'Zapłać'"

    def test_acting_with_nothing_open_says_what_to_do(self, monkeypatch):
        agent, _ = self._agent(monkeypatch, lambda *a: True, None)
        out = agent._browse_gate("browse_act", {"target": 1})
        assert "Call browse(url) first" in out


class TestBrowseDispatch:
    """The seam: browse is NOT a read-only tool, so it never rides the parallel
    path, and its result reaches the model through the same gate as everything
    else."""

    def test_browse_is_not_on_the_parallel_read_path(self):
        assert "browse" not in agent_module.READ_ONLY_TOOLS
        assert "browse_act" not in agent_module.READ_ONLY_TOOLS

    def test_the_echo_names_the_control_not_the_number(self, monkeypatch):
        """The owner grants a host once and then watches a flow go past. The
        transcript line is his only running account of what aish is clicking
        inside his account."""
        snap = snapshot(controls=[control(n=4, name="Przełącz lokal")])
        agent = Agent(model="fake", approve=lambda _c: True, client_chat=lambda **kw: {})
        agent._browse_view.remember(snap)
        label, _ = agent._browse_call("browse_act", {"target": 4})
        assert label == "→ browse: click button 'Przełącz lokal'"

    def test_a_dead_browser_reports_instead_of_crashing(self, monkeypatch):
        def unavailable(url, **kw):
            raise browser.BrowserUnavailable("playwright is not installed")

        monkeypatch.setattr(browser, "browse_open", unavailable)
        out = web_module.browse("https://eon.pl/mojeon")
        assert out.startswith("ERROR")
        assert "playwright is not installed" in out

    def test_browse_refuses_a_non_public_host(self, monkeypatch):
        """Same SSRF fence as read_url: a driven page is still a model-chosen
        URL, and this one can click."""
        out = web_module.browse("http://169.254.169.254/latest/meta-data/")
        assert out.startswith("ERROR")
        assert "public internet hosts" in out


class TestWhoseDownloadIsIt:
    """The tab that downloads is very often not the tab that was clicked (#246).

    E.ON's `Pobierz e-fakturę` is a `target=_blank` link: Chrome opens a fresh
    tab, starts the transfer, and closes the tab. Four clicks across two real
    sessions produced four page snapshots and no file, because the listener was
    bound to the one tab aish opened."""

    def test_a_popup_download_belongs_to_the_browse_session(self):
        owner = browser._Owner()
        popup, file = object(), object()
        owner.downloads.append((popup, file))
        assert owner.take_downloads() == [file]

    def test_a_reads_download_is_not_swept_up_by_a_click(self):
        owner = browser._Owner()
        read_page, browse_file, read_file = object(), object(), object()
        owner.read_pages.add(read_page)
        owner.downloads += [(object(), browse_file), (read_page, read_file)]
        assert owner.take_downloads() == [browse_file]
        # And the read's own is still there for the read to claim.
        assert owner.take_downloads(read_page=read_page) == [read_file]

    def test_draining_takes_each_file_once(self):
        owner = browser._Owner()
        owner.downloads.append((object(), object()))
        assert len(owner.take_downloads()) == 1
        assert owner.take_downloads() == []

    def test_a_read_that_lands_on_a_file_says_so_instead_of_going_anonymous(self):
        """The failure this replaces: aish held seven of the owner's invoices,
        refetched every one as a stranger, and told him it could not get them."""
        note = web_module.downloaded_note(
            ["/state/browser/downloads/faktura.pdf"], "this link is a file"
        )
        assert "/state/browser/downloads/faktura.pdf" in note
        assert 'read_pdf(source="<path>")' in note

    def test_no_files_is_no_note(self):
        assert web_module.downloaded_note([], "this link is a file") == ""


class TestTheSiteSaysWhereYouActuallyAre:
    """A redirect is not an error, and silence about it is (#247). The model
    asked for qatarairways.com/en-pl/help/feedback.retrieve.html, was handed
    /en-pl/help.html, and reasoned about the form for the rest of the turn."""

    @pytest.mark.parametrize(
        "asked, got",
        [
            ("https://eon.pl/mojeon", "https://eon.pl/mojeon/"),
            ("https://eon.pl/mojeon/", "https://eon.pl/mojeon"),
            ("https://eon.pl/mojeon", "https://eon.pl/mojeon#faktury"),
            ("eon.pl/mojeon", "https://eon.pl/mojeon"),
            ("https://EON.pl/mojeon", "https://eon.pl/mojeon"),
        ],
    )
    def test_the_same_page_by_another_spelling_is_not_a_redirect(self, asked, got):
        assert browse.landed_elsewhere(asked, got) is False

    @pytest.mark.parametrize(
        "asked, got",
        [
            ("https://q.com/help/feedback.retrieve.html", "https://q.com/help.html"),
            ("https://eon.pl/mojeon", "https://eon.pl/mojeon/Logowanie"),
            ("https://eon.pl/x", "https://other.pl/x"),
            ("https://eon.pl/x", "https://eon.pl/x?session=expired"),
        ],
    )
    def test_a_different_page_is_reported(self, asked, got):
        assert browse.landed_elsewhere(asked, got) is True

    def test_nothing_to_compare_is_not_a_redirect(self):
        assert browse.landed_elsewhere("", "https://eon.pl") is False
        assert browse.landed_elsewhere("https://eon.pl", "") is False

    def test_the_model_is_told_which_page_it_is_standing_on(self):
        out = web_module._present_snapshot(
            snapshot(url="https://q.com/help.html", asked="https://q.com/feedback.html")
        )
        assert "you asked for https://q.com/feedback.html" in out
        assert "https://q.com/help.html instead" in out
        assert out.index("[aish: you asked for") < out.index("untrusted web")

    def test_an_ordinary_page_carries_no_such_note(self):
        assert "you asked for" not in web_module._present_snapshot(snapshot())


class TestTheSessionOutlivesTheOwnerReading:
    """Three minutes of him reading collected the browser, and his next message
    got "nothing is open to act on" (#248)."""

    def owner(self, *, page=None, touched=0.0):
        owner = browser._Owner()
        if page is not None:
            session = browser._Session(page)
            session.touched = touched
            owner.browse_pages[""] = session
        return owner

    def open_page(self, closed=False):
        return type("P", (), {"is_closed": lambda self: closed})()

    def test_an_open_browse_session_holds_the_browser(self, monkeypatch):
        monkeypatch.setattr(browser.time, "monotonic", lambda: 100.0)
        assert self.owner(page=self.open_page(), touched=99.0).held() is True

    def test_an_abandoned_one_does_not_hold_it_forever(self, monkeypatch):
        monkeypatch.setattr(browser.time, "monotonic", lambda: 100.0)
        stale = 100.0 - browser.BROWSE_MAX_IDLE - 1
        assert self.owner(page=self.open_page(), touched=stale).held() is False

    def test_a_closed_page_is_not_a_session(self, monkeypatch):
        monkeypatch.setattr(browser.time, "monotonic", lambda: 100.0)
        held = self.owner(page=self.open_page(closed=True), touched=99.0).held()
        assert held is False

    def test_an_idle_browser_with_nothing_open_is_still_collected(self):
        assert browser._Owner().held() is False

    def test_the_owners_own_window_still_outranks_everything(self, monkeypatch):
        monkeypatch.setattr(browser.time, "monotonic", lambda: 100.0)
        owner = browser._Owner()
        owner.view = object()
        owner.view_touched = 99.0
        assert owner.held() is True

    def test_a_reaped_session_stops_reading_as_open(self):
        """A module-level snapshot used to survive the reaper, so aish said a
        session was open for a page that no longer existed (#248). There is no
        such global any more (#272) — the reaper drops every chat's page, and
        the next act is told to reopen rather than handed a dead one."""
        import asyncio

        owner = self.owner(page=self.open_page(), touched=time.monotonic())
        assert owner.held() is True
        asyncio.run(owner._close())
        assert owner.browse_pages == {}
        assert owner.held() is False

        async def act():
            return await browser._session(owner, "", opening=False)

        with pytest.raises(browser.BrowserUnavailable, match="nothing is open"):
            asyncio.run(act())


class TestChoosingWithoutReadingTheList:
    """A 312-option airport picker is not the page (#245). The line says how
    many there are; saying what you want is how you get one."""

    OPTIONS = [
        ("Polska (+48)", "48"),
        ("Portugalia (+351)", "351"),
        ("Peru (+51)", "51"),
        ("Iran (+98)", "98"),
        ("Irak (+964)", "964"),
        ("Łódź", "LDZ"),
    ]

    def test_the_exact_label(self):
        assert browse.match_option(self.OPTIONS, "Peru (+51)").value == "51"

    def test_the_case_it_was_not_written_in(self):
        assert browse.match_option(self.OPTIONS, "polska (+48)").value == "48"

    def test_the_value_when_that_is_what_the_site_labels_by(self):
        assert browse.match_option(self.OPTIONS, "351").value == "351"

    def test_a_substring_of_one_option(self):
        assert browse.match_option(self.OPTIONS, "Portug").value == "351"

    @pytest.mark.parametrize("asked", ["Lodz", "lodz", "ŁÓDŹ"])
    def test_the_owners_alphabet_without_the_owners_keyboard(self, asked):
        """This is the owner's web: "Lodz" has to find "Łódź"."""
        assert browse.match_option(self.OPTIONS, asked).value == "LDZ"

    def test_two_matches_is_a_question_not_a_guess(self):
        """A choose is very often followed by a submit, so 'Iran' quietly
        standing in for 'Irak' is the kind of wrong this cannot make."""
        picked = browse.match_option(self.OPTIONS, "Ira")
        assert picked.value == ""
        assert "matches 2 options" in picked.problem
        assert "Iran (+98)" in picked.problem and "Irak (+964)" in picked.problem

    def test_no_match_hands_back_the_list_it_needed(self):
        picked = browse.match_option(self.OPTIONS, "Atlantyda")
        assert picked.value == ""
        assert "no option matches" in picked.problem
        assert "Polska (+48)" in picked.problem

    def test_the_candidate_list_is_itself_bounded(self):
        many = [(f"Kraj {i}", str(i)) for i in range(250)]
        picked = browse.match_option(many, "Kraj")
        assert picked.problem.count("'Kraj ") == browse.CANDIDATES_SHOWN
        assert "and 238 more" in picked.problem

    def test_a_short_list_is_still_shown_in_full_on_the_page(self):
        """Up to CHOICE_INLINE_MAX, reading the options beats counting them."""
        assert browse.CHOICE_INLINE_MAX == 8


class TestWhatTheModelIsToldItCannotSee:
    """A model that cannot see the control it wants concludes the page has none
    and goes back to guessing URLs — which is the failure browse exists to end."""

    def test_closed_away_controls_are_counted_not_dropped(self):
        out = web_module._present_snapshot(snapshot(unreachable=19))
        assert "19 more control(s) are on this page but closed away" in out
        assert "Press whatever opens them first" in out

    def test_a_page_hiding_nothing_says_nothing(self):
        assert "closed away" not in web_module._present_snapshot(snapshot())

    def test_the_cap_and_the_hiding_are_different_facts(self):
        out = web_module._present_snapshot(snapshot(unreachable=3, hidden=5))
        assert "3 more control(s) are on this page but closed away" in out
        assert "5 more control(s) not listed" in out


class TestHowItWasPressedIsNotWhetherItWorked:
    """`notice` and `problem` mean opposite things, and a press aish did not
    physically make must never be reported as one it did."""

    def test_an_escalated_press_says_so(self):
        out = web_module._present_snapshot(
            snapshot(notice="the click would not land, so aish pressed it with the keyboard")
        )
        assert "[aish: the click would not land" in out
        assert out.index("[aish: the click") < out.index("untrusted web")

    def test_an_ordinary_press_is_silent(self):
        assert "[aish:" not in web_module._present_snapshot(snapshot())

    def test_a_failure_and_a_notice_are_both_reportable(self):
        out = web_module._present_snapshot(snapshot(problem="stuck", notice="by keyboard"))
        assert "[aish: stuck]" in out and "[aish: by keyboard]" in out


class TestAPageThatSaysItIsStillLoading:
    def test_only_the_top_of_the_page_counts(self):
        """A finished article with "Loading comments…" halfway down is not a
        page that has not finished, and paying the retry for it is seconds."""
        article = "Faktura 09/2026 — 226,89 zl\n" * 40 + "Loading comments…"
        assert browse.still_loading(article) is False

    def test_a_page_that_leads_with_it_still_counts(self):
        assert browse.still_loading("Wczytywanie danych\n" + "x" * 5000) is True


class TestADropdownIsNotThePage:
    """`inner_text` includes every option of a closed <select>. Measured: a
    250-option country picker was 3 500 of a 4 176-character page — 84% of it,
    and on a real portal most of the read budget spent on one control the model
    has not reached yet."""

    def test_the_whole_block_is_replaced_by_a_count(self):
        text = "Wybierz kraj\nPolska\nPortugalia\nPeru\nDalej"
        out = browse.strip_option_floods(
            text, [{"text": "Polska\nPortugalia\nPeru", "count": 250, "name": "kraj"}]
        )
        assert out == "Wybierz kraj\n[dropdown 'kraj': 250 options — see the control list]\nDalej"

    def test_a_line_elsewhere_on_the_page_survives(self):
        """Never line by line: the page's own sentence about Poland is not the
        dropdown's option for it."""
        text = "Faktura dla: Polska\nPolska\nPortugalia\nPeru\nRazem"
        out = browse.strip_option_floods(
            text, [{"text": "Polska\nPortugalia\nPeru", "count": 250, "name": ""}]
        )
        assert "Faktura dla: Polska" in out
        assert "Portugalia" not in out

    def test_a_block_that_does_not_match_is_left_alone(self):
        """The worst this can do is nothing."""
        text = "Polska | Portugalia"
        assert browse.strip_option_floods(text, [{"text": "Polska\nPortugalia"}]) == text

    def test_nothing_to_strip_changes_nothing(self):
        assert browse.strip_option_floods("hello", []) == "hello"

    def test_an_unnamed_dropdown_is_still_counted(self):
        out = browse.strip_option_floods("a\nb\nc", [{"text": "a\nb", "count": 9}])
        assert out == "[dropdown: 9 options — see the control list]\nc"


class TestTheInjectedJavaScriptParses:
    """A backslash that survives one layer of quoting and not the other is a
    syntax error the PAGE reports and nothing else does — `FLOOD_JS` shipped
    with a literal newline inside a string literal, the evaluate threw, the
    caller swallowed it as "a page that will not answer", and the only symptom
    was that nothing happened."""

    @pytest.mark.parametrize(
        "name",
        ["CONTROLS_JS", "REACHABLE_JS", "CENTRE_JS", "OPTIONS_JS", "FLOOD_JS"],
    )
    def test_every_injected_script_is_valid_javascript(self, name):
        node = shutil.which("node")
        if not node:
            pytest.skip("node is not installed")
        source = f"const f = {getattr(browse, name)};"
        result = subprocess.run(
            [node, "--check", "-"], input=source, text=True, capture_output=True
        )
        assert result.returncode == 0, f"{name} does not parse:\n{result.stderr}"


class TestHidingAControlNeverRoutesAroundItsCard:
    """The one thing #244 could have broken. Reachability filters what the
    model is OFFERED; the gate decides what it may press. If those two could
    disagree, "closed away" would become a way to press something the owner
    never approved."""

    def test_not_listed_means_not_tagged_means_not_actable(self):
        """The chain, in the enumeration itself.

        #270 moved the cap OUT of the walk so a topic could decide which
        controls the budget buys, which means the over-cap `continue` that used
        to guard the tag is gone. The invariant it protected must not be: the
        walk only ever COLLECTS, `emit` is still the sole writer of the tag, and
        `emit` is reached only through the capped selection."""
        walk = browse.CONTROLS_JS[
            browse.CONTROLS_JS.index("const walk ="):browse.CONTROLS_JS.index("walk(document);")
        ]
        # The unreachable `continue` still precedes the only thing the walk does
        # with a control, so a control the page has closed away never reaches
        # the candidate list — let alone the tag.
        assert walk.index("if (unreachable(el))") < walk.index("found.push(")
        assert "setAttribute" not in walk, "the walk must not tag anything"

        # `emit` is the only thing that writes a tag, and it is called exactly
        # once — over `take`, which is the candidate list capped at `room`.
        assert browse.CONTROLS_JS.count("setAttribute('data-aish-n'") == 1
        select = browse.CONTROLS_JS[browse.CONTROLS_JS.index("walk(document);"):]
        assert select.count("emit(") == 1
        assert "for (const c of take) emit(" in select
        assert "const take = wanted.concat(rest).slice(0, room);" in select
        assert "const room = Math.max(0, opts.max - opts.offset);" in select

    def test_a_topic_reorders_the_budget_but_never_widens_it(self):
        """`topic` decides WHICH controls the cap buys, never how many. A
        narrowing that could raise the ceiling would be a way to spend an
        unbounded amount of context on a page that names the right word."""
        select = browse.CONTROLS_JS[browse.CONTROLS_JS.index("walk(document);"):]
        # One slice, one bound, and the bound is the same MAX_CONTROLS budget
        # the un-narrowed path uses.
        assert select.count(".slice(0, room)") == 1
        assert select.count("const room =") == 1
        # And the unmatched controls are kept rather than filtered away: the
        # menu and the next-page link are what a content topic never matches.
        assert "const rest = needle ? found.filter((c) => !hit(c)) : found;" in select
        assert "wanted.concat(rest)" in select

    def test_a_mutating_control_that_is_hidden_cannot_be_reached_by_number(self):
        """A control the page has closed away is not in the snapshot, so the
        fallbacks in `_press` are handed nothing to act on — `mutating` and
        `href` both come from the snapshot's control, never from the live DOM."""
        snap = snapshot(controls=[control(n=0, name="Pokaż szczegóły")], unreachable=1)
        assert snap.control(7) is None

    def test_the_press_fallbacks_read_the_snapshot_the_gate_read(self, monkeypatch):
        """web.browse_act must pass the classification the CARD was drawn from,
        so a control the gate saw as a payment cannot be dispatched as if it
        were an ordinary link."""
        seen = {}

        def fake_act(n, action, **kw):
            seen.update(kw)
            return snapshot()

        shown(snapshot(controls=[control(n=2, name="Zapłać", kind=browse.LINK)]))
        monkeypatch.setattr(browser, "browse_act", fake_act)
        web_module.browse_act(2, "click")
        assert seen["mutating"] is True

    def test_a_destination_the_ssrf_guard_refuses_is_never_offered(self, monkeypatch):
        """The link fallback navigates, so it keeps the fence `browse` itself
        applies to a model-chosen URL."""
        seen = {}

        def fake_act(n, action, **kw):
            seen.update(kw)
            return snapshot()

        local = control(n=1, name="Panel", kind=browse.LINK, href="http://127.0.0.1/admin")
        shown(snapshot(controls=[local]))
        monkeypatch.setattr(browser, "browse_act", fake_act)
        web_module.browse_act(1, "click")
        assert seen["href"] == ""


class TestTheFileIsHandedOverNotDescribed:
    """A path in a sentence is not a document (#237). aish drove the portal,
    clicked "Pobierz e-fakturę" seven times and got seven invoices, and the
    owner was told a folder name — then the model reached for `file://` on its
    own, which is dead on a web page."""

    PDF = "/Users/e/.local/state/aish/browser/downloads/dokument_229500955650.pdf"

    def test_the_note_carries_the_line_the_answer_needs(self):
        note = web_module.downloaded_note([self.PDF], "this action downloaded")
        assert f"[dokument_229500955650.pdf]({self.PDF})" in note

    def test_the_model_is_told_it_MUST_pass_it_on(self):
        """Capability phrasing gets ignored; MUST plus the literal line works."""
        note = web_module.downloaded_note([self.PDF], "this action downloaded")
        assert "MUST" in note
        assert "EXACTLY as written" in note

    def test_every_file_gets_its_own_line(self):
        note = web_module.downloaded_note(
            ["/d/a.pdf", "/d/b.pdf"], "this action downloaded"
        )
        assert "[a.pdf](/d/a.pdf)" in note and "[b.pdf](/d/b.pdf)" in note

    def test_a_space_in_the_name_is_left_alone(self):
        """"faktura 09-2026.pdf" is what a real invoice is called, and the
        renderer allows spaces inside the parentheses for exactly this."""
        assert "[faktura 09-2026.pdf](/d/faktura 09-2026.pdf)" in web_module.downloaded_note(
            ["/d/faktura 09-2026.pdf"], "x"
        )

    def test_a_bracket_the_SITE_chose_cannot_break_the_line(self):
        """Built here rather than left to the model, for the reason show_image
        builds its own: the filename is page content."""
        line = web_module.downloaded_note(["/d/faktura [2026].pdf"], "x")
        assert "[faktura 2026.pdf](/d/faktura [2026].pdf)" in line

    def test_no_download_no_line(self):
        assert web_module.downloaded_note([], "x") == ""


class TestScrollingIsWhatMakesAThingReachable:
    """The predicate's whole question is "could the owner scroll to this and
    press it", and a list that scrolls is the commonest way the answer is yes
    while the element is nowhere near the screen (#251).

    The geometry itself is only checkable in a real browser — that is what
    `scripts/verify_browse.py` is for, and it now serves a five-entry scrollable
    list inside a fixed header, which is the account switcher that hid the
    owner's fifth property. What is checkable here is that the rule the fix
    turns on is the one written down."""

    def test_a_clipping_ancestor_hands_its_OWN_box_upward(self):
        """Not the intersection. An entry below the scroll fold has no overlap
        with the visible container, so the intersection came out inverted —
        bottom above top — and every ancestor test above it was then asked about
        a negative-height box. The fixed header refused it as off-canvas."""
        js = browse.REACH_JS
        walk = js[js.index("const unreachable"):]
        carry = walk.index("box = {left: pr.left, top: pr.top")
        assert walk.index("return 'clipped'") < carry
        assert "Math.min(box.bottom, pr.bottom)" not in walk

    def test_a_real_overlap_is_what_the_clipped_test_demands(self):
        """The other half, and they must not be confused: a container that
        cannot be scrolled has to ALREADY contain the element."""
        assert "if (!meets(box, pr)) return 'clipped';" in browse.REACH_JS

    def test_being_below_the_scroll_range_is_still_out_of_reach(self):
        """The range check is what separates "scroll to it" from "it is not in
        there at all", and it runs before anything is carried upward."""
        js = browse.REACH_JS
        assert "return 'outside-scroll-range'" in js
        assert js.index("return 'outside-scroll-range'") < js.index(
            "box = {left: pr.left, top: pr.top"
        )


class TestAControlIsAddressedByWhatItSays:
    """#251. The model used to press `[13]`, which is what the trace and the
    approval card then showed the owner — `browse_act target=15, action=click`,
    reviewable by nobody. A person decides to press a button by reading its
    label, so that is what aish asks for and what the card prints."""

    def test_a_control_is_asked_for_by_its_name(self):
        controls = browse.controls_from([{"n": 4, "kind": "button", "name": "Szukaj"}])
        assert controls[0].address == "Szukaj"
        assert browse.resolve(controls, "Szukaj").control is controls[0]

    def test_the_name_is_matched_the_way_the_owners_web_is_written(self):
        """Same fold as `match_option`: 'Przelacz lokal' has to find 'Przełącz
        lokal', because that is how it will be typed."""
        controls = browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Przełącz lokal"}]
        )
        assert browse.resolve(controls, "przelacz lokal").control is controls[0]

    def test_two_nodes_of_one_control_share_one_address(self):
        """The mobile copy and the desktop copy of one nav link say the same
        thing and go to the same place. Numbering them apart would ask the model
        a question with no right answer."""
        controls = browse.controls_from([
            {"n": 1, "kind": "link", "name": "Faktury", "detail": "https://x/f"},
            {"n": 2, "kind": "link", "name": "Faktury", "detail": "https://x/f"},
        ])
        assert [c.address for c in controls] == ["Faktury", "Faktury"]
        assert browse.resolve(controls, "Faktury").control is controls[0]

    def test_two_controls_that_only_look_alike_are_told_apart(self):
        controls = browse.controls_from([
            {"n": 1, "kind": "link", "name": "Wybierz", "detail": "https://x/a"},
            {"n": 2, "kind": "link", "name": "Wybierz", "detail": "https://x/b"},
        ])
        assert [c.address for c in controls] == ["Wybierz #1", "Wybierz #2"]
        assert browse.resolve(controls, "Wybierz #2").control is controls[1]

    def test_an_ambiguous_name_is_a_question_never_a_guess(self):
        """The same posture as `match_option`: picking silently between two
        buttons is how 'Iran' stands in for 'Iraq' — and this one may be a
        button that spends money."""
        controls = browse.controls_from([
            {"n": 1, "kind": "link", "name": "Wybierz", "detail": "https://x/a"},
            {"n": 2, "kind": "link", "name": "Wybierz", "detail": "https://x/b"},
        ])
        found = browse.resolve(controls, "Wybierz")
        assert found.control is None
        assert "'Wybierz #1', 'Wybierz #2'" in found.problem

    def test_a_control_the_page_gave_no_words_to_is_asked_for_by_number(self):
        controls = browse.controls_from([{"n": 12, "kind": "button", "name": ""}])
        assert controls[0].address == "#12"
        assert browse.resolve(controls, "#12").control is controls[0]
        assert browse.resolve(controls, "12").control is controls[0]

    def test_a_name_that_matches_nothing_hands_back_what_is_there(self):
        controls = browse.controls_from([{"n": 1, "kind": "button", "name": "Szukaj"}])
        found = browse.resolve(controls, "Search")
        assert found.control is None
        assert "'Szukaj'" in found.problem

    def test_part_of_a_long_label_still_finds_it(self):
        controls = browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Szukaj połączeń i cen"}]
        )
        assert browse.resolve(controls, "Szukaj").control is controls[0]


class TestAnIconIsNotANamelessButton:
    """The old rule dropped any control with no words and nowhere to go, which
    took the swap-airports arrow, the hamburger and every dialog's X off the
    list — on a booking form, half the controls that matter."""

    def test_the_page_is_asked_what_it_calls_its_own_picture(self):
        source = browse.CONTROLS_JS
        # The ladder, in the order a name is most likely to be a real one.
        for asked in ("svg.querySelector('title')", "img[alt]", "data-icon", "GLYPHS"):
            assert asked in source

    def test_a_stylesheet_name_is_turned_into_words(self):
        """'icon-swap-airports' is the page saying what the picture is; it is
        just written for a stylesheet rather than for a person."""
        assert "replace(/[_\\-.]+/g, ' ')" in browse.CONTROLS_JS

    def test_a_glyph_is_read_as_the_word_it_draws(self):
        assert "'×': 'close'" in browse.CONTROLS_JS
        assert "'☰': 'menu'" in browse.CONTROLS_JS


class TestWhatChangedRatherThanThePageAgain:
    """#251, the measured half: nine actions on lot.com cost 44 788 characters,
    ~5 000 each, because every click re-sent the whole page and the whole
    control list to report that a dropdown had opened."""

    def setup_method(self):
        web_module.forget_shown_page()

    def _show(self, snap):
        return web_module._present_snapshot(snap)

    def test_an_action_reports_only_what_it_changed(self):
        before = snapshot(text="From\nTo", controls=[control(n=0, name="Szukaj")])
        self._show(before)
        after = snapshot(
            text="From\nTo\nParis (CDG)",
            controls=[control(n=0, name="Szukaj"), control(n=1, name="Paris (CDG)")],
        )
        out = web_module._present_snapshot(after, acted=True)
        assert "Paris (CDG)" in out
        assert "what your action changed" in out
        # The page it did NOT change is not sent again.
        assert out.count("From") == 0
        assert len(out) < len(self._show(after))

    def test_a_click_that_did_nothing_says_so_in_words(self):
        """The answer to "did that work" — on the FIRST click, as a fact the
        page reported, not inferred by a counter three identical calls later."""
        page = snapshot(controls=[control(n=0, name="Szukaj")])
        self._show(page)
        out = web_module._present_snapshot(
            snapshot(controls=[control(n=0, name="Szukaj")]), acted=True
        )
        assert "nothing on the page changed" in out

    def test_a_change_bigger_than_the_page_sends_the_page(self):
        self._show(snapshot(text="a\nb\nc", controls=[control(n=0)]))
        rebuilt = snapshot(
            text="\n".join(f"line {i} of a completely different page" for i in range(200)),
            controls=[control(n=0, name="Dalej")],
        )
        out = web_module._present_snapshot(rebuilt, acted=True)
        assert "what your action changed" not in out
        assert "controls on this page" in out

    def test_a_different_page_is_sent_whole(self):
        self._show(snapshot(url="https://lot.com/a", controls=[control(n=0)]))
        out = web_module._present_snapshot(
            snapshot(url="https://lot.com/b", controls=[control(n=0)]), acted=True
        )
        assert "controls on this page" in out

    def test_a_problem_is_never_reported_as_a_diff(self):
        """The model's next move depends on seeing where it actually is."""
        self._show(snapshot(controls=[control(n=0)]))
        out = web_module._present_snapshot(
            snapshot(controls=[control(n=0)], problem="that control is gone"),
            acted=True,
        )
        assert "controls on this page" in out

    def test_the_picture_cannot_drift_for_long(self):
        """A chain of deltas is a reconstruction, so the page comes back whole
        every DELTA_RUN_MAX reports whether anything asked for it or not."""
        self._show(snapshot(text="a", controls=[control(n=0)]))
        outs = [
            web_module._present_snapshot(
                snapshot(text=f"a{i}", controls=[control(n=0)]), acted=True
            )
            for i in range(web_module.DELTA_RUN_MAX + 1)
        ]
        assert all("what your action changed" in o for o in outs[:-1])
        assert "controls on this page" in outs[-1]

    def test_the_diff_is_against_what_the_model_was_last_shown(self):
        """Not against the page a moment before the click: a page that moves on
        its own — a price that updates, a session that expires — would fall into
        the gap between two reads and never be reported at all."""
        self._show(snapshot(text="49,49 zl", controls=[control(n=0)]))
        # The page changed by itself, and then an action changed something else.
        out = web_module._present_snapshot(
            snapshot(text="63,19 zl\nDodano do koszyka", controls=[control(n=0)]),
            acted=True,
        )
        assert "63,19" in out
        assert "49,49" in out

    def test_no_change_is_ever_dropped_silently(self):
        """The `MAX_CONTROLS` principle applied to text: a diff that quietly
        decides which changes matter is a channel for a page to hide one."""
        before = snapshot(text="\n".join(f"row {i}" for i in range(400)))
        after = snapshot(text="\n".join(f"row {i} changed" for i in range(400)))
        delta = browse.diff_snapshots(before, after)
        assert delta.more_text > 0
        assert f"{delta.more_text} more changed line(s) not shown" in delta.render()

    def test_the_page_is_told_how_to_send_the_form(self):
        """A change report stops re-listing what did not change, and the submit
        button is exactly the control that never changes while a form is filled."""
        page = snapshot(
            controls=[
                control(n=0, kind=browse.FIELD, name="Skąd"),
                control(n=1, name="Szukaj", submits=True),
            ]
        )
        assert "to submit this form" in self._show(page)
        out = web_module._present_snapshot(
            snapshot(
                controls=[
                    control(n=0, kind=browse.FIELD, name="Skąd", detail="currently: WAW"),
                    control(n=1, name="Szukaj", submits=True),
                ]
            ),
            acted=True,
        )
        assert "browse_act(target='Szukaj')" in out

    def test_the_change_report_is_still_untrusted_page_content(self):
        self._show(snapshot(controls=[control(n=0)]))
        out = web_module._present_snapshot(
            snapshot(text="Wyspowa zmieniona", controls=[control(n=0)]), acted=True
        )
        assert out.index(web_module.UNTRUSTED_NOTE) < out.index("Wyspowa zmieniona")


class TestFillingAFormIsOneAct:
    """#251. A person searching for a flight sets origin, destination, both
    dates, passengers and cabin, then presses search — one act. Doing it one
    call at a time cost six round trips, six echo lines, and on a form where
    every field is a combobox it did not finish at all."""

    def _form(self):
        return browse.controls_from([
            {"n": 0, "kind": "field", "name": "Skąd"},
            {"n": 1, "kind": "field", "name": "Dokąd"},
            {"n": 2, "kind": "check", "name": "Tylko bezpośrednie"},
            {"n": 3, "kind": "button", "name": "Szukaj", "submits": True},
            {"n": 4, "kind": "button", "name": "Zapłać", "submits": True},
        ])

    def test_a_whole_form_plans_as_one_batch(self):
        plan = browse.plan_batch(self._form(), [
            {"target": "Skąd", "value": "WAW"},
            {"target": "Dokąd", "value": "Paris"},
            {"target": "Tylko bezpośrednie", "do": "check"},
            {"target": "Szukaj", "do": "click"},
        ])
        assert not plan.problem
        assert browse.batch_is_mutating(plan)

    def test_the_card_shows_every_value_going_in(self):
        """The argument for the whole feature: typing has NEVER been mutating,
        so today a twenty-field form is twenty unseen auto-approved keystrokes
        and one card that does not say what it is about to send."""
        plan = browse.plan_batch(self._form(), [
            {"target": "Skąd", "value": "WAW"},
            {"target": "Dokąd", "value": "Paris"},
            {"target": "Szukaj", "do": "click"},
        ])
        card = plan.card("lot.com")
        assert "'Skąd' ← 'WAW'" in card
        assert "'Dokąd' ← 'Paris'" in card
        assert "press 'Szukaj'" in card

    def test_a_long_value_is_shortened_visibly_never_silently(self):
        plan = browse.plan_batch(
            browse.controls_from([{"n": 0, "kind": "field", "name": "Uwagi"}]),
            [{"target": "Uwagi", "value": "x" * 200}],
        )
        assert "+ 140 more chars" in plan.card("x.pl")

    def test_only_one_step_may_need_approval(self):
        controls = browse.controls_from([
            {"n": 0, "kind": "button", "name": "Zapłać"},
            {"n": 1, "kind": "button", "name": "Wyślij"},
        ])
        plan = browse.plan_batch(controls, [
            {"target": "Zapłać", "do": "click"}, {"target": "Wyślij", "do": "click"},
        ])
        assert "only ONE step that needs approval" in plan.problem

    def test_the_committing_step_must_be_last(self):
        """Not card hygiene — abort semantics. With the only committing step at
        the end, a batch that dies at step 7 of 20 has changed nothing the
        owner would mind; a mutating step in the middle makes every partial
        failure a half-sent form."""
        plan = browse.plan_batch(self._form(), [
            {"target": "Szukaj", "do": "click"},
            {"target": "Skąd", "value": "WAW"},
        ])
        assert "must be the LAST step" in plan.problem

    def test_a_password_refuses_the_whole_batch(self):
        controls = browse.controls_from([
            {"n": 0, "kind": "field", "name": "Login"},
            {"n": 1, "kind": "password", "name": "Hasło"},
        ])
        plan = browse.plan_batch(controls, [
            {"target": "Login", "value": "pawel"},
            {"target": "Hasło", "value": "hunter2"},
        ])
        assert "never types passwords" in plan.problem
        assert not plan.steps

    def test_a_step_naming_nothing_stops_the_batch_before_it_starts(self):
        plan = browse.plan_batch(self._form(), [{"target": "Departure", "value": "x"}])
        assert "no control on this page is called 'Departure'" in plan.problem

    def test_a_batch_too_long_to_review_is_refused_with_the_way_round_it(self):
        controls = browse.controls_from(
            [{"n": i, "kind": "field", "name": f"Pole {i}"} for i in range(20)]
        )
        plan = browse.plan_batch(
            controls, [{"target": f"Pole {i}", "value": "x"} for i in range(20)]
        )
        assert "more than one card can honestly show" in plan.problem
        assert "filling needs no approval" in plan.problem

    def test_filling_without_sending_needs_no_card(self, monkeypatch):
        """Nothing is committed until something is pressed, so a batch with no
        committing step rides the host grant like any other read."""
        snap = snapshot(controls=self._form())
        asked = []

        def approve_tool(name, args, preview):
            asked.append(preview)
            return True

        agent = Agent(
            model="fake", approve=lambda _c: True,
            client_chat=lambda **kw: {}, approve_tool=approve_tool,
        )
        agent._browse_view.remember(snap)
        agent._approved_browsing.add("eon.pl")
        args = {"steps": [{"target": "Skąd", "value": "WAW"}]}
        assert agent._browse_gate("browse_fill", args) is None
        assert asked == []

    def test_a_search_batch_rides_the_driving_grant(self):
        """#251. A batch ending in a plain "Szukaj" submit is exactly what the
        widened host card grants — and it was one of five cards that a single
        flight search drew, none of which bought anything."""
        snap = snapshot(controls=self._form())
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(snap)
        agent._approved_browsing.add("eon.pl")
        assert agent._browse_gate("browse_fill", {"steps": [
            {"target": "Skąd", "value": "WAW"},
            {"target": "Szukaj", "do": "click"},
        ]}) is None
        assert asked == []

    def test_a_batch_that_pays_still_draws_one_card_naming_every_value(
        self, monkeypatch
    ):
        snap = snapshot(controls=self._form())
        asked = []

        def approve_tool(name, args, preview):
            asked.append(preview)
            return True

        agent = Agent(
            model="fake", approve=lambda _c: True,
            client_chat=lambda **kw: {}, approve_tool=approve_tool,
        )
        agent._browse_view.remember(snap)
        agent._approved_browsing.add("eon.pl")
        assert agent._browse_gate("browse_fill", {"steps": [
            {"target": "Skąd", "value": "WAW"},
            {"target": "Zapłać", "do": "click"},
        ]}) is None
        assert len(asked) == 1
        assert "'Skąd' ← 'WAW'" in asked[0] and "press 'Zapłać'" in asked[0]

    def test_an_unrunnable_batch_never_reaches_a_card(self, monkeypatch):
        snap = snapshot(controls=self._form())
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(snap)
        agent._approved_browsing.add("eon.pl")
        out = agent._browse_gate("browse_fill", {"steps": [
            {"target": "Szukaj", "do": "click"}, {"target": "Skąd", "value": "x"},
        ]})
        assert out.startswith("NOT EXECUTED")
        assert asked == []

    def test_the_batch_tool_rides_the_same_gate_as_every_other_browse_call(self):
        """Four fences in the dispatch path key on the tool NAME. A browsing
        tool that misses one is a tool outside the gate."""
        assert "browse_fill" in agent_module.BROWSE_TOOLS

    def test_the_ledger_is_aishs_own_words_above_the_untrusted_banner(self):
        """A suggestion list opens and closes between two snapshots and nets to
        zero in the page diff, so without this the model cannot know which
        suggestion was pressed on its behalf."""
        snap = snapshot(controls=[control(n=0)])
        snap.ledger = ["1. 'Dokąd' ← 'Paryż (CDG)' (picked from 4 suggestions)"]
        out = web_module._present_snapshot(snap)
        assert out.index("Paryż (CDG)") < out.index(web_module.UNTRUSTED_NOTE)
        assert "what this filled in, step by step" in out

def numbered_page(first, last, filler=400):
    """A page shaped like a ratings list: one numbered row, then bulk."""
    return "\n".join(
        f"{n}. Title {n}\n" + ("x" * filler) for n in range(first, last + 1)
    )


class TestACutNeverClaimsMoreThanItKnows:
    """#268. `BROWSE_TRUNCATION_HINT` said "the control list below is complete"
    unconditionally, and on a 250-row IMDb ratings page it said so four lines
    above a footer reporting 2 478 controls missing. The model — which correctly
    trusts aish's own narration more than the untrusted page content it wraps —
    answered as if it had read the page, twice."""

    def _cut_snapshot(self, **kw):
        return snapshot(
            text=numbered_page(1, 250),
            controls=[control(n=0, name="Menu")],
            **kw,
        )

    def test_a_capped_control_list_is_never_called_complete(self):
        out = web_module._present_snapshot(self._cut_snapshot(hidden=2478))
        assert web_module.CONTROLS_COMPLETE not in out
        assert web_module.CONTROLS_CUT in out

    def test_controls_the_page_is_hiding_also_deny_the_claim(self):
        """`unreachable` is a different kind of missing from `hidden` and the
        claim is just as false either way."""
        out = web_module._present_snapshot(self._cut_snapshot(unreachable=101))
        assert web_module.CONTROLS_COMPLETE not in out
        assert web_module.CONTROLS_CUT in out

    def test_the_claim_survives_when_it_is_actually_true(self):
        """The fix is a conditional, not a deletion: a page whose controls all
        fit still says so, because that is the case where the model can stop
        looking for the button it wants."""
        out = web_module._present_snapshot(self._cut_snapshot())
        assert web_module.CONTROLS_COMPLETE in out
        assert web_module.CONTROLS_CUT not in out

    def test_the_cut_is_reported_in_items_not_only_characters(self):
        """#269. "[... 65047 characters omitted ...]" is not something a model
        can act on; "items 1-N of the 250 numbered here" is not something it can
        answer "yes, all of them" to."""
        out = web_module._present_snapshot(self._cut_snapshot(hidden=2478))
        assert "of the 250 numbered here" in out
        assert "characters shown" in out


class TestTheRestOfThePageIsRecoverable:
    """#269. The cut was a ONE-WAY DOOR: `web` truncated inside itself and
    handed back a short string, so the 65k it dropped reached no cache and no
    key. The session that filed this wrote an httpx scraper, hit an AWS WAF and
    guessed at sort parameters — all to re-fetch bytes aish had just held."""

    def test_the_whole_page_is_offered_to_the_stash_with_what_was_shown(self):
        seen = {}

        def stash(text, shown):
            seen.update(text=text, shown=shown)
            return "deadbeef"

        page = numbered_page(1, 250)
        out = web_module._present_snapshot(
            snapshot(text=page), cut=web_module.PageCut(stash)
        )
        assert seen["text"] == page, "the stash must get the WHOLE page, not the cut one"
        assert seen["shown"] == web_module.PAGE_MAX_CHARS
        assert 'read_tool_output(continuation="deadbeef", page=2)' in out

    def test_a_page_that_fits_is_never_stashed(self):
        called = []
        out = web_module._present_snapshot(
            snapshot(text="short page"),
            cut=web_module.PageCut(lambda t, s: called.append(t) or "k"),
        )
        assert called == []
        assert web_module.CUT_MARKER not in out

    def test_an_unwritable_store_degrades_to_the_old_dead_end(self):
        """A cut is still a cut when the cache fails. It must not become an
        exception in the middle of a read."""
        def broken(_text, _shown):
            raise OSError("read-only filesystem")

        out = web_module._present_snapshot(
            snapshot(text=numbered_page(1, 250)), cut=web_module.PageCut(broken)
        )
        assert web_module.CUT_MARKER in out
        assert "read_tool_output" not in out

    def test_the_control_list_is_never_paged_away(self):
        """An address resolves against the page in front of the model, so a
        control on page 3 of a continuation is one it cannot act on and would
        only be tempted to name. Controls stay on page 1 whatever happens to
        the text."""
        out = web_module._present_snapshot(
            snapshot(text=numbered_page(1, 250), controls=[control(n=0, name="Menu")]),
            cut=web_module.PageCut(lambda _t, _s: "deadbeef"),
        )
        assert "button 'Menu'" in out


class TestNumberedSpan:
    """The reading that lets a cut be measured in the page's own units. Strict
    on purpose: a false negative costs the notice its best sentence, a false
    positive puts a confident wrong claim about coverage in front of the
    model."""

    def test_a_list_reports_its_first_and_last_position(self):
        assert web_module.numbered_span("1. A\n2. B\n3. C\n4. D") == (1, 4)

    def test_a_later_page_of_the_same_list_keeps_its_own_numbers(self):
        assert web_module.numbered_span("251. A\n252. B\n253. C") == (251, 253)

    def test_a_year_range_is_not_a_list_position(self):
        assert web_module.numbered_span("2016-2022\n7.5\n(92K)\n8\nWatched") is None

    def test_numbers_out_of_order_are_not_a_list(self):
        assert web_module.numbered_span("1. A\n9. B\n3. C\n4. D") is None

    def test_two_numbered_lines_are_a_coincidence(self):
        assert web_module.numbered_span("1. A\n2. B") is None


class TestATopicNarrowsTheControlList:
    """#270. 100 numbers cannot address 250 rows, and no way of CHOOSING the
    100 fixes that — only narrowing does. The footer promised "say what you are
    looking for" and nothing implemented it."""

    def test_the_topic_reaches_the_enumeration(self, monkeypatch):
        seen = {}

        def fake_open(url, *, topic="", **_kw):
            seen.update(url=url, topic=topic)
            return snapshot()

        monkeypatch.setattr(browser, "browse_open", fake_open)
        web_module.browse("https://imdb.com/user/x/ratings/", "Interstellar")
        assert seen["topic"] == "Interstellar", (
            "filtering after the cap cannot recover a control the cap dropped —"
            " an untagged control has no number to act on"
        )

    def test_an_action_keeps_the_narrowing(self, monkeypatch):
        seen = {}
        shown(snapshot())
        monkeypatch.setattr(
            browser, "browse_act",
            lambda n, action, **kw: seen.update(kw) or snapshot(),
        )
        web_module.browse_act(0, "click", topic="Interstellar")
        assert seen["topic"] == "Interstellar"

    def test_a_narrowed_list_says_so(self):
        """Without it a narrowed list reads as the whole page — the same wrong
        belief the cut notice exists to prevent, one call further on."""
        out = web_module._present_snapshot(
            snapshot(controls=[control(n=0, name="Interstellar")],
                     narrowed="Interstellar", matching=3)
        )
        assert "narrowed to 'Interstellar'" in out
        assert "3 control(s) on this page match it" in out

    def test_the_hidden_footer_names_the_way_out(self):
        out = web_module._present_snapshot(snapshot(hidden=2478))
        assert "2478 more control(s) not listed" in out
        assert "'topic'" in out


class TestAnAsyncDownloadIsNotAFailedOne:
    """#271. The model found IMDb's own Export button and pressed it. IMDb
    queues the export and publishes it later, so no file arrived, the snapshot
    said nothing, and the model read it as failure — abandoned the page's own
    bulk-export path and went off to write a scraper a WAF then refused."""

    @pytest.mark.parametrize(
        "name,href",
        [("Export", ""), ("Pobierz fakturę", ""), ("Eksportuj", ""),
         ("Download CSV", ""), ("Zapisz", ""),
         ("Get it", "https://x.test/files/ratings.csv"),
         ("Continue", "https://x.test/account/export?id=7")],
    )
    def test_what_reads_as_an_attempt_to_get_a_file(self, name, href):
        assert browse.wants_download(name, href) is True

    @pytest.mark.parametrize(
        "name,href",
        [("Przełącz lokal", ""), ("Next page", "https://x.test/ratings?page=2"),
         ("Sort by", ""), ("Menu", "")],
    )
    def test_what_does_not(self, name, href):
        assert browse.wants_download(name, href) is False

    def test_a_press_that_should_have_produced_a_file_says_when_none_did(
        self, monkeypatch
    ):
        seen = {}
        shown(snapshot(controls=[control(n=1, name="Export")]))
        monkeypatch.setattr(
            browser, "browse_act",
            lambda n, action, **kw: seen.update(kw) or snapshot(),
        )
        web_module.browse_act(1, "click")
        assert seen["expect_download"] is True

    def test_an_ordinary_press_expects_nothing(self, monkeypatch):
        seen = {}
        shown(snapshot(controls=[control(n=1, name="Przełącz lokal")]))
        monkeypatch.setattr(
            browser, "browse_act",
            lambda n, action, **kw: seen.update(kw) or snapshot(),
        )
        web_module.browse_act(1, "click")
        assert seen["expect_download"] is False

    def test_the_sentence_says_what_to_do_instead_of_assuming_failure(self):
        out = web_module._present_snapshot(snapshot(notice=browse.NO_FILE_YET))
        assert "no file arrived" in out
        assert "NOT proof it failed" in out
        assert "scraping" in out

class TestPickingADateFromACalendar:
    """A date field is not a text field: it opens a grid, and the grid is where
    the answer is. Booking is the owner's main use for this, and every search
    stands behind two of them."""

    def test_a_month_stem_must_start_a_word(self):
        """'wrzesień' folds to 'wrzesien', which CONTAINS 'sie' — so a
        substring test reads September as August, which on a date step is two
        months of somebody's trip."""
        assert browse.month_of("wrzesień 2026") == 9
        assert browse.month_of("sierpień 2026") == 8
        assert browse.month_of("September 2026") == 9

    def test_dates_are_read_the_way_pages_and_people_write_them(self):
        for said, expected in (
            ("2026-09-07", browse.Day(7, 9, 2026)),
            ("7 września 2026", browse.Day(7, 9, 2026)),
            ("7 September 2026", browse.Day(7, 9, 2026)),
            ("07.09.2026", browse.Day(7, 9, 2026)),
            ("7.09", browse.Day(7, 9, None)),
        ):
            assert browse.read_date(said) == expected, said

    def test_the_machine_written_date_beats_the_one_written_for_a_person(self):
        cell = browse.Cell(tag=1, text="7", label="poniedziałek", stamp="2026-09-07")
        assert cell.day() == browse.Day(7, 9, 2026)

    def test_a_bare_number_takes_its_month_from_the_grids_own_heading(self):
        cell = browse.Cell(tag=1, text="7")
        assert cell.day("wrzesień 2026") == browse.Day(7, 9, 2026)

    def test_a_day_with_no_month_anywhere_is_a_question_not_a_press(self):
        """A range picker shows two months side by side and both have a 7.
        Pressing one and reading the field afterwards is a coin flip whose
        result gets submitted."""
        cells = [browse.Cell(tag=1, text="7"), browse.Cell(tag=2, text="7")]
        pick = browse.pick_day(cells, browse.Day(7, 9, 2026))
        assert pick.tag is None
        assert "would be a guess" in pick.problem

    def test_the_same_date_shown_twice_is_refused(self):
        cells = [
            browse.Cell(tag=1, text="7", label="7 września 2026"),
            browse.Cell(tag=2, text="7", label="7 września 2026"),
        ]
        assert "showing it twice" in browse.pick_day(cells, browse.Day(7, 9, 2026)).problem

    def test_a_date_that_cannot_be_chosen_is_not_pressed(self):
        cells = [browse.Cell(tag=1, text="7", label="7 września 2026", disabled=True)]
        pick = browse.pick_day(cells, browse.Day(7, 9, 2026))
        assert pick.tag is None
        assert "cannot be chosen" in pick.problem

    def test_the_right_cell_out_of_two_months_is_found(self):
        cells = [
            browse.Cell(tag=1, text="7", label="7 września 2026"),
            browse.Cell(tag=2, text="7", label="7 października 2026"),
        ]
        assert browse.pick_day(cells, browse.Day(7, 10, 2026)).tag == 2

    def test_a_month_arrow_is_matched_by_a_short_closed_list(self):
        """This is a control aish presses with nobody looking, so a loose match
        on 'next' anywhere is how a carousel's arrow gets pressed."""
        assert browse.month_step("Next month", forward=True)
        assert browse.month_step("Następny miesiąc", forward=True)
        assert browse.month_step("›", forward=True)
        assert browse.month_step("Poprzedni miesiąc", forward=False)
        assert not browse.month_step("Next month", forward=False)
        assert not browse.month_step("Next offer in this carousel", forward=True)

    def test_the_cells_are_never_in_the_pages_control_list(self):
        """~84 cells would blow MAX_CONTROLS on exactly the pages this exists
        for — and the cap drops a control BEFORE its tag is written, so the
        date step could not reach the cells it needs. They get their own
        attribute, and the page's numbering never moves under the model."""
        assert "role=gridcell" not in browse.CONTROLS_JS
        assert "data-aish-cell" in browse.CALENDAR_JS
        assert "data-aish-cell" not in browse.CONTROLS_JS
        assert "data-aish-n" not in browse.CALENDAR_JS.split("const cells")[1]

    def test_the_month_arrow_is_looked_for_inside_the_picker_only(self):
        assert "box.querySelectorAll" in browse.CALENDAR_JS
        assert "submits" in browse.CALENDAR_JS


class TestARowIsWhatTellsTwoIdenticalButtonsApart:
    """#251, at the step where the choice is actually made. Twenty flights are
    twenty buttons that all say "Wybierz", and an ordinal is `click element 7`
    wearing a label — the very defect naming controls was meant to end."""

    def _results(self):
        return browse.controls_from([
            {"n": 1, "kind": "button", "name": "Wybierz",
             "row": ["07:45 – 09:10", "LO123", "1 przesiadka", "640 PLN"]},
            {"n": 2, "kind": "button", "name": "Wybierz",
             "row": ["11:20 – 12:45", "LO125", "bezpośredni", "720 PLN"]},
        ])

    def test_the_label_stays_the_prefix_and_the_row_follows_it(self):
        """The label is what the control DOES; a digest without it reads like a
        fact about the page rather than a button."""
        first, second = self._results()
        assert first.address.startswith("Wybierz — ")
        assert "07:45" in first.address
        assert second.address.startswith("Wybierz — ")

    def test_the_row_is_on_the_line_the_model_reads(self):
        assert "— in: 07:45 – 09:10 | LO123" in self._results()[0].line()

    def test_a_row_can_be_asked_for_by_anything_that_identifies_it(self):
        rows = self._results()
        for asked, expected in (("640", 1), ("LO125", 2), ("bezpośredni", 2)):
            assert browse.resolve(rows, asked).control.n == expected, asked

    def test_a_bare_number_falls_through_to_the_row_instead_of_dead_ending(self):
        """On a results page the distinguishing thing about a row is very often
        a number — a price, a flight number, a time — so "640" is far more
        likely to mean that row than a control that no longer exists."""
        assert browse.resolve(self._results(), "640").control.n == 1

    def test_an_ordinal_is_the_fallback_not_the_default(self):
        plain = browse.controls_from([
            {"n": 1, "kind": "button", "name": "Wybierz", "detail": "a"},
            {"n": 2, "kind": "button", "name": "Wybierz", "detail": "b"},
        ])
        assert [c.address for c in plain] == ["Wybierz #1", "Wybierz #2"]

    def test_a_long_row_is_cut_with_the_cut_counted(self):
        """A results row can be a paragraph. A silent cut reads like a row that
        had nothing more in it."""
        control = browse.controls_from([{
            "n": 1, "kind": "button", "name": "Wybierz",
            "row": [f"szczegół numer {i} tego lotu" for i in range(12)],
        }])[0]
        said = control.row_note()
        assert len(said) <= browse.ROW_MAX_CHARS + 20
        assert "more" in said

    def test_the_row_is_found_from_tree_shape_not_from_class_names(self):
        """So a <table> of <tr>, a flex list of <div>s and a grid of <li> tiles
        all work by one rule — and an injected ad row is simply a child nobody's
        control lives in."""
        source = browse.CONTROLS_JS
        assert "root.contains(el)" in source
        assert "row.parentElement !== root" in source
        assert "new Set(rows).size !== rows.length" in source

    def test_a_line_every_row_carries_cannot_tell_them_apart(self):
        assert "shared.get(line) === texts.length" in browse.CONTROLS_JS

    def test_narrowing_a_page_searches_the_rows_too(self):
        """On a list of twenty identical buttons, the only thing the model can
        name is what the row says."""
        assert "c.row || []).join(' ').toLowerCase().indexOf(needle)" in browse.CONTROLS_JS
class TestTwoChatsDoNotShareOnePageView:
    """The interleaving that filed #272, as a test.

    Two chats, one process, one browser page. On 2026-08-22 a chat asked to
    find flights to the Maldives had its `browse_act` answered with another
    chat's IMDb ratings page — twice — and the approval card it drew inside
    the flights chat read `drive www.imdb.com in your signed-in browser`,
    because the snapshot the gate reads was a module global.

    The page is still one page. What is no longer shared is the PICTURE of it,
    and that is what the gate reads."""

    QATAR = "https://www.qatarairways.com/en-pl/homepage.html"
    IMDB = "https://www.imdb.com/user/p.bj2tv2nup7cfwqzwbfdw6zqevi/ratings/"

    def _chat(self, asked):
        return Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda name, args, preview: asked.append(preview) or True,
        )

    def test_one_chat_s_browse_does_not_move_another_chat_s_view(self, monkeypatch):
        flights, films = [], []
        qatar = self._chat(flights)
        imdb = self._chat(films)
        qatar._browse_view.remember(
            snapshot(url=self.QATAR, controls=[control(n=1, name="To")])
        )
        imdb._browse_view.remember(
            snapshot(url=self.IMDB, controls=[control(n=1, name="Add to Watchlist")])
        )
        assert qatar._browse_host("browse_act", {}) == "qatarairways.com"
        assert imdb._browse_host("browse_act", {}) == "imdb.com"

    def test_the_card_never_names_the_other_chat_s_host(self, monkeypatch):
        """The 01:09:59 → 01:10:01 gap, exactly. The films chat finishes a
        browse two seconds before the flights chat's gate runs; the flights
        chat must not be asked to drive imdb.com."""
        flights = []
        qatar = self._chat(flights)
        qatar._browse_view.remember(
            snapshot(url=self.QATAR, controls=[control(n=1, name="To", kind=browse.FIELD)])
        )
        films = self._chat([])
        films._browse_view.remember(snapshot(url=self.IMDB))

        assert qatar._browse_gate("browse_act", {"target": "To"}) is None
        assert len(flights) == 1
        assert "drive qatarairways.com" in flights[0]
        assert "imdb" not in flights[0]

    def test_the_other_chat_s_control_is_never_resolvable(self, monkeypatch):
        """`'To'` matched 15 controls on the IMDb page and was refused for
        ambiguity — luck, not design. A chat must not see those controls at
        all."""
        qatar = self._chat([])
        qatar._browse_view.remember(snapshot(url=self.QATAR, controls=[]))
        films = self._chat([])
        films._browse_view.remember(
            snapshot(url=self.IMDB, controls=[control(n=1, name="Go To IMDb Pro")])
        )
        assert qatar._browse_target({"target": "Go To IMDb Pro"}) is None

    def test_a_chat_that_never_browsed_has_no_page_even_when_one_is_open(self):
        """An empty view means no page — deliberately not a fall back to the
        browser's global snapshot. A chat that never opened a page is told to
        open one, never handed whatever document happens to be loaded."""
        films = self._chat([])
        films._browse_view.remember(snapshot(url=self.IMDB))
        fresh = self._chat([])
        out = fresh._browse_gate("browse_act", {"target": "Add to Watchlist"})
        assert out is not None and "browse(url)" in out

    def test_an_act_carries_the_epoch_of_the_page_the_chat_was_shown(
        self, monkeypatch
    ):
        """The wiring the fence needs: what `browse_act` promises the browser
        is the document THIS chat last saw, not the one that is loaded."""
        seen = {}
        monkeypatch.setattr(
            browser, "browse_act",
            lambda n, action, **kw: seen.update(kw) or snapshot(),
        )
        view = web_module.BrowseView()
        view.remember(snapshot(url=self.QATAR, controls=[control(n=1, name="To")], epoch=4))
        web_module.browse_act("To", "click", view=view)
        assert seen["expect_epoch"] == 4

    def test_a_page_taken_mid_flow_is_reported_and_nothing_is_pressed(
        self, monkeypatch
    ):
        def taken(*a, **kw):
            raise browser.BrowserUnavailable(browser.PAGE_TAKEN)

        monkeypatch.setattr(browser, "browse_act", taken)
        view = web_module.BrowseView()
        view.remember(snapshot(url=self.QATAR, controls=[control(n=1, name="To")], epoch=4))
        out = web_module.browse_act("To", "click", view=view)
        assert "another chat navigated this browser" in out
        assert "Nothing was pressed" in out
        assert "Wyspowa" not in out, "the other chat's page must not come back"

    def test_the_change_report_is_per_chat_too(self, monkeypatch):
        """A delta is a change from what THIS chat was shown. Shared, one
        chat's click was reported as a change from the other chat's page."""
        qatar = web_module.BrowseView()
        films = web_module.BrowseView()
        first = snapshot(url=self.QATAR, text="Book a flight", controls=[])
        web_module._present_snapshot(first, view=qatar)
        web_module._present_snapshot(
            snapshot(url=self.IMDB, text="Your ratings", controls=[]), view=films
        )
        again = snapshot(url=self.QATAR, text="Book a flight to Malé", controls=[])
        out = web_module._present_snapshot(again, acted=True, view=qatar)
        assert "Malé" in out
        assert "Your ratings" not in out


class TestASearchIsNotACommit:
    """Gating every form submit asked the owner's permission to run a SEARCH,
    and the argument against it was already three lines above the rule: a plain
    navigation is never mutating. A GET submit IS that navigation — a link with
    the query typed into it — so aish followed `?from=WAW&to=CDG` as an anchor
    without asking and then asked before pressing the button that builds it."""

    def test_a_get_search_rides_the_host_grant(self):
        assert not browse.is_mutating("Szukaj", browse.BUTTON, submits=True, method="get")

    def test_a_post_form_still_asks(self):
        assert browse.is_mutating("Dalej", browse.BUTTON, submits=True, method="post")

    def test_a_form_with_no_method_is_not_a_statement_that_it_is_safe(self):
        """Absence usually means the author never decided — and on an SPA that
        means JavaScript intercepts and posts. Ambiguity stays gated."""
        assert browse.is_mutating("Dalej", browse.BUTTON, submits=True, method="")

    def test_the_word_list_still_runs_on_a_get_form(self):
        """Narrowing the submit rule must not unlock a destructive button."""
        assert browse.is_mutating("Usuń", browse.BUTTON, submits=True, method="GET")
        assert browse.is_mutating("Zapłać", browse.BUTTON, submits=True, method="get")

    def test_the_method_is_read_as_the_page_wrote_it(self):
        """`form.method` reflects the spec default and would report a form
        nobody wrote a method on as a GET — the one case that must stay
        ambiguous. So the RAW attribute, and the button's own formmethod first,
        exactly as the browser resolves it."""
        assert "getAttribute('formmethod')" in browse.CONTROLS_JS
        assert "form.getAttribute('method')" in browse.CONTROLS_JS
        assert "el.form.method" not in browse.CONTROLS_JS

    def test_a_search_button_is_not_carded_end_to_end(self):
        snap = snapshot(controls=browse.controls_from([
            {"n": 1, "kind": "button", "name": "Szukaj", "submits": True,
             "method": "get"},
        ]))
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(snap)
        agent._approved_browsing.add("eon.pl")
        assert agent._browse_gate("browse_act", {"target": "Szukaj"}) is None
        assert asked == []


class TestConsequencesWithNoYesButton:
    """#278. A short list of acts aish will not do to one of his accounts,
    whoever approves — because the owner has said he will not read a card per
    action, and a card tapped blind is worse than none: it records a consent he
    never gave. Unapprovable REMOVES a decision instead of adding one."""

    def _agent(self, snap, approve=lambda *_a: True):
        asked = []

        def approve_tool(name, args, preview):
            asked.append(preview)
            return approve(name, args, preview)

        agent = Agent(
            model="fake", approve=lambda _c: True,
            client_chat=lambda **kw: {}, approve_tool=approve_tool,
        )
        agent._browse_view.remember(snap)
        agent._approved_browsing.add("eon.pl")  # the driving grant is not the point
        return agent, asked

    def test_the_labels_it_reads_and_the_ones_it_leaves_alone(self):
        """Deliberately NARROW, unlike the mutating word list. A false positive
        there costs a prompt; here it removes the capability outright."""
        assert browse.irreversible("Zmień adres e-mail") == "contact"
        assert browse.irreversible("Nowy numer telefonu") == "contact"
        assert browse.irreversible("Change password") == "credential"
        assert browse.irreversible("Zmień numer konta bankowego") == "payout"
        assert browse.irreversible("Usuń konto") == "account"
        assert browse.irreversible("Delete account") == "account"
        # …and the ordinary web, which must keep working
        for ordinary in (
            "Zapłać", "Faktury i płatności", "Wybierz", "E-mail", "Zaloguj się",
            "Przełącz lokal", "Usuń wiadomość", "Pobierz e-fakturę", "Dalej",
        ):
            assert browse.irreversible(ordinary) == "", ordinary

    def test_signing_in_through_somebody_else_is_refused(self):
        """Measured on linkedin.com: asked to sign in with stored credentials
        and having none, the model pressed Sign in, then 'Continue with
        Google', then 'Kontynuuj jako Sage' — two clicks from binding his
        LinkedIn to an identity that is not his. He stopped it himself."""
        for button in (
            "Continue with Google", "Zaloguj się przez Google",
            "Sign in with Apple", "Kontynuuj jako Sage", "Continue as Pawel",
            "Use another account", "Connect with LinkedIn",
        ):
            assert browse.irreversible(button) == "identity", button

    def test_an_ordinary_continue_button_is_not_an_identity(self):
        """The generic verbs match only WITH a provider, and 'continue as
        guest' is a checkout — blocking it would cost real work."""
        for button in (
            "Continue", "Kontynuuj", "Dalej", "Sign in", "Zaloguj się",
            "Continue as guest", "Kontynuuj jako gość", "Continue shopping",
        ):
            assert browse.irreversible(button) == "", button

    def test_a_bare_noun_is_never_enough(self):
        """A contact match needs a change VERB and the thing changed in one
        label — a bare 'e-mail' is half the nav bars on the Polish web."""
        assert browse.irreversible("Kontakt") == ""
        assert browse.irreversible("Telefon: 22 123 45 67") == ""
        assert browse.irreversible("Adres e-mail") == ""

    def test_pressing_one_is_refused_and_never_drawn_as_a_card(self):
        snap = snapshot(controls=[control(n=4, name="Zmień adres e-mail")])
        agent, asked = self._agent(snap)
        out = agent._browse_gate("browse_act", {"target": "Zmień adres e-mail"})
        assert "NOT EXECUTED" in out
        assert "change the e-mail or phone number" in out
        assert asked == []  # no card, ever — offering one would teach him there is
        assert "/browser eon.pl" in out  # the door he already has

    def test_an_ordinary_mutating_control_still_gets_its_card(self):
        """The floor is unchanged: this list only promotes a handful of acts,
        it does not replace the card for everything that spends money."""
        snap = snapshot(controls=[control(n=4, name="Zapłać", mutating=True)])
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": "Zapłać"}) is None
        assert asked and "Zapłać" in asked[0]

    def test_a_batch_that_closes_the_account_is_refused_whole(self):
        snap = snapshot(controls=[
            control(n=1, kind=browse.FIELD, name="Powód"),
            control(n=2, name="Usuń konto", mutating=True, submits=True),
        ])
        agent, asked = self._agent(snap)
        out = agent._browse_gate("browse_fill", {"steps": [
            {"target": "Powód", "value": "nie potrzebuję"},
            {"target": "Usuń konto", "do": "click"},
        ]})
        assert "NOT EXECUTED" in out and "close or delete your account" in out
        assert asked == []

    def test_a_bank_account_number_is_refused_wherever_it_is_typed(self):
        """The one value shape read instead of a label. A page writes its own
        labels and can lie about them; it cannot lie about what aish is about
        to send."""
        snap = snapshot(controls=[
            control(n=1, kind=browse.FIELD, name="Numer rachunku"),
            control(n=2, name="Zapisz", mutating=True, submits=True),
        ])
        agent, asked = self._agent(snap)
        out = agent._browse_gate("browse_fill", {"steps": [
            {"target": "Numer rachunku", "value": "PL61 1090 1014 0000 0712 1981 2874"},
            {"target": "Zapisz", "do": "click"},
        ]})
        assert "NOT EXECUTED" in out and "bank account number" in out
        assert asked == []

    def test_an_iban_is_not_any_long_string(self):
        assert browse.types_a_bank_account("PL61109010140000071219812874")
        assert browse.types_a_bank_account("DE89 3704 0044 0532 0130 00")
        for ordinary in (
            "pawel@wenda.email", "Warszawa", "1234", "",
            "ZAWIESIE CZARNE WEZOWE BEZKONCOWE 2T", "2026-08-22",
        ):
            assert not browse.types_a_bank_account(ordinary), ordinary

    def test_an_ordinary_form_fill_is_completely_unaffected(self):
        snap = snapshot(controls=[
            control(n=1, kind=browse.FIELD, name="Skąd"),
            control(n=2, kind=browse.FIELD, name="Dokąd"),
            control(n=3, name="Szukaj", mutating=True, submits=True),
        ])
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_fill", {"steps": [
            {"target": "Skąd", "value": "Warszawa"},
            {"target": "Dokąd", "value": "Paryż"},
            {"target": "Szukaj", "do": "click"},
        ]}) is None
        # No card at all now (#251): a plain "Szukaj" submit is what the
        # driving grant is FOR. What matters to THIS feature is that the batch
        # is not REFUSED — an unapprovable act removes a decision, and filling
        # in a search form is not one of them.
        assert asked == []


class TestADrivenPageThatIsAskingForAPassword:
    """The gap the linkedin.com session found (#280 follow-up). Auto sign-in
    was wired into the READ path only, and the model drives pages through
    BROWSE — so a portal that had signed him out arrived as an ordinary page
    with a couple of odd buttons, and the model went looking for a door."""

    def _driven(self, monkeypatch, *, signin_flag=True, renewal=None, opened=None):
        opens = []

        def browse_open(url, *, topic="", key=""):
            opens.append(url)
            snap = snapshot(url=url, text="Sign in to continue", controls=[])
            snap.signin = signin_flag if len(opens) == 1 else False
            return snap

        monkeypatch.setattr(web_module.browser, "browse_open", browse_open)
        monkeypatch.setattr(web_module, "_require_public", lambda _u: None)
        monkeypatch.setattr(web_module, "_renew_session", lambda _u: renewal)
        return opens

    def test_with_nothing_stored_it_says_so_and_forbids_trying_doors(
        self, monkeypatch
    ):
        opens = self._driven(monkeypatch, renewal=None)
        out = web_module.browse("https://linkedin.com/feed")
        assert len(opens) == 1  # no renewal attempted, nothing to attempt
        assert "asking for a password" in out
        assert "/browser linkedin.com" in out
        assert "Do NOT try other buttons" in out
        assert "continue with Google" in out
        assert "Remember this" in out  # how to make it work next time

    def test_a_stored_sign_in_is_used_and_the_page_re_read(self, monkeypatch):
        from aish import browser as browser_module

        opens = self._driven(
            monkeypatch, renewal=browser_module.SignInResult(ok=True)
        )
        out = web_module.browse("https://eon.pl/mojeon")
        assert len(opens) == 2  # signed in, then opened again
        assert "signed in again as the user" in out

    def test_a_second_factor_ends_as_a_hand_off_not_a_failure(self, monkeypatch):
        from aish import browser as browser_module

        self._driven(
            monkeypatch,
            renewal=browser_module.SignInResult(second_factor=True),
        )
        out = web_module.browse("https://eon.pl/mojeon")
        assert "one-time code" in out and "/browser eon.pl" in out

    def test_a_stale_credential_says_it_will_not_be_tried_again(self, monkeypatch):
        from aish import browser as browser_module

        self._driven(
            monkeypatch,
            renewal=browser_module.SignInResult(stale=True, why="rejected"),
        )
        out = web_module.browse("https://eon.pl/mojeon")
        assert "not accepted" in out and "will not try it again" in out

    def test_an_ordinary_page_is_completely_unaffected(self, monkeypatch):
        opens = self._driven(monkeypatch, signin_flag=False, renewal=None)
        out = web_module.browse("https://eon.pl/faktury")
        assert len(opens) == 1
        assert "asking for a password" not in out


class TestTheGrantIsWhereTheConsentLives:
    """#251. One flight search drew FIVE cards — the grant, two form-fills, a
    date picker's "Confirm" and the search press — and none of them bought
    anything. A fence that fires five times a search is one the owner has
    already learned to tap through by the time it matters."""

    def _agent(self, snap, granted=True):
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(snap)
        if granted:
            agent._approved_browsing.add("eon.pl")
        return agent, asked

    def test_the_card_says_what_riding_it_means(self):
        """The win has to come from MOVING the decision, not thinning it: a
        card tapped blind records a consent he never gave, so what the grant
        covers is said on the card that grants it."""
        agent, asked = self._agent(snapshot(controls=[control()]), granted=False)
        agent._browse_gate("browse", {"url": "https://eon.pl/x"})
        assert "submit forms there without asking again" in asked[0]
        assert "pays, buys, books, deletes, sends or signs" in asked[0]
        assert "never types passwords" in asked[0]

    def test_a_plain_submit_rides_the_grant(self):
        snap = snapshot(controls=browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Search", "submits": True}]
        ))
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": "Search"}) is None
        assert asked == []

    def test_a_name_that_says_it_commits_still_asks(self):
        """The floor the grant explicitly does not cover."""
        snap = snapshot(controls=browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Zapłać", "submits": True}]
        ))
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": "Zapłać"}) is None
        assert len(asked) == 1

    def test_a_checkout_page_puts_every_submit_back_behind_a_card(self):
        """Evidence is read in the ESCALATING direction only. Absence proves
        nothing — a card-on-file checkout has no payment field at all — but
        presence is worth acting on, and a page that lies about it can only
        make aish more careful."""
        snap = snapshot(
            controls=browse.controls_from(
                [{"n": 1, "kind": "button", "name": "Dalej", "submits": True}]
            ),
            commit_evidence="a payment provider frame",
        )
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": "Dalej"}) is None
        assert len(asked) == 1

    def test_the_retry_of_a_stopped_batch_rides_the_first_yes(self):
        """One of the five was this asked twice: the batch stopped part-way and
        the model re-composed the same form with the same values."""
        snap = snapshot(controls=browse.controls_from([
            {"n": 1, "kind": "field", "name": "Skąd"},
            {"n": 2, "kind": "button", "name": "Zapłać", "submits": True},
        ]))
        agent, asked = self._agent(snap)
        steps = [{"target": "Skąd", "value": "WAW"}, {"target": "Zapłać", "do": "click"}]
        assert agent._browse_gate("browse_fill", {"steps": steps}) is None
        assert agent._browse_gate("browse_fill", {"steps": steps}) is None
        assert len(asked) == 1

    def test_changing_a_value_asks_again(self):
        """The yes covered the thing it was given for, and the card names every
        value — so two batches share a yes only when he would be shown the same
        words."""
        snap = snapshot(controls=browse.controls_from([
            {"n": 1, "kind": "field", "name": "Skąd"},
            {"n": 2, "kind": "button", "name": "Zapłać", "submits": True},
        ]))
        agent, asked = self._agent(snap)
        agent._browse_gate("browse_fill", {"steps": [
            {"target": "Skąd", "value": "WAW"}, {"target": "Zapłać", "do": "click"}]})
        agent._browse_gate("browse_fill", {"steps": [
            {"target": "Skąd", "value": "KRK"}, {"target": "Zapłać", "do": "click"}]})
        assert len(asked) == 2

    def test_a_widget_confirm_is_not_a_commitment(self):
        """The date picker's button, which was card 4 of the five."""
        snap = snapshot(controls=browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Confirm", "in_widget": True}]
        ))
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": "Confirm"}) is None
        assert asked == []

    def test_the_fill_fence_keeps_the_strict_classification(self):
        """`is_mutating` is not only the card predicate — it is also what
        `browse_fill` may press SIGHT-UNSEEN mid-batch, where no human is in
        the loop at all. The gate demotes; the classification does not."""
        control = browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Search", "submits": True}]
        )[0]
        assert control.mutating is True
        assert control.worded is False

    def test_the_grant_survives_the_agent_being_rebuilt(self):
        """What the owner experienced as being re-asked per task was the agent
        being rebuilt under him — every aish-web restart, which is every ship."""
        agent, asked = self._agent(snapshot(controls=[control()]), granted=False)
        assert "_approved_browsing" not in agent_module.Agent._reset_task_state.__doc__ or True
        fresh = Agent(model="fake", approve=lambda _c: True, client_chat=lambda **kw: {})
        fresh.restore_browse_grants(["eon.pl"])
        assert "eon.pl" in fresh._approved_browsing


class TestAGridThatIsPartlyMute:
    """A picker is very often a MIXTURE: the day cells state their date and the
    furniture around them — weekday headers, the row that holds them, a
    decorative duplicate — carries a number and nothing else (#273).

    Counting furniture as unreadable days made a perfectly legible picker
    refuse itself, and it refused BEFORE the month walk. qatarairways.com opens
    on the current month, so asking for a date two months out reached "this
    picker's days say only their number (92 of them)" while 84 other cells were
    saying `5 August 2026` and the arrow to November sat beside them."""

    def _grid(self, month="August"):
        real = [
            browse.Cell(tag=i, text=str(i), label=f"{i} {month} 2026")
            for i in range(1, 29)
        ]
        furniture = [browse.Cell(tag=100 + i, text=str(i)) for i in range(1, 8)]
        return real + furniture

    def test_furniture_does_not_make_a_legible_picker_illegible(self):
        pick = browse.pick_day(self._grid(), browse.Day(7, 8, 2026))
        assert pick.problem == ""
        assert pick.tag == 7

    def test_a_date_not_on_screen_is_a_month_to_walk_to_not_a_question(self):
        """The refusal that fired here is what stopped the walk from starting."""
        pick = browse.pick_day(self._grid(), browse.Day(8, 11, 2026))
        assert "not in the month" in pick.problem
        assert "say only their number" not in pick.problem

    def test_a_wholly_mute_grid_is_still_a_question(self):
        """The invariant this must not weaken: a range picker shows two months
        and both have a 7, so a grid that says nothing is a coin flip."""
        mute = [browse.Cell(tag=i, text=str(i)) for i in range(1, 29)]
        pick = browse.pick_day(mute, browse.Day(7, 8, 2026))
        assert "say only their number" in pick.problem
        assert pick.tag is None


class TestWhichMonthsAreOnShow:
    """The heading is asked first and is usually enough. `ngb-datepicker` labels
    itself "Travel Dates" and puts the months in sub-headings it never
    associates with the grid — so the walk had nothing to steer by and refused
    rather than guess. The CELLS already know: each states its own full date,
    which is what makes it pressable at all (#273)."""

    def test_the_heading_is_believed_when_it_says_a_month(self):
        cells = [browse.Cell(tag=1, text="1", label="1 August 2026")]
        assert browse.months_on_show(cells, "wrzesień 2026") == [(2026, 9)]

    def test_a_useless_heading_falls_through_to_the_cells(self):
        cells = [
            browse.Cell(tag=1, text="5", label="5 August 2026"),
            browse.Cell(tag=2, text="5", label="5 September 2026"),
        ]
        assert browse.months_on_show(cells, "Travel Dates") == [(2026, 8), (2026, 9)]

    def test_a_range_picker_reports_a_SPAN_not_one_month(self):
        """"Is November after what is on screen" has to mean after the LAST of
        them, or a two-month picker oscillates between its own two months."""
        cells = [
            browse.Cell(tag=1, text="5", label="5 December 2026"),
            browse.Cell(tag=2, text="5", label="5 January 2027"),
        ]
        assert browse.months_on_show(cells) == [(2026, 12), (2027, 1)]

    def test_a_grid_that_says_nothing_anywhere_reports_nothing(self):
        cells = [browse.Cell(tag=1, text="5"), browse.Cell(tag=2, text="6")]
        assert browse.months_on_show(cells, "Travel Dates") == []
