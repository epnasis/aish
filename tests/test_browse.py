"""Driving a page: the control list, the labelling, and the gate on pressing.

Nothing here launches Chrome — `browser.browse_open` / `browse_act` are patched,
and conftest's `no_real_browser` makes any escape fail loudly. The one thing that
cannot be faked, whether the JS finds the controls a real page actually has, is
verified against real Chrome by `scripts/verify_browse.py`.
"""

import os
import pathlib
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
    """Who may act, and what they may press.

    Two questions, not one: may aish use this site as him at all (once per
    site), and may it press THIS control (every time, by name).

    **The first is asked at the first PRESS, not at the open.** Opening a page
    and reading it is what `read_url` does, and reading his account is free —
    so the same page fetched the other way used to ask, which the model could
    sidestep by choosing the other tool. Reading is free whichever way it is
    done; the card is spent on what `read_url` cannot do.

    **And since #295 M4 it is the first press that CHANGES something**, so the
    page here carries both shapes: `Przełącz lokal`, a tab switch that changes
    nothing, and `Dalej`, the nondescript submit that posts the form. The
    grant's own tests drive the second one, because the first no longer asks."""

    PRESSABLE = (
        control(n=0, name="Przełącz lokal"),
        control(n=1, name="Dalej", submits=True),
    )

    def _agent(self, monkeypatch, approve, snap=None):
        asked = []

        def approve_tool(name, args, preview):
            asked.append(preview)
            return approve(name, args, preview)

        agent = Agent(
            model="fake", approve=lambda _c: True,
            client_chat=lambda **kw: {}, approve_tool=approve_tool,
        )
        agent._browse_view.remember(
            snap if snap is not None else snapshot(controls=self.PRESSABLE)
        )
        return agent, asked

    def _press(self, agent, target="Dalej"):
        return agent._browse_gate("browse_act", {"target": target, "action": "click"})

    def test_opening_and_reading_a_page_asks_nothing(self, monkeypatch):
        agent, asked = self._agent(monkeypatch, lambda *a: True)
        assert agent._browse_gate("browse", {"url": "https://eon.pl/mojeon"}) is None
        assert agent._browse_gate(
            "browse_act", {"target": "Przełącz lokal", "action": "read"}
        ) is None
        assert asked == []

    def test_pressing_asks_once_per_site(self, monkeypatch):
        agent, asked = self._agent(monkeypatch, lambda *a: True)
        assert self._press(agent) is None
        assert self._press(agent) is None
        assert len(asked) == 1
        assert "act on eon.pl" in asked[0]

    def test_the_card_says_it_acts_as_the_owner(self, monkeypatch):
        agent, asked = self._agent(monkeypatch, lambda *a: True)
        self._press(agent)
        assert "signed in as you" in asked[0]

    def test_denying_the_site_stops_the_flow(self, monkeypatch):
        agent, _ = self._agent(monkeypatch, lambda *a: False)
        out = self._press(agent)
        # The merged denial (#295 M1), because the card that asked was the
        # merged one: the press the grant was collected on is named in it.
        assert "acting on eon.pl as them was NOT granted" in out
        assert "Reading the site is still allowed" in out
        assert "eon.pl" not in agent._approved_sites

    def test_a_denial_with_a_comment_arms_the_stop_gate(self, monkeypatch):
        agent, _ = self._agent(monkeypatch, lambda *a: Denied("not in my account"))
        out = self._press(agent)
        assert "not in my account" in out
        assert agent._pending_comment_response

    def test_an_approval_with_a_comment_holds_the_action(self, monkeypatch):
        """Approve + comment = continue but ADJUST: the original is never run."""
        agent, _ = self._agent(monkeypatch, lambda *a: Approved("use the other one"))
        out = self._press(agent)
        assert "use the other one" in out

    def test_no_approver_fails_closed_on_a_PRESS_and_not_on_a_read(self):
        """Unattended, aish may still READ his account — that is free by any
        route now — but it may not press anything inside it."""
        agent = Agent(
            model="fake", approve=lambda _c: True,
            client_chat=lambda **kw: {}, approve_tool=None,
        )
        agent._browse_view.remember(snapshot(controls=self.PRESSABLE))
        assert agent._browse_gate("browse", {"url": "https://eon.pl/mojeon"}) is None
        out = self._press(agent)
        assert out.startswith("NOT EXECUTED")
        assert "no approver" in out

    def test_an_ordinary_click_rides_the_host_grant(self, monkeypatch):
        """A flow that clicks through twenty pages of one portal asks once — a
        card per click is a card nobody reads."""
        snap = snapshot(controls=[control(n=3, name="Przełącz lokal")])
        agent, asked = self._agent(monkeypatch, lambda *a: True, snap)
        agent._approved_sites.add("eon.pl")
        assert agent._browse_gate("browse_act", {"target": 3}) is None
        assert asked == []

    def test_a_mutating_click_asks_again_and_names_the_control(self, monkeypatch):
        """'Wyślij' and not 'Zapłać' since #342: a control whose words say it
        PAYS no longer draws a card at all, and the card is what this pins."""
        snap = snapshot(controls=[control(n=5, name="Wyślij")])
        agent, asked = self._agent(monkeypatch, lambda *a: True, snap)
        agent._approved_sites.add("eon.pl")
        assert agent._browse_gate("browse_act", {"target": 5}) is None
        assert len(asked) == 1
        assert "click button 'Wyślij' on eon.pl" in asked[0]

    def test_denying_a_mutating_click_says_nothing_changed(self, monkeypatch):
        snap = snapshot(controls=[control(n=5, name="Wyślij")])
        agent, _ = self._agent(monkeypatch, lambda *a: False, snap)
        agent._approved_sites.add("eon.pl")
        out = agent._browse_gate("browse_act", {"target": 5})
        assert "was NOT clicked and nothing on the page changed" in out
        assert "another control that does the same thing" in out

    def test_a_password_field_is_refused_outright(self, monkeypatch):
        """Structural, not a card: there is no yes that makes this a good idea,
        and a card offering one would teach the owner there is."""
        snap = snapshot(controls=[control(n=1, kind=browse.PASSWORD, name="Hasło")])
        agent, asked = self._agent(monkeypatch, lambda *a: True, snap)
        agent._approved_sites.add("eon.pl")
        out = agent._browse_gate("browse_act", {"target": 1, "action": "type"})
        assert "aish never types passwords" in out
        assert "/browser eon.pl" in out
        assert asked == []          # never even offered

    def test_the_card_names_the_control_the_model_asked_for(self, monkeypatch):
        """What the owner used to read on the card was `browse_act target=15,
        action=click`, and nobody can review "click 15". He decides to press a
        button by reading its label; the card has to carry the same words."""
        snap = snapshot(controls=[control(n=5, name="Wyślij")])
        agent, asked = self._agent(monkeypatch, lambda *a: True, snap)
        agent._approved_sites.add("eon.pl")
        assert agent._browse_gate("browse_act", {"target": "Wyślij"}) is None
        assert "click button 'Wyślij' on eon.pl" in asked[0]

    def test_the_echo_names_the_control_too(self, monkeypatch):
        """The transcript line is the owner's only running account of what aish
        is doing inside his account, once the host grant has been given."""
        snap = snapshot(controls=[control(n=5, name="Zapłać")])
        agent, _ = self._agent(monkeypatch, lambda *a: True, snap)
        label, _thunk = agent._browse_call("browse_act", {"target": "Zapłać"})
        assert label == "→ browse: click button 'Zapłać'"

    def test_acting_with_nothing_open_says_what_to_do(self, monkeypatch):
        agent, _ = self._agent(monkeypatch, lambda *a: True, snapshot())
        agent._browse_view.remember(None)
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

    def test_a_read_that_just_finished_keeps_the_browser(self, monkeypatch):
        """#224. `busy` only says whether a job is running RIGHT NOW, so the
        reaper's tick could land seconds after a read returned and close the
        context under the next one — which then paid the ~2s relaunch. On a
        chat reading every couple of minutes that repeats every tick."""
        monkeypatch.setattr(browser.time, "monotonic", lambda: 1000.0)
        owner = browser._Owner()
        owner.last_used = 1000.0 - 5  # a read finished five seconds ago
        assert owner.reapable(1000.0) is False

    def test_a_browser_quiet_for_the_whole_window_is_collected(self, monkeypatch):
        monkeypatch.setattr(browser.time, "monotonic", lambda: 1000.0)
        owner = browser._Owner()
        owner.last_used = 1000.0 - browser.IDLE_SECONDS - 1
        assert owner.reapable(1000.0) is True

    def test_a_browser_that_never_ran_anything_is_collected(self):
        """`last_used` starts at 0.0 — nothing has ever run, and closing a
        context that was never opened is a no-op, so eager is correct."""
        assert browser._Owner().reapable(1000.0) is True

    def test_a_job_in_flight_still_outranks_the_clock(self, monkeypatch):
        monkeypatch.setattr(browser.time, "monotonic", lambda: 1000.0)
        owner = browser._Owner()
        owner.last_used = 0.0  # long quiet by the clock...
        owner.busy = 1         # ...but something is running
        assert owner.reapable(1000.0) is False

    def test_a_held_session_still_outranks_the_clock(self, monkeypatch):
        """The recency check is an ADDITIONAL reason to keep the browser, never
        a replacement for the two that already existed."""
        monkeypatch.setattr(browser.time, "monotonic", lambda: 1000.0)
        owner = self.owner(page=self.open_page(), touched=999.0)
        owner.last_used = 0.0
        assert owner.held() is True
        assert owner.reapable(1000.0) is False

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

    def test_an_ORDER_to_close_needs_two_signals_that_agree(self):
        """#350, corrected. The page DECLARED something modal and the controls
        are off-screen under a locked scroll — two independent signals, and
        only that combination has earned an imperative."""
        out = web_module._present_snapshot(
            snapshot(unreachable=218, reasons={"behind-a-dialog": 218},
                     dialog="Wybierz daty")
        )
        assert "BEHIND a dialog the page calls 'Wybierz daty'" in out
        assert "CLOSE it to reach them" in out
        assert "Press whatever opens them first" not in out

    def test_the_reason_ALONE_describes_and_does_not_order(self):
        """`behind-a-dialog` is a NAME THAT ASSERTS A CAUSE its own test does
        not check: it fires on "root scroll-locked AND off-screen", and nothing
        in it looks for anything on top.

        Measured in real Chrome — an app shell with `html,body{overflow:hidden}`
        and a footer below the fold, no overlay of any kind, reports
        `{'behind-a-dialog': 2}`. So without corroboration this states what was
        SEEN and offers both repairs, rather than giving an order that is wrong
        on every such page."""
        out = web_module._present_snapshot(
            snapshot(unreachable=2, reasons={"behind-a-dialog": 2})
        )
        assert "scrolling LOCKED and they are off-screen" in out
        assert "if you can see a dialog, panel or menu" in out
        # It must not give the bare order without the page having declared one.
        assert "CLOSE it to reach them" not in out

    def test_inert_is_a_CLOSED_DRAWER_at_least_as_often_as_a_modal(self):
        """`inert` was treated as "behind something open". It is only ever the
        SITE's own attribute, and `<nav inert>` is the recommended pattern for a
        closed drawer — which was getting "close what is open" with nothing
        open. `showModal()` does NOT set it: measured, `closest('[inert]')` is
        null outside the dialog and the outside control is reachable."""
        out = web_module._present_snapshot(
            snapshot(unreachable=3, reasons={"inert": 3})
        )
        assert "Press whatever opens them first" in out
        assert "CLOSE" not in out

    def test_a_genuine_MIX_says_so_rather_than_picking_one(self):
        """Naming one repair when both were observed is the same guess this
        replaced, one rung quieter."""
        out = web_module._present_snapshot(
            snapshot(unreachable=10, reasons={"invisible": 6, "behind-a-dialog": 4})
        )
        assert "some not drawn yet, some off-screen behind a locked scroll" in out

    def test_a_control_the_page_has_not_drawn_still_says_open_something(self):
        out = web_module._present_snapshot(
            snapshot(unreachable=4, reasons={"invisible": 4})
        )
        assert "Press whatever opens them first" in out

    def test_an_AMBIGUOUS_reason_keeps_the_wording_that_names_both(self):
        """`clipped` looks like it means "behind something" and does not: a
        collapsed accordion is `height: 0; overflow: hidden`, so its contents
        are clipped too — REACH_JS says so in its own comment. A reason that
        does not choose must not make the sentence choose."""
        out = web_module._present_snapshot(
            snapshot(unreachable=5, reasons={"clipped": 5})
        )
        assert "Press whatever opens them first" in out
        assert "CLOSE what is open" not in out

    def test_an_unrecognised_reason_degrades_to_the_old_wording(self):
        """A future REACH_JS reason nobody has classified yet must land on the
        sentence that names both possibilities, never on a confident wrong one."""
        out = web_module._present_snapshot(
            snapshot(unreachable=3, reasons={"some-new-reason": 3})
        )
        assert "Press whatever opens them first" in out

    def test_a_log_with_no_reasons_keeps_the_old_wording(self):
        """A snapshot from before #350 records no reasons, and absence must not
        be read as a finding (contract corollary 2)."""
        out = web_module._present_snapshot(snapshot(unreachable=4))
        assert "Press whatever opens them first" in out


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
        # Matched on the CALL rather than on the `if` that used to wrap it:
        # #350 needed the reason as well as the fact, so the shape is now
        # `const why = unreachable(el); if (why) {`. The invariant is the
        # ordering, not the spelling.
        assert walk.index("unreachable(el)") < walk.index("found.push(")
        assert "unreached += 1" in walk and (
            walk.index("unreached += 1") < walk.index("found.push(")
        ), "an unreachable control must be counted and skipped before collection"
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
        agent._approved_sites.add("eon.pl")
        args = {"steps": [{"target": "Skąd", "value": "WAW"}]}
        assert agent._browse_gate("browse_fill", args) is None
        assert asked == []

    def test_a_search_batch_rides_the_driving_grant(self):
        """#251, and THE PROPERTY is unchanged: a batch ending in a plain
        "Szukaj" submit is exactly what the widened host card grants — it was
        one of five cards a single flight search drew, none of which bought
        anything, and it must still draw none.

        Both grants are given below because the search press now also meets the
        address question (#295 M3). That fence has its own tests; this one is
        about the DRIVING grant covering an ordinary search, and it still is."""
        snap = snapshot(controls=self._form())
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(snap)
        agent._approved_sites.add("eon.pl")
        # The send vouch too, since #295 M3: a submit carrying values aish typed
        # is the driven twin of a composed query URL and asks the address
        # question at a host with no vouch. eon.pl is one of the 18 hosts the
        # owner's own approvals seed, so this is the state he is actually in —
        # and what #251 pinned, that the SEARCH itself draws nothing, is what
        # this still pins.
        agent._approved_hosts.add("eon.pl")
        assert agent._browse_gate("browse_fill", {"steps": [
            {"target": "Skąd", "value": "WAW"},
            {"target": "Szukaj", "do": "click"},
        ]}) is None
        assert asked == []

    def test_a_batch_that_commits_still_draws_one_card_naming_every_value(
        self, monkeypatch
    ):
        """The committing press is 'Wyślij' and not 'Zapłać' since #342: a
        control whose words say it PAYS never reaches a card at all now, and
        one card for a whole form is what this pins."""
        snap = snapshot(controls=browse.controls_from([
            {"n": 0, "kind": "field", "name": "Skąd"},
            {"n": 1, "kind": "button", "name": "Wyślij", "submits": True},
        ]))
        asked = []

        def approve_tool(name, args, preview):
            asked.append(preview)
            return True

        agent = Agent(
            model="fake", approve=lambda _c: True,
            client_chat=lambda **kw: {}, approve_tool=approve_tool,
        )
        agent._browse_view.remember(snap)
        agent._approved_sites.add("eon.pl")
        assert agent._browse_gate("browse_fill", {"steps": [
            {"target": "Skąd", "value": "WAW"},
            {"target": "Wyślij", "do": "click"},
        ]}) is None
        assert len(asked) == 1
        assert "'Skąd' ← 'WAW'" in asked[0] and "press 'Wyślij'" in asked[0]

    def test_an_unrunnable_batch_never_reaches_a_card(self, monkeypatch):
        snap = snapshot(controls=self._form())
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(snap)
        agent._approved_sites.add("eon.pl")
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

    def test_the_same_date_shown_twice_is_pressed_not_refused(self):
        """Two cells agreeing on a full date is not the coin flip the refusal
        was written for — that is a cell which does not say which MONTH it is.
        wizzair.com keeps a second copy of every pane for its slide animation
        (138 cells for two months of about 77), and a legible December date
        refused itself."""
        cells = [
            browse.Cell(tag=1, text="7", label="7 września 2026"),
            browse.Cell(tag=2, text="7", label="7 września 2026"),
        ]
        assert browse.pick_day(cells, browse.Day(7, 9, 2026)).tag == 1

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
        # A submit, because since #295 M4 the grant is collected on the press
        # that changes something — a field asks nothing at all now.
        qatar._browse_view.remember(
            snapshot(url=self.QATAR, controls=[control(n=1, name="Dalej", submits=True)])
        )
        films = self._chat([])
        films._browse_view.remember(snapshot(url=self.IMDB))

        assert qatar._browse_gate("browse_act", {"target": "Dalej"}) is None
        assert len(flights) == 1
        assert "act on qatarairways.com" in flights[0]
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
        agent._approved_sites.add("eon.pl")
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
        agent._approved_sites.add("eon.pl")  # the driving grant is not the point
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
        """The floor is unchanged for everything the refusals did not take: a
        'Wyślij' that sends a message is how ordinary work happens, and it
        still asks by name. It was 'Zapłać' until #342 moved that one out of
        the approvable set altogether."""
        snap = snapshot(controls=[control(n=4, name="Wyślij")])
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": "Wyślij"}) is None
        assert asked and "Wyślij" in asked[0]

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

    def test_a_card_number_is_refused_however_the_field_is_labelled(self):
        """#304. The field says "order number" and the value decides anyway —
        that is the whole point of reading the value: no label, placeholder or
        page language is consulted, so none of them can lie past it. No card is
        drawn and no approver verdict can let it through, because the batch is
        refused before anything is offered to anybody."""
        for verdict in (True, False):
            snap = snapshot(controls=[
                control(n=1, kind=browse.FIELD, name="Numer zamówienia"),
                control(n=2, name="Dalej", mutating=True, submits=True),
            ])
            agent, asked = self._agent(snap, approve=lambda *_a, v=verdict: v)
            out = agent._browse_gate("browse_fill", {"steps": [
                {"target": "Numer zamówienia", "value": "4111 1111 1111 1111"},
                {"target": "Dalej", "do": "click"},
            ]})
            assert "NOT EXECUTED" in out and "card number" in out
            assert asked == []
            # It names WHAT it saw and WHERE, and never the value: a refusal
            # that names nothing teaches nothing, and the digits must not
            # survive the check in a message or a trace.
            assert "Numer zamówienia" in out
            for fragment in ("4111", "1111", "4111111111111111"):
                assert fragment not in out, fragment

    def test_spaces_and_dashes_are_the_same_value(self):
        for written in (
            "4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111",
            "4111 1111-1111 1111", "5555 5555 5555 4444",
            "4777777777771", "4777777777777777775",  # the 13 and 19 edges
        ):
            assert browse.types_a_card_number(written), written

    def test_what_the_card_check_leaves_alone(self):
        """Narrow at both ends of the length, and the checksum is what keeps an
        ordinary long number out. A reference number that passes Luhn by chance
        IS refused — accepted, because being wrong here costs a step the user
        finishes himself and can never cost money."""
        for ordinary in (
            "477777777772",          # 12 digits, Luhn-valid — too short
            "47777777777777777774",  # 20 digits, Luhn-valid — too long
            "4111111111111112",      # 16 digits, checksum fails
            "PL61 1090 1014 0000 0712 1981 2874", "2026-08-22", "1234",
            "pawel@wenda.email", "", "Warszawa", "4111 1111 1111 111X",
        ):
            assert not browse.types_a_card_number(ordinary), ordinary

    def test_an_ordinary_form_fill_is_completely_unaffected(self):
        """THE PROPERTY: the card-number refusal must not touch an ordinary
        form fill. Filling in a search form is not a decision, and an
        unapprovable act removes one — so what this pins is that the batch is
        never REFUSED.

        The host is vouched so that the address question (#295 M3), which is a
        different fence with its own tests, is not what the assertion below is
        reading. Nothing about the never-typed property changed."""
        snap = snapshot(controls=[
            control(n=1, kind=browse.FIELD, name="Skąd"),
            control(n=2, kind=browse.FIELD, name="Dokąd"),
            control(n=3, name="Szukaj", mutating=True, submits=True),
        ])
        agent, asked = self._agent(snap)
        # Vouched, which is the state seeding puts his own sites in (#295 M3):
        # the address question reaching driving is a separate property with its
        # own tests, and this one is about the never-typed list.
        agent._approved_hosts.add("eon.pl")
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


