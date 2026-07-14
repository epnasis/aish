"""Interactive CLI: one-shot task from argv, or a REPL keeping conversation state."""

import argparse
import os
import sys
import threading
import time
import tomllib
from pathlib import Path

from . import tools
from .agent import Agent, ModelUnavailable, environment_context, format_tokens
from .approval import (
    DEFAULT_ALLOWLIST,
    DEFAULT_DENYLIST,
    Blocked,
    check_denied,
    is_auto_approvable,
    load_prefixes,
    looks_destructive,
    save_prefix,
    suggest_prefix,
    unvetted_segments,
)
from .session import SessionLog
from .skills import GLOBAL_SKILLS_DIR, list_skills, skill_dirs

BOLD = "\033[1m"
DIM = "\033[2m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

ECHO_PREVIEW_LINES = 12
REPLAY_TOOL_LINES = 4

SLASH_COMMANDS = ("/clear", "/exit", "/help", "/jobs", "/model", "/new", "/quit", "/resume")

SLASH_HELP = f"""{DIM}commands (Tab autocompletes):
  /resume [n]    pick an earlier session to load (lists recent sessions with
                 a summary; Enter=latest, repeat to reach older ones)
  /new, /clear   fresh conversation in a new session file (clears the screen;
                 plain 'clear' works too)
  /model [name]  show or switch the Ollama model for this session
  /jobs          list background jobs started this session
  /help          this help
  /quit, /exit   quit (plain 'exit' works too)
input: Enter submits · newline: Ctrl+J, end line with \\, or Option+Enter
(iTerm2: set Option=Esc+) · pasted newlines are kept · !<cmd> runs directly
without the model · !cd <dir> moves the working directory
while a command runs: Ctrl-C cancels it · Ctrl-B detaches it to a background
job (keeps running, frees the prompt; see /jobs){RESET}"""

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

    def message(self, message: dict) -> None:
        self.log.message(message)

    def command(self, command: str, decision: str) -> None:
        self.log.command(command, decision)


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


def allow_segments_flow(command: str, allow_path: Path) -> None:
    """'always allow' asks about each unvetted chained segment independently."""
    prefixes = load_prefixes(allow_path)
    for segment in unvetted_segments(command, prefixes) or [command]:
        suggestion = suggest_prefix(segment)
        answer = input(
            f"{YELLOW}always allow prefix{RESET} [{BOLD}{suggestion}{RESET}] "
            f"(enter=yes, s=skip, or type a different prefix): "
        ).strip()
        if answer.lower() == "s":
            continue
        save_prefix(allow_path, answer or suggestion)
        print(f"{DIM}  saved: {answer or suggestion} → {allow_path}{RESET}")


def make_approver(ask_all: bool, allow_path: Path, log, deny_path: Path = DEFAULT_DENYLIST):
    def record(command: str, decision: str) -> None:
        if log:
            log.command(command, decision)

    def ask_approval(command: str) -> str | Blocked | None:
        # Denylist first: unrecoverable commands never reach the prompt and
        # the allowlist can never bypass this.
        reason = check_denied(command, load_prefixes(deny_path))
        if reason:
            print(f"\n{RED}✗ blocked ({reason}):{RESET}\n  {BOLD}{command}{RESET}")
            print(f"{DIM}  run it yourself with !{command}  if you truly mean it{RESET}")
            record(command, f"blocked: {reason}")
            return Blocked(reason)

        if not ask_all and is_auto_approvable(command, load_prefixes(allow_path)):
            print(f"\n{GREEN}✓ auto-approved:{RESET} {BOLD}{command}{RESET}")
            record(command, "auto")
            return command

        warning = f" {RED}⚠ destructive{RESET}" if looks_destructive(command) else ""
        print(f"\n{YELLOW}{BOLD}▶ run command?{RESET}{warning}\n  {BOLD}{command}{RESET}")
        try:
            answer = input(f"{YELLOW}[y/N/a(lways)/e(dit)]{RESET} ").strip().lower()
        except EOFError:
            record(command, "denied")
            return None

        if answer in ("y", "yes"):
            record(command, "approved")
            return command
        if answer == "a":
            allow_segments_flow(command, allow_path)
            record(command, "approved+allowlisted")
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


CYAN = "\033[36m"


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


def make_write_approver(log):
    def approve_write(plan) -> bool:
        verb = "create" if plan.is_new else "edit"
        print(f"\n{YELLOW}{BOLD}▶ {verb} file?{RESET} {BOLD}{plan.target}{RESET} "
              f"{DIM}(+{plan.added} -{plan.removed}){RESET}")
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
            log.command(f"{verb} {plan.target}", "approved" if approved else "denied")
        return approved

    return approve_write


