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

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from . import vocab

# How many controls one snapshot may carry. A portal page runs to a few hundred
# interactive elements once every nav link and footer link is counted, and a
# list that long is both a context bill and unreadable. The cap is reported when
# it bites (`hidden`) — a silent truncation reads as "that is all there is",
# which is how a model concludes a control does not exist.
#
# A cap this size cannot address a long list, and no way of CHOOSING the 100 is
# going to fix that (#270). Spreading them across the page's repeating rows was
# measured and rejected: a ratings row carries ~10 kinds of control, so a
# round-robin over control families buys rows 1-10 where document order buys
# rows 1-9. What changes the answer is `topic` — the caller names what they are
# looking for and the budget is spent on that first.
MAX_CONTROLS = 100

# How much of a repeated row aish reads and how much of it it shows. The first
# bounds what the PAGE can spend on this — twenty rows of a shopping list are
# twenty innerText reads — and the second bounds what the MODEL is charged for
# it. Neither is ever silent: what the display cap leaves out is counted, and
# what the model wants in full it narrows to (`match`) or reads whole.
ROW_LINES_MAX = 12
ROW_MAX_CHARS = 150

# The distinguishing fragment that goes in the ADDRESS, so a results row is
# asked for by something a person would say — 'Wybierz — 07:45', not '#7'.
# Short, because it has to be typed back exactly.
ROW_KEY_CHARS = 44

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
_MUTATING_WORDS = vocab.declare(
    "browse._MUTATING_WORDS",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    structural="the form-submit half of `is_mutating` — a non-GET submit draws a "
    "card whatever the button is called",
    note="Only the WORDED half fails open; a nondescript POST submit is gated "
    "structurally beside it.",
    entries=(
    # money
    "zapłać", "zaplac", "płatnoś", "platnos", "opłać", "oplac", "kup", "zamów",
    "zamow", "przelew", "doładuj", "doladuj", "pay", "buy", "order", "checkout",
    "subscribe", "purchase",
    # destruction
    "usuń", "usun", "skasuj", "kasuj", "wyczyść", "wyczysc", "delete", "remove",
    "erase", "clear",
    # commitment and change
    "rezerwuj", "zarezerwuj", "book", "reserve", "złóż", "zloz", "transfer",
    "autoryzuj", "zezwól", "zezwol",
    "wypowiedz", "rozwiąż", "rozwiaz", "zerwij", "anuluj", "odwołaj", "odwolaj",
    "zmień", "zmien", "edytuj", "zapisz", "wyślij", "wyslij", "potwierdź",
    "potwierdz", "akceptuj", "zaakceptuj", "podpisz", "aktywuj", "dezaktywuj",
    "cancel", "terminate", "confirm", "accept", "submit", "save", "send",
    "sign", "activate", "deactivate", "upgrade", "downgrade",
    # identity
    "wyloguj", "logout", "sign out", "zaloguj", "login", "log in", "sign in",
    # Out of its group ON PURPOSE (#342). `zamawiam` belongs with the money
    # words above, and it sits here because `TestTheCountingChangedNoMatching`
    # matches a declared change as `before − removed + added` EXACTLY — an
    # entry spliced into the middle reads as a reordering of the whole list and
    # destroys the before-picture the fence exists to hold. It was ADDED, not
    # moved: nothing here covers it (`zamow` is not a substring of `zamawiam`,
    # unlike `kupuje`/`kupie`, which ride `kup`), so `Zamawiam z obowiązkiem
    # zapłaty` — the statutory Polish checkout wording — classified as NOT
    # mutating while the gate refused it. The gate is not the only reader:
    # `browser.browse_act`'s act-time re-resolution and the batch runner's
    # sight-unseen fence both read `Control.mutating`, and a stale snapshot
    # landing on that live label would have been PRESSED where `Kup teraz` is
    # refused. That is the exact hole dual membership exists to close.
    "zamawiam",
))


# --------------------------------------------- consequences with no yes button
#
# The things aish may never do to one of his accounts, whoever approves. It is
# NOT a bigger `_MUTATING_WORDS`: that list decides what draws a CARD, and under
# the owner's own constraint — "there's this decision fatigue if I have to
# please click approve for every action, it's not gonna happen" — a card he taps
# through is worse than none, because it records a consent he never gave. Moving
# a consequence from approvable to UNAPPROVABLE removes a decision instead of
# adding one, which is the only kind of control that survives him not reading
# cards.
#
# THE SET IS IN TWO HALVES, AND THEY ARE MATCHED DIFFERENTLY ON PURPOSE (#342).
# It was one half and "deliberately narrow" for as long as it held only the
# account-control acts, where a match needs a change VERB and the thing being
# changed adjacent in one label — never a bare noun, which would take "e-mail"
# in a nav bar. The commit verbs that joined it are whole verb CLASSES, and
# broad ones: a control whose own words say it buys, pays, orders, subscribes,
# ends a contract or deletes. So "narrow" is no longer the description. What
# holds instead is that every entry is read as a WORD and not as a run of
# letters — the surname "Kuprianowicz" contains `kup` and `SZCZEGÓŁY ZAKUPU`
# contains it twice — and that the broad classes are exempted where the page's
# own structure says pressing cannot commit: a link that navigates, and a read.
#
# The asymmetry that set the calibration is unchanged and now cuts harder: a
# false positive on `_MUTATING_WORDS` costs a prompt, and a false positive here
# removes the capability outright with no way to grant it back in the moment.
# That is why the commit verbs are boundaried and why `order`, `purchase`,
# `book` and `reserve` moved as PHRASES — measured over 3,002 recorded control
# labels, the bare words live in `Order history`, `Purchase history` and
# `Book a flight`, which are read surfaces and a search box, not commitments.
#
# The escape hatch is the one `NO_PASSWORDS` already established and the reason
# this is not a wall: he is handed `/browser <host>` and does it himself. The
# capability is not removed from HIM, it is removed from aish's hands.

_CHANGE_VERBS = vocab.declare(
    "browse._CHANGE_VERBS",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    note="Half of the payout/contact pair: a miss here drops the whole "
    "unapprovable verdict back to an ordinary approval card.",
    entries=(
    "zmien", "zmiana", "edytuj", "edycja", "ustaw", "aktualizuj", "aktualizacja",
    "popraw", "podmien", "dodaj", "change", "update", "edit", "set", "modify",
    "replace", "add new",
))
_CONTACT_NOUNS = vocab.declare(
    "browse._CONTACT_NOUNS",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    entries=(
    "e-mail", "email", "mail", "adres e", "telefon", "tel.", "numer telefonu",
    "komork", "phone", "mobile", "contact detail", "dane kontaktowe",
    "recovery", "odzyskiwan",
))
_PAYOUT_NOUNS = vocab.declare(
    "browse._PAYOUT_NOUNS",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    structural="`types_a_bank_account` — an IBAN is refused as a VALUE wherever "
    "it is typed, with no label consulted",
    entries=(
    "konto bankow", "rachunek", "numer konta", "iban", "bank account",
    "payout", "payment method", "metoda platnosci", "karta platnicz",
    "billing address", "dane do przelewu",
))
# Some labels need no verb: the noun IS the act on these pages, and "nowe haslo"
# carries no verb at all.
_CONTACT_PHRASES = vocab.declare(
    "browse._CONTACT_PHRASES",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    entries=(
    "nowy adres e-mail", "nowy e-mail", "nowy email", "nowy numer telefonu",
    "nowy numer", "nowy telefon", "new e-mail address", "new email address",
    "new email", "new phone number", "new phone", "new mobile number",
    "zmien adres e-mail", "change email address", "change e-mail address",
))
_CREDENTIAL_PHRASES = vocab.declare(
    "browse._CREDENTIAL_PHRASES",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    structural="`NO_PASSWORDS` — aish never types a stored password on the browse "
    "path at all",
    entries=(
    "zmien haslo", "zmiana hasla", "nowe haslo", "ustaw haslo", "resetuj haslo",
    "reset hasla", "przypomnij haslo", "change password", "new password",
    "set password", "reset password", "update password", "change pin",
    "zmien pin",
))
# ACCOUNT-scoped, and the narrowing this comment used to argue for was
# OVERRULED by the owner (#342). It said: blanket "delete" is already
# word-matched into a card, and making every delete unapprovable would stop
# aish throwing away a draft, a reminder or a single message — far more
# capability than it protects, because a delete on a page is nearly always a row
# and not an account. He decided against it in his own words: the escape is
# `/browser`, and if a real task breaks, the softening comes back WITH THAT TASK
# as its evidence. So `_COMMIT_DELETE` below refuses every delete, and this list
# survives only because "close your account" is a narrower thing to be told than
# "delete something". Blanket destruction is still a REVERSIBILITY problem (a
# trash that empties later, #177); that mechanism would be what lets the
# softening return.
# Signing in THROUGH somebody else. Measured on linkedin.com 2026-08-22: asked
# to sign in with stored credentials and having none, the model pressed "Sign
# in", then "Continue with Google", then "Kontynuuj jako Sage" — two clicks
# from binding the owner's LinkedIn to an identity that is not his, and he
# stopped it himself. It is account control, it is not undone by clicking
# again, and there is no yes for it: aish does not choose who he is. The
# generic verbs ("continue", "kontynuuj") are matched only WITH a provider
# name, so an ordinary "Continue" button on a checkout is untouched.
_IDP_PROVIDERS = vocab.declare(
    "browse._IDP_PROVIDERS",
    languages="brand names — locale-invariant",
    on_miss=vocab.PERMITS,
    entries=(
    "google", "apple", "facebook", "microsoft", "github", "linkedin",
    "twitter", " x ", "amazon", "okta", "auth0", "saml", "sso",
))
_IDP_VERBS = vocab.declare(
    "browse._IDP_VERBS",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    entries=(
    "continue with", "sign in with", "sign up with", "log in with",
    "login with", "zaloguj sie przez", "zaloguj przez", "zaloguj sie z",
    "kontynuuj z", "kontynuuj przez", "polacz z", "connect with",
))
_IDP_ALONE = vocab.declare(
    "browse._IDP_ALONE",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    entries=(
    "kontynuuj jako", "continue as", "single sign-on", "use another account",
    "uzyj innego konta",
))
# "Continue as <name>" is an account chooser; "Continue as guest" is a checkout
# and blocking it would cost real work. The exception is the narrow one, not a
# general softening of the rule.
_NOT_AN_IDENTITY = vocab.declare(
    "browse._NOT_AN_IDENTITY",
    languages="Polish + English",
    on_miss=vocab.FRICTION,
    note="Inverted: this list EXEMPTS. A miss refuses a guest checkout, which "
    "costs work the owner finishes himself — the only list here whose miss is "
    "safe in the opposite direction.",
    entries=("guest", "gosc", "goscia", "anonim", "visitor"),
)

_CLOSE_ACCOUNT_PHRASES = vocab.declare(
    "browse._CLOSE_ACCOUNT_PHRASES",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    entries=(
    "usun konto", "usuniecie konta", "skasuj konto", "zamknij konto",
    "likwidacja konta", "zlikwiduj konto", "delete account", "close account",
    "delete my account", "terminate account", "deactivate account",
    "usun profil", "delete profile",
))

# --------------------------------------------------------- the commit verbs
#
# A control whose OWN WORDS say it buys, pays, orders, books, reserves,
# subscribes, tops up or ends a contract — and a control that deletes (#342).
# These used to draw a card, and the card was the whole problem: measured this
# period, the owner's median tap on one is 4.3 seconds and 11 of 34 were under
# 3. There is no future in which aish pressing "Zapłać" was wanted, so a popup
# offering a yes for it protects nothing and trains the tap that is waiting on
# the purchase.
#
# **THEY DID NOT LEAVE `_MUTATING_WORDS`, THEY JOINED THIS AS WELL, AND THAT
# DUAL MEMBERSHIP IS LOAD-BEARING.** `Control.mutating` is read by three fences
# besides the card: `browser.browse_act` refuses when the LIVE control needs
# approval and the approved snapshot did not ("the page moved between the card
# and the press"), and `plan_batch` uses it for the mid-step and sight-unseen
# fences. Had `kup` simply moved lists, a stale-snapshot press landing on a
# live JavaScript "Kup teraz" would have classified as not-mutating and RUN —
# a path that ends in a refusal today would have ended in a buy. The gate
# consults these lists before it draws a card, so the FATE moves while the
# structural fences keep working by construction.
#
# Matched as WORDS (`whole_words`), unlike everything above. `vocab.hit` is a
# bare substring scan, calibrated for a list whose false positive costs a
# prompt; these cost the capability outright, so the matching has to implement
# the owner's sentence — *a control whose own words SAY it buys* — rather than
# "contains these letters". Measured over 3,002 recorded control labels, bare
# substrings also refuse `facebook`, `Books`, `Kontynuuj zakupy`,
# `SZCZEGÓŁY ZAKUPU #1`, `Manage booking` and the surname `Tamara Kuprianowicz`.
#
# **A GUARD THAT MISSES FALLS BACK TO A CARD, NEVER TO SILENCE**, and that is
# what makes every narrowing below safe. Because the words stayed in
# `_MUTATING_WORDS`, a label these lists do not catch still draws an approval
# card — exactly what it did before this issue. Narrowing a guard here can move
# a label from REFUSAL back to CARD; it can never move one to FREE.
_COMMIT_MONEY = vocab.declare(
    "browse._COMMIT_MONEY",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    structural="none — and the epic says so out loud: an unworded 'Jetzt kaufen' "
    "rides the site grant. #299 owns the miss; this list owns the hit.",
    note="A miss drops back to `_MUTATING_WORDS`, which still holds every one "
    "of these words — so a miss is a CARD, never silence.",
    entries=(
    # Folded forms only: `commits()` folds its input, so `zaplac` finds
    # "Zapłać". The ASCII twins stay in `_MUTATING_WORDS` for the card path.
    "zaplac", "oplac", "kup", "kupuje", "kupie", "zamow", "zamawiam",
    "przelew", "doladuj", "pay", "buy", "checkout",
    # `order` and `purchase` move as PHRASES ONLY. Both survive word
    # boundaries in `Order history`, `Track order` and `Purchase history` —
    # read surfaces, and refusing them would cost the owner his own receipts.
    "place order", "place your order", "order now", "complete order",
    "submit order", "order and pay", "zloz zamowienie",
    "complete purchase", "purchase now", "confirm purchase",
))
_COMMIT_SUBSCRIPTION = vocab.declare(
    "browse._COMMIT_SUBSCRIPTION",
    languages="English",
    on_miss=vocab.PERMITS,
    note="Its own consequence rather than a money word: a newsletter costs "
    "nothing, so 'buy something' would be a claim the label does not support.",
    entries=("subscribe",),
)
# PHRASES ONLY, and this one is a DRIVE decision recorded on #295 — the owner
# may overrule it. Measured, the bare word's real occurrences in his corpus are
# `Book a flight` (44 sightings), `Book` (36) and `Book your flight from Krakow`
# (3): search-start buttons on a booking site's front page. Refusing them means
# aish cannot begin a flight or hotel search at all, which is the epic's own
# flagship flow — do the whole job, stop one step short.
#
# THE RESIDUAL, WRITTEN DOWN RATHER THAN LEFT TO BE DISCOVERED: a payment-free
# binding — a restaurant table, a doctor's appointment — labelled bare `Reserve`
# stays a CARD, and P2 says a card is not a control. #299's judge is the
# systemic cover, and this is one of the misses it is scoped to catch.
_COMMIT_BOOKING = vocab.declare(
    "browse._COMMIT_BOOKING",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    note="A miss is a card, not silence — `book`/`reserve`/`rezerwuj` are all "
    "still in `_MUTATING_WORDS`.",
    entries=(
    "complete booking", "confirm booking", "book and pay", "reserve and pay",
    "rezerwuje z obowiazkiem zaplaty", "potwierdz rezerwacje",
    "zarezerwuj i zaplac",
))
# ENDING A CONTRACT NEEDS THE CONTRACT, and this is the one guard here that is
# not about breadth but about HOMONYMS. `wypowiedz`, `rozwiąż` and `zerwij` are
# not narrow-or-broad versions of the same verb — they are DIFFERENT WORDS that
# fold to the same string. Measured against the census: `Rozwiąż quiz` and
# `Rozwiąż test` (rozwiązać = to SOLVE), `Dodaj wypowiedź` (a COMMENT, which
# folds to `wypowiedz` exactly) and `Zerwij z nałogiem` (break a habit) were all
# refused by the bare word. Refusing those removes a capability with no way to
# grant it back in the moment, so they take the verb+noun shape the
# account-scoped refusals in this file already use.
#
# `wypowiedzenie` is here as its own entry, not as a prefix: word boundaries
# mean `wypowiedz` does not reach inside it, and `Wypowiedzenie umowy` is
# exactly what a Polish provider calls that button. `Wypowiedzi` (comments)
# stays free because the noun is what fires it, not the verb.
_END_CONTRACT_VERBS = vocab.declare(
    "browse._END_CONTRACT_VERBS",
    languages="Polish",
    on_miss=vocab.PERMITS,
    structural="none — but a miss here is a CARD, not silence: every one of "
    "these is also in `_MUTATING_WORDS`",
    entries=("wypowiedz", "wypowiedzenie", "rozwiaz", "zerwij"),
)
# Matched as a PREFIX (plain substring, unlike everything else in this block),
# because Polish declines the noun: `umow` has to find "umowa", "umowę" and
# "umowy", and a word boundary after `umow` finds none of them.
_CONTRACT_NOUNS = vocab.declare(
    "browse._CONTRACT_NOUNS",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    note="Half of the end-a-contract pair: a miss drops the refusal back to an "
    "ordinary approval card.",
    entries=(
    "umow", "kontrakt", "abonament", "subskrypcj",
    "contract", "agreement", "subscription", "plan",
))
# `terminate` keeps the bare form: it has no benign Polish or English homonym,
# and `Terminate instance` is destructive on its own terms anyway.
_COMMIT_CONTRACT = vocab.declare(
    "browse._COMMIT_CONTRACT",
    languages="English",
    on_miss=vocab.PERMITS,
    entries=("terminate",),
)
# Every delete, not an account delete — the owner's overrule, above. `wyczyść`
# and `clear` are NOT here: the issue's prose moved one and left its English
# twin behind, and both are the vocabulary of clearing a FILTER rather than of
# destroying data, so the list is what shipped and not the prose.
_COMMIT_DELETE = vocab.declare(
    "browse._COMMIT_DELETE",
    languages="Polish + English",
    on_miss=vocab.PERMITS,
    note="A miss is a card: every one of these is also in `_MUTATING_WORDS`.",
    entries=("usun", "skasuj", "kasuj", "delete", "remove", "erase"),
)

