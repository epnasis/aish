#!/usr/bin/env -S uv run python
"""Drive a real Chrome through fake portals shaped like the ones that failed.

Everything in tests/test_browse.py patches the browser away, which leaves one
thing unverified and it is the load-bearing one: whether `CONTROLS_JS` finds, on
a REAL page, the control the model needs — and whether pressing it works. So
this serves two local pages and drives them.

`PORTAL` is shaped like eon.pl/mojeon: a `<a href="#">Przełącz lokal</a>` that
opens a menu, a table that swaps when a property is chosen, a search field, and
a panel that says "Wczytywanie danych" for a second before its content arrives.

`HARD` is every way a control can be listed and unpressable, each one taken from
a real session this week (#244, #245, #246): a responsive nav rendered twice with
the mobile copy parked off-canvas, a collapsed accordion, a dialog that pins the
page, a native checkbox hidden under a styled label, a 250-option dropdown, a
button under a transparent sheet, a control inside an iframe, and a download link
that opens in a tab Chrome closes the moment the transfer starts.

Run it directly; it is NOT part of the pytest suite (it launches Chrome, which
conftest forbids on purpose). A throwaway profile in a temp state dir: this must
never touch the owner's real profile or any real account.

    uv run python scripts/verify_browse.py
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["AISH_STATE_DIR"] = tempfile.mkdtemp(prefix="verify-browse-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aish import browse, browser, web  # noqa: E402

PORTAL = """<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><title>Mój E.ON (atrapa)</title></head>
<body>
  <nav><a href="/">Pulpit</a> <a href="/faktury">Faktury i płatności</a></nav>
  <header>
    <span id="lokal">Wyspowa</span>
    <a href="#" id="switch">Przełącz lokal</a>
    <a href="#" id="pay">Zapłać</a>
  </header>
  <ul id="menu" hidden>
    <li><button id="b1">Bluszczanska</button></li>
    <li><button id="b2">Ananasowa</button></li>
  </ul>
  <form>
    <input type="search" name="q" placeholder="Szukaj faktury">
    <button type="submit">Filtruj</button>
  </form>
  <p><a id="faktura" href="/faktura.pdf" download="faktura 09-2026.pdf">Pobierz e-fakturę</a></p>
  <section id="panel">Wczytywanie danych</section>
  <input type="hidden" name="csrf" value="secret">
  <script>
    setTimeout(() => {
      document.getElementById('panel').textContent = 'Faktura 09/2026 — 226,89 zl';
    }, 1200);
    document.getElementById('switch').onclick = (e) => {
      e.preventDefault();
      document.getElementById('menu').hidden = false;
    };
    for (const id of ['b1', 'b2']) {
      document.getElementById(id).onclick = () => {
        const name = document.getElementById(id).textContent;
        document.getElementById('lokal').textContent = name;
        document.getElementById('menu').hidden = true;
        document.getElementById('panel').textContent =
          'Faktura 09/2026 dla ' + name + ' — 118,40 zl';
      };
    }
  </script>
</body></html>
"""

HARD = """<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><title>Trudna strona (atrapa)</title>
<style>
  /* The responsive duplicate: the nav is rendered twice and the mobile copy is
     parked off-canvas. BOTH copies pass "has a box, is not display:none". */
  #drawer { position: fixed; top: 0; left: 0; width: 280px; height: 100%;
            transform: translateX(-100%); background: #eee; }
  /* The collapsed accordion: overflow hidden and nothing to scroll. */
  #folded { height: 0; overflow: hidden; }
  /* The modal that pins the page — which is what puts everything below the
     fold permanently out of reach, however hard Playwright scrolls. */
  body.locked { position: fixed; width: 100%; }
  #dialog { position: fixed; top: 10px; left: 10px; background: #fff; }
  #tall { height: 3000px; }
  /* The styled checkbox: the native input is invisible and the label IS the
     control. Every custom toggle on the web is shaped like this. */
  #agree { opacity: 0; position: absolute; width: 1px; height: 1px; }
  #wrap { position: relative; display: inline-block; }
  #sheet { position: absolute; inset: 0; background: transparent; }
  /* The account switcher, shaped like E.ON's: a scrollable list inside a FIXED
     header, showing four of its five entries. */
  #header { position: fixed; top: 0; right: 0; background: #ddd; }
  #lokale { max-height: 80px; overflow-y: auto; }
  #lokale a { display: block; height: 20px; }
