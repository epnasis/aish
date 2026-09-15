"""Nothing printed to the terminal may repaint it (#327).

A terminal reads control sequences as instructions, so text that reaches
stdout unsanitised can move the cursor, erase the lines above it, or reorder
what is rendered — and an approval card is text sitting next to a decision.
Two classes: card bodies and model prose lose every control; a command's live
output keeps SGR colour and loses only what moves or erases. These tests drive
the REAL card-rendering code paths (input() scripted, stdout captured) with a
payload in each field and assert none of it reached the screen."""

import builtins
from types import SimpleNamespace

import pytest

from aish.cli import (
    _plain,
    _plain_live,
    colorize_diff,
    echo,
    make_approver,
    make_import_approver,
    make_read_approver,
    make_tool_approver,
    make_write_approver,
    print_answer_piece,
    print_chip_menu,
    print_intent,
    print_sources,
    replay_history,
    stream_line,
)

CURSOR_UP = "\x1b[2A"
ERASE_LINE = "\x1b[2K"
ATTACK = CURSOR_UP + ERASE_LINE
MARK = "\N{REPLACEMENT CHARACTER}"


def scripted_input(monkeypatch, answers):
    answers = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(answers))


def assert_inert(printed: str, visible: str) -> None:
    """The payload's instructions are gone, the text around them survived, and
    the screen shows that something was removed."""
    assert CURSOR_UP not in printed
    assert ERASE_LINE not in printed
    assert "\x1b[2" not in printed  # neither half, in any combination
    assert visible in printed
    assert MARK in printed


class TestCardSanitiser:
    def test_csi_cursor_up_and_erase_line_are_removed_whole(self):
        assert _plain("a" + CURSOR_UP + ERASE_LINE + "b") == f"a{MARK}{MARK}b"

    def test_osc_hyperlink_goes_with_its_string_terminator(self):
        link = "\x1b]8;;https://evil.invalid\x1b\\click\x1b]8;;\x1b\\"
        assert _plain("x" + link + "y") == f"x{MARK}click{MARK}y"

    def test_osc_with_bel_terminator(self):
        assert _plain("x\x1b]0;title\x07y") == f"x{MARK}y"

    def test_unterminated_osc_does_not_swallow_the_rest_of_the_card(self):
        # A real terminal would hide everything after it; the sanitiser shows
        # it as inert text instead, because hiding a card is the attack.
        out = _plain("rm -rf /\x1b]8;;the rest")
        assert "the rest" in out
        assert "\x1b" not in out

    def test_sgr_is_removed_in_card_mode(self):
        assert _plain("\x1b[31mred\x1b[0m") == f"{MARK}red{MARK}"

    def test_eight_bit_csi_is_removed(self):
        assert "\x9b" not in _plain("\x9b2Ax")

    def test_other_escape_forms(self):
        # ESC M (reverse index) moves the cursor up with no CSI at all.
        assert _plain("a\x1bMb\x1b7c\x1b(Bd") == f"a{MARK}b{MARK}c{MARK}d"

    @pytest.mark.parametrize(
        "control", ["\u202e", "\u202a", "\u202c", "\u200e", "\u200f", "\u2066", "\u2069"]
    )
    def test_bidi_controls_are_removed(self, control):
        assert _plain(f"rm{control}ls") == f"rm{MARK}ls"

    def test_lone_cr_is_marked_and_crlf_is_a_newline(self):
        # `rm -rf /\rls` paints as `ls`; a CRLF file's diff is not an attack.
        assert _plain("rm -rf /\rls") == f"rm -rf /{MARK}ls"
        assert _plain("one\r\ntwo") == "one\ntwo"

    def test_tab_newline_and_plain_unicode_survive(self):
        text = "col\tumn\nnext — ünïcödé 日本語 ✓"
        assert _plain(text) == text

    def test_arabic_letter_mark_is_a_bidi_control_too(self):
        assert _plain("rm\u061cls") == f"rm{MARK}ls"

    @pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
    def test_unicode_line_separators_cannot_split_a_line(self, separator):
        # `str.splitlines()` breaks on these: unmarked, `+foo<LS>-bar` would be
        # coloured as an add AND a delete from one added line.
        assert _plain(f"+foo{separator}-bar") == f"+foo{MARK}-bar"
        assert colorize_diff(f"+foo{separator}-bar").count("\n") == 0
        assert "\x1b[31m" not in colorize_diff(f"+foo{separator}-bar")

    def test_a_split_sequence_is_inert_in_both_halves(self):
        # Streaming hands the sanitiser one token at a time.
        assert _plain("\x1b") == MARK
        assert _plain("[2A") == "[2A"