# What the owner is told, in his words, about what aish just refused to do.
IRREVERSIBLE = {
    "contact": "change the e-mail or phone number on your account",
    "payout": "change where money is sent from your account",
    "credential": "change a password or PIN on your account",
    "account": "close or delete your account",
    "identity": "sign you in through Google, Apple or another provider",
}

# The same, for the commit verbs. Named by the CONSEQUENCE and never by the
# mechanism (#295 P1): the owner is told what pressing it would have done to
# him, not which list caught it or which tool asked.
COMMITS = {
    "money": "buy something or pay money from your account",
    "subscription": "start a subscription in your name",
    "booking": "commit you to a booking",
    "contract": "end a contract in your name",
    "delete": "delete something",
}


@cache
def _boundaried(needles: tuple[str, ...]) -> re.Pattern[str]:
    """One alternation over `needles`, matching only at word edges.

    Cached on the tuple itself — these are module constants, so the compile
    happens once per list and a page of sixty controls pays nothing for it."""
    return re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(n) for n in needles) + r")(?!\w)"
    )


def _has(
    folded: str, needles: tuple[str, ...], name: str, *, whole_words: bool = False
) -> bool:
    """One consultation of `name`, counted (#322).

    The name is passed rather than derived, because these lists are plain
    tuples on purpose — a wrapper object that could know its own name would
    also be a wrapper that could change what is matched, and this slice's whole
    value is that nothing about the matching moved.

    `whole_words` is the one calibration that ever moved, and only for lists
    added after it (#342): a substring scan is right for a list whose false
    positive costs a prompt and wrong for one whose false positive removes a
    capability. Every list that predates it still reaches `vocab.hit` on the
    default, unchanged."""
    if whole_words:
        matched = bool(_boundaried(needles).search(folded))
        vocab.note(name, matched=matched)
        return matched
    return vocab.hit(name, needles, folded)


def commits(name: str, *, navigates: bool = False) -> str:
    """Which commitment this control's OWN WORDS claim, or "" (#342).

    **The control's NAME, not its address.** `address_controls` appends up to
    44 characters of ROW text when duplicate labels need telling apart, so a
    plain `Wybierz` button is addressed `Wybierz — Zamówienie nr 123, dostawa
    jutro` — and a refusal read off that fires on the row's words rather than
    the button's. The owner's sentence is *a control whose own words say it
    buys*. The caller falls back to the address only when there is no name, and
    an unnamed control is addressed by its number, which says nothing.

    **A link that navigates is exempt**, on the structural ground `is_mutating`
    already stands on: an `<a>` with a real http href is a GET to another page,
    which is what `read_url` does unasked, so it cannot itself commit — the
    commit is a button on the destination. Measured, without this exemption
    `Allegro Pay`, `Kup ponownie`, `Remove all filters`, `Purchase insurance`
    and `Book now` all become dead ends while pressing nothing.

    Order matters only in that the narrowest true consequence is named first."""
    if navigates:
        return ""
    folded = fold(name)
    if not folded:
        return ""
    if _has(folded, _COMMIT_MONEY, "browse._COMMIT_MONEY", whole_words=True):
        return "money"
    if _has(
        folded, _COMMIT_SUBSCRIPTION, "browse._COMMIT_SUBSCRIPTION", whole_words=True
    ):
        return "subscription"
    if _has(folded, _COMMIT_BOOKING, "browse._COMMIT_BOOKING", whole_words=True):
        return "booking"
    if _has(folded, _COMMIT_CONTRACT, "browse._COMMIT_CONTRACT", whole_words=True):
        return "contract"
    if _has(
        folded, _END_CONTRACT_VERBS, "browse._END_CONTRACT_VERBS", whole_words=True
    ) and _has(folded, _CONTRACT_NOUNS, "browse._CONTRACT_NOUNS"):
        return "contract"
    if _has(folded, _COMMIT_DELETE, "browse._COMMIT_DELETE", whole_words=True):
        return "delete"
    return ""


def irreversible(text: str) -> str:
    """Which un-approvable consequence this text claims, or "".

    Reads a control's own words, or a field's, the same way the owner would.
    Order matters only in that the most specific phrases are tested first, so
    the message names the narrowest true thing."""
    folded = fold(text)
    if not folded:
        return ""
    if _has(folded, _CLOSE_ACCOUNT_PHRASES, "browse._CLOSE_ACCOUNT_PHRASES"):
        return "account"
    if not _has(folded, _NOT_AN_IDENTITY, "browse._NOT_AN_IDENTITY") and (
        _has(folded, _IDP_ALONE, "browse._IDP_ALONE")
        or (
            _has(folded, _IDP_VERBS, "browse._IDP_VERBS")
            and _has(folded, _IDP_PROVIDERS, "browse._IDP_PROVIDERS")
        )
    ):
        return "identity"
    if _has(folded, _CREDENTIAL_PHRASES, "browse._CREDENTIAL_PHRASES"):
        return "credential"
    if _has(folded, _CONTACT_PHRASES, "browse._CONTACT_PHRASES"):
        return "contact"
    if _has(folded, _CHANGE_VERBS, "browse._CHANGE_VERBS"):
        if _has(folded, _PAYOUT_NOUNS, "browse._PAYOUT_NOUNS"):
            return "payout"
        if _has(folded, _CONTACT_NOUNS, "browse._CONTACT_NOUNS"):
            return "contact"
    return ""


# An IBAN as a person writes one: two country letters, two check digits, then
# 11-30 more characters, spaced however the form allows. Anchored at word
# boundaries so an ordinary long token cannot be one.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]){11,30}\b", re.I)


def types_a_bank_account(value: str) -> bool:
    """Is this value a bank account number?

    The one value shape that is refused wherever it is typed, with no reference
    to what the field is called. Every other check here reads a LABEL, which a
    page writes and can therefore lie about; this one reads what aish is about
    to SEND, which the page cannot. There is no task where aish typing an
    account number into a form is the answer — a payment is made by him, and a
    payout address is exactly the thing an injection would want changed.
    """
    stripped = re.sub(r"[ -]", "", value or "")
    if not (15 <= len(stripped) <= 34):
        return False
    return bool(_IBAN_RE.fullmatch(stripped))


# A payment card as a form takes one: 13-19 digits, however the field lets them
# be spaced. ASCII only — `str.isdigit` is true of digits no keypad produces.
_CARD_RE = re.compile(r"[0-9]{13,19}")


def _checksums(digits: str) -> bool:
    """Luhn: the check every issued card number carries, and an arbitrary run
    of digits passes only one time in ten."""
    total = 0
    for i, char in enumerate(reversed(digits)):
        digit = int(char)
        if i % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def types_a_card_number(value: str) -> bool:
    """Is this value a payment card number?

    The second value refusal, and it stands on the same ground as the first:
    length and checksum are properties of what aish is about to SEND, so no
    field name, placeholder or page language is consulted and none can lie its
    way past. There is no task where aish typing his card number is the answer
    — paying is card-on-file inside a flow he ran, or his own hands on the page
    — so making it impossible also removes the temptation to teach aish to pay.

    An order or reference number can pass Luhn by chance, and that refusal is
    accepted rather than softened: it costs a step he finishes himself, in the
    only direction where being wrong never costs money.

    What this function guarantees about the digits is narrow, and stating it
    wider would be a claim the code cannot keep: the REFUSAL never carries
    them. It cannot promise they are nowhere, because the model put them in
    the call's own arguments and `_call_result` writes that record before
    `_dispatch` reaches any gate — so a refused value is already in the trace,
    by design, and that is where a reader looks for it.
    """
    stripped = re.sub(r"[ -]", "", value or "")
    if not _CARD_RE.fullmatch(stripped):
        return False
    return _checksums(stripped)


# The two value refusals, named so one dictionary of wordings can be keyed on
# them and the caller never re-tests the value to decide which message to show.
NO_BANK_ACCOUNT = "bank"
NO_CARD_NUMBER = "card"


def refuses_to_type(value: str) -> str:
    """Which value aish will not type this is, or "".

    **The single definition of the never-list, so it cannot hold for one tool
    and not the other (#310).** Both refusals lived on the batch path for as
    long as they existed, which meant `browse_fill` refused a card number and
    `browse_act(action="type")` typed it — the same value, on the same page,
    decided by which tool the model happened to reach for. That is the failure
    `docs/browser.md` already records against the site grant, and the fix is
    the same one: put the check on the ACT rather than on the tool doing it.
    `typed_values` is the other half — it is what finds the act.

    Nothing about the page is consulted, deliberately. Every other test here
    reads a LABEL, which the page writes and can therefore lie about; these
    two read what aish is about to SEND, which it cannot."""
    if types_a_bank_account(value):
        return NO_BANK_ACCOUNT
    if types_a_card_number(value):
        return NO_CARD_NUMBER
    return ""


# Words that are chrome as often as they are commitments. A date picker's
# "Confirm" and a wizard's "Accept" are these; so is the button that ends a
# purchase, which is why they are demoted only INSIDE a widget the page has
# just opened and never on a control that submits a form.
CHROME_WORDS = vocab.declare(
    "browse.CHROME_WORDS",
    languages="Polish + English",
    on_miss=vocab.FRICTION,
    structural="`submits` and `in_widget` — the demotion cannot reach a control "
    "that submits a form",
    note="A miss means a date picker's Confirm keeps its card. Costs a prompt, "
    "never a consequence.",
    entries=(
    "confirm", "potwierdź", "potwierdz", "accept", "akceptuj", "zaakceptuj",
))


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
    # What tells this control's row apart from the rows around it, one line per
    # difference. Empty unless the control is one of several saying the same
    # thing inside a repeated structure — see `digestRows` in CONTROLS_JS.
    row: list[str] = field(default_factory=list)
    # Which form this control belongs to, "" for one that belongs to none. The
    # identity is opaque and only ever compared: it exists so a card about to
    # submit a form can say what that form holds.
    form: str = ""
    # Did the NAME say this commits something? `mutating` is the union of that
    # and "submits a form"; the gate needs them apart, because only one of the
    # two is something the owner can sensibly grant a whole site at once.
    worded: bool = False
    # An entry in a list the page opened (`role=option`), which is the only
    # thing a batch step may press sight-unseen. See `plan_batch`.
    option: bool = False
    # Does pressing this post the form it is in? Carried rather than re-derived
    # because it is what the page is told to say about HOW TO SUBMIT: once a
    # form-fill reports only what changed, the submit button stops being
    # re-listed on every step, and a model that cannot see it starts hunting.
    submits: bool = False
    # What the model asks for this control BY — its words, not its position.
    # `n` stays the tag that finds the element; this is the address, and
    # `address_controls` is where it is worked out. Empty only until then.
    address: str = ""
    # Does pressing this just GO somewhere — a real http href to another page?
    # Carried rather than re-derived from `kind`/`detail` because `<a href="#">`
    # is a JavaScript button wearing a link's clothes, and only the enumeration
    # (`goesElsewhere`) can tell the two apart. It is what exempts a nav link
    # from the commit refusal: a GET to another page is what `read_url` does
    # unasked, so it cannot itself be the commit.
    navigates: bool = False

    def row_note(self) -> str:
        """The row this control sits in, bounded — and saying what it left out.

        A results row can be a paragraph. The cap is what keeps twenty of them
        from costing more than the page they are on, and the count is what
        stops a cut from reading like a row that had nothing more in it: the
        model narrows (`match`) or reads the page whole to see the rest."""
        if not self.row:
            return ""
        shown: list[str] = []
        spent = 0
        for line in self.row:
            if spent + len(line) > ROW_MAX_CHARS and shown:
                break
            shown.append(line)
            spent += len(line) + 3
        left = len(self.row) - len(shown)
        said = " | ".join(shown)[:ROW_MAX_CHARS]
        return said + (f" | +{left} more" if left else "")

    def line(self) -> str:
        """One line, as the model reads it."""
        bits = f"{self.kind} {(self.address or self.name)!r}"
        if self.detail:
            bits += f" → {self.detail}"
        if said := self.row_note():
            bits += f" — in: {said}"
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


