"""Append-only JSONL session logs: the conversation (for --resume) and an
audit trail of every command decision, in one file per session."""

import datetime
import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

TITLE_MAX = 60
SNIPPET_MAX = 90  # preview line under the title in the web sessions drawer
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
RECALL_SESSIONS_TOP = 3  # sessions shown in the recall tool's fallback section
_SESSION_NAME_RE = re.compile(r"^session-[0-9-]+\.jsonl$")
# run_command appends this exit marker to its returned output; the live stream
# never carries it (the code comes via command_end), so reconstruction strips
# it before replaying the output into the terminal block.
_EXIT_MARKER_RE = re.compile(r"\n?\[exit code: -?\d+\]\s*$")


@dataclass
class SessionInfo:
    path: Path
    when: str
    count: int
    title: str
    model: str = ""  # last model used; "" for sessions logged before model records
    snippet: str = ""  # last visible message — the drawer's preview line
    mtime: float = 0.0  # last interaction (epoch seconds), the recency sort key


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
        self._fh: TextIO | None = None
        self._pending_model: str | None = None

    def close(self) -> None:
        """Release the append handle; a session that never recorded anything
        has no handle and leaves no file."""
        if self._fh is not None:
            self._fh.close()

    @classmethod
    def new(cls, state_dir: Path) -> "SessionLog":
        # Microseconds: /new within the same second must not reuse the file.
        name = datetime.datetime.now().strftime("session-%Y%m%d-%H%M%S-%f.jsonl")
        return cls(state_dir / name)

    @staticmethod
    def latest(state_dir: Path) -> Path | None:
        """Most recently interacted-with session (not most recently created)."""
        files = SessionLog._by_recency(state_dir)
        return files[0] if files else None

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
    def reconstruct_events(path: Path) -> list[dict] | None:
        """Rebuild the EXACT transcript event stream a rich client replays, so
        a cold-loaded session feeds the frontend the same events a live one
        does — same data, same code, same rendered output. Groups the log by
        task (a user message opens a turn; the turn's final assistant text is
        its `done` answer), and reassembles each run_command into its full
        `command_start → stream → command_end → tool` sequence so the terminal
        block reconstructs identically instead of falling back to a plain box.

        Returns None when the log predates trace records (no `trace` kind), so
        the caller can fall back to a flat conversation history."""
        events: list[dict] = []
        steps: list[dict] = []
        answer = ""
        open_turn = False
        has_trace = False
        pending_start: dict | None = None
        pending_end: dict | None = None

        def flush() -> None:
            nonlocal steps, answer, open_turn
            if not open_turn:
                return
            events.extend(steps)
            events.append({"type": "done", "result": answer})
            steps = []
            answer = ""
            open_turn = False

        def emit_command(step: dict) -> None:
            """Splice a run_command's terminal-block framing around its `tool`
            step, so the reconstructed stream matches the live one exactly. The
            command's output rides on the step (not duplicated in the framing
            records); it is replayed as one `stream` chunk — the final panel is
            identical whether the live output arrived in one piece or many."""
            nonlocal pending_start, pending_end
            start = pending_start
            if start is None:  # legacy log (framing not yet persisted): synthesize
                start = {"cwd": "", "command": step.get("command", "")}
            steps.append({"type": "command_start", **start})
            # The tool step's output carries run_command's trailing
            # "[exit code: N]" marker; the live stream never does (the code
            # arrives via command_end), so strip it for a byte-identical panel.
            output = _EXIT_MARKER_RE.sub("", step.get("output") or "")
            if output:
                steps.append({"type": "stream", "text": output})
            if pending_end is not None:
                steps.append({"type": "command_end", **pending_end})
            else:  # legacy: best-effort exit from the step's ok flag
                steps.append({"type": "command_end", "status": "exit",
                              "exit_code": 0 if step.get("ok") else 1})
            steps.append({"type": "step", **step})
            pending_start = pending_end = None

        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            kind = record.get("kind")
            if kind == "cmd_start":
                pending_start = {k: v for k, v in record.items() if k not in ("kind", "ts")}
            elif kind == "cmd_end":
                pending_end = {k: v for k, v in record.items() if k not in ("kind", "ts")}
            elif kind == "trace":
                has_trace = True
                step = record.get("step", {})
                if step.get("kind") == "tool" and step.get("name") == "run_command":
                    emit_command(step)
                else:
                    steps.append({"type": "step", **step})
            elif kind == "message" and record.get("role") == "user":
                flush()  # close the previous turn before the next one opens
                events.append({"type": "user", "text": record.get("content", "")})
                open_turn = True
            elif kind == "message" and record.get("role") == "assistant":
                # The task's answer is its last non-empty assistant text;
                # intermediate tool-calling turns carry no visible content.
                content = (record.get("content") or "").strip()
                if content:
                    answer = content
        flush()
        return events if has_trace else None

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
    def _derive_snippet(messages: list[dict]) -> str:
        """Preview line: the last user or assistant message with visible text.
        Tool records and empty tool-calling turns say nothing about where the
        conversation left off, so they are skipped."""
        for message in reversed(messages):
            if message.get("role") not in ("user", "assistant"):
                continue
            content = " ".join((message.get("content") or "").split())
            if not content:
                continue
            bang = _BANG_RE.match(content)
            if bang:
                content = f"! {bang.group(1)}"
            elif message.get("role") == "user":
                content = f"You: {content}"
            if len(content) > SNIPPET_MAX:
                content = content[: SNIPPET_MAX - 1] + "…"
            return content
        return ""

    @staticmethod
    def _peek_title(path: Path) -> str | None:
        """First user message without parsing the whole log — the pager needs
        only a label per session. None = no user input yet (empty chat)."""
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("kind") == "message" and record.get("role") == "user":
                        title = SessionLog._derive_title([record])
                        if len(title) > TITLE_MAX:
                            title = title[: TITLE_MAX - 1] + "…"
                        return title
        except OSError:
            return None
        return None

    @staticmethod
    def _by_recency(state_dir: Path) -> list[Path]:
        """Session files newest-first by last interaction (file mtime) — the
        one ordering every session list shares (drawer, CLI picker, swipe
        pager), so moving through any of them means the same thing."""
        stamped = []
        for path in state_dir.glob("session-*.jsonl"):
            try:
                stamped.append((path.stat().st_mtime, path))
            except OSError:
                continue
        stamped.sort(reverse=True)
        return [path for _, path in stamped]

    @staticmethod
    def pager_titles(state_dir: Path, limit: int = 30) -> list[tuple[str, str]]:
        """(name, title) pages for the web UI's swipe pager: the `limit` most
        recent chats that have a title, same recency ordering as the drawer,
        flipped oldest→newest so back = older. Chats with no user input yet
        are not pages — the cap applies after skipping them, so blank files
        can never crowd real chats out of the pager."""
        pages = []
        for path in SessionLog._by_recency(state_dir):
            title = SessionLog._peek_title(path)
            if title is not None:
                pages.append((path.name, title))
                if len(pages) == limit:
                    break
        pages.reverse()
        return pages

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
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return SessionInfo(
            path=path,
            when=when,
            count=len(messages),
            title=title,
            model=model,
            snippet=SessionLog._derive_snippet(messages),
            mtime=mtime,
        )

    @staticmethod
    def info(path: Path) -> SessionInfo | None:
        """Summary line for a session picker; None for empty sessions."""
        messages, model = SessionLog._parse(path)
        if not messages:
            return None
        return SessionLog._info_from(path, messages, model)

    @staticmethod
    def list_sessions(state_dir: Path, exclude: set | None = None) -> list[SessionInfo]:
        """Non-empty sessions by last interaction, newest first, minus
        excluded paths."""
        exclude = exclude or set()
        infos = []
        for path in SessionLog._by_recency(state_dir):
            if path in exclude:
                continue
            info = SessionLog.info(path)
            if info:
                infos.append(info)
        return infos

    @staticmethod
    def load_entries(state_dir: Path, exclude: set | None = None) -> list["SessionEntry"]:
        """Searchable sessions by last interaction, newest first, read from
        disk once — so a live picker can re-rank on every keystroke without
        touching files."""
        exclude = exclude or set()
        entries = []
        for path in SessionLog._by_recency(state_dir):
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
    def recall_sessions(state_dir: Path, query: str, exclude: set | None = None) -> str:
        """Compact sessions section for the recall tool: top matches with a
        title line and one snippet each, or "" when nothing matches — the
        episodic fallback below skills/memory results."""
        words = query.casefold().split()
        if not words:
            return ""
        infos = SessionLog.search_sessions(state_dir, query, exclude=exclude)
        lines = []
        for info in infos[:RECALL_SESSIONS_TOP]:
            lines.append(f"- {info.path.name} · {info.when} · {info.title}")
            for message in SessionLog.load_messages(info.path):
                snippet = SessionLog._snippet(message.get("content") or "", words)
                if snippet is not None:
                    lines.append(f"    [{message.get('role', '?')}] {snippet}")
                    break
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
        if self._pending_model is not None and kind != "model":
            pending, self._pending_model = self._pending_model, None
            self._record("model", model=pending)
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            **fields,
        }
        if self._fh is None:
            # Created on first record, not in __init__: a chat that never
            # gets a message must leave no file — empty session files crowd
            # every recency-ordered list and pile up across restarts.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def message(self, message: dict) -> None:
        self._record("message", **message)

    def model(self, spec: str) -> None:
        """Note the model in use; written lazily, just before the next real
        record, so the last model record is the session's current model — and
        merely opening/resuming a session never touches its file. Session
        order everywhere is file mtime ("last interaction"), so reviewing an
        old session must not hoist it to most-recent; only new activity does."""
        self._pending_model = spec

    def command(self, command: str, decision: str) -> None:
        self._record("command", command=command, decision=decision)

    def step(self, step: dict) -> None:
        """Persist one structured activity-trace step so the trace is
        reconstructable in any UI, long after the in-memory transcript is
        evicted. The step dict is the same one the web renderer receives."""
        self._record("trace", step=step)

    def command_event(self, event: dict) -> None:
        """Persist a terminal-block framing event (cmd_start / cmd_end). The
        event's `kind` names the record; reconstruct_events replays them as the
        command_start / command_end a live session emits."""
        self._record(event["kind"], **{k: v for k, v in event.items() if k != "kind"})
