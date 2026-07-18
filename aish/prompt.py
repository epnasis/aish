"""Boxed input UI: horizontal rules hugging the input line, expanding as the
entry becomes multiline; slash-command and @-file autocomplete; vi/emacs
editing.

Built as a small prompt_toolkit Application (full_screen=False) instead of
PromptSession because a "footer under the input" is not something
PromptSession can render — its bottom_toolbar pins to the screen bottom.

Keys: Enter submits · Alt/Option+Enter (or Esc, Enter) inserts a newline ·
Tab / menu completes slash commands · Ctrl-C aborts input · Ctrl-D on empty
input quits.
"""

import os
import time
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion, PathCompleter, merge_completers
from prompt_toolkit.cursor_shapes import ModalCursorShapeConfig
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import HSplit, Layout, VerticalAlign, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.shortcuts import PromptSession

RULE_STYLE = "fg:ansibrightblack"
PICKER_MAX_ROWS = 10


# Slash commands whose argument is a directory — their args get path completion.
DIR_ARG_COMMANDS = ("/cd", "/add-dir", "/dir-add")


class SlashCompleter(Completer):
    """Complete slash commands as the first word; directory paths after
    commands that take one (/cd, /add-dir)."""

    def __init__(self, commands: tuple[str, ...], get_cwd=os.getcwd):
        self.commands = commands
        self._dirs = PathCompleter(
            only_directories=True, expanduser=True, get_paths=lambda: [get_cwd()]
        )

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or "\n" in text:
            return
        first, sep, rest = text.partition(" ")
        if not sep:
            for command in self.commands:
                if command.startswith(text):
                    yield Completion(command, start_position=-len(text))
            return
        if first in DIR_ARG_COMMANDS:
            sub = Document(rest, cursor_position=len(rest))
            yield from self._dirs.get_completions(sub, complete_event)


# Directories skipped when indexing files for @-mention completion: they are
# dependency/VCS/cache trees that would drown real project files.
ATFILE_IGNORED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    }
)
ATFILE_SCAN_CAP = 5000
ATFILE_MAX_RESULTS = 8
ATFILE_CACHE_SECS = 3.0


def at_fragment(text: str) -> str | None:
    """The '@file' mention being typed at the cursor, or None. The '@' must
    start a word (line start or after whitespace, so emails don't trigger),
    and the mention is still open only while it contains no whitespace."""
    at = text.rfind("@")
    if at < 0 or (at > 0 and not text[at - 1].isspace()):
        return None
    fragment = text[at + 1 :]
    if any(ch.isspace() for ch in fragment):
        return None
    return fragment


class AtFileCompleter(Completer):
    """Complete '@<fragment>' anywhere in the input with project file paths,
    Claude Code style. Files are indexed recursively from the session cwd
    (junk dirs skipped, capped) and cached briefly so fast typing doesn't
    re-walk a large tree on every keystroke."""

    def __init__(self, get_cwd=os.getcwd):
        self.get_cwd = get_cwd
        self._cache: tuple[str, float, list[str]] | None = None

    def _index(self) -> list[str]:
        root = self.get_cwd()
        now = time.monotonic()
        if (
            self._cache
            and self._cache[0] == root
            and now - self._cache[1] < ATFILE_CACHE_SECS
        ):
            return self._cache[2]
        paths: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
            dirnames[:] = sorted(d for d in dirnames if d not in ATFILE_IGNORED_DIRS)
            rel = os.path.relpath(dirpath, root)
            prefix = "" if rel == "." else rel + "/"
            paths.extend(prefix + d + "/" for d in dirnames)
            paths.extend(prefix + f for f in sorted(filenames))
            if len(paths) >= ATFILE_SCAN_CAP:
                del paths[ATFILE_SCAN_CAP:]
                break
        self._cache = (root, now, paths)
        return paths

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):  # slash-command line: SlashCompleter's turf
            return
        fragment = at_fragment(text)
        if fragment is None:
            return
        needle = fragment.casefold()
        scored = []
        for path in self._index():
            name = os.path.basename(path.rstrip("/")).casefold()
            if not needle:
                score = 1
            elif name.startswith(needle):
                score = 3
            elif needle in name:
                score = 2
            elif needle in path.casefold():
                score = 1
            else:
                continue
            scored.append((-score, path))
        scored.sort()
        for _, path in scored[:ATFILE_MAX_RESULTS]:
            # Directories complete open-ended (keep typing to descend); files
            # get a trailing space that closes the mention.
            inserted = path if path.endswith("/") else path + " "
            yield Completion(inserted, start_position=-len(fragment), display=path)


