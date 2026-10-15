"""Which tools one task is handed — a per-task selection the harness makes
(epic #444 H1, arm B). `docs/tool-menu.md`.

Pure: no model call, no network, no file I/O beyond the kill switch's config
read. The agent supplies the full menu, the task text and the structural
requirements; this module returns a `Selection` and never decides what is
CALLABLE — only which schemas are sent.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import skills
from .paths import config_home

#: Always sent (while they exist on the full menu). The first eight are the
#: owner-specified core; read_skill and read_tool_output join them because
#: aish's own text instructs them: the knowledge index and the core prompt's
#: help pointer name read_skill, and a cut whose rest was cached names
#: read_tool_output with its key — a menu without them builds the miss.
CORE_TOOLS = (
    "read_url", "web_search", "run_command", "browse", "browse_act", "recall",
    "show_image", "read_file", "read_skill", "read_tool_output",
    # Widened after the off-menu live probe (#444): the model mostly does NOT
    # attempt a tool it was not handed — it declares it missing and degrades,
    # which no off_menu counter can see. These four were the offline
    # simulation's most-missed, so their misses would be the silent kind.
    "obsidian_read", "obsidian_find", "read_pdf", "show_video",
    # Epic #142 W2: "do this later" must not depend on how a time is worded.
    # Measured (replay batch 20261015-222604, gemini-3.8-flash, 3 runs): on
    # "Sprawdź po dwudziesty drugiej." ranking never selected it, and every
    # run wrote the call out as prose and asked in text instead of calling it.
    # Cost: +1,185 chars (compact; 1,246 as the brief records it) on every
    # selected menu — the native core goes 12 schemas / 11,681 -> 13 / 12,866.
    "schedule_task",
)

#: Tools admitted by ranking against the task, beyond core and the structural
#: categories. Name hits are admitted even past it. 16, not 12, after the same
#: probe: the measured miss rate at 12 was 12.7% of tasks and every unexpressed
#: miss is invisible, so the selection errs wider.
RELEVANT_TOP_K = 16

#: Tool identity vectors, beside the knowledge ones but never in their file:
#: `SemanticIndex._save` keeps only the keys of the corpus it was just asked
#: about, so two corpora in one file would evict each other on every save.
TOOL_EMBEDDINGS_FILE = "tool-embeddings.json"

#: The kill switch: either restores the full menu, the pre-#444 path.
MENU_ALL_ENV = "AISH_MENU_ALL"
MENU_CONFIG_KEY = "tool_menu"
#: Why the full menu was sent when the owner did not ask for it in so many
#: words: the switch fails closed (`requested_mode`).
VIA_CONFIG_UNREADABLE = "config_unreadable"
VIA_CONFIG_UNKNOWN = "config_unknown"

MODE_ALL = "all"
MODE_SEMANTIC = "semantic"
MODE_LEXICAL = "lexical"
#: Arm C: core + structure, the rest as one catalog line each inside a
#: `load_tool` meta-tool the model calls to load full definitions.
MODE_CATALOG = "catalog"
LOAD_TOOL = "load_tool"
CATALOG_LINE_CHARS = 100

# Categories, in the order a name is claimed: a name is recorded under the
# FIRST category that wants it.
CORE, REQUIRED, CONTINUITY, RELEVANT = "core", "required", "continuity", "relevant"
# Mid-task growth; never part of the selection made at task start.
OFF_MENU, CREATED, LOADED = "off_menu", "created", "loaded"
CATEGORIES = (CORE, REQUIRED, CONTINUITY, RELEVANT, OFF_MENU, CREATED, LOADED)

# A family prefix shorter than this is too generic to be a name hit ("gh").
_MIN_PREFIX = 4


@dataclass(frozen=True)
class ToolLine:
    """A tool as the ranker sees it: the identity line embeddings.entry_text
    builds (`name: description`), never the argument schema."""

    name: str
    description: str
    keywords: tuple[str, ...] = ()


@dataclass
class Selection:
    """One task's menu. `category` maps every admitted name to how it got in;
    `relevant` keeps the ranked rows the record carries."""

    mode: str
    category: dict[str, str] = field(default_factory=dict)
    relevant: list[dict] = field(default_factory=list)
    full_count: int = 0
    top_k: int = 0
    error: str = ""

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.category)

    def admit(self, name: str, how: str) -> bool:
        """Add a name mid-task; False when it was already on the menu."""
        if name in self.category:
            return False
        self.category[name] = how
        return True

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(CATEGORIES, 0)
        for how in self.category.values():
            out[how] += 1
        return out

    def record(self) -> dict:
        """The `context.menu` payload (contract §3.10)."""
        by: dict[str, list[str]] = {}
        for name, how in self.category.items():
            by.setdefault(how, []).append(name)
        return {
            "mode": self.mode,
            "count": len(self.category),
            "full_count": self.full_count,
            "top_k": self.top_k,
            "names": {how: sorted(names) for how, names in by.items()},
            "relevant": self.relevant,
            **({"error": self.error[:200]} if self.error else {}),
        }


def schema_name(schema: dict) -> str:
    function = schema.get("function") if isinstance(schema, dict) else None
    return str((function or {}).get("name") or "") if isinstance(function, dict) else ""


def tool_lines(full_menu: list[dict]) -> list[ToolLine]:
    lines = []
    for schema in full_menu:
        function = schema.get("function") or {}
        if name := schema_name(schema):
            lines.append(ToolLine(name, str(function.get("description") or "")))
    return lines


def menu_for(full_menu: list[dict], names: Iterable[str]) -> list[dict]:
    """The full menu filtered to `names`, in the FULL menu's order — the same
    set is always the same bytes (M3)."""
    wanted = set(names)
    return [schema for schema in full_menu if schema_name(schema) in wanted]


def requested_mode() -> tuple[str, str]:
    """(mode, via) the owner asked for: `(MODE_ALL, "env")` from the kill
    switch, `(MODE_ALL | MODE_CATALOG, "config")` from config.toml's
    `tool_menu`, else `("", "")` — select (arm B).

    Read at every task start, never cached: a replay arm overlays config.toml
    and may not set AISH_* (`docs/replay.md`).

    FAILS CLOSED. A config that exists but cannot be read or parsed, or a
    `tool_menu` value this module does not know, gives the FULL menu
    (`via` = `config_unreadable` / `config_unknown`): the switch is what an
    owner reaches for in an incident, and a typo in it must not leave him on
    the experiment he was trying to turn off. Only a missing file, or no
    `tool_menu` key, means "not asked"."""
    if os.environ.get(MENU_ALL_ENV, "").strip() not in ("", "0"):
        return MODE_ALL, "env"
    path = Path(os.environ.get("AISH_CONFIG") or config_home() / "config.toml")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "", ""
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return MODE_ALL, VIA_CONFIG_UNREADABLE
    value = str(data.get(MENU_CONFIG_KEY, "")).strip() if isinstance(data, dict) else ""
    if not value:
        return "", ""
    if value in (MODE_ALL, MODE_CATALOG):
        return value, "config"
    return MODE_ALL, VIA_CONFIG_UNKNOWN


def _first_sentence(text: str) -> str:
    sentence = " ".join(text.split()).split(". ", 1)[0].rstrip(".")
    if len(sentence) > CATALOG_LINE_CHARS:
        sentence = sentence[: CATALOG_LINE_CHARS - 1].rstrip() + "…"
    return sentence


def load_tool_schema(full_menu: list[dict], loaded: Iterable[str]) -> dict:
    """Arm C's meta-tool: one catalog line per tool not already on the menu.
    Built once per task, so its bytes are frozen with the menu (M2)."""
    skip = set(loaded)
    catalog = "\n".join(
        f"- {line.name}: {_first_sentence(line.description)}"
        for line in tool_lines(full_menu) if line.name not in skip
    )
    return {
        "type": "function",
        "function": {
            "name": LOAD_TOOL,
            "description": (
                "These tools exist but are NOT loaded. To use one you MUST first call "
                "load_tool with its name, then call it on your next turn. Example: "
                "load_tool(names=[\"gmail_search\"]).\n" + catalog
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tool names from the list above.",
                    },
                },
                "required": ["names"],
            },
        },
    }


def _name_hit(name: str, task_padded: str) -> bool:
    """The tool named in the message: its name, its name with `_` as spaces,
    or its family prefix (`gmail` for `gmail_search`) as a whole word. The
    prefix passes the knowledge layer's stopword filter, so `read` and `show`
    never fire a family."""
    cf = name.casefold()
    if f" {cf} " in task_padded or f" {cf.replace('_', ' ')} " in task_padded:
        return True
    prefix = cf.split("_", 1)[0]
    if prefix == cf or len(prefix) < _MIN_PREFIX:
        return False
    return bool(list(skills._content_words(prefix))) and skills._word_in(task_padded, prefix)


def _lexical_score(line: ToolLine, task_padded: str) -> int:
    """How many distinct content words of the description occur in the
    message — the knowledge layer's description net, counted."""
    words = set(skills._content_words(line.description))
    return sum(1 for word in words if skills._word_in(task_padded, word))


