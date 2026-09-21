# Rate limits and failed model calls

`aish/ratelimit.py`, and the retry loop in `agent._chat_turn`.

## The incident

On 2026-08-21 a Gemini free-tier key hit `429 RESOURCE_EXHAUSTED` in the middle of a
task. Measured from that session's own log (`session-20260821-191416-521841.jsonl`),
input-token spend per minute for the one chat:

| minute | model calls | prompt tokens |
|---|---|---|
| 19:42 | 14 | 1,191,128 |
| 19:45 | 12 | 1,180,166 |
| 19:46 | 12 | 1,304,684 |
| **19:55** | **16** | **1,911,558** |

156 calls, 12.7M input tokens, ~40 minutes, averaging ~120k prompt tokens **per call**
(peak 129,623). The quota did not fail; aish drove into it at full speed, because the
agent loop re-sends the whole history every step and this chat's history was full of
browser-rendered pages.

Three things were wrong, and the third is the one that made the first two hard to see.

## 1 · The retry was blind, and there were three of them

`_chat_turn` caught **every** exception identically, echoed `model call failed (…);
retrying once…`, and re-issued the identical request immediately. A busy local Ollama,
an exhausted cloud quota, a malformed request and a wrong API key were one event.

For a 429 that is worse than doing nothing: the retry re-sends ~120k tokens into the
quota that just ran out. For a 400 or a bad key it spends a request to relearn a
permanent answer.

And it was not one retry. Two more layers sat underneath, both invisible:

```
_stream fallback (x2)  x  SDK max_retries (x3)  x  _chat_turn (x2)  =  up to 12 requests
```

- **`OpenAICompatBackend._stream`** wrapped the streaming `create` in a bare
  `except Exception:` to work around servers that reject `stream_options`. The catch was
  unconditional, so a 429 raised at `create()` took that branch and re-sent in full
  before any other layer had a say. It is now narrowed by `_rejects_stream_options`,
  which tests for the **argument name in the error**, not the status: a gateway may
  reject an unknown field as 400, 404 or 422, but it always says which field.
- **The provider SDKs** retry 429/5xx themselves with their own backoff, inside one
  call. `SDK_RETRIES = 0` turns that off for both openai and anthropic. This is not only
  about the count: while the SDK was retrying, most 429s never surfaced to any aish code
  that could have classified or recorded them.

**There must be exactly one retry policy and aish must own it.** `TestRetryLayers`
(`tests/test_backends.py`) pins all three.

## 2 · Classification, and the field that matters most

`ratelimit.classify()` turns an exception into a `CallFailure`. It is structural and
provider-agnostic: every SDK aish speaks to (openai, anthropic, ollama) hangs a
`status_code` on its exceptions and a `response.headers` mapping behind it, so
duck-typing those reaches all three **without importing any of them** — which is also why
`tests/test_ratelimit.py` hand-builds its exceptions instead of importing an SDK. String
matching is the floor, not the mechanism: it catches a transport wrapper that lost the
status, and `matched` records what it matched on so a guess is never mistaken for a
status the provider actually sent. `TestClassify`.

**The retry hint has three homes and they do not always agree**: a `Retry-After` header
(seconds *or* an HTTP date), a `retry-after-ms` header, and Google's `RetryInfo` in the
error body, which is where the OpenAI-compat gateway sometimes puts it. "We waited 5s
because nobody told us anything" and "we waited 5s because it said 5s" are different
facts about the same wait, so `retry_after_source` is recorded beside the number.
`TestRetryHint`.

**Scope is the field that decides whether the task can continue at all.** A per-minute
quota can be waited out; a per-day one cannot, and both arrive as HTTP 429. The
difference is only in the quota id or the prose, so it is parsed, marked as the guess it
is, and treated conservatively: day is checked **before** minute (a Google quota id names
both windows often enough that minute-first would mislabel a daily quota as waitable —
the one error here that costs a whole task), and a hint longer than `LONG_WAIT_S` counts
as exhausted even when nothing named a day. Retrying into a daily quota burns every
remaining request of the day without ever completing a call. `TestQuotaScope`.

`UNKNOWN` is retryable **on purpose**. The old behaviour retried everything once; a
classifier that silently stopped retrying a case it failed to recognise would be a
regression wearing a refinement's clothes.

## 3 · The wait

`backoff_delay` prefers the provider's own hint whenever there was one — it is the only
number derived from the real quota window rather than a guess — capped at `MAX_WAIT_S`
so a wild hint cannot hang a task. Otherwise exponential, starting an order of magnitude
higher for a rate limit than for a transport blip. `TestBackoffDelay`.