# Why a snapshot carries no evidence frame (#289). A closed vocabulary, because
# it is written into a trace record and read back by two renderers: a free-text
# reason would drift between them, and both of them would be quoting a sentence
# nobody could search for.
#
# There are exactly four, and each is a different fact about the same absence.
# TWO OF THEM ARE NO LONGER WRITTEN (#320) and are kept anyway: they are in
# logs already on disk, and a reader that cannot render a word an older writer
# emitted turns a recorded fact into a blank. Retiring a vocabulary entry means
# retiring the WRITER, never the reading of it.

NO_FRAME_HANDS = "hands"  # the owner's own hands were on the browser
NO_FRAME_FAILED = "failed"  # the capture did not produce a stored picture

# READ-ONLY since #320. The page was showing a password box, which used to be
# refused on the grounds that a screenshot of a login form is the artifact that
# must not exist — but aish never types a password on the browse path, so the
# refused frame was an EMPTY form, and the refusal cost the one picture the
# owner most needed when a sign-in failed.
NO_FRAME_PASSWORD = "password"
# READ-ONLY since #320. aish could not tell whether it did. This existed only
# to resolve the question above safely; with that question gone it had no job
# of its own, and a branch that looks load-bearing and is not is worse than no
# branch. Still kept apart from `failed` when reading an older log: a capture
# that broke and a page that would not say route to different repairs.
NO_FRAME_UNKNOWN = "unknown"

NO_FRAME_REASONS = frozenset(
    {NO_FRAME_PASSWORD, NO_FRAME_UNKNOWN, NO_FRAME_HANDS, NO_FRAME_FAILED}
)
# What a snapshot may still write today. Anything outside this is a reader's
# concern only, and `test_only_two_reasons_are_still_written` pins the line.
NO_FRAME_WRITTEN = frozenset({NO_FRAME_HANDS, NO_FRAME_FAILED})


# ---------------------------------------------------------------- the console
#
# What the PAGE said while an action was being carried out. It is the piece of
# evidence a day of failed diagnosis on eon.pl did not have: four causes were
# argued confidently off page text, a badge and a fetched copy of the HTML, and
# all four were wrong, while the one sentence that would have settled it — a
# handler throwing — went to a console nobody records. This is not sign-in
# specific: the same silence is what makes "the calendar would not take the
# date" and "the dropdown never opened" unanswerable.
#
# EVIDENCE, NEVER A VERDICT, and the site this was built for is the proof. A
# press that never LANDS writes nothing here at all, because nothing ran — that
# failure's witness is the DRIVER and not the page, and it is `Cover` below,
# on a channel of its own. And eon.pl's own bundle throws a real TypeError
# that is not why its sign-in fails, so a true line here can point at the
# wrong thing. `docs/browser.md`.
#
# A RECORD, and a record is detection and never protection (#295 P2). Nothing
# in the browse gate, the act-time re-resolution fence, the irreversible
# refusals or the site grant is loosened, widened or checked less carefully
# because the console is now written down — and "he could have read it
# afterwards" is the same violation in the past tense.
#
# ERRORS AND WARNINGS ONLY, plus uncaught exceptions. `console.log` is the
# page's own chatter: a busy SPA writes thousands of lines of it a minute and
# none of them is a reason something did not happen. An uncaught exception is
# a different Playwright event (`pageerror`) from a console call, and it is the
# one that matters most here — a handler that threw wrote nothing to `console`
# at all.
CONSOLE_ERROR = "error"
CONSOLE_WARNING = "warning"
CONSOLE_UNCAUGHT = "uncaught"
# A closed vocabulary for the same reason `NO_FRAME_*` is one: these words are
# written into a trace record and read back by two renderers, and free text
# would drift between them.
CONSOLE_LEVELS = frozenset({CONSOLE_ERROR, CONSOLE_WARNING, CONSOLE_UNCAUGHT})
# What Chrome and Playwright call the levels this keeps, mapped onto the words
# above: `warn` and `warning` are one level spelled two ways depending on which
# end of the DevTools protocol you read it from.
_CONSOLE_ALIASES = {"warn": CONSOLE_WARNING, "error": CONSOLE_ERROR}

# Bounded twice, because a page in a render loop produces both a great many
# lines and very long ones, and either alone would let one page fill a turn.
# Named constants rather than literals so a record can say which cap cut
# (trace contract §8.5).
CONSOLE_MAX_MESSAGES = 20
CONSOLE_MESSAGE_CHARS = 200


class ConsoleLog:
    """What one page said to its own console during ONE action.

    Cleared when a browse call begins and drained into the snapshot that call
    produces, so a message is tied to the press that produced it rather than
    floating loose in the session. That binding is the whole point: "something
    threw at some point today" is not evidence, "that click threw this" is.

    Bounded, and never silently: past the cap the count of unkept messages is
    stated, the way `MAX_CONTROLS` states what it left out. A page that could
    quietly drop the line naming its own failure would be the one page where
    this record is worthless.

    Deliberately holds no page reference and does no I/O — it is the half of
    the capture that needs no browser, so the caps and the vocabulary are
    testable with no Chrome anywhere near them."""

    __slots__ = ("lines", "dropped")

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.dropped = 0

    def begin(self) -> None:
        """A new action starts. Whatever the last one provoked is not this
        one's evidence, and carrying it over would attribute a page's noise to
        the press that merely happened to come after it."""
        self.lines = []
        self.dropped = 0

    def note(self, level: str, text: str) -> None:
        """One message, if it is a level worth keeping.

        Never raises and never judges: this runs from a Playwright event
        handler on the owner loop, where an exception is one nothing is
        waiting for, and the text is copied down rather than interpreted."""
        word = (level or "").strip().lower()
        word = _CONSOLE_ALIASES.get(word, word)
        if word not in CONSOLE_LEVELS:
            return
        message = " ".join((text or "").split())
        if not message:
            return
        if len(self.lines) >= CONSOLE_MAX_MESSAGES:
            self.dropped += 1
            return
        if len(message) > CONSOLE_MESSAGE_CHARS:
            message = message[:CONSOLE_MESSAGE_CHARS] + "…"
        self.lines.append(f"{word}: {message}")

    def drain(self) -> list[str]:
        """This action's messages, plus the count of any the cap refused.

        Draining rather than reading is what stops one action's console being
        reported twice: a snapshot is taken once per call, and the next call
        starts from empty however it got there."""
        lines = list(self.lines)
        if self.dropped:
            lines.append(
                f"[{self.dropped} more console message(s) not kept — the cap is "
                f"{CONSOLE_MAX_MESSAGES}]"
            )
        self.begin()
        return lines


# ------------------------------------------------- a control something covers
#
# A control can be listed, visible, enabled and reachable, and still be
# unpressable because something is lying ON TOP of it. That is not a timeout
# and it is not "a control that would not take the action": it is a different
# failure with a different repair, and it is the one aish could already see
# and threw away.
#
# eon.pl is the specimen (#321). Its cookie banner covers the login button, so
# the click lands on the banner, the button's own `onclick` never runs, nothing
# is submitted — and NOTHING anywhere says why. There is no console line,
# because no handler ran; there is no error, because as far as the page is
# concerned a click happened. Four confident diagnoses were argued on top of
# that silence over a full day and all four were wrong.
#
# THE WITNESS IS THE DRIVER, NOT THE PAGE, and it already exists twice over:
# Playwright's actionability log names the intercepting element, and
# `browser._COVERED_JS` computes the same fact in aish's own code by asking
# what is at the control's own centre point. Keeping the NAME rather than
# reducing it to a bool is what makes it reportable — and it needs no
# vocabulary at all, so it covers every consent wall, modal, cookie bar and
# sticky footer that will ever exist, in any language. That is the structural
# check #295 P4 asks for, and `CONSENT_SELECTORS` is the floor under it rather
# than the mechanism.
#
# WHAT IT CANNOT SEE, stated here because the silence it fixes has the same
# shape: a press that LANDS on the right element and is then ignored — a
# handler that is missing, broken, or returns early — produces no interception
# and no console line either. An absent cover is not evidence that the press
# worked, and nothing that reads this may imply it is.
COVERED_NAME_CHARS = 60


def covering_name(raw: str) -> str:
    """The covering element in the PAGE's own words — id, class or tag.

    PAGE-AUTHORED, exactly as a control's name is, so wherever aish says it
    aloud it is quoted and attributed to the page. Collapsed and bounded HERE
    rather than trusted to the snippet that read it, because the value crosses
    into a trace record and one bound in one place is one thing to be wrong."""
    return " ".join((raw or "").split())[:COVERED_NAME_CHARS]


@dataclass
class Cover:
    """What was found sitting on top of a control aish tried to press.

    `by` empty means nothing was over it, which is the ordinary case and costs
    one `evaluate` on the rung where a real click already failed."""

    by: str = ""
    # The consent list recognised it AND the control came clear afterwards.
    # Two different facts folded deliberately: a banner dismissed that leaves
    # the control still covered is not a dismissal for any purpose here.
    dismissed: bool = False

    def record(self) -> dict:
        """The trace block, or `{}` when nothing covered anything.

        Absent rather than empty, for the reason `signin` is: a step saying
        "nothing covered this" and a step written before any of this existed
        are different facts (trace contract corollary 2)."""
        if not self.by:
            return {}
        return {"by": self.by, "dismissed": self.dismissed}


@dataclass
class Pressed:
    """How a press went: aish's note about the GESTURE, and what was in the way.

    The note has always been the model's; the cover is the owner's, and it
    travels separately because it goes somewhere else — onto the trace, where
    a structural signal only the acting model sees is one restart from being
    lost."""

    note: str = ""
    cover: Cover = field(default_factory=Cover)


# What aish says when a cover was found and the consent list took it down. The
# element is named even on the happy path, because "something was covering it"
# without saying what is the sentence that taught nobody anything for a year.
COVERED_DISMISSED = (
    "something the page calls {by!r} was covering it, so aish dismissed that "
    "and pressed it"
)
# ...and when it could not. This is the ending eon.pl produces, and it is the
# whole point: it names the element, it says why the press achieved nothing,
# and it gives the one instruction that works.
COVERED_STUCK = (
    "aish could not {action} {address!r}: something the page calls {by!r} is "
    "sitting on top of it, so a click lands on THAT and never reaches the "
    "control — and aish could not take it down. Press whatever closes it (a "
    "cookie or consent banner, a dialog, a sticky bar) and try again."
)
# The same ending with the cover ruled OUT, and the wording is narrow on
# purpose. The old sentence guessed — "Something may be covering it" — on
# every stuck control, which is how a real cover and an inert control read
# identically. This one is a statement about what aish looked for and did not
# find, and it explicitly does not claim to know what IS wrong.
STUCK_NOT_COVERED = (
    "aish could not {action} {address!r} — it is on the page and would not "
    "take the action, by click, by keyboard, or otherwise. Nothing was found "
    "covering it, so something being in the way is not the reason: the control "
    "may be inert, or its own handler may be broken. Try another route to the "
    "same thing."
)


class ConsentTally:
    """How often the consent list was handed a KNOWN obstruction, and how often
    it cleared it.

    #295 P4: where a vocabulary is unavoidable it is a floor under a structural
    check, grown only from measurement, and **it ships with counters**.
    `browser._CONSENT_SELECTORS` shipped with neither for a year. It holds
    `button:has-text('Akceptuj wszystkie')`; eon.pl's button says *Akceptuję
    wszystkie cookies*, and `has-text` is a substring match — so the list
    missed by a single letter, the `ę`, in the language the owner browses in.
    The banner stayed, it covered the login button, and the only symptom was
    that a site he uses weekly quietly stopped working (#321).

    A miss here is the failure shape #322 names as the one nobody looks for: it
    neither permits something (fail-open) nor costs friction (fail-closed) — it
    silently breaks a feature. So it is counted, and the count goes on the
    owner's own door, where a list that has stopped matching reads as a number
    instead of as nothing at all.

    **It counts OBSTRUCTIONS, never pages**, and that is what makes the number
    mean something. `_dismiss_consent` also runs speculatively on every page
    that opens, where no banner is the overwhelmingly common case; counting
    there would put the miss rate at ~100% forever and say nothing about the
    list. What is counted is the case the STRUCTURAL check has already found
    something covering a control — the list handed a known banner and asked to
    take it down. Zero cleared out of several is a list that has stopped
    matching the web it is pointed at.

    Process-lifetime and deliberately not persisted, and NOTHING reads it to
    decide anything: it is an instrument for noticing, never an input. A record
    is detection and never protection (#295 P2), and a counter even less so."""

    __slots__ = ("asked", "dismissed")

    def __init__(self) -> None:
        self.asked = 0
        self.dismissed = 0

    def note(self, *, dismissed: bool) -> None:
        """One control found covered, and whether the list cleared it."""
        self.asked += 1
        self.dismissed += int(bool(dismissed))
        # The same single event, also persisted (#322). This tally is the LIVE
        # line on `/browser` and is process-lifetime by design; the catalogue
        # needs the same consultation to survive the process. One writer still
        # — the caller writes here and nowhere else.
        vocab.note("browser._CONSENT_SELECTORS", matched=dismissed)

    @property
    def missed(self) -> int:
        """Obstructions the list could not take down."""
        return self.asked - self.dismissed

    def line(self) -> str:
        """One line for `/browser`, or "" when nothing has obstructed anything.

        Silent until then, for the reason an empty console grows no heading: a
        status screen that always carries a row saying nothing happened is one
        the eye stops reading."""
        if not self.asked:
            return ""
        said = (
            f"consent list:  cleared {self.dismissed} of {self.asked} covered "
            "control(s) this session"
        )
        if self.missed:
            said += f" — {self.missed} it could not match"
        return said


