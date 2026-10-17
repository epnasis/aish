"""One replay run, in its own process (#441) — the half that talks to aish.

The runner (`aish/replay.py`) starts this file as a SCRIPT with
`python -P replay_driver.py spec.json`, with PYTHONPATH pointing at the code
under test (this worktree, or an archive of the baseline ref) and the run's
isolated environment already exported. One process per run because
`AISH_STATE_DIR` is process-global and resolved at call time, and the config
constants under `AISH_CONFIG_HOME` are bound at import: two runs in one
process would share whichever was set first.

It drives the real `create_app` over the same WebSocket the browser uses and
answers every approval card from the scenario's policy, which DENIES by
default — or, for a card a `[[cards.comment]]` rule matches, with that rule's
approve-or-deny and the owner's comment, verbatim from the scenario file.
It imports nothing from `aish.replay*` at module level, so it also runs
against a baseline tree that predates the harness; the only aish API it
relies on is `create_app` and the wire protocol.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

#: Anything that could chain, substitute or redirect: a command that starts with
#: an approved prefix but carries one of these is NOT the approved command.
SHELL_META = re.compile(r"[;&|<>`$\n\\]|\(|\)")

Decision = tuple[bool, str]


def card_decision(event: dict, approve_commands: Sequence[str],
                  approve_tools: Sequence[str]) -> Decision:
    """(approve?, why) for one approval card. Deny unless the scenario named it."""
    kind = event.get("kind")
    if kind == "command":
        command = str(event.get("command") or "")
        prefix = next((p for p in approve_commands if command.startswith(p)), None)
        if prefix is None:
            return False, "command prefix not approved by the scenario"
        if SHELL_META.search(command):
            return False, f"starts with approved {prefix!r} but carries a shell operator"
        return True, f"command prefix {prefix!r}"
    if kind == "tool":
        tool = str(event.get("tool") or "")
        if tool in approve_tools:
            return True, f"tool {tool!r}"
        return False, f"tool {tool!r} not approved by the scenario"
    return False, f"card kind {kind!r} is never approved by a replay"


#: The only card kinds a comment rule may answer. The server reads a comment on
#: command, tool, write and import cards, but only on the first two does every
#: consumer HOLD an approval that carries one (agent.py, docs/agent-core.md L2):
#: a read card drops the comment and an approve RUNS the read, and an import
#: approved with a comment installs the skill. A write is held, but no owner
#: session that needed one has been mined; it is added when one is.
COMMENTABLE_KINDS = ("command", "tool")
COMMENT_ACTIONS = ("approve", "deny")


def comment_text(rule: dict) -> str | None:
    """The comment a rule sends, or None when the server would not see one.

    The server reads `str(answer.get("comment") or "").strip()`: a comment that
    is not a string, or is empty after Python's own strip, turns a commented
    approve into a PLAIN approve that runs. So such a rule never answers."""
    comment = rule.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        return None
    return comment


def comment_rule_for(event: dict, rules: Sequence[dict], used: list[int]) -> int | None:
    """Index of the first rule, in file order, that still has uses left and
    matches this card — or None, and the card goes to `card_decision`.

    `used[i]` counts the cards rule i answered in this run. A rule answers at
    most `times` cards (default 1), because text cannot tell a first proposal
    from its re-proposal (#368 makes them identical when the comment asks no
    change) and the owner typed each comment once; after that the card falls
    through to the deny-by-default policy, which stays the ONLY way anything
    is approved to run."""
    kind = event.get("kind")
    if kind not in COMMENTABLE_KINDS:
        return None
    for index, rule in enumerate(rules):
        times = rule.get("times", 1)
        if not isinstance(times, int) or isinstance(times, bool) or used[index] >= times:
            continue
        if rule.get("action") not in COMMENT_ACTIONS or comment_text(rule) is None:
            continue
        if kind == "command" and isinstance(rule.get("command_re"), str):
            if re.search(rule["command_re"], str(event.get("command") or "")):
                return index
        elif kind == "tool" and isinstance(rule.get("tool"), str):
            if rule["tool"] == str(event.get("tool") or ""):
                return index
    return None


def card_summary(event: dict) -> str:
    for key in ("command", "tool", "target", "path", "skill"):
        if event.get(key):
            return str(event[key])[:300]
    return ""


def drive(ws: Any, turns: Sequence[str], approve_commands: Sequence[str],
          approve_tools: Sequence[str], emit: Callable[[dict], None],
          limit: int | None = None, comment_rules: Sequence[dict] = ()) -> None:
    """Send each turn, answer its cards, and wait for its `done`.

    A card a comment rule matches is answered with that rule's action and
    comment (`comment_rule_for`); every other card by `card_decision`.
    `limit` bounds the events read per turn so a test that waits for a card can
    FAIL instead of hanging; a real run is bounded by the runner's timeout."""
    used = [0] * len(comment_rules)
    hello = ws.receive_json()
    emit({"type": "session", "session": hello.get("session"), "hello": hello.get("type")})
    ws.receive_json()  # the replay of the (empty) new session
    for index, text in enumerate(turns, 1):
        started = time.time()
        emit({"type": "turn_start", "turn": index, "text": text})
        ws.send_json({"type": "task", "text": text})
        seen = 0
        while True:
            seen += 1
            if limit is not None and seen > limit:
                raise RuntimeError(f"turn {index}: no done within {limit} events")
            event = ws.receive_json()
            kind = event.get("type")
            if kind == "approval_request":
                ruled = comment_rule_for(event, comment_rules, used)
                if ruled is not None:
                    rule = comment_rules[ruled]
                    used[ruled] += 1
                    action, comment = str(rule["action"]), comment_text(rule)
                    # `approved` keeps its meaning — sent as a plain approval,
                    # so it may run. A commented approve is held, never run.
                    emit({"type": "card", "turn": index, "kind": event.get("kind"),
                          "what": card_summary(event), "approved": False,
                          "action": action, "comment": comment, "rule": ruled + 1,
                          "why": f"comment rule {ruled + 1}, use {used[ruled]}"})
                    ws.send_json({"type": "approval", "id": event["id"],
                                  "action": action, "comment": comment})
                    continue
                approve, why = card_decision(event, approve_commands, approve_tools)
                emit({"type": "card", "turn": index, "kind": event.get("kind"),
                      "what": card_summary(event), "approved": approve,
                      "action": "approve" if approve else "deny", "why": why})
                ws.send_json({"type": "approval", "id": event["id"],
                              "action": "approve" if approve else "deny"})
            elif kind == "error":
                emit({"type": "error", "turn": index,
                      "event": json.dumps(event, ensure_ascii=False)[:1000]})
            elif kind == "done":
                emit({"type": "done", "turn": index, "secs": round(time.time() - started, 1),
                      "result": event.get("result")})
                break


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    spec = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    events = open(spec["events_path"], "a", encoding="utf-8", buffering=1)

    def emit(record: dict) -> None:
        record.setdefault("t", round(time.time(), 3))
        events.write(json.dumps(record, ensure_ascii=False) + "\n")

    import tomllib

    from starlette.testclient import TestClient

    import aish
    from aish.server import create_app

    # Evidence, not an assumption, that the code under test is what got imported.
    emit({"type": "start", "aish_file": aish.__file__, "python": sys.executable})
    config_path = Path(spec["config_path"])
    config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    app = create_app(
        spec["model"], state_dir=Path(spec["state_dir"]), allow_path=Path(spec["allow_path"]),
        deny_path=Path(spec["deny_path"]), lessons_path=Path(spec["lessons_path"]),
        config_path=config_path, cwd=spec["cwd"], token="replay",
        max_steps=int(spec.get("max_steps") or config.get("max_steps", 25)),
        num_ctx=int(spec.get("num_ctx") or config.get("num_ctx", 32768)),
        aliases=config.get("aliases"),
    )
    with TestClient(app) as client, client.websocket_connect("/ws?token=replay") as ws:
        drive(ws, spec["turns"], spec["approve_commands"], spec["approve_tools"], emit,
              comment_rules=spec.get("comment_rules") or [])
    emit({"type": "end"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
