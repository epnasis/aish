# Roles — cheaply appointed, isolated, narrow-duty helpers

`roles.py`, `aish/charters/`, `scripts/role-admission.py`, `scripts/role-mine-cases.py`,
and the one wiring in `agent.py` (`Agent._searched` → `Agent._read_snippets`). Issue #297,
stage 1 of the epic in #295.

**How to use this file.** The laws first, because everything else is a consequence of
them. Then the charter, invocation, validation, the wiring law, the exam and how
admission actually runs, the fence, cost, and — last and most important — the list of
things this does **not** do, which is where an overclaim would live if there is one.

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
skip: the worst case has to be the status quo.

**R4 · Every closed vocabulary must be able to say "I cannot tell."** `UNSURE_VALUES`, and
a charter whose enum lacks one **does not load**. A vocabulary that structurally forces a
verdict makes guessing the cheapest answer, which is exactly how a hypothesis about a
sign-in became aish's own voice for weeks (`CLAUDE.md`, *No evidence, no claim*).

**R5 · The acting model may CALL a role and may never author one.** A charter it could
write is oversight it could appoint, with content it had already read as co-author.
Refused by identity, on the file-tool path and the shell path both — never carded. See
*The fence*.

**R6 · A charter without an exam does not load**, and admission is a recorded pass rather
than a live check. See *The exam* for why the second half of that sentence is not a
downgrade of the first.

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

Wirings ship as **code**, not a data format. A node/edge format designed against a single
wiring would be designed entirely against hypotheticals; the LAW ships anyway, because it
is the regression guard and the point is that it exists *before* the wirings that need it.
With one node the check is nearly free.

**Stated honestly, because this is where the epic already narrowed once.** "Prose dies
inside the role" is fully true for a reader returning prices and dates. It is **not**
unconditionally true here: the acting model cannot choose which link to open without
titles and addresses, and those are attacker-written strings. What is enforceable is
**bounded, capped, stripped fields plus the law** — a real quantitative gain, not a proof
of isolation. See *What crosses* below for the exact accounting.

---

## The snippet reader — the one role v1 ships

`aish/charters/snippet-reader.md`. Charter version 1, kind `reader`, degradation `skip`,
one untrusted input. `TestShippedCharter` loads the real file, runs the wiring law over it,
and pins that its exam actually carries both halves described below — so a charter that
stopped shipping injection cases fails here rather than at the next incident.

**The hole it fills.** `web_search` results are attacker-writable titles and snippets from
arbitrary sites, plus an index anyone can push on with ordinary SEO. Until now they entered
the acting model's context as prose behind `web.SEARCH_RESULTS_NOTE` — a banner, which is
an instruction to the model and not a structural control. The banner's own comment in
`web.py` says so.

### What it is given

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

### What it returns, and what crosses

Three fields: `n` (row), `about` (text, ≤160, may be empty), `addressed_to_me`
(`no` | `yes` | `unclear`).

**The title and the address are not among them.** `agent._read_results` copies them from
the parsed input **by row number**, capped at `RESULT_TITLE_CHARS` and stripped by code —
so they are never laundered through the model at all. The address is deliberately
**uncapped**: a truncated URL is a URL that cannot be opened, which would break the one
thing the acting model needs these rows for.

So the accounting, measured over the same 4167 parseable result sets:

- **What stops crossing:** the snippet prose. Median **928** characters per result set,
  p90 1257, of text written by whoever wanted to rank.
- **What replaces it:** at most 160 characters per row, written by a context with no tools
  and no knowledge of the task — plus the same titles and addresses as before.
- **What is unchanged:** titles and addresses. Attacker-written, still there, now capped,
  stripped, and with `[aish: …]` broken to `(aish: …)` so a title cannot arrive wearing
  aish's own framing voice (`_not_aishs_voice`).

That is the honest claim. It is not "the acting model no longer sees attacker text"; it is
"the paragraph of attacker text is gone and what remains is short, capped, and structurally
required."

> **And it is conditional. The reader is OFF unless it was admitted.** On a fresh install
> nothing is admitted, so nothing above happens. It is also off on `claude-max` (no
> stateless seam), off on a local model (the charter declares `cloud-fast`), off whenever
> the key fails or the provider is down, and off when two answers in a row fail validation.
> In every one of those cases the snippet prose reaches the acting model exactly as it did
> before this shipped, behind exactly the old banner. That is the correct degradation —
> #295 P3 requires the worst case to be the status quo — but it means this is an
> improvement that is *sometimes* in force, not a property of the system. The record says
> which, on every single search; the counters are what make a chronic run of skips visible.

