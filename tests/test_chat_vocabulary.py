"""aish calls a conversation a CHAT, everywhere the owner can read it.

The button said "Sessions" and the panel it opened said "Chats" — one control,
two names for one thing, and it had been that way since the rail shipped. The
split ran much wider than the button: "Session roots", "Session exported",
"Allow this session", "no such session", "sessions persist".

The line, and the reason this check exists rather than a one-time sweep:

  **"Session" is the machine's word.** It names a log file, an entry in
  `WebServer.sessions`, a process lifetime — real things, which is exactly why
  the word keeps leaking outward. **"Chat" is the person's word** for the thing
  they are looking at. So the wire protocol, the log format, the Python
  identifiers and every comment in this repo go on saying `session`, and none
  of that is what this file reads.

The web was renamed first and the terminal was deliberately left alone. That
half-measure is what #260 closed: it is the SAME person and the SAME object —
the README's own promise is that a chat started on one surface is resumed on
the other — so deleting a *chat* in the browser and then reading `no such
session` in the terminal is being handed two names for one thing, which is the
complaint that started the web rename.

What this file reads is the text a person sees: attributes and prose in
`index.html`, string literals in `app.js` and in `cli.py`, and BOTH system
prompts — the model describing aish back to its owner has to use the owner's
words too, or it is describing an app that does not exist.

A genuinely different meaning is allowlisted below, by exact string, each with
what it means instead: an allowlist without reasons rots into a list of things
someone gave up on. A blind find-and-replace would have silently broken every
one of them — which is the other reason this is a test and not a sed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX = (REPO / "aish" / "static" / "index.html").read_text()
APP_JS = (REPO / "aish" / "static" / "app.js").read_text()
SERVER = (REPO / "aish" / "server.py").read_text()
CLI = (REPO / "aish" / "cli.py").read_text()
AGENT = (REPO / "aish" / "agent.py").read_text()

_WORD = re.compile(r"\bsessions?\b", re.I)

# `/session` and `--session` are the pre-#260 spellings, kept working on
# purpose, so the text that offers them is not a naming slip. They are stripped
# before the word is looked for — a command name is machine surface even when
# it appears in a help line.
_KEPT_SPELLING = re.compile(r"--session\b|/session\b")

# Strings whose "session" means something that is NOT a chat. Exact matches
# only — an allowlist of substrings would quietly cover new text.
ALLOWED = {
    # A signed-in session in aish's browser: a cookie jar, not a conversation.
    "If you just signed in, aish can use that sign-in when it reads ${host} ",
    # The three below are the SAME cookie jar, in the CLI system prompt: what
    # the site remembers about the owner being logged in. Nothing to do with
    # the conversation — the chat outlives the sign-in and resumes without it.
    "The browser keeps the user's own signed-in sessions, so reading a site they are logged into",
    "AISH, not from the page, and it is true — it tells you the session has expired, or that the",
    'says and then STOP. Say "your eon.pl session has expired — run /browser '
    "https://eon.pl to sign",
    # The sign-in attempt's outcome on a trace step: the SITE's login session,
    # seen to come up (or not) after an automatic renewal — the same cookie
    # jar as above, worded to exactly what `signin.ok` records.
    " and the session came up",
    " and the session was not seen to come up",
}

_ALPHA_WORD = re.compile(r"^[A-Za-z]{2,}$")


def _says_session(value: str) -> bool:
    """True when a string calls something a session, ignoring kept spellings."""
    return bool(_WORD.search(_KEPT_SPELLING.sub(" ", value)))


def _reads_as_words(value: str) -> bool:
    """True when a string literal is TEXT rather than a name.

    app.js is mostly machine strings — DOM ids, selectors, URL paths, wire
    fields, the `/session` command — and every one of them legitimately says
    `session`. Two shapes are text: something with two or more plain words in
    it, and a lone Capitalised word, which is how a button label looks
    (`"Session"` was one, on the approval card's scope control).
    """
    words = [w for w in value.split() if _ALPHA_WORD.match(w)]
    return len(words) >= 2 or bool(re.fullmatch(r"[A-Z][a-z]+", value.strip()))


def _visible_html() -> list[tuple[str, str]]:
    """(what, text) for every string index.html shows or announces."""
    found: list[tuple[str, str]] = []
    for attr in ("title", "aria-label", "placeholder", "alt"):
        for m in re.finditer(rf'{attr}="([^"]*)"', INDEX):
            found.append((f"{attr}=", m.group(1)))
    # Text nodes, with comments and <script>/<svg> bodies removed first.
    body = re.sub(r"<!--.*?-->", " ", INDEX, flags=re.S)
    body = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", body, flags=re.S | re.I)
    for chunk in re.split(r"<[^>]*>", body):
        text = " ".join(chunk.split())
        if text:
            found.append(("text", text))
    return found


def _app_js_strings() -> list[tuple[str, str]]:
    """Single-line string literals in app.js, comments excluded."""
    found: list[tuple[str, str]] = []
    literal = re.compile(r"""(["'`])((?:\\.|(?!\1)[^\\\n])*?)\1""")
    for line_no, line in enumerate(APP_JS.split("\n"), 1):
        stripped = line.lstrip()
        if stripped.startswith(("//", "*", "/*")):
            continue
        for m in literal.finditer(line):
            found.append((f"app.js:{line_no}", m.group(2)))
    return found


def _web_prompt() -> str:
    """The `web_usage_context` f-string: aish describing this UI to its owner."""
    start = SERVER.index("def web_usage_context")
    body = SERVER.index('return f"""\\', start)
    return SERVER[body : SERVER.index('"""', body + 14)]


def _cli_strings() -> list[tuple[str, str]]:
    """Every string literal in cli.py that a person could end up reading.

    Parsed, not regexed, because the terminal's text lives in f-strings that
    span dozens of source lines — `usage_context` is one literal — and a
    line-at-a-time scan reads their continuations as separate strings. Two
    kinds are skipped: comments (ast never sees them) and DOCSTRINGS, which
    are written for whoever is changing the code and go on saying `session`
    like every other machine-facing word here.
    """
    tree = ast.parse(CLI)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                found.append((f"cli.py:{node.lineno}", node.value))
    return found


def _cli_prompt() -> str:
    """`SYSTEM_PROMPT_TEMPLATE`: the CLI's half of aish's self-knowledge.

    The counterpart to `web_usage_context`, and read by the same rule — aish
    answers questions about itself out of this text, so it has to name things
    the way the person it is answering does.
    """
    start = AGENT.index("SYSTEM_PROMPT_TEMPLATE = ")
    body = AGENT.index('"""\\', start)
    return AGENT[body : AGENT.index('\n"""', body)]


def _snippet(text: str) -> str:
    """The words around the offending one — a `usage_context` chunk is far too
    long to put in a failure message whole."""
    match = _WORD.search(_KEPT_SPELLING.sub(" ", text))
    assert match
    return "…" + " ".join(text[max(0, match.start() - 70) : match.end() + 70].split()) + "…"


def test_nothing_the_owner_reads_calls_a_chat_a_session() -> None:
    # Everything index.html shows is text by construction, so it is judged
    # whole; app.js is mostly machine strings, so only the word-shaped ones are.
    offenders = [
        (where, text)
        for where, text in _visible_html()
        if _says_session(text) and text not in ALLOWED
    ]
    offenders += [
        (where, text)
        for where, text in _app_js_strings()
        if _says_session(text) and text not in ALLOWED and _reads_as_words(text)
    ]
    assert not offenders, (
        "the web UI calls a chat a 'session' here — say chat, or add the string "
        f"to ALLOWED if it means something else: {offenders}"
    )


def test_nothing_the_terminal_prints_calls_a_chat_a_session() -> None:
    """The CLI is the other surface onto the same chats (#260).

    Same judgement as the web's, on the same two axes: only word-shaped
    strings count (cli.py is full of machine strings that legitimately say
    session — decision tokens, log-file globs, the kept `/session` spelling),
    and a genuinely different meaning goes in ALLOWED with its reason.
    """
    strings = _cli_strings()
    # A slice that quietly stopped finding anything would pass forever.
    assert any("no such chat number" in text for _, text in strings)
    offenders = [
        (where, _snippet(text))
        for where, text in strings
        if _says_session(text) and text not in ALLOWED and _reads_as_words(text)
    ]
    assert not offenders, (
        "the terminal calls a chat a 'session' here — say chat, or add the "
        f"whole string to ALLOWED if it means something else: {offenders}"
    )


def test_the_web_prompt_describes_the_ui_in_the_ui_s_words() -> None:
    """aish answers questions about itself from this text. If it says 'session'
    while every control says 'chat', it is describing an app that does not
    exist — the same failure as letting the README go stale (CLAUDE.md)."""
    offenders = [
        line.strip()
        for line in _web_prompt().split("\n")
        if _says_session(line) and line.strip() not in ALLOWED
    ]
    assert not offenders, (
        f"web_usage_context still calls a chat a session: {offenders}"
    )


def test_the_cli_prompt_describes_the_terminal_in_the_terminal_s_words() -> None:
    """The same rule on SYSTEM_PROMPT_TEMPLATE (#260).

    Judged line by line, and ALLOWED matches a whole stripped line: the three
    allowed ones are the browser's cookie jar, and pinning them to their exact
    wrapped line means a rewrite has to re-state that it still means a cookie
    jar rather than inheriting the exemption silently.
    """
    prompt = _cli_prompt()
    # Same guard as the CLI's: a slice that missed the template reads clean.
    assert "GROUNDING" in prompt, "SYSTEM_PROMPT_TEMPLATE moved or was rewritten"
    offenders = [
        line.strip()
        for line in prompt.split("\n")
        if _says_session(line) and line.strip() not in ALLOWED
    ]
    assert not offenders, (
        f"SYSTEM_PROMPT_TEMPLATE still calls a chat a session: {offenders}"
    )


def test_the_wire_still_says_session() -> None:
    """The rename is a VOCABULARY change, not a protocol one. `approve_session`
    is the action the server's contract in make_web_approvers reads, and a log
    file is still `session-*.jsonl`. Renaming those would have been a migration
    nobody asked for, and this pins that it did not happen by accident."""
    assert '"approve_session"' in APP_JS or "approve_session:" in APP_JS
    assert 'action == "approve_session"' in SERVER
    assert 'name.startswith("session-")' in SERVER
    # The CLI's half: the verdict string the approver RECORDS is a stored value
    # `aish explain` and the web trace card read back out of logs written years
    # apart. Only the line the user reads was reworded (#260).
    assert '"approved+session" if saved else "approved"' in CLI
    assert 'chat-allowed: {typed or suggestion}' in CLI


def test_the_old_spelling_of_a_command_still_works() -> None:
    """A rename that breaks the command is a worse bug than the inconsistency
    it fixes, so `/chat` is the name on the menu and `/session` is dispatched
    beside it — in the terminal, in the browser, and as `aish usage --session`.
    Both are completable: a habit that Tab-completes is a habit that survives.
    """
    from aish.cli import SLASH_COMMANDS, SLASH_HELP

    assert "/chat" in SLASH_COMMANDS and "/session" in SLASH_COMMANDS
    assert "/chat" in SLASH_HELP
    assert 'if command in ("/chat", "/session"):' in CLI
    assert 'elif flag in ("--chat", "--session") and rest:' in CLI
    assert 'case "/chat": case "/session":' in APP_JS
    assert '"/session"' in APP_JS.split("const SLASH_ALL")[1].split("\n")[0]


def test_an_old_log_still_replays_its_approval_echo() -> None:
    """The approver's echo is both the display text and the token the trace card
    matches to suppress a line it already shows. It changed wording, so the
    matcher has to accept BOTH — every log written before today carries the old
    one, and a replay must render as the live turn did (L2)."""
    matcher = re.search(r"/\^\[✓✕\] \(([^)]*)\)/", APP_JS)
    assert matcher, "the echo-suppression matcher moved or was rewritten"
    alternatives = matcher.group(1).split("|")
    assert "session-allowed" in alternatives, (
        "dropping `session-allowed` makes every pre-rename chat replay a "
        "redundant echo line the live turn never showed"
    )
    assert "chat-allowed" in alternatives, "the current wording is not matched"
    assert 'text": f"✓ chat-allowed:' in SERVER, "the server emits something else"
