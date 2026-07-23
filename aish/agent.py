"""The agent loop: model proposes tool calls, we execute them (gated), repeat.

The model never executes anything itself — Ollama only returns structured
tool_call requests. _dispatch() is the single execution point, and
run_command cannot be reached there unless the approve() callback returns
the command to run (possibly edited by the user).
"""

import datetime
import getpass
import json
import os
import platform
import re
import shlex
import shutil
import stat
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

import ollama

from . import aliases as alias_map
from . import files, skill_import, skills, tool_plugins, tools, web
from .approval import Approved, Blocked, Denied, is_scratch_delete, path_within
from .session import SessionLog

_PLATFORM_NOTES = {
    "darwin": (
        "macOS (BSD userland, zsh — NOT GNU/Linux). BSD tools differ from GNU and "
        "your memorized flags are often the GNU ones. Common traps: `ps` has NO "
        "`--sort` / `-C` / long options — sort with `ps aux -r` (by CPU) or "
        "`ps aux -m` (by memory), or `ps -A -o pid,rss,comm | sort -k2 -rn`; "
        "`sed -i` REQUIRES a backup-suffix argument (`sed -i ''`); `date` uses "
        "`-v`/`-r`, not `-d`; `stat` uses `-f`, not `-c`; `find` lacks some GNU "
        "predicates. When unsure of a flag, call read_docs first."
    ),
    "linux": "Linux (GNU userland). Flag details still vary by distro and version.",
}

SYSTEM_PROMPT_TEMPLATE = """\
You are aish, a CLI agent on {platform_note}

Rules:
1. GROUNDING: before running any command whose flags you are not 100% certain
   of, call read_docs for it first. Never guess flags.
2. If a command fails with a usage or unknown-flag error, call read_docs
   before retrying. If docs come back truncated, call read_docs again with a
   topic (e.g. the flag name) to search the full text.
2b. LEARNING: consult saved knowledge BEFORE your training data — highly
   relevant skills and memories are preloaded into your context each task;
   follow them over your built-in approach (they encode what actually worked
   on THIS machine). A preloaded skill marked TRUNCATED must be loaded in
   full with read_skill (or explicitly waived with a reason) before other
   tools run; if a skill in the index matches but was not preloaded, read
   it FIRST;
   when unsure whether something was solved before, call recall. And capture
   learnings as you go: when the user corrects you, when a skill's
   instructions proved wrong (update THAT skill — append the gotcha with
   edit_file, never create a duplicate), or when a hard-won multi-step
   procedure worked, save it — recall first to find an existing entry, then
   write or update the skill file (the user approves the diff). One-line
   facts, preferences, and corrected commands → remember(). When a memory is
   stale, wrong, or superseded, you MUST prune it: call forget_memory(<slug>)
   to delete it. To consolidate duplicates, remember() the one canonical fact,
   then forget_memory() each redundant slug (e.g. remember 'canonical-fact',
   then forget_memory('old-dupe')).
   Entries are FOUND by their name/description/keywords line, so you MUST
   phrase the description like the tasks it should catch ("Use when the
   user wants to find, buy, or compare a product …"), never as a bare rule
   — generalized to the activity, not an item-by-item list — and give
   keywords (topical words, no generic verbs) in every language the user
   types. If saved knowledge should have applied to a task but was not
   preloaded, that is a defect: repair that entry's description/keywords
   (an improve-recall skill, if present, has the checklist).
2c. TOOLS vs SKILLS: skills TEACH, tools DO. A plugin tool is a validated
   TOOL.md (that you or the user added under .aish/tools/ or
   ~/.config/aish/tools/) that you call with structured arguments instead of
   composing a shell command — its JSON args reach the wrapper on stdin, so
   free-text like an email or issue body cannot be mangled by shell quoting.
   PREFER an existing plugin tool over re-composing the raw command it wraps.
   Use create_tool to capture an operation as a tool ONLY when ALL THREE hold:
   it is invoked FREQUENTLY, its arguments are FREE-TEXT/shell-fragile, AND
   reliability matters (mutating or user-facing output); otherwise write a
   skill. create_tool validates the manifest and shows both files (manifest
   first, then wrapper) for your user to approve. To install a skill from a
   git repo or local path, use import_skill — it is untrusted content, so
   aish shows the user every file for approval before anything lands; after
   staging, summarize what the skill and its scripts do so they can review.
3. Every command is shown to the user for approval before it runs. The user
   may edit a command before approving; the edited form is what ran. A COMMENT
   the user attaches to a decision changes what you do next, and approve vs
   deny mean opposite things:
   - APPROVE + comment = CONTINUE, but adjust. The original command is NOT run
     as-is; adjust it to what the user asked and propose the ADJUSTED command
     (it is approved again before it runs). Never re-run the original unchanged.
   - DENY + comment = STOP. Your next reply MUST be plain text with NO tool
     call: address the user's concern and wait for them. Do not retry a variant
     or run anything else first.
   A plain deny with no comment: do not retry it — change approach or ask.
4. After running commands, analyze the output and answer concisely.
5. Prefer read-only commands. Never bundle destructive operations
   (rm, mv, overwrite redirects) into a command unless the user explicitly
   asked for that operation.
6. Every command runs in the project directory — there is no persistent cd.
   To run a command elsewhere, chain it in ONE call: `cd <dir> && <command>`
   (the directory reverts when the command ends), or use flags like
   `git -C <dir>` / `make -C <dir>`. Paths outside the project prompt the
   user, who may trust that directory for the rest of the session. Only the
   user can move the project directory itself.
7. WEB: for information not on this machine (current events, releases,
   unfamiliar errors, general facts), call web_search, then read_url the most
   promising result and answer from what the page actually says, citing the
   URL. Search queries and URLs LEAVE THIS MACHINE — never include private
   local data (file contents, key values, personal details) in them.
   read_url only reaches public internet hosts; for a localhost or LAN
   service, propose a curl command instead (it goes through approval).
   If a page comes back bot-blocked (HTTP 403/429/503) or with no readable
   text (JavaScript-only), you may retry ONCE via read_url on
   https://r.jina.ai/<url> — a third-party reader that renders the page;
   never send it a URL containing tokens or other secrets.
   When researching, batch independent lookups: issue several web_search /
   read_url calls in a single reply — they run in parallel, which is much
   faster than one per turn.{scratch_note}
"""

# Per-session scratch workspace (issue #70). Injected only when a path is
# known, so the static prompt stays byte-identical for callers that render it
# without one. Imperative phrasing on purpose — small local models ignore
# capability-style hints (the "prompt hints must be imperative" convention).
SCRATCH_RULE = """
8. SCRATCH WORKSPACE: {scratch_dir} is your OWN private scratch directory. You
   MUST use it for throwaway files — staging a gh issue or PR body, a commit
   message, an intermediate patch or artifact — instead of writing them into
   the project tree. Creating, editing, AND deleting files inside that
   directory is AUTO-APPROVED (no prompt); the whole directory is deleted
   automatically when the session ends, so never leave anything there you need
   to keep. Writing or deleting ANYWHERE ELSE still requires user approval
   exactly as above — the auto-approval applies ONLY inside this directory."""

DENIED_RESULT = (
    "USER DENIED this command — it was NOT executed. "
    "Do not propose it again; change approach or ask the user."
)

CD_NOT_STICKY = (
    "cd was NOT run: every command executes in the project directory ({cwd}) "
    "— a bare cd does not persist. To run something elsewhere, chain it in "
    "ONE command: cd <dir> && <command> (the directory reverts when the "
    "command ends). Only the user can move the project directory (/cd)."
)

EMPTY_RESPONSE = (
    "(the model returned an empty response — the backend may be overloaded or "
    "still loading; try again)"
)


class ModelUnavailable(RuntimeError):
    """The model call failed after a retry (backend down, overloaded, or OOM)."""


class TaskCancelled(Exception):
    """Raised inside the loop when cancel() interrupts a streaming turn."""


CANCELLED_RESULT = "(task stopped by user — any partial work is above)"
NOT_EXECUTED = "(not executed — the user stopped the task)"

# Loop detection: the exact same tool call returning the exact same output is
# not progress. At WARN repeats the model gets one nudge to change approach;
# at STOP repeats the task ends with a diagnostic wrap-up instead of burning
# the remaining step budget. Legitimate polling (tail on a growing log,
# job-status checks) has changing output, so it never trips this.
LOOP_WARN_REPEATS = 3
LOOP_STOP_REPEATS = 5

# Progress-gated step budget (issue #108). A flat step cap has the wrong shape:
# it kills a task that is still doing useful work while letting a stalled one
# burn the whole budget. Instead the loop measures PROGRESS deterministically —
# a step is progress when at least one of its tool calls yields a
# (tool, args, result) tuple seen for the FIRST time (reusing the `repeats` dict
# the loop detector already maintains; no extra model call, no wall-clock timer).
# A steadily-progressing task may run PAST `max_steps` up to the hard ceiling; a
# task that produces no new result for MAX_STALL_STEPS consecutive steps has
# stalled and stops early. The ceiling is the unconditional cost cap that NOTHING
# exceeds — it derives from `max_steps` (which stays the base budget), so raising
# --max-steps raises the cap while the module floor keeps a sane minimum.
MAX_STALL_STEPS = 8
HARD_STEP_CEILING = 60  # effective cap = max(self.max_steps, HARD_STEP_CEILING)

# Skill-read gate (issue #40): while a preloaded-but-truncated skill is
# unread, other tool calls are refused. Must stay < LOOP_WARN_REPEATS — an
# identical refused call repeats at most GATE_MAX_REFUSALS times before the
# gate lifts and its result changes, so the loop detector never fires on the
# gate itself.
GATE_MAX_REFUSALS = 2

SKILL_GATE_REFUSAL = (
    "NOT EXECUTED — required reading first: the preloaded skill(s) {names} "
    "are truncated in your context. Call read_skill({first!r}) to load the "
    "full playbook, or state explicitly why it does not apply and retry — "
    "the call will then proceed."
)

# Stop gate (issue #81): deny + comment means STOP — the system prompt and the
# feedback note ORDER the model to address the concern in plain text and halt,
# but eager models (Gemini, small local ones) run another tool first anyway.
# This is the hard backstop — while a denial's concern is unaddressed every tool
# call is refused, so feedback is never silently folded into another command. A
# text-only reply lifts it (and ends the task); the step budget bounds a model
# that never replies. Approvals never arm this: they mean continue.
STOP_GATE_REFUSAL = (
    "NOT EXECUTED — the user DENIED your last action with a concern you have "
    "not addressed. Denial means STOP: your NEXT turn must be TEXT ONLY, with "
    "NO tool call — address the user's concern and wait for them. Do not retry "
    "a variant or run anything else."
)

LOOP_WARNING = (
    "[aish: you have issued this exact tool call {count} times and received "
    "identical output every time — repeating it cannot make progress. Change "
    "your approach; if you have no other approach, stop and explain what is "
    "blocking you.]"
)

STEP_LIMIT_NOTE = (
    "[aish: you have reached the step limit for this task, so no more tool "
    "calls are possible. Assess your work and reply with TEXT ONLY: if the "
    "task is complete, give the final answer now. Otherwise state clearly "
    "(1) what was accomplished, (2) what remains, and (3) the next concrete "
    "step — the user can ask you to continue.]"
)

LOOP_STOP_NOTE = (
    "[aish: stopping this task — the same tool call kept returning identical "
    "output even after a warning, so you are running in circles. Reply with "
    "TEXT ONLY: summarize what you tried, what failed and why you appear "
    "stuck, and what would be needed to make progress.]"
)

STALL_NOTE = (
    "[aish: stopping this task — your recent tool calls stopped producing any "
    "new results (no progress for several steps), so you appear stuck. Reply "
    "with TEXT ONLY: summarize what you accomplished, what remains, and what is "
    "blocking further progress — the user can redirect you or say 'continue'.]"
)

