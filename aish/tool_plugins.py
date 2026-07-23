"""Plugin tools: droppable TOOL.md manifests the model calls exactly like a
native tool. A tool is a folder ``<name>/TOOL.md`` (plus an optional wrapper
script and bundled files) under ``./.aish/tools/`` (project, wins on name
clash) or ``~/.config/aish/tools/`` (global).

Design (epic #141 — skills-primary, tools-as-scalpel): a tool exists ONLY for
a hot, shell-fragile, reliability-critical operation a documented skill snippet
can't do safely. Its one irreducible advantage over a skill's script: the model
never composes a shell string — validated JSON args go to the executable on
stdin, so free-text arguments cannot be mangled by shell quoting.

- **Schema on the way IN, prose on the way OUT.** Args are validated against
  the manifest schema; output is raw stdout+stderr with the exit code appended
  (no output schema, matching run_command's ``[exit code: N]`` convention).
- **Native and plugin tools are indistinguishable to the model** — this module
  emits the exact ``{"type":"function","function":{...}}`` shape native tools
  use, so ``agent._dispatch`` routes both the same way through the same gate.
- **Discovery mirrors skills' folder scan**; an invalid manifest is skipped
  (never crashes discovery) and its reason surfaced as a warning.
- ``mutating`` is declared per tool and is a floor, never authority: a
  read-only tool auto-runs; a mutating one is gated. A manifest that fails to
  declare it is invalid (fail-closed), never silently treated as read-only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

GLOBAL_TOOLS_DIR = Path.home() / ".config" / "aish" / "tools"
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_ARG_TYPES = {"string", "integer", "number", "boolean"}
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 900
_OUT_HEAD = 6000
_OUT_TAIL = 2000


def tool_dirs(cwd: str) -> list[Path]:
    return [Path(cwd) / ".aish" / "tools", GLOBAL_TOOLS_DIR]


@dataclass
class Tool:
    """One validated plugin tool. ``executable`` is either a bare PATH command
    or a ``./``-prefixed wrapper resolved inside ``dir``."""

    name: str
    description: str
    executable: str
    mutating: bool
    schema: dict  # arg name -> {"type": str, "required": bool, "description": str}
    timeout: int
    body: str
    dir: Path
    mtime: float = 0.0
    wraps: str = ""  # optional shell-command prefix this tool replaces (drift nudge)


def _truncate(text: str, head: int = _OUT_HEAD, tail: int = _OUT_TAIL) -> str:
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n… [{omitted} chars truncated] …\n{text[-tail:]}"


def _parse_bool(value: str) -> bool | None:
    s = value.strip().lower()
    if s in ("yes", "true", "1"):
        return True
    if s in ("no", "false", "0"):
        return False
    return None


def resolve_executable(tool_dir: Path, executable: str) -> str | None:
    """A bare name resolves on PATH; a path (``./x`` or containing ``/``) must
    resolve to a file INSIDE ``tool_dir`` and be executable — so a manifest can
    only ever run its own bundled wrapper, never an arbitrary absolute path."""
    executable = executable.strip()
    if not executable:
        return None
    if "/" not in executable:
        return shutil.which(executable)
    if os.path.isabs(executable):
        return None
    candidate = (tool_dir / executable).resolve()
    try:
        candidate.relative_to(tool_dir.resolve())
    except ValueError:
        return None  # escapes the tool dir
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _parse_tool(manifest: Path) -> tuple[Tool | None, list[str]]:
    """Parse+validate one TOOL.md. Returns (tool, errors); a non-empty errors
    list means the tool is skipped. The linter is deterministic and pure."""
    errors: list[str] = []
    try:
        text = manifest.read_text(encoding="utf-8")
        mtime = manifest.stat().st_mtime
    except OSError as exc:
        return None, [f"cannot read {manifest}: {exc}"]

    if not text.startswith("---"):
        return None, [f"{manifest}: missing YAML frontmatter"]
    parts = text.split("---", 2)
    if len(parts) != 3:
        return None, [f"{manifest}: malformed frontmatter"]
    _, front, body = parts

    fields: dict[str, str] = {}
    for line in front.strip().splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()

    tool_dir = manifest.parent
    name = fields.get("name", "") or tool_dir.name
    if not NAME_RE.match(name):
        errors.append(f"{manifest}: invalid name {name!r} (need [A-Za-z0-9_-], 1-64)")

    description = fields.get("description", "")
    if not description:
        errors.append(f"{manifest}: description is required")

    executable = fields.get("exec", "")
    if not executable:
        errors.append(f"{manifest}: exec is required")
    elif resolve_executable(tool_dir, executable) is None:
        errors.append(
            f"{manifest}: exec {executable!r} does not resolve "
            "(not on PATH, or not an executable wrapper inside the tool dir)"
        )

    mutating = _parse_bool(fields.get("mutating", ""))
    if mutating is None:
        errors.append(f"{manifest}: mutating must be declared as yes/no (fail-closed)")

    timeout = DEFAULT_TIMEOUT
    if "timeout" in fields:
        try:
            timeout = int(fields["timeout"])
            if not (1 <= timeout <= MAX_TIMEOUT):
                errors.append(f"{manifest}: timeout must be 1-{MAX_TIMEOUT}s")
        except ValueError:
            errors.append(f"{manifest}: timeout must be an integer")

    schema: dict = {}
    raw_schema = fields.get("schema", "").strip()
    if raw_schema:
        try:
            schema = json.loads(raw_schema)
        except json.JSONDecodeError as exc:
            errors.append(f"{manifest}: schema is not valid JSON ({exc})")
        else:
            errors.extend(_validate_schema(manifest, schema))

    if errors:
        return None, errors
    return (
        Tool(
            name=name,
            description=description,
            executable=executable,
            mutating=bool(mutating),
            schema=schema,
            timeout=timeout,
            body=body.strip(),
            dir=tool_dir,
            mtime=mtime,
            wraps=fields.get("wraps", "").strip(),
        ),
        [],
    )


def lint(manifest: Path) -> list[str]:
    """Public: the deterministic validation errors for a TOOL.md (empty =
    valid). Used by create_tool to refuse writing an invalid manifest."""
    return _parse_tool(manifest)[1]


def _validate_schema(manifest: Path, schema: object) -> list[str]:
    if not isinstance(schema, dict):
        return [f"{manifest}: schema must be a JSON object of arg -> spec"]
    errors = []
    for arg, spec in schema.items():
        if not isinstance(spec, dict):
            errors.append(f"{manifest}: schema arg {arg!r} must be an object")
            continue
        atype = spec.get("type")
        if atype not in _ARG_TYPES:
            errors.append(
                f"{manifest}: schema arg {arg!r} type {atype!r} "
                f"must be one of {sorted(_ARG_TYPES)}"
            )
        if "required" in spec and not isinstance(spec["required"], bool):
            errors.append(f"{manifest}: schema arg {arg!r} 'required' must be true/false")
    return errors


def to_tool_def(tool: Tool) -> dict:
    """The native tool-def shape, so the model cannot tell plugin from native."""
    properties = {}
    required = []
    for arg, spec in tool.schema.items():
        prop = {"type": spec.get("type", "string")}
        if spec.get("description"):
            prop["description"] = str(spec["description"])
        properties[arg] = prop
        if spec.get("required"):
            required.append(arg)
    parameters: dict = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        },
    }


def validate_args(tool: Tool, args: dict) -> str | None:
    """Structured error string (for the correct-and-retry loop) or None if the
    args satisfy the schema. Unknown args and missing required args are errors;
    types are checked leniently (ints accepted for number)."""
    problems = []
    for arg, spec in tool.schema.items():
        if spec.get("required") and arg not in args:
            problems.append(f"missing required arg {arg!r}")
    for arg, value in args.items():
        spec = tool.schema.get(arg)
        if spec is None:
            problems.append(f"unknown arg {arg!r}")
            continue
        if not _type_ok(spec.get("type"), value):
            problems.append(f"arg {arg!r} should be {spec.get('type')}, got {type(value).__name__}")
    if problems:
        allowed = ", ".join(sorted(tool.schema)) or "(none)"
        joined = "; ".join(problems)
        return f"ERROR: invalid args for {tool.name}: {joined}. Allowed args: {allowed}"
    return None


def _type_ok(atype: object, value: object) -> bool:
    if atype == "string":
        return isinstance(value, str)
    if atype == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if atype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if atype == "boolean":
        return isinstance(value, bool)
    return True


def execute(tool: Tool, args: dict, cwd: str) -> str:
    """Run the tool: validated args as JSON on stdin, raw output + exit code
    back. No shell — the args never pass through shell word-splitting."""
    exe = resolve_executable(tool.dir, tool.executable)
    if exe is None:
        return (
            f"ERROR: tool {tool.name!r} executable {tool.executable!r} could not be "
            "resolved (not on PATH, or not an executable wrapper inside the tool dir)."
        )
    try:
        proc = subprocess.run(
            [exe],
            input=json.dumps(args),
            capture_output=True,
            text=True,
            timeout=tool.timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: tool {tool.name!r} timed out after {tool.timeout}s"
    except OSError as exc:
        return f"ERROR: tool {tool.name!r} failed to start: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return f"{_truncate(out)}\n[exit code: {proc.returncode}]"


def discover(cwd: str) -> tuple[list[Tool], list[str]]:
    """All valid tools (project before global, first wins on name clash) plus a
    list of warnings for skipped invalid manifests."""
    tools: dict[str, Tool] = {}
    warnings: list[str] = []
    for directory in tool_dirs(cwd):
        try:
            subdirs = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            continue
        for sub in subdirs:
            manifest = sub / "TOOL.md"
            if not manifest.is_file():
                continue
            tool, errors = _parse_tool(manifest)
            if errors:
                warnings.extend(errors)
            elif tool is not None:
                tools.setdefault(tool.name, tool)
    return list(tools.values()), warnings


def signature(cwd: str) -> tuple:
    """Cheap change-detector for the per-iteration rescan: the set of TOOL.md
    paths and their mtimes. Rebuild the tool list only when this moves."""
    sig = []
    for directory in tool_dirs(cwd):
        try:
            subdirs = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            continue
        for sub in subdirs:
            manifest = sub / "TOOL.md"
            try:
                sig.append((str(manifest), manifest.stat().st_mtime))
            except OSError:
                continue
    return tuple(sig)
