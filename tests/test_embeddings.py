"""Semantic pre-flight selection (issue #43): SemanticIndex cache/scoring and
its integration with skills.preflight. No model, no network — a fake embed
callable returns handcrafted vectors, mirroring the FakeChat pattern."""

import json

from aish import skills as skills_module
from aish.embeddings import SemanticIndex, entry_text
from aish.skills import Entry, preflight


def entry(name, description, keywords=(), kind="memory", body=""):
    return Entry(
        name=name,
        description=description,
        keywords=list(keywords),
        body=body,
        kind=kind,
    )


def make_embed(table, calls):
    """table maps a substring of the embedded text -> vector."""

    def embed(model, texts):
        calls.append(list(texts))
        out = []
        for text in texts:
            for probe, vec in table.items():
                if probe in text:
                    out.append(list(vec))
                    break
            else:
                raise AssertionError(f"no fake vector for {text!r}")
        return out

    return embed


class TestSemanticIndex:
    def test_cosine_scores(self, tmp_path):
        hotels = entry("hotels", "accommodation searches")
        charts = entry("charts", "plotting data")
        table = {"hotels": [1.0, 0.0], "charts": [0.0, 1.0], "villa": [2.0, 0.0]}
        idx = SemanticIndex(tmp_path, model="m", embed=make_embed(table, []))
        scores = idx.scores("find a villa", [hotels, charts])
        assert scores is not None
        assert scores[id(hotels)] == 1.0  # parallel, normalized
        assert scores[id(charts)] == 0.0  # orthogonal
        assert idx.error is None

    def test_cache_hits_skip_corpus_reembedding(self, tmp_path):
        hotels = entry("hotels", "accommodation searches")
        calls: list = []
        table = {"hotels": [1.0, 0.0], "task": [1.0, 0.0]}
        idx = SemanticIndex(tmp_path, model="m", embed=make_embed(table, calls))
        idx.scores("task one", [hotels])
        assert len(calls) == 2  # corpus batch + task
        fresh = SemanticIndex(tmp_path, model="m", embed=make_embed(table, calls))
        fresh.scores("task two", [hotels])
        assert len(calls) == 3  # only the task; corpus came from the cache file

    def test_edit_reembeds_and_stale_keys_dropped(self, tmp_path):
        calls: list = []
        table = {"old wording": [1.0, 0.0], "new wording": [0.0, 1.0], "task": [1.0, 0.0]}
        idx = SemanticIndex(tmp_path, model="m", embed=make_embed(table, calls))
        idx.scores("task", [entry("fact", "old wording")])
        idx2 = SemanticIndex(tmp_path, model="m", embed=make_embed(table, calls))
        idx2.scores("task", [entry("fact", "new wording")])
        assert calls[2] == [entry_text(entry("fact", "new wording"))]  # re-embed edit only
        stored = json.loads((tmp_path / "embeddings.json").read_text())
        assert len(stored) == 1  # the old vector was garbage-collected

    def test_model_switch_is_a_cold_cache(self, tmp_path):
        calls: list = []
        table = {"": [1.0]}  # matches everything
        SemanticIndex(tmp_path, model="a", embed=make_embed(table, calls)).scores(
            "t", [entry("x", "d")]
        )
        SemanticIndex(tmp_path, model="b", embed=make_embed(table, calls)).scores(
            "t", [entry("x", "d")]
        )
        assert len(calls) == 4  # corpus re-embedded under the new model key

    def test_failure_returns_none_and_sets_error(self, tmp_path):
        def broken(model, texts):
            raise ConnectionError("ollama down")

        idx = SemanticIndex(tmp_path, model="m", embed=broken)
        assert idx.scores("task", [entry("x", "d")]) is None
        assert "ollama down" in (idx.error or "")

    def test_no_state_dir_still_scores(self):
        table = {"": [1.0]}
        idx = SemanticIndex(None, model="m", embed=make_embed(table, []))
        assert idx.scores("task", [entry("x", "d")]) is not None


class TestPreflightSemantic:
    def _corpus(self, tmp_path, monkeypatch, files):
        monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", tmp_path / "gs")
        monkeypatch.setattr(skills_module, "GLOBAL_MEMORY_DIR", tmp_path / "gm")
        d = tmp_path / ".aish" / "memory"
        d.mkdir(parents=True)
        for name, description, keywords in files:
            kw = f"keywords: {keywords}\n" if keywords else ""
            (d / f"{name}.md").write_text(
                f"---\nname: {name}\ndescription: {description}\n{kw}---\nbody of {name}\n"
            )

    def test_similarity_selects_and_ranks(self, tmp_path, monkeypatch):
        self._corpus(
            tmp_path,
            monkeypatch,
            [("hotels", "accommodation searches", ""), ("charts", "plotting data", "")],
        )
        sims = lambda task, entries: {  # noqa: E731
            id(e): {"hotels": 0.9, "charts": 0.1}[e.name] for e in entries
        }
        preload = preflight(str(tmp_path), None, "find a villa in bali", semantic=sims)
        assert preload.names == ["hotels"]  # charts below SEMANTIC_MIN_SIM

    def test_exact_keyword_rail_beats_low_similarity(self, tmp_path, monkeypatch):
        self._corpus(
            tmp_path,
            monkeypatch,
            [("prices", "never trust training data", "price"), ("other", "stuff", "")],
        )
        sims = lambda task, entries: dict.fromkeys(map(id, entries), 0.0)  # noqa: E731
        preload = preflight(str(tmp_path), None, "what is the price of X", semantic=sims)
        assert preload.names == ["prices"]

    def test_semantic_failure_falls_back_to_lexical(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, monkeypatch, [("hotels", "hotel and villa searches", "")])
        preload = preflight(
            str(tmp_path), None, "find a villa in bali", semantic=lambda t, e: None
        )
        assert preload.names == ["hotels"]  # description word matching still fires
