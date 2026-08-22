# The browser — reading pages a fetch cannot read, and driving one by hand (#221)

`browser.py`, `web._browser_read`, `Agent._login_gate`, `server._browser_view`, `aish/static/app.js` `[BROWSER-VIEW-*]`.

**Start here if you are new to this file.** Two capabilities share one Chrome and one profile: `read_url` escalates to it when a fetch cannot read a page, and the owner drives it by hand from his phone (`/browser`). The rules that govern almost every decision below are these four, and most mistakes here have been a failure to apply one of them:

1. **A round trip costs 1–3 seconds; anything local is free.** Maximise information per frame; never spend a trip on what the phone can do itself.
2. **A native dialog is a dead end.** It is browser chrome, so `page.screenshot` cannot see it and there is nothing to tap. Capabilities that summon one are removed, or brought into the page.
3. **The session must be created in the browser that will later use it**, by one identity. That is why there is no proxy and no mobile/desktop split.
4. **Follow the conventions of a mobile browser.** If a browser does it with a gesture on the content, do not add a widget. Nearly every UI complaint here has been an invented widget.

Open work is on GitHub: **#223** (correction frames double traffic), **#224** (round-trip economies), **#225** (verify Google sign-in; sign-in question blind spots — the JPEG-quality third is answered below under the frame-size measurements), **#237** (the model cannot drive the browser at all — it reads a page, it cannot click one).

`read_url` fetches with urllib: fast, cheap, anonymous, right for most of the web. Two kinds of page it cannot read **at all** — one rendered entirely by JavaScript, where the fetch returns an empty shell, and one behind a login, where the fetch is simply a different, logged-out client. This is the escalation for both: a real Chrome on this Mac, driven off-screen, with a profile that persists.

The two are **not** detected the same way, and conflating them cost the feature its point for a year. A JavaScript shell announces itself — the body is empty. A login wall does not: it is 200, with a full page of text, and there is nothing in it a fetcher can tell apart from the page it asked for. So identity is decided by **routing**, before the fetch, from the login record — never by inspecting what came back (see *A signed-in host is ROUTED*, below).

---

## What the measurements actually said

The feature exists because of a session (`session-20260814-062523`) where allegro.pl was tried five times and never once returned a page: `read_url` took **403** three times, and the `r.jina.ai` fallback returned `Warning: This page maybe requiring CAPTCHA` with an **empty body** three times. The model then twice proposed `curl`, claiming the owner's home IP had "a much greater chance of bypassing the block" — **false**, and the owner rejected both with a comment. They offered to solve the captcha themselves and there was no mechanism to hand them one.

What the probes then found, and every design decision below follows from it:

| Setup | Result |
|---|---|
| urllib, browser-ish UA | 403 |
| Jina Reader (datacenter, no session) | empty page + CAPTCHA warning |
| **headless** Chrome, cold profile | **403, zero text** |
| **headful** Chrome, cold profile | **200, 22.8k chars, real prices** |
| headful, off-screen window | 200 — off-screen costs nothing |
| headful, 2nd+ read on the same profile | **403** |
| headful + automation flags suppressed | reads keep working |

Three things follow. **Headless is what these sites block**, not the IP and not the User-Agent — so the browser runs headful and is merely parked off the visible desktop. **No captcha is ever offered**, so "show the human the challenge and let them solve it" — the escape hatch the first design was built around — had nothing to solve and was cut. And **the block lands on the profile after the first page**, which is what makes the automation flags load-bearing rather than cosmetic.

## The stealth switch is a decision, not a default

`--disable-blink-features=AutomationControlled` plus dropping `--enable-automation` is what makes reads 2..n work. Its only purpose is to hide that the browser is automated: it is anti-detection, and it is likely contrary to those sites' terms.

It ships **on**, because the owner was shown that trade-off explicitly and chose it (2026-08-14). It is one switch — `AISH_BROWSER_STEALTH=0` — so the decision stays visible and reversible rather than dissolving into a pile of flags nobody can find later. Do not add fingerprint spoofing, proxy rotation or profile rotation on top: that is an arms race lost on every Allegro deploy, and each addition makes the switch mean less.

`AISH_BROWSER=0` disables the browser entirely; `read_url` then reports the block honestly instead of rendering it. That path is pinned by `TestBlockedPageAdvice` via the `no_browser` fixture — those tests must say there is no browser rather than rely on one being absent.

**Jina Reader is retired as advice, on evidence.** It was the fallback before this existed, and across the owner's two real sessions it returned FOUR empty stubs and TWO timeouts and not one page of content — every "success" being `Title: allegro.pl / Warning: This page maybe requiring CAPTCHA / Markdown Content:` and nothing after it. Worse, `read_url` logged that stub `ok` and handed it to the model as the page, which is the same laundering `is_challenge` exists to prevent, one tool over. It fetches from a datacenter with no session, so recommending it *after* the local browser has failed points at something strictly weaker. A URL the owner pastes is still fetched; the stub is now refused (`_JINA_STUB`), and a Jina URL is never escalated to the browser, since rendering a rendering service proves nothing.

## The second identity: reading as NOBODY

`search_profile_dir()` — `~/.local/state/aish/browser/search-profile`, beside the owner's profile and never it. It exists so `web_search` can read a search engine's own results page without ever raising the question `_login_gate` is there to ask.

**The gate is not the obstacle; the identity is.** `google.com` is in `logins.txt`, so a `read_url` of `google.com/search` is a signed-in read: it shows an approval card, and in a triggered session with no approver it is refused outright. The obvious fix — exempt `google.com/search` from the gate, since "search is public" — is the wrong shape twice over. It is a claim about a URL, evaluated *before* navigation and false the moment Google 302s to `consent.google.com`, `/sorry/` or `accounts.google.com/CheckCookie`; and it would exempt `read_url` too, in every origin, turning a fail-closed invariant into a signed-in read any triggered session can reach. An exemption is also a second authority over identity with no owner lifecycle — `logins.txt` can be shown by `/browser` and revoked by `forget`; a list in the source can be neither. A profile with an empty cookie jar answers the gate's question `no` down every redirect, needs no exemption, and changes nothing in `agent.py`.

**What it costs, measured.** 2026-08-21, same IP, same minute: the signed-in profile answered `site:careers.google.com "product management" Poland` with 200 and ten correct results; a cold profile got **429 and `/sorry`** after roughly 25 automated queries that day. Google scores the IDENTITY, not the address, and reading as nobody is the identity it likes least. That is the trade taken deliberately: the account in the owner's profile is `bot@wenda.eu`, a live Workspace mailbox — `browser.read("https://mail.google.com/")` renders **Inbox (9)** with no password asked — and it is the identity `email_poll` depends on. Automated scraping is enforced per ACCOUNT, so tolerating a wall is cheaper than risking the mailbox, and the wall is survivable precisely because it is one of TWO indexes and never the only one — `web_search` asks both concurrently and degrades to the other (`docs/agent-core.md`).

## The address bar takes a search, and a list of where it has been

