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
import unicodedata
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
    # Controls the PAGE is hiding — a closed menu, an off-canvas drawer, a panel
    # behind a dialog. Also never silent, and for a sharper reason than the cap:
    # a model that cannot see the control it wants concludes the page has none
    # and goes back to guessing URLs, which is the failure the whole tool exists
    # to end. Told what is hidden, it looks for the thing that opens it.
    unreachable: int = 0
    # Which document these numbers belong to. Nothing checks this field, and it
    # should not start: the TAG is the enforcement — it is written during
    # enumeration, cleared from anything the new pass does not list, and dies
    # with the document — so a number the page has moved past resolves to
    # nothing and is refused with a fresh list. An epoch handshake the model has
    # to echo would be one more thing a small model gets wrong for no gain.
    epoch: int = 0
    # Set when the action could not be carried out at all (a stale index, a
    # disabled control). The page is still returned, because the model's next
    # move depends on seeing where it actually is.
    problem: str = ""
    # Set when it WAS carried out, but not the obvious way — pressed with the
    # keyboard, followed to the link's own destination, dispatched straight to
    # the control. Kept apart from `problem` because they mean opposite things,
    # and because a press aish did not physically make must never be reported as
    # one it did.
    notice: str = ""
    # Files this action produced, as local paths. The whole point of driving a
    # signed-in portal is often the document at the end of it, and the anonymous
    # opener behind read_pdf could never have fetched one.
    downloads: list[str] = field(default_factory=list)
    # Where the model AIMED, when that is not where it landed. A site that
    # redirects is not an error and needs no refusal — but the model asked for
    # qatarairways.com/en-pl/help/feedback.retrieve.html, was silently handed
    # /en-pl/help.html, and spent the rest of the turn reasoning about a form
    # that was never on the page in front of it (#247).
    asked: str = ""

    def control(self, n: int) -> Control | None:
        for control in self.controls:
            if control.n == n:
                return control
        return None


def landed_elsewhere(asked: str, got: str) -> bool:
    """Did the navigation end up somewhere other than where it was aimed?

    Deliberately forgiving about the things that are not a redirect: a trailing
    slash, a fragment, and the scheme aish supplied itself when the model passed
    a bare host. Anything else — a different path, a different host, an appended
    query — is the site deciding where the model actually is, and it should say
    so rather than let the header quietly retitle itself."""
    def bare(url: str) -> str:
        url = (url or "").strip()
        url = url.split("#", 1)[0]
        for scheme in ("https://", "http://"):
            if url.startswith(scheme):
                url = url[len(scheme):]
                break
        return url.rstrip("/").lower()

    if not asked or not got:
        return False
    return bare(asked) != bare(got)


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