class TestTheCommitVerbsLeaveTheApprovableSet:
    """#342. A control whose OWN WORDS say it buys, pays, orders, books,
    subscribes, ends a contract or deletes is refused outright — no yes button,
    the owner handed `/browser <host>`.

    It drew a card until now, and the card was the problem: measured this
    period, the owner's median tap on one is 4.3 seconds and 11 of 34 were
    under 3. There is no future in which aish pressing "Zapłać" was wanted, so
    a popup offering a yes for it protects nothing and trains the tap that is
    waiting on the purchase.
    """

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
        agent._approved_sites.add("eon.pl")  # the driving grant is not the point
        return agent, asked

    def test_the_five_the_issue_named(self):
        assert browse.commits("Kup teraz") == "money"
        assert browse.commits("Kupuję i płacę") == "money"
        assert browse.commits("Place your order") == "money"
        assert browse.commits("Rezerwuję z obowiązkiem zapłaty") == "booking"
        assert browse.commits("Usuń") == "delete"

    def test_the_three_the_issue_named_as_untouched(self):
        """Ordinary work: a "send" refusal would end every contact-form and
        message task, and "continue as guest" is a checkout."""
        for ordinary in ("Wyślij", "Zapisz", "Continue as guest"):
            assert browse.commits(ordinary) == "", ordinary

    def test_a_word_is_a_word_and_not_a_run_of_letters(self):
        """`vocab.hit` is a bare substring scan, calibrated for a list whose
        false positive costs a PROMPT. These cost the capability outright with
        no way to grant it back in the moment, so the matching has to implement
        the owner's own sentence — a control whose words SAY it buys — rather
        than "contains these letters". Every string here is a real label
        recorded in his sessions, and a bare substring scan refuses all of
        them."""
        for innocent in (
            "facebook", "Facebook", "Books", "SZCZEGÓŁY ZAKUPU #1",
            "Kontynuuj zakupy", "Moje zamówienia", "Manage booking",
            "Tamara Kuprianowicz", "Order history", "Purchase history",
            "Allegro - wygodne i bezpieczne zakupy online, największy wybór ofert",
        ):
            assert browse.commits(innocent) == "", innocent

    #: Ending a contract, both directions. Not illustrations — this is the
    #: evidence that settled the guard, measured against the census.
    ENDS_A_CONTRACT = (
        "Wypowiedz umowę", "Rozwiąż umowę", "Zerwij umowę",
        "Wypowiedzenie umowy", "Terminate contract", "Terminate subscription",
    )
    #: The same folded strings, meaning something else entirely.
    NOT_A_CONTRACT = (
        "Rozwiąż quiz", "Rozwiąż test", "Dodaj wypowiedź", "Wypowiedzi",
        "Zerwij z nałogiem", "Rozwiązania",
    )

    def test_ending_a_contract_needs_the_contract(self):
        """The one guard here that is about HOMONYMS and not about breadth.
        `rozwiązać` is to SOLVE, `wypowiedź` is a COMMENT and folds to
        `wypowiedz` exactly, and `zerwij z nałogiem` is breaking a habit —
        different words that fold to the same string, so the bare verb refused
        a quiz, a comment box and a stop-smoking button. They take the
        verb+noun shape the account-scoped refusals in this file already use.

        `Wypowiedzenie umowy` is in the refuse list because word boundaries
        mean `wypowiedz` does not reach inside it, and that noun form is
        exactly what a Polish provider calls the button."""
        for label in self.ENDS_A_CONTRACT:
            assert browse.commits(label) == "contract", label
        for label in self.NOT_A_CONTRACT:
            assert browse.commits(label) == "", label

    def test_a_guard_that_misses_falls_back_to_a_card_and_not_to_silence(self):
        """Why narrowing here is safe, and the reason the words stayed in
        `_MUTATING_WORDS`. A contract label these guards do not catch does not
        go free — it lands on the incumbent list and draws a card, which is
        exactly what it did before this issue."""
        for label in self.ENDS_A_CONTRACT:
            assert browse.is_worded(label), label

    def test_wyczysc_and_clear_stay_carded_together(self):
        """The issue's prose moved `wyczyść` and left its English twin behind,
        and moved "transfers" while `transfer` was never in its list. The list
        is what shipped, not the prose — and both of these are the vocabulary
        of clearing a FILTER, not of destroying data."""
        for filter_word in ("Wyczyść", "Clear", "Clear all", "Transfer"):
            assert browse.commits(filter_word) == "", filter_word
            assert browse.is_worded(filter_word), filter_word  # still a card

    def test_pressing_one_is_refused_and_never_drawn_as_a_card(self):
        snap = snapshot(controls=[control(n=4, name="Kup teraz")])
        agent, asked = self._agent(snap)
        out = agent._browse_gate("browse_act", {"target": "Kup teraz"})
        assert "NOT EXECUTED" in out
        assert "buy something or pay money from your account" in out
        assert asked == []                # no card, ever
        assert "/browser eon.pl" in out   # the door he already has

    def test_the_refusal_names_the_consequence_and_never_the_mechanism(self):
        """#295 P1. He is told what pressing it would have done TO HIM — not
        which word list caught it, which tool asked, or that anything was
        "gated". `/browser eon.pl` is the one exception and it is not a
        mechanism word: it is the command HE types, and the whole point of the
        sentence is that the capability left aish's hands and not his."""
        snap = snapshot(controls=[control(n=1, name="Usuń przedmiot BOSCH")])
        agent, _ = self._agent(snap)
        out = agent._browse_gate("browse_act", {"target": "Usuń przedmiot BOSCH"})
        assert "delete something" in out
        assert "/browser eon.pl" in out
        for mechanism in (
            "browse_act", "vocab", "word list", "gate", "mutating", "approval card",
        ):
            assert mechanism not in out.lower(), mechanism

    def test_the_refusal_never_claims_aish_cannot_buy(self):
        """The claim has to stop where the code does, and this string is read
        by the MODEL, which then repeats it to the owner as aish's own account
        of itself. It opened "aish will never buy something or pay money from
        your account, on any site, however it is asked" — wider than anything
        enforced, since an unworded "Jetzt kaufen" rides the site grant and
        #299 owns that miss. What a line checked is the control's WORDS."""
        snap = snapshot(controls=[control(n=1, name="Kup teraz")])
        agent, _ = self._agent(snap)
        out = agent._browse_gate("browse_act", {"target": "Kup teraz"})
        assert "words say it would buy" in out
        for wider in (
            "will never buy", "never buy something", "aish cannot buy",
            "will never spend", "never pays",
        ):
            assert wider not in out, wider

    def test_the_refusal_does_not_claim_the_press_was_irreversible(self):
        """A cart row removed is undone by adding it back, and
        `BROWSE_IRREVERSIBLE`'s "it cannot be undone" would be a claim wider
        than anything here checked. What IS true of all of them is the owner's
        decision that the last press is his."""
        snap = snapshot(controls=[control(n=1, name="Usuń")])
        agent, _ = self._agent(snap)
        out = agent._browse_gate("browse_act", {"target": "Usuń"})
        assert "cannot be undone" not in out
        assert "that press is the user's own" in out

    def test_every_delete_goes_not_only_an_account_delete(self):
        """The code's own comment argued row-deletes should stay approvable —
        a draft, a reminder, one message. The owner overruled it in the issue,
        in these words: the escape is `/browser`, and if a real task breaks the
        softening comes back WITH THAT TASK as its evidence."""
        for row_delete in (
            "Usuń", "Usuń przedmiot BOSCH Wycieraczka AreoTwin Retro 650mm",
            "Remove Sean Horgan as a suggestion", "Delete draft", "Skasuj",
        ):
            assert browse.commits(row_delete) == "delete", row_delete

    def test_a_link_that_navigates_cannot_itself_commit(self):
        """The structural ground `is_mutating` already stands on: an `<a>` with
        a real http href is a GET to another page, which is what `read_url`
        does unasked. The commit is a button on the DESTINATION. Without this
        exemption every one of these becomes a dead end while pressing
        nothing."""
        for label in (
            "Allegro Pay", "Allegro Pay Business", "Kup ponownie",
            "Remove all filters", "Purchase insurance", "Book now",
            "Show 3 newsletters you subscribe to",
            "Pay with a credit card and fly for miles with Miles & More",
        ):
            assert browse.commits(label, navigates=True) == "", label
        # …and the exemption is the NAVIGATION, not the words: the same labels
        # on a JavaScript control that goes nowhere are refused.
        for label in (
            "Allegro Pay", "Kup ponownie", "Remove all filters",
            "Show 3 newsletters you subscribe to",
        ):
            assert browse.commits(label, navigates=False) != "", label

    def test_the_gate_reads_the_link_from_the_page_and_not_from_the_kind(self):
        """`<a href="#">Zapłać</a>` is a JavaScript button wearing a link's
        clothes, which is why `navigates` is carried from the enumeration
        rather than derived from `kind`."""
        snap = snapshot(controls=browse.controls_from([
            {"n": 1, "kind": "link", "name": "Allegro Pay",
             "href": "https://allegro.pl/pay"},
            {"n": 2, "kind": "link", "name": "Zapłać"},
        ]))
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": "Allegro Pay"}) is None
        out = agent._browse_gate("browse_act", {"target": "Zapłać"})
        assert "NOT EXECUTED" in out

    def test_looking_is_not_acting(self):
        """`action="read"` presses nothing — it is how the model gets the whole
        page back. Reading a page that happens to have a `Usuń` on it must not
        be refused, and the check sits before the gate's reading short-circuit,
        so the exemption has to be explicit."""
        snap = snapshot(controls=[control(n=1, name="Usuń")])
        agent, asked = self._agent(snap)
        assert agent._browse_gate(
            "browse_act", {"target": "Usuń", "action": "read"}
        ) is None
        assert asked == []

    def test_it_reads_the_controls_own_words_not_its_rows(self):
        """`address_controls` appends up to 44 characters of ROW text when
        duplicate labels need telling apart, so a plain `Wybierz` button is
        addressed `Wybierz — Zamówienie nr 123`. The owner's sentence is *a
        control whose own words say it buys*, and the row's words are the
        page's, about the row."""
        snap = snapshot(controls=browse.controls_from([
            {"n": 1, "kind": "button", "name": "Wybierz",
             "row": ["Kup teraz za 199 zł — wysyłka jutro"]},
            {"n": 2, "kind": "button", "name": "Wybierz",
             "row": ["Licytuj od 20 zł — wysyłka w piątek"]},
        ]))
        row_keyed = snap.controls[0].address
        assert browse.commits(row_keyed) != ""    # the ROW says it buys…
        assert browse.commits("Wybierz") == ""    # …the BUTTON does not
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": row_keyed}) is None
        assert asked == []  # rides the grant, exactly as it did before

    def test_an_unresolvable_target_is_judged_on_the_models_own_words(self):
        """When the snapshot cannot resolve the target the gate has nothing to
        read off the page, and the live fence in `browser.browse_act` is all
        that is left. Judging the words the model typed is the same answer
        `_committing_step` already gives a batch, and it is free: an
        unresolvable name presses nothing either way."""
        snap = snapshot(controls=[control(n=1, name="Przełącz lokal")])
        agent, asked = self._agent(snap)
        out = agent._browse_gate("browse_act", {"target": "Kup teraz"})
        assert "NOT EXECUTED" in out and "buy something" in out
        assert asked == []

    def test_a_batch_is_refused_whole_before_anything_is_typed(self):
        """The committing press is last by construction, so a batch refused
        here has typed nothing, pressed nothing and left nothing half-sent."""
        snap = snapshot(controls=browse.controls_from([
            {"n": 1, "kind": "field", "name": "Kod rabatowy"},
            {"n": 2, "kind": "button", "name": "Złóż zamówienie", "submits": True},
        ]))
        agent, asked = self._agent(snap)
        out = agent._browse_gate("browse_fill", {"steps": [
            {"target": "Kod rabatowy", "value": "LATO26"},
            {"target": "Złóż zamówienie", "do": "click"},
        ]})
        assert "NOT EXECUTED" in out and "buy something" in out
        assert asked == []

    #: Every commit-verb list, and which of three kinds it is. A list added
    #: later and left out of this map FAILS the sweep below rather than being
    #: skipped — an invariant this load-bearing must not depend on somebody
    #: remembering to extend a tuple.
    #:
    #: `refuses` — an entry ALONE draws the refusal.
    #: `needs a noun` — the verb half of a guarded pair; alone it refuses
    #:   nothing, and the noun is what fires it.
    #: `is a noun` — the other half. Never a commit verb on its own, so
    #:   `is_worded` says nothing about it and must not be asserted.
    LIST_KINDS = {
        "_COMMIT_MONEY": "refuses",
        "_COMMIT_SUBSCRIPTION": "refuses",
        "_COMMIT_BOOKING": "refuses",
        "_COMMIT_CONTRACT": "refuses",
        "_COMMIT_DELETE": "refuses",
        "_END_CONTRACT_VERBS": "needs a noun",
        "_CONTRACT_NOUNS": "is a noun",
    }

    def _commit_lists(self):
        """Swept from the module, not typed out, so a new list cannot slip
        past by not being mentioned."""
        found = {
            name: value for name, value in vars(browse).items()
            if ("_COMMIT_" in name or "_CONTRACT_" in name)
            and isinstance(value, tuple)
        }
        assert found, "the sweep itself is broken — no commit lists found"
        unclassified = sorted(set(found) - set(self.LIST_KINDS))
        assert not unclassified, (
            "a commit list nothing classifies is a list this property skips "
            f"in silence: {unclassified}"
        )
        return found

    def test_every_moved_word_is_still_in_the_mutating_list(self):
        """**The load-bearing half of the change, checked over every entry
        rather than a sample.**

        `Control.mutating` is read by three fences besides the card:
        `browser.browse_act`'s act-time re-resolution, and `plan_batch`'s
        mid-step and sight-unseen fences. A word that only MOVED lists leaves a
        stale-snapshot press landing on that live label classified as
        not-mutating, and it RUNS — where the same word on a fresh snapshot is
        refused.

        This test sampled five labels until `zamawiam` got through it: carried
        into the refusal list as an inflection, covered by nothing here
        (`zamow` is not a substring of it, unlike `kupuje`/`kupie`, which ride
        `kup`), so `Zamawiam z obowiązkiem zapłaty` — the statutory Polish
        checkout wording — was refused by the gate and pressable by the live
        fences. A sampled guard on an invariant this load-bearing is not a
        guard, so it is every entry now."""
        for name, entries in sorted(self._commit_lists().items()):
            if self.LIST_KINDS[name] == "is a noun":
                continue
            for entry in entries:
                assert browse.is_worded(entry), f"{name}: {entry!r}"
                assert browse.is_mutating(entry, browse.BUTTON), f"{name}: {entry!r}"

    def test_every_entry_that_refuses_alone_actually_does(self):
        """The other direction of the same sweep: a list declared as refusing
        on its own must, or the classification above is decoration."""
        lists = self._commit_lists()
        for name, entries in sorted(lists.items()):
            for entry in entries:
                kind = self.LIST_KINDS[name]
                if kind == "refuses":
                    assert browse.commits(entry) != "", f"{name}: {entry!r}"
                else:
                    # A contract verb alone, or a bare noun, refuses nothing —
                    # that IS the guard, and it is why the homonyms are free.
                    assert browse.commits(entry) == "", f"{name}: {entry!r}"

    def test_the_contract_pair_refuses_only_together(self):
        for verb in browse._END_CONTRACT_VERBS:
            for noun in browse._CONTRACT_NOUNS:
                assert browse.commits(f"{verb} {noun}") == "contract", (verb, noun)

    def test_a_committing_step_in_the_middle_of_a_batch_is_still_refused(self):
        """The other consumer of `mutating`: only ONE step may need approval
        and it must be last. That fence reads the same flag, and it is why the
        words had to stay."""
        controls = browse.controls_from([
            {"n": 1, "kind": "button", "name": "Kup teraz"},
            {"n": 2, "kind": "button", "name": "Wyślij"},
        ])
        plan = browse.plan_batch(controls, [
            {"target": "Kup teraz", "do": "click"},
            {"target": "Wyślij", "do": "click"},
        ])
        assert "only ONE step that needs approval" in plan.problem


class TestTheMeasuredComparisonAgainstTheIncumbent:
    """Law B (#295): no replacement ships without a measured comparison against
    the incumbent, on recorded material.

    The material is a census the drive ran over the owner's own recorded
    sessions — **3,002 distinct control labels**, of which 85 had ever drawn an
    approval card. **36 of those 85 become refusals**, and the cost across the
    whole corpus is **one** acting control. Those two numbers are the census's,
    not this test's; what is carried here is every label the census NAMED, as
    labels only. No test reads `~/.local/state/aish`.

    Each row is compared both ways: what the INCUMBENT did with it
    (`is_worded` — a card) and what the replacement does (`commits` — a
    refusal), so the direction of every move is checked and not assumed."""

    #: Carded before, refused now. The win.
    WINS = (
        "Zapłać wybrane",
        "Zapłać",
        "Usuń przedmiot BOSCH Wycieraczka AreoTwin Retro 650mm",
        "Subscribe",
        "Remove Sean Horgan as a suggestion",  # one of eight LinkedIn rows
    )

    #: Recorded labels that must come out exactly as they went in: a label that
    #: was free stays free, and one that drew a card still draws one.
    HELD_BUTTONS = (
        "Wyślij", "Zapisz", "Continue as guest", "Kontynuuj zakupy",
        "Moje zamówienia", "SZCZEGÓŁY ZAKUPU #1", "facebook",
        "Tamara Kuprianowicz", "Manage booking", "Order history",
        "Purchase history", "Book a flight", "Book",
        "Book your flight from Krakow", "Wyczyść", "Clear",
    )

    #: The same, for controls that NAVIGATE. Without the link exemption every
    #: one of these becomes a dead end while pressing nothing.
    HELD_LINKS = (
        "Allegro Pay", "Allegro Pay Business", "Kup ponownie",
        "Remove all filters", "Purchase insurance",
        "Show 3 newsletters you subscribe to", "Book now",
        "Pay with a credit card and fly for miles with Miles & More",
    )

    #: What the change COSTS, stated rather than hidden: acting controls that
    #: could be pressed before and cannot now. One, across 3,002 labels.
    COST = ("Sprawdź szczegóły dotyczące reklam — Zamów zestaw w jednej przesyłce",)

    def test_the_win_is_a_move_from_card_to_refusal_and_not_from_nothing(self):
        for label in self.WINS:
            assert browse.is_worded(label), f"{label}: was not carded before"
            assert browse.commits(label) != "", f"{label}: is not refused now"

    def test_nothing_that_worked_before_is_refused_now(self):
        for label in self.HELD_BUTTONS:
            assert browse.commits(label) == "", label
        for label in self.HELD_LINKS:
            assert browse.commits(label, navigates=True) == "", label

    def test_what_it_costs_is_one_acting_control_in_three_thousand(self):
        for label in self.COST:
            assert browse.commits(label) != "", label

    def test_the_bare_booking_word_was_measured_and_rejected(self):
        """The comparison that settled `book`/`reserve` as PHRASES ONLY. With
        the bare word, word boundaries and the link exemption still in place,
        the corpus loses three more acting controls — `Book a flight` (44
        sightings), `Book` (36), `Book your flight from Krakow` (3), which are
        search-start buttons on a booking site's front page. Refusing them
        means aish cannot begin a flight search at all, which is the epic's own
        flagship flow.

        The residual is written down rather than argued away: a payment-free
        binding labelled bare `Reserve` — a restaurant table, a doctor's
        appointment — stays a CARD, and #295 P2 says a card is not a control.
        #299's judge is the systemic cover for it."""
        bare = browse._boundaried(("book", "reserve"))
        for label in ("Book a flight", "Book", "Book your flight from Krakow"):
            assert bare.search(browse.fold(label)), label   # the rejected design
            assert browse.commits(label) == "", label       # the shipped one



