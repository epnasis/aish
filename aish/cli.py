"""Interactive CLI: one-shot task from argv, or a REPL keeping conversation state."""

import argparse
import datetime
import difflib
import json
import os
import re
import sys
import threading
import time
import tomllib
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from . import aliases, backends, browser, term_image, tools
from .agent import (
    Agent,
    ModelUnavailable,
    environment_context,
    feedback_prompt,
    format_tokens,
    learn_prompt,
)
from .approval import (
    DEFAULT_ALLOWLIST,
    DEFAULT_DENYLIST,
    Blocked,
    check_denied,
    escaping_dirs,
    is_auto_approvable,
    load_prefixes,
    looks_destructive,
    prefix_suggestions,
    save_prefix,
)
from .embeddings import SemanticIndex
from .session import SessionInfo, SessionLog, attachment_names, strip_attachment_notes
from .skills import GLOBAL_SKILLS_DIR

if TYPE_CHECKING:
    from .claude_max import ClaudeMaxAgent

BOLD = "\033[1m"
# Bright black, not ANSI faint (\033[2m): faint is nearly unreadable on many
# dark terminal themes, while bright black stays legible on dark and light.
DIM = "\033[90m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"

ECHO_PREVIEW_LINES = 12
REPLAY_TOOL_LINES = 4

# Quick-reply chips (#167): the model ends a question with markdown links of
# the form [Label](aish-reply://answer text) — the web UI renders each as a tap
# button; the CLI surfaces them as a numbered menu the user picks by number.
# The pattern mirrors app.js's INLINE_RE exactly: label is [^\]\n]+, the
# url-encoded payload is [^)\n]*, both single-line.
_CHIP_RE = re.compile(r"\[([^\]\n]+)\]\(aish-reply://([^)\n]*)\)")
_CHIP_MID = "(aish-reply://"  # the fixed run after the label's closing ']'


def _decode_reply(payload: str, label: str) -> str:
    """Decode a chip payload the same way app.js's quickReplyChip does:
    trim, percent-decode, fall back to the raw payload then to the label."""
    reply = (payload or "").strip()
    try:
        decoded = urllib.parse.unquote(reply)
        if decoded:
            reply = decoded
    except Exception:
        pass
    return reply or label.strip()


