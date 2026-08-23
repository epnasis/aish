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
  #wrap2 { position: relative; display: inline-block; }
  #sheet2 { position: absolute; inset: 0; background: transparent; }
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
  <!-- Covered AND inert: every rung reaches it and none of them does anything,
       which is the case the notice used to describe as a press. -->
  <div id="wrap2"><button id="deaf">Nic nie robi</button><div id="sheet2"></div></div>

  <div id="tall">przewijana treść</div>
  <button id="deep">Na samym dole</button>

  <iframe id="ramka" src="/frame.html" width="300" height="120"></iframe>

  <div id="dialog" hidden>
    <button id="close-dialog">Zamknij okno</button>
    <input type="text" id="ref" placeholder="Numer rezerwacji">
  </div>
  <button id="open-dialog">Otwórz okno</button>

  <!-- The spinner: pressing Szukaj shows a CSS donut and fetches. The DOM sits
       perfectly still for two seconds while the page is plainly unfinished,
       which is exactly where quiescence stops standing in for "finished". -->
  <!-- Two submits, same shape, different consequence. The search is an
       explicit GET — a link with the query typed into it — and must not draw a
       card; the other says nothing about its method and must. -->
  <form id="szukaj-get" method="get" action="hard.html">
    <input name="q" aria-label="Skąd i dokąd">
    <button type="submit">Szukaj połączeń</button>
  </form>
  <!-- The date picker's own Confirm: chrome, inside a widget, submits nothing. -->
  <div id="okienko" role="dialog">
    <button id="kal-ok" type="button">Confirm</button>
  </div>
  <!-- A form with values in it, and a button whose name always cards. The
       card has to say what this form HOLDS, read live. -->
  <form id="zamowienie" action="hard.html">
    <label for="imie">Imię</label><input id="imie" value="Paweł">
    <label for="miasto">Miasto</label><input id="miasto">
    <button type="submit">Zapłać teraz</button>
  </form>
  <form id="wyslij-nieznany" action="hard.html">
    <button type="submit">Dalej</button>
  </form>

  <section id="szukaj-wolno">
    <button id="szukaj-btn" type="button">Szukaj wolno</button>
    <div id="wyniki-wolne"></div>
  </section>

  <!-- A results list: three rows whose buttons all say the same thing, and
       whose boilerplate is identical. The only thing telling them apart is
       what each row says. -->
  <section id="wyniki">
    <article class="oferta">
      <p>07:45 – 09:10</p><p>LO123</p><p>1 przesiadka</p><p>640 PLN</p>
      <p>Bagaż podręczny wliczony</p><button>Wybierz</button>
    </article>
    <article class="oferta">
      <p>11:20 – 12:45</p><p>LO125</p><p>bezpośredni</p><p>720 PLN</p>
      <p>Bagaż podręczny wliczony</p><button>Wybierz</button>
    </article>
    <article class="oferta">
      <p>18:05 – 19:30</p><p>LO129</p><p>bezpośredni</p><p>590 PLN</p>
      <p>Bagaż podręczny wliczony</p><button>Wybierz</button>
    </article>
  </section>

  <!-- Icon-only controls, every way a real site writes one. The old rule
       dropped all of these: no words, nowhere to go. On a booking form they
       are half the controls that matter. -->
  <section id="ikony">
    <button id="swap" class="btn icon-swap-airports"></button>
    <button id="shut">×</button>
    <button id="cog"><svg viewBox="0 0 16 16"><title>Ustawienia</title></svg></button>
    <button id="bin"><svg viewBox="0 0 16 16"><use href="#icon-usun-pozycje"></use></svg></button>
    <button id="pic"><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="Drukuj"></button>
  </section>
  <!-- The combobox that made a batch necessary: typing opens a list that did
       not exist when the batch was composed. -->
  <section id="szukaj-lotu">
    <label for="dokad">Dokąd</label>
    <input id="dokad" role="combobox" autocomplete="off">
    <ul id="podpowiedzi"></ul>
    <label for="pasazerowie">Pasażerowie</label>
    <input id="pasazerowie">
    <button id="szukaj-lot">Szukaj lotu</button>
  </section>
  <!-- Two picker shapes, both real. WYLOT labels every cell with its full
       date (the ARIA-conformant shape). POWROT gives bare day numbers and
       states the month only in the grid's heading — and its next-month arrow
       is a typeless <button> inside a <form>, which the browser and aish both
       read as a SUBMIT. -->
  <section id="daty">
    <label for="wylot">Wylot</label>
    <input id="wylot" readonly>
    <div id="kal-wylot" role="grid" aria-label="wrzesień 2026" hidden></div>
    <form id="termin-form">
      <label for="termin">Termin</label>
      <input id="termin" readonly>
      <div id="kal-termin" class="datepicker" hidden>
        <div class="header">wrzesień 2026</div>
        <!-- No type attribute, inside a form: the browser and aish both read
             this as a SUBMIT button. -->
        <button id="kal-termin-next">Następny miesiąc</button>
        <table><tbody><tr><td role="gridcell">1</td></tr></tbody></table>
      </div>
    </form>
    <form id="powrot-form">
      <label for="powrot">Powrót</label>
      <input id="powrot" readonly>
      <div id="kal-powrot" class="datepicker" hidden>
        <div class="header">wrzesień 2026</div>
        <button id="kal-next" type="button">Następny miesiąc</button>
        <table><tbody><tr id="kal-dni"></tr></tbody></table>
      </div>
    </form>
  </section>
  <script>
    document.getElementById('szukaj-btn').onclick = () => {
      const box = document.getElementById('wyniki-wolne');
      box.innerHTML = '<div class="spinner" aria-label="Wczytywanie"></div>';
      // Nothing touches the DOM for two seconds. A single look lands here.
      setTimeout(() => {
        fetch(location.href).then(() => {
          box.innerHTML = '<p>Znaleziono 3 połączenia</p>';
        });
      }, 2000);
    };
    const MIESIACE = ['stycznia','lutego','marca','kwietnia','maja','czerwca',
      'lipca','sierpnia','września','października','listopada','grudnia'];
    const wylotGrid = document.getElementById('kal-wylot');
    for (let d = 1; d <= 30; d += 1) {
      const cell = document.createElement('div');
      cell.setAttribute('role', 'gridcell');
      cell.textContent = String(d);
      cell.setAttribute('aria-label', d + ' września 2026');
      if (d === 3) cell.setAttribute('aria-disabled', 'true');
      cell.onclick = () => {
        document.getElementById('wylot').value = d + ' września 2026';
        wylotGrid.hidden = true;
      };
      wylotGrid.appendChild(cell);
    }
    document.getElementById('wylot').onclick = () => { wylotGrid.hidden = false; };

    let powrotMiesiac = 8;  // 0-based: wrzesień
    const powrotBox = document.getElementById('kal-powrot');
    const rysuj = () => {
      document.querySelector('#kal-powrot .header').textContent =
        ['styczeń','luty','marzec','kwiecień','maj','czerwiec','lipiec','sierpień',
         'wrzesień','październik','listopad','grudzień'][powrotMiesiac] + ' 2026';
      const row = document.getElementById('kal-dni');
      row.innerHTML = '';
      for (let d = 1; d <= 28; d += 1) {
        const cell = document.createElement('td');
        cell.setAttribute('role', 'gridcell');
        cell.textContent = String(d);
        cell.onclick = () => {
          document.getElementById('powrot').value =
            d + ' ' + MIESIACE[powrotMiesiac] + ' 2026';
          powrotBox.hidden = true;
        };
        row.appendChild(cell);
      }
    };
    rysuj();
    document.getElementById('powrot').onclick = () => { powrotBox.hidden = false; };
    document.getElementById('termin').onclick = () => {
      document.getElementById('kal-termin').hidden = false;
    };
    document.getElementById('kal-next').onclick = (e) => {
      e.preventDefault();
      powrotMiesiac += 1;
      rysuj();
    };

    document.getElementById('dokad').addEventListener('input', (e) => {
      const list = document.getElementById('podpowiedzi');
      list.innerHTML = '';
      if (!e.target.value) return;
      for (const place of ['Paryż (CDG)', 'Paryż Orly (ORY)', 'Praga (PRG)']) {
        const item = document.createElement('li');
        item.setAttribute('role', 'option');
        item.textContent = place;
        item.onclick = () => {
          e.target.value = place;
          list.innerHTML = '';
        };
        list.appendChild(item);
      }
    });
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

