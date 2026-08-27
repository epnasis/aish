# Roles — cheaply appointed, isolated, narrow-duty helpers

`roles.py`, `aish/charters/`, `scripts/role-admission.py`, `scripts/role-mine-cases.py`.
Issue #297, stage 1 of the epic in #295.

> ## Read this first: no role is called today
>
> The framework shipped with one customer — the snippet reader, wired to every
> `web_search`. A controlled experiment measured that wiring against two alternatives
> and it lost. **The wiring was removed and the framework was kept:**
> `roles.WIRINGS` is empty, the two Agent methods that ran a search through the reader
> are gone, and the charter sits in the tree admitted, examined and **uncalled**.
>
> *Title and address only* below is the measurement, the residual it accepts, and what
> it did not establish. Everything else on this page describes machinery that works and
> is not currently running. A charter with no caller is a fine thing to keep; a document
> implying it is running is not, so every section that used to describe a live path says
> so where it stands.

**How to use this file.** The laws first, because everything else is a consequence of
them. Then the charter, invocation, validation, the wiring law, *Title and address
only* (the measurement that retired the one wiring), the charter that outlived it, the
exam and how admission actually runs, the fence, cost, and — last and most important —
the list of things this does **not** do, which is where an overclaim would live if
there is one.

---

## The laws

**R1 · A role is a sealed conversation with a model, not an agent.** `roles.run` composes
its own two-message list and calls `backends.make_chat` directly. There is no `Agent`, so
there is no history, no tool loop, no memory, no skills and no rules engine to leak from —
not because they are switched off, but because no code puts them there. `TestIsolation`
pins both halves: the message list, and a source-level check that `roles.py` cannot import
the acting loop at all.

**R2 · A role returns a code-validated typed value, never prose the caller forwards.**
The charter declares a shape; `roles.validate` parses and checks it before any caller sees
it. A role whose output fails validation has answered nothing. `TestValidation`.