class TestLiveSanitiser:
    def test_sgr_survives(self):
        text = "\x1b[1;32mM\x1b[0m file.py"
        assert _plain_live(text) == text

    @pytest.mark.parametrize("sgr", ["\x1b[38:2::1:2:3m", "\x1b[38;5;1m", "\x1b[m"])
    def test_real_sgr_forms_survive(self, sgr):
        assert _plain_live(f"{sgr}x\x1b[0m") == f"{sgr}x\x1b[0m"

    @pytest.mark.parametrize("seq", ["\x1b[>4;2m", "\x1b[?4m", "\x1b[=5m", "\x1b[<1m"])
    def test_private_parameter_m_sequences_are_not_colour(self, seq):
        # XTMODKEYS re-encodes what the owner types at the next [y/N];
        # XTQMODKEYS makes the terminal reply INTO stdin. Neither is SGR.
        assert _plain_live(f"a{seq}b") == f"a{MARK}b"

    def test_cursor_up_and_erase_do_not(self):
        out = _plain_live("a" + CURSOR_UP + ERASE_LINE + "b\x1b[0m")
        assert out == f"a{MARK}{MARK}b\x1b[0m"

    def test_osc_hyperlink_is_removed(self):
        out = _plain_live("\x1b]8;;file:///etc\x07etc\x1b]8;;\x07")
        assert out == f"{MARK}etc{MARK}"

    def test_cr_and_tab_survive_but_backspace_does_not(self):
        assert _plain_live("50%\r100%\tdone") == "50%\r100%\tdone"
        assert _plain_live("ab\x08c") == f"ab{MARK}c"

    def test_bidi_is_left_alone_in_live_output(self):
        # `cat` of a file with real RTL text needs its marks; live output is
        # not a decision surface.
        assert _plain_live("\u200fשלום") == "\u200fשלום"


