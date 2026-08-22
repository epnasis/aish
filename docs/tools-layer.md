# Tools layer — plugin tools, authoring, secrets

`tool_plugins.py`, `secrets.py`, and `create_tool` in `agent.py`.

**How to use this file.** The doctrine first — it is what decides whether a capability should be a tool at all, and most review comments on this layer are really disagreements about that. Then the manifest, the gate, authoring, and the Keychain. Skills are the other half of the capability model: `docs/knowledge-layer.md`.

---

## Doctrine

**Skills-primary, tools-as-scalpel** (epic #141). A tool is added only for operations that are **frequent**, have **shell-fragile arguments**, and need **reliability** — never for read-only, simple, or one-off work, which a documented skill snippet does better. That test lives imperatively in `create_tool`'s own description, because it is the model that decides.

**Schema on the way IN, prose on the way OUT.** Validated JSON args are handed to the executable on **stdin, with no shell** — so free-text arguments cannot be mangled by quoting, which is the one irreducible win over a skill's shell-invoked script. Output is raw stdout+stderr plus `[exit code: N]`; there is no output schema.

**Indistinguishable from native tools.** `to_tool_def()` emits the exact `{"type":"function","function":{…}}` shape `tools.TOOL_SCHEMAS` uses, and `agent._dispatch` routes both through the same gate. Anything that makes plugin tools a second class — a separate dispatch path, a separate approval channel, a separate parallelism rule — is a regression. `TestToolDef`, `TestValidateArgs`, `TestExecute`.

**Fail closed.** `mutating` is **required** in the manifest: one that does not declare it is invalid, never silently read-only. It is a **floor, never authority** — see shadowing. `returns` is required on the same terms — see the output contract.

**A tool's own verdict is not evidence.** The wrapper decides its exit code, and the wrapper is usually written by a model. Everything the runtime knows about whether a call worked, it must be able to check itself.

---

## The manifest

A tool is a folder `<name>/TOOL.md` under `~/.config/aish/tools/`, bundling an optional wrapper. **Project scope (`./.aish/tools/`) is DISABLED by default** (#178 P0-1 interim): a repository's read-only manifest would otherwise run its wrapper ungated. Opt-in is `tool_plugins.INCLUDE_PROJECT_DIRS`, which neither entry point sets. `TestProjectScopeDisabled`.

Frontmatter is dependency-free line parsing (`schema` is a JSON object string). The linter `_parse_tool` is deterministic and **skips** an invalid manifest with a warning rather than crashing discovery. `resolve_executable` runs only a bundled `./wrapper` inside the tool dir or a bare PATH binary — no absolute or escaping paths. `TestParse`, `TestResolveExecutable`, `TestDiscover`.

The agent rescans only when the dirs' `signature()` (an mtime set) moves, so a mid-task manifest edit is picked up on the next step.

**A broken manifest in a TEST fixture HANGS the suite, it does not fail it.** Skipping is right in production and treacherous in tests: the tool is simply not discovered, so a `test_server.py` test waiting on its approval card waits forever — no error, no failing assertion, just a run parked at 71%. A dropped `f` prefix while reflowing a fixture string did exactly this (`{{` left doubled in a plain string, so the schema JSON was literally `{{"text": …}}`). When a tool test hangs, suspect the manifest before the socket.

**Shadowing is a monotone floor across scopes (#178 P1-3).** A project `TOOL.md` shadows a same-named global one, but a shadow may only RAISE a tool to mutating, never lower it: a downgrading shadow — project `mutating: no` over global `mutating: yes`, which would route the mutation through the ungated parallel read path — is REFUSED outright in `discover`, the global mutating tool survives (still gated), and a loud warning names both paths. Same-flag and upgrade shadows keep project-wins. Dormant while project dirs are unscanned, load-bearing for the future trust gate. `TestCollision`.

**Declarable fields beyond the schema:** `mutating` (required), `returns` (required), `preview`, `prefer_over`, `secrets`.

### The output contract — `returns` (#193)

**What a successful result must contain, declared by the author and checked by the runtime on every call.** Three forms, and all three are explicit because the field is REQUIRED: a space-separated **field list** (the JSON object must carry each one, non-empty), **`text`** (non-empty output is the whole contract — prose, or a JSON array, where a legitimately empty result is still a success), or **`none`** (nothing about the output is checkable; recorded as the opt-out it is). `_parse_returns` skips a manifest that declares nothing, exactly as a missing `mutating` does.

It flows straight into `classify_output`'s `required`, which #192 had already built and which nothing had ever populated: `Tool.returns` → `execute` → `envelope(required=…)`. `None` means no contract, `()` means `text` and records `declared: []` so a real contract can be told from an absent one in the log, and a tuple means the fields.

**The hole it closes.** `youtube_analyze` printed `transcript: null` beside a populated `error_log`, exited **0**, and was graded `ok`. The model received a title, an author, a thumbnail and no words, and wrote a confident digest of the podcast from elsewhere. Only `error_field` stood between that and a silent substitution, and only because this author happened to name the field `error_log` — a wrapper reporting failure any other way had nothing at all. The exit code is supplied by whoever wrote the wrapper, which is usually a model; the declared contract is the runtime's own check, so it holds whatever the wrapper claims.

**Declared fields with a non-JSON payload is `incomplete`, not `ok`.** Otherwise a wrapper opts out of its own contract by no longer printing JSON — the same silence in a new costume. This is why a JSON **array** tool (`gmail_search`) must declare `text`: an array holds no named fields, and zero search hits is a success, not a failure.

The linter cannot check that a declared field is one the wrapper can actually produce — that needs a run, which is #193's birth check and is deliberately not built here (a mutating tool cannot be smoke-run without mutating). What ships is the declaration, the runtime check, and `create_tool` refusing a manifest without one. `TestOutputContract`.

---

## The gate

Read-only tools auto-run. **Mutating tools are gated by `approve_tool`**, a fourth approval callback wired from both entry points — the CLI's y/N prompt, and the web's `kind:"tool"` card, which **reuses the command card verbatim**.

`_dispatch_plugin_tool` mirrors `run_command`'s #81 verdict semantics exactly: `True` runs, `None`/`False` denies, `Denied(comment)` STOPS and arms the stop gate, `Approved(comment)` HOLDS — the args are not run, the model reworks and re-proposes, and the adjusted call is approved again before it runs.

**Mutating tools are exposed to the model ONLY when a tool approver is wired.** Without one they stay hidden and also fail closed in dispatch, so a mutation can never run ungated. There is deliberately **no denylist and no auto-approval** on tools — a mutating tool always prompts. Their safety is manifest review at authoring time plus this per-call gate. `TestPluginTools`, `TestToolApproval`.

**Read-only plugin tools parallelize** exactly like native ones: `_execute_tool_calls`' concurrent filter includes `_is_readonly_plugin`, and `_read_only_call` has a plugin branch (`_run_readonly_plugin` = validate + execute, thread-safe subprocess). `TestReadonlyPluginParallel`.

### The preview seam (#157)

A manifest may declare `preview: yes`. Before gating, `_dispatch_plugin_tool` calls `preview()`, which re-runs the SAME wrapper with `AISH_TOOL_PREVIEW=1`; the wrapper is contracted to RESOLVE and describe its arguments — an id-addressed `reminders_delete` runs `rem show <id>` — and print ONE human sentence WITHOUT mutating. That ground-truth string rides the optional third argument of `approve_tool` onto the card, above the now-secondary raw args.

This is the tool layer's **plan/commit gap**, mirroring `files.py`'s diff: it fixes id-opacity, so the human sees *what* they are approving rather than `id=F5D0…`. It is **fail-OPEN** — no `preview`, an error, empty output, a timeout or an unset secret all yield None and the raw-args card — and it is **ground truth produced by the system**, deliberately NOT a model-supplied summary, because a wrong id with a right-sounding summary is exactly what the gate exists to catch. `TestPreview`.

---

## Budgets, drift, and results

**Soft tool budget (#178 item 14).** When the TOTAL exposed count (native + plugin) exceeds `TOOL_BUDGET` (25), `budget_warning()` emits a one-line consolidation nudge naming the largest `<prefix>_*` family — the per-subcommand explosion the doctrine forbids — through the same once-per-rescan warning channel as shadow warnings. **No tool is ever hidden**: every schema is resent every turn, so the budget guards the context window, not capability. `TestToolBudget`, `TestBudgetWiring`.

**Drift nudge (#140).** A manifest may declare `prefer_over:` — raw command prefixes this tool should be used INSTEAD OF, alternatives included, not only the commands it wraps. The agent builds `_tool_prefer` from EXPOSED tools and, when the model runs a matching raw `run_command`, appends an advisory note steering it to the tool next time. The command still runs: it is a learning nudge, not a block.

**Result envelope and continuation (#192).** `execute()` returns a `tools.ToolOutcome` rather than a bare string, built by `envelope()` — status, `verdict_by`, `exit_code`, `bytes`, and a `truncation` object. The cap WAS a hardcoded `6000+2000` consulting nothing: not the model, not the context window. `output_caps(window)` derives it from the real backend, floored at the old constants so a small local window never regresses and ceilinged at `_OUT_CEILING` so a 1M window does not hand the model a novel. `TestResultEnvelope`, `TestBackendSizedTruncation`.

Truncation is no longer a **dead end whose only escape was improvisation**: the full output is cached content-addressed in `state_dir/tool-output`, LRU-pruned like the media store, and the truncated result carries an imperative note naming the exact `read_tool_output(continuation=…, page=2)` call. Pages are served from that cache, so **the wrapper never re-runs** — for a nondeterministic or mutating tool a re-run is a different result or a second side effect, not merely slower. `TestContinuation`.

When status ≠ ok the result also carries `_incomplete_note`: the imperative, in-band instruction that a failure-policy *memory* structurally could not be. It is delivered on the result itself, on the channel where the triggering state lives, at the moment it lives there — rather than depending on retrieval keyed on user text to surface a rule about a tool OUTCOME.

---

## Authoring — `create_tool` (#138)

A native, model-autonomous tool that writes a new `TOOL.md` plus wrapper. Three guardrails the model cannot bypass:

1. **The WHEN test is imperative in the tool's description** (see Doctrine) — capability phrasing in aish's own prompts gets ignored by small models unless it is a MUST plus a concrete example.
2. **`Agent._create_tool` LINTS the drafted manifest in a temp dir** and refuses to write on any error, returning structured feedback so the model corrects and retries. A broken tool cannot land. A non-bool `preview:` value is written verbatim rather than coerced, precisely so this refusal catches it instead of producing a tool that silently promises a preview it never emits.
3. **Both files go through the normal `approve_write` diff gate.** The **manifest is written and approved FIRST** — the interface: review intent, then verify the code — then the wrapper. A denial aborts without leaving an orphan.

`returns` is a REQUIRED argument of `create_tool` and is written into the manifest **verbatim, including when the model omitted it** — so the lint refuses the tool rather than `_create_tool` inventing a contract on the author's behalf. A guessed contract and a checked one are indistinguishable in the log, and only one of them is true. The argument's description carries the WHEN test the same way the doctrine does (imperative, with examples), plus the instruction the original wrapper most needed: **exit non-zero when the tool did not do what it promises.**

The wrapper is `chmod +x`'d after commit and the plugin signature is reset, so the new tool is offered on the next step. `wrapper_lang` sets file and shebang (sh|python). `scope` picks global; `scope: project` is refused with a structured error while project discovery is off, since a tool that is never discovered would be a silent no-op. Each tool is its OWN directory holding `TOOL.md` + wrapper, stated imperatively in the description so the model stops inventing a flat `.json` layout. Out-of-project write cards show the full home-abbreviated path, so a global-config destination cannot be mistaken for a file in the current project.

**Knowledge reconciliation (#150).** After a tool is created, `_reconcile_candidates` deterministically finds skills and memories that mention a `prefer_over` command or share the tool's subject words, and the RESULT appends an imperative instruction to update them to point at the tool, or forget the ones describing the now-superseded manual way, while KEEPING orthogonal context. Detection is code; the judgment and the gated mutations are the model's — so stale guidance cannot silently contradict a new tool.

---

## Secrets — `secrets.py` (#142)

A local secret store backed by the **macOS login Keychain**, for the non-CLI integrations aish must hold a credential for (Fastmail, Home Assistant, ntfy, webhooks — "auth stays with the CLI" covers none of them). Each secret is a generic-password item under service `"aish"` via `/usr/bin/security`; a plaintext NAME index (never values) in the state dir backs `aish secret list`. Managed with `aish secret <set|get|list|rm>`, intercepted before the main arg parser, with `set` reading via getpass. `TestSecrets`.

Chosen over sops+age for being Apple-native, zero-dependency, and **structurally un-committable** — a Keychain item is not a file, so it cannot be swept into the git-backed `~/.config/aish`. The honest security ceiling is FileVault plus login-unlock: this defends against disk theft and accidental leakage into git, logs or model context, NOT against a live attacker already running as the user.

**Site sign-ins are a SECOND Keychain namespace (`aish-signin`), and the separation is a fence.** Two things resolve a name against the `aish` service — a manifest's `secrets:` field and `aish secret get` — and a site credential must be reachable by neither: a wrapper that could declare `secrets: eon_pl` would put the owner's password into a subprocess environment aish does not control. They ARE covered by `scrub`, which is what stops a login page echoing a failed password back into the model's context. The suite-wide guard extends with them: `no_real_secrets` empties `signin.STATE` as well as the name index, because the scrub asks for the origin list on every tool result and would otherwise shell out to `security` for the developer's live credentials — which is exactly what the guard's own pinning test caught the day this shipped. → `docs/browser.md`

**Injection into a tool.** A manifest may declare `secrets: NAME1 NAME2`; `execute()` resolves each through the Keychain and injects them into ONLY that wrapper's subprocess environment — never into args, so a value cannot leak into logs or the model's context. A declared-but-unset secret is a hard error, not a silent empty env. `create_tool` can populate the field. `TestSecretInjection`.