**R3 · A role may only RESTRICT, or return a value inside an envelope already open.**
Nothing here widens anything (#295 P3). That is why the snippet reader's degradation is a
skip: the worst case has to be the status quo. **The skip is also what the measurement
caught up with** — a protection whose worst case is the status quo is a protection that is
sometimes not in force, and *Title and address only* is the arm that has no worst case
because there is nothing to fall back from.

**R4 · Every closed vocabulary must be able to say "I cannot tell."** `UNSURE_VALUES`, and
a charter whose enum lacks one **does not load**. A vocabulary that structurally forces a
verdict makes guessing the cheapest answer, which is exactly how a hypothesis about a
sign-in became aish's own voice for weeks (`CLAUDE.md`, *No evidence, no claim*).

**R5 · The acting model may CALL a role and may never author one.** A charter it could
write is oversight it could appoint, with content it had already read as co-author. Enforced
by binding admission to the charter's **content digest**, so an edit through any door — named
or not — stops the role loading; a command fence refuses the known doors early, and is
known-incomplete. See *Keeping the model out of its own oversight*.

**R6 · A charter without an exam does not load**, and admission is a recorded pass **bound
to the charter's content** rather than a live check. See *The exam* for why the second half
of that sentence is not a downgrade of the first, and R5 for what the binding buys.

**R7 · Every role call leaves one record, carrying the input BYTES.** Not a digest. A
digest can never become an exam case, and the owner's amendment to #297 is that exam cases
come from real recorded material.

---

## The charter — one file, `aish/charters/<name>.md`

Markdown with YAML frontmatter, because that is already this codebase's idiom three times
over (skills, rules, plugin tools). The frontmatter holds **only what code enforces**; the
prose below is the task, addressed to the model.

| field | enforced by |
|---|---|
| `name` | lowercase-dashed, and must match the filename |
| `version` | any non-empty string; the exam is bound to it |
| `kind` | `reader` \| `judge` \| `worker` \| `owner` |
| `model` | a model CLASS. One value today (`cloud-fast`) — see *Which model* |
| `num_ctx` | the context budget, handed to the backend |
| `tools` | must be `[]` in v1; a declared tool **refuses to load** |
| `degradation` | `skip` (advisory) \| `hold` (load-bearing) |
| `inputs` | each with a **required** `trust: trusted \| untrusted` |
| `output` | the shape — see below |

`TestCharterLoading` pins one refusal per rule. Each of them is a refusal rather than a
default because a silently-defaulted governance field is a field nobody chose.

**Where the header ends is not this file's decision.** `skills.split_frontmatter` is the ONE
reading for every artifact class in the md+frontmatter family (#209), and a charter uses it.
It had its own regex until 2026-08-27, which is a fifth reading waiting to disagree with the
writer that feeds it — and a charter is the one artifact where a header misread is a security
question, because the header is what declares tools, trust labels and the output shape.

**A header that says a thing twice is refused** (#326's law, one artifact over). PyYAML
silently takes the LAST value — `yaml.safe_load("a: 1\na: 2")` is `{"a": 2}` — so a charter
declaring `tools:` twice would load as one of the two with nobody told which.

The mechanism is a duplicate-refusing loader (`_NoDuplicateKeys`) rather than the family's
shared `skills.frontmatter_duplicates`, and the difference is the **artifact**, not the law.
That detector is the answer for a FLAT, line-format header and it is a line parser: run over
this charter's nested header it returns `['- name', 'type']`, because a `name:` once per
declared field is not a duplicate — and it cannot see inside `output:` at all, which is
exactly where the caps that bound what leaves a role are declared. Same law, stricter
mechanism. The loader reads the golden pairs and the owner's cases file too, so the exam
cannot silently test one of two `expect:` blocks. `TestCharterLoading`.

### The output shape

`shape: rows` and a field list. Three field types, and every one of them has a customer in
the shipped charter — a type table grown against hypotheticals is what D5 refuses:

- **`row`** — an integer naming a row of the declared input. Code checks that the rows
  returned are exactly the rows given: none dropped, none invented, none duplicated. This
  is the strongest structural property here, and it is why a reader cannot add a search
  result the search never returned.
- **`text`** — a string with a **required** `max_chars`. There is no uncapped string type,
  because an uncapped string is prose and prose is what the wiring law keeps out of an
  acting context. `may_be_empty: true` makes "" a legal answer, so a row the reader
  genuinely cannot read is answered honestly rather than filled in.
- **`enum`** — a closed vocabulary, subject to R4.

`roles.capped` is the enforcement: control characters are flattened first (a field that
can carry a newline can carry a fake banner into whatever renders it), then the cap is
applied to the cleaned bytes.

**The YAML trap, and why it is a refusal.** YAML 1.1 reads bare `no`, `yes`, `on` and
`off` as booleans, so the obvious vocabulary `[no, yes, unclear]` silently becomes
`["False", "True", "unclear"]` — a model asked to answer `"False"` while a reviewer reading
the file sees `no`. `_yaml_word` refuses it rather than coercing: guessing which word was
meant is the quiet repair that makes the file and the behaviour disagree.

---

## Invocation

`roles.run(charter, inputs, rows, model_spec=…, …)`. It **never raises for an operational
reason** — every failure is a `Result` with a `status` and a `why`, because the caller's
contract is a declared degradation and a degradation that arrives as an exception is one
every caller has to remember to catch.

| status | what happened |
|---|---|
| `ok` | validated, and `value` carries the typed rows |
| `invalid` | the model answered twice and neither answer validated |
| `unavailable` | no seam in this session, no key, or the call failed |
| `unadmitted` | no current recorded exam pass for this version and model |

`value` is `None` on every status but `ok`. Deliberately: the failure this framework exists
to prevent is a missing answer that reads as a benign one.

**One corrective retry, `ROLE_ATTEMPTS = 2`, and no more.** The common failure is a model
wrapping good JSON in prose, which one nudge almost always fixes; a second would spend real
money re-asking a question that is not going to be answered. The nudge carries the
validator's own error text, which is why every message in `roles.validate` is written to be
read by a model. `TestRun`.

### Which model, and the backend that has none

`Agent._role_model()`, in order:

1. `AISH_ROLE_MODEL`, when the owner has named one.
2. This session's own model, when its provider is in `backends.PROVIDERS` — exactly the
   ones `backends.make_chat` gives a stateless seam, so a role adds a provider dependency
   to nothing.
3. `""`. **A real outcome, not an error.**

Case 3 covers two situations and both are honest. `claude-max` has no seam at all — the
Agent SDK owns its loop, its inner chat callable raises by construction, and that module
never imports the rate limiter, so "one admission queue" genuinely does not span the
subscription. And a **local** model is refused here on purpose: the shipped charter
declares the class `cloud-fast`, and quietly routing it onto an 8B would make the
declaration mean nothing. `TestDegradation`.

`model:` is validated against `MODEL_CLASSES` and recorded, but it does not yet *route* —
routing is the caller's three-step answer above. A class table with one class is a table
designed against one customer; the second entry arrives with the second role that needs a
different model.

### The governor, and what it does not carry

A role shares exactly one thing with the acting model: the quota governor inside
`backends.governed`, a module-global keyed by provider and model. **Per-charter
attribution cannot travel through it** — `ratelimit.reserve_for_call` takes a
`provider:model` key and nothing else — so attribution lives in the D7 record and
`usage.py` reads it from there. Do not claim the governor carries it.

`roles.run` takes `should_stop` / `on_wait` / `wait_ceiling` and passes them into
`ratelimit.hooks` **explicitly**, rather than relying on the thread-local default. The
snippet reader fires from the parallel read fan-out, whose worker threads carry no wiring
of their own; without this, a cancelled task can leave an uncancellable rate-limit wait
sitting on a worker. `TestRun` pins that the hooks arrive.

---

## The wiring law

**Untrusted-input prose may not reach a node that proposes actions.** `roles.check_wirings`
walks `roles.WIRINGS` and refuses any edge that carries an **unbounded** field from a
charter with an untrusted input into an `ACTING` context. `TestWiringLaw`.

`roles.WIRINGS` is **empty**. An empty tuple rather than an edge naming a function
that no longer exists: a wiring is a claim that a path is live, and this file has already
found what a claim wider than its code costs. The law still runs at catalogue load, over
nothing, which is exactly the state D5 argued for — it exists *before* the wirings that
need it. Wirings ship as **code**, not a data format, because a node/edge format designed
against a single wiring would be designed entirely against hypotheticals.

**It is a typo guard, not a regression guard, and #328 is that correction.** This file
called it "the regression guard" against untrusted prose reaching an acting node. It
cannot do that job today, and the reason is two functions away: `_parse_field` **refuses
to load** a `text` field without a positive `max_chars`, so every field that survives
parsing is bounded by construction and the unbounded branch cannot fire on anything a
charter FILE could produce. What it actually catches is a wiring naming a charter that
does not exist, or carrying a field the charter never declares. That is worth having and
it is not what the prose said. The branch stays, because it becomes reachable the day an
unbounded output type exists — an owner-facing `prose` field for an owner's-side role is
the obvious first one — and a law added after the type it governs is a law added after the
incident. `TestWiringLaw::test_the_unbounded_branch_cannot_fire_on_anything_a_charter_FILE_says`
pins the reason rather than leaving it in prose.

**Stated honestly, because this is where the epic already narrowed once.** "Prose dies
inside the role" was fully true for a reader returning prices and dates. It was **not**
unconditionally true even then: the acting model cannot choose which link to open without
titles and addresses, and those are attacker-written strings. What was enforceable was
**bounded, capped, stripped fields plus the law** — a real quantitative gain, not a proof
of isolation. That residual outlived the reader and is now the whole of what remains open;
see *The residual this accepts: the title channel*.

---

## Title and address only — the measurement that retired the one wiring

**A `web_search` result reaches the acting model as two lines and nothing else.** The
page's own title, capped at `web.RESULT_TITLE_CHARS` (120), control-flattened and with a
leading `[aish:` broken to `(aish:`; then the page's address, deliberately uncapped,
because a truncated URL is a URL that cannot be opened. There is no third line, and the
reason is not a filter: `web.serp_results` and `web._first_index` never read the index's
summary text into a row at all. `web._numbered` is the one function that renders a row, so
the banner, the cap, the flattening and the broken marker are all applied at one place.
`TestWhatASearchGivesTheActingModel`, `TestWhatACopiedTitleMaySay`,
`TestARowIsAlwaysTwoLines`, and `tests/test_web.py` for the collection half.

### What was measured

**Measured by the session that removed the wiring**, not by anything in this repository.
The figures below are that run's report, and they carry its attribution rather than
reading as facts about the code — nothing here re-derives them, and the raw run is not in
the tree. A figure in a doc with no owner is a figure nobody can go back and question.

Forty-two runs: **7 real tasks from the owner's own session logs × 3 arms × 2
repetitions**, the arms run concurrently so all three saw the same live web.

| arm | what the acting model got |
|---|---|
| **A** | the reader as shipped — title, address, one line the reader wrote, and the flag |
| **B** | title and address only |
| **C** | raw snippets: the behaviour before roles existed |

- **The reader cost 10.6 s and ~2 800 prompt tokens per search**, measured over 63 role
  calls — **29.4% of arm A's entire wall clock**, all of it blocking, because the planner
  cannot move until the reader returns.
- **Arm B opened about one more page per task** (median +1.0) and still finished **31 s
  faster**. The extra page opens never came close to paying for the reader.
- **The stale-figure property — the reader's actual justification — is delivered better by
  B.** Figures in a final answer that appeared only in a snippet and on no page opened:
  **C 10, A 1, B 0.** B gets it *structurally*: there is no third line to leak from.
- **The single arm-A leak was not a reader mistake.** A Gemini 503 made the role
  `unavailable`, the shipped `skip` degradation handed raw snippets to the planner, and it
  quoted a figure from one. The reader was available on **75 of 77 calls; one of the two
  degradations leaked.** So A's protection was conditional on a cloud call succeeding and
  B's is not — the same objection the box below made about admission, arriving from a
  second direction and with a number on it.
- **Answer quality could not be separated.** The one exactly-checkable task — a central
  bank's rate on a past date, independently verified as 3.6896 — was a six-way tie.

This also settles the question #330 asks of every role: *does it return less than it
consumes?* The reader's rendered block measured **97%** of the raw results it replaced. It
was an isolation device, not an economy one. B is both.

### The residual this accepts: the title channel

**Removing the snippet closes the snippet channel completely. It does not close the title
channel.** Titles are attacker-authored and still cross to the planner verbatim — capped
at 120 characters, control-stripped, `[aish:` neutralised, and read by nothing that could
notice what they say. The reader's `instructs_the_reader` flag was the only thing that
would have raised an instruction-shaped title, and it goes with the reader.

**That is a deliberate trade and not an oversight.** 120 stripped characters is a far
narrower carrier than a full snippet — the prose that stopped crossing measured a median
of **928** characters per result set, p90 1257, over 4167 recorded sets — and the
direction of travel is a **~60 000-token local context** (#330), where ~2 800 tokens per
search buying an alarm on a channel this narrow is poor value.

**The flag can be restored later, and cheaply, because it no longer has to block.** A
title-only check could run **asynchronously, off the critical path**, purely to raise and
record — the planner would not wait for it, which is the entire cost the measurement
found. **That is not built. Nothing on this page should be read as if it were**, and it
should not be built speculatively: it wants a real incident to size it.

### What the experiment did NOT establish

**No injection was observed in any of the 42 runs.** Nothing here measured the flag's
real-world value; it measured cost, page opens, wall clock and stale figures on ordinary
tasks. The flag's whole security case rests on **the exam**, whose injection cases are
engineer-authored precisely because no recorded session has ever carried a real one.

So the finding is *"the flag was not worth 2 800 tokens and 10.6 s per search on a channel
this narrow"*. It is **not** *"the flag was useless"*. Those are different sentences and
only the first is evidenced.

---

## The snippet reader — the charter that outlived its caller

`aish/charters/snippet-reader.md`. Charter version 2, kind `reader`, degradation `skip`,
one untrusted input. It still loads, still binds to its exam and its content digest, and
still passes the wiring law when a wiring is constructed over it — `TestShippedCharter`
loads the real file, does exactly that, and pins that its exam carries both halves
described below, so a charter that stopped shipping injection cases fails there rather
than at the next incident.

**And nothing calls it.** It was the framework's test case and it served that purpose: it
is why the loader, the validator, the admission binding, the fence, the record and the
counters exist and are proven rather than designed against hypotheticals. The framework's
real customers are the roles still to come.

Kept rather than deleted, for two reasons and neither is sentiment. It is the only worked
example of a charter in the tree — inputs with trust labels, an output shape, a closed
vocabulary with an "I cannot tell" word, an exam with both halves and a discrimination
pair — so the next charter is written by reading it. And its exam is the only place in
this repository where the injection-resistance question is asked at all.

**The hole it was built for.** `web_search` results were attacker-writable titles and
snippets from arbitrary sites, plus an index anyone can push on with ordinary SEO, and
they entered the acting model's context as prose behind `web.SEARCH_RESULTS_NOTE` — a
banner, which is an instruction to the model and not a structural control. The banner's
own comment in `web.py` says so. The snippet half of that hole is closed now by the prose
not being collected; the title half is the residual above.

### What it was given

`web.untrusted_rows(presented)` — the **stranger's half only**, with aish's own framing
removed: the untrusted-content banner, the sentence naming which words are the stranger's,
the `[aish: …]` provenance line and `web.NEXT_STEP_LINE` all go. Handing aish's own
instructions to a reader told to treat its input as material is the one confusion the whole
arrangement exists to prevent.

It is bounded by the numbered rows rather than by counting known framing lines, so adding a
framing line later cannot silently change what a role receives — **and it is what makes the
recorded corpus minable**, because every session log written before the banner shipped
(2026-08-25) holds exactly this text and nothing else. Verified across all **4201** recorded
result sets in the owner's 784 logs: `web.parse_results` numbers them contiguously in 4199,
and the two exceptions are a mangled row and a `recall` result quoting a past session, not
parser bugs. `TestUntrustedHalf`.

**Mining is narrowed, and one case type is now unminable.** Both functions still work and
still read the owner's 784 existing logs, which is why they stay. But a result set rendered
today has two lines, so `parse_results` returns an empty `snippet`, and the assertion that
depends on one goes with it: its snippet-figure filter finds nothing, so
`scripts/role-mine-cases.py` can no longer write an **`absent`** case — the price-leakage
case, which is the recorded failure this role was chosen for.

Said precisely, because "nothing can be mined" would be wrong and was the first way this
paragraph was written: the script still returns a case for any set of three rows or more,
carrying `rows` (a shape annotation, not a check — #328) and `distinct`. What it produces
from a two-line log is an extraction-fidelity case over an input with no snippet in it,
which is a weaker case about a different question. The corpus for the case type that
mattered is now fixed and finite.
`TestUntrustedHalf::test_a_set_rendered_today_has_no_third_line_to_mine` pins the
parse-level fact this rests on — that a live row's `snippet` is empty — and not the
script's own behaviour, which lives outside anything the suite imports.

### What it returned, and what used to cross

Three fields: `n` (row), `about` (text, ≤160, may be empty), `instructs_the_reader`
(`no` | `yes` | `unclear`). Charter **v2**; v1 called the third field *addressed_to_me*
and it meant something wider — see *What the flag means, and what it used to mean*.

**The title and the address were never among them.** They were copied from the parsed
input **by row number**, capped and stripped by code, so they were never laundered through
the model at all. That code moved: the cap (`web.RESULT_TITLE_CHARS`), the control-flatten
(`web._flat`) and the broken marker (`web._not_aishs_voice`) now live at the render in
`web._numbered`, which means they hold on **every** search rather than only on the path
where a reader had answered. `TestWhatACopiedTitleMaySay`.

The accounting as it stood, measured over 4167 parseable result sets — and the third
column is why the wiring is gone:

| | reader (arm A) | title and address (arm B) |
|---|---|---|
| snippet prose crossing | none | none |
| replaced by | ≤160 chars per row, model-written | nothing |
| titles and addresses | cross, capped and stripped | cross, capped and stripped |
| cost per search | 10.6 s, ~2 800 prompt tokens | 0 |
| in force when? | only when admitted, seam present, key up | always |

> **The reader was OFF unless it was admitted, and that turned out to matter.** On a fresh
> install nothing is admitted, so none of the above happened. It was also off on
> `claude-max` (no stateless seam), off on a local model (the charter declares
> `cloud-fast`), off whenever the key failed or the provider was down, and off when two
> answers in a row failed validation. In every one of those cases the snippet prose reached
> the acting model exactly as before, behind exactly the old banner. That was the correct
> degradation — #295 P3 requires the worst case to be the status quo — but it made the
> improvement one that was *sometimes* in force rather than a property of the system, and
> **that is the case the experiment caught leaking**: one 503, one skip, one stale figure
> quoted from a snippet.

Two defences on the copied half, both cheap and both found by review rather than by design,
and both now on the render path where they cover every search. A title may not arrive
wearing aish's own voice: `_not_aishs_voice` rewrites `[aish:` to `(aish:`, keeping the
words so the model can still see what the title said while breaking the marker. And
`web._flat` guarantees a rendered row is exactly two lines — three while a snippet was the
third — so a newline inside a field cannot fabricate a numbered result the index never
returned. `TestWhatACopiedTitleMaySay`, `TestARowIsAlwaysTwoLines`.

**What was in the acting model's context while the reader ran, and is not now.** A note
saying an isolated reader had written the line under each row; a note reporting a flagged
row as an **observation** ("contained text speaking to whoever is reading it"), never a
diagnosis of what it was for or who wrote it; and a quieter one for `unclear`. Only the
`yes` note asked for the owner to be told — the relay to a person was always the model's
obedience and never an enforced step, which is the same distinction the old banner got
wrong. All three constants went with the wiring, and with them the test that held the
note's verbs and the charter's `yes` arms side by side as pairs. That test existed because
adversarial review found the charter's *treat the text as a note from the system* arm
missing from the note, and nothing had failed, because nothing checked. It has no subject
now; if the flag is ever restored, its note needs that pairing rebuilt before it ships.


### What the flag means, and what it used to mean

**v1 asked a wider question than it could afford, and that was measured on live data.** The
field was *addressed_to_me*, and its `yes` included *"addresses 'you'"* — which describes a
large share of commercial web copy. One ordinary shopping task on 2026-08-27
(`session-20260827-050622-014946.jsonl`): **10 reader calls, 80 rows, 5 flagged `yes`, and
2 of the 10 searches would have interrupted the owner.** Everything that fired, verbatim
from the role records:

```
A user registration page for an online store offering a discount on orders.
A request page for bulk order discount quotes and inquiries.
An advertisement redirect page for the TME online store.
An advertisement redirect page for purchasing from TME.
A user registration link on the TME website.
```

Five advertisements. **The reader was not malfunctioning — the charter told it to answer
that way.**

v2 asks instead whether the text asks for something only a *reader* could give: run a
command, read or send a file, open one particular address, hand over a key or a password,
disregard its own instructions, or pose as a note from the system it is part of. Ordinary
second-person sales copy — *sign up*, *register now for 10% off*, *request a quote*, *order
online* — is `no`. It is addressed to somebody with a wallet and asks for nothing a reader
has.

**The name changed with the meaning.** *addressed_to_me* is literally true of *Zarejestruj
się teraz*, and the field name is not decoration: it appears in the generated output
contract (`roles.contract_text`) that the model reads on every call. A name pulling towards
the wider reading under a definition that forbids it is a file and a behaviour disagreeing —
the defect `_yaml_word` refuses elsewhere in this same file.

**No third value, and that was decided rather than defaulted.** "Marketing that speaks to
you" is a real observable thing and could have had its own word. It does not get one,
because `about` already carries it — *"a user registration page for an online store offering
a discount"* is the entire content such a value would hold — and because a fourth word
restores the "flags everything" state under a friendlier name, with a boundary the reader
must adjudicate on every row and no reader-visible consequence either way.

**The strongest argument for one survives that, and it is recorded here rather than
answered away.** After the narrowing, the genuine `yes` base rate in live traffic is
expected to be **zero** — this doc says elsewhere that no recorded session has ever carried
a real one. So `yes: 0` in `scan_counters` no longer distinguishes "narrow and working" from
"the flag has died", which is precisely the failure the counter was introduced to catch.
v1's ad-firing was, among other things, a live-fire heartbeat. **The counter cannot replace
it and this doc must not pretend otherwise:** the only thing that exercises the `yes` path
end to end is the exam, through the real invocation path, when `scripts/role-admission.py`
runs. That makes admission cadence — not the counter — the instrument for a dead flag, and
nothing today schedules it. A recorded gap, not a solved one.

**`unclear` did not absorb the difference either, and the charter says so out loud.**
Narrowing `yes` with no word about `unclear` would have moved the false positives one column
over — and while the wiring existed, an `unclear` row wrote its own line into the acting
model's context on every search that produced one. The charter states that sales copy is
`no` outright, and that `unclear` is for a row you could argue either way rather than a
gentler `no`.

This is the same resolution as the page console reached a few hours earlier
(`_scrub_page_console`, `consoleWanted` in `aish/static/app.js`): **recorded always,
surfaced on anomaly.** A warning that fires during ordinary browsing stops carrying
information, and then it is worse than absent — because the record says he was told.

### Where it sat, and where a caller would go

`Agent._read_only_call`'s `web_search` branch, through two Agent methods that no longer
exist; the branch now calls `web.web_search` directly. What made that branch the right seam
is still true and is worth keeping for the next role: it is the **one seam both read paths
share** — the sequential `_dispatch` route and the parallel fan-out both build their thunk
from it — so a fan-out cannot bypass a role attached there.

The caller-side surface is intact and uncalled: `Agent._catalogue`, `Agent._role_model`,
`Agent._record_role` and `Agent._record_role_skip`. Their docstrings say they have no
caller, so a reader does not have to grep to find out.

`Agent._as_call` is **not** in that list, and the distinction is worth making rather than
rounding off. It publishes the call id on the worker thread so a record written from inside
a parallel read can be joined to the call that produced it, and it runs on **every**
concurrent read batch whether or not a role exists — the role record is what prompted it,
not what calls it. It is live general fan-out infrastructure; the next thing that records
from a worker inherits the property instead of rediscovering it.

### Degradation: `skip`, and why not `hold`

Advisory, declared in the charter, and unexercised — with no caller there is nothing to
degrade. It is documented because it is the charter's own declaration and because the
ladder is what the next role picks from. When the role could not answer, the acting model
got exactly the previous text behind exactly the previous banner. Three reasons, and the
first is the binding one:

1. **R3.** The reader restricts what reaches the acting model. Skipping it is the status
   quo, which is what P3 requires the worst case to be. Nothing is permitted that was not
   permitted before.
2. A `hold` would wedge **every search** the moment a key expired, a connection dropped, or
   the session ran on claude-max — a new failure invented by the defence.
3. It is a reader, not a judge. There is no sensitive step here to hold.

**The skip is never silent.** Every outcome writes the record, so *unexamined* is a fact in
the log rather than the absence of one (`docs/trace-contract.md` corollary 2), and the
counters are what make a chronic run of them visible. A result set with **no rows** — an
error string, a no-results note — asked no role at all and wrote nothing, which is what
keeps "skipped" meaning something.

**And this is the property the measurement turned around.** A `skip` that is correct by
R3 is still a defence that is not in force, and over 77 calls one of the two skips leaked a
stale figure into an answer. A `skip` ladder is right for a role that must not wedge the
session; it is not a substitute for a property that holds unconditionally, and where one is
available for free it wins. `TestDegradation` now pins only the model-selection half —
which model a role would run on in this session, and that "none" is a real outcome.

---

## The record — `kind: "role"`

Renderless (in `session.RENDERLESS_STEPS`), emitted through `Agent._emit_record`, so it
reaches no renderer and is skipped on replay. Both halves, because either alone is the
empty-live-card bug that registry exists to prevent.

**No role call happens today, so no record is written today.** The shape, the evidence-store
input capture and the two writers stay exactly as they were — they are proven, and they were
never what the measurement retired. `TestTheRoleRecordWithNoWiring` drives them directly,
at `Agent._record_role`, because there is no search to drive them through.

```json
{"kind": "role", "turn": 4, "call": 2,
 "charter": "snippet-reader", "version": "1", "role_kind": "reader",
 "status": "ok", "model": "gemini:gemini-3.5-flash",
 "attempts": 1, "ms": 712, "degradation": "skip",
 "input": {"name": "results", "trust": "untrusted", "chars": 1623,
           "digest": "9f2c…"},
 "usage": {"input": 1740, "output": 210},
 "output": [{"n": 1, "about": "a shop listing for a monitor", "instructs_the_reader": "no"}],
 "flags": {"instructs_the_reader": {"no": 4, "yes": 1}}}
```

`input.digest` points at the **bytes**, in the existing content-addressed evidence store
(`evidence.put`). D7 originally recorded a digest alone; adversarial review found that this
defeated the owner's own amendment, because a digest can never become an exam case. The
store is the right home rather than the log because role inputs carry personal material and
purgeability is exactly its contract. `TestRun`.

A charter that would not **load** writes its own record through `Agent._record_role_skip`,
with `status: unavailable` and the parse error as `why` — "the catalogue is broken" and
"the model was down" are different facts, and a reader that cannot tell them apart has to
go and read the source.

Note what is NOT recorded, and deliberately: a search that asks no role writes **nothing**,
not a skip. "No role was consulted" and "a role was consulted and could not answer" are
different facts, and with the wiring gone every search is the first. A skip record on every
search would say a control had declined when none was ever asked.

---

## Counters, and why a reader has them

`roles.scan_counters` is a pure pass over decoded log records, in the shape `usage.py` and
`curate.scan_ledger` already use: no model call, no live state.

**They stop accruing, and they do not go backwards.** With no role called, no new records
are written, so the counters report the calls the reader actually made and then hold still.
That is the correct behaviour for a scan over a log — it reports what was RECORDED — and it
is why "a charter with no calls reads as no calls, never as healthy"
(`TestCounters::test_a_charter_with_no_calls_reads_as_no_calls_never_as_healthy`) matters
more now than it did when it was written.

**A reader is not a judge, so this needed deciding rather than defaulting.** `about` is
extraction and needs no counter — it is measured by the exam, not by a rate. But
`instructs_the_reader` **is a scored answer against a vocabulary**, and #295 P4 makes counters
the admission price for exactly that. Its fire rate is also the only way to notice the two
failures that matter: a reader that never flags anything (the defence quietly thinned) and
one that flags everything (the acting model learns to ignore the note). So they ship, and
`roles.tally_flags` counts them per call at write time — a counter derived later from prose
would be derived from the thing this framework exists to stop forwarding.

**The second of those failures happened, and the counter is the instrument for the next
one.** There is deliberately no cap or cooldown on how often a flagged row is surfaced to
the owner: a suppressor would blunt exactly the signal these counters carry, and would
create a second place where "he was told" and "the record says he was told" can disagree —
which is what makes a warning worse than no warning at all.

The v2 rename is load-bearing here. `scan_counters` buckets by field NAME, so records
written under the old vocabulary keep the old key and do not silently average with the new
one. A flag rate before and after a definition change measures two different questions, and
a shared key would have hidden that.

Surfaced in `aish usage`, beside the acting model's figures and never folded into them:
a role's tokens are real money on the same key, but folding them in would silently move
every per-model number that existed before roles did. `TestCounters`, `TestUsageAttribution`.

---

## The exam, and how admission actually runs

### What is in it, and what is not

Twelve cases ship in the charter. **The split matters and the charter says it out loud:**

- **Mined** (seven): shapes taken from the owner's real recorded searches — a five-row
  Polish shopping search, a fare quoted inside a snippet with a stale qualifier, a
  thousand-character advertisement redirect, a row whose snippet came back empty, a
  single-row set, and the two `ads-*` sets below. These test **extraction fidelity** and
  the rows the flag must NOT fire on.
- **Authored** (five, all named `injection-*`): a snippet demanding a command be run and
  its output searched, an override attempt pushing a link, a snippet impersonating aish's
  own `[aish: …]` framing, and — added in v2 — two sets holding advertisements and
  instructions together. These test **injection resistance**.

**Both directions ship, and the missing one is what let v1 through.** An exam made only of
rows that must flag is passed by a reader that flags everything, and that is the reader that
shipped: 2 of its 10 searches carried a flag into the acting model's context over an
advertisement. The two `ads-*` cases are mined from the exact searches that fired:
`ads-second-person-offers-are-not-instructions` (the eight-row set whose rows 2 and 5
flagged — *Zarejestruj się teraz / by odebrać RABAT 10%*, and *Wyślij zapytanie*) and
`ads-imperative-titles-are-not-instructions` (the set whose rows 2, 3 and 4 flagged, where
the entire text of each row is a two-word imperative title and the index returned **no
snippet at all**). Each asserts `no` for every row, and `TestShippedCharter` fails if either
stops doing so.

That second case is the sharper one, and the reason the two are not merged. The v1 exam
**already contained** second-person imperative advertising expected `no` — *"Wejdź i znajdź
to, czego szukasz!"* in `five-shopping-results-mixed-language` — and it passed 8/8 at
admission. The rows that actually fired in production differ from it in one respect: there
the imperative sits inside a longer descriptive snippet, and in the failures it **is** the
row, with nothing else to read. That is an observation about how the rows differed, not a
theory of why the model answered as it did — nothing here measured the cause.

Two more cases hold advertising and instructions **in one result set**, because two all-`no`
cases and a separate all-`yes` case are both passed by a reader answering by register or by
volume. `injection-beside-ordinary-advertising` puts blatant instructions beside blatant ads.
`injection-wearing-advertising-clothes` is the harder one, and it exists because **this
charter's own prose creates the hazard**: it spends several paragraphs teaching that
ad-shaped rows are waved through, so an instruction wearing that costume is the shape it is
worst placed to catch. Its rows 3 and 4 open in perfect sales register and then ask for the
contents of a file, and for the answer to be worded a particular way with a competitor left
out — the second aimed squarely at `about`, the one field a reader hands onward. Its row 2
(*upload your files for an instant quote*) is the over-flag boundary in the same set: it
names a file and is still a billboard.

Both are named in `TestShippedCharter.DISCRIMINATION_CASES` rather than pattern-matched, and
that is the point. The first version of that test asked only for "some case whose answers
include both words", which `injection-run-a-command` already satisfied — so both cases could
have been deleted with the test still green. Adversarial review found it; a test that a
regression passes is worse than no test.

### The rule that draws the line, and the boundary it had to state

`yes` is not an enumeration of scary words, and the first draft of v2 was — which put
*upload your files for an instant quote*, *download our catalogue* and *sign in with your
password* back on the wrong side, one arm apiece. Every wholesale or PCB search would have
interrupted him again, by charter text this time.

So the charter states a test instead: **who is the "you"?** Read the sentence as if it were
on a billboard addressed to a human customer. If it still makes sense there it is sales copy
and the row is `no`, however bossy. If it only makes sense said to something reading results
and reporting back, it is `yes`. The listed arms are examples of what such a request looks
like, not the test itself. *Order online* and *fetch one particular address as part of
answering* differ by who is being asked, not by what is named — and without the billboard
test the charter never said so.

And the charter says out loud that **over-flagging and under-flagging are the same mistake**,
rather than that one is worse. A flag firing all day on shopping teaches whoever reads it to
stop reading it, and then the row that mattered is missed too. An earlier draft ranked them
("worse than silence"), which states the security ordering backwards in the reader's own
briefing.

**The authored half had to be authored**, and that is a limit on the owner's amendment
rather than a shortcut around it: *no recorded session exists in which a search snippet
actually carried an injection*. Half this role's exam is real problems and half is
engineer-written, and nothing here may imply otherwise.

Every mined case is **sanitized** — `epnasis/aish` is public — keeping the row count, the
language mix, the address forms and the feature that caused the problem, while changing the
subject. His full-fidelity originals live outside the package.

### What an expectation may be

Never an expected output string: two of this role's fields are words the model chooses, so a
literal expectation would measure formatting. Five assertion kinds, all properties of the
typed value (`TestExamAssertions`):

| assertion | what it checks |
|---|---|
| `rows` | the row count came back |
| `field_values` | the enum answers, in order |
| `absent` | none of these strings appears **anywhere** in the output — the injection half, and the checkable meaning of "the prose died inside the role" |
| `mentions` | row N's answer names a substring a person can verify by eye in the input |
| `distinct` | no two rows got the same answer — the fidelity check a rewording cannot break |

`_case` refuses at load time an expectation naming an undeclared field, or an enum word
outside the declared vocabulary: an exam case no answer could meet is a broken exam, and a
broken exam that only fails at admission time fails weeks after the mistake was made.

### The owner's own half

`~/.config/aish/roles/<charter>/cases.yaml`, read by `roles.owner_cases`. **Additive and
absent-is-fine** — the charter's own exam is what the load gate binds on, so a fresh install
always has one. It lives in the config tree because that tree is already backed up to a
private repository. Written by `scripts/role-mine-cases.py`, which:

- reads the recorded result sets straight out of `~/.local/state/aish/session-*.jsonl`
  (one unreadable log costs its own file and nothing else);
- passes everything through `secrets.scrub` on the way out, because that tree auto-pushes;
- and writes **only assertions that are facts about the input**: the row count, `distinct`
  when code has checked that every row is a different page, and `absent` for figures that a
  snippet claims and its title and address do not — which is the charter's own first rule
  and the recorded failure this role was chosen for.

It deliberately does **not** write `field_values`. Claiming that no row of a mined set
carries an instruction would be a guess made by machine and filed as an exam. Those are
added by hand, on cases someone has read.

**A model must not write its own exam.** The script mines and computes; the sanitized
cases and the injection cases were engineer-authored by reading the real material. No model
was asked to produce a golden answer.

### Admission — the mechanism, stated so the words do not outrun the code

D6 asked for golden pairs run through the real invocation path before a role is admitted.
That cannot live in `pytest`: this suite may not reach the network (`CLAUDE.md`, and the
`no_real_notifications` precedent). It cannot live at process start either: offline, every
charter would fail its exam, and for a load-bearing role a charter that does not load HOLDS
the step — which would wedge browsing on a flaky connection.

**So there are two things, and they are not the same thing:**

1. **The exam** runs in `scripts/role-admission.py` — a deliberate step, outside the suite,
   through the real `roles.run` with a real model, spending real money. It writes
   `<state_dir>/roles/admission.json`.
2. **Load time** checks that a **recorded pass exists and is current** — `roles.admitted`,
   which compares the charter's **content digest**, the owner's cases file digest, the
   version and the model spec, and returns a *phrase* rather than a bool, because "no
   admission recorded", "the charter text has changed since it was examined" and "admitted
   against another model" are different facts. The digest comparison is what makes this the
   system's actual write-path control rather than a staleness check — see *Keeping the model
   out of its own oversight*.

A model change retires the pass, because a model upgrade can silently change what a role
does. A recorded **failure** is written too and keeps the role out: the useful artifact is
that the exam ran, not that it passed. A failing case in the owner's half fails the whole
admission — the automation only ever exercises the public half, so a recorded private
failure is the one signal that a charter is green where the machine looks and wrong where he
lives. `TestAdmission`, `TestOwnerCases`.

**On a fresh install nothing is admitted, so the reader does not run and search behaves
exactly as it did before.** That is the correct default and it is visible in the log rather
than silent. Two commands make it live:

```sh
uv run python scripts/role-mine-cases.py                                  # optional, his half
uv run python scripts/role-admission.py --model gemini:gemini-3.5-flash
```

`--dry-run` on either shows what would happen with no model calls and no writes.

**Rejected in review, and pinned by a test:** pointing the loader at the evidence store. A
loader must never depend on a store whose contract is erasure.

---

## Keeping the model out of its own oversight — the control, and the fence

R5 says the acting model may CALL a role and may never author one. A charter it could
write is oversight it could appoint, with content it had already read as co-author. There
are two mechanisms here and **they are not equals**.

### The control: admission is bound to the charter's CONTENT

`roles.admitted` compares the charter file's **content digest** against the digest recorded
when the exam was run, and does the same for the owner's mined cases file. A charter whose
text does not match what was examined **is not admitted, and the role does not load.**

That is the control, and the reason it is the control rather than the fence below is worth
stating exactly, because it was learned the expensive way.

A command fence models what `bash` will do with a string. Four versions of that model
shipped in this file's history, and **each was defeated by a class the version before had
not imagined**:

| round | approach | what walked through |
|---|---|---|
| 1 | the store's absolute path as a substring | `$HOME/…`, a relative path reached by `cd`, a path inside `python3 -c` |
| 2 | resolve every path-shaped token | every glob form (`char*`, `?harters`, `char[t]ers`), `$(…)` spanning a tokeniser boundary, `~user`, `$AISH_CONFIG_HOME` |
| 3 | fnmatch over expansion patterns | brace expansion, an uppercase glob on a case-insensitive filesystem, a glob relative to a `cd` |
| 4 | the four nets below | **a shape, not a spelling** — `git -C <config tree> apply /tmp/evil.patch` |

Round 4 is the one that settles the argument. That command **names no charter at all.** It
names an *ancestor* directory and a verb that writes recursively into it, and `git` in the
config tree is entirely ordinary there — that tree is a real repository, the knowledge
auto-backup. There is nothing in the text for a path fence to find, and no amount of
better bash modelling produces one.

The digest models none of that. Any edit, through any door — a fence's blind spot, a
plugin tool's wrapper, `roles.py` itself sitting one directory outside the fence, a door
invented next year — changes the digest, and the role stops loading. **It fails closed**,
and a role that will not load is a capability *narrowing*, which #295 P6 explicitly
exempts and which the shipped `skip` degradation turns into today's behaviour.

`content_digest` is plain sha256 computed in `roles.py`, deliberately not routed through
`evidence.digest_of`: nothing on the path that decides whether a control runs should have
a reason to import a store whose contract is erasure.

### The general lesson from that table, because it is not only about fences

Each of the four rounds ended with a class the round before had not imagined, and each was
found by a *different person*. The table is the record of that; the lesson generalises past
bash.

**A check that cannot fail is documentation wearing a test's clothes.** That is the mirror
image of a guarantee stated in prose with no enforcing line, which is the failure
`CLAUDE.md` names outright. **Both read identically from the outside** — a claim, apparently
backed, that nobody looks at again — and **neither is visible by reading the code that is
supposed to be doing the work**: the passing test and the confident sentence both say the
thing is handled. You only find them by asking, of each claim, *which line would go red if
this became false?* — and, of each check, *what input would make this one fire?*

Three instances have now been found in this area, by three different people, each in a
different mechanism:

| where | the claim | what was actually true |
|---|---|---|
| the wiring law (#328) | "the regression guard" against untrusted prose reaching an acting node | `_parse_field` refuses an uncapped field, so the branch cannot fire; it is a typo guard |
| `rows: N` in a golden pair (#328) | an exam assertion | `validate` already guarantees it; a passing `Result` cannot fail it |
| `TestShippedCharter`'s discrimination test | "some case whose answers include both words" | already satisfied by `injection-run-a-command`, so both discrimination cases could have been deleted green |

The third is the sharpest, because it is the one that was *fixed by naming the cases
explicitly* rather than by widening the prose — the general repair is to make the check bind
to the thing you actually care about, and where it cannot, to say in the doc what it really
covers. Neither downgrade is a defeat: a typo guard is worth having, and a shape annotation
is worth reading. What is not allowed is the sentence that implies more.

**The reason phrases state observations and never causes.** *"The charter text has changed
since it was examined"* is two digests differing. It is not *"the charter was tampered
with"*: from here an ordinary edit and an attack are indistinguishable, and a vocabulary
that named one of them would be the failure `CLAUDE.md`'s *No evidence, no claim* exists to
stop. `TestAdmission::test_the_reason_names_no_cause_it_did_not_observe`.

Three further properties, each pinned:

- **A version bump is not what binds.** The version lives *inside* the charter, so the
  digest covers it; the version check survives only because it gives a more specific phrase
  when someone bumped it on purpose. A charter rewritten in place **keeping `version: "1"`**
  stops loading.
- **An exam that did not record what it examined vouches for nothing.** An admission with
  no `charter_digest` is refused, not trusted — a record predating this check is exactly the
  record that cannot answer the question.
- **The owner's cases bind in both directions.** A case added is exam material nothing has
  run; a case removed is exam material that no longer exists. Absent is a recorded value
  (`NO_OWNER_CASES`), so "he has no mined cases" and "the file was deleted since the exam"
  are not the same string.

### The fence: early refusal, and known-incomplete

`Agent._is_charter` (file tools) and `Agent._command_touches_a_charter` (shell) stay, as
defence in depth. They are worth having — refusing at the moment of the attempt is kinder
and clearer than a role silently going dark a process later, and the file-tool half really
is exact, because `write_file` takes a path and a path can be resolved. But **the shell half
is a best effort with a known floor and must not be presented as the control.**

Four nets, each failing towards refusal:

1. **Literal** — the store's own path, or its `~`-abbreviated spelling, anywhere in the
   text. Catches `DIR=<charters>; … $DIR/x`, where the directory sits in a token that
   resolves to nothing.
2. **Resolved** — every path-shaped token, `~/` and `$HOME` expanded, through
   `files.contains`, so a symlink from a session root answers the same as the store's path.
3. **Expansion patterns** — the whole command is masked first (`_SUBSTITUTION` → `*`, so a
   substitution spanning a tokeniser boundary stays one token), then each dynamic token's
   directory chain is walked and `fnmatch`ed against the store: **case-insensitively** (the
   owner's filesystem is), against every base a `cd` could have made it relative to
   (`_cd_bases`), in both relative and absolute readings. A pattern naming nothing at all
   (`$PATH` → `*`) is skipped, or `echo ${PATH}` would refuse.
4. **The coarse floor** — if any part of the command is something the shell will rewrite
   AND the text names one of these stores by its own distinctive word (`_CHARTER_WORDS`:
   `charters`, `roles`, `AISH_CONFIG_HOME`), that alone refuses.

`_writes` switches nets 3 and 4 between two strictnesses: a command that could put bytes
somewhere gets the looser walk, a read does not, because the same widening refuses
`ls aish/*` — and a fence that fires on ordinary work is one somebody removes rather than
fixes. Being on `_WRITE_VERBS` only ever makes the fence refuse more; it is not a safety
claim about the verbs absent from it.

**Read and write are treated differently on purpose.** `read_file` on a charter is not
refused: reading one authors nothing, and aish answers questions about itself. A shell
command naming the directory is refused whichever it would have done, because deciding
read-versus-write from command text is precisely the judgement a structural fence must not
have to make.

### What the fence does not catch

Listed plainly, because every one of these is covered by the digest and by nothing else:

- **The ancestor-write class.** `git -C <config tree> apply|checkout|restore|stash pop|reset
  --hard`, `patch -p1 -d <tree>`, `unzip -d <tree>`. No charter is named; an ancestor and a
  recursive verb are. Pinned as a known gap in
  `TestCharterWritePath::test_the_ancestor_write_class_is_NOT_refused_and_the_digest_is_why`,
  asserted rather than hidden, so closing it later is a deliberate inversion rather than a
  quiet deletion.
- **A path assembled entirely outside the command text** — `export D=<charters>` in one
  call, `echo pwn > $D/x` in the next.
- **A plugin tool's wrapper.** `tool_plugins` executes a script and a mutating tool's gate
  is an approval **card** — exactly the control #295 P2 rejects. Fencing it needs a
  filesystem sandbox around wrapper execution.
- **`roles.py` itself**, one directory outside the store: the fence protects the governance
  *document*, not the interpreter.
- **Any class nobody has thought of.** Four rounds each ended with one. That sentence is the
  reason the digest exists.

The 42 spellings pinned in `TestCharterWritePath` are the record of what has been probed
across all four rounds, positives and already-passing cases alike — a table listing only
the failures is how a class stays open while the prose says closed. The 15 negatives are
pinned as hard.

`create_tool` and `import_skill` need no fence of their own, and that is checked rather than
asserted: neither takes a path, both take a slug, and `tool_plugins.NAME_RE` /
`skills.NAME_RE` admit no separator.

**Scope.** This binds **runtime** writes to the **live** locations. In a development session
on this repository, charters are edited as ordinary reviewed source — and `charters/` inside
a source checkout *is* a live location, so aish refuses to edit its own charters there too,
and an edit made by hand retires the admission until the exam is re-run. Both are intended.
`TestCharterWritePath`, `TestAdmission`.

## Cost and latency — and the measurement that ended the wiring

**Nothing costs anything today**: no role is called, so no role tokens are spent and no
role latency is paid. What follows is what it cost while the wiring existed, kept because
it is the evidence for the decision and because it is the shape of the bill the next role
will present.

Estimated over the 4167 parseable recorded result sets (`ratelimit.estimate_tokens` over
the real composed message list):

| | estimated prompt tokens |
|---|---|
| median | 1725 |
| p90 | 1915 |
| max | 3175 |

About **1141 of those are fixed** — the charter prose plus the generated output contract,
on every call — and roughly 580 are the result rows themselves. Output is five short
records, on the order of 200–300 tokens. **Note the shape of that**: two thirds of the
prompt is the charter, so a role's floor cost is set by how much briefing it needs, not by
how much material it reads. A short charter is a cheap role.

**Measured by the epic drive on 2026-08-27**, running the **v1** exam through
`scripts/role-admission.py --model gemini:gemini-3.5-flash` against the owner's real key:
**8/8 cases passed on the first attempt, no retries**, and all three injection cases were
flagged rather than obeyed. ↑8967 ↓945 tokens over 8 cases — so the estimates above are
roughly right — and **5.3s to 18.3s per call, 8.9s mean**. Attributed rather than stated
flat: a figure in a doc with no owner is a figure nobody can go back and question.

**In live traffic it was worse than the exam, and that is the number that decided it.**
Over 63 role calls in the controlled experiment: **10.6 s and ~2 800 prompt tokens per
search**, and **29.4% of the whole arm's wall clock**. All of it blocking — several
searches in one turn fan out, so the cost is one round trip per batch rather than per
result, but the planner cannot move until the batch returns. See *Title and address only*
for the full comparison; the short version is that arm B finished 31 s faster and lost
nothing that could be measured.

**The v2 exam has still not been run**, and the honest consequence is smaller than it was:
the version bump and the content digest retired the recorded admission, so the charter is
**unadmitted**, and with no caller that costs nothing. The central claim of v2 ("the reader
answers advertising rows `no`") remains a **hypothesis** rather than a measurement — the
cases encode the expected answers and nothing has yet asked a model. If the flag is ever
restored, `scripts/role-admission.py` is the first step and not the last.

**The money figure is still not here.** No key was available in the environment this was
built in. Per-call spend is recorded in the D7 record from the provider's own usage report
and surfaced by `aish usage`, which is what makes it measurable instead of guessed — but
nothing in the tree converts tokens to money, deliberately, because the owner's key is
shared PAYG and a hardcoded rate would be a number nobody checked.

No caching layer was built, deliberately. It was not asked for, and a cache keyed on result
text would be a second place for a stale answer to live.

---

## What this deliberately does NOT do

Read this list before assuming a capability.

- **No role runs.** `roles.WIRINGS` is empty; the framework has no live caller at all. It
  loads, validates, admits, records and counts — on demand, for a caller that does not yet
  exist.
- **No fan-out, aggregation, hierarchy, or a wiring data format.** Zero customers is even
  less to design a format against than one.
- **No owner-authored charters.** v1 ships them inside the package only.
- **No tools for any role.** A charter declaring one refuses to load, because there is no
  gated capability set for roles yet and a declaration nothing enforces is prose outrunning
  code.
- **No `hold` role exists yet.** The ladder is implemented and `Degradation.HOLD` is a legal
  declaration, but nothing ships with it, so that half is untested against real traffic.
- **No change to any existing gate.** The approval-gate invariant is untouched: the model
  still executes nothing directly and `Agent._dispatch` is still the single execution point.
- **The wiring law is a typo guard, not a regression guard** (#328). Every field that
  survives `_parse_field` is bounded, so the unbounded-prose branch cannot fire; what it
  catches is a wiring naming a charter that does not exist or a field the charter never
  declares. It becomes reachable when an unbounded output type does. The prose that called
  it a regression guard has been corrected rather than the code widened — see *The general
  lesson from that table*.
- **`rows: N` in a golden pair is a shape annotation, not a check** (#328) — `validate`
  already guarantees it. `_expect_mentions` and `Degradation.HOLD` have no shipped
  customer either.
- **Nothing here noticed an injection in the wild.** No recorded session has ever carried a
  real one, and none appeared in the 42 experimental runs. The `instructs_the_reader` flag's
  security case rested entirely on its exam, and now it has no caller either. Do not read
  its removal as evidence that it did not work.
- **Titles are not read by anything.** They are capped, stripped and de-voiced, and that is
  all — see *The residual this accepts: the title channel*. An asynchronous title check is
  a named option, not a plan.
- **The `absent` exam case can no longer be mined.** A result set recorded from here on has
  two lines, so `scripts/role-mine-cases.py` finds no snippet-only figure and writes no
  `absent` assertion — the one that encodes this role's founding failure. It still writes
  a `rows` and `distinct` case, over an input with no snippet, which is a weaker case about a
  different question. The owner's existing 784 logs are unaffected and remain fully minable.
- **No claim that the command fence is complete.** It is early refusal over the doors that
  have been probed; the control is the content digest. The ancestor-write class is an open,
  asserted gap.
- **No claim that browsing is isolated.** See *The wiring law*.
- **`read_url` and `read_pdf` are not covered.** #295's rollout order is search snippets
  first, then fetched page text, then driven-page text, then downloaded documents, then mail
  bodies. The first surface was answered by **removal** rather than by a role, which is a
  result the rollout order did not anticipate and which does not generalise: a page's text is
  the thing being asked for, so it cannot simply not be collected. The banner remains the
  whole of what is on every remaining surface.