# A page that plainly commits something, with none of it in the button's words:
# the amount is inside the form, and the button says "Dalej". Evidence read in
# the escalating direction is what puts the card back.
KASA = """<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><title>Kasa (atrapa)</title></head>
<body>
  <h1>Podsumowanie zamówienia</h1>
  <form action="podsumowanie.html">
    <p>Do zapłaty: 1 249,00 zł</p>
    <label for="ulica">Ulica</label><input id="ulica">
    <button type="submit">Dalej</button>
  </form>
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
        f"  narrowed to 'Interstellar': {hit.kind} {hit.address!r}"
        f" — {narrowed.matching} matching control(s)"
    )
    assert narrowed.narrowed == "Interstellar"
    assert len(narrowed.controls) <= browse.MAX_CONTROLS, "narrowing widened the budget"
    # The chrome is still there: a topic drawn from page content never matches
    # the menu, and dropping it would trade one dead end for another.
    named(narrowed, "Home")

    # And the address it gave back really does act on that row — through the
    # live re-resolution (#251), which has to be narrowed the same way or it
    # would go looking for this control on an unnarrowed page and not find it.
    after = browser.browse_act(
        hit.address, "click", href=hit.detail, mutating=False, topic="Interstellar"
    )
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
    after = browser.browse_act(switch.address, "click")
    chosen = named(after, "Bluszczanska")
    print(f"clicked 'Przełącz lokal' → {len(after.controls)} controls, menu is open")

    final = browser.browse_act(chosen.address, "click")
    assert "Bluszczanska" in final.text, final.text
    assert "118,40" in final.text, f"the table never switched: {final.text!r}"
    print(f"clicked 'Bluszczanska' → {final.text.splitlines()[-1]}")

    typed = browser.browse_act("Szukaj faktury", "type", text="wrzesień")
    field = named(typed, "Szukaj faktury")
    assert "wrzesień" in field.detail, field.detail
    print(f"typed into the search box → {field.line()}")

    stale = browser.browse_act("Wyślij pieniądze do Nigerii", "click")
    assert "no control on this page is called" in stale.problem, stale.problem
    print("unknown control refused:", stale.problem.splitlines()[0][:90])

    # The delta: the whole point of #251. Acting reports what changed, and the
    # page it did not change is not re-sent.
    before_len = len(final.text)
    changed = browse.diff_snapshots(final, typed)
    assert not changed.empty(), "typing into the search box changed nothing?"
    assert len(changed.render()) < before_len, (
        f"the change report ({len(changed.render())}) is not smaller than the "
        f"page ({before_len})"
    )
    print(f"delta: {len(changed.render())} chars vs {before_len} for the page")

    same = browse.diff_snapshots(typed, typed)
    assert same.empty() and "nothing on the page changed" in same.render()
    print("an action that changes nothing says so")


def check_icons(page) -> None:
    """An icon-only button is not a nameless button — it is a button whose name
    is a picture, and a person reads it fine (#251)."""
    # The glyph is KEPT and the meaning added beside it: it renders in a
    # terminal, it stores in the log, and '×' names that button only to someone
    # looking at it (#251).
    wanted = ("swap airports", "× (close)", "Ustawienia", "usun pozycje", "Drukuj")
    how_each = (
        "an icon class token", "a glyph", "the SVG's own <title>",
        "the <use> reference", "the <img alt>",
    )
    for expected, how in zip(wanted, how_each, strict=True):
        hits = [c for c in page.controls if c.name == expected]
        assert hits, (
            f"{how} should have named a control {expected!r}; got "
            f"{[c.name for c in page.controls]}"
        )
    print("icon-only controls named:", ", ".join(
        repr(c.address) for c in page.controls if c.name in wanted
    ))


# The shape qatarairways.com is, and the three things it broke (#273). Every
# part of this is taken from that page:
#
#   * the whole booking widget lives in an OPEN SHADOW ROOT, so `document`
#     queries and `document.activeElement` both stop at the host;
#   * the cookie wall arrives on a TIMER, after browse_open has already looked
#     for one, and then eats every click for the rest of the session;
#   * the picker is a two-month grid whose own label says "Travel Dates" while
#     the day cells state their full date, and it opens on the wrong month.
SHADOW = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Rezerwacja (atrapa)</title>
<style>
  #wall { position: fixed; inset: 0; background: rgba(0,0,0,.5); z-index: 99; }
  #wall.gone { display: none; }
</style></head>
<body>
  <div id="widget"></div>
  <div id="wall" hidden><button id="accept">Accept all</button></div>
  <script>
    // The consent wall is NOT here when the page loads. It arrives the way a
    // real consent SDK does — asynchronously, after the first look.
    setTimeout(() => { document.getElementById('wall').hidden = false; }, 1500);
    document.getElementById('accept').onclick = () => {
      document.getElementById('wall').classList.add('gone');
    };

    const host = document.getElementById('widget');
    const root = host.attachShadow({mode: 'open'});
    root.innerHTML = `
      <label for="dokad">Dokad</label>
      <input id="dokad" role="combobox" autocomplete="off">
      <ul id="podpowiedzi"></ul>
      <label for="wylot">Data wylotu</label>
      <input id="wylot" role="combobox" readonly aria-controls="kalendarz">
      <div id="kalendarz" role="grid" aria-label="Travel Dates" hidden>
        <div class="ngb-dp-weekdays" role="row">
          <div class="ngb-dp-weekday" role="columnheader">Pon</div>
          <div class="ngb-dp-weekday" role="columnheader">Wto</div>
        </div>
        <div id="dni"></div>
        <button id="poprzedni" type="button">Previous month</button>
        <button id="nastepny" type="button">Next month</button>
      </div>
      <button id="szukaj">Szukaj</button>`;

    let month = 9;  // opens on September; the test asks for November
    const MONTHS = {9: 'September', 10: 'October', 11: 'November'};
    const draw = () => {
      const box = root.getElementById('dni');
      box.innerHTML = '';
      for (let d = 1; d <= 28; d += 1) {
        const cell = document.createElement('div');
        cell.className = 'ngb-dp-day';
        cell.setAttribute('role', 'gridcell');
        cell.setAttribute('aria-label', d + ' ' + MONTHS[month] + ' 2026');
        cell.textContent = String(d);
        cell.onclick = () => {
          root.getElementById('wylot').value =
            d + ' ' + MONTHS[month].slice(0, 3) + ' 2026';
          root.getElementById('kalendarz').hidden = true;
        };
        box.appendChild(cell);
      }
    };
    draw();
    root.getElementById('wylot').onclick = () => {
      root.getElementById('kalendarz').hidden = false;
    };
    root.getElementById('nastepny').onclick = () => {
      if (month < 11) { month += 1; draw(); }
    };
    root.getElementById('poprzedni').onclick = () => {
      if (month > 9) { month -= 1; draw(); }
    };
    root.getElementById('dokad').addEventListener('input', (e) => {
      const list = root.getElementById('podpowiedzi');
      list.innerHTML = '';
      if (!e.target.value) return;
      for (const place of ['Malediwy (MLE)', 'Tamale (TML)']) {
        if (!place.toLowerCase().includes(e.target.value.toLowerCase())) continue;
        const item = document.createElement('li');
        item.setAttribute('role', 'option');
        item.textContent = place;
        item.onclick = () => { e.target.value = place; list.innerHTML = ''; };
        list.appendChild(item);
      }
    });
  </script>
</body></html>
"""


def check_shadow(url: str) -> None:
    """A whole booking form inside a shadow root, behind a late consent wall."""
    page = browser.browse_open(url + "shadow.html")
    print(f"\nopened {page.url}: {len(page.controls)} controls")

    # 1. The label lives in the shadow root with the control. Looking for it in
    #    the document found nothing, and a labelled field came back named after
    #    its generated id.
    named(page, "Dokad")
    named(page, "Data wylotu")
    print("shadow-rooted labels → fields are named, not 'mat-input-6'")

    # 2. The wall arrives AFTER browse_open looked. Every click lands on it
    #    until something looks again — which is why this is one act, not two.
    filled = browser.browse_fill([
        {"target": "Dokad", "do": "fill", "value": "MLE"},
        {"target": "Data wylotu", "do": "date", "value": "2026-11-08"},
    ])
    assert not filled.problem, filled.problem
    print("late consent wall →", filled.ledger[0])
    assert "Malediwy (MLE)" in filled.ledger[0], filled.ledger[0]

    # 3. The picker opens on September and says only "Travel Dates"; the cells
    #    say the month. Two hops to November, and the readback proves it.
    assert "8 Nov 2026" in filled.ledger[1], filled.ledger[1]
    assert "walking 2 month(s)" in filled.ledger[1], filled.ledger[1]
    print("mute heading, dated cells →", filled.ledger[1])

    # The keyboard rung is what makes a covered control reachable at all, and
    # it was being discarded: focus inside a shadow root reports the HOST as
    # active, so aish concluded focus had not landed on a field it had just
    # focused.
    typed = browser.browse_act("Dokad", "type", text="Tam")
    assert not typed.problem, typed.problem
    print("typing into a shadow-rooted field →", named(typed, "Dokad").line())


def check_batch(url: str) -> None:
    """A whole form in one act, on a real combobox (#251)."""
    browser.browse_open(url + "hard.html")
    filled = browser.browse_fill([
        {"target": "Dokąd", "do": "fill", "value": "CDG"},
        {"target": "Pasażerowie", "do": "fill", "value": "2"},
    ])
    assert not filled.problem, filled.problem
    assert len(filled.ledger) == 2, filled.ledger
    assert "Paryż (CDG)" in filled.ledger[0], filled.ledger[0]
    dokad = named(filled, "Dokąd")
    assert "Paryż (CDG)" in dokad.detail, dokad.detail
    print("batch →", " | ".join(filled.ledger))

    # Ambiguity is a question, never a guess — and it stops the batch.
    browser.browse_open(url + "hard.html")
    stopped = browser.browse_fill([
        {"target": "Dokąd", "do": "fill", "value": "Paryż"},
        {"target": "Pasażerowie", "do": "fill", "value": "2"},
    ])
    assert "matches 2 options" in stopped.ledger[-2], stopped.ledger
    assert "not attempted" in stopped.ledger[-1], stopped.ledger
    print("ambiguous suggestion →", stopped.ledger[-2][:80])


def check_calendar(url: str) -> None:
    """Both picker shapes, in real Chrome (#251)."""
    browser.browse_open(url + "hard.html")

    # Shape A: every cell carries its own full date.
    out = browser.browse_fill([{"target": "Wylot", "do": "date", "value": "2026-09-07"}])
    assert not out.problem, out.problem
    assert "7 września 2026" in out.ledger[0], out.ledger
    print("calendar (labelled cells) →", out.ledger[0])

    # A date the page will not take is refused, not pressed.
    browser.browse_open(url + "hard.html")
    blocked = browser.browse_fill(
        [{"target": "Wylot", "do": "date", "value": "2026-09-03"}]
    )
    assert "cannot be chosen" in "\n".join(blocked.ledger), blocked.ledger
    print("unavailable date refused →", blocked.ledger[0][:80])

    # Shape B: bare day numbers, month only in the heading — and the arrow that
    # would move it is a typeless <button> in a <form>, i.e. a submit.
    browser.browse_open(url + "hard.html")
    same = browser.browse_fill(
        [{"target": "Powrót", "do": "date", "value": "2026-09-14"}]
    )
    assert not same.problem, same.problem
    assert "14 września 2026" in same.ledger[0], same.ledger
    print("calendar (bare cells + heading) →", same.ledger[0])

    # A date in a later month: aish presses the picker's own arrow to get there.
    browser.browse_open(url + "hard.html")
    walked = browser.browse_fill(
        [{"target": "Powrót", "do": "date", "value": "2026-11-14"}]
    )
    assert not walked.problem, walked.problem
    assert "14 listopada 2026" in walked.ledger[0], walked.ledger
    assert "walking 2 month(s)" in walked.ledger[0], walked.ledger
    print("calendar (walked months) →", walked.ledger[0])

    # …but never an arrow that is a form submit in disguise.
    browser.browse_open(url + "hard.html")
    refused = browser.browse_fill(
        [{"target": "Termin", "do": "date", "value": "2026-10-14"}]
    )
    assert "form submit button" in "\n".join(refused.ledger), refused.ledger
    print("submit-shaped month arrow refused →", refused.ledger[0][-96:])


def check_rows(url: str) -> None:
    """Three identical buttons, told apart by their rows (#251)."""
    page = browser.browse_open(url + "hard.html")
    picks = [c for c in page.controls if c.name == "Wybierz"]
    assert len(picks) == 3, [c.line() for c in picks]

    # The label stays the prefix; what follows tells the row from its
    # neighbours; the boilerplate every row carries is gone.
    for control in picks:
        assert control.address.startswith("Wybierz — "), control.address
        assert "Bagaż podręczny wliczony" not in control.row, control.row
    assert len({c.address for c in picks}) == 3
    for control in picks:
        print("row →", control.line())

    # And a row is asked for by anything that identifies it — a price, a flight
    # number, a time — rather than by its position.
    cheapest = browse.resolve(page.controls, "590 PLN")
    assert cheapest.control is not None, cheapest.problem
    assert "18:05" in cheapest.control.address, cheapest.control.address
    assert browse.resolve(page.controls, "LO125").control.n == picks[1].n
    print("asked for by price →", cheapest.control.address)


def check_spinner(url: str) -> None:
    """A page that finishes after its DOM has gone quiet (#251)."""
    browser.browse_open(url + "hard.html")
    after = browser.browse_act("Szukaj wolno", "click")
    assert "Znaleziono 3 połączenia" in after.text, (
        "the read landed mid-spinner: " + after.text[-200:]
    )
    print("late-arriving results waited for →",
          [ln for ln in after.text.splitlines() if "Znaleziono" in ln])


def check_submit_gating(url: str) -> None:
    """A search is not a commit (#251)."""
    page = browser.browse_open(url + "hard.html")
    search = next(
        c for c in page.controls
        if c.kind == browse.BUTTON and c.name == "Szukaj połączeń"
    )
    assert not search.mutating, (
        "a GET search drew a card — aish follows the same query as a link "
        "without asking"
    )
    unknown = named(page, "Dalej")
    assert unknown.mutating, "a form that states no method must stay gated"
    print(f"GET search → {search.line()}")
    print(f"no method  → {unknown.line()}")


def check_grant_scope(url: str) -> None:
    """What the driving grant covers, and what it never will (#251)."""
    page = browser.browse_open(url + "hard.html")
    by_name = {c.name: c for c in page.controls}

    plain = by_name["Dalej"]
    assert plain.mutating and not plain.worded, plain
    picker = by_name["Confirm"]
    assert not picker.worded, "a picker's Confirm is chrome, not a commitment"
    assert not picker.mutating, picker
    pays = by_name["Zapłać teraz"]
    assert pays.worded, "a name that says it pays is never covered by a grant"
    assert not page.commit_evidence, (
        f"an ordinary page claimed commit evidence: {page.commit_evidence!r}"
    )
    print("grant covers →", plain.line())
    print("grant never covers →", pays.line())

    # …and on a page that says it commits, every submit is carded again.
    checkout = browser.browse_open(url + "podsumowanie.html")
    # Deliberately NOT a checkout-shaped address: the amount inside the form
    # is what has to be doing the work here.
    assert checkout.commit_evidence == "a price inside the form being submitted", (
        checkout.commit_evidence
    )
    print("escalates on →", checkout.commit_evidence)


def check_form_readback(url: str) -> None:
    """The card says what is about to be SENT, not just what is pressed."""
    page = browser.browse_open(url + "hard.html")
    pays = next(c for c in page.controls if c.name == "Zapłać teraz")
    held = dict(browse.form_values(page.controls, pays))
    assert held.get("Imię") == "Paweł", held
    assert held.get("Miasto") == "(empty)", held
    assert "Szukaj połączeń" not in held, "a different form on the same page"
    print("card would say →", browse.form_note(browse.form_values(page.controls, pays))
          .replace("\n", " | "))

    # And it is read LIVE: a value the page changed since aish last looked is
    # exactly what this exists to catch.
    browser.browse_act("Miasto", "type", text="Kraków")
    fresh = browser.browse_fields()
    again = next(c for c in fresh if c.name == "Zapłać teraz")
    assert dict(browse.form_values(fresh, again)).get("Miasto") == "Kraków"
    print("live re-read picked up →", "Miasto: Kraków")


def check_hard(url: str) -> None:
    """Every way a control can be listed and unpressable."""
    page = browser.browse_open(url + "hard.html")
    print(f"\nopened {page.url}: {len(page.controls)} controls, "
          f"{page.unreachable} closed away")
    check_icons(page)

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
    opened = browser.browse_act("Pokaż szczegóły", "click")
    named(opened, "Szczegóły rozliczenia")
    print("collapsed accordion → hidden until opened, then listed")

    # 3. The styled checkbox: the input is invisible, the label is the control.
    agree = named(opened, "Akceptuję regulamin")
    assert agree.kind == browse.CHECK, agree
    assert agree.detail == "unchecked", agree
    # `mutating` is what the GATE classified, and the act-time re-read refuses
    # anything the owner was not asked about (#251) — so a caller reaching past
    # `web.browse_act` has to carry the same verdict the card was drawn from.
    ticked = browser.browse_act(agree.address, "click", mutating=agree.mutating)
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

    chosen = browser.browse_act(kraj.address, "choose", value="niemcy")
    assert "Niemcy (+49)" in named(chosen, "kraj").detail, named(chosen, "kraj").detail
    print(f"chose by folded name → {named(chosen, 'kraj').detail}")

    lodz = browser.browse_act(kraj.address, "choose", value="Lodz")
    assert "Łódź" in named(lodz, "kraj").detail, named(lodz, "kraj").detail
    print("chose 'Lodz' → matched 'Łódź (+00)'")

    ambiguous = browser.browse_act(kraj.address, "choose", value="Ira")
    assert "matches 2 options" in ambiguous.problem, ambiguous.problem
    assert "Irak" in ambiguous.problem and "Iran" in ambiguous.problem
    print("ambiguous choice refused:", ambiguous.problem.splitlines()[0][:80])

    missing = browser.browse_act(kraj.address, "choose", value="Atlantyda")
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
    pressed = browser.browse_act(covered.address, "click", mutating=True)
    took = time.monotonic() - started
    assert "Wysłano zgłoszenie" in pressed.text, (pressed.problem, pressed.notice)
    assert took < 20, f"a covered control took {took:.0f}s — the ladder is not bounded"
    print(f"covered button → pressed in {took:.1f}s ({pressed.notice or 'plain click'})")
    # The rung below a real click must report what it SAW, not what it hoped.
    # This button changes its own text, so a press that took has visible proof.
    assert "may not have been registered" not in pressed.notice, (
        "the page plainly reacted — 'Wysłano zgłoszenie' is on it — so the "
        f"notice must not hedge: {pressed.notice!r}"
    )
    assert "could not check" not in pressed.notice, pressed.notice

    # 7. The dialog that pins the page.
    deep = named(pressed, "Na samym dole")
    locked = browser.browse_act("Otwórz okno", "click")
    absent(locked, "Na samym dole")
    named(locked, "Numer rezerwacji")
    deaf = browser.browse_act("Nic nie robi", "click")
    assert "may not have been registered" in deaf.notice, (
        "a control that swallows every rung must say so, not claim a press: "
        f"{deaf.notice!r}"
    )
    print("a press nothing answered →", deaf.notice[-70:])

    print(f"dialog opened → [{deep.n}] 'Na samym dole' is now out of reach, "
          f"{locked.unreachable} closed away")

    # The numbering is re-issued for what is reachable NOW, so the control that
    # was [n] before the dialog is not [n] behind it. That is the contract — the
    # gate reads the same fresh snapshot, so the card names what is really there.
    assert not any(c.n == deep.n and c.name == deep.name for c in locked.controls)

    unlocked = browser.browse_act("Zamknij okno", "click")
    named(unlocked, "Na samym dole")
    print("dialog closed → the page is reachable again")

    # 8. The download that opens in a tab Chrome closes.
    got = browser.browse_act("Pobierz e-fakturę", "click")
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
    Path(root, "podsumowanie.html").write_text(KASA, encoding="utf-8")
    Path(root, "shadow.html").write_text(SHADOW, encoding="utf-8")
    port = serve(root)
    url = f"http://127.0.0.1:{port}/"

    print(f"profile: {browser.profile_dir()}")
    check_portal(url)
    check_hard(url)
    check_batch(url)
    check_shadow(url)
    check_long_list(url)
    check_calendar(url)
    check_rows(url)
    check_spinner(url)
    check_submit_gating(url)
    check_grant_scope(url)
    check_form_readback(url)

    browser.browse_close()  # the keyless session this script drove
    browser.shutdown()
    print("\nOK — real Chrome, real DOM, real clicks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
