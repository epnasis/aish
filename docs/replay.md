# Replay — measured before/after on the owner's own model

`replay.py` (scenario, runner, report, the `aish-replay` command), `replay_driver.py` (one run, in its own process), `replay_checks.py` (verdicts over a recorded run), and the scenarios under `evals/`. Issue #441, slice 1.

**How to use this file.** Why it exists, then the laws, then each piece and the bug or review finding that shaped it. The isolation table is the part to read before trusting a number.

---

## Why

A fix used to be judged by reading one live session by eye. The owner's local model runs at temperature 1.0 with presence_penalty 1.5, so one run proves nothing: it cannot say whether a change helped, did nothing, or broke something else. A replay runs the owner's own turns, verbatim, N times per arm against an isolated aish, and reports what each recorded run shows.

```sh
aish-replay run <name>                       # baseline = main, candidate = this tree
aish-replay run <name> --candidate-overlay skills/x/SKILL.md=./new.md --runs 5
aish-replay report ~/.local/state/aish-replay/<name>/<stamp> [--full | --json] [--window TOKENS]
aish-replay check ~/.local/state/aish/<session>.jsonl --scenario <name>
```

`check` applies a scenario's checks to any existing log, read-only. That is how the owner session a fix came from becomes the first evidence that the failure is real, before anything is re-run.

## Where scenarios live — never a mined one in this repo

**A scenario mined from an owner session lives in the private config tree, `~/.config/aish/evals/<name>/` (under `AISH_CONFIG_HOME` when set), never in this repo.** The repo is public, and a mined scenario carries his own words, his family, his dates and the session it came from. The git-backed config tree is private and backed up. The repo's `evals/` holds **synthetic examples only** (`evals/example-trippy/`: invented turns, an invented party), which document the format and give the tests something to load; `TestScenarioResolution` fails unless every repo scenario's `[source] session` says it is synthetic.

`resolve_scenario`: a bare name is looked up in `<config home>/evals/<name>` first, then the repo's `evals/<name>`; anything with a slash is a path. The runner never copies `evals/` into a run's corpus — it is the harness's, not something the agent reads.

