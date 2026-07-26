"""Quick-reply chips in the CLI (#167): parsing chips out of an answer, the
numbered-menu selection, and the streaming chip-stripping filter."""

from aish.cli import (
    ChipStream,
    parse_reply_chips,
    replay_history,
    resolve_chip_selection,
)


def test_parse_chips_extracts_label_and_reply():
    answer = (
        "Ready to deploy?\n\n"
        "[Yes, deploy](aish-reply://yes, deploy now)\n"
        "[No, hold off](aish-reply://no, hold off)"
    )
    clean, chips = parse_reply_chips(answer)
    assert chips == [
        ("Yes, deploy", "yes, deploy now"),
        ("No, hold off", "no, hold off"),
    ]
    # the raw link syntax is gone from the shown body
    assert "aish-reply://" not in clean
    assert clean == "Ready to deploy?"


def test_parse_chips_url_decodes_payload():
    # matches app.js decodeURIComponent — %XX escapes are decoded
    _clean, chips = parse_reply_chips("[Show logs](aish-reply://show%20the%20logs)")
    assert chips == [("Show logs", "show the logs")]


def test_parse_chips_empty_payload_falls_back_to_label():
    _clean, chips = parse_reply_chips("[Retry](aish-reply://)")
    assert chips == [("Retry", "Retry")]


def test_answer_without_chips_is_unchanged():
    answer = "Here is the result of the analysis. No follow-up needed."
    clean, chips = parse_reply_chips(answer)
    assert chips == []
    assert clean == answer


def test_answer_with_plain_markdown_link_is_untouched():
    answer = "See [the docs](https://example.com/guide) for details."
    clean, chips = parse_reply_chips(answer)
    assert chips == []
    assert clean == answer


def test_number_selects_matching_chip():
    chips = [("Yes", "yes please"), ("No", "no thanks")]
    assert resolve_chip_selection("1", chips) == "yes please"
    assert resolve_chip_selection("2", chips) == "no thanks"


def test_non_number_input_passes_through_unchanged():
    chips = [("Yes", "yes please"), ("No", "no thanks")]
    assert resolve_chip_selection("tell me more first", chips) == "tell me more first"


def test_out_of_range_number_is_treated_as_a_normal_message():
    chips = [("Yes", "yes please")]
    assert resolve_chip_selection("5", chips) == "5"
    assert resolve_chip_selection("0", chips) == "0"


def test_number_with_no_pending_menu_is_a_normal_message():
    assert resolve_chip_selection("3", []) == "3"


def test_chip_stream_strips_chips_from_streamed_output():
    text = (
        "All set.\n\n"
        "[Run tests](aish-reply://run the tests)\n"
        "[Show diff](aish-reply://show the diff)"
    )
    out: list[str] = []
    stream = ChipStream(out.append)
    # feed one character at a time to exercise the hold-back buffer
    for ch in text:
        stream.feed(ch)
    stream.close()
    printed = "".join(out)
    assert "aish-reply://" not in printed
    assert "[Run tests]" not in printed
    assert "All set." in printed


def test_chip_stream_passes_plain_text_through():
    text = "Here is a [normal link](https://example.com) inside prose."
    out: list[str] = []
    stream = ChipStream(out.append)
    stream.feed(text)
    stream.close()
    assert "".join(out) == text


# --- resume must offer the final answer's chips, like a live turn (#167) ---


def test_replay_history_returns_final_answer_chips(capsys):
    history = [
        {"role": "user", "content": "deploy?"},
        {
            "role": "assistant",
            "content": (
                "Ready to deploy?\n\n"
                "[Yes, deploy](aish-reply://yes, deploy now)\n"
                "[No, hold off](aish-reply://no, hold off)"
            ),
        },
    ]
    pending = replay_history(history)
    assert pending == [("Yes, deploy", "yes, deploy now"), ("No, hold off", "no, hold off")]
    out = capsys.readouterr().out
    # the raw chip syntax is stripped from the replayed body...
    assert "aish-reply://" not in out
    assert "[Yes, deploy]" not in out
    # ...and the numbered menu is shown so the user can pick on resume
    assert "Ready to deploy?" in out
    assert "1." in out and "Yes, deploy" in out


def test_replay_history_trailing_user_turn_consumes_chips(capsys):
    history = [
        {"role": "assistant", "content": "Pick one:\n[A](aish-reply://a)\n[B](aish-reply://b)"},
        {"role": "user", "content": "actually, tell me more"},
    ]
    # the user already answered past the chips — nothing is pending on resume
    assert replay_history(history) == []


def test_replay_history_answer_without_chips_has_no_pending(capsys):
    history = [
        {"role": "user", "content": "status?"},
        {"role": "assistant", "content": "All done, nothing pending."},
    ]
    assert replay_history(history) == []