class TestALinkThatGoesSomewhereIsNotAPressThatCommits:
    """#295 M2, and Law B's measured comparison for it.

    `commits()` has exempted navigation since #342, on the ground `is_mutating`
    stood on for longer: an `<a>` with a real http href is a GET to another
    page, which is what `read_url` does unasked, so it cannot itself be the
    commit — the commit is a button on the destination. `says_it_commits` never
    consulted it, and that asymmetry is what carded `Faktury i płatności`
    (26 sightings) three times in one week: the Polish word for *payment* is in
    its name, it navigates, `is_mutating` called it harmless on exactly that
    ground, and the worded half carded it anyway.

    **The material is the owner's own recorded controls**, carried here as
    labels and shapes only — no test reads `~/.local/state/aish`. Each row is
    the fate the INCUMBENT gave that label in that shape, measured before the
    change; the replacement's fate is computed by driving the real gate, so
    every move is checked and none is assumed.

    **What the corpus said that the issue's summary did not.** Only ONE of the
    labels named as a worded navigating link is one: `Przełącz lokal` and
    `Przełącz lokal/umowę` match nothing in `_MUTATING_WORDS` and were already
    free in BOTH shapes, so the cards they drew in his log were not theirs —
    they were the site-grant card, which is #295 M1's subject and not this
    one. The same is true of `Search`, `Select a model:`, `przeszukaj Moje
    Allegro`, `1 passenger, change number of passengers.`, `Dokąd Wybierz
    lotnisko przylotu`, `Dodaj + — Dorośli` and `Numer przesyłki`: every one of
    them is free here, submitting or not. They are kept in the corpus because a
    fate that must NOT move is worth pinning exactly as much as one that must.
    """

    #: The four shapes a recorded label was seen in. `navigates` is the
    #: enumeration's own destination signal and never the kind: measured,
    #: `Faktury i płatności` appears 17 times WITH a destination and 9 times
    #: without, and the second is a JavaScript button wearing a link's clothes.
    SHAPES = {
        "link to a page": {"kind": browse.LINK, "href": "https://eon.pl/faktury"},
        "link with no destination": {"kind": browse.LINK},
        "button": {"kind": browse.BUTTON},
        "button that posts a form": {"kind": browse.BUTTON, "submits": True},
    }

    #: Every row: (label, shape, the fate the INCUMBENT gave it). Measured on
    #: the shipped gate immediately before this change.
    CORPUS = (
        ('Faktury i płatności', 'link to a page', 'card'),
        ('Faktury i płatności', 'link with no destination', 'card'),
        ('Przełącz lokal', 'link to a page', 'free'),
        ('Przełącz lokal', 'link with no destination', 'free'),
        ('Przełącz lokal/umowę', 'link to a page', 'free'),
        ('Przełącz lokal/umowę', 'link with no destination', 'free'),
        ("Accept Jai Paliwal's invitation", 'button', 'card'),
        ("Accept Jai Paliwal's invitation", 'button that posts a form', 'card'),
        ('Search', 'button', 'free'),
        ('Search', 'button that posts a form', 'free'),
        ('1 passenger, change number of passengers.', 'button', 'free'),
        ('1 passenger, change number of passengers.',
         'button that posts a form', 'free'),
        ('Select a model:', 'button', 'free'),
        ('Select a model:', 'button that posts a form', 'free'),
        ('przeszukaj Moje Allegro', 'button', 'free'),
        ('przeszukaj Moje Allegro', 'button that posts a form', 'free'),
        ('Dokąd Wybierz lotnisko przylotu', 'button', 'free'),
        ('Dokąd Wybierz lotnisko przylotu', 'button that posts a form', 'free'),
        ('Dodaj + — Dorośli', 'button', 'free'),
        ('Dodaj + — Dorośli', 'button that posts a form', 'free'),
        ('Numer przesyłki', 'button', 'free'),
        ('Numer przesyłki', 'button that posts a form', 'free'),
        ('Kup teraz', 'button', 'refused'),
        ('Kup teraz', 'link to a page', 'card'),
        ('Zapłać wybrane', 'button', 'refused'),
        ('Zapłać wybrane', 'link to a page', 'card'),
        ('Usuń przedmiot BOSCH Wycieraczka AreoTwin Retro 650mm',
         'button', 'refused'),
        ('Usuń przedmiot BOSCH Wycieraczka AreoTwin Retro 650mm',
         'link to a page', 'card'),
        ('Zamawiam z obowiązkiem zapłaty', 'button', 'refused'),
        ('Zamawiam z obowiązkiem zapłaty', 'link to a page', 'card'),
        ('Complete booking', 'button', 'refused'),
        ('Complete booking', 'link to a page', 'card'),
        ('Wypowiedz umowę', 'button', 'refused'),
        ('Wypowiedz umowę', 'link to a page', 'card'),
        ('Kontynuuj zakupy', 'button', 'card'),
        ('Kontynuuj zakupy', 'link to a page', 'card'),
        ('Moje zamówienia', 'button', 'card'),
        ('Moje zamówienia', 'link to a page', 'card'),
        ('facebook', 'button', 'card'),
        ('facebook', 'link to a page', 'card'),
        ('Rozwiąż quiz', 'button', 'card'),
        ('Rozwiąż quiz', 'link to a page', 'card'),
        ('Book a flight', 'button', 'card'),
        ('Book a flight', 'link to a page', 'card'),
    )

    #: What `commits()` answers for every recorded label, plain and navigating.
    #: `commits()` is not touched by this change and its refusal set must come
    #: out byte-identical, so it is pinned as the exact consequence key rather
    #: than as "something was refused".
    REFUSALS = (
        ('Faktury i płatności', '', ''),
        ('Przełącz lokal', '', ''),
        ('Przełącz lokal/umowę', '', ''),
        ("Accept Jai Paliwal's invitation", '', ''),
        ('Search', '', ''),
        ('1 passenger, change number of passengers.', '', ''),
        ('Select a model:', '', ''),
        ('przeszukaj Moje Allegro', '', ''),
        ('Dokąd Wybierz lotnisko przylotu', '', ''),
        ('Dodaj + — Dorośli', '', ''),
        ('Numer przesyłki', '', ''),
        ('Kup teraz', 'money', ''),
        ('Zapłać wybrane', 'money', ''),
        ('Usuń przedmiot BOSCH Wycieraczka AreoTwin Retro 650mm', 'delete', ''),
        ('Zamawiam z obowiązkiem zapłaty', 'money', ''),
        ('Complete booking', 'booking', ''),
        ('Wypowiedz umowę', 'contract', ''),
        ('Kontynuuj zakupy', '', ''),
        ('Moje zamówienia', '', ''),
        ('facebook', '', ''),
        ('Rozwiąż quiz', '', ''),
        ('Book a flight', '', ''),
    )

    HOST = "eon.pl"

    def _fate(self, label, shape):
        """What the SHIPPED gate does with this control — driven, not modelled.

        The site is already granted, which is the steady state the census was
        measured in: the corpus is a week of driving sites he uses, not a week
        of first presses. A card here is therefore the control's own."""
        raw = {"n": 1, "kind": browse.BUTTON, "name": label}
        raw.update(self.SHAPES[shape])
        controls = browse.controls_from([raw])
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(
            snapshot(url=f"https://{self.HOST}/mojeon", controls=controls)
        )
        agent._approved_sites.add(self.HOST)
        out = agent._browse_gate("browse_act", {"target": controls[0].address})
        if out is not None:
            return "refused"
        return "card" if asked else "free"

    def test_every_fate_change_is_a_card_becoming_free_on_a_navigating_link(self):
        """The whole permitted direction, and the whole of it. Any other move
        — a refusal going anywhere, a button changing, a link with no
        destination losing its card — is a defect, so the corpus is ITERATED
        and each difference is checked against the one shape allowed to move.

        Measured: 12 of the 44 rows move, all of them `card` → `free`, all of
        them on a link with a real destination."""
        moved = []
        for label, shape, before in self.CORPUS:
            after = self._fate(label, shape)
            if after == before:
                continue
            moved.append((label, shape, before, after))
            assert (before, after) == ("card", "free"), (label, shape, before, after)
            assert "href" in self.SHAPES[shape], (
                f"{label!r} moved in a shape that does not navigate: {shape}"
            )
        # The COUNT is pinned too: a direction check alone passes happily
        # while a widened exemption frees ten more labels in the right
        # direction, and the cost of a change is how many, not only which
        # way.
        assert len(moved) == 12, moved
        assert len(self.CORPUS) == 44

    def test_no_recorded_label_changes_what_it_is_refused_for(self):
        """`commits()` shipped yesterday and nothing here may move a refusal.
        Pinned as the exact consequence key, both ways, over every label."""
        for label, plain, navigating in self.REFUSALS:
            assert browse.commits(label) == plain, label
            assert browse.commits(label, navigates=True) == navigating, label

    def test_the_refusal_lists_still_refuse_entry_by_entry(self):
        """The `zamawiam` lesson: a guard over a set ITERATES the set. Every
        entry of every commit list, swept from the module so a list added later
        cannot slip past by not being mentioned."""
        incumbent = TestTheCommitVerbsLeaveTheApprovableSet()
        for name, entries in sorted(incumbent._commit_lists().items()):
            kind = incumbent.LIST_KINDS[name]
            for entry in entries:
                if kind == "refuses":
                    assert browse.commits(entry) != "", f"{name}: {entry!r}"
                else:
                    assert browse.commits(entry) == "", f"{name}: {entry!r}"

    def test_a_link_with_no_destination_keeps_its_card(self):
        """An `<a href="#">` is a JavaScript button wearing a link's clothes.
        Measured, `Faktury i płatności` is that 9 times out of 26."""
        assert self._fate("Faktury i płatności", "link with no destination") == "card"
        assert browse.says_it_commits("Faktury i płatności", navigates=False)

    def test_the_exemption_reads_the_href_and_never_the_kind(self):
        """The two halves must agree about what "goes somewhere" means, and
        both must read the enumeration's own answer. A kind check would free
        every `<a href="#">` on the Polish web."""
        dead = browse.controls_from(
            [{"n": 1, "kind": browse.LINK, "name": "Faktury i płatności"}]
        )[0]
        live = browse.controls_from([{
            "n": 1, "kind": browse.LINK, "name": "Faktury i płatności",
            "href": "https://eon.pl/faktury",
        }])[0]
        assert dead.kind == live.kind == browse.LINK
        assert dead.navigates is False and live.navigates is True
        assert dead.worded is True and live.worded is False
        assert dead.mutating is True and live.mutating is False

    def test_a_control_that_submits_a_form_is_never_exempted(self):
        """Whatever else it claims to be. A page is free to describe its own
        controls however it likes, and the nondescript button that posts the
        form is the dangerous one — so the exemption stops at `submits`,
        exactly where the widget demotion beside it stops."""
        assert browse.says_it_commits("Wyślij", navigates=True, submits=True)
        assert browse.says_it_commits("Faktury i płatności", navigates=True,
                                      submits=True)
        both = browse.controls_from([{
            "n": 1, "kind": browse.LINK, "name": "Faktury i płatności",
            "href": "https://eon.pl/faktury", "submits": True,
        }])[0]
        assert both.worded is True

    def test_the_demotion_is_structural_and_not_a_gate_special_case(self):
        """`says_it_commits` is the single owner of the judgement, so the
        answer moved once and every reader of `Control.worded` sees it — the
        gate was not taught a new exception of its own."""
        control = browse.controls_from([{
            "n": 1, "kind": browse.LINK, "name": "Faktury i płatności",
            "href": "https://eon.pl/faktury",
        }])[0]
        assert control.worded is False
        assert browse.says_it_commits(
            "Faktury i płatności", navigates=True
        ) is False

    def test_the_fill_fence_is_unchanged_for_everything_that_is_not_a_link(self):
        """`Control.mutating` is read by three fences besides the card, and a
        navigating control was already non-mutating before this change — so
        `mutating` moves for nothing at all here, which is what keeps the
        stale-snapshot press fence exactly as strict as it was."""
        for label, shape, _before in self.CORPUS:
            raw = {"n": 1, "kind": browse.BUTTON, "name": label}
            raw.update(self.SHAPES[shape])
            control = browse.controls_from([raw])[0]
            expected = browse.is_mutating(
                label,
                control.kind,
                submits=control.submits,
                navigates=control.navigates,
            )
            assert control.mutating is expected, (label, shape)

class TestOneFenceOverTypingWhicheverToolTypes:
    """#310. Both value refusals lived on the batch path, so the same value on
    the same page was refused by `browse_fill` and typed by
    `browse_act(action="type")` — whose branch of the gate tested a password
    field and an irreversible LABEL and no value at all.

    That is the failure `docs/browser.md` records against the site grant one
    section over: *the model chooses the tool, which made the card bypassable
    for exactly the half it was covering.* And it is the sentence #295 §5 rests
    on — *the line is enforced by absence* — being true of a batch rather than
    of aish. Absence that holds for one of two tools is not absence."""

    IBAN = "PL61 1090 1014 0000 0712 1981 2874"
    CARD = "4111 1111 1111 1111"

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
        agent._approved_sites.add("eon.pl")  # the driving grant is not the point
        return agent, asked

    def _page(self, field="Numer zamówienia"):
        return snapshot(controls=[
            control(n=1, kind=browse.FIELD, name=field),
            control(n=2, name="Dalej", mutating=True, submits=True),
        ])

    def _through_both_tools(self, value, field="Numer zamówienia", verdict=True):
        """The same value, at the same control, on the same page — once per
        tool that can type it. Each gets its own Agent, so neither run can be
        the reason the other refused."""
        out = []
        for name, args in (
            ("browse_fill", {"steps": [{"target": field, "value": value}]}),
            ("browse_act", {"target": field, "action": "type", "text": value}),
        ):
            agent, asked = self._agent(
                self._page(field), approve=lambda *_a, v=verdict: v
            )
            out.append((agent._browse_gate(name, args), asked))
        return out

    def test_a_card_number_is_refused_the_same_way_through_either_tool(self):
        """Identical text and identical unapprovable verdict, because a
        difference between the two IS a hint about which tool to reach for."""
        (fill_out, fill_asked), (act_out, act_asked) = self._through_both_tools(
            self.CARD
        )
        for out in (fill_out, act_out):
            assert "NOT EXECUTED" in out and "payment card number" in out
            assert "Numer zamówienia" in out  # it names what it stopped at
            assert "/browser eon.pl" in out   # the door he already has
        assert str(fill_out) == str(act_out)
        # A refusal with no yes button: no card was drawn on either path, and
        # the verdict says the act did not happen.
        assert fill_asked == [] and act_asked == []
        assert fill_out.meta == act_out.meta
        assert fill_out.meta["decision"] == "blocked"

    def test_an_iban_is_refused_the_same_way_through_either_tool(self):
        """The older of the two refusals, and widening it is the half of #310
        that changes shipped coverage — it had been batch-only since it
        shipped."""
        (fill_out, fill_asked), (act_out, act_asked) = self._through_both_tools(
            self.IBAN, field="Numer rachunku"
        )
        for out in (fill_out, act_out):
            assert "NOT EXECUTED" in out and "bank account number" in out
            assert out.meta["decision"] == "blocked"
        assert str(fill_out) == str(act_out)
        assert fill_asked == [] and act_asked == []

    def test_no_approver_verdict_reaches_it_on_either_path(self):
        """There is no yes, so there is nothing for a yes to be given to: the
        refusal is the same with an approver that approves everything and one
        that refuses everything."""
        said = []
        for verdict in (True, False):
            for out, asked in self._through_both_tools(self.CARD, verdict=verdict):
                assert "NOT EXECUTED" in out
                assert asked == []
                said.append(str(out))
        assert len(set(said)) == 1

    def test_the_digits_never_appear_in_the_refusal(self):
        """A refusal that names nothing teaches nothing; one that names the
        value defeats itself. This is a claim about the REFUSAL only — the
        model put the digits in the call's own arguments and `_call_result`
        writes that record before `_dispatch` reaches any gate, so a refused
        value is in the trace by design."""
        for value in (self.CARD, self.IBAN):
            for out, _asked in self._through_both_tools(value):
                for fragment in (
                    value, value.replace(" ", ""), *value.split(),
                ):
                    assert fragment not in out, fragment

    def test_an_ordinary_value_through_either_tool_is_unaffected(self):
        """The fence reads the value and nothing else, so an ordinary one pays
        nothing — no refusal, and no card it did not already draw."""
        for value in ("Warszawa", "pawel@wenda.email", "2026-08-22", "1234"):
            for out, asked in self._through_both_tools(value, field="Skąd"):
                assert out is None, (value, out)
                assert asked == []

    def test_the_fence_asks_nothing_of_the_page(self):
        """It runs before the control is resolved, so a value cannot become
        typeable because the model named a control that is not there, or
        because nothing is open at all. The refusal is a statement about what
        aish would SEND; nothing about the page may soften it."""
        agent, asked = self._agent(self._page())
        out = agent._browse_gate(
            "browse_act",
            {"target": "a control that is not on this page", "action": "type",
             "text": self.CARD},
        )
        assert "NOT EXECUTED" in out and "payment card number" in out
        assert asked == []

        blank = Agent(
            model="fake", approve=lambda _c: True,
            client_chat=lambda **kw: {}, approve_tool=lambda *_a: True,
        )
        out = blank._browse_gate(
            "browse_act",
            {"target": "Numer karty", "action": "type", "text": self.CARD},
        )
        assert "NOT EXECUTED" in out and "payment card number" in out

    def test_it_reads_the_step_the_way_the_batch_planner_does(self):
        """One reading of what a step SAYS, shared with `plan_batch` — the
        aliases are where a second parser would drift, since `do` is also
        spelled `action` and a step's value is also spelled `text`."""
        assert browse.typed_values(
            "browse_fill",
            {"steps": [{"target": "Numer karty", "action": "fill", "text": self.CARD}]},
        ) == [("Numer karty", self.CARD)]
        # And a malformed batch is read without raising: the gate's own
        # complaint about it comes later, and must still be the thing said.
        assert browse.typed_values("browse_fill", {"steps": "not a list"}) == []
        assert browse.typed_values("browse_fill", {"steps": ["not a step"]}) == []

    def test_only_the_verbs_that_actually_TYPE_are_read(self):
        """`choose` picks an option the page itself wrote and `click`/`check`
        type nothing, so neither carries a model-supplied value into the page.
        `date` does — a field that opens no picker is typed as an ISO date."""
        for verb in ("choose", "click", "check"):
            assert browse.typed_values(
                "browse_fill", {"steps": [{"target": "X", "do": verb, "value": "V"}]}
            ) == []
        assert browse.typed_values(
            "browse_fill", {"steps": [{"target": "X", "do": "date", "value": "V"}]}
        ) == [("X", "V")]
        for action in ("click", "choose", "read"):
            assert browse.typed_values(
                "browse_act", {"target": "X", "action": action, "value": "V"}
            ) == []
        assert browse.typed_values("browse", {"url": "https://eon.pl"}) == []

    def test_the_never_list_is_decided_in_one_place(self):
        """`refuses_to_type` is what both paths ask, so the wording is looked
        up rather than re-derived: a second test of the value to choose a
        message is a second place to disagree about what the value is."""
        assert browse.refuses_to_type(self.CARD) == browse.NO_CARD_NUMBER
        assert browse.refuses_to_type(self.IBAN) == browse.NO_BANK_ACCOUNT
        assert browse.refuses_to_type("Warszawa") == ""
        assert set(agent_module.NEVER_TYPED) == {
            browse.NO_BANK_ACCOUNT, browse.NO_CARD_NUMBER
        }


