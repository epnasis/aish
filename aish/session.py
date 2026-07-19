"""Append-only JSONL session logs: the conversation (for --resume) and an
audit trail of every command decision, in one file per session."""

import datetime
import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

TITLE_MAX = 60
FUZZY_THRESHOLD = 0.55  # whole query vs whole title
FUZZY_WORD_CUTOFF = 0.75  # single query word vs single session word
_PUNCT = ".,;:!?()[]{}<>'\"`"
_BANG_RE = re.compile(r"^\[I ran `(.+?)` myself")

# Model-facing search (the search_sessions tool): bounded so one call can
# never flood a small context window.
SEARCH_TOP = 5
SNIPPET_CHARS = 200
SNIPPETS_PER_SESSION = 3
DETAIL_MESSAGE_CHARS = 700
DETAIL_MAX_CHARS = 6000
DETAIL_TAIL_MESSAGES = 20
_SESSION_NAME_RE = re.compile(r"^session-[0-9-]+\.jsonl$")


@dataclass
class SessionInfo:
    path: Path
    when: str
    count: int
    title: str
    model: str = ""  # last model used; "" for sessions logged before model records


@dataclass
class SessionEntry:
    """A session preloaded for searching: display info plus casefolded
    title/contents/model and a word vocabulary, so ranking never re-reads
    the file."""

    info: SessionInfo
    title_cf: str
    content_cf: str
    words: frozenset
    model_cf: str = ""


class SessionLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def close(self) -> None:
        """Release the append handle (the file itself always persists)."""
        self._fh.close()

    @classmethod
    def new(cls, state_dir: Path) -> "SessionLog":
        # Microseconds: /new within the same second must not reuse the file.
        name = datetime.datetime.now().strftime("session-%Y%m%d-%H%M%S-%f.jsonl")
        return cls(state_dir / name)

    @staticmethod
    def latest(state_dir: Path) -> Path | None:
        files = sorted(state_dir.glob("session-*.jsonl"))
        return files[-1] if files else None

    @staticmethod
    def _parse(path: Path) -> tuple[list[dict], str]:
        """One pass over the file: conversation messages (no audit records, no
        stale system prompt — a fresh one is built on resume) plus the last
        recorded model ("" for sessions that predate model records)."""
        messages: list[dict] = []
        model = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            kind = record.get("kind")
            if kind == "model":
                model = record.get("model") or model
            elif kind == "message" and record.get("role") != "system":
                keys = ("role", "content", "tool_name", "images", "documents")
                messages.append({k: v for k, v in record.items() if k in keys})
        return messages, model

    @staticmethod
    def load_messages(path: Path) -> list[dict]:
        return SessionLog._parse(path)[0]

    @staticmethod
    def _derive_title(messages: list[dict]) -> str:
        """Untruncated title: the first user message — cheap, deterministic,
        and it almost always names the task."""
        for message in messages:
            if message.get("role") == "user":
                content = " ".join((message.get("content") or "").split())
                bang = _BANG_RE.match(content)
                return f"! {bang.group(1)}" if bang else content
        return "(no user input)"

    @staticmethod
    def _started_at(path: Path) -> datetime.datetime:
        try:  # session-YYYYmmdd-HHMMSS[-ffffff].jsonl
            _, day, clock = path.stem.split("-")[:3]
            return datetime.datetime.strptime(f"{day}-{clock}", "%Y%m%d-%H%M%S")
        except ValueError:
            return datetime.datetime.fromtimestamp(path.stat().st_mtime)

    @staticmethod
    def _info_from(path: Path, messages: list[dict], model: str = "") -> SessionInfo:
        title = SessionLog._derive_title(messages)
        if len(title) > TITLE_MAX:
            title = title[: TITLE_MAX - 1] + "…"
        when = SessionLog._started_at(path).strftime("%Y-%m-%d %H:%M")
        return SessionInfo(path=path, when=when, count=len(messages), title=title, model=model)

    @staticmethod
    def info(path: Path) -> SessionInfo | None:
        """Summary line for a session picker; None for empty sessions."""
        messages, model = SessionLog._parse(path)
        if not messages:
            return None
        return SessionLog._info_from(path, messages, model)

    @staticmethod
    def list_sessions(state_dir: Path, exclude: set | None = None) -> list[SessionInfo]:
        """Non-empty sessions, newest first, minus excluded paths."""
        exclude = exclude or set()
        infos = []
        for path in sorted(state_dir.glob("session-*.jsonl"), reverse=True):
            if path in exclude:
                continue
            info = SessionLog.info(path)
            if info:
                infos.append(info)
        return infos

    @staticmethod
    def load_entries(state_dir: Path, exclude: set | None = None) -> list["SessionEntry"]:
        """Searchable sessions, newest first, read from disk once — so a live
        picker can re-rank on every keystroke without touching files."""
        exclude = exclude or set()
        entries = []
        for path in sorted(state_dir.glob("session-*.jsonl"), reverse=True):
            if path in exclude:
                continue
            messages, model = SessionLog._parse(path)
            if not messages:
                continue
            content_cf = " ".join(
                " ".join((m.get("content") or "").split()) for m in messages
            ).casefold()
            model_cf = model.casefold()
            # Model tokens ("gemini", "2.5", "pro") join the fuzzy vocabulary
            # so a typo like "gemni" still filters by model.
            model_words = frozenset(re.split(r"[^a-z0-9.]+", model_cf)) - {""}
            entries.append(
                SessionEntry(
                    info=SessionLog._info_from(path, messages, model),
                    title_cf=SessionLog._derive_title(messages).casefold(),
                    content_cf=content_cf,
                    words=(
                        frozenset(w.strip(_PUNCT) for w in content_cf.split()) - {""}
                    ) | model_words,
                    model_cf=model_cf,
                )
            )
        return entries

    @staticmethod
    def rank(entries: list["SessionEntry"], query: str) -> list[SessionInfo]:
        """Deterministic ranking over titles, model names, and full message
        contents — no LLM. Tiers: exact title, phrase in title or model,
        phrase in contents, all words in contents/model, then fuzzy
        (difflib): every query word close to some session word, or the whole
        query close to the title. Ties keep newest-first order; an empty
        query keeps everything, newest first."""
        query_cf = " ".join(query.split()).casefold()
        words = query_cf.split()
        if not words:
            return [entry.info for entry in entries]
        ranked = []
        for entry in entries:
            if entry.title_cf == query_cf:
                score = 5
            elif query_cf in entry.title_cf or query_cf in entry.model_cf:
                score = 4
            elif query_cf in entry.content_cf:
                score = 3
            elif all(word in entry.content_cf or word in entry.model_cf for word in words):
                score = 2
            elif all(
                difflib.get_close_matches(word, entry.words, n=1, cutoff=FUZZY_WORD_CUTOFF)
                for word in words
            ) or (
                difflib.SequenceMatcher(None, query_cf, entry.title_cf).ratio()
                >= FUZZY_THRESHOLD
            ):
                score = 1
            else:
                continue
            ranked.append((score, entry.info))
        ranked.sort(key=lambda pair: -pair[0])  # stable: newest first within a tier
        return [info for _, info in ranked]

    @staticmethod
    def search_sessions(
        state_dir: Path, query: str, exclude: set | None = None
    ) -> list[SessionInfo]:
        """One-shot ranked search; empty queries match nothing."""
        if not query.split():
            return []
        return SessionLog.rank(SessionLog.load_entries(state_dir, exclude), query)

    @staticmethod
    def _snippet(content: str, words: list[str], width: int = SNIPPET_CHARS) -> str | None:
        """One flattened line of context around the first query-word hit."""
        flat = " ".join(content.split())
        flat_cf = flat.casefold()
        pos = min((p for w in words if (p := flat_cf.find(w)) >= 0), default=-1)
        if pos < 0:
            return None
        start = max(0, pos - width // 3)
        end = min(len(flat), start + width)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(flat) else ""
        return f"{prefix}{flat[start:end]}{suffix}"

    @staticmethod
    def search_excerpts(
        state_dir: Path, query: str, session: str | None = None, exclude: set | None = None
    ) -> str:
        """Model-facing session search (the search_sessions tool).

        Without `session`: ranked sessions with excerpt lines around the
        matches — enough to pick the right one. With `session`: that file's
        matching messages (or its tail when the query is empty), trimmed and
        capped so the result always fits a small context window.
        """
        words = query.casefold().split()
        if session is not None:
            return SessionLog._session_detail(state_dir, session, query, words)
        if not words:
            return (
                "ERROR: search_sessions needs a query (or a session file name "
                "from an earlier result)."
            )
        infos = SessionLog.search_sessions(state_dir, query, exclude=exclude)
        if not infos:
            return f"No past session matches {query!r}."
        lines = [f"{len(infos)} past session(s) match {query!r} (best matches first):"]
        for info in infos[:SEARCH_TOP]:
            model = f" · {info.model}" if info.model else ""
            lines.append(f"\n== {info.path.name} · {info.when} · {info.count} msgs{model}")
            lines.append(f"   title: {info.title}")
            shown = 0
            for message in SessionLog.load_messages(info.path):
                snippet = SessionLog._snippet(message.get("content") or "", words)
                if snippet is None:
                    continue
                lines.append(f"   [{message.get('role', '?')}] {snippet}")
                shown += 1
                if shown >= SNIPPETS_PER_SESSION:
                    break
        if len(infos) > SEARCH_TOP:
            lines.append(f"\n(…and {len(infos) - SEARCH_TOP} more, weaker matches)")
        lines.append(
            '\nCall search_sessions again with session="<file name>" for the full '
            "matching messages from one session."
        )
        return "\n".join(lines)

    @staticmethod
    def _session_detail(state_dir: Path, session: str, query: str, words: list[str]) -> str:
        if not _SESSION_NAME_RE.match(session):
            return (
                f"ERROR: {session!r} is not a session file name — use a name "
                "returned by search_sessions, like 'session-20260718-213000-000000.jsonl'."
            )
        path = state_dir / session
        if not path.is_file():
            return f"ERROR: no such session: {session}. Search first to find valid names."
        messages = SessionLog.load_messages(path)
        matching = [
            m for m in messages
            if any(w in (m.get("content") or "").casefold() for w in words)
        ]
        if words and matching:
            header = f"Messages matching {query!r} in {session}:"
            picked = matching
        else:
            note = f"no message matches {query!r}; " if words else ""
            header = f"{session}: {note}showing the most recent messages:"
            picked = messages[-DETAIL_TAIL_MESSAGES:]
        lines = [header]
        used = len(header)
        for i, message in enumerate(picked):
            content = (message.get("content") or "").strip()
            snippet = SessionLog._snippet(content, words, width=DETAIL_MESSAGE_CHARS)
            body = snippet if words and snippet else content[:DETAIL_MESSAGE_CHARS]
            entry = f"\n[{message.get('role', '?')}] {body}"
            if used + len(entry) > DETAIL_MAX_CHARS:
                lines.append(
                    f"\n[… {len(picked) - i} more messages omitted — refine the query]"
                )
                break
            lines.append(entry)
            used += len(entry)
        return "\n".join(lines)

    def _record(self, kind: str, **fields) -> None:
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            **fields,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def message(self, message: dict) -> None:
        self._record("message", **message)

    def model(self, spec: str) -> None:
        """Record the model in use; appended at session start and on every
        /model switch, so the last record is the session's current model."""
        self._record("model", model=spec)

    def command(self, command: str, decision: str) -> None:
        self._record("command", command=command, decision=decision)
