# The word lists, and what each one costs when it matches nothing

`aish/vocab.py`, `aish vocab`, `tests/test_vocab.py`. Issue #322, under #295 P4:

> Where a vocabulary is unavoidable it is **a floor under a structural check**, grown only
> from measurement, and it **ships with counters**.

aish decides a lot by matching words against text somebody else wrote: is this thing covering
the control a cookie banner, does this label say the button spends money, is this mail a door
into an account, is this command safe to run unasked. Each is a tuple of a few dozen strings.

**Before this, not one of them counted.** So a list that had silently stopped matching was
indistinguishable from a page that had nothing to match. On 2026-08-26 `_CONSENT_SELECTORS`
held `Akceptuj wszystkie` against a page whose button says `Akceptuję wszystkie cookies` —
**a miss by one letter, in the language the owner browses in** — the banner it failed to
dismiss was covering eon.pl's login button, the sign-in failed for days, and four confident
diagnoses were argued on top of a silence that nothing anywhere reported (#321,
`docs/browser.md`).

## What this slice did, and what it deliberately did not

**It measures. It changes no matching.** Not one word was added, removed or reordered in any
list. That is not a claim from reading the diff — a human reading a diff is exactly the
instrument that misses one letter, which is the bug. `TestTheCountingChangedNoMatching`
parses every module-level string collection in the seven files out of `git show` at the
commit before this work and compares it, entry by entry and in order, with what the tree
holds now.

The reason the fence is that tight: **the whole value of this slice is a before-picture**,
and a slice that also changed behaviour cannot provide one. A later change to any list is
judged against these numbers, and it can only be judged against them if they were taken
against what shipped.