# One tally for the process, beside the one list it counts.
CONSENT_TALLY = ConsentTally()


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
    # The topic the control list was narrowed WITH, and how many controls
    # matched it. Both are needed to say something true about the list: "12 of
    # 2678 controls match 'Interstellar'" is an answer, and the same list with
    # no topic on it is a page the model has barely seen.
    narrowed: str = ""
    matching: int = 0
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
    # Is this page asking for a PASSWORD? The read path has carried this since
    # #236; the driving path did not, so a portal that had signed the owner out
    # arrived as an ordinary page with a couple of odd buttons on it and the
    # model went looking for a door — measured on linkedin.com, where it
    # pressed Sign in, then "Continue with Google", then "Continue as Sage",
    # and was stopped by the owner. A page cannot be driven usefully until
    # somebody signs in, and saying so is what stops the guessing.
    signin: bool = False
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
    # What on THIS page says it commits something — a card field, a payment
    # provider's frame, a checkout address, a price inside the form being
    # submitted. Read only in the escalating direction: while it is set, every
    # form submit draws its own card again.
    commit_evidence: str = ""
    # Where the model AIMED, when that is not where it landed. A site that
    # redirects is not an error and needs no refusal — but the model asked for
    # qatarairways.com/en-pl/help/feedback.retrieve.html, was silently handed
    # /en-pl/help.html, and spent the rest of the turn reasoning about a form
    # that was never on the page in front of it (#247).
    asked: str = ""
    # What a BATCH did, step by step, in aish's own words — the value each
    # control HOLDS on readback, not the value that was asked for. A mask, an
    # autocomplete rewrite or a maxlength truncation lands silently otherwise,
    # and the page delta cannot report any of it: a suggestion list opens and
    # closes between two snapshots and nets to zero in the diff.
    ledger: list[str] = field(default_factory=list)
    # The evidence frame (#289): a stored picture of what this page LOOKED LIKE
    # at the moment the model was shown it. A path into the evidence-frame
    # store, or "" — the model never receives either; it is written down for
    # the owner, who otherwise has no way to check a page aish drove and he
    # cannot see. That store sits outside every workspace root (#318), so the
    # model cannot reach the bytes even if it learns the path.
    frame: str = ""
    # What the PAGE wrote to its own console while this action was carried out
    # — errors, warnings and uncaught exceptions, bounded, in the page's own
    # words. Empty is the ordinary case and costs nothing anywhere: a healthy
    # action grows no section and says nothing.
    #
    # PAGE-AUTHORED, and therefore outside content exactly like the page text
    # beside it: presented to the model INSIDE the untrusted banner and never
    # in aish's own voice. A page that can write a sentence into a warning has
    # written it into the document.
    console: list[str] = field(default_factory=list)
    # What was found SITTING ON TOP of the control this action pressed, when
    # anything was (#321). aish's OWN observation and not the page's account of
    # itself — the driver asked what is at the control's centre point and the
    # page answered with an element — so unlike `console` it is spoken above
    # the untrusted banner, with the element's own name quoted and attributed
    # the way a control's name already is.
    #
    # It rides the snapshot so it can reach the TRACE. A press that never
    # landed is exactly the failure nobody can reconstruct afterwards, and a
    # structural signal only the acting model sees is one restart from being
    # lost — which is how a day went into four wrong diagnoses of a fact Chrome
    # had already computed and named.
    covered: Cover = field(default_factory=Cover)
    # Why there is no frame, when there is none. Absence must never be the
    # evidence (trace contract corollary 2): "no picture" because a password
    # box was on the page, "no picture" because the page would not say, and
    # "no picture" because the capture failed route to three different
    # repairs, and only the last of them is a fault.
    frame_skipped: str = ""

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


# The one method HTTP itself calls SAFE. Everything else a form can be sent
# with, and the absence of any statement at all, is treated as a commit.
QUERY_METHOD = "get"


def is_worded(name: str) -> bool:
    """Does this control's NAME say, in the owner's web's own vocabulary, that
    pressing it commits something?

    Split out from `is_mutating` because the GATE needs the two reasons apart:
    a name that says "Zapłać" draws a card whatever else is true, while a
    nondescript form submit is something the owner can grant a site once."""
    lowered = f" {name.lower()} "
    return vocab.hit("browse._MUTATING_WORDS", _MUTATING_WORDS, lowered)


def _only_chrome(name: str) -> bool:
    """Everything this name matched is a word that is chrome as often as it is
    a commitment — so the name says nothing on its own."""
    lowered = f" {name.lower()} "
    hits = [word for word in _MUTATING_WORDS if word in lowered]
    vocab.note("browse._MUTATING_WORDS", matched=bool(hits))
    if not hits:
        return False
    only = all(word in CHROME_WORDS for word in hits)
    # The candidate count here is real and free: how many mutating words this
    # name matched is exactly what the demotion is choosing among.
    vocab.note("browse.CHROME_WORDS", matched=only, candidates=len(hits))
    return only


def says_it_commits(
    name: str,
    *,
    submits: bool = False,
    in_widget: bool = False,
    navigates: bool = False,
) -> bool:
    """Does this control's NAME commit something, given where it sits?

    ONE place decides this, because two things read it and they must not
    disagree: `is_mutating` (which is also the fence on what a batch may press
    sight-unseen) and the GATE (which uses it to decide what the driving grant
    cannot cover). The date picker's "Confirm" is demoted here — scoped to a
    widget the page opened, and never to something that submits a form, so the
    demotion cannot reach the button that ends a purchase.

    **A link that goes somewhere is not a press that commits (#295 M2).**
    `commits()` has exempted navigation since #342, on the ground `is_mutating`
    had already stood on for longer: an `<a>` with a real http href is a GET to
    another page, which is what `read_url` does unasked, so it cannot itself be
    the commit — the commit is a button on the destination. This function never
    consulted it, and that asymmetry is what carded `Faktury i płatności` three
    times in one week: the Polish word for *payment* is in its name, it
    navigates, `is_mutating` called it harmless on exactly that ground, and the
    worded half carded it anyway. A card that fires on reading an invoice list
    is the false positive that teaches the tap waiting on the purchase.

    **From the real destination, never from `kind == "link"`.** Measured in the
    owner's corpus, `Faktury i płatności` appears 17 times WITH a destination
    and 9 times without. An `<a href="#">` is a JavaScript button wearing a
    link's clothes and it keeps its card; only the enumeration's own
    `goesElsewhere` signal tells the two apart, which is why `Control.navigates`
    is carried rather than re-derived from the kind.

    **A control that submits a form is never exempted by this**, whatever else
    it claims to be — the same fence `_only_chrome` sits behind, and for the
    same reason: the nondescript button that posts the form is the dangerous
    one, and a page is free to describe it however it likes.

    `is_mutating` reaches the same answer through its own earlier `navigates`
    return, which is deliberately left where it is: removing it would let a
    navigating control that ALSO posts a form become mutating, which it is not
    today."""
    if navigates and not submits:
        return False
    if in_widget and not submits and _only_chrome(name):
        return False
    return is_worded(name)


def is_mutating(
    name: str,
    kind: str,
    *,
    submits: bool = False,
    navigates: bool = False,
    method: str = "",
    in_widget: bool = False,
) -> bool:
    """Would pressing this change something the owner would mind?

    Word-matched on the control's NAME, plus every form submit that COMMITS
    anything. Both halves matter: a button called "Zapłać" is obvious, and a
    nondescript "Dalej" that posts a form is the one that quietly does
    something.

    **A plain navigation is never mutating, whatever it is called.** An `<a>`
    with a real http href is a GET to another page — precisely what `read_url`
    does under auto-approval — so gating it would ask the owner's permission to
    read a page he already lets aish read, and the first thing the word list did
    was flag the link named "Faktury i płatności" because it contains the word
    for *payment*. What makes it safe is that it navigates, not that it is an
    anchor: an `<a href="#">` is a JavaScript control wearing a link's clothes,
    and it is word-matched like the button it is.

    **A GET form submit IS that navigation, and gating it was an
    inconsistency.** It is a link with the query typed into it: aish follows
    `?from=WAW&to=CDG` as an anchor without asking, then asked permission to
    press the button that builds the same URL. Nothing about a search changes
    state, so the card fired on nothing — and a card that fires on nothing is a
    SAFETY cost, not an inconvenience: it trains the owner to tap through, and
    the tap he learns is the one waiting on the purchase. The gate is worth
    exactly as much as its false-positive rate is low.

    Narrowed by HTTP's own definition of safe rather than by a guess, and only
    on an EXPLICIT `method="get"`. A form nobody wrote a method on is not a
    statement that it is safe — on an SPA it usually means JavaScript intercepts
    and posts — so absence stays gated. The word list still runs either way: a
    GET form whose button says "Usuń" is caught by its name."""
    if kind in (FIELD, PASSWORD):
        # Typing changes nothing until something is pressed. Gating the keystroke
        # would ask twice for one act and train the owner to tap through.
        return False
    if navigates:
        return False
    if submits and method.strip().lower() != QUERY_METHOD:
        return True
    # Measured: of the five cards one flight search drew, the only word from
    # the list that fired at all was the date picker's "Confirm".
    return says_it_commits(name, submits=submits, in_widget=in_widget)


# What a control that is supposed to produce a FILE is called, and what its
# destination looks like. Polish first, like the mutating list, for the same
# reason: this is the owner's web.
_DOWNLOAD_WORDS = vocab.declare(
    "browse._DOWNLOAD_WORDS",
    demanded=False,  # asked of every link on every page; no downloads is the
    # correct answer, not a defect
    languages="Polish + English",
    on_miss=vocab.BREAKS,
    structural="the `/download` and `/export` path test beside it, and the fact "
    "that no file arrived — which is what gates the consultation at all",
    note="Advisory only: a miss drops one sentence telling the model an export "
    "may be preparing in the background. #271 is what that silence cost.",
    entries=(
    "pobierz", "pobieranie", "ściągnij", "sciagnij", "zapisz", "eksport",
    "eksportuj", "wyeksportuj", "download", "export", "save as", "get csv",
    "get pdf",
))
_DOWNLOAD_SUFFIXES = vocab.declare(
    "browse._DOWNLOAD_SUFFIXES",
    demanded=False,  # as `_DOWNLOAD_WORDS` — asked of every URL seen
    languages="file suffixes — locale-invariant",
    on_miss=vocab.BREAKS,
    entries=(
    ".csv", ".pdf", ".xls", ".xlsx", ".zip", ".json", ".txt", ".ics", ".xml",
))


def wants_download(name: str, href: str = "") -> bool:
    """Does pressing this READ AS an attempt to obtain a file?

    Only ever consulted when no file arrived, and it only decides whether to
    add a sentence — so a false positive costs an advisory nobody needed, and a
    false negative costs the failure this exists for (#271): a site that
    PREPARES an export asynchronously is indistinguishable, to the model, from
    a download button that is broken. It abandoned IMDb's own Export and went
    off to write a scraper that a WAF then refused."""
    lowered = f" {(name or '').lower()} "
    if vocab.hit("browse._DOWNLOAD_WORDS", _DOWNLOAD_WORDS, lowered):
        return True
    path = (href or "").lower().split("?", 1)[0].split("#", 1)[0]
    by_suffix = any(path.endswith(suffix) for suffix in _DOWNLOAD_SUFFIXES)
    vocab.note("browse._DOWNLOAD_SUFFIXES", matched=by_suffix)
    if by_suffix:
        return True
    return "/download" in path or "/export" in path


# The sentence for the case above. It states the three things the model cannot
# work out for itself — that nothing arrived, that this is not proof the press
# failed, and what to do instead of assuming it did.
NO_FILE_YET = (
    "no file arrived from that press. That is NOT proof it failed: a site that "
    "prepares an export or a document does it in the background and publishes "
    "it later — on a downloads/exports page of its own, or by email. Look for "
    "where it will appear, or press it again only if the page says nothing is "
    "pending. Do NOT fall back to scraping the page for what the file holds "
    "without saying that is what you are doing."
)


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
# Look through shadow roots, not just past them. ONE definition, for every
# snippet in aish that reads a page.
#
# `document.querySelector`, `getElementById`, `activeElement`, `closest` and
# `contains` all stop at a shadow boundary, and a growing share of the web puts
# its whole application inside one — qatarairways.com's booking widget is an
# Angular app under `<app-nbx-explore>`. Enumeration walked shadow roots and
# the calendar reader, the label lookup and the focus test did not (#273),
# which is the worst way for a boundary to be handled: consistently blind would
# have been noticed years ago, whereas visible-then-invisible reads as "this
# page has no date cells" on a page whose cells aish had itself just tagged.
#
# It is a shared constant rather than a local of `REACH_JS` because the
# snippets that needed it most were the ones that did NOT include `REACH_JS` —
# the sign-in form reader, the 2FA probe, the option-flood stripper. A helper
# only the already-correct code can reach fixes nothing.
#
# `descend`, not `walk`: CONTROLS_JS has its own walk, and
# TestHidingAControlNeverRoutesAroundItsCard proves that walk tags nothing by
# slicing its source between two literals naming it. Any earlier occurrence of
# those literals — a second helper, or even a comment quoting them — silently
# moves the slice, and the proof then reads the wrong code.
DEEP_JS = """
  const deepAll = (selector, root) => {
    const out = [];
    const descend = (where) => {
      for (const el of where.querySelectorAll(selector)) out.push(el);
      for (const el of where.querySelectorAll('*')) {
        if (el.shadowRoot) descend(el.shadowRoot);
      }
    };
    descend(root || document);
    return out;
  };
  const deepOne = (selector, root) => deepAll(selector, root)[0] || null;
  const deepById = (id) => {
    try { return deepOne('[id="' + CSS.escape(id) + '"]'); } catch (e) { return null; }
  };
"""

REACH_JS = DEEP_JS + """
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

# The words for a control that has none of its own, shared VERBATIM by the page
# enumeration and by the picker reader — the same reason `REACH_JS` is shared:
# two scripts that name the same element differently disagree about what is
# there. Measured on wizzair.com, where the picker's own scan had a naive name
# extraction of its own: the ‹ › month arrows are neither buttons nor links and
# carry no text, so they were named "" and DROPPED, and a date three months out
# failed with "aish cannot find its next-month arrow" while the arrows sat
# plainly on screen.
NAME_JS = r"""
  const NAME_MAX = (opts && opts.nameMax) || 120;
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, NAME_MAX);

  const labelElement = (el) => {
    if (el.id) {
      try {
        // The control's OWN root, not the document: a label for a control
        // inside a shadow root lives in that shadow root, and looking in the
        // document finds nothing — which is how a labelled passenger picker
        // came back named 'mat-input-6' (#273).
        const lab = el.getRootNode()
          .querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab) return lab;
      } catch (e) { /* an id CSS cannot escape simply has no label */ }
    }
    return (el.closest && el.closest('label')) || null;
  };

  const labelFor = (el) => {
    const by = el.getAttribute && el.getAttribute('aria-labelledby');
    if (by) {
      const root = el.getRootNode();
      const byId = (id) => {
        try { return root.querySelector('[id="' + CSS.escape(id) + '"]'); }
        catch (e) { return null; }
      };
      const parts = by.split(/\s+/)
        .map(byId)
        .filter(Boolean)
        .map((n) => n.innerText || n.textContent || '');
      if (parts.length) return parts.join(' ');
    }
    const lab = labelElement(el);
    if (lab) return lab.innerText || lab.textContent || '';
    return '';
  };

  // Only for things that are unmistakably controls. A nameless <div onclick>
  // described by its neighbour would fill the list with furniture.
  const NEAR = 'a[href], button, input, [role=button], [role=link], summary';
  const nearbyWords = (el) => {
    if (!el.matches || !el.matches(NEAR)) return '';
    let at = el;
    for (let up = 0; up < 3 && at; up += 1) {
      at = at.parentElement;
      if (!at) break;
      const text = clean(at.innerText || at.textContent || '');
      if (text) return 'near ' + JSON.stringify(text.slice(0, 40));
    }
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
    if (text && /[\p{L}\p{N}]/u.test(text)) return text;
    // What is left is a picture drawn with a character. KEEP IT — it renders in
    // a terminal, it stores in the log, and it is what the owner would point at
    // — and say what it does beside it, because '×' names the button only to
    // someone looking at it. Neither half alone is the control's name.
    const said = clean(el.getAttribute('title') || el.value || '');
    const meaning = GLYPHS[text] || said || iconWords(el) || nearbyWords(el);
    if (text && meaning) return text + ' (' + meaning + ')';
    if (text) return text;
    return meaning;
  };

