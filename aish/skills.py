"""Knowledge store: skills (how-to playbooks) and memory (saved facts).

Both are markdown files with optional frontmatter, discovered live:

    ---
    name: sweepy
    description: Use when the user asks to sweep the inbox
    keywords: email, cleanup
    ---
    body ...

Lifecycle frontmatter (#178 P1-8): `status: disabled` retires an entry
without deleting it, and `expires: YYYY-MM-DD` retires it automatically past
that date — see entry_active for what "retired" means everywhere.

Skills live in ~/.config/aish/skills/ (global); memory entries mirror that
layout under ~/.config/aish/memory/. Legacy one-line lessons in lessons.md
are exposed as synthetic memory entries until migrated. Project-scope dirs
(./.aish/skills, ./.aish/memory — project wins on name clash) exist in the
code but are DISABLED by default (#178 P0-1): see INCLUDE_PROJECT_DIRS.

A skill is either a flat `<name>.md` or an agentskills.io folder
`<name>/SKILL.md` that may bundle `scripts/`, `references/`, `assets/`
beside the instructions (name defaults to the directory; frontmatter still
wins). read_skill names the bundled files so the model can read or run them.
Memory stays one flat file per fact — only skills take the folder form.

Progressive disclosure keeps the prompt small at any library size: a capped
index of name+description lines goes into the system prompt every task, full
bodies load on demand (read_skill), and the long tail is reachable through
the ranked `recall` search. The description line is what makes an entry
discoverable — for skills it states the trigger, for memory it IS the fact.
"""

import difflib
import re
import warnings
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .paths import config_home

GLOBAL_SKILLS_DIR = config_home() / "skills"
GLOBAL_MEMORY_DIR = config_home() / "memory"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The inline index is capped so the prompt stays small no matter how many
# entries accumulate; the cap admits every project skill first, then the most
# recently updated global ones. Output is byte-stable while files are
# unchanged (mtime order, no counts that vary per task) so API prompt caches
# survive across tasks.
INDEX_SKILLS_MAX = 30
INDEX_MEMORY_MAX = 15
# Standing rules (#178 P1-7): memory entries with `pinned: yes` frontmatter
# render in EVERY index under their own budget, exempt from the mtime-recency
# cap above — a behaviour rule ("never do X") must not silently rotate out of
# the prompt because newer facts moved past it, and WHICH rules apply must
# never depend on whose mtime moved last.
INDEX_PINNED_MAX = 20

# recall output caps — one call can never flood a small context window.
RECALL_TOP = 8
RECALL_SESSIONS = 3
RECALL_SNIPPET_CHARS = 200
RECALL_DETAIL_CHARS = 6000

# Pre-flight injection caps (issue #40): what run_task loads proactively
# into the per-task reminder instead of waiting for the model to recall.
PREFLIGHT_TOP = 4  # max entries injected per task
PREFLIGHT_MIN_SCORE = 2  # fuzzy tier 1 is too weak to inject on
PREFLIGHT_ENTRY_CHARS = 3000  # a bigger body is "oversized": teaser + read gate
PREFLIGHT_TOTAL_CHARS = 12000  # hard cap; the agent may pass a smaller budget
PREFLIGHT_HEAD_CHARS = 600  # teaser length for an oversized skill
# Cosine floor for embedding-based selection (issue #43), calibrated on
# embeddinggemma with retrieval prefixes against real tasks: true matches
# score 0.27-0.41 (incl. Polish task vs English entries), unrelated tasks
# peak near 0.21. Short identity lines compress the scale — do not expect
# textbook 0.6+ values here. This is the floor for DELIBERATE search
# (recall's ranked listing, which the model reads critically) and for
# keyword-rail confirmation; unsolicited injection uses the stricter
# PREFLIGHT_MIN_SIM below.
SEMANTIC_MIN_SIM = 0.24
# Unsolicited injection needs a higher bar than search (#183): preflight
# puts bodies straight into context, so a marginal match is pure noise
# there, and "inject nothing" must be a normal outcome — the old single
# 0.24 floor sat below the live corpus's noise floor (the median real task
# had 7-8 entries above it, so the top-4 slots ALWAYS filled). Calibrated
# on a judged audit of 481 live injections (issue #183): relevant entries
# scored median 0.458, irrelevant 0.290; replaying the verdicts, 0.35
# yields ~76% precision vs ~45% at 0.24.
PREFLIGHT_MIN_SIM = 0.35
# And the READ GATE needs a higher bar than injection (#238). Injection at 0.35
# buys ~76% precision, which is the right trade for a teaser: a near-miss costs
# some characters. The gate is not comparable — it REFUSES unrelated tool calls
# until the model submits to reading the skill — so it must not run on the same
# confidence.
#
# Measured, on the session that produced this: the owner asked for his energy
# invoices "for each apartment"; `trippy_search`, a playbook for finding hotels,
# scored 0.282 — below the injection floor, and almost exactly the audit's
# median for an IRRELEVANT entry (0.290). It was injected anyway because its
# author had listed `apartment` as a keyword, which drops the bar to
# SEMANTIC_MIN_SIM; and its body is 3 036 characters, 36 over the oversized
# line, so the teaser came with a gate. Two browse calls refused, one call spent
# reading a hotel playbook, and a sentence in the owner's chat explaining it —
# which the refusal text explicitly forbids.
#
# 0.45 is the audit's median for a RELEVANT entry (#183). Same calibration, a
# bar the gate can carry. A keyword hit is a PRIOR and never arms the gate on
# its own; only a match this strong, or the entry being NAMED, may block work.
GATE_MIN_SIM = 0.45
# A short follow-up ("show on map") is a hopeless retrieval query on its
# own — mid-conversation injections were the most random in the #183 audit.
# Below this task length the EMBEDDING query gains recent conversation
# text; the keyword/name rails still scan only the current message, because
# explicit invocation must come from what the user just said.
PREFLIGHT_CONTEXT_TASK_CHARS = 200
PREFLIGHT_CONTEXT_CHARS = 600