class TestADrivenPageThatIsAskingForAPassword:
    """The gap the linkedin.com session found (#280 follow-up). Auto sign-in
    was wired into the READ path only, and the model drives pages through
    BROWSE — so a portal that had signed him out arrived as an ordinary page
    with a couple of odd buttons, and the model went looking for a door."""

    def _driven(self, monkeypatch, *, signin_flag=True, renewal=None, opened=None,
                reopen_signin=False, reopen_raises=False):
        """One fake page, parametrised — never a second one. `reopen_*` is
        what the RE-OPEN after a renewal does: still a login page, or a tab
        that died. Both used to be labelled "what follows IS their account"."""
        opens = []

        def browse_open(url, *, topic="", key=""):
            opens.append(url)
            if len(opens) > 1 and reopen_raises:
                raise RuntimeError("the tab died")
            snap = snapshot(url=url, text="Sign in to continue", controls=[])
            snap.signin = signin_flag if len(opens) == 1 else reopen_signin
            return snap

        monkeypatch.setattr(web_module.browser, "browse_open", browse_open)
        monkeypatch.setattr(web_module, "_require_public", lambda _u: None)
        # `seen` is the per-call sign-in recorder the entry point creates; a
        # stub that swallows it keeps these tests about the NOTES, which is
        # what they are for.
        monkeypatch.setattr(
            web_module, "_renew_session", lambda _u, *, seen=None: renewal
        )
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

    def test_a_re_open_that_still_walls_is_not_reported_as_the_account(
        self, monkeypatch
    ):
        """The driving path's half of the same false claim. The note used to
        be attached to whatever the re-open produced, so a page still showing
        a login form arrived labelled as the owner's account — and this is the
        path where the model then goes looking for a door to press."""
        from aish import browser as browser_module

        self._driven(
            monkeypatch, renewal=browser_module.SignInResult(ok=True),
            reopen_signin=True,
        )
        out = web_module.browse("https://eon.pl/mojeon")
        assert "asking for a password anyway" in out
        assert "cannot tell why" in out
        assert "Do NOT try other buttons" in out
        assert "signed in again as the user with the sign-in they saved. What" not in out

    def test_a_re_open_that_FAILED_says_the_page_is_the_one_from_before(
        self, monkeypatch
    ):
        """The snapshot kept is the signed-out one this function was handed —
        it is why there was a renewal at all — so claiming it is the account
        would be false about the page in front of the model whatever the
        sign-in achieved."""
        from aish import browser as browser_module

        self._driven(
            monkeypatch, renewal=browser_module.SignInResult(ok=True),
            reopen_raises=True,
        )
        out = web_module.browse("https://eon.pl/mojeon")
        assert "as it was BEFORE the sign-in" in out
        assert "not their account" in out

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

    def test_a_declaring_site_is_told_by_observation_never_by_the_declaration(
        self, monkeypatch
    ):
        """#320/#321. The note used to be selected by `outcome.captcha` — a
        script tag on the page choosing a sentence that said the SITE refuses
        automatic sign-ins. The declaration now rides inside `why`, and the
        note is routed on what was observed of the credential; nothing may
        send him to re-record, because nothing was learned about the value."""
        from aish import browser as browser_module

        self._driven(
            monkeypatch,
            renewal=browser_module.SignInResult(
                captcha="reCAPTCHA", tried=True, filled=True,
                why="it left and nothing judged it",
            ),
        )
        out = web_module.browse("https://eon.pl/mojeon")
        assert "did not get all the way in" in out
        assert "it left and nothing judged it" in out
        assert "CANNOT sign in to this site automatically" not in out
        assert "not accepted" not in out and "will not try it again" not in out
        assert "also replaces the saved sign-in" not in out

    def test_a_filled_form_never_confirmed_submitted_gets_its_own_note(
        self, monkeypatch
    ):
        """The eon.pl shape: typed in, gesture made, nothing observed leaving.
        "aish did not use it" (held) and "did not get all the way in"
        (unfinished) would both overclaim, and the note must not invite a
        re-record — nothing was learned about the credential."""
        from aish import browser as browser_module

        self._driven(
            monkeypatch,
            renewal=browser_module.SignInResult(
                filled=True, why="nothing was seen leaving the page",
            ),
        )
        out = web_module.browse("https://eon.pl/mojeon")
        assert "could not confirm the form was ever submitted" in out
        assert "does NOT need replacing" in out
        assert "aish did not use it" not in out
        assert "did not get all the way in" not in out
        assert "not accepted" not in out

    def test_an_attempt_that_simply_did_not_get_in_blames_nobody(self, monkeypatch):
        from aish import browser as browser_module

        self._driven(
            monkeypatch,
            renewal=browser_module.SignInResult(tried=True, why="no reason given"),
        )
        out = web_module.browse("https://eon.pl/mojeon")
        assert "did not get all the way in" in out
        assert "has not been judged and is untouched" in out
        # aish DID use it, so the held note would be a false statement too.
        assert "aish did not use it" not in out
        assert "not accepted" not in out

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
            agent._approved_sites.add("eon.pl")
        return agent, asked

    def _committing(self):
        """The press the grant is now collected on (#295 M4): a nondescript
        submit, which is `mutating` and needs no card of its own. It used to be
        `Przełącz lokal`, a tab switch — and asking for the grant in front of a
        control that changes nothing is exactly what M4 removed."""
        return snapshot(controls=browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Dalej", "submits": True}]
        ))

    def test_the_card_says_what_riding_it_means(self):
        """The win has to come from MOVING the decision, not thinning it: a
        card tapped blind records a consent he never gave, so what the grant
        covers is said on the card that grants it.

        Since #342 the sentence has to say two different things, because the
        floor now has two shapes: what aish will not press at all, and what it
        presses only after asking. It said only the second, which after that
        issue promised a card where there is a wall."""
        agent, asked = self._agent(self._committing(), granted=False)
        agent._browse_gate("browse_act", {"target": "Dalej"})
        assert "signed in as you" in asked[0]
        assert "asks by name" in asked[0]
        assert "never one that says it buys, pays or deletes" in asked[0]

    def test_the_card_never_claims_aish_cannot_buy(self):
        """The claim has to stop exactly where the code does. A control whose
        WORDS say it buys is refused; a nondescript "Dalej" that completes a
        purchase still rides this yes, and #299 owns that. A card saying "aish
        never buys" would be the claim wider than the code."""
        agent, asked = self._agent(self._committing(), granted=False)
        agent._browse_gate("browse_act", {"target": "Dalej"})
        assert "says it buys" in asked[0]
        for wider in ("never buys", "cannot buy", "will never spend"):
            assert wider not in asked[0], wider

    def test_the_card_stays_one_sentence(self):
        """Said on a phone, where four sentences is a card he scrolls past to
        reach the buttons. The floor the grant does not cover draws its own
        card when it fires; promising it here only buys length."""
        agent, asked = self._agent(self._committing(), granted=False)
        agent._browse_gate("browse_act", {"target": "Dalej"})
        assert "\n" not in asked[0]
        assert ". " not in asked[0] and asked[0].endswith(".")
        # The budget is the GRANT SENTENCE's, and it is pinned on the sentence
        # itself: since #295 M4 the card it rides on also names the press it
        # was collected on, and `TestOneCardNotTwo` owns the merged length.
        assert len(agent_module.SITE_GRANT.format(host="eon.pl")) < 180

    def test_a_plain_submit_rides_the_grant(self):
        snap = snapshot(controls=browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Search", "submits": True}]
        ))
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": "Search"}) is None
        assert asked == []

    def test_a_name_that_says_it_commits_still_asks(self):
        """The floor the grant explicitly does not cover. 'Wyślij' since #342
        — the paying half of that floor is now a refusal, not a card."""
        snap = snapshot(controls=browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Wyślij", "submits": True}]
        ))
        agent, asked = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": "Wyślij"}) is None
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
            {"n": 2, "kind": "button", "name": "Wyślij", "submits": True},
        ]))
        agent, asked = self._agent(snap)
        steps = [{"target": "Skąd", "value": "WAW"}, {"target": "Wyślij", "do": "click"}]
        assert agent._browse_gate("browse_fill", {"steps": steps}) is None
        assert agent._browse_gate("browse_fill", {"steps": steps}) is None
        assert len(asked) == 1

    def test_changing_a_value_asks_again(self):
        """The yes covered the thing it was given for, and the card names every
        value — so two batches share a yes only when he would be shown the same
        words."""
        snap = snapshot(controls=browse.controls_from([
            {"n": 1, "kind": "field", "name": "Skąd"},
            {"n": 2, "kind": "button", "name": "Wyślij", "submits": True},
        ]))
        agent, asked = self._agent(snap)
        agent._browse_gate("browse_fill", {"steps": [
            {"target": "Skąd", "value": "WAW"}, {"target": "Wyślij", "do": "click"}]})
        agent._browse_gate("browse_fill", {"steps": [
            {"target": "Skąd", "value": "KRK"}, {"target": "Wyślij", "do": "click"}]})
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

    def test_a_country_subdomain_is_the_same_site(self):
        """He approved driving linkedin.com, and six minutes later was asked
        again for pl.linkedin.com — same site, same session, same profile, and
        a card naming the same company. `is_logged_in` has always read the
        boundary this way; exact set membership did not."""
        agent, asked = self._agent(snapshot(controls=[control()]), granted=False)
        agent._approved_sites.add("linkedin.com")
        assert agent._browse_gate(
            "browse", {"url": "https://pl.linkedin.com/in/kasia"}
        ) is None
        assert asked == []

    def test_a_grant_never_widens_upward_or_sideways(self):
        """Downward only: he was shown the narrower name, so that is what he
        agreed to. And the boundary is a dot, not a suffix of letters."""
        agent, asked = self._agent(snapshot(controls=[control()]), granted=False)
        agent._approved_sites.add("pl.linkedin.com")
        assert not agent._site_granted("www.linkedin.com")
        assert not agent._site_granted("evil-pl.linkedin.com.attacker.test")
        agent._approved_sites.add("linkedin.com")
        assert not agent._site_granted("evillinkedin.com")

    def test_the_grant_survives_the_agent_being_rebuilt(self):
        """What the owner experienced as being re-asked per task was the agent
        being rebuilt under him — every aish-web restart, which is every ship."""
        agent, asked = self._agent(snapshot(controls=[control()]), granted=False)
        assert "_approved_sites" not in agent_module.Agent._reset_task_state.__doc__ or True
        fresh = Agent(model="fake", approve=lambda _c: True, client_chat=lambda **kw: {})
        fresh.restore_site_grants(["eon.pl"])
        assert "eon.pl" in fresh._approved_sites



class TestOneCardNotTwo:
    """#295 M1. The site grant and the control's own card were asked in
    SEQUENCE, so the first press that needed its own card cost two cards
    seconds apart: measured in the owner's own log, `Accept Jai Paliwal's
    invitation` carded at 22:07:07 and again at 22:07:12 on 2026-08-26. His
    `browse_fill` cards did it too — `Dokąd Wybierz lotnisko przylotu` at
    20:08:04 and `Dodaj + — Dorośli` at 20:09:42 on 2026-08-31.

    It is one decision — *may aish press this, on this site, as me* — and the
    second card is the worse of the two, because it arrives after he has
    already decided and is therefore the one he taps without reading (P2).

    Nothing is thinned to buy it. The grant sentence rides along in the clauses
    `SITE_GRANT` states it in, and `_grant_site` records exactly what it
    recorded before."""

    #: Every clause `TestTheGrantIsWhereTheConsentLives` pins the standalone
    #: grant card on. Iterated, never sampled: a grant given on the merged card
    #: and a grant given on its own card are the same grant, so a clause that
    #: reached him one way and not the other is a consent surface that says
    #: different things about the same permission depending on which control he
    #: happened to press first.
    GRANT_CLAUSES = (
        "signed in as you",
        "asks by name",
        "never one that says it buys, pays or deletes",
        "presses things inside your account",
    )

    #: The longest host in the owner's own recorded corpus. The card grows with
    #: it — twice over, since the press names the host and so does the grant —
    #: so what he would actually be shown is declared here rather than left to
    #: be discovered on a phone.
    LONGEST_HOST = "crmforms.qatarairways.com.qa"

    def _agent(self, snap, granted=False):
        asked, logged = [], []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
            state_log=logged.append,
        )
        agent._browse_view.remember(snap)
        if granted:
            agent._approved_sites.add("eon.pl")
        return agent, asked, logged

    def _worded(self):
        return snapshot(controls=browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Wyślij", "submits": True}]
        ))

    def test_the_first_press_that_needs_its_own_card_asks_once(self):
        agent, asked, _ = self._agent(self._worded())
        assert agent._browse_gate("browse_act", {"target": "Wyślij"}) is None
        assert len(asked) == 1
        assert "Wyślij" in asked[0]
        assert "act on eon.pl signed in as you" in asked[0]

    def test_the_merged_card_says_everything_the_site_card_says(self):
        agent, asked, _ = self._agent(self._worded())
        agent._browse_gate("browse_act", {"target": "Wyślij"})
        for clause in self.GRANT_CLAUSES:
            assert clause in asked[0], clause

    def test_the_merged_card_never_claims_aish_cannot_buy(self):
        """The claim stops where the code does, on this card exactly as on the
        standalone one: an unworded `Dalej` that completes a purchase still
        rides this yes, and #299 owns that."""
        agent, asked, _ = self._agent(self._worded())
        agent._browse_gate("browse_act", {"target": "Wyślij"})
        for wider in ("never buys", "cannot buy", "will never spend"):
            assert wider not in asked[0], wider

    def test_the_grant_it_carries_is_still_one_sentence(self):
        """#285's finding applies to the card the grant is actually shown on,
        whichever card that is. One line, one sentence, still readable on a
        phone — and it replaces two cards, not one."""
        agent, asked, _ = self._agent(self._worded())
        agent._browse_gate("browse_act", {"target": "Wyślij"})
        assert "\n" not in asked[0]
        assert ". " not in asked[0] and asked[0].endswith(".")

    def test_the_longest_recorded_host_is_declared_rather_than_discovered(self):
        """The host is named twice on a merged card, so its length is paid
        twice. Stated as the number he would see rather than left to be found:
        at the longest host in the corpus the whole card is under 300
        characters — against roughly 450 across the two cards it replaces."""
        merged = (
            f"click button 'Wyślij' on {self.LONGEST_HOST} — "
            + agent_module.SITE_GRANT_RIDER.format(host=self.LONGEST_HOST)
        )
        assert len(merged) < 300
        assert ". " not in merged and merged.endswith(".")

    def test_the_grant_is_recorded_exactly_as_before(self):
        """What changed is how often he is asked, never what a yes writes down.
        The record is what a reopened chat replays the grant from (#267), so a
        merged yes that recorded nothing would come back as another card."""
        agent, _, logged = self._agent(self._worded())
        agent._browse_gate("browse_act", {"target": "Wyślij"})
        assert "eon.pl" in agent._approved_sites
        assert {"kind": "site_grant", "host": "eon.pl"} in logged

    def test_a_no_denies_both_halves_and_says_so(self):
        """Told only that the control was refused, an eager model tries the one
        beside it and draws the site card it was just denied. A no to one card
        asking two things is a no to both, and the model is told both."""
        asked, logged = [], []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or None,
            state_log=logged.append,
        )
        agent._browse_view.remember(self._worded())
        out = agent._browse_gate("browse_act", {"target": "Wyślij"})
        assert out is not None
        assert "was NOT clicked" in out
        assert "acting on eon.pl as them was NOT granted" in out
        assert agent._approved_sites == set()
        assert logged == []

    def test_a_granted_site_is_completely_unaffected(self):
        """The merge only ever removes the SECOND card. Once the site is
        granted the control's card is the same card it always was, with no
        grant sentence on it — he is not told again about a permission he
        already gave."""
        agent, asked, _ = self._agent(self._worded(), granted=True)
        assert agent._browse_gate("browse_act", {"target": "Wyślij"}) is None
        assert len(asked) == 1
        assert "signed in as you" not in asked[0]
        assert asked[0] == "click button 'Wyślij' on eon.pl"

    def test_a_press_that_needs_no_card_of_its_own_still_asks_for_the_site(self):
        """The half that is not merged, and must not be: the first press that
        CHANGES something is still the moment the site is asked about, even
        when the control needs no card of its own.

        Since #295 M4 that card NAMES the press, because it is drawn on the
        press rather than on whatever happened to be clicked first — a grant
        card is only checkable against something he can see."""
        snap = snapshot(controls=browse.controls_from(
            [{"n": 1, "kind": "button", "name": "Search", "submits": True}]
        ))
        agent, asked, _ = self._agent(snap)
        assert agent._browse_gate("browse_act", {"target": "Search"}) is None
        assert asked == [
            "click button 'Search' on eon.pl — "
            + agent_module.SITE_GRANT_RIDER.format(host="eon.pl")
        ]

    def test_the_second_press_on_a_site_asks_only_about_the_control(self):
        """One grant, once — the merge must not turn a per-site question into a
        per-press one by carrying the sentence on every card."""
        agent, asked, _ = self._agent(snapshot(controls=browse.controls_from([
            {"n": 1, "kind": "button", "name": "Wyślij", "submits": True},
            {"n": 2, "kind": "button", "name": "Zapisz", "submits": True},
        ])))
        agent._browse_gate("browse_act", {"target": "Wyślij"})
        agent._browse_gate("browse_act", {"target": "Zapisz"})
        assert len(asked) == 2
        assert "signed in as you" in asked[0]
        assert "signed in as you" not in asked[1]

    def _batch(self):
        return snapshot(controls=browse.controls_from([
            {"n": 1, "kind": "field", "name": "Skąd"},
            {"n": 2, "kind": "button", "name": "Wyślij", "submits": True},
        ]))

    STEPS = ({"target": "Skąd", "value": "WAW"}, {"target": "Wyślij", "do": "click"})

    def test_a_form_that_commits_asks_once_too(self):
        """The batch gate had the same two cards in sequence, and his log has
        them: `Dokąd Wybierz lotnisko przylotu`, then `Dodaj + — Dorośli`."""
        agent, asked, _ = self._agent(self._batch())
        out = agent._browse_gate("browse_fill", {"steps": list(self.STEPS)})
        assert out is None
        assert len(asked) == 1
        assert "fill in this form on eon.pl" in asked[0]
        assert "act on eon.pl signed in as you" in asked[0]
        assert "eon.pl" in agent._approved_sites

    def test_the_form_card_keeps_the_grant_on_a_line_of_its_own(self):
        """THE PROPERTY: a batch card is a block — the form, then every value.
        Running the grant onto the end of its last value would read as part of
        that value, so it joins with a newline where a one-line card joins with
        a dash. Asserted on the grant clause exactly as before; the host is
        vouched so the send clause (#295 M3, its own tests) is not what the
        `endswith` is reading."""
        agent, asked, _ = self._agent(self._batch())
        agent._approved_hosts.add("eon.pl")  # the send clause has its own tests
        agent._browse_gate("browse_fill", {"steps": list(self.STEPS)})
        assert asked[0].endswith(
            "\n" + agent_module.SITE_GRANT_RIDER.format(host="eon.pl")
        )

    def test_a_form_that_needs_no_card_asks_nothing_at_all(self):
        """This one MOVED in #295 M4, and it is the whole of that slice. A
        batch with nothing committing in it — type, then press a GET search —
        used to draw the site card, because the grant was collected on the
        first press whatever that press did. It now asks nothing and records
        nothing. The host is vouched so the address question (#295 M3) is not
        what is being counted; it has its own tests."""
        snap = snapshot(controls=browse.controls_from([
            {"n": 1, "kind": "field", "name": "Skąd"},
            {"n": 2, "kind": "button", "name": "Search", "submits": True,
             "method": "get"},
        ]))
        agent, asked, logged = self._agent(snap)
        agent._approved_hosts.add("eon.pl")  # the send clause has its own tests
        out = agent._browse_gate("browse_fill", {"steps": [
            {"target": "Skąd", "value": "WAW"}, {"target": "Search", "do": "click"}]})
        assert out is None
        assert asked == []
        assert agent._approved_sites == set()
        assert logged == []

    def test_a_remembered_batch_never_stands_in_for_a_grant_never_given(self):
        """The batch memo answers for the BATCH. A yes given while the site was
        already granted says nothing about a site that is not — and a memo that
        short-circuited on the card text alone would hand out the grant for
        free to any chat that had seen the same form."""
        agent, asked, _ = self._agent(self._batch(), granted=True)
        agent._browse_gate("browse_fill", {"steps": list(self.STEPS)})
        assert len(asked) == 1
        agent._approved_sites.clear()
        agent._browse_gate("browse_fill", {"steps": list(self.STEPS)})
        assert len(asked) == 2
        assert "act on eon.pl signed in as you" in asked[1]

    def test_the_batch_retry_still_rides_the_first_yes(self):
        """And the memo still does its own job: the same form, the same values,
        the same committing press, asked once."""
        agent, asked, _ = self._agent(self._batch())
        agent._browse_gate("browse_fill", {"steps": list(self.STEPS)})
        agent._browse_gate("browse_fill", {"steps": list(self.STEPS)})
        assert len(asked) == 1