The same rule binds fixtures — and a mined log cannot be made safe by scrubbing: its hotels, URLs, dates, party size and trip shape are all his, and every scrub misses some. So the check fixtures are **generated, never mined**: `tests/fixtures/replay/make_fixture.py` drives the real server and agent (via the replay driver's own card policy) with a scripted model and stubbed command, page and image seams, on invented data only — invented Lisbon/Porto listings, ids, `booking.com/hotel/pt/` URLs and photo URLs, the example's invented party. Two variants: `good` passes every check and `bad` fails every one, each failure scripted (a wrong-party search, a failed command, a 5-id details call, a page read three times, a piped command, a link never opened, a picture nothing printed). The logs are generated inside the test session, not committed: they carry wall-clock stamps, random turn ids and temp paths, and comparing a committed copy would need a normaliser that could hide drift.

---

## The laws

**R1 · Deny by default.** Every approval card a replay raises is denied unless the scenario names it: a command prefix (and then only with no shell operator in it, `SHELL_META`) or a tool name. Write, read, import and every card kind added later are denied without a scenario being able to approve them. `TestCardPolicy`.

**R2 · One process per run, one fresh corpus per run.** `AISH_STATE_DIR` is process-global and resolved at call time; the corpus directories under `AISH_CONFIG_HOME` are bound at import. Two runs in one process would share whichever was set first, and a `remember` in run 1 would be read by run 2. `TestRunnerIsolation`.

**R3 · Raw counts, never rates, never a verdict on the change.** "failed 2 of 3", "passed 3 of 3", "not observed in 3 runs". The only strong sentence is that a target failure still occurs. A report that says "fixed" or prints a percentage over three runs states something three runs cannot carry. `TestReport`.

**R4 · No candidate numbers unless the baseline reproduced the target failure.** If the baseline never showed the failure the fix is for, a clean candidate says nothing about the fix, so the report withholds the candidate column and says why. No declared target is treated the same way. `withheld_reason`.

R4 holds for the default kind, `fix`. A `guard` scenario mirrors it (§ Guard scenarios, below).

**R5 · A check may say it could not tell.** `Verdict.passed` is `True`, `False` or `None`. A check forced to choose would guess, and the guess would read as a measurement. A check that raises, a run that did not finish, a source missing from output that was cut, a log in which no command ran or no card was raised, and an answer with links but no timestamp all report `None` (§ Checks).

### Guard scenarios — R4 mirrored (#444)

A regression guard — a scenario that pins behaviour which already works — passes on the baseline by design. Under R4 that alone withholds every candidate number, so a regression in the candidate never reached the report. A guard is blind under R4.

`kind` is a top-level `scenario.toml` key. `"fix"` is the default and keeps R4 as above. `"guard"` turns the `[checks] target` list into the guarded checks and inverts the contract:

- **The baseline must pass every guarded check, or the candidate is withheld.** "Pass" means no baseline run failed it and at least one passed it. Otherwise the header reads `candidate numbers withheld: guard not green on baseline — <check> failed in N of M baseline runs: the guard itself is broken, and a regression verdict against a broken guard is noise`. A baseline that only ever "could not tell" gets the same treatment (`passed in none of M baseline runs`), because a guard nobody saw pass is no more judgeable than one seen failing. No declared target is withheld here too (`declares no guarded check`).
- **With a green baseline, candidate numbers are always shown, and the candidate's failures are the headline.** `guard: regression — <check> failed in N of M candidate runs`, or `guard: no regression observed — <check> passed N of M`. The cells read `failed N of M (regression)` for the candidate and `(guard not green)` for the baseline. A guarded check that passes is counted as `passed N of M`, not "not observed": for a guard, the pass is what the check is there to show.

**Why both kinds withhold.** It is one law: a candidate's result on a target means something only against a baseline that shows the opposite. A fix credited against a baseline that never failed says nothing about the fix. A regression flagged against a baseline that already failed says nothing about the candidate, because its failure cannot be told from the baseline's own. `--json` withholds what the text withholds, and carries the guard's `headline` and the scenario's `kind`. Context rows, non-target checks and `check` are the same for both kinds. `TestGuardScenarios` pins the fix kind's text, byte for byte, against the report as rendered before guards existed (`tests/fixtures/replay/golden/`). Its JSON gained only the `kind` key.

---

## Isolation — what a run has of its own, and what it still shares

| | per run | how |
|---|---|---|
| config tree (rules, skills, memory, config.toml) | **own copy** | snapshot once per batch, then a fresh copy per run; `AISH_CONFIG_HOME` points at it |
| plugin tools (`tools/`) | **absent** | `CORPUS_EXCLUDED`; a scenario re-adds one by name in `[corpus] tools` |
| the owner's `allow.txt` | **not used** | each run gets an empty one; the card policy decides instead |
| `.git` of the config tree | **absent** | `CORPUS_EXCLUDED` |
| state dir (sessions, evidence, media, browser profile, sign-in replay store, rate ceilings) | **own, empty** | `AISH_STATE_DIR` = the run's `state/` |
| `egress-vouches.json` | **absent** | lives in the state dir, which starts empty: vouches grant cardless egress |
| `embeddings.json` | **own copy** of one batch snapshot | both arms start from the same cache |
| notifications | **off** | `AISH_NOTIFY=0` |
| every other `AISH_*` from the caller | **dropped** | `run_env`; `AISH_LOCAL_URL` is the one passed through |
| tool data (e.g. trippy's cache) | **own** if the scenario says so | `[env] TRIPPY_DATA_DIR = "{run}/trippy"` |
| cwd | **own, empty** | |
| **real network** | shared | `web_search`, `read_url` and live fetches by approved commands leave the machine |
| **real shell** | shared | approved commands and aish's built-in read-only set execute for real in the run's cwd, and an approved command can read, write or delete wherever its arguments point — under `$HOME` or anywhere else. Every non-`AISH_*` variable of the caller passes through, so a command with no `[env]` row of its own uses this machine's own data; `unisolated_commands` names such commands in the report header and `run` warns on stderr (a name match on `<BINARY>_*`, nothing more) |
| **model server** | shared | the same `AISH_LOCAL_URL`, so runs are sequential |
| **login Keychain** | not moved | the run's secrets index is empty, so aish looks no stored secret up — and its scrub has nothing to match |

The report header repeats the shared rows (`STILL_SHARED`) so nobody reads a number without them.

The prototype that preceded this (2026-10-01) copied the owner's vouches and his allowlist into the run, kept `tools/`, and approved a command on its prefix alone (`trippy x; anything` included). The independent design review flagged those before slice 1 was built; each row above is the answer to one of them.

---

## Runner

`run_batch` runs baseline and candidate **interleaved** — b1 c1 b2 c2 — so drift on live sites lands on both arms alike. The baseline is `git archive` of a ref (default `main`), extracted beside the batch and deleted after: no worktree, no change to the repo's git state. The candidate is this tree, plus any overlays (files that replace their counterpart in the candidate's corpus copy; scenario `[candidate.overlays]` or `--candidate-overlay`). An overlay may not leave the corpus or add a tool the scenario did not allowlist.

**A tool change gets the same A/B.** A tool under test (trippy, say) is a binary on `PATH`, neither aish code nor a corpus file, and one copy is installed machine-wide — so without help both arms run the same build and a tool change cannot be measured at all. `--candidate-path DIR` / `--baseline-path DIR` put an arm's own build first on its `PATH`, and `--candidate-env` / `--baseline-env KEY=VALUE` set variables for one arm only (`{run}` = the run dir); neither may set `AISH_*`, `PYTHONPATH` or `PATH` itself. Both are recorded in `batch.json` and each run's `manifest.json`, and the report header names them, so a batch says which build each arm ran. Two arms that differ ONLY by tool build are a valid comparison. `test_a_tool_build_reaches_the_candidate_only`.

Each run is `python -P replay_driver.py spec.json` with `PYTHONPATH` = the code under test. `-P` matters: the script's own directory is `aish/`, which holds a `secrets.py` that would shadow the standard library's. The driver records which `aish` it actually imported, and `read_run` marks a run incomplete when that is not under the arm's code root — evidence instead of an assumption. The driver comes from the candidate tree for both arms, and imports nothing from the harness at load, so it runs against a baseline that predates it; the only aish API it relies on is `create_app` and the WebSocket protocol.

Output: `~/.local/state/aish-replay/<scenario>/<stamp>/{baseline,candidate}/<n>/` — beside the owner's state tree, never inside it; `AISH_REPLAY_HOME` moves it. Each run keeps `manifest.json` (code, overlays with hashes, exit, timeout), `events.jsonl` (cards and how they were answered, each turn's `done`), `driver.log`, its `state/` and its `config/`. The batch keeps a copy of the scenario, so `report` re-reads a batch with the checks it ran with. `run` refuses when baseline and candidate would be the same commit with no overlay and no uncommitted change. `TestTheRunCommand` drives `run` end to end with only the subprocess stubbed.

