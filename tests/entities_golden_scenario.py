"""The fixed task set behind the byte-identity golden (slice 2).

Imported by `tests/test_entities_agent.py` and, unchanged, by a throwaway
copy of the commit BEFORE slice 2 that recorded
`tests/fixtures/entities_byte_identity_golden.json` — so the golden is what
that code sent, not this code compared with itself. Uses only what both
commits have: `make_agent`, `FakeChat`, `save_memory`, and the suite's
`isolated_global_dirs`. No entity file is ever written here.
"""

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

from aish import skills
from tests.test_agent import make_agent, model_says

# A remembered fact longer than the whole pre-flight budget (12,000 at the
# default context): the one case the budget fix changes.
LONG_DESCRIPTION = "kettle manual: " + "x" * 13000


def _call(tool, /, **arguments):
    return SimpleNamespace(function=SimpleNamespace(name=tool, arguments=arguments))


def _normalised(chat, agent, tmp_path) -> list:
    text = json.dumps(chat.snapshots, sort_keys=True)
    text = re.sub(r"Current local time: [^<\"\\]+", "TIME", text)
    text = text.replace(str(agent.scratch_dir), "SCRATCH")
    text = text.replace(str(tmp_path), "CWD")
    text = text.replace(os.environ["AISH_CONFIG_HOME"], "CONFIG")
    return json.loads(text)


def _clear_memory() -> None:
    for path in skills.GLOBAL_MEMORY_DIR.glob("*.md"):
        path.unlink()


def run(tmp_path: Path) -> dict:
    """{scenario: every request's messages, normalised}."""
    out = {}
    skills.save_memory("descale the kettle with citric acid", skills.GLOBAL_MEMORY_DIR,
                       name="kettle-descaling", keywords="kettle")
    agent, chat = make_agent(
        [model_says(tool_calls=[_call("recall", query="kettle")]),
         model_says(tool_calls=[_call("remember", note="kettle is a Bosch",
                                      name="kettle-model")]),
         model_says("done"),
         model_says("it is a Bosch")],
        cwd=str(tmp_path),
    )
    for task in ("how do I descale the kettle", "thanks, and the kettle model?"):
        agent.run_task(task)
    out["kettle"] = _normalised(chat, agent, tmp_path)
    _clear_memory()

    skills.save_memory(LONG_DESCRIPTION, skills.GLOBAL_MEMORY_DIR, name="kettle-manual",
                       keywords="kettle")
    agent, chat = make_agent([model_says("ok")], cwd=str(tmp_path))
    agent.run_task("the kettle manual please")
    out["long_description"] = _normalised(chat, agent, tmp_path)
    _clear_memory()
    return out
