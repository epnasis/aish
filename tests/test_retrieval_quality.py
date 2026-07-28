"""Recall@k regression harness for knowledge retrieval (#178 review).

Retrieval quality was unmeasured: SEMANTIC_MIN_SIM was tuned on a small
corpus, and nothing would notice a change that silently broke preflight
selection or the semantic recall path. This harness makes both observable
in pytest with NO network and NO ollama.

The semantic layer is exercised deterministically through SemanticIndex's
`embed=` seam (the same injection point test_embeddings.py uses): a synthetic
embedder maps texts onto shared concept axes — a word table that includes the
Polish inflections a real multilingual model resolves — so intended
neighbours are actually near, plus a small hash-derived noise tail so no two
texts embed identically. This certifies the MERGE/rank plumbing and gates
recall@k; it deliberately certifies nothing about embeddinggemma itself.

The corpus and task phrasings mirror the live corpus's style, including the
motivating cross-language case (Polish task, English entry) that pure
lexical matching cannot solve. Lexical-only fallback keeps a lower, asserted
floor: the deterministic rail (name/keyword hits, incl. author-provided
bilingual keywords) must keep working when embeddings are unavailable.
"""

import hashlib

from aish import skills as skills_module
from aish.embeddings import SemanticIndex
from aish.skills import load_entries, preflight, rank_entries

# One axis per topic; a text lands on an axis when any of its words appears.
# Polish variants stand in for what a multilingual embedding model resolves.
CONCEPTS: dict[str, set[str]] = {
    "lodging": {"hotel", "hotels", "villa", "villas", "accommodation", "trippy",
                "booking", "bookings", "nocleg", "noclegi"},
    "github": {"github", "issue", "issues", "repo", "zgłoś", "zgłoszenie"},
    "deploy": {"deploy", "deploying", "deployment", "release", "production",
               "ship", "wdróż", "wdrożenie"},
    "notify": {"pushover", "notification", "notifications", "powiadomienie",
               "powiadomienia", "telefon", "phone"},
    "python": {"python", "uv", "pip", "dependency", "dependencies", "pakiet"},
    "backup": {"backup", "backups", "commit", "commits", "kopia"},
    "reminders": {"reminder", "reminders", "rem", "przypomnienie", "przypomnij"},
    "console": {"console", "shell", "tmux", "terminal", "konsola"},
    "weather": {"weather", "forecast", "wttr.in", "pogoda", "pogodę"},
    "invoices": {"invoice", "invoices", "accounting", "faktura", "faktury"},
}
_AXES = sorted(CONCEPTS)
_STRIP = ".,;:!?()[]{}<>'\"`"


def synth_embed(model: str, texts: list[str]) -> list[list[float]]:
    """Deterministic pseudo-embeddings: 1.0 on each concept axis the text
    touches + a tiny sha256-derived tail (unique, well below SEMANTIC_MIN_SIM
    so unrelated texts never cross the retrieval floor by noise)."""
    out = []
    for text in texts:
        words = {w.strip(_STRIP) for w in text.casefold().split()}
        vec = [1.0 if words & CONCEPTS[axis] else 0.0 for axis in _AXES]
        digest = hashlib.sha256(text.encode()).digest()
        vec += [(b / 255 - 0.5) * 0.1 for b in digest[:8]]
        out.append(vec)
    return out


# (name, kind, description, keywords) — realistic entry identities. Keywords
# are English except weather-brief's, whose author added the Polish synonym:
# that is the documented remember-tool practice the lexical rail rewards.
CORPUS = [
    ("hotels-use-trippy", "memory",
     "For hotel, villa, or accommodation searches always run trippy", "hotels, bookings"),
    ("gh-issue", "skill",
     "Use when opening a GitHub issue on any repo", "github, issues"),
    ("deploy-web", "skill",
     "How to deploy the web app to production", "deploy, release"),
    ("pushover-setup", "memory",
     "Pushover notification credentials live in the Keychain", "notifications"),
    ("uv-python", "memory",
     "Python dependencies are managed with uv, not pip", "python, uv, dependencies"),
    ("backup-knowledge", "memory",
     "The knowledge dir auto-commits to a private remote hourly", "backup"),
    ("reminders-rem", "memory",
     "Use the rem CLI for Apple Reminders", "reminders"),
    ("tmux-console", "memory",
     "The web console shell survives restarts via tmux", "console, tmux"),
    ("weather-brief", "memory",
     "Morning brief includes the weather forecast from wttr.in", "weather, pogoda"),
    ("invoices-folder", "memory",
     "Invoices are filed under Documents/Invoices by month", "invoices, accounting"),
]

# (task phrasing for preflight, recall query, expected entry). Polish cases
# model the live usage: tasks arrive in Polish, entries are English.
CASES = [
    ("find me a villa in bali with a private pool", "villa bali", "hotels-use-trippy"),
    ("znajdź nocleg w krakowie na przyszły weekend", "nocleg w krakowie",
     "hotels-use-trippy"),
    ("open a github issue about the broken pager", "github issue", "gh-issue"),
    ("zgłoś problem na github w repo aish", "zgłoszenie na github", "gh-issue"),
    ("ship the new version to production", "deploy to production", "deploy-web"),
    ("wyślij powiadomienie na telefon", "powiadomienie pushover", "pushover-setup"),
    ("add a new python dependency to the project", "python dependency uv", "uv-python"),
    ("ustaw przypomnienie na jutro rano", "przypomnienie", "reminders-rem"),
    ("jaka będzie jutro pogoda w warszawie", "pogoda na jutro", "weather-brief"),
    ("gdzie są faktury z czerwca", "faktury", "invoices-folder"),
]
CROSS_LANGUAGE = CASES[1]  # zero lexical overlap: the motivating scenario