def parse_reply_chips(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split quick-reply chips out of an answer. Returns the answer with the
    chip link syntax removed and the list of (label, decoded reply) chips."""
    chips: list[tuple[str, str]] = []

    def take(match: "re.Match[str]") -> str:
        chips.append((match.group(1).strip(), _decode_reply(match.group(2), match.group(1))))
        return ""

    clean = _CHIP_RE.sub(take, text)
    # Chips usually sit on their own trailing lines; stripping them leaves blank
    # lines behind — collapse runs of blank lines and trim the tail.
    clean = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", clean).rstrip()
    return clean, chips


def _chip_hold_index(buf: str) -> int:
    """Index from which the tail of `buf` might still be forming a chip (so a
    streaming caller holds it back); len(buf) when nothing could be. Only text
    from an ambiguous '[' onward is held — normal text streams unchanged."""
    start = 0
    while True:
        bracket = buf.find("[", start)
        if bracket < 0:
            return len(buf)
        if _is_chip_prefix(buf[bracket:]):
            return bracket
        start = bracket + 1


def _is_chip_prefix(s: str) -> bool:
    """True when `s` (which starts with '[') could still grow into a full chip.
    Structure: '[' label ']' '(aish-reply://' payload ')'."""
    if "\n" in s:  # chips are single-line
        return False
    if "]" not in s:  # still typing the label
        return True
    idx = s.index("]")
    if idx == 1:  # empty label — [^\]\n]+ needs at least one char
        return False
    rest = s[idx + 1:]  # should become '(aish-reply://' + payload
    if len(rest) <= len(_CHIP_MID):
        return _CHIP_MID.startswith(rest)
    if not rest.startswith(_CHIP_MID):
        return False
    return ")" not in rest[len(_CHIP_MID):]  # a ')' would have closed the chip


class ChipStream:
    """Token-stream filter that strips aish-reply chip syntax as it streams, so
    the raw link markup never flashes in the terminal. Holds back only the tail
    that could still be forming a chip; everything else prints immediately."""

    def __init__(self, emit):
        self._emit = emit
        self._buf = ""

    def feed(self, token: str) -> None:
        self._buf += token
        # Drop any complete chips, emitting the text around them.
        while True:
            match = _CHIP_RE.search(self._buf)
            if not match:
                break
            if match.start():
                self._emit(self._buf[: match.start()])
            self._buf = self._buf[match.end():]
        # Emit everything that cannot be part of a still-forming chip.
        hold = _chip_hold_index(self._buf)
        if hold:
            self._emit(self._buf[:hold])
            self._buf = self._buf[hold:]

    def close(self) -> None:
        """Flush the held tail (a chip candidate that never completed)."""
        if self._buf:
            self._emit(self._buf)
            self._buf = ""

SLASH_COMMANDS = (
    "/add-dir", "/aliases", "/browser", "/cd", "/clear", "/delete", "/dir-add",
    "/exit", "/feedback", "/help", "/jobs", "/learn", "/model", "/new", "/quit",
    "/rename", "/resume", "/session",
)

SLASH_HELP = f"""{BOLD}commands{RESET} {DIM}(Tab completes; prefixes work, /res = /resume):{RESET}
  {CYAN}/resume{RESET}        switch to an earlier session — its conversation, log file,
                 model and working directory. Type to filter by title, contents,
                 and model (exact match first, then phrase, words, fuzzy),
                 ↑/↓ select, Enter switches, Esc cancels; the chat you leave is
                 untouched and can be resumed back
  {CYAN}/resume <n>{RESET}    switch to the n-th newest session directly
  {CYAN}/resume <text>{RESET} open the picker with the filter pre-filled
  {CYAN}/delete [n|text]{RESET} delete an earlier session permanently (same picker and
                 argument forms as /resume, then a y/N confirm; removes the
                 conversation AND its command audit log — the current
                 session cannot be deleted)
  {CYAN}/rename <title>{RESET} give this chat a custom title (overrides the one derived
                 from the first message; shown in /resume and the web drawer)
  {CYAN}/new, /clear{RESET}   fresh conversation in a new session file (clears the screen;
                 plain 'clear' works too)
  {CYAN}/model [name]{RESET}  switch the model (Ollama name, or a cloud model: gemini:/
                 openai:/claude: — bare provider name picks a default); no
                 arg opens a searchable picker of local + cloud models
                 (typing provider:model there offers that exact model);
                 add --save to persist as the startup default (config.toml),
                 /model --save alone persists the current model
  {CYAN}/cd <dir>{RESET}      move the session to another project: changes the working
                 directory AND re-anchors the auto-approval root there
                 (Tab completes directories); !cd <dir> does the same
  {CYAN}/add-dir <dir>{RESET} allow auto-approved reads/commands in another directory
                 tree too (alias /dir-add); no arg lists current roots
  {CYAN}/learn [hint]{RESET}  distill this conversation into saved skills/memory (the
                 model searches existing knowledge first and you approve
                 each write); /learn lessons migrates the legacy lessons.md
  {CYAN}/feedback [text]{RESET} file a bug/idea as a GitHub issue on epnasis/aish —
                 aish drafts it, you approve, it creates the issue
  {CYAN}/browser [url]{RESET}  open a real Chrome window on aish's own persistent profile
                 so you can sign in to a site; the session is remembered, so
                 later reads of it are made as you (and each one asks first).
                 No arg shows the profile and which sites you are signed into;
                 {CYAN}/browser forget <host>{RESET} drops one, {CYAN}/browser close{RESET}
                 shuts it down
  {CYAN}/aliases{RESET}       list command aliases (config.toml [aliases]); expanded on
                 the first word before the approval gate — {CYAN}/aliases import{RESET}
                 pulls your zsh aliases in (existing entries are kept)
  {CYAN}/jobs{RESET}          list background jobs started this session
  {CYAN}/session{RESET}       show this session's JSONL log path (+ message count)
  {CYAN}/help{RESET}          this help
  {CYAN}/quit, /exit{RESET}   quit (plain 'exit' works too)
{BOLD}input:{RESET} Enter submits · newline: Ctrl+J, end line with \\, or Option+Enter
(iTerm2: set Option=Esc+) · pasted newlines are kept · @ mentions a project
file (type to filter, Tab/Enter completes) · !<cmd> runs directly
without the model · !cd <dir> = /cd (moves the project)
{BOLD}while a command runs:{RESET} Ctrl-C cancels it · Ctrl-B detaches it to a background
job (keeps running, frees the prompt; see /jobs)"""

LOGO_LINES = ("▄▀█ █ █▀ █░█", "█▀█ █ ▄█ █▀█")


def banner(info: str) -> str:
    """Two-line half-block wordmark with a dim info line beside its base."""
    top, bottom = LOGO_LINES
    white = "\033[1;97m"
    return f"{white}{top}{RESET}\n{white}{bottom}{RESET}  {DIM}{info}{RESET}"

# BoxPrompt instance when stdin is a TTY; None means plain input() fallback.
_box = None


class LogRef:
    """Mutable indirection so /new can swap the session log everywhere at once."""

    def __init__(self, log: SessionLog):
        self.log = log
        # The id the NEXT logged user message should carry (#202). The web server
        # mints it when it emits the turn's `user` event, so the live transcript
        # and the log name the same turn and a message can be removed the moment
        # it is sent — the case the feature exists for. Unset elsewhere (the CLI,
        # a resumed history), where SessionLog.message mints its own.
        self.pending_turn: str | None = None
        # The id of the last assistant record written (#229) — what the web
        # server puts on the `done` event so a live answer's Fork button names
        # the same record a replayed one does. Reset at the start of every task,
        # so a turn that produced no answer (cancelled, failed) can never hand
        # the fork the PREVIOUS turn's id.
        self.last_answer_id: str = ""

    def message(self, message: dict) -> None:
        if message.get("role") == "user" and self.pending_turn:
            message = {**message, "turn": self.pending_turn}
            self.pending_turn = None
        written = self.log.message(message)
        # Non-empty is the same test `reconstruct_events` applies to decide what
        # counts as something the model SAID — so the id the live path publishes
        # names exactly the record a cold replay would promote to the answer.
        if message.get("role") == "assistant" and (message.get("content") or "").strip():
            self.last_answer_id = written

    def command(self, command: str, decision: str, intent: str = "") -> None:
        self.log.command(command, decision, intent)

    def set_title(self, title: str) -> None:
        self.log.set_title(title)

    def model(self, spec: str) -> None:
        self.log.model(spec)

    def origin(self, origin: str) -> None:
        self.log.origin(origin)

    def step(self, step: dict) -> None:
        self.log.step(step)

    def task_start(self, prompt: str) -> None:
        self.log.task_start(prompt)

    def task_end(self, status: str = "ok", error: str = "") -> None:
        self.log.task_end(status, error)

    def command_event(self, event: dict) -> None:
        self.log.command_event(event)

    def workspace(self, record: dict) -> None:
        self.log.workspace(record)

    def rewind_last_turn(self) -> bool:
        return self.log.rewind_last_turn()

    def rating(self, turn: str, rating: str, comment: str = "") -> None:
        self.log.rating(turn, rating, comment)

    def redact_turn(self, turn: str):
        return self.log.redact_turn(turn)


def load_config(path: Path) -> dict:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        print(f"{YELLOW}warning: ignoring invalid config {path}: {exc}{RESET}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def edit_line(initial: str) -> str:
    """Line editing with the command pre-filled."""
    if _box is not None:
        return _box.edit(initial)
    try:
        import readline

        readline.set_startup_hook(lambda: readline.insert_text(initial))
        try:
            return input(f"{YELLOW}edit>{RESET} ").strip()
        finally:
            readline.set_startup_hook(None)
    except ImportError:
        return input(f"{YELLOW}edit ({initial})>{RESET} ").strip()


# Printed when 'a'/'s' has nothing to offer. A command the gate cannot model,
# or one whose paths escape the session roots, is refused BEFORE the allowlist
# is read, so a prefix saved for it would sit in allow.txt doing nothing — and
# saying so is the whole point of #265, where the button looked like it worked.
NO_PREFIX_NOTE = (
    "no saved prefix can silence this one — the gate refuses it before it "
    "reads the allowlist. Approving just this once."
)


def allow_segments_flow(command: str, allow_path: Path) -> bool:
    """'always allow' asks about each unvetted chained segment independently.
    False when there was nothing worth saving."""
    suggestions = prefix_suggestions(command, load_prefixes(allow_path))
    if not suggestions:
        print(f"{DIM}  {NO_PREFIX_NOTE}{RESET}")
        return False
    saved = False
    for suggestion in suggestions:
        answer = input(
            f"{YELLOW}always allow prefix{RESET} [{BOLD}{suggestion}{RESET}] "
            f"(enter=yes, s=skip, or type a different prefix): "
        ).strip()
        if answer.lower() == "s":
            continue
        save_prefix(allow_path, answer or suggestion)
        print(f"{DIM}  saved: {answer or suggestion} → {allow_path}{RESET}")
        saved = True
    return saved


def print_intent(said: str) -> None:
    """The model's stated reason, above a gate prompt (#252).

    Attributed and dimmed so it reads as a CLAIM standing beside the command,
    never as part of it — the terminal's version of the web card's separate
    section. Printed whole: the 120-character snippet the trace already keeps
    cuts this exact sentence at an abbreviation and loses the entire reason,
    which is the failure the feature exists for.
    """
    if not said:
        return
    lines = said.strip().splitlines()
    print(f"\n{DIM}  \u25b8 aish says: {lines[0]}{RESET}")
    for line in lines[1:]:
        print(f"{DIM}    {line}{RESET}")


def make_approver(
    ask_all: bool,
    allow_path: Path,
    log,
    deny_path: Path = DEFAULT_DENYLIST,
    get_scope=None,
    get_session_prefixes=None,
    trust_dir=None,
    get_intent=None,
):
    """get_scope() -> (cwd, roots): the agent's live directory scope, bound
    late because the agent is constructed after its approver. When present,
    auto-approval is confined to the session roots. get_session_prefixes() ->
    the LIVE set of prefixes allowed for this session only ('s' at the prompt),
    unioned with the persistent allowlist but never written to disk; it is owned
    by the AGENT beside roots (#176) and late-bound for the same reason, so a
    session switch resets both halves of the gate's session scope by
    construction. Unwired (no agent), the approver owns a set of its own and the
    allowance lasts exactly as long as the approver. trust_dir(path) -> note
    widens the live roots when the user answers 't' on a command that escapes
    them (also late-bound, in-memory only). get_intent() -> what the model said
    it was doing on the step that proposed this command (#252), printed above
    the prompt and recorded with the decision — the terminal gate shows the same
    reason the web card does, because the two gate identically."""
    own_prefixes: set[str] = set()

    def session_prefixes() -> set[str]:
        return get_session_prefixes() if get_session_prefixes else own_prefixes

    def known_prefixes() -> frozenset:
        return frozenset(load_prefixes(allow_path)) | session_prefixes()

    def intent() -> str:
        return (get_intent() if get_intent else "") or ""

    def record(command: str, decision: str) -> None:
        if log:
            log.command(command, decision, intent())

    def ask_approval(command: str) -> str | Blocked | None:
        # Denylist first: unrecoverable commands never reach the prompt and
        # the allowlist can never bypass this.
        reason = check_denied(command, load_prefixes(deny_path))
        if reason:
            print(f"\n{RED}✗ blocked ({reason}):{RESET}\n  {BOLD}{command}{RESET}")
            print(f"{DIM}  run it yourself with !{command}  if you truly mean it{RESET}")
            record(command, f"blocked: {reason}")
            return Blocked(reason)

        cwd, roots = get_scope() if get_scope else (None, None)
        if not ask_all and is_auto_approvable(
            command, known_prefixes(), cwd=cwd, roots=roots
        ):
            print(f"\n{GREEN}✓ auto-approved:{RESET} {BOLD}{command}{RESET}")
            record(command, "auto")
            return command

        print_intent(intent())
        warning = f" {RED}⚠ destructive{RESET}" if looks_destructive(command) else ""
        print(f"\n{YELLOW}{BOLD}▶ run command?{RESET}{warning}\n  {BOLD}{command}{RESET}")
        escapes = escaping_dirs(command, cwd, roots) if trust_dir and cwd and roots else []
        if escapes:
            print(f"{YELLOW}  ⚠ outside the session roots:{RESET} {', '.join(escapes)}")
        options = (
            "[y/N/a(lways)/s(ession)/t(rust dir)/e(dit)]"
            if escapes
            else "[y/N/a(lways)/s(ession)/e(dit)]"
        )
        try:
            answer = input(f"{YELLOW}{options}{RESET} ").strip().lower()
        except EOFError:
            record(command, "denied")
            return None

        if answer in ("y", "yes"):
            record(command, "approved")
            return command
        if answer == "t" and escapes:
            for directory in escapes:
                print(f"{DIM}  {trust_dir(directory)}{RESET}")
            record(command, f"approved+trusted:{','.join(escapes)}")
            return command
        if answer == "a":
            saved = allow_segments_flow(command, allow_path)
            record(command, "approved+allowlisted" if saved else "approved")
            return command
        if answer == "s":
            suggestions = prefix_suggestions(command, known_prefixes())
            if not suggestions:
                print(f"{DIM}  {NO_PREFIX_NOTE}{RESET}")
            saved = False
            for suggestion in suggestions:
                typed = input(
                    f"{YELLOW}allow prefix for THIS SESSION{RESET} "
                    f"[{BOLD}{suggestion}{RESET}] "
                    f"(enter=yes, s=skip, or type a different prefix): "
                ).strip()
                if typed.lower() == "s":
                    continue
                session_prefixes().add(typed or suggestion)
                print(f"{DIM}  session-allowed: {typed or suggestion}{RESET}")
                saved = True
            record(command, "approved+session" if saved else "approved")
            return command
        if answer == "e":
            edited = edit_line(command)
            if edited:
                # The denylist stays authoritative even for an edit — otherwise
                # `ls` could be edited into `rm -rf /` and run unchecked.
                reason = check_denied(edited, load_prefixes(deny_path))
                if reason:
                    print(f"\n{RED}✗ blocked ({reason}):{RESET}\n  {BOLD}{edited}{RESET}")
                    print(f"{DIM}  run it yourself with !{edited}  if you truly mean it{RESET}")
                    record(f"{command} => {edited}", f"blocked: {reason}")
                    return Blocked(reason)
                record(f"{command} => {edited}", "edited")
                return edited
            record(command, "denied")
            return None
        record(command, "denied")
        return None

    return ask_approval


def colorize_diff(diff: str) -> str:
    out = []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            out.append(f"{BOLD}{line}{RESET}")
        elif line.startswith("+"):
            out.append(f"{GREEN}{line}{RESET}")
        elif line.startswith("-"):
            out.append(f"{RED}{line}{RESET}")
        elif line.startswith("@@"):
            out.append(f"{CYAN}{line}{RESET}")
        else:
            out.append(f"{DIM}{line}{RESET}")
    return "\n".join(out)


# C1 too (0x80–0x9f): 0x9b IS a CSI on a terminal reading 8-bit controls, so
# stripping only C0 leaves the single-byte form of the escape it removes.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _plain(text: str) -> str:
    """Text that cannot repaint the terminal it is printed into. Newlines and
    tabs survive; every other control character, escape sequences included,
    becomes a visible marker rather than an instruction to the terminal."""
    return _CONTROL_CHARS.sub("\ufffd", text)


def make_write_approver(log, get_intent=None):
    def approve_write(plan) -> bool:
        verb = "create" if plan.is_new else "edit"
        said = (get_intent() if get_intent else "") or ""
        print_intent(said)
        if plan.rule:
            # A rule is not a file, to the person approving it.
            head = {"Created": "new rule", "Updated": "rule change",
                    "Retired": "retire rule"}.get(plan.rule_verb, "rule")
            print(f"\n{YELLOW}{BOLD}▶ {head}?{RESET} {BOLD}{plan.rule}{RESET}")
        else:
            print(f"\n{YELLOW}{BOLD}▶ {verb} file?{RESET} {BOLD}{plan.target}{RESET} "
                  f"{DIM}(+{plan.added} -{plan.removed}){RESET}")
        if plan.note:
            # Above the diff, because for a rule the diff is YAML the owner did
            # not write and the note is what he is actually agreeing to.
            # Sanitised: the note quotes the model's description and the owner's
            # own past prompts (which in a triggered session came from an email),
            # and an escape sequence reaching a terminal can repaint the very
            # approval card it is printed inside. The web card is safe by
            # construction (textContent); a terminal has no such property.
            print(f"{BOLD}{_plain(plan.note)}{RESET}\n")
        if plan.diff.strip():
            print(colorize_diff(plan.diff))
        else:
            print(f"{DIM}(no textual changes){RESET}")
        try:
            answer = input(f"{YELLOW}[y/N]{RESET} ").strip().lower()
        except EOFError:
            answer = ""
        approved = answer in ("y", "yes")
        if log:
            log.command(
                f"{verb} {plan.target}", "approved" if approved else "denied", said
            )
        return approved

    return approve_write


def make_import_approver(log, get_intent=None):
    """Consolidated review for a skill import (#139): the WHOLE skill in one
    decision — name, description, risk flags, and every file's contents — so
    untrusted code is reviewed properly, not rubber-stamped file-by-file."""

    def approve_import(name, description, files, skipped, flags, dest) -> bool:
        said = (get_intent() if get_intent else "") or ""
        print_intent(said)
        print(f"\n{YELLOW}{BOLD}▶ import skill?{RESET} {BOLD}{name}{RESET}")
        print(f"{DIM}  {description}{RESET}")
        print(f"{DIM}  → {dest}  ({len(files)} files){RESET}")
        if flags:
            print(f"{RED}  ⚠ review closely:{RESET}")
            for flag in flags:
                print(f"{RED}    • {flag}{RESET}")
        if skipped:
            print(f"{DIM}  (binary assets skipped: {', '.join(skipped)}){RESET}")
        for f in files:
            print(f"\n{CYAN}{BOLD}── {f['path']}{RESET}"
                  f"{DIM}{'  (executable)' if f.get('executable') else ''}{RESET}")
            print(f["content"].rstrip("\n"))
        try:
            answer = input(f"\n{YELLOW}install this skill? [y/N]{RESET} ").strip().lower()
        except EOFError:
            answer = ""
        approved = answer in ("y", "yes")
        if log:
            log.command(
                f"import skill {name}", "approved" if approved else "denied", said
            )
        return approved

    return approve_import


def make_tool_approver(log, get_intent=None):
    """Gate a mutating plugin tool call (issue #141). Reuses the command
    prompt's shape — the tool name + its structured args stand in for a shell
    string — but there is no denylist or auto-approval: a mutating tool always
    prompts. CLI stays y/N (the comment/adjust verdicts are web-card-only)."""

    def approve_tool(name: str, args: dict, preview: "str | None" = None) -> bool:
        shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
        said = (get_intent() if get_intent else "") or ""
        print_intent(said)
        print(f"\n{YELLOW}{BOLD}▶ run tool?{RESET} {BOLD}{name}{RESET}({shown})")
        if preview:
            print(f"{DIM}  {preview}{RESET}")
        try:
            answer = input(f"{YELLOW}[y/N]{RESET} ").strip().lower()
        except EOFError:
            answer = ""
        approved = answer in ("y", "yes")
        if log:
            log.command(
                f"tool {name}({shown})", "approved" if approved else "denied", said
            )
        return approved

    return approve_tool


def make_read_approver(log, trust_dir=None, get_intent=None):
    """Prompt before an auto-approved read_file touches a secret-bearing path
    or one outside the session roots, so an injected read_file can't silently
    pull keys — or arbitrary files elsewhere on the machine — into context.
    For out-of-root reads, 't' trusts the file's directory for the session
    (via trust_dir, same late binding as make_approver's)."""

    def approve_read(path: str, reason: str = "sensitive") -> bool:
        said = (get_intent() if get_intent else "") or ""
        print_intent(said)
        offer_trust = reason == "outside" and trust_dir is not None
        if reason == "outside":
            print(f"\n{YELLOW}{BOLD}▶ read file outside the project?{RESET} "
                  f"{BOLD}{path}{RESET} {DIM}(/cd or /add-dir widens the scope){RESET}")
        else:
            print(f"\n{YELLOW}{BOLD}▶ read sensitive file?{RESET} {BOLD}{path}{RESET} "
                  f"{RED}⚠ may contain secrets{RESET}")
        options = "[y/N/t(rust dir)]" if offer_trust else "[y/N]"
        try:
            answer = input(f"{YELLOW}{options}{RESET} ").strip().lower()
        except EOFError:
            answer = ""
        if answer == "t" and offer_trust:
            directory = os.path.dirname(os.path.expanduser(path)) or "."
            print(f"{DIM}  {trust_dir(directory)}{RESET}")
            if log:
                log.command(f"read {path}", f"approved+trusted:{directory}", said)
            return True
        approved = answer in ("y", "yes")
        if log:
            log.command(f"read {path}", "approved" if approved else "denied", said)
        return approved

    return approve_read


# LiveTimer when interactive; echo prints through it so a line landing while
# the ticker runs erases the ticker frame first instead of gluing onto it.
_timer = None


def echo(text: str) -> None:
    lines = text.splitlines()
    shown = lines[:ECHO_PREVIEW_LINES]
    out = DIM + "\n".join(f"  {line}" for line in shown) + RESET
    if len(lines) > ECHO_PREVIEW_LINES:
        out += f"\n{DIM}  … ({len(lines) - ECHO_PREVIEW_LINES} more lines fed to model){RESET}"
    if _timer is not None:
        _timer.println(out)
    else:
        print(out)


def stream_line(line: str) -> None:
    print(f"{DIM}  {line}{RESET}")


class LiveTimer:
    """One dim '✻ label… Ns · ↓ N tokens' line redrawn in place while a phase
    runs.

    start() paints immediately and spawns a ticker thread; stop() joins it and
    erases the line, so the caller may print the moment stop() returns. The
    agent guarantees stop() is called before any prompt or streamed token, and
    feeds add_tokens() as generation chunks arrive.
    """

    TICK_SECS = 0.25

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._label = ""
        self._started = 0.0
        self._tokens = 0

    def _paint(self) -> None:
        secs = int(time.perf_counter() - self._started)
        elapsed = f"{secs}s" if secs < 60 else f"{secs // 60}m{secs % 60:02d}s"
        line = f"✻ {self._label}… {elapsed}"
        if self._tokens:
            line += f" · ↓ {format_tokens(self._tokens)} tokens"
        with self._lock:
            print(f"\r\033[K{DIM}  {line}{RESET}", end="", flush=True)

    def println(self, text: str) -> None:
        """Print a full line while the ticker may be running: erase the
        current ticker frame first so the two never share a line."""
        with self._lock:
            print(f"\r\033[K{text}")

    def start(self, label: str) -> None:
        self.stop()
        self._stop_event.clear()
        self._label = label
        self._started = time.perf_counter()
        self._tokens = 0
        self._paint()

        def tick():
            while not self._stop_event.wait(self.TICK_SECS):
                self._paint()

        self._thread = threading.Thread(target=tick, daemon=True)
        self._thread.start()

    def add_tokens(self, count: int) -> None:
        self._tokens += count

    def note(self, text: str) -> None:
        pass  # the CLI ticker shows phase + timer only; gists are a web thing

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self._stop_event.set()
        thread.join(timeout=1)
        with self._lock:
            print("\r\033[K", end="", flush=True)


def read_task(cwd: str) -> str:
    """Boxed prompt (rules hugging the input, expanding with multiline entry);
    plain prompt when stdin is piped."""
    home = str(Path.home())
    display = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    if _box is None:
        return input(f"\naish:{display}> ")
    print()
    return _box.read(display)


def session_row(info: SessionInfo) -> str:
    """Picker row for a session: when, size, model (if recorded), title."""
    model = f" · {info.model}" if info.model else ""
    return f"{info.when} · {info.count:>3} msgs{model} · {info.title}"


def print_sources(agent) -> None:
    """Dim list of the pages the answer was based on (web tasks only)."""
    sources = getattr(agent, "task_sources", [])
    if not sources:
        return
    print(f"{DIM}Sources:{RESET}")
    for source in sources:
        title = source.get("title")
        line = f"{title} — {source['url']}" if title else source["url"]
        print(f"{DIM}  ↳ {line}{RESET}")


def resolve_chip_selection(task: str, chips: list[tuple[str, str]]) -> str:
    """A bare number picks the matching quick-reply chip's answer text; any
    other input (including an out-of-range number, or when no menu is pending)
    is returned unchanged as a normal message (#167)."""
    if chips and task.isdigit() and 1 <= int(task) <= len(chips):
        return chips[int(task) - 1][1]
    return task


def print_chip_menu(chips: list[tuple[str, str]]) -> None:
    """Numbered list of quick-reply chips under an answer; the user selects one
    by typing its number at the next prompt (#167)."""
    if not chips:
        return
    print(f"{DIM}Quick replies (type a number to send):{RESET}")
    for i, (label, _reply) in enumerate(chips, 1):
        print(f"{DIM}  {i}.{RESET} {label}")


def print_answer_images(agent, answer: str) -> None:
    """Show local images the answer references (![alt](/abs/path.png))
    inline, in terminals that support it (iTerm2, kitty protocol). Elsewhere
    this is a no-op — the path in the answer text is the fallback."""
    protocol = term_image.supports_images()
    if not protocol:
        return
    # The workspace boundary, not roots: show_image stores pictures in aish's own
    # media dir, which is readable but deliberately not a user-granted root (#188).
    for path in term_image.local_image_paths(answer, agent.workspace_roots()):
        term_image.emit(path, protocol)


def replay_history(messages: list[dict]) -> list[tuple[str, str]]:
    """Print a loaded conversation so the user sees what they resumed, and
    return the quick-reply chips still pending from the final answer (#167):
    a resumed session must offer the last turn's chips just like a live one,
    and its chip link syntax is stripped from the printed body, never shown raw.
    A trailing user turn consumes the chips, so it resets them to empty."""
    pending: list[tuple[str, str]] = []
    for message in messages:
        role = message.get("role")
        content = (message.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            pending = []
            # A turn that carried attachments has a line per file appended to it
            # FOR THE MODEL. Resuming a web session in the terminal printed those
            # back as if the user had typed them, absolute uploads path and all —
            # so show what was typed, and name the files as files.
            typed = strip_attachment_notes(content)
            files = attachment_names(content)
            if files:
                typed = f"{typed} {DIM}[{', '.join(files)}]{RESET}" if typed \
                    else f"{DIM}[{', '.join(files)}]{RESET}"
            print(f"\n{BOLD}❯{RESET} {typed}")
        elif role == "assistant":
            content, pending = parse_reply_chips(content)
            print(f"{GREEN}{content}{RESET}")
        else:
            lines = content.splitlines()
            print(DIM + "\n".join(f"  {line}" for line in lines[:REPLAY_TOOL_LINES]) + RESET)
            if len(lines) > REPLAY_TOOL_LINES:
                print(f"{DIM}  … ({len(lines) - REPLAY_TOOL_LINES} more lines){RESET}")
    print_chip_menu(pending)
    return pending


CATALOG_TTL = datetime.timedelta(hours=24)
CATALOG_FETCH_WAIT = 3.0  # seconds the picker will wait for provider APIs


def cloud_model_catalog(state_dir: Path) -> dict[str, list[str]]:
    """{provider: [model ids]} from the providers' list endpoints, for every
    provider with credentials. Fetches run in parallel with a hard wait cap
    (a slow provider is just absent this time) and land in a 24h disk cache
    so the picker usually opens instantly."""
    cache_path = state_dir / "cloud-models.json"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched = datetime.datetime.fromisoformat(cached["fetched"])
        if datetime.datetime.now() - fetched < CATALOG_TTL:
            return cached["models"]
    except (OSError, ValueError, KeyError):
        pass

    results: dict[str, list[str]] = {}

    def fetch(name: str) -> None:
        try:
            ids = backends.list_models(name)
        except Exception:
            return
        if ids:
            results[name] = ids

    threads = [
        threading.Thread(target=fetch, args=(name,), daemon=True)
        for name in backends.PROVIDERS
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + CATALOG_FETCH_WAIT
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    catalog = dict(results)
    if catalog:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {"fetched": datetime.datetime.now().isoformat(), "models": catalog}
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return catalog


def available_models(agent, state_dir: Path | None = None) -> list[tuple[str, str]]:
    """(name, description) pairs for the /model picker: installed Ollama
    models, the cloud providers, and — when credentials + network allow —
    each provider's actual model catalog. Ollama being down just hides the
    local ones."""
    provider_now = getattr(agent, "provider", "ollama")
    models = []
    try:
        import ollama

        listed = ollama.list().models
    except Exception:
        listed = []
    for m in listed:
        name = getattr(m, "model", None)
        if not name:
            continue
        size = (getattr(m, "size", 0) or 0) / 1e9
        current = " · current" if provider_now == "ollama" and agent.model == name else ""
        models.append((name, f"local · {size:.0f} GB{current}"))
    for pname, provider in backends.PROVIDERS.items():
        current = " · current" if provider_now == pname else ""
        models.append((pname, f"cloud · default {provider.default_model}{current}"))
    models.append(("claude-max", "cloud · Claude subscription (restart to switch)"))
    catalog = cloud_model_catalog(state_dir) if state_dir is not None else {}
    for pname, ids in catalog.items():
        label = PROVIDER_LABELS.get(pname, pname)
        for model_id in ids:
            current = " · current" if provider_now == pname and agent.model == model_id else ""
            models.append((f"{pname}:{model_id}", f"cloud · {label}{current}"))
    return models


def rank_models(models: list[tuple[str, str]], query: str) -> list[tuple[str, str]]:
    """Deterministic picker ranking: exact name, name prefix, substring in
    name or description, then fuzzy name similarity. Ties keep list order.

    Multi-word queries match per word ("gem pro" finds gemini-3.5-pro),
    and each word may be a typo (fuzzy against the name/description
    tokens). A query that itself names a cloud model (provider:model)
    becomes a selectable row on top — the static list can't enumerate
    provider catalogs, so the exact string the user typed is the offer."""
    query_cf = " ".join(query.split()).casefold()
    if not query_cf:
        return models
    words = query_cf.split()
    ranked = []
    raw = query.strip()
    provider, sep, rest = raw.partition(":")
    provider = provider.casefold()
    if sep and rest and (provider in backends.PROVIDERS or provider == "claude-max"):
        label = PROVIDER_LABELS.get(provider, provider)
        ranked.append((6, (f"{provider}:{rest}", f"cloud · {label} · this exact model")))
    for model in models:
        name_cf = model[0].casefold()
        hay = f"{name_cf} {model[1].casefold()}"
        if name_cf == query_cf:
            score = 5
        elif name_cf.startswith(query_cf):
            score = 4
        elif query_cf in hay:
            score = 3
        elif all(word in hay for word in words):
            score = 2
        else:
            tokens = frozenset(re.split(r"[^a-z0-9]+", hay)) - {""}
            if all(
                difflib.get_close_matches(word, tokens, n=1, cutoff=0.75) for word in words
            ) or difflib.SequenceMatcher(None, query_cf, name_cf).ratio() >= 0.55:
                score = 1
            else:
                continue
        ranked.append((score, model))
    ranked.sort(key=lambda pair: -pair[0])
    return [model for _, model in ranked]


def switch_model(agent, arg: str, saving: bool = False) -> bool:
    """Point the running agent at another model/backend. Returns True on
    success (so /model --save only persists a model that actually loaded)."""
    crossing_max = arg.startswith("claude-max") or (
        getattr(agent, "provider", "ollama") == "claude-max"
    )
    if crossing_max:
        print(f"{DIM}claude-max runs a different agent loop — restart aish "
              f"(aish --model {arg}) to switch{RESET}")
        return False
    try:
        chat, provider, name = backends.make_chat(arg)
    except backends.BackendError as exc:
        print(f"{RED}{exc}{RESET}")
        return False
    switched_provider = provider != getattr(agent, "provider", "ollama")
    agent.chat = chat
    agent.model = name
    agent.provider = provider
    if saving:
        print(f"{DIM}model switched to {arg}{RESET}")
    else:
        print(f"{DIM}model switched to {arg} (this session — /model --save "
              f"makes it the startup default){RESET}")
    if switched_provider:
        print(f"{DIM}note: the system prompt still describes the startup "
              f"backend — restart aish to refresh its self-description{RESET}")
    return True


def model_spec(agent) -> str:
    """The --model string that recreates the agent's current model, e.g.
    'qwen3:8b' (Ollama is the unprefixed default) or 'gemini:gemini-3.5-pro'."""
    provider = getattr(agent, "provider", "ollama")
    if provider == "ollama":
        return agent.model
    return f"{provider}:{agent.model}" if agent.model else provider


def save_default_model(config_path: Path, spec: str) -> str | None:
    """Persist `model = spec` as the startup default; returns an error string
    or None on success.

    The config is hand-edited TOML, so no TOML writer (it would drop comments):
    only the one top-level `model = ...` line is replaced in place, or inserted
    before the first [table] header. The result is re-parsed before writing so
    a bad edit can never corrupt the config."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        return f"cannot read {config_path}: {exc}"
    lines = text.splitlines()
    new_line = f'model = "{spec}"'
    top_level_end = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines)
    )
    for i in range(top_level_end):
        if re.match(r"\s*model\s*=", lines[i]):
            lines[i] = new_line
            break
    else:
        lines.insert(top_level_end, new_line)
    new_text = "\n".join(lines) + "\n"
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        return f"refusing to write {config_path}: edit would produce invalid TOML ({exc})"
    if parsed.get("model") != spec:
        return f"refusing to write {config_path}: model name {spec!r} does not survive TOML"
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = config_path.with_name(config_path.name + ".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(config_path)
    except OSError as exc:
        return f"cannot write {config_path}: {exc}"
    return None


def parse_learn(task: str, lessons_path=None) -> str | None:
    """/learn [hint] → the distillation prompt (run as a normal task so
    recall and diff approvals apply); None for any other slash input.
    Unambiguous prefixes resolve, matching handle_slash (/lea → /learn)."""
    verb = task.split()[0].lower()
    if verb != "/learn":
        matches = [c for c in SLASH_COMMANDS if c.startswith(verb)]
        if matches != ["/learn"]:
            return None
    return learn_prompt(task.partition(" ")[2], lessons_path)


def parse_feedback(task: str, block_flow: bool = False, attachments: bool = False) -> str | None:
    """/feedback [details] → the feedback-flow prompt (run as a normal task so
    the gh_issue skill, chips, and approval gate all apply); None for any other
    slash input. Unambiguous prefixes resolve, matching handle_slash. block_flow
    (web, text-only) selects the backend-owned `aish-issue` block flow (#110);
    the CLI leaves it False so the model files the issue through the gate.
    attachments (web) adds the public-upload consent rules to the classic
    prompt — the draft lists the assets for confirm/deselect (#130)."""
    verb = task.split()[0].lower()
    if verb != "/feedback":
        matches = [c for c in SLASH_COMMANDS if c.startswith(verb)]
        if matches != ["/feedback"]:
            return None
    return feedback_prompt(
        task.partition(" ")[2], block_flow=block_flow, attachments=attachments
    )


def pick_session(state_dir: Path, arg: str, exclude: set, verb: str) -> SessionInfo | None:
    """Session selection shared by /resume and /delete, so both mean the same
    thing by construction: a live-filter picker when interactive, a one-shot
    numbered flow for `<n>` arguments or piped input. Returns the chosen
    session, or None after printing why (nothing to pick / cancelled)."""
    if not arg.isdigit() and _box is not None:
        # Interactive: live-filter picker — typing re-ranks, Enter selects.
        entries = SessionLog.load_entries(state_dir, exclude=exclude)
        if not entries:
            print(f"{DIM}no earlier session to {verb}{RESET}")
            return None
        selected = _box.pick(
            lambda query: SessionLog.rank(entries, query),
            initial=arg,
            render=session_row,
        )
        if selected is None:
            print(f"{DIM}cancelled{RESET}")
        return selected

    # `<n>` argument, or no TTY (pipes/scripts): one-shot numbered flow.
    searching = bool(arg) and not arg.isdigit()
    if searching:
        sessions = SessionLog.search_sessions(state_dir, arg, exclude=exclude)
        if not sessions:
            print(f"{DIM}no session matches '{arg}'{RESET}")
            return None
    else:
        sessions = SessionLog.list_sessions(state_dir, exclude=exclude)
        if not sessions:
            print(f"{DIM}no earlier session to {verb}{RESET}")
            return None

    if arg.isdigit():
        choice = int(arg)
    elif len(sessions) == 1:
        choice = 1
    else:
        default = "best match" if searching else "latest"
        for i, info in enumerate(sessions, 1):
            row = session_row(info)
            head, _, title = row.rpartition(" · ")
            print(f"{DIM}{i:>3}. {head} ·{RESET} {title}")
        try:
            answer = input(
                f"{YELLOW}{verb} which?{RESET} [1={default}] (number, q=cancel) "
            ).strip().lower()
        except EOFError:
            answer = "q"
        if answer in ("q", "quit"):
            print(f"{DIM}cancelled{RESET}")
            return None
        choice = int(answer) if answer.isdigit() else 1
    if not 1 <= choice <= len(sessions):
        print(f"{DIM}no such session number{RESET}")
        return None
    return sessions[choice - 1]


def print_aliases(agent: "Agent | ClaudeMaxAgent") -> None:
    entries = agent.aliases
    if not entries:
        print(f"{DIM}no aliases — add an [aliases] table to config.toml, or "
              f"run /aliases import{RESET}")
        return
    width = max(len(name) for name in entries)
    for name in sorted(entries):
        print(f"  {CYAN}{name:<{width}}{RESET}  {DIM}{entries[name]}{RESET}")


def import_aliases(agent: "Agent | ClaudeMaxAgent", config_path: Path | None) -> None:
    """Pull the user's real zsh aliases into config + the live agent, keeping
    every existing entry (theirs always wins — imports only ADD)."""
    if config_path is None:
        print(f"{RED}no config path available — cannot import{RESET}")
        return
    imported = aliases.sanitize(aliases.import_from_zsh())
    if not imported:
        print(f"{DIM}nothing imported (zsh returned no parseable aliases){RESET}")
        return
    current = agent.aliases
    new = {name: value for name, value in imported.items() if name not in current}
    if not new:
        print(f"{DIM}all {len(imported)} zsh aliases are already defined — nothing "
              f"to add{RESET}")
        return
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        print(f"{RED}cannot read {config_path}: {exc}{RESET}")
        return
    merged = aliases.merge_into_config_text(text, new)
    try:
        tomllib.loads(merged)  # never write a config we can't read back
    except tomllib.TOMLDecodeError as exc:
        print(f"{RED}refusing to write {config_path}: edit would produce invalid "
              f"TOML ({exc}){RESET}")
        return
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = config_path.with_name(config_path.name + ".tmp")
        tmp_path.write_text(merged, encoding="utf-8")
        tmp_path.replace(config_path)
    except OSError as exc:
        print(f"{RED}cannot write {config_path}: {exc}{RESET}")
        return
    current.update(new)  # live, no restart needed
    print(f"{DIM}imported {len(new)} alias(es) into {config_path}:{RESET}")
    for name in sorted(new):
        print(f"  {CYAN}{name}{RESET}  {DIM}{new[name]}{RESET}")


def handle_slash(
    task: str,
    agent: "Agent | ClaudeMaxAgent",
    logref: LogRef,
    state_dir: Path,
    config_path: Path | None = None,
    chips_out: list[tuple[str, str]] | None = None,
) -> str:
    """Dispatch a /command; returns 'exit' or 'handled'. Unambiguous
    prefixes resolve (/res → /resume); ambiguous ones list the options.
    A /resume fills chips_out with the resumed session's pending quick-reply
    chips (#167) so the REPL loop can offer them, like a live answer."""
    command = task.split()[0].lower()
    if command not in SLASH_COMMANDS:
        matches = [c for c in SLASH_COMMANDS if c.startswith(command)]
        if len(matches) == 1:
            command = matches[0]
        elif matches:
            print(f"{DIM}ambiguous — did you mean {' or '.join(matches)}?{RESET}")
            return "handled"
    if command in ("/quit", "/exit"):
        return "exit"
    if command in ("/new", "/clear"):
        agent.reset()
        # A new chat is a new session, and trusted dirs are session property
        # (#176): it starts with only the current directory in scope. The dirs
        # the previous chat trusted (/add-dir, or "trust this directory"
        # answered at a prompt) belong to that chat and stay with it. cwd is
        # kept — you are still in that directory in this terminal.
        agent.restore_workspace(agent.cwd, [])
        logref.log = SessionLog.new(state_dir)
        logref.model(model_spec(agent))
        print("\033[2J\033[3J\033[H", end="")  # clear screen + scrollback
        print(banner(f"fresh conversation — session {logref.log.path.name}"))
        return "handled"
    if command == "/resume":
        parts = task.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        selected = pick_session(state_dir, arg, {logref.log.path}, "resume")
        if selected is None:
            return "handled"
        # Resume SWITCHES this REPL to that session — the same thing `aish
        # --resume` and the web drawer mean by the word. It used to merge the
        # picked history into the current chat AND copy its messages into the
        # current log, which silently grafted one conversation onto another and
        # left a triggered session's mail thread sitting in an unrelated chat.
        messages, recorded_spec, *_ = SessionLog._parse(selected.path)
        agent.reset()
        agent.load_history(messages)
        logref.log = SessionLog(selected.path)  # keep writing where the chat lives
        # Continue on the model the session last used, like the web's cold open;
        # a model that no longer loads leaves the current one in place.
        if recorded_spec and recorded_spec != model_spec(agent):
            switch_model(agent, recorded_spec)
        logref.model(model_spec(agent))
        # …and in the workspace it left off in (issue #94), not the one the
        # session we just left happened to be sitting in. restore_workspace is
        # authoritative (#176): this REPL reuses one live Agent across the
        # switch, so the roots must become exactly the resumed session's own —
        # its recorded cwd (or the launch dir) plus the dirs it trusted, with
        # the previous chat's /add-dir grants left behind.
        restored_cwd, trusted = SessionLog.restore_state(selected.path)
        agent.restore_workspace(restored_cwd, trusted)
        agent.resume_turns(SessionLog.last_turn(selected.path))
        print(f"{DIM}resumed {len(messages)} messages from {selected.path.name}:{RESET}")
        chips = replay_history(messages)
        if chips_out is not None:
            chips_out[:] = chips
        return "handled"
    if command == "/delete":
        parts = task.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        # The current session is mid-append (its file handle is open) and is
        # excluded outright — /new first, then delete the old one.
        selected = pick_session(state_dir, arg, {logref.log.path}, "delete")
        if selected is None:
            return "handled"
        try:
            answer = input(
                f"{YELLOW}delete '{selected.title}' ({selected.count} msgs)?{RESET} "
                f"removes its history and audit log [y/N] "
            ).strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            print(f"{DIM}cancelled{RESET}")
            return "handled"
        try:
            selected.path.unlink()
        except OSError as exc:
            print(f"{RED}cannot delete {selected.path.name}: {exc}{RESET}")
            return "handled"
        print(f"{DIM}deleted {selected.path.name}{RESET}")
        return "handled"
    if command == "/rename":
        parts = task.split(maxsplit=1)
        title = parts[1].strip() if len(parts) > 1 else ""
        if not title:
            print(f"{DIM}usage: /rename <new title> — gives this chat a custom name{RESET}")
            return "handled"
        logref.set_title(title)
        print(f"{DIM}renamed to '{title}'{RESET}")
        return "handled"
    if command == "/model":
        parts = task.split()[1:]
        save = "--save" in parts
        names = [p for p in parts if not p.startswith("-")]
        unknown = [p for p in parts if p.startswith("-") and p != "--save"]
        if unknown or len(names) > 1:
            print(f"{RED}usage: /model [name] [--save]{RESET}")
            return "handled"
        if names:
            if not switch_model(agent, names[0], saving=save):
                return "handled"
            logref.model(model_spec(agent))
        elif not save:
            if _box is not None:
                # Interactive: same live-filter picker as /resume, over models.
                models = available_models(agent, state_dir)
                selected = _box.pick(
                    lambda query: rank_models(models, query),
                    render=lambda model: f"{model[0]:<28} {model[1]}",
                )
                if selected is None:
                    print(f"{DIM}cancelled — still on {agent.model}{RESET}")
                elif switch_model(agent, selected[0]):
                    logref.model(model_spec(agent))
            else:
                print(f"{DIM}current model: {agent.model} — /model <name> to switch "
                      f"('ollama list' shows local models; gemini:/openai: for cloud); "
                      f"--save makes it the startup default{RESET}")
            return "handled"
        if save:
            if config_path is None:
                print(f"{RED}no config path available — cannot save{RESET}")
                return "handled"
            spec = model_spec(agent)
            error = save_default_model(config_path, spec)
            if error:
                print(f"{RED}{error}{RESET}")
            else:
                print(f"{DIM}saved {spec} as the startup default ({config_path}){RESET}")
                if os.environ.get("AISH_MODEL"):
                    print(f"{YELLOW}note: $AISH_MODEL is set and overrides the config "
                          f"at startup — unset it for the saved default to apply{RESET}")
        return "handled"
    if command == "/cd":
        parts = task.split(maxsplit=1)
        if len(parts) < 2:
            roots = ", ".join(str(r) for r in agent.roots)
            print(f"{DIM}cwd: {agent.cwd} · roots: {roots} — /cd <dir> moves both{RESET}")
            return "handled"
        result = agent.rebase(parts[1].strip())  # ERROR case still echoes itself
        if not result.startswith("ERROR"):
            print(f"{DIM}→ working directory: {agent.cwd}{RESET}")
        return "handled"
    if command in ("/add-dir", "/dir-add"):
        parts = task.split(maxsplit=1)
        if len(parts) < 2:
            for root in agent.roots:
                print(f"{DIM}  root: {root}{RESET}")
            print(f"{DIM}/add-dir <dir> allows auto-approved work there too{RESET}")
            return "handled"
        result = agent.add_root(parts[1].strip())
        color = RED if result.startswith("ERROR") else DIM
        print(f"{color}{result}{RESET}")
        return "handled"
    if command == "/aliases":
        arg = task.partition(" ")[2].strip().lower()
        if arg in ("import", "import-zsh"):
            import_aliases(agent, config_path)
        elif arg:
            print(f"{DIM}usage: /aliases (list) or /aliases import{RESET}")
        else:
            print_aliases(agent)
        return "handled"
    if command == "/browser":
        print(browser.command(task.partition(" ")[2].strip()))
        return "handled"
    if command == "/jobs":
        print(f"{DIM}{tools.jobs_table()}{RESET}")
        return "handled"
    if command == "/session":
        # Just the path (+ a message count), never the raw audit trail: the log
        # can be huge, and it's a local file the user greps/tails themselves.
        path = logref.log.path
        count = len(SessionLog.load_messages(path)) if path.exists() else 0
        print(f"{DIM}session log: {path}{RESET}")
        print(f"{DIM}{count} message(s){RESET}")
        return "handled"
    if command == "/help":
        print(SLASH_HELP)
        return "handled"
    print(f"{DIM}unknown command {command} — try /help{RESET}")
    return "handled"


DEFAULT_LESSONS = Path.home() / ".config" / "aish" / "lessons.md"


def default_workspace(cwd: str) -> str:
    """Never anchor a session at $HOME itself: the session root scopes
    auto-approved reads/commands, and home puts ~/.ssh, ~/.aws, shell
    history, etc. inside that scope. Launching from home re-anchors to a
    dedicated ~/aish workspace (created on first use); any other launch
    directory is respected as-is."""
    try:
        if Path(cwd).resolve() != Path.home().resolve():
            return cwd
        workspace = Path.home() / "aish"
        workspace.mkdir(exist_ok=True)
        return str(workspace)
    except OSError:
        return cwd


PROVIDER_LABELS = {
    "gemini": "Google Gemini",
    "openai": "OpenAI",
    "claude": "Anthropic Claude",
    "claude-max": "Anthropic Claude (subscription)",
}


def identity_context(model: str, provider: str) -> str:
    """The one system-prompt section that depends on where the model runs."""
    if provider == "ollama":
        return (
            f"- YOUR IDENTITY: you are the local model '{model}' running through Ollama "
            "ON THIS MACHINE — you are NOT a cloud service and NOT accessed over any API. "
            "The Ollama process (ollama / llama-server, often ~20+ GB RAM) that the user "
            "sees in `top`/`ps` IS you: it is the server executing your weights right now. "
            "If the user stops Ollama, quits the Ollama app, or runs `killall llama-server` "
            "/ `ollama stop`, THIS SESSION ENDS immediately — you would be killing "
            "yourself. So when the user is hunting memory hogs or asks about that process, "
            "say plainly that it is you; never recommend or run a command that kills it "
            "without first warning that it terminates the current aish session, and let "
            "them decide."
        )
    label = PROVIDER_LABELS.get(provider, provider)
    model_desc = f"the model '{model}'" if model else "a Claude model"
    return (
        f"- YOUR IDENTITY: you are {model_desc}, reached over the {label} "
        "cloud API — you do NOT run on this machine. aish executes approved commands "
        f"locally and sends only this conversation to {label}. PRIVACY: everything "
        "in the conversation — the user's messages, files you read, command output — "
        f"leaves this machine for {label}'s servers, so be conservative about "
        "pulling sensitive local data (keys, credentials, personal files) into "
        "context, and warn the user before reading such files. Local Ollama models "
        "are unrelated to you; stopping Ollama does not affect this session."
    )


def usage_context(
    model: str,
    vi_mode: bool,
    allow_path: Path,
    state_dir: Path,
    config_path: Path,
    deny_path: Path = DEFAULT_DENYLIST,
    lessons_path: Path = DEFAULT_LESSONS,
    provider: str = "ollama",
) -> str:
    """Self-knowledge for the system prompt: aish should be able to explain
    and (via approved commands) reconfigure itself."""
    return f"""\
About aish (you) — use this to answer questions about your own usage:
{identity_context(model, provider)}
- Approval prompt keys: y=run once, n=deny, a=always allow (saves command \
prefixes to {allow_path}; the suggested prefix is the static subcommand path \
— e.g. 'gh issue create', never a blanket 'gh'; chained |/&&/|| segments are \
vetted and allowlisted independently; read-only commands auto-approve), \
s=allow for THIS SESSION only (same prefix flow, kept in memory and \
forgotten on exit), e=edit the command first. \
Auto-approval is confined to the session roots (the launch directory plus \
any the user added): commands whose path arguments point outside them, and \
read_file outside them, prompt even when otherwise read-only or allowlisted. \
Only the user can widen this via /cd or /add-dir — if you need a file \
outside the roots, just try; the user will be prompted.
- File tools: prefer read_file/write_file/edit_file over cat/sed/heredocs for \
working with files. read_file takes optional offset (1-based start line) and \
limit — use it for line ranges instead of `sed -n`/`head`/`tail`, which need \
approval while read_file does not. write_file creates or overwrites; \
edit_file replaces an exact UNIQUE string (include context lines if needed; \
never include the line-number prefixes read_file shows). The user approves a \
colored diff before any write. Do NOT use sed -i or > redirects to edit files.
- Web tools: web_search (DuckDuckGo, no API key) and read_url (fetches a page \
as readable text; 'topic' searches the full text). Both auto-approve as \
read-only, and every query/URL is echoed to the user — but they send data off \
this machine, so never put private local content into them.
- Showing images: you CAN display images. Whenever your answer involves an \
image file the user would want to look at (a chart or plot you generated, a \
downloaded picture), you MUST reference it with markdown image syntax and \
its absolute path — ![caption](/absolute/path.png) — when it is inside the \
session roots; mentioning the path in prose alone does not display it. \
Terminals that support inline graphics (iTerm2, kitty, WezTerm, ghostty) \
then show the image right under your answer; elsewhere the path stays \
visible as text.
- QUICK REPLIES: you CAN turn a question into a pick-by-number menu, and the \
user EXPECTS it. Whenever you end a message with a question whose likely \
answers are a few short options (yes/no, pick-one, a short menu), you MUST \
append one markdown link per option, each on its own line, formatted \
[Label](aish-reply://answer text) — the CLI hides the link syntax and lists \
the labels as a numbered menu the user picks by typing its number, sending \
"answer text" as their reply, so write each payload as a complete, \
ready-to-send message. Asking in prose alone does NOT create the menu; you \
must add the link lines too. Example: after "Proceed with the deploy?" end \
with [Yes, deploy](aish-reply://yes, deploy now) and \
[No, hold off](aish-reply://no, hold off). Only offer chips that are a \
useful next step (a continuation, an alternative, a concrete next action) — \
never a chip whose only purpose is to end the conversation.
- Rate limits: a cloud API has a per-minute ceiling, and your whole \
conversation is resent on every step, so a long task can spend millions of \
input tokens a minute. aish paces itself against that ceiling across every \
chat, learns it from the first 429, and once it knows it, caps how much \
history you carry so several steps fit in a minute. If you are told you are \
waiting on a rate limit, that is aish pacing deliberately — not a hang. A \
quota that is SPENT rather than busy cannot be waited out; say so instead of \
suggesting a retry. The user sets their own tier with \
AISH_RATE_LIMIT_<PROVIDER>="rpm=..,tpm=..", and sees where the tokens went \
with `aish usage` (add --session <name> to see which tool filled a chat's \
context). Long pages and images are what fill it fastest.
- REPL escapes: `!<command>` runs directly without you (no approval); \
`!cd <dir>` is an alias for /cd — it moves the project directory and \
re-anchors the session root. Ctrl-C cancels only the \
running command. Ctrl-D or `exit` quits.
- REPL slash commands (Tab autocompletes them; an unambiguous prefix works, \
e.g. /res for /resume): /resume opens a live picker \
over ALL earlier sessions with start date, message count, and the model each \
session last used (summary = the session's first \
user message): typing filters by title, full contents, and model name \
deterministically (exact title match, then phrase, then all-words, then \
fuzzy — no LLM involved), arrow keys select, Enter switches, Esc cancels; \
/resume <text> \
pre-fills the filter and /resume N switches to the N-th newest directly. \
Resuming SWITCHES this REPL to the chosen session — its conversation, its \
log file, the model it last used and the directory it was working in all \
become the current ones, and the chat being left is untouched (resume it \
back the same way). It never merges two conversations. Session \
files are append-only; /delete opens the same picker to permanently remove \
an earlier session (conversation and audit log, y/N confirm — the current \
session cannot be deleted). /new or /clear (or plain 'clear') starts a \
fresh conversation and clears the screen; /model <name> switches the model \
for this session and /model alone opens the same type-to-filter picker over \
installed Ollama models and the cloud providers (typing provider:model inside \
the picker offers that exact cloud model as a selectable row); adding --save \
persists the choice as the startup default in the config file, and \
/model --save alone persists the current model; /jobs lists \
background jobs; /help lists commands; /quit or /exit quits; /cd <dir> \
moves the project directory AND re-anchors the session root there (user \
only — !cd is its alias; your commands always run in the project \
directory and a bare cd from you is rejected); /add-dir <dir> \
(alias /dir-add) adds another directory tree to the session roots.
- Long-running commands (servers, watchers, big upgrades): set \
background=true on run_command — it detaches, survives aish exiting, and \
logs to a file you can tail with normal commands. The user can also detach a \
command that is already running by pressing Ctrl-B (it becomes a background \
job); Ctrl-C cancels a running command instead.
- Safety denylist: unrecoverable command classes (rm -rf, shred, mkfs, dd \
to raw devices, diskutil erase, git clean -f, git push --force) are blocked \
outright — you cannot run them even with approval. The user can extend the \
list with segment prefixes in {deny_path} and can run blocked commands \
manually with the ! prefix. When blocked, suggest a safer alternative.
- Learning: call the remember tool to save a one-line fact or lesson — do \
this after correcting any mistake, and whenever the user asks you to \
remember something durable. Memory entries live in ~/.config/aish/memory/ \
(one small markdown file each); the most recent are shown in the Memory \
section of your context and the rest are searchable with recall. Legacy \
one-line lessons from {lessons_path} still appear there too. When the user \
asks about your learnings, quote the Memory section or search with recall. \
For longer curated notes the user maintains, ~/.config/aish/AISH.md is \
loaded each session.
- Multiline input: Enter submits; a newline is inserted by Ctrl+J, by ending \
the line with a backslash then Enter, or by Option/Alt+Enter (in iTerm2 only \
with "Left Option key: Esc+"); pasted text keeps its newlines.
- File mentions: '@<path>' in a user message references a file (the prompt \
autocompletes project files after '@'). The path is relative to the working \
directory — when its contents matter to the task, read it with read_file \
before answering.
- Sessions: conversation + command audit trail logged to {state_dir}; \
`aish --resume` opens the same session picker as /resume at launch (piped \
input resumes the most recent session). When the user refers to earlier \
work ("the fix from yesterday", "what went wrong last time"), use the \
recall tool to find and read the relevant past conversation instead of \
asking them to repeat it.
- Config file: {config_path} (TOML). Keys: vi_mode, model, num_ctx, \
max_steps, and an [aliases] table. vi_mode (prompt vi editing) is currently \
{str(vi_mode).lower()}; enable it with the line `vi_mode = true`. The \
[aliases] table maps a command name to an expansion string (e.g. \
`ll = "ls -l"`); aish rewrites a command's FIRST word from it BEFORE the \
approval gate, so the gate still sees the real command. `/aliases` lists \
them; `/aliases import` pulls the user's zsh aliases in. Config is read at \
startup only — changes take effect on the next aish start. CLI flags \
override config; $AISH_MODEL overrides the model key.
- Durable context: an AISH.md file in the working directory or \
~/.config/aish/AISH.md is loaded into your system prompt — the right place \
for host facts and user preferences.
- Skills: markdown playbooks in {GLOBAL_SKILLS_DIR} (global; project-scope \
./.aish/skills/ discovery is disabled pending a per-directory trust \
mechanism), indexed in your context and \
read via the read_skill tool. To create one when the user asks — or when \
you have just learned a procedure worth keeping — write <name>.md there \
with frontmatter lines (name:, description:) between --- markers, then a \
body of workflows, exact commands, gotchas, and safety rules. The \
description MUST state the trigger ("Use when the user asks to …") — it is \
what makes the skill discoverable. The index refreshes every task, so a \
new skill is available immediately, no restart needed.
- Current model: {model} (change via --model, $AISH_MODEL, or config; \
/model <name> --save persists a switch as the startup default).
When the user asks you to change one of your settings, edit the config file \
with a normal shell command (it goes through approval like any command)."""


def load_context_files(cwd: str) -> list[str]:
    """User-curated AISH.md files, fully loaded — deliberately static.
    Lessons/memory are NOT bulk-loaded anymore: they reach the model through
    the capped Memory index and the recall tool (relevance-scoped)."""
    parts = []
    sources = (
        Path.home() / ".config" / "aish" / "AISH.md",
        Path(cwd) / "AISH.md",
    )
    for path in sources:
        try:
            if path.is_file():
                parts.append(f"[context from {path}]\n{path.read_text(encoding='utf-8')}")
        except OSError:
            continue
    return parts


def _backend_hint(agent) -> str:
    provider = getattr(agent, "provider", "ollama")
    if provider == "ollama":
        return " — is Ollama running and not overloaded? (check `ollama ps` / system load)"
    if provider == "claude-max":
        return " — is the claude CLI installed and logged in? (run `claude` then /login)"
    return " — check your API key, network, and the provider's rate limits"


def _secret_cli(args: list[str]) -> int:
    """`aish secret <set|get|list|rm> [NAME]` — manage Keychain-backed secrets
    (issue #142). `set` reads the value via getpass (never echoed, never in
    shell history). Tools reference these by name via a `secrets:` manifest
    field; the value is injected into the wrapper's env, never the model."""
    import getpass

    from . import secrets

    usage = "usage: aish secret <set|get|list|rm> [NAME]"
    if not args:
        print(usage)
        return 2
    cmd, rest = args[0], args[1:]
    if cmd == "list":
        found = secrets.names()
        print("\n".join(found) if found else "(no secrets set)")
        return 0
    if cmd in ("set", "get", "rm") and rest:
        name = rest[0]
        if not secrets.valid_name(name):
            print(f"error: invalid secret name {name!r}")
            return 1
        if cmd == "set":
            value = getpass.getpass(f"value for {name} (hidden): ")
            if not value:
                print("aborted (empty value)")
                return 1
            try:
                secrets.put(name, value)
            except secrets.SecretError as exc:
                print(f"error: {exc}")
                return 1
            print(f"stored secret {name}")
            return 0
        if cmd == "get":
            current = secrets.get(name)
            if current is None:
                print(f"{name}: not set")
                return 1
            print(current)
            return 0
        # rm
        removed = secrets.delete(name)
        print(f"removed {name}" if removed else f"{name}: not set")
        return 0 if removed else 1
    print(usage)
    return 2


def _skill_cli(args: list[str]) -> int:
    """`aish skill <import|approve|list|discard>` — the review-in-your-editor
    path for skill imports (#139). `import` STAGES to a quarantine dir (nothing
    installed); you review the files with your own tools, then `approve` installs.
    For in-session import with an inline review card, just ask aish to import."""
    from . import skill_import, skills

    usage = "usage: aish skill <import REPO [PATH] | approve NAME | list | discard NAME>"
    if not args:
        print(usage)
        return 2
    cmd, rest = args[0], args[1:]
    if cmd == "list":
        staged = skill_import.pending()
        print("\n".join(staged) if staged else "(no skills staged for review)")
        return 0
    if cmd == "import" and rest:
        try:
            sub = rest[1] if len(rest) > 1 else ""
            name, dest, flags = skill_import.stage_to_disk(rest[0], sub)
        except skill_import.SkillImportError as exc:
            print(f"error: {exc}")
            return 1
        print(f"staged {BOLD}{name}{RESET} for review at:\n  {dest}")
        if flags:
            print(f"{RED}  ⚠ review closely:{RESET}")
            for flag in flags:
                print(f"{RED}    • {flag}{RESET}")
        print(f"\nreview the files, then install with:  {BOLD}aish skill approve {name}{RESET}")
        print(f"or drop it with:                      aish skill discard {name}")
        return 0
    if cmd == "approve" and rest:
        try:
            dest = skill_import.install(rest[0], skills.GLOBAL_SKILLS_DIR)
        except skill_import.SkillImportError as exc:
            print(f"error: {exc}")
            return 1
        print(f"installed {rest[0]} → {dest}")
        return 0
    if cmd == "discard" and rest:
        ok = skill_import.discard(rest[0])
        print(f"discarded {rest[0]}" if ok else f"{rest[0]}: not staged")
        return 0 if ok else 1
    print(usage)
    return 2


def _explain_cli(args: list[str]) -> int:
    """`aish explain <session> [turn] [--tools] [--context]` — what governed a turn (#214).

    Pure read: no model call, no rule file opened, nothing that could report how
    aish behaves TODAY instead of how it behaved then (docs/trace-contract.md §0).
    """
    from . import explain as explain_mod

    usage = "usage: aish explain <session-name-or-path> [turn] [--tools] [--context]"
    flags = {"--tools", "--context"}
    show_tools = "--tools" in args
    show_context = "--context" in args
    rest = [a for a in args if a not in flags]
    if not rest:
        print(usage)
        return 2
    turn = None
    if len(rest) > 1:
        try:
            turn = int(rest[1])
        except ValueError:
            print(usage)
            return 2
    matches = explain_mod.resolve(rest[0])
    if not matches:
        print(f"no session log matching {rest[0]!r} in {explain_mod.state_dir()}")
        return 1
    if len(matches) > 1:
        # Never guess: a diagnosis about the wrong chat is worse than no answer.
        print(f"{len(matches)} sessions match {rest[0]!r} — be more specific:")
        for path in matches[-20:]:
            print(f"  {path.name}")
        return 2
    print(
        explain_mod.explain(
            matches[0], turn, show_tools=show_tools, show_context=show_context
        )
    )
    return 0


def _usage_cli(args: list[str]) -> int:
    """`aish usage [--days N] [--week] [--session X] [--json]` — what was spent
    and what filled the context (#262).

    Pure read, like `aish explain`: no model call, no live process state. It
    measures RECORDED usage, which is not the same as a billing statement, and
    it says so rather than letting the first reconciliation against a provider
    console discredit it.
    """
    from . import explain as explain_mod
    from . import usage as usage_mod

    usage_line = "usage: aish usage [--days N] [--week] [--session NAME] [--json]"
    days: int | None = 7
    target = ""
    as_json = "--json" in args
    period = "week" if "--week" in args else "day"
    rest = [a for a in args if a not in ("--json", "--week")]
    while rest:
        flag = rest.pop(0)
        if flag == "--days" and rest:
            value = rest.pop(0)
            if not value.isdigit():
                print(usage_line)
                return 2
            days = int(value)
        elif flag == "--all":
            days = None
        elif flag == "--session" and rest:
            target = rest.pop(0)
        else:
            print(usage_line)
            return 2
    if target:
        matches = explain_mod.resolve(target)
        if not matches:
            print(f"no session log matching {target!r} in {explain_mod.state_dir()}")
            return 1
        if len(matches) > 1:
            # Never guess: a cost report about the wrong chat is worse than none.
            print(f"{len(matches)} sessions match {target!r} — be more specific:")
            for path in matches[-20:]:
                print(f"  {path.name}")
            return 2
        one = usage_mod.scan_session(matches[0])
        print(usage_mod.json_report([one]) if as_json else usage_mod.render_session(one))
        return 0
    sessions = usage_mod.scan(days=days)
    print(usage_mod.json_report(sessions) if as_json else usage_mod.render(sessions, period))
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "secret":
        return _secret_cli(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "skill":
        return _skill_cli(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "explain":
        return _explain_cli(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "usage":
        return _usage_cli(sys.argv[2:])

    config_path = Path(
        os.environ.get("AISH_CONFIG", str(Path.home() / ".config" / "aish" / "config.toml"))
    )
    config = load_config(config_path)

    parser = argparse.ArgumentParser(
        prog="aish",
        description="Local LLM agent that runs CLI commands (with your approval).",
    )
    parser.add_argument("task", nargs="*", help="task to perform; omit for interactive mode")
    default_model = os.environ.get("AISH_MODEL") or config.get("model") or "qwen3.6:35b-a3b"
    parser.add_argument(
        "--model",
        # SUPPRESS instead of the default: argparse itself then records whether
        # the flag was passed (hasattr below). Scanning sys.argv for "--model"
        # missed the abbreviations argparse accepts — `--mo gemini:x` is an
        # explicit flag, and treating it as absent let the resumed session's
        # recorded model silently override what the user asked for (#176).
        default=argparse.SUPPRESS,
        help="Ollama model name, or a cloud model: gemini:<m> / openai:<m> / "
        "claude:<m> (API keys via GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY; "
        "bare provider name picks its default), or claude-max[:opus|sonnet] to run on "
        "a Claude Pro/Max subscription via the claude CLI login. "
        "Default: $AISH_MODEL, config, or qwen3.6:35b-a3b",
    )
    parser.add_argument(
        "--num-ctx", type=int, default=int(config.get("num_ctx", 32768)),
        help="context window tokens",
    )
    parser.add_argument(
        "--max-steps", type=int, default=int(config.get("max_steps", 25)),
        help="max model turns per task",
    )
    parser.add_argument("--think", action="store_true", help="enable model thinking (slow)")
    parser.add_argument(
        "--vi",
        dest="vi_mode",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("vi_mode", False)),
        help="vi editing in the prompt (config key: vi_mode)",
    )
    parser.add_argument(
        "--ask-all",
        action="store_true",
        help="prompt for every command, including read-only ones",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="pick an earlier session to continue (same picker as /resume; "
        "resumes the latest when input is piped)",
    )
    args = parser.parse_args()
    explicit_model = hasattr(args, "model")  # see the --model SUPPRESS note above
    if not explicit_model:
        args.model = default_model

    if args.model == "claude-max" or args.model.startswith("claude-max:"):
        # Claude subscription path: the Agent SDK owns the loop, so this is a
        # different agent class, not a chat backend (built below).
        chat, provider, model_name = None, "claude-max", args.model.partition(":")[2]
    else:
        try:
            chat, provider, model_name = backends.make_chat(args.model)
        except backends.BackendError as exc:
            print(f"{RED}error:{RESET} {exc}", file=sys.stderr)
            return 1

    cwd = default_workspace(os.getcwd())
    if cwd != os.getcwd():
        print(f"{DIM}started from your home directory — working in {cwd} instead "
              f"to keep personal files out of scope (/cd moves elsewhere){RESET}")
    state_dir = Path(
        os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish"))
    )
    allow_path = Path(os.environ.get("AISH_ALLOWLIST", str(DEFAULT_ALLOWLIST)))
    deny_path = Path(os.environ.get("AISH_DENYLIST", str(DEFAULT_DENYLIST)))
    lessons_path = Path(os.environ.get("AISH_LESSONS", str(DEFAULT_LESSONS)))

    global _box
    if sys.stdin.isatty():
        from .prompt import BoxPrompt

        _box = BoxPrompt(args.vi_mode, state_dir, SLASH_COMMANDS)

    history: list[dict] = []
    recorded_spec = ""  # the model the resumed session last ran on
    resumed_chips: list[tuple[str, str]] = []  # pending chips from a resumed final answer (#167)
    log: SessionLog | None = None
    if args.resume:
        chosen: Path | None = None
        if _box is not None:
            # Interactive: same live-filter picker as /resume, rather than
            # silently assuming the most recent session.
            entries = SessionLog.load_entries(state_dir)
            if not entries:
                print(f"{DIM}no previous session found — starting fresh{RESET}")
            else:
                selected = _box.pick(
                    lambda query: SessionLog.rank(entries, query),
                    render=session_row,
                )
                if selected is None:
                    print(f"{DIM}cancelled — starting fresh{RESET}")
                else:
                    chosen = selected.path
        else:  # pipes/scripts can't pick — keep resuming the latest
            chosen = SessionLog.latest(state_dir)
            if chosen is None:
                print(f"{DIM}no previous session found — starting fresh{RESET}")
        if chosen is not None:
            history, recorded_spec, *_ = SessionLog._parse(chosen)
            log = SessionLog(chosen)
    if log is None:
        log = SessionLog.new(state_dir)
    log.model(args.model)
    logref = LogRef(log)

    context = "\n\n".join(
        part
        for part in [
            environment_context(cwd),
            usage_context(
                model_name, args.vi_mode, allow_path, state_dir, config_path,
                deny_path, lessons_path, provider=provider,
            ),
            *load_context_files(cwd),
        ]
        if part
    )

    stream_answers = sys.stdout.isatty()

    global _timer
    if stream_answers:
        _timer = LiveTimer()

    # Strip quick-reply chip syntax out of the streamed body (#167): the chips
    # are surfaced as a numbered menu after the answer, not as raw markdown.
    chip_stream = ChipStream(
        lambda text: print(f"{GREEN}{text}{RESET}", end="", flush=True)
    ) if stream_answers else None

    def print_token(token: str) -> None:
        assert chip_stream is not None
        chip_stream.feed(token)

    # The approver needs the agent's live cwd/roots, but the agent is built
    # with the approver — so the scope binds late through this holder.
    agent_holder: list = []

    def get_scope():
        # The WORKSPACE boundary, not `roots`: a read-only command touching
        # aish's own scratch/media/document stores must auto-approve, the same
        # as writing and deleting there already does (#220).
        if agent_holder:
            return agent_holder[0].cwd, agent_holder[0].workspace_roots()
        return cwd, [Path(cwd).resolve()]

    def get_session_prefixes() -> set[str]:
        # The agent owns the 's' allowances so a session switch drops them with
        # the roots (#176); before it exists nothing has been allowed yet.
        return agent_holder[0].session_prefixes if agent_holder else set()

    def trust_dir(path: str) -> str:
        if agent_holder:
            return agent_holder[0].trust_root(path)
        return "ERROR: agent not ready"

    def get_intent() -> str:
        # What the model said on the step that proposed the action being gated
        # (#252) — late-bound off the agent like the scope and the allowances.
        return agent_holder[0].turn_intent() if agent_holder else ""

    agent: Agent | ClaudeMaxAgent
    if provider == "claude-max":
        # aliased so the annotation above binds the TYPE_CHECKING import,
        # not this function-local one (F823)
        from .claude_max import ClaudeMaxAgent as _ClaudeMaxAgent
        from .claude_max import api_key_warning

        warning = api_key_warning()
        if warning:
            print(f"{YELLOW}warning: {warning}{RESET}")
        agent = _ClaudeMaxAgent(
            model=model_name,
            approve=make_approver(
                args.ask_all, allow_path, logref, deny_path, get_scope,
                get_session_prefixes, trust_dir=trust_dir, get_intent=get_intent,
            ),
            approve_write=make_write_approver(logref, get_intent),
            approve_read=make_read_approver(
                logref, trust_dir=trust_dir, get_intent=get_intent
            ),
            approve_tool=make_tool_approver(logref, get_intent),
            approve_import=make_import_approver(logref, get_intent),
            echo=echo,
            stream=stream_line,
            max_steps=args.max_steps,
            cwd=cwd,
            context=context,
            on_message=logref.message,
            # Persist trace steps (no on_step: the terminal keeps its own flat
            # progress lines) so a session started in the CLI still reconstructs
            # its activity trace when later opened in the web UI.
            step_log=logref.step,
            command_log=logref.command_event,
            # Persist cwd moves / dir trusts so resume restores the workspace.
            state_log=logref.workspace,
            on_token=print_token if stream_answers else None,
            job_log_dir=state_dir / "jobs",
            lessons_path=lessons_path,
            status=_timer,
            state_dir=state_dir,
            current_session=lambda: logref.log.path,
            aliases=config.get("aliases"),
        )
    else:
        assert chat is not None  # None only for claude-max, handled above
        agent = Agent(
            model=model_name,
            client_chat=chat,
            approve=make_approver(
                args.ask_all, allow_path, logref, deny_path, get_scope,
                get_session_prefixes, trust_dir=trust_dir, get_intent=get_intent,
            ),
            approve_write=make_write_approver(logref, get_intent),
            approve_read=make_read_approver(
                logref, trust_dir=trust_dir, get_intent=get_intent
            ),
            approve_tool=make_tool_approver(logref, get_intent),
            approve_import=make_import_approver(logref, get_intent),
            echo=echo,
            stream=stream_line,
            num_ctx=args.num_ctx,
            max_steps=args.max_steps,
            think=args.think,
            cwd=cwd,
            context=context,
            on_message=logref.message,
            # Persist trace steps (no on_step: the terminal keeps its own flat
            # progress lines) so a session started in the CLI still reconstructs
            # its activity trace when later opened in the web UI.
            step_log=logref.step,
            command_log=logref.command_event,
            # Persist cwd moves / dir trusts so resume restores the workspace.
            state_log=logref.workspace,
            on_token=print_token if stream_answers else None,
            job_log_dir=state_dir / "jobs",
            lessons_path=lessons_path,
            status=_timer,
            state_dir=state_dir,
            current_session=lambda: logref.log.path,
            semantic=SemanticIndex(state_dir),
            aliases=config.get("aliases"),
        )
        agent.provider = provider
    agent_holder.append(agent)
    if _box is not None:
        _box.get_cwd = lambda: agent.cwd  # /cd path completion follows the agent
    if history:
        agent.load_history(history)
        # Continue on the model the session last used, like /resume and the web's
        # cold open. A --model on the command line still wins: startup defaults
        # ($AISH_MODEL, config) are what the session's own model overrides, an
        # explicit flag is the user overriding the session on purpose.
        if recorded_spec and not explicit_model and recorded_spec != model_spec(agent):
            switch_model(agent, recorded_spec)
            logref.model(model_spec(agent))  # the log records what actually runs
        # Restore the workspace the session left off in (cwd + trusted dirs)
        # rather than reverting to the launch dir (issue #94).
        restored_cwd, trusted = SessionLog.restore_state(log.path)
        agent.restore_workspace(restored_cwd, trusted)
        agent.resume_turns(SessionLog.last_turn(log.path))
        print(f"{DIM}resumed {len(history)} messages from {log.path.name}"
              f" · model {model_spec(agent)} · /help:{RESET}")
        resumed_chips = replay_history(history)

    if args.task:
        try:
            result = agent.run_task(" ".join(args.task))
        except ModelUnavailable as exc:
            print(f"{RED}model unavailable:{RESET} {exc}{_backend_hint(agent)}")
            return 1
        if chip_stream is not None:
            chip_stream.close()
        clean, _chips = parse_reply_chips(result)
        if not stream_answers:
            print(f"{GREEN}{clean}{RESET}")
        print_answer_images(agent, result)
        print_sources(agent)
        return 0

    if not history:  # a resumed session continues where it was — no big banner
        print(banner(f"model {args.model} · session {log.path.name} · /help · Ctrl-D quits"))
    # quick-reply menu from the last answer; seeded from a resumed final answer (#167)
    pending_chips: list[tuple[str, str]] = resumed_chips
    while True:
        try:
            task = read_task(agent.cwd).strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print(f"{DIM}(input cleared — Ctrl-D or /quit to exit){RESET}")
            continue
        # A bare number selects the matching quick-reply chip; anything else is a
        # normal message. Chips are one-shot — consumed by whatever comes next.
        selected = resolve_chip_selection(task, pending_chips)
        if selected != task:
            print(f"{BOLD}❯{RESET} {selected}")
        task = selected
        pending_chips = []
        if task in ("exit", "quit"):
            return 0
        if task == "clear":  # parity with plain 'exit' — no slash needed
            task = "/clear"
        if not task:
            continue
        if task.startswith("/"):
            # /learn and /feedback expand to a task prompt (recall + diff
            # approvals apply); every other slash is handled inline.
            expanded = parse_learn(task, lessons_path) or parse_feedback(task)
            if expanded is None:
                if handle_slash(
                    task, agent, logref, state_dir,
                    config_path=config_path, chips_out=pending_chips,
                ) == "exit":
                    return 0
                continue
            task = expanded
        if task.startswith("!"):
            command = task[1:].strip()
            if command:
                logref.command(command, "user-direct")
                try:
                    agent.run_user_command(command)
                except KeyboardInterrupt:
                    print(f"\n{YELLOW}(command interrupted){RESET}")
            continue
        try:
            result = agent.run_task(task)
            if chip_stream is not None:
                chip_stream.close()
            clean, pending_chips = parse_reply_chips(result)
            if not stream_answers:
                print(f"\n{GREEN}{clean}{RESET}")
            print_answer_images(agent, result)
            print_sources(agent)
            print_chip_menu(pending_chips)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}(task interrupted){RESET}")
        except ModelUnavailable as exc:
            print(f"\n{RED}model unavailable:{RESET} {exc}{_backend_hint(agent)}")


if __name__ == "__main__":
    sys.exit(main())
