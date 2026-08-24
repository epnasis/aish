# Knowledge layer — skills, memory, retrieval, curation

`skills.py`, `embeddings.py`, `curate.py`, `skill_import.py`, `paths.py`.

**How to use this file.** The laws first — retrieval is the part of aish where a plausible-looking change silently degrades quality without failing anything, so the thresholds and floors below are calibrated numbers, not taste. Then the store, the three retrieval paths (index, pre-flight, deliberate recall), writing, and curation. The agent-side dispatch of these tools is in `docs/agent-core.md`.

---

## The laws

**L1 · Progressive disclosure.** The prompt carries a capped name+description **index**; full bodies load on demand via `read_skill`; the long tail is reached by the `recall` tool. Nothing gets a permanent seat in the context window just for existing.

**L2 · Lexical is the floor; embeddings are an upgrade, never a dependency.** Any embedding failure (ollama down, model not pulled) makes `scores()` return `None` and every caller degrades to the deterministic lexical tiers — byte-identically. `TestLexicalFloor` asserts a measurable floor without a semantic layer at all.

**L3 · Selection must be able to abstain.** A judged audit of 481 live injections measured **46% pure noise**, because a single 0.24 floor sat below the corpus's own noise floor: the median task cleared it with 7–8 entries, so the four slots always filled. Retrieval that cannot say "nothing relevant" is not retrieval.

**L4 · Retire, don't delete.** `status: disabled` and `expires:` remove an entry from every path without removing the file, which is why curation never needs a delete verb — and why deletion has no code path there at all.

**L5 · The action envelope is code, not prompt obedience.** Where a model decides, the set of things it is *able* to do is enforced by the parser and the writer, not by instructions. A policy that exists only as prompt text is not a policy.

**L6 · Imported knowledge is untrusted.** An imported skill's instructions and scripts are what the model would go on to follow, so the review is one consolidated human decision with deterministic risk flags — enforced in code, never by asking the model to check itself.

---

## The store — `skills.py`

**Skills** are playbooks in `~/.config/aish/skills/`; **memory** is one fact per file, same markdown + frontmatter format, in `~/.config/aish/memory/`. Legacy `lessons.md` lines appear as synthetic memory entries until `/learn lessons` migrates them. `TestParse`, `TestListAndLoad`, `TestLoadEntriesAndCache`.