class TestTheGrantIsCollectedOnTheFirstPressThatChanges:
    """#295 M4, and Law B's measured comparison for it.

    **The grant was collected on the first press, and the first press is almost
    always inert.** So the owner was asked to authorise *act on this site as
    you* while looking at a control that does nothing — a tab switch, a search
    box, an airport field. Settled against his own log: of the seventeen
    driving cards since 2026-08-24, TEN came from the site-grant card and every
    one of those was named after an inert control. The other seven came from a
    control's own card and are already correct.

    So the question moves to the first press that is CONSEQUENTIAL. An inert
    press proceeds with no card and — the half that matters as much — records
    NO GRANT: a grant taken quietly on an inert press is the same defect with a
    nicer face, because it spends the consent without ever asking for it.

    **Consequential is `Control.mutating`, and no new classifier.** It already
    answers *would pressing this change something the owner would mind*, and it
    is already the fence `browser.browse_act` and `plan_batch` enforce at act
    time. A second predicate here would be a second opinion about one question,
    and the two would drift.

    **The material is the owner's own recorded controls**, carried here as
    labels and shapes only — no test reads `~/.local/state/aish`. Each row
    carries the fate the INCUMBENT gave it and the fate the replacement gives
    it. Every row is iterated; nothing is sampled.

    **The two columns are established differently, and that is worth saying
    plainly.** The replacement's fate is DRIVEN — `_fate` builds a real `Agent`
    and calls the real gate, so it is re-derived on every run and a regression
    fails here. The incumbent's fate was measured once, by driving this same
    corpus through the shipped gate at `e34d5d0` before the change, and is
    RECORDED here as a literal: nothing in this file re-runs the old code, so a
    mis-recorded `before` would not be caught by CI. It is documentation of a
    measurement, and the direction check is only as good as it.
    """

    HOST = "eon.pl"

    #: The shapes the recorded labels were seen in. `navigates` is the
    #: enumeration's own destination signal and never the kind, and `method` is
    #: the raw attribute: an explicit `get` is HTTP's own statement that the
    #: submit is safe, and absence is not (on an SPA it usually means
    #: JavaScript intercepts and posts).
    SHAPES = {
        "navigating link": {"kind": browse.LINK, "href": "https://eon.pl/x"},
        "button": {"kind": browse.BUTTON},
        "field": {"kind": browse.FIELD},
        "submit, no method": {"kind": browse.BUTTON, "submits": True},
        "GET submit": {"kind": browse.BUTTON, "submits": True, "method": "get"},
    }

    #: (label, shape, incumbent fate, replacement fate) on an UNGRANTED site,
    #: which is the state the grant question is asked in.
    #:
    #: `free` — no card, and no grant recorded.
    #: `site card` — one card, the bare site grant, naming no control.
    #: `names it, and grants` — one card naming the press, carrying the grant.
    #: `refused` — no yes button, and the grant is never consulted (#342).
    CORPUS = (
        # The ten the log said came from the site card, in the shapes they were
        # recorded in. Every one is inert, and every one becomes free.
        ("Przełącz lokal", "navigating link", "site card", "free"),
        ("Przełącz lokal", "button", "site card", "free"),
        ("Przełącz lokal/umowę", "navigating link", "site card", "free"),
        ("Search", "button", "site card", "free"),
        ("Search", "GET submit", "site card", "free"),
        ("przeszukaj Moje Allegro", "button", "site card", "free"),
        ("przeszukaj Moje Allegro", "field", "site card", "free"),
        ("Numer przesyłki", "field", "site card", "free"),
        ("1 passenger, change number of passengers.", "button",
         "site card", "free"),
        ("Dokąd Wybierz lotnisko przylotu", "field", "site card", "free"),
        ("Dokąd Wybierz lotnisko przylotu", "button", "site card", "free"),
        ("Dodaj + — Dorośli", "button", "site card", "free"),
        # Worded and non-navigating: the presses that must keep asking, and
        # which already collected the grant on their own card since M1.
        ("Accept Jai Paliwal's invitation", "button",
         "names it, and grants", "names it, and grants"),
        ("Accept Darshan .'s invitation", "button",
         "names it, and grants", "names it, and grants"),
        ("Accept Vincent Haucke's invitation", "button",
         "names it, and grants", "names it, and grants"),
        # #342's refusals, unchanged in both directions.
        ("Kup teraz", "button", "refused", "refused"),
        ("Zapłać wybrane", "button", "refused", "refused"),
        ("Usuń przedmiot BOSCH Wycieraczka AreoTwin Retro 650mm", "button",
         "refused", "refused"),
        ("Zamawiam z obowiązkiem zapłaty", "button", "refused", "refused"),
        ("Complete booking", "button", "refused", "refused"),
        ("Wypowiedz umowę", "button", "refused", "refused"),
        # The structural half: a nondescript submit nobody wrote a method on.
        # It still asks — what moved is that the card now NAMES it, because the
        # card is drawn on the consequential press rather than on whatever
        # happened to be clicked first.
        ("Dalej", "submit, no method", "site card", "names it, and grants"),
    )

    def _drive(self, label, shape, *, evidence="", origin="user", granted=False):
        """The SHIPPED gate, driven — never modelled. Returns the fate, the
        cards drawn, and whether the grant was recorded."""
        raw = {"n": 1, "kind": browse.BUTTON, "name": label}
        raw.update(self.SHAPES[shape])
        controls = browse.controls_from([raw])
        asked, logged = [], []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
            state_log=logged.append, origin=origin,
        )
        agent._browse_view.remember(snapshot(
            url=f"https://{self.HOST}/mojeon", controls=controls,
            commit_evidence=evidence,
        ))
        if granted:
            agent._approved_sites.add(self.HOST)
        out = agent._browse_gate("browse_act", {"target": controls[0].address})
        return out, asked, logged, agent

    def _fate(self, label, shape, **kw):
        out, asked, _, agent = self._drive(label, shape, **kw)
        took = self.HOST in agent._approved_sites
        if out is not None:
            assert not asked, f"{label!r} drew a card before being refused"
            assert not took, f"{label!r} was refused and still took the grant"
            return "refused"
        if not asked:
            assert not took, f"{label!r} took the grant with no card"
            return "free"
        assert len(asked) == 1, f"{label!r} drew {len(asked)} cards"
        assert took, f"{label!r} drew the grant card and recorded nothing"
        if asked[0] == agent_module.SITE_GRANT.format(host=self.HOST):
            return "site card"
        assert agent_module.SITE_GRANT_RIDER.format(host=self.HOST) in asked[0]
        return "names it, and grants"

    def test_every_row_lands_on_the_fate_the_corpus_declares(self):
        """The measurement itself, iterated over every row. A fate is the pair
        (was he asked, was the grant taken), so a card quietly kept while the
        grant leaked — or a grant taken with no card — fails here."""
        for label, shape, _, after in self.CORPUS:
            assert self._fate(label, shape) == after, (label, shape)

    def test_the_only_permitted_move_is_an_inert_press_becoming_free(self):
        """The whole direction of the change, and the whole of it. One row is
        allowed a second move — a nondescript submit whose card now names it —
        and it is declared here rather than waved through, because it still
        asks and still grants: only the words on the card changed.

        Measured: 13 of the 22 rows move. Twelve are `site card` → `free`, and
        every one of those twelve is inert by `Control.mutating`. The
        thirteenth is the named submit. No refusal moves in either direction,
        and nothing becomes free that changes something."""
        freed, renamed = [], []
        for label, shape, before, after in self.CORPUS:
            assert self._fate(label, shape) == after, (label, shape)
            if before == after:
                continue
            if (before, after) == ("site card", "free"):
                _, _, _, agent = self._drive(label, shape)
                control = agent._browse_view.shown.controls[0]
                assert not control.mutating, f"{label!r} freed while mutating"
                freed.append((label, shape))
            elif (before, after) == ("site card", "names it, and grants"):
                renamed.append((label, shape))
            else:
                raise AssertionError(f"forbidden move: {label!r} {before}→{after}")
        # The COUNT as well as the direction: a direction check alone passes
        # happily while a widened rule frees ten more labels the right way.
        assert len(freed) == 12, freed
        assert len(renamed) == 1, renamed
        assert len(self.CORPUS) == 22

    def test_an_inert_press_records_no_grant(self):
        """The half that would be the same bug wearing a nicer face. A grant
        taken quietly on an inert press spends the consent without asking for
        it, and the next consequential press would ride a yes he never gave."""
        out, asked, logged, agent = self._drive("Przełącz lokal", "button")
        assert out is None
        assert asked == []
        assert agent._approved_sites == set()
        assert logged == []

    def test_the_first_consequential_press_collects_it_and_names_it(self):
        """One card, the merged M1 shape, naming the press the grant is being
        taken on — and the grant recorded exactly as `_grant_site` always
        recorded it, because a reopened chat replays it from that record."""
        out, asked, logged, agent = self._drive("Dalej", "submit, no method")
        assert out is None
        assert asked == [
            "click button 'Dalej' on eon.pl — "
            + agent_module.SITE_GRANT_RIDER.format(host=self.HOST)
        ]
        assert agent._approved_sites == {self.HOST}
        assert {"kind": "site_grant", "host": self.HOST} in logged

    def test_an_inert_press_first_does_not_spend_the_later_ones_card(self):
        """The sequence the owner actually walks: switch a tab, look at the
        page, then press the thing that does something. The card arrives on the
        third act and names it, where before it arrived on the first and named
        a tab switch."""
        raws = [
            {"n": 1, "kind": browse.BUTTON, "name": "Przełącz lokal"},
            {"n": 2, "kind": browse.BUTTON, "name": "Dalej", "submits": True},
        ]
        controls = browse.controls_from(raws)
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(snapshot(
            url=f"https://{self.HOST}/mojeon", controls=controls
        ))
        assert agent._browse_gate("browse_act", {"target": "Przełącz lokal"}) is None
        assert asked == []
        assert agent._browse_gate("browse_act", {"target": "Dalej"}) is None
        assert len(asked) == 1
        assert "Dalej" in asked[0]
        assert "Przełącz lokal" not in asked[0]

    def test_a_refusal_never_consults_the_grant_entry_by_entry(self):
        """#342's refusals are above the grant in `_browse_gate` and must stay
        there: a refusal that reached the grant could be turned into a card by
        a site the owner had already said yes to.

        Swept over EVERY entry of EVERY commit list rather than a sample — the
        `zamawiam` lesson applied to the guard. Driven both ways, granted and
        ungranted, and the two refusals must be the same string."""
        incumbent = TestTheCommitVerbsLeaveTheApprovableSet()
        for name, entries in sorted(incumbent._commit_lists().items()):
            if incumbent.LIST_KINDS[name] != "refuses":
                continue
            for entry in entries:
                cold, asked, logged, agent = self._drive(entry, "button")
                warm, warm_asked, _, _ = self._drive(entry, "button", granted=True)
                assert cold is not None, f"{name}: {entry!r}"
                assert cold == warm, f"{name}: {entry!r} reads the grant"
                assert asked == warm_asked == [], f"{name}: {entry!r}"
                assert agent._approved_sites == set(), f"{name}: {entry!r}"
                assert logged == [], f"{name}: {entry!r}"

    def test_a_granted_site_is_completely_unaffected(self):
        """What M4 changes is WHEN the grant is collected, never what it means
        or how it matches. Past the grant, every fate is the one it always
        was."""
        for label, shape, _, _ in self.CORPUS:
            out, asked, _, _ = self._drive(label, shape, granted=True)
            if browse.commits(label, navigates="href" in self.SHAPES[shape]):
                assert out is not None, label
                continue
            assert out is None, label
            for card in asked:
                assert "signed in as you" not in card, label

    def test_the_suffix_match_and_the_scope_are_untouched(self):
        """`_site_granted` is not what this slice changes. A grant on the site
        still covers a country subdomain and still covers nothing above it."""
        agent = Agent(model="fake", approve=lambda _c: True,
                      client_chat=lambda **kw: {})
        agent._approved_sites.add("linkedin.com")
        assert agent._site_granted("pl.linkedin.com")
        assert agent._site_granted("linkedin.com")
        assert not agent._site_granted("evil-linkedin.com")
        assert not agent._site_granted("com")

    # ------------------------------------------------------------ the batch

    def _batch(self, *, get=True, worded=False):
        return snapshot(url=f"https://{self.HOST}/mojeon", controls=(
            browse.controls_from([
                {"n": 1, "kind": "field", "name": "Skąd"},
                {"n": 2, "kind": "button", "submits": True,
                 "name": "Wyślij" if worded else "Szukaj",
                 **({"method": "get"} if get else {})},
            ])
        ))

    def _batch_agent(self, snap, *, vouched=True):
        asked, logged = [], []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
            state_log=logged.append,
        )
        agent._browse_view.remember(snap)
        if vouched:
            # The send question (#295 M3) has its own tests; it is not what is
            # being counted here.
            agent._approved_hosts.add(self.HOST)
        return agent, asked, logged

    BATCH_STEPS = ({"target": "Skąd", "value": "WAW"},
                   {"target": "Szukaj", "do": "click"})

    def test_a_batch_of_inert_steps_asks_nothing_and_grants_nothing(self):
        """The same move as the single press: type into a box and press a GET
        search, which is a link with the query typed into it. His own
        `browse_fill` cards — `Dokąd Wybierz lotnisko przylotu`, `Dodaj + —
        Dorośli` — were this."""
        agent, asked, logged = self._batch_agent(self._batch())
        out = agent._browse_gate("browse_fill", {"steps": list(self.BATCH_STEPS)})
        assert out is None
        assert asked == []
        assert agent._approved_sites == set()
        assert logged == []

    def test_a_batch_with_a_consequential_step_asks_once_and_names_it(self):
        """One card, naming the form and every value it would send, with the
        grant riding it on a line of its own."""
        agent, asked, logged = self._batch_agent(self._batch(get=False))
        out = agent._browse_gate("browse_fill", {"steps": list(self.BATCH_STEPS)})
        assert out is None
        assert len(asked) == 1
        assert "fill in this form on eon.pl" in asked[0]
        assert asked[0].endswith(
            "\n" + agent_module.SITE_GRANT_RIDER.format(host=self.HOST)
        )
        assert agent._approved_sites == {self.HOST}
        assert {"kind": "site_grant", "host": self.HOST} in logged

    def test_a_batch_whose_press_is_worded_still_draws_its_own_card(self):
        """The floor the grant explicitly does not cover is unmoved: a name
        that says it sends draws a card whatever else is true."""
        agent, asked, _ = self._batch_agent(self._batch(worded=True))
        steps = [{"target": "Skąd", "value": "WAW"},
                 {"target": "Wyślij", "do": "click"}]
        assert agent._browse_gate("browse_fill", {"steps": steps}) is None
        assert len(asked) == 1
        assert "Wyślij" in asked[0]

    # ------------------------------------------- what still holds around it

    def test_the_driven_send_question_still_fires_on_a_freed_press(self):
        """**The composition M3 shipped for, checked here as well as there.**
        The path this slice opens is: open an attacker's page (free), type
        prose into its box (free), press its GET submit — which M4 makes inert
        and therefore free of the grant card. The send question is a DIFFERENT
        grant asked about a different thing, so it still fires: the press asks
        nothing about the site and everything about the egress."""
        agent, asked, _ = self._batch_agent(
            self._batch(), vouched=False,
        )
        agent._tainted = True
        out = agent._browse_gate("browse_fill", {"steps": list(self.BATCH_STEPS)})
        assert out is None
        assert len(asked) == 1
        assert "send data to eon.pl" in asked[0]
        # …and the site half is genuinely absent, not merely outvoted.
        assert "signed in as you" not in asked[0]
        assert agent._approved_sites == set()

    def test_a_password_field_is_still_refused_before_any_of_this(self):
        """The never-typed floor and the password refusal sit above the grant
        and are untouched by when it is collected."""
        controls = browse.controls_from(
            [{"n": 1, "kind": browse.PASSWORD, "name": "Hasło"}]
        )
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(snapshot(
            url=f"https://{self.HOST}/mojeon", controls=controls
        ))
        out = agent._browse_gate(
            "browse_act", {"target": "Hasło", "action": "type", "text": "x"}
        )
        assert out is not None and "password" in out.lower()
        assert asked == []
        assert agent._approved_sites == set()


    # ------------------------------------------------- the Enter-key submit

    def _enter_page(self, *, get, form="f1"):
        return snapshot(url=f"https://{self.HOST}/x", controls=browse.controls_from([
            {"n": 1, "kind": "field", "name": "Skąd", "form": form},
            {"n": 2, "kind": "button", "name": "Go", "submits": True,
             "form": form, **({"method": "get"} if get else {})},
        ]))

    def _enter(self, snap, *, vouched=True, monkeypatch=None):
        if monkeypatch is not None:
            # A submit card carries the form's held values, read LIVE (#251).
            # There is no browser here and that fallback is not what is under
            # test; the grant clause is.
            monkeypatch.setattr(browser, "browse_fields", lambda **kw: [])
        asked, logged = [], []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
            state_log=logged.append,
        )
        agent._browse_view.remember(snap)
        if vouched:
            agent._approved_hosts.add(self.HOST)  # the send clause has its tests
        out = agent._browse_gate("browse_act", {
            "target": "Skąd", "action": "type", "text": "WAW", "submit": True,
        })
        return out, asked, logged, agent

    def test_pressing_enter_in_a_field_is_judged_by_the_form_it_sends(
        self, monkeypatch
    ):
        """A FIELD is never `mutating` — typing changes nothing until something
        is pressed — so read off the named control alone, `submit=True` would
        skip the grant while clicking that same form's button collects it. The
        difference between the two is an argument the model picks, which is the
        bypass shape this file has twice had to remove (#287, #310)."""
        out, asked, logged, agent = self._enter(
            self._enter_page(get=False), monkeypatch=monkeypatch
        )
        assert out is None
        assert len(asked) == 1
        assert agent_module.SITE_GRANT_RIDER.format(host=self.HOST) in asked[0]
        assert agent._approved_sites == {self.HOST}
        assert {"kind": "site_grant", "host": self.HOST} in logged

    def test_enter_in_a_search_box_stays_free(self):
        """And the same predicate keeps it honest in the other direction: an
        explicit `method="get"` form is a link with the query typed into it, so
        Enter there is as inert as clicking its button. His `przeszukaj Moje
        Allegro` and `Numer przesyłki` are exactly this."""
        out, asked, logged, agent = self._enter(self._enter_page(get=True))
        assert out is None
        assert asked == []
        assert agent._approved_sites == set()
        assert logged == []

    def test_a_form_this_cannot_see_into_fails_closed(self):
        """An explicit `submit=True` is the model asking to SEND. A field with
        no resolvable submit beside it is treated as one that changes something
        — being wrong costs the card he is shown today, and the other direction
        costs the grant."""
        lone = snapshot(url=f"https://{self.HOST}/x", controls=browse.controls_from(
            [{"n": 1, "kind": "field", "name": "Skąd"}]
        ))
        out, asked, _, agent = self._enter(lone)
        assert out is None
        assert len(asked) == 1
        assert agent._approved_sites == {self.HOST}


    def test_the_bare_site_card_survives_where_there_is_nothing_to_name(self):
        """The one path with no control to name: the target did not resolve
        against the snapshot, so the gate fails closed and asks about the site
        alone. It is reachable ATTENDED as well as unattended — the doc claimed
        otherwise until a review drove it, and a sentence about which card he
        can be shown has to be checked rather than reasoned about."""
        for origin in ("user", "schedule"):
            asked = []
            agent = Agent(
                model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
                approve_tool=lambda n, a, p, seen=asked: seen.append(p) or True,
                origin=origin,
            )
            agent._approved_hosts.add(self.HOST)  # the send clause has its tests
            agent._browse_view.remember(snapshot(
                url=f"https://{self.HOST}/x",
                controls=browse.controls_from(
                    [{"n": 1, "kind": "field", "name": "Skąd"}]
                ),
            ))
            out = agent._browse_gate("browse_act", {
                "target": "nothing on this page", "action": "type",
                "text": "WAW", "submit": True,
            })
            assert out is None, origin
            assert asked == [agent_module.SITE_GRANT.format(host=self.HOST)], origin
            assert agent._approved_sites == {self.HOST}, origin

    def test_typing_without_submitting_is_still_free(self):
        """Unchanged, and it is the floor the whole slice stands on: nothing is
        committed until something is pressed."""
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(self._enter_page(get=False))
        agent._approved_hosts.add(self.HOST)
        out = agent._browse_gate(
            "browse_act", {"target": "Skąd", "action": "type", "text": "WAW"}
        )
        assert out is None
        assert asked == []
        assert agent._approved_sites == set()

    def test_a_granted_site_gains_no_card_from_any_of_this(self, monkeypatch):
        """The Enter branch decides only WHEN the grant is collected. Past the
        grant it has no effect at all, so this slice adds no card class."""
        for get in (True, False):
            asked = []
            agent = Agent(
                model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
                approve_tool=lambda n, a, p, seen=asked: seen.append(p) or True,
            )
            monkeypatch.setattr(browser, "browse_fields", lambda **kw: [])
            agent._browse_view.remember(self._enter_page(get=get))
            agent._approved_sites.add(self.HOST)
            agent._approved_hosts.add(self.HOST)
            out = agent._browse_gate("browse_act", {
                "target": "Skąd", "action": "type", "text": "WAW", "submit": True,
            })
            assert out is None, get
            assert asked == [], get

    def test_a_checkout_page_puts_the_first_submit_back_behind_a_named_card(self):
        """Page evidence is read in the escalating direction only, and it is
        read before the grant is collected: a submit on a page showing commit
        structure needs a card of its own, which is also the press the grant
        rides on."""
        assert self._fate(
            "Dalej", "submit, no method", evidence="a payment provider frame"
        ) == "names it, and grants"

    def test_an_unattended_session_keeps_todays_strictness(self):
        """Nobody is going to read the answer, so the card cannot be justified
        by his attention and is not thinned by its absence. Every press in a
        triggered session is treated as due, exactly as before — the ONE thing
        that changed there is that the card now names the press."""
        for label, shape, _, _ in self.CORPUS:
            if browse.commits(label, navigates="href" in self.SHAPES[shape]):
                continue
            out, asked, _, agent = self._drive(label, shape, origin="schedule")
            assert out is None, label
            assert len(asked) == 1, label
            assert "signed in as you" in asked[0], label
            assert agent._approved_sites == {self.HOST}, label


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
    """The CELLS are asked first; the heading is the fallback. Each cell states
    its own full date, which is what makes it pressable at all (#273) — and
    `ngb-datepicker` labels itself "Travel Dates" and puts its months in
    sub-headings it never associates with the grid, so the heading alone left
    the walk with nothing to steer by."""

    def test_the_cells_win_over_a_heading_that_contradicts_them(self):
        """This was the other way round, and lot.com is why it changed. The
        "grid heading" is whatever `aria-labelledby` points at, and there it
        points at the date FIELD's label — *"Wybierz datę wylot z zakresu od 1
        września 2026 do 28 sierpnia 2027"*. That parses, so the heading branch
        won and returned a month the picker was not showing and never would,
        CONSTANT across every hop; the walk then oscillated between two arrows
        that were both "beyond" it and gave up with *"the picker stopped
        changing"*. A cell asserts what it IS; a heading is a claim about the
        grid. Where they disagree the cells are the evidence."""
        cells = [browse.Cell(tag=1, text="1", label="1 August 2026")]
        assert browse.months_on_show(cells, "wrzesień 2026") == [(2026, 8)]

    def test_the_heading_still_answers_when_the_cells_say_nothing(self):
        """Demoting it costs nothing: a grid whose cells resolve no month is one
        `pick_day` refuses anyway, so the heading is all there is to steer by
        and it is still used."""
        cells = [browse.Cell(tag=1, text="1"), browse.Cell(tag=2, text="2")]
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

    def test_the_walk_takes_the_nearest_month_beyond_the_edge(self):
        """lot.com offers BOTH ends at once — showing October and November its
        nav reads `September 2026` and `December 2026`. First-in-document-order
        is a coin flip that walks backwards half the time; nearest is also what
        keeps a year-jump arrow from eating twelve of `MONTH_HOPS`."""
        nav = [
            {"tag": 1, "name": "September 2026"},
            {"tag": 2, "name": "December 2027"},
            {"tag": 3, "name": "December 2026"},
        ]
        on_show = [(2026, 10), (2026, 11)]
        assert browse.choose_arrow(nav, on_show, forward=True)["tag"] == 3
        assert browse.choose_arrow(nav, on_show, forward=False)["tag"] == 1

class TestTheCardSaysWhatIsAboutToBeSent:
    """#251. The card named what was about to be PRESSED and never what was
    about to be SENT — and that is the half that can have gone stale, because
    filling a form needs no approval, so values are set in one call and
    submitted in another with a page free to reset a date in between."""

    def _form(self):
        return browse.controls_from([
            {"n": 1, "kind": "field", "name": "Skąd", "detail": "currently: WAW",
             "form": "0:0"},
            {"n": 2, "kind": "field", "name": "Dokąd", "form": "0:0"},
            {"n": 3, "kind": "password", "name": "Hasło", "form": "0:0"},
            {"n": 4, "kind": "check", "name": "Regulamin", "detail": "checked",
             "form": "0:0"},
            {"n": 5, "kind": "field", "name": "Szukaj w pomocy",
             "detail": "currently: x", "form": "0:9"},
            {"n": 6, "kind": "button", "name": "Wyślij", "submits": True,
             "form": "0:0"},
        ])

    def test_it_reads_the_form_the_button_would_send(self):
        controls = self._form()
        held = dict(browse.form_values(controls, controls[-1]))
        assert held["Skąd"] == "WAW"
        assert "Szukaj w pomocy" not in held  # a different form on the same page

    def test_an_empty_field_says_so_rather_than_being_left_out(self):
        """A field the owner expected to be filled and is not is exactly what
        he needs the card to show him."""
        controls = self._form()
        assert dict(browse.form_values(controls, controls[-1]))["Dokąd"] == "(empty)"

    def test_a_password_is_named_but_never_read_back(self):
        controls = self._form()
        assert dict(browse.form_values(controls, controls[-1]))["Hasło"] == browse.NOT_TYPED

    def test_a_control_in_no_form_has_nothing_to_report(self):
        loose = browse.controls_from([{"n": 1, "kind": "button", "name": "Zapłać"}])
        assert browse.form_values(loose, loose[0]) == []

    def test_a_long_form_is_cut_with_the_cut_counted(self):
        many = browse.controls_from(
            [{"n": i, "kind": "field", "name": f"Pole {i}", "form": "0:0"}
             for i in range(20)]
            + [{"n": 99, "kind": "button", "name": "Zapłać", "form": "0:0"}]
        )
        note = browse.form_note(browse.form_values(many, many[-1]))
        assert "and 8 more field(s)" in note

    def test_the_card_carries_it(self, monkeypatch):
        controls = self._form()
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(snapshot(controls=controls))
        agent._approved_sites.add("eon.pl")
        monkeypatch.setattr(browser, "browse_fields", lambda **kw: controls)
        assert agent._browse_gate("browse_act", {"target": "Wyślij"}) is None
        assert "this form currently holds:" in asked[0]
        assert "Skąd: WAW" in asked[0]

    def test_a_stale_reading_is_never_presented_as_a_current_one(self, monkeypatch):
        """A value read a minute ago and shown as current is the failure this
        exists to prevent, so it must not be able to happen silently."""
        controls = self._form()
        asked = []
        agent = Agent(
            model="fake", approve=lambda _c: True, client_chat=lambda **kw: {},
            approve_tool=lambda n, a, p: asked.append(p) or True,
        )
        agent._browse_view.remember(snapshot(controls=controls))
        agent._approved_sites.add("eon.pl")
        # The live read fails — no page, a torn-down browser, a busy loop.
        monkeypatch.setattr(browser, "browse_fields", lambda **kw: [])
        agent._browse_gate("browse_act", {"target": "Wyślij"})
        assert "when aish last looked" in asked[0]