STOPPED_LIMIT = (
    "(stopped: hit the max-steps limit — say 'continue' to keep going, or "
    "raise --max-steps)"
)
STOPPED_STALL = (
    "(stopped: no new progress for several steps — say 'continue' with a hint, "
    "or raise --max-steps)"
)
STOPPED_LOOP = "(stopped: repeating the same tool call with no progress)"
NOT_EXECUTED_LIMIT = "(not executed — the step limit was reached)"


WRITE_DENIED = (
    "USER DENIED this file change — nothing was written. "
    "Do not retry the same change; adjust it or ask the user what they want."
)

# Deny + comment = STOP. The denied action did not run; the model must address
# the user's concern in plain text and then halt (the stop gate blocks tools
# until a text-only turn, which ends the task). Small local models ignore soft
# phrasing (the "Prompt hints must be imperative" convention), so the note
# ORDERS it — MUST + a worked example.
FEEDBACK_NOTE = (
    '\n\n[The user DENIED this and left a COMMENT: "{comment}"\n'
    "Denial means STOP. Your NEXT reply MUST be plain text with NO tool call: "
    "address the user's concern, then wait for them. Do NOT retry a variant or "
    'run anything else first. Example — comment "this could delete real data" → '
    'reply "You\'re right, that would touch real files — I\'ve stopped. Here is '
    'what I would do instead…" and stop.]'
)

# Approve + comment = CONTINUE, but adjust. The original action was HELD (not
# run); the model must adjust it to what the user asked and re-propose, and the
# adjusted action is approved again before it runs — the task keeps going.
HELD_FOR_ADJUSTMENT = (
    'NOT RUN — the user APPROVED this command but attached a COMMENT: "{comment}"\n'
    "Approval means CONTINUE, so proceed — but the original command was NOT run. "
    "Adjust it to what the user asked and propose the ADJUSTED command; it will "
    "be shown for approval again before it runs. Do NOT re-run the original "
    "unchanged."
)

WRITE_HELD_FOR_ADJUSTMENT = (
    'NOT WRITTEN — the user APPROVED this change but attached a COMMENT: "{comment}"\n'
    "Approval means CONTINUE, so proceed — but nothing was written. Adjust the "
    "change to what the user asked and propose the ADJUSTED write; it will be "
    "shown for approval again before it lands. Do NOT re-apply the original "
    "unchanged."
)

TOOL_HELD_FOR_ADJUSTMENT = (
    'NOT RUN — the user APPROVED calling {name} but attached a COMMENT: "{comment}"\n'
    "Approval means CONTINUE, so proceed — but the tool was NOT run. Rework the "
    "arguments to what the user asked and call {name} again; it will be shown "
    "for approval again before it runs. Do NOT re-run the original args unchanged."
)


def _with_feedback(base: str, comment: str) -> str:
    return base + FEEDBACK_NOTE.format(comment=comment) if comment else base