# Could the OWNER put this on screen and press it right now, using nothing but
# scrolling? That is the whole question, and getting it wrong is what made the
# feature fail on three unrelated sites in its first week (#244).
#
# The old test was "has a box, is not display:none, is not opacity:0" — which is
# also true of a mobile drawer parked at translateX(-100%), a closed menu, an
# aria-hidden subtree, and everything below the fold of a page whose body a modal
# has pinned. Playwright calls all of those visible, scrolls, finds them still
# outside the viewport, and retries for the full timeout. Six of sixteen actions
# in two real sessions died that way, at 45 seconds each.
#
# Below the fold is REACHABLE — you scroll to it. `left: -9999px` is not. Inside
# a scrollable dropdown currently scrolled past it is reachable; inside an
# `overflow:hidden` drawer is not. The distinction is scrollability, not
# position, and it is why this cannot be `elementFromPoint` or an
# IntersectionObserver: both answer "on screen NOW", which is the wrong question.
#
# Shared verbatim by the enumeration and by the act-time preflight, so the two
# can never disagree about what is pressable — the tag outlives the reachability
# (a menu closes while the model thinks) and the preflight is what catches that.
REACH_JS = """
  const styleCache = new Map();
  const styleOf = (el) => {
    let s = styleCache.get(el);
    if (!s) { s = getComputedStyle(el); styleCache.set(el, s); }
    return s;
  };

  // The page's own statement that this is not interactive content right now.
  const semanticallyHidden = (el) => {
    if (el.closest('[inert]')) return 'inert';
    if (el.closest('[aria-hidden="true"]')) return 'aria-hidden';
    if (el.closest('[hidden]')) return 'hidden';
    const closed = el.closest('details:not([open])');
    // A closed <details>'s own <summary> is exactly the thing you press to open
    // it, and `closest` matches through self — so exempt it by hand.
    if (closed && !(el.tagName === 'SUMMARY' && el.parentElement === closed)) {
      return 'closed-details';
    }
    return '';
  };

  // One native call that walks the ancestor chain in C++: display, visibility,
  // opacity and content-visibility at once.
  const cssVisible = (el) => (
    el.checkVisibility
      ? el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
      : el.getClientRects().length > 0
  );

  // A REAL overlap, not a shared edge. A collapsed accordion is `height: 0;
  // overflow: hidden`, so its content's box starts exactly where the container
  // ends — a touching test calls that visible, and the button inside a folded
  // panel came back as a control the model could press.
  const MEET = 2;
  const meets = (a, b) => (
    Math.min(a.right, b.right) - Math.max(a.left, b.left) >= MEET &&
    Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) >= MEET
  );

  const clipsOn = (s, axis) => (axis === 'x' ? s.overflowX : s.overflowY) !== 'visible';

  const userScrollable = (el, s, axis) => {
    const o = axis === 'x' ? s.overflowX : s.overflowY;
    if (o !== 'auto' && o !== 'scroll') return false;
    return axis === 'x' ? el.scrollWidth > el.clientWidth + 1
                        : el.scrollHeight > el.clientHeight + 1;
  };

  // The modal trick: body{position:fixed} or html+body{overflow:hidden} while a
  // dialog is up. Nothing below the fold can be reached until it closes.
  const rootLocked = (axis) => {
    const h = styleOf(document.documentElement);
    const b = document.body ? styleOf(document.body) : h;
    if (b.position === 'fixed') return true;
    const ho = axis === 'x' ? h.overflowX : h.overflowY;
    const bo = axis === 'x' ? b.overflowX : b.overflowY;
    return (ho === 'hidden' || ho === 'clip') && (bo === 'hidden' || bo === 'clip');
  };

  // '' when the owner could press it; otherwise why not.
  const unreachable = (el) => {
    const sem = semanticallyHidden(el);
    if (sem) return sem;
    if (!cssVisible(el)) return 'invisible';
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return 'zero-size';
    let box = {left: r.left, top: r.top, right: r.right, bottom: r.bottom};
    const view = {left: 0, top: 0, right: innerWidth, bottom: innerHeight};
    for (let node = el; node; node = node.parentElement) {
      const s = styleOf(node);
      if (s.position === 'fixed') {
        // A fixed element does not move when anything scrolls, so it is
        // reachable only if it is on screen already. This is the one test that
        // kills an off-canvas drawer: getBoundingClientRect is
        // transform-inclusive, so translateX(-100%) reports where it really is.
        return meets(box, view) ? '' : 'off-canvas';
      }
      const parent = node.parentElement;
      if (!parent || parent === document.body
          || parent === document.documentElement) break;
      const ps = styleOf(parent);
      if (!clipsOn(ps, 'x') && !clipsOn(ps, 'y')) continue;
      const pr = parent.getBoundingClientRect();
      let scrollable = false;
      for (const axis of ['x', 'y']) {
        if (!clipsOn(ps, axis) || !userScrollable(parent, ps, axis)) continue;
        scrollable = true;
        const near = axis === 'x' ? box.left - pr.left + parent.scrollLeft
                                  : box.top - pr.top + parent.scrollTop;
        const span = axis === 'x' ? box.right - box.left : box.bottom - box.top;
        const range = axis === 'x' ? parent.scrollWidth : parent.scrollHeight;
        if (near + span < -1 || near > range + 1) return 'outside-scroll-range';
      }
      if (!scrollable) {
        // overflow:hidden with nothing to scroll — the closed-drawer and folded
        // -accordion idiom. The element must ALREADY be inside the clip box.
        if (!meets(box, pr)) return 'clipped';
      }
      // From here up, the question is about the CONTAINER, not about where the
      // element currently sits inside it: once scrolled to, it appears within
      // this box. Intersecting the two instead was wrong in exactly the case
      // that matters — an entry below a dropdown's scroll fold has NO overlap
      // with the visible container, so the intersection came out inverted and
      // every ancestor test after it failed. On the real portal that hid the
      // fifth of five properties in the account switcher, and the model went
      // back to guessing a URL for it (#251).
      box = {left: pr.left, top: pr.top, right: pr.right, bottom: pr.bottom};
    }
    const doc = document.scrollingElement || document.documentElement;
    const absL = box.left + scrollX;
    const absT = box.top + scrollY;
    if (rootLocked('y')) {
      if (box.bottom < 0 || box.top > innerHeight) return 'behind-a-dialog';
    } else if (absT + (box.bottom - box.top) < -1 || absT > doc.scrollHeight + 1) {
      return 'off-document';
    }
    if (rootLocked('x')) {
      if (box.right < 0 || box.left > innerWidth) return 'behind-a-dialog';
    } else if (absL + (box.right - box.left) < -1 || absL > doc.scrollWidth + 1) {
      return 'off-document';
    }
    return '';
  };
"""
# A dropdown's options are not the page. An airport picker is 312 of them and a
# country-code picker is 250, and inlining that spends the control budget on
# data the model does not need until the moment it chooses — on qatarairways.com
# it pushed 51 real controls off the end of the list (#245). Up to this many are
# shown, because a yes/no/maybe select is better read than counted.
CHOICE_INLINE_MAX = 8