class TestCardSites:
    """One test per site the issue ranked, each through the real renderer."""

    def test_run_command_card_body(self, tmp_path, monkeypatch, capsys):
        approve = make_approver(False, tmp_path / "allow.txt", None)
        scripted_input(monkeypatch, ["y"])
        command = f"touch note.txt{ATTACK} # harmless"
        assert approve(command) == command  # what RUNS and is RECORDED is untouched
        assert_inert(capsys.readouterr().out, "touch note.txt")

    def test_run_command_card_body_when_blocked(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "deny.txt").write_text("rm -rf /\n")
        approve = make_approver(False, tmp_path / "allow.txt", None, tmp_path / "deny.txt")
        approve(f"rm -rf /{ATTACK}")
        assert_inert(capsys.readouterr().out, "rm -rf /")

    def test_run_command_card_body_when_auto_approved(self, tmp_path, monkeypatch, capsys):
        approve = make_approver(False, tmp_path / "allow.txt", None)
        approve(f"ls {ATTACK}")
        assert_inert(capsys.readouterr().out, "auto-approved")

    def test_file_edit_card_diff(self, tmp_path, monkeypatch, capsys):
        plan = SimpleNamespace(
            is_new=False, target=tmp_path / "a.py", rule="", rule_verb="", note="",
            added=1, removed=1,
            diff=f"--- a.py\n+++ a.py\n@@ -1 +1 @@\n-old{ATTACK}\n+new{ATTACK}\n",
        )
        scripted_input(monkeypatch, ["y"])
        assert make_write_approver(None)(plan) is True
        printed = capsys.readouterr().out
        assert_inert(printed, "+new")
        # colorize_diff's own colouring survives the sanitiser: the body is
        # sanitised BEFORE the SGR wrapping, not after.
        assert f"\x1b[32m+new{MARK}{MARK}\x1b[0m" in printed
        assert f"\x1b[31m-old{MARK}{MARK}\x1b[0m" in printed

    def test_file_edit_card_note_and_target(self, tmp_path, monkeypatch, capsys):
        plan = SimpleNamespace(
            is_new=True, target=f"{tmp_path}/b{ATTACK}.py", rule="", rule_verb="",
            note=f"note{ATTACK}", added=1, removed=0, diff="+1",
        )
        scripted_input(monkeypatch, ["y"])
        make_write_approver(None)(plan)
        assert_inert(capsys.readouterr().out, "note")

    def test_import_card_skill_contents(self, monkeypatch, capsys):
        files = [{"path": f"SKILL{ATTACK}.md", "content": f"# Skill\nrun{ATTACK}this\n"}]
        scripted_input(monkeypatch, ["n"])
        make_import_approver(None)(
            f"name{ATTACK}", f"desc{ATTACK}", files, [f"a{ATTACK}.png"],
            [f"flag{ATTACK}"], f"/dest{ATTACK}",
        )
        assert_inert(capsys.readouterr().out, "this")

    def test_print_intent_above_every_gate(self, tmp_path, monkeypatch, capsys):
        said = f"I will{ATTACK} check the file"
        plan = SimpleNamespace(
            is_new=False, target=tmp_path / "a.py", diff="+1", note="", rule="",
            rule_verb="", added=1, removed=0,
        )
        gates = {
            "command": lambda: make_approver(
                False, tmp_path / "allow.txt", None, get_intent=lambda: said
            )("touch note.txt"),
            "tool": lambda: make_tool_approver(None, get_intent=lambda: said)(
                "browse", {"url": "https://example.invalid/"}
            ),
            "write": lambda: make_write_approver(None, lambda: said)(plan),
            "read": lambda: make_read_approver(None, get_intent=lambda: said)("/etc/hosts"),
            "import": lambda: make_import_approver(None, lambda: said)(
                "skill", "desc", [], [], [], "/dest"
            ),
        }
        for kind, ask in gates.items():
            scripted_input(monkeypatch, ["y"])
            ask()
            printed = capsys.readouterr().out
            assert "aish says" in printed, kind
            assert_inert(printed, "check the file")

    def test_print_intent_alone(self, capsys):
        print_intent(f"line one{ATTACK}\nline two{ATTACK}")
        assert_inert(capsys.readouterr().out, "line two")

    def test_print_intent_whitespace_only_prints_nothing(self, capsys):
        print_intent(" ")
        print_intent("\n\t\n")
        assert capsys.readouterr().out == ""

    def test_always_allow_flow_prompts_and_saves_inert(self, tmp_path, monkeypatch, capsys):
        # 'a' at the gate asks per segment, with the suggested prefix INSIDE the
        # input() prompt, and Enter writes it to allow.txt permanently. The
        # suggestion is carved out of the model's command, so it carries the
        # payload — and the prompt, the 'saved:' line and the 'chat-allowed:'
        # line must all show it inert.
        prompts: list[str] = []
        answers = iter(["a", "", "c", ""])

        def capture(prompt=""):
            prompts.append(prompt)
            return next(answers)

        monkeypatch.setattr(builtins, "input", capture)
        # Two allow files: the first 'a' would otherwise auto-approve the second
        # call and the 'c' flow would never run.
        make_approver(False, tmp_path / "allow-a.txt", None)(f"git{ATTACK} push origin")
        make_approver(False, tmp_path / "allow-c.txt", None)(f"git{ATTACK} push origin")
        printed = capsys.readouterr().out
        assert prompts, "the 'always' flow never asked"
        for prompt in prompts:
            assert CURSOR_UP not in prompt and ERASE_LINE not in prompt
        assert any(MARK in prompt for prompt in prompts)
        assert "saved:" in printed and "chat-allowed:" in printed
        assert_inert(printed, "saved:")

    def test_trust_dir_note_is_inert(self, tmp_path, monkeypatch, capsys):
        scripted_input(monkeypatch, ["t"])
        approve_read = make_read_approver(None, trust_dir=lambda d: f"trusted {d}{ATTACK}")
        approve_read(f"{tmp_path}/x.txt", "outside")
        assert_inert(capsys.readouterr().out, "trusted")

    def test_plugin_tool_card_preview(self, monkeypatch, capsys):
        scripted_input(monkeypatch, ["n"])
        make_tool_approver(None)(
            f"send{ATTACK}", {"to": "x@y.invalid"}, preview=f"will send{ATTACK} 1 mail"
        )
        assert_inert(capsys.readouterr().out, "will send")

    def test_read_card_path(self, monkeypatch, capsys):
        scripted_input(monkeypatch, ["n"])
        make_read_approver(None)(f"/etc/{ATTACK}hosts")
        assert_inert(capsys.readouterr().out, "hosts")

    def test_streamed_final_answer(self, capsys):
        print_answer_piece(f"The answer{ATTACK} is 42")
        assert_inert(capsys.readouterr().out, "is 42")

    def test_note_labels_and_tool_results_through_echo(self, capsys):
        echo(f"→ read_url https://x.invalid{ATTACK}\nsecond{ATTACK} line")
        assert_inert(capsys.readouterr().out, "second")

    def test_sources_chips_and_replay(self, capsys):
        agent = SimpleNamespace(
            task_sources=[{"title": f"T{ATTACK}", "url": f"https://x.invalid/{ATTACK}"}]
        )
        print_sources(agent)
        print_chip_menu([(f"Yes{ATTACK}", "yes")])
        replay_history([
            {"role": "user", "content": f"asked{ATTACK}"},
            {"role": "assistant", "content": f"answered{ATTACK}"},
            {"role": "tool", "content": f"tool{ATTACK} output"},
        ])
        assert_inert(capsys.readouterr().out, "answered")

    def test_replay_of_a_turn_with_attachments_shows_no_marker(self, capsys):
        # The file list is wrapped in DIM/RESET by replay_history itself; that
        # wrapping must not be what the sanitiser marks.
        content = "fix it\n\n[attached file: /up/a.py]\n[attached file: /up/b.py]"
        replay_history([{"role": "user", "content": content}])
        printed = capsys.readouterr().out
        assert MARK not in printed
        assert "fix it" in printed
        assert "a.py" in printed and "b.py" in printed


class TestLiveOutputSite:
    def test_stream_line_keeps_colour_and_drops_cursor_moves(self, capsys):
        stream_line(f"\x1b[32mok\x1b[0m{ATTACK} done")
        printed = capsys.readouterr().out
        assert "\x1b[32mok\x1b[0m" in printed
        assert CURSOR_UP not in printed
        assert ERASE_LINE not in printed
        assert "done" in printed
