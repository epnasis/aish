"""Verify #275 at the real web surface: a restart must not mark read chats unread.

Isolated by construction — its own temp state dir, its own knowledge dirs, port
8899. It never touches a chat it did not write itself, and it types nothing.

Run with `--broken` to restore the pre-#275 behaviour (a cold open publishes,
and the row states no output stamp). That is what makes the check sensitive:
the same script must FAIL there and PASS here.

    uv run --with playwright python scripts/verify_restart_unread.py [--broken]
"""

from __future__ import annotations

import datetime
import json
import multiprocessing
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = 8899
TOKEN = "verify-275"
CHATS = 4  # more than the two the client warms, so the count is meaningful


def write_corpus(state_dir: Path) -> list[str]:
    """Four chats that were fully READ an hour ago and have said nothing since —
    the state a restart used to turn into two unread dots."""
    state_dir.mkdir(parents=True, exist_ok=True)
    spoke = datetime.datetime.now() - datetime.timedelta(hours=1)
    names = []
    for i in range(CHATS):
        # Distinct minutes so the recency order (and so the warm targets) is
        # deterministic rather than filesystem-dependent.
        when = (spoke + datetime.timedelta(minutes=i)).isoformat(timespec="seconds")
        path = state_dir / f"session-20260101-{i:06d}-000000.jsonl"
        path.write_text(
            json.dumps({"ts": when, "kind": "message", "role": "user",
                        "content": f"question number {i}"}) + "\n"
            + json.dumps({"ts": when, "kind": "message", "role": "assistant",
                          "content": f"the answer to number {i}, read long ago"}) + "\n"
            + json.dumps({"ts": when, "kind": "title", "title": f"Read chat {i}"}) + "\n",
            encoding="utf-8",
        )
        names.append(path.name)
    # The owner read every one of them just now: the ledger is the server's, so
    # this is exactly what a genuinely up-to-date reader looks like.
    read_at = time.time()
    (state_dir / "seen.json").write_text(
        json.dumps({"seen": {name: read_at for name in names}})
    )
    return names


def serve(workdir_s: str, broken: bool) -> None:
    workdir = Path(workdir_s)
    from aish import rules as rules_module
    from aish import skills as skills_module
    from aish import tool_plugins as tool_plugins_module

    for mod, attr in ((skills_module, "GLOBAL_SKILLS_DIR"),
                      (skills_module, "GLOBAL_MEMORY_DIR"),
                      (tool_plugins_module, "GLOBAL_TOOLS_DIR"),
                      (rules_module, "GLOBAL_RULES_DIR")):
        d = workdir / "global" / attr.lower()
        d.mkdir(parents=True, exist_ok=True)
        setattr(mod, attr, d)

    import uvicorn

    from aish.server import WebServer, create_app

    if broken:
        # Put the two halves of the bug back, at their real seams.
        original_add = WebServer.add_session
        original_row = WebServer._roster_row

        def add_session(self, session, *, default, publish=True):
            original_add(self, session, default=default, publish=True)

        def roster_row(self, session):
            row = original_row(self, session)
            row.pop("out", None)
            return row

        WebServer.add_session = add_session
        WebServer._roster_row = roster_row

    class ScriptedChat:
        """The run types nothing; the one model turn it needs is the BACKGROUND
        job in scenario 2, which must still raise a dot."""

        def __call__(self, **kwargs):
            from types import SimpleNamespace
            answer = SimpleNamespace(
                message=SimpleNamespace(content="the overnight job finished",
                                        tool_calls=None)
            )
            return iter([answer]) if kwargs.get("stream") else answer

    app = create_app(
        model="verify",
        state_dir=workdir / "state",
        allow_path=workdir / "allow.txt",
        deny_path=workdir / "deny.txt",
        config_path=workdir / "config.toml",
        cwd=str(workdir / "project"),
        client_chat=ScriptedChat(),
        token=TOKEN,
    )
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")


def wait_for_server() -> None:
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/?token={TOKEN}") as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise SystemExit("server never came up")