# Enumerate, tag, and describe. Runs in the page, walks open shadow roots the
# same way `_LINKS_JS` does, and reports both what the cap left out and what the
# page is currently hiding rather than quietly stopping.
CONTROLS_JS = "(opts) => {" + REACH_JS + r"""
  const SEL = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=menuitem]', '[role=tab]',
    '[role=checkbox]', '[role=switch]', '[role=combobox]', '[onclick]',
    '[contenteditable]', '[contenteditable=true]',
  ].join(', ');
  const out = [];
  let matched = 0;
  let unreached = 0;
  const seen = new Set();

  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, opts.nameMax);

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

  const labelElement = (el) => {
    if (el.id) {
      try {
        const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab) return lab;
      } catch (e) { /* an id CSS cannot escape simply has no label */ }
    }
    return (el.closest && el.closest('label')) || null;
  };

  const labelFor = (el) => {
    const by = el.getAttribute && el.getAttribute('aria-labelledby');
    if (by) {
      const parts = by.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((n) => n.innerText || n.textContent || '');
      if (parts.length) return parts.join(' ');
    }
    const lab = labelElement(el);
    if (lab) return lab.innerText || lab.textContent || '';
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
      // `id` last, and it is not decoration: an unlabelled dropdown would
      // otherwise be dropped as nameless, and a dropdown the model cannot ask
      // for is a form it cannot fill.
      return clean(el.getAttribute('placeholder') || el.getAttribute('name')
                   || el.getAttribute('title') || el.id || '');
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
    if (el.isContentEditable) return 'field';
    if (tag === 'A' || role === 'link') return 'link';
    return 'button';
  };

  const detailOf = (el, kind) => {
    if (kind === 'choice') {
      const list = Array.from(el.options || []);
      if (!list.length) return 'type to search';
      if (list.length <= opts.inlineChoices) {
        return list.map((o) => clean(o.text)).filter(Boolean).join(' | ');
      }
      const picked = el.selectedIndex >= 0 ? clean(list[el.selectedIndex].text) : '';
      return list.length + ' options'
             + (picked ? "; selected: '" + picked + "'" : '');
    }
    if (kind === 'field') {
      const value = el.isContentEditable ? clean(el.innerText || '') : clean(el.value || '');
      return value ? 'currently: ' + value : '';
    }
    if (kind === 'check') return el.checked ? 'checked' : 'unchecked';
    return '';
  };

  const emit = (el, tagOn, kind, name, href) => {
    // Offset by what earlier FRAMES already numbered, so one page has one
    // numbering however many documents it is made of.
    const n = opts.offset + out.length;
    tagOn.setAttribute('data-aish-n', String(n));
    out.push({
      n: n,
      kind: kind,
      name: name,
      detail: href || detailOf(el, kind),
      // Reported apart from `detail` because it decides the GATE, not just the
      // description: a real http href means this control navigates.
      href: href,
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      // A form submit is gated whatever it is called: the nondescript "Dalej"
      // that posts the form is the dangerous one, not the obvious "Zapłać".
      submits: !!(el.form && (type_(el) === 'submit' || el.tagName === 'BUTTON'
                              && (el.type || 'submit') === 'submit')),
    });
  };

  const type_ = (el) => ((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();

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
      const type = type_(el);
      if (el.tagName === 'INPUT' && type === 'hidden') continue;
      let tagOn = el;
      if (unreachable(el)) {
        // The one common pattern the predicate is wrong about: a native
        // checkbox hidden under a styled label, which is every custom toggle on
        // the web. Pressing the LABEL toggles the input through the browser's
        // own label activation — a real gesture, not a synthetic one.
        const lab = (el.tagName === 'INPUT' && (type === 'checkbox' || type === 'radio'))
          ? labelElement(el) : null;
        if (!lab || unreachable(lab)) { unreached += 1; continue; }
        tagOn = lab;
      }
      const name = nameOf(el);
      const href = (el.tagName === 'A' && goesElsewhere(el)) ? el.href : '';
      // A control with no name and nowhere to go cannot be asked for and cannot
      // be described — it would arrive as `[12] button ''`, which is noise.
      if (!name && !href) continue;
      matched += 1;
      if (opts.offset + out.length >= opts.max) continue;
      emit(el, tagOn, kindOf(el, type), name, href);
    }
  };
  walk(document);
  return {controls: out, matched: matched, unreachable: unreached};
}"""


