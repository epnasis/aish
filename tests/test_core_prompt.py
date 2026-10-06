"""The from-scratch core prompt (H3, epic #444) and the fences that keep it small.

The grown prompt was 28.6k chars plus 16.3k (web) / 12.3k (CLI) of usage notes,
and it grew one helpful paragraph at a time because nothing measured it. The
core replaces both with ~4.3k per surface; what it dropped moved to the gate's
own just-in-time notes, the tool descriptions, or the two help skills §10 names.
These tests are what stops it growing back: a total cap, a per-section cap, and
the old usage-notes builders asserted GONE rather than shrunk.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import aish.agent as agent_module
import aish.cli as cli_module
import aish.server as server_module
import aish.skills as skills_module
from aish.agent import (
    HELP_SKILLS,
    IDENTITY_SLOT,
    SURFACES,
    SYSTEM_PROMPT_TEMPLATE,
    compose_system_content,
    environment_context,
    identity_context,
    surface_slot,
    system_prompt,
)

REPO = Path(__file__).resolve().parent.parent
HELP_SKILLS_DIR = REPO / "docs" / "shipped-skills"

# The approved cap (h3a draft, "Deliberate decisions" 3). The render measured
# at landing: web 4,253 / CLI 4,361 without the identity line.
MAX_CORE_CHARS = 8_000

# Per section, the rendered size at landing rounded up with ~100 chars of
# room. A section that needs more is a decision with its own evidence — the
# draft's rule is that no sentence ships without its enforcing line or its
# measured failure — not a sentence that slipped in.
SECTION_CAPS = {
    "You are aish": 200,
    "WHO IS SPEAKING": 700,
    "YOU PROPOSE, THE HARNESS DECIDES": 850,
    "HONESTY ABOUT SOURCES": 600,
    "WEB": 600,
    "SHELL": 400,
    "OUTPUT": 900,
    "KNOWLEDGE": 600,
    "WHAT RIDES EACH TASK": 400,
    "ABOUT AISH": 250,
}

# The longest identity any provider renders today rides inside the core, so
# the fence is measured with it in place.
LONGEST_IDENTITY = max(
    (identity_context("some-model-name", provider) for provider in (
        "ollama", "local", "gemini", "openai", "claude", "claude-max",
    )),
    key=len,
)


def _sections(text: str) -> dict[str, str]:
    out = {}
    for block in text.split("\n\n"):
        head = block.split("\n", 1)[0]
        key = "You are aish" if head.startswith("You are aish") else head
        out[key] = block
    return out


@pytest.mark.parametrize("surface", SURFACES)
def test_each_surface_render_fits_the_fence(surface):
    text = system_prompt(surface=surface, identity=LONGEST_IDENTITY)
    assert len(text) <= MAX_CORE_CHARS, (
        f"the {surface} core renders to {len(text)} chars, over {MAX_CORE_CHARS}"
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_no_section_grows_past_its_cap(surface):
    sections = _sections(system_prompt(surface=surface))
    assert set(sections) == set(SECTION_CAPS), "a section was added, renamed or merged"
    over = {k: len(v) for k, v in sections.items() if len(v) > SECTION_CAPS[k]}
    assert not over, f"sections over their cap: {over}"


def test_the_old_usage_notes_are_gone_not_shrunk():
    """The draft's own demand: the per-surface usage notes were the second
    half of the bloat, and a builder that survives as a stub is where the next
    paragraph lands. Gone means the builders are gone, the scratch rule that
    sat beside the prompt is gone, and no signature line of the old bulk
    reaches either surface's system message."""
    assert not hasattr(server_module, "web_usage_context")
    assert not hasattr(cli_module, "usage_context")
    for name in ("SCRATCH_RULE", "SCRATCH_IN_REMINDER"):
        assert not hasattr(agent_module, name), name
    signatures = (
        "About aish (you)", "Header controls", "REPL slash commands",
        "Approval prompt keys", "WORKS OFFLINE", "SCRATCH WORKSPACE", "GROUNDING",
        "Rate limits:", "Config file:", "Attachments: the web UI",
    )
    for surface in SURFACES:
        content = compose_system_content(
            f"{surface_slot(surface)}\n\n{environment_context('/tmp')}", "/tmp", index="",
        )
        found = [s for s in signatures if s in content]
        assert not found, f"{surface}: old usage-note text still composed: {found}"


def test_one_template_two_renders():
    web = system_prompt(surface="web")
    cli = system_prompt(surface="cli")
    for text in (web, cli):
        assert "«" not in text and "»" not in text
    assert "speaking with him through the aish web UI." in web
    assert "speaking with him through this terminal." in cli
    assert "each on its own line (one-tap buttons);" in web
    assert "each on its own line (a numbered pick menu);" in cli
    assert 'read_skill("aish-web-help")' in web
    assert 'read_skill("aish-cli-help")' in cli
    # Everything outside the marks is shared, byte for byte.
    assert web.split("\n\n")[1:6] == cli.split("\n\n")[1:6]


def test_maths_is_asked_for_in_the_terminal_only():
    """#391's split, carried into the variant: the terminal prints LaTeX raw,
    the web and the PDF export render it."""
    cli = system_prompt(surface="cli")
    web = system_prompt(surface="web")
    assert "Write maths in plain Unicode" in cli
    assert "∠TBP₁ = 90° − 10° = 80°" in cli and "h/sin(45°)" in cli
    for word in ("maths", "LaTeX", "Unicode", "∠"):
        assert word not in web


