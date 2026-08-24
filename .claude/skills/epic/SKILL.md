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

## Session zero — build the plan, post it, then work

1. **Read the epic in full**, including every comment. Reviews and adversarial passes are often posted there and they carry the reasoning that stops decisions being re-litigated. Read the linked area docs before touching anything (`CLAUDE.md`'s routing table).
2. **Extract three things** and write them down explicitly:
   - **The properties** — the epic's stated laws/invariants. These are the conformance bar for every diff.
   - **The order** — stages, dependencies, and what the epic says must land before what, *with the epic's stated reason*. A dependency without its reason gets re-argued by the next session.
   - **The gates** — see below.
3. **Reconcile with reality before planning any work.** The repo is often ahead of the tracker. For every issue in the epic: `git log --all --oneline | grep -E "#(NNN)"`, check `git worktree list`, and grep the code for the feature. Verify-and-close is cheaper than rebuild, and a subagent dispatched at done work is a wasted night. [[gh-issue-list-default-limit]] — always `--limit 100`.
4. **Post the ledger as a comment on the epic**, and keep it updated there. This is the state of the drive, and it lives on the epic so a fresh session — or the user — can pick it up cold. One row per issue: stage, status (`blocked` / `ready` / `in flight` / `in review` / `merged` / `gated` / `deferred`), and for anything not `ready`, why.
5. Only then start work.

**Resuming:** a later session reads the epic, reads the latest ledger comment, re-runs step 3's reconciliation (things move between sessions), updates the ledger, continues. Never re-plan from scratch — if the plan looks wrong, that is a gate, not a rewrite.

## The loop

1. **Pick the next issue the ORDER allows.** Not the easiest, not the most interesting — the next one whose prerequisites are merged. If nothing is ready, everything remaining is behind a gate: report and stop.
2. **Check it is still the right thing to build.** The epic was written before the code existed. If the issue's premise no longer holds, that is a gate (§Gates), not a judgement call for you.
3. **Delegate** to a background subagent with `isolation: worktree`. Never two subagents on the same files. Within a stage, issues that touch one area go to one agent, sequentially. [[always-work-in-a-worktree]] [[parallelize-independent-work]]
4. **Review the diff personally.** Correctness, concurrency, the approval-gate invariant, test quality. You are the quality gate; do not rubber-stamp a subagent's own summary.
5. **Conformance review — the step that makes this skill worth having.** Separately from "is this code correct", ask: **does this diff move the system toward the epic's properties, or does it satisfy the issue while drifting off them?** Read the diff against the properties list from session zero, one at a time. Typical drifts, all of which have happened in this repo:
   - a control added whose safety argument is "the user will see it", under a property that forbids exactly that
   - a word list or per-page heuristic added under a property demanding systematic solutions
   - a permission widened by something judged, under a property saying only structure or the owner may widen
   - a claim made in the harness's own voice that the code cannot actually check
   - a fact stored that was inferred, under a property saying observe-don't-guess

   A diff that fails conformance is **not** merged and **not** silently reworked: state which property, and why. If the property is wrong, that is a gate.
6. **Verify against the real path.** Unit tests are the floor. UI changes get driven in a real browser and the console read — a clean load is part of "verified" [[verify-frontend-in-a-real-browser]]. Web-surface behaviour uses the `verify` skill's isolated harness; **never type into a chat you did not create**. And verify the ordinary case, not only the new one [[verify-the-case-that-already-worked]].
7. **Merge, ship, document.** Ship from the main checkout after merge, never a worktree. Comment the resolution on the issue: what changed, WHY, and what the user should test.
8. **Update the ledger comment on the epic.** Every time something lands. The ledger is the deliverable of the drive, not a courtesy.

## Gates — stop and ask

A gate is where you **stop, report, and wait**. Not a slowdown: the epic's whole purpose is that some decisions are the user's, and building past one silently is how the epic gets hollowed out while every issue closes green.

**Stop at:**

- **A design gate.** An issue whose approach is not settled, or that introduces a new artifact class / new concept. Propose the design, with alternatives and a recommendation, and wait. The epic will usually say which these are; when in doubt it is one.
- **A property gate.** Work that cannot be done without violating or amending one of the epic's properties. Never amend a property to make a diff fit. Report: what the work needs, which property it collides with, and what the options are.
- **A stage boundary.** End of a stage, before starting the next. Short report: what landed, what it cost, what changed about the plan.
- **A premise gate.** Implementation reveals the epic was wrong about something. **Amend the epic first, with the user, then build.** Quietly building the better thing is exactly the local-maxima behaviour the epic exists to stop — and it leaves a design record that no longer describes the system.
- **A scope gate.** Work discovered that is real but not in the epic. **File it, link it to the epic, do not build it.** Say so in the ledger.
- **A cost gate.** Something turns out to be materially more expensive than the epic assumed. Report the number before spending it.

**Do not stop at:** an ambiguity you can resolve from the epic, the code, or the area doc. The user delegated the process, not every decision [[work-with-critical-partnership]].

## Slicing

- **The first slice must replay the session or failure that produced the issue.** If slice 1 cannot service the thing that was filed, the slicing drifted — testability bias and subsystem analogies cause this [[first-slice-replays-the-issue]].
- Each slice ships independently and passes the gates on its own.
- A slice that only builds substrate, with nothing observable, is a sign the slicing is wrong. Find the thin vertical.

## Every subagent spec must include

- The problem, root cause, file/function pointers, and the approach **you** decided.
- **The epic's relevant properties, quoted.** The subagent has no context on the design and will otherwise produce a locally-correct, globally-wrong change. This is the single highest-value line in the spec.
- Constraints: preserve the approval-gate invariant; match existing idioms; keep the change tight; comment WHY not WHAT.
- Quality gates: `uv run pytest`, `uv run ruff check .`, `uv run mypy`, `node --check aish/static/app.js` for JS. New tests follow the FakeChat / no-model / no-network / no-real-execution pattern.
- Workflow: work in its worktree; conventional-commit message with **NO** Claude attribution or co-author lines; `SSH_AUTH_SOCK= git commit` [[git-commit-needs-no-agent]]; do NOT deploy, do NOT merge — report the branch name, a summary, gate results, and anything risky or needing a decision.

## Reporting

The user reads architecture, not code [[talk-architecture-not-code]]. Reports name what changed *about the system*, not which functions moved. Coin names for new concepts; do not name existing symbols they have never read.

A stage report answers three things: what the system can now do that it could not, what the epic said would happen and did not, and what the next stage is waiting on.

## Do NOT

- Build past a gate.
- Amend an epic property to make a diff fit.
- Reorder the plan for convenience — order carries reasons.
- Close an issue you did not genuinely resolve.
- Merge a safety- or concurrency-relevant diff without reading the critical path yourself.
- Let the ledger go stale. A drive with no current ledger cannot be resumed and cannot be reviewed.

Improve this skill as the workflow sharpens — new drift patterns for step 5 are the most valuable additions.