def make_read_approver(log):
    """Prompt before an auto-approved read_file touches a secret-bearing path,
    so an injected read_file can't silently pull keys into context."""

    def approve_read(path: str) -> bool:
        print(f"\n{YELLOW}{BOLD}▶ read sensitive file?{RESET} {BOLD}{path}{RESET} "
              f"{RED}⚠ may contain secrets{RESET}")
        try:
            answer = input(f"{YELLOW}[y/N]{RESET} ").strip().lower()
        except EOFError:
            answer = ""
        approved = answer in ("y", "yes")
        if log:
            log.command(f"read {path}", "approved" if approved else "denied")
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


def replay_history(messages: list[dict]) -> None:
    """Print a loaded conversation so the user sees what they resumed."""
    for message in messages:
        role = message.get("role")
        content = (message.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            print(f"\n{BOLD}❯{RESET} {content}")
        elif role == "assistant":
            print(f"{GREEN}{content}{RESET}")
        else:
            lines = content.splitlines()
            print(DIM + "\n".join(f"  {line}" for line in lines[:REPLAY_TOOL_LINES]) + RESET)
            if len(lines) > REPLAY_TOOL_LINES:
                print(f"{DIM}  … ({len(lines) - REPLAY_TOOL_LINES} more lines){RESET}")


def handle_slash(
    task: str, agent: Agent, logref: LogRef, state_dir: Path, resumed: set | None = None
) -> str:
    """Dispatch a /command; returns 'exit' or 'handled'."""
    resumed = resumed if resumed is not None else set()
    command = task.split()[0].lower()
    if command in ("/quit", "/exit"):
        return "exit"
    if command in ("/new", "/clear"):
        agent.reset()
        logref.log = SessionLog.new(state_dir)
        print("\033[2J\033[3J\033[H", end="")  # clear screen + scrollback
        print(banner(f"fresh conversation — session {logref.log.path.name}"))
        return "handled"
    if command == "/resume":
        sessions = SessionLog.list_sessions(state_dir, exclude=resumed | {logref.log.path})
        if not sessions:
            print(f"{DIM}no earlier session to resume{RESET}")
            return "handled"

        parts = task.split()
        if len(parts) > 1 and parts[1].isdigit():
            choice = int(parts[1])
        elif len(sessions) == 1:
            choice = 1
        else:
            for i, info in enumerate(sessions[:10], 1):
                print(f"{DIM}{i:>3}. {info.when} · {info.count:>3} msgs ·{RESET} {info.title}")
            try:
                answer = input(
                    f"{YELLOW}resume which?{RESET} [1=latest] (number, q=cancel) "
                ).strip().lower()
            except EOFError:
                answer = "q"
            if answer in ("q", "quit"):
                print(f"{DIM}cancelled{RESET}")
                return "handled"
            choice = int(answer) if answer.isdigit() else 1
        if not 1 <= choice <= len(sessions):
            print(f"{DIM}no such session number{RESET}")
            return "handled"

        selected = sessions[choice - 1]
        messages = SessionLog.load_messages(selected.path)
        resumed.add(selected.path)
        agent.load_history(messages)
        for message in messages:  # keep the current session file self-contained
            logref.message(message)
        print(f"{DIM}resumed {len(messages)} messages from {selected.path.name}:{RESET}")
        replay_history(messages)
        return "handled"
    if command == "/model":
        parts = task.split()
        if len(parts) > 1:
            agent.model = parts[1]
            print(f"{DIM}model switched to {agent.model} (this session only){RESET}")
        else:
            print(f"{DIM}current model: {agent.model} — /model <name> to switch, "
                  f"'ollama list' shows what's installed{RESET}")
        return "handled"
    if command == "/jobs":
        print(f"{DIM}{tools.jobs_table()}{RESET}")
        return "handled"
    if command == "/help":
        print(SLASH_HELP)
        return "handled"
    print(f"{DIM}unknown command {command} — try /help{RESET}")
    return "handled"


DEFAULT_LESSONS = Path.home() / ".config" / "aish" / "lessons.md"


def usage_context(
    model: str,
    vi_mode: bool,
    allow_path: Path,
    state_dir: Path,
    config_path: Path,
    deny_path: Path = DEFAULT_DENYLIST,
    lessons_path: Path = DEFAULT_LESSONS,
) -> str:
    """Self-knowledge for the system prompt: aish should be able to explain
    and (via approved commands) reconfigure itself."""
    return f"""\
About aish (you) — use this to answer questions about your own usage:
- YOUR IDENTITY: you are the local model '{model}' running through Ollama \
ON THIS MACHINE — you are NOT a cloud service and NOT accessed over any API. \
The Ollama process (ollama / llama-server, often ~20+ GB RAM) that the user \
sees in `top`/`ps` IS you: it is the server executing your weights right now. \
If the user stops Ollama, quits the Ollama app, or runs `killall llama-server` \
/ `ollama stop`, THIS SESSION ENDS immediately — you would be killing \
yourself. So when the user is hunting memory hogs or asks about that process, \
say plainly that it is you; never recommend or run a command that kills it \
without first warning that it terminates the current aish session, and let \
them decide.
- Approval prompt keys: y=run once, n=deny, a=always allow (saves command \
prefixes to {allow_path}; chained |/&&/|| segments are vetted and allowlisted \
independently; read-only commands auto-approve), e=edit the command first.
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
- REPL escapes: `!<command>` runs directly without you (no approval); \
`!cd <dir>` changes the shared working directory. Ctrl-C cancels only the \
running command. Ctrl-D or `exit` quits.
- REPL slash commands (Tab autocompletes them): /resume shows a numbered \
picker of earlier sessions (summary = the session's first user message; \
Enter picks the latest, /resume N picks directly) and replays the chosen \
one into this conversation; /new or /clear (or plain 'clear') starts a \
fresh conversation and clears the screen; /model [name] shows or switches \
the model; /jobs lists \
background jobs; /help lists commands; /quit or /exit quits.
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
- Learning: call the remember tool to save a one-line lesson — do this after \
correcting any mistake, and whenever the user asks you to remember something \
durable. Lessons live in {lessons_path} and are ALREADY loaded into your \
context each session under "lessons you saved" — when the user asks about \
your learnings, quote that section (or read the file); do not go hunting \
elsewhere. For longer curated notes the user maintains, \
~/.config/aish/AISH.md is also loaded each session.
- Multiline input: Enter submits; a newline is inserted by Ctrl+J, by ending \
the line with a backslash then Enter, or by Option/Alt+Enter (in iTerm2 only \
with "Left Option key: Esc+"); pasted text keeps its newlines.
- Sessions: conversation + command audit trail logged to {state_dir}; \
`aish --resume` continues the most recent session.
- Config file: {config_path} (TOML). Keys: vi_mode, model, num_ctx, \
max_steps. vi_mode (prompt vi editing) is currently {str(vi_mode).lower()}; \
enable it with the line `vi_mode = true`. Config is read at startup only — \
changes take effect on the next aish start. CLI flags override config; \
$AISH_MODEL overrides the model key.
- Durable context: an AISH.md file in the working directory or \
~/.config/aish/AISH.md is loaded into your system prompt — the right place \
for host facts and user preferences.
- Skills: markdown playbooks in {GLOBAL_SKILLS_DIR} (global) or \
./.aish/skills/ (project; wins on name clash), listed in your context and \
read via the read_skill tool. To create one when the user asks, write \
<name>.md there with optional frontmatter lines (name:, description:) \
between --- markers, then a body of workflows, exact commands, and safety \
rules; it is picked up on the next aish start.
- Current model: {model} (change via --model, $AISH_MODEL, or config).
When the user asks you to change one of your settings, edit the config file \
with a normal shell command (it goes through approval like any command)."""


def skills_context(cwd: str) -> str:
    found = list_skills(skill_dirs(cwd))
    if not found:
        return ""
    lines = "\n".join(f"- {name}: {description}" for name, description in found)
    return (
        "Skills — task-specific playbooks with workflows and safety rules. "
        "ALWAYS call read_skill for the relevant one BEFORE first using that "
        "tool in a session:\n" + lines
    )


def load_context_files(cwd: str, lessons_path: Path = DEFAULT_LESSONS) -> list[str]:
    parts = []
    sources = (
        Path.home() / ".config" / "aish" / "AISH.md",
        Path(cwd) / "AISH.md",
        lessons_path,
    )
    for path in sources:
        try:
            if path.is_file():
                label = (
                    "lessons you saved after earlier mistakes — apply them "
                    "proactively whenever one is relevant"
                    if path == lessons_path
                    else f"context from {path}"
                )
                parts.append(f"[{label}]\n{path.read_text(encoding='utf-8')}")
        except OSError:
            continue
    return parts


def main() -> int:
    config_path = Path(
        os.environ.get("AISH_CONFIG", str(Path.home() / ".config" / "aish" / "config.toml"))
    )
    config = load_config(config_path)

    parser = argparse.ArgumentParser(
        prog="aish",
        description="Local LLM agent that runs CLI commands (with your approval).",
    )
    parser.add_argument("task", nargs="*", help="task to perform; omit for interactive mode")
    parser.add_argument(
        "--model",
        default=os.environ.get("AISH_MODEL") or config.get("model") or "qwen3.6:35b-a3b",
        help="Ollama model (default: $AISH_MODEL, config, or qwen3.6:35b-a3b)",
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
        "--resume", action="store_true", help="continue the most recent session"
    )
    args = parser.parse_args()

    cwd = os.getcwd()
    state_dir = Path(
        os.environ.get("AISH_STATE_DIR", str(Path.home() / ".local" / "state" / "aish"))
    )
    allow_path = Path(os.environ.get("AISH_ALLOWLIST", str(DEFAULT_ALLOWLIST)))
    deny_path = Path(os.environ.get("AISH_DENYLIST", str(DEFAULT_DENYLIST)))
    lessons_path = Path(os.environ.get("AISH_LESSONS", str(DEFAULT_LESSONS)))

    history: list[dict] = []
    resumed: set[Path] = set()
    if args.resume:
        latest = SessionLog.latest(state_dir)
        if latest is not None:
            resumed.add(latest)
        if latest is None:
            print(f"{DIM}no previous session found — starting fresh{RESET}")
            log = SessionLog.new(state_dir)
        else:
            history = SessionLog.load_messages(latest)
            log = SessionLog(latest)
    else:
        log = SessionLog.new(state_dir)
    logref = LogRef(log)

    global _box
    if sys.stdin.isatty():
        from .prompt import BoxPrompt

        _box = BoxPrompt(args.vi_mode, state_dir, SLASH_COMMANDS)

    context = "\n\n".join(
        part
        for part in [
            environment_context(cwd),
            usage_context(
                args.model, args.vi_mode, allow_path, state_dir, config_path,
                deny_path, lessons_path,
            ),
            skills_context(cwd),
            *load_context_files(cwd, lessons_path),
        ]
        if part
    )

    stream_answers = sys.stdout.isatty()

    global _timer
    if stream_answers:
        _timer = LiveTimer()

    def print_token(token: str) -> None:
        print(f"{GREEN}{token}{RESET}", end="", flush=True)

    agent = Agent(
        model=args.model,
        approve=make_approver(args.ask_all, allow_path, logref, deny_path),
        approve_write=make_write_approver(logref),
        approve_read=make_read_approver(logref),
        echo=echo,
        stream=stream_line,
        num_ctx=args.num_ctx,
        max_steps=args.max_steps,
        think=args.think,
        cwd=cwd,
        context=context,
        on_message=logref.message,
        on_token=print_token if stream_answers else None,
        job_log_dir=state_dir / "jobs",
        lessons_path=lessons_path,
        status=_timer,
    )
    if history:
        agent.load_history(history)
        print(f"{DIM}resumed {len(history)} messages from {log.path.name}"
              f" · model {args.model} · /help:{RESET}")
        replay_history(history)

    if args.task:
        try:
            result = agent.run_task(" ".join(args.task))
        except ModelUnavailable as exc:
            print(f"{RED}model unavailable:{RESET} {exc} — is Ollama running and not overloaded?")
            return 1
        if not stream_answers:
            print(f"{GREEN}{result}{RESET}")
        return 0

    if not history:  # a resumed session continues where it was — no big banner
        print(banner(f"model {args.model} · session {log.path.name} · /help · Ctrl-D quits"))
    while True:
        try:
            task = read_task(agent.cwd).strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print(f"{DIM}(input cleared — Ctrl-D or /quit to exit){RESET}")
            continue
        if task in ("exit", "quit"):
            return 0
        if task == "clear":  # parity with plain 'exit' — no slash needed
            task = "/clear"
        if not task:
            continue
        if task.startswith("/"):
            if handle_slash(task, agent, logref, state_dir, resumed) == "exit":
                return 0
            continue
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
            if not stream_answers:
                print(f"\n{GREEN}{result}{RESET}")
        except KeyboardInterrupt:
            print(f"\n{YELLOW}(task interrupted){RESET}")
        except ModelUnavailable as exc:
            print(f"\n{RED}model unavailable:{RESET} {exc} — is Ollama overloaded? "
                  f"(check `ollama ps` / system load)")


if __name__ == "__main__":
    sys.exit(main())