# Twelve month names as a browser's `Intl` returns them, for two languages
# `_MONTH_STEMS` does not hold. GERMAN is the one aish could not read at all;
# FRENCH is the one whose stems must grow past three letters.
GERMAN_MONTHS = (
    "Januar Februar März April Mai Juni "
    "Juli August September Oktober November Dezember"
).split()
FRENCH_MONTHS = (
    "janvier février mars avril mai juin "
    "juillet août septembre octobre novembre décembre"
).split()


class TestWhatRealPickersActuallyLookLike:
    """Measured, not imagined (#251). `scripts/probe_calendars.py` opens real
    booking sites and prints what their pickers are made of — and every rule
    below was wrong until it did, because the fixtures this repo wrote for
    itself were tidier than the web."""

    def test_a_month_arrow_is_called_things_no_guess_would_hold(self):
        """wizzair.com's are "Later dates" and "calendar page forward". A list
        guessing at "next month" matched none of them, and a date three months
        out failed with the arrows plainly on screen."""
        for said in ("Later dates", "calendar page forward"):
            assert browse.month_step(said, forward=True), said
        for said in ("Previous dates", "calendar page back"):
            assert browse.month_step(said, forward=False), said

    def test_the_vocabulary_is_still_closed(self):
        """The fence that makes a widened list safe is that a name not on it
        refuses rather than guesses."""
        assert not browse.month_step("Next offer in this carousel", forward=True)
        assert not browse.month_step("wrapper", forward=True)

    def test_an_arrow_can_be_called_the_month_it_goes_to(self):
        """lot.com's, measured off the session that filed this: showing August
        and September 2026 the forward arrow is `October 2026`, and after a
        jump to March/April 2027 the pair reads `February 2027` / `May 2027`.
        Not one word of `_FORWARD` appears in any of them."""
        for said in ("October 2026", "February 2027", "May 2027"):
            assert not browse.month_step(said, forward=True), said
            assert not browse.month_step(said, forward=False), said
        assert browse.month_arrow("October 2026") == (2026, 10)
        assert browse.month_arrow("February 2027") == (2027, 2)
        assert browse.month_arrow("May 2027") == (2027, 5)
        assert browse.month_arrow("Marzec 2027") == (2027, 3)

    def test_a_sentence_that_merely_names_a_month_is_not_an_arrow(self):
        """This is a control aish presses with nobody looking, so the whole
        name has to BE the month — one word a stem starts, one that is the
        year, nothing else."""
        for said in (
            "Show March 2027 deals", "March", "2027", "Bezposrednio z Gdanska",
            "od 5 kwietnia 2027 polecisz", "next", "",
        ):
            assert browse.month_arrow(said) is None, said

    def test_the_month_named_arrow_is_chosen_against_the_grid(self):
        """The direction is never read off the label. The month it names has to
        lie strictly outside the span on show, on the side being walked to —
        which is what stops the BACKWARD arrow being pressed on a forward walk,
        and what excludes a header button carrying a month already displayed."""
        nav = [
            {"tag": 1, "name": "February 2027"},
            {"tag": 2, "name": "March 2027"},
            {"tag": 3, "name": "May 2027"},
        ]
        on_show = [(2027, 3), (2027, 4)]
        assert browse.choose_arrow(nav, on_show, forward=True)["tag"] == 3
        assert browse.choose_arrow(nav, on_show, forward=False)["tag"] == 1

    def test_a_worded_arrow_still_wins_over_a_month_named_one(self):
        """A control that SAYS "next month" says what it does; the month shape
        is the fallback under it, not a replacement for it."""
        nav = [{"tag": 1, "name": "May 2027"}, {"tag": 2, "name": "Next month"}]
        assert browse.choose_arrow(nav, [(2027, 3)], forward=True)["tag"] == 2

    def test_a_mute_grid_gets_no_month_named_arrow(self):
        """`on_show` is one of the two operands of the comparison. With no span
        there is nothing to compare against, so a month on a label decides
        nothing — the walk refuses rather than guessing a direction."""
        nav = [{"tag": 1, "name": "October 2026"}]
        assert browse.choose_arrow(nav, [], forward=True) is None
        assert browse.month_step("October 2026", forward=True) is False

    def test_the_month_names_come_from_the_page_not_from_aish(self):
        """`_MONTH_STEMS` is Polish and English. Measured: a German arrow reads
        as no month at all against it, so the whole date step — cells, heading
        and arrows alike — is blind on a German picker, and the fix under the
        old design was another hand-written table entry per language.

        `CALENDAR_JS` asks `Intl` in the page's own locale instead, so a
        language aish has never heard of arrives with no code change."""
        german = GERMAN_MONTHS
        assert browse.month_arrow("Oktober 2026") is None
        table = browse.month_table({"de": german})
        assert browse.month_arrow("Oktober 2026", table) == (2026, 10)
        assert browse.month_of("15. Dezember 2026", table) == 12

    def test_the_language_decides_the_stem_length_not_aish(self):
        """Three letters is right for Polish and WRONG for French: `juin` and
        `juillet` are one word until the fourth, and a collision here does not
        fail loudly — it reads June as July on somebody's trip. So the shortest
        length at which all twelve are distinct is derived per language."""
        polish = browse.month_stems(
            "styczeń luty marzec kwiecień maj czerwiec lipiec sierpień "
            "wrzesień październik listopad grudzień".split()
        )
        assert polish == tuple((s,) for s in (
            "sty lut mar kwi maj cze lip sie wrz paz lis gru".split()
        )), polish
        french = browse.month_stems(FRENCH_MONTHS)
        assert french[5] == ("juin",) and french[6] == ("juil",), french

    def test_a_language_whose_months_cannot_be_told_apart_contributes_nothing(self):
        """A table that cannot separate twelve months would match the wrong one
        silently. It is dropped rather than guessed at, and `month_table`
        returning None leaves the built-in floor exactly as it was."""
        assert browse.month_stems(["same"] * 12) is None
        assert browse.month_stems(["only", "three"]) is None
        assert browse.month_table({"xx": ["same"] * 12}) is None
        assert browse.month_table({}) is None

    def test_where_the_two_tables_disagree_neither_wins(self):
        """A built-in stem saying one month while the page's own language says
        another is aish not knowing, not a tie to break by ordering. Every
        caller refuses on None rather than pressing something."""
        wrong = tuple(("mar",) for _ in range(12))
        assert browse.month_of("marzec 2027") == 3
        assert browse.month_of("marzec 2027", wrong) is None

    def test_the_page_table_is_added_to_the_floor_never_swapped_for_it(self):
        """Every page aish already reads must behave exactly as before, so the
        built-in table still answers on its own."""
        table = browse.month_table({"de": GERMAN_MONTHS})
        assert browse.month_of("7 września 2026", table) == 9
        assert browse.month_of("7 September 2026", table) == 9

    def test_the_picker_and_the_page_name_a_control_the_same_way(self):
        """One naming ladder, shared verbatim — the picker had a naive one of
        its own, so icon-only arrows were named "" and dropped."""
        assert "const nameOf" in browse.NAME_JS
        assert browse.CONTROLS_JS.count("const nameOf") == 1
        assert browse.CALENDAR_JS.count("const nameOf") == 1

    def test_the_same_date_twice_is_not_an_ambiguity(self):
        """A picker that keeps a second copy of every pane for its animation —
        138 cells for two months of about 77 — is not asking a question."""
        twice = [
            browse.Cell(tag=1, text="15", label="15 December 2026"),
            browse.Cell(tag=2, text="15", label="15 December 2026", onscreen=True),
        ]
        assert browse.pick_day(twice, browse.Day(15, 12, 2026)).tag == 2

    def test_a_cell_below_the_fold_is_still_pressable(self):
        """`onscreen` is a tie-break between duplicates, never a filter."""
        only = [browse.Cell(tag=1, text="15", label="15 December 2026")]
        assert browse.pick_day(only, browse.Day(15, 12, 2026)).tag == 1

    def test_the_picker_clears_its_own_stale_tags(self):
        """Every month hop re-stamps from 1, so without this one number matched
        a cell from the month aish had just left — "two elements, one number,
        silently", the defect CONTROLS_JS already records for data-aish-n."""
        assert "removeAttribute('data-aish-cell')" in browse.CALENDAR_JS


def page_snippets():
    """Every JS snippet aish runs in a page, by name.

    Shared by both guards below because they ask about the same set: one that
    each snippet crosses shadow boundaries, one that a tag's writer and its
    readers cross the same ones."""
    from aish import browser as browser_module

    found = {}
    for module in (browse, browser_module):
        for name in dir(module):
            if name.endswith("_JS") and isinstance(getattr(module, name), str):
                found[name] = getattr(module, name)
    return found


class TestEveryPageReaderLooksThroughShadowRoots:
    """The boundary is handled ONCE, and no new snippet may forget it (#273).

    Three of the four defects that made qatarairways.com unusable were the same
    mistake in different places: `document.querySelector`, `document
    .activeElement` and `Node.contains` all stop at a shadow boundary, and a
    growing share of the web puts its whole application inside one. Enumeration
    walked shadow roots; the calendar reader, the label lookup and the focus
    test did not.

    That mixture is worse than being wrong everywhere. Consistently blind would
    have been noticed years ago; visible-then-invisible reads as "this page has
    no date cells" on a page whose cells aish had itself just tagged. So this
    is a list, not a rule of thumb: a snippet either looks through boundaries
    or is named here with the reason it need not."""

    # Snippets that are document-scoped ON PURPOSE. The reason is the point —
    # an entry with a bad reason is how this test stops meaning anything.
    ALLOWED = {
        "_IMAGES_JS": "reads <meta> only, and <head> is never in a shadow root",
        "_MAIN_JS": "the page's own <main>, a budget choice about the top level",
        "_WATCH_JS": "installs one observer on the document, not a lookup",
        "CENTRE_JS": "scrolls the element it is given",
        "OPTIONS_JS": "reads options off the <select> it is given",
        "_ACTIVATION_JS": "reads the control it is given, plus location",
        "_COVERED_JS": "walks el's own host chain — see the `chain` set",
        "_DEEP_ACTIVE_JS": "IS the descent, for focus",
        "NAME_JS": "closest('label') — a label is in its control's own root",
    }

    def _snippets(self):
        return page_snippets()

    def test_the_inventory_is_not_empty(self):
        """A rename that empties the sweep must fail loudly, not pass."""
        found = self._snippets()
        assert len(found) >= 15, sorted(found)
        for name in ("CONTROLS_JS", "CALENDAR_JS", "FLOOD_JS", "SIGNIN_FORM_JS"):
            assert name in found

    def test_every_lookup_crosses_the_boundary_or_says_why_not(self):
        import re

        blind = {
            # `querySelectorAll?` would mean "querySelectorAl" + optional "l"
            # and never match the SINGULAR call — which is how the first sweep
            # for this missed several. The guard's own probe test caught it.
            "document.querySelector": r"document\.querySelector(All)?\(",
            "document.getElementById": r"document\.getElementById\(",
            "document.activeElement": r"document\.activeElement",
            "elementFromPoint": r"document\.elements?FromPoint\(",
            "closest()": r"\.closest\(",
            "contains()": r"\.contains\(",
        }
        offenders = {}
        for name, js in self._snippets().items():
            if name in self.ALLOWED:
                continue
            # Either it descends itself, or it walks hosts by hand.
            if "shadowRoot" in js or "getRootNode" in js:
                continue
            hits = [what for what, pat in blind.items() if re.search(pat, js)]
            if hits:
                offenders[name] = hits
        assert not offenders, (
            "these read the page but stop at a shadow boundary — use DEEP_JS's "
            f"deepAll/deepOne/deepById, or name it in ALLOWED with why: {offenders}"
        )

    def test_the_allow_list_has_no_stale_entries(self):
        """A snippet that has been deleted or fixed must leave the list, or the
        next one to take its name inherits an exemption nobody meant to give."""
        found = self._snippets()
        gone = sorted(set(self.ALLOWED) - set(found))
        assert not gone, f"named in ALLOWED but no longer exists: {gone}"

    def test_the_sweep_actually_catches_one(self, monkeypatch):
        """A guard that cannot fail is decoration. This plants exactly the
        mistake #273 was — a lookup that stops at the boundary — and the sweep
        has to find it."""
        monkeypatch.setattr(
            browse, "REGRESSION_PROBE_JS",
            "() => document.querySelector('input[type=password]')",
            raising=False,
        )
        with pytest.raises(AssertionError, match="stop at a shadow boundary"):
            self.test_every_lookup_crosses_the_boundary_or_says_why_not()

    def test_a_planted_snippet_that_descends_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(
            browse, "REGRESSION_PROBE_JS",
            "() => { for (const e of document.querySelectorAll('*')) "
            "if (e.shadowRoot) {} }",
            raising=False,
        )
        self.test_every_lookup_crosses_the_boundary_or_says_why_not()

    def test_the_helper_is_shared_not_copied(self):
        """It lives outside REACH_JS because the snippets that needed it most
        were the ones that did not include REACH_JS. A helper only the
        already-correct code can reach fixes nothing."""
        from aish import browser as browser_module

        assert "deepAll" in browse.DEEP_JS
        assert browse.REACH_JS.startswith(browse.DEEP_JS)
        for js in (browse.FLOOD_JS, browser_module.SIGNIN_FORM_JS,
                   browser_module.SECOND_FACTOR_JS):
            assert browse.DEEP_JS in js, "copied instead of shared"


DEEP = "crosses shadow boundaries"
LIGHT = "stops at the light DOM"


class TestATagsWriterAndItsReadersHaveTheSameReach:
    """#292. The sweep above is the WEAKER half, and would not have caught the
    bug that prompted it: it asks whether a snippet crosses shadow boundaries,
    never whether two snippets that talk to each other cross the SAME ones.

    Both times this bit, the shape was identical — one pass stamps an attribute
    on an element, another pass looks that attribute up and cannot see it:

    - `CONTROLS_JS` tagged `data-aish-n` through shadow roots and `CALENDAR_JS`
      looked it up with `document.querySelector`, which reported "no day cells
      were found in the picker that opened".
    - `SIGNIN_FORM_JS` tags `data-aish-signin` and `SIGNIN_STILL_OURS_JS` reads
      it back. Making the writer deep without the reader would have had aish
      tag the password field and then refuse to type into it, saying "the
      password field is gone" — and that one was introduced BY the fix for the
      first.

    Both halves pass the sweep above individually. Nothing checked that they
    agree, and the failure surfaces as a FACT ABOUT THE PAGE rather than as a
    defect, so nobody goes looking for a bug.

    The pairing is WRITTEN OUT below rather than inferred. There are three tags;
    a regex that guessed which snippet reads which tag would be one more silent
    thing to be wrong, in a guard whose whole subject is silence."""

    # tag → who stamps it, who looks it up, and how far each of them can see.
    #
    # The Playwright-side readers — `_find`'s `locator('[data-aish-n=…]')`, the
    # cell press, and the sign-in `fill` steps — are deliberately absent: the
    # CSS engine pierces open shadow roots, so they are DEEP by construction and
    # can never be the shallow half of a pair. The tag sweep below still covers
    # them, so a tag they invent cannot go unlisted.
    TAGS = {
        # The page's control numbering. `CONTROLS_JS` stamps it inside the walk
        # that descends shadow roots, and clears last pass's tag in the same
        # walk; `CALENDAR_JS` looks the field up to find whose picker is open.
        "data-aish-n": {
            "writes": {"CONTROLS_JS": DEEP},
            "reads": {"CONTROLS_JS": DEEP, "CALENDAR_JS": DEEP},
        },
        # The picker's own numbering, written and cleared entirely inside
        # `CALENDAR_JS`. LIGHT on both sides, and consistent with itself: the
        # cells come from `grid.querySelectorAll`, so a tag can only ever be
        # stamped where the document-wide clear can reach it.
        "data-aish-cell": {
            "writes": {"CALENDAR_JS": LIGHT},
            "reads": {"CALENDAR_JS": LIGHT},
        },
        # The login form's fields, re-read immediately before the press because
        # the tag survives a same-document change — and once more before the
        # evidence frame, to force the password field back to type="password"
        # (#320, #295). A shallow reader there would leave an UNMASKED field
        # inside a web component and then photograph it in plaintext, which is
        # this table's failure mode with the worst possible consequence.
        "data-aish-signin": {
            "writes": {"SIGNIN_FORM_JS": DEEP},
            "reads": {"SIGNIN_STILL_OURS_JS": DEEP, "SIGNIN_MASK_JS": DEEP},
        },
    }

    # How a lookup of a tag says how far it can see. Only lookups are visible
    # this way: a `setAttribute` carries the reach of whatever walk found the
    # element, which no regex can read — that half of the table is the human's,
    # and is why the table is written out rather than inferred.
    LOOKUP = {
        DEEP: r"deep(?:One|All|ById)\(\s*['\"]\[{tag}",
        LIGHT: r"document\.(?:querySelector(?:All)?|getElementById)\(\s*['\"]\[?{tag}",
    }

    def _entries(self, tags):
        for tag, sides in tags.items():
            for side in ("writes", "reads"):
                for name, reach in sides[side].items():
                    yield tag, side, name, reach

    def _disagreements(self, tags):
        """Tags whose writers and readers do not claim the same reach."""
        bad = {}
        for tag, sides in tags.items():
            claimed = set(sides["writes"].values()) | set(sides["reads"].values())
            if len(claimed) > 1:
                bad[tag] = sorted(claimed)
        return bad

    def _misdeclared(self, tags, snippets):
        """Entries whose lookups contradict the reach the table claims."""
        import re

        bad = {}
        for tag, _side, name, reach in self._entries(tags):
            js = snippets.get(name, "")
            seen = {
                how
                for how, pattern in self.LOOKUP.items()
                if re.search(pattern.format(tag=re.escape(tag)), js)
            }
            if seen and seen != {reach}:
                bad[(tag, name)] = f"claims {reach!r}, looks it up {sorted(seen)}"
        return bad

    def test_a_writer_and_its_readers_claim_the_same_reach(self):
        assert not self._disagreements(self.TAGS), (
            "one pass stamps this tag and another cannot see what it stamped — "
            "the reader reports it as a fact about the page"
        )

    def test_the_claimed_reach_is_the_one_the_code_uses(self):
        assert not self._misdeclared(self.TAGS, page_snippets()), (
            "the table says one thing and the lookup does another, which makes "
            "the pairing above meaningless"
        )

    def test_every_snippet_that_touches_a_tag_is_in_the_table(self):
        """A new reader that nobody paired is exactly how this happened twice.
        The table must name every snippet that mentions the tag — not the ones
        somebody remembered."""
        snippets = page_snippets()
        unlisted = {}
        for tag, sides in self.TAGS.items():
            named = set(sides["writes"]) | set(sides["reads"])
            touching = {name for name, js in snippets.items() if tag in js}
            if touching - named:
                unlisted[tag] = sorted(touching - named)
        assert not unlisted, (
            "these snippets touch a tag and the table does not say how far they "
            f"can see — add them to TAGS with their reach: {unlisted}"
        )

    def test_the_table_has_no_stale_entries(self):
        """A snippet that stopped using a tag must leave, or the next one to
        take its name inherits a claim nobody checked."""
        snippets = page_snippets()
        gone = sorted(
            f"{name} for {tag}"
            for tag, _side, name, _reach in self._entries(self.TAGS)
            if tag not in snippets.get(name, "")
        )
        assert not gone, f"named in TAGS but no longer touches the tag: {gone}"

    def test_no_tag_is_used_outside_the_table(self):
        """A NEW `data-aish-*` tag has to arrive with its pairing, or the table
        rots and the guard quietly stops covering the code."""
        found = self._tags_in_source()
        assert found == set(self.TAGS), (
            f"tags in the source: {sorted(found)}; tags paired: "
            f"{sorted(self.TAGS)}"
        )

    def _tags_in_source(self, extra=""):
        import pathlib
        import re

        from aish import browser as browser_module

        text = extra
        for module in (browse, browser_module):
            text += pathlib.Path(module.__file__).read_text()
        return set(re.findall(r"data-aish-[a-z-]+", text))

    def test_the_guard_catches_a_reader_left_behind(self):
        """A guard that cannot fail is decoration. This plants the #292 bug —
        the writer made deep, the reader left on `document.querySelector` — and
        both halves of the check have to find it."""
        doctored = {
            "data-aish-signin": {
                "writes": {"SIGNIN_FORM_JS": DEEP},
                "reads": {"SIGNIN_STILL_OURS_JS": LIGHT},
            }
        }
        assert self._disagreements(doctored) == {
            "data-aish-signin": sorted([DEEP, LIGHT])
        }
        assert self._misdeclared(doctored, page_snippets())
        # …and the same lie told the other way round: a claim of DEEP over a
        # document-scoped lookup. Both patterns have to bite, or half the check
        # is decoration.
        overclaimed = {
            "data-aish-cell": {
                "writes": {"CALENDAR_JS": DEEP},
                "reads": {"CALENDAR_JS": DEEP},
            }
        }
        assert self._misdeclared(overclaimed, page_snippets())

    def test_the_guard_catches_an_unlisted_snippet(self, monkeypatch):
        monkeypatch.setattr(
            browse, "REGRESSION_PROBE_JS",
            "() => document.querySelector('[data-aish-n=\"1\"]')",
            raising=False,
        )
        with pytest.raises(AssertionError, match="the table does not say"):
            self.test_every_snippet_that_touches_a_tag_is_in_the_table()

    def test_the_guard_catches_a_new_tag(self):
        found = self._tags_in_source(extra="el.setAttribute('data-aish-row', '1')")
        assert found - set(self.TAGS) == {"data-aish-row"}