Two defences on the copied half, both cheap and both found by review rather than by design.
A title may not arrive wearing aish's own voice: `_not_aishs_voice` rewrites `[aish:` to
`(aish:` in a copied title or address, keeping the words so the model can still see what the
title said while breaking the marker. And `web._flat` guarantees a rendered row is exactly
three lines, so a newline inside a snippet cannot fabricate a numbered result the index never
returned — both index paths already join their fields into one line, so this enforces the
property at the one function that renders a row rather than trusting two callers upstream.
`TestWhatACopiedTitleMaySay`, `TestARowIsAlwaysThreeLines`.

`agent.READ_RESULTS_NOTE` says exactly this to the model, including that the titles are the
index's own words. `agent.READ_RESULTS_FLAGGED` and `agent.READ_RESULTS_UNCLEAR` report the
flag — as an **observation** ("contained text addressed to whoever is reading it"), never a
diagnosis of what it was for or who wrote it. The old banner promised to tell the user when
a result carried an instruction; that promise survives the change, in the flag rather than
in the model's obedience. `TestTheSnippetReaderInPlace`.

### Where it sits

`Agent._read_only_call`'s `web_search` branch, via `Agent._searched`. That branch is the
**one seam both read paths share** — the sequential `_dispatch` route and the parallel
fan-out both build their thunk from it — so the fan-out cannot bypass the reader.

### Degradation: `skip`, and why not `hold`

Advisory. When the role cannot answer, the acting model gets exactly today's text behind
exactly today's banner. Three reasons, and the first is the binding one:

1. **R3.** The reader restricts what reaches the acting model. Skipping it is the status
   quo, which is what P3 requires the worst case to be. Nothing is permitted that was not
   permitted before.
2. A `hold` would wedge **every search** the moment a key expired, a connection dropped, or
   the session ran on claude-max — a new failure invented by the defence.
3. It is a reader, not a judge. There is no sensitive step here to hold.

**The skip is never silent.** Every outcome writes the record, so *unexamined* is a fact in
the log rather than the absence of one (`docs/trace-contract.md` corollary 2), and the
counters are what make a chronic run of them visible. A result set with **no rows** — an
error string, a no-results note — asks no role at all and writes nothing, which is what
keeps "skipped" meaning something. `TestDegradation`.

---

## The record — `kind: "role"`

Renderless (in `session.RENDERLESS_STEPS`), emitted through `Agent._emit_record`, so it
reaches no renderer and is skipped on replay. Both halves, because either alone is the
empty-live-card bug that registry exists to prevent.

