#!/usr/bin/env -S uv run python
"""Drive a real Chrome through a fake portal shaped like the one that failed.

Everything in tests/test_browse.py patches the browser away, which leaves one
thing unverified and it is the load-bearing one: whether `CONTROLS_JS` finds, on
a REAL page, the control the model needs. So this serves a local page built like
eon.pl/mojeon — a `<a href="#">Przełącz lokal</a>` that opens a menu, a table
that swaps when a property is chosen, a search field, and a panel that says
"Wczytywanie danych" for a second before its content arrives — and drives it.

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
from pathlib import Path

os.environ["AISH_STATE_DIR"] = tempfile.mkdtemp(prefix="verify-browse-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aish import browse, browser  # noqa: E402

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


def serve(directory: str) -> int:
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=directory, **kw
    )
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


def main() -> int:
    assert "verify-browse-" in str(browser.profile_dir()), "refusing the real profile"
    root = tempfile.mkdtemp(prefix="portal-")
    Path(root, "index.html").write_text(PORTAL, encoding="utf-8")
    # A real (tiny) PDF, so the download path is exercised end to end.
    Path(root, "faktura.pdf").write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    port = serve(root)
    url = f"http://127.0.0.1:{port}/"

    print(f"profile: {browser.profile_dir()}")
    page = browser.browse_open(url)
    print(f"opened {page.url}: {len(page.controls)} controls")
    for control in page.controls:
        print("   ", control.line())

    # 1. The control the whole feature exists for is found, and named as the
    #    owner names it.
    switch = named(page, "Przełącz lokal")
    assert switch.mutating is False, "switching property is not a mutation"
    # `<a href="#">` goes nowhere, so it carries no destination and is
    # word-matched like the JavaScript control it is.
    assert switch.detail == "", switch

    # 2. A page that says it is still loading is WAITED for, not reported.
    assert "226,89" in page.text, f"panel never settled: {page.text!r}"
    assert not browse.still_loading(page.text)

    # 3. Labelling, on real markup.
    assert named(page, "Zapłać").mutating is True, "an href=# 'Zapłać' must be gated"
    assert named(page, "Faktury i płatności").mutating is False, "a GET link is not"
    assert named(page, "Filtruj").mutating is True, "a form submit is gated"
    assert named(page, "Szukaj faktury").kind == browse.FIELD
    assert all(c.name != "csrf" for c in page.controls), "a hidden input is not a control"

    # 4. Press it. The menu it opens is not in the first snapshot at all — this
    #    is the round trip that no URL could have replaced.
    assert all(c.name != "Bluszczanska" for c in page.controls), "menu starts hidden"
    after = browser.browse_act(switch.n, "click")
    chosen = named(after, "Bluszczanska")
    print(f"clicked 'Przełącz lokal' → {len(after.controls)} controls, menu is open")

    final = browser.browse_act(chosen.n, "click")
    assert "Bluszczanska" in final.text, final.text
    assert "118,40" in final.text, f"the table never switched: {final.text!r}"
    print(f"clicked 'Bluszczanska' → {final.text.splitlines()[-1]}")

    # 5. Typing, with real keystrokes.
    typed = browser.browse_act(named(final, "Szukaj faktury").n, "type", text="wrzesień")
    field = named(typed, "Szukaj faktury")
    assert "wrzesień" in field.detail, field.detail
    print(f"typed into the search box → {field.line()}")

    # 6. A number from a page that has moved on is refused, not pressed blind.
    stale = browser.browse_act(9999, "click")
    assert "no control [9999]" in stale.problem, stale.problem
    print("stale index refused:", stale.problem.splitlines()[0])

    # 7. The document at the end of the flow: clicked through the session,
    #    saved locally, and named so read_pdf can open it.
    got = browser.browse_act(named(typed, "Pobierz e-fakturę").n, "click")
    assert got.downloads, f"nothing was downloaded: {got.problem or 'no problem reported'}"
    saved = Path(got.downloads[0])
    assert saved.exists() and saved.read_bytes().startswith(b"%PDF"), saved
    assert saved.name == "faktura 09-2026.pdf", saved.name
    assert saved.parent == browser.downloads_dir(), saved.parent
    print(f"downloaded → {saved} ({saved.stat().st_size} bytes)")

    browser.browse_close()
    browser.shutdown()
    print("\nOK — real Chrome, real DOM, real clicks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
