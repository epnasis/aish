# The browser — reading pages a fetch cannot read, and driving one by hand (#221)

`browser.py`, `web._browser_read`, `Agent._login_gate`, `server._browser_view`, `aish/static/app.js` `[BROWSER-VIEW-*]`.

**Start here if you are new to this file.** Two capabilities share one Chrome and one profile: `read_url` escalates to it when a fetch cannot read a page, and the owner drives it by hand from his phone (`/browser`). The rules that govern almost every decision below are these four, and most mistakes here have been a failure to apply one of them:

1. **A round trip costs 1–3 seconds; anything local is free.** Maximise information per frame; never spend a trip on what the phone can do itself.
2. **A native dialog is a dead end.** It is browser chrome, so `page.screenshot` cannot see it and there is nothing to tap. Capabilities that summon one are removed, or brought into the page.
3. **The session must be created in the browser that will later use it**, by one identity. That is why there is no proxy and no mobile/desktop split.
4. **Follow the conventions of a mobile browser.** If a browser does it with a gesture on the content, do not add a widget. Nearly every UI complaint here has been an invented widget.

Open work is on GitHub: **#223** (correction frames double traffic), **#224** (round-trip economies), **#225** (verify Google sign-in; sign-in question blind spots — the JPEG-quality third is answered below under the frame-size measurements).

`read_url` fetches with urllib: fast, cheap, anonymous, right for most of the web. Two kinds of page it cannot read **at all** — one rendered entirely by JavaScript, where the fetch returns an empty shell, and one behind a login, where the fetch is simply a different, logged-out client. This is the escalation for both: a real Chrome on this Mac, driven off-screen, with a profile that persists.

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

## The login gate — and why it is not the egress gate

`Agent._login_gate` holds a `read_url` that would be made with the owner's live session until they approve it, and it applies to **every origin, the attended session included**.

That is the difference from `_egress_gate`, which asks *"is this host one the owner named?"* and only in a triggered session, on the reasoning that an attended owner can see the host for themselves. This gate asks a different question — *"does this read carry the owner's session?"* — and their watching does not settle it, because the URL may have come from text on a page rather than from them. An injected instruction that steers a read at a signed-in site would otherwise pull private account data into the context silently.

Approval is per host and lasts the session (`_approved_logins`), matching the egress gate: a task that reads five pages of one portal asks once. It is session-scoped like every other grant (L4). With no approver it fails **closed** — reading the owner's account with nobody watching is the one outcome this exists to prevent.

Only `read_url` can carry a session; `show_image` / `read_pdf` / `read_media` fetch bytes through the anonymous opener, so they are never gated here and must not draw a card claiming they are. `TestLoginGate` pins all of it, including the seam that matters most: `_read_needs_prompt` routes a gated read **off** the parallel path, which has no gate at all and would otherwise bypass approval entirely.

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

