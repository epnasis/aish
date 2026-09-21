"""Where the owner's config tree and aish's state tree live — one knob each.

`~/.config/aish/` holds four directories the running agent READS on every task
(rules, skills, memory, tools) plus `config.toml`. They are one thing: the
owner's own corpus. `AISH_CONFIG_HOME` moves all of it at once.

One knob rather than one per directory, because the failure this exists for
(#254) is a run that BELIEVED it was isolated: the verify harness gave itself
its own state dir, allowlist and cwd, then read the owner's 24 live rules and
kept `remember` / `create_skill` pointed at the owner's real store. Four env
vars is four chances to isolate three of them and share the fourth, and the
fourth is the one that writes.

`~/.local/state/aish/` is the machine-wide STATE tree — sessions, the evidence
store, the browser profile, the vouches, the learned rate ceilings, the job
logs. `AISH_STATE_DIR` moves all of it at once, through `state_home()`, and
the default is what the tree resolves to when nothing sets it — with three
stragglers still bound to the real home at import and blind to the knob:
`signin.STATE`, `skill_import.QUARANTINE_ROOT` and `curate.run_curate`'s
default (#399). Before #389 the
default was spelled at each of a dozen call sites and one of them
(`ratelimit._path`) had none, so learned ceilings were persisted only when the
variable happened to be set — never under launchd, where the plist sets it
for nothing. Persistence is a property of the STATE TREE, not of a variable;
one function is what keeps a site from opting out by omission.

The four directory constants are resolved at import: set the variable before
importing aish (the verify harness runs a launcher script, so it does), or
monkeypatch the derived constants (the pytest suite does). The three FILES
beside them — `allow.txt`, `deny.txt`, `lessons.md` — resolve through
`config_home()` at CALL time instead (#390): an import-bound `Path.home()`
there was reachable by any test that drove the approval flow without naming a
path, and an append is the one write the suite's corpus guard cannot see.

`state_home()` resolves at CALL time for the same reason, and for one more:
`server.create_app` exports `AISH_STATE_DIR` at startup so that everything
resolving the state dir below it — the browser profile, the secrets index, the
vouches — agrees with the argument it was built with (#290). A value frozen at
import would be the one thing that did not.
"""

import os
from pathlib import Path

DEFAULT_CONFIG_HOME = Path.home() / ".config" / "aish"
DEFAULT_STATE_HOME = Path.home() / ".local" / "state" / "aish"


def config_home() -> Path:
    return Path(os.environ.get("AISH_CONFIG_HOME") or DEFAULT_CONFIG_HOME)


def state_home() -> Path:
    return Path(os.environ.get("AISH_STATE_DIR") or DEFAULT_STATE_HOME)