`ratelimit.wait()` is **the one place aish sleeps between attempts**, and it polls the
agent's own cancel Event rather than calling `time.sleep`, so a Stop pressed during a
27-second rate-limit wait lands as fast as one pressed mid-stream. A stop during the wait
raises `TaskCancelled`, not `ModelUnavailable`: the user is owed the cancel path, not an
error blaming the provider for their own decision. The wait also *says why it is
waiting* while it happens — it is the longest thing a task does without producing a step.
`TestInterruptibleWait`.

Being the one sleeping place is what makes it patchable suite-wide. `conftest.py`'s
`no_real_backoff_sleep` neutralises it for the whole suite — same reasoning as the
notifier and secrets guards, a module that really sleeps needs a suite-wide guard rather
than per-test discipline. The **delay is still computed and still recorded**, so
`waited_s` and the retry-vs-give-up decision stay under test; only the sleeping is
skipped. `tests/test_ratelimit.py` captures the real function at import time and restores
it, which is what keeps the waiting itself covered.

## 4 · The record — why any of this was hard to find

The failure was `self.echo(...)`: a Bridge event that fanned out to live viewers and the
hot in-memory transcript and **was never written to the session log**. `grep -c '"echo"'`
on the log of the incident returns `0`. So the owner reading the trace afterwards found a
silent gap where a quota failure had been, `aish explain` could not see it, and a cold
reload erased even the grey bubble. That is `docs/trace-contract.md` §0 corollary 2 —
*absence must never be the evidence* — and it is why the retried-**then-recovered** path
was invisible: nothing failed loudly, so nothing was reported.

`model_error` is now a **rendered** step (`_emit_step`, in `TURN_STAMPED_STEPS`, absent
from `RENDERLESS_STEPS`), on the same terms as `trim` after #243: it is the class of
governance record that *contradicts what is in front of you*, and the screen is what the
owner is reading.

| field | why |
|---|---|
| `model_call` | The join. A dossier needs to put the failure between the `brief` (what the model was handed) and the `reasoning` (what came back) on either side of it. |
| `class`, `status`, `matched` | The verdict and what it was a function of. `matched: "status:429"` and a prose match are different kinds of knowledge. |
| `retry_after_s` + `retry_after_source` | The wait, and who said so. |
| `scope` | Whether a Retry could ever work. |
| `attempt` / `attempts` / `action` | `action` is **passed in, never re-derived from `waited_s`** — a provider may legitimately answer `Retry-After: 0`, and the last attempt of a retryable failure also waits zero. Both would record the opposite of what happened. |
| `sent_chars`, `sent_messages` | Chars, not an estimated token count: chars are a measured fact, and a token estimate here would wear the same unit as the provider's own number and invite a false comparison (#262). |
| `text` + `truncated` + `cap_source` | Capped at `MODEL_ERROR_CHARS`, saying which cap cut it (contract §8.5). |

`CallFailure.record()` builds that payload, and writes only what was actually
established: an absent `status` means nobody said, and writing `0` there would be a
claim rather than a record. `TestFailureRecord`.

A rendered kind with no renderer opens an **empty live trace card** (contract §1.2), so
the `app.js` row ships in the same change as the record: `tests/js/test_model_error_row.js`
runs the real `traceStep` against it, and `TestModelErrorReplaysLikeItRendered`
(`tests/test_session.py`) pins that the hot and cold paths agree. `TestModelErrorRecord`
(`tests/test_ratelimit.py`) covers the agent side — including the case that motivated all
of it, a failure that **recovered** and must still leave evidence.

### The bound is a duration, and it was a count for one release too long

The attempt count was three, on the reasoning that the attempts are now spaced and
classified — a permanent failure spends one instead of two, so a transient one can afford
three. That is sound about the *classification* and wrong about the *unit*.

On 2026-08-30 a Gemini 429 in `session-20260830-124807-752582.jsonl` was retried at 5s and
10s and given up on at 13:59:29 — **fifteen seconds of waiting against a per-minute quota
window.** The turn had already found its answer four tool calls earlier; `task_end: failed`
threw all of it away. An attempt count cannot express "long enough to outlast a quota",
because a quota window is measured in seconds and an attempt count is not. Whatever number
it is set to, the arithmetic that matters is invisible in it.

So `MODEL_CALL_ATTEMPT_CAP` is now a **backstop** (eight, far above any real retry, there
only for a provider answering `Retry-After: 0` forever) and the bound is
`Agent._retry_wait_budget()` — the seconds of waiting one call may spend. It is the same
number as `_wait_ceiling`, deliberately: both answer *"how long may this session sit
waiting on the provider"*, and an owner who wants a turn to hold on longer means both.
They are separate methods because they bound different phases — queueing for headroom
BEFORE a call versus backing off BETWEEN calls — so sizing them apart later needs no
excavation.