def _display_path(path: Path) -> str:
    """A path with $HOME abbreviated to ~ — so a global-config destination
    reads clearly as ~/.config/aish/… rather than a bare absolute path."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


_EXIT_CODE_RE = re.compile(r"\[exit code: (-?\d+)\]\s*$")
_JOB_PID_RE = re.compile(r"pid (\d+)")


def _parse_exit_code(result: str) -> int | None:
    """The trailing exit code tools.run_command appends, or None when the
    command never started (a bare 'ERROR: failed to start …')."""
    match = _EXIT_CODE_RE.search(result)
    return int(match.group(1)) if match else None


def _parse_job_id(result: str) -> str:
    """The pid from a background/detach handle message, for the block label."""
    match = _JOB_PID_RE.search(result)
    return match.group(1) if match else ""

READ_DENIED = (
    "USER DENIED reading this sensitive file — its contents were NOT read. "
    "Do not retry; proceed without it or ask the user."
)

BLOCKED_RESULT = (
    "BLOCKED by the safety denylist ({reason}) — NOT executed, and it cannot "
    "be approved through you at all. If the user truly intends this, they must "
    "run it themselves with the ! prefix. Propose a safer alternative if one exists."
)

# The per-task nudge that makes small local models actually consult skills:
# recency is what they obey, so the reminder is (re)inserted directly before
# each user message instead of relying on the system prompt alone. It is
# appended to self.messages directly (never via _append) so it stays out of
# the session log and the web transcript, and the previous task's copy is
# removed first so exactly one exists in history.
TASK_REMINDER_MARK = "<system-reminder>"
TASK_REMINDER = (
    "<system-reminder>Before acting: scan the Skills index in your system "
    "prompt. If a skill matches this task, your FIRST action MUST be "
    "read_skill(<name>) — do not improvise the task from your training "
    "data. Skills (and the saved Memory facts in your context) override "
    "what you think you know.</system-reminder>"
)

# When pre-flight retrieval finds matching knowledge (skills.preflight), the
# reminder slot carries the content itself instead of a nudge to go look for
# it. Shares TASK_REMINDER_MARK so the strip-previous logic treats both alike.
PRELOAD_REMINDER = (
    "<system-reminder>Saved knowledge relevant to this task, preloaded for "
    "you — follow it over your training data:\n\n{knowledge}\n\n"
    "If a block above is marked TRUNCATED you MUST read_skill it in full, "
    "or state why it does not apply, before doing anything else. Also scan "
    "the Skills index in your system prompt for other "
    "matches.</system-reminder>"
)


def task_reminder(index: str, preload_text: str = "") -> str:
    """The per-task system reminder: always the current local time (issue #36
    — it lives here, not in the system prompt, so messages[0] stays
    byte-stable for prompt caching and the time is fresh every task), plus
    the preloaded knowledge when pre-flight retrieval found any (issue #40),
    else the skills nudge whenever any skills/memory are advertised."""
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    time_note = f"{TASK_REMINDER_MARK}Current local time: {now}</system-reminder>"
    if preload_text:
        return f"{time_note}\n{PRELOAD_REMINDER.format(knowledge=preload_text)}"
    return f"{time_note}\n{TASK_REMINDER}" if index else time_note


# /learn — the user-triggered distillation pass. Runs as a normal task, so
# recall/read/diff-approval all apply; shared by the CLI and the web server.
LEARN_PROMPT = (
    "Review this conversation for durable learnings{hint}. For each one: "
    "call recall first to check for an existing skill or memory entry — if "
    "one exists, UPDATE it (edit_file: append the gotcha or correct it) "
    "instead of creating a duplicate. If recall surfaces stale or duplicate "
    "memory, consolidate it: remember() the one canonical fact, then "
    "forget_memory() each redundant slug. Save multi-step procedures as skills — "
    "a markdown file in ~/.config/aish/skills/ (or ./.aish/skills/ when "
    "project-specific) with a trigger-phrased description ('Use when the "
    "user asks to …'); save one-line facts and preferences with remember(). "
    "Entries are retrieved by matching their name/description/keywords "
    "against future tasks: phrase every description like the tasks it must "
    "catch (the activity and its task shapes, generalized — no item-by-item "
    "lists; the rule after the trigger), and give keywords — topical nouns "
    "and synonyms, no generic verbs — in every language the user types. If "
    "this conversation shows saved knowledge that failed to trigger when it "
    "should have, repair that entry's description/keywords too. "
    "Then report what you saved and what you skipped and why. If nothing is "
    "worth saving, say so plainly."
)

LEARN_LESSONS_PROMPT = (
    "Migrate the legacy lessons file into structured knowledge — a conscious "
    "review, not a mechanical copy. Read {path}, group related lines, and "
    "flag obsolete ones to drop. For each keeper: recall first and UPDATE an "
    "existing entry if one matches; otherwise save procedure-shaped lessons "
    "as skills (trigger-phrased description) and fact-shaped ones with "
    "remember(). Then list what was migrated and what was dropped, and ask "
    "the user to confirm; once they confirm coverage, rename the file to "
    "lessons.md.bak with a shell command so it stops being loaded."
)


def learn_prompt(hint: str, lessons_path=None) -> str:
    if hint.strip().casefold() == "lessons" and lessons_path:
        return LEARN_LESSONS_PROMPT.format(path=lessons_path)
    clause = f", with attention to: {hint.strip()}" if hint.strip() else ""
    return LEARN_PROMPT.format(hint=clause)


# /feedback (CLI and web) expands to one of these so both entry points share
# the flow. It leans on the `gh_issue` skill for repo context rather than a
# permanent feedback skill, so nothing is added to the always-on skill index.
#
# Two flavours (issue #110):
#  - BLOCK flow (web, text-only feedback): the model emits the finished issue as
#    ONE `aish-issue` fenced block and does NOT run `gh issue create`. The block
#    is the single source of truth — the frontend renders it as a review card and
#    the backend creates it verbatim on the user's confirm (a user-direct action,
#    no approval gate, repo pinned). This drops the redundant second prompt.
#  - CLASSIC flow (CLI, and web feedback that carries attachments): the model
#    drafts the issue and runs `gh issue create` itself through the approval gate,
#    because it also has to upload the attached assets — a step the text-only
#    backend path does not handle. The approval gate is the safety boundary here.
#
# Attachments (#130): assets are published to a PUBLIC GitHub release, so
# consent is explicit — the draft lists every attached file with a per-file
# exclude chip, and only the files still listed when the user approves are
# uploaded (FEEDBACK_ASSETS_RULES). A block-flow feedback that gains
# attachments while the draft is being adjusted auto-switches to the classic
# flow via FEEDBACK_SWITCH_NOTE (appended server-side, model-only).
FEEDBACK_INTRO = (
    "The user wants to send feedback about aish — a bug report, a feature "
    "request, or an improvement idea — that will become a GitHub issue on the "
    "`epnasis/aish` repository (checked out at /Users/epnasis/dev/aish). "
    "You MUST follow this flow:\n"
    "1. Read the `gh_issue` skill (read_skill) for repo context and labels.\n"
    "2. If the request is unclear or thin, ask focused clarifying questions "
    "FIRST — one short round, not an interrogation. If they already described "
    "it{clause}, go straight to a draft.\n"
)
FEEDBACK_BLOCK_PROMPT = FEEDBACK_INTRO + (
    "3. Emit the finished issue as EXACTLY ONE fenced block, and nothing that "
    "duplicates it (no separate rendered copy, no `gh issue create`, no "
    "quick-reply chips). The block is the ONLY thing the user reviews and the "
    "EXACT text that gets filed:\n"
    "```aish-issue\n"
    "title: A concise one-line title\n"
    "---\n"
    "Body markdown here.\n"
    "Multiple lines, sections, a suggested label, etc.\n"
    "```\n"
    "The FIRST line inside the block MUST be `title: <one-line title>`; the `---` "
    "separator line is optional; everything after it is the issue body, verbatim.\n"
    "4. Do NOT run `gh issue create` — the user files it with one tap and aish "
    "creates it for them. Stop after the block; do not add chips or a trailing "
    "question."
)
FEEDBACK_CLASSIC_PROMPT = FEEDBACK_INTRO + (
    "3. Present the draft issue as ordinary rendered markdown — a bold title "
    "line and a structured body. Do NOT wrap the draft in a code block; the "
    "user reads it rendered.\n"
    "4. End that same message with exactly these two quick-reply chips, each on "
    "its own line:\n"
    "[Create the issue](aish-reply://Create the issue)\n"
    "[Edit — change something](aish-reply://I'd like to change the draft: )\n"
    "5. Run `gh issue create` ONLY after the user approves. Then show the new "
    "issue's URL."
)

# Consent for feedback attachments (#130): issue assets land on a PUBLIC GitHub
# release, so nothing is uploaded silently — the draft itself lists every
# detected file with a per-file exclude chip, and only what survives review is
# uploaded. Appended to the classic prompt when the feedback carries
# attachments, and embedded in the block→classic switch note.
FEEDBACK_ASSETS_RULES = (
    "Attachment rules — issue assets are uploaded to a PUBLIC GitHub release, "
    "so the user must see and confirm exactly what gets published:\n"
    "- The draft MUST end with an **Attachments** section listing every "
    "attached file (including any the user attaches in later turns while "
    "adjusting the draft), one per line, each with its own exclude chip:\n"
    "[Exclude <name>](aish-reply://Exclude <name> from the issue)\n"
    "- If the user excludes a file, re-present the draft without it; an "
    "excluded file is NEVER uploaded.\n"
    "- Upload ONLY the files still listed when the user approves the draft, "
    "per the `gh_issue` skill's asset workflow, and link them in the issue "
    "body."
)

# Auto-switch (#130): a text-only feedback (block flow) that gains attachments
# while the draft is being adjusted moves to the upload-capable classic flow —
# the aish-issue block cannot carry assets. The server appends this to the
# follow-up turn's text (model-only; the user's echo stays clean) and withdraws
# the stashed block draft at the same time.
FEEDBACK_SWITCH_NOTE = (
    "\n\n[The user attached files to this feedback. The aish-issue block flow "
    "cannot upload them, so the block draft is WITHDRAWN — SWITCH to the "
    "classic flow NOW and do not emit an aish-issue block again:\n"
    "- Re-present the updated draft as ordinary rendered markdown — a bold "
    "title line and a structured body, NOT in a code block.\n"
    "- End that same message with exactly these two quick-reply chips, each on "
    "its own line:\n"
    "[Create the issue](aish-reply://Create the issue)\n"
    "[Edit — change something](aish-reply://I'd like to change the draft: )\n"
    "- Run `gh issue create` ONLY after the user approves, then show the new "
    "issue's URL.\n" + FEEDBACK_ASSETS_RULES + "]"
)


def feedback_prompt(hint: str = "", block_flow: bool = False, attachments: bool = False) -> str:
    """The /feedback expansion. block_flow=True selects the web text-only path
    (emit an `aish-issue` block, backend files it); the default classic path has
    the model run `gh issue create` through the approval gate (CLI, or web
    feedback with attachments that need the asset-upload workflow). attachments
    appends the public-upload consent rules (#130): list the assets in the
    draft with per-file exclude chips, upload only what survives review."""
    hint = hint.strip()
    clause = f" (their words: {hint})" if hint else ""
    template = FEEDBACK_BLOCK_PROMPT if block_flow else FEEDBACK_CLASSIC_PROMPT
    prompt = template.format(clause=clause)
    if attachments and not block_flow:
        prompt += (
            "\nThe user attached logs, screenshots, or files — incorporate "
            "them into the issue.\n" + FEEDBACK_ASSETS_RULES
        )
    return prompt


# No side effects and no approval prompt — safe to run concurrently.
READ_ONLY_TOOLS = frozenset(
    {"read_docs", "read_skill", "web_search", "read_url", "read_file", "recall"}
)

def format_secs(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def format_tokens(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def _usage(response: Any) -> tuple[int, int]:
    """(prompt tokens, completion tokens) as Ollama reports them; zeros when
    absent. Note prompt_eval_count skips KV-cache-reused prefix tokens."""
    return (
        getattr(response, "prompt_eval_count", 0) or 0,
        getattr(response, "eval_count", 0) or 0,
    )


def _tokens_note(usage: tuple[int, int]) -> str:
    """' · ↑ 3.2k ↓ 96 tokens' — ↑ fed to the model, ↓ generated by it."""
    tokens_in, tokens_out = usage
    if not (tokens_in or tokens_out):
        return ""
    return f" · ↑ {format_tokens(tokens_in)} ↓ {format_tokens(tokens_out)} tokens"


class _NoStatus:
    """Default live-status sink: aish shows a ticking timer only when the CLI
    injects one (TTY); everywhere else these are no-ops."""

    def start(self, label: str) -> None:
        pass

    def add_tokens(self, count: int) -> None:
        pass

    def stop(self) -> None:
        pass

TRIM_KEEP_CHARS = 200
TRIMMED_NOTE = "\n[trimmed: full output dropped to save context]"
# Rough tokens→chars margin: ~4 chars/token, keep well under num_ctx so the
# system prompt is never silently evicted by Ollama's own truncation.
CHARS_PER_TOKEN_BUDGET = 3
# Command output carried in an activity-trace step is a preview (the trace
# collapses it); the full result still reaches the model and streams live.
STEP_OUTPUT_CAP = 8000


def system_prompt(scratch_dir: os.PathLike | str | None = None) -> str:
    note = _PLATFORM_NOTES.get(sys.platform, f"{sys.platform} (verify userland conventions).")
    scratch_note = SCRATCH_RULE.format(scratch_dir=scratch_dir) if scratch_dir else ""
    return SYSTEM_PROMPT_TEMPLATE.format(platform_note=note, scratch_note=scratch_note)


def compose_system_content(
    base_context: str,
    cwd: str,
    lessons_path=None,
    index: str | None = None,
    scratch_dir: os.PathLike | str | None = None,
) -> str:
    """The full system message: static rules + caller context + the live
    skills/memory index. Rebuilt at every run_task so entries created
    mid-session (or after /cd) are advertised without a restart.
    Deterministic: unchanged inputs yield a byte-identical string (the scratch
    path is stable for a session's life), keeping API prompt caches valid."""
    if index is None:
        index = skills.knowledge_index(cwd, lessons_path)
    content = system_prompt(scratch_dir) + (f"\n{base_context}" if base_context else "")
    return content + (f"\n\n{index}" if index else "")


def environment_context(cwd: str) -> str:
    if sys.platform == "darwin":
        os_desc = f"macOS {platform.mac_ver()[0]}"
    else:
        os_desc = platform.platform(terse=True)
    return (
        "Environment:\n"
        f"- session started: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}"
        " (current time arrives with each task)\n"
        f"- project directory (all commands run here): {cwd}\n"
        f"- user: {getpass.getuser()}\n"
        f"- OS: {os_desc} ({platform.machine()})"
    )


def _remove_scratch(path: Path) -> None:
    """Delete the per-session scratch workspace, ignoring errors — cleanup is
    best-effort and must never raise from a finalizer/close()."""
    shutil.rmtree(path, ignore_errors=True)


def _serialize(message: dict) -> dict:
    keys = ("role", "content", "tool_name", "images", "documents")
    return {k: message[k] for k in keys if k in message}


class Agent:
    def __init__(
        self,
        model: str,
        approve: Callable[[str], Any],
        approve_write: Callable[[Any], Any] = lambda _plan: False,  # bool, Approved or Denied
        approve_read: Callable[[str, str], bool] = lambda _path, _reason: True,
        echo: Callable[[str], None] = lambda _: None,
        stream: Callable[[str], None] | None = None,
        client_chat: Callable[..., Any] = ollama.chat,
        num_ctx: int = 32768,
        max_steps: int = 25,
        think: bool = False,
        cwd: str | None = None,
        context: str = "",
        on_message: Callable[[dict], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        job_log_dir: os.PathLike | str | None = None,
        lessons_path: os.PathLike | str | None = None,
        status: Any = None,
        state_dir: os.PathLike | str | None = None,
        current_session: Callable[[], Path] | None = None,
        semantic: Any = None,
        on_step: Callable[[dict], None] | None = None,
        on_command_start: Callable[[dict], None] | None = None,
        on_command_end: Callable[[dict], None] | None = None,
        step_log: Callable[[dict], None] | None = None,
        command_log: Callable[[dict], None] | None = None,
        state_log: Callable[[dict], None] | None = None,
        on_state: Callable[[dict], None] | None = None,
        check_pending_cwd: Callable[[], str | None] | None = None,
        check_pending_messages: Callable[[], list[str]] | None = None,
        aliases: Mapping[str, str] | None = None,
        approve_tool: Callable[[str, dict], Any] | None = None,
        approve_import: Callable[..., Any] | None = None,
    ):
        self.model = model
        self.provider = "ollama"  # callers overwrite after construction (cli/server)
        self.task_sources: list[dict] = []  # pages read_url fetched for the current task
        self.approve = approve
        self.approve_write = approve_write
        self.approve_read = approve_read
        self.approve_tool = approve_tool
        self.approve_import = approve_import
        self.echo = echo
        self.stream = stream
        self.chat = client_chat
        # aish-owned command aliases, expanded on the first word BEFORE the
        # approval gate (see aliases.py and expand_alias). Sanitized so a
        # malformed config entry can never make a command un-runnable.
        self.aliases: dict[str, str] = alias_map.sanitize(aliases or {})
        self.num_ctx = num_ctx
        self.max_steps = max_steps
        self.think = think
        self.cwd = cwd or os.getcwd()
        # Session roots: auto-approved reads/commands are confined to these
        # trees. Seeded with the launch dir; they only widen on an explicit
        # user decision — /cd, /add-dir, or "trust this directory" answered on
        # an approval prompt. Execution is stateless for the model: cwd moves
        # only on user action (/cd, !cd) — a model-issued bare cd never runs.
        self.roots: list[Path] = [Path(self.cwd).resolve()]
        self.on_message = on_message
        self.on_token = on_token
        self.job_log_dir = job_log_dir
        self.lessons_path = lessons_path
        # Session store for the search_sessions tool; current_session is
        # excluded from ranking (its content is already this conversation).
        self.state_dir = state_dir
        self.current_session = current_session
        # Embedding-based preflight selection (issue #43); opt-in from the
        # entry points so tests and bare Agents stay network-free.
        self.semantic = semantic
        self._semantic_warned = False
        self.status = status if status is not None else _NoStatus()
        # Structured activity-trace steps for a rich client (the web UI). When
        # wired, tool/thinking/knowledge progress flows through here as typed
        # events; the terminal keeps its flat echo lines (see _note). Extra
        # run_command detail (command, decision, output) is stashed here by the
        # dispatch branch and read back when the completion step is emitted.
        self.on_step = on_step
        # Terminal-block framing for a rich client (the web UI): command_start
        # carries cwd + the (possibly edited) command, command_end the exit
        # code (or a detached/interrupted label). Both are recorded so a
        # session replay reconstructs the bounded block identically. Unused by
        # the terminal, which streams output inline.
        self.on_command_start = on_command_start
        self.on_command_end = on_command_end
        # Persistence sink for the same trace steps, orthogonal to rendering:
        # both entry points wire this to the session log so the activity trace
        # survives eviction/restart and is reconstructable in any UI. The CLI
        # sets step_log WITHOUT on_step, so its terminal chatter (see _note)
        # stays while its steps are still logged for later web replay/analysis.
        self.step_log = step_log
        # Persistence sink for the terminal-block framing events, so a
        # cold-loaded session reconstructs the SAME command_start/command_end
        # event stream a live one emits — byte-identical panel, not a fallback.
        # The command's output is not duplicated here; it rides on the `tool`
        # trace step, and reconstruct_events splices it back in as one stream.
        self.command_log = command_log
        # Workspace-change sinks (issue #94), parallel to step_log/on_step:
        # state_log persists a cwd move / dir trust as a `kind:"cwd"` /
        # `kind:"trust_dir"` record so resume/cold-open can restore the
        # workspace; on_state surfaces the same change live in the web timeline.
        # reconstruct_events replays those records into the identical event.
        self.state_log = state_log
        self.on_state = on_state
        # Between-steps steering hooks (issue #95), polled at the top of every
        # run_task loop iteration so a long task stays responsive without being
        # aborted: check_pending_cwd applies a /cd the UI queued while busy
        # (moves cwd + rebuilds the system prompt for the new dir);
        # check_pending_messages injects text the user typed mid-task so the
        # next model turn pivots. Both are thread-safe get/drain callbacks (the
        # server sets from the event loop, the worker thread consumes here).
        self.check_pending_cwd = check_pending_cwd
        self.check_pending_messages = check_pending_messages
        self._run_meta: dict | None = None
        self._cancel = threading.Event()
        # Plugin tools (TOOL.md), rebuilt only when the tool dirs' signature
        # moves — a mid-task manifest edit is picked up on the next step.
        # Read-only tools are always exposed; mutating ones only when a tool
        # approver is wired (fail-closed otherwise — never run ungated).
        self._plugin_sig: tuple | None = None
        self._plugin_tools: dict[str, tool_plugins.Tool] = {}
        self._plugin_defs: list[dict] = []
        self._plugin_warned: set[str] = set()
        # (wraps-prefix, tool-name) for exposed tools that declare `wraps:` —
        # lets the agent nudge the model toward a tool when it runs the raw
        # command the tool replaces (drift detection, issue #140).
        self._tool_wraps: list[tuple[str, str]] = []
        # Skill-read gate state: oversized preloaded skills the model must
        # read_skill (or explicitly waive) before other tools run; values are
        # refusals left before the gate auto-lifts. Rebuilt every run_task.
        self._pending_skill_reads: dict[str, int] = {}
        # Stop gate (issue #81): armed when a DENIAL carries a concern, cleared
        # by the main loop only on a text-only turn (deny means stop). While
        # armed, _stop_gate refuses every tool call.
        self._pending_comment_response = False
        self.base_context = context
        # Per-session scratch workspace (issue #70): a private temp dir where
        # the model may create AND delete throwaway files without prompting.
        # Resolved so it matches operand realpaths on macOS (/var → /private).
        # A weakref.finalize cleans it up when the Agent is dropped or at
        # interpreter exit; server sessions also close() it on eviction.
        self.scratch_dir = Path(tempfile.mkdtemp(prefix="aish-scratch-")).resolve()
        self._scratch_finalizer = weakref.finalize(
            self, _remove_scratch, self.scratch_dir
        )
        content = compose_system_content(
            context, self.cwd, self.lessons_path, scratch_dir=self.scratch_dir
        )
        self.messages: list[dict] = [{"role": "system", "content": content}]

    def close(self) -> None:
        """Best-effort scratch-workspace cleanup. Idempotent; also runs
        automatically when the Agent is garbage-collected or the interpreter
        exits (weakref.finalize)."""
        self._scratch_finalizer()

    def cancel(self) -> None:
        """Stop the running task at the next boundary: mid-stream (the token
        loop), before the next model call, before executing proposed tool
        calls, or by terminating the running shell command. Thread-safe —
        called from the server loop while run_task holds a worker thread."""
        self._cancel.set()

    def reset(self) -> None:
        """Drop the conversation, keep the system prompt."""
        del self.messages[1:]

    def load_history(self, messages: list[dict]) -> None:
        """Adopt messages from a previous session (already logged — appended
        directly so they are not re-recorded)."""
        self.messages.extend(m for m in messages if m.get("role") != "system")

    def rewind_last_task(self) -> str | None:
        """Undo the most recent user turn: drop that user message and everything
        the assistant produced after it (text, tool_calls, tool results), plus
        the TASK_REMINDER that preceded it. Web retry (#60) calls this so a rerun
        regenerates from a clean context — the model never sees its discarded
        answer (run_task re-adds the prompt and reminder fresh). Returns the
        removed user text, or None when there is no user turn to undo."""
        for i in range(len(self.messages) - 1, 0, -1):
            if self.messages[i].get("role") == "user":
                text = self.messages[i].get("content")
                cut = i
                prev = self.messages[cut - 1]
                if prev.get("role") == "system" and str(
                    prev.get("content", "")
                ).startswith(TASK_REMINDER_MARK):
                    cut -= 1
                del self.messages[cut:]
                return text if isinstance(text, str) else None
        return None

    def _append(self, message: dict) -> None:
        self.messages.append(message)
        if self.on_message:
            self.on_message(_serialize(message))

    def _note(self, text: str) -> None:
        """Terminal progress chatter (✓ ran X, → read Y, ✓ thought for …).
        A rich client gets the same information as structured `on_step` events
        and renders its own activity trace, so this is suppressed there to
        avoid showing every line twice."""
        if self.on_step is None:
            self.echo(text)

    def _sink_step(self, step: dict) -> None:
        """Single delivery point for every structured trace step: persist it
        (so any UI can reconstruct the trace later) and hand it to the rich
        renderer if one is attached. Kept separate from on_step so the two
        concerns — durable logging vs live rendering — stay independent."""
        if self.step_log is not None:
            self.step_log(step)
        if self.on_step is not None:
            self.on_step(step)

    def _emit_command_start(self, command: str, user: bool = False) -> None:
        # `user` marks a command the user typed directly (! prefix): the web UI
        # renders it as a standalone terminal block in the transcript, not
        # nested in the model's activity trace.
        event: dict = {"cwd": self.cwd, "command": command}
        if user:
            event["user"] = True
        if self.command_log is not None:
            self.command_log({"kind": "cmd_start", **event})
        if self.on_command_start is not None:
            self.on_command_start(event)

    def _emit_command_end(self, **payload: Any) -> None:
        if self.command_log is not None:
            self.command_log({"kind": "cmd_end", **payload})
        if self.on_command_end is not None:
            self.on_command_end(payload)

    def _emit_step(self, **step: Any) -> None:
        self._sink_step(step)

    def _emit_workspace(self, change: str, path: str) -> None:
        """Persist a user-driven workspace change (cwd move / dir trust) and
        surface it live in the timeline — parallel to _sink_step: durable log
        vs live render. The persisted record and the live event carry the same
        data reconstruct_events replays, so cold and hot timelines match."""
        record_kind = "cwd" if change == "cwd" else "trust_dir"
        field = "cwd" if change == "cwd" else "path"
        if self.state_log is not None:
            self.state_log({"kind": record_kind, field: path})
        if self.on_state is not None:
            self.on_state({"change": change, "path": path})

    def _sync_cwd_in_context(self) -> None:
        """Keep the system prompt's 'project directory' line current after a cwd
        move, so the model reads its cwd from the (per-task / mid-task rebuilt)
        system prompt — no disruptive conversation turn needed. Only that line
        changes; the fixed session-start timestamp stays put, so an unchanged cwd
        still yields a byte-identical prompt (prompt-cache friendly)."""
        if not self.base_context:
            return
        self.base_context = re.sub(
            r"(- project directory \(all commands run here\): ).*",
            lambda m: m.group(1) + self.cwd,
            self.base_context,
            count=1,
        )

    def restore_workspace(self, cwd: str | None, trusted: list[str]) -> None:
        """Reapply a session's persisted cwd + trusted dirs on resume/cold-open,
        setting state DIRECTLY (never through rebase/trust_root) so restoring
        emits no fresh cwd/trust record — that would be a replay feedback loop.
        Missing paths degrade gracefully: a vanished cwd keeps the default, a
        vanished trusted dir is skipped."""
        if cwd and os.path.isdir(cwd):
            self.cwd = cwd
            self.roots[0] = Path(cwd).resolve()
            self._sync_cwd_in_context()  # restored cwd shows in the system prompt too
        for path in trusted:
            resolved = Path(path).resolve()
            if resolved.is_dir() and not any(
                resolved.is_relative_to(root) for root in self.roots
            ):
                self.roots.append(resolved)

    def _apply_pending_cwd(self) -> None:
        """Apply a /cd the UI queued while this task runs (issue #95), between
        steps instead of only after the whole task. rebase() moves cwd,
        re-anchors roots[0], logs the #94 cwd record and fires on_state (the web
        server turns that single signal into the top-bar chip + queue-card
        update — one path for mid-task, immediate, and post-task moves alike).
        Then the system prompt is rebuilt for the new directory — same helper
        run_task uses at entry — so the new dir's environment context and
        preloaded skills apply to every following step. Not a tool call, so it
        never touches the #81 gates or the loop-detection counters."""
        if self.check_pending_cwd is None:
            return
        target = self.check_pending_cwd()
        if not target:
            return
        # announce=False: mid-task, do NOT inject a user-turn note — the model
        # would treat it as a new prompt and abandon the running task. The cwd
        # still moves (commands run in the new dir) and the skills index below is
        # rebuilt for it; the model just isn't disruptively interrupted (#95).
        result = self.rebase(target, announce=False)
        if result.startswith("ERROR"):
            return  # a vanished/invalid dir: rebase already reported it
        self.messages[0]["content"] = compose_system_content(
            self.base_context, self.cwd, self.lessons_path, scratch_dir=self.scratch_dir
        )

    def _inject_pending_messages(self) -> None:
        """Fold in text the user typed while this task runs (issue #95): instead
        of deferring it to a separate follow-up task, each queued message is
        injected as a user turn mid-task so the very NEXT model call pivots —
        steering, not a reset. Surfaced as a distinct `injected` trace step,
        which renders live AND is replayed identically by reconstruct_events
        (kept inside the open turn, so cold and hot timelines match). The text is
        appended straight to self.messages — NOT via _append — so it logs no
        conversation `message` record, which reconstruct would otherwise replay
        as a turn-splitting second user bubble. Trade-off: the steering text is
        therefore not carried into --resume history (it shaped the answer, which
        is). Not a tool call — leaves the gates and loop counters untouched."""
        if self.check_pending_messages is None:
            return
        for msg in self.check_pending_messages():
            if not msg:
                continue
            # No echo line — the `injected` step ("You added" note) is the sole,
            # clean timeline marker for this (#95); a grey echo would duplicate it.
            self._emit_step(kind="injected", text=msg)
            self.messages.append({"role": "user", "content": msg})

    def run_task(
        self,
        task: str,
        images: list[str] | None = None,
        documents: list[str] | None = None,
    ) -> str:
        # Fresh scan every task: skills/memory created mid-session (or after
        # /cd) show up immediately, in every open session — no restart needed.
        index = skills.knowledge_index(self.cwd, self.lessons_path)
        self.messages[0]["content"] = compose_system_content(
            self.base_context, self.cwd, self.lessons_path, index, scratch_dir=self.scratch_dir
        )
        self.messages[1:] = [
            m
            for m in self.messages[1:]
            if not (
                m.get("role") == "system"
                and str(m.get("content", "")).startswith(TASK_REMINDER_MARK)
            )
        ]

        # Old tasks' raw tool outputs are rarely needed verbatim again;
        # shrinking them keeps long REPL sessions inside the context window.
        task_start = len(self.messages)
        for message in self.messages[1:task_start]:
            self._trim_tool_message(message)

        # Media rides on the user message as file paths; each backend encodes
        # them for its API (ollama `images`, data URLs, Anthropic blocks).
        user_message: dict = {"role": "user", "content": task}
        if images:
            user_message["images"] = list(images)
        if documents:
            user_message["documents"] = list(documents)
# Pre-flight retrieval (issue #40): inject matching knowledge bodies
        # directly instead of hoping the model calls recall/read_skill. The
        # /8 keeps the injection a small slice of the context-char budget.
        preload = skills.preflight(
            self.cwd,
            self.lessons_path,
            task,
            char_budget=min(
                skills.PREFLIGHT_TOTAL_CHARS,
                self.num_ctx * CHARS_PER_TOKEN_BUDGET // 8,
            ),
            semantic=self.semantic.scores if self.semantic is not None else None,
        )
        if self.semantic is not None and self.semantic.error and not self._semantic_warned:
            self._semantic_warned = True
            self.echo(
                "⚑ semantic recall unavailable "
                f"({self.semantic.error[:80]}); falling back to word matching"
            )
        self._pending_skill_reads = {n: GATE_MAX_REFUSALS for n in preload.unread}
        # A new task starts un-gated: any pending comment belonged to the last
        # task and would otherwise stall the first tool call of this one.
        self._pending_comment_response = False
        self.messages.append(
            {"role": "system", "content": task_reminder(index, preload.text)}
        )
        if preload.names:
            self._note("⚑ preloaded knowledge: " + ", ".join(preload.names))
            self._emit_step(
                kind="knowledge",
                items=[{"label": it["name"], "kind": it["kind"]} for it in preload.items],
            )
        self._append(user_message)

        self._cancel.clear()  # a stale stop must not kill the new task
        self.task_sources = []
        task_started = time.perf_counter()
        tokens_in = tokens_out = 0
        repeats: dict[tuple, int] = {}  # (tool, args, result) -> occurrences
        # Progress-gated budget (#108): `max_steps` is the base, the ceiling is
        # the hard cost cap nothing exceeds, and `stall` counts consecutive
        # no-new-progress steps. A progressing task extends past max_steps; a
        # stalled one stops at MAX_STALL_STEPS.
        ceiling = max(self.max_steps, HARD_STEP_CEILING)
        stall = 0
        step = 0
        while step < ceiling:
            step += 1
            if self._cancel.is_set():
                return self._finish_cancelled()
            # Absorb anything the user queued while this task runs (issue #95),
            # BEFORE the model call so the next turn already reflects it. Neither
            # is a tool call, so both are placed outside the dispatch path and
            # leave the #81 gates and the loop-detection counters untouched.
            self._apply_pending_cwd()
            self._inject_pending_messages()
            self._enforce_budget(task_start)
            turn_start = time.perf_counter()
            # A live "Thinking…" row on the trace timeline; it finalizes to
            # "Thought for Xs" when the turn produced tools, or is dropped when
            # the turn was a plain answer (thinking_cancel below).
            self._emit_step(kind="thinking_start")
            self.status.start("thinking")
            try:
                content, tool_calls, usage, raw_blocks = self._chat_turn()
            except TaskCancelled:
                return self._finish_cancelled()
            finally:
                self.status.stop()
            turn_secs = time.perf_counter() - turn_start
            tokens_in += usage[0]
            tokens_out += usage[1]
            entry: dict = {"role": "assistant", "content": content}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if raw_blocks:
                # Provider-native content blocks (e.g. Anthropic thinking +
                # tool_use): the backend echoes these verbatim on the next
                # request instead of reconstructing the turn.
                entry["raw_blocks"] = raw_blocks
            self._append(entry)

            # Deny means STOP: only a TEXT-ONLY turn clears the stop gate.
            # Clearing on any content would be defeated by chatty preamble (or
            # thinking surfaced as content) that models emit alongside a tool
            # call — another command would run in the same turn. So the gate
            # holds until the model stops and replies with no tool call; that
            # turn also ends the task (normal loop semantics), so the user
            # steers before anything else runs.
            if content and not tool_calls:
                self._pending_comment_response = False

            if not tool_calls:
                result = content or EMPTY_RESPONSE
                if not content and self.on_token:
                    self.on_token(result + "\n")
                self._note(f"✓ answered in {format_secs(turn_secs)}{_tokens_note(usage)}")
                total = time.perf_counter() - task_started
                self._note(
                    f"∑ total {format_secs(total)}{_tokens_note((tokens_in, tokens_out))}"
                )
                # a plain answer needs no "Thinking" row, but carry the turn time
                # and token usage so the web trace can label the answer step
                # ("Answered in Xs") and keep the "↑N ↓M tokens" header (#84) —
                # a text-only turn has no later "thinking" step to carry it.
                self._emit_step(kind="thinking_cancel", secs=turn_secs, tokens=list(usage))
                return result

            # Ollama buffers tool-call generation and streams nothing until it
            # is done, so live counts are impossible here — report per turn.
            self._note(f"✓ thought for {format_secs(turn_secs)}{_tokens_note(usage)}")
            self._emit_step(kind="thinking", secs=turn_secs, tokens=list(usage))
            if content and self.on_token is None:
                self.echo(content)

            if self._cancel.is_set():
                # Proposed calls must not run after a stop — but every
                # tool_use still needs a paired result or the next request
                # is rejected (Anthropic pairing rules).
                for call in tool_calls:
                    self._append(
                        {
                            "role": "tool",
                            "tool_name": call["function"]["name"],
                            "content": NOT_EXECUTED,
                        }
                    )
                return self._finish_cancelled()

            results = self._execute_tool_calls(tool_calls)
            warn = stuck = progressed = False
            for call, result in zip(tool_calls, results, strict=True):
                self._append(
                    {"role": "tool", "tool_name": call["function"]["name"], "content": result}
                )
                self._collect_source(call, result)
                key = self._call_key(call, result)
                repeats[key] = count = repeats.get(key, 0) + 1
                if count == 1:
                    progressed = True  # a never-seen (tool,args,result) is progress (#108)
                if count >= LOOP_STOP_REPEATS:
                    stuck = True
                elif count == LOOP_WARN_REPEATS:
                    warn = True  # injected below: never between a turn's results
            if stuck:
                self.echo("✕ loop detected: identical call, identical output — stopping")
                return self._finish_stopped(LOOP_STOP_NOTE, STOPPED_LOOP)
            # Progress resets the stall clock; a step that produced only repeats
            # (or, defensively, no results at all) advances toward the stall cap.
            stall = 0 if progressed else stall + 1
            if stall >= MAX_STALL_STEPS:
                self.echo("⚠ no new progress for several steps — asking the model to wrap up")
                return self._finish_stopped(STALL_NOTE, STOPPED_STALL)
            if warn:
                self.echo("⚠ repeated identical tool call — nudging the model to change approach")
                self._append(
                    {"role": "user", "content": LOOP_WARNING.format(count=LOOP_WARN_REPEATS)}
                )

        self.echo("⚠ step ceiling reached — asking the model to wrap up")
        return self._finish_stopped(STEP_LIMIT_NOTE, STOPPED_LIMIT)

    @staticmethod
    def _call_key(call: dict, result: str) -> tuple:
        """Identity of a tool call AND its outcome — repr(args) because
        argument values may be unhashable."""
        function = call["function"]
        arguments = repr(sorted((function.get("arguments") or {}).items()))
        return (function["name"], arguments, result)

    def _finish_stopped(self, note: str, headline: str) -> str:
        """Step budget exhausted or loop detected: one final no-tools turn so
        the model can judge completion and report state (what's done, what
        remains, why it's stuck) instead of the task cutting off with a bare
        error line. The step budget is never silently exceeded — continuing
        is the user's call."""
        self._append({"role": "user", "content": note})
        self.status.start("wrapping up")
        try:
            content, tool_calls, _usage, raw_blocks = self._chat_turn()
        except TaskCancelled:
            return self._finish_cancelled()
        except ModelUnavailable:
            content, tool_calls, raw_blocks = "", [], None
        finally:
            self.status.stop()
        if content or tool_calls:
            entry: dict = {"role": "assistant", "content": content}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if raw_blocks:
                entry["raw_blocks"] = raw_blocks
            self._append(entry)
            for call in tool_calls:  # every tool_use still needs a paired result
                self._append(
                    {
                        "role": "tool",
                        "tool_name": call["function"]["name"],
                        "content": NOT_EXECUTED_LIMIT,
                    }
                )
        if not content and self.on_token:
            self.on_token(headline + "\n")
        return f"{headline}\n\n{content}" if content else headline

    def _finish_cancelled(self) -> str:
        """History stays model-consumable: an assistant note closes the turn."""
        self._append({"role": "assistant", "content": CANCELLED_RESULT})
        if self.on_token:
            self.on_token(CANCELLED_RESULT + "\n")
        self.echo("✕ task stopped")
        return CANCELLED_RESULT

    def _chat_turn(self) -> tuple[str, list[dict], tuple[int, int], list | None]:
        """One model call; returns (content, normalized tool_calls, token usage,
        provider-native raw blocks or None). Streams content through on_token
        when set. Retries once on a transport error (a busy/overloaded local
        Ollama commonly drops or refuses a request)."""
        self._refresh_plugin_tools()
        kwargs = dict(
            model=self.model,
            messages=self.messages,
            tools=tools.TOOL_SCHEMAS + self._plugin_defs,
            options={"num_ctx": self.num_ctx},
            think=self.think,
        )
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self._one_chat(kwargs)
            except TaskCancelled:
                raise  # a user stop is not a transport error — never retry
            except Exception as exc:  # noqa: BLE001 — surface, don't crash the REPL
                last_error = exc
                if attempt == 0:
                    self.echo(f"model call failed ({exc}); retrying once…")
        raise ModelUnavailable(str(last_error)) from last_error

    def _one_chat(self, kwargs: dict) -> tuple[str, list[dict], tuple[int, int], list | None]:
        raw_blocks = None
        if self.on_token is None:
            response = self.chat(**kwargs)
            message = response.message
            content = message.content or ""
            raw_calls = message.tool_calls or []
            usage = _usage(response)
            raw_blocks = getattr(message, "raw_blocks", None)
        else:
            parts: list[str] = []
            raw_calls = []
            usage = (0, 0)
            for chunk in self.chat(stream=True, **kwargs):
                if self._cancel.is_set():
                    # Abandoning the iterator closes the connection, which
                    # stops generation server-side — the fastest stop there is.
                    raise TaskCancelled
                # Ollama streams ~one chunk per generated token, so chunk
                # count drives the live "↓ N tokens" readout on the ticker.
                self.status.add_tokens(1)
                message = chunk.message
                if message.content:
                    if not parts:
                        self.status.stop()  # erase the live timer line first
                        self.on_token("\n")
                    parts.append(message.content)
                    self.on_token(message.content)
                if message.tool_calls:
                    raw_calls.extend(message.tool_calls)
                if getattr(message, "raw_blocks", None):
                    raw_blocks = message.raw_blocks
                if _usage(chunk) != (0, 0):  # counts arrive on the final chunk
                    usage = _usage(chunk)
            content = "".join(parts)
            if content:
                self.on_token("\n")
        return content, [self._normalize_call(c) for c in raw_calls], usage, raw_blocks

    @staticmethod
    def _normalize_call(call: Any) -> dict:
        """Plain-dict tool call: safe to keep in history and send back to the
        backend. extra_content (e.g. Gemini thought signatures) must survive
        the round trip — some providers reject the next request without it."""
        if isinstance(call, dict):
            function = call.get("function") or {}
            name = function.get("name", "")
            arguments = function.get("arguments") or {}
            extra = call.get("extra_content")
        else:
            name = call.function.name
            arguments = call.function.arguments or {}
            extra = getattr(call, "extra_content", None)
        normalized = {"function": {"name": name, "arguments": dict(arguments)}}
        if extra:
            normalized["extra_content"] = extra
        return normalized

    def _trim_tool_message(self, message: dict) -> bool:
        if message.get("role") != "tool":
            return False
        content = message["content"]
        if len(content) <= TRIM_KEEP_CHARS + len(TRIMMED_NOTE):
            return False
        message["content"] = content[:TRIM_KEEP_CHARS] + TRIMMED_NOTE
        return True

    def _total_chars(self) -> int:
        return sum(len(message.get("content") or "") for message in self.messages)

    def _enforce_budget(self, task_start: int) -> None:
        """Trim this task's oldest tool outputs (never the 2 most recent)
        until the conversation fits the character budget."""
        budget = self.num_ctx * CHARS_PER_TOKEN_BUDGET
        if self._total_chars() <= budget:
            return
        tool_indices = [
            i
            for i in range(task_start, len(self.messages))
            if self.messages[i].get("role") == "tool"
        ]
        for i in tool_indices[:-2]:
            if self._trim_tool_message(self.messages[i]) and self._total_chars() <= budget:
                return

    def expand_alias(self, command: str) -> str:
        """Rewrite the first word via the aish alias map, BEFORE approval sees
        it. The single chokepoint both entry points (_dispatch for model-issued
        commands, run_user_command for ! commands) route through, so the gate,
        denylist, and cd-check always classify the REAL command — never an
        opaque alias name."""
        return alias_map.expand(command, self.aliases)

    def run_user_command(self, command: str) -> str:
        """A command the user typed directly (! prefix): no approval needed,
        but recorded in the conversation so the model has the context.
        !cd is an alias for /cd — the user moving the directory always means
        moving the project, so cwd and the primary root travel together and
        the model's anchor stays coherent."""
        command = self.expand_alias(command)
        cd_target = self._parse_cd(command)
        if cd_target is not None:
            return self.rebase(cd_target)
        self._cancel.clear()  # a stale stop must not kill the new command
        # Framing brackets the output as a terminal block for rich clients (the
        # web UI) and records it for cold replay, exactly like a model command;
        # on the CLI on_command_start/end are unset, so it stays log-only.
        self._emit_command_start(command, user=True)
        # should_stop wires the web UI Stop button to this user command: cancel()
        # sets the same event the model path polls, so a long/hung ! command is
        # interruptible (its whole process group is signaled — issue #76).
        result = tools.run_command(
            command,
            cwd=self.cwd,
            on_line=self.stream,
            allow_detach=True,
            log_dir=self.job_log_dir,
            should_stop=self._cancel.is_set,
        )
        if self._cancel.is_set():
            self._emit_command_end(status="interrupted")
        else:
            self._emit_command_end(status="exit", exit_code=_parse_exit_code(result))
        if self.stream is None:
            self.echo(result)
        self._append(
            {"role": "user", "content": f"[I ran `{command}` myself; output:]\n{result}"}
        )
        return result

    def add_user_context(self, text: str) -> None:
        """Append a user-authored context turn WITHOUT running the model: the note
        becomes visible to the model on its next task and is logged so it survives
        `--resume`, but no answer is generated now. Backs the web "share selection
        to context" action for interactive PTY sessions (issue #148), where the
        terminal I/O is otherwise private to the terminal."""
        self._append({"role": "user", "content": text})

    def rebase(self, target: str, announce: bool = True) -> str:
        """User-typed /cd (and its alias !cd): move cwd AND re-anchor the
        primary session root. Never reachable by the model — that's what
        keeps root scoping honest.

        `announce` appends a user-turn note telling the model the project moved.
        It's suppressed mid-task (announce=False from _apply_pending_cwd): a fresh
        user turn injected mid-flight reads as a new prompt, so the model
        abandons the running task to answer it. Between tasks (immediate /cd,
        post-task apply, CLI) it's fine — it's the model's cwd signal there."""
        result = self._change_dir(target)
        if result.startswith("ERROR"):
            return result
        self.roots[0] = Path(self.cwd).resolve()
        self._sync_cwd_in_context()  # system prompt reflects the new cwd (no user turn)
        self._emit_workspace("cwd", self.cwd)  # the timeline marker; no grey echo
        if announce:
            self._append(
                {"role": "user", "content": f"[I moved the session to {self.cwd} with /cd — "
                 "this directory is the project now]"}
            )
        return result

    def add_root(self, target: str) -> str:
        """User-typed /add-dir: allow auto-approved reads/commands in another tree."""
        path = Path(os.path.expanduser(target))
        if not path.is_absolute():
            path = Path(self.cwd) / path
        path = path.resolve()
        if not path.is_dir():
            return f"ERROR: no such directory: {path}"
        if path in self.roots:
            return f"[{path} is already a session root]"
        self.roots.append(path)
        self._emit_workspace("trust", str(path))
        note = f"[I added {path} as a session root with /add-dir — you may work there too]"
        self._append({"role": "user", "content": note})
        return f"[added session root {path}]"

    def trust_root(self, target: str) -> str:
        """Approver-side 'trust this directory for this session': widens the
        roots mid-approval. Unlike add_root it never touches the conversation —
        it runs while a tool call is in flight, where an injected user message
        could break providers that require tool results to follow tool calls."""
        path = Path(os.path.expanduser(target))
        if not path.is_absolute():
            path = Path(self.cwd) / path
        path = path.resolve()
        if not path.is_dir():
            return f"ERROR: no such directory: {path}"
        if any(path.is_relative_to(root) for root in self.roots):
            return f"[{path} is already inside a session root]"
        self.roots.append(path)
        self._emit_workspace("trust", str(path))
        return f"[trusted for this session: {path}]"

    def _execute_tool_calls(self, tool_calls: list[dict]) -> list[str]:
        """Run one model turn's tool calls; results keep the call order.

        Read-only tools (no side effects, no approval prompt) run concurrently
        when the turn has more than one — they are network/disk-bound, so this
        is a pure latency win. Anything that prompts the user or writes stays
        sequential: two interleaved [y/N] prompts would be unanswerable.
        """
        calls = [(c["function"]["name"], c["function"]["arguments"] or {}) for c in tool_calls]
        concurrent = [
            i
            for i, (name, args) in enumerate(calls)
            if (name in READ_ONLY_TOOLS or self._is_readonly_plugin(name))
            and not self._read_needs_prompt(name, args)
        ]
        # While either gate is armed, everything goes through _dispatch
        # sequentially — the parallel thunks below would bypass the gate (and
        # the skill-counter dict is not thread-safe).
        if len(concurrent) < 2 or self._pending_skill_reads or self._pending_comment_response:
            return [
                self._call_result(
                    name, partial(self._timed, partial(self._dispatch, name, args)), args=args
                )
                for name, args in calls
            ]

        results: list[str] = [""] * len(calls)
        with ThreadPoolExecutor(max_workers=min(len(concurrent), 8)) as pool:
            batch_start = time.perf_counter()
            futures = {}
            for i in concurrent:
                label, thunk = self._read_only_call(*calls[i])
                self._note(label)
                # _timed runs on the worker so the reported duration is the
                # call's true runtime, not how long collection waited for it.
                futures[i] = pool.submit(self._timed, thunk)
            # Collect futures first, under one live timer; future.result()
            # re-raises worker exceptions here, so error echoes stay on the
            # main thread. Tools that may prompt the user run after the timer
            # stops — a [y/N] prompt must never fight the ticking line.
            self.status.start(f"{len(futures)} parallel lookups")
            try:
                for i in futures:
                    # ⇉ marks overlapped runtimes: they exceed wall time when
                    # summed, so only the batch ✓ line below counts toward ∑.
                    results[i] = self._call_result(
                        calls[i][0], futures[i].result, mark="⇉", args=calls[i][1]
                    )
            finally:
                self.status.stop()
            self._note(
                f"✓ {len(futures)} parallel lookups "
                f"{format_secs(time.perf_counter() - batch_start)}"
            )
            for i, (name, args) in enumerate(calls):
                if i not in futures:
                    results[i] = self._call_result(
                        name, partial(self._timed, partial(self._dispatch, name, args)), args=args
                    )
        return results

    @staticmethod
    def _timed(fn: Callable[[], str]) -> tuple[str, float]:
        start = time.perf_counter()
        return fn(), time.perf_counter() - start

    @staticmethod
    def _arg_summary(name: str, args: dict) -> str:
        """A one-line human label for a tool call — the trace step subtitle."""
        a = args or {}
        if name == "read_skill":
            return str(a.get("name", ""))
        if name == "web_search":
            return str(a.get("query", ""))
        if name == "read_url":
            return str(a.get("url", ""))
        if name == "recall":
            return str(a.get("query") or a.get("name") or "")
        if name in ("read_file", "write_file", "edit_file"):
            return str(a.get("path", ""))
        if name in ("remember", "forget_memory"):
            return str(a.get("name") or "memory")
        # read_docs, run_command, and anything else: the command/topic string.
        return str(a.get("command", ""))

    def _call_result(
        self,
        name: str,
        fn: Callable[[], tuple[str, float]],
        mark: str = "✓",
        args: dict | None = None,
    ) -> str:
        args = args or {}
        self._run_meta = None
        self._emit_step(
            kind="tool_start",
            name=name,
            summary=self._arg_summary(name, args),
            command=str(args.get("command", "")) if name == "run_command" else "",
        )
        try:
            result, elapsed = fn()
        except ModuleNotFoundError as exc:
            # A broken install, not a transient failure: retrying the
            # same call can never succeed, so say so to the model too.
            result = (
                f"ERROR: tool '{name}' is unavailable — this aish "
                f"installation is missing the '{exc.name}' package. "
                "Do NOT retry this tool; it will keep failing. Tell "
                "the user to reinstall aish (uv tool install --force "
                "git+https://github.com/epnasis/aish.git) and restart."
            )
            self.echo(result)
            self._emit_step(kind="tool", name=name, secs=0.0, ok=False, summary="unavailable")
            return result
        except Exception as exc:  # noqa: BLE001 — a tool bug must not kill the session
            result = f"ERROR: tool '{name}' failed internally: {exc!r}"
            self.echo(result)
            self._emit_step(kind="tool", name=name, secs=0.0, ok=False, summary="failed")
            return result
        self._note(f"{mark} {name} {format_secs(elapsed)}")
        self._emit_tool_step(name, args, result, elapsed)
        return result

    def _emit_tool_step(self, name: str, args: dict, result: str, secs: float) -> None:
        if self.on_step is None and self.step_log is None:
            return
        ok = not (result.startswith("ERROR") or result.startswith("NOT EXECUTED"))
        step: dict[str, Any] = {
            "kind": "tool",
            "name": name,
            "secs": secs,
            "ok": ok,
            "summary": self._arg_summary(name, args),
        }
        if not ok and self._run_meta is None:
            # Non-run_command failure (a read_url/web_search error, a gate
            # refusal): carry the message so the trace can explain what broke.
            step["error"] = result[:STEP_OUTPUT_CAP]
        if self._run_meta is not None:  # run_command: command, decision, output
            step.update(self._run_meta)
            self._run_meta = None
            output = step.get("output") or ""
            if len(output) > STEP_OUTPUT_CAP:  # the trace shows a preview, not the full log
                step["output"] = output[:STEP_OUTPUT_CAP] + "\n… (truncated)"
        self._sink_step(step)

    def _read_only_call(self, name: str, args: dict) -> tuple[str, Callable[[], str]]:
        """(echo label, execution thunk) for a READ_ONLY_TOOLS member — split
        so the label prints before the thunk possibly runs on a worker thread."""
        if name == "read_docs":
            command = str(args.get("command", ""))
            topic = args.get("topic") or None
            label = f"→ read_docs: {command}" + (f" (topic: {topic})" if topic else "")
            return label, partial(tools.read_docs, command, topic=str(topic) if topic else None)
        if name == "read_skill":
            skill = str(args.get("name", ""))
            return f"→ read_skill: {skill}", partial(
                skills.load_skill, skill, skills.skill_dirs(self.cwd)
            )
        if name == "web_search":
            query = str(args.get("query", ""))
            return f"→ web_search: {query}", partial(web.web_search, query)
        if name == "read_url":
            url = str(args.get("url", ""))
            topic = args.get("topic") or None
            label = f"→ read_url: {url}" + (f" (topic: {topic})" if topic else "")
            return label, partial(web.read_url, url, topic=str(topic) if topic else None)
        if name == "recall":
            query = str(args.get("query", "") or "")
            entry = str(args.get("name", "") or "").strip() or None
            label = f"→ recall: {query or '(no query)'}" + (
                f" (name: {entry})" if entry else ""
            )
            return label, partial(self._recall, query, entry)
        tool = self._plugin_tools.get(name)
        if tool is not None:  # read-only plugin tool (mutating ones never reach here)
            shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
            return f"→ {name}({shown})", partial(self._run_readonly_plugin, tool, args)
        return self._read_file_call(args)  # read_file

    def _is_readonly_plugin(self, name: str) -> bool:
        tool = self._plugin_tools.get(name)
        return tool is not None and not tool.mutating

    def _run_readonly_plugin(self, tool: "tool_plugins.Tool", args: dict) -> str:
        problem = tool_plugins.validate_args(tool, args)
        if problem is not None:
            return problem
        return tool_plugins.execute(tool, args, cwd=self.cwd)

    def _recall(self, query: str, name: str | None) -> str:
        if self.state_dir is None:
            return skills.recall_text(self.cwd, self.lessons_path, query, name=name)
        state_dir = Path(self.state_dir)
        exclude: set = set()
        if self.current_session is not None:
            exclude.add(Path(self.current_session()))
        return skills.recall_text(
            self.cwd,
            self.lessons_path,
            query,
            name=name,
            sessions_search=lambda q: SessionLog.recall_sessions(state_dir, q, exclude=exclude),
            session_detail=lambda session, q: SessionLog.search_excerpts(
                state_dir, q, session=session
            ),
        )

    def _collect_source(self, call: dict, result: str) -> None:
        """Track pages actually fetched this task, so answers can cite them.
        Only read_url counts — web_search hits are found-but-maybe-unread."""
        if call["function"]["name"] != "read_url" or result.startswith("ERROR"):
            return
        url = str((call["function"].get("arguments") or {}).get("url", "")).strip()
        if not url or any(s["url"] == url for s in self.task_sources):
            return
        source = {"url": url}
        title = web.PAGE_TITLES.get(url)
        if title:
            source["title"] = title
        self.task_sources.append(source)

    def _read_needs_prompt(self, name: str, args: dict) -> bool:
        path = str(args.get("path", ""))
        return name == "read_file" and self._read_prompt_reason(path) is not None

    def _read_prompt_reason(self, path: str) -> str | None:
        """Why an otherwise auto-approved read_file must prompt, or None."""
        if files.is_sensitive_path(path, self.cwd):
            return "sensitive"
        if files.is_outside_roots(path, self.cwd, self.roots):
            return "outside"
        return None

    @staticmethod
    def _int_arg(args: dict, key: str, default: int) -> int:
        try:
            return int(args.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _read_file_call(self, args: dict) -> tuple[str, Callable[[], str]]:
        path = str(args.get("path", ""))
        offset = self._int_arg(args, "offset", 1)
        limit = self._int_arg(args, "limit", files.READ_MAX_LINES)
        label = f"→ read_file: {path}" + (f" (from line {offset})" if offset > 1 else "")
        return label, partial(files.read_file, path, self.cwd, offset=offset, limit=limit)

    def _arm_stop_gate(self, comment: str) -> None:
        """A DENY carried a concern — stop: hold every further tool call until
        the model addresses it in plain text (issue #81). No-op for a bare
        denial, matching the note that only fires on a comment. Approvals never
        arm this — they mean continue (the command is held for adjustment,
        re-proposed, and approved again)."""
        if comment:
            self._pending_comment_response = True

    def _stop_gate(self, name: str, args: dict) -> str | None:
        """Refusal while a denial's concern is unaddressed, else None.

        A Denied comment arms this (see _dispatch/_dispatch_write); the main
        loop clears the flag only when a turn is TEXT-ONLY (no tool call), so a
        genuine reply — not chatty preamble riding alongside a command — lifts
        it, and that text-only turn ends the task (deny means stop). Until then
        every tool call is refused. No countdown: the flag survives across gated
        turns, and the step budget bounds a model that never replies."""
        if not self._pending_comment_response:
            return None
        if name == "run_command":  # so the trace shows why it was held, not a bare row
            self._run_meta = {
                "command": str(args.get("command", "")),
                "decision": "blocked",
                "output": "Held until you address the user's concern.",
            }
        self._note("✋ stopped until you address the user's concern")
        return STOP_GATE_REFUSAL

    def _skill_gate(self, name: str, args: dict) -> str | None:
        """Refusal text while a flagged oversized skill is unread, else None.

        read_skill/recall targeting a flagged skill lifts its gate; any other
        call decrements every counter so a model that ignores the directive
        (or states why the skill does not apply and retries) is only held for
        GATE_MAX_REFUSALS rounds — enforcement, not a wedge."""
        if not self._pending_skill_reads:
            return None
        target = str(args.get("name", "") or "")
        if name in ("read_skill", "recall") and target in self._pending_skill_reads:
            del self._pending_skill_reads[target]
            return None
        names = ", ".join(self._pending_skill_reads)
        first = next(iter(self._pending_skill_reads))
        for key in list(self._pending_skill_reads):
            self._pending_skill_reads[key] -= 1
            if self._pending_skill_reads[key] <= 0:
                del self._pending_skill_reads[key]
        self._note(f"✋ gated until read_skill: {names}")
        return SKILL_GATE_REFUSAL.format(names=names, first=first)

    def _dispatch(self, name: str, args: dict) -> str:
        # The gates run before everything — a refusal must never reach an
        # approval prompt or a tool implementation. The stop gate goes first: a
        # denial's concern outranks every other rule and must be addressed
        # before any tool runs.
        refusal = self._stop_gate(name, args)
        if refusal is not None:
            return refusal

        refusal = self._skill_gate(name, args)
        if refusal is not None:
            if name == "run_command":  # so the trace shows why it was held, not a bare row
                self._run_meta = {
                    "command": str(args.get("command", "")),
                    "decision": "blocked",
                    "output": "Held until the required skill is read.",
                }
            return refusal

        if name == "read_file":
            path = str(args.get("path", ""))
            label, thunk = self._read_file_call(args)
            self._note(label)
            reason = self._read_prompt_reason(path)
            if reason is not None and not self.approve_read(path, reason):
                return READ_DENIED
            return thunk()

        if name in READ_ONLY_TOOLS:
            label, thunk = self._read_only_call(name, args)
            self._note(label)
            self.status.start(name)
            try:
                return thunk()
            finally:
                self.status.stop()

        if name == "remember":
            note = str(args.get("note", ""))
            result = skills.save_memory(
                note,
                skills.GLOBAL_MEMORY_DIR,
                name=str(args.get("name", "") or ""),
                keywords=str(args.get("keywords", "") or ""),
                cwd=self.cwd,
                lessons_path=self.lessons_path,
            )
            self._note(f"→ {result}")
            return result

        if name == "forget_memory":
            # Auto-approved like remember: strictly confined to the model's own
            # memory files (slug-validated, one fact each) and recoverable from
            # the knowledge git backup, so the create/update inverse stays
            # frictionless rather than inventing a new approval channel.
            result = skills.forget_memory(str(args.get("name", "") or ""), cwd=self.cwd)
            self._note(f"→ {result}")
            return result

        if name in ("write_file", "edit_file"):
            return self._dispatch_write(name, args)

        if name == "create_tool":
            return self._create_tool(args)

        if name == "import_skill":
            return self._import_skill(args)

        if name == "run_command":
            # Expand any aish alias on the first word BEFORE the gate, so the
            # denylist/approval/cd-check all classify the real command.
            command = self.expand_alias(str(args.get("command", "")))

            # Stateless execution: a bare model-issued cd never runs — it
            # would silently detach the model from the project directory, its
            # one stable anchor across long conversations and context trims.
            # Excursions are per-command subshells (cd x && ...), which revert
            # on exit; only the user moves the project (/cd, !cd).
            if self._parse_cd(command) is not None:
                result = CD_NOT_STICKY.format(cwd=self.cwd)
                self._note(result)
                self._run_meta = {"command": command, "decision": "rejected", "output": result}
                return result

            # Auto-approve a delete confined strictly to the scratch workspace
            # (issue #70): rm inside the ephemeral scratch dir is throwaway
            # cleanup, so it skips the prompt. is_scratch_delete fails closed —
            # anything ambiguous or escaping falls through to self.approve, so
            # the denylist and prompt still guard every other rm.
            if is_scratch_delete(command, self.cwd, self.scratch_dir):
                decision: Any = command
            else:
                decision = self.approve(command)
            if isinstance(decision, Blocked):
                self._run_meta = {
                    "command": command, "decision": "blocked", "output": decision.reason,
                }
                return BLOCKED_RESULT.format(reason=decision.reason)
            if isinstance(decision, Denied):
                # Deny + comment = STOP: address the concern, then halt. The stop
                # gate holds every tool until a text-only reply, which ends the
                # task so the user can steer before anything else runs.
                self._run_meta = {
                    "command": command, "decision": "denied", "output": decision.comment or "",
                }
                self._arm_stop_gate(decision.comment)
                return _with_feedback(DENIED_RESULT, decision.comment)
            if isinstance(decision, Approved):
                # Approve + comment = CONTINUE, but adjust: the original command
                # is NOT run as-is. Hold it — the model adjusts to what the user
                # asked and re-proposes, and that adjusted command is approved
                # again before it runs (issue #81). Approval never stops the task.
                self._run_meta = {
                    "command": command, "decision": "held", "output": decision.comment,
                }
                return HELD_FOR_ADJUSTMENT.format(comment=decision.comment)
            if decision is None or decision is False:
                self._run_meta = {"command": command, "decision": "denied", "output": ""}
                return DENIED_RESULT
            final = command if decision is True else str(decision)
            # command_start opens the bounded terminal block in the web UI:
            # cwd + the (possibly edited) command that is about to run.
            self._emit_command_start(final)
            if args.get("background"):
                result = tools.start_background(final, cwd=self.cwd, log_dir=self.job_log_dir)
                self._note(result)
                self._run_meta = {"command": final, "decision": "approved", "output": result}
                # A detached job has no exit code — label the block instead.
                self._emit_command_end(status="detached", job=_parse_job_id(result))
                return result
            result = tools.run_command(
                final,
                cwd=self.cwd,
                on_line=self.stream,
                allow_detach=True,
                log_dir=self.job_log_dir,
                should_stop=self._cancel.is_set,
            )
            self._run_meta = {"command": final, "decision": "approved", "output": result}
            # command_end closes the block: a user cancel has no clean exit
            # code, a failed-to-start command none at all; otherwise the code.
            if self._cancel.is_set():
                self._emit_command_end(status="interrupted")
            else:
                code = _parse_exit_code(result)
                self._emit_command_end(status="exit", exit_code=code)
            if self.stream is None:
                self.echo(result)
            if final != command:
                result = f"[user edited the command to: {final}]\n{result}"
            # Drift nudge (#140): the model ran a raw command a reliable plugin
            # tool already covers — point it at the tool for next time. Advisory
            # only; the command still ran.
            covered = self._tool_for_command(command)
            if covered is not None:
                self._note(f"↩ prefer tool '{covered}' over raw command")
                result += (
                    f"\n\n[aish: the '{covered}' tool covers this operation and passes "
                    "arguments safely (no shell quoting) — prefer calling it over "
                    "composing this command by hand next time.]"
                )
            return result

        tool = self._plugin_tools.get(name)
        if tool is not None:
            return self._dispatch_plugin_tool(tool, args)

        return f"ERROR: unknown tool '{name}'"

    def _refresh_plugin_tools(self) -> None:
        """Rescan TOOL.md manifests when the tool dirs' signature changed
        (mtime-cached, near-free). Read-only tools are always exposed; mutating
        ones are exposed only when a tool approver is wired (else kept for
        fail-closed dispatch but not offered). Invalid manifests are skipped
        and warned about once each."""
        sig = tool_plugins.signature(self.cwd)
        if sig == self._plugin_sig:
            return
        self._plugin_sig = sig
        found, warnings = tool_plugins.discover(self.cwd)
        self._plugin_tools = {t.name: t for t in found}
        gated_ok = self.approve_tool is not None
        exposed = [t for t in found if not t.mutating or gated_ok]
        self._plugin_defs = [tool_plugins.to_tool_def(t) for t in exposed]
        # Only nudge toward tools the model can actually call.
        self._tool_wraps = [(t.wraps, t.name) for t in exposed if t.wraps]
        for warning in warnings:
            if warning not in self._plugin_warned:
                self._plugin_warned.add(warning)
                self._note(f"⚠ tool skipped: {warning}")

    def _tool_for_command(self, command: str) -> str | None:
        """The name of an available plugin tool whose `wraps:` prefix matches
        this raw command, or None. Used to nudge the model off re-composing a
        command a reliable tool already covers (issue #140)."""
        cmd = " ".join(command.split())
        for prefix, name in self._tool_wraps:
            p = " ".join(prefix.split())
            if p and (cmd == p or cmd.startswith(p + " ")):
                return name
        return None

    def _dispatch_plugin_tool(self, tool: "tool_plugins.Tool", args: dict) -> str:
        # Args are validated BEFORE the gate, so the user never approves a call
        # that would fail validation anyway, and the error feeds the retry loop.
        problem = tool_plugins.validate_args(tool, args)
        if problem is not None:
            self._note(f"→ {tool.name}: {problem}")
            return problem

        if tool.mutating:
            if self.approve_tool is None:
                # Not exposed without an approver, so this only fires on a stale
                # tool_call — fail closed rather than run a mutation ungated.
                return (
                    f"ERROR: tool {tool.name!r} is mutating and no tool approver "
                    "is available; it cannot run."
                )
            decision = self.approve_tool(tool.name, args)
            if isinstance(decision, Denied):
                # Deny + comment = STOP (issue #81): address the concern, then halt.
                self._arm_stop_gate(decision.comment)
                return _with_feedback(DENIED_RESULT, decision.comment)
            if isinstance(decision, Approved):
                # Approve + comment = CONTINUE but adjust: the original args are
                # HELD, the model reworks them and re-proposes (re-approved).
                return TOOL_HELD_FOR_ADJUSTMENT.format(
                    name=tool.name, comment=decision.comment
                )
            if decision is None or decision is False:
                return DENIED_RESULT

        shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
        self._note(f"→ {tool.name}({shown})")
        self.status.start(tool.name)
        try:
            return tool_plugins.execute(tool, args, cwd=self.cwd)
        finally:
            self.status.stop()

    _WRAPPER_META = {  # lang -> (filename, shebang)
        "sh": ("run.sh", "#!/bin/sh"),
        "python": ("run.py", "#!/usr/bin/env python3"),
    }

    def _create_tool(self, args: dict) -> str:
        """Author a plugin tool (issue #141). Three guardrails the model cannot
        bypass: the WHEN test lives in the tool description (imperative); the
        manifest is LINTED and this refuses to write on any error (structured
        feedback → correct-and-retry); and both files go through the normal
        write-approval gate so the user sees the real code before it exists."""
        name = str(args.get("name", "")).strip()
        if not tool_plugins.NAME_RE.match(name):
            return f"ERROR: invalid tool name {name!r} (need [A-Za-z0-9_-], 1-64)."
        lang = str(args.get("wrapper_lang", "sh") or "sh").strip()
        if lang not in self._WRAPPER_META:
            return f"ERROR: wrapper_lang must be one of {sorted(self._WRAPPER_META)}."
        raw_schema = str(args.get("schema", "") or "{}").strip() or "{}"
        try:
            schema_obj = json.loads(raw_schema)
        except json.JSONDecodeError as exc:
            return f"ERROR: schema is not valid JSON ({exc})."

        wrapper_name, shebang = self._WRAPPER_META[lang]
        wrapper_body = str(args.get("wrapper", ""))
        if not wrapper_body.strip():
            return "ERROR: wrapper (the executable script body) is required."
        if not wrapper_body.startswith("#!"):
            wrapper_body = f"{shebang}\n{wrapper_body}"
        if not wrapper_body.endswith("\n"):
            wrapper_body += "\n"

        mutating = "yes" if args.get("mutating") else "no"
        lines = [
            "---",
            f"name: {name}",
            f"description: {str(args.get('description', '')).strip()}",
            f"exec: ./{wrapper_name}",
            f"mutating: {mutating}",
        ]
        if args.get("timeout"):
            lines.append(f"timeout: {int(args['timeout'])}")
        if str(args.get("wraps", "") or "").strip():
            lines.append(f"wraps: {str(args['wraps']).strip()}")
        if str(args.get("secrets", "") or "").strip():
            lines.append(f"secrets: {str(args['secrets']).strip()}")
        lines.append(f"schema: {json.dumps(schema_obj)}")
        lines.append("---")
        lines.append(str(args.get("notes", "")).strip() or f"{name} tool.")
        manifest_text = "\n".join(lines) + "\n"

        # Lint against a throwaway copy first — never prompt for an invalid tool.
        with tempfile.TemporaryDirectory(prefix="aish-tool-lint-") as tmp:
            tdir = Path(tmp)
            (tdir / wrapper_name).write_text(wrapper_body, encoding="utf-8")
            os.chmod(tdir / wrapper_name, 0o755)
            (tdir / "TOOL.md").write_text(manifest_text, encoding="utf-8")
            errors = tool_plugins.lint(tdir / "TOOL.md")
        if errors:
            joined = "; ".join(e.split(": ", 1)[-1] for e in errors)
            return (
                f"ERROR: tool {name!r} did not validate: {joined}. "
                "Fix and call create_tool again."
            )

        if self.approve_write is None:
            return "ERROR: no write approver available; cannot create a tool."

        if str(args.get("scope", "global")).strip() == "project":
            base = Path(self.cwd) / ".aish" / "tools" / name
        else:
            base = tool_plugins.GLOBAL_TOOLS_DIR / name

        # Manifest FIRST: the user reasons about the tool's interface (what it
        # does, what args it takes) before its implementation — review intent,
        # then verify the code. Each file is diff-approved; a denial aborts,
        # and an orphan (manifest without wrapper, or vice-versa) is simply
        # skipped at discovery since the linter won't resolve it.
        self._note(f"→ creating tool {name} in {_display_path(base)}")
        manifest_res = self._commit_tool_file(base / "TOOL.md", manifest_text, executable=False)
        if manifest_res is not None:
            return manifest_res
        wrapper_res = self._commit_tool_file(base / wrapper_name, wrapper_body, executable=True)
        if wrapper_res is not None:
            return wrapper_res
        self._plugin_sig = None  # force a rescan so the new tool is offered
        self._note(f"→ created tool {name} at {base}")
        return f"Created tool {name!r} at {base}. It is available on the next step."

    def _commit_tool_file(self, target: Path, content: str, executable: bool) -> str | None:
        """Write one tool file through the diff-approval gate. Returns None on
        success, or a stop/deny result string the model should surface."""
        plan = files.plan_write(str(target), content, self.cwd)
        if plan.error:
            return f"ERROR: {plan.error}"
        decision = self.approve_write(plan)
        if isinstance(decision, Denied):
            self._arm_stop_gate(decision.comment)
            return _with_feedback(WRITE_DENIED, decision.comment)
        if isinstance(decision, Approved):
            return WRITE_HELD_FOR_ADJUSTMENT.format(comment=decision.comment)
        if not decision:
            return WRITE_DENIED
        files.commit(plan)
        if executable:
            try:
                mode = os.stat(target).st_mode
                os.chmod(target, mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            except OSError as exc:
                return f"ERROR: wrote {target} but could not make it executable: {exc}"
        return None

    def _import_skill(self, args: dict) -> str:
        """Import a skill (#139). Untrusted content — the whole skill is shown in
        ONE consolidated review (every file's contents, syntax-highlighted, plus
        deterministic risk flags), and installs only on a single approval. Only a
        shallow read-only clone happens; the code is never executed on import."""
        repo = str(args.get("repo", "")).strip()
        if not repo:
            return "ERROR: repo (a git URL or local path) is required."
        if self.approve_import is None:
            return "ERROR: no import reviewer available; cannot import a skill."
        try:
            name, description, imported, skipped, tmp = skill_import.stage(
                repo, str(args.get("path", "")).strip()
            )
        except skill_import.SkillImportError as exc:
            return f"ERROR: {exc}"
        try:
            override = str(args.get("name", "")).strip()
            if override:
                if not skills.NAME_RE.match(override):
                    return f"ERROR: invalid skill name {override!r}."
                name = override
            dest = skills.GLOBAL_SKILLS_DIR / name
            flags = skill_import.safety_scan(imported)
            files_payload = [
                {"path": rel, "content": text, "lang": skill_import.lang_for(rel),
                 "executable": is_exec}
                for rel, text, is_exec in imported
            ]
            self._note(f"→ reviewing skill '{name}' ({len(imported)} files)")
            decision = self.approve_import(
                name=name, description=description, files=files_payload,
                skipped=skipped, flags=flags, dest=str(dest),
            )
            if isinstance(decision, Denied):
                self._arm_stop_gate(decision.comment)
                return _with_feedback(
                    f"Import of {name!r} was DENIED — nothing was installed.",
                    decision.comment,
                )
            if not decision:
                return f"Import of {name!r} was denied — nothing was installed."
            # Approved: install all reviewed files at once (the review already
            # happened; no per-file prompts).
            for rel, text, is_exec in imported:
                plan = files.plan_write(str(dest / rel), text, self.cwd)
                if plan.error:
                    return f"ERROR importing {rel}: {plan.error}"
                files.commit(plan)
                if is_exec:
                    target = dest / rel
                    try:
                        mode = os.stat(target).st_mode
                        os.chmod(target, mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
                    except OSError:
                        pass
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        self._note(f"→ imported skill '{name}'")
        skipped_note = f" Skipped binary assets: {', '.join(skipped)}." if skipped else ""
        return (
            f"Imported skill {name!r} into {dest} ({len(imported)} files)."
            f"{skipped_note} It is available on the next task."
        )

    def _dispatch_write(self, name: str, args: dict) -> str:
        if name == "write_file":
            plan = files.plan_write(
                str(args.get("path", "")), str(args.get("content", "")), self.cwd
            )
        else:
            plan = files.plan_edit(
                str(args.get("path", "")),
                str(args.get("old_str", "")),
                str(args.get("new_str", "")),
                self.cwd,
            )
        if plan.error:
            return f"ERROR: {plan.error}"
        # Writes into the ephemeral scratch workspace are auto-approved (issue
        # #70) — no diff card. Confined strictly inside the scratch dir;
        # anything resolving outside falls through to the normal approval gate.
        if path_within(str(plan.target), self.cwd, self.scratch_dir):
            result = files.commit(plan)
            self.echo(result)
            return result
        # The diff the approval card showed, carried onto the trace step so the
        # web timeline renders WHAT changed (or would have) — applied, denied, or
        # held alike (#55). Computed from the plan (pre-commit), so it is stable
        # regardless of the decision.
        diff_meta = {"diff": plan.diff, "added": plan.added, "removed": plan.removed}
        decision = self.approve_write(plan)
        if isinstance(decision, Denied):
            # Deny + comment = STOP: a denied write never touches disk — the
            # trace step renders denied (not a silent success), carries the
            # user's feedback, and arms the stop gate like a denied run_command.
            self._run_meta = {
                "decision": "denied", "ok": False, "output": "",
                "comment": decision.comment, **diff_meta,
            }
            self._arm_stop_gate(decision.comment)
            return _with_feedback(WRITE_DENIED, decision.comment)
        if isinstance(decision, Approved):
            # Approve + comment = CONTINUE, but adjust: hold the write (nothing
            # is committed), the model adjusts to what the user asked and
            # re-proposes, and that change is approved again before it lands.
            self._run_meta = {
                "decision": "held", "ok": False, "output": "",
                "comment": decision.comment, **diff_meta,
            }
            return WRITE_HELD_FOR_ADJUSTMENT.format(comment=decision.comment)
        if not decision:
            self._run_meta = {"decision": "denied", "ok": False, "output": "", **diff_meta}
            return WRITE_DENIED
        result = files.commit(plan)
        self.echo(result)
        self._run_meta = {"decision": "approved", **diff_meta}
        return result

    def _parse_cd(self, command: str) -> str | None:
        """Detect a bare `cd <dir>`. For the user (! prefix) it changes agent
        state; from the model it is rejected with guidance — execution is
        stateless. Compound forms (cd x && ...) run normally as subshells."""
        if any(ch in command for ch in ";&|<>`$(){}"):
            return None
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None
        if not tokens or tokens[0] != "cd" or len(tokens) > 2:
            return None
        return tokens[1] if len(tokens) == 2 else "~"

    def _change_dir(self, target: str) -> str:
        path = os.path.expanduser(target)
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(self.cwd, path))
        if not os.path.isdir(path):
            note = f"ERROR: no such directory: {path}"
            self.echo(note)
            return note
        self.cwd = path
        # Return the note (callers surface it), but don't echo it — a cwd move is
        # shown by the workspace timeline marker (web) / the CLI /cd print, not a
        # grey echo line (#94/#95 cleanup).
        return f"[working directory is now {path}]"