</style></head>
<body>
  <div id="header"><div id="lokale">
    <a href="/hard.html?ku=1">Wyspowa</a>
    <a href="/hard.html?ku=2">Bluszczanska</a>
    <a href="/hard.html?ku=3">Ananasowa</a>
    <a href="/hard.html?ku=4">Garaż Bluszczańska</a>
    <a href="/hard.html?ku=5">Marii Cetysówny 2 m. 13</a>
  </div></div>
  <nav id="bar"><a href="/hard.html?from=bar" id="nav-desktop">Faktury i płatności</a></nav>
  <nav id="drawer"><a href="/hard.html?from=drawer" id="nav-mobile">Faktury i płatności</a></nav>

  <div id="folded"><button id="in-accordion">Szczegóły rozliczenia</button></div>
  <button id="unfold">Pokaż szczegóły</button>

  <label id="agree-label" for="agree">Akceptuję regulamin</label>
  <input type="checkbox" id="agree">

  <select id="kraj" name="kraj">OPTIONS</select>

  <p><a id="pobierz" href="/faktura.pdf" target="_blank">Pobierz e-fakturę</a></p>

  <div id="wrap"><button id="covered">Wyślij zgłoszenie</button><div id="sheet"></div></div>

  <div id="tall">przewijana treść</div>
  <button id="deep">Na samym dole</button>

  <iframe id="ramka" src="/frame.html" width="300" height="120"></iframe>

  <div id="dialog" hidden>
    <button id="close-dialog">Zamknij okno</button>
    <input type="text" id="ref" placeholder="Numer rezerwacji">
  </div>
  <button id="open-dialog">Otwórz okno</button>

  <script>
    document.getElementById('unfold').onclick = () => {
      document.getElementById('folded').style.height = 'auto';
    };
    document.getElementById('covered').onclick = () => {
      document.getElementById('covered').textContent = 'Wysłano zgłoszenie';
    };
    document.getElementById('open-dialog').onclick = () => {
      document.getElementById('dialog').hidden = false;
      document.body.classList.add('locked');
    };
    document.getElementById('close-dialog').onclick = () => {
      document.getElementById('dialog').hidden = true;
      document.body.classList.remove('locked');
    };
  </script>
</body></html>
"""

FRAME = """<!doctype html>
<html lang="pl"><head><meta charset="utf-8"></head><body>
  <button id="w-ramce">Akceptuj wszystkie</button>
</body></html>
"""

# Long enough to be collapsed, with the shapes `match_option` has to get right:
# an unambiguous name, a shared prefix, a diacritic, and two that differ by one
# letter ("Iran" must never quietly stand in for "Irak").
NAMED = [
    "Polska (+48)", "Portugalia (+351)", "Peru (+51)", "Łódź (+00)",
    "Niemcy (+49)", "Norwegia (+47)", "Katar (+974)", "Iran (+98)",
    "Irak (+964)", "Hiszpania (+34)", "Francja (+33)", "Włochy (+39)",
]
OPTIONS = "".join(f"<option>{name}</option>" for name in NAMED) + "".join(
    f"<option>Kraj {i} (+{900 + i})</option>" for i in range(238)
)

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the fixtures, and serves the invoice as a DOWNLOAD.

    `Content-Disposition: attachment` is the whole point: Chrome does not render
    it, so a `target=_blank` link opens a tab, starts the transfer, and closes
    the tab — which is exactly how four real clicks produced four page snapshots
    and no file."""

    def log_message(self, *a):  # noqa: D102 — quiet
        pass

    def do_GET(self):  # noqa: N802 — http.server's spelling
        path = self.path.split("?", 1)[0]
        if path == "/faktura.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header(
                "Content-Disposition", 'attachment; filename="faktura 09-2026.pdf"'
            )
            self.send_header("Content-Length", str(len(PDF)))
            self.end_headers()
            self.wfile.write(PDF)
            return
        if path == "/przekierowanie":
            self.send_response(302)
            self.send_header("Location", "/hard.html")
            self.end_headers()
            return
        super().do_GET()