// An icon-only button is not a nameless button — it is a button whose name is
// a picture, and a person reads it fine. Dropping it (the old rule: no words
// and nowhere to go, so skip) took the swap-airports arrow, the hamburger and
// every dialog's X off the list entirely, which on a booking form is half the
// controls that matter. So: ask the page what it calls its own picture,
// several ways, and describe it in words the model can ask for it by.
  const GLYPHS = {
    '×': 'close', '✕': 'close', '✖': 'close', '╳': 'close',
    '☰': 'menu', '⋮': 'more', '⋯': 'more',
    '⇄': 'swap', '⇆': 'swap', '↔': 'swap', '⟷': 'swap',
    '←': 'back', '→': 'forward', '↑': 'up', '↓': 'down',
    '▾': 'expand', '⌄': 'expand', '▴': 'collapse', '⌃': 'collapse',
    '⚙': 'settings', '🔍': 'search', '＋': 'add', '+': 'add',
    '−': 'remove', '–': 'remove', '☆': 'favourite', '★': 'favourite',
  };
// 'icon-swap-airports' and 'swap_horiz' are both the page telling you what the
// picture is; they are just written for a stylesheet rather than for a person.
  const words = (s) => clean((s || '')
    .split(/[#\/]/).pop()
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_\-.]+/g, ' ')
    .replace(/\b(icon|ico|fa|fas|far|glyph|svg|symbol|btn|button)\b/gi, ' ')
    .toLowerCase());

  const iconWords = (el) => {
    const svg = el.querySelector && el.querySelector('svg');
    if (svg) {
      const t = svg.querySelector('title');
      const said = clean((t && (t.textContent || '')) || svg.getAttribute('aria-label') || '');
      if (said) return said;
      const use = svg.querySelector('use');
      const ref = use && (use.getAttribute('href') || use.getAttribute('xlink:href') || '');
      if (ref) { const w = words(ref); if (w) return w; }
    }
    const img = el.querySelector && el.querySelector('img[alt]');
    if (img) { const alt = clean(img.getAttribute('alt')); if (alt) return alt; }
    for (const attr of ['data-icon', 'data-testid', 'data-test', 'name']) {
      const w = words(el.getAttribute && el.getAttribute(attr));
      if (w) return w;
    }
    const marked = (el.matches && el.matches('[class*=icon], [class*=ico-]')) ? el
      : (el.querySelector && el.querySelector('[class*=icon], [class*=ico-]'));
    if (marked) {
      for (const token of (marked.getAttribute('class') || '').split(/\s+/)) {
        if (!/icon|ico-/i.test(token)) continue;
        const w = words(token);
        if (w && w.length > 2) return w;
      }
    }
    return '';
  };
"""


# Enumerate, tag, and describe. Runs in the page, walks open shadow roots the
# same way `_LINKS_JS` does, and reports both what the cap left out and what the
# page is currently hiding rather than quietly stopping.
CONTROLS_JS = "(opts) => {" + REACH_JS + NAME_JS + r"""
  const SEL = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=menuitem]', '[role=tab]',
    '[role=checkbox]', '[role=switch]', '[role=combobox]', '[onclick]',
    '[contenteditable]', '[contenteditable=true]',
    // The suggestion a search box drops down. Without this the list a site
    // opens under "Paris" is TEXT and nothing pressable, so the field can be
    // typed into and never committed — which is the lot.pl destination box in
    // the session that filed #251, unfinishable by any sequence of calls. A
    // CLOSED list is `unreachable` and drops out on its own, so this adds the
    // open one only.
    '[role=option]', '[role=treeitem]',
  ].join(', ');
  const out = [];
  // Every control that could be listed, in document order, BEFORE the budget
  // is spent. Collected first so `opts.match` can decide which ones the budget
  // buys: the cap used to run inside the walk, so a control past it was never
  // tagged and could never be acted on afterwards — and on a 250-row list that
  // put every row after the ninth permanently out of reach (#270).
  const found = [];
  let matched = 0;
  let unreached = 0;
  const seen = new Set();

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

  const kindOf = (el, type) => {
    const tag = el.tagName;
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (tag === 'SELECT') return 'choice';
    // A `role=combobox` on an INPUT is a search box wearing a dropdown's
    // clothes: you type into it, and calling it a choice hid the one thing a
    // form-fill has to report — `detailOf` gives a choice its option list, so
    // the field's own VALUE was never read back and a destination that had
    // been committed still read "type to search".
    if (role === 'combobox') {
      return (tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable)
        ? 'field' : 'choice';
    }
    if (tag === 'TEXTAREA') return 'field';
    if (tag === 'INPUT') {
      if (type === 'password') return 'password';
      if (type === 'checkbox' || type === 'radio') return 'check';
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      return 'field';
    }
    if (role === 'option' || role === 'treeitem') return 'button';
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

  const emit = (el, tagOn, kind, name, href, row) => {
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
      // Is this an entry in a list the page opened, rather than furniture that
      // happened to appear at the same moment? A batch's `fill` may press only
      // these — a cookie banner rendering mid-step is also "new".
      // What tells this control's row apart from its neighbours. See digestRows.
      row: row || [],
      // WHICH form this control belongs to, so a card about to submit one can
      // say what that form currently HOLDS. Prefixed by the frame's offset,
      // because `document.forms` is per-document and two frames would
      // otherwise both call their first form "0".
      form: el.form ? (opts.offset + ':' + formIndex(el.form)) : '',
      // Is it inside something the page OPENED — a dialog, a listbox, a
      // calendar — rather than on the page proper? Only chrome words are
      // demoted by this, and never on something that submits.
      in_widget: !!(el.closest && el.closest(WIDGET)),
      option: ['option', 'treeitem'].indexOf(
        (el.getAttribute('role') || '').toLowerCase()) >= 0,
      // A form submit is gated whatever it is called: the nondescript "Dalej"
      // that posts the form is the dangerous one, not the obvious "Zapłać".
      submits: !!(el.form && (type_(el) === 'submit' || el.tagName === 'BUTTON'
                              && (el.type || 'submit') === 'submit')),
      // …but not every submit COMMITS anything. Reported raw and judged in
      // Python: the RAW attribute, because `form.method` reflects the spec
      // default and would report a form nobody wrote a method on as a GET,
      // which is the one case that must stay ambiguous.
      method: methodOf(el),
    });
  };

  const WIDGET = '[role=dialog], [role=alertdialog], [role=listbox], [role=menu],'
               + ' [class*=datepicker], [class*=date-picker], [class*=alendar],'
               + ' [class*=picker], [class*=dropdown], [class*=modal]';

  const formIndex = (form) => {
    const all = document.forms;
    for (let i = 0; i < all.length; i += 1) if (all[i] === form) return i;
    return -1;
  };

  const type_ = (el) => ((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();

  // How this control would send its form, as the page WROTE it. A button's own
  // formmethod wins, exactly as the browser resolves it.
  const methodOf = (el) => {
    const own = el.getAttribute && el.getAttribute('formmethod');
    if (own) return own;
    const form = el.form;
    return (form && form.getAttribute && form.getAttribute('method')) || '';
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
      found.push({el: el, tagOn: tagOn, kind: kindOf(el, type), name: name, href: href});
    }
  };
  walk(document);

  // WHAT MAKES THIS ROW DIFFERENT FROM ITS NEIGHBOURS (#251).
  //
  // Twenty flights are twenty buttons that all say "Wybierz", and an ordinal
  // is `click element 7` wearing a label — the very defect naming controls was
  // meant to end, reappearing at the step where the choice is actually made.
  //
  // Both halves are deterministic and content-blind. The ROW is found from
  // tree shape: take the lowest ancestor holding every control in the group,
  // and each control's row is the child of that ancestor containing it. No
  // class-name guessing, so a <table> of <tr>, a flex list of <div>s and a
  // grid of <li> tiles all work by the same rule — and an injected ad row is
  // simply a child nobody's control lives in. The DIGEST is a difference: a
  // line every row carries ("Wybierz", "Bagaż wliczony", "Cena od") cannot
  // tell them apart, so drop exactly those and keep the rest. Line-level and
  // never word-level: `640 PLN` and `720 PLN` differ as lines, so both keep
  // their unit instead of having it stripped as boilerplate.
  const linesOf = (el) => ((el.innerText || el.textContent || '')
    .split('\n')
    .map((s) => s.replace(/\s+/g, ' ').trim())
    .filter(Boolean));

  const digestRows = (all) => {
    const groups = new Map();
    for (const c of all) {
      const key = (c.name || '').toLowerCase();
      if (!key) continue;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(c);
    }
    for (const group of groups.values()) {
      if (group.length < 2) continue;
      const els = group.map((c) => c.el);
      let root = els[0].parentElement;
      while (root && !els.every((el) => root.contains(el))) root = root.parentElement;
      if (!root) continue;
      const rows = [];
      for (const el of els) {
        let row = el;
        while (row && row.parentElement !== root) row = row.parentElement;
        if (!row) break;
        rows.push(row);
      }
      // One row each, or this is not a repeated structure and there is no
      // honest digest to take — the ordinals stand.
      if (rows.length !== els.length) continue;
      if (new Set(rows).size !== rows.length) continue;
      const texts = rows.map(linesOf);
      const shared = new Map();
      for (const lines of texts) {
        for (const line of new Set(lines)) {
          shared.set(line, (shared.get(line) || 0) + 1);
        }
      }
      for (let i = 0; i < group.length; i += 1) {
        const distinct = [];
        for (const line of texts[i]) {
          if (shared.get(line) === texts.length) continue;
          if (distinct.indexOf(line) >= 0) continue;
          distinct.push(line.slice(0, opts.nameMax));
          if (distinct.length >= opts.rowLines) break;
        }
        group[i].row = distinct;
      }
    }
  };
  digestRows(found);

  // The budget, spent on what was asked for first. Matching is a plain
  // case-insensitive substring over the name and the href — deliberately not
  // fuzzy, for the reason `match_option` is not: this decides which controls
  // the model can press at all, and a clever match that quietly promotes the
  // wrong row is worse than one that finds nothing and says so.
  const needle = (opts.match || '').toLowerCase();
  const hit = (c) => (c.name || '').toLowerCase().indexOf(needle) >= 0
                     || (c.href || '').toLowerCase().indexOf(needle) >= 0
                     // The row is what a results page is narrowed BY: on a list
                     // of twenty identical "Wybierz" buttons, the only thing
                     // the model can name is what the row says.
                     || (c.row || []).join(' ').toLowerCase().indexOf(needle) >= 0;
  const wanted = needle ? found.filter(hit) : [];
  const rest = needle ? found.filter((c) => !hit(c)) : found;
  // Never a hard filter: the chrome the model needs to get anywhere — the
  // menu, the next-page link, the view switcher — is exactly what a topic
  // drawn from the page's CONTENT will not match, and dropping it would trade
  // one dead end for another.
  const room = Math.max(0, opts.max - opts.offset);
  const take = wanted.concat(rest).slice(0, room);
  for (const c of take) emit(c.el, c.tagOn, c.kind, c.name, c.href, c.row || []);
  // EVIDENCE THAT THIS PAGE COMMITS SOMETHING, read only in the direction that
  // ADDS a card. Absence proves nothing and must never un-gate: a card-on-file
  // checkout has no payment field at all, a PSP's card form is in a
  // cross-origin frame this cannot even see into, and a BLIK confirmation is
  // one six-digit box and a button. Present, though, it is worth acting on —
  // and because it only ever tightens, a page that lies about it can make aish
  // more careful and never less.
  const PSP = new RegExp('(^|\\.)(stripe|adyen|payu|przelewy24|p24|paypal'
                       + '|klarna|checkout|braintree-api|blik)\\.', 'i');
  const CHECKOUT = new RegExp('checkout|platnosc|płatnoś|payment|zamowienie'
                            + '|zamówienie|kasa|order|koszyk|basket|cart', 'i');
  const MONEY = /\d[\d\s.,]*(zł|pln|eur|€|usd|\$|gbp|£)/i;
  const commitEvidence = () => {
    try {
      if (document.querySelector('input[autocomplete*="cc-"],'
                                 + ' input[autocomplete="one-time-code"]')) {
        return 'a card or one-time-code field';
      }
      for (const frame of document.querySelectorAll('iframe[src]')) {
        try {
          if (PSP.test(new URL(frame.src, location.href).hostname)) {
            return 'a payment provider frame';
          }
        } catch (e) { /* an unparseable src is not evidence */ }
      }
      if (CHECKOUT.test(location.pathname)) return 'a checkout address';
      for (const form of document.querySelectorAll('form')) {
        if (!form.querySelector('button, input[type=submit]')) continue;
        if (MONEY.test((form.innerText || '').slice(0, 4000))) {
          return 'a price inside the form being submitted';
        }
      }
    } catch (e) { /* a page that will not answer is not evidence either */ }
    return '';
  };

  return {
    controls: out,
    commit: commitEvidence(),
    matched: matched,
    unreachable: unreached,
    matching: wanted.length,
  };
}"""


# The open picker, read on its own terms — and deliberately NOT part of
# `CONTROLS_JS`.
#
# A two-month range picker is ~84 day cells. Putting them in the page's control
# list would blow `MAX_CONTROLS` on exactly the pages this exists for, so the
# cells the date step needs would be the ones the cap dropped — and the cap
# drops them BEFORE the tag is written, which is what makes an unlisted control
# unpressable. It would also flood the delta on every open, and turn every ARIA
# spreadsheet and seat map on the web into a page of listed controls.
#
# So: the model never sees the cells. It says "this field ← this date"; the
# executor opens the picker, reads it here, and presses one cell. The cells are
# stamped with their OWN attribute so nothing about the page's numbering moves
# under the model while this happens.
CALENDAR_JS = "(opts) => {" + REACH_JS + NAME_JS + r"""
  const field = deepOne('[data-aish-n="' + opts.n + '"]');

  // Which container is this field's picker? Its own statement first — a page
  // that says aria-controls is telling you exactly, and guessing over the top
  // of that is how you read the wrong widget on a page with two.
  const GRID = '[role=grid], [role=application][class*=alendar], [class*=datepicker],'
             + ' [class*=date-picker], [class*=alendar], [class*=aterange]';
  const named = (el, attr) => {
    const id = el && el.getAttribute && el.getAttribute(attr);
    if (!id) return null;
    for (const one of id.split(/\s+/)) {
      const found = deepById(one);
      if (found && !unreachable(found)) return found;
    }
    return null;
  };
  let grid = field ? (named(field, 'aria-controls') || named(field, 'aria-owns')) : null;
  if (grid && !grid.querySelector('[role=gridcell], td, [class*=day]')) {
    const inner = grid.querySelector(GRID);
    if (inner) grid = inner;
  }
  if (!grid && field) {
    let at = field;
    for (let up = 0; up < 5 && at && !grid; up += 1) {
      at = at.parentElement;
      if (!at) break;
      for (const one of at.querySelectorAll(GRID)) {
        if (!unreachable(one)) { grid = one; break; }
      }
    }
  }
  if (!grid) {
    for (const one of deepAll(GRID)) {
      if (!unreachable(one)) { grid = one; break; }
    }
  }
  if (!grid) return {found: false};

  // The heading is what a bare "7" needs in order to mean a date at all. Read
  // it from the grid's own accessible name before anything visual.
  const labelled = named(grid, 'aria-labelledby');
  let heading = clean(grid.getAttribute('aria-label') || '');
  if (!heading && labelled) heading = clean(labelled.innerText || labelled.textContent);
  if (!heading) {
    const cap = grid.querySelector('caption, [class*=eader], h1, h2, h3, h4');
    if (cap) heading = clean(cap.innerText || cap.textContent);
  }
  if (!heading) {
    const box = grid.closest('[class*=datepicker], [class*=alendar], [role=dialog]') || grid;
    const cap = box.querySelector('caption, [class*=eader], h1, h2, h3, h4');
    if (cap) heading = clean(cap.innerText || cap.textContent);
  }

  // Clear LAST pass's tags before this one assigns any. Not doing it was the
  // same defect `CONTROLS_JS` records for `data-aish-n` — "two elements, one
  // number, silently" — and it bites here for the same reason: every month
  // walk re-stamps from 1, so after one hop `[data-aish-cell="42"]` matched a
  // cell from the month aish had just left as well as the one it wanted, and
  // `.first` took the stale one. Measured on wizzair.com: the walk from August
  // to December worked, the picker was showing the right day, and the press
  // came back Stuck because it was aimed at a cell no longer on the page.
  for (const stale of document.querySelectorAll('[data-aish-cell]')) {
    stale.removeAttribute('data-aish-cell');
  }
  const cells = [];
  let stamp = 0;
  for (const cell of grid.querySelectorAll('[role=gridcell], td, [class*=day]')) {
    const text = clean(cell.innerText || cell.textContent || '');
    const stamped = cell.getAttribute('data-date') || cell.getAttribute('data-day')
                 || cell.getAttribute('data-value') || cell.getAttribute('data-iso') || '';
    if (!text && !stamped) continue;
    // A picker very often wraps a <button> in a <td role=gridcell>; pressing
    // the td presses nothing at all.
    const press = cell.matches('button, a[href], [role=button]')
      ? cell
      : (cell.querySelector('button, a[href], [role=button]') || cell);
    if (unreachable(press)) continue;
    const label = clean(press.getAttribute('aria-label') || cell.getAttribute('aria-label')
                        || press.getAttribute('title') || cell.getAttribute('title') || '');
    stamp += 1;
    press.setAttribute('data-aish-cell', String(stamp));
    cells.push({
      tag: stamp,
      text: text,
      label: label,
      stamp: clean(stamped),
      // wizzair.com keeps a second copy of every pane parked off to the side
      // for its slide animation, and the copy is INERT: pressing it exhausted
      // the whole click ladder and came back Stuck.
      onscreen: (() => {
        try {
          const r = press.getBoundingClientRect();
          return r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0
                 && r.left < innerWidth && r.top < innerHeight;
        } catch (e) { return false; }
      })(),
      disabled: !!press.disabled
               || press.getAttribute('aria-disabled') === 'true'
               || cell.getAttribute('aria-disabled') === 'true'
               || /disabled|unavailable|niedostep/i.test(press.className || '')
               || /disabled|unavailable|niedostep/i.test(cell.className || ''),
    });
  }

  // WHAT THIS PAGE CALLS ITS MONTHS, asked of the browser rather than held by
  // aish. `_MONTH_STEMS` is Polish and English, so a picker that writes
  // "Oktober 2026" or "Οκτώβριος 2026" is unreadable to every part of the date
  // step — the cells, the heading and the arrows alike — and the fix for each
  // new language would be another entry in a table aish maintains by hand.
  // Chrome already ships every locale's month names; `Intl` is the oracle, and
  // the page itself says which locale it is written in. English is added
  // because an aria-label is very often English on a page that is not — which
  // is exactly lot.com, whose heading reads SIERPIEŃ 2026 while its arrow is
  // labelled `October 2026`.
  const monthNames = () => {
    const out = {};
    const langs = [];
    for (const said of [document.documentElement.lang, navigator.language, 'en']) {
      const one = String(said || '').trim();
      if (one && !langs.includes(one)) langs.push(one);
    }
    for (const locale of langs) {
      let fmt;
      try { fmt = new Intl.DateTimeFormat(locale, {month: 'long', timeZone: 'UTC'}); }
      catch (e) { continue; }
      const said = [];
      for (let m = 0; m < 12; m += 1) {
        said.push(fmt.format(new Date(Date.UTC(2026, m, 15))));
      }
      out[locale] = said;
    }
    return out;
  };

  // Month arrows, scoped to the picker. A page-level carousel's "next" must be
  // out of reach of a step that presses things with nobody looking.
  const box = grid.closest('[class*=datepicker], [class*=alendar], [role=dialog]') || grid;
  // Deliberately WIDER than the page's own control selector, and safe because
  // it is scoped to the picker: wizzair.com's ‹ › are neither buttons nor
  // links nor role=button, so the old scan did not see them at all and a date
  // three months out failed with "cannot find its next-month arrow" while the
  // arrows sat plainly on screen. What keeps this narrow is the other end —
  // `month_step`'s closed vocabulary, and the refusal to press anything that
  // submits.
  const NAVISH = 'button, a[href], [role=button], [onclick], [class*=arrow],'
               + ' [class*=next], [class*=prev], [class*=chevron], [class*=nav]';
  const nav = [];
  for (const one of box.querySelectorAll(NAVISH)) {
    if (unreachable(one)) continue;
    // The SAME naming the page enumeration uses (NAME_JS), not a second one:
    // an icon-only arrow has no text, and a naive read named it "" and dropped
    // it. `nameOf` reads the svg's title, a <use> id, an img alt, a data-icon,
    // an icon class token, or the glyph itself.
    const name = nameOf(one);
    if (!name) continue;
    // A DAY CELL IS NOT AN ARROW, and on a picker whose days are <button> it
    // matches NAVISH — so this pass used to re-stamp a cell the pass above had
    // already tagged, overwriting its number. `pick_day` then returned a tag
    // that `_press_in_picker` could no longer find, and the press came back
    // Stuck with the right day plainly on screen. Measured on wizzair.com: 92
    // nav candidates against 8 things that are not days.
    if (one.hasAttribute('data-aish-cell')) continue;
    stamp += 1;
    one.setAttribute('data-aish-cell', String(stamp));
    nav.push({
      tag: stamp,
      name: name,
      // A <button> with no type attribute defaults to SUBMIT, and a picker
      // inside the search form is the normal case — so this arrow may be a
      // form submit nobody approved. Reported, never pressed on a guess.
      submits: !!(one.form && (one.tagName === 'BUTTON'
                 && (one.getAttribute('type') || 'submit') === 'submit')),
    });
  }
  return {found: true, heading: heading, cells: cells, nav: nav,
          months: monthNames()};
}"""


# What a long dropdown contributes to the page TEXT, so it can be taken back
# out. `inner_text` includes every option of a closed `<select>` — measured: a
# 250-option country picker was 3 500 of a 4 176-character page, 84% of it, and
# on a real portal that is most of the read budget spent on one control the model
# has not even reached yet. The options come back as one contiguous block, in
# document order, exactly as they appear in the text.
FLOOD_JS = "(opts) => {" + DEEP_JS + """
  const out = [];
  // A 250-option picker inside a shadow root floods the page text exactly as
  // one in the document does — #245's measurement was 84% of the page — and
  // the stripper could not see it.
  for (const sel of deepAll('select')) {
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
                    method=str(raw.get("method") or ""),
                    in_widget=bool(raw.get("in_widget")),
                ),
                worded=says_it_commits(
                    name,
                    submits=bool(raw.get("submits")),
                    in_widget=bool(raw.get("in_widget")),
                    # The same `href` `mutating` reads, one line up: the
                    # enumeration's own answer to "does pressing this GO
                    # somewhere", never the kind the page called it.
                    navigates=bool(raw.get("href")),
                ),
                disabled=bool(raw.get("disabled")),
                submits=bool(raw.get("submits")),
                option=bool(raw.get("option")),
                navigates=bool(raw.get("href")),
                form=str(raw.get("form") or ""),
                row=[str(line) for line in (raw.get("row") or [])][:ROW_LINES_MAX],
            )
        )
    return address_controls(controls)


