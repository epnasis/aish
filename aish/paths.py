"""Where the owner's config tree lives — one knob for the whole of it.

`~/.config/aish/` holds four directories the running agent READS on every task
(rules, skills, memory, tools) plus `config.toml`. They are one thing: the
owner's own corpus. `AISH_CONFIG_HOME` moves all of it at once.

One knob rather than one per directory, because the failure this exists for
(#254) is a run that BELIEVED it was isolated: the verify harness gave itself
its own state dir, allowlist and cwd, then read the owner's 24 live rules and
kept `remember` / `create_skill` pointed at the owner's real store. Four env
vars is four chances to isolate three of them and share the fourth, and the
fourth is the one that writes.

Resolved at import, like `AISH_STATE_DIR` at its call sites: set the variable
before importing aish (the verify harness runs a launcher script, so it does),
or monkeypatch the derived constants (the pytest suite does).
"""

import os
from pathlib import Path

DEFAULT_CONFIG_HOME = Path.home() / ".config" / "aish"


def config_home() -> Path:
    return Path(os.environ.get("AISH_CONFIG_HOME") or DEFAULT_CONFIG_HOME)
