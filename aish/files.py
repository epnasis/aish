"""File read/write/edit primitives with unified diffs for the approval gate.

Pure functions: plan_* compute what would change (old, new, diff) without
touching disk; commit() performs the write. The agent shows the diff, gets
approval, then commits — so nothing is written unseen.
"""

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

from .tools import truncate

READ_MAX_LINES = 2000


def resolve(path: str, cwd: str) -> Path:
    expanded = os.path.expanduser(path)
    p = Path(expanded)
    return p if p.is_absolute() else Path(cwd) / p


# Directories/files that commonly hold credentials. Reading these auto-approved
# would let an injected read_file exfiltrate secrets into context unseen, so
# they are routed through an explicit prompt instead.
_SENSITIVE_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", "gcloud"})
_SENSITIVE_NAMES = frozenset(
    {
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", "credentials",
        ".pgpass", ".git-credentials", ".htpasswd", "secrets", ".npmrc",
    }
)
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".env")


def is_sensitive_path(path: str, cwd: str) -> bool:
    """True for paths that commonly hold secrets (SSH/AWS keys, .env, .pem …).
    Advisory: used to require a prompt before an auto-approved read_file
    touches them — never a hard block."""
    target = resolve(path, cwd)
    name = target.name.lower()
    if {p.lower() for p in target.parts} & _SENSITIVE_DIRS:
        return True
    if name in _SENSITIVE_NAMES or name.startswith(".env"):
        return True
    return name.endswith(_SENSITIVE_SUFFIXES)


def read_file(path: str, cwd: str, offset: int = 1, limit: int = READ_MAX_LINES) -> str:
    target = resolve(path, cwd)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"ERROR: no such file: {target}"
    except IsADirectoryError:
        return f"ERROR: {target} is a directory"
    except OSError as exc:
        return f"ERROR: cannot read {target}: {exc}"

    lines = text.splitlines()
    offset = max(1, offset)
    limit = max(1, min(limit, READ_MAX_LINES))
    if offset > len(lines) and lines:
        return f"ERROR: offset {offset} is past the end of the file ({len(lines)} lines)"
    window = lines[offset - 1 : offset - 1 + limit]
    numbered = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(window, offset))
    remaining = len(lines) - (offset - 1 + len(window))
    if remaining > 0:
        numbered += (
            f"\n[... {remaining} more lines; call read_file again with "
            f"offset={offset + len(window)} to continue]"
        )
    return numbered or "(empty file)"


@dataclass
class WritePlan:
    target: Path
    display: str
    old: str
    new: str
    is_new: bool
    error: str | None = None

    @property
    def diff(self) -> str:
        return make_diff(self.old, self.new, self.display)

    @property
    def added(self) -> int:
        lines = self.diff.splitlines()
        return sum(1 for ln in lines if ln.startswith("+") and ln[1:2] != "+")

    @property
    def removed(self) -> int:
        lines = self.diff.splitlines()
        return sum(1 for ln in lines if ln.startswith("-") and ln[1:2] != "-")


def make_diff(old: str, new: str, display: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{display}",
        tofile=f"b/{display}",
        n=3,
    )
    text = "".join(diff)
    # difflib omits a trailing newline marker; keep the diff itself readable.
    return truncate(text, head=6000, tail=1000)


def plan_write(path: str, content: str, cwd: str) -> WritePlan:
    target = resolve(path, cwd)
    is_new = not target.exists()
    old = ""
    if not is_new:
        if target.is_dir():
            return WritePlan(target, path, "", "", False, error=f"{target} is a directory")
        try:
            old = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return WritePlan(target, path, "", "", False, error=f"cannot read {target}: {exc}")
    new = content if content.endswith("\n") or not content else content + "\n"
    return WritePlan(target, path, old, new, is_new)


def plan_edit(path: str, old_str: str, new_str: str, cwd: str) -> WritePlan:
    target = resolve(path, cwd)
    if not target.exists():
        return WritePlan(
            target, path, "", "", True, error=f"no such file: {target} (use write_file)"
        )
    try:
        old = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return WritePlan(target, path, "", "", False, error=f"cannot read {target}: {exc}")

    count = old.count(old_str)
    if count == 0:
        return WritePlan(
            target, path, old, old, False, error="old_str not found in file (verify exact text)"
        )
    if count > 1:
        return WritePlan(
            target, path, old, old, False,
            error=f"old_str appears {count} times — add surrounding context so it is unique",
        )
    new = old.replace(old_str, new_str, 1)
    return WritePlan(target, path, old, new, False)


def commit(plan: WritePlan) -> str:
    if plan.error:
        return f"ERROR: {plan.error}"
    try:
        plan.target.parent.mkdir(parents=True, exist_ok=True)
        plan.target.write_text(plan.new, encoding="utf-8")
    except OSError as exc:
        return f"ERROR: cannot write {plan.target}: {exc}"
    verb = "created" if plan.is_new else "updated"
    return f"{verb} {plan.target} (+{plan.added} -{plan.removed} lines)"