class TestTheFrameReferenceRidesTheResult:
    """#289 slice 1. The bytes live in the media store; the RESULT carries a
    reference to them, on the same envelope a page cut already rides. Bulk
    bytes never enter the log — the record only points at them, and they are
    purgeable on their own schedule."""

    def _opens(self, monkeypatch, snap):
        monkeypatch.setattr(web_module, "_require_public", lambda _u: None)
        monkeypatch.setattr(
            web_module.browser, "browse_open",
            lambda url, *, topic="", key="": snap,
        )

    def test_a_browse_hands_back_the_frame_of_the_page_it_showed(self, monkeypatch):
        view = web_module.BrowseView()
        self._opens(monkeypatch, snapshot(frame="/store/media/abc.jpg"))
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert out.meta["frame"] == "/store/media/abc.jpg"
        # A reference, never the bytes: nothing about the picture is in the
        # string the model reads or in the record that quotes it.
        assert "jpeg" not in out.lower()

    def test_the_reason_there_is_none_travels_too(self, monkeypatch):
        """Absence must never be the evidence: a page nobody pictured and a
        page nobody COULD picture route to different repairs."""
        view = web_module.BrowseView()
        self._opens(
            monkeypatch, snapshot(frame_skipped=browse.NO_FRAME_PASSWORD)
        )
        out = web_module.browse("https://eon.pl/login", view=view)
        assert out.meta["frame_skipped"] == browse.NO_FRAME_PASSWORD
        assert "frame" not in out.meta

    def test_a_call_that_reached_no_page_borrows_no_picture(self, monkeypatch):
        """The failure this guards. A refused or dead call presents nothing, so
        the view still holds the LAST page's frame — and attaching it would put
        a picture of one page on the record of another."""
        view = web_module.BrowseView()
        self._opens(monkeypatch, snapshot(frame="/store/media/abc.jpg"))
        first = web_module.browse("https://eon.pl/mojeon", view=view)
        assert first.meta["frame"] == "/store/media/abc.jpg"

        def refused(_url):
            raise web_module.BlockedURLError("not a public host")

        monkeypatch.setattr(web_module, "_require_public", refused)
        out = web_module.browse("http://192.168.1.1/", view=view)
        assert not hasattr(out, "meta")
        assert "ERROR" in out

    def test_an_act_reports_the_page_it_just_produced(self, monkeypatch):
        view = web_module.BrowseView()
        shown(snapshot(controls=[control(n=1)]))
        view.remember(snapshot(controls=[control(n=1)]))
        monkeypatch.setattr(
            web_module.browser, "browse_act",
            lambda *a, **kw: snapshot(
                url="https://eon.pl/faktury", frame="/store/media/after.jpg"
            ),
        )
        out = web_module.browse_act("Przełącz lokal", view=view)
        assert out.meta["frame"] == "/store/media/after.jpg"

    def test_a_cut_and_a_frame_ride_the_same_envelope(self, monkeypatch):
        """One envelope, both kinds of evidence — a second carrier would be a
        second thing for a reader to know about."""
        view = web_module.BrowseView()
        cut = web_module.PageCut()
        self._opens(
            monkeypatch,
            snapshot(text="x" * 200_000, frame="/store/media/abc.jpg"),
        )
        out = web_module.browse("https://eon.pl/mojeon", cut=cut, view=view)
        assert out.meta["frame"] == "/store/media/abc.jpg"
        assert out.meta["truncation"]

    def test_the_picture_carries_where_it_was_taken(self, monkeypatch):
        """A frame on its own answers "what did this page look like"; the
        question asked of a browse step is "what did this press do". The
        address and the move are already known at this seam — `shown` is the
        page before and the snapshot is the page after — so this is the delta
        written down, not a second one computed."""
        view = web_module.BrowseView()
        view.remember(snapshot(url="https://eon.pl/mojeon", controls=[control(n=1)]))
        monkeypatch.setattr(
            web_module.browser, "browse_act",
            lambda *a, **kw: snapshot(
                url="https://eon.pl/faktury", frame="/store/media/after.jpg"
            ),
        )
        out = web_module.browse_act("Przełącz lokal", view=view)
        assert out.meta["frame_url"] == "https://eon.pl/faktury"
        assert out.meta["frame_from"] == "https://eon.pl/mojeon"

    def test_a_press_that_did_not_move_the_page_says_no_move(self, monkeypatch):
        """"Navigated from the page it is still on" is a sentence with no
        content, and a caption saying it on every row is one the eye stops
        reading. The address is still recorded; the move is not invented."""
        view = web_module.BrowseView()
        view.remember(snapshot(url="https://eon.pl/mojeon", controls=[control(n=1)]))
        monkeypatch.setattr(
            web_module.browser, "browse_act",
            lambda *a, **kw: snapshot(
                url="https://eon.pl/mojeon", frame="/store/media/after.jpg"
            ),
        )
        out = web_module.browse_act("Przełącz lokal", view=view)
        assert out.meta["frame_url"] == "https://eon.pl/mojeon"
        assert "frame_from" not in out.meta

    def test_no_picture_means_no_caption_for_one(self, monkeypatch):
        """The address is written only ALONGSIDE a frame, because the claim it
        makes is about the picture. A lone address on a step with no picture
        would read as a caption for one."""
        view = web_module.BrowseView()
        self._opens(
            monkeypatch, snapshot(frame_skipped=browse.NO_FRAME_HANDS)
        )
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert "frame_url" not in out.meta and "frame_from" not in out.meta


class TestThePageSaysWhyItDidNotWork:
    """The console, captured per action.

    A day of failed diagnosis on eon.pl argued four causes off page text, a
    badge and a fetched copy of the HTML, and all four were wrong. The sentence
    that would have settled it — the login handler throwing — went to a console
    nobody recorded. It is not sign-in specific: the same silence is what makes
    a calendar that would not take a date unanswerable.

    A RECORD, and a record is detection and never protection: nothing is
    permitted, widened or checked less carefully because the console is now
    written down."""

    def test_errors_warnings_and_uncaught_exceptions_are_kept(self):
        log = browse.ConsoleLog()
        log.note("error", "ReferenceError: grecaptcha is not defined")
        log.note("warning", "third-party cookie blocked")
        log.note(browse.CONSOLE_UNCAUGHT, "TypeError: t.submit is not a function")
        assert log.drain() == [
            "error: ReferenceError: grecaptcha is not defined",
            "warning: third-party cookie blocked",
            "uncaught: TypeError: t.submit is not a function",
        ]

    def test_chatter_is_not_kept(self):
        """`console.log` is the page's own noise: a busy SPA writes thousands
        of lines a minute of it and none of them is a reason something did not
        happen."""
        log = browse.ConsoleLog()
        for level in ("log", "info", "debug", "table", "trace", ""):
            log.note(level, "rendering tile 4213")
        assert log.drain() == []

    def test_chromes_own_spelling_of_a_warning_is_understood(self):
        """`warn` and `warning` are one level spelled two ways depending on
        which end of the DevTools protocol you read it from. Dropping one of
        them would lose half the warnings for a naming reason."""
        log = browse.ConsoleLog()
        log.note("WARN", "deprecated API")
        assert log.drain() == ["warning: deprecated API"]

    def test_a_page_in_a_render_loop_cannot_fill_the_turn(self):
        """Bounded, and never silently: the count of what the cap refused is
        stated, the way MAX_CONTROLS states what it left out. A page able to
        quietly drop the line naming its own failure is the one page where
        this record would be worthless."""
        log = browse.ConsoleLog()
        for i in range(browse.CONSOLE_MAX_MESSAGES + 7):
            log.note("error", f"loop {i}")
        lines = log.drain()
        assert len(lines) == browse.CONSOLE_MAX_MESSAGES + 1
        assert lines[-1] == (
            f"[7 more console message(s) not kept — the cap is "
            f"{browse.CONSOLE_MAX_MESSAGES}]"
        )

    def test_one_enormous_message_is_cut_and_says_so(self):
        log = browse.ConsoleLog()
        log.note("error", "x" * 5000)
        (line,) = log.drain()
        assert line.endswith("…")
        assert len(line) == len("error: ") + browse.CONSOLE_MESSAGE_CHARS + 1

    def test_a_new_action_starts_from_empty(self):
        """The binding is the point: "something threw at some point today" is
        not evidence, "that click threw this" is. Carrying a page's noise into
        the next press would file it under the wrong action."""
        log = browse.ConsoleLog()
        log.note("error", "from the last press")
        log.begin()
        log.note("error", "from this one")
        assert log.drain() == ["error: from this one"]

    def test_draining_empties(self):
        """A snapshot is taken once per call; a second read must not report
        the same messages under the next action."""
        log = browse.ConsoleLog()
        log.note("error", "boom")
        assert log.drain() == ["error: boom"]
        assert log.drain() == []

    def test_a_message_that_is_only_whitespace_is_not_a_message(self):
        log = browse.ConsoleLog()
        log.note("error", "   \n\t ")
        log.note("error", "line one\n   line two")
        assert log.drain() == ["error: line one line two"]

    def test_the_vocabulary_is_closed(self):
        """Written into a trace record and read back by two renderers, so it is
        a closed vocabulary rather than free text — the same reasoning as
        NO_FRAME_*."""
        assert browse.CONSOLE_LEVELS == {
            browse.CONSOLE_ERROR, browse.CONSOLE_WARNING, browse.CONSOLE_UNCAUGHT
        }
        log = browse.ConsoleLog()
        log.note("catastrophe", "a level a later Chrome invented")
        assert log.drain() == []


class TestAControlSomethingIsCovering:
    """#321. A control can be listed, visible, enabled and reachable and still
    be unpressable because something is on top of it. That was computed —
    twice, by Playwright and by aish's own `_COVERED_JS` — and thrown away, so
    the only thing ever said about it was "something MAY be covering it", on
    every stuck control, right or wrong.

    Structural and therefore language- and site-independent: it needs no word
    list, which is what makes it the check `_CONSENT_SELECTORS` becomes a floor
    under (#295 P4)."""

    def test_nothing_over_a_control_is_the_ordinary_case_and_records_nothing(self):
        """Absent rather than empty, for the reason `signin` is: "nothing
        covered this" and "written before any of this existed" are different
        facts."""
        assert browse.Cover().record() == {}
        assert browse.Cover(by="", dismissed=True).record() == {}

    def test_the_element_is_named_rather_than_reduced_to_a_bool(self):
        cover = browse.Cover(by="clb clb-container")
        assert cover.record() == {"by": "clb clb-container", "dismissed": False}

    def test_dismissed_is_a_separate_fact_from_being_covered(self):
        """A consent button that was pressed and left the overlay in place has
        dismissed nothing for any purpose here."""
        cleared = browse.Cover(by="onetrust-banner-sdk", dismissed=True)
        assert cleared.record() == {"by": "onetrust-banner-sdk", "dismissed": True}

    def test_the_wall_says_what_can_be_pressed_ON_it(self):
        """Naming the obstruction and then saying only *"press whatever closes
        it"* hands back a fact and withholds the thing that acts on it — aish
        has walked the overlay and knows what is on it.

        Measured on lot.com: the button says `I Agree`, which no consent list
        here holds and no list reliably will. The page says it, in whatever
        language it is written in, if anyone asks."""
        cover = browse.Cover(
            by="onetrust-pc-dark-filter ot-fade-in",
            controls=["Manage", "I Agree"],
        )
        said = browse.stuck_reason(cover, action="press", address="Wylot")
        assert "'I Agree'" in said and "'Manage'" in said
        assert "What can be pressed ON IT" in said
        assert "press whatever closes it" not in said.lower()

    def test_a_nameless_wall_still_gets_the_old_ending(self):
        """An overlay with no pressable control of its own — a scroll lock, a
        transparent shim — is a different fact from one aish could enumerate,
        and folding them together is what #321 is about."""
        said = browse.stuck_reason(
            browse.Cover(by="scroll-lock"), action="press", address="Szukaj"
        )
        assert "'scroll-lock'" in said
        assert "Press whatever closes it" in said

    def test_nothing_covering_it_is_a_third_ending_not_a_weaker_second(self):
        said = browse.stuck_reason(browse.Cover(), action="press", address="Szukaj")
        assert "Nothing was found covering it" in said

    def test_the_walls_controls_reach_the_trace_bounded(self):
        """They cross into a trace record, so they are bounded where they are
        BUILT — one bound in one place is one thing to be wrong."""
        many = [f"button {i}" for i in range(20)]
        cover = browse.Cover(by="wall", controls=many)
        assert len(cover.record()["controls"]) == browse.COVER_CONTROLS_MAX
        assert f"+{20 - browse.COVER_CONTROLS_MAX} more" in cover.named()
        assert browse.Cover(by="wall").record() == {"by": "wall", "dismissed": False}

    def test_the_name_is_collapsed_and_bounded(self):
        """It crosses into a trace record, so it is bounded HERE — one bound in
        one place is one thing to be wrong."""
        assert browse.covering_name("  clb\n  clb-container ") == "clb clb-container"
        assert len(browse.covering_name("x" * 500)) == browse.COVERED_NAME_CHARS
        assert browse.covering_name("") == ""
        assert browse.covering_name(None) == ""

    def test_the_refusal_names_the_element(self):
        """A refusal that names nothing teaches nothing — which is exactly what
        a day of four wrong diagnoses was argued on top of."""
        said = browse.COVERED_STUCK.format(
            action="click", address="Zaloguj", by="clb clb-container"
        )
        assert "'clb clb-container'" in said
        assert "'Zaloguj'" in said
        # And it says the thing the model cannot work out for itself: the click
        # went somewhere else, so it never reached the control at all.
        assert "never reaches the control" in said

    def test_the_uncovered_refusal_claims_no_cover(self):
        """The narrow half, and the one that keeps the whole thing honest. A
        press that LANDS on the right element and is then ignored produces no
        interception at all, and nothing may imply it was covered."""
        said = browse.STUCK_NOT_COVERED.format(action="click", address="Wyslij")
        assert "Nothing was found covering it" in said
        assert "may be inert" in said

    def test_the_dismissed_note_names_it_too(self):
        said = browse.COVERED_DISMISSED.format(by="cookie-bar")
        assert "'cookie-bar'" in said

    def test_a_snapshot_carries_it_to_the_trace(self):
        """A structural signal only the acting model sees is one restart from
        being lost, which is exactly how this fact reached nobody."""
        snap = snapshot(covered=browse.Cover(by="clb", dismissed=False))
        assert snap.covered.record() == {"by": "clb", "dismissed": False}
        assert snapshot().covered.record() == {}


class TestTheConsentListIsAFloorWithACounterOverIt:
    """#295 P4: where a vocabulary is unavoidable it is a floor under a
    structural check and it SHIPS WITH COUNTERS. `_CONSENT_SELECTORS` had
    neither. It holds `Akceptuj wszystkie`; eon.pl's button says `Akceptuje
    wszystkie cookies`, and `has-text` is a substring match — so it missed by
    one letter, and the only symptom was a site that quietly stopped
    working."""

    def test_a_list_nobody_has_asked_says_nothing(self):
        """Silent until used, for the reason an empty console grows no
        heading."""
        assert browse.ConsentTally().line() == ""

    def test_a_match_and_a_miss_are_both_counted(self):
        tally = browse.ConsentTally()
        tally.note(dismissed=True)
        tally.note(dismissed=False)
        tally.note(dismissed=False)
        assert (tally.asked, tally.dismissed, tally.missed) == (3, 1, 2)

    def test_a_list_that_has_stopped_matching_is_a_NUMBER_and_not_silence(self):
        """The whole point. A miss neither permits something nor costs friction
        — it silently breaks a feature, which is the failure shape nobody looks
        for (#322)."""
        tally = browse.ConsentTally()
        for _ in range(9):
            tally.note(dismissed=False)
        said = tally.line()
        assert "cleared 0 of 9" in said
        assert "9 it could not match" in said

    def test_a_list_that_matches_everything_says_so_without_a_miss_clause(self):
        tally = browse.ConsentTally()
        tally.note(dismissed=True)
        assert tally.line() == (
            "consent list:  cleared 1 of 1 covered control(s) this session"
        )

    def test_it_counts_OBSTRUCTIONS_and_never_pages(self):
        """What makes the number mean anything. `_dismiss_consent` also runs
        speculatively on every page that opens, where no banner is the common
        case — counting there would put the miss rate at ~100% forever. So the
        one writer is the structural check that has ALREADY found something
        covering a control."""
        source = pathlib.Path(browser.__file__).read_text()
        writers = [
            line.strip() for line in source.splitlines()
            if "CONSENT_TALLY.note" in line
        ]
        assert writers == ["browse_mod.CONSENT_TALLY.note(dismissed=cleared)"]
        # ...and that line lives in `_uncover`, after the cover was found.
        body = source.split("async def _uncover(")[1].split("\nasync def ")[0]
        assert "CONSENT_TALLY.note" in body

    def test_the_tally_decides_nothing(self):
        """An instrument for noticing, never an input. A record is detection
        and never protection (#295 P2), and a counter even less so — so nothing
        in this module reads it back."""
        source = pathlib.Path(browse.__file__).read_text()
        readers = [line for line in source.splitlines() if "CONSENT_TALLY" in line]
        assert readers == ["CONSENT_TALLY = ConsentTally()"]


class TestTheConsoleReachesTheModelAsPageContent:
    """Console text is PAGE-AUTHORED and gets the treatment this codebase
    already has for that — inside the untrusted banner, never in aish's voice,
    and never above the banner where the provenance notes live."""

    def _opens(self, monkeypatch, snap):
        monkeypatch.setattr(web_module, "_require_public", lambda _u: None)
        monkeypatch.setattr(
            web_module.browser, "browse_open",
            lambda url, *, topic="", key="": snap,
        )

    def test_a_healthy_action_costs_nothing(self):
        """Empty is the ordinary case: no section, no heading, no blank line.
        A sentence on every result saying the page was fine is the noise that
        hides the one result where it was not."""
        assert web_module.console_note(snapshot()) == ""
        assert web_module.console_note(snapshot(console=[])) == ""

    def test_the_lines_arrive_below_the_untrusted_banner(self, monkeypatch):
        view = web_module.BrowseView()
        self._opens(monkeypatch, snapshot(
            problem="could not click 'Zaloguj': the control is inert",
            console=["uncaught: ReferenceError: grecaptcha is not defined"],
        ))
        out = str(web_module.browse("https://eon.pl/login", view=view))
        assert "grecaptcha is not defined" in out
        assert out.index(web_module.UNTRUSTED_NOTE) < out.index("grecaptcha")

    def test_the_heading_says_whose_words_these_are(self, monkeypatch):
        """A page that can write a sentence into a warning has written it into
        the document. The label is what stops a console line reading as aish's
        own account of what went wrong."""
        view = web_module.BrowseView()
        self._opens(monkeypatch, snapshot(
            problem="could not click 'Zaloguj'", console=["error: boom"]
        ))
        out = str(web_module.browse("https://eon.pl/login", view=view))
        assert "BY THE PAGE" in out
        # …and it is never in the aish-voice region above the banner, which is
        # where a provenance note goes and where page text may never appear.
        above = out.split(web_module.UNTRUSTED_NOTE)[0]
        assert "boom" not in above

    def test_a_noisy_but_healthy_page_keeps_its_console_off_the_model(
        self, monkeypatch
    ):
        """The number of sites that log errors on every healthy page is
        enormous. A clean open that handed the model those lines anchored it
        on failures nothing observed — the same over-anchoring the owner
        named on the trace, aimed at the thing deciding what to do next. The
        criterion is aish's OWN observation of something going wrong, never
        the page's noisiness; the record is untouched (next test)."""
        view = web_module.BrowseView()
        self._opens(monkeypatch, snapshot(console=["error: boom"]))
        out = str(web_module.browse("https://eon.pl/mojeon", view=view))
        assert "boom" not in out

    def test_an_action_that_visibly_worked_reports_no_console_either(
        self, monkeypatch
    ):
        """A non-empty delta is the action visibly doing something. The empty
        delta is the one case where the console is the other half of the
        answer — everywhere else it is the site's everyday noise."""
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        monkeypatch.setattr(
            web_module.browser, "browse_act",
            lambda *a, **kw: snapshot(
                controls=[control(n=1), control(n=2, name="Nowy")],
                console=["error: boom"],
            ),
        )
        outcome = web_module.browse_act("Przełącz lokal", view=view)
        assert "boom" not in str(outcome)
        # …but the owner's copy still rides the envelope, unconditionally.
        assert outcome.meta["console"] == ["error: boom"]

    def test_a_change_report_carries_it_too(self, monkeypatch):
        """A press that changed nothing and a handler that threw are ONE
        answer: the delta alone says the click did nothing and cannot say
        why."""
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        monkeypatch.setattr(
            web_module.browser, "browse_act",
            lambda *a, **kw: snapshot(
                controls=[control(n=1)],
                console=["uncaught: TypeError: t.submit is not a function"],
            ),
        )
        out = str(web_module.browse_act("Przełącz lokal", view=view))
        assert "nothing on the page changed" in out
        assert "t.submit is not a function" in out

    def test_it_rides_the_envelope_to_the_trace(self, monkeypatch):
        """The owner's copy. He reads the trace, and reading a failure through
        somebody else is the thing this work is about."""
        view = web_module.BrowseView()
        self._opens(monkeypatch, snapshot(console=["error: boom"]))
        out = web_module.browse("https://eon.pl/login", view=view)
        assert out.meta["console"] == ["error: boom"]

    def test_a_clean_page_writes_no_key_at_all(self, monkeypatch):
        view = web_module.BrowseView()
        self._opens(monkeypatch, snapshot(frame="/store/media/abc.jpg"))
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert "console" not in out.meta


