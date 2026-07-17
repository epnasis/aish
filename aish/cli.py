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
from pathlib import Path

from . import backends, tools
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

SLASH_COMMANDS = (
    "/add-dir", "/cd", "/clear", "/dir-add", "/exit", "/help", "/jobs",
    "/model", "/new", "/quit", "/resume",
)

SLASH_HELP = f"""{DIM}commands (Tab autocompletes):
  /resume        pick an earlier session: type to filter by title and
                 contents (exact match first, then phrase, words, fuzzy),
                 ↑/↓ select, Enter loads, Esc cancels
  /resume <n>    load the n-th newest session directly
  /resume <text> open the picker with the filter pre-filled
  /new, /clear   fresh conversation in a new session file (clears the screen;
                 plain 'clear' works too)
  /model [name]  switch the model (Ollama name, or a cloud model: gemini:/
                 openai:/claude: — bare provider name picks a default); no
                 arg opens a searchable picker of local + cloud models
                 (typing provider:model there offers that exact model)
  /cd <dir>      move the session to another project: changes the working
                 directory AND re-anchors the auto-approval root there
                 (Tab completes directories); !cd only moves the directory
  /add-dir <dir> allow auto-approved reads/commands in another directory
                 tree too (alias /dir-add); no arg lists current roots
  /jobs          list background jobs started this session
  /help          this help
  /quit, /exit   quit (plain 'exit' works too)
input: Enter submits · newline: Ctrl+J, end line with \\, or Option+Enter
(iTerm2: set Option=Esc+) · pasted newlines are kept · @ mentions a project
file (type to filter, Tab/Enter completes) · !<cmd> runs directly
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


def make_approver(
    ask_all: bool,
    allow_path: Path,
    log,
    deny_path: Path = DEFAULT_DENYLIST,
    get_scope=None,
):
    """get_scope() -> (cwd, roots): the agent's live directory scope, bound
    late because the agent is constructed after its approver. When present,
    auto-approval is confined to the session roots."""

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

        cwd, roots = get_scope() if get_scope else (None, None)
        if not ask_all and is_auto_approvable(
            command, load_prefixes(allow_path), cwd=cwd, roots=roots
        ):
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
    """Prompt before an auto-approved read_file touches a secret-bearing path
    or one outside the session roots, so an injected read_file can't silently
    pull keys — or arbitrary files elsewhere on the machine — into context."""

    def approve_read(path: str, reason: str = "sensitive") -> bool:
        if reason == "outside":
            print(f"\n{YELLOW}{BOLD}▶ read file outside the project?{RESET} "
                  f"{BOLD}{path}{RESET} {DIM}(/cd or /add-dir widens the scope){RESET}")
        else:
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


def switch_model(agent, arg: str) -> None:
    """Point the running agent at another model/backend (session-only)."""
    crossing_max = arg.startswith("claude-max") or (
        getattr(agent, "provider", "ollama") == "claude-max"
    )
    if crossing_max:
        print(f"{DIM}claude-max runs a different agent loop — restart aish "
              f"(aish --model {arg}) to switch{RESET}")
        return
    try:
        chat, provider, name = backends.make_chat(arg)
    except backends.BackendError as exc:
        print(f"{RED}{exc}{RESET}")
        return
    switched_provider = provider != getattr(agent, "provider", "ollama")
    agent.chat = chat
    agent.model = name
    agent.provider = provider
    print(f"{DIM}model switched to {arg} (this session only){RESET}")
    if switched_provider:
        print(f"{DIM}note: the system prompt still describes the startup "
              f"backend — restart aish to refresh its self-description{RESET}")


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
        parts = task.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        exclude = resumed | {logref.log.path}

        if not arg.isdigit() and _box is not None:
            # Interactive: live-filter picker — typing re-ranks, Enter loads.
            entries = SessionLog.load_entries(state_dir, exclude=exclude)
            if not entries:
                print(f"{DIM}no earlier session to resume{RESET}")
                return "handled"
            selected = _box.pick(
                lambda query: SessionLog.rank(entries, query),
                initial=arg,
                render=lambda info: f"{info.when} · {info.count:>3} msgs · {info.title}",
            )
            if selected is None:
                print(f"{DIM}cancelled{RESET}")
                return "handled"
        else:
            # /resume <n>, or no TTY (pipes/scripts): one-shot numbered flow.
            searching = bool(arg) and not arg.isdigit()
            if searching:
                sessions = SessionLog.search_sessions(state_dir, arg, exclude=exclude)
                if not sessions:
                    print(f"{DIM}no session matches '{arg}'{RESET}")
                    return "handled"
            else:
                sessions = SessionLog.list_sessions(state_dir, exclude=exclude)
                if not sessions:
                    print(f"{DIM}no earlier session to resume{RESET}")
                    return "handled"

            if arg.isdigit():
                choice = int(arg)
            elif len(sessions) == 1:
                choice = 1
            else:
                default = "best match" if searching else "latest"
                for i, info in enumerate(sessions, 1):
                    print(
                        f"{DIM}{i:>3}. {info.when} · {info.count:>3} msgs ·{RESET} {info.title}"
                    )
                try:
                    answer = input(
                        f"{YELLOW}resume which?{RESET} [1={default}] (number, q=cancel) "
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
            switch_model(agent, parts[1])
        elif _box is not None:
            # Interactive: same live-filter picker as /resume, over models.
            models = available_models(agent, state_dir)
            selected = _box.pick(
                lambda query: rank_models(models, query),
                render=lambda model: f"{model[0]:<28} {model[1]}",
            )
            if selected is None:
                print(f"{DIM}cancelled — still on {agent.model}{RESET}")
            else:
                switch_model(agent, selected[0])
        else:
            print(f"{DIM}current model: {agent.model} — /model <name> to switch "
                  f"('ollama list' shows local models; gemini:/openai: for cloud){RESET}")
        return "handled"
    if command == "/cd":
        parts = task.split(maxsplit=1)
        if len(parts) < 2:
            roots = ", ".join(str(r) for r in agent.roots)
            print(f"{DIM}cwd: {agent.cwd} · roots: {roots} — /cd <dir> moves both{RESET}")
            return "handled"
        agent.rebase(parts[1].strip())  # echoes its own result/error
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
    if command == "/jobs":
        print(f"{DIM}{tools.jobs_table()}{RESET}")
        return "handled"
    if command == "/help":
        print(SLASH_HELP)
        return "handled"
    print(f"{DIM}unknown command {command} — try /help{RESET}")
    return "handled"


DEFAULT_LESSONS = Path.home() / ".config" / "aish" / "lessons.md"


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
prefixes to {allow_path}; chained |/&&/|| segments are vetted and allowlisted \
independently; read-only commands auto-approve), e=edit the command first. \
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
- REPL escapes: `!<command>` runs directly without you (no approval); \
`!cd <dir>` changes the shared working directory. Ctrl-C cancels only the \
running command. Ctrl-D or `exit` quits.
- REPL slash commands (Tab autocompletes them): /resume opens a live picker \
over ALL earlier sessions with start dates (summary = the session's first \
user message): typing filters by title and full contents deterministically \
(exact title match, then phrase, then all-words, then fuzzy — no model \
involved), arrow keys select, Enter loads, Esc cancels; /resume <text> \
pre-fills the filter and /resume N loads the N-th newest directly; the \
chosen session is replayed into this conversation. Session \
files are append-only and never deleted — every past session stays \
available. /new or /clear (or plain 'clear') starts a \
fresh conversation and clears the screen; /model <name> switches the model \
and /model alone opens the same type-to-filter picker over installed Ollama \
models and the cloud providers (typing provider:model inside the picker \
offers that exact cloud model as a selectable row); /jobs lists \
background jobs; /help lists commands; /quit or /exit quits; /cd <dir> \
moves the working directory AND re-anchors the session root there (user \
only; !cd and your cd move only the working directory); /add-dir <dir> \
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
- File mentions: '@<path>' in a user message references a file (the prompt \
autocompletes project files after '@'). The path is relative to the working \
directory — when its contents matter to the task, read it with read_file \
before answering.
- Sessions: conversation + command audit trail logged to {state_dir}; \
`aish --resume` opens the same session picker as /resume at launch (piped \
input resumes the most recent session).
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


def _backend_hint(agent) -> str:
    provider = getattr(agent, "provider", "ollama")
    if provider == "ollama":
        return " — is Ollama running and not overloaded? (check `ollama ps` / system load)"
    if provider == "claude-max":
        return " — is the claude CLI installed and logged in? (run `claude` then /login)"
    return " — check your API key, network, and the provider's rate limits"


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

    cwd = os.getcwd()
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
    resumed: set[Path] = set()
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
                    render=lambda info: f"{info.when} · {info.count:>3} msgs · {info.title}",
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
            history = SessionLog.load_messages(chosen)
            log = SessionLog(chosen)
            resumed.add(chosen)
    if log is None:
        log = SessionLog.new(state_dir)
    logref = LogRef(log)

    context = "\n\n".join(
        part
        for part in [
            environment_context(cwd),
            usage_context(
                model_name, args.vi_mode, allow_path, state_dir, config_path,
                deny_path, lessons_path, provider=provider,
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

    # The approver needs the agent's live cwd/roots, but the agent is built
    # with the approver — so the scope binds late through this holder.
    agent_holder: list = []

    def get_scope():
        if agent_holder:
            return agent_holder[0].cwd, agent_holder[0].roots
        return cwd, [Path(cwd).resolve()]

    if provider == "claude-max":
        from .claude_max import ClaudeMaxAgent, api_key_warning

        warning = api_key_warning()
        if warning:
            print(f"{YELLOW}warning: {warning}{RESET}")
        agent = ClaudeMaxAgent(
            model=model_name,
            approve=make_approver(args.ask_all, allow_path, logref, deny_path, get_scope),
            approve_write=make_write_approver(logref),
            approve_read=make_read_approver(logref),
            echo=echo,
            stream=stream_line,
            max_steps=args.max_steps,
            cwd=cwd,
            context=context,
            on_message=logref.message,
            on_token=print_token if stream_answers else None,
            job_log_dir=state_dir / "jobs",
            lessons_path=lessons_path,
            status=_timer,
        )
    else:
        agent = Agent(
            model=model_name,
            client_chat=chat,
            approve=make_approver(args.ask_all, allow_path, logref, deny_path, get_scope),
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
        agent.provider = provider
    agent_holder.append(agent)
    if _box is not None:
        _box.get_cwd = lambda: agent.cwd  # /cd path completion follows the agent
    if history:
        agent.load_history(history)
        print(f"{DIM}resumed {len(history)} messages from {log.path.name}"
              f" · model {args.model} · /help:{RESET}")
        replay_history(history)

    if args.task:
        try:
            result = agent.run_task(" ".join(args.task))
        except ModelUnavailable as exc:
            print(f"{RED}model unavailable:{RESET} {exc}{_backend_hint(agent)}")
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
            print(f"\n{RED}model unavailable:{RESET} {exc}{_backend_hint(agent)}")


if __name__ == "__main__":
    sys.exit(main())
