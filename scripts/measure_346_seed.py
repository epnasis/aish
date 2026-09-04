"""Did any host in the live vouch store get there through a COMMENTED yes?

#346's LOW half changes `SessionLog.approved_read_hosts` to accept only a plain
`approved`. This measures what that costs on the owner's own machine: which
hosts the OLD rule would seed, which the NEW one does, and which of the
difference are in the store already with no clean approval behind them.

Read-only. Prints hosts and counts, never a URL, a command or a message.

    uv run python scripts/measure_346_seed.py
"""

from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

from aish.session import SessionLog

STATE = Path(
    os.environ.get("AISH_STATE_DIR") or (Path.home() / ".local/state/aish")
).expanduser()


def hosts_by_rule(path: Path, plain_only: bool) -> set[str]:
    found: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        if "url=" not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("kind") != "command":
            continue
        if record.get("superseded"):
            continue
        decision = str(record.get("decision", ""))
        ok = decision == "approved" if plain_only else decision.startswith("approved")
        if not ok:
            continue
        command = str(record.get("command", ""))
        if not SessionLog._READ_TOOL_CALL.match(command):
            continue
        if str(record.get("asked_by", "")) in SessionLog.NEVER_SEEDS:
            continue
        match = SessionLog._URL_ARG.search(command)
        if match is None:
            continue
        try:
            host = (
                urllib.parse.urlsplit(
                    match.group(1) or match.group(2) or ""
                ).hostname or ""
            ).lower()
        except ValueError:
            continue
        if host:
            found.add(host)
    return found


def main() -> None:
    logs = sorted(STATE.glob("session-*.jsonl"))
    old: set[str] = set()
    new: set[str] = set()
    for path in logs:
        old |= hosts_by_rule(path, plain_only=False)
        new |= hosts_by_rule(path, plain_only=True)

    store_path = STATE / "egress-vouches.json"
    try:
        stored = set(json.loads(store_path.read_text(encoding="utf-8")).get("hosts", []))
    except (OSError, ValueError, AttributeError):
        stored = set()

    print(f"logs scanned:                {len(logs)}")
    print(f"seeded by the OLD rule:      {len(old)}")
    print(f"seeded by the NEW rule:      {len(new)}")
    lost = sorted(old - new)
    print(f"only a COMMENTED yes:        {len(lost)} {lost}")
    print(f"hosts in the live store:     {len(stored)}")
    print(f"…of those, commented-only:   {sorted(set(lost) & stored)}")


if __name__ == "__main__":
    main()
