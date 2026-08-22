#!/usr/bin/env -S uv run python
"""Drive a real page cut into a real session log, then read it back with the
real `aish explain` (#274).

The unit tests build dossiers by hand and assert on `notes()`. That leaves the
one question this phase exists to answer untested end to end: can a person, days
later, point `aish explain` at a session and be told that a page was cut and
whether the model ever read the rest?

Two runs, differing in one thing only — whether the model pages. The whole
point is that the two produce DIFFERENT rows.

Not in the pytest suite: it writes session logs into a throwaway state dir and
renders the terminal view, which is a different kind of check from asserting on
a dict.

    uv run python scripts/verify_page_cut_trace.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["AISH_STATE_DIR"] = tempfile.mkdtemp(prefix="verify-page-cut-")
os.environ["AISH_NOTIFY"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aish import agent as agent_module  # noqa: E402
from aish import browse as browse_mod  # noqa: E402
from aish import explain as explain_mod  # noqa: E402
from aish import rules as rules_mod  # noqa: E402
from aish import skills as skills_mod  # noqa: E402
from aish import web as web_module  # noqa: E402

# The owner's LIVE rules, skills and memory are module constants with no env
# override (#254), so a script that does not blank them is driving his corpus.
# Here that is not merely untidy: one of his rules refuses `browse` outright,
# so the run under test never happened and the check passed on a turn that did
# nothing.
_EMPTY = Path(os.environ["AISH_STATE_DIR"]) / "empty"
_EMPTY.mkdir(parents=True, exist_ok=True)
rules_mod.GLOBAL_RULES_DIR = _EMPTY
skills_mod.GLOBAL_SKILLS_DIR = _EMPTY
skills_mod.GLOBAL_MEMORY_DIR = _EMPTY

ROWS = 250
PAGE = "\n".join(f"{n}. Title {n}\n" + ("x" * 400) for n in range(1, ROWS + 1))


from tests.test_agent import model_says as says  # noqa: E402
from tests.test_agent import tool_call as call  # noqa: E402


class Chat:
    """The model, scripted — the ollama client's shape, borrowed from the suite
    so this drives the same seam the tests do."""

    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, **_kw):
        # Exhausted means "the task is over": the agent may make more calls than
        # this script scripted (a final no-tools turn, a stop gate), and a
        # scripted list that runs out should end the task, not crash it.
        while self.replies and self.replies[0] is None:
            self.replies.pop(0)
        return self.replies.pop(0) if self.replies else says("done")


def run(name: str, pages_it: str = "", cacheable: bool = True) -> Path:
    """One session: browse a 250-row page, then either page the rest or not.

    `pages_it` is the continuation key to read back, taken from the FIRST run's
    log. The key is a content hash, so the same page produces the same key and
    the two sessions share one cache — which also means this second run is
    reading back the very bytes the first one cut."""
    state = Path(os.environ["AISH_STATE_DIR"])
    log = state / f"session-{name}.jsonl"

    web_module.browser.browse_open = (
        lambda url, *, topic="", **_kw: browse_mod.Snapshot(
            url=url, title="Your ratings", text=PAGE, controls=[]
        )
    )
    web_module._require_public = lambda _url: None

    replies = [says(tool_calls=[call("browse", url="https://imdb.test/ratings/")])]
    if pages_it:
        replies.append(
            says(tool_calls=[
                call("read_tool_output", continuation=pages_it, page=2)
            ])
        )
    replies.append(says("done"))

    records: list[dict] = []

    def write(record: dict) -> None:
        records.append(record)

    agent = agent_module.Agent(
        model="fake",
        approve=lambda _c: True,
        client_chat=Chat(replies),
        approve_tool=lambda *_a: True,
        state_dir=str(state),
        step_log=write,
    )

    if not cacheable:
        # What an unwritable continuation store looks like from here: the cut
        # still happens, nothing can be offered, and the record has to say so.
        # This is #269's own shape, and the one `result_cut` is for.
        agent._stash_page = lambda _text, _shown: ""  # type: ignore[method-assign]

    agent.run_task("read all my ratings")

    # A session log the reader can open: the turn bracket, the prompt, and the
    # trace steps, in the shape `explain.load` expects.
    lines = [
        {"kind": "task_start", "prompt": "read all my ratings", "ts": "2026-08-22T10:00:00"},
        {"kind": "message", "role": "user", "content": "read all my ratings"},
    ]
    lines += [{"kind": "trace", "step": r} for r in records]
    lines.append({"kind": "task_end", "status": "ok"})
    log.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return log


def show_steps(log: Path) -> None:
    for line in log.read_text().splitlines():
        step = json.loads(line).get("step") or {}
        if step.get("kind") == "tool":
            print("  step:", json.dumps(
                {k: v for k, v in step.items() if k not in ("output", "summary")},
                ensure_ascii=False,
            )[:340])


def checks_in(log: Path) -> tuple[set[str], str]:
    loaded = explain_mod.load(log)
    turn = loaded.turns[-1]
    doc = explain_mod.dossier(turn, loaded, None)
    rendered = explain_mod.render(turn, loaded, None, show_tools=True)
    return {row["check"] for row in explain_mod.notes(doc)["rows"]}, rendered


def main() -> int:
    print(f"state: {os.environ['AISH_STATE_DIR']}")

    ignored = run("ignored")
    show_steps(ignored)
    checks, rendered = checks_in(ignored)
    print(f"\nthe model was offered page 2 and did not take it → {sorted(checks)}")
    for line in rendered.splitlines():
        if "characters cut" in line or "read back from cache" in line:
            print("  " + line.strip())
    assert "continuation_unread" in checks, (
        "a cut that was never read back drew no row — the whole point of #274"
    )
    assert "result_cut" not in checks, "a continuation WAS offered; this is not a dead end"

    key = next(
        json.loads(line)["step"]["truncation"]["continuation"]
        for line in ignored.read_text().splitlines()
        if "truncation" in line
    )
    paged = run("paged", pages_it=key)
    show_steps(paged)
    checks, rendered = checks_in(paged)
    assert "tool_failed" not in checks, (
        "the paging call itself failed — this run proves nothing about the check"
    )
    print(f"\nthe model paged the rest → {sorted(checks) or 'no rows, which is correct'}")
    for line in rendered.splitlines():
        if "characters cut" in line or "read back from cache" in line:
            print("  " + line.strip())
    assert "continuation_unread" not in checks, (
        "the rest WAS read back; flagging it would bury the rows that matter"
    )

    dead_end = run("dead-end", cacheable=False)
    show_steps(dead_end)
    checks, rendered = checks_in(dead_end)
    print(f"\nnothing could be cached, so nothing was offered → {sorted(checks)}")
    for line in rendered.splitlines():
        if "characters cut" in line:
            print("  " + line.strip())
    assert "result_cut" in checks, "a dead end must be recorded AS a dead end"
    assert "continuation_unread" not in checks, (
        "nothing was offered, so 'offered and not read' is the wrong row"
    )

    print("\nOK — a real log, read back by the real reader, tells the three apart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
