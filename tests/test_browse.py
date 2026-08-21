"""Driving a page: the control list, the labelling, and the gate on pressing.

Nothing here launches Chrome — `browser.browse_open` / `browse_act` are patched,
and conftest's `no_real_browser` makes any escape fail loudly. The one thing that
cannot be faked, whether the JS finds the controls a real page actually has, is
verified against real Chrome by `scripts/verify_browse.py`.
"""

import os
import shutil
import subprocess

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
        assert line == "[7] button 'Przełącz lokal'"

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
        """The numbers are the entire point of the call, so a text cap that fell
        inside them would cut exactly what the model is meant to act on."""
        long_page = snapshot(
            text="x" * (web_module.PAGE_MAX_CHARS + 5000),
            controls=[control(n=0, name="Przełącz lokal")],
        )
        out = web_module._present_snapshot(long_page)
        assert "[0] button 'Przełącz lokal'" in out
        assert "CUT" in out

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
        monkeypatch.setattr(browser, "browse_current", lambda: snap)
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
        monkeypatch.setattr(browser, "browse_current", lambda: snap)
        agent = Agent(model="fake", approve=lambda _c: True, client_chat=lambda **kw: {})
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
        owner.browse_page = page
        owner.browse_touched = touched
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
        """`browse_is_open` answers from a module-level snapshot that used to
        survive the reaper, so it said yes for a page that no longer existed."""
        import asyncio

        browser._LAST_SNAPSHOT = snapshot()
        assert browser.browse_is_open() is True
        asyncio.run(browser._Owner()._close())
        assert browser.browse_is_open() is False


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

        monkeypatch.setattr(
            browser, "browse_current",
            lambda: snapshot(controls=[control(n=2, name="Zapłać", kind=browse.LINK)]),
        )
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
        monkeypatch.setattr(browser, "browse_current", lambda: snapshot(controls=[local]))
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
        out = web_module._present_snapshot(snapshot(text=page), stash=stash)
        assert seen["text"] == page, "the stash must get the WHOLE page, not the cut one"
        assert seen["shown"] == web_module.PAGE_MAX_CHARS
        assert 'read_tool_output(continuation="deadbeef", page=2)' in out

    def test_a_page_that_fits_is_never_stashed(self):
        called = []
        out = web_module._present_snapshot(
            snapshot(text="short page"), stash=lambda t, s: called.append(t) or "k"
        )
        assert called == []
        assert web_module.CUT_MARKER not in out

    def test_an_unwritable_store_degrades_to_the_old_dead_end(self):
        """A cut is still a cut when the cache fails. It must not become an
        exception in the middle of a read."""
        def broken(_text, _shown):
            raise OSError("read-only filesystem")

        out = web_module._present_snapshot(
            snapshot(text=numbered_page(1, 250)), stash=broken
        )
        assert web_module.CUT_MARKER in out
        assert "read_tool_output" not in out

    def test_the_control_list_is_never_paged_away(self):
        """The numbers die with the document, so a control on page 3 of a
        continuation is a control that has already expired. Controls stay on
        page 1 whatever happens to the text."""
        out = web_module._present_snapshot(
            snapshot(text=numbered_page(1, 250), controls=[control(n=0, name="Menu")]),
            stash=lambda _t, _s: "deadbeef",
        )
        assert "[0] button 'Menu'" in out


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
        monkeypatch.setattr(browser, "browse_current", lambda: snapshot())
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
        monkeypatch.setattr(
            browser, "browse_current",
            lambda: snapshot(controls=[control(n=1, name="Export")]),
        )
        monkeypatch.setattr(
            browser, "browse_act",
            lambda n, action, **kw: seen.update(kw) or snapshot(),
        )
        web_module.browse_act(1, "click")
        assert seen["expect_download"] is True

    def test_an_ordinary_press_expects_nothing(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            browser, "browse_current",
            lambda: snapshot(controls=[control(n=1, name="Przełącz lokal")]),
        )
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