Attended (`DEFAULT_WAIT_CEILING_S`, 120s) that buys 5+10+20+40 across five attempts, which
crosses a minute. **Unattended (`UNATTENDED_WAIT_CEILING_S`, 20s) it lands back on three
attempts, and that is not a compromise**: an unattended session holds a thread from the
server's bounded worker pool, which is the entire reason its ceiling is low.

`bound` on the record names which limit ended the retry — `wait_budget`, `attempt_cap`,
`not_retryable` or, since #388, `trim_exhausted` (§7) — alongside `wait_budget_s` and
`waited_total_s`. Without it a reader sees
*"gave up on attempt 5 of 8"* and cannot tell a spent budget from a bug that stopped early;
the same provenance discipline §6 applies to the three bounds on a page. `attempts` still
carries the cap, and is now rarely what bound. `TestModelErrorRecord`.

**The measurement in §5 that this replaced is falsified, and the sentence is corrected
below.** It said every 429 in every log recovered on the first 5s retry; this one recovered
on none of three. The same session had already spent a full retry cycle at 12:52 (recovered
on attempt 3), so the key had been under pressure for an hour — which is the condition the
old measurement never contained.

## 5 · The governor

`ratelimit.Governor` paces calls so the next one does not fail the way the last one did.

**Process-global, not per chat.** aish-web runs many sessions as worker threads in one
process against ONE API key, so the quota is shared whether or not the code modelling it
is; a governor per session would be N independent programs each convinced it had the whole
budget. `TestGovernorIsShared`.

**Enforced at the backend adapter seam** (`backends.governed`), not in `_chat_turn`. That
is the only in-process chokepoint EVERY consumer of a key traverses. The agent loop is one
of them; `server._model_session_title` is another — it calls `agent.chat` directly,
outside `run_task`, after every eligible completed turn, and its
`except Exception: return None` swallowed the 429 with no record at all. Governing the
loop alone would leave a shared quota governed at one of its several consumers, which is
the shape of the bug rather than a fix for it. `curate` runs in a **separate process** on
the same key and is out of reach of any in-process design; that is an acknowledged
approximation, not an oversight.

The `ollama.chat` calling convention is preserved exactly, so nothing downstream can tell
it is wrapped. Cancel and status wiring therefore cannot ride on the arguments — a keyword
added for the governor's benefit would break the one invariant that keeps the backends
interchangeable — so it rides on a **thread-local** (`ratelimit.hooks`). A thread is the
right scope by construction: the agent's worker thread is exactly the span of one
session's calls, and a caller that sets nothing (the retitler, a test) gets bounded
default behaviour rather than blocking forever.

**Keys are `provider:model`.** Quotas are per model on the tiers this matters for, and one
process genuinely mixes models on one key.

### What it believes, and how it learns

**The shipped table is empty, on purpose.** aish cannot know which billing tier a key is
on, and throttling a paid key on a guessed free-tier number would be a self-inflicted
outage. So the default is "no ceiling known" and nothing is enforced; the owner can state
theirs (`AISH_RATE_LIMIT_GEMINI=rpm=10,tpm=250000`, per provider or per `provider:model`);
and a 429 corrects it either way. `Limits.source` (`none` | `env` | `observed`) travels
with the numbers, mirroring `context_window`'s provenance discipline. `TestGovernorLimits`.

### Only a refusal that names something may teach a number

**A 429 says the model of the world is wrong. On most providers it does not say which
part.** So `observe()` forks on `CallFailure.names_a_quota` — whether the refusal named a
window (a `per minute` / `per day` quota id) or carried a real `Retry-After`:

- **It named something.** Evidence about a *rate*. The ceiling snaps, as below.
- **It named nothing.** Evidence only that *something* was too much. aish backs off in
  time and enters a `COOLDOWN_S` spell of **spacing**, and believes nothing.

The first design learned from every 429. The reasoning was that a limit is *"published,
static, per-model, and contended only by yourself"* — true of a private tier-1 key, and
false of the shared pay-as-you-go key actually in production here, whose ceiling moves by
the hour because it has neighbours. Inferring a hard number from an anonymous refusal on
such a key does not model the quota. It models the traffic of strangers at one instant,
and then enforces it for as long as the belief survives.