# A control the page gave no words to is addressed by its number, and the hash
# is what keeps that out of the namespace of real labels — plenty of pages have
# a button that says "12".
def _numbered(n: int) -> str:
    return f"#{n}"


def address_controls(controls: list[Control]) -> list[Control]:
    """Give every control the name the model will ask for it by, in place.

    **A control is addressed by what it SAYS, not by where it sits.** The
    numbering was positional and re-derived on every pass, so the same button
    was [13] and then [15]; worse, `browse_act(target=15)` is what the owner
    reads on the approval card and in the trace, and no human can review "click
    15". He reviews `click "Szukaj"` the same way he decides to press it
    himself — by reading the label — so the address IS the label. A name also
    survives the SPA re-render that renumbering never did.

    Duplicates are the whole difficulty, and they come in two kinds. Two nodes
    saying the same thing AND pointing at the same place are one control wearing
    two DOM elements — the mobile copy and the desktop copy of one nav link,
    which is most of them — so they share the address and either will do. Two
    that say the same thing and go somewhere DIFFERENT are genuinely two
    controls, and get an ordinal (`'Szukaj #1'`, `'Szukaj #2'`) on top of the
    `detail` already on their line. The ordinal is deliberately part of the
    address rather than a separate argument: it is one string the model copies
    back, and one string the card can print."""
    groups: dict[str, list[Control]] = {}
    for control in controls:
        base = (control.name or "").strip() or _numbered(control.n)
        control.address = base
        groups.setdefault(fold(base), []).append(control)
    for members in groups.values():
        if len(members) < 2:
            continue
        if len({(c.kind, c.detail) for c in members}) == 1 and not any(
            c.row for c in members
        ):
            continue  # one control, several nodes: any of them does the job
        keys = [_row_key(c) for c in members]
        # The LABEL stays the prefix — it is what the control DOES, and a row
        # digest without it reads like a fact about the page rather than a
        # button. What follows it is what tells this row from the next one, so
        # the model asks for 'Wybierz — 07:45' the way a person would say it.
        # An ordinal is the fallback for when nothing distinguishes them, not
        # the default.
        if all(keys) and len(set(keys)) == len(keys):
            for control, key in zip(members, keys, strict=True):
                control.address = f"{control.address} — {key}"
            continue
        for ordinal, control in enumerate(members, start=1):
            control.address = f"{control.address} #{ordinal}"
    return controls


def _row_key(control: Control) -> str:
    """The shortest thing that tells this control's row from its neighbours."""
    for line in control.row:
        said = line.strip()
        if said:
            return said[:ROW_KEY_CHARS].strip()
    return ""


@dataclass
class Resolution:
    """Which control the model meant, or why that could not be settled."""

    control: Control | None = None
    problem: str = ""


def resolve(controls: list[Control], target: Any) -> Resolution:
    """Find the control the model asked for by name.

    A ladder, tightest first, stopping at the first rung that yields exactly
    one — the same shape as `match_option`, and deliberately NOT fuzzy for the
    same reason: an edit-distance match silently picks, and the thing being
    picked here is a button that may spend money.

    Numbers still resolve. They are what a nameless icon is addressed by, they
    are what older sessions and the tests speak, and refusing them would turn a
    working call into a round trip for nothing."""
    asked = str(target if target is not None else "").strip()
    if not asked:
        return Resolution(problem="say which control, by the name in the list")
    if not controls:
        return Resolution(problem="this page has no controls listed")

    wanted = fold(asked)
    ladder = (
        [c for c in controls if c.address == asked],
        [c for c in controls if fold(c.address) == wanted],
        [c for c in controls if c.name == asked],
        [c for c in controls if fold(c.name) == wanted],
    )
    for hits in ladder:
        settled = _one_of(hits)
        if settled is not None:
            return Resolution(control=settled)
        if hits:
            return Resolution(problem=_ambiguous(asked, hits))

    digits = asked.lstrip("#")
    if digits.isdigit():
        for control in controls:
            if control.n == int(digits):
                return Resolution(control=control)
        # NOT a dead end: on a results page the distinguishing thing about a row
        # is very often a number — a price, a flight number, a time — so a bare
        # "640" is far more likely to mean that row than a control that no
        # longer exists. Fall through to the row match rather than refusing.

    # The row is what a repeated control is actually told apart by, so it is
    # searched with the address and not instead of it.
    loose = [
        c for c in controls
        if wanted and wanted in fold(" ".join([c.address, *c.row]))
    ]
    settled = _one_of(loose)
    if settled is not None:
        return Resolution(control=settled)
    if loose:
        return Resolution(problem=_ambiguous(asked, loose))
    if digits.isdigit():
        return Resolution(
            problem=f"there is no control {asked!r} on this page any more"
        )
    return Resolution(
        problem=f"no control on this page is called {asked!r}. This page has: "
        f"{_addresses(controls)}."
    )


def _one_of(hits: list[Control]) -> Control | None:
    """The single control these hits are, or None if they are several.

    Controls sharing an ADDRESS are one control the page drew twice — the
    mobile copy and the desktop copy of one nav link — and `address_controls`
    has already decided that. Asking the model to choose between two spellings
    of the same word is a question with no right answer."""
    if not hits:
        return None
    return hits[0] if len({c.address for c in hits}) == 1 else None


def _ambiguous(asked: str, hits: list[Control]) -> str:
    return (
        f"{asked!r} matches {len(hits)} controls — {_addresses(hits)}. "
        "Say which one, exactly as it is written."
    )


def _addresses(controls: list[Control]) -> str:
    shown = ", ".join(repr(c.address) for c in controls[:CANDIDATES_SHOWN])
    left = len(controls) - CANDIDATES_SHOWN
    return shown if left <= 0 else f"{shown}, and {left} more"


# How many steps one batch may carry. A flight search is eight; past this the
# card stops being a thing anyone reads on a phone, and the answer is better
# anyway — fill in one batch (no approval needed, nothing is committed), then
# submit in another.
BATCH_MAX_STEPS = 15

# What one step of a batch does. `fill` is the compound verb and the reason the
# batch is worth building: type, wait for the page to answer, and press the
# option that matches — because on the form this was built for, a destination
# box is not a text field. It opens a list that does not exist until the typing
# has happened, so the model CANNOT name that option up front, and a batch of
# flat primitives dies on the second step forever.
FILL = "fill"
BATCH_VERBS = (FILL, "date", "choose", "check", "click")


@dataclass
class Step:
    """One thing a batch does, as the model composed it."""

    target: str
    do: str = FILL
    value: str = ""
    control: Control | None = None


@dataclass
class Batch:
    """A validated batch, or the reason it is not one."""

    steps: list[Step] = field(default_factory=list)
    problem: str = ""

    def card(self, host: str) -> str:
        """What the owner is asked to approve — every value, in order, in the
        page's own words.

        This is MORE oversight than the single calls it replaces, not less, and
        that is the argument for the whole feature: typing has never been
        mutating (`is_mutating` — nothing is committed until something is
        pressed), so today a twenty-field form is twenty unseen auto-approved
        keystrokes and one card that does not say what it is about to send."""
        lines = []
        for step in self.steps:
            control = step.control
            said = control.address if control is not None else step.target
            if step.do == "click":
                lines.append(f"press {said!r}")
            elif step.do == "check":
                lines.append(f"tick {said!r}")
            else:
                lines.append(f"{said!r} ← {_shown(step.value)}")
            if control is not None and (row := control.row_note()):
                lines[-1] += f"  ({row})"
        return f"fill in this form on {host} and send it:\n  " + "\n  ".join(lines)


# A value long enough to be a paragraph is not reviewable on a card, and
# truncating it silently would make the card a worse record than no card.
CARD_VALUE_CHARS = 60


