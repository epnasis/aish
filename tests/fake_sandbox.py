"""Stand-ins for `aish.sandbox`'s process-wide runner (epic #462).

The suite must not depend on the host: CI is Linux, where the real sandbox is
unavailable by construction, and on macOS a real run reads the system log for
most of a second per command. `tests/conftest.py` installs `PassThroughSandbox`
for every test, so a cardless command still runs (unconfined, as the suite has
always run it) and every test that drives one can see that it WAS routed
through the sandbox seam. Tests about the sandbox itself install their own:
`UnavailableSandbox` for the fail-closed paths, `RefusingSandbox` for the
refusal record, and the real `SeatbeltSandbox` under the `macos_sandbox` marker.
"""

from __future__ import annotations

import os

from aish import sandbox


class FakeConfinement:
    tier = "read_only"
    nonce = "fakenonce"
    started = 0.0

    def __init__(self, owner: PassThroughSandbox, report: sandbox.RefusalReport):
        self._owner = owner
        self._report = report
        self.paths = None

    def launch(self, command: str) -> tuple[list[str], dict[str, str]]:
        self._owner.launched.append(command)
        return ["/bin/sh", "-c", command], dict(os.environ)

    def refusals(self, exit_code: int | None = None) -> sandbox.RefusalReport:
        self._owner.read.append(exit_code)
        return self._report

    def describe(self) -> str:
        return "it could read this chat's folders (test stand-in)"


class PassThroughSandbox:
    """Available; runs the command unconfined; records what went through it."""

    def __init__(self, report: sandbox.RefusalReport | None = None):
        self.launched: list[str] = []
        self.read: list[int | None] = []
        self.prepared: list[dict] = []
        self.report = report if report is not None else sandbox.RefusalReport()

    def prepare(self, roots, scratch, protected=(), path_env=None):
        self.prepared.append(
            {"roots": list(roots), "scratch": scratch, "protected": list(protected)}
        )
        return FakeConfinement(self, self.report)


class RefusingSandbox(PassThroughSandbox):
    """Available, and its log names one refusal for every run."""

    def __init__(self, refusal: sandbox.Refusal | None = None):
        refusal = refusal or sandbox.Refusal("sort", "file-write-data", "/etc/hosts")
        super().__init__(sandbox.RefusalReport(refusals=[refusal], seen=1))


class UnavailableSandbox:
    """What a Linux box, a missing sandbox-exec or a failed canary looks like."""

    def __init__(self, observed: str = "this machine runs linux, and aish's sandbox needs macOS"):
        self.observed = observed
        self.prepared = 0

    def prepare(self, roots, scratch, protected=(), path_env=None):
        self.prepared += 1
        return sandbox.Unavailable(self.observed)
