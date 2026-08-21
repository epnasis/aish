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

`MODEL_CALL_ATTEMPTS` is three, not the old two, because the attempts are now spaced and
classified: a permanent failure spends one attempt instead of two, so a transient one can
afford three.

## What this does not fix

Throttling is symptom management; ~120k tokens per call is the disease. At free-tier TPM
a governor pacing calls that size allows roughly two per minute, which turns the
incident's 156-call task into a multi-hour one that **still** spends 12.7M tokens and 156
requests and exhausts the daily quota anyway. The remaining work — a process-wide
governor at the backend adapter seam (the only chokepoint every consumer of an API key
traverses, including `server._model_session_title`, which calls `agent.chat` directly and
swallowed its 429s), and a spend budget distinct from the context budget in
`_history_budget` — is tracked in issue #261. Token accounting and reporting is #262.