def select(
    full_menu: list[dict],
    task: str,
    query: str,
    *,
    core: Iterable[str] = CORE_TOOLS,
    required: Iterable[str] = (),
    continuity: Iterable[str] = (),
    scores: Callable[[str, list], dict | None] | None = None,
    error: Callable[[], str | None] | None = None,
    top_k: int | None = None,
) -> Selection:
    """One task's menu. `task` is the message the name rail reads; `query`
    is what similarity ranks against (`skills.retrieval_query`). `scores` is
    `SemanticIndex.scores` or None; when absent or failing, the lexical floor
    ranks instead (knowledge-layer L2). Names not on `full_menu` are dropped
    from every category: selection never invents a tool."""
    lines = tool_lines(full_menu)
    order = {line.name: i for i, line in enumerate(lines)}
    k = RELEVANT_TOP_K if top_k is None else top_k
    selection = Selection(mode=MODE_LEXICAL, full_count=len(lines), top_k=k)
    for how, names in ((CORE, core), (REQUIRED, required), (CONTINUITY, continuity)):
        for name in names:
            if name in order:
                selection.admit(name, how)

    candidates = [line for line in lines if line.name not in selection.category]
    sims = scores(query, candidates) if scores is not None and candidates else None
    if sims is None and scores is not None and error is not None:
        selection.error = error() or ""
    task_padded = skills._pad_words(task)
    rows = []
    for line in candidates:
        rail = _name_hit(line.name, task_padded)
        if sims is not None:
            score: float = round(float(sims.get(id(line), 0.0)), 4)
        else:
            score = _lexical_score(line, task_padded)
            if not rail and score <= 0:
                continue
        rows.append((not rail, -score, order[line.name], line.name, rail, score))
    if sims is not None:
        selection.mode = MODE_SEMANTIC
    rows.sort()
    taken = 0
    for _, _, _, name, rail, score in rows:
        if not rail and taken >= k:
            break
        selection.admit(name, RELEVANT)
        taken += 1
        row: dict = {"name": name, "score": score}
        if rail:
            row["rail"] = True
        selection.relevant.append(row)
    return selection
