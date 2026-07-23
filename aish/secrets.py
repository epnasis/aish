"""Local secret store backed by the macOS login Keychain (issue #142).

Secrets for non-CLI integrations (Fastmail JMAP token, Home Assistant token,
ntfy/Pushover keys, webhook secrets) that aish must HOLD — the "auth stays with
the CLI" doctrine covers none of these. Design (decided with the owner, Fable-
reviewed):

- **Pure macOS Keychain** — each secret is a generic-password item under the
  service name "aish", read via ``/usr/bin/security``. Nothing aish writes lands
  on disk as plaintext, and a Keychain item structurally CANNOT be swept into
  the git-backed ``~/.config/aish`` (it is not a file). FileVault + login-unlock
  is the at-rest boundary.
- **Never in args, logs, or model context.** Secrets are resolved at tool-exec
  time and injected into ONLY the declaring wrapper's environment (see
  ``tool_plugins.execute``). A value never enters a tool-call's arguments (which
  are logged) nor the model's messages.

A plaintext index of secret NAMES (not values) is kept in the state dir so the
CLI can list what is set — names are metadata, not secret.

The realistic security ceiling is FileVault + OS access control: this protects
against disk theft and accidental leakage (git, logs, model context), NOT a
live attacker already running as the user. That is out of scope by design.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SERVICE = "aish"
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # env-var-shaped
NAMES_INDEX = Path.home() / ".local" / "state" / "aish" / "secret-names.txt"
_SECURITY = "/usr/bin/security"


class SecretError(RuntimeError):
    pass


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def _security(args: list[str], value: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_SECURITY, *args], input=value, text=True, capture_output=True
    )


def get(name: str) -> str | None:
    """The secret's value, or None if unset. Trailing newline stripped."""
    if not valid_name(name):
        return None
    proc = _security(["find-generic-password", "-a", name, "-s", SERVICE, "-w"])
    if proc.returncode != 0:
        return None
    # -w prints the value; a trailing newline is added by `security`, not stored.
    return proc.stdout.rstrip("\n")


def put(name: str, value: str) -> None:
    """Store or update a secret. Raises SecretError on failure."""
    if not valid_name(name):
        raise SecretError(f"invalid secret name {name!r} (need [A-Za-z_][A-Za-z0-9_]*)")
    # -U updates in place if the item exists. NOTE: the value passes through the
    # `security` process argv briefly — acceptable on a single-user box (the
    # ceiling is FileVault anyway), and `security` offers no stdin password path.
    proc = _security(
        ["add-generic-password", "-a", name, "-s", SERVICE, "-U", "-w", value]
    )
    if proc.returncode != 0:
        raise SecretError(proc.stderr.strip() or "failed to store secret")
    _index_add(name)


def delete(name: str) -> bool:
    """Remove a secret; True if it existed."""
    if not valid_name(name):
        return False
    proc = _security(["delete-generic-password", "-a", name, "-s", SERVICE])
    _index_remove(name)
    return proc.returncode == 0


def names() -> list[str]:
    """Names of stored secrets (from the state-dir index), sorted."""
    try:
        return sorted(
            n for n in NAMES_INDEX.read_text(encoding="utf-8").splitlines() if n.strip()
        )
    except OSError:
        return []


def _index_add(name: str) -> None:
    current = set(names())
    if name in current:
        return
    current.add(name)
    _write_index(current)


def _index_remove(name: str) -> None:
    current = set(names())
    if name in current:
        current.discard(name)
        _write_index(current)


def _write_index(name_set: set[str]) -> None:
    try:
        NAMES_INDEX.parent.mkdir(parents=True, exist_ok=True)
        NAMES_INDEX.write_text("\n".join(sorted(name_set)) + "\n", encoding="utf-8")
    except OSError:
        pass
