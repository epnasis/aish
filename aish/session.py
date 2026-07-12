"""Append-only JSONL session logs: the conversation (for --resume) and an
audit trail of every command decision, in one file per session."""

import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path

TITLE_MAX = 60
_BANG_RE = re.compile(r"^\[I ran `(.+?)` myself")


@dataclass
class SessionInfo:
    path: Path
    when: str
    count: int
    title: str


class SessionLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

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
    def load_messages(path: Path) -> list[dict]:
        """Conversation messages only (no audit records, no stale system
        prompt — a fresh one is built on resume)."""
        messages = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") == "message" and record.get("role") != "system":
                messages.append(
                    {k: v for k, v in record.items() if k in ("role", "content", "tool_name")}
                )
        return messages

    @staticmethod
    def info(path: Path) -> SessionInfo | None:
        """Summary line for a session picker; None for empty sessions.
        The title is the first user message — cheap, deterministic, and it
        almost always names the task."""
        messages = SessionLog.load_messages(path)
        if not messages:
            return None
        title = "(no user input)"
        for message in messages:
            if message.get("role") == "user":
                content = " ".join((message.get("content") or "").split())
                bang = _BANG_RE.match(content)
                title = f"! {bang.group(1)}" if bang else content
                break
        if len(title) > TITLE_MAX:
            title = title[: TITLE_MAX - 1] + "…"

        try:  # session-YYYYmmdd-HHMMSS[-ffffff].jsonl
            _, day, clock = path.stem.split("-")[:3]
            when = datetime.datetime.strptime(f"{day}-{clock}", "%Y%m%d-%H%M%S")
        except ValueError:
            when = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        return SessionInfo(
            path=path, when=when.strftime("%b %d %H:%M"), count=len(messages), title=title
        )

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

    def command(self, command: str, decision: str) -> None:
        self._record("command", command=command, decision=decision)