class TestAishsOwnAnomalyObservationsReachTheTrace:
    """The criterion for surfacing a step's console is aish's OWN observation
    that the step did not do what it looked like it should — and two of those
    observations were computed and then thrown away. `Snapshot.problem` (aish
    could not carry the action out as asked) and an action's EMPTY delta (the
    "did that click work" fact) reached the model's turn and nothing else, so
    a renderer had nothing to key on: a browse whose action failed still
    returns a page and sniffs ok. Both now ride the envelope, as observations
    and never verdicts."""

    def _acts(self, monkeypatch, snap):
        monkeypatch.setattr(
            web_module.browser, "browse_act", lambda *a, **kw: snap
        )

    def test_a_failed_action_carries_aishs_sentence_on_the_step(
        self, monkeypatch
    ):
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        said = browse.STUCK_NOT_COVERED.format(action="click", address="Zaloguj")
        self._acts(monkeypatch, snapshot(controls=[control(n=1)], problem=said))
        out = web_module.browse_act("Zaloguj", view=view)
        assert out.meta["problem"] == said

    def test_an_action_whose_delta_came_back_empty_records_the_fact(
        self, monkeypatch
    ):
        """The press that landed and was ignored: no problem, no cover, no
        error — the empty delta is the ONLY witness on the record, and it is
        exactly the step whose console the eon.pl day needed."""
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        self._acts(monkeypatch, snapshot(controls=[control(n=1)]))
        out = web_module.browse_act("Przełącz lokal", view=view)
        assert out.meta["unchanged"] is True

    def test_an_action_that_visibly_worked_writes_neither_key(
        self, monkeypatch
    ):
        """Absent in the ordinary case (corollary 2): a step saying "this went
        fine" on every row is the noise that hides the one that did not."""
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        self._acts(monkeypatch, snapshot(
            controls=[control(n=1), control(n=2, name="Nowy")]
        ))
        out = web_module.browse_act("Przełącz lokal", view=view)
        meta = getattr(out, "meta", {}) or {}
        assert "problem" not in meta
        assert "unchanged" not in meta

    def test_a_clean_open_writes_neither_key(self, monkeypatch):
        monkeypatch.setattr(web_module, "_require_public", lambda _u: None)
        monkeypatch.setattr(
            web_module.browser, "browse_open",
            lambda url, *, topic="", key="": snapshot(url=url),
        )
        view = web_module.BrowseView()
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        meta = getattr(out, "meta", {}) or {}
        assert "problem" not in meta
        assert "unchanged" not in meta

    def test_a_full_page_report_observes_nothing_about_change(
        self, monkeypatch
    ):
        """A first open has no "last shown" to diff against, so it must not
        write `unchanged` — no delta was computed, and the key would claim an
        observation nobody made (corollary 2)."""
        view = web_module.BrowseView()
        view.remember(snapshot(url="https://eon.pl/inne"))
        # Different URL: the delta path is not taken, the page comes whole.
        self._acts(monkeypatch, snapshot(url="https://eon.pl/mojeon"))
        out = web_module.browse_act("Przełącz lokal", view=view)
        meta = getattr(out, "meta", {}) or {}
        assert "unchanged" not in meta


class TestWhatCoveredAControlReachesTheTrace:
    """#321. The interception fact must reach the OWNER's record and not only
    the acting model's turn.

    Every one of the four wrong diagnoses of the eon.pl sign-in was argued in a
    session where Chrome knew the click was intercepted and named the element:
    the fact existed, was computed, and reached nobody who could read it
    afterwards. A structural signal only the acting model sees is one restart
    from being lost. Same pattern as the console record, on its own key because
    it is a different kind of fact — aish's own observation about its own
    hands, not the page's account of itself."""

    def _acts(self, monkeypatch, snap):
        monkeypatch.setattr(
            web_module.browser, "browse_act", lambda *a, **kw: snap
        )

    def test_a_press_that_could_not_land_names_the_element_on_the_step(
        self, monkeypatch
    ):
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        self._acts(monkeypatch, snapshot(
            controls=[control(n=1)],
            problem=browse.COVERED_STUCK.format(
                action="click", address="Zaloguj", by="clb clb-container"
            ),
            covered=browse.Cover(by="clb clb-container"),
        ))
        out = web_module.browse_act("Zaloguj", view=view)
        assert out.meta["covered"] == {
            "by": "clb clb-container", "dismissed": False
        }
        # ...and the model is told too, in aish's own voice above the banner:
        # this is aish's observation, not the site's words.
        above = str(out).split(web_module.UNTRUSTED_NOTE)[0]
        assert "clb clb-container" in above

    def test_a_dismissed_cover_is_recorded_as_dismissed(self, monkeypatch):
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        self._acts(monkeypatch, snapshot(
            controls=[control(n=1)],
            notice=browse.COVERED_DISMISSED.format(by="cookie-bar"),
            covered=browse.Cover(by="cookie-bar", dismissed=True),
        ))
        out = web_module.browse_act("Zaloguj", view=view)
        assert out.meta["covered"] == {"by": "cookie-bar", "dismissed": True}

    def test_an_ordinary_press_is_completely_unaffected(self, monkeypatch):
        """A page with nothing over it grows no key, no sentence and no cost.
        The check runs only on the rung where a real click already failed."""
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        self._acts(monkeypatch, snapshot(controls=[control(n=1)]))
        out = web_module.browse_act("Przelacz lokal", view=view)
        assert not hasattr(out, "meta") or "covered" not in out.meta

    def test_a_new_call_never_inherits_the_last_calls_obstruction(
        self, monkeypatch
    ):
        """Cleared at the top of a call for the reason the frame is: a call
        that pressed nothing must not borrow the page before it."""
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        self._acts(monkeypatch, snapshot(
            controls=[control(n=1)], covered=browse.Cover(by="clb")
        ))
        assert web_module.browse_act("Zaloguj", view=view).meta["covered"]
        self._acts(monkeypatch, snapshot(controls=[control(n=1)], text="moved"))
        out = web_module.browse_act("Zaloguj", view=view)
        assert not hasattr(out, "meta") or "covered" not in out.meta

    def test_a_covered_sign_in_is_told_as_unsubmitted_never_as_refused(self, monkeypatch):
        """The note has to be the one that says the saved sign-in is untouched
        and does not need replacing — never the stale note, which says the
        site did not accept it, and never the held note's "aish did not use
        it": the form WAS filled, so that would overclaim in the other
        direction. Nothing here judged the password, because nothing was ever
        submitted to the site."""
        outcome = TestTheSignInAttemptIsOnTheStepItHappenedUnder._Outcome()
        outcome.captcha = ""
        outcome.covered = "clb clb-container"
        outcome.why = browser.COVERED_SUBMIT.format(by="clb clb-container")
        view = web_module.BrowseView()
        monkeypatch.setattr(web_module, "_require_public", lambda _u: None)

        def browse_open(url, *, topic="", key=""):
            snap = snapshot(url=url, text="Zaloguj", controls=[])
            snap.signin = True
            return snap

        monkeypatch.setattr(web_module.browser, "browse_open", browse_open)
        monkeypatch.setattr(web_module.browser, "sign_in", lambda _u: outcome)
        out = str(web_module.browse("https://eon.pl/mojeon", view=view))
        assert "could not confirm the form was ever submitted" in out
        assert "clb clb-container" in out
        # Never the stale note: nothing said the site refused the value.
        assert "was not accepted" not in out
        assert "that also replaces the saved sign-in" not in out

    def test_a_press_that_landed_and_was_ignored_records_no_cover(
        self, monkeypatch
    ):
        """The boundary, pinned rather than described. A handler that is
        missing, broken or returns early produces no interception AND no
        console line — the press reached the right element. Nothing here may
        report that as covered, and the trace must stay silent about it."""
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        self._acts(monkeypatch, snapshot(
            controls=[control(n=1)],
            notice="the click would not land, so aish pressed it with the keyboard",
        ))
        out = web_module.browse_act("Zaloguj", view=view)
        assert not hasattr(out, "meta") or "covered" not in out.meta


class TestTheSignInAttemptIsOnTheStepItHappenedUnder:
    """#320 photographs every sign-in attempt onto `Record.last_frame`, and
    nothing rendered it — so the owner went looking for the picture, could not
    find it, and went on reading the failure through somebody else.

    It rides the call the sign-in happened INSIDE, under a key of its own. Not
    the step's `frame`: that key claims "the page at the moment the model was
    SHOWN it", and the model is never shown the sign-in page."""

    class _Outcome:
        ok = False
        # Shaped like the real never-sent ending: an observation, with the
        # declaration riding along as a page fact — never a stated cause.
        why = "aish filled the form and never saw the password leave the page"
        captcha = "reCAPTCHA"
        second_factor = False
        stale = False
        tried = False
        filled = True
        frame = "/store/frames/login.jpg"
        frame_skipped = ""
        console = ["uncaught: ReferenceError: grecaptcha is not defined"]
        covered = ""
        # No judgement by default: this stub is the shape of an outcome from
        # before #325, which must degrade to an attempt with no verdict on it.
        verdict = ""
        observed = None

    def _driven(self, monkeypatch, outcome):
        monkeypatch.setattr(web_module, "_require_public", lambda _u: None)

        def browse_open(url, *, topic="", key=""):
            snap = snapshot(url=url, text="Zaloguj", controls=[])
            snap.signin = True
            return snap

        monkeypatch.setattr(web_module.browser, "browse_open", browse_open)
        monkeypatch.setattr(web_module.browser, "sign_in", lambda _u: outcome)

    def test_the_picture_of_the_attempt_reaches_the_step(self, monkeypatch):
        view = web_module.BrowseView()
        self._driven(monkeypatch, self._Outcome())
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert out.meta["signin"]["host"] == "eon.pl"
        assert out.meta["signin"]["frame"] == "/store/frames/login.jpg"

    def test_the_outcome_travels_with_the_attempt(self, monkeypatch):
        """`SignInResult.ok` — the session seen to come up, read afresh, and
        nothing weaker — reaches the record. Without it every attempt rendered
        identically, and the owner's first automatic sign-in that worked end
        to end was painted with the same weight as a failure. Written true or
        false for every attempt, unlike the evidence keys beside it: leaving
        false absent would make a failed attempt in a new log unreadable from
        an attempt in a log written before the key existed."""
        view = web_module.BrowseView()
        self._driven(monkeypatch, self._Outcome())
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert out.meta["signin"]["ok"] is False

    def test_a_session_seen_to_come_up_is_recorded_as_such(self, monkeypatch):
        outcome = self._Outcome()
        outcome.ok = True
        view = web_module.BrowseView()
        self._driven(monkeypatch, outcome)
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert out.meta["signin"]["ok"] is True

    def test_it_is_kept_apart_from_the_page_the_model_was_shown(self, monkeypatch):
        """Two pictures of two different documents. Folding one into the other
        would state a guarantee wider than either capture enforces."""
        view = web_module.BrowseView()
        self._driven(monkeypatch, self._Outcome())
        monkeypatch.setattr(
            web_module.browser, "browse_open",
            lambda url, *, topic="", key="": snapshot(
                url=url, frame="/store/frames/page.jpg"
            ),
        )
        # A page that is NOT asking for a password: no sign-in, page frame only.
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert out.meta["frame"] == "/store/frames/page.jpg"
        assert "signin" not in out.meta

    def test_the_login_pages_console_travels_with_it(self, monkeypatch):
        """The evidence the whole eon.pl day did not have. It reaches the
        OWNER and never the renewal note — that note is aish's own voice above
        the untrusted banner, where page-authored text may not be spoken."""
        view = web_module.BrowseView()
        self._driven(monkeypatch, self._Outcome())
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert out.meta["signin"]["console"] == [
            "uncaught: ReferenceError: grecaptcha is not defined"
        ]
        above = str(out).split(web_module.UNTRUSTED_NOTE)[0]
        assert "grecaptcha" not in above

    def test_what_covered_the_sign_in_button_travels_with_it(self, monkeypatch):
        """#321. The sign-in path never asked what was in the way, so a banner
        over eon.pl's login button read as a replay that did nothing. Now it is
        on the step the owner is already looking at, beside the picture."""
        outcome = self._Outcome()
        outcome.covered = "clb clb-container"
        view = web_module.BrowseView()
        self._driven(monkeypatch, outcome)
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert out.meta["signin"]["covered"] == "clb clb-container"

    def test_a_sign_in_with_nothing_in_the_way_writes_no_covered_key(
        self, monkeypatch
    ):
        view = web_module.BrowseView()
        self._driven(monkeypatch, self._Outcome())
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert "covered" not in out.meta["signin"]

    def test_the_absence_says_which_absence(self, monkeypatch):
        outcome = self._Outcome()
        outcome.frame = ""
        outcome.frame_skipped = browse.NO_FRAME_HANDS
        view = web_module.BrowseView()
        self._driven(monkeypatch, outcome)
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert out.meta["signin"]["frame_skipped"] == browse.NO_FRAME_HANDS
        assert "frame" not in out.meta["signin"]

    def test_a_call_with_no_sign_in_writes_no_block(self, monkeypatch):
        """`host` is what says an attempt happened. An empty block would read
        as an attempt with nothing to show, which is the third state the trace
        contract forbids collapsing."""
        view = web_module.BrowseView()
        monkeypatch.setattr(web_module, "_require_public", lambda _u: None)
        monkeypatch.setattr(
            web_module.browser, "browse_open",
            lambda url, *, topic="", key="": snapshot(url=url),
        )
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        assert not hasattr(out, "meta") or "signin" not in out.meta

    def test_an_act_never_claims_a_sign_in(self, monkeypatch):
        """Renewal happens on the OPEN and never on an act, so an act carrying
        a sign-in block would be claiming something that cannot have happened
        inside it."""
        view = web_module.BrowseView()
        view.remember(snapshot(controls=[control(n=1)]))
        monkeypatch.setattr(
            web_module.browser, "browse_act",
            lambda *a, **kw: snapshot(frame="/store/frames/after.jpg"),
        )
        out = web_module.browse_act("Przełącz lokal", view=view)
        assert "signin" not in out.meta

    def test_the_recorder_is_per_call_and_not_module_state(self):
        """`read_url` runs on the parallel read path with several calls in
        flight; a recorder on the module would file one read's sign-in under
        another read's result. The same reasoning that shapes PageCut."""
        one, two = web_module.SignInSeen(), web_module.SignInSeen()
        one.note("eon.pl", self._Outcome())
        assert two.record() == {}
        assert one.record()["host"] == "eon.pl"

    def test_an_outcome_with_no_evidence_still_says_an_attempt_happened(self):
        """A sign-in that produced no picture and no console is still a
        credential SPENT on this step, and that is worth a row of its own."""
        seen = web_module.SignInSeen()
        seen.note("eon.pl", object())
        # `ok` rides along even here, as False: a stubbed or older outcome
        # object carries no `ok`, and "not seen to come up" is exactly what a
        # session nobody observed coming up is.
        assert seen.record() == {"host": "eon.pl", "ok": False}

    def _judged(self, **kw):
        from aish import browser

        outcome = self._Outcome()
        outcome.verdict = kw.pop("verdict", "never_sent")
        outcome.observed = browser.SignInObserved(**kw)
        return outcome

    def test_what_was_SEEN_of_a_failure_reaches_the_step(self, monkeypatch):
        """#325. The four observations were composed into a token and thrown
        away; only the owner-facing SENTENCE survived, as prose in the result
        and as prose in `signins.json`. Nobody could check afterwards what
        aish had actually seen — the exact hole that let a cause nothing
        observed stand for weeks."""
        view = web_module.BrowseView()
        self._driven(monkeypatch, self._judged(
            declared_widget="reCAPTCHA", body_to_own_origin=True,
        ))
        block = web_module.browse("https://eon.pl/mojeon", view=view).meta["signin"]
        assert block["verdict"] == "never_sent"
        assert block["observed"] == {
            "credential_seen_leaving": False,
            "refusal_status": False,
            "page_said_no": False,
            "declared_widget": "reCAPTCHA",
            "body_to_own_origin": True,
        }

    def test_every_observation_is_written_even_when_it_is_false(self, monkeypatch):
        """The group's PRESENCE is the discriminator, which is what lets the
        keys inside it be unconditional: a `false` here is a thing aish looked
        for and did not see, and it must not be confusable with a key nobody
        wrote (corollary 2). Two of these route to opposite repairs — chase
        the gesture, or chase the matcher."""
        view = web_module.BrowseView()
        self._driven(monkeypatch, self._judged(verdict="unexplained",
                                               credential_seen_leaving=True))
        block = web_module.browse("https://eon.pl/mojeon", view=view).meta["signin"]
        assert set(block["observed"]) == {
            "credential_seen_leaving", "refusal_status", "page_said_no",
            "declared_widget", "body_to_own_origin",
        }
        assert block["observed"]["refusal_status"] is False
        assert block["observed"]["declared_widget"] == ""

    def test_a_token_never_travels_without_the_observations_under_it(
        self, monkeypatch
    ):
        """Trace contract §4: an evidence record holds the inputs the verdict
        was a function of, never a rendering of the verdict. A stubbed or
        older outcome carrying a token and nothing else degrades to no
        judgement at all rather than to a bare conclusion."""
        outcome = self._Outcome()
        outcome.verdict = "refused"
        view = web_module.BrowseView()
        self._driven(monkeypatch, outcome)
        block = web_module.browse("https://eon.pl/mojeon", view=view).meta["signin"]
        assert "verdict" not in block and "observed" not in block

    def test_an_attempt_that_never_reached_the_table_writes_neither_key(
        self, monkeypatch
    ):
        """A session that came up and a site asking for a code are not
        failures anything judged. A verdict key on either would assert an
        outcome no observation produced — this record exists to end that, not
        to move it somewhere new."""
        for name, field in (("ok", True), ("second_factor", True)):
            outcome = self._Outcome()
            setattr(outcome, name, field)
            view = web_module.BrowseView()
            self._driven(monkeypatch, outcome)
            block = web_module.browse(
                "https://eon.pl/mojeon", view=view
            ).meta["signin"]
            assert block["host"] == "eon.pl"
            assert "verdict" not in block and "observed" not in block

    def test_the_record_cannot_carry_the_credential(self, monkeypatch):
        """The standing constraint. Four booleans and aish's own brand name
        for a token it matched — no page span, no value, no length of one."""
        view = web_module.BrowseView()
        self._driven(monkeypatch, self._judged(
            verdict="refused", credential_seen_leaving=True, refusal_status=True,
            page_said_no=True, declared_widget="reCAPTCHA",
        ))
        out = web_module.browse("https://eon.pl/mojeon", view=view)
        block = out.meta["signin"]
        assert [type(v) for v in block["observed"].values()].count(bool) == 4
        assert isinstance(block["observed"]["declared_widget"], str)


class TestWhenAishMaySayADialogIsOpen:
    """#348. The sentence an open dialog licenses — *close it to reach them* —
    is spoken in aish's OWN voice above the untrusted banner, replacing one that
    was wrong on lot.com. So the bar for saying it has to be higher than the bar
    for the sentence it replaces, or the fix puts a new false claim on more
    pages than the old one was wrong on.

    Two conditions, and this pins that NEITHER alone is enough. The JS itself
    runs only in a real Chrome; what is checkable here is that the source states
    both conditions and that the presenter is driven by the FIELD, so a future
    edit loosening either one has to walk past a test that says why."""

    def test_a_bare_role_dialog_is_not_a_modality_declaration(self):
        """Half the web keeps an inert `[role=dialog]` in the DOM, and plenty
        use the role for a cookie strip that covers nothing."""
        js = browse.CONTROLS_JS
        assert "dialog:modal" in js
        assert '[aria-modal="true"]' in js
        # The loose rung is deliberately absent from the dialog probe.
        probe = js[js.index("const openDialog"):]
        assert "[role=dialog]" not in probe
        assert "[class*=modal]" not in probe

    def test_a_declared_dialog_on_a_page_that_still_scrolls_claims_nothing(self):
        """`rootLocked` is the mechanism that actually puts the rest of the
        document out of reach — which is what `unreachable` is counting when
        this fires. Without it, the dialog is not what is holding anything
        away."""
        probe = browse.CONTROLS_JS[browse.CONTROLS_JS.index("const openDialog"):]
        assert "rootLocked" in probe

    def test_the_presenter_says_nothing_unless_the_field_is_set(self):
        """The half that IS executable: no dialog observed, no new sentence."""
        out = web_module._present_snapshot(snapshot(unreachable=7, dialog=""))
        assert "Press whatever opens them first" in out
        assert "Close it to reach them" not in out
