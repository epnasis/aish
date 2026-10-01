"""Generate the replay checks' fixture logs from INVENTED data (#441).

Nothing here comes from a real session. Every hotel, id, URL and photo is made
up; the party is the synthetic example's (2 adults, children 9 and 5); the
turns are `evals/example-trippy`'s. The log is produced by the REAL server and
agent — `create_app` over its WebSocket, answered by the replay driver's own
card policy — with a scripted model and stubbed command / page / image
seams, so every record in it has the shape aish writes today.

Two variants, each scripted so every library and example check gets a verdict
the script controls:

- `good`: right party, single-unit, ≤3 ids, links only to pages read, pictures
  only from printed photo URLs — every check passes.
- `bad`:  a wrong-party search without single-unit, a failed command, a 5-id
  details call, the same page read three times, a piped command (denied by the
  card policy), a link never opened, a picture whose URL nothing printed —
  every check fails.

Generated in-test rather than committed: the log carries wall-clock stamps,
random turn ids and temp paths, so a committed copy could only be compared
through a normaliser, and that normaliser could hide drift. Must run inside
pytest, whose autouse fixtures isolate the config home, notifications, secrets
and browser.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import aish.agent as agent_module
from aish import replay_driver
from aish.server import create_app
from tests.test_server import _is_title_call, model_says, tool_call

TURNS = [
    "Find places to stay for us: Lisbon Jul 10–14, Porto Jul 14–18 (2028). "
    "Two adults, kids aged 9 and 5.",
    "Show prices in EUR please",
    "Can all four of us stay in one apartment or house?",
    "Show me photos of the ones you suggest",
]
PARTY = "--adults 2 --children 9,5"
LISTINGS = {
    "9100101": ("Casa Azulejo Family Flat", "casa-azulejo-family-flat"),
    "9100102": ("Ribeira Loft Four", "ribeira-loft-four"),
    "9100103": ("Alfama Garden House", "alfama-garden-house"),
    "9100104": ("Foz Beach Apartment", "foz-beach-apartment"),
    "9100105": ("Bairro Alto Duplex", "bairro-alto-duplex"),
}
PHOTO_HEX = "5e1f0c3a9b7d24e6"


def page(listing_id: str) -> str:
    return f"https://www.booking.com/hotel/pt/{LISTINGS[listing_id][1]}.html"


def photo(listing_id: str, n: int) -> str:
    return (f"https://cf.bstatic.com/xdata/images/hotel/max1024/{listing_id}{n}.jpg"
            f"?k={PHOTO_HEX}{n}&o=")


NEVER_PRINTED_PHOTO = "https://cf.bstatic.com/xdata/images/hotel/max1024/4040404.jpg?k=00&o="
NEVER_OPENED_PAGE = "https://www.booking.com/hotel/pt/invented-never-opened.html"


def fake_trippy(command: str, **_kw) -> str:
    """Invented trippy output; an unknown option fails like the real CLI."""
    if "--check-in-date" in command:
        return "Usage: trippy search [OPTIONS]\nError: No such option: --check-in-date\n" \
               "[exit code: 2]"
    if command.startswith("trippy search"):
        rows = [f"[{i}] {name}  EUR {120 + int(i[-1]) * 10}/night  ★8.{i[-1]}"
                for i, (name, _) in LISTINGS.items()]
        return "\n".join(rows) + "\n[exit code: 0]"
    if command.startswith("trippy details"):
        ids = command.split("--property-id", 1)[1].split()[0].split(",")
        blocks = [f"{LISTINGS[i][0]}\nurl: {page(i)}\nphotos:\n  {photo(i, 1)}\n  {photo(i, 2)}"
                  for i in ids if i in LISTINGS]
        return "\n\n".join(blocks) + "\n[exit code: 0]"
    return f"invented output for: {command}\n[exit code: 0]"


def fake_read(url: str, topic: str | None = None, **_kw) -> str:
    return f"[{url}] Invented listing page. Sleeps 4. Kitchen. EUR prices."


def _search(city: str, dates: str, party: str = PARTY, extra: str = "--single-unit") -> dict:
    checkin, checkout = dates.split("/")
    words = ["trippy search --site booking --location", city, "--checkin", checkin,
             "--checkout", checkout, party, extra, "--live"]
    return tool_call("run_command", command=" ".join(w for w in words if w))


def _script(variant: str) -> list:
    says = model_says
    if variant == "good":
        return [
            says(tool_calls=[_search("Lisbon", "2028-07-10/2028-07-14")]),
            says(tool_calls=[_search("Porto", "2028-07-14/2028-07-18")]),
            says(tool_calls=[tool_call("run_command", command="trippy details --site booking "
                                       "--property-id 9100101,9100102 --live")]),
            says(tool_calls=[tool_call("read_url", url=page("9100101")),
                             tool_call("read_url", url=page("9100102"))]),
            says(f"Two fit all four of you: [Casa Azulejo]({page('9100101')}) in Lisbon and "
                 f"[Ribeira Loft]({page('9100102')}) in Porto."),
            says(f"Both are priced in EUR already; [Casa Azulejo]({page('9100101')}) is "
                 "EUR 130/night."),
            says("Yes — both are whole apartments that sleep four."),
            says(tool_calls=[tool_call("show_image", source=photo("9100101", 1),
                                       caption="Casa Azulejo — photo 1"),
                             tool_call("show_image", source=photo("9100102", 1),
                                       caption="Ribeira Loft — photo 1")]),
            says("Those are the first photo of each."),
        ]
    if variant == "bad":
        read_one = says(tool_calls=[tool_call("read_url", url=page("9100101"))])
        return [
            says(tool_calls=[_search("Lisbon", "2028-07-10/2028-07-14",
                                     party="--adults 1 --children 9,5", extra="")]),
            says(tool_calls=[tool_call("run_command", command="trippy search --site booking "
                                       "--location Porto --check-in-date 2028-07-14")]),
            says(tool_calls=[tool_call("run_command", command="trippy details --site booking "
                                       "--property-id 9100101,9100102,9100103,9100104,9100105 "
                                       "--live")]),
            read_one, read_one, read_one,
            says(tool_calls=[tool_call("run_command", command="trippy search --site booking "
                                       "--location Porto | head -5")]),
            says(f"Try [Casa Azulejo]({page('9100101')}) or "
                 f"[a place I remember]({NEVER_OPENED_PAGE})."),
            says("Prices are in EUR."),
            says("Probably."),
            says(tool_calls=[tool_call("show_image", source=NEVER_PRINTED_PHOTO,
                                       caption="a photo")]),
            says("Here is one."),
        ]
    raise ValueError(variant)


class ScriptedModel:
    """The test_server FakeChat, plus: a side call (a title, or any call made
    without the tool menu) never consumes the script, and a script that runs
    out answers with plain text instead of raising — an extra model call the
    agent makes (a nudge, a wrap-up) then shows up in the log, not as a hang."""

    def __init__(self, responses: list):
        self.responses = list(responses)

    def __call__(self, **kwargs):
        if _is_title_call(kwargs) or not kwargs.get("tools"):
            response = model_says("")
        else:
            response = self.responses.pop(0) if self.responses else model_says("Done.")
        return iter([response]) if kwargs.get("stream") else response


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def generate(variant: str, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drive one scripted run; return the session log it wrote."""
    state, cwd = workdir / "state", workdir / "cwd"
    cwd.mkdir(parents=True)
    monkeypatch.setenv("AISH_STATE_DIR", str(state))
    monkeypatch.setattr(agent_module.tools, "run_command", fake_trippy)
    monkeypatch.setattr(agent_module.web, "read_url", fake_read)
    monkeypatch.setattr(agent_module.web, "fetch_binary", lambda url, max_bytes: (PNG,
                                                                                 "image/png"))
    allow = workdir / "allow.txt"
    allow.write_text("", encoding="utf-8")
    app = create_app("fake", client_chat=ScriptedModel(_script(variant)), state_dir=state,
                     allow_path=allow, deny_path=workdir / "deny.txt",
                     config_path=workdir / "config.toml", lessons_path=workdir / "lessons.md",
                     cwd=str(cwd), token="fixture")
    events: list[dict] = []
    with TestClient(app) as client, client.websocket_connect("/ws?token=fixture") as ws:
        replay_driver.drive(ws, TURNS, ["trippy "], ["show_image", "read_url", "web_search"],
                            events.append, limit=500)
    (workdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    session = next(e["session"] for e in events if e["type"] == "session")
    return state / session