**The trade is measured, not assumed.** As measured to 2026-08-21, across every session log
this owner had: **21 rate-limit 429s, all recovered on the first 5s retry, none ever needing
a second.** That is no longer unconditional — on 2026-08-30 one recovered on none of three
attempts and killed its turn (#337, §4) — so read it as *what a single 5s retry usually buys
on a quiet key*, not as a bound on what a 429 can cost. It still supports the conclusion it
is used for here, which is about the relative price of the two mistakes rather than about
any 429 always recovering. Against
that, a ceiling inferred from one of them cost **30 calls of ~55s each in a single session —
23% of all time spent in model calls** — and the number it enforced swung
987k → 511k → 1076k tokens/min inside 45 minutes, because it is a snapshot of whatever the
last minute happened to contain rather than a property of the quota. The discriminator in
the logs is output size: a 61s call returning 97 output tokens sat next to a 7s call
returning 279. It was not the model working harder.

So: **re-slamming a busy quota costs one 5s retry; throttling against a wrong guess costs
55s on every call, indefinitely, on a number nobody ever checks.** When aish does not know
which ceiling it hit, the cheap mistake is the right one. `settle()` therefore lifts a
cooldown on the first response — the only direct evidence the squeeze is over, and free,
because unlike a probe it is a call the owner was making anyway. The oscillation that
permits (clear, slam, back off, clear) is accepted on that arithmetic.

**What survives without a number.** The hazard this module was born from is a *stampede* —
many worker threads in one process discovering the same limit simultaneously, each burning
a full request to learn it. That needs no ceiling to answer, only order: during a cooldown
calls on the key are spaced `COOLDOWN_SPACING_S` apart, so they find out one at a time. The
cooldown is monotonic and deliberately **not persisted** — a transient brake that outlived
its process would be a belief wearing a cooldown's clothes.

**`sent` is the first gate, and the structural one.** aish's own `RateLimited` is a
`RATE_LIMIT` carrying `scope=LONG` and a `retry_after_s` it invented, so on the fields alone
it is indistinguishable from a provider naming a daily quota. Nothing left the machine, so
the provider said nothing, so there is nothing to learn — a governor able to learn from its
own guess would ratchet on its own output. `TestAnAnonymousRefusalTeachesNothing`.

### When a refusal does name something

The ceiling snaps to just under the rate that produced the failure, against the
**effective** ceiling rather than the stored one:
if the belief had already loosened to 1.5x and *that* is what got refused, the new evidence
is about the loosened number, and tightening against a stale stored value would ratchet
down forever without ever converging on what the server actually allows.

**The ceiling is a belief, not a law, and it decays toward optimism.** The first design
learned once and held forever, on the reasoning that a published per-tier limit is static.
A real key does not behave like that: a pay-as-you-go key on a shared organisation quota
has neighbours, so the ceiling genuinely moves between one hour and the next, and a limit
learned at 23:00 is not a fact about 09:00. Holding a once-observed number forever converts
a temporary squeeze into a permanent self-imposed one — believing your own model over the
server, which is the failure this whole file is about.

So after `RELAX_AFTER_S` without a refusal the belief loosens by `RELAX_FACTOR` per quiet
stretch, and snaps back the moment the server says no. **There is no synthetic probe**, and
that is what answers the usual objection to additive increase: the thing that tests the
loosened ceiling is the next call the owner was making anyway. Occasionally that costs a
429 — now paced, recorded and legible, rather than the invisible slam it replaced.

Relaxation is aish correcting its own guess, so it applies only to an `observed` ceiling.
An owner who **stated** their tier said something aish has no business loosening.
`TestLimitsAreABeliefNotALaw`.

The loosening is stepwise rather than continuous so the value is stable between steps — a
ceiling creeping up every second would answer two calls a second apart differently for no
reason a reader could reconstruct — and `believed()` is kept separate from `limits()` so
the persisted file and any reader see what was actually learned rather than a number that
moves with the clock.

### What survives a restart

A ceiling costs a 429 to learn, so learning it once per **restart** rather than once is
paying repeatedly for the same information — and `make ship` restarts the server often. The
learned ceilings, the **age of the evidence** behind them, and the spent-quota latch are
persisted to `rate-limits.json` in the state dir — `~/.local/state/aish/` unless
`AISH_STATE_DIR` moves it, the same tree the sessions and `aish explain` already live in.
The age matters as much as the number: without it a restart would look like a fresh refusal
and re-freeze a ceiling that had spent an hour earning its way back up.

**The location is structural, not configured (#389).** For one release `_path()` returned
`None` unless `AISH_STATE_DIR` was set, and the production launchd plist sets it for
nothing — so the server, the one process whose restarts this section exists for, was the
one process that never persisted anything: `rate-limits.json` was written only by runs
that exported the variable, and the latch below did not survive a restart either, while this
section said it did. The default now comes from `paths.state_home()`, the single place the
state tree's default is spelled (the same shape as `config_home()`, #254), so no site can
opt out of persistence again by omitting a default. `test_the_store_defaults_to_the_state_tree_when_nothing_names_it`,
`test_a_governor_given_no_store_persists_into_the_state_dir`, and in `tests/test_paths.py`
`TestStateHome` (the knob itself: unset, empty, and call-time resolution) and
`TestEveryConsumerAnswersTheSame`, which pins every consumer of the state dir to the knob
rather than to a string it happens to share with it.

The latch persisting closes the gap this doc previously listed as open. Restart recovery
re-runs interrupted triggered sessions, so an in-memory latch meant a spent daily quota was
re-slammed on every restart **by the very sessions it had already refused**.

Best-effort by design: a missing or unreadable file reads as "nothing learned yet", which is
exactly the state a first run is in, so there is nothing here worth failing a model call
over. `TestLearnedLimitsSurviveRestart`.

Two clocks, deliberately. The rolling window is **monotonic** (a clock jump must not appear
to spend or refund a minute of quota); the learned ceilings and the latch are **wall**,
because they outlive the process and "how long since the server last refused" is a question
about the world rather than about this run.

### Admission

Pessimistic: debited at the **estimate**, before the call. Optimistic accounting lets a
burst of concurrent calls overshoot together, and that overshoot *is* the 429 this exists
to prevent. The three outcomes are deliberately asymmetric — `settle(actual)` replaces the
estimate with the truth, `rejected()` keeps the request debit but drops the tokens (the
request happened and plausibly counts against RPM; tokens the provider never processed
must not crowd out the retry), and **neither** leaves the estimate standing, which is what
an abandoned stream gets: usage arrives on the final chunk, so a stream nobody finished
reports nothing and the server may well have generated the whole response.
`TestGovernorAdmission`.

The estimate is chars/3 plus a flat per-image figure, because images are **char-invisible
and token-huge** and every delivered image is re-encoded into every later request.
`TestEstimate`.

**FIFO, and that is a choice with a cost.** One greedy 120k-token reservation delays
everyone behind it. The alternative starves it forever — a stream of small calls can drain
a bucket indefinitely while a large request never finds headroom — and on one owner's
machine, head-of-line blocking is the better failure.

Three things are refused rather than queued: a **spent** quota (latched until it resets, so
later callers fail fast with a sentence instead of queueing behind a wait nobody would sit
through), a request **larger than the whole per-minute budget** (a naive "wait until it
fits" blocks forever; the caller is told the one thing that can work — make it smaller),
and a wait longer than the caller's ceiling. All three raise `RateLimited`, which carries
`sent=False`: *"the provider refused"* and *"aish declined to ask"* look identical in a
bare error string and mean opposite things about whose budget just moved.

An **unattended** session queues far less than a user's own
(`UNATTENDED_WAIT_CEILING_S`), and not out of politeness: it holds a thread from the
server's bounded worker pool, which exists so a session parked on an approval cannot
starve short user actions. A session parked on headroom would re-create that hazard inside
the pool.

The governor is process-global, so `tests/conftest.py` resets it per test
(`isolated_rate_governor`). Without that, the ceiling one test INFERS from a 429 outlives
it and silently throttles every test after — with the failure landing somewhere unrelated.

## 6 · The spend budget — the lever that actually matters

Throttling is symptom management. `HISTORY_TOKEN_CEILING` sizes history against the
**context window**: what one request may contain. What a **minute** of requests may contain
is a different constraint, and only the first was ever modelled — its own comment said
"NOT a cost control — the owner's Gemini budget is not the constraint", which the incident
falsified. On a metered backend, history size **is** the spend control, because the whole
of it is resent on every step. History sat at 130k tokens, so a 300k ceiling never fired,
while sixteen calls that size spent 1.91M tokens in a minute.

`_history_budget()` is now `min(window, HISTORY_TOKEN_CEILING, tpm / SPEND_BUDGET_CALLS_PER_MINUTE)`,
and the **provenance says which of the three bound** — "why was my page cut?" has three
different answers and the number alone tells them apart from none.

Sizing history at TPM/N buys N calls a minute rather than one enormous one, which is the
difference between a task that runs slowly and one that cannot run at all. Four is
deliberately modest: a step waiting most of a minute for headroom reads as a hang. There
is a floor (`MIN_SPEND_BUDGET_TOKENS`) because below it trimming costs more than it saves
— the model loses the thread and re-fetches what was cut, which is more calls and more
tokens than it saved.

It engages **only when a limit is known** — and since a limit is now only ever known from a
refusal that named one, an anonymous 429 can no longer move it. That is not a footnote: on
2026-08-23 at 21:13 a ceiling inferred from an anonymous refusal did not merely slow the
loop, it **halved a live conversation** from 822k chars to 383k and restored it sixteen
minutes later. A transient brake must never size what the model is allowed to remember.

So a key that never hits a *named* quota behaves exactly as it did, and the budget only
ever tightens — a generous quota is never read as permission to
exceed the model's actual window. `TestSpendBudget`.

## 7 · The one 4xx with an answer (#388)

Every non-408/409/429 4xx is `BAD_REQUEST`: *a request the provider will reject
identically forever*, so retrying it spends a request to relearn a permanent
answer. That is right for a malformed body and wrong for exactly one case — a
rejection that says the request was too **big**. The identical request is indeed
refused forever; a **smaller** one is not, and only the agent can make it smaller.

So `classify()` gains one class, `CONTEXT_OVERFLOW`, and it is still
`retryable=False`. That field means *re-issuing THIS request could help*, which
remains false; what the class adds is that the caller has an action. The split is
a class rather than a flag on `BAD_REQUEST` because the two route to opposite
behaviour, and a reader of `aish explain` has to be able to tell "this will never
work" from "this size will never work". `TestClassify`.

### Recognising it, and what a miss costs

There is **no structural test for this.** The status says a 4xx; nothing in it
says whether the complaint was about the shape of the request or its size. So
`_OVERFLOW_PHRASES` is a word list under `docs/vocabularies.md`'s rules, and its
structural half is the status class it can only ever **split**: the words never
create a failure, they choose which of two unretryable verdicts one gets. A miss
therefore lands on today's behaviour exactly — `BAD_REQUEST`, no retry, the turn
ends as it did before the list existed. `friction`, in that document's column.

**Where the entries come from, because none of it is derivable here — and the
first fact is that aish has never seen one.** Across every session log on this
machine: 166 `model_error` records, 125 `rate_limit`, 39 `server`, 2 `unknown`,
and **not one `bad_request`** — no 400 of any kind has ever been recorded. So
every entry is sourced from outside this repository. Each row below names what
established it and what KIND of source that is, because they are not equally
good: a provider's own client or server source is the strongest; an independent
client's matcher that annotates the provider is corroboration of the words, not
of the response they came from; and "reported" means nothing better than that.
Every reference was read on 2026-09-19, at the file and line given; `upstream` means the
project's `main` on GitHub that day, everything else a copy on this machine.

| entry | what established it |
|---|---|
| `prompt is too long` | Anthropic. LiteLLM's *anthropic* branch (`exception_mapping_utils.py:552`, the anthropic exception-mapping branch, in the 1.75.0 copy in this machine's uv cache) and openclaw's `packages/ai/src/utils/overflow.ts:41`, whose comment quotes the body `prompt is too long: 213462 tokens > 200000 maximum`. Also a matcher in the shipped Claude Code binary (`~/.local/share/claude/versions/2.1.278`, read with a byte search): `n=e.toLowerCase();return n.includes("prompt is too long")\|\|n.includes("input is too long for requested model")`. |
| `input is too long for requested model` | **Bedrock's** wording for Anthropic models, not the direct API's: openclaw `overflow.ts:44` annotates it `Amazon Bedrock`; LiteLLM's *bedrock* branch matches the shorter `Input is too long`. The Claude Code binary matches it in the same function as `prompt is too long` (above) — Claude Code speaks Bedrock too, so that is consistent with the attribution, not against it. An earlier draft of this table called it Anthropic's second wording; no source attributes it to the direct API. aish has no Bedrock backend. Kept because it costs one bounded retry if wrong and a turn if a `claude`-family error ever arrives through Bedrock. |
| `exceed context limit` | Anthropic direct API. openclaw `overflow.ts:43` matches ``input length and `?max_tokens`? exceed context limit: N + N > N`` — the *optional* backticks around `max_tokens` are why aish's entry stops short of that word (#321); the Claude Code binary has them, `input length and \`max_tokens\` exceed context limit`, and a 400-only regex `… exceed context limit: (\d+) \+ (\d+) > (\d+)`; LiteLLM's generic list carries `exceed context limit` verbatim. |
| `context_length_exceeded` | OpenAI's machine-readable code, from OpenAI's own sources: `openai-python` `src/openai/types/beta/session_turn_error.py:14,34` (“The request exceeds the model's context window”), and the codex client matches `error.code == Some("context_length_exceeded")` at `codex-rs/codex-api/src/sse/responses.rs:721-723`. Both read from upstream `main`; the openai-python 2.46.0 in aish's own venv and the codex 0.153.4 installed here contain the string nowhere, so this is a code OpenAI's newer sources carry, not one any client on this machine has seen. The strongest entry here all the same: a code, not prose. |
| `maximum context length` | OpenAI chat-completions prose. LiteLLM's generic list matches `this model's maximum context length is`; the full body in `TestClassify` (`… 4096 tokens. However, your messages resulted in 4239 tokens …`) is the shape that phrase is taken from, and no copy of that body was captured here. |
| `exceeds the maximum number of tokens allowed` | Gemini. LiteLLM main annotates it `# Gemini` (`exception_mapping_utils.py:96` upstream; not yet in the cached 1.75.0); QwenPaw `react_agent.py:672` pairs it with `input token count`. The full body in `TestClassify` is the test's rendering of that pairing, not a capture. |
| `model only supports up to` | Gemini via **Vertex**: redpanda `ai-sdk-go/providers/google/errors.go:86-88` matches it under the comment `Legacy Vertex wording`; QwenPaw `react_agent.py:676`. Two independent clients agree on the surface; nothing from Google does. |
| `exceeds the context length` | Ollama — the one entry with LOCAL evidence (below), and in Ollama's own source: `server/routes.go` (embed, `req.Truncate` explicitly false) and `llm/llama_server.go:2462-2463` both return 400 `the input length exceeds the context length`. |
| `longer than the context length` | Ollama's own source, `llm/llama_server.go:296-300`: 400 `the prompt is longer than the context length currently available to the model; shorten the prompt, …` when context shift is off. |
| `context window` | The deliberately broad one. OpenAI's Responses body is `Your input exceeds the context window of this model. Please adjust your input and try again.` — a fixture in codex's own tests (`responses.rs:1178`); openclaw matches `/exceeds the context window/i` for OpenAI; the Claude Code binary's own broad matcher is `e.toLowerCase().includes("context window")`. Broad on purpose: a false positive costs one trim and one retry, both bounded and recorded; a miss costs the turn. |

**Two things worth keeping from the survey.** *Not one of these strings is in the
providers' own error documentation*, checked 2026-09-19: Anthropic's errors page
(`platform.claude.com/docs/en/api/errors`) lists many 400 messages verbatim and
none for the context window, and OpenAI's error-codes page carries neither the
code nor the prose. They are known from clients and captured responses only.

And **on Ollama an over-length request is not always refused**, which is the
locally-checked half of this section. `~/.ollama/logs/server-3.log` on this
machine logs `llm embedding error: the input length exceeds the context length`
at `INFO` **135 times**, and in all 135 the next access line for that request is
`POST "/api/embed" 200`. An over-length request answered 200 never reaches
`classify` at all, so on that path there is nothing for this class to fire on.
Ollama's source says why, and says when it WOULD refuse:

- **Chat truncates silently by default.** `server/prompt.go:23-77`
  (`chatPrompt(…, truncate bool)`) removes messages from the front until the
  prompt fits and logs it at `slog.Debug` — no error, no field in the response.
  The 400 `longer than the context length` is the adjacent branch in
  `llm/llama_server.go:296-300`, taken only when context shift is off.
- **Embedding refuses only when asked to.** `server/routes.go` returns the 400
  `the input length exceeds the context length` when `req.Truncate` is
  explicitly false; with the default it truncates and answers 200, which is
  exactly what the 135 local lines show. That wording is carried as an entry on
  that evidence — it is the only over-window text any log here has ever
  produced — and it has never been seen ON a 4xx here.

So on Ollama this class will usually never fire, because there is usually
nothing to fire on. The same log holds four `400`s on `/api/chat` (2026-07-19)
with **no accompanying message**, so what they were is unknown; they are
recorded so a later reader does not have to re-derive that they explain nothing.

`claude_max.py` never enters this loop at all — the SDK owns its own, and its own
compaction.

**An independent implementation corroborates part of the list — and disagrees
with it on one entry.** LiteLLM recognises these rejections the same way, a
lowercased substring match, in `litellm_core_utils/exception_mapping_utils.py`
(`ExceptionCheckers.is_error_str_context_window_exceeded`, plus per-provider
branches). Read entry by entry against the copy in this machine's uv cache:

- **Corroborated.** `exceed context limit` is in its list verbatim, and
  `this model's maximum context length is` contains aish's `maximum context
  length`. Its *anthropic* branch matches `prompt is too long`.
- **Corroborated, but shorter there.** Its *bedrock* branch matches `Input is
  too long`, where aish carries `input is too long for requested model` and
  would miss that wording. Nothing here calls Bedrock, so the entry is left as
  it was sourced rather than widened for a provider aish does not use.
- **Not corroborated by it at all:** the two Gemini wordings,
  `context_length_exceeded`, and `context window`. Their evidence is the table
  above and nothing else.
- **The disagreement, and it is deliberate.** LiteLLM treats `string too long.
  expected a string with maximum length` as a context-window error. That is
  OpenAI's 64-character cap on the `user` field — a request that is *malformed*,
  not oversized, and one no trim can fix. aish classifies it `BAD_REQUEST`, and
  `TestClassify` pins it that way.

An earlier draft of this section described that look-alike as one of two
**exclusions** LiteLLM makes by name. It is not: it is in the inclusion list.
The divergence is aish's own choice, argued above, not corroboration.

### The trim, and why it cannot use the standing budget

By the time a call goes out, `_enforce_budget` has already fitted the history to
the budget aish believes in. An over-window rejection therefore says **that
belief is wrong**, and re-trimming to the same number would free nothing — the
retry would re-send the identical request, which is the thing this whole file
argues against. The target has to be below what is being held.

**How far below is aish's own guess, and it is labelled as one.** The provider's
message sometimes names two token counts, but those count the system prompt, the
tool schemas and the images, none of which a history trim can reach — and
comparing them against aish's char measure would dress a ratio in the provider's
unit (§4, #262). So `OVERFLOW_TRIM_FRACTION` gives back **half of what is
measurably held**, the trim runs through the **existing** oldest-first machinery
(`_trim_history_to_budget`, one extra parameter, not a second trimmer), and the
record carries `policy: overflow_oldest_first` with
`cap_source: constant:OVERFLOW_TRIM_FRACTION` — the provenance of the number that
actually governed it, never the history budget it deliberately ignored. Half,
because the retry happens once: a timid cut spends the attempt without fitting,
and an over-large one costs little, since every stub carries the continuation key
that pages the text back.

The trim is a **mid-turn** event, so it joins `mid_task_budget` in
`explain.MID_TURN_TRIM` and in `app.js`'s inspector keys. A trim that fires after
a call has been refused is not part of what that call started from, and filing it
there would put it in the dossier's *given* section, above the failure it answers.

### Once, and the order of the two records

`model_error` is written **before** the trim, so the log reads in the order the
two happened; a trim above the failure would read as preparation rather than a
response. Promising a retry before doing the work is only honest because
`_can_trim_history` has already established the next request will be smaller —
with nothing left to give back, no retry is promised and none is made.

The retry is spent **once per model call**. A second overflow ends the turn with
`bound: trim_exhausted`, the fourth value of that closed field: *aish could not
make the request any smaller* — either nothing was trimmable or the one shrink
was already spent. `not_retryable` would have been true of the class and said
nothing about what actually ended it. No wait is taken on either attempt: nothing
about the provider changes in the meantime, and the next request is a different
one rather than the same one again. `TestContextOverflowTrimsAndRetriesOnce`
covers the recovered path, the trim record, the record order, the second
overflow, the nothing-to-trim case, and the fence that a 400 which is **not** an
overflow is still never retried.

### The debit the failed call leaves behind

The retry loop is not where the governor's reservation is settled, and that is
what makes the overflow path safe to add there. Every call is debited from the
process-global window on an estimate BEFORE it is sent (§3), and the correction
happens in `backends.governed` — inside the call the loop wraps, in the
`except` that re-raises to it. So by the time `_chat_turn` sees the exception
and decides to trim, the failed attempt has already been closed the way every
non-429 failure is closed: `settle(None)`, the estimate standing, no cooldown
learned, nothing refunded — and the next attempt reserves afresh on the smaller
history. `CONTEXT_OVERFLOW` is `is_rate_limit=False` exactly as `BAD_REQUEST`
is, so the split changed nothing at that seam.
`TestContextOverflowSettlesItsReservation` pins all three: the ticket is closed,
it settles identically to a plain `bad_request`, and it is not mistaken for a
quota refusal (which would zero the tokens and spell a cooldown).

One consequence for reading the counters: the same exception is **classified
twice** — once there for the governor, once in the loop for the decision — so
`_OVERFLOW_PHRASES`'s `asked` moves by two per failed 4xx (`docs/vocabularies.md`).

**What it cannot fix.** A request over the window because of **images** is
char-invisible, so a char trim may free nothing that matters and the retry is
spent for nothing (one call, then the turn ends). `_expire_delivered_images` only
reaches pictures delivered in EARLIER tasks, and nothing here widens that.

## What this does not fix

Throttling is symptom management; ~120k tokens per call is the disease. At free-tier TPM
a governor pacing calls that size allows roughly two per minute, which turns the
incident's 156-call task into a multi-hour one that **still** spends 12.7M tokens and 156
requests and exhausts the daily quota anyway. §5 and §6 are the two halves of that answer,
and §6 is the one that changes the arithmetic.

Still open, tracked on #389 (split out of #261):

- ~~**A context-window-exceeded 400 should trim and retry**~~ — built in §7 (#388). The
  unsatisfiable reservation still takes the other path: `RateLimited` tells the caller to
  shorten the conversation and nothing shortens it for them.
- **Whether Gemini's implicit cached tokens count against TPM quota**, as opposed to
  merely costing less. `cached_tokens` IS reported through the compat layer and now
  reaches the log (`aish usage` shows it), so the first half of the question is answered:
  caching is happening. Whether the cached half is excluded from the quota still is not,
  and it decides whether cache preservation or aggressive trimming is right here. The
  incident is weak evidence they DO count: the prefix was append-only and no trim fired,
  so caching should have been hitting, and 1.2–1.9M tokens/min still tripped
  RESOURCE_EXHAUSTED.
- **`curate` runs in a separate process** on the same key, so no in-process governor
  reaches it.
- ~~**Nothing is persisted in production.**~~ — closed by #389: `_path()` falls back to the
  state tree through `paths.state_home()`, so the server persists with no plist change
  (§5, "What survives a restart").

Token accounting and reporting is #262 — `docs/token-accounting.md`.