K = 3
# Floors, not exact pins: an improvement may raise them, a regression fails.
LEXICAL_PREFLIGHT_FLOOR = 0.6  # desc-word + keyword rails carry English cases
LEXICAL_RECALL_FLOOR = 0.3  # forward tiers carry only well-phrased queries


def build_corpus():
    for name, kind, description, keywords in CORPUS:
        directory = (
            skills_module.GLOBAL_SKILLS_DIR if kind == "skill"
            else skills_module.GLOBAL_MEMORY_DIR
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n"
            f"keywords: {keywords}\n---\nbody of {name}\n",
            encoding="utf-8",
        )


def semantic_index(tmp_path):
    return SemanticIndex(tmp_path / "state", model="synthetic", embed=synth_embed)


def preflight_recall_at_k(cwd, scores=None):
    hits = [
        (task, expected)
        for task, _query, expected in CASES
        if expected in preflight(cwd, None, task, semantic=scores).names[:K]
    ]
    return len(hits) / len(CASES), hits


def recall_path_recall_at_k(cwd, scores=None):
    entries = load_entries(cwd)
    hits = [
        (query, expected)
        for _task, query, expected in CASES
        if expected in [e.name for e in rank_entries(entries, query, scores)[:K]]
    ]
    return len(hits) / len(CASES), hits


class TestSemanticRecallAtK:
    def test_preflight_semantic_recall_at_3_is_full(self, tmp_path):
        build_corpus()
        idx = semantic_index(tmp_path)
        score, hits = preflight_recall_at_k(str(tmp_path), idx.scores)
        missed = [c[:1] + c[2:] for c in CASES if (c[0], c[2]) not in hits]
        assert score == 1.0, f"preflight recall@{K} dropped to {score}; missed {missed}"

    def test_recall_path_semantic_recall_at_3_is_full(self, tmp_path):
        build_corpus()
        idx = semantic_index(tmp_path)
        score, hits = recall_path_recall_at_k(str(tmp_path), idx.scores)
        missed = [c[1:] for c in CASES if (c[1], c[2]) not in hits]
        assert score == 1.0, f"recall recall@{K} dropped to {score}; missed {missed}"

    def test_cross_language_case_is_carried_by_semantic(self, tmp_path):
        # The motivating scenario: lexical misses it on BOTH paths, the
        # semantic layer finds it on BOTH paths.
        build_corpus()
        task, query, expected = CROSS_LANGUAGE
        cwd = str(tmp_path)
        assert expected not in preflight(cwd, None, task).names
        entries = load_entries(cwd)
        assert expected not in [e.name for e in rank_entries(entries, query)]
        idx = semantic_index(tmp_path)
        assert expected in preflight(cwd, None, task, semantic=idx.scores).names[:K]
        assert expected in [e.name for e in rank_entries(entries, query, idx.scores)[:K]]


class TestLexicalFloor:
    """Embeddings are an upgrade, never a dependency: with no semantic layer
    the deterministic rails must keep a measured share of the cases."""

    def test_preflight_lexical_floor(self, tmp_path):
        build_corpus()
        score, _ = preflight_recall_at_k(str(tmp_path))
        assert score >= LEXICAL_PREFLIGHT_FLOOR, f"lexical preflight floor broke: {score}"

    def test_recall_path_lexical_floor(self, tmp_path):
        build_corpus()
        score, _ = recall_path_recall_at_k(str(tmp_path))
        assert score >= LEXICAL_RECALL_FLOOR, f"lexical recall floor broke: {score}"

    def test_bilingual_keyword_rail_works_without_embeddings(self, tmp_path):
        # An author-provided Polish keyword must carry a Polish task even
        # with no semantic layer — the guarantee rail the schema asks for.
        build_corpus()
        preload = preflight(str(tmp_path), None, "jaka będzie jutro pogoda w warszawie")
        assert preload.names[:1] == ["weather-brief"]

    def test_semantic_failure_degrades_to_the_lexical_floor(self, tmp_path):
        build_corpus()
        broken = lambda task, entries: None  # noqa: E731 — ollama down
        score, _ = preflight_recall_at_k(str(tmp_path), broken)
        assert score >= LEXICAL_PREFLIGHT_FLOOR


class TestHarnessSeam:
    def test_vectors_ride_the_real_cache(self, tmp_path):
        # The harness goes through SemanticIndex's persistent cache, not
        # around it: corpus vectors are written once and reused.
        build_corpus()
        entries = load_entries(str(tmp_path))
        calls: list[list[str]] = []

        def counting_embed(model, texts):
            calls.append(list(texts))
            return synth_embed(model, texts)

        state = tmp_path / "state"
        SemanticIndex(state, model="synthetic", embed=counting_embed).scores(
            "find a villa", entries
        )
        assert (state / "embeddings.json").is_file()
        before = len(calls)
        SemanticIndex(state, model="synthetic", embed=counting_embed).scores(
            "another task", entries
        )
        assert len(calls) == before + 1  # only the new query embedded
        assert calls[-1] == ["another task"]
