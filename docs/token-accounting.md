# Token accounting

What a turn actually cost, and what filled the context that made it cost that. Issue #262.

**Built:** the recording half, `aish usage`, and — since #330 — the per-turn contributor
section inside `aish explain` and the trend inside `aish usage`. The decisions that
constrained them are below, unchanged; the last two sections say what was built on top and
what the records still cannot support.

## The unit problem — why this was urgent

`tokens: [prompt_eval_count, eval_count]` on the `reasoning` record looks like one number
with one meaning. It is three:

| provider | what the first number is |
|---|---|
| OpenAI-shaped (incl. Gemini's compat layer) | `prompt_tokens` — **includes** cached tokens |
| Anthropic | the adapter **summed** `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` |
| Ollama | `prompt_eval_count` — **excludes** tokens served from KV-cache reuse |

So a daily total across providers was adding incompatible quantities, and the cache split
— which decides whether a 120k-token resend was expensive or nearly free, on a loop that
resends its whole history every step — was **discarded at the adapter, where the fields
still existed**. A cache read bills at roughly a tenth of base input, so the collapsed
Anthropic figure cannot tell a 1M-token turn that cost 1M from one that cost
100k-equivalent.

That is evidence that **decays**: providers change what they report and how they bill it,
and a log written last month cannot be reinterpreted once they do. Recovering it later
would mean reading the provider's documentation *as it reads today* — re-derivation, the
failure `docs/trace-contract.md` §0 exists to stop.

`backends.usage_detail()` keeps the report verbatim and labels it with its own units
(`INPUT_INCLUDES_CACHE` / `INPUT_EXCLUDES_CACHE` / `INPUT_EXCLUDES_KV_REUSE`). The
semantics flag is not decoration — it is the only thing that makes two providers' numbers
addable. `TestUsageDetail` (`tests/test_backends.py`).

**Zero-valued counts are dropped.** A provider that does not report cache reads and a turn
that had none are different facts, and only the absent key can tell them apart. Same rule
one level up: a call that reported no usage at all records none rather than zeros.
`TestUsageOnTheReasoningRecord` (`tests/test_explain.py`).

The two-int summary **stays** beside the detail. Everything downstream sizes context from
it, a cache read still occupies the window, and a report needs both numbers to show the
residual between what was estimated and what was reported.

## The report — `aish usage`

`aish/usage.py` is that scanner: pure, per-file contained (one corrupt log must not take the
report down — the containment rule a single corrupt session log taught the web server), and
under the same reader law as `aish explain`. `--days N` reads the date out of the FILE NAME,
so a week's report skips a year of logs without opening them.

```
aish usage [--days N | --all] [--week] [--session NAME] [--json]
```

The summary reports spend and context separately and never adds them.
`TestSpendTotals` (`tests/test_usage.py`).

The drill-down answers the question the incident could not: **what filled this context**,
bucketed by the tool that produced it, with chars measured and residency computed from the
`model_call` stamp. A trim **ends** a result's residency, because naive
injection-to-end-of-session overstates precisely the results the system already handled
well. Stubs are matched by tool NAME, not by `stubbed[].at` — that indexes the live message
list, which does not map 1:1 to log order across resume, redact or rewind, and claiming
per-instance identity would be false precision. `TestContextAttribution`.

### What it refuses to pretend

A log written before the `model_call` stamp existed cannot say **when** a result entered the
context, so residency degrades to "chars × every call in the session" — a number that looks
like attribution and is arithmetic on an assumption. Those reports drop the column and say
so; the JSON omits the key rather than reporting `0`, because a consumer must not read *not
measurable* as *nothing was resident*. Same rule for a backend that reports no usage at all,
for a log a Retry or redaction rewrote, and for calls made outside a task.
`TestHonestAboutWhatItCannotSay`, `TestScanIsContained`.

## Decisions the reporting surface must not re-litigate

**A scanner, not a new record.** §0 forbids re-deriving from mutable live source; it
explicitly sanctions pure scans over recorded evidence — §7's ledger counters are
"computed by a `scan_*` pass over the logs… pure code, zero model calls", with
`curate.scan_ledger` as the named template. The facts are almost all recorded already:
every tool result *as the model received it* is a `message` record carrying `tool_name`;
`trim` records carry `stubbed[{at, tool, continuation}]` with byte counts and cap
provenance; `_system_evidence` carries per-part chars; the `context` record carries index
and preload chars; the `tool` step carries `bytes` and `truncation.kept`.

A per-call composition record was considered and rejected. In the only form that answers
the actual question — *which* `read_url` page, not just "read_url" — it re-records the
whole surviving history every call: O(N²) across a session, growing fastest in exactly the
runaway sessions it would exist to diagnose.

**Chars in the buckets, tokens only from the provider.** A per-bucket `estimated_tokens`
is a modelled number wearing evidence styling, in the same unit as the provider's actual —
inviting a reader to sum the buckets and contradict `tokens[0]`, a record that
arithmetically disagrees with itself. Buckets carry **chars** (a measured fact); the call
carries the provider's **token** report (a reported fact); the reader computes shares,
labels them estimated, and **displays the residual** ("actual exceeds char-estimate by 58%
— 3 images in context are uncounted") rather than silently normalising. `docs/diagnostics.md`
already got burned once comparing against `num_ctx`; that bug must not be rebuilt by
baking chars/3 into records.

**What got built is a RATIO, not the residual sentence this paragraph imagined.** *"actual
exceeds char-estimate by 58%"* needs a chars→tokens divisor to subtract across, and the
measured spread (2.01–4.11) means such a sentence would report divisor error as missing
content. The built surface shows `chars_per_token` for the call instead — both halves
recorded — and names the parts it could not size. Same purpose, no invented constant. This is also why the `model_error` record measures
`sent_chars` and not an estimated token count (`docs/rate-limits.md`).

**Context residency, never "cost".** Because every call resends the whole history, a 20k
result injected early and surviving K calls is resent 20k × K times. That is a real
quantity and it is what the *rate limiter* counts — the incident behind #261 proves
Gemini's free tier counts resent prefixes. It is **not** cost: aish keeps the prefix stable
precisely because it is cached, so on Anthropic that same result costs ~20k + 49
cache-read-priced resends, and the metric would punish exactly the residency caching makes
cheap while missing the real cloud-cost villain — cache invalidation, where a trim rewrite
re-bills the whole suffix at full price. So it is reported beside spend under its own name.

The honest denominator is *calls in which the bytes were actually resident*: residency
**ends at a trim**, and the `trim` records make that computable. Naive
injection-to-end-of-session overstates precisely the results the system already handled
well. And the report must carry the causality caveat: residency names the biggest
resident, not the most wasteful one. "read_url cost you 3M token-turns" invites the wrong
fix (stop reading pages) when the right one may be "trim it once consumed". Triage metric,
not blame metric.

**Spend and context growth are different numbers.** Summing prompt tokens across calls
double-counts resent history and reads as a far bigger figure than the context ever was.
Both are worth reporting; conflating them makes every report wrong in the same direction.

## Three things the first report must be honest about

1. **`claude-max` records nothing.** It drives the Claude Agent SDK's own loop and surfaces
   only `output_tokens` in a note — no `reasoning` record, no input tokens at all. A day
   of heavy claude-max use must read as **not recorded**, per the three-states discipline
   in `docs/diagnostics.md`. Reading it as `0` would be a confident lie.
2. **Model calls outside `run_task` are uncounted** — `server._model_session_title` calls
   `agent.chat` directly, and `curate`'s judges run in a separate process entirely.
3. **The log is not the billing truth.** `redact_turn` rewrites the log in place and
   deletes records; those calls were billed but their records are gone. The report
   measures **recorded** usage and must say so, or the first reconciliation against a
   provider console discredits it. **Web Retry used to be the bigger half of this bullet
   and no longer is:** `rewind_last_turn` deleted the discarded turn, and
   `supersede_last_turn` replaced it (#339) — the records are MARKED and stay, `usage.py`
   has no superseded filter, so a retried turn's calls are counted rather than lost, and
   nothing sets `session.rewritten` for them.

## Seam with the rate governor

`docs/rate-limits.md` covers the other half. They share **vocabulary and pure extraction
functions**, never a stateful accounting component: shared state would put log I/O (and
the one-corrupt-file failure mode) in front of every model call, and would hand the
reader live process state, breaking the law that `aish explain` is assembled from
recorded evidence alone. The governor needs **pre-call estimates**; the report needs
**post-call actuals**; one component blurs two numbers that must stay distinguishable.

The governor is therefore a **writer**: its records go to the session log, and the report
reads them like every other record. One-way flow, the record contract the only shared
artifact.

## What filled THIS call — `aish explain <chat> <turn>` (#330)

`aish usage --chat` answers *what filled this chat*, over a whole session, weighted by
residency. It cannot answer the question #330 actually needs, which is per **call**:
*a realistic local window is ~60,000 tokens — would this step have fitted, and what was
using the room?* `explain._context_cost` is that answer, and it is assembled from four
recorded things and nothing else:

| what | recorded where |
|---|---|
| the text of every message, and **which call it was first in front of** | `message` records: `content` + the `model_call` stamp |
| the system text's size, per part | `brief.system[].chars` |
| the tool menu's size | `brief.tools.digest` → the evidence store (the bytes are not in the log) |
| what a trim removed, and how much | `trim.stubbed[]` + `bytes_before - bytes_after` |
| what the provider billed | `reasoning.usage` / `tokens` |

**The stamp is what makes this possible, and its absence is a real answer.** Without
`model_call` on a message, membership in a call's context is positional — inferred from the
order lines happen to sit in the file — and a breakdown built on that would look like
attribution while being arithmetic on an assumption. Such a log reports nothing here.
Today that is most of the corpus: **41 of 786 logs** carry the stamp on 2026-08-27.

### The reconstruction is checked against a number the agent wrote down

This reader claims to know what was in front of each model call. That is a claim about the
agent's own message list, so it is not left as plausible arithmetic. One independently
recorded total is `sum(len(content))` over that live list at a NAMED call —
`model_error.sent_chars` — and the reconstruction has to land on it. Across the owner's
corpus it lands within **−3.85% to +0.68%** (median −0.08%, n=23), with the message COUNT
matching exactly. It is short far more often than long, and the one positive is inside the
upper-bound allowance the reader states for itself.

`trim.bytes_before` looks like a second anchor and **is not one**: it is measured before
`_expire_delivered_images` and `_trim_history_to_budget` run, and every snapshot here is
from after them, so the two are not the same quantity. A comparison that cannot come out
right under the correct hypothesis is not evidence, and reading its 55% gap as a reader
defect is exactly the confident-wrong-cause this file exists to prevent.
`TestTheContextBreakdownIsCheckedAgainstTheRealLoop` pins it against the real loop by
recording the char total the backend was handed at call time, and
`TestWhatFilledEachCall` pins the rest of the assembly — the stamp's absence, the three
states on every part, the units, and `test_the_reconstruction_matches_a_total_the_agent_measured_itself` against `sent_chars`.

It is short in ONE direction, by three named mechanisms, and the block says so.

### The log does not hold everything the model held

`sum(len(content))` over the log's `message` records is **not** `sum(len(content))` over
`self.messages`. **At least four** paths put text in front of the model without a full
message record, every one of them makes this reader UNDER-count, and the list is not known
to be closed:

| what | where | why it is not recorded |
|---|---|---|
| **a tool call's own arguments** — and provider-native `raw_blocks` | `agent._serialize` keeps only `role, content, tool_name, images, documents, interim`; `tool_calls` and `raw_blocks` are dropped | nothing decided to drop them; the record was shaped for the transcript, and these are not transcript |
| **steering** typed while a task ran | `agent._inject_pending_messages` appends straight to `self.messages` | a `message` record would replay as a turn-splitting second user bubble; the text survives on the rendered `injected` step |
| **a held proposal's answer** | `self.messages.append(entry)` on the `proposal` branch, deliberately not `_append` | a rules-held answer the owner never saw must not come back as an assistant bubble on the next page load; it is logged only if released |
| **the guidance form of an attachment** | `session.to_record_form` via `_append(record_content=…)` | the log keeps the **record form** (`![[…]]`) the owner reads; the model was handed the **guidance form**, a sentence per file saying what it may do with each |

**The first one dwarfs the other three by two orders of magnitude.** Every assistant message
that called a tool carries its arguments, and they are resent in front of every later call
of the turn. Measured from the `call` trace records, which do hold the args (capped at
`CALL_ARG_CHARS` = 8000, so this is itself a floor): **418,459 chars across the stamped
sessions, 5.8% of all message content** — and in `session-20260822-141044` alone, 344,630
chars. The other three run 99–1,966 chars. An earlier draft of this section named three
mechanisms and read as exhaustive; that was itself a confident wrong claim of the kind this
file exists to stop, and it is why the rendered footnote now says *at least four* and *not
known to be closed*.

Measured against the 23 valid anchors in the owner's corpus — a `model_error.sent_chars` at
a call the reader also has a snapshot for — the reconstruction is short by **0.0% to 2.1%**,
a constant 336 characters for twenty consecutive turns of one chat and then growing. It is
never long except by a trim it already reports.

**Steering was tried as a fix and made it worse, which is what settles the decision.**
Folding the `injected` text in moved a run of anchors from 336 chars short to 941 chars
long, and 336 + 941 is exactly the 1,277 chars of steering typed in that chat's earlier
turns. What that shows is that **most of it was not in front of those calls** — not that
none of it was: 336 of the 1,277 could have been resident and the arithmetic would look the
same. A restart rebuilds `self.messages` from the log, and the log is where steering is not;
the record cannot say whether that is what happened, nor how much survived. So
`steering_chars` is **sized and not placed**: its own figure, in no bucket and no total.

The same treatment now covers a second unplaceable text. `agent._log_held_entry` writes a
released hold's answer by calling `on_message` directly instead of going through `_append`,
so that record carries **no `model_call` stamp**. Reading the absent key as `0` stood it in
front of every call of its turn, including calls made before it existed — verbatim the
failure this reader's own docstring warns about, found in `session-20260824-210909` turn 4.
It is counted as `unstamped_chars` and placed nowhere.

> **The two ints proposed here (#331) are NOT built, and are redundant since #352.** The
> proposal was a `sent_chars` on the **`reasoning`** record — `self._total_chars()`, the
> integer `model_error` already writes — plus a second int for the serialised `tool_calls`
> + `raw_blocks`, because `_total_chars` sums `content` alone and shares the blindness
> above. Both would still have been sums over the aish-side list, blind to a demoted
> reminder's role and to what an adapter merged.
>
> The `sent` record (contract §3.12) answers the same question one level down and exactly:
> `sent.chars` is the length of the canonical request AS IT LEFT AISH — tool-call
> arguments, `raw_blocks`, steering, a held answer and attachment guidance included, every
> message in the role the provider saw — and each `messages[].chars` sizes one provider
> message. Adding the two ints beside it would put a second, smaller total on the
> neighbouring record and invite exactly the false comparison this file warns about.
> `_total_chars` is still not widened: it drives trim budgeting.

### What filled this call, read off the request (#352)

`_context_cost` now reads `sent` where a call has one and labels the result: each call
carries `basis: recorded` with `sent_chars` (the exact total) and `sent_parts` (chars by
provider role and tool, straight off the manifest — no stamp needed to place anything),
or `basis: inferred` with the reconstruction above standing alone. The section carries a
`basis` of its own — `recorded`, `inferred` or `mixed` — and `chars_per_token` is computed
over `sent_chars` where recorded, so both halves of that ratio are now measured numbers
for the whole request rather than for the part the log could see. `steering_chars` and
`unstamped_chars` stay reported, and are **unplaceable only for pre-`sent` logs**: a
recorded request holds whatever of them was in front of the call by construction, and the
renderer says so instead of repeating the "sized but not placed" sentence. The
reconstruction is kept beside the recorded total on purpose, as the check this section
used to run by hand against `model_error.sent_chars`.

Two things that reconstruction taught, both now enforced in `_apply_trim`:

- **The recorded total is the authority and caps the whole thing.** Applying `keep_chars`
  per stub without it read `delivered_images` — which replaces a short `[aish: …]` note
  with a shorter constant and leaves a picture behind — as 200-char stubbing of whole user
  messages, and threw the reconstruction out by **31%** on a real log.
- **A trim that named nothing is reported, never attributed.** `eager_stub` predates
  `stubbed[]` in 282 of 322 recorded instances. Those say how much text went and not what
  it came from, so the amount is carried as `unattributed_chars` and the breakdown is
  stated to be an upper bound by exactly that much.

### The unit rule, at the reporting surface

Parts carry **chars** — measured. The call carries the provider's **tokens** — reported.
The only bridge is `chars_per_token`: this call's own accounted chars over its own reported
input, so **both halves are recorded numbers** and it is a measurement of that call rather
than a constant applied to it. That matters, because the ratio is not a constant: measured
across the corpus it runs **2.01 to 4.11** (median 3.44, n=866 calls with both halves
recorded; 4.09 at a first call, where the English-plus-JSON-schema of the system text and
tool menu is nearly all of it). A fixed divisor would have attributed that spread to
missing content.

So there is deliberately **no per-part token figure and no `ratelimit.estimate_tokens`
import**. The estimator's output would be an estimate in the provider's own unit, inviting
a reader to sum the parts and contradict `tokens[0]`; importing the governor would also put
today's divisor in front of a log written under another one, and hand the reader a module
that reports how aish behaves NOW (§0). The shares ARE the estimate, and they say so.

**A part nothing sized is named, not zeroed.** No brief in force → *the system text* is
`not_recorded` (a turn with no system text has never happened). Menu bytes gone → `purged`.
Images in the context → their own row with `chars: 0` and the state `unreadable`, because
they are char-invisible and token-huge, so an unaccounted call shows a visible reason
rather than a silent gap. Any of those present suppresses `chars_per_token` entirely.

### What it will not say, and why

**Whether a role saved context.** #330 asks it directly, and the log cannot answer it. The
`role` record carries `input.chars` (what the role read) and its structured `output` (its
own answer), but what enters the ACTING context is the block that answer is *rendered*
into — nothing records that size, and no field joins a role record to the tool message that
carried it. The two numbers are therefore named `input_chars` and `answer_chars` and the
comparison is left unmade; a row built on `answer_chars` would put the answer's own size
where a reader would read the rendered block's.

> **Proposed, additive, not built here:** a **rendered_chars** field on the `role` record — the
> length of the text handed back into the acting turn, measured at the seam that builds it.
> One integer, and it turns "does this role return less than it consumes" from a
> hand-diff of two message records into a subtraction. The 97% figure in #330 was obtained
> by hand; the fields to reproduce it do not exist.

**What the panel draws.** `dossier()` carries `context_cost` and the terminal renderer
draws it; the web panel does not yet. Under the one-assembly rule (`docs/diagnostics.md`)
the data is already there for it — what is owed is the DOM.

## The trend lives in `aish usage`, not in `explain`

`aish usage [--window N]` (default 60,000, from #330) adds one table per period, built from
**the same call grouping the tables above it use**: calls, how many exceeded the window,
the median and p95 input, and the median **fixed floor** of the chats those calls belong
to. Bucketing sessions by their own start date instead put 745 calls under a day the row
directly above called 490 — two tables, one period label, two answers, and nothing on
screen to say they were counting different things.

It is here and not in `explain` because `explain` reads one turn out of one file and a
trend is a scan across sessions, which is what this module already is. Building it in both
would give two places to read the same fact, and they would disagree the first time one
changed. `TestTheTrendLivesInOnePlace`.

The window is a **default, not a fact about any backend** — a number invented in the
reporting code would otherwise be reported as though the log said it.

### What every call carries besides the conversation

**The method, so the numbers can be re-run.** Corpus: every `session-*.jsonl` under
`AISH_STATE_DIR`. Calls: `usage.scan_session(path).calls` — exactly what `aish usage`
counts, the provider's `usage` detail where a call recorded one and `tokens[0]` where it
did not, no subset. Turns: every turn for which `explain._context_cost` returns a peak.
Unit: each such turn's **peak call**, the one whose reported input is largest, because that
is the call that decides whether the turn would have fitted a window. Shares are of the sum
of measured chars across those peak calls.

At the first call of the first turn the history is a few dozen characters, so the reported
input is almost purely what the call carried besides the conversation:

```
system text 51,221 chars + tool menu 55,082 chars = 106,303 chars ≈ 25,990 reported tokens
```

Reproducible to ±0.1% across twelve consecutive sessions; over all 192 measurable turns the
same sum runs 101,187–112,333 (median 106,692). **That is about 43% of a 60,000-token
window, carried on every call before the task says a word.**

**But only one of those two halves is fixed, and it is the smaller word that matters.** The
tool menu — 55,082 chars, ≈13.5k tokens, the largest single item in the whole picture — is
the same on every task. The system text is **composed per task**. Splitting one real
brief's parts out of the evidence store (`session-20260827-173307`) gives:

| part | chars | fixed? |
|---|---|---|
| the prompt proper | 21,762 | yes |
| aish's own "About aish" usage context | 15,588 | yes |
| the live skills / memory index | 8,895 | grows with the corpus |
| the rules in force (part 1) | ~5,097 | **selected by the task** |
| preloaded knowledge (part 1) | ~5,068 | **selected by the task** |

So calling the whole thing a standing prompt would aim someone cutting for a 60k window at
the wrong component: a third of the "prompt" is aish's self-documentation, and ~10k chars
are what *this task* pulled in. **This reader does not make that split** — it would mean
parsing inside a recorded digest, which is re-derivation. The split above is a measurement
of the stored bytes, made once, by hand, and stated here.

Composition at the peak call, over 192 turns (43,269,501 chars of conversation in all):

| where | chars | share |
|---|---|---|
| carried in from EARLIER TURNS of the same chat | 37,479,738 | 58.7% |
| the tool menu | 10,553,736 | 16.5% |
| the system text | 9,975,579 | 15.6% |
| the turn's own messages | 5,789,763 | 9.1% |

Within the conversation: the model's own assistant messages 29.0%, `read_url` 18.7%,
`run_command` 17.2%, `browse` 8.8%, `browse_act` 7.1%, `web_search` 4.5%, `recall` 4.0%,
`read_file` 2.5%.

**That ordering is unit-dependent and the doc says so rather than picking a winner.**
Weighting by every call instead of by each turn's peak — which counts a long turn once per
resend — an independent pass measured `run_command` above `read_url` (assistant 34.0,
`run_command` 22.2, `read_url` 13.4). Neither is wrong; they answer *what filled the
fullest call* and *what got resent most*. The peak-call set is the one published because it
is what the shipped `dossier()` exposes and can therefore be re-derived from the tool.

And **51.2% of all recorded model calls already exceed 60,000 input tokens** (1,165 of
2,275, median 61,275). Restricting to the calls that recorded a provider `usage` detail
gives 58.8% (842 of 1,431, median 80,023) — a **newer, larger-context subset**, so quoting
that figure as the corpus rate biases it upward. Per day, on 2026-08-23 it was 73% with a
median call of 196.6k.
