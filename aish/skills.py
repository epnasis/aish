"""Knowledge store: skills (how-to playbooks) and memory (saved facts).

Both are markdown files with optional frontmatter, discovered live:

    ---
    name: sweepy
    description: Use when the user asks to sweep the inbox
    keywords: email, cleanup
    ---
    body ...

Skills live in ./.aish/skills/ (project, wins on name clash) or
~/.config/aish/skills/ (global); memory entries mirror that layout under
.aish/memory/ and ~/.config/aish/memory/. Legacy one-line lessons in
lessons.md are exposed as synthetic memory entries until migrated.

Progressive disclosure keeps the prompt small at any library size: a capped
index of name+description lines goes into the system prompt every task, full
bodies load on demand (read_skill), and the long tail is reachable through
the ranked `recall` search. The description line is what makes an entry
discoverable — for skills it states the trigger, for memory it IS the fact.
"""

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

GLOBAL_SKILLS_DIR = Path.home() / ".config" / "aish" / "skills"
GLOBAL_MEMORY_DIR = Path.home() / ".config" / "aish" / "memory"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The inline index is capped so the prompt stays small no matter how many
# entries accumulate; the cap admits every project skill first, then the most
# recently updated global ones. Output is byte-stable while files are
# unchanged (mtime order, no counts that vary per task) so API prompt caches
# survive across tasks.
INDEX_SKILLS_MAX = 30
INDEX_MEMORY_MAX = 15

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

_PUNCT = ".,;:!?()[]{}<>'\"`"
FUZZY_WORD_CUTOFF = 0.75  # single query word vs single entry word


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


def skill_dirs(cwd: str) -> list[Path]:
    return [Path(cwd) / ".aish" / "skills", GLOBAL_SKILLS_DIR]


def memory_dirs(cwd: str) -> list[Path]:
    return [Path(cwd) / ".aish" / "memory", GLOBAL_MEMORY_DIR]


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:48].rstrip("-")


def _build_words(*texts: str) -> frozenset:
    return frozenset(
        w.strip(_PUNCT) for text in texts for w in text.casefold().split()
    ) - {""}


def _parse(path: Path, kind: str = "skill") -> Entry:
    """Entry from a markdown file — name defaults to the filename,
    description to the first non-empty body line."""
    text = path.read_text(encoding="utf-8")
    name, description, keywords, body = path.stem, "", [], text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            _, front, body = parts
            for line in front.strip().splitlines():
                key, _, value = line.partition(":")
                key = key.strip()
                if key == "name" and value.strip():
                    name = value.strip()
                elif key == "description":
                    description = value.strip()
                elif key == "keywords":
                    keywords = [w.strip() for w in value.split(",") if w.strip()]
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
    )


# Parsed entries keyed by path; re-parse only when the file's mtime moved.
# At thousands of files a scan is then just glob + stat per call.
_CACHE: dict[Path, tuple[float, Entry]] = {}


def _dir_entries(directory: Path, kind: str) -> list[Entry]:
    entries: list[Entry] = []
    try:
        files = sorted(directory.glob("*.md"))
    except OSError:
        return entries
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
    """The full corpus in tie-break order: project-then-global skills,
    memory entries (newest first), then legacy lessons."""
    skills = _merged(skill_dirs(cwd), "skill")
    memory = _merged(memory_dirs(cwd), "memory")
    memory.sort(key=lambda e: e.mtime, reverse=True)
    return skills + memory + _lesson_entries(lessons_path)


def list_skills(dirs: list[Path]) -> list[tuple[str, str]]:
    """(name, description) pairs; earlier dirs win on duplicate names."""
    return sorted((e.name, e.description) for e in _merged(dirs, "skill"))