def serve(directory: str) -> int:
    handler = lambda *a, **kw: Handler(*a, directory=directory, **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def named(snapshot, name):
    for control in snapshot.controls:
        if control.name == name:
            return control
    raise AssertionError(
        f"no control named {name!r}. Found: "
        + ", ".join(f"{c.kind} {c.name!r}" for c in snapshot.controls)
    )


def absent(snapshot, name):
    hits = [c for c in snapshot.controls if c.name == name]
    assert not hits, f"{name!r} should not be listed, got {[c.line() for c in hits]}"


# A page shaped like imdb.com/user/<id>/ratings/: a long NUMBERED list whose
# rows each carry several controls, so it blows both budgets at once — the page
# text cap and the control cap (#268-#271). The row the checks reach for sits
# far past anything document order can buy.
LONG_LIST_ROWS = 250
LONG_LIST = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Your ratings</title></head>
<body>
<h1>Your ratings history</h1>
<a href="/">Home</a>
<button type="button">Actions</button>
ROWS
</body></html>
"""
ROW = """<div>
  <p>{n}. {title}</p>
  <p>{filler}</p>
  <a href="/title/tt{n}/">View title page for {title}</a>
  <button type="button">Your rating: {rating}</button>
  <button type="button">Watched {title}</button>
  <button type="button">Add {title} to Watchlist</button>
</div>"""


def long_list_html() -> str:
    filler = "Plot summary that exists to spend the page budget. " * 6
    rows = "\n".join(
        ROW.format(
            n=n,
            title=("Interstellar" if n == 137 else f"Film {n}"),
            rating=(n % 10) + 1,
            filler=filler,
        )
        for n in range(1, LONG_LIST_ROWS + 1)
    )
    return LONG_LIST.replace("ROWS", rows)


def check_long_list(url: str) -> None:
    """What a cut is allowed to claim, on a real page that provokes one.

    Everything in tests/ inspects the SOURCE of `CONTROLS_JS`. Whether the
    two-pass selection actually reaches a row the cap cannot is a question only
    a real DOM answers."""
    page = browser.browse_open(url + "ratings.html")
    rendered = web._present_snapshot(page)
    assert page.hidden, "expected this page to blow the control cap"
    print(
        f"opened the long list: {len(page.controls)} controls listed, "
        f"{page.hidden} not listed"
    )

    assert web.CUT_MARKER in rendered, "a 250-row page was not cut"
    assert f"of the {LONG_LIST_ROWS} numbered here" in rendered, (
        "the cut was not reported in the page's own units:\n" + rendered[-400:]
    )
    assert web.CONTROLS_COMPLETE not in rendered, (
        "the control list was capped and the notice still called it complete"
    )
    assert web.CONTROLS_CUT in rendered
    line = [ln for ln in rendered.splitlines() if web.CUT_MARKER in ln][0]
    print("  " + line.strip()[:160])

    # Document order cannot reach row 137. This is the failure the whole
    # narrowing exists for: not listed means not tagged means not actable.
    assert not [c for c in page.controls if "Interstellar" in c.name], (
        "row 137 should be far past the cap in document order"
    )

    narrowed = browser.browse_open(url + "ratings.html", topic="Interstellar")
    hit = named(narrowed, "View title page for Interstellar")
    print(
        f"  narrowed to 'Interstellar': [{hit.n}] {hit.kind} {hit.name!r}"
        f" — {narrowed.matching} matching control(s)"
    )
    assert narrowed.narrowed == "Interstellar"
    assert len(narrowed.controls) <= browse.MAX_CONTROLS, "narrowing widened the budget"
    # The chrome is still there: a topic drawn from page content never matches
    # the menu, and dropping it would trade one dead end for another.
    named(narrowed, "Home")

    # And the number it gave back really does act on that row.
    after = browser.browse_act(hit.n, "click", href="", mutating=False)
    assert "tt137" in after.url, f"pressing the narrowed control went to {after.url}"
    print(f"  pressed it → {after.url}")


def check_portal(url: str) -> None:
    """The flow the whole feature exists for, end to end."""
    page = browser.browse_open(url)
    print(f"opened {page.url}: {len(page.controls)} controls")

    switch = named(page, "Przełącz lokal")
    assert switch.mutating is False, "switching property is not a mutation"
    # `<a href="#">` goes nowhere, so it carries no destination and is
    # word-matched like the JavaScript control it is.
    assert switch.detail == "", switch

    assert "226,89" in page.text, f"panel never settled: {page.text!r}"
    assert not browse.still_loading(page.text)

    assert named(page, "Zapłać").mutating is True, "an href=# 'Zapłać' must be gated"
    assert named(page, "Faktury i płatności").mutating is False, "a GET link is not"
    assert named(page, "Filtruj").mutating is True, "a form submit is gated"
    assert named(page, "Szukaj faktury").kind == browse.FIELD
    absent(page, "csrf")

    # The menu is not in the first snapshot at all — this is the round trip that
    # no URL could have replaced.
    absent(page, "Bluszczanska")
    after = browser.browse_act(switch.n, "click")
    chosen = named(after, "Bluszczanska")
    print(f"clicked 'Przełącz lokal' → {len(after.controls)} controls, menu is open")

    final = browser.browse_act(chosen.n, "click")
    assert "Bluszczanska" in final.text, final.text
    assert "118,40" in final.text, f"the table never switched: {final.text!r}"
    print(f"clicked 'Bluszczanska' → {final.text.splitlines()[-1]}")

    typed = browser.browse_act(named(final, "Szukaj faktury").n, "type", text="wrzesień")
    field = named(typed, "Szukaj faktury")
    assert "wrzesień" in field.detail, field.detail
    print(f"typed into the search box → {field.line()}")

    stale = browser.browse_act(9999, "click")
    assert "no control [9999]" in stale.problem, stale.problem
    print("stale index refused:", stale.problem.splitlines()[0])


def check_hard(url: str) -> None:
    """Every way a control can be listed and unpressable."""
    page = browser.browse_open(url + "hard.html")
    print(f"\nopened {page.url}: {len(page.controls)} controls, "
          f"{page.unreachable} closed away")

    # 1. The responsive duplicate. Both copies are named the same and only one
    #    can be pressed; the drawer copy is what burned 45 seconds on eon.pl.
    navs = [c for c in page.controls if c.name == "Faktury i płatności"]
    assert len(navs) == 1, f"the off-canvas copy is still listed: {navs}"
    assert navs[0].detail.endswith("from=bar"), navs[0].detail
    print("responsive duplicate → only the on-screen copy is listed")

    # 2. The entry below a dropdown's scroll fold is REACHABLE — you scroll the
    #    dropdown. Four of five properties were listed on the real portal and
    #    the fifth was reported as closed away, so the model went back to
    #    guessing a URL for it (#251).
    for lokal in ("Wyspowa", "Bluszczanska", "Ananasowa",
                  "Garaż Bluszczańska", "Marii Cetysówny 2 m. 13"):
        named(page, lokal)
    print("scrolled-away dropdown entry → all 5 properties listed")

    # 3. A collapsed accordion is not a control list.
    absent(page, "Szczegóły rozliczenia")
    assert page.unreachable >= 2, page.unreachable
    opened = browser.browse_act(named(page, "Pokaż szczegóły").n, "click")
    named(opened, "Szczegóły rozliczenia")
    print("collapsed accordion → hidden until opened, then listed")

    # 3. The styled checkbox: the input is invisible, the label is the control.
    agree = named(opened, "Akceptuję regulamin")
    assert agree.kind == browse.CHECK, agree
    assert agree.detail == "unchecked", agree
    ticked = browser.browse_act(agree.n, "click")
    after = named(ticked, "Akceptuję regulamin")
    assert after.detail == "checked", f"the label click did not toggle it: {after}"
    print("styled checkbox → pressing the label toggled the hidden input")

    # 4. A 250-option dropdown says how many it has, not what they are.
    kraj = named(ticked, "kraj")
    assert kraj.kind == browse.CHOICE, kraj
    assert "250 options" in kraj.detail, kraj.detail
    assert "Portugalia" not in kraj.detail, kraj.detail
    print(f"long dropdown → {kraj.line()}")

    # And it is out of the page TEXT too, which is the bigger half: inner_text
    # includes every option of a closed <select>, and this one was 3 500 of the
    # 4 176 characters the model was being handed.
    assert "Kraj 200" not in page.text, page.text[:400]
    assert "250 options — see the control list" in page.text, page.text[:400]
    print(f"page text without the option flood → {len(page.text)} characters")

    chosen = browser.browse_act(kraj.n, "choose", value="niemcy")
    assert "Niemcy (+49)" in named(chosen, "kraj").detail, named(chosen, "kraj").detail
    print(f"chose by folded name → {named(chosen, 'kraj').detail}")

    lodz = browser.browse_act(kraj.n, "choose", value="Lodz")
    assert "Łódź" in named(lodz, "kraj").detail, named(lodz, "kraj").detail
    print("chose 'Lodz' → matched 'Łódź (+00)'")

    ambiguous = browser.browse_act(kraj.n, "choose", value="Ira")
    assert "matches 2 options" in ambiguous.problem, ambiguous.problem
    assert "Irak" in ambiguous.problem and "Iran" in ambiguous.problem
    print("ambiguous choice refused:", ambiguous.problem.splitlines()[0][:80])

    missing = browser.browse_act(kraj.n, "choose", value="Atlantyda")
    assert "no option matches" in missing.problem, missing.problem
    print("unmatched choice refused, with candidates")

    # 5. A control inside an iframe exists. It did not, before.
    frame_button = named(page, "Akceptuj wszystkie")
    print(f"iframe → {frame_button.line()}")

    # 6. A button under a transparent sheet. A real click cannot land; the
    #    keyboard can, and 'Wyślij' is mutating so the synthetic stage is off.
    covered = named(chosen, "Wyślij zgłoszenie")
    assert covered.mutating is True, covered
    started = time.monotonic()
    pressed = browser.browse_act(covered.n, "click", mutating=True)
    took = time.monotonic() - started
    assert "Wysłano zgłoszenie" in pressed.text, (pressed.problem, pressed.notice)
    assert took < 20, f"a covered control took {took:.0f}s — the ladder is not bounded"
    print(f"covered button → pressed in {took:.1f}s ({pressed.notice or 'plain click'})")

    # 7. The dialog that pins the page.
    deep = named(pressed, "Na samym dole")
    locked = browser.browse_act(named(pressed, "Otwórz okno").n, "click")
    absent(locked, "Na samym dole")
    named(locked, "Numer rezerwacji")
    print(f"dialog opened → [{deep.n}] 'Na samym dole' is now out of reach, "
          f"{locked.unreachable} closed away")

    # The numbering is re-issued for what is reachable NOW, so the control that
    # was [n] before the dialog is not [n] behind it. That is the contract — the
    # gate reads the same fresh snapshot, so the card names what is really there.
    assert not any(c.n == deep.n and c.name == deep.name for c in locked.controls)

    unlocked = browser.browse_act(named(locked, "Zamknij okno").n, "click")
    named(unlocked, "Na samym dole")
    print("dialog closed → the page is reachable again")

    # 8. The download that opens in a tab Chrome closes.
    got = browser.browse_act(named(unlocked, "Pobierz e-fakturę").n, "click")
    assert got.downloads, f"nothing was downloaded: {got.problem or 'no problem reported'}"
    saved = Path(got.downloads[0])
    assert saved.exists() and saved.read_bytes().startswith(b"%PDF"), saved
    assert saved.name == "faktura 09-2026.pdf", saved.name
    assert saved.parent == browser.downloads_dir(), saved.parent
    print(f"target=_blank download → {saved} ({saved.stat().st_size} bytes)")

    # 9. A READ that lands on a file keeps the file. This is the one that ended
    #    the E.ON task: aish held seven invoices, refetched every one
    #    anonymously, and told the owner it could not get them.
    read = browser.read(url + "faktura.pdf")
    assert read.downloads, f"a read that downloads must keep the file: {read.text!r}"
    assert Path(read.downloads[0]).read_bytes().startswith(b"%PDF")
    print(f"read_url on a file → {Path(read.downloads[0]).name}")

    # 10. The site sends you somewhere else and says so.
    moved = browser.browse_open(url + "przekierowanie")
    assert moved.asked.endswith("/przekierowanie"), moved.asked
    assert moved.url.endswith("/hard.html"), moved.url
    print(f"redirect → asked for {moved.asked}, landed on {moved.url}")


def main() -> int:
    assert "verify-browse-" in str(browser.profile_dir()), "refusing the real profile"
    root = tempfile.mkdtemp(prefix="portal-")
    Path(root, "index.html").write_text(PORTAL, encoding="utf-8")
    Path(root, "hard.html").write_text(HARD.replace("OPTIONS", OPTIONS), encoding="utf-8")
    Path(root, "frame.html").write_text(FRAME, encoding="utf-8")
    Path(root, "faktura.pdf").write_bytes(PDF)
    Path(root, "ratings.html").write_text(long_list_html(), encoding="utf-8")
    port = serve(root)
    url = f"http://127.0.0.1:{port}/"

    print(f"profile: {browser.profile_dir()}")
    check_portal(url)
    check_hard(url)
    check_long_list(url)

    browser.browse_close()
    browser.shutdown()
    print("\nOK — real Chrome, real DOM, real clicks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