**`as_address` decides address-or-search, and the DEFAULT is what makes it usable.** The location box took only addresses, so typing `krzyżacy 1960 obsada` became `https://krzyżacy 1960 obsada`, Chrome refused it, and the view came back blank — searching from aish's own browser meant knowing to type a `google.com/search?q=` URL by hand, which is not what an address bar is. The rules are Chrome's, in the order that matters: an explicit `http(s)` scheme is an address and nothing else is inspected; anything containing whitespace is a search, since no address has a space; `localhost`, an IPv4 address, or a dotted host with an optional port and path is an address; **everything else is a search**. That last line is the one carrying the feature — a single unrecognised word is far more often something to look up than a hostname to visit — and the last label must start with a LETTER, which is what keeps `3.14` out. The LAN gets `http://` rather than `https://` because nothing on it serves TLS (his Home Assistant, aish's own web app), and https there fails rather than falling back. It is shared by `view_open`, `view_act(goto)` and `command()`, so the location box, `/browser <anything>` and the CLI all behave the same. `browser.SEARCH_URL` is the one engine template; `web.SEARCH_ENGINE_URL` is that constant, not a second copy. `TestTheAddressBar`.

**This is why the profile verb is `/browser anon` and not `/browser search`.** Once a bare phrase in the address bar means "look this up", `/browser search cats` is a genuine coin-flip between looking up cats and signing the search profile in at `https://cats`. `close` and `forget` collide the same way in principle and never in practice; `search` would have collided constantly.

**Recents are a list, they are per SITE, and they live on the server.** The empty view offered exactly one button — Resume, the last page — which answers *"carry on where you left off"* and nothing else, while ten pages back is where the address you cannot remember actually lives. `remember_page` records one row per HOST, newest first, capped at `RECENT_MAX`; per-host is the owner's own framing and it is load-bearing, because searching from here a few times otherwise fills the list with ten google.com rows and pushes off the thing he opened it to find. It is written from `_frame` on a NAVIGATION rather than per frame — a frame is captured for every tap and scroll, and a disk write on each of those buys nothing — and it never raises, because it runs alongside a screenshot that must not fail for it. It is stored in the state dir and served over the socket rather than kept in `localStorage`, which is what the Resume button used: where aish's browser has been is a fact about **that one Chrome on that one Mac**, not about the phone looking at it, so it must read the same from every device. Each row carries the profile it was opened in, and says so on screen when that is the anonymous one. `TestRecentPages`.

**`view_open` adds the scheme, and for a long time it did not.** `view_act(goto)` and `browse_open` both normalise a schemeless address; this one did not, and the web app sends the typed line straight to it — so `/browser eon.pl` from the PWA **never opened**, coming back `about:blank` with *"could not open eon.pl (Error)"*, while `/browser https://eon.pl` worked. Only the CLI was safe, because `command()` normalises before calling in, which means the one surface the owner actually uses was the broken one. Found by driving the real page during the `/browser search` verification, not by a test. `TestSigningInTheSearchProfile`.

**Signing it in is a choice, and there is exactly one way to make it.** Reading as nobody is what Google likes least — see the measurement above — so the profile may eventually need a session of its own. `/browser anon <url>` points the remote view at it; `/browser anon` says what it is and what it is signed into. Copying the Google cookies across from the owner's profile would be the obvious shortcut and it is precisely what rule 3 forbids: a session must be created in the browser that will later use it. The record is a SEPARATE file (`search-logins.txt`), because `logins.txt` is not a note — it is what `is_logged_in` answers from and therefore what makes `_login_gate` fire on a `read_url`. A sign-in in a profile `read_url` never touches must not change what the owner's own reads are allowed to do, and his sign-ins must not make this browser look signed in when it is not. Nothing gates on the search record; it exists so the owner can see the state. What keeps the signed-in option honest is at the other end: `web_search` builds the only URL there is and `landed_on_results` pins where Chrome actually finished (`docs/agent-core.md`), so "search is fine, mail is not" is enforced rather than trusted. `TestSigningInTheSearchProfile`.

**Two identities, and it stays two.** The owner's, and nobody's. A profile per situation is the fingerprint-rotation arms race this file already refuses, and Chrome is not free under a 16 GB roof shared with a Home Assistant VM and Colima. Both contexts hang off the one owner thread and both are closed by the same idle reaper. A cold read is deliberately NOT blocked by an open `/browser` view: the view holds the owner's profile, so there is no page to steal and no phone viewport to inherit. `TestTheSearchProfile`.

## A wall says whatever it likes, and search engines do not say "captcha"

`_CHALLENGE_MARKERS` was written from the bot-management vendors — DataDome, Cloudflare, the "verify you are human" family — because those were the walls in front of the shops. Search engines word it differently and serve it with a **200**, so neither the marker list nor `BLOCK_STATUS` caught them. Measured 2026-08-21 while probing whether the browser could read a results page: `bing.com/search` returned **105 characters** — *"One last step. Please solve the challenge below to continue"* — and `is_challenge` scored it as CONTENT. Google's `/sorry` interstitial (*"unusual traffic from your computer network"*) is the same shape and is the one that matters, because it is the wall a search fallback would meet on the day Google decides against us — and handing it back as the results is the laundering failure this detector exists to prevent, one host over. Three wordings added, nothing else: the length guard stays the only structural test, and the character floor that was tried and withdrawn stays withdrawn. `TestChallengeDetection`.

## A wall on a warm profile is the SCORE, not the page

The clearest wrong call in this whole build was recorded here as fact: *"Allegro serves a completely empty 403 body for that offer, consistently — that's the site's call, not a bug."* It was neither consistent nor the site's call.

The same page returns **7 833 characters on a cold profile and ZERO on a warm one**, and dropping a single cookie — `datadome` — takes it straight back to **7 874**. Bot-management vendors issue a scoring token, and once it has decided against you it keeps deciding against you. The score IS the block, so the score is what gets discarded: a read that comes back walled sheds this host's reputation cookies and asks exactly once more.

Without that step the browser was **weaker than a third-party reader had any right to make it look**, which is the observation that produced this section — a real Chrome with a real profile should be strictly better than a session-less datacenter fetcher on every page, and where it isn't, the defect is in how it is driven.

**Deletion is BY NAME, one cookie at a time, and never `clear_cookies()`.** The same jar holds the sessions the owner signed into by hand, which is the entire reason the profile persists; clearing those to fix a scrape would trade the feature for the workaround. `cf_clearance` is deliberately not on the list — it is a PASS token, evidence a challenge was already solved, and dropping it would throw away a good thing. `TestSheddingASouredReputation` pins the login-survives case as hard as the shedding case. `TestVisitingIsNotSigningIn` pins that a visit writes nothing. `TestPasswordsAreNeverReadBack` and `TestEditingUsesRealKeystrokes` pin the input contract; `TestEveryActionActuallyRuns` executes every one of them; `TestNativeDialogsAreDeadEnds` pins the passkey removal; `TestTheViewIsDesktopSoOneFrameCarriesMore` and `TestFramesWaitForThePageToSettle` pin the viewport decision and the settle.

## Status is diagnostic only

A site that dislikes automation may answer **403 and still serve the whole listing, prices included** — measured, not hypothesised. So a browser read is judged on whether it produced **text**, never on the code. Judging on the code would throw away the exact page this feature exists to get (`TestReadUrlEscalation`).

## Where the profile lives is a safety decision

`~/.local/state/aish/browser/profile`, **never `~/.config/aish/`**. The config tree is auto-committed and pushed to a private GitHub repo by the knowledge-git agent, and this directory is made of live session cookies — a profile under config would publish the owner's logins to a git remote on a timer. `TestProfileLocation` pins it.

Persistence is the *point*, not an optimisation: a session the owner established by hand is still there next week, and every later read of that site is made as them. Nothing here ever clears the profile.

## One thread owns the browser

Playwright's sync API binds its objects to the creating thread, and `read_url` runs on a pool — `_execute_tool_calls` fans read-only tools out concurrently — so a shared context touched from a second thread errors out. Every call is marshalled to one long-lived owner thread through the owner loop. That also buys single-ownership of the profile directory, which Chrome requires: it locks the user-data-dir and a second launch against a live profile fails. It is why `open_for_login` closes the off-screen context before opening the on-screen one.

The context stays warm between reads (a launch is ~2s) and closes after `IDLE_SECONDS`, because this box runs a Home Assistant VM and Colima against a 16 GB ceiling and an idle Chrome is not free.

## `/browser` — the owner's own door

`browser.command()` is shared verbatim by the CLI and the web so both surfaces say the same thing and neither owns the wording. No argument shows the profile, the stealth state and which sites are signed in; a URL opens a **real, on-screen** window to sign in at; `forget <host>` drops one; `close` shuts it down.

aish never types the owner's credentials — it hands them a browser and stays out of it. The window opens **on the Mac**, not on the phone that asked, so the web path acks that fact immediately and reports the result whenever they close the window (up to fifteen minutes later). It runs on the worker pool, not `to_thread`, because it parks for exactly that long.

Which hosts count as signed-in is recorded from what the owner **navigated to** during a login window, not from the cookie jar: a jar is mostly third-party trackers, and "sites I logged into" is a claim only their own navigation supports (`TestLoginRecord`). Matching is on a dot boundary, so `evilallegro.pl` is not `allegro.pl`.

## A signed-in host is ROUTED to the browser, not merely gated (#236)

The login record has two jobs, and for a long time it only did one.

Everywhere else in `read_url`, escalation is wired to a **failure**: a 403/429/503, a socket timeout, an empty shell. Wire it that way and a login wall never escalates, because a login wall is not a failure — `eon.pl/mojeon` answers **200 with a full page of Polish text and no error at all**. The fetch "succeeded", so Chrome never launched, for exactly the class of page the persistent profile exists to read.

What that looked like from the owner's side (`session-20260818-174527`, #236) is the sharpest way to state it. He asked for his E.ON invoices. `_login_gate` drew a card — the card whose whole question is *"does this read carry your live session?"* — and he approved it. `read_url` then fetched the page **anonymously**, handed the model a login form, and Chrome did not launch once in the entire session. The gate and the read disagreed about what the read was, and the gate was the one telling the truth.

Then it compounded, twice. The model, holding a login screen, reported the portal as inaccessible and asked him to download the PDFs by hand — and the `read-means-read` rule fired at it for saying so, because it *had* read eon.pl. A harness that hands over the wrong page also punishes the model for describing it accurately.

So: **`browser.is_logged_in(url)` routes, exactly as `BROWSER_HOSTS` routes.** It was already consulted on this code path, for permission only. It is the strongest routing signal aish has and the owner supplied it himself, with his own hands, in a window aish opened for him.

The cost is real and accepted: a public page on a signed-in host now pays a ~2s Chrome launch. That is the price of the gate not lying, it is bounded (the context stays warm, so it is paid once per idle period), and it lands on the same hosts the gate already stops to ask about. `TestASignedInHostIsReadAsTheOwner` pins the routing, the subdomain case, and the cost fence — a host with no account still goes straight to the fetch.

**A read that falls back to the fetch says so, in aish's own voice.** The browser can be unavailable, or busy with the owner's own hands, and plenty of a signed-in host is public — so the fallback stays. Falling back *silently* is what cannot: the model has no other way to know it is looking at a stranger's view of an account it was asked about. Two notes, both **above** the untrusted-content banner, because they are statements about provenance and everything below that banner is declared to be page data:

- **the session lapsed** — the browser rendered it, as him, and the site asked for a password anyway. Names the host and `/browser <host>` to sign in again.
- **the read was anonymous** — with the *reason*, because "playwright is not installed" and "the browser is being driven by hand right now" ask for opposite things from the owner.

Neither discards the page. A wall earns an ERROR because a challenge screen is worth nothing; a sign-in page is worth precisely one thing — knowing the session lapsed — and the model must be able to say which page it was looking at when it says so.

**The evidence for "this is a door" is the password FIELD, never the wording.** *Zaloguj się* / *Sign in* sits in the navigation of half the logged-in web, so matching words would flag real account pages, and would then need maintaining per language on a corpus that is mostly Polish. `Page.signin` comes off the live DOM (Playwright's selectors pierce open shadow roots, the same reason `Page.text` does not come from `page.content()`); `browser.asks_to_sign_in` is the fetch path's weaker HTML-level twin, knowingly blind to a form built in JavaScript and aimed at the server-rendered wall a fetcher actually meets. `TestAskingForAPassword` pins both directions.

One test-isolation note, because it can hide this whole section: `BROWSER_HOSTS` is process-global and nothing clears it, so a test that reads a host successfully routes every later test's read of that host through the browser too — masking the very regression the routing prevents. `conftest.no_leaked_browser_hosts` clears it around every test.

## The login gate — and why it is not the egress gate

`Agent._login_gate` holds a `read_url` that would be made with the owner's live session until they approve it, and it applies to **every origin, the attended session included**.

That is the difference from `_egress_gate`, which asks *"is this host one the owner named?"* and only in a triggered session, on the reasoning that an attended owner can see the host for themselves. This gate asks a different question — *"does this read carry the owner's session?"* — and their watching does not settle it, because the URL may have come from text on a page rather than from them. An injected instruction that steers a read at a signed-in site would otherwise pull private account data into the context silently.

Approval is per host and lasts the session (`_approved_logins`), matching the egress gate: a task that reads five pages of one portal asks once. It is session-scoped like every other grant (L4). With no approver it fails **closed** — reading the owner's account with nobody watching is the one outcome this exists to prevent.

Only `read_url` can carry a session; `show_image` / `read_pdf` / `read_media` fetch bytes through the anonymous opener, so they are never gated here and must not draw a card claiming they are. `TestLoginGate` pins all of it, including the seam that matters most: `_read_needs_prompt` routes a gated read **off** the parallel path, which has no gate at all and would otherwise bypass approval entirely.

## Driving a page, not reading one (#237)

`read_url` has one verb: navigate to a URL and extract text. That is the whole web of documents and none of the web of applications, where what you want sits behind a control instead of an address. Two sessions on the same portal measured the gap. Asked for his E.ON invoices, the model read the dashboard and then *guessed* — `/mojeon/faktury`, `/rozliczenia`, `/pulpit`, three 404s. With the signed-in read fixed (#236) it got the real dashboard, the invoice table, the five properties and the PDF links, and stopped in exactly the same place: `Przełącz lokal` is `<a href="#">`, and no URL in the world is that button. Both times it then flailed — `osascript` at Chrome's tabs, `ls`, a Gmail search, `uv pip list` — because a system with no click verb guesses.

`browse.py` holds the parts that need no browser (the control model, the labelling, the loading test); `browser.browse_*` drives; `web.browse` / `web.browse_act` are what the model calls; `Agent._browse_gate` decides.

**A page is a LIST OF NAMED CONTROLS, not an image.** The remote view maps a tap from a JPEG because a human is looking at it; a model estimating (x, y) mis-clicks constantly and worst at the edges. The DOM already knows where every control is and what it is called, so the model reads `button 'Przełącz lokal'` — the same words the owner used to ask for it — and the click lands on the element rather than on a coordinate that used to be over it. It was a *numbered* list until #251; the numbers survive as the tag underneath and as the address of a control the page gave no words to.

**The element is TAGGED, not remembered.** Enumeration stamps `data-aish-n`; acting re-queries by it. Playwright handles go stale on every re-render, which on an SPA is constantly; an attribute survives a re-render and dies with the document, which is exactly the lifetime the numbering has. **Each pass clears the previous pass's tags first** — not doing so was a real defect and a subtle one: numbering shifts when a menu opens, so the element that was `[4]` kept its tag while a different element became `[4]`, and `locator(...).first` then picked whichever came first in the document. Two elements, one number, silently, and only on a page that changes shape — which is every page this feature exists for.

**A plain navigation is never mutating, whatever it is called.** The word list's first act was to flag the link named *Faktury i płatności*, because it contains the word for payment. An `<a>` with a real href is a GET to another page — what `read_url` does under auto-approval — so gating it asks permission to read what aish may already read. What makes it safe is that it NAVIGATES, and `el.href` resolves `href="#"` to the page's own absolute URL, so a naive "starts with http" test calls every JavaScript control a navigation. Real Chrome duly reported `<a href="#">Zapłać</a>` as a link to the current page, which would have walked it straight past the gate. Same document, fragment aside, is not going anywhere.

**A page mid-load has TEXT**, so neither the emptiness test nor the thin-page retry catches it — `Moje Umowy` came back as its own "Wczytywanie danych" twice in one session while the owner watched. `still_loading` decides only whether to WAIT longer: a false positive costs a couple of seconds, a false negative hands over the page that was about to contain the answer. Reads do not pay this; a browse action is one of a handful in a flow and the thing being waited for is the point.

`TestWhatCountsAsMutating`, `TestTheControlList`, `TestScrollingIsWhatMakesAThingReachable`, `TestStillLoading`, `TestAPageThatSaysItIsStillLoading` and `TestBrowseDispatch` pin the parts that need no browser; `TestWhatTheModelIsToldItCannotSee`, `TestHowItWasPressedIsNotWhetherItWorked` and `TestChoosingWithoutReadingTheList` and `TestADropdownIsNotThePage` pin what #244 and #245 changed about them, and `TestTheSiteSaysWhereYouActuallyAre` the redirect line.

## The address is the label, and the reply is the change (#251)

Two defects, one session, and they are the same defect: driving a page was addressed by POSITION and answered with the WHOLE PAGE.

**What the owner saw.** He asked for flights on lot.pl, by name, and the trace read `browse_act 0.7s target=15, action=click`. Nobody can review that — not him watching a flow go past, not an approval card, not a session log read back a week later. He decides to press a button by reading its label, and said so: *"Naming as it's labeled is what a human does — I would click it based on name anyway, so approval using this makes perfect sense."*

**What it cost.** Measured on that session (`session-20260821-200525`): nine `browse_act` calls returned **44 788 characters**, ~5 000 each; four `browse` calls another 35 851. Every click re-sent the page text AND the whole control list to report that a dropdown had opened. The tail of that run is the failure the loop guards then blamed on the model: three identical `click target=13` results, a `LOOP_WARNING` at the third, and a pivot off the site the owner had explicitly named.

**The address is what the control SAYS.** `address_controls` assigns it; `resolve` matches it with the same fold as `match_option`, so `przelacz lokal` finds `Przełącz lokal` — that is how it will be typed. The ladder is exact address, folded address, exact name, folded name, then number, then folded substring; it stops at the first rung yielding one control and is deliberately not fuzzy, for the reason `match_option` is not: the thing being picked may be a button that spends money. A name is also a better handle than an index for the reason the index was chosen and failed — it survives the SPA re-render that renumbering never did.

**Duplicates split two ways, and only one of them is a question.** Two nodes saying the same thing AND pointing at the same place are one control the page drew twice — the mobile copy and the desktop copy of one nav link — so they share an address and either will do; asking the model to choose between two spellings of one word is a question with no right answer. Two that say the same thing and go somewhere different are genuinely two, get ordinals (`'Wybierz #1'`, `'Wybierz #2'`) on top of the `detail` already on their line, and a bare `'Wybierz'` comes back as the candidate list rather than a guess.

**An icon-only button is not a nameless button.** The old rule dropped anything with no words and nowhere to go, which took the swap-airports arrow, the hamburger and every dialog's X off the list — on a booking form, half the controls that matter. `nameOf` now asks the page what it calls its own picture: the SVG's `<title>` or `aria-label`, the `<use href="#icon-swap-airports">` id, an `img[alt]`, `data-icon`/`data-testid`, an icon class token, a glyph read as the word it draws (`×` → close, `☰` → menu), and last a nameless-but-unmistakable control described by its neighbour. A stylesheet's name for a picture is still the page telling you what it is; it is just written for a stylesheet rather than for a person.

**The reply is what CHANGED.** A `browse_act` returns a delta: changed text lines with one line of context either side, plus controls added, removed and altered. The page comes back whole only when it is genuinely a different page, when the delta is bigger than `DELTA_MAX_CHARS` (at which point the honest answer and the cheap answer are the same one), when something went wrong, when the model asks with `action="read"`, or every `DELTA_RUN_MAX` reports — because a chain of deltas is a reconstruction and a reconstruction drifts. `action="read"` exists so that looking properly is not a round trip through the address bar; a `goto` to the same URL resets an SPA's state.

**The delta is against what the model was LAST SHOWN, never against the page a moment before the click.** A page that moves on its own — a price that updates, a session that expires — would fall into the gap between two reads and never be reported at all. This is also what makes the empty delta trustworthy: *nothing changed* is then a statement about everything since the model last looked, and it is the dead-control signal delivered on the FIRST click as a fact the page reported, instead of being inferred by a counter three identical calls later.

**Nothing is dropped silently, and no diff decides what matters.** Past the cap the count of unsent changed lines is stated, the way `MAX_CONTROLS` states what it left out. A clock ticking in the corner therefore shows up as a changed line rather than being classified as noise — that is the intended behaviour, not a gap. A diff clever enough to suppress "unimportant" changes is a channel for a page to hide one, which is the whole property the untrusted-content posture rests on.

**A change report stops re-listing what did not change, so the form's submit button is named on every reply.** `_submit_hint` carries `Control.submits` through for exactly this: the submit button is the one control that never changes while a form is being filled, and a model that cannot see it starts hunting.

**The name is re-resolved against the live page immediately before it is pressed, and that is a gate fence, not an optimisation.** The tag survives a re-render — but a framework that reuses a row's DOM node for a different row hands the same tag to different content, and pressing it would be the right element and the wrong flight. So `browse_act` re-enumerates, resolves the name again, and refuses with a fresh page if it no longer resolves. The same read enforces the card: `href` and `mutating` are what the GATE classified from the snapshot the owner was shown, and if the live control disagrees — it needs approval now and did not then, it has become a password field, its destination has changed — the action does not run. The thing the owner approved has to be the thing that happens.

`TestAControlIsAddressedByWhatItSays`, `TestAnIconIsNotANamelessButton` and `TestWhatChangedRatherThanThePageAgain` pin the addressing, the icon ladder and the delta rules with no browser; `TestTheThingApprovedIsTheThingPressed` pins the act-time fence.

### Filling a form is ONE act (`browse_fill`)

A person searching for a flight sets origin, destination, both dates, passengers and cabin, then presses search. Doing that one call at a time cost six model round trips and six echo lines — and on lot.pl it did not finish at all, which is the session that filed this.

**`fill` is the compound verb, and it is the reason the batch is worth building.** On these forms a destination box is not a text field: typing fires an async request and opens a list that did not exist when the batch was composed, so the model CANNOT name the option it will need. A batch of flat primitives therefore dies on the second step forever. `fill` types, waits for the page to answer, and presses the entry that matches — via `match_option`, the same ladder `choose` uses, aimed at what appeared instead of at a `<select>`. Deliberately not fuzzy, for the reason `match_option` is not: the thing being auto-picked feeds a submit the owner approved. An ambiguous or unmatched value STOPS the batch and hands back the candidates, which is one round trip and strictly better than pressing the wrong airport.

**A prerequisite that was a bug in its own right:** `CONTROLS_JS` had no `[role=option]`, so the list a site opens under "Paris" arrived as page TEXT with nothing pressable — the destination field was unfinishable by any sequence of calls. A CLOSED list is `unreachable` and drops out on its own, so enumerating options adds the open one only.

**Only ONE step may need approval, and it must be LAST.** Not card hygiene — abort semantics. With the committing step at the end, a batch that dies at step 7 of 20 has sent nothing. A mutating step in the middle turns every partial failure into a half-sent form. A password refuses the whole batch and never draws a card, exactly as a single action does; over `BATCH_MAX_STEPS` (15) is refused with the way round it, since filling needs no approval and can be split.

**Re-running a stopped batch is cheap but NOT always idempotent, and the difference is `do="date"`.** Typing is: `_type` overwrites, so the same fill twice is one fill — that was the original argument for putting the committing step last, and it was written before the date verb existed. Pressing a day cell is not: a range picker takes the first press as the START of a range and the second as its END, so a retried batch does not begin again, it continues, and quietly sets the wrong half of somebody's trip. A batch that had already set a date before it stopped says so on its ledger, because otherwise the model composes its retry against widget state nothing told it about.

**It stops at the first step it cannot carry out and never skips one.** Order on these pages is a dependency statement, so "did 7, skipped 3, did the rest" is unreviewable against the card. The approved press runs only if every prior step verified — a batch that could not establish its values ends UNSENT and says so, including that the approved press was not made, so the model cannot report a form as sent and the next batch cannot inherit the yes.

**The card is more oversight than the calls it replaces, which is the argument for the whole thing.** Typing has never been mutating (`is_mutating`: nothing is committed until something is pressed), so today a twenty-field form is twenty unseen auto-approved keystrokes plus one card that does not say what it is about to send. The batch card enumerates every value in order, in the control's own words, with long values shortened VISIBLY. `Approved(comment)` is the edit path: the batch is one call, so the comment holds the whole thing and the model re-composes.

**The LEDGER is why the delta is not enough.** A suggestion list opens and closes between two snapshots and nets to zero in the diff, so the page delta structurally cannot report which suggestion aish pressed on the model's behalf. The ledger sits with `_snapshot_notes`, above the untrusted banner, because it is aish's account of its own acts — and it reports what each control HOLDS on readback, not what was asked for, so a mask, an autocomplete rewrite or a `maxlength` truncation cannot land silently.

**One new gate surface, closed two ways:** `fill` presses a control that existed on no snapshot and no card, so the candidate set is only what appeared IN RESPONSE to that step and is marked `option` — a cookie banner rendering mid-step is also "new" — and a match that classifies as mutating is refused rather than pressed. Every step otherwise inherits the single-action live fences unchanged. `browse_fill` is a sibling tool rather than a mode of `browse_act` because a polymorphic tool is what small models get wrong, and every malformed hybrid would be a new parse-and-refuse path in front of the gate; `BROWSE_TOOLS` is the one name set all four dispatch fences read, so a new browsing tool cannot miss one.

`TestFillingAFormIsOneAct` pins the planning, the card and the refusals; `TestFillingAFormAsOneAct` pins the executor — the suggestion press, the ambiguous stop, the live fence and the mid-batch navigation.
## What a cut is allowed to claim (#268, #269, #270, #271)

A 250-row IMDb ratings page broke four things at once, and only one of them was about size. Asked to recommend films from his own ratings, the model read `imdb.com/user/…/ratings/`, saw **items 1-40 of 250**, and answered as though it had read all of them. He caught it, it paged to the last 31 rows, and told him it now had them all — still missing 41-250.

**The harness had told it so, in aish's own voice.** The page text cut appended one fixed sentence — *"page text truncated — the control list below is complete"* — with nothing checking whether the control list was complete. It was not: 2 478 controls dropped by the cap and 101 more closed away, both reported in their own footers four lines below. The `browse` schema said the same thing before the model ever called it (*"The control list always comes back in full"*). This is worse than an ordinary wrong string, and the reason is the untrusted banner: **everything else in a browse result is fenced as attacker-controlled precisely so the model does not believe it, and aish's own narration about the page carries no such fence.** A false statement there is the most load-bearing kind of wrong this output can be. The claim is now made only by the code that can check it — `CONTROLS_COMPLETE` when `hidden` and `unreachable` are both zero, `CONTROLS_CUT` otherwise. `TestACutNeverClaimsMoreThanItKnows`.

**The cut was also a ONE-WAY DOOR, and it was the last one in aish.** `_present`/`_present_snapshot` truncated *inside* `web.py` and returned a string that was already short, so the other 65k reached no cache and no key — while every plugin tool has had a continuation since #192. Asked afterwards whether it could read past the cut, the model described `read_tool_output` correctly and was wrong that it applied to the tool it had just used. Having no way back to bytes aish had held minutes earlier, it wrote an `httpx` scraper, hit an AWS WAF challenge, guessed at sort parameters, and went hunting for IMDb's Compact view.

The join is `Agent._stash_page`, injected as `web.Stash` rather than imported: `web` has to keep working with no agent behind it, so a missing store degrades to the old dead end and never to an exception in the middle of a read. **The shown length travels on the KEY** (`<digest>s<shown>`), because a browse cut is `PAGE_MAX_CHARS` and the backend's `output_caps` is something else entirely — paging against the caps instead of against the cut that was actually made reopens the silent mid-output hole `read_continuation` exists to prevent, from the other end. It rides on the key rather than in a sidecar so the bytes stay purely content-addressed: same output, one file, one thing for the pruner to delete. `TestTheRestOfThePageIsRecoverable`, `TestALongPageCanBePagedInsteadOfRefetched`, and `tests/test_tool_plugins.py`'s paging tests from the store's side.

**The control list is the one half that is never paged away.** An address is resolved against the page in front of the model, so a control on page 3 of a continuation is one it cannot act on and would only be tempted to name. Controls stay on page 1 whatever happens to the text — the same reasoning that already put them *after* the truncation.

**A cut is reported in the page's own units.** `[... 65047 characters omitted ...]` is not something a model can act on, and it is what the session answered over twice. A page that numbers its own rows can be measured in rows: *"12000 of 103783 characters shown, which is items 1-30 of the 250 numbered here"* is not a sentence anything can answer *"yes, all of them"* to. `numbered_span` is deliberately strict — one position out of order and it gives up — because the failure directions are not symmetrical: a false negative costs the notice its best sentence and falls back to characters, a false positive puts a confident wrong claim about coverage in front of the model, which is the failure the notice exists to remove. **The count is aish's own** — the positions it KEPT against the positions it HAD — never the site's claim about its own total, which is page content like any other and sat three lines above the cut saying `1-250 of 281`. `TestNumberedSpan`.

**`topic` now narrows the control list, and that is the only thing that can.** The cap keeps 100 controls in document order, which on that page reached row 9 of 250 — the budget spent on per-row cast links and *Add to Watchlist* buttons. Rows 10-250 were not merely unlisted, they were unactionable, and naming them (#251) does not change that: `resolve` searches the controls that were LISTED, so a row the cap dropped has no address either. The footer already said *"narrow the page first, or say what you are looking for"* and nothing implemented it. Narrowing has to happen at **enumeration** for the same reason: filtering in Python cannot recover a control the cap never tagged. `CONTROLS_JS` therefore collects candidates first and spends the budget second, matching controls first — a plain case-insensitive substring over name and href, deliberately not fuzzy, for the reason `match_option` is not. **Never a hard filter**: the chrome that gets you anywhere — the menu, the next-page link, the view switcher — is exactly what a topic drawn from page content will not match, so unmatched controls fill the remaining room. And the topic reorders the budget without widening it; a narrowing that could raise the ceiling would be a way to spend unbounded context on a page that happens to name the right word. **The re-resolution enumeration is narrowed too**, and forgetting that would have made the feature useless in one step: `browse_act` and `browse_fill` re-enumerate the live page immediately before pressing (#251), so a control only a narrowed pass reaches would have been looked for on an unnarrowed one and reported missing — the narrowing would have found the row and the press would have lost it. They pass `topic or <the name asked for>`; a name the substring matcher cannot see simply leaves the selection in document order, which is what it was before. `TestATopicNarrowsTheControlList`.

**Rejected, with the measurement that killed it:** round-robin across control *families* (title link / rating button / watched button / cast link / …) so the budget spreads over rows instead of piling into the first few. That page has ~10 families, so 100 slots buys 10 each — rows 1-10, against document order's rows 1-9. Real complexity to move the boundary by one row, because the problem is not *which* 100 controls you pick, it is that **100 listed controls cannot address 250 rows.** Only narrowing changes that.

**Moving the cap out of the walk cost a safety invariant its old proof.** `TestHidingAControlNeverRoutesAroundItsCard` used to pin that the over-cap `continue` sat before the `setAttribute`; there is no over-cap `continue` any more. The invariant is unchanged and is pinned in its new shape: the walk only ever COLLECTS and tags nothing, `emit` is still the sole writer of `data-aish-n`, and `emit` is reached only through the capped selection. An element that is not listed is still not tagged, and an untagged element still cannot be acted on.

**An asynchronous download is not a failed one.** The model did the right thing on that page: it found IMDb's own **Export** button and pressed it. IMDb queues the export and publishes it later, so no file arrived, the snapshot said nothing at all, and the model read silence as failure — abandoned the page's own bulk-export path and went off to write the scraper the WAF then refused. This is the general shape, not an IMDb quirk: an invoice run, a data export, a statement all look exactly like a broken download button to a caller who only ever learns *"no file this time"*. `wants_download` labels the control from the snapshot the gate read (never the live DOM — after the press it is a different page), and when nothing arrived, `NO_FILE_YET` says the three things the model cannot work out for itself: that no file came, that this is **not** proof the press failed, and that the next move is to find where it will appear rather than to start scraping. It is a `notice` and not a `problem`, because those mean opposite things — one is how the act went, the other is that it did not go. Only ever consulted when no file arrived, so a false positive costs an advisory nobody needed. `TestAnAsyncDownloadIsNotAFailedOne`.

### A date is a grid, not a field (`do="date"`)

Every booking search stands behind two date fields, and they are not fields: they are readonly boxes that open a calendar. `do="date"` takes an ISO date, opens the picker, and presses the day.

**Driving it is heuristic; the RESULT never is.** The month walk and the bare-number fallback are guesses about a widget aish cannot know in advance, so the field is read back afterwards and the ledger reports what it HOLDS. That posture has one hole worth naming (`TestALedgerNeverStatesWhatItDidNotRead`): `_held` only finds a value on a `field` (only `detailOf` writes `currently:`), and a great many date boxes are a `button` or a `div` showing text. Empty readback and *unreadable* are therefore different answers, and `_readback` says which — the fill path used to fold them together and report the ASKED value as though verified, in aish's own voice, above the untrusted banner.

**The cells are NOT in the page's control list, and that is structural.** A two-month picker is ~84 cells: putting them in `CONTROLS_JS` would blow `MAX_CONTROLS` on precisely the pages this exists for, and the cap drops a control *before* its tag is written — so the date step could not reach the cells it needs. It would also flood the delta on every open and turn every ARIA spreadsheet and seat map into a page of listed controls. `CALENDAR_JS` reads the picker on its own terms, scoped to the grid the field opened, stamping `data-aish-cell` so the page's own numbering never moves under the model. The model never sees a cell; it names a field and a date.

**A day number alone is never pressed.** A range picker shows two months side by side and both have a "7"; pressing one and reading the field afterwards is a coin flip whose result gets submitted. So a cell must resolve to a month — from `data-date` (machine-written, trusted first), from its own `aria-label` ("7 września 2026"), or from the grid's accessible heading. Failing all three, the step stops and says to open the month by hand. This is also why cells must not go through `resolve`: `address_controls` collapses same-name-same-detail duplicates on the theory that they are one control drawn twice, which is true of a nav link rendered for mobile and desktop and false of two months' worth of sevens.

**Month names match by STEM at a word start, Polish and English in one table** — and the word-start part is load-bearing: "wrzesień" folds to `wrzesien`, which *contains* `sie`, so a substring test reads September as August. On a date step that is two months of the owner's trip. A test pins it.

**The month arrow is the dangerous control here, and the reason is a browser default.** A `<button>` with no `type` attribute IS a submit button, and a calendar sits inside the search `<form>` on most booking sites — so a next-month arrow can be a form submit nobody approved. `do="date"` refuses to press anything `submits`, scopes its arrow search to the picker container (a page carousel's "Next" must be out of reach of a control pressed with nobody looking), matches by a short closed vocabulary rather than "contains next", caps the walk at `MONTH_HOPS`, and stops the moment a hop does not change the grid. The ledger counts the hops, because fourteen unattended clicks in the owner's session should be written down somewhere.

**The sight-unseen invariant, stated in full:** a date step may press only a cell inside the grid its own field opened, matching the date its step named, plus that picker's own month arrows — never anything that submits or word-matches mutating. That is a deliberate widening of the `fill` invariant (which is limited to `option`-marked controls that appeared in response to the step), and it is bounded by the scoping rather than by the mark.

**Known gaps, stated rather than discovered:** a picker with no ARIA and no `data-*` (a `div.day` with a React listener) is detected as "opened but no cells found" and stops rather than guessing; a range widget that commits neither end until both are picked will read back empty on the first step, which surfaces as *unreadable* rather than as a false stop; and readback verifies the DISPLAYED value, while what is submitted is usually hidden state — they almost always agree, but readback is evidence, not proof.



### One definition of a finished page, two channels over it

The owner's screen and the model's read ask the same question — *has this page finished changing?* — and used to answer it separately: a watcher polling an activity probe on one side, a one-shot MutationObserver inside `_settle` on the other. Two definitions in one file, and only one of them could be right.

**`page_is_done(quiet_ms, ready, still_for)` is now the only one.** `watch_step`'s letting-go branch IS that call, and `_settle` polls until it returns true, so the view cannot decide a page has finished while a read is still waiting on it. `ready` is load-bearing rather than decoration: a document still parsing is not a finished page however still it looks.

**What differs between the channels is the BAR, not the rule.** How long stillness must last before it is believed is a parameter, and it is a parameter because the two have opposite economics. Waiting costs the view only polls — it already has a picture on screen — so it can hold out for `WATCH_SETTLED_MS` and correct the frame if anything else lands. A read must return something, and most pages it reads finished long ago, so paying seconds on each of them would be paid on every read of the day. Ordinary reads keep `SETTLE_QUIET_MS`; the caller raises the bar when it knows it just did something.

**`started_work` is that knowledge, and it is the caller's because only the caller has it.** A press is exactly the moment a page is most likely to be BUSY rather than finished, so `browse_act` after an action, the last read of a `browse_fill` (whose final step is usually the press that sends the form), and `action="read"` all take the patient bar. `action="read"` doubles as the model's way to say *give it a moment*: a read that came back mid-spinner has one honest next move, and it must not be to keep re-reading until the loop detector stops the task.

**Looking once is what a spinner defeats.** Quiescence stands in for "finished", and a spinner is precisely where those part company — the page is stating that it is unfinished while its DOM sits perfectly still and the animation runs in CSS. The probe adds the signal a single look cannot have (a RESPONSE ARRIVING, which is also the only signal for a lazily loaded image, since it changes an existing `src` and adds no node), and the loop adds the one no signal can replace: asking again, because arrival is late.

**Silence means opposite things to the two channels, deliberately.** A page that will not run the probe is *cannot tell*, never *nothing happened*. The view resolves that by capturing anyway — never miss a frame. A read resolves it the other way after `SETTLE_UNKNOWN_TRIES`: the page that will not run scripting is server-rendered, already whole, and never about to spin, and stalling on it would be a regression paid by every such page.

`TestOneDefinitionOfAFinishedPage` pins that both channels decide with one rule and that the bar is the only difference; `TestLookingOnceIsWhatASpinnerDefeats` pins the loop and the unknown fallback; `scripts/verify_browse.py` presses a button whose results arrive two seconds after the DOM goes still, and the text it asserts on cannot exist before then.

### A results row is what tells two identical buttons apart

Twenty flights are twenty buttons that all say "Wybierz". Ordinals made them addressable (`'Wybierz #7'`) and left them unreviewable — `click element 7` wearing a label, which is the exact defect naming controls existed to end, reappearing at the step where the choice is actually made.

**The digest is a DIFFERENCE, and both halves of computing it are content-blind.** The ROW is found from tree shape: take the lowest ancestor holding every control in a same-label group, and each control's row is the child of that ancestor containing it. No class names, no "looks like a card" — so a `<table>` of `<tr>`, a flex list of `<div>`s and a grid of `<li>` tiles all work by one rule, and an injected ad row is simply a child none of the group's controls lives in. The DIGEST is then what makes a row different from its siblings: a line every row carries ("Wybierz", "Bagaż podręczny wliczony", "Cena od") cannot tell them apart, so exactly those are dropped. It is the same primitive as the page delta, pointed sideways — aish reports *change* by difference and now reports *identity* by difference too.

**Line-level, never word-level.** `640 PLN` and `720 PLN` differ as lines, so both keep their unit; a token-level subtraction would strip `PLN` as boilerplate and hand the model bare numbers.

**The label stays the prefix.** `'Wybierz — 07:45 – 09:10'`: the label is what the control DOES, and a digest without it reads as a fact about the page rather than a button. What follows is what tells this row from the next, so the model asks for it the way a person would say it. **The ordinal is now the fallback, not the default** — it appears only when nothing distinguishes the rows.

**A row is asked for by anything that identifies it.** `resolve` searches the row alongside the address, so `"640 PLN"`, `"LO125"` and `"18:05"` all land. One consequence worth naming: a purely numeric ask no longer dead-ends on "there is no control 640" — on a results page the distinguishing thing about a row is very often a number, so it falls through to the row match before refusing.

**Narrowing searches rows too.** `match` decides which controls the cap buys (#270), and on a list of twenty identical buttons the only nameable thing is what the row says — so the needle is tested against the row as well as the name and href.

**Length is bounded twice and silent nowhere.** `ROW_LINES_MAX` bounds what the PAGE spends collecting (twenty rows are twenty `innerText` reads); `ROW_MAX_CHARS` bounds what the MODEL is charged on the control line, and what it leaves out is counted (`+3 more`), because a silent cut reads like a row that had nothing more in it. The full row is reachable two ways that already exist: narrow to it with `match`, or read the page whole with `action="read"`.

**The row rides the approval card**, which is the point of the whole thing: on a results page the difference between the flight the owner wanted and the one beside it is the price and the time, and a card he cannot check against what he asked for is a card he taps through.

`TestARowIsWhatTellsTwoIdenticalButtonsApart` pins the addressing, the difference rule, the fallbacks and the cap; `scripts/verify_browse.py` drives a real three-row results list in Chrome.

`TestPickingADateFromACalendar` pins the date reading, the month stems, the cell ladder and the arrow vocabulary; `scripts/verify_browse.py` drives three real picker shapes in Chrome — labelled cells, bare cells with a heading, and a month walk — plus a disabled date and a submit-shaped arrow, both refused.

## One page, several chats (#272)

**The browser holds ONE page for the whole process, and until now the PICTURE of it was global too.** aish-web runs every chat in one process; `_Owner` is a module-level singleton and `_submit` schedules every job on its loop concurrently — `busy` is the reaper's counter, not a lock. So a second chat's `browse(url)` does not take a different page: `_browse_page(opening=True)` returns the page that is already open and `goto`s it. It **navigates the page the first chat is standing on.**

On 2026-08-22 two chats ran side by side. One was asked to find flights to the Maldives; the other was reading IMDb ratings. The flights chat spent **225 seconds** on a single `browse_act(type "Maldives" → "To")` and got back `imdb.com/user/…/ratings/?sort=user_rating,desc — you are driving this page`; four calls later a `choose From=WAW` came back on `imdb.com/find/?q=Mindhunter+2017`. It went both ways — the films chat's `read` returned the Qatar homepage, and its model said so out loud.

**Both cross-page acts were refused, and that was luck.** `'To'` matched fifteen controls on the IMDb page (`'Go To IMDb Pro'`, `'Add to Watchlist'`…), so `resolve` returned nothing. The `data-aish-n` tag dies with the document, which is what safely refuses a stale *index* — but controls are addressed **by name** (#251), and a name carries no page binding at all. `'Continue'`, `'Accept'`, `'Search'` resolve perfectly well on a stranger's page.

**The gate consequence was the serious half.** `_approved_browsing` is per-Agent — this chat's yes, to this host. `_browse_host` for `browse_act` read a module-global "last snapshot handed to the model", shared by every chat (both it and its reader are deleted now — see below). The films chat's browse completed at 01:09:59 and overwrote it; the flights chat's gate ran at 01:10:01, resolved the host to `imdb.com`, found it ungranted, and drew — *inside a chat about flights* — the card `drive www.imdb.com in your signed-in browser — aish will open pages and click on them AS YOU, and can see private account data`. Approving it added `imdb.com` to the **flights** chat's grant set. None of this is visible in the session log, because `approve_tool` records `f"tool {name}({shown})"` and drops the `preview` it showed the owner.

**So the view is per-chat: `web.BrowseView`, owned by the Agent** beside `_approved_browsing` and passed down the same seam `Stash` already uses. It holds `shown` (what a change report is a change from), `runs`, and `epoch`. `_browse_host`, `_browse_target`, `_browse_batch_gate` and the delta all read it, so the card can only ever name a control on the page **this chat** was handed.

**An empty view means no page — deliberately not a fall back to the global.** A chat that never opened a page is told to open one, never handed whatever document happens to be loaded. This is also the honest answer after a restart: the process's browse page is gone anyway, and a chat's remembered control names would be stale even if it were not.

**The epoch stops being decoration.** It counts documents a session has driven, `_snapshot` stamps it, and the chat carries the epoch of the page it was **shown**; `browse_act` and `browse_fill` pass it back as `expect_epoch` and the check runs **inside the owner loop, before the page is read** — the gate ran on another thread, so the page can move between the card and the press. A mismatch means the page changed under it. `browse.py`'s standing objection to an epoch handshake still holds and is not contradicted: it argues against one the **model** has to echo. This one is held by the harness and the model never sees it. A caller with no chat behind it — the CLI, `verify_browse.py` — passes nothing and is not fenced.

**`PAGE_TAKEN` is the one ending here that carries no page,** against the rule that every other one is a snapshot so the model is never left holding nothing. The page on the far side of this fence belongs to a **different chat** and may be any account the owner is signed into; handing it over to say "this is not yours" would be the disclosure the fence exists to prevent. It says nothing was pressed, because that is the fact the model needs to not retry blind.

### A tab per chat

Slice 1 stopped a chat **acting** on another chat's page. It could not stop the page being **taken**: `browse(url)` still navigated the one document, so the two chats took turns and the loser was told to reopen. `_Owner.browse_pages` is now a dict keyed by chat, and each entry is a `_Session` — page, epoch, last touched.

**The key lives on the `BrowseView`.** The view is already the chat's identity for the *picture* of the page, so it is the honest place to keep the identity of the page itself: one thing to pass down, and the tab a chat drives cannot go out of step with the snapshot its gate reads. A caller with no chat — the CLI, `verify_browse.py` — uses the key `""` and shares one session with every other keyless caller, which is exactly the single-session browser this used to be for everybody.

**The epoch became per-session with it, and had to.** Shared, every other chat's act would move the counter and this chat's fence would fire on a page nothing had touched — a refusal that protects nothing and blocks everything.

**A tab per chat is the point; a tab per chat the owner has ever opened is not.** `MAX_BROWSE_PAGES` (6) is judged against the same 16 GB roof as the idle timers: enough that two or three parallel flows never notice it, small enough that forgotten tabs cannot accumulate into a second Chrome's worth of memory. `_evict_stale` drops dead and idle sessions first and only then the least recently touched, and the evicted chat gets `NOTHING_OPEN` on its next act — the answer the reaper has always given. `held()` keeps the browser while **any** chat is mid-flow, and `_close()` empties the dict, so a reaped session cannot read as open (#248, whose module-global proof is gone with the global).

**Downloads had to be attributed, not just drained.** One listener sits on the CONTEXT because the tab that downloads is very often not the tab that was clicked (#246), and the old rule — "everything that is not some read's page belongs to the browse session" — had exactly one browse session to give it to. With two tabs open it would have handed one chat's invoice to whichever snapshotted next. A snapshot now takes its own page's downloads plus anything belonging to no read and **no other chat**, which keeps the ephemeral `target=_blank` popup counting without letting it land in a stranger's chat.

**`browse_close(key)` finally has a caller,** which is how it stopped being dead code: it ends one chat's session and leaves the rest alone.

**Serializing was rejected.** It refuses what the owner demonstrably does — two browsing chats at once, the night this was filed — and with nothing calling `browse_close` a lease would have been held until the ten-minute idle reap, starving every other chat with no recourse.

`TestTwoChatsDoNotShareOnePageView` pins the interleaving and the card; `TestAPageAnotherChatTookIsNotActedOn` pins the fence — that it runs before enumeration, that a refused act does not count as a document, that it leaks no page, and that the ordinary single-chat case pays nothing; `TestAChatGetsItsOwnTab` pins the tabs — two chats, two pages, per-session epochs, the keyless caller's shared tab, the bound and who it evicts, and where a download goes.

## The browse gate — two questions, not one

Reading a signed-in site carries the owner's session. Driving one carries it AND presses things with it, which is the largest blast radius aish has: a button can pay a bill, cancel a contract, or delete something. So the gate is part of the feature and not a follow-up.

**May aish drive this host at all** — asked once per host per task, session-scoped like every other grant (L4), because a card per click is a card nobody reads and a flow through one portal is twenty clicks. The card says it acts AS HIM, because that is the part he is agreeing to.

**May it press THIS control** — asked every time for anything that spends, ends or deletes, and named: `click button 'Zapłać' on eon.pl`, never `click element 7`. The match is a broad, dumb word list (Polish first — that is the owner's web) plus every form submit, because the nondescript "Dalej" that posts the form is the dangerous one and the obvious "Zapłać" is not. It costs a prompt when it is wrong and costs a paid bill when it is missing; `approval.py` settled that trade the same way years ago.

**A SEARCH is not a commit, and gating one was an inconsistency the file already argued against.** The submit rule read "every form submit", because `submits` was the only proxy for *commits* available — and the nondescript "Dalej" that posts a purchase is real, and caught by nothing else. But the paragraph above it already said a plain navigation is never mutating, *"an `<a>` with a real http href is a GET to another page — precisely what `read_url` does under auto-approval"*. A GET form submit IS that: a link with the query typed into it. aish would follow `?from=WAW&to=CDG` as an anchor without asking, then ask permission to press the button that builds the same URL.

The cost of that was not inconvenience, it was **the gate's own value**. A card that fires on nothing trains the owner to tap through, and the tap he learns is the one waiting on the purchase; a gate is worth exactly as much as its false-positive rate is low. So the rule is narrowed by HTTP's own definition of safe rather than by a guess — and only on an **explicit** `method="get"`. A form nobody wrote a method on is not a statement that it is safe (on an SPA it usually means JavaScript intercepts and posts), so absence stays gated, and the raw attribute is what is read: `form.method` reflects the spec default and would report exactly that ambiguous form as a GET. A button's own `formmethod` wins, as the browser resolves it.

Two things bound the change. The word list still runs, so a GET form whose button says "Usuń" is caught by its name. And the blast radius is smaller than it sounds: a JavaScript-driven "Szukaj" with no real `<form>` has `submits = false` already and was never gated, so this reaches classic server-rendered search forms — the ones where aish was asking permission to do a GET it would have done unasked as a link. `TestASearchIsNotACommit`, and `scripts/verify_browse.py` drives two same-shaped submits whose only difference is the method.

**A password field is refused outright and never draws a card.** aish types the owner's credentials nowhere, and this is the last door that could have started. There is no yes that makes it a good idea, and offering one would teach him there is — he is handed `/browser <host>` instead, which is the same answer the whole feature gives.

With no approver it fails closed, like the login gate. The echo line is not decoration either: the owner grants a host once and then watches a flow go past, so `→ browse: click button 'Przełącz lokal'` is his only running account of what aish is doing inside his account. `TestBrowseGate` pins all of it.

Two fences worth keeping: browse is **not** in `READ_ONLY_TOOLS`, so it never rides the parallel read path — one page, one session, one action at a time, or two clicks land on one document in an order nobody chose. And `web.browse` keeps the same SSRF fence as `read_url`: a driven page is still a model-chosen URL, and this one can click.

## The document at the end of the flow

Driving the portal gets you to the invoice; it does not get you the invoice. The E.ON session ended holding real `…/ebokapi/GetDocument?objectId=…` URLs it could do nothing with, because `read_pdf` and `fetch_binary` go through the anonymous opener — the same identity gap #236 closed for reads, one tool over. So `accept_downloads` is now **True** (it was deliberately False), and a click that produces a file saves it under `~/.local/state/aish/browser/downloads`.

**Stashed by a sync handler, saved inside the job.** The `download` event gets a plain callback that appends the object; the awaiting and writing happen in the action's own coroutine afterwards. The two obvious alternatives are worse: an async handler needs a task of its own on a loop this module is careful about, and wrapping every click in `expect_download` makes every *ordinary* click pay that timeout.

**The listener goes on the CONTEXT, not on the page** (#246). Bound to the one tab aish opened, it caught nothing on the site it was built for: E.ON's `Pobierz e-fakturę` is a `target=_blank` link, so Chrome opens a fresh tab, starts the transfer, and closes the tab — the file arrives in a tab nobody is listening to, and the snapshot faithfully reports that we are still on the invoices page. Four clicks across two sessions, four pages back, no file, after which the model went hunting the filesystem for the browser profile and was denied four times. `_Owner.watch_downloads` is registered for every page the context ever opens, and `take_downloads` decides afterwards whose a file is: a read takes its own page's, and the browse session takes everything that is not some read's page — which is what makes an ephemeral popup count.

**A read that lands on a file keeps the file.** Chrome does not render a `Content-Disposition: attachment`; it downloads it and leaves the navigation aborted, so `read_url` saw an empty page, fell back to an anonymous fetch, and told the owner it could not obtain his invoices — while holding all seven of them. `Page.downloads` carries what a read produced, `_browser_read` returns it as the answer, and the host is written into `BROWSER_HOSTS` because a file is proof the browser was the right instrument.

**The site never chooses where the write lands.** `safe_filename` strips separators, parent references and leading dots rather than escaping them: the suggested filename is page content like any other, and here the instruction would be a path. Two bounds, neither precious: one file may not exceed `DOWNLOAD_MAX_BYTES` (checked *after* the write, because Playwright streams to its own temp file and reports no length beforehand), and the directory is pruned oldest-first to `DOWNLOADS_KEEP_BYTES` — this box runs a Home Assistant VM against a 16 GB ceiling and a year of monthly invoices is a real amount of disk.

Pruning is oldest-first rather than the media store's content-addressed LRU, deliberately: these are the owner's own documents under their own names, and the name is the only handle he has on one.

`TestWhoseDownloadIsIt` pins who a file belongs to and `TestTheSessionOutlivesTheOwnerReading` the reaper's second flow. **And the file is handed to the OWNER, not just described to the model** (`TestTheFileIsHandedOverNotDescribed`). Seven invoices came down in one real session and he was told a folder name; the model then reached for `file://` on its own, which is dead on a web page. `downloaded_note` now carries the markdown line that renders as the file itself, built here rather than left to the model for the reason `show_image` builds its own — a bracket or a newline in a name the SITE chose would silently break it. The web app's half is `[FILE-LINK]` in `docs/web-frontend.md`, and `/download` serves this folder because he asked aish to press the button that produced the file.

The directory is in `Agent.workspace_roots`, so `read_pdf` may open what `browse_act` just named. Without that the tool would name a file and instruct the model to read it, and the read would cost an approval tap — the #220 asymmetry, reopened. `TestDownloads` pins the naming, the bounds, the location and that last seam.

Not yet done, and tracked on #237: the file is not surfaced in the web UI as an attachment the owner can tap. Today he gets the path and whatever the model reads out of it.

## Listing only what could actually be pressed (#244)

The first week in production said the numbered list was the easy half. Six of sixteen actions across eon.pl and qatarairways.com died on one Playwright verdict — *scrolling into view if needed → done scrolling → element is outside of the viewport*, retried for the full 45 seconds — on an ordinary nav link, on a property switcher, on an entry in a menu that had just been opened, and on a plain text field.

The old test was "has a box, is not `display:none`, is not `opacity:0`". So is a mobile drawer parked at `translateX(-100%)`. So is a folded accordion at `height: 0; overflow: hidden`. So is everything below the fold of a page whose body a dialog has pinned with `position: fixed`. Playwright agrees all of them are visible, scrolls, finds them still outside the viewport, and keeps trying.

**The question the predicate answers is: could the owner put this on screen and press it right now, using nothing but scrolling?** Below the fold is yes — you scroll to it. `left: -9999px` is no. Inside a dropdown that is scrolled past it is yes; inside an `overflow:hidden` drawer is no. The distinction is *scrollability*, not position, which is why this can be neither `elementFromPoint` nor an `IntersectionObserver` — both answer "on screen NOW", and below the fold would fail them.

`REACH_JS` is the predicate, in order, cheap first: the page's own statements that something is not interactive (`inert`, `aria-hidden`, `hidden`, a closed `<details>` — with its own `<summary>` exempted, because that is the thing you press to open it), then one `checkVisibility({checkOpacity, checkVisibilityCSS})`, then geometry. The geometry walk is where the drawers die: a `position: fixed` ancestor does not move when anything scrolls, so its contents are reachable only if they are on screen already — and `getBoundingClientRect` is transform-inclusive, so `translateX(-100%)` reports where the drawer really is. A clipping ancestor that the user can scroll gets a range check; one they cannot must already contain the element. **From a clipping ancestor upward, the box that travels is the CONTAINER's, not the element's** — once the container is scrolled, the element appears within it, so that is the honest question to ask of everything above. Intersecting the two was wrong in exactly the case that matters: an entry below a dropdown's scroll fold has no overlap with the visible container, so the intersection came out inverted, bottom above top, and every ancestor test after it was asked about a negative-height box. On eon.pl that hid the fifth of five properties in the account switcher — four listed, one counted as closed away — and the model went straight back to guessing a URL for it, which is the behaviour this whole predicate exists to end (#251). **The overlap test requires a real two-pixel intersection, not a shared edge**: a folded accordion is `height: 0`, so its content's box starts exactly where the container ends, and a touching test called that visible.

**What is excluded is COUNTED, never dropped.** `[19 more control(s) are on this page but closed away — in a collapsed menu, an off-screen panel, or behind a dialog. Press whatever opens them first.]` That sentence is the whole instruction a small model needs: it turns "the thing I want is not listed" into "find what opens it" rather than into "this page does not have one", which is what sends it back to guessing URLs.

**One rescue, and it is worth its cost:** a native checkbox hidden under a styled `<label>` — every custom toggle on the web — fails the predicate correctly, because the input really is invisible. When its label passes, the LABEL is tagged and listed with the input's state. Clicking a label toggles its input through the browser's own activation, so this is a real gesture and not a synthetic one. Without it, consent and preference pages lose every control they have.

**The gate invariant to keep** (`TestHidingAControlNeverRoutesAroundItsCard`): an element that is not listed is not tagged, and an element that is not tagged cannot be acted on — acting resolves `[data-aish-n="N"]` and nothing else. So hiding a control can only ever remove capability; it can never route around a card. The over-cap `continue` sits *before* the `setAttribute` for the same reason.

**A page is often several documents.** `page.evaluate` reaches the main frame only, so a consent wall, a login form, a card field or a chat widget — all iframes — did not exist as far as browse was concerned, and the model was told "no controls found" about a page visibly full of them. Enumeration now walks `page.frames` with the numbering continuing across them, so `[14]` means one thing on the page however many documents it is made of, and acting searches the frames for the tag. Bounded at `MAX_FRAMES`, because an ad-heavy page carries dozens and each is a round trip. One asymmetry: the link fallback below navigates the TOP page, so it is offered only for a control in the main frame — following a consent iframe's link at the top level would be the wrong surface.

## Pressing: a ladder, not a timeout

One 45-second click used to be the whole act path. Now every stage is bounded at `ACT_TIMEOUT_MS` and falls through in seconds, so the worst case is about ten rather than forty-five — and 45 was never a sane bound for "become clickable" anyway. It stays where it belongs, on `goto` and `wait_for_load_state`, where the bound is a page being slow rather than an element being stuck.

**Preflight first, and it is the cheapest win here.** The tag outlives the reachability: `_settled_text` waits, the model thinks, and a menu that closes on scroll or on a timer leaves its entries tagged and unpressable. Asking `REACHABLE_JS` about the one element costs ~50 ms and replaces a 45-second timeout with a sentence naming what closed. Then `scrollIntoView({block: 'center'})` — the middle of the viewport is the one place a sticky header is not, and the JS scroller takes a different path from the CDP scroll that has been silently failing to move anything.

Then: a real click. Then the **keyboard**, which is not a workaround — it is the other first-class way people press things, it fires trusted events, and `focus()` scrolls natively inside containers Playwright's scroller cannot move. Focus is verified before Enter is pressed, never assumed: a blind Enter after a focus that did not take goes to the document, and on a page with a form that is a submit nobody asked for. Then, for a link, **the destination the page itself declared** — the href was read off the DOM at enumeration, travels on the snapshot's `Control`, and is the same fact the gate used to classify the control as a plain navigation, so following it is the approved act by another route. It is not the URL guessing this project forbids, because nothing about it was remembered or composed.

**`force=True` appears nowhere, at any stage, ever.** It clicks a COORDINATE and presses whatever is on top of it, which makes it the one press that can land on a control the owner never approved. A dispatched event is the opposite — it activates exactly the element the model named and the gate saw — but it is still a lie about physics, so it is last, it is never used on something that spends or deletes, and the snapshot says it happened. `notice` carries that, kept apart from `problem` because they mean opposite things: one is *how* it was done, the other is *that it was not*.

**Every ending is a page.** A bare error string used to leave the model holding no page at all, which is how one stuck control turned into a lost session and five denied `find` commands. Now a stale index, an unreachable control, an ambiguous choice and an outright refusal all come back as a snapshot with a line saying what happened.

## A dropdown is not the page

An airport picker is 312 options and a country-code picker is 250. Inlining them spent the control budget on data the model does not need until it chooses: on one qatarairways.com snapshot the option dump pushed **51 real controls off the end of the list**. A `choice` line now carries what the owner can see without opening it — how many options, and which is selected — with the full list inlined only up to `CHOICE_INLINE_MAX`, because a yes/no/maybe select is better read than counted.

**The bigger half is the page TEXT, not the control line.** `inner_text` includes every option of a *closed* `<select>` — measured on one fixture, 3 500 of 4 176 characters, 84% of the page. `strip_option_floods` takes the whole contiguous block back out and leaves a count, and it does that as a block rather than line by line on purpose: the page's own sentence "Faktura dla: Polska" is not the dropdown's option for Poland. A block that does not match exactly is left alone, so the worst it can do is nothing.

The options are fetched at CHOOSE time and matched in Python, where the tests are. `match_option` is a ladder, tightest first — label verbatim, label folded, value verbatim, folded substring — and it stops at the first rung yielding exactly one. Folding is NFKD plus a hand-mapped `ł`, because this is the owner's web and "Lodz" has to find "Łódź".

**Deliberately not fuzzy.** Edit-distance matching silently picks, and a `choose` is very often followed by a form submit; "Iran" quietly standing in for "Irak" is exactly the class of wrong this module exists to prevent. Ambiguity and no-match both fail loudly *with the candidates* — which is what makes collapsing the list on the snapshot affordable in the first place: the failure message is the option list the model needed. A `role=combobox` with no `<option>` children is a search box wearing a dropdown's clothes, and says so instead of spending two `select_option` timeouts finding out.

Custom dropdowns need none of this. Their popup entries are unreachable while closed, so they no longer flood the list; once the model presses the thing that opens them they are ordinary numbered controls.

## What the fake portals are for

`scripts/verify_browse.py` serves two local pages and drives them with a real Chrome and a throwaway profile. It is not in the pytest suite (conftest forbids launching Chrome on purpose) and it is where every real bug in this feature has been found — the unit tests were green for all of them.

One page is shaped like eon.pl/mojeon. The other is every way a control can be listed and unpressable, each fixture taken from a session that failed: a nav rendered twice with the mobile copy off-canvas, a five-entry scrollable list inside a fixed header, a folded accordion, a dialog that pins the page, a checkbox hidden under a styled label, a 250-option select, a button under a transparent sheet, a control inside an iframe, a `Content-Disposition` download behind a `target=_blank` link, and a URL that redirects. It also asserts the ladder is *bounded* — a covered button must be pressed in seconds, not in the time it takes to give up.

A third page was added with #270: a 250-row numbered list whose rows each carry several controls, so it blows the page cap and the control cap at once. It is the only place the two-pass selection is actually executed — everything in `tests/` inspects the SOURCE of `CONTROLS_JS`, which cannot tell you whether narrowing reaches a row document order never gets to. Measured there: 100 controls listed and 902 not, the cut reported as *"items 1-30 of the 250 numbered here"*, and `topic="Interstellar"` putting row 137's link at `[0]` — pressed, and it navigates.

One more guard came out of building this: every injected script is node `--check`ed by `TestTheInjectedJavaScriptParses`. A backslash that survives one layer of quoting and not the other is a syntax error the PAGE reports and nothing else does — `FLOOD_JS` shipped for an hour with a literal newline inside a string literal, the `evaluate` threw, the caller swallowed it as "a page that will not answer", and the only symptom was that nothing happened.

Four bugs it caught that nothing else did: `el.href` resolving `href="#"` to the page's own URL, which would have walked `<a href="#">Zapłać</a>` past the gate; stale `data-aish-n` tags giving two elements one number; the shared-edge overlap that called a folded accordion's contents pressable; and the inverted clamp that hid whatever sat below a dropdown's scroll fold.

## The remote view — because this Mac is headless

`open_for_login` opens a window **on the Mac**, which assumes somebody is sitting at it. The owner is not: mm is a headless server they reach as a PWA from a phone, so that window is one nobody can get to. The remote view is the same act done remotely — aish screenshots its own browser, the owner taps and types in the PWA, and each action returns a fresh frame.

**Pixels, not a proxy, and that was a considered rejection.** The obvious-looking design is to proxy the site through aish's own HTTPS origin. It breaks comprehensively: the site's `Set-Cookie` carries its own `Domain` and lands on the wrong origin; an SPA builds URLs at runtime (`location.origin`, `fetch('/api/…')`) where no rewriting pass can reach them; CSP, service workers and OAuth `redirect_uri` validation are all origin-bound; and DataDome would refuse the odd-looking request anyway. Shipping the rendered page sidesteps every one of those.

It also sidesteps the deeper constraint, which is the real reason no proxy variant works: **the session must be created in the browser that will later use it.** Even a clean forward proxy leaves the cookies in the owner's phone, which is the wrong browser — and a DataDome cookie copied across is bound to the fingerprint that obtained it.

**A frame per interaction, not a video stream.** A login is about six round trips; a phone on a mobile connection would rather send six JPEGs than a screencast. `view_open` / `view_act` / `view_close` are request-response, and `bvSend` refuses to send a second interaction while one is in flight, so frames can never arrive out of order. If smooth browsing is wanted later, the screenshot and input plumbing is the same foundation a screencast needs — an upgrade, not a rewrite.

**The view gets its own context, at a fixed viewport.** Reads run at the real window size, which cannot give stable coordinates; the view pins `VIEW_WIDTH`×`VIEW_HEIGHT` so a tap at (x, y) in the PWA means that point on the page. Chrome locks the profile, so the read context is closed first — the same handoff `open_for_login` makes.

**Nothing typed through it is ever recorded.** The owner puts real passwords through this path, so the WS action and its arguments are handled and dropped: no trace step, no session entry, no status line. `TestBrowserView::test_typed_text_is_never_logged` greps the session log for the typed string. The client clears its own input the instant it sends, because a value left in a DOM node outlives the sheet.

**No site markup enters the app.** The frame is an `<img>`, and that is the whole rendering surface — a hostile page cannot script the PWA or reach the access token, which a proxy that injected its HTML into aish's origin would have handed it outright.

The coordinate mapping is the piece most likely to be quietly wrong, so it is pinned hardest (`tests/js/test_browser_view_coords.js`). `object-fit: contain` letterboxes the frame, so mapping a tap against the ELEMENT box rather than the RENDERED image is off by the letterbox margin — increasingly so toward the edges, and worst on exactly the small login field the view exists for. The test drives the shipped function through exact-fit, vertical-letterbox, horizontal-letterbox and offset-element cases, and asserts a tap in a letterbox bar is REJECTED rather than mapped into the page.

## What an adversarial review found (2026-08-14)

A model-driven design review caught four things worth recording, because each was a case where the code looked right and the *property* was not:

**A login the owner never explicitly ended was never recorded.** `view_close` was the only writer of `logins.txt`, so the host was captured only if they tapped Done. A phone PWA normally ends a session by being backgrounded or losing the network — so the usual outcome of a *successful* login was cookies persisted (the login worked) and the host never recorded, which meant the login gate never fired for it and the model could read that live account with **no approval at all**. That inverts the one property the gate exists for. Hosts are now recorded by `_note_visit` the moment they are visited, which errs toward gating a site merely *visited* — the safe direction, undone by `/browser forget`.

**The idle reaper killed exactly the login it was built for.** Frames are sent only on interaction, so an open view is silent by design, and the owner routinely goes quiet for minutes mid-login waiting on a 2FA code. At `IDLE_SECONDS` the reaper closed the context under them. It now skips a live view — but an open view cannot suppress the reaper forever or a vanished client would hold Chrome and the profile lock indefinitely, so `VIEW_MAX_IDLE` caps it and the server closes an orphaned view when its socket drops.

**A wall has text, so "it produced text" was not "it produced the page".** `is_challenge` now rejects a short body carrying a block status or known interstitial wording. This is the original failure rebuilt one layer up: without it the model reads "verify you are human" as the shop and answers from it. Length decides first — the measured allegro.pl case is 403 with 23 000 characters of real prices and MUST stay a success, and `captcha` appears on plenty of genuine pages.

**A quick-reply chip could run a slash command.** Chip payloads are model output, attacker-controlled under injection, and a chip sends on one tap — so a hostile page could render a friendly *"Sign in to continue"* button that opened aish's own login sheet at a credential-harvesting URL, borrowing exactly the trust this sheet is built to have. Chips are now messages only (`[CHIP-NEVER-COMMANDS]` in `docs/web-frontend.md`); slash commands must be typed by the human.

Also fenced: the browser is **disabled on preview** (`AISH_PREVIEW=1`), because `scripts/aish-preview.sh` shares production's state dir and therefore this profile; a read arriving while the owner is driving the view now refuses rather than borrowing their phone-sized context and returning a mobile layout as if it were the page.

## A wall wearing an auth code: the status list that was two lists (2026-08-21, #257)

Ticketmaster Poland answers a plain fetch for an **event** page with `401 Unauthorized` and a three-word body, `{"response":"identify"}`. Nothing is signed into and nothing needs to be: it is an edge bot-manager refusing an unrecognised client, the same wall Allegro puts up as a 403, worded as an auth code. Its **search** pages are served to a fetcher normally, complete with listings and event links — which is why the failure read as a partial outage rather than a wall, and why the model kept trying.

The escalation never fired. `read_url`'s trigger was a private copy of the block-status list holding only 403/429/503, so a 401 fell through to the plain error path: eight event reads failed in **0.2 seconds each**, Chrome never started, and `BROWSER_HOSTS` never learned the host — so the eighth attempt took exactly the same dead path as the first. The owner then pasted one of those URLs into `/browser`, in the same Chrome on the same profile, and got the full page: dates, section prices, resale seats. Escalation is proven to work here; it was simply never asked.

The fix is not "add 401". `is_challenge` already knew 401 was a wall — the two halves of the read path were asking the same question from opposite ends (*the browser's verdict on a page it rendered*, *the fetch's verdict on a status it got*) off two lists that had drifted by 401 and 405. **`browser.BLOCK_STATUS` is now the one authority and `web._BLOCKED_CODES` is that name**, not a second copy to keep in sync. 404 and 500 stay out, as before: nothing is there to render, and a Chrome launch to prove it costs seconds. `TestReadUrlEscalation`.

## What the first live test found (2026-08-14)

The owner's first real run of the shipped feature produced **zero browser renders** in the whole session. The escalation was wired only to `HTTPError` in 403/429/503. Allegro answers a plain fetch with a prompt 403 *usually* — but the model had just hammered the address with a hand-rolled Python script (three requests, three 403s), after which Allegro stopped answering altogether, so `read_url` died on a socket **timeout**, fell into the generic handler, and returned the error without ever reaching the browser. The capability worked perfectly when invoked directly; nothing ever invoked it.

Two changes follow. **A host that stops ANSWERING a plain fetcher is the same problem as one that refuses out loud**, so `_worth_rendering` escalates on timeouts and dropped connections too — but not on DNS failures, where there is no host to render and Chrome would cost seconds to prove a typo is a typo. And `BROWSER_HOSTS` remembers, per process, which hosts have needed the browser, so the second read of such a host skips the doomed fetch entirely rather than putting a possible tarpit on the critical path again.

The session also showed the model reaching for a **self-written Python fetcher** before trying `read_url` at all — curl with extra steps, on a page `read_url` now handles, and the owner has denied that shape of thing three times across two sessions. The system prompt now forbids fetching a page by any other means in any language, and says to read a shop's own listing URL rather than web_search'ing for it: the search engine returns its index, the shop returns today's prices.

## The second live test: a wall, mistaken for one, described as another (2026-08-14)

The browser worked — two of three Allegro offer pages came back marked *rendered in the browser*. The third did not, and the run still ended with the model reporting that Allegro cannot be read. Two separate faults, both about the SHORT page:

**A half-painted page looks exactly like a wall.** Reads serialise through the one owner thread, so the third read in a turn starts on a machine already driving two browsers (measured: 7.0s, 11.5s, 14.3s). At `SETTLE_MS` the listing had not finished painting, `is_challenge` saw a short body with a 403, and rejected real content. A thin page now gets one more settle before it is judged — a wall stays short, a slow page fills in.

**And the failure then described itself as something else.** `_browser_read` returned a bare `None`, so the caller fell back to *"the site may block simple fetchers — you may retry ONCE via r.jina.ai"* — untrue once the browser had already tried and been walled. The model followed the advice: two more calls, one a 22-second timeout, then the conclusion that the site was unreadable. It now returns a REASON, and a wall says so and explicitly forbids the third-party reader, which fetches from a datacenter with no session and is strictly weaker than the browser that just failed.

The general lesson is the one that keeps recurring here: **a fallback message is an instruction**, and an inaccurate one costs more than no message. `TestAThinPageGetsASecondChance`, `TestChallengeDetection`.

**And then the run failed anyway, in the model rather than the code.** Its final answer told the owner that *"Allegro's servers very effectively prevent automated reading"* — in a turn where it had successfully read **three** Allegro pages, two in the browser and one through Jina, with real prices in them. It reported the failures, generalised them over the whole site, discarded the data it already had, and answered from other shops. From the owner's seat that is indistinguishable from the feature not working, which is exactly what he reported.

No amount of fetching fixes that, so the reading contract in `SYSTEM_PROMPT_TEMPLATE` now states it directly: a result marked *rendered in the browser* WAS read and must be used; some pages failing does not make a site unreadable; and it must not write that a site blocks automated reading in a turn where it read a page from that site. Telling the owner a source failed when it succeeded is worse than the original failure, because it throws away the answer too. `TestTheReadingContract` pins each clause, because each one was written against a specific thing that happened and a tidy-up of the prompt would bring it straight back.

## The half of the page written for machines

`Page.declared` carries the page's schema.org JSON-LD, raw. It lives in a `<script>`, so `inner_text` cannot see it — and that blindness was structural: the reader takes what a *person* sees, so the only thing left to infer a price from was **where it sat on the page**, which is how a sponsored tile's figure ended up in an answer as the product's price.

Measured live on the offer in the session that filed it: two blocks, a page description and a product one, the second declaring an offer at price 63.19 PLN with availability OutOfStock. That is the correct price — the one the model needed eight reads to find — plus the fact that the offer was **dead**, which it never found at all.

The cap is in the JS, not in Python: a page can declare a 500 KB `@graph`, and the size is known at the point of reading it, before any of it is carried across the thread boundary. What the blocks MEAN — which offer belongs to this page, an aggregate range versus a price, agreement with what is rendered — is decided in `web.py` and documented in `docs/agent-core.md`; this module only hands over what the page said. `TestLinksInTheText`.

## A page without its links is not the page, when the page is a shop

`read_url` rendered a listing to plain text, and `page.inner_text('body')` throws every `href` away. Fine for an article. On a shop it discards the answer: the model could see that an offer costs 34,99 zł and had **no way to say where it was**.

So it did the only thing left — took a title it had just read and searched for its own URL:

```
web_search "ZAWIESIE CZARNE WĘŻOWE BEZKOŃCOWE 2T L-0,5" "allegro.pl/oferta/"
web_search site:allegro.pl/uzytkownik/R_pas lina karabińczyk szekla
```

**66 of 113 searches in one session** (`session-20260814-131203`) are that pattern, in a session where 51 Allegro reads *succeeded*. The system prompt already said, in capitals, `You MUST NOT use web_search with a site: filter to browse a shop`. It lost every time, because **an instruction cannot beat a missing capability** — the model was not disobeying, it was routing around a hole. The owner's own words for the result: *"nie dawaj mi instrukcji, jaką szukać, tylko bezpośrednie linki do ofert"*.

What the probes found on the listing behind that session:

| | measured |
|---|---|
| `<article>` cards on the page | 72 |
| anchors inside them | 166 (94 with text) |
| offer URLs recovered by the old reader | **0** |
| shadow roots | **0** — the links were in the light DOM all along |

**The URL goes ON the line, not into a list beside it.** A separate link list leaves the model to join offers to URLs *by title* — which is precisely the guess-the-URL step this removes. Merged, an offer's URL sits next to its own price and there is nothing to match up. Anchors are consumed **in order**, so a listing that shows the same title twice (a sponsored card and its organic twin) gives each line its own URL instead of pointing both at whichever came first.

**A card's href is routinely a click tracker, not the offer.** Sponsored cards link to `allegro.pl/events/clicks?…&redirect=<the offer, urlencoded>&sig=…` — citing the ad system instead of the product, at ~250 characters a link. `web.clean_link` unwraps it and strips the `bi_*` campaign parameters left on the far side. **Two encodings, found on the same site in one run**: `/events/clicks` percent-encodes its target and `/dss-proxy/clicks` base64s it, so the second survived the first fix and still arrived as an unusable tracker — only a live read showed it. An encoding not recognised is simply not unwrapped; a tracker URL is ugly, not wrong.

Unwrapping **stops at a host change**, deliberately: a redirect off-site is a different claim about where the user is being sent, and rewriting the citation to it would make an injected `?redirect=` an open door into the answer.

**The cap is where the links died, and `<main>` alone does not save them.** A read is capped at `DOCS_MAX_CHARS` = 6000, and a listing merges to ~20 000 characters carrying **101 links** — so the cap lands mid-page and takes the URLs with it. Measured end to end through the shipped `read_url`:

| | chars in | offer/product links delivered |
|---|---|---|
| what shipped before | 6 563 | **0** |
| links, no `<main>` | 6 563 | 13 |
| links + `<main>` | 6 563 | 14 |
| **links + `<main>` + carried note** | 12 441 | **48** |

`<main>` is worth keeping — it removes the category nav and the cookie wall outright, so the read now opens on the search results instead of 2 500 characters of chrome — but it is worth about **one** extra offer, not ten. An early prototype measured it at 25-vs-15 by counting nav links as wins; the shipped path excludes those, and the honest number is the table above. Do not restore that claim.

What actually recovers the links is `web.link_note`: the pairs truncation cut off, appended **after** it, exactly as `image_note` already does and for exactly the same reason — on a "which offer" task the URLs *are* the read, so letting a character cap bury them cuts the one thing that stops the guessing. It is bounded by a **character** budget rather than a link count, because a count caps nothing when a shop's URLs run to 120 characters and an encyclopedia's to 60.

**Carrying the links is the cheap way to recover them, which is why that budget is generous where `DOCS_MAX_CHARS` is not.** Raising the page cap instead buys the same links plus the card boilerplate wrapped around them — the repeated *SUPERCENA / dostawa we wtorek / star rating* under every card:

| | chars | links | chars per link |
|---|---|---|---|
| note budget 2 500 | 8 891 | 30 | 296 |
| **note budget 6 000** | **12 441** | **48** | **259** |
| whole page, uncapped | 34 231 | 101 | 339 |

The cost being protected is **context**, and the thing that makes it bite is that a tool result is re-sent on every model call for the rest of the task — not bandwidth, and not the remote view's frames, which never enter the model's context at all. It is weighed against what it replaces: 30+ searches in one turn at 1–2k characters each. **One read that ends the searching is cheaper than the searching**, which is the whole reason this is worth spending context on.

The read viewport was on the plan too, and was dropped: 1440×900 and 1280×2134 returned **byte-identical** text, because a listing renders its cards regardless of window height. The tall-viewport arithmetic that the remote view is built on does not transfer to reads.

Two fences on `<main>`. It is applied to what is handed **back**, never to what `is_challenge` **judges** — narrowing the text a wall is detected in would move thresholds measured on whole bodies, and a page wrongly called a wall is the expensive failure here. And a `<main>` holding less than half the body is **refused**: that means the site puts its content elsewhere, and preferring it would silently drop the page. Over-trimming is not a cautious version of under-trimming — same shape as the login-recording mistake above.

The extraction walks shadow roots (they were empty here, but `querySelectorAll` not piercing one is exactly how a site's cards would vanish again) and skips `nav`/`header`/`footer`, which on the measured page is ~30 anchors of category navigation spent inside a budget the offers need. It is an **upgrade to a read, never a dependency of one**: any failure in `_content_links` or `_main_text` returns empty and the read proceeds as before.

**The fetch path does the same thing**, via a chrome-depth counter and an anchor buffer in `_TextExtractor`, because if the browser path gives links and a plain fetch does not, the model learns to trust neither and goes back to searching. `TestLinksSurviveTheRender`, `TestLinksInTheText`.

With the capability in place, the prompt's prohibition stops being a fight it loses: it now states that the listing *already contains* the links, that they must be quoted exactly, and that an offer already read must never be web_searched for.

## What the owner's first hands-on session found (2026-08-14)

Three things, and the third was a design mistake rather than a bug:

**`/browser` opened a settings panel.** The bare form sent its output to the workspace sheet — so a command named after a browser answered a request to open one with a list of paths. It now opens the browser in both forms, with the address bar focused and a line saying what to do; only `forget` and `close` stay as text.

**A failed navigation was painted as a working browser.** `view_open` caught a `goto` exception and carried on to the screenshot — but a `goto` that throws navigates NOWHERE, so the frame was a white `about:blank` with an empty address bar and no explanation. The owner read that as the feature being broken, which is exactly what it looked like. A `Frame` now carries an `error`, and the client keeps the typed URL and says what happened.

**Closing the view claimed a login that never happened.** Every visited host was written to `logins.txt` on close. The rationale — "gating a merely-visited site errs safe" — was wrong in both directions: browsing to allegro.pl asserted an account there, and then every read of the site *this whole feature exists for* demanded approval. Friction on the main path, bought with a false claim about the owner's account.

A login is a fact only the owner can state, and they are right there when the sheet closes, so **it asks**: the shared confirm modal names the hosts and what agreeing means, with *Just looking* as the resting choice. `record_logins` is the only writer, and it runs when they say yes. (The button that raised this also said **Done**, which they reasonably read as "dismiss the keyboard" — it says **Close** now.)

The general shape is worth keeping: *the safe direction* is not always the restrictive one. Over-recording a login is not a cautious version of under-recording it; it is a different false statement, and this one taxed the main path to make it.

## Input: a tap opens an editor, and a password is never read back

The first input design was a text bar living permanently in the sheet. Every part of it failed in use — the owner's report is in `docs/web-frontend.md` under `[BROWSER-VIEW-EDIT]`. The server side of the replacement:

**`_FOCUS_JS` reports what the page has focused** — kind, label, rect, and for ordinary fields the current value — probed across child frames too, since a login form is routinely in an iframe, with the rect offset by the iframe's own box so the client can outline it in the one coordinate space it has.

**A password's value is NEVER read.** The frame already ships everything *visible*, so pre-filling an ordinary field adds essentially no exposure; a password field shows dots, so the pixels have never carried the value and reading it would be strictly **new** exposure — of a credential Chrome's profile may have autofilled and aish never saw typed. Masking it on the phone does not undo transmitting it. Refusal keys on `autocomplete: current-password/new-password` as well as the momentary `type`, because sites flip `type=password` to `text` for their own reveal button and tapping that first would otherwise launder the value into the read-back path. `TestPasswordsAreNeverReadBack`.

**`tapped` is decided here, not on the phone.** Focus moves as a side effect — pages autofocus their first input, dismissing a cookie banner can leave focus in one — so the server hit-tests the click against the focused element's rect. Only a tap that landed ON the field opens an editor; anything else just draws the outline.

**Writing is real keystrokes: select-all, then type.** Not Playwright `fill()`, which dispatches one `input` event and no key events at all. Keystroke-listening widgets break on that, and 2FA code boxes break outright — six one-character inputs that advance focus on each keyup would take `"123456"` into box one. Logins with 2FA are this feature's primary scenario. Typing over a selection also works in any iframe against whatever is focused, with no element handle to go stale. `TestEditingUsesRealKeystrokes`.

## A native dialog is a dead end, so the capability is removed

The owner tried to sign in to Google, and after entering his email the page went grey and the password step never came. Back did not recover it.

Cause: **Chrome's passkey prompt is browser chrome, not page content**, so `page.screenshot` cannot capture it — he was looking at a dimmed page behind a dialog that does not exist as far as this UI is concerned, with nothing to tap. Google's sign-in uses WebAuthn *conditional UI*, which fires the moment an email field is focused. Measured in this browser before the fix: WebAuthn available, conditional mediation available.

So WebAuthn is **removed** from the view rather than attempted, and sites fall back to a password — the flow he can actually complete. This is not a judgement about passkeys; it is that offering one here can only ever produce a dead end. If native dialogs are ever surfaced, this goes.

The same reasoning covers the rest of the class. Playwright **dismisses native dialogs by default, silently**, so a login that alerted an error would vanish without trace — they are reported into the frame's message instead. A file-upload picker would open a native chooser on a Mac nobody is sitting at and simply hang; it is cancelled with an explanation. `TestNativeDialogsAreDeadEnds`.

`<select>` was the last of this class and is now handled the same way — by bringing the capability into the page instead of leaving it to browser chrome. The options come up with the frame and the phone draws its own picker; the choice is applied with `select_option`, which fires `change` (typing into a select does nothing). Date and time inputs open native pickers too, but they accept typed text, so they are simply treated as editable. `TestSelectsAndNativePickers`.

## The view is DESKTOP, because round trips are the scarce resource

A frame costs 1–3 seconds. Zoom and pan happen on the phone and cost nothing. So the job is to maximise information per **frame**, not legibility per pixel — the owner zooms into whatever he wants once it has arrived.

The view was briefly given a mobile identity, on the reasoning that a phone should get the phone's web. Measured, that was backwards. Sites spend the entire first mobile screen on app-install banners and navigation: the owner's screenshot of allegro.pl's mobile home page is a coupon, a logo, a promo strip and a bottom nav bar, with **no content at all** — so reaching anything cost scroll after scroll, one round trip each.

| viewport | bytes | content | prices |
|---|---|---|---|
| 430×717 | 86 KB | 7.0k chars | 61 |
| **1280×2134** | 447 KB | **16.4k chars** | **112** |
| 1600×2667 | 703 KB | 16.6k chars | 118 |

Nearly triple the page per round trip at 1280, and little more beyond it. `view_size` therefore takes the stage the client will display in and scales that **shape** up to `VIEW_DESKTOP_WIDTH` — the shape so `object-fit: contain` has nothing to letterbox, the width so one frame carries a page.

**Bytes were the wrong target, and #227 was investigated in those terms before anyone checked.** The frame never reaches the model — `read_url` hands it extracted text, `is_challenge` judges text — so it costs no context, no tokens and no model time. Its only cost is Mac→phone, on a trip that already runs 1–3 seconds. Measured on an allegro.pl listing, one 1280×1950 frame at q50:

| density | jpeg | capture | transfer @20 Mbps |
|---|---|---|---|
| 1.5× | 331 KB | 71 ms | 136 ms |
| **2× (shipped)** | **497 KB** | **94 ms** | **204 ms** |
| 3× | 909 KB | 128 ms | 373 ms |
| 4× | 1295 KB | 228 ms | 531 ms |

Density 1.5 was shipped briefly to save bytes and cost about **90 ms of the trip** to undo — three percent — in exchange for sharpness the owner noticed immediately. So the frame is dense again, and quality stays at 50: +12% for visibly fewer artifacts is the same bargain read the right way round (#225). The general lesson is the one worth keeping: *check what the number you are optimising is actually spent on.* The width question is separate and its answer has not changed — 1280 buys page per round trip, which is the resource that is genuinely scarce.

## Density has a ceiling, so detail is fetched for what he is looking at

A frame is sharp only up to `zoom == density`, and zoom goes to **4×**. Even 2× is already magnifying at the 2.5× double-tap; the owner found it blurred past there and no setting fixes that, because serving 4× from the frame means a 1.3 MB, 228 ms capture on **every** frame — glance, scroll, tap — to serve the one moment he stops and reads.

So a frame is an **overview**, and detail is a separate capture of the rectangle actually on screen. `Page.captureScreenshot` takes a clip with its own `scale`, independent of the context's `device_scale_factor` — so this needs no second context, no reload and no re-navigation. It is reached through CDP directly because Playwright's `screenshot()` has no per-clip scale, and the scale is the entire point.

The economics are the whole argument:

| | jpeg | capture |
|---|---|---|
| detail patch at 2.5× zoom | 178 KB | 38 ms |
| detail patch at 4× zoom | **90 KB** | **18 ms** |

It gets **cheaper the further in he goes**, because the region shrinks as fast as the scale grows — a patch is always about one screenful. **Detail is O(screen) where density is O(page)**, which is why this scales and raising `VIEW_SCALE` never will.

The client asks for the scale **its own screen** can show — stage CSS width × its `devicePixelRatio`, over the page width visible — so a lesser phone asks for less and nothing here has to know about anybody's hardware. `detail_request` is pure and clamps the ask: the rect is pulled back inside the page rather than refused (a rounding error at the edge of a zoomed page should cost a few pixels of coverage, not the capture), and an over-large ask loses **scale, never coverage** — a smaller rect would silently cover less of what he is looking at, while a smaller scale only means the patch is less sharp than his screen could show. The scale actually captured rides back, so the client can tell a clamped patch from the one it asked for.

It deliberately does **not settle**. The page has not been touched; this is the same paint at more pixels, and waiting would turn a sharpening into an interaction. A failure sends **nothing** — a missing patch is a blurry patch, not an error, and the frame underneath it is still the page. The client half, and the rules about when a trip is worth spending, are `[BROWSER-VIEW-DETAIL]` in `docs/web-frontend.md`.

**It also ends an identity split that was never comfortable.** allegro.pl answers ANY mobile identity with 403 and zero text, so reads had to stay desktop while the view went mobile — meaning a session the owner created as a phone would later be read as a desktop, which is precisely the mismatch bot-scoring exists to catch. One identity again. `TestTheViewIsDesktopSoOneFrameCarriesMore`.

## A frame arrives fast, then keeps watching

The owner proved this with paired screenshots: a partly-rendered page, then the finished one, with **no navigation between them** — only another frame. The picture had been wrong, not the page, and it made everything else look broken (it is also what made the passkey problem look worse than it was: the password step HAD arrived, in a frame he was never shown).

A fixed `SETTLE_MS` cannot fix that, because "loaded" is a property of the page rather than a duration. `_settle` waits on three signals, cheapest first — network idle, `readyState === 'complete'`, then a DOM-quiescence window, which is the one that catches a page whose skeleton has loaded while its content is still being written in. Every wait is bounded by `SETTLE_MAX_MS`: a page that never settles — a ticker, a spinner — must still produce a frame.

**But settling BEFORE showing anything was its own bug.** It made every interaction feel dead — "the impression is that I'm waiting way longer… a strange sense that there's nothing happening" — and it still missed late repaints, because a page that changed after the capture never got another one, so the owner had to tap again to see the finished page. The owner's own prescription is the design: *"it's fine to show two screenshots… needs to be just once."* So an interaction returns a quick frame (`FIRST_FRAME_MS`), and the server captures again and forwards it **only if it differs**. `TestFramesWaitForThePageToSettle`.

### One correction was still one bounded question standing in for an open-ended one

The owner kept having to tap the page to force a fresh frame, and the two cases where he did were both cases the single correction could not cover.

**A spinner is where quiescence and doneness part company.** `_settle` asks *"has the DOM stopped changing?"* as a stand-in for *"is the page finished?"* A spinner sits perfectly still in the DOM, reports `readyState === 'complete'`, and animates in CSS — so the strongest statement a page can make that it is NOT ready reads to a quiescence test as the strongest evidence that it is. The correction was spent photographing the spinner, and nothing looked again.

**A scroll got no correction at all.** The correction fired for `open, goto, click, fill, choose, back, refresh` and not for `scroll`, so a lazily loaded image below the fold arrived to nobody. It is also invisible to a DOM-only watcher even in principle: lazy loading assigns an existing `<img>` a new `src` and adds no node.

Naming a spinner is a dead end — aish already has a text-based `browse.still_loading` on the model's read path, and it catches one with a label while missing a bare CSS donut, which is most of them. **So stop guessing when a page is done and keep looking, cheaply, for a bounded while.** The two halves that were fused into one expensive operation are split:

| | cost | answers |
|---|---|---|
| the probe (`view_activity`) | ~5 ms, nothing over the wire | did anything actually happen? |
| the capture (`view_settled_frame`) | ~200 ms, ~40 KB | the picture itself |

`_WATCH_JS` is the probe: a generation counter bumped by a MutationObserver **and** by a `PerformanceObserver` on completed resources — the network half is what catches both a spinner's contents arriving and a lazily loaded image. It installs itself lazily on first read rather than through `add_init_script`, so it self-heals across a navigation and works on a view that was already open when this shipped.

**The comparison is against the frame on screen, not the last poll.** A `Frame` carries the generation it was captured at, paired with `nav` because a navigation resets the counter to zero — a change that would otherwise read as no change. Comparing against the previous poll would absorb whatever arrived between the capture and the first probe, which is the exact window this feature exists for. The generation is read BEFORE the screenshot, deliberately: a mutation landing between the two then costs a redundant capture the byte-compare discards, where reading it after would count that mutation as already shown and lose the frame.

`watch_step` is the whole policy and it is **pure** — `"wait"`, `"capture"`, `"last"` or `"stop"` from primitives, no browser and no clock. That matters more than usual here, because every case it decides is one that only appears on a real site at a real speed. *Cannot tell* (a page mid-navigation cannot run the probe) falls back to capturing, because it must never be read as *nothing happened*.

**Letting go asks a harder question than "is it quiet?", and the first version of this got it wrong.** Stopping the moment the page went still shipped, passed its unit tests, and reintroduced the exact bug when driven against real Chrome: the watcher quit at its first quiet poll ~2.6 s after the spinner appeared, and the contents landed at 3 s. Stillness NOW says nothing about stillness later — the premise of the whole feature is that arrival is late. So the page must be complete AND continuously still for `WATCH_SETTLED_MS`, which every arrival resets, so a page loading in stages is followed to the ceiling. A finished page costs a dozen probes and no bytes. This is the case that only a real browser could have shown; the unit test that now pins it was written from the failure, not before it.

**This makes #223 cheaper rather than worse.** The old correction paid for a full second capture on EVERY interaction and then threw it away when the bytes matched; a finished page now pays one capture instead of two. What bounds the animated page the issue is about is the cap — `WATCH_MAX_CAPTURES` captures, `WATCH_MIN_GAP_MS` apart, inside `WATCH_MAX_MS` — and it counts CAPTURES, not sends, or identical bytes would loop for free. A carousel costs a few extra frames, not a stream.

The watcher is one task, globally, because there is one view; a new interaction **supersedes** it, since a frame captured for the previous action would land on top of the new one. `detail` is exempt — a sharpening is not an interaction, and cancelling on it would stop the watch every time he pinched a still-loading page. Polling never STARTS a browser (`_no_browser_yet`): a poll that launched the thing it polls would bring Chrome up on a machine nobody asked. `TestTheWatcherKeepsLookingAfterTheFirstCorrection`, and the server half in `TestBrowserView` — including the superseded-mid-capture interleaving, which is driven rather than asserted about.

## The sheet is full height, so it met the two edges of the screen (#226)

Reclaiming the dead band under the nav row gave `#browser-sheet` `height: 100%`, and it promptly ran into both insets — once visibly, once not.

**The top.** `.sheet` had always padded `env(safe-area-inset-bottom)` and nothing had ever accounted for the top, because until this sheet no sheet reached it: the address row went under the Dynamic Island and the top of the browser was simply not there. It is **padding, not a shorter box** — the sheet is bottom-anchored, so height arithmetic silently does nothing where `env()` does not resolve, while padding shifts the content down either way, which is what the bottom already did.

**The bottom, in landscape, which nobody had looked at.** The column is a fixed stack — address row, hint, stage, editor, nav row — and `.bv-stage` was the only elastic member, with `min-height: 150px`. **A floor on the only elastic row is a floor on every control below it.** A landscape keyboard leaves ~205 px of visual viewport; the column needed 357, so the field being typed into sat about a hundred pixels below the screen with nothing to say so. That is the same defect as the Close button at y=899 and the text field under its own keyboard — third time, same shape.

Three changes, in order of how much they matter:

- **The stage's floor is a TAP TARGET, not a picture** (44 px). A tap on the page is the only gesture that leaves a field without submitting, so the stage must stay hittable — and is owed nothing beyond that, because the field wins.
- **The browser's toolbar stands down while a field is open.** Safari does the same with its bottom bar under the keyboard; Back mid-login is a footgun besides; and it hands the stage back 50 px, so you see more of the page you are signing in to. It is keyed on the `[hidden]` attribute `bvOpenEditor`/`bvCloseEditor` already toggle — no second piece of state to fall out of step — which does mean it holds only while `#bv-edit` and `.bv-nav` are siblings.
- **Sideways, with a field open, the sheet spends nothing on furniture**: no grabber, no hint line, minimum padding. `@media (max-height: 500px)` reads the LAYOUT viewport, which iOS keeps full-height under the keyboard, so it means "the phone is sideways" rather than "the keyboard is up" — portrait, where everything already fits, is untouched. 500 because an iPhone 16 Pro Max is 440 px tall in landscape and 430 missed it, while the shortest phone in portrait is 568.

Everything is now reachable down to a 200 px visual viewport, on every phone measured.

**None of this is visible to the node tests, and that is the point.** They run the shipped functions against a fake DOM with no layout engine, so all three incidents shipped green. `scripts/check-browser-sheet.py` drives the real `index.html` and the real `style.css` through a real Chrome at real device metrics and asserts what the owner can actually reach — including a genuine `env(safe-area-inset-top)` via `Emulation.setSafeAreaInsetsOverride`, which cannot otherwise be produced on a desktop, and a keyboard modelled the way app.js models one (`body.style.height` pinned to the visual viewport, because every `vh` here lies). It is not in `uv run pytest`, which launches no Chrome by design. `tests/test_browser_view_layout.py` holds the structural preconditions that would otherwise fail in silence.

## Small things that were the whole experience

- **An ✕ at the right of an address bar CLEARS THE FIELD.** That is what it means in every browser, and it was closing the session instead — a tap meant to erase a URL destroyed a login in progress. The field has its own clear inside it now, and **closing is an ✕ on the far LEFT**, where it cannot be read as a control belonging to the field.
- **Closing is not called "Done".** It was, briefly, and that was a different mistake: "Done" reads as finishing a task, and this browser is not only for signing in. It is also for browsing so the profile's history looks human, for seeing what aish sees on a page, and in time for watching it act. The affordance should not imply the errand is over.
- **Focusing the address selects all of it**, so the next keystroke replaces the URL and a long-press offers Copy on the whole thing.

## The console is the second door into the view

A URL printed by the interactive console (`gcloud auth login`, an `ssh` banner, a dev server) is the case the view exists for and the one it was hardest to reach: seeing it meant reading the URL off the terminal and typing it into the browser sheet by hand. A tap on it now opens it in whichever browser that device has been told to prefer, and a **hold** on the link offers the choice — aish's browser, the device's own, or copy.

Which browser is right genuinely depends on the link, which is why this is a choice and not a redirect: an auth URL wants the browser aish will later *read that site as*, so the session lands in the profile that will use it (the rule the whole no-proxy design rests on), while a documentation link wants the phone's browser and its tabs.

The view opens **over** the console rather than replacing it, which is the whole shape of the sign-in flow: the CLI that printed the link is still running behind the browser, so signing in and closing the sheet returns to the prompt that is waiting for the code — one screen, one errand. The client half — the gesture arbitration against the console's existing hold-to-select, and the layering and keyboard handoff that let the two coexist — is `[CONSOLE-LINK-TARGET]` in `docs/web-frontend.md`.

## Nothing answers "no remote view is open"

That sentence is a statement about aish's bookkeeping, not about what the owner asked for — and he met it on a page still visible on his screen, after the idle reaper collected the view behind it. Any action now reopens at the last URL and shows him the page again. It stops there rather than replaying his tap, because a tap aimed at the old page would land somewhere arbitrary on a freshly loaded one.

## Every action is EXECUTED in a test, not read

A total interaction outage once shipped while 68 tests passed. A new `if` block inserted mid-chain split one `if/elif/else` into two, so `click`, non-secret `fill` and `clear` fell through to `raise ValueError("unknown view action")` — after the click had already been performed on the page. Every tap would have errored.

It passed because the input-contract tests read `inspect.getsource` and never call anything: **source inspection cannot see control flow.** `TestEveryActionActuallyRuns` drives every action against a fake page and asserts something reached it. Any test that asserts on source text needs a sibling that executes.

## Testing

Nothing in the suite launches Chrome. `browser.read` / `open_for_login` are patched per test, and conftest's autouse `no_real_browser` makes any escape fail loudly — it raises from `_submit` as a `BaseException` (an `Exception` would be swallowed by `_browser_read`'s fallback, leaving the guard silent exactly where a test is most likely wrong) and redirects `AISH_STATE_DIR` so a test-written `logins.txt` can never change how the real agent gates a real host. Same reasoning as the notifier guard in CLAUDE.md: a module that reaches a live thing outside the process needs a suite-wide guard, not per-test discipline.

`TestDetailIsFetchedForWhatHeIsLookingAt` covers the clamping and the screenful-stays-a-screenful property that makes fetched detail scale where density does not; `TestEveryActionActuallyRuns` also EXECUTES the CDP capture, since nothing else in the module takes that path. `scripts/check-browser-sheet.py` is the only thing that can see the sheet's LAYOUT — a real Chrome, real phone metrics, real safe-area insets — and is deliberately outside the suite; `tests/test_browser_view_layout.py` pins the structural facts it depends on. `TestBrowserView` covers the remote view end of the socket; `TestTheViewIsDesktopSoOneFrameCarriesMore` covers the viewport decision; `TestChallengeDetection` covers telling a wall from a page; `TestViewAndReadShareOneBrowser` and `TestPreviewFence` cover the two places one profile is contended for; `TestAThinPageGetsASecondChance` covers a slow page mistaken for a wall; `TestTheReadingContract` covers what the prompt must keep saying; `TestUnresponsiveHostEscalates` and `TestKnownBlockingHostsSkipTheDoomedFetch` cover the failure that produced no renders at all; `TestCommand` covers the shared `/browser` text; `TestProfileLocation`, `TestLoginRecord`, `TestReadUrlEscalation` cover the module; `TestLoginGate` covers the gate.

`TestBrowserCommand` covers the WebSocket wiring — the layer where a slash command actually breaks, since a missing app.js case or WS kind surfaces only as "unknown command" in the app.
