"""Semantic index for pre-flight knowledge retrieval (issue #43).

Lexical word-matching cannot tell whether a word shared by task and entry is
the entry's topic ("villa") or incidental prose ("photos") — that relation
between whole sentences is what embeddings measure. One local embedding
model scores task-vs-entry similarity no matter which chat backend runs the
task: chat models never see vectors, so retrieval behaves identically on
every --model, and the knowledge corpus never leaves the machine.

An entry embeds as a single line — "name: description (keywords: …)", never
the body: selection reads identity, bodies are injected after selection, and
vectors stay stable while playbooks grow. Vectors are cached in the state
dir keyed by sha256(model + text), so an edit re-embeds only the changed
entry, a model switch is just a cold cache, and stale keys drop at the next
save. At this corpus size scoring is a handful of pure-Python dot products —
no vector store. Any failure (ollama down, model not pulled) makes scores()
return None and the caller falls back to lexical matching; `error` carries
the reason for a one-time notice.
"""

import hashlib
import json
import math
import os
from pathlib import Path

DEFAULT_MODEL = "embeddinggemma"  # multilingual: tasks arrive in Polish, entries are English

# Retrieval-tuned embedding models expect task-type prefixes that Ollama does
# NOT add; without them similarity separation collapses (measured ~2x worse
# noise floor on embeddinggemma). Unknown models embed the raw text.
_PREFIXES: dict[str, tuple[str, str]] = {  # model -> (query prefix, document prefix)
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "mxbai-embed-large": ("Represent this sentence for searching relevant passages: ", ""),
}


def _prefixes(model: str) -> tuple[str, str]:
    return _PREFIXES.get(model.partition(":")[0], ("", ""))


def _ollama_embed(model: str, texts: list[str]) -> list[list[float]]:
    import ollama

    return [list(vec) for vec in ollama.embed(model=model, input=texts)["embeddings"]]


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def entry_text(entry) -> str:
    text = f"{entry.name}: {entry.description}"
    if entry.keywords:
        text += f" (keywords: {', '.join(entry.keywords)})"
    return text


class SemanticIndex:
    """Task-vs-entry similarity with a persistent per-entry vector cache."""

    def __init__(self, state_dir=None, model: str | None = None, embed=_ollama_embed):
        self.model = model or os.environ.get("AISH_EMBED_MODEL", DEFAULT_MODEL)
        self.cache_path = Path(state_dir) / "embeddings.json" if state_dir else None
        self._embed = embed
        self._cache: dict[str, list[float]] | None = None
        self.error: str | None = None

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\0{text}".encode()).hexdigest()

    def _load(self) -> dict[str, list[float]]:
        if self._cache is None:
            self._cache = {}
            if self.cache_path is not None:
                try:
                    stored = json.loads(self.cache_path.read_text(encoding="utf-8"))
                    self._cache = {k: v for k, v in stored.items() if isinstance(v, list)}
                except (OSError, ValueError):
                    pass
        return self._cache

    def _save(self, keep: set[str]) -> None:
        # Written only when the corpus changed; restricting to the live keys
        # garbage-collects vectors of edited entries and abandoned models.
        if self.cache_path is None or self._cache is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({k: self._cache[k] for k in keep}), encoding="utf-8"
            )
        except OSError:
            pass

    def scores(self, task: str, entries) -> dict[int, float] | None:
        """id(entry) -> cosine similarity in [-1, 1], or None when embedding
        failed — the caller then falls back to lexical matching."""
        cache = self._load()
        query_prefix, doc_prefix = _prefixes(self.model)
        # Cache keys hash the prefixed text, so a prefix change invalidates too.
        texts = {id(entry): doc_prefix + entry_text(entry) for entry in entries}
        missing = list(dict.fromkeys(t for t in texts.values() if self._key(t) not in cache))
        try:
            if missing:
                for text, vec in zip(missing, self._embed(self.model, missing), strict=False):
                    cache[self._key(text)] = _normalize(vec)
                self._save({self._key(t) for t in texts.values()})
            task_vec = _normalize(self._embed(self.model, [query_prefix + task])[0])
        except Exception as exc:  # ollama down, model not pulled, bad response
            self.error = str(exc) or exc.__class__.__name__
            return None
        self.error = None
        return {
            eid: sum(a * b for a, b in zip(task_vec, cache[self._key(text)], strict=False))
            for eid, text in texts.items()
        }