**A later change is DECLARED, never exempted (#341).** The fence compares today's tree
against the picture, so it fires on any edit to any collection in those seven files — which
is right, and it is not "no list may ever change". `TestTheCountingChangedNoMatching.DELIBERATE`
names the list, the issue and the entries gained or lost, and the comparison is
`before − removed + added` **exactly**: a declaration that waved through any change to a list
it happened to name would be an exemption rather than a record, and an undeclared drift still
fails. The first entry is `agent.EGRESS_TOOLS` gaining `browse`, which the extractor collects
because it sweeps every module-level string collection — a deliberate over-collection, since
narrowing it to "things that look like words" is exactly the judgement the fence exists to
take away from a human reading a diff. It is not a vocabulary and is not in the inventory
below: it holds aish's own TOOL NAMES, matched against the tool the model called and never
against page text, a control label or an error string.

**Half-done, and which half.** 26 lists are counted at their real call sites; 2 more are
catalogued and not counted (`counted=False`, and they say why); a further group is
inventoried in this document alone, in *"Considered and not catalogued"* below. Nothing is
silently missing: `vocab.not_counted` is a separate heading in the report, because a list
that is consulted constantly and simply not counted must never be printed as "not
consulted".

## The three failure shapes, and why there are three

`vocab.PERMITS` / `vocab.FRICTION` / `vocab.BREAKS`. Two would have been the obvious choice
and would have hidden the bug that started this.

- **`permits`** — an unmatched item is **not gated**: something happens that a match would
  have stopped or slowed. Fail-open toward consequence.
- **`friction`** — an unmatched item costs a prompt, a refusal or an extra step. Fail-closed.
  Being wrong is paid in the owner's time, never in his money. `approval.py`'s standing
  doctrine — *err toward prompting* — is this column.
- **`breaks`** — an unmatched item **silently disables a feature**. It permits nothing and
  costs no friction; it just stops working, and nothing reports it. **This is #321's shape,
  and it is the one nobody goes looking for**, which is why it is the shape a counter is
  actually for.

**These are an engineer's verdict, not a measurement**, and every renderer says so where it
prints them. `asked` and `matched` are measured; `on_miss` and `structural` are read off the
code by a person and are only as good as that reading.

## The inventory

`size` and `languages` come from the catalogue, which derives `size` with `len()` on the real
object — so it cannot drift from the list. Counted = a real call site writes to it.

| list | languages | on a miss | counted | structural check under it | size |
|---|---|---|---|---|---:|
| `browse._MUTATING_WORDS` | PL + EN | permits | yes | the form-submit half of `is_mutating`: a non-GET submit draws a card whatever the button is called | 77 |
| `browse.CHROME_WORDS` | PL + EN | friction | yes | `submits` / `in_widget` — the demotion cannot reach a control that submits a form | 6 |
| `browse._CHANGE_VERBS` | PL + EN | permits | yes | — | 17 |
| `browse._CONTACT_NOUNS` | PL + EN | permits | yes | — | 14 |
| `browse._PAYOUT_NOUNS` | PL + EN | permits | yes | `types_a_bank_account` — an IBAN is refused as a VALUE wherever typed, no label read | 11 |
| `browse._CONTACT_PHRASES` | PL + EN | permits | yes | — | 15 |
| `browse._CREDENTIAL_PHRASES` | PL + EN | permits | yes | `NO_PASSWORDS` — aish never types a stored password on the browse path | 14 |
| `browse._IDP_PROVIDERS` | brand names | permits | yes | — | 13 |
| `browse._IDP_VERBS` | PL + EN | permits | yes | — | 12 |
| `browse._IDP_ALONE` | PL + EN | permits | yes | — | 5 |
| `browse._NOT_AN_IDENTITY` | PL + EN | friction | yes | — (inverted: this list EXEMPTS) | 5 |
| `browse._CLOSE_ACCOUNT_PHRASES` | PL + EN | permits | yes | — | 13 |
| `browse._DOWNLOAD_WORDS` | PL + EN | breaks | yes | the `/download` and `/export` path test beside it | 13 |
| `browse._DOWNLOAD_SUFFIXES` | file suffixes | breaks | yes | — | 9 |
| `browse._FORWARD` | PL + EN + arrows | breaks | yes | none — but a name not on the list REFUSES rather than guesses | 14 |
| `browse._BACKWARD` | PL + EN + arrows | breaks | yes | none — as above | 15 |
| `browser._CONSENT_SELECTORS` | PL + EN + vendor ids | breaks | yes | `_COVERED_JS` / `browse.Cover` — asks the page what sits at the control's own centre point | 6 |
| `browser._CHALLENGE_MARKERS` | PL + EN | permits | yes | `BLOCK_STATUS` + `CHALLENGE_MAX_CHARS` — **the epic's worked example** | 13 |
| `browser._REPUTATION_COOKIES` | vendor cookie names | breaks | yes | — | 8 |
| `browser._LOCK_MARKERS` | EN (Chrome's own text) | breaks | yes | — | 3 |
| `signin._CAPTCHA_TOKENS` | vendor brand names | friction | yes | — | 11 |
| `provenance._SIGN_IN_PHRASES` | PL + EN | permits | yes | none, and one is hard to get — see below | 37 |
| `web._AVAILABILITY` | schema.org enum | breaks | yes | — | 9 |
| `approval.SAFE_COMMANDS` | program names | friction | yes | root scoping + `UNSAFE_FLAGS` — being on it is necessary, never sufficient | 34 |
| `approval._DESTRUCTIVE_COMMANDS` | program names | breaks | yes | none needed — the GATE is `check_denied` and the card, neither of which reads this | 12 |
| `agent.REFUSAL_OPENINGS` | EN (aish's own words) | permits | yes | `_gate_outcome` / `ToolOutcome.meta`, checked FIRST | 6 |
| `signin._SECOND_LEVEL` | public suffixes | friction | **no** | — | 10 |
| `web._NOT_BUYABLE` | schema.org enum | breaks | **no** | subset of `web._AVAILABILITY`, tested at the same call site | 3 |

### The entries worth arguing about

**`browse._MUTATING_WORDS` (77, the largest).** Fail-open, and the file says so out loud —
*"it costs a prompt when it is wrong and costs a paid bill when it is missing"*. But only the
WORDED half fails open: a nondescript `Dalej` that POSTs a form is gated by
`is_mutating`'s structural half regardless. The residual is a button that neither says a
mutating word nor submits a form — a JavaScript control on an SPA — and for that there is
nothing but the per-host, per-task driving grant.
**Could a structural check replace it?** No, and it should not try. Whether pressing a thing
spends money is not a property of the DOM.

**`browser._CHALLENGE_MARKERS` — already the shape P4 asks for.** `BLOCK_STATUS`
(401/403/405/429/503) is a test needing no words, `CHALLENGE_MAX_CHARS` is another (a real
listing runs to tens of thousands of characters), and the vocabulary sits under both as a
floor for the walls that answer 200 with a short body. Copy this pattern rather than
inventing one.

**`browser._CONSENT_SELECTORS` — the instance.** Its structural half is `_COVERED_JS`, which
asks the page what element sits at the control's own centre point. That is language- and
site-independent and needs no words at all. **The residual is the DISMISSAL, not the
detection**: aish now always knows a control is covered and by what, and the word list is
only what decides whether it can take the banner down. A miss is a reported failure, not a
silence — which is the repair #321 already shipped. It is counted at `_uncover` alone, where
the structural check has already found something covering a control; `_dismiss_consent`'s
five other callers run speculatively on pages where no banner is the overwhelmingly common
case, and counting there would put the miss rate near 100% for ever and say nothing.

**`provenance._SIGN_IN_PHRASES` (37) — fail-open with no structural half available.** A
reset link is routinely a bare tracking redirect with nothing readable in the path, which is
exactly why the WORDS around it are read. **Could a structural check replace it?** Not from
the link. Something might be got from the sender or from a mail header, but nothing in the
URL. This is the entry most exposed by a language aish's mail arrives in that nobody added.

**`approval._DESTRUCTIVE_COMMANDS` — advisory, and that is easy to misread.** It decides
whether the prompt carries a red *destructive* marker. It **never** decides whether a command
runs: `check_denied` and the approval card do that, and neither consults it. So a miss
permits nothing and costs no friction — it removes a warning from a card the owner still has
to approve. `breaks`, and the counter is the only thing that would ever say it had stopped
firing. Its counter deliberately does **not** credit it with `sudo`'s and `--force`'s
catches, which are separate tests at the same call site; folding those in would report a
dead list as a working one (`_destructive_verdict`).

**`approval.SAFE_COMMANDS` — the one that is safe by construction.** It is an ALLOWLIST, so
a miss can only ever put a card in front of the owner. It is also the highest-volume list in
the tree. Its counter is a **use-rate, not a health check**, and reading a falling match rate
as breakage would be wrong: it moves with what the model happens to run.

**`agent.REFUSAL_OPENINGS` — the odd one out.** It matches aish's OWN sentences, not a
page's, so it goes stale by an aish refactor rather than by a site. It is only reached when
no structural carrier is present, and it is counted only on that branch — so the number worth
watching is **how often it is reached at all**. A miss reads a refusal as a success, which
satisfies a rule's `must_first` and logs a verify PASS for a call the harness stopped.

**`browse._FORWARD` / `_BACKWARD` — `breaks`, but loudly.** A calendar arrow whose name is
not on the list makes `month_step` REFUSE rather than guess, so the failure surfaces at the
call site instead of pressing the wrong thing. That fence is why a closed list is right here.
`month_step` also has a two-phrase prefix fallback that is not separately counted; what the
counter reports is the LIST.

### Where a structural check already replaced the words entirely

Two of the four questions #322 opens with turned out to have **no vocabulary at all**, and
both are the pattern to copy.

- **"Is this the login button?"** — `SIGNIN_FORM_JS` presses only a control it tagged inside
  the form: a genuine submit control, or, where the form has none, **its single visible
  button, counted and never read**. With no form the submit is the Enter key. That is
  exactly the *"the only pressable control in the form"* test, it needs no words, and
  choosing by words is how the model once ended up pressing *Continue with Google*.
- **"Did the page just say the credential was refused?"** — `SIGNIN_REJECTION_JS` reads
  `input[type=password][aria-invalid=true]` and `[role=alert]` / `[aria-live=assertive]`
  regions, and `_said_no` compares before against after. Machine-readable state and a
  difference; no error wording in any language is consulted.

### Considered and not catalogued

Inventoried, and left out with the reason — an exclusion list without reasons rots into a
list of things somebody gave up on. The first group is pinned by
`TestTheCatalogueIsTheInventory.NOT_A_VOCABULARY`.

**Not natural language at all:** Chrome command-line flags (`_OFFSCREEN_ARGS`, `_LOGIN_ARGS`,
`_STEALTH_ARGS`, `_STEALTH_OMIT`), file names in aish's own profile directory (`_LOCK_FILES`),
HTTP method and status sets (`_BODY_METHODS`, `signin._SUBMIT_METHODS`, `_REFUSED_STATUS`,
`browser.BLOCK_STATUS` — that last one being the STRUCTURAL half, not a vocabulary), HTML tag
sets in `web.py` and `export.py`, and attribute-name lists inside the page-reading JavaScript.

**Decision vocabularies that are inventoried here but NOT instrumented in this slice**, named
so the next person does not have to re-find them: `approval.EXEC_WRAPPERS` (27),
`approval._CMD_WRAPPERS` (9), `approval._SHELL_NAMES` (5), `approval._WRAPPERS` (4),
`approval._DISKUTIL_DESTRUCTIVE` (6), `approval._FIND_EXEC_FLAGS` (4),
`approval.SUBCOMMAND_DEPTH` (11), `approval.UNSAFE_FLAGS` (2), `agent._WRITE_VERBS` (25),
`agent._CHARTER_WORDS` (3), `files._SENSITIVE_DIRS` (6) / `_SENSITIVE_NAMES` (11) /
`_SENSITIVE_SUFFIXES` (5), `curate.ARMING_TRIGGERS` (3), `curate.DEMANDING_VERBS` (3),
`rules.AISH_NOTE_MARKERS` (3), `dir_ignore.DEFAULT_IGNORE` (26),
`prompt.ATFILE_IGNORED_DIRS` (11). All of these are **program names, flag names, aish's own
grammar keywords or path patterns** — locale-invariant, so the failure mode that started this
(a language the list does not speak) does not apply. `files._SENSITIVE_*` is the one worth
doing next: it is `permits`-shaped (a miss lets an auto-approved `read_file` touch a
credential without a prompt) and it is the only entry in this paragraph where that is true.

## How a consultation is counted

`vocab.hit(name, needles, text)` is byte-for-byte the `any(needle in text for needle in
needles)` it replaces, short-circuit included, plus a `note()`. `vocab.note(name,
matched=…)` is for call sites whose matching is not a plain substring scan — a selector list
handed to Chrome, a cookie jar walked by name, a list comprehension that needs the hits
themselves. `vocab.looked_up` is the dict form.

**`asked` means consultations, not opportunities.** `browse.irreversible` tests seven lists
in order and returns at the first match, so a label caught by the first leaves the other six
**absent** from the record rather than at zero. That is what makes `never_consulted` mean
anything at all.

**`candidates` is recorded only where the call site already knows it** — how many mutating
words a name matched (`_only_chrome`), how many cookies were in the jar
(`_shed_reputation`), how many marks a login page carried (`captcha_vendor`), how many tokens
a command segment had. Where nothing can say, the key is **absent**, never 0, and
`Counters.mean_candidates` returns `None`: an average taken over a zero for every silent call
site is a number the log cannot support.

**`browse._has` is the one indirect counting site.** Seven of the irreversible lists are
consulted through it, and `TestTheCatalogueIsTheInventory` follows it by name — a sweep that
could not would report ten counted lists as uncounted, which is the false alarm that teaches
a reader to ignore a check.

### What it costs

Measured on this machine, on `browse._MUTATING_WORDS` (77 entries) at a real control name:
the bare scan is **0.38 µs**, the counted scan **0.44 µs** — **0.06 µs of overhead per
consultation**. A page of sixty controls making roughly eight consultations each therefore
pays about **0.027 ms**, against a page render measured in hundreds of milliseconds. The
counting is one lock acquisition and two integer increments; the lock is what keeps a browser
thread's consultation and the loop thread's from losing each other's updates.
`TestWhatCountingCosts` pins the order of magnitude rather than the number, so a machine
under load cannot fail it but a regression that made counting cost ten times the match still
would.

## The record — `kind: "vocab"`

Renderless (`session.RENDERLESS_STEPS`), emitted through `Agent._emit_record`, so it reaches
no renderer and is skipped on replay. Both halves, because either alone is the empty-live-card
bug that registry exists to prevent (`docs/trace-contract.md` §1.2, §1.3).

```json
{"kind": "vocab", "turn": 4,
 "lists": {"browse._MUTATING_WORDS": {"asked": 61, "matched": 3},
           "browser._CONSENT_SELECTORS": {"asked": 1, "matched": 0},
           "browse.CHROME_WORDS": {"asked": 3, "matched": 3,
                                   "candidates": 3, "candidates_asked": 3}}}
```

**One record per TASK, not per consultation**, and that is a volume decision with a number
behind it: a page of sixty controls asks `irreversible` sixty times and `is_worded` sixty
more, so a record each would be thousands of lines for one browse. The counters are sums
either way — `vocab.scan_counters` adds them back up — so the aggregation costs a reader
nothing it could otherwise have had.

**Nothing is written when nothing was consulted.** A task that browsed no page and ran no
command asks no list, and a record of zeros would say a set of controls had been consulted
and found nothing. That is corollary 2 of the trace contract read the right way round: what
must never be inferred from absence is a *decision*, and here the absence IS the fact.

**The flush is a `finally` at both entry points.** `Agent.run_task` is a thin wrapper around
`_run_task` for exactly this reason — that loop returns from a dozen places across the stop,
cancel and answer paths, and a flush attached to any subset of them would silently lose the
rest. `ClaudeMaxAgent.run_task` carries its own, because the SDK owns its loop and
`_run_task` never runs there; without it, a claude-max day would record no consultations at
all and every list would read as *not consulted*.

**Attribution across concurrent chats is imprecise, and nothing depends on it.** The tally
registry is process-global, so a consultation made between tasks — a `/browser` status line,
a server thread — lands on the next record that flushes, in whichever chat that is. The
counters are per LIST across a window and never per chat (`usage._role_counters` gives the
same reason for charters), so **no reported number depends on which record a consultation
landed in**.

**`browse.CONSENT_TALLY` stays exactly as it was.** It is the live `/browser` line, is
process-lifetime by design, and its single-writer discipline is pinned by test. It now also
calls `vocab.note` from inside `ConsentTally.note`, so the same single event reaches the
persisted counters — one writer still, and the consent list is the only one with both a live
line and a record.

## Reading it back

### `aish vocab`, and why not `aish usage`

`aish vocab [--days N | --all] [--json]`. Three sections: lists that were consulted (quiet
ones first, flagged), lists **not consulted** in the window, and lists **inventoried but not
counted**.

**Its own subcommand, and the reason is what `usage` IS.** That report answers *what did this
cost* — spend, context, residency, every figure in tokens on a bill. The isolated roles are
in it because a role call is real money on the same key. **A word-list consultation costs
nothing and is not about spend at all**; folding it in would make one report two, and would
put something that is not spend inside a `--json` spend document.

**What does go into `usage` is a one-line pointer**, in `_caveats`, where he already looks. It
names the quiet lists and points at `aish vocab`. It never prints the table.

### Recorded always, surfaced on anomaly

The owner has rejected over-vocal reporting twice, and the reasoning is not politeness: **a
warning that fires on ordinary browsing teaches him to stop reading it**, which costs more
than no counter at all. So every consultation is recorded and the pointer is silent unless
something is anomalous.

**"Anomalous" is derived from the window's own data, never chosen.**
`vocab.floor(counters, over=n)` is the rarest match rate achieved by a working list that was
consulted **at least `n` times**. `vocab.quiet` flags a list that matched **nothing** where
`asked × floor(over=asked) ≥ 1` — *at the worst rate a comparably consulted working list in
this window managed, this one would have been expected to match at least once, and it matched
zero*. There is exactly one number in that sentence, and it is **one match**. No confidence
level, no threshold anybody picked.

**The `over=` clause is the over-flagging guard, and it was added because the first version
cried wolf.** Driven against a seeded corpus, a list consulted **twice** that happened to
match once set a 50% bar for every list in the window, including ones consulted hundreds of
times. That is precisely the reporting the owner has twice rejected. So each candidate is
judged only against lists observed at least as often as it was, and a candidate with nothing
comparable is **not flagged at all**.
`TestTheAnomalyFloor::test_a_barely_consulted_list_never_sets_the_bar_for_a_busy_one`.

**What it does not claim.** It is not *"this list is broken"*. A list can be correctly silent
— `_CLOSE_ACCOUNT_PHRASES` should match nothing on almost every page there is. It says a list
was asked more often than the corpus's own worst comparable working rate needs and did not
fire; whether that is wrong is read off the `on_miss` column and then off the code. The
renderer prints that sentence under the flag rather than leaving it to be inferred.

**It is a WEAK instrument and nothing pretends otherwise.** The floor is a property of
whatever happened to be consulted in the window, not of the web. A window in which one list
matched on every consultation sets a 100% bar; a window of one afternoon's browsing of one
site says very little about anything. It is a pointer at rows worth reading, and the reading
is the code.

**Two ways nothing can be judged, and both report as an absent comparison rather than a pass.**
A window in which nothing ever matched at all cannot tell you which list is broken — it is as
likely to be a window with no browsing in it. And the single most-consulted list in a window
has nothing observed as often as itself, so it is never flagged. In both cases `floor` returns
`None`, `expected_at_floor` returns `None`, and `json_report` **omits** `floor_rate` rather
than reporting it as 0.

### Absence is not zero

Three states, kept apart all the way to the screen, following the discipline
`usage.NOT_RECORDED` and the sign-in verdict tri-state already set:

1. **not consulted** — declared, no record in the window. The code path did not run. Editing
   its strings would change nothing.
2. **consulted, never matched** — the words are the suspect.
3. **consulted and matching** — a rate.

And a fourth that is not a state of the counter but of this work: **inventoried, not
counted** (`vocab.not_counted`). It gets its own heading and is never mixed into the measured
rows, because a list that is consulted constantly and simply not counted must never be
printed as *not consulted*.

## The checks, and which of them can fail

*A check that cannot fail is documentation wearing a test's clothes* (`docs/roles.md`). A
counter that can only ever go up is exactly that shape, so **every counter's zero is driven
to, through the real call site**, in `TestEveryCountersZeroIsReachable` — a consent
obstruction that could not be cleared, a page with no challenge marker, a mail that is not a
sign-in, a command the safe list does not hold, an availability state with no phrase, a
control that names no file, a carousel arrow that is not a month arrow.

`TestTheCountingChangedNoMatching` is the scope fence, against `git show`.
`TestWhatACounterCounts` and `TestTheRecordAndTheScan` pin the tally and the pure scan.
`TestTheAnomalyFloor` pins the derivation, both directions — a list flagged, and a list with
too few consultations deliberately not flagged. `TestAbsenceIsNotZero` pins the tri-state and
the omitted `floor_rate`. `TestTheCatalogueIsTheInventory` pins both directions of the
catalogue: a counted name nothing declares, and a declared name nothing counts.
`TestTheRecordReachesTheLog` pins the empty case, the one-record case, the renderless
registration and the `finally` at both entry points. `TestTheReader` pins the silence on an
ordinary window and the pointer on an anomalous one. `TestWhatCountingCosts` pins the cost.

## What this does not do, stated because it is what a reader will assume

**Nothing reads a counter to decide anything.** A record is detection and never protection
(#295 P2), and a counter even less so. No gate, refusal, grant or list is loosened, widened or
checked less carefully because a number now exists — and *"it would have shown up in `aish
vocab`"* is the same violation in the past tense.

**A counter cannot tell you a list is right.** It tells you a list fired, or did not. A list
matching at a healthy rate on the wrong things looks identical here to one matching on the
right ones; that question is the exam's, not the counter's.

**These numbers begin at this commit.** Every log written before it carries no `vocab` record
at all, and the report says so rather than rendering an empty window as a clean bill.
