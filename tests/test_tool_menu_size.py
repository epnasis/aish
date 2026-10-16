"""Keep the native tool menu the imperative floor, not an essay collection.

The menu rides on EVERY model call (byte-frozen call to call — #404's prefix
stability), so on a local model it is paid in attention far more than in
tokens: measured 2026-09-23 (request digest 712c7b86efe5…), the full menu was
80k chars — 20k tokens, 57% of a 35k-token request whose user message was 52
chars, with the native half at 39.7k chars. The cut to ~31.6k kept every
MUST/NEVER and one example per tool and moved the narrative WHY to the system
prompt and docs/ (the doctrine comment above TOOL_SCHEMAS in aish/tools.py).
These budgets are what stops the essays growing back one helpful sentence at a
time; `aish tooluse` is where a cut that breaks CALLING — failures, gate
refusals, calls to tools not on the menu — would show up. Whether the RIGHT
tool was chosen is in no record, and neither these budgets nor that report
claim it.
"""

from __future__ import annotations

import json

from aish import tools

# The whole native menu, in compact JSON. NOT the same unit as the sizes
# `aish tooluse` reads back: the brief records the menu with spaced separators
# and with plugin tools included (`Agent._record_brief`), so its numbers run a
# couple of percent higher plus the plugins. This fence bounds growth; it does
# not predict that report's figure.
#
# Raised 34,000 -> 34,400 on 2026-10-06 (H3, epic #444), and the trade is
# stated so it is never mistaken for drift: the from-scratch core moved the
# browse consent and mechanics clauses and the knowledge-findability clause
# out of the system prompt and onto the tools they govern, +808 chars here
# (33,516 -> 34,324), while the system prompt fell 45,351 -> 4,893 chars on the
# web and 42,350 -> 5,001 in the terminal (gemini identity in place) — about
# -40k for +0.8k, both paid on every call.
#
# Raised 34,400 -> 35,600 on 2026-10-15 (epic #142 W2, S1) for ONE new tool,
# schedule_task (+1,185 chars: 34,395 -> 35,580). It is a new capability rather
# than an essay — the description is 365 chars, its MUST and one example — and
# the alternative, making the model say "come back after 10pm", is the failure
# the slice exists to remove. Under the per-task menu (#444 H1) it is sent only
# to tasks that select it; the full-menu path pays it on every call.
#
# Raised 35,600 -> 35,800 on 2026-10-16 (epic #142 W2, S3) for schedule_task's
# `kind` and `notify` arguments (+179 chars: 35,600 -> 35,779) — two enums and a
# 46-char description, no essay. The monitor protocol itself rides the run's
# prompt, which only a monitor run pays for, never this menu.
MAX_MENU_CHARS = 35_800

# One tool's description. The longest survivor of the cut (read_media) is 771;
# a description that needs more than this is an essay wanting to be a doc.
MAX_DESCRIPTION_CHARS = 900

# One parameter's description — fenced separately because that is where the
# biggest removed essays lived (create_tool's returns/preview/wrapper), and an
# aggregate budget alone leaves ~2.4k of headroom for them to grow back into.
# The longest survivor of the cut is 433.
MAX_PARAM_DESCRIPTION_CHARS = 550


def _param_descriptions(properties: dict) -> list[tuple[str, int]]:
    out = []
    for name, prop in properties.items():
        out.append((name, len(prop.get("description", ""))))
        items = prop.get("items")
        if isinstance(items, dict):
            out.extend(_param_descriptions(items.get("properties") or {}))
    return out


def test_the_native_menu_stays_inside_its_budget() -> None:
    size = sum(len(json.dumps(s, separators=(",", ":"))) for s in tools.TOOL_SCHEMAS)
    assert size <= MAX_MENU_CHARS, (
        f"TOOL_SCHEMAS serializes to {size} chars, over the {MAX_MENU_CHARS} "
        "budget. Descriptions are the imperative floor — a MUST and one "
        "example; new rationale belongs in the system prompt or docs/, not on "
        "every model call."
    )


def test_no_single_description_grows_back_into_an_essay() -> None:
    over = {
        s["function"]["name"]: len(s["function"].get("description", ""))
        for s in tools.TOOL_SCHEMAS
        if len(s["function"].get("description", "")) > MAX_DESCRIPTION_CHARS
    }
    assert not over, (
        f"tool descriptions over the {MAX_DESCRIPTION_CHARS}-char budget: "
        f"{over}. Keep the MUSTs, move the WHY to docs/."
    )


def test_no_parameter_description_grows_back_into_an_essay() -> None:
    over = {
        f"{s['function']['name']}.{param}": chars
        for s in tools.TOOL_SCHEMAS
        for param, chars in _param_descriptions(
            s["function"].get("parameters", {}).get("properties") or {}
        )
        if chars > MAX_PARAM_DESCRIPTION_CHARS
    }
    assert not over, (
        f"parameter descriptions over the {MAX_PARAM_DESCRIPTION_CHARS}-char "
        f"budget: {over}. Keep the MUSTs, move the WHY to docs/."
    )
