"""The agent loop: model proposes tool calls, we execute them (gated), repeat.

The model never executes anything itself — Ollama only returns structured
tool_call requests. _dispatch() is the single execution point, and
run_command cannot be reached there unless the approve() callback returns
the command to run (possibly edited by the user).
"""

import datetime
import getpass
import os
import platform
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

import ollama

from . import files, skills, tools, web
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

STOPPED_LIMIT = (
    "(stopped: hit the max-steps limit — say 'continue' to keep going, or "
    "raise --max-steps)"
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


def _with_feedback(base: str, comment: str) -> str:
    return base + FEEDBACK_NOTE.format(comment=comment) if comment else base


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
    ):
        self.model = model
        self.provider = "ollama"  # callers overwrite after construction (cli/server)
        self.task_sources: list[dict] = []  # pages read_url fetched for the current task
        self.approve = approve
        self.approve_write = approve_write
        self.approve_read = approve_read
        self.echo = echo
        self.stream = stream
        self.chat = client_chat
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
        self._run_meta: dict | None = None
        self._cancel = threading.Event()
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
        for _ in range(self.max_steps):
            if self._cancel.is_set():
                return self._finish_cancelled()
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
            warn = stuck = False
            for call, result in zip(tool_calls, results, strict=True):
                self._append(
                    {"role": "tool", "tool_name": call["function"]["name"], "content": result}
                )
                self._collect_source(call, result)
                key = self._call_key(call, result)
                repeats[key] = count = repeats.get(key, 0) + 1
                if count >= LOOP_STOP_REPEATS:
                    stuck = True
                elif count == LOOP_WARN_REPEATS:
                    warn = True  # injected below: never between a turn's results
            if stuck:
                self.echo("✕ loop detected: identical call, identical output — stopping")
                return self._finish_stopped(LOOP_STOP_NOTE, STOPPED_LOOP)
            if warn:
                self.echo("⚠ repeated identical tool call — nudging the model to change approach")
                self._append(
                    {"role": "user", "content": LOOP_WARNING.format(count=LOOP_WARN_REPEATS)}
                )

        self.echo("⚠ step limit reached — asking the model to wrap up")
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
        kwargs = dict(
            model=self.model,
            messages=self.messages,
            tools=tools.TOOL_SCHEMAS,
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

    def run_user_command(self, command: str) -> str:
        """A command the user typed directly (! prefix): no approval needed,
        but recorded in the conversation so the model has the context.
        !cd is an alias for /cd — the user moving the directory always means
        moving the project, so cwd and the primary root travel together and
        the model's anchor stays coherent."""
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

    def rebase(self, target: str) -> str:
        """User-typed /cd (and its alias !cd): move cwd AND re-anchor the
        primary session root. Never reachable by the model — that's what
        keeps root scoping honest."""
        result = self._change_dir(target)
        if result.startswith("ERROR"):
            return result
        self.roots[0] = Path(self.cwd).resolve()
        self.echo(f"[session root re-anchored to {self.roots[0]}]")
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
            if name in READ_ONLY_TOOLS and not self._read_needs_prompt(name, args)
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
        return self._read_file_call(args)  # read_file

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

        if name == "run_command":
            command = str(args.get("command", ""))

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
            return result

        return f"ERROR: unknown tool '{name}'"

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
        note = f"[working directory is now {path}]"
        self.echo(note)
        return note
