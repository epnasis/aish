"""The web UI calls a conversation a CHAT, everywhere the owner can read it.

The button said "Sessions" and the panel it opened said "Chats" — one control,
two names for one thing, and it had been that way since the rail shipped. The
split ran much wider than the button: "Session roots", "Session exported",
"Allow this session", "no such session", "sessions persist".

The line, and the reason this check exists rather than a one-time sweep:

  **"Session" is the machine's word.** It names a log file, an entry in
  `WebServer.sessions`, a process lifetime — real things, which is exactly why
  the word keeps leaking outward. **"Chat" is the person's word** for the thing
  they are looking at. So the wire protocol, the log format, the CLI, the
  Python identifiers and every comment in this repo go on saying `session`, and
  none of that is what this file reads.

What it reads is the text a person sees: attributes and prose in `index.html`,
string literals in `app.js`, and the web system prompt — which is the model
describing this UI back to its owner, so it has to use the owner's words too.

A genuinely different meaning is allowlisted below, by exact string. There is
one today (a signed-in BROWSER session), and a blind find-and-replace would
have silently broken it — which is the other reason this is a test and not a
sed.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX = (REPO / "aish" / "static" / "index.html").read_text()
APP_JS = (REPO / "aish" / "static" / "app.js").read_text()
SERVER = (REPO / "aish" / "server.py").read_text()

_WORD = re.compile(r"\bsessions?\b", re.I)

# Strings whose "session" means something that is NOT a chat. Exact matches
# only — an allowlist of substrings would quietly cover new text.
ALLOWED = {
    # A signed-in session in aish's browser: a cookie jar, not a conversation.
    "If you just signed in, aish can use that sign-in when it reads ${host} ",
}

_ALPHA_WORD = re.compile(r"^[A-Za-z]{2,}$")


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


def test_nothing_the_owner_reads_calls_a_chat_a_session() -> None:
    # Everything index.html shows is text by construction, so it is judged
    # whole; app.js is mostly machine strings, so only the word-shaped ones are.
    offenders = [
        (where, text)
        for where, text in _visible_html()
        if _WORD.search(text) and text not in ALLOWED
    ]
    offenders += [
        (where, text)
        for where, text in _app_js_strings()
        if _WORD.search(text) and text not in ALLOWED and _reads_as_words(text)
    ]
    assert not offenders, (
        "the web UI calls a chat a 'session' here — say chat, or add the string "
        f"to ALLOWED if it means something else: {offenders}"
    )


def test_the_web_prompt_describes_the_ui_in_the_ui_s_words() -> None:
    """aish answers questions about itself from this text. If it says 'session'
    while every control says 'chat', it is describing an app that does not
    exist — the same failure as letting the README go stale (CLAUDE.md)."""
    offenders = [
        line.strip()
        for line in _web_prompt().split("\n")
        if _WORD.search(line)
    ]
    assert not offenders, (
        f"web_usage_context still calls a chat a session: {offenders}"
    )


def test_the_wire_still_says_session() -> None:
    """The rename is a VOCABULARY change, not a protocol one. `approve_session`
    is the action the server's contract in make_web_approvers reads, and a log
    file is still `session-*.jsonl`. Renaming those would have been a migration
    nobody asked for, and this pins that it did not happen by accident."""
    assert '"approve_session"' in APP_JS or "approve_session:" in APP_JS
    assert 'action == "approve_session"' in SERVER
    assert 'name.startswith("session-")' in SERVER


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