## Checks

Pure functions of one session log, under the same law as `explain` (`docs/diagnostics.md`): no model call, no live rule file. `load_run` reads the log once, skips superseded records like every reader of current chat state (`docs/session-log.md` L7), and joins `call` to `tool` by `(turn, call)`.

| check | from which records |
|---|---|
| `no_unapproved_shell` | `command` records whose decision is denied or blocked |
| `no_pipes_or_redirects` | `run_command` arguments in trace `call` records, tokenised |
| `no_failed_commands` | `cmd_start`/`cmd_end` pairs; `None` when they cannot be paired by order |
| `repeated_calls` | same tool + arguments `REPEAT_REPORT_AT` (3) or more times in one task |
| `links_from_evidence` | links in each task's delivered answer (the rule engine's own extractor) against `rules.urls_acted_on` over `SessionLog.calls_that_ran`, up to the answer's time — one definition of *opened* |
| `images_from_evidence` | each `show_image` source against tool output recorded BEFORE the call; a picture pasted into an answer that no successful `show_image` showed |

The delivered answer is the task's last assistant message not marked `interim`. Scenario-local checks live in `<scenario>/checks.py` as `CHECKS = {name: function}`. `TestChecksOnGeneratedRuns` pins a pass and a fail for every check on the generated runs; `TestChecksThreeStates` and `TestTrippyScenarioChecks` pin each state.