class BoxPrompt:
    def __init__(self, vi_mode: bool, state_dir: Path, commands: tuple[str, ...] = ()):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.vi_mode = vi_mode
        # Rebindable after construction (the agent that owns the live cwd is
        # created later): path completion for /cd et al. resolves against it.
        self.get_cwd = os.getcwd
        self._history = FileHistory(str(state_dir / "history"))
        self._completer = merge_completers(
            [
                SlashCompleter(commands, get_cwd=lambda: self.get_cwd()),
                AtFileCompleter(get_cwd=lambda: self.get_cwd()),
            ]
        )
        self._edit_session: PromptSession[str] = PromptSession(vi_mode=vi_mode)

    def edit(self, initial: str) -> str:
        """Pre-filled single-line edit (for the approval 'e' option)."""
        return self._edit_session.prompt("edit> ", default=initial).strip()

    def read(self, cwd_display: str) -> str:
        """Show the boxed prompt and return the submitted text."""
        return self._build_app(cwd_display).run()

    def read_with_io(self, cwd_display: str, input, output) -> str:
        """read() with injected I/O — for tests driving keystrokes."""
        return self._build_app(cwd_display, input=input, output=output).run()

    def pick(self, search, initial: str = "", render=str):
        """Live-filter picker over arbitrary items: typing re-ranks via
        search(query), render(item) draws each row, Up/Down move the
        selection, Enter returns the selected item, Esc/Ctrl-C returns
        None."""
        return self._build_picker(search, initial, render).run()

    def pick_with_io(self, search, initial: str, render, input, output):
        """pick() with injected I/O — for tests driving keystrokes."""
        return self._build_picker(search, initial, render, input=input, output=output).run()

    def _build_picker(
        self, search, initial: str, render, input=None, output=None
    ) -> "Application[Any]":
        state = {"results": search(initial), "selected": 0}

        def refresh(buff):
            state["results"] = search(buff.text)
            state["selected"] = 0

        buffer = Buffer(
            document=Document(initial, cursor_position=len(initial)),
            multiline=False,
            on_text_changed=refresh,
        )

        def rows():
            results = state["results"]
            fragments = []
            for i, item in enumerate(results[:PICKER_MAX_ROWS]):
                line = " " + render(item)
                if i == state["selected"]:
                    fragments.append(("bold", "❯"))
                    fragments.append(("reverse", line + "\n"))
                else:
                    # Default foreground: dim rows are unreadable on dark themes.
                    fragments.append(("", " " + line + "\n"))
            if not results:
                fragments.append((RULE_STYLE, "  (no match — Esc cancels)\n"))
            elif len(results) > PICKER_MAX_ROWS:
                hidden = len(results) - PICKER_MAX_ROWS
                fragments.append((RULE_STYLE, f"  … {hidden} more, type to narrow\n"))
            return fragments

        keys = KeyBindings()

        @keys.add("enter")
        def _accept(event):
            results = state["results"]
            event.app.exit(result=results[state["selected"]] if results else None)

        @keys.add("up")
        @keys.add("c-p")
        def _up(event):
            state["selected"] = max(0, state["selected"] - 1)

        @keys.add("down")
        @keys.add("c-n")
        @keys.add("tab")
        def _down(event):
            visible = min(len(state["results"]), PICKER_MAX_ROWS)
            state["selected"] = min(max(visible - 1, 0), state["selected"] + 1)

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        def _cancel(event):
            event.app.exit(result=None)

        body = HSplit(
            [
                Window(
                    BufferControl(
                        buffer=buffer,
                        input_processors=[BeforeInput([("bold", "search❯ ")])],
                    ),
                    height=1,
                ),
                Window(FormattedTextControl(rows), dont_extend_height=True),
                Window(
                    FormattedTextControl(
                        [(RULE_STYLE, "type to filter · ↑/↓ select · Enter picks · Esc cancels")]
                    ),
                    height=1,
                ),
            ],
            align=VerticalAlign.TOP,
        )

        app: Application[Any] = Application(
            layout=Layout(body, focused_element=buffer),
            key_bindings=merge_key_bindings([load_key_bindings(), keys]),
            full_screen=False,
            input=input,
            output=output,
        )
        app.ttimeoutlen = 0.05
        return app

    def _build_app(self, cwd_display: str, input=None, output=None) -> "Application[str]":
        buffer = Buffer(
            history=self._history,
            completer=self._completer,
            complete_while_typing=True,
            multiline=False,  # Enter accepts; Alt+Enter and pastes still insert \n
            accept_handler=lambda buff: get_app().exit(result=buff.text) or True,
        )

        vi_mode = self.vi_mode

        def bottom_bar():
            """The bottom rule doubles as completion bar and vi-mode indicator.
            On the final render after submit (app.is_done) it goes back to a
            plain rule so no stale mode label lingers on screen."""
            app = get_app()
            width = app.output.get_size().columns
            state = buffer.complete_state
            if not app.is_done and state and state.completions:
                fragments = [(RULE_STYLE, "─── ")]
                for i, completion in enumerate(state.completions):
                    current = completion is state.current_completion
                    fragments.append(
                        ("reverse" if current else RULE_STYLE, completion.display_text)
                    )
                    if i < len(state.completions) - 1:
                        fragments.append((RULE_STYLE, " · "))
                fragments.append((RULE_STYLE, " "))
                used = sum(len(text) for _, text in fragments)
                fragments.append((RULE_STYLE, "─" * max(0, width - used)))
                return fragments
            if app.is_done or not vi_mode:
                return [(RULE_STYLE, "─" * width)]
            mode = app.vi_state.input_mode
            if mode == InputMode.NAVIGATION:
                label = " NORMAL "
            elif mode == InputMode.REPLACE:
                label = " REPLACE "
            else:
                label = " INSERT "
            return [(RULE_STYLE, "─" * 3 + label + "─" * max(0, width - len(label) - 3))]

        keys = KeyBindings()

        @keys.add("enter")
        def _accept(event):
            buff = event.current_buffer
            if buff.complete_state and buff.complete_state.current_completion:
                buff.apply_completion(buff.complete_state.current_completion)
            elif buff.document.text_before_cursor.endswith("\\"):
                # backslash continuation: works in any terminal
                buff.delete_before_cursor()
                buff.insert_text("\n")
            else:
                buff.validate_and_handle()

        # Alt/Option+Enter (iTerm2 needs Option=Esc+) — and Ctrl+J, which is a
        # distinct control code in every terminal, no configuration needed.
        @keys.add("escape", "enter")
        @keys.add("c-j")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        @keys.add("tab")
        def _complete(event):
            event.current_buffer.complete_next()

        @keys.add("s-tab")
        def _complete_back(event):
            event.current_buffer.complete_previous()

        @keys.add("c-c")
        def _abort(event):
            event.app.exit(exception=KeyboardInterrupt())

        @keys.add("c-d", filter=Condition(lambda: not buffer.text))
        def _eof(event):
            event.app.exit(exception=EOFError())

        body = HSplit(
            [
                Window(
                    FormattedTextControl([(RULE_STYLE, cwd_display)]), height=1
                ),
                Window(height=1, char="─", style=RULE_STYLE),
                Window(
                    BufferControl(
                        buffer=buffer,
                        input_processors=[BeforeInput([("bold", "❯ ")])],
                    ),
                    wrap_lines=True,
                    dont_extend_height=True,
                ),
                Window(FormattedTextControl(bottom_bar), height=1, style=RULE_STYLE),
            ],
            # TOP: never justify rows apart on a tall empty screen — the box
            # must hug the input regardless of space below the cursor.
            align=VerticalAlign.TOP,
        )

        app: Application[str] = Application(
            layout=Layout(body, focused_element=buffer),
            key_bindings=merge_key_bindings([load_key_bindings(), keys]),
            editing_mode=EditingMode.VI if self.vi_mode else EditingMode.EMACS,
            cursor=ModalCursorShapeConfig() if self.vi_mode else None,
            full_screen=False,
            input=input,
            output=output,
        )
        # Vim-style ttimeoutlen: flush a lone Esc after 50ms instead of the
        # 500ms default, so NORMAL mode (indicator + cursor shape) reacts
        # instantly. Terminal-sent Alt+Enter arrives as an atomic Esc+CR
        # burst, far faster than 50ms, so it still parses as one key.
        app.ttimeoutlen = 0.05
        return app