def _shown(value: str) -> str:
    if len(value) <= CARD_VALUE_CHARS:
        return repr(value)
    return f"{value[:CARD_VALUE_CHARS]!r} + {len(value) - CARD_VALUE_CHARS} more chars"


def _step_asked(raw: dict) -> Step:
    """One step as the MODEL wrote it, before the page is consulted.

    Shared by `plan_batch` and `typed_values` so there is one reading of what a
    step says. A fence that parsed these arguments its own way would be
    answering about a slightly different batch than the one that runs — and the
    aliases are exactly where that drifts: `do` is also spelled `action`, and a
    step's value is also spelled `text`."""
    return Step(
        target=str(raw.get("target", "") or ""),
        do=str(raw.get("do") or raw.get("action") or FILL).lower(),
        value=str(raw.get("value", "") or raw.get("text", "") or ""),
    )


# The verbs that put a MODEL-SUPPLIED value into the page. `choose` picks an
# option the page itself wrote, and `click`/`check` type nothing at all — so
# neither can carry a value the never-list is about. `date` is here because a
# field that opens no picker is typed as an ISO date like any other value.
TYPING_VERBS = (FILL, "date")


def typed_values(name: str, args: dict) -> list[tuple[str, str]]:
    """Every (control as the model named it, value) this call would TYPE.

    **The other half of the one fence (#310), and the half that made it one.**
    `browse_fill` and `browse_act(action="type")` are the only two ways a
    model-supplied value reaches a page, and they took different gate branches,
    so the never-list was enforced on one of them. Reading both here means the
    fence is asked once, about the ACT, before either branch is chosen —
    identical refusal, identical unapprovable outcome, by construction rather
    than by two call sites kept in step.

    **It consults nothing about the page**, not even whether the target
    resolves to a control. That is the property, not an economy: `refuses_to_type`
    reads what aish is about to send, so a value must not become typeable
    because the page moved, because a label was rewritten, or because the model
    named a control that is no longer there. The name comes back exactly as the
    model wrote it, which is what the refusal says out loud."""
    if name == "browse_fill":
        asked = args.get("steps")
        if not isinstance(asked, list):
            return []
        steps = [_step_asked(raw) for raw in asked if isinstance(raw, dict)]
        return [(step.target, step.value) for step in steps if step.do in TYPING_VERBS]
    if name == "browse_act" and str(args.get("action", "") or "") == "type":
        return [(str(args.get("target", "") or ""), str(args.get("text", "") or ""))]
    return []


def plan_batch(controls: list[Control], asked: list[Any]) -> Batch:
    """Read a batch and decide whether it may be offered at all.

    **At most ONE step may need approval, and it must be LAST.** The rule is
    not card hygiene, it is abort semantics: with the only committing step at
    the end, a batch that dies at step 7 of 20 has sent nothing. A mutating
    step in the middle turns every partial failure into a half-committed form,
    which is a state neither the owner nor the model can reason about.

    **Re-running is cheap but NOT always idempotent, and the difference is the
    date verb.** Typing is: `_type` overwrites, so the same fill twice is one
    fill. Pressing a day cell is not — a range picker takes the first press as
    the START of a range and the second as its END, so a retried batch does not
    begin again, it continues. A stopped batch that had already set a date says
    so rather than letting the model compose its retry against a widget state
    nothing told it about.

    A password field refuses the WHOLE batch and never draws a card, exactly as
    a single action does. There is no yes that makes it a good idea."""
    if not asked:
        return Batch(problem="say what to fill in — a batch needs at least one step")
    if len(asked) > BATCH_MAX_STEPS:
        return Batch(
            problem=(
                f"{len(asked)} steps is more than one card can honestly show "
                f"(limit {BATCH_MAX_STEPS}). Fill the form in smaller batches — "
                "filling needs no approval — then send it in its own call."
            )
        )
    steps: list[Step] = []
    for index, raw in enumerate(asked, start=1):
        if not isinstance(raw, dict):
            return Batch(problem=f"step {index} is not an object with a target")
        step = _step_asked(raw)
        if step.do not in BATCH_VERBS:
            return Batch(
                problem=f"step {index}: {step.do!r} is not one of {', '.join(BATCH_VERBS)}"
            )
        found = resolve(controls, step.target)
        if found.control is None:
            return Batch(problem=f"step {index}: {found.problem}")
        step.control = found.control
        if found.control.kind == PASSWORD:
            return Batch(
                problem=(
                    f"step {index} is a password field, and aish never types "
                    "passwords — nothing in this batch was done. Tell the user "
                    "to sign in themselves with /browser."
                )
            )
        steps.append(step)
    committing = [
        i for i, step in enumerate(steps)
        if step.control is not None and step.control.mutating
    ]
    if len(committing) > 1:
        return Batch(
            problem=(
                "a batch may carry only ONE step that needs approval; this one "
                f"has {len(committing)}. Do them in separate calls, so the user "
                "sees the page each one acts on."
            )
        )
    if committing and committing[0] != len(steps) - 1:
        early = steps[committing[0]].control
        return Batch(
            problem=(
                f"{(early.address if early else '?')!r} needs approval, so it "
                "must be the LAST step — otherwise a batch that fails halfway "
                "leaves the form half-sent. Put it at the end, or in its own call."
            )
        )
    return Batch(steps=steps)


def batch_is_mutating(batch: Batch) -> bool:
    return any(step.control is not None and step.control.mutating for step in batch.steps)


# Month names as STEMS, Polish and English in one table, because a picker
# writes "7 września 2026" or "7 September 2026" and the stem is what both
# spellings and every inflection share (września/wrzesień both start wrz).
# `mar` and `maj`/`may` collide across the two languages onto the same month,
# which is exactly why stems are safe here.
# What a language calls its twelve months, as stems: one tuple per month,
# January first. `None` means "only what aish itself holds" — see `month_of`.
Months = tuple[tuple[str, ...], ...] | None

_MONTH_STEMS = (
    ("sty", "jan"), ("lut", "feb"), ("mar",), ("kwi", "apr"), ("maj", "may"),
    ("cze", "jun"), ("lip", "jul"), ("sie", "aug"), ("wrz", "sep"),
    ("paz", "oct"), ("lis", "nov"), ("gru", "dec"),
)


# How short a month stem may be, and how long it is allowed to grow. Three is
# what Polish and English need; French needs four, because "juin" and "juillet"
# are one word until the fourth letter — which is the whole reason the length is
# DERIVED per language instead of chosen once.
STEM_MIN = 3
STEM_MAX = 12


def month_stems(names: list[str]) -> Months:
    """Twelve month names in one language → the shortest prefixes that tell them
    apart, one per month, or None if they cannot be told apart at all.

    **The language decides the length, not aish.** A fixed three letters is
    right for Polish (sty/lut/mar…) and WRONG for French, where `juin` and
    `juillet` collide until the fourth — and a collision here does not fail
    loudly, it silently reads June as July on somebody's trip. So the shortest
    length at which all twelve are distinct is computed, and a language where
    no length works contributes nothing rather than contributing a guess.

    Prefixes rather than whole names because pickers inflect: a Polish cell
    says "7 września" and the nominative is "wrzesień", so only a stem matches
    both. That is the same argument `_MONTH_STEMS` was built on; what is new is
    that the stem no longer has to be written down by hand."""
    folded = [fold(one) for one in names]
    if len(folded) != 12 or not all(folded):
        return None
    for length in range(STEM_MIN, STEM_MAX + 1):
        cut = [one[:length] for one in folded]
        if len(set(cut)) == 12:
            return tuple((one,) for one in cut)
    return None


def month_table(said: dict) -> Months:
    """Every language the page offered, merged into one stem table.

    A page is written in one language and its aria-labels are very often
    written in another, so this is a handful of locales and not a world atlas —
    which is what keeps the merged table free of the cross-language collisions
    a big one would collect."""
    if not isinstance(said, dict):
        return None
    merged: list[set[str]] = [set() for _ in range(12)]
    for names in said.values():
        stems = month_stems(list(names or []))
        if stems is None:
            continue
        for number, one in enumerate(stems):
            merged[number].update(one)
    if not any(merged):
        return None
    return tuple(tuple(sorted(one)) for one in merged)


def _month_by(text: str, table: tuple[tuple[str, ...], ...]) -> int | None:
    words = fold(text).replace(",", " ").split()
    for number, stems in enumerate(table, start=1):
        if any(word.startswith(stem) for word in words for stem in stems if stem):
            return number
    return None


def month_of(text: str, months: Months = None) -> int | None:
    """Which month does this text name, if any.

    A stem must START a word, never merely occur in one: "wrzesień" folds to
    "wrzesien", which CONTAINS "sie", so a substring test reads September as
    August. On a date step that is the difference between two months of the
    owner's trip.

    `months` is what the PAGE calls its months, asked of `Intl` in the page's
    own locale (see `monthNames` in `CALENDAR_JS`). It is added to the built-in
    Polish/English table, never a replacement for it — so the behaviour on
    every page aish already reads is unchanged, and a language aish has never
    heard of arrives without a code change.

    **Where the two disagree, neither wins.** A built-in stem matching one
    month while the page's own language says another is not a tie to be broken
    by ordering; it is aish not knowing, and the caller can say so. Every one
    of them refuses on None rather than pressing something."""
    mine = _month_by(text, _MONTH_STEMS)
    if months is None:
        return mine
    theirs = _month_by(text, months)
    if mine and theirs and mine != theirs:
        return None
    return mine or theirs


# A date, as much of one as could be read. A missing year is not a failure: a
# picker cell often says "7 września" with the year only in the grid's heading,
# and a match with an unknown year on one side is still a match.
@dataclass(frozen=True)
class Day:
    day: int
    month: int | None = None
    year: int | None = None

    def matches(self, other: Day) -> bool:
        """Same day, and same everything BOTH of us know."""
        if self.day != other.day:
            return False
        if self.month and other.month and self.month != other.month:
            return False
        return not (self.year and other.year and self.year != other.year)

    def complete(self) -> bool:
        return bool(self.month and self.year)


_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DMY = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
_YEAR = re.compile(r"\b(20\d{2})\b")
_DAY = re.compile(r"\b(\d{1,2})\b")


def read_date(text: str, months: Months = None) -> Day | None:
    """Read a date out of anything a page or a model writes.

    Tolerant on purpose and in one direction only: it will return a PARTIAL
    date rather than guess a whole one, because the caller can still match a
    partial safely — and the thing being matched is a day cell that feeds a
    submit."""
    text = (text or "").strip()
    if not text:
        return None
    iso = _ISO.search(text)
    if iso:
        return Day(int(iso.group(3)), int(iso.group(2)), int(iso.group(1)))
    named = month_of(text, months)
    if named:
        day = _DAY.search(text)
        year = _YEAR.search(text)
        if day:
            return Day(int(day.group(1)), named, int(year.group(1)) if year else None)
        return None
    dmy = _DMY.search(text)
    if dmy:
        said_year = dmy.group(3) or ""
        if len(said_year) == 2:
            said_year = f"20{said_year}"
        return Day(
            int(dmy.group(1)), int(dmy.group(2)), int(said_year) if said_year else None
        )
    bare = _DAY.fullmatch(text)
    if bare:
        # A cell that says only "7". Which month it belongs to has to come from
        # somewhere else, and `pick_day` will not press it until it does.
        return Day(int(bare.group(1)))
    return None


def months_on_show(
    cells: list[Cell], heading: str = "", months: Months = None
) -> list[tuple[int, int]]:
    """The (year, month) pairs the open picker is actually displaying, sorted.

    The heading is asked first and is usually enough. When it is not — an
    `ngb-datepicker` labels itself "Travel Dates" and puts the months in
    sub-headings it does not associate with the grid — the CELLS already know:
    each one states its own full date, which is what makes them pressable at
    all. Reading the span off them is the same fact from the same source, and
    it is what decides whether the month wanted is forwards or backwards.

    Deliberately a SPAN and not a single month: a range picker shows two at
    once, and "is November after what is on screen" has to mean after the LAST
    of them or the walk oscillates.

    **The cells are asked FIRST, and the heading is the fallback.** It was the
    other way round, on the reasoning that a heading is usually enough — but
    "the grid's accessible heading" is whatever `aria-labelledby` happens to
    point at, and on lot.com it points at the date FIELD's own label: *"Wybierz
    datę wylot z zakresu od 1 września 2026 do 28 sierpnia 2027"*. That parses,
    so the heading branch won, and it returned `[(2026, 8)]` — a month the
    picker was not showing and never would, CONSTANT across every hop. The walk
    then oscillated: with the span stuck in August, both `September 2026` and
    `December 2026` were "beyond" it, and pressing one then the other stepped
    forward and back until the grid signature repeated and the step gave up
    with *"the picker stopped changing"*.

    A cell states its own full date, which is what makes it pressable at all;
    a heading is a label about the grid. Where they disagree the cells are the
    evidence and the heading is a claim, so the heading is consulted only when
    the cells say nothing — and when the cells say nothing `pick_day` refuses
    anyway, which is why nothing is lost by demoting it."""
    seen = set()
    for cell in cells:
        found = cell.day(heading, months)
        if found and found.month and found.year:
            seen.add((found.year, found.month))
    if seen:
        return sorted(seen)
    month, year = read_month(heading, months)
    if month and year:
        return [(year, month)]
    return []


def read_month(text: str, months: Months = None) -> tuple[int | None, int | None]:
    """The month and year a picker's heading names — "wrzesień 2026", "September
    2026", "2026-09". A heading has no day, so `read_date` cannot read one."""
    text = (text or "").strip()
    if not text:
        return (None, None)
    year = _YEAR.search(text)
    month = month_of(text, months)
    if month is None:
        numeric = re.search(r"\b(20\d{2})[-/.](\d{1,2})\b", text)
        if numeric:
            return (int(numeric.group(2)), int(numeric.group(1)))
    return (month, int(year.group(1)) if year else None)


@dataclass
class Cell:
    """One day in an open picker, as the page describes it."""

    tag: int
    text: str = ""
    label: str = ""
    stamp: str = ""  # data-date and friends: machine-written, so trusted first
    disabled: bool = False
    # Where the owner could see it now. A TIE-BREAK between cells claiming the
    # same date, never a filter — a cell below the fold is perfectly pressable
    # and `_centre` scrolls to it.
    onscreen: bool = False

    def day(self, heading: str = "", months: Months = None) -> Day | None:
        """The date this cell IS, read from the most trustworthy thing it has.

        The stamp beats the label beats the visible text — `data-date` is
        written for a program and the label for a person, and only the text is
        ever just "7". A heading contributes only what the cell does not say
        itself."""
        for said in (self.stamp, self.label, self.text):
            found = read_date(said, months)
            if found is None:
                continue
            if found.complete() or not heading:
                return found
            month, year = read_month(heading, months)
            return Day(found.day, found.month or month, found.year or year)
        return None


@dataclass
class Pick:
    """Which cell to press, or why none may be."""

    tag: int | None = None
    label: str = ""
    problem: str = ""


