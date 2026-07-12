"""Skill files: task-specific playbooks the model reads before using a tool.

A skill is a markdown file in ./.aish/skills/ (project, wins on name clash)
or ~/.config/aish/skills/ (global), optionally with frontmatter:

    ---
    name: sweepy
    description: one line shown in the system prompt
    ---
    body ...

Skills close the gap read_docs can't: --help shows flags, but not workflows,
conventions, or safety rules for a tool.
"""

import re
from pathlib import Path

GLOBAL_SKILLS_DIR = Path.home() / ".config" / "aish" / "skills"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def skill_dirs(cwd: str) -> list[Path]:
    return [Path(cwd) / ".aish" / "skills", GLOBAL_SKILLS_DIR]


def _parse(path: Path) -> tuple[str, str, str]:
    """(name, description, body) — name defaults to the filename, description
    to the first non-empty body line."""
    text = path.read_text(encoding="utf-8")
    name, description, body = path.stem, "", text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            _, front, body = parts
            for line in front.strip().splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "name" and value.strip():
                    name = value.strip()
                elif key.strip() == "description":
                    description = value.strip()
    if not description:
        for line in body.strip().splitlines():
            if line.strip():
                description = line.strip().lstrip("# ").strip()
                break
    return name, description, body.strip()


def list_skills(dirs: list[Path]) -> list[tuple[str, str]]:
    """(name, description) pairs; earlier dirs win on duplicate names."""
    seen: dict[str, str] = {}
    for directory in dirs:
        try:
            files = sorted(directory.glob("*.md"))
        except OSError:
            continue
        for path in files:
            try:
                name, description, _ = _parse(path)
            except OSError:
                continue
            seen.setdefault(name, description)
    return sorted(seen.items())


def load_skill(name: str, dirs: list[Path]) -> str:
    if not NAME_RE.match(name or ""):
        return f"ERROR: invalid skill name {name!r}"
    for directory in dirs:
        try:
            files = sorted(directory.glob("*.md"))
        except OSError:
            continue
        for path in files:
            try:
                skill_name, _, body = _parse(path)
            except OSError:
                continue
            if skill_name == name:
                return f"[skill: {name}]\n{body}"
    available = ", ".join(n for n, _ in list_skills(dirs)) or "none"
    return f"ERROR: no skill named {name!r}. Available skills: {available}"
