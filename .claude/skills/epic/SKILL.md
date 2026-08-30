---
name: epic
description: Drive one epic to completion in the order it specifies — sequencing, review, conformance validation against the epic's own laws, and stopping at gates. Use when the user points at an epic ("drive #295", "work the epic", "continue the epic") or asks to sequence/coordinate epic work. Resumable across sessions; the plan lives on the epic itself.
---

# Epic driver

You are driving **one epic**, given by number. Not the backlog — one epic, in the order it specifies, holding its whole shape in mind.

`/orchestrate` is the other half of this pair and deliberately refuses epic-linked issues. This is where they go. The difference is not scale, it is **shape**: orchestrate is breadth-first over independent issues and optimises for how many are genuinely resolved; this is depth-first over ONE design and optimises for **the design surviving contact with implementation**.

Read `CLAUDE.md` (repo root) first, then the epic, then the area docs the epic touches. `CLAUDE.md` overrides defaults; the epic overrides your judgement about what to build next.

## Why this exists

The epic exists because independent, individually-correct fixes produced **local maxima** — several reversing each other within days, none aimed anywhere. From #295:

> "There's just too much independent work which was all local maxima as opposed to global maxima… as opposed to random choices for local issues."

So the failure this skill exists to prevent is not a bad commit. It is **six good commits that drift the system off the design**. Everything below is in service of that, and it is why conformance review (step 5) is not optional polish.

The first real drive (2026-08-24→26) shipped nine issues and still drew the owner's verdict: *"we did so many unnecessary turns back and forth without solving a problem that we wasted way more time and tokens than we should."* Where a rule below cites that drive, that is what it cost to learn.

## Session zero — build the plan, post it, then work