**Project scope is DISABLED by default** (#178 P0-1 interim): `./.aish/skills` and `./.aish/memory` are not discovered unless `skills.INCLUDE_PROJECT_DIRS` is set, which neither entry point does — a repository you clone must not be able to hand the model instructions. `TestProjectScopeDisabled` (skills) and `TestProjectScopeDisabledAgent` (end to end) both pin it, and no fixture flips the switch.

**A skill is either** a flat `<name>.md` **or** an agentskills.io-compatible folder `<name>/SKILL.md` bundling `scripts/`, `references/`, `assets/` — the name defaults to the directory, frontmatter still wins, and `_dir_entries` scans subdirs for `SKILL.md` only for `kind == "skill"`, so memory stays flat. `read_skill`/`load_skill` append a `_bundled_note` naming the folder's files so the model reads and runs them through the normal gated tools. `TestFolderSkills`.

**The whole tree moves on one knob: `AISH_CONFIG_HOME`** (`paths.config_home`, #254). Rules, skills, memory and plugin tools are four directories under `~/.config/aish/`, and they are one thing — the owner's own corpus, read at the top of every task and WRITTEN by `remember`, `create_skill` and `import_skill`. The verify harness gave itself its own state dir, allowlist, config file and cwd, and was therefore believed isolated while it read the owner's 24 live rules and pointed every knowledge write at his real store; a scripted step that reached an approval card was one click from a permanent, hand-editable artefact. One variable rather than one per directory, because four is four chances to isolate three of them and share the fourth, and the fourth is the one that writes. Resolved at IMPORT, like `AISH_STATE_DIR` at its call sites — set it before importing aish, or rebind the constants, which is what the suite's autouse `isolated_global_dirs` does. `test_suite_never_reaches_the_real_knowledge_store` pins the readers, not the constants, and proves nothing is stubbed by writing a memory through the real `save_memory` and checking where it landed.

This is the substrate for the **skills-primary, tools-as-scalpel** model (epic #141): a tool is added later only for hot, shell-fragile, reliability-critical operations a documented skill snippet cannot do safely.

---

## The index (L1)

`knowledge_index()` renders a capped, byte-stable name+description index that `agent.compose_system_content()` rebuilds into `messages[0]` at **every** `run_task`. That live rescan is what makes a new skill appear without a restart — **do not reintroduce boot-time caching** (`TestSkillsFreshness`, `TestKnowledgeIndex`, `TestMemoryIndexSection`).

Caps are **per type** (`INDEX_SKILLS_MAX`, `INDEX_MEMORY_MAX`), so one entry type growing large can never evict the others from the prompt.

**Standing rules (#178 P1-7).** `pinned: yes` (or `kind: policy`) puts a memory in its own "Standing rules" section under its OWN budget (`INDEX_PINNED_MAX`), exempt from the mtime-recency cap and sorted by NAME — a behaviour rule must not stop applying because newer facts rotated past it, and *which* rules render must never depend on whose mtime moved last. `remember` sets it; `save_memory(pinned=None)` preserves it on update. `TestPinnedTier`.

A per-task `TASK_REMINDER` system message is re-inserted before each user message — directly, never via `_append`, so it stays out of logs and transcripts. Recency is what makes small local models actually consult skills.

**The index is an INPUT to the model, and inputs get recorded too (#208).** `knowledge_index(on_index=…)` reports what the composition selected and what it capped out; `run_task` turns that into the `context` record (`docs/trace-contract.md` §3.10). A callback rather than a changed return type, for the same reason as `save_memory(on_admission=…)`: the string return is what `compose_system_content`, cli.py and ~20 tests read.

This exists because a session once answered a question about a sick child with the owner's holiday street address, and **the owner could not find where it came from in their own session log** — correctly, because it wasn't there. The address was one memory's `description`, pasted into `messages[0]` before the first token. No tool call, no trace. The gates all behaved: `rule_eval` sees message and action shapes, not assembled context; `admission` sees writes, not reads; `knowledge` records preload, a different mechanism, and is suppressed when it selects nothing.

The live rescan two paragraphs up is exactly why this cannot be left to reconstruction. The index is a pure function of a **mutable directory at a moment in time** — mtime order, `expires` filtering — so touching one file makes yesterday's index unrecoverable. The entry in that incident carried `expires:`, so the evidence was on a timer. Names only, never descriptions: the name answers "which entry?", while for `remember`-saved memories the description often *is* the whole fact, and a log that copies it has re-leaked what it was written to explain. `TestIndexSelectionRecord`, `TestContextRecord`.

**Open, and the reason that memory's body was empty:** a `description` is both the retrieval key `recall` ranks on and the payload injected into every task. Writing one to be findable makes it a fact in every unrelated conversation. Descriptions should say what an entry is *about*, with the specifics in the body — a corpus migration plus a `remember` prompt change, not yet done.

---

## Pre-flight injection (L3)

`skills.preflight` picks the top entries for a task and `run_task` injects their bodies into the per-task reminder up front, capped by the `PREFLIGHT_*` constants. Oversized bodies get a teaser plus a **read gate** armed until `read_skill` loads them — lifted after bounded refusals so it can never wedge a task (`TestPreflight`, `TestSkillGate`, `TestPreflightInjection`).

**Injecting and BLOCKING do not share a confidence bar** (#238). Injection at `PREFLIGHT_MIN_SIM` buys roughly three-quarters precision, which is the right trade for a teaser: a near-miss costs some characters. Refusing the model's next tool call until it submits to reading a playbook is not comparable, and it ran on the same number — worse, on the keyword rail, which lowers that number further.

Measured on the session that produced the rule: the owner asked for his energy invoices *"for each apartment"*; `trippy_search`, a playbook for finding hotels, scored **0.282** — below the injection floor, and almost exactly the audit's median for an irrelevant entry (0.290). Its author had listed `apartment` as a keyword, so the keyword rail dropped the bar to `SEMANTIC_MIN_SIM`. And the body runs a few dozen characters past the oversized line, so the teaser came with a gate. Two `browse` calls refused, one call spent pulling a hotel playbook into a context about Polish electricity, and a sentence in the owner's chat explaining it — the one thing the refusal text explicitly forbids.

So `_gate_arms` is a second, stricter question, asked only of an oversized skill that is already being injected. **Named in the task arms it** — cosine may not veto an explicit mention. **A keyword hit never arms it on its own**: it is a prior for injection, and the whole failure was a curated word standing in for relevance it did not have. Otherwise the similarity must clear `GATE_MIN_SIM` (0.45 — the same audit's median for a *relevant* entry). In lexical mode there is no similarity to clear, so nothing but a name arms it; that is the conservative direction on purpose, because an unarmed gate costs a playbook the model may skip and an armed one costs the owner's task. The two real cases straddle the bar cleanly: 0.282 for the invoices, 0.454 for an actual apartment search.

An unarmed teaser also stops saying **REQUIRED**. A model told a read is required when nothing enforces it learns that aish's requirements are optional. `TestOnlyAStrongMatchMayBlockWork` pins the whole rule as a table.

**The waiver is a silent retry, not a speech.** Every surface used to tell the model that it could skip a truncated skill by *stating why it does not apply* — and the model's only channel for stating anything is the owner's chat, so a question about sharing PDFs into Obsidian came back with the plan plus *"(Note: the preloaded skill `ios-ssh-terminal-architecture` does not apply here because …)"*. That note bought nothing: nothing ever read the justification, the gate lifts on the refusal counter alone, so the retry always WAS the waiver. The prompts now say so and add the missing half — retrieval is the harness's bookkeeping, and which skills were or were not used is never reported to the owner (`test_waiver_is_a_retry_never_a_speech_to_the_user`, pinning all four surfaces: the teaser, `SKILL_GATE_REFUSAL`, `PRELOAD_REMINDER`, the system prompt). Positive transparency stays where it belongs — the `knowledge` trace record already shows what was preloaded.

The thresholds, all calibrated on the #183 audit (`TestPreflightPrecision`):

- **`PREFLIGHT_MIN_SIM` = 0.35** for unsolicited injection — relevant median 0.458 versus irrelevant 0.290.
- **`SEMANTIC_MIN_SIM` = 0.24** remains the floor for deliberate `recall` *and* for keyword-rail confirmation. A keyword hit is a strong PRIOR that lowers the bar, **never a bypass**: keywords are model-authored, and one generic word used to guarantee injection on a third of all tasks.
- **A NAME hit is unconditional** — naming an entry is unambiguous, and short names embed poorly.
- **Pinned rules never compete for preflight slots.** They are already in every task's index; re-injecting them stole slots from actual skills.

**Short tasks embed with context.** Below `PREFLIGHT_CONTEXT_TASK_CHARS` the query includes recent prior user turns (synthetic `[…]` notes filtered out), because a bare follow-up like "show on map" is hopeless alone. The keyword and name rails still scan ONLY the current message.

**Diagnostics ride the log.** `Preload.mode` (`semantic`|`lexical`) plus per-item `sim`/`rail` (or lexical `score`) go out on the `knowledge` trace step, so retrieval precision is auditable from logs alone — the #183 audit had to reconstruct all of this by hand. Keep it cheap and keep it there; the curation ledger reads exactly these records.

In lexical fallback the keyword rail is a full guarantee again — there is no similarity to confirm against (L2).

---

## Deliberate recall

`rank_entries` + `recall_text`: deterministic difflib tiers, two-phase, hard caps, mtime-cached parsing (`TestRankEntries`, `TestRecallText`, `TestRecallTool`). Both take `semantic` and `Agent._recall` passes it, so embeddings reach the deliberate-search path and not only preflight (#178 P1-9). Fusion keeps strong lexical hits — exact name, whole query inside the identity line — as the deterministic rail on top, and similarity orders the rest; a `None` from `scores()` degrades byte-identically to pure lexical. `TestSemanticRecall`.

`recall` also searches past sessions, except in an unattended session, where that half is dropped (see the origin gates in `docs/agent-core.md`).

---

## Writing

`remember` writes through `save_memory` — slug-validated, create-or-update, keywords deduped and capped (`KEYWORDS_MAX`) at write time. It stays auto-approved in an attended session, deliberately, so capturing a fact costs nothing. Skill files are written with the normal diff-approved `write_file`/`edit_file`. `TestSaveMemory`, `TestRememberTool`, `TestForgetMemory`, `TestForgetMemoryTool`.

**The near-duplicate gate (#178 P1-8).** A NEW slug whose identity line is too similar to an existing memory is refused WITH that entry's name — update it, forget it, or pass `force=true`. Similarity comes from the wired `SemanticIndex.scores` (`DEDUP_MIN_SIM`) or a conservative difflib ratio (`DEDUP_LEXICAL_RATIO`) when embeddings are down. Updates to the same slug are never gated. `TestNearDuplicateGate`.

That gate demonstrably worked and was **invisible**, because its refusal begins "NOT saved — " and the runtime logged refusals green by prefix-sniffing. It now emits an `admission` record carrying `sim`/`floor`/`mode`/`against` through `save_memory(on_admission=…)` — a callback, not a changed return type, because the string return is what cli.py, curate.py and every test read — which is what finally makes `DEDUP_MIN_SIM` measurable rather than "provisional until measured". `TestNearDuplicateAdmission`.

**Lifecycle (L4, #178 P1-8).** `status: disabled` and `expires: YYYY-MM-DD` retire an entry without deleting the file. `entry_active()` is evaluated at READ time — a long-running process crosses an expiry with no mtime change — and enforced INSIDE `load_entries`, so index, preflight, recall and dedup all inherit it by construction. `load_skill` names the retire reason instead of claiming the skill is missing. Date parsing is tolerant on read (malformed = no expiry, plus a warning) and strict on write. `save_memory`/`remember` take `disabled` (None preserves, True retires, False revives): the reversible verb that lets curation avoid `forget_memory` entirely. `TestLifecycle`.

---

## `embeddings.py`

One local Ollama embedding model (`embeddinggemma` by default — multilingual, because tasks arrive in Polish while entries are English; `AISH_EMBED_MODEL` overrides) scores task-versus-entry similarity **regardless of which chat backend runs the task**, so retrieval behaves identically on every `--model` and the corpus never leaves the machine.

Entries embed as a single **identity line** (`name: description (keywords: …)`), never bodies — selection reads identity, so vectors stay stable while playbooks grow. Vectors cache in the state dir keyed by `sha256(model + text)`. Retrieval-tuned models need task-type **prefixes that Ollama does not add** (`_PREFIXES`); skipping them measurably collapses similarity separation by about 2×.

Both entry points wire it identically — `Agent(semantic=SemanticIndex(state_dir))` — and the agent threads `scores` into preflight, recall and the dedup gate. `TestSemanticIndex`, `TestPreflightSemantic`.

**Retrieval quality is regression-gated.** `tests/test_retrieval_quality.py` is a recall@3 harness over a fixture corpus with a deterministic concept-axis embedder injected at the `embed=` seam: semantic must score 1.0 on both preflight and recall including the Polish→English cases, and lexical-only floors of 0.6/0.3 are asserted. `TestSemanticRecallAtK`, `TestLexicalFloor`, `TestHarnessSeam`.

---

## `curate.py` — the self-curation loop

Console entry point `aish-curate`, weekly via launchd. **The defining property is that the orchestration lives in the SCRIPT, not in a model session**: v1 handed one agentic session a 12-suspect queue and the 8B local judge could not hold it — 55 reads, zero actions — so v2 inverted it. Three layers:

**Context health** (`scan_context`, `aish-curate --context`) — a separate pure pass over the same logs, there to answer whether the history policy change (#243) actually helped rather than to curate anything. It reports trims per task, characters destroyed, how many stubs carried a key back to the full text, repeated identical calls, and the median prompt size — and attaches **no conclusion** to them. Repeated calls are the *shape* of a model that lost what it already found, not proof of it, so the count sits beside the trim count instead of being presented as a cause. One unreadable log costs its own file and nothing else (`docs/session-log.md`). `TestContextScan`.

The baseline it measured over the 60 days before the change: 754 tasks, 475 trims (0.63 per task), **45% of tasks trimmed something**, 13.8M characters destroyed, **0% of stubs recoverable**, and 35 of 41 repeated calls occurred in a task that had already lost history. Two thirds of the trims were the unconditional `eager_stub`.

**1 · The ledger** (`scan_ledger`) — pure code over the session logs. Pairs each `knowledge` trace step with its task window (the step is emitted just BEFORE its user message, so it attaches to the NEXT user record) and measures **engagement** (a `read_skill` of an injected entry) and **misses** (a `read_skill` of an entry preflight never surfaced). `dead_weight` (≥ `MIN_INJECTIONS`, zero reads) and `missing` are the suspect lists. `TestLedgerScan`, `TestClassification`.

**2 · The judge loop** (`run_curate`) — one bounded model call per suspect. `judge_prompt` packs identity + capped body + stats + evidence and demands the forced format `VERDICT: repair|pin|disable|skip` with an example, because small local models need MUST plus a concrete example. The model is a pure judge: no tools, no session, no server — `_make_judge` calls `backends.make_chat` directly. `TestJudgePrompt`, `TestJudgeLoop`.

**The envelope is code (L5).** `parse_verdict` admits only those four verbs — a repair without a replacement description is a parse failure, with one format-nudge retry and then an unparseable skip — and `update_entry_meta` rewrites **frontmatter only**: description, keywords, pinned, status; body preserved byte for byte; other lines kept; **deletion has no code path**. `TestVerdictParsing`, `TestUpdateEntryMeta`.

Every judged entry, skips included, lands in `curation-actions.jsonl`, which doubles as the **cooldown** (`ACTION_COOLDOWN_DAYS`): the ledger's stats lag reality by up to `LEDGER_DAYS`, so without it every pass would re-flag what the last one fixed. It also makes a retried launchd run idempotent with no server-side dedup.

**3 · The duplicate pass** — cross-entry judgment cannot fit a one-entry prompt, so `dup_candidates` proposes pairs **deterministically** (identity-line embeddings at `MERGE_MIN_SIM` = 0.72, deliberately stricter than `DEDUP_MIN_SIM` because on the live corpus every TRUE duplicate scored ≥ 0.77 while every related-but-distinct trap sat at 0.63–0.66; difflib when embeddings are down) and the judge answers one pairwise merge-or-distinct. A merge names the survivor — naming neither is invalid, the envelope never guesses — and disables the loser. `TestDuplicatePass`.

**Envelope guards**, each from the first live v2 run and each a CODE refusal rather than judge wisdom (`TestEnvelopeGuards`):

- `_same_family` bars pairs where one name extends the other with a dash (reply/reply-all, gmail/gmail-send — deliberately similar siblings, and exactly what the 8B judge wrongly merged).
- A `disable` verdict on a PINNED entry is refused; repair stays allowed. The judge retired two standing rules.
- A skill never loses a merge to a memory — a playbook outranks its one-line echo.
- Dry runs use the SAME embedding scorer as live runs, so the candidate list a dry run shows is the list a live run judges.

**Privacy.** Evidence excerpts quote owner-typed text, so the judge defaults to the LOCAL model and nothing leaves the machine unless `--model`/`AISH_CURATE_MODEL` names a cloud spec explicitly. A completed pass with actions sends one Pushover summary through the `notify_fn` seam. Seams: `run_curate(judge=…, scores=…, notify_fn=…, state_dir=…, now=…, dry_run=…)`; `--dry-run` lists suspects and pairs with no model calls and no writes.

**Deliberately absent: automatic threshold retuning.** The ledger informs; `PREFLIGHT_MIN_SIM` moves by human decision.

---

## `skill_import.py` — importing a skill (L6)

The untrusted surface is imported **skills**; tools need no import check because they are BUILT locally via `create_tool` plus diff-approval.

`stage()` only fetches — a shallow, read-only `git clone` that never executes the skill's code — validates (a real `SKILL.md`, name-checked) and collects the **text** files; binary assets are skipped, not being part of the trust surface. `TestStage`, `TestSafetyScan`.

`Agent._import_skill` then presents ONE consolidated review through the `approve_import` callback: the whole skill in a single decision — description, deterministic **risk flags** (`safety_scan`: network, pipe-to-shell, sudo, sensitive-path patterns) and every file's full contents, syntax-highlighted in the web card. Deliberately NOT per-file diffs, which was approval fatigue plus an all-green diff nobody can read. On approve everything installs at once with no further prompts; a denial installs nothing. The CLI reviewer prints the flags and every file for one y/N. `TestSkillImport`.

**The escape hatch** (`aish skill <import|approve|list|discard>`): `import` STAGES to a quarantine dir and prints the risk flags plus the path, so you review the files in your own editor, and `approve` then installs them. That is the path for a skill too large to read in a card. `TestQuarantine`.

Trustworthy public sources: `anthropics/skills`, `VoltAgent/awesome-agent-skills`.