# save_memory keyword cap (#183): keywords are a retrieval rail and the
# model writes them — beyond a handful they stop being curated triggers.
KEYWORDS_MAX = 8

# Near-duplicate gate on save_memory (#178 P1-8): a NEW entry this similar to
# an existing one is refused with the existing entry's name, so the model
# updates instead of duplicating (force overrides). Both thresholds sit far
# above the retrieval floor on purpose — only a confident near-duplicate may
# block a save, and the exact-string check catches the trivial case first.
# The semantic value is provisional until measured on the live corpus.
DEDUP_MIN_SIM = 0.55  # identity line vs identity line through SemanticIndex.scores
DEDUP_LEXICAL_RATIO = 0.75  # difflib fallback when embeddings are unavailable

_PUNCT = ".,;:!?()[]{}<>'\"`"
FUZZY_WORD_CUTOFF = 0.75  # single query word vs single entry word

# Words that say nothing about an entry's topic, so description reverse
# matching skips them (issue #42): function words, aish-domain boilerplate,
# and generic task vocabulary — nouns and verbs any task might contain
# ("photo", "check", "status"). Description matching is a synonym net for
# TOPIC words only; an entry genuinely about photos or checking belongs to
# these words via name/keywords, which are never stopword-filtered.
# The membership test folds a trailing s/es, so singular forms suffice for
# nouns; verb inflections are listed explicitly.
_STOPWORDS = frozenset(
    """
    when what which where whether this that these those there here with without
    from into onto over under after before during instead rather always never
    only also then than them they your yours their have will would should must
    could sure make makes made need needs like some more most much many every
    each other another about being been just does user user's request asks
    asked wants tool skill command file using used uses execute executes
    first latest current local global based between missing directly

    photo image picture video link page site website data info information
    detail result list text output content version name number question answer
    time date example thing project repository status code path live

    check checking checked show showing shown find finding found create
    creating created delete deleting deleted remove removing removed write
    writing written read reading save saving saved upload uploading uploaded
    search searching searched answering analyze analyzing identify verify
    verifying work working run running
    """.split()
)


@dataclass
class Entry:
    """One knowledge item: a skill, a memory fact, or a legacy lesson line."""

    name: str
    description: str
    keywords: list[str]
    body: str
    kind: str  # "skill" | "memory"
    mtime: float = 0.0
    path: Path | None = None
    words: frozenset = field(default_factory=frozenset)
    status: str = ""  # "disabled" retires an entry without deleting it (#178 P1-8)
    expires: date | None = None  # past this date the entry acts disabled
    pinned: bool = False  # standing rule: always indexed, own budget (#178 P1-7)


# SECURITY (#178 P0-1, interim): project-scope discovery is OFF by default.
# A cloned repository's ./.aish/skills + ./.aish/memory would otherwise inject
# attacker-controlled prompt text into messages[0] on the first task after a
# /cd into it. The code path stays alive behind this switch so the future
# per-directory trust grant can re-enable it; NEITHER entry point (cli.py,
# server.py) may set it — only tests opt in (the `project_scope` fixture).
# tool_plugins.INCLUDE_PROJECT_DIRS is the same switch for ./.aish/tools.
INCLUDE_PROJECT_DIRS = False


def skill_dirs(cwd: str) -> list[Path]:
    if INCLUDE_PROJECT_DIRS:
        return [Path(cwd) / ".aish" / "skills", GLOBAL_SKILLS_DIR]
    return [GLOBAL_SKILLS_DIR]


def memory_dirs(cwd: str) -> list[Path]:
    if INCLUDE_PROJECT_DIRS:
        return [Path(cwd) / ".aish" / "memory", GLOBAL_MEMORY_DIR]
    return [GLOBAL_MEMORY_DIR]


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:48].rstrip("-")


def _build_words(*texts: str) -> frozenset:
    return frozenset(
        w.strip(_PUNCT) for text in texts for w in text.casefold().split()
    ) - {""}


# The frontmatter terminator, anchored to its OWN LINE. A naive `split("---")`
# treats a `---` anywhere — including mid-sentence in a description — as the
# end of the header, so every key below it becomes prose. In the rules corpus
# that landed a compiled rule with no prohibition while the diff the owner
# approved visibly contained `never_use: [web_search]`: the diff said one
# thing, the card said another, and the file behaved like the card (#191).
# Quoting cannot fix it (`"a --- b"` still contains the marker), and "empty
# --- say so" is ordinary writing rather than an attack.
#
# This lives here, beside `lifecycle_active` and `parse_expiry`, because
# skills, memory, rules and plugin tools are four artifact classes in ONE
# md+frontmatter family, and "where does the header end" must have a single
# reading across all four (#209). rules.py, tool_plugins.py and curate.py
# import it; `TestOneFrontmatterReader` fails if a fifth reading appears.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)(?:\r?\n)?^---[ \t]*\r?$\n?",
                             re.DOTALL | re.MULTILINE)