# What a long dropdown contributes to the page TEXT, so it can be taken back
# out. `inner_text` includes every option of a closed `<select>` — measured: a
# 250-option country picker was 3 500 of a 4 176-character page, 84% of it, and
# on a real portal that is most of the read budget spent on one control the model
# has not even reached yet. The options come back as one contiguous block, in
# document order, exactly as they appear in the text.
FLOOD_JS = """(opts) => {
  const out = [];
  for (const sel of document.querySelectorAll('select')) {
    const list = Array.from(sel.options || []);
    if (list.length <= opts.inlineChoices) continue;
    out.push({
      count: list.length,
      name: (sel.getAttribute('name') || sel.id || '').slice(0, 60),
      // String.fromCharCode(10) rather than an escape: this JS lives inside a
      // Python string, and a backslash that survives one layer and not the
      // other is a syntax error only the page reports.
      text: list.map((o) => (o.text || '').trim()).join(String.fromCharCode(10)),
    });
  }
  return out;
}"""


def strip_option_floods(text: str, floods: list[dict[str, Any]]) -> str:
    """Take a long dropdown's options back out of the page text.

    The whole contiguous block, replaced by a count — never line by line, which
    would also delete the legitimate line elsewhere on the page that happens to
    read "Poland". A block that does not match exactly is left alone, so the
    worst this can do is nothing."""
    for flood in floods:
        block = str(flood.get("text") or "")
        if not block or block not in text:
            continue
        name = str(flood.get("name") or "")
        count = int(flood.get("count") or 0)
        named = f" '{name}'" if name else ""
        text = text.replace(
            block, f"[dropdown{named}: {count} options — see the control list]", 1
        )
    return text


# The same predicate, run on ONE element at act time. The tag outlives the
# reachability: `_settled_text` waits, the model thinks, and a menu that closes
# on scroll or on a timer leaves its entries tagged and unpressable. Asking again
# costs ~50ms and replaces a 45-second timeout with a sentence.
REACHABLE_JS = "(el) => {" + REACH_JS + "  return unreachable(el);\n}"

# `block: 'center'` is the sticky-header dodge — the middle of the viewport is
# the one place a pinned bar is not. It also takes a different code path from the
# CDP scroll Playwright uses internally, which is the one that has been silently
# failing to move anything.
CENTRE_JS = "(el) => { el.scrollIntoView({block: 'center', inline: 'center'}); }"

