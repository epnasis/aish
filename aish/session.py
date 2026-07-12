"""Append-only JSONL session logs: the conversation (for --resume) and an
audit trail of every command decision, in one file per session."""

import datetime
import json
from pathlib import Path


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