1. **Read the epic in full**, including every comment. Reviews and adversarial passes are often posted there and they carry the reasoning that stops decisions being re-litigated. Read the linked area docs before touching anything (`CLAUDE.md`'s routing table).
2. **Extract three things** and write them down explicitly:
   - **The properties** — the epic's stated laws/invariants. These are the conformance bar for every diff.
   - **The order** — stages, dependencies, and what the epic says must land before what, *with the epic's stated reason*. A dependency without its reason gets re-argued by the next session.
   - **The gates** — see below.
3. **Reconcile with reality before planning any work.** The repo is often ahead of the tracker. For every issue in the epic: `git log --all --oneline | grep -E "#(NNN)"`, check `git worktree list`, and grep the code for the feature. Verify-and-close is cheaper than rebuild, and a subagent dispatched at done work is a wasted night. [[gh-issue-list-default-limit]] — always `--limit 100`.
4. **Post the ledger as a comment on the epic**, and keep it updated there. This is the state of the drive, and it lives on the epic so a fresh session — or the user — can pick it up cold. One row per issue: stage, status (`blocked` / `ready` / `in flight` / `in review` / `merged` / `gated` / `deferred`), and for anything not `ready`, why. Non-trivial rows also carry the two Fable verdicts (§Fable): design before `in flight`, delivery before `merged`. An empty verdict cell is visible to the owner — that visibility, not your discipline, is what enforces §Fable.
5. Only then start work.

**Resuming:** a later session reads the epic, reads the latest ledger comment, re-runs step 3's reconciliation (things move between sessions), updates the ledger, continues. Never re-plan from scratch — if the plan looks wrong, that is a gate, not a rewrite.

## The loop

1. **Pick the next issue the ORDER allows.** Not the easiest, not the most interesting — the next one whose prerequisites are merged. If nothing is ready, everything remaining is behind a gate: report and stop.
2. **Check it is still the right thing to build.** The epic was written before the code existed. If the issue's premise no longer holds, that is a gate (§Gates), not a judgement call for you.
3. **Spec, review, delegate — and prove the isolation took.** Write the subagent spec (below); for anything non-trivial it goes to Fable for design review before dispatch (§Fable), because the spec is where a locally-correct, globally-wrong build gets decided. Launch with `isolation: worktree`, then **verify it**: the agent's first report names its working path, and the branch appears in `git worktree list` before you review anything. The first drive omitted the flag on two launches — the skill said "use worktrees" then too, so saying is not the control; checking is. Agents worked in the shared checkout, and a `git add -A` there swept one agent's in-flight work into an unrelated commit that merged to main. Stage by name in the shared checkout, never `-A` [[main-checkout-is-shared]]. Never two subagents on the same files; within a stage, issues touching one area go to one agent, sequentially. [[always-work-in-a-worktree]] [[parallelize-independent-work]]
4. **Review the diff personally.** Correctness, concurrency, the approval-gate invariant, test quality. You are the quality gate; do not rubber-stamp a subagent's own summary. And **relay, never absorb**: anything the subagent flags as open or undone goes into the ledger and the next owner report verbatim. One absorbed flag cost a day — the owner was promised visible evidence that was captured, stored, and rendered nowhere he could reach; the subagent had said so.
5. **Conformance review — the step that makes this skill worth having.** Separately from "is this code correct", ask: **does this diff move the system toward the epic's properties, or does it satisfy the issue while drifting off them?** Read the diff against the properties list from session zero, one at a time. Typical drifts, all of which have happened in this repo:
   - a control added whose safety argument is "the user will see it", under a property that forbids exactly that
   - a word list or per-page heuristic added under a property demanding systematic solutions
   - a permission widened by something judged, under a property saying only structure or the owner may widen
   - a claim made in the harness's own voice that the code cannot actually check
   - a fact stored that was inferred, under a property saying observe-don't-guess
   - **a guarantee stated wider than the code enforces.** Distinct from the harness-voice drift above: there the code checks nothing; here it checks something real and the prose runs past it — "never zero" beside a line clamping TO zero, "never a sign-in page" where the test is for a password box. Eight diffs did this in one drive, all correct, tested, and green on every other gate. The check is mechanical: for each guarantee the prose states, find the line that enforces it; where the words reach further than the line, cut the words or widen the code.

   A diff that fails conformance is **not** merged and **not** silently reworked: state which property, and why. If the property is wrong, that is a gate.
6. **Verify against the real path.** Unit tests are the floor. UI changes get driven in a real browser and the console read — a clean load is part of "verified" [[verify-frontend-in-a-real-browser]]. Web-surface behaviour uses the `verify` skill's isolated harness; **never type into a chat you did not create**. Verify the ordinary case, not only the new one [[verify-the-case-that-already-worked]]. And a promise that the owner will SEE something is verified by seeing it where he would look — captured-and-stored is not rendered.
7. **Verify delivery with Fable** for anything non-trivial (§Fable, duty 2) — a different question from "is the diff correct", which step 4 already asked. Verdict in the ledger row before `merged`.
8. **Merge, ship, document.** Ship from the main checkout after merge, never a worktree. Comment the resolution on the issue: what changed, WHY, and what the user should test.
9. **Update the ledger comment on the epic.** Every time something lands. The ledger is the deliverable of the drive, not a courtesy.

## Gates — stop and ask

A gate is where you **stop, report, and wait**. Not a slowdown: the epic's whole purpose is that some decisions are the user's, and building past one silently is how the epic gets hollowed out while every issue closes green.

**Stop at:**

- **A replacement with no measured comparison against the INCUMBENT.** A change that replaces working behaviour ships only with a paired measurement against *the thing it replaces* — not against a sibling candidate, and not against intuition. Three changes were built and reversed inside one week in 2026-08: the search snippet reader, the titles-only rendering that replaced it, and a stall watchdog built for a failure mode that did not exist. Every reversal was measured and documented, and every one was missing the same thing beforehand: the incumbent arm. Titles-only cost **+34,840 prompt tokens per task** against the behaviour it replaced, the owner predicted it, and the data that would have stopped it was already on disk. Measure-after-shipping-and-revert-fast is a different and far more expensive discipline than measuring first: it costs ship cycles, degraded behaviour on the owner's own machine, and in that case a permanent two-day hole in the corpus the project mines for test material.
- **A decision that changes the plan, made in conversation.** Post it to the ledger *before* moving on. Two rejections — a whole slice, and a design the owner himself proposed — lived only in a session transcript while the ledger still read "Next: build slice 2". The ledger is the epic's memory, and a fresh session follows it; an unposted decision has not been made. This is not bookkeeping, it is the thing that makes the drive resumable.
- **A design gate.** An issue whose approach is not settled, or that introduces a new artifact class / new concept. Propose the design, with alternatives and a recommendation, and wait. The epic will usually say which these are; when in doubt it is one.
- **A property gate.** Work that cannot be done without violating or amending one of the epic's properties. Never amend a property to make a diff fit. Report: what the work needs, which property it collides with, and what the options are.
- **A stage boundary.** End of a stage, before starting the next. Short report: what landed, what it cost, what changed about the plan.
- **A premise gate.** Implementation reveals the epic was wrong about something. **Amend the epic first, with the user, then build.** Quietly building the better thing is exactly the local-maxima behaviour the epic exists to stop — and it leaves a design record that no longer describes the system.
- **A scope gate.** Work discovered that is real but not in the epic. **File it with the epic named in the issue body, and do not build it.** The epic line is not bookkeeping: `/orchestrate` claims every issue with no epic link (plus epic-linked ones tagged `nextup`), so an unlinked filing invites a parallel build [[orchestrate-skip-epic-issues]] — the first drive had one issue implemented by both drives, one implementation discarded unmerged, and still filed four issues without the line. Say so in the ledger.
- **A cost gate.** Something turns out to be materially more expensive than the epic assumed. Report the number before spending it.

**Do not stop at:** an ambiguity you can resolve from the epic, the code, or the area doc. The user delegated the process, not every decision [[work-with-critical-partnership]].

## Fable — design in, delivery out, failures immediately

Fable is the drive's second pair of eyes, and the first drive measured what its absence costs: one sign-in failure, five confident wrong diagnoses over two days, four of them the drive's own. Every one was reasoned from **artifacts** — page text, a badge, a fetched copy of the HTML, a log. Fable, consulted at last, established the cause by **experiment** in one pass (the code never targeted that form's login button; its fallback gesture was a no-op on that form shape) and killed the drive's leading hypothesis with a second. One agent call, against two days.

**Non-trivial** means it needed a plan, a design decision, or more than one obvious edit. A one-liner or a mechanical change is out of scope — if this section fires on everything, it will be skipped on everything.

Three duties. The first two leave **verdicts in the ledger** (session zero, step 4); a duty whose evidence lives in your head is a recommendation, and recommendations are what the first drive had.

1. **Design review, before dispatch.** Fable reviews the subagent spec — approach, slicing, whether the thing being built is the thing needed — **with the epic's properties and the surrounding design attached**, not just the issue: a review that cannot see the design cannot catch a locally-correct, globally-wrong plan. Verdict in the ledger before `in flight`.

2. **Delivery review, before merge of the final slice.** Did everything the issue and the epic promised arrive, reachable by the owner, with no quietly narrowed scope? Step 4 did not ask this, and it is the cheapest place to catch a slice that shrank. Verdict in the ledger before `merged`.

3. **Failure diagnosis, immediately and unasked.** The moment something you built does not work and your first look yields no cause you can demonstrate by experiment, hand it to Fable. Do not ask the owner for permission — the round trip is the waste. An owner re-reporting a failure means you are already one consultation late.

Behind duty 3 is a rule that binds the drive everywhere: **never assert a cause you have not distinguished by experiment.** Artifacts suggest hypotheses; only an experiment that would come out differently under a rival hypothesis settles one. If you cannot name that experiment, you have a guess — the first drive delivered four guesses as findings. "Unresolved, handing to Fable" beats a fifth confident answer.

## Slicing

- **The first slice must replay the session or failure that produced the issue.** If slice 1 cannot service the thing that was filed, the slicing drifted — testability bias and subsystem analogies cause this [[first-slice-replays-the-issue]].
- Each slice ships independently and passes the gates on its own.
- A slice that only builds substrate, with nothing observable, is a sign the slicing is wrong. Find the thin vertical.

## Every subagent spec must include

- The problem, root cause, file/function pointers, and the approach **you** decided.
- **The epic's relevant properties, quoted.** The subagent has no context on the design and will otherwise produce a locally-correct, globally-wrong change. This is the single highest-value line in the spec.
- Constraints: preserve the approval-gate invariant; match existing idioms; keep the change tight; comment WHY not WHAT.
- Quality gates: `uv run pytest`, `uv run ruff check .`, `uv run mypy`, `node --check aish/static/app.js` for JS. New tests follow the FakeChat / no-model / no-network / no-real-execution pattern.
- Workflow: work in its worktree and **name the worktree path in the first report**; conventional-commit message with **NO** Claude attribution or co-author lines; `SSH_AUTH_SOCK= git commit` [[git-commit-needs-no-agent]]; do NOT deploy, do NOT merge — report the branch name, a summary, gate results, and anything risky, open, or needing a decision.

## Reporting

The user reads architecture, not code [[talk-architecture-not-code]]. Reports name what changed *about the system*, not which functions moved. Coin names for new concepts; do not name existing symbols they have never read.

A stage report answers three things: what the system can now do that it could not, what the epic said would happen and did not, and what the next stage is waiting on.

**State only fresh facts.** The tracker moves under a long drive — the orchestrator, the owner, other sessions. Re-read before reporting its state or dispatching on it; the first drive reported an hours-old reading to the owner as current fact.

## Do NOT

- Build past a gate.
- Amend an epic property to make a diff fit.
- Reorder the plan for convenience — order carries reasons.
- Close an issue you did not genuinely resolve.
- Merge a safety- or concurrency-relevant diff without reading the critical path yourself.
- Dispatch non-trivial work without a Fable design verdict, merge it without a delivery verdict, or keep hold of a failing diagnosis Fable should have. All three were paid for in full once.
- `git add -A` in the shared checkout, ever.
- Let the ledger go stale. A drive with no current ledger cannot be resumed and cannot be reviewed.

Improve this skill as the workflow sharpens — new drift patterns for step 5 are the most valuable additions.
