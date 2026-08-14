# The browser — reading pages a fetch cannot read (#221)

`browser.py`, `web._browser_read`, `Agent._login_gate`.

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

`AISH_BROWSER=0` disables the browser entirely; `read_url` then degrades to exactly its pre-#221 behaviour, the Jina hint included. That fallback is pinned by `TestJinaFallbackHint` via the `no_browser` fixture — those tests must say there is no browser rather than rely on one being absent.

## Status is diagnostic only

A site that dislikes automation may answer **403 and still serve the whole listing, prices included** — measured, not hypothesised. So a browser read is judged on whether it produced **text**, never on the code. Judging on the code would throw away the exact page this feature exists to get (`TestReadUrlEscalation`).

## Where the profile lives is a safety decision

`~/.local/state/aish/browser/profile`, **never `~/.config/aish/`**. The config tree is auto-committed and pushed to a private GitHub repo by the knowledge-git agent, and this directory is made of live session cookies — a profile under config would publish the owner's logins to a git remote on a timer. `TestProfileLocation` pins it.

Persistence is the *point*, not an optimisation: a session the owner established by hand is still there next week, and every later read of that site is made as them. Nothing here ever clears the profile.

## One thread owns the browser

Playwright's sync API binds its objects to the creating thread, and `read_url` runs on a pool — `_execute_tool_calls` fans read-only tools out concurrently — so a shared context touched from a second thread errors out. Every call is marshalled to one long-lived owner thread through `_JOBS`. That also buys single-ownership of the profile directory, which Chrome requires: it locks the user-data-dir and a second launch against a live profile fails. It is why `open_for_login` closes the off-screen context before opening the on-screen one.

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

## Testing

Nothing in the suite launches Chrome. `browser.read` / `open_for_login` are patched per test, and conftest's autouse `no_real_browser` makes any escape fail loudly — it raises from `_submit` as a `BaseException` (an `Exception` would be swallowed by `_browser_read`'s fallback, leaving the guard silent exactly where a test is most likely wrong) and redirects `AISH_STATE_DIR` so a test-written `logins.txt` can never change how the real agent gates a real host. Same reasoning as the notifier guard in CLAUDE.md: a module that reaches a live thing outside the process needs a suite-wide guard, not per-test discipline.

`TestBrowserView` covers the remote view end of the socket; `TestViewSize` covers the client-declared viewport and its clamps; `TestChallengeDetection` covers telling a wall from a page; `TestViewAndReadShareOneBrowser` and `TestPreviewFence` cover the two places one profile is contended for; `TestAThinPageGetsASecondChance` covers a slow page mistaken for a wall; `TestTheReadingContract` covers what the prompt must keep saying; `TestUnresponsiveHostEscalates` and `TestKnownBlockingHostsSkipTheDoomedFetch` cover the failure that produced no renders at all; `TestCommand` covers the shared `/browser` text; `TestProfileLocation`, `TestLoginRecord`, `TestReadUrlEscalation` cover the module; `TestLoginGate` covers the gate.

`TestBrowserCommand` covers the WebSocket wiring — the layer where a slash command actually breaks, since a missing app.js case or WS kind surfaces only as "unknown command" in the app.