# Options as (label, value), read once at CHOOSE time rather than carried on
# every snapshot — see CHOICE_INLINE_MAX.
OPTIONS_JS = """(el) => Array.from(el.options || []).map(
  (o) => [(o.text || '').replace(/\\s+/g, ' ').trim(), o.value]
)"""


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


# How many candidates a failed choice hands back. The failure message IS the
# option list the model needed, which is what makes collapsing the list on the
# snapshot affordable — but a 312-option airport picker must not arrive here
# either.
CANDIDATES_SHOWN = 12


def fold(text: str) -> str:
    """Case, whitespace and accents removed, for comparing what a person typed
    against what a site wrote.

    Diacritics matter here more than they would elsewhere: this is the owner's
    web, where "Lodz" has to find "Łódź" and "zaplac" has to find "zapłać". The
    fold is NFKD + drop combining marks, which handles ó/ę/ą/ś/ż; ł has no
    combining form and is mapped by hand."""
    text = unicodedata.normalize("NFKD", (text or "").strip().casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.replace("ł", "l").split())


@dataclass
class Choice:
    """What `choose` should do: pick one, or explain instead of guessing."""

    value: str = ""
    label: str = ""
    problem: str = ""


def match_option(options: list[tuple[str, str]], asked: str) -> Choice:
    """Which option did the model mean?

    A ladder, tightest first, and it stops at the first rung that yields exactly
    one: the label verbatim, the label folded, the value verbatim, then a folded
    substring. Anything ambiguous or unmatched comes back as a PROBLEM carrying
    the candidates — the model asked wrong, and the list it needed is the answer
    to that.

    Deliberately NOT fuzzy. Edit-distance matching silently picks, and a `choose`
    is very often followed by a form submit: "Iran" quietly standing in for
    "Iraq" is the kind of wrong this whole module is built to avoid. Substring
    plus folding covers every honest case; anything past that is a question."""
    if not options:
        return Choice(problem="this control has no options to choose from")
    if not asked:
        return Choice(problem="say which option to choose")
    wanted = fold(asked)
    rungs = (
        [(lab, val) for lab, val in options if lab == asked],
        [(lab, val) for lab, val in options if fold(lab) == wanted],
        [(lab, val) for lab, val in options if val == asked],
        [(lab, val) for lab, val in options if wanted and wanted in fold(lab)],
    )
    for hits in rungs:
        if len(hits) == 1:
            return Choice(value=hits[0][1], label=hits[0][0])
        if len(hits) > 1:
            return Choice(
                problem=f"{asked!r} matches {len(hits)} options — "
                f"{_candidates(hits)}. Say which one."
            )
    return Choice(
        problem=f"no option matches {asked!r}. This control offers: "
        f"{_candidates(options)}."
    )


def _candidates(options: list[tuple[str, str]]) -> str:
    shown = ", ".join(repr(lab) for lab, _ in options[:CANDIDATES_SHOWN])
    left = len(options) - CANDIDATES_SHOWN
    return shown if left <= 0 else f"{shown}, and {left} more"


# A page that is still fetching its own content has TEXT — "Wczytywanie danych",
# a spinner's label, a skeleton — so neither the emptiness test nor the thin-page
# retry catches it. Measured on eon.pl/mojeon/Umowy-i-dane/Moje-Umowy, which came
# back as its own loading message twice in one session while the owner watched.
_LOADING = re.compile(
    r"^\s*(wczytywanie|ładowanie|ladowanie|loading|proszę czekać|prosze czekac|"
    r"please wait|one moment)\b",
    re.I | re.M,
)


# Only the top of the page. A finished article containing the line "Loading
# comments…" halfway down is not a page that is still loading, and paying the
# retry loop for it on every snapshot is a few seconds each time.
LOADING_LOOK_CHARS = 400


def still_loading(text: str) -> bool:
    """Does this page say, in so many words, that it has not finished?

    Only ever used to decide whether to WAIT longer — a false positive costs a
    couple of seconds, and a false negative hands the model a page that was
    about to contain the answer."""
    return bool(_LOADING.search((text or "")[:LOADING_LOOK_CHARS]))