Not built yet, and not to be implied: answer language, numbers and exchange rates backed by evidence, captions describing images the model could not see, contradictions. The last three need a judge; issue #441 slice 4 keeps those measurement-only.

## Report

`render_report`: header (arms, completions, `STILL_SHARED`), one row per check with each arm's raw counts, per-run counts (model calls, tool calls, cards raised, wall seconds), one context row per arm (below), then one evidence line per failing check per arm, target checks first. Capped at `REPORT_CAP` = 2048 characters; `--full` prints every verdict of every run and every run's context row, and `--json` prints the whole report — every run's verdicts, counts and context — withholding exactly what the text withholds. A run that did not complete keeps the failures it recorded, but its passes become "could not tell": a pass from a run that never reached the hard part is not a pass.

### Context per model call (#444, slice 0)

So that a context experiment — a smaller menu, a shorter prompt — has a number per arm. `replay_checks.read_context` reads each run's own log: one entry per `reasoning` record (one per recorded model call of the acting loop; isolated roles write `role` records and are not counted, as in `aish usage`), each joined to the latest `brief` before it — `tooluse`'s join, because a brief is written only when what the model was handed changes. The report pools each arm's **complete** runs call by call (an incomplete run made fewer calls, and folding it in would move the arm's figures for a reason nobody could see) and prints one line per arm:

```
baseline 2 runs, 6 calls · tokens median 50.0k p95 70.0k input_includes_cache · over 60,000: 2 of 6 · sys 1.2k/1.2k · menu 3.0k/3.0k · tools 26
```

Under the reader law of `docs/token-accounting.md`, and reading through the same helpers so the replay and `aish usage` / `aish tooluse` cannot disagree about one record:

- **Tokens are the provider's, read by `usage.recorded_input`.** A zero is not a count: the writer records zeros when the provider sent no usage, so such a call is *not recorded* — it is left out of the median, p95 and overflow and counted as such (`tokens not recorded on any of N calls`, `over window: not recorded`). The scripted backend of the generated fixtures reports nothing, and the test on those runs pins exactly that.
- **The unit label rides with the number.** A pool whose counts carry more than one `semantics` label gets no median and no overflow count (`not combined`); counts in `input_excludes_kv_reuse` (Ollama, which leaves out the prefix it served from its KV cache) carry a note that they and the overflow count are floors.
- **Chars are the brief's**: the system parts' `chars` summed per call, and the menu's bytes looked up by digest through `tooluse._menu_of` — `purged` and `not recorded` are counted apart, never 0. Tools is the brief's own `tools.count`, which survives a purge.
- **Chars and tokens are never added or converted.** No figure estimates tokens from chars.
- **`--window TOKENS`** (default `usage.LOCAL_WINDOW_TOKENS`, 60,000 — a default, not a fact about any backend) counts calls whose recorded tokens are strictly over it, out of the calls that recorded any.

Medians and p95 are `usage._percentile`, `aish usage`'s own. R4 still governs: with no target failure reproduced, the candidate's context row is withheld like its other numbers. `TestContextPerModelCall`.

**The cap is shared.** On `example-trippy` (nine checks) the header, check rows, counts and context rows take ~1.9k of the 2,048 characters, so the default report has no room left for evidence lines and says how many it omitted; `--full` and `--json` carry them.

## The driver against the real server

`TestDriverOverTheRealServer` runs `drive` against the real `create_app` with a scripted model and the command implementation stubbed: the approved card reaches the run path, the denied one does not, and the recorded log then fails `no_unapproved_shell`. It also pins the driver's import fence.

## Not here (later slices of #441)

The tool cassette (record tool outputs once, replay them so only the model varies) — required before before/after numbers go into commit messages. Step replay from the `sent` record. Judged checks.
