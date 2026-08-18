"""Driving a page, rather than reading one (#237).

`read_url` has exactly one verb: navigate to a URL and extract text. That is
enough for the web of documents and useless for the web of applications, where
what you want is behind a control and not behind an address. Measured, twice, on
the same portal: asked for his E.ON invoices, the model read the dashboard
correctly and then had to *guess* — `/mojeon/faktury`, `/rozliczenia`, `/pulpit`,
three 404s — because the thing it needed to press was `<a href="#">Przełącz
lokal</a>`, and no URL in the world is that button. The second attempt, with the
signed-in read working (#236), got further and stopped in exactly the same place.

So this module adds the verb. Three decisions shape all of it:

**A page is a NUMBERED LIST OF CONTROLS, not an image.** The remote view
(`browser.view_*`) maps a tap from a rendered JPEG because a human is looking at
it. A model estimating (x, y) mis-clicks constantly and worst at the edges, while
the DOM already knows where every control is and what it is called. Numbering
them means the model asks for `[7] button "Przełącz lokal"` — the same words the
owner used to describe what he wanted — and the click lands on the element, not
on a coordinate that used to be over it.

**The element is TAGGED, not remembered.** Enumeration stamps `data-aish-n` onto
each control and acting re-queries by that attribute. Playwright element handles
go stale the moment the page re-renders, which on an SPA is constantly; an
attribute survives a re-render and dies with the document, which is exactly the
lifetime the numbering has. A snapshot carries the epoch it was taken in, so an
index from a stale snapshot is refused rather than pressed blind.

**Nothing here decides whether an action is ALLOWED.** `is_mutating` labels a
control, and `Agent._browse_gate` is what stops it. The split is deliberate: this
module knows what the page says, the agent knows who is watching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# How many controls one snapshot may carry. A portal page runs to a few hundred
# interactive elements once every nav link and footer link is counted, and a
# list that long is both a context bill and unreadable. The cap is reported when
# it bites (`hidden`) — a silent truncation reads as "that is all there is",
# which is how a model concludes a control does not exist.
MAX_CONTROLS = 100

# Text long enough to be a paragraph is not a control's NAME. A whole article
# inside a clickable div would otherwise arrive as one enormous label.
NAME_MAX_CHARS = 120

# What a control is, in the model's vocabulary. Not the tag name: `input` covers
# a text box, a checkbox and a submit button, which need three different verbs.
LINK = "link"
BUTTON = "button"
FIELD = "field"
PASSWORD = "password"
CHOICE = "choice"
CHECK = "check"

# Verbs that make a control worth its own approval card. Polish first, because
# that is the owner's web: this is the vocabulary of a button that spends money,
# ends a contract, or throws something away.
#
# The list is deliberately BROAD and deliberately dumb. It costs a prompt when it
# is wrong and costs a paid bill when it is missing, and `approval.py` settled
# that trade the same way years ago — err toward prompting. It is a floor, not a
# guarantee: a button called "Dalej" that completes a purchase is caught by the
# form-submit rule below, or by nothing at all, which is why the driving grant is
# per host per task and every action is echoed as it happens.
_MUTATING_WORDS = (
    # money
    "zapłać", "zaplac", "płatnoś", "platnos", "opłać", "oplac", "kup", "zamów",
    "zamow", "przelew", "doładuj", "doladuj", "pay", "buy", "order", "checkout",
    "subscribe", "purchase",
    # destruction
    "usuń", "usun", "skasuj", "kasuj", "wyczyść", "wyczysc", "delete", "remove",
    "erase", "clear",
    # commitment and change
    "wypowiedz", "rozwiąż", "rozwiaz", "zerwij", "anuluj", "odwołaj", "odwolaj",
    "zmień", "zmien", "edytuj", "zapisz", "wyślij", "wyslij", "potwierdź",
    "potwierdz", "akceptuj", "zaakceptuj", "podpisz", "aktywuj", "dezaktywuj",
    "cancel", "terminate", "confirm", "accept", "submit", "save", "send",
    "sign", "activate", "deactivate", "upgrade", "downgrade",
    # identity
    "wyloguj", "logout", "sign out", "zaloguj", "login", "log in", "sign in",
)


@dataclass
class Control:
    """One thing on the page the model can touch."""

    n: int
    kind: str
    name: str
    # A link's href, a field's current value, a choice's options. Whatever the
    # model needs to decide WITHOUT another round trip — a link it can read
    # instead of clicking is a round trip saved, and the destination is often
    # the only way to tell two identically-named controls apart.
    detail: str = ""
    mutating: bool = False
    disabled: bool = False

    def line(self) -> str:
        """One line, as the model reads it."""
        bits = f"[{self.n}] {self.kind} {self.name!r}"
        if self.detail:
            bits += f" → {self.detail}"
        if self.disabled:
            bits += "  (disabled)"
        if self.mutating:
            bits += "  (needs approval)"
        return bits


# A downloaded file is bounded twice: one file may not be enormous, and the
# directory may not grow forever. Neither number is precious — they exist so that
# a portal handing back a 2 GB archive, or a year of monthly invoices, cannot
# quietly fill the disk of a box that also runs a Home Assistant VM.
DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
DOWNLOADS_KEEP_BYTES = 200 * 1024 * 1024


def safe_filename(suggested: str, fallback: str = "download") -> str:
    """A file name from a name the SITE chose.

    Everything that could steer where the write lands is removed rather than
    escaped — a path separator, a parent reference, a leading dot. The site
    picked this string, which puts it in the same class as any other page
    content: data, never instructions, and here the instruction would be a
    path."""
    name = (suggested or "").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^\w.\- ()]+", "_", name, flags=re.UNICODE).strip(". ")
    return name[:120] or fallback


def prune_downloads(directory: Any, keep_bytes: int = DOWNLOADS_KEEP_BYTES) -> list[str]:
    """Delete the oldest downloads until the directory fits. Returns what went.

    Oldest-first rather than content-addressed LRU (which is what the media
    store does): these are the owner's own documents under their own names, and
    a name is the only handle he has on one."""
    files = sorted(
        (f for f in directory.glob("*") if f.is_file()),
        key=lambda f: f.stat().st_mtime,
    )
    total = sum(f.stat().st_size for f in files)
    removed = []
    for path in files:
        if total <= keep_bytes:
            break
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed.append(path.name)
    return removed


@dataclass
class Snapshot:
    """A page as the model receives it: what it says, and what it can press."""

    url: str
    title: str
    text: str
    controls: list[Control] = field(default_factory=list)
    status: int | None = None
    # Controls the cap left out. Never silent: see MAX_CONTROLS.
    hidden: int = 0
    # Which document these numbers belong to. An index from an older epoch is
    # refused — the page moved on and [7] is a different button now.
    epoch: int = 0
    # Set when the action could not be carried out at all (a stale index, a
    # disabled control). The page is still returned, because the model's next
    # move depends on seeing where it actually is.
    problem: str = ""
    # Files this action produced, as local paths. The whole point of driving a
    # signed-in portal is often the document at the end of it, and the anonymous
    # opener behind read_pdf could never have fetched one.
    downloads: list[str] = field(default_factory=list)

    def control(self, n: int) -> Control | None:
        for control in self.controls:
            if control.n == n:
                return control
        return None


def is_mutating(
    name: str, kind: str, *, submits: bool = False, navigates: bool = False
) -> bool:
    """Would pressing this change something the owner would mind?

    Word-matched on the control's NAME, plus every form submit. Both halves
    matter: a button called "Zapłać" is obvious, and a nondescript "Dalej" that
    posts a form is the one that quietly does something.

    **A plain navigation is never mutating, whatever it is called.** An `<a>`
    with a real http href is a GET to another page — precisely what `read_url`
    does under auto-approval — so gating it would ask the owner's permission to
    read a page he already lets aish read, and the first thing the word list did
    was flag the link named "Faktury i płatności" because it contains the word
    for *payment*. What makes it safe is that it navigates, not that it is an
    anchor: an `<a href="#">` is a JavaScript control wearing a link's clothes,
    and it is word-matched like the button it is."""
    if kind in (FIELD, PASSWORD):
        # Typing changes nothing until something is pressed. Gating the keystroke
        # would ask twice for one act and train the owner to tap through.
        return False
    if navigates:
        return False
    if submits:
        return True
    lowered = f" {name.lower()} "
    return any(word in lowered for word in _MUTATING_WORDS)


# Enumerate, tag, and describe. Runs in the page, walks open shadow roots the
# same way `_LINKS_JS` does, and reports the count it left out rather than
# quietly stopping at the cap.
CONTROLS_JS = """(opts) => {
  const SEL = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=menuitem]', '[role=tab]',
    '[role=checkbox]', '[role=switch]', '[role=combobox]', '[onclick]',
  ].join(', ');
  const out = [];
  let matched = 0;
  const seen = new Set();

  const visible = (el) => {
    if (!el.getClientRects().length) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };

  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim().slice(0, opts.nameMax);

  // Does this anchor actually GO anywhere? `el.href` resolves `href="#"` to the
  // page's own absolute URL, so a plain "starts with http" test calls every
  // JavaScript control a navigation — and real Chrome duly reported
  // `<a href="#">Zapłać</a>` as a link to the current page, which would have
  // walked it straight past the gate. Same document, fragment aside, is not
  // going anywhere.
  const goesElsewhere = (el) => {
    const raw = el.getAttribute('href') || '';
    if (!raw || raw.startsWith('#')) return false;
    try {
      const to = new URL(el.href, location.href);
      const here = new URL(location.href);
      to.hash = ''; here.hash = '';
      return /^https?:$/i.test(to.protocol) && to.href !== here.href;
    } catch (e) { return false; }
  };

  const labelFor = (el) => {
    const by = el.getAttribute && el.getAttribute('aria-labelledby');
    if (by) {
      const parts = by.split(/\\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((n) => n.innerText || n.textContent || '');
      if (parts.length) return parts.join(' ');
    }
    if (el.id) {
      try {
        const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab) return lab.innerText || lab.textContent || '';
      } catch (e) { /* an id CSS cannot escape simply has no label */ }
    }
    const wrapping = el.closest && el.closest('label');
    if (wrapping) return wrapping.innerText || wrapping.textContent || '';
    return '';
  };

  const nameOf = (el) => {
    const aria = clean(el.getAttribute && el.getAttribute('aria-label'));
    if (aria) return aria;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
      // A field's own text is its VALUE, which is the user's data and not its
      // name — so the label comes first and the value is reported separately.
      const lab = clean(labelFor(el));
      if (lab) return lab;
      return clean(el.getAttribute('placeholder') || el.getAttribute('name')
                   || el.getAttribute('title') || '');
    }
    const text = clean(el.innerText || el.textContent || '');
    if (text) return text;
    return clean(el.getAttribute('title') || el.value || '');
  };

  const kindOf = (el, type) => {
    const tag = el.tagName;
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (tag === 'SELECT' || role === 'combobox') return 'choice';
    if (tag === 'TEXTAREA') return 'field';
    if (tag === 'INPUT') {
      if (type === 'password') return 'password';
      if (type === 'checkbox' || type === 'radio') return 'check';
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      return 'field';
    }
    if (role === 'checkbox' || role === 'switch') return 'check';
    if (tag === 'A' || role === 'link') return 'link';
    return 'button';
  };

  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
      // Clear LAST time's number before this pass can assign one. Each element
      // is visited exactly once, so clearing here is safe — and not clearing was
      // a real defect: the numbering shifts whenever a menu opens, so an element
      // that was [4] on the old page kept the tag while a different element
      // became [4] on the new one, and `locator(...).first` then picked whichever
      // came first in the document. Two elements, one number, silently.
      if (el.hasAttribute && el.hasAttribute('data-aish-n')) {
        el.removeAttribute('data-aish-n');
      }
      if (seen.has(el)) continue;
      let matches = false;
      try { matches = el.matches(SEL); } catch (e) { matches = false; }
      if (!matches) continue;
      seen.add(el);
      const type = ((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();
      if (el.tagName === 'INPUT' && type === 'hidden') continue;
      if (!visible(el)) continue;
      const name = nameOf(el);
      const href = (el.tagName === 'A' && goesElsewhere(el)) ? el.href : '';
      // A control with no name and nowhere to go cannot be asked for and cannot
      // be described — it would arrive as `[12] button ''`, which is noise.
      if (!name && !href) continue;
      matched += 1;
      if (out.length >= opts.max) continue;
      const n = out.length;
      el.setAttribute('data-aish-n', String(n));
      const kind = kindOf(el, type);
      let detail = href;
      if (kind === 'choice') {
        const opt = Array.from(el.options || []).map((o) => clean(o.text)).filter(Boolean);
        detail = opt.slice(0, 12).join(' | ');
      } else if (kind === 'field') {
        detail = el.value ? 'currently: ' + clean(el.value) : '';
      } else if (kind === 'check') {
        detail = el.checked ? 'checked' : 'unchecked';
      }
      out.push({
        n: n,
        kind: kind,
        name: name,
        detail: detail,
        // Reported apart from `detail` because it decides the GATE, not just the
        // description: a real http href means this control navigates.
        href: href,
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
        // A form submit is gated whatever it is called: the nondescript "Dalej"
        // that posts the form is the dangerous one, not the obvious "Zapłać".
        submits: !!(el.form && (type === 'submit' || el.tagName === 'BUTTON'
                                && (el.type || 'submit') === 'submit')),
      });
    }
  };
  walk(document);
  return {controls: out, matched: matched};
}"""


def controls_from(found: list[dict[str, Any]]) -> list[Control]:
    """The JS result, as typed controls. Split out so the labelling rules are
    testable without a browser."""
    controls = []
    for raw in found:
        kind = str(raw.get("kind") or BUTTON)
        name = str(raw.get("name") or "")
        controls.append(
            Control(
                n=int(raw.get("n", 0)),
                kind=kind,
                name=name,
                detail=str(raw.get("detail") or ""),
                mutating=is_mutating(
                    name,
                    kind,
                    submits=bool(raw.get("submits")),
                    navigates=bool(raw.get("href")),
                ),
                disabled=bool(raw.get("disabled")),
            )
        )
    return controls


# A page that is still fetching its own content has TEXT — "Wczytywanie danych",
# a spinner's label, a skeleton — so neither the emptiness test nor the thin-page
# retry catches it. Measured on eon.pl/mojeon/Umowy-i-dane/Moje-Umowy, which came
# back as its own loading message twice in one session while the owner watched.
_LOADING = re.compile(
    r"^\s*(wczytywanie|ładowanie|ladowanie|loading|proszę czekać|prosze czekac|"
    r"please wait|one moment)\b",
    re.I | re.M,
)


def still_loading(text: str) -> bool:
    """Does this page say, in so many words, that it has not finished?

    Only ever used to decide whether to WAIT longer — a false positive costs a
    couple of seconds, and a false negative hands the model a page that was
    about to contain the answer."""
    return bool(_LOADING.search(text or ""))