def pick_day(cells: list[Cell], wanted: Day, heading: str = "", months: Months = None) -> Pick:
    """Which cell in this grid is the date asked for?

    **A cell is never pressed on a day number alone.** A range picker shows two
    months side by side and both of them have a "7"; a grid that says nothing
    about which month it is showing makes the two indistinguishable, and
    pressing one of them and reading the field afterwards is a coin flip whose
    result gets submitted. Unknown month is a QUESTION, exactly as an ambiguous
    suggestion is.

    **But only when the whole picker is mute.** A grid is very often a mixture:
    the day cells state their date and the furniture around them — weekday
    headers, the row that holds them, a decorative duplicate — carries a number
    and nothing else. Counting those as unreadable days made a perfectly
    legible picker refuse itself, and worse, it refused BEFORE the month walk:
    qatarairways.com opens on the current month, so asking for a date two
    months out reached "this picker's days say only their number (92 of them)"
    when 84 other cells were saying `5 August 2026` and the arrow to November
    was sitting right there (#273). If ANY cell resolved a month, the picker
    can be read; the mute ones are furniture, and a date that is not on screen
    is a month to walk to, not a question to ask."""
    if not cells:
        return Pick(problem="no day cells were found in the picker that opened")
    hits = []
    vague = 0
    dated = 0
    for cell in cells:
        found = cell.day(heading, months)
        if found is None:
            continue
        if not found.month:
            vague += 1
            continue
        dated += 1
        if found.matches(wanted):
            hits.append(cell)
    if not hits and vague and not dated:
        return Pick(
            problem=(
                f"this picker's days say only their number ({vague} of them) and "
                "nothing says which month is shown, so pressing one would be a "
                "guess. Open the month you want and press the day yourself"
            )
        )
    if not hits:
        return Pick(problem="that date is not in the month this picker is showing")
    if len(hits) > 1:
        # NOT a coin flip, and conflating it with one was wrong. The dangerous
        # ambiguity is a cell that does not say WHICH MONTH it is — two "7"s,
        # one in each of the months on screen. These have already been matched
        # on a FULL date, so every one of them asserts the same day: pressing
        # any is right by its own label. Measured on wizzair.com, whose picker
        # keeps a second copy of every pane for its slide animation — 138 cells
        # for two months of about 77 — so a real, legible December date refused
        # itself. Prefer the one ON SCREEN: the copy parked off to the side for
        # the animation is INERT, and pressing it exhausted the whole click
        # ladder and came back Stuck. Otherwise first in document order, and
        # the readback catches a copy that turns out to be dead anyway.
        hits = [c for c in hits if c.onscreen][:1] or hits[:1]
    chosen = hits[0]
    if chosen.disabled:
        return Pick(
            problem=f"{chosen.label or chosen.text!r} cannot be chosen on this page"
        )
    return Pick(tag=chosen.tag, label=chosen.label or chosen.text)


# How far a date step may walk. Airline booking horizons run to about eleven
# months; past this the model asked for something the picker does not offer.
MONTH_HOPS = 14

# What the control that moves a picker forward is called. Deliberately a SHORT
# closed list: this is a control aish presses with nobody looking, so a loose
# match ("next" anywhere on the page) is how a carousel's arrow gets pressed.
# Extended from MEASUREMENT, not from imagination: `scripts/probe_calendars.py`
# opens real pickers and prints what their arrows are actually called.
# wizzair.com's are "Later dates" / "calendar page forward" and "Previous
# dates" / "calendar page back" — none of which a list guessing at "next month"
# would ever hold, and the walk to December failed with the arrows plainly on
# screen. Still a CLOSED list matched on the WHOLE name: the fence that makes
# this safe is that a name not on it refuses rather than guesses, and the next
# site's phrasing is one probe run away.
_FORWARD = vocab.declare(
    "browse._FORWARD",
    demanded=True,  # asked only from inside a walk that has already decided it
    # needs an arrow: an ask is a walk, a miss is an aborted one
    languages="Polish + English + arrow glyphs",
    on_miss=vocab.BREAKS,
    structural="`month_arrow` + `month_table` — an arrow whose whole name IS a "
    "month reads as the month it goes to, in whatever language the PAGE says "
    "it is written in, checked against the months the grid is showing. A name "
    "that is neither still REFUSES rather than guessing, so the break stays "
    "loud at the call site",
    entries=(
    "next month", "next", "next dates", "later", "later dates", "forward",
    "calendar page forward", "nastepny miesiac", "nastepny", "pozniejsze daty",
    "→", "›", "»", "▶",
))
_BACKWARD = vocab.declare(
    "browse._BACKWARD",
    demanded=True,  # see `browse._FORWARD`
    languages="Polish + English + arrow glyphs",
    on_miss=vocab.BREAKS,
    structural="`month_arrow` — see `browse._FORWARD`",
    entries=(
    "previous month", "previous", "prev", "previous dates", "earlier",
    "earlier dates", "back", "calendar page back", "poprzedni miesiac",
    "poprzedni", "wczesniejsze daty", "←", "‹", "«", "◀",
))


def month_step(name: str, *, forward: bool) -> bool:
    """Is this the picker's own month arrow?

    Whole name only, or one of the two phrases that can mean nothing else. "is
    the word 'next' in there somewhere" matches "Next offer in this carousel",
    and this is a control aish presses with nobody looking."""
    folded = fold(name)
    words = _FORWARD if forward else _BACKWARD
    exact = any(fold(word) == folded for word in words)
    # The two-phrase prefix fallback below is a separate, uncatalogued check —
    # see docs/vocabularies.md. What is counted here is the LIST.
    vocab.note("browse._FORWARD" if forward else "browse._BACKWARD", matched=exact)
    if exact:
        return True
    phrases = ("next month", "nastepny miesiac") if forward else (
        "previous month", "poprzedni miesiac"
    )
    return any(folded.startswith(fold(phrase)) for phrase in phrases)


def month_arrow(name: str, months: Months = None) -> tuple[int, int] | None:
    """The (year, month) an arrow goes TO, when its whole name IS that month.

    Some pickers do not call their arrows anything like "next". lot.com labels
    them with the month on the other side — showing August and September 2026,
    the forward arrow is called `October 2026`, and after a jump to March 2027
    the pair reads `February 2027` / `May 2027`. No closed list of words for
    "next" can ever hold that, so `do="date"` refused with the arrow plainly on
    screen and the model had no second way to press a day: the cells are not in
    the page's control list, by design. Seven asks and zero matches on
    `browse._FORWARD` over thirty days, which is what `aish vocab` was built to
    make visible.

    **The whole name, and nothing else.** "Show March 2027 deals" names a month
    and is not an arrow, and this is a control aish presses with nobody
    looking. Exactly two words survive the fold — one that a month stem starts
    and one that is the year — so a sentence containing a month refuses, and a
    button carrying the CURRENT month (a "pick a month" header, which some
    pickers do have) is excluded by the caller instead: `choose_arrow` presses
    a month only when it lies beyond the span already on show.

    The direction is not read off the name at all. It is decided by comparing
    the month named against the months the grid is displaying, which is the
    same compass `months_on_show` gives the walk — so an arrow cannot lie about
    where it goes, and a mislabelled one simply fails the comparison."""
    words = fold(name).split()
    if len(words) != 2:
        return None
    month = month_of(name, months)
    year = _YEAR.search(name)
    if month is None or year is None:
        return None
    stems = tuple(_MONTH_STEMS[month - 1]) + tuple(
        months[month - 1] if months else ()
    )
    named = [word for word in words if word.startswith(stems)]
    yearly = [word for word in words if word == year.group(1)]
    if len(named) != 1 or len(yearly) != 1:
        return None
    return (int(year.group(1)), month)


def choose_arrow(
    nav: list[dict], on_show: list[tuple[int, int]], *, forward: bool,
    months: Months = None,
) -> dict | None:
    """Which of the picker's own controls moves it the way the walk needs.

    The closed vocabulary first, because a control that SAYS "next month" says
    what it does. Then the month-named shape, which has to earn it against the
    grid: the month on the arrow must lie strictly outside the span on show, on
    the side being walked to. That comparison is what makes reading a month off
    a label safe — the picker's own heading is one of the two operands, so a
    header button carrying a month already displayed can never be selected, and
    neither can the backward arrow while walking forward.

    Returns the entry to press, submit-shaped or not: refusing a form submit is
    the caller's, and it stays there so the refusal keeps naming which control
    it was about."""
    for one in nav:
        if month_step(str(one.get("name") or ""), forward=forward):
            return one
    if not on_show:
        return None
    edge = on_show[-1] if forward else on_show[0]
    beyond = []
    for one in nav:
        goes = month_arrow(str(one.get("name") or ""), months)
        if goes is None:
            continue
        if (goes > edge) if forward else (goes < edge):
            beyond.append((goes, one))
    if not beyond:
        return None
    # The NEAREST, never merely the first listed. A picker that offers both
    # ends at once — lot.com shows `September 2026` and `December 2026` while
    # displaying October and November — has two candidates "beyond" any stale
    # edge, and taking the first in document order is a coin flip that walks
    # backwards half the time. Nearest is also right on a picker that offers a
    # year jump beside a month step: one hop of a month beats one hop of
    # twelve, and the walk stays inside `MONTH_HOPS`.
    beyond.sort(key=lambda pair: pair[0], reverse=not forward)
    return beyond[0][1]


# How much of a form a card shows, and how much of one value. A card nobody
# can read is the failure this whole area keeps circling, so both are bounded —
# and what is left out is counted, never dropped quietly.
FORM_FIELDS_MAX = 12
FORM_VALUE_CHARS = 48

HELD = "currently: "
NOT_TYPED = "(not typed by aish)"


def form_values(controls: list[Control], target: Control) -> list[tuple[str, str]]:
    """What the form this control would submit is HOLDING, field by field.

    The card said what was about to be pressed and never what was about to be
    SENT — which is the half the owner actually needs, and the half that can
    have changed since anyone looked. Filling a form needs no approval, so the
    values can be set in one call and submitted in another, with a page free to
    reset a date in between; a card naming only the button cannot show that.

    A password's value is never read back. That it is THERE is worth saying —
    the owner is the only one who can have filled it — but aish types none and
    reads none."""
    if not target.form:
        return []
    held: list[tuple[str, str]] = []
    for control in controls:
        if control.form != target.form or control is target:
            continue
        if control.kind == PASSWORD:
            held.append((control.name or control.address, NOT_TYPED))
        elif control.kind == FIELD:
            said = control.detail[len(HELD):] if control.detail.startswith(HELD) else ""
            held.append((control.name or control.address, said or "(empty)"))
        elif control.kind == CHECK:
            held.append((control.name or control.address, control.detail or "unchecked"))
        elif control.kind == CHOICE and "selected: " in control.detail:
            held.append(
                (control.name or control.address,
                 control.detail.split("selected: ", 1)[1].strip("'"))
            )
    return held


def form_note(held: list[tuple[str, str]]) -> str:
    """The held values as the card shows them — one per line, bounded, and
    saying what the bound left out."""
    if not held:
        return ""
    lines = []
    for name, value in held[:FORM_FIELDS_MAX]:
        shown = value if len(value) <= FORM_VALUE_CHARS else (
            f"{value[:FORM_VALUE_CHARS]}… (+{len(value) - FORM_VALUE_CHARS})"
        )
        lines.append(f"  {name}: {shown}")
    left = len(held) - FORM_FIELDS_MAX
    if left > 0:
        lines.append(f"  and {left} more field(s)")
    return "this form currently holds:\n" + "\n".join(lines)


# How much of a change report is worth sending AS a change report. Past this the
# delta has stopped being cheaper than the thing it describes, and the caller
# sends the page instead — see `worth_sending`.
DELTA_MAX_CHARS = 1500

# Lines of unchanged text kept around each change. Zero would be cheapest and
# useless: "+ 63,19 zł" means nothing without the line above it saying what is
# priced. One line either side is what makes a diff readable as prose.
DELTA_CONTEXT_LINES = 1


@dataclass
class Delta:
    """What one action did to the page — the whole result of an action, and
    not a summary of one.

    **The page is re-sent in full only when it is a different page.** Measured
    on the session that filed this: nine clicks and types on lot.com cost 44 788
    characters, ~5 000 per action, because every one of them re-sent the entire
    page and its entire control list to report that a dropdown had opened. What
    the model needed each time was the handful of lines that were not there
    before.

    Two properties make that safe to do. It is diffed against WHAT THE MODEL WAS
    LAST SHOWN, never against the page as it was a moment before the click — so
    a change the page made on its own, while nobody was acting, still arrives
    rather than falling into the gap between two reads. And nothing is ever
    dropped silently: past the cap the count of unsent changed lines is stated,
    the same way `MAX_CONTROLS` states what it left out. A diff that quietly
    decides which changes matter would be a channel for a page to hide one."""

    added: list[Control] = field(default_factory=list)
    removed: list[Control] = field(default_factory=list)
    changed: list[tuple[Control, Control]] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    # Changed lines the cap left out. Never silent: see the class docstring.
    more_text: int = 0

    def empty(self) -> bool:
        """Did nothing at all change?

        This is the answer to "did that click work" — delivered on the FIRST
        click, as a fact the page reported, rather than inferred by a counter
        three identical calls later."""
        return not (self.added or self.removed or self.changed or self.text)

    def render(self) -> str:
        if self.empty():
            return "nothing on the page changed"
        parts = []
        if self.text:
            body = "\n".join(self.text)
            if self.more_text:
                body += (
                    f"\n[{self.more_text} more changed line(s) not shown — "
                    'use action="read" to see the whole page]'
                )
            parts.append("page text:\n" + body)
        control_lines = (
            [f"+ {c.line()}" for c in self.added]
            + [f"- {c.line()}" for c in self.removed]
            + [f"~ {new.line()}" for _, new in self.changed]
        )
        if control_lines:
            parts.append("controls:\n" + "\n".join(control_lines))
        return "\n".join(parts)

    def worth_sending(self) -> bool:
        """Is the change still smaller than the page?

        A click that rebuilds the whole page produces a diff the size of two
        pages. At that point the honest and the cheap answer are the same one:
        send the page."""
        return len(self.render()) <= DELTA_MAX_CHARS


def diff_snapshots(before: Snapshot, after: Snapshot) -> Delta:
    """What changed between the page the model was last shown and this one."""
    delta = Delta()
    was = {c.address: c for c in before.controls}
    now = {c.address: c for c in after.controls}
    delta.added = [c for a, c in now.items() if a not in was]
    delta.removed = [c for a, c in was.items() if a not in now]
    delta.changed = [
        (was[a], c)
        for a, c in now.items()
        if a in was and _control_state(was[a]) != _control_state(c)
    ]
    delta.text, delta.more_text = _text_delta(before.text, after.text)
    return delta


def _control_state(control: Control) -> tuple:
    """Everything about a control that the model would act differently on. Its
    address is its identity and so is not part of its state."""
    return (control.kind, control.detail, control.disabled, control.mutating)


def _text_delta(before: str, after: str) -> tuple[list[str], int]:
    """Changed lines of page text, with context, and how many were left out."""
    lines: list[str] = []
    spent = 0
    dropped = 0
    for line in difflib.unified_diff(
        (before or "").splitlines(),
        (after or "").splitlines(),
        n=DELTA_CONTEXT_LINES,
        lineterm="",
    ):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            # The hunk header is line arithmetic for a patch program. The model
            # needs to know only that the next lines are from somewhere else.
            line = "…"
        changed = line.startswith(("+", "-"))
        if spent + len(line) > DELTA_MAX_CHARS:
            dropped += int(changed)
            continue
        spent += len(line) + 1
        lines.append(line)
    while lines and lines[-1] == "…":
        lines.pop()
    return lines, dropped


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