def fire_background_job() -> str:
    """A real background turn in a chat nobody is viewing — the case #203 exists
    for, and the one a fix for phantom dots could plausibly break."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/trigger?token={TOKEN}",
        data=json.dumps({"prompt": "run the overnight job", "origin": "schedule"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as r:
        return json.loads(r.read())["session"]


def drive() -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 420, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}/?token={TOKEN}")
        page.wait_for_selector("#input", timeout=15000)
        # The warm peeks are on a 900 ms timer after the rail's first paint —
        # this is the window the phantom rows used to arrive in.
        page.wait_for_timeout(4000)
        # app.js is a classic script, so its top-level bindings are reachable
        # by bare name in the page's global scope but not off `window`.
        state = page.evaluate(
            """() => ({
                badgeHidden: document.getElementById('back-badge').hidden,
                badge: document.getElementById('back-badge').textContent,
                counted: [...attentionSessions],
                rows: attentionRows.map(r => ({
                    name: r.name, out: r.out, ts: r.ts, state: r.state,
                })),
            })"""
        )
        # …and what the owner actually sees when they open the rail.
        page.click("#back-chip")
        page.wait_for_timeout(2000)
        state["sections"] = page.evaluate(
            """() => [...document.querySelectorAll('#sessions-list .section-label, '
                     + '#sessions-list .rail-section, #sessions-list h3')]
                     .map(e => e.textContent.trim())"""
        )
        state["dots"] = page.evaluate(
            """() => [...document.querySelectorAll('#sessions-list [class*=unread]')]
                     .map(e => e.className)"""
        )

        # ---- scenario 2: the dot that MUST still appear ---------------------
        job = fire_background_job()
        page.wait_for_timeout(6000)
        state["job"] = job
        state["afterJob"] = page.evaluate(
            """() => ({
                badgeHidden: document.getElementById('back-badge').hidden,
                badge: document.getElementById('back-badge').textContent,
                counted: [...attentionSessions],
            })"""
        )
        browser.close()
    return state


def main() -> int:
    broken = "--broken" in sys.argv
    with tempfile.TemporaryDirectory(prefix="aish-verify-275-") as tmp:
        workdir = Path(tmp)
        (workdir / "project").mkdir()
        names = write_corpus(workdir / "state")
        proc = multiprocessing.Process(target=serve, args=(str(workdir), broken))
        proc.start()
        try:
            wait_for_server()
            state = drive()
        finally:
            proc.terminate()
            proc.join(timeout=10)

    label = "PRE-#275 (bug restored)" if broken else "#275"
    print(f"\n== {label} ==")
    print(json.dumps(state, indent=2)[:2000])
    warmed = [r for r in state["rows"] if r["name"] in names]
    print(f"\nchats written: {len(names)}   rows this client holds: {len(state['rows'])}")
    print(f"counted as needing you: {state['counted']}")

    unread = state["counted"]
    if broken:
        ok = len(unread) > 0
        print("\nEXPECTED here: the bug reproduces (chats counted unread).")
    else:
        ok = len(unread) == 0 and state["badgeHidden"] is True
        print("\nEXPECTED here: nothing counted, badge hidden.")
        # A stamp of "now" on a chat that spoke an hour ago is the phantom's
        # fingerprint even when the seen ledger happens to hide it.
        fresh = [r for r in warmed if r["out"] and time.time() - r["out"] < 120]
        if fresh:
            ok = False
            print(f"a warmed chat claims it just spoke: {fresh}")

    # Scenario 2 holds in BOTH builds: a background job finishing in a chat
    # nobody is viewing must raise a dot. This is what the client's own clock
    # was doing the whole time, and dropping it silently would be a worse bug
    # than the one being fixed.
    after = state["afterJob"]
    print(f"\nbackground job {state['job']} → counted {after['counted']}, "
          f"badge {after['badge']!r} hidden={after['badgeHidden']}")
    if state["job"] not in after["counted"]:
        ok = False
        print("REGRESSION: a background job finished and raised no dot (#203)")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