def knowledge_index(cwd: str, lessons_path=None) -> str:
    """The capped Skills + Memory sections of the system prompt, rebuilt
    every task so new entries appear without a restart. Empty string when
    nothing exists."""
    sections = []
    project_dir, global_dir = skill_dirs(cwd)
    project = _dir_entries(project_dir, "skill")
    names = {e.name for e in project}
    globals_ = [e for e in _dir_entries(global_dir, "skill") if e.name not in names]
    globals_.sort(key=lambda e: e.mtime, reverse=True)
    room = max(0, INDEX_SKILLS_MAX - len(project))
    skills = project + globals_[:room]
    if skills:
        hidden = len(globals_) - min(room, len(globals_))
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
    memory = _merged(memory_dirs(cwd), "memory")
    memory.sort(key=lambda e: e.mtime, reverse=True)
    lessons = _lesson_entries(lessons_path)
    memory += lessons
    shown = memory[:INDEX_MEMORY_MAX]
    if shown:
        lines = "\n".join(f"- {e.description}" for e in shown)
        note = (
            "\n(…and more saved memory — search it with recall(<topic>))"
            if len(memory) > INDEX_MEMORY_MAX
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
    return "\n\n".join(sections)


def load_skill(name: str, dirs: list[Path]) -> str:
    if not NAME_RE.match(name or ""):
        return f"ERROR: invalid skill name {name!r}"
    for entry in _merged(dirs, "skill"):
        if entry.name == name:
            return f"[skill: {name}]\n{entry.body}"
    available = ", ".join(n for n, _ in list_skills(dirs)) or "none"
    return f"ERROR: no skill named {name!r}. Available skills: {available}"


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


def rank_entries(entries: list[Entry], query: str) -> list[Entry]:
    return [entry for _, entry in score_entries(entries, query)]


def _reverse_score(entry: Entry, task_padded: str) -> int:
    """Does the entry's identity appear in the task text? The forward tiers
    in score_entries need the whole query to appear inside the entry — right
    for short recall queries, hopeless for a multi-sentence task. Name and
    keyword hits land on the same tier scale so max(forward, reverse) works.
    Descriptions are trigger-phrased prose — too noisy to reverse-match.

    `task_padded` is the space-padded, punctuation-stripped task from
    _pad_words, so matches respect word boundaries: skill "gh" must not
    fire on a task containing "night"."""
    name_cf = entry.name.casefold()
    if f" {name_cf} " in task_padded or f" {name_cf.replace('-', ' ')} " in task_padded:
        return 4
    for keyword in entry.keywords:
        keyword_cf = keyword.casefold()
        if len(keyword_cf) >= 3 and f" {keyword_cf} " in task_padded:
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


def preflight(
    cwd: str, lessons_path, task: str, char_budget: int = PREFLIGHT_TOTAL_CHARS
) -> Preload:
    """Deterministic pre-flight retrieval: the top skills/memories matching a
    task, rendered as blocks the agent injects directly — the model wakes up
    with the content in context instead of having to remember to recall it.
    A skill too large to inject gets a teaser and its name in `unread`, which
    arms the agent's read gate until read_skill loads the full body."""
    if not task.split():
        return Preload()
    task_padded = _pad_words(task)
    entries = load_entries(cwd, lessons_path)
    forward = {id(entry): score for score, entry in score_entries(entries, task)}
    picked = []
    for entry in entries:  # corpus order: project skills first, then newest
        score = max(forward.get(id(entry), 0), _reverse_score(entry, task_padded))
        if score >= PREFLIGHT_MIN_SCORE:
            picked.append((score, entry))
    picked.sort(key=lambda pair: -pair[0])  # stable: corpus order within a tier
    blocks: list[str] = []
    names: list[str] = []
    unread: list[str] = []
    remaining = char_budget
    for _, entry in picked[:PREFLIGHT_TOP]:
        if remaining < 200:  # no room left for anything useful
            break
        if entry.kind == "memory" or len(entry.body) <= PREFLIGHT_ENTRY_CHARS:
            header = f"[{entry.kind}: {entry.name}]\n"
            body = entry.body[: min(PREFLIGHT_ENTRY_CHARS, remaining - len(header) - 1)]
            cut = "…" if len(body) < len(entry.body) else ""
            block = f"{header}{body}{cut}"
        else:  # oversized skill: teaser now, full body via the gated read_skill
            head = entry.body[:PREFLIGHT_HEAD_CHARS]
            block = (
                f"[skill: {entry.name} — TRUNCATED: first {len(head)} chars of a "
                "longer playbook]\n"
                f"{head}…\n"
                f'(REQUIRED: call read_skill("{entry.name}") for the full playbook '
                "before other tools, or state explicitly why it does not apply.)"
            )
            if len(block) > remaining:
                continue  # not even the teaser fits — leave it to recall
            unread.append(entry.name)
        blocks.append(block)
        names.append(entry.name)
        remaining -= len(block) + 2  # +2 covers the join's blank line
    return Preload("\n\n".join(blocks), names, unread)


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
            return f"[{entry.kind}: {entry.name}]\n{entry.body}"
    return None


def recall_text(
    cwd: str,
    lessons_path,
    query: str,
    name: str | None = None,
    sessions_search=None,
    session_detail=None,
) -> str:
    """Model-facing knowledge search (the recall tool), two-phase like
    search_sessions was: ranked matches with snippets, then one entry's full
    text by name. `sessions_search(query)` / `session_detail(name, query)`
    are injected by the agent so this module stays free of session-store
    wiring; either may be None when no session store exists.
    """
    entries = load_entries(cwd, lessons_path)
    if name:
        detail = _entry_detail(name, entries)
        if detail is not None:
            return detail[:RECALL_DETAIL_CHARS]
        if session_detail is not None and name.startswith("session-"):
            return session_detail(name, query)
        known = ", ".join(e.name for e in rank_entries(entries, query)[:RECALL_TOP])
        return (
            f"ERROR: nothing named {name!r}. Use a name from a recall result"
            + (f" (close matches: {known})" if known else "")
            + "."
        )
    words = query.casefold().split()
    if not words:
        return "ERROR: recall needs a query (or a name from an earlier result)."
    ranked = rank_entries(entries, query)
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


def save_memory(fact: str, memory_dir, name: str = "", keywords: str = "", cwd: str = "",
                lessons_path=None) -> str:
    """Create or update one structured memory entry. Constrained to writing a
    slug-named markdown file inside the memory dir — safe to auto-approve."""
    text = " ".join(fact.split()).strip()
    if not text:
        return "ERROR: empty fact"
    slug = name.strip() or _slugify(text)
    if not NAME_RE.match(slug or ""):
        return f"ERROR: invalid memory name {slug!r}"
    existing = load_entries(cwd, lessons_path) if cwd else []
    for entry in existing:
        # Same-name file entries are the update path; path-less entries are
        # legacy lessons, never updatable, so an identical fact is always a
        # duplicate regardless of what its synthetic name slugged to.
        if entry.kind == "memory" and entry.description == text:
            if entry.path is None or entry.name != slug:
                return "(already remembered)"
    keyword_list = [w.strip() for w in keywords.split(",") if w.strip()]
    directory = Path(memory_dir)
    path = directory / f"{slug}.md"
    front = [f"name: {slug}", f"description: {text}"]
    if keyword_list:
        front.append(f"keywords: {', '.join(keyword_list)}")
    body = ""
    try:
        if path.is_file():  # update: keep any body detail below the fact line
            body = _parse(path, "memory").body
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n" + "\n".join(front) + "\n---\n" + (body + "\n" if body else ""),
            encoding="utf-8",
        )
    except OSError as exc:
        return f"ERROR: could not save memory: {exc}"
    return f"remembered ({slug}): {text}"
