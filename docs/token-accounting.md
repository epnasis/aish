# Token accounting

What a turn actually cost, and what filled the context that made it cost that. Issue #262.

**Built so far: the recording half.** The reporting surface (`aish usage`, the per-turn
contributor section in `aish explain`) is not built yet; the decisions that constrain it
are below so it is not designed twice.

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
baking chars/3 into records. This is also why the `model_error` record measures
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
3. **The log is not the billing truth.** `rewind_last_turn` (web Retry) rewrites the log in
   place and `redact_turn` deletes records; those calls were billed but their records are
   gone. The report measures **recorded** usage and must say so, or the first
   reconciliation against a provider console discredits it.

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