def test_the_surface_is_declared_by_the_callers_context_and_stripped():
    for surface in SURFACES:
        content = compose_system_content(
            f"{surface_slot(surface)}\n\nEnvironment:\n- x", "/tmp", index=""
        )
        assert content == system_prompt(surface=surface) + "\n\nEnvironment:\n- x"
        assert "<aish:surface" not in content
    with pytest.raises(ValueError):
        surface_slot("tv")


def test_the_entry_points_declare_their_surface():
    """Read off the source: each entry point's context list opens with its
    own declaration. A missing one would silently render the CLI variant on
    the web."""
    import inspect

    assert 'surface_slot("web")' in inspect.getsource(server_module.create_app)
    assert 'surface_slot("cli")' in inspect.getsource(cli_module.main)


def test_each_render_is_byte_stable():
    identity = identity_context("gemini-3.5-flash", "gemini")
    for surface in SURFACES:
        ctx = f"{surface_slot(surface)}\n\n{environment_context('/tmp')}"
        first = compose_system_content(ctx, "/tmp", index="", identity=identity,
                                       scratch_dir="/tmp/s", scratch_in_reminder=True)
        again = compose_system_content(ctx, "/tmp", index="", identity=identity,
                                       scratch_dir="/tmp/s2", scratch_in_reminder=True)
        assert first == again  # the per-chat scratch path rides the reminder


def test_identity_fills_section_one():
    assert IDENTITY_SLOT in SYSTEM_PROMPT_TEMPLATE
    identity = identity_context("qwen3:8b", "ollama")
    text = system_prompt(surface="web", identity=identity)
    first = text.split("\n\n")[0]
    assert first.startswith("You are aish, ") and first.endswith(identity)
    assert IDENTITY_SLOT not in text
    assert IDENTITY_SLOT not in system_prompt(surface="web")  # no identity: no slot


def test_claude_max_without_a_reminder_still_gets_its_scratch_path():
    in_place = system_prompt("/tmp/scratch-x", surface="cli")
    assert in_place.endswith("Your scratch directory for this chat: /tmp/scratch-x")
    assert "/tmp/scratch-x" not in system_prompt(
        "/tmp/scratch-x", surface="cli", scratch_in_reminder=True
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_the_help_skill_the_core_names_ships_and_loads(surface, monkeypatch):
    """§10 points at a skill by name; the file in docs/shipped-skills/ must
    parse to exactly that name through the real loader, or the pointer is a
    dead end the owner meets the first time he asks aish about itself."""
    name = HELP_SKILLS[surface]
    assert f'read_skill("{name}")' in system_prompt(surface=surface)
    source = HELP_SKILLS_DIR / f"{name}.md"
    assert source.is_file(), source
    target = skills_module.GLOBAL_SKILLS_DIR
    shutil.copy(source, target / source.name)
    body = skills_module.load_skill(name, [target])
    assert not body.startswith(("ERROR", "NOTE")), body
    entry = skills_module._parse(source)
    assert entry.name == name
    assert entry.description.startswith("Use when the user asks")
    assert entry.keywords


def test_every_shipped_skill_parses_to_its_own_name():
    """The shop clause (draft decision 4) left the core for the shop-search
    skill; like the help skills it reaches the model only if the loader reads
    the file as intended — named after itself, trigger-phrased, keyworded."""
    shipped = sorted(HELP_SKILLS_DIR.glob("*.md"))
    assert {p.stem for p in shipped} == {"aish-web-help", "aish-cli-help", "shop-search"}
    for path in shipped:
        entry = skills_module._parse(path)
        assert entry.name == path.stem
        assert entry.description.startswith("Use when"), path.name
        assert entry.keywords and all(k.isascii() for k in entry.keywords), path.name


MOVED_TO_TOOLS = [
    # (tool, parameter or None, clause) — the draft's TOOL-DESC list.
    ("create_skill", "description", "name the ACTIVITY, never a bare rule"),
    ("create_skill", "description", "repair its description and keywords"),
    ("remember", "note", "name the activity it applies to"),
    ("create_rule", None, "you never write the file or YAML"),
    ("browse", None, "Reading asks nothing"),
    ("browse", None, "yes covers read_url and browse there"),
    ("browse", None, "never try another control to get around it"),
    ("browse_fill", None, "never split one across fields to avoid it"),
    ("browse_fill", "steps", "a batch that stopped did NOT send the form"),
    ("browse_act", "target", "NEVER quote a price or time that is not in the row"),
    ("browse_act", "text", "A region that did not move is at that end: stop."),
]


@pytest.mark.parametrize("tool, param, clause", MOVED_TO_TOOLS)
def test_the_moved_clauses_ride_their_tools(tool, param, clause):
    from aish import tools

    [schema] = [s["function"] for s in tools.TOOL_SCHEMAS if s["function"]["name"] == tool]
    text = (
        schema["description"] if param is None
        else schema["parameters"]["properties"][param]["description"]
    )
    assert clause in text


def test_the_help_skills_carry_the_corrected_facts():
    web = (HELP_SKILLS_DIR / "aish-web-help.md").read_text(encoding="utf-8")
    cli = (HELP_SKILLS_DIR / "aish-cli-help.md").read_text(encoding="utf-8")
    assert "Pin chat" in web  # the menu's own label (index.html)
    for text in (web, cli):
        assert "30 days" in text  # a delete is recoverable, never final