def split_frontmatter(text: str) -> tuple[str, str]:
    """(header, body). ("", text) when there is no well-formed frontmatter."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return "", text
    return match.group(1), text[match.end():]


def frontmatter_value(value: str) -> str:
    """A model-authored string collapsed to something that cannot mean more
    than one frontmatter line.

    Every writer in this family interpolates raw (`description: {text}`), so a
    value carrying a newline does not become a broken line — it becomes EXTRA
    KEYS. `save_memory(keywords="alpha\\nstatus: disabled\\nbeta")` wrote a
    file that parses back `status: disabled`, so the entry was retired from
    the index, preflight and recall, while the tool's own result line said
    `remembered (probe): a harmless fact` with no `[disabled]` marker. Same
    shape as the `_yaml_scalar` colon-newline bug (#191): the artifact means
    something its confirmation does not say.

    `save_memory` already flattened the FACT this way, which is the only
    reason the description path was closed — a mitigation nobody had written
    down as load-bearing. It is named and shared now so a writer that forgets
    it is visible rather than merely unlucky (#209).
    """
    return " ".join(str(value).split())


def _parse(path: Path, kind: str = "skill") -> Entry:
    """Entry from a markdown file — name defaults to the filename (or, for a
    folder skill's SKILL.md, its directory name), description to the first
    non-empty body line."""
    text = path.read_text(encoding="utf-8")
    default_name = path.parent.name if path.name == "SKILL.md" else path.stem
    name, description, keywords = default_name, "", []
    status, expires, pinned = "", None, False
    front, body = split_frontmatter(text)
    for line in front.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "name" and value.strip():
            name = value.strip()
        elif key == "description":
            description = value.strip()
        elif key == "keywords":
            keywords = [w.strip() for w in value.split(",") if w.strip()]
        elif key == "status":
            status = value.strip().casefold()
        elif key == "expires":
            expires = parse_expiry(value.strip(), path)
        elif key in ("pinned", "kind"):  # `kind: policy` = alias for pinned
            pinned = pinned or value.strip().casefold() in (
                "yes", "true", "1", "on", "policy",
            )
    if not description:
        for line in body.strip().splitlines():
            if line.strip():
                description = line.strip().lstrip("# ").strip()
                break
    body = body.strip()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return Entry(
        name=name,
        description=description,
        keywords=keywords,
        body=body,
        kind=kind,
        mtime=mtime,
        path=path,
        words=_build_words(name, description, " ".join(keywords), body),
        status=status,
        expires=expires,
        pinned=pinned,
    )


def parse_expiry(value: str, path: Path) -> date | None:
    """Tolerant, dependency-free: a malformed date means no expiry (the entry
    stays live — failing OPEN keeps knowledge available), with a warning so
    the typo gets noticed."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        warnings.warn(
            f"{path}: unparseable expires date {value!r} ignored (use YYYY-MM-DD)",
            stacklevel=2,
        )
        return None


def lifecycle_active(status: str, expires: date | None, today: date | None = None) -> bool:
    """The retire primitives (#178 P1-8), as a predicate over the two frontmatter
    fields alone: `status: disabled`, or a past `expires: YYYY-MM-DD` date (an
    entry stays valid through its expiry day). Split out from `entry_active` so
    the rules corpus (#191) inherits the SAME lifecycle rather than growing a
    parallel one that drifts — rules are a fourth artifact class in the same
    md+frontmatter family, differing in binding semantics, not in how they retire.
    Expiry is evaluated at read time, never baked in at parse time, so a
    long-running process crosses the boundary without an mtime change."""
    if status == "disabled":
        return False
    if expires is not None and (today or date.today()) > expires:
        return False
    return True


def entry_active(entry: Entry, today: date | None = None) -> bool:
    """Lifecycle for a knowledge entry. Inactive entries are excluded from
    load_entries — and with it the index, preflight, recall, and save_memory's
    duplicate checks — while load_skill names the reason instead of claiming the
    entry is missing."""
    return lifecycle_active(entry.status, entry.expires, today)


# Parsed entries keyed by path; re-parse only when the file's mtime moved.
# At thousands of files a scan is then just glob + stat per call.
_CACHE: dict[Path, tuple[float, Entry]] = {}


def _dir_entries(directory: Path, kind: str) -> list[Entry]:
    entries: list[Entry] = []
    try:
        files = sorted(directory.glob("*.md"))
    except OSError:
        return entries
    # Folder skills (agentskills.io layout): <directory>/<name>/SKILL.md,
    # bundling scripts/ references/ assets/ beside the instructions. Only
    # skills use the folder form; memory stays one fact per flat file.
    if kind == "skill":
        try:
            subdirs = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            subdirs = []
        files += [sub / "SKILL.md" for sub in subdirs if (sub / "SKILL.md").is_file()]
    for path in files:
        try:
            mtime = path.stat().st_mtime
            cached = _CACHE.get(path)
            if cached is not None and cached[0] == mtime:
                entries.append(cached[1])
                continue
            entry = _parse(path, kind)
        except OSError:
            continue
        _CACHE[path] = (mtime, entry)
        entries.append(entry)
    return entries


def _merged(dirs: list[Path], kind: str) -> list[Entry]:
    """Entries across dirs, earlier dirs winning on name clash (project
    before global), each dir's globals in filename order."""
    seen: dict[str, Entry] = {}
    for directory in dirs:
        for entry in _dir_entries(directory, kind):
            seen.setdefault(entry.name, entry)
    return list(seen.values())


def _lesson_entries(lessons_path) -> list[Entry]:
    """Legacy lessons.md lines as synthetic memory entries (newest first) —
    searchable and indexed until consciously migrated via /learn."""
    if lessons_path is None:
        return []
    path = Path(lessons_path)
    try:
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except OSError:
        return []
    lines = [ln.lstrip("- ").strip() for ln in text.splitlines() if ln.strip()]
    entries: list[Entry] = []
    seen: set[str] = set()
    for i, line in enumerate(reversed(lines)):
        # Content-derived slug (same recipe as save_memory): stable across
        # file edits and eligible for exact-name ranking, unlike a position
        # number. Collisions get a numeric suffix; unsluggable lines fall
        # back to their position.
        slug = _slugify(line) or f"lesson-{len(lines) - i}"
        base, n = slug, 2
        while slug in seen:
            slug, n = f"{base}-{n}", n + 1
        seen.add(slug)
        entries.append(
            Entry(
                name=slug,
                description=line,
                keywords=[],
                body=line,
                kind="memory",
                mtime=mtime,
                words=_build_words(line),
            )
        )
    return entries


def load_entries(cwd: str, lessons_path=None) -> list[Entry]:
    """The full ACTIVE corpus in tie-break order: project-then-global skills,
    memory entries (newest first), then legacy lessons. Disabled/expired
    entries never leave this function — every consumer (index, preflight,
    recall, dedup) inherits the retirement for free."""
    skills = [e for e in _merged(skill_dirs(cwd), "skill") if entry_active(e)]
    memory = [e for e in _merged(memory_dirs(cwd), "memory") if entry_active(e)]
    memory.sort(key=lambda e: e.mtime, reverse=True)
    return skills + memory + _lesson_entries(lessons_path)


def list_skills(dirs: list[Path]) -> list[tuple[str, str]]:
    """(name, description) pairs; earlier dirs win on duplicate names."""
    return sorted(
        (e.name, e.description) for e in _merged(dirs, "skill") if entry_active(e)
    )


def knowledge_index(cwd: str, lessons_path=None, on_index=None) -> str:
    """The capped Skills + Memory sections of the system prompt, rebuilt
    every task so new entries appear without a restart. Empty string when
    nothing exists.

    `on_index` receives what this composition SELECTED and what it left out —
    the `context` record's payload (docs/trace-contract.md §3.10). A callback,
    not a changed return type, for the reason `save_memory(on_admission=…)`
    is one: the string return is what `compose_system_content`, cli.py and
    every test read.

    Names only, never descriptions. The record has to stay bounded enough to
    emit on EVERY task, and the name identifies the entry — which is the
    question ("which entry told it that?"), where the description would be
    an unbounded second copy of the prompt."""
    sections = []
    selected: list[dict] = []
    *project_dirs, global_dir = skill_dirs(cwd)  # project dirs only when opted in (#178)
    project = [e for d in project_dirs for e in _dir_entries(d, "skill") if entry_active(e)]
    names = {e.name for e in project}
    globals_ = [
        e
        for e in _dir_entries(global_dir, "skill")
        if e.name not in names and entry_active(e)
    ]
    globals_.sort(key=lambda e: e.mtime, reverse=True)
    room = max(0, INDEX_SKILLS_MAX - len(project))
    skills = project + globals_[:room]
    hidden = len(globals_) - min(room, len(globals_))
    if skills:
        selected += [{"label": e.name, "kind": e.kind, "slot": "skill"} for e in skills]
        lines = "\n".join(f"- {e.name}: {e.description}" for e in skills)
        note = (
            f"\n(…and {hidden} more skills — find them with recall(<what you are doing>))"
            if hidden > 0
            else ""
        )
        sections.append(
            "Skills — proven playbooks; each description states when to use it. "
            "Highly relevant ones are preloaded into your context each task; "
            "if one matches and was NOT preloaded, call read_skill(<name>) "
            "before acting: follow the skill over your built-in approach "
            "from training data.\n" + lines + note
        )
    memory = [e for e in _merged(memory_dirs(cwd), "memory") if entry_active(e)]
    memory.sort(key=lambda e: e.mtime, reverse=True)
    lessons = _lesson_entries(lessons_path)
    memory += lessons
    # Standing rules (#178 P1-7): pinned entries render under their own budget,
    # exempt from the recency cap, sorted by NAME — a touched file must never
    # change which rules apply, and a stable order keeps the index byte-stable.
    pinned = sorted((e for e in memory if e.pinned), key=lambda e: e.name)
    unpinned = [e for e in memory if not e.pinned]
    if pinned:
        selected += [
            {"label": e.name, "kind": e.kind, "slot": "pinned"}
            for e in pinned[:INDEX_PINNED_MAX]
        ]
        lines = "\n".join(f"- {e.description}" for e in pinned[:INDEX_PINNED_MAX])
        note = (
            f"\n(…and {len(pinned) - INDEX_PINNED_MAX} more standing rules — "
            "search them with recall(<topic>))"
            if len(pinned) > INDEX_PINNED_MAX
            else ""
        )
        sections.append(
            "Standing rules — pinned memory; you MUST apply every rule below "
            "to EVERY task, without being asked:\n" + lines + note
        )
    shown = unpinned[:INDEX_MEMORY_MAX]
    if shown:
        selected += [{"label": e.name, "kind": e.kind, "slot": "memory"} for e in shown]
        lines = "\n".join(f"- {e.description}" for e in shown)
        note = (
            "\n(…and more saved memory — search it with recall(<topic>))"
            if len(unpinned) > INDEX_MEMORY_MAX
            else ""
        )
        if lessons:
            note += (
                "\n(some of these are legacy one-line lessons — if the user "
                "wants them organized, /learn lessons migrates them into "
                "structured memory)"
            )
        sections.append(
            "Memory — facts and lessons you saved earlier; apply them "
            "proactively:\n" + lines + note
        )
    text = "\n\n".join(sections)
    if on_index is not None:
        on_index(
            {
                "items": selected,
                # The thresholds in force AT THE TIME. They live as constants
                # in source, so a log recording only the outcome cannot be
                # re-read after someone moves one (contract §3.8, `floors`).
                "caps": {
                    "skills": INDEX_SKILLS_MAX,
                    "pinned": INDEX_PINNED_MAX,
                    "memory": INDEX_MEMORY_MAX,
                },
                # `memory` counts unpinned entries INCLUDING legacy lessons,
                # which is what competes for the mtime-recency cap.
                "corpus": {
                    "skills": len(project) + len(globals_),
                    "pinned": len(pinned),
                    "memory": len(unpinned),
                    "lessons": len(lessons),
                },
                # The abstentions. "Why did it know that?" and "why did my
                # entry NOT reach the prompt?" are the same question asked
                # from either side, and only one of them was answerable.
                "omitted": {
                    "skills": hidden,
                    "pinned": max(0, len(pinned) - INDEX_PINNED_MAX),
                    "memory": max(0, len(unpinned) - INDEX_MEMORY_MAX),
                },
                "chars": len(text),
            }
        )
    return text


def _block_header(entry: Entry) -> str:
    """"[kind: name] description" — the description rides along unless it is
    already the body's opening line, because for memories saved via `remember`
    the description often IS the whole fact and the body is empty (#41)."""
    if entry.description and entry.description not in entry.body:
        return f"[{entry.kind}: {entry.name}] {entry.description}"
    return f"[{entry.kind}: {entry.name}]"


def load_skill(name: str, dirs: list[Path]) -> str:
    if not NAME_RE.match(name or ""):
        return f"ERROR: invalid skill name {name!r}"
    for entry in _merged(dirs, "skill"):
        if entry.name == name:
            if not entry_active(entry):
                reason = (
                    "status: disabled"
                    if entry.status == "disabled"
                    else f"expired {entry.expires}"
                )
                return (
                    f"NOTE: skill {name!r} exists but is retired ({reason}) and is "
                    "excluded from your index, preflight, and recall. Do not follow "
                    f"it; edit its frontmatter at {entry.path} only if the user asks "
                    "to re-enable it."
                )
            return f"{_block_header(entry)}\n{entry.body}{_bundled_note(entry)}"
    available = ", ".join(n for n, _ in list_skills(dirs)) or "none"
    return f"ERROR: no skill named {name!r}. Available skills: {available}"


def _bundled_note(entry: Entry) -> str:
    """For a folder skill, name its bundled files so the model can reach them
    (read_file for docs, run_command for scripts). Flat skills have none."""
    if not (entry.path and entry.path.name == "SKILL.md"):
        return ""
    base = entry.path.parent
    try:
        bundled = sorted(
            str(p.relative_to(base))
            for p in base.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )
    except OSError:
        return ""
    if not bundled:
        return ""
    listing = ", ".join(bundled[:20])
    more = f" (+{len(bundled) - 20} more)" if len(bundled) > 20 else ""
    return (
        f"\n\nBundled files under {base}/: {listing}{more}\n"
        "Read one with read_file <path>; run a bundled script via run_command."
    )


def score_entries(entries: list[Entry], query: str) -> list[tuple[int, Entry]]:
    """Deterministic ranking, no LLM. Tiers: exact name, phrase in
    name/description/keywords, phrase in body, all words anywhere, fuzzy
    (difflib). Ties keep corpus order (project skills first, then newest)."""
    query_cf = " ".join(query.split()).casefold()
    words = query_cf.split()
    if not words:
        return []
    ranked = []
    for entry in entries:
        name_cf = entry.name.casefold()
        head_cf = f"{name_cf} {entry.description.casefold()} " + " ".join(
            entry.keywords
        ).casefold()
        body_cf = entry.body.casefold()
        if name_cf == query_cf:
            score = 5
        elif query_cf in head_cf:
            score = 4
        elif query_cf in body_cf:
            score = 3
        elif all(word in head_cf or word in body_cf for word in words):
            score = 2
        elif all(
            difflib.get_close_matches(word, entry.words, n=1, cutoff=FUZZY_WORD_CUTOFF)
            for word in words
        ):
            score = 1
        else:
            continue
        ranked.append((score, entry))
    ranked.sort(key=lambda pair: -pair[0])  # stable: corpus order within a tier
    return ranked


def rank_entries(entries: list[Entry], query: str, semantic=None) -> list[Entry]:
    """Ranked entries for a query, fusing embedding similarity into the
    lexical tiers when `semantic` (SemanticIndex.scores, or None) is wired
    (#178 P1-9) — recall is the path the model uses when it deliberately goes
    looking, and a Polish query must find English entries there too, not only
    in preflight. Mirrors preflight's combination rule: strong lexical hits
    (exact name / whole query in name+description+keywords) stay a
    deterministic guarantee rail on top, similarity orders everything else,
    weaker lexical tiers break similarity ties. Without `semantic` — or when
    it fails (returns None) — output is byte-identical to pure lexical."""
    scored = score_entries(entries, query)
    sims = semantic(query, entries) if semantic is not None else None
    if sims is None:
        return [entry for _, entry in scored]
    lexical = {id(entry): score for score, entry in scored}
    fused = []
    for entry in entries:  # corpus order keeps ties stable
        lex = lexical.get(id(entry), 0)
        sim = sims.get(id(entry), 0.0)
        if lex == 0 and sim < SEMANTIC_MIN_SIM:
            continue
        rail = lex if lex >= 4 else 0
        fused.append((rail, sim, lex, entry))
    fused.sort(key=lambda t: (-t[0], -t[1], -t[2]))
    return [entry for _, _, _, entry in fused]


def _word_in(task_padded: str, word: str) -> bool:
    """Whole-word hit, insensitive to a trailing s/es: keyword "hotels" must
    fire on a task saying "hotel" and vice versa (issue #41)."""
    variants = {word, word + "s"}
    if word.endswith("s"):
        variants.add(word[:-1])
    if word.endswith("es"):
        variants.add(word[:-2])
    return any(f" {v} " in task_padded for v in variants)


def _content_words(text: str):
    for word in text.casefold().split():
        word = word.strip(_PUNCT)
        if len(word) < 4:
            continue
        bases = {word}
        if word.endswith("s"):
            bases.add(word[:-1])
        if word.endswith("es"):
            bases.add(word[:-2])
        if bases.isdisjoint(_STOPWORDS):
            yield word


def _reverse_score(entry: Entry, task_padded: str) -> int:
    """Does the entry's identity appear in the task text? The forward tiers
    in score_entries need the whole query to appear inside the entry — right
    for short recall queries, hopeless for a multi-sentence task. Name and
    keyword hits land on the same tier scale so max(forward, reverse) works.
    Description content words score the preflight minimum: they are noisier
    than keywords, but stopword/length filters plus the preflight caps keep
    over-inclusion cheap, and a task phrased with a synonym the keywords
    missed ("villa" vs "hotels") must still surface the entry (issue #41).

    `task_padded` is the space-padded, punctuation-stripped task from
    _pad_words, so matches respect word boundaries: skill "gh" must not
    fire on a task containing "night"."""
    rail = _exact_rail(entry, task_padded)
    if rail:
        return rail
    for word in _content_words(entry.description):
        if _word_in(task_padded, word):
            return 2
    return 0


def _exact_rail(entry: Entry, task_padded: str) -> int:
    """Deliberate identity hits — the entry's name or a curated keyword
    appearing in the task. These stay a guarantee even when semantic
    selection is active: an author who writes `keywords: photo` means it."""
    name_cf = entry.name.casefold()
    if f" {name_cf} " in task_padded or f" {name_cf.replace('-', ' ')} " in task_padded:
        return 4
    for keyword in entry.keywords:
        keyword_cf = keyword.casefold()
        if len(keyword_cf) >= 3 and _word_in(task_padded, keyword_cf):
            return 3
    return 0


def _pad_words(text: str) -> str:
    """Casefolded words, punctuation-stripped, space-padded at both ends —
    the haystack for whole-word phrase matching."""
    words = (w.strip(_PUNCT) for w in text.casefold().split())
    return " " + " ".join(w for w in words if w) + " "


@dataclass
class Preload:
    """What run_task injects ahead of the model's first turn (issue #40)."""

    text: str = ""  # injectable knowledge blocks, "" when nothing qualifies
    names: list[str] = field(default_factory=list)  # best first, for the status echo
    unread: list[str] = field(default_factory=list)  # oversized skills the read gate enforces
    # {name, kind} for a rich client's chips, plus selection diagnostics
    # (#183): "sim"/"rail" in semantic mode, "score" in lexical mode — the
    # trace persists them so retrieval quality is auditable from logs alone.
    items: list[dict] = field(default_factory=list)
    mode: str = ""  # "semantic" | "lexical" — which selector actually ran


def preflight(
    cwd: str,
    lessons_path,
    task: str,
    char_budget: int = PREFLIGHT_TOTAL_CHARS,
    semantic=None,
    context: str = "",
) -> Preload:
    """Pre-flight retrieval: the top skills/memories matching a task,
    rendered as blocks the agent injects directly — the model wakes up with
    the content in context instead of having to remember to recall it.
    A skill too large to inject gets a teaser and its name in `unread`, which
    arms the agent's read gate until read_skill loads the full body.

    `semantic` is `SemanticIndex.scores` (or None): embedding similarity
    picks the entries. When it is absent or fails (returns None), the lexical
    word-matching tiers below remain the floor.

    Selection discipline (#183 — injection is unsolicited, so it must be
    able to ABSTAIN): pinned standing rules never compete — they are already
    in every task's index, and re-injecting their bodies only stole slots
    from actual skills. In semantic mode a name hit stays an unconditional
    guarantee (naming an entry is unambiguous), but a keyword hit is a
    strong PRIOR, not a bypass: it lowers the similarity bar to
    SEMANTIC_MIN_SIM instead of skipping it, because keywords are
    model-authored and one generic word ("code") used to guarantee
    injection on a third of all tasks. Everything else needs
    PREFLIGHT_MIN_SIM. `context` (recent conversation text) joins the
    embedding query only for short tasks — a bare follow-up is a hopeless
    query alone — while the rails keep reading only the current message.
    In lexical mode (no embeddings) the keyword rail remains a full
    guarantee: there is no similarity signal to confirm against."""
    if not task.split():
        return Preload()
    task_padded = _pad_words(task)
    # Pinned entries are standing rules: always rendered in the index,
    # never candidates for topical injection.
    entries = [e for e in load_entries(cwd, lessons_path) if not e.pinned]
    query = task
    if context and len(task) < PREFLIGHT_CONTEXT_TASK_CHARS:
        query = f"{task}\n{context[:PREFLIGHT_CONTEXT_CHARS]}"
    sims = semantic(query, entries) if semantic is not None else None
    if sims is not None:
        mode = "semantic"
        ranked = []
        for entry in entries:  # corpus order: project skills first, then newest
            rail = _exact_rail(entry, task_padded)
            sim = sims.get(id(entry), 0.0)
            # rail 4 = the entry's NAME in the task: unconditional (cosine
            # may not veto an explicit mention). rail 3 = keyword hit: a
            # strong prior that lowers the bar, never a bypass. No rail:
            # the strict injection floor.
            floor = PREFLIGHT_MIN_SIM if not rail else (
                -1.0 if rail >= 4 else SEMANTIC_MIN_SIM
            )
            if sim >= floor:
                ranked.append((rail, sim, entry))
        ranked.sort(key=lambda t: (-t[0], -t[1]))
        chosen = [(entry, {"sim": round(sim, 3), "rail": rail}) for rail, sim, entry in ranked]
    else:
        mode = "lexical"
        forward = {id(entry): score for score, entry in score_entries(entries, task)}
        picked = []
        for entry in entries:  # corpus order: project skills first, then newest
            score = max(forward.get(id(entry), 0), _reverse_score(entry, task_padded))
            if score >= PREFLIGHT_MIN_SCORE:
                picked.append((score, entry))
        picked.sort(key=lambda pair: -pair[0])  # stable: corpus order within a tier
        # The rail rides along even though lexical selection does not use it:
        # with no similarity to clear, being NAMED is the only thing that may
        # arm the read gate, and `_gate_arms` has to be able to see it.
        chosen = [
            (entry, {"score": score, "rail": _exact_rail(entry, task_padded)})
            for score, entry in picked
        ]
    blocks: list[str] = []
    names: list[str] = []
    unread: list[str] = []
    items: list[dict] = []
    remaining = char_budget
    for entry, diag in chosen[:PREFLIGHT_TOP]:
        if remaining < 200:  # no room left for anything useful
            break
        if entry.kind == "memory" or len(entry.body) <= PREFLIGHT_ENTRY_CHARS:
            header = _block_header(entry) + "\n"
            room = max(0, min(PREFLIGHT_ENTRY_CHARS, remaining - len(header) - 1))
            body = entry.body[:room]
            cut = "…" if len(body) < len(entry.body) else ""
            block = f"{header}{body}{cut}".rstrip()
        else:  # oversized skill: teaser now, full body via read_skill
            head = entry.body[:PREFLIGHT_HEAD_CHARS]
            # Whether this may BLOCK is a separate question from whether it may
            # be injected, and a stricter one — see GATE_MIN_SIM. A skill that
            # does not clear it still gets its teaser and can still be read; it
            # simply cannot refuse the model's next tool call.
            gated = _gate_arms(diag)
            ask = (
                f'(REQUIRED: call read_skill("{entry.name}") for the full '
                "playbook before other tools. If it plainly does not fit this "
                "task, retry your call instead — silently."
                if gated else
                f'(Call read_skill("{entry.name}") for the full playbook IF it '
                "fits this task. If it does not, ignore it and carry on."
            )
            block = (
                f"[skill: {entry.name} — TRUNCATED: first {len(head)} chars of a "
                "longer playbook]\n"
                f"{head}…\n"
                f"{ask} Never tell the user which skills you did or did not "
                "use.)"
            )
            if len(block) > remaining:
                continue  # not even the teaser fits — leave it to recall
            if gated:
                unread.append(entry.name)
        blocks.append(block)
        names.append(entry.name)
        items.append({"name": entry.name, "kind": entry.kind, **diag})
        remaining -= len(block) + 2  # +2 covers the join's blank line
    return Preload("\n\n".join(blocks), names, unread, items, mode)


def _gate_arms(diag: dict) -> bool:
    """May this entry REFUSE the model's next tool call, or only ask to be read?

    Named in the task (rail 4) is unambiguous — the owner or the model said the
    word, and a gate is what they asked for. Otherwise the similarity has to
    clear GATE_MIN_SIM on its own: a keyword hit lowers the INJECTION bar and
    must not be readable as confidence, because the whole failure it caused was
    a curated keyword ("apartment") standing in for relevance it did not have.

    In lexical mode there is no similarity to clear, so nothing but a name arms
    it. That is the conservative direction on purpose: an unarmed gate costs a
    playbook the model may skip, and an armed one costs the owner's task."""
    if diag.get("rail", 0) >= 4:
        return True
    sim = diag.get("sim")
    return sim is not None and sim >= GATE_MIN_SIM


def _snippet(text: str, words: list[str], width: int = RECALL_SNIPPET_CHARS) -> str | None:
    """One flattened line of context around the first query-word hit."""
    flat = " ".join(text.split())
    flat_cf = flat.casefold()
    pos = min((p for w in words if (p := flat_cf.find(w)) >= 0), default=-1)
    if pos < 0:
        return None
    start = max(0, pos - width // 3)
    end = min(len(flat), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return f"{prefix}{flat[start:end]}{suffix}"


def _entry_detail(name: str, entries: list[Entry]) -> str | None:
    for entry in entries:
        if entry.name == name:
            return f"{_block_header(entry)}\n{entry.body}"
    return None


def recall_text(
    cwd: str,
    lessons_path,
    query: str,
    name: str | None = None,
    sessions_search=None,
    session_detail=None,
    semantic=None,
) -> str:
    """Model-facing knowledge search (the recall tool), two-phase like
    search_sessions was: ranked matches with snippets, then one entry's full
    text by name. `sessions_search(query)` / `session_detail(name, query)`
    are injected by the agent so this module stays free of session-store
    wiring; either may be None when no session store exists. `semantic` is
    SemanticIndex.scores (or None), threaded into rank_entries (#178 P1-9).
    """
    entries = load_entries(cwd, lessons_path)
    if name:
        detail = _entry_detail(name, entries)
        if detail is not None:
            return detail[:RECALL_DETAIL_CHARS]
        if session_detail is not None and name.startswith("session-"):
            return session_detail(name, query)
        known = ", ".join(
            e.name for e in rank_entries(entries, query, semantic)[:RECALL_TOP]
        )
        return (
            f"ERROR: nothing named {name!r}. Use a name from a recall result"
            + (f" (close matches: {known})" if known else "")
            + "."
        )
    words = query.casefold().split()
    if not words:
        return "ERROR: recall needs a query (or a name from an earlier result)."
    ranked = rank_entries(entries, query, semantic)
    lines = []
    if ranked:
        lines.append(f"Saved knowledge matching {query!r} (best first):")
        for entry in ranked[:RECALL_TOP]:
            lines.append(f"- [{entry.kind}] {entry.name}: {entry.description}")
            snippet = _snippet(entry.body, words)
            if snippet and entry.body != entry.description:
                lines.append(f"    {snippet}")
        if len(ranked) > RECALL_TOP:
            lines.append(f"(…and {len(ranked) - RECALL_TOP} more, weaker matches)")
    else:
        lines.append(
            f"Nothing saved matches {query!r}. If you end up solving this in a "
            "way worth repeating, save it as a skill."
        )
    if sessions_search is not None:
        session_lines = sessions_search(query)
        if session_lines:
            lines.append("\nPast sessions that mention it:")
            lines.append(session_lines)
    lines.append(
        '\nCall recall again with name="<entry or session file name>" for the '
        "full text; read_skill(<name>) also works for skills."
    )
    return "\n".join(lines)


def _near_duplicate(identity: str, entries: list[Entry], semantic=None) -> tuple:
    """(entry-or-None, score, floor, mode) for the nearest existing memory when
    it is close enough to be the same fact (#178 P1-8). `semantic` is
    `SemanticIndex.scores` (or None): embedding similarity between identity
    lines when available; when it is absent or fails, a conservative difflib
    ratio on the identity lines is the floor — embeddings.py discipline, an
    upgrade never a dependency.

    Returns the SCORE and the FLOOR, not just the verdict (#192): this gate is
    the one that demonstrably worked in #190's evidence and it recorded
    nothing, which is why `DEDUP_MIN_SIM` is still documented as "provisional
    until measured on the live corpus" — nothing measured it. A verdict without
    its inputs cannot be recalibrated (contract §4)."""
    if not entries:
        return None, 0.0, DEDUP_MIN_SIM, "none"
    sims = semantic(identity, entries) if semantic is not None else None
    if sims is not None:
        best = max(entries, key=lambda e: sims.get(id(e), 0.0))
        score = sims.get(id(best), 0.0)
        hit = best if score >= DEDUP_MIN_SIM else None
        return hit, score, DEDUP_MIN_SIM, "semantic"
    from .embeddings import entry_text

    closest: Entry | None = None
    best_ratio = 0.0
    identity_cf = identity.casefold()
    for entry in entries:
        ratio = difflib.SequenceMatcher(
            None, identity_cf, entry_text(entry).casefold()
        ).ratio()
        if ratio > best_ratio:
            closest, best_ratio = entry, ratio
    hit = closest if best_ratio >= DEDUP_LEXICAL_RATIO else None
    return hit, best_ratio, DEDUP_LEXICAL_RATIO, "lexical"


def save_memory(fact: str, memory_dir, name: str = "", keywords: str = "", cwd: str = "",
                lessons_path=None, expires: str | None = None,
                pinned: bool | None = None, force: bool = False,
                semantic=None, disabled: bool | None = None,
                on_admission=None) -> str:
    """Create or update one structured memory entry. Constrained to writing a
    slug-named markdown file inside the memory dir — safe to auto-approve.

    `expires` ("YYYY-MM-DD") marks a fact with a known end date; past it the
    entry acts disabled (see entry_active). Write-side validation is strict —
    the model gets a correctable error, unlike the tolerant read side — and
    on an update, None keeps the file's existing expiry.

    `pinned` marks a standing rule (always indexed, #178 P1-7); None keeps an
    updated entry's existing flag, False explicitly unpins.

    `disabled` retires or revives an entry via `status: disabled` (#185) —
    the reversible half of the curation vocabulary (deletion is
    forget_memory, a separate deliberate act). None keeps an updated
    entry's existing status, False explicitly re-enables.

    A NEW slug whose identity line lands too close to an existing memory is
    refused with that entry's name (#178 P1-8) — near-duplicates compete for
    index and preflight slots, so the model must update or forget the
    existing entry instead, or pass `force` for a genuinely different fact.
    Updates to an existing slug are never gated.

    `on_admission` receives the gate's decision AND its inputs (score, floor,
    mode) so the caller can record them (#192, contract §3.7). A CALLBACK
    rather than a changed return type deliberately: the string return is what
    cli.py, curate.py and every test read, and this gate's evidence is worth
    recording without a ripple through all of them."""
    text = frontmatter_value(fact)
    if not text:
        return "ERROR: empty fact"
    slug = name.strip() or _slugify(text)
    if not NAME_RE.match(slug or ""):
        return f"ERROR: invalid memory name {slug!r}"
    expiry: date | None = None
    if expires is not None and expires.strip():
        try:
            expiry = date.fromisoformat(expires.strip())
        except ValueError:
            return f"ERROR: invalid expires date {expires!r} — use YYYY-MM-DD"
    existing = load_entries(cwd, lessons_path) if cwd else []
    for entry in existing:
        # Same-name file entries are the update path; path-less entries are
        # legacy lessons, never updatable, so an identical fact is always a
        # duplicate regardless of what its synthetic name slugged to.
        if entry.kind == "memory" and entry.description == text:
            if entry.path is None or entry.name != slug:
                return "(already remembered)"
    # Keyword hygiene (#183): keywords feed a retrieval rail, and the model
    # authors them — dedupe case-insensitively and cap the count so one
    # entry cannot carpet-bomb the trigger space with generic words. A bare
    # `.strip()` left interior newlines, which is how a keyword smuggled
    # `status: disabled` onto its own line and retired the entry (#209).
    seen_kw: set[str] = set()
    keyword_list = []
    for word in (frontmatter_value(w) for w in keywords.split(",")):
        if word and word.casefold() not in seen_kw:
            seen_kw.add(word.casefold())
            keyword_list.append(word)
    keyword_list = keyword_list[:KEYWORDS_MAX]
    directory = Path(memory_dir)
    path = directory / f"{slug}.md"
    if not force and not path.is_file():
        identity = f"{slug}: {text}"
        if keyword_list:
            identity += f" (keywords: {', '.join(keyword_list)})"
        similar, score, floor, mode = _near_duplicate(
            identity, [e for e in existing if e.kind == "memory"], semantic
        )
        if on_admission is not None:
            on_admission(
                {
                    "name": slug,
                    "verdict": "refused_duplicate" if similar else "admitted",
                    "tier": 1,
                    "evidence": {
                        "mode": mode,
                        "sim": round(score, 4),
                        "floor": floor,
                        "against": similar.name if similar else None,
                    },
                }
            )
        if similar is not None:
            return (
                f"NOT saved — a similar memory already exists — {similar.name}: "
                f"\"{similar.description}\". UPDATE it instead (remember with "
                f"name=\"{similar.name}\") or forget_memory(\"{similar.name}\") "
                "first; only if this is genuinely a different fact, retry with "
                "force=true."
            )
    body = ""
    try:
        if path.is_file():  # update: keep body detail + undeclared frontmatter
            prior = _parse(path, "memory")
            body = prior.body
            if expiry is None:
                expiry = prior.expires
            if pinned is None:
                pinned = prior.pinned
            if disabled is None:
                disabled = prior.status == "disabled"
        front = [f"name: {slug}", f"description: {text}"]
        if keyword_list:
            front.append(f"keywords: {', '.join(keyword_list)}")
        if pinned:
            front.append("pinned: yes")
        if disabled:
            front.append("status: disabled")
        if expiry is not None:
            front.append(f"expires: {expiry.isoformat()}")
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n" + "\n".join(front) + "\n---\n" + (body + "\n" if body else ""),
            encoding="utf-8",
        )
    except OSError as exc:
        return f"ERROR: could not save memory: {exc}"
    state = " [disabled]" if disabled else ""
    return f"remembered ({slug}){state}: {text}"


def forget_memory(name: str, cwd: str = "") -> str:
    """Permanently delete one memory entry by slug. Strictly confined to the
    memory dirs (project + global): slug-validated, and the resolved file must
    sit directly inside a memory dir, so it can never remove an arbitrary path.
    Legacy lessons.md lines are not files and cannot be forgotten this way."""
    slug = name.strip()
    if not NAME_RE.match(slug or ""):
        return f"ERROR: invalid memory name {slug!r}"
    for directory in memory_dirs(cwd):
        path = directory / f"{slug}.md"
        try:
            if not path.is_file():
                continue
            # Defense in depth: NAME_RE already forbids separators, but confirm
            # the file resolves to a direct child of the memory dir before unlink.
            if path.resolve().parent != directory.resolve():
                return f"ERROR: refusing to forget outside memory store: {slug!r}"
            path.unlink()
            return f"forgot ({slug})"
        except OSError as exc:
            return f"ERROR: could not forget memory: {exc}"
    return f"(no memory named {slug!r} to forget)"
