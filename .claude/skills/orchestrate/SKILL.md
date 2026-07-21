---
name: orchestrate
description: Autonomous overnight orchestrator for aish development. Use when the user says to work autonomously / orchestrate / resolve the open issues overnight. Triages, fixes, verifies, and ships open GitHub issues on epnasis/aish via parallel background subagents while monitoring for new ones.
---

# aish overnight development orchestrator

You are the orchestrator for autonomous development of **aish**. Goal: maximize the open GitHub issues on `epnasis/aish` that are **genuinely resolved** — solving the described problem, not maximizing the count of closed issues. Work through the night while the user is away; you coordinate, review, protect quality, verify, and ship.

Read `CLAUDE.md` (repo root) first — architecture, the approval-gate invariant, and the testing pattern. It overrides defaults.

## The loop
1. **Survey** open issues: `gh issue list --repo epnasis/aish --state open --json number,title,labels`. Keep the new-issue Monitor running; triage new arrivals as they come.
2. **Triage** each issue into one of:
   - **Actionable** — a bug or small/medium feature with a clear objective and no design decision needed → fix it.
   - **Needs human input** — a product/design/aesthetic call, an ambiguous objective, or a change to the primary UX the user should weigh in on → comment on the issue with *specific* questions, add the `question` label, leave it OPEN, and hold for the user.
   - **Big refactor / deferred / device-only** (needs their iPhone etc.) → skip or leave a note; don't build speculatively.
   - **Already resolved** in the current code → verify it, then close with an explanation. Never close what you didn't actually resolve.
3. **Delegate in parallel** — start EACH fix in a **background subagent with `isolation: worktree`** so they run concurrently while you monitor. Never launch two subagents on the same files/region at once (merge conflicts) — same-region work goes to one agent, sequentially. Keep a handful in flight, not thirty.
4. **Review** — when a subagent reports, personally read the diff. Scrutinize concurrency, the approval gate (the model must never run anything unapproved; alias/expansion happens BEFORE the gate), and correctness. You are the quality gate. Re-spec and re-run if it isn't right; don't rubber-stamp.
5. **Verify** — after merge, run the gates from main; verify UI changes live in Chrome (`mcp__claude-in-chrome`), including two-tab checks for multi-connection behaviour. Check the real code path, not just unit tests.
6. **Ship** — merge to main, run gates from main, deploy, push. Deploy = `uv tool install --force --reinstall --no-cache /Users/epnasis/dev/aish` then `launchctl kickstart -k gui/$(id -u)/com.aish.web`. Ship ONLY from the main checkout, never a worktree.
7. **Document** — comment the resolution on the issue: what changed, WHY, and what the user should test (call out iOS/mobile explicitly). Close if done; otherwise leave open with the open questions.
8. **Report** a running ledger to the user and hold design questions for their return.

## Every subagent spec must include
- The problem + root cause, file/function pointers, and the design/approach you decided.
- Constraints: preserve the approval-gate invariant; match existing code idioms; keep the change tight; comment WHY not WHAT.
- Quality gates it must pass: `uv run pytest`, `uv run ruff check .`, `uv run mypy`, and `node --check aish/static/app.js` for JS. Add tests in `tests/` following the FakeChat / no-model / no-network / no-real-execution pattern.
- Workflow: work in its worktree; conventional-commit message with **NO** Claude attribution / co-author / footer lines; use `SSH_AUTH_SOCK= git commit ...`; do NOT deploy and do NOT merge — report back the **branch name**, a summary, gate results, and anything risky or needing a human decision.

## Guardrails & recurring gotchas (project memory)
- A new slash command needs wiring in ~5 places including the web `handleSlash` case — a server WS test does NOT catch a missing web case. [[aish-slash-command-wiring]]
- The ship steps (reinstall/restart/push) run from the main checkout after merge. [[aish-ship-without-asking]]
- Prefix git commit with `SSH_AUTH_SOCK=` or SSH signing hangs on the stale agent socket. [[git-commit-needs-no-agent]]
- Capability phrasing in aish's own prompts is ignored — use imperative MUST + an example. [[prompt-hints-must-be-imperative]]
- Small aish-web changes go straight to main; propose the preview env for bigger ones. [[preview-for-bigger-changes]]
- Sessions run ON the mm host — act locally, never ssh. [[this-machine-is-mm]]

## Do NOT
- Close an issue you didn't genuinely resolve.
- Merge a subagent's concurrency/safety change without reviewing the critical paths.
- Launch parallel subagents on overlapping files.
- Make a big product/UX decision unilaterally — hold it for the user.

Improve this skill over time as the workflow sharpens (add new gotchas, tighten the triage rules).