```json
{"kind": "role", "turn": 4, "call": 2,
 "charter": "snippet-reader", "version": "1", "role_kind": "reader",
 "status": "ok", "model": "gemini:gemini-3.5-flash",
 "attempts": 1, "ms": 712, "degradation": "skip",
 "input": {"name": "results", "trust": "untrusted", "chars": 1623,
           "digest": "9f2c…"},
 "usage": {"input": 1740, "output": 210},
 "output": [{"n": 1, "about": "a shop listing for a monitor", "addressed_to_me": "no"}],
 "flags": {"addressed_to_me": {"no": 4, "yes": 1}}}
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

---

## Counters, and why a reader has them

`roles.scan_counters` is a pure pass over decoded log records, in the shape `usage.py` and
`curate.scan_ledger` already use: no model call, no live state.

**A reader is not a judge, so this needed deciding rather than defaulting.** `about` is
extraction and needs no counter — it is measured by the exam, not by a rate. But
`addressed_to_me` **is a scored answer against a vocabulary**, and #295 P4 makes counters
the admission price for exactly that. Its fire rate is also the only way to notice the two
failures that matter: a reader that never flags anything (the defence quietly thinned) and
one that flags everything (the acting model learns to ignore the note). So they ship, and
`roles.tally_flags` counts them per call at write time — a counter derived later from prose
would be derived from the thing this framework exists to stop forwarding.

Surfaced in `aish usage`, beside the acting model's figures and never folded into them:
a role's tokens are real money on the same key, but folding them in would silently move
every per-model number that existed before roles did. `TestCounters`, `TestUsageAttribution`.

---

## The exam, and how admission actually runs

### What is in it, and what is not

Eight cases ship in the charter. **The split matters and the charter says it out loud:**

- **Mined** (five): shapes taken from the owner's real recorded searches — a five-row
  Polish shopping search, a fare quoted inside a snippet with a stale qualifier, a
  thousand-character advertisement redirect, a row whose snippet came back empty, a
  single-row set. These test **extraction fidelity**.
- **Authored** (three, all named `injection-*`): a snippet demanding a command be run and
  its output searched, an override attempt pushing a link, and a snippet impersonating
  aish's own `[aish: …]` framing. These test **injection resistance**.

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
   which compares the charter version and the model spec and returns a *phrase* rather than
   a bool, because "no admission recorded" and "admitted against another model" are
   different facts.

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

## The fence — closing the model's write path to its own oversight

`Agent._charter_dirs` names both live locations: the package's `charters/`, and
`<config home>/roles/`. The second is there because it sits inside the tree `create_skill`
and `remember` already write to — and exam cases are not charters, but a model that can
write the exam can make a role pass one it should fail, which is R5 broken one level down
instead of head-on.

**Read this section's title carefully. Two doors are closed to the classes listed below;
neither is closed to everything, and the residuals are named at the end.**

- **The file-tool path — this one is exact.** `Agent._is_charter`, asked through
  `files.contains` (the one containment function) so a symlink from a session root answers
  the same as the store's own path — and asked at all, rather than left to the workspace
  boundary, because a session rooted at the aish checkout would put the package's charters
  back inside it. `write_file` and `edit_file` take a path, and a path is a thing that can
  be resolved. `CHARTER_NOT_WRITABLE`.
- **The shell path — this one is a best effort with a known floor.**
  `Agent._command_touches_a_charter`, checked **before** `self.approve` so the approver is
  never offered the command. Without it, a shell write to a charter falls through to an
  ordinary out-of-root approval card, which is the card D2's own argument declares fatal.

### Why the shell half cannot be exact, and what it does instead

A shell command is not a path. `bash` expands globs, braces, variables, `~user` and command
substitution at exec time, and none of that exists yet when this code has to decide. Three
rounds of reasoning about that produced three wrong fences here, each corrected only by
**probing** it:

| round | approach | what walked through |
|---|---|---|
| 1 | the store's absolute path as a substring | `$HOME/…`, a relative path reached by `cd`, a path inside `python3 -c` |
| 2 | resolve every path-shaped token | every glob form (`char*`, `?harters`, `char[t]ers`), `$(…)` spanning a tokeniser boundary, `~user`, `$AISH_CONFIG_HOME` |
| 3 | fnmatch over expansion patterns | brace expansion, an uppercase glob on a case-insensitive filesystem, a glob relative to a `cd` |

So the shipped version is **four nets, each of which fails towards refusal**, and the
generalisation is written down rather than the three fixes:

1. **Literal** — the store's own path, or its `~`-abbreviated spelling, anywhere in the
   text. Catches `DIR=<charters>; … $DIR/x`, where the directory sits in a token that
   resolves to nothing.
2. **Resolved** — every path-shaped token, `~/` and `$HOME` expanded, through
   `files.contains`.
3. **Expansion patterns** — the whole command is masked first (`_SUBSTITUTION` → `*`, so a
   substitution spanning a tokeniser boundary stays one token), then each dynamic token's
   directory chain is walked and `fnmatch`ed against the store, **case-insensitively**
   (the owner's filesystem is), against every base a `cd` could have made it relative to
   (`_cd_bases`), in both relative and absolute readings. A pattern naming nothing at all
   (`$PATH` → `*`) is skipped, or `echo ${PATH}` would refuse.
4. **The coarse floor** — if any part of the command is something the shell will rewrite
   AND the text names one of these stores by its own distinctive word (`_CHARTER_WORDS`:
   `charters`, `roles`, `AISH_CONFIG_HOME`), that alone refuses. This net exists because
   nets 1–3 are reasoning about shell expansion and that reasoning has been wrong three
   times.

`_writes` switches nets 3 and 4 between two strictnesses. A command that could put bytes
somewhere gets the looser walk, which also matches a prefix ending in a bare wildcard; a
read does not, because the same widening refuses `ls aish/*`, and a fence that fires on
ordinary work is one somebody removes rather than fixes. Being on `_WRITE_VERBS` only ever
makes the fence refuse more; it is not a safety claim about the verbs absent from it.

**The asymmetry between read and write is deliberate.** `read_file` on a charter is *not*
refused: reading one authors nothing, and aish answers questions about itself. A shell
command naming the directory is refused whichever it would have done, because deciding
read-versus-write from command text is precisely the judgement a structural fence must not
have to make.

### The residuals, named

- **A path assembled entirely outside the command text.** `export D=<charters>` in one call,
  `echo pwn > $D/x` in the next: net 1 sees no literal, net 3 masks `$D` to a pattern naming
  nothing, and net 4 sees no distinctive word. Every command in a session is separate here,
  so nothing carries the value forward — but nothing rules it out either.
- **A plugin tool's wrapper.** `tool_plugins` executes a script, and a mutating tool's gate
  is an approval **card**. A wrapper that writes a charter is therefore behind exactly the
  control D2 rejects. Not fixed: fencing it needs a filesystem sandbox around wrapper
  execution, which is its own piece of work.
- **`aish` itself is one directory away.** `<pkg>/charters` is fenced; `<pkg>/roles.py` — the
  code that reads charters — is not. The fence protects the governance *document*, not the
  interpreter. A model editing aish's own source is an approval card like any other write.
- **The exactness is per-class, not general.** The 33 spellings pinned in
  `TestCharterWritePath` are the ones known to have leaked. A class nobody has thought of
  is refused only if net 4 catches it.

`create_tool` and `import_skill` need no fence of their own, and that is checked rather than
asserted: neither takes a path, both take a slug, and `tool_plugins.NAME_RE` /
`skills.NAME_RE` admit no separator. A test pins it, because "it cannot get there" is
exactly the claim that rots.

**Scope.** This binds **runtime** writes to the **live** locations. In a development session
on this repository, charters are edited as ordinary reviewed source — and `charters/` inside
a source checkout *is* a live location, so aish refuses to edit its own charters there too.
That is intended. `TestCharterWritePath`.

## Cost and latency — real, and measured where it could be

A role call fires on **every** search, in the cloud, on a key the owner shares and pays for.
Measured over the 4167 parseable recorded result sets (`ratelimit.estimate_tokens` over the
real composed message list):

| | estimated prompt tokens |
|---|---|
| median | 1725 |
| p90 | 1915 |
| max | 3175 |

About **1141 of those are fixed** — the charter prose plus the generated output contract,
on every call — and roughly 580 are the result rows themselves. Output is five short
records, on the order of 200–300 tokens.

**The money figure is still not here.** No key was available in the environment this was
built in; the admission run above was executed separately. Per-call spend is recorded in the
D7 record from the provider's own usage report and surfaced by `aish usage`, which is what
makes this measurable instead of guessed — but nothing in the tree converts tokens to money,
deliberately, because the owner's key is shared PAYG and a hardcoded rate would be a number
nobody checked.

**Measured against the real backend on 2026-08-27**, running the shipped exam through
`scripts/role-admission.py --model gemini:gemini-3.5-flash`: **8/8 cases passed, one attempt
each, no retries**, and all three injection cases were flagged rather than obeyed.
↑8967 ↓945 tokens over 8 cases — so the estimates above are roughly right — and
**~9s per call** (5.3s to 18.3s). That latency is the headline number, not the tokens.

**Latency is a real change in how browsing feels, not an edge case.** A search now waits for
a second model call before its results reach the planner, and the reader cannot be
parallelised away: the planner cannot move until it returns. Several searches in one turn
still fan out, so the cost is one reader round trip per batch rather than per result.

No caching layer, deliberately. It was not asked for, and a cache keyed on result text
would be a second place for a stale answer to live.

---

## What this deliberately does NOT do

Read this list before assuming a capability.

- **No fan-out, aggregation, hierarchy, or a wiring data format.** One customer is not
  enough to design a format against.
- **No owner-authored charters.** v1 ships them inside the package only.
- **No tools for any role.** A charter declaring one refuses to load, because there is no
  gated capability set for roles yet and a declaration nothing enforces is prose outrunning
  code.
- **No `hold` role exists yet.** The ladder is implemented and `Degradation.HOLD` is a legal
  declaration, but nothing ships with it, so that half is untested against real traffic.
- **No change to any existing gate.** The approval-gate invariant is untouched: the model
  still executes nothing directly and `Agent._dispatch` is still the single execution point.
- **The wiring law does not yet guard what its prose describes** (#328). Every field that
  survives `_parse_field` is bounded, so the unbounded-prose branch cannot fire; what it
  catches today is a wiring naming a charter that does not exist or a field the charter
  never declares. It becomes reachable when an unbounded output type does.
- **`rows: N` in a golden pair is a shape annotation, not a check** (#328) — `validate`
  already guarantees it. `_expect_mentions` and `Degradation.HOLD` have no shipped
  customer either.
- **No claim that browsing is isolated.** See *The wiring law*.
- **`read_url` and `read_pdf` are not covered.** #295's rollout order is search snippets
  first, then fetched page text, then driven-page text, then downloaded documents, then mail
  bodies. Only the first has converted; the banner remains the stopgap on all the rest.