**The width was never what cost the bytes — the pixel density was (#227).** The owner's reaction to the shipped frame was that the resolution might simply be too big, and he was right about the symptom and wrong about the knob. Measured on an allegro.pl listing, one 1280×1950 frame at q40:

| density | 1280 wide | 1024 wide | 768 wide |
|---|---|---|---|
| **2× (was shipped)** | **446 KB** | 350 KB | 250 KB |
| **1.5× (now)** | **295 KB** | 247 KB | 167 KB |
| 1× | 182 KB | 134 KB | 93 KB |

Read across a row and narrowing the frame saves bytes; read down a column and lowering the density saves more of them — and only one of those is free. A narrower frame is paid for in page per round trip, the single thing this view is optimised for. A lower density is the *same* 1280 CSS pixels of page, the same layout, the same text, for a third fewer bytes. So the width stays at 1280 and `VIEW_SCALE` comes down.

What density buys is **zoom headroom, and only up to a point.** The stage is ~430 CSS px, so a 1280-wide frame is displayed at ~0.34 and the picture stops gaining detail once its own pixels run out: parity lands at zoom == density. 2× held true detail to a 2× zoom; 1.5× holds it to 1.5×. The double-tap is **2.5×**, so even the shipped 2× was already magnifying there — the extra pixels were being paid for on every frame and cashed in on none. Crops of a price row at 2.5×, resampled exactly as the phone does it: 2× q40 and 1.5× q50 are hard to tell apart, 1× q50 is visibly soft.

So the density that came off is partly bought back as **quality**, which is far cheaper per byte — q40 → q50 is +12%, 2× → 1.5× is −34% (#225's open question, answered the same way). Net **331 KB against 446**, same page. Coordinates are unaffected either way: they are CSS pixels, and `browserViewPoint` maps against the rendered element rather than the pixel count.

**It also ends an identity split that was never comfortable.** allegro.pl answers ANY mobile identity with 403 and zero text, so reads had to stay desktop while the view went mobile — meaning a session the owner created as a phone would later be read as a desktop, which is precisely the mismatch bot-scoring exists to catch. One identity again. `TestTheViewIsDesktopSoOneFrameCarriesMore`.

## A frame arrives fast, then corrects itself once

The owner proved this with paired screenshots: a partly-rendered page, then the finished one, with **no navigation between them** — only another frame. The picture had been wrong, not the page, and it made everything else look broken (it is also what made the passkey problem look worse than it was: the password step HAD arrived, in a frame he was never shown).

A fixed `SETTLE_MS` cannot fix that, because "loaded" is a property of the page rather than a duration. `_settle` waits on three signals, cheapest first — network idle, `readyState === 'complete'`, then a DOM-quiescence window, which is the one that catches a page whose skeleton has loaded while its content is still being written in. Every wait is bounded by `SETTLE_MAX_MS`: a page that never settles — a ticker, a spinner — must still produce a frame.

**But settling BEFORE showing anything was its own bug.** It made every interaction feel dead — "the impression is that I'm waiting way longer… a strange sense that there's nothing happening" — and it still missed late repaints, because a page that changed after the capture never got another one, so the owner had to tap again to see the finished page. The owner's own prescription is the design: *"it's fine to show two screenshots… needs to be just once."* So an interaction returns a quick frame (`FIRST_FRAME_MS`), and the server then captures once more when the page settles and forwards it **only if it differs**. `TestFramesWaitForThePageToSettle`.

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

## Nothing answers "no remote view is open"

That sentence is a statement about aish's bookkeeping, not about what the owner asked for — and he met it on a page still visible on his screen, after the idle reaper collected the view behind it. Any action now reopens at the last URL and shows him the page again. It stops there rather than replaying his tap, because a tap aimed at the old page would land somewhere arbitrary on a freshly loaded one.

## Every action is EXECUTED in a test, not read

A total interaction outage once shipped while 68 tests passed. A new `if` block inserted mid-chain split one `if/elif/else` into two, so `click`, non-secret `fill` and `clear` fell through to `raise ValueError("unknown view action")` — after the click had already been performed on the page. Every tap would have errored.

It passed because the input-contract tests read `inspect.getsource` and never call anything: **source inspection cannot see control flow.** `TestEveryActionActuallyRuns` drives every action against a fake page and asserts something reached it. Any test that asserts on source text needs a sibling that executes.

## Testing

Nothing in the suite launches Chrome. `browser.read` / `open_for_login` are patched per test, and conftest's autouse `no_real_browser` makes any escape fail loudly — it raises from `_submit` as a `BaseException` (an `Exception` would be swallowed by `_browser_read`'s fallback, leaving the guard silent exactly where a test is most likely wrong) and redirects `AISH_STATE_DIR` so a test-written `logins.txt` can never change how the real agent gates a real host. Same reasoning as the notifier guard in CLAUDE.md: a module that reaches a live thing outside the process needs a suite-wide guard, not per-test discipline.

`scripts/check-browser-sheet.py` is the only thing that can see the sheet's LAYOUT — a real Chrome, real phone metrics, real safe-area insets — and is deliberately outside the suite; `tests/test_browser_view_layout.py` pins the structural facts it depends on. `TestBrowserView` covers the remote view end of the socket; `TestTheViewIsDesktopSoOneFrameCarriesMore` covers the viewport decision; `TestChallengeDetection` covers telling a wall from a page; `TestViewAndReadShareOneBrowser` and `TestPreviewFence` cover the two places one profile is contended for; `TestAThinPageGetsASecondChance` covers a slow page mistaken for a wall; `TestTheReadingContract` covers what the prompt must keep saying; `TestUnresponsiveHostEscalates` and `TestKnownBlockingHostsSkipTheDoomedFetch` cover the failure that produced no renders at all; `TestCommand` covers the shared `/browser` text; `TestProfileLocation`, `TestLoginRecord`, `TestReadUrlEscalation` cover the module; `TestLoginGate` covers the gate.

`TestBrowserCommand` covers the WebSocket wiring — the layer where a slash command actually breaks, since a missing app.js case or WS kind surfaces only as "unknown command" in the app.
