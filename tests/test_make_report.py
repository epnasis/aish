"""make_report: the model's door onto the PDF renderer.

The session that asked for this (2026-10-18) had no such door: the model probed
for converters, ran a reportlab script through five approval cards, saved into
~/Downloads and named a path in prose. These pin the replacement — one call, no
card, nothing fetched, a file in the chat's scratch workspace, and a chip the
owner is given even when the answer forgets it.
"""

from __future__ import annotations

import io
import re

import pytest
from test_agent import FakeChat, model_says, tool_call

from aish import agent as agent_module
from aish import export
from aish.agent import EGRESS_TOOLS, READ_ONLY_TOOLS, REPORT_LINK_NOTE, Agent, surface_slot


def _agent(tmp_path, **kwargs):
    kwargs.setdefault("approve", lambda _cmd: pytest.fail("make_report must not prompt"))
    chat = FakeChat([])
    agent = Agent(
        model="fake", client_chat=chat, cwd=str(tmp_path),
        state_dir=str(tmp_path / "state"), **kwargs,
    )
    return agent, chat


def _report_result(agent):
    return next(
        m["content"] for m in agent.messages
        if m.get("role") == "tool" and m.get("tool_name") == "make_report"
    )


def _line(result):
    match = re.search(r"^\[[^\]]+\]\(/[^)]+\.pdf\)$", result, re.MULTILINE)
    assert match, result
    return match.group(0)


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, "PNG")
    return buffer.getvalue()


def test_it_needs_no_card_and_fetches_nothing():
    assert "make_report" in READ_ONLY_TOOLS
    assert "make_report" not in EGRESS_TOOLS


@pytest.fixture
def made(tmp_path):
    """An agent that made one report and answered, and the line it was handed."""

    def run(answer_for):
        agent, chat = _agent(tmp_path)
        chat.responses = [
            model_says(tool_calls=[tool_call(
                "make_report", title="Paryż — wyjazd", markdown="# Lot\n\n- WAW → CDG",
            )]),
        ]
        # The second response is written once the first result is known.
        original = agent.chat

        def respond(**kwargs):
            if not chat.responses:
                result = next(
                    m["content"] for m in kwargs["messages"]
                    if isinstance(m, dict) and m.get("tool_name") == "make_report"
                )
                chat.responses.append(model_says(answer_for(_line(result))))
            return original(**kwargs)

        agent.chat = respond
        return agent, agent.run_task("raport pdf")

    return run


def test_the_report_is_a_pdf_named_after_its_title_inside_scratch(made, tmp_path):
    agent, answer = made(lambda line: f"Gotowe:\n\n{line}")
    line = _line(_report_result(agent))
    path = agent.scratch_dir / "reports" / "Paryż-wyjazd.pdf"
    assert line == f"[{path.name}]({path})"
    assert path.read_bytes().startswith(b"%PDF")
    assert "1 page(s)" in _report_result(agent)


def test_an_answer_that_pastes_the_line_is_delivered_as_written(made):
    agent, answer = made(lambda line: f"Gotowe:\n\n{line}")
    assert answer == f"Gotowe:\n\n{_line(_report_result(agent))}"


def test_an_answer_that_forgets_the_line_is_given_it_by_aish(made):
    """The observed failure: the PDF existed and the answer named it as path
    text, which nothing renders as a chip."""
    agent, answer = made(lambda line: "Raport czeka w katalogu reports.")
    line = _line(_report_result(agent))
    assert answer.endswith(REPORT_LINK_NOTE.format(line=line))


def test_a_remote_picture_is_not_fetched_and_the_result_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "fetch_image", lambda url: pytest.fail(f"fetched {url}"))
    agent, _ = _agent(tmp_path)
    result = agent._make_report("T", "![apartament](https://example.com/a.jpg)")
    assert "https://example.com/a.jpg — a web address; it was not fetched" in result
    assert "call show_image on it" in result


def test_a_video_link_gets_no_fetched_thumbnail_and_the_result_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "fetch_image", lambda url: pytest.fail(f"fetched {url}"))
    agent, _ = _agent(tmp_path)
    result = agent._make_report("T", "[trailer](https://youtu.be/dQw4w9WgXcQ)")
    assert "its thumbnail was not fetched" in result


def test_a_report_says_it_was_made_not_exported():
    import pymupdf

    pdf, _ = export.render_report_pdf("x", "T", [])
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        text = doc[0].get_text()
    assert "Made by aish" in text and "Exported" not in text


def test_a_picture_show_image_stored_is_embedded(tmp_path):
    agent, _ = _agent(tmp_path)
    agent.media_dir.mkdir(parents=True, exist_ok=True)
    picture = agent.media_dir / "abc-flat.png"
    picture.write_bytes(_png())
    result = agent._make_report("T", f"![flat]({picture})")
    assert "NOT in the PDF" not in result
    pdf = _line(result).split("](")[1][:-1]
    assert b"/Subtype /Image" in open(pdf, "rb").read()


def test_a_picture_outside_the_session_is_named_not_read(tmp_path):
    agent, _ = _agent(tmp_path)
    result = agent._make_report("T", "![x](/etc/hosts.png)")
    assert "/etc/hosts.png — outside this session's directories" in result


def test_a_new_version_never_overwrites_the_one_an_earlier_chip_opens(tmp_path):
    agent, _ = _agent(tmp_path)
    first = _line(agent._make_report("Plan", "one")).split("](")[1][:-1]
    kept = open(first, "rb").read()
    second = _line(agent._make_report("Plan", "two")).split("](")[1][:-1]
    assert second.endswith("Plan-2.pdf")
    assert open(first, "rb").read() == kept


def test_empty_markdown_is_an_error_not_an_empty_pdf(tmp_path):
    agent, _ = _agent(tmp_path)
    assert agent._make_report("T", "  ").startswith("ERROR:")
    assert not (agent.scratch_dir / "reports").exists()


def test_the_terminal_is_not_promised_a_preview(tmp_path):
    agent, _ = _agent(tmp_path, context=surface_slot("cli"))
    result = agent._make_report("T", "x")
    assert "preview" not in result and "opens it from there" in result
    web, _ = _agent(tmp_path / "w", context=surface_slot("web"))
    assert "preview the PDF" in web._make_report("T", "x")


def test_the_owner_tapped_export_still_fetches(monkeypatch):
    """The no-fetch switch is make_report's alone."""
    fetched = []
    monkeypatch.setattr(export, "fetch_image", lambda url: fetched.append(url) or None)
    export.render_answer_pdf("![a](https://example.com/a.jpg)", "T")
    assert fetched == ["https://example.com/a.jpg"]


def test_the_tool_is_dispatched_with_its_title_as_the_trace_label():
    assert agent_module.Agent._arg_summary("make_report", {"title": "Paryż"}) == "Paryż"
