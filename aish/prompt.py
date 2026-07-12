"""Boxed input UI: horizontal rules hugging the input line, expanding as the
entry becomes multiline; slash-command autocomplete; vi/emacs editing.

Built as a small prompt_toolkit Application (full_screen=False) instead of
PromptSession because a "footer under the input" is not something
PromptSession can render — its bottom_toolbar pins to the screen bottom.

Keys: Enter submits · Alt/Option+Enter (or Esc, Enter) inserts a newline ·
Tab / menu completes slash commands · Ctrl-C aborts input · Ctrl-D on empty
input quits.
"""

from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.cursor_shapes import ModalCursorShapeConfig
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.shortcuts import PromptSession

RULE_STYLE = "fg:ansibrightblack"


class SlashCompleter(Completer):
    """Complete slash commands, only as the first word of the input."""

    def __init__(self, commands: tuple[str, ...]):
        self.commands = commands

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text and "\n" not in text:
            for command in self.commands:
                if command.startswith(text):
                    yield Completion(command, start_position=-len(text))


class BoxPrompt:
    def __init__(self, vi_mode: bool, state_dir: Path, commands: tuple[str, ...] = ()):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.vi_mode = vi_mode
        self._history = FileHistory(str(state_dir / "history"))
        self._completer = SlashCompleter(commands)
        self._edit_session = PromptSession(vi_mode=vi_mode)

    def edit(self, initial: str) -> str:
        """Pre-filled single-line edit (for the approval 'e' option)."""
        return self._edit_session.prompt("edit> ", default=initial).strip()

    def read(self, cwd_display: str) -> str:
        """Show the boxed prompt and return the submitted text."""
        return self._build_app(cwd_display).run()

    def _bottom_rule(self) -> str:
        width = get_app().output.get_size().columns
        if not self.vi_mode:
            return "─" * width
        mode = get_app().vi_state.input_mode
        if mode == InputMode.NAVIGATION:
            label = " NORMAL "
        elif mode == InputMode.REPLACE:
            label = " REPLACE "
        else:
            label = " INSERT "
        return "─" * 3 + label + "─" * max(0, width - len(label) - 3)

    def _build_app(self, cwd_display: str) -> Application:
        buffer = Buffer(
            history=self._history,
            completer=self._completer,
            complete_while_typing=True,
            multiline=False,  # Enter accepts; Alt+Enter and pastes still insert \n
            accept_handler=lambda buff: get_app().exit(result=buff.text) or True,
        )

        keys = KeyBindings()

        @keys.add("enter")
        def _accept(event):
            buff = event.current_buffer
            if buff.complete_state and buff.complete_state.current_completion:
                buff.apply_completion(buff.complete_state.current_completion)
            else:
                buff.validate_and_handle()

        @keys.add("escape", "enter")  # Alt/Option+Enter
        def _newline(event):
            event.current_buffer.insert_text("\n")

        @keys.add("tab")
        def _complete(event):
            event.current_buffer.complete_next()

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
                ),
                Window(
                    FormattedTextControl(self._bottom_rule), height=1, style=RULE_STYLE
                ),
                ConditionalContainer(
                    CompletionsMenu(max_height=6, scroll_offset=0),
                    filter=has_completions,
                ),
            ]
        )

        return Application(
            layout=Layout(body, focused_element=buffer),
            key_bindings=merge_key_bindings([load_key_bindings(), keys]),
            editing_mode=EditingMode.VI if self.vi_mode else EditingMode.EMACS,
            cursor=ModalCursorShapeConfig() if self.vi_mode else None,
            full_screen=False,
        )
